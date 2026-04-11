"""
Trakos — Telegram expense tracker that logs to Google Sheets.
v3: Comma-separated input, fixed sheet formulas, cleaner date parsing.
"""

import os
import re
import json
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import gspread
from google.oauth2.service_account import Credentials
from telegram import Update, BotCommand
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
)

# ─── Config ───────────────────────────────────────────────
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
GOOGLE_CREDS_JSON = os.environ.get("GOOGLE_CREDS_JSON", "")
SHEET_ID = os.environ.get("SHEET_ID", "")
ALLOWED_USER_IDS = os.environ.get("ALLOWED_USER_IDS", "")
TIMEZONE = ZoneInfo("Asia/Kolkata")

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("trakos")

# ─── Google Sheets ────────────────────────────────────────
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

HEADER_ROW = ["Date", "Time", "Amount", "Description", "Category", "Payment Method", "Raw Input"]

def get_spreadsheet():
    creds_dict = json.loads(GOOGLE_CREDS_JSON)
    creds = Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
    gc = gspread.authorize(creds)
    return gc.open_by_key(SHEET_ID)


def get_month_sheet(sh, target_date: datetime):
    month_name = target_date.strftime("%B %Y")
    try:
        return sh.worksheet(month_name)
    except gspread.exceptions.WorksheetNotFound:
        pass

    ws = sh.add_worksheet(title=month_name, rows=200, cols=9)

    # Header
    ws.update("A1:G1", [HEADER_ROW])
    ws.format("A1:G1", {
        "textFormat": {"bold": True, "fontSize": 10},
        "backgroundColor": {"red": 0.15, "green": 0.15, "blue": 0.15},
        "horizontalAlignment": "CENTER",
    })

    # Summary — use update with raw=False so formulas execute
    ws.update("H1", [["SUMMARY"]], raw=False)
    ws.update("H2", [[month_name]], raw=False)
    ws.update("H3", [["Total Spent:"]], raw=False)
    ws.update("I3", [['=SUM(C2:C)']], raw=False)
    ws.update("H4", [["Entries:"]], raw=False)
    ws.update("I4", [['=COUNTA(A2:A)']], raw=False)

    # Category breakdown
    categories = list(CATEGORIES.keys()) + ["Other"]
    ws.update("H6", [["BY CATEGORY"]], raw=False)
    for i, cat in enumerate(categories):
        r = 7 + i
        ws.update(f"H{r}", [[cat]], raw=False)
        ws.update(f"I{r}", [[f'=SUMPRODUCT((E$2:E=H{r})*C$2:C)']], raw=False)

    # Formatting
    ws.format("H1:H2", {"textFormat": {"bold": True}})
    ws.format("H3:H4", {"textFormat": {"bold": True}})
    ws.format("H6", {"textFormat": {"bold": True}})
    ws.format("I3", {"numberFormat": {"type": "NUMBER", "pattern": "#,##0"}})

    return ws


# ─── Date Parser ─────────────────────────────────────────
MONTH_MAP = {
    "jan":1,"january":1,"feb":2,"february":2,"mar":3,"march":3,
    "apr":4,"april":4,"may":5,"jun":6,"june":6,
    "jul":7,"july":7,"aug":8,"august":8,"sep":9,"september":9,
    "oct":10,"october":10,"nov":11,"november":11,"dec":12,"december":12,
}

