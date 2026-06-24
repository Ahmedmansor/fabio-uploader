"""
tiktok_uploader.py — TikTok Studio Creator Center Reels uploader.

Uploads and schedules a Reel to TikTok Creator Studio via Playwright + BitBrowser CDP.

Pipeline:
  1.  Connect to the running BitBrowser profile via CDP WebSocket.
  2.  Locate the existing tiktok.com/tiktokstudio page (no navigation / refresh if open).
  3.  Upload the video file and wait until 100 % upload is confirmed by "Replace" button.
  4.  Fill in the Reel description (caption) with human-like typing (clear filename autofill).
  5.  Upload a custom cover image if thumbnail is provided.
  6.  Enable Schedule option. Handle optional allow safety modal.
  7.  Scroll and select hours/minutes using JavaScript scrollIntoView center snapping.
  8.  Select target scheduling day on calendar.
  9.  Click the final "Schedule" button to confirm upload and check success.

Inherits from BaseUploader so it plugs directly into the main.py orchestrator.
"""

import logging
import time
from datetime import datetime
from pathlib import Path

from playwright.sync_api import (
    Browser,
    Page,
    TimeoutError as PlaywrightTimeout,
    sync_playwright,
)

from adspower_utils import connect_playwright_to_browser, start_browser
from config import (
    NAV_TIMEOUT_MS,
    SELECTOR_TIMEOUT_MS,
    UPLOAD_TIMEOUT_MS,
)
from pipeline_config import TIKTOK_UPLOAD_URL
from uploader_base import BaseUploader
from utils import human_sleep

logger = logging.getLogger(__name__)

# ── Selector timeouts ─────────────────────────────────────────────────────────
_STEP_TIMEOUT_MS = 15_000


# ══════════════════════════════════════════════════════════════════════════════
# Low-level page helpers
# ══════════════════════════════════════════════════════════════════════════════

def _get_or_open_tiktok_page(browser: Browser) -> Page:
    """
    Return the active TikTok Studio upload page using ultimate smart routing tab logic:
    1. Scan open tabs for 'tiktokstudio/upload'. Bring it to front if found.
    2. If not found, reuse an empty tab or open a new one and navigate to TIKTOK_UPLOAD_URL.
    """
    # Scan for existing TikTok Studio tabs
    for ctx in browser.contexts:
        for page in ctx.pages:
            if "tiktokstudio/upload" in page.url:
                logger.info("[TikTok] Reusing existing TikTok tab: %s", page.url)
                try:
                    page.bring_to_front()
                except Exception:
                    pass
                return page

    # No tab found — navigate or open a new page
    logger.info(
        "[TikTok] No TikTok Studio tab found. Opening a tab and navigating to: %s",
        TIKTOK_UPLOAD_URL,
    )

    if not browser.contexts:
        raise RuntimeError(
            "[TikTok] BitBrowser has no open contexts. Ensure the profile is running."
        )

    target_page = None
    ctx = browser.contexts[0]
    for page in ctx.pages:
        if page.url == "about:blank":
            target_page = page
            logger.info("[TikTok] Reusing blank page for navigation.")
            break

    if not target_page:
        target_page = ctx.new_page()

    target_page.goto(
        TIKTOK_UPLOAD_URL,
        wait_until="domcontentloaded",
        timeout=NAV_TIMEOUT_MS,
    )
    time.sleep(3)
    logger.info("[TikTok] TikTok Studio page loaded. URL: %s", target_page.url)
    return target_page


def _hs(min_s: float = 0.5, max_s: float = 1.5) -> None:
    """Convenience wrapper for human_sleep."""
    human_sleep(min_s, max_s)


def _wait_visible(page: Page, locator, timeout: int = _STEP_TIMEOUT_MS) -> None:
    """Wait until a locator is visible."""
    locator.wait_for(state="visible", timeout=timeout)


def _wait_for_composer_ready(page: Page) -> None:
    """Wait until composer screen is initialized."""
    landmarks = [
        page.get_by_role("button", name="Select video", exact=True),
        page.get_by_role("button", name="Select video to upload Or"),
        page.get_by_role("button", name="Replace"),
    ]
    start_time = time.time()
    while time.time() - start_time < 15:
        for lm in landmarks:
            if lm.count() > 0 and lm.first.is_visible():
                return
        time.sleep(0.5)
    logger.warning("[TikTok] Warning: No composer landmarks became visible within 15 seconds.")


