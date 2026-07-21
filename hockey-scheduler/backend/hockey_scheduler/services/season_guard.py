"""Shared active-Season guard (#159).

An archived Season is read-only: no write may create or modify anything the
Season owns (registrations, venue access, Leagues, Divisions, Games and their
results/rosters/substitutes/reschedules, or season-scoped imports) until an
authorized, reasoned reopen. Every such write routes through
:func:`require_active_season` so the rule is enforced in exactly one place and
stays consistent across SetupService, RosterService, the API facade, and the
import path (none of which share a base class).

The Season row is LOCKED (``get_season_for_update``), so this MUST run inside
the caller's ``transaction()``: the lock is held to commit, which makes the
check linearizable with ``archive_season`` — a concurrent writer either commits
before the archive (and becomes frozen history) or blocks on the row until the
archive commits, then observes ARCHIVED and fails with zero mutation. A plain
read could observe ``active`` and race past a committing archive.
"""

from ..domain.enums import SeasonStatus
from ..domain.errors import NotFoundError, ValidationError


def require_active_season(store, season_id: str):
    """Return the (row-locked) Season, or raise if it is missing/archived.

    Raises ``NotFoundError`` when the Season does not exist and
    ``ValidationError(reason="season_archived")`` when it is archived.
    """
    season = store.get_season_for_update(season_id)
    if season is None:
        raise NotFoundError(f"Season {season_id} not found.")
    if season.status == SeasonStatus.ARCHIVED:
        raise ValidationError(
            f"Season '{season.name}' is archived and read-only. Reopen it "
            "before making changes.",
            {"reason": "season_archived", "season_id": season_id})
    return season
