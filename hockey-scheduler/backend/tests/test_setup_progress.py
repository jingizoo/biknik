"""Home/Tasks hub setup-progress — Program-scoped six-workflow completion
state (#204/#330).

Unlike the installation-wide ``get_setup_overview_v2`` / ``get_onboarding_
status_v2``, ``get_setup_progress`` resolves the ACTING Program from the
caller's active context (#159) and reports completion for the six Setup
workflows #204 names (league profile/seasons, permanent teams, season
participation/divisions, clubs/players/staff, venues/rinks/ice, imports/
onboarding) — so the Home/Tasks hub can compute "Continue setup" without the
operator inferring gaps from the data model.

Coverage: per-workflow done/todo boundaries mirroring ``get_onboarding_
status_v2``'s own steps (scoped here instead of installation-wide);
next-incomplete ordering as data is added; the always-"optional" (never
done/todo) "imports and onboarding" step; cross-Program isolation (the whole
point of this endpoint vs. its installation-wide siblings); the empty
no-Program state; and the HTTP route/authz contract (401 signed-out, 403
wrong role, 200 for both League Admin and Arena Manager, 405 on the wrong
method).
"""

import json
import threading
import unittest
import urllib.error
import urllib.request
from http.cookiejar import CookieJar
from http.server import ThreadingHTTPServer

from helpers import BACKEND  # noqa: F401  (ensures sys.path is set up)

from hockey_scheduler.api import ApiService
from hockey_scheduler.domain import Role
from hockey_scheduler.store import InMemoryStore

ADMIN = (Role.LEAGUE_ADMIN, {})
ARENA = (Role.ARENA_MANAGER, {})
_WORKFLOW_KEYS = ["league_season", "teams", "participation", "roster",
                  "facilities", "import"]


def _statuses(progress):
    return {w["key"]: w["status"] for w in progress["workflows"]}


