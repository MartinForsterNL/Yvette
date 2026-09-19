"""OmniVoice server - standalone subprocess that loads the OmniVoice model once
and serves TTS generation over HTTP. Runs in the omnivoice venv
(Python 3.10 + torch 2.8.0+cu128). The TTS server's OmniVoiceBackend proxies.

Endpoints:
  GET  /health    -> {"ok": true, "loaded": true, "model": "omnivoice"}
  POST /generate  -> JSON {text, ref_audio, ref_text, instruct, num_step,
                           guidance_scale, seed}
                     returns WAV bytes (24 kHz)
"""
import io
import json
import os
import tempfile
import time

import numpy as np
import soundfile as sf
import torch
from omnivoice import OmniVoice
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get("OMNIVOICE_PORT", "8139"))
MODEL_ID = os.environ.get("OMNIVOICE_MODEL", "k2-fsa/OmniVoice")
DEVICE = os.environ.get("OMNIVOICE_DEVICE", "cuda:0")
DTYPE = os.environ.get("OMNIVOICE_DTYPE", "float16")
REF_TRIM_SEC = float(os.environ.get("OMNIVOICE_REF_TRIM", "9"))
NUM_STEP = int(os.environ.get("OMNIVOICE_NUM_STEP", "32"))
OUTPUT_SR = 24000

print(f"[omnivoice-server] loading {MODEL_ID} ({DTYPE})...", flush=True)
_dtype = torch.float16 if DTYPE == "float16" else torch.float32
model = OmniVoice.from_pretrained(MODEL_ID, device_map=DEVICE, dtype=_dtype)
print("[omnivoice-server] model loaded", flush=True)

_ref_cache = {}


def trimmed_ref_path(path):
    """Return a path to a 9s-trimmed, mono copy of the reference audio (cached)."""
    if not path or not os.path.isfile(path):
        return None
    if path not in _ref_cache:
        audio, sr = sf.read(path)
        if audio.ndim > 1:
            audio = audio.mean(axis=1)
        n = min(len(audio), int(REF_TRIM_SEC * sr))
        fd, tmp = tempfile.mkstemp(suffix=".wav")
        os.close(fd)
        sf.write(tmp, audio[:n], sr)
        _ref_cache[path] = tmp
    return _ref_cache[path]


def short_transcript(ref_text, n=2):
    t = (ref_text or "").strip()
    if not t:
        return ""
    parts = [p.strip() for p in t.split(".") if p.strip()]
    if not parts:
        return t
    return ". ".join(parts[:n]).strip() + "."


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send_json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/health":
            self._send_json({"ok": True, "loaded": True, "model": "omnivoice"})
        else:
            self._send_json({"error": "not found"}, 404)

    def do_POST(self):
        if self.path != "/generate":
            self._send_json({"error": "not found"}, 404)
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
            req = json.loads(self.rfile.read(length))
        except Exception as e:
            self._send_json({"error": f"bad request: {e}"}, 400)
            return

        text = (req.get("text") or "").strip()
        ref_audio = req.get("ref_audio") or ""
        ref_text = req.get("ref_text") or ""
        instruct = (req.get("instruct") or "").strip()
        num_step = int(req.get("num_step", NUM_STEP))
        guidance_scale = req.get("guidance_scale")
        class_temperature = req.get("class_temperature")
        t_shift = req.get("t_shift")
        position_temperature = req.get("position_temperature")
        layer_penalty_factor = req.get("layer_penalty_factor")
        denoise = req.get("denoise")
        speed = req.get("speed")
        normalize_text = req.get("normalize_text")
        seed = req.get("seed")

        if not text:
            self._send_json({"error": "text required"}, 400)
            return

        if seed is not None:
            torch.manual_seed(int(seed))
            np.random.seed(int(seed))

        gen = {"text": text, "normalize_text": bool(normalize_text) if normalize_text is not None else False}
        if speed is not None:
            gen["speed"] = float(speed)
        kwargs = {"num_step": num_step}
        if guidance_scale is not None:
            kwargs["guidance_scale"] = float(guidance_scale)
        if class_temperature is not None:
            kwargs["class_temperature"] = float(class_temperature)
        if t_shift is not None:
            kwargs["t_shift"] = float(t_shift)
        if position_temperature is not None:
            kwargs["position_temperature"] = float(position_temperature)
        if layer_penalty_factor is not None:
            kwargs["layer_penalty_factor"] = float(layer_penalty_factor)
        if denoise is not None:
            kwargs["denoise"] = bool(denoise)

        if ref_audio:
            ref_path = trimmed_ref_path(ref_audio)
            if ref_path is None:
                self._send_json({"error": f"ref_audio not found: {ref_audio}"}, 400)
                return
            gen["ref_audio"] = ref_path
            gen["ref_text"] = short_transcript(ref_text)
        elif instruct:
            gen["instruct"] = instruct
        # else: auto voice (no ref_audio, no instruct) - model picks a voice

        t0 = time.time()
        try:
            audio = model.generate(**gen, **kwargs)
        except Exception as e:
            msg = str(e)
            if "Unsupported instruct items" in msg:
                msg = ("invalid voice design instruction - OmniVoice expects comma-separated "
                       "attributes (e.g. 'female, low pitch'), not free text.")
            self._send_json({"error": msg}, 400)
            return
        gen_time = time.time() - t0

        buf = io.BytesIO()
        sf.write(buf, audio[0], OUTPUT_SR, format="WAV")
        wav_bytes = buf.getvalue()

        self.send_response(200)
        self.send_header("Content-Type", "audio/wav")
        self.send_header("Content-Length", str(len(wav_bytes)))
        self.send_header("X-Gen-Time", f"{gen_time:.2f}")
        self.end_headers()
        self.wfile.write(wav_bytes)


if __name__ == "__main__":
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"[omnivoice-server] listening on 127.0.0.1:{PORT}", flush=True)
    srv.serve_forever()
