# API Contract — First Slice

These endpoints map 1:1 to methods on `hockey_scheduler.api.service.ApiService`.
A web framework can be mounted on top of the facade later without changing
domain logic. All responses are JSON. Errors use the structured error shape
below.

## Conventions

- IDs are opaque strings.
- Timestamps are ISO-8601 UTC.
- A successful response returns `200`/`201` with the resource.
- Errors return `4xx`/`5xx` with:

```json
{ "error": { "code": "slot_already_filled", "message": "Human readable." } }
```

Error codes used in this slice: `not_found`, `validation_error`,
`roster_locked`, `already_selected`, `not_enrolled`, `invalid_transition`,
`slot_already_filled`, `not_eligible`, `game_cancelled`.

## Game & roster

```http
GET   /games/{gameId}
GET   /games/{gameId}/roster
POST  /games/{gameId}/roster/select
PATCH /games/{gameId}/roster/{playerId}/status
```

- `POST /roster/select` body: `{ "player_ids": ["player_1", ...], "actor_id": "..." }`
- `PATCH /roster/{playerId}/status` body: `{ "status": "unavailable", "actor_id": "..." }`
  (used by a selected player to confirm or back out)

## Availability

```http
GET  /games/{gameId}/availability
POST /games/{gameId}/availability
```

- `POST /availability` body:
  `{ "player_id": "...", "availability_status": "unavailable", "response_source": "player|guardian|coach", "actor_id": "..." }`

## Substitutes

```http
GET  /games/{gameId}/substitutes
POST /games/{gameId}/substitutes/enroll
POST /games/{gameId}/substitutes/withdraw
POST /games/{gameId}/substitutes/{playerId}/offer
POST /games/{gameId}/substitutes/{playerId}/accept
POST /games/{gameId}/substitutes/{playerId}/decline
POST /games/{gameId}/substitutes/{playerId}/add-to-roster
```

- `enroll` body: `{ "player_id": "...", "actor_id": "..." }`
- `withdraw` body: `{ "player_id": "...", "actor_id": "..." }`
- `offer` body: `{ "actor_id": "...", "expires_at": "<iso>?" }`
- `accept` / `decline` body: `{ "actor_id": "..." }`
- `add-to-roster` body: `{ "actor_id": "..." }` (coach override; offers + accepts in one step)

## Roster status

```http
GET /games/{gameId}/roster-status
```

Example response:

```json
{
  "game_id": "game_123",
  "team_id": "team_456",
  "target_goalies": 1,
  "confirmed_goalies": 1,
  "open_goalie_slots": 0,
  "target_skaters": 15,
  "confirmed_skaters": 14,
  "open_skater_slots": 1,
  "substitutes_enrolled": 0,
  "status": "open_slot",
  "action_required": true,
  "message": "1 skater slot open. No substitutes enrolled."
}
```

## Coach roster controls

```http
POST /games/{gameId}/roster/lock
POST /games/{gameId}/roster/unlock
POST /games/{gameId}/cancel
```

Body: `{ "actor_id": "..." }`.

## UI states (Game Detail screen)

The facade supports the three required screen states:

- **Loading** — client concern; the facade is synchronous.
- **Empty** — `GET /roster` returns `[]` and `roster-status` reports
  `status = "draft"` with `message = "No players selected yet."`.
- **Error** — any structured error shape above; the screen shows the
  `error.message`.

## Named schedule scenarios (#378)

