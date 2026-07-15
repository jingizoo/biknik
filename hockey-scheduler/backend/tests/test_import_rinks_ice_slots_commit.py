"""CSV import rinks + ice_slots COMMIT (#95).

Step 4 of the Pilot Onboarding Import Wizard: takes the same CSV shapes #92's
dry-run validator accepts, re-validates via the SAME pure ``validate_import``
gate (unchanged — rinks/ice_slots are already first-class
``IMPORT_SHEET_NAMES`` members, unlike #94's officials_availability), and —
only if that gate is clean — writes rinks (and any find-or-created venues)
plus their ice slots inside a single transaction. Teams/players (#93) and
officials/availability (#94) are out of scope here.
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
from hockey_scheduler.domain import IceSlotStatus
from hockey_scheduler.services import SetupService
from hockey_scheduler.store import InMemoryStore, SqlStore

RINKS_CSV = (
    "venue_name,rink_code,rink_name,address\n"
    "Ice Palace,R1,Rink 1,123 Main St\n"
    "Ice Palace,R2,Rink 2,123 Main St\n"
)

ICE_SLOTS_CSV = (
    "rink_code,start_time,end_time,slot_type\n"
    "R1,2026-09-01T18:00:00+00:00,2026-09-01T19:30:00+00:00,game\n"
    "R2,2026-09-01T20:00:00+00:00,2026-09-01T21:00:00+00:00,practice\n"
)

DUPLICATE_RINK_CODE_CSV = (
    "venue_name,rink_code\n"
    "Ice Palace,R1\n"
    "Ice Palace,R1\n"
)

UNKNOWN_RINK_CODE_SLOT_CSV = (
    "rink_code,start_time,end_time,slot_type\n"
    "R9,2026-09-01T18:00:00+00:00,2026-09-01T19:30:00+00:00,game\n"
)

OVERLAP_SLOT_CSV = (
    "rink_code,start_time,end_time,slot_type\n"
    "R1,2026-09-01T18:00:00+00:00,2026-09-01T19:30:00+00:00,game\n"
    "R1,2026-09-01T19:00:00+00:00,2026-09-01T20:00:00+00:00,game\n"
)


def _valid_sheets_csv():
    return {"rinks_csv": RINKS_CSV, "ice_slots_csv": ICE_SLOTS_CSV}


class ImportRinksIceSlotsCommitServiceContract:
    """Run against both store backends (mirrors test_import_commit.py)."""

    def _store(self):
        raise NotImplementedError

    def setUp(self):
        self.store = self._store()
        self.api = ApiService(self.store)
        self.setup = SetupService(self.store)

    def _rink(self, code):
        return next(r for r in self.store.all_rinks() if r.external_ref == code)

    # -- 1. first commit creates ---------------------------------------------
    def test_first_commit_creates_rinks_and_ice_slots(self):
        res = self.api.commit_rinks_ice_slots_import(
            _valid_sheets_csv(), actor_id="admin")
        self.assertNotIn("error", res)
        self.assertTrue(res["committed"])
        self.assertEqual(res["summary"]["rinks"], {"created": 2, "updated": 0})
        self.assertEqual(res["summary"]["ice_slots"], {"created": 2, "updated": 0})
        self.assertEqual(res["summary"]["venues_created"], 1)

        self.assertEqual(len(self.store.all_rinks()), 2)
        self.assertEqual({r.external_ref for r in self.store.all_rinks()},
                         {"R1", "R2"})
        self.assertEqual(len(self.store.all_ice_slots()), 2)
        # #95 is rinks+ice_slots only — a normal successful commit must never
        # touch teams/players/officials/official_availability.
        self.assertEqual(self.store.all_teams(), [])
        self.assertEqual(self.store.all_players(), [])
        self.assertEqual(self.store.all_officials(), [])

    # -- 2. idempotent repeat -------------------------------------------------
    def test_idempotent_repeat_commit_updates_not_duplicates(self):
        first = self.api.commit_rinks_ice_slots_import(
            _valid_sheets_csv(), actor_id="admin")
        self.assertTrue(first["committed"])
        rinks_after_first = len(self.store.all_rinks())
        slots_after_first = len(self.store.all_ice_slots())

        second = self.api.commit_rinks_ice_slots_import(
            _valid_sheets_csv(), actor_id="admin")
        self.assertNotIn("error", second)
        self.assertTrue(second["committed"])
        self.assertEqual(second["summary"]["rinks"], {"created": 0, "updated": 2})
        self.assertEqual(second["summary"]["ice_slots"], {"created": 0, "updated": 2})
        self.assertEqual(len(self.store.all_rinks()), rinks_after_first)
        self.assertEqual(len(self.store.all_ice_slots()), slots_after_first)

    # -- 2b. repeat import with a changed name updates in place --------------
    def test_repeat_commit_with_changed_name_updates_existing_record(self):
        self.api.commit_rinks_ice_slots_import(
            _valid_sheets_csv(), actor_id="admin")
        renamed_rinks_csv = RINKS_CSV.replace("Rink 1", "Rink 1 Renamed")
        res = self.api.commit_rinks_ice_slots_import(
            {"rinks_csv": renamed_rinks_csv}, actor_id="admin")
        self.assertTrue(res["committed"])
        self.assertEqual(res["summary"]["rinks"], {"created": 0, "updated": 2})
        self.assertEqual(len(self.store.all_rinks()), 2)  # still no duplicate
        self.assertEqual(self._rink("R1").name, "Rink 1 Renamed")

    # -- 2c. repeat import omitting rink_name must not clobber it (review fix)
    def test_repeat_commit_without_rink_name_leaves_existing_name_unchanged(self):
        self.api.commit_rinks_ice_slots_import(
            _valid_sheets_csv(), actor_id="admin")
        self.assertEqual(self._rink("R1").name, "Rink 1")
        # rink_name isn't required by validate_import — a repeat row that
        # omits it (only venue_name/rink_code supplied) must leave the
        # existing name alone rather than clobbering it back to "R1".
        bare_rinks_csv = "venue_name,rink_code\nIce Palace,R1\n"
        res = self.api.commit_rinks_ice_slots_import(
            {"rinks_csv": bare_rinks_csv}, actor_id="admin")
        self.assertTrue(res["committed"])
        self.assertEqual(res["summary"]["rinks"], {"created": 0, "updated": 1})
        self.assertEqual(self._rink("R1").name, "Rink 1")

    # -- 3. venue dedup within one commit -------------------------------------
    def test_venue_dedup_within_one_commit(self):
        res = self.api.commit_rinks_ice_slots_import(
            {"rinks_csv": RINKS_CSV}, actor_id="admin")
        self.assertNotIn("error", res)
        self.assertTrue(res["committed"])
        self.assertEqual(res["summary"]["venues_created"], 1)
        self.assertEqual(len(self.store.all_venues()), 1)
        r1 = self._rink("R1")
        r2 = self._rink("R2")
        self.assertEqual(r1.venue_id, r2.venue_id)

    # -- 4. invalid rinks row blocks the whole commit -------------------------
    def test_invalid_rinks_row_blocks_whole_commit(self):
        res = self.api.commit_rinks_ice_slots_import(
            {"rinks_csv": DUPLICATE_RINK_CODE_CSV}, actor_id="admin")
        self.assertNotIn("error", res)
        self.assertFalse(res["committed"])
        self.assertTrue(res["errors"])
        self.assertEqual(self.store.all_rinks(), [])

    # -- 5. all-or-nothing across sheets ---------------------------------------
    def test_all_or_nothing_across_sheets(self):
        res = self.api.commit_rinks_ice_slots_import(
            {"rinks_csv": RINKS_CSV,
             "ice_slots_csv": UNKNOWN_RINK_CODE_SLOT_CSV}, actor_id="admin")
        self.assertNotIn("error", res)
        self.assertFalse(res["committed"])
        self.assertTrue(res["errors"])
        # The rinks sheet was otherwise perfectly clean — but the bad
        # rink_code in the ice_slots sheet must still block it.
        self.assertEqual(self.store.all_rinks(), [])

    # -- 6. overlapping slots warn, don't block -------------------------------
    def test_overlapping_slots_warn_not_block(self):
        res = self.api.commit_rinks_ice_slots_import(
            {"rinks_csv": RINKS_CSV, "ice_slots_csv": OVERLAP_SLOT_CSV},
            actor_id="admin")
        self.assertNotIn("error", res)
        self.assertTrue(res["committed"])
        self.assertTrue(res["warnings"])
        self.assertEqual(len(self.store.all_ice_slots()), 2)

    # -- 7. audit trail --------------------------------------------------------
    def test_audit_trail_on_first_commit(self):
        res = self.api.commit_rinks_ice_slots_import(
            _valid_sheets_csv(), actor_id="admin")
        self.assertTrue(res["committed"])
        actions = [a.action for a in self.store.all_setup_audit()]
        self.assertEqual(actions.count("venue_created"), 1)
        self.assertEqual(actions.count("rink_created"), 2)
        self.assertEqual(actions.count("ice_slot_created"), 2)
        batches = [a for a in self.store.all_setup_audit()
                  if a.action == "import_committed"]
        self.assertEqual(len(batches), 1)
        self.assertEqual(batches[0].entity_type, "import_batch")
        self.assertEqual(batches[0].detail["rinks_created"], 2)
        self.assertEqual(batches[0].detail["ice_slots_created"], 2)
        self.assertEqual(batches[0].detail["import_type"], "rinks_ice_slots")
        # #102: every row-level entry this commit wrote is tagged back to
        # this same batch id, the link the Activity feed's drill-down groups
        # rows by.
        batch_id = batches[0].entity_id
        row_entries = [a for a in self.store.all_setup_audit()
                      if a.action in ("venue_created", "rink_created", "ice_slot_created")]
        self.assertEqual(len(row_entries), 5)
        for a in row_entries:
            self.assertEqual(a.detail["import_batch_id"], batch_id)

    def test_audit_trail_on_repeat_commit(self):
        self.api.commit_rinks_ice_slots_import(
            _valid_sheets_csv(), actor_id="admin")
        res = self.api.commit_rinks_ice_slots_import(
            _valid_sheets_csv(), actor_id="admin")
        self.assertTrue(res["committed"])
        actions = [a.action for a in self.store.all_setup_audit()]
        self.assertEqual(actions.count("rink_updated"), 2)
        self.assertEqual(actions.count("ice_slot_updated"), 2)
        self.assertEqual(actions.count("import_committed"), 2)

    # -- 8. unsupported sheet key -----------------------------------------------
    def test_unsupported_sheet_key_is_validation_error_no_writes(self):
        sheets = dict(_valid_sheets_csv())
        sheets["teams_csv"] = "team_code,team_name\nT1,Team One\n"
        res = self.api.commit_rinks_ice_slots_import(sheets, actor_id="admin")
        self.assertIn("error", res)
        self.assertEqual(res["error"]["code"], "validation_error")
        self.assertEqual(self.store.all_rinks(), [])

    # -- 9. a repeat import must not clobber a game-allocated slot -------------
    def test_repeat_import_does_not_clobber_allocated_slot_status(self):
        # Booking a game onto a slot flips its status to ALLOCATED
        # (create_game, setup_service.py). A repeat import of the SAME
        # ice_slots.csv row must not silently reset that back to AVAILABLE —
        # the game record would still point at a slot the import just
        # un-booked, desyncing the two.
        first = self.api.commit_rinks_ice_slots_import(
            _valid_sheets_csv(), actor_id="admin")
        self.assertTrue(first["committed"])
        slot = next(s for s in self.store.all_ice_slots()
                   if s.rink_id == self._rink("R1").id)

        league = self.setup.create_program("Test League", actor_id="admin")
        venue = self.store.get_venue(self._rink("R1").venue_id)
        season = self.setup.create_season(
            league.id, "2026 Season", actor_id="admin")
        self.setup.grant_season_venue_access(season.id, venue.id, actor_id="admin")
        division = self.setup.create_division(
            season.id, "U16", actor_id="admin")
        club = self.setup.create_club("Test Club", actor_id="admin")
        home = self.setup.create_team(club.id, division.id, "Home",
                                      actor_id="admin")
        away = self.setup.create_team(club.id, division.id, "Away",
                                      actor_id="admin")
        self.setup.register_team_for_season(season.id, home.id, division.id,
                                            actor_id="admin")
        self.setup.register_team_for_season(season.id, away.id, division.id,
                                            actor_id="admin")
        self.setup.create_game(season.id, division.id, home.id, away.id,
                               slot.id, actor_id="admin")

        allocated_slot = self.store.get_ice_slot(slot.id)
        self.assertEqual(allocated_slot.status, IceSlotStatus.ALLOCATED)

        second = self.api.commit_rinks_ice_slots_import(
            _valid_sheets_csv(), actor_id="admin")
        self.assertTrue(second["committed"])
        reimported_slot = self.store.get_ice_slot(slot.id)
        self.assertEqual(reimported_slot.status, IceSlotStatus.ALLOCATED)
        self.assertEqual(reimported_slot.slot_type.value, "game")

    # -- 10. a repeat import must not change slot_type under a booked game ----
    def test_repeat_import_with_changed_slot_type_on_allocated_slot_is_rejected(self):
        # create_game requires a booked slot's slot_type to stay GAME. A
        # repeat import that changes slot_type away from GAME on a slot a
        # game already uses must be rejected outright (all-or-nothing), not
        # silently applied — that would leave the game pointing at ice that
        # is no longer game-bookable (review fix).
        first = self.api.commit_rinks_ice_slots_import(
            _valid_sheets_csv(), actor_id="admin")
        self.assertTrue(first["committed"])
        slot = next(s for s in self.store.all_ice_slots()
                   if s.rink_id == self._rink("R1").id)

        league = self.setup.create_program("Test League", actor_id="admin")
        venue = self.store.get_venue(self._rink("R1").venue_id)
        season = self.setup.create_season(
            league.id, "2026 Season", actor_id="admin")
        self.setup.grant_season_venue_access(season.id, venue.id, actor_id="admin")
        division = self.setup.create_division(
            season.id, "U16", actor_id="admin")
        club = self.setup.create_club("Test Club", actor_id="admin")
        home = self.setup.create_team(club.id, division.id, "Home",
                                      actor_id="admin")
        away = self.setup.create_team(club.id, division.id, "Away",
                                      actor_id="admin")
        self.setup.register_team_for_season(season.id, home.id, division.id,
                                            actor_id="admin")
        self.setup.register_team_for_season(season.id, away.id, division.id,
                                            actor_id="admin")
        self.setup.create_game(season.id, division.id, home.id, away.id,
                               slot.id, actor_id="admin")

        changed_type_csv = (
            "rink_code,start_time,end_time,slot_type\n"
            "R1,2026-09-01T18:00:00+00:00,2026-09-01T19:30:00+00:00,practice\n"
        )
        res = self.api.commit_rinks_ice_slots_import(
            {"rinks_csv": RINKS_CSV, "ice_slots_csv": changed_type_csv},
            actor_id="admin")
        self.assertIn("error", res)
        self.assertEqual(res["error"]["code"], "validation_error")

        unchanged_slot = self.store.get_ice_slot(slot.id)
        self.assertEqual(unchanged_slot.slot_type.value, "game")
        self.assertEqual(unchanged_slot.status, IceSlotStatus.ALLOCATED)


class MemoryImportRinksIceSlotsCommitTest(
        ImportRinksIceSlotsCommitServiceContract, unittest.TestCase):
    def _store(self):
        return InMemoryStore()


class SqlImportRinksIceSlotsCommitTest(
        ImportRinksIceSlotsCommitServiceContract, unittest.TestCase):
    def _store(self):
        return SqlStore(":memory:")


class TransactionBoundaryTest(unittest.TestCase):
    """SQL-only (#94's own precedent): InMemoryStore's transaction is a no-op
    and can't distinguish "opened once" from "opened never"."""

    def test_commit_opens_exactly_one_transaction_for_multi_rink_multi_slot(self):
        store = SqlStore(":memory:")
        api = ApiService(store)

        calls = {"n": 0}
        real = store.transaction

        def counting():
            calls["n"] += 1
            return real()

        store.transaction = counting
        res = api.commit_rinks_ice_slots_import(
            _valid_sheets_csv(), actor_id="admin")
        self.assertNotIn("error", res)
        self.assertTrue(res["committed"])
        self.assertEqual(calls["n"], 1)


class ImportRinksIceSlotsCommitHttpTest(unittest.TestCase):
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
            c, "POST", "/api/import/commit/rinks-ice-slots", _valid_sheets_csv())
        self.assertEqual(status, 200)
        self.assertTrue(body["committed"])

    def test_arena_manager_gets_200(self):
        c = self._client()
        self._login(c, "arena")
        status, body = self._req(
            c, "POST", "/api/import/commit/rinks-ice-slots", _valid_sheets_csv())
        self.assertEqual(status, 200)
        self.assertTrue(body["committed"])

    def test_coach_forbidden(self):
        c = self._client()
        self._login(c, "coach")
        status, _ = self._req(
            c, "POST", "/api/import/commit/rinks-ice-slots", _valid_sheets_csv())
        self.assertEqual(status, 403)

    def test_player_forbidden(self):
        c = self._client()
        self._login(c, "player")
        status, _ = self._req(
            c, "POST", "/api/import/commit/rinks-ice-slots", _valid_sheets_csv())
        self.assertEqual(status, 403)

    def test_forged_actor_id_is_ignored_audit_uses_signed_in_admin(self):
        # A signed-in admin sending a forged body actor_id must not be able
        # to forge who the import (and its child rows) is attributed to —
        # same class of issue #93 shipped and had to fix after review; get
        # it right from the start here.
        admin_uid = self.srv.STATE.api.verify_login("admin", "demo")["id"]
        c = self._client()
        self._login(c, "admin")
        # A fresh, unused rink_code — other tests in this HTTP class share
        # STATE, so R1/R2 may already exist by the time this runs; a
        # brand-new code guarantees this exercises the CREATE path
        # (rink_created), not an update.
        forge_rinks_csv = "venue_name,rink_code\nForge Arena,FORGE1\n"
        body = {"rinks_csv": forge_rinks_csv, "actor_id": "attacker"}
        status, resp = self._req(
            c, "POST", "/api/import/commit/rinks-ice-slots", body)
        self.assertEqual(status, 200)
        self.assertTrue(resp["committed"])

        audit = self.srv.STATE.api.store.all_setup_audit()
        batch = [a for a in audit if a.action == "import_committed"][-1]
        self.assertEqual(batch.actor_id, admin_uid)
        self.assertNotEqual(batch.actor_id, "attacker")

        rink_row = [a for a in audit if a.action == "rink_created"][-1]
        self.assertEqual(rink_row.actor_id, admin_uid)
        self.assertNotEqual(rink_row.actor_id, "attacker")


if __name__ == "__main__":
    unittest.main()
