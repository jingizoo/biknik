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
# Single source of truth for "which team does this caller act for" — shared with
# the active-context selector so the two gates can never drift (#159 review).
# Re-exported so existing `from .scope import own_team_id` callers are unchanged.
# NOTE: this is the ACCOUNT-level, game-agnostic resolution (#159 context
# selection uses it too) — a Player's PERMANENT `team_id` pointer. It is
# deliberately NOT used below for a game-scoped decision (#205 blocker 1) —
# see `game_scoped_own_team_id`.
from ..services.subject_scope import own_team_id, player_team_id  # noqa: F401
# `RosterService.team_for_game` is THE #205 game-scoped eligibility resolver
# (`services/roster_service.py`) — the same one substitute enroll/offer/
# accept already resolve through. No import-cycle risk: roster_service.py
# depends only on ..domain/..store/.notifier/.season_guard, never on `web/`
# (confirmed by reading it), so calling it directly from here is the sound
# layering, not the caller-resolves-then-passes-team_id workaround.
from ..services.roster_service import RosterService

_SUB_ACTION = re.compile(
    r"^/api/games/[^/]+/substitutes/([^/]+)/(?:offer|accept|decline|add-to-roster)$")
_ASSIGN_RESPOND = re.compile(
    r"^/api/officials/assignments/([^/]+)/(?:accept|decline)$")
_GAME_ACTION = re.compile(r"^/api/games/[^/]+/(.+)$")
# EVERY coach-initiated HTTP action on a specific game — captures the game
# id so the COACH branch of `scope_violation` below can resolve a target
# player's team through the SAME game-scoped membership resolver
# `team_for_game` provides, instead of the permanent `Player.team_id`
# pointer (#205 blocker 1: a mid-season transfer left that pointer stale,
# wrongly denying a coach managing a legitimate Mover already resolved onto
# their team for this exact game — and symmetrically wrongly allowing a
# coach to manage a player whose membership has since moved off their team).
#
# Deliberately matches ANY `/api/games/{gid}/...` action, not a curated list
# of substitute-workflow verbs: a first pass here only listed
# substitutes/add-candidate and substitutes/{pid}/(offer|accept|decline|
# add-to-roster), which missed substitutes/enroll and substitutes/withdraw
# (same COACH branch, same player_id-in-body shape, RESPOND_AVAILABILITY
# permission — a live bypass an independent review round caught: a HOME
# coach could enroll/withdraw an AWAY player's real Mover membership on the
# stale HOME pointer) — and would have kept missing `roster/remove` and
# `roster/select` too (also player_id/player_ids-in-body, MANAGE_ROSTER,
# same gate, same fallback). `team_for_game` itself already degrades
# correctly for a game with NO LeagueSeason binding (falls back to the
# permanent pointer scoped to the game's two sides — see its own
# docstring), so widening this match costs nothing on that path; it only
# stops the fallback from firing on real, bound games where a whitelist
# entry was simply never added.
_GAME_ACTION_GID = re.compile(r"^/api/games/([^/]+)/.+$")

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
        # #205 blocker 1: for a substitute-workflow action on a specific game,
        # resolve the target player's team through the SAME game-scoped
        # membership resolver (`team_for_game`) the substitute workflow's own
        # business logic uses — never the permanent `Player.team_id` pointer,
        # which a mid-season transfer can leave stale in EITHER direction: it
        # would otherwise wrongly DENY a coach managing a legitimate Mover
        # already resolved onto their team for this game, and symmetrically
        # wrongly ALLOW a coach to manage a player whose membership has since
        # moved OFF their team even though the stale pointer still matches.
        gid_match = _GAME_ACTION_GID.match(path)
        game = store.get_game(gid_match.group(1)) if gid_match else None
        for pid in _player_ids(path, body):
            player = store.get_player(pid)
            if player is None:
                continue
            resolved_team = (RosterService(store).team_for_game(game, player)
                             if game is not None else player.team_id)
            if resolved_team != team:
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


def _player_team_for_game(scope, game, store):
    """Which team a Player-scoped caller acts for, in THIS ``game``
    specifically (#205 blocker 1) — resolved through the SAME game-scoped
    membership resolver the substitute workflow itself uses
    (``RosterService.team_for_game``), never the permanent ``Player.team_id``
    pointer alone, which a mid-season transfer can leave stale for this exact
    game in either direction (a real Mover wrongly denied, or a player whose
    membership has since moved off the team wrongly still granted).

    Preserves the #270 fail-closed posture ``player_team_id`` established — a
    deactivated player's login must not outlive their roster exit — with the
    SAME ``is_active`` check, so this is a strict refinement of that
    function's contract, not a loosening of it. Falls back to the permanent
    pointer only when ``game`` carries no LeagueSeason binding (exhibitions
    and unbound legacy games), exactly as ``team_for_game`` itself does —
    byte-for-byte pre-#205 behavior there.
    """
    scope = scope or {}
    player_id = scope.get("player_id")
    if not player_id:
        return None
    player = store.get_player(player_id)
    if player is None or not player.is_active:
        return None
    return RosterService(store).team_for_game(game, player)


def game_scoped_own_team_id(role, scope, game, store):
    """The team the caller acts for, resolved specifically against ``game``
    (#205 blocker 1) — the game-scoped analogue of ``own_team_id`` above.

    A Coach's team is unchanged: still the permanently-bound
    ``scope["team_id"]``. There is no ``CoachSeasonMembership`` (or any
    season-scoped Coach model) anywhere in this codebase — a Coach's team
    assignment genuinely IS permanent, so no game-scoped resolution applies
    there. A Player's team is resolved live against ``game`` via
    ``_player_team_for_game`` (``RosterService.team_for_game``), replacing
    the permanent ``Player.team_id`` pointer ``own_team_id``/
    ``player_team_id`` use.

    NOT a drop-in replacement for the generic, game-agnostic
    ``own_team_id`` — that function is correctly shared with the #159
    active-context selector (``services/context_scope.py``), a different
    surface with no single game to resolve against, and stays untouched.
    This helper is for exactly the two HTTP call sites that must resolve
    "own team" against ONE particular game's privacy/scope boundary:
    ``can_read_private_game_data`` and the ``availability-summary``
    sub-scope in ``web/server.py``.
    """
    scope = scope or {}
    if role == Role.COACH:
        return scope.get("team_id")
    if role == Role.PLAYER:
        return _player_team_for_game(scope, game, store)
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
        # #205 blocker 1: resolved against THIS game — see
        # `game_scoped_own_team_id`. Coach behavior is byte-for-byte
        # unchanged (still `scope["team_id"]`); Player behavior now resolves
        # through the game-scoped membership resolver instead of the
        # permanent pointer.
        team_id = game_scoped_own_team_id(role, scope, game, store)
        return team_id is not None and team_id in (
            game.home_team_id, game.away_team_id)
    if role == Role.OFFICIAL:
        official_id = scope.get("official_id")
        return official_id is not None and any(
            a.official_id == official_id
            for a in store.assignments_for_game(game_id))
    return False  # viewer / anything else
