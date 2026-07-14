"""Complete client hierarchy CSV import (#174 PR E2)."""

import json
import os
import threading
import unittest
import urllib.error
import urllib.request
from http.cookiejar import CookieJar
from http.server import ThreadingHTTPServer

from helpers import BACKEND  # noqa: F401

from hockey_scheduler.api import ApiService
from hockey_scheduler.store import InMemoryStore, SqlStore


ORGANIZATIONS = (
    "organization_code,organization_name,short_name\n"
    "CANLON,Canlon Ice Facilities,Canlon\n"
)
LEAGUES = (
    "league_code,organization_code,league_name,country,timezone\n"
    "OVER55,CANLON,Over 55,US,America/Chicago\n"
)
VENUES_RINKS = (
    "venue_code,organization_code,league_code,venue_name,address,timezone,rink_code,rink_name\n"
    "PLAINFIELD,CANLON,OVER55,Plainfield Ice,123 Main St,America/Chicago,PF1,Rink 1\n"
    "PLAINFIELD,CANLON,OVER55,Plainfield Ice,123 Main St,America/Chicago,PF2,Rink 2\n"
)
COMPETITION = (
    "league_code,season_code,season_name,level_code,level_name,level_sort_order,division_code,division_name,age_group\n"
    "OVER55,FALL26,Fall 2026,L1,Level 1,1,DIVA,Division A,Adult\n"
    "OVER55,FALL26,Fall 2026,L1,Level 1,1,DIVB,Division B,Adult\n"
)


def payload(**overrides):
    body = {
        "import_type": "hierarchy",
        "organizations_csv": ORGANIZATIONS,
        "leagues_csv": LEAGUES,
        "venues_rinks_csv": VENUES_RINKS,
        "competition_csv": COMPETITION,
    }
    body.update(overrides)
    return body


def by_ref(rows, code):
    return next(row for row in rows if row.external_ref == code)


