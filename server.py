"""voice-ai - single-package voice assistant.

Flow: mic -> Whisper (STT, in-process) -> LLM (streaming) -> sentence detection
-> TTS per sentence (in-process backends) -> avatar video (DITTO subprocess) ->
stream back to the browser.

Endpoints:
  POST /api/talk              audio upload -> {turn_id}
  GET  /api/talk/status/{id}  {status, user_text, sentences:[{text, audio_url, video_url}]}
  POST /api/tts               text -> {ok, audio_url}
  GET/POST/DELETE/PUT /api/voices   voice profile CRUD
  POST /api/models/unload     unload a TTS model (frees VRAM)
  GET  /                      the hold-to-talk UI
  GET  /admin                 the backend admin UI
"""

from __future__ import annotations

import argparse
import base64
import datetime
import json
import os
import re
import secrets
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from typing import Optional
from urllib.parse import quote

import httpx
import yaml
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles

import sys
# Reconfigure stdio to UTF-8 so debug prints (e.g. web_fetch results with
# Unicode chars) don't crash on Windows' cp1252 console/locale encoding.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

ROOT = os.path.dirname(os.path.abspath(__file__))


ENGINE_SETTINGS = {
    "kokoro": [
        {"key": "enabled", "type": "bool", "label": "Enabled", "desc": "Whether the kokoro engine (CPU) is available.", "default": True},
        {"key": "auto_start", "type": "bool", "label": "Auto-start", "desc": "Load the model at server startup instead of on first use.", "default": False},
        {"key": "default_voice", "type": "select", "label": "Default voice", "desc": "The default kokoro voice preset.", "options": ["af_alloy","af_aoede","af_bella","af_heart","af_jessica","af_kore","af_nicole","af_nova","af_river","af_sarah","af_sky","bf_alice","bf_emma","bf_isabella","bf_lily","bm_daniel","bm_fable","bm_george","bm_lewis"], "default": "bf_isabella"},
        {"key": "speed", "type": "float", "label": "Speed", "desc": "Speaking speed (higher = faster).", "min": 0.5, "max": 2.0, "default": 1.0},
    ],
    "breeze": [
        {"key": "enabled", "type": "bool", "label": "Enabled", "desc": "Whether the breeze engine (PyTorch/CUDA) is available.", "default": True},
        {"key": "auto_start", "type": "bool", "label": "Auto-start", "desc": "Load the model at server startup instead of on first use.", "default": False},
        {"key": "default_cfg_scale", "type": "float", "label": "Default CFG scale", "desc": "Default instruction-following strength (higher = stronger direction).", "min": 1.0, "max": 10.0, "default": 4.0},
        {"key": "default_seed", "type": "int", "label": "Default seed", "desc": "Default random seed (empty = random each generation).", "default": None},
        {"key": "default_voice_id", "type": "voice", "label": "Default voice", "desc": "Default clone voice for this engine.", "default": ""},
        {"key": "default_instruction_id", "type": "design", "label": "Default design", "desc": "Default voice design for this engine.", "default": ""},
        {"key": "port", "type": "int", "label": "Port", "desc": "Internal subprocess port for the breeze server.", "default": 8137},
    ],
    "omnivoice": [
        {"key": "enabled", "type": "bool", "label": "Enabled", "desc": "Whether the omnivoice engine is available.", "default": True},
        {"key": "auto_start", "type": "bool", "label": "Auto-start", "desc": "Load the model at server startup instead of on first use.", "default": False},
        {"key": "model_id", "type": "str", "label": "Model ID", "desc": "The OmniVoice HuggingFace model id.", "default": "k2-fsa/OmniVoice"},
        {"key": "dtype", "type": "select", "label": "Dtype", "desc": "Model precision (lower = less VRAM).", "options": ["float16","float32","bfloat16"], "default": "float16"},
        {"key": "num_step", "type": "int", "label": "Num steps", "desc": "Number of diffusion sampling steps.", "default": 64},
        {"key": "class_temperature", "type": "float", "label": "Class temperature", "desc": "Classifier-free guidance temperature.", "default": 1.0},
        {"key": "default_guidance_scale", "type": "float", "label": "Default guidance scale", "desc": "Default guidance strength.", "min": 1.0, "max": 10.0, "default": 4.0},
        {"key": "default_voice_id", "type": "voice", "label": "Default voice", "desc": "Default clone voice for this engine.", "default": ""},
        {"key": "default_instruction_id", "type": "design", "label": "Default design", "desc": "Default voice design for this engine.", "default": ""},
    ],
    "lux": [
        {"key": "enabled", "type": "bool", "label": "Enabled", "desc": "Whether the lux engine is available.", "default": True},
        {"key": "auto_start", "type": "bool", "label": "Auto-start", "desc": "Load the model at server startup instead of on first use.", "default": False},
        {"key": "num_steps", "type": "int", "label": "Num steps", "desc": "Number of diffusion sampling steps.", "default": 8},
        {"key": "t_shift", "type": "float", "label": "Time shift", "desc": "Time shift parameter for the scheduler.", "default": 0.9},
        {"key": "rms", "type": "float", "label": "RMS", "desc": "RMS normalization level.", "default": 0.01},
        {"key": "default_voice_id", "type": "voice", "label": "Default voice", "desc": "Default clone voice for this engine.", "default": ""},
    ],
}

def _save_config(config: dict):
    cfg_path = os.path.join(ROOT, "config.yaml")
    tmp = cfg_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        yaml.safe_dump(config, f, allow_unicode=True, sort_keys=False)
    os.replace(tmp, cfg_path)


def _path_base(config: dict) -> str:
    """Base directory used to resolve relative paths in the config."""
    base = ((config.get("paths") or {}).get("base") or "").strip()
    if not base:
        return ROOT
    base = os.path.expandvars(os.path.expanduser(base))
    if os.path.isabs(base):
        return base
    return os.path.normpath(os.path.join(ROOT, base))


def _resolve_path(p, base: str):
    if not p:
        return p
    p = os.path.expandvars(os.path.expanduser(str(p)))
    if os.path.isabs(p):
        return os.path.normpath(p)
    return os.path.normpath(os.path.join(base, p))


def _safe_next(next_url: str) -> str:
    """Validate a post-login redirect target (relative path only, no open redirect)."""
    n = (next_url or "").strip()
    if n.startswith("/") and not n.startswith("//"):
        return n
    return ""


# Rough per-model VRAM estimates (GB) for the status page. These are ballpark
# figures, not exact; the real total comes from nvidia-smi.
WHISPER_VRAM_GB = {
    "tiny": 0.9, "base": 1.0, "small": 2.0, "medium": 5.0,
    "large": 10.0, "large-v1": 10.0, "large-v2": 10.0, "large-v3": 10.0,
    "large-v3-turbo": 2.2,
    "distil-small.en": 1.0, "distil-medium.en": 1.5,
    "distil-large-v2": 3.0, "distil-large-v3": 3.0,
}
TTS_VRAM_GB = {"kokoro": 0.0, "breeze": 2.5, "omnivoice": 2.5, "lux": 2.5}
DITTO_VRAM_GB = 4.0


def _gpu_usage():
    """Return (used_mib, total_mib) from nvidia-smi, or None if unavailable."""
    try:
        r = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.used,memory.total", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=10,
        )
        if r.returncode != 0:
            return None
        used, total = r.stdout.strip().splitlines()[0].split(",")
        return int(used.strip()), int(total.strip())
    except Exception:
        return None


def _dir_size_gb(path):
    """Total size of a directory tree in GB (excluding VCS/metadata dirs), or None."""
    if not path or not os.path.isdir(path):
        return None
    total = 0
    for dirpath, dirs, files in os.walk(path):
        dirs[:] = [d for d in dirs if d not in (".git", ".hg", "__pycache__")]
        for fn in files:
            try:
                total += os.path.getsize(os.path.join(dirpath, fn))
            except OSError:
                pass
    return round(total / (1024 ** 3), 1)


def _hf_cache_dir(repo: str) -> str:
    try:
        from huggingface_hub.constants import HF_HUB_CACHE
        return os.path.join(HF_HUB_CACHE, "models--" + repo.replace("/", "--"))
    except Exception:
        return ""


def _tts_vram_est(name: str, models_cfg: dict, base: str) -> float:
    """Estimate a TTS engine's VRAM from its model file size (fallback to a rough number)."""
    fallback = TTS_VRAM_GB.get(name, 2.5)
    try:
        if name == "kokoro":
            return 0.0  # CPU engine
        if name == "breeze":
            mp = _resolve_path((models_cfg.get("breeze", {}) or {}).get("model_path", ""), base)
            sz = _dir_size_gb(mp)
            return sz if sz is not None else fallback
        if name == "omnivoice":
            mid = (models_cfg.get("omnivoice", {}) or {}).get("model_id", "k2-fsa/OmniVoice")
            sz = _dir_size_gb(_hf_cache_dir(mid))
            return sz if sz is not None else fallback
        if name == "lux":
            sz = _dir_size_gb(_hf_cache_dir("luxtts/zipvoice"))
            return sz if sz is not None else fallback
    except Exception:
        pass
    return fallback

from memory import Memory  # noqa: E402
from memory_embeddings import Embedder  # noqa: E402
from tts import TTSManager  # noqa: E402
from logs import log_event, read_log, clear_log, trim_log  # noqa: E402

_EMOJI_RE = re.compile(
    "["
    "\U0001F000-\U0001FAFF"
    "\U00002600-\U000027BF"
    "\U0001F1E0-\U0001F1FF"
    "\U0000FE00-\U0000FE0F"
    "\U0000200D"
    "]+"
)


_CONTRACTIONS = {
    "i'm": "i am", "you're": "you are", "he's": "he is", "she's": "she is",
    "it's": "it is", "we're": "we are", "they're": "they are",
    "i've": "i have", "you've": "you have", "we've": "we have", "they've": "they have",
    "i'll": "i will", "you'll": "you will", "he'll": "he will", "she'll": "she will",
    "it'll": "it will", "we'll": "we will", "they'll": "they will",
    "i'd": "i would", "you'd": "you would", "he'd": "he would", "she'd": "she would",
    "we'd": "we would", "they'd": "they would",
    "can't": "cannot", "don't": "do not", "doesn't": "does not", "didn't": "did not",
    "won't": "will not", "isn't": "is not", "aren't": "are not",
    "wasn't": "was not", "weren't": "were not",
    "haven't": "have not", "hasn't": "has not", "hadn't": "had not",
    "couldn't": "could not", "shouldn't": "should not", "wouldn't": "would not",
    "let's": "let us", "that's": "that is", "there's": "there is",
    "what's": "what is", "who's": "who is", "here's": "here is",
}


def _expand_contractions(text: str) -> str:
    def _repl(m):
        word = m.group(0)
        exp = _CONTRACTIONS.get(word.lower(), word)
        if word[:1].isupper():
            exp = exp[:1].upper() + exp[1:]
        return exp
    return re.sub(r"[A-Za-z]+'[A-Za-z]+", _repl, text)


def _image_mime(filename: str) -> str:
    ext = os.path.splitext(filename or "")[1].lower()
    return {
        ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
        ".webp": "image/webp", ".gif": "image/gif", ".bmp": "image/bmp",
    }.get(ext, "image/jpeg")


def clean_text(text: str) -> str:
    """Normalize LLM output before TTS: strip emojis, flatten dashes/quotes,
    expand contractions, collapse whitespace."""
    text = text.replace("\u2014", ", ").replace("\u2013", ", ")
    text = text.replace("\u201c", '"').replace("\u201d", '"')
    text = text.replace("\u2018", "'").replace("\u2019", "'")
    text = _EMOJI_RE.sub("", text)
    text = _expand_contractions(text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


_SENTENCE_END = re.compile(r"[.!?]\s")


def find_complete_sentences(text: str, offset: int):
    """Return (list_of_new_sentences, new_offset). Sentences end with .!?
    followed by whitespace. A trailing punctuation with no whitespace stays
    buffered (the LLM may still be emitting tokens)."""
    sentences = []
    i = offset
    n = len(text)
    while i < n:
        m = _SENTENCE_END.search(text, i)
        if not m:
            break
        end = m.start() + 1  # include the punctuation
        s = text[i:end].strip()
        if s:
            sentences.append(s)
        i = m.end()  # skip punctuation + one whitespace
    return sentences, i


def est_tokens(text: str) -> int:
    """Rough token estimate (chars / 4)."""
    return max(1, len(text or "") // 4)


CURATION_PROMPT = """You maintain a voice assistant's memory for one day. Below are memory entries in this format:

## slug
body

Clean them up:
- Keep only durable facts about the user (their identity, preferences, relationships, decisions, stable facts).
- Remove transient information (weather, news, search results), meta statements ("the user asked/requested/needed..."), and generic filler answers.
- Merge near-duplicate entries (the same fact written multiple times, or with slight wording differences) into ONE entry.
- Use plain lowercase kebab-case slugs with straight apostrophes (not curly).
- Return the cleaned entries in the same format (## slug, then body), one blank line between entries.
- If nothing is worth keeping, return exactly: EMPTY"""


TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "Search the web for current, up-to-date information",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "The search query"}
                },
                "required": ["query"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "web_fetch",
            "description": "Fetch and read the text content of a web page",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "The URL to fetch (http or https)"}
                },
                "required": ["url"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "weather",
            "description": "Get the current weather and 7-day forecast for a city or town",
            "parameters": {
                "type": "object",
                "properties": {
                    "location": {"type": "string", "description": "The place name, e.g. 'Heerde' or 'Amsterdam, Netherlands'"}
                },
                "required": ["location"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": "List files and folders in your personal folder",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Subdirectory to list, or empty for the root folder"}
                },
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read the text content of a file in your personal folder",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File path relative to your folder"}
                },
                "required": ["path"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Create or overwrite a file in your personal folder",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File path relative to your folder"},
                    "content": {"type": "string", "description": "The full text content to write"}
                },
                "required": ["path", "content"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "edit_file",
            "description": "Replace a piece of text in a file (modify it)",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File path relative to your folder"},
                    "old_text": {"type": "string", "description": "The exact text to replace"},
                    "new_text": {"type": "string", "description": "The replacement text"}
                },
                "required": ["path", "old_text", "new_text"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "delete_file",
            "description": "Delete a file or folder from your personal folder",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File or folder path relative to your folder"}
                },
                "required": ["path"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "send_file",
            "description": "Send a file from your personal folder to the user for download",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File path relative to your folder"}
                },
                "required": ["path"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "use_skill",
            "description": "Load the full instructions for a skill by name",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "The skill name"}
                },
                "required": ["name"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "create_skill",
            "description": "Create a new skill. Content must start with a '# Title' heading followed by clear instructions.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Skill name, lowercase with no spaces (e.g. 'meeting-notes')"},
                    "content": {"type": "string", "description": "The full skill instructions in markdown"}
                },
                "required": ["name", "content"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "edit_skill",
            "description": "Overwrite an existing skill's instructions",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Skill name"},
                    "content": {"type": "string", "description": "The full new skill instructions in markdown"}
                },
                "required": ["name", "content"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "delete_skill",
            "description": "Delete a skill",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Skill name"}
                },
                "required": ["name"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "set_voice_design",
            "description": "Switch the voice design (emotional tone) for this reply",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "The voice design name, e.g. default, happy, angry, sad"}
                },
                "required": ["name"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "remember",
            "description": "Save a fact or note to your long-term memory so you can recall it in later conversations",
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "The fact or note to remember, written clearly"}
                },
                "required": ["text"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "memory_search",
            "description": "Search your long-term memory for relevant facts and notes",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "What to search for in your memory"}
                },
                "required": ["query"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "memory_day",
            "description": "Return the full content of your memory for a specific day",
            "parameters": {
                "type": "object",
                "properties": {
                    "date": {"type": "string", "description": "The day: 'today', 'yesterday', or a date like 2026-09-13"}
                },
                "required": ["date"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "delete_memory",
            "description": "Delete a single memory item by its slug (short title)",
            "parameters": {
                "type": "object",
                "properties": {
                    "slug": {"type": "string", "description": "The slug (short title) of the memory item to delete"},
                    "date": {"type": "string", "description": "Optional: the day to look in ('today', 'yesterday', or YYYY-MM-DD). Omit to search all days."}
                },
                "required": ["slug"]
            }
        }
    }
]


