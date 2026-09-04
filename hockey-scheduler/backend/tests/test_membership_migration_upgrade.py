"""Migration 059 upgrade safety and install-order parity (#205 Slice A).

#425 owner review, migration-renumbering finding. This slice's migration was
originally authored as ``052_season_roster_membership`` while main had
independently advanced through 058 and already shipped its OWN
``052_epoch_fence_version``. Same leading number, DIFFERENT filename — so
git's merge reported no textual conflict at all and both files simply
coexisted in ``migrations/``, while the real defect was an ORDERING one:

* on a database already migrated through main's 058 BEFORE this file ever
  existed, ``migrate()`` applies it chronologically LAST (it was never
  recorded in ``schema_migrations``, so it lands whenever the loop first
  reaches it);
* on a FRESH database the same file sorts and applies BEFORE 053-058, since
  ``"052_season_roster_membership" < "053_season_copy_forward_commits"``
  lexicographically.

Two different real DDL-execution orders for one file, purely a function of
install history — and load-bearing here rather than cosmetic, because this
migration's own preflight and backfill READ
``seasons``/``league_seasons``/``season_team_registrations``/``players``,
tables that 053-058 also alter. "Which order" decides what the backfill
actually sees.

This is the identical defect #424 fixed by renumbering its own
``051_athlete_identity`` -> 058 (which is in turn how #426 resolved its
053/055 -> 056/057 collision), and it is resolved here the same way:
renumbering past main's claimed range, to 059. See
``059_season_roster_membership.sql``'s own NUMBERING NOTE for the full
history.

``Migration059UpgradeOrderingParityTest`` below is this round's required
regression, in the same shape ``Migration058UpgradeOrderingParityTest``
(``test_identity_migration_upgrade.py``) uses: it builds the pre-059 state
by REPLAYING THE REAL migration files main actually ships (through 058,
with ``059_season_roster_membership.sql`` absent) rather than hand-reversing
059's own forward DDL the way ``test_season_roster_membership.py``'s
``_downgrade_059`` does — so it genuinely exercises the production upgrade
path — then proves:

* 059 applies EXACTLY ONCE on that upgrade;
* the pre-059 rows seeded before the deploy all survive it;
* the backfill derived exactly the memberships it should have (and none of
  the ones it must never fabricate);
* an idempotent reopen re-applies nothing and changes no ledger row;
* a FRESH 001-through-059 database converges on the identical schema shape,
  the identical ledger set, AND the identical backfilled membership ids.

Migrations here are FORWARD-ONLY (see ``sql_store.migrate``'s docstring):
this codebase has no downgrade/revert path, so the only ordering that
exists — and the only one under test — is the forward one.

Runs on file-backed SQLite (the reopen is real) and on PostgreSQL when
TEST_DATABASE_URL is set.
"""

import os
import shutil
import tempfile
import time
import unittest

from helpers import BACKEND  # noqa: F401  (ensures sys.path is set up)

from hockey_scheduler.store import SqlStore
from hockey_scheduler.store import sql_store as _sql_store_module

_MIGRATION = "059_season_roster_membership"
_PREDECESSOR = "058_athlete_identity"

_BRANCH_MIGRATIONS_DIR = _sql_store_module._MIGRATIONS_DIR


def _copy_migrations_dir(exclude=frozenset()):
    """A temp copy of the branch's real migrations/ directory, optionally
    omitting some exact filenames — used to make migrate() REPLAY the real
    historical migration files main actually ships, in the real sorted
    order, rather than hand-reversing a forward migration's DDL. Caller
    owns ``shutil.rmtree`` cleanup."""
    tmp = tempfile.mkdtemp(prefix="mig059_parity_")
    for fname in os.listdir(_BRANCH_MIGRATIONS_DIR):
        if not fname.endswith(".sql") or fname in exclude:
            continue
        shutil.copy(os.path.join(_BRANCH_MIGRATIONS_DIR, fname),
                    os.path.join(tmp, fname))
    return tmp


