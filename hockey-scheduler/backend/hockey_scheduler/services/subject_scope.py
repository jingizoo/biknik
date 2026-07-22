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
