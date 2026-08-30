# Substitute Matching Design

> **Status:** bounded, non-production design for #287. This document records
> settled ranking rules, the five owner-approved product rulings, and contracts
> for future slices. It does **not** add or authorize production
> code, schema, migrations, persistence, API mutations, notification delivery,
> service wiring, or runtime state transitions.

## Purpose and boundary

When a roster vacancy opens, the future substitute workflow should rank the
eligible candidates deterministically, explain why the first candidate was
proposed, offer that candidate the slot, and advance to the next candidate
after a decline or timeout.

This design is deliberately split at two boundaries:

1. A future projection layer derives eligible, immutable candidate facts from
   the Game's exact `LeagueSeason` and `SeasonRosterMembership` records.
2. A pure ranking core orders those facts without reading a clock, store,
   request, session, or mutable domain record.

The existing #205 eligibility rules remain authoritative. This document does
not create a temporary eligibility model and does not reinterpret permanent
`Player.team_id` as seasonal authority.

## Decision register

### Owner-approved #287 product decisions

**Owner-confirmed (2026-08-29):** the repository owner approved all five
recommended defaults below. They are binding inputs for future authorized
slices; this approval does not itself authorize production code or merge.

| Question | Approved rule | Alternatives | Tradeoff | Status |
| --- | --- | --- | --- | --- |
| Fairness counter reset | Count completed substitute appearances within the current `LeagueSeason`; a new LeagueSeason starts at zero. Historical counts remain visible but do not affect the new season's rank. | Lifetime count; rolling last-N games; rolling time window; League-configurable reset. | Per-LeagueSeason is easy to explain and does not permanently penalize a frequent substitute. Lifetime is simpler to aggregate but lets old service dominate new seasons. Rolling windows react faster but are harder to reproduce and explain. | **Resolved 2026-08-29** |
| Which games count | Count only a finalized Game in which the substitute has a recorded occupying/participating roster row. Do not count an offer, acceptance, scheduled-but-unplayed Game, cancellation, or no-show as a completed substitute appearance. Track no-shows separately if the product later needs reliability policy. | Count accepted offers; count scheduled roster assignments; count no-shows as appearances; apply a separate no-show penalty. | Actual participation makes the fairness number truthful and avoids charging a player for cancellations. It depends on authoritative Game completion and participation records. Earlier counting is available sooner but can misstate service. | **Resolved 2026-08-29** |
| Skill-rating ownership | A League administrator owns the canonical 1–7 rating for that League context. Coaches may submit a recommendation, but cannot silently replace the canonical value; self-rating is informational only. Changes require an effective time and actor/reason audit. The data model belongs to #273. | Team-coach-owned rating; player self-rating; multi-rater average; global rating shared across Leagues. | League ownership gives one competition-wide scale and stable comparisons. Coach ownership is operationally easy but may be inconsistent across Teams. Self-rating is inclusive but not authoritative. A composite is richer but requires conflict and weighting policy. | **Resolved 2026-08-29** |
| Cross-boundary substitution | Default to the same `LeagueSeason`. Permit cross-Division candidates inside that LeagueSeason only when an explicit League policy enables it. Keep cross-League substitution off until a separately authorized rule defines allowed relationships and the responsible approver. | Same Division only; any Division in the LeagueSeason; any League in the Program/Season; affiliate/call-up relationships only; unrestricted privileged override. | Same-LeagueSeason preserves the competition boundary and uses the seasonal membership already established by #205. Narrower scope reduces the pool. Wider scope improves fill rate but adds authorization, fairness, standings, and audit consequences. | **Resolved 2026-08-29** |
| Late-game offer validity | Compute `expires_at = min(offered_at + response_window, game_start)`. Create no offer when `expires_at <= offered_at`. Treat the response interval as half-open: an acceptance must commit before `expires_at`; at the deadline, expiry wins and the workflow may advance. | Always grant the full response window; use `roster_lock_time`; stop offers at a configurable pre-game cutoff; allow post-start emergency offers. | Clamping prevents an offer from remaining live after the Game starts and produces one deterministic deadline. Very late candidates may have no usable response window. A roster-lock anchor may be too early or absent; emergency post-start behavior needs a separate explicit policy. | **Resolved 2026-08-29** |

