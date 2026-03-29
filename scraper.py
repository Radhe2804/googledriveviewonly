"""
Google Drive Scraper using Playwright
Fixes applied:
  #2  - base64 import moved to top (was inside loop)
  #3  - Duplicate JS extracted to _capture_blob_images()
  #4  - Dead variable removed from _handle_generic
  #5  - _detect_viewer waits for redirect to fully settle
  #6  - _handle_document checks HTTP status (no silent 403)
  #9  - Images written to disk per-page (not held in RAM)
  #10 - img2pdf reads from file paths, not RAM list
  #11 - Retry wrapper on page.goto (2 retries)
  #12 - cleanup() called on cancel to remove temp files
  #14 - SCROLL_STALL_LIMIT now reads from Config
  #15 - _handle_presentation added (was falling through to generic)
"""

import asyncio
import base64
import os
import re
import time
import logging
import uuid
import glob
from typing import Optional, Tuple, List

from playwright.async_api import async_playwright, Page, Browser
from PIL import Image
import img2pdf
import io
from pypdf import PdfReader

from config import Config

logger = logging.getLogger(__name__)

os.makedirs(Config.TEMP_DIR, exist_ok=True)


class DriveScraper:

    DRIVE_PATTERNS = [
        r"https?://drive\.google\.com/file/d/[\w-]+",
        r"https?://drive\.google\.com/drive/folders/[\w-]+",
        r"https?://docs\.google\.com/",
        r"https?://drive\.google\.com/open\?id=[\w-]+",
        r"https?://drive\.google\.com/open\?.*id=[\w-]+",  # handles &usp=drive_copy etc.
    ]

    def __init__(self, url, user_id, job_id, queue, status_msg, client):
        self.url = url
        self.user_id = user_id
        self.job_id = job_id
        self.queue = queue
        self.status_msg = status_msg
        self.client = client
        self.output_path = os.path.join(Config.TEMP_DIR, f"{uuid.uuid4()}.pdf")
        # Per-job temp dir for page images — avoids RAM overload (#9)
        self.page_img_dir = os.path.join(Config.TEMP_DIR, f"pages_{uuid.uuid4().hex}")
        os.makedirs(self.page_img_dir, exist_ok=True)

    @staticmethod
    def is_valid_drive_link(url: str) -> bool:
        return any(re.match(p, url) for p in DriveScraper.DRIVE_PATTERNS)

    # ─────────────────────────────────────────
    # Fix #12 — cleanup temp files on cancel
    # ─────────────────────────────────────────
    def cleanup(self):
        try:
            for f in glob.glob(os.path.join(self.page_img_dir, "*")):
                os.remove(f)
            os.rmdir(self.page_img_dir)
        except Exception:
            pass
        try:
            if os.path.exists(self.output_path):
                os.remove(self.output_path)
        except Exception:
            pass

    # ─────────────────────────────────────────
    # Fix #11 — retry wrapper for page.goto
    # ─────────────────────────────────────────
    async def _goto_with_retry(self, page: Page, url: str, retries: int = 2, **kwargs):
        last_error = None
        for attempt in range(retries + 1):
            try:
                return await page.goto(url, **kwargs)
            except Exception as e:
                last_error = e
                if attempt < retries:
                    wait = (attempt + 1) * 3
                    logger.warning(f"goto attempt {attempt+1} failed, retrying in {wait}s: {e}")
                    await self._update_status(
                        f"⚠️ Page load failed, retrying... ({attempt+2}/{retries+1})"
                    )
                    await asyncio.sleep(wait)
        raise last_error

    # ─────────────────────────────────────────
    # Main entry
    # ─────────────────────────────────────────
    async def run(self) -> Tuple[Optional[str], int]:
        async with async_playwright() as p:
            # On server (Render): headless=True but with flags that make Drive
            # render exactly like a real browser session
            browser: Browser = await p.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-gpu",
                    "--window-size=1280,900",
                    # Anti-detection — makes headless Chrome look like real Chrome to Drive
                    "--disable-blink-features=AutomationControlled",
                    "--disable-features=IsolateOrigins,site-per-process",
                    # Force Drive to render all page images (not skip offscreen ones)
                    "--force-device-scale-factor=1",
                    "--disable-web-security",       # allow canvas access to blob: URLs
                    "--allow-running-insecure-content",
                ]
            )
            context = await browser.new_context(
                viewport={"width": 1280, "height": 900},
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                ),
                extra_http_headers={
                    "Accept-Language": "en-US,en;q=0.9",
                },
                # Remove headless indicators
                java_script_enabled=True,
            )

            # Patch navigator.webdriver to hide automation
            await context.add_init_script("""
                Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
                Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3] });
                Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
            """)
            page: Page = await context.new_page()

            # Extra: override Drive's lazy-load observer so ALL pages render immediately
            await page.add_init_script("""
                // Trick IntersectionObserver — tell Drive all elements are visible
                // so it renders all pages, not just the ones in viewport
                const OriginalIO = window.IntersectionObserver;
                window.IntersectionObserver = class extends OriginalIO {
                    constructor(callback, options) {
                        super((entries, observer) => {
                            entries.forEach(e => {
                                Object.defineProperty(e, 'isIntersecting', { get: () => true });
                                Object.defineProperty(e, 'intersectionRatio', { get: () => 1 });
                            });
                            callback(entries, observer);
                        }, options);
                    }
                };
            """)

            try:
                await self._goto_with_retry(
                    page, self.url,
                    timeout=30000,
                    wait_until="domcontentloaded"
                )
                await asyncio.sleep(3)
                await page.wait_for_load_state("domcontentloaded", timeout=30000)
                await self._update_status("🔍 Detecting page structure...")

                viewer_type = await self._detect_viewer(page)
                logger.info(f"Viewer type: {viewer_type} | Final URL: {page.url}")

                if viewer_type == "pdf_viewer":
                    return await self._handle_pdf_viewer(page, browser)
                elif viewer_type == "native_pdf_viewer":
                    return await self._handle_native_pdf_viewer(page, browser)
                elif viewer_type == "document":
                    return await self._handle_document(page, browser)
                elif viewer_type == "presentation":
                    return await self._handle_presentation(page, browser)
                else:
                    return await self._handle_generic(page, browser)

            finally:
                await browser.close()

    # ─────────────────────────────────────────
    # Fix #5 — waits for redirect to fully settle
    # ─────────────────────────────────────────
    async def _detect_viewer(self, page: Page) -> str:
        await asyncio.sleep(2)
        url = page.url
        logger.info(f"Detecting viewer from URL: {url}")

        if "docs.google.com/document" in url:
            return "document"

        if "docs.google.com/presentation" in url:
            return "presentation"

        if "drive.google.com/file" in url or "docs.google.com/viewer" in url:
            await asyncio.sleep(2)
            blob_imgs = await page.query_selector_all("img[src^='blob:']")
            if blob_imgs:
                return "pdf_viewer"
            return "native_pdf_viewer"

        return "generic"

    # ─────────────────────────────────────────
    # Step 1: Scroll all pages into view first
    # Step 2: Inject the exact working JS to
    #         capture all blob images at once
    # ─────────────────────────────────────────
    async def _capture_blob_images(self, page: Page) -> List[dict]:
        """Capture currently visible blob images via canvas — same logic as the working bookmarklet."""
        return await page.evaluate("""
            () => {
                const imgs = document.getElementsByTagName('img');
                const result = [];
                for (let img of imgs) {
                    if (img.src && img.src.startsWith('blob:') && img.naturalWidth > 100) {
                        try {
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
                        } catch(e) {
                            console.warn('Skipped image:', img.src, e);
                        }
                    }
                }
                return result;
            }
        """)

    # ─────────────────────────────────────────
    # Handler: Blob image viewer
    # Key insight: browser bookmarklet works because ALL pages are
    # already loaded. We must scroll the ENTIRE document first,
    # wait for all pages to render, THEN do one final capture.
    # Fix #2 #3 #9 #12 #14
    # ─────────────────────────────────────────
    async def _handle_pdf_viewer(self, page: Page, browser: Browser) -> Tuple[Optional[str], int]:
        await self._update_status("📖 PDF viewer found!\n\n⏳ Phase 1/2: Scrolling to load all pages...")

        # ── Phase 1: Scroll entire document to trigger lazy loading ──
        scroll_attempts = 0
        no_new_count = 0
        MAX_NO_NEW = Config.SCROLL_STALL_LIMIT
        last_blob_count = 0
        stall_start = None

        while scroll_attempts < 1000:
            if self.queue.is_cancelled(self.user_id):
                self.cleanup()
                raise asyncio.CancelledError()

            # Count blob images without capturing (fast — no canvas)
            blob_count = await page.evaluate("""
                () => {
                    const imgs = document.getElementsByTagName('img');
                    let count = 0;
                    for (let img of imgs) {
                        if (img.src && img.src.startsWith('blob:') && img.naturalWidth > 100) count++;
                    }
                    return count;
                }
            """)

            if blob_count > last_blob_count:
                no_new_count = 0
                stall_start = None
                last_blob_count = blob_count
                await self._update_status(
                    f"⏳ Phase 1/2: Loading pages...\n\n"
                    f"✅ Loaded so far: `{blob_count}` pages\n"
                    f"📜 Scrolling to reveal more..."
                )
            else:
                no_new_count += 1
                if stall_start is None:
                    stall_start = time.time()
                stall_secs = round(time.time() - stall_start)
                remaining = round((MAX_NO_NEW - no_new_count) * Config.SCROLL_DELAY)

                if no_new_count % 2 == 0:
                    await self._update_status(
                        f"⏳ Phase 1/2: Loading pages...\n\n"
                        f"✅ Loaded so far: `{blob_count}` pages\n"
                        f"⏸ No new pages for `{stall_secs}s`\n"
                        f"Stopping in ~{remaining}s if none appear"
                    )

                if no_new_count >= MAX_NO_NEW:
                    # One final scroll to absolute bottom
                    await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                    await asyncio.sleep(Config.SCROLL_DELAY * 2)
                    break

            await page.evaluate("window.scrollBy(0, window.innerHeight * 0.8)")
            await asyncio.sleep(Config.SCROLL_DELAY)
            scroll_attempts += 1

            if blob_count >= Config.MAX_PAGES:
                await self._update_status(f"⚠️ Max page limit ({Config.MAX_PAGES}) reached.")
                break

        total_blobs = last_blob_count
        if total_blobs == 0:
            raise Exception("No pages found. Make sure the link is public and accessible.")

        # ── Phase 2: All pages loaded — now capture all at once ──
        # This mirrors exactly what the bookmarklet does after page is fully loaded
        await self._update_status(
            f"✅ All {total_blobs} pages loaded!\n\n"
            f"🎨 Phase 2/2: Capturing all pages...\n"
            f"(This may take a moment for large documents)"
        )

        # Scroll back to top so canvas renders cleanly
        await page.evaluate("window.scrollTo(0, 0)")
        await asyncio.sleep(1)

        imgs = await self._capture_blob_images(page)

        if not imgs:
            raise Exception("Pages were found but capture failed. Drive may have blocked canvas access.")

        await self._update_status(
            f"✅ Captured `{len(imgs)}` pages!\n\n"
            f"🔨 Building PDF..."
        )

        # Save each captured image to disk
        page_files: List[str] = []
        for i, img in enumerate(imgs):
            if self.queue.is_cancelled(self.user_id):
                self.cleanup()
                raise asyncio.CancelledError()

            img_path = os.path.join(self.page_img_dir, f"page_{i:04d}.jpg")
            _, b64data = img['dataUrl'].split(",", 1)
            raw = base64.b64decode(b64data)

            pil_img = Image.open(io.BytesIO(raw))
            if pil_img.mode in ("RGBA", "P"):
                pil_img = pil_img.convert("RGB")
            pil_img.save(img_path, format="JPEG", quality=90, optimize=True)
            page_files.append(img_path)

            if (i + 1) % 10 == 0 or (i + 1) == len(imgs):
                pct = round((i + 1) / len(imgs) * 100)
                bar = '█' * (pct // 5) + '░' * (20 - pct // 5)
                await self._update_status(
                    f"🔨 Building PDF...\n\n"
                    f"`{bar}` `{pct}%`\n"
                    f"Pages saved: `{i+1}/{len(imgs)}`"
                )

        return await self._build_pdf_from_files(page_files, len(page_files))

    # ─────────────────────────────────────────
    # Handler: Native Google Drive PDF viewer
    # ─────────────────────────────────────────
    async def _handle_native_pdf_viewer(self, page: Page, browser: Browser) -> Tuple[Optional[str], int]:
        await self._update_status("📄 Google Drive PDF viewer detected.\n⬇️ Downloading directly...")

        url = page.url
        file_id = None

        for pattern in [r"/file/d/([\w-]+)", r"[?&]id=([\w-]+)"]:
            match = re.search(pattern, url)
            if match:
                file_id = match.group(1)
                break
        if not file_id:
            match = re.search(r"[?&]id=([\w-]+)", self.url)
            if match:
                file_id = match.group(1)
        if not file_id:
            raise Exception("Could not extract file ID from Drive URL.")

        logger.info(f"File ID: {file_id}")
        export_url = f"https://drive.google.com/uc?export=download&id={file_id}"

        await self._update_status(f"📄 File ID: `{file_id}`\n⬇️ Downloading PDF...")

        download_page = await browser.new_page()
        try:
            async with download_page.expect_download(timeout=300000) as dl_info:
                await download_page.goto(export_url, wait_until="domcontentloaded", timeout=30000)
                try:
                    btn = await download_page.wait_for_selector(
                        "a#uc-download-link, form#download-form input[type=submit]",
                        timeout=5000
                    )
                    if btn:
                        await self._update_status("⚠️ Large file warning — confirming...")
                        await btn.click()
                except Exception:
                    pass

            dl = await dl_info.value
            await self._update_status("📦 Download complete! Saving...")
            await dl.save_as(self.output_path)

        except Exception as e:
            logger.warning(f"Direct download failed: {e}. Trying fallback...")
            try:
                await download_page.close()
            except Exception:
                pass
            return await self._download_via_fetch(file_id, browser)
        finally:
            try:
                await download_page.close()
            except Exception:
                pass

        page_count = self._count_pdf_pages(self.output_path)
        size_mb = round(os.path.getsize(self.output_path) / (1024 * 1024), 2)
        await self._update_status(
            f"✅ PDF downloaded!\n\n"
            f"📄 Pages: `{page_count}`\n"
            f"📦 Size: `{size_mb} MB`"
        )
        return self.output_path, page_count

    async def _download_via_fetch(self, file_id: str, browser: Browser) -> Tuple[Optional[str], int]:
        await self._update_status("🔄 Trying alternative download method...")
        dl_page = await browser.new_page()
        try:
            url = f"https://drive.google.com/uc?export=download&id={file_id}&confirm=t"
            response = await dl_page.goto(url, timeout=300000, wait_until="networkidle")
            if response and response.status == 200:
                body = await response.body()
                with open(self.output_path, "wb") as f:
                    f.write(body)
                return self.output_path, self._count_pdf_pages(self.output_path)
            raise Exception(f"HTTP {response.status if response else '?'}")
        finally:
            await dl_page.close()

    # ─────────────────────────────────────────
    # Handler: Google Docs
    # Fix #6 — checks HTTP status, no silent 403
    # ─────────────────────────────────────────
    async def _handle_document(self, page: Page, browser: Browser) -> Tuple[Optional[str], int]:
        await self._update_status("📄 Google Doc detected. Exporting as PDF...")
        doc_id = re.search(r"/d/([a-zA-Z0-9_-]+)", page.url)
        if not doc_id:
            raise Exception("Could not extract document ID.")

        export_url = f"https://docs.google.com/document/d/{doc_id.group(1)}/export?format=pdf"
        response = await page.goto(export_url, timeout=60000, wait_until="domcontentloaded")

        if not response or response.status != 200:
            raise Exception(
                f"Export failed (HTTP {response.status if response else '?'}). "
                "Make sure the doc is shared as 'Anyone with the link'."
            )
        pdf_bytes = await response.body()
        if len(pdf_bytes) < 100:
            raise Exception("Exported file is empty. Document may not be publicly accessible.")

        with open(self.output_path, "wb") as f:
            f.write(pdf_bytes)
        return self.output_path, self._count_pdf_pages(self.output_path)

    # ─────────────────────────────────────────
    # Handler: Google Slides
    # Fix #15 — was missing, now handled properly
    # ─────────────────────────────────────────
    async def _handle_presentation(self, page: Page, browser: Browser) -> Tuple[Optional[str], int]:
        await self._update_status("📊 Google Slides detected. Exporting as PDF...")
        pres_id = re.search(r"/d/([a-zA-Z0-9_-]+)", page.url)
        if not pres_id:
            raise Exception("Could not extract presentation ID.")

        export_url = f"https://docs.google.com/presentation/d/{pres_id.group(1)}/export?format=pdf"
        response = await page.goto(export_url, timeout=60000, wait_until="domcontentloaded")

        if not response or response.status != 200:
            raise Exception(
                f"Slides export failed (HTTP {response.status if response else '?'}). "
                "Make sure the presentation is shared as 'Anyone with the link'."
            )
        pdf_bytes = await response.body()
        if len(pdf_bytes) < 100:
            raise Exception("Exported file is empty. Presentation may not be publicly accessible.")

        with open(self.output_path, "wb") as f:
            f.write(pdf_bytes)
        return self.output_path, self._count_pdf_pages(self.output_path)

    # ─────────────────────────────────────────
    # Handler: Generic fallback
    # Fix #4 — dead img_list variable removed
    # ─────────────────────────────────────────
    async def _handle_generic(self, page: Page, browser: Browser) -> Tuple[Optional[str], int]:
        await self._update_status("📸 Taking full page screenshot...")
        screenshot = await page.screenshot(full_page=True, type="jpeg", quality=92)
        return await self._build_pdf_from_raw([screenshot], 1)

    # ─────────────────────────────────────────
    # Build PDF from disk files
    # Fix #9 #10 — disk-based, not RAM-based
    # ─────────────────────────────────────────
    async def _build_pdf_from_files(self, page_files: List[str], total: int) -> Tuple[str, int]:
        if self.queue.is_cancelled(self.user_id):
            self.cleanup()
            raise asyncio.CancelledError()

        # img2pdf reads from file paths — never holds all images in RAM (#10)
        with open(self.output_path, "wb") as f:
            f.write(img2pdf.convert(page_files))

        # Clean up page images after PDF built
        for fp in page_files:
            try:
                os.remove(fp)
            except Exception:
                pass
        try:
            os.rmdir(self.page_img_dir)
        except Exception:
            pass

        await self._update_status(
            f"🔨 Building PDF...\n\n"
            f"{'█' * 20} `100%` — Done!"
        )
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

    def _count_pdf_pages(self, pdf_path: str) -> int:
        try:
            return len(PdfReader(pdf_path).pages)
        except Exception:
            return 1

    async def _update_status(self, text: str):
        try:
            await self.status_msg.edit_text(text)
        except Exception:
            pass
