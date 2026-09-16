import asyncio
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch
from xml.sax.saxutils import quoteattr

import bot
from sms_import import Ledger, MAX_BYTES, TZ, parse_sms
from sms_workflow import SMSWorkflow, safe_csv

STAMP = str(int(datetime(2026, 9, 16, 10, 30, tzinfo=TZ).timestamp() * 1000))
DEBIT = 'Rs.450.00 debited from A/c XX1234 to Zomato on 16/09/2026 10:30. UPI Ref No 123456789012. Avl Bal Rs.10,000.00'


def xml(*messages):
    rows = []
    for item in messages:
        if isinstance(item, str):
            item = dict(body=item)
        data = dict(address='JD-HDFCBK', date=STAMP, type='1', **item)
        rows.append('<sms ' + ' '.join(f'{k}={quoteattr(str(v))}' for k, v in data.items()) + '/>')
    return ('<smses>' + ''.join(rows) + '</smses>').encode()


class ParserTests(unittest.TestCase):
    def test_debit_uses_transaction_amount_not_balance(self):
        tx, reason = parse_sms('JD-HDFCBK', DEBIT, STAMP)
        self.assertEqual(reason, 'parsed')
        self.assertEqual(tx.amount_paise, 45000)
        self.assertEqual(tx.account, '1234')
        self.assertEqual(tx.reference, '123456789012')
        self.assertEqual(tx.direction, 'debit')
        self.assertEqual(tx.merchant, 'Zomato')

    def test_cbi_credit(self):
        tx, _ = parse_sms('VM-CENTBK', 'Your A/c XX5678 credited by Rs.1,200.50 from CLIENT on 16/09/2026. UTR ABCD123456', STAMP)
        self.assertEqual(tx.amount_paise, 120050)
        self.assertEqual(tx.bank, 'CBI')
        self.assertEqual(tx.kind, 'income')

    def test_otp_failed_and_non_bank_are_not_transactions(self):
        for sender, body in [('JD-HDFCBK', 'OTP 123456 for Rs.500 debited'),
                             ('JD-HDFCBK', 'Rs.500 debited transaction failed'),
                             ('FRIEND', DEBIT), ('JD-HDFCBK', 'Payment due date: Rs.500')]:
            self.assertIsNone(parse_sms(sender, body, STAMP)[0])

    def test_balance_only_and_ambiguous_alert_are_unparsed(self):
        for body in ['Avl Bal Rs.5,000', 'Rs.500 debited and Rs.100 credited']:
            self.assertIsNone(parse_sms('JD-HDFCBK', body, STAMP)[0])

    def test_explicit_transaction_date(self):
        tx, _ = parse_sms('JD-HDFCBK', DEBIT.replace('16/09/2026', '15/09/2026'), STAMP)
        self.assertTrue(tx.occurred_at.startswith('2026-09-15'))

    def test_cash_withdrawal_suggests_exclusion(self):
        tx, _ = parse_sms('JD-HDFCBK', 'Rs.500 withdrawn from A/c XX1234 at ATM', STAMP)
        self.assertEqual(tx.kind, 'cash_withdrawal')


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Ledger(Path(self.temp.name) / 'test.sqlite3')

    def tearDown(self):
        self.temp.cleanup()

    def test_reimport_and_overlapping_backups(self):
        self.assertEqual(self.db.import_xml(xml(DEBIT), 1)['new'], 1)
        self.assertTrue(self.db.import_xml(xml(DEBIT), 1)['already_imported'])
        report = self.db.import_xml(xml(DEBIT, DEBIT.replace('123456789012', '123456789013')), 1)
        self.assertEqual(report['new'], 1)
        self.assertEqual(report['duplicate'], 1)
        self.assertEqual(len(self.db.list(1)), 2)

    def test_same_reference_variant_merges(self):
        report = self.db.import_xml(xml(DEBIT, DEBIT.replace('Avl Bal', 'Available Balance')), 1)
        self.assertEqual(report['duplicate'], 1)
        self.assertEqual(len(self.db.list(1)), 1)
        with self.db.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM messages').fetchone()[0], 2)

    def test_same_amount_different_refs_remain_separate(self):
        report = self.db.import_xml(xml(DEBIT, DEBIT.replace('123456789012', '123456789099')), 1)
        self.assertEqual(report['new'], 2)
        self.assertEqual(report['possible_duplicate'], 1)
        self.assertEqual([t['status'] for t in self.db.list(1)], ['review', 'review'])

    def test_different_accounts_and_directions_not_merged(self):
        report = self.db.import_xml(xml(DEBIT, DEBIT.replace('XX1234', 'XX5678'), DEBIT.replace('debited', 'credited')), 1)
        self.assertEqual(report['new'], 3)

    def test_missing_reference_never_auto_merges(self):
        first = DEBIT.replace('UPI Ref No 123456789012.', '')
        report = self.db.import_xml(xml(first, first.replace('Avl Bal', 'Available Balance')), 1)
        self.assertEqual(report['new'], 2)

    def test_no_cross_user_dedup_or_review(self):
        self.db.import_xml(xml(DEBIT), 1)
        self.assertEqual(self.db.import_xml(xml(DEBIT), 2)['new'], 1)
        self.assertFalse(self.db.resolve(1, 2, 'expense', 'Food'))
        self.assertIsNone(self.db.get(1, 2))

    def test_persistence_and_double_click(self):
        self.db.import_xml(xml(DEBIT), 1)
        restored = Ledger(self.db.path)
        self.assertTrue(restored.resolve(1, 1, 'expense', 'Food'))
        self.assertFalse(restored.resolve(1, 1, 'expense', 'Food'))
        self.assertEqual(restored.get(1, 1)['status'], 'approved')

    def test_credit_cannot_be_approved_as_spending(self):
        self.db.import_xml(xml(DEBIT.replace('debited', 'credited')), 1)
        with self.assertRaises(ValueError):
            self.db.resolve(1, 1, 'expense', 'Food')
        self.assertTrue(self.db.resolve(1, 1, 'exclude'))

    def test_malformed_xml_rolls_back(self):
        with self.assertRaises(Exception):
            self.db.import_xml(xml(DEBIT)[:-8], 1)
        self.assertEqual(self.db.list(1), [])

    def test_entity_expansion_rejected(self):
        payload = b'<!DOCTYPE smses [<!ENTITY x "secret">]><smses><sms body="&x;"/></smses>'
        with self.assertRaises(Exception):
            self.db.import_xml(payload, 1)

    def test_non_sms_root_rejected(self):
        with self.assertRaises(ValueError):
            self.db.import_xml(b'<calls/>', 1)

    def test_unparsed_kept_but_personal_messages_not_stored(self):
        self.db.import_xml(xml('Avl Bal Rs.5000'), 1)
        self.assertEqual(len(self.db.unparsed(1)), 1)
        private = b'<smses><sms type="1" address="Friend" body="hello" date="123"/></smses>'
        self.db.import_xml(private, 1)
        self.assertEqual(len(self.db.unparsed(1)), 1)


class ProjectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        with patch.dict('os.environ', {'TRAKOS_DB_PATH': str(Path(self.temp.name) / 'db.sqlite3'), 'SMS_OWNER_USER_ID': '1'}), patch.object(bot, 'ALLOWED_USER_IDS', '1'):
            self.flow = SMSWorkflow(bot)
        self.flow.db.import_xml(xml(DEBIT), 1)

    def tearDown(self):
        self.temp.cleanup()

    def test_review_rows_do_not_enter_monthly_totals(self):
        from sms_workflow import LEDGER_HEADER
        sh, ws = MagicMock(), MagicMock()
        sh.worksheet.return_value = ws
        ws.get_all_values.return_value = [LEDGER_HEADER]
        ws.row_count = 1000
        with patch.object(bot, 'get_spreadsheet', return_value=sh), patch.object(bot, 'append_to_data_area') as append:
            self.flow.export_views(1)
            append.assert_not_called()
        ws.batch_update.assert_called_once()

    def test_crash_after_append_does_not_duplicate_on_retry(self):
        from sms_workflow import LEDGER_HEADER
        self.flow.db.resolve(1, 1, 'expense', 'Food')
        sh, ledger_ws, monthly = MagicMock(), MagicMock(), MagicMock()
        sh.worksheet.return_value = ledger_ws
        ledger_ws.get_all_values.return_value = [LEDGER_HEADER]
        ledger_ws.row_count = 1000
        raw_rows = []
        monthly.col_values.side_effect = lambda _: list(raw_rows)
        def append(ws, row):
            raw_rows.append(row[6])
        with patch.object(bot, 'get_spreadsheet', return_value=sh), patch.object(bot, 'get_month_sheet', return_value=monthly), patch.object(bot, 'append_to_data_area', side_effect=append) as writer:
            with patch.object(self.flow.db, 'mark_exported', side_effect=RuntimeError('simulated crash')):
                with self.assertRaises(RuntimeError):
                    self.flow.export_views(1)
            self.flow.export_views(1)
            self.assertEqual(writer.call_count, 1)
            self.assertEqual(self.flow.db.get(1, 1)['exported'], 1)

    def test_excluded_entries_remain_in_ledger(self):
        self.flow.db.resolve(1, 1, 'exclude')
        self.assertEqual(self.flow.db.get(1, 1)['status'], 'exclude')

    def test_csv_formula_injection(self):
        self.assertEqual(safe_csv('=1+1'), "'=1+1")
        self.assertEqual(safe_csv(' @SUM(A1)'), "' @SUM(A1)")


if __name__ == '__main__':
    unittest.main()
