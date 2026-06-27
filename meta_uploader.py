"""
meta_uploader.py — Meta Business Suite Reels uploader (Phase 2).

Uploads and schedules a Reel to both Facebook and Instagram simultaneously
via Meta Business Suite (business.facebook.com) using Playwright + BitBrowser CDP.

Pipeline:
  1.  Connect to the running BitBrowser profile via CDP WebSocket.
  2.  Locate the existing business.facebook.com page (no navigation / refresh).
  3.  Upload the video file and wait until 100 % upload is confirmed.
  4.  Fill in the Reel description with human-like typing.
  5.  Upload a custom thumbnail image.
  6.  Click "Next" twice to reach the Share tab.
  7.  Select "Schedule" option.
  8.  Set the date & time for Facebook (using .first selectors).
  9.  Set the date & time for Instagram (using .nth(1) selectors).
  10. Click the final "Schedule" button to confirm.

Inherits from BaseUploader so it plugs directly into the main.py orchestrator.

SCALABILITY NOTE:
  Adding a new social platform later only requires:
    1. A new class that inherits BaseUploader.
    2. Zero changes to this file, config.py, or main.py.
"""

import logging
import re
import time
from datetime import datetime
from pathlib import Path

from playwright.sync_api import (
    Browser,
    Page,
    TimeoutError as PlaywrightTimeout,
    sync_playwright,
)

from adspower_utils import connect_playwright_to_browser, start_browser, stop_browser
from config import (
    NAV_TIMEOUT_MS,
    SELECTOR_TIMEOUT_MS,
    UPLOAD_TIMEOUT_MS,
)
from pipeline_config import META_COMPOSER_URL

from uploader_base import BaseUploader
from utils import human_sleep, set_file_via_cdp

logger = logging.getLogger(__name__)

META_BUSINESS_HOST = "business.facebook.com"

# ── Upload / selector timeouts ─────────────────────────────────────────────────
_VIDEO_UPLOAD_TIMEOUT_MS = 300_000   # 5 minutes max for 100 % confirmation
_STEP_TIMEOUT_MS         = 15_000    # general element wait
_CALENDAR_TIMEOUT_MS     = 8_000     # calendar popup interactions


# ══════════════════════════════════════════════════════════════════════════════
# Low-level page helpers
# ══════════════════════════════════════════════════════════════════════════════

def _get_or_open_meta_page(browser: Browser) -> Page:
    """
    Return the active Meta Business Suite page using the ultimate smart routing tab logic:

    Step 1 — Scan: Look through all currently open tabs/pages in the browser contexts.
             If a tab with URL containing 'latest/reels_composer' is found, bring it
             to the front and return it immediately without calling page.goto().
             
    Step 2 — Navigate: If no Meta Composer tab is found, search for any empty tab
             (URL 'about:blank') to navigate, or open a new page in the first context
             and navigate directly to META_COMPOSER_URL.
    """
    # ── Step 1: Scan for existing Reels Composer tabs ───────────────────────
    for ctx in browser.contexts:
        for page in ctx.pages:
            if "latest/reels_composer" in page.url:
                logger.info("[Meta] Reusing existing Meta Composer tab: %s", page.url)
                try:
                    page.bring_to_front()
                except Exception:
                    pass
                return page

    # ── Step 2: No Composer tab found — find an empty page to navigate or open new one ──
    logger.info(
        "[Meta] No Meta Composer tab found. Opening a tab and navigating to: %s", 
        META_COMPOSER_URL,
    )

    if not browser.contexts:
        raise RuntimeError(
            "[Meta] BitBrowser has no open contexts. "
            "Ensure the profile is running and signed in to Meta Business Suite."
        )

    # Search for an empty tab to reuse
    target_page = None
    ctx = browser.contexts[0]
    for page in ctx.pages:
        if page.url == "about:blank":
            target_page = page
            logger.info("[Meta] Reusing blank page for navigation.")
            break

    if not target_page:
        target_page = ctx.new_page()

    target_page.goto(
        META_COMPOSER_URL,
        wait_until="domcontentloaded",
        timeout=NAV_TIMEOUT_MS,
    )
    # Extra pause for React/JS hydration
    time.sleep(3)
    logger.info("[Meta] Composer page loaded. URL: %s", target_page.url)
    return target_page


def _hs(min_s: float = 0.5, max_s: float = 1.5) -> None:
    """Convenience wrapper for human_sleep used inside this module."""
    human_sleep(min_s, max_s)


