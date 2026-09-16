"""
Trakos — Telegram expense tracker that logs to Google Sheets.
v5: Groq LLM for intelligent parsing + category detection.
"""

import os
import re
import json
import logging
import math
import sys
import threading
import secrets
from functools import wraps
from pathlib import Path
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
import gspread
from google.oauth2.service_account import Credentials
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.helpers import escape_markdown
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    PicklePersistence,
    filters,
    ContextTypes,
)

# ─── Config ───────────────────────────────────────────────
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
GOOGLE_CREDS_JSON = os.environ.get("GOOGLE_CREDS_JSON", "")
SHEET_ID = os.environ.get("SHEET_ID", "")
ALLOWED_USER_IDS = os.environ.get("ALLOWED_USER_IDS", "")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
GROQ_MODEL = os.environ.get("GROQ_MODEL", "openai/gpt-oss-120b")
TIMEZONE = ZoneInfo("Asia/Kolkata")
SHEET_LOCK = threading.RLock()


def sheet_locked(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        with SHEET_LOCK:
            return function(*args, **kwargs)
    return wrapped

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("trakos")
logging.getLogger("httpx").setLevel(logging.WARNING)

# ─── Google Sheets ────────────────────────────────────────
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

HEADER_ROW = ["Date", "Time", "Amount", "Description", "Category", "Payment Method", "Raw Input"]

CATEGORY_LIST = [
    "Food",
    "Transport",
    "Shopping",
    "Subscriptions",
    "Business",
    "Health",
    "Financial Investment",
    "Business Investment",
    "Bills & Utilities",
    "Other",
]

PAYMENT_METHODS = ["UPI", "CARD1", "CARD2", "CASH", "BANK"]


def get_spreadsheet():
    creds_dict = json.loads(GOOGLE_CREDS_JSON)
    creds = Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
    gc = gspread.authorize(creds)
    return gc.open_by_key(SHEET_ID)


@sheet_locked
def get_month_sheet(sh, target_date: datetime):
    month_name = target_date.strftime("%B %Y")
    try:
        return sh.worksheet(month_name)
    except gspread.exceptions.WorksheetNotFound:
        pass

    ws = sh.add_worksheet(title=month_name, rows=200, cols=9)

    ws.update("A1:G1", [HEADER_ROW])
    ws.format("A1:G1", {
        "textFormat": {"bold": True, "fontSize": 10},
        "backgroundColor": {"red": 0.15, "green": 0.15, "blue": 0.15},
        "horizontalAlignment": "CENTER",
    })

    ws.update("H1", [["SUMMARY"]], raw=False)
    ws.update("H2", [[month_name]], raw=False)
    ws.update("H3", [["Total Spent:"]], raw=False)
    ws.update("I3", [['=SUM(C2:C)']], raw=False)
    ws.update("H4", [["Entries:"]], raw=False)
    ws.update("I4", [['=COUNTA(A2:A)']], raw=False)

    ws.update("H6", [["BY CATEGORY"]], raw=False)
    for i, cat in enumerate(CATEGORY_LIST):
        r = 7 + i
        ws.update(f"H{r}", [[cat]], raw=False)
        ws.update(f"I{r}", [[f'=SUMPRODUCT((E$2:E=H{r})*C$2:C)']], raw=False)

    ws.format("H1:H2", {"textFormat": {"bold": True}})
    ws.format("H3:H4", {"textFormat": {"bold": True}})
    ws.format("H6", {"textFormat": {"bold": True}})
    ws.format("I3", {"numberFormat": {"type": "NUMBER", "pattern": "#,##0"}})

    return ws


@sheet_locked
def append_to_data_area(ws, row_data):
    """Append a row to the next empty row in column A, then sort by date ascending."""
    col_a = ws.col_values(1)
    if not col_a or col_a[0] != "Date":
        raise ValueError("Expense sheet header is missing")
    next_row = len(col_a) + 1
    cell_range = f"A{next_row}:G{next_row}"
    row_data = list(row_data)
    date = datetime.strptime(row_data[0], "%d/%m/%Y")
    row_data[0] = (date - datetime(1899, 12, 30)).days
    if next_row > ws.row_count:
        ws.add_rows(max(200, next_row - ws.row_count))
    # RAW keeps descriptions and SMS bodies literal, even when they start with '='.
    # Date serials avoid Google Sheets locale-dependent date interpretation.
    ws.format(f"A{next_row}", {"numberFormat": {"type": "DATE", "pattern": "dd/mm/yyyy"}})
    ws.format(cell_range, {"horizontalAlignment": "CENTER"})
    ws.update(cell_range, [row_data], value_input_option="RAW")
    
    # Sort data rows by date (column A) ascending
    if next_row > 2:
        try:
            sort_month_sheet(ws)
        except Exception as exc:
            # A successful append must not be reported as failed if sorting fails.
            log.warning("Expense saved; sorting deferred (%s)", type(exc).__name__)


@sheet_locked
def sort_month_sheet(ws):
    """Normalize legacy displayed DD/MM dates only when every date matches its tab."""
    month = datetime.strptime(ws.title, "%B %Y")
    values = ws.col_values(1)
    if not values or values[0] != "Date":
        return
    dates = []
    for value in values[1:]:
        if not value:
            dates.append([""])
            continue
        parsed = datetime.strptime(value, "%d/%m/%Y")
        if (parsed.year, parsed.month) != (month.year, month.month):
            raise ValueError("Date does not match month tab; review before sorting")
        dates.append([(parsed - datetime(1899, 12, 30)).days])
    if dates:
        ws.spreadsheet.batch_update({'requests': [
            {'updateCells': {'start': {'sheetId': ws.id, 'rowIndex': 1, 'columnIndex': 0},
                'rows': [{'values': [{'userEnteredValue': {'numberValue': d[0]} if d[0] != '' else {'stringValue': ''},
                    'userEnteredFormat': {'numberFormat': {'type': 'DATE', 'pattern': 'dd/mm/yyyy'}}}]} for d in dates],
                'fields': 'userEnteredValue,userEnteredFormat.numberFormat'}},
            {'sortRange': {'range': {'sheetId': ws.id, 'startRowIndex': 1,
                'endRowIndex': len(values), 'startColumnIndex': 0, 'endColumnIndex': 7},
                'sortSpecs': [{'dimensionIndex': 0, 'sortOrder': 'ASCENDING'}]}}
        ]})


# ─── Groq LLM Parser ────────────────────────────────────
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"

SYSTEM_PROMPT = """You are a financial expense parser. Given a user's message about expenses, extract each expense as a separate entry.

For EACH expense, extract:
1. amount: The numeric amount in INR. Handle words ("fifteen thousand" = 15000), suffixes ("2.5K" = 2500, "1.5L" = 150000), math ("272+272" = 544). Required.
2. description: What the expense was for. Clean it up, Title Case. Required.
3. date: The date of the expense in DD/MM/YYYY format. If not mentioned, set to null (the system will use today). Handle "yesterday", "18th April", "last Monday", etc. Use the year from today's date unless stated otherwise.
4. category: One of these exact values: Food, Transport, Shopping, Subscriptions, Business, Health, Financial Investment, Business Investment, Bills & Utilities, Other. Pick the best match based on context. If genuinely ambiguous, set to null.
5. payment: Payment method. One of: UPI, CARD1, CARD2, CASH, BANK. Default to UPI if not mentioned. Look for keywords like "via cash", "using card", "gpay/phonepe/paytm" = UPI.

Today's date is {today}.

ALWAYS respond with a JSON array, even for a single expense. No markdown, no explanation.
[{{"amount": number, "description": "string", "date": "DD/MM/YYYY" or null, "category": "string" or null, "payment": "string"}}]

Examples:
- "On 7th April I spent 43 rupees on document printing" -> [{{"amount": 43, "description": "Document Printing", "date": "07/04/2026", "category": "Business", "payment": "UPI"}}]
- "880, Kling, 25th April" -> [{{"amount": 880, "description": "Kling", "date": "25/04/2026", "category": "Subscriptions", "payment": "UPI"}}]
- "450 chai\\n2000 uber\\n500 gym" -> [{{"amount": 450, "description": "Chai", "date": null, "category": "Food", "payment": "UPI"}},{{"amount": 2000, "description": "Uber", "date": null, "category": "Transport", "payment": "UPI"}},{{"amount": 500, "description": "Gym", "date": null, "category": "Health", "payment": "UPI"}}]
- "today I spent 200 on chai, 1500 on uber, and 3000 on groceries" -> [{{"amount": 200, "description": "Chai", "date": null, "category": "Food", "payment": "UPI"}},{{"amount": 1500, "description": "Uber", "date": null, "category": "Transport", "payment": "UPI"}},{{"amount": 3000, "description": "Groceries", "date": null, "category": "Food", "payment": "UPI"}}]"""


async def parse_with_groq(text: str) -> list[dict] | None:
    """Send text to Groq LLM for intelligent parsing. Returns list of entries."""
    if not GROQ_API_KEY:
        return None

    now = datetime.now(TIMEZONE)
    yesterday = (now - timedelta(days=1)).strftime("%d/%m/%Y")
    today = now.strftime("%d/%m/%Y")

    prompt = SYSTEM_PROMPT.replace("2026", str(now.year)).format(today=today, yesterday=yesterday)

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                GROQ_URL,
                headers={
                    "Authorization": f"Bearer {GROQ_API_KEY}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": GROQ_MODEL,
                    "messages": [
                        {"role": "system", "content": prompt},
                        {"role": "user", "content": text},
                    ],
                    "max_tokens": 4096,
                    "temperature": 0,
                },
            )
            resp.raise_for_status()
            data = resp.json()
            if data["choices"][0].get("finish_reason") == "length":
                raise ValueError("Truncated parser response")
            content = data["choices"][0]["message"]["content"].strip()

            # Clean markdown fences if present
            content = re.sub(r"^```json\s*", "", content)
            content = re.sub(r"\s*```$", "", content)

            parsed = json.loads(content)

            # Handle both single object and array responses
            if isinstance(parsed, dict):
                parsed = [parsed]

            if not isinstance(parsed, list) or len(parsed) == 0:
                return None

            # Validate each entry
            valid_entries = []
            for entry in parsed:
                if not isinstance(entry, dict):
                    raise ValueError("Invalid expense object")
                amount = entry.get("amount")
                if isinstance(amount, bool) or not isinstance(amount, (int, float)) or not math.isfinite(amount) or amount <= 0:
                    raise ValueError("Invalid amount")
                if not isinstance(entry.get("description"), str) or not entry["description"].strip():
                    raise ValueError("Missing description")
                entry["description"] = to_title_case(entry["description"].strip())

                # Validate category
                if entry.get("category") and entry["category"] not in CATEGORY_LIST:
                    entry["category"] = None

                # Validate payment
                if entry.get("payment") not in PAYMENT_METHODS:
                    entry["payment"] = "UPI"

                # Parse date string to datetime
                if entry.get("date"):
                    try:
                        entry["date"] = datetime.strptime(entry["date"], "%d/%m/%Y").replace(tzinfo=TIMEZONE)
                    except (ValueError, TypeError):
                        raise ValueError("Invalid explicit date")

                valid_entries.append(entry)

            return valid_entries if valid_entries else None

    except Exception as e:
        log.warning("Groq parse failed (%s); using fallback", type(e).__name__)
        return None


