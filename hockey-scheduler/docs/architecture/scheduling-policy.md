# Scheduling policy — turnover buffers, minimum playable time, curfew (#277)

Operational placement rules for game ice: pre-game warm-up reservation,
post-game resurfacing/turnover, a minimum playable span, and a hard end-by
curfew. One optional `SchedulingPolicy` row per **Program**, **Season**, or
**Rink**; the effective policy for a placement resolves **field by field**
with Rink overriding Season overriding Program. A `null` field always means
"inherit from the next scope up", and a field unset at every scope is the
no-op default — installs that never configure a policy behave exactly as
before this feature existed, with one deliberate exception: two games can
never share physically overlapping slots on one rink
(`slot_overlap_conflict` below), a rule that binds regardless of any
policy.

## Model

| Field | Meaning |
|---|---|
| `warmup_minutes` | Ice reserved **before** the playable start (pre-game warm-up). |
| `resurfacing_minutes` | Ice reserved **after** the playable end (resurfacing, bench change). |
| `min_playable_minutes` | Minimum playable span a slot must offer to host a game. |
| `curfew_local` | `"HH:MM"` wall-clock end-by bound, evaluated in the slot's **venue** timezone (Program timezone fallback). |

An `IceSlot`'s stored `[start_time, end_time]` remains the **playable** span.
The **reserved** facility span is derived at read time as
`[start - warmup, end + resurfacing]` — imported contracted-ice rows are
never rewritten, and setting a policy never time-shifts existing data.

## Enforcement

All four checks run inside `SetupService._assert_slot_free` — the single
shared placement gate — so `create_game`, `move_game`, and both
`commit_draft_schedule` implementations reject identically (no draft-only
exception), each with a stable machine-readable `details["reason"]`:

* `insufficient_playable_time` — the slot's playable span is shorter than
  the effective minimum. Contracted slivers import untouched (with
  validation warnings); they are refused a *game* here instead.
* `slot_overlap_conflict` — the candidate slot **physically overlaps**
  another active game's slot on the same rink. Refused **unconditionally**
  — a zero or absent policy changes nothing, and the rule binds even for
  season-less legacy games where no policy scope resolves at all — because
  the import path deliberately persists overlapping contracted rows
  (warnings, never silent rewrites), making this gate the
  physical-exclusivity enforcement point. Exact adjacency (`end == start`)
  is *not* overlap and stays compliant even at a zero requirement. This is
  the one rule that applies even in installs that never configured a
  policy.
* `turnover_buffer_conflict` — another active game's slot on the **same
  rink** sits closer than the **directional** requirement: the required gap
  between two games is the *earlier* game's `resurfacing_minutes` plus the
  *later* game's `warmup_minutes`, each resolved from **that game's own**
  effective policy (its rink + its season — neighbors from another season
  sharing the rink contribute their own side). The two fields on the
  irrelevant side never block: a candidate placed *after* an existing game
  is never refused for its own `resurfacing_minutes` or the neighbor's
  huge `warmup_minutes`, only for the neighbor's resurfacing plus its own
  warm-up. Committed drafts count. Half-open boundary: a gap **exactly
  equal** to the requirement is compliant. Physically overlapping spans
  never reach this check — `slot_overlap_conflict` above refuses them
  first. Game-vs-game only; buffers against non-game slots (maintenance,
  public skate) belong to the #189 event model.
* `curfew_violation` — the playable end passes the curfew instant,
  compared as true UTC instants (deterministic across DST; the ambiguous
  fall-back wall clock resolves to its earlier occurrence, and a curfew
  wall time skipped by spring-forward pins to its normalized instant —
  e.g. an `02:30` curfew on the US spring-forward night means 03:30 CDT).
  Anchoring is per **operating day**: an afternoon/evening curfew
  (`>= 12:00`) is a deadline on the slot's local start date — a slot that
  merely *starts* past it violates. A small-hours curfew (`< 12:00`, e.g.
  an `01:00` building close) ends the operating day that began the
  *previous* evening: a slot starting **at or before** the curfew wall
  clock is in that closing night's small hours and is bound to **that**
  date's instance (a `00:30` start violates tonight's `01:00` close;
  starting exactly at curfew violates), while a slot starting **after**
  the curfew wall clock (a morning practice, an evening game) belongs to
  the operating day ending at the **following** date's instance — so an
  `01:00` close never outlaws daytime ice. Ending exactly at curfew is
  compliant.

