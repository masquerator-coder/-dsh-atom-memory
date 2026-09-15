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
- **Pinned (固定) profile rows (done).** The user profile is a derived view over
  active facts, so by default a newer contradicting fact rewrites the row it
  owns. A profile row can now be **pinned** in the settings panel, which freezes
  it against the memory pipeline: the facts → profile projection skips it
  entirely (`profile.upsert_profile` refuses when the caller states no pin
  state), and only an explicit edit in the panel — including releasing the pin —
  changes it. The flag is a second lock, independent of source priority, so even
  a more authoritative derived write cannot replace a pinned row; it round-trips
  through backup/restore (a restore cannot silently unfreeze it), and `user_md`
  marks fixed rows so a reader can tell which stability is deliberate (schema v4,
  migration `004_init.sql`).
- **Reuse reinforcement (done).** Reuse now strengthens a memory, so what the
  user keeps coming back to outranks what was merely written once. Facts carry a
  reuse aggregate (`reinforce_count` / `last_used_at`, schema v5, migration
  `005_init.sql`) fed by an append-only `fact_reinforcements` evidence log; the
  score is the extractor's importance plus a **saturating** bonus
  (`A_MAX · (1 − e^(−λn))`), which is locally linear — the first reuses each add
  a comparable amount — yet bounded and concave, so each further reuse counts
  strictly less. Strength also **decays** (75-day half-life) and is re-earned, so
  a memory that stops being used fades on its own. See
  [Reuse reinforcement](#reuse-reinforcement).

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
| `detail=False` (compact) | the session-start-**frozen system-prompt snapshot** and the settings **"view memory.md" dialog** | Facts grouped by memory type, ordered by a blend of importance and recency; single-valued attributes fold to `predicate: value` and repeated attributes/preferences merge onto one line; **no `fact_id`**; **every rendered line capped at 80 characters** (`_MAX_COMPACT_LINE_CHARS`, ellipsis included), with folded values clipped to 40 characters each *before* joining (`_MAX_FOLDED_VALUE_CHARS`) so one runaway value cannot hide its siblings; no document title. |
| `detail=True` (detail) | the `memory_memory_md` tool | One bullet per fact with its `fact_id`, plus the knowledge body on a folded sub-line. Each of `subject` / `predicate` / `object` is clipped to 120 characters (`_MAX_DETAIL_FIELD_CHARS`) and the body sub-line to 120 (`_DETAIL_CONTENT_CHARS`) — the `fact_id` and the bullet structure are never truncated, because locating a fact by id is what this depth is for. |

Both read paths that a human inspects are therefore *the model's own view*: the
dialog asks for the compact depth so the panel cannot drift from what the
prompt carries. The only reason to render `detail=True` is to obtain a
`fact_id` for locating a fact — which is precisely what the
`memory_memory_md` tool is for, so the dialog does not need it.

Ordering blends **importance and recency** into one score, rather than ranking by
importance with recency only as a tie-break. `importance` is only treated as a
signal when the extractor actually supplied one: the neutral default of `0.5`
means "unknown" and falls back to the fact's type rank
(`models.TYPE_IMPORTANCE` — `decision_rule` 0.90, `lesson` 0.85, `sop` 0.80,
`procedural` 0.70, `semantic` 0.60, `episodic`/`few_shot` 0.50). Without that
fallback every fact ties at 0.5 and the order degenerates to plain recency,
which is exactly what made the injected view a flat, undifferentiated list.
Recency is a real second dimension — weight 0.3 against importance's 0.7, with a
14-day half-life measured *relative to the newest fact in the set*, so the
ranking stays deterministic and clock-independent — because the score is also
what decides what a tight budget keeps. Ranking by importance alone would always
sacrifice the newest material: a fact recorded minutes ago would lose to durable
knowledge from months back. Sections are ordered by the score of their **best**
fact, so a genuinely important (or genuinely fresh) attribute can outrank a
section of stale minor rules.

Nothing is dropped while the render fits. On overflow the artifact is shrunk one
line at a time, always giving up the **globally lowest-scoring** line still
present, and a section that loses every line loses its label too, so no bare
`流程` stub survives. Giving up lines globally — instead of emptying whole
sections from the tail inwards — is what makes a small budget keep "the most
important and most recent memory" rather than whatever happens to live in the
first sections. The footer reports both the kept count per type and what was
hidden, so a trimmed view still says *which kinds* of memory exist. The fit is
measured on the **assembled artifact** (body + that very footer), so the token
budget is a hard cap on what is actually returned.

The budget for the injected snapshot is user-configurable in the dsh settings
panel (**系统提示词注入体积（memory.md）**: a slider over the fixed gears
300 / 800 / 1500 / 3000 / 6000 / 12000 tokens) and defaults to 800. Gears rather
than a free number, because this is the one memory knob whose cost recurs on
*every* request of a session: a slipped digit cannot silently multiply it, and
the budget is a cap rather than a target, so a large gear costs nothing while
the store is smaller than it. A settings value between gears (from the old
custom field, or from the plugin composition) parks the handle on the nearest
gear and says that it is off the ladder instead of pretending to be that gear.
The budget is resolved when a session freezes its snapshot, so a change applies
to every session that has not frozen yet, while already-frozen sessions keep
their byte-identical text and their KV cache.

`fact_id` is deliberately absent from the compact depth: 19 UUIDs cost roughly
700 tokens, more than they carry information for the model, while every fact
stays addressable through `recall` (which returns `fact_id`), the
`memory_memory_md` tool, and the settings editor.

### Per-line length cap

Both depths bound the size of every line they render, so a memory stays a
recognisable headline instead of a paragraph:

| Depth | Cap | Applies to |
| --- | --- | --- |
| compact | 80 chars | **every rendered content line, as a whole** — the `- ` marker, any `[when]` prefix, a `predicate:`, and the values all count toward it. This is the one choke point (`_render_section_lines`), so no line shape can escape it. |
| compact | 40 chars | each value *inside* a folded line, applied before joining, so one long value cannot consume the line and hide every sibling value. |
| detail | 120 chars | each of `subject` / `predicate` / `object`, plus the `> 知识内容` sub-line. The `fact_id` and the bullet structure are never truncated. |

Clipping marks the cut with `…`, and the ellipsis is counted **inside** the cap,
so `len(line) <= cap` holds for every rendered line. Nothing is lost from the
store: `recall` returns the untruncated `content` and `object`, and the detail
depth shows 120 characters of each field.

Capping lines also *raises* what a budget can hold — truncating the padding
leaves room for more memories. On the real 95-fact store the injected snapshot
fits 49 lines at 1500 tokens, against 46 before the cap existed.

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
  adds the `content` body column; `004_init.sql` adds `user_profile.pinned`, the
  user's 固定 flag, defaulting every pre-existing row to unpinned;
  `005_init.sql` adds `facts.reinforce_count` / `last_used_at` / `last_seen_at`
  and the `fact_reinforcements` evidence log, defaulting every pre-existing fact
  to un-reinforced) with `PRAGMA user_version`-gated migrations.
