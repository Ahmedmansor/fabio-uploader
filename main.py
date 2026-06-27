"""
main.py — Orchestrator for Fabio_Uploader (Multi-Platform Pipeline).

Platforms:  YouTube  →  Meta (Facebook + Instagram).
Control:    Edit ENABLE_YOUTUBE / ENABLE_FACEBOOK / ENABLE_INSTAGRAM in
            pipeline_config.py before each run.

upload_state.json schema (v3 — per-platform with attempt tracking):
{
  "project_1": {
    "IT": {
      "youtube": {
        "status":         "pending|loading|success|error|skipped by the user",
        "attempts_count": 2,
        "attempts_log":   ["2026-06-23 21:00", "2026-06-24 00:05"]
      },
      "facebook":  { ... same structure ... },
      "instagram": { ... same structure ... }
    }
  }
}

Archiving rule:
  A folder is moved to Uploaded_Done ONLY when ALL THREE platform statuses
  are "success". Any other combination keeps the folder in Upload_Queue.

Queue / retry logic (per platform):
  "success"          → skip (done).
  "skipped by user"  + flag still False  → skip (still disabled).
  "skipped by user"  + flag now True     → re-attempt (user re-enabled it).
  "error" / "loading" / "pending"        → attempt upload.

Midnight-crossing safety:
  attempt tracking is date-stamped. If a run fails at 23:55 and resumes
  at 00:05, the scheduler finds the next free day from schedule_tracker.json
  while the state machine still sees "error" → project is resumed correctly.

Facebook + Instagram note:
  Both platforms share one Meta Business Suite upload session.
  They are always attempted or skipped together. If one flag is True but
  the other is False, BOTH are marked "skipped by the user" with a warning.

Usage:
  py main.py                        # Auto-find and process first pending folder
  py main.py --folder project_2     # Override to a specific folder
  py main.py --dry-run              # Simulate (Gemini runs; no browser)
  py main.py --log-level DEBUG      # Verbose output
"""

import argparse
import json
import logging
import os
import re
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

import pytz
from dotenv import load_dotenv

from config import (
    BASE_DIR,
    EGYPT_TIMEZONE,
    LANGUAGES,
    LOGS_DIR,
    UPLOAD_QUEUE_DIR,
    UPLOAD_RETRY_COOLDOWN_SEC,
    UPLOAD_STATE_FILE,
    UPLOADED_DONE_DIR,
)
from pipeline_config import (
    ENABLE_FACEBOOK,
    ENABLE_INSTAGRAM,
    ENABLE_YOUTUBE,
    ENABLE_TIKTOK,
    ENABLE_TELEGRAM,
)
from metadata_agent import generate_metadata_for_folder
from scheduler import (
    get_next_slot,
    get_next_slot_meta,
    get_next_slot_tiktok,
    increment_upload_count,
)
from utils import (
    ensure_dirs,
    find_thumbnail,
    find_video_file,
    human_sleep,
    move_folder_to_done,
    setup_logging,
)
from youtube_uploader import YouTubeUploader
from meta_uploader import MetaUploader
from tiktok_uploader import TikTokUploader
from telegram_notifier import send_telegram_report

# ── Force UTF-8 for Windows terminals ─────────────────────────────────────────
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

load_dotenv()
logger = logging.getLogger(__name__)

# ─── Platform & Status Constants ─────────────────────────────────────────────
PLATFORMS = ["youtube", "facebook", "instagram", "tiktok"]

STATUS_PENDING = "pending"
STATUS_LOADING = "loading"
STATUS_SUCCESS = "success"
STATUS_ERROR   = "error"
STATUS_SKIPPED = "skipped by the user"

# Statuses that allow a new upload attempt
RESUMABLE = {STATUS_PENDING, STATUS_LOADING, STATUS_ERROR}

_tz = pytz.timezone(EGYPT_TIMEZONE)


def _natural_sort_key(path: Path) -> list:
    """Key for sorting paths naturally (e.g. project_5 before project_10)."""
    return [int(text) if text.isdigit() else text.lower() for text in re.split(r'(\d+)', path.name)]


