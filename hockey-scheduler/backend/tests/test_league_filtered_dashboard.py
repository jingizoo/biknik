"""Regression tests for ``get_demo_overview``'s Program/Season/League scoping
(#367, #369) -- the Home/Arena/Schedule/Public data source behind the
Dashboard, and issue #367's own named required-consistent surface ("Dashboard
counts and standings snapshot").

Before #367, ``get_demo_overview`` was a single global, fully unauthenticated
view of the ENTIRE installation -- every Program's teams/divisions/games in
one response, reachable with no session at all. The endpoint now optionally
accepts ``(user_id, role, scope)``; when a real ``role`` is supplied (the
``/api/demo/overview`` HTTP route always supplies one now, and requires a
real session to do it), every collection with a real Program/Season/League
join is scoped to the caller's resolved active context (``ContextService.
resolve_with_league``): mandatory Program, the resolved Season as a HARD
ceiling, and the selected League (else every League's data -- the "No
League" broader view). Called with no arguments at all (``role`` left
``None``, the default), the endpoint is exactly the pre-#367 full,
unfiltered installation view -- dozens of existing direct/internal call
sites across the suite rely on this default staying unchanged.

#369 review made three corrections to an intermediate revision of this
method, each covered below:

  1. Season is a HARD filter again (an intermediate revision had made it
     non-restrictive; review rejected that as silently widening the
     contract). An operator active in Season S1 never sees S2's divisions,
     registrations, games, results, or venue/rink/ice-slot inventory -- even
     under the SAME Program+League, even when S2 is ARCHIVED. Teams are
     Program+League scoped only (confirmed against the source) and are NOT
     narrowed by Season. A Program-only context (no Season resolved) narrows
     every Season-scoped collection to EMPTY, never "every Season".
  2. Zero-authorized-Program fails CLOSED with NO exception: every key in
     the returned dict -- including Clubs/Organizations/Officials/Venues/
     Rinks/IceSlots, a previous "bootstrap" exception -- comes back empty,
     never an installation-wide list of names.
  3. ``setup_audit`` is now filtered to the active Program (Program-LEVEL
     only, not further narrowed by Season/League) via a real per-
     entity_type parent-chain resolver; a row whose entity_type has no
     resolvable parent chain at all (``user_account``, ``auth``,
     ``guardian_link``, ``import_batch``, anything unrecognized), or whose
     entity_id is genuinely dangling, is OMITTED rather than guessed.

Coverage:
  * the no-args legacy call performs no scoping at all (sanity);
  * two Programs, each with a full vertical slice, never leak into each
    other's resolved view -- the primary "zero cross-Program leakage" proof;
  * two Leagues in the SAME Program+Season narrow teams/divisions/
    registrations/schedule independently; "No League" shows the union; a
    league-less (exhibition) Game is universally eligible regardless of
    which League is selected;
  * two Seasons in the SAME Program+League narrow every Season-scoped
    collection independently, including facility inventory (venues/rinks/
    ice slots), even when the other Season is ARCHIVED; a Program-only
    context narrows those collections to EMPTY;
  * ``role=None`` vs a real role at the facade level, and the named empty
    state -- now fully empty, no exception -- when the resolved role/scope
    has NO authorized Program at all, for Coach/Official/Guardian
    identities specifically, even with other Programs' real data sitting in
    the same store;
  * ``setup_audit`` scopes to the active Program only, omitting both the
    sibling Program's activity and any genuinely unjoinable row;
  * the full role matrix (League Admin, Arena Manager, Viewer, Coach,
    Player, Official, Guardian) each resolving to its own Program only;
  * a degraded/corrupted League selection never leaks data or errors;
  * the real HTTP route: 401 signed-out, 200 Program-scoped for a real
    session, per-session isolation, and HTTP-level checks for the zero-
    Program and audit-filtering corrections.
"""

import json
import os
import threading
import unittest
import urllib.error
import urllib.request
from dataclasses import asdict
from datetime import datetime, timezone
from http.cookiejar import CookieJar
from http.server import ThreadingHTTPServer

from helpers import BACKEND  # noqa: F401  (ensures sys.path is set up)

from hockey_scheduler.api import ApiService
from hockey_scheduler.domain import GuardianLink, Role, SeasonStatus, SetupAuditLog
from hockey_scheduler.domain.setup_models import ActiveContext
from hockey_scheduler.store import InMemoryStore, SqlStore

ADMIN = (Role.LEAGUE_ADMIN, {})

# The exact empty shape ``get_demo_overview`` returns when a real role/scope
# resolves to NO authorized Program at all -- every collection key present
# and empty, never an error, and (#369) never an exception for Clubs/
# Organizations/Officials/Venues/Rinks/IceSlots either (mirrored verbatim
# from api/service.py).
_EMPTY_OVERVIEW = {
    "league": None, "leagues": [], "seasons": [], "levels": [],
    "divisions": [], "clubs": [], "teams": [],
    "organizations": [], "venues": [], "rinks": [],
    "ice_slots": [], "officials": [], "schedule": [],
    "public_fixtures": [], "registrations": [],
    "setup_audit": [], "setup_audit_count": 0,
}


def _backends():
    yield "memory", InMemoryStore()
    yield "sqlite", SqlStore(":memory:")
    url = os.environ.get("TEST_DATABASE_URL")
    if url:
        SqlStore(url).clear_all_data()
        yield "postgres", SqlStore(url)


def _close(store):
    if isinstance(store, SqlStore):
        store.close()


def _ids(rows, key="id"):
    return {r[key] for r in rows}


def _build_scheduled_program(api, pname, actor_id="admin"):
    """A Program with its own Season/League/Division, two Teams registered
    into it, and one schedulable Game -- a minimal but COMPLETE vertical
    slice so every collection ``get_demo_overview`` scopes (leagues, seasons,
    teams, divisions, registrations, schedule) has real data of its own to
    isolate against a sibling Program's. Every create call along the way
    records a real ``setup_audit`` row as a side effect."""
    program = api.create_program(pname, "US", "UTC", actor_id=actor_id)
    season = api.create_season(program["id"], "Season", actor_id=actor_id)
    league = api.create_league(season["id"], "League", actor_id=actor_id)
    division = api.create_division(season["id"], "Division",
                                   league_id=league["id"], actor_id=actor_id)
    club = api.create_club(f"{pname} Club", actor_id=actor_id)
    home = api.create_team(club_id=club["id"], name=f"{pname} Home",
                           league_id=league["id"], actor_id=actor_id)
    away = api.create_team(club_id=club["id"], name=f"{pname} Away",
                           league_id=league["id"], actor_id=actor_id)
    reg_home = api.register_team_for_season(
        season["id"], home["id"], division["id"], actor_id=actor_id,
        league_id=league["id"])
    reg_away = api.register_team_for_season(
        season["id"], away["id"], division["id"], actor_id=actor_id,
        league_id=league["id"])
    assert "error" not in reg_home, reg_home
    assert "error" not in reg_away, reg_away
    venue = api.create_venue(f"{pname} Venue", league_id=program["id"],
                             actor_id=actor_id)
    api.grant_season_venue_access(season["id"], venue["id"], actor_id=actor_id)
    rink = api.create_rink(venue["id"], f"{pname} Rink", actor_id=actor_id)
    slot = api.create_ice_slot(
        rink["id"], "2026-09-01T18:30:00+00:00", "2026-09-01T20:00:00+00:00",
        actor_id=actor_id)
    game = api.create_game(season["id"], division["id"], home["id"],
                           away["id"], slot["id"], actor_id=actor_id,
                           league_id=league["id"])
    assert "error" not in game, game
    return {
        "program_id": program["id"], "season_id": season["id"],
        "league_id": league["id"], "division_id": division["id"],
        "club_id": club["id"],
        "team_ids": {home["id"], away["id"]},
        "reg_team_ids": {home["id"], away["id"]},
        "game_id": game["id"], "venue_id": venue["id"], "rink_id": rink["id"],
        "ice_slot_id": slot["id"],
    }


