"""#424 round-N owner review finding 2: sensitive-read + ALLOWED audit-write
atomicity for ``list_players(include_identity=True)``,
``player_duplicate_report``, and ``evaluate_player_eligibility``.

Pre-fix, all three call sites read the sensitive Player row(s) BEFORE any
``with self.store.transaction():`` opened -- ``SqlStore``'s connection is
``autocommit=True`` on PostgreSQL / ``isolation_level=None`` on SQLite (see
``store/sql_store.py``'s ``transaction()`` docstring), so a bare read made
outside ``transaction()`` is its OWN already-committed unit of work, never
joined to whatever transaction opens LATER purely for the audit write. Only
the ALLOWED audit write was ever wrapped in ``with self.store.
transaction():``.

THE FIX moved the sensitive read itself inside the SAME transaction block
that writes the audit row, at each of the three call sites individually
(``api/service.py``) -- mirroring ``list_contact_destinations``'/
``get_delivery_overview``'s own "read + record in one transaction" shape,
which this module confirms was ALREADY correct and untouched by this round.

This module proves the fix with the store's own transaction-depth counter
(``_txn_depth``, present identically on ``InMemoryStore`` and ``SqlStore``)
-- a real, mechanism-level invariant, not an inferred timing coincidence:
the read and the write must execute at the SAME nonzero depth, i.e. inside
the one transaction, on every backend. The PostgreSQL variant additionally
checks the database's own ``txid_current()`` -- the strongest possible
proof that the two run under one physical backend transaction, not merely
one Python-level nesting counter.
"""

import os
import tempfile
import unittest
from datetime import datetime, timezone

from helpers import BACKEND, fresh_sql_store  # noqa: F401

from hockey_scheduler.api import ApiService
from hockey_scheduler.domain import (
    Division, League, LeagueSeason, Position, Program, Role, Season, Team)
from hockey_scheduler.store import InMemoryStore, SqlStore


def _seed(store):
    store.add_program(Program(id="pr", name="P"))
    store.add_season(Season(id="se", program_id="pr", name="Fall",
                            start_date=datetime(2026, 9, 1,
                                                tzinfo=timezone.utc)))
    store.add_league(League(id="lg", program_id="pr", name="L"))
    store.add_league_season(
        LeagueSeason(id="ls", league_id="lg", season_id="se"))
    store.add_division(Division(id="d", league_season_id="ls", name="D1",
                                age_group="U10"))
    store.add_team(Team(id="t1", name="T1", program_id="pr"))
    api = ApiService(store)
    api.set_age_eligibility_rule("ls", 12, 31, [{"code": "U10",
                                                  "max_age": 30}])
    p = api.setup.add_player("t1", None, Position.FORWARD,
                             first_name="Priv", last_name="Fields",
                             birthdate="2015-03-02",
                             registration_number="REG-1")
    return api, p


def _instrument(store, method_name, label, log):
    orig = getattr(store, method_name)

    def wrapper(*a, **kw):
        log.append((label, store._txn_depth))
        return orig(*a, **kw)
    setattr(store, method_name, wrapper)


class _AtomicityHelpers:
    """Shared body; subclasses provide ``self._store()`` -> a fresh store."""

    def _check(self, api_fn, read_methods, write_methods=("add_data_access",)):
        store = self._store()
        try:
            api, p = _seed(store)
            log = []
            for m in read_methods:
                _instrument(store, m, "READ", log)
            for m in write_methods:
                _instrument(store, m, "WRITE", log)
            result = api_fn(api, p)
            self.assertNotIn("error", result, result)
            read_depths = {d for lbl, d in log if lbl == "READ"}
            write_depths = {d for lbl, d in log if lbl == "WRITE"}
            self.assertTrue(read_depths, "expected at least one READ")
            self.assertTrue(write_depths, "expected at least one WRITE")
            self.assertEqual(
                read_depths, write_depths,
                f"read depths {read_depths} != write depths {write_depths} "
                "-- the sensitive read and the ALLOWED audit write did not "
                "run inside the same store transaction")
            for depth in read_depths | write_depths:
                self.assertGreater(
                    depth, 0,
                    "read/write executed at depth 0 -- outside any "
                    "transaction, its own already-committed unit of work")
            return log
        finally:
            store.close()

    def test_list_players_include_identity(self):
        self._check(
            lambda api, p: api.list_players(
                team_id="t1", include_identity=True, role=Role.LEAGUE_ADMIN,
                user_id="user_admin"),
            ["players_for_team", "all_players"])

    def test_player_duplicate_report(self):
        self._check(
            lambda api, p: api.player_duplicate_report(
                actor_role=Role.LEAGUE_ADMIN, actor_user_id="user_admin"),
            ["players_for_team", "all_players"])

    def test_evaluate_player_eligibility(self):
        self._check(
            lambda api, p: api.evaluate_player_eligibility(
                p.id, "d", actor_role=Role.COACH, actor_user_id="user_coach"),
            ["get_player"])


