-- memory_core / migrations / 001_multiplatform.sql
--
-- One-way migration: schema v1 -> v2 (multi-platform source + context_scope).
--
-- BEFORE RUNNING:
--   1. Stop the memory_core HTTP service.
--   2. Make a copy of memory.db (e.g. memory.db.bak-v1).
--   3. Confirm schema_meta.schema_version = '1'.
--
-- RUN (one time):
--   sqlite3 memory_core/memory.db < memory_core/migrations/001_multiplatform.sql
--
-- This script preserves all existing rows:
--   - old QQ source_kind/source_id -> platform='qq', context_type=kind, context_id=id
--   - old visibility -> context_scope:
--       global  -> share_all
--       origin  -> share_same_platform
--       private -> share_owner_only
--   - old source/mention/audit tables are rebuilt inside one transaction.
--
-- It does NOT delete or rewrite memories.content, tags, relations, approvals,
-- or the FTS index. The FTS index is unaffected because only memories columns
-- used by FTS (search_text/summary/subject/object) stay unchanged.

PRAGMA foreign_keys = OFF;
BEGIN;

-- ---------------------------------------------------------------------------
-- 1. memories: add context_scope
-- ---------------------------------------------------------------------------
ALTER TABLE memories
  ADD COLUMN context_scope TEXT NOT NULL DEFAULT 'share_same_platform'
  CHECK (context_scope IN ('share_all', 'share_owner_only', 'share_same_platform'));

UPDATE memories
   SET context_scope = CASE visibility
                          WHEN 'global'  THEN 'share_all'
                          WHEN 'private' THEN 'share_owner_only'
                          ELSE 'share_same_platform'
                        END;

-- The old table cannot gain a new table-level CHECK through ALTER TABLE;
-- enforce the same combination rule with triggers.
CREATE TRIGGER IF NOT EXISTS memories_context_scope_bi
BEFORE INSERT ON memories
WHEN new.context_scope = 'share_owner_only' AND new.visibility <> 'private'
BEGIN
  SELECT RAISE(ABORT, 'share_owner_only requires visibility=private');
END;

CREATE TRIGGER IF NOT EXISTS memories_context_scope_bu
BEFORE UPDATE OF visibility, context_scope ON memories
WHEN new.context_scope = 'share_owner_only' AND new.visibility <> 'private'
BEGIN
  SELECT RAISE(ABORT, 'share_owner_only requires visibility=private');
END;

-- ---------------------------------------------------------------------------
-- 2. approvals: remember the requested/decided sharing scope
-- ---------------------------------------------------------------------------
ALTER TABLE memory_approvals
  ADD COLUMN requested_context_scope TEXT NOT NULL DEFAULT 'share_same_platform'
  CHECK (requested_context_scope IN ('share_all', 'share_owner_only', 'share_same_platform'));

ALTER TABLE memory_approvals
  ADD COLUMN decided_context_scope TEXT
  CHECK (decided_context_scope IS NULL OR decided_context_scope IN ('share_all', 'share_owner_only', 'share_same_platform'));

UPDATE memory_approvals
   SET requested_context_scope = COALESCE(
         (SELECT m.context_scope FROM memories m WHERE m.id = memory_approvals.memory_id),
         'share_same_platform'
       );

UPDATE memory_approvals
   SET decided_context_scope = CASE
         WHEN status = 'approved'
           THEN COALESCE(
                  (SELECT m.context_scope FROM memories m WHERE m.id = memory_approvals.memory_id),
                  'share_same_platform'
                )
         ELSE NULL
       END;

-- ---------------------------------------------------------------------------
-- 3. category defaults (lifecycle phase will read this table)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS memory_category_defaults (
  category       TEXT PRIMARY KEY,
  half_life_days REAL NOT NULL CHECK (half_life_days > 0),
  description    TEXT,
  updated_at     INTEGER NOT NULL
);

INSERT OR REPLACE INTO memory_category_defaults(category, half_life_days, description, updated_at) VALUES
  ('identity',       365, 'identity, names, boundaries',           CAST(strftime('%s', 'now') AS INTEGER) * 1000),
  ('preference',     180, 'likes, dislikes, habits',               CAST(strftime('%s', 'now') AS INTEGER) * 1000),
  ('relationship',   180, 'people and relationships',              CAST(strftime('%s', 'now') AS INTEGER) * 1000),
  ('promise',         90, 'promises and agreements',               CAST(strftime('%s', 'now') AS INTEGER) * 1000),
  ('task',            14, 'short-lived tasks and reminders',       CAST(strftime('%s', 'now') AS INTEGER) * 1000),
  ('opinion',         60, 'opinions and attitudes',                CAST(strftime('%s', 'now') AS INTEGER) * 1000),
  ('event',           30, 'events and happenings',                 CAST(strftime('%s', 'now') AS INTEGER) * 1000),
  ('gossip',          14, 'low-value chatter',                     CAST(strftime('%s', 'now') AS INTEGER) * 1000),
  ('emotion',         14, 'emotional states, fast decay',          CAST(strftime('%s', 'now') AS INTEGER) * 1000),
  ('experience',      90, 'personal experiences and stories',      CAST(strftime('%s', 'now') AS INTEGER) * 1000),
  ('game_progress',  120, 'game progress and long-term saves',     CAST(strftime('%s', 'now') AS INTEGER) * 1000),
  ('media',           45, 'videos, posts, media impressions',      CAST(strftime('%s', 'now') AS INTEGER) * 1000),
  ('media_impression', 7, 'fast-decaying feed/video impressions',   CAST(strftime('%s', 'now') AS INTEGER) * 1000),
  ('other',           60, 'uncategorized memories',                CAST(strftime('%s', 'now') AS INTEGER) * 1000);

