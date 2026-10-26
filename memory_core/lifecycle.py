"""Memory lifecycle: C-scheme decay, compression, and cleanup.

This module deliberately uses a mock compressor (first N characters) so the
whole lifecycle can be tested without an LLM. Replace `_mock_compress` later
with a real summarizer; the state transitions stay the same.

Slow lane:
  level 1 at effective_weight < 0.35 and age >= 45 days
  level 2 at effective_weight < 0.15 and age >= 120 days
  soft purge at effective_weight < 0.08, age >= 60 days, mentions <= 2
  hard delete 30 days after soft purge

Fast lane (`media_impression`):
  level 1 at age >= 1 day
  level 2 at age >= 7 days
  soft purge at age >= 14 days and mentions <= 1
  hard delete 7 days after soft purge
"""

from __future__ import annotations

import time
from typing import Any

from .store import HALF_LIFE_DAYS, MemoryStore, effective_weight

DAY_MS = 86_400_000

PROTECTED_FROM_HARD_DELETE = {"identity", "relationship", "promise"}
SLOW_LEVEL1_EFF = 0.35
SLOW_LEVEL1_AGE_DAYS = 45
SLOW_LEVEL2_EFF = 0.15
SLOW_LEVEL2_AGE_DAYS = 120
SLOW_PURGE_EFF = 0.08
SLOW_PURGE_AGE_DAYS = 60
SLOW_HARD_GRACE_DAYS = 30

FAST_LEVEL1_AGE_DAYS = 1
FAST_LEVEL2_AGE_DAYS = 7
FAST_PURGE_AGE_DAYS = 14
FAST_HARD_GRACE_DAYS = 7
FAST_PROMOTE_MENTIONS = 3
FAST_PROMOTE_TARGET = "experience"

MOCK_LEVEL1_CHARS = 50
MOCK_LEVEL2_CHARS = 24


def _now_ms() -> int:
    return int(time.time() * 1000)


def _mock_compress(text: str, max_chars: int) -> str:
    """LLM placeholder: keep the first N characters."""
    text = " ".join(str(text or "").split())
    return text[:max(1, int(max_chars))]


def _audit(store: MemoryStore, action: str, memory_id: int,
           detail: dict[str, Any], now: int) -> None:
    store._audit(  # noqa: SLF001 - lifecycle is part of the same package
        store.conn, action, memory_id, None, detail
    )


def _compress_row(store: MemoryStore, row: Any, level: int,
                  now: int) -> str:
    max_chars = MOCK_LEVEL1_CHARS if level <= 1 else MOCK_LEVEL2_CHARS
    summary = _mock_compress(row["content"], max_chars)
    store.conn.execute(
        """
        UPDATE memories
           SET summary = ?, compression_level = ?, last_compressed_at = ?,
               updated_at = ?
         WHERE id = ?
        """,
        (summary, level, now, now, row["id"]),
    )
    _audit(store, "compress", int(row["id"]), {"level": level}, now)
    return summary


def compress_memory(store: MemoryStore, memory_id: int,
                    max_chars: int = MOCK_LEVEL1_CHARS,
                    level: int | None = None,
                    now_ms: int | None = None,
                    force: bool = False) -> dict[str, Any]:
    """Compress one memory with the mock summarizer.

    Pinned memories are skipped unless force=True. content is retained; only
    summary/compression_level change, so this is reversible while the row exists.
    """
    now = int(now_ms) if now_ms is not None else _now_ms()
    with store.lock, store.conn:
        row = store.conn.execute(
            "SELECT * FROM memories WHERE id = ?", (int(memory_id),)
        ).fetchone()
        if not row:
            raise ValueError(f"memory {memory_id} not found")
        if row["pinned"] and not force:
            return {"ok": True, "skipped": "pinned", "memory_id": int(memory_id)}
        final_level = int(level if level is not None else row["compression_level"])
        summary = " ".join(str(row["content"] or "").split())[:max(1, int(max_chars))]
        store.conn.execute(
            """
            UPDATE memories
               SET summary = ?, compression_level = ?, last_compressed_at = ?,
                   updated_at = ?
             WHERE id = ?
            """,
            (summary, final_level, now, now, int(memory_id)),
        )
        _audit(store, "compress", int(memory_id),
               {"level": final_level, "mock_chars": max_chars}, now)
        return {
            "ok": True,
            "memory_id": int(memory_id),
            "compression_level": final_level,
            "summary": summary,
            "summary_chars": len(summary),
        }


def _has_owner_shared_tag(store: MemoryStore, memory_id: int) -> bool:
    return bool(store.conn.execute(
        "SELECT 1 FROM memory_tags WHERE memory_id = ? AND tag = 'owner-shared'",
        (int(memory_id),),
    ).fetchone())


def _promote_fast_lane(store: MemoryStore, row: Any, now: int,
                       reason: str) -> None:
    target = FAST_PROMOTE_TARGET
    half_life = float(HALF_LIFE_DAYS[target])
    store.conn.execute(
        """
        UPDATE memories
           SET category = ?, half_life_days = ?, compression_level = 0,
               summary = NULL, status = 'active', purged_at = NULL,
               updated_at = ?
         WHERE id = ?
        """,
        (target, half_life, now, int(row["id"])),
    )
    _audit(store, "promote", int(row["id"]), {
        "from": "media_impression",
        "to": target,
        "reason": reason,
        "automatic": True,
    }, now)


