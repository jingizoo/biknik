"""CSV import teams + players COMMIT (#93).

Step 2 of the Pilot Onboarding Import Wizard: takes the same CSV shapes #92's
dry-run validator accepts, re-validates via the SAME pure ``validate_import``
gate, and — only if that gate is clean — writes teams/players (and any
find-or-created clubs/divisions) inside a single transaction. Officials,
rinks, and ice slots commit are out of scope here (#94/#95).
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

import hierarchy_fixtures as fx
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
        # #283: teams register into a Season through a LeagueSeason, so the
        # import target Season needs a League bound to it (the nine-sheet
        # hierarchy import always establishes one via the competition sheet;
        # this two-sheet teams+players import expects it to pre-exist).
        self.setup.create_league(self.season.id, "L1", actor_id="admin")

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
        u16 = self._u16()
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
        self.setup.create_league(season2.id, "L1", actor_id="admin")
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
        # #283: a Division's Season is reached through its LeagueSeason.
        return next(
            d for d in self.store.all_divisions()
            if d.name == "U16"
            and (ls := self.store.get_league_season(d.league_season_id))
            and ls.season_id == self.season.id)

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


@unittest.skipUnless(os.environ.get("TEST_DATABASE_URL"),
                     "PostgreSQL required (set TEST_DATABASE_URL)")
class PostgresTeamsPlayersImportRaceTest(unittest.TestCase):
    """#331 review round 13 finding 2: commit_teams_players_import's Club
    find-or-create had the same unlocked check-then-create shape round 12
    fixed for the officials/rinks imports, but was never touched -- and
    only this method's OWN target Season is locked, so two imports for
    DIFFERENT Seasons never serialize against each other here. club_name,
    team_code, and player_code are all documented as globally matched (not
    Season-scoped), so each could see an identical new value absent and
    create a duplicate. Each thread drives its OWN
    ApiService(SqlStore(...)) -- a separate connection and process-local
    RLock -- so passing here depends on the real cross-connection
    next_id() lock the fix now participates in, not the in-process lock a
    shared store instance would provide for free."""

    def setUp(self):
        self.url = os.environ["TEST_DATABASE_URL"]
        SqlStore(self.url).clear_all_data()

    def _seed_two_seasons(self, seed_store):
        seed_setup = SetupService(seed_store)
        program = seed_setup.create_program("Race League", actor_id="admin")
        season_a = seed_setup.create_season(program.id, "Season A", actor_id="admin")
        seed_setup.create_league(season_a.id, "LA", actor_id="admin")
        season_b = seed_setup.create_season(program.id, "Season B", actor_id="admin")
        seed_setup.create_league(season_b.id, "LB", actor_id="admin")
        return season_a, season_b

    def _pausing(self, store, prefix_to_pause, barrier):
        real = store.next_id
        def _wrapped(prefix):
            if prefix == prefix_to_pause:
                barrier.wait(timeout=10)
            return real(prefix)
        return _wrapped

    def test_identical_new_club_name_across_seasons_does_not_duplicate(self):
        seed_store = SqlStore(self.url)
        season_a, season_b = self._seed_two_seasons(seed_store)

        store_a, store_b = SqlStore(self.url), SqlStore(self.url)
        api_a, api_b = ApiService(store_a), ApiService(store_b)

        # Distinct team_codes so the Team side never contends -- isolating
        # the Club race specifically, same design as the officials/rinks
        # Club and Venue races in round 12.
        barrier = threading.Barrier(2)
        store_a.next_id = self._pausing(store_a, "club", barrier)
        store_b.next_id = self._pausing(store_b, "club", barrier)

        a_csv = "team_code,team_name,club_name\nRACE_TA,Team A,Race Club\n"
        b_csv = "team_code,team_name,club_name\nRACE_TB,Team B,Race Club\n"

        results = {}

        def run(api, season, csv, key):
            try:
                results[key] = api.commit_teams_players_import(
                    season.id, {"teams_csv": csv}, actor_id=key)
            except Exception as exc:
                results[key] = exc

        ta = threading.Thread(target=run, args=(api_a, season_a, a_csv, "a"))
        tb = threading.Thread(target=run, args=(api_b, season_b, b_csv, "b"))
        ta.start(); tb.start()
        ta.join(20); tb.join(20)

        self.assertFalse(ta.is_alive(), "thread a hung")
        self.assertFalse(tb.is_alive(), "thread b hung")
        for key, res in results.items():
            self.assertNotIsInstance(
                res, Exception,
                f"commit {key} raised instead of committing cleanly: {res!r}")
            self.assertTrue(res.get("committed"), f"commit {key}: {res}")

        fresh = SqlStore(self.url)
        clubs = [c for c in fresh.all_clubs() if c.name == "Race Club"]
        self.assertEqual(
            len(clubs), 1,
            f"expected exactly one Club named 'Race Club', got {[c.id for c in clubs]}")
        team_a = next(t for t in fresh.all_teams() if t.external_ref == "RACE_TA")
        team_b = next(t for t in fresh.all_teams() if t.external_ref == "RACE_TB")
        self.assertEqual(team_a.club_id, clubs[0].id,
                         "team A must point at the surviving Club, not an orphan")
        self.assertEqual(team_b.club_id, clubs[0].id,
                         "team B must point at the surviving Club, not an orphan")

    def test_identical_global_team_and_player_codes_across_seasons_the_loser_is_rejected(self):
        """#331 review round 14 finding 2: this test's own PRIOR assertion --
        that both commits succeed, leaving the SAME Team registered under
        BOTH Season A's permanent League (LA) and Season B's (LB) -- was
        itself the exact bug the reviewer caught. _seed_two_seasons binds
        Season A/B to distinct permanent Leagues under one shared Program,
        so Team.program_id agrees for both sides while Team.league_id does
        not: the loser's late double-checked-locking recheck used to adopt
        the winner's Team and write a registration into ITS OWN (different)
        League's LeagueSeason regardless, violating the canonical invariant
        Team.league_id == registration.league_season.league_id. The correct
        outcome is exactly one winner (whichever actually created the Team,
        under its own permanent League) and the loser rejected atomically
        with team_permanent_league_move_blocked -- no registration, no
        duplicate Team/Player, and the Team's league_id never silently
        moves to match the loser instead."""
        seed_store = SqlStore(self.url)
        season_a, season_b = self._seed_two_seasons(seed_store)

        store_a, store_b = SqlStore(self.url), SqlStore(self.url)
        api_a, api_b = ApiService(store_a), ApiService(store_b)

        barrier = threading.Barrier(2)
        store_a.next_id = self._pausing(store_a, "team", barrier)
        store_b.next_id = self._pausing(store_b, "team", barrier)

        race_teams_csv = "team_code,team_name\nRACE_TEAM,Race Team\n"
        race_players_csv = (
            "player_code,first_name,last_name,team_code,jersey_number,position\n"
            "RACE_PLAYER,Race,Player,RACE_TEAM,7,forward\n"
        )

        results = {}

        def run(api, season, key):
            try:
                results[key] = api.commit_teams_players_import(
                    season.id,
                    {"teams_csv": race_teams_csv, "players_csv": race_players_csv},
                    actor_id=key)
            except Exception as exc:
                results[key] = exc

        ta = threading.Thread(target=run, args=(api_a, season_a, "a"))
        tb = threading.Thread(target=run, args=(api_b, season_b, "b"))
        ta.start(); tb.start()
        ta.join(20); tb.join(20)

        self.assertFalse(ta.is_alive(), "thread a hung")
        self.assertFalse(tb.is_alive(), "thread b hung")
        for key, res in results.items():
            self.assertNotIsInstance(
                res, Exception,
                f"commit {key} raised instead of returning a structured result: {res!r}")

        winner_key = next((k for k, r in results.items() if r.get("committed")), None)
        self.assertIsNotNone(
            winner_key, f"expected exactly one side to commit, got: {results}")
        loser_key = "b" if winner_key == "a" else "a"
        self.assertFalse(
            results[loser_key].get("committed"),
            f"expected the OTHER side to be atomically rejected, not also "
            f"commit into a different permanent league: {results}")
        self.assertEqual(
            results[loser_key]["errors"][0]["reason"],
            "team_permanent_league_move_blocked", results[loser_key])

        winner_season = season_a if winner_key == "a" else season_b
        loser_season = season_b if winner_key == "a" else season_a
        winner_league_name = "LA" if winner_key == "a" else "LB"

        fresh = SqlStore(self.url)
        teams = [t for t in fresh.all_teams() if t.external_ref == "RACE_TEAM"]
        self.assertEqual(
            len(teams), 1,
            f"expected exactly one Team for RACE_TEAM, got {[t.id for t in teams]}")
        players = [p for p in fresh.all_players() if p.external_ref == "RACE_PLAYER"]
        self.assertEqual(
            len(players), 1,
            f"expected exactly one Player for RACE_PLAYER, got {[p.id for p in players]}")
        self.assertEqual(players[0].team_id, teams[0].id)

        winner_league = next(l for l in fresh.all_leagues()
                             if l.name == winner_league_name)
        self.assertEqual(
            teams[0].league_id, winner_league.id,
            "the Team's permanent league must match the WINNER's season, "
            "never silently move to the loser's")

        reg_winner = next((r for r in fresh.registrations_for_season(winner_season.id)
                           if r.team_id == teams[0].id), None)
        reg_loser = next((r for r in fresh.registrations_for_season(loser_season.id)
                          if r.team_id == teams[0].id), None)
        self.assertIsNotNone(reg_winner, "the winner's season must have its "
                                         "own registration for the surviving Team")
        self.assertIsNone(reg_loser, "the loser must not have written a "
                                     "registration into the wrong permanent league")
        # An isolated "Team pre-existing for both, only Player races" variant
        # (mirroring the officials-availability window-only split in round
        # 11) was attempted and deliberately dropped: every audit row this
        # method writes -- including the UNCONDITIONAL team_updated audit a
        # pre-existing Team always gets -- shares ONE global next_id
        # ("setupaudit") counter. Two concurrent commits that both reference
        # an existing Team therefore already fully serialize against each
        # other at that EARLIER point (whichever side's team_updated audit
        # loses the counter race blocks, at the DB level, until the other's
        # entire transaction -- Team AND Player -- commits), so by the time
        # either side would reach the Player step, the race is already over
        # and the Player-specific double-check is never genuinely exercised.
        # Attempting to force it anyway (pausing at next_id("player")) hits
        # exactly the self-inflicted circular-wait class this file's own
        # next_id()-pausing tests take care to avoid: one side ends up
        # holding the setupaudit row open while blocked on the Python
        # barrier, and the other blocks on that row before ever reaching the
        # barrier. The Player-level fix (see setup_service.py) still
        # verifiably prevents duplication where it CAN be exercised --
        # exactly the combined scenario this test proves.

    def test_identical_team_code_across_different_programs_the_loser_is_rejected(self):
        """#331 review round 14 finding 2, second required scenario: distinct
        PROGRAMS entirely, not just distinct permanent Leagues under one
        shared Program -- the worse case the reviewer separately called
        out, since the pre-fix code's loser recheck overwrote
        Team.program_id UNCONDITIONALLY with no re-run of
        team_league_move_blocked at all. The Program-level (1) gate fires
        first here (existing.program_id differs), before the League-level
        (1b) gate this file's other new test exercises."""
        seed_store = SqlStore(self.url)
        seed_setup = SetupService(seed_store)
        program_x = seed_setup.create_program("Program X", actor_id="admin")
        season_x = seed_setup.create_season(program_x.id, "Season X", actor_id="admin")
        seed_setup.create_league(season_x.id, "LX", actor_id="admin")
        program_y = seed_setup.create_program("Program Y", actor_id="admin")
        season_y = seed_setup.create_season(program_y.id, "Season Y", actor_id="admin")
        seed_setup.create_league(season_y.id, "LY", actor_id="admin")

        store_a, store_b = SqlStore(self.url), SqlStore(self.url)
        api_a, api_b = ApiService(store_a), ApiService(store_b)

        barrier = threading.Barrier(2)
        store_a.next_id = self._pausing(store_a, "team", barrier)
        store_b.next_id = self._pausing(store_b, "team", barrier)

        race_teams_csv = "team_code,team_name\nRACE_XY,Race XY Team\n"

        results = {}

        def run(api, season, key):
            try:
                results[key] = api.commit_teams_players_import(
                    season.id, {"teams_csv": race_teams_csv}, actor_id=key)
            except Exception as exc:
                results[key] = exc

        ta = threading.Thread(target=run, args=(api_a, season_x, "a"))
        tb = threading.Thread(target=run, args=(api_b, season_y, "b"))
        ta.start(); tb.start()
        ta.join(20); tb.join(20)

        self.assertFalse(ta.is_alive(), "thread a hung")
        self.assertFalse(tb.is_alive(), "thread b hung")
        for key, res in results.items():
            self.assertNotIsInstance(
                res, Exception,
                f"commit {key} raised instead of returning a structured result: {res!r}")

        winner_key = next((k for k, r in results.items() if r.get("committed")), None)
        self.assertIsNotNone(
            winner_key, f"expected exactly one side to commit, got: {results}")
        loser_key = "b" if winner_key == "a" else "a"
        self.assertFalse(results[loser_key].get("committed"), results)
        self.assertEqual(
            results[loser_key]["errors"][0]["reason"], "team_league_move_blocked",
            f"the loser must be rejected as a Program move, writing nothing: "
            f"{results[loser_key]}")

        winner_program = program_x if winner_key == "a" else program_y
        winner_season = season_x if winner_key == "a" else season_y
        loser_season = season_y if winner_key == "a" else season_x

        fresh = SqlStore(self.url)
        teams = [t for t in fresh.all_teams() if t.external_ref == "RACE_XY"]
        self.assertEqual(len(teams), 1, teams)
        self.assertEqual(
            teams[0].program_id, winner_program.id,
            "the Team's Program must never silently move to the loser's Program")
        reg_winner = next((r for r in fresh.registrations_for_season(winner_season.id)
                           if r.team_id == teams[0].id), None)
        reg_loser = next((r for r in fresh.registrations_for_season(loser_season.id)
                          if r.team_id == teams[0].id), None)
        self.assertIsNotNone(reg_winner)
        self.assertIsNone(reg_loser, "the loser must not have written a "
                                     "registration across Programs")

    def test_shared_permanent_league_across_two_seasons_both_racing_imports_succeed(self):
        """Positive-case counterpart to the two rejection tests above,
        proving round 14's new League-level gate (1b) doesn't over-reject:
        ONE permanent League genuinely bound to BOTH racing Seasons (a
        real, supported topology -- create_league_season lets a League
        span multiple Seasons of its Program) must let both imports
        succeed, each with its own valid registration resolving to the
        one shared Team.league_id."""
        seed_store = SqlStore(self.url)
        seed_setup = SetupService(seed_store)
        program = seed_setup.create_program("Shared League Program", actor_id="admin")
        season_a = seed_setup.create_season(program.id, "Season A", actor_id="admin")
        season_b = seed_setup.create_season(program.id, "Season B", actor_id="admin")
        league = seed_setup.create_league(season_a.id, "Shared League", actor_id="admin")
        seed_setup.create_league_season(league.id, season_b.id, actor_id="admin")

        store_a, store_b = SqlStore(self.url), SqlStore(self.url)
        api_a, api_b = ApiService(store_a), ApiService(store_b)

        barrier = threading.Barrier(2)
        store_a.next_id = self._pausing(store_a, "team", barrier)
        store_b.next_id = self._pausing(store_b, "team", barrier)

        race_teams_csv = "team_code,team_name\nRACE_SHARED,Race Shared Team\n"

        results = {}

        def run(api, season, key):
            try:
                results[key] = api.commit_teams_players_import(
                    season.id, {"teams_csv": race_teams_csv}, actor_id=key)
            except Exception as exc:
                results[key] = exc

        ta = threading.Thread(target=run, args=(api_a, season_a, "a"))
        tb = threading.Thread(target=run, args=(api_b, season_b, "b"))
        ta.start(); tb.start()
        ta.join(20); tb.join(20)

        self.assertFalse(ta.is_alive(), "thread a hung")
        self.assertFalse(tb.is_alive(), "thread b hung")
        for key, res in results.items():
            self.assertNotIsInstance(
                res, Exception,
                f"commit {key} raised instead of committing cleanly: {res!r}")
            self.assertTrue(res.get("committed"), f"commit {key}: {res}")

        fresh = SqlStore(self.url)
        teams = [t for t in fresh.all_teams() if t.external_ref == "RACE_SHARED"]
        self.assertEqual(
            len(teams), 1,
            f"expected exactly one Team for RACE_SHARED, got {[t.id for t in teams]}")
        self.assertEqual(teams[0].league_id, league.id)

        reg_a = next((r for r in fresh.registrations_for_season(season_a.id)
                     if r.team_id == teams[0].id), None)
        reg_b = next((r for r in fresh.registrations_for_season(season_b.id)
                     if r.team_id == teams[0].id), None)
        self.assertIsNotNone(reg_a, "Season A must have its own registration")
        self.assertIsNotNone(reg_b, "Season B must have its own registration")
        self.assertEqual(fresh.get_league_season(reg_a.league_season_id).league_id,
                         teams[0].league_id)
        self.assertEqual(fresh.get_league_season(reg_b.league_season_id).league_id,
                         teams[0].league_id)

    def test_officials_and_teams_imports_share_the_same_club_resolver(self):
        """Proves both call sites participate in the SAME serialization
        point, not just that each is independently safe against its own
        kind -- an officials import and a teams/players import landing on
        the identical new club_name must still converge on one Club.
        Reverting either call site's use of _find_or_create_import_club
        (back to its own unprotected inline copy) must make this fail."""
        seed_store = SqlStore(self.url)
        seed_setup = SetupService(seed_store)
        program = seed_setup.create_program("Race League", actor_id="admin")
        season = seed_setup.create_season(program.id, "2026 Season", actor_id="admin")
        seed_setup.create_league(season.id, "L1", actor_id="admin")

        store_a, store_b = SqlStore(self.url), SqlStore(self.url)
        api_a, api_b = ApiService(store_a), ApiService(store_b)

        barrier = threading.Barrier(2)
        store_a.next_id = self._pausing(store_a, "club", barrier)
        store_b.next_id = self._pausing(store_b, "club", barrier)

        teams_csv = "team_code,team_name,club_name\nRACE_TX,Race Team,Cross Club\n"
        officials_csv = "official_code,name,home_club_name\nRACE_OX,Race Official,Cross Club\n"

        results = {}

        def run_teams():
            try:
                results["teams"] = api_a.commit_teams_players_import(
                    season.id, {"teams_csv": teams_csv}, actor_id="a")
            except Exception as exc:
                results["teams"] = exc

        def run_officials():
            try:
                results["officials"] = api_b.commit_officials_availability_import(
                    {"officials_csv": officials_csv}, actor_id="b")
            except Exception as exc:
                results["officials"] = exc

        ta = threading.Thread(target=run_teams)
        tb = threading.Thread(target=run_officials)
        ta.start(); tb.start()
        ta.join(20); tb.join(20)

        self.assertFalse(ta.is_alive(), "teams thread hung")
        self.assertFalse(tb.is_alive(), "officials thread hung")
        for key, res in results.items():
            self.assertNotIsInstance(
                res, Exception,
                f"commit {key} raised instead of committing cleanly: {res!r}")
            self.assertTrue(res.get("committed"), f"commit {key}: {res}")

        fresh = SqlStore(self.url)
        clubs = [c for c in fresh.all_clubs() if c.name == "Cross Club"]
        self.assertEqual(
            len(clubs), 1,
            f"expected exactly one Club named 'Cross Club', got {[c.id for c in clubs]}")
        team = next(t for t in fresh.all_teams() if t.external_ref == "RACE_TX")
        official = next(o for o in fresh.all_officials() if o.external_ref == "RACE_OX")
        self.assertEqual(team.club_id, clubs[0].id,
                         "the teams import must point at the surviving Club")
        self.assertEqual(official.home_club_id, clubs[0].id,
                         "the officials import must point at the surviving Club")


