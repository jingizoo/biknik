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

**Requirement**: before the first implementation PR starts, Player, Guardian,
and Official each get a real e2e journey (even a thin one) — three of seven
roles currently have zero scripted acceptance coverage, and a redesign that
only re-validates the four already-covered roles would silently ship
regressions to the other three.

### 2. Task-oriented navigation and setup

**IA crosswalk** — every currently-reachable tab must land somewhere in the
new 7-area IA #204 already proposed (Home/Tasks, Schedule, Teams & People,
Facilities, Communications, Reports, Administration). Nothing today's five
groups can reach may become unreachable:

| Today's group (#145) | Today's tabs | Proposed new area |
| --- | --- | --- |
| Home | Dashboard, Home (player), My Players (guardian), My Assignments (official), Activity | **Home/Tasks** — becomes the task-oriented landing hub (§ below), not just a dashboard |
| Schedule | Arena Calendar, Games, Scheduler, Standings, Game Sheet, Public | **Schedule** — unchanged grouping, refined states/actions only |
| People | Roster, Users | **Teams & People** — renamed/expanded per #204; Users (account admin) may belong under Administration instead — **flagged as an open placement question**, not decided by this package |
| Operations | Notifications, Delivery, Pilot Readiness, Import | Splits: Notifications/Delivery → **Communications**; Pilot Readiness → **Reports**; Import → folds into the Setup hub's "Imports and onboarding" workflow (§ below) |
| Admin Setup | Initial Setup (onboarding), Setup | Setup's six sub-workflows split across **Facilities** (venues/rinks/ice) and **Administration** (league profile/seasons, permanent teams, season participation/divisions, clubs/players/staff, imports/onboarding) |

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
- **Requirement**: League and Division selection — currently local to each
  screen (e.g. the Scheduler's own Division picker) — get an explicit
  decision: promoted into the persistent context bar alongside Program/
  Season, or intentionally left screen-local with a stated reason (e.g.
  "Division is a Scheduler-only refinement of the Season already selected
  above, not a global axis"). Undecided today; must not stay undecided after
  this package is signed off.
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

**Requirement**: before the first implementation PR, produce a per-screen
table (screen → today's competing actions → the one designated primary
action → what every other action becomes) for at least the Setup mega-page
(explicitly named in #204 as the current offender) and the Home/Tasks hub.
This table is a required artifact of *this* package, not deferred to the
implementation PR, so that PR has a checklist rather than a design decision
to make mid-implementation.

### 5. Loading, empty, stale, error, retry, and confirmation states

Today's de facto conventions become required conventions, extended to every
new screen the redesign introduces:

| State | Required pattern (today's baseline → what's required going forward) |
| --- | --- |
| Loading | Skeleton placeholder before first paint (as today, `app.js:5441`) — every new hub/screen gets one; no blank white flash. |
| Empty | The `.empty` + `pageIntro` convention, with the full #311 recipe as the bar: name the real count/scope, explain the cause in operator vocabulary, give an in-place remediation link, disable any action that would operate on the empty set. A bare "No data" is a regression, not an acceptable empty state. |
| Stale | The existing "stale-response guard" pattern (discard out-of-date async results) extends to every new async view — required, not optional, for any screen with more than one in-flight request source (e.g. context switch mid-load). |
| Error | **Decision needed** (see Product decisions below): today is whole-pane-only (one banner replaces all content). The new hub/multi-card screens must state, per screen, whether a single card's failure blanks the whole screen or degrades just that card — this package requires the decision be made and stated per screen, not left as an accident of implementation order. |
| Retry | An explicit retry affordance at every failure point, matching today's granularity options (page-level `#retry-btn`, row-level retry as in dead-letter notification rows) — no error state without a next action. |
| Confirmation | Every destructive action (delete/reset/cleanup) uses a modal that names the specific resource being affected — never a generic "Are you sure?" — matching the existing `destructive-surfaces`/`safe-destructive`/`factory-reset`/`records-delete`/`division-delete-cleanup`/`registration-cleanup`/`venue-access-cleanup` journeys. Every new destructive action introduced by the redesign must have an equivalent e2e journey before it ships. |

**Requirement**: produce a states matrix — rows are every screen in the
proposed 7-area IA, columns are {loading, empty, stale, error, retry,
confirm} — filled in (not left blank) as part of this package's validation,
so the implementation PR is executing a checklist, not improvising per
screen.

### 6. Desktop and 390px behavior

- **Requirement**: `390×844` remains the canonical phone test viewport
  (already used in 10+ specs) — do not introduce a second phone size.
  Desktop stays whatever each existing spec already uses (commonly
  `1280×800`).
- **Requirement**: consolidate today's eight ad hoc breakpoint values
  (460/480/520/680/720/760/880/1040) into a small set of named tokens as
  part of the design-system deliverable #204 already scopes (e.g. a phone
  breakpoint, the 880px structural nav-flip, and at most one intermediate
  tablet-ish breakpoint) — new hub CSS must not add a ninth magic number.
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

Target is **WCAG 2.2 AA** specifically (not 2.0 or 2.1) — the following are
concrete, testable requirements closing today's real, named gaps rather than
citing "AA" as a slogan:

- **Keyboard**: every interactive control in the new IA must be reachable by
  Tab/Shift+Tab in DOM/visual order (SC 2.1.1, 2.4.3). The existing global
  Escape-closes and Enter/Space-activates-`role="button"` handlers must
  extend to every new dialog/menu/hub surface — an audit is required because
  the redesign adds dialog-shaped surfaces beyond today's two (`.modal`,
  `.drawer`).
- **Focus**: every surface marked `aria-modal="true"` must implement a real
  focus trap — Tab/Shift+Tab cycles only within the dialog, initial focus
  lands on the first control or a heading, and focus returns to the
  triggering element on close (SC 2.4.3, 2.4.11 focus-not-obscured). This is
  a **currently-failing requirement**: neither existing dialog shape does
  this today (the drawer sets initial focus only; nothing traps or returns
  it). New dialogs in the redesign must not repeat the gap.
- **Labeling**: unify today's two divergent conventions behind one explicit
  rule — prefer a real, bound `<label for="...">` wherever the control's
  markup is static enough to support it; reserve `aria-label` for genuinely
  dynamic/JS-templated or icon-only controls (SC 1.3.1, 4.1.2). State this
  rule in the design system so it is a decision, not an accident of which
  screen a given control happened to ship on first.
- **Screen-reader**: extend the existing `aria-live="polite"` toast pattern
  to any new async confirmation surface; empty/error copy must remain
  conveyed through readable text, never through icon or color alone
  (SC 1.4.1); every icon-only control keeps the existing `title` +
  `aria-label` pairing convention.
- **Target size** (new in 2.2, SC 2.5.8): icon buttons already grow to 40×40
  at the 480px phone breakpoint (`styles.css:913`) — the redesign must carry
  this forward to every new touch target, including any newly introduced
  icon-only controls in the Home/Tasks hub and Setup workflows.
- **Automated gate**: add an automated accessibility check (e.g. axe-core)
  to CI. `ROADMAP.md:217` (#208) already anticipates this; this package
  treats it as one of *its own* closeable acceptance items (see §8), not an
  indefinitely deferred aspiration.
- **Manual acceptance**: automated checks catch violations, not usability —
  a manual keyboard-only and screen-reader pass on the Home/Tasks hub and
  guided Setup hub is required before that PR is considered done, per
  #204's own "manual keyboard/screen-reader acceptance" line.

### 8. Operator validation and measurable success criteria

**This package's own acceptance criteria** (narrower than epic #204's, which
cover the eventual shipped redesign — these gate *this document*):

- [ ] Every one of sections 1–7 above has concrete, testable requirements —
      no item left open as "TBD."
- [ ] The role → journey coverage table (§1) is complete for all 7 roles.
- [ ] The IA crosswalk (§2) accounts for 100% of today's reachable tabs.
- [ ] The per-screen primary-action table (§4) exists for at least the
      Setup mega-page and the Home/Tasks hub.
- [ ] The states matrix (§5) is filled in for every screen in the proposed
      IA — not left as an empty template.
- [ ] The WCAG 2.2 AA checklist (§7) names specific success-criterion
      numbers, not just "AA."
- [ ] The five product decisions below are resolved and signed off by
      @jingizoo.
- [ ] Real operator validation is scheduled — who and by what method — not
      deferred indefinitely (see below).
- [ ] No application code changes are included in this deliverable.

**Operator validation plan** (proposed, for sign-off): a moderated
walkthrough of the Home/Tasks + guided Setup hub prototype, once built,
against this package's requirements — with at minimum a League Admin and an
Arena Manager (the two heaviest-permission roles) and a Coach (the most
common day-to-day operator), matching #204's own "user testing with
representative league/arena operators is documented before final rollout"
line. Exact participants/scheduling are a sign-off item, not decided here.

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

Five open decisions this package proposes an answer for, gathered here so
they can be reviewed and checked off in one place rather than hunting
through each section:

1. **Context filtering** (§3): does the Program/Season context bar become
   filtering-by-default for every screen, or does it keep an explicit,
   documented exception list? *Proposed: filtering-by-default; exceptions
   must be named, not silent.*
2. **Error granularity** (§5): whole-pane (today's behavior) or per-card
   error boundaries for the new multi-card hub screens? *Proposed:
   per-card for hub/dashboard-shaped screens (Home/Tasks, Setup hub
   landings); whole-pane remains correct for single-purpose detail screens.*
3. **WCAG 2.2 success criteria in scope** (§7): are the named SCs (2.1.1,
   2.4.3, 2.4.11, 1.3.1, 4.1.2, 1.4.1, 2.5.8) the complete relevant set, or
   does review add/remove any? *Proposed: as listed in §7.*
4. **Operator validation participants/method** (§8): who and how? *Proposed:
   a moderated walkthrough with a League Admin, an Arena Manager, and a
   Coach.*
5. **Breakpoint token values** (§6): what are the new named breakpoints,
   replacing today's eight ad hoc values? *Proposed: one phone breakpoint
   (consolidating 460/480/520), the existing 880px structural nav-flip kept
   as-is, and at most one intermediate/tablet breakpoint (consolidating
   680/720/760/1040) — exact values are a sign-off item, not fixed here.*

## Out of scope for this package

- Any implementation code — the first implementation PR (Home/Tasks + guided
  Setup hub) is separate and starts only after this package is validated.
- Schedule/Facilities UX — explicitly deferred to the PR after Home/Tasks +
  Setup hub, per the reordered plan.
- Planner v2 (#206) — resumes after both of the above land.
- A native mobile app, rebranding, or new business features before existing
  workflows are understandable — already out of scope per #204 itself.

## Relationships

Child of epic #204 (issue #324). Blocks the first implementation PR
(Home/Tasks + guided Setup hub). The Schedule/Facilities UX PR follows that.
#206 (Planner v2) resumes once both land.
