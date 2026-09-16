"""Local SMS ingestion and durable review ledger. No SMS is sent to an LLM."""
from __future__ import annotations

import hashlib
import io
import json
import re
import sqlite3
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from zoneinfo import ZoneInfo

from defusedxml import ElementTree

TZ = ZoneInfo('Asia/Kolkata')
MAX_BYTES = 20 * 1024 * 1024
MAX_MESSAGES = 100000
BANKS = {
    'HDFC': r'hdfc',
    'CBI': r'cent(?:ral)?\s*bank|centbk|cbin|\bcbi\b',
}
MONEY = r'(?:INR|Rs\.?|₹)\s*([\d,]+(?:\.\d{1,2})?)(?!\d)'
ACCOUNT = r'(?:a/c|ac(?:ct)?(?:ount)?|card)\s*(?:no\.?|number|ending(?:\s+in)?|xx)?\s*[:.*xX -]*([\d*xX]{4,})'


def fingerprint(*values):
    return hashlib.sha256(json.dumps(values, ensure_ascii=False).encode()).hexdigest()


@dataclass
class Transaction:
    occurred_at: str
    amount_paise: int
    direction: str
    bank: str
    account: str
    merchant: str
    reference: str
    payment: str
    kind: str
    category: str | None = None


def parse_sms(sender: str, body: str, timestamp_ms: str):
    """Return (transaction, reason). Unsupported bank alerts remain reviewable.

    Templates are deliberately conservative until tested against the owner's SMS.
    Balance, OTP, request, failed-payment and promotional amounts aren't expenses.
    """
    bank = next((name for name, pattern in BANKS.items()
                 if re.search(pattern, sender, re.I)), '')
    if not bank:
        return None, 'non_bank'
    if re.search(r'\b(?:OTP|one.time password|verification code)\b', body, re.I):
        return None, 'otp'
    if re.search(r'\b(?:failed|declined|unsuccessful|will be|scheduled|due date|payment due|requested|request to pay)\b', body, re.I):
        return None, 'non_transaction'
    direction_patterns = [
        ('debit', rf'{MONEY}\s*(?:has been |is |was |been )?(?:debited|spent|paid|withdrawn|sent)\b'),
        ('credit', rf'{MONEY}\s*(?:has been |is |was |been )?(?:credited|received|refunded|reversed)\b'),
        ('debit', rf'\b(?:debited|spent|paid|withdrawn|sent)\s*(?:by |with |for |of |: )?{MONEY}'),
        ('credit', rf'\b(?:credited|received|refunded|reversed)\s*(?:by |with |for |of |: )?{MONEY}'),
    ]
    matches = [(direction, match) for direction, pattern in direction_patterns
               for match in re.finditer(pattern, body, re.I)]
    unique = {(d, m.group(1)) for d, m in matches}
    if len(unique) != 1:
        return None, 'unsupported_or_ambiguous_amount'
    direction, amount = next(iter(unique))
    try:
        paise = Decimal(amount.replace(',', '')) * 100
        if not paise.is_finite() or paise <= 0 or paise != paise.to_integral_value():
            return None, 'invalid_amount'
        occurred = datetime.fromtimestamp(int(timestamp_ms) / 1000, TZ)
    except (ValueError, OverflowError, OSError, InvalidOperation):
        return None, 'invalid_amount_or_time'
    # Prefer the explicit bank transaction date over delivery time.
    date_match = re.search(r'\bon\s+(\d{1,2}[-/]\d{1,2}[-/]\d{2,4})(?:\s+(\d{2}:\d{2}(?::\d{2})?))?', body, re.I)
    if date_match:
        raw_date = date_match.group(1).replace('-', '/')
        try:
            fmt = '%d/%m/%Y' if len(raw_date.split('/')[-1]) == 4 else '%d/%m/%y'
            date = datetime.strptime(raw_date, fmt)
            occurred = occurred.replace(year=date.year, month=date.month, day=date.day)
            if date_match.group(2):
                h, m, *s = map(int, date_match.group(2).split(':'))
                occurred = occurred.replace(hour=h, minute=m, second=s[0] if s else 0)
        except ValueError:
            return None, 'invalid_transaction_date'
    account_match = re.search(ACCOUNT, body, re.I)
    account = re.sub(r'\D', '', account_match.group(1))[-4:] if account_match else ''
    ref = re.search(r'\b(?:UPI\s*(?:Ref(?:erence)?(?:\s*No\.?)?|txn(?:\s*id)?)|UTR|RRN|Ref(?:erence)?(?:\s*(?:No\.?|ID))?)\s*[:#.-]?\s*([A-Z0-9]{6,40})\b', body, re.I)
    reference = ref.group(1).upper() if ref and any(c.isdigit() for c in ref.group(1)) else ''
    merchant_match = re.search(r'\b(?:to|at|from)\s+(?!(?:your\s+)?(?:a/c|account|card)\b)(.+?)(?=\s+(?:on|via|using|UPI|Ref|UTR|Avl|Available|Bal|from\s+a/c)\b|[;\n]|$)', body, re.I)
    merchant = merchant_match.group(1).strip(' .,-')[:120] if merchant_match else ''
    if merchant and re.match(r'(?:your\s+)?(?:a/c|account|card)\b', merchant, re.I):
        merchant = ''
    kind = 'expense' if direction == 'debit' else 'income'
    if re.search(r'\b(?:refund|refunded|reversal|reversed)\b', body, re.I):
        kind = 'refund'
    elif re.search(r'(?:credit\s*card|card\s+bill).*(?:payment|paid)|payment.*credit\s*card', body, re.I):
        kind = 'card_payment'
    elif re.search(r'\b(?:self transfer|own account)\b', body, re.I):
        kind = 'transfer'
    elif re.search(r'\b(?:ATM|cash withdrawal|withdrawn)\b', body, re.I):
        kind = 'cash_withdrawal'
    payment = 'UPI' if re.search(r'\bUPI\b', body, re.I) else 'BANK'
    if account_match and account_match.group(0).lower().startswith('card'):
        payment = 'CARD1' if bank == 'HDFC' else 'CARD2'
    return Transaction(occurred.isoformat(), int(paise), direction, bank, account,
                       merchant, reference, payment, kind), 'parsed'