def chunk_text(text: str, max_chars: int = 400, min_chunk_chars: int = 100) -> list:
    sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", text) if s.strip()]
    chunks = []
    i = 0
    while i < len(sentences):
        s = sentences[i]
        i += 1
        if len(s) > max_chars:
            chunks.append(s)
            continue
        buf = s
        while (i < len(sentences) and len(buf) < min_chunk_chars
               and len(buf) + 1 + len(sentences[i]) <= max_chars):
            buf = buf + " " + sentences[i]
            i += 1
        chunks.append(buf)
    return [c for c in chunks if c]


def web_search(query: str, max_results: int = 5) -> str:
    try:
        from ddgs import DDGS
        with DDGS() as ddgs:
            results = list(ddgs.text(query, max_results=max_results))
        out = [{"title": r.get("title", ""), "url": r.get("href", ""), "snippet": r.get("body", "")} for r in results]
        return json.dumps(out, ensure_ascii=False)[:3000]
    except Exception as e:  # noqa: BLE001
        return f"search error: {e}"


def web_fetch(url: str, max_chars: int = 4000) -> str:
    try:
        r = httpx.get(url, timeout=20, follow_redirects=True)
        html = r.text
        html = re.sub(r"<script[^>]*>.*?</script>", "", html, flags=re.DOTALL | re.IGNORECASE)
        html = re.sub(r"<style[^>]*>.*?</style>", "", html, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r"<[^>]+>", " ", html)
        text = re.sub(r"\s+", " ", text).strip()
        return text[:max_chars]
    except Exception as e:  # noqa: BLE001
        return f"fetch error: {e}"


WMO_CODES = {
    0: "clear sky", 1: "mainly clear", 2: "partly cloudy", 3: "overcast",
    45: "fog", 48: "depositing rime fog",
    51: "light drizzle", 53: "drizzle", 55: "dense drizzle",
    61: "slight rain", 63: "rain", 65: "heavy rain",
    71: "slight snow", 73: "snow", 75: "heavy snow", 77: "snow grains",
    80: "slight showers", 81: "showers", 82: "violent showers",
    85: "snow showers", 86: "heavy snow showers",
    95: "thunderstorm", 96: "thunderstorm with hail", 99: "thunderstorm with heavy hail",
}


def weather(location: str) -> str:
    """Get current weather + 7-day forecast for a place via Open-Meteo (no API key)."""
    try:
        # Try progressively simpler forms of the location (the geocoder dislikes
        # country qualifiers like ", Netherlands").
        candidates = [location]
        if "," in location:
            candidates.append(location.split(",")[0].strip())
        words = location.replace(",", " ").split()
        if words and words[0].lower() not in [c.lower() for c in candidates]:
            candidates.append(words[0])
        r = None
        for cand in candidates:
            geo = httpx.get(
                "https://geocoding-api.open-meteo.com/v1/search",
                params={"name": cand, "count": 1, "language": "en", "format": "json"},
                timeout=20, follow_redirects=True,
            )
            geo.raise_for_status()
            results = geo.json().get("results") or []
            if results:
                r = results[0]
                break
        if r is None:
            return f"weather: location '{location}' not found"
        lat, lon = float(r["latitude"]), float(r["longitude"])
        label = r.get("name", location)
        admin = r.get("admin1") or ""
        country = r.get("country") or ""

        w = httpx.get(
            "https://api.open-meteo.com/v1/forecast",
            params={
                "latitude": lat, "longitude": lon,
                "current": "temperature_2m,weather_code,wind_speed_10m",
                "daily": "weather_code,temperature_2m_max,temperature_2m_min",
                "timezone": "auto",
            },
            timeout=20, follow_redirects=True,
        )
        w.raise_for_status()
        d = w.json()
        cur = d.get("current", {})
        daily = d.get("daily", {})
        loc = label + (", " + admin if admin else "") + (", " + country if country else "")
        lines = [f"Weather for {loc}:"]
        lines.append(
            f"Now: {cur.get('temperature_2m')} degrees Celsius, {WMO_CODES.get(cur.get('weather_code'), 'unknown')}, "
            f"wind {cur.get('wind_speed_10m')} kilometers per hour"
        )
        times = daily.get("time", [])
        codes = daily.get("weather_code", [])
        tmax = daily.get("temperature_2m_max", [])
        tmin = daily.get("temperature_2m_min", [])
        lines.append("7-day forecast:")
        for i in range(min(7, len(times))):
            lines.append(
                f"  {times[i]}: {WMO_CODES.get(codes[i], 'unknown')}, {tmin[i]} to {tmax[i]} degrees Celsius"
            )
        return "\n".join(lines)
    except Exception as e:  # noqa: BLE001
        return f"weather error: {e}"


def parse_tool_call(text: str):
    m = re.search(r'\{[^{}]*"tool"\s*:\s*"[^"]+"[^{}]*\}', text or "")
    if not m:
        return None
    try:
        obj = json.loads(m.group(0))
        if obj.get("tool") in ("web_search", "web_fetch"):
            return obj
    except Exception:
        pass
    return None