# ─── Fallback Regex Parser ───────────────────────────────
# (Kept as backup if Groq is down or API key not set)

WORD_NUMS = {
    "zero":0,"one":1,"two":2,"three":3,"four":4,"five":5,"six":6,"seven":7,
    "eight":8,"nine":9,"ten":10,"eleven":11,"twelve":12,"thirteen":13,
    "fourteen":14,"fifteen":15,"sixteen":16,"seventeen":17,"eighteen":18,
    "nineteen":19,"twenty":20,"thirty":30,"forty":40,"fifty":50,"sixty":60,
    "seventy":70,"eighty":80,"ninety":90,
}
MULTIPLIERS = {
    "hundred":100,"thousand":1000,"lakh":100000,"lakhs":100000,
    "crore":10000000,"crores":10000000,"million":1000000,"billion":1000000000,
}
MONTH_MAP = {
    "jan":1,"january":1,"feb":2,"february":2,"mar":3,"march":3,
    "apr":4,"april":4,"apirl":4,"may":5,"jun":6,"june":6,
    "jul":7,"july":7,"aug":8,"august":8,"sep":9,"september":9,
    "oct":10,"october":10,"nov":11,"november":11,"dec":12,"december":12,
}
CATEGORY_KEYWORDS = {
    "Food": ["chai","tea","coffee","lunch","dinner","breakfast","biryani","rice","chicken","pizza","burger","snack","sweets","zomato","swiggy","restaurant","dhaba","thali","momos","dosa","roti","dal","paneer","egg","milk","bread","grocery","groceries","fruit","juice","water","coke","pepsi","ice cream","cake","noodles","maggi","food","peanut butter","wefit"],
    "Transport": ["uber","ola","auto","rickshaw","cab","taxi","fuel","petrol","diesel","metro","bus","train","flight","parking","toll","rapido"],
    "Shopping": ["amazon","flipkart","myntra","ajio","clothing","shoes","electronics","phone","headphones","mouse","keyboard","monitor","shirt","jeans","watch","meesho","nothing phone"],
    "Subscriptions": ["netflix","spotify","youtube","premium","hotstar","adobe","figma","notion","chatgpt","claude","anthropic","gym membership","vpn","icloud","google one","canva","cursor","subscription","x.com","kling"],
    "Business": ["client","freelance","outsource","contractor","equipment","mic","camera","light","render","hosting","domain","catalystx","fiverr","upwork","document print","printing","induction"],
    "Health": ["gym","supplement","protein","vitamin","medicine","doctor","hospital","pharmacy","medical","dental","eye","test","lab","scan","whey"],
    "Financial Investment": ["sip","etf","niftybees","juniorbees","goldbees","silverbees","mutual fund","groww","zerodha","stock","share","bond","fd","fixed deposit","ppf","nps","parag parikh","quant small cap","sbi small cap","investment"],
    "Business Investment": ["course","book","udemy","skillshare","masterclass","workshop","seminar","conference","coaching","mentorship","learning","standing desk","english gpt","cable"],
    "Bills & Utilities": ["electricity","electric","phone bill","recharge","wifi","internet","broadband","jio","airtel","vi","water bill","gas","rent","emi","insurance"],
}

