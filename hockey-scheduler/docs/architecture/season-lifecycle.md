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

### The `reason` value contract

`reason` is type-validated and normalized **before any row is touched**, so a
malformed value never mutates a Season or writes a 500:

- `reason` may only be JSON `null` or a string. Any other JSON type — boolean,
  number, array, object — returns a stable `invalid_reason` /
  `field="reason"` error (400) with zero Season/audit change. `false`/`0`/`[]`/
  `{}` are rejected the same as their truthy counterparts — never silently
  coerced to "missing" — and a truthy non-string never reaches `.strip()`.
- A string is trimmed; the trimmed value is what the audit records. A blank
  string collapses to `null` (recorded as no reason on archive; `reason_required`
  on reopen).
- Archive accepts `null`/blank (audit `reason` is `null`); reopen requires the
  trimmed result to be non-empty (`reason_required` otherwise).

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
are blocked. Reopening restores writability. **Deleting** an archived Season is
blocked too (`delete_season` fails closed with `season_archived` before its
dependency scan, under the same Season row lock): read-only history must be
retained, so an operator must reopen a Season before it can be removed.

**Deleting a permanent League** (`delete_league`) is guarded the same way: a
League participates in a Season only through its `LeagueSeason` bindings, so the
delete first locks every distinct Season those bindings reference (canonical
sorted order) and fails `season_archived` if any is archived — otherwise the
League's deletion would drop that archived Season's `LeagueSeason` (and Game)
history. Game references are treated as explicit dependencies (a Game-backed
League blocks on the Game), and when the delete is permitted the League's own
now-empty `LeagueSeason` bindings are removed in the same transaction so none
are orphaned. A truly unbound League still deletes cleanly.

A **Team transfer** (direct or import-driven, via the shared
`_transfer_team_to_league_inner`) locks every distinct Season its candidate
registrations touch — in canonical sorted order — *before* classifying them, so
its move-or-freeze decision reads each Season's status under that lock. A
registration in a Season that is archived under the lock is frozen history and
never moved; a concurrent archive cannot slip between the status read and the
registration rewrite.

## Scope / follow-ups

This slice is the lifecycle foundation for #159. Later slices add the active
Program/Season context selection (persisted per user with an authorized
deterministic fallback), the switcher UI, new-Season copy-forward preview, and
cross-context isolation hardening.