class DemoOverviewBackCompatTest(unittest.TestCase):
    """Called with no context args at all, the endpoint is exactly the
    pre-#367 full, unfiltered installation view -- the many existing direct
    unit-test call sites elsewhere in the suite depend on this staying true;
    this is one more explicit lock on the contract, not the primary focus."""

    def test_no_args_returns_full_unfiltered_view(self):
        api = ApiService(InMemoryStore())
        program = api.create_program("Prog", "US", "UTC")
        season = api.create_season(program["id"], "Fall")
        league = api.create_league(season["id"], "Adult League")
        club = api.create_club("Club")
        team = api.create_team(club_id=club["id"], name="Team",
                               league_id=league["id"])
        self.assertNotIn("error", team, team)

        result = api.get_demo_overview()
        self.assertNotIn("error", result, result)
        self.assertIn(program["id"], _ids(result["leagues"]))
        self.assertIn(season["id"], _ids(result["seasons"]))
        self.assertIn(team["id"], _ids(result["teams"]))


class DemoOverviewCrossProgramIsolationTest(unittest.TestCase):
    """The primary "zero cross-Program leakage" proof: two Programs, each
    with a full vertical slice (League/Division/Teams/Registrations/Game),
    must never bleed into each other's Dashboard view."""

    def test_program_a_and_b_never_leak_into_each_other(self):
        api = ApiService(InMemoryStore())
        a = _build_scheduled_program(api, "Prog A")
        b = _build_scheduled_program(api, "Prog B")

        set_a = api.set_active_context(
            "u1", *ADMIN, a["program_id"], a["season_id"])
        self.assertNotIn("error", set_a, set_a)
        view_a = api.get_demo_overview("u1", *ADMIN)
        self.assertNotIn("error", view_a, view_a)
        self.assertEqual(_ids(view_a["leagues"]), {a["program_id"]})
        self.assertEqual(_ids(view_a["seasons"]), {a["season_id"]})
        self.assertEqual(_ids(view_a["teams"]), a["team_ids"])
        self.assertEqual(_ids(view_a["divisions"]), {a["division_id"]})
        self.assertEqual(
            {r["team_id"] for r in view_a["registrations"]},
            a["reg_team_ids"])
        self.assertEqual(
            {g["game_id"] for g in view_a["schedule"]}, {a["game_id"]})
        # Nothing of Program B's leaks into Program A's view.
        self.assertNotIn(b["program_id"], _ids(view_a["leagues"]))
        self.assertTrue(b["team_ids"].isdisjoint(_ids(view_a["teams"])))
        self.assertNotIn(b["division_id"], _ids(view_a["divisions"]))
        self.assertNotIn(
            b["game_id"], {g["game_id"] for g in view_a["schedule"]})

        set_b = api.set_active_context(
            "u1", *ADMIN, b["program_id"], b["season_id"])
        self.assertNotIn("error", set_b, set_b)
        view_b = api.get_demo_overview("u1", *ADMIN)
        self.assertNotIn("error", view_b, view_b)
        self.assertEqual(_ids(view_b["leagues"]), {b["program_id"]})
        self.assertEqual(_ids(view_b["seasons"]), {b["season_id"]})
        self.assertEqual(_ids(view_b["teams"]), b["team_ids"])
        self.assertEqual(_ids(view_b["divisions"]), {b["division_id"]})
        self.assertEqual(
            {r["team_id"] for r in view_b["registrations"]},
            b["reg_team_ids"])
        self.assertEqual(
            {g["game_id"] for g in view_b["schedule"]}, {b["game_id"]})
        # And the reverse: nothing of Program A's leaks into Program B's view.
        self.assertNotIn(a["program_id"], _ids(view_b["leagues"]))
        self.assertTrue(a["team_ids"].isdisjoint(_ids(view_b["teams"])))
        self.assertNotIn(a["division_id"], _ids(view_b["divisions"]))
        self.assertNotIn(
            a["game_id"], {g["game_id"] for g in view_b["schedule"]})


