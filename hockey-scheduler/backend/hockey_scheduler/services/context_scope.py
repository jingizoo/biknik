"""Which Programs/Seasons an account may see, for the active-context selector (#159).

A context selection is a VIEW preference, never authority: on every resolve and
set it is filtered through the caller's real role + account scope (the same
#211/#266/#202 rules the rest of the app enforces), so a scoped Coach, Player,
Official, or Guardian can neither select nor enumerate a Program/Season outside
their scope. A saved selection outside the caller's current authorized scope is
IGNORED on resolve (a fallback is returned) but is NOT rewritten, so restoring
authorization restores the choice.

Global operators (League Admin, Arena Manager) and the read-only Viewer are not
resource-scoped in the current account model (#211) — they see every Program.
If org-scoped operators are introduced later, THIS is the single place to narrow
them; resolve/set already route through here on every request.

Caller-identity resolution ("which team does this caller act for") is NOT
redefined here — it is the shared `subject_scope.own_team_id`, the SAME resolver
the web scope guards use, so the two gates can never drift. This module only adds
the new Program/Season *projection* on top of that canonical identity.
"""

from ..domain import GameType, Role
from . import scope_bridge
from . import season_guard
from .league_scope import (
    exact_league_season_or_conflict, exact_registration_or_conflict)
from .subject_scope import assignment_grants_official_scope
from .subject_scope import own_team_id as _own_team_id

# Roles that see every Program under the current account model (#211): the two
# global operators plus the global read-only Viewer.
_GLOBAL_ROLES = frozenset({Role.LEAGUE_ADMIN, Role.ARENA_MANAGER, Role.VIEWER})


def _team_season_ids(store, team):
    """Season ids a Team actively participates in — its permanent League's
    LeagueSeasons crossed with its active registrations (#283 permanent model).

    Scope note (#159): this is the Team's CURRENT-league active participation.
    A same-league Season that is later ARCHIVED stays here (its registration is
    still active), so a scoped user keeps read-only access to its own league's
    history. But once a Team TRANSFERS to a new League, #283 freezes its prior
    registration under the FORMER LeagueSeason — that Season is intentionally NOT
    surfaced here, so a scoped Coach/Player/Guardian loses prior-Team history.
    Restoring historical entitlement across a Team's prior registrations is a
    deliberate #159 follow-up (see ``docs/architecture/season-lifecycle.md``);
    it is deferred because it would widen a scoped user's view to Seasons under a
    League their Team has left, which warrants its own reviewed slice."""
    out = set()
    if team is None:
        return out
    for ls in store.league_seasons_for_league(team.league_id):
        # #331 review round 19: fail CLOSED on exact-key multiplicity. This
        # is a read-only visibility gate with no caller to report a
        # structured conflict to -- an ambiguous key must never grant
        # access, and must never vary with insertion order (a corrupted
        # duplicate row must not let which one happens to load first decide
        # whether a scoped Coach/Player/Guardian can see this Season).
        reg, _conflicts = exact_registration_or_conflict(store, ls.id, team.id)
        if reg is not None and reg.active:
            out.add(ls.season_id)
    return out


def _official_program_seasons(store, official_id):
    """An Official's authorized (programs, seasons) — those of the games they are
    assigned to (mirrors web.scope.can_read_private_game_data's official branch).

    Which assignments COUNT is
    :func:`~.subject_scope.assignment_grants_official_scope`, the one shared
    product predicate — not a status test spelled out again here. See that
    function for the drift this closes: a DECLINED assignment stopped admitting
    the Official to the private-game family while still offering them the
    target Program and Season in the context switcher.
    """
    programs, seasons = set(), set()
    if not official_id:
        return programs, seasons
    for a in store.assignments_for_official(official_id):
        if not assignment_grants_official_scope(a, official_id):
            continue
        game = store.get_game(a.game_id)
        if game is None or not game.season_id:
            continue
        season = store.get_season(game.season_id)
        if season is None:
            continue
        seasons.add(season.id)
        programs.add(scope_bridge.season_scope_id(season))
    return programs, seasons


