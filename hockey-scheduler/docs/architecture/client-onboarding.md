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

## 7. Home/Tasks hub setup-progress (#204/#330, PR #331 bounded slice)

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

Per the product-owner scope split confirmed on 2026-07-27, this section
documents PR #331's bounded first slice: the Program-scoped setup-progress
contract and Home/Tasks card, deep-links into existing Setup screens, and
role-home regression coverage. The guided Setup hub, broader IA and
context-filtering redesign, full state/error treatment, breakpoint
consolidation, repository-wide accessibility gates, manual accessibility
acceptance, and operator validation remain in #345, ahead of
Schedule/Facilities UX.

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

That ordering is also why `next_blocked` is **not** a per-workflow
prerequisite contract, and must never be read as one (#365 review, Facilities
fail-open). It describes exactly ONE workflow — the first permitted TODO
one — so for a League Admin, whose permitted list starts four workflows
earlier, a facilities gap is simply never reported there. A surface that
derived the Facilities card's own prerequisite from `next_blocked` therefore
failed OPEN for the very role that can resolve it.

So each `workflows[]` row may additionally carry `prerequisites`: an ordered
list of ASSERTED facts about that workflow, for this exact resolved
Program/Season/League tuple, additive and independent of `status` exactly as
`attention` is. Today one row exists, on "facilities":

```json
{"key": "venue_access", "met": false, "reason": "venue_access_missing",
 "detail": "No rink is reachable through active venue access for Season 'Fall 2025' yet, …"}
```

`met` is computed from the SAME `schedulable_rink_ids` set the workflow's own
done/todo check and `_workflow_prerequisite_gap` read — the Rinks reachable
through ACTIVE `SeasonVenueAccess` for the selected Season — so the card, the
roll-up and the Ice Availability Builder's own `venue_access_missing` refusal
can never disagree. It is emitted to every role that can see the workflow
(both League Admin and Arena Manager hold `MANAGE_ARENA`), because the fact is
just as load-bearing for the role that cannot fix it.

The row is deliberately ROLE-INVARIANT — it describes the selected Season's
data, not the caller, and both roles receive byte-identical rows. WHO may
resolve the gap is a separate, permission question: granting
`SeasonVenueAccess` requires `MANAGE_SETUP`, which an Arena Manager does not
hold. The client answers it from the caller's own permission set (the same
`hasPerm` every other control is gated on), so a League Admin is offered the
real venue-access resolution path while an Arena Manager is offered no
mutation control at all plus guidance that a League Admin must grant it.
Restating the permission in the payload would create a second authority on it,
free to drift from the first.

This is deliberately NOT derivable from the Setup overview's Venue/Rink
lists. Those correctly include revoked-grant history and creator-owned
pending rows (see `get_setup_overview_v2`), so "a Rink is VISIBLE" and "a
Rink is SCHEDULABLE this Season" are different claims; the read contract is
right and is not narrowed, and the schedulability claim gets its own asserted
field instead.

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
drawers/builder — a deliberate exception that survived #367/#369, which
scoped the operational READS to the active tuple but left these shared
create-drawer field definitions global); it only fixes what they default
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
`commit_ice_availability`), which takes that id at its word, so nothing
else would have caught it either — a real, committable cross-Program
write, not a cosmetic staleness. (#367/#369 has since scoped the
operational reads and added a parent-id write gate, but neither closes
this: the stale id was chosen while the operator was legitimately in the
OLD context, so it is a valid id — just no longer the one they are
working in. The revision counter below remains the guard.) A single
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

Round 8's fix still left one gap open (#331 review round 9): `contextSwitchSeq`
only ever governs which RESPONSE the browser honors — it never stopped the
underlying POSTs themselves from reaching the server out of order, and
`ContextService.set()`'s own persistence (both store backends) is a plain
last-write-wins overwrite with no generation/version guard at all. A rapid
A→B→C used to fire three independent `/api/context` POSTs; whichever one the
SERVER happened to finish processing last decided what was actually
persisted, regardless of which response the client chose to render — the
switcher (and the URL hash) could show C while the server, and everything
that reads its own persisted context afterward starting with
`/api/v2/setup/progress`, stayed on B. The fix coalesces client-side instead
of adding server-side ordering: `setActiveContext()` now only ever has ONE
`/api/context` POST in flight at a time (`contextSwitchInFlight`); a switch
requested while one is already outstanding is queued
(`contextSwitchQueued`), overwriting any earlier still-queued one since only
the LATEST intent is ever worth sending, and is dispatched immediately once
the in-flight one settles — before that response gets any chance to
reconcile anything. With at most one such POST ever in flight, the server's
own last-write-wins persistence becomes trivially equivalent to "last intent
wins," on either backend, since there is no window left for the two to
disagree; an intermediate pick in a rapid burst can be — and typically is —
dropped without ever reaching the network at all.
`resetTransientUiState()`'s identity-transition hook discards a still-queued
switch outright (`contextSwitchQueued = null`) and bumps `contextSwitchSeq`
again, so a switch the OLD identity initiated but never got a POST out for
can never fire against the NEXT signed-in identity's session once the
in-flight request it was queued behind finally settles; that in-flight
request's own completion recognizes the same way an ordinary superseded
switch already does, via the pre-existing `contextSwitchSeq` check, that it
has nothing left to reconcile either. `restoreContextDeepLink()` (the
separate boot/sign-in-time resolver) is deliberately NOT part of this queue —
it cannot race a user-initiated switch, since the switcher is not yet
interactive when it runs.

A second, distinct gap in the same lifecycle (also round 9): neither Import's
Validate handler nor the Ice Builder's Preview handler had ever checked
`contextRevision` on their OWN async completion, only on the click-time setup
their round-7/8 predecessors already covered. `invalidateContextScopedMutations()`
clears `importState.report`/`validatedKey` and `iceBuilder.preview` the
instant a switch is attempted, but a Validate or Preview request already
in flight BEFORE that moment can still resolve well AFTER the switch has
fully settled — with the pasted sheet text and selected type both unchanged,
which is exactly what defeats Import's own pre-existing
`importSnapshotKey()` staleness check, since that only ever detected the
SHEETS changing, never a context change underneath them. Landing
un-guarded, a straggling Validate response would silently reattach a
report/validatedKey the operator never reviewed under the new context,
re-enabling Commit with no fresh review; a straggling Preview response —
which had no staleness check of any kind — would directly overwrite
`iceBuilder.preview` with a DIFFERENT context's stale slots, re-enabling
Create. Both now snapshot `contextRevision` into a local immediately before
the vulnerable `await` and recheck it after, the same snapshot-before-await,
recheck-after idiom `contextSeededDrawerValues()`'s own `stillCurrent` check
already established in round 8. The Commit/Create counterparts of both
handlers already have a pre-await guard (a fingerprint or `validatedKey`
check) that makes a deliberate click a genuinely-authorized write at click
time regardless — the write itself is not at risk — but their own
post-await RESPONSE handling reaches into the same live, shared
`importState`/`iceBuilder` objects a brand-new context's own action may
already be using by the time a stale response lands: an unguarded success
would paint a misleading "Committed"/"Created" result under a context the
operator never acted in, and — more seriously for Ice Builder's Commit,
whose success path unconditionally nulls `iceBuilder` to close the form —
would silently discard a different context's own brand-new, still-open
builder out from under the operator. Both Commit handlers gained the
identical snapshot-and-recheck guard for this reason, closing the whole
lifecycle class (attempt-time, pending-response, and settled-but-straggling)
in one pass across all four handlers.

Verifying finding A's coalescing design meant redesigning the existing rapid
A→B→C regression, not just re-running it: its original mechanics (independently
holding and releasing three separate POSTs by program id) no longer describe
what the fixed client actually does, since coalescing means an intermediate
pick may never reach the network as its own request at all. The rebuilt
version tracks every `/api/context` POST that leaves the page during the
burst and asserts the coalescing directly — exactly one request for the
first pick, then the LATEST pick's own request firing the instant the first
settles, with the superseded middle pick never appearing on the wire at
all — then reads the server's own persisted context back via a real `GET`
(never inferring persistence from the client's own hash/DOM, which is
exactly the gap finding A exploited), then re-confirms that same persisted
value survives a genuinely hash-free reload (proving real server
persistence, not a client-side or URL-hash artifact), then forces the one
request the fix DOES send to come back rejected and confirms the client
converges on the server's true prior context rather than getting stuck
showing a target that was never accepted. A companion regression holds a
switch in the queue across an identity change specifically, proving
`resetTransientUiState()`'s own discard rather than assuming it from the
identity-clearing coverage above, which never exercises a queued (as
opposed to already-settled) switch. New regressions for finding B hold a
Validate, a Preview, and each Commit response across a FULLY SETTLED switch
(not merely a still-pending one, which the existing coverage already
proved) before releasing them, and the existing identity-transition
coverage was strengthened to check Commit's own enabled state — not only
that pasted text was gone, which alone would not have caught a bug in
clearing `report`/`validatedKey` specifically — and extended with a leg
that signs in as a role holding no `manage_arena` at all, proving the
disclosure risk closed even for an identity that could never open its own
Import wizard or Ice Builder to compare notes; the two admin/arena legs
alone can only ever prove `resetTransientUiState()` itself runs, since both
roles share every permission the state in question is gated behind.

Round 9's fixes still left two gaps open in the same lifecycle (#331 review
round 10). The first was in `sendContextSwitch()`'s own failure branch: when
an intermediate switch in a coalesced burst succeeds while another is
already queued behind it, the function dequeues straight to the queued pick
and returns, so the success branch's own `syncContextHash()` call — which
only runs on that branch — never fires for the intermediate pick at all. If
the queued pick's own request is then rejected, the failure branch already
reconciles `contextOptions` and `#ctx-select` from a real
`loadContextOptions()` call, but never synced the hash to match — so the
server, the options list, and the selector all correctly converge on the
intermediate pick while the hash keeps showing whatever was live before the
whole burst started. A reload then runs `restoreContextDeepLink()`, reads
that stale hash as an intentional deep link, and silently POSTs the
original context back, rolling back a switch the server had already
persisted. The existing "(L, failure convergence)" regression could not
have caught this, since it only ever rejects a single direct switch at a
point where the hash and server already agree beforehand — never a switch
that both succeeded AND had its own hash reconciliation skipped by the
coalescing path. The fix is a single added `syncContextHash()` call in the
failure branch, right after `loadContextOptions()` — always safe to call
there, since it is a no-op whenever the hash already matches. A new
regression, "(L, queued-then-rejected hash sync)", holds one switch in
flight, queues a second behind it, lets the first succeed and the second
(now dequeued) get rejected, then confirms the selector, three independent
backend reads (`/api/context`, `/api/context/options`,
`/api/v2/setup/progress`), and the decoded hash all agree on the first
switch's target — then, unlike every prior reload regression, reloads with
the hash left INTACT rather than stripped, tracking every `/api/context`
POST the reload itself issues to prove no compensating rollback request
goes out.