def words_to_number(text: str):
    tokens = re.sub(r"[,\-]", " ", text.lower()).replace(" and ", " ").split()
    total, current, found = 0, 0, False
    for t in tokens:
        if t in WORD_NUMS:
            current += WORD_NUMS[t]
            found = True
        elif t in MULTIPLIERS:
            if current == 0: current = 1
            if MULTIPLIERS[t] >= 1000:
                total += current * MULTIPLIERS[t]
                current = 0
            else:
                current *= MULTIPLIERS[t]
            found = True
    total += current
    return total if found and total > 0 else None

def _parse_single_amount(s: str):
    s = s.strip()
    m = re.match(r"^([\d,]+\.?\d*)\s*(k|l|lakh|lakhs|cr|crore|crores)?$", s, re.IGNORECASE)
    if m:
        num = float(m.group(1).replace(",", ""))
        suf = (m.group(2) or "").lower()
        if suf == "k": return num * 1000
        if suf in ("l", "lakh", "lakhs"): return num * 100000
        if suf in ("cr", "crore", "crores"): return num * 10000000
        return num
    try:
        val = float(s.replace(",", ""))
        if val > 0: return val
    except ValueError:
        pass
    return words_to_number(s)

def parse_amount_str(raw: str):
    s = raw.strip()
    if "+" in s:
        parts = s.split("+")
        total = 0
        for p in parts:
            p = p.strip()
            if not p: continue
            val = _parse_single_amount(p)
            if val is None: return None
            total += val
        return total if total > 0 else None
    return _parse_single_amount(s)

