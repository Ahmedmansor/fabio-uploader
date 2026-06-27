"""
config.py — Central configuration file for Fabio_Uploader.

All constants, paths, and tunable settings live here so that every
other module imports from a single source of truth.

SCALABILITY NOTE:
  To add a new language/platform in a future phase, you only need to:
    1. Add its code to LANGUAGES list.
    2. Add its AdsPower profile ID to ADSPOWER_PROFILES.
    3. Add its peak times to PEAK_TIMES.
    4. Add its context to LANGUAGE_CONTEXTS.
    5. Add its tags/hashtags/disclaimers to the relevant dicts.
  Zero changes needed in the core uploader logic.
"""

import datetime
import os
from pathlib import Path
from dotenv import load_dotenv

# Initialize/Load environment variables early in the configuration setup
load_dotenv()

CURRENT_YEAR = datetime.datetime.now().year

# ─── Base Paths ────────────────────────────────────────────────────────────────
BASE_DIR               = Path(__file__).parent.resolve()
UPLOAD_QUEUE_DIR       = BASE_DIR / "Upload_Queue"
UPLOADED_DONE_DIR      = BASE_DIR / "Uploaded_Done"
LOGS_DIR               = BASE_DIR / "logs"
UPLOAD_STATE_FILE      = BASE_DIR / "upload_state.json"
SCHEDULE_TRACKER_FILE  = BASE_DIR / "schedule_tracker.json"

# ─── Active Languages (Phase 1: IT only) ──────────────────────────────────────
# To add English in the future: append "EN" here and fill the dicts below.
LANGUAGES = ["IT"]

# ─── Video File Naming ─────────────────────────────────────────────────────────
# The script looks for a file named exactly <LANG_CODE>.mp4 (uppercase) in
# each folder. e.g. "IT.mp4", "EN.mp4", etc.
VIDEO_FILENAME_TEMPLATE = "{lang}.mp4"

# ─── Show / Channel Identity ───────────────────────────────────────────────────
CHANNEL_NAME   = "Fabio Egypt"
CHANNEL_HANDLE = "@FabioEgyptItaly"

# ─── Browser Selection Flags ──────────────────────────────────────────────────
# Control which browser automation software is active
USE_BITBROWSER = False
USE_ADSPOWER   = True

# ─── AdsPower Local API ────────────────────────────────────────────────────────
# AdsPower must be running on the local machine for this to work.
ADSPOWER_API_BASE = "http://127.0.0.1:50325/api/v1/browser"

# Map each language code → its AdsPower profile user_id.
# To add EN in the future: "EN": "<adspower_profile_id_for_EN_account>"
ADSPOWER_PROFILES: dict[str, str] = {
    "IT": os.getenv("ADSPOWER_PROFILE_IT", "k1dscqy8"),
}

# ─── BitBrowser Local API ──────────────────────────────────────────────────────
# BitBrowser must be running on the local machine for this to work.
BITBROWSER_API_BASE = "http://127.0.0.1:54345/browser"

# Map each language code → its BitBrowser profile user_id.
BITBROWSER_PROFILES: dict[str, str] = {
    "IT": os.getenv("BITBROWSER_PROFILE_IT", "7c74bf2e8a264e72aaaf29c2f6432e29"),
}

# ─── Scheduling — Egypt Timezone (EET / UTC+2 year-round) ─────────────────────
EGYPT_TIMEZONE = "Africa/Cairo"

# One upload per day, per language.
# IT peak hour: 20:00 Italy time = 21:00 Egypt time (UTC+2 year-round).
# We only need one train since the rule is 1 video per day.
MAX_UPLOADS_PER_LANG_PER_DAY = 1

PEAK_TIMES: dict[str, list[dict[str, int]]] = {
    "IT": [{"hour": 21, "minute": 0}],   # 21:00 Egypt = 20:00 Italy
    # Future expansion example:
    # "EN": [{"hour": 17, "minute": 0}],
}

# ─── Gemini API ────────────────────────────────────────────────────────────────
GEMINI_MODEL     = "models/gemini-3.1-flash-lite"
GEMINI_RPM_SLEEP = 15   # seconds to sleep between sequential API calls

# Language-specific context fed to Gemini for metadata generation
LANGUAGE_CONTEXTS: dict[str, dict] = {
    "IT": {
        "language_name":   "Italian",
        "language_native": "Italiano",
        "audience":        (
            "Italian tourists and travellers interested in visiting Egypt "
            "and Sharm el-Sheikh — people who love sun, sea, adventure, and "
            "authentic cultural experiences"
        ),
        "tone": (
            "warm, enthusiastic, inviting, and inspiring — like a friendly "
            "local guide whispering a travel secret. Use everyday Italian, "
            "not formal/bureaucratic language."
        ),
        "channel_note": (
            "Fabio is an Italian tour guide based in Egypt. His channel "
            "showcases the beauty of Egypt — ancient monuments, the Red Sea, "
            "Sharm el-Sheikh, local food, and hidden gems — to convince "
            "Italian viewers to book a trip or a tour package."
        ),
    },
    # Future expansion:
    # "EN": { ... },
}

# Core brand hashtags to be mixed with AI generated dynamic ones
CORE_BRAND_HASHTAGS: dict[str, list[str]] = {
    "IT": ["#FabioEgypt", "#ViaggioInEgitto"],
}

# Static disclaimer appended to every description, per language
DISCLAIMERS: dict[str, str] = {
    "IT": (
        "Questo video è realizzato da Fabio, guida turistica italiana in Egitto. "
        "Per informazioni su tour e pacchetti viaggio, scrivici nei commenti!"
    ),
}

# Tags injected into YouTube's tag field, per language (no '#' prefix)
VIDEO_TAGS: dict[str, list[str]] = {
    "IT": [
        "egitto", "sharm el sheikh", "viaggio egitto", "tour egitto",
        "guida turistica egitto", "vacanza egitto", "mar rosso",
        "egitto meraviglioso", "visita egitto", "cosa vedere egitto",
        f"egitto {CURRENT_YEAR}", "travel egitto", "fabio egypt",
    ],
}

# ─── Playwright / Browser Settings ────────────────────────────────────────────
BROWSER_SLOW_MO_MS   = 50       # global slow-motion for all Playwright actions (ms)
NAV_TIMEOUT_MS       = 60_000   # page navigation timeout
UPLOAD_TIMEOUT_MS    = 1_800_000  # max wait for "upload complete" indicator (30 min)
SELECTOR_TIMEOUT_MS  = 30_000   # general element wait timeout

BROWSER_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--disable-notifications",
    "--start-maximized",
    "--no-first-run",
    "--no-default-browser-check",
]

# ─── Retry Settings ───────────────────────────────────────────────────────────
UPLOAD_RETRY_COOLDOWN_SEC = 30   # seconds to wait before the single auto-retry
