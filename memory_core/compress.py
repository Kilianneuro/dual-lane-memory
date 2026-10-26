"""Pluggable compression.

`MockCompressor` is intentionally boring: it collapses whitespace and keeps the
first N characters. It lets lifecycle tests run offline. Swap in an LLM-backed
implementation by implementing the same protocol.
"""

from __future__ import annotations

from typing import Protocol


class Compressor(Protocol):
    def compress(self, text: str, *, max_chars: int = 50, level: int = 1) -> str:
        ...


class MockCompressor:
    def compress(self, text: str, *, max_chars: int = 50, level: int = 1) -> str:
        value = " ".join(str(text or "").split())
        return value[:max(1, int(max_chars))]


DEFAULT_COMPRESSOR: Compressor = MockCompressor()


def compress_text(text: str, *, max_chars: int = 50, level: int = 1,
                  compressor: Compressor | None = None) -> str:
    return (compressor or DEFAULT_COMPRESSOR).compress(
        text, max_chars=max_chars, level=level,
    )
