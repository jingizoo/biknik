"""Same-team registration-number invariant: bypass fix + race backstop
(#273 review round 2 finding 2).

``SetupService._assert_registration_number_available`` already refused a
same-team duplicate governing-body id at create/update, but three other
paths could still land two rows with one registration number on ONE team:

* ``assign_player_team()`` checked ONLY jersey availability before moving a
  player onto a destination team — the registration number carried along
  for the ride, unchecked.
* Both import paths (the legacy teams+players CSV commit and the nine-sheet
  hierarchy import's ``upsert_imported_player``) skipped the check whenever
  the sheet's registration_number cell was BLANK (leave the existing value
  as-is) or explicitly re-supplied UNCHANGED — even when the row's team_code
  moved it onto a different team than it started on.
* Even with every service-layer check firing, a check-then-write is not
  safe under a cross-process race: two concurrent writers can each observe
  the number as free before either commits. Migration 051 now adds a
  partial ``UNIQUE (team_id, registration_number) WHERE registration_number
  IS NOT NULL`` index as the database backstop, translated at the store
  boundary into the SAME stable ``duplicate_registration_number`` conflict.

Covers, on Memory/SQLite/[PostgreSQL]:

* active AND inactive direct moves (unlike jersey, this invariant is NOT
  conditioned on ``is_active`` — the pre-existing create/update check
  already covers inactive rows, and a move must honor the same rule);
* both import paths with blank/same/changed registration values, including
  the dry-run previewer writing zero rows either way;
* direct SQL constraint probes (SQLite/PostgreSQL): a bypassing raw INSERT
  is rejected, NULLs stay unconstrained, cross-team duplicates stay allowed;
* two-connection PostgreSQL races (create/create, create/edit, edit/edit,
  move/import) with the SAME race attempted (fully serialized, so it can
  only ever land the same correct outcome) on Memory/SQLite, and no audit
  entry for the losing side of any race.
"""

import os
import threading
import unittest

from helpers import BACKEND  # noqa: F401  (ensures sys.path is set up)

from hockey_scheduler.api import ApiService
from hockey_scheduler.domain import Player, Position, Team
from hockey_scheduler.domain.errors import IntegrityConflictError, ValidationError
from hockey_scheduler.store import InMemoryStore, SqlStore

REG = "REG-1"
OTHER_REG = "REG-2"


def _backends():
    stores = [("memory", InMemoryStore()), ("sqlite", SqlStore(":memory:"))]
    url = os.environ.get("TEST_DATABASE_URL")
    if url:
        pg = SqlStore(url)
        pg.reset_schema()
        stores.append(("postgres", pg))
    return stores


def _seed_two_teams(store):
    store.add_team(Team(id="t1", name="Team One"))
    store.add_team(Team(id="t2", name="Team Two"))


def _player_audits(store):
    return [a for a in store.all_setup_audit()
            if a.action in ("player_created", "player_updated",
                            "player_added", "player_team_assigned")]


