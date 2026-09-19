"""LuxTTS server - standalone subprocess that loads LuxTTS once and serves
voice cloning over HTTP. Runs in the lux venv (Python 3.10 + torch 2.5.1+cu121).
The TTS server's LuxBackend proxies to this.

Endpoints:
  GET  /health    -> {"ok": true, "loaded": true, "model": "lux"}
  POST /generate  -> JSON {text, ref_audio, num_steps}
                     returns WAV bytes (24 kHz, resampled from native 48 kHz)
"""
import io
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import librosa
import soundfile as sf
from zipvoice.luxvoice import LuxTTS
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get("LUX_PORT", "8140"))
NUM_STEPS = int(os.environ.get("LUX_NUM_STEPS", "8"))
GUIDANCE_SCALE = float(os.environ.get("LUX_GUIDANCE_SCALE", "3.0"))
T_SHIFT = float(os.environ.get("LUX_T_SHIFT", "0.9"))
SPEED = float(os.environ.get("LUX_SPEED", "1.0"))
RETURN_SMOOTH = os.environ.get("LUX_RETURN_SMOOTH", "0") == "1"
RMS = float(os.environ.get("LUX_RMS", "0.01"))
DURATION = int(os.environ.get("LUX_DURATION", "5"))
SRC_SR = 48000
OUTPUT_SR = 24000

print("[lux-server] loading LuxTTS (cuda)...", flush=True)
lux = LuxTTS("YatharthS/LuxTTS", device="cuda")
print("[lux-server] model loaded", flush=True)

_ref_cache = {}


def get_encoded(ref_audio, rms, duration):
    if not ref_audio or not os.path.isfile(ref_audio):
        return None
    key = (ref_audio, rms, duration)
    if key not in _ref_cache:
        _ref_cache[key] = lux.encode_prompt(ref_audio, duration=duration, rms=rms)
    return _ref_cache[key]


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
            self._send_json({"ok": True, "loaded": True, "model": "lux"})
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
        num_steps = int(req.get("num_steps", NUM_STEPS))
        guidance_scale = float(req.get("guidance_scale", GUIDANCE_SCALE))
        t_shift = float(req.get("t_shift", T_SHIFT))
        speed = float(req.get("speed", SPEED))
        return_smooth = bool(req.get("return_smooth", RETURN_SMOOTH))
        rms = float(req.get("rms", RMS))
        duration = int(req.get("duration", DURATION))

        if not text:
            self._send_json({"error": "text required"}, 400)
            return
        enc = get_encoded(ref_audio, rms, duration)
        if enc is None:
            self._send_json({"error": f"ref_audio not found: {ref_audio}"}, 400)
            return

        t0 = time.time()
        wav = lux.generate_speech(text, enc, num_steps=num_steps, guidance_scale=guidance_scale,
                                  t_shift=t_shift, speed=speed, return_smooth=return_smooth)
        gen_time = time.time() - t0

        audio = wav.numpy().squeeze()
        if OUTPUT_SR != SRC_SR:
            audio = librosa.resample(audio, orig_sr=SRC_SR, target_sr=OUTPUT_SR)

        buf = io.BytesIO()
        sf.write(buf, audio, OUTPUT_SR, format="WAV")
        wav_bytes = buf.getvalue()

        self.send_response(200)
        self.send_header("Content-Type", "audio/wav")
        self.send_header("Content-Length", str(len(wav_bytes)))
        self.send_header("X-Gen-Time", f"{gen_time:.2f}")
        self.end_headers()
        self.wfile.write(wav_bytes)


if __name__ == "__main__":
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"[lux-server] listening on 127.0.0.1:{PORT}", flush=True)
    srv.serve_forever()
