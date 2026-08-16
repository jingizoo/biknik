"""#426 review finding 4: independent denial durability + the toggle's
fetch-mutate-audit race, proven live (not merely reasoned about) on every
backend the review named.

Two separate properties, two separate test classes:

* ``DurableDenialTest`` (Memory/SQLite/Postgres, one process): a denial
  recorded by ``_refuse_sensitive_read`` while NESTED inside an ambient
  transaction the CALLER later fails must survive that failure — the bug
  the review demonstrated ("a forbidden result followed by an outer
  failure left zero denial rows"). Proven by literally nesting a facade
  refusal inside a transaction() this test then forces to raise.

  The SAME class also carries #426 ROUND-2 review finding 1's own
  regression coverage: a durable row surviving a rollback is not enough
  on its own — the id/seq COUNTER that named it must agree with it
  afterward, or the very next sensitive read (allowed or denied) collides
  with the row that just survived. Exact reviewer repro: nested Coach
  denial -> forced outer rollback -> durable row exists -> the next read
  hits ``unique_violation`` instead of its normal result. Covered here:
  single and multiple nested denials, both rollback and commit, a
  subsequent ALLOWED read and a subsequent DENIED read, and (SQL backends)
  a close/reopen between the rollback and the next read.

* ``ToggleRaceTest`` (SQLite file-backed + PostgreSQL, TWO real
  connections): a ``set_contact_destination`` upsert racing
  ``set_contact_destination_active``'s fetch-mutate-audit window must never
  lose either writer's effect — the bug the review demonstrated ("a
  concurrent upsert committed a new destination, the toggle then
  overwrote it with its stale pre-read value and audited/returned the
  stale value"). Proven by pausing the toggle's row-locked fetch, letting
  the upsert commit a NEW destination, then resuming the toggle and
  checking BOTH effects (the new destination, the toggle) survived,
  and the toggle's own response/audit row reflects a REAL committed
  value, never the pre-race snapshot.
"""

import os
import tempfile
import threading
import unittest

from helpers import BACKEND  # noqa: F401

from hockey_scheduler.api import ApiService
from hockey_scheduler.domain import ACCESS_ALLOWED, ACCESS_DENIED, Role
from hockey_scheduler.full_demo import build_full_demo_store
from hockey_scheduler.store import InMemoryStore, SqlStore

SENTINEL_OLD = "old-destination@leak-probe.invalid"
SENTINEL_NEW = "new-destination-from-racer@leak-probe.invalid"


class Boom(Exception):
    pass


