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
existing ``from .scope import game_scoped_own_team_id`` IMPORT still
resolves and there is still exactly ONE definition.

THE CALLERS ARE NOT UNCHANGED, AND THIS USED TO SAY THEY WERE. Until #427
round 20 the sentence above ended "so every existing caller is unchanged",
which was true of the MOVE and stopped being true of the projection:
``game_scoped_own_team_id`` no longer takes the session mapping, so its
ARITY changed and every call site was rewritten to hand it two immutable
ids. What survives untouched is the IMPORT PATH, not the call. A stale
"callers are unchanged" in an authorization module is exactly the kind of
claim a later reader relies on, so it is corrected here rather than left to
be inferred from the signature. The migrated call sites are inventoried, and
a new one that hands this function anything but a scalar fails by name —
see ``tests/test_authenticated_side_noninterference``'s
``_refuse_a_resolver_caller_that_still_passes_a_mapping``.

NOTHING HERE READS A REQUEST. The inputs are a session-resolved ``role``,
the session's own ``scope`` binding, the ``game`` the server already
selected, and the store. A query string, a body field or a header can
never reach this resolution, which is the whole property the private-game
family rests on.

WHAT THE STATIC AUDIT OVER THIS MODULE IS WORTH, AND IT IS THE OWNER'S
RULING (#427 round 23). ``tests/test_authenticated_side_noninterference``
derives this module's admission branches and refuses fourteen classes of
shape. That audit is a MANDATORY STRUCTURAL TRIPWIRE, NOT AUTHORIZATION
PROOF: STATIC RED BLOCKS, STATIC GREEN NEVER CLOSES A BLOCKER, and the
authenticated HTTP matrix beside it is authoritative only within its
DECLARED COVERAGE. Two measurements say why, and both are of green audits
over a wrong product: a rebinding made from ANOTHER package after import
changes zero characters of this file, so the audit is empty by
construction while sixty cells of the private-game family flip tri-store;
and in round 23 a FACADE surface one file away
(``ApiService._schedule_roster_status``) still took the session mapping
whole after this module had stopped, which handed a third team's Coach
HOME's private roster status with both audits green. Do not read an empty
audit as evidence that a change to this boundary is safe.
"""

from dataclasses import dataclass
from typing import Optional, Tuple

from ..domain import Role
from .roster_service import RosterService
from .subject_scope import assignment_grants_official_scope


def _player_team_for_game(scoped_player_id, authorization, store):
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
    pointer only when the game carries no LeagueSeason binding (exhibitions
    and unbound legacy games), exactly as ``team_for_game`` itself does —
    byte-for-byte pre-#205 behavior there.

    AND IT TAKES THE FROZEN ``authorization``, NOT THE ``Game`` (#427 round
    24). The membership query below is the LAST thing in this boundary that
    still read the mutable row, and it read it AFTER
    :func:`resolve_private_game_read` had already snapshotted the facts the
    decision is carried in — so a competition rebinding landing between the
    two reads moved admission while the record said otherwise. The snapshot
    answers every field this query needs, ``id`` included, and it is the
    same subject either way.

    TAKES THE PLAYER'S ID, NOT THE SESSION MAPPING (#427 round 20, the
    owner's blocker-2 ruling). It used to take ``scope`` and read
    ``scope.get("player_id")`` out of it for itself, which put a MUTABLE
    mapping one call deeper than the boundary that vouches for it — see
    :func:`resolve_private_game_read`'s "THE PROJECTION" note. The value is
    the same value; what is gone is the object it used to arrive inside.
    """
    if not scoped_player_id:
        return None
    player = store.get_player(scoped_player_id)
    if player is None or not player.is_active:
        return None
    return RosterService(store).team_for_game(authorization, player)


def game_scoped_own_team_id(role, scoped_team_id, scoped_player_id,
                            authorization, store):
    """The team the caller acts for, resolved specifically against ONE
    game (#205 blocker 1) — the game-scoped analogue of
    ``subject_scope.own_team_id``.

    IT TAKES TWO IMMUTABLE IDS, NOT THE SESSION MAPPING (#427 round 20, the
    owner's blocker-2 ruling). Every caller PROJECTS its own scope into
    these two scalars and hands the scalars over; the mapping itself never
    crosses this boundary. See :func:`resolve_private_game_read`'s "THE
    PROJECTION" note for why the mapping's absence is the whole point and
    not a tidying: between the projection and this decision there is no
    longer a mutable object for anything to change.

    A Coach's team is unchanged: still the permanently-bound
    ``scope["team_id"]``, now arriving as ``scoped_team_id``. There is no
    ``CoachSeasonMembership`` (or any
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
    if role == Role.COACH:
        return scoped_team_id
    if role == Role.PLAYER:
        return _player_team_for_game(scoped_player_id, authorization,
                                     store)
    return None


@dataclass(frozen=True)
class GameAuthorization:
    """THE FIVE FACTS ABOUT A ``Game`` A PRIVATE READ IS AUTHORIZED BY,
    FROZEN AT THE MOMENT THE DECISION IS TAKEN (#427 round 23, the owner's
    class-3 ruling).

    WHY A SNAPSHOT AND NOT THE ROW. ``Game`` is MUTABLE and stays mutable,
    and freezing the domain model was never on the table: MEASURED on this
    tree, ELEVEN production statements write a ``Game`` in place — ``locked``
    three times, ``published`` twice, and ``is_draft``, ``cancelled``,
    ``ice_slot_id``, ``start_time``, ``end_time`` and ``rink`` once each,
    across ``api/service.py``, ``services/roster_service.py`` and
    ``services/setup_service.py``. :class:`PrivateGameRead` used to hold that
    live row and answer ``side_ids`` out of it ON EVERY READ, so the two team
    ids a leaf was served for were re-read from an object anything holding
    the same row could have changed since admission was decided. The record
    already half-did this — one fetch, carried rather than recomputed — and
    this is the completion of it: the row is read ONCE, its five
    authorization-bearing fields are copied here, and nothing downstream
    touches the ``Game`` again.

    WHAT THE MEASUREMENT DOES *NOT* SAY, because the honest version is
    weaker than the dramatic one: NONE of those eleven writes touches any of
    the five fields below. This freeze is therefore not defending against a
    mutation the product performs today — it is closing the SHAPE, which is
    that an authorization decision was carried in a record that re-derived
    its answer from a mutable object every time it was asked. Nothing
    structural stopped the twelfth write from being ``home_team_id``.

    EXACTLY THESE FIVE, and they are the owner's ruling verbatim. What each
    is for, stated rather than left to be discovered:

    * ``home_team_id``/``away_team_id`` are what ADMISSION DECIDES BY — the
      participation test in :func:`resolve_private_game_read` — and what
      :attr:`PrivateGameRead.side_ids` answers, which is the pair every
      leaf of the private-game family projects against.
    * ``game_id`` is WHICH GAME this decision was taken about, so a record
      that outlives its call site still says so.
    * ``league_season_id`` and ``season_id`` are the game's COMPETITION
      BINDING — the two fields
      ``RosterService.resolve_membership_context`` validates the PLAYER
      authority's membership spine against. NOTHING IN-TREE READS THEM
      TODAY, and that is said here rather than left for a later reader to
      discover and quietly delete: they are carried because the ruling names
      them, because a post-capture rebinding of the game's competition is
      exactly the mutation this record must not follow, and because a
      snapshot that states only what today's consumers happen to read goes
      stale the first time a consumer changes.

    NOT A ``Game`` SUBSTITUTE, AND THE ONE PLACE IT IS HANDED WHERE A
    ``Game`` USED TO GO IS DELIBERATE (#427 round 24). This record answers
    exactly the five fields above plus :attr:`id`, which is ``game_id``
    under the name a row spells it — and nothing else. It is handed to the
    PLAYER authority's membership resolution, which used to take the live
    ``Game``: that query runs and finishes INSIDE
    :func:`resolve_private_game_read`, so taking the row there was never a
    lifetime bug, but it did mean the decision READ THE MUTABLE OBJECT
    AGAIN after the snapshot had been taken, and a competition rebinding
    between the two reads could move admission while the frozen record
    stayed put. It now decides from the same frozen facts the rest of the
    function does. Anything this record does NOT answer still fails loudly
    where a real ``Game`` is required, which is the property that keeps the
    substitution honest rather than convenient.
    """

    game_id: Optional[str]
    home_team_id: Optional[str]
    away_team_id: Optional[str]
    league_season_id: Optional[str]
    season_id: Optional[str]

    @property
    def id(self):
        """``game_id`` under the name a ``Game`` answers it by (#427 round 24).

        The membership resolver this snapshot is now handed reads a game by
        the SAME five attribute names the snapshot carries, and anything it
        reaches that wants an id spells it ``.id``. Exposing it here is what
        lets the PLAYER decision be taken against the FROZEN facts without
        widening the snapshot or reshaping the resolver: read-only, derived,
        and adding no fact the snapshot did not already hold."""
        return self.game_id

    @classmethod
    def of(cls, game):
        """The snapshot of ``game``, or ``None`` when there is no game.

        ``None`` in, ``None`` out, so the not-found passthrough and the
        operator short-circuit share one spelling with every other branch
        instead of each testing for the absent row themselves."""
        if game is None:
            return None
        return cls(game_id=game.id,
                   home_team_id=game.home_team_id,
                   away_team_id=game.away_team_id,
                   league_season_id=game.league_season_id,
                   season_id=game.season_id)


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

    IT CARRIES A FROZEN SNAPSHOT AND NOT THE ``Game`` (#427 round 23, the
    owner's class-3 ruling). This field used to be the live, mutable row and
    ``side_ids`` re-read it on every access — so the two side ids a leaf was
    served for came off an object that anything holding the same ``Game``
    could have changed since the moment admission was decided. It is now a
    :class:`GameAuthorization`: five scalars, copied out of the row once,
    frozen. See that class for why the domain model itself is NOT frozen and
    for what each of the five fields is for.

    ``authorization`` is ``None`` only when the game does not exist; the
    caller is still ADMITTED so the facade can return its normal
    ``not_found`` payload rather than a 403 that would confirm the id's
    absence differently from every other route.

    ``own_team`` is the TRUSTED side and is ``None`` for every caller who has
    no side of their own — an unscoped operator, an assigned official, an
    in-process caller. It is never ``None`` for an ADMITTED team-scoped
    caller: that combination is exactly what admission refuses.
    """

    role: object
    authorization: Optional[GameAuthorization]
    own_team: Optional[str]
    admitted: bool

    @property
    def side_ids(self) -> Tuple[Optional[str], Optional[str]]:
        """``(home, away)`` of the game THIS decision was taken against —
        read off the FROZEN snapshot the decision was taken from, so a side
        id can never come from a different read of the row than the one that
        admitted the caller, and cannot change under a caller who is still
        holding the record."""
        if self.authorization is None:
            return (None, None)
        return (self.authorization.home_team_id,
                self.authorization.away_team_id)


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
    #
    # THE PROJECTION (#427 round 20, the owner's blocker-2 ruling). The raw
    # session `scope` is read HERE, ONCE, BEFORE ANY ROLE BRANCH, into three
    # explicit immutable scalar ids — and it is never read again. Only those
    # scalars cross into `game_scoped_own_team_id`.
    #
    # WHY THE SHAPE CHANGED RATHER THAN THE RULES. Five rounds tried to
    # INFER from the source whether the mutable mapping had been tampered
    # with between the gate receiving it and the resolver deciding from it,
    # and each was defeated one spelling later: a forged argument, a rebuilt
    # value, an in-place mutation, a resolver-side rebuild, and finally the
    # OFFICIAL branch's own read hoisted two branches up —
    #
    #     official_id = (scope.update({'team_id': game.home_team_id})
    #                    or scope.get("official_id"))
    #
    # which the liveness rule admits (`official_id` really is read) while
    # the mutation runs for every caller. Measured at `f9b094e`: static
    # audit `[]`, and over real authenticated HTTP twenty of the fifty
    # derived unentitled cells answered 200, with EIGHT of HOME's private
    # players in the `gid` `/lineups` body at `restricted=false`, on Memory,
    # SQLite and real PostgreSQL alike. An ALIAS of the same statement
    # (`_alias = scope`, mutated through `_alias`) measured byte-identically.
    #
    # "Which calls mutate shared state" is not decidable from a source tree,
    # so no liveness rule over a mutable mapping can be sound. This removes
    # the question instead of answering it: after these three lines there is
    # no mutable object left between the projection and the decision, and
    # the audit's job shrinks to three SYNTACTIC facts — each scalar is
    # assigned exactly once, each reaches the resolver unchanged, and raw
    # `scope` is never read again.
    #
    # BEHAVIOUR-PRESERVING, NOT MERELY INTENDED TO BE. `dict.get` is a pure
    # read, so hoisting the three reads above the role branches changes no
    # value any branch decides from; the operator short-circuit, the
    # not-found passthrough and all three grant branches answer exactly what
    # they answered before. Proven rather than asserted: every
    # (principal x game x leaf) cell of the private-game family was driven
    # over real authenticated HTTP, tri-store, before and after, and the
    # two matrices are equal.
    scope = scope or {}
    scoped_team_id = scope.get("team_id")
    scoped_player_id = scope.get("player_id")
    scoped_official_id = scope.get("official_id")
    # THE SUBJECT, FETCHED ONCE, AND THE SNAPSHOT TAKEN OFF IT IMMEDIATELY
    # (#427 round 23, the owner's class-3 ruling). The fetch is HOISTED above
    # the operator short-circuit, which used to do its own
    # `store.get_game(game_id)` inline: one call either way, on every path,
    # and now exactly one place where the mutable row is read at all. From
    # the next line on, every decision this function takes and everything the
    # record carries is a scalar of `authorization` — the `Game` itself is
    # handed on ONLY to the PLAYER authority's live membership query, which
    # completes before this function returns.
    game = store.get_game(game_id)
    authorization = GameAuthorization.of(game)
    if role in (Role.LEAGUE_ADMIN, Role.ARENA_MANAGER):
        # Admitted before the game matters, exactly as
        # `can_read_private_game_data` short-circuited: an operator's
        # admission does not depend on the game existing. The snapshot is
        # still carried so `side_ids` is usable.
        return PrivateGameRead(role=role, authorization=authorization,
                               own_team=None, admitted=True)
    if game is None:
        # Let the facade answer its normal not_found. Byte-for-byte the
        # pre-existing `can_read_private_game_data` behaviour.
        #
        # `authorization=None` IS SPELLED AS A LITERAL and not as the
        # (identical) `authorization`, and the reason is auditability rather
        # than style: this is the ONE admission every role reaches, and it is
        # exempt from carrying an authority precisely because it fills
        # neither side-bearing field. The audit reads that exemption off the
        # literal `None`; a name it would have to resolve is a name a later
        # spelling can rebind.
        return PrivateGameRead(role=role, authorization=None, own_team=None,
                               admitted=True)
    if role in (Role.COACH, Role.PLAYER):
        # THE PARTICIPATION TEST DECIDES AGAINST THE SNAPSHOT, not against
        # the row. The two ids it reads and the two ids `side_ids` later
        # answers are now THE SAME FROZEN PAIR, so a `Game` mutated after
        # this decision cannot move it and cannot move what it is served for.
        own_team = game_scoped_own_team_id(
            role, scoped_team_id, scoped_player_id, authorization, store)
        admitted = own_team is not None and own_team in (
            authorization.home_team_id, authorization.away_team_id)
        # `own_team` is deliberately dropped on refusal: a refused read must
        # not carry a side any downstream code could still answer for.
        return PrivateGameRead(role=role, authorization=authorization,
                               own_team=own_team if admitted else None,
                               admitted=admitted)
    if role == Role.OFFICIAL:
        # AN ASSIGNMENT THE OFFICIAL DECLINED IS NOT AN ADMISSION (#427
        # round 11). `OfficialAssignmentStatus.is_active` is the product's
        # own statement of which assignments hold anything — "Proposed or
        # accepted assignments hold the official's time" — and every other
        # consumer of these rows already honours it: `assign_official`'s
        # duplicate and overlap checks, `setup_service._active_officials`,
        # the game notification fan-outs in `setup_service`/`roster_service`,
        # and `calendar.py`, which will not even name an official's ROLE on
        # their own feed for an inactive row. This read gate was the one
        # place that did not, so a DECLINED official kept 200 on `/board`,
        # `/lineups` and `/roster` and the private sheet they carry.
        #
        # THE TEST IS NOW THE SHARED PREDICATE, not the inline conjunction it
        # was written as (#205). Fixing this surface alone left the SAME
        # question answered in three places, and the two it did not touch —
        # `context_scope._official_program_seasons` and `_official_league_ids`
        # — went on granting the declined Official's Program, Season and
        # League in the context switcher, so authorization disagreed across
        # product surfaces. `subject_scope.assignment_grants_official_scope`
        # is that one definition; this call site keeps byte-identical
        # behaviour (exact official AND `status.is_active`) while ceasing to
        # be an independent copy of it.
        admitted = scoped_official_id is not None and any(
            assignment_grants_official_scope(a, scoped_official_id)
            for a in store.assignments_for_game(game_id))
        return PrivateGameRead(role=role, authorization=authorization,
                               own_team=None, admitted=admitted)
    return PrivateGameRead(role=role, authorization=authorization,
                           own_team=None, admitted=False)
