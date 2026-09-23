import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, AsyncMock

import bot
from expense_sheet import normalize_row, near_manual_matches
from sms_workflow import SMSWorkflow
from sms_auto import process
from test_sms import xml, DEBIT
from unittest.mock import MagicMock
from types import SimpleNamespace
from expense_edit import ExpenseEditor
from expense_sheet import HEADERS, ensure_layout
from expense_reports import read_spending
from datetime import datetime


class LayoutTests(unittest.TestCase):
    def test_growing_month_extends_highlights_and_validation(self):
        ws=MagicMock(row_count=2)
        ws.col_values.return_value=['Date','01/09/2026']
        with patch.object(bot,'format_month') as formatting,patch.object(bot,'ensure_dashboard') as dashboard,patch.object(bot,'sort_month_sheet'):
            bot.append_to_data_area(ws,['02/09/2026','12:00',10,'Cafe','Food','UPI','10 cafe'])
        ws.add_rows.assert_called_once()
        formatting.assert_called_once_with(ws,bot.CATEGORY_LIST)
        dashboard.assert_called_once_with(ws.spreadsheet,ws,bot.CATEGORY_LIST)

    def test_overview_stays_in_month_and_preserves_selector_on_refresh(self):
        from expense_sheet import ensure_dashboard, HIGHLIGHT_FORMULA
        sh=MagicMock()
        ws=MagicMock(id=123,col_count=24,row_count=200)
        ws.title='September 2026'
        ws.acell.return_value.value='Business'
        ws.get.return_value=[['Matches'],['=IFERROR(QUERY(A1:L,"select A",1),"")']]
        sh.fetch_sheet_metadata.return_value={'sheets':[{'properties':{'sheetId':123},
            'charts':[{'chartId':9,'spec':{'title':'September 2026 - confirmed spending'}}],
            'conditionalFormats':[{'booleanRule':{'condition':{'values':[{'userEnteredValue':HIGHLIGHT_FORMULA}]}}}],
            'columnGroups':[{'range':{'startIndex':6,'endIndex':9}},{'range':{'startIndex':10,'endIndex':12}}]}]}
        ensure_dashboard(sh,ws,bot.CATEGORY_LIST)
        sh.add_worksheet.assert_not_called()
        sh.worksheet.assert_not_called()
        self.assertEqual(ws.update.call_args_list[0].kwargs['values'][1][1],'Business')
        self.assertTrue(all(c.kwargs['range_name'].startswith('N') for c in ws.update.call_args_list))
        requests=sh.batch_update.call_args.args[0]['requests']
        self.assertFalse(any('addChart' in r or 'addDimensionGroup' in r for r in requests))
        visible_status=next(r['updateDimensionProperties'] for r in requests if r.get('updateDimensionProperties',{}).get('range',{}).get('startIndex')==9)
        self.assertFalse(visible_status['properties']['hiddenByUser'])
        highlight=next(r['updateConditionalFormatRule']['rule'] for r in requests if 'updateConditionalFormatRule' in r)
        self.assertEqual(highlight['ranges'][0]['sheetId'],ws.id)
        self.assertIn('$E2=$O$2',highlight['booleanRule']['condition']['values'][0]['userEnteredValue'])
        cleared=next(r['updateCells']['range'] for r in requests if 'updateCells' in r)
        self.assertEqual((cleared['startColumnIndex'],cleared['endColumnIndex']),(16,21))
        position=next(r['updateEmbeddedObjectPosition']['newPosition']['overlayPosition'] for r in requests if 'updateEmbeddedObjectPosition' in r)
        self.assertEqual(position['anchorCell'],{'sheetId':123,'rowIndex':23,'columnIndex':13})
        values=ws.update.call_args_list[0].kwargs['values']
        self.assertIn('Return pending', values[4][1])
        self.assertIn('Return pending', values[11][1])
        self.assertEqual(values[-1][0], 'Refunded (not counted)')
        chart=next(r['updateChartSpec']['spec'] for r in requests if 'updateChartSpec' in r)
        self.assertEqual(chart['pieChart']['series']['sourceRange']['sources'][0]['endRowIndex'],20)
        ws.get.return_value=[['My notes'],['Keep this']]
        ensure_dashboard(sh,ws,bot.CATEGORY_LIST)
        self.assertFalse(any('updateCells' in r for r in sh.batch_update.call_args.args[0]['requests']))
    def test_currency_format_does_not_accept_invalid_characters(self):
        from expense_sheet import paise
        self.assertEqual(paise('\u20b91,234.50'),123450)
        with self.assertRaises(Exception):
            paise('?123')

    def test_quota_retries_are_bounded(self):
        import gspread
        from expense_sheet import QuotaRetryClient
        response=MagicMock()
        response.json.return_value={'error':{'code':429,'message':'Quota'}}
        error=gspread.exceptions.APIError(response)
        client=object.__new__(QuotaRetryClient)
        with patch.object(gspread.HTTPClient,'request',side_effect=error) as request,patch('expense_sheet.time.sleep') as sleep:
            with self.assertRaises(gspread.exceptions.APIError):
                client.request('get','example')
        self.assertEqual(request.call_count,8)
        self.assertEqual(sleep.call_count,7)
        self.assertLessEqual(max(c.args[0] for c in sleep.call_args_list),32)

    def test_ambiguous_server_write_is_not_blindly_retried(self):
        import gspread
        from expense_sheet import QuotaRetryClient
        response=MagicMock()
        response.json.return_value={'error':{'code':503,'message':'Unavailable'}}
        client=object.__new__(QuotaRetryClient)
        with patch.object(gspread.HTTPClient,'request',side_effect=gspread.exceptions.APIError(response)) as request:
            with self.assertRaises(gspread.exceptions.APIError):
                client.request('post','example')
        request.assert_called_once()

    def test_migration_uses_numeric_amounts_and_preserves_literal_input(self):
        ws = MagicMock(title='September 2026', row_count=200, col_count=9)
        ws.title = 'September 2026'
        ws.row_values.return_value = HEADERS[:7]
        ws.get.return_value = [['16/09/2026','12:00','1,234.50','Cafe','Food','UPI','=literal']]
        with patch('expense_sheet.format_month'), patch('expense_sheet.ensure_dashboard'):
            ensure_layout(ws, bot.CATEGORY_LIST)
        cells = ws.spreadsheet.batch_update.call_args.args[0]['requests'][0]['updateCells']['rows'][1]['values']
        self.assertEqual(cells[2]['userEnteredValue'], {'numberValue':1234.5})
        self.assertEqual(cells[6]['userEnteredValue'], {'stringValue':'=literal'})

    def test_reports_separate_pending_duplicates_and_investment(self):
        ws=MagicMock(row_count=200,col_count=12)
        ws.title='September 2026'
        rows=[]
        for amount,status,treatment,category in [(100,'Confirmed','Expense','Food'),(200,'Needs details','Expense',''),(300,'Possible duplicate','Expense',''),(400,'Confirmed','Investment','Financial Investment'),(500,'Excluded','Expense','Food')]:
            rows.append(['16/09/2026','12:00',amount,'Test',category,'UPI','','','Mtest',status,treatment,'Manual'])
        ws.get.return_value=[HEADERS]+rows
        sh=MagicMock()
        sh.worksheets.return_value=[ws]
        with patch.object(bot,'get_spreadsheet',return_value=sh):
            month=read_spending(bot,datetime(2026,9,17,tzinfo=bot.TIMEZONE))['periods']['month']
        self.assertEqual([month[k] for k in ('total','pending','possible_duplicates','investments')],[10000,20000,30000,40000])
    def test_bank_message_has_own_column_and_blank_description(self):
        row = normalize_row(['16/09/2026','12:00',100,'CBI Transaction','Other','UPI',
                             'Bank alert\n[trakos-sms:' + 'a'*32 + ']'])
        self.assertEqual(row[3:5], ['', ''])
        self.assertEqual(row[6:10], ['', 'Bank alert', 'S'+'a'*32, 'Needs details'])

    def test_manual_input_preserved_and_investment_separate(self):
        row = normalize_row(['16/09/2026','12:00',100,'Mutual fund','Financial Investment','BANK','100 mutual fund'])
        self.assertEqual(row[6], '100 mutual fund')
        self.assertEqual(row[7], '')
        self.assertEqual(row[10], 'Investment')

    def test_rounded_amount_matches_only_manual_same_day(self):
        tx = {'occurred_at':'2026-09-16T12:00:00', 'amount_paise':123480}
        manual = {'date':'2026-09-16', 'amount_paise':123400, 'sms_id':None}
        self.assertEqual(near_manual_matches(tx, [manual]), [manual])
        self.assertEqual(near_manual_matches(tx, [{**manual, 'sms_id':1}]), [])


