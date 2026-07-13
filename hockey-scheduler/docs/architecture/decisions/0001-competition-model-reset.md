# ADR 0001 — Competition model reset: Program → Season → League → optional Division; decouple venues

- **Status:** Accepted (design of record for epic #233). Supersedes the competition-hierarchy portions of `docs/architecture/data-model.md` until that doc is rewritten in Slice B.
- **Date:** 2026-07-13
- **Epic:** #233 (P0). Related: #180, #159, #201, #204, #205, #206, #209 (canonical docs), #212 (roadmap).
- **Scope of this ADR (epic Slice A):** define the canonical model, publish the old→new compatibility map, and set the migration-compatibility / API-deprecation rules. **No code or schema changes land in Slice A** — this is the single reference reviewers use for Slices B–G.

## Context

The product was built around this competition hierarchy and venue relationship:

```
League → Season → Level → Division
League → permanent Teams
League → Venues            (a venue is owned by one league)
```

Validated client feedback shows the real operating model is different in both shape and vocabulary:

```
Program → Season → League → optional Division
Program → permanent Teams → Players
Team → optional Club
Facility Owner (Organization) → Venue → Rink → Ice Slot
Season ↔ allowed Venues     (many-to-many)
```

Concrete client examples:
- **Programs:** Adult Men, Juniors, Ladies Hockey, High School.
- **Leagues in a Program/Season:** Diamond, Platinum, Gold, Silver, Bronze, Leisure (Adult Men); Varsity, Freshman (High School).
- **Teams in a League:** D1–D4, Rangers, …
- **Club:** optional; many programs use none.
- **Venue:** Twin Rinks hosts its own programs *and* external ones (e.g. Illinois High School Hockey); a competition may use several venues and a venue may host several independent competitions.

This is **not a cosmetic rename**. The word "League" moves *down* one level and a new top umbrella ("Program") appears, so a blind find-and-replace would corrupt the model. Continuing to add foreign keys, UX flows, imports, and scheduling logic against the current shape would harden the wrong relationships and raise migration cost. Hence the P0 reset.

## Decision — canonical target model

```
Organization
├── owns Venues
└── optionally operates Programs        (operator_organization_id, nullable)

Program                                 (permanent competition umbrella)
├── operator_organization_id  (nullable)
├── Seasons
└── permanent Teams

Season
├── program_id
├── Leagues
└── allowed Venues                      (many-to-many, SeasonVenueAccess)

League                                  (season-specific competitive grouping)
├── season_id
└── optional Divisions

Division                                (optional further split, e.g. North/South)
└── league_id

Team
├── program_id
└── club_id                             (nullable — Club is optional)

SeasonTeamRegistration                  (a permanent Team's placement for one Season)
├── season_id
├── team_id
├── league_id
├── division_id                         (nullable)
└── status/active

Player  →  Team   (seasonal roster model deferred to #205)
```

Invariants that Slices C–E will enforce (only **after** the target relationship is settled, per #201's "constraints last" rule):
- A Division belongs to exactly one League; a League to exactly one Season; a Season to exactly one Program.
- A Team belongs permanently to exactly one Program (never to a season-specific League/Division).
- A registration's `league_id` (and `division_id` when set) must resolve to the **same Season/Program** as the Team. Cross-program or cross-season assignments are rejected.
- `division_id`, `club_id`, and `operator_organization_id` are **nullable**. No placeholder rows (no fake `NA` Club, no synthetic "default" Division/League).
- Venue↔Program is **not** a permanent relationship. A Season's eligible ice comes from `SeasonVenueAccess`, not from Program ownership of a Venue.

## Terminology & compatibility map (old → new)

### Vocabulary shift (the collision)

| Today | Becomes | Note |
|---|---|---|
| **League** (top umbrella) | **Program** | The concept moves; the word is *reused* one level down. |
| **Level** (tier under Season) | **League** | Season-specific competitive grouping. |
| **Division** | **Division** | Same word, **reparented** from Season/Level to League. Stays optional. |
| **Season** | **Season** | Reparented from League to Program. |

Because "League" is reused, every reference must be disambiguated by layer, never text-replaced blindly.

### Entities & columns

| Current entity (`setup_models.py` / table) | Key columns today | Target entity | Column changes |
|---|---|---|---|
| `League` / `leagues` | `id, name, country, timezone, organization_id?, external_ref?` | **Program** | rename table→`programs`; `organization_id` → `operator_organization_id` (nullable, operator not owner) |
| `Season` / `seasons` | `id, league_id, name, dates, external_ref?` | **Season** | `league_id` → `program_id` |
| `Level` / `levels` | `id, season_id, name, sort_order, external_ref?` | **League** | rename table→`leagues` (new meaning); keeps `season_id` |
| `Division` / `divisions` | `id, season_id, name, age_group, level_id?, external_ref?` | **Division** | parent becomes `league_id` (from `level_id`/`season_id`); stays optional under a League |
| `Team` / `teams` | `id, name, division(legacy str), club_id?, division_id?(legacy), external_ref?, league_id?(#180 permanent)` | **Team** | `league_id` (permanent owner) → `program_id`; `club_id` stays **nullable**; retire legacy `division`/`division_id` scalars (already inert post-#180) |
| `SeasonTeamRegistration` / `season_team_registrations` | `id, season_id, team_id, division_id?, active` | **SeasonTeamRegistration** | **add `league_id`** (required once the League layer exists); `division_id` stays nullable; keep `(season_id, team_id)` uniqueness |
| `Club` / `clubs` | `id, name, country` | **Club** | unchanged; affiliation stays optional (no `NA` rows) |
| `Organization` / `organizations` | `id, name, short_name, external_ref?` | **Organization** | unchanged; gains a Program-operator role in addition to Venue ownership |
| `Venue` / `venues` | `id, name, address, timezone, organization_id?, league_id?(#173 permanent), external_ref?` | **Venue** | **drop `league_id`** (no permanent Program ownership); keep `organization_id` (facility owner) |
| — | — | **SeasonVenueAccess** (new) | `id, season_id, venue_id, active` (+ optional allocation metadata later); replaces `Venue.league_id`; many-to-many |
| `Game` / `games` | `…, season_id, division_id, ice_slot_id, …` | **Game** | schedule scope becomes Season + League (+ optional Division); `division_id` stays optional; a `league_id` scope column is added when the scheduler aligns (#206 / Slice G) |

`Rink` / `IceSlot` are unchanged (they hang off Venue, which stays owned by Organization).

### Routes / API (families; exact endpoints in `api-contract.md`)

| Current route family | Target family | Compatibility |
|---|---|---|
| `/api/setup/leagues/…` | `/api/setup/programs/…` | keep `leagues` as a deprecated alias returning the new Program shape until clients migrate |
| `/api/setup/levels/…` | `/api/setup/leagues/…` | **breaking meaning change** — must be versioned/deprecated explicitly, never silently repointed |
| `/api/setup/seasons/…` | `/api/setup/seasons/…` | payload gains `program_id` (was `league_id`); keep old key as deprecated read alias |
| `/api/setup/divisions/…` | `/api/setup/divisions/…` | parent key `league_id` (was `season_id`/`level_id`) |
| `/api/setup/venues/…` | `/api/setup/venues/…` | drop `league_id`; add Season↔Venue access endpoints |
| `/api/setup/registrations/…` | `/api/setup/registrations/…` | payload gains `league_id` |

### UI labels (Slice B)

- Screen/label **"League"** → **"Program"**; **"Level"** → **"League"**; **"Division"** stays, shown as optional.
- Update: Setup, Records, Hierarchy, onboarding wizard, imports, scheduler labels, validation messages, audit labels, and docs — with explicit route/API compatibility, not a blind text pass.

## Deterministic data-mapping rules (migration)

Forward-only, per-dialect (SQLite rebuild + PostgreSQL ALTER), following the #201 pattern: a **pre-migration report** names any ambiguous/invalid rows and the upgrade **aborts** rather than guessing. No silent reassignment, deletion, or cascade. Audit/history identifiers are preserved.

| Concept | Rule | Abort-and-report when |
|---|---|---|
| League → Program | Each current `leagues` row becomes a `programs` row, same `id` (id stability preserves history/audit). `organization_id` → `operator_organization_id`. | — (1:1, deterministic) |
| Season.league_id → program_id | Copy the value; the referenced league-now-program keeps its id. | a Season references a league id with no row |
| Level → League | Each `levels` row becomes a `leagues`(new) row, same `id`, keeping `season_id`. | — (1:1) |
| Division reparent | For a Division with `level_id` set → parent = that level-now-league. For a Division with only `season_id` (no level) → **needs a League**: attach to the season's sole League if exactly one exists; otherwise **abort and report** (operator must pick/create a League). | a season has 0 or >1 Leagues and a level-less Division exists |
| Team.league_id → program_id | Copy the permanent-owner value. | a Team's `league_id` has no league row |
| Registration.league_id (new) | Derive from the Team's Division-for-that-season if unambiguous; else from a single League under the season. | the league can't be uniquely derived from existing division/season data |
| Venue.league_id → SeasonVenueAccess | For each `Venue.league_id`, create `SeasonVenueAccess` rows for the seasons under that league-now-program where the link is unambiguous. | a venue's owning-league maps to 0 or many candidate seasons ambiguously |
| Club | `NA`, blank, and NULL in any source all map to **no Club** (null `club_id`); never create a Club record. | — |

Every mapping keys on **stable ids** so audit logs, results, rosters, and external_refs continue to resolve.

## Migration compatibility & sequencing

Implementation order (from the epic; Slice A is this document):

- **A — ADR + compatibility map** (this doc). Canonical docs updated before code.
- **B — terminology without data loss:** UI/label/route-alias rename (League→Program, Level→League), explicit API compatibility. No schema change.
- **C — competition schema:** introduce `programs`, reparent Season→Program, rename Level→League(new), reparent Division→League, move Team ownership to Program, add `registration.league_id`; forward-only SQLite+PostgreSQL migrations with pre-migration integrity reports.
- **D — optional Clubs:** `team.club_id` nullable end-to-end; drop any create/import Club requirement; `NA`/blank/null → no Club.
- **E — venue decoupling:** drop permanent Program/League→Venue ownership; add `SeasonVenueAccess`; keep Organization→Venue ownership; migrate unambiguous venue links.
- **F — imports & onboarding:** columns `facility_owner, venue, program, season, league, division, club, team, player`; wizard questions per the epic.
- **G — scheduler alignment:** generate for Season + League (+ optional Division); resolve teams via `SeasonTeamRegistration`, ice via `SeasonVenueAccess`; blackout/holiday inputs; unschedulable diagnostics.

Cross-cutting requirements (all slices): memory/SQLite/PostgreSQL parity; historical upgrade tests from the current production schema through the final model; restart and backup/restore acceptance on PostgreSQL; concurrency-safe constraints only after the relationship is settled.

## API deprecation plan

1. **Additive first:** new fields (`program_id`, `league_id` on registration) are added alongside the old ones; responses carry both during the transition.
2. **Aliases, not silent repoints:** `/api/setup/leagues` continues to answer with Program semantics; the *new* League concept is served at a new/clearly-versioned path so `levels→leagues` never silently changes a caller's meaning.
3. **Deprecation window:** old keys/paths return a deprecation marker (header/field) and are removed only after clients migrate, tracked in `api-contract.md`.
4. **No breaking change without a version bump** or an explicit, documented deprecation.

## Guardrails while epic #233 is open

- **Do not** add new FKs or NOT NULL constraints that permanently encode `Venue → Program`, `Team → old-League`, or **mandatory** Division ownership. (These would cement relationships this epic is deliberately reshaping.)
- #201 may continue on **orthogonal** transaction/idempotency/invariant work that does not encode competition ownership (e.g. the roster→game/player FKs already landed; a roster NOT-NULL follow-up is fine, but Team/Venue/Division ownership FKs wait for Slice C/E).
- #232 delete-actions work may continue independently, but must adopt the new terminology once Slice B lands.

## Consequences

- **Positive:** the schema and UI will match the client's real operating model; venues become shareable across programs; teams model permanent-program ownership with per-season league/division placement; later work (#159 multi-program context, #205 seasonal rosters, #206 scheduler v2) builds on correct relationships.
- **Cost/risk:** a vocabulary collision ("League" reused) demands careful, non-textual migration and dual-read API compatibility; the Division-reparent and Venue-decouple mappings have genuinely ambiguous cases that must **abort with row-level diagnostics** rather than guess.
- **Reversibility:** each slice is forward-only but bounded and independently reviewed; terminology (B) precedes schema (C) so reviewers share one vocabulary before data moves.