def _table_columns(store, table):
    cur = store.conn.cursor()
    if store.backend == "sqlite":
        cur.execute(f"PRAGMA table_info('{table}')")
        return {row["name"] for row in cur.fetchall()}
    cur.execute(
        "SELECT column_name AS name FROM information_schema.columns "
        "WHERE table_name = %s", (table,))
    return {row["name"] for row in cur.fetchall()}


def _ledger_versions(store):
    """Every version ever recorded in schema_migrations, as a set — the
    full upgrade history, order-independent."""
    cur = store.conn.cursor()
    cur.execute("SELECT version FROM schema_migrations")
    return {row["version"] for row in cur.fetchall()}


def _applied_count(store, version):
    cur = store.conn.cursor()
    cur.execute(store.dialect.sql(
        "SELECT COUNT(*) AS n FROM schema_migrations WHERE version = ?"),
        (version,))
    return cur.fetchone()["n"]


def _membership_rows(store):
    """``(id, player_id, league_season_id, season_id, team_id, status,
    position, jersey_number, shoots, effective_from, effective_to)`` for
    every backfilled row, read through raw SQL (not the domain store) so
    this assertion is about what the MIGRATION physically wrote."""
    cur = store.conn.cursor()
    cur.execute(
        "SELECT id, player_id, league_season_id, season_id, team_id, "
        "status, position, jersey_number, shoots, effective_from, "
        "effective_to FROM season_roster_memberships")
    return sorted(
        (r["id"], r["player_id"], r["league_season_id"], r["season_id"],
         r["team_id"], r["status"], r["position"], r["jersey_number"],
         r["shoots"], r["effective_from"], r["effective_to"])
        for r in cur.fetchall())


class Migration059SortsAfterEveryMainMigrationTest(unittest.TestCase):
    """The renumbering invariant itself, asserted directly on the loader.

    This is the assertion that actually FAILS if this file is renumbered
    back to 052 — the upgrade/parity test below does not, and that is worth
    stating plainly: for this particular migration the two install orders
    happen to produce the same final rows, so schema/ledger/backfill parity
    alone cannot detect the defect. What genuinely differs at 052 is the
    file's POSITION in the applied sequence, and only an order assertion
    catches it.

    ``migrate()`` applies ``_load_migrations()`` in sorted-version order and
    skips anything already recorded. So a file whose version stem sorts
    after EVERY migration main ships is guaranteed to apply in the same
    relative position on both install paths:

    * on a fresh database it sorts last, so it runs last;
    * on a database already migrated through main's 058 it is the only
      unrecorded version, so it also runs last.

    At 052 that guarantee is broken in exactly one direction — a fresh
    database would sort it BEFORE 053-058 while an upgrade still ran it
    after them — which is the whole reason for the rename. Pinning it here
    means one of MAIN's independently-shipped, low-numbered files can never
    silently reintroduce that same collision underneath 059 again.

    #205 blocker 3 (``060_substitute_team_id``) legitimately sorts AFTER
    059 — that is not a repeat of the 052 collision at all: it is this same
    branch's own forward continuation, authored fresh with 059 already on
    disk, so both install paths (fresh database; upgrade from a database
    already at 059) apply it last in the same relative position either way
    — no ordering divergence is possible for a version that postdates 059
    on the SAME lineage. What must still never happen is one of MAIN's own
    versions (anything below ``060`` — the 001-058 range this branch does
    not own) ending up after 059; that is the collision this test exists to
    catch, so it is asserted explicitly by name below rather than by a
    blanket "nothing may ever sort after 059" rule that would also reject
    this slice's own legitimate later migrations.
    """

    # This branch's own legitimate forward continuations of 059 — each is
    # itself a fresh, uncollided version number authored with every prior
    # file already on disk, so none of them can reproduce 059's original
    # divergence (see the class docstring). Extend this set, by name, the
    # next time a #205 slice adds a migration after 059 — a new version NOT
    # listed here still fails the test below, so an accidental collision
    # (or an unreviewed addition) cannot pass silently.
    _KNOWN_FOLLOWERS = frozenset({
        "060_substitute_team_id",
        "061_roster_entry_durable_attribution",
        "062_cancelled_game_ice_history",
        "063_subtree_deletion_challenges",
        "064_cross_team_substitute_provenance",
    })

    def test_migration_sorts_after_every_other_shipped_migration(self):
        versions = [v for v, _ in _sql_store_module._load_migrations()]
        self.assertIn(_MIGRATION, versions)
        after = [v for v in versions if v > _MIGRATION]
        unexpected = [v for v in after if v not in self._KNOWN_FOLLOWERS]
        self.assertEqual(
            unexpected, [],
            "059_season_roster_membership must sort after every migration "
            "MAIN ships (so its applied position is identical on a fresh "
            "install and on an upgrade from main) and after nothing else "
            "unaccounted for. Versions sorting after it that are not in "
            f"_KNOWN_FOLLOWERS: {unexpected}")
        # And specifically past main's own claimed range: the collision
        # that started this was with main's 052_epoch_fence_version, a
        # DIFFERENT filename sharing the same leading number.
        self.assertIn("052_epoch_fence_version", versions,
                      "sanity: main's own 052 is still present and is a "
                      "separate migration this branch must not disturb")
        self.assertLess("052_epoch_fence_version", _MIGRATION)
        self.assertLess(_PREDECESSOR, _MIGRATION)


