"""CSV import dry-run validator (#92).

Step 1 of the Pilot Onboarding Import Wizard: an operator submits
spreadsheet-shaped CSV text for teams/players/officials/rinks/ice_slots and
gets back a structured validation report. Nothing is ever written to the
store in this slice — commit/write flows are separate follow-up PRs
(#93-#96).
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
from hockey_scheduler.services.import_validator import parse_csv_text, validate_import
from hockey_scheduler.store import InMemoryStore, SqlStore

TEAMS_CSV = (
    "team_code,team_name,club_name,division_name\n"
    "U16-LIONS,U16 Lions,Lions Club,U16\n"
    "U16-FALCONS,U16 Falcons,Falcons Club,U16\n"
)

PLAYERS_CSV = (
    "player_code,first_name,last_name,team_code,jersey_number,position,email\n"
    "P1,Aarav,M,U16-LIONS,9,forward,aarav@example.com\n"
    "P2,Kabir,S,U16-FALCONS,10,defense,kabir@example.com\n"
)

OFFICIALS_CSV = (
    "official_code,name,email,home_club_name\n"
    "O1,Pat Referee,pat@example.com,\n"
)

RINKS_CSV = (
    "venue_name,rink_code,rink_name,address\n"
    "Ice Palace,R1,Rink 1,123 Main St\n"
)

ICE_SLOTS_CSV = (
    "rink_code,start_time,end_time,slot_type\n"
    "R1,2026-09-01T18:00:00+00:00,2026-09-01T19:30:00+00:00,game\n"
    "R1,2026-09-01T20:00:00+00:00,2026-09-01T21:00:00+00:00,game\n"
)


def _valid_sheets_csv():
    return {
        "teams_csv": TEAMS_CSV,
        "players_csv": PLAYERS_CSV,
        "officials_csv": OFFICIALS_CSV,
        "rinks_csv": RINKS_CSV,
        "ice_slots_csv": ICE_SLOTS_CSV,
    }


class ImportDryRunServiceContract:
    """Run against both store backends — the validator itself never touches
    either one, but the facade method takes a store-backed ApiService."""

    def _store(self):
        raise NotImplementedError

    def setUp(self):
        self.store = self._store()
        self.api = ApiService(self.store)

    def test_fully_valid_multi_sheet_import_is_ok(self):
        res = self.api.get_import_dry_run(_valid_sheets_csv())
        self.assertNotIn("error", res)
        self.assertTrue(res["ok"])
        self.assertEqual(res["errors"], [])
        self.assertEqual(res["summary"], {
            "teams": 2, "players": 2, "officials": 1, "rinks": 1, "ice_slots": 2,
        })

    def test_missing_required_field_errors(self):
        sheets = {"teams_csv": (
            "team_code,team_name,club_name,division_name\n"
            ",U16 Lions,Lions Club,U16\n"
        )}
        res = self.api.get_import_dry_run(sheets)
        self.assertFalse(res["ok"])
        self.assertEqual(len(res["errors"]), 1)
        err = res["errors"][0]
        self.assertEqual(err["sheet"], "teams")
        self.assertEqual(err["row"], 1)
        self.assertEqual(err["field"], "team_code")

    def test_teams_missing_code_header_reports_structured_error(self):
        # A header column entirely absent (not just a blank cell) must still
        # surface as a normal structured error, never an exception — _codes()
        # builds the cross-sheet reference set from this same sheet and must
        # not assume the field key is present on every row (#92 review).
        sheets = {"teams_csv": "team_name\nU16 Lions\n"}
        res = self.api.get_import_dry_run(sheets)
        self.assertNotIn("error", res)
        self.assertFalse(res["ok"])
        err = next(e for e in res["errors"] if e["sheet"] == "teams")
        self.assertEqual(err["field"], "team_code")

    def test_rinks_missing_code_header_reports_structured_error(self):
        sheets = {"rinks_csv": "venue_name\nIce Palace\n"}
        res = self.api.get_import_dry_run(sheets)
        self.assertNotIn("error", res)
        self.assertFalse(res["ok"])
        err = next(e for e in res["errors"] if e["sheet"] == "rinks")
        self.assertEqual(err["field"], "rink_code")

    def test_duplicate_code_within_sheet_errors(self):
        sheets = {
            "teams_csv": "team_code,team_name\nT1,Team One\n",
            "players_csv": (
                "player_code,first_name,last_name,team_code\n"
                "P1,Aarav,M,T1\n"
                "P1,Kabir,S,T1\n"
            ),
        }
        res = self.api.get_import_dry_run(sheets)
        self.assertFalse(res["ok"])
        self.assertEqual(len(res["errors"]), 1)
        self.assertEqual(res["errors"][0]["sheet"], "players")
        self.assertIn("Duplicate player_code P1", res["errors"][0]["message"])

    def test_unknown_team_code_on_player_row_errors(self):
        sheets = {
            "teams_csv": "team_code,team_name\nT1,Team One\n",
            "players_csv": (
                "player_code,first_name,last_name,team_code\n"
                "P1,Aarav,M,T9\n"
            ),
        }
        res = self.api.get_import_dry_run(sheets)
        self.assertFalse(res["ok"])
        err = next(e for e in res["errors"] if e["sheet"] == "players")
        self.assertEqual(err["field"], "team_code")
        self.assertIn("Unknown team_code T9", err["message"])

    def test_unknown_rink_code_on_ice_slot_row_errors(self):
        sheets = {
            "rinks_csv": "venue_name,rink_code\nIce Palace,R1\n",
            "ice_slots_csv": (
                "rink_code,start_time,end_time,slot_type\n"
                "R9,2026-09-01T18:00:00+00:00,2026-09-01T19:30:00+00:00,game\n"
            ),
        }
        res = self.api.get_import_dry_run(sheets)
        self.assertFalse(res["ok"])
        err = next(e for e in res["errors"] if e["field"] == "rink_code")
        self.assertEqual(err["sheet"], "ice_slots")
        self.assertIn("Unknown rink_code R9", err["message"])

    def test_bad_datetime_on_ice_slot_row_errors(self):
        sheets = {
            "rinks_csv": "venue_name,rink_code\nIce Palace,R1\n",
            "ice_slots_csv": (
                "rink_code,start_time,end_time,slot_type\n"
                "R1,not-a-date,2026-09-01T19:30:00+00:00,game\n"
            ),
        }
        res = self.api.get_import_dry_run(sheets)
        self.assertFalse(res["ok"])
        err = next(e for e in res["errors"] if e["field"] == "start_time")
        self.assertEqual(err["sheet"], "ice_slots")
        self.assertEqual(err["row"], 1)

    def test_overlapping_ice_slots_on_same_rink_warn_not_error(self):
        sheets = {
            "rinks_csv": "venue_name,rink_code\nIce Palace,R1\n",
            "ice_slots_csv": (
                "rink_code,start_time,end_time,slot_type\n"
                "R1,2026-09-01T18:00:00+00:00,2026-09-01T19:30:00+00:00,game\n"
                "R1,2026-09-01T19:00:00+00:00,2026-09-01T20:00:00+00:00,game\n"
            ),
        }
        res = self.api.get_import_dry_run(sheets)
        self.assertTrue(res["ok"])
        self.assertEqual(res["errors"], [])
        self.assertEqual(len(res["warnings"]), 1)
        self.assertIn("overlaps another slot on the same rink",
                      res["warnings"][0]["message"])

    def test_omitted_sheet_key_is_treated_as_empty(self):
        sheets = _valid_sheets_csv()
        del sheets["officials_csv"]
        res = self.api.get_import_dry_run(sheets)
        self.assertNotIn("error", res)
        self.assertTrue(res["ok"])
        self.assertEqual(res["summary"]["officials"], 0)

    def test_no_writes_to_store(self):
        before = (len(self.store.all_teams()), len(self.store.all_officials()),
                  len(self.store.all_rinks()), len(self.store.all_ice_slots()))
        res = self.api.get_import_dry_run(_valid_sheets_csv())
        self.assertTrue(res["ok"])
        after = (len(self.store.all_teams()), len(self.store.all_officials()),
                 len(self.store.all_rinks()), len(self.store.all_ice_slots()))
        self.assertEqual(before, (0, 0, 0, 0))
        self.assertEqual(before, after)


class MemoryImportDryRunTest(ImportDryRunServiceContract, unittest.TestCase):
    def _store(self):
        return InMemoryStore()


class SqlImportDryRunTest(ImportDryRunServiceContract, unittest.TestCase):
    def _store(self):
        return SqlStore(":memory:")


class ValidateImportPureTest(unittest.TestCase):
    """Directly against the pure validator with hand-built rows and text —
    no store, no ApiService involved (#92)."""

    def test_no_sheets_is_ok_with_zero_summary(self):
        res = validate_import({})
        self.assertTrue(res["ok"])
        self.assertEqual(res["summary"], {
            "teams": 0, "players": 0, "officials": 0, "rinks": 0, "ice_slots": 0,
        })

    def test_parse_csv_text_rows_are_1_indexed_after_the_header(self):
        rows = parse_csv_text("team_code,team_name\nA1,Team A\nB1,Team B\n")
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["team_code"], "A1")
        self.assertEqual(rows[1]["team_code"], "B1")

    def test_parse_csv_text_empty_string_is_no_rows(self):
        self.assertEqual(parse_csv_text(""), [])