# ─── Platform Flag Helpers ────────────────────────────────────────────────────

def _is_platform_enabled(platform: str) -> bool:
    """Return True if the given platform's flag is set to True in pipeline_config."""
    return {
        "youtube":   ENABLE_YOUTUBE,
        "facebook":  ENABLE_FACEBOOK,
        "instagram": ENABLE_INSTAGRAM,
        "tiktok":    ENABLE_TIKTOK,
    }[platform]


# ─── State Schema Helpers ─────────────────────────────────────────────────────

def _init_platform_state() -> dict:
    """
    Return a fresh per-platform state entry (v3 schema).
    {
        "status":         "pending",
        "attempts_count": 0,
        "attempts_log":   []
    }
    """
    return {
        "status":         STATUS_PENDING,
        "attempts_count": 0,
        "attempts_log":   [],
    }


def _init_lang_state() -> dict:
    """Return a fresh per-language state dict with all three platforms initialised."""
    return {p: _init_platform_state() for p in PLATFORMS}


def _extract_status(entry) -> str:
    """
    Safely extract the status string from any supported entry format:
      - None          → "pending"
      - "success"     → "success"  (v1/v2 flat string — migration path)
      - {"status": …} → the status value  (v3 dict)
    """
    if entry is None:
        return STATUS_PENDING
    if isinstance(entry, str):
        return entry
    if isinstance(entry, dict):
        return entry.get("status", STATUS_PENDING)
    return STATUS_PENDING


# ─── State I/O ────────────────────────────────────────────────────────────────

def _load_state() -> dict:
    """Load upload_state.json from disk. Returns {} if missing or corrupt."""
    if not UPLOAD_STATE_FILE.exists():
        return {}
    try:
        with UPLOAD_STATE_FILE.open("r", encoding="utf-8") as fh:
            return json.load(fh)
    except json.JSONDecodeError as exc:
        logger.error("upload_state.json corrupted (%s) — starting fresh.", exc)
        return {}


def _save_state(state: dict) -> None:
    """Write upload_state.json atomically (temp-file → rename)."""
    parent = UPLOAD_STATE_FILE.parent
    parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=parent, suffix=".tmp", prefix="state_")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(state, fh, ensure_ascii=False, indent=2)
        os.replace(tmp_path, UPLOAD_STATE_FILE)
        logger.debug("upload_state.json saved.")
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _ensure_platform_dict(state: dict, folder: str, lang: str) -> None:
    """
    Ensure every platform for (folder, lang) is a proper v3 dict.
    Migrates automatically from:
      v1: {"IT": "success"}          → flat string on the lang level
      v2: {"IT": {"youtube": "ok"}}  → flat string on the platform level
      v3: {"IT": {"youtube": {...}}} → already correct
    """
    state.setdefault(folder, {}).setdefault(lang, {})
    lang_entry = state[folder][lang]

    # Guard: if lang_entry itself is a flat string (v1 schema), reset it
    if not isinstance(lang_entry, dict):
        state[folder][lang] = _init_lang_state()
        return

    for p in PLATFORMS:
        p_entry = lang_entry.get(p)
        if p_entry is None:
            # Missing platform — initialise fresh
            lang_entry[p] = _init_platform_state()
        elif isinstance(p_entry, str):
            # v2 flat string → v3 dict (preserve the old status)
            old_status = p_entry
            lang_entry[p] = _init_platform_state()
            lang_entry[p]["status"] = old_status
        # else: already a v3 dict — leave it alone


def _set_status(
    state: dict, folder: str, lang: str, platform: str, status: str
) -> dict:
    """
    Update one platform's status and persist immediately.
    Preserves existing attempts_count and attempts_log.
    """
    _ensure_platform_dict(state, folder, lang)
    state[folder][lang][platform]["status"] = status
    _save_state(state)
    logger.info("[%s][%s][%s] Status → %s", folder, lang, platform, status.upper())
    return state


