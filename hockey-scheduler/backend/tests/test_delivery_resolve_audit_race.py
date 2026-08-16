"""#426 round-2 external review finding 3: the delivery worker's audit and
destination resolution used to be two INDEPENDENT reads
(``_audit_system_contact_read`` and ``resolve_destination``, each its own
unlocked ``store.get_contact_destination`` call) with no shared
transaction or snapshot between them — so a concurrent upsert/retire
landing in the gap could make the delivered destination and the audited
row describe DIFFERENT snapshots, or drop the audit for a disclosure that
genuinely happened. The fix (``services/delivery._resolve_and_audit_
destination``) collapses both reads into ONE, and this module proves that
holds under REAL two-connection races, for both call sites the review
named — ``enqueue()`` and the retry-worker's re-resolution — across a
CREATE, an UPDATE, and a RETIRE racing write, in BOTH interleavings
(racer commits before our read is unblocked; racer commits after our read
already captured its snapshot), on SQLite and PostgreSQL, plus a
same-process Memory/two-thread parity check.

Every test asserts the FULL agreement set the review asked for: the
selected destination, whether the audit fired at all, the audited
subject, the purpose, and (positive audits) the shared run correlation id.
"""

import os
import tempfile
import threading
import unittest
from datetime import datetime, timezone

from helpers import BACKEND  # noqa: F401

from hockey_scheduler.api import ApiService
from hockey_scheduler.domain import (
    Notification,
    NotificationAudience,
    NotificationChannel,
    NotificationKind,
    Role,
)
from hockey_scheduler.services import DeliveryWorker
from hockey_scheduler.services.delivery import enqueue
from hockey_scheduler.store import InMemoryStore, SqlStore

EMAIL = NotificationChannel.EMAIL
OLD_DESTINATION = "pre-race-old@leak-probe.invalid"
NEW_DESTINATION = "post-race-new@leak-probe.invalid"


def _clock():
    return datetime.now(timezone.utc)


def _mk_notification(store, api, oid):
    return store.add_notification(Notification(
        id=store.next_id("notif"),
        kind=NotificationKind.ASSIGNMENT_OFFERED,
        audience=NotificationAudience.OFFICIAL, audience_ref=oid,
        title="Assigned", message="body", at=api.roster.clock()))


