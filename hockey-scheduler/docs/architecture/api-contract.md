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
  `status = "draft"` with `message = "No players selected yet."`. This is a
  claim that the roster **is** empty, and it is therefore never used to
  express withheld data — see *Private game visibility* below.
- **Error** — any structured error shape above; the screen shows the
  `error.message`.

## Private game visibility (#427)

`board`, `lineups`, `roster-status`, `roster`, `substitutes`,
`availability-summary`, `substitute-candidates` and `substitute-addable` —
every leaf of the `/api/games/{id}/…` dispatch that carries private player
data — expose one game's **private** state. Passing the session +
participation gate proves the caller belongs to *a* team in the game; it does
**not** decide *which side* they may read. The server resolves the caller's
own side once (`game_scoped_own_team_id`) and every one of these routes is
projected on it.

**Admission and projection are one decision, taken once.**
`services.game_side_scope.resolve_private_game_read` decides whether the
caller is admitted *and* resolves their trusted side, against a **single**
fetch of the game, and the dispatch carries that one record into every leaf.
`can_read_private_game_data` remains only as a fast-denial preflight — it is
literally `resolve_private_game_read(...).admitted`, so the two cannot answer
differently — and it is no longer the authoritative gate. Previously the
handler authorised, then independently re-fetched the game and re-resolved
membership; a membership transferred, ended or deactivated **between** those
two reads left the second one with no side, which fell through to `/board`'s
home default and answered the caller **HOME's** private pool, status,
notifications and audit stream. Losing an authority now produces a refusal,
not a fallback.

**The home default is audience-bound.** `/board` answers one side and still
defaults to home — but only for an audience with no side of its own *by
design*: an unscoped operator, an assigned official (whose default side is
served through the submitted-lineup projection, so nothing widens), or an
in-process caller with no role. A Coach or Player who arrives with a missing
or nonparticipant side is `restricted: true` with `team_id: null` — the
response does not name the side it declined to answer for.

**A side is never trusted from the query string or the body** — a `?team_id=`
naming the opponent is *ignored* for a scoped caller, so a hinted request
returns exactly what the un-hinted one returns. It is deliberately not
rejected: a 403 raised only for the opponent's id is itself an oracle for
which team is playing, while an unchanged own-side answer discloses nothing.

**And `?side=` is not a parameter this server has at all**, which is a
different and stronger fact than "it is ignored" — stated separately because
the two were run together here, and the sentence that ran them together was
what a round of verification walked through. Measured from the source rather
than asserted: `web/server.py` reads exactly SEVEN query-string parameters
(`team_id`, `season_id`, `scope_type`, `scope_id`, `recipient_ref`,
`actor_type`, `actor_ref`), enumerated from its own `parse_qs` call sites by
`web/route_extract.query_parameter_names`. `side` is not among them, so
`?side=away` reaches no branch — and the behavioural sweep now derives its
hint variants from that inventory, so a parameter this server BEGINS reading
enters the sweep instead of being tested under a name nothing consumes.

| Caller | board / lineups | roster-status | roster | substitutes | availability-summary | substitute-candidates / -addable |
|---|---|---|---|---|---|---|
| League Admin | both sides, full | full | full | full | any side (hint honoured) | any side (hint honoured) |
| Arena Manager | both sides, full | full | full | full | any side (hint honoured) | **403** (no `MANAGE_ROSTER`) |
| Coach | own side; opponent `restricted` | own side only | own side's durably attributed rows | own side's durably owned rows | own side only (hint ignored) | own side only (hint ignored) |
| Player | own side; opponent `restricted` | own side only | own side's durably attributed rows | own side's durably owned rows | own side only (hint ignored) | **403** (no `MANAGE_ROSTER`) |
| Assigned official | both sides' submitted lineup | **403** | both sides' submitted lineup | **403** | **403** | **403** |
| Viewer | **403** | **403** | **403** | **403** | **403** | **403** |

**"Submitted lineup" means the rows that OCCUPY a slot**, not the rows a
screen happens to group under *selected*. `_lineup_rows` keeps a seated
player in that group after they have gone unavailable or been removed, marked
`backed_out: true`, so their own coach can still see the row for cleanup —
that is the side's roster *history*, not its current sheet, and an official
receives none of it. Occupancy is `RosterEntryStatus.occupies_slot`
(`selected` / `confirmed` / `offered` / `accepted`), the same predicate slot
accounting already uses; `backed_out` is invariantly `false` in an official's
rows. The rule is applied in one place, `_submitted_lineup_rows`, which
`/board`, `/lineups` and `/roster` all share.

