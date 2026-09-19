"""OmniVoice backend - multilingual zero-shot TTS (600+ languages) via subprocess.

OmniVoice runs in a separate Python venv (Python 3.10 + torch 2.8.0+cu128),
so the TTS server drives it through a small HTTP subprocess
(omnivoice_server.py). Supports voice cloning (ref audio) and voice design
(instruct). Mirrors the Parkiet backend pattern.
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


class OmniVoiceBackend(ModelBackend):
    def __init__(self, config: dict, root: str):
        super().__init__("omnivoice", config, root)
        self._cfg = config
        self._port = int(self._cfg.get("port", 8139))
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
            raise RuntimeError("omnivoice python or server_script not configured")
        env = dict(os.environ)
        env["OMNIVOICE_PORT"] = str(self._port)
        if self._cfg.get("model_id"):
            env["OMNIVOICE_MODEL"] = self._cfg["model_id"]
        if self._cfg.get("dtype"):
            env["OMNIVOICE_DTYPE"] = self._cfg["dtype"]
        if self._cfg.get("num_step"):
            env["OMNIVOICE_NUM_STEP"] = str(self._cfg["num_step"])

        log_path = os.path.join(self.root, "output", f"omnivoice_{int(time.time())}.log")
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
        deadline = time.time() + 600  # first load can be slow
        url = f"http://127.0.0.1:{self._port}/health"
        while time.time() < deadline:
            if self._proc.poll() is not None:
                raise RuntimeError(
                    f"omnivoice-server exited early (code {self._proc.returncode}). See log: {log_path}"
                )
            try:
                r = requests.get(url, timeout=2)
                if r.status_code < 500:
                    return
            except requests.RequestException:
                pass
            time.sleep(2)
        raise RuntimeError(f"omnivoice-server did not come up within 600s. See log: {log_path}")

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
        self._kill_orphans(["omnivoice_server.py"])
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
        return True

    def voices(self) -> list[dict]:
        return []

    def settings_schema(self) -> list[dict]:
        return [
            {"name": "num_step", "type": "int", "default": self._cfg.get("num_step", 64), "min": 16, "max": 128,
             "description": "Diffusion steps (higher = better quality, slower)"},
            {"name": "guidance_scale", "type": "float", "default": self._cfg.get("default_guidance_scale", 4.0), "min": 1.0, "max": 10.0,
             "description": "Guidance scale (voice design only)"},
            {"name": "t_shift", "type": "float", "default": 0.1, "min": 0.0, "max": 2.0,
             "description": "Time-step shift for the noise schedule"},
            {"name": "class_temperature", "type": "float", "default": self._cfg.get("class_temperature", 1.0), "min": 0.0, "max": 2.0,
             "description": "Token sampling temperature (higher = more varied)"},
            {"name": "position_temperature", "type": "float", "default": 5.0, "min": 0.0, "max": 10.0,
             "description": "Mask-position selection temperature"},
            {"name": "layer_penalty_factor", "type": "float", "default": 5.0, "min": 0.0, "max": 10.0,
             "description": "Penalty on deeper codebook layers"},
            {"name": "denoise", "type": "bool", "default": True,
             "description": "Prepend denoise token for cleaner speech"},
            {"name": "speed", "type": "float", "default": 1.0, "min": 0.5, "max": 2.0,
             "description": "Speaking speed (higher = faster)"},
            {"name": "normalize_text", "type": "bool", "default": False,
             "description": "Normalize numbers to words"},
        ]

    def status(self) -> dict:
        s = super().status()
        s["engine"] = "omnivoice/diffusion-lm"
        s["port"] = self._port
        s["dtype"] = self._cfg.get("dtype", "float16")
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

        s = settings or {}
        num_step = int(s.get("num_step", self._cfg.get("num_step", 64)))
        class_temp = float(s.get("class_temperature", self._cfg.get("class_temperature", 1.0)))
        t_shift = float(s.get("t_shift", 0.1))
        position_temp = float(s.get("position_temperature", 5.0))
        layer_penalty = float(s.get("layer_penalty_factor", 5.0))
        denoise = bool(s.get("denoise", True))
        speed = float(s.get("speed", 1.0))
        normalize_text = bool(s.get("normalize_text", False))

        payload = {"text": text}
        if ref_audio:
            payload["ref_audio"] = ref_audio
            payload["ref_text"] = ref_text or ""
        if instruction:
            payload["instruct"] = instruction

        guidance = s.get("guidance_scale", cfg_scale)
        if guidance is None and instruction and self._cfg.get("default_guidance_scale"):
            guidance = float(self._cfg["default_guidance_scale"])
        if guidance is not None:
            payload["guidance_scale"] = float(guidance)
        if seed is not None:
            payload["seed"] = seed
        payload["num_step"] = num_step
        payload["class_temperature"] = class_temp
        payload["t_shift"] = t_shift
        payload["position_temperature"] = position_temp
        payload["layer_penalty_factor"] = layer_penalty
        payload["denoise"] = denoise
        payload["speed"] = speed
        payload["normalize_text"] = normalize_text

        r = requests.post(
            f"http://127.0.0.1:{self._port}/generate",
            json=payload, timeout=600,
        )
        if r.status_code != 200:
            try:
                detail = r.json().get("error", r.text)
            except Exception:
                detail = r.text
            raise RuntimeError(f"omnivoice: {detail}")
        wav = r.content
        if not wav:
            raise RuntimeError("omnivoice returned no audio")

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
            voice=voice or ("clone" if ref_audio else ("design" if instruction else "auto")),
            extra={"engine": "omnivoice", "seed": seed},
        )
