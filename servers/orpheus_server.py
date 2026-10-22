"""Orpheus TTS 3 wrapper.

Owns the llama.cpp server that runs the Orpheus 3B GGUF and the SNAC decoder
(in-process). Exposes a tiny HTTP surface for the app's TTS backend:

    GET  /health   -> 200 when the model is loaded, 503 while loading
    POST /generate -> {"text": "...", "voice": "tara"} -> audio/wav

The upstream project is a FastAPI web app; we deliberately import its
tts_engine as a library instead of running that app, so there is no extra
web UI, no .env file, and no per-request file server in the middle.
"""
from __future__ import annotations

import atexit
import os
import subprocess
import sys
import tempfile
import threading
import time

import requests
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel

# The upstream modules print emoji and non-ASCII voice names. Force UTF-8
# before importing them (Windows consoles default to cp1252).
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

PORT = int(os.environ.get("ORPHEUS_PORT", "8142"))
LLAMA_PORT = int(os.environ.get("ORPHEUS_LLAMA_PORT", "8143"))
LLAMA_BIN = os.environ.get("ORPHEUS_LLAMA_BIN", os.path.join(ROOT, "llama", "llama-server.exe"))
MODEL_PATH = os.environ.get("ORPHEUS_MODEL_PATH", os.path.join(ROOT, "models", "Orpheus-3b-FT-Q4_K_M.gguf"))
MAX_TOKENS = os.environ.get("ORPHEUS_MAX_TOKENS", "8192")
TEMPERATURE = os.environ.get("ORPHEUS_TEMPERATURE", "0.6")
TOP_P = os.environ.get("ORPHEUS_TOP_P", "0.9")
SAMPLE_RATE = os.environ.get("ORPHEUS_SAMPLE_RATE", "24000")
THREADS = os.environ.get("ORPHEUS_THREADS", "8")

# The upstream inference module reads these at import time.
os.environ.setdefault("ORPHEUS_API_URL", "http://127.0.0.1:%d/v1/completions" % LLAMA_PORT)
os.environ.setdefault("ORPHEUS_API_TIMEOUT", "600")
os.environ.setdefault("ORPHEUS_MAX_TOKENS", MAX_TOKENS)
os.environ.setdefault("ORPHEUS_TEMPERATURE", TEMPERATURE)
os.environ.setdefault("ORPHEUS_TOP_P", TOP_P)
os.environ.setdefault("ORPHEUS_SAMPLE_RATE", SAMPLE_RATE)
os.environ.setdefault("ORPHEUS_MODEL_NAME", "orpheus")

_state = {
    "llama": None,
    "ready": False,
    "error": None,
    "generate": None,
    "voices": [],
    "sample_rate": int(SAMPLE_RATE),
}
_lock = threading.Lock()
_gen_lock = threading.Lock()

app = FastAPI(title="Yvette Orpheus TTS", version="1.0")


class GenerateRequest(BaseModel):
    text: str
    voice: str = "tara"


def _wait_health(url: str, timeout: int) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            r = requests.get(url, timeout=3)
            if r.status_code == 200:
                return
        except requests.RequestException:
            pass
        time.sleep(1)
    raise RuntimeError("llama-server did not become healthy in %ds" % timeout)


def _start_llama():
    if not os.path.isfile(LLAMA_BIN):
        raise RuntimeError("llama-server not found: %s" % LLAMA_BIN)
    if not os.path.isfile(MODEL_PATH):
        raise RuntimeError("Orpheus model not found: %s" % MODEL_PATH)
    log_path = os.path.join(ROOT, "orpheus_llama.log")
    log = open(log_path, "a", encoding="utf-8", errors="replace")
    cmd = [
        LLAMA_BIN,
        "-m", MODEL_PATH,
        "-c", str(MAX_TOKENS),
        "-n", str(MAX_TOKENS),
        "--rope-scaling", "linear",
        "--host", "127.0.0.1",
        "--port", str(LLAMA_PORT),
        "-ngl", "999",
        "--threads", str(THREADS),
    ]
    log.write("\n=== starting llama-server: %s ===\n" % " ".join(cmd))
    log.flush()
    proc = subprocess.Popen(cmd, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT)
    _wait_health("http://127.0.0.1:%d/health" % LLAMA_PORT, 300)
    return proc


def _stop_llama() -> None:
    proc = _state.get("llama")
    if proc is not None and proc.poll() is None:
        try:
            proc.terminate()
            proc.wait(timeout=10)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
    _state["llama"] = None


def _init() -> None:
    try:
        _state["llama"] = _start_llama()
        from tts_engine import AVAILABLE_VOICES, generate_speech_from_api
        _state["generate"] = generate_speech_from_api
        _state["voices"] = list(AVAILABLE_VOICES)
        _state["ready"] = True
    except Exception as e:  # noqa: BLE001 - report through /health
        _state["error"] = "%s: %s" % (type(e).__name__, e)


@app.on_event("startup")
def startup() -> None:
    threading.Thread(target=_init, daemon=True).start()


@app.on_event("shutdown")
def shutdown() -> None:
    _stop_llama()


atexit.register(_stop_llama)


@app.get("/health")
def health():
    if _state["error"]:
        return JSONResponse(status_code=500, content={"ok": False, "error": _state["error"]})
    if not _state["ready"]:
        return JSONResponse(status_code=503, content={"ok": False, "loading": True})
    return {"ok": True, "voices": len(_state["voices"]), "sample_rate": _state["sample_rate"]}


@app.post("/generate")
def generate(req: GenerateRequest):
    if not _state["ready"]:
        raise HTTPException(503, _state["error"] or "model is still loading")
    if not req.text or not req.text.strip():
        raise HTTPException(400, "text required")
    voice = (req.voice or "tara").strip()
    out_dir = os.path.join(ROOT, "outputs")
    os.makedirs(out_dir, exist_ok=True)
    fd, out_path = tempfile.mkstemp(prefix="orpheus_", suffix=".wav", dir=out_dir)
    os.close(fd)
    try:
        with _gen_lock:
            _state["generate"](
                prompt=req.text,
                voice=voice,
                output_file=out_path,
                use_batching=False,
                max_batch_chars=1000,
            )
        with open(out_path, "rb") as f:
            data = f.read()
        if not data:
            raise RuntimeError("Orpheus returned no audio")
        return Response(content=data, media_type="audio/wav")
    finally:
        try:
            os.remove(out_path)
        except OSError:
            pass


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="info")
