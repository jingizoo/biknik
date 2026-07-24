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
"""

from datetime import date, timedelta

from ..domain import GameType, IceSlotStatus, IceSlotType
from ..domain.errors import ValidationError
from .league_scope import (
    registered_team_ids_in_division,
    registered_teams_by_division_in_league,
)
from .setup_service import SetupService as _PolicySetup


def round_robin_pairings(team_ids):
    """Single round-robin pairings via the circle method (deterministic).

    Returns a flat list of ``(home_team_id, away_team_id)`` in round order.
    Every team plays every other exactly once. An odd number of teams yields a
    bye each round (the byed team simply has no game that round). Home/away
    alternates by round for basic balance.
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
        "max_per_day": int(max_per_day),
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


def _policy_advisor(store, season_id):
    """Per-run advisory closure over the shared policy evaluation (#277
    Slice B). Returns ``check(slot, tentative_spans) -> (code, message)`` or
    ``(None, None)``; ``tentative_spans`` is this run's not-yet-persisted
    same-rink picks as ``(slot_id, start, end)``. Plain reads only. ALWAYS
    active (#318 review): the gate's slot_overlap_conflict is unconditional
    — it binds with zero policy rows and even when no season resolves
    (season-less legacy divisions) — so the advisory mirrors it through the
    same shared implementation. The active-game inventory is snapshotted
    ONCE per run and handed down, so a draft over N candidate slots never
    re-scans the game table N times; with no policies and no overlapping
    candidates the proposal output remains byte-identical to pre-Slice-B."""
    setup = _PolicySetup(store)
    pairs = []
    for g in store.all_games():
        if g.cancelled or not g.ice_slot_id:
            continue
        s = store.get_ice_slot(g.ice_slot_id)
        if s is not None:
            pairs.append((g, s))

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


def _assign_ice(store, pairings, slots, constraints, policy_check=None):
    """Greedy earliest-slot-first assignment shared by every entry point.

    ``pairings`` is ``[(home_team_id, away_team_id, division_id_or_None), ...]``
    — a league-wide draft tags each pairing with the Division its two teams
    are actually registered in (or ``None``), so the resulting rows stay
    per-pairing accurate even though one draft can span several Divisions.
    Returns ``(draft_games, unscheduled)``.
    """
    con = _normalize_constraints(constraints)
    draft_games, unscheduled = [], []
    used = set()
    team_slots = {}  # team_id -> [assigned start_time]
    rink_spans = {}  # rink_id -> [(slot_id, start, end)] picked this run (#277)
    for home, away, division_id in pairings:
        chosen, messages, codes = None, [], []
        for s in slots:
            if s.id in used:
                continue
            code, message = _slot_reason(s, home, away, con, team_slots)
            if code is None and policy_check is not None:
                code, message = policy_check(
                    s, rink_spans.get(s.rink_id, ()))
            if code is None:
                chosen = s
                break
            messages.append(message)
            codes.append(code)
        if chosen is not None:
            used.add(chosen.id)
            team_slots.setdefault(home, []).append(chosen.start_time)
            team_slots.setdefault(away, []).append(chosen.start_time)
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
            })
    return draft_games, unscheduled


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


def _existing_pairing_games(store, division_ids):
    """``{frozenset({home_team_id, away_team_id}): existing_game_id}`` for
    every non-cancelled REGULAR Game already in any of ``division_ids``
    (#206 slice 1 — preserve existing Games, generate only missing
    round-robin matchups). Draft or committed, published or not,
    roster-locked or not all count — the risk this closes is re-running
    Generate silently proposing (and Commit silently creating) a duplicate
    for a pairing that already has ANY real Game, not only a published one.
    A CANCELLED game does not count (that is exactly the signal the
    pairing needs re-scheduling); an EXHIBITION game does not count either
    (#283: it never affects standings and was never the round-robin
    obligation) — only a Regular game satisfies a Regular pairing."""
    wanted = set(division_ids)
    found = {}
    for g in store.all_games():
        if g.cancelled or g.division_id not in wanted:
            continue
        if g.game_type != GameType.REGULAR.value:
            continue
        found[frozenset((g.home_team_id, g.away_team_id))] = g.id
    return found