The last two carry a second, independent gate that the other five do not:
`MANAGE_ROSTER`, a **role capability** ("may this kind of caller manage a
roster at all"), checked before the side question is asked. That is why a
Player is refused there but served their own side everywhere else — and, for
the same reason, why an **Arena Manager is refused there too** although it is
an unscoped operator admitted to both sides everywhere else in this family.
`Role.ARENA_MANAGER` holds `MANAGE_ARENA`, `MANAGE_SCHEDULE` and `VIEW`, not
`MANAGE_ROSTER`, so the two operator roles are NOT interchangeable on these
two leaves; the row above said they were until the measurement in
`test_authenticated_side_noninterference
.TheArenaManagerIsAnOperatorWithoutRosterAuthority` was taken. That test now
pins the whole ten-leaf matrix for both roles, so this table and the product
cannot drift apart again silently. The two gates are deliberately not folded
together — the side rule must not be
contingent on a permission table that can change without it, so the facade
refuses an audience with no claim on a private candidate pool even though the
capability gate makes that unreachable over today's HTTP dispatch.

Two rules govern how withheld data is represented, and both exist because an
empty collection is an *operational claim about the game* rather than a
statement about the reader:

- **Never `[]`, never `0`.** A redacted lineup side carries
  `restricted: true` with `players: null` and `status: null`. A route with no
  readable projection for the caller answers **403 `forbidden`**.
- **Nothing is counted that was not sent.** `board.audit_scope` is `full`,
  `own_side` or `withheld`; `notifications`, `audit` and `audit_count` are all
  `null` when `withheld`, and `audit_count` otherwise counts the rows actually
  returned, so it cannot report the size of what was omitted.

An event is retained for a side only when **every player identity it discloses
is durably attributed to that side, and it discloses at least one**.
Attribution comes from the stored `GameRosterEntry.team_side` and
`SubstituteEnrollment.team_id` — never live membership and never
`Player.team_id`. A legacy row with NULL attribution names no side, so it is
withheld from **both** rather than guessed onto one.

### Per-side private state outside the `/games/{id}/…` family (#205)

The boundary is the *state*, not the path. Two routes outside that dispatch
carry a per-side private value:

```http
GET /api/demo/overview    ->  schedule[].roster_status
GET /api/me/player-home   ->  next_game.team_status
```

`roster_status` is the same per-side operational enum `roster-status`
returns, and it is **not** public — `/api/public/schedule`,
`/api/public/games/{id}` and this payload's own `public_fixtures` all omit it.
It used to be computed with no side at all, so `RosterService`'s
`team_id or game.home_team_id` default served the **home** side's value to
every reader — including a coach whose team plays in the game as the away
side, and a coach whose team is not in the game at all.

The schedule is a **cross-game list**, so the side is resolved **per row**
(`game_scoped_own_team_id` against that row's game) and classified by the same
`route_audience` the family uses:

| Caller | `schedule[].roster_status` |
|---|---|
| League Admin, Arena Manager | home side, unchanged; `roster_status_team_id` now names it |
| Coach, Player — in that game | **their own side's** value |
| Coach, Player — not in that game | **omitted** |
| Assigned official | **omitted** |
| Guardian, viewer | **omitted** |

A withheld row **omits the `roster_status` key entirely** and carries
`roster_status_restricted: true` with `roster_status_team_id: null`. The
marker is not decoration: every consumer of this field asks
`["roster_confirmed", "locked"].includes(g.roster_status)`, which a missing key
and a `null` both answer `false` — rendering as "Roster open" / "not
confirmed", i.e. the withheld state expressed as an *empty operational state*.
The flag is what lets a screen say "not shown" instead. It is present on every
row, entitled or not, so a consumer never infers withholding from a missing
key. `roster_status_team_id` names which side the value describes on entitled
rows.

An **assigned official is withheld here** even though the family serves them a
`submitted_lineup` projection, because this route has no per-game assignment
gate: its schedule lists every game in the active Program/Season/League, so
honouring that projection would serve an official private state for games they
were never assigned to — strictly *wider* than the family. There is also no
honest single-side answer (one enum, a two-sided entitlement), and the value it
would carry is `needs_substitute`, which `_submitted_lineup_status` neutralises
by name one route away.

#### `GET /api/me/player-home` — the subject's own *resolved* side

`next_game.team_status` is that same per-side enum again, relabelled for the
Player Home Page: `needs_substitute` renders as `"sub_search"` and `open_slot`
as `"short"`. `GET /api/me/guardian/home` carries it too — that route returns
each verified junior's Player Home payload — so every rule here applies to it
unchanged.

The subject is **always the signed-in caller** (or their verified junior),
resolved from the session scope and never from a query parameter, so there is
no audience to classify and no client hint to ignore. The only question is
*which side of the game the subject is on*, and the answer is the same
`game_scoped_own_team_id` resolution the family uses — **never**
`Player.team_id`, the permanent pointer, which a mid-season transfer leaves
stale for a particular game in either direction.

Entitlement therefore comes from **authoritative game-scoped membership**, and
a pointer that has outlived the membership grants nothing:

| pointer / membership for this game | `next_game` |
|---|---|
| pointer HOME, membership AWAY | the **away** side |
| pointer names a team in neither game, membership HOME | the **home** side |
| pointer HOME, membership moved to another team | **no BOUND game, neither side** (an unbound exhibition the pointer still names is unaffected) |
| pointer HOME, membership ended or absent | **no BOUND game, neither side** (an unbound exhibition the pointer still names is unaffected) |
| pointer and membership agree | that side, unchanged |
| no player binding on the session (including an unscoped operator) | the empty stub |

**Game selection follows the same authority.** `next_game` and `today_count`
choose *which games* the page is about through the game-scoped membership
resolution as well, because a player handed the wrong game cannot be rescued
by resolving the side correctly within it — and the availability POST the
screen offers is addressed to `next_game.game_id`. For a **mover** this
changed three things at once, and only the first is a privacy fix:

| | before | now |
|---|---|---|
| `next_game.team_status` | the **pointer** team's private per-side enum | the subject's own side's |
| `next_game.team_name` / `opponent_name` | inverted — the opponent named as their team | the resolved side, named correctly |
| which games are listed | games the **pointer** team plays; a mover on a new team saw none, a departed player saw their old team's | games the subject's membership actually puts them in |

A game with **no LeagueSeason binding** — an exhibition, or an unbound legacy
row — has no membership authority, so the permanent pointer remains both the
side *and* the selector there, unchanged.

### The side rule is machine-enforced (#205)

The four defects above were each found by hand, one round at a time, and each
was the same shape: a private-state read reaching a side by default or by a
client hint instead of by the server's resolution. That rule is now a build
gate — `backend/hockey_scheduler/services/side_provenance.py`, driven by
`backend/tests/test_side_provenance_guard.py`. It fails on any read of
roster / availability / substitute / audit state whose side did not come from
`game_scoped_own_team_id` or an adjudicated decision, on any new
`x or <game>.home_team_id` default, and on any new leaf of the
`/api/games/{id}/…` dispatch. Its accepted-site ledger is empty and may only
shrink; the legitimate cases (unscoped-operator defaults, the live-membership
discoveries, the create-state side) are documented exemptions whose conditions
are checked rather than asserted.

### Which side a substitute enrollment belongs to

`SubstituteEnrollment.team_id` — the side the row was **admitted** on — and
nothing else. This is one rule with three consequences, because attribution
and liveness are different questions asked of the same row:

- **Served** — `substitutes` and `substitute-candidates` are two views of one
  resource and name the **same** rows. A NULL-owner row appears in neither.
  The two are also answered for the same side by the same rule, so they cannot
  disagree about *whose* rows they are either.
- **Counted** — `substitutes_enrolled` and `substitutes_available` count only
  rows this game durably attributes to the side *and* whose occupant is still
  a live member of it. Attribution is durable so a transfer cannot move an
  existing row into the opponent's count; liveness is live so a candidacy that
  has ended drops out of the count immediately, while the row itself stays
  visible to its owning coach for cleanup.
- **Actionable** — a **team-scoped** actor cannot transition a row whose
  admitting side is unknown: `offer`, `accept` and `add-to-roster` answer
  **403** with `reason: "attribution_missing"`, alongside `withdraw` and
  `decline`, which already did. This matters because `offer` *writes*
  `team_id`, so permitting it would mint an admitting side out of today's
  membership and make the guess durable. An unscoped operator claims no side
  and is unaffected — they remain the path by which a legacy row is repaired.

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

The number guaranteed is the team's **final season total**, counting the
non-cancelled Regular Games already in scope, so a draft generates only the
games still MISSING and `already_scheduled[]` reports every counted existing
Game (one row each, carrying that Game's own home/away). Two further
`400 validation_error` refusals cover the cases the existing Games make
impossible — both raised before any placement or persistence, so a refused
Generate *or* Commit writes nothing at all:

* `details.reason = "games_per_team_over_scheduled"` — at least one team
  already plays more than `G`, and generation can only add. `details` carries
  `over_scheduled_teams[]` (`team_id`, `team_name`, `existing_games`).
* `details.reason = "games_per_team_residual_infeasible"` — some team needs
  more games than every other team combined can still supply, so the remaining
  games would have to be played against teams that have reached their own
  total. `details` carries `short_teams[]` (`team_id`, `team_name`,
  `residual_games`, `available_games`).

Both carry `details.nearest_achievable`: the smallest accepted `games_per_team`
these existing Games can still be completed to, or `null` when none can.

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
