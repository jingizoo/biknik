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
from hockey_scheduler.domain import NotificationChannel, Season
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

NO_CLUB_TEAMS_CSV = (
    "team_code,team_name,club_name,division_name\n"
    "N1,No Club Blank,,U16\n"
    "N2,No Club NA,NA,U16\n"
    "N3,No Club na lower,na,U16\n"
    "N4,Has Club,Real Club,U16\n"
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
        league = self.setup.create_program("Test League", actor_id="admin")
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

    # -- #180: import converges Team league_id + season registration --------
    def test_import_sets_team_league_and_active_registration(self):
        res = self.api.commit_teams_players_import(
            self.season.id, _valid_sheets_csv(), actor_id="admin")
        self.assertTrue(res["committed"])
        season = self.store.get_season(self.season.id)
        t1 = self._team("T1")
        # Permanent league is the imported season's league; NO legacy division.
        self.assertEqual(t1.program_id, season.program_id)
        self.assertIsNone(t1.division_id)
        # An active registration ties T1 to the season + its imported division,
        # so the imported team is immediately schedulable (not teams_without_league).
        reg = self.store.registration_for_team_in_season(self.season.id, t1.id)
        self.assertIsNotNone(reg)
        self.assertTrue(reg.active)
        u16 = next(d for d in self.store.all_divisions()
                   if d.name == "U16" and d.season_id == self.season.id)
        self.assertEqual(reg.division_id, u16.id)

    def test_import_registration_is_idempotent(self):
        self.api.commit_teams_players_import(
            self.season.id, _valid_sheets_csv(), actor_id="admin")
        t1 = self._team("T1")
        before = [r for r in self.store.all_season_team_registrations()
                  if r.team_id == t1.id]
        self.api.commit_teams_players_import(
            self.season.id, _valid_sheets_csv(), actor_id="admin")
        after = [r for r in self.store.all_season_team_registrations()
                 if r.team_id == t1.id]
        self.assertEqual(len(before), 1)
        self.assertEqual(len(after), 1)  # updated in place, never duplicated

    def test_import_into_second_season_leaves_first_registration_untouched(self):
        self.api.commit_teams_players_import(
            self.season.id, _valid_sheets_csv(), actor_id="admin")
        t1 = self._team("T1")
        first = self.store.registration_for_team_in_season(self.season.id, t1.id)
        first_div = first.division_id
        league_id = self.store.get_season(self.season.id).program_id
        season2 = self.setup.create_season(league_id, "2027", actor_id="admin")
        self.api.commit_teams_players_import(
            season2.id, _valid_sheets_csv(), actor_id="admin")
        # Season 1's registration is unchanged…
        again = self.store.registration_for_team_in_season(self.season.id, t1.id)
        self.assertEqual(again.division_id, first_div)
        self.assertTrue(again.active)
        # …and a distinct registration now exists for season 2.
        second = self.store.registration_for_team_in_season(season2.id, t1.id)
        self.assertIsNotNone(second)
        self.assertNotEqual(second.id, first.id)

    # -- #180 review: import integrity gate --------------------------------
    def test_import_does_not_re_home_a_team_across_leagues(self):
        # First import establishes T1's permanent league.
        self.api.commit_teams_players_import(
            self.season.id, _valid_sheets_csv(), actor_id="admin")
        t1 = self._team("T1")
        league1 = self.store.get_season(self.season.id).program_id
        self.assertEqual(t1.program_id, league1)
        # Re-import the SAME team_code into a DIFFERENT league's season.
        league2 = self.setup.create_program("Other League", actor_id="admin").id
        season2 = self.setup.create_season(league2, "2027", actor_id="admin")
        res = self.api.commit_teams_players_import(
            season2.id, _valid_sheets_csv(), actor_id="admin")
        self.assertFalse(res["committed"])
        self.assertTrue(any(e["reason"] == "team_league_move_blocked"
                            for e in res["errors"]))
        # Zero writes: T1 keeps its league; no registration was made in season2.
        self.assertEqual(self._team("T1").program_id, league1)
        self.assertIsNone(
            self.store.registration_for_team_in_season(season2.id, t1.id))

    def test_import_rejects_a_season_without_a_valid_league(self):
        orphan = Season(id=self.store.next_id("season"), program_id=None,
                        name="Orphan")
        self.store.add_season(orphan)
        res = self.api.commit_teams_players_import(
            orphan.id, _valid_sheets_csv(), actor_id="admin")
        self.assertFalse(res["committed"])
        self.assertTrue(any(e["reason"] == "season_league_missing"
                            for e in res["errors"]))
        self.assertEqual(self.store.all_teams(), [])  # zero writes

    def _u16(self):
        return next(d for d in self.store.all_divisions()
                    if d.name == "U16" and d.season_id == self.season.id)

    def _t1_with_committed_game(self):
        """Import, then a committed U16 game for T1. Returns (t1, u16, game_id)."""
        self.api.commit_teams_players_import(
            self.season.id, _valid_sheets_csv(), actor_id="admin")
        t1 = self._team("T1")
        u16 = self._u16()
        league = self.store.get_season(self.season.id).program_id
        club2 = self.setup.create_club("C2", actor_id="admin").id
        mate = self.api.create_team(club2, u16.id, "Mate", actor_id="admin")["id"]
        self.api.register_team_for_season(
            self.season.id, mate, u16.id, actor_id="admin")
        venue = self.setup.create_venue("V", league_id=league, actor_id="admin").id
        self.setup.grant_season_venue_access(self.season.id, venue, actor_id="admin")
        rink = self.setup.create_rink(venue, "R", actor_id="admin").id
        slot = self.api.create_ice_slot(
            rink, "2027-06-01T18:00:00+00:00", "2027-06-01T19:00:00+00:00",
            actor_id="admin")["id"]
        game = self.api.create_game(
            self.season.id, u16.id, t1.id, mate, slot, actor_id="admin")
        self.assertNotIn("error", game)
        return t1, u16, game["id"]

    def _reimport(self, division_value):
        moved = TEAMS_CSV.replace(
            "T1,Team One,Lions Club,U16",
            f"T1,Team One,Lions Club,{division_value}")
        return self.api.commit_teams_players_import(
            self.season.id, {"teams_csv": moved, "players_csv": PLAYERS_CSV},
            actor_id="admin")

    def test_import_division_move_stranding_a_committed_game_is_rejected(self):
        t1, u16, game_id = self._t1_with_committed_game()
        res = self._reimport("U20")  # move T1 to a different named division
        self.assertFalse(res["committed"])
        err = next(e for e in res["errors"]
                   if e["reason"] == "registration_division_move_strands_games")
        self.assertIn(game_id, err["affected_game_ids"])
        reg = self.store.registration_for_team_in_season(self.season.id, t1.id)
        self.assertEqual(reg.division_id, u16.id)  # zero writes

    def test_import_clearing_division_with_committed_game_is_rejected(self):
        # A BLANK division would clear the registration — must hit the same
        # game-safety guard (#180 review, blank-to-unassigned path).
        t1, u16, game_id = self._t1_with_committed_game()
        res = self._reimport("")  # blank division_name
        self.assertFalse(res["committed"])
        err = next(e for e in res["errors"]
                   if e["reason"] == "registration_division_move_strands_games")
        self.assertIn(game_id, err["affected_game_ids"])
        reg = self.store.registration_for_team_in_season(self.season.id, t1.id)
        self.assertEqual(reg.division_id, u16.id)  # zero writes, still assigned
        self.assertTrue(reg.active)

    def test_import_moving_inactive_registration_with_committed_game_is_rejected(self):
        # An inactive/historical registration + a committed (non-cancelled)
        # game: a re-import that reactivates it into a different (or blank)
        # division still strands the game and is rejected.
        t1, u16, game_id = self._t1_with_committed_game()
        reg = self.store.registration_for_team_in_season(self.season.id, t1.id)
        reg.active = False
        self.store.save_season_team_registration(reg)
        res = self._reimport("")  # blank while inactive
        self.assertFalse(res["committed"])
        err = next(e for e in res["errors"]
                   if e["reason"] == "registration_division_move_strands_games")
        self.assertIn(game_id, err["affected_game_ids"])
        again = self.store.registration_for_team_in_season(self.season.id, t1.id)
        self.assertEqual(again.division_id, u16.id)  # zero writes
        self.assertFalse(again.active)

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

    # -- 3b. blank/NA club_name means no Club, never a placeholder (#233 D) --
    def test_blank_and_na_club_name_creates_no_club(self):
        res = self.api.commit_teams_players_import(
            self.season.id, {"teams_csv": NO_CLUB_TEAMS_CSV}, actor_id="admin")
        self.assertNotIn("error", res)
        self.assertTrue(res["committed"])
        # Only "Real Club" is created — blank/NA/na never become a Club row.
        self.assertEqual(res["summary"]["clubs_created"], 1)
        clubs = self.store.all_clubs()
        self.assertEqual(len(clubs), 1)
        self.assertEqual(clubs[0].name, "Real Club")
        self.assertIsNone(self._team("N1").club_id)
        self.assertIsNone(self._team("N2").club_id)
        self.assertIsNone(self._team("N3").club_id)
        self.assertEqual(self._team("N4").club_id, clubs[0].id)

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
        # #102: the batch summary tags its own type, and every row-level
        # entry this commit wrote is tagged back to this SAME batch id — the
        # link the Activity feed's drill-down groups rows by.
        self.assertEqual(batches[0].detail["import_type"], "teams_players")
        batch_id = batches[0].entity_id
        row_entries = [a for a in self.store.all_setup_audit()
                      if a.action in ("team_created", "player_added")]
        self.assertEqual(len(row_entries), 5)
        for a in row_entries:
            self.assertEqual(a.detail["import_batch_id"], batch_id)

    # #102: get_demo_overview()'s setup_audit serialization must carry
    # actor_id/detail so the Activity feed can group + label batches — it
    # used to drop both, silently making import_committed indistinguishable
    # from any other setup action in the API response.
    def test_overview_serializes_actor_id_and_detail(self):
        self.api.commit_teams_players_import(
            self.season.id, _valid_sheets_csv(), actor_id="admin")
        ov = self.api.get_demo_overview()
        batch = next(a for a in ov["setup_audit"] if a["action"] == "import_committed")
        self.assertEqual(batch["actor_id"], "admin")
        self.assertEqual(batch["detail"]["import_type"], "teams_players")
        self.assertEqual(batch["detail"]["teams_created"], 2)
        team_row = next(a for a in ov["setup_audit"] if a["action"] == "team_created")
        self.assertEqual(team_row["detail"]["import_batch_id"], batch["entity_id"])

    # #102 review fix: /api/demo/overview is UNAUTHENTICATED (do_GET in
    # web/server.py serves it with no session/permission check), so
    # actor_id/detail must stay scoped to import-batch entries only — NOT
    # leak for every setup-audit action. user_account_created's detail
    # stores {"username", "role"}; confirm it's never exposed here, even
    # though import batches on the SAME overview response do carry theirs.
    def test_overview_omits_detail_for_non_import_audit_entries(self):
        from hockey_scheduler.services.account_service import AccountService
        AccountService(self.store).create_account(
            "sidefx", "hunter2", "viewer", actor_id="admin")
        self.api.commit_teams_players_import(
            self.season.id, _valid_sheets_csv(), actor_id="admin")
        ov = self.api.get_demo_overview()
        account_row = next(a for a in ov["setup_audit"]
                           if a["action"] == "user_account_created")
        self.assertNotIn("detail", account_row)
        self.assertNotIn("actor_id", account_row)
        blob = json.dumps(ov["setup_audit"])
        self.assertNotIn("sidefx", blob)
        self.assertNotIn("hunter2", blob)
        # Import batch entries on the SAME response are unaffected by the fix.
        batch = next(a for a in ov["setup_audit"] if a["action"] == "import_committed")
        self.assertIn("detail", batch)
        self.assertIn("actor_id", batch)

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
        # #102: the two commits must produce two DISTINCT batch ids, and each
        # repeat commit's own row-level entries must reference the SECOND
        # (most recent) batch, not bleed into the first commit's batch.
        batches = [a for a in self.store.all_setup_audit()
                  if a.action == "import_committed"]
        self.assertNotEqual(batches[0].entity_id, batches[1].entity_id)
        second_batch_id = batches[1].entity_id
        second_updates = [a for a in self.store.all_setup_audit()
                          if a.action in ("team_updated", "player_updated")]
        self.assertEqual(len(second_updates), 5)
        for a in second_updates:
            self.assertEqual(a.detail["import_batch_id"], second_batch_id)

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
        league = setup.create_program("Test League", actor_id="admin")
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
