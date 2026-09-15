# The `atom_memory` Python library

The consumable contract of the Python half: what to install, what to call, which
shapes come back, and how the store is designed. For the dsh side — the bridge,
the tools, and the settings panel — read [`dsh/README.md`](../dsh/README.md).

## Form and dependencies

- **Form**: Python library, importable on its own and embedded in the dsh child
  process (`python -m atom_memory.rpc`).
- **Storage**: a single SQLite file (WAL).
- **Embedding**: FastEmbed local inference, no network at runtime.
- **Vector search**: `sqlite-vec` (`vec0`, cosine distance, 512-dim).
- **Full-text search**: SQLite FTS5 (`unicode61`).
- **Concurrency**: an asyncio in-process worker.
- **Dependencies**: `sqlite-vec`, `fastembed`, `jieba` only — no external
  middleware, no standalone service, no runtime network dependency.

## Core idea

Atomic facts are the single authoritative minimum memory unit. The memory
summary and the user profile are all derived views over those facts.

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
    print(await mem.summary("user_1"))                  # full list, with fact_id
    print(await mem.summary("user_1", detail=False))    # compact injected digest
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
| `recall` | `async recall(user_id, query, token_budget=2000, top_k=10, include_pending=True) -> dict` | Ranked active facts + pending candidates. |
| `replace` | `async replace(user_id, fact_id, new_text) -> dict` | Soft-replace: old fact → `superseded`, `superseded_by` set, new fact `active`. |
| `forget` | `async forget(user_id, fact_id=None, session_id=None) -> dict` | Soft-delete: fact(s) → `retracted`. Pass exactly one of `fact_id`/`session_id`. |
| `summary` | `async summary(user_id, max_tokens=1500, detail=True) -> str` | Render the memory summary. `detail=True` lists every fact with its `fact_id`; `detail=False` renders the compact digest injected into the prompt. |
| `user_md` | `async user_md(user_id, max_tokens=800) -> str` | Render the user's profile markdown. |
| `stats` | `stats(user_id) -> dict` | Counters: `facts`, `pending`. |
| `reinforce` | `async reinforce(user, fact_id, kind, session_id) -> dict` | Explicit reuse evidence. See [Reuse reinforcement](reinforcement.md). |

### `summary` — one view, two depths

The memory summary is a derived view over the active facts. It renders at two
depths from one implementation (`summary.generate_summary`):

| Depth | Used by | Shape |
| --- | --- | --- |
| `detail=False` (compact) | the session-start-**frozen system-prompt snapshot** and the settings **"view memory" dialog** | Facts grouped by memory type, ordered by a blend of importance and recency; single-valued attributes fold to `predicate: value` and repeated attributes/preferences merge onto one line; **no `fact_id`**; **every rendered line capped at 80 characters** (`_MAX_COMPACT_LINE_CHARS`, ellipsis included), with folded values clipped to 40 characters each *before* joining (`_MAX_FOLDED_VALUE_CHARS`) so one runaway value cannot hide its siblings; no document title. |
| `detail=True` (detail) | the `memory_summary_detail` tool | One bullet per fact with its `fact_id`, plus the knowledge body on a folded sub-line. Each of `subject` / `predicate` / `object` is clipped to 120 characters (`_MAX_DETAIL_FIELD_CHARS`) and the body sub-line to 120 (`_DETAIL_CONTENT_CHARS`) — the `fact_id` and the bullet structure are never truncated, because locating a fact by id is what this depth is for. |

Both read paths that a human inspects are therefore *the model's own view*: the
dialog asks for the compact depth so the panel cannot drift from what the
prompt carries. The only reason to render `detail=True` is to obtain a
`fact_id` for locating a fact — which is precisely what the
`memory_summary_detail` tool is for, so the dialog does not need it.

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
panel (**系统提示词注入体积（记忆摘要）**: a slider over the fixed gears
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
`memory_summary_detail` tool, and the settings editor.

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
    summary_token_limit: int = 1500
    user_md_token_limit: int = 800
    candidate_retention_days: int = 7
    max_retries: int = 3
    worker_poll_interval_sec: float = 0.5
    llm_extractor: Optional[Callable] = None
    privacy_filter: str = "private"
```

## Design

**Atomic facts are the single authoritative memory unit.** The memory summary
and `user_profile` are *derived views* rebuilt from facts.

- **Storage**: one SQLite file (WAL) via stdlib `sqlite3`; schema lives in
  `migrations/` (`001_init.sql` creates `facts`, `fact_candidates`,
  `user_profile`, `events`, `task_queue` plus the `facts_fts` /
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
  "maximally different in age". See [Recency](reinforcement.md#recency).
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

### Memory types in the summary

Every fact carries a `type` discriminator (`semantic` / `procedural` /
`episodic` / `sop` / `decision_rule` / `few_shot` / `lesson`), and the summary
view's compact bucket groups the types by how each renders, so none is lost:

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
by `recall`, rendered in the summary, and their `fact_id` stays tracked in the
summary for consistency. A fact's full knowledge body is available through the
`content` field on `recall` results and the summary's detail depth. Knowledge
categories are extracted by both the rule engine (lesson / SOP / decision-rule
patterns) and — authoritatively — by the LLM extractor, which can also produce
`few_shot` and `type`-tagged candidates.

Provider behavior notes:

- **Procedural** — extraction splits ordered steps (numbered lists or
  `首先/然后/最后`), stored as `qualifiers.steps`; conflict is single-valued
  per workflow name (an updated step list goes through `replace`).
- **Episodic** — events are naturally many and independent, so different
  events never conflict; the time word is captured as `qualifiers.when`.
- `user_profile` reflects only `semantic` facts (it answers "who is the
  user"), so procedural workflows and events do not pollute the profile.

## Tests

```bash
pytest                          # full suite
pytest tests/test_integration.py -v
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
`summary` renderer in both depths (`tests/test_summary.py`: type grouping,
no `fact_id`/title/scores in the compact depth, type-rank fallback ordering,
multi-value folding, the token budget as a hard cap, tail-first trimming),
reuse reinforcement (`tests/test_reinforce.py` and
`tests/test_reinforce_algorithm.py`: the curve's monotonicity, concavity, local
linearity and bound; cooldown and session idempotency; decay; event-log replay
fidelity incl. suppressed events; `retrieved_only` staying inert; the
API/worker paths that produce events; and recency — half-life decay, the
relative shift and its cap, same-session ages staying near-identical, an
all-old set still spreading, and `last_used_at` beating `created_at`), and
the end-to-end pipeline (add/recall/replace/forget/summary/
idempotency, plus knowledge facts persisting `type` / `content` through recall
and the summary). Set `ATOM_MEMORY_REAL_EMBED=1` to enable the live-model
embedding test (needs one-time download).