The second gap was broader: every staleness guard through round 9 —
`contextRevision`, `contextSwitchSeq`, the snapshot-before-await/recheck-
after idiom itself — detects only the ACTIVE CONTEXT changing under an
in-flight request, never a same-context event that should just as validly
obsolete it. Canceling and reopening the Ice Builder, editing its form or
an exclusion while a Preview or Commit is in flight, or simply firing two
Previews (or two identical-input Validates) back to back and letting them
resolve out of order all leave `contextRevision` untouched and `iceBuilder`
non-null throughout, so none of round 9's checks fire. Two new
module-level monotonic counters, `iceOperationSeq` and `importOperationSeq`,
close this the same way `contextSwitchSeq` already closes its own class:
each bumps on every event that should obsolete a not-yet-resolved
Preview/Validate/Commit response for that surface — opening or canceling
the Ice Builder, editing its live form or an exclusion, switching Import's
type, loading Import's sample data, or issuing a newer request of the same
kind — and each handler snapshots the current value immediately before its
own vulnerable `await` and rechecks it after, alongside the existing
`contextRevision` check rather than in place of it. A single global counter
per surface is enough, since only one `iceBuilder`/`importState` is ever
live at a time — no per-instance identity is needed beyond "the latest
token issued." Four new regressions cover the reviewer's own scenarios
directly: canceling and reopening the Ice Builder across a held Preview and
across a held Commit (each proving a stale response cannot resurrect or
discard the WRONG builder instance), two Previews on the same still-open
builder released in reverse order (proving the older response cannot
overwrite the newer one's already-painted slots), and two identical-input
Import Validates released in reverse order (proving a stale success cannot
re-enable Commit after a newer failure already disabled it). The audit
that followed caught a second, previously-missed Ice Builder "open" site —
`goToSetupWorkflow("facilities")`'s own hub-driven entry point, distinct
from the manual "Build ice" button — constructing a fresh builder without
bumping `iceOperationSeq`, and two Import handlers (type switch, sample-
data load) resetting reusable string state rather than a monotonic value,
both fixed for the same reason: `importState.type` and the pasted sheet
text can coincidentally cycle back to a value an in-flight request's own
snapshot already matches, unlike `contextRevision`, which — being
append-only — never can.

Round 10's own sweep still left two gaps in the operation-token coverage
(#331 review round 11). The first: `.ib-form`'s `change` listener only
ever bumps `iceOperationSeq` on blur — a `change` event, by definition,
never fires while a text/date/number field is still focused and being
typed into. A Preview held in flight while the operator is mid-keystroke
into, say, `#ib-playable` therefore saw no bump at all until they
eventually tabbed or clicked away, well after the stale response could
already have landed. The fix adds a second listener on the same
`.ib-form`, this time for `input` (which *does* fire continuously while
focused), bumping `iceOperationSeq` on every keystroke and, if a preview
panel is already showing, removing its DOM node directly rather than
calling the full `render()` this listener's own edit is trying to avoid
mid-keystroke — a full render would rebuild the very field the operator
is typing into and steal focus/cursor out from under them, the same
concern round 8's drawer-removal already had to design around.

Regression-testing this exposed a subtler trap than the fix itself: the
first two test designs both passed even with the fix fully reverted — a
false negative discovered only by deliberately reverting the fix and
finding the "regression" didn't fail. Checking the field's *final*
settled value doesn't work, because `render()` replaces `#content` via
`innerHTML`, which tears down the still-focused, value-changed
`#ib-playable` node — and removing a focused, dirty `<input>` is a
documented native browser behavior that synthesizes a `change` event on
it as part of the removal. That synthetic `change` lands on the
pre-existing, entirely unrelated `.ib-form` listener, which reads the
live (correct) value via `readIceBuilderForm()` and re-renders without
the stale preview — so an incorrectly-accepted stale response and a
correctly-rejected one converge on the identical final DOM, self-healing
the very bug being tested for. A tight poll for the preview panel's
transient appearance *also* failed to catch it: the corrective
re-render, triggered synchronously by the same DOM removal that painted
the stale one, consistently finished after it, regardless of poll
frequency — the two `render()` calls are a race, and the self-correcting
one reliably won in this environment's timing. The regression that
finally works checks neither value nor preview state: it tags the
*original* `#ib-playable` DOM node with a marker property right after
typing, then asserts `document.activeElement` is still that exact
tagged node once everything settles. That signal survives the self-heal
because *any* `render()` call tears the original node down and nothing
in this app ever re-focuses a freshly-rendered field — so the guard
being reverted is detectable by "was the focused element ever destroyed
at all," independent of which of the two racing renders happened to
paint last.

The second gap: `#import-season`'s own `onchange` assigned
`importState.seasonId` but never touched `importOperationSeq` or
participated in Commit's response-ownership check. Switching to a
different Season in the same Program (no context change, so
`contextRevision` doesn't move) while an earlier Commit for the
previously-selected Season was still in flight let that stale response
land and be presented as the newly-selected Season's own result —
including wiping `report`/`validatedKey` state the operator never
actually invalidated. The fix is the same one-line idiom as every other
same-context operation-boundary event: bump `importOperationSeq` in the
handler. Validate's own dry-run body deliberately stays season-agnostic
(its correctness never depended on which Season was selected), so this
only closes the *response-ownership* gap, not a validation contract
change — a distinction worth preserving since the two are easy to
conflate. This one's regression test worked correctly on the first
attempt: no DOM-removal side effect confounds a Commit's own success/
failure banner the way it did the Ice Builder's live form value.

A third, unrelated finding landed in the same round: `commit_officials_
availability_import` and `commit_rinks_ice_slots_import` have no row to
lock for a brand-new natural key. `commit_teams_players_import`'s own
Season row lock (acquired via `get_season_for_update` as the first
statement inside its transaction) already serializes concurrent commits
against each other, but officials and rinks are both deliberately *not*
season-scoped — there is no equivalent parent row two concurrent
commits could contend on before checking whether an `official_code` or
`rink_code` already exists. Two commits landing the identical new key
could each see it absent and each create their own row: a duplicate
Official, a duplicate availability window even for an *already-existing*
Official, or a duplicate Rink (and, since Venue creation for a new Rink
happens in the same transaction, a duplicate Venue riding along with it).

The fix mirrors `commit_ice_availability`'s own established idiom rather
than inventing a new one: migrations 047 and 048 add unique indexes —
`officials.external_ref`, `official_availability(official_id,
start_time, end_time)`, and `rinks.external_ref`, each a partial index
(`WHERE ... IS NOT NULL`) since the columns are legitimately absent for
non-imported rows, matching migration 023's identical reasoning for
`game_roster_entries`. Both commit functions now wrap their transaction
in the same three-attempt retry loop `commit_ice_availability` already
uses: a race-losing INSERT is translated to the stable
`IntegrityConflictError` shape `db_errors.translate_db_exception`'s
generic `unique_violation` fallback already produces (no new translator
function needed — migration 045 already established that this generic
path exists), the whole attempt rolls back, and the retry's fresh
absence-check sees the winning transaction's committed row and correctly
takes the update path instead of inserting a second one. Because a new
Rink's Venue is created in the *same* transaction as the Rink itself, the
Venue-by-name race resolves for free: rolling back the losing side's
whole attempt also undoes its own uncommitted Venue insert, so its retry
finds the winner's already-committed Venue rather than creating a
second one. This deliberately does not close every interleaving of the
*separate* Venue-by-name match on its own (e.g. two different new
`rink_code`s that happen to share one brand-new venue name) — that is
structurally the same unlocked check-then-create as
`commit_officials_availability_import`'s own Club-by-name match, already
accepted as out of scope for the identical reason, and whether Venue
names should be globally unique is a product decision this migration
does not assume.

Forced PostgreSQL regressions for both paths use the codebase's
established two-independent-connections pattern (`SeasonArchiveRaceTest`'s
own template): each side drives its own `ApiService(SqlStore(url))`, a
`threading.Barrier` pauses each side's `next_id()` call for the specific
prefix under test (`"official"`, `"oavail"`, or `"rink"`) rather than the
write call itself — pausing any later would risk a self-inflicted
circular wait, since `next_id()` itself upserts a shared per-prefix
counter row and could leave one side holding that row's lock while
blocked on the same barrier the other side's own `next_id()` call needs
to clear. A companion test isolates the availability-window race
specifically, since the primary officials-race test only ever contends
on `next_id("official")` — the window insert never actually races
between the two threads in that test (the losing side's retry already
finds the Official and takes the update path before it ever reaches its
own window insert), so it cannot by itself prove the *second* unique
index is load-bearing. Fixing the underlying race also broke an
unrelated, pre-existing legacy-adoption test
(`test_migrations.test_adoption_over_legacy_marker_is_safe`, which
simulates a pre-#94/#95 database by manually dropping the columns these
migrations' indexes now depend on): the same `DROP INDEX IF EXISTS`
before the indexed column drop the test already applies for the #173/
#174 columns now also covers `officials.external_ref` and
`rinks.external_ref`.

Migrations 047/048 backstop the Official/Rink row itself, but round 11's
own comment on migration 048 already flagged what they deliberately left
open: the Club/Venue each is found-or-created *from* has no unique-by-name
index of its own, so the identical race can still land one step earlier in
the same chain (#331 review round 12 finding 1). Concretely: two
concurrent commits can both observe a brand-new `home_club_name` (or
`venue_name`) absent, both create their own Club/Venue row, and — because
neither insert violates any constraint — the retry loop never fires at
all. The *loser* then resolves its Official/Rink lookup fresh, now sees
the *winner's* already-committed Official/Rink (protected by 047/048), and
silently updates it to point at the loser's own orphaned Club/Venue rather
than the winner's. Migration 047/048's coverage of the child key actively
hides this: nothing about the sequence raises `IntegrityConflictError`, so
from the caller's side both commits simply "succeed."

A further unique index on `Club.name`/`Venue.name` would close it the same
way 047/048 closed the child race, but would also assert a new,
unreviewed product invariant — global name uniqueness — neither migration
was willing to assume (048's own comment names this explicitly). Round 12
closes the gap a different way instead: double-checked locking over
`next_id()`'s own cross-connection counter-row lock, the same mechanism
the retry loops already depend on for their synchronization, repurposed
here as a mutex rather than a pure id generator. Each find-or-create now
calls `next_id("club")`/`next_id("venue")` *before* creating — which
blocks a concurrent creator until this transaction commits or rolls back
— then re-checks absence, now guaranteed fresh, before actually
inserting. A reservation that goes unused (the re-check finds the row
after all) is simply a harmless gap in that id's sequence, consistent
with this codebase's own "ids are opaque strings" convention. No schema
change, no new product decision required — the fix lives entirely in
`setup_service.py`, at the two call sites, with migration 048's stale
"deliberately does not close" comment updated to point at it.

The second finding was a genuinely different race in
`commit_rinks_ice_slots_import`: the booked-slot `slot_type` gate — which
exists so a repeat import can never retype a slot a Game already depends
on staying `GAME`-bookable — ran as a lock-free preflight, entirely before
the transaction and its per-rink row lock (#331 review round 12 finding
2). `create_game` takes that identical rink lock before allocating a slot.
So a Game could commit in the exact window between the import's stale
preflight read (which correctly saw no Game yet) and the import's own
lock acquisition moments later — at which point the import would proceed
to overwrite `slot_type` unconditionally, using an answer that was true
when read but false by the time it was acted on. The existing
`game_using_ice_slot` check the import's update path *already* runs
(guarding `status`, not `slot_type`) sits on the correct side of the lock;
the `slot_type` gate simply wasn't colocated with it. The fix folds the
`slot_type` check into that same post-lock pass — literally the same loop
already computing the overlap gate's exact-tuple match, since both need
the identical "is this update-path row already the Game's slot" answer —
so the only version of this check that ever runs again is the one made
fresh, under the lock, at the last possible moment before any write. The
lock-free preflight is deleted outright rather than kept as an early exit:
a stale-but-cheap early check that must be re-verified anyway adds nothing
but the appearance of safety.

Forced regressions for both findings extend the same two-independent-
connections pattern round 11 established. Finding 1's races use two
distinct new officials/rinks (different `official_code`/`rink_code`, so
migration 047/048's own indexes can't be what saves the test) that share
one brand-new `home_club_name`/`venue_name`, with the barrier at
`next_id("club")`/`next_id("venue")` specifically — isolating the Club/
Venue race from the already-covered child-key race. Finding 2's test
can't use a `next_id()` barrier at all, since nothing about the race turns
on id generation; it instead patches `get_rink_for_update` on the
import's own store to pause — via a `threading.Event`, not a `Barrier`,
since the two sides need strict *ordering* (create_game fully commits
before the import's lock attempt resumes) rather than simultaneous
release — right before the import would acquire the target rink's lock,
letting an independent-connection `create_game` claim the slot first. All
three tests assert the same shape of outcome: exactly one surviving
parent row with every child correctly pointing at it (findings 1), or a
stable rejection with zero partial writes and the Game still owning its
slot (finding 2) — falsifiability-verified by disabling each guard in
isolation and confirming the specific new test fails for exactly that
reason before restoring it.

A third, non-code finding closed this round: issue #330 lists new schema/
migrations as an explicit non-goal, and migrations 047/048 (added in round
11 to fix a release-blocking data-integrity bug in already-shipped
officials/rinks import code from #94/#95) are exactly that. Recorded as
decision 10 in `docs/product/operator-ux-requirements.md`, the owner
confirmed them on 2026-07-27 as a narrow prerequisite for the pre-existing
import data-integrity defect. The PR body's "Data/privacy impact" and
rollback notes describe the migrations as existing rather than claiming
none exist.

Round 12's own convergence review (#331 review round 13) found two further
gaps in the same import-commit family, both closed without any additional
migration.

The first: `commit_rinks_ice_slots_import`'s lock plan is itself built from
a snapshot — `_existing_rink_by_code` — taken *before* the lock loop that
reads it runs. A `rink_code` absent from that snapshot is therefore never
locked and is treated by both the `slot_type` gate and the overlap gate as
"brand-new, nothing persisted to protect" — but a concurrent transaction
can create that exact `rink_code`, a `GAME` slot on it, and even a Game
booked on that slot, in the gap between this attempt's snapshot and its
lock acquisition. Round 12's own regressions for the `slot_type`/overlap
gates both pre-seeded their target Rink specifically, so neither could
ever exercise this: the snapshot already included it in every case they
tried. Closed by re-resolving every requested `rink_code` immediately
*after* the lock loop, mirroring `commit_ice_availability`'s existing
`_SeasonReparented` internal-retry-signal pattern: if any code that was
absent from the original snapshot now resolves to a real Rink outside the
locked set, the whole attempt is stale — raise `_RinkLockPlanDrifted`,
caught by the same bounded retry loop the child-key race already uses, and
retry with a fresh snapshot that will lock and gate the Rink correctly.
One fix closes both the `slot_type` and overlap variants, since both read
the same lock-plan-derived state. Regression tests for each variant pause
the import's `get_rink_for_update` call on a second, unrelated,
already-existing Rink (not the drifting one — it was never locked at all,
so there is nothing on it to pause) to prove the snapshot has already run
and missed the new Rink before letting the concurrent write land.

The second: `commit_teams_players_import` has the identical unlocked
Club-by-name find-or-create round 12 fixed in the officials import, but
was never touched — and, unlike the officials/rinks imports, only locks
its own target Season, so two imports for *different* Seasons never
serialize against each other at all. `team_code` and `player_code`,
documented in this method's own docstring as globally stable external
references, have the same unprotected shape with no durable uniqueness
backstop of their own (migration 009 only adds the columns). The Club fix
is extracted into a single shared helper,
`_find_or_create_import_club`, used by both
`commit_officials_availability_import` and `commit_teams_players_import`
(a new regression drives one of each import type at the identical new
club name specifically, to prove they share the one serialization point
rather than each having its own, independently-fixed copy). `team_code`
and `player_code` get the identical reserve-then-recheck treatment inline.
One planned regression — isolating the Player race alone, with the Team
already pre-existing for both sides — turned out not to be constructible:
every audit row this method writes, including the *unconditional*
`team_updated` audit a pre-existing Team always gets, shares one global
`next_id("setupaudit")` counter, so two commits that both reference an
existing Team already fully serialize against each other at that earlier
point, before either could reach the Player step at all. Forcing it
anyway reproduced the exact self-inflicted circular-wait class this file's
other `next_id()`-pausing tests are already careful to avoid. The combined
Team+Player regression (both racing on a brand-new `team_code`, carrying
an identical `player_code`) still proves the required end state — exactly
one Team, exactly one Player, and a correct per-Season registration on
each side — without hitting that trap.

A final, non-code correction: round 12's own text described the
officials/rinks import-commit code as "this PR's" or already "introduced"
by it. `commit_officials_availability_import`, `commit_rinks_ice_slots_import`,
and `commit_teams_players_import` all predate this PR (#93/#94/#95,
already on `main`) — this PR's review process found and fixed concurrency
bugs in that existing code, it did not write the code being fixed.
Decision 10 and the PR body are corrected accordingly, along with the PR
body's rollback note: rounds 11–13's fixes change those three functions'
own internal locking/retry/validation-ordering behavior, so the code
revert is not purely "additive-only" the way the new route and Dashboard
card are, even though no public API contract changes.

Round 13's own convergence review (#331 review round 14) found three
further gaps in the same import-commit family.

The first: round 13's `_RinkLockPlanDrifted` recheck re-resolved every
requested `rink_code` after the lock loop, but only compared *code
presence* against the original snapshot — it never verified that the
freshly-resolved row was the *same row*, by id, that the lock loop had
actually locked. Two distinct gaps followed from that: a Rink deleted and
recreated under the identical code between the snapshot and the recheck
passes the code-presence check with a different id than the one held
under lock, so the lock protects nothing; and even when the recheck
itself is sound, the apply phase re-resolved `rink_code` a second time via
its own fresh `all_rinks()` scan rather than reusing the verified
mapping, reopening the same window one step later for a Rink that arrives
after the recheck but before the write. Round 12's regressions couldn't
reach either path, since both pre-seeded their target Rink and so never
exercised "a Rink appears where the snapshot found none." Closed by
building one id-verified `_rink_plan`: for each requested code, the
freshly-resolved row is trusted only if its id appears in `_locked_by_id`
(the map of rows the lock loop actually acquired); any other outcome
raises `_RinkLockPlanDrifted` for the existing retry loop to catch. Both
the overlap gate and the apply phase's rink resolution now read `_rink_plan`
exclusively — the apply phase's separate fresh scan is deleted rather than
kept as a second source of truth. One fix closes both sub-cases, since both
trace back to the same un-pinned re-read. Four new regressions (delete-
recreate id substitution and post-recheck arrival, each proven against
both the `slot_type` retype and the overlap gate) pause the import's own
`get_rink_for_update` on a second, already-locked, unrelated Rink to give
an independent connection room to land the drifting write before the
paused side resumes.

The second: `commit_teams_players_import`'s Program-level gate check
(round 12) had no League-level counterpart — a team's target League could
drift under a concurrent import exactly the way its Program already could
before round 12's fix, uncaught. A new read-only helper,
`_resolve_import_target_league_id_readonly`, predicts the target permanent
League the same way the real resolution path would, without performing
any of that path's auto-create side effects, and gate-checks it alongside
the existing Program check (documented residual: it returns `None`, and
so skips the check, for the rare case where the real resolution would
itself need to auto-create a League — a narrower gap than the one being
closed, not a new one). Separately, the per-row update/create branch for
an existing Team never confirmed the Team it found under lock was the same
one the gate-check pass had already validated against; a Team created
concurrently, between the gate check and the lock, could be silently
adopted via the update branch, bypassing every gate the create branch
would have run. Closed with the same drift-then-retry shape as the Rink
fix: if a locked Team's code was absent from the gate-check-time snapshot,
raise the new `_TeamLockPlanDrifted` rather than proceed, caught by
wrapping the whole import transaction in the existing bounded retry loop
(three attempts, `ConcurrencyConflictError` with reason
`team_import_raced` on the last). Round 13's own positive-case regression,
`test_identical_global_team_and_player_codes_across_seasons_does_not_duplicate`,
assumed the pre-round-14 behavior — a racing duplicate silently
deduplicating — as correct; the widened gate now rejects the loser
instead, so the test is rewritten to assert exactly that
(`..._the_loser_is_rejected`), plus a cross-Program variant and a
positive control proving two Seasons legitimately sharing one League via
`create_league_season` still both succeed.

The third: `hierarchy_import.py`'s `commit_hierarchy_import` (the #260
Slice F nine-sheet importer) is a separate code path from the three
pilot importers above, with its own `upsert_imported_team`/
`upsert_imported_player` helpers — and those helpers had no protection at
all against a *pilot* import creating the identical global `team_code` or
`player_code` concurrently; only pilot-vs-pilot and hierarchy-vs-hierarchy
races were ever covered. Closed with the same reserve-then-recheck shape
`commit_teams_players_import` already uses for its own create branch: the
new id is reserved via `next_id` first, then `all_teams()`/`all_players()`
is re-read; a matching `external_ref` appearing there raises the new
`_HierarchyTeamOrPlayerDrifted` instead of creating a duplicate, caught by
wrapping `commit_hierarchy_import`'s transaction in the identical
three-attempt retry loop. Forcing genuine two-connection concurrency for
this cross-importer race hit four self-inflicted circular waits in turn —
the Season lock, the League lock `commit_hierarchy_import` cannot avoid
taking (required for its own sheet validation), the unconditional
Team-row lock both importers take, and, underneath all of them, the one
global `next_id("importbatch")` counter every import commit path reserves
essentially as its first statement and holds for the whole transaction —
each root-caused with a timestamped diagnostic script rather than assumed
by analogy. Team's race is constructible as a genuine two-connection pause
in one direction — hierarchy commits first while pilot is paused inside
its own `next_id("team")` reservation, using a two-season seed so the
Season lock doesn't serialize the two sides — because that pause point
precedes pilot's own involvement with any shared counter. The reverse
direction (pilot commits first, hierarchy discovers) is not: the global
`next_id("importbatch")` counter every import commit path reserves
essentially as its first statement, and holds for the whole transaction,
means pausing hierarchy at any point still leaves it holding that counter,
and pilot's own commit cannot even begin without it — a self-inflicted
circular wait, not a race. Player's version of the same asymmetry is
worse: pilot's `players_csv` row always requires a `teams_csv` row for
the same team in the same upload, and processing that row unconditionally
writes an audit entry that reserves the one global `next_id("setupaudit")`
counter *before* pilot's player loop ever runs — so pausing pilot at any
point at or after its own player processing already holds that counter
too, and hierarchy's player-row audit write blocks behind it exactly like
the importbatch case. Confirmed by direct reproduction (giving each side
its own separate, non-colliding team still forced an 8+ second block) for
both Player directions, not just the one Team's own asymmetry already
predicted.

Both non-constructible directions are instead covered by the same
stale-snapshot technique: `all_teams()`/`all_players()` on the *would-be-
paused* side's own store is patched so its first call (or first two, for
`commit_teams_players_import`'s player loop specifically — an earlier,
identity-unrelated jersey-release snapshot occupies the position a naive
single-call patch would target) after a tracked `next_id("importbatch")`
marker returns a result that omits a row a separate, already-fully-
committed connection just created, while the final call — the code's own
internal reserve-then-recheck — always sees real, current data. This
exercises the exact drift a genuine race would produce, against a real
PostgreSQL-backed store, through the complete retry-and-recovery path,
without literal thread interleaving; each such test's docstring states
plainly that it proves the recovery path rather than the underlying
race's constructibility. Four tests in total cover both entities in both
winner directions — Team's genuine-pause direction plus its stale-snapshot
reverse, and Player's two stale-snapshot directions, the second of which
(hierarchy commits first, pilot's own reserve-then-recheck discovers it)
closes a gap the first round-14 pass left: the combined Team+Player
regression alone cannot independently prove Player's late-winner branch,
since Team/audit serialization already settles that test before Player's
own logic is exercised, a point #331 review round 14 raised directly.

A correction to round 13's own PR reply: it described a third home-tasks-hub
e2e retry as having been run "three more times," passing once. The actual
transcript shows only one additional run after the two already reported,
and it passed — the true tally across this PR's investigation is two
failures and three passes with round 13's changes present, one pass with
them reverted, consistent throughout with the pre-existing, already-tracked
#337 frontend `#import-season` race rather than anything round 13 changed.

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
This is decision 9 in `docs/product/operator-ux-requirements.md` and was
confirmed by @jingizoo on 2026-07-27; PR #331 implements the approved
contract.

Round 14's own convergence review (#331 review round 15) found two further
gaps, both in the import-commit family the last several rounds have been
converging.

The first: `commit_teams_players_import`'s round-14 League-level gate
predicted the target League via `_resolve_import_target_league_id_readonly`,
which returned `None` — silently skipping the gate for that row — whenever
the row's Division or LeagueSeason didn't exist yet, rather than treating a
missing auto-create target as "nothing to compare." An existing Team
already bound to permanent League A, re-imported into a Season whose own
League is B (or with no League yet at all) via a row naming a not-yet-
existing Division, committed cleanly: apply preserved `Team.league_id = A`
while writing the new registration under League B's freshly-resolved
LeagueSeason, violating the same `Team.league_id ==
registration.LeagueSeason.league_id` invariant round 14 closed for the
race case specifically. The correction is a full plan freeze rather than a
narrower patch: an unlocked pre-read gathers every candidate Team this
batch's `team_code` rows could resolve to, then — *before* the Season
guard, honoring the codebase's own canonical Team → League → Season lock
order (`register_team_for_season`'s established comment; inverting it
risks a PostgreSQL deadlock against `delete_league`/`transfer_team_to_
league`) — every candidate Team is row-locked in deterministic (id-sorted)
order, followed by every distinct League among them. A new
`_resolve_import_team_target_league` replaces the old readonly resolver:
it never returns `None` when the Team already has a permanent League,
returning that League as the predicted target for both a not-yet-existing
named Division and a blank one, and only falling back to the Season's own
ambient default for a genuinely League-less Team — closing the exact
skip-the-gate gap while also tracking, across every row in one gate pass,
whether two different Teams' rows predict *different* Leagues for the
*same* not-yet-existing Division name (a real same-upload conflict apply
can only resolve one way, rejected with the new `import_new_division_
league_conflict` reason rather than silently resolved by row order). A
matching apply-side `_bind_import_team_league_season` binds a new
Division/LeagueSeason to the Team's own League when it has one — safe to
call this deep inside an already-Season-locked transaction specifically
because the pre-lock pass above already holds every League it could
resolve to. A post-lock verification (id, not just code presence, mirroring
the Rink fix's own discipline) confirms every row's resolved Team is the
identical one the pre-lock pass actually locked, retrying the whole attempt
via the existing `_TeamLockPlanDrifted` signal on any mismatch — covering
both a Team deleted and recreated under its code and one that simply didn't
exist at pre-read time. Regression coverage spans both non-race
reproductions from the review (Memory/SQLite/HTTP), the same-upload
conflicting-Division case, and a forced two-connection PostgreSQL transfer
race (an existing Team's permanent League changes between this attempt's
unlocked pre-read and its row lock); the desktop/390px import smoke test
gains a same-upload-conflict case asserting the structured error rather
than a bare `Committed` state.

Re-verifying this fix against the full suite surfaced a further, non-review
correction: round 14's own `test_identical_global_team_and_player_codes_
across_seasons_the_loser_is_rejected` — two Seasons under one Program, each
with its own distinct permanent League, racing to create an identical new
`team_code` — encoded a contract round 15's own required correction
explicitly supersedes. That correction reads, in full: "[e]ither bind the
Team's existing compatible League into the target Season or reject … never
preserve League A while registering under League B" — and its own two
reproductions above are the *sequential*, non-race case of exactly this
topology, required to succeed by binding to the Team's own League.
`_link_league_season`'s only real compatibility constraint is a shared
Program (rule 5), which both Leagues here satisfy, so there is no
principled basis for rejecting the concurrent case while the sequential one
is now required to succeed — the create-race loser now adopts the winner's
League into its own Season (creating that second `LeagueSeason` binding as
a side effect) exactly as `test_shared_permanent_league_across_two_
seasons_both_racing_imports_succeed` already proves for a League an
operator pre-bound to both Seasons by hand. The test is rewritten (`..._
converge_on_one_league`) to assert that outcome instead — both sides commit,
exactly one Team and one Player survive, and both Seasons' registrations
resolve to whichever League the winner actually established — and
falsifiability-verified against the old contract: reverting the target-
League resolver to the Season's ambient default (ignoring the Team's own
League) makes the second side observe `team_permanent_league_move_blocked`
again instead of a second commit. The adjacent cross-*Program* variant
(`..._across_different_programs_the_loser_is_rejected`) is untouched by any
of this — gate (1)'s Program-grain check, and the review's own correction,
both still reject a genuine cross-Program move outright.

The second: `commit_hierarchy_import` (the #260 Slice F nine-sheet
importer) snapshots every `player_code` it will touch exactly once, near
the top of its transaction, via one `all_players()` call — then passes
those same objects through swap-safe jersey release and, unchanged, into
`upsert_imported_player`, which validated and saved an existing Player with
no `get_player_for_update()` call or any other re-fetch. A canonical
delete, deactivation, reactivation, or team-reassignment — every one of
which *does* row-lock the Player — landing between that snapshot and this
import's write is invisible to it: apply's `store.save_player(obj)` writes
the *whole* stale object back, so a field this import never touches (most
concretely `is_active`) silently reverts to its pre-concurrent-change
value. Concretely: Memory's save is an unconditional dict-replace, so a
deleted Player is resurrected under its original id with the import's new
field values; SQLite's `UPDATE` by id matches zero rows and raises nothing,
so the write is silently a no-op that still reports `committed`/`updated`
success; PostgreSQL, under genuine concurrent interleaving, loses the
concurrent write outright. Closed with a dedicated pre-lock pass inserted
immediately before `release_batch_player_jerseys` (which is itself the
first thing to read the stale snapshot): every Player id this batch's rows
resolved to in the top-level snapshot is row-locked, in deterministic
(id-sorted, not upload-row-order) order, via `get_player_for_update`; a
lock that returns `None` or a row whose `external_ref` no longer matches
the code it was locked for is dropped back to "not found" in the working
map rather than treated as live — routing it into the existing create
branch's own reserve-then-recheck (round 14 finding 3), which already
discovers and safely adopts a concurrently recreated row instead of
duplicating it, so nothing here needs to reimplement that. Every downstream
read — jersey availability, jersey release, field apply, contact sync,
counts, and audit — uses only the freshly locked result from that point on.
A second, defense-in-depth fix inside `upsert_imported_player` itself
performs the identical re-fetch under lock whenever it is called with a
non-`None` `existing`, mirroring `upsert_imported_team`'s own established
#201 pattern; redundant (a no-op re-lock of the same row) when called from
the now-fixed hierarchy path, but it keeps the helper safe to call on its
own rather than trusting every future caller to replicate the batch-level
discipline. Regression coverage spans forced two-connection PostgreSQL
races against canonical delete and deactivation (proving no resurrection
and no silently reverted state, respectively, while the import's own field
changes still land against the fresh row), plus a deterministic Memory/
SQLite parity pair using the same stale-snapshot-injection technique this
file's cross-importer races already established — patching `all_players()`
so the one call `commit_hierarchy_import`'s own snapshot makes still
returns the pre-mutation object, reproducing what an unlocked snapshot
taken before a concurrent commit would see without needing a second
connection. Both directions are falsifiability-verified against the full
two-layer fix (the pre-lock pass and `upsert_imported_player`'s own
re-fetch together), including a test-construction correction the
deactivation parity test's own falsifiability check caught: Memory's
`all_players()`/`get_player_for_update` return live object references, not
copies, so capturing the "stale" snapshot as a bare reference before
deactivating aliased the very mutation under test, keeping the test green
even with both fix layers reverted; fixed by snapshotting an independent
copy before mutating.

Round 15's own convergence review (#331 review round 16) confirmed both of
its findings fixed, then found three further reproductions still violating
the identical invariant round 15 was meant to close
(`Team.league_id == registration.LeagueSeason.league_id`, and a non-null
Division must belong to that same LeagueSeason) through code paths round 15
never touched — all three traced back to the same root cause the review
named directly: gate validation and apply each derived a row's Division and
target League independently, so the two could silently diverge.

The first: a brand-new Team's row was invisible to the per-existing-Team
gate loop entirely (`if existing is None: continue`), so it never
contributed to — or was constrained by — round 15's own same-upload
new-Division-League conflict tracking. Worse, apply's own per-row Division
resolution was a *live* `divisions_for_season()` re-query, so a Division an
**earlier** row in the same apply pass had just created (for a brand-new
Team, correctly falling back to the Season's ambient default League, since
a new Team has nothing else to prefer) became visible to a **later**
row for an *existing* Team naming the identical Division name — which then
silently reused it, leaving `Team.league_id` at the existing Team's real
permanent League while the registration landed under the ambient default.
Reversing the two rows' upload order avoided the defect, confirming it was
order-dependence, not a genuine conflict. Closed by moving the entire
cross-row Division-League resolution to a whole-batch pre-pass that runs
*before* any row is decided: every row in the upload — new Team or existing
alike — that names a not-yet-existing Division contributes its Team's own
permanent League (when it has one) to a `{name: {league_ids}}` map; more
than one distinct League named for the same Division is the existing
same-upload conflict, rejected before any write; exactly one is the
resolved target for every row naming that name, regardless of which row a
loop visits first or whether that row's own Team has a preference at all.

The second: the registration created inside `commit_teams_players_import`
apply is a `SeasonTeamRegistration` — its `active` and `division_id` fields
were the *only* signal the apply loop's own update-vs-no-op branch checked;
`league_season_id` itself was never compared. A registration already
active, with a division that already matches, silently skipped the update
branch entirely even when `league_season_id` had drifted to point at the
wrong League — reporting success while leaving the wrong League in place.
Distinct from the first finding: this is a row whose Team's own
`league_id` may already be perfectly correct, but whose separately-stored
registration disagrees with it regardless — a state no current write path
can produce going forward (this fix, and `register_team_for_season`'s own
Rule 7, both prevent it), but which a write path predating this invariant
(the pre-round-15 importer, or any other direct write) could leave behind.
Closed with the same lifecycle semantics `assign_season_team_league`
already established for exactly this class of change — never merely adding
`league_season_id` to the raw-save condition, which alone would silently
move a registration out of a League committed games still reference: a new
gate check compares each existing registration's *actual* current League
against the row's resolved target, and where they disagree, scans for a
non-cancelled Game still referencing the current (soon-to-be-wrong) League
for that Team in that Season. Any such Game rejects the whole batch with
zero mutation and the same `registration_league_change_strands_games`
reason `assign_season_team_league` itself raises; otherwise the repair
proceeds — the update branch's own condition now includes the
`league_season_id` comparison, safe specifically *because* the gate above
already proved it game-free.

The third: a Season can validly contain two Divisions sharing an identical
name under two different permanent Leagues — Division creation enforces no
name-uniqueness constraint at all. Gate's own snapshot was a
`{name: division}` dict comprehension — last-wins by store iteration order
— while apply's own lookup was a `next(...)` generator expression over a
live query — first-wins. Whichever one the store's iteration order happened
to put last vs. first could therefore diverge, letting apply commit a
registration under a League gate never actually validated. Closed with one
shared `_pick_division_candidate` helper both gate and apply call
identically: Divisions are now grouped by name (never collapsed), a lone
candidate is always unambiguous regardless of League, and two or more
requires the row's own Team's permanent League to resolve to *exactly* one
of them — no preference to disambiguate with, or none/more than one
candidate actually matching it, is genuinely ambiguous and rejected with a
new `import_division_name_ambiguous` reason before any write, never
silently guessed.

All three fixes converge on one general resolver, `_resolve_row_division_
and_league`, replacing round 15's narrower `_resolve_import_team_target_
league`: called identically by gate validation (to predict what apply would
do, without apply's own auto-create side effects) and by apply itself (to
actually do it) against the SAME frozen `_divisions_by_name` grouping and
whole-batch `_new_division_target_league` consensus map — the one property
the review's own required correction named explicitly: "gate and apply must
operate on the same frozen identities, independent of upload/insertion
order." A Division apply creates mid-pass is registered back into that same
frozen grouping immediately, so a later row in the identical apply pass
correctly reuses it (gate's own whole-batch pre-pass already proved every
row naming that name agrees on its League, so this reuse is provably safe,
not a reintroduction of the live-requery bug the first finding closes).

Regression coverage runs all eight new scenarios (the three reproductions,
plus their positive/negative counterparts — both upload orders for the
first, both Division-creation orders for the third, and the game-free-
repair/stranded-rejection/already-correct-no-op triple for the second)
through the SAME shared `ImportCommitServiceContract` every other
Memory/SQLite import-commit test in this file already uses — extended with
a third mix-in, `PostgresImportCommitTest`, running the identical contract
against a real PostgreSQL-backed store (none of the three reproductions is
a genuine concurrency race; all are deterministic within one commit call,
one connection, so a real third backend, not a forced two-connection race,
is what the review's own required matrix calls for here). Two of the eight
scenarios are additionally driven through the real HTTP commit route. All
three fixes are falsifiability-verified independently — each of the three
mechanisms (the whole-batch consensus map, the shared disambiguation
helper, and the registration-repair gate check) reverted in turn, with the
rest of the fix left in place, confirming each scenario's own regressions
fail for the exact documented reason and no other, before every revert is
restored. Desktop and 390px import smoke coverage gains a second scenario
(alongside round 15's) proving the ambiguous-Division-name rejection
surfaces in the real UI, never a false `Committed` state.

Round 16's own convergence review (#331 review round 17) found one further
blocker in the same registration lifecycle, adjacent to but distinct from
all three round 16 reproductions: both gate and apply picked `reg = next(r
for r in registrations_for_season(season_id) if r.team_id == team.id)` —
the FIRST registration the store happened to return for the Team anywhere
in the Season, regardless of which `LeagueSeason` it actually belonged to.
Migration 035's schema explicitly permits more than one row per (Team,
Season) — one per `LeagueSeason`, unique only on `(team_id,
league_season_id)` — and `transfer_team_to_league` relies on exactly that:
it deliberately leaves an inactive prior-League registration untouched as
history when transferring a Team whose active participation there has
already been unregistered. A `next(...)` lookup with no `LeagueSeason`
filter can't tell that untouched historical row apart from the one this
import row actually means to upsert. Two failure shapes followed: an
import re-run after such a transfer found the inactive row (the *only* row
that existed) and rewrote it in place — `active=True`, `league_season_id`
repointed at the new target — silently erasing the fact the Team was ever
registered in the old League at all, rather than preserving it and
creating a distinct new row for the new one; and, when a second,
genuinely-correct active registration already existed in the target
LeagueSeason (created directly via `register_team_for_season`, bypassing
the import), re-running the import could pick the *other*, unrelated
active row and rebind it onto the same unique key — a silently duplicated
active registration on Memory (two rows both claiming to be the Team's
canonical participation in that LeagueSeason), and a raw, unhandled
`{"error": {"code": "conflict", "details": {"reason":
"unique_violation"}}}` on SQLite/PostgreSQL instead of the structured,
zero-mutation `errors[]` shape every other rejection in this gate
produces.

Closed with a new shared resolver, `_resolve_import_row_registration`,
called identically by gate and apply (the same "never let the two derive
different answers" discipline round 16 established for Division/League
resolution, now extended to registration-row *selection* specifically). It
looks up the Team's registration by its exact `(team_id,
target_league_season_id)` identity via the store's existing
`league_season_for` / `registration_for_team_in_league_season` primitives
— never a bare Season-wide scan — so an inactive row in a *different*
`LeagueSeason` is structurally invisible to it and can never be touched.
When no row exists at the exact target but the Team holds precisely one
OTHER active registration elsewhere in the Season, that row is reused
in place via the identical "move" `transfer_team_to_league` and
`assign_season_team_league` already perform for a Rule-7-violating active
registration (rebinding its `league_season_id`, gated by the same
non-cancelled-Game stranding check on its *own* current League this file's
round 16 section above already describes) — preserving the round 16
`registration_league_change_strands_games` repair behavior for the common
single-stray-row case byte-for-byte, including its game-free-repair,
stranded-rejection, and already-correct-no-op triple (now additionally
proven safe against a planted CANCELLED game in the stale League that does
not block the repair, closing a gap the round 16 matrix itself didn't
cover). Any
other shape — the target row already exists AND a separate active row
also exists elsewhere, or more than one other active row exists with none
at the target — is a genuine, no-safe-default conflict: rejected before
any write with a new `team_registration_conflict` reason listing every
conflicting registration id, mirroring this codebase's consistent
preference (`team_transfer_strands_games`, `team_league_ambiguous`,
`import_division_name_ambiguous`, and round 16's own three reasons)
for a structured, operator-visible rejection over ever silently guessing
which of two active participations is authoritative. The existing
division-move stranding check (checked against ANY committed game in the
Season) is skipped specifically for the reused-elsewhere "move" case — its
old League's own division belongs to a different `LeagueSeason`'s division
pool entirely, so comparing it to the new target's is meaningless,
exactly as `transfer_team_to_league` itself unconditionally clears the
Division on a cross-League move rather than treating it as a change to
strand-check.

Regression coverage adds six new test methods to the same
`ImportCommitServiceContract` mix-in — each inherited by Memory, SQLite, and
PostgreSQL (18 executions), per round 16's own established pattern above —
plus one separate test driven through the real HTTP commit route (19
executions total): the canonical
transfer-from-inactive-only reproduction (a distinct new active row is
created, the inactive row is byte-for-byte untouched); the same
end-state as a true no-op in both physical insertion orders (the
Season's-first-created row inactive as the natural chronological result of
a transfer, and — planted the opposite way, reusing `register_team_for_
season`'s own reactivate-in-place semantics — the Season's-first-created
row still active with a chronologically LATER inactive sibling); the
missing cancelled-Game-doesn't-block case for round 16's own stale-registration
repair; and the two-simultaneously-active-registrations conflict rejection,
in both insertion orders, over the service layer and over real HTTP,
planted directly at the store level exactly as round 16's own stale-
registration fixture is (a Rule 7 violation no current service-layer write
path can produce going forward, reproducing legacy data or a state a write
path predating Rule 7 could have left behind). Falsifiability-verified by
reverting only the `setup_service.py` resolver change and re-running the
full new suite: the history-cannibalization and no-op-insertion-order
scenarios fail exactly as documented, the HTTP conflict test fails, and —
concretely confirming the review's own predicted failure shape — the
SQLite conflict scenario doesn't fail an assertion at all but raises the
exact raw `conflict`/`unique_violation` error shape described above,
proving the pre-fix code never reaches a structured rejection on that
backend. No UI change and no new e2e coverage was needed for the new
`team_registration_conflict` reason: the import error renderer
(`renderImportRows`/`renderImportResult`) is reason-agnostic — it renders
`sheet`/`row`/`field`/`message` generically for every entry in `errors[]`
— so the identical code path round 16's own desktop/390px coverage above
already exercises for `import_division_name_ambiguous` renders this reason
too, with no reason-specific branch to leave uncovered.

Out of scope for this round, found during investigation and intentionally
NOT fixed here: `upsert_imported_registration` (the analogous upsert
`commit_hierarchy_import`, a separate module, calls for its own
`registrations` sheet) contains the textually identical `next(r for r in
registrations_for_season(season_id) if r.team_id == team_id)` pattern.
Unlike the fix above, closing it properly would also require adding an
equivalent multi-active-registration conflict check to
`hierarchy_import.py`'s own `_preflight_reassignment_safety` gate — a
second module with its own sheet-row-indexed error contract and test
suite — which this review's report did not name and this round did not
attempt, to keep the shipped fix scoped to exactly what was reported and
independently verifiable.

Round 17's own convergence review (#331 review round 18) generalized the
identical defect class it closed for `commit_teams_players_import` to six
more places sharing the same underlying assumption: that a Team has at most
one registration row per Season, so a Season-wide scan (`next(r for r in
registrations_for_season(...) if r.team_id == team_id)`, or an equivalent
first-match pattern) safely identifies "the" row. Migration 035 never
enforced that — its uniqueness is `(team_id, league_season_id)` — and this
whole review cycle's own round 16/17 fixes exist precisely because
`transfer_team_to_league` deliberately leaves a Season's prior-League row
untouched as history. Every write or read path that re-derives "the"
registration from a bare team id, rather than resolving the Team's *current*
target `LeagueSeason` first, inherits the same two failure shapes: picking an
inactive historical row and cannibalizing it (destroying preserved history),
or picking an unrelated active row and colliding with it (a silent duplicate
on Memory, a raw `unique_violation` on SQLite/PostgreSQL).

The six sites and their fixes:

1. **Hierarchy import** (`hierarchy_import.py`'s `_preflight_reassignment_
   safety` check (b), and `commit_hierarchy_import`'s own
   `upsert_imported_registration` apply path) had the textually identical
   `next(...)` pattern round 17's own "out of scope" note named but
   deliberately deferred. Round 17's resolver, `_resolve_import_row_
   registration`, was a `SetupService` method — unusable from
   `hierarchy_import.py`'s module-level functions, which have no service
   instance. Lifted to a module-level, `store`-only function,
   `resolve_team_registration_for_import(store, season_id, team_id,
   target_league_id)`, with the identical exact-identity/conflict contract;
   `SetupService._resolve_import_row_registration` is now a one-line
   delegate to it, so `commit_teams_players_import`, `upsert_imported_
   registration`, Season rollover v1 (`roll_forward_registrations`), and
   the hierarchy preflight gate all resolve a row through the exact same
   callable — never two
   independently-written lookups that could quietly diverge on the identical
   input, the same discipline round 16 established for Division/League
   resolution and round 17 extended to row selection. The preflight's
   existing per-row stranded-Game guard (unconditional on `league_changed`/
   `division_changed`, already correct) composes with the resolver's
   corrected identity unchanged.

2. **Season rollover v1** (`roll_forward_registrations`) reused the same
   resolver for its per-team apply loop, but its skip/reactivate branching —
   `if existing is not None and existing.active: skipped += 1; continue` —
   was written before the resolver's three-way contract was fully reasoned
   through. The resolver's own "move" candidate (`other_active[0]` when
   `target_reg` is `None` and exactly one other active row exists) is active
   *by construction*, since `other_active` is filtered on `r.active`. Without
   also excluding the move case, every move candidate satisfied `existing is
   not None and existing.active` and was silently counted as "already
   correctly registered" — the move branch that would rewrite its
   `league_season_id` was unreachable dead code, and a Team stuck under the
   wrong League from a stale write path stayed there forever, silently
   reported as a successful skip. Fixed by adding `and not _is_move` to the
   skip condition, and — since nothing had ever gated the move itself against
   a committed Game — adding the identical guard `assign_season_team_league`
   and `commit_teams_players_import` already apply before an equivalent
   move: a non-cancelled Game still referencing the row's *current* League
   blocks the move with a new `registration_league_change_strands_games`
   rejection instead of silently stranding it.

3. **Season rollover v2** (`roll_forward_registrations_v2`) does not reuse
   the shared resolver at all — reusing its "move" semantics would have
   regressed v2's own deliberate, pre-existing "reject on any active-row
   mismatch, never silently move" contract (the code's own comment: a
   mismatch "would silently ignore the selection's required League/Division
   ... a contract violation"). Instead, the identity bug is fixed narrowly:
   the gate's existing-row lookup and its scan for any *other* active
   registration elsewhere in the target Season now both resolve by exact
   `(team_id, target_league_season_id)` identity via `registration_for_team_
   in_league_season`, never a Season-wide `next(...)`/unfiltered scan. v2's
   own conservative behavior is unchanged and unconditional either way: an
   inactive sibling under a different League never blocks (a distinct new
   active row is created, matching v1/hierarchy import), and any *other
   active* row under a different League is always rejected as `rollover_
   conflicts_active_registration` — regardless of whether a committed Game
   is even present to strand, since v2 never attempts the move that would
   strand one in the first place.

4. **`_transfer_team_to_league_inner`** and **`assign_season_team_league`**
   both blindly rebound a Team's active registration's `league_season_id`
   onto the target LeagueSeason with no existence check at the destination —
   silently duplicating an active row on Memory, or raising a raw `unique_
   violation` on SQLite/PostgreSQL, when the Team already retained a row
   there (inactive history from a prior assignment/transfer cycle, or —
   rarer — an independently active row left by legacy data). Both gained a
   direct `registration_for_team_in_league_season(target_ls.id, team_id)`
   check before any write: a retained *inactive* row is reactivated in place
   and the source row retired (mirroring `register_team_for_season`'s own
   reactivate-in-place semantics for the identical situation), while an
   independently *active* row at the target is an unresolvable conflict,
   rejected with a new `team_registration_conflict` before any write.
   `_transfer_team_to_league_inner` additionally pre-scans its full
   candidate batch (one candidate per Season the Team holds a mismatched
   active row in) for two candidates that would collide on the *same*
   target LeagueSeason — reachable only when the Team already held two
   concurrently active rows in two other Leagues for one Season, itself a
   pre-existing Rule 7 violation — before any candidate in the batch writes,
   preserving the function's documented zero-mutation-on-rejection contract.

   `assign_season_team_league`'s first attempt at this fix reused the shared
   import resolver (passing its own already-known `reg` as the team id's
   implicit "row to write"), on the reasoning that `reg` would always
   surface as the resolver's `is_move=True` reflection of itself. That holds
   only when nothing yet exists at the target — once a retained row *is*
   found there, `reg` (being active and elsewhere) unavoidably appears in
   the resolver's own `other_active` scan too, self-reported as a second
   conflicting registration, since the resolver has no way to know `reg` was
   the caller's own row rather than a genuine second candidate. Caught by
   the new regression suite below (a false `team_registration_conflict` on
   exactly the retained-inactive-row case this fix is supposed to handle)
   and corrected: unlike every other caller of the shared resolver — which
   must first *discover* which row a team id resolves to — `assign_season_
   team_league` already has the specific row pinned by its caller, so it
   never needed the resolver's discovery machinery at all. Replaced with the
   same direct, resolver-independent `registration_for_team_in_league_
   season` check `_transfer_team_to_league_inner` uses.

5. **`team_registration_valid`** (`league_scope.py`), the single shared
   resolver every live-scheduling consumer (`create_game`, standings, draft
   generation) reads through, had the same `next(r for r in registrations_
   for_season(...) if r.team_id == team_id)` pattern at its core — a Team's
   genuinely valid active registration under its current permanent League
   could be hidden behind an inactive or cross-League sibling that happened
   to sort first. Rewritten to resolve the Team's permanent-League
   `LeagueSeason` first (`league_season_for(team.league_id, season.id)`),
   then look up the registration by that exact identity
   (`registration_for_team_in_league_season`) — unambiguous by construction,
   since at most one row can ever match one exact LeagueSeason.

6. **`get_setup_progress`/`get_onboarding_status_v2`** (Home/Tasks hub
   readiness and the installation-wide onboarding readiness) counted *any*
   active registration whose `LeagueSeason` fell within the resolved
   Season as schedulable participation, without checking that Team against
   the same Rule 7 invariant `team_registration_valid` and `register_team_
   for_season` both enforce. `transfer_team_to_league` deliberately leaves a
   Season's active registration frozen at a Team's OLD League while `Team.
   league_id` moves on (history preservation for an ended Season, or the
   identical same-Program cross-League drift a stale pre-Rule-7 write path
   could leave in a current one) — exactly the row `create_game`/`move_game`
   /`publish` would all reject outright, yet both readiness endpoints
   reported it as "done"/`ready_to_schedule`, a false-positive completion
   signal an operator would only discover by hitting the real rejection
   later. Both now require `team.league_id == ls.league_id` alongside the
   existing Program-membership check before counting a registration.

Regression coverage: nine new tests across `test_import_teams_
registrations.py` (hierarchy import — inactive-sibling-creates-distinct-row,
true no-op, and the two-active-registrations conflict, mirroring round 17's
own `commit_teams_players_import` matrix on the identical shared resolver);
eight in a new `RolloverExactIdentityTest` in `test_season_rollover.py` (v1 —
inactive-sibling, true no-op in both physical insertion orders, the stale-
active-row move succeeding game-free and with only a cancelled Game present,
rejecting with zero mutation when a non-cancelled Game is present, and the
two-active-row conflict in both insertion orders); three new test methods
extending `RollForwardConflictSqlTest` in `test_v2_reassignment_integrity_
sql.py`, each run against both SQLite and PostgreSQL (v2 — inactive-sibling,
and the active-elsewhere conflict proven identical whether or not a
committed Game is present); four new tests in two new SQL-integrity classes
in the same file,
`TransferRetainedTargetSqlTest` and `AssignLeagueRetainedTargetSqlTest`
(retained-inactive-row reactivation and independently-active-row conflict,
zero mutation on rejection, for both `_transfer_team_to_league_inner` and
`assign_season_team_league`, on both SQLite and PostgreSQL); a new test in
`test_team_division_id_removed.py`'s Memory/SQLite/PostgreSQL contract
proving `team_registration_valid` resolves correctly (and `create_game`
schedules successfully) with an inactive cross-League sibling present, in
both insertion orders; and one new Memory test each in `test_setup_
progress.py` and `test_v2_onboarding_status.py` proving a stale wrong-League
active registration is excluded from "done"/`ready_to_schedule`, surfaces as
an actionable (not blocked) `next` step when a genuinely eligible Team
exists, and — deliberately — is left untouched and still reported even after
an operator adds the correct registration alongside it, since this endpoint
never silently drops a known-bad row just because unrelated data now makes
the installation schedulable.

Falsifiability-verified for the four backend behavior changes with the
sharpest failure shapes: reverting the `not _is_move` exclusion in v1
rollover's skip condition fails exactly the two stale-active-row move tests
(`rolled_forward: 0, skipped: 1` instead of the expected move); reverting
`get_setup_progress`'s and `get_onboarding_status_v2`'s added League-match
condition each fail their new test with the exact false-positive the finding
described (`participation: done` / `ready_to_schedule: true`) restored; and
the frontend fix (below) fails its new e2e assertions with the Team's home-
League row missing from the DOM entirely once its `regByTeamLeague` map is
collapsed back to a team-id-only key, confirming the map population and its
read site must both stay keyed by `(team_id, league_id)` together.

**Frontend**: `renderSeasonParticipation`'s `regByTeam` map was keyed by
`team_id` alone, `regRow(t, divId)` looked it up the same way regardless of
which League section it was rendering — so a Team holding two simultaneously
active registrations in one Season across two Leagues (the exact Rule 7
violation `team_registration_conflict` exists to catch before an import can
create one, but which pre-existing legacy data or a stale write path can
still leave behind) collapsed onto whichever row the map's last write
happened to keep: only one League section rendered a row for that Team at
all, and any Save/Remove control shown addressed that one winning
registration regardless of which section's controls were actually clicked.
Fixed by keying (and looking up) by `${team_id}::${league_id}` instead, so
each League section's row is bound to its own section's own registration.
Proven with new desktop and 390×844 e2e coverage in `season-participation.js`:
a second active registration is planted directly into the running journey's
own durable SQLite file (the same direct-injection technique the file's
existing repair-surface coverage already uses for `registration_league_not_
in_season`), both League sections' rows are confirmed present as genuinely
distinct DOM elements naming the same Team, the non-home League's row is
removed via a Tab-focus-then-Enter keyboard activation (not just a click),
and the home League's row — same Team, different registration id — is
confirmed both still present in the DOM and still active in the store,
proving Remove on one row never reaches the other.

The same finding separately named the import error renderer itself:
`renderImportRows` showed only `sheet`/`row`/`field`/`message` for every
`errors[]`/`warnings[]` entry, never the `affected_registration_ids` (or
`affected_game_ids`) several structured reasons carry — including `team_
registration_conflict` — so an operator seeing "Team HOME already has more
than one active registration" had no way to identify exactly which two rows
were conflicting before navigating to Season participation to resolve one
via its now-fixed per-League controls above. Fixed by rendering a reason-
agnostic "Affected: registration(s) …/game(s) …" line whenever either id
list is present and non-empty, with no reason-specific branch to leave
uncovered later, matching this renderer's existing reason-agnostic
philosophy. Verified directly in a real browser (calling the live
`renderImportRows` with representative fixture rows, one of each id-bearing
shape plus a plain row with neither, confirming the plain row renders no
spurious "Affected:" line) and captured as new permanent desktop/390×844
coverage in `hierarchy-import.js`.

Not independently confirmed and left for a future round if the reviewer
still considers it in scope: `renderRollover`'s own team-id-derived
`data-rollover-pick` selection ids, flagged during this round's frontend
investigation as *structurally similar* to the fixed `regByTeam` pattern, but
rollover's UI only ever offers *source*-Season teams for selection (never
renders a control bound to a specific *target*-Season registration id the
way Season participation's Save/Remove do), so whether the identical
two-active-registrations shape can actually reach it was not established.

Round 18's own convergence review (#331 review round 19) confirmed the
identical defect class in four more places — including a direct answer to
round 18's own open question above — plus one genuinely new failure shape:
`InMemoryStore` enforces none of migration 035's `(team_id,
league_season_id)` uniqueness at all, so even the *exact-identity* lookups
round 17/18 introduced specifically to replace ambiguous Season-wide scans
(`registration_for_team_in_league_season`) were themselves trusting a bare
first match whenever more than one row happened to share one identical key.

1. **Exact-key multiplicity.** SQL's `ux_team_league_season` index (migration
   035) makes a second row at one exact `(team_id, league_season_id)` key
   impossible to `INSERT`, so `registration_for_team_in_league_season` can
   only ever return 0 or 1 rows on SQLite/PostgreSQL. `InMemoryStore` has no
   equivalent constraint on `add`/`save`, so every one of that primitive's
   seven direct callers — `assign_season_team_league`, `register_team_for_
   season`, `_transfer_team_to_league_inner`, both call sites in `roll_
   forward_registrations_v2` (gate and apply), `team_registration_valid`,
   and `context_scope._team_season_ids` — silently returned whichever
   matching row happened to sort first, hiding a second one entirely, if
   legacy/corrupted data (or a write path this whole review cycle predates)
   ever left two rows at the identical key. Closed with a new shared
   primitive, `league_scope.exact_registration_or_conflict(store,
   league_season_id, team_id)`, returning `(reg_or_None, conflicting_ids)`:
   0 rows → `(None, [])`; exactly 1 → `(that row, [])`; 2+ → `(None, [every
   row's id])` — an *unconditional* conflict regardless of any row's
   `active` flag, since the rows' mere co-existence at one exact key is
   itself the corrupted state, never a fact about which one is "really"
   current that would be safe to guess. Lives in `league_scope.py` (not
   `setup_service.py`) specifically so both `context_scope.py` — which must
   stay independent of the much larger `setup_service` module — and
   `setup_service.py` itself, which already imports from `league_scope`,
   can share it without a circular import.

   All seven call sites now route through it, split by contract: the five
   WRITE sites (`assign_season_team_league`, `register_team_for_season`,
   `_transfer_team_to_league_inner`, and `roll_forward_registrations_v2`'s
   gate and apply) raise the existing `team_registration_conflict`
   structured error with every conflicting row's id, zero mutation, exactly
   like every other conflict shape these functions already reject. The two
   READ-ONLY sites — `team_registration_valid` (the live-scheduling
   resolver every `create_game`/standings/draft-generation call reads
   through) and `context_scope._team_season_ids` (which Seasons a scoped
   Coach/Player/Guardian may see) — have no caller to report a structured
   conflict to, so both fail CLOSED instead: an ambiguous key is treated
   identically to "no valid registration here," deterministically, never
   varying by which corrupted row happens to sort first. This directly
   answers a reproduction from this same round: a Coach's context-visible
   Seasons flipping between "denied" and "granted" depending purely on
   which of two colliding rows was inserted first — fail-closed makes the
   answer always "denied" while the corruption exists, regardless of
   insertion order, the same "answer must not depend on which row sorts
   first" discipline `team_registration_valid` itself already established
   in round 18.

   `assign_season_team_league` additionally still carried a narrower,
   pre-existing bug independent of exact-key multiplicity: its round-18 fix
   guarded the whole retained-target check on `if reg.active`, reasoning an
   inactive `reg` had no Rule 7 participation to protect — true, but
   irrelevant, since the `(team_id, league_season_id)` uniqueness a blind
   rebind can violate is unconditional, not "only among active rows." A
   public lifecycle ending in this call on an inactive historical row could
   still collide with a retained target (silently duplicating it on
   Memory, raising a raw `unique_violation` on SQLite). Fixed by moving the
   target-existence check outside the `active` gate entirely: only "`reg`
   active, target inactive" is a safe supersede (reactivate the target,
   retire `reg`, matching the existing branch); every other combination —
   active/active, or `reg` inactive with any row already at the target — is
   an unresolvable conflict.

2. **Single-active-registration enforcement gaps.** `get_setup_progress`'s
   participation loop already excluded a registration whose League didn't
   match the Team's current permanent League (round 18's own fix), but
   silently `continue`d past it with no signal at all — a Team with one
   genuinely valid registration elsewhere reported "done" with no way for
   an operator to discover the excluded row still needs cleanup, unlike its
   installation-wide sibling `get_onboarding_status_v2`, which has always
   tracked this shape as its own `invalid_registrations` blocker. Fixed by
   counting excluded rows separately (`needs_attention`, never merged into
   `schedulable` and never changing `status`) and surfacing them as a new,
   additive `attention` field on the workflow entry — `{"reason":
   "invalid_registrations", "count": N, "detail": "..."}` , omitted
   entirely when nothing needs attention, the same "absent means
   irrelevant" contract `next_blocked` already follows — so "done" and
   "needs attention" can both be true at once without either hiding the
   other.

   Separately, `get_demo_overview`'s `_registration_is_operational(r)`
   answered "does this Team have *some* valid registration" (delegating
   entirely to `team_registration_valid`, resolved via the Team's own
   permanent League) rather than "is *this row* (`r`) the valid one" — so
   for a Team with a genuinely valid registration under its permanent
   League plus a stray active row under a different League, calling this
   with the *stray* row still resolved the *other*, valid row and returned
   non-`None`. Both rows were reported operational in
   `get_demo_overview()["registrations"]` — the exact list the scheduling
   wizard reads — each claiming a different `league_id` for the identical
   `team_id`, with no way for a caller to tell which one was actually safe
   to act on. Fixed by comparing the resolved row's own id back to `r.id`:
   operational now means this specific row, never merely "the Team has one
   somewhere." A third angle from the same finding — a live write path that
   could still schedule a game against the stray cross-League row directly
   — was investigated and not confirmed: `team_registration_valid` can
   never resolve to a Team's non-permanent-League row in the first place
   (it only ever looks up via `team.league_id`), and `create_game`'s
   separate Rules 8/9 League-match check rejects any attempt scoped to a
   League other than whichever row *was* resolved, before any write.

3. **`hierarchyResultHtml` never rendered affected ids.** Round 18's
   `renderImportRows` fix (§ above) added `affected_registration_ids`/
   `affected_game_ids` rendering, but only to `app.js`'s own renderer —
   `hierarchy-import.js` (the real hierarchy Setup panel's script, distinct
   from the identically-named `e2e/hierarchy-import.js` test file, which is
   what actually made this easy to conflate) defines a **completely
   separate**, independently-implemented `hierarchyResultHtml()` that the
   real "Validate hierarchy"/"Commit hierarchy" buttons render through,
   never `renderImportRows`. It rendered only `sheet`/`row`/`message` for
   errors (`field` and both id lists dropped; warnings dropped `sheet`/
   `row`/`field` too), so a `team_registration_conflict` surfaced through
   the actual hierarchy panel — as opposed to the *other* teams/players
   import panel `renderImportRows` already covers — gave an operator no way
   to identify which rows to resolve. Fixed by having `hierarchyResultHtml`
   call `renderImportRows` directly for both its errors and warnings lists,
   rather than re-fixing a second, independent copy — the two panels' error
   shape now stays identical by construction, not by convention that could
   drift again. The round-18 e2e coverage of this exact concern had itself
   only ever called `renderImportRows` directly via `page.evaluate`,
   self-documented in its own comment as a stand-in for wiring a real
   conflict through validate/commit — it never touched `hierarchyResultHtml`
   or the real buttons at all. Replaced with a genuine reproduction: a
   second active registration is planted directly into the running
   journey's own durable SQLite file (this test file's server previously
   ran in-memory; switched to a durable file for exactly this), driving the
   real "Validate hierarchy" (which passes — the conflict check runs only at
   commit time, inside `commit_hierarchy_import`'s own `_preflight_
   reassignment_safety`, not during `validate_hierarchy_import`'s dry run;
   noted for a future round as a gate/apply asymmetry, not fixed here since
   the reviewer's finding was about rendering, not this timing gap) and then
   "Commit hierarchy" buttons, asserting both conflicting registration ids
   appear as `<code>` elements in the real rendered DOM and that the
   rejected commit leaves every record count unchanged.

4. **Registration identity lost in the hierarchy tree and Season
   rollover.** Two related gaps, one backend field and one frontend keying
   pattern, both closing round 18's own open question about `renderRollover`
   above.

   `get_setup_hierarchy_v2`'s `team_node(t)` helper carried only the
   Team's own fields — `id` was always `t.id`, the *Team's* id, never a
   registration id — across all of its Season-participation call sites
   (division-nested teams, and `teams_without_division`). A Team with two
   active registrations in one Season (a Rule 7 violation, or — Memory only
   — the exact-key duplicate finding 1 above closes off at every write path
   going forward) produced two structurally-identical nodes a consumer
   could only re-associate with a lossy `(team_id, league_id)`
   reconstruction — precisely what `renderSeasonParticipation`'s own
   round-18-fixed `regByTeamLeague` map has to do today, and precisely
   where that map's own remaining gap lived: two rows sharing the exact
   same `(team_id, league_id)` target (finding 1's own corruption, not
   round 18's different-League case) still collapse onto one map entry, so
   the tree's now-duplicated `team_node` occurrences both rendered bound to
   the SAME winning registration's Save/Remove controls — two visually
   distinct rows, but one of the two registrations entirely unreachable,
   `regByTeamLeague`'s original team-id-only failure mode one level up.
   Fixed on both ends: `team_node` gained an optional `registration_id`
   parameter, `None` for the permanent Program→League→Team tree (no
   Season/registration involved there) and the specific row's id everywhere
   Season participation is represented — `get_setup_hierarchy_v2` already
   emits one tree entry per registration in the duplicate case
   (`teams_by_div`/`teams_direct_by_league` append one `(team, reg)` pair
   per registration), so a Team with two rows at one target was already
   appearing twice; and `renderSeasonParticipation`'s `regRow` now resolves
   each row's registration via `t.registration_id` directly (through a
   `regsById` map keyed by registration id, replacing `regByTeamLeague`)
   instead of re-deriving one from `(team_id, league_id)` — trusting the id
   each tree entry already carries, rather than reconstructing one, is what
   makes both rows independently addressable.

   `renderRollover`'s answer to round 18's open question turned out to be:
   yes, the identical shape reaches it, just via *source*-Season rows
   (round 18's own note assumed only a *target*-bound control would be at
   risk). `eligible` held bare `team` objects, and every per-row control —
   `data-rollover-pick`, `data-rollover-league`, `data-rollover-div` — was
   keyed by `team.id`; a Team with two active source registrations rendered
   two rows sharing every one of those attribute values. Both the commit
   handler and `updateRolloverCommitState` then resolved a checked row's
   League/Division via a *global*, value-matched `c.querySelector(...)`
   rather than scoping to that row (`cb.closest(".reg-row")`) — always
   returning the *first* matching element in document order regardless of
   which row's checkbox actually triggered the read. Checking the second
   row and picking a Division for it would silently read back the first
   row's untouched "No division" default, persisting the wrong Division (or
   none at all) for whichever row the operator actually meant to submit.
   Fixed on three fronts: `eligible` now carries `{reg, team}` pairs, so
   every per-row attribute keys off `reg.id` (never `team.id`) with
   `data-rollover-team` added alongside the checkbox so the commit handler
   can still report which team a picked row belongs to; every DOM read that
   used to be a global value-matched query — the commit handler, `
   updateRolloverCommitState`, and the League-select's own Division-cascade
   handler — now scopes through `cb.closest(".reg-row")` / `sel.closest(
   ".reg-row")` first; and the outgoing selection payload now carries an
   explicit `registration_id` alongside `team_id`.

   `roll_forward_registrations_v2` was updated to use it: when a
   selection's `registration_id` is present (the frontend now always sends
   one; the field is optional for back-compat with any other caller), it
   must resolve to an *active* registration for *exactly* that team in the
   *source* Season — cross-checked, not merely trusted — rejecting with a
   new `rollover_registration_mismatch` before any write if it names a
   different team, an inactive row, or a row from the wrong Season.
   Independent of that field's presence, two selections naming the *same*
   `team_id` in one batch are now rejected outright with a new `rollover_
   duplicate_team_selection` — the prior `wanted[tid] = (lid, div_id)`
   aggregation would otherwise let the later selection silently overwrite
   the earlier one before any write happened, exactly the shape two
   now-distinguishable rows for one Team could produce.

Regression coverage: a new `test_exact_registration_identity.py` covering
the shared primitive directly (0/1/2+ rows, active/inactive combinations
all still conflicting, insertion-order independence) plus both fail-closed
READ paths and all five WRITE paths, each with a zero-mutation assertion on
`InMemoryStore` (the only backend where this corruption is constructible);
three new Memory tests in `test_setup_progress.py` (the `attention` field
appearing on an otherwise-`todo` participation step, remaining present once
a genuinely valid registration makes it `done`, and staying entirely absent
when nothing needs it) and one new Memory/SQLite test in `test_team_
division_id_removed.py` (`get_demo_overview` exposing exactly one
registration — the Team's own permanent-League row — for a Team with a
stray cross-League sibling, never both); two new `test_setup_hierarchy.py`
tests for `team_node`'s `registration_id` (`None` on the permanent tree,
correct for a real registration, and distinct across two active
registrations at one target); new browser coverage in `hierarchy-import.js`
(the real conflict reproduction described above, both desktop and 390×844)
and `season-rollover.js` (two active source registrations for one Team
rendering as genuinely distinct DOM elements, the *second* row checked and
given an explicit Division via real interaction — the League cascade's own
Division-select — then committed via a real keyboard Enter activation on the
focused Commit button rather than a click, proving the outgoing
`registration_id`/`division_id` match the row actually operated on and the
Team's untouched first row is byte-for-byte unaffected); new SQL-backend
tests extending `RollForwardConflictSqlTest` in `test_v2_reassignment_
integrity_sql.py` for the `registration_id` cross-check (matching row
accepted, wrong-team row rejected, inactive row rejected) and the
duplicate-team-selection rejection, each with the standard zero-record/
zero-audit-mutation assertion; and a new dedicated browser journey,
`hierarchy-duplicate-registration.js`, for the `regByTeamLeague`/`regsById`
fix specifically. That corruption is unconstructible on any SQL backend (the
same `ux_team_league_season` index finding 1 relies on), so this file runs
the demo server in-memory via a small Python launcher that starts
`hockey_scheduler.web.server`'s own unmodified `serve()` in a background
thread and injects the second registration directly into that live store on
demand, rather than the durable-SQLite-file-plus-separate-process technique
the other files above use. Two independent fixtures each build one real
registration through genuine HTTP writes, inject a second active one at the
identical target, confirm the Setup hierarchy renders both as distinct,
independently-attributed rows, then keyboard-remove one via a real `Tab`
walk from the row's own League select to its Remove button (bounded-loop
Tab presses, never a JS `.focus()`) followed by a real `Enter` — one fixture
removing the first-created row, the other removing the second-created
(injected) row, so both orders are proven, each asserting the untouched row
survives active and unchanged, at desktop and 390×844.

Falsifiability-verified: the shared primitive's "unconditional regardless of
active state" design decision (temporarily narrowed to active-only rows,
confirmed five tests across three different call sites fail — the primitive
itself, `team_registration_valid`, and `assign_season_team_league` — each
for the documented reason); `get_demo_overview`'s row-identity comparison
(reverted to the bare `is None` check, confirmed both the valid and stray
rows are reported operational again, on both Memory and SQLite);
`get_setup_progress`'s `needs_attention` counter (removed, confirmed the new
test fails with a `KeyError` on the now-absent `attention` field); the
`roll_forward_registrations_v2` `registration_id` cross-check and duplicate-
selection guard (each removed in turn, confirmed the corresponding new SQL
test fails with no `error` key at all — the request that should be rejected
silently succeeds); the frontend rollover keying fix (reverted `data-
rollover-pick`/`-league`/`-div` back to `team.id`, confirmed the new e2e
section times out locating the second row by its registration id — it no
longer exists as a distinguishable element at all); and the `regByTeamLeague`
→ `regsById` hierarchy-tree fix (reverted `regRow` to its prior `(team_id,
league_id)`-derived lookup, confirmed `hierarchy-duplicate-registration.js`
fails with the exact shape the bug produces — one registration id matching
zero DOM elements, the other matching two — restored, reconfirmed both
viewports green).

Round 19's own convergence review (#331 review round 20) confirmed both
remaining halves of finding 4 and surfaced one much larger new finding.

**Finding 4, completed.** Two gaps in the round-19 fix: (a) the accessible
name on a duplicate row's Save/Remove controls was built purely from
`t.name`/`s.name`, so two rows for the identical team under the identical
League — the exact shape the fix exists for — announced identically to a
screen reader despite being independently addressable underneath. Fixed by
a `dupKeyCounts` map gating a `(registration ${id})` suffix onto both the
visible label and the Save/Remove aria-labels, only when a genuine
duplicate exists — the ordinary single-row case is byte-for-byte unchanged.
(b) The reviewer's own round-20 reproduction of this exact bug used two
rows in *different* Leagues, where `(team, league)` is unique by
construction — unable to falsify the true failure mode — and used a
JS-forced `.focus()` rather than proving Tab reachability. Replaced with a
dedicated new browser journey, `hierarchy-duplicate-registration.js`: since
this corruption (two ACTIVE rows at the identical exact key) is
unconstructible on any SQL backend — the same `ux_team_league_season`
unique index finding 1 relies on — the demo server runs IN-MEMORY here via
a small Python launcher that starts `hockey_scheduler.web.server`'s own
unmodified `serve()` in a background thread and injects the second
registration directly into that live store on demand. Two independent
fixtures each prove a same-target duplicate is reachable and removable via
genuine `Tab`/`Shift+Tab` walks (a bounded loop of real key presses, never
`.focus()`) and both native button-activation keys (`Enter` in one order,
`Space` in the other), in both selection orders, asserting the untouched
row survives active and unchanged and the two rows' accessible names are
distinct and self-identifying.

**Finding 1 — a unified Team/Season participation resolver.** The required
invariant: an exact `(team_id, league_season_id)` key is never ambiguous
(round 19), *and* live participation exists only when the Team has
**exactly one** active registration in the Season, matching the expected
LeagueSeason. Four parallel research passes over the full call graph
confirmed every one of the reviewer's reproductions and one they hadn't
named:

1. `register_team_for_season` only ever checked the exact TARGET key via
   `exact_registration_or_conflict` — nothing anywhere in the method asked
   "does this Team already have an active row at a DIFFERENT LeagueSeason
   this Season." Registering a Team into its own permanent League while an
   active stray already existed elsewhere (legacy data, or a write path
   predating full Rule 7 enforcement) committed a brand-new row and left
   both active.
2. `team_registration_valid` resolves exclusively via the Team's own
   `league_id` → LeagueSeason → exact row; it has no branch that inspects
   any *other* LeagueSeason, so it is structurally blind to a same-Season,
   different-League active sibling. Its four callers — `create_game`,
   `_revalidate_game_participation` (`move_game`/`publish_game`),
   `_require_team_registered`, `_registration_is_operational`/
   `get_demo_overview` — inherited the same blind spot, letting `create_game`
   commit a regular Game, and `move_game`/`publish_game` revalidate and
   pass, against a Team whose participation was genuinely ambiguous.
3. `_require_batch_team_participation` (the draft-commit gate) built
   `active = {r.team_id: r for r in registrations_for_league_season(...) if
   r.active}` — for the round-19 exact-key corruption (2+ ACTIVE rows at
   one key), this dict comprehension silently keeps whichever row wins the
   key collision and **accepts the batch on it** — an actual
   accept-when-should-reject bug, not merely a blind spot, and a
   measurably weaker guarantee than the `exact_registration_or_conflict`
   pattern every sibling write path already uses for the identical class of
   corruption.
4. `_require_team_registered`'s fallback error-reason lookup picked
   whichever registration row a bare `next(...)` scan (all rows, active or
   not) happened to find first, so its reported `reason` — `"team_not_
   registered"` vs `"team_wrong_division"` — flipped depending on
   insertion order for the identical underlying corrupted state.
5. `registered_team_ids_in_division`/`registered_teams_by_division_in_league`
   (draft *generation*, read-only) and the independent, fifth raw-loop
   implementation inside `_standings_for_league_season` were confirmed to
   have the identical blind spot in isolation, but NOT to misattribute or
   double-count in practice: both build a plain `set` of team ids, and a
   `set` cannot hold a duplicate regardless of how many underlying rows
   feed it, and standings independently re-derives trust from `team.
   league_id` per row rather than trusting "a resolved registration
   exists." Any unsafe proposal these two would admit is independently
   caught at commit time by fix 3 above. Left unchanged, with this
   rationale recorded rather than silently addressed — the same "flag,
   don't invent a fix for a non-demonstrated problem" discipline earlier
   rounds have followed.
6. `context_scope._team_season_ids`, `get_demo_overview`, and
   `get_onboarding_status_v2` were investigated and confirmed to already
   behave correctly: Context access is granted via the Team's genuinely
   valid row in its OWN permanent League regardless of an unrelated stray
   elsewhere (correct — the scoped user's own participation is
   unambiguous; a data-hygiene issue elsewhere is an admin concern, not a
   reason to hide a Season the Team really is in), `get_demo_overview`
   already inherits fix 2 for free (it delegates to `team_registration_
   valid`), and `get_onboarding_status_v2`'s `invalid_registrations`
   blocker already independently trips on the stray, unaffected by whether
   a valid row also exists. `get_setup_progress`'s "done" + `attention`
   coexistence (round 19) is a deliberate, separately-tested UX contract —
   a per-user "what's next" nudge, not the installation-wide hard gate —
   and was left as-is.

Closed with one new shared primitive, `league_scope.team_season_
participation(store, season_id, team_id)`: every ACTIVE registration for
this Team across the whole Season, any League. Returns `(reg_or_None,
conflicting_ids)` — 0 active rows → `(None, [])`; exactly 1 → `(that row,
[])`; 2+ anywhere in the Season → `(None, [every active row's id])`. This
is a *complement* to `exact_registration_or_conflict`, never a
replacement: the new primitive is ACTIVE-only and — by design — blind to a
same-key active+inactive pair, which is still `exact_registration_or_
conflict`'s job, unconditional on active state. `team_registration_valid`
now layers both checks (the exact-key check first, then the season-wide
check against the *same* row it resolved), so every one of its four
callers inherited the fix automatically. `register_team_for_season` gained
an equivalent season-wide guard ahead of its existing exact-key check,
rejecting with the established `team_registration_conflict` reason before
any write. `_require_batch_team_participation` was rewritten to resolve
each team through both primitives explicitly (replacing the unsafe dict)
so it independently fails closed on either shape. `_require_team_
registered` now checks for a structured conflict (either shape) before its
fallback raw pick, so the reported reason is deterministic regardless of
insertion order.

One consequence of the tightened invariant: an operator can no longer
register a Team into its correct League while an active stray exists
elsewhere in the same Season — the two round-19 tests whose fixtures relied
on that succeeding (`test_stale_wrong_league_active_registration_never_
counts_as_done`, `test_same_program_cross_league_registration_is_invalid_
not_schedulable`) were updated to first assert the new rejection, explicitly
remove the stray via `unregister_team_from_season` (the same action Season
participation's own "Remove" button already performs), and only then
register — matching the real remediation workflow round 20 now requires.

Regression coverage: `test_exact_registration_identity.py` gained five new
test classes covering the primitive directly (0/1/2+ active rows, any
number of different Leagues, insertion-order independence, an inactive
sibling never counting), `team_registration_valid`'s layered season-wide
fail-closed behavior plus a live `create_game` end-to-end rejection,
`register_team_for_season`'s season-wide rejection (single and multiple
strays), `_require_batch_team_participation` tested directly against both
failure shapes (bypassing the public API's own independent
`draft_fingerprint`-staleness net, which would otherwise mask which check
is actually rejecting — a duplicate row for an already-rostered team
doesn't change the `set` of registered team ids draft generation computes,
so this corruption is invisible to that generic net and
`_require_batch_team_participation`'s own check is confirmed the only line
of defense), and `_require_team_registered`'s deterministic reason
(exact-key both insertion orders, season-wide). A new `test_v2_
reassignment_integrity_sql.py` class proves the season-wide corruption —
unlike the exact-key one — is genuinely constructible on real SQL (no
unique index spans two different keys) and correctly rejected on both
SQLite and PostgreSQL, for both `register_team_for_season` and
`create_game`, zero mutation. A new HTTP-level test in `test_season_
registration_http.py` proves the same rejection over the real `/api/v2/
setup/seasons/{id}/team-registrations` boundary the Season participation
UI actually posts to.

Falsifiability-verified: reverting the finding-4 accessible-label
distinguisher (confirmed `hierarchy-duplicate-registration.js` fails with
identical aria-labels on both rows, restored); reverting all of finding 1's
backend changes together (confirmed every new test in `test_exact_
registration_identity.py` fails to even import, since `team_season_
participation` no longer exists; confirmed the new SQL-backend and HTTP
tests reproduce the reviewer's exact claims live — `create_game` genuinely
commits a Game against an ambiguous Team on real SQLite, and `register_
team_for_season` genuinely returns 200 instead of rejecting — restored,
reconfirmed all green).

**Verification:** full Memory/SQLite/PostgreSQL suite green (3495 tests, +23
from this round). `season-participation.js`, `season-rollover.js`,
`hierarchy-duplicate-registration.js`, `team-division-participation.js`, and
`registration-cleanup.js` all re-confirmed green at desktop and 390×844
after the backend changes, zero console errors.

### Round 21: divisionless move/publish bypass and setup-progress false completion

Round 20's convergence review (#331 review round 21) confirmed the season-wide
resolver's two write-side fixes but found two more places still bypassing it
entirely.

**Finding 1 — `_revalidate_game_participation`'s divisionless branch.**
`move_game`/`publish_game` share one revalidation gate before any write; the
divisioned branch already routed through `_require_team_registered` (and so
already inherited round 20's fix), but the branch behind a DIVISION-LESS
regular Game fell back to a raw `{r.team_id for r in registrations_for_league_
season(ls_id) if r.active}` set — "is this Team active SOMEWHERE in this
LeagueSeason." That stayed true with an active stray registration elsewhere
in the same Season (the same shape create_game/register_team_for_season
already reject), and even with an exact active+inactive duplicate at the
Game's own LeagueSeason (`exact_registration_or_conflict`'s own unconditional
job, invisible to a plain set built from only the `.active` rows). Fixed by
routing this branch through `_require_team_registered` too — inheriting both
conflict checks for free — followed by an explicit `registration_wrong_league`
re-check (mirroring `create_game`'s own Rules 8/9 check for the identical
gap): `_require_team_registered` resolves via the Team's own permanent
League, not necessarily this specific Game's `league_season_id`, so a Team
whose permanent League has since diverged from the Game's own recorded
LeagueSeason needs that separate, explicit comparison.

**Finding 2 — `get_setup_progress` reported an unschedulable Team as fully
done.** The participation step counted a row `schedulable` whenever its
LeagueSeason matched the Team's own permanent League in isolation — exactly
the round-18 check finding 1 above widened everywhere else, but never applied
here. A Team holding a genuinely valid row at its own League AND an active
stray elsewhere in the Season (finding 1's own reproduction) had its valid
row counted as `schedulable` regardless, so participation read `"done"`,
overall `complete` could read `true`, and the Home/Tasks hub's success card
told the operator every workflow was finished while the shared resolver would
reject every Game that Team plays in. Fixed by resolving each candidate row's
Team through `team_registration_valid` (memoized once per Team, not
recomputed per row) instead of comparing its League in isolation: a row only
counts as `schedulable` when it IS that Team's one live resolved registration
this Season; every other row — the Team's own stray included — now counts
toward `attention`, which also gained an `affected_registration_ids` list
(the established shape every other conflict response already carries)
alongside its existing count and guidance text.

That fix alone made `complete`/`status` correct, but the Home/Tasks hub's own
rendering had never read a workflow's `attention` field AT ALL, in any of its
three states — not just the success card the reviewer named, but the
"Continue setup" and blocked-workflow states too, which list every workflow's
`detail` but never its separate `attention`. Round 19 deliberately designed
`attention` to coexist with a workflow reading `"done"` (a DIFFERENT Team's
unrelated stray, say), so the fix could not simply rely on `complete` going
`false` — that only happens when the SAME Team's own row is what's
ambiguous. Both the primary card (the success state's own new attention rows,
and "Continue setup"'s own named workflow) and the secondary workflow list
below it now render a workflow's `attention.detail` as a distinct, visibly
separate line, reusing existing markup this same card already had elsewhere
(the blocked-state's amber `na-row`, and the draft scheduler's `.li-sub.
conflict` convention) rather than introducing new, unreviewed color
combinations into a card round 3's own review already had to fix real
contrast failures in once.

**Regression coverage:** a new `GameParticipationRevalidationTest` class in
`test_exact_registration_identity.py` covers the divisionless branch directly
— a clean baseline (both `move_game` and `publish_game` succeed with no
corruption), the season-wide active-stray rejection for the home Team AND
independently for the away Team, the exact active+inactive duplicate at the
Game's own LeagueSeason, and that a purely historical inactive stray never
blocks either write — every rejection asserted zero-mutation (the Game's
slot/publish state unchanged). `test_setup_progress.py` gained two new tests:
the exact reproduction (valid registration + active stray, every other
workflow complete, asserting `status`/`complete`/`next`/`attention.count`/
`affected_registration_ids` all correct) and the required boundary case from
the review's own wording — one active row plus an inactive historical
sibling at a different LeagueSeason must still read fully complete with no
attention at all. The pre-existing `test_stale_wrong_league_active_
registration_never_counts_as_done` assertion on the exact `attention` shape
was extended for the new `affected_registration_ids` field. A new
`setup-progress-conflict-attention.js` browser journey reuses the in-memory-
server-with-live-injection technique `hierarchy-duplicate-registration.js`
established (the season-wide shape, unlike the exact-key one, spans two
different LeagueSeasons and so needs no special corruption technique of its
own — it is genuinely constructible on a real database — but the technique
is convenient here regardless, letting one page session build the fixture,
inject, and re-observe the same live server): it builds every workflow to
completion via genuine HTTP writes, confirms the success card renders first,
injects the active stray, and confirms the card switches to "Continue setup"
with the conflict visibly named in both the primary card and the workflow
list below it, on desktop and 390px.

**Falsifiability:** reverting the `_revalidate_game_participation` fix alone
reproduces all three claimed shapes live on Memory (`move_game`/`publish_game`
both succeed against an active stray at a different League, and `move_game`
succeeds despite an exact active+inactive duplicate) — restored, reconfirmed
green. Reverting the `get_setup_progress` backend fix reproduces the exact
false-`"done"`/`complete: true` state the review named — restored. Reverting
the frontend attention-rendering addition alone (backend fix left in place)
makes `setup-progress-conflict-attention.js` fail with an empty
`attentionRows` array — proving the browser journey genuinely exercises the
rendering fix, not just the backend's already-correct `complete: false` —
restored, reconfirmed green both viewports.

**Verification:** full Memory/SQLite (3504 tests) and PostgreSQL (3504 tests,
1009.8s) suites both green, +9 from round 20. `hierarchy-duplicate-
registration.js`, `season-participation.js`, `season-rollover.js`, `team-
division-participation.js`, `registration-cleanup.js`, and `home-tasks-hub.js`
(the full suite, both roles) all re-confirmed green at desktop and 390×844
after the shared-code changes, plus the new `setup-progress-conflict-
attention.js` journey itself, zero console errors throughout.

### Round 22: LeagueSeason identity gap in round 21's own divisionless fix

Round 21's convergence review (#331 review round 22) confirmed both prior
reproductions fixed, but found a NEW release blocker introduced by round 21's
own fix.

**The bug.** `_revalidate_game_participation`'s division-less branch
resolved each Team's registration via `_require_team_registered` (correctly
season-wide/exact-key-conflict-aware since round 20), but then compared only
that registration's permanent League id against
`get_league_season(game.league_season_id).league_id` — never confirming
`game.league_season_id` itself resolves to a real row, nor that the row it
DOES resolve to actually belongs to the Game's own Season. Two adjacent
shapes slipped through as a result, reproduced live on Memory and real
SQLite: a dangling (non-`None` but nonexistent) `league_season_id`, and one
silently reassigned to a genuinely different Season's LeagueSeason for the
identical permanent League. Both `move_game` and `publish_game` accepted
either shape and mutated the Game (slot occupancy / `published`) plus wrote
an audit row — for BOTH the divisionless and (confirmed while reproducing
over real HTTP against the demo's own seeded divisioned Game) the divisioned
branch, since neither branch had ever independently validated the Game's own
`league_season_id` before this round.

**Fix.** Resolves and validates the Game's own `league_season_id` first —
existence, then Season match — using the identical two-step
`_require_batch_team_participation` already uses for its own target
LeagueSeason (missing → `regular_game_missing_league_season`; wrong Season →
the new `game_league_season_mismatch`), placed BEFORE the divisioned/
divisionless branch split so it protects both unconditionally. Each Team is
then required to hold an unambiguous, active registration EXACTLY at that
now-validated LeagueSeason — not merely one sharing its League — via a new
shared `_require_team_in_league_season(season_id, league_season, team_id)`,
extracted from `_require_batch_team_participation`'s own per-team block
(which now calls it too, replacing its prior duplicated inline logic with
zero behavior change) so the identical exact-key + season-wide two-layer
resolution and cross-League check live in exactly one place, callable from
both the draft-commit gate and this game-revalidation gate.

**Regression coverage:** four new tests in `GameParticipationRevalidationTest`
(`test_exact_registration_identity.py`) covering both shapes for both
`move_game` and `publish_game`, each asserting zero mutation (Game fields,
slot occupancy, `published` state, and audit-log count all unchanged on
rejection). A new `GameLeagueSeasonIdentitySqlTest` class in `test_v2_
reassignment_integrity_sql.py` proves the identical four cases on real
SQLite always and PostgreSQL when `TEST_DATABASE_URL` is set, using the same
zero-mutation assertions. A new `test_game_league_season_identity_http.py`
proves the dangling-id rejection over the actual authenticated `/api/games/
{id}/move` and `/api/games/{id}/publish` HTTP routes the UI itself posts to
— both actions write an audited mutation, so (unlike simpler role-only
checks) they require a genuine logged-in session, not just the `X-Demo-Role`
header other lighter-weight HTTP tests use.

**Falsifiability:** reverting the `setup_service.py` fix alone reproduces all
four claimed shapes live (both `move_game` and `publish_game` accept a
dangling `league_season_id` and one reassigned to a different Season) —
restored, reconfirmed green on Memory, both SQL backends, and over real
HTTP.

**Verification:** full Memory/SQLite (3514 tests, 314.6s) and PostgreSQL
(3514 tests, 917.9s) suites both green, +10 from round 21.
`scheduler-empty-state.js`/`scheduler-already-scheduled.js` (both exercise
`_require_batch_team_participation` via the draft scheduler, the other
caller of the now-shared per-team resolver) and `setup-progress-conflict-
attention.js` all reconfirmed green at desktop and 390×844, zero console
errors — no frontend files changed this round.

### Round 23: Game↔LeagueSeason League parity gap in round 22's own fix

Round 22's convergence review (#331 review round 23) confirmed both
previously reported reproductions fixed, but found one more adjacent
release blocker in the same identity invariant, introduced by round 22's
own fix.

**The bug.** `_revalidate_game_participation` validated that the Game's own
`league_season_id` resolves to a real row and belongs to the Game's Season,
and that both Teams hold an exact, unambiguous, active registration there
— but never checked the Game's own legacy `league_id` column against
`league_season.league_id`. Standings (`_standings_for_division`/
`_standings_for_league_season`) already fail closed on exactly this drift
when READING — a regular Game whose `league_id`/`season_id` disagree with
its `league_season_id` returns `data_integrity_error`/
`game_league_season_mismatch` rather than silently miscounting. Nothing
stopped `move_game`, `publish_game`, or `decide_reschedule`'s approve path
from COMMITTING that same drift in the first place. Reproduced live by
changing only a valid Game's stored `league_id` to a different League in
the same Season (leaving its valid `league_season_id`, Season, Teams, and
registrations untouched): on Memory and real SQLite, both `move_game` and
`publish_game` succeeded — move changed slot occupancy and wrote an audit
row, publish set `published=true` and wrote an audit row — for both the
division-less and divisioned Game, and `decide_reschedule`'s approve path
(which calls `move_game`/`publish_game` directly) committed the identical
drift. No current write path can produce this state (`create_game` always
derives `league_season_id` from the same League it stores as `league_id`),
so — like every gap this identity invariant has closed since round 21 —
this only ever fires on a corrupted/hand-edited row.

**Fix.** Adds one more unconditional check alongside round 22's own Season-
match check, in the same place (before the divisioned/divisionless split):
if `game.league_id` is set and disagrees with the already-validated
LeagueSeason's `league_id`, reject with the same `game_league_season_
mismatch` reason round 22 introduced (the standings boundary's existing
reason code, now reused rather than duplicated), including both the
Game's stored `league_id` and the LeagueSeason's actual `league_id` in the
details so remediation is unambiguous. `decide_reschedule`'s approve path
requires no separate change — it calls `move_game`/`publish_game`
directly, so it inherits every guard in `_revalidate_game_participation`
automatically.

**Regression coverage:** four new tests in `GameParticipationRevalidationTest`
(`test_exact_registration_identity.py`) — division-less `move_game`/
`publish_game`, a divisioned `move_game` (a fresh Division + Teams built
within the test, proving the divisioned branch is equally protected), and
`decide_reschedule`'s approve path end-to-end (request → opponent accept →
corrupt → approve, asserting the Game, its slot, and the
`RescheduleRequest`'s own status all stay unchanged on rejection) — each
asserting zero mutation. A new `GameLeagueIdIdentitySqlTest` class in
`test_v2_reassignment_integrity_sql.py` proves the `move`/`publish` cases on
real SQLite always and PostgreSQL when `TEST_DATABASE_URL` is set, same
zero-mutation assertions. `test_game_league_season_identity_http.py` gains
two more tests proving the rejection over the actual authenticated
`/api/games/{id}/move` and `/api/games/{id}/publish` routes, against the
demo's own seeded (divisioned) Game — covering the divisioned boundary at
the HTTP layer while the division-less shape is covered directly at the
facade/SQL layers. Fixing this file's tests also surfaced a test-isolation
bug of its own: the HTTP test class only called `STATE.reset()` once in
`setUpClass`, so one test's corruption of the shared demo Game leaked into
whichever test ran next (alphabetical, not declaration order); moved to a
per-test `setUp` so every HTTP test starts from a clean demo seed.

**Falsifiability:** reverting the `setup_service.py` fix alone reproduces
every new case live — all 8 new tests fail with the exact bug symptom
(facade/SQL: the call succeeds instead of returning a structured error;
HTTP: 200 instead of 400) — restored, reconfirmed green on Memory, both SQL
backends, and over real HTTP.

**Verification:** full Memory/SQLite (3522 tests, 466.5s) and PostgreSQL
(3522 tests, 1245.9s) suites both green, +8 from round 22.
`destructive-surfaces.js` (exercises `publish_game` on a real, uncorrupted
demo Game as setup for its own Cancel/Delete assertions) reconfirmed green
at desktop and phone — no frontend files changed this round, and no
existing e2e journey exercises `decide_reschedule` (unaffected either way,
since it needed no code change beyond `_revalidate_game_participation`
itself).

### Round 24: a falsy Game League bypassed round 23's own parity check

Round 23's convergence review (#331 review round 24) confirmed the non-null
wrong-League reproduction fixed, but found the same equality invariant still
open for a falsy stored League — again introduced by the previous round's
own fix.

**The bug.** Round 23 wrote the parity check as
`if game.league_id and ls.league_id != game.league_id:`. That truthiness
guard is not an equality check: it skips the comparison entirely whenever
the stored League is falsy. `Game.league_id` is explicitly `Optional` and
`games.league_id TEXT` is nullable on both SQLite and PostgreSQL, so `None`
is a genuinely storable row — and `""` behaves identically. With a valid
Season, LeagueSeason, Teams, and exact registrations, the write proceeded.
Reproduced live across every entry point before writing any fix: `move_game`
and `publish_game` for both the division-less and divisioned Game (move
changed slot occupancy and wrote an audit; publish set `published=true` and
wrote an audit plus notifications), `decide_reschedule`'s approve path
end-to-end (advancing the request from `pending_league_approval` to
`republished`), and single **and** batch draft publishing (clearing
`is_draft` and publishing). Standings then rejected the committed Game with
`data_integrity_error`/`game_league_season_mismatch` — the same read-vs-write
asymmetry round 23 was supposed to close.

**Fix.** The comparison is now unconditional plain equality:
`if game.league_id != ls.league_id:`. By that point the Game is REGULAR
(exhibitions return far earlier) and `ls` is a real, Season-matched
LeagueSeason, so a missing League is drift rather than a legacy-tolerant
case. This also closes any other falsy value, `""` included. The stored
value is reported in the error detail as-is — `None` included — so an
operator repairing the row sees what is actually on it.

**Checked the sibling, and it is not the same defect.** Round 22's Season
check carries the identical `if game.season_id and ...` shape, so it was
tested rather than assumed: a `None` `season_id` is still rejected, by the
downstream per-Team `_require_team_in_league_season` (season-wide
participation resolves to nothing → `team_not_registered`), so unlike the
League case there is no fail-open write. It is left as-is: changing it
would alter an existing, already-correct rejection's reason code without
closing any live gap.

**Regression coverage:** 11 new tests in `GameParticipationRevalidationTest`
covering `None` and `""` for division-less move/publish, `None` for
divisioned move/publish, reschedule approval, single draft publish, batch
draft publish by explicit ids **and** via `all_drafts=True`, plus a positive
control proving plain equality did not make the guard over-eager. Each
rejection asserts a whole-state snapshot is byte-identical: every Game row's
complete field set, every slot's status, the audit log, the notification
feed, notification deliveries, and every reschedule request's status — with
the batch cases additionally asserting the valid peer never left the draft
state. `GameLeagueIdIdentitySqlTest` gains 3 more (null move, null publish,
batch rollback) on real SQLite always and PostgreSQL when
`TEST_DATABASE_URL` is set. `test_game_league_season_identity_http.py` gains
2 more proving the rejection over the authenticated `/api/games/{id}/move`
and `/api/games/{id}/publish` routes against the demo's seeded divisioned
Game, asserting a null is reported as `null` and nothing was committed.

**Falsifiability:** restoring only the truthiness guard makes exactly the 15
new negative tests fail (13 facade/SQL, 2 HTTP) and nothing else — every
positive control and every round-21/22/23 test stays green, so the new
tests pin this specific defect rather than the surrounding behavior.

**Verification:** full Memory/SQLite (3538 tests, 459.1s) and PostgreSQL
(3538 tests, 1226.7s) suites both green, +16 from round 23.
`destructive-surfaces.js` (exercises `publish_game` and draft deletion on
real, uncorrupted demo Games) reconfirmed green at desktop and phone — no
frontend files changed this round.

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
