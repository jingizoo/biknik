"""``get_setup_overview_v2``'s active-context scoping (#369, rearchitected by
the #367 owner ruling).

This file has now been through two contract reversals, and both are load-
bearing history:

1. The pre-#369 contract let a scoped role see its FULL authorized Program
   set, and left Club/Organization/Official/Venue/Rink/IceSlot reference data
   globally unfiltered. #369 review closed both: ``programs`` collapses to the
   single persisted ACTIVE Program, and the six reference kinds are included
   only through a real, validatable chain into it.

2. #369's own answer to "then how does a create-then-link flow reach a
   just-created record?" was an additive ``unassigned_*`` list per kind,
   holding every record in the installation linked to NO Program, offered to
   every scoped caller on the reasoning that "an unassigned row is nobody's
   data yet". The #367 owner ruling REVERSES that: an unassigned record is not
   automatically nobody's data, and enumerating installation-wide
   ``unassigned_*`` names -- Officials' personal names included -- to every
   scoped operator (and to a caller authorized for ZERO Programs) is itself
   the disclosure. Unjoinable rows are now OMITTED, and the create-then-link
   flows are served by the narrow creator-owned contract the ruling permits:
   ``pending_link_*`` carries the unlinked rows THIS caller created, per the
   audit trail, and nothing else.

The ruling also makes the **active Season a hard ceiling** on this surface,
reversing #369's own "Season is not a further filter here, a management
surface needs every Season" decision. ``seasons`` is the active Season alone;
a Program-only context returns EMPTY for every Season-bound collection. Two
axes are deliberately NOT narrowed, and the tests below assert each
explicitly so a future over-narrowing regression fails here:

* ``leagues`` -- a League is permanent Program structure (#283), so it stays
  Program-scoped and is not narrowed by Season or by the League selection.
* the FACILITY tree (Venue/Rink/IceSlot + owning Organization) -- it has a
  Season axis via SeasonVenueAccess and NO competition-League axis, so it
  stays active-Season-wide under ANY League selection. "No League" is
  therefore exactly the Program + active-Season union.

Clubs and Officials, by contrast, ARE League-bound -- Clubs through their
Teams, Officials through an in-scope home Club or assignment -- so a League
selection narrows both.

Facade-level across the full Memory/SQLite/(optional)PostgreSQL matrix and
the full role matrix (League Admin, Coach, Player, Guardian, Official, Arena
Manager, Viewer), plus a real authenticated-HTTP class at the bottom for the
two contracts that cross the route boundary (the Season ceiling, and the
creator-owned pending-link list keyed on the SESSION's own user id).
"""

import json
import os
import threading
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timezone
from http.cookiejar import CookieJar
from http.server import ThreadingHTTPServer

from helpers import BACKEND, fresh_sql_store  # noqa: F401  (ensures sys.path is set up)

from hockey_scheduler.api import ApiService
from hockey_scheduler.domain import (GuardianLink, OfficialRole, Role,
                                     SetupAuditLog)
from hockey_scheduler.domain.models import Game
from hockey_scheduler.domain.setup_models import OfficialAssignment
from hockey_scheduler.store import InMemoryStore, SqlStore

_ALL_OVERVIEW_KEYS = (
    "programs", "seasons", "leagues", "divisions", "teams", "clubs",
    "organizations", "officials", "venues", "rinks", "ice_slots",
)
# The creator-owned create-then-link lists that replaced `unassigned_*`.
_ALL_PENDING_KEYS = (
    "pending_link_clubs", "pending_link_organizations",
    "pending_link_officials", "pending_link_venues", "pending_link_rinks",
    "pending_link_ice_slots",
)
# The exact key names the REVERSED mechanism used. No response may carry them
# again: re-adding one would silently restore installation-wide disclosure
# while every assertion below still passed.
_REVERSED_UNASSIGNED_KEYS = (
    "unassigned_clubs", "unassigned_organizations", "unassigned_officials",
    "unassigned_venues", "unassigned_rinks", "unassigned_ice_slots",
)

_TS = datetime(2027, 1, 1, tzinfo=timezone.utc)

# Every role the endpoint must scope correctly, independent of which roles the
# HTTP route permits today (#369 review: the facade's own authorization logic
# must narrow correctly for ANY role/scope it is given).
_ROLE_MATRIX = (Role.LEAGUE_ADMIN, Role.ARENA_MANAGER, Role.VIEWER,
               Role.COACH, Role.PLAYER, Role.GUARDIAN, Role.OFFICIAL)


def _backends():
    yield "memory", InMemoryStore()
    yield "sqlite", SqlStore(":memory:")
    url = os.environ.get("TEST_DATABASE_URL")
    if url:
        # fresh_sql_store (not a bare SqlStore) so a database polluted by an
        # earlier module in this same serial worker is recovered rather than
        # failing to open at all -- see helpers.fresh_sql_store (#369).
        yield "postgres", fresh_sql_store(url)


def _close(store):
    if isinstance(store, SqlStore):
        store.close()


def _assign_game(api, official_id, *, season_id, team_id):
    """Assign ``official_id`` to a minimal REGULAR Game under ``season_id`` —
    enough for ``context_scope``'s Official-authorization branch (which reads
    only the Game's ``season_id``) and for the endpoint's own
    assignment-based Official-inclusion branch (same field)."""
    store = api.store
    with store.transaction():
        gid = store.next_id("game")
        store.add_game(Game(id=gid, home_team_id=team_id, away_team_id=team_id,
                            start_time=None, season_id=season_id))
        store.add_official_assignment(OfficialAssignment(
            id=store.next_id("assign"), game_id=gid, official_id=official_id,
            role=OfficialRole.REFEREE))
    return gid