-- ---------------------------------------------------------------------------
-- 4. rebuild memory_sources with generalized source columns
-- ---------------------------------------------------------------------------
DROP TABLE IF EXISTS memory_sources_v2;

CREATE TABLE memory_sources_v2 (
  id               INTEGER PRIMARY KEY AUTOINCREMENT,
  memory_id        INTEGER NOT NULL
                   REFERENCES memories(id) ON DELETE CASCADE,

  platform         TEXT NOT NULL,
  context_type     TEXT NOT NULL,
  context_id       TEXT NOT NULL,

  source_name      TEXT,
  source_url       TEXT,
  source_user_id   TEXT,
  source_user_name TEXT,

  first_seen_at    INTEGER NOT NULL,
  last_seen_at     INTEGER NOT NULL,
  mention_count    INTEGER NOT NULL DEFAULT 1
                   CHECK (mention_count > 0),
  last_message_id  TEXT,
  context_excerpt  TEXT,

  UNIQUE (memory_id, platform, context_type, context_id)
);

INSERT INTO memory_sources_v2(
  id, memory_id, platform, context_type, context_id,
  source_name, source_url, source_user_id, source_user_name,
  first_seen_at, last_seen_at, mention_count, last_message_id, context_excerpt
)
SELECT
  id, memory_id, 'qq', source_kind, source_id,
  source_name, NULL, source_user_id, source_user_name,
  first_seen_at, last_seen_at, mention_count, last_message_id, context_excerpt
FROM memory_sources;

DROP TABLE memory_sources;
ALTER TABLE memory_sources_v2 RENAME TO memory_sources;

CREATE INDEX idx_sources_platform_context
  ON memory_sources(platform, context_type, context_id, last_seen_at DESC);
CREATE INDEX idx_sources_memory
  ON memory_sources(memory_id);

-- ---------------------------------------------------------------------------
-- 5. rebuild memory_mentions with generalized source columns
-- ---------------------------------------------------------------------------
DROP TABLE IF EXISTS memory_mentions_v2;

CREATE TABLE memory_mentions_v2 (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  memory_id       INTEGER NOT NULL
                  REFERENCES memories(id) ON DELETE CASCADE,

  platform        TEXT NOT NULL,
  context_type    TEXT NOT NULL,
  context_id      TEXT NOT NULL,
  message_id      TEXT,
  user_id         TEXT,

  mentioned_at    INTEGER NOT NULL,
  weight_before   REAL,
  weight_after    REAL,

  reason          TEXT NOT NULL DEFAULT 'mention'
                  CHECK (reason IN ('extract', 'mention', 'manual', 'merge', 'import')),

  context_excerpt TEXT
);

INSERT INTO memory_mentions_v2(
  id, memory_id, platform, context_type, context_id,
  message_id, user_id, mentioned_at, weight_before, weight_after,
  reason, context_excerpt
)
SELECT
  id, memory_id, 'qq', source_kind, source_id,
  message_id, user_id, mentioned_at, weight_before, weight_after,
  reason, context_excerpt
FROM memory_mentions;

DROP TABLE memory_mentions;
ALTER TABLE memory_mentions_v2 RENAME TO memory_mentions;

CREATE INDEX idx_mentions_memory_time
  ON memory_mentions(memory_id, mentioned_at DESC);
CREATE INDEX idx_mentions_time
  ON memory_mentions(mentioned_at);
CREATE INDEX idx_mentions_platform_time
  ON memory_mentions(platform, context_type, context_id, mentioned_at DESC);

-- ---------------------------------------------------------------------------
-- 6. rebuild memory_audit with generalized source columns
-- ---------------------------------------------------------------------------
DROP TABLE IF EXISTS memory_audit_v2;

CREATE TABLE memory_audit_v2 (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  action       TEXT NOT NULL,
  memory_id    INTEGER,
  actor        TEXT,
  platform     TEXT,
  context_type TEXT,
  context_id   TEXT,
  detail_json  TEXT NOT NULL DEFAULT '{}',
  created_at   INTEGER NOT NULL
);

INSERT INTO memory_audit_v2(
  id, action, memory_id, actor, platform,
  context_type, context_id, detail_json, created_at
)
SELECT
  id, action, memory_id, actor, 'qq',
  source_kind, source_id, detail_json, created_at
FROM memory_audit;

DROP TABLE memory_audit;
ALTER TABLE memory_audit_v2 RENAME TO memory_audit;

CREATE INDEX idx_audit_time
  ON memory_audit(created_at DESC);

-- ---------------------------------------------------------------------------
-- 7. indexes for the new scope column
-- ---------------------------------------------------------------------------
CREATE INDEX IF NOT EXISTS idx_memories_context_scope
  ON memories(context_scope, visibility, status);

-- ---------------------------------------------------------------------------
-- 8. mark schema version
-- ---------------------------------------------------------------------------
INSERT INTO schema_meta(key, value) VALUES ('schema_version', '2')
ON CONFLICT(key) DO UPDATE SET value = excluded.value;

COMMIT;
PRAGMA foreign_keys = ON;

-- Optional verification:
-- SELECT value FROM schema_meta WHERE key = 'schema_version';  -- expect 2
-- SELECT COUNT(*) FROM memory_sources;
-- SELECT COUNT(*) FROM memory_mentions;
-- PRAGMA integrity_check;
