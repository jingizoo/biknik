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
import os
import threading
import unittest
import urllib.error
import urllib.request
from http.cookiejar import CookieJar
from http.server import ThreadingHTTPServer

from helpers import BACKEND  # noqa: F401  (ensures sys.path is set up)

from hockey_scheduler.api import ApiService
from hockey_scheduler.domain import IceSlotStatus, Venue
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


RACE_RINK_CSV = (
    "venue_name,rink_code\n"
    "Race Arena,RACE1\n"
)

RACE_SLOT_CSV = (
    "rink_code,start_time,end_time,slot_type\n"
    "RACE1,2026-09-01T18:00:00+00:00,2026-09-01T19:30:00+00:00,game\n"
)


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

    # -- 6b. a new slot overlapping ALREADY-PERSISTED ice is rejected ---------
    def test_import_overlapping_persisted_slot_is_rejected_and_rolls_back(self):
        # Same-sheet overlaps stay warning-only (above), but a new slot that
        # would overlap ice a PRIOR write already persisted is a hard conflict:
        # the whole import is rejected and fully rolled back on every backend
        # (#158 review). A first import persists R1's 18:00-19:30 game slot.
        first = self.api.commit_rinks_ice_slots_import(
            _valid_sheets_csv(), actor_id="admin")
        self.assertTrue(first["committed"])
        slots_before = {s.id for s in self.store.all_ice_slots()}
        audits_before = len(self.store.all_setup_audit())

        # A second import brings a NON-exact overlapping slot on the same rink
        # (19:00-20:00 overlaps the persisted 18:00-19:30) -- the case migration
        # 045's exact-tuple unique index cannot catch.
        overlap_csv = (
            "rink_code,start_time,end_time,slot_type\n"
            "R1,2026-09-01T19:00:00+00:00,2026-09-01T20:00:00+00:00,game\n"
        )
        res = self.api.commit_rinks_ice_slots_import(
            {"rinks_csv": RINKS_CSV, "ice_slots_csv": overlap_csv},
            actor_id="admin")
        self.assertIn("error", res)
        self.assertEqual(res["error"]["code"], "schedule_conflict")
        # Full rollback: no new slot landed, the persisted slot is untouched,
        # and the rejected batch wrote NO audit rows (not even the rink_updated
        # rows its rink sheet would have produced) -- Memory and SQLite alike.
        self.assertEqual({s.id for s in self.store.all_ice_slots()}, slots_before)
        self.assertEqual(len(self.store.all_setup_audit()), audits_before)

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


@unittest.skipUnless(os.environ.get("TEST_DATABASE_URL"),
                     "PostgreSQL required (set TEST_DATABASE_URL)")
