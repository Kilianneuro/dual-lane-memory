"""Dual-lane lifecycle memory core for chat agents.

Public API: MemoryStore. Extraction and compression are pluggable through
memory_core.extract and memory_core.compress.
"""

from .compress import Compressor, MockCompressor
from .extract import ExtractedFact, Extractor, RegexExtractor
from .store import MemoryStore

__all__ = [
    "MemoryStore",
    "Compressor",
    "MockCompressor",
    "Extractor",
    "ExtractedFact",
    "RegexExtractor",
]
__version__ = "0.1.0"
