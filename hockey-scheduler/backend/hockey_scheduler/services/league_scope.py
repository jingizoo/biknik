"""League/venue scheduling-scope invariants (#173 PR C).

All scheduling paths use these helpers to resolve IceSlot -> Rink -> Venue and
Game/Division -> Season -> League. Keeping the traversal in one module prevents
manual create, move/reschedule, draft generation, and publish from drifting.
"""

from typing import Optional

from ..domain.errors import DomainError, NotFoundError, ValidationError


def venue_for_slot(store, ice_slot_id):
    """Resolve an ice slot's venue, raising a structured error for broken links."""
    if not ice_slot_id:
        raise ValidationError(
            "A game ice slot is required.",
            details={"reason": "game_slot_missing"},
        )
    slot = store.get_ice_slot(ice_slot_id)
    if slot is None:
        raise NotFoundError(
            f"Ice slot {ice_slot_id} not found.",
            details={"reason": "slot_missing", "ice_slot_id": ice_slot_id},
        )
    rink = store.get_rink(slot.rink_id) if slot.rink_id else None
    if rink is None:
        raise ValidationError(
            "The selected ice slot is not linked to a rink.",
            details={"reason": "slot_rink_missing", "ice_slot_id": ice_slot_id},
        )
    venue = store.get_venue(rink.venue_id) if rink.venue_id else None
    if venue is None:
        raise ValidationError(
            "The selected rink is not linked to a venue.",
            details={
                "reason": "rink_venue_missing",
                "ice_slot_id": ice_slot_id,
                "rink_id": rink.id,
            },
        )
    return venue


def league_id_for_division(store, division_id: str) -> str:
    """Resolve Division -> Season -> League, rejecting dangling structure."""
    division = store.get_division(division_id) if division_id else None
    if division is None:
        raise NotFoundError(
            "Division not found.",
            details={"reason": "division_missing", "division_id": division_id},
        )
    season = store.get_season(division.season_id) if division.season_id else None
    if season is None:
        raise ValidationError(
            "Division is not linked to a valid season.",
            details={
                "reason": "division_season_missing",
                "division_id": division.id,
                "season_id": division.season_id,
            },
        )
    if not season.league_id or store.get_league(season.league_id) is None:
        raise ValidationError(
            "Season is not linked to a valid league.",
            details={
                "reason": "season_league_missing",
                "season_id": season.id,
                "league_id": season.league_id,
            },
        )
    return season.league_id


def league_id_for_game(store, game) -> Optional[str]:
    """Resolve and reconcile every available league signal on a game.

    New games carry both ``season_id`` and ``division_id``. Legacy rows may
    carry only a division or team relationship. Explicit but dangling links are
    rejected; conflicting season/division/team relationships are never hidden
    by simply trusting the first one encountered.
    """
    if game is None:
        return None

    resolved = set()
    season_id = getattr(game, "season_id", None)
    if season_id:
        season = store.get_season(season_id)
        if season is None:
            raise ValidationError(
                "Game is linked to a missing season.",
                details={
                    "reason": "game_season_missing",
                    "game_id": getattr(game, "id", None),
                    "season_id": season_id,
                },
            )
        if not season.league_id or store.get_league(season.league_id) is None:
            raise ValidationError(
                "Game season is not linked to a valid league.",
                details={
                    "reason": "season_league_missing",
                    "game_id": getattr(game, "id", None),
                    "season_id": season.id,
                    "league_id": season.league_id,
                },
            )
        resolved.add(season.league_id)

    division_id = getattr(game, "division_id", None)
    if division_id:
        resolved.add(league_id_for_division(store, division_id))

    # Team-derived context is a consistency check on top of the explicit
    # season/division. A Team's league is its permanent ``league_id`` (#180),
    # never re-derived through its legacy division. A team with no concrete
    # league_id can't corroborate scope and is skipped here; where a Team's
    # league is actually required (registration validity, scheduling), the
    # missing league is rejected rather than guessed.
    for team_id in (getattr(game, "home_team_id", None),
                    getattr(game, "away_team_id", None)):
        team = store.get_team(team_id) if team_id else None
        if team and team.league_id:
            resolved.add(team.league_id)

    if len(resolved) > 1:
        raise ValidationError(
            "The game's season, division, or teams resolve to different leagues.",
            details={
                "reason": "game_league_ambiguous",
                "game_id": getattr(game, "id", None),
                "league_ids": sorted(resolved),
            },
        )
    return next(iter(resolved)) if resolved else None