# ══════════════════════════════════════════════════════════════════════════════
# Individual upload steps
# ══════════════════════════════════════════════════════════════════════════════

def _step_upload_video(page: Page, video_path: Path) -> bool:
    """
    Step 1 & 2 — Upload video or resume if already present.
    Returns True if video file was submitted, False if skipped.
    """
    logger.info("[TikTok] Step 1 & 2 → Uploading video: %s", video_path.name)
    if not video_path.exists():
        raise FileNotFoundError(f"Video file not found: {video_path}")

    replace_btn = page.get_by_role("button", name="Replace")
    if replace_btn.count() > 0 and replace_btn.first.is_visible():
        logger.info("[TikTok] Step 1 & 2 → 'Replace' button visible. Video already uploaded, skipping upload...")
        return False

    select_btn = page.get_by_role("button", name="Select video", exact=True)
    if select_btn.count() == 0 or not select_btn.first.is_visible():
        select_btn = page.get_by_role("button", name="Select video to upload Or")

    _wait_visible(page, select_btn, timeout=_STEP_TIMEOUT_MS)
    _hs(0.5, 1.0)

    with page.expect_file_chooser(timeout=20_000) as fc_info:
        select_btn.first.click()

    fc_info.value.set_files(str(video_path))
    logger.info("[TikTok] Step 1 ✓ Video file submitted to file chooser. Waiting for upload…")
    return True


def _step_wait_for_upload_complete(page: Page, was_skipped: bool = False) -> None:
    """Step 2 (cont) — Wait for 100% upload progress confirmation via the Replace button."""
    logger.info("[TikTok] Step 2 → Waiting for upload confirmation (Replace button)…")
    replace_btn = page.get_by_role("button", name="Replace")

    if was_skipped:
        if replace_btn.count() > 0 and replace_btn.first.is_visible():
            logger.info("[TikTok] Step 2 ✓ Video already uploaded (Replace button visible).")
            return

    # Wait for the upload progress and Replace button to appear
    start_time = time.time()
    while time.time() - start_time < UPLOAD_TIMEOUT_MS / 1000:
        if replace_btn.count() > 0 and replace_btn.first.is_visible():
            logger.info("[TikTok] Step 2 ✓ Video upload reached 100%%. Replace button visible.")
            return

        # Print upload progress text if % is visible
        try:
            percent_loc = page.get_by_text("%")
            if percent_loc.count() > 0:
                pct_text = [el.inner_text() for el in percent_loc.all() if "%" in el.inner_text()]
                if pct_text:
                    logger.info("[TikTok] Video upload progress: %s", pct_text[0])
        except Exception:
            pass

        time.sleep(3)

    raise RuntimeError(
        f"Video upload did not complete within the allowed timeout ({UPLOAD_TIMEOUT_MS // 1000}s)."
    )


def _step_fill_description(page: Page, description_text: str, video_path: Path) -> None:
    """Step 3 — Type caption into description contenteditable box, clearing autofills."""
    logger.info("[TikTok] Step 3 → Filling description…")

    # Click Details tab if visible/needed
    details_tab = page.get_by_text("Details", exact=True)
    if details_tab.count() > 0 and details_tab.first.is_visible():
        try:
            details_tab.first.click(timeout=3000)
            _hs(0.5, 1.0)
        except Exception:
            pass

    editor = page.locator("div[contenteditable='true']").first
    _wait_visible(page, editor)

    current_text = (editor.text_content() or "").strip()
    target_text = description_text.strip()
    filename_stem = video_path.stem

    if current_text == target_text:
        logger.info("[TikTok] Step 3 → Description already filled with target text, skipping...")
        return
    elif target_text in current_text and len(current_text) > len(filename_stem) + 5:
        logger.info("[TikTok] Step 3 → Description already contains target text, skipping...")
        return

    logger.info(
        "[TikTok] Description current text: '%s'. Clearing and typing target...",
        current_text
    )
    editor.click()
    _hs(0.3, 0.6)
    page.keyboard.press("Control+A")
    _hs(0.2, 0.4)
    page.keyboard.press("Backspace")
    _hs(0.2, 0.4)

    page.keyboard.type(target_text, delay=60)
    logger.info("[TikTok] Step 3 ✓ Description typed.")
    _hs(0.5, 1.0)