class SetupProgressComputationTest(unittest.TestCase):
    """Facade-level, Memory-backed: the workflow-completion logic itself has
    no concurrency angle (a pure read composed from store methods that each
    already carry their own Memory/SQLite/PostgreSQL parity coverage), so one
    backend is sufficient here — the same scope test_v2_onboarding_status.py
    takes for the sibling logic this mirrors."""

    def _api(self):
        return ApiService(InMemoryStore())

    def test_no_program_is_a_named_empty_state_not_an_error(self):
        api = self._api()
        progress = api.get_setup_progress("u1", *ADMIN)
        self.assertNotIn("error", progress, progress)
        self.assertIsNone(progress["program_id"])
        self.assertEqual(progress["workflows"], [])
        self.assertIsNone(progress["next"])
        self.assertFalse(progress["complete"])

    def test_fresh_program_lists_all_six_workflows_todo_league_first(self):
        api = self._api()
        api.create_user_account("admin", "pw", "league_admin")
        program = api.create_program("Prog", actor_id="admin")
        progress = api.get_setup_progress("admin", *ADMIN)
        self.assertEqual(progress["program_id"], program["id"])
        self.assertEqual(progress["program"]["name"], "Prog")
        self.assertEqual([w["key"] for w in progress["workflows"]],
                         _WORKFLOW_KEYS)
        statuses = _statuses(progress)
        # "import" ("Imports and onboarding") is "optional" from the very
        # start, never "todo" — see test_import_workflow_is_always_optional_
        # never_next_or_complete_blocking for why it carries no done/todo
        # signal of its own (#330 review round 1 finding 5).
        self.assertTrue(all(statuses[k] == "todo" for k in _WORKFLOW_KEYS
                            if k != "import"))
        self.assertEqual(statuses["import"], "optional")
        self.assertEqual(progress["next"]["key"], "league_season")
        self.assertFalse(progress["complete"])

    def test_workflows_flip_done_in_order_as_data_is_added(self):
        api = self._api()
        api.create_user_account("admin", "pw", "league_admin")
        program = api.create_program("Prog", actor_id="admin")
        pid = program["id"]

        def next_key():
            return api.get_setup_progress("admin", *ADMIN)["next"]["key"]

        self.assertEqual(next_key(), "league_season")

        season = api.create_season(pid, "Fall", actor_id="admin")
        league = api.create_league(season["id"], "Adult League", actor_id="admin")
        self.assertEqual(next_key(), "teams")

        club = api.create_club("C", actor_id="admin")
        team = api.create_team(club["id"], None, "T", actor_id="admin",
                               program_id=pid)
        self.assertEqual(next_key(), "participation")

        reg = api.register_team_for_season(season["id"], team["id"],
                                           actor_id="admin",
                                           league_id=league["id"])
        self.assertNotIn("error", reg, reg)
        self.assertEqual(next_key(), "roster")

        api.create_player(team["id"], "Vince Skater", "forward",
                          actor_id="admin")
        self.assertEqual(next_key(), "facilities")

        venue = api.create_venue("V", league_id=pid, actor_id="admin")
        rink = api.create_rink(venue["id"], "R", actor_id="admin")
        api.create_ice_slot(rink["id"], "2026-09-01T18:30:00+00:00",
                            "2026-09-01T20:00:00+00:00", actor_id="admin")
        api.grant_season_venue_access(season["id"], venue["id"],
                                      actor_id="admin")

        final = api.get_setup_progress("admin", *ADMIN)
        self.assertIsNone(final["next"], final)
        self.assertTrue(final["complete"], final)
        # "Imports and onboarding" reports "optional", not "done" — it has
        # no real completion signal of its own, so completing 1-5 must never
        # flip it (see test_import_workflow_is_always_optional_never_next_
        # or_complete_blocking) — but it must still be LISTED so it stays
        # reachable as its own hub entry point regardless (#330).
        self.assertEqual(_statuses(final)["import"], "optional")
        self.assertEqual([w["key"] for w in final["workflows"]],
                         _WORKFLOW_KEYS)

    def test_import_workflow_is_always_optional_never_next_or_complete_blocking(self):
        """#330 review round 1 finding 5: the prior shape derived "imports
        and onboarding"'s done/todo state from whether workflows 1-5 all
        happened to be done — an invented rule with no grounding (two of the
        three import-commit paths write no season/program-derivable field
        into their own audit summary row, so there is no real Program-scoped
        "has an import run here" signal to compute a state from), and as a
        side effect meant this workflow could never itself be offered as
        `next`. It now reports a third status, "optional", from the very
        first call on a brand new Program through to a fully complete one —
        never "todo" (so it can never become `next`, regardless of a role
        holding the MANAGE_SETUP permission mapped to it in
        ``_WORKFLOW_PERMISSION``) and never "done" (so it can never silently
        satisfy `complete` on its own, and completing 1-5 must never flip
        it)."""
        api = self._api()
        api.create_user_account("admin", "pw", "league_admin")
        program = api.create_program("Prog", actor_id="admin")
        pid = program["id"]

        fresh = api.get_setup_progress("admin", *ADMIN)
        self.assertEqual(_statuses(fresh)["import"], "optional")
        self.assertEqual(fresh["next"]["key"], "league_season",
                         "import must never be offered as next, even on a "
                         "fresh Program with nothing else done yet")

        season = api.create_season(pid, "Fall", actor_id="admin")
        league = api.create_league(season["id"], "Adult League",
                                   actor_id="admin")
        club = api.create_club("C", actor_id="admin")
        team = api.create_team(club["id"], None, "T", actor_id="admin",
                               program_id=pid)
        reg = api.register_team_for_season(season["id"], team["id"],
                                           actor_id="admin",
                                           league_id=league["id"])
        self.assertNotIn("error", reg, reg)
        api.create_player(team["id"], "Vince Skater", "forward",
                          actor_id="admin")
        venue = api.create_venue("V", league_id=pid, actor_id="admin")
        rink = api.create_rink(venue["id"], "R", actor_id="admin")
        api.create_ice_slot(rink["id"], "2026-09-01T18:30:00+00:00",
                            "2026-09-01T20:00:00+00:00", actor_id="admin")
        api.grant_season_venue_access(season["id"], venue["id"],
                                      actor_id="admin")

        final = api.get_setup_progress("admin", *ADMIN)
        self.assertTrue(final["complete"], final)
        self.assertIsNone(final["next"], final)
        self.assertEqual(_statuses(final)["import"], "optional",
                         "completing 1-5 must never flip import to done")

    def test_league_required_per_season_even_with_another_season_ok(self):
        """Mirrors get_onboarding_status_v2's own per-Season League rule: a
        SECOND Season with no grouping League keeps "league_season" todo even
        though the first Season has one."""
        api = self._api()
        api.create_user_account("admin", "pw", "league_admin")
        program = api.create_program("Prog", actor_id="admin")
        season1 = api.create_season(program["id"], "Fall", actor_id="admin")
        api.create_league(season1["id"], "Adult League", actor_id="admin")
        api.create_season(program["id"], "Spring", actor_id="admin")  # no league

        progress = api.get_setup_progress("admin", *ADMIN)
        self.assertEqual(_statuses(progress)["league_season"], "todo")
        self.assertEqual(progress["next"]["key"], "league_season")

    def test_cross_program_isolation(self):
        """The whole point of this endpoint vs. its installation-wide
        siblings: Program B's team must never make Program A's "teams"
        workflow read done."""
        api = self._api()
        api.create_user_account("admin", "pw", "league_admin")
        program_a = api.create_program("Prog A", actor_id="admin")
        program_b = api.create_program("Prog B", actor_id="admin")
        season_b = api.create_season(program_b["id"], "Fall", actor_id="admin")
        api.create_league(season_b["id"], "B League", actor_id="admin")
        club = api.create_club("C", actor_id="admin")
        team_b = api.create_team(club["id"], None, "T-B", actor_id="admin",
                                 program_id=program_b["id"])
        self.assertNotIn("error", team_b, team_b)

        # Force resolution onto Program A specifically via an explicit
        # active-context selection (#159) rather than relying on fallback
        # ordering between two equally-authorized Programs.
        api.set_active_context("admin", Role.LEAGUE_ADMIN, {},
                               program_a["id"], None)
        progress_a = api.get_setup_progress("admin", *ADMIN)
        self.assertEqual(progress_a["program_id"], program_a["id"])
        self.assertEqual(_statuses(progress_a)["teams"], "todo")

        api.set_active_context("admin", Role.LEAGUE_ADMIN, {},
                               program_b["id"], None)
        progress_b = api.get_setup_progress("admin", *ADMIN)
        self.assertEqual(progress_b["program_id"], program_b["id"])
        self.assertEqual(_statuses(progress_b)["teams"], "done")

    def test_next_action_is_role_aware(self):
        """#330 review round 1 finding 1: an Arena Manager (MANAGE_ARENA
        only, no MANAGE_SETUP) must never be handed a MANAGE_SETUP-only
        action like "Add Season" — on a fresh Program they're routed
        straight to facilities, the one workflow their role can actually
        execute; League Admin's own ordering is unaffected. The GLOBAL
        workflow list (informational) stays identical across roles — only
        the primary-action recommendation differs."""
        api = self._api()
        api.create_user_account("admin", "pw", "league_admin")
        api.create_program("Prog", actor_id="admin")

        admin_progress = api.get_setup_progress("admin", *ADMIN)
        self.assertEqual(admin_progress["next"]["key"], "league_season")
        self.assertEqual(admin_progress["next"]["primary_action"], "Add Season")

        arena_progress = api.get_setup_progress("admin", *ARENA)
        self.assertEqual(arena_progress["next"]["key"], "facilities",
                         f"Arena Manager must get an executable action, "
                         f"got {arena_progress['next']}")
        self.assertEqual(arena_progress["next"]["primary_action"], "Add Ice")
        self.assertEqual(_statuses(arena_progress), _statuses(admin_progress))

    def test_next_action_is_none_but_not_complete_when_nothing_actionable_for_role(self):
        """Once facilities (the only Arena-Manager-actionable workflow) is
        done but League-Admin-only workflows remain, Arena Manager's `next`
        must go None WITHOUT `complete` becoming true — "nothing more for
        you" is a different claim from "the Program's setup is finished"."""
        api = self._api()
        api.create_user_account("admin", "pw", "league_admin")
        program = api.create_program("Prog", actor_id="admin")
        season = api.create_season(program["id"], "Fall", actor_id="admin")
        venue = api.create_venue("V", league_id=program["id"], actor_id="admin")
        rink = api.create_rink(venue["id"], "R", actor_id="admin")
        api.create_ice_slot(rink["id"], "2026-09-01T18:30:00+00:00",
                            "2026-09-01T20:00:00+00:00", actor_id="admin")
        api.grant_season_venue_access(season["id"], venue["id"], actor_id="admin")
        # No league/team/registration/player yet — all League-Admin-only.

        arena_progress = api.get_setup_progress("admin", *ARENA)
        self.assertEqual(_statuses(arena_progress)["facilities"], "done")
        self.assertIsNone(arena_progress["next"], arena_progress)
        self.assertFalse(arena_progress["complete"])

        admin_progress = api.get_setup_progress("admin", *ADMIN)
        self.assertEqual(admin_progress["next"]["key"], "league_season")

    def test_participation_and_facilities_scope_to_selected_season_only(self):
        """#330 review round 1 finding 2: an OLDER Season's registrations/
        venue-access/ice must never make participation/facilities read done
        for a DIFFERENT, newly-selected Season that has none of its own."""
        api = self._api()
        api.create_user_account("admin", "pw", "league_admin")
        program = api.create_program("Prog", actor_id="admin")

        # Old season: fully set up (league, team, registration, venue+ice).
        old_season = api.create_season(program["id"], "Old", actor_id="admin")
        old_league = api.create_league(old_season["id"], "Old League",
                                       actor_id="admin")
        club = api.create_club("C", actor_id="admin")
        team = api.create_team(club["id"], None, "T", actor_id="admin",
                               program_id=program["id"])
        reg = api.register_team_for_season(old_season["id"], team["id"],
                                           actor_id="admin",
                                           league_id=old_league["id"])
        self.assertNotIn("error", reg, reg)
        old_venue = api.create_venue("OldV", league_id=program["id"],
                                     actor_id="admin")
        old_rink = api.create_rink(old_venue["id"], "OldR", actor_id="admin")
        api.create_ice_slot(old_rink["id"], "2026-09-01T18:30:00+00:00",
                            "2026-09-01T20:00:00+00:00", actor_id="admin")
        api.grant_season_venue_access(old_season["id"], old_venue["id"],
                                      actor_id="admin")

        # New season: has a league (so league_season stays satisfied
        # Program-wide) but NOTHING of its own for participation/facilities.
        new_season = api.create_season(program["id"], "New", actor_id="admin")
        api.create_league(new_season["id"], "New League", actor_id="admin")

        # Select the NEW season explicitly.
        api.set_active_context("admin", Role.LEAGUE_ADMIN, {},
                               program["id"], new_season["id"])
        progress = api.get_setup_progress("admin", *ADMIN)
        self.assertEqual(progress["program_id"], program["id"])
        statuses = _statuses(progress)
        self.assertEqual(
            statuses["participation"], "todo",
            "the OLD season's registration must not satisfy the NEW season")
        self.assertEqual(
            statuses["facilities"], "todo",
            "the OLD season's ice/venue-access must not satisfy the NEW season")
        self.assertEqual(
            statuses["league_season"], "done",
            "both seasons DO have a league — this workflow stays Program-wide")

        # Selecting the OLD season again correctly reads it as fully done.
        api.set_active_context("admin", Role.LEAGUE_ADMIN, {},
                               program["id"], old_season["id"])
        progress_old = api.get_setup_progress("admin", *ADMIN)
        statuses_old = _statuses(progress_old)
        self.assertEqual(statuses_old["participation"], "done")
        self.assertEqual(statuses_old["facilities"], "done")