@unittest.skipUnless(os.environ.get("TEST_DATABASE_URL"),
                     "PostgreSQL required (set TEST_DATABASE_URL)")
class PostgresHierarchyVsPilotTeamPlayerRaceTest(unittest.TestCase):
    """#331 review round 14 finding 3: commit_hierarchy_import and
    commit_teams_players_import each resolve Team/Player external_ref
    against their OWN snapshot taken before their own write loop, with no
    shared lock across the two entry points until this round's fix
    (upsert_imported_team/upsert_imported_player's own reserve-then-recheck,
    caught as _HierarchyTeamOrPlayerDrifted and retried by
    commit_hierarchy_import; commit_teams_players_import's matching
    _TeamLockPlanDrifted check already covers ITS side, round 14 finding 2).
    Every test here races the TWO DIFFERENT entry points against each other
    (not two of the same one, which the class above already covers), in
    both winner directions, using a deterministic one-side-pauses/other-
    completes handshake rather than a symmetric barrier so each test
    reliably exercises the SPECIFIC recheck it names."""

    def setUp(self):
        self.url = os.environ["TEST_DATABASE_URL"]
        SqlStore(self.url).clear_all_data()

    def _seed_shared_league_and_team(self, seed_store, stable_team_code):
        """Program OVER55 / Season FALL26 (season_a) / League L1, all with
        real external_ref codes, PLUS a SECOND Season (season_b) the SAME
        permanent League L1 also participates in, PLUS one stable Team (for
        the Player races to attach to) -- seeded via the hierarchy importer
        itself so both racing imports can resolve the identical Program/
        League by code.

        Two Seasons, not one, because commit_hierarchy_import's own
        upsert_imported_season unconditionally re-locks its target Season
        (_require_active_season, #159) on EVERY commit, even a fully
        unchanged repeat row -- if the racing pilot import ALSO targeted
        season_a, its own _require_active_season lock (held for its whole
        paused transaction) would block hierarchy's identical lock attempt
        until the pilot side's transaction concluded, which is exactly the
        ordering these tests exist to force the OTHER way around. That is
        correct, expected serialization on a shared Season, not a bug --
        but it makes season_a unusable as the pilot side's OWN target here.
        Racing the SAME team/player identity across two DIFFERENT Seasons
        of the one shared permanent League isolates the IDENTITY race this
        finding is about from that unrelated, already-correct Season-lock
        serialization."""
        seed_api = ApiService(seed_store)
        result = seed_api.commit_hierarchy_import(fx.full_payload(
            clubs_csv="", players_csv="", registrations_csv="",
            season_venue_access_csv="",
            permanent_teams_csv=fx.permanent_teams_csv(
                rows=(f"OVER55,L1,{stable_team_code},Stable Team,",))))
        assert result["committed"], result
        season_a = next(s for s in seed_store.all_seasons()
                        if s.external_ref == "FALL26")
        league = next(l for l in seed_store.all_leagues()
                     if l.external_ref == "L1")
        program = next(p for p in seed_store.all_programs()
                       if p.external_ref == "OVER55")
        seed_setup = SetupService(seed_store)
        season_b = seed_setup.create_season(program.id, "Winter 2026",
                                            actor_id="admin")
        seed_setup.create_league_season(league.id, season_b.id, actor_id="admin")
        team = next(t for t in seed_store.all_teams()
                   if t.external_ref == stable_team_code)
        return season_b, team

    def _pausing_next_id(self, store, prefix, reached_event, resume_event):
        """Two-event handshake, not a bare sleep: reached_event proves this
        side's own snapshot has already run and it is now paused right
        before reserving prefix's id; the caller only then releases the
        OTHER side to commit, and signals resume_event once that commit is
        done, so this side's reservation+recheck deterministically lands
        AFTER the other side's write, never racing it by luck."""
        real = store.next_id
        def _wrapped(p):
            if p == prefix:
                reached_event.set()
                if not resume_event.wait(timeout=10):
                    raise AssertionError("other side never signalled completion")
            return real(p)
        return _wrapped

    def test_team_race_hierarchy_wins_pilot_discovers_and_adopts(self):
        """Genuine two-connection PostgreSQL race, real thread interleaving:
        pilot is paused inside its own next_id("team") reservation (having
        already taken its gate_errors snapshot with the code absent) while
        hierarchy commits the SAME code to completion; pilot then resumes
        and must discover and adopt the now-real Team rather than
        duplicate it. Honest scope note: this specific direction exercises
        commit_teams_players_import's PILOT-side _TeamLockPlanDrifted
        check (round 14 finding 2), already unit-proven for pilot-vs-pilot
        by the class above -- not this round's NEW hierarchy-side
        _HierarchyTeamOrPlayerDrifted fix, since hierarchy here is simply
        the first and only writer, never itself discovering a race. Its
        value is proving that mechanism ALSO converges correctly against
        a hierarchy-originated row specifically (a real cross-entry-point
        integration check), not in exercising new round-14 code. The
        reverse direction below -- where hierarchy's OWN new fix is what
        must fire -- could not be forced this same way; see its own
        docstring for why, and for the real-store technique used instead."""
        seed_store = SqlStore(self.url)
        season, _stable = self._seed_shared_league_and_team(
            seed_store, "STABLE_TA")

        store_h, store_p = SqlStore(self.url), SqlStore(self.url)
        api_h, api_p = ApiService(store_h), ApiService(store_p)

        pilot_reached = threading.Event()
        pilot_resume = threading.Event()
        store_p.next_id = self._pausing_next_id(
            store_p, "team", pilot_reached, pilot_resume)

        hierarchy_payload = fx.full_payload(
            clubs_csv="", players_csv="", registrations_csv="",
            season_venue_access_csv="",
            permanent_teams_csv=fx.permanent_teams_csv(
                rows=("OVER55,L1,RACE_HP,Race HP Team,",)))
        pilot_teams_csv = "team_code,team_name\nRACE_HP,Race HP Team (pilot)\n"

        results = {}

        def run_pilot():
            try:
                results["p"] = api_p.commit_teams_players_import(
                    season.id, {"teams_csv": pilot_teams_csv})
            except Exception as exc:
                results["p"] = exc

        tp = threading.Thread(target=run_pilot)
        tp.start()
        self.assertTrue(pilot_reached.wait(timeout=10),
                        "pilot never reached its next_id(team) pause")
        # Pilot is now paused, holding its reservation call, right before
        # it would return; commit the hierarchy import to full completion
        # while it waits, so pilot's later recheck finds a real,
        # already-committed Team it never anticipated.
        try:
            results["h"] = api_h.commit_hierarchy_import(hierarchy_payload)
        except Exception as exc:
            results["h"] = exc
        finally:
            pilot_resume.set()
        tp.join(20)

        self.assertFalse(tp.is_alive(), "pilot thread hung")
        for key, res in results.items():
            self.assertNotIsInstance(
                res, Exception, f"commit {key} raised: {res!r}")
            self.assertTrue(res.get("committed"), f"commit {key}: {res}")

        fresh = SqlStore(self.url)
        teams = [t for t in fresh.all_teams() if t.external_ref == "RACE_HP"]
        self.assertEqual(
            len(teams), 1,
            f"expected exactly one Team for RACE_HP, got {[t.id for t in teams]}")
        league = next(l for l in fresh.all_leagues() if l.external_ref == "L1")
        program = next(p for p in fresh.all_programs() if p.external_ref == "OVER55")
        self.assertEqual(teams[0].league_id, league.id)
        self.assertEqual(teams[0].program_id, program.id)

    def _stale_snapshot_all_teams(self, store, exclude_code):
        """Real-PostgreSQL-store falsifiability technique for the
        direction genuine two-connection concurrency cannot force (see
        this method's own callers for why): patches store.next_id to
        notice when commit_hierarchy_import reserves its batch_id (a
        unique, one-time marker -- the same style of phase tracking
        used elsewhere in this file rather than a raw call count, for
        the same reason the rinks test file's own docstring gives), then
        makes the FIRST store.all_teams() call after that marker --
        exactly commit_hierarchy_import's own top-level {code: obj}
        snapshot -- omit exclude_code, reproducing precisely what a
        snapshot taken before a concurrent writer's commit would see.
        Every OTHER call, including upsert_imported_team's own internal
        reserve-then-recheck, sees the real, current data -- so the
        drift this forces is exactly the one the recheck exists to
        catch, not an artifact of the patch itself."""
        phase = {"batch_reserved": False, "snapshot_taken": False}
        real_next_id = store.next_id
        def _tracking_next_id(p):
            result = real_next_id(p)
            if p == "importbatch":
                phase["batch_reserved"] = True
            return result
        store.next_id = _tracking_next_id
        real_all_teams = store.all_teams
        def _wrapped():
            result = real_all_teams()
            if phase["batch_reserved"] and not phase["snapshot_taken"]:
                phase["snapshot_taken"] = True
                return [t for t in result if t.external_ref != exclude_code]
            return result
        store.all_teams = _wrapped

    def _stale_snapshot_all_players(self, store, exclude_code, stale_calls=1):
        """Player counterpart of _stale_snapshot_all_teams above -- same
        importbatch phase marker, but on store.all_players(), with the
        number of leading calls to stale made configurable.
        commit_hierarchy_import's own players snapshot (captured
        immediately after its teams snapshot) is the ONLY all_players()
        call before upsert_imported_player's own recheck, so 1 (the
        default) reproduces it exactly. commit_teams_players_import's
        player loop calls all_players() TWICE before its own
        reserve-then-recheck: once for swap-safe jersey release (identity-
        unrelated, but still the first call after the batch marker) and
        once for the row's real existing-player decision -- both must
        stay stale, or the second (real) call would already see the
        concurrently-committed row and short-circuit straight to the
        update branch, proving nothing about the reserve-then-recheck
        path this exists to exercise. Either way the FINAL call --
        the recheck immediately after next_id("player") reserves an id --
        is always left real, so the code's own drift-catching logic is
        what actually converges the result, not the patch."""
        phase = {"batch_reserved": False, "stale_calls_remaining": stale_calls}
        real_next_id = store.next_id
        def _tracking_next_id(p):
            result = real_next_id(p)
            if p == "importbatch":
                phase["batch_reserved"] = True
            return result
        store.next_id = _tracking_next_id
        real_all_players = store.all_players
        def _wrapped():
            result = real_all_players()
            if phase["batch_reserved"] and phase["stale_calls_remaining"] > 0:
                phase["stale_calls_remaining"] -= 1
                return [p for p in result if p.external_ref != exclude_code]
            return result
        store.all_players = _wrapped

    def test_team_race_pilot_wins_hierarchy_discovers_via_stale_snapshot(self):
        """The reverse direction of the hierarchy-wins test above (pilot
        creates first, hierarchy's later import of the identical code
        must discover and adopt rather than duplicate) is NOT
        constructible via a forced two-connection concurrent pause,
        confirmed empirically, not assumed: EVERY import commit path in
        this codebase -- commit_hierarchy_import and
        commit_teams_players_import alike -- reserves its own batch_id
        from the SAME global next_id("importbatch") counter as
        essentially its first substantive statement, and that counter's
        lock (like every next_id() lock -- #331 review round 13's own
        established mechanism) is held for the reserving transaction's
        entire duration. Pausing hierarchy at ANY later point -- inside
        its League lock (_lock_league_for_binding, also probed and
        confirmed blocking), or even immediately after its own top-level
        {code: obj} snapshot, before any lock at all -- still leaves it
        holding the importbatch counter reserved from moments earlier;
        pilot's own commit needs that identical counter to generate ITS
        batch_id and cannot even begin until hierarchy's paused
        transaction concludes. This is the same self-inflicted
        circular-wait CLASS as the Season-lock and League-lock variants
        the team-race docstrings above already document, and the
        setupaudit-counter variant round 13's own dropped isolated-
        Player test first identified -- confirmed here via direct
        reproduction (a 6-second forced pause blocks pilot's commit
        completely, then both sides resolve within milliseconds of each
        other the instant hierarchy's transaction is forced to abort)
        before concluding it and moving to the technique below, not
        assumed by analogy.

        Proven instead against a REAL PostgreSQL-backed store, one
        connection per side, with pilot's commit fully executed and
        committed BEFORE hierarchy's import runs at all:
        _stale_snapshot_all_teams patches hierarchy's OWN store so its
        one-time top-level snapshot omits the code pilot already
        committed -- reproducing exactly what that snapshot would see
        under genuine concurrent overlap -- while every other read
        (including upsert_imported_team's internal recheck) sees the
        real, current row. This exercises the complete real code path --
        the drift signal, commit_hierarchy_import's retry loop rolling
        back and re-snapshotting, the second attempt's clean convergence
        -- everything but the literal interleaving, which the lock
        analysis above (and the reproduction behind it) shows cannot be
        forced without a self-inflicted deadlock. Falsifiability-verified:
        reverting round 14's fix makes this test observe TWO Teams for
        RACE_PH, not one."""
        seed_store = SqlStore(self.url)
        season, _stable = self._seed_shared_league_and_team(
            seed_store, "STABLE_TB")

        pilot_teams_csv = "team_code,team_name\nRACE_PH,Race PH Team (pilot)\n"
        pilot_api = ApiService(seed_store)
        pilot_result = pilot_api.commit_teams_players_import(
            season.id, {"teams_csv": pilot_teams_csv})
        self.assertTrue(pilot_result.get("committed"), pilot_result)

        store_h = SqlStore(self.url)
        api_h = ApiService(store_h)
        self._stale_snapshot_all_teams(store_h, "RACE_PH")

        hierarchy_payload = fx.full_payload(
            clubs_csv="", players_csv="", registrations_csv="",
            season_venue_access_csv="",
            permanent_teams_csv=fx.permanent_teams_csv(
                rows=("OVER55,L1,RACE_PH,Race PH Team,",)))
        hierarchy_result = api_h.commit_hierarchy_import(hierarchy_payload)
        self.assertTrue(hierarchy_result.get("committed"), hierarchy_result)
        self.assertEqual(
            hierarchy_result["summary"]["permanent_teams"]["created"], 0,
            "hierarchy must UPDATE the pilot's Team via its own drift "
            f"retry, never create a duplicate: {hierarchy_result}")

        fresh = SqlStore(self.url)
        teams = [t for t in fresh.all_teams() if t.external_ref == "RACE_PH"]
        self.assertEqual(
            len(teams), 1,
            f"expected exactly one Team for RACE_PH, got {[t.id for t in teams]}")
        league = next(l for l in fresh.all_leagues() if l.external_ref == "L1")
        program = next(p for p in fresh.all_programs() if p.external_ref == "OVER55")
        self.assertEqual(teams[0].league_id, league.id)
        self.assertEqual(teams[0].program_id, program.id)

    def test_player_race_hierarchy_discovers_pilots_player_via_stale_snapshot(self):
        """Player counterpart of the stale-snapshot Team test above.
        Neither forced-concurrent-overlap Player direction is
        constructible via mid-transaction pausing either, confirmed by
        direct reproduction: pilot's players_csv row requires a teams_csv
        row for its target team in the SAME upload (sheet-internal-only
        matching -- unlike hierarchy's players_csv, confirmed to accept
        an already-persisted team_code with no matching permanent_teams_
        csv row), and processing that teams_csv row unconditionally
        writes a self._audit(...) entry, whether create or update; every
        audit entry across every import path shares the ONE global
        next_id("setupaudit") counter (round 13's own already-documented
        finding for the pilot-vs-pilot case, same mechanism reproduced
        and confirmed here for hierarchy-vs-pilot). Reproduced directly:
        even giving each side its OWN separate pre-existing team (so
        neither needs the other's Team-row lock) did not avoid an 8-
        second forced block, confirming the contention is the shared
        counter, not a Team- or League-row lock specifically.

        Proven instead with the SAME stale-snapshot technique as the
        Team test above, applied to store.all_players(): pilot commits
        its player fully first; hierarchy's own top-level players
        snapshot is patched to omit it, forcing upsert_imported_player's
        real internal recheck to discover it and drift-retry.
        Falsifiability-verified: reverting round 14's fix makes this
        test observe TWO Players for RACE_PLSTALE, not one."""
        seed_store = SqlStore(self.url)
        season, stable_team = self._seed_shared_league_and_team(
            seed_store, "STABLE_PW")

        pilot_teams_csv = (
            f"team_code,team_name\n{stable_team.external_ref},Stable Team\n")
        pilot_players_csv = (
            "player_code,first_name,last_name,team_code,jersey_number,position\n"
            f"RACE_PLSTALE,Pilot,Player,{stable_team.external_ref},9,defense\n")
        pilot_api = ApiService(seed_store)
        pilot_result = pilot_api.commit_teams_players_import(
            season.id, {"teams_csv": pilot_teams_csv,
                       "players_csv": pilot_players_csv})
        self.assertTrue(pilot_result.get("committed"), pilot_result)

        store_h = SqlStore(self.url)
        api_h = ApiService(store_h)
        self._stale_snapshot_all_players(store_h, "RACE_PLSTALE")

        hierarchy_payload = fx.full_payload(
            clubs_csv="", registrations_csv="", season_venue_access_csv="",
            permanent_teams_csv="",
            players_csv=fx.players_csv(
                rows=(f"RACE_PLSTALE,{stable_team.external_ref},Hier,Player,7,forward,",)))
        hierarchy_result = api_h.commit_hierarchy_import(hierarchy_payload)
        self.assertTrue(hierarchy_result.get("committed"), hierarchy_result)
        self.assertEqual(
            hierarchy_result["summary"]["players"]["created"], 0,
            "hierarchy must UPDATE the pilot's Player via its own drift "
            f"retry, never create a duplicate: {hierarchy_result}")

        fresh = SqlStore(self.url)
        players = [p for p in fresh.all_players()
                  if p.external_ref == "RACE_PLSTALE"]
        self.assertEqual(
            len(players), 1,
            f"expected exactly one Player for RACE_PLSTALE, got {[p.id for p in players]}")
        self.assertEqual(players[0].team_id, stable_team.id)

    def test_player_race_hierarchy_wins_pilot_discovers_via_stale_snapshot(self):
        """The reverse direction of the test above: hierarchy commits its
        Player first, pilot's own commit_teams_players_import must
        discover and adopt it rather than duplicate. #331 review round 14
        named this combination explicitly ("force Team and Player races
        independently, in both winner directions") and pointed out the
        combined Team+Player regression above doesn't independently prove
        it, since Team/audit serialization already settles that test
        first -- this is the still-missing fourth combination (Team had
        both directions; Player only had the reverse one).

        Not constructible as a genuine two-connection pause either, for
        the SAME reason test_player_race_hierarchy_discovers_pilots_
        player_via_stale_snapshot's own docstring already established and
        reproduced: pilot's players_csv row requires a teams_csv row for
        the same team in the SAME upload, and that team row unconditionally
        writes a self._audit(...) entry -- reserving the one global
        next_id("setupaudit") counter -- BEFORE pilot's own player loop
        ever runs, regardless of whether the team codes collide with
        hierarchy's. Pausing pilot at any point in or after its player
        loop therefore already holds that counter, and hierarchy's own
        player-row audit write blocks on it -- the identical self-
        inflicted circular wait, just encountered from the opposite
        direction. Confirmed by direct reproduction (an 8+ second forced
        block that only resolves once the paused side is abandoned),
        not assumed by analogy to the already-documented case.

        Proven instead with the same stale-snapshot technique, applied
        this time to PILOT's own store rather than hierarchy's: hierarchy
        commits its Player fully and first; pilot's all_players() is
        patched (stale_calls=2) so BOTH calls before its own
        reserve-then-recheck -- the swap-safe jersey-release snapshot and
        the row's real existing-player decision -- omit the code
        hierarchy already committed, forcing pilot down the "not found"
        branch into next_id("player") and its own recheck, which (left
        unpatched) sees the real committed row and must adopt it instead
        of creating a duplicate. Falsifiability-verified: reverting round
        14's pilot-side reserve-then-recheck (#331 review round 13's own
        fix, still present but insufficient alone without this round's
        _TeamLockPlanDrifted-adjacent guards being intact) makes this
        test observe TWO Players for RACE_PLHIER, not one."""
        seed_store = SqlStore(self.url)
        season, stable_team = self._seed_shared_league_and_team(
            seed_store, "STABLE_PH")

        store_h = SqlStore(self.url)
        api_h = ApiService(store_h)
        hierarchy_payload = fx.full_payload(
            clubs_csv="", registrations_csv="", season_venue_access_csv="",
            permanent_teams_csv="",
            players_csv=fx.players_csv(
                rows=(f"RACE_PLHIER,{stable_team.external_ref},Hier,Player,7,forward,",)))
        hierarchy_result = api_h.commit_hierarchy_import(hierarchy_payload)
        self.assertTrue(hierarchy_result.get("committed"), hierarchy_result)

        store_p = SqlStore(self.url)
        api_p = ApiService(store_p)
        self._stale_snapshot_all_players(store_p, "RACE_PLHIER", stale_calls=2)

        pilot_teams_csv = (
            f"team_code,team_name\n{stable_team.external_ref},Stable Team\n")
        pilot_players_csv = (
            "player_code,first_name,last_name,team_code,jersey_number,position\n"
            f"RACE_PLHIER,Pilot,Player,{stable_team.external_ref},9,defense\n")
        pilot_result = api_p.commit_teams_players_import(
            season.id, {"teams_csv": pilot_teams_csv,
                       "players_csv": pilot_players_csv})
        self.assertTrue(pilot_result.get("committed"), pilot_result)
        self.assertEqual(
            pilot_result["summary"]["players"]["created"], 0,
            "pilot must UPDATE hierarchy's Player via its own round-13 "
            f"reserve-then-recheck, never create a duplicate: {pilot_result}")

        fresh = SqlStore(self.url)
        players = [p for p in fresh.all_players()
                  if p.external_ref == "RACE_PLHIER"]
        self.assertEqual(
            len(players), 1,
            f"expected exactly one Player for RACE_PLHIER, got {[p.id for p in players]}")
        self.assertEqual(players[0].team_id, stable_team.id)
        self.assertEqual(players[0].name, "Pilot Player")


if __name__ == "__main__":
    unittest.main()