class HierarchyImportContract:
    def make_store(self):
        raise NotImplementedError

    def setUp(self):
        self.store = self.make_store()
        self.api = ApiService(self.store)

    def tearDown(self):
        conn = getattr(self.store, "conn", None)
        if conn is not None:
            conn.close()

    def test_dry_run_validates_without_writes(self):
        result = self.api.get_hierarchy_import_dry_run(payload())
        self.assertTrue(result["ok"])
        self.assertEqual(result["entities"]["organizations"], 1)
        self.assertEqual(result["entities"]["rinks"], 2)
        self.assertEqual(result["entities"]["divisions"], 2)
        self.assertEqual(self.store.all_organizations(), [])
        self.assertEqual(self.store.all_setup_audit(), [])

    def test_unknown_cross_file_reference_blocks_dry_run(self):
        result = self.api.get_hierarchy_import_dry_run(payload(
            leagues_csv=LEAGUES.replace("CANLON,Over 55", "MISSING,Over 55")))
        self.assertFalse(result["ok"])
        self.assertTrue(any("Unknown organization_code MISSING" in e["message"]
                            for e in result["errors"]))
        self.assertEqual(self.store.all_programs(), [])

    def test_venue_owner_must_match_league_owner(self):
        organizations = ORGANIZATIONS + "OTHER,Other Owner,Other\n"
        bad_venues = VENUES_RINKS.replace(
            "PLAINFIELD,CANLON,OVER55", "PLAINFIELD,OTHER,OVER55")
        result = self.api.get_hierarchy_import_dry_run(payload(
            organizations_csv=organizations, venues_rinks_csv=bad_venues))
        self.assertFalse(result["ok"])
        self.assertTrue(any("does not own league_code OVER55" in e["message"]
                            for e in result["errors"]))

    def test_inconsistent_repeated_venue_is_rejected(self):
        bad = VENUES_RINKS.replace(
            "PLAINFIELD,CANLON,OVER55,Plainfield Ice,123 Main St,America/Chicago,PF2",
            "PLAINFIELD,CANLON,OVER55,Different Venue,123 Main St,America/Chicago,PF2")
        result = self.api.get_hierarchy_import_dry_run(payload(
            venues_rinks_csv=bad))
        self.assertFalse(result["ok"])
        self.assertTrue(any("Rows using venue_code PLAINFIELD disagree" in e["message"]
                            for e in result["errors"]))

    def test_first_commit_creates_full_hierarchy(self):
        result = self.api.commit_hierarchy_import(payload(), actor_id="admin")
        self.assertTrue(result["committed"])
        summary = result["summary"]
        self.assertEqual(summary["organizations"]["created"], 1)
        self.assertEqual(summary["leagues"]["created"], 1)
        self.assertEqual(summary["venues"]["created"], 1)
        self.assertEqual(summary["rinks"]["created"], 2)
        self.assertEqual(summary["seasons"]["created"], 1)
        self.assertEqual(summary["levels"]["created"], 1)
        self.assertEqual(summary["divisions"]["created"], 2)

        org = by_ref(self.store.all_organizations(), "CANLON")
        league = by_ref(self.store.all_programs(), "OVER55")
        venue = by_ref(self.store.all_venues(), "PLAINFIELD")
        season = by_ref(self.store.all_seasons(), "FALL26")
        level = by_ref(self.store.all_leagues(), "L1")
        division = by_ref(self.store.all_divisions(), "DIVA")
        self.assertEqual(league.operator_organization_id, org.id)
        self.assertEqual(venue.organization_id, org.id)
        self.assertEqual(venue.league_id, league.id)
        self.assertEqual(season.program_id, league.id)
        self.assertEqual(level.season_id, season.id)
        self.assertEqual(division.season_id, season.id)
        self.assertEqual(division.league_id, level.id)
        self.assertEqual({r.venue_id for r in self.store.all_rinks()}, {venue.id})

    def test_identical_repeat_is_skipped_not_duplicated(self):
        self.api.commit_hierarchy_import(payload(), actor_id="admin")
        result = self.api.commit_hierarchy_import(payload(), actor_id="admin")
        self.assertTrue(result["committed"])
        # Nothing is created or updated on an identical repeat.
        for counts in result["summary"].values():
            self.assertEqual(counts["created"], 0)
            self.assertEqual(counts["updated"], 0)
        # Every entity present in this payload was skipped (not re-created). The
        # teams/registrations sheets aren't part of this payload, so they carry
        # no rows and are legitimately 0/0/0.
        for name in ("organizations", "leagues", "venues", "rinks",
                     "seasons", "levels", "divisions"):
            self.assertGreater(result["summary"][name]["skipped"], 0)
        self.assertEqual(len(self.store.all_organizations()), 1)
        self.assertEqual(len(self.store.all_programs()), 1)
        self.assertEqual(len(self.store.all_venues()), 1)
        self.assertEqual(len(self.store.all_rinks()), 2)
        self.assertEqual(len(self.store.all_seasons()), 1)
        self.assertEqual(len(self.store.all_leagues()), 1)
        self.assertEqual(len(self.store.all_divisions()), 2)

    def test_changed_repeat_updates_in_place(self):
        self.api.commit_hierarchy_import(payload(), actor_id="admin")
        ids = {
            "org": by_ref(self.store.all_organizations(), "CANLON").id,
            "league": by_ref(self.store.all_programs(), "OVER55").id,
            "venue": by_ref(self.store.all_venues(), "PLAINFIELD").id,
            "season": by_ref(self.store.all_seasons(), "FALL26").id,
            "level": by_ref(self.store.all_leagues(), "L1").id,
            "division": by_ref(self.store.all_divisions(), "DIVA").id,
        }
        changed = payload(
            organizations_csv=ORGANIZATIONS.replace("Canlon Ice Facilities", "Canlon Arenas"),
            leagues_csv=LEAGUES.replace("Over 55", "Over 55 League"),
            venues_rinks_csv=VENUES_RINKS.replace("Plainfield Ice", "Plainfield Arena"),
            competition_csv=COMPETITION.replace("Fall 2026", "Autumn 2026")
                                             .replace("Division A", "Premier A"))
        result = self.api.commit_hierarchy_import(changed, actor_id="admin")
        self.assertTrue(result["committed"])
        self.assertEqual(result["summary"]["organizations"]["updated"], 1)
        self.assertEqual(result["summary"]["leagues"]["updated"], 1)
        self.assertEqual(result["summary"]["venues"]["updated"], 1)
        self.assertEqual(result["summary"]["seasons"]["updated"], 1)
        self.assertEqual(result["summary"]["divisions"]["updated"], 1)
        self.assertEqual(by_ref(self.store.all_organizations(), "CANLON").id, ids["org"])
        self.assertEqual(by_ref(self.store.all_programs(), "OVER55").id, ids["league"])
        self.assertEqual(by_ref(self.store.all_venues(), "PLAINFIELD").id, ids["venue"])
        self.assertEqual(by_ref(self.store.all_seasons(), "FALL26").id, ids["season"])
        self.assertEqual(by_ref(self.store.all_leagues(), "L1").id, ids["level"])
        self.assertEqual(by_ref(self.store.all_divisions(), "DIVA").id, ids["division"])

    def test_incremental_files_can_reference_existing_codes(self):
        first = payload(venues_rinks_csv="", competition_csv="")
        self.assertTrue(self.api.commit_hierarchy_import(first)["committed"])
        second = payload(organizations_csv="", leagues_csv="")
        self.assertTrue(self.api.commit_hierarchy_import(second)["committed"])
        self.assertEqual(len(self.store.all_venues()), 1)
        self.assertEqual(len(self.store.all_rinks()), 2)
        self.assertEqual(len(self.store.all_seasons()), 1)

    def test_missing_rows_never_delete_existing_records(self):
        self.api.commit_hierarchy_import(payload())
        result = self.api.commit_hierarchy_import(payload(
            leagues_csv="", venues_rinks_csv="", competition_csv=""))
        self.assertTrue(result["committed"])
        self.assertEqual(len(self.store.all_programs()), 1)
        self.assertEqual(len(self.store.all_venues()), 1)
        self.assertEqual(len(self.store.all_divisions()), 2)

    def test_invalid_commit_is_all_or_nothing_and_has_no_audit(self):
        bad = payload(competition_csv=COMPETITION.replace("OVER55,FALL26", "UNKNOWN,FALL26"))
        result = self.api.commit_hierarchy_import(bad, actor_id="admin")
        self.assertFalse(result["committed"])
        self.assertEqual(self.store.all_organizations(), [])
        self.assertEqual(self.store.all_programs(), [])
        self.assertEqual(self.store.all_setup_audit(), [])

    def test_batch_and_entity_audits_share_batch_id(self):
        result = self.api.commit_hierarchy_import(payload(), actor_id="admin")
        self.assertTrue(result["committed"])
        audits = self.store.all_setup_audit()
        batch = next(a for a in audits if a.action == "import_committed")
        self.assertEqual(batch.actor_id, "admin")
        self.assertEqual(batch.detail["import_type"], "hierarchy")
        self.assertEqual(batch.detail["created"], 9)
        entity_rows = [a for a in audits if a.entity_type != "import_batch"]
        self.assertEqual(len(entity_rows), 9)
        self.assertTrue(all(a.detail["import_batch_id"] == batch.entity_id
                            for a in entity_rows))


