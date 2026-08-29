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

WHICH Season is the authority for a GAME-owned write — :func:`guard_game_season`
(PR #427 blocker, owner comment 5379031499). Answering "the Game's own
``season_id``" was the defect: that column is a NULLABLE, unconstrained
DENORMALIZATION of the Game's LeagueSeason, and both guard families skipped the
whole check when it was falsy and locked the WRONG row when it had drifted to a
sibling Season. The competition boundary is the Game's ``league_season_id``, so
that is what is resolved, and the Season the LeagueSeason names is what is
locked, checked and reported. See :func:`guard_game_season` for the precedence.
"""

from ..domain.enums import SeasonStatus
from ..domain.errors import NotFoundError, ValidationError

# The reason codes :func:`guard_game_season` reports. Both are PRE-EXISTING
# strings, deliberately reused rather than newly minted: ``season_archived`` is
# ``require_active_season``'s own refusal, and the other two are the codes
# ``SetupService._revalidate_game_participation`` has raised for these exact
# two broken shapes since #331 review round 22. One shape, one code, whichever
# guard happens to see it first.
SEASON_ARCHIVED = "season_archived"
GAME_LEAGUE_SEASON_MISMATCH = "game_league_season_mismatch"
MISSING_LEAGUE_SEASON = "regular_game_missing_league_season"


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
            {"reason": SEASON_ARCHIVED, "season_id": season_id})
    return season


def game_is_league_season_bound(game) -> bool:
    """The ONE definition of "this Game belongs to a competition" (PR #427).

    ``league_season_id is not None`` — an IDENTITY test, never truthiness.
    A LeagueSeason id is an opaque string, so ``""`` is not "no competition",
    it is a corrupted binding; answering the bound-ness question with ``bool``
    would route exactly that row down the unbound branch and hand it back the
    legacy permanent-pointer authority the cutover took away. Written once and
    called, so the resolver, the two write guards and every bound/unbound
    branch site cannot drift apart on what "bound" means.
    """
    return getattr(game, "league_season_id", None) is not None


def require_game_league_season(store, game):
    """The BOUND Game's LeagueSeason row, or refuse.

    One copy of the dangling-binding refusal, shared by
    :func:`game_season_authority_id` and :func:`guard_game_season` so the
    locator and the guard can never disagree about whether a binding resolves.
    Callers must already know the Game is bound.
    """
    ls = store.get_league_season(game.league_season_id)
    if ls is None:
        raise ValidationError(
            "This game's league-season no longer exists; it cannot be "
            "changed until it is repaired.",
            {"reason": MISSING_LEAGUE_SEASON, "game_id": game.id,
             "league_season_id": game.league_season_id})
    return ls


def game_season_authority_id(store, game):
    """The id of the Season that authorizes a mutation on ``game`` — resolved
    but NOT locked. A pre-lock LOCATOR, for a BATCH that must take several
    Season locks in one canonical (sorted) order to stay deadlock-free and so
    cannot let :func:`guard_game_season` lock them one at a time in arrival
    order. Every caller still runs the full :func:`guard_game_season` per Game
    afterwards; the re-lock is idempotent, and the archive/mismatch decisions
    are made there, under the lock, never off this read.

    Raises the same ``regular_game_missing_league_season`` a dangling binding
    gets from the guard itself: a bound Game whose LeagueSeason does not
    resolve has no authorizing Season to plan a lock for, and inventing one
    from ``game.season_id`` here would smuggle back exactly the fallback the
    guard refuses. ``None`` only for an unbound row naming no Season.
    """
    if game is None:
        return None
    if game_is_league_season_bound(game):
        ls = require_game_league_season(store, game)
        return ls.season_id
    return game.season_id or None


def guard_game_season(store, game):
    """Row-LOCK and CHECK the Season that authorizes a mutation on ``game``.

    Returns the locked Season (``None`` only for an unbound row that names no
    Season at all). MUST run inside the caller's ``transaction()`` — the lock
    is what makes the archive check linearizable, and it is held to commit.

    THE DEFECT THIS REPLACES (owner comment 5379031499). Both guard families
    used to read ``if game.season_id: require_active_season(store,
    game.season_id)``. ``games.season_id`` is a nullable, FK-less, CHECK-less
    denormalization of the Game's LeagueSeason, so that expression was wrong in
    two directions at once on a bound Game: a NULL skipped the archive guard
    entirely, and a value that had drifted to a SIBLING Season locked and
    judged that sibling — the refusal even NAMED the sibling while the resolved
    context's Season was the real one. Either way an archived competition could
    still be mutated, and no two writers on the same competition shared a Season
    row to serialize on.

    THE AUTHORITY IS THE LEAGUESEASON'S SEASON. For a bound Game the
    LeagueSeason is the competition boundary, so it is resolved here, inside
    the transaction, and the Season IT names is the row that gets locked,
    checked and reported. ``game.season_id`` is then only a denormalization to
    be VERIFIED against that authority, never the authority itself.

    THE PRECEDENCE IS FIXED, and is the owner's 2026-08-23 ruling:

    1. a bound Game whose LeagueSeason does not resolve is broken, not legacy
       — ``regular_game_missing_league_season``, the same code
       ``_revalidate_game_participation`` already raises for a dangling
       binding. There is NO fallback to ``game.season_id`` here: falling back
       would re-create the very split authority this function exists to close,
       and would let a Game escape its competition by having its LeagueSeason
       deleted;
    2. the canonical Season is locked and its ARCHIVE state judged FIRST, so an
       archived competition answers ``season_archived`` naming the CANONICAL
       Season id — never the drifted one the Game happens to carry;
    3. only once the canonical Season is ACTIVE is ``game.season_id`` compared,
       UNCONDITIONALLY. NULL and sibling-drift are the same failure — the
       denormalization disagrees with its authority — and both answer
       ``game_league_season_mismatch``.

    Inverting 2 and 3 would report a repair-the-row error for a Game whose real
    problem is that its competition is closed, and would make the mismatch code
    unreachable for the archived case; that is why the order is stated here
    rather than left to each caller.

    UNBOUND (an EXHIBITION, which carries ``league_season_id=None`` and a real
    ``season_id``, or a pre-#283 legacy row) keeps its existing authority: its
    own ``season_id``, guarded exactly as before. The falsy test survives ONLY
    on this branch, where it means "a legacy row that names no Season", and it
    is reached only when the Game was never bound — never as a fallback from a
    bound Game whose binding failed to resolve.
    """
    if game is None:
        return None
    if game_is_league_season_bound(game):
        ls = require_game_league_season(store, game)
        # (2) THE AUTHORITY: locked and archive-checked before anything else
        # is judged, and it is the CANONICAL id that gets reported.
        season = require_active_season(store, ls.season_id)
        # (3) …and only then is the denormalization verified against it.
        # Unconditional equality: `None` is a disagreement, not an exemption.
        if game.season_id != ls.season_id:
            raise ValidationError(
                "This game's season does not match its league-season; it "
                "cannot be changed until it is repaired.",
                {"reason": GAME_LEAGUE_SEASON_MISMATCH, "game_id": game.id,
                 "season_id": game.season_id, "league_season_id": ls.id,
                 "league_season_season_id": ls.season_id})
        return season
    if game.season_id:
        return require_active_season(store, game.season_id)
    return None
