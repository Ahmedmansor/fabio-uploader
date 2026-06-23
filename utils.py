"""
utils.py — Shared helper utilities for Fabio_Uploader.

Covers:
  • Logging setup (rotating file + coloured console)
  • Human-emulation delays (random sleep, per-character typing)
  • File / folder helpers (ensure_dirs, move_folder_to_done)
"""

import logging
import random
import shutil
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

from playwright.sync_api import Page

from config import LOGS_DIR


# ─── Logging ──────────────────────────────────────────────────────────────────

class _ColourFormatter(logging.Formatter):
    """ANSI colour codes for the console handler only."""
    GREY     = "\x1b[38;20m"
    YELLOW   = "\x1b[33;20m"
    RED      = "\x1b[31;20m"
    BOLD_RED = "\x1b[31;1m"
    CYAN     = "\x1b[36;20m"
    RESET    = "\x1b[0m"

    LEVEL_COLOURS = {
        logging.DEBUG:    GREY,
        logging.INFO:     CYAN,
        logging.WARNING:  YELLOW,
        logging.ERROR:    RED,
        logging.CRITICAL: BOLD_RED,
    }

    def format(self, record: logging.LogRecord) -> str:
        colour  = self.LEVEL_COLOURS.get(record.levelno, self.RESET)
        fmt_str = (
            f"{colour}%(asctime)s | %(levelname)-8s | %(name)s | %(message)s{self.RESET}"
        )
        formatter = logging.Formatter(fmt_str, datefmt="%Y-%m-%d %H:%M:%S")
        return formatter.format(record)


def setup_logging(level: int = logging.INFO) -> logging.Logger:
    """
    Configure the root logger with:
      - Rotating file handler → logs/uploader.log  (5 MB × 3 backups)
      - Coloured console handler

    Returns the root logger.
    """
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    log_file = LOGS_DIR / "uploader.log"

    root = logging.getLogger()
    root.setLevel(level)

    # Avoid adding duplicate handlers on re-import
    if root.handlers:
        return root

    # File handler
    file_handler = RotatingFileHandler(
        log_file, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    file_handler.setLevel(level)
    file_handler.setFormatter(
        logging.Formatter(
            "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )

    # Console handler
    console_handler = logging.StreamHandler()
    console_handler.setLevel(level)
    console_handler.setFormatter(_ColourFormatter())

    root.addHandler(file_handler)
    root.addHandler(console_handler)
    return root


# ─── Human-Emulation Helpers ─────────────────────────────────────────────────

def human_sleep(min_sec: float = 0.5, max_sec: float = 2.0) -> None:
    """Sleep for a random duration within [min_sec, max_sec] seconds."""
    time.sleep(random.uniform(min_sec, max_sec))


def slow_type(
    page: Page,
    selector: str,
    text: str,
    min_delay_ms: int = 80,
    max_delay_ms: int = 180,
) -> None:
    """
    Type text into a Playwright element character-by-character with a random
    per-keystroke delay to emulate a human typist.
    """
    delay = random.randint(min_delay_ms, max_delay_ms)
    page.type(selector, text, delay=delay)


def slow_fill(page: Page, selector: str, text: str) -> None:
    """Use Playwright's fill() to set a field value instantly, preceded by a human delay."""
    human_sleep(0.3, 0.8)
    page.fill(selector, text)


# ─── File / Folder Helpers ────────────────────────────────────────────────────

def ensure_dirs(*dirs: Path) -> None:
    """Create directories if they do not already exist."""
    for d in dirs:
        d.mkdir(parents=True, exist_ok=True)


def find_video_file(folder_path: Path, lang: str) -> Path | None:
    """
    Look for the video file <LANG>.mp4 (case-sensitive, uppercase) in *folder_path*.

    Returns the Path if found, or None if it doesn't exist.
    """
    video_path = folder_path / f"{lang.upper()}.mp4"
    return video_path if video_path.exists() else None


def find_thumbnail(folder_path: Path) -> Path | None:
    """
    Look for a thumbnail image file whose name starts with 'Thumbnail'
    (case-insensitive) inside *folder_path*.

    Supported extensions: jpg, jpeg, png, webp — searched in that order.
    Returns the first match found, or None if no thumbnail exists.

    NOTE: This function is ONLY called for Meta (Facebook + Instagram) uploads.
    The YouTube uploader never receives a thumbnail path.
    """
    for ext in ("jpg", "jpeg", "png", "webp"):
        for pattern in (f"Thumbnail*.{ext}", f"thumbnail*.{ext}"):
            matches = sorted(folder_path.glob(pattern))
            if matches:
                logger = logging.getLogger(__name__)
                logger.debug("Thumbnail found: %s", matches[0].name)
                return matches[0]
    return None


def move_folder_to_done(folder_path: Path, done_dir: Path) -> None:
    """
    Move a completed video folder into the Uploaded_Done directory.
    If the destination already exists, a _duplicate suffix is appended.
    """
    logger = logging.getLogger(__name__)
    destination = done_dir / folder_path.name
    if destination.exists():
        destination = done_dir / f"{folder_path.name}_duplicate"
        logger.warning(
            "Destination '%s' already exists — moving to '%s' instead.",
            done_dir / folder_path.name,
            destination,
        )
    shutil.move(str(folder_path), str(destination))
    logger.info("Moved '%s' → '%s'", folder_path.name, destination)