def _guardian_program_seasons(store, user_id):
    """A Guardian's authorized (programs, seasons) — those of the juniors they are
    VERIFIED-linked to (an unverified link grants nothing, #26/#35)."""
    programs, seasons = set(), set()
    if not user_id:
        return programs, seasons
    for link in store.guardian_links_for(user_id):
        if not getattr(link, "verified", False):
            continue
        player = store.get_player(link.player_id)
        if player is None or not player.team_id:
            continue
        team = store.get_team(player.team_id)
        if team is None:
            continue
        pid = scope_bridge.team_scope_id(team)
        if pid:
            programs.add(pid)
        seasons |= _team_season_ids(store, team)
    return programs, seasons


def authorized_program_ids(store, role, scope, user_id):
    """The set of Program ids the account may see/select. Global roles see all;
    scoped roles see only what their subject resolves to; anything unrecognized
    fails closed (empty)."""
    if role in _GLOBAL_ROLES:
        return {p.id for p in store.all_programs()}
    if role in (Role.COACH, Role.PLAYER):
        team_id = _own_team_id(role, scope, store)
        team = store.get_team(team_id) if team_id else None
        pid = scope_bridge.team_scope_id(team) if team else None
        return {pid} if pid else set()
    if role == Role.OFFICIAL:
        return _official_program_seasons(store, (scope or {}).get("official_id"))[0]
    if role == Role.GUARDIAN:
        return _guardian_program_seasons(store, user_id)[0]
    return set()


def authorized_season_ids(store, role, scope, program_id, user_id):
    """The set of Season ids the account may see/select within ``program_id``."""
    if program_id is None:
        return set()
    program_seasons = {s.id for s in store.seasons_for_program(program_id)}
    if role in _GLOBAL_ROLES:
        return program_seasons
    if role in (Role.COACH, Role.PLAYER):
        team_id = _own_team_id(role, scope, store)
        team = store.get_team(team_id) if team_id else None
        return _team_season_ids(store, team) & program_seasons
    if role == Role.OFFICIAL:
        return (_official_program_seasons(store, (scope or {}).get("official_id"))[1]
                & program_seasons)
    if role == Role.GUARDIAN:
        return _guardian_program_seasons(store, user_id)[1] & program_seasons
    return set()


def _official_league_ids(store, official_id):
    """The Leagues an Official's ASSIGNMENTS actually grant (#345 review).

    Derived from each assigned Game's own FROZEN competition identity —
    ``game.league_season_id``, the single source of truth #283 Slice E
    established — and never from either Team's mutable permanent
    ``Team.league_id``.

    The distinction is the whole point, and reading the Team was a real defect:
    a Team transfer deliberately changes ``Team.league_id`` WITHOUT rewriting
    historical Games, so a Team-derived projection hands the Official whichever
    League their opponent happens to sit in today. After a transfer that both
    granted a League the assignment never covered and revoked the League the
    assignment actually grants — a scoped role enumerating outside its scope,
    which is exactly what this module exists to prevent.

    Fails CLOSED, contributing nothing for a Game whose identity is missing or
    drifted: an exhibition (no owning LeagueSeason by definition), a regular
    Game with no ``league_season_id``, a dangling binding, or a binding that
    disagrees with the Game's own denormalized ``season_id``/``league_id``. A
    read-only visibility gate has no caller to report a structured conflict to,
    so an untrustworthy identity must never widen what the Official can see.

    Strictly READ-ONLY: it resolves existing bindings and never creates or
    repairs one, even when it detects drift.
    """
    leagues = set()
    if not official_id:
        return leagues
    for a in store.assignments_for_official(official_id):
        # Which assignments COUNT is the one shared predicate, identical to
        # the Program/Season projection above and to the private-game
        # admission — see `subject_scope.assignment_grants_official_scope`.
        if not assignment_grants_official_scope(a, official_id):
            continue
        game = store.get_game(a.game_id)
        if game is None:
            continue
        # Exhibitions carry no owning League at all (#283 Slice D), so an
        # assignment to one grants no League view.
        if (game.game_type or GameType.REGULAR.value) != GameType.REGULAR.value:
            continue
        if not season_guard.game_is_league_season_bound(game):
            continue
        ls = store.get_league_season(game.league_season_id)
        if ls is None:
            continue
        # The binding must agree with the Game's own columns. They cannot drift
        # through any supported write path, so disagreement means corrupted or
        # legacy data -- fail closed rather than trust either side.
        #
        # PR #427: the Season comparison is UNCONDITIONAL, and the League one
        # stays as it was. The `game.season_id and` prefix it used to carry was
        # the same falsy-skip the two write guards carried: a bound Game with a
        # NULL `season_id` skipped the check entirely and the Official was
        # granted the League anyway -- widening a scoped role's visibility off a
        # Game whose identity does not hold together, which is precisely what
        # this module exists to prevent. `games.season_id` is nullable with no
        # FK and no CHECK, so NULL is the reachable corrupted shape, not an
        # exemption. Same reasoning as #331 review round 24 applied to
        # `league_id` one line down.
        if ls.season_id != game.season_id:
            continue
        if game.league_id and ls.league_id != game.league_id:
            continue
        if ls.league_id:
            leagues.add(ls.league_id)
    return leagues


