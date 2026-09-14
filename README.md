# dsh-atom-memory

Lightweight in-process long-term memory plugin for DeepSeek Harness, built on
atomic facts with vector (sqlite-vec) and full-text (SQLite FTS5) retrieval.

- **Form**: Python library embedded in the Harness process
- **Storage**: single SQLite file (WAL)
- **Embedding**: FastEmbed local inference, no network at runtime
- **Vector search**: `sqlite-vec` (`vec0`, cosine distance, 512-dim)
- **Full-text search**: SQLite FTS5 (`unicode61`)
- **Concurrency**: asyncio in-process worker
- **Dependencies**: `sqlite-vec`, `fastembed`, `jieba` only — no external
  middleware, no standalone service, no runtime network dependency

## Core idea

Atomic facts are the single authoritative minimum memory unit. Summaries,
user profiles and `memory.md` are all derived views over those facts.

## Status

Implemented incrementally behind human review gates — **all four stages done**:

- **Stage 1 — Skeleton + storage + embedding (done).** Project scaffold,
  configuration, SQLite storage layer (`open_db`, sqlite-vec, WAL, migration
  `001_init.sql`) and the FastEmbed wrapper (`Embedder`).
- **Stage 2 — Write pipeline (done).** Rule extractor (`extractor.py`),
  validation chain (`validator.py`), asyncio worker (`worker.py`) and the
  public `AtomMem` class with `start` / `stop` / `add`.
- **Stage 3 — Recall pipeline (done).** Hybrid retriever (`retriever.py`:
  FTS5 + vec0 KNN + RRF + re-ranking + token budgeting), `recall`,
  `memory_md` and `profile`/`user_md`.
- **Stage 4 — Replace / forget / summaries + integration (done).**
  Soft-replace and soft-forget, `summarizer.py` (stale marking, debounce,
  rebuild), `stats()`, end-to-end integration tests and the full README.
- **Type-aware summaries + knowledge categories (done).** `type` column
  (`semantic` / `procedural` / `episodic`, migration 002); summaries bucket all
  three types. Knowledge categories (migration 003) add a `content` body column
  and four knowledge types (`sop` / `decision_rule` / `few_shot` / `lesson`):
  light ones render in summaries, long-form ones stay searchable but are
  excluded from the summary text.
- **Degenerate-fact filtering (done).** The validation chain gained a
  `degenerate` check, giving the order `empty -> degenerate -> confidence ->
  idempotency -> conflict -> privacy`. It rejects facts whose object merely
  echoes the subject or predicate, is a known placeholder, or restates the
  predicate core while ending in a generic head noun
  (`起到的作用 -> 起到的作用`, `被谁调用 -> 被调用的对象`). Such facts carry no
  information but used to pollute the rendered summary and `memory.md`.
