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

from playwright.sync_api import Page, Locator

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


def set_file_via_cdp(page: Page, element, file_path: Path) -> None:
    """
    Set files on a file input (either a Locator or ElementHandle) using CDP,
    bypassing Playwright's 50MB file transfer limit. Supports nested iframes
    (both same-process and cross-process OOPIFs) and shadow DOMs by recursively 
    traversing the resolved DOM tree in the appropriate CDP session.
    """
    import logging
    logger = logging.getLogger(__name__)

    abs_path = file_path.resolve()
    if not abs_path.exists():
        raise FileNotFoundError(f"File to upload not found: {abs_path}")

    logger.info("[CDP Upload] Injecting file directly via CDP: %s", abs_path.name)

    # 1. Resolve to ElementHandle
    from playwright.sync_api import Locator
    if isinstance(element, Locator):
        element.first.wait_for(state="attached")
        element_handle = element.first.element_handle()
    else:
        element_handle = element

    if not element_handle:
        raise RuntimeError("Target element handle is None.")

    # 2. Get owner frame
    frame = element_handle.owner_frame()

    # 3. Check if connected. If detached, temporarily attach it.
    was_connected = element_handle.evaluate("el => el.isConnected")
    if not was_connected:
        logger.info("[CDP Upload] Element is detached. Temporarily attaching to DOM…")
        element_handle.evaluate("el => { el.ownerDocument.body.appendChild(el); el.setAttribute('data-playwright-upload-target', 'true'); }")
    else:
        element_handle.evaluate("el => el.setAttribute('data-playwright-upload-target', 'true')")

    try:
        # 4. Open CDP session on the FRAME with fallback to PAGE
        client = None
        if frame:
            try:
                client = page.context.new_cdp_session(frame)
                logger.debug("CDP session opened on frame.")
            except Exception as exc:
                logger.debug("Failed to open CDP session on frame, falling back to page: %s", exc)
        
        if not client:
            client = page.context.new_cdp_session(page)
            logger.debug("CDP session opened on page.")
        
        # 5. Retrieve entire document root recursively with pierce=True
        doc = client.send("DOM.getDocument", {"depth": -1, "pierce": True})
        
        # 6. Recursively search for the target attribute in the node tree
        def _find_node_in_tree(node: dict) -> int | None:
            attrs = node.get("attributes", [])
            for i in range(0, len(attrs), 2):
                if attrs[i] == "data-playwright-upload-target" and attrs[i+1] == "true":
                    return node["nodeId"]

            for child in node.get("children", []):
                res = _find_node_in_tree(child)
                if res is not None:
                    return res

            if "contentDocument" in node:
                res = _find_node_in_tree(node["contentDocument"])
                if res is not None:
                    return res

            for shadow_root in node.get("shadowRoots", []):
                res = _find_node_in_tree(shadow_root)
                if res is not None:
                    return res

            return None

        node_id = _find_node_in_tree(doc["root"])
        
        if not node_id:
            raise RuntimeError("Failed to resolve target element nodeId via CDP search.")

        # 7. Set the file path
        client.send("DOM.setFileInputFiles", {
            "files": [str(abs_path)],
            "nodeId": node_id
        })
        logger.info("[CDP Upload] File successfully injected.")
    finally:
        # 8. Clean up: remove attribute and detach if it was originally detached
        try:
            if not was_connected:
                element_handle.evaluate("el => { el.removeAttribute('data-playwright-upload-target'); el.remove(); }")
                logger.info("[CDP Upload] Detached element from DOM.")
            else:
                element_handle.evaluate("el => el.removeAttribute('data-playwright-upload-target')")
        except Exception:
            pass