class ImportDryRunHttpTest(unittest.TestCase):
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

    def _login(self, opener, username):
        return self._req(opener, "POST", "/api/auth/login",
                         {"username": username, "password": "demo"})

    def test_non_operator_role_forbidden(self):
        c = self._client()
        self._login(c, "coach")
        status, _ = self._req(c, "POST", "/api/import/dry-run", _valid_sheets_csv())
        self.assertEqual(status, 403)

    def test_operator_gets_200_with_structured_report(self):
        c = self._client()
        self._login(c, "arena")
        status, body = self._req(c, "POST", "/api/import/dry-run", _valid_sheets_csv())
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertEqual(body["summary"], {
            "teams": 2, "players": 2, "officials": 1, "rinks": 1, "ice_slots": 2,
        })

    def test_missing_header_over_http_returns_normal_report_not_server_error(self):
        # A missing column header (not a blank cell) over the real route must
        # still come back as a normal 200 report, never a 500 (#92 review).
        c = self._client()
        self._login(c, "arena")
        status, body = self._req(c, "POST", "/api/import/dry-run",
                                 {"teams_csv": "team_name\nU16 Lions\n"})
        self.assertEqual(status, 200)
        self.assertFalse(body["ok"])
        err = next(e for e in body["errors"] if e["sheet"] == "teams")
        self.assertEqual(err["field"], "team_code")

    def test_league_admin_also_permitted(self):
        c = self._client()
        self._login(c, "admin")
        status, body = self._req(c, "POST", "/api/import/dry-run", _valid_sheets_csv())
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])

    def test_no_writes_via_http_route(self):
        before = len(self.srv.STATE.api.store.all_teams())
        c = self._client()
        self._login(c, "admin")
        status, body = self._req(c, "POST", "/api/import/dry-run", _valid_sheets_csv())
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertEqual(len(self.srv.STATE.api.store.all_teams()), before)


if __name__ == "__main__":
    unittest.main()
