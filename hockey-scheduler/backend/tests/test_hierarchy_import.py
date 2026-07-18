"""Canonical nine-sheet client hierarchy CSV import (#174 PR E2, #260 Slice F).

Column vocabulary and sheet set follow ADR 0001 and issue #260's locked
design review: organizations, programs, venues_rinks, competition (season/
league/optional division), clubs, permanent_teams, players, registrations,
season_venue_access. Fixtures live in ``hierarchy_fixtures.py`` so each test
states only what it overrides rather than repeating full CSV strings.
"""

import json
import os
import threading
import unittest
import urllib.error
import urllib.request
from http.cookiejar import CookieJar
from http.server import ThreadingHTTPServer

from helpers import BACKEND  # noqa: F401

import hierarchy_fixtures as fx
from hockey_scheduler.api import ApiService
from hockey_scheduler.store import InMemoryStore, SqlStore

by_ref = fx.by_ref


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

    # -- dry run --------------------------------------------------------
    def test_dry_run_validates_without_writes(self):
        result = self.api.get_hierarchy_import_dry_run(fx.full_payload())
        self.assertTrue(result["ok"], result.get("errors"))
        self.assertEqual(result["entities"]["organizations"], 1)
        self.assertEqual(result["entities"]["programs"], 1)
        self.assertEqual(result["entities"]["venues"], 1)
        self.assertEqual(result["entities"]["rinks"], 2)
        self.assertEqual(result["entities"]["seasons"], 1)
        self.assertEqual(result["entities"]["leagues"], 1)
        self.assertEqual(result["entities"]["divisions"], 2)
        self.assertEqual(result["entities"]["clubs"], 1)
        self.assertEqual(result["entities"]["permanent_teams"], 2)
        self.assertEqual(result["entities"]["players"], 1)
        self.assertEqual(result["entities"]["registrations"], 2)
        self.assertEqual(result["entities"]["season_venue_access"], 1)
        self.assertEqual(self.store.all_organizations(), [])
        self.assertEqual(self.store.all_setup_audit(), [])

    def test_unknown_cross_file_reference_blocks_dry_run(self):
        result = self.api.get_hierarchy_import_dry_run(fx.full_payload(
            programs_csv=fx.programs_csv(
                rows=("OVER55,MISSING,Over 55,US,America/Chicago",))))
        self.assertFalse(result["ok"])
        self.assertTrue(any("Unknown operator_organization_code MISSING"
                            in e["message"] for e in result["errors"]))
        self.assertEqual(self.store.all_programs(), [])

    def test_all_errors_returned_in_one_pass_not_fail_fast(self):
        # Two independent, unrelated errors in the same upload — the dry run
        # must report both, never stop at the first (#260 review).
        result = self.api.get_hierarchy_import_dry_run(fx.full_payload(
            programs_csv=fx.programs_csv(
                rows=("OVER55,MISSING,Over 55,US,America/Chicago",)),
            clubs_csv=fx.clubs_csv(rows=("EAGLES,Eagles HC,US",
                                         "EAGLES,Eagles HC Again,US"))))
        self.assertFalse(result["ok"])
        codes = {e["code"] for e in result["errors"]}
        self.assertIn("unknown_organization_code", codes)
        self.assertIn("duplicate_code", codes)

    def test_inconsistent_repeated_venue_is_rejected(self):
        bad = fx.venues_rinks_csv(rows=(
            "PLAINFIELD,CANLON,Plainfield Ice,123 Main St,America/Chicago,PF1,Rink 1",
            "PLAINFIELD,CANLON,Different Venue,123 Main St,America/Chicago,PF2,Rink 2"))
        result = self.api.get_hierarchy_import_dry_run(
            fx.full_payload(venues_rinks_csv=bad))
        self.assertFalse(result["ok"])
        self.assertTrue(any(
            "Rows using venue_code PLAINFIELD disagree" in e["message"]
            for e in result["errors"]))

    # -- commit / idempotency --------------------------------------------
    def test_first_commit_creates_full_hierarchy(self):
        result = self.api.commit_hierarchy_import(
            fx.full_payload(), actor_id="admin")
        self.assertTrue(result["committed"], result.get("errors"))
        summary = result["summary"]
        self.assertEqual(summary["organizations"]["created"], 1)
        self.assertEqual(summary["programs"]["created"], 1)
        self.assertEqual(summary["venues"]["created"], 1)
        self.assertEqual(summary["rinks"]["created"], 2)
        self.assertEqual(summary["seasons"]["created"], 1)
        self.assertEqual(summary["leagues"]["created"], 1)
        self.assertEqual(summary["divisions"]["created"], 2)
        self.assertEqual(summary["clubs"]["created"], 1)
        self.assertEqual(summary["permanent_teams"]["created"], 2)
        self.assertEqual(summary["players"]["created"], 1)
        self.assertEqual(summary["registrations"]["created"], 2)
        self.assertEqual(summary["season_venue_access"]["created"], 1)

        org = by_ref(self.store.all_organizations(), "CANLON")
        program = by_ref(self.store.all_programs(), "OVER55")
        venue = by_ref(self.store.all_venues(), "PLAINFIELD")
        season = by_ref(self.store.all_seasons(), "FALL26")
        league = by_ref(self.store.all_leagues(), "L1")
        division = by_ref(self.store.all_divisions(), "DIVA")
        club = by_ref(self.store.all_clubs(), "EAGLES")
        lions = by_ref(self.store.all_teams(), "LIONS")
        bears = by_ref(self.store.all_teams(), "BEARS")
        player = by_ref(self.store.all_players(), "P001")

        self.assertEqual(program.operator_organization_id, org.id)
        self.assertEqual(venue.organization_id, org.id)
        # The legacy Venue<->Program bridge is retired (#233 Slice E) and is
        # never written by this importer (#260 review).
        self.assertIsNone(venue.league_id)
        self.assertEqual(season.program_id, program.id)
        # #283: a League belongs to a Program; its participation in this Season
        # is a LeagueSeason, off which the Division hangs.
        self.assertEqual(league.program_id, program.id)
        league_season = self.store.league_season_for(league.id, season.id)
        self.assertIsNotNone(league_season)
        div_ls = self.store.get_league_season(division.league_season_id)
        self.assertEqual(div_ls.season_id, season.id)
        self.assertEqual(div_ls.league_id, league.id)
        self.assertEqual({r.venue_id for r in self.store.all_rinks()},
                         {venue.id})
        # Club matched by club_code; LIONS resolves it, BEARS' blank
        # club_code means club_id=None, never a placeholder Club (#260
        # review decision 1).
        self.assertEqual(lions.club_id, club.id)
        self.assertIsNone(bears.club_id)
        self.assertEqual(player.team_id, lions.id)
        contact = self.store.get_contact_destination(
            f"player:{player.id}",
            __import__("hockey_scheduler.domain", fromlist=["x"])
            .NotificationChannel.EMAIL)
        self.assertIsNotNone(contact)
        self.assertEqual(contact.destination, "jane@example.com")
        reg = self.store.registration_for_team_in_season(season.id, lions.id)
        self.assertEqual(
            self.store.get_league_season(reg.league_season_id).league_id,
            league.id)
        self.assertEqual(reg.division_id, division.id)
        self.assertTrue(reg.active)
        access = self.store.season_venue_access_for_pair(season.id, venue.id)
        self.assertTrue(access.active)

    def test_identical_repeat_is_skipped_not_duplicated(self):
        self.api.commit_hierarchy_import(fx.full_payload(), actor_id="admin")
        result = self.api.commit_hierarchy_import(
            fx.full_payload(), actor_id="admin")
        self.assertTrue(result["committed"])
        for counts in result["summary"].values():
            self.assertEqual(counts["created"], 0)
            self.assertEqual(counts["updated"], 0)
        for name in result["summary"]:
            self.assertGreater(result["summary"][name]["skipped"], 0)
        self.assertEqual(len(self.store.all_organizations()), 1)
        self.assertEqual(len(self.store.all_programs()), 1)
        self.assertEqual(len(self.store.all_venues()), 1)
        self.assertEqual(len(self.store.all_rinks()), 2)
        self.assertEqual(len(self.store.all_seasons()), 1)
        self.assertEqual(len(self.store.all_leagues()), 1)
        self.assertEqual(len(self.store.all_divisions()), 2)
        self.assertEqual(len(self.store.all_clubs()), 1)
        self.assertEqual(len(self.store.all_teams()), 2)
        self.assertEqual(len(self.store.all_players()), 1)
        self.assertEqual(len(self.store.all_season_team_registrations()), 2)
        self.assertEqual(len(self.store.all_season_venue_access()), 1)

    def test_changed_repeat_updates_in_place(self):
        self.api.commit_hierarchy_import(fx.full_payload(), actor_id="admin")
        ids = {
            "org": by_ref(self.store.all_organizations(), "CANLON").id,
            "program": by_ref(self.store.all_programs(), "OVER55").id,
            "venue": by_ref(self.store.all_venues(), "PLAINFIELD").id,
            "season": by_ref(self.store.all_seasons(), "FALL26").id,
            "league": by_ref(self.store.all_leagues(), "L1").id,
            "division": by_ref(self.store.all_divisions(), "DIVA").id,
            "club": by_ref(self.store.all_clubs(), "EAGLES").id,
        }
        changed = fx.full_payload(
            organizations_csv=fx.organizations_csv(
                rows=("CANLON,Canlon Arenas,Canlon",)),
            programs_csv=fx.programs_csv(
                rows=("OVER55,CANLON,Over 55 League,US,America/Chicago",)),
            venues_rinks_csv=fx.venues_rinks_csv(rows=(
                "PLAINFIELD,CANLON,Plainfield Arena,123 Main St,America/Chicago,PF1,Rink 1",
                "PLAINFIELD,CANLON,Plainfield Arena,123 Main St,America/Chicago,PF2,Rink 2")),
            competition_csv=fx.competition_csv(rows=(
                "OVER55,FALL26,Autumn 2026,L1,Adult League,1,DIVA,Premier A,Adult",
                "OVER55,FALL26,Autumn 2026,L1,Adult League,1,DIVB,Division B,Adult")),
            clubs_csv=fx.clubs_csv(rows=("EAGLES,Eagles Hockey Club,US",)))
        result = self.api.commit_hierarchy_import(changed, actor_id="admin")
        self.assertTrue(result["committed"], result.get("errors"))
        self.assertEqual(result["summary"]["organizations"]["updated"], 1)
        self.assertEqual(result["summary"]["programs"]["updated"], 1)
        self.assertEqual(result["summary"]["venues"]["updated"], 1)
        self.assertEqual(result["summary"]["seasons"]["updated"], 1)
        self.assertEqual(result["summary"]["divisions"]["updated"], 1)
        self.assertEqual(result["summary"]["clubs"]["updated"], 1)
        self.assertEqual(by_ref(self.store.all_organizations(), "CANLON").id,
                         ids["org"])
        self.assertEqual(by_ref(self.store.all_programs(), "OVER55").id,
                         ids["program"])
        self.assertEqual(by_ref(self.store.all_venues(), "PLAINFIELD").id,
                         ids["venue"])
        self.assertEqual(by_ref(self.store.all_seasons(), "FALL26").id,
                         ids["season"])
        self.assertEqual(by_ref(self.store.all_leagues(), "L1").id,
                         ids["league"])
        self.assertEqual(by_ref(self.store.all_divisions(), "DIVA").id,
                         ids["division"])
        self.assertEqual(by_ref(self.store.all_clubs(), "EAGLES").id,
                         ids["club"])

    def test_incremental_files_can_reference_existing_codes(self):
        first = fx.full_payload(
            venues_rinks_csv="", clubs_csv="", permanent_teams_csv="",
            players_csv="", registrations_csv="", season_venue_access_csv="")
        self.assertTrue(
            self.api.commit_hierarchy_import(first)["committed"])
        second = {"import_type": "hierarchy",
                  "venues_rinks_csv": fx.venues_rinks_csv()}
        self.assertTrue(
            self.api.commit_hierarchy_import(second)["committed"])
        self.assertEqual(len(self.store.all_venues()), 1)
        self.assertEqual(len(self.store.all_rinks()), 2)
        self.assertEqual(len(self.store.all_seasons()), 1)

    def test_missing_rows_never_delete_existing_records(self):
        self.api.commit_hierarchy_import(fx.full_payload())
        result = self.api.commit_hierarchy_import(fx.full_payload(
            programs_csv="", venues_rinks_csv="", competition_csv="",
            clubs_csv="", permanent_teams_csv="", players_csv="",
            registrations_csv="", season_venue_access_csv=""))
        self.assertTrue(result["committed"], result.get("errors"))
        self.assertEqual(len(self.store.all_programs()), 1)
        self.assertEqual(len(self.store.all_venues()), 1)
        self.assertEqual(len(self.store.all_divisions()), 2)
        self.assertEqual(len(self.store.all_clubs()), 1)
        self.assertEqual(len(self.store.all_players()), 1)

    def test_invalid_commit_is_all_or_nothing_and_has_no_audit(self):
        bad = fx.full_payload(competition_csv=fx.competition_csv(rows=(
            "UNKNOWN,FALL26,Fall 2026,L1,Adult League,1,DIVA,Division A,Adult",)))
        result = self.api.commit_hierarchy_import(bad, actor_id="admin")
        self.assertFalse(result["committed"])
        self.assertEqual(self.store.all_organizations(), [])
        self.assertEqual(self.store.all_programs(), [])
        self.assertEqual(self.store.all_setup_audit(), [])

    def test_batch_and_entity_audits_share_batch_id(self):
        result = self.api.commit_hierarchy_import(
            fx.full_payload(), actor_id="admin")
        self.assertTrue(result["committed"])
        audits = self.store.all_setup_audit()
        batch = next(a for a in audits if a.action == "import_committed")
        self.assertEqual(batch.actor_id, "admin")
        self.assertEqual(batch.detail["import_type"], "hierarchy")
        self.assertEqual(batch.detail["created"], 16)
        entity_rows = [a for a in audits if a.entity_type != "import_batch"]
        self.assertEqual(len(entity_rows), 16)
        self.assertTrue(all(a.detail["import_batch_id"] == batch.entity_id
                            for a in entity_rows))

    # -- optional Club / Division (#260 review decision 1) ----------------
    def test_optional_club_and_division_never_fabricated(self):
        result = self.api.commit_hierarchy_import(fx.full_payload(
            clubs_csv="",
            permanent_teams_csv=fx.permanent_teams_csv(
                rows=("OVER55,LIONS,Lions,",)),
            competition_csv=fx.competition_csv(rows=(
                "OVER55,FALL26,Fall 2026,L1,Adult League,1,,,",)),
            registrations_csv=fx.registrations_csv(rows=("FALL26,LIONS,L1,",)),
        ), actor_id="admin")
        self.assertTrue(result["committed"], result.get("errors"))
        self.assertEqual(self.store.all_clubs(), [])
        lions = by_ref(self.store.all_teams(), "LIONS")
        self.assertIsNone(lions.club_id)
        self.assertEqual(self.store.all_divisions(), [])
        season = by_ref(self.store.all_seasons(), "FALL26")
        reg = self.store.registration_for_team_in_season(season.id, lions.id)
        self.assertIsNone(reg.division_id)

    def test_repeat_import_with_blank_club_code_unassigns_club(self):
        # A blank club_code on a REPEAT row genuinely unassigns a previously
        # set club — never a placeholder Club, but a real unassign is
        # allowed on re-import (#260 review decision 1).
        self.api.commit_hierarchy_import(fx.full_payload(), actor_id="admin")
        club = by_ref(self.store.all_clubs(), "EAGLES")
        lions = by_ref(self.store.all_teams(), "LIONS")
        self.assertEqual(lions.club_id, club.id)
        result = self.api.commit_hierarchy_import(fx.full_payload(
            permanent_teams_csv=fx.permanent_teams_csv(
                rows=("OVER55,LIONS,Lions,",))
        ), actor_id="admin")
        self.assertTrue(result["committed"], result.get("errors"))
        self.assertIsNone(by_ref(self.store.all_teams(), "LIONS").club_id)

    # -- Player upsert + optional email (#260) -----------------------------
    def test_player_email_omitted_on_repeat_preserves_existing_contact(self):
        self.api.commit_hierarchy_import(fx.full_payload(), actor_id="admin")
        player = by_ref(self.store.all_players(), "P001")
        result = self.api.commit_hierarchy_import(fx.full_payload(
            players_csv=fx.players_csv(rows=(
                "P001,LIONS,Jane,Smith,7,forward,",))
        ), actor_id="admin")
        self.assertTrue(result["committed"], result.get("errors"))
        from hockey_scheduler.domain import NotificationChannel
        contact = self.store.get_contact_destination(
            f"player:{player.id}", NotificationChannel.EMAIL)
        self.assertIsNotNone(contact)
        self.assertEqual(contact.destination, "jane@example.com")

    def test_player_email_change_updates_contact_in_place_no_duplicate(self):
        self.api.commit_hierarchy_import(fx.full_payload(), actor_id="admin")
        player = by_ref(self.store.all_players(), "P001")
        self.api.commit_hierarchy_import(fx.full_payload(
            players_csv=fx.players_csv(rows=(
                "P001,LIONS,Jane,Smith,7,forward,jane.new@example.com",))
        ), actor_id="admin")
        from hockey_scheduler.domain import NotificationChannel
        contacts = [c for c in self.store.all_contact_destinations()
                   if c.recipient_ref == f"player:{player.id}"
                   and c.channel == NotificationChannel.EMAIL]
        self.assertEqual(len(contacts), 1)
        self.assertEqual(contacts[0].destination, "jane.new@example.com")

    # -- registrations: explicit league_code (#260 review decision 2) ------
    def test_registration_missing_league_code_gets_clear_diagnostic(self):
        result = self.api.get_hierarchy_import_dry_run(fx.full_payload(
            registrations_csv="season_code,team_code\nFALL26,LIONS\n"))
        self.assertFalse(result["ok"])
        err = next(e for e in result["errors"]
                  if e.get("field") == "league_code")
        self.assertEqual(err["code"], "league_code_required")
        self.assertIn("season_code,team_code,league_code,division_code",
                      err["message"])

    def test_registration_league_must_belong_to_season(self):
        two_leagues = fx.competition_csv(rows=(
            "OVER55,FALL26,Fall 2026,L1,Adult League,1,DIVA,Division A,Adult",
            "OVER55,SPRING,Spring 2026,L2,Spring League,1,,,"))
        result = self.api.get_hierarchy_import_dry_run(fx.full_payload(
            competition_csv=two_leagues,
            registrations_csv=fx.registrations_csv(
                rows=("FALL26,LIONS,L2,DIVA",))))
        self.assertFalse(result["ok"])
        self.assertTrue(any(e["code"] == "league_season_mismatch"
                            for e in result["errors"]))

    # -- season venue access: create/reactivate/revoke (#260, new) ---------
    def test_season_venue_access_create_reactivate_revoke(self):
        self.api.commit_hierarchy_import(fx.full_payload(), actor_id="admin")
        season = by_ref(self.store.all_seasons(), "FALL26")
        venue = by_ref(self.store.all_venues(), "PLAINFIELD")
        access = self.store.season_venue_access_for_pair(season.id, venue.id)
        self.assertTrue(access.active)

        revoke = self.api.commit_hierarchy_import(fx.full_payload(
            season_venue_access_csv=fx.season_venue_access_csv(
                rows=("FALL26,PLAINFIELD,false",))
        ), actor_id="admin")
        self.assertTrue(revoke["committed"], revoke.get("errors"))
        self.assertEqual(revoke["summary"]["season_venue_access"]["updated"], 1)
        self.assertFalse(self.store.season_venue_access_for_pair(
            season.id, venue.id).active)

        reactivate = self.api.commit_hierarchy_import(fx.full_payload(),
                                                       actor_id="admin")
        self.assertTrue(reactivate["committed"])
        self.assertEqual(
            reactivate["summary"]["season_venue_access"]["updated"], 1)
        self.assertTrue(self.store.season_venue_access_for_pair(
            season.id, venue.id).active)

    def test_season_venue_access_revoke_of_nonexistent_pair_is_noop_warning(self):
        # Commit everything EXCEPT season_venue_access first, so FALL26 and
        # PLAINFIELD already exist in the store with no access grant between
        # them — only then does a revoke-of-nothing dry run see them as a
        # genuinely non-existent pair.
        self.assertTrue(self.api.commit_hierarchy_import(fx.full_payload(
            season_venue_access_csv=""), actor_id="admin")["committed"])

        result = self.api.get_hierarchy_import_dry_run({
            "import_type": "hierarchy",
            "season_venue_access_csv": fx.season_venue_access_csv(
                rows=("FALL26,PLAINFIELD,false",))})
        self.assertTrue(result["ok"])
        self.assertTrue(any(w["code"] == "revoke_noop"
                            for w in result["warnings"]))

        commit = self.api.commit_hierarchy_import({
            "import_type": "hierarchy",
            "season_venue_access_csv": fx.season_venue_access_csv(
                rows=("FALL26,PLAINFIELD,false",))
        }, actor_id="admin")
        self.assertTrue(commit["committed"], commit.get("errors"))
        self.assertEqual(
            commit["summary"]["season_venue_access"]["skipped"], 1)
        self.assertEqual(self.store.all_season_venue_access(), [])

    # -- shared Venue across Seasons/Programs (#233 Slice E, #260) ---------
    def test_venue_is_owned_only_by_organization_never_by_program(self):
        # A second Program in the same Organization can reuse the same
        # Venue via its own season_venue_access row — Venue carries no
        # program/league column at all.
        self.api.commit_hierarchy_import(fx.full_payload(), actor_id="admin")
        second = self.api.commit_hierarchy_import({
            "import_type": "hierarchy",
            "programs_csv": fx.programs_csv(rows=(
                "YOUTH,CANLON,Youth,US,America/Chicago",)),
            "competition_csv": fx.competition_csv(rows=(
                "YOUTH,WINTER26,Winter 2026,Y1,Youth League,1,,,",)),
            "season_venue_access_csv": "season_code,venue_code,active\n"
                                       "WINTER26,PLAINFIELD,true\n",
        }, actor_id="admin")
        self.assertTrue(second["committed"], second.get("errors"))
        venue = by_ref(self.store.all_venues(), "PLAINFIELD")
        self.assertIsNone(venue.league_id)
        winter = by_ref(self.store.all_seasons(), "WINTER26")
        self.assertTrue(self.store.season_venue_access_for_pair(
            winter.id, venue.id).active)
        fall = by_ref(self.store.all_seasons(), "FALL26")
        self.assertTrue(self.store.season_venue_access_for_pair(
            fall.id, venue.id).active)


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
        result = api.commit_hierarchy_import(fx.full_payload(), actor_id="admin")
        self.assertTrue(result["committed"], result.get("errors"))
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
            fx.full_payload(dry_run=True))
        self.assertEqual(status, 200)
        self.assertTrue(preview["ok"], preview.get("errors"))
        self.assertEqual(
            len(self.srv.STATE.api.store.all_organizations()), before)

        status, committed = self.request(
            client, "/api/import/commit/teams-players", fx.full_payload())
        self.assertEqual(status, 200)
        self.assertTrue(committed["committed"], committed.get("errors"))

    def test_arena_manager_is_forbidden(self):
        client = self.client()
        self.login(client, "arena")
        status, body = self.request(
            client, "/api/import/commit/teams-players",
            fx.full_payload(dry_run=True))
        self.assertEqual(status, 403)
        self.assertEqual(body["error"]["details"]["required"], "manage_setup")

    def test_forged_actor_and_secret_fields_are_ignored(self):
        admin_id = self.srv.STATE.api.verify_login("admin", "demo")["id"]
        client = self.client()
        self.login(client, "admin")
        body = fx.full_payload(actor_id="attacker", password="plaintext-secret")
        status, response = self.request(
            client, "/api/import/commit/teams-players", body)
        self.assertEqual(status, 200)
        self.assertTrue(response["committed"], response.get("errors"))
        blob = json.dumps(response)
        self.assertNotIn("attacker", blob)
        self.assertNotIn("plaintext-secret", blob)
        batch = [a for a in self.srv.STATE.api.store.all_setup_audit()
                 if a.action == "import_committed"][-1]
        self.assertEqual(batch.actor_id, admin_id)
        self.assertNotIn("plaintext-secret", json.dumps(batch.detail))