class PostgresRinksIceSlotsImportRaceTest(unittest.TestCase):
    """#331 review round 11 finding 2: commit_rinks_ice_slots_import row-
    locks every rink it ALREADY has, but a brand-new rink_code has no row
    to lock -- two concurrent commits landing the identical new rink_code
    must not both succeed in creating their own duplicate Rink. Each
    thread drives its OWN ApiService(SqlStore(...)) -- a separate
    connection and process-local RLock -- so passing here depends on the
    real PostgreSQL unique-index backstop (migration 048), not the
    in-process lock a shared store instance would provide for free.

    The Venue is pre-seeded (existing, not raced) so both threads' Venue
    lookups resolve identically without contending on next_id("venue")'s
    own shared counter row -- that contention is a SEPARATE, out-of-scope
    concern (see migration 048's own comment on the Venue-by-name match),
    not something this test's synchronization needs to fight through to
    exercise the Rink race cleanly.

    Memory/SQLite parity for the same "identical input committed twice
    does not duplicate" property is already covered by
    test_idempotent_repeat_commit_updates_not_duplicates above, which runs
    on both backends via ImportRinksIceSlotsCommitServiceContract -- no
    separate parity test is added here to avoid duplicating it."""

    def setUp(self):
        self.url = os.environ["TEST_DATABASE_URL"]
        SqlStore(self.url).clear_all_data()

    def test_identical_new_rink_and_slot_commits_do_not_duplicate(self):
        seed_store = SqlStore(self.url)
        seed_venue = Venue(id=seed_store.next_id("venue"), name="Race Arena", address="")
        seed_store.add_venue(seed_venue)

        store_a, store_b = SqlStore(self.url), SqlStore(self.url)
        api_a, api_b = ApiService(store_a), ApiService(store_b)

        barrier = threading.Barrier(2)
        # Pause each side's OWN connection right before it generates a new
        # Rink's id -- reached only after its own Rink absence-check
        # already ran and found nothing (the Venue lookup above it finds
        # the pre-seeded row and never calls next_id("venue") at all),
        # exactly the review's required "after their initial absence
        # observation before either writes" ordering. Deliberately NOT
        # hooked any later than this (e.g. at add_rink itself): next_id()
        # upserts a shared per-prefix row in `counters`, so pausing AFTER
        # it already ran would leave one side holding that row's lock
        # while blocked on this very barrier waiting for the other side --
        # which can't arrive because ITS OWN next_id() call is blocked on
        # that same lock. A real circular wait, self-inflicted by the
        # test, not the production code (the same class of bug already
        # fixed once in this PR's history for the forced cancel-vs-commit
        # race).
        def _pausing(store):
            real = store.next_id
            def _wrapped(prefix):
                if prefix == "rink":
                    barrier.wait(timeout=10)
                return real(prefix)
            return _wrapped
        store_a.next_id = _pausing(store_a)
        store_b.next_id = _pausing(store_b)

        results = {}

        def run(api, key):
            try:
                results[key] = api.commit_rinks_ice_slots_import(
                    {"rinks_csv": RACE_RINK_CSV, "ice_slots_csv": RACE_SLOT_CSV},
                    actor_id=key)
            except Exception as exc:
                results[key] = exc

        ta = threading.Thread(target=run, args=(api_a, "a"))
        tb = threading.Thread(target=run, args=(api_b, "b"))
        ta.start(); tb.start()
        ta.join(20); tb.join(20)

        self.assertFalse(ta.is_alive(), "thread a hung")
        self.assertFalse(tb.is_alive(), "thread b hung")
        for key, res in results.items():
            self.assertNotIsInstance(
                res, Exception,
                f"commit {key} raised instead of committing or retrying "
                f"cleanly: {res!r}")
            self.assertTrue(res.get("committed"), f"commit {key}: {res}")

        fresh = SqlStore(self.url)
        rinks = [r for r in fresh.all_rinks() if r.external_ref == "RACE1"]
        self.assertEqual(
            len(rinks), 1,
            f"expected exactly one Rink for RACE1, got {[r.id for r in rinks]}")
        venues = [v for v in fresh.all_venues() if v.name == "Race Arena"]
        self.assertEqual(
            len(venues), 1,
            "expected exactly one Venue for 'Race Arena' (the pre-seeded "
            f"one, untouched), got {[v.id for v in venues]}")
        slots = [s for s in fresh.all_ice_slots() if s.rink_id == rinks[0].id]
        self.assertEqual(
            len(slots), 1, f"expected exactly one ice slot, got {[s.id for s in slots]}")
        audit_batches = [a for a in fresh.all_setup_audit()
                        if a.action == "import_committed"]
        self.assertEqual(
            len(audit_batches), 2,
            f"expected one import_committed audit row per commit call, got "
            f"{len(audit_batches)}")

    def test_identical_new_venue_name_commits_do_not_duplicate_venue(self):
        """#331 review round 12 finding 1: migration 048 protects the Rink
        row itself, but the Venue it's found-or-created FROM has no
        unique-by-name backstop (this migration's own comment names the
        gap explicitly). Two concurrent commits that each resolve a
        brand-new venue_name can each see it absent and each create their
        own duplicate Venue -- and since that doesn't violate any DB
        constraint, the existing retry loop never fires: the LOSER's later
        Rink lookup (a DIFFERENT rink_code here, so IT never contends)
        just proceeds normally and points its own new Rink at its own
        orphaned Venue, never discovering the winner's. Distinct rink_codes
        specifically so the Rink index can't be what saves it -- only the
        fix under test (double-checked locking over next_id("venue")'s own
        cross-connection counter-row lock) can."""
        store_a, store_b = SqlStore(self.url), SqlStore(self.url)
        api_a, api_b = ApiService(store_a), ApiService(store_b)

        barrier = threading.Barrier(2)
        # Same reasoning as the Rink race above, keyed to "venue" instead --
        # reached only after each side's own Venue absence-check already ran
        # and found nothing.
        def _pausing(store):
            real = store.next_id
            def _wrapped(prefix):
                if prefix == "venue":
                    barrier.wait(timeout=10)
                return real(prefix)
            return _wrapped
        store_a.next_id = _pausing(store_a)
        store_b.next_id = _pausing(store_b)

        race_rink_a_csv = "venue_name,rink_code\nRace Venue,RACE_VENUE_A\n"
        race_rink_b_csv = "venue_name,rink_code\nRace Venue,RACE_VENUE_B\n"

        results = {}

        def run(api, key, csv):
            try:
                results[key] = api.commit_rinks_ice_slots_import(
                    {"rinks_csv": csv}, actor_id=key)
            except Exception as exc:
                results[key] = exc

        ta = threading.Thread(target=run, args=(api_a, "a", race_rink_a_csv))
        tb = threading.Thread(target=run, args=(api_b, "b", race_rink_b_csv))
        ta.start(); tb.start()
        ta.join(20); tb.join(20)

        self.assertFalse(ta.is_alive(), "thread a hung")
        self.assertFalse(tb.is_alive(), "thread b hung")
        for key, res in results.items():
            self.assertNotIsInstance(
                res, Exception,
                f"commit {key} raised instead of committing or retrying "
                f"cleanly: {res!r}")
            self.assertTrue(res.get("committed"), f"commit {key}: {res}")

        fresh = SqlStore(self.url)
        venues = [v for v in fresh.all_venues() if v.name == "Race Venue"]
        self.assertEqual(
            len(venues), 1,
            f"expected exactly one Venue named 'Race Venue', got {[v.id for v in venues]}")
        rink_a = next(r for r in fresh.all_rinks() if r.external_ref == "RACE_VENUE_A")
        rink_b = next(r for r in fresh.all_rinks() if r.external_ref == "RACE_VENUE_B")
        self.assertEqual(
            rink_a.venue_id, venues[0].id,
            "rink A must point at the surviving Venue, not an orphan")
        self.assertEqual(
            rink_b.venue_id, venues[0].id,
            "rink B must point at the surviving Venue, not an orphan")


