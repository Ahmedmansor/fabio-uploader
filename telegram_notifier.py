"""
telegram_notifier.py — Premium status reports of upload runs to Telegram.

Reads configuration from pipeline_config.py (ENABLE_TELEGRAM) and credentials from .env.
Catches all exceptions internally to prevent notification errors from interrupting the uploader pipeline.
"""

import io
import json
import logging
import os
from pathlib import Path
import requests
from dotenv import load_dotenv
from PIL import Image

from config import (
    UPLOAD_STATE_FILE,
    SCHEDULE_TRACKER_FILE,
    UPLOAD_QUEUE_DIR,
    UPLOADED_DONE_DIR,
)
from pipeline_config import ENABLE_TELEGRAM

logger = logging.getLogger(__name__)


def send_telegram_report(
    folder_name: str,
    local_upload_time: str,
    time_taken: str,
) -> None:
    """
    Builds a luxurious, professional Markdown status report for folder_name,
    aggressively compresses the project cover image in-memory, and posts it to the Telegram Group.
    Falls back to a text message if image loading or sending fails.
    """
    if not ENABLE_TELEGRAM:
        logger.debug("[Telegram] Notification disabled in pipeline_config.py.")
        return

    # Load credentials
    load_dotenv()
    bot_token = os.getenv("Telegram_BOT_TOKEN") or os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_Groub_CHAT_ID")

    if not bot_token or not chat_id:
        logger.warning(
            "[Telegram] Cannot send notification: Telegram_BOT_TOKEN or "
            "TELEGRAM_Groub_CHAT_ID not configured in .env."
        )
        return

    bot_token = bot_token.strip()
    chat_id = chat_id.strip()

    # 1. Parse upload_state.json
    state = {}
    if UPLOAD_STATE_FILE.exists():
        try:
            with open(UPLOAD_STATE_FILE, "r", encoding="utf-8") as f:
                state = json.load(f)
        except Exception as exc:
            logger.warning("[Telegram] Could not load upload_state.json: %s", exc)

    project_state = state.get(folder_name, {})
    if not project_state:
        logger.warning("[Telegram] Project '%s' not found in upload_state.json", folder_name)
        return

    # Determine the language code (e.g. "IT")
    langs = list(project_state.keys())
    if not langs:
        logger.warning("[Telegram] No language keys found under '%s' in state", folder_name)
        return
    lang = langs[0]  # Usually "IT"
    platform_data = project_state[lang]

    # 2. Parse schedule_tracker.json
    tracker = {}
    if SCHEDULE_TRACKER_FILE.exists():
        try:
            with open(SCHEDULE_TRACKER_FILE, "r", encoding="utf-8") as f:
                tracker = json.load(f)
        except Exception as exc:
            logger.warning("[Telegram] Could not load schedule_tracker.json: %s", exc)

    # 3. Calculate metrics
    total_attempts = 0
    platform_lines = []

    for platform, details in platform_data.items():
        if isinstance(details, dict):
            status = details.get("status", "unknown")
            attempts = details.get("attempts_count", 0)
        else:
            status = str(details)
            attempts = 0
        
        try:
            total_attempts += int(attempts)
        except (ValueError, TypeError):
            pass

        # Look up scheduled times from tracker across all date keys
        scheduled_info = "N/A"
        for date_key in sorted(tracker.keys(), reverse=True):
            lang_data = tracker[date_key].get(lang, {})
            p_data = lang_data.get(platform, {})
            times = p_data.get("scheduled_times", [])
            if times:
                scheduled_info = f"{date_key} {times[-1]}"
                break

        # Select emoji based on status
        status_lower = status.lower()
        if status_lower == "success":
            emoji = "✅"
        elif status_lower == "error":
            emoji = "❌"
        elif status_lower == "loading":
            emoji = "⏳"
        elif "skip" in status_lower:
            emoji = "⏭️"
        else:
            emoji = "📝"

        platform_lines.append(f"• *{platform.upper()}*")
        platform_lines.append(f"  Status: {emoji} `{status.upper()}`")
        platform_lines.append(f"  Attempts: `{attempts}`")
        platform_lines.append(f"  Scheduled: `{scheduled_info}`")
        platform_lines.append("")

    # 4. Find project directory and load YouTube Title from metadata.json
    project_dir = UPLOAD_QUEUE_DIR / folder_name
    if not project_dir.exists():
        project_dir = UPLOADED_DONE_DIR / folder_name

    yt_title = "N/A"
    meta_path = project_dir / "metadata.json"
    if meta_path.exists():
        try:
            with open(meta_path, "r", encoding="utf-8") as f:
                metadata_content = json.load(f)
                yt_title = metadata_content.get(lang, {}).get("youtube", {}).get("title", "N/A")
        except Exception as exc:
            logger.warning("[Telegram] Could not load title from metadata.json: %s", exc)

    # 5. Format Premium Markdown message
    lines = [
        "✨ *FABIO UPLOADER REPORT* ✨",
        "==================================",
        f"📂 *Folder:* `{folder_name}`",
        f"🎬 *Title:* *{yt_title}*",
        f"🌐 *Language:* `{lang}`",
        "",
        "⏱️ *Upload Metrics:*",
        f"• *Egypt Time:* `{local_upload_time}`",
        f"• *Time Taken:* `{time_taken}`",
        f"• *Total System Attempts:* `{total_attempts}`",
        "",
        "📊 *Platform Execution Summary:*",
        "----------------------------------",
    ] + platform_lines + [
        "=================================="
    ]

    message = "\n".join(lines)

    # 6. Find Thumbnail.jpg
    thumbnail_path = project_dir / "Thumbnail.jpg"

    # 7. Delivery: Send photo first, fallback to text message
    sent_successfully = False

    if thumbnail_path.exists():
        try:
            logger.info("[Telegram] Cover thumbnail found. Compressing in-memory...")
            # Perform aggressive in-memory compression
            with Image.open(thumbnail_path) as img:
                max_width = 800
                if img.width > max_width:
                    ratio = max_width / float(img.width)
                    new_height = int(float(img.height) * ratio)
                    try:
                        resample_filter = Image.Resampling.LANCZOS
                    except AttributeError:
                        resample_filter = Image.ANTIALIAS
                    img = img.resize((max_width, new_height), resample_filter)

                # Convert RGBA to RGB to prevent JPEG save errors
                if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
                    img = img.convert("RGB")

                # Save aggressively to byte buffer
                img_byte_arr = io.BytesIO()
                img.save(img_byte_arr, format="JPEG", quality=30, optimize=True)
                img_byte_arr.seek(0)

            # Send photo payload
            logger.info("[Telegram] Dispatching sendPhoto API request...")
            url_photo = f"https://api.telegram.org/bot{bot_token}/sendPhoto"
            photo_payload = {
                "chat_id": chat_id,
                "caption": message,
                "parse_mode": "Markdown",
            }
            files = {
                "photo": ("Thumbnail.jpg", img_byte_arr, "image/jpeg")
            }
            resp = requests.post(url_photo, data=photo_payload, files=files, timeout=20)
            resp.raise_for_status()
            logger.info("[Telegram] Cover photo notification sent successfully.")
            sent_successfully = True

        except Exception as exc:
            logger.warning("[Telegram] Photo notification failed: %s. Retrying text-only message fallback...", exc)

    else:
        logger.info("[Telegram] No Thumbnail.jpg found. Proceeding with text-only message.")

    if not sent_successfully:
        # Fallback to text message
        try:
            logger.info("[Telegram] Dispatching sendMessage API request...")
            url_text = f"https://api.telegram.org/bot{bot_token}/sendMessage"
            text_payload = {
                "chat_id": chat_id,
                "text": message,
                "parse_mode": "Markdown",
            }
            resp = requests.post(url_text, json=text_payload, timeout=10)
            resp.raise_for_status()
            logger.info("[Telegram] Text notification sent successfully.")
        except Exception as exc:
            logger.error("[Telegram] Final notification dispatch failed: %s", exc)
