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

from ..domain import Role
from . import scope_bridge
from .league_scope import (
    exact_league_season_or_conflict, exact_registration_or_conflict)
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
    assigned to (mirrors web.scope.can_read_private_game_data's official branch)."""
    programs, seasons = set(), set()
    if not official_id:
        return programs, seasons
    for a in store.assignments_for_official(official_id):
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


def _own_league_ids(store, role, scope, user_id):
    """The permanent League ids a SCOPED account's own subject belongs to.

    Coach/Player resolve through the same canonical ``own_team_id`` identity the
    web scope guards use, so the League projection can never drift from the Team
    projection above. Guardian resolves through its VERIFIED links only. Official
    is derived from the Teams actually playing in the games they are assigned to
    — an Official's entitlement is per-game, so their League view is exactly the
    Leagues of those games' teams and nothing wider."""
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
        official_id = (scope or {}).get("official_id")
        if not official_id:
            return leagues
        for a in store.assignments_for_official(official_id):
            game = store.get_game(a.game_id)
            if game is None:
                continue
            for team_id in (game.home_team_id, game.away_team_id):
                team = store.get_team(team_id) if team_id else None
                if team is not None and team.league_id:
                    leagues.add(team.league_id)
        return leagues
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
