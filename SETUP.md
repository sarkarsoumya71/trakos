# Trakos — Setup Guide

Personal Telegram expense tracker that logs directly to Google Sheets.

## What You'll Set Up

1. Telegram bot (2 min)
2. Google Sheets API access (10 min)
3. Deploy the bot (5 min)

Total: ~17 minutes.

---

## Step 1: Create the Telegram Bot

1. Open Telegram, search for **@BotFather**
2. Send `/newbot`
3. Name: `Trakos`
4. Username: pick something like `trakos_finance_bot` (must be unique and end in `bot`)
5. BotFather will reply with a **token** like `7123456789:AAH...`. Copy it. You'll need it.

## Step 2: Get Your Telegram User ID

1. Open Telegram, search for **@userinfobot**
2. Send `/start`
3. It replies with your **User ID** (a number like `123456789`). Copy it.

This ensures only YOU can use the bot.

## Step 3: Set Up Google Sheets API

### 3a. Create a Google Cloud Project

1. Go to [console.cloud.google.com](https://console.cloud.google.com)
2. Click the project dropdown at the top → **New Project**
3. Name it `Trakos` → **Create**
4. Make sure the new project is selected in the dropdown

### 3b. Enable the APIs

1. Go to **APIs & Services → Library**
2. Search for **Google Sheets API** → click it → **Enable**
3. Search for **Google Drive API** → click it → **Enable**

### 3c. Create a Service Account

1. Go to **APIs & Services → Credentials**
2. Click **Create Credentials → Service Account**
3. Name: `trakos-bot` → **Create and Continue**
4. Skip the role/access steps → **Done**
5. Click on the service account you just created
6. Go to **Keys** tab → **Add Key → Create New Key → JSON → Create**
7. A `.json` file downloads. This is your credentials file. Keep it safe.

### 3d. Create and Share the Google Sheet

1. Go to [sheets.google.com](https://sheets.google.com) → create a new blank spreadsheet
2. Name it `Trakos Expenses`
3. Copy the **Sheet ID** from the URL:
   ```
   https://docs.google.com/spreadsheets/d/COPY_THIS_PART/edit
   ```
4. Open the JSON credentials file you downloaded. Find the `client_email` field (looks like `trakos-bot@trakos-xxxxx.iam.gserviceaccount.com`)
5. In your Google Sheet, click **Share** → paste that email → give **Editor** access → **Send**

---

## Step 4: Deploy

### Option A: Railway (Recommended — Free Tier)

1. Push this code to a GitHub repo (or use Railway's direct deploy)
2. Go to [railway.app](https://railway.app) → **New Project → Deploy from GitHub Repo**
3. Select the repo
4. Go to **Variables** tab and add:
   - `TELEGRAM_TOKEN` = your bot token from Step 1
   - `GOOGLE_CREDS_JSON` = the entire contents of the JSON file (paste as one line)
   - `SHEET_ID` = your sheet ID from Step 3d
   - `ALLOWED_USER_IDS` = your Telegram user ID from Step 2
5. Deploy. The bot starts automatically.

### Option B: Render (Free Tier)

1. Push code to GitHub
2. Go to [render.com](https://render.com) → **New → Background Worker**
3. Connect your GitHub repo
4. Runtime: **Docker**
5. Add the same environment variables as above
6. Deploy.

### Option C: Run Locally (for testing)

```bash
cd trakos
pip install -r requirements.txt

export TELEGRAM_TOKEN="your_token"
export GOOGLE_CREDS_JSON='paste_json_here'
export SHEET_ID="your_sheet_id"
export ALLOWED_USER_IDS="your_user_id"

python bot.py
```

---

## How to Use

Open your bot in Telegram and just type:

| Input | What gets logged |
|---|---|
| `450 chai` | ₹450, chai, Food, UPI |
| `fifteen thousand rent` | ₹15,000, rent, Bills & Utilities, UPI |
| `2.5K uber via cash` | ₹2,500, uber, Transport, CASH |
| `5000 SIP quant small cap` | ₹5,000, SIP quant small cap, Financial Investment, UPI |
| `3200 headphones #shopping` | ₹3,200, headphones, Shopping, UPI |

### Commands

| Command | What it does |
|---|---|
| `/today` | Today's total + breakdown |
| `/week` | Last 7 days summary |
| `/month` | Last 30 days summary |
| `/sheet` | Direct link to your Google Sheet |
| `/categories` | List all categories |
| `/help` | Usage guide |

### Category Override

Add `#categoryname` to manually set the category:
- `500 gift #shopping`
- `2000 books #businessinvestment`

### Payment Method

Add `via cash` or `via card` or `using gpay`:
- `450 chai via cash`
- `3000 shoes via card1`

Default payment method is UPI if not specified.

---

## Your Google Sheet

The bot creates these columns automatically:

| Date | Time | Amount | Description | Category | Payment Method | Raw Input |
|---|---|---|---|---|---|---|
| 11/04/2026 | 14:30 | 450 | chai | Food | UPI | 450 chai |

You can add charts, pivot tables, or formulas on top of this data.
The "Raw Input" column keeps your original message for reference.

---

## Security

- Only your Telegram user ID can interact with the bot
- Google credentials are stored as environment variables (not in code)
- The bot only has access to the one sheet you shared with it
- No data is stored anywhere except your Google Sheet
