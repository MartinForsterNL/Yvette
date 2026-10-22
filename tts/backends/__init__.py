"""TTS model backends."""
from .base import ModelBackend, TTSResult
from .kokoro import KokoroBackend
from .breeze import BreezeBackend
from .omnivoice import OmniVoiceBackend
from .lux import LuxBackend
from .higgs import HiggsBackend
from .orpheus import OrpheusBackend

__all__ = ["ModelBackend", "TTSResult", "KokoroBackend", "BreezeBackend", "OmniVoiceBackend", "LuxBackend", "HiggsBackend", "OrpheusBackend"]
