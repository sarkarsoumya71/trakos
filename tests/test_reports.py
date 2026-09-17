import tempfile
import unittest
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import bot
from expense_reports import money, period_starts, read_spending, render
from sms_import import Ledger
from sms_workflow import SMSWorkflow


def sheet(title, rows):
    ws = MagicMock()
    ws.title, ws.row_count = title, 1000
    ws.get.return_value = [bot.HEADER_ROW[:6]] + rows
    return ws


def expense(date, amount, category='Food', description='Cafe'):
    return [date, '12:00', amount, description, category, 'UPI']


class ReportTests(unittest.TestCase):
    def snapshot(self, now, sheets):
        sh = MagicMock()
        sh.worksheets.return_value = sheets
        with patch.object(bot, 'get_spreadsheet', return_value=sh):
            return read_spending(bot, now)

    def test_calendar_month_not_thirty_days_and_no_double_count_of_ledger(self):
        now = datetime(2026, 9, 17, 22, tzinfo=bot.TIMEZONE)
        august = sheet('August 2026', [expense('31/08/2026','999')])
        ledger = sheet('SMS Ledger', [expense('17/09/2026','999')])
        september = sheet('September 2026', [expense('01/09/2026','100.10'),
            expense('17/09/2026','200.20','Business'), expense('30/09/2026','888')])
        result = self.snapshot(now, [august, september, ledger])
        self.assertEqual(result['periods']['month']['total'], 30030)
        self.assertEqual(result['periods']['today']['total'], 20020)
        self.assertEqual(result['periods']['month']['categories'], {'Food':10010,'Business':20020})
        august.get.assert_not_called()
        ledger.get.assert_not_called()
        september.get.assert_called_once_with('A1:F1000')

    def test_week_starts_monday_and_crosses_month_boundary(self):
        result = self.snapshot(datetime(2026,9,2,22,tzinfo=bot.TIMEZONE), [
            sheet('August 2026',[expense('30/08/2026','500'),expense('31/08/2026','100')]),
            sheet('September 2026',[expense('01/09/2026','200'),expense('02/09/2026','300')])])
        self.assertEqual(result['periods']['week']['total'],60000)
        self.assertEqual(result['periods']['month']['total'],50000)
        self.assertEqual(result['periods']['today']['total'],30000)

    def test_year_boundary(self):
        result=self.snapshot(datetime(2027,1,1,22,tzinfo=bot.TIMEZONE),[
            sheet('December 2026',[expense('28/12/2026','100')]),
            sheet('January 2027',[expense('01/01/2027','200')])])
        self.assertEqual(result['periods']['week']['total'],30000)
        self.assertEqual(result['periods']['month']['total'],20000)

    def test_monday_week_equals_today(self):
        starts=period_starts(datetime(2026,9,21,12,tzinfo=bot.TIMEZONE))
        self.assertEqual(starts['week'],starts['today'])

    def test_invalid_rows_fail_instead_of_silently_understating_totals(self):
        now=datetime(2026,9,17,22,tzinfo=bot.TIMEZONE)
        for bad in ['NaN','Infinity','12.345','not a number','-100']:
            with self.assertRaises(ValueError):
                self.snapshot(now,[sheet('September 2026',[expense('17/09/2026','100'),expense('17/09/2026',bad)])])

    def test_sheet_error_does_not_return_partial_totals(self):
        ws=sheet('September 2026',[])
        ws.get.side_effect=RuntimeError('offline')
        with self.assertRaises(RuntimeError):
            self.snapshot(datetime(2026,9,17,tzinfo=bot.TIMEZONE),[ws])

    def test_month_mismatch_fails(self):
        with self.assertRaises(ValueError):
            self.snapshot(datetime(2026,9,17,tzinfo=bot.TIMEZONE),[sheet('September 2026',[expense('09/01/2026','100')])])

    def test_zero_today_still_shows_week_month_and_freshness(self):
        result=self.snapshot(datetime(2026,9,17,22,tzinfo=bot.TIMEZONE),[
            sheet('September 2026',[expense('16/09/2026','100')])])
        text=render(result,freshness='Latest SMS backup upload: 16 Sep 2026.',nightly=True)
        self.assertIn('Total: ₹0',text)
        self.assertIn('September 2026',text)
        self.assertIn('Food: ₹100',text)
        self.assertIn('16 Sep 2026',text)
        self.assertIn('not your available bank balance',text)

    def test_money_and_long_report_fit_telegram(self):
        self.assertEqual(money(123456),'₹1,234.56')
        rows=[expense('17/09/2026','1234.56',f'Category {i} '+('x'*200),'merchant '+('x'*200)) for i in range(100)]
        result=self.snapshot(datetime(2026,9,17,22,tzinfo=bot.TIMEZONE),[sheet('September 2026',rows)])
        text=render(result)
        self.assertLess(len(text.encode('utf-16-le'))//2,4096)
        self.assertIn('+92 more',text)
        self.assertIn('Remaining categories',text)


class ScheduleTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.env=patch.dict('os.environ',{'TRAKOS_DB_PATH':str(Path(self.temp.name)/'db.sqlite3'),
            'SMS_OWNER_USER_ID':'1','SMS_DRIVE_FOLDER_ID':'','DAILY_REPORT_ENABLED':'1','DAILY_REPORT_TIME':'22:00'})
        self.env.start()
        with patch.object(bot,'ALLOWED_USER_IDS','1'):
            self.flow=SMSWorkflow(bot)
        self.flow.telegram=SimpleNamespace(send_message=AsyncMock())
        self.now=datetime(2026,9,17,22,tzinfo=bot.TIMEZONE)
        starts=period_starts(self.now)
        self.snapshot={'as_of':self.now,'periods':{k:dict(start=v,total=0,count=0,categories=Counter(),entries=[]) for k,v in starts.items()}}

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    async def test_due_once_and_persistent_across_restart(self):
        with patch('expense_reports.read_spending',return_value=self.snapshot):
            self.assertFalse(await self.flow.daily_report_once(self.now.replace(hour=21,minute=59)))
            self.assertTrue(await self.flow.daily_report_once(self.now))
            self.flow.db=Ledger(self.flow.db.path)
            self.assertFalse(await self.flow.daily_report_once(self.now.replace(hour=23)))
        self.flow.telegram.send_message.assert_awaited_once()
        self.assertFalse(self.flow.db.report_delivered(2,'2026-09-17'))

    async def test_failed_delivery_retries(self):
        self.flow.telegram.send_message.side_effect=[RuntimeError('offline'),None]
        with patch('expense_reports.read_spending',return_value=self.snapshot):
            with self.assertRaises(RuntimeError): await self.flow.daily_report_once(self.now)
            self.assertFalse(self.flow.db.report_delivered(1,'2026-09-17'))
            self.assertTrue(await self.flow.daily_report_once(self.now))

    async def test_sheet_failure_does_not_send_zero_report(self):
        with patch('expense_reports.read_spending',side_effect=RuntimeError('offline')):
            with self.assertRaises(RuntimeError): await self.flow.daily_report_once(self.now)
        self.flow.telegram.send_message.assert_not_awaited()

    async def test_failed_drive_refresh_discloses_stale_data(self):
        self.flow.folder_id='folder'
        with patch.object(self.flow,'drive_import',side_effect=RuntimeError('offline')),patch('expense_reports.read_spending',return_value=self.snapshot):
            await self.flow.daily_report_once(self.now)
        self.assertIn('SMS refresh failed',self.flow.telegram.send_message.call_args.kwargs['text'])

    async def test_india_time_and_next_day_delivery(self):
        with patch('expense_reports.read_spending',return_value=self.snapshot):
            self.assertTrue(await self.flow.daily_report_once(datetime(2026,9,17,16,30,tzinfo=timezone.utc)))
            self.assertTrue(await self.flow.daily_report_once(datetime(2026,9,18,16,30,tzinfo=timezone.utc)))
        self.assertEqual(self.flow.telegram.send_message.await_count,2)

    async def test_feature_disabled(self):
        with patch.dict('os.environ',{'DAILY_REPORT_ENABLED':'0'}):
            self.assertFalse(await self.flow.daily_report_once(self.now))
        self.flow.telegram.send_message.assert_not_awaited()

    async def test_freshness_persists(self):
        self.flow.db.set_sync_state('latest_backup_upload','2026-09-17T12:00:00Z')
        with patch.object(bot,'ALLOWED_USER_IDS','1'):
            restored=SMSWorkflow(bot)
        self.assertIn('17 Sep 2026, 17:30',restored.report_freshness())

    async def test_old_backup_explicitly_warns_about_incomplete_today(self):
        self.flow.latest_backup_upload='2026-09-16T12:00:00Z'
        self.assertIn('today’s total may be incomplete',self.flow.report_freshness(self.now))
        self.flow.latest_backup_upload='2026-09-17T12:00:00Z'
        self.assertNotIn('incomplete',self.flow.report_freshness(self.now))

    async def test_commands_require_owner_and_private_chat(self):
        update=MagicMock()
        update.effective_user.id=2
        update.message.reply_text=AsyncMock()
        with patch.object(bot,'ALLOWED_USER_IDS','1'),patch('expense_reports.read_spending') as read:
            await bot.cmd_check(update,None)
            read.assert_not_called()
            update.effective_user.id=1
            update.effective_chat.type='group'
            await bot.cmd_check(update,None)
            read.assert_not_called()
        self.assertIn('private chat',update.message.reply_text.call_args.args[0])

    async def test_check_refreshes_before_reading_and_discloses_failure(self):
        update=MagicMock()
        update.effective_user.id=1
        update.effective_chat.type='private'
        update.message.reply_text=AsyncMock()
        flow=SimpleNamespace(owner=1,folder_id='folder',refresh_for_report=AsyncMock(side_effect=RuntimeError('offline')),
                             report_freshness=lambda:'Latest backup: yesterday')
        with patch.object(bot,'ALLOWED_USER_IDS','1'),patch.object(bot,'SMS_WORKFLOW',flow),patch('expense_reports.read_spending',return_value=self.snapshot):
            await bot.cmd_check(update,None)
        flow.refresh_for_report.assert_awaited_once()
        self.assertIn('does not start a backup on your phone',update.message.reply_text.call_args_list[0].args[0])
        self.assertIn('Could not refresh SMS',update.message.reply_text.call_args.args[0])

    async def test_other_expenses_show_missing_merchant_reason_and_id(self):
        from test_sms import xml,DEBIT
        self.flow.db.import_xml(xml(DEBIT),1)
        self.flow.db.resolve(1,1,'expense','Other')
        with self.flow.db.connect() as db:
            db.execute("UPDATE transactions SET merchant='',reason='Merchant missing; recorded as Other' WHERE id=1")
        tx=self.flow.db.get(1,1)
        self.snapshot['periods']['month']['entries']=[{'date':self.now.date(),'paise':10000,'category':'Other',
            'description':'HDFC Transaction','sheet':'September 2026','row':2}]
        sh,ws=MagicMock(),MagicMock()
        sh.worksheet.return_value=ws
        ws.col_values.return_value=['Raw Input',f'[{self.flow.marker(tx)}]']
        with patch('expense_reports.read_spending',return_value=self.snapshot),patch.object(bot,'get_spreadsheet',return_value=sh):
            text=self.flow.other_expenses(1,'month',1,self.now)
        self.assertIn('SMS #1',text)
        self.assertIn('Not provided in the bank alert',text)
        self.assertIn('Why Other:',text)
        self.assertIn('/smscategory ID Food',text)

    async def test_other_expenses_paginate_and_preserve_total(self):
        self.snapshot['periods']['month']['entries']=[{'date':self.now.date(),'paise':10000,'category':'Other',
            'description':'Test purchase '+('x'*100),'sheet':'September 2026','row':i+2} for i in range(11)]
        sh,ws=MagicMock(),MagicMock()
        sh.worksheet.return_value=ws
        ws.col_values.return_value=['Raw Input']+['manual']*11
        with patch('expense_reports.read_spending',return_value=self.snapshot),patch.object(bot,'get_spreadsheet',return_value=sh):
            first=self.flow.other_expenses(1,'month',1,self.now)
            second=self.flow.other_expenses(1,'month',2,self.now)
            invalid=self.flow.other_expenses(1,'month',3,self.now)
        self.assertEqual(first.count('Sheet row'),8)
        self.assertEqual(second.count('Sheet row'),3)
        self.assertIn('11 entries · total ₹1,100',first)
        self.assertIn('Next page: /others month 2',first)
        self.assertIn('Choose a page',invalid)
        self.assertLess(len(first.encode('utf-16-le'))//2,4096)

    async def test_others_command_rejects_unauthorized_and_bad_page(self):
        update=MagicMock()
        update.effective_user.id=2
        update.effective_chat.type='private'
        update.message.reply_text=AsyncMock()
        with patch.object(bot,'ALLOWED_USER_IDS','1'),patch.object(self.flow,'other_expenses') as details:
            await self.flow.others(update,SimpleNamespace(args=[]))
            details.assert_not_called()
            update.effective_user.id=1
            await self.flow.others(update,SimpleNamespace(args=['month','-1']))
            details.assert_not_called()
        self.assertIn('Use /others',update.message.reply_text.call_args.args[0])
