"""Server-derived onboarding status (#174 PR C).

`GET /api/onboarding/status` reports where a fresh client installation stands
on the path to scheduling its first game — computed only from persisted domain
records plus the #143 deployment checks and #173 owner/league/venue rules. It
returns `{complete, ready_to_schedule, steps[], blocking[], warnings[]}`,
surfaces each hard gap as an individually-actionable blocker (suppressing
downstream consequences until their prerequisite exists), keeps players and
officials as non-blocking warnings, and leaks no player names, usernames, or
secrets. The endpoint is League-Admin-only at the HTTP boundary.
"""

import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from helpers import BACKEND  # noqa: F401  (ensures sys.path is set up)

from hockey_scheduler.api import ApiService
from hockey_scheduler.store import InMemoryStore, SqlStore
from hockey_scheduler.web import server as srv


def _codes(status):
    return {b["code"] for b in status["blocking"]}


def _warn_codes(status):
    return {w["code"] for w in status["warnings"]}


class OnboardingStatusServiceTest(unittest.TestCase):
    def setUp(self):
        self.api = ApiService(InMemoryStore())

    def _admin(self):
        # An active League Admin is the first thing every installation needs;
        # create one so the other blockers can be examined in isolation.
        self.api.accounts.create_account("installer", "pw-not-logged",
                                         "league_admin")

    # -- empty installation ------------------------------------------------
    def test_empty_db_blocks_from_the_top(self):
        s = self.api.get_onboarding_status("demo")
        self.assertFalse(s["ready_to_schedule"])
        self.assertFalse(s["complete"])
        codes = _codes(s)
        # Foundational gaps fire at once; downstream ones stay suppressed until
        # their prerequisite exists.
        self.assertIn("no_active_admin", codes)
        self.assertIn("no_organization", codes)
        self.assertIn("no_league", codes)
        self.assertNotIn("no_venue_assigned_to_league", codes)
        self.assertNotIn("no_season", codes)
        self.assertNotIn("no_team", codes)

    def test_admin_clears_admin_blocker(self):
        self._admin()
        s = self.api.get_onboarding_status("demo")
        self.assertNotIn("no_active_admin", _codes(s))

    # -- one blocker per step, revealed in order ---------------------------
    def test_each_step_clears_its_own_blocker_in_order(self):
        self._admin()

        # Only the org/league gaps remain; nothing downstream leaks in yet.
        self.assertEqual(_codes(self.api.get_onboarding_status("demo")),
                         {"no_organization", "no_league"})

        org = self.api.create_organization("Canlon")
        self.assertEqual(_codes(self.api.get_onboarding_status("demo")),
                         {"no_league"})

        league = self.api.create_league("Over 55", organization_id=org["id"])
        # A league unlocks its two dependents: a venue and a season.
        self.assertEqual(_codes(self.api.get_onboarding_status("demo")),
                         {"no_venue_assigned_to_league", "no_season"})

        venue = self.api.create_venue("Plainfield", league_id=league["id"])
        self.assertEqual(_codes(self.api.get_onboarding_status("demo")),
                         {"no_rink", "no_season"})

        rink = self.api.create_rink(venue["id"], "Rink 1")
        self.assertEqual(_codes(self.api.get_onboarding_status("demo")),
                         {"no_available_ice", "no_season"})

        self.api.create_ice_slot(
            rink["id"], "2026-09-01T18:30:00+00:00",
            "2026-09-01T20:00:00+00:00")
        self.assertEqual(_codes(self.api.get_onboarding_status("demo")),
                         {"no_season"})

        season = self.api.create_season(league["id"], "Fall 2026")
        self.assertEqual(_codes(self.api.get_onboarding_status("demo")),
                         {"no_division"})

        div = self.api.create_division(season["id"], "Div A")
        self.assertEqual(_codes(self.api.get_onboarding_status("demo")),
                         {"no_team"})

        club = self.api.create_club("Club X")
        self.api.create_team(club["id"], div["id"], "Team1")
        s = self.api.get_onboarding_status("demo")
        self.assertEqual(_codes(s), set())
        self.assertTrue(s["ready_to_schedule"])

    # -- #173 owner/league integrity ---------------------------------------
    def test_venue_owner_mismatch_is_a_blocker(self):
        self._admin()
        org1 = self.api.create_organization("Owner One")
        org2 = self.api.create_organization("Owner Two")
        league = self.api.create_league("Over 55", organization_id=org1["id"])
        venue = self.api.create_venue("Plainfield", league_id=league["id"])
        # Legacy-style drift: the venue's own owner no longer matches its
        # league's owner. create/assign enforce agreement, so force it directly.
        self.api.store.get_venue(venue["id"]).organization_id = org2["id"]
        s = self.api.get_onboarding_status("demo")
        codes = _codes(s)
        self.assertIn("venue_owner_mismatch", codes)
        # The mismatched venue isn't "soundly assigned", so its rink step is
        # still gated closed rather than falsely reported ready.
        self.assertNotIn("no_rink", codes)

    def test_league_without_organization_is_a_blocker(self):
        self._admin()
        self.api.create_league("Ownerless")  # no organization_id
        s = self.api.get_onboarding_status("demo")
        self.assertIn("league_without_organization", _codes(s))

    def test_dangling_season_parent_is_a_blocker(self):
        self._admin()
        org = self.api.create_organization("Canlon")
        league = self.api.create_league("Over 55", organization_id=org["id"])
        season = self.api.create_season(league["id"], "Fall 2026")
        # Point the season at a league that no longer exists.
        self.api.store.get_season(season["id"]).league_id = "league_ghost"
        s = self.api.get_onboarding_status("demo")
        self.assertIn("seasons_without_league", _codes(s))

    # -- warnings don't block ----------------------------------------------
    def _ready_hierarchy(self):
        self._admin()
        org = self.api.create_organization("Canlon")
        league = self.api.create_league("Over 55", organization_id=org["id"])
        venue = self.api.create_venue("Plainfield", league_id=league["id"])
        rink = self.api.create_rink(venue["id"], "Rink 1")
        self.api.create_ice_slot(
            rink["id"], "2026-09-01T18:30:00+00:00",
            "2026-09-01T20:00:00+00:00")
        season = self.api.create_season(league["id"], "Fall 2026")
        div = self.api.create_division(season["id"], "Div A")
        club = self.api.create_club("Club X")
        team = self.api.create_team(club["id"], div["id"], "Team1")
        return team

    def test_missing_players_and_officials_warn_but_do_not_block(self):
        self._ready_hierarchy()
        s = self.api.get_onboarding_status("demo")
        self.assertTrue(s["ready_to_schedule"])
        self.assertEqual(s["blocking"], [])
        warn = _warn_codes(s)
        self.assertIn("no_players", warn)
        self.assertIn("no_officials", warn)
        # Warnings keep onboarding "not complete" even though scheduling is
        # unblocked.
        self.assertFalse(s["complete"])

    def test_complete_when_fully_populated(self):
        team = self._ready_hierarchy()
        self.api.create_player(team["id"], "Vince Skater", "forward")
        self.api.create_official("Ref Riley")
        s = self.api.get_onboarding_status("demo")
        self.assertTrue(s["ready_to_schedule"])
        self.assertEqual(s["warnings"], [])
        self.assertTrue(s["complete"])

    def test_orphan_player_is_a_warning(self):
        team = self._ready_hierarchy()
        p = self.api.create_player(team["id"], "Vince Skater", "forward")
        self.api.create_official("Ref Riley")
        self.api.store.get_player(p["id"]).team_id = "team_ghost"
        s = self.api.get_onboarding_status("demo")
        self.assertIn("players_without_team", _warn_codes(s))
        self.assertTrue(s["ready_to_schedule"])  # a stray player never blocks

    # -- deployment foundation ---------------------------------------------
    def test_production_requires_durable_store(self):
        self._admin()  # in-memory store
        prod = self.api.get_onboarding_status("production")
        self.assertIn("non_durable_store", _codes(prod))
        # Outside production an in-memory store is legitimate (demo default).
        demo = self.api.get_onboarding_status("demo")
        self.assertNotIn("non_durable_store", _codes(demo))

    def test_durable_sql_store_clears_storage_blocker_in_production(self):
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.addCleanup(os.remove, path)
        api = ApiService(SqlStore(path))
        api.accounts.create_account("installer", "pw-not-logged", "league_admin")
        s = api.get_onboarding_status("production")
        self.assertNotIn("non_durable_store", _codes(s))

    # -- shape + privacy ---------------------------------------------------
    def test_steps_cover_the_whole_path_and_round_trip(self):
        self._admin()
        s = self.api.get_onboarding_status("demo")
        keys = [step["key"] for step in s["steps"]]
        for expected in ("league_admin", "durable_storage", "migrations",
                         "organization", "league", "league_ownership", "venue",
                         "rink", "ice", "season", "division", "team"):
            self.assertIn(expected, keys)
        for step in s["steps"]:
            self.assertIn(step["status"], ("done", "todo"))
        # The admin step is satisfied; org/league are not.
        by_key = {step["key"]: step for step in s["steps"]}
        self.assertEqual(by_key["league_admin"]["status"], "done")
        self.assertEqual(by_key["organization"]["status"], "todo")
        json.dumps(s)  # must serialize with no custom encoder

    def test_no_player_names_usernames_or_secrets_leak(self):
        team = self._ready_hierarchy()
        self.api.create_player(team["id"], "Top Secret Kid", "forward")
        self.api.accounts.create_account("secret_user", "sup3r-secret-pw",
                                         "coach")
        blob = json.dumps(self.api.get_onboarding_status("demo"))
        self.assertNotIn("Top Secret Kid", blob)
        self.assertNotIn("secret_user", blob)
        self.assertNotIn("sup3r-secret-pw", blob)


