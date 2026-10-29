from __future__ import annotations

import unittest

from memory_core.extract import ExtractedFact, RegexExtractor, extract_facts


class ExtractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.extractor = RegexExtractor()

    def test_game_progress(self) -> None:
        facts = self.extractor.extract("我现在在玩只狼，卡在苇名弦一郎")
        self.assertEqual(len(facts), 1)
        self.assertIsInstance(facts[0], ExtractedFact)
        self.assertEqual(facts[0].category, "game_progress")
        self.assertIn("用户现在在玩只狼", facts[0].content)

    def test_sensitive_tag(self) -> None:
        facts = self.extractor.extract("我住在朝阳区某小区")
        self.assertEqual(len(facts), 1)
        self.assertEqual(facts[0].sensitivity, "sensitive")

    def test_no_match(self) -> None:
        self.assertEqual(self.extractor.extract("今天天气不错"), [])

    def test_wrapper_returns_dicts(self) -> None:
        facts = extract_facts("我喜欢吃火锅")
        self.assertEqual(facts[0]["category"], "preference")
        self.assertEqual(facts[0]["content"], "用户喜欢吃火锅")

    def test_sensitive_phone_fallback(self) -> None:
        facts = self.extractor.extract("我的手机号是 13800138000")
        self.assertEqual(len(facts), 1)
        self.assertEqual(facts[0].sensitivity, "sensitive")
        self.assertIn("13800138000", facts[0].content)

    def test_sensitive_id_fallback(self) -> None:
        facts = self.extractor.extract("我的身份证号是 440101199001011234")
        self.assertEqual(len(facts), 1)
        self.assertEqual(facts[0].sensitivity, "sensitive")


if __name__ == "__main__":
    unittest.main()
