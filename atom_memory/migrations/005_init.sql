-- dsh-atom-memory schema, migration 005 (reuse reinforcement).
-- Executed by dsh_atom_memory.db.open_db() via PRAGMA user_version gating.
--
-- Facts gain a reuse aggregate: `reinforce_count` is the *effective* number of
-- reinforcement events (exponentially decayed over time, see
-- atom_memory/reinforce.py), and `last_used_at` is the timestamp of the last
-- event that actually *counted*. `last_used_at` is also the better age signal
-- for recency ranking: a long-lived but actively reused fact should not be aged
-- out merely for being old.
--
-- `last_seen_at` is the last event of any kind, including suppressed ones. It
-- exists for observability only and must never drive the maths: dating the
-- decay from an event that was discarded would make the aggregate depend on
-- evidence the design explicitly threw away, and would stop the event log from
-- replaying to the same number.
--
-- `fact_reinforcements` is the append-only evidence log. The aggregate columns
-- on `facts` are a cache derived from it and can be rebuilt at any time with
-- reinforce.rebuild_fact_reinforcement().
--
-- The UNIQUE index is the anti-abuse mechanism, not an optimisation: it makes
-- "one reinforcement per fact per session per kind" a database invariant, so a
-- claim restated five times inside one session yields exactly one event.
--
-- Pre-existing rows start at zero reinforcement, i.e. their effective
-- importance equals the importance written at extraction time.

ALTER TABLE facts ADD COLUMN reinforce_count REAL NOT NULL DEFAULT 0;
ALTER TABLE facts ADD COLUMN last_used_at INTEGER;
ALTER TABLE facts ADD COLUMN last_seen_at INTEGER;

CREATE TABLE fact_reinforcements (
    fact_id     TEXT NOT NULL,
    user_id     TEXT NOT NULL,
    session_id  TEXT NOT NULL,
    kind        TEXT NOT NULL,
    gain        REAL NOT NULL DEFAULT 0,
    created_at  INTEGER NOT NULL,
    FOREIGN KEY (fact_id) REFERENCES facts(fact_id) ON DELETE CASCADE
);
CREATE UNIQUE INDEX idx_reinforce_unique
    ON fact_reinforcements(user_id, session_id, fact_id, kind);
CREATE INDEX idx_reinforce_fact ON fact_reinforcements(fact_id, created_at);
