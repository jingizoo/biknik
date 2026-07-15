"""League-scoped scheduler facade (#173 PR C).

Wraps the deterministic v1 scheduler while restricting its candidate ice to the
league that owns the requested division. Explicit slot selections are validated
instead of silently dropping another league's inventory.
"""

from .league_scope import (
    league_id_for_division,
    require_slot_belongs_to_season,
    slot_belongs_to_season,
)
from .scheduler import (
    draft_schedule as _base_draft_schedule,
    round_robin_pairings,
)


def draft_schedule(store, division_id, slot_ids=None, constraints=None):
    division = store.get_division(division_id) if division_id else None
    # Preserve the scheduler/API's established not-found behavior via the shared
    # resolver rather than returning an empty cross-league proposal.
    league_id = league_id_for_division(store, division_id)
    season_id = division.season_id

    explicit = list(slot_ids) if slot_ids else None
    if explicit is not None:
        for slot_id in explicit:
            require_slot_belongs_to_season(store, slot_id, season_id)
        scoped_slot_ids = explicit
    else:
        scoped_slot_ids = [
            slot.id for slot in store.all_ice_slots()
            if slot_belongs_to_season(store, slot.id, season_id)
        ]

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


__all__ = ["draft_schedule", "round_robin_pairings"]
