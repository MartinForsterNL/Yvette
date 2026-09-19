"""Voice profile store.

A voice profile is a named voice the user can select in the UI. Two kinds:
  - clone:  reference audio + exact transcript (for Breeze voice cloning)
  - design: natural-language instruction (for Breeze voice design)

Profiles are stored in voices/profiles.json. Reference audio lives in
voices/ref_audio/. Preset voices (Kokoro) are not stored here - they come
from the backend.
"""
from __future__ import annotations

import json
import os
import shutil
import time
import uuid
from typing import Optional


class VoiceStore:
    def __init__(self, root: str, config: dict):
        self.root = root
        self.config = config or {}
        self.profiles_file = os.path.join(root, self.config.get("profiles_file", "voices/profiles.json"))
        self.ref_dir = os.path.join(root, self.config.get("ref_audio_dir", "voices/ref_audio"))
        os.makedirs(os.path.dirname(self.profiles_file), exist_ok=True)
        os.makedirs(self.ref_dir, exist_ok=True)

    # -- load / save -----------------------------------------------------

    def _load(self) -> dict:
        if not os.path.isfile(self.profiles_file):
            return {}
        try:
            with open(self.profiles_file) as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return {}

    def _save(self, profiles: dict) -> None:
        tmp = self.profiles_file + ".tmp"
        with open(tmp, "w") as f:
            json.dump(profiles, f, indent=2)
        os.replace(tmp, self.profiles_file)

    # -- CRUD ------------------------------------------------------------

    def list(self) -> list[dict]:
        profiles = self._load()
        out = []
        for pid, p in profiles.items():
            item = dict(p)
            item["id"] = pid
            # reference audio availability
            if p.get("ref_audio"):
                item["ref_audio_exists"] = os.path.isfile(os.path.join(self.root, p["ref_audio"]))
            out.append(item)
        out.sort(key=lambda x: x.get("created", ""), reverse=True)
        return out

    def get(self, pid: str) -> Optional[dict]:
        profiles = self._load()
        p = profiles.get(pid)
        if p is None:
            return None
        item = dict(p)
        item["id"] = pid
        return item

    def create(
        self,
        name: str,
        kind: str,
        model: str = "breeze",
        ref_audio_path: Optional[str] = None,
        transcript: str = "",
        instruction: str = "",
        engines: Optional[list] = None,
    ) -> dict:
        """Create a profile. ref_audio_path is an absolute path to a temp file
        that will be moved into the ref_audio dir."""
        if kind not in ("clone", "design"):
            raise ValueError("kind must be 'clone' or 'design'")
        if kind == "clone" and not ref_audio_path:
            raise ValueError("clone profile needs ref_audio")
        if kind == "design" and not instruction:
            raise ValueError("design profile needs instruction")

        profiles = self._load()
        pid = f"v_{int(time.time())}_{uuid.uuid4().hex[:6]}"

        rel_ref = None
        if ref_audio_path and os.path.isfile(ref_audio_path):
            ext = os.path.splitext(ref_audio_path)[1] or ".wav"
            dest = os.path.join(self.ref_dir, f"{pid}{ext}")
            shutil.move(ref_audio_path, dest)
            rel_ref = os.path.relpath(dest, self.root)

        eng = list(engines) if engines else ([model] if model else ["breeze"])
        profile = {
            "name": name,
            "kind": kind,
            "model": eng[0],
            "engines": eng,
            "ref_audio": rel_ref,
            "transcript": transcript,
            "instruction": instruction,
            "created": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }
        profiles[pid] = profile
        self._save(profiles)
        profile["id"] = pid
        return profile

    def delete(self, pid: str) -> bool:
        profiles = self._load()
        if pid not in profiles:
            return False
        p = profiles.pop(pid)
        if p.get("ref_audio"):
            ref = os.path.join(self.root, p["ref_audio"])
            if os.path.isfile(ref):
                os.remove(ref)
        self._save(profiles)
        return True

    def update(
        self,
        pid: str,
        *,
        name: Optional[str] = None,
        transcript: Optional[str] = None,
        instruction: Optional[str] = None,
        engines: Optional[list] = None,
    ) -> Optional[dict]:
        profiles = self._load()
        p = profiles.get(pid)
        if p is None:
            return None
        if name is not None:
            p["name"] = name
        if transcript is not None:
            p["transcript"] = transcript
        if instruction is not None:
            p["instruction"] = instruction
        if engines is not None:
            eng = list(engines)
            p["engines"] = eng
            if eng:
                p["model"] = eng[0]
        self._save(profiles)
        item = dict(p)
        item["id"] = pid
        return item

    def ref_audio_abs(self, pid: str) -> Optional[str]:
        p = self.get(pid)
        if p and p.get("ref_audio"):
            return os.path.join(self.root, p["ref_audio"])
        return None
