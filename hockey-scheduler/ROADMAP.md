# Hockey Scheduler — Roadmap

Authoritative delivery roadmap. This file and issue
[#212](https://github.com/jingizoo/biknik/issues/212) are kept in sync; where
they differ, treat the newer of the two as correct and reconcile the other.

## Baselines

- **Current `main`:** `1e9171c` — #429 ownership/dependency inventory and pure
  preview contract, merged via PR #448 (after #428 via PR #447).
- **Previously recorded here:** `5aa84da` (#404 database-connection recovery,
  PR #406), 377 commits stale when reconciled on 2026-09-02.
- **Older recorded hierarchy baseline:** `bf85403` (Epic #283 competition-hierarchy
  reset, Slices A–E, via PR #284). That baseline was **428 commits stale** when
  this file was reconciled on 2026-08-07; the hierarchy sections below still
  describe the model #283 established, which is current.
- **Historical expert-review baseline:** `8cbe003` — the 16 July 2026 hands-on
  review of `main`. Findings from that review still drive Releases 0–4 below,
  but the code baseline they were written against has since advanced: read the
  hierarchy sections against the **current model**, not the review-era one.

## Current data model (permanent hierarchy)

Epic #283 replaced the season-scoped `Program → Season → League` shape with a
**permanent competition spine and a seasonal overlay**:

```text
Program                                  (participant category — Adult Men, Adult Women)
  ├── League              permanent      (League.program_id NOT NULL — the hard competition boundary: Platinum, Bronze)
  │     └── Team          permanent      (Team.league_id NOT NULL — a Team belongs to exactly one League)
  │
  └── Season                             (Season.program_id NOT NULL — Winter 2026, Summer 2027)
        └── LeagueSeason                 (League × Season; UNIQUE(league_id, season_id))
              ├── Division   optional     (Division.league_season_id NOT NULL — North/South, season-specific)
              └── SeasonTeamRegistration  (team_id, league_season_id, division_id NULL; UNIQUE(team_id, league_season_id))
```

Enforced invariants (service layer + DB, both stores + PostgreSQL):

```text
team.league_id            = league_season.league_id
league.program_id         = season.program_id
division.league_season_id = registration.league_season_id
```

- Regular Games reference `league_season_id`; both registrations must belong to
  it. Cross-Program and cross-League **regular** Games are rejected.
- **Exhibition** Games may cross Leagues within one Season and never affect
  standings.
- Moving a Team between Leagues (promotion/relegation/transfer) preserves prior
  Seasons' registrations, Games, results, and standings byte-for-byte.

See `docs/architecture/data-model.md` and ADR
`docs/architecture/decisions/0001-competition-model-reset.md`.

## Completed foundations (do not re-open)

- **#233** — first competition-model reset (Program/Season/League, permanent
  Program Teams, `SeasonTeamRegistration`, `SeasonVenueAccess`). Superseded in
  shape by #283 but its slices (imports, scheduler alignment, deletion safety)
  remain in force.
- **#283 / PR #284** — permanent `Program → League → Team` with the
  `LeagueSeason` overlay; migrations 034–037 with halt-and-report gates;
  LeagueSeason game invariants, Exhibition type, and LeagueSeason standings;
  imports/rollover/Records/history hardening. **Complete on `main`.**
- **#266 / PR #282** — Coach (and Player) account scope fails closed;
  strict account payload schema; concurrency-safe bind/rebind; audited
  remediation route. **Complete.**
- **#256 / PR #264 + #265** — guarded production factory-reset Danger Zone
  (preview, challenge token, typed phrase, atomic wipe + durable event,
  PostgreSQL rollback). **Complete.**
- **#267 / PR #286** — login throttling by source IP and normalized username
  (atomic reserve-slot; fixed-size hashed keys; amortized sweep + hard
  cardinality cap with sweep-then-fail-closed admission; clamped, non-finite-safe
  config), production minimum-password policy, generic 429/`Retry-After` with no
  username oracle, and a safe aggregate lockout audit. **Complete on `main`.**

Completed issues are removed from the pending tracks below.

---

## Product baseline to protect (non-regression in every phase)

- Goalie and skater roster slots remain separate end to end.
- Player backout immediately produces the correct open-slot/substitute decision;
  removed roster rows are revived, not duplicated; roster locks stay authoritative.
- Destructive setup uses dependency-gated, itemized blockers — no silent cascades.
- Audit actors are always resolved from the authenticated server session.
- Guardian authority exists only through verified consent links.
- Public payloads are publish-gated and contain no junior Player/guardian/
  contact/medical data by default.
- Schedule generation stays draft/review/commit with deterministic output and
  machine-readable failure reasons.
- Onboarding stays resumable and recomputed from real records.
- The permanent hierarchy invariants above hold on every write path, and no
  transfer or rollover rewrites historical registrations, Games, or standings.

---

## Release 0 — production security gate

**Do not add more production users until this gate passes.**

**Status — verified against GitHub on 2026-08-07:** every numbered item below
is closed except the **#202** half of item 3. #266 and #267 are closed too.

1. **#160** — **CLOSED.** Canonical Player account scope: private-Game reads
   work from `player_id`, with `team_id` derived or guaranteed.
2. **#271** — **CLOSED.** Strict write schemas: unknown-field rejection,
   malformed-JSON handling, and JSON `405` responses with `Allow`.
3. **Bounded #201/#202** — **#201 CLOSED; #202 still OPEN.** Only the
   transaction, concurrency, authorization, and route-contract work required
   to close the paths above. The remaining work is #202's route-contract and
   authorization half.

Whether the exit gate below is now *met* is a product-owner determination, not
one this file makes: the items are closed, and the gate is a behavioural claim
about the deployed system.

**Exit gate:** credential guessing is throttled without a username oracle; weak
new credentials are rejected; Player/Coach scope fails closed; unknown fields and
malformed JSON never reach business logic; known unsupported methods return JSON
`405` with `Allow`; Memory/SQLite/PostgreSQL/HTTP security matrices pass.

## Release 1 — correctable Player records

**Status — verified against GitHub on 2026-08-07: COMPLETE.** All four items
are closed (#269, #268, #270, #272). The list and exit gate are kept because
the exit gate remains the standing non-regression contract, not because work
is outstanding.

1. **#269** — jersey range `1..98` and active-Team uniqueness on
   create/edit/import/reactivate/reassignment, backed by DB constraints.
2. **#268** — audited Player edit workflow and operator drawer (reuses #269's
   jersey validation).
3. **#270** — deactivate/reactivate lifecycle; retain history instead of
   deleting departures/injuries (reuses jersey + eligibility checks).
4. **#272** — date-only Season boundaries with explicit timezone semantics.

**Exit gate:** name, position, jersey, shooting hand and email are correctable
without deleting history; duplicate active jerseys cannot pass any
service/import/reassignment/concurrent-SQL path; deactivate/reactivate preserves
old Games and roster rows; Season date-only values behave consistently across
manual setup and imports.

## Release 2 — athlete identity and seasonal rosters

Ship as ordered slices; **do not** land #205 as one large migration PR.

1. **#159** — explicit Program/Season context and archived read-only history
   (realigned to the permanent hierarchy: context is `Program → League` plus the
   selected `Season → LeagueSeason`).
2. **Bounded #124** — field-level visibility, sensitive-read audit, birthdate and
   registration-number rules.
3. **#273** — durable athlete identity (names, private birthdate, governing-body
   registration number, shooting hand, age tiers, duplicate detection).
4. **#205** — `SeasonRosterMembership` schema and migration, built **on top of**
   the permanent `Team.league_id` + `LeagueSeason` spine (an athlete's seasonal
   membership hangs off the Team's `LeagueSeason`, not a re-created League).
5. Cut roster, substitutes, imports, accounts and notifications over to seasonal
   membership.
6. **#287** — substitute matching engine (after #205, since eligibility resolves
   through the Game's `LeagueSeason` and the player's seasonal membership).
   Deterministic, explainable, League-configurable matching (fairness by fewest
   completed sub games, 1–7 skill proximity, notice-window exclusion, position
   preference with goalie strictly separate, random tiebreaker, authorized
   override) plus an offer → accept/decline/timeout → next-candidate workflow.
   Six bounded slices: (1) preferences + League policy config; (2) eligibility,
   ranking, explainable selection; (3) offer/accept/decline/timeout state
   machine; (4) notifications + auto next-candidate retry; (5) captain/manager
   override with authz + audit; (6) operator UI + e2e. **The #205
   LeagueSeason/membership eligibility boundary required by #287 is now on
   `main`, while #205 remains open for its remaining epic work. The repository
   owner settled #287's five design questions on 2026-08-29 in #435. On
   **2026-09-04** the owner separately authorized one bounded runtime slice:
   proactive availability for an explicit target whose active registration
   shares the source membership's exact `LeagueSeason` and same non-null
   `Division`, with the source team outside both Game sides. That slice uses
   target-owned provenance, one active row per player/Game, and the existing
   offer lifecycle with a server-owned 30-minute deadline. It does **not**
   complete ranking, policy configuration, automatic advancement, or
   cross-Division substitution; those remain in the bounded sequence above.**
7. **#276** — privacy-minimized Coach Team directory.
8. **#275** — Guardian invite-by-email, activation and consent acceptance.
9. **#278** — C/LW/RW/D/G positions, game lines/pairs/goalie designation, and
   governed affiliate call-ups.
10. **#280** — emergency contacts and narrowly scoped medical/safety alerts.
11. **#190** — Team staff records, certifications, screening and expiry (after
    scoped identity + sensitive-data governance exist).

