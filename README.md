# Trakos

Personal Telegram expense tracking with Google Sheets output and optional bank-SMS imports.

Send `450 chai`, `2.5K uber via cash`, or one expense per line. Manual expense messages use Groq with a visible basic-parser fallback. Monthly tabs provide purchase descriptions, categories, separate bank messages and an overview with a spending chart.

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
- Explicit own-account transfers, credit-card bill payments, credits/refunds and withdrawals are retained outside spending. Routine debit expenses are categorized and saved automatically when `SMS_AUTO_APPROVE=1`. Missing/unclear merchants stay in Needs details and receive category/description choices on Telegram. Confirmed merchant choices override the model; model guesses are never learned as confirmed rules.
- A Telegram notification follows a successful Sheet sync. A large backlog gets a summary; smaller batches include each expense. Delivery retries after failure, with notification state persisted in SQLite. A crash after Telegram accepts a message but before acknowledgement is saved can repeat a notification, but not an expense.
- An optional, bounded historical-overlap policy can keep possible duplicates outside totals without requiring review. It never applies to transactions above the configured backlog ID.
- Unsupported known-bank alerts are inspectable with `/unparsed`.

**SMS import is off by default.** Validate real bank templates before activation. See [setup](SETUP.md) and [upgrade notes](UPGRADE.md).

## Commands

| Command | Purpose |
| --- | --- |
| `/check` or `/report` | Fetch available Drive uploads, then show today, this week and this month |
| `/others` or `/review` | Review purchases needing details in the current month |
| `/today`, `/week`, `/month` | Detailed view for today, this calendar week, or this calendar month |
| `/sheet`, `/categories`, `/help` | Existing bot tools |
| `/sort` | Sort data by date, keeping each entry and its metadata together |
| `/cancel` | Cancel pending manual category choices |
| `/syncsms` | Import from the configured Drive folder |
| `/edit [request]` | Find a purchase by amount/date and confirm description/category edits |
| `/breakdown Category` | Show confirmed purchases/services within a category |
| `/smsentry ID` | Inspect a possible duplicate |
| `/smscategory ID Category` | Correct a saved expense and learn your merchant choice |
| `/smsstatus` | Review counts and Drive-check status |
| `/unparsed` | Show up to five unsupported bank alerts |
| `/retrysms` | Retry syncing saved decisions to Sheets |
| `/exportledger` | Download an Excel-compatible SMS ledger CSV |

SMS commands require a private chat with an allowed user. All allowed users share the configured Google Sheet; this is not a private multi-user product.

## Daily spending report

Reports read recorded expenses from monthly tabs, not the SMS audit ledger, so excluded transfers and possible duplicates are not counted twice. Amounts are summed in paise and grouped by category. `/today` and the combined report also list up to eight purchases; longer lists remain in the Sheet. `/week` is Monday through today, including the previous month's tab when necessary. `/month` starts on the first day of the current calendar month, never a rolling 30 days.

Enable `DAILY_REPORT_ENABLED=1` and set `DAILY_REPORT_TIME=22:00` for a nightly report in Asia/Kolkata time. The existing SMS workflow, owner and persistent SQLite database are used. The scheduler checks each minute, refreshes Drive before reporting, and retries delivery failures after five minutes. A delivered-date record prevents normal restart duplicates. A crash immediately after Telegram accepts a message but before the local acknowledgement can repeat that report. If the bot restarts after the scheduled time, it sends that day's report once; it does not send older missed days.

The report shows the latest observed SMS-backup upload time. A failed SMS refresh is disclosed; a failed Sheet read never produces a misleading zero or partial total. Reports are spending summaries, not available-bank-balance calculations. Phone backup frequency still controls SMS freshness.

`/check` refreshes the available Drive backups before calculating its report. It cannot start SMS Backup & Restore on the phone: the existing connection grants Drive access, not remote phone control. Use the phone's scheduled backups or Back Up Now, then `/check` after uploading.

## Purchase details and cleanup

`/review` includes manual and SMS purchases needing details. Category buttons, short replies, or phone keyboard dictation prepare an edit for confirmation. `/edit the 300 payment on 17 September was an Uber ride` searches the current month; ambiguous matches offer a picker. Description/category edits require Apply and reject stale records. Amount/date edits are not supported by this conversational editor. Voice notes are not transcribed.

The editor sends purchase dates, amounts, descriptions and categories to the configured Groq model to identify the requested entry. It does not send the Bank Message column. Raw Input retains what you manually entered; Bank Message holds the original alert. Unknown descriptions/categories remain blank with Needs details status, never Other.

Current/new monthly sheets have Date, Time, Amount, Description, Category, Payment Method, Raw Input, Bank Message, hidden Entry ID, Status, Treatment and Source. Each overview contains confirmed spending, pending amounts, possible duplicates, separate investments, a category dropdown with purchase totals, and a doughnut chart. These are live formulas. Only confirmed expenses enter spending totals; investments and amounts needing clarification remain visible separately.

Near matches between manual and SMS entries (same day, within two rupees) are candidates, never proof. A confirmed merge retains the selected entry and links the original bank message. A durable merge record recovers interrupted writes. Exact matching SMS references remain idempotent.

The startup migration converts the current month and takes a SQLite snapshot before ledger changes. An optional private `TRAKOS_CLEANUP_PLAN` applies explicitly reviewed, identified corrections once; keep this plan out of source control. Back up the live workbook before migration. Older tabs retain their original schema until written to; reports can read both layouts.

## Local checks

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements.txt
.\.venv\Scripts\python -m unittest discover -s tests -v
.\.venv\Scripts\python sms_cli.py 'C:\path\sms-backup.xml'
```

The CLI prints counts using a temporary database and never contacts Telegram, Google or Groq. Keep real backups outside the repository.