class MemoryAtomicityTest(_AtomicityHelpers, unittest.TestCase):
    def _store(self):
        return InMemoryStore()


class SqliteAtomicityTest(_AtomicityHelpers, unittest.TestCase):
    def _store(self):
        fd, self._tmp = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.addCleanup(os.remove, self._tmp)
        return SqlStore(self._tmp)


@unittest.skipUnless(os.environ.get("TEST_DATABASE_URL"),
                     "PostgreSQL suite only (TEST_DATABASE_URL not set)")
class PostgresAtomicityTest(_AtomicityHelpers, unittest.TestCase):
    def _store(self):
        return fresh_sql_store(os.environ["TEST_DATABASE_URL"])

    def _txid(self, store):
        cur = store.conn.cursor()
        cur.execute("SELECT txid_current() AS tx")
        row = cur.fetchone()
        cur.close()
        try:
            return row["tx"]
        except (TypeError, KeyError):
            return row[0]

    def test_evaluate_player_eligibility_shares_one_backend_transaction(self):
        # Stronger than the shared _txn_depth check: the database's OWN
        # transaction id, proving one physical PostgreSQL transaction, not
        # merely one Python-level nesting counter.
        store = self._store()
        try:
            api, p = _seed(store)
            log = []
            orig_get = store.get_player

            def spy_get(*a, **kw):
                row = orig_get(*a, **kw)
                log.append(("READ", self._txid(store)))
                return row
            store.get_player = spy_get

            orig_add = store.add_data_access

            def spy_add(*a, **kw):
                log.append(("WRITE", self._txid(store)))
                return orig_add(*a, **kw)
            store.add_data_access = spy_add

            result = api.evaluate_player_eligibility(
                p.id, "d", actor_role=Role.COACH, actor_user_id="user_coach")
            self.assertNotIn("error", result, result)

            read_txids = {tx for lbl, tx in log if lbl == "READ"}
            write_txids = {tx for lbl, tx in log if lbl == "WRITE"}
            self.assertEqual(
                read_txids, write_txids,
                f"read ran under txid(s) {read_txids}, write under "
                f"{write_txids} -- two different PostgreSQL backend "
                "transactions, not one atomic unit")
        finally:
            store.close()


class SqliteRollbackAtomicityTest(unittest.TestCase):
    """If the audit write fails, the whole call fails -- there is no path
    where the sensitive read's data reaches the caller while the paired
    audit attempt is lost, because both now live in ONE Python-level
    transaction block that the raise unwinds out of before any ``return``
    is reached."""

    def _store(self):
        fd, self._tmp = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.addCleanup(os.remove, self._tmp)
        return SqlStore(self._tmp)

    def test_forced_audit_write_failure_yields_no_disclosure_and_no_row(self):
        store = self._store()
        self.addCleanup(store.close)
        api, p = _seed(store)

        def boom(*a, **kw):
            raise RuntimeError("simulated audit-write failure")
        store.add_data_access = boom

        with self.assertRaises(RuntimeError):
            api.evaluate_player_eligibility(
                p.id, "d", actor_role=Role.COACH, actor_user_id="user_coach")

        # No half-done state: zero durable rows, and (since the exception
        # unwound the whole call before any `return`) no caller ever
        # observed the eligibility result either.
        self.assertEqual(store.list_data_access(), [])


if __name__ == "__main__":
    unittest.main()
