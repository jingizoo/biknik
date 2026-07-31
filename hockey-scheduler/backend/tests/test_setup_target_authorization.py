"""Permission to use a setup capability is not authority over every setup
RECORD (#369).

The blocker: an Arena Manager POSTed
``/api/v2/setup/venue/<foreign-id>/delete`` for a Venue created by a different
account, linked to a different Program. The route answered 200, echoed the
foreign record's own fields back (its NAME included — an information leak on
top of the write), deleted the row, and wrote a ``venue_deleted`` audit entry.
Nothing was wrong with the role gate: an Arena Manager genuinely may delete
Venues. What was missing was any check of the TARGET.

What this file proves, per mutation route and per API version:

positive        the target inside the caller's ACTIVE Program is accepted
foreign         a target in another Program is refused
nonexistent     refused RAW-BODY-IDENTICALLY to the foreign case — compared as
                BYTES, since two equal dicts say nothing about what a reader of
                the socket can distinguish (key order, spacing, a stray field).
                The only permitted difference is the echoed id itself, which is
                normalised out exactly as the owner's repro does.
no-mutation     after a refusal the store's row-id set AND the setup-audit row
                count are both unchanged: no write, no audit, no trace
context-switch  switching to the target's OWN Program makes the SAME call
                succeed — proving a scope decision was made, not a blanket
                block. Without this case, a guard that refused everything
                would pass the four cases above.

Fixture rule, and it is the whole reason these tests can fail: the two
identities are GENUINELY distinguishable. ``attacker`` created nothing at all
and shares no Program with the records it attacks, so no positive result in
here can be explained by creator ownership, and no refusal can be explained by
the record being unreachable for some unrelated reason. Every refusal asserts
its own precondition FIRST — the target's Program chain, recomputed in this
file straight from the store rather than read back out of the code under test.

ONE SCOPING NUANCE, deliberately not asserted here. The owner's repro states
the precondition as "prove the Venue is absent from reads". On THIS branch
``get_setup_overview_v2`` is NOT yet Program-scoped — that lands in #369
proper, which rebases on top of this work — so read-absence is not assertable
yet and asserting it would fail. Everywhere that precondition belongs, this
file asserts instead the Program-chain FACT the guard actually relies on (the
record resolves to a Program that is not the active one) and says so at the
call site. The write behaviour is asserted in full; nothing else is weakened to
compensate, and no read scoping is added on this branch.

PART 1b and PART 2b cover the OTHER TWO AXES (#369 re-review). The first cut
of this gate resolved all three and then discarded Season and League, judging
every record by its Program alone: with Program P / Season S / League A
persisted, ``POST /api/v2/setup/player/<League-B-player>/update`` returned 200
and renamed a Player in a League the caller had not selected, and the identical
construction let a Season-A caller mutate Season-B rows. Those parts hold the
Program CONSTANT and vary exactly one of the other two axes, so no assertion in
them can be satisfied by the Program ceiling, and each asserts -- from the
stored rows, through ``chain_axes`` -- that its victim differs from the
selection in that one axis and nothing else.

The facility-tree EXCEPTION is covered explicitly, because it is the one
deviation from the rule above: ``setup_venue_grantable`` governs ONLY the Venue
argument of ``POST /api/v2/setup/seasons/<id>/venue-access``, since an arena
serves several leagues and the generic rule deadlocked sharing on its first
use. So a Venue linked to ANOTHER Program is still grantable, a Venue linked to
NOTHING and created by a DIFFERENT account is not (that exact case leaked in an
earlier round), and the SEASON argument of the same route stays generic.
"""

import json
import os
import tempfile
import threading
import time as _time
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from http.cookiejar import CookieJar
from http.server import ThreadingHTTPServer

from helpers import BACKEND  # noqa: F401  (ensures sys.path is set up)

from hockey_scheduler.api import ApiService
from hockey_scheduler.domain import Role
from hockey_scheduler.store import InMemoryStore, SqlStore


def _backends():
    """The repo's established facade-leg idiom: Memory, SQLite, and PostgreSQL
    when TEST_DATABASE_URL is configured (see test_hierarchy_program_scope)."""
    yield "memory", InMemoryStore()
    yield "sqlite", SqlStore(":memory:")
    url = os.environ.get("TEST_DATABASE_URL")
    if url:
        store = SqlStore(url)
        store.clear_all_data()
        yield "postgres", store


def _close(store):
    if isinstance(store, SqlStore):
        store.close()


_FUTURE = datetime(2031, 3, 1, 18, 0, tzinfo=timezone.utc)


def _slot_times(offset_hours=0):
    start = _FUTURE + timedelta(hours=offset_hours)
    return start.isoformat(), (start + timedelta(hours=1)).isoformat()


# --------------------------------------------------------------------------
# The Program chain, recomputed INDEPENDENTLY of the code under test.
#
# Every refusal in this file asserts its precondition through this function
# rather than through ``ApiService._setup_target_edges``. If the
# precondition were read out of the same helper the guard uses, a bug that
# collapsed the chain to the empty set would make both the guard and its own
# precondition agree, and the test would pass while the product leaked.
# --------------------------------------------------------------------------
def chain_programs(store, kind, record_id):
    """The set of Program ids ``record_id`` is LINKED to, derived from the
    stored rows only. Mirrors the documented chain in ``service.py`` — Program
    is itself; Season/League carry program_id; a LeagueSeason resolves through
    its Season; Division through its LeagueSeason; Team through program_id or
    (only when null) its permanent League; Player through its Team; Game
    through its Season, else League, else Division; Club through its Teams;
    Official through its home Club and officiated Games; Venue through EVERY
    SeasonVenueAccess grant (active or revoked) plus the legacy
    ``Venue.league_id``, which holds a PROGRAM id; Rink through its Venue;
    IceSlot through its Rink; Organization through the Programs it operates
    and the Programs its Venues serve."""
    kind = kind.replace("-", "_")
    if kind == "program":
        return {record_id} if store.get_program(record_id) else set()
    if kind == "season":
        season = store.get_season(record_id)
        return {season.program_id} if season and season.program_id else set()
    if kind == "league":
        league = store.get_league(record_id)
        return {league.program_id} if league and league.program_id else set()
    if kind == "league_season":
        ls = store.get_league_season(record_id)
        return chain_programs(store, "season", ls.season_id) if ls else set()
    if kind == "division":
        division = store.get_division(record_id)
        if division is None:
            return set()
        return chain_programs(store, "league_season", division.league_season_id)
    if kind == "team":
        team = store.get_team(record_id)
        if team is None:
            return set()
        if team.program_id:
            return {team.program_id}
        if team.league_id:
            return chain_programs(store, "league", team.league_id)
        return set()
    if kind == "player":
        player = store.get_player(record_id)
        if player is None or not player.team_id:
            return set()
        return chain_programs(store, "team", player.team_id)
    if kind == "game":
        game = store.get_game(record_id)
        if game is None:
            return set()
        if game.season_id:
            return chain_programs(store, "season", game.season_id)
        if game.league_id:
            return chain_programs(store, "league", game.league_id)
        if game.division_id:
            return chain_programs(store, "division", game.division_id)
        return set()
    if kind == "club":
        ids = set()
        for team in store.all_teams():
            if team.club_id == record_id:
                ids |= chain_programs(store, "team", team.id)
        return ids
    if kind == "official":
        official = store.get_official(record_id)
        if official is None:
            return set()
        ids = set()
        if official.home_club_id:
            ids |= chain_programs(store, "club", official.home_club_id)
        for assignment in store.assignments_for_official(record_id):
            ids |= chain_programs(store, "game", assignment.game_id)
        return ids
    if kind == "venue":
        venue = store.get_venue(record_id)
        if venue is None:
            return set()
        ids = set()
        for grant in store.season_venue_access_for_venue(record_id):
            ids |= chain_programs(store, "season", grant.season_id)
        if venue.league_id:
            ids.add(venue.league_id)   # LEGACY bridge: this IS a Program id
        return ids
    if kind == "rink":
        rink = store.get_rink(record_id)
        if rink is None or not rink.venue_id:
            return set()
        return chain_programs(store, "venue", rink.venue_id)
    if kind == "ice_slot":
        slot = store.get_ice_slot(record_id)
        if slot is None or not slot.rink_id:
            return set()
        return chain_programs(store, "rink", slot.rink_id)
    if kind == "organization":
        ids = set()
        for program in store.all_programs():
            if program.operator_organization_id == record_id:
                ids.add(program.id)
        for venue in store.all_venues():
            if venue.organization_id == record_id:
                ids |= chain_programs(store, "venue", venue.id)
        return ids
    # The two bridge rows are judged by the parent that names their Program.
    if kind == "registration":
        row = store.get_season_team_registration(record_id)
        if row is None:
            return set()
        return chain_programs(store, "league_season", row.league_season_id)
    if kind == "season_venue_access":
        row = store.get_season_venue_access(record_id)
        if row is None:
            return set()
        return chain_programs(store, "season", row.season_id)
    raise AssertionError(f"chain_programs has no rule for kind {kind!r}")


# --------------------------------------------------------------------------
# The full LINK TRIPLE, recomputed INDEPENDENTLY of the code under test.
#
# ``chain_programs`` above answers only the Program axis, which is exactly the
# question the first cut of #369 mistook for the whole one: it resolved all
# three axes and then discarded Season and League, so a League-B Player in the
# caller's OWN Program answered 200 to an update. This function returns every
# ``(program, season, league)`` link the STORED ROWS say a record carries, so
# each axis negative below can assert its own precondition -- "the same
# Program, the same Season, a different League" -- rather than trusting the
# predicate it is about to measure.
# --------------------------------------------------------------------------
def chain_axes(store, kind, record_id):
    """Every ``(program_id, season_id | None, league_id | None)`` link this
    record carries, derived from the stored rows only.

    A ``None`` component means the link genuinely names no such axis -- a
    permanent League has no Season, the facility tree has no League -- never
    "unknown" and never "any". The Program component of every triple agrees
    with ``chain_programs`` by construction; the other two are what this file's
    axis negatives are about."""
    kind = kind.replace("-", "_")

    if kind == "program":
        return {(record_id, None, None)} if store.get_program(record_id) else set()
    if kind == "season":
        season = store.get_season(record_id)
        if season is None or not season.program_id:
            return set()
        return {(season.program_id, season.id, None)}
    if kind == "league":
        # PERMANENT: a League outlives the Seasons it plays in, so it names no
        # Season of its own -- its per-Season participation is a LeagueSeason.
        league = store.get_league(record_id)
        if league is None or not league.program_id:
            return set()
        return {(league.program_id, None, league.id)}
    if kind == "league_season":
        ls = store.get_league_season(record_id)
        if ls is None:
            return set()
        season = store.get_season(ls.season_id)
        if season is None or not season.program_id:
            return set()
        return {(season.program_id, season.id, ls.league_id or None)}
    if kind == "division":
        division = store.get_division(record_id)
        if division is None:
            return set()
        return chain_axes(store, "league_season", division.league_season_id)
    if kind == "team":
        # PERMANENT too: a Team's Season participation is its registration, not
        # the Team row, so a Team carries a League and no Season.
        team = store.get_team(record_id)
        if team is None:
            return set()
        if team.program_id:
            return {(team.program_id, None, team.league_id or None)}
        if team.league_id:
            league = store.get_league(team.league_id)
            if league is not None and league.program_id:
                return {(league.program_id, None, team.league_id)}
        return set()
    if kind == "player":
        player = store.get_player(record_id)
        if player is None or not player.team_id:
            return set()
        return chain_axes(store, "team", player.team_id)
    if kind == "game":
        game = store.get_game(record_id)
        if game is None:
            return set()
        league_id = game.league_id or None
        if league_id is None and getattr(game, "league_season_id", None):
            ls = store.get_league_season(game.league_season_id)
            league_id = ls.league_id if ls is not None else None
        if league_id is None and game.division_id:
            division = store.get_division(game.division_id)
            ls = (store.get_league_season(division.league_season_id)
                  if division is not None else None)
            league_id = ls.league_id if ls is not None else None
        if game.season_id:
            season = store.get_season(game.season_id)
            if season is None or not season.program_id:
                return set()
            return {(season.program_id, season.id, league_id)}
        if game.league_id:
            league = store.get_league(game.league_id)
            if league is None or not league.program_id:
                return set()
            return {(league.program_id, None, game.league_id)}
        if game.division_id:
            return chain_axes(store, "division", game.division_id)
        return set()
    if kind == "club":
        axes = set()
        for team in store.all_teams():
            if team.club_id == record_id:
                axes |= chain_axes(store, "team", team.id)
        return axes
    if kind == "official":
        official = store.get_official(record_id)
        if official is None:
            return set()
        axes = set()
        if official.home_club_id:
            axes |= chain_axes(store, "club", official.home_club_id)
        for assignment in store.assignments_for_official(record_id):
            axes |= chain_axes(store, "game", assignment.game_id)
        return axes
    if kind == "venue":
        # The facility tree reaches the competition tree ONLY through
        # SeasonVenueAccess, which names a Season and never a League. The legacy
        # ``Venue.league_id`` holds a PROGRAM id and names no Season.
        venue = store.get_venue(record_id)
        if venue is None:
            return set()
        axes = set()
        for grant in store.season_venue_access_for_venue(record_id):
            season = store.get_season(grant.season_id)
            if season is not None and season.program_id:
                axes.add((season.program_id, season.id, None))
        if venue.league_id:
            axes.add((venue.league_id, None, None))
        return axes
    if kind == "rink":
        rink = store.get_rink(record_id)
        if rink is None or not rink.venue_id:
            return set()
        return chain_axes(store, "venue", rink.venue_id)
    if kind == "ice_slot":
        slot = store.get_ice_slot(record_id)
        if slot is None or not slot.rink_id:
            return set()
        return chain_axes(store, "rink", slot.rink_id)
    if kind == "organization":
        axes = set()
        for program in store.all_programs():
            if program.operator_organization_id == record_id:
                axes.add((program.id, None, None))
        for venue in store.all_venues():
            if venue.organization_id == record_id:
                axes |= chain_axes(store, "venue", venue.id)
        return axes
    # The two bridge rows are judged by the parent that names their axes.
    if kind == "registration":
        row = store.get_season_team_registration(record_id)
        if row is None:
            return set()
        return chain_axes(store, "league_season", row.league_season_id)
    if kind == "season_venue_access":
        row = store.get_season_venue_access(record_id)
        if row is None:
            return set()
        return chain_axes(store, "season", row.season_id)
    raise AssertionError(f"chain_axes has no rule for kind {kind!r}")


def created_by(store, kind, record_id):
    """The set of actor ids the setup audit trail records as having CREATED
    this record — recomputed here, independently of
    ``ApiService._setup_target_created_by``, for the same reason
    ``chain_programs`` is."""
    keys = {
        "program": {("league_created", "league"), ("program_created", "program")},
        "league": {("level_created", "level"), ("league_created", "league")},
        "season": {("season_created", "season")},
        "league_season": {("league_season_created", "league_season")},
        "division": {("division_created", "division")},
        "team": {("team_created", "team")},
        # Both spellings are live: `add_player` (the interactive create) writes
        # "player_added", the CSV import's `upsert_imported_player` writes
        # "player_created". Derived from what the audit calls actually write,
        # NOT copied from `_SETUP_CREATION_AUDIT_KEYS` -- listing only
        # "player_created" here (as the product's map did) makes every
        # creator-ownership assertion about a hand-entered Player vacuous.
        "player": {("player_added", "player"), ("player_created", "player")},
        "game": {("game_created", "game")},
        "club": {("club_created", "club")},
        "official": {("official_created", "official")},
        "venue": {("venue_created", "venue")},
        "rink": {("rink_created", "rink")},
        "ice_slot": {("ice_slot_created", "ice_slot")},
        "organization": {("organization_created", "organization")},
    }[kind.replace("-", "_")]
    return {row.actor_id for row in store.all_setup_audit()
            if row.entity_id == record_id
            and (row.action, row.entity_type) in keys
            and row.actor_id}


# ==========================================================================
# PART 1 — the predicate itself, on Memory, SQLite and PostgreSQL.
# ==========================================================================

# Every canonical kind the gate accepts, with the store getter that proves the
# row survived a refusal. ``_SETUP_TARGET_KINDS`` in service.py is asserted
# against this list, so a NEW kind cannot be added to the product without
# either appearing here or failing this file.
_KINDS = ("program", "season", "league", "league_season", "division", "team",
          "player", "game", "club", "official", "venue", "rink", "ice_slot",
          "organization")


def _facade_world(api, store, tag, actor):
    """One complete, self-consistent Program tree, created by ``actor``.

    Every kind in ``_KINDS`` gets exactly one record whose Program chain
    resolves to this world's Program and nothing else, so a cross-world
    comparison measures Program scope and only Program scope. The Venue uses
    the LEGACY ``Venue.league_id`` bridge (which holds a Program id) rather
    than a SeasonVenueAccess grant: a granted Venue can never be deleted (the
    grant is itself a delete blocker), so the legacy bridge is the only shape
    that is both LINKED and deletable — which is exactly the blocker's own
    shape."""
    program = api.create_program(f"TA-{tag} Program", "US", "UTC", None, actor)
    season = api.create_season(program["id"], f"TA-{tag} Season",
                               actor_id=actor)
    league = api.create_league(season["id"], f"TA-{tag} League", 0, actor)
    binding = store.league_season_for(league["id"], season["id"])
    division = api.create_division_v2(league["id"], f"TA-{tag} Division", "",
                                      actor)
    club = api.create_club(f"TA-{tag} Club", "", actor)
    team = api.create_team(club["id"], None, f"TA-{tag} Team", actor,
                           league_id=league["id"])
    player = api.create_player(team["id"], f"TA-{tag} Player", "forward",
                               None, None, None, True, actor)
    official = api.create_official(f"TA-{tag} Official", club["id"], actor)
    org = api.create_organization(f"TA-{tag} Org", "", actor)
    venue = api.create_venue(f"TA-{tag} Venue", "", "UTC", org["id"],
                             program["id"], actor)
    rink = api.create_rink(venue["id"], f"TA-{tag} Rink", actor)
    start, end = _slot_times(0 if tag == "A" else 6)
    slot = api.create_ice_slot(rink["id"], start, end, "game", actor)
    for name, value in (("program", program), ("season", season),
                        ("league", league), ("division", division),
                        ("club", club), ("team", team), ("player", player),
                        ("official", official), ("org", org),
                        ("venue", venue), ("rink", rink), ("slot", slot)):
        assert isinstance(value, dict) and "error" not in value, (name, value)
    # A draft Game is minted by the scheduler, not by any setup route, so it is
    # injected — this file's assertions are about the target gate, not about
    # how a draft comes into being.
    from hockey_scheduler.domain.models import Game
    game = Game(id=store.next_id("game"), home_team_id=team["id"],
                start_time=_FUTURE, season_id=season["id"], is_draft=True,
                game_type="exhibition")
    store.add_game(game)
    api.setup._audit("game_created", "game", game.id, actor, None)
    return {
        "program": program["id"], "season": season["id"],
        "league": league["id"], "league_season": binding.id,
        "division": division["id"], "team": team["id"],
        "player": player["id"], "game": game.id, "club": club["id"],
        "official": official["id"], "venue": venue["id"], "rink": rink["id"],
        "ice_slot": slot["id"], "organization": org["id"],
    }


class SetupTargetPredicateParityTest(unittest.TestCase):
    """``ApiService.setup_target_accessible`` on every store.

    The route legs in Part 2 drive real HTTP, which is where the wiring lives.
    This class drives the DECISION in isolation from route/session/wire-format
    concerns, across every kind and every backend, so a change to the predicate
    is caught here even if the HTTP plumbing changes independently."""

    OWNER = "user_target_owner_a"
    ATTACKER = "user_target_attacker_b"

    def _identities(self, api):
        owner = (self.OWNER, Role.LEAGUE_ADMIN, {})
        attacker = (self.ATTACKER, Role.ARENA_MANAGER, {})
        return owner, attacker

    def test_kind_list_matches_the_product(self):
        """A kind added to the gate but not to this file would slip through the
        whole matrix silently."""
        self.assertEqual(
            set(_KINDS), set(ApiService._SETUP_TARGET_KINDS),
            "the kinds this file exercises have drifted from the kinds the "
            "gate accepts; every gated kind needs its own matrix row")

    def test_every_kind_five_case_matrix_on_each_backend(self):
        for backend, store in _backends():
            with self.subTest(backend=backend):
                api = ApiService(store)
                owner, attacker = self._identities(api)
                world_a = _facade_world(api, store, "A", self.OWNER)
                world_b = _facade_world(api, store, "B", self.ATTACKER)

                for kind in _KINDS:
                    target_a, target_b = world_a[kind], world_b[kind]
                    # The active context for a record is its own world's
                    # Program AND Season (#369 re-review). Program-only is no
                    # longer sufficient: a Season-bound row -- the Season
                    # itself, its LeagueSeason, Division, Game, registration or
                    # venue grant -- fails CLOSED when nothing has been
                    # validated to compare its Season against. Each world has
                    # exactly one Season, so this is the world's own context and
                    # nothing is widened; the kinds that are not Season-bound
                    # (Program, League, Team, Player, Club, Official and the
                    # whole facility tree) are unaffected by it.
                    #
                    # No League is selected here on purpose: the two-League
                    # negatives live in their own test below, and "No League"
                    # is the approved Program + active-Season union, so it
                    # leaves this matrix measuring exactly the Program axis it
                    # was written to measure.
                    scope_a = (world_a["program"], world_a["season"])
                    scope_b = (world_b["program"], world_b["season"])

                    # -- PRECONDITION, recomputed from the store --------------
                    # #369 note: the owner's repro states this precondition as
                    # "the record is absent from reads". get_setup_overview_v2
                    # is not Program-scoped until #369 proper (which rebases on
                    # top of this branch), so read-absence is not assertable
                    # here yet. The Program-chain fact below is what the guard
                    # actually decides on, and is asserted in its place.
                    chain = chain_programs(store, kind, target_a)
                    self.assertTrue(
                        chain,
                        f"[{backend}/{kind}] fixture is not distinguishable: "
                        f"the Program-A record has NO Program chain at all, so "
                        f"a refusal would prove nothing about scope")
                    self.assertNotIn(
                        scope_b, chain,
                        f"[{backend}/{kind}] fixture is not distinguishable: "
                        f"the Program-A record resolves to the ATTACKER's "
                        f"active Program {scope_b}")
                    self.assertNotIn(
                        self.ATTACKER, created_by(store, kind, target_a),
                        f"[{backend}/{kind}] fixture is not distinguishable: "
                        f"the attacker CREATED the record it is attacking, so "
                        f"a positive could come from creator ownership")

                    api.set_active_context(*attacker, *scope_b)

                    # -- positive: the target inside the ACTIVE Program -------
                    self.assertIs(
                        api.setup_target_accessible(kind, target_b, *attacker),
                        True,
                        f"[{backend}/{kind}] the caller's OWN Program's record "
                        f"was refused -- the gate is blanket-blocking")
                    # -- foreign: another Program's record -------------------
                    self.assertIs(
                        api.setup_target_accessible(kind, target_a, *attacker),
                        False,
                        f"[{backend}/{kind}] a Program-A record was accepted "
                        f"while Program B was active")
                    # -- nonexistent: the SAME answer, never an oracle -------
                    self.assertIs(
                        api.setup_target_accessible(
                            kind, f"{kind}_no_such_id", *attacker), False,
                        f"[{backend}/{kind}] a nonexistent id did not fail "
                        f"closed")
                    # -- context switch: the same record, the other Program ---
                    api.set_active_context(*attacker, *scope_a)
                    self.assertIs(
                        api.setup_target_accessible(kind, target_a, *attacker),
                        True,
                        f"[{backend}/{kind}] switching to the record's OWN "
                        f"Program did not make it accessible -- the refusal "
                        f"was a blanket block, not a scope decision")
                    api.set_active_context(*attacker, *scope_b)

                # The owner's world is unaffected by any of the above: a
                # refusal is a read-only decision.
                self.assertIsNotNone(store.get_venue(world_a["venue"]))
                _close(store)

    def test_no_active_program_fails_closed(self):
        """Rule 2. An account that has selected nothing has validated nothing
        to compare against, so there is no record it may mutate. Failing OPEN
        here would reinstate the blocker for every fresh account."""
        for backend, store in _backends():
            with self.subTest(backend=backend):
                api = ApiService(store)
                owner, attacker = self._identities(api)
                world_a = _facade_world(api, store, "A", self.OWNER)
                # A scoped role whose subject resolves to nothing has no
                # authorized Program at all, so no context can resolve.
                nobody = ("user_nobody", Role.COACH, {})
                self.assertIsNone(
                    api.context.resolve_with_league(*nobody)[0],
                    f"[{backend}] fixture invalid: this identity DOES resolve "
                    f"a Program, so the test would not exercise rule 2")
                for kind in _KINDS:
                    self.assertIs(
                        api.setup_target_accessible(
                            kind, world_a[kind], *nobody), False,
                        f"[{backend}/{kind}] a caller with NO active Program "
                        f"was granted authority over a record")
                _close(store)

    def test_identityless_caller_is_not_gated(self):
        """Rule 1. ``role is None`` returns None — "no user context, do not
        gate" — not True, so no call site can read a skipped gate as an
        approval. The seeds, the acceptance harnesses and many internal callers
        drive this facade with no identity at all."""
        for backend, store in _backends():
            with self.subTest(backend=backend):
                api = ApiService(store)
                world_a = _facade_world(api, store, "A", self.OWNER)
                for kind in _KINDS:
                    self.assertIsNone(
                        api.setup_target_accessible(kind, world_a[kind],
                                                    None, None, None),
                        f"[{backend}/{kind}] an identity-less internal caller "
                        f"was gated")
                    self.assertIsNone(
                        api.setup_target_accessible(kind, "no_such_id",
                                                    None, None, None),
                        f"[{backend}/{kind}] rule 1 must precede existence")
                _close(store)

    def test_unknown_and_untranslated_kinds_fail_closed(self):
        """v1's vocabulary ("league" for a Program, "level" for a League) MUST
        be translated by the caller. Guessing between the two meanings of
        "league" is the vocabulary trap that produced this defect class, so an
        untranslated word refuses rather than being interpreted."""
        for backend, store in _backends():
            with self.subTest(backend=backend):
                api = ApiService(store)
                _owner, attacker = self._identities(api)
                world_a = _facade_world(api, store, "A", self.OWNER)
                api.set_active_context(*attacker, world_a["program"],
                                       world_a["season"])
                # "level" is v1 for the competition League; the League record
                # here IS accessible under its canonical kind, so a False for
                # "level" is the untranslated-kind refusal and nothing else.
                self.assertIs(api.setup_target_accessible(
                    "league", world_a["league"], *attacker), True)
                for bogus in ("level", "venues", "", "Venue", None):
                    self.assertIs(
                        api.setup_target_accessible(
                            bogus, world_a["league"], *attacker), False,
                        f"[{backend}] kind {bogus!r} did not fail closed")
                _close(store)

    def test_creator_ownership_applies_only_while_unlinked(self):
        """Rules 5 and 6, and the boundary between them.

        An UNLINKED record is the pending-link state a two-step setup flow
        legitimately passes through (create a Venue, then grant a Season access
        to it), and only its creator may act on it. The moment it joins a
        Program's chain the chain becomes the SOLE authority: permanent creator
        authority surviving linking was ruled a blocker in its own right — an
        unrevokable back door no Program admin could see or remove."""
        for backend, store in _backends():
            with self.subTest(backend=backend):
                api = ApiService(store)
                owner, attacker = self._identities(api)
                world_a = _facade_world(api, store, "A", self.OWNER)
                world_b = _facade_world(api, store, "B", self.ATTACKER)
                api.set_active_context(*owner, world_a["program"],
                                       world_a["season"])
                api.set_active_context(*attacker, world_b["program"],
                                       world_b["season"])

                draft = api.create_venue("TA Pending Venue", "", "UTC", None,
                                         None, self.OWNER)
                self.assertEqual(chain_programs(store, "venue", draft["id"]),
                                 set(), "fixture: the draft must be UNLINKED")

                # Its creator may act on it, from any context...
                self.assertIs(api.setup_target_accessible(
                    "venue", draft["id"], *owner), True,
                    f"[{backend}] the creator of an unlinked draft cannot even "
                    f"delete it -- the pending-link flow is broken")
                # ...and nobody else may, even holding the same role.
                self.assertIs(api.setup_target_accessible(
                    "venue", draft["id"], *attacker), False,
                    f"[{backend}] another account claimed an unlinked draft "
                    f"Venue")

                # Now LINK it to the attacker's Program. Creator authority must
                # NOT survive: the chain, and only the chain, now decides.
                api.grant_season_venue_access(world_b["season"], draft["id"],
                                              self.ATTACKER)
                self.assertEqual(
                    chain_programs(store, "venue", draft["id"]),
                    {world_b["program"]},
                    "fixture: the draft must now be linked to Program B only")
                self.assertIs(api.setup_target_accessible(
                    "venue", draft["id"], *attacker), True,
                    f"[{backend}] the linking Program lost access to the Venue "
                    f"it linked")
                self.assertIs(api.setup_target_accessible(
                    "venue", draft["id"], *owner), False,
                    f"[{backend}] PERMANENT CREATOR AUTHORITY SURVIVED "
                    f"LINKING: the account that first typed this Venue in "
                    f"keeps delete rights over it inside another Program -- an "
                    f"unrevokable back door no Program admin can see")
                _close(store)

    def test_a_revoked_grant_still_links(self):
        """Revoking deactivates rather than deletes, and a revoked grant is
        durable evidence the Venue was used by that Program. Treating it as "no
        link" would let a Program revoke its own access and thereby turn a
        shared Venue into an unlinked record an unrelated account could claim by
        creator ownership — a privilege GAIN produced by giving something up."""
        for backend, store in _backends():
            with self.subTest(backend=backend):
                api = ApiService(store)
                owner, attacker = self._identities(api)
                world_a = _facade_world(api, store, "A", self.OWNER)
                world_b = _facade_world(api, store, "B", self.ATTACKER)
                api.set_active_context(*attacker, world_b["program"],
                                       world_b["season"])

                shared = api.create_venue("TA Revoked Venue", "", "UTC", None,
                                          None, self.ATTACKER)
                grant = api.grant_season_venue_access(
                    world_a["season"], shared["id"], self.OWNER)
                api.revoke_season_venue_access(grant["id"], self.OWNER)
                self.assertEqual(
                    chain_programs(store, "venue", shared["id"]),
                    {world_a["program"]},
                    "fixture: the only grant is revoked but still links")
                self.assertIs(api.setup_target_accessible(
                    "venue", shared["id"], *attacker), False,
                    f"[{backend}] a REVOKED grant decayed into 'unlinked', "
                    f"handing the Venue back to its creator inside another "
                    f"Program")
                _close(store)

    def test_a_dangling_chain_never_becomes_unlinked(self):
        """A record whose chain EXISTS but cannot be resolved is treated as
        linked-and-unmatched, never as unlinked. Corrupting data must not be a
        route into the permissive creator branch."""
        for backend, store in _backends():
            with self.subTest(backend=backend):
                api = ApiService(store)
                _owner, attacker = self._identities(api)
                world_b = _facade_world(api, store, "B", self.ATTACKER)
                api.set_active_context(*attacker, world_b["program"],
                                       world_b["season"])

                orphan = api.create_team(None, None, "TA Orphan Team",
                                         self.ATTACKER,
                                         program_id=world_b["program"])
                team = store.get_team(orphan["id"])
                team.program_id = None
                team.league_id = "league_vanished"   # dangling edge
                store.save_team(team)
                self.assertEqual(
                    chain_programs(store, "team", orphan["id"]), set(),
                    "fixture: the chain must resolve to no Program")
                self.assertIn(self.ATTACKER,
                              created_by(store, "team", orphan["id"]),
                              "fixture: the caller IS the creator, so a True "
                              "here would come from the creator branch")
                self.assertIs(api.setup_target_accessible(
                    "team", orphan["id"], *attacker), False,
                    f"[{backend}] a Team with a DANGLING League edge fell "
                    f"through to creator ownership -- breaking data is a "
                    f"privilege escalation")
                _close(store)


