-- memory_core migration: v3 -> v3.1
-- Phase B: self-model proposals for major personality changes.
-- Minor changes are written directly to self_model_versions by the service.
-- Back up memory.db before applying.

BEGIN;

CREATE TABLE IF NOT EXISTS self_model_proposals (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  base_version    INTEGER NOT NULL,
  proposed_json   TEXT NOT NULL,
  diff_json       TEXT,
  evidence_ids    TEXT,
  owner_weight    REAL NOT NULL DEFAULT 1.0,
  severity        TEXT NOT NULL DEFAULT 'minor',
  reason          TEXT,
  status          TEXT NOT NULL DEFAULT 'pending',
  created_at      INTEGER NOT NULL,
  decided_at      INTEGER
);

CREATE INDEX IF NOT EXISTS idx_self_model_proposals_status
  ON self_model_proposals(status, created_at DESC);

COMMIT;
