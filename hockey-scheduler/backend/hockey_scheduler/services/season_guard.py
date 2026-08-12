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


def season_is_historical(season, now) -> bool:
    """The ONE definition of "this Season is history" (#159 + #283 rule 10).

    There are TWO INDEPENDENT routes into history and both count:

    * the explicit **ARCHIVED** lifecycle state (#159). ``archive_season``
      deliberately does *not* invent an ``end_date``, so an archived Season is
      routinely undated (or even future-dated); and
    * a real ``end_date`` that has **definitely** passed (#283 rule 10).

    A missing/undated, non-archived Season is current/future — the safe default
    — until an operator resolves it. ``season`` may be ``None`` (an unresolvable
    Season is not history).

    Every historicity decision goes through this one function, and the reason is
    concrete: the transfer WRITE path freezes a historical Season's registration
    instead of moving it, so the standings READERS must stop re-checking the
    Team's *current* permanent League for exactly the same Seasons. When the
    expression was written out twice the two halves drifted — the readers tested
    only the date — and a later, entirely legitimate Team transfer retroactively
    deleted the Team from, and zeroed its opponent's record in, an ARCHIVED
    Season's standings. Do not write the expression a third time; call this.

    ``now`` is passed in rather than read here so a caller that already
    snapshotted the clock (the transfer path holds one ``now`` across every
    Season it locked; the import preflight holds one for the whole batch) makes
    a decision that cannot straddle a clock tick.
    """
    if season is None:
        return False
    if season.status == SeasonStatus.ARCHIVED:
        return True
    end = season.end_date
    return end is not None and now is not None and end < now


def season_is_read_only(season) -> bool:
    """The ONE definition of "this Season refuses writes" — the ARCHIVED
    lifecycle state, and nothing else (#159).

    Deliberately NARROWER than :func:`season_is_historical`, and the two are not
    interchangeable. Historicity is a READING rule and takes a passed-in ``now``
    because a real elapsed ``end_date`` also counts; read-only-ness is the WRITE
    rule ``require_active_season`` enforces below, it is clock-independent, and
    a dated-but-not-archived Season is still perfectly writable. Answering the
    write question with the reading predicate would freeze every Season whose
    end_date has passed.

    Extracted so the write refusal and every payload that ADVERTISES it are one
    expression rather than two that agree today. ``get_setup_hierarchy_v2``
    emits ``read_only`` per Season from this function and the client skips the
    grant-candidate read on it, so a client guard that disagreed with the
    server's refusal would issue exactly the request the server answers 404 —
    which is what happened while the client guessed from a cached
    ``/api/context/options``. ``season`` may be ``None`` (nothing to refuse).
    """
    return season is not None and season.status == SeasonStatus.ARCHIVED


def require_active_season(store, season_id: str):
    """Return the (row-locked) Season, or raise if it is missing/archived.

    Raises ``NotFoundError`` when the Season does not exist and
    ``ValidationError(reason="season_archived")`` when it is archived.
    """
    season = store.get_season_for_update(season_id)
    if season is None:
        raise NotFoundError(f"Season {season_id} not found.")
    if season_is_read_only(season):
        raise ValidationError(
            f"Season '{season.name}' is archived and read-only. Reopen it "
            "before making changes.",
            {"reason": "season_archived", "season_id": season_id})
    return season
