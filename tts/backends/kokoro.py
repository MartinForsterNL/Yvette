"""Kokoro TTS backend (CPU).

Ported from the agent-yvette tts_speak tool. Runs on CPU, works on Linux and
Windows. Generates at 22050 Hz, resampled to the server output rate (24000).

Chunking is handled by the server layer (see chunking.py); this backend
generates a single chunk per call.
"""
from __future__ import annotations

import os
import re
from typing import Optional

import numpy as np
import soundfile as sf

from .base import ModelBackend, TTSResult

VALID_VOICES = [
    "af_alloy", "af_aoede", "af_bella", "af_heart", "af_jessica", "af_kore",
    "af_nicole", "af_nova", "af_river", "af_sarah", "af_sky",
    "bf_alice", "bf_emma", "bf_isabella", "bf_lily",
    "bm_daniel", "bm_fable", "bm_george", "bm_lewis",
]


def _derive_lang_code(voice: str) -> str:
    if voice.startswith(("bf", "bm")):
        return "b"
    if voice.startswith(("af", "am")):
        return "a"
    return "b"


def _filter_characters(text: str) -> str:
    replacements = [
        (r"\*", ","), (r"'", ","), (r'"', ","),
        ("\u2014", ","), (";", ","), (":", ","),
    ]
    for pattern, repl in replacements:
        text = re.sub(pattern, repl, text)
    return text


class KokoroBackend(ModelBackend):
    def __init__(self, config: dict, root: str):
        super().__init__("kokoro", config, root)
        self._pipeline = None

    def _available(self) -> bool:
        try:
            import kokoro  # noqa: F401
            return True
        except ImportError:
            return False

    def _load(self) -> None:
        if not self._available():
            raise RuntimeError("kokoro module not installed (pip install kokoro)")
        from kokoro import KPipeline
        self._pipeline = KPipeline(lang_code="a")  # lang set per-call via voice

    def voices(self) -> list[dict]:
        return [
            {"id": v, "name": v, "lang_code": _derive_lang_code(v)}
            for v in VALID_VOICES
        ]

    def settings_schema(self) -> list[dict]:
        return [
            {"name": "speed", "type": "float", "default": 1.0, "min": 0.5, "max": 2.0,
             "description": "Speaking speed (higher = faster)"},
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
        if not voice:
            voice = self.config.get("default_voice", "bf_isabella")
        if voice not in VALID_VOICES:
            raise ValueError(f"unknown kokoro voice '{voice}'. Valid: {', '.join(VALID_VOICES)}")

        lang_code = _derive_lang_code(voice)
        if self._pipeline is None or getattr(self._pipeline, "lang_code", None) != lang_code:
            from kokoro import KPipeline
            self._pipeline = KPipeline(lang_code=lang_code)

        text = _filter_characters(text)
        speed = float((settings or {}).get("speed", self.config.get("speed", 1.0)))
        result = next(self._pipeline(text=text, voice=voice, speed=speed))
        audio = result.audio  # numpy at 22050 Hz
        orig_sr = 22050
        num = int(len(audio) * sample_rate / orig_sr)
        resampled = np.interp(np.linspace(0, len(audio), num), np.arange(len(audio)), audio)

        out_path = self._out_path(output_format)
        if output_format == "ogg":
            sf.write(out_path, resampled, sample_rate, format="ogg", subtype="opus")
        else:
            sf.write(out_path, resampled, sample_rate)

        return TTSResult(
            audio_path=out_path,
            filename=os.path.basename(out_path),
            sample_rate=sample_rate,
            duration_sec=round(len(audio) / orig_sr, 2),
            model=self.name,
            voice=voice,
            extra={"lang_code": lang_code},
        )
