"""Self-model reflection storage with a strict change policy.

The base role card in ``roles/*.md`` is never modified here. This module only
maintains the AI's own overlay (traits/tone/relationships), with minor changes
applied automatically and major changes held for owner approval.

The actual reflection text must come from a model. This module deliberately
does not call a model; it validates and persists a proposal produced elsewhere.
"""

from __future__ import annotations

import json
import time
from typing import Any

from .store import MemoryStore

DEFAULT_STATE: dict[str, Any] = {
    "version": 0,
    "traits": {},
    "values": [],
    "tone": {},
    "relationships": {},
}
ALLOWED_KEYS = {"traits", "tone", "relationships", "values"}
IMMUTABLE_KEYS = {"guardrails", "safety", "tools", "role_card", "base_role"}
MAX_MINOR_DELTA = 0.2


def _now() -> int:
    return int(time.time() * 1000)


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


def _normalize(state: Any) -> dict[str, Any]:
    if not isinstance(state, dict):
        raise ValueError("self-model state 必须是 JSON 对象")
    for key in state:
        if key in IMMUTABLE_KEYS:
            raise ValueError(f"self-model 不能修改 {key}")
        if key not in ALLOWED_KEYS and key != "version":
            raise ValueError(f"self-model 不支持字段 {key}")
    traits = state.get("traits") or {}
    if not isinstance(traits, dict):
        raise ValueError("traits 必须是对象")
    clean_traits: dict[str, float] = {}
    for name, weight in traits.items():
        key = str(name).strip()[:40]
        if not key:
            continue
        clean_traits[key] = round(_clamp(weight), 3)
    values = state.get("values") or []
    if not isinstance(values, list):
        raise ValueError("values 必须是数组")
    clean_values = [str(v).strip()[:200] for v in values if str(v).strip()][:12]
    for key in ("tone", "relationships"):
        if key in state and not isinstance(state[key], dict):
            raise ValueError(f"{key} 必须是对象")
    return {
        "traits": clean_traits,
        "values": clean_values,
        "tone": dict(state.get("tone") or {}),
        "relationships": dict(state.get("relationships") or {}),
    }


def diff_states(current: dict[str, Any], proposed: dict[str, Any]) -> tuple[list[dict[str, Any]], str]:
    """Return human-readable changes and severity ('minor'/'major')."""
    changes: list[dict[str, Any]] = []
    severity = "minor"
    old_traits = current.get("traits") or {}
    new_traits = proposed.get("traits") or {}
    names = set(old_traits) | set(new_traits)
    for name in sorted(names):
        old = float(old_traits.get(name, 0.0))
        new = float(new_traits.get(name, 0.0))
        delta = round(new - old, 3)
        if abs(delta) < 0.0005:
            continue
        changes.append({"field": f"traits.{name}", "from": old, "to": new, "delta": delta})
        if abs(delta) > MAX_MINOR_DELTA or name not in old_traits or name not in new_traits:
            severity = "major"
    for key in ("values", "tone", "relationships"):
        old = current.get(key) or ([] if key == "values" else {})
        new = proposed.get(key) or ([] if key == "values" else {})
        if old != new:
            changes.append({"field": key, "from": old, "to": new})
            severity = "major"
    return changes, severity


