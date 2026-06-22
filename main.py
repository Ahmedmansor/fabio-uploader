"""
main.py — Orchestrator for Fabio_Uploader Phase 1.

Pipeline per run:
  1. Scan Upload_Queue for ALL folders that contain IT.mp4.
  2. Process them in alphabetical order (one at a time).
  3. For the first folder whose IT upload is NOT yet "success":
       a. Generate / load metadata (Gemini + metadata.json cache).
       b. Compute the next available schedule slot (1 per day, 21:00 Egypt TZ).
       c. Start the AdsPower IT profile + attach Playwright.
       d. Navigate to YouTube Studio → verify @FabioEgyptItaly → upload → schedule.
       e. On success: increment schedule_tracker + mark "success" in upload_state.json.
       f. ONLY move the folder to Uploaded_Done when the upload is 100% confirmed.
  4. Stop after one folder per run (queue blocking: one at a time).

Usage:
  python main.py                         # Auto-find first pending folder
  python main.py --folder project_1      # Override to a specific folder name
  python main.py --dry-run               # Simulate: calls Gemini, skips browser
  python main.py --log-level DEBUG       # Verbose output
  python main.py --dry-run --folder x   # Dry-run a specific folder

State file: upload_state.json
  Schema: { "<folder_name>": { "IT": "pending"|"loading"|"success"|"error" } }
"""

import argparse
import json
import logging
import os
import sys
import tempfile
import time
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
from metadata_agent import generate_metadata_for_folder
from scheduler import get_next_slot, increment_upload_count
from utils import ensure_dirs, find_video_file, human_sleep, move_folder_to_done, setup_logging
from youtube_uploader import YouTubeUploader

# Force UTF-8 encoding for standard output and error to prevent UnicodeEncodeError on Windows.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

load_dotenv()
logger = logging.getLogger(__name__)

# ─── Status Constants ─────────────────────────────────────────────────────────
STATUS_PENDING = "pending"
STATUS_LOADING = "loading"
STATUS_SUCCESS = "success"
STATUS_ERROR   = "error"
RESUMABLE_STATUSES = {STATUS_PENDING, STATUS_LOADING, STATUS_ERROR}

_tz = pytz.timezone(EGYPT_TIMEZONE)


# ─── State Management ─────────────────────────────────────────────────────────

def _load_state() -> dict:
    if not UPLOAD_STATE_FILE.exists():
        return {}
    try:
        with UPLOAD_STATE_FILE.open("r", encoding="utf-8") as fh:
            return json.load(fh)
    except json.JSONDecodeError as exc:
        logger.error("upload_state.json corrupted (%s) — starting fresh.", exc)
        return {}


def _save_state(state: dict) -> None:
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


def _set_status(state: dict, folder: str, lang: str, status: str) -> dict:
    """Update one cell in the state dict and persist to disk immediately."""
    if folder not in state:
        state[folder] = {l: STATUS_PENDING for l in LANGUAGES}
    state[folder][lang] = status
    _save_state(state)
    logger.info("[%s][%s] Status → %s", folder, lang, status.upper())
    return state


def _sync_state_with_queue(state: dict) -> dict:
    """
    Add any folders in Upload_Queue that are not yet tracked in state.
    Only folders that actually contain IT.mp4 are registered.
    """
    if not UPLOAD_QUEUE_DIR.exists():
        return state
    added = []
    for folder_path in sorted(UPLOAD_QUEUE_DIR.iterdir()):
        if not folder_path.is_dir():
            continue
        name = folder_path.name
        if name not in state and find_video_file(folder_path, "IT") is not None:
            state[name] = {l: STATUS_PENDING for l in LANGUAGES}
            added.append(name)
    if added:
        logger.info("Registered %d new folder(s) in state: %s", len(added), added)
        _save_state(state)
    return state


# ─── Queue Scanning ───────────────────────────────────────────────────────────

