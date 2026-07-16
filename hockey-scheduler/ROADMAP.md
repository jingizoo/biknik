# Hockey Scheduler roadmap

**Authoritative planning issue:** #212  
**Review baseline:** `main` at `8cbe003`  
**Review date:** 16 July 2026

This roadmap incorporates the hands-on expert application review performed from an ice-hockey coach and league-administrator perspective. It replaces the old feature-first/first-slice ordering.

The product already has a strong league-operations foundation. The next releases must fix the two account-security defects, make Player records correctable and hockey-valid, then add athlete eligibility, real Game Sheet/discipline data, and rink-real scheduling. Native and growth work remains behind those gates.

## Baseline to protect

Every change must preserve these proven behaviors:

- Goalie and skater roster slots are counted separately.
- A Player backout immediately produces the correct open-slot/substitute decision.
- Removed roster rows are revived rather than duplicated; roster locks remain authoritative.
- Setup deletes are dependency-gated with itemized blockers; no silent cascades.
- Audit actors are server-resolved from the authenticated session.
- Guardian authority exists only through verified consent links and linked-junior routes.
- Public payloads are publish-gated and contain no junior Player, Guardian, contact, medical, or restricted data by default.
- Scheduling remains deterministic draft/review/commit with machine-readable failure reasons.
- Onboarding remains resumable, record-derived, and explicit about the next blocker.

## Release sequence

### Release 0 — rollout security gate

Do not add more production accounts until this gate passes.

1. #266 — fail closed on missing/invalid Coach Team scope and remediate existing unscoped Coaches.
2. #267 — throttle login attempts and enforce a minimum credential policy.
3. #271 — reject unknown/malformed write bodies and return consistent JSON errors on critical current routes.
4. #160 — make the Player account scope contract consistent for private Game reads.
5. Complete the bounded #202/#201 authorization, contract, and concurrency work required by those fixes.

**Exit gate**

- An unscoped/malformed Coach has no roster or private-Player authority.
- Credential guessing is throttled without a username oracle.
- Write-body typos fail before business logic and write nothing.
- Private reads and mutations have a tested role/resource matrix on Memory, SQLite, and PostgreSQL.

### Release 1 — correctable, rule-clean Player records

1. #268 — audited Player edit workflow and operator UI.
2. #269 — jersey range `1..98` and active-Team uniqueness on create, edit, import, reactivation, and reassignment, backed by database constraints.
3. #270 — deactivate/reactivate lifecycle while preserving all history.
4. #271 — finish validation, version/deprecation, method/error, and venue-access remediation behavior.
5. #272 — accept date-only Season boundaries with explicit timezone semantics.

**Exit gate**

- Operators correct Player data without delete/recreate.
- Inactive Players cannot enter new roster/substitute flows, but historical records remain intact.
- Jersey conflicts cannot pass through service, import, reassignment, or concurrent database writes.
- Setup errors identify the field and corrective action.

### Release 2 — athlete identity, seasonal rosters, and junior safety

#### Model and privacy contract

1. #159 — explicit Program/Season context, archive, and read-only history.
2. #205 — athlete identity plus Season roster membership, transfers/releases, deadlines, and eligibility history.
3. #124 — sensitive-field visibility, read auditing, export/delete, and retention.
4. #273 — first/last/preferred name, private birthdate, governing-body registration id, shooting hand, structured age tiers, and duplicate detection.

#### Operational people workflows

5. #276 — privacy-minimized Team Player directory for scoped Coaches.
6. #275 — Guardian invite/activation and consent acceptance.
7. #278 — C/LW/RW/D/G, lines/pairs/goalie designation, and governed affiliate call-ups.
8. #280 — emergency contacts and narrowly scoped medical/safety alerts.
9. #190 — Team staff, certifications, screening, and expiry.

**Exit gate**

- The system can prove age eligibility against a versioned Season cutoff rule.
- Athlete identity is separate from seasonal Team membership; transfers never rewrite old Games.
- Birthdate, registration, contact, medical, and restricted fields are server-side private and sensitive reads are auditable.
- Coaches see only their Team's roster-operational data.
- Guardian/staff onboarding does not require sharing operator-created plaintext passwords.

### Release 3 — real hockey Game records and discipline

Recommended order:

```text
#31 outcome/rules foundation
  -> #156 authoritative event-level Game Sheet
  -> #279 Official/timekeeper sign-off and fee extensions
  -> #274 discipline and suspension eligibility
  -> #157 final-data Player/goalie statistics
  -> #34 public results/standings/bracket/stat surfaces
```