def parse_date_part(text: str):
    """
    Parse a date from a standalone segment (after comma splitting).
    Returns datetime or None.
    """
    s = text.strip().lower()
    now = datetime.now(TIMEZONE)

    if s == "yesterday":
        return now - timedelta(days=1)
    if s == "today":
        return now

    # "18th of April" / "18 of april"
    m = re.match(r"^(?:on\s+)?(\d{1,2})(?:st|nd|rd|th)?\s+of\s+(\w+)$", s)
    if m:
        day, month_str = int(m.group(1)), m.group(2)
        if month_str in MONTH_MAP and 1 <= day <= 31:
            try:
                return datetime(now.year, MONTH_MAP[month_str], day, tzinfo=TIMEZONE)
            except ValueError:
                pass

    # "18th April 2026" / "18 April 2026"
    m = re.match(r"^(?:on\s+)?(\d{1,2})(?:st|nd|rd|th)?\s+(\w+)\s+(\d{4})$", s)
    if m:
        day, month_str, year = int(m.group(1)), m.group(2), int(m.group(3))
        if month_str in MONTH_MAP and 1 <= day <= 31:
            try:
                return datetime(year, MONTH_MAP[month_str], day, tzinfo=TIMEZONE)
            except ValueError:
                pass

    # "18th April" / "18 april" / "18th apr"
    m = re.match(r"^(?:on\s+)?(\d{1,2})(?:st|nd|rd|th)?\s+(\w+)$", s)
    if m:
        day, month_str = int(m.group(1)), m.group(2)
        if month_str in MONTH_MAP and 1 <= day <= 31:
            try:
                return datetime(now.year, MONTH_MAP[month_str], day, tzinfo=TIMEZONE)
            except ValueError:
                pass

    # "April 18th" / "april 18"
    m = re.match(r"^(?:on\s+)?(\w+)\s+(\d{1,2})(?:st|nd|rd|th)?$", s)
    if m:
        month_str, day = m.group(1), int(m.group(2))
        if month_str in MONTH_MAP and 1 <= day <= 31:
            try:
                return datetime(now.year, MONTH_MAP[month_str], day, tzinfo=TIMEZONE)
            except ValueError:
                pass

    # "18/04/2026" or "18-04-2026"
    m = re.match(r"^(\d{1,2})[/\-](\d{1,2})[/\-](\d{4})$", s)
    if m:
        day, month, year = int(m.group(1)), int(m.group(2)), int(m.group(3))
        try:
            return datetime(year, month, day, tzinfo=TIMEZONE)
        except ValueError:
            pass

    # "18/04" or "18-04"
    m = re.match(r"^(\d{1,2})[/\-](\d{1,2})$", s)
    if m:
        day, month = int(m.group(1)), int(m.group(2))
        if 1 <= month <= 12 and 1 <= day <= 31:
            try:
                return datetime(now.year, month, day, tzinfo=TIMEZONE)
            except ValueError:
                pass

    # Just "18th" or "18" with ordinal — only if it looks like a day
    m = re.match(r"^(?:on\s+)?(\d{1,2})(?:st|nd|rd|th)$", s)
    if m:
        day = int(m.group(1))
        if 1 <= day <= 31:
            try:
                return datetime(now.year, now.month, day, tzinfo=TIMEZONE)
            except ValueError:
                pass

    return None


# ─── Word-to-Number Parser ───────────────────────────────
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

def words_to_number(text: str):
    tokens = re.sub(r"[,\-]", " ", text.lower()).replace(" and ", " ").split()
    total, current, found = 0, 0, False
    for t in tokens:
        if t in WORD_NUMS:
            current += WORD_NUMS[t]
            found = True
        elif t in MULTIPLIERS:
            if current == 0:
                current = 1
            if MULTIPLIERS[t] >= 1000:
                total += current * MULTIPLIERS[t]
                current = 0
            else:
                current *= MULTIPLIERS[t]
            found = True
    total += current
    return total if found and total > 0 else None


def parse_amount_str(raw: str):
    """Parse amount from a string. Returns float or None."""
    s = raw.strip()
    # Numeric with suffix
    m = re.match(r"^([\d,]+\.?\d*)\s*(k|l|lakh|lakhs|cr|crore|crores)?$", s, re.IGNORECASE)
    if m:
        num = float(m.group(1).replace(",", ""))
        suf = (m.group(2) or "").lower()
        if suf == "k": return num * 1000
        if suf in ("l", "lakh", "lakhs"): return num * 100000
        if suf in ("cr", "crore", "crores"): return num * 10000000
        return num
    # Plain numeric
    try:
        val = float(s.replace(",", ""))
        if val > 0: return val
    except ValueError:
        pass
    # Word-based
    return words_to_number(s)