class VenueGrantablePredicateTest(unittest.TestCase):
    """The facility-tree EXCEPTION (``setup_venue_grantable``), on every store.

    It governs ONE argument: the Venue end of the venue-access grant. The
    generic rule deadlocked shared arenas — once Program A held the grant the
    Venue was linked to A, so every other Program failed the active-context
    check and could never obtain the grant that would have made it accessible.
    The capability failed on its own first use."""

    OWNER = "user_grant_owner_a"
    ATTACKER = "user_grant_attacker_b"

    def _fixture(self, store):
        api = ApiService(store)
        owner = (self.OWNER, Role.LEAGUE_ADMIN, {})
        attacker = (self.ATTACKER, Role.LEAGUE_ADMIN, {})
        world_a = _facade_world(api, store, "A", self.OWNER)
        world_b = _facade_world(api, store, "B", self.ATTACKER)
        api.set_active_context(*owner, world_a["program"],
                               world_a["season"])
        api.set_active_context(*attacker, world_b["program"],
                               world_b["season"])
        return api, owner, attacker, world_a, world_b

    def test_an_established_arena_is_shared_across_programs(self):
        """Clause 1, and the reason the exception exists at all."""
        for backend, store in _backends():
            with self.subTest(backend=backend):
                api, _owner, attacker, world_a, _b = self._fixture(store)
                self.assertEqual(
                    chain_programs(store, "venue", world_a["venue"]),
                    {world_a["program"]},
                    "fixture: the arena must be linked to Program A only")
                self.assertIs(
                    api.setup_venue_grantable(world_a["venue"], *attacker),
                    True,
                    f"[{backend}] a shared arena already linked to another "
                    f"Program was refused -- venue sharing deadlocks on its "
                    f"first use")
                # ...while the GENERIC rule refuses that same Venue, which is
                # precisely why this predicate exists and is scoped to one
                # argument. If this ever passes, the "exception" has silently
                # become the rule everywhere.
                self.assertIs(
                    api.setup_target_accessible("venue", world_a["venue"],
                                                *attacker), False,
                    f"[{backend}] the generic rule no longer refuses a foreign "
                    f"Venue, so the exception is indistinguishable from it")
                _close(store)

    # Clause 2 was genuinely broken when this test was written: the predicate
    # tested `self._venue_program_ids(venue)` directly, and that helper returns
    # a (ids, saw_link) 2-tuple -- always truthy -- so the creator check below
    # it was dead code and EVERY Venue was grantable. The assertion was left
    # un-weakened and the product line fixed instead.
    def test_another_accounts_unlinked_private_draft_is_not_grantable(self):
        """Clause 2, the load-bearing one — see the comment above."""
        for backend, store in _backends():
            with self.subTest(backend=backend):
                api, _owner, attacker, _a, _b = self._fixture(store)
                private = api.create_venue("TA Private Draft", "", "UTC", None,
                                           None, self.OWNER)
                self.assertEqual(
                    chain_programs(store, "venue", private["id"]), set(),
                    "fixture: the draft must be linked to nothing")
                self.assertNotIn(
                    self.ATTACKER, created_by(store, "venue", private["id"]),
                    "fixture: a DIFFERENT account must have created it")
                try:
                    self.assertIs(
                        api.setup_venue_grantable(private["id"], *attacker),
                        False,
                        f"[{backend}] another operator's never-linked private "
                        f"arena was offered up as grantable")
                finally:
                    _close(store)

    def test_the_creator_may_finish_its_own_pending_grant(self):
        """Clause 2's other half, and the whole reason clause 2 is not "no
        unlinked Venue, ever": create a Venue, then grant a Season access to it
        is a legitimate two-step flow, and its own author must be able to
        finish it."""
        for backend, store in _backends():
            with self.subTest(backend=backend):
                api, owner, _attacker, _a, _b = self._fixture(store)
                private = api.create_venue("TA Own Draft", "", "UTC", None,
                                           None, self.OWNER)
                self.assertEqual(
                    chain_programs(store, "venue", private["id"]), set())
                self.assertIn(self.OWNER,
                              created_by(store, "venue", private["id"]))
                self.assertIs(
                    api.setup_venue_grantable(private["id"], *owner), True,
                    f"[{backend}] the creator cannot grant its own pending "
                    f"Venue -- the two-step setup flow cannot complete")
                _close(store)

    def test_grantable_edges_fail_closed(self):
        """Nonexistent and no-active-Program both refuse; no identity at all is
        ungated, exactly like the generic predicate."""
        for backend, store in _backends():
            with self.subTest(backend=backend):
                api, _owner, attacker, world_a, _b = self._fixture(store)
                self.assertIs(
                    api.setup_venue_grantable("venue_no_such", *attacker),
                    False, f"[{backend}] a nonexistent Venue was grantable")
                self.assertIs(
                    api.setup_venue_grantable(None, *attacker), False,
                    f"[{backend}] a null Venue was grantable")
                self.assertIs(
                    api.setup_venue_grantable(world_a["venue"],
                                              "user_nobody", Role.COACH, {}),
                    False, f"[{backend}] no active Program must fail closed")
                self.assertIsNone(
                    api.setup_venue_grantable(world_a["venue"], None, None,
                                              None),
                    f"[{backend}] an identity-less caller must not be gated")
                _close(store)


# ==========================================================================
# PART 1b — the SEASON and LEAGUE axes at the predicate, on every store.
#
# Part 1 varies the PROGRAM and holds everything else fixed, which is exactly
# the blind spot the re-review found: the gate resolved all three axes and then
# authorized on the Program alone, so a same-Program record in another League
# or another Season sailed through. Everything below therefore holds the
# Program CONSTANT and varies exactly one of the other two axes, so a refusal
# can only be about the axis under test -- the Program ceiling provably cannot
# explain any of it.
# ==========================================================================

#: The two Seasons and the two Leagues every fixture in this part is built
#: from. Both Leagues are bound to BOTH Seasons, so "switch the League" and
#: "switch the Season" are each a single, legal context move -- a fixture where
#: League B existed only in Season B would make every negative ambiguous
#: between the two axes.
_AXIS_KEYS = ("A", "B")


def _axis_world(api, store, actor):
    """ONE Program, TWO Seasons, TWO Leagues, all four bindings, and one record
    of every applicable kind at each corner.

    Returns a dict whose League-keyed entries are ``{league_key: id}``,
    Season-keyed entries ``{season_key: id}`` and corner-keyed entries
    ``{(league_key, season_key): id}``.

    The Venues here are deliberately built the OPPOSITE way round from Part 1's:
    no legacy ``Venue.league_id`` at all, only a SeasonVenueAccess grant. The
    legacy bridge holds a PROGRAM id and names no Season, so a Venue carrying
    one is not Season-bound and could never demonstrate the facility tree's
    Season axis -- it would pass these tests with the Season comparison
    deleted."""
    program = api.create_program("AX Program", "US", "UTC", None, actor)
    world = {"program": program["id"], "season": {}, "league": {},
             "league_season": {}, "division": {}, "team": {}, "player": {},
             "club": {}, "official": {}, "game": {}, "registration": {},
             "venue": {}, "rink": {}, "ice_slot": {},
             "season_venue_access": {}}
    for key in _AXIS_KEYS:
        world["season"][key] = api.create_season(
            program["id"], f"AX Season {key}", actor_id=actor)["id"]
    for key in _AXIS_KEYS:
        league = api.create_league(world["season"]["A"], f"AX League {key}",
                                   0, actor)
        world["league"][key] = league["id"]
        # Bind the same PERMANENT League to Season B as well. There is no HTTP
        # route for binding an existing League to a further Season (only
        # `create_league`, which mints a new one), so the setup service is
        # called directly -- a fixture step, exactly like the injected draft
        # Game below.
        api.setup.create_league_season(league["id"], world["season"]["B"],
                                       actor)
    for lk in _AXIS_KEYS:
        world["club"][lk] = api.create_club(f"AX Club {lk}", "", actor)["id"]
        world["team"][lk] = api.create_team(
            world["club"][lk], None, f"AX Team {lk}", actor,
            league_id=world["league"][lk])["id"]
        world["player"][lk] = api.create_player(
            world["team"][lk], f"AX Player {lk}", "forward", None, None, None,
            True, actor)["id"]
        world["official"][lk] = api.create_official(
            f"AX Official {lk}", world["club"][lk], actor)["id"]
    for sk in _AXIS_KEYS:
        # A Venue linked ONLY by a grant: its whole chain is (Program, Season).
        venue = api.create_venue(f"AX Venue {sk}", "", "UTC", None, None,
                                 actor)
        grant = api.grant_season_venue_access(world["season"][sk],
                                              venue["id"], actor)
        rink = api.create_rink(venue["id"], f"AX Rink {sk}", actor)
        start, end = _slot_times(30 if sk == "A" else 40)
        slot = api.create_ice_slot(rink["id"], start, end, "game", actor)
        world["venue"][sk] = venue["id"]
        world["season_venue_access"][sk] = grant["id"]
        world["rink"][sk] = rink["id"]
        world["ice_slot"][sk] = slot["id"]
    from hockey_scheduler.domain.models import Game
    for lk in _AXIS_KEYS:
        for sk in _AXIS_KEYS:
            binding = store.league_season_for(world["league"][lk],
                                              world["season"][sk])
            assert binding is not None, ("fixture: both Leagues must be bound "
                                         "to both Seasons", lk, sk)
            world["league_season"][(lk, sk)] = binding.id
            world["division"][(lk, sk)] = api.create_division_v2(
                world["league"][lk], f"AX Div {lk}{sk}", "", actor,
                season_id=world["season"][sk])["id"]
            world["registration"][(lk, sk)] = api.register_team_for_season(
                world["season"][sk], world["team"][lk], None, actor,
                league_id=world["league"][lk])["id"]
            game = Game(id=store.next_id("game"),
                        home_team_id=world["team"][lk],
                        start_time=_FUTURE, season_id=world["season"][sk],
                        league_id=world["league"][lk], is_draft=True,
                        game_type="exhibition")
            store.add_game(game)
            api.setup._audit("game_created", "game", game.id, actor, None)
            world["game"][(lk, sk)] = game.id
    for name, value in world.items():
        if isinstance(value, dict):
            for key, rid in value.items():
                assert isinstance(rid, str) and rid, (name, key, rid)
    return world


#: (kind, corner-picker) for every kind whose chain really names a LEAGUE.
#: The picker takes ``(world, league_key)`` and returns the record for that
#: League in Season A, so the two records differ in the League and NOTHING
#: else.
_LEAGUE_BOUND = (
    ("league", lambda w, lk: w["league"][lk]),
    ("league_season", lambda w, lk: w["league_season"][(lk, "A")]),
    ("division", lambda w, lk: w["division"][(lk, "A")]),
    ("team", lambda w, lk: w["team"][lk]),
    ("player", lambda w, lk: w["player"][lk]),
    ("club", lambda w, lk: w["club"][lk]),
    ("official", lambda w, lk: w["official"][lk]),
    ("game", lambda w, lk: w["game"][(lk, "A")]),
)

#: (kind, corner-picker) for every kind whose chain really names a SEASON.
#: The picker takes ``(world, season_key)``; where the kind also carries a
#: League it is always League A, so the two records differ in the Season and
#: NOTHING else. The facility tree is here because SeasonVenueAccess is a real
#: Season link -- and it is NOT in the League table above, because the facility
#: tree has no League axis and none is invented for it.
_SEASON_BOUND = (
    ("season", lambda w, sk: w["season"][sk]),
    ("league_season", lambda w, sk: w["league_season"][("A", sk)]),
    ("division", lambda w, sk: w["division"][("A", sk)]),
    ("game", lambda w, sk: w["game"][("A", sk)]),
    ("venue", lambda w, sk: w["venue"][sk]),
    ("rink", lambda w, sk: w["rink"][sk]),
    ("ice_slot", lambda w, sk: w["ice_slot"][sk]),
)

#: Kinds that carry NO Season axis at all, used by the Program-only test: they
#: must stay reachable from a Program-only context, or a brand-new Program with
#: no Season selected becomes unmanageable.
_SEASON_FREE = ("program", "league", "team", "player", "club", "official")


class SetupTargetAxisParityTest(unittest.TestCase):
    """The persisted tuple is Program AND Season AND League (#369 re-review).

    The reported blocker, verbatim: "``setup_target_accessible()`` resolves all
    three axes, discards ``_season`` and ``_league``, reduces every record to
    Program ids, then authorizes on ``program.id in program_ids``. With Program
    P / Season S / League A persisted, ``POST
    /api/v2/setup/player/<League-B-player>/update`` returned 200, renamed the
    League-B Player, and persisted the change; a nonexistent Player returned
    404. Both Players were in the same Program."

    Every fixture below is inside ONE Program, so no assertion here can be
    satisfied by the Program ceiling. The caller created nothing, so no
    positive can be creator ownership."""

    OWNER = "user_axis_owner"
    CALLER = "user_axis_caller"

    def _fixture(self, store):
        api = ApiService(store)
        caller = (self.CALLER, Role.LEAGUE_ADMIN, {})
        world = _axis_world(api, store, self.OWNER)
        return api, caller, world

    def _assert_only_the_league_differs(self, store, kind, victim, world,
                                        backend):
        """Precondition: this record is in the caller's OWN Program and OWN
        Season, and differs from the selection in the LEAGUE alone."""
        axes = chain_axes(store, kind, victim)
        self.assertTrue(
            axes,
            f"[{backend}/{kind}] fixture is not distinguishable: the victim "
            f"has no chain at all, so refusing it would prove nothing")
        self.assertEqual(
            {t[0] for t in axes}, {world["program"]},
            f"[{backend}/{kind}] fixture is not distinguishable: the victim is "
            f"not in the caller's own Program, so the PROGRAM ceiling alone "
            f"could explain the refusal")
        self.assertEqual(
            {t[2] for t in axes}, {world["league"]["B"]},
            f"[{backend}/{kind}] fixture is not distinguishable: the victim's "
            f"League is not exactly the unselected League B")
        self.assertLessEqual(
            {t[1] for t in axes} - {None}, {world["season"]["A"]},
            f"[{backend}/{kind}] fixture is not distinguishable: the victim "
            f"names a Season other than the selected one, so the SEASON axis "
            f"could explain the refusal instead of the League")

    def _assert_only_the_season_differs(self, store, kind, victim, world,
                                        backend):
        axes = chain_axes(store, kind, victim)
        self.assertTrue(
            axes,
            f"[{backend}/{kind}] fixture is not distinguishable: the victim "
            f"has no chain at all")
        self.assertEqual(
            {t[0] for t in axes}, {world["program"]},
            f"[{backend}/{kind}] fixture is not distinguishable: the victim is "
            f"not in the caller's own Program")
        self.assertEqual(
            {t[1] for t in axes}, {world["season"]["B"]},
            f"[{backend}/{kind}] fixture is not distinguishable: the victim's "
            f"Season is not exactly the unselected Season B")
        # The League axis provably cannot explain these refusals: they are all
        # driven with NO League selected, which is the approved Program +
        # active-Season union and compares no League at all.

    def test_league_axis_negative_for_every_league_bound_kind(self):
        """At League A, a League-B record in the SAME Program and the SAME
        Season is refused -- and selecting League B makes the very same call on
        the very same record succeed."""
        for backend, store in _backends():
            with self.subTest(backend=backend):
                api, caller, w = self._fixture(store)
                program, season_a = w["program"], w["season"]["A"]
                for kind, pick in _LEAGUE_BOUND:
                    mine, victim = pick(w, "A"), pick(w, "B")
                    self._assert_only_the_league_differs(
                        store, kind, victim, w, backend)
                    self.assertNotIn(
                        self.CALLER, created_by(store, kind, victim),
                        f"[{backend}/{kind}] fixture: the caller created the "
                        f"record it is attacking")

                    api.set_active_context(*caller, program, season_a,
                                           w["league"]["A"])
                    self.assertIs(
                        api.setup_target_accessible(kind, mine, *caller), True,
                        f"[{backend}/{kind}] the caller's OWN League's record "
                        f"was refused -- a blanket block, not a scope decision")
                    self.assertIs(
                        api.setup_target_accessible(kind, victim, *caller),
                        False,
                        f"[{backend}/{kind}] THE BLOCKER: a League-B record "
                        f"was authorized while League A was persisted. Same "
                        f"Program, same Season -- only the League differs, and "
                        f"the League was discarded")
                    self.assertIs(
                        api.setup_target_accessible(
                            kind, f"{kind}_ax_absent", *caller), False,
                        f"[{backend}/{kind}] a nonexistent id did not fail "
                        f"closed")

                    # Switching the EXACT missing axis, and nothing else.
                    api.set_active_context(*caller, program, season_a,
                                           w["league"]["B"])
                    self.assertIs(
                        api.setup_target_accessible(kind, victim, *caller),
                        True,
                        f"[{backend}/{kind}] selecting the record's own League "
                        f"did not make it accessible -- the refusal was a "
                        f"blanket block rather than a League decision")
                _close(store)

    def test_season_axis_negative_for_every_season_bound_kind(self):
        """At Season A (and NO League, so the League axis provably plays no
        part), a Season-B record in the SAME Program is refused -- and
        selecting Season B makes the same call succeed."""
        for backend, store in _backends():
            with self.subTest(backend=backend):
                api, caller, w = self._fixture(store)
                program = w["program"]
                for kind, pick in _SEASON_BOUND:
                    mine, victim = pick(w, "A"), pick(w, "B")
                    self._assert_only_the_season_differs(
                        store, kind, victim, w, backend)

                    api.set_active_context(*caller, program, w["season"]["A"],
                                           None)
                    self.assertIs(
                        api.setup_target_accessible(kind, mine, *caller), True,
                        f"[{backend}/{kind}] the caller's OWN Season's record "
                        f"was refused")
                    self.assertIs(
                        api.setup_target_accessible(kind, victim, *caller),
                        False,
                        f"[{backend}/{kind}] a Season-B record was authorized "
                        f"while Season A was persisted. Same Program -- only "
                        f"the Season differs, and the Season was discarded")

                    api.set_active_context(*caller, program, w["season"]["B"],
                                           None)
                    self.assertIs(
                        api.setup_target_accessible(kind, victim, *caller),
                        True,
                        f"[{backend}/{kind}] selecting the record's own Season "
                        f"did not make it accessible")
                _close(store)

    def test_no_league_is_the_program_plus_active_season_union(self):
        """Explicit "No League" is a first-class selection meaning the approved
        Program + ACTIVE-SEASON union: every League inside that already
        validated Program/Season, and nothing outside it.

        Both halves matter. Permitting nothing would make No League a dead
        context (it is the state every operator starts in). Permitting
        everything would make it a way to opt out of the Season ceiling."""
        for backend, store in _backends():
            with self.subTest(backend=backend):
                api, caller, w = self._fixture(store)
                api.set_active_context(*caller, w["program"],
                                       w["season"]["A"], None)
                self.assertIsNone(
                    api.context.resolve_with_league(*caller)[2],
                    f"[{backend}] fixture: no League may be resolved, or this "
                    f"is not the No-League state at all")

                # -- the UNION half: every League inside the active Season ---
                for kind, pick in _LEAGUE_BOUND:
                    for lk in _AXIS_KEYS:
                        self.assertIs(
                            api.setup_target_accessible(kind, pick(w, lk),
                                                        *caller), True,
                            f"[{backend}/{kind}] No League refused League {lk} "
                            f"inside the active Season -- No League is not the "
                            f"union it is defined to be")

                # -- the ONLY half: nothing outside the active Season --------
                for kind, pick in _SEASON_BOUND:
                    self.assertIs(
                        api.setup_target_accessible(kind, pick(w, "B"),
                                                    *caller), False,
                        f"[{backend}/{kind}] No League reached OUTSIDE the "
                        f"active Season -- selecting no League became a way "
                        f"to opt out of the Season ceiling")
                # ...including a League-bound row that lives in Season B, which
                # is the exact combination "any League" would wave through.
                for kind, corner in (("league_season", "league_season"),
                                     ("division", "division"),
                                     ("game", "game")):
                    for lk in _AXIS_KEYS:
                        self.assertIs(
                            api.setup_target_accessible(
                                kind, w[corner][(lk, "B")], *caller), False,
                            f"[{backend}/{kind}] a League-{lk} row in Season B "
                            f"was authorized from a Season-A No-League context")
                _close(store)

    def test_program_only_fails_closed_for_every_season_bound_kind(self):
        """A Program-only context has validated no Season, so there is nothing
        to compare a Season-bound row against: it fails CLOSED.

        The complement is asserted in the same breath, because failing closed
        for EVERYTHING would pass this test while making a brand-new Program
        (which has no Season yet) unmanageable: the permanent, Season-free
        kinds stay reachable."""
        for backend, store in _backends():
            with self.subTest(backend=backend):
                api, caller, w = self._fixture(store)
                api.set_active_context(*caller, w["program"], None, None)
                program, season = api.context.resolve_with_league(*caller)[:2]
                self.assertIsNotNone(program, f"[{backend}] fixture: the "
                                              f"Program must resolve")
                self.assertIsNone(
                    season,
                    f"[{backend}] fixture: a Season resolved anyway, so this "
                    f"is not the Program-only state under test")

                for kind, pick in _SEASON_BOUND:
                    for sk in _AXIS_KEYS:
                        self.assertIs(
                            api.setup_target_accessible(kind, pick(w, sk),
                                                        *caller), False,
                            f"[{backend}/{kind}] a Program-only caller was "
                            f"granted authority over a Season-bound row in "
                            f"Season {sk} -- nothing was validated to compare "
                            f"its Season against")

                free = {"program": w["program"], "league": w["league"]["A"],
                        "team": w["team"]["A"], "player": w["player"]["A"],
                        "club": w["club"]["A"], "official": w["official"]["A"]}
                for kind in _SEASON_FREE:
                    self.assertIs(
                        api.setup_target_accessible(kind, free[kind], *caller),
                        True,
                        f"[{backend}/{kind}] a PERMANENT, Season-free record "
                        f"was refused to a Program-only caller -- a Program "
                        f"with no Season yet would be unmanageable")
                _close(store)

    def test_the_facility_tree_has_a_season_axis_and_no_league_axis(self):
        """Both halves of the owner's facility ruling, in one place.

        The Season axis is REAL: it arrives through SeasonVenueAccess, the only
        join between the facility tree and the competition tree. The League axis
        does NOT exist and must not be invented: with League A selected, a Venue
        (and its Rink and IceSlot) inside the active Season stays reachable,
        because no link it carries names a League to compare."""
        for backend, store in _backends():
            with self.subTest(backend=backend):
                api, caller, w = self._fixture(store)
                for kind in ("venue", "rink", "ice_slot"):
                    axes = chain_axes(store, kind, w[kind]["A"])
                    self.assertEqual(
                        {t[2] for t in axes}, {None},
                        f"[{backend}/{kind}] fixture: the facility record has "
                        f"acquired a League, so this proves nothing")
                    self.assertEqual(
                        {t[1] for t in axes}, {w["season"]["A"]},
                        f"[{backend}/{kind}] fixture: the facility record is "
                        f"not bound to exactly Season A")

                for league_key in _AXIS_KEYS:
                    api.set_active_context(*caller, w["program"],
                                           w["season"]["A"],
                                           w["league"][league_key])
                    for kind in ("venue", "rink", "ice_slot"):
                        self.assertIs(
                            api.setup_target_accessible(kind, w[kind]["A"],
                                                        *caller), True,
                            f"[{backend}/{kind}] a League was INVENTED for the "
                            f"facility tree: selecting League {league_key} "
                            f"changed whether an arena in the active Season is "
                            f"reachable")
                        self.assertIs(
                            api.setup_target_accessible(kind, w[kind]["B"],
                                                        *caller), False,
                            f"[{backend}/{kind}] a facility record granted "
                            f"only to Season B was reachable from Season A")
                _close(store)