def _record_attempt(
    state: dict, folder: str, lang: str, platform: str
) -> dict:
    """
    Increment attempts_count and append the current Egypt-TZ timestamp to
    attempts_log.  Called once at the start of each upload attempt so that
    midnight-crossing scenarios are fully auditable.
    """
    _ensure_platform_dict(state, folder, lang)
    now_str = datetime.now(_tz).strftime("%Y-%m-%d %H:%M")
    entry   = state[folder][lang][platform]
    entry["attempts_count"] += 1
    entry["attempts_log"].append(now_str)
    _save_state(state)
    logger.info(
        "[%s][%s][%s] Attempt #%d recorded at %s",
        folder, lang, platform,
        entry["attempts_count"], now_str,
    )
    return state


def _get_platform_status(
    state: dict, folder: str, lang: str, platform: str
) -> str:
    """Safely read one platform's status (handles all schema versions)."""
    entry = state.get(folder, {}).get(lang, {}).get(platform)
    return _extract_status(entry)


# ─── State Sync ───────────────────────────────────────────────────────────────

def _sync_state_with_queue(state: dict) -> dict:
    """
    Register any new Upload_Queue folders not yet tracked in state.
    Also migrates old v1/v2 entries to the current v3 schema on first access.
    """
    if not UPLOAD_QUEUE_DIR.exists():
        return state

    changed = False
    for folder_path in sorted(UPLOAD_QUEUE_DIR.iterdir(), key=_natural_sort_key):
        if not folder_path.is_dir():
            continue
        name = folder_path.name
        if find_video_file(folder_path, "IT") is None:
            continue

        state.setdefault(name, {})

        for lang in LANGUAGES:
            lang_entry = state[name].get(lang)

            if lang_entry is None:
                # Brand-new folder
                state[name][lang] = _init_lang_state()
                logger.info("Initialized state for '%s/%s'.", name, lang)
                changed = True

            elif isinstance(lang_entry, str):
                # v1 schema: {"IT": "success"} — reset to v3 (can't recover platforms)
                logger.warning(
                    "Migrating v1 flat-string state for '%s/%s' → per-platform v3.",
                    name, lang,
                )
                state[name][lang] = _init_lang_state()
                changed = True

            elif isinstance(lang_entry, dict):
                # v2 or v3 — migrate individual platform entries as needed
                for p in PLATFORMS:
                    p_entry = lang_entry.get(p)
                    if p_entry is None:
                        lang_entry[p] = _init_platform_state()
                        changed = True
                    elif isinstance(p_entry, str):
                        # v2: flat string → v3 dict, preserving status
                        old_status = p_entry
                        lang_entry[p] = _init_platform_state()
                        lang_entry[p]["status"] = old_status
                        logger.info(
                            "Migrated v2→v3 for '%s/%s/%s': status=%s",
                            name, lang, p, old_status,
                        )
                        changed = True

    if changed:
        _save_state(state)
    return state


# ─── Queue Logic ──────────────────────────────────────────────────────────────

def _has_pending_work(it_state: dict) -> bool:
    """
    Return True if any ENABLED platform still requires an upload attempt.

    Re-enable case: if a platform was "skipped by the user" but its flag is
    now True, it is treated as needing work so it gets picked up this run.
    """
    for platform in PLATFORMS:
        if not _is_platform_enabled(platform):
            continue
        status = _extract_status(it_state.get(platform))
        if status in RESUMABLE:
            return True
        if status == STATUS_SKIPPED:
            return True   # flag re-enabled → retry this previously-skipped platform
    return False


def _all_done(it_state: dict) -> bool:
    """Return True ONLY when ALL THREE platforms report 'success'."""
    return all(
        _extract_status(it_state.get(p)) == STATUS_SUCCESS
        for p in PLATFORMS
    )