class LegacyPlayersCsvRoutingRegressionTest(unittest.TestCase):
    """#260 regression: the hierarchy `players` sheet's CSV key
    (``players_csv``) is IDENTICAL to the legacy teams+players importer's
    own ``players_csv`` key. Only an explicit ``import_type: "hierarchy"``
    may route a submission into the hierarchy importer — the mere presence
    of ``players_csv`` must never do so, or a legacy (non-hierarchy)
    teams+players submission would be misrouted and rejected as "mixed
    with existing onboarding sheets".
    """

    def setUp(self):
        self.store = InMemoryStore()
        self.api = ApiService(self.store)
        hierarchy = self.api.commit_hierarchy_import(fx.full_payload(
            clubs_csv="", permanent_teams_csv="", players_csv="",
            registrations_csv="", season_venue_access_csv=""),
            actor_id="admin")
        self.assertTrue(hierarchy["committed"], hierarchy.get("errors"))
        self.season_id = by_ref(self.store.all_seasons(), "FALL26").id

    def test_legacy_teams_players_submission_commits_without_hierarchy_error(self):
        legacy_body = {
            "teams_csv": (
                "team_code,team_name,club_name,division_name\n"
                "SHARKS,Sharks,Shark Club,U16\n"),
            "players_csv": (
                "player_code,first_name,last_name,team_code,jersey_number,"
                "position,email\nP9,Lee,Park,SHARKS,9,forward,\n"),
        }
        result = self.api.commit_teams_players_import(
            self.season_id, legacy_body, actor_id="admin")
        self.assertNotIn("error", result)
        legacy_team = next(
            t for t in self.store.all_teams() if t.external_ref == "SHARKS")
        self.assertIsNotNone(legacy_team)


if __name__ == "__main__":
    unittest.main()