class DemoOverviewLeagueNarrowingTest(unittest.TestCase):
    """Two Leagues in the SAME Program+Season, with deliberately DIFFERENT
    Team/Registration/Game counts, narrow independently; "No League" shows
    the union; a league-less (exhibition) Game is universally eligible
    regardless of which League is selected -- the real edge case this locks
    down."""

    def setUp(self):
        self.api = api = ApiService(InMemoryStore())
        self.program = api.create_program("P", "US", "UTC")
        self.season = api.create_season(self.program["id"], "S")
        self.league_x = api.create_league(self.season["id"], "League X")
        self.league_y = api.create_league(self.season["id"], "League Y")
        self.div_x = api.create_division(self.season["id"], "Div X",
                                         league_id=self.league_x["id"])
        self.div_y = api.create_division(self.season["id"], "Div Y",
                                         league_id=self.league_y["id"])
        club = api.create_club("Club")
        # League X: two teams. League Y: three. Deliberately different
        # counts so a filter that accidentally passes everything through
        # (or nothing) cannot masquerade as correct.
        self.tx1 = api.create_team(club_id=club["id"], name="X1",
                                   league_id=self.league_x["id"])
        self.tx2 = api.create_team(club_id=club["id"], name="X2",
                                   league_id=self.league_x["id"])
        self.ty1 = api.create_team(club_id=club["id"], name="Y1",
                                   league_id=self.league_y["id"])
        self.ty2 = api.create_team(club_id=club["id"], name="Y2",
                                   league_id=self.league_y["id"])
        self.ty3 = api.create_team(club_id=club["id"], name="Y3",
                                   league_id=self.league_y["id"])
        for t, div, lg in ((self.tx1, self.div_x, self.league_x),
                          (self.tx2, self.div_x, self.league_x),
                          (self.ty1, self.div_y, self.league_y),
                          (self.ty2, self.div_y, self.league_y),
                          (self.ty3, self.div_y, self.league_y)):
            reg = api.register_team_for_season(
                self.season["id"], t["id"], div["id"], league_id=lg["id"])
            self.assertNotIn("error", reg, reg)

        venue = api.create_venue("V", league_id=self.program["id"])
        api.grant_season_venue_access(self.season["id"], venue["id"])
        rink = api.create_rink(venue["id"], "R")
        slots = [api.create_ice_slot(
                    rink["id"], f"2026-09-0{i}T18:30:00+00:00",
                    f"2026-09-0{i}T20:00:00+00:00")
                for i in range(1, 5)]
        for s in slots:
            self.assertNotIn("error", s, s)

        # League X: one game. League Y: two. Again deliberately unequal.
        gx = api.create_game(self.season["id"], self.div_x["id"],
                             self.tx1["id"], self.tx2["id"], slots[0]["id"],
                             league_id=self.league_x["id"])
        gy1 = api.create_game(self.season["id"], self.div_y["id"],
                              self.ty1["id"], self.ty2["id"], slots[1]["id"],
                              league_id=self.league_y["id"])
        gy2 = api.create_game(self.season["id"], self.div_y["id"],
                              self.ty2["id"], self.ty3["id"], slots[2]["id"],
                              league_id=self.league_y["id"])
        # The league-LESS game: an exhibition crossing League X/Y, owning no
        # LeagueSeason at all -- must be "universally eligible" regardless of
        # which League is selected (#367's own leagueless-team pattern).
        ge = api.create_game(self.season["id"], None, self.tx1["id"],
                             self.ty1["id"], slots[3]["id"],
                             game_type="exhibition")
        for g in (gx, gy1, gy2, ge):
            self.assertNotIn("error", g, g)
        self.gx, self.gy1, self.gy2, self.ge = gx, gy1, gy2, ge

    def _select(self, league_id):
        selection = self.api.set_active_context(
            "u1", *ADMIN, self.program["id"], self.season["id"], league_id)
        self.assertNotIn("error", selection, selection)
        return self.api.get_demo_overview("u1", *ADMIN)

    def test_league_x_shows_only_its_own_plus_the_leagueless_game(self):
        view = self._select(self.league_x["id"])
        self.assertEqual(_ids(view["teams"]),
                         {self.tx1["id"], self.tx2["id"]})
        self.assertEqual(_ids(view["divisions"]), {self.div_x["id"]})
        self.assertEqual({r["team_id"] for r in view["registrations"]},
                         {self.tx1["id"], self.tx2["id"]})
        self.assertEqual(
            {g["game_id"] for g in view["schedule"]},
            {self.gx["id"], self.ge["id"]},
            "the league-less exhibition must still show under League X")
        self.assertNotIn(
            self.gy1["id"], {g["game_id"] for g in view["schedule"]})
        self.assertNotIn(
            self.gy2["id"], {g["game_id"] for g in view["schedule"]})

    def test_league_y_shows_only_its_own_plus_the_leagueless_game(self):
        view = self._select(self.league_y["id"])
        self.assertEqual(
            _ids(view["teams"]),
            {self.ty1["id"], self.ty2["id"], self.ty3["id"]})
        self.assertEqual(_ids(view["divisions"]), {self.div_y["id"]})
        self.assertEqual(
            {r["team_id"] for r in view["registrations"]},
            {self.ty1["id"], self.ty2["id"], self.ty3["id"]})
        self.assertEqual(
            {g["game_id"] for g in view["schedule"]},
            {self.gy1["id"], self.gy2["id"], self.ge["id"]},
            "the league-less exhibition must still show under League Y")
        self.assertNotIn(
            self.gx["id"], {g["game_id"] for g in view["schedule"]})

    def test_no_league_selected_shows_the_union_of_both(self):
        view = self._select(None)
        self.assertEqual(
            _ids(view["teams"]),
            {self.tx1["id"], self.tx2["id"], self.ty1["id"], self.ty2["id"],
             self.ty3["id"]})
        self.assertEqual(
            _ids(view["divisions"]), {self.div_x["id"], self.div_y["id"]})
        self.assertEqual(
            {g["game_id"] for g in view["schedule"]},
            {self.gx["id"], self.gy1["id"], self.gy2["id"], self.ge["id"]})


class DemoOverviewFacilityScopeTest(unittest.TestCase):
    """Venues/Rinks/IceSlots have no competition-League axis at all -- they
    scope by Program+Season only, via active SeasonVenueAccess. #369 review
    correction: an intermediate revision had made facility inventory ignore
    the resolved Season entirely (a Program-wide "union of every Season"
    view); review correctly rejected that as silently widening the released
    active-context contract -- physical resources are exactly as
    Season-scoped as every other collection this method returns. See
    ``DemoOverviewSeasonHardFilterTest`` for the dedicated regression."""

    def test_two_programs_each_see_only_their_own_facility_inventory(self):
        api = ApiService(InMemoryStore())
        pa = api.create_program("Prog A", "US", "UTC")
        sa = api.create_season(pa["id"], "Season A")
        va = api.create_venue("Venue A", league_id=pa["id"])
        ra = api.create_rink(va["id"], "Rink A")
        ia = api.create_ice_slot(ra["id"], "2026-09-01T18:30:00+00:00",
                                 "2026-09-01T20:00:00+00:00")
        api.grant_season_venue_access(sa["id"], va["id"])

        pb = api.create_program("Prog B", "US", "UTC")
        sb = api.create_season(pb["id"], "Season B")
        vb = api.create_venue("Venue B", league_id=pb["id"])
        rb = api.create_rink(vb["id"], "Rink B")
        ib = api.create_ice_slot(rb["id"], "2026-09-01T18:30:00+00:00",
                                 "2026-09-01T20:00:00+00:00")
        api.grant_season_venue_access(sb["id"], vb["id"])

        api.set_active_context("u1", *ADMIN, pa["id"], sa["id"])
        view_a = api.get_demo_overview("u1", *ADMIN)
        self.assertEqual(_ids(view_a["venues"]), {va["id"]})
        self.assertEqual(_ids(view_a["rinks"]), {ra["id"]})
        self.assertEqual(_ids(view_a["ice_slots"]), {ia["id"]})

        api.set_active_context("u1", *ADMIN, pb["id"], sb["id"])
        view_b = api.get_demo_overview("u1", *ADMIN)
        self.assertEqual(_ids(view_b["venues"]), {vb["id"]})
        self.assertEqual(_ids(view_b["rinks"]), {rb["id"]})
        self.assertEqual(_ids(view_b["ice_slots"]), {ib["id"]})

    def test_facility_inventory_now_excludes_a_different_seasons_venue_grant(self):
        """#369 review correction (reversing the prior test's own assertion):
        facility inventory now DOES exclude a different Season's venue-access
        grant, even within the SAME Program -- proven directly here; the full
        matrix (including ARCHIVED and Program-only) lives in
        ``DemoOverviewSeasonHardFilterTest``."""
        api = ApiService(InMemoryStore())
        program = api.create_program("Prog", "US", "UTC")
        s1 = api.create_season(program["id"], "Season 1")
        v1 = api.create_venue("Venue 1", league_id=program["id"])
        r1 = api.create_rink(v1["id"], "Rink 1")
        i1 = api.create_ice_slot(r1["id"], "2026-09-01T18:30:00+00:00",
                                 "2026-09-01T20:00:00+00:00")
        api.grant_season_venue_access(s1["id"], v1["id"])

        s2 = api.create_season(program["id"], "Season 2")
        v2 = api.create_venue("Venue 2", league_id=program["id"])
        r2 = api.create_rink(v2["id"], "Rink 2")
        i2 = api.create_ice_slot(r2["id"], "2026-10-01T18:30:00+00:00",
                                 "2026-10-01T20:00:00+00:00")
        api.grant_season_venue_access(s2["id"], v2["id"])

        # Season 1 active: ONLY Season 1's venue/rink/ice show -- Season 2's
        # (granted to a DIFFERENT Season in the SAME Program) must NOT.
        api.set_active_context("u1", *ADMIN, program["id"], s1["id"])
        view1 = api.get_demo_overview("u1", *ADMIN)
        self.assertEqual(_ids(view1["venues"]), {v1["id"]})
        self.assertEqual(_ids(view1["rinks"]), {r1["id"]})
        self.assertEqual(_ids(view1["ice_slots"]), {i1["id"]})

        # Season 2 active: the reverse.
        api.set_active_context("u1", *ADMIN, program["id"], s2["id"])
        view2 = api.get_demo_overview("u1", *ADMIN)
        self.assertEqual(_ids(view2["venues"]), {v2["id"]})
        self.assertEqual(_ids(view2["rinks"]), {r2["id"]})
        self.assertEqual(_ids(view2["ice_slots"]), {i2["id"]})

        # Program-only (no Season chosen): narrows to EMPTY, never the union
        # -- #369's own "never fall back to every Season" correction.
        api.set_active_context("u1", *ADMIN, program["id"], None)
        view_none = api.get_demo_overview("u1", *ADMIN)
        self.assertEqual(view_none["venues"], [])
        self.assertEqual(view_none["rinks"], [])
        self.assertEqual(view_none["ice_slots"], [])