class SelfModelService:
    def __init__(self, store: MemoryStore) -> None:
        self.store = store

    def current(self) -> dict[str, Any]:
        row = self.store.conn.execute(
            "SELECT * FROM self_model_versions WHERE status='active' ORDER BY version DESC LIMIT 1"
        ).fetchone()
        if not row:
            return dict(DEFAULT_STATE)
        try:
            state = json.loads(row["state_json"])
        except (TypeError, json.JSONDecodeError):
            state = dict(DEFAULT_STATE)
        state["version"] = int(row["version"])
        return state

    def submit(self, proposed: Any, *, evidence_ids: list[int] | None = None,
               owner_weight: float = 1.0, reason: str = "") -> dict[str, Any]:
        current = self.current()
        clean = _normalize(proposed)
        changes, severity = diff_states(current, clean)
        if not changes:
            return {"ok": True, "changed": False, "version": current.get("version", 0),
                    "severity": severity, "changes": []}
        now = _now()
        if severity == "minor":
            applied = self._apply_version(clean, evidence_ids or [], owner_weight, reason, now)
            return {"ok": True, "changed": True, "applied": True,
                    "version": applied, "severity": severity, "changes": changes}
        with self.store.lock, self.store.conn:
            cur = self.store.conn.execute(
                """INSERT INTO self_model_proposals
                   (base_version, proposed_json, diff_json, evidence_ids, owner_weight,
                    severity, reason, status, created_at)
                   VALUES (?,?,?,?,?,?,?, 'pending', ?)""",
                (int(current.get("version") or 0),
                 json.dumps(clean, ensure_ascii=False),
                 json.dumps(changes, ensure_ascii=False),
                 json.dumps(evidence_ids or [], ensure_ascii=False),
                 float(owner_weight), severity, reason[:500], now),
            )
            return {"ok": True, "changed": True, "applied": False,
                    "proposal_id": int(cur.lastrowid), "severity": severity,
                    "changes": changes}

    def list_proposals(self, status: str = "pending") -> list[dict[str, Any]]:
        rows = self.store.conn.execute(
            "SELECT * FROM self_model_proposals WHERE status=? ORDER BY created_at DESC LIMIT 50",
            (status,),
        ).fetchall()
        return [{
            "id": int(row["id"]),
            "base_version": int(row["base_version"]),
            "proposed": json.loads(row["proposed_json"]),
            "changes": json.loads(row["diff_json"] or "[]"),
            "evidence_ids": json.loads(row["evidence_ids"] or "[]"),
            "owner_weight": float(row["owner_weight"]),
            "severity": row["severity"],
            "reason": row["reason"],
            "created_at": int(row["created_at"]),
        } for row in rows]

    def decide(self, proposal_id: int, decision: str, *, decided_by: str = "owner") -> dict[str, Any]:
        decision = str(decision).strip().lower()
        if decision not in ("approve", "reject"):
            raise ValueError("decision 必须是 approve / reject")
        with self.store.lock, self.store.conn:
            row = self.store.conn.execute(
                "SELECT * FROM self_model_proposals WHERE id=?", (int(proposal_id),)
            ).fetchone()
            if not row:
                raise ValueError("proposal 不存在")
            if row["status"] != "pending":
                raise ValueError("proposal 已经处理过")
            now = _now()
            if decision == "reject":
                self.store.conn.execute(
                    "UPDATE self_model_proposals SET status='rejected', decided_at=? WHERE id=?",
                    (now, int(proposal_id)),
                )
                return {"ok": True, "proposal_id": int(proposal_id), "status": "rejected"}
            state = json.loads(row["proposed_json"])
            evidence = json.loads(row["evidence_ids"] or "[]")
            version = self._apply_version(
                _normalize(state), evidence, float(row["owner_weight"]),
                str(row["reason"] or "owner-approved"), now,
            )
            self.store.conn.execute(
                "UPDATE self_model_proposals SET status='approved', decided_at=? WHERE id=?",
                (now, int(proposal_id)),
            )
            return {"ok": True, "proposal_id": int(proposal_id),
                    "status": "approved", "version": version,
                    "decided_by": decided_by}

    def _apply_version(self, state: dict[str, Any], evidence_ids: list[int],
                       owner_weight: float, reason: str, now: int) -> int:
        with self.store.lock, self.store.conn:
            current = self.current()
            version = int(current.get("version") or 0) + 1
            payload = dict(state)
            payload["version"] = version
            self.store.conn.execute(
                """INSERT INTO self_model_versions
                   (version, state_json, evidence_ids, owner_weight, reason, status, created_at)
                   VALUES (?,?,?,?,?,'active',?)""",
                (version, json.dumps(payload, ensure_ascii=False),
                 json.dumps(evidence_ids, ensure_ascii=False),
                 float(owner_weight), reason[:500], now),
            )
            self.store.conn.execute(
                """INSERT INTO memory_audit
                   (action, memory_id, actor, platform, context_type, context_id, detail_json, created_at)
                   VALUES ('self_model_update', NULL, 'ai', NULL, NULL, NULL, ?, ?)""",
                (json.dumps({"version": version, "reason": reason[:200],
                             "owner_weight": owner_weight}, ensure_ascii=False), now),
            )
            return version