class MemoryHierarchyImportTest(HierarchyImportContract, unittest.TestCase):
    def make_store(self):
        return InMemoryStore()


class DurableHierarchyImportTest(HierarchyImportContract, unittest.TestCase):
    def make_store(self):
        url = os.environ.get("TEST_DATABASE_URL") or ":memory:"
        store = SqlStore(url)
        store.reset_schema()
        return store


class HierarchyImportTransactionTest(unittest.TestCase):
    def test_commit_opens_exactly_one_store_transaction(self):
        store = SqlStore(":memory:")
        self.addCleanup(store.conn.close)
        api = ApiService(store)
        calls = {"count": 0}
        real = store.transaction

        def counting_transaction():
            calls["count"] += 1
            return real()

        store.transaction = counting_transaction
        result = api.commit_hierarchy_import(payload(), actor_id="admin")
        self.assertTrue(result["committed"])
        self.assertEqual(calls["count"], 1)


class HierarchyImportHttpTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = __import__("hockey_scheduler.web.server", fromlist=["x"])
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), cls.srv.Handler)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.thread.join(timeout=5)

    def setUp(self):
        self.srv.STATE.reset()

    def client(self):
        return urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(CookieJar()))

    def request(self, opener, path, body):
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=json.dumps(body).encode(), method="POST",
            headers={"Content-Type": "application/json"})
        try:
            with opener.open(req) as response:
                return response.status, json.loads(response.read() or b"{}")
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read() or b"{}")

    def login(self, opener, username):
        return self.request(opener, "/api/auth/login",
                            {"username": username, "password": "demo"})

    def test_league_admin_can_validate_and_commit(self):
        client = self.client()
        self.login(client, "admin")
        before = len(self.srv.STATE.api.store.all_organizations())
        status, preview = self.request(
            client, "/api/import/commit/teams-players",
            payload(dry_run=True))
        self.assertEqual(status, 200)
        self.assertTrue(preview["ok"])
        self.assertEqual(
            len(self.srv.STATE.api.store.all_organizations()), before)

        status, committed = self.request(
            client, "/api/import/commit/teams-players", payload())
        self.assertEqual(status, 200)
        self.assertTrue(committed["committed"])

    def test_arena_manager_is_forbidden(self):
        client = self.client()
        self.login(client, "arena")
        status, body = self.request(
            client, "/api/import/commit/teams-players",
            payload(dry_run=True))
        self.assertEqual(status, 403)
        self.assertEqual(body["error"]["details"]["required"], "manage_setup")

    def test_forged_actor_and_secret_fields_are_ignored(self):
        admin_id = self.srv.STATE.api.verify_login("admin", "demo")["id"]
        client = self.client()
        self.login(client, "admin")
        body = payload(actor_id="attacker", password="plaintext-secret")
        status, response = self.request(
            client, "/api/import/commit/teams-players", body)
        self.assertEqual(status, 200)
        self.assertTrue(response["committed"])
        blob = json.dumps(response)
        self.assertNotIn("attacker", blob)
        self.assertNotIn("plaintext-secret", blob)
        batch = [a for a in self.srv.STATE.api.store.all_setup_audit()
                 if a.action == "import_committed"][-1]
        self.assertEqual(batch.actor_id, admin_id)
        self.assertNotIn("plaintext-secret", json.dumps(batch.detail))


if __name__ == "__main__":
    unittest.main()
