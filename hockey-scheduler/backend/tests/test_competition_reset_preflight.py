"""Competition-model reset — migration preflight (#233 Slice C1a).

Slice C reparents each Division onto a League (today's ``levels``) and derives a
``league_id`` for every SeasonTeamRegistration. ADR 0001 requires the upgrade to
*abort and report* any row it cannot map from a validated, same-Season chain
rather than guess. Because these setup relationships still have no DB foreign
keys, a non-null ``level_id``/``division_id`` is not proof of a valid derivation:
legacy/directly-loaded rows can dangle or point across Seasons. This suite covers
the read-only preflight that finds those rows — it lands before the reparent
migration (C1b), which will register ``assert_competition_reset_ready`` as its
pre-migration gate.

Proven on SQLite and (when ``TEST_DATABASE_URL`` is set) PostgreSQL:
 - a fresh install and any deterministic dataset (valid parents) pass cleanly;
 - level-less Divisions whose Season has 0/>1 leagues are reported;
 - dangling and cross-season Division→Level references are reported;
 - registrations with dangling/cross-season Division or Level are reported;
 - underivable (no single league) registrations are reported;
 - a registration resolving via a validated same-Season Division→Level is not;
 - the abort names the offending rows with reason + parent ids, and leaves every
   row byte-for-byte unchanged (full ordered snapshot, not only counts).

Scope note: this preflight validates the competition reparent chain only
(Division→Season/Level, Registration→Season/Division/Level). Team→Program and
other reference integrity is validated separately in C1b; fixtures here still
create real parent rows so the gate is never fed invalid input.
"""

import os
import unittest

from helpers import BACKEND, suspend_program_org_fks  # noqa: F401

from hockey_scheduler.store import SqlStore
from hockey_scheduler.store.integrity_checks import (
    MigrationDataError,
    assert_competition_reset_ready,
    find_underivable_registration_leagues,
    find_undeterminable_division_leagues,
)


def _sql_backends():
    backends = [("sqlite", ":memory:")]
    url = os.environ.get("TEST_DATABASE_URL")
    if url:
        backends.append(("postgres", url))
    return backends


def _downgrade_to_pre028(store):
    """Reverse the competition-model migrations so the pre-migration gate queries
    the exact PRE-028 schema it runs against in production — the ``levels``
    grouping table (with ``season_id``), ``divisions.level_id``, and
    ``season_team_registrations.season_id``. The gate only reads
    divisions/levels/seasons/season_team_registrations, so those are the only
    names that must be restored.

    035 (#283) now runs at the end of the chain: it folded the divisions and
    registration ``season_id``/``league_id`` into ``league_season_id`` and made
    ``leagues`` a permanent Program child (``program_id``, no ``season_id``).
    Reverse those first, then finish reversing 028's rename of the promoted
    grouping back to ``levels``. This runs on a freshly-migrated (empty)
    database, so only the SCHEMA is reversed."""
    cur = store.conn.cursor()
    # -- reverse 035 (#283) back to the post-028 competition shape --
    # 050 is a later child of the hierarchy being rewound. PostgreSQL requires
    # the dependent table to go before league_seasons can be removed; these
    # read-only preflight tests never exercise later scenario persistence.
    cur.execute("DROP TABLE IF EXISTS schedule_scenarios")
    cur.execute("DROP INDEX IF EXISTS ux_team_league_season")
    cur.execute("DROP INDEX IF EXISTS ix_reg_league_season_division")
    cur.execute("ALTER TABLE season_team_registrations DROP COLUMN league_season_id")
    cur.execute("ALTER TABLE season_team_registrations ADD COLUMN season_id TEXT")
    cur.execute("DROP INDEX IF EXISTS ix_divisions_league_season")
    cur.execute("ALTER TABLE divisions DROP COLUMN league_season_id")
    cur.execute("ALTER TABLE divisions ADD COLUMN season_id TEXT")
    cur.execute("ALTER TABLE divisions ADD COLUMN level_id TEXT")
    cur.execute("DROP INDEX IF EXISTS ix_leagues_program")
    cur.execute("ALTER TABLE leagues DROP COLUMN program_id")
    cur.execute("ALTER TABLE leagues ADD COLUMN season_id TEXT")
    cur.execute("DROP INDEX IF EXISTS ux_league_season")
    cur.execute("DROP TABLE IF EXISTS league_seasons")
    # -- finish reversing 028: the promoted grouping is `levels` pre-028 --
    cur.execute("DROP INDEX IF EXISTS ix_leagues_external_ref")
    cur.execute("ALTER TABLE leagues RENAME TO levels")