class DemoOverviewSeasonHardFilterTest(unittest.TestCase):
    """#369 review's central correction: the resolved Season is a HARD
    ceiling again. Two Seasons in the SAME Program, bound to the SAME
    League, with distinguishable divisions/registrations/games/results/
    venue-grants in each -- proven on Memory/SQLite/PostgreSQL, per the
    reviewer's explicit ask for this specific proof."""

    def _build(self, api):
        """Two Seasons in the SAME Program, bound to the SAME permanent
        League. Each Season gets its OWN pair of registered Teams (a game
        needs both sides registered in the Season it is played in) -- but
        ALL FOUR Teams are permanently members of the same League/Program,
        so Team visibility (Program+League scoped only, never Season-scoped,
        confirmed against the source) stays identical across either Season
        selection; only the SEASON-scoped collections (divisions,
        registrations, games/results, venue/rink/ice inventory) narrow."""
        program = api.create_program("Prog", "US", "UTC")
        s1 = api.create_season(program["id"], "S1")
        s2 = api.create_season(program["id"], "S2")
        league = api.create_league(s1["id"], "League")
        # Bind the SAME permanent League to S2 too -- deliberately the same
        # competition across both Seasons, so a League selection is never
        # what tells the two apart; only the resolved SEASON is.
        bound = api.setup.create_league_season(league["id"], s2["id"])
        self.assertIsNotNone(bound)
        div1 = api.create_division(s1["id"], "Div1", league_id=league["id"])
        div2 = api.create_division(s2["id"], "Div2", league_id=league["id"])
        club = api.create_club("Club")
        a1 = api.create_team(club_id=club["id"], name="A1",
                             league_id=league["id"])
        a2 = api.create_team(club_id=club["id"], name="A2",
                             league_id=league["id"])
        b1 = api.create_team(club_id=club["id"], name="B1",
                             league_id=league["id"])
        b2 = api.create_team(club_id=club["id"], name="B2",
                             league_id=league["id"])
        reg_a1 = api.register_team_for_season(
            s1["id"], a1["id"], div1["id"], league_id=league["id"])
        reg_a2 = api.register_team_for_season(
            s1["id"], a2["id"], div1["id"], league_id=league["id"])
        reg_b1 = api.register_team_for_season(
            s2["id"], b1["id"], div2["id"], league_id=league["id"])
        reg_b2 = api.register_team_for_season(
            s2["id"], b2["id"], div2["id"], league_id=league["id"])
        for reg in (reg_a1, reg_a2, reg_b1, reg_b2):
            self.assertNotIn("error", reg, reg)
        v1 = api.create_venue("Venue1", league_id=program["id"])
        v2 = api.create_venue("Venue2", league_id=program["id"])
        api.grant_season_venue_access(s1["id"], v1["id"])
        api.grant_season_venue_access(s2["id"], v2["id"])
        r1 = api.create_rink(v1["id"], "Rink1")
        r2 = api.create_rink(v2["id"], "Rink2")
        i1 = api.create_ice_slot(r1["id"], "2026-09-01T18:30:00+00:00",
                                 "2026-09-01T20:00:00+00:00")
        i2 = api.create_ice_slot(r2["id"], "2026-10-01T18:30:00+00:00",
                                 "2026-10-01T20:00:00+00:00")
        g1 = api.create_game(s1["id"], div1["id"], a1["id"], a2["id"],
                             i1["id"], league_id=league["id"])
        g2 = api.create_game(s2["id"], div2["id"], b1["id"], b2["id"],
                             i2["id"], league_id=league["id"])
        self.assertNotIn("error", g1, g1)
        self.assertNotIn("error", g2, g2)
        # Distinguishable results too -- S1's game is recorded+approved, S2's
        # is left untouched, so `result_status` differs per Season.
        rec = api.record_result(g1["id"], 4, 1)
        self.assertNotIn("error", rec, rec)
        appr = api.approve_result(g1["id"])
        self.assertNotIn("error", appr, appr)
        pub1 = api.publish_game(g1["id"])
        pub2 = api.publish_game(g2["id"])
        self.assertNotIn("error", pub1, pub1)
        self.assertNotIn("error", pub2, pub2)
        return {
            "program_id": program["id"], "s1": s1["id"], "s2": s2["id"],
            "league_id": league["id"], "div1": div1["id"], "div2": div2["id"],
            "team_ids": {a1["id"], a2["id"], b1["id"], b2["id"]},
            "s1_team_ids": {a1["id"], a2["id"]},
            "s2_team_ids": {b1["id"], b2["id"]},
            "g1": g1["id"], "g2": g2["id"],
            "v1": v1["id"], "v2": v2["id"], "r1": r1["id"], "r2": r2["id"],
            "i1": i1["id"], "i2": i2["id"],
        }

    def test_s1_active_excludes_s2_even_when_s2_is_archived(self):
        for label, store in _backends():
            with self.subTest(backend=label):
                api = ApiService(store)
                f = self._build(api)
                # Archive S2 -- history, not merely "a different season".
                s2_obj = store.get_season(f["s2"])
                s2_obj.status = SeasonStatus.ARCHIVED
                store.save_season(s2_obj)

                # Both "a League selected" and "No League" must exclude S2's
                # data identically -- the Season ceiling is orthogonal to the
                # League axis.
                for league_choice in (f["league_id"], None):
                    with self.subTest(league=league_choice):
                        sel = api.set_active_context(
                            "u1", *ADMIN, f["program_id"], f["s1"],
                            league_choice)
                        self.assertNotIn("error", sel, sel)
                        view = api.get_demo_overview("u1", *ADMIN)
                        self.assertNotIn("error", view, view)
                        self.assertEqual(_ids(view["divisions"]), {f["div1"]})
                        self.assertEqual(
                            {r["team_id"] for r in view["registrations"]},
                            f["s1_team_ids"])
                        self.assertEqual(
                            {g["game_id"] for g in view["schedule"]},
                            {f["g1"]})
                        self.assertEqual(_ids(view["venues"]), {f["v1"]})
                        self.assertEqual(_ids(view["rinks"]), {f["r1"]})
                        self.assertEqual(_ids(view["ice_slots"]), {f["i1"]})
                        # Teams are Program+League scoped ONLY (confirmed
                        # against the source) -- NOT narrowed by Season, so
                        # both stay visible under either Season selection.
                        self.assertEqual(_ids(view["teams"]), f["team_ids"])
                        # The one visible game carries S1's OWN result.
                        row = next(g for g in view["schedule"]
                                  if g["game_id"] == f["g1"])
                        self.assertEqual(row["result_status"], "final")
                _close(store)

    def test_s2_active_excludes_s1(self):
        for label, store in _backends():
            with self.subTest(backend=label):
                api = ApiService(store)
                f = self._build(api)
                s2_obj = store.get_season(f["s2"])
                s2_obj.status = SeasonStatus.ARCHIVED
                store.save_season(s2_obj)
                # An ARCHIVED Season is honored when EXPLICITLY selected --
                # exactly like the Program/Season axis's own existing
                # read-only-history rule.
                sel = api.set_active_context(
                    "u1", *ADMIN, f["program_id"], f["s2"])
                self.assertNotIn("error", sel, sel)
                view = api.get_demo_overview("u1", *ADMIN)
                self.assertNotIn("error", view, view)
                self.assertEqual(_ids(view["divisions"]), {f["div2"]})
                self.assertEqual(
                    {r["team_id"] for r in view["registrations"]},
                    f["s2_team_ids"])
                self.assertEqual(
                    {g["game_id"] for g in view["schedule"]}, {f["g2"]})
                self.assertEqual(_ids(view["venues"]), {f["v2"]})
                self.assertEqual(_ids(view["rinks"]), {f["r2"]})
                self.assertEqual(_ids(view["ice_slots"]), {f["i2"]})
                self.assertEqual(_ids(view["teams"]), f["team_ids"])
                row = next(g for g in view["schedule"]
                          if g["game_id"] == f["g2"])
                self.assertIsNone(row["result_status"])
                _close(store)

    def test_program_only_context_narrows_season_scoped_collections_to_empty(self):
        for label, store in _backends():
            with self.subTest(backend=label):
                api = ApiService(store)
                f = self._build(api)
                sel = api.set_active_context(
                    "u1", *ADMIN, f["program_id"], None)
                self.assertNotIn("error", sel, sel)
                view = api.get_demo_overview("u1", *ADMIN)
                self.assertNotIn("error", view, view)
                # #369: Program-only narrows every SEASON-scoped collection
                # to EMPTY -- never "every Season" (the rejected behavior).
                self.assertEqual(view["divisions"], [])
                self.assertEqual(view["registrations"], [])
                self.assertEqual(view["schedule"], [])
                self.assertEqual(view["public_fixtures"], [])
                self.assertEqual(view["venues"], [])
                self.assertEqual(view["rinks"], [])
                self.assertEqual(view["ice_slots"], [])
                self.assertEqual(view["seasons"], [])
                # Teams (Program+League scoped only) and Levels/Leagues
                # (Program scoped only) are UNAFFECTED by the missing Season.
                self.assertEqual(_ids(view["teams"]), f["team_ids"])
                self.assertEqual(_ids(view["levels"]), {f["league_id"]})
                _close(store)


