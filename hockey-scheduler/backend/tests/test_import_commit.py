"""CSV import teams + players COMMIT (#93).

Step 2 of the Pilot Onboarding Import Wizard: takes the same CSV shapes #92's
dry-run validator accepts, re-validates via the SAME pure ``validate_import``
gate, and — only if that gate is clean — writes teams/players (and any
find-or-created clubs/divisions) inside a single transaction. Officials,
rinks, and ice slots commit are out of scope here (#94/#95).
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
from hockey_scheduler.domain import NotificationChannel
from hockey_scheduler.services import SetupService
from hockey_scheduler.store import InMemoryStore, SqlStore

TEAMS_CSV = (
    "team_code,team_name,club_name,division_name\n"
    "T1,Team One,Lions Club,U16\n"
    "T2,Team Two,Falcons Club,U18\n"
)

PLAYERS_CSV = (
    "player_code,first_name,last_name,team_code,jersey_number,position,email\n"
    "P1,Aarav,M,T1,9,forward,aarav@example.com\n"
    "P2,Kabir,S,T1,10,defense,\n"
    "P3,Sam,G,T2,1,goalie,sam@example.com\n"
)

DEDUP_TEAMS_CSV = (
    "team_code,team_name,club_name,division_name\n"
    "D1,Team D1,Shared Club,U16\n"
    "D2,Team D2,Shared Club,U18\n"
)

DUPLICATE_TEAM_CODE_CSV = (
    "team_code,team_name\n"
    "T1,Team One\n"
    "T1,Team Two\n"
)


def _valid_sheets_csv():
    return {"teams_csv": TEAMS_CSV, "players_csv": PLAYERS_CSV}


class ImportCommitServiceContract:
    """Run against both store backends (mirrors test_import_dry_run.py)."""

    def _store(self):
        raise NotImplementedError

    def setUp(self):
        self.store = self._store()
        self.api = ApiService(self.store)
        self.setup = SetupService(self.store)
        league = self.setup.create_league("Test League", actor_id="admin")
        self.season = self.setup.create_season(
            league.id, "2026 Season", actor_id="admin")

    def _player(self, code):
        return next(p for p in self.store.all_players() if p.external_ref == code)

    def _team(self, code):
        return next(t for t in self.store.all_teams() if t.external_ref == code)

    # -- 1. first commit creates -------------------------------------------
    def test_first_commit_creates_teams_and_players(self):
        res = self.api.commit_teams_players_import(
            self.season.id, _valid_sheets_csv(), actor_id="admin")
        self.assertNotIn("error", res)
        self.assertTrue(res["committed"])
        self.assertEqual(res["summary"]["teams"], {"created": 2, "updated": 0})
        self.assertEqual(res["summary"]["players"], {"created": 3, "updated": 0})
        self.assertEqual(res["summary"]["clubs_created"], 2)
        self.assertEqual(res["summary"]["divisions_created"], 2)

        self.assertEqual(len(self.store.all_teams()), 2)
        self.assertEqual(len(self.store.all_players()), 3)
        self.assertEqual({t.external_ref for t in self.store.all_teams()},
                         {"T1", "T2"})
        self.assertEqual({p.external_ref for p in self.store.all_players()},
                         {"P1", "P2", "P3"})
        # #93 is teams+players only — a normal successful commit must never
        # touch officials/rinks/ice_slots (those land in #94/#95).
        self.assertEqual(self.store.all_officials(), [])
        self.assertEqual(self.store.all_rinks(), [])
        self.assertEqual(self.store.all_ice_slots(), [])

    # -- 2. idempotent repeat -----------------------------------------------
    def test_idempotent_repeat_commit_updates_not_duplicates(self):
        first = self.api.commit_teams_players_import(
            self.season.id, _valid_sheets_csv(), actor_id="admin")
        self.assertTrue(first["committed"])
        teams_after_first = len(self.store.all_teams())
        players_after_first = len(self.store.all_players())

        second = self.api.commit_teams_players_import(
            self.season.id, _valid_sheets_csv(), actor_id="admin")
        self.assertNotIn("error", second)
        self.assertTrue(second["committed"])
        self.assertEqual(second["summary"]["teams"], {"created": 0, "updated": 2})
        self.assertEqual(second["summary"]["players"], {"created": 0, "updated": 3})
        self.assertEqual(len(self.store.all_teams()), teams_after_first)
        self.assertEqual(len(self.store.all_players()), players_after_first)

    # -- 2b. repeat import with a changed name updates in place -------------
    def test_repeat_commit_with_changed_name_updates_existing_record(self):
        self.api.commit_teams_players_import(
            self.season.id, _valid_sheets_csv(), actor_id="admin")
        renamed_teams_csv = TEAMS_CSV.replace("Team One", "Team One Renamed")
        renamed_players_csv = PLAYERS_CSV.replace("Aarav,M", "Aarav,Mehta")
        res = self.api.commit_teams_players_import(
            self.season.id,
            {"teams_csv": renamed_teams_csv, "players_csv": renamed_players_csv},
            actor_id="admin")
        self.assertTrue(res["committed"])
        self.assertEqual(res["summary"]["teams"], {"created": 0, "updated": 2})
        self.assertEqual(len(self.store.all_teams()), 2)  # still no duplicate
        self.assertEqual(self._team("T1").name, "Team One Renamed")
        self.assertEqual(self._player("P1").name, "Aarav Mehta")

    # -- 3. club/division dedup within one commit ---------------------------
    def test_club_dedup_within_one_commit(self):
        res = self.api.commit_teams_players_import(
            self.season.id, {"teams_csv": DEDUP_TEAMS_CSV}, actor_id="admin")
        self.assertNotIn("error", res)
        self.assertTrue(res["committed"])
        self.assertEqual(res["summary"]["clubs_created"], 1)
        self.assertEqual(res["summary"]["divisions_created"], 2)
        self.assertEqual(len(self.store.all_clubs()), 1)

    # -- 4. validation reuse blocks the whole commit -------------------------
    def test_invalid_row_blocks_whole_commit(self):
        res = self.api.commit_teams_players_import(
            self.season.id, {"teams_csv": DUPLICATE_TEAM_CODE_CSV},
            actor_id="admin")
        self.assertNotIn("error", res)
        self.assertFalse(res["committed"])
        self.assertTrue(res["errors"])
        self.assertEqual(self.store.all_teams(), [])
        self.assertEqual(self.store.all_players(), [])

    # -- 5. all-or-nothing across sheets --------------------------------------
    def test_all_or_nothing_across_sheets(self):
        teams_csv = "team_code,team_name\nT1,Team One\n"
        players_csv = (
            "player_code,first_name,last_name,team_code\n"
            "P1,Aarav,M,T9\n"
        )
        res = self.api.commit_teams_players_import(
            self.season.id, {"teams_csv": teams_csv, "players_csv": players_csv},
            actor_id="admin")
        self.assertNotIn("error", res)
        self.assertFalse(res["committed"])
        self.assertEqual(self.store.all_teams(), [])
        self.assertEqual(self.store.all_players(), [])

    # -- 7. audit trail -------------------------------------------------------
    def test_audit_trail_on_first_commit(self):
        res = self.api.commit_teams_players_import(
            self.season.id, _valid_sheets_csv(), actor_id="admin")
        self.assertTrue(res["committed"])
        actions = [a.action for a in self.store.all_setup_audit()]
        self.assertEqual(actions.count("team_created"), 2)
        self.assertEqual(actions.count("player_added"), 3)
        batches = [a for a in self.store.all_setup_audit()
                  if a.action == "import_committed"]
        self.assertEqual(len(batches), 1)
        self.assertEqual(batches[0].entity_type, "import_batch")
        self.assertEqual(batches[0].detail["teams_created"], 2)
        self.assertEqual(batches[0].detail["players_created"], 3)
        self.assertEqual(batches[0].detail["season_id"], self.season.id)

    def test_audit_trail_on_repeat_commit(self):
        self.api.commit_teams_players_import(
            self.season.id, _valid_sheets_csv(), actor_id="admin")
        res = self.api.commit_teams_players_import(
            self.season.id, _valid_sheets_csv(), actor_id="admin")
        self.assertTrue(res["committed"])
        actions = [a.action for a in self.store.all_setup_audit()]
        self.assertEqual(actions.count("team_updated"), 2)
        self.assertEqual(actions.count("player_updated"), 3)
        self.assertEqual(actions.count("import_committed"), 2)

    # -- 8. email contact destination -----------------------------------------
    def test_email_contact_destination(self):
        res = self.api.commit_teams_players_import(
            self.season.id, _valid_sheets_csv(), actor_id="admin")
        self.assertTrue(res["committed"])
        p1 = self._player("P1")
        p2 = self._player("P2")
        dest_p1 = self.store.get_contact_destination(
            f"player:{p1.id}", NotificationChannel.EMAIL)
        self.assertIsNotNone(dest_p1)
        self.assertEqual(dest_p1.destination, "aarav@example.com")
        dest_p2 = self.store.get_contact_destination(
            f"player:{p2.id}", NotificationChannel.EMAIL)
        self.assertIsNone(dest_p2)

    # -- 9. missing/unknown season_id ------------------------------------------
    def test_missing_season_id_is_not_found_no_writes(self):
        res = self.api.commit_teams_players_import(
            "season_does_not_exist", _valid_sheets_csv(), actor_id="admin")
        self.assertIn("error", res)
        self.assertEqual(res["error"]["code"], "not_found")
        self.assertEqual(self.store.all_teams(), [])
        self.assertEqual(self.store.all_players(), [])

    # -- 10. unsupported sheet key ---------------------------------------------
    def test_unsupported_sheet_key_is_validation_error_no_writes(self):
        sheets = dict(_valid_sheets_csv())
        sheets["officials_csv"] = "official_code,name\nO1,Pat Referee\n"
        res = self.api.commit_teams_players_import(
            self.season.id, sheets, actor_id="admin")
        self.assertIn("error", res)
        self.assertEqual(res["error"]["code"], "validation_error")
        self.assertEqual(self.store.all_teams(), [])
        self.assertEqual(self.store.all_players(), [])


class MemoryImportCommitTest(ImportCommitServiceContract, unittest.TestCase):
    def _store(self):
        return InMemoryStore()


class SqlImportCommitTest(ImportCommitServiceContract, unittest.TestCase):
    def _store(self):
        return SqlStore(":memory:")


class TransactionBoundaryTest(unittest.TestCase):
    """SQL-only (#88's own precedent): InMemoryStore's transaction is a no-op
    and can't distinguish "opened once" from "opened never"."""

    def test_commit_opens_exactly_one_transaction_for_multi_team_multi_player(self):
        store = SqlStore(":memory:")
        setup = SetupService(store)
        league = setup.create_league("Test League", actor_id="admin")
        season = setup.create_season(league.id, "2026 Season", actor_id="admin")
        api = ApiService(store)

        calls = {"n": 0}
        real = store.transaction

        def counting():
            calls["n"] += 1
            return real()

        store.transaction = counting  # patch AFTER league/season setup
        res = api.commit_teams_players_import(
            season.id, _valid_sheets_csv(), actor_id="admin")
        self.assertNotIn("error", res)
        self.assertTrue(res["committed"])
        self.assertEqual(calls["n"], 1)


class ImportCommitHttpTest(unittest.TestCase):
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

    def _login(self, opener, username):
        return self._req(opener, "POST", "/api/auth/login",
                         {"username": username, "password": "demo"})

    def _payload(self):
        season_id = self.srv.STATE.ids["season_id"]
        body = dict(_valid_sheets_csv())
        body["season_id"] = season_id
        return body

    def test_league_admin_gets_200(self):
        c = self._client()
        self._login(c, "admin")
        status, body = self._req(c, "POST", "/api/import/commit/teams-players", self._payload())
        self.assertEqual(status, 200)
        self.assertTrue(body["committed"])

    def test_arena_manager_forbidden(self):
        c = self._client()
        self._login(c, "arena")
        status, _ = self._req(c, "POST", "/api/import/commit/teams-players", self._payload())
        self.assertEqual(status, 403)

    def test_coach_forbidden(self):
        c = self._client()
        self._login(c, "coach")
        status, _ = self._req(c, "POST", "/api/import/commit/teams-players", self._payload())
        self.assertEqual(status, 403)

    def test_player_forbidden(self):
        c = self._client()
        self._login(c, "player")
        status, _ = self._req(c, "POST", "/api/import/commit/teams-players", self._payload())
        self.assertEqual(status, 403)

    def test_forged_actor_id_is_ignored_audit_uses_signed_in_admin(self):
        # A signed-in admin sending a forged body actor_id must not be able
        # to forge who the import (and its child rows) is attributed to —
        # same class of issue already fixed for notification preferences,
        # official availability, and calendar feeds.
        admin_uid = self.srv.STATE.api.verify_login("admin", "demo")["id"]
        c = self._client()
        self._login(c, "admin")
        body = self._payload()
        body["actor_id"] = "attacker"
        status, resp = self._req(c, "POST", "/api/import/commit/teams-players", body)
        self.assertEqual(status, 200)
        self.assertTrue(resp["committed"])

        audit = self.srv.STATE.api.store.all_setup_audit()
        batch = [a for a in audit if a.action == "import_committed"][-1]
        self.assertEqual(batch.actor_id, admin_uid)
        self.assertNotEqual(batch.actor_id, "attacker")

        team_row = [a for a in audit
                   if a.action in ("team_created", "team_updated")][-1]
        self.assertEqual(team_row.actor_id, admin_uid)
        self.assertNotEqual(team_row.actor_id, "attacker")


if __name__ == "__main__":
    unittest.main()
