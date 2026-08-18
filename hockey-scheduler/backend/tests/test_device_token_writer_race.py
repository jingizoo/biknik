"""#426 round-4 review finding 1: register_device_token/set_device_token_
active writer-vs-writer races.

``register_device_token()``'s upsert used to be an UNLOCKED
``get_device_token_by_value()`` read followed by a separate
``save_device_token()``/``add_device_token()`` write, entirely outside any
transaction. ``set_device_token_active()`` opened a transaction but read
via the plain ``get_device_token()`` — no row lock at all, unlike its
ContactDestination sibling's ``get_contact_destination_for_update()``.
``SqlStore._update()`` writes every column, and migration 001 had no
unique constraint on ``(recipient_ref, token)``. Two concrete failures
followed: (1) if a toggle read a token and a concurrent re-registration of
that SAME value committed a new provider/label before the toggle's own
save, the toggle's stale full-row save silently reverted the
re-registration's already-committed change; (2) two concurrent FIRST
registrations of the identical ``(recipient_ref, token)`` pair could both
observe "no row" and both insert, leaving two rows for one logical
registration.

THE FIX: migration 055's UNIQUE index on ``(recipient_ref, token)`` plus
``store.upsert_device_token``'s single atomic ``INSERT ... ON CONFLICT ...
DO UPDATE`` (closes failure 2 by construction — the conflict resolves
inside ONE statement, never a separate read-then-decide) and
``store.get_device_token_for_update``'s real row lock, used by
``set_device_token_active`` (closes failure 1 — the toggle's read and the
upsert's conflict resolution now serialize on the SAME physical row in
EITHER commit order).

This module proves both fixes hold under REAL two-connection contention on
PostgreSQL and file-backed SQLite, plus a same-process Memory parity check.

TOGGLE VS RE-REGISTRATION, BOTH COMMIT ORDERS:

* ``locked_before``: the toggle's transaction takes the row lock (via
  ``get_device_token_for_update``) and PAUSES while still holding it. The
  concurrent re-registration is started and PROVEN to genuinely block
  (never observed completing while the toggle still holds the lock) — not
  a timing coincidence, real mutual exclusion. Resuming the toggle lets it
  commit (STALE pre-race provider/label, its own requested active flag);
  the re-registration then unblocks and commits SECOND (its own submitted
  provider/label, always active=True). Both responses and the FINAL row
  are asserted, plus the toggle's own audit readback.
* ``sequential_after``: the re-registration runs to FULL completion first
  (new provider/label committed), THEN the toggle runs to completion. The
  toggle's own locked read must observe the FRESH (not stale)
  provider/label — proving the row lock's re-fetch, not a
  pre-transaction snapshot, is what the mutation and its audit readback
  are based on. Plain sequential calls are a complete, deterministic proof
  of this ordering (the earlier write has FULLY committed and released
  before the later transaction even opens — there is no interleaving
  window left to matter), mirroring test_delivery_resolve_audit_race.py's
  own ``_run_sequential`` reasoning for its "after" interleaving.

SIMULTANEOUS FIRST REGISTRATION: two threads register the SAME brand-new
``(recipient_ref, token)`` pair with their OWN start synchronized by a
``threading.Barrier`` — there is no internal pause point left to patch
(``upsert_device_token`` is one atomic statement, by design) — proving
migration 055 plus the atomic upsert leave exactly one row, both requests
reporting success, regardless of which statement the engine happens to
execute first.
"""

import os
import tempfile
import threading
import unittest

from helpers import BACKEND  # noqa: F401

from hockey_scheduler.api import ApiService
from hockey_scheduler.domain import ACCESS_ALLOWED, Role
from hockey_scheduler.store import InMemoryStore, SqlStore

SEED_TOKEN = "writer-race-seed-token"
NEW_PAIR_TOKEN = "writer-race-new-pair-token"