- **Retrieval**: `sqlite-vec` `vec0` KNN (cosine, 512-dim) ⊕ FTS5 (jieba
  word-segmented for Chinese), fused by Reciprocal Rank Fusion and re-ranked with
  `0.4·rrf + 0.2·effective_importance + 0.2·recency + 0.2·trust`. Neither
  `importance` nor `recency` is min-max normalised: min-max rescales per query,
  so a negligible gap between two candidates (in relevance, or in age) is
  stretched across the term's whole weight — enough to cancel the entire
  reinforcement budget, and enough to call two facts written milliseconds apart
  "maximally different in age". See [Recency](#recency).
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
3. **User profile editing** — add/edit/delete profile rows
   (`upsert_profile` / `delete_profile`), written back as highest-priority
   `user_explicit`, with a **固定** (pinned) checkbox per row that freezes the
   row against automatic memory updates.
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

## Reuse reinforcement

A fact the user keeps coming back to is worth more than one written once — but
"more" has to be bounded, or a single loudly repeated claim eventually outranks
everything. `reinforce.py` turns reuse evidence into an **effective importance**
that is both increasing and capped:

```
A(n)  = A_MAX · (1 − e^(−λn))          λ = ln2 / N_HALF
score = clamp(base_importance + A(n), 0, 1)
```

The shape is the point. At `n = 0` the derivative is `A_MAX · λ`, its maximum,
and for small `n` the curve is nearly straight — the first reuses each add a
comparable amount (**locally linear**). Past that the derivative decays to zero,
so every further reuse adds strictly less than the one before
(**diminishing marginal effect**), and `A` never reaches `A_MAX` (**bounded**).
The tuned defaults give:

| n | 0 | 1 | 2 | 3 | 4 | 5 | 8 | ∞ |
|---|---|---|---|---|---|---|---|---|
| `A(n)` | .000 | .111 | .197 | .264 | .316 | .357 | .432 | .500 |
| marginal | — | +.111 | +.086 | +.067 | +.052 | +.041 | +.019 | → 0 |

| parameter | default | meaning |
|---|---|---|
| `A_MAX` | `0.5` | most reinforcement can ever add to a fact's importance |
| `N_HALF` | `3` | reuses needed to bank half of `A_MAX` (`λ = ln2/N_HALF`) |
| `HALF_LIFE_DAYS` | `75` | how fast banked strength decays without reuse |
| `COOLDOWN_SEC` | `600` | events closer than this bank nothing |

**Decay is the other half.** The count is not a plain counter but a decaying
float, topped up by each event: `n ← n·e^(−Δt/τ) + gain`. The same number of
reuses spread over months therefore outweighs a burst confined to one session,
and a memory that stops being used fades without anyone deleting it.

**State and strength are different things.** The database holds a *snapshot*: a
decayed count plus the instant it was taken (`reinforce_count`, `last_used_at`).
Strength is always *derived* by decaying that snapshot to the moment being asked
about (`reinforce.adjust` → `effective_importance`). No reader treats the column
as the current value — doing so was a real defect, because a fact reinforced once
and then untouched for a year went on reporting its year-old strength forever, so
"reuse decays" was true of the formula and false of every number the system
showed. Callers get both: `reinforce_count` (the snapshot) and `strength` (the
same snapshot decayed to now).

Only an event that actually **banked** something advances the snapshot and its
timestamp. That is one rule with three consequences, and they are why the write
path and a replay agree exactly:

| event | snapshot | `last_used_at` | `last_seen_at` |
|---|---|---|---|
| banked > 0 | advances | advances | advances |
| passed the gate, zero gain (`retrieved_only`) | unchanged | unchanged | advances |
| suppressed by the cooldown | unchanged | unchanged | advances |

A suppressed event leaves the snapshot alone so the decay keeps applying from the
right origin — a flood of duplicates can neither preserve strength nor slide the
window forward to deny the fact future reinforcement. A gate-passing zero-gain
event is treated the same way because the replay gate requires a *positive* gain;
letting it start a cooldown would make the two paths disagree.

**What counts as reuse** — and what deliberately does not:

| kind | gain | evidence |
|---|---|---|
| `user_confirmed` | 1.0 | the user confirmed the fact |
| `user_restated` | 0.8 | the same claim was stated again in a later session |
| `applied` | 0.6 | the fact demonstrably shaped an answer |
| `retrieved_only` | 0.0 | mere recall: logged for observability, never strengthens |

The last row is the load-bearing one. Feeding retrieval hits back into the score
is a rich-get-richer loop: a fact that merely matched one query's wording becomes
easier to match forever, and noise hardens into "core memory". Only genuine reuse
counts. For the same reason **a settings-panel edit is not a confirmation** — an
edit can be a reword, a type fix, or the correction of a *wrong* memory, the last
of which is evidence against it. Callers that mean "the user confirmed this" say
so via `reinforce(...)`, which is explicit and auditable.

Anti-abuse is structural rather than heuristic:

- **Idempotency** is a database invariant — a UNIQUE index on
  `(user_id, session_id, fact_id, kind)` means a claim restated five times in one
  session yields exactly one event, and the extractor's existing `idempotent`
  validation path records it at no extra cost. Growth is therefore linear in
  *sessions*, not in messages.
- **A burst collapses to one gain.** Events inside the cooldown bank nothing, and
  the clock they are measured against only moves for banked events.
- **Replayable.** `fact_reinforcements` recomputes the snapshot exactly, because
  `roll()` is a pure function of the prior state and the advance rule above is the
  same in both directions. That is what makes retuning `A_MAX` or `HALF_LIFE_DAYS`
  retro-applicable, and suspected abuse auditable. It is verified by
  differentially replaying randomised multi-year timelines, not by a hand-picked
  sequence (`tests/test_reinforce_algorithm.py`).

The base `importance` is never rewritten — reinforcement only adds on top of it,
bounded by `A_MAX` and clamped at 1.0, so reuse can never invert a clear ordering
of the written-down values. The effective value reaches both ranking surfaces:
retrieval (`retriever._rerank`) and the injected digest (`memory.md`, which is
what gets frozen into the system prompt at session start). `AtomMem.reinforce(
user, fact_id, kind, session_id)` is the explicit entry point; the implicit one
fires when the extractor sees the user re-state a claim already stored.

Reinforcement history is **local to the database**: `backup`/`restore` carry the
facts and their base importance, not the reuse log. A restored fact is therefore
un-reinforced — the honest outcome, since the events that justified the strength
are not in the snapshot either and the restored fact could not be re-audited.


## Recency

The recency term answers "which of the things matching *this query* is most
current". It is **not** a min-max rescale of the candidate ages, for the same
reason the importance term is not rescaled: min-max hands the newest candidate
`1.0` and the oldest `0.0` *whatever the actual spread is*. Two facts written
milliseconds apart inside one session — the common case — would be treated as
maximally different in age, and the entire 0.2 recency weight would be spent on a
difference nobody can perceive.

Instead, ages are made relative and then decayed (`db.age_offset` →
`db.recency_credit`):

```
offset  = min(age - newest_age, window)      # newest candidate is the reference
recency = 0.5 ** (offset / half_life)
```

| fact | age | min-max (before) | shifted decay (now) |
|---|---|---|---|
| newest | 0 s | 1.00 | 1.000 |
| same session | +86 s | 0.00 | 0.999 |
| same day | +1 d | 0.01 | 0.977 |
| one week | +7 d | 0.10 | 0.851 |
| one month | +30 d | 0.50 | 0.500 |

The min-max column is what the same set looks like when the candidate ages span
only those 30 days: the newest wins the whole term and the same-session fact is
scored as maximally stale. The shifted decay keeps near-identical ages
near-identical, while still resolving a real month.

Two properties fall out, and both need the other:

- **The reference is the newest candidate, not the wall clock.** So nothing can
  be marked "ancient" against a clock the memory does not know about, the score
  is deterministic, and an all-old result set still spreads instead of reading as
  uniformly stale.
- **The shift is capped** at `RECENCY_REFERENCE_WINDOW_DAYS`. Without a cap, a
  set that is entirely old would push every member past the decay and collapse
  them to the same ~0, switching the term off. The cap is a backstop and must
  stay well above the half-life (it is set to 3×), otherwise it *becomes* the
  dominant shaper and flattens genuinely different ages onto one credit.

Age is measured from `last_used_at` where the fact has been used, falling back to
`created_at`. That matters: without it, recency would penalise exactly the
long-lived facts that reinforcement just promoted, and the two mechanisms would
cancel each other out.

`memory.md` uses the same decay shape (`db.recency_credit`) with a shorter
half-life (14 days vs 30) and no window cap — the right anchor for a viewer that
renders one user's *whole* memory, where the newest memory is a meaningful
definition of "now".

## Tests

```bash
pytest                          # full suite
pytest tests/test_integration.py -v
(cd dsh && pnpm test && pnpm run build)
```

The suite covers storage migrations (including v1→v2 `type`, v2→v3 `content`,
v3→v4 `pinned` and v4→v5 reinforcement-column upgrades), rule extraction
(semantic / procedural / episodic + the
`lesson` / `sop` / `decision_rule` knowledge categories), the validation chain
(episodic non-conflict, procedural single-valued, degenerate
placeholder/predicate-echo rejection), retrieval, derived views
(three-type summary bucketing + light-vs-long knowledge inclusion, pinned-profile
rows surviving the facts → profile projection while the panel's own edit still
applies), the
`memory.md` renderer in both depths (`tests/test_memory_md.py`: type grouping,
no `fact_id`/title/scores in the compact depth, type-rank fallback ordering,
multi-value folding, the token budget as a hard cap, tail-first trimming),
reuse reinforcement (`tests/test_reinforce.py`: the curve's monotonicity,
concavity, local linearity and bound; cooldown and session idempotency; decay;
event-log replay fidelity incl. suppressed events; `retrieved_only` staying
inert; the API/worker paths that produce events; and recency — half-life decay,
the relative shift and its cap, same-session ages staying near-identical, an
all-old set still spreading, and `last_used_at` beating `created_at`), and
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