# =========================================================================== #
# Direct move: assign_player_team() used to check ONLY jersey availability.   #
# =========================================================================== #
class DirectMoveInvariantTest(unittest.TestCase):
    def _seeded_api(self, store):
        _seed_two_teams(store)
        return ApiService(store)

    def test_active_move_onto_conflicting_team_refused_zero_mutation(self):
        for label, store in _backends():
            with self.subTest(backend=label):
                api = self._seeded_api(store)
                api.create_player("t2", "Holder", "forward",
                                  registration_number=REG, actor_id="op")
                mover = api.create_player("t1", "Mover", "forward",
                                          registration_number=REG, actor_id="op")
                audits_before = len(_player_audits(store))
                res = api.assign_player_team(mover["id"], "t2", actor_id="op")
                self.assertEqual(res.get("error", {}).get("code"),
                                 "validation_error", (label, res))
                self.assertEqual(
                    res["error"]["details"]["reason"],
                    "duplicate_registration_number", (label, res))
                # Zero mutation: the mover is still on t1, and no new audit.
                self.assertEqual(
                    store.get_player(mover["id"]).team_id, "t1", label)
                self.assertEqual(len(_player_audits(store)), audits_before,
                                 label)
                if isinstance(store, SqlStore):
                    store.close()

    def test_inactive_move_onto_conflicting_team_is_ALSO_refused(self):
        """Unlike jersey (active-only), a registration number stays reserved
        by an INACTIVE row too — deactivating a player must not free the
        identity for an accidental duplicate on a move."""
        for label, store in _backends():
            with self.subTest(backend=label):
                api = self._seeded_api(store)
                holder = api.create_player("t2", "Holder", "forward",
                                           registration_number=REG,
                                           actor_id="op")
                api.set_player_active(holder["id"], False, actor_id="op")
                mover = api.create_player("t1", "Mover", "forward",
                                          registration_number=REG,
                                          actor_id="op")
                res = api.assign_player_team(mover["id"], "t2", actor_id="op")
                self.assertEqual(
                    res.get("error", {}).get("details", {}).get("reason"),
                    "duplicate_registration_number", (label, res))
                self.assertEqual(
                    store.get_player(mover["id"]).team_id, "t1", label)
                if isinstance(store, SqlStore):
                    store.close()

    def test_move_onto_team_with_no_conflict_succeeds(self):
        """Positive control: the fix does not over-block ordinary moves."""
        for label, store in _backends():
            with self.subTest(backend=label):
                api = self._seeded_api(store)
                mover = api.create_player("t1", "Mover", "forward",
                                          registration_number=REG,
                                          actor_id="op")
                res = api.assign_player_team(mover["id"], "t2", actor_id="op")
                self.assertNotIn("error", res, (label, res))
                self.assertEqual(store.get_player(mover["id"]).team_id, "t2",
                                 label)
                if isinstance(store, SqlStore):
                    store.close()

    def test_move_with_no_registration_number_is_unconstrained(self):
        for label, store in _backends():
            with self.subTest(backend=label):
                api = self._seeded_api(store)
                api.create_player("t2", "Holder", "forward",
                                  registration_number=REG, actor_id="op")
                mover = api.create_player("t1", "Mover", "forward",
                                          actor_id="op")  # no registration
                res = api.assign_player_team(mover["id"], "t2", actor_id="op")
                self.assertNotIn("error", res, (label, res))
                if isinstance(store, SqlStore):
                    store.close()


# =========================================================================== #
# Both import paths: blank/same/changed cell on a Team move.                  #
# =========================================================================== #
_LEGACY_HEADER = ("player_code,first_name,last_name,team_code,jersey_number,"
                  "position,email,registration_number\n")

_HIERARCHY_PLAYERS_HEADER = (
    "player_code,team_code,first_name,last_name,jersey_number,position,"
    "email,preferred_name,shoots,birthdate,registration_number,"
    "skill_rating\n")


def _import_rejected(res):
    """True if an import commit was refused, in EITHER response shape.

    ``commit_teams_players_import``/``commit_hierarchy_import`` run the
    pure pre-commit validator (``validate_import``/``validate_hierarchy_
    import``) FIRST; if IT catches the problem, the commit returns the
    clean ``{"committed": False, "errors": [...]}`` report without ever
    reaching a write. At the time this finding (round 2) landed, that
    pre-check's OWN duplicate-registration logic did not yet know to check
    a BLANK cell's EFFECTIVE retained value on a Team move, so it could wave
    a blank-cell move through clean; this write-time fix (inside the row
    loop itself) was what actually caught it, surfacing as the facade's
    normal exception envelope, ``{"error": {...}}``, instead of the clean
    validator report.

    #273 review round 3 finding 1 closed that preview-side gap: the SAME
    blank-cell-Team-move scenario now ALSO previews ``ok: False`` before any
    write is attempted (see test_import_effective_registration_state.py),
    so every scenario in THIS module's ``ImportBypassTest`` below in fact
    now reaches the clean ``{"committed": False, "errors": [...]}`` shape
    too, not the exception envelope. This helper is kept accepting EITHER
    shape anyway — it is still correct, it costs nothing, and a caller
    verifying the REAL invariant should check zero mutation directly rather
    than assume one specific response shape survives every future change.
    """
    return bool(res.get("error")) or res.get("committed") is False


