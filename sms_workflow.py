"""Telegram review, Google Drive ingestion and retryable Sheets projections."""
import asyncio
import csv
import io
import json
import logging
import os
import re
from datetime import datetime
from decimal import Decimal
from collections import defaultdict
from pathlib import Path

import gspread
from google.auth.transport.requests import AuthorizedSession
from google.oauth2.service_account import Credentials
from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup
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
        self.report_task = None
        self.daily_report_time = os.environ.get('DAILY_REPORT_TIME', '22:00')
        if not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d', self.daily_report_time):
            raise ValueError('DAILY_REPORT_TIME must be HH:MM in India time')
        self.last_drive_check = self.db.get_sync_state('last_drive_check') or 'Not checked yet'
        self.latest_backup_upload = self.db.get_sync_state('latest_backup_upload')
        self.last_error = None
        self.telegram = None

    @property
    def auto_enabled(self):
        return os.environ.get('SMS_AUTO_APPROVE', '0') == '1'

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

        Approved rows enter monthly totals after manual or automatic categorization.
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
        transactions = self.db.list(owner, limit=100000)
        groups = defaultdict(list)
        for tx in transactions:
            if tx['status'] == 'approved' and not tx['exported']:
                groups[tx['occurred_at'][:7]].append(tx)
        for pending in groups.values():
            monthly = self.bot.get_month_sheet(sh, datetime.fromisoformat(pending[0]['occurred_at']))
            raw_values = monthly.col_values(7)
            rows = []
            for tx in pending:
                marker = self.marker(tx)
                if any(f'[{marker}]' in str(raw) for raw in raw_values):
                    continue
                stamp = datetime.fromisoformat(tx['occurred_at'])
                rows.append([stamp.strftime('%d/%m/%Y'), stamp.strftime('%H:%M'), tx['amount_paise'] / 100,
                    self.bot.to_title_case(tx['merchant'] or f"{tx['bank']} Transaction"), tx['category'], tx['payment'],
                    f"{self.db.body(tx['id'], owner)}\n[{marker}]"])
            if len(rows) == 1:
                self.bot.append_to_data_area(monthly, rows[0])
            elif rows:
                self.bot.append_many_to_data_area(monthly, rows)
            for tx in pending:
                self.db.mark_exported(tx['id'], owner)
        positions = {row[0]: i for i, row in enumerate(existing, 1) if row and row[0]}
        next_row = len(existing) + 1
        changes = []
        for tx in transactions:
            stamp = datetime.fromisoformat(tx['occurred_at'])
            marker = self.marker(tx)
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

    def monthly_records(self, pending):
        sh = self.bot.get_spreadsheet()
        owner = pending[0]['owner']
        markers = {self.marker(tx): tx['id'] for tx in self.db.list(owner, limit=100000)}
        names = sorted({datetime.fromisoformat(tx['occurred_at']).strftime('%B %Y') for tx in pending if tx['direction'] == 'debit'})
        records = []
        with self.bot.SHEET_LOCK:
            for name in names:
                try:
                    ws = sh.worksheet(name)
                except gspread.exceptions.WorksheetNotFound:
                    continue
                for row in ws.get(f'A2:G{ws.row_count}'):
                    if not row or not row[0]:
                        continue
                    if len(row) < 5:
                        raise ValueError('Incomplete existing expense row')
                    stamp = datetime.strptime(row[0], '%d/%m/%Y')
                    if stamp.strftime('%B %Y') != name:
                        raise ValueError('Expense date does not match month tab')
                    amount = Decimal(str(row[2]).replace(',', '')) * 100
                    if not amount.is_finite() or amount != amount.to_integral_value():
                        raise ValueError('Invalid existing expense amount')
                    raw = row[6] if len(row) > 6 else ''
                    marker = re.search(r'\[(trakos-sms:[a-f0-9]+)\]', raw)
                    records.append({'date': stamp.strftime('%Y-%m-%d'), 'amount_paise': int(amount),
                        'description': row[3], 'category': row[4], 'raw': raw,
                        'sms_id': markers.get(marker[1]) if marker else None})
        return records

    async def complete_sync(self, owner, telegram):
        if self.auto_enabled:
            from sms_auto import process
            await process(self, owner)
        await asyncio.to_thread(self.export_views, owner)
        if self.auto_enabled and os.environ.get('SMS_NOTIFY_ENABLED', '1') == '1':
            from sms_auto import notify
            await notify(self, telegram, owner)

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
        backup_uploads = []
        with AuthorizedSession(creds) as session:
            folder = session.get(f'https://www.googleapis.com/drive/v3/files/{self.folder_id}',
                                 params={'fields': 'id,mimeType'}, timeout=30)
            folder.raise_for_status()
            if folder.json().get('mimeType') != 'application/vnd.google-apps.folder':
                raise ValueError('The configured SMS Drive source must be a folder')
            token = None
            while True:
                params = {'q': f"'{self.folder_id}' in parents and trashed=false",
                          'fields': 'nextPageToken,files(id,name,size,modifiedTime)', 'pageSize': 100}
                if token:
                    params['pageToken'] = token
                response = session.get('https://www.googleapis.com/drive/v3/files', params=params, timeout=30)
                response.raise_for_status()
                page = response.json()
                for file in page.get('files', []):
                    if not file['name'].lower().startswith('sms') or not file['name'].lower().endswith('.xml'):
                        continue
                    if file.get('modifiedTime'):
                        backup_uploads.append(file['modifiedTime'])
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
        self.db.set_sync_state('last_drive_check', self.last_drive_check)
        if backup_uploads:
            self.latest_backup_upload = max(backup_uploads)
            self.db.set_sync_state('latest_backup_upload', self.latest_backup_upload)
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
        await update.message.reply_text('Reading the SMS backup. Automatic categorization is enabled.' if self.auto_enabled else 'Reading the SMS backup. Transactions will be staged for review.')
        try:
            file = await doc.get_file()
            payload = bytes(await file.download_as_bytearray())
            async with self.lock:
                report = await asyncio.to_thread(self.db.import_xml, payload, update.effective_user.id, self.bot.guess_category_keywords)
            await update.message.reply_text(self.report_text([report]) + '\nUse /unparsed for unsupported bank alerts.')
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
                await self.complete_sync(update.effective_user.id, context.bot)
            await update.message.reply_text('Sheet synced. Automatic expenses are recorded; /review is only for exceptions.' if self.auto_enabled else 'Sheet synced. Only approved expenses enter monthly totals. Use /review for pending entries.')
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
        buttons.append([InlineKeyboardButton('Transfer between my accounts', callback_data=f"sms:{tx['id']}:transfer")])
        if tx['direction'] == 'debit':
            buttons.append([InlineKeyboardButton('Credit-card bill payment', callback_data=f"sms:{tx['id']}:card_payment")])
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
            f"\nAutomatic categorization: {'On' if self.auto_enabled else 'Off'}\n" +
            f"Daily report: {self.daily_report_time} IST" + ('\n' if os.environ.get('DAILY_REPORT_ENABLED', '0') == '1' else ' (off)\n') +
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

    async def change_category(self, update, context):
        if not self.allowed(update):
            return
        try:
            tx_id = int(context.args[0])
            category = next(c for c in self.bot.CATEGORY_LIST if c.casefold() == ' '.join(context.args[1:]).casefold())
            async with self.lock:
                def change():
                    tx = self.db.get(tx_id, update.effective_user.id)
                    if not tx or tx['status'] != 'approved':
                        raise ValueError('Approved expense not found')
                    with self.bot.SHEET_LOCK:
                        if tx['exported']:
                            sh = self.bot.get_spreadsheet()
                            ws = sh.worksheet(datetime.fromisoformat(tx['occurred_at']).strftime('%B %Y'))
                            indices = [i for i, raw in enumerate(ws.col_values(7), 1) if f'[{self.marker(tx)}]' in str(raw)]
                            if len(indices) != 1:
                                raise ValueError('Cannot identify the saved expense uniquely')
                            ws.update(f'E{indices[0]}', [[category]], value_input_option='RAW')
                        self.db.recategorize(tx_id, update.effective_user.id, category)
                    self.export_views(update.effective_user.id)
                await asyncio.to_thread(change)
            await update.message.reply_text(f'SMS #{tx_id}: category changed to {category}. Future expenses at this merchant will use it.')
        except (ValueError, IndexError, StopIteration):
            await update.message.reply_text('Use /smscategory ID Category for a saved expense, for example /smscategory 12 Food. See /categories.')
        except Exception as exc:
            log.warning('Category correction failed (%s)', type(exc).__name__)
            await update.message.reply_text('Category sync did not complete. Retry the same /smscategory command.')

    async def poll(self):
        interval = max(300, int(os.environ.get('SMS_POLL_SECONDS', '3600')))
        while True:
            try:
                async with self.lock:
                    await asyncio.to_thread(self.drive_import)
                    await self.complete_sync(self.owner, self.telegram)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.last_error = type(exc).__name__
                log.warning('Scheduled SMS sync failed (%s)', self.last_error)
            await asyncio.sleep(interval)

    def report_freshness(self, now=None):
        if self.latest_backup_upload:
            stamp = datetime.fromisoformat(self.latest_backup_upload.replace('Z', '+00:00')).astimezone(self.bot.TIMEZONE)
            text = f'Latest SMS backup upload: {stamp:%d %b %Y, %H:%M} IST.'
            if stamp.date() < (now or datetime.now(self.bot.TIMEZONE)).date():
                text += '\nNo SMS backup from today yet; today’s total may be incomplete.'
            return text
        return 'SMS backup upload time is not available yet.'

    async def refresh_for_report(self):
        async with self.lock:
            await asyncio.to_thread(self.drive_import)
            await self.complete_sync(self.owner, self.telegram)

    def other_expenses(self, owner, period, page, now):
        from expense_reports import clean, money, read_spending
        with self.bot.SHEET_LOCK:
            snapshot = read_spending(self.bot, now)
            entries = [row for row in snapshot['periods'][period]['entries'] if row['category'].casefold() == 'other']
            entries.sort(key=lambda row: (row['date'], row['row']), reverse=True)
            if not entries:
                return f'No Other expenses recorded for {period}.'
            pages = (len(entries) + 7) // 8
            if page < 1 or page > pages:
                return f'Choose a page from 1 to {pages}: /others {period} 1'
            selected = entries[(page-1)*8:page*8]
            sh = self.bot.get_spreadsheet()
            raw_columns = {name: sh.worksheet(name).col_values(7) for name in {row['sheet'] for row in selected}}
            markers = {self.marker(tx): tx for tx in self.db.list(owner, limit=100000)}
            lines = [f'Other expenses · {period} · page {page}/{pages}',
                     f"{len(entries)} entries · total {money(sum(row['paise'] for row in entries))}",
                     'These expenses were counted, but a more specific category was not established.']
            for entry in selected:
                values = raw_columns[entry['sheet']]
                raw = str(values[entry['row']-1]) if entry['row'] <= len(values) else ''
                match = re.search(r'\[(trakos-sms:[a-f0-9]+)\]', raw)
                tx = markers.get(match[1]) if match else None
                lines.append('')
                label = f"SMS #{tx['id']}" if tx else f"Sheet row {entry['row']}"
                lines.append(f"{label} · {entry['date']:%d %b} · {money(entry['paise'])}")
                if tx:
                    lines.append(f"{tx['bank']} · account ending {tx['account'] or 'unavailable'} · {tx['payment']}")
                    lines.append(f"Merchant: {clean(tx['merchant']) if tx['merchant'] else 'Not provided in the bank alert'}")
                    if not tx['merchant']:
                        reason = 'The bank alert gives no merchant or purchase description.'
                    elif tx['reason'].startswith('GPT-OSS'):
                        reason = 'The merchant description was too unclear to classify confidently.'
                    elif 'corrected by you' in tx['reason']:
                        reason = 'This category was chosen by you.'
                    else:
                        reason = 'Recorded as Other; no more classification detail is available.'
                    lines.append('Why Other: ' + reason)
                else:
                    lines.append('Description: ' + (clean(entry['description']) or 'Not provided'))
                    lines.append('Why Other: The sheet uses Other; no linked SMS details are available.')
            lines.extend(['', 'See a bank alert: /smsentry ID', 'Correct an SMS category: /smscategory ID Food',
                          'For sheet-only entries, edit Category in /sheet.'])
            if page < pages:
                lines.append(f'Next page: /others {period} {page+1}')
            return '\n'.join(lines)

    async def others(self, update, context):
        if not self.allowed(update):
            return
        args = list(context.args)
        period = args.pop(0).lower() if args and args[0].lower() in ('today', 'week', 'month') else 'month'
        try:
            if len(args) > 1:
                raise ValueError('Too many arguments')
            page = int(args[0]) if args else 1
            if page < 1:
                raise ValueError('Invalid page')
        except ValueError:
            await update.message.reply_text('Use /others, /others today, /others week, or /others month 2.')
            return
        try:
            async with self.lock:
                text = await asyncio.to_thread(self.other_expenses, update.effective_user.id, period, page, datetime.now(self.bot.TIMEZONE))
            await update.message.reply_text(text)
        except Exception as exc:
            log.warning('Other expense details failed (%s)', type(exc).__name__)
            await update.message.reply_text('Could not read complete Other expense details. Please retry.')

    async def daily_report_once(self, now=None):
        if os.environ.get('DAILY_REPORT_ENABLED', '0') != '1':
            return False
        now = now or datetime.now(self.bot.TIMEZONE)
        now = now.astimezone(self.bot.TIMEZONE)
        report_date = now.date().isoformat()
        if now.strftime('%H:%M') < self.daily_report_time or self.db.report_delivered(self.owner, report_date):
            return False
        from expense_reports import read_spending, render
        async with self.lock:
            if self.db.report_delivered(self.owner, report_date):
                return False
            refresh_failed = False
            if self.folder_id:
                try:
                    await asyncio.to_thread(self.drive_import)
                    await self.complete_sync(self.owner, self.telegram)
                except Exception as exc:
                    self.last_error = type(exc).__name__
                    log.warning('Pre-report SMS refresh failed (%s)', self.last_error)
                    refresh_failed = True
            snapshot = await asyncio.to_thread(read_spending, self.bot, now)
            freshness = self.report_freshness(now)
            if refresh_failed:
                freshness += '\nSMS refresh failed; this report covers entries already saved in the sheet.'
            await self.telegram.send_message(chat_id=self.owner,
                text=render(snapshot, freshness=freshness, nightly=True))
            self.db.mark_report_delivered(self.owner, report_date)
            log.info('Daily spending report delivered for %s', report_date)
        return True

    async def report_loop(self):
        while True:
            try:
                await self.daily_report_once()
                delay = 60
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning('Daily spending report failed (%s); retrying in five minutes', type(exc).__name__)
                delay = 300
            await asyncio.sleep(delay)

    async def start(self, app):
        self.telegram = app.bot
        try:
            await app.bot.set_my_commands([BotCommand(name, description) for name, description in [
                ('check', 'Refresh Drive and show today, week and month'),
                ('others', 'Explain Other expenses and why they lack a category'),
                ('today', "Today's spending, purchases and categories"),
                ('week', 'Spending from Monday through today'),
                ('month', 'Current calendar month from the 1st'),
                ('syncsms', 'Check Drive and record new SMS transactions'),
                ('smsstatus', 'Import status and daily report schedule'),
                ('sheet', 'Open the expense sheet'),
                ('smscategory', 'Correct a saved category: ID Category'),
                ('review', 'Review exceptional or possible duplicate entries'),
                ('categories', 'Show expense categories'),
                ('help', 'Show all commands')]])
        except Exception as exc:
            log.warning('Telegram command menu update failed (%s)', type(exc).__name__)
        if self.folder_id:
            self.poll_task = asyncio.create_task(self.poll())
        if os.environ.get('DAILY_REPORT_ENABLED', '0') == '1':
            self.report_task = asyncio.create_task(self.report_loop())
            log.info('Daily spending reports enabled at %s IST', self.daily_report_time)

    async def stop(self, app):
        for task in (self.poll_task, self.report_task):
            if not task:
                continue
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    def register(self, app):
        for command, handler in [('syncsms', self.sync), ('review', self.review),
            ('smsstatus', self.status), ('unparsed', self.unparsed), ('smsentry', self.entry),
            ('retrysms', self.retry), ('exportledger', self.export_csv)]:
            app.add_handler(CommandHandler(command, handler))
        app.add_handler(CommandHandler('smscategory', self.change_category))
        app.add_handler(CommandHandler('others', self.others))
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
