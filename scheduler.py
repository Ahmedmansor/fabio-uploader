"""
scheduler.py — Egypt-timezone scheduling logic for Fabio_Uploader.

Rule: Configurable frequency (Every day, Every other day, or 1 day on / 2 days off).
Reads and checks bookings directly from upload_state.json.

Unified upload_state.json schema (v4):
{
  "project_1": {
    "IT": {
      "youtube": {
        "status": "success",
        "scheduled_date": "2026-08-09",
        "scheduled_time": "21:00",
        "attempts_count": 1,
        "attempts_log": ["2026-08-09 04:30"]
      },
      "facebook": {
        "status": "success",
        "scheduled_date": "2026-08-09",
        "scheduled_time": "21:00",
        "attempts_count": 1,
        "attempts_log": ["2026-08-09 04:33"]
      },
      ...
    }
  }
}
"""

import json
import logging
from datetime import date, datetime, timedelta
from pathlib import Path
import pytz

from config import (
    EGYPT_TIMEZONE,
    LANGUAGES,
    MAX_UPLOADS_PER_LANG_PER_DAY,
    PEAK_TIMES,
    UPLOAD_STATE_FILE,
)
from pipeline_config import (
    META_PEAK_TIMES,
    TIKTOK_PEAK_TIMES,
    get_schedule_cadence_step_days,
)

logger = logging.getLogger(__name__)

_tz = pytz.timezone(EGYPT_TIMEZONE)


# ─── State Helpers ────────────────────────────────────────────────────────────

def load_upload_state() -> dict:
    """Load upload_state.json from disk. Returns {} if missing or corrupt."""
    if not UPLOAD_STATE_FILE.exists():
        return {}
    try:
        with UPLOAD_STATE_FILE.open("r", encoding="utf-8") as fh:
            return json.load(fh)
    except json.JSONDecodeError as exc:
        logger.error("upload_state.json corrupted (%s) — starting fresh.", exc)
        return {}


def get_booked_dates(state: dict, lang: str, platform: str) -> set[str]:
    """Return a set of date_keys ('YYYY-MM-DD') that are already booked for this lang/platform."""
    booked = set()
    for folder_name, folder_data in state.items():
        if not isinstance(folder_data, dict):
            continue
        lang_data = folder_data.get(lang, {})
        if not isinstance(lang_data, dict):
            continue
        p_data = lang_data.get(platform, {})
        if not isinstance(p_data, dict):
            continue

        st_date = p_data.get("scheduled_date")
        status = p_data.get("status", "")
        # Booked if successfully uploaded with a scheduled date
        if st_date and status == "success":
            booked.add(st_date)
    return booked


def get_latest_scheduled_date(state: dict, lang: str, platform: str) -> date | None:
    """Find the most future scheduled date for this lang/platform."""
    booked = get_booked_dates(state, lang, platform)
    dates: list[date] = []
    for d_str in booked:
        try:
            dates.append(datetime.strptime(d_str, "%Y-%m-%d").date())
        except ValueError:
            pass
    return max(dates) if dates else None


# ─── Internal Helpers ─────────────────────────────────────────────────────────

def _build_candidate(target_date: date, slot: dict) -> datetime:
    """Build a timezone-aware datetime from a calendar date + slot dict."""
    naive = datetime(target_date.year, target_date.month, target_date.day, slot["hour"], slot["minute"], 0)
    return _tz.localize(naive)


# ─── Public Scheduling API ────────────────────────────────────────────────────

def get_next_slot(lang: str, platform: str) -> tuple[datetime, str]:
    """
    Find the next available upload slot for *lang* on *platform* (YouTube).
    Applies the cadence step configured in pipeline_config.py.

    Returns:
        (scheduled_datetime, date_key_str)  e.g. (datetime(...), "2026-08-09")
    """
    state = load_upload_state()
    booked = get_booked_dates(state, lang, platform)
    latest_date = get_latest_scheduled_date(state, lang, platform)
    step = get_schedule_cadence_step_days()

    now = datetime.now(_tz)
    today = now.date()
    trains = PEAK_TIMES[lang]
    slot = trains[0]  # Primary peak slot

    # Determine start candidate date
    if latest_date is None or latest_date < today:
        candidate_date = today
    else:
        candidate_date = latest_date + timedelta(days=step)

    # Search for the next available slot
    for _ in range(60):
        # If candidate is today, verify peak time is in the future
        candidate_dt = _build_candidate(candidate_date, slot)
        if candidate_date == today and candidate_dt <= now + timedelta(minutes=2):
            # Peak time for today has passed, advance
            if latest_date is None or latest_date < today:
                candidate_date = today + timedelta(days=1)
            else:
                candidate_date = max(today + timedelta(days=1), latest_date + timedelta(days=step))
            continue

        date_key = candidate_date.isoformat()
        if date_key not in booked:
            logger.info(
                "[%s][%s] Next slot (cadence step=%d) → %s at %s (Egypt TZ)",
                lang, platform, step, date_key, candidate_dt.strftime("%H:%M %Z"),
            )
            return candidate_dt, date_key

        candidate_date += timedelta(days=step)

    raise RuntimeError(
        f"[{lang}][{platform}] No available slot found within search window. "
        "Check upload_state.json for anomalies."
    )


