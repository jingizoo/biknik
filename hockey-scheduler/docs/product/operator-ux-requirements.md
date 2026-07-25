# Operator UX, Information Architecture & Accessibility — Requirements Package

## Goal

> Before any redesign code ships, define exactly what "better" means — per
> role, per screen, per state, per breakpoint, per WCAG success criterion —
> so the first implementation PR (Home/Tasks + a guided Setup hub) has an
> unambiguous, testable target instead of a general mandate to "make it
> nicer."

## Status

Bounded first deliverable of epic #204 (child issue #324; #204 stays open).
Per the reordered plan, this package now precedes #206 (Planner v2), which
resumes once this package and the first implementation PR land. **This
document is the deliverable — no application code changes accompany it.**
Sequencing:

```text
this requirements package (#324)
  → validated by @jingizoo
  → PR 1: Home/Tasks + guided Setup hub
  → PR 2: Schedule/Facilities UX
  → bounded #287 pre-#205 deliverable (documentation/design + a
    non-production UX prototype only — no schema, persistence, API
    mutations, notifications, or runtime state transitions; matches
    Release 2's own bar, corrected per review — see ROADMAP.md)
  → #206 (Planner v2) resumes
```

## Current state (baseline — requirements below are deltas, not inventions)

Grounding facts, so every requirement in this doc is a stated change against
something real rather than a guess:

- **Navigation today**: one persistent sidebar (`index.html:13-60`) with five
  task-oriented groups — Home, Schedule, People, Operations, Admin Setup —
  already organized "by task, not by implementation area" per an inline
  #145 comment (`index.html:20-24`). There is no router: a single module-level
  `view` variable (`app.js`) drives one large `render()` dispatcher
  (`app.js:5435`) that branches on `view === "..."` and one `switchTab()`
  (`app.js:6968`) that resets transient UI state and repaints. Tabs are
  shown/hidden per role via `hasPerm()` checks (e.g. `app.js:7325-7353`).
