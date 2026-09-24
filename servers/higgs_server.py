"""Higgs TTS 3 server - HTTP wrapper around the HiggsTTS.cpp binary (GGUF).

Higgs TTS 3 is served by a small C++ server (higgs_server.exe) that loads one
GGUF quant and clones a voice from a reference wav. That binary speaks a framed
TCP protocol, is clone-only, and takes the reference at startup, so this wrapper
keeps the app-facing subprocess contract (GET /health, POST /generate -> WAV
bytes) and manages the child for us:

  - one child per (quant, reference, seed); it is restarted when any of those
    changes. The restart re-loads the GGUF and re-encodes the reference prefill,
    which costs a few seconds - only the first request after a change pays it.
  - one request at a time, because the binary handles a single request per
    socket.
  - temperature is passed per request, so changing it restarts nothing.

Child protocol (one-shot, from the HiggsTTS.cpp README):
  request:  [4B text_len BE][4B temperature BE float][UTF-8 text]
  response: [4B n_samples BE][float32 PCM @ 24kHz mono]
  error:    [4B int32 -1 BE]

Model weights: NeemaShioSe/HiggsTTS3.gguf - the q4_k / q6_k / q8_0 GGUF quants of
Boson AI's bosonai/higgs-tts-3-4b. Boson AI's Higgs Audio v3 license is research /
non-commercial only.

Endpoints:
  GET  /health    -> {"ok": true, "loaded": true, "model": "higgs"}
  POST /generate  -> JSON {text, ref_audio, ref_text, quant, temperature, seed}
                     returns WAV bytes (24 kHz mono)

Env:
  HIGGS_PORT         HTTP port for this wrapper (default 8141)
  HIGGS_BIN          path to higgs_server.exe
  HIGGS_MODELS_DIR   folder holding the GGUF files
  HIGGS_QUANT        default quant: q4_k | q6_k | q8_0 (default q4_k)
  HIGGS_TEMPERATURE  default sampling temperature (the binary's default is 0.9)
  HIGGS_SEED         default random seed (the binary's default is 42)
  HIGGS_EXTRA_ARGS   extra args appended to the child command line
  HIGGS_CHILD_PORT   TCP port for the child (default: HIGGS_PORT + 1)
"""
import array
import io
import json
import os
import socket
import struct
import subprocess
import sys
import threading
import time
import urllib.request
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get("HIGGS_PORT", "8141"))
CHILD_PORT = int(os.environ.get("HIGGS_CHILD_PORT", str(PORT + 1)))
BIN = (os.environ.get("HIGGS_BIN") or "").strip()
MODELS_DIR = (os.environ.get("HIGGS_MODELS_DIR") or "").strip()
DEFAULT_QUANT = (os.environ.get("HIGGS_QUANT") or "q4_k").strip()
DEFAULT_TEMPERATURE = float(os.environ.get("HIGGS_TEMPERATURE", "0.9"))
DEFAULT_SEED = int(os.environ.get("HIGGS_SEED", "42"))
OUTPUT_SR = 24000

# quant -> GGUF filename. install.ps1 downloads only q4_k; the others are fetched
# here on demand the first time admin picks them.
QUANT_FILES = {
    "q4_k": "higgs-v3-tts-q4_k.gguf",
    "q6_k": "higgs-v3-tts-q6_k.gguf",
    "q8_0": "higgs-v3-tts-q8_0.gguf",
}
MODEL_BASE_URL = os.environ.get(
    "HIGGS_MODEL_URL", "https://huggingface.co/NeemaShioSe/HiggsTTS3.gguf/resolve/main"
).rstrip("/")
ALLOW_DOWNLOAD = os.environ.get("HIGGS_ALLOW_DOWNLOAD", "1") != "0"


def _split_args(s):
    """Split HIGGS_EXTRA_ARGS on spaces, honouring single/double quotes."""
    out, cur, quote = [], "", ""
    for ch in s or "":
        if quote:
            if ch == quote:
                quote = ""
            else:
                cur += ch
        elif ch in "\"'":
            quote = ch
        elif ch.isspace():
            if cur:
                out.append(cur)
                cur = ""
        else:
            cur += ch
    if cur:
        out.append(cur)
    return out


EXTRA_ARGS = _split_args(os.environ.get("HIGGS_EXTRA_ARGS", ""))

CHILD_LOCK = threading.Lock()


def _recv_exact(sock, n):
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(min(65536, n - len(buf)))
        if not chunk:
            raise RuntimeError(f"connection closed after {len(buf)} of {n} bytes")
        buf += chunk
    return bytes(buf)


def _model_path(quant):
    name = QUANT_FILES.get(quant)
    if not name:
        raise RuntimeError(f"unknown quant '{quant}' (expected one of {', '.join(QUANT_FILES)})")
    if not MODELS_DIR:
        raise RuntimeError("HIGGS_MODELS_DIR not configured")
    return os.path.join(MODELS_DIR, name)


def _ensure_model(quant):
    """Return the GGUF path for a quant, downloading it on first use if needed."""
    path = _model_path(quant)
    if os.path.isfile(path):
        return path
    if not ALLOW_DOWNLOAD:
        raise RuntimeError(f"model not found: {path} (auto-download disabled)")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    url = f"{MODEL_BASE_URL}/{os.path.basename(path)}"
    print(f"[higgs-server] downloading {url} -> {path}", flush=True)
    tmp = path + ".part"
    try:
        with urllib.request.urlopen(url, timeout=120) as resp, open(tmp, "wb") as f:
            while True:
                chunk = resp.read(1 << 20)
                if not chunk:
                    break
                f.write(chunk)
        os.replace(tmp, path)
    except Exception:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise
    print(f"[higgs-server] downloaded {os.path.basename(path)}", flush=True)
    return path


