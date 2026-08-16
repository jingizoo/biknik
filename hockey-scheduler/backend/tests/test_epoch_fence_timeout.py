"""Round-N review finding 2: the PostgreSQL fence FAILS OPEN on timeout, and
before this file's fix ``_read_under_context_gate`` ignored that failure
entirely.

THE BUG, verbatim from the review. ``_epoch_fence_shared_postgres`` yields
``False`` when ``pg_advisory_lock_shared`` hits ``SET LOCAL lock_timeout``
against a held EXCLUSIVE holder (SQLSTATE 55P03) -- a DELIBERATE fail-open,
matching ``ContextSwitchGate``'s own choice: a scoped read that can't get the
fence must not be able to lock out a writer. But ``web/server.py``'s
``_read_under_context_gate`` used to do:

    with ... store.epoch_fence_acquire_shared(key1), \\
         ... store.epoch_fence_acquire_shared(key2):
        current = current_epoch(...)
        ...
        return produce()

-- entering the ``with`` blocks on ``False`` exactly as readily as on
``True``, and NEVER LOOKING AT the yielded value. So a read that could not
confirm "no exclusive writer is held" proceeded to derive the epoch and call
``produce()`` completely unprotected. If the exclusive writer that outlasted
the bound then committed between those two calls, the response could observe
exactly the same torn state the whole fence exists to prevent -- served,
not discarded.

THE FIX (bounded, "a concrete bug fix" per the review's own words):
``_read_under_context_gate`` now captures both yielded values and, on ANY
``False``, returns ``DISCARDED_READ`` -- the SAME empty-204/no-service-call
path an epoch MISMATCH takes -- before ``current_epoch()`` or ``produce()``
is ever called. This file proves that, against a REAL PostgreSQL server: a
writer holds each exclusive key past a SHORT, test-configured timeout, and
the read is required to time out, discard (204), and never touch the real
``ApiService`` method at all (a genuine ZERO-call spy, unlike round-N finding
1's version-check tests -- there is no "let it run and discard the result"
here; the fence check happens strictly BEFORE the epoch is even derived).
Both the per-user and the global key are covered, since the review named
both explicitly, and a no-leak check confirms releasing the writer leaves no
trace a THIRD connection would have to wait behind.

REQUIRES A REAL POSTGRESQL SERVER (``TEST_DATABASE_URL``) -- the failure mode
under test is a real ``pg_advisory_lock_shared`` timing out against a real
``pg_advisory_xact_lock``, which SQLite/Memory cannot reproduce (their
shared-fence halves are documented no-ops that never even attempt to wait).
"""

import os
import threading
import time
import unittest
import uuid

from helpers import BACKEND  # noqa: F401  (ensures sys.path is set up)

from hockey_scheduler.services.context_epoch import CONTEXT_EPOCH_HEADER
from hockey_scheduler.services.epoch_fence import (
    EPOCH_FENCE_GLOBAL_KEY, user_fence_key)
from hockey_scheduler.store import SqlStore
from test_context_switch_server_exit import (
    PATIENCE, ContextGateFixtureBase)

_SHORT_TIMEOUT_SECONDS = "1"


def _pg_url():
    return os.environ.get("TEST_DATABASE_URL")


