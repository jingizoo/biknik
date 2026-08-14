"""Age-eligibility rule writes: Season guard, orphan deletion, version
serialization (#273 review round 2 finding 3).

Three holes in the original ``set_age_eligibility_rule()`` / ``delete_
league_season()``:

* ``set_age_eligibility_rule()`` read the LeagueSeason with a plain
  UNLOCKED ``get_league_season()`` and never called ``_require_active_
  season`` at all -- a rule could be appended to an ARCHIVED Season's
  frozen history.
* ``delete_league_season()`` never scanned ``age_eligibility_rules`` as a
  dependent -- deleting a LeagueSeason left its rule history pointing at a
  binding that no longer existed.
* ``version = existing[-1].version + 1 if existing else 1`` was computed
  with no lock held -- two concurrent writers could read the same
  ``existing`` list and race for the same next version.

The fix locks the OWNING Season first (mirroring ``create_league_season``/
``delete_league_season``'s identical Season-first lock order), re-reads the
LeagueSeason under that lock, and keeps the lock held through the version
computation -- closing the archived-write hole AND serializing version
assignment in one mechanism, since a second writer for the SAME
league_season_id blocks on the SAME Season row until the first commits.
``delete_league_season`` now includes rule rows in its itemized dependency
gate, and migration 051 adds a NOT NULL + FK backstop at the database level.

Covers, on Memory/SQLite/[PostgreSQL]:

* an archived-Season write is refused with zero rows and zero audits;
* archive-vs-set and delete-vs-set races in BOTH orders (PostgreSQL, real
  row locks; Memory/SQLite carry the documented sequential parity — see
  test_season_lifecycle_concurrency.py's identical convention);
* delete is dependency-blocked while a rule exists (no orphan possible
  through the service);
* raw FK/NOT NULL probes directly against the SQL schema;
* two concurrent writers append CONSECUTIVE versions (never a collision,
  never a lost write) with exactly one audit each.
"""

import os
import threading
import unittest
from datetime import datetime, timezone

from helpers import BACKEND  # noqa: F401  (ensures sys.path is set up)

from hockey_scheduler.api import ApiService
from hockey_scheduler.domain import (
    Division, League, LeagueSeason, Program, Season, Team)
from hockey_scheduler.domain.enums import SeasonStatus
from hockey_scheduler.domain.errors import (
    HasDependenciesError, IntegrityConflictError, NotFoundError,
    ValidationError)
from hockey_scheduler.store import InMemoryStore, SqlStore

UTC = timezone.utc
TIERS = [{"code": "U10", "max_age": 10}, {"code": "U13", "max_age": 13}]


def _backends():
    stores = [("memory", InMemoryStore()), ("sqlite", SqlStore(":memory:"))]
    url = os.environ.get("TEST_DATABASE_URL")
    if url:
        pg = SqlStore(url)
        pg.reset_schema()
        stores.append(("postgres", pg))
    return stores


def _seed(store, season_name="S1", start=None):
    api = ApiService(store)
    program = api.create_program("Prog", "US", "UTC")
    season = api.create_season(program["id"], season_name)
    league = api.create_league(season["id"], "Gold")
    ls_id = next(ls.id for ls in store.all_league_seasons()
                if ls.season_id == season["id"])
    return api, season["id"], ls_id


def _rule_audits(store, ls_id=None):
    rows = [a for a in store.all_setup_audit()
           if a.action == "age_eligibility_rule_set"]
    if ls_id is not None:
        rows = [a for a in rows if a.detail.get("league_season_id") == ls_id]
    return rows