def _step_upload_thumbnail(page: Page, thumbnail_path: Path) -> None:
    """Step 4 — Upload a custom cover image."""
    logger.info("[TikTok] Step 4 → Uploading cover: %s", thumbnail_path.name)
    if not thumbnail_path.exists():
        raise FileNotFoundError(f"Thumbnail file not found: {thumbnail_path}")

    cover_tab = page.get_by_text("Cover", exact=True)
    if cover_tab.count() > 0 and cover_tab.first.is_visible():
        try:
            cover_tab.first.evaluate("el => el.scrollIntoView({block: 'center', inline: 'nearest'})")
            _hs(0.2, 0.4)
            cover_tab.first.click(force=True)
            _hs(0.5, 1.0)
        except Exception:
            try:
                cover_tab.first.evaluate("el => el.click()")
            except Exception:
                pass

    edit_cover = page.get_by_text("Edit cover")
    if edit_cover.count() == 0 or not edit_cover.first.is_visible():
        logger.info("[TikTok] Step 4 → 'Edit cover' button not visible. Assuming cover already uploaded/customized.")
        return

    _hs(0.3, 0.8)
    try:
        edit_cover.first.evaluate("el => el.scrollIntoView({block: 'center', inline: 'nearest'})")
        _hs(0.2, 0.4)
        edit_cover.first.click(force=True)
    except Exception:
        try:
            edit_cover.first.evaluate("el => el.click()")
        except Exception:
            pass
    _hs(1.0, 2.0)

    upload_btn = page.get_by_role("button", name="Upload cover image")
    if upload_btn.count() > 0 and upload_btn.first.is_visible():
        with page.expect_file_chooser(timeout=20_000) as fc_info:
            upload_btn.first.click()
        fc_info.value.set_files(str(thumbnail_path))
        logger.info("[TikTok] Cover file submitted.")
        _hs(1.5, 3.0)

        save_btn = page.get_by_role("button", name="Save")
        save_btn.wait_for(state="visible", timeout=10000)
        
        # Save button is located in a fixed-position top header inside the modal.
        # Direct scrollIntoView might push it off-screen; click via JS evaluation for stability.
        logger.info("[TikTok] Clicking Save button via JS click...")
        try:
            save_btn.first.evaluate("el => el.click()")
        except Exception as exc:
            logger.warning("[TikTok] JS click failed: %s. Trying standard forced click fallback...", exc)
            save_btn.first.click(force=True)

        # Wait for cover modal to close
        save_btn.wait_for(state="hidden", timeout=10000)
        logger.info("[TikTok] Step 4 ✓ Cover uploaded and saved.")
        _hs(0.5, 1.0)
    else:
        logger.info("[TikTok] Cover upload button not found in modal. Clicking Cancel to close cover modal...")
        cancel_btn = page.get_by_role("button", name="Cancel")
        if cancel_btn.count() > 0 and cancel_btn.first.is_visible():
            cancel_btn.first.click(force=True)
        else:
            page.locator("text=Cancel").first.click(force=True)
        _hs(0.5, 1.0)


def _step_enable_scheduling(page: Page) -> None:
    """Step 5 — Enable Schedule switch option and handle safety popup modal."""
    logger.info("[TikTok] Step 5 → Enabling schedule…")

    # Scroll to reveal Settings and Schedule options
    page.evaluate("window.scrollBy(0, 800)")
    _hs(0.5, 1.0)

    # Search forchecked-false Schedule radio next to "Schedule" text
    schedule_radio = page.locator(".Radio__innerCircle.Radio__innerCircle--checked-false").first
    if schedule_radio.count() > 0 and schedule_radio.is_visible():
        schedule_radio.click()
        logger.info("[TikTok] Schedule radio checked.")
        _hs(1.0, 2.0)
    else:
        logger.info("[TikTok] Schedule radio is already enabled or not found.")

    # Optional safety modal check
    try:
        allow_btn = page.get_by_role("button", name="Allow")
        allow_btn.wait_for(state="visible", timeout=10000)
        logger.info("[TikTok] Optional safety modal 'Allow your video to be saved...' popped up. Clicking Allow.")
        allow_btn.click()
        _hs(0.5, 1.5)
    except PlaywrightTimeout:
        logger.info("[TikTok] Optional safety modal did not appear. Proceeding.")


