"""
Trakos — Telegram expense tracker that logs to Google Sheets.
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
GOOGLE_CREDS_JSON = os.environ.get("GOOGLE_CREDS_JSON", "")  # JSON string of service account
SHEET_ID = os.environ.get("SHEET_ID", "")  # Google Sheet ID
ALLOWED_USER_IDS = os.environ.get("ALLOWED_USER_IDS", "")  # comma-separated Telegram user IDs
TIMEZONE = ZoneInfo("Asia/Kolkata")

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("trakos")

# ─── Google Sheets ────────────────────────────────────────
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

def get_sheet():
    """Connect to Google Sheets and return the first worksheet."""
    creds_dict = json.loads(GOOGLE_CREDS_JSON)
    creds = Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
    gc = gspread.authorize(creds)
    sh = gc.open_by_key(SHEET_ID)
    
    # Ensure header row exists
    ws = sh.sheet1
    first_row = ws.row_values(1)
    if not first_row or first_row[0] != "Date":
        ws.update("A1:G1", [["Date", "Time", "Amount", "Description", "Category", "Payment Method", "Raw Input"]])
        ws.format("A1:G1", {"textFormat": {"bold": True}, "backgroundColor": {"red": 0.95, "green": 0.95, "blue": 0.95}})
    return ws


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

def words_to_number(text: str) -> float | None:
    """Convert word-based numbers to float. Returns None if no number words found."""
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


def parse_amount(raw: str) -> float | None:
    """Parse amount from string — handles digits, suffixes (K/L/Cr), and words."""
    s = raw.strip()
    
    # Numeric with suffix: 2.5K, 1.5L, 3Cr
    m = re.match(r"^([\d,]+\.?\d*)\s*(k|l|lakh|lakhs|cr|crore|crores)?$", s, re.IGNORECASE)
    if m:
        num = float(m.group(1).replace(",", ""))
        suf = (m.group(2) or "").lower()
        if suf == "k":
            return num * 1000
        if suf in ("l", "lakh", "lakhs"):
            return num * 100000
        if suf in ("cr", "crore", "crores"):
            return num * 10000000
        return num
    
    # Plain numeric
    try:
        val = float(s.replace(",", ""))
        if val > 0:
            return val
    except ValueError:
        pass
    
    # Word-based
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
    return "UPI"  # default


# ─── Input Parser ────────────────────────────────────────
def parse_input(raw: str) -> dict | None:
    """
    Parse user input into {amount, description, category, payment}.
    Supports formats:
        450 chai
        chai 450
        fifteen thousand rent
        rent fifteen thousand
        2.5K uber via cash
        5000 SIP quant small cap
    """
    text = raw.strip()
    if not text:
        return None
    
    # Extract payment method if "via X" is present
    payment = "UPI"
    via_match = re.search(r"\b(?:via|using|through|by)\s+(.+)$", text, re.IGNORECASE)
    if via_match:
        payment = guess_payment(via_match.group(1))
        text = text[:via_match.start()].strip()
    
    # Try: leading number (digits)
    m = re.match(r"^([\d,]+\.?\d*\s*(?:k|l|lakh|lakhs|cr|crore|crores)?)\s+(.+)", text, re.IGNORECASE)
    if m:
        amt = parse_amount(m.group(1))
        if amt:
            desc = m.group(2).strip()
            return {"amount": amt, "description": desc, "category": guess_category(desc), "payment": payment}
    
    # Try: trailing number (digits)
    m = re.match(r"(.+?)\s+([\d,]+\.?\d*\s*(?:k|l|lakh|lakhs|cr|crore|crores)?)$", text, re.IGNORECASE)
    if m:
        amt = parse_amount(m.group(2))
        if amt:
            desc = m.group(1).strip()
            return {"amount": amt, "description": desc, "category": guess_category(desc), "payment": payment}
    
    # Try: word-numbers at start
    word_num_pattern = r"((?:(?:zero|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|twenty|thirty|forty|fifty|sixty|seventy|eighty|ninety|hundred|thousand|lakh|lakhs|crore|crores|million|billion|and)\s*)+)"
    
    m = re.match(rf"^{word_num_pattern}\s+(.+)", text, re.IGNORECASE)
    if m:
        amt = words_to_number(m.group(1))
        if amt and amt > 0:
            desc = m.group(2).strip()
            return {"amount": amt, "description": desc, "category": guess_category(desc), "payment": payment}
    
    # Try: word-numbers at end
    m = re.match(rf"(.+?)\s+{word_num_pattern}$", text, re.IGNORECASE)
    if m:
        amt = words_to_number(m.group(2))
        if amt and amt > 0:
            desc = m.group(1).strip()
            return {"amount": amt, "description": desc, "category": guess_category(desc), "payment": payment}
    
    # Fallback: find any number in string
    m = re.search(r"[\d,]+\.?\d*", text)
    if m:
        amt = parse_amount(m.group())
        if amt:
            desc = text.replace(m.group(), "").strip().strip("-–—: ")
            return {"amount": amt, "description": desc or "Unnamed", "category": guess_category(desc or text), "payment": payment}
    
    return None


def format_inr(n: float) -> str:
    """Format number as INR with commas."""
    if n >= 10000000:
        return f"₹{n/10000000:.1f}Cr".replace(".0Cr", "Cr")
    if n >= 100000:
        return f"₹{n/100000:.1f}L".replace(".0L", "L")
    return f"₹{n:,.0f}"


# ─── Auth Check ──────────────────────────────────────────
def is_authorized(user_id: int) -> bool:
    if not ALLOWED_USER_IDS:
        return True  # no restriction if not set
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
        "`5000 SIP quant small cap`\n\n"
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
    """Read sheet and compute summary for given period."""
    try:
        ws = get_sheet()
        rows = ws.get_all_values()[1:]  # skip header
    except Exception as e:
        log.error(f"Sheet read error: {e}")
        await update.message.reply_text("Could not read sheet. Check connection.")
        return
    
    now = datetime.now(TIMEZONE)
    if days == 0:
        cutoff = now.replace(hour=0, minute=0, second=0, microsecond=0)
    else:
        cutoff = now - timedelta(days=days)
    
    total = 0.0
    cat_totals = {}
    count = 0
    
    for row in rows:
        if len(row) < 3:
            continue
        try:
            # Parse date from DD/MM/YYYY format
            row_date = datetime.strptime(row[0], "%d/%m/%Y").replace(tzinfo=TIMEZONE)
            if row_date >= cutoff:
                amt = float(row[2].replace(",", ""))
                total += amt
                count += 1
                cat = row[4] if len(row) > 4 else "Other"
                cat_totals[cat] = cat_totals.get(cat, 0) + amt
        except (ValueError, IndexError):
            continue
    
    if count == 0:
        await update.message.reply_text(f"*{label}:* No expenses logged.", parse_mode="Markdown")
        return
    
    # Build summary
    lines = [f"*{label}*\n", f"Total: *{format_inr(total)}* ({count} entries)\n"]
    
    sorted_cats = sorted(cat_totals.items(), key=lambda x: -x[1])
    for cat, amt in sorted_cats:
        pct = (amt / total) * 100
        lines.append(f"  {cat}: {format_inr(amt)} ({pct:.0f}%)")
    
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle incoming text messages — parse and log expenses."""
    if not is_authorized(update.effective_user.id):
        await update.message.reply_text("Not authorized.")
        return
    
    raw = update.message.text.strip()
    if not raw or raw.startswith("/"):
        return
    
    # Check for manual category override: "450 chai #food"
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
            "`2.5K uber via cash`",
            parse_mode="Markdown",
        )
        return
    
    if manual_cat:
        parsed["category"] = manual_cat
    
    # Log to Google Sheets
    now = datetime.now(TIMEZONE)
    row = [
        now.strftime("%d/%m/%Y"),
        now.strftime("%H:%M"),
        parsed["amount"],
        parsed["description"],
        parsed["category"],
        parsed["payment"],
        update.message.text.strip(),  # raw input for reference
    ]
    
    try:
        ws = get_sheet()
        ws.append_row(row, value_input_option="USER_ENTERED")
    except Exception as e:
        log.error(f"Sheet write error: {e}")
        await update.message.reply_text("Logged but failed to write to sheet. Will retry.")
        return
    
    await update.message.reply_text(
        f"✓ *{format_inr(parsed['amount'])}* — {parsed['description']}\n"
        f"  {parsed['category']} · {parsed['payment']}",
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
    
    # Register handlers
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
