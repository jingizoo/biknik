# Season lifecycle — archive / read-only history (#159 Slice 1)

Part of epic #159 (Program/Season context, switchers, lifecycle, archive). This
slice adds an explicit, persisted lifecycle state to a Season so operators can
close a finished Season into read-only historical mode and keep active work from
leaking into it.

## Model

`Season.status` is a `SeasonStatus` enum:

| value | meaning |
|-------|---------|
| `active` (default) | accepts operational writes |
| `archived` | read-only historical record |

`Season.archived_at` stamps when the Season was archived (`null` while active).
Migration **039** adds both columns; existing Seasons are `active` (the prior
implicit behaviour). Both fields serialize in the season DTO and in
`/api/v2/setup/overview`, so an archived Season stays visible in read payloads
and the UI can flag it / exclude it from active-work pickers.

## Transitions

Both are audited (`SetupAuditLog`) and authorized at the HTTP boundary with
`MANAGE_SETUP` (League Admin):

- **Archive** — `POST /api/v2/setup/seasons/{id}/archive` (optional `reason`).
  Sets `status=archived` + `archived_at`. Re-archiving an archived Season is a
  stable error (`season_already_archived`); the transition is recorded exactly
  once.
- **Reopen** — `POST /api/v2/setup/seasons/{id}/reopen` (**`reason` required**).
  Clears the archived state back to `active`. This is the privileged, *reasoned*
  path called out by the epic; reopening a non-archived Season is a stable error
  (`season_not_archived`). A missing/blank reason returns `reason_required`.

Only `reason` is accepted in either body (strict schema, #271); unknown keys are
rejected before any write.

## Read-only enforcement

Every operational write that targets a Season fails closed with
`ValidationError` / `reason="season_archived"` (and zero mutation) while the
Season is archived, via `SetupService._require_active_season`:

- register a Team for the Season (`register_team_for_season`)
- create a League / bind a LeagueSeason (`create_league`, `create_league_season`)
- create a Division (`create_division`)
- grant Season→Venue access (`grant_season_venue_access`)
- create a Game (`create_game`, both the base and league-scoped paths)
- commit a draft schedule into the Season (`commit_draft_schedule`)
- roll registrations **into** the Season as a roll-forward *target*
  (`roll_forward_registrations` / `_v2`) — a rollover may still **read** an
  archived *source* Season's history, it just may not write into an archived
  target.

Archived Seasons remain fully readable — all prior registrations, divisions,
games and history are preserved and continue to render; only new writes are
blocked. Reopening restores writability.

## Scope / follow-ups

This slice is the lifecycle foundation for #159. Later slices add the active
Program/Season context selection (persisted per user with an authorized
deterministic fallback), the switcher UI, new-Season copy-forward preview, and
cross-context isolation hardening.