- #31 — explicit REG/OT/SO outcomes, configurable points, head-to-head, forfeits, and playoffs.
- #156 — goals, assists, penalties, shots, and goalies by period/time; derived score; finalize/reopen/version workflow.
- #274 — PIM, discipline cases, automatic review triggers, and roster blocks for suspensions.
- #279 — timekeeper, required Game crew, scorekeeper/referee sign-off, and fee export.
- #157 — deterministic skater/goalie statistics from approved final data only.
- #34 — public league portal under an explicit publication policy.

**Exit gate**

- A final score, signed Game Sheet, result, and standings cannot disagree.
- Suspended athletes cannot enter a lineup through manual, auto-fill, copy, or substitute paths.
- Corrections recompute every dependent result/standing/PIM/suspension/statistic exactly once.
- Public output contains only policy-permitted published data.

### Release 4 — rink-real scheduling and coherent operator UX

1. #277 — warm-up, resurfacing/turnover buffers, and curfew shared by manual and generated schedules.
2. #158 — recurring ice templates, conflict preview, and month view.
3. #206 — planner v2 formats, policy versions, fairness, scenarios, locks/repair, and actionable explanations.
4. #204 — task-focused operator IA, design system, responsive UX, and accessibility built on #159 and the new Player/Game workflows.
5. #146 — retain only as bounded UX follow-up where it does not conflict with #204.

**Exit gate**

- A database-conflict-free schedule is also physically operable at the rink.
- Operators compare and repair scenarios without regenerating published work.
- Program/Season context is explicit everywhere.
- Desktop and 390 px journeys pass keyboard/accessibility and zero-console-error gates.

## Production-hardening track

These issues run alongside Releases 0–4. Feature work must not bypass their applicable requirements.

- #201 — transaction parity, constraints, and concurrency safety.
- #202 — declarative routing, validation, authorization, and public/private contracts.
- #203 — production runtime, observability, and isolated background jobs.
- #207 — multi-worker notification reliability and recipient privacy.
- #208 — code, accessibility, visual, cross-browser, concurrency, upgrade, load, and security release gates.
- #209 — current product docs, ADRs, and machine-readable API contract; replace the stale first-slice README.
- #211 — invite/reset/MFA/multi-scope identity lifecycle after #266/#267.
- #256 — complete the guarded factory-reset operator UI.
- #155 — confirmation/preview before bulk notification processing.
- #210 — reporting, audit search, and exports after authoritative data sources exist.

A release is not production-ready until its migrations, rollback behavior, authorization, privacy, observability, and PostgreSQL acceptance journeys pass.

## Later operational and growth work

- #185 — live Venue/Rink status and disruption workflow.
- #189 — recurring practices and multi-day events.
- #186 — scheduled announcements after #203/#207/#155.
- #187 — registration, eligibility, waivers, and payments after #205/#273/#124/#201/#202/#207.
- #188 — configurable public league website after #34/#185/#186/#187/#189.
- #27 — native SwiftUI remains P3 until API, identity, Game Sheet, context, and privacy contracts stabilize.

## Backlog audit completed with this reset

### Closed as implemented or superseded

- #24 — obsolete auth/RBAC umbrella; remainder is #202/#211/#266/#267.
- #25 — home/away Team-aware roster baseline implemented.
- #28 — baseline scheduler implemented; remainder is #206/#277.
- #30 — Officials baseline implemented; remainder is #279/#190.
- #35 — privacy umbrella split into shipped consent foundation plus #124/#275.
- #36 — obsolete first-slice roadmap.
- #165 — hierarchy view implemented.
- #166 — superseded by completed #233 and focused #205/#273/#278/#190/#204 work.

### Rewritten to represent only remaining work

#27, #31, #34, #124, #156, #157, #159, #190, #202, #205, #206, and #211.

### New issues created from the review

#266, #267, #268, #269, #270, #271, #272, #273, #274, #275, #276, #277, #278, #279, and #280.

### Retained as still relevant

#146, #155, #158, #160, #185–#189, #201, #203, #204, #207–#210, and #256.

## Delivery rules

- One bounded PR slice at a time; no unrelated cleanup bundled into a defect fix.
- Migrations are forward-only, preflight existing conflicts, and preserve history/identifiers.
- Every mutation is authorized, transactional, server-attributed, and audited.
- Database constraints back invariants that concurrent requests could bypass.
- Sensitive reads and public payloads receive explicit privacy tests.
- UI work includes desktop, 390 px phone, keyboard/accessibility, and zero-console-error evidence.
- Every PR states its acceptance journey, migration/rollback behavior, privacy impact, and closing issue.
- An epic closes only when its production acceptance journey passes.