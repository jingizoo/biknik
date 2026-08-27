"""Canonical caller-subject resolution (#51/#160/#211).

The ONE place that answers "which team does this caller act for". Shared by the
per-request web scope guards (`web/scope.py`) and the active-context selector
(`services/context_scope.py`, #159) so the two authorization gates can never
resolve a caller's identity differently and drift apart.

A Coach's team is its canonical stored ``team_id``; a Player's is resolved LIVE
from ``player_id`` every time (never a stored ``team_id``, which could be stale
after a transfer), failing closed on an unknown, inactive, or teamless player.
"""

from ..domain import Role


def player_team_id(scope, store):
    """The team a Player account belongs to, resolved LIVE from ``player_id``.

    A stored ``team_id`` could be stale after a transfer or removal and would
    then retain access to a former team, so it is never trusted. No player_id,
    an unknown/deleted player, an INACTIVE player (#270 — a departed/IR player's
    login must not outlive the roster exit), or a teamless player each resolve to
    ``None`` — callers then fail closed.
    """
    scope = scope or {}
    player_id = scope.get("player_id")
    if not player_id:
        return None
    player = store.get_player(player_id)
    if player is None or not player.is_active:
        return None
    return player.team_id


def own_team_id(role, scope, store):
    """The team the caller acts for: a Coach's stored ``team_id``; a Player's
    resolved LIVE via :func:`player_team_id`. ``None`` for any other role or when
    there is no bound/current team."""
    scope = scope or {}
    if role == Role.COACH:
        return scope.get("team_id")
    if role == Role.PLAYER:
        return player_team_id(scope, store)
    return None


def assignment_grants_official_scope(assignment, official_id):
    """Does this ``OfficialAssignment`` grant ``official_id`` any scope? (#205)

    THE ONE PRODUCT PREDICATE for "an assignment an Official's authority
    actually flows from", and the reason it lives HERE, beside
    :func:`own_team_id`, is the reason stated at the top of this module: an
    Official's assignment-derived grant is the same species of fact as a
    Coach's team binding, and it was drifting across surfaces for exactly the
    want of one shared definition.

    WHAT DRIFTED, and why a third copy was the bug rather than the fix. An
    Official's scope is projected in three places — the private-game admission
    (``game_side_scope.resolve_private_game_read``) and the two active-context
    projections (``context_scope._official_program_seasons`` and
    ``_official_league_ids``). The private-game half was corrected first, by
    inlining ``a.status.is_active`` at its own call site; the two context
    projections went on iterating EVERY assignment for the Official. So the
    identical declined grant was revoked on ``/board``, ``/lineups`` and
    ``/roster`` while remaining live in the context switcher: an Official who
    had ENDED the relationship still saw the target Program, Season and League
    as selectable options. Reproduced through public ``get_context_options`` on
    Memory, SQLite and PostgreSQL after a real
    ``respond_assignment(..., accept=False)``.

    Authorization that disagrees across product surfaces is the failure mode,
    so the predicate is now defined once and CALLED from all three, rather than
    spelled out three times and kept in step by hand.

    THE TWO CONDITIONS, and why both are load-bearing:

    * THE EXACT OFFICIAL. Redundant where the caller already queried
      ``assignments_for_official`` — and essential in
      ``resolve_private_game_read``, which iterates ``assignments_for_game``
      and must not admit an Official on somebody else's assignment.
    * ``status.is_active`` — the product's OWN statement of which assignments
      hold anything ("Proposed or accepted assignments hold the official's
      time", ``OfficialAssignmentStatus.is_active``). It is deliberately that
      property and not a status literal spelled out again here, so a future
      status lands in one place. PROPOSED and ACCEPTED grant; DECLINED does
      not.

    A missing assignment or a falsy ``official_id`` grants nothing — this is a
    visibility gate with no caller to report a structured reason to, so an
    unanswerable input fails CLOSED.
    """
    if assignment is None or not official_id:
        return False
    return (assignment.official_id == official_id
            and assignment.status.is_active)
