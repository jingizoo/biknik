# Hockey Scheduler — Roadmap

Authoritative delivery roadmap. This file and issue
[#212](https://github.com/jingizoo/biknik/issues/212) are kept in sync; where
they differ, treat the newer of the two as correct and reconcile the other.

## Baselines

- **Current `main`:** `bf85403` — Epic #283 (competition-hierarchy reset,
  Slices A–E) merged via PR #284.
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

**Do not add more production users until this gate passes.** (#266 is done.)

1. **#267** — login throttling by IP and normalized username, safe backoff,
   generic errors, and a minimum password policy.
2. **#160** — canonical Player account scope: private-Game reads work from
   `player_id`, with `team_id` derived or guaranteed (they still depend on an
   optional `scope.team_id`).
3. **#271** — strict write schemas: unknown-field rejection, malformed-JSON
   handling, and JSON `405` responses with `Allow`.
4. **Bounded #201/#202** — only the transaction, concurrency, authorization, and
   route-contract work required to close the paths above.

**Exit gate:** credential guessing is throttled without a username oracle; weak
new credentials are rejected; Player/Coach scope fails closed; unknown fields and
malformed JSON never reach business logic; known unsupported methods return JSON
`405` with `Allow`; Memory/SQLite/PostgreSQL/HTTP security matrices pass.

## Release 1 — correctable Player records

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
6. **#276** — privacy-minimized Coach Team directory.
7. **#275** — Guardian invite-by-email, activation and consent acceptance.
8. **#278** — C/LW/RW/D/G positions, game lines/pairs/goalie designation, and
   governed affiliate call-ups.
9. **#280** — emergency contacts and narrowly scoped medical/safety alerts.
10. **#190** — Team staff records, certifications, screening and expiry (after
    scoped identity + sensitive-data governance exist).

**Exit gate:** one athlete can represent different Teams in different Seasons
without altering old Games; birthdate/governing-body ids stay private with
audited sensitive reads; jerseys/positions/eligibility become Season-specific;
Guardian links survive migration; current roster eligibility resolves through the
Game's `LeagueSeason` and seasonal athlete membership.

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
   moves and generated schedules.
2. **#158** — recurring ice templates, conflict preview and month view.
3. **#206** — planner scenarios, fairness, locks, repair and explanations
   (realigned: schedule within a `LeagueSeason`/Division; regular Games never
   cross Leagues).
4. **#204** — task-oriented operator UX, accessibility and design consistency,
   built on the permanent-hierarchy context and the new Player/Game workflows.
5. **#146** — remaining bounded UX polish where it does not conflict with #204.

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

## Exact next implementation queue

```text
1. Roadmap reset — this file + issue #212 (documentation only; supersedes PR #281)
2. #267 — login security
3. #160 — Player private-read scope
4. #271 — strict write/API contracts
5. #269 — jersey constraints
6. #268 — Player edit workflow
7. #270 — Player deactivate/reactivate
8. #272 — Season date-only boundaries
9. #159 / #124 / #273 — design and privacy foundation
10. #205 — seasonal athlete membership (on the permanent Team→League spine)
```

Production security first; Player invariants before the #205 migration; no new
broad model change before its privacy and history rules are locked.

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
