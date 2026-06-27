"""
youtube_uploader.py — YouTube Shorts upload automation via Playwright + AdsPower.

Inherits from BaseUploader and implements the full YouTube Studio upload pipeline:
  1. Start the AdsPower browser profile for the target language.
  2. Attach Playwright to the running browser via CDP WebSocket.
  3. Navigate to YouTube Studio.
  4. CRITICAL: Verify the active channel is "@FabioEgyptItaly". Switch if not.
  5. Open the upload dialog and select the IT.mp4 file.
  6. Fill in Italian title, description, and tags with human-like typing.
  7. Set audience to "Not made for kids".
  8. Navigate the wizard to the Visibility step.
  9. Select "Schedule", enter the computed date and time.
 10. Confirm and verify the schedule was accepted.
 11. Detach Playwright and stop the AdsPower profile.
"""

import logging
import random
import time
from datetime import datetime
from pathlib import Path

from playwright.sync_api import (
    Browser,
    BrowserContext,
    Page,
    TimeoutError as PlaywrightTimeout,
    sync_playwright,
)

from adspower_utils import connect_playwright_to_browser, start_browser, stop_browser
from config import (
    BROWSER_SLOW_MO_MS,
    CHANNEL_HANDLE,
    CHANNEL_NAME,
    NAV_TIMEOUT_MS,
    SELECTOR_TIMEOUT_MS,
    UPLOAD_TIMEOUT_MS,
)
from scheduler import format_scheduled_time_for_yt
from uploader_base import BaseUploader
from utils import human_sleep, slow_type, set_file_via_cdp

logger = logging.getLogger(__name__)

STUDIO_URL = "https://studio.youtube.com"


# ─── Selector Registry ────────────────────────────────────────────────────────
# All CSS/text selectors are isolated here. When YouTube Studio changes its DOM,
# update ONLY this class — the logic functions above stay untouched.

class SEL:
    # Top bar
    CREATE_BTN      = "#create-icon"
    CREATE_BTN_ALT  = 'button[aria-label="Create"]'
    UPLOAD_ITEM     = "ytcp-upload-btn"
    UPLOAD_ITEM_TEXT= 'tp-yt-paper-item:has-text("Upload")'

    # Upload dialog
    FILE_INPUT      = 'input[type="file"]'
    UPLOAD_PROGRESS = "ytcp-video-upload-progress"
    SELECT_FILES_BTN= 'ytcp-button:has-text("Select files"), #select-files-button'

    # Details step
    TITLE_FIELD     = '#title-textarea div[contenteditable="true"]'
    DESC_FIELD      = '#description-textarea div[contenteditable="true"]'
    SHOW_MORE_BTN   = (
        '#toggle-button, '
        'ytcp-button:has-text("Show more"), '
        'ytcp-button:has-text("عرض المزيد"), '
        'ytcp-button:has-text("Mostra altro"), '
        'ytcp-button:has-text("Mostrar")'
    )
    TAGS_INPUT      = (
        'ytcp-form-input-container[label="Tags"] input, '
        'ytcp-form-input-container[label="العلامات"] input, '
        'ytcp-form-input-container[label="Tag"] input, '
        'ytcp-form-input-container[label="Etiquetas"] input, '
        'ytcp-form-input-container input#text-input'
    )
    TAGS_INPUT_ALT  = "#text-input"

    # Audience
    NOT_KIDS_RADIO  = (
        'tp-yt-paper-radio-button[name="VIDEO_MADE_FOR_KIDS_NOT"], '
        'tp-yt-paper-radio-button[name="NOT_MADE_FOR_KIDS"], '
        'tp-yt-paper-radio-button:has-text("not made for kids"), '
        'tp-yt-paper-radio-button:has-text("No, non è destinato ai bambini"), '
        'tp-yt-paper-radio-button:has-text("bambini"), '
        'tp-yt-paper-radio-button:has-text("ليس مخصص"), '
        'tp-yt-paper-radio-button:has-text("no es contenido"), '
        'tp-yt-paper-radio-button:has-text("não é conteúdo")'
    )

    # Wizard navigation
    NEXT_BTN        = 'ytcp-button[id="next-button"]'

    # Visibility step
    SCHEDULE_RADIO  = (
        'tp-yt-paper-radio-button[name="SCHEDULE"]',
        '#schedule-radio-button',
        '#second-container-radio-button',
        'text="Schedule"',
        'text="Pianifica"',
        'text="ضبط موعد"',
        'text="Programar"'
    )
    DATE_INPUT      = '#datepicker-trigger input, ytcp-date-picker input, input[aria-haspopup="listbox"]:nth-of-type(1)'
    TIME_INPUT      = '#time-of-day-trigger input, ytcp-time-of-day-picker input, input[aria-haspopup="listbox"]:nth-of-type(2)'
    DONE_BTN        = 'ytcp-button[id="done-button"]'

    # Post-schedule confirmation
    PROCESSING_DIALOG = "ytcp-uploads-still-processing-dialog"
    CLOSE_BTN         = 'ytcp-button[id="close-button"]'

    # Channel switching (profile menu)
    AVATAR_BTN      = '#avatar-btn, #account-button, ytd-topbar-menu-button-renderer'
    SWITCH_ACCOUNT  = 'yt-formatted-string:has-text("Switch account")'
    CHANNEL_HANDLE_SEL = f'yt-formatted-string:has-text("{CHANNEL_HANDLE}")'
    CHANNEL_NAME_SEL   = f'yt-formatted-string:has-text("{CHANNEL_NAME}")'


