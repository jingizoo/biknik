"""Season scheduler engine v1 — draft fixture generation (#84, extended #233
Slice G).

A deliberately simple, deterministic generator: single round-robin pairings,
each pairing assigned to the earliest available game ice slot. It produces a
*draft* proposal only — nothing is persisted or published here; review and
publish are a later slice (#86). Pure over the store so the output is
reproducible and unit-testable.

Two entry points share the same pairing/ice-assignment core:
``draft_schedule`` (a single Division's registered teams, unchanged since #84)
and ``draft_schedule_for_league`` (a whole League for a Season, optionally
narrowed to one Division — #233 Slice G). A league-wide draft never pairs
teams across different Divisions of that League: registrations are grouped by
their own Division (or "no Division") and each group gets its own
round-robin, so Gold only ever plays Gold.

A pairing that already has a real Game (draft or committed, published or
not, roster-locked or not — a CANCELLED or EXHIBITION game does not count)
is reported in ``already_scheduled``, never re-proposed and never silently
dropped (#206 slice 1) — re-running Generate against a Division that
already has some Games fills in only the missing matchups; it never
duplicates the ones that exist.

The regular-season FORMAT is configurable (#375): ``meetings_per_opponent``
asks for N meetings between every pair instead of one, so 6 teams x 3
meetings is 15 games per team. The three counting rules that decide whether
a meeting is already satisfied are stated once, in
:func:`_existing_pairing_games`, and are the same rules the single
round-robin always used — an existing Regular game counts, a cancelled one
does not, an exhibition does not. Home/away for the Nth meeting is decided
deterministically from the sorted team ids alone (see
:func:`round_robin_pairings`), never randomly and never from iteration
order, so the same inputs reproduce the same split in any process and on
any store backend.

A team is never proposed for two overlapping games (#373). Occupancy is
team-wide — keyed by stable team id and actual interval, never by rink or
display name — and covers BOTH already-persisted games and the candidates
this very batch has already accepted, so the preview can no longer show (and
offer to commit) a physically impossible schedule.

A team also gets a configurable TURNAROUND between consecutive games (#390),
``min_turnaround_minutes``, measured in minutes from the previous game's END.
Three defects compounded before it existed and none was visible alone: the
Generate screen sent no rest value at all, so it defaulted to zero; the
calculation compared game START times, so a 14:00-15:30 game followed by a
15:30 game reported 90 minutes of "rest" and no actual ice-free time; and it
consulted only this batch's own picks, never the games already on the ice.
The fix reads the SAME occupancy map #373 built — persisted games plus this
batch's accepted candidates — through one predicate,
:func:`turnaround_conflicts`, which the preview's decision path, the
preview's bounded explanation, and the commit gate all call. Zero (the
default) returns no conflicts without reading occupancy at all, so every
pre-#390 proposal is byte-identical.
"""

import hashlib
import json
from datetime import date, timedelta

from ..domain import GameType, IceSlotStatus, IceSlotType
from ..domain.errors import ValidationError
from ..domain.time_utils import intervals_overlap
from .league_scope import (
    registered_team_ids_in_division,
    registered_teams_by_division_in_league,
)
from .schedule_explanations import (
    ExplanationBudget,
    build_unplaced_explanation,
)
from .setup_service import SetupService as _PolicySetup


# #375 — an operator-configurable regular-season format is still a bounded
# one: the pairing list this engine materializes grows as
# ``meetings × C(teams, 2)``, so an unbounded value turns one request into an
# arbitrarily large allocation. Real formats sit in the 1–4 range (single,
# double, triple, quadruple round-robin); the ceiling is deliberately far
# above any of them while still being a ceiling.
MAX_MEETINGS_PER_OPPONENT = 20


def _normalize_meetings(value, field="meetings_per_opponent"):
    """Validate the configurable meetings-per-opponent count (#375).

    Client input, so every bad shape raises a structured ``ValidationError``
    rather than letting a raw TypeError cross the facade boundary — the same
    contract :func:`_normalize_constraints` already holds for the scheduling
    constraints. ``None`` means "not specified" and keeps the historical
    single round-robin, so every existing caller is unchanged.

    ``bool`` is rejected explicitly even though it is an ``int`` subclass:
    ``True`` would otherwise silently mean "1 meeting" and ``False`` would
    mean "0", neither of which any caller can have intended.
    """
    if value is None:
        return 1
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValidationError(
            f"'{field}' must be an integer >= 1.")
    if value < 1:
        raise ValidationError(f"'{field}' must be an integer >= 1.")
    if value > MAX_MEETINGS_PER_OPPONENT:
        raise ValidationError(
            f"'{field}' must be at most {MAX_MEETINGS_PER_OPPONENT}.")
    return value


