from __future__ import annotations

import unittest

from memory_core.store import (
    ApprovalPendingError,
    ConflictError,
    MemoryStore,
)


class MemoryCoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.store = MemoryStore(":memory:", owner_id="10001")
        self.group_a = {"platform": "qq", "context_type": "group", "context_id": "1001", "context_name": "A群"}
        self.group_b = {"platform": "qq", "context_type": "group", "context_id": "1002", "context_name": "B群"}
        self.owner = {"platform": "qq", "context_type": "private", "context_id": "10001"}
        self.other_private = {"platform": "qq", "context_type": "private", "context_id": "9999"}
        self.game = {"platform": "local_game", "context_type": "game_session", "context_id": "elden-ring-ng+"}
        self.game_other = {"platform": "local_game", "context_type": "game_session", "context_id": "sekiro-ng+"}
        self.video = {"platform": "bilibili", "context_type": "video", "context_id": "BV1test"}

    def tearDown(self) -> None:
        self.store.close()

    def _save(self, content: str, source: dict, **extra):
        payload = {"content": content, "category": "preference", "source": source}
        payload.update(extra)
        return self.store.save(payload)

    def test_normal_save_cross_group_short_query_and_stats(self) -> None:
        result = self._save(
            "用户喜欢在深夜聊天。",
            self.group_a,
            subject="用户",
            predicate="喜欢",
            object="深夜聊天",
            tags=["作息"],
            context_scope="share_all",
            visibility="global",
        )
        self.assertFalse(result["approval_required"])
        self.assertEqual(result["approval_status"], "approved")
        self.assertEqual(result["context_scope"], "share_all")

        hits = self.store.search("深夜", self.group_b)
        self.assertEqual(hits["count"], 1)
        self.assertEqual(hits["results"][0]["source_hint"], "其他会话")
        self.assertEqual(self.store.search("深夜", self.group_a)["count"], 1)

        merged = self._save(
            "用户喜欢在深夜聊天。",
            self.group_b,
            subject="用户",
            predicate="喜欢",
            object="深夜聊天",
        )
        self.assertTrue(merged["merged"])
        self.assertEqual(merged["memory_id"], result["memory_id"])
        detail = self.store.get(result["memory_id"], admin=True)
        self.assertEqual(len(detail["sources"]), 2)
        self.assertEqual(detail["sources"][0]["platform"], "qq")

        stats = self.store.stats()
        self.assertEqual(stats["total"], 1)
        self.assertEqual(stats["by_sensitivity"]["normal"], 1)

    def test_mention_increases_weight(self) -> None:
        result = self._save("用户喜欢喝乌龙茶。", self.group_a)
        first = self.store.get(result["memory_id"], admin=True)["weight"]
        touched = self.store.mention(result["memory_id"], self.group_a, message_id="m2")
        self.assertGreater(touched["weight"], first)
        self.assertEqual(touched["mention_count"], 2)

    def test_sensitive_requires_owner_approval(self) -> None:
        result = self._save(
            "用户住在某个具体地址。",
            self.group_a,
            category="identity",
            sensitivity="sensitive",
            context_scope="share_all",
            visibility="global",
        )
        self.assertTrue(result["approval_required"])
        self.assertEqual(result["approval_status"], "pending")
        self.assertIsNotNone(result["approval_id"])

        self.assertEqual(self.store.search("具体地址", self.group_a)["count"], 0)
        self.assertEqual(self.store.search("具体地址", self.group_b)["count"], 0)
        pending = self.store.pending_approvals()
        self.assertEqual(pending["count"], 1)
        self.assertEqual(pending["items"][0]["requested_context_scope"], "share_all")

        with self.assertRaises(ApprovalPendingError):
            self.store.get(result["memory_id"], source=self.group_a)
        with self.assertRaises(ApprovalPendingError):
            self.store.mention(result["memory_id"], self.group_a)

        decided = self.store.decide_approval(
            result["approval_id"],
            "approve",
            visibility="global",
            context_scope="share_all",
            reason="ok",
        )
        self.assertEqual(decided["status"], "approved")
        self.assertEqual(decided["context_scope"], "share_all")
        self.assertEqual(self.store.search("具体地址", self.group_b)["count"], 1)
        with self.assertRaises(ConflictError):
            self.store.decide_approval(result["approval_id"], "approve")

    def test_sensitive_origin_stays_local(self) -> None:
        result = self._save(
            "用户在某群说过一个敏感偏好。",
            self.group_a,
            sensitivity="sensitive",
            context_scope="share_same_platform",
            visibility="origin",
        )
        self.store.decide_approval(
            result["approval_id"], "approve",
            visibility="origin", context_scope="share_same_platform",
        )
        self.assertEqual(self.store.search("敏感偏好", self.group_a)["count"], 1)
        self.assertEqual(self.store.search("敏感偏好", self.group_b)["count"], 0)

    def test_secret_stays_private(self) -> None:
        result = self._save(
            "这是绝对不能外传的秘密。",
            self.group_a,
            category="identity",
            sensitivity="secret",
        )
        self.store.decide_approval(
            result["approval_id"], "approve",
            visibility="global", context_scope="share_all",
        )
        self.assertEqual(self.store.search("秘密", self.group_a)["count"], 0)
        self.assertEqual(self.store.search("秘密", self.owner)["count"], 1)
        self.assertEqual(self.store.search("秘密", self.other_private)["count"], 0)
        detail = self.store.get(result["memory_id"], admin=True)
        self.assertEqual(detail["visibility"], "private")
        self.assertEqual(detail["context_scope"], "share_owner_only")

    def test_rejected_never_searchable(self) -> None:
        result = self._save(
            "一条待拒绝的敏感信息。",
            self.group_a,
            sensitivity="sensitive",
        )
        self.store.decide_approval(result["approval_id"], "reject", reason="no")
        self.assertEqual(self.store.search("待拒绝", self.group_a)["count"], 0)
        self.assertEqual(self.store.pending_approvals()["count"], 0)

    def test_private_visibility_is_owner_only(self) -> None:
        result = self._save("主人私下说的一句话。", self.owner, visibility="private")
        self.assertEqual(result["context_scope"], "share_owner_only")
        self.assertEqual(self.store.search("私下", self.owner)["count"], 1)
        self.assertEqual(self.store.search("私下", self.other_private)["count"], 0)
        self.assertEqual(self.store.search("私下", self.group_a)["count"], 0)

    def test_normal_memory_upgrades_to_pending_when_sensitive(self) -> None:
        first = self._save(
            "用户有一个特殊习惯。",
            self.group_a,
            context_scope="share_all",
            visibility="global",
        )
        self.assertEqual(first["approval_status"], "approved")
        upgraded = self._save(
            "用户有一个特殊习惯。", self.group_b,
            sensitivity="sensitive",
            context_scope="share_all",
            visibility="global",
        )
        self.assertTrue(upgraded["approval_required"])
        self.assertEqual(upgraded["approval_status"], "pending")
        self.assertEqual(self.store.search("特殊习惯", self.group_a)["count"], 0)
        self.assertEqual(self.store.pending_approvals()["count"], 1)

    def test_cross_platform_context_scope(self) -> None:
        # Same platform, global within platform: another game session can see it.
        game = self._save(
            "用户在游戏里卡在女武神。",
            self.game,
            category="game_progress",
            context_scope="share_same_platform",
            visibility="global",
        )
        self.assertEqual(game["context_scope"], "share_same_platform")
        self.assertEqual(self.store.search("女武神", self.game_other)["count"], 1)
        self.assertEqual(self.store.search("女武神", self.group_a)["count"], 0)
        self.assertEqual(self.store.search("女武神", self.video)["count"], 0)

        # Same platform but origin visibility: only the exact source context.
        exact = self._save(
            "用户在这个存档里选了法师。",
            self.game,
            category="game_progress",
            context_scope="share_same_platform",
            visibility="origin",
        )
        self.assertEqual(self.store.search("法师", self.game)["count"], 1)
        self.assertEqual(self.store.search("法师", self.game_other)["count"], 0)

        # share_all crosses platforms.
        video = self._save(
            "用户看到一个很好笑的视频。",
            self.video,
            category="media",
            context_scope="share_all",
            visibility="global",
        )
        self.assertEqual(video["context_scope"], "share_all")
        self.assertEqual(self.store.search("好笑", self.group_a)["count"], 1)

        # share_owner_only never reaches groups.
        self._save(
            "主人私下说的游戏计划。",
            self.owner,
            category="game_progress",
            context_scope="share_owner_only",
            visibility="private",
        )
        self.assertEqual(self.store.search("游戏计划", self.owner)["count"], 1)
        self.assertEqual(self.store.search("游戏计划", self.group_a)["count"], 0)
        self.assertEqual(self.store.search("游戏计划", self.video)["count"], 0)

    def test_relaxed_query_matches_natural_question(self) -> None:
        self._save(
            "用户今晚吃的是菌子火锅。",
            self.group_a,
            category="experience",
            context_scope="share_all",
            visibility="global",
        )
        result = self.store.search(
            "我今晚吃了火锅，你记得是什么火锅吗",
            self.group_b,
        )
        self.assertEqual(result["count"], 1)
        self.assertTrue(result["relaxed"])
        self.assertEqual(result["results"][0]["category"], "experience")


    def test_owner_private_reads_all_scopes(self) -> None:
        store = MemoryStore(":memory:", owner_id="10001", owner_read_all=True)
        try:
            source = {"platform": "qq", "context_type": "group", "context_id": "3001"}
            owner_source = {"platform": "qq", "context_type": "private", "context_id": "10001"}
            other_group = {"platform": "qq", "context_type": "group", "context_id": "3002"}
            store.save({
                "content": "只在原群可见的记忆。",
                "category": "preference",
                "context_scope": "share_same_platform",
                "visibility": "origin",
                "source": source,
            })
            self.assertEqual(store.search("原群可见", owner_source)["count"], 1)
            self.assertEqual(store.search("原群可见", other_group)["count"], 0)
        finally:
            store.close()

if __name__ == "__main__":
    unittest.main()
