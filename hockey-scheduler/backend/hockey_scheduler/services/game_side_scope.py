"""THE SERVER'S TRUSTED RESOLUTION of "which side of THIS game is this
caller's" (#205 blocker 1, #427 blocker).

WHY THIS IS A ``services/`` MODULE AND NOT A ``web/`` ONE. It began in
``web/scope.py``, next to the per-request authorization guards, because its
only two consumers were HTTP call sites (``can_read_private_game_data`` and
the ``availability-summary`` sub-scope). ``GET /api/demo/overview`` is the
third consumer and it cannot live there: the Dashboard read is a CROSS-GAME
list, so the side is a PER-ROW decision taken inside the facade's own
schedule loop, and ``api/service.py`` deliberately imports nothing from
``web/`` (the facade is the layer a different transport would be wired on
top of — see ``CLAUDE.md`` "Layering").

The alternative was a second copy of "which team does this caller act for"
in the facade. Four rounds of this blocker were spent deleting exactly that
shape, so the function MOVED rather than being duplicated:
``web/scope.py`` imports both names straight back out of here, so every
existing ``from .scope import game_scoped_own_team_id`` caller is unchanged
and there is still exactly ONE definition.

NOTHING HERE READS A REQUEST. The inputs are a session-resolved ``role``,
the session's own ``scope`` binding, the ``game`` the server already
selected, and the store. A query string, a body field or a header can
never reach this resolution, which is the whole property the private-game
family rests on.
"""

from ..domain import Role
from .roster_service import RosterService


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
    (#205 blocker 1) — the game-scoped analogue of
    ``subject_scope.own_team_id``.

    A Coach's team is unchanged: still the permanently-bound
    ``scope["team_id"]``. There is no ``CoachSeasonMembership`` (or any
    season-scoped Coach model) anywhere in this codebase — a Coach's team
    assignment genuinely IS permanent, so no game-scoped resolution applies
    there. A Player's team is resolved live against ``game`` via
    ``_player_team_for_game`` (``RosterService.team_for_game``), replacing
    the permanent ``Player.team_id`` pointer ``own_team_id``/
    ``player_team_id`` use.

    ``None`` for every other role, which is the fail-closed answer the three
    consumers all need: an unscoped operator and an assigned official have no
    side OF THEIR OWN, and a guardian, a viewer or an unrecognised role has
    no side at all.

    NOT a drop-in replacement for the generic, game-agnostic
    ``own_team_id`` — that function is correctly shared with the #159
    active-context selector (``services/context_scope.py``), a different
    surface with no single game to resolve against, and stays untouched.
    This helper is for exactly the call sites that must resolve "own team"
    against ONE particular game's privacy/scope boundary:
    ``web/scope.can_read_private_game_data``, the private-game dispatch
    family's single hoisted ``own_team`` in ``web/server.py``, and — per
    schedule row — ``ApiService.get_demo_overview``.
    """
    scope = scope or {}
    if role == Role.COACH:
        return scope.get("team_id")
    if role == Role.PLAYER:
        return _player_team_for_game(scope, game, store)
    return None
