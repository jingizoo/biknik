"""Substitute matching engine — ranking prototype (issue #287, slice 2).

PROTOTYPE SCOPE. This module implements **only** deterministic eligibility,
ranking and explainable selection as pure functions over plain projected data.

Deliberately NOT here (they land after #205 / SeasonRosterMembership):
  * the offer / accept / decline / timeout workflow,
  * any schema, migration, store or HTTP route,
  * any dependency on ``domain.models.Player`` or ``SubstituteEnrollment``.

The engine takes *plain projected data* rather than the persistence-shaped
domain models on purpose: #205 will reshape those models, and a prototype that
never imports them survives that reshaping unchanged. Only the pure enums
``Position`` and ``SlotType`` are reused, because they carry no persistence
shape and the rest of the codebase already speaks them.

Non-negotiable properties, and where they are enforced:

  DETERMINISTIC  Every comparison key is an ``int``; the composite sort key ends
                 in ``player_id`` so the ordering is total and shuffling the
                 input candidate list cannot change the output. Randomness, when
                 a League enables it, is a pure function of ``(seed, player_id)``
                 — never a draw consumed by walking the candidate list.
  EXPLAINABLE    Every eligible candidate carries one ``RuleTrace`` per enabled
                 rule (naming the rule, its priority and its raw value); every
                 excluded candidate carries every ``Exclusion`` it earned.
  PURE           No I/O, no clock (``as_of`` is passed in, per CLAUDE.md), no
                 module-level RNG, no global state.
  ONE AXIS       A vacancy names ``needed_position`` and nothing else; the
                 goalie/skater ``slot_type`` is derived from it. There is no
                 second field to contradict the first, so a request that is a
                 goalie slot for a forward cannot be constructed.
  ATTRIBUTED     ``deciding_rule`` names a rule only when that rule is
                 COUNTERFACTUALLY load-bearing — remove it and the proposal
                 changes. See ``SubstituteRanking`` for the exact definition.
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional, Sequence

from ..domain.enums import Position, SlotType

__all__ = [
    "PolicyError",
    "GateKind",
    "RuleKind",
    "RuleSetting",
    "LeagueRankingPolicy",
    "SubstituteRequest",
    "SubstituteCandidate",
    "SelectionOverride",
    "RuleTrace",
    "Exclusion",
    "CandidateEvaluation",
    "SubstituteRanking",
    "rank_substitutes",
    "GATE_ORDER",
    "MIN_SKILL",
    "MAX_SKILL",
]


MIN_SKILL = 1
MAX_SKILL = 7

# Worst possible skill distance on the 1..7 scale. Used when a skill is unknown
# (see ASSUMPTION "unknown skill" below).
_MAX_SKILL_DISTANCE = MAX_SKILL - MIN_SKILL

# Bucket size for the seeded random draw. Collisions are harmless: the trailing
# player_id in the composite key still yields a total order.
_RANDOM_BUCKETS = 1_000_000


class PolicyError(ValueError):
    """A programmer / configuration error in the ranking inputs.

    Raised only for malformed configuration or malformed candidate data. It is
    *never* raised because nobody is eligible — an empty eligible pool is a
    normal result (see ``rank_substitutes``), matching CLAUDE.md's "the facade
    returns structured errors, not exceptions across the boundary".
    """


class GateKind(Enum):
    """Hard exclusions. A gate answers a boolean question, so it is not part of
    the League-configurable priority list — see ``GATE_ORDER``."""

    GOALIE_SEPARATION = "goalie_separation"
    NOTICE_WINDOW = "notice_window"


class RuleKind(Enum):
    """Ranking rules. Order within a ``LeagueRankingPolicy`` *is* priority."""

    FAIRNESS = "fairness"
    SKILL_MATCH = "skill_match"
    POSITION_PREFERENCE = "position_preference"
    RANDOM = "random"


# Gate evaluation order is a fixed module constant, NOT League-configurable.
# For a boolean filter, priority changes nothing about *who* is excluded — only
# the reading order of the reasons — so making it configurable would be a false
# affordance.
GATE_ORDER: tuple[GateKind, ...] = (
    GateKind.GOALIE_SEPARATION,
    GateKind.NOTICE_WINDOW,
)


# --------------------------------------------------------------------------
# Inputs
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class RuleSetting:
    """One ranking rule as configured by a League."""

    kind: RuleKind
    enabled: bool = True
    # No parameters in the prototype. The extension point is named here only so
    # a later slice can add weights/thresholds without reshaping the tuple.
    # ASSUMPTION (pending owner ruling): no League needs per-rule parameters in
    # the prototype; the conservative reading of "enable and prioritize" is that
    # a rule is on or off and carries a position, nothing more.


@dataclass(frozen=True)
class LeagueRankingPolicy:
    """Per-League control over which rules apply and in what order.

    ``rules`` order == priority: ``rules[0]`` is the primary sort key. This is
    what makes rule order DATA rather than something hardcoded in the engine.
    """

    league_id: str
    rules: tuple[RuleSetting, ...]
    # The one configurable gate. GOALIE_SEPARATION is deliberately absent: it is
    # always on and cannot be disabled or outranked by reordering rules.
    notice_window_enabled: bool = True
    # REQUIRED iff a RANDOM rule is enabled. This is how "unseeded is impossible
    # by construction" is enforced: a policy with an unseeded RANDOM rule cannot
    # be constructed, so the engine has no unseeded code path at all.
    random_seed: Optional[int] = None

    def __post_init__(self) -> None:
        if not self.rules:
            raise PolicyError("league policy must configure at least one rule")

        seen: set[RuleKind] = set()
        for setting in self.rules:
            if not isinstance(setting, RuleSetting):
                raise PolicyError(f"rules must contain RuleSetting, got {setting!r}")
            if setting.kind in seen:
                raise PolicyError(
                    f"duplicate rule {setting.kind.name} in league policy "
                    f"{self.league_id}; each rule may appear at most once"
                )
            seen.add(setting.kind)

        enabled = self.enabled_rules
        if not enabled:
            raise PolicyError(
                f"league policy {self.league_id} has zero enabled rules; "
                "ranking would be arbitrary"
            )

        if RuleKind.RANDOM in enabled and self.random_seed is None:
            raise PolicyError(
                "RANDOM rule is enabled but random_seed is None; an unseeded "
                "draw is not reproducible and is refused by construction"
            )

    @property
    def enabled_rules(self) -> tuple[RuleKind, ...]:
        """Enabled rule kinds in priority order. Disabled rules are *absent*,
        not zero-weighted — omission is how "genuinely skipped" is provable."""
        return tuple(s.kind for s in self.rules if s.enabled)

    @property
    def gates_applied(self) -> tuple[GateKind, ...]:
        return tuple(
            gate
            for gate in GATE_ORDER
            if gate is not GateKind.NOTICE_WINDOW or self.notice_window_enabled
        )


@dataclass(frozen=True)
class SubstituteRequest:
    """The vacancy being filled.

    ONE POSITION AXIS. ``needed_position`` is the only stored fact about what
    the vacancy needs; ``slot_type`` is DERIVED from it. An earlier shape stored
    both, which made the inconsistent state ``slot_type=SKATER`` +
    ``needed_position=GOALIE`` (and its mirror) representable — and since the
    goalie gate judges the slot while the position rule judges the position,
    such a request would let a goalie reach a skater slot and a skater a goalie
    vacancy. That is not fixed with a guard clause: two fields for one fact are
    the defect. With ``slot_type`` gone from the constructor there is no second
    field to disagree with, so the inconsistent pair cannot be built at all —
    passing ``slot_type=`` raises ``TypeError``, and the derived property has no
    setter on a frozen dataclass.
    """

    request_id: str
    needed_position: Position
    absent_skill: Optional[int]
    game_start: datetime  # tz-aware UTC
    as_of: datetime  # tz-aware UTC; the explicit "now" — never read from a clock

    @property
    def slot_type(self) -> SlotType:
        """The slot this vacancy is, derived from the position it needs.

        Reuses ``Position.slot_type`` (GOALIE -> goalie slot, everything else ->
        skater slot) rather than restating the mapping, so the engine and the
        rest of the codebase cannot drift apart on what a goalie slot is.
        """
        return self.needed_position.slot_type

    @property
    def minutes_remaining(self) -> int:
        """Whole minutes of notice left before puck drop.

        ASSUMPTION (pending owner ruling): a partial minute does not count as
        available notice, so this floors. Flooring is the conservative side —
        it can only shrink the window, never claim notice that is not there.
        Negative when the game has already started, which excludes everyone who
        requires any notice at all.
        """
        return math.floor((self.game_start - self.as_of).total_seconds() / 60)


@dataclass(frozen=True)
class SubstituteCandidate:
    """One player projected out of whatever the substitute pool turns out to be.

    ``completed_sub_games`` is already computed by the caller; what counts as a
    completed substitute game (this season? this League? all time?) is the
    caller's decision.
    ASSUMPTION (pending owner ruling): the engine does not define that scope and
    must not — narrowing it here would silently pick a fairness horizon the
    owner has not chosen.
    """

    player_id: str
    primary_position: Position
    also_plays: frozenset[Position] = field(default_factory=frozenset)
    skill: Optional[int] = None
    completed_sub_games: int = 0
    min_notice_minutes: int = 0

    @property
    def positions(self) -> frozenset[Position]:
        return frozenset({self.primary_position}) | self.also_plays

    @property
    def is_goalie(self) -> bool:
        """ASSUMPTION (pending owner ruling): a player registered for goalie in
        *any* capacity is treated as a goalie and is therefore barred from
        skater slots. That is the strictest reading of "goalie is strictly
        separate"; the alternative (dual-registration players float between
        pools) weakens a rule the issue calls a hard exclusion."""
        return Position.GOALIE in self.positions

    @property
    def plays_both(self) -> bool:
        return {Position.FORWARD, Position.DEFENSE} <= set(self.positions)


@dataclass(frozen=True)
class SelectionOverride:
    """An authorized manager/captain overriding the engine's proposal.

    Modelled only. No authorization logic lives here — whether ``actor_id`` may
    override is a question for the service/api layer, after #205.
    """

    player_id: str
    actor_id: str
    reason: str


# --------------------------------------------------------------------------
# Outputs
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class RuleTrace:
    """Why a rule scored a candidate the way it did. One per enabled rule."""

    rule: RuleKind
    priority: int  # index in policy.rules — makes "which rule outranked which" readable
    value: int  # the raw comparison key; LOWER IS BETTER, uniformly
    detail: str


@dataclass(frozen=True)
class Exclusion:
    gate: GateKind
    detail: str


@dataclass(frozen=True)
class CandidateEvaluation:
    player_id: str
    eligible: bool
    exclusions: tuple[Exclusion, ...] = ()
    rule_traces: tuple[RuleTrace, ...] = ()
    sort_key: tuple = ()
    rank: Optional[int] = None


@dataclass(frozen=True)
class SubstituteRanking:
    """The ranking plus the "why this player" surface.

    DEFINITION — ``proposal_deciding_rules``
        Every enabled rule that is COUNTERFACTUALLY LOAD-BEARING for
        ``proposed_player_id``, in policy priority order. A rule is
        load-bearing exactly when deleting it from the policy — leaving every
        other rule, its order, the same pool and the same gates untouched —
        makes the engine propose a DIFFERENT player. Nothing else qualifies.

        This is not "the first rule where the winner beat the runner-up". That
        earlier reading was wrong in both directions:
          * UNDERCLAIM — it compares rank 1 against rank 2 only, so a rule that
            already knocked the real challenger down to rank 3 never gets
            named, even though deleting it changes the winner.
          * OVERCLAIM — it credits the first rule that happened to separate the
            top two even when the winner would still win without that rule
            (an overdetermined win), which reads causally and is not.
        The counterfactual is the definition a manager is actually reading off
        the line: "if the League turned this rule off, would we be calling a
        different player?"

    DEFINITION — ``deciding_rule``
        The single rule that decided ``selected_player_id``:
          * with no override: ``proposal_deciding_rules[0]`` — the
            highest-priority load-bearing rule — or ``None`` when the tuple is
            empty (a sole eligible candidate, an all-rule tie broken only by
            player id, or an overdetermined win where no ONE rule is
            load-bearing);
          * with an override: always ``None``. A human chose the selected
            player, so no rule decided it. Crediting a rule here produced an
            actively false pair — e.g. FAIRNESS named for a selection that
            FAIRNESS ranked last — for any consumer binding
            ``(selected_player_id, deciding_rule)`` together. The proposal's
            own attribution is still available in ``proposal_deciding_rules``
            and is labelled as the (unselected) proposal in the summary.

    ``decision_summary`` restates the same facts in one line, naming EVERY
    load-bearing rule with the winner's value and the specific rival that rule
    displaced, and leading with the override when there is one.
    """

    league_id: str
    request_id: str
    rules_applied: tuple[RuleKind, ...]
    gates_applied: tuple[GateKind, ...]
    ranked: tuple[CandidateEvaluation, ...]
    excluded: tuple[CandidateEvaluation, ...]
    proposed_player_id: Optional[str]
    deciding_rule: Optional[RuleKind]
    proposal_deciding_rules: tuple[RuleKind, ...]
    decision_summary: str
    override: Optional[SelectionOverride] = None
    override_conflicts: tuple[Exclusion, ...] = ()
    selected_player_id: Optional[str] = None


# --------------------------------------------------------------------------
# Stage 1 — validate
# --------------------------------------------------------------------------


def _validate(
    request: SubstituteRequest,
    candidates: Sequence[SubstituteCandidate],
    policy: LeagueRankingPolicy,
) -> None:
    """Programmer/config errors only. Never raised for "nobody is eligible"."""
    if request.absent_skill is not None and not (
        MIN_SKILL <= request.absent_skill <= MAX_SKILL
    ):
        raise PolicyError(
            f"absent_skill {request.absent_skill} outside {MIN_SKILL}..{MAX_SKILL}"
        )

    seen: set[str] = set()
    for candidate in candidates:
        if candidate.player_id in seen:
            raise PolicyError(
                f"duplicate player_id {candidate.player_id!r} in candidate pool"
            )
        seen.add(candidate.player_id)
        if candidate.skill is not None and not (
            MIN_SKILL <= candidate.skill <= MAX_SKILL
        ):
            raise PolicyError(
                f"candidate {candidate.player_id} skill {candidate.skill} outside "
                f"{MIN_SKILL}..{MAX_SKILL}"
            )
        if candidate.min_notice_minutes < 0:
            raise PolicyError(
                f"candidate {candidate.player_id} min_notice_minutes "
                f"{candidate.min_notice_minutes} is negative"
            )


# --------------------------------------------------------------------------
# Stage 2 — gates (hard exclusions)
# --------------------------------------------------------------------------


def _goalie_separation(
    request: SubstituteRequest, candidate: SubstituteCandidate
) -> Optional[Exclusion]:
    """Bidirectional, always on, not League-configurable.

    Modelled as a gate rather than a very large POSITION_PREFERENCE penalty so
    that no League can disable it or outrank it by reordering its rule list.

    ``request.slot_type`` is derived from ``request.needed_position``, so the
    fact this gate judges and the fact POSITION_PREFERENCE judges are the same
    fact. There is no second field that could say "skater slot" while the
    vacancy is a goalie vacancy.
    """
    slot_is_goalie = request.slot_type is SlotType.GOALIE
    if candidate.is_goalie == slot_is_goalie:
        return None
    if candidate.is_goalie:
        detail = "goalie may not fill a skater slot"
    else:
        detail = "skater may not fill a goalie slot"
    return Exclusion(gate=GateKind.GOALIE_SEPARATION, detail=detail)


def _notice_window(
    request: SubstituteRequest, candidate: SubstituteCandidate
) -> Optional[Exclusion]:
    """Exclude candidates needing more notice than the time remaining.

    Comparison is inclusive: exactly-enough notice is ELIGIBLE, one minute short
    is not.
    """
    remaining = request.minutes_remaining
    if remaining >= candidate.min_notice_minutes:
        return None
    return Exclusion(
        gate=GateKind.NOTICE_WINDOW,
        detail=(
            f"needs {candidate.min_notice_minutes} min notice, "
            f"{remaining} min remain"
        ),
    )


def _apply_gates(
    request: SubstituteRequest,
    candidate: SubstituteCandidate,
    policy: LeagueRankingPolicy,
) -> tuple[Exclusion, ...]:
    """Evaluate EVERY enabled gate — no short-circuiting — so a candidate who
    fails both gates reports both reasons."""
    exclusions: list[Exclusion] = []
    for gate in policy.gates_applied:
        if gate is GateKind.GOALIE_SEPARATION:
            found = _goalie_separation(request, candidate)
        elif gate is GateKind.NOTICE_WINDOW:
            found = _notice_window(request, candidate)
        else:  # pragma: no cover — defensive; GATE_ORDER is exhaustive
            raise PolicyError(f"unknown gate {gate!r}")
        if found is not None:
            exclusions.append(found)
    return tuple(exclusions)


# --------------------------------------------------------------------------
# Stage 3 — score + sort
# --------------------------------------------------------------------------


def _score_fairness(
    request: SubstituteRequest,
    candidate: SubstituteCandidate,
    policy: LeagueRankingPolicy,
) -> tuple[int, str]:
    count = candidate.completed_sub_games
    plural = "" if count == 1 else "s"
    return count, f"{count} completed substitute game{plural}"


def _score_skill_match(
    request: SubstituteRequest,
    candidate: SubstituteCandidate,
    policy: LeagueRankingPolicy,
) -> tuple[int, str]:
    if request.absent_skill is None or candidate.skill is None:
        # ASSUMPTION (pending owner ruling): an unknown skill scores the worst
        # possible distance rather than a neutral 0. Treating "unknown" as a
        # perfect match would let missing data win a slot, which is the opposite
        # of conservative.
        known = (
            "absent player skill unknown"
            if request.absent_skill is None
            else "candidate skill unknown"
        )
        return _MAX_SKILL_DISTANCE, f"{known} (scored as worst distance {_MAX_SKILL_DISTANCE})"
    distance = abs(candidate.skill - request.absent_skill)
    # Symmetric on purpose: against an absent 4, candidates 3 and 5 tie.
    return distance, f"skill {candidate.skill} vs {request.absent_skill} (distance {distance})"


def _score_position_preference(
    request: SubstituteRequest,
    candidate: SubstituteCandidate,
    policy: LeagueRankingPolicy,
) -> tuple[int, str]:
    """0 = exact position, 1 = plays both forward and defence, 2 = alternate.

    Goalies never reach this rule — stage 2 already removed every goalie/skater
    mismatch — so the goalie wall is structural, not a large penalty number.

    ASSUMPTION (pending owner ruling): "exact" means the candidate's PRIMARY
    position is the needed one. Reading "exact" as "the needed position appears
    anywhere in their positions" would make the middle tier unreachable — a
    player who plays both forward and defence always contains the needed skater
    position — collapsing the issue's three-tier preference into two. Primary-
    position-is-exact keeps all three tiers distinct and preserves the issue's
    stated order: exact > both > alternate.
    """
    if request.slot_type is SlotType.GOALIE:
        return 0, "goalie filling a goalie slot"
    if candidate.primary_position is request.needed_position:
        return 0, f"exact position ({request.needed_position.value})"
    if candidate.plays_both:
        return 1, "plays both forward and defence"
    # ASSUMPTION (pending owner ruling): a candidate whose position is the
    # generic Position.SKATER (or otherwise does not name the needed position)
    # lands in the alternate tier. Unspecified is not evidence of versatility.
    return 2, (
        f"alternate position ({candidate.primary_position.value} "
        f"for a {request.needed_position.value} slot)"
    )


def _score_random(
    request: SubstituteRequest,
    candidate: SubstituteCandidate,
    policy: LeagueRankingPolicy,
) -> tuple[int, str]:
    """A seeded draw that is a pure FUNCTION OF (seed, request, player_id).

    This is the crux of shuffle-invariance: any RNG consumed by walking the
    candidate list makes the output depend on input order. Per-player derivation
    cannot. A cryptographic digest is used rather than ``random.Random`` so the
    value is stable across processes and Python builds (``hash()`` of a str is
    salted per process).
    """
    seed = policy.random_seed
    if seed is None:  # pragma: no cover — unreachable: refused in __post_init__
        raise PolicyError("RANDOM rule reached with no seed")
    material = f"{seed}:{request.request_id}:{candidate.player_id}".encode("utf-8")
    digest = hashlib.sha256(material).digest()
    value = int.from_bytes(digest[:8], "big") % _RANDOM_BUCKETS
    return value, f"seeded draw {value} (seed {seed})"


_SCORERS = {
    RuleKind.FAIRNESS: _score_fairness,
    RuleKind.SKILL_MATCH: _score_skill_match,
    RuleKind.POSITION_PREFERENCE: _score_position_preference,
    RuleKind.RANDOM: _score_random,
}


def _trace(
    request: SubstituteRequest,
    candidate: SubstituteCandidate,
    policy: LeagueRankingPolicy,
) -> tuple[RuleTrace, ...]:
    """One RuleTrace per ENABLED rule, in policy priority order.

    ``priority`` is the rule's index in ``policy.rules`` (its configured
    position), so a reader can see which rule outranked which even when earlier
    rules are disabled.
    """
    traces: list[RuleTrace] = []
    for index, setting in enumerate(policy.rules):
        if not setting.enabled:
            continue  # omitted entirely — disabled is absent, not zero-weighted
        value, detail = _SCORERS[setting.kind](request, candidate, policy)
        traces.append(
            RuleTrace(rule=setting.kind, priority=index, value=value, detail=detail)
        )
    return tuple(traces)


def _sort_key(player_id: str, traces: Sequence[RuleTrace]) -> tuple:
    """Composite key: every enabled rule's value in priority order, then the
    player_id as the last-resort total-order tiebreak.

    Because this is a single lexicographic sort, "League-configured rule ORDER
    changes the winner" falls out of tuple ordering with no branching in the
    engine — and the trailing player_id means shuffling the input cannot change
    the result.
    """
    return tuple(trace.value for trace in traces) + (player_id,)


# --------------------------------------------------------------------------
# Stage 4 — explain + override
# --------------------------------------------------------------------------


def _without(key: tuple, index: int) -> tuple:
    """``key`` with the value at ``index`` deleted — the sort key the engine
    would have produced had that rule not been configured at all.

    Deleting rather than neutralising matters: a zero-weighted rule still
    occupies a position and can still tie-break, so it would not answer the
    counterfactual "what if this rule were not in the policy?".
    """
    return key[:index] + key[index + 1 :]


def _load_bearing_rules(
    ranked: Sequence[CandidateEvaluation],
) -> tuple[tuple[int, str], ...]:
    """The counterfactually load-bearing rules for ``ranked[0]``.

    Returns ``(trace_index, displaced_player_id)`` pairs in priority order: one
    per enabled rule whose deletion changes the proposal, naming the player who
    would be proposed instead. Empty when no single rule is load-bearing.

    Why this is exact rather than a heuristic: every sort key ends in the
    player_id, so each reduced key is still a TOTAL order and "who wins without
    rule i" is a plain minimum — the same answer a full re-rank with that rule
    removed would give (the gates and every other rule's value are unchanged by
    dropping a rule from the key).
    """
    if len(ranked) < 2:
        # Nobody to displace: with one eligible candidate — or none — no rule
        # can change who is proposed, so no rule decided anything.
        return ()

    winner = ranked[0]
    decisions: list[tuple[int, str]] = []
    for index in range(len(winner.rule_traces)):
        reduced_winner = min(
            ranked, key=lambda evaluation: _without(evaluation.sort_key, index)
        )
        if reduced_winner.player_id != winner.player_id:
            decisions.append((index, reduced_winner.player_id))
    return tuple(decisions)


def _proposal_summary(
    ranked: Sequence[CandidateEvaluation],
    decisions: Sequence[tuple[int, str]],
) -> str:
    """One line for the proposal, naming EVERY load-bearing rule.

    Naming only the first would reproduce the underclaim the counterfactual
    definition exists to remove: when two rules are each load-bearing, a reader
    told about one of them cannot answer "why not the other player?".
    """
    if not ranked:
        return "no eligible candidate"

    winner = ranked[0]
    if len(ranked) == 1:
        return f"{winner.player_id} proposed: sole eligible candidate"

    runner_up = ranked[1]
    if decisions:
        by_id = {evaluation.player_id: evaluation for evaluation in ranked}
        clauses = []
        for index, displaced_id in decisions:
            mine = winner.rule_traces[index]
            theirs = by_id[displaced_id].rule_traces[index]
            clauses.append(
                f"{mine.rule.name} ({mine.detail} vs {displaced_id}: {theirs.detail})"
            )
        return f"{winner.player_id} proposed: " + ", then ".join(clauses)

    tied_on_every_rule = [trace.value for trace in winner.rule_traces] == [
        trace.value for trace in runner_up.rule_traces
    ]
    if tied_on_every_rule:
        return (
            f"{winner.player_id} proposed: no rule separated them from "
            f"{runner_up.player_id}; broke the tie on player id"
        )
    # Rules did separate them, but no single rule is load-bearing: the win is
    # overdetermined. Saying "RULE X decided it" here would be an overclaim.
    return (
        f"{winner.player_id} proposed: no single rule decided it; "
        f"{winner.player_id} still ranks first with any one rule removed"
    )


def _override_conflicts(
    request: SubstituteRequest,
    candidates: Sequence[SubstituteCandidate],
    policy: LeagueRankingPolicy,
    override: SelectionOverride,
) -> tuple[tuple[Exclusion, ...], str]:
    """Gates the overridden player fails, plus the summary HEAD.

    The override is always honoured (CLAUDE.md: "coach override always wins");
    conflicts are reported, not enforced. Authorization is not this module's job.

    The fragment is the head of the summary rather than a clause appended after
    the proposal's explanation: the selected player IS the override target, so
    the line has to open with the player who was actually chosen and who chose
    them. Leading with the engine's proposal and mentioning the override later
    describes a player nobody selected.
    """
    by_id = {c.player_id: c for c in candidates}
    candidate = by_id.get(override.player_id)
    if candidate is None:
        # ASSUMPTION (pending owner ruling): an override naming a player outside
        # the supplied pool is honoured but its gates CANNOT be evaluated — the
        # engine has no data on that player and refuses to invent a clean bill
        # of health. It is reported in the summary rather than smuggled in as a
        # fabricated Exclusion, because no gate actually fired.
        return (), (
            f"{override.player_id} selected: overridden by {override.actor_id} "
            f"to {override.player_id} ({override.reason}); player is not in the "
            "candidate pool, gates not evaluated"
        )
    conflicts = _apply_gates(request, candidate, policy)
    fragment = (
        f"{override.player_id} selected: overridden by {override.actor_id} "
        f"to {override.player_id} ({override.reason})"
    )
    if conflicts:
        reasons = "; ".join(c.detail for c in conflicts)
        fragment += f"; overridden player fails: {reasons}"
    return conflicts, fragment


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------


def rank_substitutes(
    request: SubstituteRequest,
    candidates: Sequence[SubstituteCandidate],
    policy: LeagueRankingPolicy,
    override: Optional[SelectionOverride] = None,
) -> SubstituteRanking:
    """Rank a substitute pool for one vacancy. Pure: no I/O, no clock, no RNG.

    An empty eligible pool is a normal result (``proposed_player_id is None``
    plus the full exclusion list), never an exception.
    """
    _validate(request, candidates, policy)

    eligible: list[CandidateEvaluation] = []
    excluded: list[CandidateEvaluation] = []

    for candidate in candidates:
        exclusions = _apply_gates(request, candidate, policy)
        if exclusions:
            # Excluded candidates get no rule_traces and no rank: they were
            # never ranked, and claiming otherwise would be a lie.
            excluded.append(
                CandidateEvaluation(
                    player_id=candidate.player_id,
                    eligible=False,
                    exclusions=exclusions,
                )
            )
            continue
        traces = _trace(request, candidate, policy)
        eligible.append(
            CandidateEvaluation(
                player_id=candidate.player_id,
                eligible=True,
                rule_traces=traces,
                sort_key=_sort_key(candidate.player_id, traces),
            )
        )

    eligible.sort(key=lambda evaluation: evaluation.sort_key)
    ranked = tuple(
        CandidateEvaluation(
            player_id=evaluation.player_id,
            eligible=True,
            rule_traces=evaluation.rule_traces,
            sort_key=evaluation.sort_key,
            rank=position,
        )
        for position, evaluation in enumerate(eligible, start=1)
    )
    excluded.sort(key=lambda evaluation: evaluation.player_id)

    winner = ranked[0] if ranked else None
    decisions = _load_bearing_rules(ranked)
    proposal_deciding_rules = tuple(
        winner.rule_traces[index].rule for index, _ in decisions
    ) if winner is not None else ()
    summary = _proposal_summary(ranked, decisions)

    proposed_player_id = winner.player_id if winner else None
    conflicts: tuple[Exclusion, ...] = ()
    selected_player_id = proposed_player_id
    # No override: the selection IS the proposal, so the highest-priority
    # load-bearing rule decided it (None when no single rule was load-bearing).
    deciding_rule = proposal_deciding_rules[0] if proposal_deciding_rules else None
    if override is not None:
        conflicts, fragment = _override_conflicts(
            request, candidates, policy, override
        )
        # The override head leads; the proposal is demoted to what it now is —
        # a recommendation that was not taken. Crediting a rule for a selection
        # a human made would be false, so the structured field goes to None.
        summary = f"{fragment} | engine proposal (not selected): {summary}"
        deciding_rule = None
        selected_player_id = override.player_id

    return SubstituteRanking(
        league_id=policy.league_id,
        request_id=request.request_id,
        rules_applied=policy.enabled_rules,
        gates_applied=policy.gates_applied,
        ranked=ranked,
        excluded=tuple(excluded),
        proposed_player_id=proposed_player_id,
        deciding_rule=deciding_rule,
        proposal_deciding_rules=proposal_deciding_rules,
        decision_summary=summary,
        override=override,
        override_conflicts=conflicts,
        selected_player_id=selected_player_id,
    )