class ArchivedSeasonWriteTest(unittest.TestCase):
    """set_age_eligibility_rule() must fail closed on an archived Season,
    exactly like every other Season-owned write, with ZERO rows and ZERO
    audits -- not merely a caught error somewhere downstream."""

    def test_archived_season_write_is_refused_zero_rows_zero_audits(self):
        for label, store in _backends():
            with self.subTest(backend=label):
                api, season_id, ls_id = _seed(store)
                api.setup.archive_season(season_id, actor_id="op",
                                         reason="year end")
                with self.assertRaises(ValidationError, msg=label) as ctx:
                    api.setup.set_age_eligibility_rule(
                        ls_id, 12, 31, TIERS, actor_id="op")
                self.assertEqual(ctx.exception.details["reason"],
                                 "season_archived", label)
                self.assertEqual(
                    store.age_eligibility_rules_for_league_season(ls_id),
                    [], label)
                self.assertEqual(_rule_audits(store, ls_id), [], label)
                if isinstance(store, SqlStore):
                    store.close()

    def test_active_season_write_still_works(self):
        """Positive control: the guard does not over-block ordinary writes."""
        for label, store in _backends():
            with self.subTest(backend=label):
                api, season_id, ls_id = _seed(store)
                rule = api.setup.set_age_eligibility_rule(
                    ls_id, 12, 31, TIERS, actor_id="op")
                self.assertEqual(rule.version, 1, label)
                if isinstance(store, SqlStore):
                    store.close()

    def test_missing_league_season_still_not_found(self):
        """The new Season-lock path must not turn a missing binding into
        a different error."""
        for label, store in _backends():
            with self.subTest(backend=label):
                with self.assertRaises(NotFoundError, msg=label):
                    ApiService(store).setup.set_age_eligibility_rule(
                        "ghost-ls", 12, 31, TIERS, actor_id="op")
                if isinstance(store, SqlStore):
                    store.close()


class OrphanDeletionTest(unittest.TestCase):
    """delete_league_season() must refuse to delete a binding that still
    owns rule history -- no orphan is reachable through the service."""

    def test_delete_blocked_while_a_rule_exists(self):
        for label, store in _backends():
            with self.subTest(backend=label):
                api, season_id, ls_id = _seed(store)
                api.setup.set_age_eligibility_rule(
                    ls_id, 12, 31, TIERS, actor_id="op")
                with self.assertRaises(HasDependenciesError, msg=label) as ctx:
                    api.setup.delete_league_season(ls_id, actor_id="op")
                groups = ctx.exception.details["dependencies"]
                rule_group = next(g for g in groups
                                  if g["type"] == "age eligibility rule")
                self.assertEqual(rule_group["count"], 1, label)
                # The binding survives, unorphaned.
                self.assertIsNotNone(store.get_league_season(ls_id), label)
                self.assertEqual(
                    len(store.age_eligibility_rules_for_league_season(ls_id)),
                    1, label)
                if isinstance(store, SqlStore):
                    store.close()

    def test_delete_of_a_ruleless_binding_still_succeeds(self):
        """Positive control: the new dependency check does not over-block a
        binding that genuinely has no rule history."""
        for label, store in _backends():
            with self.subTest(backend=label):
                api, season_id, ls_id = _seed(store)
                res = api.setup.delete_league_season(ls_id, actor_id="op")
                self.assertTrue(res["deleted"], label)
                self.assertIsNone(store.get_league_season(ls_id), label)
                if isinstance(store, SqlStore):
                    store.close()


# --------------------------------------------------------------------------- #
# Raw SQL constraint probes: NOT NULL + FK, bypassing the service entirely.   #
# --------------------------------------------------------------------------- #
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


