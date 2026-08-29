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
| Notice anchor | Remaining notice is measured to `game_start`, not `roster_lock_time`, and the comparison is inclusive. | `game_start - decision_at == minimum_notice` is eligible; one unit less is ineligible. |

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
- explicit `decision_at` and `game_start` timestamps;
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
- Timestamps are timezone-aware and supplied by the caller.
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
        if request.game_start - request.decision_at < candidate.minimum_notice:
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
project_candidates(game_id, vacancy, authenticated_scope, decision_at):
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

```text
open_or_advance(vacancy_id, decision_at):
    require authorized workflow actor or system command
    atomically read the still-open vacancy and current attempt ordinal
    project currently eligible candidates
    exclude candidates already declined, expired, or invalidated for vacancy
    ranking = pure rank(projected request, candidates, policy)

    if ranking.proposed is none:
        record vacancy remains unfilled with the ranking fingerprint
        return NO_CANDIDATE

    offered_at = capture server time inside the transaction
    expires_at = min(offered_at + response_window, game_start)
    if expires_at <= offered_at:
        record no usable response window
        return NO_VALID_OFFER_WINDOW

    create OFFERED attempt with candidate, ordinal, policy version,
        ranking fingerprint, explanation, offered_at, expires_at
    append server-attributed audit
    record typed notification intent for a later authorized delivery slice
    return OFFERED

accept(offer_id, authenticated_responder, accepted_at):
    authorize player or verified guardian for the offered candidate
    atomically lock offer and vacancy
    require offer is OFFERED and accepted_at < expires_at
    revalidate vacancy is open and candidate remains eligible
    transition offer to ACCEPTED and fill exactly one vacancy
    invalidate any competing stale attempt
    append server-attributed audit

decline(offer_id, authenticated_responder, declined_at):
    authorize player or verified guardian for the offered candidate
    atomically transition OFFERED -> DECLINED exactly once
    append server-attributed audit
    enqueue idempotent open_or_advance for the next ordinal

expire(offer_id, observed_at):
    atomically require offer is OFFERED and observed_at >= expires_at
    transition OFFERED -> EXPIRED exactly once
    append server-attributed audit
    enqueue idempotent open_or_advance for the next ordinal

cancel_or_invalidate(offer_id, reason):
    atomically transition OFFERED -> CANCELLED or INVALIDATED
    append server-attributed audit
    advance only if the Game and vacancy remain offerable
```

Accept-versus-expire and two accept requests must serialize on the same offer
and vacancy. Exactly one terminal transition wins; a retry returns the stored
outcome without a second roster row, audit event, or next-candidate action.

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
engine proposal, policy version, ranking fingerprint, reason, server time, and
correlation/idempotency key. This is an audit contract only; no audit schema or
write is introduced here.

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
| V7 notice boundary | Decision `18:00`, start `19:00`; `a(minimum 60m)`, `b(minimum 61m)` | Any | `a` eligible by inclusive equality; `b` is rejected by `notice_window`. | Settled ruling |
| V8 configurable priority | Need rating 4; `a(rating 1, completed 0)`, `b(rating 4, completed 5)` | A: `FAIRNESS, SKILL_MATCH`; B: `SKILL_MATCH, FAIRNESS` | Policy A proposes `a`; policy B proposes `b`. | Decided #287 rule |
| V9 input permutation | V8 candidates supplied as `[a,b]` and `[b,a]` with the same policy/seed | Either V8 policy | Byte-equivalent ordered IDs, proposal, traces, and explanation. | Determinism contract |
| V10 override | Engine order `a,b`; validated override selects `b` | Any | `proposed=a`, `selected=b`, no deciding rule credited for the human choice; explanation names override and preserves proposal. | Override design boundary |
| V11 approved boundary | Game is `ls1`; `a` has eligible membership in `ls1`; `b` only in sibling `ls2` | Any | Under the approved rule, only `a` reaches ranking. | Owner-approved Q4 (2026-08-29) |
| V12 decline advance | Rank `a,b`; offer `a`; authenticated `a` declines | Any | Attempt 1 becomes `DECLINED`; attempt 2 offers `b` with ordinal 2 and a fresh fingerprint. | State-machine design |
| V13 timeout boundary | Offer at `18:50`, start `19:00`, response window 30m | Any | Approved expiry is `19:00`; accept at `18:59:59` may win, accept at `19:00` cannot; repeated expiry is idempotent. | Owner-approved Q5 (2026-08-29) |
| V14 accept/expire race | One process accepts before expiry while another evaluates timeout | Any | Row locking/conditional transition permits exactly one terminal result, one roster fill at most, and no duplicate advance. | Future concurrency contract |
| V15 no candidate | Every projected candidate fails goalie/skater or notice gate | Any | No proposal; explanation lists named rejections and vacancy remains unfilled. | Core/state boundary |

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
