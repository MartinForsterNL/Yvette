"""Orpheus TTS 3 backend - preset voices and emotion tags.

Runs the pinned Lex-au/Orpheus-FastAPI project as a library behind a thin
wrapper (servers/orpheus_server.py). The wrapper owns llama.cpp with the Orpheus
3B GGUF and the SNAC 24 kHz decoder; this backend just spawns it and posts text.

Preset voices only: no reference-audio cloning and no instruction/design mode.
Expressiveness comes from inline tags in the text (`<laugh>`, `<sigh>`, ...).
The engine is multilingual (EN, FR, DE, KO, HI, ZH, ES, IT) but has no Dutch.

Model: lex-au/Orpheus-3b-FT-Q4_K_M.gguf (Apache-2.0).
Wrapper and upstream: Apache-2.0.
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

# id, display name, language code (the upstream project's voice list)
VOICES = [
    ("tara", "Tara", "en"),
    ("leah", "Leah", "en"),
    ("jess", "Jess", "en"),
    ("leo", "Leo", "en"),
    ("dan", "Dan", "en"),
    ("mia", "Mia", "en"),
    ("zac", "Zac", "en"),
    ("zoe", "Zoe", "en"),
    ("pierre", "Pierre", "fr"),
    ("amelie", "Amelie", "fr"),
    ("marie", "Marie", "fr"),
    ("jana", "Jana", "de"),
    ("thomas", "Thomas", "de"),
    ("max", "Max", "de"),
    ("유나", "Yuna", "ko"),
    ("준서", "Junseo", "ko"),
    ("ऋतिका", "Ritika", "hi"),
    ("长乐", "Changle", "zh"),
    ("白芷", "Baizhi", "zh"),
    ("javi", "Javi", "es"),
    ("sergio", "Sergio", "es"),
    ("maria", "Maria", "es"),
    ("pietro", "Pietro", "it"),
    ("giulia", "Giulia", "it"),
    ("carlo", "Carlo", "it"),
]
VALID_VOICES = [v[0] for v in VOICES]


class OrpheusBackend(ModelBackend):
    def __init__(self, config: dict, root: str):
        super().__init__("orpheus", config, root)
        self._cfg = config or {}
        self._port = int(self._cfg.get("port", 8142))
        self._llama_port = int(self._cfg.get("llama_port", 8143))
        self._python = (self._cfg.get("python") or "").strip()
        self._script = (self._cfg.get("server_script") or "").strip()
        self._bin = (self._cfg.get("bin") or "").strip()
        self._model = (self._cfg.get("model_path") or "").strip()
        self._proc: Optional[subprocess.Popen] = None
        self._log_file = None

    # -- availability ----------------------------------------------------

    def _available(self) -> bool:
        if not self._python or not os.path.isfile(self._python):
            return False
        if not self._script or not os.path.isfile(self._script):
            return False
        if not self._bin or not os.path.isfile(self._bin):
            return False
        return bool(self._model and os.path.isfile(self._model))

    # -- lifecycle -------------------------------------------------------

    def _load(self) -> None:
        if not self._available():
            raise RuntimeError("orpheus is not installed (missing python, wrapper, llama-server or model)")
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUTF8"] = "1"
        env["ORPHEUS_PORT"] = str(self._port)
        env["ORPHEUS_LLAMA_PORT"] = str(self._llama_port)
        env["ORPHEUS_LLAMA_BIN"] = self._bin
        env["ORPHEUS_MODEL_PATH"] = self._model
        if self._cfg.get("max_tokens") is not None:
            env["ORPHEUS_MAX_TOKENS"] = str(self._cfg["max_tokens"])
        if self._cfg.get("temperature") is not None:
            env["ORPHEUS_TEMPERATURE"] = str(self._cfg["temperature"])
        if self._cfg.get("top_p") is not None:
            env["ORPHEUS_TOP_P"] = str(self._cfg["top_p"])

        log_path = os.path.join(self.root, "output", "orpheus_%d.log" % int(time.time()))
        self._log_file = open(log_path, "w", encoding="utf-8", errors="replace")
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
        url = "http://127.0.0.1:%d/health" % self._port
        while time.time() < deadline:
            if self._proc.poll() is not None:
                raise RuntimeError(
                    "orpheus-server exited early (code %s). See log: %s"
                    % (self._proc.returncode, log_path)
                )
            try:
                r = requests.get(url, timeout=3)
                if r.status_code == 200 and r.json().get("ok"):
                    return
                if r.status_code == 500:
                    raise RuntimeError("orpheus-server: %s" % (r.json().get("error") or r.text[:300]))
            except requests.RequestException:
                pass
            time.sleep(2)
        raise RuntimeError("orpheus-server did not come up within 600s. See log: %s" % log_path)

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
        # The wrapper's llama-server child can outlive a hard kill and keep VRAM.
        # Match the engine-specific path, not every llama-server on the machine.
        self._kill_orphans(["orpheus_server.py", "orpheus.*llama-server"])
        self._loaded = False
        self._load_error = None

    # -- capabilities ----------------------------------------------------

    @property
    def supports_cloning(self) -> bool:
        return False

    @property
    def supports_design(self) -> bool:
        return False

    def voices(self) -> list[dict]:
        default = self._cfg.get("default_voice", "tara")
        return [
            {"id": vid, "name": name, "lang_code": lang, "default": vid == default}
            for vid, name, lang in VOICES
        ]

    def settings_schema(self) -> list[dict]:
        # No per-request knobs: voice is the only generation choice. Temperature,
        # top_p and max_tokens are engine-level settings in the admin (load time).
        return []

    def status(self) -> dict:
        s = super().status()
        s["engine"] = "orpheus/3b-gguf"
        s["port"] = self._port
        s["llama_port"] = self._llama_port
        s["model"] = os.path.basename(self._model) if self._model else ""
        s["default_voice"] = self._cfg.get("default_voice", "tara")
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
        voice = (voice or self._cfg.get("default_voice", "tara")).strip()
        if voice not in VALID_VOICES:
            raise ValueError("unknown orpheus voice '%s'" % voice)

        r = requests.post(
            "http://127.0.0.1:%d/generate" % self._port,
            json={"text": text, "voice": voice},
            timeout=900,
        )
        if r.status_code != 200:
            try:
                detail = r.json().get("detail") or r.json().get("error")
            except Exception:
                detail = r.text
            raise RuntimeError("orpheus: %s" % (detail or ("HTTP %d" % r.status_code)))
        wav = r.content
        if not wav:
            raise RuntimeError("orpheus returned no audio")

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
            voice=voice,
            extra={"engine": "orpheus"},
        )
