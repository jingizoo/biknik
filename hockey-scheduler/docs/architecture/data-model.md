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
| skill_rating | int? | existing global/provisional 1–7 value; #287's 2026-08-29 ruling requires the canonical substitute-ranking rating to be League-context, League-admin-owned, and audited in #273; `None` = unrated, ranked last but never excluded |

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
| team_side | str? | **durable** game-side attribution — the Team id this row was seated against, written at creation/re-seat from the validated `GameMembershipContext` (migration 061) |
| seated_position | Position? | **durable** — the position this row was seated to occupy; buckets into `GOALIE`/`SKATER` via `seated_slot_type` |

`team_side`/`seated_position` are what the slot engine counts a seated row
against — never the player's *current* membership, and never the permanent
`Player.team_id` pointer. They are `NULL` only for rows written before
migration 061; such a row is charged as occupying on **every** side and in
**both** buckets (fail closed — it can reduce an open count but never reopen
one, and it never names a side). See migration 061 for the full rationale.

A roster entry **occupies a slot** when its status is one of `SELECTED`,
`CONFIRMED`, `OFFERED`, `ACCEPTED`. It frees the slot when `UNAVAILABLE` or
`REMOVED`. It is **confirmed** (counts as a confirmed body) when `CONFIRMED`
or `ACCEPTED`.

**Player self-service into `CONFIRMED` asks two questions with two different
answerers.** The durable row *identifies* the side and bucket in play; the
player's *current live* membership context *authorizes* the transition. So
`POST /api/games/{id}/availability` with `available` — the only route the UI
offers a player — refuses when the live context resolves to a different team
than `team_side` (`not_eligible`, `details.reason =
"seated_side_not_live_eligible"`), and refuses when no live context resolves
at all. One gate,
`RosterService._authorize_seated_side`, serves both routes into `CONFIRMED`:
re-confirming a backed-out (`UNAVAILABLE`) row, and confirming a row that
never backed out. It runs before any store write.

The two routes differ in exactly one respect, deliberately. Re-confirming
*re-takes* a freed slot, so it additionally requires that slot to still be
open, and refuses a pre-061 `NULL`-attribution row whose side cannot be
identified. Confirming a still-occupying row takes nothing — `SELECTED` and
`CONFIRMED` hold the same slot — so it applies **no** open-slot check (that
would refuse the last seat's own confirmation) and lets a `NULL` row through.

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

Non-selected players willing to play. The legacy form is availability for the
player's own Game side. The bounded #287 cross-team form is a proactive opt-in
for one explicit target side of another team's Game in the same competition
boundary; it does not require an open vacancy at enrollment time.

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
| offer_expires_at | datetime? | server-owned for cross-team rows: `min(offered_at + 30 minutes, game.start_time)`; equality is expired |
| accepted_at | datetime? | |
| declined_at | datetime? | |
| team_id | str? | durable owning Game side; for a cross-team row this is the explicit target, not the source team; null only on honest legacy rows |
| source_membership_id | str? | cross-team-only exact source stint; paired with `source_team_id`, deliberately no foreign key so history survives cleanup |
| source_team_id | str? | cross-team-only source team; paired with `source_membership_id`, deliberately no foreign key |

A fresh cross-team row is valid only when source and target resolve through the
exact same `LeagueSeason`, both registrations share the same non-null
`Division`, and the source team is neither participating side in the target
Game. `team_id` owns the row and eventual roster seat; the source pair proves
where eligibility came from and is revalidated before offer or acceptance.
The public API never returns the source pair.

The partial unique index `ux_substitute_active_game_player` permits at most one
`ENROLLED` or `OFFERED` row per `(game_id, player_id)`. Terminal rows remain as
history and do not prevent a later lifecycle. A substitute becomes offerable
for a slot only when status is `ENROLLED`, its snapshotted slot type matches an
open target-side slot, and its eligibility/provenance still validates. For a
cross-team `OFFERED` row, accept or decline at the server-owned deadline records
`EXPIRED`; that transition emits `substitute_expired` audit evidence and the
overdue offer remains explicitly dismissible from Player Home so that action
can persist the terminal outcome.

Same-team rows keep both source columns null. Omitted-target enrollment,
offer-time live retargeting, client deadline handling, and the established
same-team response boundary are unchanged by migration 064.

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
                      substitute_accepted, substitute_declined, substitute_expired,
                      substitute_added_to_roster,
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
| team_id | str? | internal frozen Game-side attribution; historical snapshot, no FK |

`team_id` is captured from the validated side of a side-owned lifecycle write.
It is deliberately not backfilled and carries no foreign key, so audit history
survives later Team/subtree deletion. A legacy NULL uses the conservative
durable-player-attribution fallback. The field is authorization metadata and
is stripped from every board response.

## NotificationEvent

| Field | Type | Notes |
| --- | --- | --- |
| id | str | |
| game_id | str | |
| type | NotificationType | |
| audience | str | intended audience class |
| message | str | |
| subject_player_id | str? | who it was about |
| at | datetime | |
| team_id | str? | internal frozen Game-side attribution; historical snapshot, no FK |

`NotificationEvent.team_id` follows the same no-backfill, no-FK and
never-serialized rules as `AuditLog.team_id`. It prevents later terminal
history for another side from erasing or reattributing the original event.
