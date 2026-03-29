"""
Google Drive Scraper using Playwright
Scrolls through viewer, captures all page images, builds PDF
"""

import asyncio
import os
import re
import time
import logging
import uuid
from typing import Optional, Tuple

from playwright.async_api import async_playwright, Page, Browser
from PIL import Image
import img2pdf
import io

from config import Config

logger = logging.getLogger(__name__)

# Ensure temp dir exists
os.makedirs(Config.TEMP_DIR, exist_ok=True)


class DriveScraper:
    # Valid Google Drive URL patterns
    DRIVE_PATTERNS = [
        r"https?://drive\.google\.com/file/d/[\w-]+",
        r"https?://drive\.google\.com/drive/folders/[\w-]+",
        r"https?://docs\.google\.com/",
        r"https?://drive\.google\.com/open\?id=[\w-]+",
    ]

    def __init__(self, url, user_id, job_id, queue, status_msg, client):
        self.url = url
        self.user_id = user_id
        self.job_id = job_id
        self.queue = queue
        self.status_msg = status_msg
        self.client = client
        self.output_path = os.path.join(Config.TEMP_DIR, f"{uuid.uuid4()}.pdf")

    @staticmethod
    def is_valid_drive_link(url: str) -> bool:
        return any(re.match(p, url) for p in DriveScraper.DRIVE_PATTERNS)

    async def run(self) -> Tuple[Optional[str], int]:
        async with async_playwright() as p:
            browser: Browser = await p.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-gpu",
                ]
            )

            context = await browser.new_context(
                viewport={"width": 1280, "height": 900},
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                )
            )

            page: Page = await context.new_page()

            try:
                # Open the Drive link
                await page.goto(self.url, timeout=Config.BROWSER_TIMEOUT, wait_until="networkidle")
                await asyncio.sleep(2)

                # Update status
                await self._update_status("🔍 Detecting page structure...")

                # Detect Drive viewer type
                viewer_type = await self._detect_viewer(page)
                logger.info(f"Viewer type: {viewer_type}")

                if viewer_type == "pdf_viewer":
                    return await self._handle_pdf_viewer(page, browser)
                elif viewer_type == "image_viewer":
                    return await self._handle_image_viewer(page, browser)
                elif viewer_type == "document":
                    return await self._handle_document(page, browser)
                else:
                    return await self._handle_generic(page, browser)

            finally:
                await browser.close()

    async def _detect_viewer(self, page: Page) -> str:
        url = page.url
        if "docs.google.com/viewer" in url or "drive.google.com/file" in url:
            # Check if it's rendering images page by page
            has_page_imgs = await page.query_selector_all("img[src*='blob:']")
            if has_page_imgs:
                return "pdf_viewer"
        if "docs.google.com/document" in url:
            return "document"
        if "docs.google.com/presentation" in url:
            return "presentation"
        return "generic"

    # ─────────────────────────────────────────
    # Handler: Google Drive PDF/Book Viewer
    # (blob: images, page by page)
    # ─────────────────────────────────────────
    async def _handle_pdf_viewer(self, page: Page, browser: Browser) -> Tuple[Optional[str], int]:
        await self._update_status("📖 Found PDF viewer. Scrolling to load all pages...")

        images_data = []
        seen_srcs = set()
        scroll_attempts = 0
        max_scroll_attempts = 200
        last_img_count = 0
        no_new_count = 0

        # Scroll loop to lazy-load all pages
        while scroll_attempts < max_scroll_attempts:
            # Check cancel
            if self.queue.is_cancelled(self.user_id):
                raise asyncio.CancelledError()

            # Capture all current blob images
            imgs = await page.evaluate("""
                () => {
                    const imgs = document.getElementsByTagName('img');
                    const result = [];
                    for (let img of imgs) {
                        if (img.src && img.src.startsWith('blob:') && img.naturalWidth > 100) {
                            const canvas = document.createElement('canvas');
                            canvas.width = img.naturalWidth;
                            canvas.height = img.naturalHeight;
                            const ctx = canvas.getContext('2d');
                            ctx.drawImage(img, 0, 0);
                            result.push({
                                src: img.src,
                                dataUrl: canvas.toDataURL('image/jpeg', 0.92),
                                width: img.naturalWidth,
                                height: img.naturalHeight
                            });
                        }
                    }
                    return result;
                }
            """)

            # Add new images
            for img in imgs:
                if img['src'] not in seen_srcs and img['width'] > 100:
                    seen_srcs.add(img['src'])
                    images_data.append(img)

            current_count = len(images_data)

            # Update progress
            if current_count > last_img_count:
                await self._update_status(
                    f"📄 Loading pages... `{current_count}` found so far\n"
                    f"⏳ Scrolling to load more..."
                )
                no_new_count = 0
            else:
                no_new_count += 1

            # Stop if no new images for 5 scroll attempts
            if no_new_count >= 5:
                break

            last_img_count = current_count

            # Scroll down
            await page.evaluate("window.scrollBy(0, window.innerHeight * 0.8)")
            await asyncio.sleep(Config.SCROLL_DELAY)
            scroll_attempts += 1

            # Safety cap
            if current_count >= Config.MAX_PAGES:
                await self._update_status(f"⚠️ Hit max page limit ({Config.MAX_PAGES}). Building PDF...")
                break

        if not images_data:
            raise Exception("No pages found. Make sure the link is public and accessible.")

        total = len(images_data)
        await self._update_status(f"✅ Found **{total} pages**! Building PDF...")

        return await self._build_pdf(images_data, total)

    # ─────────────────────────────────────────
    # Handler: Generic (screenshot each page)
    # ─────────────────────────────────────────
    async def _handle_generic(self, page: Page, browser: Browser) -> Tuple[Optional[str], int]:
        await self._update_status("📸 Taking full page screenshot...")

        screenshot = await page.screenshot(full_page=True, type="jpeg", quality=92)

        img_list = [{"dataUrl": None, "raw": screenshot, "width": 1280, "height": 900}]
        return await self._build_pdf_from_raw([screenshot], 1)

    async def _handle_document(self, page: Page, browser: Browser) -> Tuple[Optional[str], int]:
        await self._update_status("📄 Google Doc detected. Exporting as PDF...")
        # For Google Docs, use export URL
        doc_id = re.search(r"/d/([a-zA-Z0-9_-]+)", page.url)
        if doc_id:
            export_url = f"https://docs.google.com/document/d/{doc_id.group(1)}/export?format=pdf"
            response = await page.goto(export_url)
            pdf_bytes = await response.body()
            with open(self.output_path, "wb") as f:
                f.write(pdf_bytes)
            return self.output_path, 1
        raise Exception("Could not extract document ID")

    async def _handle_image_viewer(self, page: Page, browser: Browser) -> Tuple[Optional[str], int]:
        return await self._handle_pdf_viewer(page, browser)

    # ─────────────────────────────────────────
    # Build PDF from dataURL images
    # ─────────────────────────────────────────
    async def _build_pdf(self, images_data: list, total: int) -> Tuple[str, int]:
        image_bytes_list = []

        for i, img_data in enumerate(images_data):
            if self.queue.is_cancelled(self.user_id):
                raise asyncio.CancelledError()

            # Parse dataURL → bytes
            data_url = img_data['dataUrl']
            header, b64data = data_url.split(",", 1)
            import base64
            img_bytes = base64.b64decode(b64data)

            # Optimize with PIL
            pil_img = Image.open(io.BytesIO(img_bytes))
            if pil_img.mode in ("RGBA", "P"):
                pil_img = pil_img.convert("RGB")

            out = io.BytesIO()
            pil_img.save(out, format="JPEG", quality=90, optimize=True)
            image_bytes_list.append(out.getvalue())

            # Progress update every 5 pages
            if (i + 1) % 5 == 0 or (i + 1) == total:
                self.queue.update_progress(self.user_id, i + 1, total)
                await self._update_status(
                    f"🔨 Building PDF...\n\n"
                    f"Progress: `{i+1}/{total}` pages\n"
                    f"{'█' * int((i+1)/total*20)}{'░' * (20 - int((i+1)/total*20))} `{round((i+1)/total*100)}%`"
                )

        # Write PDF
        with open(self.output_path, "wb") as f:
            f.write(img2pdf.convert(image_bytes_list))

        return self.output_path, total

    async def _build_pdf_from_raw(self, raw_images: list, total: int) -> Tuple[str, int]:
        processed = []
        for raw in raw_images:
            pil_img = Image.open(io.BytesIO(raw))
            if pil_img.mode in ("RGBA", "P"):
                pil_img = pil_img.convert("RGB")
            out = io.BytesIO()
            pil_img.save(out, format="JPEG", quality=90)
            processed.append(out.getvalue())

        with open(self.output_path, "wb") as f:
            f.write(img2pdf.convert(processed))

        return self.output_path, total

    async def _update_status(self, text: str):
        try:
            await self.status_msg.edit_text(text)
        except Exception:
            pass
