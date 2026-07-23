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
"""

from datetime import date, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ..domain import IceSlotStatus, IceSlotType
from ..domain.errors import ValidationError
from ..domain.time_utils import intervals_overlap
from .ice_availability import parse_hhmm
from .ice_policy import effective_policy
from .league_scope import (
    registered_team_ids_in_division,
    registered_teams_by_division_in_league,
)


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
# #277: the same policy reason codes the shared checker emits, so a draft never
# proposes ice the commit's _assert_slot_free_for_game would then reject.
CURFEW_VIOLATION = "curfew_violation"
TURNOVER_CONFLICT = "turnover_buffer_conflict"


def _resolve_tz(program):
    """The Program's ``tzinfo`` for curfew wall-clock math, or ``None`` when it
    is unknown/invalid — in which case curfew is not enforced (no zone, no
    guess), mirroring the shared checker."""
    name = getattr(program, "timezone", None) if program is not None else None
    if not name:
        return None
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return None


def _program_for_season(store, season_id):
    season = store.get_season(season_id) if season_id else None
    pid = getattr(season, "program_id", None) if season is not None else None
    return store.get_program(pid) if pid else None


def _season_id_for_division(store, division_id):
    """A Division's Season resolves via its LeagueSeason (#283)."""
    division = store.get_division(division_id) if division_id else None
    ls_id = getattr(division, "league_season_id", None) if division else None
    ls = store.get_league_season(ls_id) if ls_id else None
    return getattr(ls, "season_id", None) if ls else None


def _slot_reason(slot, home, away, con, team_slots,
                 policy=None, tz=None, rink_slots=None):
    """Why ``slot`` can't host ``home`` vs ``away`` under the constraints, as
    ``(code, message)``, or ``(None, None)`` if it can. ``team_slots`` maps
    team_id -> [assigned start_time]; ``rink_slots`` maps rink_id ->
    [(start, end)] of ice already assigned in this draft; ``policy``/``tz`` are
    the slot's rink policy and the Program timezone for #277 turnover/curfew."""
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
    # #277 curfew: the reserved slot must end by the rink's curfew wall-clock in
    # the Program timezone, anchored to the slot's START local date (a game past
    # midnight still counts against that evening's curfew). Only with a zone.
    if policy is not None and policy.curfew_local and tz is not None:
        start_local = slot.start_time.astimezone(tz)
        end_local = slot.end_time.astimezone(tz)
        curfew_h, curfew_m = parse_hhmm(policy.curfew_local, "curfew_local")
        curfew_at = start_local.replace(hour=curfew_h, minute=curfew_m,
                                        second=0, microsecond=0)
        if end_local > curfew_at:
            return CURFEW_VIOLATION, "past the rink's curfew"
    # #277 turnover: keep the rink's turnover gap from games already assigned to
    # this rink in this draft (same reserved-window rule as the shared checker).
    if policy is not None and (policy.turnover_minutes or 0) > 0 \
            and rink_slots is not None:
        turnover = policy.turnover_minutes
        for a_start, a_end in rink_slots.get(slot.rink_id, ()):
            if intervals_overlap(slot.start_time, slot.end_time, a_start, a_end):
                continue
            gap = ((a_start - slot.end_time) if slot.end_time <= a_start
                   else (slot.start_time - a_end)).total_seconds() / 60
            if gap < turnover:
                return TURNOVER_CONFLICT, "insufficient turnover gap on the rink"
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