### Settled ranking rulings

These three rules were settled by the repository owner on issue #287 and are
not reopened by the five rulings above.

| Topic | Settled rule | Consequence |
| --- | --- | --- |
| Skill direction | The scale is `1..7`, with **7 strongest**. Skill proximity is symmetric: `abs(candidate_rating - needed_rating)`. | Candidates one point stronger and one point weaker are equally close. A League may change rule priority, not the meaning of `SKILL_MATCH`. |
| Missing ratings | An unrated candidate is not excluded. When skill matching is enabled, every rated candidate ranks ahead of every unrated candidate; unrated candidates then order by the remaining rules. | A missing future #273 value cannot empty the pool or masquerade as a perfect match. If all candidates are unrated, skill matching has no effect. |
| Notice anchor | Remaining notice is measured to `game_start`, not `roster_lock_time`, and the comparison is inclusive. | `game_start - as_of == minimum_notice` is eligible; one unit less is ineligible. A future command boundary captures `as_of` from its authoritative clock under the transaction lock; it is never client-controlled. |

## Pure ranking contract

The names below describe design records, not implemented APIs.

### Inputs

`RankingRequest` contains only immutable scalars:

- Game and vacancy identifiers;
- a stable ranking-request identifier for that vacancy/offer chain, reused on
  retries;
- exact `league_season_id`;
- needed `Position` (goalie/skater slot type is derived from it, never supplied
  as a second independent axis);
- optional absent-player skill rating;
- explicit `as_of` and `game_start` timestamps; the pure core receives
  `as_of` as a scalar, while a future command boundary must capture it from
  its authoritative clock under the transaction lock rather than accept it
  from a client;
- a stable random seed when the League policy enables random ordering.

`RankingCandidate` contains only projected facts:

- `player_id` and the exact membership identifier used for eligibility;
- eligible positions;
- optional 1–7 skill rating;
- completed-substitute count under the owner-approved fairness policy;
- minimum notice duration;
- any boundary classification already authorized by the projection layer.

A future production `LeagueRankingPolicy` contains a versioned, ordered list of
enabled rules. The current pure prototype models the ordered rules but does not
yet add a durable policy version. The ranking core may understand these rule
kinds:

- `POSITION_PREFERENCE`;
- `FAIRNESS`;
- `SKILL_MATCH`;
- `RANDOM`.

Goalie-versus-skater compatibility and sufficient notice are eligibility gates,
not soft ranking rules. A League cannot rank its way around the goalie gate.
Cross-boundary permission is also resolved before ranking.

### Preconditions

- Candidate IDs and membership IDs are unique within one request.
- Timestamps are timezone-aware and supplied explicitly to the pure core. In a
  future HTTP integration, the command boundary captures `as_of` from its
  authoritative clock under the transaction lock; request data cannot provide
  or override it.
- Ratings, when present, are integers in `1..7`.
- The policy contains no duplicate rule and only recognized rule kinds.
- Needed position is the single source of truth for goalie/skater slot type.
- An override candidate, if present, has already passed the authorization and
  hard-eligibility boundary described below.

Malformed input fails by name. The core never repairs conflicting identities,
guesses a LeagueSeason, reads the permanent Team pointer, or consults current
time implicitly.

### Ordering and result