def _find_pending_folder(
    state: dict, override: str | None = None
) -> str | None:
    """
    Return the name of the first folder in Upload_Queue with pending work,
    or None if everything is complete under the current config flags.
    """
    if override:
        folder_path = UPLOAD_QUEUE_DIR / override
        if not folder_path.is_dir():
            logger.error("--folder '%s' does not exist in Upload_Queue.", override)
            return None
        if find_video_file(folder_path, "IT") is None:
            logger.error("--folder '%s' does not contain IT.mp4.", override)
            return None
        logger.info("Queue lock OVERRIDDEN by --folder flag: '%s'", override)
        return override

    if not UPLOAD_QUEUE_DIR.exists():
        return None

    for folder_path in sorted(UPLOAD_QUEUE_DIR.iterdir(), key=_natural_sort_key):
        if not folder_path.is_dir():
            continue
        name = folder_path.name
        if find_video_file(folder_path, "IT") is None:
            continue
        it_state = state.get(name, {}).get("IT", {})
        if _has_pending_work(it_state):
            return name

    return None


# ─── Metadata Preview ─────────────────────────────────────────────────────────

def _print_metadata_preview(folder: str, meta: dict | None) -> None:
    sep = "-" * 64
    print(f"\n{'=' * 64}")
    print(f"  METADATA PREVIEW - {folder}")
    print(f"{'=' * 64}")
    print(f"\n{sep}")
    print("  [IT]")
    print(sep)
    if meta is None:
        print("  [WARNING] Metadata generation FAILED.")
    else:
        yt = meta.get("youtube", {})
        meta_reels = meta.get("meta_reels") or meta.get("short_form", {})
        tiktok = meta.get("tiktok") or meta.get("short_form", {})
        
        print("  --- YOUTUBE SHORTS ---")
        print(f"  TITLE       : {yt.get('title', 'N/A')}")
        print("  DESCRIPTION :")
        for line in yt.get("description", "").splitlines():
            print(f"                {line}")
        tags_str = ", ".join(yt.get("tags", []))
        print(f"  TAGS        : {tags_str}")
        
        print("\n  --- META REELS (FB & IG) ---")
        print(f"  CAPTION     : {meta_reels.get('caption', 'N/A')}")
        meta_hash = " ".join(meta_reels.get("hashtags", []))
        print(f"  HASHTAGS    : {meta_hash}")
        
        print("\n  --- TIKTOK ---")
        print(f"  CAPTION     : {tiktok.get('caption', 'N/A')}")
        tiktok_hash = " ".join(tiktok.get("hashtags", []))
        print(f"  HASHTAGS    : {tiktok_hash}")
    print(f"\n{'=' * 64}\n")


# ─── Platform Upload Functions ────────────────────────────────────────────────

def _run_youtube(
    folder: str,
    video_path: Path,
    metadata: dict,
    state: dict,
    dry_run: bool,
) -> dict:
    """
    Attempt the YouTube upload for *folder* with one automatic retry.
    Logs each attempt with a real-world timestamp for midnight-crossing audits.
    """
    lang = "IT"
    yt_scheduled, yt_date_key = get_next_slot(lang, "youtube")
    state = _set_status(state, folder, lang, "youtube", STATUS_LOADING)
    uploader = YouTubeUploader(lang=lang)

    for attempt in range(1, 3):
        logger.info("[%s][youtube] Upload attempt %d/2…", folder, attempt)
        # Record this attempt before launching so midnight-crossing is captured
        state = _record_attempt(state, folder, lang, "youtube")

        success = uploader.upload(
            video_path=video_path,
            metadata=metadata,
            scheduled_time=yt_scheduled,
            dry_run=dry_run,
        )
        if success:
            state = _set_status(state, folder, lang, "youtube", STATUS_SUCCESS)
            increment_upload_count(lang, "youtube", yt_date_key, yt_scheduled)
            return state

        if attempt == 1:
            logger.warning(
                "[%s][youtube] Attempt 1 failed — cooling down %ds before retry…",
                folder, UPLOAD_RETRY_COOLDOWN_SEC,
            )
            time.sleep(UPLOAD_RETRY_COOLDOWN_SEC)

    logger.error("[%s][youtube] Both attempts failed — marking ERROR.", folder)
    state = _set_status(state, folder, lang, "youtube", STATUS_ERROR)
    return state


