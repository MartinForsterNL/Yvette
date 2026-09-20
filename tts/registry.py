"""TTS manager - in-process orchestration of the TTS backends.

Refactored from the standalone tts_server.py so the merged app can call TTS
directly (no HTTP hop). Holds the backend registry, the voice profile store,
Whisper STT, and the synthesize() flow.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from typing import Optional

import numpy as np
import soundfile as sf

from logs import log_event

from .backends import KokoroBackend, BreezeBackend, OmniVoiceBackend, LuxBackend
from .voices import VoiceStore
from .stt import STT
from .chunking import chunk_text

# Serializes model generation across concurrent requests (one model at a time).
GEN_LOCK = threading.Lock()


def _path_base(config: dict) -> str:
    base = ((config.get("paths") or {}).get("base") or "").strip()
    if not base:
        return ""
    base = os.path.expandvars(os.path.expanduser(base))
    return base


def _resolve_path(p, root: str, config: dict):
    if not p:
        return p
    p = os.path.expandvars(os.path.expanduser(str(p)))
    if os.path.isabs(p):
        return os.path.normpath(p)
    base = _path_base(config)
    if base and os.path.isabs(base):
        return os.path.normpath(os.path.join(base, p))
    return os.path.normpath(os.path.join(root, base or ".", p))


def _ffmpeg_path(config: dict) -> Optional[str]:
    return shutil.which("ffmpeg") or (config.get("ffmpeg") or "").strip() or None


def concat_audio(files: list, out_path: str, output_format: str, sample_rate: int, ffmpeg: Optional[str], gain_db: float = 0.0) -> None:
    """Concatenate chunk files into a single audio file, applying an optional gain."""
    if not files:
        raise ValueError("no chunks to concatenate")
    gain_db = float(gain_db or 0.0)
    if output_format == "ogg":
        if not ffmpeg:
            raise RuntimeError("ffmpeg not found (required for ogg concatenation)")
        tmpdir = tempfile.mkdtemp(prefix="tts_concat_")
        try:
            concat_list = os.path.join(tmpdir, "concat.txt")
            with open(concat_list, "w") as f:
                for cf in files:
                    f.write(f"file '{cf}'\n")
            cmd = [ffmpeg, "-y", "-f", "concat", "-safe", "0", "-i", concat_list]
            if gain_db:
                cmd += ["-af", f"volume={gain_db}dB,alimiter=limit=0.95"]
            cmd += ["-c:a", "libopus", "-application", "lowdelay", out_path]
            r = subprocess.run(cmd, capture_output=True, text=True)
            if r.returncode != 0:
                raise RuntimeError(f"ffmpeg concat failed: {r.stderr[-300:]}")
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)
    else:
        arrays = [sf.read(cf)[0] for cf in files]
        combined = np.concatenate(arrays) if arrays else np.zeros(1, dtype=np.float32)
        if gain_db:
            combined = np.clip(combined * (10.0 ** (gain_db / 20.0)), -0.95, 0.95)
        sf.write(out_path, combined, sample_rate)


class TTSManager:
    def __init__(self, root: str, config: dict):
        self.root = root
        self.config = config or {}
        server_cfg = self.config.get("server", {}) or {}
        self.sample_rate = int(server_cfg.get("sample_rate", 24000))
        self.output_format = server_cfg.get("output_format", "wav")
        chunk_cfg = self.config.get("chunking", {}) or {}
        self.max_chunk_chars = int(chunk_cfg.get("max_chunk_chars", 400))
        self.min_chunk_chars = int(chunk_cfg.get("min_chunk_chars", 100))
        self.ffmpeg = _ffmpeg_path(self.config)
        self.gain_db = float(server_cfg.get("gain_db", 0) or 0)
        self.backends = {}
        self.voices = VoiceStore(root, self.config.get("voices", {}))
        self.stt = STT(self.config.get("stt", {}), hf_token=(self.config.get("huggingface", {}) or {}).get("token", ""))
        self.output_dir = os.path.join(root, "output")
        os.makedirs(self.output_dir, exist_ok=True)
        self.unload_idle_minutes = int((self.config.get("models", {}) or {}).get("unload_idle_minutes", 60))
        self.unload_others_before_load = bool((self.config.get("models", {}) or {}).get("unload_others_before_load", True))
        self._last_used = {}
        self._init_backends()
        threading.Thread(target=self._unload_idle_loop, daemon=True).start()

    # -- backends --------------------------------------------------------

    def _resolve_backend_paths(self, cfg):
        cfg = dict(cfg or {})
        for key in ("python", "repo_dir", "model_path", "server_script"):
            if cfg.get(key):
                cfg[key] = _resolve_path(cfg[key], self.root, self.config)
        return cfg

    def _init_backends(self):
        models_cfg = self.config.get("models", {}) or {}
        if models_cfg.get("kokoro", {}).get("enabled", True):
            self.backends["kokoro"] = KokoroBackend(models_cfg.get("kokoro", {}), self.root)
        if models_cfg.get("breeze", {}).get("enabled", True):
            self.backends["breeze"] = BreezeBackend(self._resolve_backend_paths(models_cfg.get("breeze", {})), self.root)
        if models_cfg.get("omnivoice", {}).get("enabled", True):
            self.backends["omnivoice"] = OmniVoiceBackend(self._resolve_backend_paths(models_cfg.get("omnivoice", {})), self.root)
        if models_cfg.get("lux", {}).get("enabled", True):
            self.backends["lux"] = LuxBackend(self._resolve_backend_paths(models_cfg.get("lux", {})), self.root)
        for name, be in self.backends.items():
            if be.config.get("auto_start", False):
                be.ensure_loaded()
                self._last_used[name] = time.time()

    def get_backend(self, name: str):
        be = self.backends.get(name)
        if be is None:
            raise KeyError(f"unknown model '{name}'. Available: {list(self.backends)}")
        return be

    def model_status(self) -> list:
        out = []
        for name, be in self.backends.items():
            try:
                out.append(be.status())
            except Exception as e:  # noqa: BLE001
                out.append({"name": name, "error": str(e)})
        return out

    def unload(self, name: str) -> None:
        self.get_backend(name).unload()

    def load(self, name: str):
        """Load a model, optionally unloading all other loaded models first."""
        if self.unload_others_before_load:
            for n, be in self.backends.items():
                if n != name and be._loaded:
                    be.unload()
                    log_event("info", f"unloaded '{n}' to make room for '{name}'")
        be = self.get_backend(name)
        be.ensure_loaded()
        if be._load_error:
            raise RuntimeError(f"model '{name}' not available: {be._load_error}")
        self._last_used[name] = time.time()
        return be

    def _unload_idle_loop(self):
        while True:
            time.sleep(60)
            try:
                if self.unload_idle_minutes <= 0:
                    continue
                now = time.time()
                for name, be in self.backends.items():
                    if not be._loaded:
                        continue
                    last = self._last_used.get(name, 0)
                    if last and (now - last) > self.unload_idle_minutes * 60:
                        be.unload()
            except Exception:
                pass

    # -- synthesis -------------------------------------------------------

    def synthesize(
        self,
        text: str,
        model: str = "",
        *,
        voice: str = "",
        voice_id: str = "",
        instruction_id: str = "",
        instruction: str = "",
        cfg_scale: Optional[float] = None,
        seed: Optional[int] = None,
        output_format: str = "",
        gain_db: Optional[float] = None,
        settings: Optional[dict] = None,
    ) -> dict:
        """Generate speech for one sentence/chunk set. Returns
        {audio_path, filename, duration_sec, sample_rate, model}."""
        if not text or not text.strip():
            raise ValueError("text required")
        model = model or self.config.get("models", {}).get("default", "breeze")
        be = self.get_backend(model)

        fmt = output_format or self.output_format

        # Resolve voice profile -> ref_audio / ref_text / instruction
        ref_audio = ref_text = instr = None
        if voice_id:
            profile = self.voices.get(voice_id)
            if profile is None:
                raise KeyError(f"voice profile '{voice_id}' not found")
            if profile.get("kind") == "clone":
                ref_audio = self.voices.ref_audio_abs(voice_id)
                ref_text = profile.get("transcript", "")
                if not ref_audio:
                    raise RuntimeError(f"voice '{voice_id}' has no reference audio")
            elif profile.get("kind") == "design":
                instr = profile.get("instruction", "")
        if instruction_id:
            profile = self.voices.get(instruction_id)
            if profile is None:
                raise KeyError(f"voice profile '{instruction_id}' not found")
            instr = profile.get("instruction", "") or instr
        if (instruction or "").strip():
            instr = instruction.strip()

        chunks = chunk_text(text, self.max_chunk_chars, self.min_chunk_chars)
        if not chunks:
            raise ValueError("text produced no chunks after filtering")

        gen_args = dict(
            voice=voice,
            ref_audio=ref_audio,
            ref_text=ref_text,
            instruction=instr,
            cfg_scale=cfg_scale,
            seed=seed,
            settings=settings,
            sample_rate=self.sample_rate,
        )

        be = self.load(model)

        chunk_files = []
        durations = 0.0
        try:
            with GEN_LOCK:
                for chunk in chunks:
                    result = be.generate(chunk, **gen_args, output_format="wav")
                    chunk_files.append(result.audio_path)
                    durations += result.duration_sec
        except Exception as e:  # noqa: BLE001
            log_event("error", f"TTS inference error ({model}): {type(e).__name__}: {e}")
            raise RuntimeError(f"generation failed: {type(e).__name__}: {e}")

        ext = "ogg" if fmt == "ogg" else "wav"
        final_path = os.path.join(self.output_dir, f"tts_{int(time.time())}_{uuid.uuid4().hex[:6]}.{ext}")
        try:
            eff_gain = gain_db if gain_db is not None else self.gain_db
            concat_audio(chunk_files, final_path, fmt, self.sample_rate, self.ffmpeg, eff_gain)
        except Exception as e:  # noqa: BLE001
            raise RuntimeError(f"concat failed: {e}")
        finally:
            for cf in chunk_files:
                try:
                    os.remove(cf)
                except OSError:
                    pass

        return {
            "audio_path": final_path,
            "filename": os.path.basename(final_path),
            "duration_sec": round(durations, 2),
            "sample_rate": self.sample_rate,
            "model": model,
        }

    # -- speech-to-text --------------------------------------------------

    def transcribe(self, audio_path: str, language: Optional[str] = None) -> dict:
        return self.stt.transcribe(audio_path, language)
