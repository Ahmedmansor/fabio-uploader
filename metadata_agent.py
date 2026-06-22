"""
metadata_agent.py — Gemini API integration for YouTube Shorts metadata.

Reads the episode script (script.txt OR script.srt), generates an Italian
title and description optimised for Fabio Egypt's travel channel, and caches
the result to metadata.json inside the episode folder.

Caching logic:
  - If metadata.json already exists with valid IT data → use it (no API call).
  - If it doesn't exist or is missing IT data → call Gemini, save to cache.

SRT support:
  If the script file is a .srt subtitle file, all timestamp lines and
  sequence-number lines are stripped, leaving only the spoken dialogue text.

Output schema (stored in metadata.json under the "IT" key):
{
    "title":       "Catchy Italian title #Shorts",
    "description": "Hook + summary\\n\\n<disclaimer>\\n\\n<hashtags>",
    "tags":        ["tag1", "tag2", ...]
}
"""

import json
import logging
import os
import re
import tempfile
import time
from pathlib import Path

from google import genai
from google.genai import types
from dotenv import load_dotenv

from config import (
    DISCLAIMERS,
    GEMINI_MODEL,
    GEMINI_RPM_SLEEP,
    HASHTAGS,
    LANGUAGE_CONTEXTS,
    LANGUAGES,
    VIDEO_TAGS,
)

load_dotenv()
logger = logging.getLogger(__name__)

METADATA_CACHE_FILENAME = "metadata.json"


# ─── Script Reading ───────────────────────────────────────────────────────────

def _strip_srt_timestamps(raw: str) -> str:
    """
    Remove SRT sequence numbers and timestamp lines from a .srt file,
    returning only the spoken dialogue text.

    SRT format:
        1
        00:00:01,000 --> 00:00:04,500
        This is the dialogue line.

        2
        00:00:05,000 --> 00:00:08,000
        Another line here.
    """
    # Remove lines that are purely a number (sequence index)
    # Remove lines that match the SRT timestamp pattern: HH:MM:SS,mmm --> HH:MM:SS,mmm
    lines = raw.splitlines()
    cleaned = []
    for line in lines:
        stripped = line.strip()
        # Skip empty lines
        if not stripped:
            continue
        # Skip pure sequence numbers
        if stripped.isdigit():
            continue
        # Skip timestamp lines
        if re.match(r"^\d{2}:\d{2}:\d{2}[,\.]\d{3}\s*-->\s*\d{2}:\d{2}:\d{2}[,\.]\d{3}", stripped):
            continue
        cleaned.append(stripped)

    return " ".join(cleaned)


def read_script(folder_path: Path) -> str:
    """
    Read the episode script from *folder_path*.

    Search order:
      1. script.txt  (plain text — used as-is)
      2. script.srt  (subtitle file — timestamps are stripped automatically)

    Returns the clean script text.

    Raises:
        FileNotFoundError: if neither script.txt nor script.srt exists.
    """
    txt_path = folder_path / "script.txt"
    srt_path = folder_path / "script.srt"

    if txt_path.exists():
        text = txt_path.read_text(encoding="utf-8").strip()
        logger.debug("Loaded script from script.txt (%d chars)", len(text))
        return text

    if srt_path.exists():
        raw = srt_path.read_text(encoding="utf-8")
        text = _strip_srt_timestamps(raw).strip()
        logger.debug(
            "Loaded script from script.srt → stripped to %d chars of dialogue.", len(text)
        )
        return text

    raise FileNotFoundError(
        f"No script file found in '{folder_path}'. "
        "Expected 'script.txt' or 'script.srt'."
    )


# ─── Cache Helpers ────────────────────────────────────────────────────────────

def _load_cache(folder_path: Path) -> dict:
    """Load metadata.json from *folder_path*. Returns {} if missing or corrupt."""
    cache_file = folder_path / METADATA_CACHE_FILENAME
    if not cache_file.exists():
        return {}
    try:
        with cache_file.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        logger.debug("Loaded metadata cache from %s", cache_file)
        return data
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning(
            "Could not read metadata cache %s (%s) — starting fresh.", cache_file, exc
        )
        return {}