class DemoOverviewZeroProgramFailClosedTest(unittest.TestCase):
    """#369 review correction: when ``program`` resolves to ``None``, EVERY
    key -- including Clubs/Organizations/Officials/Venues/Rinks/IceSlots, a
    previous "bootstrap" exception that let a scoped account enumerate
    installation-wide names -- comes back empty, with NO exception. Proven
    for Coach/Official/Guardian identities each with a missing/revoked/
    unassigned scope, with OTHER Programs' real Club/Organization/Official
    data sitting in the very same store the whole time, on Memory/SQLite/
    PostgreSQL per the reviewer's explicit ask."""

    def test_scoped_identities_with_no_authorized_program_get_fully_empty_shape(self):
        for label, store in _backends():
            with self.subTest(backend=label):
                api = ApiService(store)
                real = _build_scheduled_program(api, "Real Prog")
                org = api.create_organization("Real Org", "RO")
                self.assertNotIn("error", org, org)
                lone_official = api.create_official("Lone Ref")
                self.assertNotIn("error", lone_official, lone_official)

                # Coach pointed at a team that does not exist at all.
                coach = (Role.COACH, {"team_id": "team_does_not_exist"})
                self.assertEqual(
                    api.get_demo_overview("u_coach", *coach), _EMPTY_OVERVIEW,
                    label)

                # An Official who is real but has NO assignments anywhere --
                # authorized_program_ids resolves to the empty set.
                unassigned = api.create_official("Unassigned Ref")
                self.assertNotIn("error", unassigned, unassigned)
                official = (Role.OFFICIAL, {"official_id": unassigned["id"]})
                self.assertEqual(
                    api.get_demo_overview("u_official", *official),
                    _EMPTY_OVERVIEW, label)

                # A Guardian with an UNVERIFIED link (never verified, i.e.
                # revoked-in-effect) -- grants nothing per #26/#35. Written
                # directly to the store (mirrors test_active_context.py's
                # own GuardianLink fixture pattern) since the account-
                # creation facade requires a real pre-existing guardian
                # UserAccount that is orthogonal to what this proves.
                team_id = next(iter(real["team_ids"]))
                junior = api.create_player(team_id, "Junior", "forward")
                self.assertNotIn("error", junior, junior)
                with store.transaction():
                    store.add_guardian_link(GuardianLink(
                        id=store.next_id("guardianlink"),
                        guardian_user_id="u_guardian", player_id=junior["id"],
                        created_at=datetime.now(timezone.utc),
                        verified=False))
                guardian = (Role.GUARDIAN, {})
                self.assertEqual(
                    api.get_demo_overview("u_guardian", *guardian),
                    _EMPTY_OVERVIEW, label)
                _close(store)


