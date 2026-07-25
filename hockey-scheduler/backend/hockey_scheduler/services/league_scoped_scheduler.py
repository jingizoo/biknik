"""League-scoped scheduler facade (#173 PR C, extended #233 Slice G).

Wraps the deterministic v1 scheduler while restricting its candidate ice to
the Season that owns the requested Division/League. Explicit slot selections
are validated instead of silently dropping another season's inventory.
"""

from ..domain.errors import NotFoundError
from .league_scope import (
    league_id_for_division,
    require_league_belongs_to_season,
    require_slot_belongs_to_season,
    slot_belongs_to_season,
)
from .scheduler import (
    draft_schedule as _base_draft_schedule,
    draft_schedule_for_league as _base_draft_schedule_for_league,
    round_robin_pairings,
)


def _season_scoped_slot_ids(store, season_id, slot_ids):
    """Explicit slot ids are validated; an omitted selection scans every slot
    whose Venue holds active ``SeasonVenueAccess`` for this Season. Shared by
    both entry points below so ice scoping can never drift between them."""
    explicit = list(slot_ids) if slot_ids else None
    if explicit is not None:
        for slot_id in explicit:
            require_slot_belongs_to_season(store, slot_id, season_id)
        return explicit
    return [slot.id for slot in store.all_ice_slots()
            if slot_belongs_to_season(store, slot.id, season_id)]


def season_candidate_rink_ids(store, season_id, slot_ids):
    """#328 review round 13 -- every Rink ``draft_season_schedule`` can
    possibly draw ice from for this (season, slot_ids) call, computed the
    SAME way regardless of whether any of those Rinks currently have an
    existing IceSlot: an explicit selection resolves to exactly those
    slots' Rinks; an omitted selection resolves to every Rink whose Venue
    holds active SeasonVenueAccess for the Season.

    Deliberately NOT derived from :func:`_season_scoped_slot_ids` (which
    would miss a Rink with zero EXISTING slots): a lock-scope computation
    for a not-yet-populated but already season-eligible Rink is exactly
    the case a commit must still cover, since a concurrent ice-availability
    BUILDER commit (or CSV import) can give that Rink its FIRST slot at
    any moment, not only add to a Rink that already has one."""
    if slot_ids:
        rinks = set()
        for slot_id in slot_ids:
            slot = store.get_ice_slot(slot_id)
            if slot is not None:
                rinks.add(slot.rink_id)
        return rinks
    rinks = set()
    for rink in store.all_rinks():
        access = store.season_venue_access_for_pair(season_id, rink.venue_id)
        if access is not None and access.active:
            rinks.add(rink.id)
    return rinks


def draft_schedule(store, division_id, slot_ids=None, constraints=None):
    division = store.get_division(division_id) if division_id else None
    # Preserve the scheduler/API's established not-found behavior via the shared
    # resolver rather than returning an empty cross-league proposal.
    league_id = league_id_for_division(store, division_id)
    # #283: Division.season_id dropped; resolve its Season via LeagueSeason.
    # league_id_for_division above already rejected a dangling Division/chain.
    division_ls = (store.get_league_season(division.league_season_id)
                   if division and division.league_season_id else None)
    season_id = division_ls.season_id if division_ls else None

    scoped_slot_ids = _season_scoped_slot_ids(store, season_id, slot_ids)

    # The base scheduler interprets an empty list as "all slots". Pass an
    # impossible sentinel so an empty season inventory remains empty while the
    # base function still validates constraints and builds normal unscheduled
    # rows.
    base_ids = scoped_slot_ids or ["\0no-season-scoped-slot"]
    result = _base_draft_schedule(
        store, division_id, slot_ids=base_ids, constraints=constraints)

    if not scoped_slot_ids:
        for row in result["unscheduled"]:
            row["reason"] = "No available game ice is assigned to this season."

    result["season_id"] = season_id
    result["league_id"] = league_id
    for row in result["draft_games"]:
        row["season_id"] = season_id
        row["league_id"] = league_id
    return result


def draft_schedule_for_league(store, season_id, league_id, division_id=None,
                              slot_ids=None, constraints=None):
    """League-wide counterpart of ``draft_schedule`` (#233 Slice G): the
    ``league_id`` here is the canonical grouping League
    (``store.get_league``), never this module's own Program-scoped
    ``league_id`` vocabulary — see ``require_league_belongs_to_season``'s
    docstring."""
    require_league_belongs_to_season(store, league_id, season_id)
    if division_id is not None:
        division = store.get_division(division_id)
        # #283: a Division belongs to a LeagueSeason; it's in this League+Season
        # exactly when its league_season_id matches this (league, season) row.
        league_season = store.league_season_for(league_id, season_id)
        if (division is None or league_season is None
                or division.league_season_id != league_season.id):
            raise NotFoundError(
                f"Division {division_id} not found.",
                details={"reason": "division_missing", "division_id": division_id})

    scoped_slot_ids = _season_scoped_slot_ids(store, season_id, slot_ids)
    base_ids = scoped_slot_ids or ["\0no-season-scoped-slot"]
    result = _base_draft_schedule_for_league(
        store, season_id, league_id, division_id=division_id,
        slot_ids=base_ids, constraints=constraints)

    if not scoped_slot_ids:
        for row in result["unscheduled"]:
            row["reason"] = "No available game ice is assigned to this season."

    return result


__all__ = ["draft_schedule", "draft_schedule_for_league", "round_robin_pairings"]
