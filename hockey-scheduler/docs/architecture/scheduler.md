# Draft scheduler — round-robin generation and draft-commit (#84 / #86 / #233 Slice G / #206 slice 1)

A deliberately simple, deterministic fixture generator: single round-robin
pairings, each assigned to the earliest available game ice slot. Generation
is pure over the store (`services/scheduler.py`) and produces a *draft*
proposal only; nothing is persisted until the separate commit step.

## Model

`round_robin_pairings` (the circle method) takes a Division's — or a
League-wide draft's per-Division group's — registered team ids, sorted for
determinism, and returns every unordered pair exactly once (an odd team
count gets a bye each round). Two entry points share the same
pairing/ice-assignment core:

* `draft_schedule(store, division_id, ...)` — one Division.
* `draft_schedule_for_league(store, season_id, league_id, division_id=None, ...)`
  — a whole League for a Season, optionally narrowed to one Division.
  Registrations are grouped by their own Division (or "no Division") and
  each group gets its own round-robin — a league-wide draft never pairs
  teams across different Divisions of that League.

`_assign_ice` greedily assigns each pairing (in round-robin order) the
earliest still-free candidate slot that satisfies every constraint
(season/holiday/team/rink blackouts, the #277 scheduling-policy advisory);
a pairing with no satisfying slot is reported in `unscheduled[]` with
structured `reason_codes` (`season_blackout`, `holiday`, `team_blackout`,
`rink_blackout`, `max_per_day`, `min_rest`, `no_ice_available`,
`turnover_buffer_conflict`, `insufficient_playable_time`,
`curfew_violation`), and `unschedulable_teams[]` rolls up any team whose
*every* pairing failed.

## Existing-pairing exclusion (#206 slice 1)

Re-running Generate against a Division that already has some Games used to
recompute the **full** round-robin every time, with no awareness that a
pairing might already be scheduled — a re-run would either silently
duplicate a fixture already on the calendar (drift between two proposals
sharing no state) or, depending on ice availability, crowd out a genuinely
missing pairing for one that already exists. This is the production risk
slice 1 fixes: **existing Games — published or draft, roster-locked or
not — are always preserved untouched, and generation fills in only the
matchups that are genuinely still missing.**

`_existing_pairing_games(store, division_ids)` scans every Game already in
the target Division(s) and indexes `frozenset({home_team_id,
away_team_id}) -> existing_game_id`, with two deliberate exemptions:

* a **cancelled** Game does not count — a cancelled fixture is not "on the
  calendar" and its pairing is eligible for regeneration;
* an **exhibition** Game (`game_type != "regular"`) does not count either
  (#283) — a friendly is not a standings fixture, so it never satisfies a
  Regular pairing.

The index is flat across whichever Division(s) one call is scoped to — a
single Division for `draft_schedule`, or every Division a league-wide
`draft_schedule_for_league` call spans. It does not additionally check that
an existing Game's own `division_id` matches the specific pairing's
assigned Division; it only asks "does a real Game already exist for these
two team ids among the Divisions this call is looking at." For a
league-wide draft this is provably correct **as long as team ids are
unique to their current Division** (the normal case: `draft_schedule_for_league`
groups registrations by current Division, so the same two team ids are
only ever a live pairing candidate in one group). It would over-exclude in
a data-anomaly edge case a review flagged but no fixture reaches today — a
stale Game between the same two team ids, tagged to a *different*
in-scope Division from before one or both teams were reassigned — which
is out of this slice's bounded scope (tracked as a follow-up rather than
blocking here).

`_split_already_scheduled` partitions the full computed pairing list
against that index *before* `_assign_ice` ever runs: a pairing with a real
existing Game is removed from ice assignment entirely and reported by name
in the response's new `already_scheduled[]` (home/away id + name, division
id, `existing_game_id`) — visible to the operator, never silently dropped,
and never re-proposed alongside the genuinely missing pairings. Both
`draft_schedule` and `draft_schedule_for_league` return this key with
identical shape.

## Draft-then-commit workflow

Generation (`draft_season_schedule`) never writes to the store. Committing
(`commit_draft_schedule`) is a separate, audited step that persists the
proposal's rows as `is_draft=True`, unpublished Games and allocates their
ice slots; `publish_draft_games` / `discard_draft_games` make them public or
remove them. The commit body is implemented **twice** — the base facade
(`api/service.py`) and the league-scoped override
(`api/league_scoped_service.py`, the one production actually resolves to,
which additionally persists season/league scope and revalidates the
league-ice invariant) — the override fully reimplements the commit body
rather than delegating to `super()`, so any invariant the commit must
enforce has to be added to **both**.

## Concurrency: the commit-time recheck

A committed draft's proposal is generated *before* its transaction opens, so
the gap between generation and commit is a real window: a concurrent write
(another commit, a manual `create_game`) can turn one of the proposal's
still-pending pairings into a real Game before this commit reaches it. The
existing final placement gate (`_assert_slot_free_for_game` — physical slot
freedom plus per-team time-overlap) cannot see this case on its own: a
newly-real Game for the same pairing, sitting on a *different*, non-
overlapping slot, trips neither check.

Both commit implementations close this gap with a second, freshly-computed
`_existing_pairing_games` read, taken under the same Team lock the commit
already acquires (the canonical **Program → Team → Rink → Season** order
established by #277/#313/#314/#318 — no new lock was added for this). Per
row, inside the same loop as `_assert_slot_free_for_game`, checked **after**
that call succeeds: the existing physical-feasibility diagnosis
(`team_overlap`, `slot_unavailable`, and the #277 policy reasons) keeps
priority when a row happens to trip both, since it is the more specific,
still-valid answer; the new check only ever catches the residual case those
checks structurally cannot see.

A hit does not invent a new terminal error — it raises the existing
retryable `ConcurrencyConflictError` with `reason: "placement_raced"`, the
same signal #313/#314/#318 already use for a pre-lock scope locator
invalidated by a concurrent write. `commit_draft_schedule`'s retry shell
(inherited by both facades) already retries up to three times on exactly
this reason, regenerating a fresh proposal each attempt — and because
generation itself now excludes already-scheduled pairings (above), the
regenerated proposal correctly omits the now-real pairing and fills the
freed slot with a genuinely still-missing one instead. No bespoke recovery
path was needed; reusing the existing signal and the existing retry shell
was sufficient.

This is pinned by *forced* two-session races on real PostgreSQL
(`test_placement_concurrency.py`,
`_PairingDuplicationRaceMixin` and its two concrete subclasses covering
both facades): two draft commits for the same Division, each restricted to
its own single, non-conflicting ice slot, so both independently target the
same first pairing before either transaction opens. The loser's recheck is
proven load-bearing by neutering it directly (falsifiable, not
timing-dependent): with the check removed, the loser silently persists a
second Game for the identical pairing; with generation's exclusion removed
instead, the loser's every retry reproduces the same conflict and exhausts
the retry shell, surfacing a raw `placement_raced` error instead of
resolving automatically. Both facades are tested independently since
neither delegates to the other.

## Reason codes referenced here

Unscheduled-pairing codes are generation-time (`services/scheduler.py`,
listed above under Model). `placement_raced` is a commit-time
`ConcurrencyConflictError` reason, defined and retried in
`api/service.py`/`api/league_scoped_service.py`, not scheduler-specific —
shared with every other placement path that re-verifies a pre-lock locator
under its locks.
