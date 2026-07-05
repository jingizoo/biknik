"""CSV import officials + official_availability COMMIT (#94).

Step 3 of the Pilot Onboarding Import Wizard: takes officials.csv and
official_availability.csv, re-validates via #92's existing officials-sheet
checks (unchanged) plus the NEW sibling ``validate_official_availability``,
and — only if that combined gate is clean — writes officials (and any
find-or-created clubs) plus their availability windows inside a single
transaction. Teams/players (#93) and rinks/ice_slots (#95) are out of scope
here.
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

OFFICIALS_CSV = (
    "official_code,name,email,home_club_name\n"
    "O1,Pat Referee,pat@example.com,Lions Club\n"
    "O2,Sam Linesperson,,Lions Club\n"
)

AVAILABILITY_CSV = (
    "official_code,start_time,end_time,status,note\n"
    "O1,2026-08-01T18:00:00+00:00,2026-08-01T20:00:00+00:00,available,\n"
    "O2,2026-08-01T18:00:00+00:00,2026-08-01T20:00:00+00:00,unavailable,Vacation\n"
)

DUPLICATE_OFFICIAL_CODE_CSV = (
    "official_code,name\n"
    "O1,Pat Referee\n"
    "O1,Pat Two\n"
)

UNKNOWN_OFFICIAL_CODE_AVAIL_CSV = (
    "official_code,start_time,end_time,status\n"
    "O9,2026-08-01T18:00:00+00:00,2026-08-01T20:00:00+00:00,available\n"
)

OVERLAP_AVAIL_CSV = (
    "official_code,start_time,end_time,status\n"
    "O1,2026-08-01T18:00:00+00:00,2026-08-01T20:00:00+00:00,available\n"
    "O1,2026-08-01T19:00:00+00:00,2026-08-01T21:00:00+00:00,available\n"
)


def _valid_sheets_csv():
    return {"officials_csv": OFFICIALS_CSV, "official_availability_csv": AVAILABILITY_CSV}


class ImportOfficialsAvailabilityCommitServiceContract:
    """Run against both store backends (mirrors test_import_commit.py)."""

    def _store(self):
        raise NotImplementedError

    def setUp(self):
        self.store = self._store()
        self.api = ApiService(self.store)
        self.setup = SetupService(self.store)

    def _official(self, code):
        return next(o for o in self.store.all_officials() if o.external_ref == code)

    # -- 1. first commit creates -------------------------------------------
    def test_first_commit_creates_officials_and_availability(self):
        res = self.api.commit_officials_availability_import(
            _valid_sheets_csv(), actor_id="admin")
        self.assertNotIn("error", res)
        self.assertTrue(res["committed"])
        self.assertEqual(res["summary"]["officials"], {"created": 2, "updated": 0})
        self.assertEqual(res["summary"]["official_availability"],
                         {"created": 2, "updated": 0})
        self.assertEqual(res["summary"]["clubs_created"], 1)

        self.assertEqual(len(self.store.all_officials()), 2)
        self.assertEqual({o.external_ref for o in self.store.all_officials()},
                         {"O1", "O2"})
        # #94 is officials+availability only — a normal successful commit
        # must never touch teams/players/rinks/ice_slots.
        self.assertEqual(self.store.all_teams(), [])
        self.assertEqual(self.store.all_players(), [])
        self.assertEqual(self.store.all_rinks(), [])
        self.assertEqual(self.store.all_ice_slots(), [])

    # -- 2. idempotent repeat -----------------------------------------------
    def test_idempotent_repeat_commit_updates_not_duplicates(self):
        first = self.api.commit_officials_availability_import(
            _valid_sheets_csv(), actor_id="admin")
        self.assertTrue(first["committed"])
        officials_after_first = len(self.store.all_officials())
        avail_after_first = len(self.store.availability_for_official(
            self._official("O1").id)) + len(self.store.availability_for_official(
            self._official("O2").id))

        second = self.api.commit_officials_availability_import(
            _valid_sheets_csv(), actor_id="admin")
        self.assertNotIn("error", second)
        self.assertTrue(second["committed"])
        self.assertEqual(second["summary"]["officials"], {"created": 0, "updated": 2})
        self.assertEqual(second["summary"]["official_availability"],
                         {"created": 0, "updated": 2})
        self.assertEqual(len(self.store.all_officials()), officials_after_first)
        after = len(self.store.availability_for_official(
            self._official("O1").id)) + len(self.store.availability_for_official(
            self._official("O2").id))
        self.assertEqual(after, avail_after_first)

    # -- 3. repeat with changed name/status updates in place -----------------
    def test_repeat_commit_with_changes_updates_existing_records(self):
        self.api.commit_officials_availability_import(
            _valid_sheets_csv(), actor_id="admin")
        renamed_officials_csv = OFFICIALS_CSV.replace(
            "Pat Referee", "Pat Referee Renamed")
        changed_avail_csv = AVAILABILITY_CSV.replace(
            "O1,2026-08-01T18:00:00+00:00,2026-08-01T20:00:00+00:00,available,",
            "O1,2026-08-01T18:00:00+00:00,2026-08-01T20:00:00+00:00,unavailable,Injured")
        res = self.api.commit_officials_availability_import(
            {"officials_csv": renamed_officials_csv,
             "official_availability_csv": changed_avail_csv}, actor_id="admin")
        self.assertTrue(res["committed"])
        self.assertEqual(res["summary"]["officials"], {"created": 0, "updated": 2})
        self.assertEqual(len(self.store.all_officials()), 2)  # no duplicate
        self.assertEqual(self._official("O1").name, "Pat Referee Renamed")

        windows = self.store.availability_for_official(self._official("O1").id)
        self.assertEqual(len(windows), 1)
        self.assertEqual(windows[0].status.value, "unavailable")
        self.assertEqual(windows[0].note, "Injured")

    # -- 4. club dedup within one commit --------------------------------------
    def test_club_dedup_within_one_commit(self):
        res = self.api.commit_officials_availability_import(
            {"officials_csv": OFFICIALS_CSV}, actor_id="admin")
        self.assertNotIn("error", res)
        self.assertTrue(res["committed"])
        self.assertEqual(res["summary"]["clubs_created"], 1)
        self.assertEqual(len(self.store.all_clubs()), 1)
        o1 = self._official("O1")
        o2 = self._official("O2")
        self.assertEqual(o1.home_club_id, o2.home_club_id)

    # -- 4b. official email contact destination -------------------------------
    def test_email_contact_destination(self):
        res = self.api.commit_officials_availability_import(
            _valid_sheets_csv(), actor_id="admin")
        self.assertTrue(res["committed"])
        o1 = self._official("O1")
        o2 = self._official("O2")
        dest_o1 = self.store.get_contact_destination(
            f"official:{o1.id}", NotificationChannel.EMAIL)
        self.assertIsNotNone(dest_o1)
        self.assertEqual(dest_o1.destination, "pat@example.com")
        dest_o2 = self.store.get_contact_destination(
            f"official:{o2.id}", NotificationChannel.EMAIL)
        self.assertIsNone(dest_o2)

    def test_email_contact_destination_updates_idempotently_on_repeat_import(self):
        self.api.commit_officials_availability_import(
            _valid_sheets_csv(), actor_id="admin")
        changed_officials_csv = OFFICIALS_CSV.replace(
            "pat@example.com", "pat.referee@example.com")
        res = self.api.commit_officials_availability_import(
            {"officials_csv": changed_officials_csv}, actor_id="admin")
        self.assertTrue(res["committed"])
        o1 = self._official("O1")
        dests = [c for c in self.store.all_contact_destinations()
                if c.recipient_ref == f"official:{o1.id}"
                and c.channel == NotificationChannel.EMAIL]
        self.assertEqual(len(dests), 1)  # updated in place, not duplicated
        self.assertEqual(dests[0].destination, "pat.referee@example.com")

    # -- 5. invalid officials row blocks the whole commit ---------------------
    def test_invalid_officials_row_blocks_whole_commit(self):
        res = self.api.commit_officials_availability_import(
            {"officials_csv": DUPLICATE_OFFICIAL_CODE_CSV,
             "official_availability_csv": AVAILABILITY_CSV}, actor_id="admin")
        self.assertNotIn("error", res)
        self.assertFalse(res["committed"])
        self.assertTrue(res["errors"])
        self.assertEqual(self.store.all_officials(), [])

    # -- 6. all-or-nothing across sheets --------------------------------------
    def test_all_or_nothing_across_sheets(self):
        res = self.api.commit_officials_availability_import(
            {"officials_csv": OFFICIALS_CSV,
             "official_availability_csv": UNKNOWN_OFFICIAL_CODE_AVAIL_CSV},
            actor_id="admin")
        self.assertNotIn("error", res)
        self.assertFalse(res["committed"])
        self.assertTrue(res["errors"])
        # The officials sheet was otherwise perfectly clean — but the bad
        # official_code in the availability sheet must still block it.
        self.assertEqual(self.store.all_officials(), [])

    # -- 7. cross-commit official_code resolution -----------------------------
    def test_cross_commit_official_code_resolves_against_existing_official(self):
        first = self.api.commit_officials_availability_import(
            {"officials_csv": OFFICIALS_CSV}, actor_id="admin")
        self.assertTrue(first["committed"])
        self.assertEqual(len(self.store.all_officials()), 2)
        o1_id = self._official("O1").id

        second = self.api.commit_officials_availability_import(
            {"official_availability_csv": AVAILABILITY_CSV}, actor_id="admin")
        self.assertNotIn("error", second)
        self.assertTrue(second["committed"])
        self.assertEqual(second["summary"]["officials"], {"created": 0, "updated": 0})
        self.assertEqual(second["summary"]["official_availability"],
                         {"created": 2, "updated": 0})
        windows = self.store.availability_for_official(o1_id)
        self.assertEqual(len(windows), 1)

    # -- 8. overlapping windows warn, don't block -----------------------------
    def test_overlapping_availability_windows_warn_not_block(self):
        res = self.api.commit_officials_availability_import(
            {"officials_csv": OFFICIALS_CSV,
             "official_availability_csv": OVERLAP_AVAIL_CSV}, actor_id="admin")
        self.assertNotIn("error", res)
        self.assertTrue(res["committed"])
        self.assertTrue(res["warnings"])
        self.assertEqual(len(self.store.availability_for_official(
            self._official("O1").id)), 2)

    # -- 9. audit trail --------------------------------------------------------
    def test_audit_trail_on_first_commit(self):
        res = self.api.commit_officials_availability_import(
            _valid_sheets_csv(), actor_id="admin")
        self.assertTrue(res["committed"])
        actions = [a.action for a in self.store.all_setup_audit()]
        self.assertEqual(actions.count("official_created"), 2)
        self.assertEqual(actions.count("official_availability_set"), 2)
        batches = [a for a in self.store.all_setup_audit()
                  if a.action == "import_committed"]
        self.assertEqual(len(batches), 1)
        self.assertEqual(batches[0].entity_type, "import_batch")
        self.assertEqual(batches[0].detail["officials_created"], 2)
        self.assertEqual(batches[0].detail["availability_created"], 2)

    def test_audit_trail_on_repeat_commit(self):
        self.api.commit_officials_availability_import(
            _valid_sheets_csv(), actor_id="admin")
        res = self.api.commit_officials_availability_import(
            _valid_sheets_csv(), actor_id="admin")
        self.assertTrue(res["committed"])
        actions = [a.action for a in self.store.all_setup_audit()]
        self.assertEqual(actions.count("official_updated"), 2)
        self.assertEqual(actions.count("official_availability_updated"), 2)
        self.assertEqual(actions.count("import_committed"), 2)

    # -- 10. unsupported sheet key ----------------------------------------------
    def test_unsupported_sheet_key_is_validation_error_no_writes(self):
        sheets = dict(_valid_sheets_csv())
        sheets["teams_csv"] = "team_code,team_name\nT1,Team One\n"
        res = self.api.commit_officials_availability_import(sheets, actor_id="admin")
        self.assertIn("error", res)
        self.assertEqual(res["error"]["code"], "validation_error")
        self.assertEqual(self.store.all_officials(), [])


class MemoryImportOfficialsAvailabilityCommitTest(
        ImportOfficialsAvailabilityCommitServiceContract, unittest.TestCase):
    def _store(self):
        return InMemoryStore()


class SqlImportOfficialsAvailabilityCommitTest(
        ImportOfficialsAvailabilityCommitServiceContract, unittest.TestCase):
    def _store(self):
        return SqlStore(":memory:")


class TransactionBoundaryTest(unittest.TestCase):
    """SQL-only (#93's own precedent): InMemoryStore's transaction is a no-op
    and can't distinguish "opened once" from "opened never"."""

    def test_commit_opens_exactly_one_transaction_for_multi_official_multi_window(self):
        store = SqlStore(":memory:")
        api = ApiService(store)

        calls = {"n": 0}
        real = store.transaction

        def counting():
            calls["n"] += 1
            return real()

        store.transaction = counting
        res = api.commit_officials_availability_import(
            _valid_sheets_csv(), actor_id="admin")
        self.assertNotIn("error", res)
        self.assertTrue(res["committed"])
        self.assertEqual(calls["n"], 1)


class ImportOfficialsAvailabilityCommitHttpTest(unittest.TestCase):
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

    def test_league_admin_gets_200(self):
        c = self._client()
        self._login(c, "admin")
        status, body = self._req(
            c, "POST", "/api/import/commit/officials-availability", _valid_sheets_csv())
        self.assertEqual(status, 200)
        self.assertTrue(body["committed"])

    def test_arena_manager_gets_200(self):
        c = self._client()
        self._login(c, "arena")
        status, body = self._req(
            c, "POST", "/api/import/commit/officials-availability", _valid_sheets_csv())
        self.assertEqual(status, 200)
        self.assertTrue(body["committed"])

    def test_coach_forbidden(self):
        c = self._client()
        self._login(c, "coach")
        status, _ = self._req(
            c, "POST", "/api/import/commit/officials-availability", _valid_sheets_csv())
        self.assertEqual(status, 403)

    def test_player_forbidden(self):
        c = self._client()
        self._login(c, "player")
        status, _ = self._req(
            c, "POST", "/api/import/commit/officials-availability", _valid_sheets_csv())
        self.assertEqual(status, 403)

    def test_forged_actor_id_is_ignored_audit_uses_signed_in_admin(self):
        # A signed-in admin sending a forged body actor_id must not be able
        # to forge who the import (and its child rows) is attributed to —
        # same class of issue #93 shipped and had to fix after review; get
        # it right from the start here.
        admin_uid = self.srv.STATE.api.verify_login("admin", "demo")["id"]
        c = self._client()
        self._login(c, "admin")
        # A fresh, unused official_code — other tests in this HTTP class
        # share STATE, so O1/O2 may already exist by the time this runs;
        # a brand-new code guarantees this exercises the CREATE path
        # (official_created), not an update.
        forge_officials_csv = "official_code,name\nFORGE1,Forged Official\n"
        body = {"officials_csv": forge_officials_csv, "actor_id": "attacker"}
        status, resp = self._req(
            c, "POST", "/api/import/commit/officials-availability", body)
        self.assertEqual(status, 200)
        self.assertTrue(resp["committed"])

        audit = self.srv.STATE.api.store.all_setup_audit()
        batch = [a for a in audit if a.action == "import_committed"][-1]
        self.assertEqual(batch.actor_id, admin_uid)
        self.assertNotEqual(batch.actor_id, "attacker")

        official_row = [a for a in audit if a.action == "official_created"][-1]
        self.assertEqual(official_row.actor_id, admin_uid)
        self.assertNotEqual(official_row.actor_id, "attacker")


if __name__ == "__main__":
    unittest.main()