# ==========================================================================
# PART 2 — every gated route, over real authenticated HTTP, on both API
# versions and every backend.
#
# Part 1 proves the DECISION. This proves the WIRING: 22 separate call sites
# route through one shared gate, and a site that forgot to call it, or called
# it with the wrong kind, or called it AFTER the facade, is invisible to a
# facade-level test. The response bytes and the store are both inspected,
# because the reported blocker leaked the foreign record's name in the
# response body before it deleted the row.
# ==========================================================================

# The label the FACADE's own not-found uses for each gated kind. Deliberately
# duplicated here rather than imported from the handler: this is the wire
# contract a client sees, and a silent relabelling in server.py would make an
# inaccessible record distinguishable from a nonexistent one again.
_WORD = {
    "program": "Program", "season": "Season", "league": "League",
    "league_season": "LeagueSeason", "division": "Division", "team": "Team",
    "player": "Player", "game": "Game", "club": "Club",
    "official": "Official", "venue": "Venue", "rink": "Rink",
    "ice_slot": "Ice slot", "organization": "Organization",
    "registration": "Registration",
    "season_venue_access": "Season-venue access",
}

# Kinds whose creation is recorded in the setup audit trail. The two bridge
# rows are not (they are judged by their parent), so the "the attacker did not
# create this" precondition does not apply to them.
_CREATABLE = frozenset(_WORD) - {"registration", "season_venue_access"}

# Every setup table, for the no-mutation row-id snapshot.
_TABLES = (
    ("programs", "all_programs"), ("seasons", "all_seasons"),
    ("leagues", "all_leagues"), ("league_seasons", "all_league_seasons"),
    ("divisions", "all_divisions"), ("clubs", "all_clubs"),
    ("teams", "all_teams"), ("players", "all_players"),
    ("games", "all_games"), ("officials", "all_officials"),
    ("organizations", "all_organizations"), ("venues", "all_venues"),
    ("rinks", "all_rinks"), ("ice_slots", "all_ice_slots"),
    ("registrations", "all_season_team_registrations"),
    ("venue_access", "all_season_venue_access"),
)

# Error codes a positive case may legitimately carry: the target gate ALLOWED
# the call and some later, non-authorization rule stopped it. Never
# "not_found", which is the refusal itself.
_NON_AUTH_CODES = frozenset({"has_dependencies", "validation_error"})