class ImportBypassTest(unittest.TestCase):
    """Both import paths used to skip the check whenever the sheet's
    registration_number cell was blank or explicitly unchanged, even when
    the row's team_code moved the player onto a conflicting team."""

    # -- legacy teams+players pilot import (commit_teams_players_import) --
    def _legacy_seed(self, store):
        """Team ids are NOT hardcoded anywhere here: the importer
        found-or-creates a Team per ``team_code`` and auto-generates its
        real id, matched by ``external_ref`` on repeat imports (never a
        literal "t1"/"t2") -- so the mover's STARTING team_id is read back
        from the store, not assumed."""
        api = ApiService(store)
        program = api.create_program("P", "US", "UTC")
        season = api.create_season(program["id"], "S")
        api.create_league(season["id"], "L")
        teams_csv = "team_code,team_name,club_name,division_name\nT1,Team One,,\nT2,Team Two,,\n"
        players_csv = (_LEGACY_HEADER
                      + f"H1,Holder,H,T2,,forward,,{REG}\n"
                      + f"M1,Mover,M,T1,,forward,,{REG}\n")
        res = api.commit_teams_players_import(
            season["id"], {"teams_csv": teams_csv, "players_csv": players_csv},
            actor_id="op")
        self.assertTrue(res.get("committed"), res)
        mover_team_id = next(p for p in store.all_players()
                             if p.external_ref == "M1").team_id
        return api, season["id"], teams_csv, mover_team_id

    def _legacy_move_row(self, registration_cell):
        """M1 moves T1 -> T2 with the given registration_number cell text
        (empty string reproduces a blank cell)."""
        return (_LEGACY_HEADER
                + f"H1,Holder,H,T2,,forward,,{REG}\n"
                + f"M1,Mover,M,T2,,forward,,{registration_cell}\n")

    def test_legacy_import_move_with_blank_cell_refused(self):
        for label, store in _backends():
            with self.subTest(backend=label):
                api, season_id, teams_csv, mover_team_id = self._legacy_seed(store)
                audits_before = len(_player_audits(store))
                res = api.commit_teams_players_import(
                    season_id,
                    {"teams_csv": teams_csv,
                     "players_csv": self._legacy_move_row("")},
                    actor_id="op")
                self.assertTrue(_import_rejected(res), (label, res))
                mover = next(p for p in store.all_players()
                            if p.external_ref == "M1")
                self.assertEqual(mover.team_id, mover_team_id,
                                 label)  # never moved
                self.assertEqual(len(_player_audits(store)), audits_before,
                                 label)
                if isinstance(store, SqlStore):
                    store.close()

    def test_legacy_import_move_with_same_value_refused(self):
        for label, store in _backends():
            with self.subTest(backend=label):
                api, season_id, teams_csv, mover_team_id = self._legacy_seed(store)
                res = api.commit_teams_players_import(
                    season_id,
                    {"teams_csv": teams_csv,
                     "players_csv": self._legacy_move_row(REG)},
                    actor_id="op")
                self.assertFalse(res.get("committed", True), (label, res))
                if isinstance(store, SqlStore):
                    store.close()

    def test_legacy_import_move_with_changed_value_still_refused(self):
        """Sanity: the OLD code path this replaces already caught a CHANGED
        conflicting value -- pinned so the rewrite does not regress it."""
        for label, store in _backends():
            with self.subTest(backend=label):
                api, season_id, teams_csv, mover_team_id = self._legacy_seed(store)
                res = api.commit_teams_players_import(
                    season_id,
                    {"teams_csv": teams_csv,
                     "players_csv": self._legacy_move_row(REG)},
                    actor_id="op")
                self.assertFalse(res.get("committed", True), (label, res))
                if isinstance(store, SqlStore):
                    store.close()

    def test_legacy_import_dry_run_never_writes(self):
        """The dry-run previewer never writes -- and (#273 review round 3
        finding 1 closed the preview-side gap this test used to route
        around; see ``_import_rejected``'s docstring) now ALSO correctly
        reports this exact blank-cell Team move as unsafe, matching the
        commit two tests above, which proves the SAME payload is in fact
        refused when it actually tries to write."""
        for label, store in _backends():
            with self.subTest(backend=label):
                api, season_id, teams_csv, mover_team_id = self._legacy_seed(store)
                before = len(store.all_players())
                preview = api.get_import_dry_run(
                    {"teams_csv": teams_csv,
                     "players_csv": self._legacy_move_row("")})
                self.assertFalse(preview["ok"], (label, preview))
                self.assertEqual(len(store.all_players()), before, label)
                if isinstance(store, SqlStore):
                    store.close()

    # -- nine-sheet hierarchy import (api.commit_hierarchy_import) ---------
    # Uses the FACADE entry point (not the service function directly): it is
    # what parses raw ``*_csv`` text into the row-dict sheets the service
    # function actually consumes, and is what every real caller (HTTP,
    # other tests) goes through.
    _HIERARCHY_PROGRAMS_CSV = (
        "program_code,operator_organization_code,program_name,country,timezone\n"
        "PR1,,P,US,UTC\n")
    _HIERARCHY_COMPETITION_CSV = (
        "program_code,season_code,season_name,league_code,league_name,"
        "league_sort_order,division_code,division_name,age_group,"
        "season_start,season_end\nPR1,S1,Season,L1,League,1,,,,,\n")
    _HIERARCHY_TEAMS_CSV = (
        "program_code,league_code,team_code,team_name,club_code\n"
        "PR1,L1,T1,Team One,\nPR1,L1,T2,Team Two,\n")

    def _hierarchy_seed(self, store):
        api = ApiService(store)
        sheets = {
            "import_type": "hierarchy",
            "programs_csv": self._HIERARCHY_PROGRAMS_CSV,
            "competition_csv": self._HIERARCHY_COMPETITION_CSV,
            "permanent_teams_csv": self._HIERARCHY_TEAMS_CSV,
            "players_csv": (_HIERARCHY_PLAYERS_HEADER
                            + f"H1,T2,Holder,H,,forward,,,,,{REG},\n"
                            + f"M1,T1,Mover,M,,forward,,,,,{REG},\n"),
        }
        res = api.commit_hierarchy_import(sheets, actor_id="op")
        self.assertTrue(res.get("committed"), res)
        mover_team_id = next(p for p in store.all_players()
                             if p.external_ref == "M1").team_id
        return api, sheets, mover_team_id

    def _hierarchy_move_row(self, sheets, registration_cell):
        moved = dict(sheets)
        moved["players_csv"] = (
            _HIERARCHY_PLAYERS_HEADER
            + f"H1,T2,Holder,H,,forward,,,,,{REG},\n"
            + f"M1,T2,Mover,M,,forward,,,,,{registration_cell},\n")
        return moved

    def test_hierarchy_import_move_with_blank_cell_refused(self):
        for label, store in _backends():
            with self.subTest(backend=label):
                api, sheets, mover_team_id = self._hierarchy_seed(store)
                audits_before = len(_player_audits(store))
                res = api.commit_hierarchy_import(
                    self._hierarchy_move_row(sheets, ""), actor_id="op")
                self.assertTrue(_import_rejected(res), (label, res))
                mover = next(p for p in store.all_players()
                            if p.external_ref == "M1")
                self.assertEqual(mover.team_id, mover_team_id,
                                 (label, mover.team_id))  # never moved
                self.assertEqual(len(_player_audits(store)), audits_before,
                                 label)
                if isinstance(store, SqlStore):
                    store.close()

    def test_hierarchy_import_move_with_same_value_refused(self):
        for label, store in _backends():
            with self.subTest(backend=label):
                api, sheets, _mover_team_id = self._hierarchy_seed(store)
                res = api.commit_hierarchy_import(
                    self._hierarchy_move_row(sheets, REG), actor_id="op")
                self.assertFalse(res.get("committed", True), (label, res))
                if isinstance(store, SqlStore):
                    store.close()

    def test_hierarchy_import_dry_run_never_writes(self):
        """See ``test_legacy_import_dry_run_never_writes``'s sibling test
        docstring just above: the previewer never writes, and now (#273
        review round 3 finding 1) also correctly reports this blank-cell
        Team move as unsafe."""
        for label, store in _backends():
            with self.subTest(backend=label):
                api, sheets, _mover_team_id = self._hierarchy_seed(store)
                before = len(store.all_players())
                moved = self._hierarchy_move_row(sheets, "")
                preview = api.get_hierarchy_import_dry_run(moved)
                self.assertFalse(preview["ok"], (label, preview))
                self.assertEqual(len(store.all_players()), before, label)
                if isinstance(store, SqlStore):
                    store.close()


