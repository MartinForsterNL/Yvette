"""TTS model backends."""
from .base import ModelBackend, TTSResult
from .kokoro import KokoroBackend
from .breeze import BreezeBackend
from .omnivoice import OmniVoiceBackend
from .lux import LuxBackend

__all__ = ["ModelBackend", "TTSResult", "KokoroBackend", "BreezeBackend", "OmniVoiceBackend", "LuxBackend"]