When `SKILL_MATCH` is enabled, the settled rated/unrated partition is the first
comparison: every rated candidate precedes every unrated candidate. The League-
configured rule order then applies within each partition, and every enabled
rule contributes one comparison key in that order. Lower keys rank first.
Position preference is exact position, then a skater who supports both forward
and defence, then the alternate skater position. A generic `SKATER` vacancy
supplies no forward/defence preference, so all non-goalies tie on this rule
instead of rewarding an unspecified candidate record. Fairness uses the owner-
selected completed-appearance count. Skill uses the settled symmetric distance.
Random uses a stable digest of
`(policy_seed, ranking_request_id, player_id)`, never input order or an
unseeded shuffle. The request identifier must remain stable across retries;
changing it deliberately creates a new draw for a different vacancy.

When all configured keys tie and random is disabled, canonical `player_id`
ascending is the final stability key. This fallback is an implementation
stability rule, not a claim that the identifier is a hockey preference.

The future production result contract contains:

- the complete ordered candidate list;
- the engine's proposed candidate, if any;
- a per-candidate trace of gate outcomes and comparison keys;
- a concise explanation based only on those recorded facts;
- the policy version and a reproducible input/sort fingerprint (future
  integration metadata, not fields claimed by the current prototype);
- a separately labelled override selection, when one was authorized.

A rule is named as the deciding rule only if removing that rule from the
policy changes the engine's proposed candidate. Overdetermined results say
that no single rule decided the outcome. An override says that a person made
the selection and preserves the engine proposal as context: selected when the
override confirms that proposal, otherwise explicitly non-selected.

### Pure ranking pseudocode

```text
rank(request, candidates, policy, authorized_override = none):
    validate immutable request, candidates, and policy

    eligible = []
    rejected = []
    for candidate in candidates ordered by canonical player_id:
        if candidate slot type != request slot type:
            rejected += trace(candidate, "position_class_mismatch")
            continue
        if request.game_start - request.as_of < candidate.minimum_notice:
            rejected += trace(candidate, "insufficient_notice")
            continue
        eligible += candidate

    ordered = stable_sort(eligible, keys_from(policy, request))
    proposed = first(ordered) or none
    deciding_rule = counterfactual_decider(proposed, eligible, policy)

    if authorized_override is present:
        require authorized_override in eligible
        selected = authorized_override
    else:
        selected = proposed

    return immutable result(
        selected, proposed, ordered, rejected,
        deciding_rule, explanations, policy.version, fingerprint)
```

The function has no I/O, no clock read, no persistence, and no side effect.

## Future eligibility projection

This is a contract for a later authorized integration slice, not production
wiring delivered by this document.

```text
project_candidates(game_id, vacancy, authenticated_scope, as_of):
    require as_of is the authoritative timestamp captured by the calling command
    load the Game and capture its immutable authorization fields once
    require the Game's exact LeagueSeason binding to be coherent
    resolve the requesting side and vacancy without trusting client team ids

    derive candidate memberships through the existing #205 game-scoped rule
    for each candidate:
        require an eligible SeasonRosterMembership for game.league_season_id
        apply the owner-approved cross-boundary policy
        derive positions and future skill data from their owning records
        compute fairness facts under the owner-approved counting rule
        copy only immutable scalar facts into RankingCandidate

    return RankingRequest, projected candidates, policy version
```

The projection must use `Game.LeagueSeason` and the exact
`SeasonRosterMembership` relationship. Raw session mappings, mutable `Game`
objects, and mutable membership objects do not cross into the ranking core.
The projection may reduce the pool; the ranking core cannot grant eligibility
that the projection refused.

`Player.skill_rating` already supplies a global optional 1–7 value, but it does
not satisfy the approved League-context ownership rule. Issue #273 still owns
the durable model that gives a League administrator canonical authority and
records scope, privacy, effective time, actor, and reason. Until that contract
exists, production integration must project candidates as unrated; this
document does not add a temporary field or treat the permanent Player value as
seasonal authority.

## Offer workflow design

### Conceptual states

Candidate enrollment and vacancy state remain distinct. For one vacancy, an
offer attempt has these states:

```text
OFFERED
  ├── ACCEPTED       slot filled; terminal
  ├── DECLINED       candidate excluded for this vacancy; advance
  ├── EXPIRED        candidate excluded for this vacancy; advance
  ├── CANCELLED      vacancy/Game no longer offerable; terminal
  └── INVALIDATED    candidate lost eligibility; advance when vacancy remains
```

Only one current offer exists for one vacancy in the recommended first design.
A declined or expired candidate may remain enrolled for other Games, but is
not offered the same vacancy again unless a future owner rule explicitly
permits a new attempt.

### State-machine pseudocode

The transaction, audit, and notification language below specifies required
future behavior. It does not claim that those mechanisms are implemented here.

Every command that starts from an `offer_id` resolves that offer's immutable
`vacancy_id` from stored state, never from request data. All commands then take
locks in one global order: vacancy first, offer/attempt second. Every
current-time observation and transition timestamp comes from one authoritative
clock abstraction owned by the command/store boundary. Database-backed stores
may source it from the database. A fake clock may be bound only by a test
fixture constructing an isolated Memory command/store. Future production
composition must bind its trusted authoritative source independently of
request, session, queue, and worker data. No externally decoded field or
externally callable command parameter may supply or override the clock or a
current-time scalar. Internal projection may receive only the scalar captured
by that boundary.

The future store contract must combine its authoritative-clock comparison,
complete eligibility/version check, and conditional state write in one atomic
primitive. Sampling time or eligibility in application code and writing later
does not satisfy this contract. For the portable Memory/SQLite/PostgreSQL
contract, “commit before `expires_at`” means that the transaction's atomic
terminal transition serializes before the deadline; an aborted transaction is
not an acceptance, while later storage flush, client acknowledgement, or
notification delivery is not a second eligibility event. Exactly at that
serialization point the atomic predicate selects `EXPIRED`. Next-attempt work
is recorded transactionally and consumed only after the terminal transaction
commits, never called synchronously while either lock is held.

Terminal state and response evidence are separate facts. An offer attempt
durably records at most one first authenticated response observation: response
kind (`ACCEPT` or `DECLINE`), server-attributed responder, and
`response_recorded_at` from the same authoritative clock. The conditional write
is atomic and idempotent. If the timeout command serializes first, the losing
authenticated response still records that evidence without changing the
`EXPIRED` state or creating another terminal audit or next-attempt intent.
Future reliability policy must use the observation and must not infer silence
from `EXPIRED` alone.

