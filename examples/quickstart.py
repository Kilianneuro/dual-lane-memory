"""Minimal end-to-end demo for Dual-Lane Memory Core.

Run from the repository root:

    python examples/quickstart.py

Everything is in-memory; no database, token, or network is created.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from memory_core.lifecycle import decay_memory  # noqa: E402
from memory_core.store import MemoryStore  # noqa: E402

DAY = 86_400_000
BASE = 1_760_000_000_000


def main() -> int:
    store = MemoryStore(":memory:", owner_id="demo-owner")
    source = {"platform": "qq", "context_type": "group", "context_id": "demo-group"}

    try:
        saved = store.save({
            "content": "用户现在在玩只狼，卡在苇名弦一郎。",
            "category": "game_progress",
            "context_scope": "share_same_platform",
            "visibility": "origin",
            "source": source,
        }, now_ms=BASE)
        print("SAVE")
        print(json.dumps(saved, ensure_ascii=False, indent=2))

        found = store.search("只狼", source, now_ms=BASE)
        print("\nSEARCH")
        print(json.dumps(found, ensure_ascii=False, indent=2))

        report = decay_memory(store, now_ms=BASE + 400 * DAY)
        print("\nLIFECYCLE after 400 simulated days")
        print(json.dumps(report, ensure_ascii=False, indent=2))

        print("\nSTATS")
        print(json.dumps(store.stats(), ensure_ascii=False, indent=2))
    finally:
        store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