class SetupTargetRouteMatrixTest(unittest.TestCase):
    """The five-case matrix against every gated route on both API versions.

    Three identities, and the differences between them are what make a refusal
    meaningful:

    * ``owner_a`` (League Admin) creates every Program-A record and never acts;
    * ``owner_b`` (League Admin) does the same for Program B;
    * ``attacker`` (League Admin) creates NOTHING and drives every call in the
      matrix, switching only its ACTIVE Program.

    So no positive here can be explained by creator ownership, and no refusal
    by the attacker lacking the role: it holds MANAGE_SETUP and MANAGE_ARENA,
    the two permissions every route in the table requires. The only variable is
    the active Program. (The owner's verbatim Arena-Manager repro is a separate
    test below.)
    """

    @classmethod
    def setUpClass(cls):
        srv = __import__("hockey_scheduler.web.server", fromlist=["x"])
        cls.srv = srv
        os.environ.pop("DATABASE_URL", None)   # class baseline is Memory
        srv.STATE.reset(seed=False)
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), srv.Handler)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever,
                                      daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.thread.join(timeout=5)

    # -- backend steering ---------------------------------------------------
    def _reset_backend(self, database_url, backend):
        """Rebuild the live ``STATE`` on the named backend for this one test.

        ``create_store()`` resolves the backend purely from ``DATABASE_URL`` at
        call time and ``DemoState.reset()`` calls it with no override, so
        setting the env var only around this one ``reset()`` is enough to steer
        it. The value is restored immediately, and a cleanup flips ``STATE``
        back to a fresh Memory store the moment this test ends, so no later
        test inherits a SQL-backed ``STATE``."""
        prev = os.environ.get("DATABASE_URL")

        def _set(url):
            if url is None:
                os.environ.pop("DATABASE_URL", None)
            else:
                os.environ["DATABASE_URL"] = url

        _set(database_url)
        try:
            self.srv.STATE.reset(seed=False)
        finally:
            _set(prev)

        def _restore_memory():
            _set(None)
            try:
                self.srv.STATE.reset(seed=False)
            finally:
                _set(prev)

        self.addCleanup(_restore_memory)

        # Prove the store REALLY is the one this variant claims BEFORE
        # asserting anything about the guard. Without this the whole matrix is
        # theatre -- the defect this repo already hit once was a write test
        # that believed it covered PostgreSQL while silently running on
        # InMemoryStore, and it looked green the entire time.
        live = self.srv.STATE.api.store
        if backend == "memory":
            self.assertIsInstance(
                live, InMemoryStore,
                f"expected the in-memory store, got {type(live).__name__}")
        else:
            self.assertIsInstance(
                live, SqlStore,
                f"the {backend} variant is not running on a SQL store at all "
                f"-- got {type(live).__name__}; it would silently re-prove the "
                f"Memory case")
            self.assertEqual(
                live.backend, backend,
                f"the {backend} variant is running on a {live.backend!r}-"
                f"backed SqlStore")

    # -- HTTP plumbing ------------------------------------------------------
    def _client(self):
        return urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(CookieJar()))

    def _raw(self, opener, method, path, body=None):
        """(status, raw bytes). The byte-identity claim cannot be tested
        through a decoded body: two equal dicts say nothing about key order,
        spacing or a stray field, any of which is an oracle to a reader of the
        socket."""
        url = f"http://127.0.0.1:{self.port}{path}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with opener.open(req) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def _req(self, opener, method, path, body=None):
        status, raw = self._raw(opener, method, path, body)
        return status, json.loads(raw or b"{}")

    def _post(self, opener, path, body):
        status, raw = self._raw(opener, "POST", path, body)
        return status, json.loads(raw or b"{}"), raw

    def _account(self, username, role):
        """Create the login directly on the live store (the HTTP account routes
        need an operator session that does not exist yet on a clean slate) and
        return (opener, user_id)."""
        account = self.srv.STATE.api.accounts.create_account(
            username, "targetauth-pw", role, scope={},
            actor_id="test_seed")
        opener = self._client()
        status, resp = self._req(opener, "POST", "/api/auth/login",
                                 {"username": username,
                                  "password": "targetauth-pw"})
        self.assertEqual(status, 200, (username, resp))
        return opener, account.id

    def _select(self, opener, program_id, season_id=None, league_id=None):
        """Persist all three context axes and PROVE what actually landed.

        The three axes are asserted back out of the response rather than
        assumed: ``set_with_league`` is allowed to move the Season when a
        League is selected (#364's canonical resolution), and a matrix that
        believed it had selected (S, A) while the server had persisted (S', A)
        would be measuring a context nobody holds."""
        body = {"program_id": program_id}
        if season_id is not None:
            body["season_id"] = season_id
        if league_id is not None:
            body["league_id"] = league_id
        status, resp = self._req(opener, "POST", "/api/context", body)
        self.assertEqual(status, 200, (body, resp))
        self.assertEqual(resp.get("program", {}).get("id"), program_id, resp)
        if season_id is not None:
            self.assertEqual((resp.get("season") or {}).get("id"), season_id,
                             ("the persisted Season is not the one selected",
                              body, resp))
        if league_id is not None:
            self.assertEqual((resp.get("league") or {}).get("id"), league_id,
                             ("the persisted League is not the one selected",
                              body, resp))
        return resp

    def _ok(self, opener, path, body, why=""):
        """A fixture call that must succeed. A fixture that half-built itself
        would make the matrix below assert against a shape nobody has."""
        status, resp, _ = self._post(opener, path, body)
        self.assertEqual(status, 200, (why or path, body, resp))
        self.assertNotIn("error", resp, (why or path, resp))
        return resp

    # -- store snapshots ----------------------------------------------------
    def _snapshot(self):
        store = self.srv.STATE.api.store
        snap = {name: {r.id for r in getattr(store, getter)()}
                for name, getter in _TABLES}
        snap["setup_audit_rows"] = len(store.all_setup_audit())
        return snap

    def _row(self, kind, record_id):
        """A field-level snapshot of one target row, so an in-place edit that
        does not change any row-id SET (player/update, season/archive) is
        caught by the no-mutation case too."""
        store = self.srv.STATE.api.store
        getter = {
            "program": store.get_program, "season": store.get_season,
            "league": store.get_league,
            "league_season": store.get_league_season,
            "division": store.get_division, "team": store.get_team,
            "player": store.get_player, "game": store.get_game,
            "club": store.get_club, "official": store.get_official,
            "venue": store.get_venue, "rink": store.get_rink,
            "ice_slot": store.get_ice_slot,
            "organization": store.get_organization,
            "registration": store.get_season_team_registration,
            "season_venue_access": store.get_season_venue_access,
        }[kind]
        row = getter(record_id)
        return None if row is None else repr(sorted(vars(row).items(),
                                                    key=lambda kv: kv[0]))

    # -- fixtures -----------------------------------------------------------
    def _setup_world(self, tag, opener, uid):
        """One complete Program tree, built over real HTTP by its own owner.

        The Venue uses the LEGACY ``Venue.league_id`` bridge (a PROGRAM id
        despite the name, and a v1-only request key) rather than a
        SeasonVenueAccess grant: a granted Venue can never be deleted, because
        the grant is itself a delete blocker, so the legacy bridge is the only
        Venue shape that is both LINKED to a Program and deletable -- which is
        exactly the reported blocker's shape. The standing grant below therefore
        hangs off a SECOND, separate Venue."""
        program = self._ok(opener, "/api/v2/setup/program",
                           {"name": f"TA-{tag} Program"})
        # A Team create is itself Program-scoped (#367), so the owner works
        # inside its own Program rather than being exempted from that guard.
        self._select(opener, program["id"])
        season = self._ok(opener, "/api/v2/setup/season",
                          {"program_id": program["id"],
                           "name": f"TA-{tag} Season"})
        # The Season axis is a real ceiling (#369 re-review), so the owner
        # selects its own Season before touching anything Season-bound -- the
        # standing venue-access grant below is exactly such a write.
        self._select(opener, program["id"], season["id"])
        league = self._ok(opener, "/api/v2/setup/league",
                          {"season_id": season["id"],
                           "name": f"TA-{tag} League"})
        binding = self.srv.STATE.api.store.league_season_for(league["id"],
                                                             season["id"])
        self.assertIsNotNone(binding, "fixture: the League/Season binding")
        division = self._ok(opener, "/api/v2/setup/division",
                            {"league_id": league["id"],
                             "season_id": season["id"],
                             "name": f"TA-{tag} Division"})
        club = self._ok(opener, "/api/v2/setup/club", {"name": f"TA-{tag} Club"})
        team = self._ok(opener, "/api/v2/setup/team",
                        {"club_id": club["id"], "league_id": league["id"],
                         "name": f"TA-{tag} Team"})
        player = self._ok(opener, "/api/v2/setup/player",
                          {"team_id": team["id"], "name": f"TA-{tag} Player",
                           "position": "forward"})
        official = self._ok(opener, "/api/v2/setup/official",
                            {"name": f"TA-{tag} Official",
                             "home_club_id": club["id"]})
        org = self._ok(opener, "/api/v2/setup/organization",
                       {"name": f"TA-{tag} Org"})
        venue = self._ok(opener, "/api/setup/venue",
                         {"name": f"TA-{tag} Venue",
                          "organization_id": org["id"],
                          "league_id": program["id"]})
        rink = self._ok(opener, "/api/v2/setup/rink",
                        {"venue_id": venue["id"], "name": f"TA-{tag} Rink"})
        slot = self._ok(opener, "/api/v2/setup/ice-slot",
                        dict(zip(("start_time", "end_time"),
                                 self._next_slot_times()),
                             rink_id=rink["id"], slot_type="game"))
        registration = self._ok(
            opener, f"/api/v2/setup/seasons/{season['id']}/team-registrations",
            {"team_id": team["id"], "league_id": league["id"],
             "division_id": division["id"]})
        granted_venue = self._ok(opener, "/api/setup/venue",
                                 {"name": f"TA-{tag} Granted Venue"})
        grant = self._ok(opener,
                         f"/api/v2/setup/seasons/{season['id']}/venue-access",
                         {"venue_id": granted_venue["id"]})
        game_id = self._inject_draft_game(tag, team["id"], season["id"], uid)
        return {
            "program": program["id"], "season": season["id"],
            "league": league["id"], "league_season": binding.id,
            "division": division["id"], "club": club["id"],
            "team": team["id"], "player": player["id"], "game": game_id,
            "official": official["id"], "organization": org["id"],
            "venue": venue["id"], "rink": rink["id"], "ice_slot": slot["id"],
            "registration": registration["id"],
            "season_venue_access": grant["id"],
            "granted_venue": granted_venue["id"],
        }

    def _next_slot_times(self):
        """Distinct times per call: an ice slot's (rink, start) is unique, and a
        collision would surface as a conflict that looks nothing like the gate."""
        self._slot_seq = getattr(self, "_slot_seq", 0) + 1
        return _slot_times(self._slot_seq * 3)

    def _inject_draft_game(self, tag, team_id, season_id, uid):
        """A draft Game is minted by the scheduler, never by a setup route, so
        it is injected -- with its creation audit attributed to the real owner
        account, since creator ownership is one of the things under test."""
        from hockey_scheduler.domain.models import Game
        store = self.srv.STATE.api.store
        game = Game(id=store.next_id("game"), home_team_id=team_id,
                    start_time=_FUTURE, season_id=season_id, is_draft=True,
                    game_type="exhibition")
        store.add_game(game)
        self.srv.STATE.api.setup._audit("game_created", "game", game.id, uid,
                                        None)
        return game.id

    # -- mint recipes: one FRESH target linked to `tag`'s Program ------------
    # Used by the destructive routes, where the positive and context-switch
    # cases consume the record. Every one is created by that world's OWNER, so
    # the attacker never has creator ownership of anything it touches.
    def _o(self, tag):
        return self.openers[tag]

    def _mint_program(self, tag):
        # A childless Program: linked to ITSELF (a Program is its own scope) and
        # the only Program shape a dependency-gated delete can actually remove.
        return self._ok(self._o(tag), "/api/v2/setup/program",
                        {"name": f"TA-{tag} Spare Program {self._seq()}"})["id"]

    def _mint_season(self, tag):
        return self._ok(self._o(tag), "/api/v2/setup/season",
                        {"program_id": self.worlds[tag]["program"],
                         "name": f"TA-{tag} Spare Season {self._seq()}"})["id"]

    def _mint_archived_season(self, tag):
        """A spare, archived Season.

        The archive route is itself gated on the Season axis (#369
        re-review), so the world's owner selects the spare Season before
        archiving it and restores its standing context afterwards -- the
        other mints run as the same identity and expect the world's Season.
        A fixture is fixed by switching context explicitly, never by
        weakening the guard it is about to be measured against."""
        season = self._mint_season(tag)
        self._select(self._o(tag), self.worlds[tag]["program"], season)
        self._ok(self._o(tag), f"/api/v2/setup/seasons/{season}/archive",
                 {"reason": "fixture"})
        self._select(self._o(tag), self.worlds[tag]["program"],
                     self.worlds[tag]["season"])
        return season

    def _mint_league(self, tag):
        """A League with its Season binding removed: still linked through its
        own ``program_id``, but now free of the binding that would otherwise
        block every League delete."""
        league = self._ok(self._o(tag), "/api/v2/setup/league",
                          {"season_id": self.worlds[tag]["season"],
                           "name": f"TA-{tag} Spare League {self._seq()}"})
        binding = self.srv.STATE.api.store.league_season_for(
            league["id"], self.worlds[tag]["season"])
        self._ok(self._o(tag),
                 f"/api/v2/setup/league-season/{binding.id}/delete", {})
        return league["id"]

    def _mint_league_season(self, tag):
        league = self._ok(self._o(tag), "/api/v2/setup/league",
                          {"season_id": self.worlds[tag]["season"],
                           "name": f"TA-{tag} Bound League {self._seq()}"})
        return self.srv.STATE.api.store.league_season_for(
            league["id"], self.worlds[tag]["season"]).id

    def _mint_division(self, tag):
        return self._ok(self._o(tag), "/api/v2/setup/division",
                        {"league_id": self.worlds[tag]["league"],
                         "season_id": self.worlds[tag]["season"],
                         "name": f"TA-{tag} Spare Div {self._seq()}"})["id"]

    def _mint_team(self, tag):
        return self._ok(self._o(tag), "/api/v2/setup/team",
                        {"club_id": self.worlds[tag]["club"],
                         "league_id": self.worlds[tag]["league"],
                         "name": f"TA-{tag} Spare Team {self._seq()}"})["id"]

    def _mint_player(self, tag):
        return self._ok(self._o(tag), "/api/v2/setup/player",
                        {"team_id": self.worlds[tag]["team"],
                         "name": f"TA-{tag} Spare Player {self._seq()}",
                         "position": "forward"})["id"]

    def _mint_game(self, tag):
        return self._inject_draft_game(tag, self.worlds[tag]["team"],
                                       self.worlds[tag]["season"],
                                       self.uids[tag])

    def _mint_official(self, tag):
        # Linked through its home Club, which owns a Team in this Program.
        return self._ok(self._o(tag), "/api/v2/setup/official",
                        {"name": f"TA-{tag} Spare Official {self._seq()}",
                         "home_club_id": self.worlds[tag]["club"]})["id"]

    def _mint_club(self, tag):
        """A Club linked to this Program through a Team it owns.

        A Club has no Program of its own -- its chain is the union of its Teams'
        Programs -- so a Club that is LINKED necessarily has a Team, and a Club
        with a Team can never be deleted. That is why the club/organization
        delete rows in the table below expect the dependency block on their
        context-switch case: the gate ALLOWS the call, and the facade's own
        dependency rule stops it. Their positive case uses the unlinked
        pending-link shape instead, which is a real, deletable positive."""
        club = self._ok(self._o(tag), "/api/v2/setup/club",
                        {"name": f"TA-{tag} Spare Club {self._seq()}"})
        self._ok(self._o(tag), "/api/v2/setup/team",
                 {"club_id": club["id"],
                  "league_id": self.worlds[tag]["league"],
                  "name": f"TA-{tag} Club Team {self._seq()}"})
        return club["id"]

    def _mint_organization(self, tag):
        """An Organization linked to this Program through a Venue it owns --
        and, for the same reason as ``_mint_club``, therefore undeletable."""
        org = self._ok(self._o(tag), "/api/v2/setup/organization",
                       {"name": f"TA-{tag} Spare Org {self._seq()}"})
        self._ok(self._o(tag), "/api/setup/venue",
                 {"name": f"TA-{tag} Org Venue {self._seq()}",
                  "organization_id": org["id"],
                  "league_id": self.worlds[tag]["program"]})
        return org["id"]

    def _mint_venue(self, tag):
        return self._ok(self._o(tag), "/api/setup/venue",
                        {"name": f"TA-{tag} Spare Venue {self._seq()}",
                         "league_id": self.worlds[tag]["program"]})["id"]

    def _mint_rink(self, tag):
        return self._ok(self._o(tag), "/api/v2/setup/rink",
                        {"venue_id": self._mint_venue(tag),
                         "name": f"TA-{tag} Spare Rink {self._seq()}"})["id"]

    def _mint_ice_slot(self, tag):
        start, end = self._next_slot_times()
        return self._ok(self._o(tag), "/api/v2/setup/ice-slot",
                        {"rink_id": self._mint_rink(tag), "start_time": start,
                         "end_time": end, "slot_type": "game"})["id"]

    def _mint_registration(self, tag):
        return self._ok(
            self._o(tag),
            f"/api/v2/setup/seasons/{self.worlds[tag]['season']}"
            f"/team-registrations",
            {"team_id": self._mint_team(tag),
             "league_id": self.worlds[tag]["league"]})["id"]

    def _mint_inactive_registration(self, tag):
        reg = self._mint_registration(tag)
        self._ok(self._o(tag),
                 f"/api/v2/setup/season-team-registration/{reg}/remove", {})
        return reg

    def _mint_grant(self, tag):
        venue = self._ok(self._o(tag), "/api/setup/venue",
                         {"name": f"TA-{tag} Grant Venue {self._seq()}"})
        return self._ok(
            self._o(tag),
            f"/api/v2/setup/seasons/{self.worlds[tag]['season']}/venue-access",
            {"venue_id": venue["id"]})["id"]

    def _mint_revoked_grant(self, tag):
        grant = self._mint_grant(tag)
        self._ok(self._o(tag),
                 f"/api/v2/setup/season-venue-access/{grant}/remove", {})
        return grant

    def _mint_unlinked_club_as_attacker(self, _tag):
        """The pending-link shape, created BY THE CALLER. Linked to nothing, so
        rule 6 (creator only) applies -- a genuine, deletable positive for a
        kind whose linked form can never be deleted."""
        return self._ok(self.openers["attacker"], "/api/v2/setup/club",
                        {"name": f"TA Attacker Club {self._seq()}"})["id"]

    def _mint_unlinked_org_as_attacker(self, _tag):
        return self._ok(self.openers["attacker"], "/api/v2/setup/organization",
                        {"name": f"TA Attacker Org {self._seq()}"})["id"]

    def _seq(self):
        self._n = getattr(self, "_n", 0) + 1
        return self._n

    def _standing(self, key):
        """A target that the route under test does not consume, so the standing
        world record can be reused."""
        return lambda tag: self.worlds[tag][key]

    # -- the three identities, and the two worlds ---------------------------
    OWNER_A = "ta_owner_a"
    OWNER_B = "ta_owner_b"
    ATTACKER = "ta_attacker"
    ARENA = "ta_arena_manager"

    #: The victim's world, and the attacker's own. Named rather than inlined
    #: because every case below reads as "world A's record, world B's context".
    VICTIM = "A"
    OWN = "B"

    def _build(self, backend, database_url):
        """Point ``STATE`` at ``backend``, then build both worlds over HTTP.

        ``owner_a``/``owner_b`` each create their own Program tree and then
        never act again. ``attacker`` creates nothing that appears in either
        world and drives every call in the matrix, changing only which Program
        it has selected -- so a positive can never be creator ownership and a
        refusal can never be a missing role (it is a League Admin, holding both
        MANAGE_SETUP and MANAGE_ARENA).
        """
        self._reset_backend(database_url, backend)
        self.openers, self.uids, self.worlds = {}, {}, {}
        for tag, username in (("A", self.OWNER_A), ("B", self.OWNER_B)):
            self.openers[tag], self.uids[tag] = self._account(
                username, Role.LEAGUE_ADMIN)
        self.openers["attacker"], self.uids["attacker"] = self._account(
            self.ATTACKER, Role.LEAGUE_ADMIN)
        for tag in ("A", "B"):
            self.worlds[tag] = self._setup_world(
                tag, self.openers[tag], self.uids[tag])
        self._select(self.openers["attacker"], self.worlds[self.OWN]["program"])
        # The two worlds must really be two: a single shared Program would make
        # every "foreign" case below a same-Program case, and the whole matrix
        # would stay green with the guard deleted.
        self.assertNotEqual(self.worlds["A"]["program"],
                            self.worlds["B"]["program"],
                            "fixture: the two worlds share a Program")

    # -- assertions ---------------------------------------------------------
    def _rows_of(self, witnesses):
        """Field-level snapshots of the records a single call could touch."""
        return {(kind, rid): self._row(kind, rid) for kind, rid in witnesses}

    def _assert_allowed(self, status, resp, codes, where):
        """The gate let this call through to the facade.

        Not "returned 200": a route whose facade then refuses for a
        NON-authorization reason (a Club that still owns Teams) has still
        proved the gate allowed it, which is the only thing under test here.
        ``not_found`` is never such a reason -- that IS the refusal.
        """
        code = (resp.get("error") or {}).get("code")
        self.assertNotEqual(
            code, "not_found",
            f"{where} was REFUSED by the target gate ({resp}). A gate that "
            f"turns away the caller's own Program is a blanket block, not a "
            f"scope decision -- and would pass the refusal cases for free.")
        if code is None:
            self.assertEqual(status, 200, f"{where}: {resp}")
            return
        self.assertIn(
            code, _NON_AUTH_CODES,
            f"{where} failed with {code!r}, which is not a recognised "
            f"non-authorization outcome: {resp}")
        self.assertIn(
            code, codes,
            f"{where} failed with {code!r}, which this route's row does not "
            f"declare as an expected non-authorization outcome: {resp}")

    @staticmethod
    def _blind(raw, record_id):
        """The refusal bytes with the echoed id masked out.

        The ONLY difference the contract permits between a foreign target and a
        nonexistent one is the id the caller itself supplied, so that is the one
        thing normalised away. Everything else -- key order, spacing, a stray
        field, the status line -- must match byte for byte, because anything
        that differs is an existence oracle for another Program's records.
        """
        return raw.replace(record_id.encode(), b"<the id the caller sent>")

    # -- the five-case matrix, driven once per table row --------------------
    def _five_cases(self, backend, row):
        att = self.openers["attacker"]
        store = self.srv.STATE.api.store
        kind, own, victim_tag = row["kind"], self.OWN, self.VICTIM
        where = f"[{backend}] {row['name']}"

        # ---- positive: a target inside the caller's ACTIVE Program --------
        mine = row["pmint"](own)
        self._select(att, *row["ctx"](own, mine))
        path, body, _w = row["call"](own, mine)
        status, resp, _raw = self._post(att, path, body)
        self._assert_allowed(status, resp, row["codes"],
                             f"{where} POSITIVE ({path})")

        # ---- the victim, and the precondition that makes a refusal mean
        #      anything at all ---------------------------------------------
        victim = row["mint"](victim_tag)
        active = self.worlds[own]["program"]
        # The attacker's own Program AND Season (#369 re-review): a
        # Program-only context now fails closed against every Season-bound
        # row, which would make half this table refuse for the wrong reason.
        self._select(att, active, self.worlds[own]["season"])
        # #369 note: the owner's repro words this precondition as "the record is
        # absent from the attacker's reads". ``get_setup_overview_v2`` is not
        # Program-scoped until #369 proper (which rebases on top of this
        # branch), so read-absence is not assertable here yet and asserting it
        # would fail. The Program-chain fact below is what the guard actually
        # decides on, recomputed from the stored rows rather than read back out
        # of the code under test, and stands in its place.
        chain = chain_programs(store, kind, victim)
        self.assertTrue(
            chain,
            f"{where}: fixture is not distinguishable -- the victim record has "
            f"NO Program chain, so refusing it would prove nothing about scope")
        self.assertNotIn(
            active, chain,
            f"{where}: fixture is not distinguishable -- the victim resolves "
            f"to the attacker's OWN active Program {active}")
        if kind in _CREATABLE:
            creators = created_by(store, kind, victim)
            # Assert who DID create it before asserting who did not: an
            # `assertNotIn` against an empty set (a mint whose audit actor never
            # reached the store, an actor-id format that stopped matching the
            # session's) passes while proving nothing at all.
            if creators:
                self.assertEqual(
                    creators, {self.uids[victim_tag]},
                    f"{where}: fixture is not distinguishable -- the victim's "
                    f"recorded creator is {creators}, not the other world's "
                    f"owner, so the creator-ownership check below would be "
                    f"comparing against the wrong account")
            else:
                # Exactly ONE kind carries no creation attribution at all, and
                # it is a product fact rather than a fixture gap: a
                # LeagueSeason binding minted as a side effect of create_league
                # gets no row of its own (create_league audits
                # ("level_created", "level") against the LEAGUE's id). It is
                # harmless here for a specific reason -- the chain assertion
                # above already proved this record is LINKED, so rule 6
                # ("unlinked -> creator only") is unreachable for it and
                # creator ownership cannot explain any outcome. Pinned to that
                # one kind so any OTHER record losing its attribution fails
                # here instead of quietly going vacuous.
                self.assertEqual(
                    kind, "league_season",
                    f"{where}: fixture is not distinguishable -- NOTHING is "
                    f"recorded as having created the victim, so the "
                    f"creator-ownership check below proves nothing")
            self.assertNotIn(
                self.uids["attacker"], creators,
                f"{where}: fixture is not distinguishable -- the attacker "
                f"CREATED the record it is attacking, so a refusal would be "
                f"about something other than Program scope")

        # ---- foreign + no-mutation ---------------------------------------
        path, body, witnesses = row["call"](own, victim)
        before, rows_before = self._snapshot(), self._rows_of(witnesses)
        status, resp, raw_foreign = self._post(att, path, body)
        self.assertEqual(
            status, 404,
            f"{where} FOREIGN: another Program's record answered {status} "
            f"({resp}) -- this is the reported blocker")
        self.assertEqual(
            resp, {"error": {"code": "not_found",
                             "message": f"{_WORD[kind]} {victim} not found."}},
            f"{where} FOREIGN: the refusal is not the facade's own generic "
            f"not-found, so it is distinguishable from a nonexistent id")
        self.assertEqual(
            self._snapshot(), before,
            f"{where} NO-MUTATION: a REFUSED call changed the store's row-id "
            f"sets or wrote a setup-audit row")
        self.assertEqual(
            self._rows_of(witnesses), rows_before,
            f"{where} NO-MUTATION: a REFUSED call edited a record in place")

        # ---- nonexistent: the same bytes, never an oracle -----------------
        absent = f"{kind}_ta_absent_{self._seq()}"
        path, body, _w = row["call"](own, absent)
        status_absent, _resp, raw_absent = self._post(att, path, body)
        self.assertEqual(status_absent, 404, f"{where} NONEXISTENT: {_resp}")
        self.assertEqual(
            (status, self._blind(raw_foreign, victim)),
            (status_absent, self._blind(raw_absent, absent)),
            f"{where} NONEXISTENT: a reader of the socket can tell a record "
            f"that EXISTS in another Program from one that does not exist at "
            f"all -- the refusal is an existence oracle")

        # ---- context switch: the SAME call, the target's own Program -------
        self._select(att, *row["ctx"](victim_tag, victim))
        path, body, _w = row["call"](victim_tag, victim)
        status, resp, _raw = self._post(att, path, body)
        self._assert_allowed(
            status, resp, row["codes"],
            f"{where} CONTEXT SWITCH ({path}): selecting the target's OWN "
            f"Program did not make the same call on the same target succeed, "
            f"so the refusal above was a blanket block rather than a scope "
            f"decision")
        self._select(att, self.worlds[own]["program"],
                     self.worlds[own]["season"])

    # ----------------------------------------------------------------------
    # THE ROUTE TABLE
    #
    # One row per (route, GATED ARGUMENT). A reassign contributes TWO rows --
    # one for the SOURCE record named in the path and one for the DESTINATION
    # id carried in the body -- because those are two separate calls into the
    # shared gate, and a route that gated only the source would otherwise sail
    # through a source-only matrix while still moving a foreign record.
    #
    #   kind   the CANONICAL kind of the gated argument. Picks the label the
    #          refusal must carry (``_WORD``) and the store getter the
    #          no-mutation case field-snapshots.
    #   mint   (tag) -> a fresh target linked to THAT world's Program. Used by
    #          the foreign / nonexistent / context-switch cases.
    #   pmint  optional override for the POSITIVE case only, for the two kinds
    #          (Club, Organization) whose LINKED form can never be deleted.
    #   ctx    (tag, target) -> the (program_id, season_id, league_id) the
    #          attacker selects for the positive and context-switch cases. The
    #          world's Program AND Season by default, because a Program-only
    #          context now fails closed against every Season-bound row (#369
    #          re-review). Two overrides: the TARGET ITSELF when the target is
    #          a Program (a Program is its own scope, and it carries no Season
    #          axis to select), and the TARGET SEASON when the target IS a
    #          Season that is not the world's standing one. League is left
    #          unselected ("No League" = the approved Program + active-Season
    #          union), so this table keeps measuring the PROGRAM axis it was
    #          written for; the League axis has its own negatives below.
    #   call   (active tag, target id) -> (path, body, witnesses). Witnesses are
    #          every record this one call could have written, so the no-mutation
    #          case covers an in-place edit that changes no row-id set. A
    #          DESTINATION row mints its own source inside ``call``, in whatever
    #          Program is active, so the only thing its refusal can be about is
    #          the destination id.
    #   codes  non-authorization error codes the positive / context-switch case
    #          may legitimately carry (see ``_NON_AUTH_CODES``).
    # ----------------------------------------------------------------------
    def _route_rows(self):
        rows = []

        def add(name, kind, mint, call, pmint=None, ctx=None,
                codes=frozenset()):
            rows.append({"name": name, "kind": kind, "mint": mint,
                         "pmint": pmint or mint, "call": call,
                         "codes": codes,
                         "ctx": ctx or (lambda tag, _tid:
                                        (self.worlds[tag]["program"],
                                         self.worlds[tag]["season"], None))})

        # -- the generic deletes, on BOTH versions -------------------------
        # (canonical kind, v1 wire word or None, v2 wire word or None, mint,
        #  positive-case mint, ctx, tolerated non-auth codes). v1's vocabulary
        # differs: its "league" is today's PROGRAM and its "level" is today's
        # competition LEAGUE. Player/Official delete is v2-only (#232/#271) and
        # league-season delete is a v2 addition (#159), hence the Nones.
        # A Program IS its own scope, and it carries no Season axis to select.
        _target_is_its_own_scope = (lambda _tag, tid: (tid, None, None))
        # A Season IS the Season axis: "switch to the target's own context"
        # means selecting that very Season, not the world's standing one.
        _target_season_is_the_scope = (
            lambda tag, tid: (self.worlds[tag]["program"], tid, None))
        deletes = [
            ("organization", "organization", "organization",
             self._mint_organization, self._mint_unlinked_org_as_attacker,
             None, frozenset({"has_dependencies"})),
            ("program", "league", "program", self._mint_program, None,
             _target_is_its_own_scope, frozenset()),
            ("season", "season", "season", self._mint_season, None,
             _target_season_is_the_scope, frozenset()),
            ("league", "level", "league", self._mint_league, None, None,
             frozenset()),
            ("league_season", None, "league-season", self._mint_league_season,
             None, None, frozenset()),
            ("division", "division", "division", self._mint_division, None,
             None, frozenset()),
            ("club", "club", "club", self._mint_club,
             self._mint_unlinked_club_as_attacker, None,
             frozenset({"has_dependencies"})),
            ("team", "team", "team", self._mint_team, None, None, frozenset()),
            ("player", None, "player", self._mint_player, None, None,
             frozenset()),
            ("official", None, "official", self._mint_official, None, None,
             frozenset()),
            ("venue", "venue", "venue", self._mint_venue, None, None,
             frozenset()),
            ("rink", "rink", "rink", self._mint_rink, None, None, frozenset()),
            ("ice_slot", "ice-slot", "ice-slot", self._mint_ice_slot, None,
             None, frozenset()),
            ("game", "game", "game", self._mint_game, None, None, frozenset()),
        ]
        for kind, v1w, v2w, mint, pmint, ctx, codes in deletes:
            for base, word in (("/api/setup", v1w), ("/api/v2/setup", v2w)):
                if word is None:
                    continue

                def call(_active, tid, _b=base, _w=word, _k=kind):
                    return f"{_b}/{_w}/{tid}/delete", {}, [(_k, tid)]

                add(f"POST {base}/{word}/<id>/delete", kind, mint, call,
                    pmint=pmint, ctx=ctx, codes=codes)

        # -- every assign-<target> reassign, BOTH ENDS ---------------------
        # (base, path entity word, assign-<word>, body key, SOURCE kind,
        #  source mint, DESTINATION kind, the world key naming a valid
        #  destination inside whichever Program is active).
        # The destination for a SOURCE row is always taken from the ACTIVE
        # world, so the only thing the source row can fail on is its source.
        reassigns = [
            ("/api/setup", "league", "organization", "organization_id",
             "program", self._standing("program"), "organization",
             "organization"),
            ("/api/setup", "venue", "organization", "organization_id",
             "venue", self._mint_venue, "organization", "organization"),
            ("/api/setup", "rink", "venue", "venue_id",
             "rink", self._mint_rink, "venue", "venue"),
            ("/api/setup", "division", "level", "level_id",
             "division", self._mint_division, "league", "league"),
            ("/api/setup", "team", "club", "club_id",
             "team", self._mint_team, "club", "club"),
            ("/api/setup", "player", "team", "team_id",
             "player", self._mint_player, "team", "team"),
            ("/api/v2/setup", "program", "organization",
             "operator_organization_id", "program", self._standing("program"),
             "organization", "organization"),
            ("/api/v2/setup", "division", "league", "league_id",
             "division", self._mint_division, "league", "league"),
            ("/api/v2/setup", "team", "club", "club_id",
             "team", self._mint_team, "club", "club"),
            ("/api/v2/setup", "team", "league", "league_id",
             "team", self._mint_team, "league", "league"),
            ("/api/v2/setup", "player", "team", "team_id",
             "player", self._mint_player, "team", "team"),
            ("/api/v2/setup", "rink", "venue", "venue_id",
             "rink", self._mint_rink, "venue", "venue"),
            ("/api/v2/setup", "venue", "organization", "organization_id",
             "venue", self._mint_venue, "organization", "organization"),
        ]
        for (base, ent, word, key, src_kind, src_mint, dest_kind,
             dest_key) in reassigns:

            def source_call(active, tid, _b=base, _e=ent, _w=word, _k=key,
                            _sk=src_kind, _dk=dest_key):
                return (f"{_b}/{_e}/{tid}/assign-{_w}",
                        {_k: self.worlds[active][_dk]}, [(_sk, tid)])

            def dest_call(active, tid, _b=base, _e=ent, _w=word, _k=key,
                          _sm=src_mint, _sk=src_kind, _dk=dest_kind):
                source = _sm(active)
                return (f"{_b}/{_e}/{source}/assign-{_w}", {_k: tid},
                        [(_dk, tid), (_sk, source)])

            add(f"POST {base}/{ent}/<id>/assign-{word} [SOURCE]",
                src_kind, src_mint, source_call,
                ctx=(_target_is_its_own_scope if src_kind == "program"
                     else None))
            add(f"POST {base}/{ent}/<id>/assign-{word} [DESTINATION]",
                dest_kind, self._standing(dest_key), dest_call)

        # -- the two in-place Player edits (v2 only) -----------------------
        def player_update_call(_active, tid):
            return (f"/api/v2/setup/player/{tid}/update",
                    {"name": f"TA Renamed {self._seq()}"}, [("player", tid)])

        def player_active_call(_active, tid):
            return (f"/api/v2/setup/player/{tid}/active",
                    {"active": False, "reason": "matrix"}, [("player", tid)])

        add("POST /api/v2/setup/player/<id>/update", "player",
            self._mint_player, player_update_call)
        add("POST /api/v2/setup/player/<id>/active", "player",
            self._mint_player, player_active_call)

        # -- Season lifecycle: an in-place status flip on an existing row ---
        def archive_call(_active, tid):
            return (f"/api/v2/setup/seasons/{tid}/archive",
                    {"reason": "matrix"}, [("season", tid)])

        def reopen_call(_active, tid):
            return (f"/api/v2/setup/seasons/{tid}/reopen",
                    {"reason": "matrix"}, [("season", tid)])

        add("POST /api/v2/setup/seasons/<id>/archive", "season",
            self._mint_season, archive_call,
            ctx=_target_season_is_the_scope)
        add("POST /api/v2/setup/seasons/<id>/reopen", "season",
            self._mint_archived_season, reopen_call,
            ctx=_target_season_is_the_scope)

        # -- the venue-access grant's SEASON argument, which is generic ------
        # (its VENUE argument is the facility-tree exception and has its own
        # test below -- the whole point being that the two arguments of this
        # one route are judged by DIFFERENT rules).
        def grant_season_call(active, tid):
            return (f"/api/v2/setup/seasons/{tid}/venue-access",
                    {"venue_id": self._mint_venue(active)}, [("season", tid)])

        add("POST /api/v2/setup/seasons/<id>/venue-access [SEASON]", "season",
            self._mint_season, grant_season_call,
            ctx=_target_season_is_the_scope)

        # -- bridge row: SeasonTeamRegistration -----------------------------
        # It carries no Program of its own -- it IS the (Team, LeagueSeason)
        # join -- so it is judged by its LeagueSeason while still being
        # REPORTED under its own id and label.
        registration_routes = [
            ("/api/setup", "assign-division", "division_id", "division",
             "division", self._mint_registration),
            ("/api/v2/setup", "assign-league", "league_id", "league", "league",
             self._mint_registration),
            ("/api/v2/setup", "assign-division", "division_id", "division",
             "division", self._mint_registration),
        ]
        for base, verb, key, dest_kind, dest_key, reg_mint in \
                registration_routes:

            def reg_source_call(active, tid, _b=base, _v=verb, _k=key,
                                _dk=dest_key):
                return (f"{_b}/season-team-registration/{tid}/{_v}",
                        {_k: self.worlds[active][_dk]}, [("registration", tid)])

            def reg_dest_call(active, tid, _b=base, _v=verb, _k=key,
                              _dk=dest_kind):
                source = self._mint_registration(active)
                return (f"{_b}/season-team-registration/{source}/{_v}",
                        {_k: tid}, [(_dk, tid), ("registration", source)])

            add(f"POST {base}/season-team-registration/<id>/{verb} [SOURCE]",
                "registration", reg_mint, reg_source_call)
            add(f"POST {base}/season-team-registration/<id>/{verb} "
                f"[DESTINATION]", dest_kind, self._standing(dest_key),
                reg_dest_call)

        for base in ("/api/setup", "/api/v2/setup"):
            def remove_call(_active, tid, _b=base):
                return (f"{_b}/season-team-registration/{tid}/remove", {},
                        [("registration", tid)])

            add(f"POST {base}/season-team-registration/<id>/remove",
                "registration", self._mint_registration, remove_call)

        def registration_delete_call(_active, tid):
            return (f"/api/v2/setup/season-team-registration/{tid}/delete", {},
                    [("registration", tid)])

        add("POST /api/v2/setup/season-team-registration/<id>/delete",
            "registration", self._mint_inactive_registration,
            registration_delete_call)

        # -- bridge row: SeasonVenueAccess, judged by its Season ------------
        def access_remove_call(_active, tid):
            return (f"/api/v2/setup/season-venue-access/{tid}/remove", {},
                    [("season_venue_access", tid)])

        def access_delete_call(_active, tid):
            return (f"/api/v2/setup/season-venue-access/{tid}/delete", {},
                    [("season_venue_access", tid)])

        add("POST /api/v2/setup/season-venue-access/<id>/remove",
            "season_venue_access", self._mint_grant, access_remove_call)
        add("POST /api/v2/setup/season-venue-access/<id>/delete",
            "season_venue_access", self._mint_revoked_grant,
            access_delete_call)

        return rows

    def _run_route_matrix(self, database_url, backend):
        self._build(backend, database_url)
        rows = self._route_rows()
        # A table that silently shrank (a lambda that stopped being appended, a
        # loop that stopped looping) would still report OK, having proved
        # nothing about the routes it dropped.
        self.assertGreaterEqual(
            len(rows), 60,
            "the route table has shrunk; every gated argument on every gated "
            "route needs its own row")
        for row in rows:
            with self.subTest(route=row["name"]):
                self._five_cases(backend, row)

    def test_every_gated_route_five_case_matrix_memory(self):
        self._run_route_matrix(None, "memory")

    def test_every_gated_route_five_case_matrix_sqlite(self):
        self._run_route_matrix(":memory:", "sqlite")

    @unittest.skipUnless(os.environ.get("TEST_DATABASE_URL"),
                         "PostgreSQL required (set TEST_DATABASE_URL) -- "
                         "the SQL leg is covered by the SQLite variant")
    def test_every_gated_route_five_case_matrix_postgres(self):
        self._run_route_matrix(os.environ["TEST_DATABASE_URL"], "postgres")

    # ----------------------------------------------------------------------
    # PART 2b — the SEASON and LEAGUE axes over authenticated v1/v2 HTTP.
    #
    # Part 2 varies the PROGRAM. These legs hold the Program CONSTANT and vary
    # exactly ONE of the other two axes, which is the blind spot the re-review
    # found: the shared gate resolved all three and authorized on the Program
    # alone, so `POST /api/v2/setup/player/<League-B-player>/update` answered
    # 200 and renamed a Player in a League the caller had not selected. The
    # decision is proved in Part 1b; this proves the WIRING, on both API
    # versions, for the same 23 call sites.
    # ----------------------------------------------------------------------
    AXIS_OWNER = "ta_axis_owner"
    AXIS_CALLER = "ta_axis_caller"

    def _build_axis_world(self, backend, database_url):
        """ONE Program, TWO Seasons, TWO Leagues, all four bindings — built
        over real HTTP by an owner who then never acts again.

        ``axis_caller`` (League Admin, holding both MANAGE_SETUP and
        MANAGE_ARENA) creates nothing that appears in the world and drives
        every call below, changing only its SEASON or its LEAGUE. So no
        positive can be creator ownership, no refusal can be a missing role,
        and — because there is only ONE Program — no refusal can be the Program
        ceiling either."""
        self._reset_backend(database_url, backend)
        self.ax_owner, self.ax_owner_uid = self._account(
            self.AXIS_OWNER, Role.LEAGUE_ADMIN)
        self.ax_caller, self.ax_caller_uid = self._account(
            self.AXIS_CALLER, Role.LEAGUE_ADMIN)
        o = self.ax_owner
        program = self._ok(o, "/api/v2/setup/program", {"name": "AX Program"})
        w = {"program": program["id"], "season": {}, "league": {},
             "league_season": {}, "club": {}, "team": {}}
        self._select(o, program["id"])
        for key in ("A", "B"):
            w["season"][key] = self._ok(
                o, "/api/v2/setup/season",
                {"program_id": program["id"],
                 "name": f"AX Season {key}"})["id"]
        for key in ("A", "B"):
            league = self._ok(o, "/api/v2/setup/league",
                              {"season_id": w["season"]["A"],
                               "name": f"AX League {key}"})
            w["league"][key] = league["id"]
            # No HTTP route binds an EXISTING League to a further Season
            # (`create_league` only ever mints a new one), so the setup service
            # is called directly — a fixture step, like the injected draft Game.
            # Both Leagues bound to both Seasons is what makes "switch the
            # League" and "switch the Season" each a single legal context move;
            # a League that existed only in Season B would leave every negative
            # ambiguous between the two axes.
            self.srv.STATE.api.setup.create_league_season(
                league["id"], w["season"]["B"], self.ax_owner_uid)
        for lk in ("A", "B"):
            w["club"][lk] = self._ok(o, "/api/v2/setup/club",
                                     {"name": f"AX Club {lk}"})["id"]
            w["team"][lk] = self._ok(
                o, "/api/v2/setup/team",
                {"club_id": w["club"][lk], "league_id": w["league"][lk],
                 "name": f"AX Team {lk}"})["id"]
        store = self.srv.STATE.api.store
        for lk in ("A", "B"):
            for sk in ("A", "B"):
                binding = store.league_season_for(w["league"][lk],
                                                  w["season"][sk])
                self.assertIsNotNone(
                    binding,
                    f"fixture: League {lk} must be bound to Season {sk}")
                w["league_season"][(lk, sk)] = binding.id
        self.assertNotEqual(w["season"]["A"], w["season"]["B"])
        self.assertNotEqual(w["league"]["A"], w["league"]["B"])
        self.ax = w
        #: binding id -> the League it binds, for the LeagueSeason rows' ctx.
        self._ax_ls_league = {}
        self._ax_owner_at(w["season"]["A"])

    # -- fixture context ----------------------------------------------------
    def _ax_owner_at(self, season_id):
        """Put the WORLD OWNER in a given Season. Several fixture steps below
        (a venue-access grant, a revoke, a registration remove, an archive) are
        themselves Season-bound writes and go through the very gate under test;
        the fixture switches context explicitly rather than the guard being
        relaxed for it."""
        self._select(self.ax_owner, self.ax["program"], season_id)

    # -- axis mints: one FRESH record at the (League, Season) corner asked for
    def _ax_team(self, lk, _sk):
        return self._ok(self.ax_owner, "/api/v2/setup/team",
                        {"club_id": self.ax["club"][lk],
                         "league_id": self.ax["league"][lk],
                         "name": f"AX Spare Team {self._seq()}"})["id"]

    def _ax_player(self, lk, _sk):
        return self._ok(self.ax_owner, "/api/v2/setup/player",
                        {"team_id": self.ax["team"][lk],
                         "name": f"AX Spare Player {self._seq()}",
                         "position": "forward"})["id"]

    def _ax_club(self, lk, _sk):
        """A Club whose only Team is in League ``lk`` — so its whole chain
        names that League and nothing else. A linked Club can never be deleted
        (its Team blocks it), which is why the club rows tolerate
        ``has_dependencies`` on the cases the gate ALLOWS."""
        club = self._ok(self.ax_owner, "/api/v2/setup/club",
                        {"name": f"AX Spare Club {self._seq()}"})["id"]
        self._ok(self.ax_owner, "/api/v2/setup/team",
                 {"club_id": club, "league_id": self.ax["league"][lk],
                  "name": f"AX Club Team {self._seq()}"})
        return club

    def _ax_official(self, lk, _sk):
        return self._ok(self.ax_owner, "/api/v2/setup/official",
                        {"name": f"AX Spare Official {self._seq()}",
                         "home_club_id": self.ax["club"][lk]})["id"]

    def _ax_division(self, lk, sk):
        return self._ok(self.ax_owner, "/api/v2/setup/division",
                        {"league_id": self.ax["league"][lk],
                         "season_id": self.ax["season"][sk],
                         "name": f"AX Spare Div {self._seq()}"})["id"]

    def _ax_league_season(self, _lk, sk):
        """A FRESH permanent League bound to Season ``sk``, returned as its
        BINDING id — never one of the world's standing bindings, which the
        route under test would delete out from under every later row.

        Its League is recorded so the League-axis rows can select it: a
        LeagueSeason's League axis is the League it binds, so "switch to the
        record's own League" means that one. Childless, so the dependency gate
        never masks the target gate's answer."""
        league = self._ok(self.ax_owner, "/api/v2/setup/league",
                          {"season_id": self.ax["season"][sk],
                           "name": f"AX Bound League {self._seq()}"})["id"]
        binding = self.srv.STATE.api.store.league_season_for(
            league, self.ax["season"][sk])
        self.assertIsNotNone(binding, "fixture: the League/Season binding")
        self._ax_ls_league[binding.id] = league
        return binding.id

    def _ax_game(self, lk, sk):
        from hockey_scheduler.domain.models import Game
        store = self.srv.STATE.api.store
        game = Game(id=store.next_id("game"), home_team_id=self.ax["team"][lk],
                    start_time=_FUTURE, season_id=self.ax["season"][sk],
                    league_id=self.ax["league"][lk], is_draft=True,
                    game_type="exhibition")
        store.add_game(game)
        self.srv.STATE.api.setup._audit("game_created", "game", game.id,
                                        self.ax_owner_uid, None)
        return game.id

    def _ax_registration(self, lk, sk):
        self._ax_owner_at(self.ax["season"][sk])
        return self._ok(
            self.ax_owner,
            f"/api/v2/setup/seasons/{self.ax['season'][sk]}/team-registrations",
            {"team_id": self._ax_team(lk, sk),
             "league_id": self.ax["league"][lk]})["id"]

    def _ax_inactive_registration(self, lk, sk):
        reg = self._ax_registration(lk, sk)
        self._ax_owner_at(self.ax["season"][sk])
        self._ok(self.ax_owner,
                 f"/api/v2/setup/season-team-registration/{reg}/remove", {})
        return reg

    def _ax_season(self, _lk, _sk):
        """A spare, childless Season — the only Season shape a dependency-gated
        delete can remove, and its own Season axis."""
        return self._ok(self.ax_owner, "/api/v2/setup/season",
                        {"program_id": self.ax["program"],
                         "name": f"AX Spare Season {self._seq()}"})["id"]

    def _ax_archived_season(self, lk, sk):
        season = self._ax_season(lk, sk)
        self._ax_owner_at(season)
        self._ok(self.ax_owner, f"/api/v2/setup/seasons/{season}/archive",
                 {"reason": "fixture"})
        self._ax_owner_at(self.ax["season"]["A"])
        return season

    def _ax_grantable_venue(self):
        """A Venue the facility-tree EXCEPTION will accept as an established
        arena: linked through the LEGACY ``Venue.league_id`` bridge (a PROGRAM
        id). Used only as the BODY of a venue-access grant whose SEASON is the
        gated argument."""
        return self._ok(self.ax_owner, "/api/setup/venue",
                        {"name": f"AX Grantable Venue {self._seq()}",
                         "league_id": self.ax["program"]})["id"]

    def _ax_venue(self, _lk, sk):
        """A Venue whose ONLY link is a grant to Season ``sk``.

        Deliberately NOT the legacy ``Venue.league_id`` shape Part 2 uses: that
        bridge holds a Program id and names no Season, so a Venue carrying one
        is not Season-bound at all and would pass every assertion below with
        the Season comparison deleted."""
        venue = self._ok(self.ax_owner, "/api/v2/setup/venue",
                         {"name": f"AX Venue {self._seq()}"})["id"]
        self._ax_owner_at(self.ax["season"][sk])
        self._ok(self.ax_owner,
                 f"/api/v2/setup/seasons/{self.ax['season'][sk]}/venue-access",
                 {"venue_id": venue})
        return venue

    def _ax_rink(self, lk, sk):
        return self._ok(self.ax_owner, "/api/v2/setup/rink",
                        {"venue_id": self._ax_venue(lk, sk),
                         "name": f"AX Rink {self._seq()}"})["id"]

    def _ax_ice_slot(self, lk, sk):
        start, end = self._next_slot_times()
        return self._ok(self.ax_owner, "/api/v2/setup/ice-slot",
                        {"rink_id": self._ax_rink(lk, sk),
                         "start_time": start, "end_time": end,
                         "slot_type": "game"})["id"]

    def _ax_grant(self, _lk, sk):
        venue = self._ok(self.ax_owner, "/api/v2/setup/venue",
                         {"name": f"AX Grant Venue {self._seq()}"})["id"]
        self._ax_owner_at(self.ax["season"][sk])
        return self._ok(
            self.ax_owner,
            f"/api/v2/setup/seasons/{self.ax['season'][sk]}/venue-access",
            {"venue_id": venue})["id"]

    def _ax_revoked_grant(self, lk, sk):
        grant = self._ax_grant(lk, sk)
        self._ax_owner_at(self.ax["season"][sk])
        self._ok(self.ax_owner,
                 f"/api/v2/setup/season-venue-access/{grant}/remove", {})
        return grant

    def _ax_standing(self, key):
        return lambda lk, sk: self.ax[key][lk]

    # -- the runner ---------------------------------------------------------
    def _axis_cases(self, backend, row):
        """The five cases of Part 2, with the PROGRAM held constant and one of
        the other two axes varied."""
        caller = self.ax_caller
        store = self.srv.STATE.api.store
        kind, axis, where = row["kind"], row["axis"], f"[{backend}] {row['name']}"
        # ("A", "A") is always the SELECTED corner; the victim sits one axis
        # away from it and nowhere else.
        same = ("A", "A")
        other = ("B", "A") if axis == "league" else ("A", "B")

        # Every record is minted BEFORE anything is called, and the REFUSAL is
        # measured first. A destructive route consumes its target, and for the
        # two kinds that NAME their own axis (a Season, a LeagueSeason) the
        # context is that very record -- so running the positive first would
        # delete the context the refusal is supposed to be judged in, and the
        # refusal would then be about a context that no longer exists rather
        # than about the axis under test.
        victim = row["mint"](*other)
        ctx_other = row["ctx"](victim, *other)
        mine = row["mint"](*same)
        ctx_same = row["ctx"](mine, *same)

        # ---- the precondition that makes the refusal mean exactly ONE axis -
        self._select(caller, *ctx_same)
        active_program, active_season, active_league = ctx_same
        axes = chain_axes(store, kind, victim)
        self.assertTrue(
            axes,
            f"{where}: fixture is not distinguishable — the victim has no "
            f"chain at all, so refusing it would prove nothing")
        self.assertEqual(
            {t[0] for t in axes}, {active_program},
            f"{where}: fixture is not distinguishable — the victim is not in "
            f"the caller's OWN Program, so the Program ceiling alone could "
            f"explain the refusal and nothing about {axis} would be proved")
        if axis == "league":
            self.assertTrue(
                all(t[2] is not None and t[2] != active_league for t in axes),
                f"{where}: fixture is not distinguishable — the victim does "
                f"not name a League other than the selected one: {axes}")
            self.assertLessEqual(
                {t[1] for t in axes} - {None}, {active_season},
                f"{where}: fixture is not distinguishable — the victim names "
                f"a Season other than the selected one, so the SEASON axis "
                f"could explain the refusal instead of the League: {axes}")
        else:
            self.assertIsNone(
                active_league,
                f"{where}: the Season negatives run with NO League selected, "
                f"so the League comparison provably plays no part; a League "
                f"is selected here, which makes the refusal ambiguous")
            self.assertTrue(
                all(t[1] is not None and t[1] != active_season for t in axes),
                f"{where}: fixture is not distinguishable — the victim does "
                f"not name a Season other than the selected one: {axes}")
        if kind in _CREATABLE:
            self.assertNotIn(
                self.ax_caller_uid, created_by(store, kind, victim),
                f"{where}: fixture is not distinguishable — the caller CREATED "
                f"the record it is attacking")

        # ---- the refusal + no mutation ------------------------------------
        path, body, witnesses = row["call"](victim, *same)
        before, rows_before = self._snapshot(), self._rows_of(witnesses)
        status, resp, raw_victim = self._post(caller, path, body)
        self.assertEqual(
            status, 404,
            f"{where} CROSS-{axis.upper()}: a same-Program record in another "
            f"{axis} answered {status} ({resp}) — this is the reported blocker")
        self.assertEqual(
            resp, {"error": {"code": "not_found",
                             "message": f"{_WORD[kind]} {victim} not found."}},
            f"{where} CROSS-{axis.upper()}: the refusal is not the facade's "
            f"own generic not-found")
        self.assertEqual(
            self._snapshot(), before,
            f"{where} NO-MUTATION: a REFUSED call changed the store's row-id "
            f"sets or wrote a setup-audit row")
        self.assertEqual(
            self._rows_of(witnesses), rows_before,
            f"{where} NO-MUTATION: a REFUSED call edited a record in place")

        # ---- nonexistent: the same bytes, never an oracle -----------------
        absent = f"{kind}_ax_absent_{self._seq()}"
        path, body, _w = row["call"](absent, *same)
        status_absent, _resp, raw_absent = self._post(caller, path, body)
        self.assertEqual(status_absent, 404, f"{where} NONEXISTENT: {_resp}")
        self.assertEqual(
            (status, self._blind(raw_victim, victim)),
            (status_absent, self._blind(raw_absent, absent)),
            f"{where} NONEXISTENT: a reader of the socket can tell a record "
            f"that EXISTS in another {axis} from one that does not exist at "
            f"all — the refusal is an existence oracle")

        # ---- positive: the same call inside the caller's own context ------
        self._select(caller, *ctx_same)
        path, body, _w = row["call"](mine, *same)
        status, resp, _raw = self._post(caller, path, body)
        self._assert_allowed(status, resp, row["codes"],
                             f"{where} POSITIVE ({path})")

        # ---- switch the EXACT missing axis --------------------------------
        self.assertEqual(
            ctx_other[0], active_program,
            f"{where}: the switch must change ONLY the {axis}, never the "
            f"Program")
        if axis == "league":
            self.assertEqual(ctx_other[1], active_season,
                             f"{where}: the League switch changed the Season "
                             f"too, so it proves nothing about the League")
        else:
            self.assertEqual(ctx_other[2], active_league,
                             f"{where}: the Season switch changed the League "
                             f"too")
        self._select(caller, *ctx_other)
        path, body, _w = row["call"](victim, *other)
        status, resp, _raw = self._post(caller, path, body)
        self._assert_allowed(
            status, resp, row["codes"],
            f"{where} AXIS SWITCH ({path}): selecting the target's own {axis} "
            f"did not make the same call on the same record succeed, so the "
            f"refusal above was a blanket block rather than an {axis} decision")

    # -- the axis route table -----------------------------------------------
    def _axis_route_rows(self):
        """One row per (route, gated argument, axis under test).

        Same contract as the Part 2 table, plus ``axis`` ("league" | "season")
        and a ``mint``/``call``/``ctx`` that take the (League key, Season key)
        CORNER rather than a world tag. ``ctx`` returns the whole triple, so a
        row whose target IS an axis value (a Season) can name itself."""
        rows = []
        ax = lambda: self.ax                                   # noqa: E731

        def add(name, kind, axis, mint, call, ctx=None, codes=frozenset()):
            default = (
                (lambda _tid, lk, sk: (self.ax["program"],
                                       self.ax["season"][sk],
                                       self.ax["league"][lk]))
                if axis == "league" else
                (lambda _tid, _lk, sk: (self.ax["program"],
                                        self.ax["season"][sk], None)))
            rows.append({"name": f"{name} [{axis}]", "kind": kind,
                         "axis": axis, "mint": mint, "call": call,
                         "codes": codes, "ctx": ctx or default})

        # -- deletes, both versions, per axis ------------------------------
        # (kind, v1 wire word or None, v2 wire word or None, mint, axes, codes)
        deletes = [
            ("team", "team", "team", self._ax_team, ("league",), frozenset()),
            ("player", None, "player", self._ax_player, ("league",),
             frozenset()),
            ("official", None, "official", self._ax_official, ("league",),
             frozenset()),
            ("club", "club", "club", self._ax_club, ("league",),
             frozenset({"has_dependencies"})),
            ("division", "division", "division", self._ax_division,
             ("league", "season"), frozenset()),
            ("game", "game", "game", self._ax_game, ("league", "season"),
             frozenset()),
            ("league_season", None, "league-season", self._ax_league_season,
             ("league", "season"), frozenset()),
            ("season", "season", "season", self._ax_season, ("season",),
             frozenset()),
            # THE FACILITY TREE — a Season axis through SeasonVenueAccess and
            # no League axis whatever, so it appears under "season" only.
            ("venue", "venue", "venue", self._ax_venue, ("season",),
             frozenset({"has_dependencies"})),
            ("rink", "rink", "rink", self._ax_rink, ("season",),
             frozenset({"has_dependencies"})),
            ("ice_slot", "ice-slot", "ice-slot", self._ax_ice_slot,
             ("season",), frozenset()),
        ]
        # A Season IS the Season axis and a LeagueSeason names its own League:
        # for those two kinds "switch to the record's own axis" means the
        # record itself, not the world's standing value.
        _season_names_itself = (
            lambda tid, _lk, _sk: (self.ax["program"], tid, None))
        _binding_names_its_league = (
            lambda tid, _lk, sk: (self.ax["program"], self.ax["season"][sk],
                                  self._ax_ls_league[tid]))
        _own_axis = {("season", "season"): _season_names_itself,
                     ("league_season", "league"): _binding_names_its_league}
        for kind, v1w, v2w, mint, axes, codes in deletes:
            for base, word in (("/api/setup", v1w), ("/api/v2/setup", v2w)):
                if word is None:
                    continue

                def call(tid, _lk, _sk, _b=base, _w=word, _k=kind):
                    return f"{_b}/{_w}/{tid}/delete", {}, [(_k, tid)]

                for axis in axes:
                    add(f"POST {base}/{word}/<id>/delete", kind, axis, mint,
                        call, ctx=_own_axis.get((kind, axis)), codes=codes)

        # -- the two in-place Player edits: the owner's VERBATIM repro ------
        def player_update_call(tid, _lk, _sk):
            return (f"/api/v2/setup/player/{tid}/update",
                    {"name": f"AX Renamed {self._seq()}"}, [("player", tid)])

        def player_active_call(tid, _lk, _sk):
            return (f"/api/v2/setup/player/{tid}/active",
                    {"active": False, "reason": "axis"}, [("player", tid)])

        add("POST /api/v2/setup/player/<id>/update", "player", "league",
            self._ax_player, player_update_call)
        add("POST /api/v2/setup/player/<id>/active", "player", "league",
            self._ax_player, player_active_call)

        # -- Season lifecycle: in-place status flips on an existing Season --
        def archive_call(tid, _lk, _sk):
            return (f"/api/v2/setup/seasons/{tid}/archive", {"reason": "axis"},
                    [("season", tid)])

        def reopen_call(tid, _lk, _sk):
            return (f"/api/v2/setup/seasons/{tid}/reopen", {"reason": "axis"},
                    [("season", tid)])

        add("POST /api/v2/setup/seasons/<id>/archive", "season", "season",
            self._ax_season, archive_call, ctx=_season_names_itself)
        add("POST /api/v2/setup/seasons/<id>/reopen", "season", "season",
            self._ax_archived_season, reopen_call, ctx=_season_names_itself)

        # -- the venue-access grant's SEASON argument (generic, Season-bound)
        def grant_season_call(tid, _lk, _sk):
            return (f"/api/v2/setup/seasons/{tid}/venue-access",
                    {"venue_id": self._ax_grantable_venue()},
                    [("season", tid)])

        add("POST /api/v2/setup/seasons/<id>/venue-access [SEASON]", "season",
            "season", self._ax_season, grant_season_call,
            ctx=_season_names_itself)

        # -- bridge row: SeasonVenueAccess, judged by its Season ------------
        def access_remove_call(tid, _lk, _sk):
            return (f"/api/v2/setup/season-venue-access/{tid}/remove", {},
                    [("season_venue_access", tid)])

        def access_delete_call(tid, _lk, _sk):
            return (f"/api/v2/setup/season-venue-access/{tid}/delete", {},
                    [("season_venue_access", tid)])

        add("POST /api/v2/setup/season-venue-access/<id>/remove",
            "season_venue_access", "season", self._ax_grant,
            access_remove_call)
        add("POST /api/v2/setup/season-venue-access/<id>/delete",
            "season_venue_access", "season", self._ax_revoked_grant,
            access_delete_call)

        # -- every assign-<target> reassign, BOTH ENDS, per axis ------------
        # (base, path word, assign-<word>, body key, SOURCE kind, source mint,
        #  SOURCE axes, DESTINATION kind, destination mint, DESTINATION axes).
        # The two ends carry DIFFERENT axes and are declared separately: a
        # Division source is Program+Season+League, but a League destination is
        # PERMANENT and names no Season at all, so generating a Season-axis row
        # for it would assert a Season that record provably does not have.
        _L, _LS = ("league",), ("league", "season")
        _S = ("season",)
        reassigns = [
            ("/api/setup", "player", "team", "team_id", "player",
             self._ax_player, _L, "team", self._ax_standing("team"), _L),
            ("/api/v2/setup", "player", "team", "team_id", "player",
             self._ax_player, _L, "team", self._ax_standing("team"), _L),
            ("/api/setup", "team", "club", "club_id", "team", self._ax_team,
             _L, "club", self._ax_standing("club"), _L),
            ("/api/v2/setup", "team", "club", "club_id", "team", self._ax_team,
             _L, "club", self._ax_standing("club"), _L),
            ("/api/v2/setup", "team", "league", "league_id", "team",
             self._ax_team, _L, "league", self._ax_standing("league"), _L),
            ("/api/setup", "division", "level", "level_id", "division",
             self._ax_division, _LS, "league", self._ax_standing("league"),
             _L),
            ("/api/v2/setup", "division", "league", "league_id", "division",
             self._ax_division, _LS, "league", self._ax_standing("league"),
             _L),
            ("/api/setup", "rink", "venue", "venue_id", "rink", self._ax_rink,
             _S, "venue", self._ax_venue, _S),
            ("/api/v2/setup", "rink", "venue", "venue_id", "rink",
             self._ax_rink, _S, "venue", self._ax_venue, _S),
        ]
        for (base, ent, word, key, src_kind, src_mint, src_axes, dest_kind,
             dest_mint, dest_axes) in reassigns:

            def source_call(tid, lk, sk, _b=base, _e=ent, _w=word, _k=key,
                            _sk2=src_kind, _dm=dest_mint):
                # The DESTINATION always comes from the ACTIVE corner, so the
                # only thing a SOURCE row can fail on is its source.
                return (f"{_b}/{_e}/{tid}/assign-{_w}", {_k: _dm(lk, sk)},
                        [(_sk2, tid)])

            def dest_call(tid, lk, sk, _b=base, _e=ent, _w=word, _k=key,
                          _sm=src_mint, _sk2=src_kind, _dk=dest_kind):
                source = _sm(lk, sk)
                return (f"{_b}/{_e}/{source}/assign-{_w}", {_k: tid},
                        [(_dk, tid), (_sk2, source)])

            for axis in src_axes:
                add(f"POST {base}/{ent}/<id>/assign-{word} [SOURCE]", src_kind,
                    axis, src_mint, source_call)
            for axis in dest_axes:
                add(f"POST {base}/{ent}/<id>/assign-{word} [DESTINATION]",
                    dest_kind, axis, dest_mint, dest_call)

        # -- bridge row: SeasonTeamRegistration, judged by its LeagueSeason --
        registration_routes = [
            ("/api/setup", "assign-division", "division_id", "division",
             self._ax_division, _LS),
            ("/api/v2/setup", "assign-division", "division_id", "division",
             self._ax_division, _LS),
            # A League destination is permanent: League axis only.
            ("/api/v2/setup", "assign-league", "league_id", "league",
             self._ax_standing("league"), _L),
        ]
        for base, verb, key, dest_kind, dest_mint, dest_axes in \
                registration_routes:

            def reg_source_call(tid, lk, sk, _b=base, _v=verb, _k=key,
                                _dm=dest_mint):
                return (f"{_b}/season-team-registration/{tid}/{_v}",
                        {_k: _dm(lk, sk)}, [("registration", tid)])

            def reg_dest_call(tid, lk, sk, _b=base, _v=verb, _k=key,
                              _dk=dest_kind):
                source = self._ax_registration(lk, sk)
                return (f"{_b}/season-team-registration/{source}/{_v}",
                        {_k: tid}, [(_dk, tid), ("registration", source)])

            for axis in ("league", "season"):
                add(f"POST {base}/season-team-registration/<id>/{verb} "
                    f"[SOURCE]", "registration", axis, self._ax_registration,
                    reg_source_call)
            for axis in dest_axes:
                add(f"POST {base}/season-team-registration/<id>/{verb} "
                    f"[DESTINATION]", dest_kind, axis, dest_mint,
                    reg_dest_call)

        for base in ("/api/setup", "/api/v2/setup"):
            def remove_call(tid, _lk, _sk, _b=base):
                return (f"{_b}/season-team-registration/{tid}/remove", {},
                        [("registration", tid)])

            for axis in ("league", "season"):
                add(f"POST {base}/season-team-registration/<id>/remove",
                    "registration", axis, self._ax_registration, remove_call)

        def registration_delete_call(tid, _lk, _sk):
            return (f"/api/v2/setup/season-team-registration/{tid}/delete", {},
                    [("registration", tid)])

        for axis in ("league", "season"):
            add("POST /api/v2/setup/season-team-registration/<id>/delete",
                "registration", axis, self._ax_inactive_registration,
                registration_delete_call)
        return rows

    def _run_axis_matrix(self, database_url, backend):
        self._build_axis_world(backend, database_url)
        rows = self._axis_route_rows()
        league_rows = [r for r in rows if r["axis"] == "league"]
        season_rows = [r for r in rows if r["axis"] == "season"]
        # A table that silently shrank would still report OK, having proved
        # nothing about the rows it dropped.
        self.assertGreaterEqual(len(league_rows), 36, "the LEAGUE-axis table "
                                                      "has shrunk")
        self.assertGreaterEqual(len(season_rows), 32, "the SEASON-axis table "
                                                      "has shrunk")
        for row in rows:
            with self.subTest(route=row["name"]):
                self._axis_cases(backend, row)

    def test_axis_route_matrix_memory(self):
        self._run_axis_matrix(None, "memory")

    def test_axis_route_matrix_sqlite(self):
        self._run_axis_matrix(":memory:", "sqlite")

    @unittest.skipUnless(os.environ.get("TEST_DATABASE_URL"),
                         "PostgreSQL required (set TEST_DATABASE_URL)")
    def test_axis_route_matrix_postgres(self):
        self._run_axis_matrix(os.environ["TEST_DATABASE_URL"], "postgres")

    # ----------------------------------------------------------------------
    # "No League" over HTTP: the approved Program + active-Season union.
    # ----------------------------------------------------------------------
    def _run_no_league_union(self, database_url, backend):
        """With NO League selected, a League-B Player update SUCCEEDS (that is
        the union) while a Season-B registration mutation is still refused
        (that is the "only").

        The first half is what stops "No League" from being a dead context —
        it is the state every operator starts in. The second is what stops it
        from becoming an opt-out of the Season ceiling."""
        self._build_axis_world(backend, database_url)
        caller, store = self.ax_caller, self.srv.STATE.api.store
        self._select(caller, self.ax["program"], self.ax["season"]["A"])
        self.assertIsNone(
            (self._req(caller, "GET", "/api/context")[1].get("league")),
            f"[{backend}] fixture: a League is selected, so this is not the "
            f"No-League state under test")

        for lk in ("A", "B"):
            player = self._ax_player(lk, "A")
            self.assertEqual(
                {t[2] for t in chain_axes(store, "player", player)},
                {self.ax["league"][lk]},
                f"[{backend}] fixture: the Player is not in League {lk}")
            status, resp, _raw = self._post(
                caller, f"/api/v2/setup/player/{player}/update",
                {"name": f"AX Union {lk} {self._seq()}"})
            self.assertEqual(
                status, 200,
                f"[{backend}] No League refused a League-{lk} Player inside "
                f"the active Season — No League is not the approved Program + "
                f"active-Season UNION it is defined to be: {resp}")

        victim = self._ax_registration("A", "B")
        self._select(caller, self.ax["program"], self.ax["season"]["A"])
        self.assertEqual(
            {t[1] for t in chain_axes(store, "registration", victim)},
            {self.ax["season"]["B"]},
            f"[{backend}] fixture: the registration is not in Season B")
        before = self._snapshot()
        status, resp, _raw = self._post(
            caller,
            f"/api/v2/setup/season-team-registration/{victim}/remove", {})
        self.assertEqual(
            status, 404,
            f"[{backend}] selecting NO League became a way OUT of the Season "
            f"ceiling: a Season-B registration was mutated from a Season-A "
            f"context ({resp})")
        self.assertEqual(
            resp, {"error": {"code": "not_found",
                             "message": f"Registration {victim} not found."}},
            resp)
        self.assertEqual(self._snapshot(), before,
                         f"[{backend}] the refused call still mutated the store")

    def test_no_league_union_memory(self):
        self._run_no_league_union(None, "memory")

    def test_no_league_union_sqlite(self):
        self._run_no_league_union(":memory:", "sqlite")

    @unittest.skipUnless(os.environ.get("TEST_DATABASE_URL"),
                         "PostgreSQL required (set TEST_DATABASE_URL)")
    def test_no_league_union_postgres(self):
        self._run_no_league_union(os.environ["TEST_DATABASE_URL"], "postgres")

    # ----------------------------------------------------------------------
    # The ONE deliberate exception, over HTTP.
    # ----------------------------------------------------------------------
    def _run_venue_access_exception(self, database_url, backend):
        """``POST /api/v2/setup/seasons/<id>/venue-access`` judges its two
        arguments by two DIFFERENT rules, on purpose.

        The SEASON end is generic and is covered by its own row in the table
        above. The VENUE end is the facility-tree exception: an arena serves
        several leagues, so a Venue linked to ANOTHER Program stays grantable
        (the generic rule deadlocked sharing on its very first use -- once one
        Program held the grant no other Program could ever obtain it). What is
        NOT grantable is a Venue linked to NOTHING and created by a DIFFERENT
        account: somebody's private draft, which leaked by name in an earlier
        round of this work.
        """
        self._build(backend, database_url)
        att, api = self.openers["attacker"], self.srv.STATE.api
        store = api.store
        attacker = (self.uids["attacker"], Role.LEAGUE_ADMIN, {})
        season = self.worlds[self.OWN]["season"]
        path = f"/api/v2/setup/seasons/{season}/venue-access"

        # -- positive: an arena inside the caller's own Program -------------
        # Program AND Season: the SEASON argument of this route is generic and
        # Season-bound (#369 re-review), so a Program-only context would refuse
        # every case below on the Season end and prove nothing about the Venue
        # end this test exists to pin down.
        self._select(att, self.worlds[self.OWN]["program"],
                     self.worlds[self.OWN]["season"])
        mine = self._mint_venue(self.OWN)
        self._ok(att, path, {"venue_id": mine}, "own-Program arena")

        # -- THE EXCEPTION: an established arena in ANOTHER Program ---------
        arena = self._mint_venue(self.VICTIM)
        self.assertEqual(
            chain_programs(store, "venue", arena),
            {self.worlds[self.VICTIM]["program"]},
            "fixture: the shared arena must be linked to the OTHER Program")
        # ...and the GENERIC rule refuses that very record. Asserted here so
        # that if the exception ever quietly becomes the rule everywhere, this
        # test says so instead of passing for the wrong reason.
        self.assertIs(
            api.setup_target_accessible("venue", arena, *attacker), False,
            f"[{backend}] the generic rule no longer refuses a foreign Venue, "
            f"so this route's exception is indistinguishable from it")
        granted = self._ok(att, path, {"venue_id": arena}, "shared arena")
        self.assertEqual(granted.get("venue_id"), arena)

        # -- and the half that must NOT pass: another account's unlinked
        #    private draft ------------------------------------------------
        private = self._ok(self.openers[self.VICTIM], "/api/setup/venue",
                           {"name": f"TA Private Draft {self._seq()}"})["id"]
        self.assertEqual(
            chain_programs(store, "venue", private), set(),
            "fixture: the private draft must be linked to nothing, or the "
            "established-arena clause would carry it")
        self.assertNotIn(
            self.uids["attacker"], created_by(store, "venue", private),
            "fixture: a DIFFERENT account must have created the draft")
        before = self._snapshot()
        status, resp, raw_private = self._post(att, path,
                                               {"venue_id": private})
        self.assertEqual(
            status, 404,
            f"[{backend}] another operator's never-linked private arena was "
            f"accepted into this Season by name ({resp})")
        self.assertEqual(
            resp, {"error": {"code": "not_found",
                             "message": f"Venue {private} not found."}}, resp)
        self.assertEqual(
            self._snapshot(), before,
            f"[{backend}] the refused grant still mutated the store")

        # -- nonexistent: byte-identical to the refusal above ---------------
        absent = f"venue_ta_absent_{self._seq()}"
        status_absent, _r, raw_absent = self._post(att, path,
                                                   {"venue_id": absent})
        self.assertEqual(
            (status, self._blind(raw_private, private)),
            (status_absent, self._blind(raw_absent, absent)),
            f"[{backend}] the venue-access refusal is an existence oracle for "
            f"another account's private drafts")

        # -- the "scope decision, not a blanket block" half. A draft is
        #    creator-gated rather than Program-gated, so the proof is that its
        #    OWN author can finish the two-step flow -- switching Programs
        #    would (correctly) change nothing here.
        self._ok(self.openers[self.VICTIM],
                 f"/api/v2/setup/seasons/{self.worlds[self.VICTIM]['season']}"
                 f"/venue-access", {"venue_id": private},
                 "the creator finishing its own pending grant")

    def test_venue_access_exception_memory(self):
        self._run_venue_access_exception(None, "memory")

    def test_venue_access_exception_sqlite(self):
        self._run_venue_access_exception(":memory:", "sqlite")

    @unittest.skipUnless(os.environ.get("TEST_DATABASE_URL"),
                         "PostgreSQL required (set TEST_DATABASE_URL)")
    def test_venue_access_exception_postgres(self):
        self._run_venue_access_exception(os.environ["TEST_DATABASE_URL"],
                                         "postgres")

    # ----------------------------------------------------------------------
    # The owner's verbatim repro.
    # ----------------------------------------------------------------------
    def _run_owner_repro(self, database_url, backend):
        """An ARENA MANAGER POSTs ``/api/v2/setup/venue/<foreign-id>/delete``.

        Word for word the reported blocker, including the part that is easy to
        forget once the write is stopped: the 200 response ECHOED the foreign
        Venue's own fields back, so the caller learned its NAME even before the
        row disappeared. The role gate was never the problem -- an Arena Manager
        genuinely may delete Venues -- so this identity holds exactly the
        permission the route requires and differs from the owner in one respect
        only: which Program it has selected.
        """
        self._build(backend, database_url)
        store = self.srv.STATE.api.store
        arena, arena_uid = self._account(self.ARENA, Role.ARENA_MANAGER)
        self._select(arena, self.worlds[self.OWN]["program"])

        # A Venue linked to the other Program through the LEGACY
        # ``Venue.league_id`` bridge and carrying no Rink -- the blocker's own
        # shape, and the only Venue shape a dependency gate can actually delete
        # (a granted Venue's grant is itself a delete blocker).
        victim = self._mint_venue(self.VICTIM)
        row = store.get_venue(victim)
        self.assertIsNotNone(row, "fixture: the victim Venue must exist")
        self.assertEqual(
            chain_programs(store, "venue", victim),
            {self.worlds[self.VICTIM]["program"]},
            "fixture: the victim must belong to the OTHER Program")
        self.assertNotIn(
            arena_uid, created_by(store, "venue", victim),
            "fixture: a different account must have created the victim")

        before = self._snapshot()
        status, resp, raw = self._post(
            arena, f"/api/v2/setup/venue/{victim}/delete", {})
        self.assertEqual(
            status, 404,
            f"[{backend}] THE BLOCKER: an Arena Manager deleted a Venue in "
            f"another Program ({resp})")
        self.assertEqual(
            resp, {"error": {"code": "not_found",
                             "message": f"Venue {victim} not found."}}, resp)
        self.assertNotIn(
            row.name.encode(), raw,
            f"[{backend}] the refusal LEAKED the foreign Venue's name back to "
            f"the caller -- the information leak half of the blocker")
        self.assertIsNotNone(
            store.get_venue(victim),
            f"[{backend}] the foreign Venue was deleted anyway")
        self.assertEqual(
            self._snapshot(), before,
            f"[{backend}] the refused delete still wrote to the store or the "
            f"setup audit trail")

        # A scope decision, not a blanket block: the same Arena Manager, the
        # same call, after selecting the Venue's own Program.
        self._select(arena, self.worlds[self.VICTIM]["program"])
        self._ok(arena, f"/api/v2/setup/venue/{victim}/delete", {},
                 "the same call inside the Venue's own Program")

    def test_owner_repro_arena_manager_memory(self):
        self._run_owner_repro(None, "memory")

    def test_owner_repro_arena_manager_sqlite(self):
        self._run_owner_repro(":memory:", "sqlite")

    @unittest.skipUnless(os.environ.get("TEST_DATABASE_URL"),
                         "PostgreSQL required (set TEST_DATABASE_URL)")
    def test_owner_repro_arena_manager_postgres(self):
        self._run_owner_repro(os.environ["TEST_DATABASE_URL"], "postgres")


