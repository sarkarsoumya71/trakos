import unittest
from unittest.mock import patch, MagicMock, AsyncMock
from datetime import datetime

import bot


class BotRegressionTests(unittest.TestCase):
    def test_category_words_and_specific_phrases(self):
        self.assertIsNone(bot.guess_category_keywords('service fee'))
        self.assertEqual(bot.guess_category_keywords('water bill'), 'Bills & Utilities')
        self.assertEqual(bot.guess_category_keywords('gym membership'), 'Subscriptions')

    def test_empty_allowlist_denies_access(self):
        with patch.object(bot, 'ALLOWED_USER_IDS', ''):
            self.assertFalse(bot.is_authorized(999))

    def test_fallback_preserves_relative_date(self):
        result = bot.fallback_parse('fifteen thousand rent yesterday')
        self.assertEqual(result['description'], 'Rent')
        self.assertIsNotNone(result['date'])

    def test_sheet_write_uses_numeric_date_and_literal_text(self):
        ws = MagicMock()
        ws.col_values.return_value = ['Date']
        ws.row_count = 200
        bot.append_to_data_area(ws, ['04/09/2026', '12:30', 450, '=1+1', 'Food', 'UPI', '=1+1'])
        call = ws.update.call_args
        self.assertEqual(call.kwargs['value_input_option'], 'RAW')
        self.assertIsInstance(call.args[1][0][0], int)
        self.assertEqual(call.args[1][0][3], '=1+1')

    def test_legacy_dates_are_normalized_atomically(self):
        ws = MagicMock()
        ws.id = 12
        ws.title = 'September 2026'
        ws.col_values.return_value = ['Date', '01/09/2026', '15/09/2026']
        bot.sort_month_sheet(ws)
        request = ws.spreadsheet.batch_update.call_args.args[0]['requests']
        self.assertEqual(len(request), 2)
        serial = request[0]['updateCells']['rows'][0]['values'][0]['userEnteredValue']['numberValue']
        self.assertEqual(serial, (datetime(2026, 9, 1) - datetime(1899, 12, 30)).days)
        self.assertEqual(request[1]['sortRange']['range']['endColumnIndex'], 7)

    def test_mismatched_date_is_not_rewritten(self):
        ws = MagicMock()
        ws.title = 'September 2026'
        ws.col_values.return_value = ['Date', '09/01/2026']
        with self.assertRaises(ValueError):
            bot.sort_month_sheet(ws)
        ws.spreadsheet.batch_update.assert_not_called()


class AsyncRegressionTests(unittest.IsolatedAsyncioTestCase):
    async def test_stale_category_picker_cannot_save_new_pending_entry(self):
        update, context = MagicMock(), MagicMock()
        update.effective_user.id = 1
        update.callback_query.answer = AsyncMock()
        update.callback_query.edit_message_text = AsyncMock()
        update.callback_query.data = 'cat:old:0'
        context.user_data = {'pending_entry': {'_picker_token': 'new', 'amount': 500}}
        with patch.object(bot, 'ALLOWED_USER_IDS', '1'), patch.object(bot, 'get_spreadsheet') as sheet:
            await bot.handle_category_callback(update, context)
            sheet.assert_not_called()
        self.assertEqual(context.user_data['pending_entry']['amount'], 500)

    async def test_unauthorized_callback_cannot_write(self):
        update, context = MagicMock(), MagicMock()
        update.effective_user.id = 2
        update.callback_query.answer = AsyncMock()
        with patch.object(bot, 'ALLOWED_USER_IDS', '1'), patch.object(bot, 'get_spreadsheet') as sheet:
            await bot.handle_category_callback(update, context)
            sheet.assert_not_called()

    async def test_llm_partial_invalid_output_is_rejected(self):
        import json
        response = MagicMock()
        response.json.return_value = {'choices': [{'message': {'content': json.dumps([
            {'amount': 450, 'description': 'Chai'}, {'amount': -50, 'description': 'Cab'}])}}]}
        client = AsyncMock()
        client.post.return_value = response
        manager = MagicMock()
        manager.__aenter__ = AsyncMock(return_value=client)
        manager.__aexit__ = AsyncMock(return_value=False)
        with patch.object(bot, 'GROQ_API_KEY', 'test'), patch.object(bot.httpx, 'AsyncClient', return_value=manager):
            self.assertIsNone(await bot.parse_with_groq('sample'))

    async def test_calendar_month_excludes_future_and_non_month_tabs(self):
        class FixedDateTime(datetime):
            @classmethod
            def now(cls, tz=None):
                return cls(2026, 9, 16, 12, tzinfo=tz)
        update = MagicMock()
        update.effective_chat.type = 'private'
        update.message.reply_text = AsyncMock()
        ws = MagicMock()
        ws.title = 'September 2026'
        ws.row_count = 200
        ws.col_count = 12
        ws.get.return_value = [bot.HEADER_ROW,
            ['01/09/2026', '12:00', '450', 'Chai', 'Food'],
            ['30/09/2026', '12:00', '900', 'Future', 'Food']]
        old = MagicMock()
        old.title = 'Sheet1'
        sh = MagicMock()
        sh.worksheets.return_value = [ws, old]
        with patch.object(bot, 'datetime', FixedDateTime), patch.object(bot, 'get_spreadsheet', return_value=sh):
            await bot._send_summary(update, -1, 'This Month')
        self.assertIn('450', update.message.reply_text.call_args.args[0])
        self.assertNotIn('900', update.message.reply_text.call_args.args[0])
        old.get.assert_not_called()


if __name__ == '__main__':
    unittest.main()
