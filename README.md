# 📄 Google Drive → PDF Telegram Bot

Converts public Google Drive document viewer links to PDF and sends via Telegram.
Uses **Pyrogram (MTProto)** — supports up to **2GB** file uploads!

---

## ✨ Features

- 🔗 Accepts public Google Drive viewer links
- 📖 Auto-scrolls to load all lazy pages
- 📊 Live progress updates while processing
- 📤 Upload progress bar while sending
- 🗜️ Image optimization for smaller PDFs
- 👥 Queue system — multiple users supported
- ⏱️ Rate limiting (3 jobs/user/hour)
- ❌ /cancel — cancel ongoing job
- 📊 /stats — admin usage dashboard
- 💾 SQLite logging of all jobs
- 🐳 Docker-ready for Render deployment

---

## 🚀 Quick Setup

### 1. Get Credentials

**Telegram API credentials** → https://my.telegram.org/apps
- Note your `API_ID` and `API_HASH`

**Bot Token** → Message @BotFather on Telegram → `/newbot`

**Your User ID** → Message @userinfobot on Telegram

### 2. Configure

```bash
cp .env.example .env
# Edit .env with your credentials
nano .env
```

### 3. Run Locally

```bash
# Install dependencies
pip install -r requirements.txt

# Install Playwright browser
playwright install chromium
playwright install-deps chromium

# Run
python bot.py
```

---

## 🌐 Deploy on Render

### Option A: Docker (Recommended)

1. Push this folder to a **GitHub repo**
2. Go to https://render.com → New → Web Service
3. Connect your GitHub repo
4. Choose **Docker** runtime
5. Set environment variables in the Render dashboard:
   - `API_ID`
   - `API_HASH`
   - `BOT_TOKEN`
   - `ADMIN_ID`
6. Deploy! 🎉

### Option B: Use render.yaml

The `render.yaml` file is already configured.
Just connect your repo to Render and it will auto-detect it.

### 💡 Render Plan Recommendation

| Plan | RAM | Price | Recommendation |
|------|-----|-------|---------------|
| Free | 512MB | $0 | Works, but sleeps after 15min |
| Starter | 512MB | $7/mo | ✅ Recommended — always on |
| Standard | 2GB | $25/mo | For heavy usage |

> **Note:** Playwright + Chromium needs ~300-400MB RAM.
> Free tier is tight but works for personal use.

---

## 📁 File Structure

```
drive-pdf-bot/
├── bot.py            # Main Telegram bot (Pyrogram)
├── scraper.py        # Playwright Drive scraper
├── queue_manager.py  # Job queue
├── database.py       # SQLite logging + rate limits
├── config.py         # Configuration
├── requirements.txt
├── Dockerfile
├── render.yaml
└── .env.example
```

---

## 🤖 Bot Commands

| Command | Description |
|---------|-------------|
| `/start` | Welcome message + instructions |
| `/status` | Check your current job status |
| `/cancel` | Cancel your ongoing job |
| `/stats` | Admin: view bot-wide statistics |
| Send a Drive link | Starts conversion job |

---

## ⚠️ Limitations

- Only works with **public** Google Drive links
- Google may block headless browsers occasionally (retry if it fails)
- Free Render tier sleeps after 15min — first request after sleep takes ~30s
- Very large documents (500+ pages) may take several minutes

---

## 🛠️ Troubleshooting

**Bot not responding?**
→ Check your `BOT_TOKEN` in `.env`

**"No pages found" error?**
→ Make sure the Drive link is publicly accessible (open in incognito to verify)

**Playwright not installed?**
→ Run `playwright install chromium && playwright install-deps chromium`

**Out of memory on Render?**
→ Upgrade to Starter plan ($7/mo)