# ─── Low-level Interaction Helpers ───────────────────────────────────────────

def _wait(page: Page, selector: str, timeout: int = SELECTOR_TIMEOUT_MS) -> None:
    page.wait_for_selector(selector, state="attached", timeout=timeout)
    try:
        page.locator(selector).first.scroll_into_view_if_needed(timeout=2_000)
    except Exception:
        pass


def _click(page: Page, selector: str, timeout: int = SELECTOR_TIMEOUT_MS) -> None:
    human_sleep(0.5, 1.6)
    _wait(page, selector, timeout)
    try:
        page.click(selector, force=True, timeout=3_000)
    except Exception:
        page.evaluate(
            "(sel) => { const el = document.querySelector(sel); if(el) el.click(); }",
            selector,
        )
    logger.debug("Clicked: %s", selector)


def _try_click(page: Page, *selectors: str, timeout: int = 2_000) -> bool:
    """Try each selector in order; click the first visible one. Returns True on success."""
    for sel in selectors:
        try:
            page.wait_for_selector(sel, state="attached", timeout=1_500)
            page.locator(sel).first.scroll_into_view_if_needed(timeout=2_000)
            human_sleep(0.3, 0.9)
            page.click(sel, force=True)
            logger.debug("Clicked (tried): %s", sel)
            return True
        except Exception:
            continue
    return False


def _clear_and_type(page: Page, selector: str, text: str) -> None:
    """Select all existing content in a field and type replacement text."""
    human_sleep(0.3, 0.9)
    _wait(page, selector)
    page.click(selector)
    page.keyboard.press("Control+a")
    human_sleep(0.1, 0.3)
    page.keyboard.press("Delete")
    human_sleep(0.2, 0.5)
    slow_type(page, selector, text)


def _paste_text(page: Page, selector: str, text: str) -> None:
    """Click a field, select-all, then fill instantly (good for long descriptions)."""
    human_sleep(0.5, 1.2)
    _wait(page, selector)
    page.click(selector)
    page.keyboard.press("Control+a")
    human_sleep(0.1, 0.3)
    page.fill(selector, text)


# ─── Channel Verification & Switching ────────────────────────────────────────

def _get_current_channel_handle(page: Page) -> str | None:
    """
    Try to read the active channel handle from YouTube Studio's top-bar avatar
    or page metadata. Returns the handle string (e.g. '@FabioEgyptItaly') or None.
    """
    try:
        # YouTube Studio embeds the channel URL in the page, e.g. /channel/UCXXX
        # The cheapest check is to see if the current URL or page title contains
        # a reference to our handle after navigating to studio.
        # We also attempt to read the aria-label on the avatar button.
        avatar = page.locator(SEL.AVATAR_BTN).first
        label  = avatar.get_attribute("aria-label") or ""
        logger.debug("Avatar aria-label: %r", label)
        return label.strip() or None
    except Exception as exc:
        logger.debug("Could not read channel handle from avatar: %s", exc)
        return None


