"""Whisper STT via faster-whisper (CTranslate2).

The model is cached across calls. Also exposes model management (list, check
installed, download, delete) so the admin UI can manage Whisper models.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from typing import Optional

import soundfile as sf

from logs import log_event


def _free_cuda_memory() -> bool:
    """Force-release the CUDA context so GPU memory is returned to the driver.

    faster-whisper runs on CTranslate2, which keeps its CUDA context (and the
    memory pool) alive even after the model object is garbage collected. Calling
    cudaDeviceReset() destroys the context and returns all VRAM to the driver.
    This is safe here because Whisper is the only in-process CUDA user (kokoro
    and the embedder run on CPU; the other TTS engines are subprocesses).
    """
    try:
        import ctypes
        names = (
            "cudart64_13.dll", "cudart64_12.dll", "cudart64_11.dll",
            "cudart64_10.dll", "cudart.dll",
            "libcudart.so.13", "libcudart.so.12", "libcudart.so.11", "libcudart.so",
        )
        for name in names:
            try:
                cudart = ctypes.CDLL(name)
            except OSError:
                continue
            try:
                cudart.cudaDeviceReset()
                return True
            except Exception:
                continue
    except Exception:
        pass
    return False


# model id -> HuggingFace repo id
WHISPER_MODELS = {
    "tiny": "Systran/faster-whisper-tiny",
    "base": "Systran/faster-whisper-base",
    "small": "Systran/faster-whisper-small",
    "medium": "Systran/faster-whisper-medium",
    "large": "Systran/faster-whisper-large",
    "large-v2": "Systran/faster-whisper-large-v2",
    "large-v3": "Systran/faster-whisper-large-v3",
    "large-v3-turbo": "mobiuslabsgmbh/faster-whisper-large-v3-turbo",
    "distil-small.en": "distil-whisper/distil-small.en",
    "distil-medium.en": "distil-whisper/distil-medium.en",
    "distil-large-v2": "distil-whisper/distil-large-v2",
    "distil-large-v3": "distil-whisper/distil-large-v3",
}


class STT:
    def __init__(self, config: dict, hf_token: str = ""):
        self.config = config or {}
        self.hf_token = hf_token or ""
        self.model_name = self.config.get("model", "large-v3-turbo")
        self.language = self.config.get("language")  # None = auto-detect
        self.device = self.config.get("device", "cuda")
        self.compute_type = self.config.get("compute_type", "float16")
        self.beam_size = int(self.config.get("beam_size", 5))
        self.enabled = bool(self.config.get("enabled", True))
        self._model = None
        self._ffmpeg = None

    def _ffmpeg_path(self) -> Optional[str]:
        if self._ffmpeg is None:
            import shutil
            self._ffmpeg = shutil.which("ffmpeg")
        return self._ffmpeg

    def _ensure_model(self):
        if self._model is None:
            from faster_whisper import WhisperModel
            self._model = WhisperModel(
                self.model_name,
                device=self.device,
                compute_type=self.compute_type,
            )
        return self._model

    def reload(self):
        """Drop the cached model so the next transcribe re-loads with new settings."""
        self.unload()

    def is_loaded(self) -> bool:
        return self._model is not None

    def load(self):
        """Warm up the model (load into memory/VRAM)."""
        if self._model is not None:
            return
        log_event("info", f"loading Whisper model '{self.model_name}'")
        try:
            self._ensure_model()
            log_event("info", f"Whisper model '{self.model_name}' loaded")
        except Exception as e:
            log_event("error", f"Whisper model '{self.model_name}' failed to load: {type(e).__name__}: {e}")
            raise

    def unload(self):
        """Drop the model and free VRAM (including the CUDA context)."""
        was_loaded = self._model is not None
        self._model = None
        import gc
        gc.collect()
        _free_cuda_memory()
        if was_loaded:
            log_event("info", f"Whisper model '{self.model_name}' unloaded")

    def transcribe(self, audio_path: str, language: Optional[str] = None) -> dict:
        """Transcribe an audio file. Returns {text, language, duration_sec}."""
        if not self.enabled:
            return {"text": "", "language": None, "duration_sec": 0.0}
        if not os.path.isfile(audio_path):
            raise FileNotFoundError(f"audio file not found: {audio_path}")

        tmpdir = tempfile.mkdtemp(prefix="stt_")
        wav_path = os.path.join(tmpdir, "audio.wav")
        try:
            ffmpeg = self._ffmpeg_path()
            if ffmpeg:
                r = subprocess.run(
                    [ffmpeg, "-y", "-i", audio_path, "-ar", "16000", "-ac", "1", wav_path],
                    capture_output=True, text=True,
                )
                if r.returncode != 0:
                    raise RuntimeError(f"ffmpeg conversion failed: {r.stderr[-300:]}")
            else:
                data, sr = sf.read(audio_path)
                if data.ndim > 1:
                    data = data.mean(axis=1)
                if sr != 16000:
                    import numpy as np
                    num = int(len(data) * 16000 / sr)
                    data = np.interp(np.linspace(0, len(data), num), np.arange(len(data)), data)
                sf.write(wav_path, data, 16000)

            model = self._ensure_model()
            lang = language or self.language
            segments, info = model.transcribe(wav_path, language=lang, beam_size=self.beam_size)
            text = " ".join(seg.text.strip() for seg in segments).strip()

            duration = 0.0
            try:
                d, _ = sf.read(wav_path)
                duration = round(len(d) / 16000, 2)
            except Exception:
                pass
            return {
                "text": text,
                "language": getattr(info, "language", None),
                "duration_sec": duration,
            }
        except Exception as e:  # noqa: BLE001
            log_event("error", f"STT transcription error: {type(e).__name__}: {e}")
            raise
        finally:
            import shutil
            shutil.rmtree(tmpdir, ignore_errors=True)

    # -- model management -----------------------------------------------

    def _repo(self, model: str) -> str:
        return WHISPER_MODELS.get(model, f"Systran/faster-whisper-{model}")

    def _cache_dir(self, model: str) -> str:
        from huggingface_hub.constants import HF_HUB_CACHE
        return os.path.join(HF_HUB_CACHE, "models--" + self._repo(model).replace("/", "--"))

    def is_installed(self, model: str) -> bool:
        return os.path.isdir(self._cache_dir(model))

    def list_models(self) -> list:
        out = []
        for mid, repo in WHISPER_MODELS.items():
            out.append({
                "id": mid,
                "repo": repo,
                "installed": self.is_installed(mid),
                "current": mid == self.model_name,
            })
        return out

    def download_model(self, model: str) -> None:
        from huggingface_hub import snapshot_download
        snapshot_download(self._repo(model), token=self.hf_token or None)

    def delete_model(self, model: str) -> bool:
        d = self._cache_dir(model)
        if os.path.isdir(d):
            shutil.rmtree(d)
            return True
        return False