def _step_set_date(page: Page, scheduled_time: datetime) -> None:
    """Step 6 — Set scheduled date on calendar daypicker."""
    day_str = str(scheduled_time.day)
    logger.info("[TikTok] Step 6 → Setting scheduled date to day: %s…", day_str)

    date_input = page.get_by_role("textbox").nth(2)
    _wait_visible(page, date_input)
    _hs(0.4, 0.8)
    date_input.click()
    _hs(0.6, 1.2)

    day_locators = page.get_by_text(day_str, exact=True)
    if day_locators.count() > 1:
        day_btn = day_locators.nth(1)
    else:
        day_btn = day_locators.first

    _wait_visible(page, day_btn)
    day_btn.click()
    logger.info("[TikTok] Step 6 ✓ Selected date day %s.", day_str)
    _hs(0.5, 1.0)


def _step_set_time(page: Page, scheduled_time: datetime) -> None:
    """Step 7 — Set scheduled time by clicking the hour and minute options directly in the popover."""
    hour_str = scheduled_time.strftime("%H")
    minute_str = scheduled_time.strftime("%M")
    desired_time = f"{hour_str}:{minute_str}"
    logger.info("[TikTok] Step 7 → Setting scheduled time to %s…", desired_time)

    # Locate the time input textbox (typically the second textbox)
    time_input = page.get_by_role("textbox").nth(1)
    _wait_visible(page, time_input)
    _hs(0.4, 0.8)

    # Click the time input to open the popover
    logger.info("[TikTok] Clicking time input to open picker...")
    time_input.click()
    _hs(1.0, 1.5)

    # Click the target hour element
    logger.info("[TikTok] Selecting hour '%s'...", hour_str)
    hour_el = page.locator(".tiktok-timepicker-left").get_by_text(hour_str, exact=True).first
    _wait_visible(page, hour_el)
    hour_el.scroll_into_view_if_needed()
    _hs(0.2, 0.4)
    hour_el.click(force=True)
    _hs(0.4, 0.8)

    # Click the target minute element
    logger.info("[TikTok] Selecting minute '%s'...", minute_str)
    min_el = page.locator(".tiktok-timepicker-right").get_by_text(minute_str, exact=True).first
    _wait_visible(page, min_el)
    min_el.scroll_into_view_if_needed()
    _hs(0.2, 0.4)
    min_el.click(force=True)
    _hs(0.5, 1.0)

    # Click outside to close the popover
    logger.info("[TikTok] Closing time picker popover...")
    outside_anchor = page.locator("text=Who can see this post").first
    if outside_anchor.count() > 0 and outside_anchor.is_visible():
        outside_anchor.click(force=True)
    else:
        page.keyboard.press("Escape")
    _hs(1.0, 1.5)

    # Verify time is set correctly
    current_val = (time_input.input_value() or "").strip()
    logger.info("[TikTok] Time input value reads: '%s'", current_val)
    if current_val != desired_time:
        logger.warning(
            "[TikTok] Time input value mismatch (expected: '%s', got: '%s'). Retrying popover selection...",
            desired_time, current_val
        )
        time_input.click()
        _hs(1.0, 1.5)
        
        hour_el.scroll_into_view_if_needed()
        hour_el.click(force=True)
        _hs(0.4, 0.8)
        
        min_el.scroll_into_view_if_needed()
        min_el.click(force=True)
        _hs(0.5, 1.0)
        
        if outside_anchor.count() > 0 and outside_anchor.is_visible():
            outside_anchor.click(force=True)
        else:
            page.keyboard.press("Escape")
        _hs(1.0, 1.5)
        
        current_val = (time_input.input_value() or "").strip()
        logger.info("[TikTok] Time input value reads after retry: '%s'", current_val)

    if current_val != desired_time:
        raise RuntimeError(f"Failed to set TikTok scheduled time to {desired_time}")

    logger.info("[TikTok] Step 7 ✓ Time set successfully to: %s", desired_time)