def guess_category_keywords(text: str) -> str | None:
    lower = text.lower()
    matches = [(len(kw), cat) for cat, keywords in CATEGORY_KEYWORDS.items()
               for kw in keywords if re.search(r"(?<!\w)" + re.escape(kw) + r"(?!\w)", lower)]
    if matches:
        return max(matches, key=lambda item: item[0])[1]
    return None

def to_title_case(text: str) -> str:
    acronyms = {"sip","etf","upi","emi","nps","ppf","fd","vpn","neft","imps","rtgs","x.com","hdfc","cbi","gpt"}
    words = text.split()
    return " ".join(w.upper() if w.lower() in acronyms else w.capitalize() for w in words)

def parse_date_part(text: str):
    s = text.strip().lower().rstrip(".")
    now = datetime.now(TIMEZONE)
    if s == "yesterday": return now - timedelta(days=1)
    if s == "today": return now

    patterns = [
        (r"^(?:on\s+)?(\d{1,2})(?:st|nd|rd|th)?\s+of\s+(\w+)$", lambda m: (int(m.group(1)), m.group(2), now.year)),
        (r"^(?:on\s+)?(\d{1,2})(?:st|nd|rd|th)?\s+(\w+)\s+(\d{4})$", lambda m: (int(m.group(1)), m.group(2), int(m.group(3)))),
        (r"^(?:on\s+)?(\d{1,2})(?:st|nd|rd|th)?\s+(\w+)$", lambda m: (int(m.group(1)), m.group(2), now.year)),
        (r"^(?:on\s+)?(\w+)\s+(\d{1,2})(?:st|nd|rd|th)?$", lambda m: (int(m.group(2)), m.group(1), now.year)),
    ]
    for pat, extractor in patterns:
        m = re.match(pat, s)
        if m:
            day, month_str, year = extractor(m)
            if month_str.lower() in MONTH_MAP and 1 <= day <= 31:
                try: return datetime(year, MONTH_MAP[month_str.lower()], day, tzinfo=TIMEZONE)
                except ValueError: pass

    m = re.match(r"^(\d{1,2})[/\-](\d{1,2})[/\-](\d{4})$", s)
    if m:
        try: return datetime(int(m.group(3)), int(m.group(2)), int(m.group(1)), tzinfo=TIMEZONE)
        except ValueError: pass

    m = re.match(r"^(\d{1,2})[/\-](\d{1,2})$", s)
    if m:
        day, month = int(m.group(1)), int(m.group(2))
        if 1 <= month <= 12 and 1 <= day <= 31:
            try: return datetime(now.year, month, day, tzinfo=TIMEZONE)
            except ValueError: pass

    m = re.match(r"^(?:on\s+)?(\d{1,2})(?:st|nd|rd|th)$", s)
    if m:
        day = int(m.group(1))
        if 1 <= day <= 31:
            try: return datetime(now.year, now.month, day, tzinfo=TIMEZONE)
            except ValueError: pass

    return None

def smart_split(text: str) -> list[str]:
    protected = re.sub(r"(\d),(\d)", r"\1§\2", text)
    parts = [p.strip() for p in protected.split(",") if p.strip()]
    return [p.replace("§", ",") for p in parts]