# ─── Categories ──────────────────────────────────────────
CATEGORIES = {
    "Food": ["chai","tea","coffee","lunch","dinner","breakfast","biryani","rice","chicken","pizza","burger","snack","sweets","zomato","swiggy","restaurant","dhaba","thali","momos","dosa","roti","dal","paneer","egg","milk","bread","grocery","groceries","fruit","juice","water","coke","pepsi","ice cream","cake","noodles","maggi","food","peanut butter"],
    "Transport": ["uber","ola","auto","rickshaw","cab","taxi","fuel","petrol","diesel","metro","bus","train","flight","parking","toll","rapido"],
    "Shopping": ["amazon","flipkart","myntra","ajio","clothing","shoes","electronics","phone","headphones","mouse","keyboard","monitor","shirt","jeans","watch","meesho"],
    "Subscriptions": ["netflix","spotify","youtube","premium","hotstar","adobe","figma","notion","chatgpt","claude","anthropic","gym membership","vpn","icloud","google one","canva","cursor","subscription"],
    "Business": ["client","freelance","outsource","contractor","equipment","mic","camera","light","render","hosting","domain","catalystx","fiverr","upwork","document print","printing"],
    "Health": ["gym","supplement","protein","vitamin","medicine","doctor","hospital","pharmacy","medical","dental","eye","test","lab","scan","whey"],
    "Financial Investment": ["sip","etf","niftybees","juniorbees","goldbees","silverbees","mutual fund","groww","zerodha","stock","share","bond","fd","fixed deposit","ppf","nps","parag parikh","quant small cap","sbi small cap","investment"],
    "Business Investment": ["course","book","udemy","skillshare","masterclass","workshop","seminar","conference","coaching","mentorship","learning"],
    "Bills & Utilities": ["electricity","electric","phone bill","recharge","wifi","internet","broadband","jio","airtel","vi","water bill","gas","rent","emi","insurance"],
}

PAYMENT_KEYWORDS = {
    "upi": ["upi","gpay","phonepe","paytm","bhim"],
    "card1": ["card 1","card1","hdfc card","hdfc"],
    "card2": ["card 2","card2","cbi card","central bank"],
    "cash": ["cash","notes"],
    "bank": ["bank transfer","neft","imps","rtgs","wire"],
}

def guess_category(text: str) -> str:
    lower = text.lower()
    for cat, keywords in CATEGORIES.items():
        for kw in keywords:
            if kw in lower:
                return cat
    return "Other"

def guess_payment(text: str) -> str:
    lower = text.lower()
    for method, keywords in PAYMENT_KEYWORDS.items():
        for kw in keywords:
            if kw in lower:
                return method.upper()
    return "UPI"


def to_title_case(text: str) -> str:
    acronyms = {"sip","etf","upi","emi","nps","ppf","fd","vpn","neft","imps","rtgs","x.com","hdfc","cbi"}
    words = text.split()
    result = []
    for w in words:
        if w.lower() in acronyms:
            result.append(w.upper())
        else:
            result.append(w.capitalize())
    return " ".join(result)


# ─── Smart Comma Splitter ────────────────────────────────
def smart_split(text: str) -> list[str]:
    """
    Split by commas, but NOT commas inside numbers (e.g. 3,000).
    "411, peanut butter, 9th april" -> ["411", "peanut butter", "9th april"]
    "3,000 chai" -> ["3,000 chai"]  (comma inside number, no split)
    """
    # Replace number-internal commas with a placeholder
    protected = re.sub(r"(\d),(\d)", r"\1§\2", text)
    # Split by comma
    parts = [p.strip() for p in protected.split(",") if p.strip()]
    # Restore commas in numbers
    parts = [p.replace("§", ",") for p in parts]
    return parts


# ─── Input Parser ────────────────────────────────────────
def parse_input(raw: str):
    """
    Parse user input. Supports two modes:

    Mode 1 (comma-separated): "411, peanut butter, 9th april"
       - segments can be in any order
       - one segment = amount, one = description, one = date (optional)

    Mode 2 (no commas, legacy): "450 chai" / "fifteen thousand rent"
       - amount + description as before
    """
    text = raw.strip()
    if not text:
        return None

    # Extract payment method if "via X" is present (before splitting)
    payment = "UPI"
    via_match = re.search(r"\b(?:via|using|through|by)\s+(.+)$", text, re.IGNORECASE)
    if via_match:
        payment = guess_payment(via_match.group(1))
        text = text[:via_match.start()].strip()

    # Check if comma-separated (has commas that aren't inside numbers)
    has_separator = bool(re.search(r",(?!\d)", text))

    if has_separator:
        return _parse_comma_mode(text, payment)
    else:
        return _parse_legacy_mode(text, payment)