def _run_meta(
    folder: str,
    video_path: Path,
    thumbnail_path: Path | None,
    metadata: dict,
    state: dict,
    dry_run: bool,
) -> dict:
    """
    Attempt the Meta (Facebook + Instagram) upload for *folder* with one retry.
    Both platforms share one session — both states are updated together.
    Each attempt is logged for midnight-crossing audits.
    """
    lang = "IT"
    meta_scheduled, meta_date_key = get_next_slot_meta(lang)
    state = _set_status(state, folder, lang, "facebook",  STATUS_LOADING)
    state = _set_status(state, folder, lang, "instagram", STATUS_LOADING)
    uploader = MetaUploader(lang=lang, thumbnail_path=thumbnail_path)

    for attempt in range(1, 3):
        logger.info("[%s][meta] Upload attempt %d/2…", folder, attempt)
        # Record for both platforms before launching
        state = _record_attempt(state, folder, lang, "facebook")
        state = _record_attempt(state, folder, lang, "instagram")

        success = uploader.upload(
            video_path=video_path,
            metadata=metadata,
            scheduled_time=meta_scheduled,
            dry_run=dry_run,
        )
        if success:
            state = _set_status(state, folder, lang, "facebook",  STATUS_SUCCESS)
            state = _set_status(state, folder, lang, "instagram", STATUS_SUCCESS)
            increment_upload_count(lang, "facebook",  meta_date_key, meta_scheduled)
            increment_upload_count(lang, "instagram", meta_date_key, meta_scheduled)
            return state

        if attempt == 1:
            logger.warning(
                "[%s][meta] Attempt 1 failed — cooling down %ds before retry…",
                folder, UPLOAD_RETRY_COOLDOWN_SEC,
            )
            time.sleep(UPLOAD_RETRY_COOLDOWN_SEC)

    logger.error("[%s][meta] Both attempts failed — marking ERROR.", folder)
    state = _set_status(state, folder, lang, "facebook",  STATUS_ERROR)
    state = _set_status(state, folder, lang, "instagram", STATUS_ERROR)
    return state


def _run_tiktok(
    folder: str,
    video_path: Path,
    thumbnail_path: Path | None,
    metadata: dict,
    state: dict,
    dry_run: bool,
) -> dict:
    """
    Attempt the TikTok upload for *folder* with one retry.
    Logs each attempt with a real-world timestamp for midnight-crossing audits.
    """
    lang = "IT"
    tt_scheduled, tt_date_key = get_next_slot_tiktok(lang)
    state = _set_status(state, folder, lang, "tiktok", STATUS_LOADING)
    uploader = TikTokUploader(lang=lang, thumbnail_path=thumbnail_path)

    for attempt in range(1, 3):
        logger.info("[%s][tiktok] Upload attempt %d/2…", folder, attempt)
        # Record this attempt before launching so midnight-crossing is captured
        state = _record_attempt(state, folder, lang, "tiktok")

        success = uploader.upload(
            video_path=video_path,
            metadata=metadata,
            scheduled_time=tt_scheduled,
            dry_run=dry_run,
        )
        if success:
            state = _set_status(state, folder, lang, "tiktok", STATUS_SUCCESS)
            increment_upload_count(lang, "tiktok", tt_date_key, tt_scheduled)
            return state

        if attempt == 1:
            logger.warning(
                "[%s][tiktok] Attempt 1 failed — cooling down %ds before retry…",
                folder, UPLOAD_RETRY_COOLDOWN_SEC,
            )
            time.sleep(UPLOAD_RETRY_COOLDOWN_SEC)

    logger.error("[%s][tiktok] Both attempts failed — marking ERROR.", folder)
    state = _set_status(state, folder, lang, "tiktok", STATUS_ERROR)
    return state


# ─── Main Folder Processor ────────────────────────────────────────────────────