def _own_league_ids(store, role, scope, user_id):
    """The permanent League ids a SCOPED account's own subject belongs to.

    Coach/Player resolve through the same canonical ``own_team_id`` identity the
    web scope guards use, so the League projection can never drift from the Team
    projection above. Guardian resolves through its VERIFIED links only. Both are
    genuinely Team-derived: their entitlement follows the Team wherever it goes,
    so reading the Team's CURRENT permanent League is correct for them — unlike
    an Official, whose entitlement is per-Game and therefore resolves through
    :func:`_official_league_ids` against each Game's frozen identity instead."""
    leagues = set()
    if role in (Role.COACH, Role.PLAYER):
        team_id = _own_team_id(role, scope, store)
        team = store.get_team(team_id) if team_id else None
        if team is not None and team.league_id:
            leagues.add(team.league_id)
        return leagues
    if role == Role.GUARDIAN:
        for link in store.guardian_links_for(user_id or ""):
            if not getattr(link, "verified", False):
                continue
            player = store.get_player(link.player_id)
            if player is None or not player.team_id:
                continue
            team = store.get_team(player.team_id)
            if team is not None and team.league_id:
                leagues.add(team.league_id)
        return leagues
    if role == Role.OFFICIAL:
        return _official_league_ids(store, (scope or {}).get("official_id"))
    return leagues


def authorized_league_ids(store, role, scope, program_id, user_id,
                          season_id=None):
    """The permanent League ids the account may see/select within ``program_id``
    (#345), optionally narrowed to those BOUND to ``season_id``.

    League is the third persistent context axis. This mirrors
    ``authorized_season_ids`` exactly in shape and discipline, so all three axes
    are filtered by one consistent rule set:

    * the candidate set is always the Leagues permanently under ``program_id``,
      so a League from another Program can never appear regardless of role —
      the cross-Program invariant is enforced by construction here, not only by
      the selection-time check in the service;
    * global roles (League Admin, Arena Manager, Viewer) see all of them;
    * a scoped role sees only the League(s) its own subject belongs to,
      intersected with the Program's — so a scoped Coach/Player/Guardian/
      Official can neither select nor ENUMERATE a League outside their scope;
    * anything unrecognized fails closed (empty).

    When ``season_id`` is given, the result is further narrowed to Leagues with
    an EXISTING ``LeagueSeason`` binding to that Season, resolved through
    :func:`exact_league_season_or_conflict` so an ambiguous binding fails closed
    rather than granting access on whichever duplicate row sorts first. This is
    a read-only projection: it never creates a binding.
    """
    if program_id is None:
        return set()
    program_leagues = {lg.id for lg in store.leagues_for_program(program_id)}
    if role in _GLOBAL_ROLES:
        allowed = program_leagues
    elif role in (Role.COACH, Role.PLAYER, Role.OFFICIAL, Role.GUARDIAN):
        allowed = _own_league_ids(store, role, scope, user_id) & program_leagues
    else:
        return set()
    if season_id is None:
        return allowed
    return {lid for lid in allowed
            if exact_league_season_or_conflict(store, lid, season_id)[0]
            is not None}