class _RaceHelpers:
    """Shared, non-test helper body; subclasses provide ``self._store()``
    -> a fresh connection/instance onto the backing database/store (a
    genuinely separate SQL connection for SQLite/Postgres, or the SAME
    shared InMemoryStore for the Memory parity check)."""

    def _seed_official(self, store, api):
        oid = api.create_official("Race Official", actor_id="seed")["id"]
        return oid, f"official:{oid}"

    def _pause_before_read(self, store, paused, resume):
        """Patch store.get_contact_destination to block BEFORE calling
        through — so a racer's write can land, and complete, strictly
        BEFORE this read ever executes (interleaving: racer-then-read)."""
        orig = store.get_contact_destination

        def instrumented(recipient, channel):
            paused.set()
            resume.wait(15)
            return orig(recipient, channel)
        store.get_contact_destination = instrumented

    def _run_sequential(self, mutate_fn, target_fn):
        """The 'after' interleaving: run ``target_fn`` (the read side) to
        FULL completion FIRST, only then run ``mutate_fn`` (the racer).

        Deliberately NOT a pause/resume thread barrier the way ``before``
        below is — the read-only half of ``_resolve_and_audit_destination``
        holds SQLite's SHARED lock for its own transaction's duration
        (released the moment that transaction exits, well before this
        function returns), so pausing INSIDE it while a racer's write tries
        to COMMIT (needing to upgrade to EXCLUSIVE, which requires every
        SHARED holder to release first) makes the racer wait on OUR own
        paused thread — exactly the deadlock finding 3's fix itself had to
        avoid (see ``_resolve_and_audit_destination``'s own docstring) —
        for a property that does not need concurrency to prove at all:
        once a read has FULLY completed and its result is already
        captured, no subsequent, independent write can retroactively
        change a decision already made from it. Plain sequential
        execution — read fully, THEN write — is the strongest possible
        witness for that: there is no interleaving window left to matter.
        """
        value = target_fn()
        mutate_fn()
        return value

    def _run_race_before(self, read_store, mutate_fn, target_fn):
        """The 'before' interleaving: the racer's write commits, and fully
        releases every lock it held, strictly BEFORE ``target_fn``'s (the
        read side, against ``read_store``) own read ever executes — proven
        with a genuine two-thread barrier, since this direction needs real
        concurrency to construct at all. Returns ``target_fn``'s result."""
        paused = threading.Event()
        resume = threading.Event()
        racer_done = threading.Event()
        self._pause_before_read(read_store, paused, resume)

        result = {}
        errors = {}

        def run_target():
            try:
                result["value"] = target_fn()
            except BaseException as exc:  # noqa: BLE001 - surface, don't hide
                errors["target"] = exc

        def run_racer():
            try:
                if not paused.wait(15):
                    errors["racer"] = RuntimeError("read never reached the pause")
                    return
                mutate_fn()
                racer_done.set()
            except BaseException as exc:  # noqa: BLE001 - surface, don't hide
                errors["racer"] = exc
            finally:
                resume.set()

        t_target = threading.Thread(target=run_target)
        t_racer = threading.Thread(target=run_racer)
        t_target.start()
        t_racer.start()
        t_target.join(15)
        t_racer.join(15)
        if errors:
            raise AssertionError(f"race harness thread(s) raised: {errors}")
        self.assertTrue(racer_done.is_set(), "racer never completed")
        return result["value"]

    # -- the three racing mutations ---------------------------------------
    def _create_race(self, ref):
        racer_api = ApiService(self._store())
        return lambda: racer_api.set_contact_destination(
            ref, "email", NEW_DESTINATION)

    def _update_race(self, store, api, ref):
        api.set_contact_destination(ref, "email", OLD_DESTINATION)
        racer_api = ApiService(self._store())
        return lambda: racer_api.set_contact_destination(
            ref, "email", NEW_DESTINATION)

    def _retire_race(self, store, api, ref):
        cid = api.set_contact_destination(ref, "email", OLD_DESTINATION)["id"]
        racer_api = ApiService(self._store())

        def go():
            result = racer_api.set_contact_destination_active(
                cid, False, actor_id="ops", actor_role=Role.LEAGUE_ADMIN)
            self.assertNotIn("error", result, result)
        return go

    # -- enqueue() target --------------------------------------------------
    def _enqueue_target(self, store, notification):
        def go():
            created = enqueue(store, notification, channels=(EMAIL,))
            return next(d for d in created if d.channel == EMAIL)
        return go

    # -- retry-worker target ------------------------------------------------
    def _worker_target(self, store):
        def go():
            worker = DeliveryWorker(store, _clock)
            worker.process_pending()
            return next(d for d in store.all_notification_deliveries()
                        if d.channel == EMAIL)
        return go

    def _seed_pending_delivery(self, store, api):
        # enqueue() with no destination registered yet -> a PENDING row
        # addressed to `ref`, resolved to the placeholder at emission time
        # (re-resolved for real by the worker under test below).
        oid, ref = self._seed_official(store, api)
        notification = _mk_notification(store, api, oid)
        enqueue(store, notification, channels=(EMAIL,))
        return ref

    def _assert_agreement(self, store, ref, destination, expect_destination,
                          expect_audited):
        self.assertEqual(destination, expect_destination)
        # Filtered to THIS resolution's own purpose — a retire racer's own
        # set_contact_destination_active call is ITSELF an independent,
        # already-covered sensitive read (its own purpose=
        # "set_contact_destination_active" row) and must not be mistaken
        # for (or mask) the delivery-resolution audit under test here.
        rows = [r for r in store.list_data_access()
               if r.subject_id == ref and r.purpose == "delivery_resolve"]
        if not expect_audited:
            self.assertEqual(rows, [], rows)
            return
        self.assertEqual(len(rows), 1, rows)
        row = rows[0]
        self.assertEqual(row.outcome, "allowed")
        self.assertEqual(row.actor_role, "system")
        self.assertIsNone(row.actor_user_id)
        self.assertEqual(row.purpose, "delivery_resolve")
        self.assertTrue(row.request_id.startswith("req_"))