def _find_pending_folder(state: dict, override: str | None = None) -> str | None:
    """
    Find the first folder in Upload_Queue that:
      - Contains IT.mp4
      - Has IT status in RESUMABLE_STATUSES (pending / loading / error)

    If *override* is provided, use that folder name directly (for debugging).
    Returns the folder name string, or None if everything is done.
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

    for folder_path in sorted(UPLOAD_QUEUE_DIR.iterdir()):
        if not folder_path.is_dir():
            continue
        name = folder_path.name
        if find_video_file(folder_path, "IT") is None:
            continue   # no IT.mp4 in this folder — skip
        status = state.get(name, {}).get("IT", STATUS_PENDING)
        if status in RESUMABLE_STATUSES:
            logger.debug("'%s' is pending/error for IT — selected.", name)
            return name

    return None   # everything is already successful


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
        print(f"  TITLE       : {meta['title']}")
        print("  DESCRIPTION :")
        for line in meta["description"].splitlines():
            print(f"                {line}")
        tags_str = ", ".join(meta.get("tags", []))
        print(f"  TAGS        : {tags_str}")
    print(f"\n{'=' * 64}\n")


# ─── Single Upload with Retry ─────────────────────────────────────────────────

def _attempt_upload(
    folder: str,
    video_path: Path,
    metadata: dict,
    state: dict,
    dry_run: bool,
) -> tuple[bool, dict]:
    """
    Try to upload once, retry once on failure.
    Returns (success: bool, updated_state: dict).
    """
    lang = "IT"

    # Compute the schedule slot BEFORE marking loading (so tracker stays clean)
    scheduled_time, date_key = get_next_slot(lang)

    state = _set_status(state, folder, lang, STATUS_LOADING)
    uploader = YouTubeUploader(lang=lang)

    for attempt in range(1, 3):
        logger.info("[%s][%s] Upload attempt %d / 2…", folder, lang, attempt)
        success = uploader.upload(
            video_path=video_path,
            metadata=metadata,
            scheduled_time=scheduled_time,
            dry_run=dry_run,
        )

        if success:
            state = _set_status(state, folder, lang, STATUS_SUCCESS)
            increment_upload_count(lang, date_key)
            return True, state

        if attempt == 1:
            logger.warning(
                "[%s][%s] Attempt 1 failed — cooling down %ds before retry…",
                folder, lang, UPLOAD_RETRY_COOLDOWN_SEC,
            )
            time.sleep(UPLOAD_RETRY_COOLDOWN_SEC)

    logger.error("[%s][%s] Both attempts failed — marking ERROR.", folder, lang)
    state = _set_status(state, folder, lang, STATUS_ERROR)
    return False, state


# ─── CLI Argument Parsing ─────────────────────────────────────────────────────

def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Fabio_Uploader — YouTube Shorts Auto-Uploader (Phase 1: IT)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Simulate: calls Gemini for metadata preview but skips all browser actions.",
    )
    parser.add_argument(
        "--folder", default=None,
        help="Override auto-scan and process a specific folder name in Upload_Queue.",
    )
    parser.add_argument(
        "--log-level", default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging verbosity (default: INFO).",
    )
    return parser.parse_args()


# ─── Pre-flight Check ─────────────────────────────────────────────────────────

def _pre_flight(dry_run: bool) -> None:
    ensure_dirs(UPLOAD_QUEUE_DIR, UPLOADED_DONE_DIR, LOGS_DIR)
    logger.info("=" * 64)
    logger.info("  Fabio_Uploader — YouTube Shorts Auto-Uploader")
    logger.info("  Base dir  : %s", BASE_DIR)
    logger.info("  Dry-run   : %s", dry_run)
    logger.info("=" * 64)

    if not UPLOAD_QUEUE_DIR.exists() or not any(UPLOAD_QUEUE_DIR.iterdir()):
        logger.warning(
            "Upload_Queue is empty. Add video folders (each containing IT.mp4 "
            "and script.txt or script.srt)."
        )


# ─── Main ─────────────────────────────────────────────────────────────────────

def main() -> None:
    args = _parse_args()
    setup_logging(getattr(logging, args.log_level))

    _pre_flight(args.dry_run)

    # 1. Load & sync state
    state = _load_state()
    state = _sync_state_with_queue(state)

    # 2. Find the folder to process
    target_folder = _find_pending_folder(state, override=args.folder)

    if target_folder is None:
        logger.info("✅ All folders in Upload_Queue are fully uploaded. Nothing to do.")
        return

    folder_path = UPLOAD_QUEUE_DIR / target_folder
    lang        = "IT"

    logger.info(
        "🎬 Processing folder: '%s'  (status: %s)",
        target_folder,
        state.get(target_folder, {}).get(lang, "new"),
    )

    # 3. Find IT.mp4
    video_path = find_video_file(folder_path, lang)
    if video_path is None:
        logger.error(
            "IT.mp4 not found in '%s'. Skipping this folder.", target_folder
        )
        state = _set_status(state, target_folder, lang, STATUS_ERROR)
        return

    logger.info("Found video: %s (%.1f MB)", video_path.name, video_path.stat().st_size / 1e6)

    # 4. Generate / load metadata
    logger.info("[%s] Loading / generating Italian metadata…", target_folder)
    try:
        metadata = generate_metadata_for_folder(folder_path, lang=lang)
    except FileNotFoundError as exc:
        logger.error("[%s] Script file missing: %s — marking ERROR.", target_folder, exc)
        state = _set_status(state, target_folder, lang, STATUS_ERROR)
        return
    except Exception as exc:
        logger.exception("[%s] Unexpected metadata error: %s", target_folder, exc)
        state = _set_status(state, target_folder, lang, STATUS_ERROR)
        return

    # 5. Preview metadata (always shown, even in dry-run)
    _print_metadata_preview(target_folder, metadata)

    if metadata is None:
        logger.error("[%s] Metadata generation failed — marking ERROR.", target_folder)
        state = _set_status(state, target_folder, lang, STATUS_ERROR)
        return

    # 6. Upload (with one automatic retry on failure)
    success, state = _attempt_upload(
        folder=target_folder,
        video_path=video_path,
        metadata=metadata,
        state=state,
        dry_run=args.dry_run,
    )

    # 7. Archive on success
    if success:
        logger.info("[%s] ✅ Upload confirmed — moving to Uploaded_Done…", target_folder)
        if not args.dry_run:
            move_folder_to_done(folder_path, UPLOADED_DONE_DIR)
        else:
            logger.info("[DRY-RUN] Would move '%s' to Uploaded_Done.", target_folder)
    else:
        logger.warning(
            "[%s] ⛔ Upload failed — folder remains in Upload_Queue. "
            "Re-run the script to retry.",
            target_folder,
        )

    # 8. Final summary
    logger.info("=" * 64)
    logger.info("Run complete.")
    it_status = state.get(target_folder, {}).get(lang, "?")
    logger.info("[%s] Final IT status: %s", target_folder, it_status.upper())
    logger.info("=" * 64)


if __name__ == "__main__":
    main()
