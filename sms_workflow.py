"""Telegram review, Google Drive ingestion and retryable Sheets projections."""
import asyncio
import csv
import io
import json
import logging
import os
import re
from datetime import datetime
from pathlib import Path

import gspread
from google.auth.transport.requests import AuthorizedSession
from google.oauth2.service_account import Credentials
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import CallbackQueryHandler, CommandHandler, MessageHandler, filters

from sms_import import Ledger, MAX_BYTES, fingerprint

log = logging.getLogger('trakos.sms')
LEDGER_HEADER = ['ID', 'Date', 'Time', 'Amount', 'Direction', 'Bank', 'Account ending',
                 'Merchant', 'Reference', 'Payment', 'Type', 'Category', 'Status',
                 'Possible duplicate', 'Original SMS']


def safe_csv(value):
    text = str(value if value is not None else '')
    return "'" + text if text.lstrip().startswith(('=', '+', '-', '@')) else text


class SMSWorkflow:
    def __init__(self, bot):
        self.bot = bot
        self.db = Ledger(os.environ.get('TRAKOS_DB_PATH', './data/trakos.sqlite3'))
        self.folder_id = os.environ.get('SMS_DRIVE_FOLDER_ID', '').strip()
        self.owner = int(os.environ.get('SMS_OWNER_USER_ID') or bot.ALLOWED_USER_IDS.split(',')[0])
        if not bot.is_authorized(self.owner):
            raise ValueError('SMS_OWNER_USER_ID must be an allowed Telegram user')
        if self.folder_id and not re.fullmatch(r'[\w-]+', self.folder_id):
            raise ValueError('SMS_DRIVE_FOLDER_ID must be the folder ID, not the URL')
        self.lock = asyncio.Lock()
        self.poll_task = None
        self.last_drive_check = 'Not checked yet'
        self.last_error = None

    def allowed(self, update):
        return (update.effective_user and self.bot.is_authorized(update.effective_user.id)
                and update.effective_chat and update.effective_chat.type == 'private')

    def marker(self, tx):
        return 'trakos-sms:' + fingerprint(tx['owner'], tx['bank'], tx['account'],
                                         tx['reference'], tx['occurred_at'], tx['amount_paise'],
                                         tx['direction'], self.db.body(tx['id'], tx['owner']))[:32]

    def export_views(self, owner):
        with self.bot.SHEET_LOCK:
            return self._export_views(owner)

    def _export_views(self, owner):
        """One process, serialized by lock; a stable marker makes retries idempotent.

        Imported rows never contribute to monthly totals until explicitly approved.
        Existing monthly headers and H:I summaries are preserved.
        """
        sh = self.bot.get_spreadsheet()
        try:
            ws = sh.worksheet('SMS Ledger')
        except gspread.exceptions.WorksheetNotFound:
            ws = sh.add_worksheet(title='SMS Ledger', rows=1000, cols=len(LEDGER_HEADER))
            ws.update('A1:O1', [LEDGER_HEADER], value_input_option='RAW')
            ws.freeze(rows=1)
        existing = ws.get_all_values()
        if not existing or existing[0][:len(LEDGER_HEADER)] != LEDGER_HEADER:
            raise ValueError('SMS Ledger has unexpected headers; refusing to overwrite it')
        positions = {row[0]: i for i, row in enumerate(existing, 1) if row and row[0]}
        next_row = len(existing) + 1
        changes = []
        for tx in self.db.list(owner, limit=100000):
            stamp = datetime.fromisoformat(tx['occurred_at'])
            marker = self.marker(tx)
            # Project expenses first. A crash after append is recovered by marker lookup.
            if tx['status'] == 'approved' and not tx['exported']:
                monthly = self.bot.get_month_sheet(sh, stamp)
                raw_values = monthly.col_values(7)
                if not any(f'[{marker}]' in str(raw) for raw in raw_values):
                    self.bot.append_to_data_area(monthly, [stamp.strftime('%d/%m/%Y'),
                        stamp.strftime('%H:%M'), tx['amount_paise'] / 100,
                        self.bot.to_title_case(tx['merchant'] or f"{tx['bank']} Transaction"),
                        tx['category'], tx['payment'], f"{self.db.body(tx['id'], owner)}\n[{marker}]"])
                self.db.mark_exported(tx['id'], owner)
            row = [marker, stamp.strftime('%d/%m/%Y'), stamp.strftime('%H:%M'),
                   tx['amount_paise'] / 100, tx['direction'], tx['bank'], tx['account'],
                   tx['merchant'], tx['reference'], tx['payment'], tx['kind'], tx['category'] or '',
                   tx['status'], tx['possible_duplicate'] or '', self.db.body(tx['id'], owner)]
            index = positions.get(marker)
            if index is None:
                index = next_row
                next_row += 1
            elif [str(x) for x in row] == existing[index - 1]:
                continue
            changes.append({'range': f'A{index}:O{index}', 'values': [row]})
        if next_row - 1 > ws.row_count:
            ws.add_rows(next_row - 1 - ws.row_count + 100)
        for start in range(0, len(changes), 200):
            ws.batch_update(changes[start:start + 200], value_input_option='RAW')

    def manual_candidates(self, tx):
        stamp = datetime.fromisoformat(tx['occurred_at'])
        try:
            ws = self.bot.get_spreadsheet().worksheet(stamp.strftime('%B %Y'))
        except gspread.exceptions.WorksheetNotFound:
            return []
        matches = []
        for index, row in enumerate(ws.get('A2:G'), 2):
            try:
                if row[0] != stamp.strftime('%d/%m/%Y') or abs(float(str(row[2]).replace(',', '')) * 100 - tx['amount_paise']) > 0.1:
                    continue
                if len(row) > 6 and f'[{self.marker(tx)}]' in row[6]:
                    continue
                matches.append(f"row {index}: {row[3]} ({row[4]})")
            except (ValueError, IndexError, TypeError):
                continue
        return matches

    def drive_import(self):
        if not self.folder_id:
            raise ValueError('Set SMS_DRIVE_FOLDER_ID to the shared backup folder ID.')
        creds = Credentials.from_service_account_info(json.loads(self.bot.GOOGLE_CREDS_JSON),
                    scopes=['https://www.googleapis.com/auth/drive.readonly'])
        reports = []
        with AuthorizedSession(creds) as session:
            token = None
            while True:
                params = {'q': f"'{self.folder_id}' in parents and trashed=false",
                          'fields': 'nextPageToken,files(id,name,size)', 'pageSize': 100}
                if token:
                    params['pageToken'] = token
                response = session.get('https://www.googleapis.com/drive/v3/files', params=params, timeout=30)
                response.raise_for_status()
                page = response.json()
                for file in page.get('files', []):
                    if not file['name'].lower().startswith('sms') or not file['name'].lower().endswith('.xml'):
                        continue
                    if int(file.get('size', 0)) > MAX_BYTES:
                        raise ValueError('A Drive backup is over 20 MB. Export SMS only.')
                    with session.get(f"https://www.googleapis.com/drive/v3/files/{file['id']}",
                                     params={'alt': 'media'}, stream=True, timeout=60) as download:
                        download.raise_for_status()
                        data = bytearray()
                        for chunk in download.iter_content(65536):
                            data.extend(chunk)
                            if len(data) > MAX_BYTES:
                                raise ValueError('Backup exceeds the 20 MB limit')
                    reports.append(self.db.import_xml(bytes(data), self.owner, self.bot.guess_category_keywords))
                token = page.get('nextPageToken')
                if not token:
                    break
        self.last_drive_check = datetime.now(self.bot.TIMEZONE).strftime('%d %b %H:%M')
        self.last_error = None
        return reports

    async def upload(self, update, context):
        if not self.allowed(update):
            return
        doc = update.message.document
        if not (doc.file_name or '').lower().endswith('.xml'):
            await update.message.reply_text('Send the SMS Backup & Restore SMS .xml file.')
            return
        if not doc.file_size or doc.file_size > MAX_BYTES:
            await update.message.reply_text('Use an SMS-only XML backup smaller than 20 MB.')
            return
        await update.message.reply_text('Reading the SMS backup. Transactions will be staged for review.')
        try:
            file = await doc.get_file()
            payload = bytes(await file.download_as_bytearray())
            async with self.lock:
                report = await asyncio.to_thread(self.db.import_xml, payload, update.effective_user.id, self.bot.guess_category_keywords)
            await update.message.reply_text(self.report_text([report]) + '\nUse /review to check transactions, /unparsed for unsupported bank alerts.')
            await self.retry(update, context)
        except Exception as exc:
            log.warning('SMS upload failed (%s)', type(exc).__name__)
            await update.message.reply_text('Import could not complete. Check that this is a valid SMS Backup & Restore SMS XML under 20 MB. No partial file was imported.')

    @staticmethod
    def report_text(reports):
        totals = {k: sum(r.get(k, 0) for r in reports) for k in ('new', 'duplicate', 'ignored', 'unparsed', 'possible_duplicate')}
        return (f"New: {totals['new']} · Duplicates skipped: {totals['duplicate']}\n"
                f"Ignored: {totals['ignored']} · Unsupported bank alerts: {totals['unparsed']}\n"
                f"Possible duplicates to check: {totals['possible_duplicate']}\n"
                f"Previously imported files: {sum(bool(r.get('already_imported')) for r in reports)}")

    async def sync(self, update, context):
        if not self.allowed(update):
            return
        if update.effective_user.id != self.owner:
            await update.message.reply_text('Drive imports belong to the configured SMS owner.')
            return
        await update.message.reply_text('Checking the SMS backup folder in Drive…')
        try:
            async with self.lock:
                reports = await asyncio.to_thread(self.drive_import)
            await update.message.reply_text(self.report_text(reports) if reports else 'No SMS XML backups found in that folder.')
            await self.retry(update, context)
        except Exception as exc:
            self.last_error = type(exc).__name__
            log.warning('Drive import failed (%s)', self.last_error)
            await update.message.reply_text('Drive import failed. Check the folder ID and share the folder with the service account as Viewer. Already imported files are safe to retry.')

    async def retry(self, update, context):
        if not self.allowed(update):
            return
        try:
            async with self.lock:
                await asyncio.to_thread(self.export_views, update.effective_user.id)
            await update.message.reply_text('Sheet synced. Only approved expenses enter monthly totals. Use /review for pending entries.')
        except Exception as exc:
            log.warning('SMS sheet projection failed (%s)', type(exc).__name__)
            await update.message.reply_text('Transactions are saved in the ledger; Sheet sync needs a retry. Use /retrysms. Do not re-enter the expenses manually.')

    async def review(self, update, context):
        if not self.allowed(update):
            return
        async with self.lock:
            entries = await asyncio.to_thread(self.db.list, update.effective_user.id, 'review', 1)
            if not entries:
                await update.message.reply_text('No SMS transactions awaiting review.')
                return
            tx = entries[0]
            try:
                candidates = await asyncio.to_thread(self.manual_candidates, tx)
            except Exception:
                await update.message.reply_text('Could not check existing sheet entries for duplicates. Retry /review when Sheets is available.')
                return
        stamp = datetime.fromisoformat(tx['occurred_at'])
        lines = [f"SMS #{tx['id']} · ₹{tx['amount_paise']/100:,.2f} · {tx['direction']}",
                 f"{tx['merchant'] or 'Merchant unknown'} · {stamp:%d %b %Y %H:%M}",
                 f"{tx['bank']} ••{tx['account'] or 'unknown'} · {tx['kind']}"]
        if tx['category']:
            lines.append(f"Suggested category: {tx['category']}")
        if tx['possible_duplicate']:
            lines.append(f"Possible duplicate or own transfer: SMS #{tx['possible_duplicate']}. Check /smsentry {tx['possible_duplicate']}.")
        if candidates:
            lines.append('Same amount and date already in the sheet:\n' + '\n'.join(candidates[:5]))
        if tx['kind'] in ('transfer', 'card_payment', 'cash_withdrawal'):
            lines.append('Usually excluded from spending to avoid counting the same money twice.')
        lines.append('\n' + self.db.body(tx['id'], update.effective_user.id)[:1600])
        buttons = []
        if tx['direction'] == 'debit':
            lines.append('\nChoose a category to count this as an expense:')
            categories = self.bot.CATEGORY_LIST
            for i in range(0, len(categories), 2):
                buttons.append([InlineKeyboardButton(categories[j], callback_data=f"sms:{tx['id']}:cat:{j}") for j in range(i, min(i+2, len(categories)))])
        buttons.append([InlineKeyboardButton('Keep, exclude from spending', callback_data=f"sms:{tx['id']}:exclude")])
        buttons.append([InlineKeyboardButton('Already counted / duplicate', callback_data=f"sms:{tx['id']}:duplicate")])
        await update.message.reply_text('\n'.join(lines), reply_markup=InlineKeyboardMarkup(buttons))

    async def callback(self, update, context):
        query = update.callback_query
        if not self.allowed(update):
            await query.answer('Not authorized.', show_alert=True)
            return
        await query.answer()
        parts = query.data.split(':')
        try:
            tx_id = int(parts[1])
            action = parts[2]
            category = None
            if action == 'cat':
                category_index = int(parts[3])
                if category_index < 0:
                    raise ValueError('Invalid category')
                category = self.bot.CATEGORY_LIST[category_index]
                action = 'expense'
            async with self.lock:
                changed = await asyncio.to_thread(self.db.resolve, tx_id, update.effective_user.id, action, category)
                if not changed:
                    await query.edit_message_text('This entry was already reviewed. Use /retrysms if its Sheet sync failed.')
                    return
                try:
                    await asyncio.to_thread(self.export_views, update.effective_user.id)
                    result = 'Saved and synced.'
                except Exception as exc:
                    log.warning('Review saved; projection failed (%s)', type(exc).__name__)
                    result = 'Decision saved. Sheet sync failed; use /retrysms.'
            await query.edit_message_text(f'SMS #{tx_id}: {result}\nUse /review for the next transaction.')
        except (ValueError, IndexError):
            await query.edit_message_text('Invalid review action. Use /review again.')

    async def status(self, update, context):
        if not self.allowed(update):
            return
        stats = self.db.stats(update.effective_user.id)
        await update.message.reply_text('SMS ledger\n' + '\n'.join(f'{k}: {v}' for k, v in stats.items()) +
            f"\nLast Drive check: {self.last_drive_check}\nLast Drive error: {self.last_error or 'None'}\n"
            'Use /syncsms, /review, /unparsed, /retrysms or /exportledger.')

    async def entry(self, update, context):
        if not self.allowed(update):
            return
        try:
            tx = self.db.get(int(context.args[0]), update.effective_user.id)
            if not tx:
                raise ValueError('Not found')
        except (ValueError, IndexError):
            await update.message.reply_text('Use /smsentry ID with an entry from your ledger.')
            return
        await update.message.reply_text(f"SMS #{tx['id']} · {tx['status']} · {tx['direction']} · ₹{tx['amount_paise']/100:,.2f}\n"
            f"{tx['bank']} ••{tx['account']} · {tx['occurred_at']}\n{self.db.body(tx['id'], update.effective_user.id)[:2500]}")

    async def unparsed(self, update, context):
        if not self.allowed(update):
            return
        rows = self.db.unparsed(update.effective_user.id)
        if not rows:
            await update.message.reply_text('No unsupported bank alerts stored.')
        for row in rows:
            await update.message.reply_text(f"{row['sender']} · {row['reason']}\n{row['body'][:2500]}\n\n"
                'Not counted. If this is an expense, log it as a normal Trakos message. Keep this alert for parser improvements.')

    async def export_csv(self, update, context):
        if not self.allowed(update):
            return
        rows = self.db.list(update.effective_user.id, limit=100000)
        buffer = io.StringIO(newline='')
        if rows:
            writer = csv.writer(buffer)
            writer.writerow(rows[0].keys())
            writer.writerows([[safe_csv(v) for v in row.values()] for row in rows])
        payload = io.BytesIO(buffer.getvalue().encode('utf-8-sig'))
        payload.name = 'trakos-sms-ledger.csv'
        await update.message.reply_document(payload, caption='SMS ledger, including review/excluded entries. Opens in Excel.')

    async def poll(self):
        interval = max(300, int(os.environ.get('SMS_POLL_SECONDS', '3600')))
        while True:
            try:
                async with self.lock:
                    await asyncio.to_thread(self.drive_import)
                    await asyncio.to_thread(self.export_views, self.owner)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.last_error = type(exc).__name__
                log.warning('Scheduled SMS sync failed (%s)', self.last_error)
            await asyncio.sleep(interval)

    async def start(self, app):
        if self.folder_id:
            self.poll_task = asyncio.create_task(self.poll())

    async def stop(self, app):
        if self.poll_task:
            self.poll_task.cancel()
            try:
                await self.poll_task
            except asyncio.CancelledError:
                pass

    def register(self, app):
        for command, handler in [('syncsms', self.sync), ('review', self.review),
            ('smsstatus', self.status), ('unparsed', self.unparsed), ('smsentry', self.entry),
            ('retrysms', self.retry), ('exportledger', self.export_csv)]:
            app.add_handler(CommandHandler(command, handler))
        app.add_handler(CallbackQueryHandler(self.callback, pattern=r'^sms:'))
        app.add_handler(MessageHandler(filters.Document.ALL, self.upload))


def configured(bot):
    if os.environ.get('SMS_IMPORT_ENABLED', '0') != '1':
        return None
    if os.environ.get('RAILWAY_ENVIRONMENT_ID'):
        mount = os.environ.get('RAILWAY_VOLUME_MOUNT_PATH')
        path = Path(os.environ.get('TRAKOS_DB_PATH', './data/trakos.sqlite3')).resolve()
        if not mount or not path.is_relative_to(Path(mount).resolve()):
            raise ValueError('SMS import on Railway needs a persistent Volume; set TRAKOS_DB_PATH inside its mount path.')
    return SMSWorkflow(bot)