# ==========================================================================
# PART 3 — the CHECK/USE gap: authorization and mutation must be ONE unit.
#
# The blocker this part closes is not "the gate is wrong" but "the gate is not
# atomic with the write". `setup_target_accessible` is a PREDICATE: it opens a
# snapshot, decides, and CLOSES its transaction. The transport layer then called
# the facade in a SECOND transaction. Between them the target's chain could
# move, and it did: an authorized Program-A delete of Venue V raced a commit
# that moved V into Program B, and the Program-A request answered 200 and
# deleted B's row, having authorized against a world that no longer existed.
# Source/destination reassigns had the identical gap, and the bridge-row parent
# lookup ran outside even the predicate's transaction.
#
# Snapshot isolation is NOT the fix and never was: it makes the chain WALK
# coherent, then ends before the write. The fix is `_guarded_mutation` —
# target lookup, bridge-parent resolution, active-tuple authorization,
# source/destination validation, the mutation and its audit inside ONE
# `store.transaction(isolation="SERIALIZABLE")`, with every named row locked
# (`SELECT ... FOR UPDATE`) as that transaction's FIRST statements, so the
# authorization snapshot is established no earlier than the lock and nothing
# touching a named row can commit between the decision and the write.
#
# How each test below forces the exact interleaving, deterministically and with
# no sleeps: the request is paused with an Event at
# `ApiService.setup_guarded_mutation`'s entry — i.e. AFTER the handler's
# preflight has authorized the target and BEFORE the atomic section opens, the
# precise window the blocker describes. No store lock is held there, so the
# concurrent writer really does commit inside the window on every backend
# (on PostgreSQL from a SECOND, REAL connection; Memory and SQLite are
# single-connection stores by construction, and their process-wide lock is what
# they offer instead).
#
# Each test asserts all four things the owner named: the original request
# refuses GENERICALLY, Program B's row survives untouched, the setup audit
# trail is unchanged, and the moved record's own fields are never serialized
# into the response — compared as BYTES, because a decoded body says nothing
# about what a reader of the socket can see.
#
# The load-bearing element is the IN-TRANSACTION re-authorization: neutralise
# `ApiService._authorize_setup_targets` and every test here fails with the
# request answering 200 and mutating Program B's record.
# ==========================================================================
class SetupTargetAtomicityTest(unittest.TestCase):
    """Authorization and mutation are one transaction (#369 re-review)."""

    OWNER_A = "ta_race_owner_a"
    OWNER_B = "ta_race_owner_b"

    @classmethod
    def setUpClass(cls):
        srv = __import__("hockey_scheduler.web.server", fromlist=["x"])
        cls.srv = srv
        os.environ.pop("DATABASE_URL", None)   # class baseline is Memory
        srv.STATE.reset(seed=False)
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), srv.Handler)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever,
                                      daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.thread.join(timeout=5)

    # -- plumbing (same shape as the route matrix above) --------------------
    def _reset_backend(self, database_url, backend):
        prev = os.environ.get("DATABASE_URL")

        def _set(url):
            if url is None:
                os.environ.pop("DATABASE_URL", None)
            else:
                os.environ["DATABASE_URL"] = url

        _set(database_url)
        try:
            self.srv.STATE.reset(seed=False)
        finally:
            _set(prev)

        def _restore_memory():
            _set(None)
            try:
                self.srv.STATE.reset(seed=False)
            finally:
                _set(prev)

        self.addCleanup(_restore_memory)
        # Prove the store REALLY is the one this variant claims, before
        # asserting anything: a race test that silently ran on InMemoryStore
        # while believing it covered PostgreSQL would look green throughout.
        live = self.srv.STATE.api.store
        if backend == "memory":
            self.assertIsInstance(live, InMemoryStore, type(live).__name__)
        else:
            self.assertIsInstance(live, SqlStore, type(live).__name__)
            self.assertEqual(live.backend, backend,
                             f"the {backend} variant is running on "
                             f"{live.backend!r}")

    def _client(self):
        return urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(CookieJar()))

    def _raw(self, opener, method, path, body=None):
        url = f"http://127.0.0.1:{self.port}{path}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with opener.open(req) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def _post(self, opener, path, body):
        status, raw = self._raw(opener, "POST", path, body)
        return status, json.loads(raw or b"{}"), raw

    def _account(self, username, role):
        account = self.srv.STATE.api.accounts.create_account(
            username, "targetauth-pw", role, scope={}, actor_id="test_seed")
        opener = self._client()
        status, resp, _ = self._post(opener, "/api/auth/login",
                                     {"username": username,
                                      "password": "targetauth-pw"})
        self.assertEqual(status, 200, (username, resp))
        return opener, account.id

    def _select(self, opener, program_id, season_id=None):
        body = {"program_id": program_id}
        if season_id is not None:
            body["season_id"] = season_id
        status, resp, _ = self._post(opener, "/api/context", body)
        self.assertEqual(status, 200, (body, resp))
        self.assertEqual(resp.get("program", {}).get("id"), program_id, resp)
        if season_id is not None:
            self.assertEqual((resp.get("season") or {}).get("id"), season_id,
                             resp)
        return resp

    def _ok(self, opener, path, body, why=""):
        status, resp, _ = self._post(opener, path, body)
        self.assertEqual(status, 200, (why or path, body, resp))
        self.assertNotIn("error", resp, (why or path, resp))
        return resp

    def _audit_rows(self):
        return len(self.srv.STATE.api.store.all_setup_audit())

    # -- the two Programs ---------------------------------------------------
    def _build(self, backend, database_url):
        """Program A (the caller's) and Program B (the concurrent winner's),
        each with a Season, a League and their LeagueSeason binding."""
        self._reset_backend(database_url, backend)
        self.backend = backend
        self.database_url = database_url
        self.openers, self.uids, self.worlds = {}, {}, {}
        for tag, name in ((self.OWNER_A, "A"), (self.OWNER_B, "B")):
            opener, uid = self._account(f"{tag}_{backend}", Role.LEAGUE_ADMIN)
            self.openers[tag], self.uids[tag] = opener, uid
            program = self._ok(opener, "/api/v2/setup/program",
                               {"name": f"TA-race {name} Program"})
            self._select(opener, program["id"])
            season = self._ok(opener, "/api/v2/setup/season",
                              {"program_id": program["id"],
                               "name": f"TA-race {name} Season"})
            self._select(opener, program["id"], season["id"])
            league = self._ok(opener, "/api/v2/setup/league",
                              {"season_id": season["id"],
                               "name": f"TA-race {name} League"})
            binding = self.srv.STATE.api.store.league_season_for(
                league["id"], season["id"])
            self.assertIsNotNone(binding, "fixture: the League/Season binding")
            self.worlds[tag] = {"program": program["id"],
                                "season": season["id"],
                                "league": league["id"],
                                "league_season": binding.id}

    # -- the deterministic barrier -----------------------------------------
    def _writer_store(self):
        """The connection the CONCURRENT writer commits on.

        PostgreSQL gets a SECOND, REAL ``SqlStore`` — a genuinely independent
        connection and transaction, which is the only way to prove the guard
        rather than the process lock. Memory and SQLite are single-connection
        stores by construction (a second ``SqlStore(":memory:")`` would be a
        different, empty database), so the writer commits on the live store;
        the request is paused OUTSIDE the guard's transaction, so nothing is
        held and the write really does land inside the window."""
        if self.backend == "postgres":
            store = SqlStore(self.database_url)
            self.addCleanup(store.close)
            return store
        return self.srv.STATE.api.store

    def _race(self, opener, path, body, concurrent_write):
        """POST ``path``, pause between the preflight and the atomic section,
        run ``concurrent_write(writer_store)`` to completion, release.

        Returns ``(status, decoded, raw)``. Deterministic: two Events, no
        sleeps and no polling — the request cannot proceed until the writer has
        committed, and the writer cannot start until the request has been
        authorized against the pre-move world."""
        api = self.srv.STATE.api
        authorized = threading.Event()
        released = threading.Event()
        orig = api.setup_guarded_mutation
        entered = []

        def paused(*a, **k):
            # Entry = the handler's preflight has ALREADY authorized every
            # target and no transaction is open yet. This is the exact window
            # the blocker describes.
            entered.append(True)
            authorized.set()
            self.assertTrue(released.wait(20), "the writer never released")
            return orig(*a, **k)

        api.setup_guarded_mutation = paused
        self.addCleanup(lambda: setattr(api, "setup_guarded_mutation", orig)
                        if api.setup_guarded_mutation is paused else None)
        out = {}

        def run():
            out["r"] = self._post(opener, path, body)

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        self.assertTrue(
            authorized.wait(20),
            "the request never reached the guard -- it was refused by the "
            "preflight, so this test proved nothing about the race")
        concurrent_write(self._writer_store())      # committed in the window
        released.set()
        thread.join(30)
        self.assertFalse(thread.is_alive(), "the guarded request never returned")
        api.setup_guarded_mutation = orig
        self.assertTrue(entered, "the barrier never fired")
        return out["r"]

    def _assert_refused(self, result, label, record_id, leaked, why):
        """The refusal contract: generic not-found, byte-identical to a
        nonexistent id, and not one field of the moved record echoed back."""
        status, resp, raw = result
        self.assertEqual(status, 404, (why, resp))
        self.assertEqual(
            resp, {"error": {"code": "not_found",
                             "message": f"{label} {record_id} not found."}},
            (why, resp))
        for secret in leaked:
            self.assertNotIn(
                secret.encode(), raw,
                f"{why}: the response SERIALIZED a field of the record that "
                f"had already moved into Program B ({secret!r})")

    # ----------------------------------------------------------------------
    # Race 1 — the owner's verbatim repro: a delete authorized while the Venue
    # belonged to Program A, with the move to Program B committing in between.
    # ----------------------------------------------------------------------
    def _run_venue_delete_race(self, database_url, backend):
        self._build(backend, database_url)
        owner = self.openers[self.OWNER_A]
        a, b = self.worlds[self.OWNER_A], self.worlds[self.OWNER_B]
        # The LEGACY Venue.league_id bridge holds a PROGRAM id: the one Venue
        # shape that is both LINKED to a Program and still deletable (a
        # SeasonVenueAccess grant is itself a delete blocker).
        venue = self._ok(owner, "/api/setup/venue",
                         {"name": "TA-race Arena", "league_id": a["program"]})
        store = self.srv.STATE.api.store
        self.assertEqual(store.get_venue(venue["id"]).league_id, a["program"],
                         "fixture: the Venue starts in Program A")

        def move_to_b(writer):
            row = writer.get_venue(venue["id"])
            row.league_id = b["program"]
            writer.save_venue(row)              # committed: autocommit store

        before_audit = self._audit_rows()
        result = self._race(owner, f"/api/v2/setup/venue/{venue['id']}/delete",
                            {}, move_to_b)
        self._assert_refused(
            result, "Venue", venue["id"], ("TA-race Arena", b["program"]),
            f"[{backend}] a Program-A delete authorized BEFORE the move still "
            f"deleted the Venue out of Program B")
        row = store.get_venue(venue["id"])
        self.assertIsNotNone(
            row, f"[{backend}] Program B's Venue row was deleted by a request "
                 f"that was only ever authorized against Program A")
        self.assertEqual(row.league_id, b["program"],
                         f"[{backend}] Program B's row was modified")
        self.assertEqual(self._audit_rows(), before_audit,
                         f"[{backend}] the refused delete wrote an audit row")

    def test_venue_delete_race_memory(self):
        self._run_venue_delete_race(None, "memory")

    def test_venue_delete_race_sqlite(self):
        self._run_venue_delete_race(":memory:", "sqlite")

    @unittest.skipUnless(os.environ.get("TEST_DATABASE_URL"),
                         "PostgreSQL required (set TEST_DATABASE_URL)")
    def test_venue_delete_race_postgres(self):
        self._run_venue_delete_race(os.environ["TEST_DATABASE_URL"],
                                    "postgres")

    # ----------------------------------------------------------------------
    # Race 2 — a BRIDGE row. A SeasonTeamRegistration carries no Program of its
    # own and is judged by its LeagueSeason. That parent lookup used to run
    # outside even the authorization transaction, so re-pointing it was the
    # cheapest way through the gate.
    # ----------------------------------------------------------------------
    def _run_bridge_parent_race(self, database_url, backend):
        self._build(backend, database_url)
        owner = self.openers[self.OWNER_A]
        a, b = self.worlds[self.OWNER_A], self.worlds[self.OWNER_B]
        club = self._ok(owner, "/api/v2/setup/club", {"name": "TA-race Club"})
        team = self._ok(owner, "/api/v2/setup/team",
                        {"club_id": club["id"], "league_id": a["league"],
                         "name": "TA-race Team"})
        registration = self._ok(
            owner, f"/api/v2/setup/seasons/{a['season']}/team-registrations",
            {"team_id": team["id"], "league_id": a["league"]})
        store = self.srv.STATE.api.store
        self.assertEqual(
            store.get_season_team_registration(
                registration["id"]).league_season_id,
            a["league_season"], "fixture: the registration starts under A")

        def repoint_to_b(writer):
            row = writer.get_season_team_registration(registration["id"])
            row.league_season_id = b["league_season"]
            row.league_id = b["league"]
            writer.save_season_team_registration(row)

        before_audit = self._audit_rows()
        result = self._race(
            owner,
            f"/api/v2/setup/season-team-registration/{registration['id']}"
            f"/remove", {}, repoint_to_b)
        self._assert_refused(
            result, "Registration", registration["id"],
            (b["league_season"], b["league"]),
            f"[{backend}] a registration re-pointed at Program B's "
            f"LeagueSeason was still mutated by the Program-A request")
        row = store.get_season_team_registration(registration["id"])
        self.assertEqual(row.league_season_id, b["league_season"],
                         f"[{backend}] Program B's bridge parent was changed")
        self.assertTrue(row.active,
                        f"[{backend}] the registration was deactivated anyway "
                        f"-- the mutation ran against Program B's row")
        self.assertEqual(self._audit_rows(), before_audit,
                         f"[{backend}] the refused remove wrote an audit row")

    def test_bridge_parent_race_memory(self):
        self._run_bridge_parent_race(None, "memory")

    def test_bridge_parent_race_sqlite(self):
        self._run_bridge_parent_race(":memory:", "sqlite")

    @unittest.skipUnless(os.environ.get("TEST_DATABASE_URL"),
                         "PostgreSQL required (set TEST_DATABASE_URL)")
    def test_bridge_parent_race_postgres(self):
        self._run_bridge_parent_race(os.environ["TEST_DATABASE_URL"],
                                     "postgres")

    # ----------------------------------------------------------------------
    # Race 3 — a reassign's DESTINATION end. Both ends of a move are gated, so
    # both must be locked and re-judged: a destination that leaves the caller's
    # Program mid-request would otherwise receive the moved record.
    # ----------------------------------------------------------------------
    def _run_reassign_destination_race(self, database_url, backend):
        self._build(backend, database_url)
        owner = self.openers[self.OWNER_A]
        a, b = self.worlds[self.OWNER_A], self.worlds[self.OWNER_B]
        source_venue = self._ok(owner, "/api/setup/venue",
                                {"name": "TA-race Source Venue",
                                 "league_id": a["program"]})
        rink = self._ok(owner, "/api/v2/setup/rink",
                        {"venue_id": source_venue["id"],
                         "name": "TA-race Rink"})
        destination = self._ok(owner, "/api/setup/venue",
                               {"name": "TA-race Destination Venue",
                                "league_id": a["program"]})

        def move_destination_to_b(writer):
            row = writer.get_venue(destination["id"])
            row.league_id = b["program"]
            writer.save_venue(row)

        store = self.srv.STATE.api.store
        before_audit = self._audit_rows()
        result = self._race(
            owner, f"/api/v2/setup/rink/{rink['id']}/assign-venue",
            {"venue_id": destination["id"]}, move_destination_to_b)
        self._assert_refused(
            result, "Venue", destination["id"],
            ("TA-race Destination Venue", b["program"]),
            f"[{backend}] a Rink was reassigned INTO a Venue that had already "
            f"moved to Program B")
        self.assertEqual(
            store.get_rink(rink["id"]).venue_id, source_venue["id"],
            f"[{backend}] the Rink was moved even though the refusal was sent")
        self.assertEqual(self._audit_rows(), before_audit,
                         f"[{backend}] the refused reassign wrote an audit row")

    def test_reassign_destination_race_memory(self):
        self._run_reassign_destination_race(None, "memory")

    def test_reassign_destination_race_sqlite(self):
        self._run_reassign_destination_race(":memory:", "sqlite")

    @unittest.skipUnless(os.environ.get("TEST_DATABASE_URL"),
                         "PostgreSQL required (set TEST_DATABASE_URL)")
    def test_reassign_destination_race_postgres(self):
        self._run_reassign_destination_race(os.environ["TEST_DATABASE_URL"],
                                            "postgres")


