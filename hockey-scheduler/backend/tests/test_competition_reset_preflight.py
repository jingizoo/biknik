"""Competition-model reset — migration preflight (#233 Slice C1a).

Slice C reparents each Division onto a League (today's ``levels``) and derives a
``league_id`` for every SeasonTeamRegistration. ADR 0001 requires the upgrade to
*abort and report* rows it cannot map deterministically rather than guess. This
suite covers the read-only preflight that finds those ambiguous rows — it lands
before the reparent migration (C1b), which will register
``assert_competition_reset_ready`` as its pre-migration gate.

Proven on SQLite and (when ``TEST_DATABASE_URL`` is set) PostgreSQL:
 - a fresh install and any deterministic dataset pass cleanly;
 - a level-less Division whose season has 0 or >1 leagues is reported;
 - a registration whose league can't be uniquely derived is reported;
 - a registration whose division carries a level is derivable (not reported);
 - the check is read-only — an abort leaves every row unchanged.
"""

import os
import unittest

from helpers import BACKEND  # noqa: F401  (ensures sys.path is set up)

from hockey_scheduler.domain.setup_models import (
    Division, Level, Season, SeasonTeamRegistration)
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


def _fresh(url):
    store = SqlStore(url)
    if url != ":memory:":
        store.reset_schema()
    return store


def _season(store, sid):
    store.add_season(Season(id=sid, league_id="prog", name=sid))


def _level(store, lid, sid):
    store.add_level(Level(id=lid, season_id=sid, name=lid))


def _division(store, did, sid, level_id=None):
    store.add_division(Division(id=did, season_id=sid, name=did, level_id=level_id))


def _registration(store, rid, sid, division_id=None):
    store.add_season_team_registration(SeasonTeamRegistration(
        id=rid, season_id=sid, team_id="t_" + rid, division_id=division_id))


def _row_counts(store):
    cur = store.conn.cursor()
    counts = {}
    for table in ("divisions", "levels", "season_team_registrations"):
        cur.execute(f"SELECT COUNT(*) AS n FROM {table}")
        counts[table] = cur.fetchone()["n"]
    return counts


class CompetitionResetPreflightTest(unittest.TestCase):
    def test_fresh_install_is_clean(self):
        for label, url in _sql_backends():
            with self.subTest(backend=label):
                store = _fresh(url)
                self.assertEqual(find_undeterminable_division_leagues(store.conn), [])
                self.assertEqual(find_underivable_registration_leagues(store.conn), [])
                # No raise on an empty database.
                assert_competition_reset_ready(store.conn)

    def test_deterministic_dataset_passes(self):
        # A season with exactly one level: a level-less division attaches to that
        # sole league, and registrations resolve either via their division's
        # level or via the season's single league.
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

    def test_level_less_division_in_ambiguous_season_is_reported(self):
        for label, url in _sql_backends():
            with self.subTest(backend=label):
                store = _fresh(url)
                _season(store, "s_multi")
                _level(store, "la", "s_multi")
                _level(store, "lb", "s_multi")
                _division(store, "d_multi", "s_multi", level_id=None)  # 2 leagues → ambiguous
                _season(store, "s_none")  # no levels at all
                _division(store, "d_none", "s_none", level_id=None)   # 0 leagues → ambiguous
                # A leveled division under the multi-league season is fine.
                _division(store, "d_ok", "s_multi", level_id="la")

                found = find_undeterminable_division_leagues(store.conn)
                self.assertEqual([d[0] for d in found], ["d_multi", "d_none"])
                with self.assertRaises(MigrationDataError) as ctx:
                    assert_competition_reset_ready(store.conn)
                msg = str(ctx.exception)
                self.assertIn("d_multi", msg)
                self.assertIn("d_none", msg)
                self.assertIn("division", msg)

    def test_underivable_registration_is_reported(self):
        for label, url in _sql_backends():
            with self.subTest(backend=label):
                store = _fresh(url)
                _season(store, "s_multi")
                _level(store, "la", "s_multi")
                _level(store, "lb", "s_multi")
                # No division + >1 league → league can't be derived.
                _registration(store, "r_nodiv", "s_multi", division_id=None)
                # Division without a level + >1 league → still underivable.
                _division(store, "d_flat", "s_multi", level_id=None)
                _registration(store, "r_flatdiv", "s_multi", division_id="d_flat")

                found = find_underivable_registration_leagues(store.conn)
                self.assertEqual(
                    sorted(r[0] for r in found), ["r_flatdiv", "r_nodiv"])
                with self.assertRaises(MigrationDataError) as ctx:
                    assert_competition_reset_ready(store.conn)
                self.assertIn("registration", str(ctx.exception))

    def test_registration_with_leveled_division_is_derivable(self):
        for label, url in _sql_backends():
            with self.subTest(backend=label):
                store = _fresh(url)
                _season(store, "s_multi")
                _level(store, "la", "s_multi")
                _level(store, "lb", "s_multi")
                _division(store, "d_leveled", "s_multi", level_id="la")
                # A registration whose division carries a level resolves even when
                # the season has several leagues.
                _registration(store, "r_ok", "s_multi", division_id="d_leveled")
                self.assertEqual(find_underivable_registration_leagues(store.conn), [])
                assert_competition_reset_ready(store.conn)  # no raise

    def test_abort_leaves_data_unchanged(self):
        for label, url in _sql_backends():
            with self.subTest(backend=label):
                store = _fresh(url)
                _season(store, "s_none")
                _division(store, "d_none", "s_none", level_id=None)
                _registration(store, "r_none", "s_none", division_id=None)
                before = _row_counts(store)
                with self.assertRaises(MigrationDataError):
                    assert_competition_reset_ready(store.conn)
                self.assertEqual(_row_counts(store), before)


if __name__ == "__main__":
    unittest.main()