class TalkApp:
    def __init__(self, config: dict):
        self.config = config
        self.root = ROOT
        self.llm = config.get("llm", {})
        self.tts = config.get("tts", {})
        self.stt = config.get("stt", {})
        self.ditto = config.get("ditto", {})
        self.max_history_tokens = int(self.llm.get("max_history_tokens", 24000))
        self.temperature = float(self.llm.get("temperature", 0.7))
        self.max_tokens = int(self.llm.get("max_tokens", 8192))
        self.thinking = bool(self.llm.get("thinking", False))
        chunk_cfg = config.get("chunking", {}) or {}
        self.min_chunk_chars = int(chunk_cfg.get("min_chunk_chars", 100))
        self.max_chunk_chars = int(chunk_cfg.get("max_chunk_chars", 400))
        self.default_mode = chunk_cfg.get("default_mode", "chunked")
        self.sessions = set()
        self.auth = config.get("auth", {}) or {}
        self.personality_file = os.path.join(ROOT, config.get("personality_file", "personality.md"))
        self.system_prompt = self._load_personality()
        self.personalities = {}
        self._reload_personalities()
        self.profiles_file = os.path.join(ROOT, "profiles.json")
        self.profiles = self._load_profiles()

        emb_cfg = config.get("memory", {}).get("embedding", {})
        self.embedder = Embedder(
            model_name=emb_cfg.get("model_name", "Qwen/Qwen3-Embedding-0.6B"),
            device=emb_cfg.get("device", "cpu"),
        )
        self.memory = Memory(ROOT, config, embedder=self.embedder)
        self.embedder.warmup()
        self.tts_manager = TTSManager(ROOT, config)

        self.turns = {}          # turn_id -> turn dict
        self.lock = threading.Lock()

        self.history_dir = os.path.join(ROOT, "history")
        self.history_max_turns = int(config.get("history", {}).get("max_turns", 60))
        self.turns_dir = os.path.join(ROOT, "output", "turns")

        mem_cfg = config.get("memory", {}) or {}
        self.curation_interval = max(1, int(mem_cfg.get("curate_every", 10)))
        self._mem_pending = []            # (profile, user_text, assistant_text) awaiting extraction
        self._mem_lock = threading.Lock()
        self._curator_busy = False

        # index memory in the background (all profiles)
        threading.Thread(target=self._reindex_all_profiles, daemon=True).start()

        # spawn the avatar (DITTO) subprocess in the background (if enabled)
        self._ditto_proc = None
        if (self.config.get("ditto", {}) or {}).get("enabled", False):
            threading.Thread(target=self._start_ditto, daemon=True).start()

        # periodic media TTL cleanup (audio + avatar videos)
        threading.Thread(target=self._media_cleanup_loop, daemon=True).start()

        # periodic log trimming
        threading.Thread(target=self._log_trim_loop, daemon=True).start()

    def _load_personality(self) -> str:
        try:
            with open(self.personality_file, encoding="utf-8") as f:
                return f.read().strip()
        except OSError:
            return "You are a helpful, friendly voice assistant."

    def _reload_personalities(self):
        """Hot-load all personalities from disk (called per request so edits
        apply without a restart)."""
        personalities = {"default": self._load_personality()}
        pers_dir = os.path.join(ROOT, "personalities")
        if os.path.isdir(pers_dir):
            for fname in sorted(os.listdir(pers_dir)):
                if fname.endswith(".md"):
                    try:
                        with open(os.path.join(pers_dir, fname), encoding="utf-8") as f:
                            personalities[fname[:-3]] = f.read().strip()
                    except OSError:
                        pass
        self.personalities = personalities

    def _load_skill(self, name: str) -> str:
        name = (name or "").strip()
        if not name or "/" in name or "\\" in name or ".." in name:
            return ""
        path = os.path.join(ROOT, "skills", name + ".md")
        try:
            with open(path, encoding="utf-8") as f:
                return f.read().strip()
        except OSError:
            return ""

    def _list_skills(self) -> list:
        skills_dir = os.path.join(ROOT, "skills")
        out = []
        if os.path.isdir(skills_dir):
            for fname in sorted(os.listdir(skills_dir)):
                if fname.endswith(".md"):
                    name = fname[:-3]
                    title = name
                    try:
                        with open(os.path.join(skills_dir, fname), encoding="utf-8") as f:
                            for line in f.read().split("\n"):
                                line = line.strip()
                                if line.startswith("# "):
                                    title = line[2:].strip()
                                    break
                    except OSError:
                        pass
                    out.append({"name": name, "title": title})
        return out

    def _skill_index(self) -> str:
        skills = self._list_skills()
        if not skills:
            return ""
        lines = ["Available skills:"]
        for s in skills:
            lines.append("- " + s["name"] + ": " + s["title"])
        lines.append("\nUse the use_skill tool to load a skill's full instructions when a task matches one.")
        return "\n".join(lines)

    def _voice_designs(self) -> dict:
        """Map voice design name -> instruction_id (from the voice store)."""
        designs = {}
        for p in self.tts_manager.voices.list():
            if p.get("kind") == "design":
                designs[p.get("name", "")] = p.get("id", "")
        return designs

    def _voice_design_index(self, current_iid: str = "") -> str:
        designs = self._voice_designs()
        if not designs:
            return ""
        names = sorted(designs.keys())
        current_name = "default"
        for name, vid in designs.items():
            if vid == current_iid:
                current_name = name
                break
        lines = ["Voice designs for emotional tone:"]
        for n in names:
            lines.append("- " + n + (" (current)" if n == current_name else ""))
        lines.append("")
        lines.append("Current voice design: " + current_name)
        lines.append("Only call set_voice_design when your reply needs a different voice than the current one. If the current design already fits the emotion, skip the tool call.")
        return "\n".join(lines)

    def _load_profiles(self) -> dict:
        try:
            with open(self.profiles_file, encoding="utf-8") as f:
                data = json.load(f)
                return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save_profiles(self):
        with open(self.profiles_file, "w", encoding="utf-8") as f:
            json.dump(self.profiles, f, indent=2)

    def _public_profiles(self) -> dict:
        return {k: v for k, v in self.profiles.items() if k != "_default"}

    # -- persistent per-profile history ----------------------------------
    def _history_path(self, profile: str) -> str:
        key = (profile or "").strip() or "default"
        key = key.replace("/", "_").replace("\\", "_").replace("..", "_")
        return os.path.join(self.history_dir, key + ".json")

    def _load_history(self, profile: str) -> list:
        try:
            with open(self._history_path(profile), encoding="utf-8") as f:
                data = json.load(f)
                return data if isinstance(data, list) else []
        except (OSError, ValueError):
            return []

    def _save_history(self, profile: str, turns: list):
        path = self._history_path(profile)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(turns, f, ensure_ascii=False)

    def _delete_turn_files(self, turn: dict):
        tid = (turn or {}).get("turn_id", "")
        if not tid:
            return
        shutil.rmtree(os.path.join(self.turns_dir, tid), ignore_errors=True)

    def _append_turn(self, profile: str, turn: dict):
        turns = self._load_history(profile)
        turns.append(turn)
        while len(turns) > self.history_max_turns:
            dropped = turns.pop(0)
            self._delete_turn_files(dropped)
        self._save_history(profile, turns)

    def _clear_history(self, profile: str):
        for turn in self._load_history(profile):
            self._delete_turn_files(turn)
        path = self._history_path(profile)
        try:
            os.remove(path)
        except OSError:
            pass

    def _reindex_all_profiles(self):
        try:
            if os.path.isdir(self.memory.brain_dir):
                for name in os.listdir(self.memory.brain_dir):
                    if os.path.isdir(os.path.join(self.memory.brain_dir, name)):
                        self.memory.reindex(name)
        except Exception:  # noqa: BLE001
            pass
        self.memory.reindex("default")

    # -- helpers --------------------------------------------------------

    def _llm_url(self) -> str:
        return (self.llm.get("base_url") or "").rstrip("/") + "/chat/completions"

    def _model_config(self, model_name: str = "") -> dict:
        name = (model_name or "").strip()
        for m in (self.llm.get("models") or []):
            if m.get("name") == name:
                return m
        return self.llm

    def stt_transcribe(self, audio_path: str) -> str:
        lang = (self.stt.get("language") or "").strip() or None
        d = self.tts_manager.transcribe(audio_path, language=lang)
        return (d.get("text") or "").strip()

    def tts_sentence(self, text: str, voice_id: str = "", instruction_id: str = "", model: str = "", voice: str = "", speed=None) -> str:
        text = clean_text(text)
        if not self.tts.get("enabled", True):
            return ""
        model = model or self.tts.get("model", "breeze")
        voice_id = voice_id or self.tts.get("voice_id", "")
        instruction_id = instruction_id or self.tts.get("instruction_id", "")
        settings = None
        if speed is not None and speed != "":
            try:
                settings = {"speed": float(speed)}
            except (TypeError, ValueError):
                settings = None
        result = self.tts_manager.synthesize(
            text, model,
            voice=voice,
            voice_id=voice_id,
            instruction_id=instruction_id,
            instruction=self.tts.get("instruction", ""),
            cfg_scale=self.tts.get("cfg_scale"),
            settings=settings,
        )
        return result["audio_path"]

    def _copy_tts_audio(self, audio_path: str, turn_id: str, idx: int) -> str:
        """Copy the synthesized audio into the turn dir so it survives cleanup
        and can be replayed from history."""
        ext = os.path.splitext(audio_path)[1] or ".ogg"
        turn_dir = os.path.join(self.turns_dir, turn_id)
        os.makedirs(turn_dir, exist_ok=True)
        local_name = f"assistant_{idx}{ext}"
        shutil.copy(audio_path, os.path.join(turn_dir, local_name))
        return f"/api/turn-file/{turn_id}/{local_name}"

    def _ditto_paths(self):
        ditto_cfg = self.config.get("ditto", {}) or {}
        base = _path_base(self.config)
        ditto_dir = _resolve_path(ditto_cfg.get("dir") or "ditto", base)
        python = _resolve_path(ditto_cfg.get("python") or "ditto/venv/Scripts/python.exe", base)
        trt = _resolve_path(ditto_cfg.get("tensorrt_lib") or "", base)
        images_dir = _resolve_path(ditto_cfg.get("images_dir") or "ditto/example/images", base)
        return ditto_dir, python, trt, images_dir

    def _start_ditto(self):
        """Spawn the DITTO avatar server as a subprocess (its own TensorRT venv)."""
        ditto_cfg = self.config.get("ditto", {}) or {}
        ditto_dir, venv_python, trt, _ = self._ditto_paths()
        env = dict(os.environ)
        if trt:
            env["PATH"] = trt + ";" + env.get("PATH", "")
        env["HEAD_MOTION_ALPHA"] = str(ditto_cfg.get("head_motion_alpha", 1.25))
        try:
            log_event("info", "starting DITTO avatar server")
            self._ditto_proc = subprocess.Popen(
                [venv_python, "ditto_server.py"],
                cwd=ditto_dir, env=env,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        except Exception as e:  # noqa: BLE001
            self._ditto_proc = None
            log_event("error", f"DITTO server failed to start: {type(e).__name__}: {e}")

    def _ditto_running(self) -> bool:
        p = self._ditto_proc
        return p is not None and p.poll() is None

    def _stop_ditto(self):
        p = self._ditto_proc
        if p is not None:
            log_event("info", "stopping DITTO avatar server")
            try:
                p.terminate()
                p.wait(timeout=10)
            except Exception:  # noqa: BLE001
                try:
                    p.kill()
                except Exception:  # noqa: BLE001
                    pass
        self._ditto_proc = None

    def ditto_avatar(self, audio_path: str, turn_id: str, idx: int, avatar: str = "") -> str:
        """Render the reply audio into a talking-head video via the DITTO server."""
        ditto_cfg = self.config.get("ditto", {}) or {}
        if not ditto_cfg.get("enabled", False):
            return ""
        base = (ditto_cfg.get("base_url") or "").rstrip("/")
        if not base or not os.path.exists(audio_path):
            return ""
        settings = self._avatar_settings(avatar or "")
        hm_alpha = settings.get("head_motion_alpha", 1.25)
        try:
            with open(audio_path, "rb") as f:
                files = {"audio": (os.path.basename(audio_path), f, "audio/ogg")}
                r = httpx.post(base + "/api/avatar", files=files, data={"avatar": avatar, "head_motion_alpha": str(hm_alpha)}, timeout=300)
            if r.status_code != 200:
                log_event("error", f"DITTO avatar generation failed (HTTP {r.status_code})")
                return ""
            turn_dir = os.path.join(self.turns_dir, turn_id)
            os.makedirs(turn_dir, exist_ok=True)
            local_name = f"avatar_{idx}.mp4"
            with open(os.path.join(turn_dir, local_name), "wb") as f:
                f.write(r.content)
            return f"/api/turn-file/{turn_id}/{local_name}"
        except Exception as e:  # noqa: BLE001
            log_event("error", f"DITTO avatar generation error: {type(e).__name__}: {e}")
            return ""

    def _avatars_dir(self):
        return self._ditto_paths()[3]

    def _static_avatars_dir(self):
        d = os.path.join(ROOT, "static", "avatars")
        os.makedirs(d, exist_ok=True)
        return d

    def _avatars(self):
        img_dir = self._avatars_dir()
        avatars = []
        if os.path.isdir(img_dir):
            for fn in sorted(os.listdir(img_dir)):
                stem, ext = os.path.splitext(fn)
                if ext.lower() in (".png", ".jpg", ".jpeg"):
                    avatars.append({"id": stem, "image": fn})
        return avatars

    def _avatar_settings_file(self):
        return os.path.join(ROOT, "avatars_settings.json")

    def _load_avatar_settings(self):
        try:
            with open(self._avatar_settings_file(), encoding="utf-8") as f:
                d = json.load(f)
                return d if isinstance(d, dict) else {}
        except (OSError, ValueError):
            return {}

    def _avatar_settings(self, stem):
        s = self._load_avatar_settings().get(stem, {})
        ditto_cfg = self.config.get("ditto", {}) or {}
        return {
            "head_motion_alpha": float(s.get("head_motion_alpha", ditto_cfg.get("head_motion_alpha", 1.25))),
            "idle_motion_alpha": float(s.get("idle_motion_alpha", ditto_cfg.get("idle_motion_alpha", 1.5))),
            "idle_length": float(s.get("idle_length", ditto_cfg.get("idle_length", 60))),
            "name": s.get("name") or f"Avatar {stem}",
        }

    def _gen_idle(self, image_name):
        stem = os.path.splitext(image_name)[0]
        ditto_dir, python, trt, _ = self._ditto_paths()
        settings = self._avatar_settings(stem)
        alpha = settings["idle_motion_alpha"]
        length = settings["idle_length"]
        img_path = os.path.join(self._avatars_dir(), image_name)
        out_path = os.path.join(ditto_dir, "idle_videos_15", stem + ".mp4")
        os.makedirs(os.path.dirname(out_path), exist_ok=True)
        env = dict(os.environ)
        if trt:
            env["PATH"] = trt + ";" + env.get("PATH", "")
        # gen_idle_worker.py uses StreamSDK directly with the configured idle motion.
        cmd = [python, os.path.join(ditto_dir, "gen_idle_worker.py"), img_path, out_path, str(alpha), str(length)]
        try:
            r = subprocess.run(cmd, cwd=ditto_dir, env=env, capture_output=True, text=True, timeout=300)
            if r.returncode == 0 and os.path.exists(out_path):
                shutil.copy(out_path, os.path.join(self._static_avatars_dir(), stem + ".mp4"))
                return True
        except Exception:
            pass
        return False

    def _cleanup_media(self):
        """Delete audio + avatar-video files older than their configured TTL."""
        try:
            srv = self.config.get("server", {}) or {}
            audio_ttl = float(srv.get("audio_ttl_hours", 48) or 0)
            video_ttl = float(srv.get("video_ttl_hours", 48) or 0)
            now = time.time()
            out_dir = getattr(self.tts_manager, "output_dir", "")
            if audio_ttl > 0 and out_dir and os.path.isdir(out_dir):
                for fn in os.listdir(out_dir):
                    p = os.path.join(out_dir, fn)
                    if os.path.isfile(p) and (now - os.path.getmtime(p)) > audio_ttl * 3600:
                        try:
                            os.remove(p)
                        except OSError:
                            pass
            if video_ttl > 0 and os.path.isdir(self.turns_dir):
                for tid in os.listdir(self.turns_dir):
                    d = os.path.join(self.turns_dir, tid)
                    if not os.path.isdir(d):
                        continue
                    for fn in os.listdir(d):
                        p = os.path.join(d, fn)
                        if fn.lower().endswith(".mp4") and os.path.isfile(p) and (now - os.path.getmtime(p)) > video_ttl * 3600:
                            try:
                                os.remove(p)
                            except OSError:
                                pass
                    if not os.listdir(d):
                        try:
                            os.rmdir(d)
                        except OSError:
                            pass
        except Exception:
            pass

    def _media_cleanup_loop(self):
        self._cleanup_media()
        while True:
            time.sleep(3600)
            self._cleanup_media()

    def _log_trim_loop(self):
        while True:
            time.sleep(3600)
            try:
                hours = float((self.config.get("logs", {}) or {}).get("retention_hours", 48) or 0)
                trim_log(hours)
            except Exception:
                pass

    def _llm_call(self, messages: list, tools=None, model_name: str = ""):
        cfg = self._model_config(model_name)
        base = (cfg.get("base_url") or self.llm.get("base_url") or "").rstrip("/")
        temperature = cfg.get("temperature")
        max_tokens = cfg.get("max_tokens")
        thinking = cfg.get("thinking")
        use_thinking = bool(thinking) if thinking not in (None, "") else self.thinking
        api_token = (cfg.get("api_token") or self.llm.get("api_token") or "").strip()
        headers = {"Authorization": "Bearer " + api_token} if api_token else {}
        payload = {
            "model": cfg.get("model") or self.llm.get("model", "qwen/qwen3-1.7b"),
            "messages": messages,
            "temperature": float(temperature) if temperature not in (None, "") else self.temperature,
            "stream": False,
            "max_tokens": int(max_tokens) if max_tokens not in (None, "") else self.max_tokens,
            "reasoning": {"effort": "high" if use_thinking else "none"},
        }
        if tools:
            payload["tools"] = tools
        r = httpx.post(base + "/chat/completions", json=payload, headers=headers, timeout=300)
        r.raise_for_status()
        d = r.json()
        msg = d["choices"][0]["message"]
        content = (msg.get("content") or "").strip()
        tool_calls = msg.get("tool_calls") or []
        reasoning = (msg.get("reasoning_content") or "").strip()
        return content, tool_calls, reasoning

    def _stream_llm_grouped(self, messages, on_chunk):
        """Stream the LLM and emit complete sentences grouped into chunks of at
        least min_chunk_chars (so short sentences combine)."""
        min_c = self.min_chunk_chars
        payload = {
            "model": self.llm.get("model", "qwen/qwen3-1.7b"),
            "messages": messages,
            "temperature": self.temperature,
            "stream": True,
        }
        buffer = ""
        consumed = 0
        pending = []
        with httpx.Client(timeout=None) as client:
            with client.stream("POST", self._llm_url(), json=payload) as resp:
                resp.raise_for_status()
                for line in resp.iter_lines():
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        obj = json.loads(data)
                        delta = obj["choices"][0]["delta"].get("content") or ""
                    except Exception:
                        continue
                    if not delta:
                        continue
                    buffer += delta
                    sentences, consumed = find_complete_sentences(buffer, consumed)
                    for s in sentences:
                        pending.append(s)
                    text = " ".join(pending).strip()
                    if len(text) >= min_c:
                        on_chunk(text)
                        pending = []
        if pending:
            on_chunk(" ".join(pending).strip())

    def _build_messages(self, profile: str, user_text: str, mem_block: str, image: str = None, skill: str = "", instruction_id: str = "", file_content: str = "", file_name: str = "", model_name: str = ""):
        self._reload_personalities()
        system = self.personalities.get(profile) or self.system_prompt
        if skill:
            injected = False
            for sk in [s.strip() for s in skill.split(",") if s.strip()]:
                skill_text = self._load_skill(sk)
                if skill_text:
                    system = system + "\n\n## Active skill: " + sk + "\n" + skill_text
                    injected = True
            if injected:
                system = system + "\n\nYou must follow the instructions of the active skill(s) for this turn."
        skill_index = self._skill_index()
        if skill_index:
            system = system + "\n\n" + skill_index
        voice_index = self._voice_design_index(instruction_id or self.tts.get("instruction_id", ""))
        if voice_index:
            system = system + "\n\n" + voice_index
        if mem_block:
            system = system + "\n\n" + mem_block
        now = datetime.datetime.now()
        system = system + "\n\nCurrent date and time: " + now.strftime("%Y-%m-%d %H:%M") + " (" + now.strftime("%A") + ")"
        history = []
        for turn in self._load_history(profile):
            history.append({"role": "user", "content": turn.get("user_text", "")})
            history.append({"role": "assistant", "content": turn.get("assistant_text", "")})
        cfg = self._model_config(model_name)
        max_hist = int(cfg.get("max_history_tokens") or self.max_history_tokens)
        total = sum(est_tokens(m["content"]) for m in history)
        while total > max_hist and len(history) > 2:
            removed = history.pop(0)
            total -= est_tokens(removed["content"])
        if file_content:
            file_block = "[Uploaded file: " + (file_name or "file") + "]\n" + file_content
            model_text = (user_text + "\n\n" + file_block) if user_text else file_block
        else:
            model_text = user_text
        if image:
            user_msg = {
                "role": "user",
                "content": [
                    {"type": "text", "text": model_text},
                    {"type": "image_url", "image_url": {"url": image}},
                ],
            }
        else:
            user_msg = {"role": "user", "content": model_text}
        return [{"role": "system", "content": system}] + history + [user_msg]

    def _update_history(self, profile: str, user_text: str, assistant_text: str, turn_id: str = "", user_audio_url=None, user_image_url=None, assistant_sentences=None, tool_calls=None):
        turn = {
            "user_text": user_text,
            "assistant_text": assistant_text,
            "turn_id": turn_id or "",
            "user_audio_url": user_audio_url,
            "user_image_url": user_image_url,
            "assistant_sentences": assistant_sentences or [],
            "tool_calls": tool_calls or [],
        }
        self._append_turn(profile, turn)

    # -- turn worker ----------------------------------------------------

    def _files_base(self, profile: str) -> str:
        key = (profile or "default").strip() or "default"
        key = key.replace("/", "_").replace("\\", "_").replace("..", "_")
        return os.path.join(ROOT, "files", key)

    def _safe_file_path(self, profile: str, path: str):
        base = self._files_base(profile)
        p = (path or "").strip().replace("\\", "/").lstrip("/")
        if ".." in p.split("/"):
            return None
        full = os.path.normpath(os.path.join(base, p))
        if full != base and not full.startswith(base + os.sep):
            return None
        return full

    def _tool_list_files(self, profile, path=""):
        base = self._files_base(profile)
        target = self._safe_file_path(profile, path) if path else base
        if target is None:
            return "error: invalid path"
        if not os.path.isdir(target):
            return "error: not a directory"
        try:
            names = sorted(os.listdir(target))
            out = [n + "/" if os.path.isdir(os.path.join(target, n)) else n for n in names]
            return "\n".join(out) or "(empty)"
        except Exception as e:  # noqa: BLE001
            return f"list error: {e}"

    def _tool_read_file(self, profile, path):
        target = self._safe_file_path(profile, path)
        if target is None:
            return "error: invalid path"
        if not os.path.isfile(target):
            return "error: file not found"
        try:
            with open(target, encoding="utf-8") as f:
                return f.read()
        except Exception as e:  # noqa: BLE001
            return f"read error: {e}"

    def _tool_write_file(self, profile, path, content):
        target = self._safe_file_path(profile, path)
        if target is None:
            return "error: invalid path"
        try:
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with open(target, "w", encoding="utf-8") as f:
                f.write(content or "")
            return f"wrote {os.path.basename(target)}"
        except Exception as e:  # noqa: BLE001
            return f"write error: {e}"

    def _tool_edit_file(self, profile, path, old_text, new_text):
        target = self._safe_file_path(profile, path)
        if target is None:
            return "error: invalid path"
        if not os.path.isfile(target):
            return "error: file not found"
        try:
            with open(target, encoding="utf-8") as f:
                content = f.read()
            if old_text not in content:
                return "error: text not found in file"
            with open(target, "w", encoding="utf-8") as f:
                f.write(content.replace(old_text, new_text, 1))
            return f"edited {os.path.basename(target)}"
        except Exception as e:  # noqa: BLE001
            return f"edit error: {e}"

    def _tool_delete_file(self, profile, path):
        target = self._safe_file_path(profile, path)
        if target is None:
            return "error: invalid path"
        if target == self._files_base(profile):
            return "error: cannot delete the root folder"
        try:
            if os.path.isdir(target):
                shutil.rmtree(target)
                return f"deleted folder {os.path.basename(target)}"
            if os.path.isfile(target):
                os.remove(target)
                return f"deleted {os.path.basename(target)}"
            return "error: file or folder not found"
        except Exception as e:  # noqa: BLE001
            return f"delete error: {e}"

    def _tool_send_file(self, profile, path):
        target = self._safe_file_path(profile, path)
        if target is None:
            return json.dumps({"error": "invalid path"})
        if not os.path.isfile(target):
            return json.dumps({"error": "file not found"})
        try:
            basename = os.path.basename(target)
            send_id = uuid.uuid4().hex[:10]
            send_dir = os.path.join(ROOT, "output", "sent", send_id)
            os.makedirs(send_dir, exist_ok=True)
            shutil.copy(target, os.path.join(send_dir, basename))
            return json.dumps({"ok": True, "message": f"Sent {basename} to the user for download.", "filename": basename, "url": f"/api/sent-file/{send_id}/{basename}"})
        except Exception as e:  # noqa: BLE001
            return json.dumps({"error": f"send error: {e}"})

    def _tool_write_skill(self, name, content):
        name = (name or "").strip().lower()
        name = re.sub(r"[^a-z0-9_-]", "", name)
        if not name:
            return "error: invalid skill name"
        if not content or not content.strip():
            return "error: skill content required"
        try:
            path = os.path.join(ROOT, "skills", name + ".md")
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                f.write(content.strip() + "\n")
            return f"saved skill {name}"
        except Exception as e:  # noqa: BLE001
            return f"write skill error: {e}"

    def _tool_delete_skill(self, name):
        name = (name or "").strip().lower()
        name = re.sub(r"[^a-z0-9_-]", "", name)
        if not name:
            return "error: invalid skill name"
        path = os.path.join(ROOT, "skills", name + ".md")
        if not os.path.isfile(path):
            return "error: skill not found"
        try:
            os.remove(path)
            return f"deleted skill {name}"
        except Exception as e:  # noqa: BLE001
            return f"delete skill error: {e}"

    def _run_tool(self, name: str, args: dict, profile: str = "default") -> str:
        if name == "web_search":
            return web_search(args.get("query", ""))
        if name == "web_fetch":
            return web_fetch(args.get("url", ""))
        if name == "weather":
            return weather(args.get("location", ""))
        if name == "list_files":
            return self._tool_list_files(profile, args.get("path", ""))
        if name == "read_file":
            return self._tool_read_file(profile, args.get("path", ""))
        if name == "write_file":
            return self._tool_write_file(profile, args.get("path", ""), args.get("content", ""))
        if name == "edit_file":
            return self._tool_edit_file(profile, args.get("path", ""), args.get("old_text", ""), args.get("new_text", ""))
        if name == "delete_file":
            return self._tool_delete_file(profile, args.get("path", ""))
        if name == "send_file":
            return self._tool_send_file(profile, args.get("path", ""))
        if name == "use_skill":
            skill_text = self._load_skill(args.get("name", ""))
            return skill_text or "error: skill not found"
        if name == "create_skill":
            return self._tool_write_skill(args.get("name", ""), args.get("content", ""))
        if name == "edit_skill":
            return self._tool_write_skill(args.get("name", ""), args.get("content", ""))
        if name == "delete_skill":
            return self._tool_delete_skill(args.get("name", ""))
        if name == "set_voice_design":
            return "voice design set to " + str(args.get("name", ""))
        if name == "remember":
            text = (args.get("text") or "").strip()
            if not text:
                return "error: nothing to remember"
            self.memory.add_item(text, profile)
            return "remembered"
        if name == "memory_search":
            block = self.memory.search_block(args.get("query", ""), profile)
            return block or "no relevant memory found"
        if name == "memory_day":
            return self.memory.read_day(args.get("date", ""), profile)
        if name == "delete_memory":
            return self.memory.delete_item(args.get("slug", ""), args.get("date"), profile)
        return "unknown tool"

    def _resolve_final_answer(self, messages: list, profile: str = "default", model_name: str = "", turn: dict = None):
        """Run the tool loop (native function calling, up to 3 rounds).
        Returns (final_answer_text, tool_calls_made)."""
        tools = TOOLS if self.config.get("toolcalling", {}).get("enabled", True) else None
        calls_made = []
        for i in range(10):
            content, tool_calls, reasoning = self._llm_call(messages, tools=tools, model_name=model_name)
            print("[tool] round " + str(i) + ": tool_calls=" + str(len(tool_calls)))
            if not tool_calls:
                return content, calls_made
            assistant_msg = {"role": "assistant", "content": content, "tool_calls": tool_calls}
            if reasoning:
                assistant_msg["reasoning_content"] = reasoning
            messages.append(assistant_msg)
            for tc in tool_calls:
                fn = tc.get("function", {})
                name = fn.get("name", "")
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except Exception:
                    args = {}
                entry = {"name": name, "args": args, "result": ""}
                calls_made.append(entry)
                if turn is not None:
                    turn["tool_calls"] = list(calls_made)
                result = self._run_tool(name, args, profile)
                entry["result"] = result[:300]
                if turn is not None:
                    turn["tool_calls"] = list(calls_made)
                print("[tool] " + name + "(" + json.dumps(args) + ") -> " + result[:200])
                model_result = result
                if name == "send_file":
                    try:
                        info = json.loads(result)
                        model_result = info.get("message") or info.get("error") or result
                    except Exception:
                        pass
                messages.append({"role": "tool", "tool_call_id": tc.get("id", ""), "content": model_result})
        content, _, _ = self._llm_call(messages, tools=None, model_name=model_name)
        return content, calls_made

    def _extract_memory(self, profile: str, user_text: str, assistant_text: str):
        try:
            prompt = [
                {"role": "system", "content": "You extract durable facts from a conversation. Extract ONLY stable facts about the user: their identity (name, who they are), preferences, relationships, and decisions. Do NOT extract transient information (weather, news, current events), the user's questions or requests, or generic answers. If the user shared a durable fact, output ONE concise sentence. If nothing durable, output exactly: NONE"},
                {"role": "user", "content": "User said: " + user_text + "\nAssistant said: " + assistant_text},
            ]
            result, _, _ = self._llm_call(prompt)
            print("[memory] extraction result: " + repr(result))
            if result and result.upper() != "NONE":
                self.memory.add_item(result, profile)
                print("[memory] wrote item")
        except Exception as e:  # noqa: BLE001
            print("[memory] extraction failed: " + repr(e))

    def _curate_today(self, profile: str):
        """Curate a profile's today brain file: dedup, merge, remove noise."""
        date = datetime.date.today().isoformat()
        fpath = os.path.join(self.memory._profile_dir(profile), date + ".md")
        if not os.path.isfile(fpath):
            return
        content = open(fpath, encoding="utf-8").read().strip()
        if not content:
            return
        try:
            prompt = [
                {"role": "system", "content": CURATION_PROMPT},
                {"role": "user", "content": content},
            ]
            curated, _, _ = self._llm_call(prompt)
            curated = (curated or "").strip()
            if curated.upper() == "EMPTY":
                curated = ""
            with open(fpath, "w", encoding="utf-8") as f:
                f.write(curated + ("\n" if curated else ""))
            self.memory.reindex(profile)
            print("[memory] curated today")
        except Exception as e:  # noqa: BLE001
            print("[memory] curation failed: " + repr(e))

    def _queue_memory(self, profile: str, user_text: str, assistant_text: str):
        """Batch memory maintenance. Extraction runs per exchange, but the
        expensive daily curation only fires every `curate_every` turns, and
        never more than one curator at a time."""
        with self._mem_lock:
            self._mem_pending.append((profile, user_text, assistant_text))
            if len(self._mem_pending) < self.curation_interval or self._curator_busy:
                return
            pending = list(self._mem_pending)
            self._mem_pending = []
            self._curator_busy = True
        threading.Thread(target=self._curate_worker, args=(pending,), daemon=True).start()

    def _curate_worker(self, pending):
        try:
            for profile, user_text, assistant_text in pending:
                self._extract_memory(profile, user_text, assistant_text)
                self._curate_today(profile)
        except Exception as e:  # noqa: BLE001
            print("[memory] curation worker failed: " + repr(e))
        finally:
            with self._mem_lock:
                self._curator_busy = False

    def run_turn(self, turn_id: str, profile: str, audio_path, user_text: str = "", voice_id: str = "", instruction_id: str = "", mode: str = "", image_data: str = None, skill: str = "", model_name: str = "", file_content: str = "", file_name: str = "", tts_model: str = "", avatar: str = "", video: str = "1", gen_audio: str = "1", voice: str = "", speed: str = ""):
        turn = self.turns.get(turn_id)
        if turn is None:
            return
        try:
            if audio_path:
                user_text = self.stt_transcribe(audio_path)
            else:
                user_text = (user_text or "").strip()
            if image_data and not user_text:
                user_text = "Describe this image."
            if file_content:
                if len(file_content) > 30000:
                    file_content = file_content[:30000] + "\n... [truncated]"
                if not user_text:
                    user_text = "[Attached file: " + (file_name or "file") + "]"
            turn["user_text"] = user_text
            if not user_text and not file_content:
                raise RuntimeError("no user text")

            mem_block = self.memory.search_block(user_text, profile)
            messages = self._build_messages(profile, user_text, mem_block, image_data, skill, instruction_id, file_content, file_name, model_name)
            mode = (mode or self.default_mode or "chunked").lower()

            effective_iid = {"value": instruction_id}

            def add_sentence(text):
                text = clean_text(text)
                if not text:
                    return
                idx = len(turn["sentences"])
                audio_url = None
                video_url = ""
                try:
                    if gen_audio == "1":
                        audio_url = self.tts_sentence(text, voice_id=voice_id, instruction_id=effective_iid["value"], model=tts_model, voice=voice, speed=speed)
                    if audio_url:
                        audio_url = self._copy_tts_audio(audio_url, turn_id, idx)
                        local_name = audio_url.rsplit("/", 1)[-1]
                        local_audio = os.path.join(self.turns_dir, turn_id, local_name)
                        if os.path.exists(local_audio) and (video == "1"):
                            video_url = self.ditto_avatar(local_audio, turn_id, idx, avatar)
                except Exception as e:  # noqa: BLE001
                    audio_url = None
                    turn.setdefault("errors", []).append(str(e))
                turn["sentences"].append({
                    "index": idx,
                    "text": text,
                    "audio_url": audio_url,
                    "video_url": video_url,
                })

            final_answer, tool_calls_made = self._resolve_final_answer(messages, profile, model_name, turn)
            turn["tool_calls"] = tool_calls_made
            for tc in tool_calls_made:
                if tc.get("name") == "set_voice_design":
                    design_name = (tc.get("args") or {}).get("name", "")
                    resolved = self._voice_designs().get(design_name)
                    if resolved:
                        effective_iid["value"] = resolved
            final_answer = clean_text(final_answer)
            if not final_answer:
                final_answer = "Sorry, I couldn't come up with an answer."

            if mode == "full":
                add_sentence(final_answer)
            else:
                for c in chunk_text(final_answer, self.max_chunk_chars, self.min_chunk_chars):
                    add_sentence(c)

            assistant_sentences = [{"text": s["text"], "audio_url": s.get("audio_url"), "video_url": s.get("video_url")} for s in turn["sentences"]]
            self._update_history(profile, user_text, final_answer, turn_id=turn_id, user_audio_url=turn.get("user_audio_url"), user_image_url=turn.get("user_image_url"), assistant_sentences=assistant_sentences, tool_calls=tool_calls_made)
            turn["status"] = "done"

            # memory maintenance is batched + single-threaded (see _queue_memory)
            self._queue_memory(profile, user_text, final_answer)
        except Exception as e:  # noqa: BLE001
            turn["status"] = "error"
            turn["error"] = f"{type(e).__name__}: {e}"


def create_app(config: dict) -> FastAPI:
    app = FastAPI(title="Talk Server", version="1.0")
    app.state.app = TalkApp(config)
    app.mount("/static", StaticFiles(directory=os.path.join(ROOT, "static")), name="static")

    LOGIN_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Login - Yvette</title>
<style>
  body { margin:0; min-height:100vh; display:flex; align-items:center; justify-content:center; background:#0f1115; color:#e6e8ee; font-family:system-ui,sans-serif; }
  .box { background:#1a1d24; padding:32px; border-radius:16px; width:320px; }
  h1 { font-size:20px; margin:0 0 20px; }
  input { width:100%; padding:12px 14px; margin-bottom:12px; border-radius:8px; border:1px solid #2e3341; background:#232732; color:#e6e8ee; font-size:15px; box-sizing:border-box; }
  button { width:100%; padding:12px; border-radius:8px; border:none; background:#5b8def; color:#fff; font-size:15px; font-weight:600; cursor:pointer; }
  .err { color:#e05252; font-size:13px; margin-bottom:12px; }
</style>
</head>
<body>
<div class="box">
  <h1>Yvette</h1>
  __ERROR__
  <form method="post" action="/login">
    __NEXT__
    <input name="username" placeholder="Username" autocomplete="username" required>
    <input type="password" name="password" placeholder="Password" autocomplete="current-password" required>
    <button type="submit">Login</button>
  </form>
</div>
</body>
</html>"""

    @app.middleware("http")
    async def auth_middleware(request, call_next):
        path = request.url.path
        if path == "/login" or path == "/api/tts" or path.startswith("/static/") or path == "/favicon.ico":
            return await call_next(request)
        a = app.state.app
        token = request.cookies.get("talk_session")
        if token and token in a.sessions:
            return await call_next(request)
        if path.startswith("/api/"):
            return JSONResponse({"detail": "unauthorized"}, status_code=401)
        return RedirectResponse("/login?next=" + quote(path, safe="/"), status_code=303)

    @app.get("/login")
    def login_page(request: Request):
        error = '<div class="err">Invalid username or password</div>' if request.query_params.get("error") else ""
        next_url = _safe_next(request.query_params.get("next", ""))
        next_field = f'<input type="hidden" name="next" value="{next_url}">' if next_url else ""
        return HTMLResponse(LOGIN_HTML.replace("__ERROR__", error).replace("__NEXT__", next_field))

    @app.post("/login")
    async def login(username: str = Form(""), password: str = Form(""), next: str = Form("")):
        a = app.state.app
        target = _safe_next(next) or "/"
        if username.strip() == a.auth.get("username", "") and password == a.auth.get("password", ""):
            token = secrets.token_hex(32)
            a.sessions.add(token)
            resp = RedirectResponse(target, status_code=303)
            resp.set_cookie("talk_session", token, httponly=True, samesite="lax", max_age=30 * 24 * 3600)
            return resp
        err_target = "/login?error=1"
        if target and target != "/":
            err_target += "&next=" + quote(target, safe="/")
        return RedirectResponse(err_target, status_code=303)

    @app.get("/logout")
    def logout(request: Request):
        a = app.state.app
        token = request.cookies.get("talk_session")
        if token:
            a.sessions.discard(token)
        resp = RedirectResponse("/login", status_code=303)
        resp.delete_cookie("talk_session")
        return resp

    @app.get("/")
    def index():
        return FileResponse(
            os.path.join(ROOT, "static", "index.html"),
            headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
        )

    @app.get("/admin")
    def admin_index():
        return FileResponse(
            os.path.join(ROOT, "static", "admin", "index.html"),
            headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
        )

    @app.post("/api/talk")
    async def talk(
        audio: Optional[UploadFile] = File(None),
        image: Optional[UploadFile] = File(None),
        file: Optional[UploadFile] = File(None),
        text: str = Form(""),
        conv_id: str = Form(""),
        voice_id: str = Form(""),
        instruction_id: str = Form(""),
        mode: str = Form(""),
        personality: str = Form(""),
        skill: str = Form(""),
        model: str = Form(""),
        tts_model: str = Form(""),
        avatar: str = Form(""),
        video: str = Form("1"),
        gen_audio: str = Form("1"),
        voice: str = Form(""),
        speed: str = Form(""),
    ):
        a = app.state.app
        user_text = (text or "").strip()

        conv_id = conv_id or uuid.uuid4().hex
        turn_id = uuid.uuid4().hex

        turn_dir = os.path.join(ROOT, "output", "turns", turn_id)
        audio_path = None
        user_audio_url = None
        if audio is not None:
            suffix = os.path.splitext(audio.filename or "audio.webm")[1] or ".webm"
            os.makedirs(turn_dir, exist_ok=True)
            audio_path = os.path.join(turn_dir, "user" + suffix)
            with open(audio_path, "wb") as f:
                f.write(await audio.read())
            user_audio_url = "/api/turn-audio/" + turn_id

        image_data = None
        user_image_url = None
        if image is not None:
            img_bytes = await image.read()
            mime = _image_mime(image.filename)
            image_data = f"data:{mime};base64," + base64.b64encode(img_bytes).decode()
            ext = os.path.splitext(image.filename or "image.png")[1] or ".png"
            os.makedirs(turn_dir, exist_ok=True)
            image_path = os.path.join(turn_dir, "image" + ext)
            with open(image_path, "wb") as f:
                f.write(img_bytes)
            user_image_url = "/api/turn-image/" + turn_id
            # also save a copy to the profile's personal files folder
            try:
                prof = (personality or "default").strip() or "default"
                files_base = a._files_base(prof)
                os.makedirs(files_base, exist_ok=True)
                img_safe_name = os.path.basename(image.filename or "image.png").replace("..", "_")
                with open(os.path.join(files_base, img_safe_name), "wb") as f:
                    f.write(img_bytes)
            except Exception:
                pass

        file_content = None
        file_name = None
        user_file_url = None
        if file is not None:
            file_name = file.filename or "file"
            file_bytes = await file.read()
            safe_name = os.path.basename(file_name).replace("..", "_")
            ext = os.path.splitext(safe_name)[1] or ""
            stored_name = "file" + ext
            os.makedirs(turn_dir, exist_ok=True)
            file_path = os.path.join(turn_dir, stored_name)
            with open(file_path, "wb") as f:
                f.write(file_bytes)
            user_file_url = "/api/turn-file/" + turn_id + "/" + stored_name
            try:
                file_content = file_bytes.decode("utf-8")
            except UnicodeDecodeError:
                file_content = None
            # also save a copy to the profile's personal files folder so the model can find it in later turns
            try:
                prof = (personality or "default").strip() or "default"
                files_base = a._files_base(prof)
                os.makedirs(files_base, exist_ok=True)
                with open(os.path.join(files_base, safe_name), "wb") as f:
                    f.write(file_bytes)
            except Exception:
                pass

        if not audio_path and not user_text and not image_data and not file_content:
            raise HTTPException(400, "audio, text, image, or file required")

        turn = {
            "turn_id": turn_id,
            "conv_id": conv_id,
            "status": "processing",
            "user_text": "",
            "user_audio_url": user_audio_url,
            "user_image_url": user_image_url,
            "user_file_url": user_file_url,
            "sentences": [],
            "tool_calls": [],
            "errors": [],
            "error": None,
            "created": time.time(),
        }
        with a.lock:
            a.turns[turn_id] = turn
        profile = (personality or "default").strip() or "default"
        threading.Thread(target=a.run_turn, args=(turn_id, profile, audio_path, user_text, voice_id, instruction_id, mode, image_data, skill, model, file_content, file_name, tts_model, avatar, video, gen_audio, voice, speed), daemon=True).start()
        return {"ok": True, "turn_id": turn_id, "conv_id": conv_id}

    @app.get("/api/talk/status/{turn_id}")
    def status(turn_id: str):
        a = app.state.app
        turn = a.turns.get(turn_id)
        if turn is None:
            raise HTTPException(404, "turn not found")
        return {
            "turn_id": turn["turn_id"],
            "conv_id": turn["conv_id"],
            "status": turn["status"],
            "user_text": turn["user_text"],
            "user_audio_url": turn.get("user_audio_url"),
            "user_image_url": turn.get("user_image_url"),
            "user_file_url": turn.get("user_file_url"),
            "sentences": list(turn["sentences"]),
            "tool_calls": list(turn.get("tool_calls", [])),
            "error": turn["error"],
        }

    @app.post("/api/tts")
    async def tts_full(
        text: str = Form(...),
        model: str = Form(""),
        voice: str = Form(""),
        voice_id: str = Form(""),
        instruction_id: str = Form(""),
        instruction: str = Form(""),
        output_format: str = Form(""),
        cfg_scale: Optional[float] = Form(None),
        seed: Optional[int] = Form(None),
    ):
        """Generate speech with the configured voice. Returns a JSON with the audio URL."""
        a = app.state.app
        if not text or not text.strip():
            raise HTTPException(400, "text required")
        try:
            result = a.tts_manager.synthesize(
                text.strip(),
                model or "",
                voice=voice,
                voice_id=voice_id,
                instruction_id=instruction_id,
                instruction=instruction,
                cfg_scale=cfg_scale,
                seed=seed,
                output_format=output_format,
            )
        except Exception as e:  # noqa: BLE001
            raise HTTPException(500, f"tts failed: {e}")
        return {
            "ok": True,
            "audio_url": f"/api/audio/{result['filename']}",
            "filename": result["filename"],
            "duration_sec": result["duration_sec"],
            "sample_rate": result["sample_rate"],
            "model": result["model"],
        }

    @app.get("/api/voices")
    def voices():
        a = app.state.app
        return {"voices": a.tts_manager.voices.list()}

    def _clean_personality_name(name: str) -> str:
        name = (name or "").strip()
        if not name:
            raise HTTPException(400, "name is required")
        if name.lower() == "default":
            raise HTTPException(400, "'default' is reserved")
        if not re.fullmatch(r"[A-Za-z0-9 _-]+", name):
            raise HTTPException(400, "name may only contain letters, numbers, spaces, dashes and underscores")
        return name

    @app.get("/api/personality/default")
    def personality_default():
        a = app.state.app
        return {"prompt": a._load_personality()}

    @app.post("/api/personality/default")
    async def save_personality_default(prompt: str = Form("")):
        a = app.state.app
        prompt = (prompt or "").replace("\r\n", "\n").replace("\r", "\n").strip()
        with open(a.personality_file, "w", encoding="utf-8", newline="\n") as f:
            f.write(prompt + "\n")
        a._reload_personalities()
        return {"ok": True}

    @app.get("/api/personalities")
    def personalities():
        a = app.state.app
        a._reload_personalities()
        return {"personalities": [{"id": k, "name": k, "prompt": v} for k, v in a.personalities.items()]}

    @app.post("/api/personalities/add")
    async def add_personality(name: str = Form(""), prompt: str = Form("")):
        a = app.state.app
        name = _clean_personality_name(name)
        path = os.path.join(ROOT, "personalities", name + ".md")
        if os.path.exists(path):
            raise HTTPException(409, "a personality with this name already exists")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write((prompt or "").replace("\r\n", "\n").replace("\r", "\n").strip() + "\n")
        a._reload_personalities()
        return {"id": name, "name": name}

    @app.put("/api/personalities/{pid}")
    async def edit_personality(pid: str, name: str = Form(""), prompt: str = Form("")):
        a = app.state.app
        old = os.path.basename(pid or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9 _-]+", old):
            raise HTTPException(400, "invalid id")
        new_name = _clean_personality_name(name)
        pers_dir = os.path.join(ROOT, "personalities")
        old_path = os.path.join(pers_dir, old + ".md")
        new_path = os.path.join(pers_dir, new_name + ".md")
        if not os.path.exists(old_path):
            raise HTTPException(404, "personality not found")
        if new_name != old and os.path.exists(new_path):
            raise HTTPException(409, "a personality with this name already exists")
        with open(new_path, "w", encoding="utf-8", newline="\n") as f:
            f.write((prompt or "").replace("\r\n", "\n").replace("\r", "\n").strip() + "\n")
        if new_name != old:
            os.remove(old_path)
        a._reload_personalities()
        return {"id": new_name, "name": new_name}

    @app.post("/api/personalities/remove")
    async def remove_personality(id: str = Form("")):
        a = app.state.app
        stem = os.path.basename((id or "").strip())
        if not re.fullmatch(r"[A-Za-z0-9 _-]+", stem) or stem.lower() == "default":
            raise HTTPException(400, "invalid id")
        path = os.path.join(ROOT, "personalities", stem + ".md")
        if os.path.exists(path):
            os.remove(path)
            a._reload_personalities()
            return {"ok": True}
        return {"ok": False}

    @app.get("/api/skills")
    def list_skills():
        a = app.state.app
        return {"skills": a._list_skills()}

    @app.get("/api/skills/{name}")
    def get_skill(name: str):
        a = app.state.app
        name = (name or "").strip().lower()
        name = re.sub(r"[^a-z0-9_-]", "", name)
        path = os.path.join(ROOT, "skills", name + ".md")
        if not os.path.isfile(path):
            raise HTTPException(404, "skill not found")
        return {"name": name, "content": a._load_skill(name)}

    @app.post("/api/skills")
    async def add_skill(name: str = Form(""), content: str = Form("")):
        a = app.state.app
        name = (name or "").strip().lower()
        name = re.sub(r"[^a-z0-9_-]", "", name)
        if not name:
            raise HTTPException(400, "invalid skill name")
        if not content or not content.strip():
            raise HTTPException(400, "skill content required")
        path = os.path.join(ROOT, "skills", name + ".md")
        if os.path.exists(path):
            raise HTTPException(409, "a skill with this name already exists")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        content = content.replace("\r\n", "\n").replace("\r", "\n").strip()
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write(content + "\n")
        return {"ok": True, "name": name}

    @app.put("/api/skills/{name}")
    async def edit_skill(name: str, content: str = Form("")):
        a = app.state.app
        name = (name or "").strip().lower()
        name = re.sub(r"[^a-z0-9_-]", "", name)
        if not name:
            raise HTTPException(400, "invalid skill name")
        if not content or not content.strip():
            raise HTTPException(400, "skill content required")
        path = os.path.join(ROOT, "skills", name + ".md")
        if not os.path.isfile(path):
            raise HTTPException(404, "skill not found")
        content = content.replace("\r\n", "\n").replace("\r", "\n").strip()
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write(content + "\n")
        return {"ok": True, "name": name}

    @app.delete("/api/skills/{name}")
    async def delete_skill(name: str):
        a = app.state.app
        name = (name or "").strip().lower()
        name = re.sub(r"[^a-z0-9_-]", "", name)
        if not name:
            raise HTTPException(400, "invalid skill name")
        path = os.path.join(ROOT, "skills", name + ".md")
        if not os.path.isfile(path):
            raise HTTPException(404, "skill not found")
        os.remove(path)
        return {"ok": True}

    @app.get("/api/models")
    def list_models():
        a = app.state.app
        models = a.llm.get("models") or []
        return {"models": [{"name": m.get("name", "")} for m in models]}

    @app.get("/api/llm/defaults")
    def llm_defaults():
        a = app.state.app
        llm = a.llm
        return {
            "context_tokens": int(llm.get("context_tokens", 32000)),
            "max_history_tokens": int(llm.get("max_history_tokens", 24000)),
            "temperature": float(llm.get("temperature", 0.7)),
            "max_tokens": int(llm.get("max_tokens", 8192)),
            "thinking": bool(llm.get("thinking", False)),
        }

    @app.post("/api/llm/defaults")
    async def save_llm_defaults(
        context_tokens: str = Form(""),
        max_history_tokens: str = Form(""),
        temperature: str = Form(""),
        max_tokens: str = Form(""),
        thinking: str = Form(""),
    ):
        a = app.state.app
        llm = a.llm
        try:
            if context_tokens.strip():
                llm["context_tokens"] = int(context_tokens)
            if max_history_tokens.strip():
                llm["max_history_tokens"] = int(max_history_tokens)
            if temperature.strip():
                llm["temperature"] = float(temperature)
            if max_tokens.strip():
                llm["max_tokens"] = int(max_tokens)
        except ValueError:
            raise HTTPException(400, "invalid numeric value")
        llm["thinking"] = (thinking or "0").strip().lower() in ("1", "true", "yes", "on")
        _save_config(a.config)
        a.max_history_tokens = int(llm.get("max_history_tokens", 24000))
        a.temperature = float(llm.get("temperature", 0.7))
        a.max_tokens = int(llm.get("max_tokens", 8192))
        a.thinking = bool(llm.get("thinking", False))
        return {"ok": True}

    @app.get("/api/llm/models")
    def llm_models():
        a = app.state.app
        defaults = {
            "context_tokens": int(a.llm.get("context_tokens", 32000)),
            "max_history_tokens": int(a.llm.get("max_history_tokens", 24000)),
            "temperature": float(a.llm.get("temperature", 0.7)),
            "max_tokens": int(a.llm.get("max_tokens", 8192)),
            "thinking": bool(a.llm.get("thinking", False)),
        }
        models = []
        for m in (a.llm.get("models") or []):
            models.append({
                "name": m.get("name", ""),
                "base_url": m.get("base_url", ""),
                "model": m.get("model", ""),
                "context_tokens": int(m.get("context_tokens") or defaults["context_tokens"]),
                "max_history_tokens": int(m.get("max_history_tokens") or defaults["max_history_tokens"]),
                "temperature": float(m.get("temperature")) if m.get("temperature") not in (None, "") else defaults["temperature"],
                "max_tokens": int(m.get("max_tokens")) if m.get("max_tokens") not in (None, "") else defaults["max_tokens"],
                "thinking": bool(m.get("thinking")) if m.get("thinking") not in (None, "") else defaults["thinking"],
                "api_token": m.get("api_token", "") or "",
            })
        return {"models": models}

    @app.post("/api/llm/models/add")
    async def add_llm_model(
        name: str = Form(""),
        base_url: str = Form(""),
        model: str = Form(""),
        context_tokens: str = Form(""),
        max_history_tokens: str = Form(""),
        temperature: str = Form(""),
        max_tokens: str = Form(""),
        thinking: str = Form(""),
        api_token: str = Form(""),
    ):
        a = app.state.app
        name = (name or "").strip()
        base_url = (base_url or "").strip().rstrip("/")
        model = (model or "").strip()
        if not name or not base_url or not model:
            raise HTTPException(400, "name, base_url and model are required")
        if not re.fullmatch(r"[A-Za-z0-9 _-]+", name):
            raise HTTPException(400, "invalid name")
        models = a.llm.setdefault("models", [])
        if any(m.get("name") == name for m in models):
            raise HTTPException(409, "a model with this name already exists")
        entry = {"name": name, "base_url": base_url, "model": model}
        if context_tokens.strip():
            entry["context_tokens"] = int(context_tokens)
        if max_history_tokens.strip():
            entry["max_history_tokens"] = int(max_history_tokens)
        if temperature.strip():
            entry["temperature"] = float(temperature)
        if max_tokens.strip():
            entry["max_tokens"] = int(max_tokens)
        if thinking.strip():
            entry["thinking"] = thinking.strip().lower() in ("1", "true", "yes", "on")
        if api_token.strip():
            entry["api_token"] = api_token.strip()
        models.append(entry)
        _save_config(a.config)
        return {"ok": True}

    @app.put("/api/llm/models/{name}")
    async def edit_llm_model(
        name: str,
        new_name: str = Form(""),
        base_url: str = Form(""),
        model: str = Form(""),
        context_tokens: str = Form(""),
        max_history_tokens: str = Form(""),
        temperature: str = Form(""),
        max_tokens: str = Form(""),
        thinking: str = Form(""),
        api_token: str = Form(""),
    ):
        a = app.state.app
        old = os.path.basename(name or "").strip()
        models = a.llm.get("models") or []
        entry = next((m for m in models if m.get("name") == old), None)
        if entry is None:
            raise HTTPException(404, "model not found")
        new_name = (new_name or "").strip()
        if new_name and new_name != old:
            if not re.fullmatch(r"[A-Za-z0-9 _-]+", new_name):
                raise HTTPException(400, "invalid name")
            if any(m.get("name") == new_name for m in models):
                raise HTTPException(409, "a model with this name already exists")
            entry["name"] = new_name
        if base_url.strip():
            entry["base_url"] = base_url.strip().rstrip("/")
        if model.strip():
            entry["model"] = model.strip()
        if context_tokens.strip():
            entry["context_tokens"] = int(context_tokens)
        if max_history_tokens.strip():
            entry["max_history_tokens"] = int(max_history_tokens)
        if temperature.strip():
            entry["temperature"] = float(temperature)
        if max_tokens.strip():
            entry["max_tokens"] = int(max_tokens)
        if thinking.strip():
            entry["thinking"] = thinking.strip().lower() in ("1", "true", "yes", "on")
        if api_token.strip():
            entry["api_token"] = api_token.strip()
        _save_config(a.config)
        return {"ok": True}

    @app.post("/api/llm/models/remove")
    async def remove_llm_model(id: str = Form("")):
        a = app.state.app
        name = (id or "").strip()
        models = a.llm.get("models") or []
        new_models = [m for m in models if m.get("name") != name]
        if len(new_models) == len(models):
            return {"ok": False}
        a.llm["models"] = new_models
        _save_config(a.config)
        return {"ok": True}

    @app.post("/api/llm/fetch-models")
    async def fetch_models(base_url: str = Form("")):
        base = (base_url or "").strip().rstrip("/")
        if not base:
            raise HTTPException(400, "base_url is required")
        try:
            r = httpx.get(base + "/models", timeout=15)
            r.raise_for_status()
            d = r.json()
            models = [m.get("id", "") for m in d.get("data", []) if m.get("id")]
            return {"models": sorted(set(models))}
        except Exception as e:
            raise HTTPException(502, f"failed to fetch models: {e}")

    @app.post("/api/avatars/add")
    async def add_avatar(image: UploadFile = File(...), head_motion_alpha: str = Form(""), idle_motion_alpha: str = Form(""), idle_length: str = Form(""), name: str = Form("")):
        a = app.state.app
        try:
            data = await image.read()
            ext = os.path.splitext(image.filename or "avatar.png")[1].lower()
            if ext not in (".png", ".jpg", ".jpeg"):
                ext = ".png"
            img_dir = a._avatars_dir()
            os.makedirs(img_dir, exist_ok=True)
            used = {os.path.splitext(f)[0] for f in os.listdir(img_dir) if os.path.splitext(f)[1].lower() in (".png", ".jpg", ".jpeg")}
            nid = 1
            while str(nid) in used:
                nid += 1
            fn = f"{nid}{ext}"
            with open(os.path.join(img_dir, fn), "wb") as f:
                f.write(data)
            shutil.copy(os.path.join(img_dir, fn), os.path.join(a._static_avatars_dir(), fn))
            # store per-avatar settings
            if head_motion_alpha.strip() or idle_motion_alpha.strip() or idle_length.strip() or name.strip():
                settings = a._load_avatar_settings()
                s = settings.setdefault(str(nid), {})
                if head_motion_alpha.strip():
                    s["head_motion_alpha"] = float(head_motion_alpha)
                if idle_motion_alpha.strip():
                    s["idle_motion_alpha"] = float(idle_motion_alpha)
                if idle_length.strip():
                    s["idle_length"] = float(idle_length)
                if name.strip():
                    s["name"] = name.strip()
                with open(a._avatar_settings_file(), "w", encoding="utf-8") as f:
                    json.dump(settings, f, indent=2)
            threading.Thread(target=a._gen_idle, args=(fn,), daemon=True).start()
            return {"id": str(nid), "image": fn}
        except Exception as e:
            raise HTTPException(500, str(e))

    @app.post("/api/avatars/remove")
    async def remove_avatar(id: str = Form("")):
        a = app.state.app
        stem = os.path.basename((id or "").strip())
        if not re.fullmatch(r"[A-Za-z0-9_-]+", stem):
            raise HTTPException(400, "invalid id")
        removed = False
        img_dir = a._avatars_dir()
        for ext in (".png", ".jpg", ".jpeg"):
            p = os.path.join(img_dir, stem + ext)
            if os.path.exists(p):
                os.remove(p)
                removed = True
        static_avatars = a._static_avatars_dir()
        for name in (stem + ".mp4", stem + ".png", stem + ".jpg", stem + ".jpeg"):
            p = os.path.join(static_avatars, name)
            if os.path.exists(p):
                os.remove(p)
                removed = True
        ditto_dir = a._ditto_paths()[0]
        idle = os.path.join(ditto_dir, "idle_videos_15", stem + ".mp4")
        if os.path.exists(idle):
            os.remove(idle)
            removed = True
        return {"ok": removed}

    @app.get("/api/profiles")
    def list_profiles():
        a = app.state.app
        return {"profiles": a._public_profiles(), "default": a.profiles.get("_default", "")}

    @app.post("/api/profiles")
    async def save_profile(
        name: str = Form(...),
        personality: str = Form(""),
        voice_id: str = Form(""),
        instruction_id: str = Form(""),
        mode: str = Form(""),
        model: str = Form(""),
        tts_model: str = Form(""),
        voice: str = Form(""),
        speed: str = Form(""),
    ):
        a = app.state.app
        name = name.strip()
        if not name or name == "_default":
            raise HTTPException(400, "invalid profile name")
        a.profiles[name] = {
            "personality": personality,
            "voice_id": voice_id,
            "instruction_id": instruction_id,
            "mode": mode,
            "model": model,
            "tts_model": tts_model,
            "voice": voice,
            "speed": speed,
        }
        a._save_profiles()
        return {"ok": True, "profiles": a._public_profiles(), "default": a.profiles.get("_default", "")}

    @app.post("/api/profiles/set-default")
    async def set_default_profile(name: str = Form("")):
        a = app.state.app
        name = name.strip()
        if name and name in a.profiles and name != "_default":
            a.profiles["_default"] = name
            a._save_profiles()
        return {"ok": True, "default": a.profiles.get("_default", "")}

    @app.post("/api/profiles/delete")
    async def delete_profile(name: str = Form("")):
        a = app.state.app
        name = name.strip()
        if name and name in a.profiles:
            del a.profiles[name]
            if a.profiles.get("_default") == name:
                a.profiles.pop("_default", None)
            a._save_profiles()
        return {"ok": True, "profiles": a._public_profiles(), "default": a.profiles.get("_default", "")}

    @app.get("/api/turn-audio/{turn_id}")
    def turn_audio(turn_id: str):
        if "/" in turn_id or "\\" in turn_id or ".." in turn_id:
            raise HTTPException(400, "bad turn id")
        turn_dir = os.path.join(ROOT, "output", "turns", turn_id)
        if not os.path.isdir(turn_dir):
            raise HTTPException(404, "audio not found")
        for fname in sorted(os.listdir(turn_dir)):
            if fname.startswith("user"):
                path = os.path.join(turn_dir, fname)
                media = "audio/ogg" if fname.endswith(".ogg") else ("audio/webm" if fname.endswith(".webm") else "audio/wav")
                return FileResponse(path, media_type=media)
        raise HTTPException(404, "audio not found")

    @app.get("/api/turn-image/{turn_id}")
    def turn_image(turn_id: str):
        if "/" in turn_id or "\\" in turn_id or ".." in turn_id:
            raise HTTPException(400, "bad turn id")
        turn_dir = os.path.join(ROOT, "output", "turns", turn_id)
        if not os.path.isdir(turn_dir):
            raise HTTPException(404, "image not found")
        for fname in sorted(os.listdir(turn_dir)):
            if fname.startswith("image"):
                path = os.path.join(turn_dir, fname)
                if fname.endswith(".png"):
                    media = "image/png"
                elif fname.endswith((".jpg", ".jpeg")):
                    media = "image/jpeg"
                else:
                    media = "image/webp"
                return FileResponse(path, media_type=media)
        raise HTTPException(404, "image not found")

    @app.get("/api/turn-file/{turn_id}/{filename}")
    def turn_file(turn_id: str, filename: str):
        if "/" in turn_id or "\\" in turn_id or ".." in turn_id:
            raise HTTPException(400, "bad turn id")
        if "/" in filename or "\\" in filename or ".." in filename:
            raise HTTPException(400, "bad filename")
        path = os.path.join(ROOT, "output", "turns", turn_id, filename)
        if not os.path.isfile(path):
            raise HTTPException(404, "file not found")
        media = "audio/ogg" if filename.endswith(".ogg") else ("audio/wav" if filename.endswith(".wav") else "application/octet-stream")
        return FileResponse(path, media_type=media)

    @app.get("/api/sent-file/{send_id}/{filename}")
    def sent_file(send_id: str, filename: str):
        if "/" in send_id or "\\" in send_id or ".." in send_id:
            raise HTTPException(400, "bad send id")
        if "/" in filename or "\\" in filename or ".." in filename:
            raise HTTPException(400, "bad filename")
        path = os.path.join(ROOT, "output", "sent", send_id, filename)
        if not os.path.isfile(path):
            raise HTTPException(404, "file not found")
        return FileResponse(path, media_type="application/octet-stream", filename=filename)

    @app.get("/api/audio/{filename}")
    def proxy_audio(filename: str):
        if "/" in filename or "\\" in filename or ".." in filename:
            raise HTTPException(400, "bad filename")
        a = app.state.app
        path = os.path.join(a.tts_manager.output_dir, filename)
        if not os.path.isfile(path):
            raise HTTPException(404, "audio not found")
        media = "audio/ogg" if filename.endswith(".ogg") else "audio/wav"
        return FileResponse(path, media_type=media)

    @app.post("/api/memory")
    async def add_memory(text: str = Form(...), profile: str = Form("")):
        a = app.state.app
        a.memory.add_item(text, profile or "default")
        return {"ok": True}

    @app.get("/api/history")
    def get_history(profile: str = "default"):
        a = app.state.app
        return {"turns": a._load_history(profile or "default")}

    @app.post("/api/history/clear")
    async def clear_history(profile: str = Form("")):
        a = app.state.app
        a._clear_history(profile or "default")
        return {"ok": True}

    @app.post("/api/memory/clear")
    async def clear_memory(profile: str = Form("")):
        a = app.state.app
        a.memory.clear(profile or "default")
        return {"ok": True}


    # ===== admin / TTS engine endpoints =====

    @app.get("/api/health")
    def health():
        a = app.state.app
        return {"models": a.tts_manager.model_status()}

    @app.get("/api/tts/status")
    def tts_status():
        a = app.state.app
        return {"models": a.tts_manager.model_status()}

    @app.post("/api/models/unload")
    def unload_model(model: str = Form("")):
        a = app.state.app
        if not model:
            raise HTTPException(400, "model name required")
        a.tts_manager.unload(model)
        return {"ok": True, "model": model}

    @app.post("/api/models/load")
    async def load_model(model: str = Form("")):
        a = app.state.app
        if not model:
            raise HTTPException(400, "model name required")
        try:
            a.tts_manager.load(model)
        except Exception as e:
            raise HTTPException(500, str(e))
        return {"ok": True, "model": model}

    @app.get("/api/engines")
    def engines():
        a = app.state.app
        out = []
        for name, be in a.tts_manager.backends.items():
            st = be.status()
            out.append({
                "name": name,
                "enabled": st.get("enabled", True),
                "available": st.get("available"),
                "loaded": st.get("loaded"),
                "supports_cloning": st.get("supports_cloning"),
                "supports_design": st.get("supports_design"),
                "settings": st.get("settings", []),
                "voices": st.get("voices", []),
                "default_voice_id": (a.config.get("models", {}).get(name, {}) or {}).get("default_voice_id", ""),
                "default_instruction_id": (a.config.get("models", {}).get(name, {}) or {}).get("default_instruction_id", ""),
            })
        return {"engines": out}

    @app.post("/api/voices")
    async def create_voice(
        name: str = Form(...),
        kind: str = Form("clone"),
        model: str = Form("breeze"),
        transcript: str = Form(""),
        instruction: str = Form(""),
        ref_audio: Optional[UploadFile] = File(None),
        auto_transcribe: bool = Form(True),
        engines: str = Form(""),
    ):
        a = app.state.app
        if not name or not name.strip():
            raise HTTPException(400, "name required")
        tmp_path = None
        if kind == "clone":
            if ref_audio is None:
                raise HTTPException(400, "clone profile requires ref_audio upload")
            suffix = os.path.splitext(ref_audio.filename or "audio.wav")[1] or ".wav"
            tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
            tmp.write(await ref_audio.read())
            tmp.close()
            tmp_path = tmp.name
            if auto_transcribe and not transcript.strip():
                try:
                    stt_result = a.tts_manager.transcribe(tmp_path)
                    transcript = stt_result["text"]
                except Exception as e:  # noqa: BLE001
                    os.unlink(tmp_path)
                    raise HTTPException(500, f"auto-transcribe failed: {e}")
            if not transcript.strip():
                os.unlink(tmp_path)
                raise HTTPException(400, "transcript required (or enable auto_transcribe)")
        try:
            eng = [e.strip() for e in (engines or "").split(",") if e.strip()]
            profile = a.tts_manager.voices.create(
                name=name.strip(), kind=kind, model=model,
                ref_audio_path=tmp_path, transcript=transcript, instruction=instruction,
                engines=eng,
            )
        except ValueError as e:
            if tmp_path and os.path.isfile(tmp_path):
                os.unlink(tmp_path)
            raise HTTPException(400, str(e))
        return {"ok": True, "voice": profile}

    @app.delete("/api/voices/{voice_id}")
    def delete_voice(voice_id: str):
        a = app.state.app
        if a.tts_manager.voices.delete(voice_id):
            return {"ok": True}
        raise HTTPException(404, "voice not found")

    @app.put("/api/voices/{voice_id}")
    async def update_voice(
        voice_id: str,
        name: str = Form(""),
        transcript: str = Form(""),
        instruction: str = Form(""),
        engines: str = Form(""),
    ):
        a = app.state.app
        fields = {}
        if name.strip():
            fields["name"] = name.strip()
        if transcript.strip():
            fields["transcript"] = transcript.strip()
        if instruction.strip():
            fields["instruction"] = instruction.strip()
        if (engines or "").strip():
            fields["engines"] = [e.strip() for e in engines.split(",") if e.strip()]
        updated = a.tts_manager.voices.update(voice_id, **fields)
        if updated is None:
            raise HTTPException(404, "voice not found")
        return {"ok": True, "voice": updated}

    @app.post("/api/stt")
    async def stt_endpoint(audio: UploadFile = File(...), language: str = Form("")):
        a = app.state.app
        suffix = os.path.splitext(audio.filename or "audio.wav")[1] or ".wav"
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
        tmp.write(await audio.read())
        tmp.close()
        try:
            result = a.tts_manager.transcribe(tmp.name, language=language or None)
        except Exception as e:  # noqa: BLE001
            os.unlink(tmp.name)
            raise HTTPException(500, f"transcribe failed: {e}")
        os.unlink(tmp.name)
        return {"ok": True, "text": result.get("text", ""), "language": result.get("language"), "duration_sec": result.get("duration_sec")}


    # ===== STT (Whisper) settings + model management =====

    @app.get("/api/stt/settings")
    def stt_settings():
        a = app.state.app
        cfg = a.config.get("stt", {}) or {}
        return {
            "model": cfg.get("model", "large-v3-turbo"),
            "language": cfg.get("language") or "",
            "device": cfg.get("device", "cuda"),
            "compute_type": cfg.get("compute_type", "float16"),
            "beam_size": int(cfg.get("beam_size", 5)),
            "enabled": bool(cfg.get("enabled", True)),
        }

    @app.post("/api/stt/settings")
    async def save_stt_settings(
        model: str = Form(""),
        language: str = Form(""),
        device: str = Form(""),
        compute_type: str = Form(""),
        beam_size: str = Form(""),
        enabled: str = Form(""),
    ):
        a = app.state.app
        cfg = a.config.setdefault("stt", {})
        if model:
            cfg["model"] = model
        cfg["language"] = (language or "").strip() or None
        if device:
            cfg["device"] = device
        if compute_type:
            cfg["compute_type"] = compute_type
        if beam_size:
            cfg["beam_size"] = int(beam_size)
        cfg["enabled"] = (enabled or "1").strip().lower() in ("1", "true", "yes", "on")
        st = a.tts_manager.stt
        st.config = cfg
        st.model_name = cfg.get("model", "large-v3-turbo")
        st.language = cfg.get("language")
        st.device = cfg.get("device", "cuda")
        st.compute_type = cfg.get("compute_type", "float16")
        st.beam_size = int(cfg.get("beam_size", 5))
        st.enabled = bool(cfg.get("enabled", True))
        st.reload()
        _save_config(a.config)
        return {"ok": True}

    @app.get("/api/stt/models")
    def stt_models():
        a = app.state.app
        return {"models": a.tts_manager.stt.list_models()}

    @app.post("/api/stt/download")
    async def stt_download(model: str = Form("")):
        a = app.state.app
        if not model:
            raise HTTPException(400, "model required")
        threading.Thread(target=a.tts_manager.stt.download_model, args=(model,), daemon=True).start()
        return {"ok": True, "model": model}

    @app.post("/api/stt/delete")
    def stt_delete(model: str = Form("")):
        a = app.state.app
        if not model:
            raise HTTPException(400, "model required")
        return {"ok": a.tts_manager.stt.delete_model(model), "model": model}

    @app.get("/api/stt/status")
    def stt_status():
        a = app.state.app
        st = a.tts_manager.stt
        return {"enabled": st.enabled, "loaded": st.is_loaded(), "model": st.model_name}

    @app.post("/api/stt/load")
    async def stt_load():
        a = app.state.app
        st = a.tts_manager.stt
        if st.is_loaded():
            return {"ok": True, "loaded": True}
        threading.Thread(target=st.load, daemon=True).start()
        return {"ok": True, "loading": True}

    @app.post("/api/stt/unload")
    async def stt_unload():
        a = app.state.app
        a.tts_manager.stt.unload()
        return {"ok": True, "loaded": False}


    # ===== engine settings =====

    @app.get("/api/engines/settings")
    def engine_settings():
        a = app.state.app
        out = []
        for name, spec in ENGINE_SETTINGS.items():
            cfg = a.config.get("models", {}).get(name, {}) or {}
            items = []
            for s in spec:
                item = dict(s)
                item["value"] = cfg.get(s["key"], s.get("default"))
                items.append(item)
            out.append({"name": name, "enabled": cfg.get("enabled", True), "settings": items})
        return {"engines": out}

    @app.post("/api/engines/settings")
    async def save_engine_settings(data: str = Form("")):
        a = app.state.app
        try:
            updates = json.loads(data or "{}")
        except ValueError:
            raise HTTPException(400, "invalid json")
        models = a.config.setdefault("models", {})
        for name, spec in ENGINE_SETTINGS.items():
            vals = updates.get(name)
            if not isinstance(vals, dict):
                continue
            cfg = models.setdefault(name, {})
            for s in spec:
                k = s["key"]
                if k not in vals:
                    continue
                v = vals[k]
                if v is None or v == "":
                    cfg[k] = None
                elif s["type"] == "bool":
                    cfg[k] = bool(v)
                elif s["type"] == "int":
                    cfg[k] = int(v)
                elif s["type"] == "float":
                    cfg[k] = float(v)
                else:
                    cfg[k] = str(v)
        _save_config(a.config)
        return {"ok": True}

    # ===== general settings =====

    @app.get("/api/general")
    def general_settings():
        a = app.state.app
        m = a.config.get("models", {}) or {}
        return {
            "unload_idle_minutes": int(m.get("unload_idle_minutes", 60)),
            "unload_others_before_load": bool(m.get("unload_others_before_load", True)),
        }

    @app.post("/api/general")
    async def save_general_settings(unload_idle_minutes: str = Form(""), unload_others_before_load: str = Form("")):
        a = app.state.app
        try:
            mins = int(unload_idle_minutes)
        except (TypeError, ValueError):
            mins = 60
        m = a.config.setdefault("models", {})
        m["unload_idle_minutes"] = mins
        m["unload_others_before_load"] = (unload_others_before_load or "1").strip().lower() in ("1", "true", "yes", "on")
        a.tts_manager.unload_idle_minutes = mins
        a.tts_manager.unload_others_before_load = m["unload_others_before_load"]
        _save_config(a.config)
        return {"ok": True, "unload_idle_minutes": mins, "unload_others_before_load": m["unload_others_before_load"]}

    # ===== memory settings =====

    @app.get("/api/memory/settings")
    def memory_settings():
        a = app.state.app
        mem = a.config.get("memory", {}) or {}
        emb = mem.get("embedding", {}) or {}
        return {
            "brain_dir": mem.get("brain_dir", "brain"),
            "db_path": mem.get("db_path", "brain.index.db"),
            "embedding_model": emb.get("model_name", "Qwen/Qwen3-Embedding-0.6B"),
            "embedding_device": emb.get("device", "cpu"),
            "top_k": int(mem.get("top_k", 5)),
            "min_score": float(mem.get("min_score", 0.3)),
            "max_chars": int(mem.get("max_chars", 1200)),
            "body_chars": int(mem.get("body_chars", 200)),
            "curate_every": int(mem.get("curate_every", 10)),
            "history_max_turns": int((a.config.get("history", {}) or {}).get("max_turns", 60)),
        }

    @app.post("/api/memory/settings")
    async def save_memory_settings(
        brain_dir: str = Form(""),
        db_path: str = Form(""),
        embedding_model: str = Form(""),
        embedding_device: str = Form(""),
        top_k: str = Form(""),
        min_score: str = Form(""),
        max_chars: str = Form(""),
        body_chars: str = Form(""),
        curate_every: str = Form(""),
        history_max_turns: str = Form(""),
    ):
        a = app.state.app
        mem = a.config.setdefault("memory", {})
        try:
            if brain_dir.strip(): mem["brain_dir"] = brain_dir.strip()
            if db_path.strip(): mem["db_path"] = db_path.strip()
            emb = mem.setdefault("embedding", {})
            if embedding_model.strip(): emb["model_name"] = embedding_model.strip()
            if embedding_device.strip(): emb["device"] = embedding_device.strip()
            if top_k.strip(): mem["top_k"] = int(top_k)
            if min_score.strip(): mem["min_score"] = float(min_score)
            if max_chars.strip(): mem["max_chars"] = int(max_chars)
            if body_chars.strip(): mem["body_chars"] = int(body_chars)
            if curate_every.strip(): mem["curate_every"] = int(curate_every)
            if history_max_turns.strip(): a.config.setdefault("history", {})["max_turns"] = int(history_max_turns)
        except ValueError:
            raise HTTPException(400, "invalid numeric value")
        _save_config(a.config)
        a.history_max_turns = int((a.config.get("history", {}) or {}).get("max_turns", 60))
        return {"ok": True}

    # ===== server settings =====

    @app.get("/api/server/settings")
    def server_settings():
        a = app.state.app
        srv = a.config.get("server", {}) or {}
        auth = a.config.get("auth", {}) or {}
        cert = os.path.join(ROOT, "cert.pem")
        key = os.path.join(ROOT, "key.pem")
        return {
            "host": srv.get("host", "0.0.0.0"),
            "port": int(srv.get("port", 8900)),
            "https": bool(srv.get("https", False)),
            "api_token": auth.get("api_token", ""),
            "ssl_ready": bool(os.path.isfile(cert) and os.path.isfile(key)),
        }

    @app.post("/api/server/settings")
    async def save_server_settings(
        host: str = Form(""),
        port: str = Form(""),
        https: str = Form(""),
    ):
        a = app.state.app
        srv = a.config.setdefault("server", {})
        if host.strip(): srv["host"] = host.strip()
        if port.strip():
            try:
                srv["port"] = int(port)
            except ValueError:
                raise HTTPException(400, "invalid port")
        srv["https"] = (https or "0").strip().lower() in ("1", "true", "yes", "on")
        _save_config(a.config)
        return {"ok": True}

    @app.post("/api/server/generate-token")
    async def generate_api_token():
        a = app.state.app
        auth = a.config.setdefault("auth", {})
        auth["api_token"] = secrets.token_hex(32)
        _save_config(a.config)
        return {"api_token": auth["api_token"]}

    @app.post("/api/server/generate-cert")
    async def generate_cert():
        a = app.state.app
        cert = os.path.join(ROOT, "cert.pem")
        key = os.path.join(ROOT, "key.pem")
        openssl = shutil.which("openssl") or r"C:\Program Files\Git\usr\bin\openssl.exe"
        if not os.path.isfile(openssl):
            raise HTTPException(500, "openssl not found (expected under Git)")
        try:
            r = subprocess.run(
                [openssl, "req", "-x509", "-newkey", "rsa:2048",
                 "-keyout", key, "-out", cert, "-days", "3650", "-nodes",
                 "-subj", "/CN=voice-ai"],
                capture_output=True, text=True, timeout=120)
        except Exception as e:
            raise HTTPException(500, "cert generation failed: " + str(e))
        if r.returncode != 0:
            raise HTTPException(500, "cert generation failed: " + (r.stderr or "").strip()[:300])
        return {"ok": True, "ssl_ready": True}

    # ===== auth settings =====

    @app.get("/api/auth/settings")
    def auth_settings():
        a = app.state.app
        auth = a.config.get("auth", {}) or {}
        hf = a.config.get("huggingface", {}) or {}
        return {
            "username": auth.get("username", ""),
            "password": "",
            "hf_token": hf.get("token", ""),
        }

    @app.post("/api/auth/settings")
    async def save_auth_settings(
        username: str = Form(""),
        password: str = Form(""),
        hf_token: str = Form(""),
    ):
        a = app.state.app
        auth = a.config.setdefault("auth", {})
        if username.strip(): auth["username"] = username.strip()
        if password.strip(): auth["password"] = password.strip()
        hf = a.config.setdefault("huggingface", {})
        if hf_token.strip(): hf["token"] = hf_token.strip()
        _save_config(a.config)
        return {"ok": True}

    @app.post("/api/auth/generate-password")
    async def generate_password():
        a = app.state.app
        auth = a.config.setdefault("auth", {})
        auth["password"] = secrets.token_urlsafe(16)
        _save_config(a.config)
        return {"password": auth["password"]}

    # ===== toolcalling =====

    @app.get("/api/toolcalling")
    def toolcalling_settings():
        a = app.state.app
        return {"enabled": bool((a.config.get("toolcalling", {}) or {}).get("enabled", True))}

    @app.post("/api/toolcalling")
    async def save_toolcalling(enabled: str = Form("")):
        a = app.state.app
        val = (enabled or "0").strip().lower() in ("1", "true", "yes", "on")
        a.config.setdefault("toolcalling", {})["enabled"] = val
        _save_config(a.config)
        return {"ok": True, "enabled": val}

    # ===== TTS general settings =====

    @app.get("/api/tts/general")
    def tts_general():
        a = app.state.app
        srv = a.config.get("server", {}) or {}
        chunk = a.config.get("chunking", {}) or {}
        tts = a.config.get("tts", {}) or {}
        return {
            "min_chunk_chars": int(chunk.get("min_chunk_chars", 100)),
            "max_chunk_chars": int(chunk.get("max_chunk_chars", 400)),
            "default_mode": chunk.get("default_mode", "chunked"),
            "output_format": srv.get("output_format", "ogg"),
            "gain_db": float(srv.get("gain_db", 0) or 0),
            "sample_rate": int(srv.get("sample_rate", 24000)),
            "audio_ttl_hours": float(srv.get("audio_ttl_hours", 48) or 0),
            "enabled": bool(tts.get("enabled", True)),
            "default_model": tts.get("model", "breeze"),
            "default_voice_id": tts.get("voice_id", ""),
            "default_instruction_id": tts.get("instruction_id", ""),
            "default_instruction": tts.get("instruction", ""),
            "default_cfg_scale": float(tts.get("cfg_scale", 4) or 4),
        }

    @app.post("/api/tts/general")
    async def save_tts_general(
        min_chunk_chars: str = Form(""),
        max_chunk_chars: str = Form(""),
        default_mode: str = Form(""),
        output_format: str = Form(""),
        gain_db: str = Form(""),
        sample_rate: str = Form(""),
        audio_ttl_hours: str = Form(""),
        enabled: str = Form(""),
        default_model: str = Form(""),
        default_voice_id: str = Form(""),
        default_instruction_id: str = Form(""),
        default_instruction: str = Form(""),
        default_cfg_scale: str = Form(""),
    ):
        a = app.state.app
        srv = a.config.setdefault("server", {})
        chunk = a.config.setdefault("chunking", {})
        tts = a.config.setdefault("tts", {})
        try:
            if min_chunk_chars.strip(): chunk["min_chunk_chars"] = int(min_chunk_chars)
            if max_chunk_chars.strip(): chunk["max_chunk_chars"] = int(max_chunk_chars)
            if default_mode.strip(): chunk["default_mode"] = default_mode.strip()
            if output_format.strip(): srv["output_format"] = output_format.strip()
            if gain_db.strip(): srv["gain_db"] = float(gain_db)
            if sample_rate.strip(): srv["sample_rate"] = int(sample_rate)
            if audio_ttl_hours.strip(): srv["audio_ttl_hours"] = float(audio_ttl_hours)
            tts["enabled"] = (enabled or "1").strip().lower() in ("1", "true", "yes", "on")
            if default_model.strip(): tts["model"] = default_model.strip()
            tts["voice_id"] = default_voice_id.strip()
            tts["instruction_id"] = default_instruction_id.strip()
            if default_instruction.strip(): tts["instruction"] = default_instruction.strip()
            if default_cfg_scale.strip(): tts["cfg_scale"] = float(default_cfg_scale)
        except ValueError:
            raise HTTPException(400, "invalid numeric value")
        _save_config(a.config)
        a.min_chunk_chars = int(chunk.get("min_chunk_chars", 100))
        a.max_chunk_chars = int(chunk.get("max_chunk_chars", 400))
        a.default_mode = chunk.get("default_mode", "chunked")
        a.tts_manager.output_format = srv.get("output_format", "ogg")
        a.tts_manager.sample_rate = int(srv.get("sample_rate", 24000))
        a.tts_manager.gain_db = float(srv.get("gain_db", 0) or 0)
        return {"ok": True}

    # ===== avatar general settings =====

    @app.get("/api/avatar/general")
    def avatar_general():
        a = app.state.app
        srv = a.config.get("server", {}) or {}
        return {"video_ttl_hours": float(srv.get("video_ttl_hours", 48) or 0)}

    @app.post("/api/avatar/general")
    async def save_avatar_general(video_ttl_hours: str = Form("")):
        a = app.state.app
        srv = a.config.setdefault("server", {})
        try:
            if video_ttl_hours.strip():
                srv["video_ttl_hours"] = float(video_ttl_hours)
        except ValueError:
            raise HTTPException(400, "invalid numeric value")
        _save_config(a.config)
        return {"ok": True}

    # ===== DITTO (video generation) management =====

    @app.get("/api/ditto/status")
    def ditto_status():
        a = app.state.app
        ditto_cfg = a.config.get("ditto", {}) or {}
        running = a._ditto_running()
        model_loaded = False
        if running:
            base = (ditto_cfg.get("base_url") or "").rstrip("/")
            try:
                r = httpx.get(base + "/api/health", timeout=3)
                model_loaded = bool(r.json().get("model_loaded", False))
            except Exception:  # noqa: BLE001
                model_loaded = False
        return {"enabled": bool(ditto_cfg.get("enabled", False)), "running": running, "model_loaded": model_loaded}

    @app.post("/api/ditto/toggle")
    async def ditto_toggle(enabled: str = Form("")):
        a = app.state.app
        val = (enabled or "0").strip().lower() in ("1", "true", "yes", "on")
        a.config.setdefault("ditto", {})["enabled"] = val
        _save_config(a.config)
        return {"ok": True, "enabled": val}

    @app.post("/api/ditto/load")
    async def ditto_load():
        a = app.state.app
        if a._ditto_running():
            return {"ok": True, "running": True}
        a._ditto_proc = None
        threading.Thread(target=a._start_ditto, daemon=True).start()
        return {"ok": True, "starting": True}

    @app.post("/api/ditto/unload")
    async def ditto_unload():
        a = app.state.app
        a._stop_ditto()
        return {"ok": True, "running": False}

    # ===== avatar defaults + idle regeneration =====

    @app.get("/api/avatar/defaults")
    def avatar_defaults():
        a = app.state.app
        d = a.config.get("ditto", {}) or {}
        return {
            "head_motion_alpha": float(d.get("head_motion_alpha", 1.25)),
            "idle_motion_alpha": float(d.get("idle_motion_alpha", 1.5)),
            "idle_length": float(d.get("idle_length", 60)),
        }

    @app.post("/api/avatar/defaults")
    async def save_avatar_defaults(
        head_motion_alpha: str = Form(""),
        idle_motion_alpha: str = Form(""),
        idle_length: str = Form(""),
    ):
        a = app.state.app
        d = a.config.setdefault("ditto", {})
        try:
            if head_motion_alpha.strip():
                d["head_motion_alpha"] = float(head_motion_alpha)
            if idle_motion_alpha.strip():
                d["idle_motion_alpha"] = float(idle_motion_alpha)
            if idle_length.strip():
                d["idle_length"] = float(idle_length)
        except ValueError:
            raise HTTPException(400, "invalid numeric value")
        _save_config(a.config)
        return {"ok": True}

    @app.post("/api/avatars/{avatar_id}/regen")
    async def regen_idle(avatar_id: str):
        a = app.state.app
        stem = os.path.basename(avatar_id or "")
        if not re.fullmatch(r"[A-Za-z0-9_-]+", stem):
            raise HTTPException(400, "invalid id")
        img = None
        for ext in (".png", ".jpg", ".jpeg"):
            p = os.path.join(a._avatars_dir(), stem + ext)
            if os.path.isfile(p):
                img = stem + ext
                break
        if img is None:
            raise HTTPException(404, "avatar not found")
        threading.Thread(target=a._gen_idle, args=(img,), daemon=True).start()
        return {"ok": True, "avatar": stem}

    # ===== avatar list with settings + edit =====

    @app.get("/api/avatars")
    def list_avatars():
        a = app.state.app
        settings = a._load_avatar_settings()
        avs = []
        for av in a._avatars():
            idle_ready = os.path.isfile(os.path.join(a._static_avatars_dir(), av["id"] + ".mp4"))
            avs.append({**av, "idle_ready": idle_ready, **a._avatar_settings(av["id"]), "overrides": av["id"] in settings})
        return {"avatars": avs}

    @app.put("/api/avatars/{avatar_id}")
    async def edit_avatar(avatar_id: str, image: UploadFile = File(None), head_motion_alpha: str = Form(""), idle_motion_alpha: str = Form(""), idle_length: str = Form(""), name: str = Form("")):
        a = app.state.app
        stem = os.path.basename(avatar_id or "")
        if not re.fullmatch(r"[A-Za-z0-9_-]+", stem):
            raise HTTPException(400, "invalid id")
        settings = a._load_avatar_settings()
        s = settings.setdefault(stem, {})
        if head_motion_alpha.strip():
            s["head_motion_alpha"] = float(head_motion_alpha)
        if idle_motion_alpha.strip():
            s["idle_motion_alpha"] = float(idle_motion_alpha)
        if idle_length.strip():
            s["idle_length"] = float(idle_length)
        if name.strip():
            s["name"] = name.strip()
        with open(a._avatar_settings_file(), "w", encoding="utf-8") as f:
            json.dump(settings, f, indent=2)
        # optional: replace the image and regenerate idle in the background
        if image is not None:
            data = await image.read()
            ext = os.path.splitext(image.filename or "avatar.png")[1].lower()
            if ext not in (".png", ".jpg", ".jpeg"):
                ext = ".png"
            img_dir = a._avatars_dir()
            for e in (".png", ".jpg", ".jpeg"):
                if e != ext:
                    old = os.path.join(img_dir, stem + e)
                    if os.path.exists(old):
                        os.remove(old)
            fn = stem + ext
            with open(os.path.join(img_dir, fn), "wb") as f:
                f.write(data)
            shutil.copy(os.path.join(img_dir, fn), os.path.join(a._static_avatars_dir(), fn))
            threading.Thread(target=a._gen_idle, args=(fn,), daemon=True).start()
        return {"ok": True}

    # ===== status / logs =====

    @app.get("/api/status")
    def system_status():
        a = app.state.app
        st = a.tts_manager.stt
        models_cfg = a.config.get("models", {}) or {}
        base = _path_base(a.config)

        stt_loaded = st.is_loaded()
        stt_available = st.is_installed(st.model_name)
        stt_vram = _dir_size_gb(st._cache_dir(st.model_name))
        if stt_vram is None:
            stt_vram = WHISPER_VRAM_GB.get(st.model_name, 2.0)

        tts_items = []
        tts_loaded_vram = 0.0
        for name, be in a.tts_manager.backends.items():
            s = be.status()
            loaded = bool(s.get("loaded"))
            vram = _tts_vram_est(name, models_cfg, base)
            if loaded:
                tts_loaded_vram += vram
            tts_items.append({
                "name": name,
                "enabled": bool(s.get("enabled", True)),
                "loaded": loaded,
                "available": bool(s.get("available", True)),
                "load_error": s.get("load_error") or "",
                "vram_est_gb": round(vram, 1),
            })

        ditto_cfg = a.config.get("ditto", {}) or {}
        ditto_running = a._ditto_running()
        ditto_dir = _resolve_path(ditto_cfg.get("dir") or "ditto", base)
        ditto_vram = _dir_size_gb(os.path.join(ditto_dir, "checkpoints", "ditto_trt_3090"))
        if ditto_vram is None:
            ditto_vram = DITTO_VRAM_GB

        emb_cfg = a.config.get("memory", {}).get("embedding", {}) or {}
        emb_model = emb_cfg.get("model_name", "Qwen/Qwen3-Embedding-0.6B")
        emb_device = emb_cfg.get("device", "cpu")
        emb_installed = os.path.isdir(_hf_cache_dir(emb_model))
        emb_loaded = bool(getattr(a.embedder, "available", lambda: False)())

        models_used_gb = (stt_vram if stt_loaded else 0.0) + tts_loaded_vram + (ditto_vram if ditto_running else 0.0)

        gpu = _gpu_usage()
        used_mib, total_mib = gpu if gpu else (None, None)
        used_gb = round(used_mib / 1024, 1) if used_mib is not None else None
        total_gb = round(total_mib / 1024, 1) if total_mib is not None else None
        system_gb = round(max(0.0, (used_mib / 1024) - models_used_gb), 1) if used_mib is not None else None

        return {
            "stt": {
                "enabled": bool(st.enabled),
                "loaded": stt_loaded,
                "model": st.model_name,
                "available": stt_available,
                "repo": f"https://huggingface.co/{st._repo(st.model_name)}",
                "vram_est_gb": round(stt_vram, 1),
            },
            "tts": tts_items,
            "ditto": {
                "enabled": bool(ditto_cfg.get("enabled", False)),
                "running": ditto_running,
                "vram_est_gb": round(ditto_vram, 1),
            },
            "embedding": {
                "model": emb_model,
                "device": emb_device,
                "loaded": emb_loaded,
                "installed": emb_installed,
                "repo": f"https://huggingface.co/{emb_model}",
                "vram_est_gb": round(_dir_size_gb(_hf_cache_dir(emb_model)) or 0.0, 1),
            },
            "gpu": {
                "used_gb": used_gb,
                "total_gb": total_gb,
                "models_used_gb": round(models_used_gb, 1),
                "system_used_gb": system_gb,
            },
        }

    @app.get("/api/logs")
    def get_logs():
        return {"log": read_log(500)}

    @app.post("/api/logs/clear")
    async def clear_logs():
        return {"ok": clear_log()}

    @app.get("/api/logs/settings")
    def logs_settings():
        a = app.state.app
        return {"retention_hours": float((a.config.get("logs", {}) or {}).get("retention_hours", 48) or 0)}

    @app.post("/api/logs/settings")
    async def save_logs_settings(retention_hours: str = Form("")):
        a = app.state.app
        try:
            hours = float(retention_hours) if retention_hours.strip() else 48.0
        except ValueError:
            raise HTTPException(400, "invalid number")
        a.config.setdefault("logs", {})["retention_hours"] = hours
        _save_config(a.config)
        return {"ok": True, "retention_hours": hours}

    @app.post("/api/embedding/download")
    async def download_embedding():
        a = app.state.app
        emb_cfg = a.config.get("memory", {}).get("embedding", {}) or {}
        model = emb_cfg.get("model_name", "Qwen/Qwen3-Embedding-0.6B")
        hf_token = (a.config.get("huggingface", {}) or {}).get("token", "")

        def _dl():
            from huggingface_hub import snapshot_download
            snapshot_download(model, token=hf_token or None)
            log_event("info", f"embedding model '{model}' downloaded")

        threading.Thread(target=_dl, daemon=True).start()
        return {"ok": True, "model": model}

    return app


def main():
    parser = argparse.ArgumentParser(description="voice-ai")
    parser.add_argument("--port", type=int, default=None)
    args = parser.parse_args()

    cfg_path = os.path.join(ROOT, "config.yaml")
    with open(cfg_path) as f:
        config = yaml.safe_load(f) or {}
    server_cfg = config.get("server", {})
    host = server_cfg.get("host", "0.0.0.0")
    port = args.port or int(server_cfg.get("port", 8900))
    use_https = bool(server_cfg.get("https", True))

    app = create_app(config)

    import uvicorn
    cert = os.path.join(ROOT, "cert.pem")
    key = os.path.join(ROOT, "key.pem")
    if use_https and os.path.isfile(cert) and os.path.isfile(key):
        print(f"voice-ai starting on https://{host}:{port}")
        uvicorn.run(app, host=host, port=port, ssl_certfile=cert, ssl_keyfile=key, log_level="info")
    else:
        print(f"voice-ai starting on http://{host}:{port}")
        uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