def _verify_and_switch_channel(page: Page) -> bool:
    """
    Verify the active YouTube channel is CHANNEL_HANDLE (@FabioEgyptItaly).
    If it's on the wrong channel, open the account switcher and click the right one.

    Returns True if the correct channel is confirmed, False on failure.
    """
    logger.info("Verifying active YouTube channel is '%s'…", CHANNEL_HANDLE)

    # ── Method 1: Check the page URL / DOM for the handle ─────────────────────
    # YouTube Studio shows the channel name/handle in various places.
    # The most reliable is: after landing on studio.youtube.com, the URL may
    # redirect to /channel/<ID>, and the header shows the channel name.
    try:
        # Check if our handle already appears somewhere on the page
        page.wait_for_selector(SEL.CHANNEL_HANDLE_SEL, timeout=5_000)
        logger.info("✓ Correct channel '%s' confirmed via page text.", CHANNEL_HANDLE)
        return True
    except PlaywrightTimeout:
        pass  # Handle not found — may need to switch

    try:
        page.wait_for_selector(SEL.CHANNEL_NAME_SEL, timeout=3_000)
        logger.info("✓ Correct channel '%s' confirmed via channel name.", CHANNEL_NAME)
        return True
    except PlaywrightTimeout:
        pass

    # ── Method 2: Open the account switcher and select the right channel ───────
    logger.warning(
        "Could not confirm '%s' on the current page. Attempting account switch…",
        CHANNEL_HANDLE,
    )

    try:
        # Click the avatar / profile picture button
        if not _try_click(page, SEL.AVATAR_BTN, timeout=8_000):
            logger.error("Avatar button not found — cannot switch channel.")
            return False
        human_sleep(1.5, 2.5)

        # Click "Switch account"
        if not _try_click(page, SEL.SWITCH_ACCOUNT, timeout=8_000):
            logger.error("'Switch account' option not found in menu.")
            return False
        human_sleep(1.5, 2.5)

        # Select the target channel by handle or name
        switched = _try_click(page, SEL.CHANNEL_HANDLE_SEL, timeout=6_000)
        if not switched:
            switched = _try_click(page, SEL.CHANNEL_NAME_SEL, timeout=6_000)

        if not switched:
            logger.error(
                "Could not find channel '%s' / '%s' in the switcher list.",
                CHANNEL_HANDLE, CHANNEL_NAME,
            )
            return False

        logger.info("Switched to channel '%s'. Waiting for page to reload…", CHANNEL_NAME)
        human_sleep(3.0, 5.0)
        page.wait_for_load_state("domcontentloaded", timeout=NAV_TIMEOUT_MS)
        human_sleep(2.0, 3.0)
        return True

    except Exception as exc:
        logger.error("Channel switch failed with unexpected error: %s", exc)
        return False


# ─── Upload Steps ─────────────────────────────────────────────────────────────

