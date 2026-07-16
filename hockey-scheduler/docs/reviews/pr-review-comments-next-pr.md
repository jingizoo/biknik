# Review comments for the next hockey-scheduler PR

Prepared 2026-07-16 against main @ 8cbe003. Paste individual comments onto the next PR
or use as the review checklist. Full context, journey evidence, and screenshots:
`docs/reviews/2026-07-16-expert-journey-review.md` (.docx/.pdf alongside).

The open PRs on the repository are unrelated to hockey-scheduler (Terraform/Oracle work from other projects in the same repo), and hockey-scheduler changes land through merged, numbered slices. The comments below are therefore written as a ready-to-post review for the **next hockey-scheduler PR branch**, ordered the way a maintainer should burn them down. Each carries the file anchors a reviewer needs.

## PR-1 — [Blocker] Fail closed on unscoped coach accounts

`backend/hockey_scheduler/web/scope.py:52  |  services/account_service.py:80–97  |  web/server.py:1713`

`scope_violation()` returns `None` for a coach with no `team_id` ('unbound coach (dev fallback) — not resource-scoped'), which grants global roster authority; and `create_account` never requires/validates a coach's `team_id` even though it validates `player_id`/`official_id` subjects. Verified live: a coach created with the scope key misplaced ended up unscoped and removed another team's rostered player. Fix: (a) in production mode treat an unscoped coach as *no* authority, not full authority; (b) require `scope.team_id` resolving to a real team when role=coach (mirror the #232 pattern used for player/official); (c) reject unknown top-level keys in the account-creation body so a misplaced `team_id` errors instead of vanishing. Please add a regression test: unscoped coach → 403 on any roster mutation.

## PR-2 — [Blocker] Rate-limit login and add a minimal password policy

`web/server.py:1239 (login route — no _rate_limited call)  |  services/account_service.py:77`

Every other anonymous surface has a bucket (`public_read`, `bootstrap_claim` 5/min, `factory_reset` 5/5min) — login has none, and `create_account` accepts any non-empty password. Add `_rate_limited("auth_login", limit≈5–10/min per IP)` plus a small per-username backoff, and enforce a minimum password length (8–10) at creation. Cheap, contained, and closes the only credential-guessing door in an app holding juniors' data.

## PR-3 — [Major] Add a player update endpoint (and UI edit drawer)

`services/setup_service.py:2179 (add_player)  |  api/service.py:3514  |  web/static/app.js:701–755`

Introduce `POST /api/v2/setup/player/{id}/update` accepting name, position, jersey_number, shoots, is_active, email — same validation as create, `player_updated` audit entry with changed-field diff, MANAGE_SETUP gate. Without it, correcting a misspelled name requires delete/recreate, which the dependency gate (correctly) blocks for any player with history. This is the single highest-value change on the board. While in there: give `/api/*` unknown-method requests a JSON 405 with an `Allow` header instead of the stdlib HTML 501 page.

## PR-4 — [Major] Jersey number integrity

`services/setup_service.py:2191–2193  |  services/setup_service.py:1207 (assign_player_team)  |  services/import_validator.py:146`

Enforce (a) range 1–98 inclusive; (b) uniqueness among *active* players of the same team — on create, on import, and on team reassignment (the destination-team check is currently absent: verified by moving a #17 onto a team that already had #17). Suggest a soft-conflict override flag for admins (leagues do grandfather odd cases) but never silent acceptance. Add the matching partial unique index in a migration so the SQL store enforces it too, following the migration-023 pattern.

## PR-5 — [Major] Player birthdate + structured age tiers

`domain/models.py:45  |  domain/setup_models.py:78 (Division.age_group free text)  |  services/hierarchy_import.py:71`

Add `birthdate: Optional[date]` to Player (nullable for migration), an age-tier table (U8…Senior) with cutoff dates per season, and validate at roster/registration time that the player's age fits the division's tier — warning-level at first, hard-block once data is backfilled. Extend the players import sheet with a `birthdate` column. Age eligibility is the most protested rule in junior hockey; the app currently cannot answer it at all.

## PR-6 — [Major] Player deactivate/retire lifecycle

`api/service.py (no set_player_active)  |  cf. accounts pattern web/server.py:1721–1728`

Add `POST /api/v2/setup/player/{id}/active {active: bool}` mirroring the accounts pattern; inactive players excluded from selection/substitute candidates (the engine already respects `is_active`), kept in history and standings. Injured reserve / mid-season departures are the normal case, deletion is the exception — the lifecycle should say so.

## PR-7 — [Minor] Wire or remove the dead fields

`domain/models.py:51 (shoots), :54 (guardian_person_id)`

`shoots` and `guardian_person_id` are defined, persisted, serialized in every player payload — and never written by any code path. Either expose `shoots` through create/update/import (preferred; coaches want it) and delete `guardian_person_id` in favour of the real GuardianLink model, or drop both. Dead-but-serialized fields mislead every API consumer reading the payloads.

## PR-8 — [Minor] Error-message and strictness polish

`services/setup_service.py:2189  |  api routes generally`

(a) Missing `team_id` → "Team None not found." — return `validation_error: team_id is required` instead of interpolating None. (b) Consider strict body-key validation on write endpoints; two live probes in this review had a mistyped key silently ignored (account scope; availability status), which is exactly how client bugs ship. (c) `venue_access_missing` details should include the remediation route so the fix is one click/curl away.

## PR-9 — [Minor] Close the v1/v2 delete asymmetry

`web/server.py:1938–1943 (v1 delete regex, no player/official)  |  :2173–2186 (v2 includes them)`

Either add player/official to the v1 delete dispatch or return an explicit 'moved to v2' error. 'Unknown setup entity' on a documented entity reads as a bug and cost this reviewer a probe cycle.

## PR-10 — [Minor] Accept date-only season bounds

`api/service.py (_parse_dt at season create)`

Accept `YYYY-MM-DD` for season start/end (midnight in the program's timezone). Seasons are dates in every league office on earth.

## PR-11 — [Enhancement] Standings & results: OT/SO and head-to-head

`api/service.py:2179–2269 (_standings_for_division)`

Add result types REG/OT/SO with a configurable points scheme, and head-to-head into the tiebreak ladder ahead of goal difference. Keep 'name' as the final deterministic tiebreak — that instinct is right for reproducibility, just label it 'drawing of lots' in the docs like the rulebooks do.

## PR-12 — [Enhancement] Statistics, penalties, suspensions (next slice proposal)

The natural next vertical slice after player identity: goal/assist and penalty events per game (scorekeeper-entered via the Game Sheet tab), PIM aggregation, and a suspension record that the roster engine's eligibility check consumes (suspended → not selectable, shows on the coach's screen with the reason). It reuses the existing audit, notification, and roster-status machinery beautifully — the architecture is already shaped to receive it.

## Praise worth keeping (so the good parts survive refactors)

- Goalie/skater slot separation everywhere — never collapse it.
- Dependency-gated deletes with itemized blockers; retire-don't-delete for contacts/preferences.
- Server-resolved audit actors (no client-supplied actor_id accepted anywhere it matters).
- Guardian authority strictly via verified consent links, with guardian actions blocked on general routes.
- Publish-gated public surface with zero junior PII; unpublished games 404 publicly.
- Draft-then-commit scheduler with machine-readable no-placement reason codes.
- The resumable onboarding checklist recomputed from real records.