def _split_already_scheduled(store, pairings, existing):
    """Partition ``pairings`` (``home, away, division_id`` triples) into
    ``(remaining, already_scheduled)`` against ``existing`` (from
    :func:`_existing_pairing_games`) — #206 slice 1: a pairing that already
    has a real Game is reported by name, not silently dropped (which would
    look identical to "not asked for") or silently re-proposed (the
    production risk this slice fixes)."""
    remaining, already = [], []
    for home, away, division_id in pairings:
        existing_game_id = existing.get(frozenset((home, away)))
        if existing_game_id is not None:
            already.append({
                "home_team_id": home, "away_team_id": away,
                "home_team_name": _team_name(store, home),
                "away_team_name": _team_name(store, away),
                "division_id": division_id,
                "existing_game_id": existing_game_id,
            })
        else:
            remaining.append((home, away, division_id))
    return remaining, already


def draft_schedule(store, division_id, slot_ids=None, constraints=None):
    """Generate a draft round-robin schedule for a division (#84/#85).

    Returns ``{division_id, team_count, draft_games, unscheduled,
    already_scheduled, unschedulable_teams}``. Each pairing takes the
    earliest available slot that satisfies the optional constraints; a
    pairing with no valid slot is returned in ``unscheduled`` with the
    reason(s) that blocked it. A pairing that already has a real Game (#206
    slice 1 — see :func:`_existing_pairing_games`) is reported in
    ``already_scheduled`` instead of being re-proposed or silently dropped.
    Nothing is persisted.
    """
    # A division's teams are those validly registered in it this season (#180),
    # via the shared resolver — active rows whose Team exists and whose league
    # matches, so an orphaned or cross-league registration row can never enter a
    # draft (#199/#200 review). Same source of truth game creation, moves,
    # publishing, and standings use.
    teams = sorted(registered_team_ids_in_division(store, division_id))
    all_pairings = [(h, a, division_id) for h, a in round_robin_pairings(teams)]
    pairings, already_scheduled = _split_already_scheduled(
        store, all_pairings, _existing_pairing_games(store, (division_id,)))
    slots = _available_game_slots(store, slot_ids)
    draft_games, unscheduled = _assign_ice(
        store, pairings, slots, constraints,
        policy_check=_policy_advisor(
            store, _resolve_division_season_id(store, division_id)))
    return {
        "division_id": division_id, "team_count": len(teams),
        "draft_games": draft_games, "unscheduled": unscheduled,
        "already_scheduled": already_scheduled,
        "unschedulable_teams": _unschedulable_teams(
            store, teams, pairings, unscheduled),
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
    groups = registered_teams_by_division_in_league(
        store, season_id, league_id, division_id)
    all_pairings = []
    all_teams = set()
    for div_id, team_ids in groups.items():
        all_teams |= team_ids
        all_pairings.extend(
            (h, a, div_id) for h, a in round_robin_pairings(sorted(team_ids)))
    pairings, already_scheduled = _split_already_scheduled(
        store, all_pairings, _existing_pairing_games(store, groups.keys()))
    slots = _available_game_slots(store, slot_ids)
    draft_games, unscheduled = _assign_ice(
        store, pairings, slots, constraints,
        policy_check=_policy_advisor(store, season_id))
    return {
        "season_id": season_id, "league_id": league_id,
        "division_id": division_id, "team_count": len(all_teams),
        "draft_games": draft_games, "unscheduled": unscheduled,
        "already_scheduled": already_scheduled,
        "unschedulable_teams": _unschedulable_teams(
            store, all_teams, pairings, unscheduled),
    }
