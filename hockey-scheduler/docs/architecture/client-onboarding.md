# Client production onboarding

The end-to-end journey a new client follows from a fresh production deployment
to scheduling their first game, and how each step is verified. This ties
together the pieces built under #174; deployment mechanics (env vars, backup
commands, migration checks) live in the [production runbook](production-runbook.md),
which this guide references rather than repeats.

## The journey

```text
Fresh production deployment (durable DB, no demo data)
  → client claims the installation with a one-time setup code
  → client creates their own first League Admin and signs in
  → guided Initial Setup walks the hierarchy: owner → program → venues/rinks
    → season/league/divisions → clubs/teams → players/officials → game ice → staff
  → each step is entered manually or bulk-imported from templates
  → onboarding status reports what still blocks scheduling
  → back up / restore is proven to reproduce the configured client
  → begin scheduling
```

Every write goes through the normal service → store → audit path; nothing about
setup progress lives in the browser. Restart, redeploy, or a fresh sign-in all
resume from the same server-derived state.

## 1. Durable production deployment

Production must run on a durable SQL store and never seed demo data. Set
`APP_MODE=production` and a real `DATABASE_URL` (durable Postgres, or a real
SQLite file for a single-instance pilot). `/api/readiness` fails closed for
in-memory or ephemeral storage, stale migrations, missing admin, or unhardened
cookies — treat it as the go/no-go gate before routing traffic. See the runbook
sections *Environment*, *Migration verification*, and *Durable-store readiness*.

## 2. One-time client-owned admin claim

The client creates their own first League Admin in the browser at `/setup`, so
the deployment operator never chooses, sees, or stores the client's password.
The operator generates a high-entropy `INITIAL_SETUP_CODE`, delivers it through
a secure channel, and the client exchanges it (plus a username/password of their
choosing) for exactly one atomically-created League Admin.

- `GET /api/bootstrap/status` → `claim_available` reflects a fresh, durable,
  unclaimed installation with a configured code — and nothing else (no account
  count, username, or database detail).
- `POST /api/bootstrap/claim` validates the code in constant time, creates the
  admin, marks the installation claimed, and audits the claim. A second attempt
  fails closed with `409 already_claimed`; two admins can never exist.

Remove `INITIAL_SETUP_CODE` after a successful claim. Full mechanics: runbook
*One-time client-owned admin claim*.

## 3. Guided Initial Setup

A first admin signing in to an empty database is routed to a resumable **Initial
Setup** wizard that sequences the whole hierarchy. It does not trap the admin —
they can navigate to any advanced screen — and it never stores progress in the
browser; every step reads back from persisted data. Each step reuses the
existing Setup drawers, the Users screen, the Import screen, and the Calendar.

## 4. Manual entry and bulk import converge on one model

