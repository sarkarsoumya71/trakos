# Trakos setup

For an existing deployment, retain the Telegram token, service-account credentials, Sheet ID and allowed-user list. Never put credentials in GitHub or chat.

## Existing bot

Set these process environment variables in the hosting dashboard:

| Variable | Value |
| --- | --- |
| TELEGRAM_TOKEN | Existing BotFather token |
| GOOGLE_CREDS_JSON | Complete service-account JSON |
| SHEET_ID | Existing Google Sheet ID |
| ALLOWED_USER_IDS | Your numeric Telegram ID; comma-separated for shared access |
| GROQ_API_KEY | Existing key; optional for basic parsing |
| GROQ_MODEL | openai/gpt-oss-120b by default |

Enable Google Sheets and Drive APIs. Share the Sheet with the service account as Editor. Run exactly one bot process, including during testing. The app reads process environment variables; copying .env.example to .env does not load it automatically.

## Activate SMS import on Railway

First validate a real backup with `python sms_cli.py path/to/sms-backup.xml` and check the bank templates.

1. Open Railway, select the existing Trakos project and service.
2. Add a Volume attached to this service with mount path `/data`. Follow [Railway's Volume instructions](https://docs.railway.com/volumes).
3. In the service Variables tab add:

   ```text
   SMS_IMPORT_ENABLED=1
   TRAKOS_DB_PATH=/data/trakos.sqlite3
   SMS_OWNER_USER_ID=<your allowed Telegram ID>
   SMS_POLL_SECONDS=3600
   ```

4. Initially leave SMS_DRIVE_FOLDER_ID empty. Private Telegram XML uploads work without starting Drive imports.
5. Deploy the reviewed code. The Dockerfile starts `python bot.py`. Check that the startup log says SMS import is enabled.
6. Upload a small SMS XML sample in a private Telegram chat. Inspect `/smsstatus`, `/review`, `/smsentry ID` and `/unparsed`. Approve validated examples and compare Sheet rows and totals.
7. Upload the same sample again: there should be no new transactions or expenses. Restart and verify pending reviews persist.

## Connect SMS Backup & Restore to Drive

1. In the phone app, open backup details and locate the Google Drive destination.
2. In Drive, share that folder with the service account's client_email as Viewer. Keep access restricted.
3. Copy its folder ID from the URL after `/folders/`.
4. Set SMS_DRIVE_FOLDER_ID to the ID in Railway and deploy the variable change.
5. Trakos scans direct child files named `sms*.xml` at startup and hourly. `/syncsms` forces a check.
6. Set the phone backup schedule to match the desired freshness; a daily schedule is suitable for daily review. Trakos cannot import messages the phone has not backed up.

Use unencrypted SMS-only XML under 20 MB, excluding MMS/call logs. See [SyncTech's FAQ](https://www.synctech.com.au/sms-backup-restore/sms-faqs/).

## Review and recovery

- In `/review`, choosing a category counts a debit as spending. **Keep, exclude from spending** retains income, refunds, transfers or card bill payments without adding them to totals.
- **Already counted / duplicate** keeps the audit record without adding a second expense.
- `/retrysms` retries Sheet sync. Do not manually re-enter expenses after a sync failure.
- `/exportledger` downloads CSV for Excel. It includes all statuses; filter status=approved for approved expenses.
- Keep the SQLite database on the Volume and arrange Volume backups. Original SMS and CSV exports complement database backups.
- Copy the existing Sheet before `/sort`. Legacy date conversion requires each displayed DD/MM/YYYY date to agree with its month tab.
- Clear SMS_DRIVE_FOLDER_ID to stop scheduled imports, or set SMS_IMPORT_ENABLED=0 to disable all SMS commands. Retain the Volume.

See [UPGRADE.md](UPGRADE.md) for known limits and validation still required. Hosting costs depend on your plan; this guide does not assume any provider is free.