def _process_folder(
    folder: str,
    folder_path: Path,
    metadata: dict,
    state: dict,
    dry_run: bool,
) -> dict:
    """
    Run all enabled platforms for *folder* in order: YouTube → Meta.

    Per-platform decision matrix:
      Disabled (flag=False)           → write "skipped by the user", skip.
      Already "success"               → log and skip.
      "skipped by user" + flag now True → re-attempt (user re-enabled it).
      "error" / "loading" / "pending"  → attempt upload.

    Facebook + Instagram mismatch (one True, one False):
      Cannot split a single Meta session → mark BOTH as "skipped" + warn.
    """
    lang       = "IT"
    video_path = find_video_file(folder_path, lang)

    # ── YouTube ───────────────────────────────────────────────────────────────
    if not ENABLE_YOUTUBE:
        yt_status = _get_platform_status(state, folder, lang, "youtube")
        if yt_status != STATUS_SUCCESS:
            logger.info("[%s] YouTube DISABLED — marking as skipped.", folder)
            state = _set_status(state, folder, lang, "youtube", STATUS_SKIPPED)
        else:
            logger.info("[%s] YouTube already succeeded — leaving status intact.", folder)
    else:
        yt_status = _get_platform_status(state, folder, lang, "youtube")
        if yt_status == STATUS_SUCCESS:
            logger.info("[%s] YouTube already uploaded — skipping.", folder)
        else:
            if yt_status == STATUS_SKIPPED:
                logger.info(
                    "[%s] YouTube was previously skipped — now re-enabled. Retrying…",
                    folder,
                )
            logger.info("[%s] Running YouTube upload…", folder)
            state = _run_youtube(folder, video_path, metadata, state, dry_run)

    # ── Meta (Facebook + Instagram together) ──────────────────────────────────
    meta_both     = ENABLE_FACEBOOK and ENABLE_INSTAGRAM
    meta_neither  = not ENABLE_FACEBOOK and not ENABLE_INSTAGRAM
    meta_mismatch = ENABLE_FACEBOOK != ENABLE_INSTAGRAM

    if meta_neither:
        for p in ("facebook", "instagram"):
            p_status = _get_platform_status(state, folder, lang, p)
            if p_status != STATUS_SUCCESS:
                logger.info(
                    "[%s] %s DISABLED — marking as skipped.", folder, p.capitalize()
                )
                state = _set_status(state, folder, lang, p, STATUS_SKIPPED)
            else:
                logger.info(
                    "[%s] %s already succeeded — leaving status intact.",
                    folder, p.capitalize(),
                )

    elif meta_mismatch:
        logger.warning(
            "[%s] ENABLE_FACEBOOK=%s / ENABLE_INSTAGRAM=%s are mismatched. "
            "Facebook and Instagram must be toggled together (shared upload session). "
            "Marking BOTH as 'skipped by the user' until both flags are aligned.",
            folder, ENABLE_FACEBOOK, ENABLE_INSTAGRAM,
        )
        for p in ("facebook", "instagram"):
            p_status = _get_platform_status(state, folder, lang, p)
            if p_status != STATUS_SUCCESS:
                state = _set_status(state, folder, lang, p, STATUS_SKIPPED)

    elif meta_both:
        fb_status = _get_platform_status(state, folder, lang, "facebook")
        ig_status = _get_platform_status(state, folder, lang, "instagram")

        if fb_status == STATUS_SUCCESS and ig_status == STATUS_SUCCESS:
            logger.info("[%s] Meta (Facebook + Instagram) already uploaded — skipping.", folder)
        else:
            for p, st in (("facebook", fb_status), ("instagram", ig_status)):
                if st == STATUS_SKIPPED:
                    logger.info(
                        "[%s] %s was previously skipped — now re-enabled. Retrying…",
                        folder, p.capitalize(),
                    )

            thumbnail_path = find_thumbnail(folder_path)
            if thumbnail_path:
                logger.info(
                    "[%s] Thumbnail found for Meta: %s", folder, thumbnail_path.name
                )
            else:
                logger.info(
                    "[%s] No thumbnail found — Meta upload will skip thumbnail step.", folder
                )

            logger.info("[%s] Running Meta (Facebook + Instagram) upload…", folder)
            state = _run_meta(
                folder, video_path, thumbnail_path, metadata, state, dry_run
            )

    # ── TikTok ────────────────────────────────────────────────────────────────
    if not ENABLE_TIKTOK:
        tt_status = _get_platform_status(state, folder, lang, "tiktok")
        if tt_status != STATUS_SUCCESS:
            logger.info("[%s] TikTok DISABLED — marking as skipped.", folder)
            state = _set_status(state, folder, lang, "tiktok", STATUS_SKIPPED)
        else:
            logger.info("[%s] TikTok already succeeded — leaving status intact.", folder)
    else:
        tt_status = _get_platform_status(state, folder, lang, "tiktok")
        if tt_status == STATUS_SUCCESS:
            logger.info("[%s] TikTok already uploaded — skipping.", folder)
        else:
            if tt_status == STATUS_SKIPPED:
                logger.info(
                    "[%s] TikTok was previously skipped — now re-enabled. Retrying…",
                    folder,
                )
            thumbnail_path = find_thumbnail(folder_path)
            logger.info("[%s] Running TikTok upload…", folder)
            state = _run_tiktok(folder, video_path, thumbnail_path, metadata, state, dry_run)

    return state