class _WriterRaceHelpers:
    """Shared body; subclasses provide ``self._store()`` -> a fresh
    connection (a genuinely separate SqlStore/connection for SQLite/
    Postgres) or the SAME shared store (Memory — see
    MemoryWriterRaceTest's own docstring)."""

    def _track_store(self, store):
        self.addCleanup(store.close)
        return store

    def _seed_official(self, store, api):
        oid = api.create_official("Writer Race Official", actor_id="seed")["id"]
        return oid, f"official:{oid}"

    def _pause_before_locked_read(self, store, paused, resume):
        orig = store.get_device_token_for_update

        def instrumented(token_id):
            row = orig(token_id)
            paused.set()
            assert resume.wait(15), "resume never set"
            return row
        store.get_device_token_for_update = instrumented

    def _run_locked_before(self, lock_store, primary_fn, secondary_fn):
        """``primary_fn``'s transaction takes ``get_device_token_for_
        update``'s row lock and pauses WHILE STILL HOLDING IT;
        ``secondary_fn`` is started concurrently and asserted to still be
        blocked after a short wait — genuine mutual exclusion, not a
        timing coincidence. Resuming lets ``primary_fn`` commit (and
        release), after which ``secondary_fn`` completes. Returns
        ``(primary_result, secondary_result, secondary_was_blocked)``.
        """
        paused = threading.Event()
        resume = threading.Event()
        self._pause_before_locked_read(lock_store, paused, resume)

        results = {}
        errors = {}

        def run_primary():
            try:
                results["primary"] = primary_fn()
            except BaseException as exc:  # noqa: BLE001 - surface, don't hide
                errors["primary"] = exc

        def run_secondary():
            try:
                results["secondary"] = secondary_fn()
            except BaseException as exc:  # noqa: BLE001 - surface, don't hide
                errors["secondary"] = exc

        t_primary = threading.Thread(target=run_primary)
        t_primary.start()
        assert paused.wait(15), "primary never reached its locked read"

        t_secondary = threading.Thread(target=run_secondary)
        t_secondary.start()
        # Confirm secondary is genuinely blocked on the lock, not merely
        # slow to start — give it a real window, then check it has NOT
        # finished while primary still holds the row lock.
        t_secondary.join(1.5)
        blocked = t_secondary.is_alive()

        resume.set()
        t_primary.join(15)
        t_secondary.join(15)
        if errors:
            raise AssertionError(f"race harness thread(s) raised: {errors}")
        if t_primary.is_alive() or t_secondary.is_alive():
            raise AssertionError("a race harness thread never completed")
        return results["primary"], results["secondary"], blocked

    def _audit_rows(self, store, ref, purpose="set_device_token_active"):
        return [r for r in store.list_data_access()
               if r.subject_id == ref and r.purpose == purpose]


class _WriterRaceContract(_WriterRaceHelpers):
    """Backend-parametrised test bodies; concrete subclasses supply
    ``self._store()``."""

    def test_toggle_locked_before_reregistration_blocks_then_final_state_is_consistent(self):
        store = self._store()
        api = ApiService(store)
        _oid, ref = self._seed_official(store, api)
        first = api.register_device_token(ref, "fcm", SEED_TOKEN, label="orig-label")
        self.assertNotIn("error", first, first)
        tok_id = first["id"]

        api_toggle = ApiService(self._store())
        api_reregister = ApiService(self._store())

        def do_toggle():
            return api_toggle.set_device_token_active(
                tok_id, False, actor_id="ops", actor_role=Role.LEAGUE_ADMIN)

        def do_reregister():
            return api_reregister.register_device_token(
                ref, "apns", SEED_TOKEN, label="new-label-from-race")

        toggle_resp, reregister_resp, blocked = self._run_locked_before(
            api_toggle.store, do_toggle, do_reregister)

        self.assertTrue(
            blocked,
            "re-registration completed while the toggle still held the row "
            "lock -- the two writers are not actually serialized")
        self.assertNotIn("error", toggle_resp, toggle_resp)
        self.assertNotIn("error", reregister_resp, reregister_resp)

        # Toggle committed FIRST in this ordering: its own response
        # reflects what it read BEFORE the race (a stale provider/label is
        # expected and correct here -- it genuinely committed first), with
        # its own requested active flag.
        self.assertEqual(toggle_resp["provider"], "fcm")
        self.assertEqual(toggle_resp["label"], "orig-label")
        self.assertFalse(toggle_resp["active"])

        # Re-registration's response ALWAYS echoes its own submitted values
        # (never gated -- see register_device_token's own docstring).
        self.assertEqual(reregister_resp["provider"], "apns")
        self.assertEqual(reregister_resp["label"], "new-label-from-race")
        self.assertTrue(reregister_resp["active"])
        self.assertEqual(reregister_resp["id"], tok_id)

        # THE invariant this finding is about: re-registration's LATER,
        # already-serialized commit must survive -- never silently
        # reverted by the toggle's own stale full-row save.
        final = store.get_device_token(tok_id)
        self.assertEqual(final.provider, "apns")
        self.assertEqual(final.label, "new-label-from-race")
        self.assertTrue(final.active)

        # The toggle's own audit readback (#124/#426 round-3 review
        # finding 1): recorded inside its own transaction, reflecting what
        # IT read and did.
        rows = self._audit_rows(store, ref)
        self.assertEqual(len(rows), 1, rows)
        self.assertEqual(rows[0].outcome, ACCESS_ALLOWED)
        self.assertEqual(rows[0].actor_role, "league_admin")
        self.assertEqual(rows[0].actor_user_id, "ops")

    def test_reregistration_sequential_before_toggle_sees_fresh_values(self):
        store = self._store()
        api = ApiService(store)
        _oid, ref = self._seed_official(store, api)
        first = api.register_device_token(ref, "fcm", SEED_TOKEN, label="orig-label")
        self.assertNotIn("error", first, first)
        tok_id = first["id"]

        api_reregister = ApiService(self._store())
        reregister_resp = api_reregister.register_device_token(
            ref, "apns", SEED_TOKEN, label="new-label-from-race")
        self.assertNotIn("error", reregister_resp, reregister_resp)

        api_toggle = ApiService(self._store())
        toggle_resp = api_toggle.set_device_token_active(
            tok_id, False, actor_id="ops", actor_role=Role.LEAGUE_ADMIN)
        self.assertNotIn("error", toggle_resp, toggle_resp)

        # The toggle's locked read must observe the FRESH, already-
        # committed re-registration -- not a stale pre-transaction
        # snapshot -- and its own write only changes `active`.
        self.assertEqual(toggle_resp["provider"], "apns")
        self.assertEqual(toggle_resp["label"], "new-label-from-race")
        self.assertFalse(toggle_resp["active"])

        final = store.get_device_token(tok_id)
        self.assertEqual(final.provider, "apns")
        self.assertEqual(final.label, "new-label-from-race")
        self.assertFalse(final.active)

        rows = self._audit_rows(store, ref)
        self.assertEqual(len(rows), 1, rows)
        self.assertEqual(rows[0].outcome, ACCESS_ALLOWED)
        self.assertEqual(rows[0].actor_role, "league_admin")

    def test_simultaneous_first_registration_leaves_exactly_one_row(self):
        store = self._store()
        api = ApiService(store)
        _oid, ref = self._seed_official(store, api)

        api_a = ApiService(self._store())
        api_b = ApiService(self._store())
        barrier = threading.Barrier(2, timeout=15)
        results = {}
        errors = {}

        def go(which, api_x, label):
            try:
                barrier.wait()
                results[which] = api_x.register_device_token(
                    ref, "fcm", NEW_PAIR_TOKEN, label=label)
            except BaseException as exc:  # noqa: BLE001 - surface, don't hide
                errors[which] = exc

        t_a = threading.Thread(target=go, args=("A", api_a, "label-A"))
        t_b = threading.Thread(target=go, args=("B", api_b, "label-B"))
        t_a.start()
        t_b.start()
        t_a.join(15)
        t_b.join(15)
        if errors:
            raise AssertionError(f"race harness thread(s) raised: {errors}")
        if t_a.is_alive() or t_b.is_alive():
            raise AssertionError("a race harness thread never completed")

        self.assertNotIn("error", results["A"], results["A"])
        self.assertNotIn("error", results["B"], results["B"])
        self.assertEqual(
            results["A"]["id"], results["B"]["id"],
            "both registrations must resolve to the SAME row")

        rows = [t for t in store.device_tokens_for(ref)
               if t.token == NEW_PAIR_TOKEN]
        self.assertEqual(len(rows), 1, rows)
        self.assertTrue(rows[0].active)
        self.assertIn(rows[0].label, ("label-A", "label-B"))