# -- season-registration resolver (#180) -------------------------------------
# Participation is resolved through SeasonTeamRegistration, but a registration
# row is only trusted once its Team is resolved and the Team's permanent league
# matches the season's league. As #199 established, the registration store can
# hold orphaned or cross-league rows; scheduling, standings, and draft
# generation must never silently trust one. These two helpers are the single
# shared resolver every scheduling path uses so the rule cannot drift.

def team_registration_valid(store, season, team_id, division_id=None,
                            require_division=True):
    """Return the active, league-consistent registration for ``team_id`` in
    ``season``, or ``None``. A row is trusted only if it is active, its Team
    exists, and the Team's permanent ``league_id`` equals the season's league.
    When ``require_division`` and ``division_id`` is given, the registration's
    division must match too. A registration is valid only when the season has a
    concrete league and the Team has the *same* concrete league — a missing
    league on either side is never treated as a match (#200 review)."""
    if season is None or not season.league_id:
        return None
    # The shared league must actually EXIST (#180 review): a season and team
    # that share a dangling/non-existent league id are not league-consistent —
    # no operational consumer may trust such a row.
    if store.get_league(season.league_id) is None:
        return None
    reg = store.registration_for_team_in_season(season.id, team_id)
    if reg is None or not reg.active:
        return None
    team = store.get_team(team_id)
    if team is None or not team.league_id or team.league_id != season.league_id:
        return None
    if require_division and division_id is not None and reg.division_id != division_id:
        return None
    return reg


def registered_team_ids_in_division(store, division_id):
    """Team ids validly registered in ``division_id`` this season: the row is
    active and in this division, its Team exists, and the Team's permanent
    league matches the division's season league. Orphaned/cross-league rows are
    excluded rather than trusted (#199). Shared by standings and draft
    generation so both read exactly the same roster."""
    division = store.get_division(division_id)
    if division is None:
        return set()
    season = store.get_season(division.season_id)
    if season is None or not season.league_id:
        return set()  # dangling season, or a season with no league — trust nothing
    league_id = season.league_id
    ids = set()
    for reg in store.registrations_for_season(division.season_id):
        if not reg.active or reg.division_id != division_id:
            continue
        team = store.get_team(reg.team_id)
        if team is None or not team.league_id or team.league_id != league_id:
            continue  # orphaned, null-league, or cross-league row — never trusted
        ids.add(reg.team_id)
    return ids


def require_game_league_id(store, game) -> str:
    """Return a game's league or reject a row with no usable league context."""
    league_id = league_id_for_game(store, game)
    if not league_id:
        raise ValidationError(
            "Game is not linked to a league through its season or division.",
            details={
                "reason": "game_league_missing",
                "game_id": getattr(game, "id", None),
            },
        )
    return league_id


def require_slot_belongs_to_league(store, ice_slot_id: str, league_id: str):
    """Require an IceSlot's Venue to be assigned to ``league_id``.

    Returns the resolved Venue so callers that need venue context do not repeat
    the traversal. Unassigned and cross-league inventory are both invalid.
    """
    league = store.get_league(league_id) if league_id else None
    if league is None:
        raise ValidationError(
            "A valid league is required before assigning game ice.",
            details={"reason": "league_missing", "league_id": league_id},
        )
    venue = venue_for_slot(store, ice_slot_id)
    if not venue.league_id:
        raise ValidationError(
            "The selected ice slot's venue is not assigned to a league.",
            details={
                "reason": "venue_league_unassigned",
                "ice_slot_id": ice_slot_id,
                "venue_id": venue.id,
                "league_id": league_id,
            },
        )
    if venue.league_id != league_id:
        raise ValidationError(
            "The selected ice slot belongs to a different league.",
            details={
                "reason": "cross_league_slot",
                "ice_slot_id": ice_slot_id,
                "venue_id": venue.id,
                "expected_league_id": league_id,
                "actual_league_id": venue.league_id,
            },
        )
    if (league.organization_id or None) != (venue.organization_id or None):
        raise ValidationError(
            "The venue owner does not match the league owner.",
            details={
                "reason": "venue_owner_mismatch",
                "ice_slot_id": ice_slot_id,
                "venue_id": venue.id,
                "league_id": league_id,
                "league_organization_id": league.organization_id,
                "venue_organization_id": venue.organization_id,
            },
        )
    return venue


def slot_belongs_to_league(store, ice_slot_id: str, league_id: str) -> bool:
    """Boolean filtering form used when scanning all inventory for a draft."""
    try:
        require_slot_belongs_to_league(store, ice_slot_id, league_id)
        return True
    except DomainError:
        return False
