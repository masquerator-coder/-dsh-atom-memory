# Reinforcement and recency

The two signals that let "what the user keeps coming back to" and "what is most
current" beat "what was written down first" — and the reasons each is shaped the
way it is. Owner: [`atom_memory/reinforce.py`](../atom_memory/reinforce.py),
[`atom_memory/db.py`](../atom_memory/db.py), [`atom_memory/retriever.py`](../atom_memory/retriever.py).

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