class LeagueFilteredOverviewV2Test(unittest.TestCase):
    """Facade-level, Memory/SQLite/(optional)PostgreSQL-parametrized: mirrors
    ``test_active_context_league.py``'s own backend-matrix discipline for a
    pure read composed from store methods."""

    def _ids(self, ov, key):
        return {row["id"] for row in ov[key]}

    def _build_program_env(self, api, label, actor_id=None):
        """One two-League Program environment labeled ``label``: 2 Seasons,
        2 sibling Leagues (each bound to its own Season), 2 Divisions, 2
        Clubs, 2 Teams (one permanently in each League, registered into its
        League's Season), 1 Organization owning 1 Venue (granted active
        ``SeasonVenueAccess`` to Season 1), 1 Rink, 1 IceSlot, and 1 Official
        (home club = Club 1) — rich enough to prove the Program axis (vs. a
        sibling Program), the League axis (vs. a sibling League within the
        SAME Program) and the Season axis (Season 1 vs. Season 2 within the
        same Program) all at once.

        ``actor_id`` is threaded through every create so a test that needs the
        creator-owned pending-link contract can decide WHO created each row."""
        program = api.create_program(f"Program {label}", "US", "UTC",
                                     actor_id=actor_id)
        self.assertNotIn("error", program, program)
        season1 = api.create_season(program["id"], f"Season {label}1",
                                    actor_id=actor_id)
        self.assertNotIn("error", season1, season1)
        season2 = api.create_season(program["id"], f"Season {label}2",
                                    actor_id=actor_id)
        self.assertNotIn("error", season2, season2)
        league1 = api.create_league(season1["id"], f"League {label}1",
                                    actor_id=actor_id)
        self.assertNotIn("error", league1, league1)
        league2 = api.create_league(season2["id"], f"League {label}2",
                                    actor_id=actor_id)
        self.assertNotIn("error", league2, league2)
        division1 = api.create_division(season1["id"], f"Division {label}1",
                                        league_id=league1["id"],
                                        actor_id=actor_id)
        self.assertNotIn("error", division1, division1)
        division2 = api.create_division(season2["id"], f"Division {label}2",
                                        league_id=league2["id"],
                                        actor_id=actor_id)
        self.assertNotIn("error", division2, division2)
        club1 = api.create_club(f"Club {label}1", actor_id=actor_id)
        self.assertNotIn("error", club1, club1)
        club2 = api.create_club(f"Club {label}2", actor_id=actor_id)
        self.assertNotIn("error", club2, club2)
        team1 = api.create_team(club_id=club1["id"], division_id=division1["id"],
                                name=f"Team {label}1", program_id=program["id"],
                                league_id=league1["id"], actor_id=actor_id)
        self.assertNotIn("error", team1, team1)
        team2 = api.create_team(club_id=club2["id"], division_id=division2["id"],
                                name=f"Team {label}2", program_id=program["id"],
                                league_id=league2["id"], actor_id=actor_id)
        self.assertNotIn("error", team2, team2)
        reg1 = api.register_team_for_season(season1["id"], team1["id"],
                                            division1["id"],
                                            league_id=league1["id"],
                                            actor_id=actor_id)
        self.assertNotIn("error", reg1, reg1)
        reg2 = api.register_team_for_season(season2["id"], team2["id"],
                                            division2["id"],
                                            league_id=league2["id"],
                                            actor_id=actor_id)
        self.assertNotIn("error", reg2, reg2)
        org = api.create_organization(f"Org {label}", actor_id=actor_id)
        self.assertNotIn("error", org, org)
        venue = api.create_venue(f"Venue {label}", organization_id=org["id"],
                                 actor_id=actor_id)
        self.assertNotIn("error", venue, venue)
        sva = api.grant_season_venue_access(season1["id"], venue["id"],
                                            actor_id=actor_id)
        self.assertNotIn("error", sva, sva)
        rink = api.create_rink(venue["id"], f"Rink {label}", actor_id=actor_id)
        self.assertNotIn("error", rink, rink)
        slot = api.create_ice_slot(
            rink["id"], "2026-09-01T18:30:00+00:00",
            "2026-09-01T20:00:00+00:00", actor_id=actor_id)
        self.assertNotIn("error", slot, slot)
        official = api.create_official(f"Official {label}",
                                       home_club_id=club1["id"],
                                       actor_id=actor_id)
        self.assertNotIn("error", official, official)
        return {"program": program, "season1": season1, "season2": season2,
                "league1": league1, "league2": league2,
                "division1": division1, "division2": division2,
                "club1": club1, "club2": club2, "team1": team1, "team2": team2,
                "org": org, "venue": venue, "rink": rink, "slot": slot,
                "official": official}

    def _role_scope_for(self, api, role, env, label):
        """``(user_id, scope, extra_official_id)`` for ``role``, authorized
        for ONLY ``env``'s Program — constructing whatever real subject row
        (Player / assigned Official / verified GuardianLink) that role's own
        ``context_scope`` resolution requires. ``extra_official_id`` is the id
        of a side-effect Official row (Official role only); callers must
        account for its presence in ``officials``."""
        team1 = env["team1"]
        if role in (Role.LEAGUE_ADMIN, Role.ARENA_MANAGER, Role.VIEWER):
            return f"user_{role.value}_{label}", {}, None
        if role == Role.COACH:
            return f"coach_{label}", {"team_id": team1["id"]}, None
        if role == Role.PLAYER:
            player = api.create_player(team1["id"], f"Player {label}", "forward")
            self.assertNotIn("error", player, player)
            return f"player_{label}", {"player_id": player["id"]}, None
        if role == Role.OFFICIAL:
            official = api.create_official(f"Caller Official {label}")
            self.assertNotIn("error", official, official)
            _assign_game(api, official["id"], season_id=env["season1"]["id"],
                        team_id=team1["id"])
            return (f"official_{label}", {"official_id": official["id"]},
                    official["id"])
        if role == Role.GUARDIAN:
            player = api.create_player(team1["id"], f"Junior {label}", "forward")
            self.assertNotIn("error", player, player)
            uid = f"guardian_{label}"
            with api.store.transaction():
                api.store.add_guardian_link(GuardianLink(
                    id=api.store.next_id("glink"), guardian_user_id=uid,
                    player_id=player["id"], created_at=_TS, verified=True))
            return uid, {}, None
        raise AssertionError(f"unhandled role {role}")

    def _assert_no_reversed_keys(self, ov, label):
        for key in _REVERSED_UNASSIGNED_KEYS:
            self.assertNotIn(
                key, ov,
                f"[{label}] the REVERSED installation-wide `{key}` bucket is "
                "back in the payload (#367 owner ruling: unjoinable rows are "
                "omitted; only the caller's own creations may be offered, "
                "through pending_link_*)")

    # -- 1. no-role default stays the full, unfiltered installation view -----
    def test_no_role_returns_full_unfiltered_installation_view(self):
        """Unchanged legacy behavior when ``role`` is left ``None`` (the
        default) — a couple of pre-#367 direct call sites
        (``test_season_lifecycle.py``) rely on this exact shape."""
        for label, store in _backends():
            with self.subTest(backend=label):
                api = ApiService(store)
                a = self._build_program_env(api, "A")
                b = self._build_program_env(api, "B")

                ov = api.get_setup_overview_v2()
                self.assertNotIn("error", ov, ov)

                expected = (
                    ("programs", {a["program"]["id"], b["program"]["id"]}),
                    ("seasons", {a["season1"]["id"], a["season2"]["id"],
                                b["season1"]["id"], b["season2"]["id"]}),
                    ("leagues", {a["league1"]["id"], a["league2"]["id"],
                                b["league1"]["id"], b["league2"]["id"]}),
                    ("divisions", {a["division1"]["id"], a["division2"]["id"],
                                  b["division1"]["id"], b["division2"]["id"]}),
                    ("teams", {a["team1"]["id"], a["team2"]["id"],
                              b["team1"]["id"], b["team2"]["id"]}),
                    ("clubs", {a["club1"]["id"], a["club2"]["id"],
                              b["club1"]["id"], b["club2"]["id"]}),
                    ("organizations", {a["org"]["id"], b["org"]["id"]}),
                    ("venues", {a["venue"]["id"], b["venue"]["id"]}),
                    ("rinks", {a["rink"]["id"], b["rink"]["id"]}),
                    ("ice_slots", {a["slot"]["id"], b["slot"]["id"]}),
                    ("officials", {a["official"]["id"], b["official"]["id"]}),
                )
                for key, ids in expected:
                    self.assertEqual(self._ids(ov, key), ids, (label, key))

                # pending_link_* are always empty in the unfiltered shape — the
                # main lists above are already unfiltered, so there is nothing
                # additive left to offer.
                for key in _ALL_PENDING_KEYS:
                    self.assertEqual(ov[key], [], (label, key))
                self._assert_no_reversed_keys(ov, label)
                _close(store)

    # -- 2. zero Programs anywhere: still SCOPED, not a bootstrap bypass -----
    def test_zero_programs_anywhere_keeps_an_authenticated_read_scoped(self):
        """REVERSED (#369 review blocker). This test previously asserted the
        opposite — that a brand-new install with LITERALLY no Program made a
        real role/scope fall through to the same unfiltered shape as
        ``role=None``, on the reasoning that such an install has no "other
        Program" to leak. It does have something to leak: its pre-Program
        Clubs, Venues and Officials, each created by SOME account. Absence of
        a Program is not an authorization, so an authenticated caller stays
        scoped here exactly as it does when Programs exist.

        Kept deliberately narrow: this is the shape assertion on a truly empty
        store, where the two shapes still coincide because there is nothing at
        all to divide. ``test_zero_program_bootstrap_scoping.py`` is where the
        contract is proven with real rows and two real accounts."""
        for label, store in _backends():
            with self.subTest(backend=label):
                api = ApiService(store)
                no_role = api.get_setup_overview_v2()
                self.assertNotIn("error", no_role, no_role)
                with_role = api.get_setup_overview_v2(
                    "someone", Role.COACH, {"team_id": "team_does_not_exist"})
                self.assertNotIn("error", with_role, with_role)

                for key in _ALL_OVERVIEW_KEYS + _ALL_PENDING_KEYS:
                    self.assertEqual(no_role[key], [], (label, key))
                    self.assertEqual(with_role[key], [], (label, key))

                # A pre-Program row, owned by an account that is NOT the
                # caller above: the scoped read must not start answering with
                # the installation's contents just because no Program exists.
                club = api.create_club("Pre-Program Club",
                                       actor_id="user_someone_else")
                self.assertNotIn("error", club, club)
                official = api.create_official("Pre-Program Official",
                                               actor_id="user_someone_else")
                self.assertNotIn("error", official, official)
                after = api.get_setup_overview_v2(
                    "someone", Role.COACH, {"team_id": "team_does_not_exist"})
                self.assertNotIn("error", after, after)
                for key in _ALL_OVERVIEW_KEYS + _ALL_PENDING_KEYS:
                    self.assertEqual(
                        after[key], [],
                        f"[{label}] a zero-Program installation published "
                        f"{key} to a caller that owns none of it")
                # ...while the identity-less legacy call is unchanged.
                self.assertEqual(
                    self._ids(api.get_setup_overview_v2(), "clubs"),
                    {club["id"]}, label)
                _close(store)

    # -- 3. every role in the matrix narrows to its OWN active tuple ---------
    def test_role_matrix_each_role_narrows_to_its_active_program_and_season(self):
        """League Admin, Arena Manager, Viewer, Coach, Player, Guardian, and
        Official each independently resolve to Program A / Season A1 (never
        Program B, which also exists in the same installation, and never
        Season A2's records) once that tuple is set as the active context —
        proving both the full role matrix (#369 review) and that EVERY
        collection excludes a sibling Program's rows AND a sibling Season's,
        for every role, not just Coach/League Admin.

        The Season half of this is the #367 owner ruling's reversal: this
        test previously asserted ``seasons`` held BOTH of the Program's
        Seasons and ``divisions`` both of its Divisions."""
        for label, store in _backends():
            with self.subTest(backend=label):
                api = ApiService(store)
                a = self._build_program_env(api, "A")
                self._build_program_env(api, "B")  # never expected to appear

                for role in _ROLE_MATRIX:
                    with self.subTest(backend=label, role=role.value):
                        user_id, scope, extra_official_id = (
                            self._role_scope_for(api, role, a, "A"))
                        ctx = api.set_active_context(
                            user_id, role, scope, a["program"]["id"],
                            a["season1"]["id"], None)
                        self.assertNotIn("error", ctx, (label, role, ctx))
                        self.assertEqual(ctx["program_id"], a["program"]["id"])
                        self.assertEqual(ctx["season_id"], a["season1"]["id"])

                        ov = api.get_setup_overview_v2(user_id, role, scope)
                        self.assertNotIn("error", ov, (label, role, ov))

                        expected = (
                            ("programs", {a["program"]["id"]}),
                            # REVERSED (#367 ruling): the ACTIVE Season only.
                            ("seasons", {a["season1"]["id"]}),
                            # NOT reversed, and deliberately so: a League is
                            # permanent Program structure, not Season-bound
                            # data, so both of the Program's Leagues stay
                            # offered even though A2 is bound to Season A2.
                            ("leagues", {a["league1"]["id"],
                                        a["league2"]["id"]}),
                            # REVERSED: Season A2's Division is gone.
                            ("divisions", {a["division1"]["id"]}),
                            # Teams are permanent Program+League structure —
                            # NOT Season-bound — so both remain.
                            ("teams", {a["team1"]["id"], a["team2"]["id"]}),
                            # No League selected, so both Leagues' Clubs.
                            ("clubs", {a["club1"]["id"], a["club2"]["id"]}),
                            ("organizations", {a["org"]["id"]}),
                            # Granted to Season A1, which IS active.
                            ("venues", {a["venue"]["id"]}),
                            ("rinks", {a["rink"]["id"]}),
                            ("ice_slots", {a["slot"]["id"]}),
                        )
                        for key, ids in expected:
                            # Exact equality: since it never includes any of
                            # Program B's (differently-id'd) rows, this proves
                            # exclusion, not merely presence of Program A's.
                            self.assertEqual(
                                self._ids(ov, key), ids, (label, role, key))

                        expected_officials = {a["official"]["id"]}
                        if extra_official_id:
                            expected_officials.add(extra_official_id)
                        self.assertEqual(
                            self._ids(ov, "officials"), expected_officials,
                            (label, role))
                        self._assert_no_reversed_keys(ov, label)
                _close(store)

    # -- 4. the Season ceiling: S1/S2 in ONE Program, under League and none --
    def test_active_season_is_the_hard_setup_ceiling_and_flips_with_the_selection(self):
        """#367 owner ruling, B1's required regression: two Seasons in ONE
        Program with distinguishable records must show ONLY the active
        Season's — under a selected League and under "No League" alike — and
        must FLIP, not merely widen, when the other Season is selected.

        The previous contract resolved the Season and then ignored it,
        returning every Season of the Program and everything derived from
        one. That is what this reverses.

        Season A1 owns League A1 / Division A1 / the venue grant; Season A2
        owns League A2 / Division A2 / its own venue grant (added here, so
        the facility tree has something distinguishable in BOTH Seasons and
        an S2 selection cannot look correct merely by emptying out).
        """
        for label, store in _backends():
            with self.subTest(backend=label):
                api = ApiService(store)
                a = self._build_program_env(api, "A")
                # Season A2 gets its own Venue → Rink → IceSlot, so the
                # facility assertions below are a genuine FLIP in both
                # directions rather than "S1 has ice, S2 has none".
                org2 = api.create_organization("Org A2")
                venue2 = api.create_venue("Venue A2",
                                          organization_id=org2["id"])
                self.assertNotIn("error", venue2, venue2)
                grant2 = api.grant_season_venue_access(a["season2"]["id"],
                                                       venue2["id"])
                self.assertNotIn("error", grant2, grant2)
                rink2 = api.create_rink(venue2["id"], "Rink A2")
                slot2 = api.create_ice_slot(
                    rink2["id"], "2026-10-01T18:30:00+00:00",
                    "2026-10-01T20:00:00+00:00")
                self.assertNotIn("error", slot2, slot2)

                s1_only = {
                    "seasons": {a["season1"]["id"]},
                    "divisions": {a["division1"]["id"]},
                    "venues": {a["venue"]["id"]},
                    "rinks": {a["rink"]["id"]},
                    "ice_slots": {a["slot"]["id"]},
                    "organizations": {a["org"]["id"]},
                }
                s2_only = {
                    "seasons": {a["season2"]["id"]},
                    "divisions": {a["division2"]["id"]},
                    "venues": {venue2["id"]},
                    "rinks": {rink2["id"]},
                    "ice_slots": {slot2["id"]},
                    "organizations": {org2["id"]},
                }

                # Season A1 active, under "No League" AND under each League:
                # the League axis never re-opens the Season ceiling, and the
                # FACILITY axis never closes under a League (it has no
                # competition-League axis at all -- the ruling's explicit
                # "keep the facility tree active-Season-wide under a League
                # selection", stated here so a future League-narrowing of
                # venues fails).
                for league_choice in (None, a["league1"]["id"]):
                    with self.subTest(season="A1", league=league_choice):
                        ctx = api.set_active_context(
                            "admin_a", Role.LEAGUE_ADMIN, {},
                            a["program"]["id"], a["season1"]["id"],
                            league_choice)
                        self.assertNotIn("error", ctx, ctx)
                        self.assertEqual(ctx["season_id"], a["season1"]["id"],
                                         ctx)
                        ov = api.get_setup_overview_v2(
                            "admin_a", Role.LEAGUE_ADMIN, {})
                        self.assertNotIn("error", ov, ov)
                        for key, ids in s1_only.items():
                            self.assertEqual(self._ids(ov, key), ids,
                                             (label, key, league_choice))
                            self.assertFalse(
                                self._ids(ov, key) & s2_only[key],
                                f"[{label}] Season A2's {key} leaked into a "
                                f"Season A1 context (league={league_choice})")

                # Selecting League A2 from a Season A1 context moves the whole
                # tuple to Season A2 atomically (#364 owner ruling: the SERVER
                # resolves the canonical pair, and the returned Season may
                # differ from the requested one). The payload must follow the
                # Season the server RESOLVED, not the one that was asked for --
                # otherwise the Season ceiling would be applied against a
                # value the context row no longer holds.
                moved = api.set_active_context(
                    "admin_a", Role.LEAGUE_ADMIN, {}, a["program"]["id"],
                    a["season1"]["id"], a["league2"]["id"])
                self.assertNotIn("error", moved, moved)
                self.assertEqual(moved["season_id"], a["season2"]["id"], moved)
                ov_moved = api.get_setup_overview_v2("admin_a",
                                                     Role.LEAGUE_ADMIN, {})
                self.assertEqual(self._ids(ov_moved, "seasons"),
                                 {a["season2"]["id"]}, label)
                self.assertEqual(self._ids(ov_moved, "divisions"),
                                 {a["division2"]["id"]}, label)
                self.assertEqual(self._ids(ov_moved, "venues"),
                                 {venue2["id"]}, label)

                # ...and selecting Season A2 FLIPS every one of them.
                for league_choice in (None, a["league2"]["id"]):
                    with self.subTest(season="A2", league=league_choice):
                        ctx = api.set_active_context(
                            "admin_a", Role.LEAGUE_ADMIN, {},
                            a["program"]["id"], a["season2"]["id"],
                            league_choice)
                        self.assertNotIn("error", ctx, ctx)
                        ov = api.get_setup_overview_v2(
                            "admin_a", Role.LEAGUE_ADMIN, {})
                        for key, ids in s2_only.items():
                            self.assertEqual(self._ids(ov, key), ids,
                                             (label, key, league_choice))
                            self.assertFalse(
                                self._ids(ov, key) & s1_only[key],
                                f"[{label}] Season A1's {key} survived the "
                                f"switch to Season A2 (league={league_choice})")
                _close(store)

    def test_program_only_context_returns_empty_for_every_season_bound_list(self):
        """The other half of the ruling's Season rule: "a Program-only
        context returns EMPTY for Season-bound data" — never a silent
        fall-back to the Program's whole set. The NON-Season-bound
        collections (the Program itself, its permanent Leagues, Teams and
        their Clubs) stay visible, which is what keeps this an assertion
        about the Season axis rather than about emptiness."""
        for label, store in _backends():
            with self.subTest(backend=label):
                api = ApiService(store)
                a = self._build_program_env(api, "A")
                ctx = api.set_active_context(
                    "admin_a", Role.LEAGUE_ADMIN, {}, a["program"]["id"],
                    None, None)
                self.assertNotIn("error", ctx, ctx)
                self.assertIsNone(ctx["season_id"], ctx)

                ov = api.get_setup_overview_v2("admin_a", Role.LEAGUE_ADMIN, {})
                self.assertNotIn("error", ov, ov)
                for key in ("seasons", "divisions", "venues", "rinks",
                            "ice_slots"):
                    self.assertEqual(ov[key], [], (label, key))
                # Program-permanent structure is unaffected.
                self.assertEqual(self._ids(ov, "programs"),
                                 {a["program"]["id"]}, label)
                self.assertEqual(
                    self._ids(ov, "leagues"),
                    {a["league1"]["id"], a["league2"]["id"]}, label)
                self.assertEqual(
                    self._ids(ov, "teams"),
                    {a["team1"]["id"], a["team2"]["id"]}, label)
                self.assertEqual(
                    self._ids(ov, "clubs"),
                    {a["club1"]["id"], a["club2"]["id"]}, label)
                # The Organization is in scope only through the active
                # Program's operator edge here -- it owns no in-scope Venue,
                # because no Season is active for a grant to name.
                self.assertEqual(ov["organizations"], [], label)
                _close(store)

    # -- 5. a selected League narrows Teams/Divisions/Clubs/Officials --------
    def test_selected_league_narrows_the_league_bound_lists_only(self):
        """Within Program A (two sibling Leagues, A1 and A2, one per Season),
        selecting a League narrows every LEAGUE-BOUND list — Teams,
        Divisions, and (#367 owner ruling) Clubs *through their Teams* and
        Officials *through an in-scope home Club* — while the Program's
        Leagues and the facility tree stay put.

        The Clubs half is a reversal: this test previously asserted Clubs
        were "Program-level, not League-level" and that BOTH Clubs stayed
        visible under League A1. The ruling names Clubs among the
        League-bound rows, reached through their Teams, and the Dashboard
        read agrees — a sibling League's Club is not the active League's
        data just because it shares a Program.
        """
        for label, store in _backends():
            with self.subTest(backend=label):
                api = ApiService(store)
                a = self._build_program_env(api, "A")

                scope = {"team_id": a["team1"]["id"]}
                ctx = api.set_active_context(
                    "coach_a", Role.COACH, scope, a["program"]["id"],
                    a["season1"]["id"], a["league1"]["id"])
                self.assertNotIn("error", ctx, ctx)
                self.assertEqual(ctx["league_id"], a["league1"]["id"], ctx)

                ov = api.get_setup_overview_v2("coach_a", Role.COACH, scope)
                self.assertNotIn("error", ov, ov)

                # League-narrowed: League A1's own Team/Division/Club, and the
                # Official whose home club is that Club.
                self.assertEqual(self._ids(ov, "teams"), {a["team1"]["id"]},
                                 label)
                self.assertEqual(self._ids(ov, "divisions"),
                                 {a["division1"]["id"]}, label)
                self.assertEqual(
                    self._ids(ov, "clubs"), {a["club1"]["id"]},
                    f"[{label}] a SIBLING League's Club is not the active "
                    "League's data (#367 ruling: Clubs through their Teams)")
                self.assertEqual(self._ids(ov, "officials"),
                                 {a["official"]["id"]}, label)

                # NOT League-narrowed: the facility tree has no
                # competition-League axis at all, so it stays exactly as wide
                # as the active Season made it.
                self.assertEqual(self._ids(ov, "organizations"),
                                 {a["org"]["id"]}, label)
                self.assertEqual(self._ids(ov, "venues"), {a["venue"]["id"]},
                                 label)
                self.assertEqual(self._ids(ov, "rinks"), {a["rink"]["id"]},
                                 label)
                self.assertEqual(self._ids(ov, "ice_slots"), {a["slot"]["id"]},
                                 label)
                # ...nor are the Program's own permanent Leagues.
                self.assertEqual(
                    self._ids(ov, "leagues"),
                    {a["league1"]["id"], a["league2"]["id"]}, label)
                # ...and the Season ceiling is still exactly one Season.
                self.assertEqual(self._ids(ov, "seasons"),
                                 {a["season1"]["id"]}, label)

                # Selecting the sibling League FLIPS the League-bound lists.
                ctx2 = api.set_active_context(
                    "admin_a", Role.LEAGUE_ADMIN, {}, a["program"]["id"],
                    a["season1"]["id"], a["league2"]["id"])
                self.assertNotIn("error", ctx2, ctx2)
                ov2 = api.get_setup_overview_v2("admin_a", Role.LEAGUE_ADMIN,
                                                {})
                self.assertEqual(self._ids(ov2, "teams"), {a["team2"]["id"]},
                                 label)
                self.assertEqual(self._ids(ov2, "clubs"), {a["club2"]["id"]},
                                 label)
                self.assertEqual(
                    self._ids(ov2, "officials"), set(),
                    f"[{label}] Official A's home club is League A1's -- it "
                    "must not survive a switch to League A2")

                # The additive Team.league_id DTO field (#283) survives.
                row = next(t for t in ov["teams"] if t["id"] == a["team1"]["id"])
                self.assertEqual(row["league_id"], a["league1"]["id"], label)
                self.assertEqual(row["program_id"], a["program"]["id"], label)
                _close(store)

    def test_official_reached_only_through_a_sibling_leagues_game_is_out_of_scope(self):
        """The assignment edge is evaluated against the COMPLETE active
        tuple, not the Season alone.

        An Official with no home Club reaches the active context only through
        an assignment, and the ruling makes that edge League-bound: "Officials
        through an in-scope home Club or assignment". Checking only the Season
        left a sibling League's referee in scope — the same rule the Dashboard
        applies, so both surfaces now run the ONE shared
        ``_game_matches_active`` predicate rather than two copies that can
        drift (they already did, inside a single method).
        """
        for label, store in _backends():
            with self.subTest(backend=label):
                api = ApiService(store)
                a = self._build_program_env(api, "A")
                # A second League inside Season A1, so League is the only axis
                # that differs between the two games below.
                league3 = api.create_league(a["season1"]["id"], "League A3")
                self.assertNotIn("error", league3, league3)
                team3 = api.create_team(club_id=a["club1"]["id"],
                                        name="Team A3",
                                        program_id=a["program"]["id"],
                                        league_id=league3["id"])
                self.assertNotIn("error", team3, team3)

                ref1 = api.create_official("Ref League A1")   # no home club
                ref3 = api.create_official("Ref League A3")
                for r in (ref1, ref3):
                    self.assertNotIn("error", r, r)
                with store.transaction():
                    for ref, team, lg in ((ref1, a["team1"], a["league1"]),
                                          (ref3, team3, league3)):
                        gid = store.next_id("game")
                        store.add_game(Game(
                            id=gid, home_team_id=team["id"],
                            away_team_id=team["id"], start_time=None,
                            season_id=a["season1"]["id"],
                            league_id=lg["id"]))
                        store.add_official_assignment(OfficialAssignment(
                            id=store.next_id("assign"), game_id=gid,
                            official_id=ref["id"], role=OfficialRole.REFEREE))

                ctx = api.set_active_context(
                    "admin_a", Role.LEAGUE_ADMIN, {}, a["program"]["id"],
                    a["season1"]["id"], a["league1"]["id"])
                self.assertNotIn("error", ctx, ctx)
                ov = api.get_setup_overview_v2("admin_a", Role.LEAGUE_ADMIN, {})
                ids = self._ids(ov, "officials")
                self.assertIn(ref1["id"], ids,
                              f"[{label}] the League A1 referee reaches the "
                              "active tuple through its own assignment")
                self.assertNotIn(
                    ref3["id"], ids,
                    f"[{label}] an Official reachable only through a SIBLING "
                    "League's game stayed in scope -- the assignment edge is "
                    "League-bound, not Season-only")

                # "No League" is the union: both referees' games are in the
                # active Season, so both belong.
                ctx = api.set_active_context(
                    "admin_a", Role.LEAGUE_ADMIN, {}, a["program"]["id"],
                    a["season1"]["id"], None)
                self.assertNotIn("error", ctx, ctx)
                ov_none = api.get_setup_overview_v2("admin_a",
                                                    Role.LEAGUE_ADMIN, {})
                self.assertLessEqual({ref1["id"], ref3["id"]},
                                     self._ids(ov_none, "officials"), label)
                _close(store)

    def test_player_list_never_spans_programs(self):
        """#369 self-audit: ``list_players`` was the last Setup collection
        still answering installation-wide.

        The Setup "Clubs, players and staff" list is fed by ``/api/players``,
        which returned ``store.all_players()`` regardless of active context —
        every player NAME in the installation, and on that MANAGE_SETUP route
        every EMAIL too. The route already refuses to be folded into any
        unauthenticated payload because these names are usually juniors; the
        same reasoning makes a cross-Program answer wrong, and the review's
        required regression names players explicitly.

        The legacy no-context form stays unfiltered — many internal callers
        depend on it — so the scoping must key off a supplied role, not off
        whether a Program happens to resolve.
        """
        for label, store in _backends():
            with self.subTest(backend=label):
                api = ApiService(store)
                a = self._build_program_env(api, "A")
                b = self._build_program_env(api, "B")
                pa = api.create_player(team_id=a["team1"]["id"],
                                       name="A-PLAYER", position="forward")
                self.assertNotIn("error", pa, pa)
                pb = api.create_player(team_id=b["team1"]["id"],
                                       name="B-PLAYER", position="forward")
                self.assertNotIn("error", pb, pb)

                ctx = api.set_active_context(
                    "admin_a", Role.LEAGUE_ADMIN, {}, a["program"]["id"],
                    None, None)
                self.assertNotIn("error", ctx, ctx)

                scoped = [p["name"] for p in api.list_players(
                    user_id="admin_a", role=Role.LEAGUE_ADMIN, scope={})]
                self.assertIn("A-PLAYER", scoped, (label, "own player missing"))
                self.assertNotIn(
                    "B-PLAYER", scoped,
                    f"[{label}] another Program's player NAME leaked into the "
                    "active Program's player list")

                # IDOR: an EXPLICIT team_id must not bypass the ceiling.
                # The first version of this fix scoped only the unfiltered
                # form, so `?team_id=<another Program's team>` skipped the
                # gate entirely -- and the first version of THIS test only
                # ever passed the active Program's own team_id, so it agreed.
                # team_id selects WHICH rows to consider; it is never evidence
                # of authorization for them.
                own_team = [p["name"] for p in api.list_players(
                    a["team1"]["id"], user_id="admin_a",
                    role=Role.LEAGUE_ADMIN, scope={})]
                self.assertIn("A-PLAYER", own_team, (label, "own team"))
                foreign_team = [p["name"] for p in api.list_players(
                    b["team1"]["id"], user_id="admin_a",
                    role=Role.LEAGUE_ADMIN, scope={})]
                self.assertEqual(
                    foreign_team, [],
                    f"[{label}] IDOR: an explicit team_id for a Team in "
                    "ANOTHER Program returned that Team's players")
                # Same via the email-bearing form the operator route uses.
                foreign_email = api.list_players(
                    b["team1"]["id"], include_email=True, user_id="admin_a",
                    role=Role.LEAGUE_ADMIN, scope={})
                self.assertEqual(
                    foreign_email, [],
                    f"[{label}] IDOR: the include_email form disclosed "
                    "another Program's players")

                # A selected League narrows within the Program. Players are
                # League-narrowable through their Team's real permanent
                # Team.league_id (#283), like Clubs and Officials -- unlike
                # the facility tree, which has no League axis at all.
                pa2 = api.create_player(team_id=a["team2"]["id"],
                                        name="A2-PLAYER", position="forward")
                self.assertNotIn("error", pa2, pa2)
                ctx_l1 = api.set_active_context(
                    "admin_a", Role.LEAGUE_ADMIN, {}, a["program"]["id"],
                    a["season1"]["id"], a["league1"]["id"])
                self.assertNotIn("error", ctx_l1, ctx_l1)
                in_l1 = [p["name"] for p in api.list_players(
                    user_id="admin_a", role=Role.LEAGUE_ADMIN, scope={})]
                self.assertIn("A-PLAYER", in_l1, (label, "League 1's own"))
                self.assertNotIn(
                    "A2-PLAYER", in_l1,
                    f"[{label}] a SAME-Program player from another League "
                    "leaked while League 1 was active")
                # ...and an explicit team_id for the sibling League's Team is
                # refused too, not just filtered out of the unscoped list.
                self.assertEqual(
                    api.list_players(a["team2"]["id"], user_id="admin_a",
                                     role=Role.LEAGUE_ADMIN, scope={}), [],
                    f"[{label}] explicit team_id reached a sibling League's "
                    "Team while another League was active")

                # Legacy no-context form is deliberately unchanged.
                legacy = [p["name"] for p in api.list_players()]
                self.assertIn("A-PLAYER", legacy, label)
                self.assertIn("B-PLAYER", legacy, label)

                # A caller whose scope resolves to no Program at all gets
                # nothing, rather than falling back to everything.
                dangling = api.list_players(
                    user_id="nobody", role=Role.COACH, scope={"team_id": "gone"})
                self.assertEqual(dangling, [], label)
                _close(store)

    # -- 6. the creator-owned pending-link contract --------------------------
    def test_every_program_edge_disqualifies_a_record_from_pending_link(self):
        """"Linked to no Program" must be computed over EVERY edge into
        Program, not just the obvious one — otherwise a record that belongs
        to ANOTHER Program falls through into the pending-link list, which
        would leak it to its creator while they work in a different Program.

        Two edges were missed on the first attempt and each disclosed
        another Program's record by name:

        * ``Program.operator_organization_id`` — an Organization can reach a
          Program without owning any Venue at all.
        * a REVOKED ``SeasonVenueAccess`` — history still ties the Venue to
          the Program that revoked it.

        (``Venue.league_id`` is the third such edge — legacy vocabulary that
        stores a PROGRAM id — covered by the same assertions below.)

        Every record here is created by ``admin_a``, the same identity that
        then reads as Program A. That is deliberate: the creator-ownership
        half of the contract is therefore SATISFIED for all of them, so the
        only thing that can keep Program B's Organization and Venue out of
        the pending list is the linked-to-some-Program computation itself.
        Building them under a different actor would make this test pass
        without exercising the edge set at all.
        """
        for label, store in _backends():
            with self.subTest(backend=label):
                api = ApiService(store)
                org_a = api.create_organization("Op Org A", actor_id="admin_a")
                prog_a = api.create_program(
                    "Prog A", operator_organization_id=org_a["id"],
                    actor_id="admin_a")
                season_a = api.create_season(prog_a["id"], "A S",
                                             actor_id="admin_a")
                org_b = api.create_organization("Op Org B", actor_id="admin_a")
                prog_b = api.create_program(
                    "Prog B", operator_organization_id=org_b["id"],
                    actor_id="admin_a")
                season_b = api.create_season(prog_b["id"], "B S",
                                             actor_id="admin_a")

                # The Venue gets its OWN owner, distinct from either Program's
                # operating Organization. Without this, `org_b` would be
                # disqualified through the VENUE edge and the operator-edge
                # assertion below could never fail — verified by mutation:
                # reverting the operator edge left this test green until the
                # two owners were separated.
                venue_owner_b = api.create_organization("B Venue Owner",
                                                        actor_id="admin_a")
                # Program B's Venue, granted then REVOKED — still B's.
                venue_b = api.create_venue(
                    "B Venue", organization_id=venue_owner_b["id"],
                    actor_id="admin_a")
                grant = api.setup.grant_season_venue_access(
                    season_b["id"], venue_b["id"], actor_id="admin_a")
                api.setup.revoke_season_venue_access(grant.id,
                                                     actor_id="admin_a")

                ctx = api.set_active_context(
                    "admin_a", Role.LEAGUE_ADMIN, {}, prog_a["id"],
                    season_a["id"], None)
                self.assertNotIn("error", ctx, ctx)
                ov = api.get_setup_overview_v2("admin_a", Role.LEAGUE_ADMIN, {})
                self.assertNotIn("error", ov, ov)

                self.assertNotIn(
                    org_b["id"], self._ids(ov, "pending_link_organizations"),
                    f"[{label}] Program B's OPERATING organization leaked into "
                    "Program A's pending-link list")
                self.assertNotIn(
                    venue_b["id"], self._ids(ov, "pending_link_venues"),
                    f"[{label}] a Venue whose grant was REVOKED by Program B "
                    "leaked into Program A's pending-link list")
                # It must not be in the SCOPED lists either — it is neither
                # unlinked nor Program A's.
                self.assertNotIn(org_b["id"], self._ids(ov, "organizations"),
                                 (label, "org_b in scoped organizations"))
                self.assertNotIn(venue_b["id"], self._ids(ov, "venues"),
                                 (label, "venue_b in scoped venues"))
                # Positive control: A's OWN operating organization IS in
                # scope through that same operator edge, so the assertions
                # above cannot be passing merely because everything is empty.
                self.assertIn(org_a["id"], self._ids(ov, "organizations"),
                              (label, "Program A's operating org must show"))
                _close(store)

    def test_unlinked_records_are_visible_to_their_creator_and_to_nobody_else(self):
        """#367 owner ruling / B2's required regression, and the reversal of
        this file's previous ``unassigned_*`` test.

        The old contract published EVERY never-linked Club/Organization/
        Venue/Rink/IceSlot/Official in the installation to every scoped
        caller, on the reasoning that "an unassigned row is nobody's data
        yet". It is not: any Arena Manager could enumerate them all — a
        second operator working in an unrelated Program, and even an identity
        authorized for ZERO Programs — and Officials' names are personal
        data.

        Now: the operator who CREATED an unlinked record still sees it (that
        is what keeps create-then-link working, and it works from either of
        that operator's Programs, because the record belongs to no Program);
        a second, Program-scoped Arena Manager sees none of them; and a
        zero-scope identity sees none of them either. A record linked to ONE
        specific Program never appears in anyone's pending-link list.
        """
        for label, store in _backends():
            with self.subTest(backend=label):
                api = ApiService(store)
                a = self._build_program_env(api, "A")
                b = self._build_program_env(api, "B")

                # `creator` builds a full set of never-linked records.
                creator = "user_creator"
                free_club = api.create_club("Free Club", actor_id=creator)
                free_org = api.create_organization("Free Org",
                                                    actor_id=creator)
                # Deliberately no organization_id: an Organization is only
                # unlinked when it owns ZERO linked Venues anywhere, so a
                # Venue owned by NO Organization keeps the two checks
                # independent of each other.
                free_venue = api.create_venue("Free Venue", actor_id=creator)
                free_rink = api.create_rink(free_venue["id"], "Free Rink",
                                            actor_id=creator)
                free_slot = api.create_ice_slot(
                    free_rink["id"], "2026-10-01T10:00:00+00:00",
                    "2026-10-01T11:00:00+00:00", actor_id=creator)
                free_official = api.create_official("Free Official",
                                                    actor_id=creator)
                for r in (free_club, free_org, free_venue, free_rink,
                          free_slot, free_official):
                    self.assertNotIn("error", r, r)
                free = {
                    "pending_link_clubs": free_club["id"],
                    "pending_link_organizations": free_org["id"],
                    "pending_link_venues": free_venue["id"],
                    "pending_link_rinks": free_rink["id"],
                    "pending_link_ice_slots": free_slot["id"],
                    "pending_link_officials": free_official["id"],
                }

                # (1) The CREATOR sees its own unlinked records, from either
                #     of the Programs it works in -- they belong to no
                #     Program, so no Program's selection hides them.
                for env_label, env in (("A", a), ("B", b)):
                    ctx = api.set_active_context(
                        creator, Role.ARENA_MANAGER, {}, env["program"]["id"],
                        env["season1"]["id"], None)
                    self.assertNotIn("error", ctx, ctx)
                    ov = api.get_setup_overview_v2(creator, Role.ARENA_MANAGER,
                                                   {})
                    self.assertNotIn("error", ov, ov)
                    for key, rid in free.items():
                        self.assertIn(
                            rid, self._ids(ov, key),
                            f"[{label}] the creator lost sight of its own "
                            f"unlinked record in {key} while Program "
                            f"{env_label} was active -- create-then-link "
                            "cannot work")
                    # A record already linked to ONE specific Program never
                    # appears as pending-link, from either side.
                    for owner in (a, b):
                        self.assertNotIn(owner["club1"]["id"],
                                         self._ids(ov, "pending_link_clubs"),
                                         (label, env_label))
                        self.assertNotIn(
                            owner["org"]["id"],
                            self._ids(ov, "pending_link_organizations"),
                            (label, env_label))
                        self.assertNotIn(owner["venue"]["id"],
                                         self._ids(ov, "pending_link_venues"),
                                         (label, env_label))
                        self.assertNotIn(owner["rink"]["id"],
                                         self._ids(ov, "pending_link_rinks"),
                                         (label, env_label))
                        self.assertNotIn(
                            owner["slot"]["id"],
                            self._ids(ov, "pending_link_ice_slots"),
                            (label, env_label))
                        self.assertNotIn(
                            owner["official"]["id"],
                            self._ids(ov, "pending_link_officials"),
                            (label, env_label))

                # (2) A DIFFERENT, Program-scoped Arena Manager (active in
                #     Program B, fully authorized for its own work) may not
                #     enumerate any of them.
                other = "user_other_arena_mgr"
                ctx = api.set_active_context(
                    other, Role.ARENA_MANAGER, {}, b["program"]["id"],
                    b["season1"]["id"], None)
                self.assertNotIn("error", ctx, ctx)
                ov_other = api.get_setup_overview_v2(other, Role.ARENA_MANAGER,
                                                     {})
                self.assertNotIn("error", ov_other, ov_other)
                for key in _ALL_PENDING_KEYS:
                    self.assertEqual(
                        ov_other[key], [],
                        f"[{label}] a second Arena Manager enumerated "
                        f"{key} it did not create -- this is the "
                        "installation-wide disclosure the ruling reverses")
                # Positive control: this caller is NOT blind -- Program B's
                # own linked records are right there.
                self.assertIn(b["club1"]["id"], self._ids(ov_other, "clubs"),
                              (label, "own Program's club"))
                self.assertIn(b["venue"]["id"], self._ids(ov_other, "venues"),
                              (label, "own Program's venue"))
                self._assert_no_reversed_keys(ov_other, label)

                # (3) A ZERO-scope identity (a Coach whose team_id resolves to
                #     nothing) gets the fully empty shape -- pending-link
                #     lists included. This is the case the old contract
                #     answered with the installation's whole unlinked
                #     inventory.
                ov_zero = api.get_setup_overview_v2(
                    "user_nobody", Role.COACH, {"team_id": "team_gone"})
                self.assertNotIn("error", ov_zero, ov_zero)
                for key in _ALL_OVERVIEW_KEYS + _ALL_PENDING_KEYS:
                    self.assertEqual(
                        ov_zero[key], [],
                        f"[{label}] a caller authorized for ZERO Programs "
                        f"received {key}")
                self._assert_no_reversed_keys(ov_zero, label)
                _close(store)

    def test_ownership_comes_from_the_create_entry_only(self):
        """The creator-owned contract reads ownership from the record's
        CREATE audit entry and nothing else, and an identity-less caller owns
        nothing.

        Both restrictions matter. If any later write counted, an operator who
        guessed an id and poked a rename/reassign route would thereby earn the
        right to enumerate the record afterwards — turning a write-side IDOR
        into a read-side one. And if ``user_id=None`` matched, every row an
        internal/seed call path created with ``actor_id=None`` would be handed
        to every identity-less caller at once.
        """
        for label, store in _backends():
            with self.subTest(backend=label):
                api = ApiService(store)
                a = self._build_program_env(api, "A")

                mine = api.create_club("Mine", actor_id="user_mine")
                theirs = api.create_club("Theirs", actor_id="user_theirs")
                seeded = api.create_club("Seeded")          # actor_id None
                for r in (mine, theirs, seeded):
                    self.assertNotIn("error", r, r)

                # A LATER write by `user_mine` against the club it did NOT
                # create. Written straight to the audit log (the same fixture
                # idiom test_league_filtered_dashboard.py uses for audit
                # rows), because what is under test is how this read
                # INTERPRETS such an entry, not which write path produced it.
                with store.transaction():
                    store.add_setup_audit(SetupAuditLog(
                        id=store.next_id("audit"), action="club_updated",
                        entity_type="club", entity_id=theirs["id"],
                        at=datetime.now(timezone.utc), actor_id="user_mine",
                        detail={"name": "Theirs Renamed"}))

                ctx = api.set_active_context(
                    "user_mine", Role.LEAGUE_ADMIN, {}, a["program"]["id"],
                    a["season1"]["id"], None)
                self.assertNotIn("error", ctx, ctx)
                ov = api.get_setup_overview_v2("user_mine", Role.LEAGUE_ADMIN,
                                               {})
                pending = self._ids(ov, "pending_link_clubs")
                self.assertEqual(
                    pending, {mine["id"]},
                    f"[{label}] pending-link club ownership came from "
                    "something other than this caller's own create entry")

                # An identity-less scoped caller owns nothing at all -- not
                # even the `actor_id=None` seeded row.
                ov_anon = api.get_setup_overview_v2(None, Role.LEAGUE_ADMIN, {})
                self.assertEqual(
                    ov_anon["pending_link_clubs"], [],
                    f"[{label}] an identity-less caller was handed the rows "
                    "created with actor_id=None")
                _close(store)

    # -- 7. Viewer: a pure read, no mutation ---------------------------------
    def test_viewer_role_is_a_pure_read(self):
        """A sanity check that calling this facade method with ``Role.VIEWER``
        never itself mutates store state — it is a pure read composed from
        store query methods. (Viewer's actual permission gate — rejecting
        mutation ENDPOINTS — is enforced at the HTTP/web authorization layer,
        not this Python facade; a facade-level test has no HTTP request to
        fabricate, so that half of the Viewer contract is out of scope here by
        design.)"""
        for label, store in _backends():
            with self.subTest(backend=label):
                api = ApiService(store)
                self._build_program_env(api, "A")

                def _snapshot():
                    return {
                        "programs": {p.id for p in api.store.all_programs()},
                        "seasons": {s.id for s in api.store.all_seasons()},
                        "leagues": {lg.id for lg in api.store.all_leagues()},
                        "divisions": {d.id for d in api.store.all_divisions()},
                        "teams": {t.id for t in api.store.all_teams()},
                        "clubs": {c.id for c in api.store.all_clubs()},
                        "organizations":
                            {o.id for o in api.store.all_organizations()},
                        "officials": {o.id for o in api.store.all_officials()},
                        "venues": {v.id for v in api.store.all_venues()},
                        "rinks": {r.id for r in api.store.all_rinks()},
                        "ice_slots": {s.id for s in api.store.all_ice_slots()},
                    }

                before = _snapshot()
                ov = api.get_setup_overview_v2("viewer_1", Role.VIEWER, {})
                self.assertNotIn("error", ov, ov)
                after = _snapshot()
                self.assertEqual(before, after, label)
                _close(store)


