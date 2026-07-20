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

Every write that creates or modifies anything a Season owns fails closed with
`ValidationError` / `reason="season_archived"` (and zero mutation) while the
Season is archived, via the shared `services/season_guard.require_active_season`
(routed through `SetupService._require_active_season` /
`_guard_game_season` and `RosterService._guard_active_season`). The full set:

- **Structure:** `register_team_for_season`, `create_league`,
  `create_league_season`, `create_division`, `grant_season_venue_access`,
  `revoke_season_venue_access`, `delete_season_venue_access`.
- **Games:** `create_game` (base + league-scoped), `move_game`, `publish_game`,
  `delete_game`, `record_result`, `approve_result`, `request_reschedule`,
  `respond_to_reschedule`, `decide_reschedule`, `assign_official`,
  `respond_assignment`, `unassign_official`; the draft batches
  `commit_draft_schedule`, `publish_draft_games`, `discard_draft_games`.
- **Roster / substitutes** (`RosterService`, via `_guard_mutable` plus
  `set_availability`, `lock_roster`, `unlock_roster`, `cancel_game`):
  `select_roster`, `remove_player`, `copy_previous_roster`,
  `set_roster_entry_status`, `enroll_substitute`, `withdraw_substitute`,
  `offer_substitute`, `accept_substitute`, `decline_substitute`,
  `add_substitute_to_roster`.
- **Imports:** `commit_teams_players_import` and the hierarchy upserts
  (`upsert_imported_season` **update** branch, `upsert_imported_registration`,
  `upsert_imported_venue_access`) — a hierarchy batch may still **create** new
  Seasons, it just may not modify an existing archived one.
- **Roll-forward target:** `roll_forward_registrations` / `_v2` — a rollover may
  **read** an archived *source* Season's history but never write into an
  archived *target*.

### Linearizability

The guard row-locks the Season (`get_season_for_update`) and runs inside the
caller's transaction, and `archive_season`/`reopen_season` lock the same row.
So a write racing an archive on PostgreSQL is serialized: the write either
commits before the archive (frozen history) or blocks on the row until the
archive commits, then observes `archived` and fails with zero mutation — never a
write landing on an already-archived Season. Memory/SQLite carry the same
invariant via their process-wide transaction lock.

Archived Seasons remain fully readable — all prior registrations, divisions,
games, results and history are preserved and continue to render; only new writes
are blocked. Reopening restores writability. (`delete_season` itself is a
separate destructive operation with its own dependency guards and is
deliberately **not** blocked by archive — removing a Season is not a write
*into* it.)

## Scope / follow-ups

This slice is the lifecycle foundation for #159. Later slices add the active
Program/Season context selection (persisted per user with an authorized
deterministic fallback), the switcher UI, new-Season copy-forward preview, and
cross-context isolation hardening.
