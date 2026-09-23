import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, AsyncMock, patch

import bot
from expense_edit import ExpenseEditor
from sms_import import Ledger
from sms_workflow import SMSWorkflow, LEDGER_HEADER
from test_sms import xml, DEBIT


def record(identity='Mnew', status='Possible duplicate', source='Manual', amount=450):
    return {'id':identity,'sheet':'September 2026','row':2,
        'values':['19/09/2026','18:53',amount,'Healthy food','Health','UPI','450 food','',identity,status,'Expense',source]}


class DuplicateReviewTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.flow=SimpleNamespace(bot=bot,owner=1,allowed=lambda u:True,lock=asyncio.Lock(),db=Ledger(Path(self.temp.name)/'db'))
        self.editor=ExpenseEditor(self.flow)
        self.context=SimpleNamespace(user_data={})
        self.message=SimpleNamespace(reply_text=AsyncMock())

    def tearDown(self):
        self.temp.cleanup()

    def test_manual_arriving_after_sms_is_held(self):
        ws=MagicMock(row_count=200)
        ws.col_values.return_value=['Date','19/09/2026']
        existing=record('Sbank','Confirmed','SMS',450.34)['values']
        ws.get.return_value=[existing]
        with patch.object(bot,'sort_month_sheet'):
            result=bot.append_to_data_area(ws, record(status='Confirmed')['values'])
        self.assertEqual(ws.update.call_args.args[1][0][9],'Possible duplicate')
        self.assertEqual(result['status'],'Possible duplicate')

    def test_category_edit_cannot_approve_possible_duplicate(self):
        r=record()
        with patch.object(self.editor,'find',return_value=r),patch.object(self.editor,'transaction') as tx:
            with self.assertRaisesRegex(ValueError,'duplicate'):
                self.editor.save(r,'Healthy food','Health')
            tx.assert_not_called()

    async def test_duplicate_card_compares_records_and_offers_both_choices(self):
        current=record()
        other=record('Sold','Confirmed','SMS',450.34)
        with patch.object(self.editor,'records',return_value=[current,other]):
            await self.editor.duplicate_card(self.message,self.context,current)
        text=self.message.reply_text.call_args.args[0]
        self.assertIn('450.34',text)
        self.assertIn('450.00',text)
        buttons=self.message.reply_text.call_args.kwargs['reply_markup'].inline_keyboard
        self.assertEqual([b.text for line in buttons for b in line],['Same purchase','Separate purchases'])

    def test_separate_decision_persists_and_does_not_hide_other_candidates(self):
        current=record()
        first=record('Sfirst','Confirmed','SMS')
        second=record('Ssecond','Confirmed','SMS')
        with patch.object(self.editor,'records',return_value=[current,first,second]),patch.object(self.editor,'find',side_effect=lambda i:next(r for r in [current,first,second] if r['id']==i)),patch.object(self.editor,'save') as save:
            self.editor.separate(current,first)
            save.assert_not_called()
            self.assertEqual([r['id'] for r in self.editor.duplicate_matches(current)],['Ssecond'])
            self.flow.db=Ledger(self.flow.db.path)
            self.editor.separate(current,second)
            save.assert_called_once()

    def test_stale_pair_cannot_merge_or_separate(self):
        current=record()
        other=record('Sold','Confirmed','SMS')
        changed=record('Sold','Refunded','SMS')
        with patch.object(self.editor,'find',side_effect=lambda i:current if i==current['id'] else changed):
            with self.assertRaises(ValueError):
                self.editor.separate(current,other)

    async def test_old_category_button_redirects_to_duplicate_resolution(self):
        update=MagicMock()
        update.callback_query.data='ex:cat:Mnew:0'
        update.callback_query.answer=AsyncMock()
        with patch.object(self.editor,'find',return_value=record()),patch.object(self.editor,'duplicate_card',new=AsyncMock()) as card,patch.object(self.editor,'propose',new=AsyncMock()) as propose:
            await self.editor.callback(update,self.context)
        card.assert_awaited_once()
        propose.assert_not_awaited()

    def test_merge_keeps_manual_details_exact_bank_amount_and_refund_status(self):
        self.flow.db.import_xml(xml(DEBIT.replace('450.00','450.34')),1)
        self.flow.marker=lambda tx:'trakos-sms:'+'a'*32
        source=record('S'+'a'*32,'Possible duplicate','SMS',450.34)
        source['values'][6]=''
        source['values'][7]=self.flow.db.body(1,1)
        target=record('Mkept','Refunded','Manual',450)
        target['row']=3
        sh=MagicMock()
        ws=sh.worksheet.return_value
        with patch.object(self.editor,'records',return_value=[source,target]),patch.object(bot,'get_spreadsheet',return_value=sh):
            self.editor.merge(source,target,bank_amount=True)
        requests=ws.spreadsheet.batch_update.call_args.args[0]['requests']
        writes={r['updateCells']['start']['columnIndex']:r['updateCells']['rows'][0]['values'][0]['userEnteredValue'] for r in requests if 'updateCells' in r}
        self.assertEqual(writes[2],{'numberValue':450.34})
        self.assertEqual(writes[6],{'stringValue':'450 food'})
        self.assertEqual(writes[7],{'stringValue':source['values'][7]})
        self.assertNotIn(3,writes)
        self.assertNotIn(4,writes)
        self.assertNotIn(9,writes)
        self.assertEqual(self.flow.db.get(1,1)['status'],'duplicate')
        self.flow.db.import_xml(xml(DEBIT.replace('450.00','450.34')),1)
        self.assertEqual(self.flow.db.get(1,1)['status'],'duplicate')

    def test_sms_projection_catches_manual_inserted_after_ai_read(self):
        self.flow.db.import_xml(xml(DEBIT),1)
        self.flow.db.resolve(1,1,'expense','Food')
        flow=SMSWorkflow.__new__(SMSWorkflow)
        flow.bot,flow.db=bot,self.flow.db
        sh,ledger_ws,month_ws=MagicMock(),MagicMock(),MagicMock()
        sh.worksheet.return_value=ledger_ws
        ledger_ws.row_count=1000
        ledger_ws.get_all_values.return_value=[LEDGER_HEADER]
        manual=record(status='Confirmed')['values']
        manual[0]='16/09/2026'
        month_ws.row_count=200
        month_ws.get.return_value=[manual]
        with patch.object(bot,'get_spreadsheet',return_value=sh),patch.object(bot,'get_month_sheet',return_value=month_ws),patch.object(bot,'append_to_data_area') as append:
            flow.export_views(1)
        self.assertEqual(append.call_args.args[1][9],'Possible duplicate')
        self.assertEqual(self.flow.db.get(1,1)['status'],'review')

    def test_same_amount_cash_purchase_is_not_linked_to_upi(self):
        from expense_sheet import matching_rows
        current=record()['values']
        other=record('Sold','Confirmed','SMS')['values']
        current[5]='CASH'
        self.assertEqual(matching_rows(current,[other]),[])

    async def test_same_purchase_keeps_manual_row_regardless_of_arrival_order(self):
        manual=record('Mmanual')
        bank=record('Sbank','Confirmed','SMS',450.34)
        update=MagicMock()
        update.callback_query.answer=AsyncMock()
        update.callback_query.edit_message_text=AsyncMock()
        for current,other in [(manual,bank),(bank,manual)]:
            self.context.user_data={'duplicate_choices':{'token':{'record':current,'other':other}}}
            update.callback_query.data='ex:same:token'
            with patch.object(self.editor,'merge') as merge,patch.object(self.editor,'find',return_value=record('Mmanual','Confirmed')):
                await self.editor.callback(update,self.context)
            merge.assert_called_once_with(bank,manual,None,True)
            self.assertNotIn('token',self.context.user_data['duplicate_choices'])
