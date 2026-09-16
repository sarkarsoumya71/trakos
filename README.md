# Trakos

Personal Telegram expense tracking with Google Sheets output and optional bank-SMS imports.

Send `450 chai`, `2.5K uber via cash`, or one expense per line. Manual expense messages use Groq with a visible basic-parser fallback. Monthly tabs retain the original seven columns and category summaries.

## SMS workflow

```text
SMS Backup & Restore XML (Telegram upload or Drive folder)
    -> local bank parser -> SQLite ledger -> duplicate checks
    -> GPT-OSS merchant categories -> SMS Ledger tab + monthly expenses + Telegram notification
```

- Known HDFC/CBI sender patterns and amounts are parsed locally. With automatic categorization enabled, only sanitized merchant names go to Groq. Raw SMS, account numbers, references, balances and amounts are not included in category requests.
- Personal messages, outgoing SMS, OTPs and obvious failed/requested payments are skipped.
- Repeated files and identical messages are skipped. Matching bank/account/reference/date/amount/direction alerts share one transaction.
- Equal amounts alone never auto-merge. Possible duplicates remain separate for review.
- Automatic processing checks existing monthly entries and SMS references before booking expenses. An ambiguous match is held aside; equal amounts alone never prove a duplicate.
- Transfers, card payments, withdrawals, income and refunds can stay in the ledger without entering spending totals.
- Explicit own-account transfers, credit-card bill payments, credits/refunds and withdrawals are retained outside spending. Routine debit expenses are categorized and saved automatically when `SMS_AUTO_APPROVE=1`. Missing/unclear merchants use Other. Confirmed merchant choices override the model; model guesses are never learned as confirmed rules.
- A Telegram notification follows a successful Sheet sync. A large backlog gets a summary; smaller batches include each expense. Delivery retries after failure, with notification state persisted in SQLite. A crash after Telegram accepts a message but before acknowledgement is saved can repeat a notification, but not an expense.
- An optional, bounded historical-overlap policy can keep possible duplicates outside totals without requiring review. It never applies to transactions above the configured backlog ID.
- Unsupported known-bank alerts are inspectable with `/unparsed`.

**SMS import is off by default.** Validate real bank templates before activation. See [setup](SETUP.md) and [upgrade notes](UPGRADE.md).

## Commands

| Command | Purpose |
| --- | --- |
| `/today`, `/week`, `/month` | Today, last seven calendar days, calendar month through today |
| `/sheet`, `/categories`, `/help` | Existing bot tools |
| `/sort` | Normalize matching monthly dates and sort A:G, preserving H:I |
| `/cancel` | Cancel pending manual category choices |
| `/syncsms` | Import from the configured Drive folder |
| `/review` | Review the next imported transaction |
| `/smsentry ID` | Inspect a possible duplicate |
| `/smscategory ID Category` | Correct a saved expense and learn your merchant choice |
| `/smsstatus` | Review counts and Drive-check status |
| `/unparsed` | Show up to five unsupported bank alerts |
| `/retrysms` | Retry syncing saved decisions to Sheets |
| `/exportledger` | Download an Excel-compatible SMS ledger CSV |

SMS commands require a private chat with an allowed user. All allowed users share the configured Google Sheet; this is not a private multi-user product.

## Local checks

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements.txt
.\.venv\Scripts\python -m unittest discover -s tests -v
.\.venv\Scripts\python sms_cli.py 'C:\path\sms-backup.xml'
```

The CLI prints counts using a temporary database and never contacts Telegram, Google or Groq. Keep real backups outside the repository.