```text
open_or_advance(vacancy_id):
    require authorized workflow actor or system command
    atomically lock the still-open vacancy, then its current offer/attempt
    now = read the authoritative clock after locks are held
    project every eligibility and ranking fact with RankingRequest.as_of = now
    exclude candidates already declined, expired, or invalidated for vacancy
    ranking = pure rank(projected request, candidates, policy)

    if ranking.proposed is none:
        record vacancy remains unfilled with the ranking fingerprint
        return NO_CANDIDATE

    offered_at = now
    expires_at = min(offered_at + response_window, game_start)
    if expires_at <= offered_at:
        record no usable response window
        return NO_VALID_OFFER_WINDOW

    result = one atomic conditional-create primitive that, on success, stores
        the OFFERED row, candidate, ordinal, policy version, ranking fingerprint,
        explanation, offered_at, expires_at, validated projection/source
        versions, one server-attributed audit, and one notification intent;
        it may serialize successfully only if all of these remain true at its
        atomic transition point:
            - vacancy is open and has no current offer
            - authoritative transition time is before expires_at
            - selected candidate still passes every hard and competition-boundary gate
            - the complete projected ID/fact set, policy version, and ranking-relevant
              source versions equal those used to produce the ranking fingerprint
    if result says vacancy is no longer open:
        return the stored terminal vacancy outcome without creating an offer
    if result says a current offer already exists:
        return that stored current offer/idempotent outcome
    if result says deadline, projection, policy, or source version drifted:
        abort without an OFFERED row and return RETRY_REQUIRED; any later retry
        must recapture now, projection, ranking, offered_at, and expires_at
        rather than reuse a prior value
    return OFFERED after commit

accept(offer_id, authenticated_responder):
    resolve immutable vacancy_id from the stored offer
    atomically lock vacancy, then offer
    authorize player or verified guardian for the offered candidate
    if offer already has a terminal outcome:
        if outcome is EXPIRED, atomically record the first authenticated ACCEPT
            response observation using authoritative command time captured
            after locks are held
        return that stored outcome without another roster row, terminal audit,
            intent, or duplicate response observation
    require offer is OFFERED
    atomically record the first authenticated ACCEPT response observation,
        using the authoritative transition time
    atomically revalidate vacancy and complete candidate eligibility, then:
        - if the authoritative transition time is before expires_at, commit
          OFFERED -> ACCEPTED, fill exactly one vacancy, invalidate any
          competing stale attempt, and append one ACCEPTED audit
        - otherwise commit OFFERED -> EXPIRED, append one EXPIRED audit, and
          record one idempotent next-attempt intent; the separate response
          observation preserves that the candidate tried to accept
    return the committed terminal outcome

decline(offer_id, authenticated_responder):
    resolve immutable vacancy_id from the stored offer
    atomically lock vacancy, then offer
    authorize player or verified guardian for the offered candidate
    if offer already has a terminal outcome:
        if outcome is EXPIRED, atomically record the first authenticated DECLINE
            response observation using authoritative command time captured
            after locks are held
        return that stored outcome without another terminal audit, intent, or
            duplicate response observation
    require offer is OFFERED
    atomically record the first authenticated DECLINE response observation,
        using the authoritative transition time
    atomically read the authoritative transition time and:
        - before expires_at, commit OFFERED -> DECLINED with one DECLINED audit
        - at or after expires_at, commit OFFERED -> EXPIRED with one EXPIRED audit;
          the separate response observation preserves that the candidate declined
    record one idempotent next-attempt intent in the same transaction

expire(offer_id):
    resolve immutable vacancy_id from the stored offer
    atomically lock vacancy, then offer
    require authorized expiry worker or system command
    if offer already has a terminal outcome:
        return that stored outcome without another audit or intent
    atomically require offer is OFFERED and authoritative transition time is at or
        after expires_at, then commit OFFERED -> EXPIRED, one EXPIRED audit,
        and one idempotent next-attempt intent; do not fabricate a response
        observation

cancel_or_invalidate(offer_id, reason):
    resolve immutable vacancy_id from the stored offer
    atomically lock vacancy, then offer
    transition OFFERED -> CANCELLED or INVALIDATED
    append server-attributed audit
    record a next-attempt intent only if the Game and vacancy remain offerable
```

Accept-versus-expire, decline-versus-expire, and duplicate responder requests
must serialize on the same vacancy and offer in the global lock order. Exactly
one terminal transition wins; a retry returns the stored outcome without a
second roster row, terminal audit, or next-candidate action. A losing first
authenticated response may add only the one durable observation described
above; retries cannot replace or duplicate it.

Each new attempt takes a fresh projection and records a fresh ranking
fingerprint. That allows eligibility or policy changes to be explained rather
than silently replaying a stale candidate list.

## Override, authorization, and audit boundary

The pure ranking core does not authenticate, authorize, or append audit rows.
Those responsibilities stay at a future server-side command boundary:

- Resolve the actor from the authenticated session, never from request fields.
- Require the owner-approved manager/captain/admin capability for the exact
  Game, side, and vacancy.
- Permit an override to change ranking order only. It cannot bypass the
  goalie/skater gate, current membership eligibility, roster lock, filled-slot
  check, or an owner-approved competition boundary.
- Require a non-empty reason and record the engine proposal alongside the
  human selection.
- Revalidate authorization and candidate eligibility inside the same atomic
  operation that records the future selection/offer.

