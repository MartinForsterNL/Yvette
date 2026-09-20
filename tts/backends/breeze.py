"""Breeze TTS 2 backend - Python/PyTorch/CUDA (subprocess).

Runs `python -m breeze_infer.api` from the breezeblue-ai/breeze-tts repo (its own
venv with torch 2.9.1+cu128) and proxies TTS requests to its /v1/audio/speech
endpoint. This replaces the old C++/GGUF/Vulkan build.

Model weights: BreezeBlue/breeze-tts-2 (research/non-commercial license).
"""
from __future__ import annotations

import os
import random
import subprocess
import time
from typing import Optional

import httpx
import numpy as np
import soundfile as sf

from .base import ModelBackend, TTSResult


class BreezeBackend(ModelBackend):
    def __init__(self, config: dict, root: str):
        super().__init__("breeze", config, root)
        self._proc: Optional[subprocess.Popen] = None
        self._log_file = None

    def _available(self) -> bool:
        return True

    def _load(self) -> None:
        python = self.config.get("python")
        repo_dir = self.config.get("repo_dir")
        model_path = self.config.get("model_path")
        if not python or not repo_dir or not model_path:
            raise RuntimeError("breeze backend not configured (missing python/repo_dir/model_path)")
        port = str(self.config.get("port", 8137))
        env = dict(os.environ)
        env["PYTHONPATH"] = repo_dir
        log_path = os.path.join(self.root, "output", f"breeze_{int(time.time())}.log")
        self._log_file = open(log_path, "w")
        self._proc = subprocess.Popen(
            [python, "-m", "breeze_infer.api", model_path, "--host", "127.0.0.1", "--port", port],
            cwd=repo_dir, env=env,
            stdout=self._log_file, stderr=subprocess.STDOUT,
        )
        base = f"http://127.0.0.1:{port}"
        for _ in range(180):
            if self._proc.poll() is not None:
                raise RuntimeError(f"breeze-server exited early (code {self._proc.returncode})")
            try:
                r = httpx.get(base + "/health", timeout=3)
                if r.status_code == 200:
                    return
            except Exception:
                pass
            time.sleep(1)
        raise RuntimeError("breeze-server did not become healthy in time")

    def unload(self) -> None:
        if self._proc and self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self._proc.kill()
        self._proc = None
        if self._log_file:
            try:
                self._log_file.close()
            except Exception:
                pass
        self._log_file = None
        self._loaded = False
        self._load_error = None
        self._kill_orphans(["breeze_infer.api"])

    @property
    def supports_cloning(self) -> bool:
        return True

    @property
    def supports_design(self) -> bool:
        return True

    def voices(self) -> list:
        return []

    def settings_schema(self) -> list:
        return [
            {"name": "cfg_scale", "type": "float", "default": 4.0, "min": 1.0, "max": 10.0,
             "description": "Instruction-following strength (higher = stronger direction)."},
        ]

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
        port = str(self.config.get("port", 8137))
        base = f"http://127.0.0.1:{port}"
        if seed is None:
            ds = self.config.get("default_seed")
            seed = int(ds) if (ds is not None and ds != "" and ds != 0) else random.randint(0, 2**31 - 1)
        data = {"text": text, "seed": str(seed)}
        if instruction:
            data["instruction"] = instruction
            if cfg_scale is None:
                cfg_scale = float(self.config.get("default_cfg_scale", 4.0))
            data["cfg_scale"] = str(cfg_scale)
        else:
            data["cfg_scale"] = "1.0"

        files = None
        ref_handle = None
        try:
            if ref_audio and os.path.isfile(ref_audio):
                ref_handle = open(ref_audio, "rb")
                files = {"ref_audio": (os.path.basename(ref_audio), ref_handle, "audio/wav")}
                if ref_text:
                    data["ref_text"] = ref_text
            r = httpx.post(base + "/v1/audio/speech", data=data, files=files, timeout=600)
            r.raise_for_status()
        finally:
            if ref_handle:
                ref_handle.close()

        sr = int(r.headers.get("X-Sample-Rate", str(sample_rate)))
        pcm = np.frombuffer(r.content, dtype="<i2").astype(np.float32) / 32767.0

        out_path = self._out_path(output_format)
        if output_format == "ogg":
            sf.write(out_path, pcm, sr, format="ogg", subtype="opus")
        else:
            sf.write(out_path, pcm, sr)

        return TTSResult(
            audio_path=out_path,
            filename=os.path.basename(out_path),
            sample_rate=sr,
            duration_sec=round(len(pcm) / sr, 2),
            model=self.name,
            voice=voice,
        )