class DemoOverviewAuthGateTest(unittest.TestCase):
    """``role=None`` (the default) is the legacy unscoped branch; supplying a
    real role activates scoping."""

    def test_role_none_is_unscoped_role_supplied_is_scoped(self):
        api = ApiService(InMemoryStore())
        pa = _build_scheduled_program(api, "Prog A")
        pb = _build_scheduled_program(api, "Prog B")

        unscoped = api.get_demo_overview()  # role omitted entirely
        self.assertNotIn("error", unscoped, unscoped)
        self.assertEqual(_ids(unscoped["leagues"]),
                         {pa["program_id"], pb["program_id"]})

        api.set_active_context(
            "u1", *ADMIN, pa["program_id"], pa["season_id"])
        scoped = api.get_demo_overview("u1", *ADMIN)
        self.assertEqual(_ids(scoped["leagues"]), {pa["program_id"]})

    def test_zero_authorized_programs_is_a_named_empty_state_not_an_error(self):
        """#369 review correction: this used to assert Clubs (and only
        Clubs) still leaked real names as a "bootstrap" exception even with
        zero authorized Programs; review correctly flagged that as a real
        leak. The named empty state is now FULLY empty, no exception."""
        api = ApiService(InMemoryStore())
        _build_scheduled_program(api, "Prog")  # real data exists, unreachable
        # ...but this Coach's team_id resolves to nothing -- zero authorized
        # Programs, per context_scope.authorized_program_ids.
        coach = (Role.COACH, {"team_id": "team_does_not_exist"})
        result = api.get_demo_overview("u1", *coach)
        self.assertEqual(result, _EMPTY_OVERVIEW, result)


class DemoOverviewAuditFilterTest(unittest.TestCase):
    """#369 review correction: ``setup_audit`` is now filtered to the ACTIVE
    Program (Program-LEVEL only -- not further narrowed by Season/League),
    via a real per-entity_type parent-chain resolver in ``get_demo_overview``
    (``_audit_matches_active``). A row for an entity_type with no resolvable
    parent chain at all, or whose entity_id is genuinely dangling, is
    OMITTED entirely -- never guessed, never shown globally. Proven on
    Memory/SQLite/PostgreSQL per the reviewer's explicit ask."""

    def test_audit_scopes_to_active_program_and_omits_unjoinable_rows(self):
        for label, store in _backends():
            with self.subTest(backend=label):
                api = ApiService(store)
                a = _build_scheduled_program(api, "Prog A", actor_id="actor_a")
                b = _build_scheduled_program(api, "Prog B", actor_id="actor_b")

                with store.transaction():
                    # Entity_type with NO resolvable parent chain at all
                    # (#369's own named example).
                    store.add_setup_audit(SetupAuditLog(
                        id=store.next_id("audit"),
                        action="user_account_created",
                        entity_type="user_account", entity_id="ghost_user",
                        at=datetime.now(timezone.utc), actor_id="nobody",
                        detail={"username": "ghost", "role": "coach"}))
                    # A resolvable entity_type whose entity_id is genuinely
                    # dangling -- also must be omitted, not guessed.
                    store.add_setup_audit(SetupAuditLog(
                        id=store.next_id("audit"),
                        action="team_created", entity_type="team",
                        entity_id="team_does_not_exist_anywhere",
                        at=datetime.now(timezone.utc)))

                api.set_active_context(
                    "u1", *ADMIN, a["program_id"], a["season_id"])
                view_a = api.get_demo_overview("u1", *ADMIN)
                self.assertNotIn("error", view_a, view_a)
                a_entity_ids = {e["entity_id"] for e in view_a["setup_audit"]}
                a_types = {e["entity_type"] for e in view_a["setup_audit"]}

                # Program A's own legitimate activity (real mutations
                # _build_scheduled_program performed under it) IS present...
                self.assertIn(a["program_id"], a_entity_ids)
                self.assertIn(a["division_id"], a_entity_ids)
                self.assertTrue(a["team_ids"] & a_entity_ids)
                self.assertIn(a["game_id"], a_entity_ids)
                self.assertIn(a["venue_id"], a_entity_ids)

                # ...Program B's is entirely absent, entity-for-entity...
                self.assertNotIn(b["program_id"], a_entity_ids)
                self.assertNotIn(b["division_id"], a_entity_ids)
                self.assertTrue(b["team_ids"].isdisjoint(a_entity_ids))
                self.assertNotIn(b["game_id"], a_entity_ids)
                self.assertNotIn(b["venue_id"], a_entity_ids)

                # ...and neither unjoinable row leaks in -- the whole row,
                # including any actor_id/detail it might have carried, is
                # omitted rather than partially redacted.
                self.assertNotIn("ghost_user", a_entity_ids)
                self.assertNotIn("user_account", a_types)
                self.assertNotIn("team_does_not_exist_anywhere", a_entity_ids)
                self.assertTrue(
                    all(e.get("actor_id") != "nobody"
                        for e in view_a["setup_audit"]))

                self.assertEqual(
                    view_a["setup_audit_count"], len(view_a["setup_audit"]))

                # And the reverse holds for Program B's own view.
                api.set_active_context(
                    "u1", *ADMIN, b["program_id"], b["season_id"])
                view_b = api.get_demo_overview("u1", *ADMIN)
                b_entity_ids = {e["entity_id"] for e in view_b["setup_audit"]}
                self.assertIn(b["program_id"], b_entity_ids)
                self.assertNotIn(a["program_id"], b_entity_ids)
                self.assertNotIn("ghost_user", b_entity_ids)
                _close(store)


