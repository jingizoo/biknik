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

### Two independent routes into "history" — and one predicate

A Season becomes **history** by *either* of two routes, and they are
independent:

1. the explicit **ARCHIVED** lifecycle state (#159) — and `archive_season`
   deliberately does **not** set `end_date`, so an archived Season is routinely
   **undated**, or even future-dated; or
2. a real **`end_date` that has definitely passed** (#283 rule 10) — on an
   `active` Season that simply ran out.

Neither implies the other. Every historicity test must therefore accept **both**,
and there is exactly one place that decides it:
**`services/season_guard.season_is_historical(season, now)`**, reached from the
service layer as `SetupService.season_is_historical(season, now=None)` (which
defaults `now` to the service clock). `now` is a parameter, not a fresh
`clock()` call, so a caller holding a snapshot across several Seasons makes one
decision that cannot straddle a tick.

Historicity flips a **read** rule, never a write rule: on a historical Season the
live rule-7 check "the Team's *current* permanent League must still be this
League" is **not** re-applied, because the transfer write path froze that
registration in place rather than moving it. Writes stay refused either way —
an archived Season answers `season_archived` regardless (see above), so widening
what counts as history can only widen a **row set**, never authorize a mutation.

The call sites converted so far, all three of which must stay in agreement:

| Site | Role |
| --- | --- |
| `services/setup_service.py` `_transfer_team_to_league_inner` | the **write**: freeze a historical registration instead of moving it |
| `api/service.py` `_standings_for_division` | reader: `enforce_team_league=not season_is_history` |
| `api/service.py` `_standings_for_league_season` | reader: skip the rule-7 `team.league_id != league_id` exclusion |

This is written down because the halves **had** drifted: the two standings
readers tested only `end_date`, so an ARCHIVED-but-undated Season read as if it
were live, and a later legitimate Team transfer retroactively deleted a Team
from — and zeroed its opponent's record in — that Season's operator *and* public
standings, in both the Division and the LeagueSeason view. Do not re-derive the
expression at a fourth site; call the predicate.

#### Known outstanding (NOT delivered here)

* `services/hierarchy_import.py` `_preflight_reassignment_safety` (a2) still
  re-derives its own historicity expression and has drifted the *other* way,
  refusing a move the write would have allowed. Converting it is a separate
  follow-up; this section does not claim it is done.
* This slice fixes only **which registered Teams** an archived Season's
  standings are built from. The **points, tiebreak and eligibility rules** are
  still read live and are **not** version-pinned to the Season, so changing a
  live points rule *does* still alter archived output. That behaviour is
  identical with and without the change described here — it is not a regression
  introduced by it — and pinning rule versions is a separate, larger #159 child.

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

- **`GET /api/context`** → the effective `{program_id, season_id, league_id,
  read_only, program, season, league}`: the saved selection when its Program is
  still authorized+present and its Season (if any) still authorized+present; else
  a deterministic authorized fallback; else empty. Fallback prefers an authorized
  **active** Season (chosen by **semantically parsed** start_date — latest wins,
  id tiebreak, a null date never beats a dated one), and otherwise a
  **Program-only** context (null Season) so new/empty Programs remain selectable.
  `read_only` is true iff the resolved Season is archived.

  `league_id`/`league` are the **third axis** (#345, wired to HTTP by #360),
  added **additively**: both keys are ALWAYS present, so a client never has to
  distinguish "absent" from "null", and the Program/Season half is byte-identical
  to the pre-#360 payload. A null League is a **first-class state** (Program-only,
  and Season-without-League), never an error. The League resolves through
  `ContextService.resolve_with_league`, under the *same* single serializable
  snapshot as the other two axes, so it can never contradict the Program/Season
  it is rendered beside.
- **`POST /api/context`** `{program_id, season_id?, league_id?}` (strict body:
  `program_id` required, `season_id`/`league_id` optional/nullable, no unknown
  fields) records a selection. `season_id` may be null (Program-only). Omitting
  `league_id` is **meaningful, not a no-op**: it selects "no League", which is
  exactly what the pre-#360 two-field body always did — a League is never carried
  onto a Program/Season it was not chosen for. An **archived** Season is
  accepted as a **read-only historical** context — honored, never silently
  swapped for an active one — while writes against it stay blocked by the Season
  read-only guard above. An unauthorized **or** non-existent Program/Season both
  return the *same* generic `not_found` (no existence oracle).

  A League is held to two extra rules, both enforced at selection time: it must
  belong to the selected **Program**, and a Season+League pair must name an
  **existing `LeagueSeason`** binding. Every invalid League — nonexistent,
  cross-Program, unauthorized, deleted, Season-unbound, or **ambiguous** (a
  duplicate binding fails closed rather than picking a winner) — returns ONE
  indistinguishable `not_found`, for the same no-oracle reason as the other two
  axes, and changes **zero** context rows. The endpoint resolves a `LeagueSeason`
  strictly read-only: it **never creates or repairs** one, so a view preference
  can never manufacture competition structure — binding stays the authorized,
  audited job of `setup_service.create_league_season`. `set_active_context`
  is an atomic `INSERT .. ON CONFLICT (id) DO UPDATE`, so re-selecting the same
  context is idempotent and two concurrent first writes for one user both succeed
  (exactly one row, last-committed wins) rather than racing the primary key into a
  500.

Both endpoints need only a valid session — never the operator permission gate —
because the selection grants nothing (a Viewer may record its own selection yet
still gets 403 on an operator write). `user_id` is always the server-resolved
session user; no client-supplied actor.

**Authorization is linearizable with scope-changing writes.** The whole scope
computation + selection (and, for `POST`, the write) runs under **one
`SERIALIZABLE` snapshot** — a narrow, per-request isolation on the context
transaction only, never a global connection change — with a bounded retry on a
serialization conflict (`ContextService._snapshot` → `store.transaction(isolation=
"SERIALIZABLE")`). So a concurrent scope revocation (an Official unassignment, a
Player/Guardian reassignment) either orders **entirely before** the request (it
sees the old scope) or **entirely after** it (it sees the new scope): the result
always corresponds *wholly* to one authorization snapshot and can never be a
hybrid — e.g. an old Program set paired with a now-empty Season set, or a
Program-only fallback that matches no single snapshot. Errors stay non-oracle
across the boundary. Memory/SQLite get the identical guarantee from their
process-wide transaction lock, which already fully serializes writers, so the
retry is a no-op there.

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

## Active-context switcher (#159 Slice 3 — the authenticated UI)

The topbar Program/Season switcher (`web/static`: `#context-switcher` in
`index.html`, `renderContextSwitcher`/`setActiveContext` in `app.js`) — a saved
context, consuming the Slice-2 endpoints. It grants no authority. **It did not
filter existing screens when this slice shipped**; #367/#369 has since scoped
the operational reads to the same server-resolved `(Program, Season, League)`
tuple, so switching now genuinely changes what Games, Roster, Standings, the
Dashboard and the Setup summaries show — see
`docs/architecture/active-context-scoping.md` for the read contracts, which are
the authority on what is and is not scoped. A persistent, always-visible scope
note (`#ctx-scope-note`) sits next to the control in its normal closed state
(not hidden inside a dropdown or a hover tooltip) and is wired as
`aria-describedby` on both selects. That note is deliberately written as
capability wording rather than a list of screens: its two earlier revisions each
enumerated what was or was not filtered, and each rotted into a false statement
as more surfaces became context-aware. It is one consistent control for every
role.

- **`GET /api/context/options`** → `{programs: [{id, name, seasons: [{id, name,
  status, read_only, start_date}], leagues: [{id, name}]}], selected:
  {program_id, season_id, league_id, read_only}, saved: {program_id, season_id,
  league_id}}`, filtered through the **same**
  `context_scope` rules as get/set (`ContextService.options_with_saved` runs
  under the same one serializable snapshot). So
  the switcher only ever offers a context the caller could actually select — it
  never enumerates an unrelated Program/Season from the (unfiltered) overview.
  A **Program-overview (no-season)** choice is offered for **every** authorized
  Program — even when that Program has Seasons — in addition to one entry per
  authorized Season (archived ones flagged read-only). `selected` is guaranteed
  to be one of the options. Session-only, like the other context endpoints.

  `leagues` (#345/#360) is **Program-scoped, not Season-scoped**, deliberately: a
  League that exists under the Program but is not bound to the currently-selected
  Season is still offered, because selecting it is a legitimate way to move to a
  Season+League pair. The binding requirement is enforced at **selection time**,
  where it can report a precise reason, rather than silently by omission here. A
  League carries no `read_only` — that is a property of the Season's lifecycle,
  not of the permanent League.

  `saved` (#411) is a **different fact from `selected`**, and the two must not be
  collapsed. `selected` is the resolved, renderable context and may have been
  invented by `ContextService._fallback()`; `saved` is what the operator
  themselves persisted, validated exactly as `resolve_saved_with_league`
  validates it — the authority every #409 create/mutation gate is judged
  against. All-null means nothing valid is persisted. On a one-Program
  installation the two carry the **same Program id while nothing is saved at
  all**, because the fallback walks the authorized Programs in id order, so no
  comparison inside `selected` could ever separate them: a UI drawn from
  `selected` alone necessarily claims a selection the next create will refuse.
  A stale saved Season drops off while the Program survives, which is exactly
  what the create gate grants in that state.
- **One control, every role — a native `<select>`.** The switcher renders as a
  native `<select>` (grouped by Program via `<optgroup>` when more than one
  Program is authorized) so it gets the full keyboard / screen-reader contract —
  focus, Arrow/Home/End, type-ahead, Enter/Escape — for free, with no custom
  menu-radio handling. It collapses to a **static chip** only when there is
  exactly one selectable context (a single Program with no Seasons). The
  read-only badge is a persistent reflection of the current selection, visible
  in the closed state.

  **The collapsed state carries its own control (`#ctx-confirm`, #411).** The
  chip is a label, and a label cannot select anything — so on a first-run
  installation (one Program, no Seasons) there was no control on the page wired
  to `setActiveContext`, while every Program-axis create answered 409 *"Select a
  Program before creating records in it"*. A real `<button>` now stands where
  the `<select>` would be, labelled with the Program (`Select <name>`), so
  Enter/Space activate it natively and its text is its accessible name. It is
  painted from `saved`, never from `selected`, so it appears exactly while
  nothing is persisted and withdraws itself once something is — and it is wired
  once, outside the render pass, because **rendering must never persist**:
  auto-selecting on render would launder the fallback back into mutation
  authority, which is the whole thing #409 removed. On success it hands focus to
  the surviving chip and announces through the one sitewide live region. The
  guided Initial Setup view paints the switcher too (`onboarding.js`), because
  that is the surface a first-run operator is landed on and where the refusal is
  actually raised. It never hard-codes role logic in the browser — the
  option set comes entirely from the endpoint above. A `POST` of an option the
  server rejects (a race) surfaces the same generic not-found, shown as a
  generic message (no existence oracle).
- **Deep-link restoration.** The selection is mirrored in a structured, encoded
  URL hash (`#ctx=<base64url(JSON)>` — a versioned `{v,p,s}` object encoded with
  true Base64URL: `+`→`-`, `/`→`_`, `=` padding stripped, **no** percent-
  encoding) via `replaceState` — coexisting with the existing `#public` guest
  route (a different prefix), never clobbering it. On load the persisted context
  (from the endpoint) is applied first; if the URL carries a *different* context,
  it is adopted by `POST` (the backend authorizes it — no client-side role
  logic), and an unauthorized **or** non-existent link is normalized to the
  persisted context with a generic message, then the hash is rewritten to the
  resolved selection (never the bogus id). Because the SPA has no `hashchange`
  listener, a shared link is adopted on a full load of that URL (a new tab /
  reload), matching the `#public` precedent.

## Scope / follow-ups

Slice 1 (lifecycle), Slice 2 (backend selection foundation) and Slice 3 (this
switcher UI + deep-link restoration) are done. **Consumer-by-consumer
cross-context isolation** — lists, counts and the operational reads resolving
strictly through the selected tuple — has since landed under #345 as #367/#369
(`docs/architecture/active-context-scoping.md`); exports and background jobs are
not covered by it. Remaining #159 work, to be taken as separate slices:
**new-Season copy-forward preview**; and **prior-Team historical
entitlement** — letting a scoped
Coach/Player/Guardian re-enter (read-only) a Season their Team was registered in
under a League it has since **left** (resolving view entitlement from all of a
Team's registrations, independent of its current `league_id`, kept separate from
the active-work fallback; a security-sensitive scope widening, so intentionally
its own slice). #159 stays open.
