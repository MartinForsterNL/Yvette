"""Sentence-level text chunking shared by all TTS backends."""

import re


def _split_long(text: str, max_chars: int) -> list[str]:
    """Split a single over-long sentence into pieces <= max_chars."""
    parts = []
    while len(text) > max_chars:
        m = re.split(r",\s*", text, maxsplit=1)
        if len(m) > 1 and len(m[0]) < len(m[1]):
            parts.append(m[0].strip())
            text = m[1]
        else:
            words = text.split()
            mid = len(words) // 2
            parts.append(" ".join(words[:mid]).strip())
            text = " ".join(words[mid:])
    parts.append(text.strip())
    return [p for p in parts if p]


def chunk_text(text: str, max_chars: int = 400, min_chunk_chars: int = 40) -> list[str]:
    """Split text into sentence-level chunks.

    - One sentence per chunk by default.
    - Very short sentences are merged forward (until min_chunk_chars).
    - Over-long sentences are split further (up to max_chars).
    """
    sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", text) if s.strip()]
    chunks = []
    i = 0
    while i < len(sentences):
        s = sentences[i]
        i += 1
        if len(s) > max_chars:
            chunks.extend(_split_long(s, max_chars))
            continue
        buf = s
        while (
            i < len(sentences)
            and len(buf) < min_chunk_chars
            and len(buf) + 1 + len(sentences[i]) <= max_chars
        ):
            buf = buf + " " + sentences[i]
            i += 1
        chunks.append(buf)
    return [c for c in chunks if c]
