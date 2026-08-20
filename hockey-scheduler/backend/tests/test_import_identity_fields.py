"""Canonical import/export schema extension for athlete identity (#273).

Both import paths — the legacy #92/#93 teams+players wizard and the #260
nine-sheet hierarchy import — carry the optional identity columns
(``preferred_name``, ``shoots``, ``birthdate``, ``registration_number``,
``skill_rating``), persist the structured names they always carried instead
of flattening them away, and report ambiguous identities BEFORE any write
(AC[4]): invalid cells and same-team duplicate registration numbers are
dry-run ERRORS; same-name-without-disambiguation and cross-team shared
registration numbers are dry-run WARNINGS that never block and never merge.

Runs the write paths on Memory, SQLite, and PostgreSQL.
"""

import os
import unittest
from datetime import date

from helpers import BACKEND  # noqa: F401  (ensures sys.path is set up)

import hierarchy_fixtures as fx
from hockey_scheduler.api import ApiService
from hockey_scheduler.domain import Position, Team
from hockey_scheduler.services import SetupService
from hockey_scheduler.services.hierarchy_import import HIERARCHY_TEMPLATES
from hockey_scheduler.services.import_validator import (
    parse_csv_text, validate_import)
from hockey_scheduler.store import InMemoryStore, SqlStore

EXT_PLAYERS_HEADER = (
    "player_code,first_name,last_name,team_code,jersey_number,position,"
    "email,preferred_name,shoots,birthdate,registration_number,skill_rating")

TEAMS_CSV = ("team_code,team_name,club_name,division_name\n"
             "T1,Team One,Lions Club,U10\n"
             "T2,Team Two,Lions Club,U10\n")


def _players_csv(*rows):
    return "\n".join((EXT_PLAYERS_HEADER,) + rows) + "\n"


def _rows(*rows):
    return parse_csv_text(_players_csv(*rows))


def _service_backends():
    stores = [("memory", InMemoryStore()), ("sqlite", SqlStore(":memory:"))]
    url = os.environ.get("TEST_DATABASE_URL")
    if url:
        pg = SqlStore(url)
        pg.reset_schema()
        stores.append(("postgres", pg))
    return stores


class DryRunIdentityValidationTest(unittest.TestCase):
    """Pure validate_import checks — no store writes anywhere."""

    def _report(self, players_rows, store=None, today=None):
        return validate_import(
            {"teams": parse_csv_text(TEAMS_CSV), "players": players_rows},
            store=store, today=today)

    def test_invalid_identity_cells_are_field_errors(self):
        rows = _rows(
            "P1,Jane,Smith,T1,7,forward,,JJ,X,2015-03-02,HC-1,4",
            "P2,Kai,Lund,T1,8,forward,,,L,2015-02-30,HC-2,4",
            "P3,Ana,Rey,T1,9,forward,,,R,2015-03-02,HC 3,4",
            "P4,Ben,Ola,T1,10,forward,,,L,2015-03-02,HC-4,9")
        report = self._report(rows)
        fields = sorted((e["row"], e["field"]) for e in report["errors"])
        self.assertEqual(fields, [(1, "shoots"), (2, "birthdate"),
                                  (3, "registration_number"),
                                  (4, "skill_rating")])
        self.assertFalse(report["ok"])

    def test_future_birthdate_reported_only_when_today_supplied(self):
        rows = _rows("P1,Jane,Smith,T1,7,forward,,,,2031-01-01,,")
        without_today = self._report(rows)
        self.assertTrue(without_today["ok"])
        with_today = self._report(rows, today=date(2026, 8, 14))
        self.assertFalse(with_today["ok"])
        self.assertEqual(with_today["errors"][0]["field"], "birthdate")
        self.assertIn("in_future", with_today["errors"][0]["message"])

    def test_same_team_registration_duplicate_is_error_cross_team_warning(self):
        report = self._report(_rows(
            "P1,Jane,Smith,T1,7,forward,,,,,HC-1,",
            "P2,Ana,Rey,T1,8,forward,,,,,HC-1,"))
        self.assertEqual(
            [e["field"] for e in report["errors"]],
            ["registration_number", "registration_number"])
        report = self._report(_rows(
            "P1,Jane,Smith,T1,7,forward,,,,,HC-1,",
            "P2,Ana,Rey,T2,8,forward,,,,,HC-1,"))
        self.assertTrue(report["ok"])
        self.assertEqual(len(report["warnings"]), 2)
        self.assertIn("several teams", report["warnings"][0]["message"])

    def test_same_name_same_team_warns_unless_disambiguated(self):
        undis = self._report(_rows(
            "P1,Jane,Smith,T1,7,forward,,,,,,",
            "P2,Jane,Smith,T1,8,forward,,,,,,"))
        self.assertTrue(undis["ok"])  # a warning, never a block
        self.assertEqual(len(undis["warnings"]), 2)
        self.assertIn("never merged by name", undis["warnings"][0]["message"])
        disambiguated = self._report(_rows(
            "P1,Jane,Smith,T1,7,forward,,,,2015-01-01,,",
            "P2,Jane,Smith,T1,8,forward,,,,2016-01-01,,"))
        self.assertEqual(disambiguated["warnings"], [])

    def test_store_backed_checks_skip_the_rows_own_self_update(self):
        for label, store in _service_backends():
            with self.subTest(backend=label):
                store.add_team(Team(id="t1", name="Team One",
                                    external_ref="T1"))
                setup = SetupService(store)
                existing = setup.add_player(
                    "t1", None, Position.FORWARD, first_name="Jane",
                    last_name="Smith", registration_number="HC-1")
                existing.external_ref = "P1"
                store.save_player(existing)
                try:
                    # Same player_code re-imported at its own registration:
                    # a self-update, not a duplicate.
                    clean = self._report(_rows(
                        "P1,Jane,Smith,T1,7,forward,,,,,HC-1,"),
                        store=store)
                    self.assertTrue(clean["ok"], (label, clean))
                    self.assertEqual(clean["warnings"], [], label)
                    # A DIFFERENT code at that registration on that team is
                    # the commit-refused collision -> dry-run error.
                    collision = self._report(_rows(
                        "P9,New,Kid,T1,9,forward,,,,,HC-1,"), store=store)
                    self.assertEqual(collision["errors"][0]["field"],
                                     "registration_number", label)
                    # And an exact-name match without disambiguation warns.
                    named = self._report(_rows(
                        "P9,Jane,Smith,T1,9,forward,,,,,,"), store=store)
                    self.assertTrue(named["ok"], label)
                    self.assertIn("SEPARATE player",
                                  named["warnings"][0]["message"], label)
                finally:
                    if isinstance(store, SqlStore):
                        store.close()