A future durable override audit should include actor and effective role,
Game/LeagueSeason/side/vacancy identifiers, selected candidate and membership,
engine proposal, policy version, ranking fingerprint, reason, authoritative
transition time, and correlation/idempotency key. This is an audit contract
only; no audit schema or write is introduced here.

## Fixed design test vectors

Times below are UTC. Unless stated otherwise, candidates share the same
eligible `LeagueSeason`, have zero completed substitute appearances, require
no notice, and have no override. `F/D` means a skater who supports both forward
and defence.

| ID | Inputs | Policy | Expected result | Authority |
| --- | --- | --- | --- | --- |
| V1 goalie hard gate | Need `G`; `g1(G, rating 3, completed 4)` and `f1(F, rating 4, completed 0)` | `FAIRNESS, SKILL_MATCH` | `g1` is the only ranked candidate; `f1` is rejected by `goalie_separation`. | Settled core invariant |
| V2 skater position preference | Need `F`; `a(F)`, `b(F/D)`, `c(D)`, otherwise equal | `POSITION_PREFERENCE` | Order `a, b, c`. | Decided #287 rule |
| V3 seasonal fairness | `a(completed 1)`, `b(completed 3)` | `FAIRNESS` | Order `a, b`. Counts are supplied facts derived from finalized participation in the current `LeagueSeason`; a new LeagueSeason resets the count. | Owner-approved Q1/Q2 (2026-08-29) |
| V4 symmetric skill | Need rating 4; `a(rating 3)`, `b(rating 5)` | `SKILL_MATCH` | Equal skill distance; with random disabled, canonical ID fallback gives `a, b`. | Settled ruling |
| V5 unrated last | Need rating 4; `a(rating 1)`, `b(unrated)` | `SKILL_MATCH` | `a, b` even though `a` is not a close match. `b` remains in the pool. | Settled ruling |
| V6 all unrated | `a(unrated, completed 2)`, `b(unrated, completed 0)` | `SKILL_MATCH, FAIRNESS` | Skill contributes equality; order `b, a` by fairness. | Settled ruling |
| V7 notice boundary | Server-captured `as_of` is `18:00`, start `19:00`; `a(minimum 60m)`, `b(minimum 61m)` | Any | `a` eligible by inclusive equality; `b` is rejected by `notice_window`. The same `as_of` becomes `offered_at` if an offer is created. | Settled ruling |
| V8 configurable priority | Need rating 4; `a(rating 1, completed 0)`, `b(rating 4, completed 5)` | A: `FAIRNESS, SKILL_MATCH`; B: `SKILL_MATCH, FAIRNESS` | Policy A proposes `a`; policy B proposes `b`. | Decided #287 rule |
| V9 input permutation | V8 candidates supplied as `[a,b]` and `[b,a]` with the same policy/seed | Either V8 policy | Byte-equivalent ordered IDs, proposal, traces, and explanation. | Determinism contract |
| V10 override | Engine order `a,b`; validated override selects `b` | Any | `proposed=a`, `selected=b`, no deciding rule credited for the human choice; explanation names override and preserves proposal. | Override design boundary |
| V11 approved boundary | Game is in League `l1`, LeagueSeason `ls1`, Division `d1`; `a` is in `d1`; `b` is in `d2` of `ls1`; `c` is in another League; `d` is in sibling LeagueSeason `ls2` of `l1`; evaluate with cross-Division policy OFF then ON | Any | OFF: only `a` reaches ranking. ON: `a,b` reach ranking. `c` and `d` are refused in both cases because cross-League substitution remains off and LeagueSeason must match exactly. | Owner-approved Q4 (2026-08-29) |
| V12 decline advance | Rank `a,b`; offer `a`; authenticated `a` declines | Any | Attempt 1 becomes `DECLINED`; attempt 2 offers `b` with ordinal 2 and a fresh fingerprint. | State-machine design |
| V13 timeout boundary | Offer at `18:50`, start `19:00`, response window 30m; accept while timeout evaluates at `19:00` | Any | Approved expiry is `19:00`; an acceptance terminal transition serialized at `18:59:59` may win. At `19:00`, the outcome is `EXPIRED` with one EXPIRED audit and one next-attempt intent, while the separate first-response observation preserves `response_kind=ACCEPT` regardless of whether accept or timeout serialized first. Repeated expiry and accept commands are idempotent. | Owner-approved Q5 (2026-08-29) |
| V14 accept/expire race | One process accepts before expiry while another evaluates timeout | Any | Row locking/conditional transition permits exactly one terminal result, one roster fill at most, and no duplicate advance. | Future concurrency contract |
| V15 no candidate | Every projected candidate fails goalie/skater or notice gate | Any | No proposal; explanation lists named rejections and vacancy remains unfilled. | Core/state boundary |
| V16 decline at deadline | One process declines while another evaluates timeout at exactly `expires_at` | Any | The single terminal result is `EXPIRED`, never `DECLINED`; one next-attempt intent is recorded atomically and consumed only after commit. In either serialization order the first-response observation preserves `response_kind=DECLINE` exactly once, so future reliability logic can distinguish the response from silence without changing the expiry winner. | Owner-approved Q5 / future concurrency contract |
| V17 League rating authority | In League `l1`, canonical effective rating is 3; coach recommends 6; player self-rates 7; legacy global rating is 5 | `SKILL_MATCH` | Future #273 projection supplies 3 with League-admin/effective-time audit provenance. Recommendation, self-rating, and legacy global value do not replace it. Before #273 exists, production projection supplies unrated instead. | Owner-approved Q3 (2026-08-29) |
| V18 participation-only fairness | Current LeagueSeason history contains one finalized Game with a recorded occupying/participating roster row, one accepted-but-cancelled Game, one accepted but scheduled-and-unplayed Game, and one no-show | `FAIRNESS` | Completed-substitute count is 1. The acceptance, cancellation, scheduled-but-unplayed Game, and no-show are excluded. | Owner-approved Q1/Q2 (2026-08-29) |
| V19 no late offer | Authoritative `as_of`/`offered_at` equals or follows `game_start`; response window is positive | Any | `expires_at <= offered_at`; no OFFERED row, offer audit, or notification intent is created and the result is `NO_VALID_OFFER_WINDOW`. A server-attributed audit of that refusal may still be recorded. | Owner-approved Q5 (2026-08-29) |
| V20 terminal retry | (a) An authorized responder repeats accept or decline after its command reached a terminal outcome; (b) timeout already set `EXPIRED`, then the responder submits its first late response and repeats it | Any | The stored terminal outcome is returned with no second roster row, terminal audit, or next-attempt intent. In (a), the existing response observation is unchanged. In (b), the first authenticated late response creates the one durable observation; the repeat cannot replace or duplicate it. | Idempotency/concurrency contract |

The vectors above record design contracts, including the five owner-approved
policy rulings. They do not claim that production integration exists.

## Preconditions for a later implementation slice

Before any production integration is proposed:

1. Later slices must implement the five approved rulings exactly; an
   alternative requires a new explicit owner decision.
2. #273 must implement the approved League-context skill ownership and its
   privacy/effective-time/audit rules, or production integration must
   explicitly operate with every candidate unrated.
3. The exact #205 eligibility resolver and competition-boundary policy must be
   reused rather than copied.
4. Policy persistence, API contracts, notification delivery, timeout workers,
   authorization, audit durability, migrations, rollback, and tri-store
   concurrency require their own authorized slices and acceptance evidence.
5. Desktop, 390px, keyboard, screen-reader, empty/error/timeout, and
   "why this player" prototype journeys remain a separate non-production
   design deliverable.

Nothing in this document authorizes merge or runtime behavior.
