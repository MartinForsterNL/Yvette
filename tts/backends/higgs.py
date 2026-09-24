"""Higgs TTS 3 backend - zero-shot voice cloning via a C++ GGUF subprocess.

Higgs TTS 3 runs as a small C++ server (HiggsTTS.cpp, ggml/CUDA) that loads one
GGUF quant and clones a voice from a reference wav. The TTS server drives it
through a thin HTTP wrapper (higgs_server.py), which owns the child process.
Clone-only: needs ref_audio, no design/instruction mode. Mirrors the LuxTTS
backend.

Quant selects the GGUF file (q4_k default, q6_k, q8_0) and is a load-time
setting: the wrapper restarts its child when it changes.

Model weights: NeemaShioSe/HiggsTTS3.gguf, the GGUF quants of Boson AI's
bosonai/higgs-tts-3-4b (research / non-commercial license).
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


class HiggsBackend(ModelBackend):
    def __init__(self, config: dict, root: str):
        super().__init__("higgs", config, root)
        self._cfg = config
        self._port = int(self._cfg.get("port", 8141))
        self._python = (self._cfg.get("python") or "").strip()
        self._script = (self._cfg.get("server_script") or "").strip()
        self._bin = (self._cfg.get("bin") or "").strip()
        self._proc: Optional[subprocess.Popen] = None
        self._log_file = None

    # -- availability ----------------------------------------------------

    def _available(self) -> bool:
        if not self._python or not os.path.isfile(self._python):
            return False
        if not self._script or not os.path.isfile(self._script):
            return False
        return bool(self._bin and os.path.isfile(self._bin))

    # -- lifecycle -------------------------------------------------------

    def _load(self) -> None:
        if not self._python or not self._script:
            raise RuntimeError("higgs python or server_script not configured")
        env = dict(os.environ)
        env["HIGGS_PORT"] = str(self._port)
        if self._bin:
            env["HIGGS_BIN"] = self._bin
        if self._cfg.get("models_dir"):
            env["HIGGS_MODELS_DIR"] = str(self._cfg["models_dir"])
        if self._cfg.get("quant"):
            env["HIGGS_QUANT"] = str(self._cfg["quant"])
        if self._cfg.get("temperature") is not None:
            env["HIGGS_TEMPERATURE"] = str(self._cfg["temperature"])
        if self._cfg.get("seed") is not None:
            env["HIGGS_SEED"] = str(self._cfg["seed"])
        if self._cfg.get("extra_args"):
            env["HIGGS_EXTRA_ARGS"] = str(self._cfg["extra_args"])

        log_path = os.path.join(self.root, "output", f"higgs_{int(time.time())}.log")
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
                    f"higgs-server exited early (code {self._proc.returncode}). See log: {log_path}"
                )
            try:
                r = requests.get(url, timeout=2)
                if r.status_code < 500:
                    return
            except requests.RequestException:
                pass
            time.sleep(2)
        raise RuntimeError(f"higgs-server did not come up within 600s. See log: {log_path}")

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
        # Also sweep the wrapper and the C++ child, which outlive the wrapper on a
        # hard kill and would otherwise keep holding VRAM.
        self._kill_orphans(["higgs_server.py", "higgs_server.exe"])
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
            {"name": "quant", "type": "select", "default": self._cfg.get("quant", "q4_k"),
             "options": ["q4_k", "q6_k", "q8_0"],
             "description": "GGUF quantisation: q4_k is fastest and lightest (~3.5 GB VRAM), "
                            "q6_k balanced (~4.5 GB), q8_0 closest to the original (~5.7 GB). "
                            "Load-time setting"},
            {"name": "temperature", "type": "float", "default": self._cfg.get("temperature", 0.9),
             "min": 0.0, "max": 2.0,
             "description": "Sampling temperature (higher = more varied)"},
            {"name": "seed", "type": "int", "default": self._cfg.get("seed", 42),
             "description": "Random seed (fixed when the model loads)"},
        ]

    def status(self) -> dict:
        s = super().status()
        s["engine"] = "higgs/tts-3-4b-gguf"
        s["port"] = self._port
        s["quant"] = self._cfg.get("quant", "q4_k")
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
            raise ValueError("higgs needs ref_audio (clone)")

        s = settings or {}
        quant = str(s.get("quant", self._cfg.get("quant", "q4_k")))
        temperature = float(s.get("temperature", self._cfg.get("temperature", 0.9)))
        req_seed = seed if seed is not None else self._cfg.get("seed", 42)
        payload = {
            "text": text, "ref_audio": ref_audio, "ref_text": ref_text or "",
            "quant": quant, "temperature": temperature, "seed": req_seed,
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
            raise RuntimeError(f"higgs: {detail}")
        wav = r.content
        if not wav:
            raise RuntimeError("higgs returned no audio")

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
            extra={"engine": "higgs", "quant": quant},
        )