def fallback_parse(raw: str) -> dict | None:
    """Regex-based fallback parser."""
    text = raw.strip()
    if not text: return None

    # Remove a trailing date before parsing the amount/description.
    tokens = text.split()
    for start in range(1, len(tokens)):
        suffix = " ".join(tokens[start:]).strip(" ,")
        date = parse_date_part(suffix)
        if date:
            prefix = " ".join(tokens[:start]).rstrip(" ,")
            result = fallback_parse(prefix)
            if result:
                result["date"] = date
                return result

    payment = "UPI"
    via_match = re.search(r"\b(?:via|using|through|by)\s+(.+)$", text, re.IGNORECASE)
    if via_match:
        pay_text = via_match.group(1).lower()
        if any(k in pay_text for k in ["cash","notes"]): payment = "CASH"
        elif any(k in pay_text for k in ["card 1","card1","hdfc"]): payment = "CARD1"
        elif any(k in pay_text for k in ["card 2","card2","cbi"]): payment = "CARD2"
        elif any(k in pay_text for k in ["neft","imps","rtgs","wire","bank"]): payment = "BANK"
        text = text[:via_match.start()].strip()

    has_separator = bool(re.search(r",(?!\d)", text))

    if has_separator:
        parts = smart_split(text)
        amount, description, date, category = None, None, None, None

        for part in parts:
            if amount is None:
                amt = parse_amount_str(part)
                if amt:
                    amount = amt
                    continue
            if date is None:
                d = parse_date_part(part)
                if d:
                    date = d
                    continue
            # Check if it's a category name
            if category is None:
                for cat in CATEGORY_LIST:
                    if part.strip().lower() == cat.lower() or part.strip().lower().replace(" ", "") == cat.lower().replace(" ", "").replace("&", ""):
                        category = cat
                        break
                if category:
                    continue
            if description is None:
                description = part
            else:
                description = description + " " + part

        if amount is None: return None
        desc = to_title_case(re.sub(r"^on\s+", "", (description or "Unnamed").strip(), flags=re.IGNORECASE).strip(".,;: "))
        if not category:
            category = guess_category_keywords(desc)
        return {"amount": amount, "description": desc, "category": category, "payment": payment, "date": date}
    else:
        # Legacy non-comma mode
        m = re.match(r"^([\d,]+\.?\d*\s*(?:k|l|lakh|lakhs|cr|crore|crores)?)\s+(.+)", text, re.IGNORECASE)
        if m:
            amt = parse_amount_str(m.group(1))
            if amt:
                desc_raw = re.sub(r"^on\s+", "", m.group(2).strip(), flags=re.IGNORECASE).strip(".,;: ")
                desc = to_title_case(desc_raw)
                cat = guess_category_keywords(desc)
                return {"amount": amt, "description": desc, "category": cat, "payment": payment, "date": None}

        m = re.match(r"(.+?)\s+([\d,]+\.?\d*\s*(?:k|l|lakh|lakhs|cr|crore|crores)?)$", text, re.IGNORECASE)
        if m:
            amt = parse_amount_str(m.group(2))
            if amt:
                desc_raw = re.sub(r"^on\s+", "", m.group(1).strip(), flags=re.IGNORECASE).strip(".,;: ")
                desc = to_title_case(desc_raw)
                cat = guess_category_keywords(desc)
                return {"amount": amt, "description": desc, "category": cat, "payment": payment, "date": None}

        word_num_pattern = r"((?:(?:zero|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|twenty|thirty|forty|fifty|sixty|seventy|eighty|ninety|hundred|thousand|lakh|lakhs|crore|crores|million|billion|and)\s*)+)"
        m = re.match(rf"^{word_num_pattern}\s+(.+)", text, re.IGNORECASE)
        if m:
            amt = words_to_number(m.group(1))
            if amt and amt > 0:
                desc = to_title_case(m.group(2).strip())
                cat = guess_category_keywords(desc)
                return {"amount": amt, "description": desc, "category": cat, "payment": payment, "date": None}

    return None


# ─── Helpers ─────────────────────────────────────────────
def format_inr(n: float) -> str:
    if n >= 10000000:
        return f"\u20B9{n/10000000:.1f}Cr".replace(".0Cr", "Cr")
    if n >= 100000:
        return f"\u20B9{n/100000:.1f}L".replace(".0L", "L")
    return f"\u20B9{n:,.0f}"

def is_authorized(user_id: int) -> bool:
    if not ALLOWED_USER_IDS:
        return False
    try:
        allowed = [int(x.strip()) for x in ALLOWED_USER_IDS.split(",") if x.strip()]
    except ValueError:
        return False
    return user_id in allowed