def _wait_visible(page: Page, locator, timeout: int = _STEP_TIMEOUT_MS) -> None:
    """Wait until a locator is visible."""
    locator.wait_for(state="visible", timeout=timeout)


# ══════════════════════════════════════════════════════════════════════════════
# Individual upload steps
# ══════════════════════════════════════════════════════════════════════════════

def _wait_for_composer_ready(page: Page) -> None:
    """Wait until at least one composer screen landmark element is visible."""
    # Landmarks: 'Add Video' button, 'Schedule' option button, 'Next' button, or a video element
    landmarks = [
        page.get_by_role("button", name="Add Video"),
        page.get_by_role("button", name="Schedule"),
        page.get_by_role("button", name="Next"),
        page.locator("video")
    ]
    
    start_time = time.time()
    while time.time() - start_time < 10:
        for lm in landmarks:
            if lm.count() > 0 and lm.first.is_visible():
                return
        time.sleep(0.5)
    logger.warning("[Meta] Warning: No composer landmarks became visible within 10 seconds.")


def _is_past_upload_screen(page: Page) -> bool:
    """Check if the page is currently on a step past the initial video upload screen (e.g. Edit or Share tab)."""
    # If the "Schedule" option button is visible, we are on the Share tab
    schedule_btn = page.get_by_role("button", name="Schedule")
    if schedule_btn.count() > 0 and any(b.is_visible() for b in schedule_btn.all()):
        return True
    # If the "Next" button is visible and we are not on the upload screen (Add Video not visible)
    next_btn = page.get_by_role("button", name="Next")
    add_video_btn = page.get_by_role("button", name="Add Video")
    if next_btn.count() > 0 and any(b.is_visible() for b in next_btn.all()):
        if add_video_btn.count() == 0 or not add_video_btn.first.is_visible():
            return True
    return False


def _step_upload_video(page: Page, video_path: Path) -> bool:
    """
    Step 1 — Upload the video file via the file-chooser dialog.
    Returns True if video file was submitted, False if upload was skipped/resumed.
    """
    logger.info("[Meta] Step 1 → Uploading video: %s", video_path.name)
    if not video_path.exists():
        raise FileNotFoundError(f"Video file not found: {video_path}")

    # Check if we are already past the upload screen
    if _is_past_upload_screen(page):
        logger.info("[Meta] Step 1 → Already past upload screen. Video upload assumed complete, skipping...")
        return False

    # Check if video is already present
    if page.locator("video").count() > 0:
        logger.info("[Meta] Step 1 → Video element already present. Video already uploaded, skipping...")
        return False

    add_video_btn = page.get_by_role("button", name="Add Video")
    try:
        add_video_btn.wait_for(state="visible", timeout=5000)
    except PlaywrightTimeout:
        # If 'Add Video' button is missing after 5 seconds, let's see if video exists
        if page.locator("video").count() > 0:
            logger.info("[Meta] Step 1 → Video element found. Video already uploaded, skipping...")
            return False
        # Fallback wait if it's just slow to load
        logger.info("[Meta] Step 1 → 'Add Video' button not visible after 5s. Waiting up to 10s more...")
        _wait_visible(page, add_video_btn, timeout=10000)

    # Check if button is disabled (often true if video already uploaded/processing)
    if not add_video_btn.is_enabled():
        logger.info("[Meta] Step 1 → 'Add Video' button is disabled. Video already uploaded, skipping...")
        return False

    _hs(0.5, 1.0)

    with page.expect_file_chooser(timeout=20_000) as fc_info:
        add_video_btn.click()

    set_file_via_cdp(page, fc_info.value.element, video_path)
    logger.info("[Meta] Step 1 ✓ Video file submitted to file chooser.")
    return True


