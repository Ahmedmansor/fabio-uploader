"""
scheduler.py — Egypt-timezone scheduling logic for Fabio_Uploader.

Rule: ONE upload per day, per language, per platform.

schedule_tracker.json schema (v2 — per-platform):
{
    "2026-06-23": {
        "IT": {
            "youtube":   {"count": 1, "scheduled_times": ["21:00"]},
            "facebook":  {"count": 1, "scheduled_times": ["21:00"]},
            "instagram": {"count": 1, "scheduled_times": ["21:00"]}
        }
    }
}

Algorithm:
  For YouTube : reads PEAK_TIMES from config.py.
  For Meta    : reads META_PEAK_TIMES from pipeline_config.py.
  Both scan forward from today until a day is found where the platform's
  count is 0 AND the peak-time slot is more than 2 minutes in the future.
"""

import json
import logging
import os
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

import pytz

from config import (
    EGYPT_TIMEZONE,
    LANGUAGES,
    MAX_UPLOADS_PER_LANG_PER_DAY,
    PEAK_TIMES,
    SCHEDULE_TRACKER_FILE,
)

logger = logging.getLogger(__name__)

_tz = pytz.timezone(EGYPT_TIMEZONE)


# ─── Tracker I/O (atomic write) ───────────────────────────────────────────────

def load_schedule_tracker() -> dict:
    """Load schedule_tracker.json. Returns {} if missing or corrupt."""
    if not SCHEDULE_TRACKER_FILE.exists():
        return {}
    try:
        with SCHEDULE_TRACKER_FILE.open("r", encoding="utf-8") as fh:
            return json.load(fh)
    except json.JSONDecodeError as exc:
        logger.error("schedule_tracker.json corrupted (%s) — starting fresh.", exc)
        return {}


def save_schedule_tracker(tracker: dict) -> None:
    """Write tracker atomically (temp-file → rename) to prevent corruption."""
    parent = SCHEDULE_TRACKER_FILE.parent
    parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=parent, suffix=".tmp", prefix="sched_")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(tracker, fh, ensure_ascii=False, indent=2)
        os.replace(tmp_path, SCHEDULE_TRACKER_FILE)
        logger.debug("schedule_tracker.json saved.")
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


# ─── Internal Helpers ─────────────────────────────────────────────────────────

def _build_candidate(date: "datetime.date", slot: dict) -> datetime:
    """Build a timezone-aware datetime from a calendar date + slot dict."""
    naive = datetime(date.year, date.month, date.day, slot["hour"], slot["minute"], 0)
    return _tz.localize(naive)


def _get_platform_count(
    tracker: dict, date_key: str, lang: str, platform: str
) -> int:
    """Safely read the upload count for one platform on one day. Defaults to 0."""
    return (
        tracker
        .get(date_key, {})
        .get(lang, {})
        .get(platform, {})
        .get("count", 0)
    )


def _ensure_platform_entry(
    tracker: dict, date_key: str, lang: str, platform: str
) -> None:
    """Create nested dict entries without overwriting existing data."""
    tracker.setdefault(date_key, {})
    tracker[date_key].setdefault(lang, {})
    tracker[date_key][lang].setdefault(platform, {"count": 0, "scheduled_times": []})


# ─── Public Scheduling API ────────────────────────────────────────────────────

def get_next_slot(lang: str, platform: str) -> tuple[datetime, str]:
    """
    Find the next available upload slot for *lang* on *platform* (YouTube).

    Reads peak times from config.PEAK_TIMES.

    Returns:
        (scheduled_datetime, date_key_str)  e.g. (datetime(...), "2026-06-24")

    Raises:
        RuntimeError: if no free slot is found within 30 days.
    """
    tracker = load_schedule_tracker()
    now     = datetime.now(_tz)
    today   = now.date()
    trains  = PEAK_TIMES[lang]

    for day_offset in range(31):
        candidate_date = today + timedelta(days=day_offset)
        date_key       = candidate_date.isoformat()
        day_count      = _get_platform_count(tracker, date_key, lang, platform)

        if day_count >= MAX_UPLOADS_PER_LANG_PER_DAY:
            logger.debug(
                "[%s][%s] %s fully booked (%d/%d) — skipping.",
                lang, platform, date_key, day_count, MAX_UPLOADS_PER_LANG_PER_DAY,
            )
            continue

        for slot in trains:
            candidate_dt = _build_candidate(candidate_date, slot)
            if candidate_dt > now + timedelta(minutes=2):
                logger.info(
                    "[%s][%s] Next slot → %s at %s (Egypt TZ)",
                    lang, platform, date_key, candidate_dt.strftime("%H:%M %Z"),
                )
                return candidate_dt, date_key

    raise RuntimeError(
        f"[{lang}][{platform}] No available slot found within 30 days. "
        "Check schedule_tracker.json for anomalies."
    )


