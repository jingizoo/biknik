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


def draft_schedule(store, division_id, slot_ids=None, constraints=None):
    division = store.get_division(division_id) if division_id else None
    # Preserve the scheduler/API's established not-found behavior via the shared
    # resolver rather than returning an empty cross-league proposal.
    league_id = league_id_for_division(store, division_id)
    season_id = division.season_id

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
        if (division is None or division.season_id != season_id
                or division.league_id != league_id):
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