def _step_wait_for_upload_complete(page: Page, was_skipped: bool = False) -> None:
    """
    Step 2 — Poll until the upload progress indicator shows '100%'.
    This prevents proceeding before the video is fully received by Meta.
    """
    logger.info("[Meta] Step 2 → Waiting for video upload to reach 100%%…")

    # If the video upload was skipped/resumed and video element is already present,
    # the 100% progress text might have disappeared entirely.
    if was_skipped or _is_past_upload_screen(page):
        if _is_past_upload_screen(page):
            logger.info("[Meta] Step 2 ✓ Already past upload screen. Bypassing upload progress wait.")
            return
        if page.locator("video").count() > 0:
            logger.info("[Meta] Step 2 ✓ Video element is already present. Bypassing upload progress wait.")
            return

    start_time = time.time()
    while time.time() - start_time < _VIDEO_UPLOAD_TIMEOUT_MS / 1000:
        if _is_past_upload_screen(page):
            logger.info("[Meta] Step 2 ✓ Advanced past upload screen. Upload is complete.")
            return

        try:
            # Check for percentage text in visible elements
            pct_loc = page.get_by_text(re.compile(r"\b\d+%\b"))
            if pct_loc.count() > 0:
                for el in pct_loc.all():
                    try:
                        if el.is_visible():
                            text = el.inner_text().strip()
                            match = re.search(r"(\d+)%", text)
                            if match:
                                pct_val = int(match.group(1))
                                logger.info("[Meta] Video upload progress: %d%%", pct_val)
                                if pct_val == 100:
                                    logger.info("[Meta] Step 2 ✓ Video upload reached 100%%.")
                                    _hs(1.0, 2.0)
                                    return
                                break
                    except Exception:
                        pass
        except Exception as e:
            logger.debug("Error checking percentage: %s", e)

        # Fallback check: if no % text is visible but the video preview is rendered
        if page.locator("video").count() > 0 and page.locator("video").first.is_visible():
            logger.info("[Meta] Step 2 ✓ Video element/preview visible. Upload is complete.")
            return

        time.sleep(5)

    raise RuntimeError(
        f"Video upload did not complete within the allowed timeout ({_VIDEO_UPLOAD_TIMEOUT_MS // 1000}s)."
    )


def _step_fill_description(page: Page, description_text: str) -> None:
    """Step 3 — Type the Reel description into the description text box."""
    logger.info("[Meta] Step 3 → Filling description…")
    
    if _is_past_upload_screen(page):
        logger.info("[Meta] Step 3 → Already past upload screen. Description assumed filled, skipping...")
        return
    
    # Try locating the description box using the contenteditable textbox selector,
    # or fallback to the original placeholder has_text locator if needed.
    desc_box = page.locator("div[role='textbox'][contenteditable='true']").first
    if desc_box.count() == 0:
        desc_box = (
            page.locator("div")
            .filter(has_text=re.compile(r"^Let viewers know what your reel is about$"))
            .first
        )

    _wait_visible(page, desc_box)
    
    # Smart check: read text content and input value
    txt_content = (desc_box.text_content() or "").strip()
    val = ""
    try:
        val = (desc_box.input_value() or "").strip()
    except Exception:
        pass

    if txt_content or val:
        placeholder_texts = ["Let viewers know what your reel is about", "Scrivi qualcosa", "Write something"]
        is_placeholder = any(p.lower() in txt_content.lower() for p in placeholder_texts)
        
        if not is_placeholder:
            logger.info("[Meta] Step 3 → Description already filled (Content: '%s...'), skipping...", txt_content[:30])
            return

    _hs(0.4, 0.9)
    desc_box.click()
    _hs(0.3, 0.7)
    page.keyboard.type(description_text, delay=60)
    logger.info("[Meta] Step 3 ✓ Description typed.")
    _hs(0.5, 1.0)