Both paths write the same domain entities through the same services — the wizard
creates no parallel data store. Small configurations can be typed in; a full
client hierarchy is imported from nine CSV sheets on the Import screen:
`organizations`, `programs`, `venues_rinks`, `competition` (season/league/
optional division), `clubs`, `permanent_teams`, `players`, `registrations`, and
`season_venue_access` (issue #260's locked design). Imports are matched by
stable external codes (never names — a Club's `club_code` closed the last gap),
validate every cross-file reference before any write (every error in one pass,
never fail-fast), commit each batch in one transaction, are idempotent on
repeat upload, and never delete rows that are absent from a later file. A
`permanent_teams` row carries a required `league_code`: a Team is a permanent
member of a League (which belongs to its Program), validated to sit in the
Team's Program (#283 Slice E). A `registrations` row's `league_code` is always
explicit and validated against the season's chain — never derived from a
division or "the season's only league" — and must equal the Team's own
permanent League (a Team may only register into its own League).
Every commit reports created/updated/skipped counts per entity and writes a
batch audit plus entity audits attributed to the signed-in admin.

The Import screen's "Setup profile" — seven quick questions (what you're
setting up, whether you use clubs/divisions, importing players now, one venue
or several, granting venue access now, first-time vs. updating) — is pure
UI-routing state: it only decides which of the nine sheet cards are shown and
which contextual hints appear, has no backend field of its own, and is never
persisted. Every answer combination still submits through this one canonical
import engine.

## 5. Onboarding status: what still blocks scheduling

`GET /api/v2/onboarding/status` (League-Admin only, canonical vocabulary —
same shape as the legacy `GET /api/onboarding/status`) derives progress from
persisted records and the owner/program/venue rules — never a browser step
counter. It returns ordered `steps`, a flat `blocking` list (each individually
actionable — missing organization, program, owner tie, venue, rink, game ice,
season, league, division, team, or a dangling parent), and non-blocking
`warnings` (no players or officials yet, orphaned records). `ready_to_schedule`
is true when nothing blocks; `complete` additionally requires the warnings
clear. The wizard's **Start scheduling** action unlocks only when
`ready_to_schedule` is true.

## 6. Recovery: backup and restore

A configured client must be reproducible from a backup. The runbook documents
the `pg_dump`/`pg_restore` (Postgres) and file-copy (SQLite) mechanics; the
checked-in acceptance smoke proves the round trip actually works:

```bash
python -m hockey_scheduler.acceptance.backup_restore --database-url ./client.db
```

It backs the deployment up, restores into a fresh empty database, starts the app
against the restore, and verifies the record census and onboarding status match
the source — exiting non-zero on any divergence. Run it after configuring a
client and after any risky operation. See runbook *Backup/restore acceptance
check*.

## 7. Home/Tasks hub setup-progress (#204/#330)

A second, narrower progress view sits alongside the wizard above:
`GET /api/v2/setup/progress` (`MANAGE_ARENA` — League Admin **and** Arena
Manager, unlike this wizard's League-Admin-only `onboarding/status`) resolves
the caller's ACTIVE Program **and Season** from the #159 session context and
reports completion for the six Setup workflows #204 names — league profile
and seasons, permanent teams, season participation/divisions, clubs/players/
staff, venues/rinks/ice, and imports/onboarding — scoped to that one Program,
never the whole installation. League profile/seasons and permanent teams are
deliberately Program-wide (an integrity check with no Season dimension of
its own, for teams); season participation and facilities are scoped further,
to the ACTUAL resolved Season, so an older Season's registrations or granted
ice can never mask required work in a newly-selected Season.

The Dashboard's Home/Tasks hub card leads with a single "Continue setup"
primary action naming the actual next incomplete workflow and deep-linking
straight into that workflow's real entry point — a create drawer, the Ice
Availability Builder, or the Setup hierarchy tree, never a generic Setup
landing; the other five list below as a non-competing secondary list
(today's `.act.primary` convention — see #204's "one primary action per
screen" principle), each with an accessible, visible-text status ("Done" /
"To do" / "Optional" — never an icon-only badge).

`next` is the FIRST todo workflow, in the fixed #204 order (league profile/
seasons → permanent teams → season participation → clubs/players/staff →
venues/rinks/ice), that the caller's role can actually execute (an Arena
Manager, who holds `MANAGE_ARENA` but not `MANAGE_SETUP`, is routed to
facilities, never to a League-Admin-only action like "Add Season") AND that
is actually safe to run given the resolved Season's own state (#331 review
rounds 3–5): both "facilities" and "participation" need a resolved, ACTIVE
Season (their real writes both route through the same #159 active-Season
guard and fail identically — `season_missing` with none resolved,
`season_archived` if the resolved one is archived). "participation" needs
this for the same reason `commit_ice_availability` needs it for facilities,
plus one more: its real destination on the Home/Tasks card,
`focusParticipationRegisterControl()`, needs an exact selected Season to
deep-link/focus the one specific Register control the card promises (#330's
round-2 review requirement) — with none resolved it can only fall back to
a generic, unbound landing on the Setup tree.