def _fresh(url):
    store = SqlStore(url)
    if url != ":memory:":
        store.reset_schema()
    # Migration 042's seasons.program_id → programs FK would reject the legacy
    # dangling rows these pre-028 gate tests plant; suspend it (and its siblings)
    # so the pre-042 data can be modeled (#201 Slice 4). Must run BEFORE the
    # downgrade renames leagues/programs away, so PostgreSQL can drop the named
    # constraints while the tables still carry their canonical names.
    suspend_program_org_fks(store)
    _downgrade_to_pre028(store)
    return store


def _exec(store, sql, params):
    store.conn.cursor().execute(store.dialect.sql(sql), params)


def _season(store, sid, program="prog"):
    _exec(store, "INSERT INTO seasons (id, program_id, name) VALUES (?, ?, ?)",
          (sid, program, sid))


def _level(store, lid, sid):
    _exec(store, "INSERT INTO levels (id, season_id, name) VALUES (?, ?, ?)",
          (lid, sid, lid))


def _division(store, did, sid, level_id=None):
    _exec(store, "INSERT INTO divisions (id, season_id, name, level_id) "
          "VALUES (?, ?, ?, ?)", (did, sid, did, level_id))


def _registration(store, rid, sid, division_id=None):
    tid = "t_" + rid
    _exec(store, "INSERT INTO teams (id, name) VALUES (?, ?)", (tid, tid))
    _exec(store, "INSERT INTO season_team_registrations "
          "(id, season_id, team_id, division_id, active) VALUES (?, ?, ?, ?, 1)",
          (rid, sid, tid, division_id))


def _snapshot(store):
    """Full ordered row snapshot of the three tables the reparent touches."""
    specs = {
        "divisions": ["id", "season_id", "name", "age_group", "level_id",
                      "external_ref"],
        "levels": ["id", "season_id", "name", "sort_order", "external_ref"],
        "season_team_registrations": ["id", "season_id", "team_id", "division_id",
                                      "active"],
    }
    cur = store.conn.cursor()
    out = {}
    for table, cols in specs.items():
        cur.execute(f"SELECT {', '.join(cols)} FROM {table} ORDER BY id")
        out[table] = [tuple(row[c] for c in cols) for row in cur.fetchall()]
    return out