class OnboardingStatusHttpTest(unittest.TestCase):
    """The route is League-Admin-only (MANAGE_SETUP): non-admins get 403, an
    invalid cookie 401, and a League Admin the full payload."""

    @classmethod
    def setUpClass(cls):
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

    def _get(self, role=None, cookie=None):
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/api/onboarding/status", method="GET")
        if role is not None:
            req.add_header("X-Demo-Role", role)
        if cookie is not None:
            req.add_header("Cookie", cookie)
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def test_league_admin_gets_status(self):
        status, body = self._get(role="league_admin")
        self.assertEqual(status, 200)
        for key in ("complete", "ready_to_schedule", "steps", "blocking",
                    "warnings"):
            self.assertIn(key, body)

    def test_viewer_is_forbidden(self):
        status, body = self._get(role="viewer")
        self.assertEqual(status, 403)
        self.assertEqual(body["error"]["details"]["required"], "manage_setup")

    def test_arena_manager_is_forbidden(self):
        # MANAGE_SETUP is League-Admin-only; an Arena Manager (MANAGE_ARENA /
        # MANAGE_SCHEDULE) may run facility inventory but not read full
        # onboarding data.
        status, body = self._get(role="arena_manager")
        self.assertEqual(status, 403)
        self.assertEqual(body["error"]["details"]["required"], "manage_setup")

    def test_invalid_cookie_is_unauthorized(self):
        status, body = self._get(cookie="hs_sid=bogus-session")
        self.assertEqual(status, 401)
        self.assertEqual(body["error"]["code"], "unauthorized")


if __name__ == "__main__":
    unittest.main()
