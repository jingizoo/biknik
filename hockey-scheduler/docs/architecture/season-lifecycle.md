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

**Deleting a permanent League** (`delete_league`) is dependency-gated with **no
silent cascades** (the service's destructive-delete contract). It locks the
League row (so a concurrent Team create/rebind serializes) and every distinct
Season its `LeagueSeason` bindings reference (canonical order), failing
`season_archived` if any is archived. It then blocks — itemized — on every
dependent: Divisions, registrations, Games, the League's permanent **Teams**
(`Team.league_id`, which have no DB FK to catch an orphan), and the
`LeagueSeason` **bindings themselves**. Nothing is implicitly removed; a truly
unbound, team-free League still deletes cleanly.

To remove a binding, an operator calls the explicit, authorized
(`MANAGE_SETUP`), audited **unbind** — `delete_league_season` /
`POST /api/v2/setup/league-season/{id}/delete`. It is itself dependency-gated
(a binding still owning a Division, registration, or Game is refused) and
read-only-guarded (`season_archived` on an archived Season, under the Season row
lock). This is the counterpart to `create_league_season`, and the step that
clears a League's binding dependency before the League can be deleted.

**Team create / rebind** (`create_team`, `transfer_team_to_league`, direct and
import-driven) lock the target League row, so a Team can never be bound to a
League that a concurrent `delete_league` is removing, nor deleted out from under
the create — on PostgreSQL the two serialize on that row; Memory/SQLite via the
process-wide transaction lock.

A **Team transfer** (direct or import-driven, via the shared
`_transfer_team_to_league_inner`) locks every distinct Season its candidate
registrations touch — in canonical sorted order — *before* classifying them, so
its move-or-freeze decision reads each Season's status under that lock. A
registration in a Season that is archived under the lock is frozen history and
never moved; a concurrent archive cannot slip between the status read and the
registration rewrite.

## Active-context selection (#159 Slice 2 — backend foundation)

Which Program + Season a user is *working in*, persisted per user in
`user_active_context` (one row, `id` = the `user_id`; migration 044) and served
by `ContextService`. This is the backend **preference + resolution foundation**
only — **not** the shell switcher/UI, **not** deep-link restoration, **not**
cross-context isolation of existing reads/writes/workers/exports (they still take
explicit ids), and **not** completion of #159 (which stays open).

**Authorization on every request.** The selection is a VIEW preference, never
authority. On every resolve and set it is filtered through the caller's real
role + account scope (`services/context_scope.py`, the same #211/#266/#202 rules
the rest of the app uses): the two global operators and the read-only Viewer see
every Program (the current model has no org-scoped operator — when one lands,
`context_scope` is the single place to narrow it); a Coach/Player sees only its
team's Program and the Seasons its team **actively participates in under its
current League** (a same-league Season that is later archived stays selectable
read-only); an Official only the Programs/Seasons of its assigned games; a
Guardian only its verified juniors'; an unbound/unknown role fails closed. So a
scoped account can neither select nor *enumerate* an unrelated context.

**Prior-Team history is out of scope for this slice.** When a Team **transfers**
to a new League, #283 freezes its prior registration under the *former*
LeagueSeason. That Season leaves the scoped user's entitlement (their Team's
current-league participation no longer includes it), so a scoped Coach/Player/
Guardian loses selectable access to a *prior* Team Season after a transfer —
even though the registration, Games, results and standings remain preserved and
readable through the ordinary (id-scoped) history views. Restoring historical
entitlement across a Team's prior registrations is a deliberate **#159
follow-up** (below); it is deferred because it would widen a scoped user's view
to Seasons under a League their Team has left, which warrants its own reviewed
slice. This slice's tests assert the *current* (narrowed) behavior explicitly. A saved selection outside the caller's *current*
authorized scope is **ignored** (a fallback is returned) but the row is **not
rewritten**, so if authorization is later restored the saved choice resolves
again. "Which team does this caller act for" is resolved by
`services/subject_scope.own_team_id` — the **same** resolver the web scope guards
use, so the two gates can never drift; `context_scope` adds only the new
Program/Season projection on top of that shared identity.

- **`GET /api/context`** → the effective `{program_id, season_id, read_only,
  program, season}`: the saved selection when its Program is still authorized+
  present and its Season (if any) still authorized+present; else a deterministic
  authorized fallback; else empty. Fallback prefers an authorized **active**
  Season (chosen by **semantically parsed** start_date — latest wins, id
  tiebreak, a null date never beats a dated one), and otherwise a **Program-only**
  context (null Season) so new/empty Programs remain selectable. `read_only` is
  true iff the resolved Season is archived.
- **`POST /api/context`** `{program_id, season_id?}` (strict body: `program_id`
  required, `season_id` optional/nullable, no unknown fields) records a
  selection. `season_id` may be null (Program-only). An **archived** Season is
  accepted as a **read-only historical** context — honored, never silently
  swapped for an active one — while writes against it stay blocked by the Season
  read-only guard above. An unauthorized **or** non-existent Program/Season both
  return the *same* generic `not_found` (no existence oracle). `set_active_context`
  is an atomic `INSERT .. ON CONFLICT (id) DO UPDATE`, so re-selecting the same
  context is idempotent and two concurrent first writes for one user both succeed
  (exactly one row, last-committed wins) rather than racing the primary key into a
  500.

Both endpoints need only a valid session — never the operator permission gate —
because the selection grants nothing (a Viewer may record its own selection yet
still gets 403 on an operator write). `user_id` is always the server-resolved
session user; no client-supplied actor.

**Snapshot-consistent rendering.** `ContextService.resolve`/`set` do every read
inside one `store.transaction()` and return the *exact* Program/Season objects
they validated — not scalar ids the API layer must independently re-fetch. Those
objects are **detached** from the store's live rows (a copy) before the lock is
released, because `InMemoryStore` hands back its shared, mutable rows; the facade
then serializes each object **once** and derives every payload field
(`program_id`/`season_id` and `read_only`) from that single serialized DTO. So
the payload can never internally contradict itself — a non-null `program_id`/
`season_id` always carries its object (no dangling id), and `read_only` always
agrees with the serialized Season `status` — even if a concurrent archive /
reopen / Season-delete / Program-delete lands between two requests *or in-place
during rendering*. For a `POST`, validation and the write share that transaction,
so a concurrent parent delete is either seen (and rejected non-oracle) or lands
after the row is written — where it is harmless, because a saved row pointing at
a since-deleted parent is ignored (never rendered) by the next `resolve` and
grants no authority.

## Scope / follow-ups

Slice 1 (lifecycle) and Slice 2 (this backend selection foundation) are done.
Remaining #159 work, to be taken as separate slices: the authenticated-shell
**switcher UI + deep-link restoration** consuming these endpoints; then
**consumer-by-consumer cross-context isolation** (lists, counts, exports,
background jobs resolving strictly through the selected Season); then
**new-Season copy-forward preview**; and **prior-Team historical entitlement**
— letting a scoped Coach/Player/Guardian re-enter (read-only) a Season their
Team was registered in under a League it has since **left** (resolving view
entitlement from all of a Team's registrations, independent of its current
`league_id`, kept separate from the active-work fallback). That last one is a
security-sensitive scope widening — a scoped user would regain view access to a
Season under a League their Team left — so it is intentionally its own slice.
#159 stays open.