class Ledger:
    def __init__(self, path):
        self.path = str(path)
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript('''
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS transactions (
                    id INTEGER PRIMARY KEY, occurred_at TEXT NOT NULL,
                    amount_paise INTEGER NOT NULL, direction TEXT NOT NULL,
                    bank TEXT NOT NULL, account TEXT NOT NULL, merchant TEXT NOT NULL,
                    reference TEXT NOT NULL, payment TEXT NOT NULL, kind TEXT NOT NULL,
                    category TEXT, status TEXT NOT NULL DEFAULT 'review',
                    possible_duplicate INTEGER, reason TEXT NOT NULL DEFAULT '',
                    exported INTEGER NOT NULL DEFAULT 0, owner INTEGER NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE INDEX IF NOT EXISTS transaction_ref ON transactions(owner, bank, account, reference);
                CREATE TABLE IF NOT EXISTS messages (
                    fingerprint TEXT PRIMARY KEY, transaction_id INTEGER,
                    sender TEXT NOT NULL, body TEXT NOT NULL, timestamp_ms TEXT NOT NULL,
                    reason TEXT NOT NULL, owner INTEGER NOT NULL,
                    FOREIGN KEY(transaction_id) REFERENCES transactions(id)
                );
                CREATE TABLE IF NOT EXISTS imports (
                    fingerprint TEXT PRIMARY KEY, report TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS merchant_rules (
                    owner INTEGER, bank TEXT, merchant TEXT, category TEXT,
                    PRIMARY KEY(owner, bank, merchant)
                );
            ''')

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        try:
            with db:
                yield db
        finally:
            db.close()

    def import_xml(self, payload: bytes, owner: int, categorize=lambda _: None):
        if len(payload) > MAX_BYTES:
            raise ValueError('Backup exceeds 20 MB; export SMS only, without MMS/call logs.')
        file_key = fingerprint(owner, hashlib.sha256(payload).hexdigest())
        report = dict(new=0, duplicate=0, ignored=0, unparsed=0, possible_duplicate=0)
        # The whole file commits atomically. A malformed tail cannot leave a half-import.
        with self.connect() as db:
            if db.execute('SELECT 1 FROM imports WHERE fingerprint=?', (file_key,)).fetchone():
                return dict(report, already_imported=True)
            events = ElementTree.iterparse(io.BytesIO(payload), events=('start', 'end'), forbid_dtd=True)
            root = None
            count = 0
            for event, element in events:
                if root is None:
                    root = element
                    if root.tag != 'smses':
                        raise ValueError('Expected an SMS Backup & Restore XML with an smses root.')
                if event != 'end':
                    continue
                if element.tag != 'sms':
                    continue
                count += 1
                if count > MAX_MESSAGES:
                    raise ValueError('Backup contains too many SMS messages.')
                attrs = element.attrib
                if attrs.get('type') != '1':
                    report['ignored'] += 1
                    element.clear()
                    continue
                sender, body, timestamp = (attrs.get(k, '') for k in ('address', 'body', 'date'))
                key = fingerprint(owner, sender.strip().upper(), timestamp, body.strip())
                if db.execute('SELECT 1 FROM messages WHERE fingerprint=?', (key,)).fetchone():
                    report['duplicate'] += 1
                    element.clear()
                    continue
                tx, reason = parse_sms(sender, body, timestamp)
                if reason in ('non_bank', 'otp', 'non_transaction'):
                    report['ignored'] += 1
                    element.clear()
                    continue
                tx_id = None
                if tx:
                    exact = None
                    if tx.account and tx.reference:
                        exact = db.execute('''SELECT id FROM transactions WHERE owner=? AND bank=?
                            AND account=? AND reference=? AND direction=? AND amount_paise=?
                            AND substr(occurred_at,1,10)=?''',
                            (owner, tx.bank, tx.account, tx.reference, tx.direction, tx.amount_paise, tx.occurred_at[:10])).fetchone()
                    if exact:
                        tx_id = exact['id']
                        report['duplicate'] += 1
                    else:
                        candidate = db.execute('''SELECT id FROM transactions WHERE owner=?
                            AND amount_paise=? AND substr(occurred_at,1,10)=?
                            AND status != 'duplicate'
                            AND ((direction=? AND bank=? AND (account=? OR account='' OR ?=''))
                                 OR (direction!=? AND reference!='' AND reference=?))
                            ORDER BY id LIMIT 1''',
                            (owner, tx.amount_paise, tx.occurred_at[:10], tx.direction, tx.bank,
                             tx.account, tx.account, tx.direction, tx.reference)).fetchone()
                        rule = db.execute('SELECT category FROM merchant_rules WHERE owner=? AND bank=? AND merchant=?',
                                          (owner, tx.bank, tx.merchant.casefold())).fetchone()
                        tx.category = rule['category'] if rule else categorize(tx.merchant)
                        data = asdict(tx)
                        data.update(owner=owner, possible_duplicate=candidate['id'] if candidate else None,
                                    reason='Check possible duplicate/own transfer' if candidate else 'Confirm transaction and category')
                        columns = ','.join(data)
                        tx_id = db.execute(f'INSERT INTO transactions ({columns}) VALUES ({",".join("?" for _ in data)})', tuple(data.values())).lastrowid
                        report['new'] += 1
                        if candidate:
                            report['possible_duplicate'] += 1
                else:
                    report['unparsed'] += 1
                db.execute('INSERT INTO messages VALUES (?,?,?,?,?,?,?)',
                           (key, tx_id, sender, body, timestamp, reason, owner))
                element.clear()
            db.execute('INSERT INTO imports (fingerprint,report) VALUES (?,?)', (file_key, json.dumps(report)))
        return report

    def get(self, tx_id, owner):
        with self.connect() as db:
            row = db.execute('SELECT * FROM transactions WHERE id=? AND owner=?', (tx_id, owner)).fetchone()
            return dict(row) if row else None

    def list(self, owner, status=None, limit=50):
        with self.connect() as db:
            query = 'SELECT * FROM transactions WHERE owner=?'
            args = [owner]
            if status:
                query += ' AND status=?'
                args.append(status)
            return [dict(r) for r in db.execute(query + ' ORDER BY id LIMIT ?', (*args, limit))]

    def body(self, tx_id, owner):
        with self.connect() as db:
            row = db.execute('SELECT body FROM messages WHERE transaction_id=? AND owner=? LIMIT 1', (tx_id, owner)).fetchone()
            return row['body'] if row else ''

    def unparsed(self, owner, limit=5):
        with self.connect() as db:
            return [dict(r) for r in db.execute('SELECT sender,body,reason FROM messages WHERE owner=? AND transaction_id IS NULL LIMIT ?', (owner, limit))]

    def resolve(self, tx_id, owner, action, category=None):
        if action not in ('expense', 'exclude', 'duplicate'):
            raise ValueError('Invalid review action')
        with self.connect() as db:
            tx = db.execute('SELECT * FROM transactions WHERE id=? AND owner=?', (tx_id, owner)).fetchone()
            if not tx or tx['status'] != 'review':
                return False
            if action == 'expense' and (tx['direction'] != 'debit' or not category):
                raise ValueError('Only a categorized debit can be approved as spending')
            db.execute('UPDATE transactions SET status=?,category=? WHERE id=?',
                       ('approved' if action == 'expense' else action, category or tx['category'], tx_id))
            if action == 'expense' and tx['merchant']:
                db.execute('INSERT OR REPLACE INTO merchant_rules VALUES (?,?,?,?)',
                           (owner, tx['bank'], tx['merchant'].casefold(), category))
            return True

    def mark_exported(self, tx_id, owner):
        with self.connect() as db:
            db.execute('UPDATE transactions SET exported=1 WHERE id=? AND owner=?', (tx_id, owner))

    def stats(self, owner):
        with self.connect() as db:
            return {r['status']: r['n'] for r in db.execute('SELECT status,COUNT(*) n FROM transactions WHERE owner=? GROUP BY status', (owner,))}