class SetupGuardedMutationContractTest(unittest.TestCase):
    """The three contracts ``setup_guarded_mutation`` rests on, on every store.

    They are asserted directly because each is invisible in a passing route
    test but fatal if it regresses:

    1. **Reentrant isolation.** The guard opens ONE transaction at the
       strongest level any participant needs (SERIALIZABLE, for the context
       selector's #159 linearizability) and the participants then ask for their
       own — SERIALIZABLE again, and REPEATABLE READ for the chain walk. A join
       asking for the SAME or a WEAKER level is already satisfied and must
       join; one asking for MORE cannot retro-raise an open transaction and
       must still raise, or an inner guarantee would be silently downgraded.
    2. **Identity-less callers stay untouched.** ``role is None`` (the legacy
       internal callers, the demo/full seeds, the acceptance harnesses) must
       run completely ungated, open no transaction and take no lock, so they
       can never be made to wait on — or deadlock with — anybody.
    3. **An error response rolls back.** The facade's ``@catch`` turns a domain
       error into a dict INSIDE the guard's transaction, and a nested
       ``transaction()`` only joins rather than rolling back, so without an
       explicit rollback a half-applied mutation would commit under an error
       body. Before the guard the facade owned the outermost transaction and
       got that rollback from the store; it must still get it.
    """

    ACTOR = "user_guard_contract"

    def test_a_nested_join_may_not_raise_the_isolation(self):
        for backend, store in _backends():
            with self.subTest(backend=backend):
                if isinstance(store, SqlStore):
                    with store.transaction(isolation="SERIALIZABLE"):
                        with store.transaction(isolation="SERIALIZABLE"):
                            with store.transaction(isolation="REPEATABLE READ"):
                                with store.transaction():
                                    pass
                    with self.assertRaises(RuntimeError):
                        with store.transaction(isolation="REPEATABLE READ"):
                            with store.transaction(isolation="SERIALIZABLE"):
                                pass
                    with self.assertRaises(RuntimeError):
                        with store.transaction():
                            with store.transaction(isolation="REPEATABLE READ"):
                                pass
                    self.assertEqual(store._txn_depth, 0)
                    self.assertIsNone(store._txn_isolation)
                else:
                    # The in-memory store documents isolation as a no-op: its
                    # process-wide lock already gives the strongest level.
                    with store.transaction(isolation="SERIALIZABLE"):
                        with store.transaction(isolation="REPEATABLE READ"):
                            pass
                _close(store)

    def test_an_identityless_caller_opens_no_transaction_and_takes_no_lock(self):
        for backend, store in _backends():
            with self.subTest(backend=backend):
                api = ApiService(store)
                world = _facade_world(api, store, "A", self.ACTOR)
                seen = {}

                def mutation():
                    seen["depth"] = store._txn_depth
                    return {"ran": True}

                payload, refused = api.setup_guarded_mutation(
                    [("venue", world["venue"], "scope")], mutation,
                    None, None, {})
                self.assertEqual(payload, {"ran": True},
                                 f"[{backend}] the ungated mutation did not run")
                self.assertIsNone(refused, backend)
                self.assertEqual(
                    seen["depth"], 0,
                    f"[{backend}] an identity-less caller was wrapped in a "
                    f"transaction it never asked for -- the path that must "
                    f"stay lock-free and deadlock-free")
                _close(store)

    def test_an_error_payload_rolls_the_whole_unit_back(self):
        for backend, store in _backends():
            with self.subTest(backend=backend):
                api = ApiService(store)
                world = _facade_world(api, store, "A", self.ACTOR)
                api.set_active_context(self.ACTOR, Role.LEAGUE_ADMIN, {},
                                       world["program"], world["season"])
                planted = f"club_planted_{backend}"

                def mutation():
                    # A write the facade would have rolled back when IT owned
                    # the outermost transaction, followed by the error dict its
                    # own @catch produces.
                    api.setup._audit("club_created", "club", planted,
                                     self.ACTOR, None)
                    return {"error": {"code": "validation_error",
                                      "message": "nope"}}

                before = len(store.all_setup_audit())
                payload, refused = api.setup_guarded_mutation(
                    [("venue", world["venue"], "scope")], mutation,
                    self.ACTOR, Role.LEAGUE_ADMIN, {})
                self.assertIsNone(refused, backend)
                self.assertEqual(payload["error"]["code"], "validation_error",
                                 (backend, payload))
                self.assertEqual(
                    len(store.all_setup_audit()), before,
                    f"[{backend}] the mutation's write COMMITTED under an "
                    f"error response -- the nested transaction only joined, so "
                    f"nothing rolled it back")
                _close(store)