Beyond the Season itself, each has one more hard floor that would otherwise
leave its CTA a guaranteed dead end even with an active Season resolved
(#331 review round 5 findings 1/2) — both are existence checks the real
write also has no way around, not heuristics:

- **"facilities"** needs at least one Rink reachable via active
  `SeasonVenueAccess` for the resolved Season. With none, every Rink the
  Ice Availability Builder could offer lands in `venue_access_missing`, so
  a preview provably generates zero slots no matter what the operator
  picks — and an Arena Manager cannot grant that access themselves
  (`MANAGE_SETUP`-only), so this is a true dead end, not a gap the same
  role could close. Reported as `next_blocked.reason: "venue_access_missing"`.
- **"participation"** needs at least one of the Program's Teams to be
  eligible for the resolved Season's League(s) — no permanent League yet
  (an ordinary, if rare, state for a legacy-imported Team), or a permanent
  League that matches one of the Season's own `LeagueSeason`s. A Team WITH
  a permanent League can only ever register into a `LeagueSeason` of that
  same League (`register_team_for_season` rule 7); with none of the
  Program's Teams eligible, every possible registration is a guaranteed
  `team_league_mismatch` rejection regardless of which Team the operator
  picks in the control. Reported as `next_blocked.reason:
  "team_league_mismatch"`.

Critically, `next` never skips AHEAD to a later, incidentally-safe workflow
just because the first todo one is blocked — #330's "actual next incomplete
step" is a strictly ordered contract, and reordering it around a blocker
would read as that step being skipped or forgotten rather than blocked. The
FIRST todo workflow this role can manage is the one this whole prerequisite
check applies to: if it's safe, it's `next`; if it's blocked, `next` is
`None` and `next_blocked` names that same workflow with a reason code and a
plain-language explanation of what to resolve first, so a role that cannot
resolve it themselves (an Arena Manager blocked on a Season only a League
Admin can create) is still told clearly rather than routed into a silent
failure or a workflow further down the list.

The `workflows` list itself is also filtered to what the caller's role can
manage, not global (a reversal of the original design — an Arena Manager
must never receive League-Admin-only completion signals or exact team/
registration/player counts, the same role/privacy boundary `next`'s own
filter exists to hold). `complete` is still computed from the full,
unfiltered internal list first, so its raw value keeps meaning "the WHOLE
Program's setup is done," never flipping true just because the one workflow
a caller can see happens to be done — but that value is only ever *exposed*
to a role whose `workflows` already equals the full list (today, League
Admin); anyone with a narrower view (Arena Manager) receives `null` instead
(#331 review round 5 finding 3). Exposing the real boolean unconditionally
still let a change to a workflow that role can't even see flip a bit in
their own response — an information leak through the same redaction
boundary `workflows` itself holds. `null` is neither an overclaimed `true`
nor an independently meaningful `false`; it is constant regardless of
invisible state, so by construction it carries none of it. The
complete-state secondary "Import data" action inherits the `workflows`
filter: it renders only when "import" survives that same per-role filter,
so an Arena Manager (MANAGE_SETUP-only workflow) never receives an enabled
action for a surface they cannot use — and since the success state itself
only ever renders on a real `true`, an Arena Manager can never reach it
either, even when the whole Program happens to be genuinely done.

Once every required workflow reads done the card shows the required success
state ("All setup steps complete" plus a Schedule link) instead of
disappearing; a failed fetch shows a per-card error with a working Retry
rather than silently rendering nothing, and a monotonic fetch-sequence guard
discards a stale response that resolves after a newer one (e.g. a slow
fetch completing after a context switch already rendered the fresher
result).

Every destination `next`'s CTA opens is seeded from the ACTIVE Program/
Season, not left to fall through to whatever happens to sort first in that
destination's own (Program-unfiltered) option list (#331 review round 5
finding 4). `goToSetupWorkflow()` resolves the correct parent field — the
active Program itself for "Add Season", the active Program's own permanent
League for "Add Team", one of its own Teams for "Add Player" (each via a
fresh fetch of the canonical Program→League→Team hierarchy, not the
Setup screen's own cached state, which can be stale or entirely
unpopulated when this CTA is clicked straight from the Dashboard) — and
pre-fills it into the create drawer before opening it. `defaultIceForm()`
does the same for the Ice Availability Builder's own Season selector,
preferring the #159 active Season over "the first `status === "active"`
Season it finds," since that list also spans every Program. None of this
changes what these destinations' own dropdowns OFFER (they stay
Program-unfiltered, matching every other entry point into the same
drawers/builder — #159's active-context selection is still a display-only
convenience, not a sitewide data filter); it only fixes what they default
to, so a silent submit against the wrong Program can no longer happen from
this hub's own navigation.

