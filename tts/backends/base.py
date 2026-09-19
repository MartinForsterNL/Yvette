"""Model backend base class.

A backend wraps one TTS engine (Kokoro, Breeze, ...). The server keeps a
registry of backends; each exposes a uniform interface so the API and Web UI
are model-agnostic.
"""
from __future__ import annotations

import abc
import os
import time
import uuid
from dataclasses import dataclass, field
from typing import Optional

from logs import log_event


@dataclass
class TTSResult:
    """Result of a TTS generation."""
    audio_path: str          # absolute path to the generated file
    filename: str            # basename
    sample_rate: int
    duration_sec: float
    model: str
    voice: str
    extra: dict = field(default_factory=dict)


class ModelBackend(abc.ABC):
    """Abstract TTS model backend."""

    def __init__(self, name: str, config: dict, root: str):
        self.name = name
        self.config = config or {}
        self.root = root  # tts-server root dir
        self._loaded = False
        self._load_error: Optional[str] = None

    # -- lifecycle -------------------------------------------------------

    @abc.abstractmethod
    def _load(self) -> None:
        """Load the model / start the subprocess. Raise on failure."""

    def ensure_loaded(self) -> None:
        """Idempotent load. Sets _load_error instead of raising on failure."""
        if self._loaded:
            return
        try:
            log_event("info", f"loading TTS engine '{self.name}'")
            self._load()
            self._loaded = True
            self._load_error = None
            log_event("info", f"TTS engine '{self.name}' loaded")
        except Exception as e:  # noqa: BLE001 - report, don't crash the server
            self._load_error = f"{type(e).__name__}: {e}"
            log_event("error", f"TTS engine '{self.name}' failed to load: {self._load_error}")

    def unload(self) -> None:
        """Release resources (model weights, subprocess)."""
        was_loaded = self._loaded
        self._loaded = False
        self._load_error = None
        if was_loaded:
            log_event("info", f"TTS engine '{self.name}' unloaded")

    def _kill_orphans(self, patterns) -> None:
        """Hard-kill any leftover subprocess whose command line matches a pattern.

        The subprocess backends spawn separate processes (breeze-server.exe,
        omnivoice_server.py, lux_server.py). If the TTS server restarts while one
        is running, that subprocess is orphaned and keeps holding VRAM. This
        sweeps them up so unload() actually frees memory.
        """
        if os.name != "nt":
            return
        try:
            import subprocess
            cond = " -or ".join([f"$_.CommandLine -match '{p}'" for p in patterns])
            script = f"Get-CimInstance Win32_Process | Where-Object {{ {cond} }} | ForEach-Object {{ Stop-Process -Id $_.ProcessId -Force }}"
            subprocess.run(["powershell", "-Command", script], capture_output=True, timeout=30)
        except Exception:
            pass

    # -- capabilities ----------------------------------------------------

    @property
    def available(self) -> bool:
        """True if the backend can run on this machine (deps + hardware)."""
        return self._available()

    @abc.abstractmethod
    def _available(self) -> bool:
        ...

    @property
    def supports_cloning(self) -> bool:
        return False

    @property
    def supports_design(self) -> bool:
        return False

    def voices(self) -> list[dict]:
        """List of preset voices: [{id, name, lang_code?}]."""
        return []

    def settings_schema(self) -> list[dict]:
        """Tunable generation settings for this engine.

        Each item: {name, type, default, min?, max?, options?, description}.
        type is one of: int | float | bool | select.
        """
        return []

    # -- generation ------------------------------------------------------

    @abc.abstractmethod
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
        """Generate speech. Must be called after ensure_loaded()."""

    # -- status ----------------------------------------------------------

    def status(self) -> dict:
        return {
            "name": self.name,
            "enabled": bool(self.config.get("enabled", True)),
            "available": self.available,
            "loaded": self._loaded,
            "load_error": self._load_error,
            "supports_cloning": self.supports_cloning,
            "supports_design": self.supports_design,
            "settings": self.settings_schema(),
            "voices": self.voices(),
        }

    # -- helpers ---------------------------------------------------------

    def _out_path(self, output_format: str) -> str:
        out_dir = os.path.join(self.root, "output")
        os.makedirs(out_dir, exist_ok=True)
        ext = "ogg" if output_format == "ogg" else "wav"
        return os.path.join(out_dir, f"tts_{self.name}_{int(time.time())}_{uuid.uuid4().hex[:6]}.{ext}")