class CompetitionResetPreflightTest(unittest.TestCase):
    # -- clean paths --------------------------------------------------------
    def test_fresh_install_is_clean(self):
        for label, url in _sql_backends():
            with self.subTest(backend=label):
                store = _fresh(url)
                self.assertEqual(find_undeterminable_division_leagues(store.conn), [])
                self.assertEqual(find_underivable_registration_leagues(store.conn), [])
                assert_competition_reset_ready(store.conn)  # no raise

    def test_deterministic_dataset_passes(self):
        for label, url in _sql_backends():
            with self.subTest(backend=label):
                store = _fresh(url)
                _season(store, "s1")
                _level(store, "l1", "s1")
                _division(store, "d_leveled", "s1", level_id="l1")
                _division(store, "d_levelless", "s1", level_id=None)  # sole league
                _registration(store, "r_div", "s1", division_id="d_leveled")
                _registration(store, "r_nodiv", "s1", division_id=None)
                self.assertEqual(find_undeterminable_division_leagues(store.conn), [])
                self.assertEqual(find_underivable_registration_leagues(store.conn), [])
                assert_competition_reset_ready(store.conn)  # no raise

    def test_registration_with_same_season_leveled_division_is_derivable(self):
        for label, url in _sql_backends():
            with self.subTest(backend=label):
                store = _fresh(url)
                _season(store, "s_multi")
                _level(store, "la", "s_multi")
                _level(store, "lb", "s_multi")
                _division(store, "d_leveled", "s_multi", level_id="la")
                _registration(store, "r_ok", "s_multi", division_id="d_leveled")
                self.assertEqual(find_underivable_registration_leagues(store.conn), [])
                assert_competition_reset_ready(store.conn)  # no raise

    # -- ambiguous (0/>1 league) --------------------------------------------
    def test_level_less_division_in_ambiguous_season_is_reported(self):
        for label, url in _sql_backends():
            with self.subTest(backend=label):
                store = _fresh(url)
                _season(store, "s_multi")
                _level(store, "la", "s_multi")
                _level(store, "lb", "s_multi")
                _division(store, "d_multi", "s_multi", level_id=None)  # 2 leagues
                _season(store, "s_none")
                _division(store, "d_none", "s_none", level_id=None)   # 0 leagues
                _division(store, "d_ok", "s_multi", level_id="la")    # fine
                found = find_undeterminable_division_leagues(store.conn)
                self.assertEqual([d["division_id"] for d in found], ["d_multi", "d_none"])
                self.assertTrue(all(d["reason"] == "no_single_league" for d in found))
                with self.assertRaises(MigrationDataError) as ctx:
                    assert_competition_reset_ready(store.conn)
                msg = str(ctx.exception)
                self.assertIn("d_multi", msg)
                self.assertIn("d_none", msg)
                self.assertIn("no_single_league", msg)

    def test_underivable_registration_is_reported(self):
        for label, url in _sql_backends():
            with self.subTest(backend=label):
                store = _fresh(url)
                _season(store, "s_multi")
                _level(store, "la", "s_multi")
                _level(store, "lb", "s_multi")
                _registration(store, "r_nodiv", "s_multi", division_id=None)
                found = find_underivable_registration_leagues(store.conn)
                self.assertEqual([r["registration_id"] for r in found], ["r_nodiv"])
                self.assertEqual(found[0]["reason"], "no_single_league")
                with self.assertRaises(MigrationDataError) as ctx:
                    assert_competition_reset_ready(store.conn)
                self.assertIn("registration", str(ctx.exception))

    # -- invalid parent chains (the false-pass cases) -----------------------
    def test_dangling_division_level_is_reported(self):
        for label, url in _sql_backends():
            with self.subTest(backend=label):
                store = _fresh(url)
                _season(store, "s1")
                _level(store, "l1", "s1")            # season has a sole league…
                _division(store, "d_dangle", "s1", level_id="ghost")  # …but level_id dangles
                found = find_undeterminable_division_leagues(store.conn)
                self.assertEqual([d["division_id"] for d in found], ["d_dangle"])
                self.assertEqual(found[0]["reason"], "dangling_level")
                with self.assertRaises(MigrationDataError) as ctx:
                    assert_competition_reset_ready(store.conn)
                self.assertIn("dangling_level", str(ctx.exception))

    def test_cross_season_division_level_is_reported(self):
        for label, url in _sql_backends():
            with self.subTest(backend=label):
                store = _fresh(url)
                _season(store, "s_a")
                _season(store, "s_b")
                _level(store, "l_b", "s_b")
                _division(store, "d_x", "s_a", level_id="l_b")  # level from another season
                found = find_undeterminable_division_leagues(store.conn)
                self.assertEqual([d["division_id"] for d in found], ["d_x"])
                self.assertEqual(found[0]["reason"], "cross_season_level")
                with self.assertRaises(MigrationDataError) as ctx:
                    assert_competition_reset_ready(store.conn)
                msg = str(ctx.exception)
                self.assertIn("d_x", msg)
                self.assertIn("cross_season_level", msg)

    def test_missing_division_season_is_reported(self):
        for label, url in _sql_backends():
            with self.subTest(backend=label):
                store = _fresh(url)
                _division(store, "d_orphan", "ghost_season", level_id=None)
                found = find_undeterminable_division_leagues(store.conn)
                self.assertEqual([d["division_id"] for d in found], ["d_orphan"])
                self.assertEqual(found[0]["reason"], "missing_season")

    def test_registration_dangling_division_is_reported(self):
        for label, url in _sql_backends():
            with self.subTest(backend=label):
                store = _fresh(url)
                _season(store, "s1")
                _level(store, "l1", "s1")  # sole league — a null division would be fine
                _registration(store, "r_dangle", "s1", division_id="ghost")
                found = find_underivable_registration_leagues(store.conn)
                self.assertEqual([r["registration_id"] for r in found], ["r_dangle"])
                self.assertEqual(found[0]["reason"], "dangling_division")

    def test_registration_cross_season_leveled_division_is_reported(self):
        for label, url in _sql_backends():
            with self.subTest(backend=label):
                store = _fresh(url)
                _season(store, "s_a")
                _level(store, "l_a", "s_a")
                _season(store, "s_b")
                _level(store, "l_b", "s_b")
                _division(store, "d_b", "s_b", level_id="l_b")  # a leveled division in s_b
                _registration(store, "r_x", "s_a", division_id="d_b")  # used from s_a
                found = find_underivable_registration_leagues(store.conn)
                self.assertEqual([r["registration_id"] for r in found], ["r_x"])
                self.assertEqual(found[0]["reason"], "cross_season_division")
                # The diagnostic carries the division's actual Level id, not None.
                self.assertEqual(found[0]["level_id"], "l_b")
                with self.assertRaises(MigrationDataError) as ctx:
                    assert_competition_reset_ready(store.conn)
                msg = str(ctx.exception)
                self.assertIn("r_x", msg)
                self.assertIn("level=l_b", msg)
                self.assertIn("leagues_in_season=1", msg)  # s_a has the sole level l_a

    def test_registration_via_division_with_dangling_level_is_reported(self):
        for label, url in _sql_backends():
            with self.subTest(backend=label):
                store = _fresh(url)
                _season(store, "s1")
                _level(store, "l1", "s1")  # season has a sole league…
                _division(store, "d_dl", "s1", level_id="ghost")  # …div's level dangles
                _registration(store, "r_dl", "s1", division_id="d_dl")
                found = find_underivable_registration_leagues(store.conn)
                self.assertEqual([r["registration_id"] for r in found], ["r_dl"])
                self.assertEqual(found[0]["reason"], "dangling_level")
                self.assertEqual(found[0]["level_id"], "ghost")
                with self.assertRaises(MigrationDataError) as ctx:
                    assert_competition_reset_ready(store.conn)
                msg = str(ctx.exception)
                self.assertIn("r_dl", msg)
                self.assertIn("reason=dangling_level", msg)

    def test_registration_via_division_with_cross_season_level_is_reported(self):
        for label, url in _sql_backends():
            with self.subTest(backend=label):
                store = _fresh(url)
                _season(store, "s1")
                _level(store, "l1", "s1")   # s1's sole league
                _season(store, "s2")
                _level(store, "l2", "s2")   # a level belonging to another season
                _division(store, "d_cl", "s1", level_id="l2")  # same-season div, cross-season level
                _registration(store, "r_cl", "s1", division_id="d_cl")
                found = find_underivable_registration_leagues(store.conn)
                self.assertEqual([r["registration_id"] for r in found], ["r_cl"])
                self.assertEqual(found[0]["reason"], "cross_season_level")
                self.assertEqual(found[0]["level_id"], "l2")
                with self.assertRaises(MigrationDataError) as ctx:
                    assert_competition_reset_ready(store.conn)
                msg = str(ctx.exception)
                self.assertIn("r_cl", msg)
                self.assertIn("level=l2", msg)
                self.assertIn("cross_season_level", msg)

    # -- diagnostics + read-only guarantee ----------------------------------
    def test_abort_text_includes_reason_and_parent_ids(self):
        for label, url in _sql_backends():
            with self.subTest(backend=label):
                store = _fresh(url)
                _season(store, "s1")
                _level(store, "l1", "s1")
                _division(store, "d_dangle", "s1", level_id="ghost")
                with self.assertRaises(MigrationDataError) as ctx:
                    assert_competition_reset_ready(store.conn)
                msg = str(ctx.exception)
                self.assertIn("division d_dangle", msg)
                self.assertIn("season=s1", msg)
                self.assertIn("level=ghost", msg)
                self.assertIn("reason=dangling_level", msg)

    def test_abort_leaves_full_row_snapshots_unchanged(self):
        for label, url in _sql_backends():
            with self.subTest(backend=label):
                store = _fresh(url)
                _season(store, "s_none")
                _level(store, "l_stray", "s_other")     # a stray level row
                _division(store, "d_none", "s_none", level_id=None)
                _registration(store, "r_none", "s_none", division_id=None)
                before = _snapshot(store)
                with self.assertRaises(MigrationDataError):
                    assert_competition_reset_ready(store.conn)
                self.assertEqual(_snapshot(store), before)


if __name__ == "__main__":
    unittest.main()
