# League + Arena Setup — Data Model & API

Adds the organization and arena entities used to create games. Implemented in
`services/setup_service.py` and exposed via `api/service.py`. Same layered
architecture as the roster slice: pure service logic over the in-memory store,
injected clock, structured errors, audited writes.

## Hierarchy

```text
League ──< Season ──< Division
Club   ──< Team   >── Division        (a team belongs to one division)
Venue  ──< Rink   ──< IceSlot
Game   >── Season, Division, home Team, away Team, IceSlot
```

## Entities

### League
| Field | Type | Notes |
| --- | --- | --- |
| id | str | `league_…` |
| name | str | required |
| country | str | optional |
| timezone | str | IANA name, default `UTC` |

### Season
| Field | Type | Notes |
| --- | --- | --- |
| id | str | `season_…` |
| league_id | str | parent league (must exist) |
| name | str | required, e.g. "2026/27" |
| start_date / end_date | datetime? | timezone-aware UTC; end ≥ start |

### Division
| Field | Type | Notes |
| --- | --- | --- |
| id | str | `division_…` |
| season_id | str | parent season (must exist) |
| name | str | required, e.g. "U16" |
| age_group | str | optional |

### Club / Team
`Club { id, name, country }`. `Team` (extends the roster-slice model) gains
`club_id` and `division_id`; a team is created under a club and a division.

### Venue / Rink / IceSlot
- `Venue { id, name, address, timezone }`
- `Rink { id, venue_id, name }`
- `IceSlot { id, rink_id, start_time, end_time, slot_type, status }`
  - `slot_type`: `game | practice | tournament | maintenance | public_skate`
  - `status`: `available | allocated | blocked` (set to `allocated` when a
    game is created on it)

### Game (extended)
The existing `Game` gains `season_id`, `division_id`, and `ice_slot_id`. A
manually-created game copies `start_time`/`end_time` from the ice slot and the
rink name, and is immediately usable by `RosterService`.

## Rules

- A child entity's parent must exist, else `not_found`.
- Names are required (non-blank), else `validation_error`.
- Ice-slot times must be timezone-aware UTC and `end > start`, else `validation_error`.
- A division must belong to the game's season, else `validation_error`.
- A team cannot play itself, else `validation_error`.
- Both teams must be in the game's division unless `allow_division_override`,
  else `division_mismatch`.
- One game per ice slot (active games), else `schedule_conflict`.
- Every create appends a `SetupAuditLog` entry.

## API (facade methods → suggested REST)

```http
POST /leagues                         create_league
GET  /leagues                         list_leagues
POST /leagues/{id}/seasons            create_season
POST /seasons/{id}/divisions          create_division
POST /clubs                           create_club
POST /clubs/{id}/teams                create_team        (body: division_id, name)
POST /venues                          create_venue
POST /venues/{id}/rinks               create_rink
POST /rinks/{id}/ice-slots            create_ice_slot
POST /teams/{id}/players              create_player
POST /games                           create_game
```

`create_game` body:

```json
{
  "season_id": "season_1",
  "division_id": "division_1",
  "home_team_id": "team_1",
  "away_team_id": "team_2",
  "ice_slot_id": "slot_1",
  "allow_division_override": false
}
```

New error codes: `schedule_conflict` (409), `division_mismatch` (409), plus the
existing `not_found` (404) and `validation_error` (400).
