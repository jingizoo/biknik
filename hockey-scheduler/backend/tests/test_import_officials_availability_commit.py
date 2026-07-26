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
import os
import threading
import unittest
import urllib.error
import urllib.request
from http.cookiejar import CookieJar
from http.server import ThreadingHTTPServer

from helpers import BACKEND  # noqa: F401  (ensures sys.path is set up)

from hockey_scheduler.api import ApiService
from hockey_scheduler.domain import NotificationChannel, Official
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

NO_CLUB_OFFICIALS_CSV = (
    "official_code,name,home_club_name\n"
    "N1,No Club Blank,\n"
    "N2,No Club NA,NA\n"
    "N3,No Club na lower,na\n"
    "N4,Has Club,Real Club\n"
)


def _valid_sheets_csv():
    return {"officials_csv": OFFICIALS_CSV, "official_availability_csv": AVAILABILITY_CSV}


RACE_OFFICIAL_CSV = (
    "official_code,name\n"
    "RACE1,Race Official\n"
)

RACE_AVAILABILITY_CSV = (
    "official_code,start_time,end_time,status\n"
    "RACE1,2026-08-01T18:00:00+00:00,2026-08-01T20:00:00+00:00,available\n"
)

# #331 review round 12 finding 1: DISTINCT official_codes so the Official
# unique index (migration 047) never contends -- the only shared identity
# under race is the brand-new home_club_name both rows resolve to.
RACE_CLUB_OFFICIAL_A_CSV = (
    "official_code,name,home_club_name\n"
    "RACE_CLUB_A,Race Official A,Race Club\n"
)
RACE_CLUB_OFFICIAL_B_CSV = (
    "official_code,name,home_club_name\n"
    "RACE_CLUB_B,Race Official B,Race Club\n"
)


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

    # -- 4a. blank/NA home_club_name means no Club (#233 Slice D) -------------
    def test_blank_and_na_home_club_name_creates_no_club(self):
        res = self.api.commit_officials_availability_import(
            {"officials_csv": NO_CLUB_OFFICIALS_CSV}, actor_id="admin")
        self.assertNotIn("error", res)
        self.assertTrue(res["committed"])
        self.assertEqual(res["summary"]["clubs_created"], 1)
        clubs = self.store.all_clubs()
        self.assertEqual(len(clubs), 1)
        self.assertEqual(clubs[0].name, "Real Club")
        self.assertIsNone(self._official("N1").home_club_id)
        self.assertIsNone(self._official("N2").home_club_id)
        self.assertIsNone(self._official("N3").home_club_id)
        self.assertEqual(self._official("N4").home_club_id, clubs[0].id)

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
        self.assertEqual(batches[0].detail["import_type"], "officials_availability")
        # #102: official_availability_set is written via the shared
        # set_official_availability() single-entity method (reused for the
        # "brand new window" case) — confirm its extra_detail plumbing
        # actually tags the row back to this commit's batch, same as every
        # other row-level entry.
        batch_id = batches[0].entity_id
        row_entries = [a for a in self.store.all_setup_audit()
                      if a.action in ("official_created", "official_availability_set")]
        self.assertEqual(len(row_entries), 4)
        for a in row_entries:
            self.assertEqual(a.detail["import_batch_id"], batch_id)

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


@unittest.skipUnless(os.environ.get("TEST_DATABASE_URL"),
                     "PostgreSQL required (set TEST_DATABASE_URL)")
