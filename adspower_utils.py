"""
adspower_utils.py — AdsPower Local API integration.

Responsibilities:
  1. Start a specific AdsPower browser profile via the Local REST API.
  2. Extract the WebSocket (CDP) debugging URL from the API response.
  3. Connect Playwright to that running browser session via CDP.
  4. Provide a clean close/stop function that also stops the AdsPower profile.

AdsPower Local API runs at http://127.0.0.1:50325 by default.
Make sure AdsPower is open and running before calling any function here.

SCALABILITY NOTE:
  The language-to-profile mapping lives in config.ADSPOWER_PROFILES.
  Adding a new language only requires adding a key there — no changes here.
"""

import logging
import os
import time

import requests
from dotenv import load_dotenv
from playwright.sync_api import Browser, Playwright

from config import (
    USE_BITBROWSER,
    USE_ADSPOWER,
    ADSPOWER_API_BASE,
    ADSPOWER_PROFILES,
    BITBROWSER_API_BASE,
    BITBROWSER_PROFILES,
)

# Load environment variables (contains ADSPOWER_API_KEY)
load_dotenv()

logger = logging.getLogger(__name__)

# ─── AdsPower API Endpoints ───────────────────────────────────────────────────
_START_URL  = f"{ADSPOWER_API_BASE}/start"
_STOP_URL   = f"{ADSPOWER_API_BASE}/stop"
_STATUS_URL = f"{ADSPOWER_API_BASE}/active"

# Request timeout for the local API (should be very fast — it's localhost)
_API_TIMEOUT = 30   # seconds