# ─── Category Picker (Inline Keyboard) ───────────────────
def build_category_keyboard(token):
    """Build inline keyboard with category buttons (2 per row)."""
    buttons = []
    for i in range(0, len(CATEGORY_LIST), 2):
        row = [InlineKeyboardButton(CATEGORY_LIST[i], callback_data=f"cat:{token}:{i}")]
        if i + 1 < len(CATEGORY_LIST):
            row.append(InlineKeyboardButton(CATEGORY_LIST[i+1], callback_data=f"cat:{token}:{i+1}"))
        buttons.append(row)
    return InlineKeyboardMarkup(buttons)


async def handle_category_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle category selection from inline keyboard."""
    query = update.callback_query
    if not is_authorized(update.effective_user.id):
        await query.answer("Not authorized.", show_alert=True)
        return
    await query.answer()

    if not query.data.startswith("cat:"):
        return

    pending = context.user_data.get("pending_entry")
    parts = query.data.split(':')
    if not pending or len(parts) != 3 or parts[1] != pending.get('_picker_token'):
        await query.edit_message_text("This category picker has expired. Use the latest picker, or /cancel and resend.")
        return
    try:
        index = int(parts[2])
        if index < 0:
            return
        category = CATEGORY_LIST[index]
    except (ValueError, IndexError):
        return

    pending["category"] = category

    # Log to sheet
    now = datetime.now(TIMEZONE)
    entry_date = pending.get("date") or now
    if isinstance(entry_date, str):
        try:
            entry_date = datetime.strptime(entry_date, "%d/%m/%Y").replace(tzinfo=TIMEZONE)
        except ValueError:
            entry_date = now

    row = [
        entry_date.strftime("%d/%m/%Y"),
        now.strftime("%H:%M"),
        pending["amount"],
        pending["description"],
        pending["category"],
        pending.get("payment", "UPI"),
        pending.get("raw", ""),
    ]

    try:
        sh = get_spreadsheet()
        ws = get_month_sheet(sh, entry_date)
        append_to_data_area(ws, row)
    except Exception as e:
        log.error("Sheet write error (%s)", type(e).__name__)
        await query.edit_message_text("Failed to write to sheet. Try again.")
        return

    context.user_data.pop("pending_entry", None)

    date_str = f" · {entry_date.strftime('%d %b')}" if pending.get("date") else ""

    await query.edit_message_text(
        f"✓ *{format_inr(pending['amount'])}* — {escape_markdown(pending['description'])}\n"
        f"  {pending['category']} · {pending.get('payment', 'UPI')}{date_str}",
        parse_mode="Markdown",
    )

    # Check if there are more entries in the queue
    queue = context.user_data.get("pending_queue", [])
    if queue:
        next_entry = queue.pop(0)
        next_entry['_picker_token'] = secrets.token_hex(4)
        next_entry["raw"] = pending.get("raw", "")
        context.user_data["pending_entry"] = next_entry
        context.user_data["pending_queue"] = queue

        entry_date = next_entry.get("date") or now
        if isinstance(entry_date, str):
            try:
                entry_date = datetime.strptime(entry_date, "%d/%m/%Y").replace(tzinfo=TIMEZONE)
                next_entry["date"] = entry_date
            except ValueError:
                entry_date = now

        date_str = f" · {entry_date.strftime('%d %b')}" if next_entry.get("date") else ""

        await query.message.reply_text(
            f"*{format_inr(next_entry['amount'])}* — {escape_markdown(next_entry['description'])}{date_str}\n\n"
            "Pick a category:",
            parse_mode="Markdown",
            reply_markup=build_category_keyboard(next_entry['_picker_token']),
        )


# ─── Bot Handlers ────────────────────────────────────────
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update.effective_user.id):
        await update.message.reply_text("Not authorized.")
        return
    await update.message.reply_text(
        "◉ *Trakos*\n\n"
        "Send me your expenses in any format.\n\n"
        "*Examples:*\n"
        "`450, chai`\n"
        "`On 7th April I spent 43 on printing`\n"
        "`fifteen thousand rent yesterday`\n"
        "`272 + 272, wefit`\n"
        "`880, Kling, 25th April, subscriptions`\n"
        "`2.5K uber via cash`\n\n"
        "I'll figure out the amount, description, date, and category.\n"
        "If I can't determine the category, I'll ask you.\n\n"
        "*Commands:*\n"
        "/today — today's expenses\n"
        "/week — this week's summary\n"
        "/month — this month's summary\n"
        "/sheet — link to your sheet\n"
        "/sort — sort all sheets by date\n"
        "/categories — list all categories\n"
        "/help — show this message",
        parse_mode="Markdown",
    )

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await cmd_start(update, context)

async def cmd_categories(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update.effective_user.id):
        return
    cats = "\n".join(f"• *{cat}*" for cat in CATEGORY_LIST)
    await update.message.reply_text(f"*Categories:*\n{cats}", parse_mode="Markdown")

async def cmd_sheet(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update.effective_user.id):
        return
    await update.message.reply_text(
        f"[Open your sheet](https://docs.google.com/spreadsheets/d/{SHEET_ID})",
        parse_mode="Markdown",
    )

async def cmd_today(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update.effective_user.id):
        return
    await _send_summary(update, days=0, label="Today")

async def cmd_week(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update.effective_user.id):
        return
    await _send_summary(update, days=7, label="This Week")

async def cmd_month(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update.effective_user.id):
        return
    await _send_summary(update, days=-1, label="This Month")


async def cmd_sort(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Sort all monthly sheets by date ascending."""
    if not is_authorized(update.effective_user.id):
        return

    try:
        sh = get_spreadsheet()
        sorted_count = 0

        for ws in sh.worksheets():
            try:
                col_a = ws.col_values(1)
                data_rows = len(col_a)
                if data_rows > 2 and col_a[0] == "Date":
                    sort_month_sheet(ws)
                    sorted_count += 1
            except Exception:
                continue

        await update.message.reply_text(
            f"✓ Sorted {sorted_count} sheet(s) by date.",
            parse_mode="Markdown",
        )
    except Exception as e:
        log.error(f"Sort error: {e}")
        await update.message.reply_text("Sort failed. Check connection.")


