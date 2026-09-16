"""Bank-template regressions using entirely synthetic amounts, accounts and payees."""
import unittest
from sms_import import parse_sms
from test_sms import STAMP


class BankTemplateTests(unittest.TestCase):
    def parse(self, body, sender='AD-HDFCBK-S'):
        tx, reason = parse_sms(sender, body, STAMP)
        self.assertIsNotNone(tx, reason)
        return tx

    def test_hdfc_card_merchant_and_iso_datetime(self):
        tx = self.parse('Spent Rs.765.43 From HDFC Bank Card x4321 At EXAMPLE SOFTWARE On 2026-09-14:23:12:05 Bal Rs.20000.00 Not You? Call 18000000000')
        self.assertEqual(tx.merchant, 'EXAMPLE SOFTWARE')
        self.assertEqual(tx.occurred_at, '2026-09-14T23:12:05+05:30')
        self.assertEqual(tx.account, '4321')
        self.assertEqual(tx.payment, 'CARD1')

    def test_hdfc_multiline_upi_payee(self):
        tx = self.parse('Sent Rs.320.00\nFrom HDFC Bank A/C *1234\nTo EXAMPLE SHOP\nOn 14/09/26\nRef 987654321012\nNot You?\nCall 18000000000/SMS BLOCK UPI to 9000000000')
        self.assertEqual(tx.merchant, 'EXAMPLE SHOP')
        self.assertEqual(tx.reference, '987654321012')
        self.assertEqual(tx.payment, 'UPI')

    def test_cbi_vpa_has_no_reference_suffix_in_merchant(self):
        tx = self.parse('Your account no. XXXX6789 is successfully debited for Rs.320.00 to VPA exampleshop@bank (UPI Ref no 987654321012) - Central Bank of India', 'AD-CENTBK-S')
        self.assertEqual(tx.account, '6789')
        self.assertEqual(tx.merchant, 'exampleshop@bank')

    def test_cbi_neft_three_decimal_zero(self):
        tx = self.parse('Rs. 12345.000 credited to your A/c 1XXXXX6789 on 14/09/2026 through NEFT vide Ref No./XUTR/EXAMH12345678901 By.EXAMPLE CLIENT -CBoI', 'AD-CENTBK-S')
        self.assertEqual(tx.amount_paise, 1234500)
        self.assertEqual(tx.reference, 'EXAMH12345678901')
        self.assertEqual(tx.merchant, 'EXAMPLE CLIENT')
        self.assertEqual(tx.direction, 'credit')

    def test_fractional_paise_rejected(self):
        self.assertIsNone(parse_sms('AD-CENTBK-S', 'Rs.123.456 credited to A/c XX6789', STAMP)[0])

    def test_hdfc_deduction_and_mandate_is_not_transaction_ref(self):
        tx = self.parse('PAYMENT ALERT! INR 1200.00 deducted from HDFC Bank A/C No 1234 towards EXAMPLE CLEARING UMRN: HDFC1234567890123456')
        self.assertEqual(tx.amount_paise, 120000)
        self.assertEqual(tx.merchant, 'EXAMPLE CLEARING')
        self.assertEqual(tx.reference, '')

    def test_cbi_pos(self):
        tx = self.parse('Card ending x8765 used at POS EXAMPLE SHOP on 14/09/2026 for txn Rs 5000.00 Bal Rs 10000.00. If not done by you? Call 18000000 to Block. CBoI', 'AX-CENTBK-S')
        self.assertEqual(tx.amount_paise, 500000)
        self.assertEqual(tx.account, '8765')
        self.assertEqual(tx.merchant, 'EXAMPLE SHOP')
        self.assertEqual(tx.payment, 'CARD2')

    def test_autopay_inr_success(self):
        tx = self.parse('AutoPay (E-mandate) Success!\nFor EXAMPLE SOFTWARE\nTxn Amt:INR765.43\nDt:14/09/2026\nVia:HDFC Bank DC 4321\nMandate ID: synthetic\nTnC')
        self.assertEqual(tx.amount_paise, 76543)
        self.assertEqual(tx.merchant, 'EXAMPLE SOFTWARE')
        self.assertEqual(tx.account, '4321')
        self.assertEqual(tx.reference, '')
        self.assertTrue(tx.occurred_at.startswith('2026-09-14'))

    def test_foreign_autopay_not_treated_as_rupees(self):
        tx, reason = parse_sms('AD-HDFCBK-S', 'AutoPay (E-mandate) Success!\nFor EXAMPLE\nTxn Amt:USD10.00\nDt:14/09/2026\nVia:HDFC Bank DC 4321', STAMP)
        self.assertIsNone(tx)
        self.assertEqual(reason, 'foreign_currency_receipt')

    def test_security_marketing_and_mandates_are_not_expenses(self):
        for body in ['AutoPay Active! For EXAMPLE Starting:14/09/2026 On HDFC Bank Card 4321',
                     'Congrats! Rs.500 voucher is waiting for you',
                     'Your UPI-Mandate is successfully created towards EXAMPLE for Rs5000.00. Funds are blocked from A/C No. XXXX6789',
                     'Notice! Forex Markup fee applies to your HDFC Bank Debit Card International payment',
                     'Login Alert! There was a login to your NetBanking']:
            with self.subTest(body=body):
                self.assertEqual(parse_sms('AD-HDFCBK-S', body, STAMP)[1], 'non_transaction')
