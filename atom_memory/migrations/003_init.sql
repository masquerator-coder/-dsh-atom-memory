-- dsh-atom-memory schema, migration 003 (knowledge content column).
-- Executed by dsh_atom_memory.db.open_db() via PRAGMA user_version gating.
-- Adds a free-form / structured content slot so knowledge-classified facts
-- (SOP, decision rules, few-shot examples, lessons) can carry their full body
-- alongside the SPO "title" triple. The type column continues to distinguish
-- the memory/知识 category.

ALTER TABLE facts ADD COLUMN content TEXT;
ALTER TABLE fact_candidates ADD COLUMN content TEXT;