def get_next_slot_meta(lang: str) -> tuple[datetime, str]:
    """
    Find the next available slot for Meta (Facebook + Instagram together).
    Applies the cadence step configured in pipeline_config.py.

    Returns:
        (scheduled_datetime, date_key_str)
    """
    state = load_upload_state()
    fb_booked = get_booked_dates(state, lang, "facebook")
    ig_booked = get_booked_dates(state, lang, "instagram")
    all_booked = fb_booked | ig_booked

    fb_latest = get_latest_scheduled_date(state, lang, "facebook")
    ig_latest = get_latest_scheduled_date(state, lang, "instagram")
    latest_candidates = [d for d in (fb_latest, ig_latest) if d is not None]
    latest_date = max(latest_candidates) if latest_candidates else None

    step = get_schedule_cadence_step_days()
    now = datetime.now(_tz)
    today = now.date()
    trains = META_PEAK_TIMES[lang]
    slot = trains[0]

    if latest_date is None or latest_date < today:
        candidate_date = today
    else:
        candidate_date = latest_date + timedelta(days=step)

    for _ in range(60):
        candidate_dt = _build_candidate(candidate_date, slot)
        if candidate_date == today and candidate_dt <= now + timedelta(minutes=2):
            if latest_date is None or latest_date < today:
                candidate_date = today + timedelta(days=1)
            else:
                candidate_date = max(today + timedelta(days=1), latest_date + timedelta(days=step))
            continue

        date_key = candidate_date.isoformat()
        if date_key not in all_booked:
            logger.info(
                "[%s][meta] Next slot (cadence step=%d) → %s at %s (Egypt TZ)",
                lang, step, date_key, candidate_dt.strftime("%H:%M %Z"),
            )
            return candidate_dt, date_key

        candidate_date += timedelta(days=step)

    raise RuntimeError(
        f"[{lang}][meta] No available Meta slot found within search window. "
        "Check upload_state.json for anomalies."
    )


def get_next_slot_tiktok(lang: str) -> tuple[datetime, str]:
    """
    Find the next available slot for TikTok.
    Applies the cadence step configured in pipeline_config.py.

    Returns:
        (scheduled_datetime, date_key_str)
    """
    state = load_upload_state()
    booked = get_booked_dates(state, lang, "tiktok")
    latest_date = get_latest_scheduled_date(state, lang, "tiktok")
    step = get_schedule_cadence_step_days()

    now = datetime.now(_tz)
    today = now.date()
    trains = TIKTOK_PEAK_TIMES[lang]
    slot = trains[0]

    if latest_date is None or latest_date < today:
        candidate_date = today
    else:
        candidate_date = latest_date + timedelta(days=step)

    for _ in range(60):
        candidate_dt = _build_candidate(candidate_date, slot)
        if candidate_date == today and candidate_dt <= now + timedelta(minutes=2):
            if latest_date is None or latest_date < today:
                candidate_date = today + timedelta(days=1)
            else:
                candidate_date = max(today + timedelta(days=1), latest_date + timedelta(days=step))
            continue

        date_key = candidate_date.isoformat()
        if date_key not in booked:
            logger.info(
                "[%s][tiktok] Next slot (cadence step=%d) → %s at %s (Egypt TZ)",
                lang, step, date_key, candidate_dt.strftime("%H:%M %Z"),
            )
            return candidate_dt, date_key

        candidate_date += timedelta(days=step)

    raise RuntimeError(
        f"[{lang}][tiktok] No available TikTok slot found within search window. "
        "Check upload_state.json for anomalies."
    )


def format_scheduled_time_for_yt(dt: datetime) -> tuple[str, str]:
    """
    Format a datetime for YouTube Studio's date and time input fields.

    Returns:
        (date_str, time_str)  e.g. ("06/24/2026", "09:00 PM")
    """
    date_str = dt.strftime("%m/%d/%Y")   # MM/DD/YYYY
    time_str = dt.strftime("%I:%M %p")   # hh:MM AM/PM
    return date_str, time_str
