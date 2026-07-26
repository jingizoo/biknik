"""League/venue scheduling-scope invariants (#173 PR C).

All scheduling paths use these helpers to resolve IceSlot -> Rink -> Venue and
Game/Division -> Season -> League. Keeping the traversal in one module prevents
manual create, move/reschedule, draft generation, and publish from drifting.
"""

from typing import Optional

from ..domain.errors import DomainError, NotFoundError, ValidationError
from .scope_bridge import (
    get_scope_program,
    season_scope_id,
    team_scope_id,
)


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
    # #283: Division no longer carries season_id; it hangs off a LeagueSeason,
    # so resolve Division -> LeagueSeason -> Season.
    league_season = (store.get_league_season(division.league_season_id)
                     if division.league_season_id else None)
    season_id = league_season.season_id if league_season else None
    season = store.get_season(season_id) if season_id else None
    if season is None:
        raise ValidationError(
            "Division is not linked to a valid season.",
            details={
                "reason": "division_season_missing",
                "division_id": division.id,
                "season_id": season_id,
            },
        )
    scope_id = season_scope_id(season)
    if not scope_id or get_scope_program(store, scope_id) is None:
        raise ValidationError(
            "Season is not linked to a valid league.",
            details={
                "reason": "season_league_missing",
                "season_id": season.id,
                "league_id": scope_id,
            },
        )
    return scope_id


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
        season_scope = season_scope_id(season)
        if not season_scope or get_scope_program(store, season_scope) is None:
            raise ValidationError(
                "Game season is not linked to a valid league.",
                details={
                    "reason": "season_league_missing",
                    "game_id": getattr(game, "id", None),
                    "season_id": season.id,
                    "league_id": season_scope,
                },
            )
        resolved.add(season_scope)

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
        if team and team_scope_id(team):
            resolved.add(team_scope_id(team))

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
    season_scope = season_scope_id(season) if season is not None else None
    if season is None or not season_scope:
        return None
    # The shared league must actually EXIST (#180 review): a season and team
    # that share a dangling/non-existent league id are not league-consistent —
    # no operational consumer may trust such a row.
    if get_scope_program(store, season_scope) is None:
        return None
    team = store.get_team(team_id)
    if team is None or not team_scope_id(team) or team_scope_id(team) != season_scope:
        return None
    # #331 review round 18: resolved by the Team's exact (team, permanent-
    # League LeagueSeason) identity -- never the first Season-wide row for
    # this team regardless of active status/LeagueSeason. A Team can hold
    # more than one registration row in the same Season (migration 035:
    # unique only on (team_id, league_season_id)) -- an inactive prior-
    # League row transfer_team_to_league deliberately preserved as history,
    # or a stale cross-League row a write path predating Rule 7 left behind
    # (#331 review round 17/18's own commit_teams_players_import fix names
    # both exact failure shapes). Picking the first one regardless of League
    # could silently hide a genuinely valid active row behind an inactive or
    # cross-League sibling that happens to sort first -- rejecting real
    # participation instead of validating it. #283 rules 7-9: a registration
    # must sit in the Team's OWN permanent League; at most one row can ever
    # match (team_id, league_season_id) for that exact LeagueSeason, so this
    # lookup is unambiguous by construction, never a guess.
    if not team.league_id:
        return None
    ls = store.league_season_for(team.league_id, season.id)
    if ls is None:
        return None
    reg = store.registration_for_team_in_league_season(ls.id, team_id)
    if reg is None or not reg.active:
        return None
    if require_division and division_id is not None and reg.division_id != division_id:
        return None
    return reg


