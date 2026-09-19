"""LuxTTS backend - fast zero-shot voice cloning (English/Chinese) via subprocess.

LuxTTS runs in a separate venv (Python 3.10 + torch 2.5.1+cu121), so the TTS
server drives it through a small HTTP subprocess (lux_server.py). Clone-only:
needs ref_audio, no design/instruction mode. Mirrors the OmniVoice backend.
"""
from __future__ import annotations

import io
import os
import subprocess
import time
from typing import Optional

import requests
import soundfile as sf

from .base import ModelBackend, TTSResult


class LuxBackend(ModelBackend):
    def __init__(self, config: dict, root: str):
        super().__init__("lux", config, root)
        self._cfg = config
        self._port = int(self._cfg.get("port", 8140))
        self._python = (self._cfg.get("python") or "").strip()
        self._script = (self._cfg.get("server_script") or "").strip()
        self._proc: Optional[subprocess.Popen] = None
        self._log_file = None

    # -- availability ----------------------------------------------------

    def _available(self) -> bool:
        if not self._python or not os.path.isfile(self._python):
            return False
        if not self._script or not os.path.isfile(self._script):
            return False
        return True

    # -- lifecycle -------------------------------------------------------

    def _load(self) -> None:
        if not self._python or not self._script:
            raise RuntimeError("lux python or server_script not configured")
        env = dict(os.environ)
        env["LUX_PORT"] = str(self._port)
        if self._cfg.get("num_steps"):
            env["LUX_NUM_STEPS"] = str(self._cfg["num_steps"])
        if self._cfg.get("t_shift") is not None:
            env["LUX_T_SHIFT"] = str(self._cfg["t_shift"])
        if self._cfg.get("rms"):
            env["LUX_RMS"] = str(self._cfg["rms"])

        log_path = os.path.join(self.root, "output", f"lux_{int(time.time())}.log")
        self._log_file = open(log_path, "w")
        self._proc = subprocess.Popen(
            [self._python, self._script],
            cwd=os.path.dirname(self._script) or self.root,
            stdout=self._log_file,
            stderr=subprocess.STDOUT,
            env=env,
        )
        self._wait_ready(log_path)

    def _wait_ready(self, log_path: str) -> None:
        deadline = time.time() + 600
        url = f"http://127.0.0.1:{self._port}/health"
        while time.time() < deadline:
            if self._proc.poll() is not None:
                raise RuntimeError(
                    f"lux-server exited early (code {self._proc.returncode}). See log: {log_path}"
                )
            try:
                r = requests.get(url, timeout=2)
                if r.status_code < 500:
                    return
            except requests.RequestException:
                pass
            time.sleep(2)
        raise RuntimeError(f"lux-server did not come up within 600s. See log: {log_path}")

    def unload(self) -> None:
        if self._proc and self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self._proc.kill()
        self._proc = None
        self._loaded = False
        self._load_error = None
        self._kill_orphans(["lux_server.py"])
        if self._log_file:
            try:
                self._log_file.close()
            except Exception:
                pass
        self._log_file = None
        self._loaded = False
        self._load_error = None

    # -- capabilities ----------------------------------------------------

    @property
    def supports_cloning(self) -> bool:
        return True

    @property
    def supports_design(self) -> bool:
        return False

    def voices(self) -> list[dict]:
        return []

    def settings_schema(self) -> list[dict]:
        return [
            {"name": "num_steps", "type": "int", "default": self._cfg.get("num_steps", 8), "min": 1, "max": 32,
             "description": "Diffusion steps (higher = better quality, slower)"},
            {"name": "guidance_scale", "type": "float", "default": 3.0, "min": 1.0, "max": 10.0,
             "description": "Classifier-free guidance scale"},
            {"name": "t_shift", "type": "float", "default": self._cfg.get("t_shift", 0.9), "min": 0.0, "max": 2.0,
             "description": "Noise schedule shift (higher = better quality, more pronunciation risk)"},
            {"name": "speed", "type": "float", "default": 1.0, "min": 0.5, "max": 2.0,
             "description": "Speaking speed (higher = faster)"},
            {"name": "return_smooth", "type": "bool", "default": False,
             "description": "Smoother 24k output (softer but less clean)"},
            {"name": "rms", "type": "float", "default": self._cfg.get("rms", 0.01), "min": 0.0, "max": 0.1,
             "description": "Reference audio loudness normalization"},
            {"name": "duration", "type": "int", "default": 5, "min": 1, "max": 30,
             "description": "Seconds of reference audio used for the clone prompt"},
        ]

    def status(self) -> dict:
        s = super().status()
        s["engine"] = "luxtts/zipvoice"
        s["port"] = self._port
        s["num_steps"] = self._cfg.get("num_steps", 4)
        return s

    # -- generation ------------------------------------------------------

    def generate(
        self,
        text: str,
        voice: str = "",
        *,
        ref_audio: Optional[str] = None,
        ref_text: Optional[str] = None,
        instruction: Optional[str] = None,
        cfg_scale: Optional[float] = None,
        seed: Optional[int] = None,
        settings: Optional[dict] = None,
        output_format: str = "wav",
        sample_rate: int = 24000,
    ) -> TTSResult:
        if not text or not text.strip():
            raise ValueError("text required")
        if not ref_audio:
            raise ValueError("lux needs ref_audio (clone)")

        s = settings or {}
        num_steps = int(s.get("num_steps", self._cfg.get("num_steps", 8)))
        guidance_scale = float(s.get("guidance_scale", 3.0))
        t_shift = float(s.get("t_shift", self._cfg.get("t_shift", 0.9)))
        speed = float(s.get("speed", 1.0))
        return_smooth = bool(s.get("return_smooth", False))
        rms = float(s.get("rms", self._cfg.get("rms", 0.01)))
        duration = int(s.get("duration", 5))
        payload = {
            "text": text, "ref_audio": ref_audio,
            "num_steps": num_steps, "guidance_scale": guidance_scale, "t_shift": t_shift,
            "speed": speed, "return_smooth": return_smooth, "rms": rms, "duration": duration,
        }

        r = requests.post(
            f"http://127.0.0.1:{self._port}/generate",
            json=payload, timeout=600,
        )
        if r.status_code != 200:
            try:
                detail = r.json().get("error", r.text)
            except Exception:
                detail = r.text
            raise RuntimeError(f"lux: {detail}")
        wav = r.content
        if not wav:
            raise RuntimeError("lux returned no audio")

        audio, sr = sf.read(io.BytesIO(wav))

        out_path = self._out_path(output_format)
        if output_format == "ogg":
            sf.write(out_path, audio, sr, format="ogg", subtype="opus")
        else:
            sf.write(out_path, audio, sr)

        return TTSResult(
            audio_path=out_path,
            filename=os.path.basename(out_path),
            sample_rate=sr,
            duration_sec=round(len(audio) / sr, 2),
            model=self.name,
            voice=voice or "clone",
            extra={"engine": "luxtts", "num_steps": self._cfg.get("num_steps", 4)},
        )