def _pcm_to_wav(pcm_bytes):
    """float32 mono PCM @ 24 kHz -> 16-bit WAV bytes."""
    samples = array.array("f")
    samples.frombytes(pcm_bytes)
    if sys.byteorder == "big":
        samples.byteswap()
    pcm16 = array.array("h", (int(max(-1.0, min(1.0, x)) * 32767) for x in samples))
    buf = io.BytesIO()
    w = wave.open(buf, "wb")
    w.setnchannels(1)
    w.setsampwidth(2)
    w.setframerate(OUTPUT_SR)
    w.writeframes(pcm16.tobytes())
    w.close()
    return buf.getvalue()


class ChildServer:
    """The higgs_server.exe child and the framed protocol in front of it."""

    def __init__(self):
        self._proc = None
        self._key = None

    def _wait_ready(self, timeout=180.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self._proc.poll() is not None:
                raise RuntimeError(
                    f"higgs_server.exe exited during load (code {self._proc.returncode})"
                )
            try:
                with socket.create_connection(("127.0.0.1", CHILD_PORT), timeout=1):
                    return
            except OSError:
                pass
            time.sleep(0.5)
        raise RuntimeError(
            f"higgs_server.exe did not start listening on {CHILD_PORT} within {timeout:.0f}s"
        )

    def start(self, quant, ref_audio, ref_text, seed):
        if not BIN or not os.path.isfile(BIN):
            raise RuntimeError(f"higgs_server.exe not found: {BIN or '(HIGGS_BIN unset)'}")
        model = _ensure_model(quant)
        cmd = [
            BIN, "--model", model, "--ref-wav", ref_audio,
            "--port", str(CHILD_PORT),
            "--temperature", str(DEFAULT_TEMPERATURE), "--seed", str(seed),
        ]
        if ref_text:
            cmd += ["--ref-text", ref_text]
        cmd += EXTRA_ARGS
        print(f"[higgs-server] starting child: quant={quant} ref={ref_audio} seed={seed}", flush=True)
        self._proc = subprocess.Popen(cmd, cwd=os.path.dirname(BIN) or None)
        self._wait_ready()

    def stop(self):
        proc = self._proc
        self._proc = None
        self._key = None
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass

    def ensure(self, quant, ref_audio, ref_text, seed):
        """Start the child, or restart it when quant / reference / seed changed."""
        ref = os.path.abspath(ref_audio)
        if not os.path.isfile(ref):
            raise RuntimeError(f"reference audio not found: {ref_audio}")
        try:
            mtime = os.path.getmtime(ref)
        except OSError:
            mtime = 0.0
        key = (quant, ref, mtime, ref_text or "", seed)
        if self._proc is not None and self._proc.poll() is None and self._key == key:
            return
        self.stop()
        self.start(quant, ref, ref_text or "", seed)
        self._key = key

    def synthesize(self, text, temperature):
        payload = text.encode("utf-8")
        req = struct.pack(">I", len(payload)) + struct.pack(">f", float(temperature)) + payload
        with socket.create_connection(("127.0.0.1", CHILD_PORT), timeout=30) as sock:
            sock.settimeout(600)
            sock.sendall(req)
            (n,) = struct.unpack(">i", _recv_exact(sock, 4))
            if n == -1:
                raise RuntimeError("higgs_server.exe reported a synthesis error")
            if n <= 0:
                raise RuntimeError(f"higgs_server.exe returned {n} samples")
            return _recv_exact(sock, n * 4)


CHILD = ChildServer()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

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
            # "loaded" here means the wrapper is up; the GGUF child starts lazily
            # on the first /generate and is reported through the logs.
            self._send_json({"ok": True, "loaded": True, "model": "higgs"})
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
        quant = (req.get("quant") or DEFAULT_QUANT).strip()
        temperature = float(req.get("temperature", DEFAULT_TEMPERATURE))
        seed = int(req.get("seed", DEFAULT_SEED))

        if not text:
            self._send_json({"error": "text required"}, 400)
            return
        if not ref_audio:
            # The C++ server hard-requires --ref-wav, so higgs is clone-only.
            self._send_json({"error": "ref_audio required (higgs is clone-only)"}, 400)
            return

        t0 = time.time()
        try:
            with CHILD_LOCK:
                CHILD.ensure(quant, ref_audio, ref_text, seed)
                pcm = CHILD.synthesize(text, temperature)
        except Exception as e:
            self._send_json({"error": str(e)}, 400)
            return
        gen_time = time.time() - t0

        wav_bytes = _pcm_to_wav(pcm)
        print(
            f"[higgs-server] generated {len(pcm) / 4 / OUTPUT_SR:.2f}s audio in {gen_time:.2f}s "
            f"(quant={quant})",
            flush=True,
        )

        self.send_response(200)
        self.send_header("Content-Type", "audio/wav")
        self.send_header("Content-Length", str(len(wav_bytes)))
        self.send_header("X-Gen-Time", f"{gen_time:.2f}")
        self.end_headers()
        self.wfile.write(wav_bytes)


if __name__ == "__main__":
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"[higgs-server] listening on 127.0.0.1:{PORT} (child port {CHILD_PORT})", flush=True)
    try:
        srv.serve_forever()
    finally:
        CHILD.stop()