- **Two-depth memory.md + real priority signals (done).** `memory.md` no longer
  renders one flat, recency-ordered list of raw facts for every consumer. The
  injected prompt snapshot is a compact, type-grouped, priority-ordered digest
  with no `fact_id`; the `memory_memory_md` tool keeps the full list with
  `fact_id`, and the settings dialog renders the compact digest too, so the
  panel shows exactly the text the model receives. Extraction now supplies
  `importance`/`confidence` (with a type-rank fallback), which is what makes
  "most important first" mean anything — see
  [`memory.md` — one view, two depths](#memorymd--one-view-two-depths).

## Installation

```bash
pip install -e .
# or:  pip install .
```

Requirements: Python 3.11+. Dependencies are pulled automatically —
`sqlite-vec`, `fastembed`, `jieba`. No external services, no runtime network.

> The embedding model (`BAAI/bge-small-zh-v1.5`) is downloaded once on first
> use (the only network call) and cached locally; afterwards all inference is
> entirely offline. If the model is already cached, loads are offline-first
> and make no network attempt.

## Quick start

```python
import asyncio
from atom_memory import AtomMem, MemConfig

async def main():
    mem = AtomMem(MemConfig(db_path="./memory.db"))
    await mem.start()

    # write: enqueue an utterance; worker extracts it into atomic facts
    await mem.add("user_1", "session_1", "用户喜欢黑咖啡")
    await mem.add("user_1", "session_1", "用户的职业是工程师")
    await asyncio.sleep(2)   # let the worker drain the queue

    # read: semantic + lexical recall
    result = await mem.recall("user_1", "咖啡", token_budget=2000)
    for fact in result["facts"]:
        print(fact["subject"], fact["predicate"], fact["object"], fact["final_score"])

    # derived views
    print(await mem.memory_md("user_1"))                  # full list, with fact_id
    print(await mem.memory_md("user_1", detail=False))    # compact injected digest
    print(await mem.user_md("user_1"))                    # user profile markdown

    # mutate: soft replace and soft forget
    active = mem.db.execute(
        "SELECT fact_id FROM facts WHERE user_id='user_1' "
        "AND status='active' LIMIT 1"
    ).fetchone()[0]
    await mem.replace("user_1", active, "用户喜欢少糖黑咖啡")
    await asyncio.sleep(1)
    await mem.forget("user_1", fact_id=active)
    await asyncio.sleep(1)

    print(mem.stats("user_1"))
    await mem.stop()

asyncio.run(main())
```

## Public API reference

Primary class: `AtomMem`.

| Method | Signature | Description |
| --- | --- | --- |
| `start` | `async start() -> None` | Open DB, load embedder, start worker. Idempotent. |
| `stop` | `async stop() -> None` | Stop worker and close DB. Idempotent. |
| `add` | `async add(user_id, session_id, text, turn_id=0) -> dict` | Enqueue an utterance for extraction. Returns `{candidate_id, status, trace_id}` (`status='pending'`). |
| `recall` | `async recall(user_id, query, token_budget=2000, top_k=10, include_pending=True) -> dict` | Ranked active facts + fresh summaries + pending candidates. |
| `replace` | `async replace(user_id, fact_id, new_text) -> dict` | Soft-replace: old fact → `superseded`, `superseded_by` set, new fact `active`. |
| `forget` | `async forget(user_id, fact_id=None, session_id=None) -> dict` | Soft-delete: fact(s) → `retracted`. Pass exactly one of `fact_id`/`session_id`. |
| `memory_md` | `async memory_md(user_id, max_tokens=1500, detail=True) -> str` | Render the user's memory.md. `detail=True` lists every fact with its `fact_id`; `detail=False` renders the compact digest injected into the prompt. |
| `user_md` | `async user_md(user_id, max_tokens=800) -> str` | Render the user's profile markdown. |
| `stats` | `stats(user_id) -> dict` | Counters: `facts`, `pending`, `stale_summaries`, `summaries`. |

### `memory.md` — one view, two depths

`memory.md` is a derived view over the active facts. It renders at two depths
from one implementation (`memory_md.generate_memory_md`):

| Depth | Used by | Shape |
| --- | --- | --- |
| `detail=False` (compact) | the session-start-**frozen system-prompt snapshot** and the settings **"view memory.md" dialog** | Facts grouped by memory type, ordered by priority; single-valued attributes fold to `predicate: value` and repeated attributes/preferences merge onto one line; **no `fact_id`**; long knowledge bodies truncated to 80 chars; no document title. |
| `detail=True` (detail) | the `memory_memory_md` tool | One bullet per fact with its `fact_id`, plus the knowledge body on a folded sub-line. |

Both read paths that a human inspects are therefore *the model's own view*: the
dialog asks for the compact depth so the panel cannot drift from what the
prompt carries. The only reason to render `detail=True` is to obtain a
`fact_id` for locating a fact — which is precisely what the
`memory_memory_md` tool is for, so the dialog does not need it.

Ordering is **priority first**, not recency. `importance` is only treated as a
signal when the extractor actually supplied one: the neutral default of `0.5`
means "unknown" and falls back to the fact's type rank
(`models.TYPE_IMPORTANCE` — `decision_rule` 0.90, `lesson` 0.85, `sop` 0.80,
`procedural` 0.70, `semantic` 0.60, `episodic`/`few_shot` 0.50). Without that
fallback every fact ties at 0.5 and the order degenerates to plain recency,
which is exactly what made the injected view a flat, undifferentiated list.
Sections are ordered by the rank of their **best** fact, so a genuinely
important attribute can outrank a section of minor rules.

Nothing is dropped while the render fits. On overflow the lowest-priority
sections shrink first — pruning walks from the tail inwards, so events go before
rules — and a section that loses every line loses its label too, so no bare
`### 流程` stub survives. The footer reports both the kept count per type and
what was hidden, so a trimmed view still says *which kinds* of memory exist.
The footer is charged against the same token budget, and the rendered artifact
(not just its body) is guaranteed to fit it.

`fact_id` is deliberately absent from the compact depth: 19 UUIDs cost roughly
700 tokens, more than they carry information for the model, while every fact
stays addressable through `recall` (which returns `fact_id`), the
`memory_memory_md` tool, and the settings editor.

### `recall` return shape

```json
{
  "facts": [
    {"fact_id": "...", "subject": "...", "predicate": "...", "object": "...",
     "confidence": 0.9, "importance": 0.7, "final_score": 0.88, "status": "active",
     "type": "lesson", "content": "<full knowledge body, present for knowledge facts>"}
  ],
  "summaries": [{"summary_id": "...", "scope": "global", "theme": "...",
                 "text": "...", "fact_ids": "[...]", "version": 1}],
  "pending": [{"candidate_id": "...", "subject": "...", "predicate": "...",
               "object": "...", "status": "pending"}],
  "conflicts": [],
  "token_count": 1200,
  "trace_id": "uuid"
}
```

Pending candidates are reported by `candidate_id` only (never `fact_id`).

### Configuration (`MemConfig`)

```python
from dataclasses import dataclass
from typing import Callable, Optional

@dataclass
class MemConfig:
    db_path: str = "~/.atom_memory/memory.db"
    embedding_model: str = "BAAI/bge-small-zh-v1.5"
    embedding_dim: int = 512
    default_token_budget: int = 2000
    memory_md_token_limit: int = 1500
    user_md_token_limit: int = 800
    candidate_retention_days: int = 7
    summary_rebuild_debounce_sec: int = 30
    max_retries: int = 3
    worker_poll_interval_sec: float = 0.5
    llm_extractor: Optional[Callable] = None
    privacy_filter: str = "private"
```

## Design

**Atomic facts are the single authoritative memory unit.** Summaries,
`user_profile` and `memory.md` are *derived views* rebuilt from facts.

- **Storage**: one SQLite file (WAL) via stdlib `sqlite3`; schema lives in
  `migrations/` (`001_init.sql` creates `facts`, `fact_candidates`,
  `summaries`, `user_profile`, `events`, `task_queue` plus the `facts_fts` /
  `facts_vec` virtual tables; `002_init.sql` adds the `type` column; `003_init.sql`
  adds the `content` body column) with `PRAGMA user_version`-gated migrations.
- **Retrieval**: `sqlite-vec` `vec0` KNN (cosine, 512-dim) ⊕ FTS5 (jieba
  word-segmented for Chinese), fused by Reciprocal Rank Fusion and re-ranked
  with the trust/importance/recency formula.
- **Isolation**: every query is scoped to `user_id`; internal lookups for
  conflict/idempotency honour the same boundary.
- **Soft deletion**: facts are never physically deleted; `status` moves
  `active → superseded|retracted` and retrieval filters on `active`.
- **Extraction precedence (LLM-first)**: when `MemConfig.llm_extractor` is
  set, its non-empty result is authoritative and rule-based extraction only
  runs as a **fallback** — i.e. when the LLM throws/times out or returns
  nothing usable. Dict-style LLM candidates that omit their owner are stamped
  with the current call's `user_id` / `session_id` / `turn_id`, so facts stay
  correctly scoped. The LLM callable is injected by the host (e.g. a dsh
  plugin that reads the current preset's first model and calls `ctx.llm`); the
  library itself stays free of any harness dependency.
- **Priority defaults**: when an extractor omits `importance`, the candidate is
  stamped with its **type's** rank (`models.default_importance`) rather than a
  flat `0.5`. A uniform default makes every fact tie, which silently collapses
  the ordering of every derived view to recency; the type rank at least
  reflects how long each kind of memory stays valuable. `confidence` falls back
  to a uniform `0.7` (how sure we are it was stated, which is uniform for a
  direct user message).

### Memory types in summaries

Every fact carries a `type` discriminator (`semantic` / `procedural` /
`episodic` / `sop` / `decision_rule` / `few_shot` / `lesson`), and the derived
summary is a **whole-picture compression** that buckets the compact types so
none is lost:

| Type | Example extraction source | Summary rendering |
| --- | --- | --- |
| `semantic` (preferences) | `我 喜欢 X` | `偏好 X (喜欢)` |
| `semantic` (attributes) | `我的职业是工程师` | `职业: 工程师` |
| `procedural` (workflows) | `发布流程是1.构建 2.测试 3.部署` | `工作流程-发布流程: 1)构建 2)测试 3)部署` |
| `episodic` (events) | `今天完成了项目发布` | `[今天] 项目发布` |

Knowledge categories carry an optional rich **`content`** body alongside the
SPO "title" triple (`object` is a short headline, `content` is the full body):

| Type | Example extraction source | In summary text |
| --- | --- | --- |
| `lesson` | `这次的教训是不能在没测试的情况下直接上线` | `教训: 不能在没测试的情况下直接上线` (light) |
| `decision_rule` | `当线上出事故时应该先回滚再排查` | `决策规则: 当线上出事故时 应该 先回滚再排查` (light) |
| `sop` | `发布SOP是先构建再测试最后部署` | excluded (long) — but searchable, fact_id tracked |
| `few_shot` | (LLM-provided example pair) | excluded (long) — but searchable, fact_id tracked |

Light knowledge (`lesson` / `decision_rule`) is small enough to compress into
the summary as `<predicate>: <object>`. Long-form knowledge (`sop` / `few_shot`)
is intentionally left out of the summary text — the bodies are too large to
compress usefully — but their facts are still indexed (vector + FTS), returned
by `recall`, rendered in `memory.md`, and their `fact_id` stays tracked in the
summary for consistency. A fact's full knowledge body is available through the
`content` field on `recall` results and `memory.md`. Knowledge categories are
extracted by both the rule engine (lesson / SOP / decision-rule patterns) and —
authoritatively — by the LLM extractor, which can also produce `few_shot` and
`type`-tagged candidates.

Provider behavior notes:

- **Procedural** — extraction splits ordered steps (numbered lists or
  `首先/然后/最后`), stored as `qualifiers.steps`; conflict is single-valued
  per workflow name (an updated step list goes through `replace`).
- **Episodic** — events are naturally many and independent, so different
  events never conflict; the time word is captured as `qualifiers.when`.
- `user_profile` reflects only `semantic` facts (it answers "who is the
  user"), so procedural workflows and events do not pollute the profile.

## dsh integration (bridge)

The `dsh/` npm subpackage wires this library into DeepSeek Harness as an
**isolated Python child process**:

```
dsh plugin (dsh/src/*.ts)
   │  child_process.spawn('python', ['-m','atom_memory.rpc'])
   ▼
Python atom_memory/rpc.py   (NDJSON over stdin/stdout/stderr)
   ▼
AtomMem  (in-process memory: worker, retriever, summaries …)
```

- **Protocol**: one NDJSON request per line on stdin
  `{"id","method","params"}`, one response per line on stdout
  `{"id","ok","result"|"error"}`, tagged background events (`EVT …`) and logs
  (`LOG …`) on stderr.
- **LLM-first extraction crosses the process boundary**: the dsh host runs
  extraction with its current default model and sends typed candidates via
  `persist_candidates` (→ `persist_pre` worker task); pure-Python rule
  extraction remains the fallback. Everything stays validatable through the
  same conflict/idempotency chain.
- **Lifecycle**: `start`/`stop` RPC keyed to the plugin; the child is killed on
  unload so the worker flushes and the DB closes cleanly.

Methods: `start`, `stop`, `health`, `add`, `recall`, `replace`, `forget`,
`forget_all`, `persist_candidates`, `memory_md`, `user_md`, `stats`,
`list_facts`, `edit_fact`, `list_profile`, `upsert_profile`, `delete_profile`,
`backup`, `restore`.

### Memory settings (dsh Web)

The plugin ships a browser client-plugin (`dsh/src/client/`) that adds a
**记忆/En Memory** section to the dsh settings sidebar, covering:

1. **Memory master switch** — a runtime toggle (`enabled`) applied live via the
   `atom-memory` settings namespace; no restart needed.
2. **LLM extraction model** — follow the dsh default model or pin a
   provider/model override (`extractionModel`).
3. **User profile editing** — add/edit/delete profile rows (`upsert_profile` /
   `delete_profile`), written back as highest-priority `user_explicit`.
4. **Memory & edit** — view and edit atomic facts (`list_facts` / `edit_fact`).
5. **Backup & restore** — export memory to / import from a JSON file
   (`backup` / `restore`, replace semantics).

The browser bundle ships **prebuilt** as `dsh/lib/client.js` in the harness's
`window.__ModuleLoader__.load({id, factory(require)})` module-table format
(framework deps stay external `require()` rows; the plugin's own code is
inlined). dsh serves it directly without a dev:web rebuild, so a git install
shows the settings section immediately. The `dsh/src/client` source is kept and
typechecked locally via `tsconfig.client.json`.

Run `python -m pytest tests/test_rpc.py` to exercise the wire protocol end to
end, and the `dsh/` package's own vitest + build gate for the host side.

### Install as a dsh profile layer (git distribution)

This repository is a dual-purpose package: the Python library at the root
(`atom_memory/`), and the dsh Cordis plugin built from `dsh/src`. The
**root `package.json` is the dsh bundle shell** — its `name`
(`dsh-atom-memory`) matches the `cordis.patch.yml` plugin entry and its
`dsh.bundle.patch` points at `./dsh/cordis.patch.yml`, so the whole repo
installs as one dsh profile layer directly from a git URL (no separate npm
package to publish):

```bash
dsh plugin --profile web add "https://atomgit.com/foqiang/dsh_atom_memory.git"
```

The host-side build output (`dsh/lib`) is **committed** (like `dsh-memory`)
so a git checkout loads without a build step. To update it:

```bash
(cd dsh && pnpm build && pnpm test)
```

Verify the plugin mounted as a profile layer, not a plain dependency:

```bash
dsh --profile web --dump-config | findstr /C:"atom-memory"
```

## Tests

```bash
pytest                          # full suite
pytest tests/test_integration.py -v
(cd dsh && pnpm test && pnpm run build)
```

The suite covers storage migrations (including v1→v2 `type` and v2→v3 `content`
upgrades), rule extraction (semantic / procedural / episodic + the
`lesson` / `sop` / `decision_rule` knowledge categories), the validation chain
(episodic non-conflict, procedural single-valued, degenerate
placeholder/predicate-echo rejection), retrieval, derived views
(three-type summary bucketing + light-vs-long knowledge inclusion), the
`memory.md` renderer in both depths (`tests/test_memory_md.py`: type grouping,
no `fact_id`/title/scores in the compact depth, type-rank fallback ordering,
multi-value folding, the token budget as a hard cap, tail-first trimming), and
the end-to-end pipeline (add/recall/replace/forget/memory_md/summarize/
idempotency, plus knowledge facts persisting `type` / `content` through recall
and `memory.md`). Set `ATOM_MEMORY_REAL_EMBED=1` to enable the live-model
embedding test (needs one-time download).

> On Windows, tests using `tmp_path` can fail during fixture setup with
> `PermissionError: [WinError 5]`. That is an environment issue in pytest's
> temp-dir cleanup, not a suite failure — `tests/test_memory_md.py` deliberately
> uses `connect_for_tests()` (in-memory) instead, and
> `pytest -p no:cacheprovider --basetemp=<workspace dir>` works around it.

## License

MIT