def _save_cache(folder_path: Path, cache: dict) -> None:
    """Atomically write the metadata cache to metadata.json (temp → rename)."""
    cache_file = folder_path / METADATA_CACHE_FILENAME
    fd, tmp_path = tempfile.mkstemp(
        dir=folder_path, suffix=".tmp", prefix="meta_"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(cache, fh, ensure_ascii=False, indent=2)
        os.replace(tmp_path, cache_file)
        logger.debug("Metadata cache saved → %s", cache_file)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _is_cache_valid(entry: object) -> bool:
    """Return True only if a cache entry contains all three required keys."""
    return (
        isinstance(entry, dict)
        and all(k in entry for k in ("title", "description", "tags"))
    )


# ─── Prompt Builder ───────────────────────────────────────────────────────────

def _build_prompt(script_text: str, lang: str) -> str:
    """Build the full Gemini prompt for the given language."""
    import datetime
    current_year = datetime.datetime.now().year
    ctx = LANGUAGE_CONTEXTS[lang]
    return f"""You are a passionate Italian travel content creator helping Fabio,
an Italian tour guide working in Egypt, to create viral YouTube Shorts content
that inspires Italian tourists to book a trip to Egypt or Sharm el-Sheikh.

CURRENT YEAR: {current_year}
- Keep in mind that the current year is {current_year}. Do NOT reference outdated years (like 2024 or 2025) in any generated text.

CHANNEL NOTE: {ctx['channel_note']}
TARGET AUDIENCE: {ctx['audience']}
REQUIRED TONE: {ctx['tone']}

SOURCE SCRIPT (the narration/dialogue for this Short — may be in any language):
---
{script_text.strip()}
---

LANGUAGE & TONE INSTRUCTIONS:
- Write ENTIRELY in {ctx['language_name']} ({ctx['language_native']}).
- Use warm, natural, everyday Italian — NOT formal or corporate language.
- Make the viewer feel they are missing out on something magical if they don't book.
- Focus on beauty, adventure, value, and the unique experience Egypt offers.

SMART TITLE LOGIC:
1. Read the script carefully. Find the most emotionally compelling or curiosity-inducing
   moment — the thing that would make an Italian scroll STOP and watch.
2. Build your title around that moment. It can be a question, a bold statement,
   or a surprising reveal (e.g. "Non crederai a quello che ho trovato a Sharm!" or
   "Ecco perché l'Egitto è il posto più bello del mondo").
3. MAXIMUM 60 characters (not counting the ' #Shorts' suffix).

OUTPUT RULES:

TITLE:
- Write in Italian.
- MAXIMUM 60 characters (not counting ' #Shorts').
- MUST end with the exact string ' #Shorts' (one space before the hash).
- Must be dramatic, curiosity-driven, and click-optimised for Shorts.
- Do NOT start with "Video", "Guarda", or "Ep". No quotation marks.

DESCRIPTION:
- Write in Italian.
- MUST start with a compelling hook sentence.
- Then 2–4 lines summarising the beauty / experience shown in the video.
- End with a clear call-to-action (e.g. "Scrivici nei commenti per info sui tour!").
- Do NOT include any hashtags in the description body — they will be added automatically.
- 4–6 lines maximum total.

CRITICAL: Respond with ONLY a valid JSON object — no markdown fences, no prose,
no explanation outside the JSON. Exact schema required:
{{
  "title": "string",
  "description": "string"
}}"""


# ─── Gemini Client ────────────────────────────────────────────────────────────

def _init_client() -> genai.Client:
    """Create and return an authenticated google-genai Client from .env key."""
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        raise EnvironmentError(
            "GEMINI_API_KEY is not set. "
            "Add it to your .env file in the project root."
        )
    return genai.Client(api_key=api_key)


# ─── Single API Call ──────────────────────────────────────────────────────────

def _call_gemini(client: genai.Client, prompt: str, lang: str) -> dict | None:
    """
    Execute one Gemini API call and return the validated metadata dict.
    Returns None on any failure after all retries are exhausted.

    Includes retry logic for transient server errors (503, 429, UNAVAILABLE).
    """
    max_retries = 6
    for attempt in range(max_retries):
        try:
            response = client.models.generate_content(
                model=GEMINI_MODEL,
                contents=prompt,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    temperature=0.85,
                ),
            )
            raw = response.text.strip()

            # Strip accidental markdown fences Gemini sometimes adds
            if raw.startswith("```"):
                lines = raw.splitlines()
                raw = "\n".join(
                    line for line in lines
                    if not line.strip().startswith("```")
                ).strip()

            data = json.loads(raw)

            # Validate required keys
            for key in ("title", "description"):
                if key not in data:
                    raise ValueError(f"Missing key '{key}' in Gemini JSON response.")

            # Enforce #Shorts at end of title
            title = data["title"].rstrip()
            if not title.endswith("#Shorts"):
                title = title.rstrip() + " #Shorts"
            data["title"] = title

            # Clean description and inject disclaimer + hashtags
            desc = data["description"].strip()
            desc = desc.replace("#Shorts", "").replace("#shorts", "").strip()

            disclaimer = DISCLAIMERS.get(lang, "")
            hashtags   = HASHTAGS.get(lang, "")
            data["description"] = f"{desc}\n\n{disclaimer}\n\n{hashtags}"

            # Inject static SEO tags
            data["tags"] = VIDEO_TAGS.get(lang, [])

            logger.info("[%s] ✓ Metadata generated — Title: %s", lang, data["title"])
            return data

        except json.JSONDecodeError as exc:
            logger.error("[%s] Gemini returned invalid JSON: %s", lang, exc)
            return None
        except ValueError as exc:
            logger.error("[%s] Gemini schema error: %s", lang, exc)
            return None
        except Exception as exc:
            err_str = str(exc)
            is_transient = any(
                code in err_str
                for code in ["503", "429", "UNAVAILABLE", "Too Many Requests"]
            )
            if is_transient and attempt < max_retries - 1:
                wait = 15
                logger.warning(
                    "[%s] Gemini busy (attempt %d/%d). Retrying in %ds…",
                    lang, attempt + 1, max_retries, wait,
                )
                time.sleep(wait)
                continue
            logger.error("[%s] Gemini API call failed: %s", lang, exc)
            return None

    return None


