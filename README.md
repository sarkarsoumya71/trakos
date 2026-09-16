# Trakos

Personal Telegram expense tracking with Google Sheets output and optional bank-SMS imports.

Send `450 chai`, `2.5K uber via cash`, or one expense per line. Manual expense messages use Groq with a visible basic-parser fallback. Monthly tabs retain the original seven columns and category summaries.

## SMS workflow

```text
SMS Backup & Restore XML (Telegram upload or Drive folder)
    -> local bank parser -> SQLite ledger -> duplicate checks
    -> Telegram review -> SMS Ledger tab + approved monthly expenses
```

- Imported SMS never goes to Groq. Known HDFC/CBI sender patterns are parsed locally.
- Personal messages, outgoing SMS, OTPs and obvious failed/requested payments are skipped.
- Repeated files and identical messages are skipped. Matching bank/account/reference/date/amount/direction alerts share one transaction.
- Equal amounts alone never auto-merge. Possible duplicates remain separate for review.
- Review also checks existing monthly entries for equal amounts and dates.
- Transfers, card payments, withdrawals, income and refunds can stay in the ledger without entering spending totals.
- Explicit own-account transfers and credit-card bill payments are retained and automatically excluded from spending. Other parsed transactions start in review; a category choice approves a debit and learns a merchant-category suggestion.
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
