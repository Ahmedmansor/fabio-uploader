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
    CORE_BRAND_HASHTAGS,
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
    """Return True only if a cache entry matches the new multi-platform schema."""
    return (
        isinstance(entry, dict)
        and "youtube" in entry
        and "meta_reels" in entry
        and "tiktok" in entry
        and isinstance(entry["youtube"], dict)
        and isinstance(entry["meta_reels"], dict)
        and isinstance(entry["tiktok"], dict)
        and all(k in entry["youtube"] for k in ("title", "description", "tags"))
        and all(k in entry["meta_reels"] for k in ("caption", "hashtags"))
        and all(k in entry["tiktok"] for k in ("caption", "hashtags"))
    )


# ─── Prompt Builder ───────────────────────────────────────────────────────────

def _build_prompt(script_text: str, lang: str) -> str:
    """Build the full Gemini prompt for the given language using the new multi-platform schema."""
    import datetime
    current_year = datetime.datetime.now().year
    ctx = LANGUAGE_CONTEXTS[lang]
    disclaimer = DISCLAIMERS.get(lang, "")
    brand_tags = CORE_BRAND_HASHTAGS.get(lang, [])
    brand_tags_str = ", ".join(brand_tags)
    
    return f"""You are a passionate Italian travel content creator helping Fabio,
an Italian tour guide working in Egypt, to create viral short-form video content
tailored for YouTube Shorts, Meta Reels (Facebook & Instagram), and TikTok.
Your goal is to inspire Italian tourists to book a trip to Egypt or Sharm el-Sheikh.

CURRENT YEAR: {current_year}
- Keep in mind that the current year is {current_year}. Do NOT reference outdated years (like 2024 or 2025).

CHANNEL NOTE: {ctx['channel_note']}
TARGET AUDIENCE: {ctx['audience']}
REQUIRED TONE: {ctx['tone']}

SOURCE SCRIPT:
---
{script_text.strip()}
---

INSTRUCTIONS:
1. Write ENTIRELY in {ctx['language_name']} ({ctx['language_native']}).
2. Use warm, natural, everyday Italian — NOT formal or corporate language.
3. Tailor the title, caption, and hashtags specifically to each platform's distinct style and limits.

SCHEMA RULES:
You must output a JSON object containing a single root key matching the language code "{lang}".
Inside it, there must be three objects: "youtube", "meta_reels", and "tiktok".

For "youtube" (YouTube Shorts):
- "title": A curiosity-driven, high-click title strictly under 50 characters. Do NOT write '#shorts' or '#Shorts' (the backend will automatically append this).
- "description": A detailed SEO-friendly description starting with a hook sentence, followed by 2-4 lines summarizing the video, then this exact disclaimer appended at the end:
  "{disclaimer}"
- "tags": An array of relevant tags/keywords for the YouTube backend (do NOT include '#' prefix).

For "meta_reels" (Meta Reels - Shared Facebook & Instagram Composer):
- "caption": A visually appealing, clean, and highly aesthetic caption optimized for Instagram layout (use clear line breaks/paragraphs and rich emojis) but limited in hashtags to prevent Facebook spam flags. Do NOT include any titles or markdown headers.
- "hashtags": An array containing exactly 1 to 2 dynamic, hyper-relevant hashtags, PLUS the 2 core brand hashtags: {brand_tags_str} (yielding exactly 3 to 4 hashtags in total). All hashtags must start with '#' symbol.

For "tiktok" (TikTok):
- "caption": A short, high-energy, punchy 1-2 sentence caption (Hook + strong Call to Action to watch or follow). SNAPPY and completely informal. Do NOT include markdown formatting or titles.
- "hashtags": An array containing exactly 2 to 4 dynamic, hyper-relevant hashtags (including TikTok-centric discovery tags like #fyp or #perte), PLUS the 2 core brand hashtags: {brand_tags_str} (yielding exactly 4 to 6 hashtags in total). All hashtags must start with '#' symbol.

OUTPUT SCHEMA FORMAT:
{{
  "{lang}": {{
    "youtube": {{
      "title": "Base title strictly under 50 chars",
      "description": "SEO description + disclaimer",
      "tags": ["tag1", "tag2"]
    }},
    "meta_reels": {{
      "caption": "Aesthetic caption with line breaks and emojis.",
      "hashtags": ["#DynamicTag", "#FabioEgypt", "#ViaggioInEgitto"]
    }},
    "tiktok": {{
      "caption": "Snappy high-energy TikTok caption + CTA.",
      "hashtags": ["#fyp", "#perte", "#DynamicTag", "#FabioEgypt", "#ViaggioInEgitto"]
    }}
  }}
}}

CRITICAL: Respond with ONLY a valid JSON object — no markdown fences, no prose, no explanations outside the JSON."""


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
    Execute one Gemini API call and return the validated multi-platform metadata dict.
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

            # Get inner data from root key lang (e.g. "IT") if present
            if lang in data:
                inner_data = data[lang]
            else:
                inner_data = data

            # Validate required keys
            for key in ("youtube", "meta_reels", "tiktok"):
                if key not in inner_data:
                    raise ValueError(f"Missing key '{key}' in Gemini JSON response.")

            yt_data = inner_data["youtube"]
            meta_data = inner_data["meta_reels"]
            tt_data = inner_data["tiktok"]

            # Validate inner structures
            for key in ("title", "description", "tags"):
                if key not in yt_data:
                    raise ValueError(f"Missing YouTube key '{key}' in Gemini response.")

            for plat_name, plat_data in [("meta_reels", meta_data), ("tiktok", tt_data)]:
                for key in ("caption", "hashtags"):
                    if key not in plat_data:
                        raise ValueError(f"Missing {plat_name} key '{key}' in Gemini response.")

            # Clean YouTube title: remove quotes, clean whitespace, and automatically append ' #shorts'
            yt_title = yt_data["title"].strip().strip('"').strip("'")
            yt_title = yt_title.replace("#Shorts", "").replace("#shorts", "").strip()
            yt_title = f"{yt_title} #shorts"
            yt_data["title"] = yt_title

            # Clean YouTube description and append disclaimer
            yt_desc = yt_data["description"].strip()
            disclaimer = DISCLAIMERS.get(lang, "")
            if disclaimer and disclaimer not in yt_desc:
                yt_desc = f"{yt_desc}\n\n{disclaimer}"
            yt_data["description"] = yt_desc

            # Enforce YouTube tags (backend tags: no '#' prefix)
            yt_tags = yt_data.get("tags", [])
            if not isinstance(yt_tags, list):
                yt_tags = []
            yt_tags = [t.replace("#", "").strip() for t in yt_tags if t.strip()]
            static_tags = VIDEO_TAGS.get(lang, [])
            for st in static_tags:
                if st not in yt_tags:
                    yt_tags.append(st)
            yt_data["tags"] = yt_tags

            # Clean and enrich meta_reels
            meta_caption = meta_data["caption"].strip().strip('"').strip("'")
            meta_data["caption"] = meta_caption
            meta_hashtags = meta_data.get("hashtags", [])
            if not isinstance(meta_hashtags, list):
                meta_hashtags = []
            meta_hashtags = [h if h.startswith("#") else f"#{h}" for h in meta_hashtags if h.strip()]
            
            brand_tags = CORE_BRAND_HASHTAGS.get(lang, [])
            for bt in brand_tags:
                if bt not in meta_hashtags:
                    meta_hashtags.append(bt)
            meta_data["hashtags"] = meta_hashtags

            # Clean and enrich tiktok
            tt_caption = tt_data["caption"].strip().strip('"').strip("'")
            tt_data["caption"] = tt_caption
            tt_hashtags = tt_data.get("hashtags", [])
            if not isinstance(tt_hashtags, list):
                tt_hashtags = []
            tt_hashtags = [h if h.startswith("#") else f"#{h}" for h in tt_hashtags if h.strip()]
            
            for bt in brand_tags:
                if bt not in tt_hashtags:
                    tt_hashtags.append(bt)
            tt_data["hashtags"] = tt_hashtags

            final_data = {
                "youtube": yt_data,
                "meta_reels": meta_data,
                "tiktok": tt_data,
            }

            logger.info("[%s] ✓ Multi-platform metadata generated successfully.", lang)
            return final_data

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