class DemoOverviewRoleMatrixTest(unittest.TestCase):
    """Every operator/subject role -- not just League Admin -- resolves
    through the SAME Program/Season/League scoping this method applies;
    each sees only its own authorized Program's Dashboard data, whatever its
    scope shape (global-empty, team-scoped, live-player-resolved, official-
    assignment-resolved, or verified-guardian-link-resolved)."""

    def _identity_for(self, api, role, program):
        team_id = next(iter(program["team_ids"]))
        if role in (Role.LEAGUE_ADMIN, Role.ARENA_MANAGER, Role.VIEWER):
            return f"u_{role.value}", role, {}
        if role == Role.COACH:
            return "u_coach", role, {"team_id": team_id}
        if role == Role.PLAYER:
            player = api.create_player(team_id, "Pat", "forward")
            self.assertNotIn("error", player, player)
            return "u_player", role, {"player_id": player["id"]}
        if role == Role.OFFICIAL:
            official = api.create_official("Ref")
            self.assertNotIn("error", official, official)
            assignment = api.assign_official(
                program["game_id"], official["id"], "referee")
            self.assertNotIn("error", assignment, assignment)
            return "u_official", role, {"official_id": official["id"]}
        if role == Role.GUARDIAN:
            # Written directly to the store (mirrors test_active_context.py's
            # own GuardianLink fixture pattern), since the account-creation
            # facade requires a real pre-existing guardian UserAccount that
            # is orthogonal to what this proves.
            junior = api.create_player(team_id, "Junior", "forward")
            self.assertNotIn("error", junior, junior)
            store = api.store
            with store.transaction():
                store.add_guardian_link(GuardianLink(
                    id=store.next_id("guardianlink"),
                    guardian_user_id="u_guardian", player_id=junior["id"],
                    created_at=datetime.now(timezone.utc), verified=True))
            return "u_guardian", role, {}
        raise AssertionError(f"unhandled role {role}")

    def test_every_role_sees_only_its_own_program(self):
        api = ApiService(InMemoryStore())
        a = _build_scheduled_program(api, "Prog A")
        b = _build_scheduled_program(api, "Prog B")
        for role in (Role.LEAGUE_ADMIN, Role.ARENA_MANAGER, Role.VIEWER,
                     Role.COACH, Role.PLAYER, Role.OFFICIAL, Role.GUARDIAN):
            with self.subTest(role=role.value):
                user_id, role_, scope = self._identity_for(api, role, a)
                sel = api.set_active_context(
                    user_id, role_, scope, a["program_id"], a["season_id"])
                self.assertNotIn("error", sel, (role, sel))
                view = api.get_demo_overview(user_id, role_, scope)
                self.assertNotIn("error", view, (role, view))
                self.assertEqual(_ids(view["leagues"]), {a["program_id"]},
                                 role)
                self.assertTrue(a["team_ids"] & _ids(view["teams"]), role)
                self.assertTrue(
                    b["team_ids"].isdisjoint(_ids(view["teams"])), role)
                self.assertNotIn(b["program_id"], _ids(view["leagues"]), role)


class DemoOverviewViewerNoMutationTest(unittest.TestCase):
    """Viewer is read-only by role design (#24's Permission model grants it
    no MANAGE_* permission at all); real enforcement of that for mutation
    ENDPOINTS lives at the HTTP/web layer (``web/scope.py``'s operator-
    permission gate), not here. This is a narrow facade-level sanity check:
    calling the read-only ``get_demo_overview`` as a Viewer touches no store
    state at all."""

    def test_viewer_facade_call_never_mutates_the_store(self):
        api = ApiService(InMemoryStore())
        program = _build_scheduled_program(api, "Prog")
        api.set_active_context(
            "u_viewer", Role.VIEWER, {}, program["program_id"],
            program["season_id"])
        store = api.store

        def snapshot():
            return (
                [asdict(x) for x in store.all_programs()],
                [asdict(x) for x in store.all_seasons()],
                [asdict(x) for x in store.all_leagues()],
                [asdict(x) for x in store.all_divisions()],
                [asdict(x) for x in store.all_teams()],
                [asdict(x) for x in store.all_clubs()],
                [asdict(x) for x in store.all_games()],
                [asdict(x) for x in store.all_venues()],
                [asdict(x) for x in store.all_rinks()],
                [asdict(x) for x in store.all_ice_slots()],
                [asdict(x) for x in store.all_setup_audit()],
            )

        before = snapshot()
        result = api.get_demo_overview("u_viewer", Role.VIEWER, {})
        self.assertNotIn("error", result, result)
        after = snapshot()
        self.assertEqual(before, after)


class DemoOverviewNegativeContextStatesTest(unittest.TestCase):
    """A degraded/corrupted saved League selection must never leak data or
    error -- it falls back to the same generic "No League" Program+Season
    view an ordinary explicit-None selection already produces. Mirrors the
    fixture-construction patterns ``test_active_context_league.py`` already
    established for these exact shapes."""

    def test_league_unbound_after_selection_falls_back_without_leaking(self):
        api = ApiService(InMemoryStore())
        a = _build_scheduled_program(api, "Prog A")
        b = _build_scheduled_program(api, "Prog B")  # sibling Program: proves
        # the degraded state below stays inside Program A regardless.

        api.set_active_context(
            "u1", *ADMIN, a["program_id"], a["season_id"], a["league_id"])
        selected = api.get_demo_overview("u1", *ADMIN)
        self.assertEqual(_ids(selected["teams"]), a["team_ids"])

        # Remove the LeagueSeason binding directly (the store-level effect of
        # an authorized unbind, per test_active_context_league.py's own
        # `_unbind` helper) -- the saved League selection is now stale.
        ls = api.store.league_season_for(a["league_id"], a["season_id"])
        with api.store.transaction():
            api.store.delete_league_season(ls.id)

        degraded = api.get_demo_overview("u1", *ADMIN)
        self.assertNotIn("error", degraded, degraded)
        # Program/Season are retained (ignore-don't-rewrite); the League
        # silently drops to the broader "No League" view within the SAME
        # Program -- Program A's own team and game are still visible...
        self.assertEqual(_ids(degraded["leagues"]), {a["program_id"]})
        self.assertEqual(_ids(degraded["teams"]), a["team_ids"])
        self.assertIn(
            a["game_id"], {g["game_id"] for g in degraded["schedule"]})
        # ...and Program B never appears, unbind-degraded or not.
        self.assertNotIn(b["program_id"], _ids(degraded["leagues"]))
        self.assertTrue(b["team_ids"].isdisjoint(_ids(degraded["teams"])))
        self.assertNotIn(
            b["game_id"], {g["game_id"] for g in degraded["schedule"]})

    def test_cross_program_persisted_league_is_ignored_not_leaked(self):
        """A League from a DIFFERENT Program somehow persisted on the active
        context row (only reachable by writing the store directly -- the
        real ``set_active_context`` validates and would refuse this) must
        never leak that other Program's League into the resolved view; it
        is dropped exactly like any other invalid saved League."""
        api = ApiService(InMemoryStore())
        a = _build_scheduled_program(api, "Prog A")
        b = _build_scheduled_program(api, "Prog B")

        with api.store.transaction():
            api.store.set_active_context(ActiveContext(
                id="u2", program_id=a["program_id"], season_id=a["season_id"],
                updated_at=datetime.now(timezone.utc),
                league_id=b["league_id"]))

        result = api.get_demo_overview("u2", *ADMIN)
        self.assertNotIn("error", result, result)
        self.assertEqual(_ids(result["leagues"]), {a["program_id"]})
        # Program A's own data shows (the League silently fell back to
        # None, so this is the "No League" broader view within Program A)...
        self.assertEqual(_ids(result["teams"]), a["team_ids"])
        self.assertEqual(_ids(result["divisions"]), {a["division_id"]})
        self.assertEqual(
            {r["team_id"] for r in result["registrations"]},
            a["reg_team_ids"])
        self.assertEqual(
            {g["game_id"] for g in result["schedule"]}, {a["game_id"]})
        # ...never Program B's, despite the dangling cross-Program league_id.
        self.assertTrue(b["team_ids"].isdisjoint(_ids(result["teams"])))
        self.assertNotIn(b["division_id"], _ids(result["divisions"]))
        self.assertNotIn(
            b["game_id"], {g["game_id"] for g in result["schedule"]})