**Exit gate:** one athlete can represent different Teams in different Seasons
without altering old Games; birthdate/governing-body ids stay private with
audited sensitive reads; jerseys/positions/eligibility become Season-specific;
Guardian links survive migration; current roster eligibility resolves through the
Game's `LeagueSeason` and seasonal athlete membership; substitute matching is
deterministic, explainable, and League-configurable, resolving eligibility
through the Game's `LeagueSeason` and seasonal membership.

## Release 3 — hockey Game operations

Dependency chain:

```text
#31 outcome and points rules
  → #156 authoritative event-level Game Sheet
  → #279 Officials/timekeeper sign-off and fees
  → #274 discipline and suspension enforcement
  → #157 Player and goalie statistics
  → #34 public results, standings and playoff surfaces
```

**Exit gate:** final score, Game Sheet, standings and statistics cannot disagree;
suspended athletes cannot enter a lineup through any path; corrections are
versioned, authorized and recomputed exactly once; public outputs stay
publish-gated and privacy-safe.

## Release 4 — rink-real scheduling and operator UX

1. **#277** — warm-up, resurfacing, turnover and curfew rules shared by manual
   moves and generated schedules. **All six acceptance criteria satisfied**
   (PR #318, closing bounded child #320; PR #319, closing bounded child
   #321; both merged) — see #277 for the criterion-by-criterion evidence.
   **#277 is now CLOSED** (2026-07-24). The text here previously said it
   "remains open" and described closing it as a pending product-owner
   decision — that decision has since been taken. Merging those PRs closed
   their own bounded children; the parent was closed separately.
2. **#158** — recurring ice templates, conflict preview and month view.
   **Done and closed** (#313, closing #315, plus #277's policy integration
   via #318/#319 — closed per @jingizoo's sign-off on the #204 requirements
   package, #324, confirming all four of #158's acceptance bullets are met).
3. **#204** — task-oriented operator UX, accessibility and design consistency,
   built on the permanent-hierarchy context and the new Player/Game workflows.
   **Reordered ahead of #206** (plan update): the bounded requirements
   package (#324) landed first — role-specific operator journeys,
   task-oriented IA, persistent Program/Season/League context, one primary
   action per screen, loading/empty/stale/error/retry/confirmation states,
   desktop + 390px behavior, a full WCAG 2.2 AA conformance matrix, and
   operator validation + measurable success criteria. **Scope split approved
   by @jingizoo (2026-07-27):** #330 / PR #331 is the bounded first
   implementation slice, delivering the Program-scoped setup-progress
   contract and Home/Tasks card, deep-links into existing Setup screens, and
   role-home regression coverage. It does not complete the guided Setup/IA
   milestone. #345 carries the remaining guided Setup, IA, context-filtering,
   state/error, breakpoint, accessibility, and operator-validation acceptance
   work. #345 remains next; Schedule/Facilities UX follows it.
4. **#287** — substitute matching engine, **bounded pre-full-#205 work**.
   **Owner update (2026-09-04):** this authorization supersedes the older
   prototype-only restriction for one narrow production slice. The authorized
   slice lets a Player use proactive Home checkboxes to volunteer for an
   explicit target side before a vacancy exists, only where the player's
   active source membership and target registration share the exact
   `LeagueSeason` and same non-null `Division`, and the source team is neither
   Game side. The selected target durably owns the row and Coach offer/seating
   actions; the Player or verified Guardian owns accept/decline, while a Coach
   seats only through the explicit audited override. The exact source
   membership/team is retained as private provenance. One active
   (`ENROLLED`/`OFFERED`) row is allowed per player/Game, terminal history is
   retained, and a cross-team offer uses the server-owned
   `min(offered_at + 30 minutes, game_start)` deadline with expiry winning at
   equality, an expiry audit, and explicit Player dismissal. Omitted-target
   same-team behavior is unchanged.

   The earlier plan allowed only documentation/design and a non-production UX
   prototype at this position. That blanket limitation is retired **only for
   the bounded slice above**. It remains the scope boundary for everything not
   expressly authorized: no claim that the deterministic ranking engine,
   fairness or skill rules, configurable policy, automatic next-candidate
   workflow, notifications, or cross-Division/cross-LeagueSeason borrowing is
   implemented. Full production integration still proceeds through the six
   Release 2 slices and their own authorization/evidence.
5. **#206** — planner scenarios, fairness, locks, repair and explanations
   (realigned: schedule within a `LeagueSeason`/Division; regular Games never
   cross Leagues). Resumes after #204's #330 / PR #331 bounded slice, #345,
   the Schedule/Facilities UX slice, and the bounded #287 substitute PR land.
6. **#146** — remaining bounded UX polish where it does not conflict with #204.

**Exit gate:** a DB-valid schedule is physically operable at the rink; operators
compare/repair scenarios without regenerating published Games; desktop and 390px
journeys pass accessibility and zero-console-error checks.

---

## Continuous hardening track (bounded children on active feature PRs)

Deliver as bounded children attached to the feature PRs above — not oversized
rewrites:

- **#201** — constraints, transaction parity, concurrency.
- **#202** — declarative routes, authorization, contracts.
- **#203** — production runtime, observability, background jobs.
- **#207** — reliable multi-worker notifications.
- **#208** — release-quality gates (lint/type/coverage/a11y/visual/upgrade/load).
- **#209** — current architecture, API and operational documentation reset
  (must describe the permanent hierarchy, not the superseded season-scoped one).
- **#211** — invite/reset/MFA and account lifecycle (after #266/#267).
- **#155** — notification confirmation/preview before bulk processing.
- **#210** — reporting, audit search and exports (after authoritative schedule,
  Game Sheet and statistics data exist).

A release is production-ready only when its migrations, rollback, authorization,
privacy, observability and acceptance journeys pass on PostgreSQL.

## Later operational and growth modules

Retain, implement only after their prerequisites: #185 (live Venue/Rink status),
#189 (recurring practices/multi-day events), #186 (targeted announcements, after
#203/#207/#155), #187 (online registration/payments, after
#205/#273/#124/#201/#202/#207), #188 (public league website, after
#34/#185/#186/#187/#189), #27 (native SwiftUI client — P3, after the API,
identity, Game Sheet and context contracts stabilize).

---

## Currently active sequencing

The single static queue previously here drifted out of sync with actual
execution — it never listed #277, #158, or #204, all of which were the
Release-4 items actually being built, and still asserted #160 as the sole
"NEXT" item after those had already landed. Removed rather than patched
again: cross-Release execution order is directed by the product owner per
work session, not a static file that goes stale between sessions. The
authoritative **per-Release** order remains the numbered lists in
Releases 0–4 above.

**Currently active** (Release 4, UX-first per plan update): #204
requirements package (#324) → bounded Home/Tasks first slice (#330 /
PR #331) → guided Setup, seven-area IA, accessibility, and operator
validation completion (#345) → Schedule/Facilities UX PR → bounded #287
September 4 runtime slice (same-`LeagueSeason`, same non-null `Division`,
explicit-target proactive availability only; no full ranking or
cross-Division policy) → #206 resumes. The broader #287 engine retains the
Release 2 order and authorization gates.

Owner-directed addition (2026-09-02): #428 landed through PR #447. #429's
complete ownership/dependency inventory and pure preview contract then landed
through PR #448. **#429's separate execution slice is now active**: add the
high-privilege, token-bound, atomic destructive command, survivor audit, and
desktop/390px confirmation flow. This does not widen any ordinary
dependency-gated delete endpoint.

Owner-directed split (2026-07-27): PR #331 may merge as the technically
clean first slice. #345 is the immediate next critical deliverable and
retains every unfinished guided Setup/IA/accessibility/responsive/state/
role-journey criterion plus the three moderated operator-validation
sessions. No Schedule/Facilities, #287, or #206 work advances ahead of
#345. The owner-directed 2026-09-04 #287 authorization above supersedes that
older no-#287 clause only for its named same-Division availability slice.

Owner-directed parallel exception (2026-08-02): while #375 implements only
configurable regular-season meeting counts and deterministic home/away balance,
bounded #206 children may independently advance named scenario persistence and
material-input stale commit refusal (#378), plus preview explainability. These
exceptions do not reorder or replace #375/#345, and their contracts must not
overlap: scenario persistence treats generated explanation payloads as opaque,
and neither parallel slice owns meeting-count or home/away generation behavior.

Clarified after #375 merged as PR #382: "does not own" means does not decide.
A named scenario still **records and replays** the regular-season format it
was generated under, because a scenario that re-derived the format at commit
would commit a different-sized schedule from the one reviewed. The value is
#375's to compute; it is #378's to preserve.

Updated when #375 inverted the control: the operator-facing field is now
`games_per_team` (guaranteed games each team plays), from which the
per-opponent count is derived. `meetings_per_opponent` remains accepted as
the legacy spelling precisely so scenarios stored under it keep replaying;
both are recorded and replayed by the same rule above.

**Done since this section was last accurate** — every state below verified
against GitHub on 2026-08-07, not carried forward from the previous text:

- #267 (login security, PR #286); #277 (turnover/curfew policy, PR #318/#319);
  #313/#315 (recurring ice templates + month view, which closed #158).
- **#160, #271 and #201 are closed** — the implementation issues behind
  Release 0's security gate. Whether that behavioural gate is *met* on the
  deployed system is a separate, unresolved product-owner determination; see
  Release 0.
- **Release 1 is COMPLETE**: #269, #268, #270, #272 all closed.
- #324 and #330 (the #204 requirements package and the bounded Home/Tasks
  first slice, PR #331) are closed.
- #375 and #378 are closed.
- Operational reliability landed after those: #399 (an exhibition blocking ice
  is now nameable in a preview explanation, PR #402); #302's delivery
  mechanism — every request now gets a structured answer instead of a dropped
  connection, PR #403; and #404 (database-connection recovery plus a health
  status that can report ill-health, PR #406).

> **Correction.** The previous version of this paragraph asserted that
> "#269/#268/#270/#272/#159/#124/#273/#205 remain queued". Six of those eight
> were already closed. The genuinely queued Release 2 items are **#159, #124,
> #273 and #205** — their relative priority against the active Release 4
> thread is a product-owner call to make explicitly when that work resumes,
> not implied by this file.

**Still open and genuinely queued**, for the avoidance of the same drift:
#202 (Release 0 remainder), #159/#124/#273/#205 (Release 2), #287, #429, #206,
#146, and the epics #203/#207/#208/#209/#210/#211/#212.

> **Needs an owner ruling — not decided here.** The 2026-08-02 parallel
> exception above is written in the present tense about #375 ("while #375
> implements only…") and names #378, and **both are now closed**. Whether that
> exception lapsed with them or still authorises bounded #206 preview-
> explainability children is a product-owner determination. It is left exactly
> as written until ruled on, because it is the clause that authorised #399 and
> misreading it once already produced a false authorization claim on PR #402.

## Delivery rules

- One bounded PR slice at a time; no unrelated cleanup bundled into a defect fix.
- Migrations are forward-only, preflight existing conflicts, and preserve
  historical identifiers/data.
- Every mutation is authorized, transactional, server-attributed and audited.
- DB constraints back invariants that concurrent requests could bypass.
- Sensitive reads and public payloads get explicit privacy tests.
- UI work includes desktop, 390px, keyboard/accessibility and
  zero-console-error evidence.
- Every PR states its acceptance journey, rollback/migration behavior, privacy
  impact and the issue it closes.
- An epic closes only when its production acceptance journey passes, not when
  backend classes merely exist.