class PostgresOfficialsAvailabilityImportRaceTest(unittest.TestCase):
    """#331 review round 11 finding 2: unlike commit_teams_players_import's
    Season row lock, commit_officials_availability_import has no row to
    lock for a brand-new official_code or availability window -- two
    concurrent commits landing the identical key must not both succeed in
    creating their own duplicate row. Each thread drives its OWN
    ApiService(SqlStore(...)) -- a separate connection and process-local
    RLock -- so passing here depends on the real PostgreSQL unique-index
    backstop (migration 047), not the in-process lock a shared store
    instance would provide for free.

    Memory/SQLite parity for the same "identical input committed twice
    does not duplicate" property is already covered by
    test_idempotent_repeat_commit_updates_not_duplicates above, which runs
    on both backends via ImportOfficialsAvailabilityCommitServiceContract
    -- no separate parity test is added here to avoid duplicating it."""

    def setUp(self):
        self.url = os.environ["TEST_DATABASE_URL"]
        SqlStore(self.url).clear_all_data()

    def test_identical_official_and_window_commits_do_not_duplicate(self):
        store_a, store_b = SqlStore(self.url), SqlStore(self.url)
        api_a, api_b = ApiService(store_a), ApiService(store_b)

        barrier = threading.Barrier(2)
        # Pause each side's OWN connection right before it generates a new
        # Official's id -- reached only after its own absence-check read
        # already ran and found nothing, exactly the review's required
        # "after their initial absence observation before either writes"
        # ordering. Deliberately NOT hooked any later than this (e.g. at
        # add_official itself): next_id() upserts a shared per-prefix row
        # in `counters`, so pausing AFTER it already ran would leave one
        # side holding that row's lock while blocked on this very barrier
        # waiting for the other side -- which can't arrive because ITS OWN
        # next_id() call is blocked on that same lock. A real circular
        # wait, self-inflicted by the test, not the production code (the
        # same class of bug already fixed once in this PR's history for
        # the forced cancel-vs-commit race). Filtered to the "official"
        # prefix only, so the unrelated next_id("importbatch") call before
        # the retry loop starts isn't paused too.
        def _pausing(store):
            real = store.next_id
            def _wrapped(prefix):
                if prefix == "official":
                    barrier.wait(timeout=10)
                return real(prefix)
            return _wrapped
        store_a.next_id = _pausing(store_a)
        store_b.next_id = _pausing(store_b)

        results = {}

        def run(api, key):
            try:
                results[key] = api.commit_officials_availability_import(
                    {"officials_csv": RACE_OFFICIAL_CSV,
                     "official_availability_csv": RACE_AVAILABILITY_CSV},
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
        officials = [o for o in fresh.all_officials() if o.external_ref == "RACE1"]
        self.assertEqual(
            len(officials), 1,
            "expected exactly one Official for RACE1, got "
            f"{[o.id for o in officials]}")
        windows = fresh.availability_for_official(officials[0].id)
        self.assertEqual(
            len(windows), 1,
            f"expected exactly one availability window, got {[w.id for w in windows]}")
        audit_batches = [a for a in fresh.all_setup_audit()
                        if a.action == "import_committed"]
        self.assertEqual(
            len(audit_batches), 2,
            "expected one import_committed audit row per commit call "
            f"(one create, one retried-then-update), got {len(audit_batches)}")

    def test_identical_window_commits_for_a_pre_existing_official_do_not_duplicate(self):
        """The test above races next_id("official") specifically, so its
        losing side's retry finds the Official ALREADY there and never
        contends on the window itself -- it cannot tell apart a working
        official_availability unique index (migration 047's second index)
        from a missing one. This test isolates that half: the Official is
        pre-seeded (existing for BOTH threads from the start, never raced),
        so the ONLY contended write left is the availability window for an
        identical (official, start, end) -- exactly the review's own
        "even a pre-existing Official can receive two identical windows"
        sentence."""
        seed_store = SqlStore(self.url)
        seed_official = Official(id=seed_store.next_id("official"),
                                 name="Race Official", external_ref="RACE1")
        seed_store.add_official(seed_official)

        store_a, store_b = SqlStore(self.url), SqlStore(self.url)
        api_a, api_b = ApiService(store_a), ApiService(store_b)

        barrier = threading.Barrier(2)
        # Same reasoning as above, keyed to "oavail" (OfficialAvailability's
        # id prefix) instead of "official" -- reached only after each side's
        # own (official_id, start, end) absence-check already ran and found
        # nothing.
        def _pausing(store):
            real = store.next_id
            def _wrapped(prefix):
                if prefix == "oavail":
                    barrier.wait(timeout=10)
                return real(prefix)
            return _wrapped
        store_a.next_id = _pausing(store_a)
        store_b.next_id = _pausing(store_b)

        results = {}

        def run(api, key):
            try:
                results[key] = api.commit_officials_availability_import(
                    {"official_availability_csv": RACE_AVAILABILITY_CSV},
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
        officials = [o for o in fresh.all_officials() if o.external_ref == "RACE1"]
        self.assertEqual(
            len(officials), 1,
            "the pre-seeded Official must stay singular, got "
            f"{[o.id for o in officials]}")
        windows = fresh.availability_for_official(officials[0].id)
        self.assertEqual(
            len(windows), 1,
            f"expected exactly one availability window, got {[w.id for w in windows]}")

    def test_identical_new_home_club_name_commits_do_not_duplicate_club(self):
        """#331 review round 12 finding 1: migration 047's indexes protect
        the Official row itself, but the Club it's found-or-created FROM has
        no unique-by-name backstop of its own. Two concurrent commits that
        each resolve a brand-new home_club_name can each see it absent and
        each create their own duplicate Club -- and since that doesn't
        violate any DB constraint, the existing retry loop never fires: the
        LOSER's later Official lookup (a DIFFERENT official_code here, so
        IT never contends) just proceeds normally and points its own new
        Official at its own orphaned Club, never discovering the winner's.
        This test uses two distinct official_codes specifically so the
        Official index can't be what saves it -- only the fix under test
        (double-checked locking over next_id("club")'s own cross-connection
        counter-row lock, see the comment at that call site) can."""
        store_a, store_b = SqlStore(self.url), SqlStore(self.url)
        api_a, api_b = ApiService(store_a), ApiService(store_b)

        barrier = threading.Barrier(2)
        # Pause each side's OWN connection right before it generates a new
        # Club's id -- reached only after its own Club absence-check already
        # ran and found nothing. Not hooked any later (e.g. add_club
        # itself): next_id() upserts the shared per-prefix "club" counter
        # row, so pausing after it already ran risks the same self-inflicted
        # circular wait this file's other race tests document at length.
        def _pausing(store):
            real = store.next_id
            def _wrapped(prefix):
                if prefix == "club":
                    barrier.wait(timeout=10)
                return real(prefix)
            return _wrapped
        store_a.next_id = _pausing(store_a)
        store_b.next_id = _pausing(store_b)

        results = {}

        def run(api, key, csv):
            try:
                results[key] = api.commit_officials_availability_import(
                    {"officials_csv": csv}, actor_id=key)
            except Exception as exc:
                results[key] = exc

        ta = threading.Thread(target=run, args=(api_a, "a", RACE_CLUB_OFFICIAL_A_CSV))
        tb = threading.Thread(target=run, args=(api_b, "b", RACE_CLUB_OFFICIAL_B_CSV))
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
        clubs = [c for c in fresh.all_clubs() if c.name == "Race Club"]
        self.assertEqual(
            len(clubs), 1,
            f"expected exactly one Club named 'Race Club', got {[c.id for c in clubs]}")
        official_a = next(o for o in fresh.all_officials()
                          if o.external_ref == "RACE_CLUB_A")
        official_b = next(o for o in fresh.all_officials()
                          if o.external_ref == "RACE_CLUB_B")
        self.assertEqual(
            official_a.home_club_id, clubs[0].id,
            "official A must point at the surviving Club, not an orphan")
        self.assertEqual(
            official_b.home_club_id, clubs[0].id,
            "official B must point at the surviving Club, not an orphan")


if __name__ == "__main__":
    unittest.main()