def get_next_slot_meta(lang: str) -> tuple[datetime, str]:
    """
    Find the next available slot for Meta (Facebook + Instagram together).

    Reads peak times from pipeline_config.META_PEAK_TIMES.
    Both 'facebook' and 'instagram' counts must be below the daily cap
    on the chosen day (they are always uploaded in the same session).

    Returns:
        (scheduled_datetime, date_key_str)

    Raises:
        RuntimeError: if no free slot is found within 30 days.
    """
    from pipeline_config import META_PEAK_TIMES  # lazy import avoids circular deps

    tracker = load_schedule_tracker()
    now     = datetime.now(_tz)
    today   = now.date()
    trains  = META_PEAK_TIMES[lang]

    for day_offset in range(31):
        candidate_date = today + timedelta(days=day_offset)
        date_key       = candidate_date.isoformat()
        fb_count       = _get_platform_count(tracker, date_key, lang, "facebook")
        ig_count       = _get_platform_count(tracker, date_key, lang, "instagram")

        if (fb_count >= MAX_UPLOADS_PER_LANG_PER_DAY or
                ig_count >= MAX_UPLOADS_PER_LANG_PER_DAY):
            logger.debug(
                "[%s][meta] %s fully booked (fb=%d, ig=%d) — skipping.",
                lang, date_key, fb_count, ig_count,
            )
            continue

        for slot in trains:
            candidate_dt = _build_candidate(candidate_date, slot)
            if candidate_dt > now + timedelta(minutes=2):
                logger.info(
                    "[%s][meta] Next slot → %s at %s (Egypt TZ)",
                    lang, date_key, candidate_dt.strftime("%H:%M %Z"),
                )
                return candidate_dt, date_key

    raise RuntimeError(
        f"[{lang}][meta] No available Meta slot found within 30 days. "
        "Check schedule_tracker.json for anomalies."
    )


def increment_upload_count(
    lang: str,
    platform: str,
    date_key: str,
    scheduled_time: datetime,
) -> None:
    """
    Record one successful upload for *lang* + *platform* on *date_key*.

    Increments the count and appends the scheduled time ("HH:MM") to the
    scheduled_times list for full audit transparency.

    MUST be called ONLY after a confirmed successful upload.
    """
    tracker  = load_schedule_tracker()
    time_str = scheduled_time.strftime("%H:%M")

    _ensure_platform_entry(tracker, date_key, lang, platform)
    tracker[date_key][lang][platform]["count"] += 1
    tracker[date_key][lang][platform]["scheduled_times"].append(time_str)

    save_schedule_tracker(tracker)
    logger.info(
        "[%s][%s] Tracker updated: %s → count=%d, times=%s",
        lang, platform, date_key,
        tracker[date_key][lang][platform]["count"],
        tracker[date_key][lang][platform]["scheduled_times"],
    )


def get_daily_count(lang: str, platform: str, date_key: str | None = None) -> int:
    """
    Return the upload count for *lang* + *platform* on *date_key*.
    Defaults to today (Egypt TZ) if date_key is not provided.
    """
    if date_key is None:
        date_key = datetime.now(_tz).date().isoformat()
    return _get_platform_count(load_schedule_tracker(), date_key, lang, platform)


def format_scheduled_time_for_yt(dt: datetime) -> tuple[str, str]:
    """
    Format a datetime for YouTube Studio's date and time input fields.

    Returns:
        (date_str, time_str)  e.g. ("06/24/2026", "09:00 PM")
    """
    date_str = dt.strftime("%m/%d/%Y")   # MM/DD/YYYY
    time_str = dt.strftime("%I:%M %p")   # hh:MM AM/PM
    return date_str, time_str
