-- dsh-atom-memory schema, migration 004 (pinned profile rows).
-- Executed by dsh_atom_memory.db.open_db() via PRAGMA user_version gating.
-- Adds the user-controlled "fixed" flag on profile rows: a pinned row is frozen
-- against the memory pipeline, so the facts -> profile projection may not update
-- or replace it. Only an explicit edit in the settings panel changes such a row.
-- 0 = not pinned (the default, and the meaning of every pre-existing row).

ALTER TABLE user_profile ADD COLUMN pinned INTEGER NOT NULL DEFAULT 0;