def _soft_purge(store: MemoryStore, row: Any, now: int) -> None:
    store.conn.execute(
        """
        UPDATE memories
           SET status = 'purged', purged_at = ?, updated_at = ?
         WHERE id = ?
        """,
        (now, now, row["id"]),
    )
    _audit(store, "soft_purge", int(row["id"]),
           {"category": row["category"], "mention_count": row["mention_count"]}, now)


def _hard_delete(store: MemoryStore, row: Any, now: int) -> None:
    _audit(store, "hard_purge", int(row["id"]),
           {"category": row["category"], "purged_at": row["purged_at"]}, now)
    store.conn.execute("DELETE FROM memories WHERE id = ?", (int(row["id"]),))


def decay_memory(store: MemoryStore, now_ms: int | None = None,
                 dry_run: bool = False) -> dict[str, Any]:
    """Run one lifecycle pass.

    - materialize decay for active, non-pinned rows
    - apply slow-lane or fast-lane compression
    - soft purge edge rows
    - hard delete rows whose grace period has expired

    pinned rows are never modified. identity/relationship/promise rows may
    compress but are never soft/hard purged.
    """
    now = int(now_ms) if now_ms is not None else _now_ms()
    result = {
        "ok": True,
        "now": now,
        "dry_run": dry_run,
        "scanned": 0,
        "decayed": 0,
        "compressed_level1": 0,
        "compressed_level2": 0,
        "promoted_fast_lane": 0,
        "soft_purged": 0,
        "hard_deleted": 0,
        "skipped_pinned": 0,
        "skipped_protected": 0,
        "actions": [],
    }

    with store.lock, store.conn:
        rows = store.conn.execute(
            "SELECT * FROM memories ORDER BY id"
        ).fetchall()

        for row in rows:
            result["scanned"] += 1
            memory_id = int(row["id"])

            if row["pinned"]:
                result["skipped_pinned"] += 1
                continue

            # Tombstones: hard delete after the grace period.
            if row["status"] == "purged":
                if row["category"] in PROTECTED_FROM_HARD_DELETE:
                    result["skipped_protected"] += 1
                    continue
                grace = (FAST_HARD_GRACE_DAYS if row["category"] == "media_impression"
                         else SLOW_HARD_GRACE_DAYS) * DAY_MS
                if int(row["purged_at"] or now) + grace <= now:
                    result["hard_deleted"] += 1
                    result["actions"].append({"id": memory_id, "action": "hard_delete"})
                    if not dry_run:
                        _hard_delete(store, row, now)
                continue

            if row["status"] != "active":
                continue

            age_days = max(0.0, (now - int(row["last_mentioned_at"])) / DAY_MS)
            eff = effective_weight(
                float(row["weight"]), row["last_decay_at"], row["half_life_days"], now
            )

            if not dry_run:
                store.conn.execute(
                    "UPDATE memories SET weight = ?, last_decay_at = ? WHERE id = ?",
                    (eff, now, memory_id),
                )
                result["decayed"] += 1

            protected = row["category"] in PROTECTED_FROM_HARD_DELETE
            if protected:
                result["skipped_protected"] += 1

            if row["category"] == "media_impression":
                owner_shared = _has_owner_shared_tag(store, memory_id)
                if owner_shared or int(row["mention_count"]) >= FAST_PROMOTE_MENTIONS:
                    result["promoted_fast_lane"] += 1
                    result["actions"].append({"id": memory_id, "action": "promote"})
                    if not dry_run:
                        _promote_fast_lane(
                            store, row, now,
                            "owner_shared" if owner_shared else "repeated_mentions",
                        )
                    continue
                if age_days >= FAST_PURGE_AGE_DAYS and int(row["mention_count"]) <= 1:
                    result["soft_purged"] += 1
                    result["actions"].append({"id": memory_id, "action": "soft_purge"})
                    if not dry_run:
                        _soft_purge(store, row, now)
                    continue
                if age_days >= FAST_LEVEL2_AGE_DAYS and int(row["compression_level"]) < 2:
                    result["compressed_level2"] += 1
                    result["actions"].append({"id": memory_id, "action": "compress_level2"})
                    if not dry_run:
                        _compress_row(store, row, 2, now)
                    continue
                if age_days >= FAST_LEVEL1_AGE_DAYS and int(row["compression_level"]) < 1:
                    result["compressed_level1"] += 1
                    result["actions"].append({"id": memory_id, "action": "compress_level1"})
                    if not dry_run:
                        _compress_row(store, row, 1, now)
                continue

            # Slow lane.
            if (not protected
                    and eff < SLOW_PURGE_EFF
                    and age_days >= SLOW_PURGE_AGE_DAYS
                    and int(row["mention_count"]) <= 2):
                result["soft_purged"] += 1
                result["actions"].append({"id": memory_id, "action": "soft_purge"})
                if not dry_run:
                    _soft_purge(store, row, now)
                continue

            # One clarity step per pass. A very old level-0 memory first becomes
            # level 1; the next pass can take it to level 2.
            if int(row["compression_level"]) < 1:
                if eff < SLOW_LEVEL1_EFF and age_days >= SLOW_LEVEL1_AGE_DAYS:
                    result["compressed_level1"] += 1
                    result["actions"].append({"id": memory_id, "action": "compress_level1"})
                    if not dry_run:
                        _compress_row(store, row, 1, now)
            elif int(row["compression_level"]) < 2:
                if eff < SLOW_LEVEL2_EFF and age_days >= SLOW_LEVEL2_AGE_DAYS:
                    result["compressed_level2"] += 1
                    result["actions"].append({"id": memory_id, "action": "compress_level2"})
                    if not dry_run:
                        _compress_row(store, row, 2, now)

        if dry_run:
            store.conn.rollback()
    return result
