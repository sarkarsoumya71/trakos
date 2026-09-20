import copy
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import bot
from sms_auto import categorize, merchant_text, notify, plan, process
from sms_import import Ledger
from sms_workflow import SMSWorkflow, LEDGER_HEADER
from test_sms import DEBIT, xml


def transaction(**kwargs):
    tx = dict(id=1, owner=1, occurred_at='2026-09-16T12:00:00+05:30', amount_paise=10000,
        direction='debit', bank='HDFC', account='1234', merchant='Cafe Test', reference='123456789012',
        payment='UPI', kind='expense', category=None, status='review', possible_duplicate=None, exported=0)
    tx.update(kwargs)
    return tx


class PlanTests(unittest.TestCase):
    def test_unknown_merchant_is_not_a_review_block(self):
        tx = transaction(merchant='')
        self.assertEqual(plan([tx], [tx], [])[0]['action'], 'expense')

    def test_credits_and_movements_are_not_spending(self):
        for attrs in [dict(direction='credit'), dict(kind='card_payment'), dict(kind='cash_withdrawal'), dict(kind='transfer')]:
            tx = transaction(**attrs)
            self.assertEqual(plan([tx], [tx], [])[0]['action'], 'exclude')

    def test_opposite_reference_is_excluded(self):
        first = transaction()
        other = transaction(id=2, bank='CBI', account='9876', direction='credit')
        self.assertEqual(plan([first, other], [first], [])[0]['action'], 'exclude')

    def test_equal_amount_alone_does_not_establish_duplicate(self):
        tx = transaction()
        row = dict(date='2026-09-16', amount_paise=10000, description='Something else', raw='', sms_id=None)
        self.assertEqual(plan([tx], [tx], [row])[0]['action'], 'review')

    def test_manual_match_cannot_be_claimed_twice(self):
        first, second = transaction(), transaction(id=2, reference='222222222222')
        row = dict(date='2026-09-16', amount_paise=10000, description='Cafe Test', raw='', sms_id=None)
        self.assertEqual([d['action'] for d in plan([first, second], [first, second], [row])], ['duplicate', 'review'])

    def test_distinct_references_remain_distinct_even_after_export(self):
        first = transaction(status='approved')
        second = transaction(id=2, reference='222222222222', possible_duplicate=1)
        row = dict(date='2026-09-16', amount_paise=10000, description='Cafe Test', raw='', sms_id=1)
        self.assertEqual(plan([first, second], [second], [row])[0]['action'], 'expense')

    def test_privacy_filter(self):
        self.assertEqual(merchant_text('Netflix Ref 123456789012 Balance 100000'), 'Netflix')
        self.assertNotIn('1234567890', merchant_text('merchant1234567890@bank'))
        self.assertNotIn('https:', merchant_text('Cafe https://test.com/ref'))


class AutomaticTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        with patch.dict('os.environ', {'TRAKOS_DB_PATH': str(Path(self.temp.name)/'ledger.sqlite3'), 'SMS_OWNER_USER_ID':'1'}), patch.object(bot, 'ALLOWED_USER_IDS', '1'):
            self.flow = SMSWorkflow(bot)
        self.flow.db.import_xml(xml(DEBIT), 1)

    def tearDown(self):
        self.temp.cleanup()

    async def test_auto_categorization_does_not_teach_unconfirmed_rule(self):
        with patch.object(self.flow, 'monthly_records', return_value=[]), patch('sms_auto.categorize', new=AsyncMock(return_value={'SWIGGY':'Food','Swiggy':'Food'})):
            # Use the actual sanitized synthetic payee as the mock response key.
            name = merchant_text(self.flow.db.get(1,1)['merchant'])
            with patch('sms_auto.categorize', new=AsyncMock(return_value={name:'Food'})):
                await process(self.flow, 1)
        tx = self.flow.db.get(1,1)
        self.assertEqual((tx['status'],tx['category']), ('approved','Food'))
        self.assertIsNone(self.flow.db.merchant_category(1,tx['bank'],tx['merchant']))
        self.assertEqual(self.flow.db.automatic_pending(1), [])

    async def test_model_failure_preserves_pending_for_retry(self):
        with patch.object(self.flow, 'monthly_records', return_value=[]), patch('sms_auto.categorize', new=AsyncMock(side_effect=ValueError('bad response'))):
            with self.assertRaises(ValueError): await process(self.flow,1)
        self.assertEqual(self.flow.db.get(1,1)['status'],'review')
        self.assertEqual(len(self.flow.db.automatic_pending(1)),1)

    async def test_owner_and_explicit_decisions_are_preserved(self):
        self.flow.db.resolve(1,1,'expense','Food')
        await process(self.flow,1)
        self.assertEqual(self.flow.db.get(1,1)['category'],'Food')
        self.assertEqual(self.flow.db.automatic_pending(2),[])

    async def test_notification_after_export_only_and_restart_dedup(self):
        tx = self.flow.db.get(1,1)
        self.flow.db.apply_automatic(1,[dict(tx=tx, action='expense',reason='auto',category='Food')])
        telegram = SimpleNamespace(send_message=AsyncMock())
        await notify(self.flow,telegram,1)
        telegram.send_message.assert_not_called()
        self.flow.db.mark_exported(1,1)
        await notify(self.flow,telegram,1)
        self.flow.db = Ledger(self.flow.db.path)
        await notify(self.flow,telegram,1)
        telegram.send_message.assert_awaited_once()

    async def test_notification_failure_is_retryable(self):
        tx=self.flow.db.get(1,1)
        self.flow.db.apply_automatic(1,[dict(tx=tx,action='exclude',reason='auto',category=None)])
        telegram=SimpleNamespace(send_message=AsyncMock(side_effect=RuntimeError('offline')))
        with self.assertRaises(RuntimeError): await notify(self.flow,telegram,1)
        self.assertEqual(len(self.flow.db.automatic_notifications(1)),1)

    async def test_invalid_or_incomplete_model_response_is_rejected(self):
        for entries in [[{'id':0,'category':'Food'},{'id':0,'category':'Food'}], [], [{'id':0,'category':'Invalid'}]]:
            response=MagicMock()
            response.json.return_value={'choices':[{'finish_reason':'stop','message':{'content':json.dumps({'items':entries})}}]}
            client=AsyncMock()
            client.post.return_value=response
            with patch('sms_auto.httpx.AsyncClient') as cls, patch.object(bot,'GROQ_API_KEY','test'):
                cls.return_value.__aenter__.return_value=client
                with self.assertRaises(ValueError): await categorize(bot,['Cafe'])

    async def test_sheet_failure_prevents_auto_approval(self):
        with patch.object(self.flow,'monthly_records',side_effect=RuntimeError('offline')):
            with self.assertRaises(RuntimeError): await process(self.flow,1)
        self.assertEqual(self.flow.db.get(1,1)['status'],'review')

    async def test_failed_projection_never_sends_success_notification(self):
        with patch.dict('os.environ',{'SMS_AUTO_APPROVE':'1'}), patch('sms_auto.process',new=AsyncMock()), patch.object(self.flow,'export_views',side_effect=RuntimeError('offline')), patch('sms_auto.notify',new=AsyncMock()) as sender:
            with self.assertRaises(RuntimeError): await self.flow.complete_sync(1,AsyncMock())
            sender.assert_not_awaited()

    async def test_batch_projection_recovers_without_duplicate_rows(self):
        self.flow.db.import_xml(xml(DEBIT.replace('123456789012','222222222222')),1)
        for tx in self.flow.db.list(1): self.flow.db.resolve(tx['id'],1,'expense','Food')
        sh,ws,monthly=MagicMock(),MagicMock(),MagicMock()
        sh.worksheet.return_value=ws
        ws.get_all_values.return_value=[LEDGER_HEADER]
        ws.row_count=1000
        saved=[]
        monthly.get.side_effect=lambda _: list(saved)
        def append(_,rows): saved.extend(rows)
        with patch.object(bot,'get_spreadsheet',return_value=sh), patch.object(bot,'get_month_sheet',return_value=monthly), patch.object(bot,'append_many_to_data_area',side_effect=append) as writer:
            with patch.object(self.flow.db,'mark_exported',side_effect=RuntimeError('crash')):
                with self.assertRaises(RuntimeError): self.flow.export_views(1)
            self.flow.export_views(1)
            writer.assert_called_once()
        self.assertEqual(len(saved),2)
        self.assertTrue(all(tx['exported'] for tx in self.flow.db.list(1)))

    async def test_category_correction_is_owner_scoped_and_learned(self):
        self.flow.db.resolve(1,1,'expense','Other')
        with self.assertRaises(ValueError): self.flow.db.recategorize(1,2,'Food')
        self.flow.db.recategorize(1,1,'Food')
        tx=self.flow.db.get(1,1)
        self.assertEqual(self.flow.db.merchant_category(1,tx['bank'],tx['merchant']),'Food')

    async def test_historical_overlap_policy_is_explicit_and_bounded(self):
        tx=self.flow.db.get(1,1)
        from datetime import datetime
        row=dict(date=tx['occurred_at'][:10],amount_paise=tx['amount_paise'],description='Unrelated',raw='',sms_id=None)
        with patch.dict('os.environ',{'SMS_BACKLOG_MAX_ID':'1','SMS_BACKLOG_OVERLAPS':'exclude'}), patch.object(self.flow,'monthly_records',return_value=[row]):
            await process(self.flow,1)
        self.assertEqual(self.flow.db.get(1,1)['status'],'exclude')
        self.assertIn('Possible historical duplicate',self.flow.db.get(1,1)['reason'])


class BatchTests(unittest.TestCase):
    def test_batch_raw_dates_and_formula_text(self):
        ws=MagicMock()
        ws.col_values.return_value=['Date']
        ws.row_count=1000
        rows=[['16/09/2026','12:00',100,'=danger','Food','UPI','raw'],['17/09/2026','12:00',50,'Cafe','Food','UPI','raw']]
        with patch.object(bot,'sort_month_sheet'):
            bot.append_many_to_data_area(ws,rows)
        args,kwargs=ws.update.call_args
        self.assertEqual(args[0],'A2:L3')
        self.assertIsInstance(args[1][0][0],int)
        self.assertEqual(args[1][0][3],'=danger')
        self.assertEqual(kwargs['value_input_option'],'RAW')
