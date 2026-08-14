# Data Model

> **⚠️ Competition hierarchy is being reset (epic #233).** The competition/venue
> layer (Program → Season → League → optional Division; Organization-owned Venues
> shared with Seasons) is documented in
> [Competition + Facility Setup](league-arena-setup.md) and defined by
> [ADR 0001 — Competition model reset](decisions/0001-competition-model-reset.md).
> This page covers only the **roster + substitute** slice.
>
> Two entities on this page **also change** (per the ADR): **`Team`** — its permanent
> owner becomes a **Program** (`league_id` → `program_id`), `club_id` stays nullable;
> and **`Game`** — it gains a `league_id` competition-scope column (Season + League +
> optional Division). The roster/substitute *workflow* entities (`GameRosterEntry`,
> `GameAvailability`, `SubstituteEnrollment`, `AuditLog`) and computed `RosterStatus`
> are unaffected.

Entities for the roster + substitute slice. `RosterStatus` is **calculated
dynamically** from the other entities (not stored).

## Entity overview

```text
Team ──< Player
Game ──< GameRosterEntry >── Player
Game ──< GameAvailability >── Player
Game ──< SubstituteEnrollment >── Player
Game ──< AuditLog
(RosterStatus is computed from a Game + its entries + substitutes)
```

## Team

| Field | Type | Notes |
| --- | --- | --- |
| id | str | `team_…` |
| name | str | |
| division | str | e.g. "U16", "Senior A" |

## Player

| Field | Type | Notes |
| --- | --- | --- |
| id | str | `player_…` |
| team_id | str | owning team |
| name | str | flattened DISPLAY name; **derived** ("first last") whenever the structured names below are set (#273), free-typed only on legacy rows |
| position | Position | `GOALIE` / `DEFENSE` / `FORWARD` / `SKATER` |
| shoots | str? | "L" / "R" (optional) |
| jersey_number | int? | 1–98, or unset; unique among a team's **active** players (#269) |
| is_active | bool | inactive players are not eligible |
| external_ref | str? | stable import-matching `player_code` (#93) |
| first_name | str? | structured name (#273); never guessed by splitting `name` |
| last_name | str? | set/cleared together with `first_name` |
| preferred_name | str? | optional preferred given name |
| birthdate | str? | **private** `YYYY-MM-DD`; stripped from default facade payloads, operator opt-in only |
| registration_number | str? | **private** stable governing-body id; same-team duplicates refused, cross-team duplicates warned |
| skill_rating | int? | 1–7 coach rating (#287 ruling → #273); `None` = unrated, ranked last but never excluded |

`position` maps to a **slot type**: `GOALIE` → goalie slot; everything else →
skater slot.

The guardian relationship is `GuardianLink` (verified, consent-recorded —
#26/#35). The legacy `guardian_person_id` field was removed from the model
and every payload in #273 (nothing ever read it); its dead DB column is
retained until an explicit follow-up drop migration.

## AgeEligibilityRule (#273)

| Field | Type | Notes |
| --- | --- | --- |
| id | str | `agerule_…` |
| league_season_id | str | the LeagueSeason the rule governs |
| version | int | append-only; (league_season_id, version) unique — rows are immutable history |
| cutoff_month / cutoff_day | int | age is measured on this month/day in the Season's start year (Feb 29 refused) |
| tiers | list | `{"code": "U10", "max_age": 10}`; `max_age: null` = open tier; eligible iff age at cutoff is strictly under `max_age` |
| enforcement | str | `warn` (default) / `block`; warn-first — no consumer hard-blocks yet |
| created_at | datetime | |
| actor_id | str? | |

A Division declares its tier via its existing `age_group` text, matched
case-insensitively against the rule's tier codes. Evaluation answers
eligible / ineligible / indeterminate (`no_rule`, `no_birthdate`,
`unknown_tier`, `no_season_start`, …) and always names the exact rule
version used; the result never contains the birthdate itself.

## Game

| Field | Type | Notes |
| --- | --- | --- |
| id | str | `game_…` |
| home_team_id | str | the team this roster belongs to |
| away_team_id | str? | opponent (display only in this slice) |
| rink | str? | |
| start_time | datetime | UTC |
| end_time | datetime? | UTC |
| target_goalies | int | required goalies (e.g. 1) |
| target_skaters | int | required skaters (e.g. 15) |
| max_skaters | int | hard cap |
| roster_lock_time | datetime? | informational |
| locked | bool | coach-locked flag |
| cancelled | bool | game cancelled flag |

`status` is **not stored** — it is computed by the roster-status engine.

## GameRosterEntry

One row per player selected for / added to the game.

| Field | Type | Notes |
| --- | --- | --- |
| id | str | |
| game_id | str | |
| player_id | str | |
| roster_role | RosterRole | `SELECTED` / `SUBSTITUTE_ADDED` |
| selection_source | SelectionSource | `COACH_SELECTED` / `SUBSTITUTE_POOL` / `MANUAL_OVERRIDE` / `AUTO_FILL` |
| status | RosterEntryStatus | `SELECTED` / `CONFIRMED` / `UNAVAILABLE` / `REMOVED` / `OFFERED` / `ACCEPTED` |
| selected_by | str? | actor id |
| selected_at | datetime | |
| updated_at | datetime | |

A roster entry **occupies a slot** when its status is one of `SELECTED`,
`CONFIRMED`, `OFFERED`, `ACCEPTED`. It frees the slot when `UNAVAILABLE` or
`REMOVED`. It is **confirmed** (counts as a confirmed body) when `CONFIRMED`
or `ACCEPTED`.

## GameAvailability

Tracks a player's response.

| Field | Type | Notes |
| --- | --- | --- |
| id | str | |
| game_id | str | |
| player_id | str | |
| availability_status | AvailabilityStatus | `PENDING` / `AVAILABLE` / `UNAVAILABLE` / `MAYBE` |
| response_source | str | `PLAYER` / `GUARDIAN` / `COACH` |
| responded_at | datetime? | |
| notes | str? | |

## SubstituteEnrollment

Non-selected players willing to play.

| Field | Type | Notes |
| --- | --- | --- |
| id | str | `sub_…` |
| game_id | str | |
| player_id | str | |
| position | Position | the substitute's position |
| status | SubstituteStatus | `ENROLLED` / `OFFERED` / `ACCEPTED` / `DECLINED` / `EXPIRED` / `WITHDRAWN` / `CANCELLED` |
| priority_rank | int? | coach-controlled ordering |
| enrolled_at | datetime | |
| offered_at | datetime? | |
| offer_expires_at | datetime? | |
| accepted_at | datetime? | |
| declined_at | datetime? | |

A substitute is **available for a slot** when status is `ENROLLED` and the
substitute's slot type matches the open slot type.

## RosterStatus (computed)

| Field | Type |
| --- | --- |
| game_id | str |
| team_id | str |
| target_goalies | int |
| confirmed_goalies | int |
| open_goalie_slots | int |
| target_skaters | int |
| confirmed_skaters | int |
| open_skater_slots | int |
| substitutes_enrolled | int |
| status | GameStatus |
| action_required | bool |
| message | str |

## Enumerations

```text
Position            : GOALIE, DEFENSE, FORWARD, SKATER
RosterRole          : SELECTED, SUBSTITUTE_ADDED
SelectionSource     : COACH_SELECTED, SUBSTITUTE_POOL, MANUAL_OVERRIDE, AUTO_FILL
RosterEntryStatus   : SELECTED, CONFIRMED, UNAVAILABLE, REMOVED, OFFERED, ACCEPTED
AvailabilityStatus  : PENDING, AVAILABLE, UNAVAILABLE, MAYBE
SubstituteStatus    : ENROLLED, OFFERED, ACCEPTED, DECLINED, EXPIRED, WITHDRAWN, CANCELLED
SlotType            : GOALIE, SKATER
SlotStatus          : FULL, OPEN, NEEDS_COACH_DECISION
GameStatus          : DRAFT, SELECTED, AWAITING_RESPONSES, ROSTER_CONFIRMED,
                      NEEDS_SUBSTITUTE, OPEN_SLOT, LOCKED, FINAL
AuditAction         : roster_selected, availability_set, player_backed_out,
                      substitute_enrolled, substitute_withdrawn, substitute_offered,
                      substitute_accepted, substitute_declined, substitute_added_to_roster,
                      player_removed, roster_locked, roster_unlocked, game_cancelled
```

## AuditLog

| Field | Type | Notes |
| --- | --- | --- |
| id | str | |
| game_id | str | |
| action | AuditAction | |
| actor_id | str? | who performed it |
| subject_player_id | str? | who it was about |
| detail | dict | structured before/after where useful |
| at | datetime | |
