"""Named immutable schedule scenarios and material-input snapshots (#378).

This module deliberately does not understand or reshape scheduler explanation
payloads.  It snapshots authoritative inputs; the complete generator response
is persisted opaquely by the API facade.
"""

from dataclasses import asdict, is_dataclass
from datetime import datetime
from enum import Enum
import hashlib
import json

from ..domain.errors import NotFoundError, ValidationError


SCENARIO_PLANNER_VERSION = "round-robin-v1"
SCENARIO_SNAPSHOT_VERSION = "schedule-material-v1"


def _plain(value):
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        return value.isoformat()
    if is_dataclass(value):
        return _plain(asdict(value))
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    if isinstance(value, (set, frozenset)):
        values = [_plain(v) for v in value]
        return sorted(
            values,
            key=lambda item: json.dumps(
                item, sort_keys=True, separators=(",", ":"),
                ensure_ascii=False))
    return value


def canonical_json(value) -> str:
    """Typed, delimiter-safe canonical JSON for deterministic identities."""
    return json.dumps(
        _plain(value), sort_keys=True, separators=(",", ":"),
        ensure_ascii=False)


def canonical_fingerprint(value) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def resolve_scenario_scope(store, division_id=None, season_id=None,
                           league_id=None) -> dict:
    """Resolve the permanent Program→League plus Season overlay identity."""
    if bool(season_id) != bool(league_id):
        raise ValidationError(
            "season_id and league_id must be provided together.",
            {"reason": "scenario_scope_incomplete"})

    division = None
    if season_id and league_id:
        season = store.get_season(season_id)
        league = store.get_league(league_id)
        league_season = store.league_season_for(league_id, season_id)
        if season is None or league is None or league_season is None:
            raise NotFoundError(
                "The selected League is not linked to the selected Season.",
                details={"reason": "league_season_missing"})
        if season.program_id != league.program_id:
            raise ValidationError(
                "The selected Season and League do not belong to one Program.",
                {"reason": "scenario_program_mismatch"})
        if division_id:
            division = store.get_division(division_id)
            if (division is None
                    or division.league_season_id != league_season.id):
                raise NotFoundError(
                    "Division not found in the selected LeagueSeason.",
                    details={"reason": "division_missing"})
    else:
        if not division_id:
            raise ValidationError(
                "A division_id, or a season_id and league_id, is required.",
                {"reason": "scenario_scope_required"})
        division = store.get_division(division_id)
        league_season = (store.get_league_season(division.league_season_id)
                         if division is not None else None)
        season = (store.get_season(league_season.season_id)
                  if league_season is not None else None)
        league = (store.get_league(league_season.league_id)
                  if league_season is not None else None)
        if any(value is None
               for value in (division, league_season, season, league)):
            raise NotFoundError(
                "Division not found.", details={"reason": "division_missing"})
        if season.program_id != league.program_id:
            raise ValidationError(
                "The Division's Season and League do not share one Program.",
                {"reason": "scenario_program_mismatch"})

    program = store.get_program(season.program_id)
    if program is None:
        raise NotFoundError(
            "Program not found.", details={"reason": "program_missing"})
    return {
        "program_id": program.id,
        "season_id": season.id,
        "league_id": league.id,
        "league_season_id": league_season.id,
        "division_id": division.id if division is not None else None,
    }


def _scope_entities(store, scope):
    division_id = scope.get("division_id")
    program = store.get_program(scope["program_id"])
    season = store.get_season(scope["season_id"])
    league = store.get_league(scope["league_id"])
    league_season = store.get_league_season(scope["league_season_id"])
    division = store.get_division(division_id) if division_id else None
    return {
        # Program timezone and Season dates/lifecycle are planner inputs and
        # their rows are part of the commit lock plan, so preserve them whole.
        "program": _plain(program),
        "season": _plain(season),
        # League display metadata and Division labels do not affect fixture
        # generation. Bind only the permanent topology here; this avoids a
        # harmless rename making a reviewed schedule stale while still catching
        # any material reparenting. LeagueSeason rows are topology-only models.
        "league": ({"id": league.id, "program_id": league.program_id}
                   if league is not None else None),
        "league_season": _plain(league_season),
        "division": ({"id": division.id,
                      "league_season_id": division.league_season_id}
                     if division is not None else None),
    }


