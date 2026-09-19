"""App post-install helper.

Runs in the app venv after pip install. Two jobs:
1. Write a random api_token and the HF token into config.yaml.
2. Pre-download the 3 internal models so the first message doesn't stall.

Usage: python app_install.py [hf_token]
"""
import os
import sys
import secrets

import yaml

ROOT = os.path.dirname(os.path.abspath(__file__))
cfg_path = os.path.join(ROOT, "config.yaml")
hf_token = (sys.argv[1] if len(sys.argv) > 1 else "").strip()

# 1. write tokens into config.yaml
if os.path.isfile(cfg_path):
    cfg = yaml.safe_load(open(cfg_path, encoding="utf-8")) or {}
    cfg.setdefault("auth", {})["api_token"] = secrets.token_hex(32)
    if hf_token:
        cfg.setdefault("huggingface", {})["token"] = hf_token
    yaml.safe_dump(cfg, open(cfg_path, "w", encoding="utf-8"))
    print("[install] config.yaml: api_token generated" + (" + HF token set" if hf_token else ""), flush=True)
else:
    print("[install] WARNING: config.yaml not found, skipping token write", flush=True)

# 2. pre-download internal models
print("[install] downloading Whisper large-v3-turbo ...", flush=True)
from faster_whisper import WhisperModel
WhisperModel("large-v3-turbo", device="cpu", compute_type="int8")
print("[install] whisper ok", flush=True)

print("[install] downloading Kokoro ...", flush=True)
from kokoro import KPipeline
KPipeline(lang_code="a")
print("[install] kokoro ok", flush=True)

print("[install] downloading Qwen3-Embedding-0.6B ...", flush=True)
from sentence_transformers import SentenceTransformer
SentenceTransformer("Qwen/Qwen3-Embedding-0.6B", device="cpu")
print("[install] embeddings ok", flush=True)

print("[install] app post-install complete", flush=True)
