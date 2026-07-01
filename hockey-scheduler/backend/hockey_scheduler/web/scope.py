"""Per-resource authorization scoping (#51).

RBAC (#24) answers "may this role do this kind of action?". Scoping answers
"may this *specific* user act on this *specific* resource?" — a coach may manage
only their own team's players, and a player may respond only for themselves.
Runs after the coarse permission check, with the request body/path and the
store, so it can resolve a player's team. League admins and arena managers are
not resource-scoped here (their permissions already bound them).
"""

import re

from ..domain import Role

_SUB_ACTION = re.compile(
    r"^/api/games/[^/]+/substitutes/([^/]+)/(?:offer|accept|decline|add-to-roster)$")
_ASSIGN_RESPOND = re.compile(
    r"^/api/officials/assignments/([^/]+)/(?:accept|decline)$")
_GAME_ACTION = re.compile(r"^/api/games/[^/]+/(.+)$")

# Game-wide actions a scoped coach may NOT perform — they flip whole-game state
# (game.locked / game.cancelled) affecting the other team, and carry no target
# for scope_violation to constrain. Until per-side locks exist, block them for a
# bound coach; league admins are unscoped and keep these controls.
_GAME_WIDE_COACH_ACTIONS = {"roster/lock", "roster/unlock", "cancel"}


def _player_ids(path: str, body: dict):
    """Every player id an action targets, from the body and the path."""
    ids = []
    if body.get("player_id"):
        ids.append(body["player_id"])
    ids.extend(pid for pid in (body.get("player_ids") or []) if pid)
    m = _SUB_ACTION.match(path)
    if m:
        ids.append(m.group(1))
    return ids


def scope_violation(role, scope, path, body, store):
    """Return a human message if the action is outside the user's scope, else None.

    ``scope`` is the session binding (``team_id`` for a coach, ``player_id`` for
    a player). An unbound session (e.g. the dev header fallback) is not scoped.
    """
    scope = scope or {}
    if role == Role.COACH:
        team = scope.get("team_id")
        if not team:
            return None  # unbound coach (dev fallback) — not resource-scoped
        m = _GAME_ACTION.match(path)
        if m and m.group(1) in _GAME_WIDE_COACH_ACTIONS:
            return ("A coach can't lock, unlock, or cancel the whole game "
                    "(that affects the other team) — ask a league admin.")
        for pid in _player_ids(path, body):
            player = store.get_player(pid)
            if player is not None and player.team_id != team:
                return "A coach can only manage their own team's players."
        team_id = body.get("team_id")
        if team_id and team_id != team:
            return "A coach can only manage their own team's roster."
    elif role == Role.PLAYER:
        own = scope.get("player_id")
        if not own:
            return None
        for pid in _player_ids(path, body):
            if pid != own:
                return "Players can only respond for themselves."
    elif role == Role.OFFICIAL:
        own = scope.get("official_id")
        if not own:
            return None
        m = _ASSIGN_RESPOND.match(path)
        if m:
            assignment = store.get_official_assignment(m.group(1))
            if assignment is not None and assignment.official_id != own:
                return "Officials can only respond to their own assignments."
    return None
