"""Every live table is covered by the subtree-deletion graph lock (#454).

Since #453, ``SubtreeDeletionService.execute`` runs at READ COMMITTED and its
consistency rests on ``SqlStore.lock_subtree_graph`` holding EXCLUSIVE on
every table that carries graph state. That lock is built from ``SPECS``, and
so is ``test_graph_lock_statement_is_complete_and_nonblocking``'s expectation,
so that test cannot notice a table that exists in the database but has no
Spec. Tables have been added that way before — raw SQL, no Spec.

This compares ``SPECS`` against the LIVE catalog instead. A table that is in
neither ``SPECS`` nor ``NOT_GRAPH_STATE`` fails the build, so adding one is a
written decision: give it a Spec (and therefore the lock), or record here why
it holds no graph state.
"""

import os
import tempfile
import unittest

from helpers import BACKEND, fresh_sql_store  # noqa: F401

from hockey_scheduler.store import SqlStore
from hockey_scheduler.store.sql_store import SPECS

# Tables that exist in the database, have no Spec, and are therefore OUTSIDE
# the graph lock. Each entry must say why that is safe.
NOT_GRAPH_STATE = {
    # (prefix, value). execute() does write it under the lock, via
    # next_id("setupaudit"), but a counter row references no graph row, so a
    # concurrent writer cannot make the deletion's view of the graph stale.
    "counters": "id sequence; rows reference nothing in the graph",
    # (fence_key, version). Bumped by execute() BEFORE the graph lock, as the
    # cross-replica context fence; it carries no graph rows.
    "epoch_fence_version": "context-epoch fence counter; no graph rows",
    # (version, applied_at). Never read or written by deletion.
    "schema_migrations": "migration ledger; not graph state",
}


def live_tables(store):
    if store.backend == "postgres":
        cur = store.conn.cursor()
        cur.execute("SELECT tablename FROM pg_tables "
                    "WHERE schemaname = current_schema()")
        rows = cur.fetchall()
        return {r["tablename"] if isinstance(r, dict) else r[0] for r in rows}
    rows = store.conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' "
        "AND name NOT LIKE 'sqlite_%'").fetchall()
    return {r["name"] for r in rows}


def unlocked_graph_candidates(store):
    """Live tables that are neither locked (Spec) nor declared non-graph."""
    spec_tables = {spec.table for spec in SPECS.values()}
    return live_tables(store) - spec_tables - set(NOT_GRAPH_STATE)


class _CatalogCoverageContract:
    """Shared assertions; subclasses provide ``self.store``."""

    def test_every_live_table_is_locked_or_declared(self):
        self.assertEqual(
            unlocked_graph_candidates(self.store), set(),
            "These tables exist but are outside lock_subtree_graph(). Give "
            "each a Spec, or add it to NOT_GRAPH_STATE with the reason it "
            "holds no graph state.")

    def test_every_spec_table_exists(self):
        spec_tables = {spec.table for spec in SPECS.values()}
        self.assertEqual(spec_tables - live_tables(self.store), set())

    def test_no_declared_exemption_is_stale(self):
        live = live_tables(self.store)
        spec_tables = {spec.table for spec in SPECS.values()}
        self.assertEqual(set(NOT_GRAPH_STATE) - live, set(),
                         "NOT_GRAPH_STATE names a table that no longer exists")
        self.assertEqual(set(NOT_GRAPH_STATE) & spec_tables, set(),
                         "NOT_GRAPH_STATE names a table that has a Spec and "
                         "is already locked")

    def test_an_unspecced_table_is_caught(self):
        # Falsifier: a table created the way counters was — raw SQL, no
        # Spec — must be reported, or the check above proves nothing.
        cur = self.store.conn.cursor()
        cur.execute("CREATE TABLE graph_side_table_454 (id TEXT PRIMARY KEY)")
        self.store.conn.commit()
        try:
            self.assertEqual(unlocked_graph_candidates(self.store),
                             {"graph_side_table_454"})
        finally:
            cur.execute("DROP TABLE graph_side_table_454")
            self.store.conn.commit()


class SqliteCatalogCoverageTest(_CatalogCoverageContract, unittest.TestCase):
    def setUp(self):
        fd, self._tmp = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.store = SqlStore(self._tmp)

    def tearDown(self):
        self.store.close()
        os.remove(self._tmp)


@unittest.skipUnless(os.environ.get("TEST_DATABASE_URL"),
                     "PostgreSQL suite only (TEST_DATABASE_URL not set)")
class PostgresCatalogCoverageTest(_CatalogCoverageContract, unittest.TestCase):
    def setUp(self):
        self.store = fresh_sql_store(os.environ["TEST_DATABASE_URL"])

    def tearDown(self):
        self.store.close()


if __name__ == "__main__":
    unittest.main()