@unittest.skipUnless(os.environ.get("TEST_DATABASE_URL"),
                     "PostgreSQL required (set TEST_DATABASE_URL)")
class PostgresRinksIceSlotsSlotTypeGameRaceTest(unittest.TestCase):
    """#331 review round 12 finding 2: the booked-slot slot_type gate used
    to run in a lock-free preflight, before commit_rinks_ice_slots_import's
    own transaction/rink lock. create_game takes that SAME rink lock before
    allocating a slot, so a Game could commit in the window between the
    import's stale preflight read and the import's own lock acquisition,
    leaving the import to overwrite slot_type on a slot a brand-new Game
    now depends on staying GAME-bookable. A single connection can never
    observe this ordering -- the fix moved the check to run fresh, under
    the lock -- so this needs two independent connections racing for real,
    like this file's other Postgres-only tests."""

    def setUp(self):
        self.url = os.environ["TEST_DATABASE_URL"]
        SqlStore(self.url).clear_all_data()

    def test_import_cannot_retype_a_slot_a_concurrent_game_just_claimed(self):
        seed_store = SqlStore(self.url)
        seed_api = ApiService(seed_store)
        seed_setup = SetupService(seed_store)
        first = seed_api.commit_rinks_ice_slots_import(
            _valid_sheets_csv(), actor_id="admin")
        self.assertTrue(first["committed"])
        rink = next(r for r in seed_store.all_rinks() if r.external_ref == "R1")
        slot = next(s for s in seed_store.all_ice_slots() if s.rink_id == rink.id)

        program = seed_setup.create_program("Race League", actor_id="admin")
        venue = seed_store.get_venue(rink.venue_id)
        season = seed_setup.create_season(program.id, "2026 Season", actor_id="admin")
        seed_setup.grant_season_venue_access(season.id, venue.id, actor_id="admin")
        division = seed_setup.create_division(season.id, "U16", actor_id="admin")
        club = seed_setup.create_club("Race Club", actor_id="admin")
        home = seed_setup.create_team(club.id, division.id, "Home", actor_id="admin")
        away = seed_setup.create_team(club.id, division.id, "Away", actor_id="admin")
        seed_setup.register_team_for_season(season.id, home.id, division.id,
                                            actor_id="admin")
        seed_setup.register_team_for_season(season.id, away.id, division.id,
                                            actor_id="admin")

        audit_ids_before = {a.id for a in seed_store.all_setup_audit()}

        store_a, store_b = SqlStore(self.url), SqlStore(self.url)
        api_a = ApiService(store_a)
        setup_b = SetupService(store_b)

        rink_locked = threading.Event()
        game_committed = threading.Event()

        # Pause the import right before it locks the target rink -- the
        # exact point its now-fixed slot_type gate runs after. Let
        # create_game run to completion on an INDEPENDENT connection first,
        # then release the import: its fresh, under-lock read must see the
        # Game that just claimed the slot.
        real_get_rink_for_update = store_a.get_rink_for_update
        def _pausing_get_rink_for_update(rink_id):
            if rink_id == rink.id:
                rink_locked.set()
                if not game_committed.wait(timeout=10):
                    raise AssertionError(
                        "create_game thread never signalled completion")
            return real_get_rink_for_update(rink_id)
        store_a.get_rink_for_update = _pausing_get_rink_for_update

        changed_type_csv = (
            "rink_code,start_time,end_time,slot_type\n"
            f"R1,{slot.start_time.isoformat()},{slot.end_time.isoformat()},practice\n"
        )

        results = {}

        def run_import():
            try:
                results["import"] = api_a.commit_rinks_ice_slots_import(
                    {"rinks_csv": RINKS_CSV, "ice_slots_csv": changed_type_csv},
                    actor_id="importer")
            except Exception as exc:
                results["import"] = exc

        def run_create_game():
            self.assertTrue(rink_locked.wait(timeout=10),
                            "import never reached its rink-lock pause")
            try:
                results["game"] = setup_b.create_game(
                    season.id, division.id, home.id, away.id, slot.id,
                    actor_id="scheduler")
            except Exception as exc:
                results["game"] = exc
            finally:
                game_committed.set()

        ta = threading.Thread(target=run_import)
        tb = threading.Thread(target=run_create_game)
        ta.start(); tb.start()
        ta.join(20); tb.join(20)

        self.assertFalse(ta.is_alive(), "import thread hung")
        self.assertFalse(tb.is_alive(), "create_game thread hung")

        self.assertNotIsInstance(
            results.get("game"), Exception,
            f"create_game raised unexpectedly: {results.get('game')!r}")

        imported = results.get("import")
        self.assertIsInstance(
            imported, dict,
            f"import raised instead of returning a structured rejection: {imported!r}")
        self.assertIn(
            "error", imported,
            "the import must be rejected, not silently retype a slot a "
            f"concurrent game just claimed: {imported}")
        self.assertEqual(imported["error"]["code"], "validation_error")

        fresh = SqlStore(self.url)
        allocated_slot = fresh.get_ice_slot(slot.id)
        self.assertEqual(allocated_slot.slot_type.value, "game")
        self.assertEqual(allocated_slot.status.value, "allocated")
        self.assertIsNotNone(
            fresh.game_using_ice_slot(slot.id),
            "the Game must still claim its slot")

        new_audits = [a for a in fresh.all_setup_audit()
                     if a.id not in audit_ids_before]
        self.assertEqual(
            [a.action for a in new_audits], ["game_created"],
            "the rejected import must not write any audit row -- only "
            f"create_game's own success, got {[a.action for a in new_audits]}")


if __name__ == "__main__":
    unittest.main()
