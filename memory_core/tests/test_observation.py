from __future__ import annotations

import unittest

from memory_core.lifecycle import decay_memory
from memory_core.observer import MockObserver, Observation
from memory_core.store import MemoryStore

DAY = 86_400_000
BASE = 1_760_000_000_000


class ObservationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.store = MemoryStore(":memory:", owner_id="10001", owner_read_all=True)
        self.group = {"platform": "qq", "context_type": "group", "context_id": "2001"}
        self.other = {"platform": "qq", "context_type": "group", "context_id": "2002"}
        self.owner = {"platform": "qq", "context_type": "private", "context_id": "10001"}
        self.messages = [
            {"seq": 1, "user_id": "10002", "text": "今晚聊只狼和炉石"},
            {"seq": 2, "user_id": "10003", "text": "只狼太难了"},
        ]

    def tearDown(self) -> None:
        self.store.close()

    def test_episode_is_origin_scoped_and_owner_visible(self) -> None:
        result = self.store.record_observation(
            self.group, self.messages, MockObserver().observe(self.messages),
            now_ms=BASE,
        )
        self.assertFalse(result["duplicate"])
        self.assertEqual(result["memory_ids"], [1])
        row = self.store.get(1, admin=True)
        self.assertEqual(row["category"], "episode")
        self.assertEqual(row["half_life_days"], 30)
        self.assertEqual(row["visibility"], "origin")
        self.assertEqual(self.store.search("只狼", self.group)["count"], 1)
        self.assertEqual(self.store.search("只狼", self.other)["count"], 0)
        self.assertEqual(self.store.search("只狼", self.owner)["count"], 1)

    def test_duplicate_window_is_idempotent(self) -> None:
        obs = MockObserver().observe(self.messages)
        first = self.store.record_observation(self.group, self.messages, obs, now_ms=BASE)
        second = self.store.record_observation(self.group, self.messages, obs, now_ms=BASE + 1)
        self.assertTrue(second["duplicate"])
        self.assertEqual(second["window_id"], first["window_id"])
        self.assertEqual(self.store.stats()["total"], 1)

    def test_impressions_become_opinion_and_emotion(self) -> None:
        observation = Observation(
            episode="群里聊到配配在玩梗。",
            topics=("配配", "玩梗"),
            impressions=(
                {"about": "配配", "stance": "他在玩梗，不是真生气", "intensity": 0.6},
                {"about": "刷屏", "emotion": "有点烦", "intensity": 0.7},
            ),
        )
        result = self.store.record_observation(
            self.group, self.messages, observation, now_ms=BASE,
        )
        self.assertEqual(len(result["memory_ids"]), 3)
        categories = {self.store.get(mid, admin=True)["category"] for mid in result["memory_ids"]}
        self.assertEqual(categories, {"episode", "opinion", "emotion"})

    def test_episode_enters_slow_lifecycle(self) -> None:
        self.store.record_observation(
            self.group, self.messages, MockObserver().observe(self.messages),
            now_ms=BASE,
        )
        ahead = decay_memory(self.store, now_ms=BASE + 200 * DAY)
        self.assertEqual(ahead["scanned"], 1)
        self.assertGreaterEqual(ahead["compressed_level1"] + ahead["soft_purged"], 1)


if __name__ == "__main__":
    unittest.main()
