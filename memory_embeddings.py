"""Embedding model provider for the talk server's memory.

Local-first SentenceTransformer (Qwen3-Embedding-0.6B). Loads lazily in a
background thread. Never raises - failures degrade to keyword-only search.
"""

import threading

import numpy as np

DEFAULT_MODEL = "Qwen/Qwen3-Embedding-0.6B"


class Embedder:
    def __init__(self, model_name: str = DEFAULT_MODEL, device: str = "cpu"):
        self.model_name = model_name
        self.device = device
        self._model = None
        self._available = False
        self._load_started = False
        self._lock = threading.Lock()

    def warmup(self):
        if self._load_started:
            return
        self._load_started = True
        threading.Thread(target=self._load_model, daemon=True).start()

    def _load_model(self):
        try:
            from sentence_transformers import SentenceTransformer
            self._model = SentenceTransformer(self.model_name, device=self.device)
            self._available = True
            print(f"[embedder] ready: {self.model_name}")
        except Exception as e:  # noqa: BLE001
            self._available = False
            print(f"[embedder] load failed: {e}")

    def available(self) -> bool:
        return self._available and self._model is not None

    def embed(self, texts) -> list:
        """Encode a list of texts. Raises if unavailable."""
        if not self.available():
            raise RuntimeError("embedder not ready")
        vecs = self._model.encode(
            list(texts), normalize_embeddings=True,
            batch_size=8, show_progress_bar=False,
        )
        return [np.asarray(v, dtype=np.float32) for v in vecs]