class SqliteWriterRaceTest(_WriterRaceContract, unittest.TestCase):
    def setUp(self):
        fd, self._tmp = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        SqlStore(self._tmp).close()  # migrate, then release
        self.addCleanup(os.remove, self._tmp)

    def _store(self):
        return self._track_store(SqlStore(self._tmp))


@unittest.skipUnless(os.environ.get("TEST_DATABASE_URL"),
                     "PostgreSQL suite only (TEST_DATABASE_URL not set)")
class PostgresWriterRaceTest(_WriterRaceContract, unittest.TestCase):
    def setUp(self):
        self.url = os.environ["TEST_DATABASE_URL"]
        self._track_store(SqlStore(self.url)).clear_all_data()

    def _store(self):
        return self._track_store(SqlStore(self.url))


class MemoryWriterRaceTest(_WriterRaceContract, unittest.TestCase):
    """Same-process Memory parity (#426 round-4 review finding 1): unlike
    the SQL matrix, ``self._store()`` deliberately returns the SAME
    ``InMemoryStore`` instance every call within one test — mirroring
    ``test_delivery_resolve_audit_race.py``'s ``MemoryRaceParityTest``
    reasoning. ``InMemoryStore.transaction()`` takes its single
    process-wide ``self._lock`` (a ``threading.RLock``) for its ENTIRE
    body: two DIFFERENT threads each opening ``store.transaction()``
    against the SAME instance genuinely serialize on that lock exactly
    like two PostgreSQL connections serialize on a row lock — coarser
    (whole-store, not per-row), but observably identical for this
    module's two-party races, so real cross-thread contention over that
    SAME lock is the correct proof here too, not merely a substitute for
    one.
    """

    def setUp(self):
        self._shared = InMemoryStore()

    def _store(self):
        return self._shared


if __name__ == "__main__":
    unittest.main()