class SetupProgressHttpTest(unittest.TestCase):
    """Route/authz contract over real HTTP — mirrors test_v2_setup_contract.
    py's harness. The demo seed (STATE.reset()) provisions the standard
    admin/arena/coach accounts this reuses."""

    @classmethod
    def setUpClass(cls):
        srv = __import__("hockey_scheduler.web.server", fromlist=["x"])
        cls.srv = srv
        srv.STATE.reset()
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), srv.Handler)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
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
        status, _ = self._req(c, "GET", "/api/v2/setup/progress")
        self.assertIn(status, (401, 403))

    def test_denies_role_without_manage_arena(self):
        c = self._login("coach")
        status, _ = self._req(c, "GET", "/api/v2/setup/progress")
        self.assertEqual(status, 403)

    def test_allows_league_admin(self):
        c = self._login("admin")
        status, resp = self._req(c, "GET", "/api/v2/setup/progress")
        self.assertEqual(status, 200, resp)
        self.assertIn("workflows", resp)
        self.assertIn("program_id", resp)

    def test_allows_arena_manager(self):
        # #330: the Home/Tasks hub is also the Arena Manager's landing, so
        # this must be MANAGE_ARENA like /api/v2/setup/overview, not the
        # League-Admin-only MANAGE_SETUP /api/v2/onboarding/status uses.
        c = self._login("arena")
        status, resp = self._req(c, "GET", "/api/v2/setup/progress")
        self.assertEqual(status, 200, resp)
        self.assertIn("workflows", resp)

    def test_wrong_method_is_405_not_500(self):
        c = self._login("admin")
        status, resp = self._req(c, "POST", "/api/v2/setup/progress", {})
        self.assertEqual(status, 405, resp)

    def test_next_action_is_role_aware_over_real_http(self):
        """#330 review round 1 finding 1, over the real route: League Admin
        and Arena Manager viewing the SAME fresh Program must get DIFFERENT
        primary actions — League Admin the normal ordering, Arena Manager an
        executable one (facilities/Add Ice), never a MANAGE_SETUP-only
        action they cannot perform. Coach stays denied entirely (unchanged)."""
        admin = self._login("admin")
        status, program = self._req(admin, "POST", "/api/v2/setup/program",
                                    {"name": "Round1F1 HTTP Prog", "country": "US"})
        self.assertEqual(status, 200, program)
        status, _ = self._req(admin, "POST", "/api/context",
                              {"program_id": program["id"], "season_id": None})
        self.assertEqual(status, 200)

        status, admin_progress = self._req(admin, "GET", "/api/v2/setup/progress")
        self.assertEqual(status, 200, admin_progress)
        self.assertEqual(admin_progress["next"]["key"], "league_season")

        arena = self._login("arena")
        status, _ = self._req(arena, "POST", "/api/context",
                              {"program_id": program["id"], "season_id": None})
        self.assertEqual(status, 200,
                         "Arena Manager must be able to select the same Program")
        status, arena_progress = self._req(arena, "GET", "/api/v2/setup/progress")
        self.assertEqual(status, 200, arena_progress)
        self.assertEqual(
            arena_progress["next"]["key"], "facilities",
            f"Arena Manager must get an executable action, not the League "
            f"Admin-only one: {arena_progress}")
        self.assertEqual(arena_progress["next"]["primary_action"], "Add Ice")

        coach = self._login("coach")
        status, _ = self._req(coach, "GET", "/api/v2/setup/progress")
        self.assertEqual(status, 403)


if __name__ == "__main__":
    unittest.main()