All four routes require a **signed-in session** plus server-side
`MANAGE_SCHEDULE`, **and** are bound to that session's persisted active
Program/Season/League tuple — see
[active-context-scoping.md](active-context-scoping.md#named-schedule-scenarios-378--381).
Audit actors come from the authenticated session, never a request field.

```http
POST /api/scheduler/scenarios
GET  /api/scheduler/scenarios
GET  /api/scheduler/scenarios/{scenarioId}
POST /api/scheduler/scenarios/{scenarioId}/commit
```

Create accepts a strict body:

```json
{
  "name": "Opening-week plan",
  "season_id": "season_1",
  "league_id": "league_1",
  "division_id": "division_1",
  "slot_ids": ["slot_1"],
  "constraints": {},
  "games_per_team": 12
}
```

Alternatively, the existing Division-only scope may omit `season_id` and
`league_id`. `constraints` carries the optional blackout/holiday dates,
`min_rest_hours` (start-to-start), `max_games_per_team_per_day`, and
`min_turnaround_minutes` (#390 — the ice-free interval measured from the
previous game's END, in minutes; zero, the default, is exactly the
pre-#390 behaviour). `POST /api/scheduler/draft` echoes the normalized
`min_turnaround_minutes` back on the proposal, and
`POST /api/scheduler/commit` must be sent the SAME `constraints` the
preview was generated with. The normalized `min_turnaround_minutes` is
bound into `draft_fingerprint` itself, so a dropped or changed reviewed
turnaround is refused as `preview_stale` **even when the resulting rows
are byte-for-byte identical** — echoing the value would leave a caller
free to Generate with a turnaround and Commit with `0`, which skips the
commit-time turnaround check entirely.
A commit whose reviewed row no longer meets the turnaround is refused with
`409 schedule_conflict` and `details.reason = "min_turnaround"`, naming
the blocking Game, the measured `gap_minutes` and the
`shortfall_minutes`.

`games_per_team` (#375) is the regular-season format: the number of games each
team is GUARANTEED, from which the per-opponent count is derived
(`base = G // (T-1)` against everyone, with `rem = G % (T-1)` opponents played
once more). It is refused with `400 validation_error` and
`details.reason = "games_per_team_infeasible"` when `teams x games` is odd —
every game contributes 2 to the league-wide count, so no construction can then
give every team exactly G — and the message names the nearest achievable counts
(`G-1`, `G+1`) in `details.nearest_achievable`.

`meetings_per_opponent` is the LEGACY spelling (how many times each team plays
every other) and remains accepted, because stored scenarios replay under it and
it is the only way to say "play everyone once" without knowing a Division's
size. **Sending both is refused** with
`details.reason = "schedule_format_conflict"`: they cannot be reconciled when a
League's Divisions differ in team count. A proposal echoes exactly one of them
and `null` for the other. A scenario records the values the generator actually
applied and **replays that same format at commit**.

The response includes immutable `name`, `scope`, `planner`
fingerprints/version, `request_input` (carrying the resolved format), the
opaque `proposal`, and the full `generation_snapshot`. Commit takes an empty
body. It creates unpublished draft Games only when the current material-input
fingerprint still matches; otherwise it returns `409 concurrency_conflict` with
`details.reason = "schedule_scenario_stale"`, section-level `changed_inputs`,
and `required_action = "generate_new_scenario"`. Publishing remains
`POST /api/scheduler/drafts/publish`.

Scope refusals are deliberately **non-oracular**:

| request | answer |
| --- | --- |
| inside the active exact tuple | the normal response |
| a scenario id outside it (get / commit) | `404 not_found`, `schedule_scenario_missing` — byte-identical to a scenario id that never existed once the echoed `scenario_id` is masked |
| create naming a Division outside it | `404 not_found`, `division_missing` — the same body a nonexistent `division_id` produces |
| create naming a Season+League outside it | `404 not_found`, `league_season_missing` — the same body an unlinked/nonexistent pair produces |
| create naming a Season+League outside it **plus any `division_id`** | the same `league_season_missing` body, whether that Division is real, foreign, or invented. The tuple is judged the instant the LeagueSeason link resolves, so the later `division_missing` refusal is never reached from outside the tuple — otherwise the pair of answers reports whether a guessed `(season_id, league_id)` is genuinely linked |
| list | `200` containing **only** the active exact tuple's scenarios |
