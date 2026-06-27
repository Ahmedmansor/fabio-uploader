"""
tiktok_inspector.py — TEMPORARY selector-discovery tool for TikTok.

Purpose:
    Attach Playwright's built-in Inspector to an *existing* BitBrowser session
    that already has a TikTok Creator Center page open, WITHOUT refreshing it.
    Use the Inspector's picker to click UI elements and capture exact selectors.

Usage:
    1. Open BitBrowser manually and log in to tiktok.com.
    2. Navigate to the page / state you want to inspect (e.g. the upload creator center).
    3. Run:   py tiktok_inspector.py
    4. The Playwright Inspector window will open.
       Use the "Pick" button (crosshair icon) to click elements and see selectors.
    5. Close the Inspector window (or press Ctrl+C here) when done.

NOTE: This script does NOT upload anything.
"""

import os
import sys
import requests
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright

# Import browser configuration from config.py
from config import (
    USE_BITBROWSER,
    USE_ADSPOWER,
    ADSPOWER_API_BASE,
    BITBROWSER_API_BASE,
    ADSPOWER_PROFILES,
    BITBROWSER_PROFILES,
)

# ── Load env variables ────────────────────────────────────────────────────────
load_dotenv()

# Resolve PROFILE_ID dynamically based on the active browser configuration
if USE_BITBROWSER:
    PROFILE_ID = os.getenv("BITBROWSER_PROFILE_IT") or BITBROWSER_PROFILES.get("IT")
else:
    PROFILE_ID = os.getenv("ADSPOWER_PROFILE_IT") or ADSPOWER_PROFILES.get("IT")

TARGET_HOST = "tiktok.com/tiktokstudio/upload"

# Set PWDEBUG=1 so Playwright opens its Inspector window automatically
os.environ["PWDEBUG"] = "1"


def _get_ws_endpoint(profile_id: str) -> str:
    """
    Get the CDP WebSocket URL for the given profile.
    If USE_BITBROWSER is True: Calls BitBrowser's /browser/open API.
    If USE_ADSPOWER is True: Calls AdsPower's /browser/active API.
    """
    if USE_BITBROWSER:
        url = f"{BITBROWSER_API_BASE}/open"
        print(f"[inspector] [BitBrowser] Requesting CDP endpoint for profile: {profile_id}")
        try:
            resp = requests.post(url, json={"id": profile_id}, timeout=15)
            resp.raise_for_status()
        except requests.RequestException as exc:
            sys.exit(
                f"[inspector] [ERROR] Could not reach BitBrowser Local API: {exc}\n"
                "Make sure BitBrowser is running and the Local API is enabled on port 54345."
            )

        payload = resp.json()
        if not payload.get("success"):
            sys.exit(f"[inspector] [ERROR] BitBrowser /open failed: {payload.get('msg')}")

        data   = payload.get("data", {})
        ws_url = data.get("ws") or data.get("wsUrl") or ""

        if not ws_url:
            sys.exit(
                f"[inspector] [ERROR] No WebSocket URL in BitBrowser response.\n"
                f"Full payload: {payload}"
            )

        if not ws_url.startswith("ws"):
            ws_url = f"ws://{ws_url}"

        print(f"[inspector] [OK] CDP WebSocket endpoint: {ws_url}")
        return ws_url
    else:
        # AdsPower active/status endpoint to get the running profile's WebSocket
        url = f"{ADSPOWER_API_BASE}/active"
        print(f"[inspector] [AdsPower] Requesting CDP endpoint for profile: {profile_id}")
        
        # Handle optional Auth headers
        api_key = os.getenv("ADSPOWER_API_KEY", "")
        headers = {}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        try:
            resp = requests.get(url, params={"user_id": profile_id}, headers=headers, timeout=15)
            resp.raise_for_status()
        except requests.RequestException as exc:
            sys.exit(
                f"[inspector] [ERROR] Could not reach AdsPower Local API: {exc}\n"
                "Make sure AdsPower is running and the Local API is enabled on port 50325."
            )

        payload = resp.json()
        if payload.get("code") != 0:
            sys.exit(
                f"[inspector] [ERROR] AdsPower /active failed: code={payload.get('code')}, msg={payload.get('msg')}\n"
                "Ensure the browser profile is currently running/active in AdsPower."
            )

        data = payload.get("data", {})
        ws_info = data.get("ws", {})
        ws_url = ws_info.get("puppeteer") or ws_info.get("selenium")

        if not ws_url:
            sys.exit(
                f"[inspector] [ERROR] No WebSocket URL in AdsPower response.\n"
                f"Full payload: {payload}"
            )

        print(f"[inspector] [OK] CDP WebSocket endpoint: {ws_url}")
        return ws_url


def _find_target_page(browser, host: str):
    """
    Scan all existing contexts and pages for one whose URL contains *host*.
    Returns the first matching Page, or None if not found.
    """
    for ctx in browser.contexts:
        for page in ctx.pages:
            print(f"[inspector]   found page -> {page.url}")
            if host in page.url:
                return page
    return None


def main() -> None:
    ws_endpoint = _get_ws_endpoint(PROFILE_ID)
    browser_name = "BitBrowser" if USE_BITBROWSER else "AdsPower"

    with sync_playwright() as pw:
        print(f"[inspector] Attaching Playwright to running {browser_name} session…")
        browser = pw.chromium.connect_over_cdp(ws_endpoint)
        print(f"[inspector] Connected. Contexts: {len(browser.contexts)}")

        # ── Find the TikTok page ─────────────────────────────────
        print(f"[inspector] Searching for a page containing '{TARGET_HOST}'…")
        target_page = _find_target_page(browser, TARGET_HOST)

        if target_page is None:
            print(
                f"\n[inspector] [ERROR] No page containing '{TARGET_HOST}' was found.\n"
                f"  -> Make sure the TikTok page is open in {browser_name}\n"
                "    before running this script.\n"
                "  -> Pages scanned above"
            )
            browser.close()
            return

        print(f"\n[inspector] [OK] Target page found:\n    {target_page.url}")

        # Bring the tab to the front so the Inspector picker is visible
        try:
            target_page.bring_to_front()
        except Exception:
            pass  # not fatal

        print(
            "\n[inspector] ==================================================\n"
            "  Playwright Inspector is opening.\n"
            "  -> Click the '+ Pick' button (crosshair) in the Inspector.\n"
            "  -> Hover over any UI element to see its best selector.\n"
            "  -> Close the Inspector window (or Ctrl+C here) when done.\n"
            "[inspector] ==================================================\n"
        )

        # Pause — this opens the Playwright Inspector on the existing page
        # without navigating or changing any page state.
        target_page.pause()

        print("[inspector] Inspector closed. Disconnecting from BitBrowser…")
        # Do NOT call browser.close() — that would kill the live BitBrowser tab.


if __name__ == "__main__":
    main()