# ==========================================================================
# Parent-axis CONSISTENCY (#369 re-review 2, blocking fail-open).
#
# The edge resolver trusted each record's independently persisted parent
# references without checking that they AGREE. The columns involved are
# separate FKs with no database constraint tying them together — nothing stops
# a persisted Team from carrying ``program_id = A`` while its ``league_id``
# names a League in Program B, or an A-Season LeagueSeason from naming the B
# League. Legacy data, imports, migrations or a prior partial write can all
# produce these shapes. Under Program A / Season A / **No League** the League
# comparison is legitimately skipped (No League = the approved Program +
# active-Season union), so the disagreeing edge authorized and every guarded
# mutation that trusted it could act on a row whose persisted parent graph
# crosses the Program ceiling.
#
# The rule now: while resolving an edge, every named parent is independently
# resolved and the axes must be MUTUALLY CONSISTENT before the edge is
# emitted. A found-but-inconsistent (or dangling) chain is LINKED BUT
# UNAUTHORIZED — ``(set(), True)`` — so it can neither authorize nor fall
# through to creator ownership / No-League union semantics.
#
# Every case below runs under Program A / Season A / No League — the exact
# reproduced conditions — over authenticated HTTP, on Memory, FILE-BACKED
# SQLite and PostgreSQL. Each corrupt shape is materialized directly in the
# store (FK-valid: every referenced row exists), then:
#   * the mutation must be RAW-BODY-EQUIVALENT to a nonexistent id (the
#     ``_blind`` masking used by the route matrix),
#   * the row and the setup audit trail must be unchanged,
#   * a clean in-scope sibling must still succeed (so a blanket refusal
#     cannot pass these for free).
#
# Mutation-proof (run manually, documented in the PR discussion): disabling
# any one of the three consistency checks in ``_team_edges`` /
# ``_setup_target_edges`` ("league_season", "game") makes its shape's cases
# fail on every backend.
# ==========================================================================
class SetupTargetAxisConsistencyTest(unittest.TestCase):
    """Inconsistent cross-Program parent references must fail closed."""

    OWNER_A = "ax_owner_a"
    OWNER_B = "ax_owner_b"

    @classmethod
    def setUpClass(cls):
        srv = __import__("hockey_scheduler.web.server", fromlist=["x"])
        cls.srv = srv
        os.environ.pop("DATABASE_URL", None)
        srv.STATE.reset(seed=False)
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), srv.Handler)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever,
                                      daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.thread.join(timeout=5)

    # -- plumbing (same shape as the atomicity class above) -----------------
    def _reset_backend(self, database_url, backend):
        prev = os.environ.get("DATABASE_URL")

        def _set(url):
            if url is None:
                os.environ.pop("DATABASE_URL", None)
            else:
                os.environ["DATABASE_URL"] = url

        _set(database_url)
        try:
            self.srv.STATE.reset(seed=False)
        finally:
            _set(prev)

        def _restore_memory():
            _set(None)
            try:
                self.srv.STATE.reset(seed=False)
            finally:
                _set(prev)

        self.addCleanup(_restore_memory)
        live = self.srv.STATE.api.store
        if backend == "memory":
            self.assertIsInstance(live, InMemoryStore, type(live).__name__)
        else:
            self.assertIsInstance(live, SqlStore, type(live).__name__)
            self.assertEqual(live.backend, backend,
                             f"the {backend} variant is running on "
                             f"{live.backend!r}")

    def _client(self):
        return urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(CookieJar()))

    def _raw(self, opener, method, path, body=None):
        url = f"http://127.0.0.1:{self.port}{path}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with opener.open(req) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def _post(self, opener, path, body):
        status, raw = self._raw(opener, "POST", path, body)
        return status, json.loads(raw or b"{}"), raw

    def _account(self, username, role):
        account = self.srv.STATE.api.accounts.create_account(
            username, "axisconsist-pw", role, scope={}, actor_id="test_seed")
        opener = self._client()
        status, resp, _ = self._post(opener, "/api/auth/login",
                                     {"username": username,
                                      "password": "axisconsist-pw"})
        self.assertEqual(status, 200, (username, resp))
        return opener, account.id

    def _select(self, opener, program_id, season_id=None):
        body = {"program_id": program_id}
        if season_id is not None:
            body["season_id"] = season_id
        status, resp, _ = self._post(opener, "/api/context", body)
        self.assertEqual(status, 200, (body, resp))
        self.assertEqual(resp.get("program", {}).get("id"), program_id, resp)
        return resp

    def _ok(self, opener, path, body, why=""):
        status, resp, _ = self._post(opener, path, body)
        self.assertEqual(status, 200, (why or path, body, resp))
        self.assertNotIn("error", resp, (why or path, resp))
        return resp

    def _audit_rows(self):
        return len(self.srv.STATE.api.store.all_setup_audit())

    @staticmethod
    def _blind(raw, record_id):
        """The refusal bytes with the echoed id masked out — see the route
        matrix's ``_blind``: byte-for-byte equality is the contract, because
        any other difference is an existence oracle."""
        return raw.replace(record_id.encode(), b"<the id the caller sent>")

    def _build(self, backend, database_url):
        """Program A (the caller's) and Program B (the foreign graph's),
        each with a Season, a League and their LeagueSeason binding."""
        self._reset_backend(database_url, backend)
        self.backend = backend
        self.openers, self.worlds = {}, {}
        for tag, name in ((self.OWNER_A, "A"), (self.OWNER_B, "B")):
            opener, _uid = self._account(f"{tag}_{backend}", Role.LEAGUE_ADMIN)
            self.openers[tag] = opener
            program = self._ok(opener, "/api/v2/setup/program",
                               {"name": f"AX {name} Program"})
            self._select(opener, program["id"])
            season = self._ok(opener, "/api/v2/setup/season",
                              {"program_id": program["id"],
                               "name": f"AX {name} Season"})
            self._select(opener, program["id"], season["id"])
            league = self._ok(opener, "/api/v2/setup/league",
                              {"season_id": season["id"],
                               "name": f"AX {name} League"})
            binding = self.srv.STATE.api.store.league_season_for(
                league["id"], season["id"])
            self.assertIsNotNone(binding, "fixture: the League/Season binding")
            self.worlds[tag] = {"program": program["id"],
                                "season": season["id"],
                                "league": league["id"],
                                "league_season": binding.id}

    def _assert_foreign_equivalent(self, opener, path_for, body, label,
                                   record_id, why):
        """POST the mutation at the corrupt record AND at a nonexistent id;
        the two answers must be 404 and byte-identical after masking only the
        id the caller sent."""
        ghost = f"{label}_does_not_exist"
        g_status, _g_resp, g_raw = self._post(opener, path_for(ghost), body)
        status, resp, raw = self._post(opener, path_for(record_id), body)
        self.assertEqual(status, 404, (why, resp))
        self.assertEqual(g_status, 404, (why, "nonexistent probe", g_raw))
        self.assertEqual(
            self._blind(raw, record_id), self._blind(g_raw, ghost),
            f"{why}: the corrupt-graph refusal is distinguishable from a "
            f"nonexistent id — an existence oracle for the foreign graph")

    # -- the driver ---------------------------------------------------------
    def _run_axis_consistency(self, database_url, backend):
        self._build(backend, database_url)
        owner = self.openers[self.OWNER_A]
        a, b = self.worlds[self.OWNER_A], self.worlds[self.OWNER_B]
        store = self.srv.STATE.api.store
        # The exact reproduced conditions: Program A / Season A / NO League —
        # the union context whose League comparison is legitimately skipped.
        self._select(owner, a["program"], a["season"])

        # ================= SHAPE 1: Team (+ Player propagation) ============
        # team.program_id stays A; team.league_id is re-pointed at B's League.
        # FK-valid: both rows exist. The axes disagree.
        control_team = self._ok(owner, "/api/v2/setup/team",
                                {"league_id": a["league"],
                                 "name": "AX Control Team"})
        corrupt_team = self._ok(owner, "/api/v2/setup/team",
                                {"league_id": a["league"],
                                 "name": "AX Corrupt Team"})
        passenger = self._ok(owner, "/api/v2/setup/player",
                             {"team_id": corrupt_team["id"],
                              "name": "AX Passenger Player",
                              "position": "forward"})
        row = store.get_team(corrupt_team["id"])
        self.assertEqual(row.program_id, a["program"],
                         "fixture: the Team's own Program stays A")
        row.league_id = b["league"]
        store.save_team(row)

        self._ok(owner, f"/api/v2/setup/team/{control_team['id']}/assign-club",
                 {"club_id": None},
                 why=f"[{backend}] clean in-scope Team control")

        before = self._audit_rows()
        self._assert_foreign_equivalent(
            owner, lambda i: f"/api/v2/setup/team/{i}/assign-club",
            {"club_id": None}, "team", corrupt_team["id"],
            f"[{backend}] a Team whose league_id crosses into Program B was "
            f"still mutable under No League")
        after_row = store.get_team(corrupt_team["id"])
        self.assertEqual(after_row.league_id, b["league"],
                         f"[{backend}] the corrupt Team row was changed")
        self.assertEqual(self._audit_rows(), before,
                         f"[{backend}] the refused Team mutation wrote audit")

        # Player propagation: the Player inherits its Team's (inconsistent)
        # chain verbatim, so the same mutation class must refuse identically.
        self._assert_foreign_equivalent(
            owner, lambda i: f"/api/v2/setup/player/{i}/update",
            {"name": "AX Renamed"}, "player", passenger["id"],
            f"[{backend}] a Player on the inconsistent Team was still "
            f"editable under No League")
        self.assertEqual(store.get_player(passenger["id"]).name,
                         "AX Passenger Player",
                         f"[{backend}] the passenger Player was renamed")

        # ================= SHAPE 2: LeagueSeason (+ Division, Registration) =
        # A's LeagueSeason keeps season_id = A-Season but names B's League.
        division = self._ok(owner, "/api/v2/setup/division",
                            {"league_id": a["league"], "name": "AX Division",
                             "season_id": a["season"]})
        control_div = self._ok(owner, "/api/v2/setup/division",
                               {"league_id": a["league"],
                                "name": "AX Control Division",
                                "season_id": a["season"]})
        registration = self._ok(
            owner, f"/api/v2/setup/seasons/{a['season']}/team-registrations",
            {"team_id": control_team["id"], "league_id": a["league"]})
        ls = store.get_league_season(a["league_season"])
        self.assertEqual(ls.season_id, a["season"],
                         "fixture: the LeagueSeason's Season stays A")
        ls.league_id = b["league"]
        store.save_league_season(ls)

        before = self._audit_rows()
        self._assert_foreign_equivalent(
            owner, lambda i: f"/api/v2/setup/league-season/{i}/delete",
            {}, "league_season", a["league_season"],
            f"[{backend}] an A-Season LeagueSeason naming B's League was "
            f"still deletable under No League")
        self.assertIsNotNone(store.get_league_season(a["league_season"]),
                             f"[{backend}] the corrupt LeagueSeason was "
                             f"deleted")

        self._assert_foreign_equivalent(
            owner, lambda i: f"/api/v2/setup/division/{i}/delete",
            {}, "division", division["id"],
            f"[{backend}] a Division under the inconsistent LeagueSeason was "
            f"still deletable under No League")
        self.assertIsNotNone(store.get_division(division["id"]),
                             f"[{backend}] the Division was deleted")

        self._assert_foreign_equivalent(
            owner,
            lambda i: f"/api/v2/setup/season-team-registration/{i}/remove",
            {}, "registration", registration["id"],
            f"[{backend}] a registration judged by the inconsistent "
            f"LeagueSeason was still removable under No League")
        reg_row = store.get_season_team_registration(registration["id"])
        self.assertTrue(reg_row.active,
                        f"[{backend}] the registration was deactivated")
        self.assertEqual(self._audit_rows(), before,
                         f"[{backend}] a refused LeagueSeason-shape mutation "
                         f"wrote audit")

        # Repair the binding so the clean control still proves the refusals
        # above were the CONSISTENCY check, not a blanket block.
        ls = store.get_league_season(a["league_season"])
        ls.league_id = a["league"]
        store.save_league_season(ls)
        self._ok(owner, f"/api/v2/setup/division/{control_div['id']}/delete",
                 {}, why=f"[{backend}] clean in-scope Division control")

        # ================= SHAPE 3: Game ===================================
        # game.season_id = A-Season while game.league_id names B's League.
        from hockey_scheduler.domain.models import Game
        start = datetime(2027, 1, 9, 18, 0, tzinfo=timezone.utc)
        # Draft games, so the CONTROL delete is reachable (only a draft may be
        # deleted); the corrupt twin must be refused by the GATE, upstream of
        # that rule.
        control_game = Game(id=store.next_id("game"),
                            home_team_id=control_team["id"],
                            start_time=start,
                            away_team_id=corrupt_team["id"],
                            season_id=a["season"], league_id=a["league"],
                            is_draft=True)
        store.add_game(control_game)
        corrupt_game = Game(id=store.next_id("game"),
                            home_team_id=control_team["id"],
                            start_time=start + timedelta(days=1),
                            away_team_id=corrupt_team["id"],
                            season_id=a["season"], league_id=b["league"],
                            is_draft=True)
        store.add_game(corrupt_game)

        self._ok(owner, f"/api/v2/setup/game/{control_game.id}/delete", {},
                 why=f"[{backend}] clean in-scope Game control")

        before = self._audit_rows()
        self._assert_foreign_equivalent(
            owner, lambda i: f"/api/v2/setup/game/{i}/delete",
            {}, "game", corrupt_game.id,
            f"[{backend}] an A-Season Game naming B's League was still "
            f"deletable under No League")
        self.assertIsNotNone(store.get_game(corrupt_game.id),
                             f"[{backend}] the corrupt Game was deleted")
        self.assertEqual(self._audit_rows(), before,
                         f"[{backend}] the refused Game delete wrote audit")

    def test_axis_consistency_memory(self):
        self._run_axis_consistency(None, "memory")

    def test_axis_consistency_sqlite_file(self):
        # FILE-BACKED on purpose (#369 re-review): a second connection must be
        # able to open the same database, and ":memory:" cannot express the
        # real cross-connection semantics this suite exists to prove.
        tmp = tempfile.mkdtemp(prefix="hs-axis-")
        path = os.path.join(tmp, "axis.db")
        self._run_axis_consistency(f"sqlite:///{path}", "sqlite")

    @unittest.skipUnless(os.environ.get("TEST_DATABASE_URL"),
                         "PostgreSQL required (set TEST_DATABASE_URL)")
    def test_axis_consistency_postgres(self):
        self._run_axis_consistency(os.environ["TEST_DATABASE_URL"],
                                   "postgres")


# ==========================================================================
# A GAME'S FOUR PARENTS, EACH VALIDATED INDEPENDENTLY (#372).
#
# The blocker `SetupTargetAxisConsistencyTest` above did NOT catch: the gate
# resolved a Game's League by PRECEDENCE -- explicit `league_id`, else via
# `league_season_id`, else via `division_id` -- and then validated only that
# one winner against the Game's Season. So a legitimate Program-A `league_id`
# HID the other two parents: a `league_season_id` or `division_id` pointing
# into Program B (or into a different Season of the same Program) was never
# examined at all, and `POST /api/{,v2/}setup/game/<id>/delete` deleted a Game
# whose persisted parent graph straddled the ceiling.
#
# The shape above varies `game.league_id` -- the WINNER of that precedence --
# which is why it passed while the losers went unchecked. This class varies
# each LOSER in turn, on both axes:
#
#   * a valid Program-A `league_id` + a `league_season_id` in Program B
#   * a valid Program-A `league_id` + a `division_id` in Program B
#   * both again with the foreign parent in another SEASON of the SAME
#     Program, so the Program ceiling cannot explain the refusal
#   * every parent column dangling in turn (the `saw_link` fail-closed rule)
#   * the fully consistent Game, which must still SUCCEED -- the fix is a
#     scope decision, not a blanket block on Games carrying several parents.
#
# Every precondition is read out of the STORED ROWS (not out of
# `_setup_target_edges`, and not out of `chain_axes`, which mirrors the OLD
# first-match walk on purpose): the parents really do disagree, and in the
# disagreement cases every referenced row really exists, so no refusal below
# can be explained by a dangling id instead of by the disagreement.
# ==========================================================================

def _game_parent_world(api, store, tag, actor):
    """One Program with TWO Seasons, one PERMANENT League bound to BOTH, and a
    Division under each binding.

    Two Seasons per Program are what make the same-Program/other-Season half of
    this class expressible: the foreign parent then differs from the Game's own
    Season in the SEASON axis alone -- same Program, same League -- so no
    refusal it produces can be explained by the Program ceiling that the rest
    of this file already proves."""
    program = api.create_program(f"GP-{tag} Program", "US", "UTC", None, actor)
    assert "error" not in program, program
    seasons = []
    for n in (1, 2):
        season = api.create_season(program["id"], f"GP-{tag} Season {n}",
                                   actor_id=actor)
        assert "error" not in season, season
        seasons.append(season)
    league = api.create_league(seasons[0]["id"], f"GP-{tag} League", 0, actor)
    assert "error" not in league, league
    bindings, divisions = [], []
    for index, season in enumerate(seasons):
        if index == 0:
            binding = store.league_season_for(league["id"], season["id"])
        else:
            # The permanent League joins a SECOND Season (#283 rule 5). There
            # is no v1/v2 route for binding an existing League to another
            # Season, so the product's own service method does it.
            binding = api.setup.create_league_season(league["id"],
                                                     season["id"], actor)
        assert binding is not None, (tag, season["id"])
        bindings.append(binding.id)
        division = api.create_division_v2(league["id"],
                                          f"GP-{tag} Division {index + 1}", "",
                                          actor, season_id=season["id"])
        assert "error" not in division, division
        divisions.append(division["id"])
    club = api.create_club(f"GP-{tag} Club", "", actor)
    team = api.create_team(club["id"], None, f"GP-{tag} Team", actor,
                           league_id=league["id"])
    assert "error" not in team, team
    return {"program": program["id"],
            "seasons": [season["id"] for season in seasons],
            "league": league["id"], "league_seasons": bindings,
            "divisions": divisions, "team": team["id"]}


def _inject_parented_draft_game(store, home_team_id, offset_hours, parents):
    """A draft Game carrying exactly ``parents``.

    Injected rather than created through a route for the same reason every
    other draft in this file is: a draft is minted by the scheduler, never by a
    setup route -- and `create_game` derives the very consistency this class
    exists to violate, so it cannot express these shapes at all."""
    from hockey_scheduler.domain.models import Game
    game = Game(id=store.next_id("game"), home_team_id=home_team_id,
                start_time=_FUTURE + timedelta(hours=offset_hours),
                is_draft=True, **parents)
    store.add_game(game)
    return game.id


def game_parent_axes(store, game):
    """Each NON-NULL parent of ``game``, resolved from the STORED ROWS to the
    ``(program, season, league)`` it names -- or None when it dangles.

    Deliberately NOT `chain_axes`: that helper mirrors the FIRST-MATCH walk and
    therefore reports one League for the whole Game, which is exactly the
    collapse under test. This one keeps the parents apart, so a precondition
    here can state "these two parents disagree" as a fact about the database
    rather than as an opinion of the code being measured."""
    def _binding(league_season_id):
        ls = store.get_league_season(league_season_id)
        if ls is None:
            return None
        season = store.get_season(ls.season_id) if ls.season_id else None
        league = store.get_league(ls.league_id) if ls.league_id else None
        if season is None or league is None:
            return None
        return (season.program_id, season.id, league.id)

    axes = {}
    if game.season_id:
        season = store.get_season(game.season_id)
        axes["season_id"] = (None if season is None
                             else (season.program_id, season.id, None))
    if game.league_id:
        league = store.get_league(game.league_id)
        axes["league_id"] = (None if league is None
                             else (league.program_id, None, league.id))
    if getattr(game, "league_season_id", None):
        axes["league_season_id"] = _binding(game.league_season_id)
    if game.division_id:
        division = store.get_division(game.division_id)
        axes["division_id"] = (
            None if division is None or not division.league_season_id
            else _binding(division.league_season_id))
    return axes


class SetupTargetGameParentConsistencyTest(unittest.TestCase):
    """#372, the PREDICATE leg: every Game parent on every backend."""

    OWNER_A = "gp_owner_a"
    OWNER_B = "gp_owner_b"
    CALLER = "gp_caller"

    # -- the shapes, and the precondition each one has to establish ----------
    #
    # (label, parent overrides, expected verdict, precondition). The base row
    # is Program A / Season 1 / League A, CONSISTENT across all four columns;
    # every shape moves exactly ONE parent, so nothing else can explain its
    # outcome.
    def _shapes(self, a, b):
        consistent = {"season_id": a["seasons"][0],
                      "league_id": a["league"],
                      "league_season_id": a["league_seasons"][0],
                      "division_id": a["divisions"][0]}
        return [
            ("the fully consistent Game", consistent, True,
             ("consistent", None)),
            ("a valid Program-A league_id hiding a Program-B "
             "league_season_id",
             {**consistent, "league_season_id": b["league_seasons"][0]},
             False, ("disagree", "program")),
            ("a valid Program-A league_id hiding a Program-B division_id",
             {**consistent, "division_id": b["divisions"][0]},
             False, ("disagree", "program")),
            ("a valid Season-1 parent set hiding a Season-2 "
             "league_season_id",
             {**consistent, "league_season_id": a["league_seasons"][1]},
             False, ("disagree", "season")),
            ("a valid Season-1 parent set hiding a Season-2 division_id",
             {**consistent, "division_id": a["divisions"][1]},
             False, ("disagree", "season")),
            ("a DANGLING league_season_id",
             {**consistent, "league_season_id": "gp_league_season_vanished"},
             False, ("dangling", "league_season_id")),
            ("a DANGLING division_id",
             {**consistent, "division_id": "gp_division_vanished"},
             False, ("dangling", "division_id")),
            ("a DANGLING league_id",
             {**consistent, "league_id": "gp_league_vanished"},
             False, ("dangling", "league_id")),
            ("a DANGLING season_id",
             {**consistent, "season_id": "gp_season_vanished"},
             False, ("dangling", "season_id")),
        ]

    def _assert_precondition(self, store, game_id, parents, precondition,
                             where):
        """The fixture really is what its label says, read from the store.

        Without this, every refusal below is satisfied by a gate that refuses
        Games outright, by a store that silently dropped a parent column, or by
        a fixture whose "foreign" parent turned out to be a dangling id."""
        game = store.get_game(game_id)
        self.assertIsNotNone(game, f"{where}: the fixture Game was not stored")
        for column, value in parents.items():
            self.assertEqual(
                getattr(game, column), value,
                f"{where}: the store did not persist {column} -- every "
                f"assertion about this shape would be vacuous")
        axes = game_parent_axes(store, game)
        self.assertEqual(
            set(axes), set(parents),
            f"{where}: the resolved parents {sorted(axes)} are not the four "
            f"columns the fixture set")
        rule, subject = precondition

        if rule == "dangling":
            self.assertIsNone(
                axes[subject],
                f"{where}: {subject} RESOLVES, so this is not the dangling "
                f"case it claims to be")
            for column, resolved in axes.items():
                if column != subject:
                    self.assertIsNotNone(
                        resolved,
                        f"{where}: {column} dangles too, so a refusal cannot "
                        f"be attributed to {subject}")
            return

        for column, resolved in axes.items():
            self.assertIsNotNone(
                resolved,
                f"{where}: the {column} parent does not resolve -- a refusal "
                f"would be the DANGLING rule, not the disagreement rule")
        programs = {resolved[0] for resolved in axes.values()}
        seasons = {resolved[1] for resolved in axes.values()
                   if resolved[1] is not None}
        leagues = {resolved[2] for resolved in axes.values()
                   if resolved[2] is not None}

        if rule == "consistent":
            self.assertEqual(
                (len(programs), len(seasons), len(leagues)), (1, 1, 1),
                f"{where}: the control's own parents disagree "
                f"({programs}/{seasons}/{leagues}), so its acceptance would "
                f"prove nothing")
            return

        self.assertEqual(rule, "disagree", rule)
        if subject == "program":
            self.assertEqual(
                len(programs), 2,
                f"{where}: the parents agree on Program ({programs}) -- the "
                f"fixture does not reproduce the reported blocker")
        else:
            self.assertEqual(
                len(programs), 1,
                f"{where}: the parents ALSO cross the Program ceiling "
                f"({programs}), so a refusal could be the Program rule rather "
                f"than the Season rule this shape is about")
            self.assertEqual(
                len(seasons), 2,
                f"{where}: the parents agree on Season ({seasons}) -- this "
                f"shape does not vary the axis it claims to")

    def test_every_game_parent_is_validated_on_each_backend(self):
        for backend, store in _backends():
            with self.subTest(backend=backend):
                api = ApiService(store)
                caller = (self.CALLER, Role.LEAGUE_ADMIN, {})
                a = _game_parent_world(api, store, "A", self.OWNER_A)
                b = _game_parent_world(api, store, "B", self.OWNER_B)
                # Program A + Season 1 + NO League: the union selection whose
                # League comparison is legitimately skipped, and the exact
                # context the blocker was reported under. A refusal here
                # therefore cannot come from an unmatched League component.
                api.set_active_context(*caller, a["program"], a["seasons"][0])

                for offset, (label, parents, expected, precondition) in \
                        enumerate(self._shapes(a, b)):
                    where = f"[{backend}] {label}"
                    game_id = _inject_parented_draft_game(
                        store, a["team"], offset, parents)
                    self._assert_precondition(store, game_id, parents,
                                              precondition, where)
                    # An injected draft has NO creation audit row, so rule 6
                    # (unlinked -> creator only) can never explain a True.
                    self.assertEqual(
                        created_by(store, "game", game_id), set(),
                        f"{where}: the injected Game has a creator audit row, "
                        f"so an acceptance could come from creator ownership "
                        f"rather than from its parent chain")
                    self.assertIs(
                        api.setup_target_accessible("game", game_id, *caller),
                        expected,
                        f"{where}: expected accessible={expected}. A Game "
                        f"whose parents disagree is linked-but-UNAUTHORIZED; "
                        f"a Game whose parents all agree must stay manageable")
                # The gate is still a scope decision on this backend: a
                # nonexistent Game and the foreign world's consistent Game both
                # fail closed, and switching context reaches the latter.
                foreign = _inject_parented_draft_game(
                    store, b["team"], 90,
                    {"season_id": b["seasons"][0], "league_id": b["league"],
                     "league_season_id": b["league_seasons"][0],
                     "division_id": b["divisions"][0]})
                self.assertIs(
                    api.setup_target_accessible("game", foreign, *caller),
                    False, f"[{backend}] a Program-B Game was accessible")
                self.assertIs(
                    api.setup_target_accessible("game", "gp_no_such_game",
                                                *caller),
                    False, f"[{backend}] a nonexistent Game did not fail "
                           f"closed")
                api.set_active_context(*caller, b["program"], b["seasons"][0])
                self.assertIs(
                    api.setup_target_accessible("game", foreign, *caller),
                    True,
                    f"[{backend}] selecting the Game's OWN Program/Season did "
                    f"not make it accessible -- the refusals above are a "
                    f"blanket block on multi-parent Games, not a scope "
                    f"decision")
                _close(store)


