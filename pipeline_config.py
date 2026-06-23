"""
pipeline_config.py — Per-run platform toggles and Meta configuration.

Edit ONLY this file before each run to control which platforms are active.
The main pipeline reads these flags once at startup before processing any folder.

IMPORTANT — Facebook & Instagram:
  Facebook and Instagram are uploaded together in a single Meta Business Suite
  session. If you set one flag to False while the other is True, the pipeline
  will mark BOTH as "skipped by the user" because they cannot be separated in
  the current architecture. Always enable or disable them as a pair.
"""

# ─── Platform Enable / Disable Flags ─────────────────────────────────────────
# True  → attempt upload (unless state is already "success").
# False → skip immediately and write "skipped by the user" in upload_state.json.
ENABLE_YOUTUBE   = True
ENABLE_FACEBOOK  = True
ENABLE_INSTAGRAM = True

# ─── Meta Business Suite Composer URL ────────────────────────────────────────
# The pipeline navigates here automatically when no Meta Business Suite tab is
# already open in the active BitBrowser session.
#
# You can append your account-specific query parameters for direct access:
# Example:
#   "https://business.facebook.com/latest/reels_composer"
#   "?asset_id=1106891826149093&business_id=1177063933416599"
META_COMPOSER_URL = "https://business.facebook.com/latest/reels_composer"

# ─── Meta Peak Time — Egypt Timezone (Africa/Cairo) ──────────────────────────
# Facebook and Instagram are always scheduled at the same time (one session).
# Values are 24-hour Egypt TZ. Add "EN" here when adding English support.
META_PEAK_TIMES: dict[str, list[dict[str, int]]] = {
    "IT": [{"hour": 21, "minute": 0}],   # 21:00 Egypt = 20:00 Italy
}
