from __future__ import annotations

import unittest

from memory_core.compress import MockCompressor, compress_text


class UpperCompressor:
    def compress(self, text: str, *, max_chars: int = 50, level: int = 1) -> str:
        return str(text).upper()[:max_chars]


class CompressTest(unittest.TestCase):
    def test_mock_collapses_and_truncates(self) -> None:
        out = MockCompressor().compress("  hello   world  ", max_chars=8)
        self.assertEqual(out, "hello wo")

    def test_custom_compressor_is_used(self) -> None:
        out = compress_text("hello", compressor=UpperCompressor())
        self.assertEqual(out, "HELLO")

    def test_level_is_passed_to_custom_compressor(self) -> None:
        class LevelCompressor:
            def compress(self, text: str, *, max_chars: int = 50, level: int = 1) -> str:
                return f"{level}:{text}"[:max_chars]
        self.assertEqual(compress_text("x", compressor=LevelCompressor(), level=2), "2:x")


if __name__ == "__main__":
    unittest.main()
