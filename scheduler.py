"""
scheduler.py — Egypt-timezone scheduling logic for Fabio_Uploader.

Rule: ONE video per day, per language. The upload is always scheduled for
the configured peak time on the next available day.

schedule_tracker.json schema:
{
    "2026-06-22": { "IT": 1 },
    "2026-06-23": { "IT": 0 },
    ...
}
Values:
    0 = no upload scheduled on this day yet
    1 = one upload already claimed for this day (day is FULL for IT)

Algorithm:
  Starting from today, find the first calendar day where the IT count is 0.
  Return the peak-time datetime for that day.
  If today's peak time has already passed, skip to tomorrow.

This means:
  - First run  → finds today (if peak time is in the future) or tomorrow.
  - Second run → finds the next free day automatically.
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


# ─── Tracker I/O (atomic) ─────────────────────────────────────────────────────

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
    """Write tracker atomically (temp → rename)."""
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


# ─── Core Scheduling Logic ────────────────────────────────────────────────────

def _build_candidate(date: "datetime.date", slot: dict) -> datetime:
    """
    Build a timezone-aware datetime from a calendar date and a slot dict
    (e.g. {"hour": 21, "minute": 0}).
    """
    naive = datetime(date.year, date.month, date.day, slot["hour"], slot["minute"], 0)
    return _tz.localize(naive)


def get_next_slot(lang: str) -> tuple[datetime, str]:
    """
    Find the next available upload slot for *lang* (one per day rule).

    Scans forward from today until it finds a day with 0 uploads for *lang*
    AND whose peak time is still more than 2 minutes in the future.

    Returns:
        (scheduled_datetime, date_key)  e.g. (datetime(2026-06-23 21:00 EET), "2026-06-23")

    Raises:
        RuntimeError: if no slot is found within 30 days.
    """
    tracker = load_schedule_tracker()
    now     = datetime.now(_tz)
    today   = now.date()
    trains  = PEAK_TIMES[lang]   # e.g. [{"hour": 21, "minute": 0}]

    for day_offset in range(31):   # safety guard: max 30 days ahead
        candidate_date = today + timedelta(days=day_offset)
        date_key       = candidate_date.isoformat()
        day_count      = tracker.get(date_key, {}).get(lang, 0)

        if day_count >= MAX_UPLOADS_PER_LANG_PER_DAY:
            logger.debug(
                "[%s] %s already fully booked (%d/%d) — skipping.",
                lang, date_key, day_count, MAX_UPLOADS_PER_LANG_PER_DAY,
            )
            continue

        # Check each peak-time slot for this day
        for slot in trains:
            candidate_dt = _build_candidate(candidate_date, slot)
            if candidate_dt > now + timedelta(minutes=2):
                logger.info(
                    "[%s] Next slot → %s at %s (Egypt TZ)",
                    lang, date_key, candidate_dt.strftime("%H:%M %Z"),
                )
                return candidate_dt, date_key

    raise RuntimeError(
        f"[{lang}] No available upload slot found within the next 30 days. "
        "Check schedule_tracker.json for anomalies."
    )


def increment_upload_count(lang: str, date_key: str) -> None:
    """
    Mark one upload as consumed for *lang* on *date_key*.
    Must be called ONLY after a confirmed successful upload.
    """
    tracker = load_schedule_tracker()
    if date_key not in tracker:
        tracker[date_key] = {l: 0 for l in LANGUAGES}
    tracker[date_key].setdefault(lang, 0)
    tracker[date_key][lang] += 1
    save_schedule_tracker(tracker)
    logger.info(
        "[%s] Incremented schedule tracker for %s → total: %d",
        lang, date_key, tracker[date_key][lang],
    )


def get_daily_count(lang: str, date_key: str | None = None) -> int:
    """
    Return how many uploads are already tracked for *lang* on *date_key*.
    Defaults to today (Egypt TZ).
    """
    if date_key is None:
        date_key = datetime.now(_tz).date().isoformat()
    tracker = load_schedule_tracker()
    return tracker.get(date_key, {}).get(lang, 0)


def format_scheduled_time_for_yt(dt: datetime) -> tuple[str, str]:
    """
    Convert a datetime to the date and time strings expected by YouTube Studio.

    Returns:
        (date_str, time_str)  e.g. ("06/23/2026", "09:00 PM")
    """
    date_str = dt.strftime("%m/%d/%Y")   # MM/DD/YYYY
    time_str = dt.strftime("%I:%M %p")   # hh:MM AM/PM
    return date_str, time_str
