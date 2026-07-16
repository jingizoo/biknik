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


def scope_violation(role, scope, path, body, store, *,
                    allow_unscoped_dev_fallback=False):
    """Return a human message if the action is outside the user's scope, else None.

    ``scope`` is the session binding (``team_id`` for a coach, ``player_id`` for
    a player).

    Coach scope fails **closed** (#266): a Coach whose session carries no
    ``team_id`` — an account created/left without a valid team — has NO roster
    authority and is refused, rather than being silently treated as unscoped and
    allowed to mutate any team's roster. The ONE exception is the demo-only
    ``X-Demo-Role`` header fallback, which produces an identity-less
    (``user_id is None``) coach session for scripts/curl; the caller passes
    ``allow_unscoped_dev_fallback=True`` only for that path, and only outside
    production (the header is never even read in production), so the fallback is
    explicit and impossible to activate in a production deployment.
    """
    scope = scope or {}
    if role == Role.COACH:
        team = scope.get("team_id")
        if not team:
            if allow_unscoped_dev_fallback:
                return None  # demo-only X-Demo-Role fallback; unreachable in prod
            return ("This coach account has no assigned team, so it can't "
                    "manage any roster — ask a league admin to assign a team.")
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
    elif role == Role.GUARDIAN:
        # A guardian's authority is per-link and is enforced ONLY on the
        # dedicated /api/me/guardian/* routes (which verify the guardian↔junior
        # link before every action and never reach this gate). The general
        # player/coach routes carry no such link check, so a guardian must
        # never target a player through them — otherwise the verified-link
        # requirement could be side-stepped by posting a player_id directly.
        if _player_ids(path, body):
            return ("Guardians respond through their linked-player screen, "
                    "not this route.")
    elif role == Role.OFFICIAL:
        own = scope.get("official_id")
        m = _ASSIGN_RESPOND.match(path)
        if m:
            # This slice is about self-service identity: responding requires a
            # bound official, and only to their own assignment (#54 review).
            if not own:
                return "Official assignment response requires a signed-in official."
            assignment = store.get_official_assignment(m.group(1))
            if assignment is not None and assignment.official_id != own:
                return "Officials can only respond to their own assignments."
    return None


def can_read_private_game_data(role, scope, game_id, store) -> bool:
    """May this signed-in user read a game's private player data? (#73)

    Authentication alone isn't the privacy boundary — a signed-in viewer or an
    unrelated coach/player/official must not see another game's rosters,
    availability, substitutes, or staff assignments. Operators see everything;
    a coach/player only their own team's games; an official only the games they
    are assigned to; a plain viewer, none.
    """
    scope = scope or {}
    if role in (Role.LEAGUE_ADMIN, Role.ARENA_MANAGER):
        return True
    game = store.get_game(game_id)
    if game is None:
        return True  # let the facade return its normal not_found payload
    if role in (Role.COACH, Role.PLAYER):
        team_id = scope.get("team_id")
        return team_id in (game.home_team_id, game.away_team_id)
    if role == Role.OFFICIAL:
        official_id = scope.get("official_id")
        return official_id is not None and any(
            a.official_id == official_id
            for a in store.assignments_for_game(game_id))
    return False  # viewer / anything else
