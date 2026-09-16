import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import bot
from sms_import import Ledger, parse_sms
from sms_workflow import SMSWorkflow, LEDGER_HEADER
from test_sms import STAMP, DEBIT, xml


class AccountingPolicyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Ledger(Path(self.temp.name) / 'ledger.sqlite3')

    def tearDown(self):
        self.temp.cleanup()

    def test_explicit_transfer_and_card_bill_are_kept_but_excluded(self):
        self.db.import_xml(xml('Rs.5000 debited from A/c XX1234 to own account',
                               'Rs.450 debited from A/c XX1234 for credit card bill payment'), 1)
        entries = self.db.list(1)
        self.assertEqual([e['kind'] for e in entries], ['transfer', 'card_payment'])
        self.assertEqual([e['status'] for e in entries], ['exclude', 'exclude'])
        self.assertTrue(self.db.body(entries[0]['id'], 1))

    def test_card_purchase_remains_an_expense(self):
        tx, _ = parse_sms('AD-HDFCBK-S', 'Rs.450 spent on HDFC credit card x4321 at EXAMPLE SHOP on 16/09/2026 payment successful', STAMP)
        self.assertEqual(tx.kind, 'expense')

    def test_unconfirmed_transfer_is_not_inferred_from_equal_amounts(self):
        self.db.import_xml(xml(DEBIT, DEBIT.replace('debited', 'credited').replace('XX1234', 'XX5678')), 1)
        self.assertEqual([e['status'] for e in self.db.list(1)], ['review', 'review'])
        self.assertTrue(self.db.resolve(1, 1, 'transfer'))
        self.assertEqual(self.db.get(1, 1)['kind'], 'transfer')
        self.assertEqual(self.db.get(1, 1)['status'], 'exclude')
        self.assertEqual(self.db.get(2, 1)['status'], 'review')

    def test_card_bill_confirmation_is_owner_scoped_and_idempotent(self):
        self.db.import_xml(xml(DEBIT), 1)
        self.assertFalse(self.db.resolve(1, 2, 'card_payment'))
        self.assertTrue(self.db.resolve(1, 1, 'card_payment'))
        self.assertFalse(self.db.resolve(1, 1, 'expense', 'Food'))
        self.assertEqual(self.db.get(1, 1)['kind'], 'card_payment')

    def test_policy_updates_pending_not_previously_booked_expenses(self):
        self.db.import_xml(xml(DEBIT, DEBIT.replace('123456789012', '123456789013')), 1)
        with self.db.connect() as db:
            db.execute("UPDATE transactions SET kind='transfer'")
            db.execute("UPDATE transactions SET status='approved',exported=1 WHERE id=2")
        restored = Ledger(self.db.path)
        self.assertEqual(restored.get(1, 1)['status'], 'exclude')
        self.assertEqual(restored.get(2, 1)['status'], 'approved')

    def test_excluded_transfer_syncs_audit_only(self):
        self.db.import_xml(xml('Rs.5000 debited from A/c XX1234 to own account'), 1)
        flow = SMSWorkflow.__new__(SMSWorkflow)
        flow.bot, flow.db = bot, self.db
        sh, ws = MagicMock(), MagicMock()
        sh.worksheet.return_value = ws
        ws.get_all_values.return_value = [LEDGER_HEADER]
        ws.row_count = 1000
        with patch.object(bot, 'get_spreadsheet', return_value=sh), patch.object(bot, 'append_to_data_area') as append:
            flow.export_views(1)
            append.assert_not_called()
        row = ws.batch_update.call_args.args[0][0]['values'][0]
        self.assertEqual(row[10], 'transfer')
        self.assertEqual(row[12], 'exclude')
