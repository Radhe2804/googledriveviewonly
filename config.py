"""
Configuration — reads from environment variables
"""

import os
from dotenv import load_dotenv

load_dotenv()


class Config:
    # Telegram credentials (from my.telegram.org)
    API_ID: int = int(os.getenv("API_ID", "0"))
    API_HASH: str = os.getenv("API_HASH", "")
    BOT_TOKEN: str = os.getenv("BOT_TOKEN", "")

    # Your Telegram user ID (for /stats admin command)
    ADMIN_ID: int = int(os.getenv("ADMIN_ID", "0"))

    # Rate limiting
    MAX_JOBS_PER_HOUR: int = int(os.getenv("MAX_JOBS_PER_HOUR", "3"))

    # Playwright settings
    BROWSER_TIMEOUT: int = int(os.getenv("BROWSER_TIMEOUT", "30000"))    # ms — only for initial page load
    SCROLL_DELAY: float = float(os.getenv("SCROLL_DELAY", "1.5"))         # seconds between scrolls
    MAX_PAGES: int = int(os.getenv("MAX_PAGES", "500"))                   # safety cap
    SCROLL_STALL_LIMIT: int = int(os.getenv("SCROLL_STALL_LIMIT", "8"))  # scrolls with no new page before stopping

    # Temp directory for PDFs
    TEMP_DIR: str = os.getenv("TEMP_DIR", "/tmp/drive_pdf_bot")

    # SQLite DB path
    DB_PATH: str = os.getenv("DB_PATH", "bot_data.db")