class LegacyCommitIdentityTest(unittest.TestCase):
    def _each(self):
        for label, store in _service_backends():
            with self.subTest(backend=label):
                api = ApiService(store)
                setup = api.setup
                program = setup.create_program("Test League", actor_id="op")
                season = setup.create_season(program.id, "2026",
                                             actor_id="op")
                setup.create_league(season.id, "L1", actor_id="op")
                try:
                    yield label, store, api, setup, season
                finally:
                    if isinstance(store, SqlStore):
                        store.close()

    def _player(self, store, code):
        return next(p for p in store.all_players()
                    if p.external_ref == code)

    def test_commit_persists_structured_names_and_identity_cells(self):
        for label, store, api, setup, season in self._each():
            res = api.commit_teams_players_import(
                season.id,
                {"teams_csv": TEAMS_CSV,
                 "players_csv": _players_csv(
                     "P1,Jane,Smith,T1,7,forward,j@x.com,JJ,L,2015-03-02,"
                     "HC-1,4")},
                actor_id="op")
            self.assertNotIn("error", res, (label, res))
            self.assertTrue(res["committed"], label)
            player = self._player(store, "P1")
            self.assertEqual(
                (player.first_name, player.last_name, player.name),
                ("Jane", "Smith", "Jane Smith"), label)
            self.assertEqual(
                (player.preferred_name, player.shoots, player.birthdate,
                 player.registration_number, player.skill_rating),
                ("JJ", "L", "2015-03-02", "HC-1", 4), label)

    def test_reimport_with_blank_optional_cells_leaves_values_alone(self):
        for label, store, api, setup, season in self._each():
            sheets = {"teams_csv": TEAMS_CSV,
                      "players_csv": _players_csv(
                          "P1,Jane,Smith,T1,7,forward,,JJ,L,2015-03-02,"
                          "HC-1,4")}
            api.commit_teams_players_import(season.id, sheets, actor_id="op")
            # Second upload: identity cells blank -> leave-as-is, not clear.
            res = api.commit_teams_players_import(
                season.id,
                {"teams_csv": TEAMS_CSV,
                 "players_csv": _players_csv(
                     "P1,Jane,Smith,T1,7,forward,,,,,,")},
                actor_id="op")
            self.assertTrue(res["committed"], label)
            player = self._player(store, "P1")
            self.assertEqual(
                (player.preferred_name, player.shoots, player.birthdate,
                 player.registration_number, player.skill_rating),
                ("JJ", "L", "2015-03-02", "HC-1", 4), label)

    def test_commit_blocked_by_identity_errors_writes_nothing(self):
        for label, store, api, setup, season in self._each():
            res = api.commit_teams_players_import(
                season.id,
                {"teams_csv": TEAMS_CSV,
                 "players_csv": _players_csv(
                     "P1,Jane,Smith,T1,7,forward,,,,2031-01-01,,")},
                actor_id="op")
            self.assertFalse(res["committed"], label)
            self.assertEqual(res["errors"][0]["field"], "birthdate", label)
            self.assertEqual(store.all_players(), [], label)

    def test_same_name_import_creates_both_and_audits_a_warning(self):
        for label, store, api, setup, season in self._each():
            res = api.commit_teams_players_import(
                season.id,
                {"teams_csv": TEAMS_CSV,
                 "players_csv": _players_csv(
                     "P1,Jane,Smith,T1,7,forward,,,,,,",
                     "P2,Jane,Smith,T1,8,forward,,,,,,")},
                actor_id="op")
            self.assertTrue(res["committed"], label)
            self.assertEqual(res["summary"]["players"]["created"], 2, label)
            warns = [a for a in store.all_setup_audit()
                     if a.action == "player_duplicate_warning"]
            self.assertEqual(len(warns), 1, label)
            self.assertIn("import_batch_id", warns[0].detail, label)