def registered_team_ids_in_division(store, division_id, enforce_team_league=True):
    """Team ids validly registered in ``division_id`` this season: the row is
    active and in this division, its Team exists, and the Team's permanent
    league matches the division's season league. Orphaned/cross-league rows are
    excluded rather than trusted (#199). Shared by standings and draft
    generation so both read exactly the same roster.

    ``enforce_team_league`` (default ``True``) is the LIVE-scheduling rule: a
    Team must currently belong to this Division's League (rules 7-9), so draft
    generation and current-Season standings exclude a same-Program cross-League
    drift. Pass ``False`` for an ENDED Season's historical standings (#283 rule
    10): a Team validly transferred to another League afterward keeps its
    completed-Season registration/Games here and must still be counted — the
    caller decides a Season is ended and asks history not to re-check current
    ownership. Orphaned/null-Program/cross-Program rows are excluded either way.
    """
    division = store.get_division(division_id)
    if division is None:
        return set()
    # #283: Division.season_id dropped; resolve Season via its LeagueSeason.
    league_season = (store.get_league_season(division.league_season_id)
                     if division.league_season_id else None)
    season = (store.get_season(league_season.season_id)
              if league_season else None)
    if season is None or not season_scope_id(season):
        return set()  # dangling season, or a season with no league — trust nothing
    program_scope = season_scope_id(season)
    # #283 rules 7-9: the Division's competition League — a Team is trusted here
    # only when its permanent League is exactly this one (not merely the same
    # Program), excluding a same-Program cross-League drift.
    division_league_id = league_season.league_id
    ids = set()
    for reg in store.registrations_for_season(season.id):
        if not reg.active or reg.division_id != division_id:
            continue
        team = store.get_team(reg.team_id)
        if team is None or not team_scope_id(team) or team_scope_id(team) != program_scope:
            continue  # orphaned, null-Program, or cross-Program row — never trusted
        if enforce_team_league and (
                not team.league_id or team.league_id != division_league_id):
            continue  # live cross-League drift within the Program — never trusted
        ids.add(reg.team_id)
    return ids


def require_league_belongs_to_season(store, league_id: str, season_id: str):
    """Resolve and validate a canonical League belongs to ``season_id`` (#233
    Slice G). This ``league_id`` is the competition grouping League between
    Season and Division (``store.get_league`` / ``Division.league_id`` /
    ``SeasonTeamRegistration.league_id``) — distinct from this module's own
    ``league_id`` vocabulary elsewhere, which names the Program-level
    scheduling *scope* (see ``scope_bridge.py``'s docstring). Mirrors
    ``SetupService.create_game``'s own League validation so a league-wide
    draft rejects a dangling/cross-season League the same way manual game
    creation already does."""
    league = store.get_league(league_id) if league_id else None
    if league is None:
        raise NotFoundError(
            f"League {league_id} not found.",
            details={"reason": "league_missing", "league_id": league_id},
        )
    # #283: League no longer carries season_id; its participation in a Season is
    # a LeagueSeason row, so "belongs to this Season" is that row's existence.
    if store.league_season_for(league_id, season_id) is None:
        raise ValidationError(
            "League belongs to a different season.",
            details={
                "reason": "league_season_mismatch",
                "league_id": league_id, "season_id": season_id,
            },
        )
    return league


def registered_teams_by_division_in_league(store, season_id, league_id,
                                           division_id=None):
    """Active, league-consistent registrations for a canonical League this
    Season (#233 Slice G), grouped by Division — ``None`` keys the teams
    registered directly at the League level with no Division. Mirrors
    ``registered_team_ids_in_division``'s trust rules (active row, Team
    exists, Team's permanent Program matches the Season's) but scopes by the
    League directly via ``SeasonTeamRegistration.league_id`` rather than
    requiring one Division. When ``division_id`` is given, only that
    Division's bucket is returned (possibly empty) — this lets a league-wide
    draft be narrowed to one Division without switching to the division-only
    entry point.

    Grouping by Division here (rather than flattening every registered team
    into one pool) is deliberate: a League's Divisions are genuine competitive
    splits (Gold/Silver/Diamond), so a league-wide draft must never pair a
    Gold team against a Silver team just because they share a League.
    """
    season = store.get_season(season_id) if season_id else None
    if season is None or not season_scope_id(season):
        return {}
    program_scope = season_scope_id(season)
    league = store.get_league(league_id) if league_id else None
    # #283: a League's participation in this Season is a LeagueSeason row; the
    # Season's registrations/Divisions for this League hang off it.
    league_season = store.league_season_for(league_id, season_id) if league_id else None
    if league is None or league_season is None:
        return {}
    groups = {}
    for reg in store.registrations_for_league_season(league_season.id):
        if not reg.active:
            continue
        if division_id is not None and reg.division_id != division_id:
            continue
        if reg.division_id is not None:
            division = store.get_division(reg.division_id)
            # #283: a Division belongs to a LeagueSeason; it's in this League+Season
            # exactly when its league_season_id matches this LeagueSeason.
            if division is None or division.league_season_id != league_season.id:
                continue  # missing/cross-Season/cross-League Division — never trusted
        team = store.get_team(reg.team_id)
        if team is None or not team_scope_id(team) or team_scope_id(team) != program_scope:
            continue  # orphaned, null-Program, or cross-Program row — never trusted
        # #283 rules 7-9: the Team's permanent League must be exactly this League
        # (not merely the same Program) — a same-Program cross-League drift (a
        # Bronze Team in Platinum's LeagueSeason) is never trusted.
        if not team.league_id or team.league_id != league_id:
            continue
        groups.setdefault(reg.division_id, set()).add(reg.team_id)
    return groups


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


