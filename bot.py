"""
Google Drive → PDF Telegram Bot
Uses Pyrogram (MTProto) for 2GB file support
"""

import asyncio
import os
import time
import logging
from pyrogram import Client, filters
from pyrogram.types import Message
from config import Config
from database import Database
from queue_manager import JobQueue
from scraper import DriveScraper

# Logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)
logger = logging.getLogger(__name__)

# Init
app = Client(
    "drive_pdf_bot",
    api_id=Config.API_ID,
    api_hash=Config.API_HASH,
    bot_token=Config.BOT_TOKEN
)

db = Database()
queue = JobQueue()


# ─────────────────────────────────────────────
# /start
# ─────────────────────────────────────────────
@app.on_message(filters.command("start"))
async def start_handler(client: Client, message: Message):
    await message.reply_text(
        "👋 **Google Drive → PDF Bot**\n\n"
        "Send me a public Google Drive document/viewer link and I'll convert it to a PDF and send it back!\n\n"
        "**Supported links:**\n"
        "• `drive.google.com/file/...`\n"
        "• `drive.google.com/drive/folders/...`\n"
        "• `docs.google.com/...`\n\n"
        "**Commands:**\n"
        "/start - Show this message\n"
        "/status - Check your job status\n"
        "/cancel - Cancel current job\n"
        "/stats - Bot statistics (admin)\n\n"
        "⚡ Powered by Pyrogram — supports up to **2GB** PDFs!"
    )


# ─────────────────────────────────────────────
# /cancel
# ─────────────────────────────────────────────
@app.on_message(filters.command("cancel"))
async def cancel_handler(client: Client, message: Message):
    user_id = message.from_user.id
    cancelled = queue.cancel_job(user_id)
    if cancelled:
        await message.reply_text("❌ Your job has been cancelled.")
    else:
        await message.reply_text("ℹ️ No active job to cancel.")


# ─────────────────────────────────────────────
# /status
# ─────────────────────────────────────────────
@app.on_message(filters.command("status"))
async def status_handler(client: Client, message: Message):
    user_id = message.from_user.id
    job = queue.get_job(user_id)
    if job:
        await message.reply_text(
            f"🔄 **Job in progress**\n"
            f"Status: `{job['status']}`\n"
            f"Pages done: `{job.get('pages_done', 0)}/{job.get('total_pages', '?')}`"
        )
    else:
        stats = db.get_user_stats(user_id)
        await message.reply_text(
            f"✅ No active job.\n\n"
            f"📊 Your stats:\n"
            f"Total jobs: `{stats['total_jobs']}`\n"
            f"Total PDFs: `{stats['total_pdfs']}`"
        )


# ─────────────────────────────────────────────
# /stats (Admin only)
# ─────────────────────────────────────────────
@app.on_message(filters.command("stats"))
async def stats_handler(client: Client, message: Message):
    if message.from_user.id != Config.ADMIN_ID:
        return await message.reply_text("⛔ Admin only.")
    stats = db.get_global_stats()
    await message.reply_text(
        f"📊 **Bot Statistics**\n\n"
        f"Total users: `{stats['total_users']}`\n"
        f"Total jobs: `{stats['total_jobs']}`\n"
        f"Successful: `{stats['successful']}`\n"
        f"Failed: `{stats['failed']}`\n"
        f"Active jobs: `{queue.active_count()}`"
    )


# ─────────────────────────────────────────────
# Handle Drive links
# ─────────────────────────────────────────────
@app.on_message(filters.text & filters.private & ~filters.command(["start", "cancel", "status", "stats"]))
async def link_handler(client: Client, message: Message):
    user_id = message.from_user.id
    text = message.text.strip()

    # Validate Drive link
    if not DriveScraper.is_valid_drive_link(text):
        return await message.reply_text(
            "⚠️ Please send a valid **public Google Drive** link.\n\n"
            "Example:\n`https://drive.google.com/file/d/XXXX/view`"
        )

    # Rate limit check (admin is exempt)
    if user_id != Config.ADMIN_ID and not db.check_rate_limit(user_id):
        return await message.reply_text(
            "⏳ **Rate limit reached!**\n"
            "You can process max 3 links per hour. Please wait."
        )

    # Check if already has active job
    if queue.has_active_job(user_id):
        return await message.reply_text(
            "🔄 You already have an active job running!\n"
            "Use /cancel to cancel it first."
        )

    # Start job
    status_msg = await message.reply_text(
        "🚀 **Job started!**\n\n"
        "⏳ Launching browser and opening link...\n"
        "This may take a few seconds."
    )

    db.log_job_start(user_id, text)
    job_id = queue.add_job(user_id)

    try:
        scraper = DriveScraper(
            url=text,
            user_id=user_id,
            job_id=job_id,
            queue=queue,
            status_msg=status_msg,
            client=client
        )

        start_time = time.time()
        pdf_path, page_count = await scraper.run()
        elapsed = round(time.time() - start_time, 1)

        if not pdf_path:
            raise Exception("PDF generation failed")

        file_size_mb = round(os.path.getsize(pdf_path) / (1024 * 1024), 2)

        await status_msg.edit_text(
            f"📤 **Uploading PDF...**\n\n"
            f"📄 Pages: `{page_count}`\n"
            f"📦 Size: `{file_size_mb} MB`\n"
            f"⏱️ Processed in: `{elapsed}s`\n\n"
            f"Please wait while file uploads..."
        )

        # Send PDF via Pyrogram (supports up to 2GB)
        await client.send_document(
            chat_id=message.chat.id,
            document=pdf_path,
            caption=(
                f"✅ **PDF Ready!**\n\n"
                f"📄 Pages: `{page_count}`\n"
                f"📦 Size: `{file_size_mb} MB`\n"
                f"⏱️ Time taken: `{elapsed}s`"
            ),
            progress=upload_progress,
            progress_args=(status_msg, start_time)
        )

        await status_msg.delete()
        db.log_job_success(job_id, page_count, file_size_mb)

        # Cleanup
        if os.path.exists(pdf_path):
            os.remove(pdf_path)

    except asyncio.CancelledError:
        await status_msg.edit_text("❌ Job cancelled by user.")
        db.log_job_failed(job_id, "Cancelled by user")

    except Exception as e:
        logger.error(f"Job failed for user {user_id}: {e}")
        await status_msg.edit_text(
            f"❌ **Job Failed!**\n\n"
            f"Error: `{str(e)[:200]}`\n\n"
            "Please try again or check if the link is publicly accessible."
        )
        db.log_job_failed(job_id, str(e))

    finally:
        queue.remove_job(user_id)


# ─────────────────────────────────────────────
# Upload progress callback
# ─────────────────────────────────────────────
async def upload_progress(current, total, status_msg, start_time):
    percent = round((current / total) * 100, 1)
    uploaded_mb = round(current / (1024 * 1024), 1)
    total_mb = round(total / (1024 * 1024), 1)
    elapsed = round(time.time() - start_time, 1)

    # Update every 10% to avoid flood
    if percent % 10 < 1:
        try:
            await status_msg.edit_text(
                f"📤 **Uploading PDF...**\n\n"
                f"Progress: `{percent}%`\n"
                f"Uploaded: `{uploaded_mb} MB / {total_mb} MB`\n"
                f"⏱️ Elapsed: `{elapsed}s`"
            )
        except Exception:
            pass


# ─────────────────────────────────────────────
# Run
# ─────────────────────────────────────────────
if __name__ == "__main__":
    logger.info("🤖 Bot starting...")
    app.run()