class _FullRaceMatrixTests(_RaceHelpers):
    """The full {enqueue, worker} x {create, update, retire} x
    {before, after} matrix — 12 tests, run against two REAL connections on
    both SQL backends."""

    # -- enqueue() x {create, update, retire} x {before, after} -----------

    def test_enqueue_create_race_before_uses_racers_value(self):
        store = self._store()
        api = ApiService(store)
        oid, ref = self._seed_official(store, api)
        notification = _mk_notification(store, api, oid)
        mutate = self._create_race(ref)
        d = self._run_race_before(store, mutate,
                                  self._enqueue_target(store, notification))
        self._assert_agreement(store, ref, d.destination,
                               NEW_DESTINATION, True)

    def test_enqueue_create_race_after_uses_pre_race_placeholder(self):
        store = self._store()
        api = ApiService(store)
        oid, ref = self._seed_official(store, api)
        notification = _mk_notification(store, api, oid)
        mutate = self._create_race(ref)
        d = self._run_sequential(mutate, self._enqueue_target(store, notification))
        # No row existed at the moment of our (only) read: placeholder,
        # unaudited — regardless of what the racer committed afterward.
        self.assertNotEqual(d.destination, NEW_DESTINATION)
        self._assert_agreement(store, ref, d.destination, d.destination, False)

    def test_enqueue_update_race_before_uses_racers_value(self):
        store = self._store()
        api = ApiService(store)
        oid, ref = self._seed_official(store, api)
        notification = _mk_notification(store, api, oid)
        mutate = self._update_race(store, api, ref)
        d = self._run_race_before(store, mutate,
                                  self._enqueue_target(store, notification))
        self._assert_agreement(store, ref, d.destination,
                               NEW_DESTINATION, True)

    def test_enqueue_update_race_after_uses_pre_race_value(self):
        store = self._store()
        api = ApiService(store)
        oid, ref = self._seed_official(store, api)
        notification = _mk_notification(store, api, oid)
        mutate = self._update_race(store, api, ref)
        d = self._run_sequential(mutate, self._enqueue_target(store, notification))
        self._assert_agreement(store, ref, d.destination, OLD_DESTINATION, True)

    def test_enqueue_retire_race_before_uses_placeholder(self):
        store = self._store()
        api = ApiService(store)
        oid, ref = self._seed_official(store, api)
        notification = _mk_notification(store, api, oid)
        mutate = self._retire_race(store, api, ref)
        d = self._run_race_before(store, mutate,
                                  self._enqueue_target(store, notification))
        # Retired before our read: nothing ACTIVE to disclose.
        self.assertNotEqual(d.destination, OLD_DESTINATION)
        self._assert_agreement(store, ref, d.destination, d.destination, False)

    def test_enqueue_retire_race_after_uses_pre_race_value(self):
        store = self._store()
        api = ApiService(store)
        oid, ref = self._seed_official(store, api)
        notification = _mk_notification(store, api, oid)
        mutate = self._retire_race(store, api, ref)
        d = self._run_sequential(mutate, self._enqueue_target(store, notification))
        # Our read already captured the still-active row before the racer
        # retired it — the disclosure genuinely happened, so it is audited.
        self._assert_agreement(store, ref, d.destination, OLD_DESTINATION, True)

    # -- retry-worker x {create, update, retire} x {before, after} --------

    def test_worker_create_race_before_uses_racers_value(self):
        store = self._store()
        api = ApiService(store)
        ref = self._seed_pending_delivery(store, api)
        mutate = self._create_race(ref)
        d = self._run_race_before(store, mutate, self._worker_target(store))
        self._assert_agreement(store, ref, d.destination,
                               NEW_DESTINATION, True)

    def test_worker_create_race_after_uses_pre_race_placeholder(self):
        store = self._store()
        api = ApiService(store)
        ref = self._seed_pending_delivery(store, api)
        mutate = self._create_race(ref)
        d = self._run_sequential(mutate, self._worker_target(store))
        self.assertNotEqual(d.destination, NEW_DESTINATION)
        self._assert_agreement(store, ref, d.destination, d.destination, False)

    def test_worker_update_race_before_uses_racers_value(self):
        store = self._store()
        api = ApiService(store)
        ref = self._seed_pending_delivery(store, api)
        mutate = self._update_race(store, api, ref)
        d = self._run_race_before(store, mutate, self._worker_target(store))
        self._assert_agreement(store, ref, d.destination,
                               NEW_DESTINATION, True)

    def test_worker_update_race_after_uses_pre_race_value(self):
        store = self._store()
        api = ApiService(store)
        ref = self._seed_pending_delivery(store, api)
        mutate = self._update_race(store, api, ref)
        d = self._run_sequential(mutate, self._worker_target(store))
        self._assert_agreement(store, ref, d.destination, OLD_DESTINATION, True)

    def test_worker_retire_race_before_uses_placeholder(self):
        store = self._store()
        api = ApiService(store)
        ref = self._seed_pending_delivery(store, api)
        mutate = self._retire_race(store, api, ref)
        d = self._run_race_before(store, mutate, self._worker_target(store))
        self.assertNotEqual(d.destination, OLD_DESTINATION)
        self._assert_agreement(store, ref, d.destination, d.destination, False)

    def test_worker_retire_race_after_uses_pre_race_value(self):
        store = self._store()
        api = ApiService(store)
        ref = self._seed_pending_delivery(store, api)
        mutate = self._retire_race(store, api, ref)
        d = self._run_sequential(mutate, self._worker_target(store))
        self._assert_agreement(store, ref, d.destination, OLD_DESTINATION, True)


