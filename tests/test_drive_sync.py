import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import bot
from sms_workflow import SMSWorkflow
from test_sms import DEBIT, xml


class DriveSyncTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        with patch.dict('os.environ', {'TRAKOS_DB_PATH': str(Path(self.temp.name) / 'db.sqlite3'),
                                      'SMS_OWNER_USER_ID': '1', 'SMS_DRIVE_FOLDER_ID': 'synthetic_folder'}), patch.object(bot, 'ALLOWED_USER_IDS', '1'):
            self.flow = SMSWorkflow(bot)

    def tearDown(self):
        self.temp.cleanup()

    def response(self, value):
        response = MagicMock()
        response.json.return_value = value
        return response

    def test_folder_poll_imports_once_and_paginates(self):
        session = MagicMock()
        session.__enter__.return_value = session
        download = MagicMock()
        download.__enter__.return_value = download
        download.iter_content.return_value = [xml(DEBIT)]
        folder = self.response({'mimeType': 'application/vnd.google-apps.folder'})
        first = self.response({'files': [{'id': 'backup1', 'name': 'sms-latest.xml', 'size': 1000}], 'nextPageToken': 'next'})
        second = self.response({'files': [{'id': 'other', 'name': 'calls-latest.xml', 'size': 1000}]})
        def get(url, **kwargs):
            if url.endswith('/synthetic_folder'):
                return folder
            if url.endswith('/backup1'):
                return download
            if url.endswith('/files'):
                return second if kwargs['params'].get('pageToken') else first
            raise AssertionError('Unexpected request')
        session.get.side_effect = get
        with patch.object(bot, 'GOOGLE_CREDS_JSON', '{}'), patch('sms_workflow.Credentials.from_service_account_info'), patch('sms_workflow.AuthorizedSession', return_value=session):
            first_report = self.flow.drive_import()
            second_report = self.flow.drive_import()
        self.assertEqual(first_report[0]['new'], 1)
        self.assertTrue(second_report[0]['already_imported'])
        self.assertEqual(len(self.flow.db.list(1)), 1)
        self.assertNotEqual(self.flow.last_drive_check, 'Not checked yet')

    def test_file_id_cannot_silently_act_as_empty_folder(self):
        session = MagicMock()
        session.__enter__.return_value = session
        session.get.return_value = self.response({'mimeType': 'text/xml'})
        with patch.object(bot, 'GOOGLE_CREDS_JSON', '{}'), patch('sms_workflow.Credentials.from_service_account_info'), patch('sms_workflow.AuthorizedSession', return_value=session):
            with self.assertRaises(ValueError):
                self.flow.drive_import()
        self.assertEqual(self.flow.last_drive_check, 'Not checked yet')

    def test_unshared_folder_does_not_report_success(self):
        session = MagicMock()
        session.__enter__.return_value = session
        session.get.return_value.raise_for_status.side_effect = PermissionError('unshared')
        with patch.object(bot, 'GOOGLE_CREDS_JSON', '{}'), patch('sms_workflow.Credentials.from_service_account_info'), patch('sms_workflow.AuthorizedSession', return_value=session):
            with self.assertRaises(PermissionError):
                self.flow.drive_import()
        self.assertEqual(self.flow.db.list(1), [])
        self.assertEqual(self.flow.last_drive_check, 'Not checked yet')