class RawConstraintProbeTest(unittest.TestCase):
    def _insert_rule(self, store, **overrides):
        cols = dict(id="r1", league_season_id="ls1", version=1,
                    cutoff_month=12, cutoff_day=31, tiers="[]",
                    enforcement="warn", created_at="2026-01-01T00:00:00",
                    actor_id=None)
        cols.update(overrides)
        names = ", ".join(cols)
        placeholders = ", ".join(["?"] * len(cols))
        cur = store.conn.cursor()
        cur.execute(store.dialect.sql(
            f"INSERT INTO age_eligibility_rules ({names}) "
            f"VALUES ({placeholders})"), tuple(cols.values()))

    def test_null_required_columns_are_rejected(self):
        for label, url in _sql_backends():
            store = _fresh(url)
            try:
                store.add_program(Program(id="p", name="P"))
                store.add_season(Season(id="s", program_id="p", name="S"))
                store.add_league(League(id="lg", program_id="p", name="L"))
                store.add_league_season(
                    LeagueSeason(id="ls1", league_id="lg", season_id="s"))
                for column in ("league_season_id", "version", "cutoff_month",
                              "cutoff_day", "tiers", "enforcement",
                              "created_at"):
                    with self.assertRaises(Exception, msg=(label, column)):
                        with store.transaction():
                            self._insert_rule(store, **{column: None})
            finally:
                store.close()

    def test_actor_id_stays_nullable(self):
        """Positive control: actor_id is a legitimate unattributed system
        write, like every other actor column in this schema."""
        for label, url in _sql_backends():
            store = _fresh(url)
            try:
                store.add_program(Program(id="p", name="P"))
                store.add_season(Season(id="s", program_id="p", name="S"))
                store.add_league(League(id="lg", program_id="p", name="L"))
                store.add_league_season(
                    LeagueSeason(id="ls1", league_id="lg", season_id="s"))
                with store.transaction():
                    self._insert_rule(store, actor_id=None)
                self.assertEqual(
                    len(store.age_eligibility_rules_for_league_season("ls1")),
                    1, label)
            finally:
                store.close()

    def test_foreign_key_to_a_missing_league_season_is_rejected(self):
        for label, url in _sql_backends():
            store = _fresh(url)
            try:
                with self.assertRaises(Exception, msg=label):
                    with store.transaction():
                        self._insert_rule(store,
                                          league_season_id="does-not-exist")
            finally:
                store.close()

    def test_deleting_a_referenced_league_season_directly_is_rejected(self):
        """The FK is a RESTRICTIVE backstop: even a raw SQL delete that
        bypasses the service's dependency gate cannot orphan a rule row."""
        for label, url in _sql_backends():
            store = _fresh(url)
            try:
                store.add_program(Program(id="p", name="P"))
                store.add_season(Season(id="s", program_id="p", name="S"))
                store.add_league(League(id="lg", program_id="p", name="L"))
                store.add_league_season(
                    LeagueSeason(id="ls1", league_id="lg", season_id="s"))
                with store.transaction():
                    self._insert_rule(store)
                with self.assertRaises(Exception, msg=label):
                    with store.transaction():
                        cur = store.conn.cursor()
                        cur.execute(store.dialect.sql(
                            "DELETE FROM league_seasons WHERE id = ?"),
                            ("ls1",))
                # Untouched: the delete rolled back.
                self.assertIsNotNone(store.get_league_season("ls1"), label)
            finally:
                store.close()


# =========================================================================== #
# Memory/SQLite sequential parity for version serialization.                  #
# =========================================================================== #
class VersionSerializationParityTest(unittest.TestCase):
    def test_two_writers_get_consecutive_versions_one_audit_each(self):
        for label, store in [("memory", InMemoryStore()),
                             ("sqlite", SqlStore(":memory:"))]:
            with self.subTest(backend=label):
                api, season_id, ls_id = _seed(store)
                r1 = api.setup.set_age_eligibility_rule(
                    ls_id, 12, 31, TIERS, actor_id="a")
                r2 = api.setup.set_age_eligibility_rule(
                    ls_id, 8, 31, TIERS, actor_id="b")
                self.assertEqual((r1.version, r2.version), (1, 2), label)
                self.assertEqual(len(_rule_audits(store, ls_id)), 2, label)
                if isinstance(store, SqlStore):
                    store.close()


# =========================================================================== #
# Real two-connection PostgreSQL races.                                       #
# =========================================================================== #
@unittest.skipUnless(os.environ.get("TEST_DATABASE_URL"),
                     "cross-connection race needs PostgreSQL")
