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

`_existing_pairing_games(store, division_scope)` scans every Game already
in `division_scope` — an iterable of `(league_season_id, division_id)`
tuples, never bare division ids — and indexes `(league_season_id,
division_id, frozenset({home_team_id, away_team_id})) -> existing_game_id`,
with two deliberate exemptions:

* a **cancelled** Game does not count — a cancelled fixture is not "on the
  calendar" and its pairing is eligible for regeneration;
* an **exhibition** Game (`game_type != "regular"`) does not count either
  (#283) — a friendly is not a standings fixture, so it never satisfies a
  Regular pairing.

Both the filter (which Games are even considered) and the returned map's
key are scoped by the full `(league_season_id, division_id)` tuple, not by
`division_id` or pairing alone (#328 review, two rounds):

* **Round 1** — a league-wide draft's "no Division" group is keyed by
  `division_id=None` for every League/Season, and Teams are permanent, so
  scoping the *filter* by `division_id` alone let a division-less Regular
  Game from a completely unrelated Season/League — the same two team ids
  reused later — wrongly suppress a pairing never actually played in THIS
  League+Season.
* **Round 2** — scoping the filter alone was not enough: the returned map
  was still *keyed* by pairing alone, so a league-wide call with several
  Divisions in scope at once (all sharing one League+Season — the normal
  case for `draft_schedule_for_league`) could let a real Game that only
  ever qualified for Division A's scope wrongly match a lookup for
  Division B's fresh pairing — reachable whenever a team pair is
  reassigned from one Division to another and a stale Game is left behind
  in the Division they left. Keying the map by the full `(league_season_id,
  division_id, pairing)` tuple, and looking it up with the SAME tuple
  (`_split_already_scheduled` takes the call's single `league_season_id` —
  every pairing in one `draft_schedule` or `draft_schedule_for_league` call
  shares it — plus each pairing's own `division_id`), closes this
  precisely: Division A's stale Game can never satisfy a lookup keyed to
  Division B.

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
row, inside the same loop as `_assert_slot_free_for_game`, checked **before**
that call runs (#328 review round 4 — reversed from the original design):
a row whose exact pairing already has a real Game is a terminal,
product-confirmed fact regardless of whether that same row would *also*
fail the physical check — for example the winning Game happens to sit on
the row's own slot, or on a slot that overlaps it. `pairing_already_scheduled`
names the specific pairing and the winning Game; the physical-feasibility
diagnoses (`team_overlap`, `slot_unavailable`, and the #277 policy reasons)
cannot. An *unrelated* pairing's physical conflict — a different pairing
merely sharing one team, or an unrelated slot collision — is not in the
freshly-computed index for that row's own key, so it still falls through to
the unchanged physical check below.

A hit is deliberately **terminal**, not retried (#328 review round 2,
product-owner decision): it raises `ConcurrencyConflictError` with
`reason: "pairing_already_scheduled"`, naming `home_team_id`,
`away_team_id`, and the winning `existing_game_id`. This is a different
reason from `placement_raced` on purpose — `commit_draft_schedule`'s retry
shell (inherited by both facades) only retries that one specific reason,
so anything else, including this one, reaches the caller unretried on the
first attempt, with the whole batch rolled back atomically (the exception
propagates out of the same transaction the commit runs in).

The reason this does not auto-retry, unlike `placement_raced`: a winning
commit already changed what "missing" means, so a fresh proposal generated
mid-retry can genuinely differ from the one the operator reviewed — a
different pairing could fill the freed slot, silently substituting a
different batch into the commit the operator approved. That would break
the draft → review → commit contract (the operator must review what they
are about to commit). The caller must explicitly regenerate and re-review
before committing again, exactly like any other stale-preview case.

This is pinned by *forced* two-session races on real PostgreSQL
(`test_placement_concurrency.py`,
`_PairingDuplicationRaceMixin` and its two concrete subclasses covering
both facades): two draft commits for the same Division, each restricted to
its own single, non-conflicting ice slot, so both independently target the
same first pairing before either transaction opens. Exactly one commit
succeeds; the other is refused with the terminal reason, and only the
winner's Game ever exists. The loser's recheck is proven load-bearing by
neutering it directly (falsifiable, not timing-dependent): with the check
removed, the loser silently persists a second Game for the identical
pairing (both assertions fail); reverting the reason back to
`placement_raced` instead of the terminal one lets the loser be silently
retried to a DIFFERENT success instead of a named terminal error (the
"exactly one error" assertion fails). Both facades are tested independently
since neither delegates to the other.

The check-ordering priority (#328 review round 4 — pairing-identity before
the physical gate, not after) is pinned the same way with two further
forced-race scenarios per facade: the winning Game lands on the loser's
*own* slot, and on a *different* slot that physically overlaps it. Both
must still return `pairing_already_scheduled`, never `slot_unavailable` /
`team_overlap`. Falsifiable by reverting the check order alone (leaving the
terminal reason and the retry-shell distinction untouched): the loser then
gets the physical-conflict reason instead, failing exactly these two new
scenarios while the original non-overlapping-slot scenario (and everything
else in this section) still passes — isolating the ordering claim from the
terminal-vs-retry claim above it. Six deterministic Memory/SQLite tests
(same-slot / overlapping-slot / non-overlapping-slot × both facades, in
`test_scheduler.py`) cover the same three scenarios without needing real
concurrency, since a single thread can construct "a winning commit already
landed" directly — inserted directly rather than via a second real commit,
picking the LAST of six rows (not the first, #328 review round 6) so the
loser's own transaction has already tentatively created five earlier
rows before reaching the contested one, proving the WHOLE batch — not
merely the bad row — rolls back.

### Hardening the forced races against timing (#328 review round 6)

Two rigor gaps were found in the forced two-session PostgreSQL races
(`test_placement_concurrency.py`) after round 4 landed:

1. **Not forced at the critical boundary.** `_ForcedRaceHarnessMixin._run()`
   only barrier-synchronizes *entry* into each thread's target function.
   The original design had each side call the REAL `commit_draft_schedule`,
   whose own `draft_season_schedule()` regeneration runs AFTER that
   barrier — unsynchronized. A side whose regeneration happened to run
   after the other side had already committed could see an empty or
   different proposal, never exercising the locked `_existing_now` recheck
   this suite exists to prove. The fix: both sides' proposals are now
   hand-constructed and frozen (via the same monkeypatch technique the
   single-threaded stale-proposal tests already used) BEFORE the barrier
   releases either thread. The barrier's synchronization point is then
   exactly where the two committed batches are decided; the only race left
   is the one under test — which side's transaction acquires the shared
   Team lock for the contested pairing first.
2. **Only ever proved a one-row batch.** Each side's frozen proposal is TWO
   rows: a pairing UNIQUE to that side (never touched by the other) plus
   the SAME contested pairing both sides race for. Whichever side loses
   has therefore already tentatively created its own unique row within the
   same (about-to-roll-back) transaction before reaching the contested
   row — symmetrically, regardless of which side actually loses — so the
   assertions prove the loser's OWN earlier row rolls back to AVAILABLE,
   not merely that the contested row itself is refused.

`_run_multi_row_pairing_race` / `_assert_multi_row_pairing_race_terminal`
(shared by all three forced-race mixins: duplication/non-overlapping,
same-slot, overlapping-slot) implement this. Verified falsifiable the same
way as before (reverting the check order fails same-slot/overlapping-slot
only; removing the pairing-identity guard entirely fails all three), and
stress-run dozens of times with no flakiness observed.

### Proving the rollback mechanism directly, not by inference (#328 review round 8 finding 2)

Round 6's two-row batches (above) argue that the loser's own unique row
gets tentatively written before the contested row fails: both facades lock
every batch Team upfront via `_lock_teams`, *before* the per-row loop, so
the losing side's `get_team_for_update` on the contested Team **blocks**
(PostgreSQL `SELECT ... FOR UPDATE` waits — it does not fail) until the
winner commits and releases it; only once unblocked does the loser's
transaction proceed into its own per-row loop, where it necessarily reaches
and writes its own unique row before reaching the contested one. That is a
correct argument about the code's lock semantics, but the forced-race
test's final-state assertion (the loser's unique slot back to `AVAILABLE`)
cannot, by itself, distinguish "written, then rolled back" from "never
attempted" — both look identical from outside a transaction that never
commits.

`test_scheduler.py`'s `test_league_scoped_later_row_failure_rolls_back_earlier_writes`
(and its base-facade sibling) closes this by proving the mechanism
directly instead of arguing it from lock semantics: it injects a failure
on the *last* of six rows, and the injection point itself — running inside
the very same transaction — queries the store to confirm each of the five
earlier rows' Games and slot flips are already visible *before* raising the
error that unwinds the whole transaction. No threads are needed for this;
it is a direct, in-transaction observation of "written, then rolled back,"
verified (by a throwaway probe during development, checking rows that
provably had NOT yet been written) to correctly report absence when
something genuinely isn't there yet, ruling out a vacuously-true check.

### Revalidating `already_scheduled` under the lock (#328 review round 8/9)

Everything above rechecks pairings in `proposal["draft_games"]` — the rows
this commit is about to *write*. Nothing re-examined a row already
classified `already_scheduled`, because the commit never writes it. That
was a genuine gap: the wide `draft_fingerprint` gate (below) only catches
drift that had *already happened* by the moment this method's own
`draft_season_schedule()` regeneration ran, at the very top of the
function, before any lock. If the Game blocking an `already_scheduled` row
was cancelled in the narrow window *after* that regeneration/fingerprint
compare but *before* this transaction's locks were taken, the commit
proceeded — the reviewed batch it wrote was accurate as of a moment that
had already passed, and the pairing that just became genuinely open was
silently left unscheduled with no error.

The fix mirrors the existing `_existing_now` recheck rather than adding a
new mechanism: `_existing_pairing_games`'s scope now also covers every
`already_scheduled` row's `(league_season_id, division_id)`, and — using
that same locked snapshot — each `already_scheduled` row's
`existing_game_id` is compared against what the fresh read actually finds
for its pairing. A mismatch (the Game vanished — cancelled — or a
different Game now occupies that pairing) raises the terminal
`preview_stale`, exactly like any other form of drift, **before** the
per-row `draft_games` loop below it runs — so this check is fail-fast:
zero rows are ever written on this path, not a partial batch that then
rolls back.

This recheck is only genuinely race-free, not just incidentally so, if a
concurrent write touching an `already_scheduled` row's Teams is forced to
serialize against it. The existing Team lock (`_lock_teams`) previously
covered only `draft_games`' teams; it now also covers `already_scheduled`
rows' teams, so a Team that appears *exclusively* in an `already_scheduled`
row (never in any `draft_games` row) is locked by this commit too, matching
the same guarantee `draft_games` teams already had.

Pinned by a forced two-session PostgreSQL race
(`test_placement_concurrency.py`,
`_AlreadyScheduledCancelRaceMixin` and its two facade subclasses): one
session cancels the `already_scheduled` row's blocking Game; the other
commits a frozen mixed proposal (two genuinely-missing rows plus that one
`already_scheduled` row) whose blocked pairing's Teams appear in no
`draft_games` row at all — the specific shape the review asked for,
proving the Team-lock gap directly. A plain barrier-released race is not
enough to prove this: `cancel_game`'s path to its lock is far shorter than
`commit_draft_schedule`'s, so in practice the cancellation always finishes
first regardless of whether the fix is present — a build with the
revalidation removed entirely still passed a naively-asserted version of
this race every time, because "commit succeeds" looks identical whether
that success was legitimately fresh or blindly unconditional. The race
therefore forces the ordering explicitly: the commit thread's own
`_existing_pairing_games` call is monkeypatched to wait on an event the
cancel thread sets only after its cancellation has committed, guaranteeing
the commit's locked read happens strictly after. With that forced, exactly
one outcome is correct — terminal `preview_stale`, zero writes — and the
test asserts precisely that. Falsifiable: with the revalidation loop
removed, this forced version fails 100% of the time (confirmed
repeatedly), where the unforced version incorrectly passed every time.

## Preview binding: the commit-to-preview staleness gate (#328 review round 5)

The commit-time recheck above closes a *narrow* race: the gap between this
commit's own proposal regeneration and the moment it takes its locks,
typically milliseconds. It does not close the *wide* gap between the
operator's Generate click — which may sit on screen for seconds or
minutes while they review it — and their later Commit click:
`commit_draft_schedule` previously took only the scope (`division_id` /
`season_id`+`league_id`, `slot_ids`, `constraints`) and regenerated its own
proposal fresh, with no memory of what the operator actually reviewed. A
Game created for one of the reviewed pairings in that wider window would
silently shrink the committed batch (quietly reclassified into
`already_scheduled` by the fresh regeneration); a Game that had been
blocking a pairing, if cancelled in that window, would silently grow it (a
pairing the operator never reviewed newly appears and gets committed) —
either way, the batch actually committed could silently diverge from the
one the operator reviewed and approved.

`draft_season_schedule` now returns a `draft_fingerprint` alongside
`draft_games`/`already_scheduled` — a SHA-256 hash (`services/scheduler.py`,
`_draft_fingerprint`) over the *identity* of every pairing in each list
(`league_season_id`, `division_id`, team ids, and — for `already_scheduled`
— the existing Game id), sorted for order-independence. This mirrors the
ice-availability builder's `template_fingerprint`
(`SetupService.commit_ice_availability`): recompute the same deterministic
function fresh and compare, rather than store a server-side session.

**#328 review round 7 correction:** the fingerprint also binds each
`draft_games` row's `ice_slot_id` and `start_time` — an earlier version
deliberately left placement out, reasoning that the per-row physical check
(`_assert_slot_free_for_game`) at commit time already re-validates slot
freedom fresh. That reasoning conflated two different properties: the
physical check proves a placement is *legal* (free, non-conflicting); it
does not prove it is the *same* placement the operator reviewed. If the
reviewed slot became unavailable between Generate and Commit and a
different, still-valid slot was chosen instead for the identical pairing —
or if `slot_ids`/`constraints` simply differed between the preview call
and the commit's own regeneration — the pairing/already-scheduled identity
alone would still match, and the commit would silently persist a
placement the operator never saw. Binding placement closes this: any
change to which slot (or its time) a still-missing pairing resolves to
now changes the fingerprint, and the commit is refused
(`preview_stale`) rather than silently substituting it.

`commit_draft_schedule` (both facades) now requires a `draft_fingerprint`
argument, checked immediately after regenerating the proposal and BEFORE
any lock is taken or write happens:

* missing entirely → `ValidationError`, `reason: "preview_required"` (a
  caller error — mirrors the ice-availability builder's identical
  requirement that a commit can never proceed without first previewing);
* present but not equal to the freshly-regenerated proposal's own
  fingerprint → `ConcurrencyConflictError`, `reason: "preview_stale"` — the
  world has moved since this was previewed; regenerate and review again.

Both checks precede — and are independent of — the narrower
`_existing_now` recheck described above: this fingerprint check is the
coarse, wide-window gate (the whole preview-to-commit-click gap); the
narrower recheck remains responsible for the sub-millisecond gap between
THIS call's own regeneration and taking its locks. A caller that races
another commit may still see either reason fire, depending on precisely
when the drift happened, and both are equally terminal, atomic-rollback
outcomes.

The Scheduler UI (`web/static/app.js`) sends the fingerprint from the
displayed preview automatically; on either `preview_stale` or
`pairing_already_scheduled` it clears the stale preview client-side so
Commit cannot be retried without a fresh Generate — the same reaction
already established for the narrower race in round 3. Verified falsifiable
by neutering each check in turn (`test_scheduler.py`,
`test_league_scoped_commit_refuses_stale_preview_after_pairing_created`
and its cancel/base-facade/missing-fingerprint siblings): with the check
disabled, a stale commit through either facade proceeds and creates a Game
instead of being refused.

### This is an intentional breaking change to the Commit request contract (#328 review round 9)

`draft_fingerprint` is **required**, not optional or additive: a request to
`POST /api/scheduler/commit` that omits it — the entire payload shape any
caller could send before this change — is unconditionally refused, never
silently accepted. This is a breaking change to the *request* contract, not
merely an additive change to the *response* shape (the new
`already_scheduled`/`draft_fingerprint` response keys genuinely are
additive — a caller that doesn't read them is unaffected by their
presence). Concretely:

* **Required sequence.** A caller MUST call `POST /api/scheduler/draft`
  (or `draft_season_schedule` directly) first, and MUST pass the exact
  `draft_fingerprint` from that response's body to the following
  `POST /api/scheduler/commit` call. There is no way to commit a draft
  schedule without first previewing it — by design, matching the
  ice-availability builder's identical, already-shipped
  `template_fingerprint` requirement (`SetupService.commit_ice_availability`,
  #313); this feature is held to the same contract, not a looser one.
* **Error/status contract** (`ERROR_HTTP_STATUS` in `web/server.py`, keyed
  by the `DomainError` subclass's `code`, not its `reason` — see "Reason
  codes referenced here" below for the full reason-code list):
  - `draft_fingerprint` omitted entirely → `ValidationError`
    (`code: "validation_error"`, HTTP **400**), `reason: "preview_required"`.
  - `draft_fingerprint` present but not matching the fresh regeneration
    (or a pairing already scheduled by a race) →
    `ConcurrencyConflictError` (`code: "concurrency_conflict"`, HTTP
    **409**), `reason: "preview_stale"` or `"pairing_already_scheduled"`.
  - The exact fingerprint from the immediately-preceding Generate
    response → HTTP **200**, the commit proceeds normally.
  In every refusal case, zero Games, slot allocations, or audit rows are
  written — the refusal happens before the transaction opens (the
  `preview_required`/`preview_stale` checks) or is rolled back atomically
  within it (`pairing_already_scheduled`).
* **Affected consumers.** This is a demo/operator application with no
  external/third-party API consumers. The only two real callers are the
  Scheduler UI (`web/static/app.js`'s `schedCommit.onclick`, which already
  sends `schedulerState.preview.draft_fingerprint` — the exact value from
  its own most recent Generate response) and this repository's own test
  suite (backend service/HTTP tests, e2e browser journeys). A full
  repository audit (every `/api/scheduler/commit` HTTP call site and every
  `commit_draft_schedule(` Python call site) found no caller that violates
  the new contract: every real caller supplies a fingerprint sourced from
  a genuine, immediately-preceding `draft_season_schedule`/
  `POST /api/scheduler/draft` response. The only exceptions are tests
  written specifically to prove the refusal contract itself (omitting or
  staling the fingerprint on purpose), and two forced-concurrency unit
  tests in `test_placement_concurrency.py` that use a hardcoded-but-
  self-consistent token (fed identically to both the monkeypatched
  `draft_season_schedule` stub and the `commit_draft_schedule` call) to
  isolate a Team-lock ordering race unrelated to fingerprint freshness —
  documented as such in their own docstrings, and never reaching the real
  HTTP route.
* **No versioned/legacy bypass.** The requirement applies unconditionally
  to both the base and league-scoped facades, with no opt-out flag and no
  parallel legacy route. Given there are no external consumers to break
  and the alternative (an unauthenticated legacy path that skips the
  preview-binding safety gate this whole section exists to add) would
  reopen exactly the silent-staleness risk described above, versioning the
  endpoint was rejected in favor of documenting this plainly as an
  intentional breaking change.

Pinned by HTTP-level contract regressions (`test_scheduler.py`,
`SchedulerHttpTest`) that exercise the real route end-to-end, not just the
service method directly: `test_commit_without_fingerprint_is_preview_required_via_http`
(the legacy no-fingerprint payload → 400, zero writes),
`test_commit_with_exact_fingerprint_succeeds_via_http` (the documented
sequence → 200, real Games created), and
`test_commit_with_stale_fingerprint_is_preview_stale_via_http` (a
fingerprint valid at Generate time but no longer current → 409, zero
writes beyond the change that made it stale). Each verified falsifiable by
neutering the corresponding check in the league-scoped facade (the one the
HTTP route actually resolves to) and confirming only the matching test
fails.

### The fingerprint must bind team eligibility, not just placed/already-scheduled rows (#328 review round 10 finding 1)

`_draft_fingerprint` originally hashed only `draft_games` (the missing
pairings, with placement — round 7) and `already_scheduled`. That leaves a
real gap: the circle method's round-robin (`round_robin_pairings`) can add
or remove a team from a Division and leave the exact SAME pairing placed on
the exact SAME slot, because the new/removed team only reshuffles *other*
pairings' bye/mirror positions. With exactly one open ice slot, for
example, registering a new team can grow `team_count` and the
`unscheduled` set from 4/5 to 5/9 while `draft_games` and
`already_scheduled` stay byte-for-byte identical on Commit's own
regeneration — invisible to a fingerprint that never looked at either
field, so Commit would silently persist a batch reviewed under a
DIFFERENT, now-stale roster.

`_draft_fingerprint` now also binds the full eligible `team_ids` set
(order-independent — sorted before hashing) and, per unscheduled pairing,
its own `division_id`/pairing/`reason_codes`, plus the `unschedulable_teams`
rollup. Any team registration change, or any change to WHY a pairing is
unscheduled (not just WHICH pairings are), between Generate and Commit's
own regeneration now invalidates the preview (terminal `preview_stale`,
zero writes) even when every placed/already-scheduled row is unchanged.

Pinned two ways:

* **Direct, deterministic proof of the fingerprint function itself**
  (`test_scheduler.py`, `DraftFingerprintTest`) — calls `_draft_fingerprint`
  directly with identical `draft_games`/`already_scheduled` but a varied
  `team_ids`, `unscheduled` reason code, or `unschedulable_teams` entry, and
  asserts the hash changes; plus determinism and team-id-order-independence
  checks. This isolates the property precisely without depending on any
  particular round-robin fixture.
* **End-to-end regressions through both commit facades**
  (`_stale_preview_refused_on_team_eligibility_change` in
  `SchedulerContract`, covering Memory/SQLite/PostgreSQL depending on how
  the suite is invoked) that exploit the SAME circle-method mechanic
  organically: a 4-team Division with exactly one ice slot places `t0` vs
  `t3` (the circle method's `fixed` team against the last team in sort
  order) and leaves five pairings unscheduled. Registering a 5th team that
  sorts alphabetically *before* every existing team gives that new team
  the round-0 bye instead, shifting the original four teams up by exactly
  one array position each and leaving `t0` vs `t3` placed identically —
  `team_count`/`unscheduled` change size, `draft_games` does not.
  Unregistering runs the same fixture in reverse. Both directions assert
  terminal `preview_stale` and zero Games/audit rows written.
* **A real (unstubbed) browser round trip**
  (`scheduler-already-scheduled.js`, scenario 5, desktop and 390px): unlike
  every other refusal scenario in that journey, Commit here is NOT
  intercepted — a genuine `POST /api/scheduler/commit` reaches the real
  backend. A 2-team Division's one pairing is previewed and placed; a 3rd
  team (engineered, via one unregistered decoy team, to sort alphabetically
  before the other two — `team_N` ids compare as strings, so a `team_9` →
  `team_10` boundary crossing would otherwise silently sort the new team
  in the wrong position) registers afterward, byeing itself in round 0 and
  leaving the original pairing/slot unchanged. Clicking Commit shows the
  same actionable toast and stale-preview recovery as the stubbed
  scenarios, and a follow-up Generate confirms both zero Games were created
  AND the placed row is still exactly the one originally previewed —
  proving the refusal fired despite nothing about the reviewed placement
  having changed.

All three verified falsifiable together: reverting the fingerprint to its
pre-round-10 shape breaks the direct unit tests, both `SchedulerContract`
regressions (Commit silently succeeds and creates the previewed Game), and
the browser scenario (the follow-up Generate shows the pairing as
already-scheduled — a real Game the refused-in-appearance Commit actually
created).

### Test-harness correctness is part of the contract too (#328 review round 10 findings 2/3)

Two further round 10 findings were not about the product code at all, but
about whether the EXISTING regressions proving earlier rounds' fixes could
be trusted:

* **A forced-race harness that could deadlock.** The already_scheduled
  cancel-vs-commit PostgreSQL race
  (`_run_already_scheduled_cancel_vs_commit_race`,
  `test_placement_concurrency.py`) originally monkeypatched the commit
  thread's own `_existing_pairing_games` call — which runs AFTER
  `commit_draft_schedule` has already acquired its Season lock — to wait on
  a `cancel_committed` event set only once `cancel_game` (which needs that
  SAME Season lock) returns. If the commit thread reached the Season lock
  first, the two sides deadlocked: commit holding the lock while waiting
  for an event only the (now permanently blocked) cancellation could set.
  Bounded only by a 10s `wait(timeout=...)`, which side lock first was
  scheduling luck, not something the harness controlled. Fixed by moving
  the wait to BEFORE `commit_draft_schedule` is called at all, while the
  commit thread holds no locks whatsoever — `cancel_game` then always runs
  unimpeded, and the commit's own (now unpatched) locked read naturally
  observes the already-committed cancellation. Stress-run 10x (20 test
  executions) against real PostgreSQL with no hangs or failures.
* **Rollback proof that stopped short of the id counter.** The direct
  in-transaction rollback proof (`_later_row_failure_rolls_back_earlier_writes`,
  `test_scheduler.py`, covering Memory/SQLite/PostgreSQL) observed that
  five tentatively-written Games and their `ALLOCATED` slots vanish after
  the injected failure unwinds the transaction, but never checked the five
  `next_id("game")` increments themselves — a rollback that restored every
  row but left the Memory `_counters`/SQL `counters` mutation outside the
  rolled-back transaction would still have passed. The test now captures
  each earlier row's actual allocated Game id and asserts a fresh
  `next_id("game")` call after rollback returns exactly the first one —
  proving the counter itself rolled back, not just the rows built on it.
  Verified falsifiable by temporarily excluding `_counters` from the
  Memory store's transaction snapshot and confirming the assertion catches
  the resulting id leak.

### Binding the reason TEXT, and revalidating everything bound under the lock (#328 review round 11)

Round 10 widened `_draft_fingerprint` to cover the eligible-team set and
each unscheduled pairing's `reason_codes`, but two gaps remained:

1. **The fingerprint bound `reason_codes` but not `reason`.** A scheduling
   policy's THRESHOLD (`min_playable_minutes`, a turnover buffer, a
   curfew) can change between Generate and Commit and rewrite the
   human-readable `reason` text an unscheduled row shows — the number
   embedded in the message — while the reason CODE, a fixed category,
   stays identical. A 4-team Division with a 60-minute slot (always
   playable) and a 30-minute slot (never, at either threshold) places one
   pairing on the 60-minute slot and leaves the other five citing
   `insufficient_playable_time` against the 30-minute one; raising
   `min_playable_minutes` from 45 to 50 rewrites each of those five rows'
   "requires at least N" text without moving the placed row or changing
   any code — invisible to a fingerprint that only hashed the code.
   `_draft_fingerprint` now also binds `reason` for exactly this reason:
   the operator reviewed the specific explanation on screen, not just its
   category.

2. **Only `draft_games`/`already_scheduled` row identity was revalidated
   under the lock, not the newly fingerprint-bound dimensions.** A team
   registering or unregistering in the narrow gap between commit's own
   pre-lock regeneration (the one the wide `draft_fingerprint` gate
   compares against) and the locks it then acquires is invisible to every
   existing check: the wide gate only compares against its OWN
   regeneration, which ran before the change, and the changed team need
   not appear in `draft_games`/`already_scheduled` at all if it only
   affects the `unscheduled` portion.

   Rather than hand-list this as one more narrow, dimension-specific
   check, both facades now regenerate the COMPLETE current proposal a
   second time, once every lock the proposal's inputs depend on
   (Program/Team/Rink/Season) is held, and compare its own
   `draft_fingerprint` against the one the operator's Generate call
   returned — a single general check covering every fingerprint-bound
   dimension at once, current and future, rather than one hand-written
   recheck per field. This is sound because `register_team_for_season`
   and `unregister_team_from_season` both lock the Season row too (the
   same `require_active_season` guard `cancel_game` uses, per round 8's
   revalidation): by the time this transaction holds that lock, any such
   concurrent write has either already committed — and this regeneration
   observes it — or is blocked behind this transaction and cannot land
   before its writes. The pre-existing narrower checks (already_scheduled
   revalidation, the per-row `pairing_already_scheduled` guard) are left
   in place unchanged: they give a more specific, actionable error for the
   scenarios they were built for, and are simply redundant-but-harmless
   for a commit this new general check has already passed.

Pinned two ways. Finding 1: a direct `DraftFingerprintTest` unit test
(identical `reason_codes`, different `reason` text → different
fingerprints) plus a both-facade Memory/SQLite/PostgreSQL regression that
raises `min_playable_minutes` between Generate and Commit while confirming
the placed row, pairing identities, and reason codes all stay unchanged —
isolating the reason-TEXT axis from the (separately covered) placement
axis — then asserting terminal `preview_stale` and zero writes. Finding 2:
a both-facade Memory/SQLite/PostgreSQL regression that forces the exact
ordering deterministically, without threads — a monkeypatched
`draft_season_schedule` returns the SAME frozen (pre-change) proposal on
its first call (modeling the wide gate's own regeneration running before
the registration change), applies the registration/unregistration change
on the second call, then delegates to the real function — so commit's own
locked regeneration is the first call to observe the new state, exactly
reproducing the narrow window the finding describes; the test also asserts
the mock was called exactly twice, since if it is not, the whole scenario
the fix protects against was never even modeled. Both findings verified
falsifiable: reverting the `reason` binding lets the stale threshold-change
commit silently succeed; removing the locked full regeneration collapses
the mock to a single call (the registration/unregistration mutation is
never even applied), directly demonstrating the narrow window is
unguarded.

## Reason codes referenced here

Unscheduled-pairing codes are generation-time (`services/scheduler.py`,
listed above under Model). `pairing_already_scheduled`, `preview_required`,
and `preview_stale` are commit-time reasons defined in
`api/service.py`/`api/league_scoped_service.py`:

* `pairing_already_scheduled` (`ConcurrencyConflictError`) — the narrow,
  under-lock recheck above; names the pairing and winning
  `existing_game_id`.
* `preview_required` (`ValidationError`) — the wide preview-binding gate,
  no fingerprint supplied at all.
* `preview_stale` (`ConcurrencyConflictError`) — the wide preview-binding
  gate, fingerprint supplied but no longer matches current state.

All three are deliberately distinct from `placement_raced` (the pre-lock
scope-locator staleness reason #313/#314/#318 use, which the retry shell
DOES retry) precisely so none of them are ever retried. `placement_raced`
is not scheduler-specific; it is shared with every other placement path
that re-verifies a pre-lock locator under its locks.