def _parse_comma_mode(text: str, payment: str):
    """Parse comma-separated input. Segments in any order."""
    parts = smart_split(text)

    amount = None
    description = None
    date = None

    for part in parts:
        # Try as amount
        if amount is None:
            amt = parse_amount_str(part)
            if amt:
                amount = amt
                continue

        # Try as date
        if date is None:
            d = parse_date_part(part)
            if d:
                date = d
                continue

        # Must be description
        if description is None:
            description = part

    if amount is None:
        return None

    description = to_title_case(description or "Unnamed")
    category = guess_category(description)

    return {"amount": amount, "description": description, "category": category, "payment": payment, "date": date}


def _parse_legacy_mode(text: str, payment: str):
    """Parse non-comma input (legacy mode): "450 chai", "fifteen thousand rent"."""

    # Leading number
    m = re.match(r"^([\d,]+\.?\d*\s*(?:k|l|lakh|lakhs|cr|crore|crores)?)\s+(.+)", text, re.IGNORECASE)
    if m:
        amt = parse_amount_str(m.group(1))
        if amt:
            desc = to_title_case(m.group(2).strip())
            return {"amount": amt, "description": desc, "category": guess_category(desc), "payment": payment, "date": None}

    # Trailing number
    m = re.match(r"(.+?)\s+([\d,]+\.?\d*\s*(?:k|l|lakh|lakhs|cr|crore|crores)?)$", text, re.IGNORECASE)
    if m:
        amt = parse_amount_str(m.group(2))
        if amt:
            desc = to_title_case(m.group(1).strip())
            return {"amount": amt, "description": desc, "category": guess_category(desc), "payment": payment, "date": None}

    # Word-numbers at start
    word_num_pattern = r"((?:(?:zero|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|twenty|thirty|forty|fifty|sixty|seventy|eighty|ninety|hundred|thousand|lakh|lakhs|crore|crores|million|billion|and)\s*)+)"

    m = re.match(rf"^{word_num_pattern}\s+(.+)", text, re.IGNORECASE)
    if m:
        amt = words_to_number(m.group(1))
        if amt and amt > 0:
            desc = to_title_case(m.group(2).strip())
            return {"amount": amt, "description": desc, "category": guess_category(desc), "payment": payment, "date": None}

    # Word-numbers at end
    m = re.match(rf"(.+?)\s+{word_num_pattern}$", text, re.IGNORECASE)
    if m:
        amt = words_to_number(m.group(2))
        if amt and amt > 0:
            desc = to_title_case(m.group(1).strip())
            return {"amount": amt, "description": desc, "category": guess_category(desc), "payment": payment, "date": None}

    # Fallback
    m = re.search(r"[\d,]+\.?\d*", text)
    if m:
        amt = parse_amount_str(m.group())
        if amt:
            desc = text.replace(m.group(), "").strip().strip("-–—: ")
            desc = to_title_case(desc) if desc else "Unnamed"
            return {"amount": amt, "description": desc, "category": guess_category(desc), "payment": payment, "date": None}

    return None


def format_inr(n: float) -> str:
    if n >= 10000000:
        return f"\u20B9{n/10000000:.1f}Cr".replace(".0Cr", "Cr")
    if n >= 100000:
        return f"\u20B9{n/100000:.1f}L".replace(".0L", "L")
    return f"\u20B9{n:,.0f}"


def is_authorized(user_id: int) -> bool:
    if not ALLOWED_USER_IDS:
        return True
    allowed = [int(x.strip()) for x in ALLOWED_USER_IDS.split(",") if x.strip()]
    return user_id in allowed