def _step_upload_thumbnail(page: Page, thumbnail_path: Path) -> None:
    """Step 4 — Upload a custom thumbnail image."""
    logger.info("[Meta] Step 4 → Uploading thumbnail: %s", thumbnail_path.name)
    if not thumbnail_path.exists():
        raise FileNotFoundError(f"Thumbnail file not found: {thumbnail_path}")

    # Scroll down to reveal the thumbnail section if it's off-screen
    page.evaluate("window.scrollBy(0, 400)")
    _hs(0.5, 1.0)

    if _is_past_upload_screen(page):
        logger.info("[Meta] Step 4 → Already past upload screen. Thumbnail assumed uploaded, skipping...")
        return

    try:
        # Check if thumbnail is already uploaded/selected
        upload_image_btn = page.get_by_role("button", name="Upload image")
        
        has_custom_thumbnail = False
        if upload_image_btn.count() == 0 or not upload_image_btn.first.is_visible() or not upload_image_btn.first.is_enabled():
            has_custom_thumbnail = True
            
        # Check if there is an active/enabled Delete button near the custom thumbnail section
        delete_btn = page.get_by_role("button", name="Delete")
        if delete_btn.count() > 0 and any(b.is_visible() and b.is_enabled() for b in delete_btn.all()):
            has_custom_thumbnail = True

        if has_custom_thumbnail:
            logger.info("[Meta] Step 4 → Custom thumbnail already uploaded/selected, skipping...")
            return

        # Perform the upload
        try:
            _wait_visible(page, upload_image_btn, timeout=5000)
        except PlaywrightTimeout:
            # Extra scroll attempt if not yet visible
            page.evaluate("window.scrollBy(0, 600)")
            _hs(0.8, 1.2)
            _wait_visible(page, upload_image_btn, timeout=5000)

        _hs(0.3, 0.8)

        with page.expect_file_chooser(timeout=20_000) as fc_info:
            upload_image_btn.click()
            _hs(0.3, 0.7)
            # Click the "Upload image" link inside the popup that appears
            page.get_by_role("link", name="Upload image").click()

        set_file_via_cdp(page, fc_info.value.element, thumbnail_path)
        logger.info("[Meta] Step 4 ✓ Thumbnail submitted.")
        _hs(1.0, 2.0)

    except Exception as exc:
        logger.warning(
            "[Meta] Step 4 Warning → Thumbnail upload failed or skipped during retry: %s. "
            "Proceeding without crashing...", exc
        )


def _step_navigate_to_share_tab(page: Page) -> None:
    """Step 5 — Navigating to Share tab (Directly click tab or click Next)."""
    logger.info("[Meta] Step 5 → Navigating to Share tab…")
    
    # Check if already on the Share tab
    schedule_option = page.get_by_role("button", name="Schedule")
    if schedule_option.count() > 0 and schedule_option.first.is_visible():
        logger.info("[Meta] Step 5 → Already on Share tab, skipping navigation.")
        return

    # Try clicking the Share tab button directly
    share_tab_btn = page.get_by_role("button", name="Share pending status")
    try:
        if share_tab_btn.count() > 0 and share_tab_btn.first.is_visible():
            logger.info("[Meta] Step 5 → Clicking Share tab button directly…")
            share_tab_btn.first.click()
            _hs(1.5, 2.5)
            
            # Verify if we reached Share tab by checking Schedule button
            if schedule_option.count() > 0 and schedule_option.first.is_visible():
                logger.info("[Meta] Step 5 ✓ Navigated to Share tab directly.")
                return
    except Exception as exc:
        logger.warning("[Meta] Step 5 → Direct tab navigation failed: %s. Trying fallback...", exc)

    # Fallback: click Next twice (using .first to avoid strict mode error)
    logger.info("[Meta] Step 5 → Fallback: Click 'Next' twice to move past Edit tab to the Share tab…")
    next_btn = page.get_by_role("button", name="Next")

    # First Next click
    _wait_visible(page, next_btn)
    _hs(0.8, 1.5)
    next_btn.first.click()
    logger.info("[Meta] Step 5   → Clicked Next (1/2).")
    _hs(1.5, 2.5)

    # Second Next click
    _wait_visible(page, next_btn)
    _hs(0.8, 1.5)
    next_btn.first.click()
    logger.info("[Meta] Step 5 ✓ Clicked Next (2/2) — now on Share tab.")
    _hs(1.5, 2.5)


def _step_select_schedule(page: Page) -> None:
    """Step 6 — Click the 'Schedule' option on the Share tab."""
    logger.info("[Meta] Step 6 → Selecting 'Schedule' option…")
    schedule_btn = page.get_by_role("button", name="Schedule")
    _wait_visible(page, schedule_btn)

    # Check if Schedule option is already selected (date picker inputs visible)
    date_inputs = page.get_by_role("textbox", name="Select a future date and time")
    if date_inputs.count() > 0 and date_inputs.first.is_visible():
        logger.info("[Meta] Step 6 → 'Schedule' option is already selected (date inputs visible), skipping...")
        return

    _hs(0.5, 1.0)
    schedule_btn.click()
    logger.info("[Meta] Step 6 ✓ Schedule option selected.")
    _hs(1.0, 1.8)