class SqliteRaceTest(_FullRaceMatrixTests, unittest.TestCase):
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
class PostgresRaceTest(_FullRaceMatrixTests, unittest.TestCase):
    def setUp(self):
        self.url = os.environ["TEST_DATABASE_URL"]
        SqlStore(self.url).clear_all_data()

    def _store(self):
        return SqlStore(self.url)


class MemoryRaceParityTest(unittest.TestCase):
    """Memory parity for finding 3 — deliberately NOT the same pause/resume
    barrier the SQL matrix above uses. ``InMemoryStore.transaction()``'s own
    docstring is explicit that ``read_only`` is a no-op there: its single
    process-wide ``self._lock`` is taken at the top of ``transaction()``
    itself, before ANYTHING in the ``with`` block runs, and held for the
    body's ENTIRE duration regardless — "the strongest isolation, for
    free". A paused thread already holds that lock the moment it enters
    the read-only transaction, well before reaching any pause point INSIDE
    it, so a second thread calling ANY store method (the racer's own
    write) just blocks on the SAME lock until the first releases it —
    confirmed empirically: an earlier draft of this test tried an
    "unbarriered" two-thread race with no explicit ordering and found the
    racer thread deterministically won every single one of 40 runs, never
    the reader — not because the fix is somehow biased, but because
    Memory's full-lock model leaves no partial-execution window for a
    second thread to land in AT ALL. Reusing the SQL barrier here, or
    hoping thread scheduling alone will explore both orderings, tests
    nothing about the fix that plain sequential calls do not already
    prove more directly and deterministically.

    What Memory's own concurrency model actually GUARANTEES is stronger
    than either SQL backend's: since ``self._lock`` fully serializes every
    store call, the ONLY two orderings that can EVER occur are "the
    create fully completes, then the resolution runs" or the reverse —
    there is no third, partial-interleaving possibility to construct.
    Plain sequential calls, in each order, are therefore a COMPLETE
    parity proof for Memory, not merely a substitute for one.
    """

    def _fresh_pending_delivery(self):
        store = InMemoryStore()
        api = ApiService(store)
        oid = api.create_official("Race Official", actor_id="seed")["id"]
        ref = f"official:{oid}"
        notification = _mk_notification(store, api, oid)
        enqueue(store, notification, channels=(EMAIL,))
        return store, ref

    def _assert_agreement(self, store, ref, destination, expect_destination,
                          expect_audited):
        self.assertEqual(destination, expect_destination)
        rows = [r for r in store.list_data_access()
               if r.subject_id == ref and r.purpose == "delivery_resolve"]
        if not expect_audited:
            self.assertEqual(rows, [], rows)
            return
        self.assertEqual(len(rows), 1, rows)
        row = rows[0]
        self.assertEqual(row.outcome, "allowed")
        self.assertEqual(row.actor_role, "system")
        self.assertEqual(row.purpose, "delivery_resolve")

    def test_enqueue_create_before_uses_created_value(self):
        store = InMemoryStore()
        api = ApiService(store)
        oid = api.create_official("Race Official", actor_id="seed")["id"]
        ref = f"official:{oid}"
        notification = _mk_notification(store, api, oid)
        api.set_contact_destination(ref, "email", NEW_DESTINATION)
        created = enqueue(store, notification, channels=(EMAIL,))
        d = next(x for x in created if x.channel == EMAIL)
        self._assert_agreement(store, ref, d.destination, NEW_DESTINATION, True)

    def test_enqueue_create_after_uses_pre_race_placeholder(self):
        store = InMemoryStore()
        api = ApiService(store)
        oid = api.create_official("Race Official", actor_id="seed")["id"]
        ref = f"official:{oid}"
        notification = _mk_notification(store, api, oid)
        created = enqueue(store, notification, channels=(EMAIL,))
        d = next(x for x in created if x.channel == EMAIL)
        api.set_contact_destination(ref, "email", NEW_DESTINATION)
        self.assertNotEqual(d.destination, NEW_DESTINATION)
        self._assert_agreement(store, ref, d.destination, d.destination, False)

    def test_worker_create_before_uses_created_value(self):
        store, ref = self._fresh_pending_delivery()
        ApiService(store).set_contact_destination(ref, "email", NEW_DESTINATION)
        DeliveryWorker(store, _clock).process_pending()
        d = next(x for x in store.all_notification_deliveries()
                if x.channel == EMAIL)
        self._assert_agreement(store, ref, d.destination, NEW_DESTINATION, True)

    def test_worker_create_after_uses_pre_race_placeholder(self):
        store, ref = self._fresh_pending_delivery()
        DeliveryWorker(store, _clock).process_pending()
        d = next(x for x in store.all_notification_deliveries()
                if x.channel == EMAIL)
        ApiService(store).set_contact_destination(ref, "email", NEW_DESTINATION)
        self.assertNotEqual(d.destination, NEW_DESTINATION)
        self._assert_agreement(store, ref, d.destination, d.destination, False)

    def test_concurrent_threads_never_split_destination_from_audit(self):
        # Bonus sanity check, not a substitute for the deterministic pairs
        # above: real threads, genuinely concurrent, no artificial
        # ordering — the store's own lock decides who goes first, but
        # whichever it is, destination and audit must still agree. Proves
        # no corruption under real thread contention, even though (per
        # this class's own docstring) the lock means only one order is
        # ever actually observed in practice.
        for n in range(10):
            store = InMemoryStore()
            api = ApiService(store)
            oid = api.create_official(f"Race {n}", actor_id="seed")["id"]
            ref = f"official:{oid}"
            notification = _mk_notification(store, api, oid)
            result = {}

            def target():
                created = enqueue(store, notification, channels=(EMAIL,))
                result["d"] = next(x for x in created if x.channel == EMAIL)

            def racer():
                ApiService(store).set_contact_destination(
                    ref, "email", NEW_DESTINATION)

            t_target = threading.Thread(target=target)
            t_racer = threading.Thread(target=racer)
            t_racer.start()
            t_target.start()
            t_target.join(15)
            t_racer.join(15)
            d = result["d"]
            self._assert_agreement(
                store, ref, d.destination, d.destination,
                d.destination == NEW_DESTINATION)


if __name__ == "__main__":
    unittest.main()
