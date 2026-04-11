"""
Trakos — Telegram expense tracker that logs to Google Sheets.
v2: Custom dates, monthly sheet tabs, title case formatting.
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
    ws.update("A1:G1", [HEADER_ROW])
    ws.format("A1:G1", {
        "textFormat": {"bold": True, "fontSize": 10},
        "backgroundColor": {"red": 0.15, "green": 0.15, "blue": 0.15},
        "horizontalAlignment": "CENTER",
    })

    ws.update("H1:I2", [["SUMMARY", ""], [month_name, ""]])
    ws.update("H3:I4", [["Total Spent:", '=SUMPRODUCT(ISNUMBER(C2:C)*C2:C)'], ["Entries:", '=COUNTA(A2:A)']])

    categories = list(CATEGORIES.keys()) + ["Other"]
    row = 6
    ws.update(f"H{row}", [["BY CATEGORY"]])
    cat_data = [[cat, f'=SUMPRODUCT((E2:E=H{row+1+i})*C2:C)'] for i, cat in enumerate(categories)]
    ws.update(f"H{row+1}:I{row+len(categories)}", cat_data)

    ws.format("H1:I2", {"textFormat": {"bold": True}})
    ws.format(f"H3:H4", {"textFormat": {"bold": True}})
    ws.format(f"H{row}", {"textFormat": {"bold": True}})

    return ws


# ─── Date Parser ─────────────────────────────────────────
MONTH_MAP = {
    "jan":1,"january":1,"feb":2,"february":2,"mar":3,"march":3,
    "apr":4,"april":4,"may":5,"jun":6,"june":6,
    "jul":7,"july":7,"aug":8,"august":8,"sep":9,"september":9,
    "oct":10,"october":10,"nov":11,"november":11,"dec":12,"december":12,
}

ORDINAL_SUFFIXES = r"(?:st|nd|rd|th)"

def parse_date(text: str):
    now = datetime.now(TIMEZONE)
    remaining = text

    m = re.search(r"\byesterday\b", text, re.IGNORECASE)
    if m:
        d = now - timedelta(days=1)
        remaining = text[:m.start()].strip() + " " + text[m.end():].strip()
        return d, remaining.strip()

    m = re.search(r"\btoday\b", text, re.IGNORECASE)
    if m:
        remaining = text[:m.start()].strip() + " " + text[m.end():].strip()
        return now, remaining.strip()

    # "18th of April"
    m = re.search(rf"\b(?:on\s+)?(\d{{1,2}}){ORDINAL_SUFFIXES}?\s+of\s+(\w+)\b", text, re.IGNORECASE)
    if m:
        day = int(m.group(1))
        month_str = m.group(2).lower()
        if month_str in MONTH_MAP and 1 <= day <= 31:
            try:
                d = datetime(now.year, MONTH_MAP[month_str], day, tzinfo=TIMEZONE)
                remaining = text[:m.start()].strip() + " " + text[m.end():].strip()
                return d, remaining.strip()
            except ValueError:
                pass

    # "18 April 2026"
    m = re.search(rf"\b(?:on\s+)?(\d{{1,2}}){ORDINAL_SUFFIXES}?\s+(\w+)\s+(\d{{4}})\b", text, re.IGNORECASE)
    if m:
        day, month_str, year = int(m.group(1)), m.group(2).lower(), int(m.group(3))
        if month_str in MONTH_MAP and 1 <= day <= 31:
            try:
                d = datetime(year, MONTH_MAP[month_str], day, tzinfo=TIMEZONE)
                remaining = text[:m.start()].strip() + " " + text[m.end():].strip()
                return d, remaining.strip()
            except ValueError:
                pass

    # "18th April" / "18 April"
    m = re.search(rf"\b(?:on\s+)?(\d{{1,2}}){ORDINAL_SUFFIXES}?\s+(\w+)\b", text, re.IGNORECASE)
    if m:
        day, month_str = int(m.group(1)), m.group(2).lower()
        if month_str in MONTH_MAP and 1 <= day <= 31:
            try:
                d = datetime(now.year, MONTH_MAP[month_str], day, tzinfo=TIMEZONE)
                remaining = text[:m.start()].strip() + " " + text[m.end():].strip()
                return d, remaining.strip()
            except ValueError:
                pass

    # "April 18th"
    m = re.search(rf"\b(?:on\s+)?(\w+)\s+(\d{{1,2}}){ORDINAL_SUFFIXES}?\b", text, re.IGNORECASE)
    if m:
        month_str, day = m.group(1).lower(), int(m.group(2))
        if month_str in MONTH_MAP and 1 <= day <= 31:
            try:
                d = datetime(now.year, MONTH_MAP[month_str], day, tzinfo=TIMEZONE)
                remaining = text[:m.start()].strip() + " " + text[m.end():].strip()
                return d, remaining.strip()
            except ValueError:
                pass

    # DD/MM/YYYY
    m = re.search(r"\b(\d{1,2})[/\-](\d{1,2})[/\-](\d{4})\b", text)
    if m:
        day, month, year = int(m.group(1)), int(m.group(2)), int(m.group(3))
        try:
            d = datetime(year, month, day, tzinfo=TIMEZONE)
            remaining = text[:m.start()].strip() + " " + text[m.end():].strip()
            return d, remaining.strip()
        except ValueError:
            pass

    # DD/MM
    m = re.search(r"\b(\d{1,2})[/\-](\d{1,2})\b", text)
    if m:
        day, month = int(m.group(1)), int(m.group(2))
        if 1 <= month <= 12 and 1 <= day <= 31:
            try:
                d = datetime(now.year, month, day, tzinfo=TIMEZONE)
                remaining = text[:m.start()].strip() + " " + text[m.end():].strip()
                return d, remaining.strip()
            except ValueError:
                pass

    # "on 18th"
    m = re.search(rf"\bon\s+(\d{{1,2}}){ORDINAL_SUFFIXES}?\b", text, re.IGNORECASE)
    if m:
        day = int(m.group(1))
        if 1 <= day <= 31:
            try:
                d = datetime(now.year, now.month, day, tzinfo=TIMEZONE)
                remaining = text[:m.start()].strip() + " " + text[m.end():].strip()
                return d, remaining.strip()
            except ValueError:
                pass

    return None, text


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


def parse_amount(raw: str):
    s = raw.strip()
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


# ─── Categories ──────────────────────────────────────────
CATEGORIES = {
    "Food": ["chai","tea","coffee","lunch","dinner","breakfast","biryani","rice","chicken","pizza","burger","snack","sweets","zomato","swiggy","restaurant","dhaba","thali","momos","dosa","roti","dal","paneer","egg","milk","bread","grocery","groceries","fruit","juice","water","coke","pepsi","ice cream","cake","noodles","maggi","food"],
    "Transport": ["uber","ola","auto","rickshaw","cab","taxi","fuel","petrol","diesel","metro","bus","train","flight","parking","toll","rapido"],
    "Shopping": ["amazon","flipkart","myntra","ajio","clothing","shoes","electronics","phone","headphones","mouse","keyboard","monitor","shirt","jeans","watch","meesho"],
    "Subscriptions": ["netflix","spotify","youtube","premium","hotstar","adobe","figma","notion","chatgpt","claude","anthropic","gym membership","vpn","icloud","google one","canva","cursor","subscription"],
    "Business": ["client","freelance","outsource","contractor","equipment","mic","camera","light","render","hosting","domain","catalystx","fiverr","upwork"],
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
    acronyms = {"sip","etf","upi","emi","nps","ppf","fd","vpn","neft","imps","rtgs","x.com"}
    words = text.split()
    result = []
    for w in words:
        if w.lower() in acronyms:
            result.append(w.upper())
        else:
            result.append(w.capitalize())
    return " ".join(result)


# ─── Input Parser ────────────────────────────────────────
def parse_input(raw: str):
    text = raw.strip()
    if not text:
        return None

    payment = "UPI"
    via_match = re.search(r"\b(?:via|using|through|by)\s+(.+)$", text, re.IGNORECASE)
    if via_match:
        payment = guess_payment(via_match.group(1))
        text = text[:via_match.start()].strip()

    custom_date, text = parse_date(text)
    text = text.strip()
    if not text:
        return None

    # Leading number
    m = re.match(r"^([\d,]+\.?\d*\s*(?:k|l|lakh|lakhs|cr|crore|crores)?)\s+(.+)", text, re.IGNORECASE)
    if m:
        amt = parse_amount(m.group(1))
        if amt:
            desc = to_title_case(m.group(2).strip())
            return {"amount": amt, "description": desc, "category": guess_category(desc), "payment": payment, "date": custom_date}

    # Trailing number
    m = re.match(r"(.+?)\s+([\d,]+\.?\d*\s*(?:k|l|lakh|lakhs|cr|crore|crores)?)$", text, re.IGNORECASE)
    if m:
        amt = parse_amount(m.group(2))
        if amt:
            desc = to_title_case(m.group(1).strip())
            return {"amount": amt, "description": desc, "category": guess_category(desc), "payment": payment, "date": custom_date}

    # Word-numbers at start
    word_num_pattern = r"((?:(?:zero|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|twenty|thirty|forty|fifty|sixty|seventy|eighty|ninety|hundred|thousand|lakh|lakhs|crore|crores|million|billion|and)\s*)+)"

    m = re.match(rf"^{word_num_pattern}\s+(.+)", text, re.IGNORECASE)
    if m:
        amt = words_to_number(m.group(1))
        if amt and amt > 0:
            desc = to_title_case(m.group(2).strip())
            return {"amount": amt, "description": desc, "category": guess_category(desc), "payment": payment, "date": custom_date}

    # Word-numbers at end
    m = re.match(rf"(.+?)\s+{word_num_pattern}$", text, re.IGNORECASE)
    if m:
        amt = words_to_number(m.group(2))
        if amt and amt > 0:
            desc = to_title_case(m.group(1).strip())
            return {"amount": amt, "description": desc, "category": guess_category(desc), "payment": payment, "date": custom_date}

    # Fallback
    m = re.search(r"[\d,]+\.?\d*", text)
    if m:
        amt = parse_amount(m.group())
        if amt:
            desc = text.replace(m.group(), "").strip().strip("-–—: ")
            desc = to_title_case(desc) if desc else "Unnamed"
            return {"amount": amt, "description": desc, "category": guess_category(desc), "payment": payment, "date": custom_date}

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
        "`fifteen thousand rent`\n"
        "`2.5K uber via cash`\n"
        "`5000 SIP quant small cap`\n"
        "`450 chai on 18th April`\n"
        "`3000 groceries yesterday`\n\n"
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
        "Categories are auto-detected from keywords. You can also set it manually:\n"
        "`450 chai #food`",
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
        raw = raw[:cat_match.start()].strip()

    parsed = parse_input(raw)
    if not parsed:
        await update.message.reply_text(
            "Couldn't parse that. Try:\n"
            "`450 chai`\n"
            "`fifteen thousand rent`\n"
            "`2.5K uber via cash`\n"
            "`450 chai on 18th April`",
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

    log.info("Trakos is running.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
