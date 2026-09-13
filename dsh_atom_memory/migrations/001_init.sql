-- dsh-atom-memory schema, migration 001 (initial).
-- Executed by dsh_atom_memory.db.open_db() via PRAGMA user_version gating.


CREATE TABLE facts (
    fact_id        TEXT PRIMARY KEY,
    user_id        TEXT NOT NULL,
    session_id     TEXT NOT NULL,
    subject        TEXT NOT NULL,
    predicate      TEXT NOT NULL,
    object         TEXT NOT NULL,
    qualifiers     TEXT,
    confidence     REAL NOT NULL DEFAULT 0.5,
    importance     REAL NOT NULL DEFAULT 0.5,
    privacy        TEXT NOT NULL DEFAULT 'private',
    source_type    TEXT NOT NULL DEFAULT 'user_explicit',
    status         TEXT NOT NULL DEFAULT 'active',
    superseded_by  TEXT,
    observed_at    INTEGER NOT NULL,
    created_at     INTEGER NOT NULL,
    trace_id       TEXT,
    version        INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX idx_facts_user_status ON facts(user_id, status);
CREATE INDEX idx_facts_session ON facts(session_id);
CREATE INDEX idx_facts_spo ON facts(user_id, subject, predicate);

CREATE TABLE fact_candidates (
    candidate_id    TEXT PRIMARY KEY,
    user_id         TEXT NOT NULL,
    session_id      TEXT NOT NULL,
    turn_id         INTEGER NOT NULL,
    raw_text        TEXT,
    subject         TEXT,
    predicate       TEXT,
    object          TEXT,
    qualifiers      TEXT,
    confidence      REAL DEFAULT 0.5,
    importance      REAL DEFAULT 0.5,
    privacy         TEXT DEFAULT 'private',
    status          TEXT NOT NULL DEFAULT 'pending',
    idempotency_key TEXT UNIQUE,
    created_at      INTEGER NOT NULL
);
CREATE INDEX idx_cand_user_status ON fact_candidates(user_id, status);

CREATE TABLE summaries (
    summary_id  TEXT PRIMARY KEY,
    user_id     TEXT NOT NULL,
    scope       TEXT NOT NULL,
    theme       TEXT,
    text        TEXT NOT NULL,
    fact_ids    TEXT NOT NULL,
    version     INTEGER NOT NULL DEFAULT 1,
    stale       INTEGER NOT NULL DEFAULT 0,
    token_count INTEGER,
    updated_at  INTEGER NOT NULL
);
CREATE INDEX idx_sum_user_scope ON summaries(user_id, scope, stale);

CREATE TABLE user_profile (
    user_id    TEXT NOT NULL,
    section    TEXT NOT NULL,
    key        TEXT NOT NULL,
    value      TEXT NOT NULL,
    source     TEXT NOT NULL DEFAULT 'system_inferred',
    confidence REAL NOT NULL DEFAULT 0.5,
    privacy    TEXT NOT NULL DEFAULT 'private',
    updated_at INTEGER NOT NULL,
    PRIMARY KEY (user_id, section, key)
);

CREATE TABLE events (
    event_id   TEXT PRIMARY KEY,
    user_id    TEXT NOT NULL,
    type       TEXT NOT NULL,
    payload    TEXT NOT NULL,
    trace_id   TEXT,
    created_at INTEGER NOT NULL
);
CREATE INDEX idx_events_user_type ON events(user_id, type);

CREATE TABLE task_queue (
    task_id      TEXT PRIMARY KEY,
    task_type    TEXT NOT NULL,
    payload      TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'pending',
    priority     INTEGER NOT NULL DEFAULT 5,
    retry_count  INTEGER NOT NULL DEFAULT 0,
    max_retries  INTEGER NOT NULL DEFAULT 3,
    error        TEXT,
    created_at   INTEGER NOT NULL,
    started_at   INTEGER,
    completed_at INTEGER
);
CREATE INDEX idx_task_status_priority ON task_queue(status, priority, created_at);

CREATE VIRTUAL TABLE facts_fts USING fts5(
    fact_id UNINDEXED,
    text,
    tokenize = 'unicode61'
);

CREATE VIRTUAL TABLE facts_vec USING vec0(
    fact_id TEXT PRIMARY KEY,
    embedding float[512] distance_metric=cosine
);