async def _send_summary(update: Update, days: int, label: str):
    try:
        sh = get_spreadsheet()
        now = datetime.now(TIMEZONE)
        today = now.replace(hour=0, minute=0, second=0, microsecond=0)
        cutoff = today.replace(day=1) if days == -1 else today - timedelta(days=max(0, days - 1))
        end = today + timedelta(days=1)

        total = 0.0
        cat_totals = {}
        count = 0

        for ws in sh.worksheets():
            try:
                datetime.strptime(ws.title, "%B %Y")
                rows = ws.get_all_values()[1:]
            except ValueError:
                continue
            for row in rows:
                if len(row) < 3:
                    continue
                try:
                    row_date = datetime.strptime(row[0], "%d/%m/%Y").replace(tzinfo=TIMEZONE)
                    if cutoff <= row_date < end:
                        amt = float(str(row[2]).replace(",", ""))
                        total += amt
                        count += 1
                        cat = row[4] if len(row) > 4 else "Other"
                        cat_totals[cat] = cat_totals.get(cat, 0) + amt
                except (ValueError, IndexError):
                    continue

        if count == 0:
            await update.message.reply_text(f"*{label}:* No expenses logged.", parse_mode="Markdown")
            return

        lines = [f"*{label}*\n", f"Total: *{format_inr(total)}* ({count} entries)\n"]
        sorted_cats = sorted(cat_totals.items(), key=lambda x: -x[1])
        for cat, amt in sorted_cats:
            pct = (amt / total) * 100
            lines.append(f"  {cat}: {format_inr(amt)} ({pct:.0f}%)")

        await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

    except Exception as e:
        log.error(f"Summary error: {e}")
        await update.message.reply_text("Could not read sheet. Check connection.")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update.effective_user.id):
        await update.message.reply_text("Not authorized.")
        return

    raw = update.message.text.strip()
    if not raw or raw.startswith("/"):
        return
    if context.user_data.get("pending_entry"):
        await update.message.reply_text("Choose the category for your pending expense first, or /cancel it. Then resend this message.")
        return

    # Manual category override via #tag
    manual_cat = None
    cat_match = re.search(r"#(\w+)", raw)
    if cat_match:
        tag = cat_match.group(1).lower()
        for cat_name in CATEGORY_LIST:
            if tag == cat_name.lower().replace(" ", "").replace("&", ""):
                manual_cat = cat_name
                break
        if not manual_cat and tag == "other":
            manual_cat = "Other"
        raw = (raw[:cat_match.start()] + raw[cat_match.end():]).strip().strip(",").strip()

    # Try Groq LLM first (returns list)
    entries = await parse_with_groq(raw)

    # Fallback to regex if Groq failed
    if not entries:
        await update.message.reply_text("AI parsing is unavailable for this message. Trying the basic parser; check the amount and date in the confirmation.")
        lines = [l.strip() for l in raw.split("\n") if l.strip()]
        if any(re.search(r"\band\s+(?:₹|rs\.?\s*)?\d", line, re.I) for line in lines):
            await update.message.reply_text("Please resend each expense on its own line: amount, description, date.")
            return
        fallback_results = [fallback_parse(l) for l in lines]
        entries = fallback_results if fallback_results and all(fallback_results) else None

    if not entries:
        await update.message.reply_text(
            "Couldn't parse that. Try:\n"
            "`450, chai`\n"
            "`On 7th April I spent 43 on printing`\n"
            "`fifteen thousand, rent`",
            parse_mode="Markdown",
        )
        return

    # Apply manual category override to all entries
    if manual_cat:
        for e in entries:
            e["category"] = manual_cat

    # Separate entries with and without categories
    ready = [e for e in entries if e.get("category")]
    needs_category = [e for e in entries if not e.get("category")]

    # Log all ready entries
    now = datetime.now(TIMEZONE)
    for entry in entries:
        entry["date"] = entry.get("date") or now
    logged_lines = []
    raw_text = update.message.text.strip()

    for entry in ready:
        entry_date = entry.get("date") or now
        if isinstance(entry_date, str):
            try:
                entry_date = datetime.strptime(entry_date, "%d/%m/%Y").replace(tzinfo=TIMEZONE)
            except ValueError:
                entry_date = now

        row = [
            entry_date.strftime("%d/%m/%Y"),
            now.strftime("%H:%M"),
            entry["amount"],
            entry["description"],
            entry["category"],
            entry.get("payment", "UPI"),
            raw_text,
        ]

        try:
            sh = get_spreadsheet()
            ws = get_month_sheet(sh, entry_date)
            append_to_data_area(ws, row)

            date_str = f" · {entry_date.strftime('%d %b')}" if entry.get("date") else ""
            logged_lines.append(
                f"✓ *{format_inr(entry['amount'])}* — {escape_markdown(entry['description'])}\n"
                f"  {entry['category']} · {entry.get('payment', 'UPI')}{date_str}"
            )
        except Exception as e:
            log.error("Sheet write error (%s)", type(e).__name__)
            logged_lines.append(f"✗ {format_inr(entry['amount'])} — {escape_markdown(entry['description'])} (write failed)")

    # Send confirmation for logged entries
    if logged_lines:
        await update.message.reply_text("\n\n".join(logged_lines), parse_mode="Markdown")

    # Handle entries that need category selection (one at a time)
    if needs_category:
        for pending in needs_category:
            pending["raw"] = raw_text
        # Store remaining ones in queue
        entry = needs_category[0]
        entry['_picker_token'] = secrets.token_hex(4)
        entry["raw"] = raw_text
        context.user_data["pending_entry"] = entry
        context.user_data["pending_queue"] = needs_category[1:] if len(needs_category) > 1 else []

        entry_date = entry.get("date") or now
        if isinstance(entry_date, str):
            try:
                entry_date = datetime.strptime(entry_date, "%d/%m/%Y").replace(tzinfo=TIMEZONE)
                entry["date"] = entry_date
            except ValueError:
                entry_date = now

        date_str = f" · {entry_date.strftime('%d %b')}" if entry.get("date") else ""

        await update.message.reply_text(
            f"*{format_inr(entry['amount'])}* — {escape_markdown(entry['description'])}{date_str}\n\n"
            "Pick a category:",
            parse_mode="Markdown",
            reply_markup=build_category_keyboard(entry['_picker_token']),
        )