# ─── CLI Argument Parsing ─────────────────────────────────────────────────────

def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Fabio_Uploader — Multi-Platform Auto-Uploader",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Simulate: generates metadata but skips all browser actions.",
    )
    parser.add_argument(
        "--folder", default=None,
        help="Override auto-scan and process a specific folder name.",
    )
    parser.add_argument(
        "--log-level", default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    return parser.parse_args()


# ─── Pre-flight ───────────────────────────────────────────────────────────────

def _pre_flight(dry_run: bool) -> None:
    ensure_dirs(UPLOAD_QUEUE_DIR, UPLOADED_DONE_DIR, LOGS_DIR)
    logger.info("=" * 64)
    logger.info("  Fabio_Uploader — Multi-Platform Auto-Uploader")
    logger.info("  Base dir   : %s", BASE_DIR)
    logger.info("  Dry-run    : %s", dry_run)
    logger.info("  YouTube    : %s", "ON" if ENABLE_YOUTUBE   else "OFF (will be skipped)")
    logger.info("  Facebook   : %s", "ON" if ENABLE_FACEBOOK  else "OFF (will be skipped)")
    logger.info("  Instagram  : %s", "ON" if ENABLE_INSTAGRAM else "OFF (will be skipped)")
    logger.info("  TikTok     : %s", "ON" if ENABLE_TIKTOK    else "OFF (will be skipped)")
    logger.info("=" * 64)

    if not UPLOAD_QUEUE_DIR.exists() or not any(UPLOAD_QUEUE_DIR.iterdir()):
        logger.warning("Upload_Queue is empty. Add a video folder with IT.mp4 to begin.")


# ─── Main ─────────────────────────────────────────────────────────────────────

def main() -> None:
    args = _parse_args()
    setup_logging(getattr(logging, args.log_level))

    _pre_flight(args.dry_run)

    # 1. Load & sync state (migrates old schemas automatically)
    state = _load_state()
    state = _sync_state_with_queue(state)

    # 2. Find the folder to process
    target_folder = _find_pending_folder(state, override=args.folder)

    if target_folder is None:
        logger.info(
            "All folders in Upload_Queue are fully uploaded or up-to-date "
            "under the current platform configuration. Nothing to do."
        )
        return

    folder_path = UPLOAD_QUEUE_DIR / target_folder
    lang        = "IT"
    it_state    = state.get(target_folder, {}).get(lang, {})

    logger.info(
        "Processing folder: '%s'  |  current statuses: %s",
        target_folder,
        {p: _extract_status(it_state.get(p)) for p in PLATFORMS},
    )

    # 3. Find IT.mp4
    video_path = find_video_file(folder_path, lang)
    if video_path is None:
        logger.error("IT.mp4 not found in '%s' — marking all platforms ERROR.", target_folder)
        for p in PLATFORMS:
            state = _set_status(state, target_folder, lang, p, STATUS_ERROR)
        return

    logger.info(
        "Found video: %s (%.1f MB)", video_path.name, video_path.stat().st_size / 1e6
    )

    # 4. Generate / load metadata (shared across all platforms)
    logger.info("[%s] Loading / generating Italian metadata…", target_folder)
    try:
        metadata = generate_metadata_for_folder(folder_path, lang=lang)
    except FileNotFoundError as exc:
        logger.error("[%s] Script file missing: %s — marking ERROR.", target_folder, exc)
        for p in PLATFORMS:
            state = _set_status(state, target_folder, lang, p, STATUS_ERROR)
        return
    except Exception as exc:
        logger.exception("[%s] Unexpected metadata error: %s", target_folder, exc)
        for p in PLATFORMS:
            state = _set_status(state, target_folder, lang, p, STATUS_ERROR)
        return

    _print_metadata_preview(target_folder, metadata)

    if metadata is None:
        logger.error("[%s] Metadata generation failed — marking ERROR.", target_folder)
        for p in PLATFORMS:
            state = _set_status(state, target_folder, lang, p, STATUS_ERROR)
        return

    # 5. Run the multi-platform upload pipeline
    start_time = time.time()
    state    = _process_folder(target_folder, folder_path, metadata, state, args.dry_run)
    it_state = state.get(target_folder, {}).get(lang, {})
    end_time = time.time()

    # 6. Archive ONLY when ALL THREE platforms are "success"
    if _all_done(it_state):
        logger.info(
            "[%s] All platforms successful — moving to Uploaded_Done…", target_folder
        )
        if not args.dry_run:
            move_folder_to_done(folder_path, UPLOADED_DONE_DIR)
        else:
            logger.info("[DRY-RUN] Would move '%s' to Uploaded_Done.", target_folder)
    else:
        incomplete = [p for p in PLATFORMS if _extract_status(it_state.get(p)) != STATUS_SUCCESS]
        logger.info(
            "[%s] Not all platforms complete — folder stays in Upload_Queue.\n"
            "       Still pending/failed: %s",
            target_folder, incomplete,
        )

    # 7. Final summary (with attempt counts)
    logger.info("=" * 64)
    logger.info("Run complete.")
    logger.info("[%s] Final platform statuses:", target_folder)
    for p in PLATFORMS:
        entry    = it_state.get(p, {})
        status   = _extract_status(entry)
        attempts = entry.get("attempts_count", 0) if isinstance(entry, dict) else "?"
        logger.info("  %-12s → %-25s (total attempts: %s)", p, status.upper(), attempts)
    logger.info("=" * 64)

    # 8. Send Telegram Notification (Only if all enabled platforms succeeded)
    all_enabled_success = all(
        _extract_status(it_state.get(p)) == STATUS_SUCCESS
        for p in PLATFORMS
        if _is_platform_enabled(p)
    )
    if all_enabled_success:
        if not args.dry_run:
            try:
                # Format time metrics
                duration = end_time - start_time
                mins = int(duration // 60)
                secs = int(duration % 60)
                if mins > 0:
                    duration_formatted = f"{mins} mins {secs} secs"
                else:
                    duration_formatted = f"{secs} secs"

                local_upload_time = datetime.now(_tz).strftime("%Y-%m-%d %H:%M (Egypt Time)")

                send_telegram_report(target_folder, local_upload_time, duration_formatted)
            except Exception as exc:
                logger.warning("Could not dispatch Telegram status report: %s", exc)
        else:
            logger.info("[%s] Skipping Telegram notification since this is a dry-run.", target_folder)
    else:
        logger.info(
            "[%s] Skipping Telegram notification since not all enabled platforms are successful.",
            target_folder,
        )


if __name__ == "__main__":
    main()
