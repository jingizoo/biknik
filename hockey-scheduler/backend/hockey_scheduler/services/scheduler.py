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

A team is never proposed for two overlapping games (#373). Occupancy is
team-wide — keyed by stable team id and actual interval, never by rink or
display name — and covers BOTH already-persisted games and the candidates
this very batch has already accepted, so the preview can no longer show (and
offer to commit) a physically impossible schedule.
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
from .setup_service import SetupService as _PolicySetup


def round_robin_pairings(team_ids, meetings=1):
    """Round-robin pairings via the circle method (deterministic).

    Returns a flat list of ``(home_team_id, away_team_id)`` in round order.
    An odd number of teams yields a bye each round (the byed team simply has
    no game that round). Home/away alternates by round for basic balance.

    ``meetings`` (#375) is how many times each team plays every other team.
    ``meetings=1`` is a single round robin and returns EXACTLY what this
    function has always returned — the default path is byte-identical, which
    matters because the precise output order is pinned by existing tests.

    For ``meetings=N`` the base single round robin is emitted N times, and
    every ODD-indexed cycle is mirrored so the pair swaps ends:

        cycle 0 -> (home, away)     cycle 1 -> (away, home)     cycle 2 -> ...

    Two properties follow, and both are asserted by #375's regressions:

    * **Deterministic.** The orientation depends only on ``sorted(team_ids)``
      and the cycle index — no clock, no randomness, no store read. The same
      inputs always produce the same split, which is the guarantee #375
      actually makes falsifiable.
    * **Per-pair balanced.** Even N splits a pair exactly N/2 each way. Odd N
      cannot split evenly, so the extra game goes to whichever team the BASE
      round robin already designates as home for that pair: ceil(N/2) to it
      and floor(N/2) to the other. For N=3 that is 2-1, never 3-0.

    Emission is CYCLE-grouped (the whole schedule repeated: ABC ABC ABC), not
    pairing-grouped (AABBCC). ``_assign_ice`` is greedy earliest-slot-first,
    so pairing-grouped output would stack a pair's repeat meetings into
    adjacent slots; cycle-grouped spreads them across the ice pool instead.

    Per-TEAM home totals are NOT equalized: they inherit whatever imbalance
    the single round robin already has (for 6 teams the base gives one team 4
    home out of 5), which over odd N carries through rather than cancelling.
    Equalizing that would mean changing the ``meetings=1`` output, which is
    out of scope here — see the #375 PR body.
    """
    teams = sorted(team_ids)
    if len(teams) < 2:
        return []
    if len(teams) % 2 == 1:
        teams = teams + [None]  # None marks the bye
    n = len(teams)
    fixed, rot = teams[0], teams[1:]
    pairings = []
    for r in range(n - 1):
        arrangement = [fixed] + rot
        for i in range(n // 2):
            a, b = arrangement[i], arrangement[n - 1 - i]
            if a is None or b is None:
                continue  # bye
            pairings.append((a, b) if r % 2 == 0 else (b, a))
        rot = [rot[-1]] + rot[:-1]  # rotate all but the fixed team
    if meetings <= 1:
        return pairings
    repeated = []
    for cycle in range(meetings):
        repeated.extend(
            pairings if cycle % 2 == 0
            else [(away, home) for home, away in pairings])
    return repeated


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
    ``min_rest_hours`` (number ≥ 0), ``max_games_per_team_per_day`` (int ≥ 0),
    ``meetings_per_opponent`` (int ≥ 1, #375 — how many times each team plays
    every other; 1 is the single round robin this engine has always drafted).
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

    max_per_day = c.get("max_games_per_team_per_day")
    max_per_day = 0 if max_per_day is None else max_per_day
    if isinstance(max_per_day, bool) or not isinstance(max_per_day, int) \
            or max_per_day < 0:
        raise ValidationError(
            "'max_games_per_team_per_day' must be an integer >= 0.")

    meetings = c.get("meetings_per_opponent")
    meetings = 1 if meetings is None else meetings
    # Same bool-is-an-int guard as above: True must not sneak through as 1.
    if isinstance(meetings, bool) or not isinstance(meetings, int) \
            or meetings < 1:
        raise ValidationError(
            "'meetings_per_opponent' must be an integer >= 1.")

    return {
        "team_blackouts": _blackout_map(c.get("team_blackouts"), "team_blackouts"),
        "rink_blackouts": _blackout_map(c.get("rink_blackouts"), "rink_blackouts"),
        "season_blackout_dates": _date_set(
            c.get("season_blackout_dates"), "season_blackout_dates"),
        "holiday_dates": _date_set(c.get("holiday_dates"), "holiday_dates"),
        "min_rest_hours": float(min_rest),
        "max_per_day": int(max_per_day),
        "meetings_per_opponent": int(meetings),
    }


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


def _slot_reason(slot, home, away, con, team_slots):
    """Why ``slot`` can't host ``home`` vs ``away`` under the constraints, as
    ``(code, message)``, or ``(None, None)`` if it can. ``team_slots`` maps
    team_id -> [assigned start_time]."""
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
            for t in team_slots.get(tid, []):
                if abs(slot.start_time - t) < rest:
                    return MIN_REST, "minimum rest between games not met"
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

    def check(slot, tentative_spans):
        violation = setup._slot_policy_violation(
            slot, season_id, extra_rink_spans=tentative_spans,
            rink_games=pairs)
        if violation is None:
            return None, None
        message, details = violation
        return details["reason"], message

    return check


def _resolve_division_season_id(store, division_id):
    """A Division's Season via its LeagueSeason (#283 chain), or ``None`` for
    a dangling/legacy row — the advisory pass simply stays off then."""
    division = store.get_division(division_id) if division_id else None
    ls = (store.get_league_season(division.league_season_id)
          if division is not None and division.league_season_id else None)
    return ls.season_id if ls is not None else None


def _assign_ice(store, pairings, slots, con, policy_check=None,
                team_spans=None):
    """Greedy earliest-slot-first assignment shared by every entry point.

    ``con`` is normalized constraints from :func:`_normalize_constraints`
    (#375). Normalization used to happen here, but ``meetings_per_opponent``
    has to be read BEFORE pairings are generated, so both entry points now
    normalize once up front and pass the result down — which also means a
    malformed ``constraints`` raises its structured ``ValidationError``
    before any pairing work rather than after.

    Raw (un-normalized) constraints, including ``None``, are still accepted
    so every existing direct caller keeps working unchanged. The two forms
    are distinguishable without ambiguity: ``max_per_day`` is a key only
    :func:`_normalize_constraints` ever produces (the raw request key is
    ``max_games_per_team_per_day``), so its presence means "already done".

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
    if not isinstance(con, dict) or "max_per_day" not in con:
        con = _normalize_constraints(con)
    draft_games, unscheduled = [], []
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
        for s in slots:
            if s.id in used:
                continue
            code, message = _slot_reason(s, home, away, con, team_slots)
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
            unscheduled.append({
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
            })
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
    away_team_id})): [existing_game_id, ...]}`` for every non-cancelled
    REGULAR Game already in any of ``division_scope`` (#206 slice 1 —
    preserve existing Games, generate only missing round-robin matchups).

    The value is a SORTED LIST, not a single id (#375). Once a pairing can
    legitimately be required N times, "does this pairing already have a
    game" is the wrong question — the caller needs to know HOW MANY it has,
    so it can propose only the shortfall. The sort is load-bearing rather
    than cosmetic: ``store.all_games()`` iteration order is not guaranteed
    and genuinely differs between ``InMemoryStore``'s dict and ``SqlStore``
    on SQLite vs PostgreSQL, and these ids reach ``already_scheduled[]``,
    which is bound into ``draft_fingerprint`` — so an unsorted list would
    hash differently per backend for identical data. That is exactly the
    "counting crosses the backend boundary" case #375 calls out.
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
    game satisfies a Regular pairing."""
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
        key = scope + (frozenset((g.home_team_id, g.away_team_id)),)
        found.setdefault(key, []).append(g.id)
    for ids in found.values():
        ids.sort()
    return found


def _split_already_scheduled(store, pairings, existing, league_season_id):
    """Partition ``pairings`` (``home, away, division_id`` triples) into
    ``(remaining, already_scheduled)`` against ``existing`` (from
    :func:`_existing_pairing_games`) — #206 slice 1: a pairing that already
    has a real Game is reported by name, not silently dropped (which would
    look identical to "not asked for") or silently re-proposed (the
    production risk this slice fixes). ``league_season_id`` is the single
    constant identity shared by every pairing in this call (#328 review
    round 2 — the lookup key must match Division, not just pairing).

    Matching is by OCCURRENCE, not by membership (#375). ``pairings`` already
    contains a pairing exactly N times, so N never needs to be passed in
    here: the k-th occurrence of a pairing consumes the k-th existing game
    for it. A pairing needed 3 times with 1 existing game therefore yields 1
    ``already_scheduled`` row and 2 ``remaining`` triples — fill-only-missing
    falls out of the arithmetic rather than needing a separate code path, and
    so does idempotence (once all N exist, every occurrence is accounted and
    ``remaining`` is empty).

    A pairing with MORE existing games than the format requires (an operator
    lowered N, or games were hand-created) reports only the first N and
    leaves the surplus untouched — this is a fill-only-missing feature and
    has no mandate to delete anything. See the #375 PR body.
    """
    remaining, already = [], []
    seen = {}
    for home, away, division_id in pairings:
        key = (league_season_id, division_id, frozenset((home, away)))
        index = seen.get(key, 0)
        seen[key] = index + 1
        existing_ids = existing.get(key) or ()
        if index < len(existing_ids):
            already.append({
                "home_team_id": home, "away_team_id": away,
                "home_team_name": _team_name(store, home),
                "away_team_name": _team_name(store, away),
                "division_id": division_id,
                "existing_game_id": existing_ids[index],
            })
        else:
            remaining.append((home, away, division_id))
    return remaining, already


def pairing_accounted_counts(already_scheduled, league_season_id):
    """``{(league_season_id, division_id, pairing): count}`` — how many
    existing games the reviewed proposal already accounted for, per pairing
    (#375).

    Shared by both commit facades so the count arithmetic cannot drift
    between them: ``api/service.py`` and ``api/league_scoped_service.py``
    carry two independent copies of the commit body, and the league-scoped
    one is what HTTP actually reaches."""
    counts = {}
    for row in already_scheduled or ():
        key = (league_season_id, row.get("division_id"),
               frozenset((row["home_team_id"], row["away_team_id"])))
        counts[key] = counts.get(key, 0) + 1
    return counts


def unaccounted_pairing_row(draft_games, already_scheduled, league_season_id,
                            existing_now):
    """The first proposed row whose pairing gained an existing game the
    reviewed proposal did NOT account for, with the id of the game that beat
    it — or ``None`` (#375).

    Membership is the wrong test once a pairing may legitimately be required
    N times: meeting 2 of a pair is proposed while meeting 1 already exists,
    so ``key in existing_now`` would refuse every valid repeat meeting. The
    right test is arithmetic — refuse only when the pairing has MORE existing
    games than the proposal accounted for."""
    accounted = pairing_accounted_counts(already_scheduled, league_season_id)
    consumed = dict(accounted)
    for row in draft_games or ():
        key = (league_season_id, row.get("division_id"),
               frozenset((row["home_team_id"], row["away_team_id"])))
        index = consumed.get(key, 0)
        consumed[key] = index + 1
        ids = existing_now.get(key) or ()
        if index < len(ids):
            return row, ids[index]
    return None


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
                        meetings_per_opponent=1):
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
        # #375. Belt-and-braces in the ordinary case — a different N already
        # changes the row multiset — but it closes the degenerate scope where
        # every bucket is empty for EVERY N (fewer than two teams), and it
        # follows the rounds 10/12/15 precedent of binding the reviewed INPUT
        # dimension explicitly rather than trusting the output to differ.
        "meetings_per_opponent": int(meetings_per_opponent),
    }
    return hashlib.sha256(json.dumps(
        payload, sort_keys=True, separators=(",", ":"), default=str
    ).encode()).hexdigest()[:16]