class UnknownTests(unittest.IsolatedAsyncioTestCase):
    async def test_unclear_merchant_remains_reviewable_not_other(self):
        with tempfile.TemporaryDirectory() as folder:
            with patch.dict('os.environ', {'TRAKOS_DB_PATH':str(Path(folder)/'db'), 'SMS_OWNER_USER_ID':'1'}), patch.object(bot, 'ALLOWED_USER_IDS','1'):
                flow = SMSWorkflow(bot)
            flow.db.import_xml(xml(DEBIT), 1)
            with patch.object(flow,'monthly_records',return_value=[]), patch('sms_auto.categorize',new=AsyncMock(return_value={flow.db.get(1,1)['merchant']:'Other'})):
                await process(flow,1)
            tx=flow.db.get(1,1)
            self.assertEqual(tx['status'],'review')
            self.assertIsNone(tx['category'])


class EditTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.flow=SimpleNamespace(bot=bot,allowed=lambda u:True,lock=__import__('asyncio').Lock())
        self.editor=ExpenseEditor(self.flow)
        self.record={'id':'Mtest','sheet':'September 2026','row':2,'values':['16/09/2026','12:00',300,'','', 'UPI','','','Mtest','Needs details','Expense','Manual']}
        self.context=SimpleNamespace(user_data={})
        self.update=MagicMock()
        self.update.callback_query.answer=AsyncMock()
        self.update.callback_query.message.reply_text=AsyncMock()
        self.update.callback_query.edit_message_text=AsyncMock()

    async def test_proposal_does_not_write_and_apply_requires_current_token(self):
        message=SimpleNamespace(reply_text=AsyncMock())
        with patch.object(self.editor,'save') as save:
            await self.editor.propose(message,self.context,self.record,{'description':'Uber ride','category':'Transport'})
            save.assert_not_called()
            self.update.callback_query.data='ex:apply:expired'
            await self.editor.callback(self.update,self.context)
            save.assert_not_called()
            self.update.callback_query.data='ex:apply:'+self.context.user_data['expense_proposal']['token']
            await self.editor.callback(self.update,self.context)
            save.assert_called_once_with(self.record,'Uber ride','Transport','Confirmed')

    async def test_stale_blank_button_cannot_apply_another_entry_category(self):
        self.context.user_data['edit_suggestion']={'category':'Food','entry_ids':['Mother']}
        self.update.callback_query.data='ex:blank:Mtest'
        with patch.object(self.editor,'find',return_value=self.record),patch.object(self.editor,'propose',new=AsyncMock()) as propose:
            await self.editor.callback(self.update,self.context)
            propose.assert_not_awaited()

    async def test_unauthorized_callback_cannot_read_or_write(self):
        self.flow.allowed=lambda u:False
        with patch.object(self.editor,'find') as find:
            await self.editor.callback(self.update,self.context)
            find.assert_not_called()

    async def test_ambiguous_reference_only_offers_picker(self):
        other={**self.record,'id':'Mother'}
        self.update.message.reply_text=AsyncMock()
        with patch.object(self.editor,'records',return_value=[self.record,other]),patch.object(self.editor,'model',new=AsyncMock(return_value={'ids':['Mtest','Mother'],'description':'Uber','category':'Transport'})),patch.object(self.editor,'save') as save:
            await self.editor.search(self.update,self.context,'Change the 300 payment to Uber')
            save.assert_not_called()
            self.assertNotIn('expense_proposal',self.context.user_data)
            self.assertIn('Which purchase',self.update.message.reply_text.call_args.args[0])

    async def test_stale_edit_fails_before_ledger_write(self):
        changed={**self.record,'values':self.record['values'][:]}
        changed['values'][3]='Changed elsewhere'
        with patch.object(self.editor,'find',return_value=changed),patch.object(self.editor,'transaction') as tx:
            with self.assertRaises(ValueError):
                self.editor.save(self.record,'Uber','Transport')
            tx.assert_not_called()

    async def test_merge_retries_after_sheet_outage(self):
        with tempfile.TemporaryDirectory() as folder:
            from sms_import import Ledger
            self.flow.db=Ledger(str(Path(folder)/'db'))
            self.flow.owner=1
            target={**self.record,'id':'Mtarget','row':3,'values':self.record['values'][:]}
            target['values'][8]='Mtarget'
            sh=MagicMock()
            ws=sh.worksheet.return_value
            ws.spreadsheet.batch_update.side_effect=[RuntimeError('offline'),None]
            records=[self.record,target]
            with patch.object(bot,'get_spreadsheet',return_value=sh),patch.object(self.editor,'find',side_effect=lambda i:next(r for r in records if r['id']==i)),patch.object(self.editor,'transaction',return_value=None),patch.object(self.editor,'records',return_value=records):
                with self.assertRaises(RuntimeError):
                    self.editor.merge(self.record,target)
                self.editor.recover_merges()
            self.assertEqual(self.flow.db.get_sync_state('merge_pending:Mtest'),'done')
            requests=ws.spreadsheet.batch_update.call_args.args[0]['requests']
            self.assertFalse(any('deleteDimension' in r for r in requests))
            deletion=next(r['deleteRange'] for r in requests if 'deleteRange' in r)
            self.assertEqual(deletion['range']['endColumnIndex'],12)