def require_slot_belongs_to_season(store, ice_slot_id: str, season_id: str):
    """Require an IceSlot's Venue to hold active SeasonVenueAccess for ``season_id``.

    Returns the resolved Venue so callers that need venue context do not repeat
    the traversal. Replaces the legacy one-Venue-one-Program bridge (#233 Slice
    E): a Venue may legitimately host many Seasons/Programs, and a facility
    owner and Program operator may be different organizations, so no
    owner-match check is imposed here.
    """
    season = store.get_season(season_id) if season_id else None
    if season is None:
        raise ValidationError(
            "A valid season is required before assigning game ice.",
            details={"reason": "season_missing", "season_id": season_id},
        )
    venue = venue_for_slot(store, ice_slot_id)
    access = store.season_venue_access_for_pair(season_id, venue.id)
    if access is None or not access.active:
        raise ValidationError(
            "The selected ice slot's venue is not allowed for this season.",
            details={
                "reason": "venue_access_missing",
                "ice_slot_id": ice_slot_id,
                "venue_id": venue.id,
                "season_id": season_id,
                # Actionable remediation (#271) built only from ids already in
                # this error — grant the season→venue access that unblocks it.
                # No private data is added.
                "remediation": (
                    "Grant this season access to the venue, then retry: add a "
                    "season-venue-access grant for this venue on the season's "
                    "venue-access list."),
                "remediation_route":
                    f"/api/v2/setup/seasons/{season_id}/venue-access",
            },
        )
    return venue


def slot_belongs_to_season(store, ice_slot_id: str, season_id: str) -> bool:
    """Boolean filtering form used when scanning all inventory for a draft."""
    try:
        require_slot_belongs_to_season(store, ice_slot_id, season_id)
        return True
    except DomainError:
        return False


def require_slots_belong_to_locked_season(store, ice_slot_ids, season_id):
    """Re-run ``require_slot_belongs_to_season`` for every slot in
    ``ice_slot_ids`` against ``season_id`` (#314 review) — the canonical
    per-slot/Season eligibility check, shared by ``move_game`` and both
    ``commit_draft_schedule`` implementations.

    ``require_slot_belongs_to_season`` is a pure read with no lock of its own,
    so calling it before the caller holds the relevant Rink(s) and Season lock
    leaves a window: a concurrent ``SeasonVenueAccess`` revoke (which locks the
    Season the same way ``_require_active_season``/``_guard_active_seasons``
    does) or a Rink→Venue reassignment (whose write needs the very Rink row
    lock ``_lock_rinks`` took) can commit in that gap, after which the caller's
    write lands a Game onto ice that no longer serves the Season.

    Callers MUST call this only AFTER already holding, for every slot checked
    here, the Rink(s) (``_lock_rinks``) and Season (``_require_active_season``
    / ``_guard_active_seasons``) locks — at that point a conflicting write has
    either already committed (and this sees it) or is blocked waiting on our
    lock (and cannot land until after we do), so the answer is race-free.
    Called BEFORE any Game, slot-status, or audit write. A single-slot caller
    (``move_game``) passes a length-1 iterable; a draft-commit batch passes
    every proposed slot.
    """
    for ice_slot_id in ice_slot_ids:
        require_slot_belongs_to_season(store, ice_slot_id, season_id)