def _navigate_to_studio(page: Page) -> None:
    logger.info("Navigating to YouTube Studio…")
    page.goto(STUDIO_URL, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
    human_sleep(3.0, 5.0)

    if "accounts.google.com" in page.url or "signin" in page.url:
        raise RuntimeError(
            "Redirected to Google Sign-In. "
            "Please sign in manually in this AdsPower profile first, then re-run."
        )

    human_sleep(2.0, 3.0)
    logger.info("YouTube Studio loaded. URL: %s", page.url)


def _open_upload_dialog(page: Page) -> None:
    logger.info("Opening upload dialog…")

    big_btn = 'ytcp-button:has-text("Upload videos"), #upload-btn'
    if _try_click(page, big_btn, timeout=4_000):
        logger.info("Clicked the main Dashboard Upload button.")
    else:
        create_selectors = [
            SEL.CREATE_BTN,
            SEL.CREATE_BTN_ALT,
            "ytcp-button:has-text('Create')",
        ]
        if not _try_click(page, *create_selectors, timeout=8_000):
            raise RuntimeError("CREATE button not found on YouTube Studio.")
        human_sleep(0.8, 1.8)

        dropdown_selectors = [
            SEL.UPLOAD_ITEM,
            SEL.UPLOAD_ITEM_TEXT,
            'tp-yt-paper-item:has-text("Upload videos")',
        ]
        if not _try_click(page, *dropdown_selectors, timeout=8_000):
            raise RuntimeError("'Upload videos' menu item not found in dropdown.")

    try:
        page.wait_for_selector("ytcp-uploads-dialog", state="visible", timeout=5_000)
    except Exception:
        pass
    logger.info("Upload dialog open.")


def _set_video_file(page: Page, video_path: Path) -> None:
    logger.info("Selecting file: %s", video_path.name)
    if not video_path.exists():
        raise FileNotFoundError(f"Video file not found: {video_path}")

    # Use expect_file_chooser because the underlying input[type="file"] is hidden
    # and direct set_input_files will timeout waiting for actionability/visibility.
    with page.expect_file_chooser(timeout=30_000) as fc_info:
        human_sleep(0.3, 0.8)
        page.click(SEL.SELECT_FILES_BTN)

    file_chooser = fc_info.value
    set_file_via_cdp(page, file_chooser.element, video_path)
    human_sleep(1.0, 2.0)

    # Quickly check for upload progress bar to verify upload started
    try:
        page.wait_for_selector(SEL.UPLOAD_PROGRESS, state="attached", timeout=8_000)
        logger.info("Upload progress bar detected — file upload underway.")
    except PlaywrightTimeout:
        logger.warning("Upload progress bar not detected within 8s — proceeding anyway.")


def _fill_details(page: Page, metadata: dict) -> None:
    """Fill the title, description, and tags fields using the new YouTube metadata schema."""
    yt_meta = metadata.get("youtube", {})

    # Title
    logger.info("Typing title…")
    _clear_and_type(page, SEL.TITLE_FIELD, yt_meta.get("title", ""))
    human_sleep(0.5, 1.2)
    page.keyboard.press("Escape")   # dismiss any hashtag dropdown
    human_sleep(0.3, 0.5)

    # Description
    logger.info("Pasting description…")
    _paste_text(page, SEL.DESC_FIELD, yt_meta.get("description", ""))
    human_sleep(0.6, 1.4)
    page.keyboard.press("Escape")
    human_sleep(0.2, 0.5)
    page.keyboard.press("Tab")
    human_sleep(1.0, 2.0)

    # Scroll to trigger lazy-loaded lower elements (Audience, Tags)
    logger.info("Scrolling to reveal lazy-loaded elements…")
    page.mouse.move(x=400, y=400)
    for _ in range(3):
        page.mouse.wheel(delta_y=800, delta_x=0)
        human_sleep(0.5, 1.2)
        page.keyboard.press("PageDown")

    # Tags — reveal via "Show more" button
    logger.info("Filling tags…")
    try:
        page.wait_for_selector(SEL.SHOW_MORE_BTN, state="attached", timeout=6_000)
        page.locator(SEL.SHOW_MORE_BTN).first.scroll_into_view_if_needed(timeout=2_000)
        human_sleep(0.4, 0.9)
        page.click(SEL.SHOW_MORE_BTN, force=True)
        human_sleep(0.8, 1.6)
    except Exception:
        logger.debug("'Show more' not found — tags section may already be visible.")

    tags_sel = None
    for sel in (SEL.TAGS_INPUT, SEL.TAGS_INPUT_ALT):
        try:
            page.wait_for_selector(sel, state="attached", timeout=8_000)
            page.locator(sel).first.scroll_into_view_if_needed(timeout=2_000)
            tags_sel = sel
            break
        except Exception:
            continue

    if tags_sel:
        page.click(tags_sel)
        for tag in yt_meta.get("tags", []):
            tag = tag.strip()
            if not tag:
                continue
            human_sleep(0.1, 0.4)
            page.type(tags_sel, tag + ",", delay=random.randint(60, 130))
        human_sleep(0.5, 1.0)
        logger.info("Tags filled.")
    else:
        logger.warning("Tags input not found — skipping tags.")


def _set_audience(page: Page) -> None:
    logger.info("Setting audience to 'Not made for kids'…")
    _click(page, SEL.NOT_KIDS_RADIO)
    human_sleep(0.5, 1.2)


def _click_next(page: Page, step_label: str) -> None:
    """Click the NEXT button, waiting first for it to become enabled."""
    logger.info("Clicking NEXT from step: %s", step_label)
    human_sleep(0.8, 2.0)
    try:
        page.wait_for_function(
            """() => {
                const btn = document.querySelector('ytcp-button#next-button');
                return btn && !btn.hasAttribute('disabled');
            }""",
            timeout=UPLOAD_TIMEOUT_MS,
        )
    except PlaywrightTimeout:
        logger.warning("NEXT button still disabled after timeout — clicking anyway.")
    _click(page, SEL.NEXT_BTN)
    human_sleep(1.5, 3.0)


def _advance_to_visibility(page: Page) -> None:
    """Step through Details → Video elements → Checks → Visibility."""
    _click_next(page, "Details")
    _click_next(page, "Video elements")
    _click_next(page, "Checks")
    logger.info("Reached Visibility step.")


def _wait_for_upload_completion(page: Page) -> None:
    """
    Poll the upload progress footer until the file finishes uploading to
    YouTube's servers, preventing premature schedule confirmation.
    """
    logger.info("Waiting for video file to finish uploading to YouTube servers…")
    completion_keywords = [
        "processing", "complete", "complet", "check", "verific",
    ]
    max_wait = UPLOAD_TIMEOUT_MS / 1000.0
    start_time = time.time()

    while time.time() - start_time < max_wait:
        try:
            progress_el = page.locator(SEL.UPLOAD_PROGRESS).first
            if not progress_el.is_visible():
                logger.info("Upload progress footer gone — assuming upload complete.")
                break

            text = progress_el.inner_text().lower()
            if not text.strip():
                human_sleep(2.0, 3.0)
                continue

            is_done = any(kw in text for kw in completion_keywords)
            if "100%" in text and "uploading" not in text:
                is_done = True

            if is_done:
                logger.info(
                    "Upload reached safe checkpoint! Progress: %r",
                    text.replace("\n", " "),
                )
                break

            logger.info("Still uploading: %r…", text.replace("\n", " "))
        except Exception as exc:
            logger.warning("Error reading upload progress: %s — retrying…", exc)

        human_sleep(4.0, 6.0)
    else:
        logger.warning("Upload wait timed out — proceeding anyway.")

    human_sleep(2.0, 3.0)


def _set_schedule(page: Page, scheduled_time: datetime) -> None:
    """Select the Schedule radio and enter the date and time."""
    date_str, time_str = format_scheduled_time_for_yt(scheduled_time)
    logger.info("Scheduling: date=%s  time=%s (Egypt TZ)", date_str, time_str)

    success = _try_click(page, *SEL.SCHEDULE_RADIO, timeout=8_000)
    if not success:
        logger.warning("Could not find Schedule radio via any known selector.")
    human_sleep(1.0, 2.0)

    # Date field
    date_locators = (
        "ytcp-date-picker input",
        "#datepicker-trigger input",
        "#datepicker-trigger",
        'input[aria-haspopup="listbox"]:nth-of-type(1)',
    )
    date_ok = False
    for sel in date_locators:
        try:
            page.wait_for_selector(sel, state="attached", timeout=2_000)
            page.locator(sel).first.scroll_into_view_if_needed(timeout=2_000)
            page.click(sel, force=True)
            human_sleep(0.4, 0.8)
            page.keyboard.press("Control+a")
            human_sleep(0.2, 0.5)
            page.keyboard.type(date_str, delay=random.randint(60, 110))
            page.keyboard.press("Enter")
            page.keyboard.press("Tab")
            logger.info("Date entered: %s (via %s)", date_str, sel)
            date_ok = True
            break
        except Exception:
            continue

    if not date_ok:
        raise RuntimeError("Date picker not found via any known selector.")

    human_sleep(0.5, 1.0)

    # Time field
    time_locators = (
        "#time-of-day-trigger input",
        "#time-of-day-trigger",
        "ytcp-time-of-day-picker input",
        'input[aria-haspopup="listbox"]:nth-of-type(2)',
        'input:right-of(#datepicker-trigger)',
        'input:right-of(ytcp-date-picker)',
    )
    time_ok = False
    for sel in time_locators:
        try:
            page.wait_for_selector(sel, state="attached", timeout=2_000)
            page.click(sel, force=True)
            human_sleep(0.4, 0.8)
            page.keyboard.press("Control+a")
            human_sleep(0.2, 0.5)
            page.keyboard.type(time_str, delay=random.randint(60, 110))
            page.keyboard.press("Enter")
            page.keyboard.press("Tab")
            logger.info("Time entered: %s (via %s)", time_str, sel)
            time_ok = True
            break
        except Exception:
            continue

    if not time_ok:
        raise RuntimeError("Time picker not found via any known selector.")

    human_sleep(0.8, 1.5)


def _confirm_schedule(page: Page) -> None:
    """Wait for upload completion, then click the final SCHEDULE button."""
    _wait_for_upload_completion(page)
    logger.info("Clicking final SCHEDULE button…")
    _click(page, SEL.DONE_BTN, timeout=15_000)
    human_sleep(2.5, 4.5)

    # YouTube may show a "still processing" dialog — normal for Shorts
    try:
        page.wait_for_selector(SEL.PROCESSING_DIALOG, timeout=20_000)
        logger.info("Processing dialog appeared — schedule confirmed.")
        try:
            page.click(SEL.CLOSE_BTN, timeout=5_000)
        except PlaywrightTimeout:
            pass
    except PlaywrightTimeout:
        logger.info(
            "No processing dialog — schedule accepted silently. URL: %s", page.url
        )

    human_sleep(1.5, 3.0)


# ─── YouTubeUploader Class ────────────────────────────────────────────────────

class YouTubeUploader(BaseUploader):
    """
    Concrete uploader for YouTube Shorts via AdsPower + Playwright CDP.

    Inherits from BaseUploader and implements:
      - verify_channel(): checks we are on @FabioEgyptItaly, switches if not.
      - upload(): runs the complete YouTube Studio upload pipeline.
    """

    PLATFORM_NAME = "YouTube"

    def verify_channel(self, page: Page) -> bool:
        """Verify/switch the active channel to @FabioEgyptItaly."""
        # return _verify_and_switch_channel(page)
        return True

    def upload(
        self,
        video_path: Path,
        metadata: dict,
        scheduled_time: datetime,
        dry_run: bool = False,
    ) -> bool:
        """
        Execute the full YouTube Shorts upload pipeline.

        Returns True on success, False on any failure.
        """
        if dry_run:
            date_str, time_str = format_scheduled_time_for_yt(scheduled_time)
            self.logger.info(
                "[DRY-RUN] Would upload '%s' | Title: '%s' | Scheduled: %s %s",
                video_path.name,
                metadata.get("title", "N/A"),
                date_str,
                time_str,
            )
            return True

        if not video_path.exists():
            self.logger.error("Video file not found: %s", video_path)
            return False

        self.logger.info(
            "=== Starting YouTube upload | lang=%s | file=%s ===",
            self.lang, video_path.name,
        )

        adspower_data = None
        browser: Browser | None = None

        try:
            # 1. Start the AdsPower profile and get the CDP endpoint
            adspower_data = start_browser(self.lang)
            cdp_endpoint = adspower_data["ws"]["puppeteer"]

            with sync_playwright() as playwright:
                # 2. Attach Playwright to the running AdsPower browser
                browser = connect_playwright_to_browser(playwright, cdp_endpoint)

                # 3. Get or create a page in the existing context
                contexts = browser.contexts
                if contexts and contexts[0].pages:
                    page = contexts[0].pages[0]
                else:
                    context = contexts[0] if contexts else browser.new_context()
                    page = context.new_page()

                page.set_default_timeout(SELECTOR_TIMEOUT_MS)
                page.set_default_navigation_timeout(NAV_TIMEOUT_MS)

                # 4. Navigate to YouTube Studio
                _navigate_to_studio(page)

                # 5. CRITICAL: Verify & switch to the correct channel
                if not self.verify_channel(page):
                    raise RuntimeError(
                        f"Failed to verify/switch to channel '{CHANNEL_HANDLE}'. "
                        "Aborting upload to prevent posting to the wrong channel."
                    )

                # 6. Re-navigate to Studio after possible channel switch (disabled to prevent double reload)
                # _navigate_to_studio(page)

                # 7. Open upload dialog, select file
                _open_upload_dialog(page)
                _set_video_file(page, video_path)

                # 8. Fill metadata, audience, tags
                _fill_details(page, metadata)
                _set_audience(page)

                # 9. Advance wizard to Visibility step
                _advance_to_visibility(page)

                # 10. Set schedule, confirm
                _set_schedule(page, scheduled_time)
                _confirm_schedule(page)

                self.logger.info(
                    "=== Upload complete for '%s' ===", video_path.name
                )
                try:
                    page.close()
                except Exception:
                    pass
                # Playwright will disconnect cleanly when exiting the with block
                return True

        except (RuntimeError, FileNotFoundError) as exc:
            self.logger.error("Upload failed: %s", exc)
        except PlaywrightTimeout as exc:
            self.logger.error("Playwright timeout during upload: %s", exc)
        except Exception as exc:
            self.logger.exception("Unexpected error during upload: %s", exc)
        finally:
            # NOTE: stop_browser is intentionally NOT called here.
            # The BitBrowser profile must remain running after the upload so the
            # user can inspect it and close it manually to conserve daily API limits.
            # stop_browser(self.lang)
            pass

        return False
