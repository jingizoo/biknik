# Scheduling policy — turnover buffers, minimum playable time, curfew (#277)

Operational placement rules for game ice: pre-game warm-up reservation,
post-game resurfacing/turnover, a minimum playable span, and a hard end-by
curfew. One optional `SchedulingPolicy` row per **Program**, **Season**, or
**Rink**; the effective policy for a placement resolves **field by field**
with Rink overriding Season overriding Program. A `null` field always means
"inherit from the next scope up", and a field unset at every scope is the
no-op default — installs that never configure a policy behave exactly as
before this feature existed.

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

All three checks run inside `SetupService._assert_slot_free` — the single
shared placement gate — so `create_game`, `move_game`, and both
`commit_draft_schedule` implementations reject identically (no draft-only
exception), each with a stable machine-readable `details["reason"]`:

* `insufficient_playable_time` — the slot's playable span is shorter than
  the effective minimum. Contracted slivers import untouched (with
  validation warnings); they are refused a *game* here instead.
* `turnover_buffer_conflict` — another active game's slot on the **same
  rink** sits closer than `warmup + resurfacing` minutes. Committed drafts
  count. Half-open boundary: a gap **exactly equal** to the buffer is
  compliant. Game-vs-game only; buffers against non-game slots
  (maintenance, public skate) belong to the #189 event model.
* `curfew_violation` — the playable end passes the curfew instant,
  compared as true UTC instants (deterministic across DST; the ambiguous
  fall-back wall clock resolves to its earlier occurrence). Anchoring: an
  afternoon/evening curfew (`>= 12:00`) is a deadline on the slot's local
  start date — a slot that merely *starts* past it violates; a small-hours
  curfew (`< 12:00`, e.g. an `01:00` building close) means the morning
  after an afternoon/evening start, but **that same** morning for a slot
  itself starting in the small hours. Ending exactly at curfew is
  compliant.

The draft scheduler mirrors the same checks as an **advisory** during slot
assignment (one shared implementation, `_slot_policy_violation`): a slot the
commit gate would reject is skipped, and the code surfaces in the proposal's
`unscheduled[].reason_codes` — including against the run's own tentative
same-rink picks. The gate stays authoritative.

## Concurrency

Policy reads are plain reads inside the placing transaction, after its
existing Team→Rink→Season locks — no new lock, no new deadlock shape; the
same-rink buffer scan is serialized by the rink row lock every placement
already holds. The write path (`set_scheduling_policy`) serializes racing
upserts on the scope row's own `FOR UPDATE` lock, with the
`(scope_type, scope_id)` unique index as a belt-and-braces backstop.
Deleting a Program/Season/Rink cascade-deletes (and audits) its policy row.

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
