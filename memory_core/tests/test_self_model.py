from __future__ import annotations

import unittest

from memory_core.self_model import SelfModelService
from memory_core.store import MemoryStore


class SelfModelTest(unittest.TestCase):
    def setUp(self) -> None:
        self.store = MemoryStore(":memory:", owner_id="10001")
        self.service = SelfModelService(self.store)
        # Seed one trait so a later +0.1 change is a minor update.
        self.service._apply_version(
            {"traits": {"嘴硬心软": 0.8}, "values": [], "tone": {}, "relationships": {}},
            [], 1.0, "seed", 1_760_000_000_000,
        )

    def tearDown(self) -> None:
        self.store.close()

    def test_minor_trait_change_applies_automatically(self) -> None:
        result = self.service.submit(
            {"traits": {"嘴硬心软": 0.9}, "values": [], "tone": {}, "relationships": {}},
            reason="small drift",
        )
        self.assertTrue(result["applied"])
        self.assertEqual(result["severity"], "minor")
        self.assertEqual(self.service.current()["version"], 2)
        self.assertEqual(self.service.current()["traits"]["嘴硬心软"], 0.9)

    def test_partial_update_keeps_omitted_fields(self) -> None:
        self.service._apply_version(
            {"traits": {"嘴硬心软": 0.8}, "values": ["不装客服"], "tone": {}, "relationships": {}},
            [], 1.0, "seed values", 1_760_000_000_001,
        )
        self.service.submit({"traits": {"嘴硬心软": 0.9}}, reason="small drift")
        current = self.service.current()
        self.assertEqual(current["traits"]["嘴硬心软"], 0.9)
        self.assertEqual(current["values"], ["不装客服"])

    def test_major_trait_change_needs_owner(self) -> None:
        result = self.service.submit(
            {"traits": {"嘴硬心软": 0.4}, "values": [], "tone": {}, "relationships": {}},
            reason="large drift",
        )
        self.assertFalse(result["applied"])
        self.assertEqual(result["severity"], "major")
        self.assertEqual(self.service.current()["version"], 1)
        self.assertEqual(len(self.service.list_proposals()), 1)
        approved = self.service.decide(result["proposal_id"], "approve")
        self.assertEqual(approved["version"], 2)
        self.assertEqual(self.service.current()["traits"]["嘴硬心软"], 0.4)

    def test_guardrails_cannot_be_changed(self) -> None:
        with self.assertRaises(ValueError):
            self.service.submit({"guardrails": {}, "traits": {"嘴硬心软": 0.8}})

    def test_rejected_proposal_does_not_change_state(self) -> None:
        result = self.service.submit(
            {"values": ["变一个人"], "traits": {"嘴硬心软": 0.8}},
            reason="too large",
        )
        self.service.decide(result["proposal_id"], "reject")
        self.assertEqual(self.service.current()["version"], 1)
        self.assertEqual(self.service.current()["values"], [])


if __name__ == "__main__":
    unittest.main()
