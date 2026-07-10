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
    """Best-effort league resolution for current and legacy game rows.

    New games carry ``season_id``. Older draft/test rows may only carry a
    division or team relationship, so those are safe deterministic fallbacks.
    """
    if game is None:
        return None
    if getattr(game, "season_id", None):
        season = store.get_season(game.season_id)
        if season and season.league_id:
            return season.league_id
    if getattr(game, "division_id", None):
        try:
            return league_id_for_division(store, game.division_id)
        except DomainError:
            pass

    resolved = set()
    for team_id in (getattr(game, "home_team_id", None),
                    getattr(game, "away_team_id", None)):
        team = store.get_team(team_id) if team_id else None
        if not team or not team.division_id:
            continue
        try:
            resolved.add(league_id_for_division(store, team.division_id))
        except DomainError:
            continue
    if len(resolved) == 1:
        return next(iter(resolved))
    if len(resolved) > 1:
        raise ValidationError(
            "The game's teams resolve to different leagues.",
            details={"reason": "game_league_ambiguous", "game_id": game.id},
        )
    return None


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
