-- memory_core migration: v2 -> v3
-- Phase A: observation windows, episode category, self-model version table.
-- Safe to run once. Back up memory.db before applying.

BEGIN;

INSERT OR IGNORE INTO memory_category_defaults(category, half_life_days, description, updated_at)
VALUES ('episode', 30, 'rolling group conversation summaries', CAST(strftime('%s', 'now') AS INTEGER) * 1000);

CREATE TABLE IF NOT EXISTS observation_windows (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  platform       TEXT NOT NULL,
  context_type   TEXT NOT NULL,
  context_id     TEXT NOT NULL,
  start_seq      INTEGER,
  end_seq        INTEGER,
  message_count  INTEGER NOT NULL,
  content_hash   TEXT NOT NULL,
  status         TEXT NOT NULL DEFAULT 'pending',
  created_at     INTEGER NOT NULL,
  processed_at   INTEGER,
  UNIQUE(platform, context_type, context_id, content_hash)
);

CREATE INDEX IF NOT EXISTS idx_observation_windows_context
  ON observation_windows(platform, context_type, context_id, created_at DESC);

CREATE TABLE IF NOT EXISTS self_model_versions (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  version         INTEGER NOT NULL UNIQUE,
  state_json      TEXT NOT NULL,
  evidence_ids    TEXT,
  owner_weight    REAL NOT NULL DEFAULT 1.0,
  reason          TEXT,
  status          TEXT NOT NULL DEFAULT 'active',
  created_at      INTEGER NOT NULL
);

INSERT INTO schema_meta(key, value) VALUES ('schema_version', '3')
ON CONFLICT(key) DO UPDATE SET value = excluded.value;

COMMIT;
