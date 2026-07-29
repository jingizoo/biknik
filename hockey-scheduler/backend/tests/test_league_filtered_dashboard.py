"""Regression tests for ``get_demo_overview``'s NEW Program/Season/League
scoping (#367) -- the Home/Arena/Schedule/Public data source behind the
Dashboard, and issue #367's own named required-consistent surface ("Dashboard
counts and standings snapshot").

Before #367, ``get_demo_overview`` was a single global, fully unauthenticated
view of the ENTIRE installation -- every Program's teams/divisions/games in
one response, reachable with no session at all. The endpoint now optionally
accepts ``(user_id, role, scope)``; when a real ``role`` is supplied (the
``/api/demo/overview`` HTTP route always supplies one now, and requires a
real session to do it), every collection with a real Program/Season/League
join is scoped to the caller's resolved active context (``ContextService.
resolve_with_league``): mandatory Program, narrowed to the selected Season
(else the union of every Season the Program has) and to the selected League
(else every League's data -- the "No League" broader view). Venues/Rinks/Ice
slots have no competition-League axis at all and scope by Program+Season
(via active ``SeasonVenueAccess``) only. Called with no arguments at all
(``role`` left ``None``, the default), the endpoint is exactly the pre-#367
full, unfiltered installation view -- dozens of existing direct/internal
call sites across the suite rely on this default staying unchanged.

Coverage:
  * the no-args legacy call performs no scoping at all (sanity);
  * two Programs, each with a full vertical slice (League/Division/Teams/
    Registrations/Game), never leak into each other's resolved view --
    the primary "zero cross-Program leakage" proof for this method;
  * two Leagues in the SAME Program+Season narrow teams/divisions/
    registrations/schedule independently; "No League" shows the union; a
    league-less (exhibition) Game is universally eligible regardless of
    which League is selected;
  * Venues/Rinks/IceSlots scope by Program+SeasonVenueAccess only (no
    League axis at all), including the Program-only "union of every Season"
    broader view;
  * ``role=None`` vs a real role at the facade level, and the named empty
    state when the resolved role/scope has NO authorized Program at all;
  * a degraded/corrupted League selection (unbound after being selected, or
    a League from a different Program somehow persisted on the context row)
    never leaks data -- it falls back to the same generic "No League" view
    an ordinary explicit-None selection already produces;
  * the real HTTP route: 401 signed-out (previously fully unauthenticated),
    200 Program-scoped for a real session, and genuine per-session isolation
    between two different signed-in users.
"""

import json
import threading
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timezone
from http.cookiejar import CookieJar
from http.server import ThreadingHTTPServer

from helpers import BACKEND  # noqa: F401  (ensures sys.path is set up)

from hockey_scheduler.api import ApiService
from hockey_scheduler.domain import Role
from hockey_scheduler.domain.setup_models import ActiveContext
from hockey_scheduler.store import InMemoryStore

ADMIN = (Role.LEAGUE_ADMIN, {})

# The exact empty shape ``get_demo_overview`` returns when a real role/scope
# resolves to NO authorized Program at all -- every collection key present
# and empty, never an error (mirrored verbatim from api/service.py).
_EMPTY_OVERVIEW = {
    "league": None, "leagues": [], "seasons": [], "levels": [],
    "divisions": [], "clubs": [], "teams": [],
    "organizations": [], "venues": [], "rinks": [],
    "ice_slots": [], "officials": [], "schedule": [],
    "public_fixtures": [], "registrations": [],
    "setup_audit": [], "setup_audit_count": 0,
}


def _ids(rows, key="id"):
    return {r[key] for r in rows}


def _build_scheduled_program(api, pname, actor_id="admin"):
    """A Program with its own Season/League/Division, two Teams registered
    into it, and one schedulable Game -- a minimal but COMPLETE vertical
    slice so every collection ``get_demo_overview`` scopes (leagues, seasons,
    teams, divisions, registrations, schedule) has real data of its own to
    isolate against a sibling Program's."""
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
    scope by Program only, via active SeasonVenueAccess to ANY of the
    Program's Seasons. Deliberately NEVER narrowed to a single resolved
    Season (unlike every other collection get_demo_overview scopes): a real
    CI regression proved a Program with several Seasons, only one of which
    holds venue access, must still show that inventory even when a
    different sibling Season is the one currently resolved."""

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

    def test_facility_inventory_is_program_wide_regardless_of_season_selection(self):
        """#367 review correction: a real CI regression (an existing,
        unrelated browser journey building a Program with two Seasons where
        only ONE holds venue access) proved facility inventory must NOT
        narrow to whichever single Season happens to be resolved for other
        purposes — physical resources are a Program-wide operational
        concern, exactly like get_setup_overview_v2's own treatment of the
        same three collections. Program-only (no Season chosen) and a
        specific Season selected must show the IDENTICAL union either way;
        only switching to a DIFFERENT Program narrows anything (covered by
        test_two_programs_each_see_only_their_own_facility_inventory)."""
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

        # Program-only (no Season chosen): the union across both Seasons.
        api.set_active_context("u1", *ADMIN, program["id"], None)
        view = api.get_demo_overview("u1", *ADMIN)
        self.assertEqual(_ids(view["venues"]), {v1["id"], v2["id"]})
        self.assertEqual(_ids(view["rinks"]), {r1["id"], r2["id"]})
        self.assertEqual(_ids(view["ice_slots"]), {i1["id"], i2["id"]})

        # Selecting Season 1 specifically must NOT narrow facility inventory
        # -- Season 2's venue/rink/ice (granted to a DIFFERENT Season in the
        # SAME Program) still shows, exactly like the Program-only view.
        api.set_active_context("u1", *ADMIN, program["id"], s1["id"])
        with_season = api.get_demo_overview("u1", *ADMIN)
        self.assertEqual(_ids(with_season["venues"]), {v1["id"], v2["id"]})
        self.assertEqual(_ids(with_season["rinks"]), {r1["id"], r2["id"]})
        self.assertEqual(_ids(with_season["ice_slots"]), {i1["id"], i2["id"]})


class DemoOverviewAuthGateTest(unittest.TestCase):
    """``role=None`` (the default) is the legacy unscoped branch; supplying a
    real role activates scoping. A role/scope that resolves to NO authorized
    Program at all (a Coach pointed at a nonexistent Team) is a named empty
    state, not an error."""

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
        api = ApiService(InMemoryStore())
        _build_scheduled_program(api, "Prog")  # data exists, including a Club...
        # ...but this Coach's team_id resolves to nothing -- zero authorized
        # Programs, per context_scope.authorized_program_ids.
        coach = (Role.COACH, {"team_id": "team_does_not_exist"})
        result = api.get_demo_overview("u1", *coach)
        # Every Program-dependent collection is empty...
        program_dependent = dict(_EMPTY_OVERVIEW)
        program_dependent.pop("clubs")
        for key, expected in program_dependent.items():
            self.assertEqual(result[key], expected, (key, result))
        # ...but Clubs (#367 review correction: no Program dependency in the
        # domain model at all, same as Organizations/Officials) still shows
        # real data -- a role authorized for zero Programs must still be
        # able to see/create this Program-independent reference data.
        self.assertEqual([c["name"] for c in result["clubs"]], ["Prog Club"])


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
    context, and genuine per-session isolation between two different
    signed-in users."""

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

    def _login(self, username):
        c = self._client()
        self._req(c, "POST", "/api/auth/login",
                  {"username": username, "password": "demo"})
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


if __name__ == "__main__":
    unittest.main()
