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
ENABLE_TIKTOK    = True

# ─── Upload Schedule Cadence / Frequency ─────────────────────────────────────
# Exactly ONE of these boolean flags should be True.
#   - SCHEDULE_EVERY_DAY       : Every day (يومياً) -> +1 day interval
#   - SCHEDULE_EVERY_OTHER_DAY : Day on, day off (يوم ويوم) -> +2 days interval
#   - SCHEDULE_EVERY_3_DAYS    : 1 day on, 2 days off (يوم ويومين لا) -> +3 days interval
SCHEDULE_EVERY_DAY       = False
SCHEDULE_EVERY_OTHER_DAY = False
SCHEDULE_EVERY_3_DAYS    = True


def get_schedule_cadence_step_days() -> int:
    """
    Validate active frequency flags and return interval in days.
    Guarantees exactly one active step (defaults to 3 days if misconfigured).
    """
    active_modes = [
        ("EVERY_DAY", SCHEDULE_EVERY_DAY, 1),
        ("EVERY_OTHER_DAY", SCHEDULE_EVERY_OTHER_DAY, 2),
        ("EVERY_3_DAYS", SCHEDULE_EVERY_3_DAYS, 3),
    ]
    selected = [mode for mode in active_modes if mode[1]]
    if len(selected) == 1:
        return selected[0][2]
    
    # If user selected multiple or none by mistake, default to SCHEDULE_EVERY_3_DAYS (3 days)
    if SCHEDULE_EVERY_DAY:
        return 1
    if SCHEDULE_EVERY_OTHER_DAY:
        return 2
    return 3

# ─── Telegram Notifier ───────────────────────────────────────────────────────
# True  → send notification to Telegram Bot if bot credentials exist in .env.
# False → disabled.
ENABLE_TELEGRAM  = True

# ─── Meta Business Suite Composer URL ────────────────────────────────────────
# The pipeline navigates here automatically when no Meta Business Suite tab is
# already open in the active BitBrowser session.
#
# You can append your account-specific query parameters for direct access:
# Example:
#   "https://business.facebook.com/latest/reels_composer"
#   "?asset_id=1106891826149093&business_id=1177063933416599"
META_COMPOSER_URL = "https://business.facebook.com/latest/reels_composer"

# ─── TikTok Studio Creator Center Upload URL ──────────────────────────────────
TIKTOK_UPLOAD_URL = "https://www.tiktok.com/tiktokstudio/upload?from=creator_center"

# ─── Meta Peak Time — Egypt Timezone (Africa/Cairo) ──────────────────────────
# Facebook and Instagram are always scheduled at the same time (one session).
# Values are 24-hour Egypt TZ. Add "EN" here when adding English support.
META_PEAK_TIMES: dict[str, list[dict[str, int]]] = {
    "IT": [{"hour": 21, "minute": 0}],   # 21:00 Egypt = 20:00 Italy
}

# ─── TikTok Peak Time — Egypt Timezone (Africa/Cairo) ────────────────────────
# Values are 24-hour Egypt TZ.
TIKTOK_PEAK_TIMES: dict[str, list[dict[str, int]]] = {
    "IT": [{"hour": 21, "minute": 0}],   # 21:00 Egypt = 20:00 Italy
}
