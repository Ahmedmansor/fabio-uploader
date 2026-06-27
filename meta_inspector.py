"""
meta_inspector.py — TEMPORARY selector-discovery tool.

Purpose:
    Attach Playwright's built-in Inspector to an *existing* AdsPower session
    that already has a Meta Business Suite page open, WITHOUT refreshing it.
    Use the Inspector's picker to click UI elements and capture exact selectors.

Usage:
    1. Open AdsPower manually and log in to business.facebook.com.
    2. Navigate to the page / state you want to inspect (e.g. the upload dialog).
    3. Run:   py meta_inspector.py
    4. The Playwright Inspector window will open.
       Use the "Pick" button (crosshair icon) to click elements and see selectors.
    5. Close the Inspector window (or press Ctrl+C here) when done.

NOTE: This script does NOT upload anything. Delete it after selector discovery.
"""

import os
import sys
import requests
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright

# ── Load env so ADSPOWER_PROFILE_IT is available ──────────────────────────────
load_dotenv()

# ── AdsPower connection settings (mirrors config.py) ──────────────────────────
ADSPOWER_API_BASE = "http://127.0.0.1:50325/api/v1/browser"
PROFILE_ID          = os.getenv("ADSPOWER_PROFILE_IT", "k1dscqy8")
TARGET_HOST         = "business.facebook.com"

# Set PWDEBUG=1 so Playwright opens its Inspector window automatically
os.environ["PWDEBUG"] = "1"


def _get_ws_endpoint(profile_id: str) -> str:
    """
    Call AdsPower's /start API to get the CDP WebSocket URL
    for the given profile.  The browser must already be running — the API
    returns the live ws:// endpoint even if it was opened earlier.
    """
    url = f"{ADSPOWER_API_BASE}/start"
    print(f"[inspector] Requesting CDP endpoint for AdsPower profile: {profile_id}")
    
    # AdsPower authentication headers
    api_key = os.getenv("ADSPOWER_API_KEY", "")
    headers = {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    params = {
        "user_id": profile_id,
        "open_tabs": 1,
        "ip_tab": 0
    }
    
    try:
        resp = requests.get(url, params=params, headers=headers, timeout=15)
        resp.raise_for_status()
    except requests.RequestException as exc:
        sys.exit(
            f"[inspector] ✗ Could not reach AdsPower Local API: {exc}\n"
            "Make sure AdsPower is running and the Local API is enabled on port 50325."
        )

    payload = resp.json()
    if payload.get("code") != 0:
        sys.exit(f"[inspector] ✗ AdsPower /start failed: {payload.get('msg')} (code: {payload.get('code')})")

    data = payload.get("data", {})
    ws_info = data.get("ws", {})
    ws_url = ws_info.get("puppeteer") or ws_info.get("selenium")

    if not ws_url:
        sys.exit(
            f"[inspector] ✗ No WebSocket CDP endpoint in AdsPower response.\n"
            f"Full payload: {payload}"
        )

    print(f"[inspector] ✓ CDP WebSocket endpoint: {ws_url}")
    return ws_url


def _find_target_page(browser, host: str):
    """
    Scan all existing contexts and pages for one whose URL contains *host*.
    Returns the first matching Page, or None if not found.
    """
    for ctx in browser.contexts:
        for page in ctx.pages:
            print(f"[inspector]   found page → {page.url}")
            if host in page.url:
                return page
    return None


def main() -> None:
    ws_endpoint = _get_ws_endpoint(PROFILE_ID)

    with sync_playwright() as pw:
        print("[inspector] Attaching Playwright to running AdsPower session…")
        browser = pw.chromium.connect_over_cdp(ws_endpoint)
        print(f"[inspector] Connected. Contexts: {len(browser.contexts)}")

        # ── Find the Meta Business Suite page ─────────────────────────────────
        print(f"[inspector] Searching for a page containing '{TARGET_HOST}'…")
        target_page = _find_target_page(browser, TARGET_HOST)

        if target_page is None:
            print(
                f"\n[inspector] ✗ No page containing '{TARGET_HOST}' was found.\n"
                "  → Make sure the Meta Business Suite page is open in AdsPower\n"
                "    before running this script.\n"
                "  → Pages scanned above ↑"
            )
            browser.close()
            return

        print(f"\n[inspector] ✓ Target page found:\n    {target_page.url}")

        # Bring the tab to the front so the Inspector picker is visible
        try:
            target_page.bring_to_front()
        except Exception:
            pass  # not fatal

        print(
            "\n[inspector] ══════════════════════════════════════════════════\n"
            "  Playwright Inspector is opening.\n"
            "  → Click the '⊕ Pick' button (crosshair) in the Inspector.\n"
            "  → Hover over any UI element to see its best selector.\n"
            "  → Close the Inspector window (or Ctrl+C here) when done.\n"
            "[inspector] ══════════════════════════════════════════════════\n"
        )

        # Pause — this opens the Playwright Inspector on the existing page
        # without navigating or changing any page state.
        target_page.pause()

        print("[inspector] Inspector closed. Disconnecting from AdsPower…")
        # Do NOT call browser.close() — that would kill the live AdsPower tab.


if __name__ == "__main__":
    main()