class SetupTargetGameParentConsistencyHttpTest(unittest.TestCase):
    """#372 over AUTHENTICATED v1 and v2 HTTP, on every backend.

    Same shapes as the predicate class above, driven through
    ``POST /api/setup/game/<id>/delete`` and
    ``POST /api/v2/setup/game/<id>/delete`` -- the two routes the blocker was
    reported against. Each refusal is compared as RAW BYTES with the same call
    against a nonexistent Game id (only the echoed id masked), and the Game row
    and setup-audit trail are asserted unchanged afterwards."""

    OWNER_A = "gpx_owner_a"
    OWNER_B = "gpx_owner_b"

    @classmethod
    def setUpClass(cls):
        srv = __import__("hockey_scheduler.web.server", fromlist=["x"])
        cls.srv = srv
        os.environ.pop("DATABASE_URL", None)
        srv.STATE.reset(seed=False)
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), srv.Handler)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever,
                                      daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.thread.join(timeout=5)

    # -- plumbing (the axis-consistency class's shape, verbatim) ------------
    _reset_backend = SetupTargetAxisConsistencyTest._reset_backend
    _client = SetupTargetAxisConsistencyTest._client
    _raw = SetupTargetAxisConsistencyTest._raw
    _post = SetupTargetAxisConsistencyTest._post
    _select = SetupTargetAxisConsistencyTest._select
    _ok = SetupTargetAxisConsistencyTest._ok
    _audit_rows = SetupTargetAxisConsistencyTest._audit_rows
    _blind = staticmethod(SetupTargetAxisConsistencyTest._blind)
    _shapes = SetupTargetGameParentConsistencyTest._shapes
    _assert_precondition = \
        SetupTargetGameParentConsistencyTest._assert_precondition

    def _account(self, username, role):
        account = self.srv.STATE.api.accounts.create_account(
            username, "gameparent-pw", role, scope={}, actor_id="test_seed")
        opener = self._client()
        status, resp, _ = self._post(opener, "/api/auth/login",
                                     {"username": username,
                                      "password": "gameparent-pw"})
        self.assertEqual(status, 200, (username, resp))
        return opener, account.id

    def _world(self, opener, tag):
        """The HTTP twin of ``_game_parent_world``: two Seasons, one permanent
        League bound to both, a Division under each binding, and a Team."""
        store = self.srv.STATE.api.store
        program = self._ok(opener, "/api/v2/setup/program",
                           {"name": f"GPX {tag} Program"})
        self._select(opener, program["id"])
        seasons = []
        for n in (1, 2):
            season = self._ok(opener, "/api/v2/setup/season",
                              {"program_id": program["id"],
                               "name": f"GPX {tag} Season {n}"})
            seasons.append(season["id"])
        self._select(opener, program["id"], seasons[0])
        league = self._ok(opener, "/api/v2/setup/league",
                          {"season_id": seasons[0],
                           "name": f"GPX {tag} League"})
        bindings, divisions = [], []
        for index, season_id in enumerate(seasons):
            if index == 0:
                binding = store.league_season_for(league["id"], season_id)
            else:
                # No route binds an EXISTING League to a second Season; the
                # product's own service method does it (#283 rule 5).
                binding = self.srv.STATE.api.setup.create_league_season(
                    league["id"], season_id, "test_seed")
            self.assertIsNotNone(binding,
                                 f"fixture: the {tag}/{index} binding")
            bindings.append(binding.id)
            division = self._ok(opener, "/api/v2/setup/division",
                                {"league_id": league["id"],
                                 "season_id": season_id,
                                 "name": f"GPX {tag} Division {index + 1}"})
            divisions.append(division["id"])
        team = self._ok(opener, "/api/v2/setup/team",
                        {"league_id": league["id"], "name": f"GPX {tag} Team"})
        return {"program": program["id"], "seasons": seasons,
                "league": league["id"], "league_seasons": bindings,
                "divisions": divisions, "team": team["id"]}

    def _refused(self, opener, base, game_id, why):
        """The delete is refused, byte-identically to a nonexistent id, and
        writes nothing -- neither the Game row nor the setup-audit trail."""
        store = self.srv.STATE.api.store
        before_audit = self._audit_rows()
        before_row = dict(vars(store.get_game(game_id)))
        ghost = "gpx_game_does_not_exist"
        g_status, g_raw = self._raw(opener, "POST",
                                    f"{base}/game/{ghost}/delete", {})
        status, raw = self._raw(opener, "POST",
                                f"{base}/game/{game_id}/delete", {})
        self.assertEqual(status, 404, (why, base, raw))
        self.assertEqual(g_status, 404, (why, base, "ghost probe", g_raw))
        self.assertEqual(
            self._blind(raw, game_id), self._blind(g_raw, ghost),
            f"{why} [{base}]: the disagreeing-parent refusal is "
            f"distinguishable from a nonexistent id -- an existence oracle")
        row = store.get_game(game_id)
        self.assertIsNotNone(row, f"{why} [{base}]: the Game was DELETED")
        self.assertEqual(dict(vars(row)), before_row,
                         f"{why} [{base}]: the refused delete edited the row")
        self.assertEqual(self._audit_rows(), before_audit,
                         f"{why} [{base}]: the refused delete wrote audit")

    def _run(self, database_url, backend):
        self._reset_backend(database_url, backend)
        store = self.srv.STATE.api.store
        owner_a, _uid_a = self._account(f"{self.OWNER_A}_{backend}",
                                        Role.LEAGUE_ADMIN)
        owner_b, _uid_b = self._account(f"{self.OWNER_B}_{backend}",
                                        Role.LEAGUE_ADMIN)
        a = self._world(owner_a, "A")
        b = self._world(owner_b, "B")
        # The reported context: Program A, Season 1, NO League.
        self._select(owner_a, a["program"], a["seasons"][0])

        offset = 0
        for label, parents, allowed, precondition in self._shapes(a, b):
            for base in ("/api/setup", "/api/v2/setup"):
                where = f"[{backend}] {label}"
                offset += 1
                # A fresh Game per version: the control's delete CONSUMES it,
                # and a refusal must be measured against an untouched row.
                game_id = _inject_parented_draft_game(store, a["team"],
                                                      offset, parents)
                self._assert_precondition(store, game_id, parents,
                                          precondition, where)
                self.assertEqual(
                    created_by(store, "game", game_id), set(),
                    f"{where} [{base}]: the injected Game has a creator audit "
                    f"row, so a 200 could come from creator ownership")
                if allowed:
                    self._ok(owner_a, f"{base}/game/{game_id}/delete", {},
                             why=f"{where} [{base}] consistent-Game control")
                    self.assertIsNone(
                        store.get_game(game_id),
                        f"{where} [{base}]: the control delete answered 200 "
                        f"without deleting anything")
                else:
                    self._refused(owner_a, base, game_id, where)

    def test_game_parent_consistency_memory(self):
        self._run(None, "memory")

    def test_game_parent_consistency_sqlite_file(self):
        # FILE-BACKED, matching the axis-consistency class: ":memory:" cannot
        # express the real cross-connection semantics this suite proves.
        tmp = tempfile.mkdtemp(prefix="hs-gameparent-")
        self._run(f"sqlite:///{os.path.join(tmp, 'gameparent.db')}", "sqlite")

    @unittest.skipUnless(os.environ.get("TEST_DATABASE_URL"),
                         "PostgreSQL required (set TEST_DATABASE_URL)")
    def test_game_parent_consistency_postgres(self):
        self._run(os.environ["TEST_DATABASE_URL"], "postgres")


# ==========================================================================
# IN-TRANSACTION interleaving and the row locks (#369 re-review, blocking
# test gap).
#
# `SetupTargetAtomicityTest` above pauses at `setup_guarded_mutation`'s ENTRY
# — before the transaction opens — so it proves the second, in-transaction
# authorization catches a move that committed BEFORE the atomic section. It
# never puts a writer between the in-transaction decision and `mutation()`,
# and its Memory/SQLite variants share the live store/connection, so the row
# locks themselves were unfalsified: neutralising `_lock_setup_row()` left
# all of its runnable cases green.
#
# This class closes that gap. The barrier here fires INSIDE the outer
# SERIALIZABLE transaction — in `_authorize_setup_targets`, AFTER every named
# row has been locked and every target authorized, and BEFORE `mutation()`
# runs. While the request is paused there, a concurrent writer attempts to
# move/relink a named row:
#
#   * PostgreSQL — a SECOND, REAL `SqlStore` connection. Its single-column
#     UPDATE must BLOCK on the `SELECT ... FOR UPDATE` row lock and commit
#     only after the guarded transaction does. Removing the relevant FOR
#     UPDATE (`_lock_setup_row` → no-op) makes these cases FAIL: the writer
#     commits mid-window, the guarded write then raises a serialization
#     conflict, the bounded retry re-authorizes against the moved row and
#     refuses — observed as a 404 where this test demands the linearized 200.
#   * SQLite — a FILE-BACKED database and a second `SqlStore` connection
#     (":memory:" cannot express cross-connection semantics at all). The
#     writer's autocommit UPDATE needs the file's write lock; the guarded
#     transaction's snapshot holds SHARED, so every mid-window attempt fails
#     with the database-locked error, which the writer treats as "locked out,
#     retry" — never surfaced raw, never a 500 — and first succeeds only
#     after the guarded commit.
#   * Memory — a second THREAD against the live store. Every store call takes
#     the process-wide lock the outermost transaction holds, so the thread
#     provably cannot interleave until the guarded unit exits.
#
# Each case asserts, in order: the writer did NOT commit inside the window;
# the guarded request answered 200 (it was authorized against the world it
# locked, and nothing moved under it); and the FINAL store state is the one
# valid linearization — guarded mutation first, writer second. Shapes: a
# direct target (player in-place update), a bridge row + its parent (a
# registration remove), and a reassign DESTINATION (rink → venue).
#
# `_authorize_setup_targets` neutralisation is deliberately NOT this suite's
# lock proof — that proves the in-transaction recheck is load-bearing (the
# class above), not that the check stays atomic with the write.
# ==========================================================================
class SetupTargetLockAtomicityTest(unittest.TestCase):
    """The check→mutate window is closed BY THE LOCKS, per backend."""

    OWNER_A = "lk_owner_a"
    OWNER_B = "lk_owner_b"
    # How long a writer may keep retrying before the harness calls it hung.
    WRITER_DEADLINE = 20.0
    # The mid-window grace: how long the writer gets to (wrongly) commit
    # while the guarded transaction is paused on the barrier.
    WINDOW_GRACE = 0.4

    @classmethod
    def setUpClass(cls):
        srv = __import__("hockey_scheduler.web.server", fromlist=["x"])
        cls.srv = srv
        os.environ.pop("DATABASE_URL", None)
        srv.STATE.reset(seed=False)
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), srv.Handler)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever,
                                      daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.thread.join(timeout=5)

    # -- plumbing (same shape as the classes above) -------------------------
    def _reset_backend(self, database_url, backend):
        prev = os.environ.get("DATABASE_URL")

        def _set(url):
            if url is None:
                os.environ.pop("DATABASE_URL", None)
            else:
                os.environ["DATABASE_URL"] = url

        _set(database_url)
        try:
            self.srv.STATE.reset(seed=False)
        finally:
            _set(prev)

        def _restore_memory():
            _set(None)
            try:
                self.srv.STATE.reset(seed=False)
            finally:
                _set(prev)

        self.addCleanup(_restore_memory)
        live = self.srv.STATE.api.store
        if backend == "memory":
            self.assertIsInstance(live, InMemoryStore, type(live).__name__)
        else:
            self.assertIsInstance(live, SqlStore, type(live).__name__)
            self.assertEqual(live.backend, backend,
                             f"the {backend} variant is running on "
                             f"{live.backend!r}")

    def _client(self):
        return urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(CookieJar()))

    def _raw(self, opener, method, path, body=None):
        url = f"http://127.0.0.1:{self.port}{path}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with opener.open(req) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def _post(self, opener, path, body):
        status, raw = self._raw(opener, "POST", path, body)
        return status, json.loads(raw or b"{}"), raw

    def _account(self, username, role):
        account = self.srv.STATE.api.accounts.create_account(
            username, "lockatomic-pw", role, scope={}, actor_id="test_seed")
        opener = self._client()
        status, resp, _ = self._post(opener, "/api/auth/login",
                                     {"username": username,
                                      "password": "lockatomic-pw"})
        self.assertEqual(status, 200, (username, resp))
        return opener, account.id

    def _select(self, opener, program_id, season_id=None):
        body = {"program_id": program_id}
        if season_id is not None:
            body["season_id"] = season_id
        status, resp, _ = self._post(opener, "/api/context", body)
        self.assertEqual(status, 200, (body, resp))
        return resp

    def _ok(self, opener, path, body, why=""):
        status, resp, _ = self._post(opener, path, body)
        self.assertEqual(status, 200, (why or path, body, resp))
        self.assertNotIn("error", resp, (why or path, resp))
        return resp

    def _build(self, backend, database_url):
        """Program A (the caller's) and Program B (the writer's target),
        each with a Season, a League, its LeagueSeason binding and one Team."""
        self._reset_backend(database_url, backend)
        self.backend = backend
        self.database_url = database_url
        self.openers, self.worlds = {}, {}
        for tag, name in ((self.OWNER_A, "A"), (self.OWNER_B, "B")):
            opener, _uid = self._account(f"{tag}_{backend}", Role.LEAGUE_ADMIN)
            self.openers[tag] = opener
            program = self._ok(opener, "/api/v2/setup/program",
                               {"name": f"LK {name} Program"})
            self._select(opener, program["id"])
            season = self._ok(opener, "/api/v2/setup/season",
                              {"program_id": program["id"],
                               "name": f"LK {name} Season"})
            self._select(opener, program["id"], season["id"])
            league = self._ok(opener, "/api/v2/setup/league",
                              {"season_id": season["id"],
                               "name": f"LK {name} League"})
            team = self._ok(opener, "/api/v2/setup/team",
                            {"league_id": league["id"],
                             "name": f"LK {name} Team"})
            binding = self.srv.STATE.api.store.league_season_for(
                league["id"], season["id"])
            self.assertIsNotNone(binding, "fixture: the League/Season binding")
            self.worlds[tag] = {"program": program["id"],
                                "season": season["id"],
                                "league": league["id"],
                                "league_season": binding.id,
                                "team": team["id"]}

    # -- the second connection / second thread ------------------------------
    def _writer_store(self):
        """A genuinely independent way to write, per backend.

        PostgreSQL and file-backed SQLite get a SECOND, REAL ``SqlStore`` — a
        separate connection, so the contention observed is the database's, not
        the process lock's. Memory returns the live store: the writer runs on
        a second THREAD inside a REAL ``store.transaction()`` (the way every
        actual mutation writes), and the process-wide lock the outermost
        transaction holds is exactly the mechanism under test there — a raw
        un-transactional poke would bypass the lock and prove nothing."""
        if self.backend in ("postgres", "sqlite"):
            store = SqlStore(self.database_url)
            if store.backend == "sqlite":
                # The sqlite3 driver's default 5s busy-wait would swallow the
                # contention this suite exists to OBSERVE: the mover would sit
                # inside execute() until the guarded commit and the locked-out
                # signal would never fire. Zero it on the WRITER's connection
                # only — the collision then surfaces as the database-locked
                # error the harness records and retries, which is the
                # lock-or-retry contract under test.
                store.conn.execute("PRAGMA busy_timeout = 0")
            self.addCleanup(store.close)
            return store
        return self.srv.STATE.api.store

    def _column_update(self, store, table, assignments, row_id):
        """One targeted UPDATE on the writer's own connection.

        Deliberately NOT read-modify-write through the store's save_* helpers:
        those write every column, so a writer that read before the guarded
        commit would clobber the guarded mutation on release and no
        linearization could be asserted at all."""
        cols = ", ".join(f"{c} = ?" for c in assignments)
        query = f"UPDATE {table} SET {cols} WHERE id = ?"
        params = tuple(assignments.values()) + (row_id,)
        if store.backend == "postgres":
            query = query.replace("?", "%s")
        store.conn.execute(query, params)

    # -- the in-transaction barrier -----------------------------------------
    def _race_inside_txn(self, opener, path, body, move):
        """POST ``path``; pause INSIDE the guarded transaction — after every
        named row is locked and every target authorized, before
        ``mutation()`` — run the concurrent mover, prove it cannot commit
        inside the window, release, and return ``(status, decoded, raw)``.

        ``move`` is ``{"table":…, "assignments":…, "row_id":…, "mem_apply":…}``
        — the SQL single-column move and its Memory read-modify-write twin.

        The barrier lives in ``_authorize_setup_targets``: its return is the
        exact point the re-review names — the decision is taken, the locks
        are held, the write has not happened. Per backend the mover behaves
        as that backend's real contention demands:

        * **PostgreSQL** — one UPDATE on the second connection. It BLOCKS on
          the ``FOR UPDATE`` row lock and, being autocommit, has committed
          the moment it returns. With `_lock_setup_row` neutralised it
          commits inside the window instead, and the mid-window assertion
          fails — the locks are what this suite falsifies.
        * **SQLite** — repeated UPDATE attempts on the second connection for
          the whole window. Each one needs the database file's write lock,
          collides with the guarded transaction's snapshot, and raises the
          database-locked error, which is recorded as the LOCKED-OUT signal
          and retried — never surfaced raw, never a 500. Only after the
          request completes does the mover apply cleanly, so the asserted
          final state is the one valid linearization.
        * **Memory** — a second thread entering a REAL ``transaction()``,
          which blocks on the process-wide lock the guarded unit holds until
          that unit exits.
        """
        api = self.srv.STATE.api
        backend = self.backend
        in_txn = threading.Event()
        released = threading.Event()
        request_done = threading.Event()
        committed = threading.Event()
        locked_out = threading.Event()
        writer_store = self._writer_store()
        writer_errors = []
        orig = api._authorize_setup_targets
        fired = []

        def paused(*a, **k):
            refused = orig(*a, **k)
            # Pause only the first AUTHORIZED pass: a retry (or a refusal)
            # must run straight through, or a legitimate second attempt would
            # deadlock the harness.
            if refused is None and not fired:
                fired.append(True)
                in_txn.set()
                if not released.wait(20):
                    raise AssertionError("the harness never released the "
                                         "guarded transaction")
            return refused

        def writer_body():
            try:
                if backend == "memory":
                    with writer_store.transaction():   # blocks on the lock
                        move["mem_apply"](writer_store)
                    committed.set()
                    return
                if backend == "postgres":
                    self._column_update(writer_store, move["table"],
                                        move["assignments"], move["row_id"])
                    committed.set()
                    return
                import sqlite3
                while not released.is_set():
                    try:
                        self._column_update(writer_store, move["table"],
                                            move["assignments"],
                                            move["row_id"])
                        committed.set()      # window breach — the outer
                        return               # assertion reports it
                    except sqlite3.OperationalError as exc:
                        if "locked" not in str(exc).lower():
                            raise
                        locked_out.set()
                        _time.sleep(0.02)
                # Window over: let the guarded request finish COMMITTING
                # before the clean final apply, so the linearization is
                # deterministic rather than a busy-loop coin toss.
                if not request_done.wait(self.WRITER_DEADLINE):
                    raise AssertionError("the guarded request never "
                                         "completed")
                deadline = _time.monotonic() + self.WRITER_DEADLINE
                while True:
                    try:
                        self._column_update(writer_store, move["table"],
                                            move["assignments"],
                                            move["row_id"])
                        committed.set()
                        return
                    except sqlite3.OperationalError as exc:
                        if ("locked" not in str(exc).lower()
                                or _time.monotonic() > deadline):
                            raise
                        _time.sleep(0.02)
            except BaseException as exc:      # surfaced by the main thread
                writer_errors.append(exc)

        api._authorize_setup_targets = paused
        self.addCleanup(lambda: setattr(api, "_authorize_setup_targets", orig)
                        if api._authorize_setup_targets is paused else None)
        out = {}

        def run():
            out["r"] = self._post(opener, path, body)

        request = threading.Thread(target=run, daemon=True)
        request.start()
        self.assertTrue(
            in_txn.wait(20),
            "the request never reached the in-transaction barrier — it was "
            "refused upstream, so this test proved nothing about the locks")

        writer_thread = threading.Thread(target=writer_body, daemon=True)
        writer_thread.start()
        # THE CLAIM UNDER TEST: with the guarded transaction holding the row
        # locks (PostgreSQL), the file's write lock (SQLite) or the process
        # lock (Memory), the mover cannot commit inside the window.
        self.assertFalse(
            committed.wait(self.WINDOW_GRACE),
            f"[{backend}] the concurrent writer COMMITTED between the "
            f"in-transaction authorization and the mutation — the "
            f"check→mutate window is open")
        released.set()
        request.join(30)
        self.assertFalse(request.is_alive(),
                         "the guarded request never returned")
        request_done.set()
        api._authorize_setup_targets = orig
        writer_thread.join(self.WRITER_DEADLINE + 10)
        self.assertFalse(writer_thread.is_alive(),
                         f"[{backend}] the writer never completed after the "
                         f"guarded commit released the locks")
        self.assertEqual(
            writer_errors, [],
            f"[{backend}] the writer leaked a raw error instead of the "
            f"lock-or-retry contract: {writer_errors!r}")
        self.assertTrue(committed.is_set(),
                        f"[{backend}] the writer finished without "
                        f"committing its move")
        if backend == "sqlite":
            self.assertTrue(
                locked_out.is_set(),
                "[sqlite] the mover was never locked out during the window — "
                "no real cross-connection contention was observed")
        self.assertTrue(fired, "the in-transaction barrier never fired")
        return out["r"]

    # ----------------------------------------------------------------------
    # Shape 1 — a DIRECT target: an in-place player update, with the writer
    # relinking the same Player into Program B's Team.
    # ----------------------------------------------------------------------
    def _run_player_update_lock(self, database_url, backend):
        self._build(backend, database_url)
        owner = self.openers[self.OWNER_A]
        a, b = self.worlds[self.OWNER_A], self.worlds[self.OWNER_B]
        self._select(owner, a["program"], a["season"])
        player = self._ok(owner, "/api/v2/setup/player",
                          {"team_id": a["team"], "name": "LK Locked Player",
                           "position": "forward"})
        store = self.srv.STATE.api.store

        def mem_apply(ws):
            row = ws.get_player(player["id"])
            row.team_id = b["team"]
            ws.save_player(row)

        status, resp, _raw = self._race_inside_txn(
            owner, f"/api/v2/setup/player/{player['id']}/update",
            {"name": "LK Renamed Under Lock"},
            {"table": "players", "assignments": {"team_id": b["team"]},
             "row_id": player["id"], "mem_apply": mem_apply})
        self.assertEqual(status, 200,
                         (f"[{backend}] the guarded update was authorized "
                          f"under the locks and nothing moved under it — it "
                          f"must succeed", resp))
        self.assertEqual(resp.get("name"), "LK Renamed Under Lock", resp)
        self.assertEqual(
            resp.get("team_id"), a["team"],
            f"[{backend}] the response must echo the PRE-MOVE row the "
            f"mutation actually ran against")
        # The one valid linearization: guarded write first, mover second.
        final = store.get_player(player["id"])
        self.assertEqual(final.name, "LK Renamed Under Lock",
                         f"[{backend}] the writer's move CLOBBERED the "
                         f"guarded mutation — not a linearization")
        self.assertEqual(final.team_id, b["team"],
                         f"[{backend}] the writer's move never applied")

    def test_player_update_lock_memory(self):
        self._run_player_update_lock(None, "memory")

    def test_player_update_lock_sqlite_file(self):
        tmp = tempfile.mkdtemp(prefix="hs-lock-")
        self._run_player_update_lock(
            f"sqlite:///{os.path.join(tmp, 'lock.db')}", "sqlite")

    @unittest.skipUnless(os.environ.get("TEST_DATABASE_URL"),
                         "PostgreSQL required (set TEST_DATABASE_URL)")
    def test_player_update_lock_postgres(self):
        self._run_player_update_lock(os.environ["TEST_DATABASE_URL"],
                                     "postgres")

    # ----------------------------------------------------------------------
    # Shape 2 — a BRIDGE row and its locked parent: a registration remove,
    # with the writer re-pointing the registration at Program B's
    # LeagueSeason.
    # ----------------------------------------------------------------------
    def _run_bridge_parent_lock(self, database_url, backend):
        self._build(backend, database_url)
        owner = self.openers[self.OWNER_A]
        a, b = self.worlds[self.OWNER_A], self.worlds[self.OWNER_B]
        self._select(owner, a["program"], a["season"])
        registration = self._ok(
            owner, f"/api/v2/setup/seasons/{a['season']}/team-registrations",
            {"team_id": a["team"], "league_id": a["league"]})
        store = self.srv.STATE.api.store

        def mem_apply(ws):
            row = ws.get_season_team_registration(registration["id"])
            row.league_season_id = b["league_season"]
            ws.save_season_team_registration(row)

        # ``league_season_id`` alone: migration 035 dropped the redundant
        # ``league_id`` column — the LeagueSeason IS the competition edge.
        status, resp, _raw = self._race_inside_txn(
            owner,
            f"/api/v2/setup/season-team-registration/{registration['id']}"
            f"/remove", {},
            {"table": "season_team_registrations",
             "assignments": {"league_season_id": b["league_season"]},
             "row_id": registration["id"], "mem_apply": mem_apply})
        self.assertEqual(status, 200,
                         (f"[{backend}] the guarded remove was authorized "
                          f"under the locks — it must succeed", resp))
        final = store.get_season_team_registration(registration["id"])
        self.assertFalse(final.active,
                         f"[{backend}] the guarded remove never applied")
        self.assertEqual(final.league_season_id, b["league_season"],
                         f"[{backend}] the writer's re-point never applied")

    def test_bridge_parent_lock_memory(self):
        self._run_bridge_parent_lock(None, "memory")

    def test_bridge_parent_lock_sqlite_file(self):
        tmp = tempfile.mkdtemp(prefix="hs-lock-")
        self._run_bridge_parent_lock(
            f"sqlite:///{os.path.join(tmp, 'lock.db')}", "sqlite")

    @unittest.skipUnless(os.environ.get("TEST_DATABASE_URL"),
                         "PostgreSQL required (set TEST_DATABASE_URL)")
    def test_bridge_parent_lock_postgres(self):
        self._run_bridge_parent_lock(os.environ["TEST_DATABASE_URL"],
                                     "postgres")

    # ----------------------------------------------------------------------
    # Shape 3 — a reassign DESTINATION: rink → venue, with the writer moving
    # the destination Venue into Program B.
    # ----------------------------------------------------------------------
    def _run_reassign_destination_lock(self, database_url, backend):
        self._build(backend, database_url)
        owner = self.openers[self.OWNER_A]
        a, b = self.worlds[self.OWNER_A], self.worlds[self.OWNER_B]
        self._select(owner, a["program"], a["season"])
        source = self._ok(owner, "/api/setup/venue",
                          {"name": "LK Source Venue",
                           "league_id": a["program"]})
        rink = self._ok(owner, "/api/v2/setup/rink",
                        {"venue_id": source["id"], "name": "LK Rink"})
        destination = self._ok(owner, "/api/setup/venue",
                               {"name": "LK Destination Venue",
                                "league_id": a["program"]})
        store = self.srv.STATE.api.store

        def mem_apply(ws):
            row = ws.get_venue(destination["id"])
            row.league_id = b["program"]
            ws.save_venue(row)

        status, resp, _raw = self._race_inside_txn(
            owner, f"/api/v2/setup/rink/{rink['id']}/assign-venue",
            {"venue_id": destination["id"]},
            {"table": "venues", "assignments": {"league_id": b["program"]},
             "row_id": destination["id"], "mem_apply": mem_apply})
        self.assertEqual(status, 200,
                         (f"[{backend}] the guarded reassign was authorized "
                          f"under the locks — it must succeed", resp))
        self.assertEqual(store.get_rink(rink["id"]).venue_id,
                         destination["id"],
                         f"[{backend}] the guarded reassign never applied")
        self.assertEqual(store.get_venue(destination["id"]).league_id,
                         b["program"],
                         f"[{backend}] the writer's move never applied")

    def test_reassign_destination_lock_memory(self):
        self._run_reassign_destination_lock(None, "memory")

    def test_reassign_destination_lock_sqlite_file(self):
        tmp = tempfile.mkdtemp(prefix="hs-lock-")
        self._run_reassign_destination_lock(
            f"sqlite:///{os.path.join(tmp, 'lock.db')}", "sqlite")

    @unittest.skipUnless(os.environ.get("TEST_DATABASE_URL"),
                         "PostgreSQL required (set TEST_DATABASE_URL)")
    def test_reassign_destination_lock_postgres(self):
        self._run_reassign_destination_lock(os.environ["TEST_DATABASE_URL"],
                                            "postgres")