def _get_headers() -> dict:
    """Return the headers needed for AdsPower Local API authentication."""
    api_key = os.getenv("ADSPOWER_API_KEY", "")
    headers = {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    return headers


# ─── Profile Lookup ────────────────────────────────────────────────────────────

def get_profile_id(lang: str) -> str:
    """
    Return the browser profile user_id for the given language code.

    Raises:
        KeyError: if no profile is configured for *lang* in config.py.
    """
    if USE_BITBROWSER:
        profile_id = BITBROWSER_PROFILES.get(lang)
        if not profile_id:
            raise KeyError(
                f"No BitBrowser profile configured for language '{lang}'. "
                f"Add it to BITBROWSER_PROFILES in config.py."
            )
        return profile_id
    else:
        profile_id = ADSPOWER_PROFILES.get(lang)
        if not profile_id:
            raise KeyError(
                f"No AdsPower profile configured for language '{lang}'. "
                f"Add it to ADSPOWER_PROFILES in config.py."
            )
        return profile_id


# ─── Browser Lifecycle ─────────────────────────────────────────────────────────

def start_browser(lang: str) -> dict:
    """
    Start the browser profile for *lang* via the active browser's Local API.

    Returns a dict containing ws connection info. Conforming to AdsPower format:
    {"ws": {"puppeteer": ws_url}}

    Raises:
        RuntimeError: on API errors.
    """
    profile_id = get_profile_id(lang)

    if USE_BITBROWSER:
        logger.info("[BitBrowser] Starting profile '%s' for lang='%s'…", profile_id, lang)
        url = f"{BITBROWSER_API_BASE}/open"
        try:
            resp = requests.post(
                url,
                json={"id": profile_id},
                timeout=_API_TIMEOUT,
            )
            resp.raise_for_status()
        except requests.RequestException as exc:
            raise RuntimeError(
                f"[BitBrowser] HTTP request to Local API failed: {exc}\n"
                "Is BitBrowser running? Is the Local API enabled (port 54345)?"
            ) from exc

        payload = resp.json()
        logger.debug("[BitBrowser] /open response: %s", payload)

        if not payload.get("success"):
            raise RuntimeError(
                f"[BitBrowser] /open returned error: {payload.get('msg')}"
            )

        data = payload.get("data", {})
        ws_url = data.get("ws")
        if not ws_url:
            ws_url = data.get("wsUrl")

        if not ws_url:
            raise RuntimeError(
                f"[BitBrowser] No WebSocket address in API response. Full payload: {payload}"
            )

        # Standardize ws:// prefix if missing
        if ws_url and not ws_url.startswith("ws"):
            ws_url = f"ws://{ws_url}"

        logger.info(
            "[BitBrowser] Profile started. WS endpoint: %s",
            ws_url,
        )

        time.sleep(2)
        return {"ws": {"puppeteer": ws_url}}

    else:
        logger.info("[AdsPower] Starting profile '%s' for lang='%s'…", profile_id, lang)

        params = {
            "user_id": profile_id,
            "open_tabs": 1,         # open a new tab in the profile
            "ip_tab": 0,            # don't open the IP checker tab
        }

        try:
            resp = requests.get(
                _START_URL,
                params=params,
                headers=_get_headers(),
                timeout=_API_TIMEOUT,
            )
            resp.raise_for_status()
        except requests.RequestException as exc:
            raise RuntimeError(
                f"[AdsPower] HTTP request to Local API failed: {exc}\n"
                "Is AdsPower running? Is the Local API enabled (port 50325)?"
            ) from exc

        payload = resp.json()
        logger.debug("[AdsPower] /start response: %s", payload)

        if payload.get("code") != 0:
            raise RuntimeError(
                f"[AdsPower] /start returned error — code={payload.get('code')}, "
                f"msg={payload.get('msg')}"
            )

        data = payload.get("data", {})
        ws_info = data.get("ws", {})
        cdp_endpoint = ws_info.get("puppeteer") or ws_info.get("selenium")

        if not cdp_endpoint:
            raise RuntimeError(
                f"[AdsPower] No WebSocket CDP endpoint in API response. "
                f"Full data block: {data}"
            )

        logger.info(
            "[AdsPower] Profile started. CDP endpoint: %s",
            cdp_endpoint,
        )

        # Brief pause to let the browser fully initialise before we attach
        time.sleep(2)

        return data


def connect_playwright_to_browser(playwright: Playwright, cdp_endpoint: str) -> Browser:
    """
    Attach a Playwright Browser object to an already-running Chrome instance
    via the Chrome DevTools Protocol (CDP) WebSocket URL.

    Args:
        playwright:    The sync Playwright instance (from sync_playwright()).
        cdp_endpoint:  The ws://... URL returned by AdsPower's /start API.

    Returns:
        A connected Playwright Browser object (NOT a new context — the existing
        tabs of the AdsPower profile are accessible via browser.contexts[0]).
    """
    logger.info("[AdsPower] Attaching Playwright to CDP endpoint…")
    browser = playwright.chromium.connect_over_cdp(cdp_endpoint)
    logger.info("[AdsPower] Playwright connected. Contexts: %d", len(browser.contexts))
    return browser


def stop_browser(lang: str) -> None:
    """
    Stop the active browser profile for *lang* via the Local API.

    This is a best-effort call — failure is logged but not re-raised, so it
    won't mask the original upload result.
    """
    profile_id = get_profile_id(lang)

    if USE_BITBROWSER:
        logger.info("[BitBrowser] Stopping profile '%s' for lang='%s'…", profile_id, lang)
        url = f"{BITBROWSER_API_BASE}/close"
        try:
            resp = requests.post(
                url,
                json={"id": profile_id},
                timeout=_API_TIMEOUT,
            )
            resp.raise_for_status()
            payload = resp.json()
            if payload.get("success"):
                logger.info("[BitBrowser] Profile '%s' stopped successfully.", profile_id)
            else:
                logger.warning(
                    "[BitBrowser] /close returned failure — msg=%s",
                    payload.get("msg"),
                )
        except Exception as exc:
            logger.warning("[BitBrowser] Could not stop profile for lang='%s': %s", lang, exc)
    else:
        logger.info("[AdsPower] Stopping profile '%s' for lang='%s'…", profile_id, lang)
        try:
            resp = requests.get(
                _STOP_URL,
                params={"user_id": profile_id},
                headers=_get_headers(),
                timeout=_API_TIMEOUT,
            )
            resp.raise_for_status()
            payload = resp.json()
            if payload.get("code") == 0:
                logger.info("[AdsPower] Profile '%s' stopped successfully.", profile_id)
            else:
                logger.warning(
                    "[AdsPower] /stop returned non-zero — code=%s, msg=%s",
                    payload.get("code"), payload.get("msg"),
                )
        except Exception as exc:
            logger.warning("[AdsPower] Could not stop profile for lang='%s': %s", lang, exc)