class PostgresRaceTest(unittest.TestCase):
    def setUp(self):
        self.url = os.environ["TEST_DATABASE_URL"]
        SqlStore(self.url).reset_schema()

    def _run(self, fn, key, results, barrier):
        try:
            barrier.wait(timeout=10)
            fn()
            results[key] = "ok"
        except ValidationError as exc:
            results[key] = exc.details.get("reason") or "validation"
        except HasDependenciesError:
            results[key] = "has_dependencies"
        except NotFoundError:
            results[key] = "not_found"
        except Exception as exc:  # pragma: no cover
            results[key] = f"ERR:{exc!r}"

    def test_archive_vs_set_archive_first_or_set_first_both_valid(self):
        """Either a valid linearization: archive wins and set fails
        season_archived with zero rows, OR set commits first (while still
        active) and archive then wins regardless -- NEVER a rule silently
        landing on an already-archived Season."""
        seed = SqlStore(self.url)
        api, season_id, ls_id = _seed(seed)
        seed.close()
        api_arch = ApiService(SqlStore(self.url))
        api_set = ApiService(SqlStore(self.url))
        barrier = threading.Barrier(2)
        results = {}
        ta = threading.Thread(target=self._run, args=(
            lambda: api_arch.setup.archive_season(
                season_id, actor_id="a", reason="race"),
            "archive", results, barrier))
        tb = threading.Thread(target=self._run, args=(
            lambda: api_set.setup.set_age_eligibility_rule(
                ls_id, 12, 31, TIERS, actor_id="b"),
            "set", results, barrier))
        ta.start(); tb.start(); ta.join(15); tb.join(15)

        self.assertEqual(results["archive"], "ok", results)
        check = SqlStore(self.url)
        rules = check.age_eligibility_rules_for_league_season(ls_id)
        season = check.get_season(season_id)
        self.assertEqual(season.status, SeasonStatus.ARCHIVED, results)
        if results["set"] == "ok":
            # set-first ordering: it committed while still active.
            self.assertEqual(len(rules), 1, results)
        else:
            # archive-first ordering: set observed the archive and lost.
            self.assertEqual(results["set"], "season_archived", results)
            self.assertEqual(rules, [], results)
        check.close()

    def test_delete_vs_set_delete_first_or_set_first_both_valid(self):
        """Either valid linearization: delete wins (binding gone, set fails
        not_found with zero rows) OR set commits first and delete then
        blocks on the itemized dependency it just created -- NEVER a rule
        landing on a deleted binding."""
        seed = SqlStore(self.url)
        api, season_id, ls_id = _seed(seed)
        seed.close()
        api_del = ApiService(SqlStore(self.url))
        api_set = ApiService(SqlStore(self.url))
        barrier = threading.Barrier(2)
        results = {}
        ta = threading.Thread(target=self._run, args=(
            lambda: api_del.setup.delete_league_season(ls_id, actor_id="a"),
            "delete", results, barrier))
        tb = threading.Thread(target=self._run, args=(
            lambda: api_set.setup.set_age_eligibility_rule(
                ls_id, 12, 31, TIERS, actor_id="b"),
            "set", results, barrier))
        ta.start(); tb.start(); ta.join(15); tb.join(15)

        check = SqlStore(self.url)
        if results["delete"] == "ok":
            # delete-first: the binding is gone; set must have lost.
            self.assertIn(results["set"], ("not_found",), results)
            self.assertIsNone(check.get_league_season(ls_id), results)
        else:
            # set-first: the rule committed, delete now blocks on it.
            self.assertEqual(results["set"], "ok", results)
            self.assertEqual(results["delete"], "has_dependencies", results)
            self.assertIsNotNone(check.get_league_season(ls_id), results)
            self.assertEqual(
                len(check.age_eligibility_rules_for_league_season(ls_id)),
                1, results)
        check.close()

    def test_two_concurrent_set_calls_get_consecutive_versions(self):
        seed = SqlStore(self.url)
        api, season_id, ls_id = _seed(seed)
        seed.close()
        api_a = ApiService(SqlStore(self.url))
        api_b = ApiService(SqlStore(self.url))
        barrier = threading.Barrier(2)
        results = {}
        ta = threading.Thread(target=self._run, args=(
            lambda: api_a.setup.set_age_eligibility_rule(
                ls_id, 12, 31, TIERS, actor_id="a"),
            "a", results, barrier))
        tb = threading.Thread(target=self._run, args=(
            lambda: api_b.setup.set_age_eligibility_rule(
                ls_id, 8, 31, TIERS, actor_id="b"),
            "b", results, barrier))
        ta.start(); tb.start(); ta.join(15); tb.join(15)

        self.assertEqual(results["a"], "ok", results)
        self.assertEqual(results["b"], "ok", results)
        check = SqlStore(self.url)
        rules = check.age_eligibility_rules_for_league_season(ls_id)
        self.assertEqual(sorted(r.version for r in rules), [1, 2], results)
        audits = _rule_audits(check, ls_id)
        self.assertEqual(len(audits), 2, results)  # one each, no loser
        check.close()


if __name__ == "__main__":
    unittest.main()