def _navigate_calendar_to_month(page: Page, target_date: datetime, nth: int) -> None:
    """
    Helper — Advance the calendar popup to the correct month/year,
    clicking 'Next month' as many times as needed, then click the day button.

    Args:
        page:        Playwright Page.
        target_date: The datetime object representing the desired date.
        nth:         0 = use .first selectors (Facebook); 1 = use .nth(1) (Instagram).
    """
    # Expected button label: e.g. "Wednesday, 24 June"
    # Windows/cross-platform compatible formatting (no zero-pad day)
    day_num_no_pad = str(target_date.day)
    day_label = target_date.strftime("%A, ") + day_num_no_pad + target_date.strftime(" %B")

    logger.info("[Meta]   → Navigating calendar to: %s", day_label)

    max_month_clicks = 6  # safety cap — never click forward more than 6 months
    for attempt in range(max_month_clicks + 1):
        day_btn = page.get_by_role("button", name=day_label)
        if day_btn.count() > 0:
            try:
                day_btn.first.scroll_into_view_if_needed(timeout=2_000)
                _hs(0.3, 0.6)
                day_btn.first.click()
                logger.info("[Meta]   ✓ Clicked day button: %s", day_label)
                return
            except Exception:
                pass

        if attempt == max_month_clicks:
            raise RuntimeError(
                f"[Meta] Calendar day '{day_label}' not found after {max_month_clicks} "
                "month-forward clicks."
            )

        # Click "Next month" and wait for calendar to refresh
        logger.info("[Meta]   → Day not visible yet — clicking 'Next month'…")
        next_month_btn = page.get_by_role("button", name="Next month")
        _wait_visible(page, next_month_btn, timeout=_CALENDAR_TIMEOUT_MS)
        next_month_btn.click()
        _hs(0.5, 1.0)


def _set_datetime_for_platform(
    page: Page,
    target_date: datetime,
    nth: int,
) -> None:
    """
    Set the date and time pickers for one platform (Facebook or Instagram).

    Args:
        nth: 0 → .first (Facebook), 1 → .nth(1) (Instagram).
    """
    platform = "Facebook" if nth == 0 else "Instagram"
    logger.info("[Meta]   Setting date/time for %s…", platform)

    # ── Open date picker ─────────────────────────────────────────────────────
    date_input = page.get_by_role("textbox", name="Select a future date and time")
    specific_input = date_input.first if nth == 0 else date_input.nth(1)
    _wait_visible(page, specific_input, timeout=_STEP_TIMEOUT_MS)
    _hs(0.4, 0.8)
    specific_input.click(force=True)
    _hs(0.6, 1.2)

    # ── Navigate to correct month and click the day ───────────────────────────
    _navigate_calendar_to_month(page, target_date, nth)
    _hs(0.6, 1.0)

    # Forcefully press "Escape" to close the calendar popup to clear the overlay
    page.keyboard.press("Escape")
    _hs(0.4, 0.8)

    # ── Hours ─────────────────────────────────────────────────────────────────
    hour_12 = target_date.strftime("%I").lstrip("0") or "12"   # 12-hour, no leading zero
    logger.info("[Meta]   → Setting hour to: %s", hour_12)
    hours_spinner = page.get_by_role("spinbutton", name="hours")
    specific_hours = hours_spinner.first if nth == 0 else hours_spinner.nth(1)
    _wait_visible(page, specific_hours, timeout=_CALENDAR_TIMEOUT_MS)
    
    specific_hours.click(force=True)
    _hs(0.2, 0.4)
    page.keyboard.press("Control+A")
    _hs(0.1, 0.3)
    page.keyboard.press("Backspace")
    _hs(0.1, 0.3)
    page.keyboard.type(hour_12, delay=50)
    _hs(0.3, 0.6)

    # ── Minutes ───────────────────────────────────────────────────────────────
    minute_str = target_date.strftime("%M")
    logger.info("[Meta]   → Setting minutes to: %s", minute_str)
    minutes_spinner = page.get_by_role("spinbutton", name="minutes")
    specific_minutes = minutes_spinner.first if nth == 0 else minutes_spinner.nth(1)
    _wait_visible(page, specific_minutes, timeout=_CALENDAR_TIMEOUT_MS)
    
    specific_minutes.click(force=True)
    _hs(0.2, 0.4)
    page.keyboard.press("Control+A")
    _hs(0.1, 0.3)
    page.keyboard.press("Backspace")
    _hs(0.1, 0.3)
    page.keyboard.type(minute_str, delay=50)
    _hs(0.3, 0.6)

    # ── AM / PM meridiem ──────────────────────────────────────────────────────
    desired_meridiem = target_date.strftime("%p").upper()   # "AM" or "PM"
    logger.info("[Meta]   → Setting meridiem to: %s", desired_meridiem)
    meridiem_spinner = page.get_by_role("spinbutton", name="meridiem")
    specific_meridiem = meridiem_spinner.first if nth == 0 else meridiem_spinner.nth(1)
    _wait_visible(page, specific_meridiem, timeout=_CALENDAR_TIMEOUT_MS)
    
    specific_meridiem.click(force=True)
    _hs(0.2, 0.4)
    page.keyboard.press("Control+A")
    _hs(0.1, 0.3)
    page.keyboard.press("Backspace")
    _hs(0.1, 0.3)
    page.keyboard.type(desired_meridiem, delay=50)
    _hs(0.4, 0.8)

    # Double check and fallback to arrow key toggle if needed
    current = (specific_meridiem.input_value() or "").upper().strip()
    if current != desired_meridiem:
        logger.info("[Meta]   → Current meridiem is '%s' — trying arrow keys toggle…", current)
        specific_meridiem.press("ArrowUp")
        _hs(0.2, 0.4)
        current = (specific_meridiem.input_value() or "").upper().strip()
        if current != desired_meridiem:
            specific_meridiem.press("ArrowDown")
            _hs(0.2, 0.4)

    logger.info("[Meta]   ✓ Date/time set for %s.", platform)
    _hs(0.4, 0.8)