# =========================================================================== #
# Direct SQL constraint probes (SQLite / PostgreSQL).                         #
# =========================================================================== #
def _sql_backends():
    backends = [("sqlite", ":memory:")]
    url = os.environ.get("TEST_DATABASE_URL")
    if url:
        backends.append(("postgres", url))
    return backends


def _fresh(url):
    store = SqlStore(url)
    if url != ":memory:":
        store.reset_schema()
    return store


def _player(pid, team, reg=None, active=True):
    return Player(id=pid, team_id=team, name=pid, position=Position.FORWARD,
                  registration_number=reg, is_active=active)


class ConstraintProbeTest(unittest.TestCase):
    def test_direct_duplicate_insert_bypassing_the_service_is_rejected(self):
        for label, url in _sql_backends():
            store = _fresh(url)
            try:
                _seed_two_teams(store)
                with store.transaction():
                    store.add_player(_player("a", "t1", REG))
                with self.assertRaises(IntegrityConflictError, msg=label) as ctx:
                    with store.transaction():
                        store.add_player(_player("b", "t1", REG))
                self.assertEqual(ctx.exception.details["reason"],
                                 "duplicate_registration_number", label)
                self.assertEqual(ctx.exception.details["team_id"], "t1",
                                 label)
            finally:
                store.close()

    def test_inactive_duplicate_is_ALSO_rejected_by_the_constraint(self):
        """The index is NOT active-only (unlike jersey's) -- an inactive
        holder still reserves the number at the database level too."""
        for label, url in _sql_backends():
            store = _fresh(url)
            try:
                _seed_two_teams(store)
                with store.transaction():
                    store.add_player(_player("a", "t1", REG, active=False))
                with self.assertRaises(IntegrityConflictError, msg=label):
                    with store.transaction():
                        store.add_player(_player("b", "t1", REG))
            finally:
                store.close()

    def test_any_number_of_nulls_and_cross_team_duplicates_are_allowed(self):
        for label, url in _sql_backends():
            store = _fresh(url)
            try:
                _seed_two_teams(store)
                with store.transaction():
                    store.add_player(_player("a", "t1", REG))
                    store.add_player(_player("b", "t2", REG))    # other team
                    store.add_player(_player("c", "t1", None))   # null
                    store.add_player(_player("d", "t1", None))   # null again
                cur = store.conn.cursor()
                cur.execute("SELECT COUNT(*) AS n FROM players")
                self.assertEqual(cur.fetchone()["n"], 4, label)
            finally:
                store.close()

    def test_conflict_makes_zero_writes(self):
        for label, url in _sql_backends():
            store = _fresh(url)
            try:
                _seed_two_teams(store)
                with store.transaction():
                    store.add_player(_player("a", "t1", REG))
                with self.assertRaises(IntegrityConflictError, msg=label):
                    with store.transaction():
                        store.add_player(_player("ok", "t1", OTHER_REG))
                        store.add_player(_player("b", "t1", REG))  # violates
                self.assertIsNone(store.get_player("ok"), label)  # rolled back
            finally:
                store.close()


