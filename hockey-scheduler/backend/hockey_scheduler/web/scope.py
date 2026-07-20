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

    Coach and Player scope both fail **closed** (#266/#282): a Coach whose
    session carries no ``team_id``, or a Player whose session carries no
    ``player_id`` — an account created/left without a valid subject — has NO
    authority and is refused, rather than being silently treated as unscoped and
    allowed to mutate any team's roster / respond for any player. The ONE
    exception is the demo-only
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
        # Player scope fails **closed** (#266/#282), exactly like Coach above: a
        # Player whose session carries no ``player_id`` has NO self-service
        # identity and is refused, rather than being silently treated as
        # unscoped and allowed to RESPOND_AVAILABILITY / self-service for ANY
        # player id. The only permitted unscoped player is the demo-only
        # X-Demo-Role fallback (identity-less, never read in production).
        if not own:
            if allow_unscoped_dev_fallback:
                return None  # demo-only X-Demo-Role fallback; unreachable in prod
            return ("This player account has no assigned player, so it can't "
                    "respond for anyone — ask a league admin to assign a player.")
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


def _player_team_id(scope, store):
    """The team a Player account belongs to, for the private-read gate (#160).

    A Player's canonical scope key is ``player_id``; the team is resolved **live**
    from it every time and is **never** taken from a stored ``team_id``. A stored
    team_id could be stale after a transfer or a removal and would then retain
    access to a former team's private data, so it is not trusted here at all.
    No player_id, an unknown/deleted player, an INACTIVE player (#270 — a
    departed/IR player's login must not outlive the roster exit), or a teamless
    player each resolve to ``None`` — the gate then fails closed.
    """
    player_id = scope.get("player_id")
    if not player_id:
        return None
    player = store.get_player(player_id)
    if player is None or not player.is_active:
        return None
    return player.team_id


def own_team_id(role, scope, store):
    """The team the caller acts for, for team-level scope checks (#160).

    A Coach's team is its canonical stored ``team_id``; a Player's is resolved
    LIVE from ``player_id`` (never a stored ``team_id``, which could be stale).
    Returns ``None`` for any other role or when there is no bound/current team.
    """
    scope = scope or {}
    if role == Role.COACH:
        return scope.get("team_id")
    if role == Role.PLAYER:
        return _player_team_id(scope, store)
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
    if role == Role.COACH:
        team_id = scope.get("team_id")
        return team_id is not None and team_id in (
            game.home_team_id, game.away_team_id)
    if role == Role.PLAYER:
        # A Player account's canonical scope key is ``player_id`` (#135/#160),
        # but this gate is by team. Resolve the player's team from ``player_id``
        # live (authoritative, never stale on a transfer), falling back to an
        # explicit ``scope.team_id`` only when player_id is absent/unresolvable —
        # so a Player account created with player_id ONLY still reads its own
        # team's private data, and a teamless player fails closed.
        team_id = _player_team_id(scope, store)
        return team_id is not None and team_id in (
            game.home_team_id, game.away_team_id)
    if role == Role.OFFICIAL:
        official_id = scope.get("official_id")
        return official_id is not None and any(
            a.official_id == official_id
            for a in store.assignments_for_game(game_id))
    return False  # viewer / anything else
