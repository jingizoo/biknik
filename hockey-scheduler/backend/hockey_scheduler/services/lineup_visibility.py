"""WHICH SIDE of a game's private lineup a caller may read (#427 blocker,
owner ruling comment 5394947899).

THE DEFECT THIS EXISTS TO CLOSE. ``GET /api/games/{id}/lineups`` and
``GET /api/games/{id}/board`` were gated by exactly one check —
``web.scope.can_read_private_game_data`` — which proves the caller belongs to
*a* team in this game and stops there. It has no team-level narrowing (the
route registry records both routes ``scope_axis="none"``), so:

* an AWAY Coach was admitted to ``/board``, which then HARD-CODED
  ``game.home_team_id`` and answered with the HOME side's private pool and the
  HOME side's roster status — reproduced tri-store over a real authenticated
  session, ``status.team_id`` naming HOME to an AWAY Coach;
* both Coaches were admitted to ``/lineups``, which returns BOTH sides'
  private candidate lists, per-player availability and substitute workflow
  state.

"UI convenience" is what the ``get_board`` docstring called this, and it
cannot override the private-data boundary. The server now resolves the
caller's game-scoped team and passes that TRUSTED side into the read; nothing
here ever consults a client-supplied side.

THE THREE PROJECTIONS, and what each one is FOR:

``FULL``
    Every private field of a side: candidates, availability, substitute
    rows, status. For UNSCOPED OPERATORS (League Admin, Arena Manager) on
    both sides, and for a Coach/Player on THEIR OWN side only.

``SUBMITTED_LINEUP``
    The two-side projection an ASSIGNED OFFICIAL needs for the Game Sheet:
    the players each side actually SUBMITTED, and the slot counts. NOT
    either side's unselected candidates, per-player availability, or
    substitute state — an official referees the game, they do not manage
    anyone's roster.

``RESTRICTED``
    The opponent side of a scoped Coach/Player. Public game and team
    metadata is preserved; private ``status`` and ``players`` are omitted.

    *** RESTRICTED IS NOT AN EMPTY ROSTER. *** ``players: []`` already means
    something specific and operationally different on both screens — the
    Game Sheet renders it as "No lineup submitted." and the roster view as
    "No players on the roster yet" — so reusing it for redaction would tell
    a Coach their opponent has failed to submit a lineup. Redaction is
    therefore carried by an explicit ``restricted: true`` flag with
    ``players: null`` (JSON ``null``, never ``[]``), which no existing
    consumer can mistake for a count of zero.

FAIL CLOSED. An unrecognised role, a missing role, or a Coach/Player whose
resolved team is not one of this game's two sides gets RESTRICTED on both
sides. Pure functions over ``(role, viewer_team_id, home, away)`` — no store,
no request — so the whole matrix is unit-testable without a socket, and so
the rule cannot be restated differently by the two endpoints that enforce it.
"""

from ..domain import Role

FULL = "full"
SUBMITTED_LINEUP = "submitted_lineup"
RESTRICTED = "restricted"

#: A team-scoped caller's OWN side, and only it. Used by the flat-list sibling
#: routes, which have no per-side envelope to hang a ``restricted`` marker on
#: and therefore narrow the ROWS instead of labelling a side.
OWN_SIDE = "own_side"

#: Roles whose authority is league-wide rather than team-scoped. They are the
#: only callers who keep the full two-side private read, and they are exactly
#: the two roles ``can_read_private_game_data`` short-circuits to ``True``.
_UNSCOPED_OPERATORS = (Role.LEAGUE_ADMIN, Role.ARENA_MANAGER)

#: Roles that act FOR one team and must therefore be narrowed to it.
_TEAM_SCOPED = (Role.COACH, Role.PLAYER)


def side_projections(role, viewer_team_id, home_team_id, away_team_id) -> dict:
    """``{"home": <projection>, "away": <projection>}`` for one caller.

    ``viewer_team_id`` is the side the SERVER resolved for this caller
    against THIS game (``web.scope.game_scoped_own_team_id``) — a Coach's
    permanently-bound ``scope["team_id"]``, a Player's live game-scoped
    membership. It is never read from the query string or the body.

    ``role`` of ``None`` means an in-process caller that is not acting on
    behalf of any signed-in user — the facade's own default, kept ``FULL`` so
    non-HTTP callers are unchanged. Every HTTP-originated read passes a real
    session-resolved :class:`Role`: ``server.py``'s ``_resolve_role()`` never
    returns a live request with ``role=None`` (a missing or invalid session is
    an error resolved to a status BEFORE any facade call), so this default is
    unreachable from the network.
    """
    if role is None or role in _UNSCOPED_OPERATORS:
        return {"home": FULL, "away": FULL}
    if role == Role.OFFICIAL:
        # Assignment to THIS game is proven upstream by
        # `can_read_private_game_data`'s OFFICIAL branch; an unassigned
        # official never reaches the read at all.
        return {"home": SUBMITTED_LINEUP, "away": SUBMITTED_LINEUP}
    # `viewer_team_id is not None` FIRST: a Game may carry a NULL side (a
    # placeholder fixture), and `None in (home, None)` is True — which would
    # hand FULL access to a caller whose own team never resolved.
    if role in _TEAM_SCOPED and viewer_team_id is not None \
            and viewer_team_id in (home_team_id, away_team_id):
        return {
            "home": FULL if viewer_team_id == home_team_id else RESTRICTED,
            "away": FULL if viewer_team_id == away_team_id else RESTRICTED,
        }
    return {"home": RESTRICTED, "away": RESTRICTED}


