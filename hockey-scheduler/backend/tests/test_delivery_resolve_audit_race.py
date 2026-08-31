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

PUSH / DeviceToken matrix (#426 round-3 review finding 1). The EMAIL/
ContactDestination matrix above proves the "one read decides both
outputs" property for _resolve_and_audit_destination's ContactDestination
branch; ``_FullRaceMatrixTests``' PUSH-suffixed tests below prove the
SAME property for its DeviceToken branch, across the SAME CREATE/UPDATE/
RETIRE x before/after matrix, adapted to DeviceToken's actual mechanism
(see ``_update_push_race``'s own docstring for why "update" means
something structurally different there than it does for
ContactDestination's in-place value overwrite).
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

PUSH = NotificationChannel.PUSH
OLD_PUSH_TOKEN = "pre-race-old-push-token"
NEW_PUSH_TOKEN = "post-race-new-push-token"


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
    shared InMemoryStore for the Memory parity check).

    CONNECTION LIFECYCLE (#426 round-3 review finding 2). Every concrete
    ``self._store()`` implementation below routes through
    :meth:`_track_store`, so every SqlStore this class's 12 tests open —
    the per-test PRIMARY connection AND the SEPARATE racer connection
    ``_create_race``/``_update_race``/``_retire_race`` each open via their
    own ``ApiService(self._store())`` — is registered for a guaranteed
    ``addCleanup`` close. Before this fix, NONE of them were ever closed:
    running ``python3 -W always::ResourceWarning -m unittest -v
    test_delivery_resolve_audit_race`` against ``SqliteRaceTest`` alone
    emitted 24 ``ResourceWarning: unclosed database`` warnings (12 tests
    x 2 leaked connections each — the primary plus one racer; every test
    exercises exactly one of the three racing mutations) — reproduced
    verbatim before this fix, see the PR history for the captured count.
    """

    def _track_store(self, store):
        """Register a freshly opened SqlStore for a guaranteed close.

        ``addCleanup`` is unittest's own "runs no matter what" hook: it
        fires after ``tearDown`` regardless of whether ``setUp``, the test
        method, or ``tearDown`` itself raised — including when the store
        was opened inside a racer/target thread body that then errors
        (the thread's own exception is re-raised on the MAIN thread by
        ``_run_race_before`` above, well before ``doCleanups`` ever runs,
        so the store this method already registered is still closed
        regardless of THAT failure path). See
        ``CloseTrackingHarnessTest`` below for a direct, isolated proof of
        this guarantee under an injected failure — this method itself
        stays a thin, obviously-correct delegation to ``addCleanup``
        rather than hand-rolling try/finally bookkeeping that could drift
        from unittest's own well-tested semantics.

        Every concrete subclass's OWN ``setUp`` registers ITS cleanup
        (removing the SQLite temp file; nothing extra for Postgres, which
        never deletes its database) BEFORE any test method runs and
        therefore before any call to this method — ``addCleanup``'s LIFO
        order then guarantees every store this method tracks is closed
        BEFORE that file-removal cleanup runs, never after.
        """
        self.addCleanup(store.close)
        return store

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

    def _pause_before_push_read(self, store, paused, resume):
        """The PUSH-branch analogue of ``_pause_before_read`` (#426
        round-3 review finding 1): patches ``store.active_device_token_
        for`` instead of ``store.get_contact_destination``, since that is
        the call ``_resolve_and_audit_destination``'s read-only
        transaction makes when ``channel == PUSH``."""
        orig = store.active_device_token_for

        def instrumented(recipient):
            paused.set()
            resume.wait(15)
            return orig(recipient)
        store.active_device_token_for = instrumented

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

    def _run_race_before(self, read_store, mutate_fn, target_fn, pause_fn=None):
        """The 'before' interleaving: the racer's write commits, and fully
        releases every lock it held, strictly BEFORE ``target_fn``'s (the
        read side, against ``read_store``) own read ever executes — proven
        with a genuine two-thread barrier, since this direction needs real
        concurrency to construct at all. Returns ``target_fn``'s result.

        ``pause_fn`` (#426 round-3 review finding 1): which read to pause —
        defaults to ``_pause_before_read`` (the ContactDestination branch,
        every EMAIL-matrix call site's existing behavior, unchanged); the
        PUSH-matrix call sites pass ``_pause_before_push_read`` instead.
        """
        pause_fn = pause_fn or self._pause_before_read
        paused = threading.Event()
        resume = threading.Event()
        racer_done = threading.Event()
        pause_fn(read_store, paused, resume)

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

    # -- the three PUSH/DeviceToken racing mutations (#426 round-3 review
    # finding 1) ------------------------------------------------------------
    def _create_push_race(self, ref):
        racer_api = ApiService(self._store())
        return lambda: racer_api.register_device_token(
            ref, "fcm", NEW_PUSH_TOKEN)

    def _update_push_race(self, store, api, ref):
        """DeviceToken's 'update' analogue.

        Unlike ContactDestination's ``set_contact_destination``, which
        overwrites ONE existing row's ``destination`` value in place,
        ``register_device_token``'s upsert key is ``(recipient_ref,
        token)`` — a DIFFERENT token value is always a DIFFERENT row, so
        there is no in-place value edit to race against.
        ``active_device_token_for`` returns the FIRST *active* token by
        id (``resolve_destination``'s own docstring), so seeding TWO
        active tokens ahead of the race — OLD (registered first, lower
        id) and NEW (registered second, higher id) — and having the racer
        deactivate ONLY OLD moves resolution from OLD's value to NEW's
        value in ONE atomic write: the real-value-to-a-DIFFERENT-real-
        value transition an 'update' race is meant to exercise, through
        DeviceToken's actual mechanism rather than forcing
        ContactDestination's shape onto a domain that doesn't have it.
        """
        api.register_device_token(ref, "fcm", OLD_PUSH_TOKEN)
        api.register_device_token(ref, "fcm", NEW_PUSH_TOKEN)
        old_id = next(t.id for t in store.device_tokens_for(ref)
                     if t.token == OLD_PUSH_TOKEN)
        racer_api = ApiService(self._store())

        def go():
            result = racer_api.set_device_token_active(
                old_id, False, actor_id="ops", actor_role=Role.LEAGUE_ADMIN)
            self.assertNotIn("error", result, result)
        return go

    def _retire_push_race(self, store, api, ref):
        tok_id = api.register_device_token(ref, "fcm", OLD_PUSH_TOKEN)["id"]
        racer_api = ApiService(self._store())

        def go():
            result = racer_api.set_device_token_active(
                tok_id, False, actor_id="ops", actor_role=Role.LEAGUE_ADMIN)
            self.assertNotIn("error", result, result)
        return go

    # -- enqueue() target --------------------------------------------------
    def _enqueue_target(self, store, notification):
        def go():
            created = enqueue(store, notification, channels=(EMAIL,))
            return next(d for d in created if d.channel == EMAIL)
        return go

    def _enqueue_push_target(self, store, notification):
        def go():
            created = enqueue(store, notification, channels=(PUSH,))
            return next(d for d in created if d.channel == PUSH)
        return go

    # -- retry-worker target ------------------------------------------------
    def _worker_target(self, store):
        def go():
            worker = DeliveryWorker(store, _clock)
            worker.process_pending()
            return next(d for d in store.all_notification_deliveries()
                        if d.channel == EMAIL)
        return go

    def _worker_push_target(self, store):
        def go():
            worker = DeliveryWorker(store, _clock)
            worker.process_pending()
            return next(d for d in store.all_notification_deliveries()
                        if d.channel == PUSH)
        return go

    def _seed_pending_delivery(self, store, api):
        # enqueue() with no destination registered yet -> a PENDING row
        # addressed to `ref`, resolved to the placeholder at emission time
        # (re-resolved for real by the worker under test below).
        oid, ref = self._seed_official(store, api)
        notification = _mk_notification(store, api, oid)
        enqueue(store, notification, channels=(EMAIL,))
        return ref

    def _seed_pending_push_delivery(self, store, api):
        oid, ref = self._seed_official(store, api)
        notification = _mk_notification(store, api, oid)
        enqueue(store, notification, channels=(PUSH,))
        return ref

    def _assert_agreement(self, store, ref, destination, expect_destination,
                          expect_audited, purpose="delivery_resolve"):
        self.assertEqual(destination, expect_destination)
        # Filtered to THIS resolution's own purpose — a retire racer's own
        # set_contact_destination_active/set_device_token_active call is
        # ITSELF an independent, already-covered sensitive read (its own
        # purpose="set_contact_destination_active"/"set_device_token_
        # active" row) and must not be mistaken for (or mask) the
        # delivery-resolution audit under test here. ``purpose`` defaults
        # to the ContactDestination branch's own value; PUSH-matrix call
        # sites pass "delivery_resolve_push_token" (#426 round-3 review
        # finding 1).
        rows = [r for r in store.list_data_access()
               if r.subject_id == ref and r.purpose == purpose]
        if not expect_audited:
            self.assertEqual(rows, [], rows)
            return
        self.assertEqual(len(rows), 1, rows)
        row = rows[0]
        self.assertEqual(row.outcome, "allowed")
        self.assertEqual(row.actor_role, "system")
        self.assertIsNone(row.actor_user_id)
        self.assertEqual(row.purpose, purpose)
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

    # -- PUSH/DeviceToken matrix (#426 round-3 review finding 1) — the
    # SAME 12-test shape as the EMAIL matrix above, against the worker's
    # DeviceToken branch instead of its ContactDestination one. See
    # _update_push_race's own docstring for why "update" here means
    # "the FIRST-active-by-id token changes", not an in-place value edit.

    def test_enqueue_push_create_race_before_uses_racers_value(self):
        store = self._store()
        api = ApiService(store)
        oid, ref = self._seed_official(store, api)
        notification = _mk_notification(store, api, oid)
        mutate = self._create_push_race(ref)
        d = self._run_race_before(store, mutate,
                                  self._enqueue_push_target(store, notification),
                                  pause_fn=self._pause_before_push_read)
        self._assert_agreement(store, ref, d.destination, NEW_PUSH_TOKEN,
                               True, purpose="delivery_resolve_push_token")

    def test_enqueue_push_create_race_after_uses_pre_race_placeholder(self):
        store = self._store()
        api = ApiService(store)
        oid, ref = self._seed_official(store, api)
        notification = _mk_notification(store, api, oid)
        mutate = self._create_push_race(ref)
        d = self._run_sequential(
            mutate, self._enqueue_push_target(store, notification))
        self.assertNotEqual(d.destination, NEW_PUSH_TOKEN)
        self._assert_agreement(store, ref, d.destination, d.destination,
                               False, purpose="delivery_resolve_push_token")

    def test_enqueue_push_update_race_before_uses_racers_value(self):
        store = self._store()
        api = ApiService(store)
        oid, ref = self._seed_official(store, api)
        notification = _mk_notification(store, api, oid)
        mutate = self._update_push_race(store, api, ref)
        d = self._run_race_before(store, mutate,
                                  self._enqueue_push_target(store, notification),
                                  pause_fn=self._pause_before_push_read)
        self._assert_agreement(store, ref, d.destination, NEW_PUSH_TOKEN,
                               True, purpose="delivery_resolve_push_token")

    def test_enqueue_push_update_race_after_uses_pre_race_value(self):
        store = self._store()
        api = ApiService(store)
        oid, ref = self._seed_official(store, api)
        notification = _mk_notification(store, api, oid)
        mutate = self._update_push_race(store, api, ref)
        d = self._run_sequential(
            mutate, self._enqueue_push_target(store, notification))
        self._assert_agreement(store, ref, d.destination, OLD_PUSH_TOKEN,
                               True, purpose="delivery_resolve_push_token")

    def test_enqueue_push_retire_race_before_uses_placeholder(self):
        store = self._store()
        api = ApiService(store)
        oid, ref = self._seed_official(store, api)
        notification = _mk_notification(store, api, oid)
        mutate = self._retire_push_race(store, api, ref)
        d = self._run_race_before(store, mutate,
                                  self._enqueue_push_target(store, notification),
                                  pause_fn=self._pause_before_push_read)
        self.assertNotEqual(d.destination, OLD_PUSH_TOKEN)
        self._assert_agreement(store, ref, d.destination, d.destination,
                               False, purpose="delivery_resolve_push_token")

    def test_enqueue_push_retire_race_after_uses_pre_race_value(self):
        store = self._store()
        api = ApiService(store)
        oid, ref = self._seed_official(store, api)
        notification = _mk_notification(store, api, oid)
        mutate = self._retire_push_race(store, api, ref)
        d = self._run_sequential(
            mutate, self._enqueue_push_target(store, notification))
        self._assert_agreement(store, ref, d.destination, OLD_PUSH_TOKEN,
                               True, purpose="delivery_resolve_push_token")

    def test_worker_push_create_race_before_uses_racers_value(self):
        store = self._store()
        api = ApiService(store)
        ref = self._seed_pending_push_delivery(store, api)
        mutate = self._create_push_race(ref)
        d = self._run_race_before(store, mutate,
                                  self._worker_push_target(store),
                                  pause_fn=self._pause_before_push_read)
        self._assert_agreement(store, ref, d.destination, NEW_PUSH_TOKEN,
                               True, purpose="delivery_resolve_push_token")

    def test_worker_push_create_race_after_uses_pre_race_placeholder(self):
        store = self._store()
        api = ApiService(store)
        ref = self._seed_pending_push_delivery(store, api)
        mutate = self._create_push_race(ref)
        d = self._run_sequential(mutate, self._worker_push_target(store))
        self.assertNotEqual(d.destination, NEW_PUSH_TOKEN)
        self._assert_agreement(store, ref, d.destination, d.destination,
                               False, purpose="delivery_resolve_push_token")

    def test_worker_push_update_race_before_uses_racers_value(self):
        store = self._store()
        api = ApiService(store)
        ref = self._seed_pending_push_delivery(store, api)
        mutate = self._update_push_race(store, api, ref)
        d = self._run_race_before(store, mutate,
                                  self._worker_push_target(store),
                                  pause_fn=self._pause_before_push_read)
        self._assert_agreement(store, ref, d.destination, NEW_PUSH_TOKEN,
                               True, purpose="delivery_resolve_push_token")

    def test_worker_push_update_race_after_uses_pre_race_value(self):
        store = self._store()
        api = ApiService(store)
        ref = self._seed_pending_push_delivery(store, api)
        mutate = self._update_push_race(store, api, ref)
        d = self._run_sequential(mutate, self._worker_push_target(store))
        self._assert_agreement(store, ref, d.destination, OLD_PUSH_TOKEN,
                               True, purpose="delivery_resolve_push_token")

    def test_worker_push_retire_race_before_uses_placeholder(self):
        store = self._store()
        api = ApiService(store)
        ref = self._seed_pending_push_delivery(store, api)
        mutate = self._retire_push_race(store, api, ref)
        d = self._run_race_before(store, mutate,
                                  self._worker_push_target(store),
                                  pause_fn=self._pause_before_push_read)
        self.assertNotEqual(d.destination, OLD_PUSH_TOKEN)
        self._assert_agreement(store, ref, d.destination, d.destination,
                               False, purpose="delivery_resolve_push_token")

    def test_worker_push_retire_race_after_uses_pre_race_value(self):
        store = self._store()
        api = ApiService(store)
        ref = self._seed_pending_push_delivery(store, api)
        mutate = self._retire_push_race(store, api, ref)
        d = self._run_sequential(mutate, self._worker_push_target(store))
        self._assert_agreement(store, ref, d.destination, OLD_PUSH_TOKEN,
                               True, purpose="delivery_resolve_push_token")


class SqliteRaceTest(_FullRaceMatrixTests, unittest.TestCase):
    def setUp(self):
        fd, self._tmp = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        SqlStore(self._tmp).close()  # migrate, then release
        # Registered BEFORE any test method (and therefore any _store()
        # call) runs, so addCleanup's LIFO order closes every tracked
        # store first and removes the file last (#426 round-3 review
        # finding 2) — see _RaceHelpers._track_store's own docstring.
        self.addCleanup(os.remove, self._tmp)

    def _store(self):
        return self._track_store(SqlStore(self._tmp))


@unittest.skipUnless(os.environ.get("TEST_DATABASE_URL"),
                     "PostgreSQL suite only (TEST_DATABASE_URL not set)")
class PostgresRaceTest(_FullRaceMatrixTests, unittest.TestCase):
    def setUp(self):
        self.url = os.environ["TEST_DATABASE_URL"]
        # The one-off connection used only to reset the schema is tracked
        # too (#426 round-3 review finding 2) — it used to be a bare,
        # never-closed `SqlStore(self.url)`.
        self._track_store(SqlStore(self.url)).clear_all_data()

    def _store(self):
        return self._track_store(SqlStore(self.url))


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

    def _fresh_pending_push_delivery(self):
        store = InMemoryStore()
        api = ApiService(store)
        oid = api.create_official("Race Official", actor_id="seed")["id"]
        ref = f"official:{oid}"
        notification = _mk_notification(store, api, oid)
        enqueue(store, notification, channels=(PUSH,))
        return store, ref

    def _assert_agreement(self, store, ref, destination, expect_destination,
                          expect_audited, purpose="delivery_resolve"):
        self.assertEqual(destination, expect_destination)
        rows = [r for r in store.list_data_access()
               if r.subject_id == ref and r.purpose == purpose]
        if not expect_audited:
            self.assertEqual(rows, [], rows)
            return
        self.assertEqual(len(rows), 1, rows)
        row = rows[0]
        self.assertEqual(row.outcome, "allowed")
        self.assertEqual(row.actor_role, "system")
        self.assertEqual(row.purpose, purpose)

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

    # -- PUSH/DeviceToken parity (#426 round-3 review finding 1) — same
    # CREATE-only scope as the EMAIL parity above, for the same reason
    # (this class's own docstring): Memory's full-lock model only ever
    # produces one of two total orderings, and plain sequential calls,
    # in each order, are a complete parity proof for both of them.

    def test_enqueue_push_create_before_uses_created_value(self):
        store = InMemoryStore()
        api = ApiService(store)
        oid = api.create_official("Race Official", actor_id="seed")["id"]
        ref = f"official:{oid}"
        notification = _mk_notification(store, api, oid)
        api.register_device_token(ref, "fcm", NEW_PUSH_TOKEN)
        created = enqueue(store, notification, channels=(PUSH,))
        d = next(x for x in created if x.channel == PUSH)
        self._assert_agreement(store, ref, d.destination, NEW_PUSH_TOKEN,
                               True, purpose="delivery_resolve_push_token")

    def test_enqueue_push_create_after_uses_pre_race_placeholder(self):
        store = InMemoryStore()
        api = ApiService(store)
        oid = api.create_official("Race Official", actor_id="seed")["id"]
        ref = f"official:{oid}"
        notification = _mk_notification(store, api, oid)
        created = enqueue(store, notification, channels=(PUSH,))
        d = next(x for x in created if x.channel == PUSH)
        api.register_device_token(ref, "fcm", NEW_PUSH_TOKEN)
        self.assertNotEqual(d.destination, NEW_PUSH_TOKEN)
        self._assert_agreement(store, ref, d.destination, d.destination,
                               False, purpose="delivery_resolve_push_token")

    def test_worker_push_create_before_uses_created_value(self):
        store, ref = self._fresh_pending_push_delivery()
        ApiService(store).register_device_token(ref, "fcm", NEW_PUSH_TOKEN)
        DeliveryWorker(store, _clock).process_pending()
        d = next(x for x in store.all_notification_deliveries()
                if x.channel == PUSH)
        self._assert_agreement(store, ref, d.destination, NEW_PUSH_TOKEN,
                               True, purpose="delivery_resolve_push_token")

    def test_worker_push_create_after_uses_pre_race_placeholder(self):
        store, ref = self._fresh_pending_push_delivery()
        DeliveryWorker(store, _clock).process_pending()
        d = next(x for x in store.all_notification_deliveries()
                if x.channel == PUSH)
        ApiService(store).register_device_token(ref, "fcm", NEW_PUSH_TOKEN)
        self.assertNotEqual(d.destination, NEW_PUSH_TOKEN)
        self._assert_agreement(store, ref, d.destination, d.destination,
                               False, purpose="delivery_resolve_push_token")

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


class CloseTrackingHarnessTest(_RaceHelpers, unittest.TestCase):
    """Proves ``_RaceHelpers._track_store`` — the actual fix above, not a
    reimplementation of it — really closes every SqlStore it registers on
    the ordinary SUCCESS path AND when the code between opening a store
    and the test method returning RAISES (a racer/target failure), rather
    than merely asserting the code SHAPE looks right (#426 round-3 review
    finding 2). Mixing in ``_RaceHelpers`` itself (the SAME base
    ``SqliteRaceTest``/``PostgresRaceTest`` use) means ``self._store()``
    below IS the identical ``_track_store(SqlStore(...))`` call those
    classes make — this harness exercises the real method, not a parallel
    stand-in that could quietly drift from it.

    Forces ``doCleanups()`` to run MID-test (a legitimate, public
    ``unittest.TestCase`` API — cleanups are a LIFO stack that
    ``doCleanups()`` drains; calling it early just runs what is queued so
    far, and the automatic post-``tearDown`` call that follows finds an
    empty stack and no-ops) so the close can be observed and asserted on
    directly, rather than trusting the framework's own documented
    "cleanups always run" guarantee on faith.
    """

    def setUp(self):
        fd, self._tmp = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        SqlStore(self._tmp).close()  # migrate, then release
        self.addCleanup(os.remove, self._tmp)

    def _store(self):
        return self._track_store(SqlStore(self._tmp))

    def _new_tracked_store(self):
        return self._store()

    def test_tracked_stores_are_closed_after_a_successful_test(self):
        stores = [self._new_tracked_store() for _ in range(3)]
        for s in stores:
            self.assertFalse(s._closed)
        self.doCleanups()
        for s in stores:
            self.assertTrue(s._closed)

    def test_tracked_stores_are_still_closed_when_the_body_raises(self):
        stores = [self._new_tracked_store() for _ in range(3)]

        def racer():
            raise RuntimeError("injected racer failure")

        with self.assertRaises(RuntimeError):
            racer()
        # The injected failure propagated (proving it was never
        # swallowed) — now prove doCleanups() STILL closes every tracked
        # store, exactly as unittest will do automatically once this
        # method itself returns/raises.
        self.doCleanups()
        for s in stores:
            self.assertTrue(s._closed)

    def test_a_racer_that_opens_a_store_then_raises_still_closes_it(self):
        # Closer to the real shape this fix protects: a store is opened
        # (mirroring _create_race/_update_race/_retire_race's
        # racer_api = ApiService(self._store())), THEN the operation that
        # uses it raises — the store must already be tracked BEFORE that
        # failure, not after, since there is no "after" for a raise.
        store = self._new_tracked_store()
        api = ApiService(store)

        def racer():
            api.set_contact_destination("official:missing", "email", "x")
            raise RuntimeError("injected failure after using the store")

        with self.assertRaises(RuntimeError):
            racer()
        self.doCleanups()
        self.assertTrue(store._closed)

    def test_multiple_racers_all_close_even_when_one_of_them_raises(self):
        # The exact _create_race/_update_race/_retire_race shape: TWO
        # stores open per test (primary + racer) — one succeeds, the
        # other's operation raises. Neither store's close may be skipped
        # because of the other's failure.
        primary = self._new_tracked_store()
        racer_store = self._new_tracked_store()
        ApiService(primary).create_official("Race Official", actor_id="seed")

        def racer():
            ApiService(racer_store).set_contact_destination(
                "official:missing", "email", "x")
            raise RuntimeError("injected racer failure")

        with self.assertRaises(RuntimeError):
            racer()
        self.doCleanups()
        self.assertTrue(primary._closed)
        self.assertTrue(racer_store._closed)


if __name__ == "__main__":
    unittest.main()