class HierarchyImportIdentityTest(unittest.TestCase):
    def _each(self):
        for label, store in _service_backends():
            with self.subTest(backend=label):
                api = ApiService(store)
                try:
                    yield label, store, api
                finally:
                    if isinstance(store, SqlStore):
                        store.close()

    def test_canonical_template_round_trips_clean(self):
        for label, store, api in self._each():
            payload = fx.full_payload(
                players_csv=HIERARCHY_TEMPLATES["players_csv"])
            dry = api.get_hierarchy_import_dry_run(payload)
            self.assertEqual(dry["errors"], [], (label, dry))
            res = api.commit_hierarchy_import(payload, actor_id="op")
            self.assertTrue(res["committed"], (label, res))
            player = next(p for p in store.all_players()
                          if p.external_ref == "P001")
            self.assertEqual(
                (player.first_name, player.last_name, player.name,
                 player.preferred_name, player.shoots, player.birthdate,
                 player.registration_number, player.skill_rating),
                ("Jane", "Smith", "Jane Smith", "JJ", "L", "2015-03-02",
                 "HC-12345", 4), label)
            # Idempotent re-import: same rows -> zero updates, values kept.
            again = api.commit_hierarchy_import(payload, actor_id="op")
            self.assertTrue(again["committed"], label)
            self.assertEqual(again["summary"]["players"],
                             {"created": 0, "updated": 0, "skipped": 1},
                             label)

    def test_legacy_header_without_identity_columns_still_imports(self):
        for label, store, api in self._each():
            payload = fx.full_payload()  # fixture header has no #273 columns
            res = api.commit_hierarchy_import(payload, actor_id="op")
            self.assertTrue(res["committed"], (label, res))
            player = next(p for p in store.all_players()
                          if p.external_ref == "P001")
            self.assertEqual((player.first_name, player.last_name),
                             ("Jane", "Smith"), label)
            self.assertIsNone(player.birthdate, label)
            self.assertIsNone(player.registration_number, label)

    def test_dry_run_reports_identity_problems_before_write(self):
        ext_header = (fx.PLAYERS_HEADER
                      + ",preferred_name,shoots,birthdate,"
                        "registration_number,skill_rating")
        rows = ("P001,LIONS,Jane,Smith,7,forward,,,Q,2015-02-30,HC 1,9",
                "P002,LIONS,Jane,Smith,8,forward,,,,,,")
        players_csv = "\n".join((ext_header,) + rows) + "\n"
        for label, store, api in self._each():
            dry = api.get_hierarchy_import_dry_run(
                fx.full_payload(players_csv=players_csv))
            error_fields = {(e["row"], e["field"]) for e in dry["errors"]}
            self.assertEqual(
                error_fields,
                {(1, "shoots"), (1, "birthdate"),
                 (1, "registration_number"), (1, "skill_rating")}, label)
            warning_codes = {w["code"] for w in dry["warnings"]}
            self.assertIn("duplicate_player_name", warning_codes, label)
            self.assertEqual(store.all_players(), [], label)


if __name__ == "__main__":
    unittest.main()