- **Persistent context today**: a topbar Program/Season switcher
  (`index.html:88-101`, #159) is a **saved display context**, not a filter —
  the UI carries a permanent, literal caption: "display only · screens not
  filtered" (`index.html:100`). League/Division selection is still local to
  each screen (e.g. the Scheduler tab's own Division picker). The switcher
  is a native `<select>` specifically so it inherits full keyboard/AT
  semantics for free — documented almost verbatim in both `app.js:7226-7228`
  and `docs/architecture/season-lifecycle.md:251-254`; any redesign must keep
  this native-control choice, not replace it with a custom listbox.
- **Roles today** (`hockey_scheduler/domain/roles.py`): 7 roles over 8
  permissions.

  | Role | Label | Permissions held |
  | --- | --- | --- |
  | `league_admin` | League Admin | all 8 (full control) |
  | `arena_manager` | Arena Manager | `manage_arena`, `manage_schedule`, `view` |
  | `coach` | Coach | `manage_roster`, `respond_availability`, `view` |
  | `player` | Player | `respond_availability`, `view` |
  | `guardian` | Guardian | `respond_availability`, `view` (scoped to linked juniors, enforced per-action) |
  | `official` | Official | `respond_assignment`, `view` |
  | `viewer` | Viewer | `view` only |

  Enforcement is layered: the server (`web/authz.py`) is the authoritative
  per-endpoint gate; `scope.py` further narrows some permissions to
  "your own team/game" (Coach); the client's `hasPerm()` hides/shows
  controls as a convenience only, never as security.
- **States today**: a shared skeleton (`app.js:5441`) paints before every
  view's data fetch; a whole-pane failure renders one banner with a single
  `#retry-btn` that re-runs `render()` (`app.js:5734-5742`); a ubiquitous
  `.empty` div convention (25+ call sites) always pairs a specific message
  with a concrete next action, never a bare "No data"; a shared
  `pageIntro(helperText, primaryActionHtml)` helper (`app.js:254`) gives 7+
  screens a consistent one-line description; a named, recurring
  "stale-response guard" pattern discards out-of-date async results
  (`app.js`, multiple sites); one fixed toast root
  (`#toast-root`, `role="status" aria-live="polite"`) surfaces every
  success/error message. Issue #311 (closed 2026-07-23, PR #312) is the
  canonical worked example of the required empty-state shape: it names the
  real backend-supplied count (`team_count`), states the exact selected
  scope ("Division (League)"), explains the domain cause in operator
  vocabulary (active *Season* registration vs. permanent *Team* membership),
  gives an in-place remediation link, and disables the primary commit action
  whenever the result set is empty. `e2e/scheduler-empty-state.js` is the
  existing acceptance test for that exact shape.
- **Accessibility today**: real, if partial, affordances exist — `title` +
  `aria-label` paired on every icon-only button; `role="button" tabindex="0"`
  plus a descriptive `aria-label` on non-native clickable rows; a global
  Escape-closes-anything handler and a global Enter/Space-activates-any-
  `role="button"` handler (`app.js`, #118 Phases 5-6); `:focus-visible` rings
  that never leave `outline:none` bare (`styles.css:907-908`). Real gaps: the
  app's only two dialog shapes (`.modal`, the Setup `.drawer`) are both
  `aria-modal="true"` but **neither implements a focus trap** — only the
  drawer moves initial focus to its first control, and nothing returns focus
  to the trigger on close; labeling is inconsistent (bound `<label for>` on
  static HTML, `aria-label` on JS-templated controls, with no stated rule for
  which to use when); there is **no automated accessibility check in CI**
  today (confirmed: zero `axe`/`a11y` references in `.github/workflows/`);
  and **zero WCAG references exist anywhere in the repo** — `ROADMAP.md:198`
  names #204 as the epic that must close this gap, and `ROADMAP.md:217`
  (#208) anticipates the automated gate but has not built it.
- **Responsive today**: one codebase, no separate mobile bundle. Breakpoints
  are ad hoc and file-scattered — 460/480/520/680/720/760/880/1040px across
  four CSS files, with **880px** the one true structural flip (sidebar →
  horizontal scrollable top nav, `web.css:271-289`) and **480px** the closest
  thing to a repeated "phone" convention. The one true canonical constant is
  a **test** viewport, not a CSS breakpoint: every viewport-sensitive
  Playwright spec hardcodes `{width: 390, height: 844}` labeled `"phone"`
  (e.g. `e2e/context-switcher.js:38-39`, `e2e/allowed-venues.js:31-32`,
  `e2e/coach-scope.js:22-23`) — matching `ROADMAP.md:203-204,266`'s repeated
  "desktop and 390px" delivery rule. A documented iOS fix forces form-input
  `font-size:16px` below 480px specifically to prevent Safari's auto-zoom
  (`styles.css:1162-1167`).
- **Existing e2e journeys** (`hockey-scheduler/e2e/`) already script real
  operator behavior worth anchoring to rather than re-inventing:
  `onboarding-wizard`, `demo-lifecycle`, `coach-scope`, `context-switcher`,
  `scheduler-empty-state`, `ice-availability-builder`, `hierarchy-import`,
  `season-rollover`, `setup-claim`, `allowed-venues`, `venue-sharing`,
  `permanent-teams`, `team-club-optional`, `team-division-participation`,
  `season-participation`, `player-lifecycle`, `player-edit`,
  `factory-reset`, `destructive-surfaces`, `safe-destructive`,
  `records-delete`, `division-delete-cleanup`, `registration-cleanup`,
  `venue-access-cleanup`, `api-error-resilience`, `scheduling-policy`,
  `season-dates`, `smoke`. (`ci-classify*`, `check-v1-route-contract`,
  `v1-dependency-proof`, and `season-fmt-unit` are CI-tooling/contract/unit
  tests, not operator journeys — excluded from role/journey mapping below.)

## Scope

### 1. Role-specific operator journeys

Every one of the 7 roles gets a named primary job, anchored to an existing
e2e journey where one exists, and an explicit flag where the redesign must
create one:

| Role | Primary job-to-be-done | Existing e2e anchor | Gap this package flags |
| --- | --- | --- | --- |
| League Admin | Stand up a new season end-to-end (league profile, seasons, teams, divisions, venues) and see what's still incomplete | `onboarding-wizard`, `hierarchy-import`, `season-rollover`, `setup-claim` | No single "what's left to do" view exists today — this is exactly the Home/Tasks hub's job |
| Arena Manager | Publish recurring ice availability and resolve rink-level scheduling conflicts | `ice-availability-builder`, `allowed-venues`, `venue-sharing` | Facilities as its own top-level area (vs. buried in Setup) — this package's IA crosswalk (§2) |
| Coach | See my team's schedule/roster and act only within my team's scope | `coach-scope`, `player-lifecycle`, `player-edit` | None identified — journey exists and is scope-tested |
| Player | Confirm availability, see my next game | *(none named — player_home tab exists in nav but is hidden by default, `index.html:29`)* | **New journey needed**: a Player-facing e2e journey does not exist yet |
| Guardian | Respond on behalf of a linked junior | *(none named — guardian_home tab exists but hidden by default, `index.html:30`)* | **New journey needed** |
| Official | Accept/decline my own assignments | *(none named — inbox tab exists but hidden by default, `index.html:31`)* | **New journey needed** |
| Viewer | Read schedule/roster/standings with zero mutation surface | `smoke` (partial coverage only) | Confirm no primary action ever renders enabled for Viewer; needs an explicit assertion, not incidental coverage |

**Requirement**: Player, Guardian, and Official each get a real e2e journey
(even a thin one) as part of the **first implementation PR's own test
plan** — not a prerequisite gate before that PR starts. This is not a docs-
vs-code conflict: Player Home, Guardian Home ("My Players"), and Official
Inbox ("My Assignments") are themselves part of the Home/Tasks area (§2),
so their journeys are naturally in scope for the PR that builds Home/Tasks,
same as any other screen it touches needing its own acceptance test. Three
of seven roles currently have zero scripted acceptance coverage, and a
Home/Tasks PR that only re-validates the two roles already covered
(League Admin's Dashboard, and indirectly Coach via `coach-scope`) would
silently ship regressions to the other three — so this package requires
the PR's test plan name all three explicitly, rather than treat "Home/Tasks
built and desktop looks right" as sufficient.

### 2. Task-oriented navigation and setup

**IA crosswalk** — every currently-reachable tab maps to exactly one new
screen (not just an area) in the 7-area IA #204 already proposed (Home/
Tasks, Schedule, Teams & People, Facilities, Communications, Reports,
Administration), so nothing today's five groups can reach becomes
unreachable, and nothing is left as "lands somewhere in Facilities"
without saying where:

| Today's group (#145) | Today's tab | New area | New screen (final destination) |
| --- | --- | --- | --- |
| Home | Dashboard | Home/Tasks | Home/Tasks hub (replaces Dashboard as the League Admin/Arena Manager landing) |
| Home | Home (player) | Home/Tasks | Player Home |
| Home | My Players (guardian) | Home/Tasks | Guardian Home |
| Home | My Assignments (official) | Home/Tasks | Official Inbox |
| Home | Activity | Home/Tasks | Activity |
| Schedule | Arena Calendar | Schedule | Arena Calendar (unchanged) |
| Schedule | Games | Schedule | Games (unchanged) |
| Schedule | Scheduler | Schedule | Scheduler (unchanged) |
| Schedule | Standings | Schedule | Standings (unchanged) |
| Schedule | Game Sheet | Schedule | Game Sheet (unchanged) |
| Schedule | Public | Schedule | Public (unchanged) |
| People | Roster | Teams & People | Roster (unchanged) |
| People | Users | **Administration** | Users — **resolved** (was open): account/login lifecycle (activate/deactivate, `MANAGE_USERS`) is a one-time administrative concern distinct from roster/team-membership, and sits with the other one-time configuration workflows, not with Teams & People's day-to-day roster concerns |
| Operations | Notifications | Communications | Notifications (unchanged) |
| Operations | Delivery | Communications | Delivery (unchanged) |
| Operations | Pilot Readiness | Reports | Pilot Readiness / Reports |
| Operations | Import | Administration | Folds into the "Imports and onboarding" Setup workflow (no standalone Import screen) |
| Admin Setup | Initial Setup (onboarding) | Administration | Folds into the "Imports and onboarding" Setup workflow |
| Admin Setup | Setup | Facilities + Administration | Splits across the six named Setup workflows below: "Venues, rinks and ice" → Facilities; the other five → Administration |

**Setup hub decomposition** — #204 already names the six focused workflows
the current single Setup page must split into; this package makes each one
concrete enough to build against:

1. League profile and seasons
2. Permanent teams
3. Season participation/divisions
4. Clubs, players and staff
5. Venues, rinks and ice
6. Imports and onboarding

Each workflow gets: one entry point reachable from the Home/Tasks hub, one
landing summary (not a form), and drill-in detail screens — matching #204's
"show summary first; reveal detail progressively" principle. The Home/Tasks
hub's job (per the League Admin journey in §1) is to compute and display
*which of these six is next incomplete*, not to require the operator to
infer that from the data model.

**Requirement**: every new area/hub screen states its own single primary
action per §4 below — this section defines *where* things live; §4 defines
*what the one obvious next step is* once you're there.

### 3. Persistent Program/Season context

The existing context switcher (native `<select>`, optgroup-by-Program,
`(scope_type, scope_id)`-gated options endpoint, base64url URL-hash
deep-link — all reviewed and merged, not to be re-litigated) is the right
mechanism. What it does with the selection is the open gap:

- **Requirement**: for every screen in the new IA, state explicitly whether
  it is (a) filtered by the selected Program/Season, or (b) an explicit,
  named exception (e.g. a cross-season Reports view) — with a one-line
  reason. "Not filtered yet" is not an acceptable final state for any screen
  that logically has a Program/Season scope.
- **Requirement**: once a screen is wired to filter, the permanent
  "display only · screens not filtered" caption (`index.html:100`) is
  removed for that screen's context, or narrowed to name only the still-
  unwired exceptions — the caption must never silently become a stale lie.
- **Resolved** (was open): **League** is promoted into the persistent
  context bar alongside Program/Season. Structurally, `League` and `Season`
  are both direct children of `Program` (a `LeagueSeason` join pairs a
  specific League with a specific Season, and Divisions/registrations key
  off that pairing) — League is a first-class axis most screens need, not a
  refinement of Season, so it belongs beside Season in the persistent bar.
  **Division** stays screen-local (e.g. the Scheduler's own Division
  picker): it is a narrower slice *within* an already-selected League+Season,
  used by only a minority of screens (Scheduler, some Reports), so promoting
  it would add a persistent control most screens ignore. This mirrors #204's
  own "keep one selected League and Season context visible throughout the
  app" principle literally, while Division correctly stays local.
- **Constraint**: whatever is decided must preserve the reviewed #159/#322/
  #323 mechanics (native control, session-gated authorization, deep-link
  reconciliation) — this is a filtering-behavior change, not a rebuild.

### 4. One primary action per screen

**Definition**: exactly one control per screen (or per hub sub-view) is
styled/marked as the primary action (today's `.act.primary` convention);
every other action is visually secondary or tertiary. Ambiguous verbs are
replaced with the specific verb #204 already names as the standard — e.g.
"Move" becomes "Change club," "Change venue," or "Assign division"
depending on what is actually changing.

**Primary-action audit** — completed here, for the Home/Tasks hub and each
of the six Setup workflows, so the implementation PR executes a checklist
rather than making a design decision mid-implementation:

| Screen | Today's competing actions | Designated primary action | Every other action becomes |
| --- | --- | --- | --- |
| Home/Tasks hub | Doesn't exist as such today (today's Dashboard has no single action hierarchy) | "Continue setup" — a dynamic label naming the actual next incomplete step, deep-linking straight into it | Viewing activity, jumping directly to any specific workflow, dismissing/reordering tasks → a secondary task list below the primary card, never competing for primary styling |
| League profile and seasons | Today: "Add Season" competes visually with inline league-profile edit fields inside the single Setup page | "Add Season" (the action that creates new schedulable time) | League-profile edits, venue-access grants, season history → secondary/tertiary controls inside the season-detail drill-in |
| Permanent teams | Today: "Add Team" competes with inline "Move" (ambiguous re-parenting) and delete controls | "Add Team" | "Change club" (renamed from "Move"), deactivate/reactivate, delete → secondary actions on each team's row/detail; delete is confirmation-gated (§5) |
| Season participation/divisions | Today: "Register Team" and "Assign division" render as similar-weight buttons | "Register Team" (the entry action) | "Assign division" becomes a secondary follow-up prompt surfaced right after registration, not a permanently-competing button; unregister/deactivate → tertiary, confirmation-gated |
| Clubs, players and staff | Today: "Add Player," "Add Staff," "Import" all render with equal visual weight | "Add Player" (the highest-frequency action) | "Add Staff," "Import roster," edit → secondary/tertiary; deactivate/delete → confirmation-gated |
| Venues, rinks and ice | Today: "Add Venue," "Add Rink," "Add Ice Slot," and the Ice Availability Builder's "Generate" all compete | "Add Ice" (opens the Ice Availability Builder — the highest-leverage action, since it bulk-generates recurring slots) | "Add Venue"/"Add Rink" → secondary (rare, mostly one-time); a single ad hoc "Add Ice Slot" → tertiary, for the rare exception the recurring builder doesn't cover |
| Imports and onboarding | Today: the onboarding wizard's own step "Next" and the separate Import tab's upload control aren't unified | "Import data" — one entry point unifying the onboarding wizard's bulk-import step and the standalone Import tab | Manual single-record entry (the alternative to bulk import) → a secondary "or add one at a time" link, not a second primary button |

### 5. Loading, empty, stale, error, retry, and confirmation states

Today's de facto conventions become required conventions, extended to every
new screen the redesign introduces:

| State | Required pattern (today's baseline → what's required going forward) |
| --- | --- |
| Loading | Skeleton placeholder before first paint (as today, `app.js:5441`) — every new hub/screen gets one; no blank white flash. |
| Empty | The `.empty` + `pageIntro` convention, with the full #311 recipe as the bar: name the real count/scope, explain the cause in operator vocabulary, give an in-place remediation link, disable any action that would operate on the empty set. A bare "No data" is a regression, not an acceptable empty state. |
| Stale | The existing "stale-response guard" pattern (discard out-of-date async results) extends to every new async view — required, not optional, for any screen with more than one in-flight request source (e.g. context switch mid-load). |
| Error | **Resolved** (see Product decisions): today is whole-pane-only. Going forward, hub/dashboard-shaped screens (Home/Tasks, Setup workflow landings) use per-card error boundaries — one card's failure never blanks the rest; single-purpose detail screens keep the existing whole-pane pattern. The states matrix below states which applies per screen. |
| Retry | An explicit retry affordance at every failure point, matching today's granularity options (page-level `#retry-btn`, row-level retry as in dead-letter notification rows) — no error state without a next action. |
| Confirmation | Every destructive action (delete/reset/cleanup) uses a modal that names the specific resource being affected — never a generic "Are you sure?" — matching the existing `destructive-surfaces`/`safe-destructive`/`factory-reset`/`records-delete`/`division-delete-cleanup`/`registration-cleanup`/`venue-access-cleanup` journeys. Every new destructive action introduced by the redesign must have an equivalent e2e journey before it ships. |

**States matrix** — every screen in the proposed 7-area IA (per the §2
crosswalk), filled in rather than left as a template. "Skeleton"/"stale-
guard"/"named-resource modal" refer to the existing patterns described
above; entries only add detail where a screen's behavior is genuinely
different from the default.

*Home/Tasks*

| Screen | Loading | Empty | Stale | Error | Retry | Confirm |
| --- | --- | --- | --- | --- | --- | --- |
| Home/Tasks hub | Skeleton task-card list | "All setup steps complete" success state + link into Schedule (a success state, not a failure) | Stale-guard on context switch / task completion mid-view | Per-card (§ decision above) | Row-level, on the failed card | n/a — no destructive action lives here |
| Player Home | Skeleton | "No upcoming games" + link to team schedule | Stale-guard on context/role switch | Per-card | Row-level | n/a |
| Guardian Home | Skeleton | "No linked players yet" + contact-league-admin guidance | Stale-guard on linked-player list refresh | Per-card | Row-level | n/a |
| Official Inbox | Skeleton | "No open assignments" | Stale-guard (assignment list can change server-side between polls) | Per-card | Row-level | Accept/decline get a lightweight inline confirm naming the specific game (not a full modal — low-risk, reversible-until-deadline action) |
| Activity | Skeleton | "No recent activity" | Stale-guard on feed refresh | Whole-pane (single-purpose feed, not multi-card) | Page-level `#retry-btn` | n/a |

*Schedule*

| Screen | Loading | Empty | Stale | Error | Retry | Confirm |
| --- | --- | --- | --- | --- | --- | --- |
| Arena Calendar | Skeleton | "No ice scheduled this range" + "Add Ice" link | Stale-guard on month/context navigation (existing, keep) | Whole-pane | Page-level | Deleting/cancelling ice → named-resource modal |
| Games | Skeleton | #311-style: names the real count/scope | Stale-guard (existing) | Whole-pane | Page-level | Cancelling a game → named-resource modal |
| Scheduler | Skeleton | The existing #311 empty-state recipe (already built — keep verbatim) | Stale-guard (existing, keep) | Whole-pane | Page-level | Discarding a draft → named-resource modal |
| Standings | Skeleton | "No completed games yet" | Stale-guard on context switch | Whole-pane | Page-level | n/a (read-only) |
| Game Sheet | Skeleton | n/a — only reachable for an existing game; no meaningful empty state | Stale-guard on live score updates | Whole-pane | Page-level | Finalizing/correcting a score → named-resource modal |
| Public | Skeleton | "No public schedule available" | Stale-guard (existing `#public-retry-btn`, keep) | Whole-pane (existing) | Page-level `#public-retry-btn` (existing) | n/a — read-only, unauthenticated |

*Teams & People*

| Screen | Loading | Empty | Stale | Error | Retry | Confirm |
| --- | --- | --- | --- | --- | --- | --- |
| Roster | Skeleton | "No players on this roster yet" + add link | Stale-guard on team/context switch | Whole-pane | Page-level | Removing a player → named-resource modal |

*Facilities*

| Screen | Loading | Empty | Stale | Error | Retry | Confirm |
| --- | --- | --- | --- | --- | --- | --- |
| Venues, rinks & ice | Skeleton | Landing: "No venues yet" + "Add Venue"; drill-in: "No ice on this rink yet" + "Add Ice" | Stale-guard on builder preview-vs-commit (existing, keep) | Per-card on the landing summary (independent venue cards); whole-pane on a single venue/rink drill-in | Row-level (summary) / page-level (drill-in) | Deleting a venue/rink/ice slot → named-resource modal (existing `destructive-surfaces` pattern, keep) |

*Communications*

| Screen | Loading | Empty | Stale | Error | Retry | Confirm |
| --- | --- | --- | --- | --- | --- | --- |
| Notifications | Skeleton | "No notifications" | Stale-guard (existing) | Whole-pane | Page-level + existing row-level retry/ignore on dead-letter rows | Clearing/dismissing → lightweight inline confirm, not a full modal |
| Delivery | Skeleton | "No delivery activity yet" | Stale-guard | Whole-pane | Page-level + row-level (existing dead-letter retry) | n/a |

*Reports*

| Screen | Loading | Empty | Stale | Error | Retry | Confirm |
| --- | --- | --- | --- | --- | --- | --- |
| Pilot Readiness / Reports | Skeleton | "No data for this range yet" | Stale-guard on date-range/context change | Per-card (independent report widgets) | Row-level, per widget | n/a (read-only) |

*Administration*

| Screen | Loading | Empty | Stale | Error | Retry | Confirm |
| --- | --- | --- | --- | --- | --- | --- |
| League profile and seasons | Skeleton | "No seasons yet" + "Add Season" link | Stale-guard on season list after edit | Per-card (season list can grow long) | Row-level | Archiving/deleting a season → named-resource modal |
| Permanent teams | Skeleton | "No teams yet" + "Add Team" link | Stale-guard | Per-card (independent team rows) | Row-level | Delete/deactivate → named-resource modal (existing `records-delete`/`safe-destructive` pattern) |
| Season participation/divisions | Skeleton | The existing #311 recipe, reused verbatim | Stale-guard (existing) | Whole-pane | Page-level | Unregistering a team → named-resource modal (existing `registration-cleanup` pattern) |
| Clubs, players and staff | Skeleton | "No players yet" + "Add Player" link | Stale-guard | Per-card (player/staff lists can grow long) | Row-level | Deactivate/delete → named-resource modal (existing `player-lifecycle` pattern) |
| Users | Skeleton | "No user accounts yet" (rare — at least one admin always exists post-claim) | Stale-guard on role/permission edits | Whole-pane | Page-level | Deactivating a login → named-resource modal |
| Imports and onboarding | Skeleton during file parse/validation | "No import in progress — upload a file to begin" | Stale-guard (existing dry-run-vs-commit pattern, keep) | Whole-pane (a wizard, single-purpose per step) | Page-level | Committing an import with warnings → a named-resource-style refusal naming exactly which rows are affected (existing `ice_slot_overlap`-style copy, keep) |

### 6. Desktop and 390px behavior

- **Requirement**: `390×844` remains the canonical phone test viewport
  (already used in 10+ specs) — do not introduce a second phone size.
  Desktop stays whatever each existing spec already uses (commonly
  `1280×800`).
- **Resolved** (was open — exact values, not a category): today's eight ad
  hoc breakpoints consolidate into four named tokens, chosen by inspecting
  what each one actually does (`styles.css:342-343,795-799,899,913,961-964,
  1079-1082,1153-1167`; `web.css:267-289,292+`):

  | Token | Value | Replaces | Rationale |
  | --- | --- | --- | --- |
  | `--bp-phone` | **480px** | 460 (Ice Builder 2-col→1-col), 480 (drawer full-width, icon-btn 40×40 touch target, context-switcher compact mode, the dedicated "#100: responsive pass" block, iOS 16px input fix), 520 (setup.css) | 480 is already the dominant, most-used value (5 of 8 sites); 460 and 520 are the same "phone-density" concern authored at slightly different times, so consolidating onto 480 is the target choice — **not yet visually verified**: folding 460→480 and 520→480 moves each affected layout's collapse point by up to 40px, and must be checked at the affected widths plus desktop/390px before this token ships |
  | `--bp-tablet` | **720px** | 680 (Game Sheet grids → 1-col), 720 (Arena Calendar `.cal-layout` row→column, kept as-is), 760 (onboarding) | 720 already serves the single most content-dense of the three (the Calendar), making it the target consolidation point — **not yet visually verified**: folding 680→720 and 760→720 must be checked at those exact widths (and desktop/390px) as part of the first implementation PR, not assumed negligible from the pixel delta alone |
  | `--bp-nav-flip` | **880px** | 880 (unchanged) | The one true structural layout change (sidebar → horizontal scrollable top nav) stays isolated from the two content-collapse tokens above so nav layout and content density can vary independently — unchanged, so no new verification needed here |
  | `--bp-wide` | **1040px** | 1040 (unchanged) | A distinct, wider concern (dashboard/report grid de-densifies pre-emptively while the sidebar is still full-width, i.e. *before* the 880px nav flip) — kept as its own token rather than force-merged into 880, since it fires at a meaningfully different width for a different reason; unchanged, so no new verification needed here |

  New hub CSS uses these four tokens exclusively — it must not add a fifth
  magic number. **The two consolidations above are a target decision, not a
  demonstrated non-regression** — the first implementation PR must visually
  verify Game Sheet, Ice Builder, onboarding, and setup.css's affected
  layouts at their old and new breakpoints (plus the standard desktop/390px
  pair) before relying on the new tokens.
- **Requirement**: the redesigned nav (7 areas instead of 5 groups) states
  its own mobile collapse behavior explicitly. Today's 880px flip
  (`web.css:271-289`) turns the sidebar into a horizontal scrollable tab
  strip; two additional top-level areas changes how much of that strip is
  visible without scrolling on a 390px screen — this is a specific,
  named risk to validate, not an incidental side effect.
- **Constraint**: preserve the documented 16px minimum input font-size fix
  (`styles.css:1162-1167`) that prevents iOS Safari's auto-zoom-on-focus —
  every new form control on any new screen inherits this, no exceptions.

### 7. WCAG 2.2 AA — keyboard, focus, labeling, screen-reader

**Correction from initial draft**: WCAG 2.2 Level AA conformance means
conformance to **every applicable Level A and Level AA success criterion**
under the [official W3C standard](https://www.w3.org/TR/WCAG22/) — not a
hand-picked subset. An earlier version of this section named seven SCs as
if they were the scope; that was wrong. Those seven (plus per-view page
titling, identified during the full-matrix audit below) remain valid — they
map to real, currently-failing gaps this app has today — but they are now
correctly framed as a **priority regression list**, subordinate to the
**full conformance matrix** below, which is the actual target.

**Also corrected**: **SC 2.4.12 "Focus Not Obscured (Enhanced)" is Level
AAA**, not AA — it is not part of the required matrix and is called out
separately, below, as a voluntary stretch goal. **SC 2.4.11 "Focus Not
Obscured (Minimum)" is the correct AA criterion**, and it covers a
*different* concern (a sticky header/toolbar visually covering the
currently-focused element) than dialog focus trapping, which is properly
governed by **SC 2.1.2 "No Keyboard Trap"** and **SC 2.4.3 "Focus Order"**
— the priority list below cites the correct SCs for each concern.

**Automated tooling is one gate, not proof of conformance.** Axe-core (or
equivalent) catches a meaningful subset of failures automatically — mostly
markup-detectable ones (missing labels, contrast ratios, missing landmarks)
— but most of the matrix below requires manual review (keyboard-only
walkthroughs, screen-reader testing, judgment calls like "is this sensory
characteristic description ambiguous"). Both are required; neither replaces
the other.

**Priority regressions** (known, currently-failing today — fix these first,
without waiting for the full matrix below to be worked through screen by
screen):

- **Dialog focus management** (SC 2.1.2 No Keyboard Trap, SC 2.4.3 Focus
  Order): every surface marked `aria-modal="true"` must implement a real
  focus trap — Tab/Shift+Tab cycles only within the dialog, initial focus
  lands on the first control or a heading, and focus returns to the
  triggering element on close. **Currently failing**: neither existing
  dialog shape (`.modal`, the Setup `.drawer`) does this — the drawer sets
  initial focus only; nothing cycles or returns it. New dialogs in the
  redesign must not repeat the gap.
- **Bypass blocks** (SC 2.4.1): no "skip to main content" link exists in
  `index.html` today, despite a persistent sidebar repeated on every view.
  **Currently failing** — add one as part of the nav rebuild.
- **Per-view page titling** (SC 2.4.2): the `<title>` (`index.html:6`) is
  static across every view of this single-page app. W3C's own guidance for
  SPAs calls for the title (or an equivalent programmatic announcement) to
  change with each distinct view. **Currently failing** — update
  `document.title` (or use a live-region view-change announcement) on
  `switchTab()`.
- **Dragging alternative** (SC 2.5.7, new in 2.2): the draggable ice slot
  interaction (`app.js:2437`) needs a confirmed non-drag alternative (e.g.
  a "Move" menu action) — not confirmed present in the current codebase.
- **Labeling convention** (SC 1.3.1, 3.3.2, 4.1.2): unify today's two
  divergent conventions — bound `<label for="...">` on static HTML,
  `aria-label` on JS-templated controls — behind one explicit rule: prefer
  a real, bound label wherever the markup is static enough to support it;
  reserve `aria-label` for genuinely dynamic/JS-templated or icon-only
  controls. State this rule in the design system so it is a decision, not
  an accident of which screen a control happened to ship on first.
- **Reflow at 320px** (SC 1.4.10): this package's 390×844 phone test
  convention (§6) does not by itself prove compliance at WCAG's 320 CSS px
  reflow target — a narrower check is needed, at least for the new hub/
  Setup screens.
- **Contrast and target-size audit at desktop width** (SC 1.4.3, 2.5.8):
  icon buttons already meet the 24×24 CSS px minimum at the 480px phone
  breakpoint (`styles.css:913`), but 2.5.8 applies at every viewport size —
  desktop-width icon buttons are unverified. No automated contrast check
  exists today (the axe-core gate below is meant to catch this class of
  issue going forward).

**Full A + AA conformance matrix** — every applicable Level A/AA success
criterion in WCAG 2.2 (Level AAA criteria, including 2.4.12, are excluded —
listed separately below as voluntary). Status is honest about what can be
asserted today, held to a strict evidentiary bar: **Met** means specific,
verifiable evidence was checked and satisfies the criterion (a grep
confirming an absence, a cited code rule, a directly-read markup fact) —
"no known violation" alone does not qualify and is marked **Verify**
instead. **Partial** means real, cited coverage exists but doesn't reach
every case the criterion requires. **Gap** is a known, currently-failing
requirement. **Verify** covers both existing screens whose conformance
hasn't actually been tested and new screens that don't exist yet. **N/A**
is genuinely inapplicable, with why.

*Perceivable*

| SC | Name | Level | Status |
| --- | --- | --- | --- |
| 1.1.1 | Non-text Content | A | Partial — icon-only controls pair `title`+`aria-label` (existing convention); new screens verify at implementation |
| 1.2.1 | Audio-only/Video-only (Prerecorded) | A | N/A — no audio/video content anywhere in the app |
| 1.2.2 | Captions (Prerecorded) | A | N/A — no video content |
| 1.2.3 | Audio Description or Media Alternative | A | N/A — no video content |
| 1.2.4 | Captions (Live) | AA | N/A — no live audio/video |
| 1.2.5 | Audio Description (Prerecorded) | AA | N/A — no video content |
| 1.3.1 | Info and Relationships | A | Partial — see labeling-convention gap above |
| 1.3.2 | Meaningful Sequence | A | Verify — no known violation; audit new card/grid layouts for DOM-vs-visual order |
| 1.3.3 | Sensory Characteristics | A | Verify — no known violation, not explicitly audited |
| 1.3.4 | Orientation | AA | Met — grep-confirmed zero orientation-lock code anywhere in `web/static/` (no `orientation` references); the criterion is a prohibition, and its absence is verified, not assumed |
| 1.3.5 | Identify Input Purpose | AA | Verify — `autocomplete` attributes on common fields (email, name) not confirmed present |
| 1.4.1 | Use of Color | A | Partial — conflict/status styling already pairs color with text/weight; no systematic audit done |
| 1.4.2 | Audio Control | A | N/A — no auto-playing audio |
| 1.4.3 | Contrast (Minimum) | AA | Verify — no automated contrast check exists today (see automated-gate note above) |
| 1.4.4 | Resize Text | AA | Verify — no `user-scalable=no`/fixed viewport meta blocks zoom (`index.html:5`), but that only shows zoom isn't *disabled*; it doesn't prove text reaches 200% without loss of content or functionality. Needs an actual 200%-zoom test, not inferred from viewport markup |
| 1.4.5 | Images of Text | AA | Met — grep-confirmed zero `<img>`/`background-image` usage anywhere in `web/static/`; all text renders as text/CSS by construction, not by absence of a known violation |
| 1.4.10 | Reflow | AA | Gap — see priority list above (390px convention doesn't prove the 320px target) |
| 1.4.11 | Non-text Contrast | AA | Verify — the `:focus-visible` ring exists (`styles.css:907-908`) but its contrast ratio is unmeasured |
| 1.4.12 | Text Spacing | AA | Verify — no test for user style overrides today |
| 1.4.13 | Content on Hover or Focus | AA | N/A today (only native `title` tooltips, exempt); verify if new hover-triggered content is added |

*Operable*

| SC | Name | Level | Status |
| --- | --- | --- | --- |
| 2.1.1 | Keyboard | A | Partial — `role="button" tabindex="0"` retrofits + global Enter/Space activator cover most controls, but the draggable ice slot's keyboard-equivalent is unconfirmed (see 2.5.1/2.5.7 below), so not *all* functionality is verified keyboard-operable yet |
| 2.1.2 | No Keyboard Trap | A | Gap — see priority list (correct citation for dialog escapability) |
| 2.1.4 | Character Key Shortcuts | A | N/A — no single-key shortcuts today; constraint if the redesign adds any |
| 2.2.1 | Timing Adjustable | A | Verify — session/auth timeout extension mechanism not audited here |
| 2.2.2 | Pause, Stop, Hide | A | N/A — no auto-advancing/continuously auto-refreshing content observed |
| 2.3.1 | Three Flashes | A | N/A — no flashing content anywhere |
| 2.4.1 | Bypass Blocks | A | Gap — see priority list (no skip-to-content link) |
| 2.4.2 | Page Titled | A | Gap — `<title>Hockey Scheduler — Operator Console</title>` (`index.html:6`) is static across every view; W3C's own guidance for single-page apps calls for the title (or an equivalent programmatic announcement) to change with each distinct view, which this app does not do today. Added to the priority regression list above |
| 2.4.3 | Focus Order | A | Gap — see priority list (dialog focus management) |
| 2.4.4 | Link Purpose (In Context) | A | Verify — icon-only links' `aria-label` needs to read sensibly out of context |
| 2.4.5 | Multiple Ways | AA | Verify — the sidebar nav is one way to locate content; URL-hash deep-linking is a state-restoration mechanism, not a second user-facing way to *find* content, so it doesn't independently satisfy this SC. A genuine second locating mechanism (search, a site-map-style index) is not confirmed to exist — name one or accept single-path status pending review |
| 2.4.6 | Headings and Labels | AA | Verify — needs a heading-level audit once the new hub/workflow screens exist |
| 2.4.7 | Focus Visible | AA | Met — `:focus-visible` rings never left bare (`styles.css:907-908`); new screens must preserve this |
| 2.4.11 | Focus Not Obscured (Minimum) | AA | Verify — the fixed toast root and any sticky topbar/sidebar elements must never obscure a focused control; not yet audited |
| 2.5.1 | Pointer Gestures | A | Verify — the draggable ice slot needs a confirmed single-pointer alternative |
| 2.5.2 | Pointer Cancellation | A | Verify — click handlers should fire on up-event, not down-event; not audited |
| 2.5.3 | Label in Name | A | Verify — check specifically where `aria-label` text differs from visible label text |
| 2.5.4 | Motion Actuation | A | N/A — no device-motion-triggered controls |
| 2.5.7 | Dragging Movements | AA | Gap — see priority list (draggable ice slot alternative) |
| 2.5.8 | Target Size (Minimum) | AA | Partial — met at the 480px phone breakpoint (`styles.css:913`); desktop-width icon buttons unverified (see priority list) |

*Understandable*

| SC | Name | Level | Status |
| --- | --- | --- | --- |
| 3.1.1 | Language of Page | A | Met — `<html lang="en">` (`index.html:2`) |
| 3.1.2 | Language of Parts | AA | N/A — no mixed-language content today |
| 3.2.1 | On Focus | A | Verify — no control should trigger a context change merely on receiving focus; no known violation |
| 3.2.2 | On Input | A | Reviewed exception — the context switcher's `<select>` intentionally changes context on selection; this is an SC-permitted, documented behavior (#159), not an accidental violation |
| 3.2.3 | Consistent Navigation | AA | Met — one static `<nav class="side-nav">` block (`index.html:25-60`) renders for every view; `render()` toggles visibility/active state per role but never reorders or regenerates the DOM per view, so order is structurally guaranteed, not merely observed; redesign must preserve this |
| 3.2.4 | Consistent Identification | AA | Partial — the shared `pageIntro`/`.empty`/icon+label helper functions guarantee consistency for the screens that call them, but not every icon/control in the app has been confirmed to route through those helpers rather than a one-off implementation |
| 3.2.6 | Consistent Help | A | N/A today — no persistent help/contact mechanism exists; constraint if one is added (same location every screen) |
| 3.3.1 | Error Identification | A | Partial — the normalized `{error:{code,message}}` envelope + inline `role="alert"` validation identify errors at the API/form-submit level; field-level identification (which specific input is wrong, on every form) is not confirmed comprehensive across the app |
| 3.3.2 | Labels or Instructions | A | Partial — see labeling-convention gap above |
| 3.3.3 | Error Suggestion | AA | Partial — the #311 empty-state recipe is a genuine, verified example of this SC done right for that one flow; it is the required bar for all new copy (§5), but doesn't by itself prove every existing form gives a correction suggestion, not just an error label |
| 3.3.4 | Error Prevention (Legal, Financial, Data) | AA | Partial — the named-resource confirmation-modal convention covers deletions specifically; not every in-scope legal/financial/data-modifying submission in the app has been confirmed to have an equivalent reversible/checked/confirmed step |
| 3.3.7 | Redundant Entry | A | Verify — confirm wizard steps (onboarding, Ice Availability Builder) never require re-entering already-given data; extend to the redesigned Setup workflows |
| 3.3.8 | Accessible Authentication (Minimum) | AA | Verify — no CAPTCHA/cognitive-function-test observed on login, but paste/autofill behavior and every authentication step (not just the primary login form) have not been audited; "likely" is not a verified status |

*Robust*

| SC | Name | Level | Status |
| --- | --- | --- | --- |
| 4.1.2 | Name, Role, Value | A | Partial — see labeling-convention gap; the dialog focus-management gap is partly behavioral, not markup, here |
| 4.1.3 | Status Messages | AA | Met — the `aria-live="polite"` toast root and modal `role="alert"` validation; new async confirmations must reuse this |

*(SC 4.1.1 Parsing was removed in WCAG 2.2 and is not part of this matrix.)*

**Voluntary AAA stretch goals** (not required for AA conformance, listed
only so they are never mistaken for AA requirements): SC 2.4.12 Focus Not
Obscured (Enhanced), SC 2.4.13 Focus Appearance, SC 3.3.9 Accessible
Authentication (Enhanced). If the redesign happens to satisfy any of these,
that's a bonus — none gate this package's or the first implementation PR's
acceptance.

**Automated + manual gates**:

- Add an automated accessibility check (e.g. axe-core) to CI, catching the
  markup-detectable subset of the matrix above. `ROADMAP.md:217` (#208)
  already anticipates this; this package treats it as one of *its own*
  closeable acceptance items (§8).
- A manual keyboard-only and screen-reader pass on the Home/Tasks hub and
  guided Setup hub is required before that PR is considered done, per
  #204's own "manual keyboard/screen-reader acceptance" line — this is what
  catches everything the automated gate structurally cannot.

### 8. Operator validation and measurable success criteria

**This package's own acceptance criteria** (narrower than epic #204's, which
cover the eventual shipped redesign — these gate *this document*). **All
nine satisfied and signed off by @jingizoo (2026-07-24)** — see the sign-off
note under "Product decisions requiring sign-off" below for the decision-
by-decision record:

- [x] Every one of sections 1–7 above has concrete, testable requirements —
      no item left open as "TBD."
- [x] The role → journey coverage table (§1) is complete for all 7 roles.
- [x] The IA crosswalk (§2) accounts for 100% of today's reachable tabs, each
      mapped to one specific new screen.
- [x] The per-screen primary-action table (§4) is complete for the
      Home/Tasks hub and all six Setup workflows.
- [x] The states matrix (§5) is filled in for every screen in the proposed
      IA — not left as an empty template.
- [x] The WCAG 2.2 AA conformance matrix (§7) covers every applicable A/AA
      success criterion, not a curated subset; the priority regression list
      is subordinate to it, not a substitute for it.
- [x] The eight product decisions below are resolved and signed off by
      @jingizoo.
- [x] Real operator validation is **scheduled** (owner, participants, tasks,
      evidence, and milestone below) — the sessions themselves run against
      the first implementation PR's prototype, per the stated milestone;
      "scheduled" is satisfied now, "conducted" is that PR's own gate.
- [x] No application code changes are included in this deliverable.

**Operator validation plan**: a moderated usability walkthrough of the
Home/Tasks + guided Setup hub prototype, run before that PR is considered
done — not deferred to "sometime before final rollout":

- **Owner**: @jingizoo (product owner), as the accountable party for
  commissioning and running the sessions — consistent with every other
  product decision in this doc requiring their sign-off.
- **Participants**: three sessions minimum, one per role — a League Admin,
  an Arena Manager, and a Coach (the two heaviest-permission roles plus the
  most common day-to-day operator), ~30–45 minutes each.
- **Moderated tasks** (concrete, not "walk through the hub"):
  - League Admin: starting from the Home/Tasks hub with no prompting, find
    and complete the next incomplete setup step.
  - Arena Manager: using Facilities, add a week of recurring ice via the
    Ice Availability Builder without consulting help text.
  - Coach: confirm the team's roster for the next game and identify any
    open slot, using only Home/Tasks + Schedule.
- **Evidence to capture per session**: task completion (yes/no), time-on-
  task, number of moderator interventions/hints needed, a post-task 1–5
  ease rating, and verbatim quotes on any confusion point. Recorded as
  session notes (recording optional, moderator's call).
- **Milestone**: sessions run, and their results documented, **before the
  first implementation PR (Home/Tasks + guided Setup hub) is merged** —
  this gates that PR's completion, not a later "final rollout" checkpoint,
  matching #204's own "documented before final rollout" line applied to the
  earliest point it can actually be tested.

**Measurable success criteria for the first implementation PR** (forward-
looking — the bar that PR must clear once it ships):

- A first-time League Admin can identify their next incomplete setup step
  without reading the Setup mega-page's raw data model (#204's own epic
  criterion, made concrete: fewer clicks/screens than today's single Setup
  page for the same task).
- Zero tab reachable today becomes unreachable in the new IA (per the §2
  crosswalk).
- Zero new automated accessibility violations (serious/critical) versus the
  pre-redesign baseline, measured by the new CI gate from §7.
- 100% of destructive actions in the redesigned areas use the named-resource
  confirmation pattern from §5 — no generic "Are you sure?" surfaces.
- The context bar's "display only · screens not filtered" caption is either
  removed (because the screen now filters) or narrowed to an explicit,
  justified exception list (§3) — it does not survive unchanged.
- Desktop and 390px journeys both pass for every new screen, per the
  existing delivery rule (`ROADMAP.md:266`).

## Product decisions requiring sign-off

**Signed off by @jingizoo (2026-07-24)**: decisions 1–7 accepted as written.
Decision 8 (#158 closure) resolved below — #158's own four acceptance
bullets (recurring weekly ice block + preview; exclusion dates honored and
conflicts reported, not silently created; a month view renders; zero
console errors desktop+phone with backend tests) are each satisfied by
#313's delivered scope, with no unmet item identified — #158 is closed as
functionally complete, citing #313 (closing #315) and #277's policy
integration (#318/#319). Decision 9 (workflow 6's completion contract) was
raised and resolved during #331's review, after implementation against
decisions 1–8 was already under way — see below.

Nine decisions this package resolves or proposes an answer for, gathered
here so they can be reviewed and checked off in one place rather than
hunting through each section. All nine now have a concrete, stated answer
— none is left as a bare open question:

1. **Users placement** (§2): Administration, not Teams & People — account/
   login lifecycle is a one-time administrative concern, distinct from
   day-to-day roster/team-membership management.
2. **League/Division context placement** (§3): League is promoted into the
   persistent context bar alongside Program/Season (structurally a peer of
   Season under Program, needed by most screens); Division stays
   screen-local (a narrower slice within an already-selected League+Season,
   needed by only a minority of screens).
3. **Context filtering** (§3): the Program/Season(/League) context bar
   becomes filtering-by-default for every screen; exceptions must be named
   and justified, never silent.
4. **Error granularity** (§5): per-card error boundaries for hub/dashboard-
   shaped screens (Home/Tasks, Setup workflow landings); whole-pane remains
   correct for single-purpose detail screens. Applied per screen in the §5
   states matrix.
5. **WCAG 2.2 conformance scope** (§7): **corrected** — conformance means
   every applicable Level A and AA success criterion (the full matrix in
   §7), not a curated subset. The originally-proposed seven-item list is
   retained as a priority regression list, not the scope itself. SC 2.4.12
   is Level AAA and is explicitly excluded from the AA target (listed only
   as a voluntary stretch goal).
6. **Operator validation** (§8): owner (@jingizoo), participants (League
   Admin, Arena Manager, Coach — 3 sessions), moderated tasks, evidence, and
   milestone (before the first implementation PR merges) are all specified
   in §8 — reviewed here for confirmation, not decided from scratch.
7. **Breakpoint token values** (§6): four exact tokens — `--bp-phone: 480px`,
   `--bp-tablet: 720px`, `--bp-nav-flip: 880px`, `--bp-wide: 1040px` —
   chosen by inspecting what each of today's eight ad hoc values actually
   does, not left as an abstract category.
8. **#158 status** (recurring ice templates/month view): **resolved —
   closed**. #313 (closing #315) delivered the builder mechanics; #313's
   own text explicitly held #158 open until #277's warm-up/resurfacing/
   curfew policy items landed. Those items are now merged (#318/#319).
   #158's own four acceptance bullets are each satisfied by #313's
   delivered scope (recurring weekly ice block + preview; exclusion dates
   honored and conflicts reported; a month view renders; zero console
   errors desktop+phone with backend tests) — no unmet item identified.
   Closed per @jingizoo's sign-off above, citing #313, #315, #318, #319.
9. **Workflow 6 ("Imports and onboarding") completion contract** (§2/§4):
   **resolved — always-reachable alternative, not a gated step.** The first
   Home/Tasks hub implementation (#330 PR #331) derived this workflow's
   done/todo state from whether workflows 1–5 were all done — an invented
   rule with no grounding, flagged in #331's review as inventing an
   undocumented completion signal and, as a side effect, making this
   workflow impossible to ever surface as the hub's `next` action. There is
   no reliable Program-scoped "has an import ever run here" signal to
   compute a real done/todo state from instead: two of the three import-
   commit paths (officials/availability, rinks/ice-slots) write only
   aggregate counts into their own audit summary row, no season- or
   program-derivable field. Resolved as a third status, distinct from both
   "done" and "todo" — this workflow is never a candidate for the hub's
   `next` recommendation and never blocks the hub's complete/success state,
   but stays fully visible and reachable as its own entry point the whole
   time (already true independent of the hub card, via the persistent
   Import nav tab both League Admin and Arena Manager hold). Signed off via
   @jingizoo's #331 review comment, 2026-07-25.

## Out of scope for this package

- Any implementation code — the first implementation PR (Home/Tasks + guided
  Setup hub) is separate and starts only after this package is validated.
- Schedule/Facilities UX — explicitly deferred to the PR after Home/Tasks +
  Setup hub, per the reordered plan.
- The bounded #287 pre-#205 deliverable — documentation/design plus a
  non-production UX prototype only (no schema, persistence, API
  mutations, notifications, or runtime state transitions; matches
  Release 2's "implementation must not land ahead of #205" bar exactly,
  not a narrower carve-out for eligibility logic specifically) —
  sequenced after Schedule/Facilities UX, before #206 resumes; the full
  production workflow (all six slices) stays gated on #205.
- Planner v2 (#206) — resumes after all of the above land.
- A native mobile app, rebranding, or new business features before existing
  workflows are understandable — already out of scope per #204 itself.

## Relationships

Child of epic #204 (issue #324). Blocks the first implementation PR
(Home/Tasks + guided Setup hub). The Schedule/Facilities UX PR follows that,
then the bounded #287 pre-#205 deliverable (documentation/design plus a
non-production UX prototype only — no schema, persistence, API mutations,
notifications, or runtime state transitions, matching Release 2's bar
exactly). #206 (Planner v2) resumes once all of the above land.