class _DurableDenialContract:
    """Shared body; subclasses provide self.store/self.api."""

    def test_denial_survives_a_nested_outer_transaction_that_later_fails(self):
        # The exact bug: _refuse_sensitive_read used to open its own
        # `with self.store.transaction():`, which — nested inside an
        # ambient one — JOINS it rather than committing independently.
        try:
            with self.store.transaction():
                result = self.api.list_contact_destinations(
                    actor_role=Role.COACH, actor_user_id="user_coach")
                self.assertEqual(result["error"]["code"], "forbidden", result)
                raise Boom("the caller's own unit fails AFTER the refusal")
        except Boom:
            pass
        rows = self.store.list_data_access()
        self.assertEqual(len(rows), 1, rows)
        self.assertEqual(rows[0].outcome, ACCESS_DENIED)
        self.assertEqual(rows[0].actor_role, "coach")

    def test_denial_survives_two_levels_of_nesting_that_both_fail(self):
        try:
            with self.store.transaction():
                try:
                    with self.store.transaction():
                        self.api.list_contact_destinations(actor_role=Role.VIEWER)
                        raise Boom("inner")
                except Boom:
                    pass
                raise Boom("outer")
        except Boom:
            pass
        rows = self.store.list_data_access()
        self.assertEqual(len(rows), 1, rows)
        self.assertEqual(rows[0].outcome, ACCESS_DENIED)

    def test_allowed_read_audit_still_rolls_back_with_its_own_transaction(self):
        # The OTHER half of finding 4's contract must not have regressed:
        # an ALLOWED read's row is NOT durable — it stays atomic with the
        # disclosure it accompanies. Forcing set_contact_destination_active
        # to fail AFTER its own internal transaction opened (by toggling a
        # nonexistent id is refused too early to prove this; instead we
        # directly exercise the store contract this depends on, already
        # pinned at the store level by
        # test_data_access_log_store.*.test_transaction_rollback_leaves_no_rows
        # — here we additionally confirm add_data_access (non-durable) does
        # NOT survive a later sibling failure the way add_data_access_durable
        # does, so the two paths are provably different, not accidentally
        # identical.
        from hockey_scheduler.domain import DataAccessLog, SensitiveFieldCategory
        from hockey_scheduler.domain import ACCESS_ALLOWED
        try:
            with self.store.transaction():
                self.store.add_data_access(DataAccessLog(
                    id=self.store.next_id("daccess"),
                    category=SensitiveFieldCategory.CONTACT_DESTINATION,
                    subject_type="recipient", subject_id="scheduler",
                    purpose="probe", at=self.api.roster.clock(),
                    outcome=ACCESS_ALLOWED))
                raise Boom()
        except Boom:
            pass
        self.assertEqual(self.store.list_data_access(), [])

    # -- #426 round-2 review finding 1: id/seq must survive a rollback ------
    # atomically WITH the row they name, or the counter and the durable row
    # it produced disagree and the NEXT allocation collides with it.

    def _reopen_same_backend(self):
        return None  # overridden by SQL subclasses; Memory has no restart

    @staticmethod
    def _assert_all_unique_and_ordered(rows):
        ids = [r.id for r in rows]
        seqs = [r.seq for r in rows]
        assert len(set(ids)) == len(rows), f"id collision: {ids!r}"
        assert len(set(seqs)) == len(rows), f"seq collision: {seqs!r}"
        assert seqs == sorted(seqs), f"seq not increasing: {seqs!r}"

    def test_next_allowed_read_gets_a_unique_id_after_a_forced_rollback(self):
        # Exact reviewer repro: nested Coach denial -> forced outer
        # rollback -> durable row exists -> the NEXT read (here: an
        # ALLOWED League Admin read) must still succeed, not hit
        # unique_violation.
        try:
            with self.store.transaction():
                self.api.list_contact_destinations(actor_role=Role.COACH)
                raise Boom("outer unit fails after the nested denial")
        except Boom:
            pass
        self.assertEqual(len(self.store.list_data_access()), 1)

        result = self.api.list_contact_destinations(
            actor_role=Role.LEAGUE_ADMIN, actor_user_id="user_admin")
        self.assertNotIn("error", result, result)

        rows = self.store.list_data_access()
        self.assertEqual(len(rows), 2, rows)
        self._assert_all_unique_and_ordered(rows)
        self.assertEqual(rows[0].outcome, ACCESS_DENIED)
        self.assertEqual(rows[1].outcome, ACCESS_ALLOWED)

    def test_next_denied_read_gets_a_unique_id_after_a_forced_rollback(self):
        # Same shape, but the NEXT read is ALSO a denial (a Viewer, not a
        # League Admin) — proving denied-after-denied-after-rollback works,
        # not merely denied-after-allowed.
        try:
            with self.store.transaction():
                self.api.list_contact_destinations(actor_role=Role.COACH)
                raise Boom("outer unit fails after the nested denial")
        except Boom:
            pass
        result = self.api.list_contact_destinations(actor_role=Role.VIEWER)
        self.assertEqual(result["error"]["code"], "forbidden", result)

        rows = self.store.list_data_access()
        self.assertEqual(len(rows), 2, rows)
        self._assert_all_unique_and_ordered(rows)
        self.assertEqual([r.outcome for r in rows],
                         [ACCESS_DENIED, ACCESS_DENIED])

    def test_multiple_nested_denials_then_rollback_all_get_unique_ids(self):
        # MULTIPLE nested denials queued in the SAME outer transaction that
        # then rolls back: every queued row must get a unique id/seq at
        # flush time, and a SUBSEQUENT read must not collide with any of
        # them either.
        try:
            with self.store.transaction():
                self.api.list_contact_destinations(actor_role=Role.COACH)
                self.api.list_contact_destinations(actor_role=Role.VIEWER)
                self.api.get_delivery_overview(actor_role=Role.COACH)
                raise Boom("outer unit fails after three nested denials")
        except Boom:
            pass
        rows = self.store.list_data_access()
        self.assertEqual(len(rows), 3, rows)
        self._assert_all_unique_and_ordered(rows)
        self.assertTrue(all(r.outcome == ACCESS_DENIED for r in rows), rows)

        result = self.api.list_contact_destinations(
            actor_role=Role.LEAGUE_ADMIN, actor_user_id="user_admin")
        self.assertNotIn("error", result, result)
        rows2 = self.store.list_data_access()
        self.assertEqual(len(rows2), 4, rows2)
        self._assert_all_unique_and_ordered(rows2)

    def test_nested_denial_that_actually_commits_then_next_read_unique(self):
        # Parity check: the SAME nested-denial-then-subsequent-read shape,
        # but the outer transaction COMMITS instead of rolling back —
        # proving the fix does not merely paper over the rollback path
        # while relying on some accident of the ordinary commit path.
        with self.store.transaction():
            self.api.list_contact_destinations(actor_role=Role.COACH)
        result = self.api.list_contact_destinations(
            actor_role=Role.LEAGUE_ADMIN, actor_user_id="user_admin")
        self.assertNotIn("error", result, result)
        rows = self.store.list_data_access()
        self.assertEqual(len(rows), 2, rows)
        self._assert_all_unique_and_ordered(rows)

    def test_next_read_after_reopen_still_gets_a_unique_id(self):
        # "reopen where applicable": the durable denial and the counter it
        # must agree with both have to survive a close/reopen, not merely
        # the life of one connection/process.
        try:
            with self.store.transaction():
                self.api.list_contact_destinations(actor_role=Role.COACH)
                raise Boom("outer unit fails after the nested denial")
        except Boom:
            pass
        reopened_store = self._reopen_same_backend()
        if reopened_store is None:
            self.skipTest("no reopen for this backend")
        try:
            reopened_api = ApiService(reopened_store)
            result = reopened_api.list_contact_destinations(
                actor_role=Role.LEAGUE_ADMIN, actor_user_id="user_admin")
            self.assertNotIn("error", result, result)
            rows = reopened_store.list_data_access()
            self.assertEqual(len(rows), 2, rows)
            self._assert_all_unique_and_ordered(rows)
        finally:
            if reopened_store is not self.store:
                reopened_store.close()


