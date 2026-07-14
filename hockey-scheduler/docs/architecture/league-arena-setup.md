# Competition + Facility Setup — Data Model & API

Organization, competition and facility entities used to create games. Implemented
in `services/setup_service.py` and exposed via `api/service.py`. Same layered
architecture as the roster slice: pure service logic over the store, injected
clock, structured errors, audited writes.

> **Terminology (epic #233).** This document uses the **canonical vocabulary** from
> [ADR 0001 — Competition model reset](decisions/0001-competition-model-reset.md):
> **Program → Season → League → optional Division**, Team permanent to a Program,
> optional Club, and Organization-owned Venues shared with Seasons. This is a
> **terminology + docs** change (epic Slice B1): the running code and the **v1**
> API still use the legacy names during the transition — today's **League** is the
> new **Program**, today's **Level** is the new **League** — and the schema plus the
> **v2** API land in **Slice C**. See the ADR for the exact old→new column/route map
> and the forward-only migration plan; the legacy names are called out inline below.

## Hierarchy (canonical)

```text
Organization ──< Venue ──< Rink ──< IceSlot     (facility owner owns the building)
Organization ..< Program                        (optional: an org may operate programs)

Program ──< Season ──< League ──< (optional) Division
Program ──< Team   >── (optional) Club

Season >──< Venue          (many-to-many: SeasonVenueAccess — a Season's eligible ice)
SeasonTeamRegistration >── Season, Team, League, (optional) Division
Game   >── Season, League, (optional) Division, home Team, away Team, IceSlot
```

- A **Program** is the permanent competition umbrella (e.g. *Adult Men*, *High School*).
- A **League** is a season-specific grouping within a Program/Season (e.g. *Adult
  League*, *Junior League*; *Varsity League*, *Freshman League*). *(Legacy name:
  **Level**.)*
- A **Division** keeps its name — only its parent changes (now a **League**, was
  Season/Level) and it becomes **optional**, an extra split of a League (e.g.
  *Gold*/*Silver*/*Diamond*, or *North*/*South*). *(Issue #245: Gold/Silver/Diamond
  are Division examples, never League examples — an earlier doc revision had this
  reversed.)*
- A **Team** belongs permanently to one **Program**; its per-season placement
  (League + optional Division) is a **SeasonTeamRegistration**, not a field on the Team.
- **Club** is a team affiliation. It becomes **optional end-to-end in Slice D** (a team
  may then have none, with no placeholder Club); **today the v1 `create_team` still
  requires a valid `club_id`**.
- A **Venue** is owned by an **Organization** (facility owner) and made available to a
  Season via **SeasonVenueAccess** — one Season may use several Venues and one Venue
  may host several independent Programs/Seasons.

## Entities

Column names below are the **target**; where the live schema still carries a legacy
name (until Slice C) it is noted. See ADR 0001 for the full column map.

### Program *(legacy table/entity: `League` / `leagues`)*
| Field | Type | Notes |
| --- | --- | --- |
| id | str | `league_…` today (id preserved across the rename) |
| name | str | required |
| country / timezone | str | optional; IANA tz, default `UTC` |
| operator_organization_id | str? | optional operating org *(legacy: `organization_id`)* |

### Season
| Field | Type | Notes |
| --- | --- | --- |
| id | str | `season_…` |
| program_id | str | parent Program *(legacy column: `league_id`)* |
| name | str | required, e.g. "2026/27" |
| start_date / end_date | datetime? | timezone-aware UTC; end ≥ start |

### League *(legacy table/entity: `Level` / `levels`)*
| Field | Type | Notes |
| --- | --- | --- |
| id | str | season-specific grouping |
| season_id | str | parent Season |
| name | str | required, e.g. "Adult League" |
| sort_order | int | display order |

### Division (optional)
| Field | Type | Notes |
| --- | --- | --- |
| id | str | `division_…` |
| league_id | str | parent League *(legacy: `season_id` + optional `level_id`)* |
| name | str | required, e.g. "Gold" or "North" |
| age_group | str | optional |

### Team / Club
- `Club { id, name, country }` — affiliation.
- `Team` belongs permanently to a **Program** *(legacy column: `league_id` → `program_id`)*.
  Its `club_id` is **required in v1 today** and becomes **nullable in Slice D**. Season
  placement is a SeasonTeamRegistration.

### SeasonTeamRegistration
| Field | Type | Notes |
| --- | --- | --- |
| id | str | one per `(season_id, team_id)` |
| season_id / team_id | str | the Team's participation in that Season |
| league_id | str | the Season's League the team plays in *(added in Slice C)* |
| division_id | str? | optional Division within that League |
| active | bool | participation status |

### Organization / Venue / Rink / IceSlot
- `Organization { id, name, short_name, ... }` — facility owner; may also operate Programs.
- `Venue { id, name, address, timezone, organization_id? }` — owned by an Organization.
  *(Season access `SeasonVenueAccess` is added in **Slice E1**; the legacy
  `Venue.league_id` permanent ownership is removed in **Slice E2**.)*
- `Rink { id, venue_id, name }`
- `IceSlot { id, rink_id, start_time, end_time, slot_type, status }`
  - `slot_type`: `game | practice | tournament | maintenance | public_skate`
  - `status`: `available | allocated | blocked` (→ `allocated` when a game is created)

### SeasonVenueAccess *(new — Slice E1)*
`{ id, season_id, venue_id, active }` — the Season's eligible ice (many-to-many).
Added in Slice E1; it replaces the permanent Program/League→Venue ownership that
Slice E2 then removes.

### Game (extended)
Carries `season_id`, **`league_id`** *(added in Slice C)*, `division_id?`, and
`ice_slot_id`. A manually-created game copies `start_time`/`end_time` from the ice slot
and the rink name, and is immediately usable by `RosterService`.

## Rules

- A child entity's parent must exist, else `not_found`.
- Names are required (non-blank), else `validation_error`.
- Ice-slot times must be timezone-aware UTC and `end > start`, else `validation_error`.
- A registration's League (and Division when set) must resolve to the **same
  Season/Program** as the Team, else `validation_error`.
- A team cannot play itself, else `validation_error`.
- Both teams must match the game's League/Division, else `division_mismatch`.
  `allow_division_override` is a **compatibility no-op** today — it is accepted for
  payload stability but does **not** permit cross-division games.
- One active game per ice slot, else `schedule_conflict`.
- Every create appends a `SetupAuditLog` entry.

## API

Two versioned surfaces (ADR 0001): **v1 `/api/setup/…`** keeps today's legacy
vocabulary for the deprecation window; **v2 `/api/v2/setup/…`** uses the canonical
names and lands with the schema in **Slice C**. Exact route/payload matrix is in the
ADR.

```http
# v1 (current, legacy names — frozen during the deprecation window)
POST /api/setup/league        create_league   (the umbrella; = Program)
POST /api/setup/season        create_season   (body: league_id, …)
POST /api/setup/level         create_level    (the grouping; = League)
POST /api/setup/division      create_division (body: season_id, level_id?)
POST /api/setup/team          create_team     (body: league_id, club_id, …)   # club_id required in v1
POST /api/setup/venue         create_venue    (body: organization_id?, league_id?)
POST /api/setup/game          create_game     (body: season_id, division_id, …)

# v2 (canonical) — the entity routes below land with the schema in Slice C
POST /api/v2/setup/program     (body: operator_organization_id?, …)
POST /api/v2/setup/season      (body: program_id, …)
POST /api/v2/setup/league      (body: season_id, …)          # the new League
POST /api/v2/setup/division    (body: league_id, …)
POST /api/v2/setup/team        (body: program_id, club_id, …)   # club_id nullable in Slice D
POST /api/v2/setup/venue       (body: organization_id?)       # no league ownership
POST /api/v2/setup/game        (body: season_id, league_id, division_id?, …)

# Season↔Venue access lands in Slice E1 (not C)
POST /api/v2/setup/seasons/{id}/venues   (body: venue_id, active)   # SeasonVenueAccess
```

Error codes: `schedule_conflict` (409), `division_mismatch` (409), `not_found` (404),
`validation_error` (400).