class Migration059UpgradeOrderingParityTest(unittest.TestCase):
    """059_season_roster_membership (renumbered from 052) must apply
    exactly once whether a database is fresh (001 through 059 in one
    ``migrate()`` pass) or an existing installation that already migrated
    through main's 058 BEFORE this file ever existed on disk — and the two
    paths must converge on the identical final schema, ledger, and
    backfilled data.

    Every database this test touches is a fresh, uniquely-named one it
    creates and tears down itself; nothing here reuses TEST_DATABASE_URL's
    own database or any state another test may have left behind.
    """

    def setUp(self):
        # Belt and suspenders: the test body always restores
        # _MIGRATIONS_DIR in a `finally` right after each use, but this
        # guarantees it regardless, so a bug here can never leak a
        # temp-directory pointer into a later test in the same process
        # (run_parallel.py shards multiple test MODULES per subprocess).
        self.addCleanup(setattr, _sql_store_module, "_MIGRATIONS_DIR",
                        _BRANCH_MIGRATIONS_DIR)

    def _locations(self):
        yield "sqlite", None
        base = os.environ.get("TEST_DATABASE_URL")
        if base:
            yield "postgres", base

    def _new_sqlite_url(self):
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.addCleanup(lambda: os.path.exists(path) and os.remove(path))
        return path

    def _new_postgres_url(self, base_url, tag):
        import psycopg
        admin_url = base_url.rsplit("/", 1)[0] + "/postgres"
        name = (base_url.rsplit("/", 1)[-1]
                + f"_059parity_{int(time.time() * 1000)}_{tag}")

        def _admin(sql_text):
            conn = psycopg.connect(admin_url, autocommit=True)
            try:
                conn.execute(sql_text)
            finally:
                conn.close()

        _admin(f'DROP DATABASE IF EXISTS "{name}"')
        _admin(f'CREATE DATABASE "{name}"')
        self.addCleanup(_admin, f'DROP DATABASE IF EXISTS "{name}"')
        return f"{base_url.rsplit('/', 1)[0]}/{name}"

    def _new_url(self, label, base, tag):
        return (self._new_sqlite_url() if label == "sqlite"
                else self._new_postgres_url(base, tag))

    # -- the pre-059 world -------------------------------------------- #
    #
    # Planted through raw SQL (not the service facade) because on the
    # replay leg the membership tables genuinely do not exist yet. The
    # shape deliberately satisfies every one of 059's preflight checks —
    # coherent Team -> League -> LeagueSeason -> Season -> Program spine,
    # no dangling refs, no duplicate active registrations, no candidate
    # jersey duplicates — so this test exercises the BACKFILL, not the
    # abort path (which test_season_roster_membership.py already covers
    # row-shape by row-shape).
    #
    # It also plants the two shapes the backfill must NEVER translate: an
    # INACTIVE player, and an ARCHIVED Season the same Team is actively
    # registered in. Both must still be here, untouched and un-backfilled,
    # on the far side of the upgrade.
    def _seed_pre_059_data(self, store):
        sql = store.dialect.sql
        cur = store.conn.cursor()
        cur.execute(sql("INSERT INTO programs (id, name) VALUES (?, ?)"),
                    ("pr1", "Program One"))
        cur.execute(sql(
            "INSERT INTO leagues (id, name, program_id) VALUES (?, ?, ?)"),
            ("lg1", "League One", "pr1"))
        # One ACTIVE Season (backfilled) and one ARCHIVED Season (never).
        cur.execute(sql(
            "INSERT INTO seasons (id, program_id, name, status) "
            "VALUES (?, ?, ?, ?)"), ("s1", "pr1", "Active Season", "active"))
        cur.execute(sql(
            "INSERT INTO seasons (id, program_id, name, status) "
            "VALUES (?, ?, ?, ?)"),
            ("s2", "pr1", "Old Season", "archived"))
        cur.execute(sql(
            "INSERT INTO league_seasons (id, league_id, season_id) "
            "VALUES (?, ?, ?)"), ("ls1", "lg1", "s1"))
        cur.execute(sql(
            "INSERT INTO league_seasons (id, league_id, season_id) "
            "VALUES (?, ?, ?)"), ("ls2", "lg1", "s2"))
        cur.execute(sql(
            "INSERT INTO teams (id, name, program_id, league_id) "
            "VALUES (?, ?, ?, ?)"), ("t1", "Team One", "pr1", "lg1"))
        cur.execute(sql(
            "INSERT INTO season_team_registrations "
            "(id, team_id, active, league_season_id) VALUES (?, ?, ?, ?)"),
            ("r1", "t1", 1, "ls1"))
        # Active registration in the ARCHIVED Season too — the backfill
        # must still refuse to mint a stint there.
        cur.execute(sql(
            "INSERT INTO season_team_registrations "
            "(id, team_id, active, league_season_id) VALUES (?, ?, ?, ?)"),
            ("r2", "t1", 1, "ls2"))
        cur.execute(sql(
            "INSERT INTO players (id, team_id, name, position, shoots, "
            "jersey_number, is_active, external_ref) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)"),
            ("p1", "t1", "Active Skater", "forward", "L", 9, 1, "EXT-P1"))
        cur.execute(sql(
            "INSERT INTO players (id, team_id, name, position, shoots, "
            "jersey_number, is_active, external_ref) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)"),
            ("p2", "t1", "Benched Skater", "defense", "R", 2, 0, "EXT-P2"))
        store.conn.commit()

    def _assert_seed_data_intact(self, store, label):
        """Every pre-059 row survives the upgrade byte for byte. Migrations
        never rewrite data; this pins that for the exact rows planted."""
        sql = store.dialect.sql
        cur = store.conn.cursor()
        cur.execute(sql(
            "SELECT name, position, shoots, jersey_number, is_active, "
            "external_ref FROM players WHERE id = ?"), ("p1",))
        row = cur.fetchone()
        self.assertEqual(
            (row["name"], row["position"], row["shoots"],
             row["jersey_number"], bool(row["is_active"]),
             row["external_ref"]),
            ("Active Skater", "forward", "L", 9, True, "EXT-P1"), label)
        cur.execute(sql(
            "SELECT name, is_active, external_ref FROM players "
            "WHERE id = ?"), ("p2",))
        row = cur.fetchone()
        self.assertEqual(
            (row["name"], bool(row["is_active"]), row["external_ref"]),
            ("Benched Skater", False, "EXT-P2"), label)
        cur.execute(sql("SELECT name, program_id, league_id FROM teams "
                        "WHERE id = ?"), ("t1",))
        row = cur.fetchone()
        self.assertEqual(
            (row["name"], row["program_id"], row["league_id"]),
            ("Team One", "pr1", "lg1"), label)
        cur.execute("SELECT id FROM season_team_registrations")
        self.assertEqual(
            sorted(r["id"] for r in cur.fetchall()), ["r1", "r2"], label)
        cur.execute("SELECT id, status FROM seasons")
        self.assertEqual(
            sorted((r["id"], r["status"]) for r in cur.fetchall()),
            [("s1", "active"), ("s2", "archived")], label)

    def _expected_backfill(self):
        """EXACTLY one membership: the ACTIVE player, in the ACTIVE Season
        his Team is actively registered in — carrying the player's
        season-scoped attributes copied forward and NO fabricated dates.

        Not present, and asserted so by exact-set equality: any row for the
        inactive player p2, and any row in the archived Season's ls2
        despite its own active registration r2."""
        return [("srm_legacy_p1_ls1", "p1", "ls1", "s1", "t1", "active",
                 "forward", 9, "L", None, None)]

    def test_upgrade_from_main_058_applies_059_once_and_matches_fresh(self):
        for label, base in self._locations():
            with self.subTest(backend=label):
                url = self._new_url(label, base, "up")
                main_only_dir = _copy_migrations_dir(
                    exclude={"059_season_roster_membership.sql"})
                self.addCleanup(shutil.rmtree, main_only_dir,
                                ignore_errors=True)

                # (1) Build EXACTLY current-main's schema/ledger state:
                # migrated through 058 by REPLAYING the real main files,
                # 059_season_roster_membership never applied — a real
                # existing production installation about to take this
                # PR's deploy.
                _sql_store_module._MIGRATIONS_DIR = main_only_dir
                try:
                    store = SqlStore(url)
                finally:
                    _sql_store_module._MIGRATIONS_DIR = _BRANCH_MIGRATIONS_DIR

                self.assertEqual(
                    _applied_count(store, _MIGRATION), 0,
                    f"{label}: 059 must not be applied yet on the "
                    "pre-deploy replay")
                self.assertEqual(
                    _applied_count(store, _PREDECESSOR), 1,
                    f"{label}: sanity, the replay really did reach "
                    "main's 058")
                self.assertNotIn(
                    "season_roster_memberships", _ledger_versions(store),
                    label)

                self._seed_pre_059_data(store)
                store.close()

                # (2) THE DEPLOY: this branch's migrations/ directory (059
                # included) lands; the app restarts and migrate() runs
                # again against the SAME, already-existing database. 059's
                # preflight runs against the seeded rows, then its DDL and
                # backfill.
                upgraded = SqlStore(url)
                try:
                    self.assertEqual(
                        _applied_count(upgraded, _MIGRATION), 1,
                        f"{label}: 059 must apply exactly once")

                    # (3) the pre-059 rows survive the upgrade unchanged.
                    self._assert_seed_data_intact(upgraded, label)

                    # (4) the backfill derived exactly the right stints.
                    self.assertEqual(
                        _membership_rows(upgraded), self._expected_backfill(),
                        f"{label}: upgraded-from-058 backfill must derive "
                        "exactly the active player's active-Season stint "
                        "-- never the inactive player, never the archived "
                        "Season")

                    upgraded_cols = _table_columns(
                        upgraded, "season_roster_memberships")
                    upgraded_event_cols = _table_columns(
                        upgraded, "season_roster_membership_events")
                    upgraded_ledger = _ledger_versions(upgraded)
                finally:
                    upgraded.close()

                # (5) idempotent reopen: migrate() runs again with nothing
                # left to apply — no error, still recorded exactly once,
                # and critically the backfill does NOT run a second time
                # and duplicate every membership.
                reopened = SqlStore(url)
                try:
                    self.assertEqual(
                        _applied_count(reopened, _MIGRATION), 1,
                        f"{label}: idempotent reopen must not re-apply or "
                        "duplicate the ledger row")
                    self.assertEqual(
                        _membership_rows(reopened),
                        self._expected_backfill(),
                        f"{label}: idempotent reopen must not re-run the "
                        "backfill")
                    self.assertEqual(
                        _table_columns(reopened, "season_roster_memberships"),
                        upgraded_cols, label)
                    self.assertEqual(
                        _ledger_versions(reopened), upgraded_ledger,
                        f"{label}: idempotent reopen must not change the "
                        "recorded ledger set")
                finally:
                    reopened.close()

                # (6) parity: a FRESH database, migrated 001 through 059 in
                # ONE pass — where 059 sorts LAST rather than landing last
                # chronologically — ends up at the identical final schema,
                # the identical recorded ledger set, and (seeded the same
                # way, then migrated) the identical backfilled rows.
                fresh_url = self._new_url(label, base, "fresh")
                fresh = SqlStore(fresh_url)
                try:
                    self.assertEqual(
                        _table_columns(fresh, "season_roster_memberships"),
                        upgraded_cols,
                        f"{label}: fresh-install season_roster_memberships "
                        "columns must match the upgraded-from-058 path "
                        "exactly")
                    self.assertEqual(
                        _table_columns(
                            fresh, "season_roster_membership_events"),
                        upgraded_event_cols,
                        f"{label}: fresh-install event-table columns must "
                        "match the upgraded-from-058 path exactly")
                    self.assertEqual(
                        _ledger_versions(fresh), upgraded_ledger,
                        f"{label}: fresh-install and upgraded-from-058 "
                        "must record the identical set of applied "
                        "migrations")
                finally:
                    fresh.close()

    def test_fresh_install_backfills_identically_to_the_upgrade_path(self):
        """The same seeded world, reached the OTHER way round: planted on a
        database that already has 059 (fresh 001-through-059), then
        re-migrated. Proves the ordering split is genuinely closed — the
        backfill's OUTPUT, not just the final schema, is identical on both
        install paths.

        This is the assertion the pre-rename numbering could not have
        satisfied: as 052 the file ran BEFORE 053-058 on a fresh database
        and AFTER them on an upgrade, so the two paths' backfills read
        different intermediate shapes of the very tables they select from.
        """
        for label, base in self._locations():
            with self.subTest(backend=label):
                # Fresh 001-through-059, membership tables already present
                # and empty; seed the same pre-059 world, then re-run the
                # backfill by un-recording ONLY the ledger row (the tables
                # and their CREATE ... IF NOT EXISTS DDL are unchanged, so
                # this replays exactly the INSERT ... SELECT).
                url = self._new_url(label, base, "freshback")
                store = SqlStore(url)
                try:
                    self.assertEqual(
                        _applied_count(store, _MIGRATION), 1,
                        f"{label}: fresh install must record 059")
                    self.assertEqual(
                        _membership_rows(store), [],
                        f"{label}: nothing to backfill on an empty fresh DB")
                    self._seed_pre_059_data(store)
                    cur = store.conn.cursor()
                    cur.execute(store.dialect.sql(
                        "DELETE FROM schema_migrations WHERE version = ?"),
                        (_MIGRATION,))
                    store.conn.commit()
                finally:
                    store.close()

                replayed = SqlStore(url)
                try:
                    self.assertEqual(
                        _applied_count(replayed, _MIGRATION), 1, label)
                    self._assert_seed_data_intact(replayed, label)
                    self.assertEqual(
                        _membership_rows(replayed),
                        self._expected_backfill(),
                        f"{label}: the fresh-install path's backfill must "
                        "derive byte-identical rows to the "
                        "upgraded-from-058 path")
                finally:
                    replayed.close()


if __name__ == "__main__":
    unittest.main()
