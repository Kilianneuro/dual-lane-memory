from __future__ import annotations

import unittest

from memory_core.lifecycle import (
    FAST_HARD_GRACE_DAYS,
    FAST_PURGE_AGE_DAYS,
    SLOW_HARD_GRACE_DAYS,
    SLOW_PURGE_AGE_DAYS,
    compress_memory,
    decay_memory,
)
from memory_core.store import MemoryStore, NotFoundError

DAY = 86_400_000
BASE = 1_760_000_000_000


class LifecycleTest(unittest.TestCase):
    def setUp(self) -> None:
        self.store = MemoryStore(":memory:", owner_id="10001")
        self.group = {"platform": "qq", "context_type": "group", "context_id": "4001"}
        self.video = {"platform": "bilibili", "context_type": "video", "context_id": "BV1life"}
        self.video2 = {"platform": "bilibili", "context_type": "video", "context_id": "BV2life"}

    def tearDown(self) -> None:
        self.store.close()

    def _save(self, content: str, source: dict, **extra):
        payload = {
            "content": content,
            "category": "media_impression",
            "source": source,
            "context_scope": "share_same_platform",
            "visibility": "global",
        }
        payload.update(extra)
        return self.store.save(payload, now_ms=BASE)

    def test_fast_lane_full_lifecycle(self) -> None:
        result = self._save("用户刷到一个很好笑的视频，笑了一路。" * 10, self.video)
        memory_id = result["memory_id"]
        self.assertEqual(result["category"], "media_impression")

        at_2d = decay_memory(self.store, now_ms=BASE + 2 * DAY)
        row = self.store.get(memory_id, admin=True)
        self.assertEqual(row["status"], "active")
        self.assertEqual(row["compression_level"], 1)
        self.assertLessEqual(len(row["summary"] or ""), 50)
        self.assertGreater(at_2d["compressed_level1"], 0)
        self.assertLess(row["weight"], result["weight"])

        decay_memory(self.store, now_ms=BASE + 8 * DAY)
        row = self.store.get(memory_id, admin=True)
        self.assertEqual(row["compression_level"], 2)
        self.assertLessEqual(len(row["summary"] or ""), 24)

        decay_memory(self.store, now_ms=BASE + FAST_PURGE_AGE_DAYS * DAY)
        row = self.store.get(memory_id, admin=True)
        self.assertEqual(row["status"], "purged")
        self.assertIsNotNone(row["purged_at"])

        decay_memory(self.store, now_ms=BASE + (FAST_PURGE_AGE_DAYS + FAST_HARD_GRACE_DAYS) * DAY)
        with self.assertRaises(NotFoundError):
            self.store.get(memory_id, admin=True)

    def test_slow_lane_levels_and_purge(self) -> None:
        result = self.store.save(
            {
                "content": "用户在《只狼》卡在某个 Boss，试了很多次。" * 8,
                "category": "game_progress",
                "source": self.group,
                "context_scope": "share_all",
                "visibility": "global",
            },
            now_ms=BASE,
        )
        memory_id = result["memory_id"]

        decay_memory(self.store, now_ms=BASE + 120 * DAY)
        row = self.store.get(memory_id, admin=True)
        self.assertEqual(row["compression_level"], 1)
        self.assertLessEqual(len(row["summary"] or ""), 50)

        decay_memory(self.store, now_ms=BASE + 250 * DAY)
        row = self.store.get(memory_id, admin=True)
        self.assertEqual(row["compression_level"], 2)
        self.assertLessEqual(len(row["summary"] or ""), 24)

        decay_memory(self.store, now_ms=BASE + SLOW_PURGE_AGE_DAYS * 7 * DAY)
        row = self.store.get(memory_id, admin=True)
        self.assertEqual(row["status"], "purged")

        decay_memory(self.store, now_ms=BASE + (SLOW_PURGE_AGE_DAYS * 7 + SLOW_HARD_GRACE_DAYS) * DAY)
        with self.assertRaises(NotFoundError):
            self.store.get(memory_id, admin=True)

    def test_pinned_never_dies(self) -> None:
        result = self.store.save(
            {
                "content": "这是一条主人钉死的身份记忆。",
                "category": "identity",
                "pinned": True,
                "source": self.group,
            },
            now_ms=BASE,
        )
        decay_memory(self.store, now_ms=BASE + 3650 * DAY)
        row = self.store.get(result["memory_id"], admin=True)
        self.assertEqual(row["status"], "active")
        self.assertEqual(row["compression_level"], 0)
        self.assertEqual(row["weight"], result["weight"])

    def test_media_impression_does_not_cross_platform(self) -> None:
        result = self._save("用户看到一个跨平台测试视频。", self.video)
        same = self.store.search("跨平台测试视频", self.video2, now_ms=BASE)
        self.assertEqual(same["count"], 1)
        other = self.store.search("跨平台测试视频", self.group, now_ms=BASE)
        self.assertEqual(other["count"], 0)
        self.assertEqual(result["category"], "media_impression")

    def test_upgrade_media_impression_to_experience(self) -> None:
        result = self._save("用户反复提到这个视频，值得长期记住。" * 5, self.video)
        memory_id = result["memory_id"]
        decay_memory(self.store, now_ms=BASE + 2 * DAY)
        upgraded = self.store.upgrade_memory(
            memory_id, "experience", source=self.video, now_ms=BASE + 2 * DAY
        )
        self.assertEqual(upgraded["category"], "experience")
        self.assertEqual(upgraded["half_life_days"], 90)
        row = self.store.get(memory_id, admin=True)
        self.assertEqual(row["compression_level"], 0)
        self.assertIsNone(row["summary"])
        self.assertEqual(row["status"], "active")

    def test_fast_lane_auto_promotes_on_repeated_mentions(self) -> None:
        result = self._save("用户反复提到这个视频，应该巩固。" * 4, self.video)
        memory_id = result["memory_id"]
        for i in range(3):
            self.store.mention(memory_id, self.video, now_ms=BASE + i * 1000)
        run = decay_memory(self.store, now_ms=BASE + DAY)
        self.assertEqual(run["promoted_fast_lane"], 1)
        row = self.store.get(memory_id, admin=True)
        self.assertEqual(row["category"], "experience")
        self.assertEqual(row["half_life_days"], 90)
        self.assertEqual(row["compression_level"], 0)
        self.assertEqual(row["status"], "active")

    def test_compress_memory_mock(self) -> None:
        result = self._save("这是需要被压缩成摘要的一段很长的内容。" * 10, self.video)
        compressed = compress_memory(
            self.store, result["memory_id"], max_chars=50,
            level=1, now_ms=BASE,
        )
        self.assertTrue(compressed["ok"])
        self.assertEqual(compressed["compression_level"], 1)
        self.assertLessEqual(len(compressed["summary"]), 50)
        row = self.store.get(result["memory_id"], admin=True)
        self.assertEqual(row["compression_level"], 1)
        self.assertEqual(row["content"], "这是需要被压缩成摘要的一段很长的内容。" * 10)

    def test_compress_memory_accepts_custom_compressor(self) -> None:
        result = self._save("需要自定义压缩的内容。" * 5, self.video)
        class UpperCompressor:
            def compress(self, text: str, *, max_chars: int = 50, level: int = 1) -> str:
                return str(text).upper()[:max_chars]
        compressed = compress_memory(
            self.store, result["memory_id"], max_chars=20,
            compressor=UpperCompressor(), now_ms=BASE,
        )
        self.assertTrue(compressed["summary"].startswith("需要自定义压缩"))
        self.assertEqual(len(compressed["summary"]), 20)

    def test_search_limit_and_text_budget(self) -> None:
        for i in range(10):
            self.store.save(
                {
                    "content": f"预算测试{i} " + ("很长的记忆内容" * 60),
                    "category": "other",
                    "source": self.group,
                    "context_scope": "share_all",
                    "visibility": "global",
                },
                now_ms=BASE,
            )
        result = self.store.search("预算测试", self.group, limit=50, now_ms=BASE)
        self.assertLessEqual(result["count"], 8)
        self.assertLessEqual(sum(len(item["text"] or "") for item in result["results"]), 1200)


if __name__ == "__main__":
    unittest.main()