@unittest.skipUnless(_pg_url(), "PostgreSQL required (set TEST_DATABASE_URL)")
class EpochFenceTimeoutFailClosedHttpTest(ContextGateFixtureBase,
                                          unittest.TestCase):
    """Real PostgreSQL, real ``ThreadingHTTPServer``, a genuinely independent
    writer connection holding the advisory lock past the reader's own bound.
    """

    STORE_URL = _pg_url()

    def setUp(self):
        super().setUp()
        self._set_epoch_fence_timeout_env(_SHORT_TIMEOUT_SECONDS)

    # -- the independent writer, held past the reader's bound ---------------
    def _hold_exclusive_fence(self, key):
        """Open a FRESH, independent ``SqlStore`` -- its own psycopg
        connection, nothing shared with the server under test -- take the
        EXCLUSIVE side of the fence for ``key``, and hold the transaction
        open (uncommitted) until ``release`` is set. Returns
        ``(store, thread, release_event, held_event)``; the caller must
        ``release_event.set(); thread.join(...)`` and ``store.close()``.
        """
        store = SqlStore(self.STORE_URL)
        held = threading.Event()
        release = threading.Event()

        def run():
            with store.transaction(isolation="SERIALIZABLE"):
                store.epoch_fence_acquire_exclusive(key)
                held.set()
                release.wait(PATIENCE)

        t = threading.Thread(target=run, daemon=True)
        t.start()
        self.assertTrue(held.wait(PATIENCE),
                        "writer never reported holding the exclusive fence")
        return store, t, release

    def _watch(self, method_name):
        calls = []

        def factory(original):
            def wrapper(sid, *a, **kw):
                calls.append(sid)
                return original(sid, *a, **kw)
            return wrapper

        self._wrap(self.api, method_name, factory)
        return calls

    def _run_the_race(self, holder_key_fn):
        """Shared body: hold ``holder_key_fn(user_id)`` exclusively past the
        configured timeout, issue a real scoped read with the current epoch,
        and require it to time out, discard, and never call the service.
        """
        fx = self._program_with_two_seasons("Fo" + uuid.uuid4().hex[:6])
        username, user_id = self._operator("foto")
        client = self._login(username)
        self._select(client, fx["program_id"], fx["s1"])
        epoch = self._req(client, "GET", "/api/context")[2]["context_epoch"]

        calls = self._watch("get_venue_grant_candidates")

        holder_store, holder_thread, release = self._hold_exclusive_fence(
            holder_key_fn(user_id))
        try:
            t0 = time.monotonic()
            status, raw, _body = self._req(
                client, "GET",
                f"/api/v2/setup/seasons/{fx['s1']}/venue-candidates",
                headers={CONTEXT_EPOCH_HEADER: epoch},
                timeout=PATIENCE)
            elapsed = time.monotonic() - t0
        finally:
            release.set()
            holder_thread.join(PATIENCE)
            holder_store.close()

        self.assertEqual(
            status, 204,
            f"a read that could not confirm the fence must fail CLOSED "
            f"(discard), never serve unprotected: got {status} {raw!r}")
        self.assertEqual(raw, "", f"a 204 must carry no body: {raw!r}")
        self.assertEqual(
            calls, [],
            "the service must NEVER have been called -- unlike finding 1's "
            "version-check design, this discard happens strictly BEFORE "
            "current_epoch()/produce() are reached at all")
        # Genuinely waited close to the configured bound (not an instant
        # short-circuit that would pass by accident) and did not hang well
        # past it either.
        self.assertGreaterEqual(
            elapsed, float(_SHORT_TIMEOUT_SECONDS) * 0.5,
            f"the read returned suspiciously fast ({elapsed:.2f}s) for a "
            f"{_SHORT_TIMEOUT_SECONDS}s configured bound -- did it actually "
            f"attempt to acquire the fence at all?")
        self.assertLess(
            elapsed, PATIENCE,
            f"the read took {elapsed:.2f}s -- it must be bounded by the "
            f"configured lock_timeout, not hang")
        return fx, user_id

    def test_user_key_timeout_fails_closed(self):
        """The PER-USER key (rows 1-3: context switch, scope rebind,
        account activate) held past the bound."""
        self._run_the_race(user_fence_key)

    def test_global_key_timeout_fails_closed(self):
        """The GLOBAL key (rows 4-17) held past the bound."""
        self._run_the_race(lambda _user_id: EPOCH_FENCE_GLOBAL_KEY)

    def test_no_lock_or_connection_leak_after_the_timeout(self):
        """Releasing the writer must leave NOTHING behind for a THIRD,
        independent connection to wait behind -- the reader's own FAILED
        acquisition attempt (SQLSTATE 55P03) must not itself have left a
        dangling connection or a stuck lock. Mirrors
        ``EpochFenceRollbackReleasesTest``'s existing no-leak pattern,
        applied to the NEW code path (a shared acquisition that itself
        timed out) rather than only the exclusive/rollback one it already
        covers."""
        fx, user_id = self._run_the_race(lambda _uid: EPOCH_FENCE_GLOBAL_KEY)
        third = SqlStore(self.STORE_URL)
        try:
            t0 = time.monotonic()
            with third.transaction():
                third.epoch_fence_acquire_exclusive(EPOCH_FENCE_GLOBAL_KEY)
            elapsed = time.monotonic() - t0
        finally:
            third.close()
        self.assertLess(
            elapsed, 1.0,
            f"a third, independent connection waited {elapsed:.2f}s to "
            f"acquire the SAME global key after the timed-out reader's "
            f"attempt and the writer's release -- something leaked")


if __name__ == "__main__":
    unittest.main()