# =========================================================================== #
# Memory/SQLite parity: the SAME contending pair, attempted concurrently.     #
# =========================================================================== #
class RegistrationRaceMemorySqliteParityTest(unittest.TestCase):
    """Memory and SQLite (this codebase's ``SqlStore(":memory:")``) hold a
    process-wide transaction lock for a whole ``@_transactional`` call's
    body (see ``test_write_race_hardening.py`` / ``test_season_lifecycle_
    concurrency.py``'s identical documented convention) — TWO THREADS
    sharing ONE store instance can never actually interleave inside it, so
    launching them as real threads here would only deadlock one of them
    against the other's held lock without ever exercising the invariant.
    The correct Memory/SQLite parity proof is therefore the SEQUENTIAL one:
    attempt both contending writes back-to-back on the SAME store and prove
    the invariant still holds -- second writer refused, zero mutation, zero
    orphan audit -- exactly as if they had genuinely raced. The REAL
    concurrent-thread races over two independent connections, decided by
    the database, are PostgreSQL-only below."""

    def _mem_sqlite_backends(self):
        return [("memory", InMemoryStore()), ("sqlite", SqlStore(":memory:"))]

    def _sequential_pair(self, store, action_a, action_b, team_id):
        audits_before = len(_player_audits(store))
        api = ApiService(store)
        action_a(api)  # winner
        with self.assertRaises(ValidationError) as ctx:
            action_b(api)  # loser: sees the winner's already-committed row
        self.assertEqual(ctx.exception.details["reason"],
                         "duplicate_registration_number")
        self.assertEqual(len(_player_audits(store)), audits_before + 1)
        holders = [p for p in store.all_players()
                  if p.team_id == team_id and p.registration_number == REG]
        self.assertEqual(len(holders), 1)

    def test_create_vs_create(self):
        for label, store in self._mem_sqlite_backends():
            with self.subTest(backend=label):
                _seed_two_teams(store)
                self._sequential_pair(
                    store,
                    lambda api: api.setup.add_player(
                        "t1", "A", Position.FORWARD,
                        registration_number=REG, actor_id="a"),
                    lambda api: api.setup.add_player(
                        "t1", "B", Position.FORWARD,
                        registration_number=REG, actor_id="b"),
                    "t1")

    def test_create_vs_edit(self):
        for label, store in self._mem_sqlite_backends():
            with self.subTest(backend=label):
                _seed_two_teams(store)
                api = ApiService(store)
                other = api.setup.add_player(
                    "t1", "Other", Position.FORWARD,
                    registration_number=OTHER_REG, actor_id="seed")
                self._sequential_pair(
                    store,
                    lambda api: api.setup.add_player(
                        "t1", "New", Position.FORWARD,
                        registration_number=REG, actor_id="a"),
                    lambda api: api.setup.update_player(
                        other.id, registration_number=REG, actor_id="b"),
                    "t1")

    def test_edit_vs_edit(self):
        for label, store in self._mem_sqlite_backends():
            with self.subTest(backend=label):
                _seed_two_teams(store)
                api = ApiService(store)
                p1 = api.setup.add_player("t1", "P1", Position.FORWARD,
                                          registration_number="P1-REG",
                                          actor_id="seed")
                p2 = api.setup.add_player("t1", "P2", Position.FORWARD,
                                          registration_number="P2-REG",
                                          actor_id="seed")
                self._sequential_pair(
                    store,
                    lambda api: api.setup.update_player(
                        p1.id, registration_number=REG, actor_id="a"),
                    lambda api: api.setup.update_player(
                        p2.id, registration_number=REG, actor_id="b"),
                    "t1")

    def test_move_vs_import(self):
        for label, store in self._mem_sqlite_backends():
            with self.subTest(backend=label):
                api = ApiService(store)
                program = api.create_program("P", "US", "UTC")
                season = api.create_season(program["id"], "S")
                api.create_league(season["id"], "L")
                teams_csv = ("team_code,team_name,club_name,division_name\n"
                            "T1,Team One,,\nT2,Team Two,,\n")
                # Seed both teams AND the mover (on T1) through the SAME
                # import mechanism the race itself uses below, so team_code
                # "T2" resolves to the SAME store id on both sides of the
                # race (an import upsert matches an EXISTING team by its
                # external_ref -- a team seeded directly via store.add_team
                # with no external_ref would never match, and the "move"
                # and "import" sides would silently land on two DIFFERENT
                # teams that share nothing but a display name).
                seed_res = api.commit_teams_players_import(
                    season["id"],
                    {"teams_csv": teams_csv,
                     "players_csv": (_LEGACY_HEADER
                                     + f"M1,Mover,M,T1,,forward,,{REG}\n")},
                    actor_id="seed")
                self.assertTrue(seed_res.get("committed"), seed_res)
                mover = next(p for p in store.all_players()
                            if p.external_ref == "M1")
                team2_id = next(t.id for t in store.all_teams()
                                if t.external_ref == "T2")
                players_csv = (_LEGACY_HEADER
                               + f"NEW1,New,Guy,T2,,forward,,{REG}\n")

                audits_before = len(_player_audits(store))
                moved = api.assign_player_team(mover.id, team2_id,
                                               actor_id="a")
                self.assertNotIn("error", moved, moved)
                res = api.commit_teams_players_import(
                    season["id"],
                    {"teams_csv": teams_csv, "players_csv": players_csv},
                    actor_id="b")
                self.assertTrue(_import_rejected(res), (label, res))
                self.assertEqual(len(_player_audits(store)),
                                 audits_before + 1)
                holders = [p for p in store.all_players()
                          if p.team_id == team2_id
                          and p.registration_number == REG]
                self.assertEqual(len(holders), 1)


