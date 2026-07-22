# Ice Availability Builder (#158)

An arena operator builds a **draft ice inventory** from a recurring weekly
template, previews the exact slots, then explicitly and idempotently commits them
as available Game ice. No games or published schedule are created here — the
scheduling planner (#206) consumes this inventory later. This is the first
scheduler foundation: "Generate schedule" never silently creates permanent ice.

## Model

- A generated slot's `[start, end]` is its **playable game window**
  (`playable_minutes`) — a clean window the existing draft scheduler already
  consumes (`services/scheduler.py:_available_game_slots`). Consecutive slots on a
  day are spaced by `playable + turnover`, so a turnover gap sits between games.
- **Reserved facility time** (the contracted window) is surfaced for display
  only; it is not persisted per slot. The deeper turnover/curfew model — per-Rink
  policy defaults, hard curfew end-by rules, and applying buffers to
  move/reschedule and the planner — is **#277**, delivered after this.
- Every slot targets a specific **Rink**. The Rink's Venue must hold active
  `SeasonVenueAccess` for the Season (`services/league_scope`); a rink without it
  is reported (with a remediation route) and produces no slots. The builder never
  grants access — that stays a `MANAGE_SETUP` action.
- All wall-clock arithmetic is done in the Season's **Program timezone**
  (`Season` has no timezone of its own) and stored as UTC instants, reusing the
  `parse_season_boundary` / `resolve_timezone` conventions. **Season dates are
  never mutated** — they are read as the default range only.

## Engine

`services/ice_availability.py` — a pure, deterministic planner
(`plan_ice_windows`): iterate each date in `[start, end]`, keep selected
weekdays, drop exclusion dates (recorded with a reason), and within each day's
local window step by `playable + turnover`, emitting a slot per full playable
block. A selected day whose window cannot host one playable game is reported as
`too_short`. Guard rails cap the range (`MAX_RANGE_DAYS`) and total slots
(`MAX_WINDOWS`).

## API

Both routes require `MANAGE_ARENA` (like `/api/setup/ice-slot` and the
rinks/ice-slots import). Request body:

```
{ season_id, rink_ids:[...], weekdays:[0..6],   # Mon=0 .. Sun=6
  start_local:"HH:MM", end_local:"HH:MM",
  start_date:"YYYY-MM-DD"|null, end_date:"YYYY-MM-DD"|null,  # null => Season range
  playable_minutes, turnover_minutes, exclusion_dates:["YYYY-MM-DD", ...] }
```

- `POST /api/setup/ice-availability/preview` — computes proposed slots classified
  as `new` / `duplicate` / `conflict`, per-rink counts, capacity-in-games,
  reserved vs playable minutes, skipped exclusion dates, too-short days, and any
  venue lacking access. **Writes nothing.**
- `POST /api/setup/ice-availability/commit` — re-derives the same plan and creates
  only the `new` `AVAILABLE` `GAME` slots, server-attributed and audited
  (`ice_slot_created` per slot + a batch-level `ice_availability_committed`).
  Requires an active Season.

## Idempotency

Dedupe is by the natural `(rink_id, start_time, end_time)` tuple (the same
approach as the rinks/ice-slots importer): an exact match is a **duplicate**
(skipped, never re-created or overwritten); an overlap that is not an exact match
is a **conflict** (reported, never overwritten). Re-running the same template
therefore creates nothing new.

## UI

Mounted in the Arena Calendar (a `🧊 Build ice` toolbar button, `MANAGE_ARENA`),
reusing the calendar's render-override pattern. A **Month** view was added
alongside the existing Day/Week views. See `web/static/app.js`
(`renderIceBuilder`, `renderMonth`) and `e2e/ice-availability-builder.js`.