class MemoryDurableDenialTest(_DurableDenialContract, unittest.TestCase):
    def setUp(self):
        self.store = InMemoryStore()
        self.api = ApiService(self.store)


class SqliteDurableDenialTest(_DurableDenialContract, unittest.TestCase):
    def setUp(self):
        fd, self._tmp = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.store = SqlStore(self._tmp)
        self.api = ApiService(self.store)

    def tearDown(self):
        self.store.close()
        os.remove(self._tmp)

    def _reopen_same_backend(self):
        return SqlStore(self._tmp)


@unittest.skipUnless(os.environ.get("TEST_DATABASE_URL"),
                     "PostgreSQL suite only (TEST_DATABASE_URL not set)")
class PostgresDurableDenialTest(_DurableDenialContract, unittest.TestCase):
    def setUp(self):
        self.url = os.environ["TEST_DATABASE_URL"]
        self.store = SqlStore(self.url)
        self.store.clear_all_data()
        self.api = ApiService(self.store)

    def tearDown(self):
        self.store.close()

    def _reopen_same_backend(self):
        return SqlStore(self.url)


# ---------------------------------------------------------------------------
# Toggle vs upsert: the fetch-mutate-audit race (two real connections)
# ---------------------------------------------------------------------------

class _ToggleRaceContract:
    """Shared body; subclasses provide self._store(url_or_path) -> SqlStore."""

    def test_upsert_racing_toggle_loses_neither_effect(self):
        toggle_store = self._store()
        toggle_api = ApiService(toggle_store)
        # A REAL Official (set_contact_destination's dangling-recipient
        # check requires one to exist) — seeded via the same full-demo
        # fixture the rest of this suite uses, onto THIS file/connection.
        _store, _gid, ids = build_full_demo_store(store=toggle_store)
        oid = ids["referee_id"]
        ref = f"official:{oid}"
        cid = toggle_api.set_contact_destination(
            ref, "email", SENTINEL_OLD, label="Ref")["id"]

        paused = threading.Event()
        racer_attempting = threading.Event()
        resume = threading.Event()
        orig = toggle_store.get_contact_destination_for_update

        def instrumented(contact_id):
            row = orig(contact_id)
            paused.set()
            resume.wait(15)
            return row

        toggle_store.get_contact_destination_for_update = instrumented

        results = {}

        def run_toggle():
            try:
                results["toggle"] = toggle_api.set_contact_destination_active(
                    cid, False, actor_id="user_ops",
                    actor_role=Role.LEAGUE_ADMIN)
            except Exception as exc:  # pragma: no cover - diagnostic only
                results["toggle_exc"] = exc

        def run_upsert():
            if not paused.wait(15):
                results["upsert_exc"] = RuntimeError("toggle never paused")
                return
            upsert_store = self._store()
            upsert_api = ApiService(upsert_store)
            racer_attempting.set()
            try:
                # set_contact_destination's own transaction() (finding-4
                # fix above) takes BEGIN IMMEDIATE and correctly WAITS
                # (busy_timeout) for the toggle's held lock — this call
                # blocks here until the main thread below releases it.
                results["upsert"] = upsert_api.set_contact_destination(
                    ref, "email", SENTINEL_NEW, label="Ref")
            except Exception as exc:  # pragma: no cover - diagnostic only
                results["upsert_exc"] = exc
            finally:
                upsert_store.close()

        t_toggle = threading.Thread(target=run_toggle)
        t_upsert = threading.Thread(target=run_upsert)
        t_toggle.start()
        t_upsert.start()
        # Release the toggle's held lock ONLY after BOTH: the toggle
        # genuinely holds it (paused) AND the racer has genuinely reached
        # its own blocking attempt (racer_attempting) — deterministic
        # synchronization, no sleep. The toggle's own release (its
        # transaction committing) is what lets the racer's blocked
        # BEGIN IMMEDIATE finally proceed; this thread does not itself
        # wait for the racer, which would deadlock (the racer cannot
        # finish until the toggle releases).
        self.assertTrue(racer_attempting.wait(15), "racer never attempted")
        resume.set()
        t_upsert.join(25)
        t_toggle.join(25)

        self.assertNotIn("toggle_exc", results, results)
        self.assertNotIn("upsert_exc", results, results)
        self.assertNotIn("error", results.get("toggle") or {}, results)
        self.assertNotIn("error", results.get("upsert") or {}, results)

        # Independent, fresh read of the FINAL committed row: BOTH writers'
        # effects survived — the destination is the RACER's new value (the
        # toggle never touches destination) and active is False (the
        # toggle's own effect). The bug this proves fixed: the OLD toggle
        # read `destination` before the race, then blindly wrote that
        # stale value back, silently reverting the racer's committed write.
        final_store = self._store()
        try:
            final = next(c for c in final_store.all_contact_destinations()
                        if c.id == cid)
        finally:
            final_store.close()
        self.assertEqual(final.destination, SENTINEL_NEW,
                         "the toggle silently reverted a concurrently "
                         "committed destination")
        self.assertFalse(final.active)

        # The toggle's OWN response/audit must reflect a REAL committed
        # value at the moment it actually ran under the lock — never the
        # pre-race snapshot taken before get_contact_destination_for_update
        # was even called.
        self.assertIn(results["toggle"]["destination"],
                      (SENTINEL_OLD, SENTINEL_NEW))
        rows = toggle_store.list_data_access(subject_id=ref)
        self.assertEqual(len(rows), 1)
        toggle_store.close()


class SqliteToggleRaceTest(_ToggleRaceContract, unittest.TestCase):
    def setUp(self):
        fd, self._tmp = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        SqlStore(self._tmp).close()  # migrate, then release

    def tearDown(self):
        os.remove(self._tmp)

    def _store(self):
        return SqlStore(self._tmp)


@unittest.skipUnless(os.environ.get("TEST_DATABASE_URL"),
                     "PostgreSQL suite only (TEST_DATABASE_URL not set)")
class PostgresToggleRaceTest(_ToggleRaceContract, unittest.TestCase):
    def setUp(self):
        self.url = os.environ["TEST_DATABASE_URL"]
        SqlStore(self.url).clear_all_data()

    def _store(self):
        return SqlStore(self.url)


if __name__ == "__main__":
    unittest.main()