# ─── Bot Handlers ────────────────────────────────────────
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update.effective_user.id):
        await update.message.reply_text("Not authorized.")
        return
    await update.message.reply_text(
        "◉ *Trakos*\n\n"
        "Send me your expenses. I'll log them to your Google Sheet.\n\n"
        "*How to log:*\n"
        "`450 chai`\n"
        "`411, peanut butter, 9th april`\n"
        "`fifteen thousand, rent, yesterday`\n"
        "`2.5K, uber, via cash`\n"
        "`3,000 groceries`\n\n"
        "Use commas to separate amount, description, and date.\n"
        "Or just type amount + description without commas.\n\n"
        "*Commands:*\n"
        "/today — today's expenses\n"
        "/week — this week's summary\n"
        "/month — this month's summary\n"
        "/sheet — link to your sheet\n"
        "/categories — list all categories\n"
        "/help — show this message",
        parse_mode="Markdown",
    )

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await cmd_start(update, context)

async def cmd_categories(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update.effective_user.id):
        return
    cats = "\n".join(f"• *{cat}*" for cat in CATEGORIES.keys())
    await update.message.reply_text(
        f"*Categories:*\n{cats}\n\n• *Other* (default)\n\n"
        "Categories are auto-detected. Override with #tag:\n"
        "`450, chai, #food`",
        parse_mode="Markdown",
    )

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
    await _send_summary(update, days=30, label="This Month")

async def _send_summary(update: Update, days: int, label: str):
    try:
        sh = get_spreadsheet()
        now = datetime.now(TIMEZONE)
        cutoff = now.replace(hour=0, minute=0, second=0, microsecond=0) if days == 0 else now - timedelta(days=days)

        total = 0.0
        cat_totals = {}
        count = 0

        for ws in sh.worksheets():
            try:
                rows = ws.get_all_values()[1:]
            except Exception:
                continue
            for row in rows:
                if len(row) < 3:
                    continue
                try:
                    row_date = datetime.strptime(row[0], "%d/%m/%Y").replace(tzinfo=TIMEZONE)
                    if row_date >= cutoff:
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

    # Manual category override
    manual_cat = None
    cat_match = re.search(r"#(\w+)", raw)
    if cat_match:
        tag = cat_match.group(1).lower()
        for cat_name in CATEGORIES:
            if tag in cat_name.lower().replace(" ", "").replace("&", ""):
                manual_cat = cat_name
                break
        if not manual_cat and tag == "other":
            manual_cat = "Other"
        raw = raw[:cat_match.start()].strip().rstrip(",").strip()

    parsed = parse_input(raw)
    if not parsed:
        await update.message.reply_text(
            "Couldn't parse that. Try:\n"
            "`450, chai`\n"
            "`411, peanut butter, 9th april`\n"
            "`fifteen thousand, rent`\n"
            "`2.5K uber via cash`",
            parse_mode="Markdown",
        )
        return

    if manual_cat:
        parsed["category"] = manual_cat

    now = datetime.now(TIMEZONE)
    entry_date = parsed.get("date") or now

    row = [
        entry_date.strftime("%d/%m/%Y"),
        now.strftime("%H:%M"),
        parsed["amount"],
        parsed["description"],
        parsed["category"],
        parsed["payment"],
        update.message.text.strip(),
    ]

    try:
        sh = get_spreadsheet()
        ws = get_month_sheet(sh, entry_date)
        ws.append_row(row, value_input_option="USER_ENTERED")
    except Exception as e:
        log.error(f"Sheet write error: {e}")
        await update.message.reply_text("Failed to write to sheet. Try again.")
        return

    date_str = f" · {entry_date.strftime('%d %b')}" if parsed.get("date") else ""

    await update.message.reply_text(
        f"✓ *{format_inr(parsed['amount'])}* — {parsed['description']}\n"
        f"  {parsed['category']} · {parsed['payment']}{date_str}",
        parse_mode="Markdown",
    )


# ─── Main ────────────────────────────────────────────────
def main():
    if not TELEGRAM_TOKEN:
        raise ValueError("TELEGRAM_TOKEN not set")
    if not GOOGLE_CREDS_JSON:
        raise ValueError("GOOGLE_CREDS_JSON not set")
    if not SHEET_ID:
        raise ValueError("SHEET_ID not set")

    app = Application.builder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("categories", cmd_categories))
    app.add_handler(CommandHandler("sheet", cmd_sheet))
    app.add_handler(CommandHandler("today", cmd_today))
    app.add_handler(CommandHandler("week", cmd_week))
    app.add_handler(CommandHandler("month", cmd_month))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    log.info("Trakos v3 is running.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
