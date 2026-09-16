# Trakos v6 upgrade notes

## Verified baseline and fixes

Started from actual repository commit `b84ba46`, rather than reconstructed chat code.

- Groq already used `openai/gpt-oss-120b` and returned arrays. The model is now configurable and the prompt year is dynamic.
- Reproduced substring category errors (`service fee` matched `vi`; `water bill` matched `water`). Matching now uses word boundaries and prefers longer phrases.
- An empty allowlist previously granted access. Startup now requires a valid allowlist; category callbacks also enforce it.
- Native-cell inspection confirmed the connected Sheet's US locale had stored early-month dates with month/day reversed, while later dates were text.
- New dates use numeric serials and `dd/mm/yyyy` formatting. RAW text writes prevent descriptions/SMS from becoming formulas.
- Sorting normalizes displayed DD/MM dates only if every date matches its month tab. Conversion and sorting use one atomic Sheets batch. Mismatches require review.
- `/month` means the current calendar month through today. Summaries ignore non-month tabs and stop on worksheet read failures rather than silently reporting partial totals.
- Category buttons are bound to their pending entry; old buttons cannot categorize a new expense. Pending dates are fixed before category selection.

## Bank-template validation

The supplied real backup exposed missing templates and merchant/date extraction errors. These are corrected using synthetic test examples, without committing private SMS. INR Autopay successes and direct debit/card alerts can overlap; uncertain matches stay in review. Mandate IDs are not treated as transaction references. Foreign-currency Autopay receipts stay separate from INR spending. Generic CBI balance alerts often contain no merchant, so category input may still be needed. Drive polling now verifies that the source is an accessible folder before claiming success.

## Persistence and recovery

`sms_import.py` owns local parsing, atomic XML imports, integer-paise amounts, duplicate candidates, review decisions and merchant rules. `sms_workflow.py` owns Drive polling, Telegram review and Sheet projections. `sms_cli.py` provides an offline preview.

SQLite is the source of truth for imported SMS. Historical/manual expenses remain in the original monthly Sheets tabs. The new `SMS Ledger` tab contains all parsed imported transactions and their review status. Only approved debit expenses enter monthly tabs.

A stable `[trakos-sms:...]` marker in Raw Input supports retries after a crash between a Sheets write and the local export flag. Do not edit these markers. SMS operations and Sheet writes are serialized within one process. Run exactly one replica, retain the SQLite database on a persistent Volume, and back it up. CSV is an export, not a full database restore.

## Validation

- 51 automated tests cover manual regressions, dates, auth, stale callbacks, incomplete LLM output, amounts versus balances, overlapping backups, duplicate references, equal-amount separate purchases, account/direction separation, persistence, double clicks, hostile/malformed XML, exclusions and crash/retry recovery.
- Live Groq smoke test passed with two synthetic expenses; no real bank SMS was sent to Groq.
- Dependency checks and Python compilation passed locally. CI targets Python 3.12 to match Docker.
- Railway now has a persistent Volume mounted at `/data` for the SMS database.
- Initial development did not deploy code or mutate the production Sheet.

## Activation completed — 16 September 2026

1. The dedicated Google Drive backup folder is configured and the production service account can read its SMS XML. The chat Drive connector does not list these XML files; direct service-account access was verified.
2. Real HDFC/CBI template checks cover ISO card timestamps, multiline UPI payees, CBI NEFT credits with three decimal places, deductions, POS purchases and successful Autopay notices. Synthetic regression fixtures protect privacy. The first production import and Sheet projection succeeded. Review rendering was checked against a copy of the production database with replies captured locally, without sending Telegram messages or approving real spending.
3. The accounting policy is confirmed: own-account transfers and credit-card bill payments stay in the ledger and are excluded from spending. Explicitly identified types are excluded automatically; uncertain transactions remain in review, with dedicated buttons to confirm either type. Equal amounts alone never establish account ownership.
4. Hourly Drive polling is enabled. Restart verification confirmed the database persisted and the repeated import did not duplicate Sheet rows. A private local SQLite snapshot passed its integrity check; it is excluded from Git and Docker builds.
5. A native copy of the existing expense Sheet was created and verified before deployment. Legacy date normalization runs when a monthly sheet is sorted. Historical dates are inferred from displayed DD/MM values and month-tab names; inconsistent rows need review.

## Current limits

- Unencrypted SMS XML only, maximum 20 MB and 100,000 messages. No MMS, call logs, ZIPs or encrypted backups.
- Conservative English templates and known HDFC/CBI senders only. Other bank senders are ignored; unsupported known-bank alerts are retained.
- Every new spending candidate needs review in this initial rollout; explicit own-account transfers and card bill payments are excluded automatically. Merchant learning suggests categories, not automatic approval.
- Fuzzy matches and own transfers are flagged, not automatically merged. Different bank legs can be flagged when they share an explicit reference; other transfers require owner review.
- Income/refunds remain in the ledger but do not reduce spending totals. No net-cash-flow dashboard is included.
- Existing monthly overlaps are checked at SMS review time. Later manual duplicates are not automatically reconciled against previously approved SMS.
- Old unsupported imports are not automatically reparsed after parser changes. `/unparsed` shows the first five; dismissal/editing is not implemented.
- Drive scans direct child files named `sms*.xml`, revisits backups, and uses content fingerprints. Set the exact nested folder ID if needed.
- Freshness is limited by the phone's backup schedule. Hourly polling cannot make weekly backups current.
- Automatic import, Sheet projection, restart persistence, and duplicate prevention have been verified in production. Actual Telegram button interaction still needs the owner's first review. A complete restore after Volume loss and recurring database snapshots are not configured. Foreign-currency receipts remain unparsed and are never converted into INR by guessing.

## Primary sources checked

- [SyncTech XML and backup FAQs](https://www.synctech.com.au/sms-backup-restore/sms-faqs/)
- [Groq supported models](https://console.groq.com/docs/models) — the handover's Llama decommission claim was not assumed to be correct.
- [Railway Volumes](https://docs.railway.com/volumes)