That seeding fails CLOSED, not open (#331 review round 6):
`contextSeededDrawerValues()` returns a discriminated `{ok, values}` rather
than a bare values object, and re-checks the active Program's id AFTER its
own `await` against the value captured BEFORE it. Either a failed hierarchy
fetch or a context mismatch — the operator using the unrelated #159 context
switcher while that fetch is still in flight — returns `{ok: false}`;
`goToSetupWorkflow()` then shows an error toast and returns without opening
the drawer or switching tabs, leaving the operator on Home/Tasks with the
CTA itself standing in as the retry. Without this, either failure mode
reopens exactly the risk the paragraph above closed: an empty seed (`{}` on
fetch failure) or a stale one (the OLD Program's values, resolved after a
NEWER Program already won the switch) both fall straight into the shared
field's own first-global-option fallback, once again risking a write under
the wrong Program. A second, narrower guard (`drawerSeedFetchSeq`, mirroring
`loadSetupProgressCard()`'s own `setupProgressFetchSeq`) covers a different
trigger — a second, faster click on the same CTA superseding an in-flight
first one — by discarding the older call's result outright rather than
letting it race the newer one to paint.

Two more hub-adjacent surfaces had the identical class of gap, closed the
same round (#331 review round 7): the Import wizard's chosen Season, and an
already-open Ice Availability Builder's whole form and preview. Neither
`goToSetupWorkflow("import")` (which only switches tabs) nor
`setActiveContext()` (the switcher's own handler) ever touched either one,
so a Season/Program switch made mid-review — while the operator is still
looking at an Import form or a live Builder preview from the PRIOR
selection — left both cached against the old context. Both send their
Season verbatim to a real commit endpoint (`commit_import`,
`commit_ice_availability`), and #159's context is display-only, not a
backend filter, so nothing else would have caught it either — a real,
committable cross-Program write, not a cosmetic staleness. A single
monotonic `contextRevision` counter, bumped by every successful
`setActiveContext()` call, gives every context-scoped view a cheap way to
tell "still the same selection" apart from "changed and changed back": each
stamps the revision it was last bound under, and a mismatch is unambiguous
proof something needs rebinding. Import rebinds its Season (and discards
any already-validated report/committed result — a stale review is worth no
more than a stale seed) on every render while `view === "import"`, whether
reached fresh via the hub CTA or already open when the switch happens; with
no Season actively selected it fails CLOSED to an explicit, disabled
placeholder option, never a fresh global-first default disguised as "no
selection made yet." The Ice Builder discards its ENTIRE cached form (not
just the Season field) and any live preview on a revision mismatch, since
rink selections are just as Program/Venue-scoped as the Season is; clearing
the preview alone is what makes a stale one uncommittable, since Create is
already bound to a previewed template's own fingerprint and renders only
when a preview exists.

Round 7's own fix still had a gap (#331 review round 8): `contextRevision`
only bumped AFTER `setActiveContext()`'s `/api/context` POST succeeded, not
when the switch was merely ATTEMPTED — the native `<select>` already shows
the new choice, and any other in-flight async work (a hub drawer's own
hierarchy fetch, a Commit/Create click) already reads the PRIOR selection,
well before that round trip can complete. A drawer seed racing the switch
could still open with a stale value; an Import Commit or Ice Builder Create
clicked in that window still targeted the Program the switcher no longer
showed. `setActiveContext()` now invalidates synchronously, in the same
tick as the switch intent, before its own `await`:
`invalidateContextScopedMutations()` clears `importState.report`/
`validatedKey`/`committed` and `iceBuilder.preview` outright (both
Commit/Create handlers already re-check these fresh at click time, so
clearing them is enough on its own — Ice Builder's own Create handler
gained the identical belt-and-suspenders bail-out Import's already had,
rather than trusting the server's fingerprint rejection alone), and
surgically removes any open hub drawer's DOM node directly (not via a full
render(), which would also re-paint `#ctx-select`'s own option list from
the still-stale `contextOptions.selected` and visibly snap the switcher
back for the round trip's duration). `contextRevision` itself bumps TWICE
per switch now — once at intent, so `contextSeededDrawerValues()`'s own
in-flight-fetch guard and the render()-time rebind checks above already
read "changed"; again once the canonical new selection is confirmed, so
that same rebind logic actually re-seeds against it rather than finding its
own stamp already "current" from the first bump and skipping the reseed. A
second counter, `contextSwitchSeq`, bumps on every call regardless of
outcome so a rapid A→B→C — however its three responses happen to race back
over the wire — only ever lets the LATEST attempt apply its own
POST/refresh/render result; a superseded one recognizes that and does
nothing further, on success or failure alike. None of this is scoped to a
Program/Season switch alone: `resetTransientUiState()` — already
`setUser()`'s identity-transition hook, firing on sign-out, sign-in, and
the demo persona-switch dropdown alike — now resets `importState` to its
own initial shape and fully closes the Ice Builder (`iceBuilder = null`,
its own "not open" sentinel) the same way, since a no-reload identity
change can hand an in-progress paste (real player names/emails), a
validated report, or a live preview to a completely different signed-in
person, lower-privileged or not. Separately, `defaultIceForm()`'s own
Program-only fallback — `seasons.find(active) || seasons[0]`, global and
unfiltered, when no Season is actively selected — was the identical unsafe
default Import's own Season field was already fixed to refuse in round 7,
just never extended to the Ice Builder; it now fails closed the same way,
with the matching disabled-placeholder treatment in its own `<select>`.
Writing the regression coverage for the drawer-race case surfaced a real
false-negative in the test itself, not the fix: checking `#ctx-select`'s
own value is an early signal within a render() cycle (the same gap the
rebind checks above already had to account for), but here TWO independent
render() calls can be genuinely in flight together — the drawer CTA's own
`switchTab("setup")`-triggered one, and `setActiveContext()`'s own
completion one — and only whichever finishes painting LAST decides what
`#content` actually shows; a bare wait on the switcher's own value could
observe a still-mid-flight paint and miss a real stale-drawer render
entirely. The regression now waits for the network to actually go quiet
before asserting against `#content` — the same class of fix round 7 already
applied to an unrelated `logout()` race in the same test file (see that
function's own comment in `home-tasks-hub.js`), just not yet generalized to
this render-level race until it produced a real false negative here.

The card's async states carry real accessibility semantics, not just visual
ones (#331 review round 5 finding 5): `#sp-card-slot` (the wrapper
`loadSetupProgressCard()` swaps content into, itself painted once by
`render()` and never replaced) carries `role="status"`/`aria-live="polite"`
for the whole of its lifetime, so a screen reader is told whenever this
region's content settles — loading finishing, a retry resolving, a context
switch's fresher response landing — without needing separate handling per
transition. `aria-busy` tracks the fetch itself, not just the very first
one: true from the moment any fetch (including a retry) starts, false once
its result has actually painted. The loading state is a real, heading-
bearing state of its own now (`<h3>Setup progress</h3>` plus a visually-
hidden label), not a bare, unlabeled skeleton excluded from both screen
readers and the same accessibility scan every other state already passes —
excluding a state from the gate is not the same as it having no
accessibility surface to gate. The error banner additionally carries
`role="alert"`, an assertive announcement distinct from the outer region's
own polite one, since a failed fetch is more urgent than routine settling.

This is additive to, not a replacement for, the wizard in this slice:
first-Program bootstrapping still belongs to Initial Setup above — an
operator with zero Programs sees nothing from this card (`get_setup_progress`
returns an empty `workflows: []` rather than inventing progress for a Program
that does not exist yet), and a fresh, genuinely-incomplete League Admin
session still lands there first. Per #204's IA crosswalk the wizard is slated
to eventually fold into this same hub's "Imports and onboarding" workflow
rather than stay a standalone screen — not done as of this slice.

"Imports and onboarding" (the sixth workflow) reports a third status,
`"optional"`, instead of `"done"`/`"todo"`: it is a standing, always-
available alternative entry point into the other five (bulk-import teams/
players, officials/availability, or rinks/ice-slots), not an independently
gated step. Unlike the other five, there is no reliable Program-scoped "has
an import ever run here" signal to compute a real done/todo state from —
two of the three import-commit paths write only aggregate counts into their
own audit summary row, no season- or program-derivable field. An earlier
shape derived "done" from whether the other five happened to all be done,
which was an invented rule with no such grounding and, as a side effect,
made this workflow impossible to ever surface as `next`. `"optional"` is
never a candidate for `next` and never blocks `complete`, but the workflow
stays fully visible on the card and independently reachable at all times via
the persistent Import nav tab both League Admin and Arena Manager hold.
Proposed as decision 9 in `docs/product/operator-ux-requirements.md`'s
"Product decisions requiring sign-off" section — implemented as the working
assumption in PR #331, but not yet confirmed by the product owner and
reversible pending that confirmation.

## Authorization and privacy invariants

- The bootstrap claim is the only unauthenticated mutation, and only on a fresh,
  durable, unclaimed installation.
- After claim, all setup and onboarding-status detail requires an authenticated
  League Admin. An Arena Manager manages facility inventory but cannot claim an
  installation, create League Admin accounts, or read client-wide onboarding
  detail — the one exception is the Home/Tasks hub's Program-scoped
  `/api/v2/setup/progress` (§7), which both roles read since both land on that
  hub.
- Passwords and the setup code never appear in responses, logs, audit detail,
  browser state, or toasts. Post-claim actor attribution comes from the session.
- Onboarding status, the setup hierarchy, and the acceptance report expose
  counts and structural names only — never player names or credentials.

## Related documents

- [production-runbook.md](production-runbook.md) — deployment, readiness,
  backup/restore, migration verification, admin claim mechanics.
- [persistence.md](persistence.md) — durable store, migrations, restart survival.
- [data-model.md](data-model.md) / [league-arena-setup.md](league-arena-setup.md)
  — the hierarchy entities and their relationships.
- [api-contract.md](api-contract.md) — endpoint shapes.