def route_audience(role, viewer_team_id, home_team_id, away_team_id):
    """WHICH AUDIENCE this caller belongs to on a SINGLE-ANSWER private-game
    sibling — ``/roster-status``, ``/roster``, ``/substitutes`` and
    ``/availability-summary`` (#427 final blocker, owner ruling: "the
    acceptance boundary is the caller's obtainable private game state, not
    only `/board` and `/lineups`; leaving F2/F3 would make the fix bypassable
    through sibling routes").

    THE DEFECT THIS EXISTS TO CLOSE. All of them sat behind the SAME
    single ``can_read_private_game_data`` gate as ``/board`` and ``/lineups``,
    and like them carried no team-level narrowing at all — so once the two
    lineup reads were projected, an identical pivot remained one path segment
    away: ``/roster`` handed either Coach the opponent's seated rows,
    ``/substitutes`` handed either Coach *and* an assigned official the
    opponent's substitute workflow rows, and ``/roster-status`` hard-coded HOME
    for everybody exactly as ``get_board`` used to.

    THE FIFTH LEAF, and why it was last (round 2).
    ``/availability-summary`` is the ONLY route in this family that reads a
    side FROM THE QUERY STRING, and its narrowing was spelled inline in
    ``web/server.py`` naming only COACH and PLAYER — so an assigned OFFICIAL
    fell straight through it. Measured tri-store over a real session at
    ae21c40: the un-hinted call answered that official 400 while
    ``?team_id=<either side>`` answered **200** with that side's whole
    candidate pool, carrying NAMES and per-player availability — the two
    classes ``_submitted_lineup_rows``/``_submitted_lineup_status`` exist to
    remove, recovered one path segment from where they are removed. The
    client hint was the SOLE side selector, which is the ruling's "ignore
    client side hints" defeated on the one route that reads one.

    A HINT IS ADJUDICATED HERE, NEVER TRUSTED. ``FULL`` keeps it (an unscoped
    operator may read either side anyway, and narrowing them would be its own
    regression); ``OWN_SIDE`` IGNORES it and answers for the trusted side, so
    a hinted call is byte-identical to an un-hinted one exactly as it is on
    the other four; ``SUBMITTED_LINEUP``/``RESTRICTED`` refuse. Ignoring
    rather than refusing is deliberate: a 403 raised only for the opponent's
    id is itself an oracle for which team is playing, while an unchanged
    own-side answer discloses nothing the caller did not already hold.

    Returns one of the SAME four labels :func:`side_projections` uses, so the
    two-sided and flat-list reads cannot describe one caller differently:

    ``FULL``
        Unscoped operator (or an in-process caller with no role) — the whole
        game, unchanged. Narrowing the operator would be its own regression.
    ``OWN_SIDE``
        Coach / Player — rows this game DURABLY attributes to their own side.
    ``SUBMITTED_LINEUP``
        Assigned official. Servable only where the submitted lineup is
        genuinely what the route is for (``/roster``); the routes whose whole
        subject is candidate, availability or substitute workflow state
        refuse it instead, because an official referees the game and does not
        manage anyone's roster. Each route names its own choice — that is a
        per-route decision, not a property of the role.
    ``RESTRICTED``
        Nothing on this route. A flat list has no per-side envelope to carry a
        ``restricted: true`` marker the way ``/lineups`` does, so the honest
        answer is an explicit refusal — never ``[]``, which asserts the
        operationally different "there is none".

    FAIL CLOSED, on the same clause order and for the same reason
    :func:`side_projections` gives: ``viewer_team_id is not None`` is tested
    FIRST, because a Game may carry a NULL side and ``None in (home, None)``
    is ``True`` — which would hand a caller whose own team never resolved the
    full whole-game read.
    """
    if role is None or role in _UNSCOPED_OPERATORS:
        return FULL
    if role == Role.OFFICIAL:
        # Assignment to THIS game is proven upstream by
        # `can_read_private_game_data`'s OFFICIAL branch.
        return SUBMITTED_LINEUP
    if role in _TEAM_SCOPED and viewer_team_id is not None \
            and viewer_team_id in (home_team_id, away_team_id):
        return OWN_SIDE
    return RESTRICTED


def own_side(role, viewer_team_id, home_team_id, away_team_id):
    """THE ONE side a single-sided read (``/board``) must answer for, or
    ``None`` to mean "this caller has no own side, use the caller-supplied or
    default side".

    ``get_board`` used to hard-code ``game.home_team_id`` for everybody,
    which is how an AWAY Coach was handed the HOME pool. A team-scoped caller
    now gets THEIR side and only theirs; an unscoped operator keeps the
    existing default, and an official has no side of their own."""
    if role in _TEAM_SCOPED and viewer_team_id is not None \
            and viewer_team_id in (home_team_id, away_team_id):
        return viewer_team_id
    return None
