import asyncio
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import bot
from expense_edit import ExpenseEditor
from expense_reports import read_spending, render
from expense_sheet import HEADERS, near_manual_matches
from sms_import import Ledger
from sms_workflow import SMSWorkflow, LEDGER_HEADER, COMMAND_MENU
from test_sms import xml, DEBIT


def purchase(status='Confirmed'):
    return {'id':'Mtest', 'sheet':'September 2026', 'row':2,
        'values':['16/09/2026','12:00',600,'Toy','Shopping','UPI','600 toy','bank evidence','Mtest',status,'Expense','Manual']}


class ReturnTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.flow=SimpleNamespace(bot=bot, owner=1, allowed=lambda u: True, lock=asyncio.Lock())
        self.editor=ExpenseEditor(self.flow)
        self.context=SimpleNamespace(user_data={}, args=[])
        self.update=MagicMock()
        self.update.message.reply_text=AsyncMock()
        self.update.callback_query.answer=AsyncMock()
        self.update.callback_query.message.reply_text=AsyncMock()
        self.update.callback_query.edit_message_text=AsyncMock()

    async def test_return_requires_apply_and_undo_restores_expense(self):
        for action, before, after in [('pending','Confirmed','Return pending'),
                ('refunded','Return pending','Refunded'), ('restore','Refunded','Confirmed')]:
            record=purchase(before)
            with patch.object(self.editor,'find',return_value=record), patch.object(self.editor,'save') as save:
                self.update.callback_query.data='ex:'+action+':Mtest'
                await self.editor.callback(self.update,self.context)
                save.assert_not_called()
                proposal=self.context.user_data['expense_proposal']
                self.assertEqual(proposal['status'],after)
                self.update.callback_query.data='ex:apply:'+proposal['token']
                await self.editor.callback(self.update,self.context)
                save.assert_called_once_with(record,'Toy','Shopping',after)
                await self.editor.callback(self.update,self.context)
                self.assertEqual(save.call_count,1)

    async def test_edit_does_not_restore_refunded_purchase_or_touch_evidence(self):
        record=purchase('Refunded')
        sh=MagicMock()
        with patch.object(self.editor,'find',return_value=record), patch.object(self.editor,'transaction',return_value=None), patch.object(bot,'get_spreadsheet',return_value=sh), patch('expense_edit.ensure_dashboard'):
            self.editor.save(record,'Stuffed toy','Shopping')
        writes=sh.worksheet.return_value.batch_update.call_args.args[0]
        self.assertEqual(writes,[{'range':'D2:E2','values':[['Stuffed toy','Shopping']]},
                                {'range':'J2:K2','values':[['Refunded','Expense']]}])
        await self.editor.propose(self.update.message,self.context,record,{'description':'Stuffed toy'})
        self.assertEqual(self.context.user_data['expense_proposal']['status'],'Refunded')

    async def test_stale_return_cannot_overwrite_new_state(self):
        with patch.object(self.editor,'find',return_value=purchase('Refunded')),patch.object(self.editor,'transaction') as transaction:
            with self.assertRaises(ValueError):
                self.editor.save(purchase(),'Toy','Shopping','Return pending')
            transaction.assert_not_called()

    async def test_no_ai_needed_for_recent_purchase_buttons(self):
        with patch.object(self.editor,'records',return_value=[purchase()]), patch.object(self.editor,'model') as model:
            await self.editor.return_purchase(self.update,self.context)
        model.assert_not_called()
        buttons=self.update.message.reply_text.call_args.kwargs['reply_markup'].inline_keyboard
        self.assertEqual(buttons[0][0].callback_data,'ex:return:Mtest')
        self.assertTrue({'return','returns'}.issubset(dict(COMMAND_MENU)))

    async def test_unknown_duplicate_or_investment_cannot_be_refunded(self):
        for status in ['Needs details','Possible duplicate','Excluded','Duplicate']:
            with self.assertRaises(ValueError):
                self.editor.validate_return(purchase(status))
        record=purchase()
        record['values'][10]='Investment'
        with self.assertRaises(ValueError):
            self.editor.validate_return(record)

    async def test_returned_manual_purchase_still_blocks_duplicate_sms_booking(self):
        row={'date':'2026-09-16','amount_paise':60000,'sms_id':None,'status':'Refunded'}
        matches=near_manual_matches({'occurred_at':'2026-09-16T12:00:00','amount_paise':60000},[row])
        self.assertEqual(matches,[row])

    async def test_report_includes_pending_excludes_refunded_and_undo_restores(self):
        ws=MagicMock(row_count=200,col_count=12)
        ws.title='September 2026'
        sh=MagicMock()
        sh.worksheets.return_value=[ws]
        for status,total in [('Return pending',60000),('Refunded',0),('Confirmed',60000)]:
            ws.get.return_value=[HEADERS,purchase(status)['values']]
            with patch.object(bot,'get_spreadsheet',return_value=sh):
                snapshot=read_spending(bot,datetime(2026,9,17,tzinfo=bot.TIMEZONE))
            month=snapshot['periods']['month']
            self.assertEqual(month['total'],total)
            self.assertEqual(month['categories'].get('Shopping',0),total)
            self.assertEqual(month['return_pending'],60000 if status=='Return pending' else 0)
            self.assertEqual(month['refunded'],60000 if status=='Refunded' else 0)
            if status=='Return pending':
                self.assertIn('Refund pending (still included)',render(snapshot))


class LedgerReturnTests(unittest.TestCase):
    def test_sms_return_survives_reimport_export_retry_and_restoration(self):
        with tempfile.TemporaryDirectory() as folder:
            db=Ledger(Path(folder)/'db')
            db.import_xml(xml(DEBIT),1)
            flow=SMSWorkflow.__new__(SMSWorkflow)
            flow.bot,flow.db=bot,db
            sh, ledger_ws, month_ws=MagicMock(),MagicMock(),MagicMock()
            sh.worksheet.return_value=ledger_ws
            ledger_ws.get_all_values.return_value=[LEDGER_HEADER]
            ledger_ws.row_count=1000
            month_ws.row_count=200
            from expense_sheet import sms_id
            row=purchase()['values']
            row[8]=sms_id(flow.marker(db.get(1,1)))
            month_ws.get.return_value=[row]
            for status,display in [('return_pending','Return pending'),('refunded','Refunded'),('approved','Confirmed')]:
                db.edit_details(1,1,'Toy','Shopping',status)
                db.import_xml(xml(DEBIT),1)
                self.assertEqual(db.get(1,1)['status'],status)
                with patch.object(bot,'get_spreadsheet',return_value=sh),patch.object(bot,'get_month_sheet',return_value=month_ws):
                    month_ws.batch_update.side_effect=RuntimeError('offline')
                    with self.assertRaises(RuntimeError):
                        flow.export_views(1)
                    self.assertFalse(db.get(1,1)['exported'])
                    month_ws.batch_update.side_effect=None
                    flow.export_views(1)
                updated=month_ws.batch_update.call_args.args[0][0]['values'][0]
                self.assertEqual(updated[9],display)
                self.assertEqual(updated[2],db.get(1,1)['amount_paise']/100)
                self.assertEqual(updated[7],db.body(1,1))
                self.assertTrue(db.get(1,1)['exported'])
            with self.assertRaises(ValueError):
                db.edit_details(1,2,'Toy','Shopping','refunded')