class SetupOverviewV2HttpTest(unittest.TestCase):
    """The two contracts that cross the ROUTE boundary, over real
    authenticated HTTP against the running server:

    * the Season ceiling, driven by the persisted context the ``/api/context``
      route writes (never a client-supplied scoping parameter);
    * the creator-owned pending-link list, which is keyed on the SESSION's own
      account id — the same id the write routes record as ``actor_id``. That
      identity linkage exists only at this layer: a facade test passes any
      string it likes for both, so only this proves the two really are the
      same value in production.
    """

    @classmethod
    def setUpClass(cls):
        srv = __import__("hockey_scheduler.web.server", fromlist=["x"])
        cls.srv = srv
        srv.STATE.reset()
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), srv.Handler)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever,
                                      daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.thread.join(timeout=5)
        cls.httpd.server_close()

    def _client(self):
        return urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(CookieJar()))

    def _req(self, opener, method, path, body=None):
        url = f"http://127.0.0.1:{self.port}{path}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with opener.open(req) as r:
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def _login(self, username):
        c = self._client()
        status, resp = self._req(c, "POST", "/api/auth/login",
                                 {"username": username, "password": "demo"})
        self.assertEqual(status, 200, resp)
        return c

    def _post(self, c, entity, body):
        status, resp = self._req(c, "POST", f"/api/v2/setup/{entity}", body)
        self.assertEqual(status, 200, (entity, resp))
        self.assertNotIn("error", resp, (entity, resp))
        return resp

    def _select(self, c, program_id, season_id=None, league_id=None):
        body = {"program_id": program_id}
        if season_id is not None:
            body["season_id"] = season_id
        if league_id is not None:
            body["league_id"] = league_id
        status, resp = self._req(c, "POST", "/api/context", body)
        self.assertEqual(status, 200, resp)
        return resp

    def _overview(self, c):
        status, ov = self._req(c, "GET", "/api/v2/setup/overview")
        self.assertEqual(status, 200, ov)
        self.assertNotIn("error", ov, ov)
        return ov

    @staticmethod
    def _ids(ov, key):
        return {row["id"] for row in ov.get(key, [])}

    def test_http_season_ceiling_flips_with_the_persisted_selection(self):
        """B1 over HTTP: one Program, two Seasons, distinguishable records in
        each. The route serves only the ACTIVE Season's, under a selected
        League and under "No League" alike, and flips when the other Season is
        selected — with the selection coming from the PERSISTED context, never
        from anything the client sent to this GET."""
        c = self._login("admin")
        org = self._post(c, "organization", {"name": "HTTP Ceil Org"})
        program = self._post(c, "program",
                             {"name": "HTTP Ceil Program",
                              "operator_organization_id": org["id"]})
        # #409: a Season create is PROGRAM-AXIS and a League/Division create
        # is SEASON-OWNED, so each is judged against the axis the operator
        # CHOSE. Select the Program, then each Season as its own records are
        # built -- the same per-Season selection the venue grants below already
        # make, extended to the creates. Every record is the one this test
        # always built, and the assertions set their own selection anyway.
        self._select(c, program["id"])
        s1 = self._post(c, "season",
                        {"program_id": program["id"], "name": "HTTP-S1"})
        s2 = self._post(c, "season",
                        {"program_id": program["id"], "name": "HTTP-S2"})
        self._select(c, program["id"], s1["id"])
        lg1 = self._post(c, "league", {"season_id": s1["id"], "name": "HTTP-L1"})
        d1 = self._post(c, "division", {"league_id": lg1["id"], "name": "HTTP-D1"})
        self._select(c, program["id"], s2["id"])
        lg2 = self._post(c, "league", {"season_id": s2["id"], "name": "HTTP-L2"})
        d2 = self._post(c, "division", {"league_id": lg2["id"], "name": "HTTP-D2"})
        v1 = self._post(c, "venue", {"name": "HTTP-V1",
                                     "organization_id": org["id"]})
        v2 = self._post(c, "venue", {"name": "HTTP-V2",
                                     "organization_id": org["id"]})
        # Each grant is a WRITE naming an EXISTING Season, so its Season end is
        # ceilinged on the ACTIVE Season (#369 target authorization). Select
        # the destination Season for each one — the guard is honoured, not
        # bypassed; the assertions below set their own selection anyway.
        self._select(c, program["id"], s1["id"])
        self._post(c, f"seasons/{s1['id']}/venue-access", {"venue_id": v1["id"]})
        self._select(c, program["id"], s2["id"])
        self._post(c, f"seasons/{s2['id']}/venue-access", {"venue_id": v2["id"]})
        r1 = self._post(c, "rink", {"venue_id": v1["id"], "name": "HTTP-R1"})
        r2 = self._post(c, "rink", {"venue_id": v2["id"], "name": "HTTP-R2"})

        for league_choice in (None, lg1["id"]):
            with self.subTest(season="S1", league=league_choice):
                self._select(c, program["id"], s1["id"], league_choice)
                ov = self._overview(c)
                self.assertEqual(self._ids(ov, "seasons"), {s1["id"]}, ov)
                self.assertEqual(self._ids(ov, "divisions"), {d1["id"]}, ov)
                self.assertEqual(self._ids(ov, "venues"), {v1["id"]}, ov)
                self.assertEqual(self._ids(ov, "rinks"), {r1["id"]}, ov)
                self.assertNotIn(s2["id"], self._ids(ov, "seasons"))
                self.assertNotIn(d2["id"], self._ids(ov, "divisions"))
                self.assertNotIn(v2["id"], self._ids(ov, "venues"))
                self.assertNotIn(r2["id"], self._ids(ov, "rinks"))
                # Both permanent Leagues stay offered (Program structure).
                self.assertEqual(self._ids(ov, "leagues"),
                                 {lg1["id"], lg2["id"]}, ov)

        self._select(c, program["id"], s2["id"])
        ov = self._overview(c)
        self.assertEqual(self._ids(ov, "seasons"), {s2["id"]}, ov)
        self.assertEqual(self._ids(ov, "divisions"), {d2["id"]}, ov)
        self.assertEqual(self._ids(ov, "venues"), {v2["id"]}, ov)
        self.assertEqual(self._ids(ov, "rinks"), {r2["id"]}, ov)

    def test_http_pending_link_records_are_visible_only_to_their_creator(self):
        """B2 over HTTP: the Arena Manager persona creates never-linked
        records; the League Admin persona — a different real session, MORE
        broadly authorized than the creator and working in the same
        installation — must not receive any of them. The creating session
        still sees its own, which is what keeps its create-then-link flow
        alive.

        The Arena Manager is the identity the blocker names: it holds
        MANAGE_ARENA, so this route answers it 200 — and the reversed
        contract answered it with every never-linked record in the
        installation, Officials included.

        Only this layer can prove the identity linkage the contract rests on:
        the ``actor_id`` the write route recorded and the ``user_id`` the read
        route resolves its context from are the SAME session account id. A
        facade-level test passes both by hand and would agree with itself
        even if production wired two different values."""
        # The League Admin session creates the never-linked records (Club and
        # Official are MANAGE_SETUP/MANAGE_SCHEDULE routes an Arena Manager
        # cannot reach at all).
        admin = self._login("admin")
        club = self._post(admin, "club", {"name": "HTTP-Pending-Club"})
        venue = self._post(admin, "venue", {"name": "HTTP-Pending-Venue"})
        official = self._post(admin, "official",
                              {"name": "HTTP-Pending-Official"})

        own = self._overview(admin)
        self.assertIn(club["id"], self._ids(own, "pending_link_clubs"), own)
        self.assertIn(venue["id"], self._ids(own, "pending_link_venues"), own)
        self.assertIn(official["id"],
                      self._ids(own, "pending_link_officials"), own)

        arena = self._login("arena")
        ov = self._overview(arena)
        self.assertNotIn(
            club["id"], self._ids(ov, "pending_link_clubs"),
            "an Arena Manager enumerated another session's unlinked Club")
        self.assertNotIn(
            venue["id"], self._ids(ov, "pending_link_venues"),
            "an Arena Manager enumerated another session's unlinked Venue")
        self.assertNotIn(
            official["id"], self._ids(ov, "pending_link_officials"),
            "an Arena Manager enumerated another session's unlinked "
            "Official -- an official's name is personal data")
        # ...and the reversed installation-wide buckets are gone from the
        # wire format entirely.
        for key in _REVERSED_UNASSIGNED_KEYS:
            self.assertNotIn(key, ov, key)

        # The reverse direction too: what the Arena Manager creates stays its
        # own, and the more broadly authorized League Admin does not get it.
        arena_venue = self._post(arena, "venue",
                                 {"name": "HTTP-Pending-Arena-Venue"})
        self.assertIn(arena_venue["id"],
                      self._ids(self._overview(arena), "pending_link_venues"))
        self.assertNotIn(
            arena_venue["id"],
            self._ids(self._overview(admin), "pending_link_venues"),
            "a League Admin enumerated an Arena Manager's unlinked Venue")


if __name__ == "__main__":
    unittest.main()
