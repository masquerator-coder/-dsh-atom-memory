-- dsh-atom-memory schema, migration 002 (fact type column).
-- Executed by dsh_atom_memory.db.open_db() via PRAGMA user_version gating.
-- Adds the memory-type discriminator so derived views (summaries) can render
-- semantic / procedural / episodic facts differently instead of flattening
-- everything into attribute/preference form.

ALTER TABLE facts ADD COLUMN type TEXT NOT NULL DEFAULT 'semantic';
ALTER TABLE fact_candidates ADD COLUMN type TEXT NOT NULL DEFAULT 'semantic';