# =========================================================================== #
# Real two-connection PostgreSQL races: create/create, create/edit,           #
# edit/edit, move/import.                                                     #
# =========================================================================== #
@unittest.skipUnless(os.environ.get("TEST_DATABASE_URL"),
                     "cross-connection race needs PostgreSQL")
class RegistrationRacePostgresTest(unittest.TestCase):
    """TWO REAL, INDEPENDENT ``SqlStore`` connections (no shared Python lock
    between them, unlike the parity test above) racing to a genuine
    database-level decision. Every scenario checks the SAME three things:
    exactly one winner, the loser gets the stable conflict (never a raw
    error), and the loser wrote no audit entry."""

    def setUp(self):
        self.url = os.environ["TEST_DATABASE_URL"]
        SqlStore(self.url).reset_schema()

    def _race(self, action_a, action_b):
        """Force BOTH sides' ``_assert_registration_number_available``
        pre-check to observe the number as free before either write lands,
        then release them together -- mirrors ``JerseyRaceTest`` in
        test_jersey_number_rules.py. Each side opens its OWN connection, so
        this is a genuine two-connection race, not a shared-lock replay."""
        barrier = threading.Barrier(2)
        results = {}

        def run(key, action):
            store = SqlStore(self.url)
            api = ApiService(store)
            original = api.setup._assert_registration_number_available

            def checked_then_wait(*args, **kwargs):
                original(*args, **kwargs)
                barrier.wait(timeout=10)

            api.setup._assert_registration_number_available = checked_then_wait
            try:
                action(api)
                results[key] = "ok"
            except Exception as exc:  # pragma: no cover
                results[key] = f"error:{exc!r}"
            finally:
                store.close()

        ta = threading.Thread(target=run, args=("a", action_a))
        tb = threading.Thread(target=run, args=("b", action_b))
        ta.start(); tb.start()
        ta.join(timeout=15); tb.join(timeout=15)
        return results

    def _assert_one_winner_no_loser_audit(self, results, team_id):
        check = SqlStore(self.url)
        try:
            outcomes = list(results.values())
            self.assertEqual(len([r for r in outcomes if r == "ok"]), 1,
                             results)
            self.assertEqual(len([r for r in outcomes if r != "ok"]), 1,
                             results)
            holders = [p for p in check.all_players()
                      if p.team_id == team_id
                      and p.registration_number == REG]
            self.assertEqual(len(holders), 1, results)
        finally:
            check.close()

    def _audit_count(self):
        s = SqlStore(self.url)
        try:
            return len(_player_audits(s))
        finally:
            s.close()

    def test_create_vs_create(self):
        seed = SqlStore(self.url)
        _seed_two_teams(seed)
        seed.close()

        def create_a(api):
            res = api.create_player("t1", "A", "forward",
                                    registration_number=REG,
                                    actor_id="racer_a")
            if "error" in res:
                raise ValidationError(res["error"]["message"],
                                      res["error"].get("details", {}))

        def create_b(api):
            res = api.create_player("t1", "B", "forward",
                                    registration_number=REG,
                                    actor_id="racer_b")
            if "error" in res:
                raise ValidationError(res["error"]["message"],
                                      res["error"].get("details", {}))

        audits_before = self._audit_count()
        results = self._race(create_a, create_b)
        self._assert_one_winner_no_loser_audit(results, "t1")
        self.assertEqual(self._audit_count(), audits_before + 1, results)

    def test_create_vs_edit(self):
        seed = SqlStore(self.url)
        _seed_two_teams(seed)
        seed_api = ApiService(seed)
        other = seed_api.create_player("t1", "Other", "forward",
                                       registration_number=OTHER_REG,
                                       actor_id="seed")
        seed.close()

        def create_racer(api):
            res = api.create_player("t1", "New", "forward",
                                    registration_number=REG,
                                    actor_id="racer_a")
            if "error" in res:
                raise ValidationError(res["error"]["message"],
                                      res["error"].get("details", {}))

        def edit_racer(api):
            res = api.update_player(other["id"], registration_number=REG,
                                    actor_id="racer_b")
            if "error" in res:
                raise ValidationError(res["error"]["message"],
                                      res["error"].get("details", {}))

        audits_before = self._audit_count()
        results = self._race(create_racer, edit_racer)
        self._assert_one_winner_no_loser_audit(results, "t1")
        self.assertEqual(self._audit_count(), audits_before + 1, results)

    def test_edit_vs_edit(self):
        seed = SqlStore(self.url)
        _seed_two_teams(seed)
        seed_api = ApiService(seed)
        p1 = seed_api.create_player("t1", "P1", "forward",
                                    registration_number="P1-REG",
                                    actor_id="seed")
        p2 = seed_api.create_player("t1", "P2", "forward",
                                    registration_number="P2-REG",
                                    actor_id="seed")
        seed.close()

        def edit_a(api):
            res = api.update_player(p1["id"], registration_number=REG,
                                    actor_id="racer_a")
            if "error" in res:
                raise ValidationError(res["error"]["message"],
                                      res["error"].get("details", {}))

        def edit_b(api):
            res = api.update_player(p2["id"], registration_number=REG,
                                    actor_id="racer_b")
            if "error" in res:
                raise ValidationError(res["error"]["message"],
                                      res["error"].get("details", {}))

        audits_before = self._audit_count()
        results = self._race(edit_a, edit_b)
        self._assert_one_winner_no_loser_audit(results, "t1")
        self.assertEqual(self._audit_count(), audits_before + 1, results)

    def test_move_vs_import(self):
        """A direct assign_player_team() move races a legacy-import commit
        that lands a NEW player with the same registration number on the
        SAME destination team.

        Both teams AND the mover are seeded through the SAME import
        mechanism the race itself uses: an import found-or-creates a Team
        per ``team_code``, auto-generating its real id and matching it by
        ``external_ref`` on repeat imports -- never a literal "t1"/"t2" --
        so a Team seeded directly via ``store.add_team`` would never match
        and the "move" and "import" sides would silently land on two
        DIFFERENT teams that share nothing but a display name."""
        seed = SqlStore(self.url)
        seed_api = ApiService(seed)
        program = seed_api.create_program("P", "US", "UTC")
        season = seed_api.create_season(program["id"], "S")
        seed_api.create_league(season["id"], "L")
        teams_csv = ("team_code,team_name,club_name,division_name\n"
                    "T1,Team One,,\nT2,Team Two,,\n")
        seed_res = seed_api.commit_teams_players_import(
            season["id"],
            {"teams_csv": teams_csv,
             "players_csv": (_LEGACY_HEADER
                             + f"M1,Mover,M,T1,,forward,,{REG}\n")},
            actor_id="seed")
        self.assertTrue(seed_res.get("committed"), seed_res)
        mover_id = next(p for p in seed.all_players()
                        if p.external_ref == "M1").id
        team2_id = next(t.id for t in seed.all_teams()
                        if t.external_ref == "T2")
        seed.close()
        players_csv = (_LEGACY_HEADER
                       + f"NEW1,New,Guy,T2,,forward,,{REG}\n")

        def move_racer(api):
            res = api.assign_player_team(mover_id, team2_id,
                                         actor_id="racer_a")
            if "error" in res:
                raise ValidationError(res["error"]["message"],
                                      res["error"].get("details", {}))

        def import_racer(api):
            res = api.commit_teams_players_import(
                season["id"],
                {"teams_csv": teams_csv, "players_csv": players_csv},
                actor_id="racer_b")
            if _import_rejected(res):
                raise ValidationError("import rejected", res)

        audits_before = self._audit_count()
        results = self._race(move_racer, import_racer)
        self._assert_one_winner_no_loser_audit(results, team2_id)
        self.assertEqual(self._audit_count(), audits_before + 1, results)


if __name__ == "__main__":
    unittest.main()