def _assign_ice(store, pairings, slots, constraints, program=None):
    """Greedy earliest-slot-first assignment shared by every entry point.

    ``pairings`` is ``[(home_team_id, away_team_id, division_id_or_None), ...]``
    — a league-wide draft tags each pairing with the Division its two teams
    are actually registered in (or ``None``), so the resulting rows stay
    per-pairing accurate even though one draft can span several Divisions.
    ``program`` (the draft's Season's Program) supplies the #277 turnover /
    curfew policy + timezone so a draft never proposes ice the commit's shared
    checker would then reject. Returns ``(draft_games, unscheduled)``.
    """
    con = _normalize_constraints(constraints)
    tz = _resolve_tz(program)
    # Resolve each slot's effective rink policy once (rink value, else Program
    # default, else the system default of turnover 0 / no curfew).
    policy_by_slot = {
        s.id: effective_policy(
            store.get_rink(s.rink_id) if s.rink_id else None, program)
        for s in slots}

    def team_name(tid):
        t = store.get_team(tid)
        return t.name if t else tid

    draft_games, unscheduled = [], []
    used = set()
    team_slots = {}  # team_id -> [assigned start_time]
    rink_slots = {}  # rink_id -> [(start, end)] assigned in this draft (turnover)
    for home, away, division_id in pairings:
        chosen, messages, codes = None, [], []
        for s in slots:
            if s.id in used:
                continue
            code, message = _slot_reason(
                s, home, away, con, team_slots,
                policy=policy_by_slot.get(s.id), tz=tz, rink_slots=rink_slots)
            if code is None:
                chosen = s
                break
            messages.append(message)
            codes.append(code)
        if chosen is not None:
            used.add(chosen.id)
            team_slots.setdefault(home, []).append(chosen.start_time)
            team_slots.setdefault(away, []).append(chosen.start_time)
            rink_slots.setdefault(chosen.rink_id, []).append(
                (chosen.start_time, chosen.end_time))
            rink = store.get_rink(chosen.rink_id) if chosen.rink_id else None
            draft_games.append({
                "home_team_id": home, "away_team_id": away,
                "home_team_name": team_name(home), "away_team_name": team_name(away),
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
                "home_team_name": team_name(home), "away_team_name": team_name(away),
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

    def team_name(tid):
        t = store.get_team(tid)
        return t.name if t else tid

    rollup = []
    for tid in sorted(team_ids):
        if total.get(tid, 0) > 0 and blocked_count.get(tid, 0) == total[tid]:
            rollup.append({
                "team_id": tid, "team_name": team_name(tid),
                "reason_codes": sorted(blocked_codes.get(tid, ())),
            })
    return rollup


def draft_schedule(store, division_id, slot_ids=None, constraints=None):
    """Generate a draft round-robin schedule for a division (#84/#85).

    Returns ``{division_id, team_count, draft_games, unscheduled,
    unschedulable_teams}``. Each pairing takes the earliest available slot
    that satisfies the optional constraints; a pairing with no valid slot is
    returned in ``unscheduled`` with the reason(s) that blocked it. Nothing is
    persisted.
    """
    # A division's teams are those validly registered in it this season (#180),
    # via the shared resolver — active rows whose Team exists and whose league
    # matches, so an orphaned or cross-league registration row can never enter a
    # draft (#199/#200 review). Same source of truth game creation, moves,
    # publishing, and standings use.
    teams = sorted(registered_team_ids_in_division(store, division_id))
    pairings = [(h, a, division_id) for h, a in round_robin_pairings(teams)]
    slots = _available_game_slots(store, slot_ids)
    program = _program_for_season(store, _season_id_for_division(store, division_id))
    draft_games, unscheduled = _assign_ice(
        store, pairings, slots, constraints, program=program)
    return {
        "division_id": division_id, "team_count": len(teams),
        "draft_games": draft_games, "unscheduled": unscheduled,
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
    pairings = []
    all_teams = set()
    for div_id, team_ids in groups.items():
        all_teams |= team_ids
        pairings.extend(
            (h, a, div_id) for h, a in round_robin_pairings(sorted(team_ids)))
    slots = _available_game_slots(store, slot_ids)
    program = _program_for_season(store, season_id)
    draft_games, unscheduled = _assign_ice(
        store, pairings, slots, constraints, program=program)
    return {
        "season_id": season_id, "league_id": league_id,
        "division_id": division_id, "team_count": len(all_teams),
        "draft_games": draft_games, "unscheduled": unscheduled,
        "unschedulable_teams": _unschedulable_teams(
            store, all_teams, pairings, unscheduled),
    }