class DemoOverviewHttpTest(unittest.TestCase):
    """Route/authz contract over real HTTP -- mirrors test_setup_progress.
    py's SetupProgressHttpTest pattern exactly. Proves the actual #367
    security fix (this endpoint was fully unauthenticated before), that a
    real session gets the Program-scoped view matching its own active
    context, genuine per-session isolation between two different signed-in
    users, and (#369) HTTP-level checks for the zero-Program fail-closed and
    audit-filtering corrections."""

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

    def _login(self, username, password="demo"):
        c = self._client()
        self._req(c, "POST", "/api/auth/login",
                  {"username": username, "password": password})
        return c

    def test_requires_signed_in_session(self):
        c = self._client()
        status, _ = self._req(c, "GET", "/api/demo/overview")
        self.assertEqual(
            status, 401,
            "#367's actual fix: this endpoint was fully unauthenticated "
            "before -- any cookieless caller must now be refused")

    def test_signed_in_session_returns_own_program_scoped_view(self):
        api = self.srv.STATE.api
        program = api.create_program("HTTP Prog", "US", "UTC")
        season = api.create_season(program["id"], "Season")
        league = api.create_league(season["id"], "League")
        club = api.create_club("Club")
        team = api.create_team(club_id=club["id"], name="Team",
                               league_id=league["id"])
        self.assertNotIn("error", team, team)

        admin = self._login("admin")
        status, _ = self._req(
            admin, "POST", "/api/context",
            {"program_id": program["id"], "season_id": season["id"]})
        self.assertEqual(status, 200)

        status, resp = self._req(admin, "GET", "/api/demo/overview")
        self.assertEqual(status, 200, resp)
        self.assertEqual([p["id"] for p in resp["leagues"]], [program["id"]])
        self.assertIn(team["id"], {t["id"] for t in resp["teams"]})

    def test_two_sessions_see_only_their_own_program(self):
        """Two DIFFERENT signed-in users (admin and arena), each with their
        OWN active context pointing at a different Program, must each see
        only their own Program's data -- proving genuine per-session (really
        per-user, since the active context is keyed on user_id) isolation
        over real HTTP, not just at the facade layer."""
        api = self.srv.STATE.api
        prog_admin = api.create_program("Admin's Prog", "US", "UTC")
        season_admin = api.create_season(prog_admin["id"], "Season")
        league_admin = api.create_league(season_admin["id"], "League")
        club_admin = api.create_club("Admin Club")
        team_admin = api.create_team(club_id=club_admin["id"],
                                     name="AdminTeam",
                                     league_id=league_admin["id"])
        self.assertNotIn("error", team_admin, team_admin)

        prog_arena = api.create_program("Arena's Prog", "US", "UTC")
        season_arena = api.create_season(prog_arena["id"], "Season")
        league_arena = api.create_league(season_arena["id"], "League")
        club_arena = api.create_club("Arena Club")
        team_arena = api.create_team(club_id=club_arena["id"],
                                     name="ArenaTeam",
                                     league_id=league_arena["id"])
        self.assertNotIn("error", team_arena, team_arena)

        admin = self._login("admin")
        arena = self._login("arena")
        status, _ = self._req(
            admin, "POST", "/api/context",
            {"program_id": prog_admin["id"], "season_id": season_admin["id"]})
        self.assertEqual(status, 200)
        status, _ = self._req(
            arena, "POST", "/api/context",
            {"program_id": prog_arena["id"], "season_id": season_arena["id"]})
        self.assertEqual(status, 200)

        status, admin_view = self._req(admin, "GET", "/api/demo/overview")
        self.assertEqual(status, 200, admin_view)
        status, arena_view = self._req(arena, "GET", "/api/demo/overview")
        self.assertEqual(status, 200, arena_view)

        admin_team_ids = {t["id"] for t in admin_view["teams"]}
        arena_team_ids = {t["id"] for t in arena_view["teams"]}
        self.assertIn(team_admin["id"], admin_team_ids)
        self.assertNotIn(team_arena["id"], admin_team_ids)
        self.assertIn(team_arena["id"], arena_team_ids)
        self.assertNotIn(team_admin["id"], arena_team_ids)
        self.assertEqual([p["id"] for p in admin_view["leagues"]],
                         [prog_admin["id"]])
        self.assertEqual([p["id"] for p in arena_view["leagues"]],
                         [prog_arena["id"]])

    def test_http_zero_authorized_program_returns_fully_empty_shape(self):
        """#369's fail-closed correction, over real HTTP: a signed-in
        Official account with NO assignments anywhere (zero authorized
        Programs) gets 200 + the fully empty shape -- never an
        installation-wide list of Club/Organization/Official names, even
        though OTHER Programs have real data in the same server state."""
        api = self.srv.STATE.api
        _build_scheduled_program(api, "HTTP Real Prog")  # other real data
        official = api.create_official("Lone HTTP Ref")
        self.assertNotIn("error", official, official)
        account = api.create_user_account(
            "lone_official_http", "demo1234", Role.OFFICIAL.value,
            scope={"official_id": official["id"]})
        self.assertNotIn("error", account, account)

        session = self._login("lone_official_http", "demo1234")
        status, resp = self._req(session, "GET", "/api/demo/overview")
        self.assertEqual(status, 200, resp)
        self.assertEqual(resp["clubs"], [])
        self.assertEqual(resp["organizations"], [])
        self.assertEqual(resp["officials"], [])
        self.assertEqual(resp["venues"], [])
        self.assertEqual(resp["teams"], [])
        self.assertEqual(resp["setup_audit"], [])

    def test_http_audit_scopes_to_the_signed_in_sessions_own_program(self):
        """#369's audit-filtering correction, over real HTTP: the signed-in
        session's own Program's setup_audit activity shows; a sibling
        Program's never does."""
        api = self.srv.STATE.api
        mine = _build_scheduled_program(
            api, "HTTP Audit Mine", actor_id="mine_actor")
        other = _build_scheduled_program(
            api, "HTTP Audit Other", actor_id="other_actor")

        admin = self._login("admin")
        status, _ = self._req(
            admin, "POST", "/api/context",
            {"program_id": mine["program_id"], "season_id": mine["season_id"]})
        self.assertEqual(status, 200)

        status, resp = self._req(admin, "GET", "/api/demo/overview")
        self.assertEqual(status, 200, resp)
        entity_ids = {e["entity_id"] for e in resp["setup_audit"]}
        self.assertIn(mine["division_id"], entity_ids)
        self.assertNotIn(other["division_id"], entity_ids)
        self.assertTrue(other["team_ids"].isdisjoint(entity_ids))


if __name__ == "__main__":
    unittest.main()