def draft_schedule(store, division_id, slot_ids=None, constraints=None):
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
    """
    # A division's teams are those validly registered in it this season (#180),
    # via the shared resolver — active rows whose Team exists and whose league
    # matches, so an orphaned or cross-league registration row can never enter a
    # draft (#199/#200 review). Same source of truth game creation, moves,
    # publishing, and standings use.
    # #375 — normalize FIRST: meetings_per_opponent drives pairing generation,
    # so it has to be read before the round robin is built.
    con = _normalize_constraints(constraints)
    meetings = con["meetings_per_opponent"]
    teams = sorted(registered_team_ids_in_division(store, division_id))
    all_pairings = [(h, a, division_id)
                    for h, a in round_robin_pairings(teams, meetings=meetings)]
    # #328 review — scope the exclusion by this Division's own LeagueSeason,
    # not division_id alone (see _existing_pairing_games).
    division = store.get_division(division_id) if division_id else None
    ls_id = division.league_season_id if division else None
    scope = {(ls_id, division_id)}
    pairings, already_scheduled = _split_already_scheduled(
        store, all_pairings, _existing_pairing_games(store, scope), ls_id)
    slots = _available_game_slots(store, slot_ids)
    active = _active_game_slot_pairs(store)
    draft_games, unscheduled = _assign_ice(
        store, pairings, slots, con,
        policy_check=_policy_advisor(
            store, _resolve_division_season_id(store, division_id), active),
        team_spans=_persisted_team_spans(active))
    unschedulable_teams = _unschedulable_teams(
        store, teams, pairings, unscheduled)
    return {
        "division_id": division_id, "team_count": len(teams),
        "draft_games": draft_games, "unscheduled": unscheduled,
        "already_scheduled": already_scheduled,
        "unschedulable_teams": unschedulable_teams,
        "draft_fingerprint": _draft_fingerprint(
            ls_id, teams, draft_games, unscheduled, unschedulable_teams,
            already_scheduled, meetings_per_opponent=meetings),
    }


def draft_schedule_for_league(store, season_id, league_id, division_id=None,
                              slot_ids=None, constraints=None):
    """Generate a draft schedule for a whole League within a Season, optionally
    narrowed to one Division (#233 Slice G).

    Teams are resolved through ``SeasonTeamRegistration`` exactly like the
    division-only entry point, but grouped by each registration's own
    Division (or "no Division" when the League itself has none) — a
    league-wide draft never pairs a Gold team against a Silver team just
    because they share a League; each Division's teams get their own
    round-robin, all sharing the same season-scoped ice pool for assignment.
    """
    # #375 — normalize FIRST (see draft_schedule). The same N applies to every
    # Division group in this League: N is request-scoped, so there is nowhere
    # for a per-Division value to come from.
    con = _normalize_constraints(constraints)
    meetings = con["meetings_per_opponent"]
    groups = registered_teams_by_division_in_league(
        store, season_id, league_id, division_id)
    all_pairings = []
    all_teams = set()
    for div_id, team_ids in groups.items():
        all_teams |= team_ids
        all_pairings.extend(
            (h, a, div_id) for h, a in round_robin_pairings(
                sorted(team_ids), meetings=meetings))
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
        store, pairings, slots, con,
        policy_check=_policy_advisor(store, season_id, active),
        team_spans=_persisted_team_spans(active))
    unschedulable_teams = _unschedulable_teams(
        store, all_teams, pairings, unscheduled)
    return {
        "season_id": season_id, "league_id": league_id,
        "division_id": division_id, "team_count": len(all_teams),
        "draft_games": draft_games, "unscheduled": unscheduled,
        "already_scheduled": already_scheduled,
        "unschedulable_teams": unschedulable_teams,
        "draft_fingerprint": _draft_fingerprint(
            ls_id, all_teams, draft_games, unscheduled, unschedulable_teams,
            already_scheduled, meetings_per_opponent=meetings),
    }