The draft scheduler mirrors the same checks as an **advisory** during slot
assignment (one shared implementation, `_slot_policy_violation`): a slot the
commit gate would reject is skipped, and the code surfaces in the proposal's
`unscheduled[].reason_codes` — including against the run's own tentative
same-rink picks. The gate stays authoritative.

## Concurrency

Every policy **scope row** a placement's gate reads is locked before the
read — the candidate's chain AND each same-rink neighbor game's Season and
Program (the directional buffer resolves the neighbor's own policy). The
global lock order is **Program → Team → Rink → Season**, agreeing with the
ice-availability builder and hierarchy import (both lock
Program → Rink → Season; taking the Program after the Rink instead is a
real ABBA deadlock against a concurrent builder commit — reproduced on
PostgreSQL and pinned by `test_placement_concurrency`'s
builder-vs-placement races). Because the full scope set (neighbor seasons
included) is only *discoverable* by reading, each placement runs a plain
**pre-lock locator** (`_policy_scope_lock_plan`), locks the planned
Programs first and every involved Season in one sorted batch, then
**re-verifies the plan under the locks** — any drift (a game landing on
the rink mid-flight, a Season reparented to an unlocked Program) refuses
with the retryable `placement_raced` instead of reading a scope row the
transaction does not hold; `create_game` retries that signal in a fresh
transaction (as `move_game`'s existing race harness already does), so
callers still receive the precise terminal answer.

The write path (`set_scheduling_policy`) serializes on the scope row's own
`FOR UPDATE` lock, so a policy edit and an in-flight placement reading
that scope — either side of the candidate/neighbor split — are strictly
ordered: one sees the other's outcome, never a torn read. A policy writer
holds exactly **one** row of the chain, so it can never deadlock the
multi-lock placement path; the `(scope_type, scope_id)` unique index
backstops racing upserts. The draft scheduler's *advisory* runs lock-free
by design — the commit gate stays authoritative. Deleting a
Program/Season/Rink locks the scope row first and holds it through the
cascade (serializing with any racing `set_scheduling_policy`), then
cascade-deletes (and audits) its policy row — a policy row can never
orphan-survive its scope. All of the above is pinned by *forced* races on
PostgreSQL: the racing thread is released only while the placement/delete
provably holds its locks, so the serialization claims are falsifiable, not
timing-dependent.

## API

`MANAGE_ARENA` (both operator roles), server-attributed, audited
(`scheduling_policy_set` / `scheduling_policy_cleared`).

### `POST /api/setup/scheduling-policy`

```json
{"scope_type": "rink", "scope_id": "rink_1",
 "warmup_minutes": 5, "resurfacing_minutes": 10,
 "min_playable_minutes": 45, "curfew_local": "22:30"}
```

Strict schema; a set **replaces the row wholesale** (it is a settings form,
not a patch): an omitted/`null` knob means "inherit", and all-`null` clears
the row entirely. Unknown keys are a 400 (`unknown_field`) — never silently
dropped, which under replace-wholesale semantics would otherwise clear the
mistyped knob. Responds with the stored row (`"policy": null` after a
clear).

### `GET /api/setup/scheduling-policy?scope_type=&scope_id=[&season_id=]`

Returns the raw stored row, plus — for a `rink` scope with a `season_id` —
the resolved `effective` values and `effective_sources` (which scope each
set field came from), for the settings UI's "inherited from …" affordance.

## Validation reasons

`unknown_policy_scope`, `policy_scope_missing`, `invalid_warmup_minutes`,
`invalid_resurfacing_minutes`, `invalid_min_playable_minutes`,
`invalid_curfew_local`.
