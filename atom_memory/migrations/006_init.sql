-- dsh-atom-memory schema, migration 006 (drop the derived summary layer).
-- Executed by dsh_atom_memory.db.open_db() via PRAGMA user_version gating.
--
-- The separate `summaries` aggregate (summarizer.py + the summaries table) was
-- a second implementation of the same whole-picture compression that memory.md
-- (now `summary`, cf. summary.py) already produces. It was redundant: memory.md
-- is the injected compact digest, and the `summaries` table only ever backed
-- the `summary()` API / `memory_summary` tool / the settings summary modal,
-- all of which are removed. The table and its index are dropped; existing
-- databases lose the derived rows (facts / profile are unaffected).

DROP TABLE IF EXISTS summaries;
DROP INDEX IF EXISTS idx_sum_user_scope;