def round_robin_pairings(team_ids, meetings=1):
    """Round-robin pairings via the circle method (deterministic).

    Returns a flat list of ``(home_team_id, away_team_id)``. With the default
    ``meetings=1`` this is a SINGLE round-robin in round order — every team
    plays every other exactly once — byte-identical to the pre-#375 output.
    An odd number of teams yields a bye each round (the byed team simply has
    no game that round). Home/away alternates by round for basic balance.

    ``meetings`` (#375) repeats that base round-robin N times, so every team
    plays every other exactly N times: ``meetings × C(teams, 2)`` pairings in
    total, i.e. ``meetings × (teams - 1)`` games per team. The worked example
    from the issue: 6 teams × 3 meetings = 45 pairings = 15 games per team.

    Home/away is decided DETERMINISTICALLY, never arbitrarily: meeting *m*
    (0-indexed) reuses the base round-robin's own orientation when *m* is
    even and the exact reverse when *m* is odd. Two consequences worth
    stating, because they are the guarantee rather than an accident:

    * Every pair's home/away split is balanced to within one game — for even
      ``meetings`` it is exactly even (each side hosts ``meetings / 2``), for
      odd ``meetings`` the base orientation hosts one extra.
    * The decision depends only on the sorted team ids and ``meetings``. No
      RNG, no clock, no dict/set iteration order, no store state — so the
      same inputs reproduce the same split in a different process, on a
      different backend, in a different order of registration.

    The cycles are emitted whole (all of meeting 0, then all of meeting 1,
    …) rather than interleaved, which is both what a real league schedule
    looks like and what keeps the ``meetings=1`` output unchanged.
    """
    teams = sorted(team_ids)
    if len(teams) < 2:
        return []
    if len(teams) % 2 == 1:
        teams = teams + [None]  # None marks the bye
    n = len(teams)
    fixed, rot = teams[0], teams[1:]
    base = []
    for r in range(n - 1):
        arrangement = [fixed] + rot
        for i in range(n // 2):
            a, b = arrangement[i], arrangement[n - 1 - i]
            if a is None or b is None:
                continue  # bye
            base.append((a, b) if r % 2 == 0 else (b, a))
        rot = [rot[-1]] + rot[:-1]  # rotate all but the fixed team
    if meetings == 1:
        return base
    return [(h, a) if m % 2 == 0 else (a, h)
            for m in range(meetings) for h, a in base]


# #375 — the ceiling on GUARANTEED GAMES PER TEAM. The bound is an
# ALLOCATION bound, not a taste judgement: the pairing list this engine
# materializes in memory before any ice is consulted is exactly
# ``teams x games / 2`` rows, so an unbounded ``games_per_team`` turns one
# request into an arbitrarily large allocation. 120 sits far above any real
# regular season (the NHL plays 82) while still being a ceiling.
MAX_GAMES_PER_TEAM = 120


def _normalize_games_per_team(value, field="games_per_team"):
    """Validate the operator-facing guaranteed-games-per-team count (#375).

    Client input, so every bad shape raises a structured ``ValidationError``
    rather than letting a raw TypeError cross the facade boundary — the same
    contract :func:`_normalize_meetings` and :func:`_normalize_constraints`
    already hold. ``None`` means "not specified" and is returned as ``None``
    rather than defaulted, because the caller still has to tell the legacy
    ``meetings_per_opponent`` path apart from this one.

    ``bool`` is rejected explicitly even though it is an ``int`` subclass:
    ``True`` would otherwise silently mean "1 game" and ``False`` "0",
    neither of which any caller can have intended.

    FEASIBILITY (``teams x games`` must be even) is deliberately NOT checked
    here — it needs the team count, which only the draft entry points know.
    See :func:`_require_feasible_games_per_team`.
    """
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValidationError(f"'{field}' must be an integer >= 1.")
    if value < 1:
        raise ValidationError(f"'{field}' must be an integer >= 1.")
    if value > MAX_GAMES_PER_TEAM:
        raise ValidationError(
            f"'{field}' must be at most {MAX_GAMES_PER_TEAM}.")
    return value


def _require_feasible_games_per_team(team_count, games, label=None):
    """Refuse an arithmetically impossible guarantee (#375), actionably.

    Every game is played by two teams, so it contributes 2 to the
    league-wide game count: the total ``T x G`` MUST be even. When ``T`` and
    ``G`` are both odd it is not, and no amount of scheduling cleverness
    fixes it — some team will always end up one game short. The owner's
    ruling is to REFUSE rather than quietly deliver an uneven schedule under
    a control the operator reads as "guaranteed".

    The single parity condition suffices for the whole construction, not
    just the total: ``T x (T-1)`` is always even (one of two consecutive
    integers is), so ``T x G`` even is equivalent to ``T x rem`` even, which
    is exactly the precondition :func:`_extra_pairs` needs — odd ``T``
    requires an even ``rem`` to build its symmetric chords.

    The message NAMES THE NEAREST ACHIEVABLE COUNTS (``G-1`` and ``G+1``,
    both of which flip the parity) rather than merely reporting failure: an
    operator told "20 is impossible" has to guess, while one told "ask for
    19 or 21" can act. A neighbour that falls outside the accepted range is
    dropped rather than suggested — ``G-1`` is not offered when it is 0, and
    ``G+1`` is not offered past the ceiling.

    ``label`` names the team group when a League-wide draft has several
    Divisions with different team counts, so the operator learns WHICH one
    cannot honour the request instead of just that something could not.
    """
    if team_count < 2 or (team_count * games) % 2 == 0:
        return
    nearest = [value for value in (games - 1, games + 1)
               if 1 <= value <= MAX_GAMES_PER_TEAM]
    where = f" in {label}" if label else ""
    advice = (f" Ask for {' or '.join(str(v) for v in nearest)} games "
              f"instead." if nearest else "")
    raise ValidationError(
        f"{games} guaranteed games per team is impossible with "
        f"{team_count} teams{where}: every game is played by two teams, so "
        f"the league-wide total ({team_count} x {games} = "
        f"{team_count * games}) would have to be an odd number of team "
        f"appearances and one team would always finish a game short."
        f"{advice}",
        {"reason": "games_per_team_infeasible",
         "team_count": team_count,
         "games_per_team": games,
         "nearest_achievable": nearest})


def _extra_pairs(teams, rem):
    """A ``rem``-REGULAR multigraph on ``teams`` — the heart of #375.

    "rem-regular" means every single team gains exactly ``rem`` extra games.
    That is the whole correctness burden of the remainder: the ``base``
    complete round-robins below already give everyone ``base x (T-1)``, so
    if this function is rem-regular the total is exactly ``G`` for every
    team, and if it is off by one anywhere the guarantee is a lie for that
    team. Returned as UNORDERED pairs; orientation is decided by the caller.

    Two constructions, because one shape does not cover both parities:

    EVEN T — the circle method's rounds. With an even number of teams each
    of the ``T-1`` rotation rounds is a PERFECT MATCHING (every team appears
    in exactly one pair of that round), so taking the first ``rem`` rounds
    gives every team exactly ``rem`` extra games. There are always enough
    rounds: ``rem < T-1`` by construction, since it is a remainder mod
    ``T-1``. Rounds of a round-robin are edge-disjoint, so no PAIR gains
    more than one — which is what keeps the per-opponent spread at 1.

    ODD T — a circulant. Odd ``T`` has no perfect matching at all (a
    matching covers an even number of vertices), so the round trick cannot
    work. But odd ``T`` also forces ``rem`` to be EVEN: feasibility requires
    ``T x G`` even, so odd ``T`` forces ``G`` even; ``T-1`` is then even
    too; and ``rem = G - base x (T-1)`` is even minus even. So the extras
    can be built from symmetric chords: for each ``k`` in ``1..rem/2``, join
    team ``i`` to team ``(i+k) mod T`` for every ``i``. Each ``k`` adds
    exactly 2 to every team's degree (its ``+k`` chord and its ``-k`` one),
    so the total is ``2 x rem/2 = rem`` — rem-regular. The chords are
    distinct and non-degenerate because ``k < T/2``: ``rem <= T-2`` gives
    ``rem/2 <= (T-2)/2 < T/2``, so ``k`` and ``T-k`` never collide and no
    chord doubles back on itself. Different ``k`` touch disjoint pairs, so
    again no pair gains more than one.

    Both are pure functions of the SORTED team list — no RNG, no clock, no
    set or dict iteration order, no store state — which is the determinism
    contract ``round_robin_pairings`` already holds and ``_draft_fingerprint``
    depends on.
    """
    if rem <= 0:
        return []
    n = len(teams)
    if n % 2 == 0:
        # Replay the same rotation `round_robin_pairings` uses, keeping only
        # the first `rem` rounds. Written out rather than sliced off that
        # function's flat output because the flat list carries orientation
        # and round boundaries are exactly what a naive slice loses.
        pairs = []
        fixed, rot = teams[0], list(teams[1:])
        for _round in range(rem):
            arrangement = [fixed] + rot
            for i in range(n // 2):
                pairs.append((arrangement[i], arrangement[n - 1 - i]))
            rot = [rot[-1]] + rot[:-1]
        return pairs
    # Odd T: `rem` is guaranteed even here (see the docstring), so this
    # halving is exact. `_require_feasible_games_per_team` is what makes
    # that guarantee true before the generator is ever reached.
    return [(teams[i], teams[(i + k) % n])
            for k in range(1, rem // 2 + 1)
            for i in range(n)]


def games_per_team_pairings(team_ids, games_per_team):
    """Pairings giving EVERY team exactly ``games_per_team`` games (#375).

    The operator-facing inversion: instead of asking for N meetings against
    every opponent and letting games-per-team fall out as ``N x (T-1)``, the
    operator asks for the number of games their own team is guaranteed and
    the per-opponent count is derived:

        opponents = T - 1
        base      = G // opponents   # games against EVERY opponent
        rem       = G %  opponents   # this many opponents are played once more

    So the output is ``base`` complete round-robins (the existing circle-method
    generator, reused unchanged) plus a ``rem``-regular extras multigraph
    (:func:`_extra_pairs`). Every team plays ``base x opponents + rem == G``
    games, and no opponent is played more than one time more than any other.

    Worked examples from the issue, all of which the property tests pin:
    T=2/G=20 -> the two teams meet 20 times (20 games); T=5/G=20 -> base 5,
    rem 0, every pair meets 5x (50 games); T=20/G=20 -> base 1, rem 1,
    everyone meets once (19 games) plus one extra each (10 more) = 200.

    FEASIBILITY is the caller's job, not this function's: ``T x G`` must be
    even or no construction can give every team exactly G (see
    :func:`_require_feasible_games_per_team`, which refuses with an
    actionable message). Reaching here with an infeasible pair would
    silently produce an odd ``rem`` on odd ``T`` and lose a game, so the
    assertion of that precondition lives at the entry points where the team
    count is known and a refusal can still be reported to the operator.

    Fewer than two teams yields no pairings — the same answer
    :func:`round_robin_pairings` already gives, and what keeps the ``T-1``
    denominator away from zero.

    HOME/AWAY is balanced within one game per pair and decided
    deterministically from the sorted team ids alone. The base cycles
    inherit :func:`round_robin_pairings`'s own alternation (meeting *m*
    reuses the base orientation when *m* is even, reverses it when odd), and
    the extras are oriented as if they were meeting number ``base`` — the
    NEXT meeting in that same alternation. That is what keeps the guarantee
    exact rather than approximately right: an extra game blindly added in
    the base orientation would push a pair that had already hosted one more
    (odd ``base``) to a two-game imbalance. Following the alternation
    instead means an even ``base`` becomes a one-game difference and an odd
    ``base`` comes out exactly level.
    """
    teams = sorted(team_ids)
    if len(teams) < 2:
        return []
    opponents = len(teams) - 1
    base, rem = divmod(games_per_team, opponents)
    pairings = round_robin_pairings(teams, base)
    if rem <= 0:
        return pairings
    # The canonical orientation of every pair, read off meeting 0 of the
    # base round-robin, so the extras alternate against the SAME reference
    # the base cycles do. Built even when `base` is 0 (no cycles emitted):
    # the reference is the circle method's own answer, not a count of what
    # was emitted.
    canonical = {frozenset(pair): pair
                 for pair in round_robin_pairings(teams, 1)}
    for pair in _extra_pairs(teams, rem):
        home, away = canonical[frozenset(pair)]
        pairings.append((home, away) if base % 2 == 0 else (away, home))
    return pairings


def _available_game_slots(store, slot_ids=None):
    """Game-type ice slots that are AVAILABLE and not already tied to a game,
    earliest first (deterministic tie-break on id)."""
    wanted = set(slot_ids) if slot_ids else None
    slots = []
    for s in store.all_ice_slots():
        if wanted is not None and s.id not in wanted:
            continue
        if s.slot_type != IceSlotType.GAME or s.status != IceSlotStatus.AVAILABLE:
            continue
        if store.game_using_ice_slot(s.id) is not None:
            continue
        slots.append(s)
    slots.sort(key=lambda s: (s.start_time, s.id))
    return slots


def _team_name(store, tid):
    t = store.get_team(tid)
    return t.name if t else tid


def _validate_day(value, field):
    """A blackout/holiday entry must be a strict ``YYYY-MM-DD`` string (#85).

    The scheduler compares against ``slot.start_time.date().isoformat()``, so a
    loosely-formatted date (``2026/01/05``, ``Jan 5``, ``2026-1-5``) would never
    match and the blackout would be silently ignored — worse than rejecting it.
    The round-trip check pins the exact canonical format."""
    if not isinstance(value, str):
        raise ValidationError(
            f"'{field}' entries must be YYYY-MM-DD date strings.")
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        raise ValidationError(
            f"'{field}' entries must be YYYY-MM-DD date strings.")
    if parsed.isoformat() != value:
        raise ValidationError(
            f"'{field}' entries must be YYYY-MM-DD date strings.")
    return value


def _blackout_map(value, field):
    """Validate a ``{id: [YYYY-MM-DD, …]}`` blackout map into ``{id: set}``.

    Client input, so every bad shape raises a structured ``ValidationError``
    (never a raw AttributeError/TypeError across the facade boundary, #85)."""
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValidationError(
            f"'{field}' must be an object of id to a list of "
            f"YYYY-MM-DD date strings.")
    out = {}
    for key, days in value.items():
        if days is None:
            out[str(key)] = set()
            continue
        if not isinstance(days, (list, tuple)):
            raise ValidationError(
                f"'{field}[{key}]' must be a list of YYYY-MM-DD date strings.")
        out[str(key)] = {_validate_day(d, f"{field}[{key}]") for d in days}
    return out


def _date_set(value, field):
    """Validate a flat ``[YYYY-MM-DD, …]`` list into a ``set`` (#233 Slice G).

    Distinct from ``_blackout_map``: a season blackout or holiday applies to
    every team/rink uniformly, so it has no per-id key."""
    if value is None:
        return set()
    if not isinstance(value, (list, tuple)):
        raise ValidationError(
            f"'{field}' must be a list of YYYY-MM-DD date strings.")
    return {_validate_day(d, field) for d in value}


def _normalize_constraints(constraints):
    """Validate and coerce the request constraints into a predictable shape
    (#85, extended #233 Slice G).

    Recognized keys (all optional): ``team_blackouts`` {team_id: [YYYY-MM-DD]},
    ``rink_blackouts`` {rink_id: [YYYY-MM-DD]}, ``season_blackout_dates``
    [YYYY-MM-DD] (blocks every team/rink in the whole draft), ``holiday_dates``
    [YYYY-MM-DD] (same effect, kept as a distinct input/reason code so an
    operator can tell a holiday closure from an ad hoc season blackout),
    ``min_rest_hours`` (number ≥ 0), ``max_games_per_team_per_day`` (int ≥ 0).
    Malformed input raises a ``ValidationError`` so the facade returns a
    structured error rather than letting a raw exception cross the boundary.
    """
    if constraints is None:
        constraints = {}
    if not isinstance(constraints, dict):
        raise ValidationError("'constraints' must be an object or null.")
    c = constraints

    min_rest = c.get("min_rest_hours")
    min_rest = 0 if min_rest is None else min_rest
    # bool is an int subclass — reject it explicitly so True/False can't sneak
    # through as 1/0.
    if isinstance(min_rest, bool) or not isinstance(min_rest, (int, float)) \
            or min_rest < 0:
        raise ValidationError("'min_rest_hours' must be a number >= 0.")

    # #390 — the turnaround, in MINUTES and measured from the previous game's
    # END. Deliberately a SECOND field rather than a redefinition of
    # ``min_rest_hours``: that one is a start-to-start rest window (#85) and
    # every caller and regression that uses it means exactly that. A rink
    # turnaround is a resurfacing-and-changeover interval, so hours cannot
    # express it and the edge it measures from is different. Both may be set;
    # they are independent clauses evaluated independently.
    turnaround = c.get("min_turnaround_minutes")
    turnaround = 0 if turnaround is None else turnaround
    if isinstance(turnaround, bool) \
            or not isinstance(turnaround, (int, float)) or turnaround < 0:
        raise ValidationError(
            "'min_turnaround_minutes' must be a number >= 0.")

    max_per_day = c.get("max_games_per_team_per_day")
    max_per_day = 0 if max_per_day is None else max_per_day
    if isinstance(max_per_day, bool) or not isinstance(max_per_day, int) \
            or max_per_day < 0:
        raise ValidationError(
            "'max_games_per_team_per_day' must be an integer >= 0.")

    return {
        "team_blackouts": _blackout_map(c.get("team_blackouts"), "team_blackouts"),
        "rink_blackouts": _blackout_map(c.get("rink_blackouts"), "rink_blackouts"),
        "season_blackout_dates": _date_set(
            c.get("season_blackout_dates"), "season_blackout_dates"),
        "holiday_dates": _date_set(c.get("holiday_dates"), "holiday_dates"),
        "min_rest_hours": float(min_rest),
        "min_turnaround_minutes": float(turnaround),
        "max_per_day": int(max_per_day),
    }


def turnaround_minutes(constraints):
    """The normalized turnaround (minutes) from a raw constraints mapping.

    The commit gate needs the same number the preview used and must derive it
    the same way — including the same ``ValidationError`` for a bad shape —
    so it reads it through this one accessor rather than reaching into the
    request body itself (#390 req 3)."""
    return _normalize_constraints(constraints)["min_turnaround_minutes"]


# Every code this engine can report, in the fixed priority order _slot_reason
# checks them — the first one that applies wins (#233 Slice G: previously only
# a free-text message existed; the code is the new, additional, machine-
# readable signal a UI or downstream automation can act on without parsing
# prose).
SEASON_BLACKOUT = "season_blackout"
HOLIDAY = "holiday"
TEAM_BLACKOUT = "team_blackout"
RINK_BLACKOUT = "rink_blackout"
MAX_PER_DAY = "max_per_day"
MIN_REST = "min_rest"
# #390 — the configurable turnaround, measured from the previous game's END.
# A DISTINCT code from MIN_REST rather than an overload of it: the two answer
# different questions (start-to-start rest vs. ice-free changeover time),
# carry different units, and an operator remediating one is not remediating
# the other. It is registered in #379's own tables — the reason-code order,
# the detail allowlist, the nested-row allowlist and the alternatives
# formatter — so the explanation extends that contract through its existing
# mechanisms instead of inventing a parallel one.
MIN_TURNAROUND = "min_turnaround"
NO_ICE_AVAILABLE = "no_ice_available"
# #277 Slice B — the scheduler's ADVISORY mirror of the commit gate's policy
# checks (SetupService._slot_policy_violation, the single shared
# implementation): a slot the gate would reject is skipped during assignment
# and its code surfaces in unscheduled[].reason_codes, so a draft never
# proposes a row commit_draft_schedule is guaranteed to refuse. The gate
# stays authoritative — this is a preview courtesy, not the enforcement.
TURNOVER_BUFFER_CONFLICT = "turnover_buffer_conflict"
INSUFFICIENT_PLAYABLE_TIME = "insufficient_playable_time"
CURFEW_VIOLATION = "curfew_violation"
# #318 review — physical overlap is refused by the gate UNCONDITIONALLY
# (zero/absent policies change nothing), so the advisory mirrors it too.
SLOT_OVERLAP_CONFLICT = "slot_overlap_conflict"
# #373 — the preview's mirror of the commit gate's team-wide double-booking
# refusal (SetupService._assert_slot_free_for_game). Deliberately the SAME
# string the gate raises in details["reason"], so an operator sees one stable
# code (and the move panel's existing label copy) whether the conflict was
# caught in preview or at commit. Evaluated LAST, after _slot_reason and the
# policy advisory: like the #277 Slice B codes before it, appending the new
# check keeps every reason code an existing slot already reported unchanged —
# TEAM_OVERLAP only ever appears where the generator previously reported
# nothing at all, which is precisely the impossible-schedule bug it closes.
TEAM_OVERLAP = "team_overlap"
# Explanation-only fact: the slot was valid inventory at the beginning of the
# preview, but an earlier pairing in this same deterministic proposal selected
# it. It is not added to legacy ``reason_codes`` (whose semantics stay intact).
ICE_ALREADY_SELECTED = "ice_already_selected"


MAX_TURNAROUND_CONFLICTS = 4
# One wording, used by the preview's short-circuit reason, the preview's
# bounded explanation, and the commit gate's refusal — the phrase the issue
# asks an operator to see.
TURNAROUND_MESSAGE = "minimum turnaround not met"


def _turnaround_gap_minutes(start, end, other_start, other_end):
    """Ice-free minutes between two of ONE team's games, or ``None`` if the
    two windows overlap (#390).

    This is the whole arithmetic of the fix, isolated so preview and commit
    cannot drift apart. Two properties are load-bearing:

    * the gap is measured EDGE TO EDGE — the later game's start minus the
      earlier game's END. Comparing starts (what ``min_rest_hours`` does, and
      what this engine did for every rest calculation before #390) reports 90
      minutes of rest for a 14:00-15:30 game followed by a 15:30 game, which
      has none;
    * it is UNDIRECTED. The comparison set is a team's persisted occupancy
      plus this batch's accepted candidates, neither of which is ordered
      relative to the candidate being tested, so the neighbour may be on
      either side. A one-sided check would silently pass a candidate that
      ends the instant an already-booked game begins.

    Overlapping windows return ``None`` rather than a negative gap: an
    overlap is a physical impossibility, already refused (and far more
    precisely diagnosed) by ``team_overlap``, and turning it into a
    turnaround refusal would change which reason an established fixture
    reports.
    """
    if start >= other_end:
        return (start - other_end).total_seconds() / 60.0
    if other_start >= end:
        return (other_start - end).total_seconds() / 60.0
    return None


def turnaround_conflicts(start, end, team_ids, occupancy, minutes,
                         in_scope_game_ids=None):
    """Every booking that leaves one of ``team_ids`` less than ``minutes`` of
    turnaround around the window ``start``-``end`` (#390).

    THE shared predicate. The preview's decision path, the preview's bounded
    explanation observer, and the commit gate all call this one function over
    the same shape of input, so "the identical check at commit" is a property
    of the code rather than a claim about two implementations. #382 shipped a
    commit guard that asked a different question from the preview and refused
    legitimate commits for a month; one predicate is the structural fix for
    that class of defect.

    ``occupancy`` is ``{team_id: [(start, end, game_id_or_None)]}`` — exactly
    the map :func:`_team_overlap_reason` reads, which is what makes BOTH of
    the issue's enforcement clauses fall out of one lookup: it is seeded from
    :func:`_persisted_team_spans` (the games already on the ice, which the
    pre-#390 rest calculation never consulted at all) and grown in place with
    every candidate this batch accepts (``game_id`` ``None``, since nothing is
    persisted yet). A pairing placed earlier in the same generation is as real
    a constraint as one already committed.

    ``minutes`` of zero (the default, and every pre-#390 caller) returns no
    conflicts without reading ``occupancy``, so the proposal is byte-identical
    to the historical one.

    ``in_scope_game_ids``, when supplied, is the set of Game ids the caller is
    entitled to be told about (#379). The occupancy snapshot is deliberately
    unfiltered so preview and commit measure the same physical edges (#373),
    which is exactly why a neighbouring Season sharing a Venue can put a
    foreign Game into this comparison: the collision is still reported, with
    its ``conflict_game_id`` withheld. ``None`` means "no boundary applies" —
    the commit gate's own use, where the conflicting Game is by construction
    blocking the caller's own batch and every sibling gate
    (``_assert_slot_free_for_game``) already names it.

    Rows are returned in canonical order so two runs over the same facts
    produce the identical list, and therefore the identical
    ``draft_fingerprint``, on every backend.
    """
    if minutes <= 0:
        return []
    conflicts = []
    for tid in team_ids:
        for other_start, other_end, game_id in occupancy.get(tid, ()):
            gap = _turnaround_gap_minutes(start, end, other_start, other_end)
            if gap is None or gap >= minutes:
                continue
            # The SOURCE is decided before the id is withheld, so a foreign
            # existing Game is never mislabelled as one of this batch's own
            # proposals just because its id was suppressed.
            source = "existing_game" if game_id is not None else "proposed_game"
            reported_id = game_id
            if in_scope_game_ids is not None \
                    and reported_id not in in_scope_game_ids:
                reported_id = None
            conflicts.append({
                "team_id": tid,
                "conflict_source": source,
                "conflict_game_id": reported_id,
                "conflict_start_time": other_start.isoformat(),
                "conflict_end_time": other_end.isoformat(),
                "gap_minutes": gap,
                "shortfall_minutes": minutes - gap,
            })
    return sorted(conflicts, key=_canonical_sort_key)


def _slot_constraint_rejections(slot, home, away, con, team_slots, occupancy,
                                in_scope_game_ids=None):
    """All request-constraint rejections for one candidate, in stable priority.

    The order mirrors :func:`_slot_reason`, whose independent established path
    still short-circuits at its FIRST cause.  This complete-list evaluator runs
    only inside the bounded observer after a pairing is already known to be
    unplaced, so simultaneous hard causes are visible without changing which
    fixture wins a slot or adding work to successful placement (#379).
    """
    day = slot.start_time.date().isoformat()
    rejected = []
    if day in con["season_blackout_dates"]:
        rejected.append({
            "code": SEASON_BLACKOUT,
            "message": "season blackout date",
            "details": {"date": day},
        })
    if day in con["holiday_dates"]:
        rejected.append({
            "code": HOLIDAY,
            "message": "holiday",
            "details": {"date": day},
        })
    blackout_teams = sorted(
        tid for tid in (home, away)
        if day in con["team_blackouts"].get(tid, ()))
    if blackout_teams:
        rejected.append({
            "code": TEAM_BLACKOUT,
            "message": "team blackout date",
            "details": {"date": day, "team_ids": blackout_teams},
        })
    if day in con["rink_blackouts"].get(slot.rink_id, ()):
        rejected.append({
            "code": RINK_BLACKOUT,
            "message": "rink blackout date",
            "details": {"date": day, "rink_id": slot.rink_id},
        })
    if con["max_per_day"] > 0:
        maxed_teams = []
        for tid in (home, away):
            same_day = sum(1 for t in team_slots.get(tid, [])
                           if t.date() == slot.start_time.date())
            if same_day >= con["max_per_day"]:
                maxed_teams.append(tid)
        if maxed_teams:
            rejected.append({
                "code": MAX_PER_DAY,
                "message": "max games per team per day reached",
                "details": {
                    "date": day,
                    "team_ids": sorted(set(maxed_teams)),
                    "limit": con["max_per_day"],
                },
            })
    if con["min_rest_hours"] > 0:
        rest = timedelta(hours=con["min_rest_hours"])
        conflicts = []
        hit_teams = set()
        for tid in (home, away):
            for start in team_slots.get(tid, []):
                if abs(slot.start_time - start) < rest:
                    hit_teams.add(tid)
                    conflicts.append({
                        "team_id": tid,
                        "start_time": start.isoformat(),
                    })
        if conflicts:
            # Two teams plus their nearest few proposal starts are enough to
            # remediate this candidate; keep nested evidence bounded too.
            conflicts.sort(key=_canonical_sort_key)
            kept = conflicts[:4]
            rejected.append({
                "code": MIN_REST,
                "message": "minimum rest between games not met",
                "details": {
                    "team_ids": sorted(hit_teams),
                    "min_rest_hours": con["min_rest_hours"],
                    "conflicts": kept,
                    "omitted_conflict_count": len(conflicts) - len(kept),
                },
            })
    # #390 — the same nested bound ``min_rest`` above carries: the nearest few
    # blocking games are enough to remediate this candidate, and the rest is
    # COUNTED rather than dropped silently.
    turnaround = turnaround_conflicts(
        slot.start_time, slot.end_time, (home, away), occupancy,
        con["min_turnaround_minutes"], in_scope_game_ids)
    if turnaround:
        kept = turnaround[:MAX_TURNAROUND_CONFLICTS]
        rejected.append({
            "code": MIN_TURNAROUND,
            "message": TURNAROUND_MESSAGE,
            "details": {
                "team_ids": sorted({c["team_id"] for c in turnaround}),
                "min_turnaround_minutes": con["min_turnaround_minutes"],
                # An unnameable blocker DROPS the field rather than nulling
                # it — byte-identical to how the `team_overlap` observer
                # treats an out-of-context (or not-yet-persisted) Game, so
                # the allowlist simply omits it and a reader never has to
                # tell "no id" from "id withheld" by inspecting a null.
                "conflicts": [
                    {k: v for k, v in c.items()
                     if not (k == "conflict_game_id" and v is None)}
                    for c in kept],
                "omitted_conflict_count": len(turnaround) - len(kept),
            },
        })
    return rejected


def _slot_reason(slot, home, away, con, team_slots, occupancy):
    """Why ``slot`` can't host ``home`` vs ``away`` under the constraints, as
    ``(code, message)``, or ``(None, None)`` if it can. ``team_slots`` maps
    team_id -> [assigned start_time]."""
    # Keep the established decision path short-circuiting at the first cause.
    # The all-causes helper above is explanation-only and globally capped.
    day = slot.start_time.date().isoformat()
    if day in con["season_blackout_dates"]:
        return SEASON_BLACKOUT, "season blackout date"
    if day in con["holiday_dates"]:
        return HOLIDAY, "holiday"
    if day in con["team_blackouts"].get(home, ()) \
            or day in con["team_blackouts"].get(away, ()):
        return TEAM_BLACKOUT, "team blackout date"
    if day in con["rink_blackouts"].get(slot.rink_id, ()):
        return RINK_BLACKOUT, "rink blackout date"
    if con["max_per_day"] > 0:
        for tid in (home, away):
            same_day = sum(1 for t in team_slots.get(tid, [])
                           if t.date() == slot.start_time.date())
            if same_day >= con["max_per_day"]:
                return MAX_PER_DAY, "max games per team per day reached"
    if con["min_rest_hours"] > 0:
        rest = timedelta(hours=con["min_rest_hours"])
        for tid in (home, away):
            for start in team_slots.get(tid, []):
                if abs(slot.start_time - start) < rest:
                    return MIN_REST, "minimum rest between games not met"
    # #390 — evaluated LAST of the request constraints and through the SAME
    # predicate the explanation observer and the commit gate call. Appending
    # it here keeps every reason code an existing slot already reported
    # unchanged: with the default zero turnaround the predicate returns
    # nothing without reading ``occupancy`` at all.
    if turnaround_conflicts(slot.start_time, slot.end_time, (home, away),
                            occupancy, con["min_turnaround_minutes"]):
        return MIN_TURNAROUND, TURNAROUND_MESSAGE
    return None, None


def _active_game_slot_pairs(store):
    """Every non-cancelled Game that actually occupies ice, paired with its
    resolved ``IceSlot`` — the run's single snapshot of the active-game
    inventory, taken ONCE and handed to both the policy advisory (#277
    Slice B) and the team-occupancy seed (#373) so a draft over N candidate
    slots never re-scans the game table N times.

    Exactly the read-set the commit gate's own scan uses
    (``SetupService._assert_slot_free_for_game``): a game is in scope iff it
    is not cancelled and resolves to a real slot — no Season, League,
    Division, draft/published, or GameType filtering. That is deliberate on
    both sides. A team is one physical roster: it cannot be on two sheets of
    ice at once because the second booking happens to be an EXHIBITION, a
    still-unpublished draft, or a fixture in another Division or Season.
    Filtering here would make the preview propose rows the gate is
    guaranteed to refuse — the exact divergence #373 exists to close.
    """
    pairs = []
    for g in store.all_games():
        if g.cancelled or not g.ice_slot_id:
            continue
        s = store.get_ice_slot(g.ice_slot_id)
        if s is not None:
            pairs.append((g, s))
    # Store iteration order is not a contract (especially on PostgreSQL).  The
    # policy/team observers may stop at the first conflicting Game, so sort the
    # shared snapshot before either can select evidence from it (#379).
    pairs.sort(key=lambda pair: (
        pair[1].start_time, pair[1].end_time, pair[1].id, pair[0].id))
    return pairs


def _persisted_team_spans(pairs):
    """``{team_id: [(start, end, game_id)]}`` occupancy from the persisted
    games in ``pairs`` (#373).

    Keyed by stable team id, never by rink or display name, and a team's HOME
    and AWAY appearances are recorded identically — the conflict is "this
    roster is already on the ice", which has no home/away asymmetry, so all
    four home/home, home/away, away/home and away/away permutations resolve
    through this one map.

    The interval is the SLOT's, not the Game's own ``start_time``/
    ``end_time``: those are copied onto the Game at creation and the gate
    likewise compares slot to slot, so reading the slot keeps preview and
    commit measuring the same edges."""
    spans = {}
    for g, s in pairs:
        for tid in (g.home_team_id, g.away_team_id):
            if tid:
                spans.setdefault(tid, []).append((s.start_time, s.end_time, g.id))
    return spans


def _team_overlap_reason(store, slot, home, away, team_spans):
    """Why ``slot`` would double-book ``home`` or ``away`` (#373), as
    ``(code, message, conflicts)``, or ``(None, None, ())`` if it would not.

    ``team_spans`` is ``{team_id: [(start, end, game_id_or_None)]}`` — the
    persisted occupancy from :func:`_persisted_team_spans` PLUS every
    candidate this batch has already accepted (``game_id`` ``None``, since
    nothing is persisted yet). Checking the two together is the point: a
    batch's own rows conflict with each other exactly as readily as with the
    existing schedule, and a preview that only consulted the database would
    still happily emit the two-rinks-one-team pair this closes.

    Overlap is a half-open INTERVAL test via the shared
    :func:`~..domain.time_utils.intervals_overlap`, not a same-start-time
    test: a partial overlap starting at a different minute is just as
    impossible, while a back-to-back game beginning exactly when the
    previous one ends stays legal.

    ``conflicts`` names each affected team (and the persisted game it
    collides with, when there is one) so the caller can report a structured
    result rather than only prose."""
    conflicts = []
    for tid in (home, away):
        for start, end, game_id in team_spans.get(tid, ()):
            if not intervals_overlap(slot.start_time, slot.end_time, start, end):
                continue
            conflicts.append({
                "team_id": tid,
                "team_name": _team_name(store, tid),
                "conflict_source": ("existing_game" if game_id is not None
                                    else "proposed_game"),
                "conflict_game_id": game_id,
            })
            break  # one conflicting booking per team is enough to refuse
    if not conflicts:
        return None, None, ()
    names = sorted({c["team_name"] for c in conflicts})
    verb = "has" if len(names) == 1 else "have"
    return (TEAM_OVERLAP,
            f"{' and '.join(names)} already {verb} an overlapping game",
            tuple(conflicts))


def _policy_advisor(store, season_id, pairs=None):
    """Per-run advisory closure over the shared policy evaluation (#277
    Slice B). Returns ``check(slot, tentative_spans) -> (code, message)`` or
    ``(None, None)``; ``tentative_spans`` is this run's not-yet-persisted
    same-rink picks as ``(slot_id, start, end)``. Plain reads only. ALWAYS
    active (#318 review): the gate's slot_overlap_conflict is unconditional
    — it binds with zero policy rows and even when no season resolves
    (season-less legacy divisions) — so the advisory mirrors it through the
    same shared implementation. ``pairs`` is the run's shared active-game
    snapshot (:func:`_active_game_slot_pairs`), so a draft over N candidate
    slots never re-scans the game table N times; with no policies and no
    overlapping candidates the proposal output remains byte-identical to
    pre-Slice-B. It is optional purely so a caller with no snapshot to share
    (a direct unit test of this advisory) still gets one — every draft path
    passes the run's own."""
    setup = _PolicySetup(store)
    if pairs is None:
        pairs = _active_game_slot_pairs(store)

    def check(slot, tentative_spans, include_details=False):
        violation = setup._slot_policy_violation(
            slot, season_id, extra_rink_spans=tentative_spans,
            rink_games=pairs)
        if violation is None:
            return (None, None, {}) if include_details else (None, None)
        message, details = violation
        if include_details:
            return details["reason"], message, dict(details)
        return details["reason"], message

    return check


def _resolve_division_season_id(store, division_id):
    """A Division's Season via its LeagueSeason (#283 chain), or ``None`` for
    a dangling/legacy row — the advisory pass simply stays off then."""
    division = store.get_division(division_id) if division_id else None
    ls = (store.get_league_season(division.league_season_id)
          if division is not None and division.league_season_id else None)
    return ls.season_id if ls is not None else None


def _in_scope_game_ids(pairs, scope):
    """The Games in ``pairs`` that lie inside the explanation's own context.

    #386/#388 bound the whole draft surface to the caller's persisted active
    tuple: a principal standing in ``(program, season, league)`` is refused a
    proposal for any other corner, including the two near misses — same
    Program/different Season, and same Season/different League.

    The candidate SCANNER already honours that boundary (it never returns
    another Season's ice), but the active-game snapshot deliberately does not:
    :func:`_active_game_slot_pairs` is unfiltered by design (#373) so the
    preview and the commit gate measure the same physical edges. That is
    correct for DECIDING, and it is exactly why the observer must filter
    before REPORTING — two Seasons holding access to one Venue is what
    ``SeasonVenueAccess`` is for, so a neighbouring Season's Game routinely
    collides with in-context ice.

    Deriving the set from the run's shared snapshot keeps this free of extra
    store reads and guarantees it covers every id the observer can encounter:
    the policy advisory and the team-occupancy seed both read that same list.
    Fail-closed — an unresolved Season/League scope yields the empty set, so a
    dangling or legacy target withholds every id rather than defaulting open.
    """
    season_id = (scope or {}).get("season_id")
    league_id = (scope or {}).get("league_id")
    if not season_id or not league_id:
        return frozenset()
    return frozenset(
        game.id for game, _slot in pairs
        if game.season_id == season_id and game.league_id == league_id)


def _candidate_explanation_record(store, slot, home, away, *, used, con,
                                  team_slots, policy_check, rink_spans,
                                  occupancy, in_scope_game_ids=frozenset()):
    """Evaluate one bounded explanation candidate without changing decisions.

    The placement loop has already failed the pairing before this runs.  This
    observer therefore reads the exact same current tentative state but never
    mutates ``used``/``team_slots``/``rink_spans``/``occupancy``.  It may report
    several simultaneous hard causes even though the legacy scheduler retains
    its established first-cause short circuit.
    """
    rink = store.get_rink(slot.rink_id) if slot.rink_id else None
    raw = {
        "ice_slot_id": slot.id,
        "rink_id": slot.rink_id,
        "venue_id": rink.venue_id if rink is not None else None,
        "start_time": slot.start_time.isoformat(),
        "end_time": slot.end_time.isoformat(),
        "rejections": [],
    }
    if slot.id in used:
        raw["rejections"].append({
            "code": ICE_ALREADY_SELECTED,
            "details": {},
        })
        return raw

    raw["rejections"].extend({
        "code": item["code"],
        "details": item.get("details") or {},
    } for item in _slot_constraint_rejections(
        slot, home, away, con, team_slots, occupancy,
        # #379's in-scope-game-id rule applies to the turnaround evidence for
        # the same reason it applies to ``team_overlap``: the occupancy this
        # reads is the UNFILTERED active-game snapshot (#373), so a
        # neighbouring Season sharing a Venue routinely collides with
        # in-context ice.
        in_scope_game_ids))

    if policy_check is not None:
        policy_code, _policy_message, policy_details = policy_check(
            slot, rink_spans.get(slot.rink_id, ()), include_details=True)
        if policy_code is not None:
            policy_details["rink_id"] = slot.rink_id
            # The colliding Game may belong to a neighbouring Season sharing
            # this Rink.  ``conflict_slot_id`` stays: the candidate's Rink is
            # in-context ice, so a slot on it is the caller's OWN inventory.
            # The GAME is not, and it is dropped rather than nulled so the
            # allowlist simply omits the field.
            if policy_details.get("conflict_game_id") not in in_scope_game_ids:
                policy_details.pop("conflict_game_id", None)
            raw["rejections"].append({
                "code": policy_code,
                "details": policy_details,
            })

    overlap_code, _overlap_message, conflicts = _team_overlap_reason(
        store, slot, home, away, occupancy)
    if overlap_code is not None:
        rows = []
        for conflict in conflicts:
            row = {
                "team_id": conflict["team_id"],
                "conflict_source": conflict["conflict_source"],
            }
            # One rule for every conflicting Game this observer can name, so
            # the boundary cannot hold on one code and lapse on the other. A
            # team may appear in another Season's Game; the collision is still
            # reported, only its out-of-context id is withheld.
            if conflict["conflict_game_id"] in in_scope_game_ids:
                row["conflict_game_id"] = conflict["conflict_game_id"]
            rows.append(row)
        raw["rejections"].append({
            "code": overlap_code,
            "details": {
                "team_ids": sorted({c["team_id"] for c in conflicts}),
                "conflicts": rows,
            },
        })
    return raw


def _assign_ice(store, pairings, slots, constraints, policy_check=None,
                team_spans=None, *, explain=True, explanation_context=None,
                active_pairs=()):
    """Greedy earliest-slot-first assignment shared by every entry point.

    ``pairings`` is ``[(home_team_id, away_team_id, division_id_or_None), ...]``
    — a league-wide draft tags each pairing with the Division its two teams
    are actually registered in (or ``None``), so the resulting rows stay
    per-pairing accurate even though one draft can span several Divisions.
    Returns ``(draft_games, unscheduled)``.

    ``team_spans`` (#373) is the persisted team occupancy from
    :func:`_persisted_team_spans`; it is COPIED, then grown in place as each
    candidate is accepted, so a later pairing in this same batch is checked
    against the earlier ones and not merely against the database. The copy
    keeps the caller's snapshot reusable and this function free of
    side effects on it.
    """
    con = _normalize_constraints(constraints)
    draft_games, unscheduled = [], []
    explanation_budget = ExplanationBudget() if explain else None
    explanation_context = dict(explanation_context or {})
    # Computed ONCE per run, from the same shared snapshot the policy advisory
    # and the team-occupancy seed read, so every Game id the observer can
    # encounter is covered by one membership test and no extra store reads.
    in_scope_game_ids = _in_scope_game_ids(active_pairs, explanation_context)
    used = set()
    team_slots = {}  # team_id -> [assigned start_time]
    rink_spans = {}  # rink_id -> [(slot_id, start, end)] picked this run (#277)
    # team_id -> [(start, end, game_id_or_None)]: persisted bookings plus this
    # run's accepted candidates (#373).
    occupancy = {tid: list(spans)
                 for tid, spans in (team_spans or {}).items()}
    for home, away, division_id in pairings:
        chosen, messages, codes = None, [], []
        team_conflicts = []
        turnaround_rows = []
        for s in slots:
            if s.id in used:
                continue
            code, message = _slot_reason(s, home, away, con, team_slots,
                                         occupancy)
            if code == MIN_TURNAROUND:
                # #390 — the structured half of the turnaround report, built
                # from the SAME predicate that just refused the slot. The
                # in-scope filter is applied here too, one notch tighter than
                # #379 requires of the explanation alone, so no out-of-context
                # Game id can reach the response through this field either.
                turnaround_rows.extend(turnaround_conflicts(
                    s.start_time, s.end_time, (home, away), occupancy,
                    con["min_turnaround_minutes"], in_scope_game_ids))
            if code is None and policy_check is not None:
                code, message = policy_check(
                    s, rink_spans.get(s.rink_id, ()))
            if code is None:
                code, message, conflicts = _team_overlap_reason(
                    store, s, home, away, occupancy)
                team_conflicts.extend(conflicts)
            if code is None:
                chosen = s
                break
            messages.append(message)
            codes.append(code)
        if chosen is not None:
            used.add(chosen.id)
            team_slots.setdefault(home, []).append(chosen.start_time)
            team_slots.setdefault(away, []).append(chosen.start_time)
            for tid in (home, away):
                occupancy.setdefault(tid, []).append(
                    (chosen.start_time, chosen.end_time, None))
            rink_spans.setdefault(chosen.rink_id, []).append(
                (chosen.id, chosen.start_time, chosen.end_time))
            rink = store.get_rink(chosen.rink_id) if chosen.rink_id else None
            draft_games.append({
                "home_team_id": home, "away_team_id": away,
                "home_team_name": _team_name(store, home),
                "away_team_name": _team_name(store, away),
                "division_id": division_id,
                "ice_slot_id": chosen.id,
                "rink_id": chosen.rink_id,
                "rink_name": rink.name if rink else None,
                "start_time": chosen.start_time.isoformat(),
                "end_time": chosen.end_time.isoformat(),
            })
        else:
            # No free slot at all, or every candidate hit a constraint.
            if not messages:
                reason = "No available ice slot for this pairing."
                reason_codes = [NO_ICE_AVAILABLE]
            else:
                reason = "No slot satisfies constraints: " + \
                    ", ".join(sorted(set(messages))) + "."
                reason_codes = sorted(set(codes))
            row = {
                "home_team_id": home, "away_team_id": away,
                "home_team_name": _team_name(store, home),
                "away_team_name": _team_name(store, away),
                "division_id": division_id, "reason": reason,
                "reason_codes": reason_codes,
                # #373 — the structured half of the team-conflict report:
                # WHICH team is already booked, and against what. The prose
                # ``reason`` above names the team too, but only a
                # machine-readable row lets a caller act on it without
                # parsing English. Empty for every pairing blocked purely by
                # blackouts/rest/policy, so it is a positive signal rather
                # than a field that is always present and usually noise.
                "team_conflicts": _dedupe_team_conflicts(team_conflicts),
                # #390 — the same shape for the turnaround: WHICH team, which
                # blocking game, and the shortfall in minutes, so a caller can
                # act on the refusal without parsing English. Empty for every
                # pairing not blocked by turnaround, and always empty when no
                # turnaround is configured.
                "turnaround_conflicts": _dedupe_turnaround_conflicts(
                    turnaround_rows),
            }
            if explanation_budget is not None:
                allowance, preview_limited = explanation_budget.reserve(len(slots))
                raw_candidates = [
                    _candidate_explanation_record(
                        store, candidate, home, away, used=used, con=con,
                        team_slots=team_slots, policy_check=policy_check,
                        rink_spans=rink_spans, occupancy=occupancy,
                        in_scope_game_ids=in_scope_game_ids)
                    for candidate in slots[:allowance]
                ]
                row["explanation"] = build_unplaced_explanation(
                    pairing=row,
                    scope=explanation_context,
                    legacy_reason_codes=reason_codes,
                    raw_candidates=raw_candidates,
                    candidate_total=len(slots),
                    preview_budget_limited=preview_limited,
                )
            unscheduled.append(row)
    return draft_games, unscheduled


def _dedupe_team_conflicts(conflicts):
    """Collapse the per-candidate-slot conflict records (#373) into one
    deterministic list. A pairing is tried against EVERY free slot, so the
    same team can be reported blocked several times over — once per slot it
    collides with — and the same (team, conflicting game) pair naturally
    repeats. Order is canonical, never generation order, so two runs over the
    same facts produce the identical list (and therefore the identical
    ``draft_fingerprint``)."""
    seen, out = set(), []
    for c in conflicts:
        key = (c["team_id"], c["conflict_source"], c["conflict_game_id"])
        if key in seen:
            continue
        seen.add(key)
        out.append(c)
    return sorted(out, key=_canonical_sort_key)


def _dedupe_turnaround_conflicts(conflicts):
    """Collapse the per-candidate-slot turnaround records into one
    deterministic, STRUCTURALLY BOUNDED list (#390) — the same shape
    :func:`_dedupe_team_conflicts` gives overlaps.

    A pairing is tried against EVERY free slot, and each candidate has its own
    gap from the same blocker, so a list keyed by the measured gap would grow
    as ``slots x blockers x teams``. This list is returned in the response AND
    hashed into ``draft_fingerprint``, so its size has to be a function of the
    facts rather than of how much ice happened to be scanned. Keyed by
    ``(team, source, blocking game)`` alone, it is bounded exactly as the
    overlap sibling is: two teams times the games they are actually booked in.

    Which row survives is not arbitrary. An operator asking "how close did
    this come?" wants the SMALLEST shortfall, so the surviving row is the
    LARGEST gap — the nearest miss. Ties fall back to the row's own canonical
    JSON, so the choice never depends on generation order, and the final list
    is canonically ordered: two runs over the same facts produce the identical
    list, and therefore the identical ``draft_fingerprint``, on every backend.
    """
    best = {}
    for c in conflicts:
        key = (c["team_id"], c["conflict_source"], c["conflict_game_id"])
        current = best.get(key)
        if current is None or (c["gap_minutes"], _canonical_sort_key(c)) > (
                current["gap_minutes"], _canonical_sort_key(current)):
            best[key] = c
    return sorted(best.values(), key=_canonical_sort_key)


def _unschedulable_teams(store, team_ids, pairings, unscheduled):
    """Per-team rollup (#233 Slice G): a team with EVERY one of its pairings
    unscheduled is surfaced once, with the union of reason codes that blocked
    it — an operator scanning N per-pairing rows can otherwise miss that the
    same team is the common thread across all of them. A team with only SOME
    pairings unscheduled is a normal constraint-density outcome, not flagged
    here."""
    total = {}
    for home, away, _ in pairings:
        total[home] = total.get(home, 0) + 1
        total[away] = total.get(away, 0) + 1
    blocked_count = {}
    blocked_codes = {}
    for row in unscheduled:
        for tid in (row["home_team_id"], row["away_team_id"]):
            blocked_count[tid] = blocked_count.get(tid, 0) + 1
            blocked_codes.setdefault(tid, set()).update(row["reason_codes"])

    rollup = []
    for tid in sorted(team_ids):
        if total.get(tid, 0) > 0 and blocked_count.get(tid, 0) == total[tid]:
            rollup.append({
                "team_id": tid, "team_name": _team_name(store, tid),
                "reason_codes": sorted(blocked_codes.get(tid, ())),
            })
    return rollup


def _existing_pairing_games(store, division_scope):
    """``{(league_season_id, division_id, frozenset({home_team_id,
    away_team_id})): existing_game_id}`` for every non-cancelled REGULAR
    Game already in any of ``division_scope`` (#206 slice 1 — preserve
    existing Games, generate only missing round-robin matchups).
    ``division_scope`` is an iterable of ``(league_season_id, division_id)``
    tuples, not bare division ids (#328 review): a league-wide draft's "no
    Division" group is keyed by ``division_id=None``, and Teams are
    permanent, so scoping by ``division_id`` alone would let a
    division-less Regular Game from a completely different Season/League
    — the same two team ids reused later — wrongly suppress a pairing that
    has never actually been played in THIS League+Season.

    The returned mapping is keyed by the FULL ``(league_season_id,
    division_id, pairing)`` tuple, not by pairing alone (#328 review round
    2): a league-wide call can have SEVERAL Divisions in scope at once
    (all sharing one League+Season), and a bare pairing key would let a
    real Game that only ever qualified for Division A's scope wrongly
    match a lookup for Division B's fresh pairing — exactly the bug this
    keying closes for a team pair reassigned between Divisions, leaving a
    stale Game behind in the Division they left. Callers must look up with
    the SAME full tuple (:func:`_split_already_scheduled` takes the call's
    single ``league_season_id`` — every pairing in one ``draft_schedule``
    or ``draft_schedule_for_league`` call shares it — plus each pairing's
    own ``division_id``).

    Draft or committed, published or not, roster-locked or not all count —
    the risk this closes is re-running Generate silently proposing (and
    Commit silently creating) a duplicate for a pairing that already has
    ANY real Game, not only a published one. A CANCELLED game does not
    count (that is exactly the signal the pairing needs re-scheduling); an
    EXHIBITION game does not count either (#283: it never affects
    standings and was never the round-robin obligation) — only a Regular
    game satisfies a Regular pairing.

    #375 — the value is a LIST of every qualifying Game's id, not a single
    id. A configurable format asks each pair to meet N times, so "does this
    pairing already have a Game?" is no longer the question; "how many of
    its N meetings are already satisfied?" is, and a mapping that can only
    hold one id per pair structurally cannot answer it. The three counting
    rules above are unchanged and are what the count is taken OVER: an
    existing Regular game counts, a cancelled one does not, an exhibition
    does not.

    The list is SORTED by game id, which is load-bearing rather than
    cosmetic: ``store.all_games()`` is insertion-ordered on the in-memory
    store but ``ORDER BY id`` on the SQL store, so an unsorted list would
    make WHICH existing Game a given already-scheduled row reports — and
    therefore the ``draft_fingerprint`` derived from it — differ between
    Memory, SQLite and PostgreSQL for identical data. Sorting here is the
    single point that makes the count and its reporting backend-independent.
    """
    wanted = set(division_scope)
    found = {}
    for g in store.all_games():
        if g.cancelled:
            continue
        scope = (g.league_season_id, g.division_id)
        if scope not in wanted:
            continue
        if g.game_type != GameType.REGULAR.value:
            continue
        found.setdefault(
            scope + (frozenset((g.home_team_id, g.away_team_id)),), []).append(g.id)
    return {key: sorted(ids) for key, ids in found.items()}


def reviewed_existing_counts(already_scheduled, league_season_id):
    """``{(league_season_id, division_id, pairing): count}`` — how many
    existing Games the reviewed proposal ACCOUNTED FOR per pairing (#375).

    The commit gate's race checks used to ask "does this pairing have a Game
    at all?", which was the right question only while every pairing needed
    exactly one meeting. With N meetings a pairing can legitimately have
    K < N existing Games *and still be in this batch* for its remaining
    N - K, so the question becomes "does it have MORE than the proposal
    reviewed?". This builds the baseline that comparison is made against,
    straight from the proposal's own ``already_scheduled`` rows.

    At the historical single round-robin this returns 0 for every pairing
    that has a ``draft_games`` row (such a pairing has no already-scheduled
    row), so ``len(existing) > 0`` — the exact pre-#375 predicate — is what
    the callers' comparison reduces to.
    """
    counts = {}
    for a in already_scheduled:
        key = (league_season_id, a.get("division_id"),
               frozenset((a["home_team_id"], a["away_team_id"])))
        counts[key] = counts.get(key, 0) + 1
    return counts


def raced_pairing_game_id(existing_now, reviewed_counts, key):
    """The id of a Game that appeared for ``key`` BEYOND what the reviewed
    proposal accounted for, or ``None`` if none did (#375).

    ``existing_now`` is a fresh :func:`_existing_pairing_games` snapshot
    taken under the commit's locks; ``reviewed_counts`` comes from
    :func:`reviewed_existing_counts`. A pairing whose current count exceeds
    the reviewed count had a Game created for it since the operator
    reviewed the preview — the terminal ``pairing_already_scheduled`` fact.

    The id returned is the first one the proposal did NOT account for, taken
    from the sorted list, so the error names the same Game on every backend.
    At the single round-robin (reviewed count 0, one existing Game) that is
    that Game's id — identical to the pre-#375 ``existing_now[key]`` lookup.
    """
    ids = existing_now.get(key, ())
    reviewed = reviewed_counts.get(key, 0)
    if len(ids) <= reviewed:
        return None
    return ids[reviewed]


def _split_already_scheduled(store, pairings, existing, league_season_id):
    """Partition ``pairings`` (``home, away, division_id`` triples) into
    ``(remaining, already_scheduled)`` against ``existing`` (from
    :func:`_existing_pairing_games`) — #206 slice 1: a pairing that already
    has a real Game is reported by name, not silently dropped (which would
    look identical to "not asked for") or silently re-proposed (the
    production risk this slice fixes). ``league_season_id`` is the single
    constant identity shared by every pairing in this call (#328 review
    round 2 — the lookup key must match Division, not just pairing).

    #375 — with a configurable format the SAME unordered pair appears
    ``meetings`` times in ``pairings``, and ``existing`` holds a sorted LIST
    of the Games that already satisfy it. Each pair's existing Games are
    consumed one per requested meeting, in order: the first K requested
    meetings (K = however many qualifying Games exist, capped at the number
    requested) become ``already_scheduled`` rows naming one existing Game
    each, and only the surplus stays in ``remaining``. That is precisely
    "regeneration fills only the missing meetings" — and, run again with
    nothing changed in between, every requested meeting is matched by an
    existing Game, ``remaining`` is empty, and the regeneration is a no-op.

    Consumption is by MEETING ORDER, not by trying to match each existing
    Game's actual home/away orientation. That is a deliberate determinism
    choice: meeting order is a total order fixed by the inputs alone, so the
    same facts always leave the same meetings outstanding, whereas
    orientation-matching would need a tie-break the inputs do not supply
    whenever several existing Games share an orientation.

    A pair with MORE existing Games than the format requests (an
    over-scheduled pairing) simply has no remaining meetings — the surplus
    is left unreported, never used to manufacture negative work.

    Each row also carries ``existing_game_count``: how many qualifying
    Games the pairing held WHEN THE PROPOSAL WAS BUILT — the whole count,
    not the capped number of rows. The commit gate's race check needs that
    number and nothing else can supply it: the rows are capped at the
    requested meetings, so counting them tells you ``min(K, N)`` and an
    over-scheduled pairing is indistinguishable from a raced one. Carrying
    it also puts the true count inside ``draft_fingerprint``, so a Game
    gained on an over-scheduled pairing — which changes no row's
    ``existing_game_id``, since the gained Game sorts after the ones the
    rows already name — stops being invisible to the wide gate too."""
    remaining, already = [], []
    consumed = {}
    for home, away, division_id in pairings:
        key = (league_season_id, division_id, frozenset((home, away)))
        existing_ids = existing.get(key, ())
        taken = consumed.get(key, 0)
        if taken < len(existing_ids):
            consumed[key] = taken + 1
            already.append({
                "home_team_id": home, "away_team_id": away,
                "home_team_name": _team_name(store, home),
                "away_team_name": _team_name(store, away),
                "division_id": division_id,
                "existing_game_id": existing_ids[taken],
                "existing_game_count": len(existing_ids),
            })
        else:
            remaining.append((home, away, division_id))
    return remaining, already


def _canonical_sort_key(row):
    """A row's own canonical JSON serialization (#328 review round 16),
    used purely to give ``_draft_fingerprint``'s bucket lists a
    deterministic order independent of generation order -- e.g. so the
    same SET of unscheduled rows hashes identically regardless of which
    order the constraint-evaluation loop happened to produce them in.
    Never delimiter-joined: unlike the pre-round-16 string encoding this
    replaces, a dict's own JSON serialization has no field-boundary
    ambiguity for a sort key to inherit."""
    return json.dumps(row, sort_keys=True, separators=(",", ":"), default=str)


def _draft_fingerprint(league_season_id, team_ids, draft_games, unscheduled,
                        unschedulable_teams, already_scheduled,
                        meetings_per_opponent=1, *, min_turnaround_minutes):
    """Deterministic identity of exactly what this proposal reviewed — #328
    review round 5, widened rounds 7, 10, 11, 12, 15, and 16. Bound into the response
    so the commit path can prove, right before writing, that this fact
    hasn't changed since: a Regular Game silently created or cancelled in
    the gap between the operator's preview and clicking Commit changes
    this fingerprint (via the already-scheduled split below), and so does
    the SAME pairing silently landing on a different `ice_slot_id`/time —
    whether because the reviewed slot became unavailable and another was
    chosen instead, or because `slot_ids`/`constraints` differed between
    the preview call and the commit's own regeneration. Either way the
    commit is refused (terminal `preview_stale`) rather than silently
    persisting a different placement, or a different reviewed BATCH, than
    the one reviewed and approved.

    #328 review round 7 correction: an earlier version of this fingerprint
    deliberately left placement out, reasoning that the per-row physical
    check at commit time (#277) already re-validates slot freedom fresh.
    That check proves the NEW placement is physically legal; it does not
    prove it is the placement the operator actually reviewed — a
    genuinely free, non-conflicting *different* slot for the identical
    pairing sails through it unnoticed. Binding `ice_slot_id` plus
    `start_time` here closes that gap; a slot's own time is included
    alongside its id as defense in depth against the (currently
    unsupported) possibility of a slot's time being edited in place
    without changing its id.

    #328 review round 10 correction: an earlier version bound only
    `draft_games`/`already_scheduled` — the two buckets a commit actually
    writes from. But the eligible-team set and the `unscheduled`/
    `unschedulable_teams` diagnosis are also part of what the operator
    reviewed, and the circle method's round-robin can leave the exact
    SAME pairing placed on the exact SAME slot even after a team
    registers or unregisters in the Division (the new/removed team
    reshuffles which OTHER pairings are missing/unscheduled without
    disturbing this one) — invisible to a fingerprint that never looked
    at `team_ids`/`unscheduled`. Binding the full eligible `team_ids` set
    plus each unscheduled pairing's own reason codes and the
    `unschedulable_teams` rollup closes that gap: any registration change
    or altered unscheduled diagnosis between Generate and Commit now
    invalidates the preview even when the placed/already-scheduled rows
    are byte-for-byte identical.

    #328 review round 11 correction: round 10 bound each unscheduled
    row's `reason_codes` but not its `reason` — the human-readable text
    the Scheduler UI actually renders. A scheduling-policy THRESHOLD
    change (`min_playable_minutes`, a turnover buffer, a curfew) between
    Generate and Commit can rewrite that text (e.g. "requires at least
    45" becoming "requires at least 50") while the reason CODE
    (`insufficient_playable_time`) and every other field stay identical,
    since the code is a fixed category but the message embeds the
    current policy value. Binding `reason` too closes that gap: the
    operator reviewed the specific explanation on screen, not just its
    category.

    #328 review round 12 finding 2 correction: `ice_slot_id` alone does
    not bind everything a `draft_games` row displays AND persists onto
    its created Game. Each row also carries `rink_name` (resolved once,
    at generation time, from the slot's Rink) and `end_time` — both
    written verbatim onto `Game.rink`/`Game.end_time` by commit, neither
    a live reference re-resolved at write time. A Rink rename (e.g. a
    repeat CSV import that re-labels an existing Rink) between Generate
    and Commit leaves `ice_slot_id`/`start_time` byte-for-byte identical
    — same slot, same instant — while the name the operator reviewed and
    the name about to be persisted onto the Game have already diverged;
    an in-place edit of a slot's own `end_time` (its `id`/`start_time`
    unchanged) is the identical risk for the playing-time span shown and
    persisted. Binding `rink_id` (defense in depth: the id itself is
    stable even across a rename, so this only ever adds coverage, never
    narrows what `rink_name` alone already catches), `rink_name`, and
    `end_time` closes both gaps.

    #328 review round 15 finding 1 correction: every row above binds each
    team by id only, never by the display name the operator actually
    reviewed on screen -- and the name a commit is about to persist onto
    the created Game (``Game.home_team``/``away_team``, resolved once at
    generation time via ``_team_name`` and never re-resolved from a live
    Team reference at commit time, exactly like round 12 finding 2's
    `rink_name`). A repeat teams/players CSV import that renames an
    existing Team (matched by `team_code`/`external_ref`, #92) between
    Generate and Commit leaves every id-keyed field above byte-for-byte
    identical -- same team, same pairing, same placement -- while the
    matchup label already differs. Binding `home_team_name`/
    `away_team_name` (`draft_games`/`already_scheduled`/`unscheduled`) and
    `team_name` (`unschedulable_teams`) closes that gap the same way
    `rink_name` closed it for ice.

    #328 review round 16 correction: every row above was previously a
    single ``"|"``-delimited string, e.g.
    ``f"{home_team_name}|{away_team_name}|..."``. Team names (and other
    operator-controlled text) are free-form and may themselves contain
    ``"|"``, so two DIFFERENT reviewed proposals could concatenate to the
    IDENTICAL pre-hash string: renaming a pairing's teams from
    (``"A|B"``, ``"C"``) to (``"A"``, ``"B|C"``) leaves
    ``f"{home}|{away}"`` as ``"A|B|C"`` either way -- a stale preview could
    then commit undetected. Each row is now a STRUCTURED dict of typed
    fields, embedded directly in the hashed JSON payload (whose own
    quoting/escaping makes field boundaries unambiguous -- ``{"a":"X|Y",
    "b":"Z"}`` and ``{"a":"X","b":"Y|Z"}`` serialize to textually
    different JSON) rather than pre-flattened to a string; row ORDER
    within each bucket is still made deterministic by sorting on each
    row's own canonical JSON serialization (`_canonical_sort_key`), never
    by concatenating fields together the way the old sort key did.
    """
    missing = sorted((
        {
            "division_id": d.get("division_id"),
            "home_team_id": d["home_team_id"],
            "away_team_id": d["away_team_id"],
            "home_team_name": d.get("home_team_name"),
            "away_team_name": d.get("away_team_name"),
            "ice_slot_id": d.get("ice_slot_id"),
            "start_time": d.get("start_time"),
            "end_time": d.get("end_time"),
            "rink_id": d.get("rink_id"),
            "rink_name": d.get("rink_name"),
        }
        for d in draft_games), key=_canonical_sort_key)
    scheduled = sorted((
        {
            "division_id": a.get("division_id"),
            "home_team_id": a["home_team_id"],
            "away_team_id": a["away_team_id"],
            "home_team_name": a.get("home_team_name"),
            "away_team_name": a.get("away_team_name"),
            "existing_game_id": a["existing_game_id"],
        }
        for a in already_scheduled), key=_canonical_sort_key)
    unresolved = sorted((
        {
            "division_id": u.get("division_id"),
            "home_team_id": u["home_team_id"],
            "away_team_id": u["away_team_id"],
            "home_team_name": u.get("home_team_name"),
            "away_team_name": u.get("away_team_name"),
            "reason_codes": sorted(u.get("reason_codes") or ()),
            "reason": u.get("reason") or "",
            # #373 — bound for the same reason round 11 bound `reason`: this
            # is part of what the operator reviewed. A team's conflicting
            # game being cancelled (or a NEW one appearing) between Generate
            # and Commit rewrites which team is named and which
            # `conflict_game_id` it points at, while the pairing, its reason
            # codes, and every placement above can stay byte-for-byte
            # identical — the reviewed diagnosis has changed even though the
            # reviewed batch looks unmoved.
            "team_conflicts": list(u.get("team_conflicts") or ()),
            # #390 — bound for the identical reason `team_conflicts` is: the
            # blocking game and the shortfall an operator read on screen are
            # part of what was reviewed. A blocking game being cancelled (or
            # moved a few minutes) between Generate and Commit rewrites the
            # named game and the shortfall while the pairing, its reason
            # codes and every placement above stay byte-for-byte identical.
            "turnaround_conflicts": list(u.get("turnaround_conflicts") or ()),
        }
        for u in unscheduled), key=_canonical_sort_key)
    blocked_teams = sorted((
        {
            "team_id": t["team_id"],
            "team_name": t.get("team_name"),
            "reason_codes": sorted(t.get("reason_codes") or ()),
        }
        for t in unschedulable_teams), key=_canonical_sort_key)
    payload = {
        "league_season_id": league_season_id,
        "team_ids": sorted(team_ids),
        "missing": missing,
        "already_scheduled": scheduled,
        "unscheduled": unresolved,
        "unschedulable_teams": blocked_teams,
        # #375 — the requested regular-season FORMAT is part of what the
        # operator reviewed, so it is bound directly rather than left to be
        # inferred from the buckets above. Changing N almost always changes
        # those buckets too, but "almost always" is not the standard the
        # rest of this fingerprint is held to: binding the number itself
        # makes "the preview was generated for a different format than the
        # commit is asking for" a guaranteed mismatch instead of one that
        # happens to fall out of the row lists.
        "meetings_per_opponent": meetings_per_opponent,
        # #390 review blocker — the reviewed TURNAROUND POLICY, bound directly
        # for exactly the reason `meetings_per_opponent` above is, and it is
        # the third time this repo has had to learn it (#382 bound the format,
        # #381 had to persist and replay it).
        #
        # A parameter that changes what is ALLOWED but is not bound to what
        # was REVIEWED is echoed, not enforced. Before this, a caller could
        # Generate with a non-zero turnaround and Commit with 0 whenever both
        # values happened to produce identical rows — and committing with 0
        # skips the commit-time turnaround state entirely, so a same-team game
        # landing inside the reviewed gap could be committed straight through
        # a safety rule the operator had explicitly asked for.
        #
        # Binding the NORMALIZED value (a float, from
        # :func:`_normalize_constraints`) rather than the raw request field
        # means `60`, `60.0` and an equivalent restatement hash identically,
        # while any real change to the reviewed policy is a guaranteed
        # mismatch instead of one that happens to fall out of the row lists.
        #
        # The parameter is keyword-only and REQUIRED, with no default: this
        # function has two call sites (Division-only and League-wide), and a
        # default would let a missed one silently reopen the hole on that path
        # rather than failing loudly.
        "min_turnaround_minutes": min_turnaround_minutes,
    }
    return hashlib.sha256(json.dumps(
        payload, sort_keys=True, separators=(",", ":"), default=str
    ).encode()).hexdigest()[:16]


def draft_schedule(store, division_id, slot_ids=None, constraints=None,
                   meetings_per_opponent=None):
    """Generate a draft round-robin schedule for a division (#84/#85).

    Returns ``{division_id, team_count, draft_games, unscheduled,
    already_scheduled, unschedulable_teams, draft_fingerprint}``. Each
    pairing takes the earliest available slot that satisfies the optional
    constraints; a pairing with no valid slot is returned in ``unscheduled``
    with the reason(s) that blocked it. A pairing that already has a real
    Game (#206 slice 1 — see :func:`_existing_pairing_games`) is reported in
    ``already_scheduled`` instead of being re-proposed or silently dropped.
    ``draft_fingerprint`` (#328 review round 5, widened round 7 — see
    :func:`_draft_fingerprint`) binds this exact missing/already-scheduled
    split AND each missing pairing's placement (slot, time) so a later
    commit can detect drift in either. Nothing is persisted.

    ``meetings_per_opponent`` (#375) configures how many times each team
    plays every other; ``None`` keeps the historical single round-robin.
    Existing Regular Games count toward the requirement, so a regeneration
    proposes only the meetings still missing.
    """
    meetings = _normalize_meetings(meetings_per_opponent)
    # A division's teams are those validly registered in it this season (#180),
    # via the shared resolver — active rows whose Team exists and whose league
    # matches, so an orphaned or cross-league registration row can never enter a
    # draft (#199/#200 review). Same source of truth game creation, moves,
    # publishing, and standings use.
    teams = sorted(registered_team_ids_in_division(store, division_id))
    all_pairings = [(h, a, division_id)
                    for h, a in round_robin_pairings(teams, meetings)]
    # #328 review — scope the exclusion by this Division's own LeagueSeason,
    # not division_id alone (see _existing_pairing_games).
    division = store.get_division(division_id) if division_id else None
    ls_id = division.league_season_id if division else None
    league_season = store.get_league_season(ls_id) if ls_id else None
    scope = {(ls_id, division_id)}
    pairings, already_scheduled = _split_already_scheduled(
        store, all_pairings, _existing_pairing_games(store, scope), ls_id)
    slots = _available_game_slots(store, slot_ids)
    active = _active_game_slot_pairs(store)
    draft_games, unscheduled = _assign_ice(
        store, pairings, slots, constraints,
        policy_check=_policy_advisor(
            store, _resolve_division_season_id(store, division_id), active),
        team_spans=_persisted_team_spans(active),
        active_pairs=active,
        explanation_context={
            "season_id": league_season.season_id if league_season else None,
            "league_id": league_season.league_id if league_season else None,
            "division_id": division_id,
        })
    unschedulable_teams = _unschedulable_teams(
        store, teams, pairings, unscheduled)
    # #390 — resolved ONCE, then both echoed and hashed. Computing it twice
    # would let the number the operator reviewed and the number the commit
    # compares against drift apart, which is the whole class of defect the
    # binding below exists to close.
    turnaround = turnaround_minutes(constraints)
    return {
        "division_id": division_id, "team_count": len(teams),
        "meetings_per_opponent": meetings,
        # #390 — echo the NORMALIZED turnaround the proposal was generated
        # under, exactly as ``meetings_per_opponent`` above echoes the
        # format. The Scheduler UI must send the same value back at Commit
        # (it is an input to the regeneration the fingerprint is compared
        # against), and reading it off the reviewed proposal rather than off
        # a live control is what stops an operator who nudges the control
        # while reading a valid preview from silently redefining the batch
        # they are about to commit.
        "min_turnaround_minutes": turnaround,
        "draft_games": draft_games, "unscheduled": unscheduled,
        "already_scheduled": already_scheduled,
        "unschedulable_teams": unschedulable_teams,
        "draft_fingerprint": _draft_fingerprint(
            ls_id, teams, draft_games, unscheduled, unschedulable_teams,
            already_scheduled, meetings,
            min_turnaround_minutes=turnaround),
    }


def draft_schedule_for_league(store, season_id, league_id, division_id=None,
                              slot_ids=None, constraints=None,
                              meetings_per_opponent=None):
    """Generate a draft schedule for a whole League within a Season, optionally
    narrowed to one Division (#233 Slice G).

    Teams are resolved through ``SeasonTeamRegistration`` exactly like the
    division-only entry point, but grouped by each registration's own
    Division (or "no Division" when the League itself has none) — a
    league-wide draft never pairs a Gold team against a Silver team just
    because they share a League; each Division's teams get their own
    round-robin, all sharing the same season-scoped ice pool for assignment.

    ``meetings_per_opponent`` (#375) applies to every Division group alike:
    each group's own round-robin is repeated N times, so a team plays every
    opponent IN ITS OWN DIVISION N times and still never meets one from
    another Division.
    """
    meetings = _normalize_meetings(meetings_per_opponent)
    groups = registered_teams_by_division_in_league(
        store, season_id, league_id, division_id)
    all_pairings = []
    all_teams = set()
    for div_id, team_ids in groups.items():
        all_teams |= team_ids
        all_pairings.extend(
            (h, a, div_id)
            for h, a in round_robin_pairings(sorted(team_ids), meetings))
    # #328 review — scope the exclusion by THIS League+Season's own
    # LeagueSeason, not division_id alone (see _existing_pairing_games):
    # the "no Division" group's div_id is None for every League, so
    # division_id alone would match a division-less Regular Game from any
    # other League/Season sharing the same two (permanent) team ids.
    league_season = (store.league_season_for(league_id, season_id)
                     if league_id else None)
    ls_id = league_season.id if league_season is not None else None
    scope = {(ls_id, div_id) for div_id in groups.keys()}
    pairings, already_scheduled = _split_already_scheduled(
        store, all_pairings, _existing_pairing_games(store, scope), ls_id)
    slots = _available_game_slots(store, slot_ids)
    active = _active_game_slot_pairs(store)
    draft_games, unscheduled = _assign_ice(
        store, pairings, slots, constraints,
        policy_check=_policy_advisor(store, season_id, active),
        team_spans=_persisted_team_spans(active),
        active_pairs=active,
        explanation_context={
            "season_id": season_id,
            "league_id": league_id,
            "division_id": division_id,
        })
    unschedulable_teams = _unschedulable_teams(
        store, all_teams, pairings, unscheduled)
    # #390 — see the Division-only entry point above. This is the SECOND
    # fingerprint call site, and binding only the other one would leave the
    # reviewed policy unenforced on every League-wide commit.
    turnaround = turnaround_minutes(constraints)
    return {
        "season_id": season_id, "league_id": league_id,
        "division_id": division_id, "team_count": len(all_teams),
        "meetings_per_opponent": meetings,
        "min_turnaround_minutes": turnaround,
        "draft_games": draft_games, "unscheduled": unscheduled,
        "already_scheduled": already_scheduled,
        "unschedulable_teams": unschedulable_teams,
        "draft_fingerprint": _draft_fingerprint(
            ls_id, all_teams, draft_games, unscheduled, unschedulable_teams,
            already_scheduled, meetings,
            min_turnaround_minutes=turnaround),
    }
