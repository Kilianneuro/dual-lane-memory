"""Cross-conversation memory store.

Phase 2 scope:
  - save / merge / search / mention
  - source provenance across groups and private chats
  - owner approval for sensitive/secret facts
  - HTTP service lives in server.py

Deliberately NOT implemented yet:
  - time decay, LLM compression, edge-information cleanup (lifecycle phase).
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sqlite3
import threading
import time
from functools import wraps
from typing import Any

from .db import MemoryDB
from .text import make_fts_query, make_search_text, normalize_key, normalize_tag

CATEGORY_PRIOR = {
    "identity": 0.90,
    "preference": 0.70,
    "relationship": 0.70,
    "promise": 0.65,
    "task": 0.55,
    "opinion": 0.50,
    "event": 0.45,
    "gossip": 0.25,
    "emotion": 0.55,
    "experience": 0.60,
    "game_progress": 0.60,
    "media": 0.35,
    "media_impression": 0.25,
    "other": 0.40,
}

HALF_LIFE_DAYS = {
    "identity": 365,
    "preference": 180,
    "relationship": 180,
    "promise": 90,
    "opinion": 60,
    "event": 30,
    "task": 14,
    "gossip": 14,
    "emotion": 14,
    "experience": 90,
    "game_progress": 120,
    "media": 45,
    "media_impression": 7,
    "other": 60,
}

SENSITIVITY_RANK = {"normal": 0, "sensitive": 1, "secret": 2}
VISIBILITIES = {"global", "origin", "private"}
CONTEXT_SCOPES = {"share_all", "share_owner_only", "share_same_platform"}


class MemoryError(Exception):
    def __init__(self, message: str, code: str = "BAD_REQUEST", status: int = 400):
        super().__init__(message)
        self.code = code
        self.status = status


class NotFoundError(MemoryError):
    def __init__(self, message: str = "memory not found"):
        super().__init__(message, "NOT_FOUND", 404)


class ForbiddenError(MemoryError):
    def __init__(self, message: str = "forbidden"):
        super().__init__(message, "FORBIDDEN", 403)


class ConflictError(MemoryError):
    def __init__(self, message: str, code: str = "CONFLICT"):
        super().__init__(message, code, 409)


class ApprovalPendingError(MemoryError):
    def __init__(self, message: str = "memory is waiting for owner approval"):
        super().__init__(message, "APPROVAL_PENDING", 409)


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def effective_weight(weight: float, last_decay_at: int | None, half_life_days: float,
                     now_ms: int) -> float:
    """0..1 strength after time decay since the last materialization."""
    if not last_decay_at:
        return _clamp(float(weight))
    age_days = max(0.0, (int(now_ms) - int(last_decay_at)) / 86_400_000.0)
    half_life = max(0.01, float(half_life_days or 1.0))
    return _clamp(float(weight) * (2.0 ** (-age_days / half_life)))


def _now_ms() -> int:
    return int(time.time() * 1000)


def _as_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _locked(method):
    """Serialize access to the single sqlite3 connection."""
    @wraps(method)
    def wrapper(self, *args, **kwargs):
        with self.lock:
            return method(self, *args, **kwargs)
    return wrapper


class MemoryStore:
    def __init__(self, db_path: str, owner_id: str = "", owner_read_all: bool = False):
        self.db = MemoryDB(db_path)
        self.conn = self.db.conn
        self.owner_id = str(owner_id or "").strip()
        self.owner_read_all = bool(owner_read_all)
        self.lock = threading.RLock()

    def close(self) -> None:
        self.db.close()

    # ------------------------------------------------------------------
    # Validation / normalization
    # ------------------------------------------------------------------
    def _validate_source(self, source: Any) -> dict[str, str]:
        if not isinstance(source, dict):
            raise MemoryError("source 必须是对象，包含 platform/context_type/context_id")

        platform = str(source.get("platform") or "").strip().lower()
        context_type = str(source.get("context_type") or source.get("kind") or "").strip().lower()
        context_id = str(source.get("context_id") or source.get("id") or "").strip()

        # Backward compatibility: v1 source {kind, id} means QQ.
        if not platform:
            platform = "qq"
            context_type = context_type or str(source.get("kind") or "").strip().lower()
            context_id = context_id or str(source.get("id") or "").strip()

        if not platform:
            raise MemoryError("source.platform 不能为空")
        if not context_type:
            raise MemoryError("source.context_type 不能为空（如 group/private/video/post/game_session）")
        if not context_id:
            raise MemoryError("source.context_id 不能为空")
        if platform == "qq" and context_type not in ("group", "private"):
            raise MemoryError("QQ 来源的 context_type 必须是 group 或 private")

        return {
            "platform": platform,
            "context_type": context_type,
            "context_id": context_id,
            "source_name": str(source.get("context_name") or source.get("name") or "").strip()[:120] or None,
            "source_url": str(source.get("context_url") or source.get("url") or "").strip()[:500] or None,
            "user_id": str(source.get("user_id") or "").strip()[:80] or None,
            "user_name": str(source.get("user_name") or "").strip()[:80] or None,
            "message_id": str(source.get("message_id") or "").strip()[:120] or None,
            "context_excerpt": str(source.get("context_excerpt") or "").strip()[:200] or None,
        }

    def _normalize_tags(self, raw: Any) -> list[str]:
        if raw is None:
            return []
        if isinstance(raw, str):
            raw = [raw]
        if not isinstance(raw, (list, tuple, set)):
            raise MemoryError("tags 必须是字符串数组")
        tags: list[str] = []
        for item in raw:
            tag = normalize_tag(item)
            if tag and tag not in tags:
                tags.append(tag)
            if len(tags) >= 20:
                break
        return tags

    def _dedupe_key(self, *, category: str, subject_key: str, predicate: str,
                    object_key: str, content: str) -> str:
        if subject_key or predicate or object_key:
            raw = f"{category}|{subject_key}|{predicate}|{object_key}"
        else:
            raw = f"{category}|{normalize_key(content)}"
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()

    def _initial_weight(self, category: str, importance: float, confidence: float) -> float:
        prior = CATEGORY_PRIOR.get(category, CATEGORY_PRIOR["other"])
        return _clamp(prior * (0.5 + 0.5 * importance) * (0.5 + 0.5 * confidence))

    def _normalize_payload(self, payload: Any) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise MemoryError("请求体必须是 JSON 对象")

        content = str(payload.get("content") or "").strip()
        if not content:
            raise MemoryError("content 不能为空")
        if len(content) > 4000:
            raise MemoryError("content 过长（上限 4000 字）")

        source = self._validate_source(payload.get("source"))
        category = str(payload.get("category") or "other").strip().lower()[:40] or "other"

        sensitivity = str(payload.get("sensitivity") or "normal").strip().lower()
        if sensitivity not in SENSITIVITY_RANK:
            raise MemoryError("sensitivity 必须是 normal / sensitive / secret")

        context_scope = str(payload.get("context_scope") or "share_same_platform").strip().lower()
        if context_scope not in CONTEXT_SCOPES:
            raise MemoryError("context_scope 必须是 share_all / share_owner_only / share_same_platform")

        visibility_raw = payload.get("visibility")
        visibility_explicit = visibility_raw is not None and str(visibility_raw).strip() != ""
        if visibility_explicit:
            visibility = str(visibility_raw).strip().lower()
        else:
            visibility = {
                "share_all": "global",
                "share_owner_only": "private",
                "share_same_platform": "origin",
            }[context_scope]
        if visibility not in VISIBILITIES:
            raise MemoryError("visibility 必须是 global / origin / private")

        # Keep the two gates consistent: owner-only means private; private
        # visibility is also owner-only regardless of the requested platform scope.
        if context_scope == "share_owner_only":
            visibility = "private"
        if visibility == "private":
            context_scope = "share_owner_only"

        # Sensitive facts are stored as origin/private until the owner approves.
        # The requested scopes are kept on the approval row so the owner can choose.
        requested_visibility = visibility
        requested_context_scope = context_scope
        if sensitivity == "secret":
            requested_visibility = "private"
            requested_context_scope = "share_owner_only"
            visibility = "private"
            context_scope = "share_owner_only"
        elif sensitivity == "sensitive":
            if visibility == "private":
                requested_visibility = "private"
                visibility = "private"
            elif visibility == "global":
                requested_visibility = "global"
                visibility = "origin"
            else:
                requested_visibility = "origin"
                visibility = "origin"
            requested_context_scope = context_scope

        subject = str(payload.get("subject") or "").strip() or None
        predicate = str(payload.get("predicate") or "").strip() or None
        obj = str(payload.get("object") or "").strip() or None
        subject_key = normalize_key(payload.get("subject_key") or subject or "")
        object_key = normalize_key(payload.get("object_key") or obj or "")
        predicate_key = normalize_key(predicate or "")

        summary = str(payload.get("summary") or "").strip() or None
        tags = self._normalize_tags(payload.get("tags"))

        importance = _clamp(float(payload.get("importance", 0.5)))
        confidence = _clamp(float(payload.get("confidence", 0.7)))

        dedupe_key = str(payload.get("dedupe_key") or "").strip()
        if dedupe_key:
            dedupe_key = dedupe_key[:128]
        else:
            dedupe_key = self._dedupe_key(
                category=category,
                subject_key=subject_key,
                predicate=predicate_key,
                object_key=object_key,
                content=content,
            )

        expires_at = _as_int(payload.get("expires_at"))
        meta = payload.get("meta")
        if meta is None:
            meta_json = "{}"
        elif isinstance(meta, str):
            meta_json = meta[:4000] if meta else "{}"
        else:
            try:
                meta_json = json.dumps(meta, ensure_ascii=False)[:4000]
            except (TypeError, ValueError):
                meta_json = "{}"

        pinned = 1 if payload.get("pinned") is True else 0

        approval_status = "approved" if sensitivity == "normal" else "pending"

        return {
            "content": content,
            "summary": summary,
            "source": source,
            "category": category,
            "category_explicit": "category" in payload,
            "sensitivity": sensitivity,
            "visibility": visibility,
            "visibility_explicit": visibility_explicit,
            "context_scope": context_scope,
            "context_scope_explicit": "context_scope" in payload,
            "requested_visibility": requested_visibility,
            "requested_context_scope": requested_context_scope,
            "subject": subject,
            "subject_key": subject_key or None,
            "predicate": predicate,
            "predicate_key": predicate_key or None,
            "object": obj,
            "object_key": object_key or None,
            "tags": tags,
            "importance": importance,
            "confidence": confidence,
            "dedupe_key": dedupe_key,
            "expires_at": expires_at,
            "meta_json": meta_json,
            "pinned": pinned,
            "approval_status": approval_status,
        }

    # ------------------------------------------------------------------
    # Small DB helpers
    # ------------------------------------------------------------------
    def _audit(self, conn: sqlite3.Connection, action: str, memory_id: int | None,
               source: dict[str, str] | None, detail: dict[str, Any]) -> None:
        conn.execute(
            "INSERT INTO memory_audit(action, memory_id, actor, platform, context_type, context_id, detail_json, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                action,
                memory_id,
                "memory_core",
                source.get("platform") if source else None,
                source.get("context_type") if source else None,
                source.get("context_id") if source else None,
                json.dumps(detail, ensure_ascii=False)[:4000],
                _now_ms(),
            ),
        )

    def _source_upsert(self, conn: sqlite3.Connection, memory_id: int,
                       source: dict[str, str], now: int) -> None:
        conn.execute(
            """
            INSERT INTO memory_sources(
              memory_id, platform, context_type, context_id,
              source_name, source_url, source_user_id, source_user_name,
              first_seen_at, last_seen_at, mention_count,
              last_message_id, context_excerpt
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
            ON CONFLICT(memory_id, platform, context_type, context_id) DO UPDATE SET
              source_name = coalesce(excluded.source_name, memory_sources.source_name),
              source_url = coalesce(excluded.source_url, memory_sources.source_url),
              source_user_id = coalesce(excluded.source_user_id, memory_sources.source_user_id),
              source_user_name = coalesce(excluded.source_user_name, memory_sources.source_user_name),
              last_seen_at = excluded.last_seen_at,
              mention_count = memory_sources.mention_count + 1,
              last_message_id = coalesce(excluded.last_message_id, memory_sources.last_message_id),
              context_excerpt = coalesce(excluded.context_excerpt, memory_sources.context_excerpt)
            """,
            (
                memory_id,
                source["platform"],
                source["context_type"],
                source["context_id"],
                source.get("source_name"),
                source.get("source_url"),
                source.get("user_id"),
                source.get("user_name"),
                now,
                now,
                source.get("message_id"),
                source.get("context_excerpt"),
            ),
        )

    def _refresh_distinct_sources(self, conn: sqlite3.Connection, memory_id: int) -> int:
        count = conn.execute(
            "SELECT COUNT(*) FROM memory_sources WHERE memory_id = ?", (memory_id,)
        ).fetchone()[0]
        conn.execute(
            "UPDATE memories SET distinct_source_count = ? WHERE id = ?",
            (count, memory_id),
        )
        return int(count)

    def _insert_mention(self, conn: sqlite3.Connection, memory_id: int,
                        source: dict[str, str], now: int, reason: str,
                        weight_before: float | None, weight_after: float | None) -> None:
        conn.execute(
            """
            INSERT INTO memory_mentions(
              memory_id, platform, context_type, context_id, message_id, user_id,
              mentioned_at, weight_before, weight_after, reason, context_excerpt
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                memory_id,
                source["platform"],
                source["context_type"],
                source["context_id"],
                source.get("message_id"),
                source.get("user_id"),
                now,
                weight_before,
                weight_after,
                reason,
                source.get("context_excerpt"),
            ),
        )

    def _create_approval(self, conn: sqlite3.Connection, memory_id: int,
                         requested_visibility: str, requested_context_scope: str,
                         now: int, reason: str = "sensitive_fact") -> int:
        existing = conn.execute(
            "SELECT id FROM memory_approvals WHERE memory_id = ? AND status = 'pending'",
            (memory_id,),
        ).fetchone()
        if existing:
            return int(existing["id"])
        cur = conn.execute(
            """
            INSERT INTO memory_approvals(
              memory_id, status, requested_at, reason,
              requested_visibility, requested_context_scope
            ) VALUES (?, 'pending', ?, ?, ?, ?)
            """,
            (memory_id, now, reason, requested_visibility, requested_context_scope),
        )
        return int(cur.lastrowid)

    def _apply_mention(self, conn: sqlite3.Connection, memory_id: int,
                       source: dict[str, str], now: int, reason: str = "mention") -> dict[str, Any]:
        row = conn.execute("SELECT * FROM memories WHERE id = ?", (memory_id,)).fetchone()
        if not row:
            raise NotFoundError()

        old_weight = effective_weight(
            float(row["weight"]), row["last_decay_at"], row["half_life_days"], now
        )
        mention_count = int(row["mention_count"])
        distinct_sources = int(row["distinct_source_count"])
        boost = min(
            0.30,
            0.10
            + 0.05 * math.log1p(mention_count)
            + 0.02 * distinct_sources,
        )
        new_weight = _clamp(old_weight + (1.0 - old_weight) * boost)

        conn.execute(
            """
            UPDATE memories
               SET weight = ?, mention_count = mention_count + 1,
                   last_mentioned_at = ?, last_decay_at = ?, updated_at = ?
             WHERE id = ?
            """,
            (new_weight, now, now, now, memory_id),
        )
        self._source_upsert(conn, memory_id, source, now)
        distinct = self._refresh_distinct_sources(conn, memory_id)
        self._insert_mention(conn, memory_id, source, now, reason, old_weight, new_weight)

        return {
            "memory_id": memory_id,
            "weight": round(new_weight, 6),
            "mention_count": mention_count + 1,
            "distinct_source_count": distinct,
            "approval_status": row["approval_status"],
            "visibility": row["visibility"],
            "context_scope": row["context_scope"],
        }

    # ------------------------------------------------------------------
    # Save / merge
    # ------------------------------------------------------------------
    def save(self, payload: Any, now_ms: int | None = None) -> dict[str, Any]:
        data = self._normalize_payload(payload)
        now = int(now_ms) if now_ms is not None else _now_ms()
        source = data["source"]

        with self.lock, self.conn:
            existing = self.conn.execute(
                "SELECT * FROM memories WHERE dedupe_key = ?", (data["dedupe_key"],)
            ).fetchone()

            if not existing:
                initial = self._initial_weight(
                    data["category"], data["importance"], data["confidence"]
                )
                half_life = HALF_LIFE_DAYS.get(data["category"], HALF_LIFE_DAYS["other"])
                search_text = make_search_text(
                    data["content"], data["summary"], data["subject"],
                    data["object"], " ".join(data["tags"]),
                )
                approved_at = now if data["approval_status"] == "approved" else None
                approved_by = "auto" if data["approval_status"] == "approved" else None

                cur = self.conn.execute(
                    """
                    INSERT INTO memories(
                      dedupe_key, category, subject, subject_key, predicate, object, object_key,
                      content, summary, search_text, importance, confidence,
                      base_weight, weight, half_life_days,
                      mention_count, distinct_source_count, retrieval_count,
                      first_seen_at, last_mentioned_at, last_retrieved_at,
                      last_decay_at, last_compressed_at, compression_level,
                      visibility, context_scope, sensitivity, approval_status, approved_at, approved_by,
                      pinned, status, expires_at, purged_at, created_at, updated_at, meta_json
                    ) VALUES (
                      ?, ?, ?, ?, ?, ?, ?,
                      ?, ?, ?, ?, ?,
                      ?, ?, ?,
                      1, 1, 0,
                      ?, ?, NULL,
                      ?, NULL, 0,
                      ?, ?, ?, ?, ?, ?,
                      ?, 'active', ?, NULL, ?, ?, ?
                    )
                    """,
                    (
                        data["dedupe_key"], data["category"], data["subject"], data["subject_key"],
                        data["predicate"], data["object"], data["object_key"],
                        data["content"], data["summary"], search_text,
                        data["importance"], data["confidence"],
                        initial, initial, half_life,
                        now, now,
                        now,
                        data["visibility"], data["context_scope"], data["sensitivity"],
                        data["approval_status"], approved_at, approved_by,
                        data["pinned"], data["expires_at"], now, now, data["meta_json"],
                    ),
                )
                memory_id = int(cur.lastrowid)

                for tag in data["tags"]:
                    self.conn.execute(
                        "INSERT OR IGNORE INTO memory_tags(memory_id, tag) VALUES (?, ?)",
                        (memory_id, tag),
                    )
                self._source_upsert(self.conn, memory_id, source, now)
                self._refresh_distinct_sources(self.conn, memory_id)
                self._insert_mention(
                    self.conn, memory_id, source, now, "extract", None, initial
                )
                approval_id = None
                if data["approval_status"] == "pending":
                    approval_id = self._create_approval(
                        self.conn, memory_id,
                        data["requested_visibility"], data["requested_context_scope"], now
                    )
                self._audit(
                    self.conn, "save", memory_id, source,
                    {
                        "dedupe_key": data["dedupe_key"],
                        "sensitivity": data["sensitivity"],
                        "approval_id": approval_id,
                    },
                )
                return {
                    "ok": True,
                    "memory_id": memory_id,
                    "merged": False,
                    "dedupe_key": data["dedupe_key"],
                    "category": data["category"],
                    "approval_required": data["approval_status"] == "pending",
                    "approval_id": approval_id,
                    "approval_status": data["approval_status"],
                    "visibility": data["visibility"],
                    "context_scope": data["context_scope"],
                    "sensitivity": data["sensitivity"],
                    "weight": round(initial, 6),
                    "mention_count": 1,
                    "distinct_source_count": 1,
                }

            # ---------------- merge existing ----------------
            old = dict(existing)
            old_sensitivity = old["sensitivity"]
            final_sensitivity = (
                data["sensitivity"]
                if SENSITIVITY_RANK[data["sensitivity"]] >= SENSITIVITY_RANK[old_sensitivity]
                else old_sensitivity
            )

            approval_status = old["approval_status"]
            visibility = old["visibility"]
            context_scope = old["context_scope"]
            approved_at = old["approved_at"]
            approved_by = old["approved_by"]
            approval_id = None

            if SENSITIVITY_RANK[final_sensitivity] > SENSITIVITY_RANK[old_sensitivity]:
                # A previously normal memory became sensitive: hide it until approved.
                approval_status = "pending"
                if final_sensitivity == "secret":
                    visibility = "private"
                    context_scope = "share_owner_only"
                else:
                    visibility = "private" if data["requested_context_scope"] == "share_owner_only" else "origin"
                    context_scope = data["requested_context_scope"]
                approved_at = None
                approved_by = None
            elif old["approval_status"] == "pending":
                approval_status = "pending"
            elif old["approval_status"] == "rejected":
                approval_status = "rejected"
                visibility = "private"
                context_scope = "share_owner_only"

            summary = data["summary"] if data["summary"] is not None else old["summary"]
            content = data["content"]
            importance = max(float(old["importance"]), data["importance"])
            confidence = max(float(old["confidence"]), data["confidence"])
            final_category = data["category"] if data["category_explicit"] else old["category"]
            promoted = (
                old["category"] == "media_impression"
                and final_category in ("experience", "preference", "game_progress")
            )
            half_life = float(old["half_life_days"])
            compression_level = int(old["compression_level"])
            if final_category != old["category"]:
                half_life = float(HALF_LIFE_DAYS.get(final_category, old["half_life_days"]))
            if promoted:
                compression_level = 0
                if data["summary"] is None:
                    summary = None
            tags = list(data["tags"])
            existing_tags = [
                r["tag"] for r in self.conn.execute(
                    "SELECT tag FROM memory_tags WHERE memory_id = ?", (old["id"],)
                ).fetchall()
            ]
            for tag in existing_tags:
                if tag not in tags:
                    tags.append(tag)

            search_text = make_search_text(
                content, summary, data["subject"] or old["subject"],
                data["object"] or old["object"], " ".join(tags),
            )

            # Normal approved facts may change visibility only when the caller
            # explicitly supplies it; otherwise keep the existing scope.
            if approval_status == "approved" and final_sensitivity == "normal":
                if data["visibility_explicit"]:
                    visibility = data["visibility"]
                if data["context_scope_explicit"]:
                    context_scope = data["context_scope"]

            self.conn.execute(
                """
                UPDATE memories
                   SET category = ?, subject = ?, subject_key = ?, predicate = ?,
                       object = ?, object_key = ?, content = ?, summary = ?,
                       search_text = ?, importance = ?, confidence = ?,
                       sensitivity = ?, visibility = ?, context_scope = ?, approval_status = ?,
                       approved_at = ?, approved_by = ?, pinned = ?,
                       half_life_days = ?, compression_level = ?,
                       expires_at = coalesce(?, expires_at), updated_at = ?
                 WHERE id = ?
                """,
                (
                    final_category,
                    data["subject"] or old["subject"],
                    data["subject_key"] or old["subject_key"],
                    data["predicate"] or old["predicate"],
                    data["object"] or old["object"],
                    data["object_key"] or old["object_key"],
                    content,
                    summary,
                    search_text,
                    importance,
                    confidence,
                    final_sensitivity,
                    visibility,
                    context_scope,
                    approval_status,
                    approved_at,
                    approved_by,
                    max(int(old["pinned"]), data["pinned"]),
                    half_life,
                    compression_level,
                    data["expires_at"],
                    now,
                    old["id"],
                ),
            )

            for tag in tags:
                self.conn.execute(
                    "INSERT OR IGNORE INTO memory_tags(memory_id, tag) VALUES (?, ?)",
                    (old["id"], tag),
                )

            # A newly sensitive memory needs an approval row before it can be searched.
            if approval_status == "pending":
                approval_id = self._create_approval(
                    self.conn, old["id"],
                    data["requested_visibility"], data["requested_context_scope"], now
                )

            touched = self._apply_mention(self.conn, old["id"], source, now, "merge")
            self._audit(
                self.conn, "merge", old["id"], source,
                {
                    "dedupe_key": data["dedupe_key"],
                    "sensitivity": final_sensitivity,
                    "approval_id": approval_id,
                },
            )
            return {
                "ok": True,
                "memory_id": old["id"],
                "merged": True,
                "dedupe_key": data["dedupe_key"],
                "category": final_category,
                "approval_required": approval_status == "pending",
                "approval_id": approval_id,
                "approval_status": approval_status,
                "visibility": visibility,
                "context_scope": context_scope,
                "sensitivity": final_sensitivity,
                "weight": touched["weight"],
                "mention_count": touched["mention_count"],
                "distinct_source_count": touched["distinct_source_count"],
            }

    # ------------------------------------------------------------------
    # Mention
    # ------------------------------------------------------------------
    def mention(self, memory_id: int, source: Any, message_id: str | None = None,
                context_excerpt: str | None = None, now_ms: int | None = None) -> dict[str, Any]:
        src = self._validate_source(source)
        if message_id:
            src["message_id"] = str(message_id)[:120]
        if context_excerpt:
            src["context_excerpt"] = str(context_excerpt).strip()[:200]
        now = int(now_ms) if now_ms is not None else _now_ms()

        with self.lock, self.conn:
            row = self.conn.execute(
                "SELECT * FROM memories WHERE id = ?", (int(memory_id),)
            ).fetchone()
            if not row:
                raise NotFoundError()
            if row["approval_status"] != "approved":
                raise ApprovalPendingError(
                    f"记忆 {memory_id} 当前状态为 {row['approval_status']}，需要主人先审批"
                )
            touched = self._apply_mention(self.conn, int(memory_id), src, now, "mention")
            self._audit(self.conn, "mention", int(memory_id), src, {"weight": touched["weight"]})
            return {"ok": True, **touched}

    # ------------------------------------------------------------------
    # Explicit promotion from fast lane to long-term memory
    # ------------------------------------------------------------------
    def upgrade_memory(self, memory_id: int, target_category: str,
                       source: Any = None, reason: str = "promote",
                       now_ms: int | None = None) -> dict[str, Any]:
        target = str(target_category or "").strip().lower()
        if target not in ("experience", "preference", "game_progress"):
            raise MemoryError("target_category 必须是 experience / preference / game_progress")
        now = int(now_ms) if now_ms is not None else _now_ms()
        src = self._validate_source(source) if source is not None else None

        with self.lock, self.conn:
            row = self.conn.execute(
                "SELECT * FROM memories WHERE id = ?", (int(memory_id),)
            ).fetchone()
            if not row:
                raise NotFoundError()
            if row["category"] != "media_impression":
                raise ConflictError("只有 media_impression 可以升级为长期记忆")

            effective = effective_weight(
                float(row["weight"]), row["last_decay_at"], row["half_life_days"], now
            )
            half_life = float(HALF_LIFE_DAYS[target])
            self.conn.execute(
                """
                UPDATE memories
                   SET category = ?, half_life_days = ?, compression_level = 0,
                       summary = NULL,
                       weight = ?, last_decay_at = ?, last_mentioned_at = ?,
                       status = 'active', purged_at = NULL, updated_at = ?
                 WHERE id = ?
                """,
                (target, half_life, effective, now, now, now, row["id"]),
            )
            if src is not None:
                touched = self._apply_mention(self.conn, int(row["id"]), src, now, "manual")
            else:
                touched = {"weight": effective, "mention_count": int(row["mention_count"])}
            self._audit(
                self.conn, "promote", int(row["id"]), src,
                {"from": "media_impression", "to": target, "reason": reason[:200]},
            )
            return {
                "ok": True,
                "memory_id": int(row["id"]),
                "from_category": "media_impression",
                "category": target,
                "half_life_days": half_life,
                "weight": round(float(touched["weight"]), 6),
                "mention_count": int(touched["mention_count"]),
            }

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------
    def _visibility_clause(self, source: dict[str, str], alias: str = "m") -> tuple[str, list[Any]]:
        # Explicit owner-private read-all mode: groups keep their normal filters.
        if (self.owner_read_all and self.owner_id
                and source.get("context_type") == "private"
                and source.get("context_id") == self.owner_id):
            return "1 = 1", []
        sql = f"""(
          (
            {alias}.visibility = 'global'
            OR ({alias}.visibility = 'origin' AND EXISTS (
                  SELECT 1 FROM memory_sources s
                   WHERE s.memory_id = {alias}.id
                     AND s.platform = ? AND s.context_type = ? AND s.context_id = ?
                ))
            OR ({alias}.visibility = 'private' AND ? = 'private' AND ? = ?)
          )
          AND (
            {alias}.context_scope = 'share_all'
            OR ({alias}.context_scope = 'share_same_platform' AND EXISTS (
                  SELECT 1 FROM memory_sources s3
                   WHERE s3.memory_id = {alias}.id
                     AND s3.platform = ?
                ))
            OR ({alias}.context_scope = 'share_owner_only' AND ? = 'private' AND ? = ?)
          )
        )"""
        return sql, [
            source["platform"], source["context_type"], source["context_id"],
            source["context_type"], source["context_id"], self.owner_id,
            source["platform"],
            source["context_type"], source["context_id"], self.owner_id,
        ]

    @_locked
    def search(self, query: str, source: Any, limit: int = 8,
               min_weight: float = 0.0, categories: list[str] | None = None,
               include_content: bool = False,
               now_ms: int | None = None) -> dict[str, Any]:
        src = self._validate_source(source)
        query = str(query or "").strip()
        limit = max(1, min(8, int(limit)))
        min_weight = _clamp(float(min_weight))
        if isinstance(categories, str):
            categories = [categories]
        categories = [str(c).strip().lower() for c in (categories or []) if str(c).strip()][:10]

        now = int(now_ms) if now_ms is not None else _now_ms()
        vis_sql, vis_params = self._visibility_clause(src)
        fast_sql = (
            " AND (m.category <> 'media_impression' OR EXISTS ("
            "SELECT 1 FROM memory_sources s4 "
            "WHERE s4.memory_id = m.id AND s4.platform = ?))"
        )
        fast_params: list[Any] = [src["platform"]]
        cat_sql = ""
        cat_params: list[Any] = []
        if categories:
            cat_sql = " AND m.category IN (" + ",".join("?" for _ in categories) + ")"
            cat_params = categories

        fts_query = make_fts_query(query)
        rows: list[sqlite3.Row] = []
        relaxed_used = False

        if fts_query:
            sql = f"""
              SELECT m.*, bm25(memory_fts) AS rank,
                     EXISTS(
                       SELECT 1 FROM memory_sources s2
                        WHERE s2.memory_id = m.id
                          AND s2.platform = ? AND s2.context_type = ? AND s2.context_id = ?
                     ) AS is_local
                FROM memory_fts
                JOIN memories m ON m.id = memory_fts.rowid
               WHERE memory_fts MATCH ?
                 AND m.status = 'active'
                 AND m.approval_status = 'approved'
                 {cat_sql}
                 AND {vis_sql}
                 {fast_sql}
               ORDER BY rank
               LIMIT ?
            """
            params: list[Any] = [
                src["platform"], src["context_type"], src["context_id"],
                fts_query,
            ]
            params.extend(cat_params)
            params.extend(vis_params)
            params.extend(fast_params)
            params.append(max(limit * 5, 20))
            rows = list(self.conn.execute(sql, params).fetchall())

        if not rows and fts_query:
            # Precision-first failed (the query has extra question words).
            # Retry with OR and let BM25 rank documents that share more terms.
            relaxed_query = make_fts_query(query, "OR")
            if relaxed_query and relaxed_query != fts_query:
                params = [
                    src["platform"], src["context_type"], src["context_id"],
                    relaxed_query,
                ]
                params.extend(cat_params)
                params.extend(vis_params)
                params.extend(fast_params)
                params.append(max(limit * 10, 40))
                rows = list(self.conn.execute(f"""
                  SELECT m.*, bm25(memory_fts) AS rank,
                         EXISTS(
                           SELECT 1 FROM memory_sources s2
                            WHERE s2.memory_id = m.id
                              AND s2.platform = ? AND s2.context_type = ? AND s2.context_id = ?
                         ) AS is_local
                    FROM memory_fts
                    JOIN memories m ON m.id = memory_fts.rowid
                   WHERE memory_fts MATCH ?
                     AND m.status = 'active'
                     AND m.approval_status = 'approved'
                     {cat_sql}
                     AND {vis_sql}
                     {fast_sql}
                   ORDER BY rank
                   LIMIT ?
                """, params).fetchall())
                relaxed_used = bool(rows)

        if not rows:
            # Fallback for 1-character Chinese queries, punctuation-only input,
            # or a query that the bigram AND was too strict for.
            like_terms = [part for part in re.split(r"\s+", query) if part]
            like_sql = ""
            like_params: list[Any] = []
            if like_terms:
                clauses = []
                for term in like_terms[:8]:
                    pattern = f"%{term}%"
                    clauses.append(
                        "(m.content LIKE ? OR coalesce(m.summary,'') LIKE ? "
                        "OR coalesce(m.subject,'') LIKE ? OR coalesce(m.object,'') LIKE ?)"
                    )
                    like_params.extend([pattern, pattern, pattern, pattern])
                like_sql = " AND (" + " AND ".join(clauses) + ")"

            sql = f"""
              SELECT m.*, NULL AS rank,
                     EXISTS(
                       SELECT 1 FROM memory_sources s2
                        WHERE s2.memory_id = m.id
                          AND s2.platform = ? AND s2.context_type = ? AND s2.context_id = ?
                     ) AS is_local
                FROM memories m
               WHERE m.status = 'active'
                 AND m.approval_status = 'approved'
                 {cat_sql}
                 AND {vis_sql}
                 {fast_sql}
                 {like_sql}
               ORDER BY m.weight DESC, m.last_mentioned_at DESC
               LIMIT ?
            """
            params = [src["platform"], src["context_type"], src["context_id"]]
            params.extend(cat_params)
            params.extend(vis_params)
            params.extend(fast_params)
            params.extend(like_params)
            params.append(limit)
            rows = list(self.conn.execute(sql, params).fetchall())

        if not rows:
            return {"ok": True, "query": query, "count": 0, "results": []}

        ranks = [abs(float(row["rank"])) for row in rows if row["rank"] is not None]
        max_rank = max(ranks) if ranks else 0.0

        results: list[dict[str, Any]] = []
        for row in rows:
            age_days = max(0.0, (now - int(row["last_mentioned_at"])) / 86_400_000.0)
            recency = 0.5 ** (age_days / 30.0)
            if row["rank"] is None:
                fts_score = 0.5
            elif max_rank <= 0:
                fts_score = 1.0
            else:
                fts_score = max(0.0, 1.0 - abs(float(row["rank"])) / max_rank)

            effective = effective_weight(
                float(row["weight"]), row["last_decay_at"], row["half_life_days"], now
            )
            if effective < min_weight:
                continue
            score = (
                0.55 * fts_score
                + 0.25 * effective
                + 0.12 * recency
                + 0.08 * float(row["confidence"])
            )
            text = row["summary"] or row["content"]
            results.append({
                "memory_id": int(row["id"]),
                "category": row["category"],
                "text": text,
                "summary": row["summary"],
                "content": row["content"] if include_content else None,
                "score": round(score, 6),
                "weight": round(float(row["weight"]), 6),
                "effective_weight": round(effective, 6),
                "confidence": round(float(row["confidence"]), 6),
                "last_mentioned_at": int(row["last_mentioned_at"]),
                "compression_level": int(row["compression_level"]),
                "sensitivity": row["sensitivity"],
                "context_scope": row["context_scope"],
                "source_hint": (
                    {
                        "group": "本群",
                        "private": "本私聊",
                        "video": "本视频",
                        "post": "本帖",
                        "game_session": "本游戏会话",
                    }.get(src["context_type"], "本会话")
                    if int(row["is_local"]) else "其他会话"
                ),
            })

        results.sort(key=lambda item: item["score"], reverse=True)

        # Prompt budget: at most 8 results and ~1200 Chinese characters
        # (roughly 600 tokens). Truncate the last item or drop it.
        trimmed: list[dict[str, Any]] = []
        used_chars = 0
        truncated = False
        for item in results:
            if len(trimmed) >= limit:
                break
            remaining = 1200 - used_chars
            if remaining <= 0:
                break
            text = str(item["text"] or "")
            if len(text) > remaining:
                if remaining < 80:
                    break
                text = text[:remaining - 1] + "…"
                item["text"] = text
                if item["summary"] is not None:
                    item["summary"] = text
                truncated = True
            used_chars += len(item["text"] or "")
            trimmed.append(item)

        return {
            "ok": True,
            "query": query,
            "count": len(trimmed),
            "truncated": truncated,
            "relaxed": relaxed_used,
            "results": trimmed,
        }

    # ------------------------------------------------------------------
    # Read / approvals / stats
    # ------------------------------------------------------------------
    def _tags_for(self, memory_id: int) -> list[str]:
        return [
            row["tag"] for row in self.conn.execute(
                "SELECT tag FROM memory_tags WHERE memory_id = ? ORDER BY tag", (memory_id,)
            ).fetchall()
        ]

    def _sources_for(self, memory_id: int) -> list[dict[str, Any]]:
        return [
            dict(row) for row in self.conn.execute(
                "SELECT platform, context_type, context_id, source_name, source_url, "
                "source_user_name, first_seen_at, last_seen_at, mention_count, context_excerpt "
                "FROM memory_sources WHERE memory_id = ? ORDER BY last_seen_at DESC",
                (memory_id,),
            ).fetchall()
        ]

    @_locked
    def get(self, memory_id: int, source: Any = None, admin: bool = False) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT * FROM memories WHERE id = ?", (int(memory_id),)
        ).fetchone()
        if not row:
            raise NotFoundError()

        if not admin:
            if row["approval_status"] != "approved":
                raise ApprovalPendingError(
                    f"记忆 {memory_id} 当前状态为 {row['approval_status']}，需要主人先审批"
                )
            src = self._validate_source(source)
            visible_sql, visible_params = self._visibility_clause(src)
            visible = self.conn.execute(
                f"SELECT 1 FROM memories m WHERE m.id = ? AND {visible_sql}",
                [int(memory_id), *visible_params],
            ).fetchone()
            if not visible:
                raise ForbiddenError("当前会话无权读取这条记忆")

        data = {
            "ok": True,
            "memory_id": int(row["id"]),
            "category": row["category"],
            "content": row["content"],
            "summary": row["summary"],
            "subject": row["subject"],
            "predicate": row["predicate"],
            "object": row["object"],
            "tags": self._tags_for(int(row["id"])),
            "importance": row["importance"],
            "confidence": row["confidence"],
            "weight": row["weight"],
            "effective_weight": round(effective_weight(
                float(row["weight"]), row["last_decay_at"], row["half_life_days"], _now_ms()
            ), 6),
            "half_life_days": row["half_life_days"],
            "compression_level": int(row["compression_level"]),
            "mention_count": row["mention_count"],
            "distinct_source_count": row["distinct_source_count"],
            "visibility": row["visibility"],
            "context_scope": row["context_scope"],
            "sensitivity": row["sensitivity"],
            "approval_status": row["approval_status"],
            "approved_at": row["approved_at"],
            "approved_by": row["approved_by"],
            "pinned": bool(row["pinned"]),
            "status": row["status"],
            "purged_at": row["purged_at"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "last_mentioned_at": row["last_mentioned_at"],
        }
        data["sources"] = self._sources_for(int(row["id"])) if admin else []
        return data

    @_locked
    def pending_approvals(self, limit: int = 50) -> dict[str, Any]:
        limit = max(1, min(200, int(limit)))
        rows = self.conn.execute(
            """
            SELECT a.id AS approval_id, a.memory_id, a.requested_at, a.reason,
                   a.requested_visibility, a.requested_context_scope,
                   m.category, m.content, m.summary,
                   m.sensitivity, m.visibility, m.context_scope, m.pinned, m.created_at
              FROM memory_approvals a
              JOIN memories m ON m.id = a.memory_id
             WHERE a.status = 'pending'
             ORDER BY a.requested_at ASC
             LIMIT ?
            """,
            (limit,),
        ).fetchall()
        items = []
        for row in rows:
            sources = self._sources_for(int(row["memory_id"]))
            items.append({
                "approval_id": int(row["approval_id"]),
                "memory_id": int(row["memory_id"]),
                "requested_at": int(row["requested_at"]),
                "requested_visibility": row["requested_visibility"],
                "requested_context_scope": row["requested_context_scope"],
                "category": row["category"],
                "content": row["content"],
                "summary": row["summary"],
                "sensitivity": row["sensitivity"],
                "visibility": row["visibility"],
                "context_scope": row["context_scope"],
                "pinned": bool(row["pinned"]),
                "latest_source": sources[0] if sources else None,
            })
        return {"ok": True, "count": len(items), "items": items}

    def decide_approval(self, approval_id: int, decision: str,
                        visibility: str | None = None,
                        context_scope: str | None = None,
                        reason: str = "", decided_by: str = "owner") -> dict[str, Any]:
        decision = str(decision or "").strip().lower()
        if decision not in ("approve", "reject"):
            raise MemoryError("decision 必须是 approve 或 reject")
        if visibility is not None:
            visibility = str(visibility).strip().lower()
            if visibility not in VISIBILITIES:
                raise MemoryError("visibility 必须是 global / origin / private")
        if context_scope is not None:
            context_scope = str(context_scope).strip().lower()
            if context_scope not in CONTEXT_SCOPES:
                raise MemoryError("context_scope 必须是 share_all / share_owner_only / share_same_platform")

        now = _now_ms()
        with self.lock, self.conn:
            approval = self.conn.execute(
                "SELECT * FROM memory_approvals WHERE id = ?", (int(approval_id),)
            ).fetchone()
            if not approval:
                raise NotFoundError("approval not found")
            if approval["status"] != "pending":
                raise ConflictError(f"该审批已经是 {approval['status']} 状态")

            memory = self.conn.execute(
                "SELECT * FROM memories WHERE id = ?", (approval["memory_id"],)
            ).fetchone()
            if not memory:
                raise NotFoundError("memory not found")

            if decision == "approve":
                final_visibility = visibility or approval["requested_visibility"]
                final_context_scope = context_scope or approval["requested_context_scope"]
                if memory["sensitivity"] == "secret":
                    final_visibility = "private"
                    final_context_scope = "share_owner_only"
                if final_context_scope == "share_owner_only":
                    final_visibility = "private"
                if final_visibility == "private":
                    final_context_scope = "share_owner_only"
                self.conn.execute(
                    """
                    UPDATE memories
                       SET approval_status = 'approved', approved_at = ?,
                           approved_by = ?, visibility = ?, context_scope = ?, updated_at = ?
                     WHERE id = ?
                    """,
                    (now, decided_by[:80], final_visibility, final_context_scope, now, memory["id"]),
                )
                self.conn.execute(
                    """
                    UPDATE memory_approvals
                       SET status = 'approved', decided_at = ?, decided_by = ?,
                           reason = ?, decided_visibility = ?, decided_context_scope = ?
                     WHERE id = ?
                    """,
                    (now, decided_by[:80], reason[:500], final_visibility, final_context_scope, approval_id),
                )
                self._audit(
                    self.conn, "approve", int(memory["id"]), None,
                    {
                        "approval_id": int(approval_id),
                        "visibility": final_visibility,
                        "context_scope": final_context_scope,
                    },
                )
                return {
                    "ok": True,
                    "approval_id": int(approval_id),
                    "memory_id": int(memory["id"]),
                    "status": "approved",
                    "visibility": final_visibility,
                    "context_scope": final_context_scope,
                }

            # reject: keep the row for audit but never search it.
            self.conn.execute(
                """
                UPDATE memories
                   SET approval_status = 'rejected', approved_at = NULL,
                       approved_by = NULL, visibility = 'private',
                       context_scope = 'share_owner_only', updated_at = ?
                 WHERE id = ?
                """,
                (now, memory["id"]),
            )
            self.conn.execute(
                """
                UPDATE memory_approvals
                   SET status = 'rejected', decided_at = ?, decided_by = ?,
                       reason = ?, decided_visibility = NULL,
                       decided_context_scope = NULL
                 WHERE id = ?
                """,
                (now, decided_by[:80], reason[:500], approval_id),
            )
            self._audit(
                self.conn, "reject", int(memory["id"]), None,
                {"approval_id": int(approval_id)},
            )
            return {
                "ok": True,
                "approval_id": int(approval_id),
                "memory_id": int(memory["id"]),
                "status": "rejected",
                "visibility": "private",
                "context_scope": "share_owner_only",
            }

    @_locked
    def stats(self) -> dict[str, Any]:
        total = self.conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
        by_status = {
            row["status"]: row["n"] for row in self.conn.execute(
                "SELECT status, COUNT(*) AS n FROM memories GROUP BY status"
            ).fetchall()
        }
        by_approval = {
            row["approval_status"]: row["n"] for row in self.conn.execute(
                "SELECT approval_status, COUNT(*) AS n FROM memories GROUP BY approval_status"
            ).fetchall()
        }
        by_sensitivity = {
            row["sensitivity"]: row["n"] for row in self.conn.execute(
                "SELECT sensitivity, COUNT(*) AS n FROM memories GROUP BY sensitivity"
            ).fetchall()
        }
        pending = self.conn.execute(
            "SELECT COUNT(*) FROM memory_approvals WHERE status = 'pending'"
        ).fetchone()[0]
        size = 0
        if self.db.db_path != ":memory:" and os.path.exists(self.db.db_path):
            size = os.path.getsize(self.db.db_path)
        return {
            "ok": True,
            "db_path": self.db.db_path,
            "db_bytes": size,
            "total": total,
            "by_status": by_status,
            "by_approval": by_approval,
            "by_sensitivity": by_sensitivity,
            "pending_approvals": pending,
        }