# ─── Main ────────────────────────────────────────────────
async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update.effective_user.id):
        return
    context.user_data.pop("pending_entry", None)
    context.user_data.pop("pending_queue", None)
    await update.message.reply_text("Pending manual entries cancelled. Already saved expenses are unchanged.")


async def handle_error(update, context):
    log.error("Telegram handler failed (%s)", type(context.error).__name__)


def main():
    if not TELEGRAM_TOKEN:
        raise ValueError("TELEGRAM_TOKEN not set")
    if not GOOGLE_CREDS_JSON:
        raise ValueError("GOOGLE_CREDS_JSON not set")
    if not SHEET_ID:
        raise ValueError("SHEET_ID not set")
    if not GROQ_API_KEY:
        log.warning("GROQ_API_KEY not set — using regex fallback only")
    if not ALLOWED_USER_IDS or not all(x.strip().isdigit() for x in ALLOWED_USER_IDS.split(",")):
        raise ValueError("ALLOWED_USER_IDS must contain your Telegram user ID")

    from sms_workflow import configured
    sms = configured(sys.modules[__name__])
    builder = Application.builder().token(TELEGRAM_TOKEN)
    if sms:
        builder = builder.post_init(sms.start).post_stop(sms.stop)
        builder = builder.persistence(PicklePersistence(filepath=str(Path(sms.db.path).with_suffix('.pending.pickle'))))
    app = builder.build()
    if sms:
        sms.register(app)

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("categories", cmd_categories))
    app.add_handler(CommandHandler("sheet", cmd_sheet))
    app.add_handler(CommandHandler("today", cmd_today))
    app.add_handler(CommandHandler("week", cmd_week))
    app.add_handler(CommandHandler("month", cmd_month))
    app.add_handler(CommandHandler("sort", cmd_sort))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(CallbackQueryHandler(handle_category_callback, pattern=r'^cat:'))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    app.add_error_handler(handle_error)
    log.info("Trakos v6 is running. SMS import: %s", bool(sms))
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