def material_input_snapshot(store, scope: dict, request_input: dict,
                            planner_version=SCENARIO_PLANNER_VERSION) -> dict:
    """Read every currently authoritative material input for ``scope``.

    Lists are sorted by stable ids before hashing.  The scheduler's proposal is
    not interpreted here: registrations, accessible inventory, relevant Games
    (including lock/cancel/publish state), and applicable policy rows are read
    directly from the store.  Request constraints/blackouts are preserved as
    immutable planner input.
    """
    season_id = scope["season_id"]
    league_season_id = scope["league_season_id"]

    registrations = sorted(
        (r for r in store.all_season_team_registrations()
         if r.league_season_id == league_season_id),
        key=lambda r: r.id)
    registered_team_ids = {r.team_id for r in registrations}
    teams = sorted(
        (t for t in store.all_teams() if t.id in registered_team_ids),
        key=lambda t: t.id)

    venue_access = sorted(
        (a for a in store.all_season_venue_access()
         if a.season_id == season_id), key=lambda a: a.id)
    active_venue_ids = {a.venue_id for a in venue_access if a.active}

    requested_slot_ids = request_input.get("slot_ids")
    all_rinks = list(store.all_rinks())
    all_slots = list(store.all_ice_slots())
    if requested_slot_ids:
        requested = set(requested_slot_ids)
        candidate_slots = [s for s in all_slots if s.id in requested]
        candidate_rink_ids = {s.rink_id for s in candidate_slots}
        candidate_rinks = [r for r in all_rinks if r.id in candidate_rink_ids]
    else:
        candidate_rinks = [r for r in all_rinks
                           if r.venue_id in active_venue_ids]
        candidate_rink_ids = {r.id for r in candidate_rinks}
        candidate_slots = [s for s in all_slots
                           if s.rink_id in candidate_rink_ids]

    material_venue_ids = ({r.venue_id for r in candidate_rinks}
                          | {a.venue_id for a in venue_access})
    venues = sorted(
        (v for v in store.all_venues() if v.id in material_venue_ids),
        key=lambda v: v.id)
    candidate_rinks.sort(key=lambda r: r.id)
    candidate_slots.sort(key=lambda s: s.id)

    slot_rink = {s.id: s.rink_id for s in all_slots}
    games = []
    for game in store.all_games():
        if (game.league_season_id == league_season_id
                or game.home_team_id in registered_team_ids
                or game.away_team_id in registered_team_ids
                or slot_rink.get(game.ice_slot_id) in candidate_rink_ids):
            games.append(game)
    games.sort(key=lambda g: g.id)

    # Directional turnover rules read a neighbor Game's own Season/Program
    # policy as well as this scenario's scopes, so include those exact rows.
    policy_scope_keys = {
        ("program", scope["program_id"]),
        ("season", season_id),
    }
    policy_scope_keys.update(("rink", rid) for rid in candidate_rink_ids)
    for game in games:
        if slot_rink.get(game.ice_slot_id) not in candidate_rink_ids:
            continue
        if game.season_id:
            policy_scope_keys.add(("season", game.season_id))
            neighbor_season = store.get_season(game.season_id)
            if neighbor_season is not None:
                policy_scope_keys.add(("program", neighbor_season.program_id))
    policies = sorted(
        (p for p in store.all_scheduling_policies()
         if (getattr(p.scope_type, "value", p.scope_type), p.scope_id)
         in policy_scope_keys), key=lambda p: p.id)

    sections = {
        "scope": _scope_entities(store, scope),
        "planner_input": {
            "scope_mode": request_input.get("scope_mode"),
            "slot_ids": _plain(requested_slot_ids),
            # Includes the current constraint/blackout contract opaquely.
            "constraints": _plain(request_input.get("constraints")),
        },
        "registrations": {
            "rows": _plain(registrations),
            "teams": _plain(teams),
        },
        "venue_access": {
            "rows": _plain(venue_access),
            "venues": _plain(venues),
            "rinks": _plain(candidate_rinks),
        },
        "ice": _plain(candidate_slots),
        "games": _plain(games),
        "policies": _plain(policies),
    }
    snapshot = {
        "snapshot_version": SCENARIO_SNAPSHOT_VERSION,
        "planner_version": planner_version,
        **sections,
        "section_fingerprints": {
            key: canonical_fingerprint(value)
            for key, value in sections.items()
        },
    }
    return snapshot


def changed_snapshot_sections(generated: dict, current: dict) -> list:
    """Stable section names for actionable stale-commit diagnostics."""
    generated_fps = generated.get("section_fingerprints") or {}
    current_fps = current.get("section_fingerprints") or {}
    keys = sorted(set(generated_fps) | set(current_fps))
    changed = [key for key in keys
               if generated_fps.get(key) != current_fps.get(key)]
    if generated.get("planner_version") != current.get("planner_version"):
        changed.insert(0, "planner_version")
    if generated.get("snapshot_version") != current.get("snapshot_version"):
        changed.insert(0, "snapshot_version")
    return changed