# ─── Public Entry Point ───────────────────────────────────────────────────────

def get_metadata(folder_path: Path, script_text: str, lang: str) -> dict | None:
    """
    Return metadata for a single language, using cache when available.

    Flow:
      1. Check metadata.json for a pre-existing valid entry for *lang*.
         → If found:  return cached data immediately (no API call).
         → If absent: call Gemini, save result to cache, return it.

    Args:
        folder_path:  Path to the video folder (metadata.json lives here).
        script_text:  The cleaned script text to feed Gemini.
        lang:         Language code, e.g. "IT".

    Returns:
        Metadata dict or None if generation failed.
    """
    cache = _load_cache(folder_path)

    if _is_cache_valid(cache.get(lang)):
        logger.info("[%s] ✓ Using cached metadata — Gemini call skipped.", lang)
        return cache[lang]

    logger.info("[%s] No valid cache entry — calling Gemini…", lang)
    client = _init_client()
    prompt = _build_prompt(script_text, lang)
    result = _call_gemini(client, prompt, lang)

    if result is not None:
        cache[lang] = result
        _save_cache(folder_path, cache)

    return result


def generate_metadata_for_folder(folder_path: Path, lang: str = "IT") -> dict | None:
    """
    Convenience function: read script + generate/load metadata for one folder.

    Raises FileNotFoundError if no script file exists in the folder.
    Returns the metadata dict or None on Gemini failure.
    """
    script_text = read_script(folder_path)
    if not script_text.strip():
        raise ValueError(f"Script is empty in folder: {folder_path}")
    return get_metadata(folder_path, script_text, lang)