def _step_set_facebook_datetime(page: Page, target_date: datetime) -> None:
    """Step 7 — Set date and time for the Facebook Reel (uses .first selectors)."""
    logger.info("[Meta] Step 7 → Setting Facebook schedule date/time…")
    _set_datetime_for_platform(page, target_date, nth=0)
    logger.info("[Meta] Step 7 ✓ Facebook date/time set.")


def _step_set_instagram_datetime(page: Page, target_date: datetime) -> None:
    """Step 8 — Set date and time for the Instagram Reel (uses .nth(1) selectors)."""
    logger.info("[Meta] Step 8 → Setting Instagram schedule date/time…")
    _set_datetime_for_platform(page, target_date, nth=1)
    logger.info("[Meta] Step 8 ✓ Instagram date/time set.")


def _step_final_schedule(page: Page) -> None:
    """Step 9 — Scroll down and click the final 'Schedule' confirmation button."""
    logger.info("[Meta] Step 9 → Clicking final Schedule confirmation…")
    page.evaluate("window.scrollBy(0, 1000)")
    _hs(0.8, 1.5)
    # The second Schedule button confirms the scheduled post
    final_btn = page.get_by_role("button", name="Schedule").nth(1)
    
    try:
        _wait_visible(page, final_btn, timeout=5000)
    except PlaywrightTimeout:
        # If the button is not visible, check if we are no longer in the composer flow.
        if "reels_composer" not in page.url:
            logger.info("[Meta] Step 9 → URL has changed (not on composer page anymore). Scheduling assumed complete.")
            return
        else:
            # Try scrolling again and wait
            page.evaluate("window.scrollBy(0, 1000)")
            _hs(0.8, 1.5)
            _wait_visible(page, final_btn, timeout=5000)

    _hs(0.5, 1.0)
    final_btn.click()
    logger.info("[Meta] Step 9 ✓ Final Schedule button clicked.")
    _hs(2.0, 3.5)


# ══════════════════════════════════════════════════════════════════════════════
# MetaUploader class
# ══════════════════════════════════════════════════════════════════════════════

