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

from dataclasses import dataclass
from typing import Optional, Tuple

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


@dataclass(frozen=True)
class PrivateGameRead:
    """THE ONE resolution a private-game read is decided by — admission AND
    projection — carried, not recomputed (#427 round 2, blocker 1).

    WHY THIS TYPE EXISTS. ``web/server.py``'s private-game family used to
    take that decision TWICE. First ``can_read_private_game_data`` fetched
    the game and resolved the caller's game-scoped team to decide whether to
    admit them at all; then, independently, the dispatch fetched the SAME
    game again and resolved the SAME team again to decide which side to
    answer for. Nothing held the two together, and the gap between them was
    a disclosure window: a membership transferred, ended or invalidated
    after the first resolution and before the second left ``own_team`` empty,
    which collapsed to ``own_side() -> None`` and then to ``get_board``'s
    HOME default — so a caller who had just LOST their authority received
    the HOME side's private pool, status block, notifications and audit
    stream with ``restricted: false``. Reproduced over a real authenticated
    session, parked between the two reads, on Memory and SQLite (200,
    ``team_id`` naming HOME, six HOME identities, three HOME notifications,
    four HOME audit rows) and on two-connection PostgreSQL.

    Loss of authority must produce a REFUSAL, never a fallback. The fix is
    structural rather than a third check: there is now ONE resolution, taken
    once, and everything downstream reads it off this record instead of
    asking the store again. This is the READ-PATH TWIN of the pattern the
    coach-authorization work established on the WRITE path — a preflight may
    remain for fast denial, but it cannot be the authoritative gate, and the
    authoritative answer is resolved once and carried.

    ``game`` is ``None`` only when the game does not exist; the caller is
    still ADMITTED so the facade can return its normal ``not_found`` payload
    rather than a 403 that would confirm the id's absence differently from
    every other route.

    ``own_team`` is the TRUSTED side and is ``None`` for every caller who has
    no side of their own — an unscoped operator, an assigned official, an
    in-process caller. It is never ``None`` for an ADMITTED team-scoped
    caller: that combination is exactly what admission refuses.
    """

    role: object
    game: object
    own_team: Optional[str]
    admitted: bool

    @property
    def side_ids(self) -> Tuple[Optional[str], Optional[str]]:
        """``(home, away)`` of the game THIS decision was taken against —
        the same fetch, so a side id can never come from a different read of
        the row than the one that admitted the caller."""
        if self.game is None:
            return (None, None)
        return (self.game.home_team_id, self.game.away_team_id)


def resolve_private_game_read(role, scope, game_id, store) -> PrivateGameRead:
    """Resolve, ONCE, everything a private-game read is decided by.

    This is the whole of the #73 admission rule and the whole of the #205
    trusted-side resolution, taken together against ONE fetch of the game:

    * an UNSCOPED OPERATOR is admitted with no side of their own;
    * a COACH/PLAYER is admitted only when ``game_scoped_own_team_id``
      resolves a side that is actually one of this game's two — a missing,
      ended, deactivated or nonparticipant side is a REFUSAL, never a
      default;
    * an assigned OFFICIAL is admitted with no side of their own;
    * everyone else (a viewer, an unrecognised role) is refused.

    Nothing here reads a request. The inputs are a session-resolved ``role``,
    the session's own ``scope``, an already-selected ``game_id`` and the
    store — so a query string, a body field or a header can never reach this
    resolution, which is the property the whole private-game family rests on.
    """
    # The role tests below are spelled EXACTLY as the two functions this one
    # merges already spelled them — `can_read_private_game_data`'s operator
    # short-circuit and `game_scoped_own_team_id`'s COACH/PLAYER branches —
    # rather than introducing role tuples here. A second list of "which roles
    # are team-scoped" is the drift shape this whole boundary exists to
    # remove; `services/lineup_visibility.py` holds the one that classifies
    # PROJECTIONS, and nothing here needs a copy of it.
    scope = scope or {}
    if role in (Role.LEAGUE_ADMIN, Role.ARENA_MANAGER):
        # Admitted before the game matters, exactly as
        # `can_read_private_game_data` short-circuited: an operator's
        # admission does not depend on the game existing. The game is still
        # fetched so `side_ids` is usable.
        return PrivateGameRead(role=role, game=store.get_game(game_id),
                               own_team=None, admitted=True)
    game = store.get_game(game_id)
    if game is None:
        # Let the facade answer its normal not_found. Byte-for-byte the
        # pre-existing `can_read_private_game_data` behaviour.
        return PrivateGameRead(role=role, game=None, own_team=None,
                               admitted=True)
    if role in (Role.COACH, Role.PLAYER):
        own_team = game_scoped_own_team_id(role, scope, game, store)
        admitted = own_team is not None and own_team in (
            game.home_team_id, game.away_team_id)
        # `own_team` is deliberately dropped on refusal: a refused read must
        # not carry a side any downstream code could still answer for.
        return PrivateGameRead(role=role, game=game,
                               own_team=own_team if admitted else None,
                               admitted=admitted)
    if role == Role.OFFICIAL:
        official_id = scope.get("official_id")
        admitted = official_id is not None and any(
            a.official_id == official_id
            for a in store.assignments_for_game(game_id))
        return PrivateGameRead(role=role, game=game, own_team=None,
                               admitted=admitted)
    return PrivateGameRead(role=role, game=game, own_team=None,
                           admitted=False)