def _step_final_confirm(page: Page) -> bool:
    """Step 8 — Click Schedule button and verify success."""
    logger.info("[TikTok] Step 8 → Clicking final Schedule confirmation…")
    schedule_btn = page.get_by_role("button", name="Schedule")
    _wait_visible(page, schedule_btn)
    _hs(0.5, 1.0)
    schedule_btn.click()
    _hs(2.0, 4.0)

    # Monitor URL transition or success message (typically redirects away from /upload page or indicates processing)
    start_time = time.time()
    while time.time() - start_time < 15:
        if "upload" not in page.url:
            logger.info("[TikTok] Step 8 ✓ Successfully scheduled. URL transitioned to: %s", page.url)
            return True
        time.sleep(1)

    content = page.content().lower()
    if "scheduled" in content or "success" in content or "posts" in content:
        logger.info("[TikTok] Step 8 ✓ Successfully scheduled (Found success indicators in page content).")
        return True

    raise RuntimeError("TikTok final scheduling confirmation could not be verified.")


# ══════════════════════════════════════════════════════════════════════════════
# TikTokUploader class
# ══════════════════════════════════════════════════════════════════════════════

class TikTokUploader(BaseUploader):
    """
    Concrete uploader for TikTok Creator Studio.
    Inherits from BaseUploader.
    """

    PLATFORM_NAME = "TikTok"

    def __init__(self, lang: str, thumbnail_path: Path | None = None):
        super().__init__(lang)
        self.thumbnail_path = thumbnail_path

    def verify_channel(self, page: Page) -> bool:
        """Verify TikTok Studio page is loaded correctly."""
        if "tiktok" in page.url:
            self.logger.info("✓ Correct TikTok page confirmed: %s", page.url)
            return True
        self.logger.error("Active page is NOT TikTok. URL: %s", page.url)
        return False

    def upload(
        self,
        video_path: Path,
        metadata: dict,
        scheduled_time: datetime,
        dry_run: bool = False,
    ) -> bool:
        """Execute the full TikTok Creator Studio upload pipeline."""
        if dry_run:
            self.logger.info(
                "[DRY-RUN] Would upload '%s' to TikTok | Scheduled: %s",
                video_path.name,
                scheduled_time.strftime("%Y-%m-%d %H:%M %Z"),
            )
            return True

        if not video_path.exists():
            self.logger.error("Video file not found: %s", video_path)
            return False

        description = metadata.get("description", "")
        if not description:
            self.logger.warning("No description provided — uploading without caption.")

        self.logger.info(
            "=== Starting TikTok upload | lang=%s | file=%s | scheduled=%s ===",
            self.lang,
            video_path.name,
            scheduled_time.strftime("%Y-%m-%d %H:%M %Z"),
        )

        browser_data = None
        browser: Browser | None = None

        try:
            # 1. Start BitBrowser profile and get CDP endpoint
            browser_data = start_browser(self.lang)
            cdp_endpoint = browser_data["ws"]["puppeteer"]

            with sync_playwright() as playwright:
                # 2. Attach Playwright to the running BitBrowser window
                browser = connect_playwright_to_browser(playwright, cdp_endpoint)

                # 3. Get the TikTok Studio page
                page = _get_or_open_tiktok_page(browser)

                page.set_default_timeout(SELECTOR_TIMEOUT_MS)
                page.set_default_navigation_timeout(NAV_TIMEOUT_MS)

                # 4. Verify we are on correct site
                if not self.verify_channel(page):
                    raise RuntimeError("Active page does not match TikTok host. Aborting.")

                # 5. Run the 8-step upload pipeline
                _wait_for_composer_ready(page)
                was_uploaded = _step_upload_video(page, video_path)
                _step_wait_for_upload_complete(page, was_skipped=not was_uploaded)
                _step_fill_description(page, description, video_path)

                if self.thumbnail_path is not None:
                    _step_upload_thumbnail(page, self.thumbnail_path)
                else:
                    self.logger.info("[TikTok] No thumbnail cover provided — skipping.")

                _step_enable_scheduling(page)
                _step_set_date(page, scheduled_time)
                _step_set_time(page, scheduled_time)

                if _step_final_confirm(page):
                    self.logger.info("=== TikTok upload complete for '%s' ===", video_path.name)
                    return True

        except (RuntimeError, FileNotFoundError) as exc:
            self.logger.error("TikTok upload failed: %s", exc)
        except PlaywrightTimeout as exc:
            self.logger.error("Playwright timeout during TikTok upload: %s", exc)
        except Exception as exc:
            self.logger.exception("Unexpected error during TikTok upload: %s", exc)
        finally:
            # BitBrowser must remain running (do not stop or close)
            pass

        return False