class MetaUploader(BaseUploader):
    """
    Concrete uploader for Meta Business Suite (Facebook + Instagram Reels)
    via BitBrowser + Playwright CDP.

    Inherits from BaseUploader and implements:
      - verify_channel(): confirms the correct Meta Business account is active.
      - upload():         runs the full 9-step Reels scheduling pipeline.

    Constructor Args (in addition to BaseUploader's lang):
        thumbnail_path: Optional path to a custom thumbnail image (.jpg/.png).
                        If None, the thumbnail step is skipped.
    """

    PLATFORM_NAME = "Meta"

    def __init__(self, lang: str, thumbnail_path: Path | None = None):
        super().__init__(lang)
        self.thumbnail_path = thumbnail_path

    # ── BaseUploader interface ─────────────────────────────────────────────────

    def verify_channel(self, page: Page) -> bool:
        """
        Confirm the correct Meta Business Suite page is loaded.
        For now, trusts that the BitBrowser profile is already logged in.
        A stricter check can query the active business_id from the URL.
        """
        if META_BUSINESS_HOST in page.url:
            self.logger.info(
                "✓ Correct Meta Business Suite page confirmed: %s", page.url
            )
            return True
        self.logger.error(
            "Active page is NOT Meta Business Suite. URL: %s", page.url
        )
        return False

    def upload(
        self,
        video_path: Path,
        metadata: dict,
        scheduled_time: datetime,
        dry_run: bool = False,
    ) -> bool:
        """
        Execute the full Meta Reels upload + schedule pipeline.

        Args:
            video_path:      Absolute Path to the .mp4 video file.
            metadata:        Dict with at least a "description" key (title ignored
                             on Meta — description is the caption).
            scheduled_time:  Timezone-aware datetime for when the Reel should publish.
            dry_run:         If True, skip all browser actions and return True.

        Returns:
            True on success, False on any failure.
        """
        if dry_run:
            self.logger.info(
                "[DRY-RUN] Would upload '%s' to Meta | Scheduled: %s",
                video_path.name,
                scheduled_time.strftime("%Y-%m-%d %H:%M %Z"),
            )
            return True

        if not video_path.exists():
            self.logger.error("Video file not found: %s", video_path)
            return False

        meta_meta = metadata.get("meta_reels") or metadata.get("short_form", {})
        caption = meta_meta.get("caption", "").strip()
        hashtags = " ".join(meta_meta.get("hashtags", []))
        description = f"{caption}\n\n{hashtags}".strip() if hashtags else caption
        if not description:
            self.logger.warning("No short_form description provided — uploading without caption.")

        self.logger.info(
            "=== Starting Meta upload | lang=%s | file=%s | scheduled=%s ===",
            self.lang,
            video_path.name,
            scheduled_time.strftime("%Y-%m-%d %H:%M %Z"),
        )

        browser_data = None
        browser: Browser | None = None

        try:
            # ── 1. Start the BitBrowser profile and get the CDP endpoint ─────
            browser_data  = start_browser(self.lang)
            cdp_endpoint  = browser_data["ws"]["puppeteer"]

            with sync_playwright() as playwright:
                # ── 2. Attach Playwright to the running BitBrowser window ────
                browser = connect_playwright_to_browser(playwright, cdp_endpoint)

                # ── 3. Get the Meta Business Suite page (auto-navigate if needed) ─
                page = _get_or_open_meta_page(browser)

                page.set_default_timeout(SELECTOR_TIMEOUT_MS)
                page.set_default_navigation_timeout(NAV_TIMEOUT_MS)

                # ── 4. Verify we are on the correct account ──────────────────
                if not self.verify_channel(page):
                    raise RuntimeError(
                        "Active Meta Business page does not match expected host. "
                        "Aborting to prevent posting to the wrong account."
                    )

                # ── 5. Run the 9-step upload pipeline ────────────────────────
                _wait_for_composer_ready(page)
                was_uploaded = _step_upload_video(page, video_path)
                _step_wait_for_upload_complete(page, was_skipped=not was_uploaded)
                _step_fill_description(page, description)

                if self.thumbnail_path is not None:
                    _step_upload_thumbnail(page, self.thumbnail_path)
                else:
                    self.logger.info(
                        "[Meta] No thumbnail provided — skipping thumbnail step."
                    )

                _step_navigate_to_share_tab(page)
                _step_select_schedule(page)
                _step_set_facebook_datetime(page, scheduled_time)
                _step_set_instagram_datetime(page, scheduled_time)
                _step_final_schedule(page)

                self.logger.info(
                    "=== Meta upload complete for '%s' ===", video_path.name
                )
                try:
                    page.close()
                except Exception:
                    pass
                return True

        except (RuntimeError, FileNotFoundError) as exc:
            self.logger.error("Meta upload failed: %s", exc)
        except PlaywrightTimeout as exc:
            self.logger.error("Playwright timeout during Meta upload: %s", exc)
        except Exception as exc:
            self.logger.exception("Unexpected error during Meta upload: %s", exc)
        finally:
            # NOTE: stop_browser is intentionally NOT called here.
            # The BitBrowser profile must remain running after the upload so the
            # user can inspect it and close it manually to conserve daily API limits.
            # stop_browser(self.lang)
            pass

        return False
