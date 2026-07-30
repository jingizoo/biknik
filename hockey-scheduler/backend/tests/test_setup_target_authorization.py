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
import threading
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
# rather than through ``ApiService._setup_target_program_ids``. If the
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
                    # The active Program for a record is its own world's
                    # Program -- except for a Program, which IS its own scope.
                    scope_a = (target_a if kind == "program"
                               else world_a["program"])
                    scope_b = (target_b if kind == "program"
                               else world_b["program"])

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

                    api.set_active_context(*attacker, scope_b, None)

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
                    api.set_active_context(*attacker, scope_a, None)
                    self.assertIs(
                        api.setup_target_accessible(kind, target_a, *attacker),
                        True,
                        f"[{backend}/{kind}] switching to the record's OWN "
                        f"Program did not make it accessible -- the refusal "
                        f"was a blanket block, not a scope decision")
                    api.set_active_context(*attacker, scope_b, None)

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
                api.set_active_context(*attacker, world_a["program"], None)
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
                api.set_active_context(*owner, world_a["program"], None)
                api.set_active_context(*attacker, world_b["program"], None)

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
                api.set_active_context(*attacker, world_b["program"], None)

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
                api.set_active_context(*attacker, world_b["program"], None)

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
        api.set_active_context(*owner, world_a["program"], None)
        api.set_active_context(*attacker, world_b["program"], None)
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

    def _select(self, opener, program_id):
        status, resp = self._req(opener, "POST", "/api/context",
                                 {"program_id": program_id})
        self.assertEqual(status, 200, (program_id, resp))
        self.assertEqual(resp.get("program", {}).get("id"), program_id, resp)

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
        season = self._mint_season(tag)
        self._ok(self._o(tag), f"/api/v2/setup/seasons/{season}/archive",
                 {"reason": "fixture"})
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
        self._select(att, row["ctx"](own, mine))
        path, body, _w = row["call"](own, mine)
        status, resp, _raw = self._post(att, path, body)
        self._assert_allowed(status, resp, row["codes"],
                             f"{where} POSITIVE ({path})")

        # ---- the victim, and the precondition that makes a refusal mean
        #      anything at all ---------------------------------------------
        victim = row["mint"](victim_tag)
        active = self.worlds[own]["program"]
        self._select(att, active)
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
        self._select(att, row["ctx"](victim_tag, victim))
        path, body, _w = row["call"](victim_tag, victim)
        status, resp, _raw = self._post(att, path, body)
        self._assert_allowed(
            status, resp, row["codes"],
            f"{where} CONTEXT SWITCH ({path}): selecting the target's OWN "
            f"Program did not make the same call on the same target succeed, "
            f"so the refusal above was a blanket block rather than a scope "
            f"decision")
        self._select(att, self.worlds[own]["program"])

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
    #   ctx    (tag, target) -> the Program the attacker selects for the
    #          positive and context-switch cases. The world's Program by
    #          default; the TARGET ITSELF when the target is a Program, since a
    #          Program is its own scope and "switch to the target's Program"
    #          means exactly that.
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
                                        self.worlds[tag]["program"])})

        # -- the generic deletes, on BOTH versions -------------------------
        # (canonical kind, v1 wire word or None, v2 wire word or None, mint,
        #  positive-case mint, ctx, tolerated non-auth codes). v1's vocabulary
        # differs: its "league" is today's PROGRAM and its "level" is today's
        # competition LEAGUE. Player/Official delete is v2-only (#232/#271) and
        # league-season delete is a v2 addition (#159), hence the Nones.
        _target_is_its_own_scope = (lambda _tag, tid: tid)
        deletes = [
            ("organization", "organization", "organization",
             self._mint_organization, self._mint_unlinked_org_as_attacker,
             None, frozenset({"has_dependencies"})),
            ("program", "league", "program", self._mint_program, None,
             _target_is_its_own_scope, frozenset()),
            ("season", "season", "season", self._mint_season, None, None,
             frozenset()),
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
            self._mint_season, archive_call)
        add("POST /api/v2/setup/seasons/<id>/reopen", "season",
            self._mint_archived_season, reopen_call)

        # -- the venue-access grant's SEASON argument, which is generic ------
        # (its VENUE argument is the facility-tree exception and has its own
        # test below -- the whole point being that the two arguments of this
        # one route are judged by DIFFERENT rules).
        def grant_season_call(active, tid):
            return (f"/api/v2/setup/seasons/{tid}/venue-access",
                    {"venue_id": self._mint_venue(active)}, [("season", tid)])

        add("POST /api/v2/setup/seasons/<id>/venue-access [SEASON]", "season",
            self._mint_season, grant_season_call)

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
        self._select(att, self.worlds[self.OWN]["program"])
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
