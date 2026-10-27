-- memory_core / schema_v2.sql
-- Stage 1 design: multi-platform memory architecture (schema version 2).
-- No application logic lives in this file.
--
-- This is the proposed v2 schema. The current schema.sql (v1) and memory.db
-- stay untouched until the owner approves this design and runs the migration.
--
-- Timestamps are Unix epoch milliseconds (INTEGER), UTC.
-- Weights are normalized to 0.0 .. 1.0.
-- The database file will live at: memory_core/memory.db

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;
PRAGMA busy_timeout = 5000;
PRAGMA synchronous = NORMAL;

BEGIN;

-- ---------------------------------------------------------------------------
-- 1. Schema metadata
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS schema_meta (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

INSERT INTO schema_meta(key, value) VALUES ('schema_version', '3')
ON CONFLICT(key) DO UPDATE SET value = excluded.value;

INSERT INTO schema_meta(key, value)
VALUES ('created_at', CAST(strftime('%s', 'now') AS INTEGER) * 1000)
ON CONFLICT(key) DO NOTHING;

-- ---------------------------------------------------------------------------
-- 1.1 Category defaults (lifecycle phase will consume these)
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
  ('episode',         30, 'rolling group conversation summaries',    CAST(strftime('%s', 'now') AS INTEGER) * 1000),
  ('gossip',          14, 'low-value chatter',                     CAST(strftime('%s', 'now') AS INTEGER) * 1000),
  ('emotion',         14, 'emotional states, fast decay',          CAST(strftime('%s', 'now') AS INTEGER) * 1000),
  ('experience',      90, 'personal experiences and stories',      CAST(strftime('%s', 'now') AS INTEGER) * 1000),
  ('game_progress',  120, 'game progress and long-term saves',     CAST(strftime('%s', 'now') AS INTEGER) * 1000),
  ('media',           45, 'videos, posts, media impressions',      CAST(strftime('%s', 'now') AS INTEGER) * 1000),
  ('media_impression', 7, 'fast-decaying feed/video impressions',   CAST(strftime('%s', 'now') AS INTEGER) * 1000),
  ('other',           60, 'uncategorized memories',                CAST(strftime('%s', 'now') AS INTEGER) * 1000);

-- ---------------------------------------------------------------------------
-- 2. Canonical memory rows
--
-- One row = one canonical fact/preference/event, shared across conversations.
-- The same fact seen in several groups has one memories row and several
-- memory_sources rows.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS memories (
  id                    INTEGER PRIMARY KEY AUTOINCREMENT,

  -- Stable identity for upsert/merge. The extractor computes this from
  -- normalized category + subject + predicate + object (or content for events).
  dedupe_key            TEXT NOT NULL UNIQUE,

  category              TEXT NOT NULL DEFAULT 'other',

  -- Optional structured triple for matching/relations. Display strings are
  -- kept separately from normalized lookup keys.
  subject               TEXT,
  subject_key           TEXT,
  predicate             TEXT,
  object                TEXT,
  object_key            TEXT,

  -- Original fact sentence. Kept even after compression for audit/correction.
  content               TEXT NOT NULL,

  -- Compressed human-readable version (short summary or keywords).
  -- NULL = not compressed yet. Prompt injection uses this before content.
  summary               TEXT,

  -- Pre-tokenized text used by FTS5. The writer fills this with jieba tokens
  -- (space separated) + tags + aliases, because unicode61 cannot segment Chinese.
  search_text           TEXT NOT NULL DEFAULT '',

  importance            REAL NOT NULL DEFAULT 0.5
                        CHECK (importance >= 0 AND importance <= 1),
  confidence            REAL NOT NULL DEFAULT 0.7
                        CHECK (confidence >= 0 AND confidence <= 1),

  -- base_weight = weight assigned at creation (category/importance/confidence).
  -- weight      = materialized weight at last_decay_at; effective weight is
  --               computed lazily as weight * 2^(-age_days / half_life_days).
  base_weight           REAL NOT NULL DEFAULT 0.5
                        CHECK (base_weight >= 0 AND base_weight <= 1),
  weight                REAL NOT NULL DEFAULT 0.5
                        CHECK (weight >= 0 AND weight <= 1),
  half_life_days        REAL NOT NULL DEFAULT 90
                        CHECK (half_life_days > 0),

  mention_count         INTEGER NOT NULL DEFAULT 0
                        CHECK (mention_count >= 0),
  distinct_source_count INTEGER NOT NULL DEFAULT 0
                        CHECK (distinct_source_count >= 0),
  retrieval_count       INTEGER NOT NULL DEFAULT 0
                        CHECK (retrieval_count >= 0),

  first_seen_at         INTEGER NOT NULL,
  last_mentioned_at     INTEGER NOT NULL,
  last_retrieved_at     INTEGER,
  last_decay_at         INTEGER NOT NULL,
  last_compressed_at    INTEGER,

  -- 0 = full content, 1 = summary, 2 = keywords only.
  compression_level     INTEGER NOT NULL DEFAULT 0
                        CHECK (compression_level BETWEEN 0 AND 2),

  -- global  = can be retrieved from any group/private conversation
  -- origin  = only the source conversation(s) can retrieve it
  -- private = owner-only, never injected into group prompts
  visibility            TEXT NOT NULL DEFAULT 'global'
                        CHECK (visibility IN ('global', 'origin', 'private')),

  -- Cross-platform sharing boundary (new in v2):
  --   share_all             any platform/context, subject to visibility
  --   share_same_platform   only contexts on a platform already in memory_sources
  --   share_owner_only      owner private only
  --
  -- visibility and context_scope are both enforced. For example, a memory can
  -- be share_same_platform + visibility=origin, which means "only the exact
  -- source context, and only on its own platform".
  context_scope         TEXT NOT NULL DEFAULT 'share_same_platform'
                        CHECK (context_scope IN ('share_all', 'share_owner_only', 'share_same_platform')),

  -- normal/sensitive/secret. secret must be private (enforced below).
  sensitivity           TEXT NOT NULL DEFAULT 'normal'
                        CHECK (sensitivity IN ('normal', 'sensitive', 'secret')),

  -- sensitive/secret facts are not searchable until the owner approves them.
  -- normal facts are auto-approved at save time.
  approval_status       TEXT NOT NULL DEFAULT 'approved'
                        CHECK (approval_status IN ('pending', 'approved', 'rejected')),
  approved_at           INTEGER,
  approved_by           TEXT,

  pinned                INTEGER NOT NULL DEFAULT 0
                        CHECK (pinned IN (0, 1)),

  -- active = searchable; archived = low-priority/compressed; purged = tombstone.
  status                TEXT NOT NULL DEFAULT 'active'
                        CHECK (status IN ('active', 'archived', 'purged')),

  expires_at            INTEGER,
  purged_at             INTEGER,

  created_at            INTEGER NOT NULL,
  updated_at            INTEGER NOT NULL,
  meta_json             TEXT NOT NULL DEFAULT '{}',

  CHECK (sensitivity <> 'secret' OR visibility = 'private'),
  CHECK (context_scope <> 'share_owner_only' OR visibility = 'private')
);

-- Extra guard for clients/tools that bypass the table CHECK (e.g. old DBs
-- after migration, where SQLite cannot add a table-level CHECK).
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
-- 3. Evidence / provenance: where a canonical memory was seen
--
-- One row per (memory, conversation). This is what makes cross-group memory
-- auditable: a memory can be supported by several groups without duplicating
-- the fact itself.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS memory_sources (
  id               INTEGER PRIMARY KEY AUTOINCREMENT,
  memory_id        INTEGER NOT NULL
                   REFERENCES memories(id) ON DELETE CASCADE,

  -- Generalized source (v2):
  --   platform      qq | bilibili | heybox | local_game | web | other
  --   context_type  group | private | video | post | game_session |
  --                 comment | live | other
  --   context_id    group number / BV id / post id / game session id
  platform         TEXT NOT NULL,
  context_type     TEXT NOT NULL,
  context_id       TEXT NOT NULL,

  source_name      TEXT,                   -- group name / video title / game name
  source_url       TEXT,                   -- optional B站/小黑盒 link
  source_user_id   TEXT,                   -- last speaker/author on that platform
  source_user_name TEXT,

  first_seen_at    INTEGER NOT NULL,
  last_seen_at     INTEGER NOT NULL,
  mention_count    INTEGER NOT NULL DEFAULT 1
                   CHECK (mention_count > 0),
  last_message_id  TEXT,

  -- Short evidence excerpt, max 200 chars by application policy.
  -- This is evidence, not a full chat log.
  context_excerpt  TEXT,

  UNIQUE (memory_id, platform, context_type, context_id)
);

-- ---------------------------------------------------------------------------
-- 4. Mention event log
--
-- Append-only time series used by decay/compression diagnostics and by
-- "was this actually mentioned recently?" checks. It can be pruned by a
-- retention job; source rows and the memory itself remain.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS memory_mentions (
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

-- ---------------------------------------------------------------------------
-- 5. Tags (normalized, one row per tag)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS memory_tags (
  memory_id INTEGER NOT NULL
            REFERENCES memories(id) ON DELETE CASCADE,
  tag       TEXT NOT NULL,                 -- lower-case / normalized by app
  PRIMARY KEY (memory_id, tag)
);

-- ---------------------------------------------------------------------------
-- 6. Relations between memories
--
-- Keeps contradictions and updates explicit instead of silently overwriting
-- an older fact. Search/ranking can prefer the newer "updates" target.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS memory_relations (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  from_memory_id INTEGER NOT NULL
                 REFERENCES memories(id) ON DELETE CASCADE,
  to_memory_id   INTEGER NOT NULL
                 REFERENCES memories(id) ON DELETE CASCADE,

  relation       TEXT NOT NULL
                 CHECK (relation IN ('related', 'updates', 'contradicts', 'same_as', 'derived_from')),
  confidence     REAL NOT NULL DEFAULT 0.7
                 CHECK (confidence >= 0 AND confidence <= 1),
  note           TEXT,
  created_at     INTEGER NOT NULL,

  UNIQUE (from_memory_id, to_memory_id, relation),
  CHECK (from_memory_id <> to_memory_id)
);

-- ---------------------------------------------------------------------------
-- 7. Audit log
--
-- No FK to memories on purpose: the audit trail should survive a hard purge.
-- detail_json must never contain raw secrets or full message logs.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS memory_audit (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  action      TEXT NOT NULL,
  memory_id   INTEGER,
  actor       TEXT,
  platform    TEXT,
  context_type TEXT,
  context_id  TEXT,
  detail_json TEXT NOT NULL DEFAULT '{}',
  created_at  INTEGER NOT NULL
);

-- ---------------------------------------------------------------------------
-- 7.1 Owner approval queue
--
-- Sensitive facts are saved with approval_status='pending' and are excluded
-- from search until the owner approves them. Only one pending request per
-- memory is allowed.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS memory_approvals (
  id                   INTEGER PRIMARY KEY AUTOINCREMENT,
  memory_id            INTEGER NOT NULL
                       REFERENCES memories(id) ON DELETE CASCADE,
  status               TEXT NOT NULL DEFAULT 'pending'
                       CHECK (status IN ('pending', 'approved', 'rejected')),
  requested_at         INTEGER NOT NULL,
  decided_at           INTEGER,
  decided_by           TEXT,
  reason               TEXT,
  requested_visibility TEXT NOT NULL DEFAULT 'origin'
                       CHECK (requested_visibility IN ('global', 'origin', 'private')),
  decided_visibility   TEXT
                       CHECK (decided_visibility IS NULL OR decided_visibility IN ('global', 'origin', 'private')),
  requested_context_scope TEXT NOT NULL DEFAULT 'share_same_platform'
                       CHECK (requested_context_scope IN ('share_all', 'share_owner_only', 'share_same_platform')),
  decided_context_scope   TEXT
                       CHECK (decided_context_scope IS NULL OR decided_context_scope IN ('share_all', 'share_owner_only', 'share_same_platform'))
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_memory_approvals_pending
  ON memory_approvals(memory_id)
  WHERE status = 'pending';

CREATE INDEX IF NOT EXISTS idx_approvals_status
  ON memory_approvals(status, requested_at);

-- ---------------------------------------------------------------------------
-- 8. Full-text index
--
-- External-content FTS5 table. search_text is already jieba-tokenized by the
-- application, which is required for Chinese search with unicode61.
-- For 1-2 character queries the application should fall back to LIKE/instr.
-- ---------------------------------------------------------------------------
CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(
  search_text,
  summary,
  subject,
  object,
  content = 'memories',
  content_rowid = 'id',
  tokenize = 'unicode61 remove_diacritics 2'
);

CREATE TRIGGER IF NOT EXISTS memories_ai
AFTER INSERT ON memories
BEGIN
  INSERT INTO memory_fts(rowid, search_text, summary, subject, object)
  VALUES (
    new.id,
    new.search_text,
    coalesce(new.summary, ''),
    coalesce(new.subject, ''),
    coalesce(new.object, '')
  );
END;

CREATE TRIGGER IF NOT EXISTS memories_ad
AFTER DELETE ON memories
BEGIN
  INSERT INTO memory_fts(memory_fts, rowid, search_text, summary, subject, object)
  VALUES (
    'delete',
    old.id,
    old.search_text,
    coalesce(old.summary, ''),
    coalesce(old.subject, ''),
    coalesce(old.object, '')
  );
END;

CREATE TRIGGER IF NOT EXISTS memories_au
AFTER UPDATE OF search_text, summary, subject, object ON memories
BEGIN
  INSERT INTO memory_fts(memory_fts, rowid, search_text, summary, subject, object)
  VALUES (
    'delete',
    old.id,
    old.search_text,
    coalesce(old.summary, ''),
    coalesce(old.subject, ''),
    coalesce(old.object, '')
  );
  INSERT INTO memory_fts(rowid, search_text, summary, subject, object)
  VALUES (
    new.id,
    new.search_text,
    coalesce(new.summary, ''),
    coalesce(new.subject, ''),
    coalesce(new.object, '')
  );
END;

-- If rows existed before this schema was applied, rebuild the FTS index once:
-- INSERT INTO memory_fts(memory_fts) VALUES ('rebuild');

-- ---------------------------------------------------------------------------
-- 9. Indexes
-- ---------------------------------------------------------------------------
CREATE INDEX IF NOT EXISTS idx_memories_status_weight
  ON memories(status, weight DESC, last_mentioned_at DESC);

CREATE INDEX IF NOT EXISTS idx_memories_purge
  ON memories(status, pinned, mention_count, last_mentioned_at);

CREATE INDEX IF NOT EXISTS idx_memories_visibility
  ON memories(visibility, sensitivity, status);

CREATE INDEX IF NOT EXISTS idx_memories_category
  ON memories(category, status);

CREATE INDEX IF NOT EXISTS idx_memories_last_mentioned
  ON memories(last_mentioned_at DESC);

CREATE INDEX IF NOT EXISTS idx_memories_approval
  ON memories(approval_status, status);

CREATE INDEX IF NOT EXISTS idx_memories_context_scope
  ON memories(context_scope, visibility, status);

CREATE INDEX IF NOT EXISTS idx_sources_platform_context
  ON memory_sources(platform, context_type, context_id, last_seen_at DESC);

CREATE INDEX IF NOT EXISTS idx_sources_memory
  ON memory_sources(memory_id);

CREATE INDEX IF NOT EXISTS idx_mentions_memory_time
  ON memory_mentions(memory_id, mentioned_at DESC);

CREATE INDEX IF NOT EXISTS idx_mentions_time
  ON memory_mentions(mentioned_at);

CREATE INDEX IF NOT EXISTS idx_mentions_platform_time
  ON memory_mentions(platform, context_type, context_id, mentioned_at DESC);

CREATE INDEX IF NOT EXISTS idx_tags_tag
  ON memory_tags(tag, memory_id);

CREATE INDEX IF NOT EXISTS idx_relations_to
  ON memory_relations(to_memory_id, relation);

CREATE INDEX IF NOT EXISTS idx_audit_time
  ON memory_audit(created_at DESC);


-- ---------------------------------------------------------------------------
-- 10. Observation windows and self-model versions (phase A)
-- ---------------------------------------------------------------------------
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

COMMIT;
