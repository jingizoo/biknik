"""The web process must BOOT and BIND when its database is unreachable.

THE PRODUCTION FAILURE THIS EXISTS FOR. A managed Postgres whose host stopped
resolving took the entire web service down::

    File ".../web/server.py", line 571, in <module>
        STATE = DemoState()
    ...
    psycopg.OperationalError: failed to resolve host 'dpg-...-a':
        [Errno -2] Name or service not known
    ==> Exited with status 1
    ==> No open ports detected, continuing to scan...

``STATE = DemoState()`` runs at MODULE IMPORT, so the exception propagated out
of ``from .server import serve`` and the process died before binding a port.
The platform then had nothing to talk to: no ``/api/health``, no
``/api/readiness``, no logs from a running instance, and a restart loop that
could not recover on its own even after the database came back.

WHAT MAKES THAT PARTICULARLY WRONG HERE: this server ALREADY has the guards for
exactly this condition. ``/api/health`` deliberately answers 503 rather than 200
"or a bricked instance is never restarted", and ``/api/readiness`` exists to
report deployment problems. An eager import-time connection defeated both --
the endpoints could not answer because the module could not finish importing.

THE CONTRACT ASSERTED BELOW, in the order it matters:

  1. importing the module with an unreachable store SUCCEEDS;
  2. the server BINDS its port anyway -- this is the difference between an
     instance you can diagnose and `No open ports detected`;
  3. ``/api/health`` and ``/api/readiness`` ANSWER, with 503 and the real
     reason, because reporting this is their entire purpose;
  4. every data route answers a clear 503 ``store_unavailable`` rather than a
     traceback;
  5. the instance RECOVERS on the next request once the store returns -- no
     redeploy, no manual restart;
  6. none of this changes anything when the store is healthy.

FALSIFICATION: restore the bare ``self.reset(seed=False)`` in
``DemoState.__init__`` and case 1 dies at import, taking 2-5 with it. Remove the
``STATE.store_available`` guard in ``serve()`` and the port stops binding, so
case 2 fails while 1 still passes -- which is why they are separate cases.
"""

import json
import subprocess
import sys
import tempfile
import time
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from helpers import BACKEND  # noqa: F401  (ensures sys.path is set up)

from hockey_scheduler.web import server as srv

# A host that cannot resolve, shaped like the Render internal name that failed.
DEAD_URL = "postgresql://u@dpg-doesnotexist-a/db"


class BootWithoutStoreTest(unittest.TestCase):
    maxDiff = None

    def _serve(self):
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), srv.Handler)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(httpd.server_close)
        self.addCleanup(thread.join, 5)
        self.addCleanup(httpd.shutdown)
        return httpd.server_address[1]

    def _get(self, port, path):
        try:
            with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}{path}", timeout=10) as r:
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            with e:
                return e.code, json.loads(e.read() or b"{}")

    def _break_store(self):
        """Put the live STATE into the failed-boot condition, and restore it.

        POINTING ``DATABASE_URL`` AT THE DEAD HOST IS NOT OPTIONAL. Nulling
        ``api`` alone is not the production condition: the guard retries on
        every request, and with no ``DATABASE_URL`` the retry immediately
        succeeds against the in-memory store, so the server answers 200 and the
        assertions below pass for the wrong reason. The environment has to make
        reconnection FAIL for as long as the outage is being simulated.
        """
        import os
        original_api = srv.STATE.api
        original_error = getattr(srv.STATE, "boot_error", None)
        original_url = os.environ.get("DATABASE_URL")
        original_mode = os.environ.get("APP_MODE")

        def _restore():
            for key, value in (("DATABASE_URL", original_url),
                               ("APP_MODE", original_mode)):
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

        # EVERY piece of degraded state is restored, not just `api`. Leaving
        # `boot_error_ref`, `_runtime_started`, `_next_retry_at` or
        # `_retry_backoff` behind makes later tests order-dependent -- most
        # sharply `_runtime_started`, which would leave the restored healthy
        # facade marked started while owning no loop.
        original_ref = getattr(srv.STATE, "boot_error_ref", None)
        original_started = getattr(srv.STATE, "_runtime_started", False)
        original_next = getattr(srv.STATE, "_next_retry_at", 0.0)
        original_backoff = getattr(srv.STATE, "_retry_backoff", 0.0)
        original_clock = getattr(srv.STATE, "_clock", None)
        self.addCleanup(_restore)
        self.addCleanup(setattr, srv.STATE, "_clock", original_clock)
        self.addCleanup(setattr, srv.STATE, "_retry_backoff", original_backoff)
        self.addCleanup(setattr, srv.STATE, "_next_retry_at", original_next)
        self.addCleanup(setattr, srv.STATE, "_runtime_started", original_started)
        self.addCleanup(setattr, srv.STATE, "boot_error_ref", original_ref)
        self.addCleanup(setattr, srv.STATE, "boot_error", original_error)
        self.addCleanup(setattr, srv.STATE, "api", original_api)
        os.environ["APP_MODE"] = "production"
        os.environ["DATABASE_URL"] = DEAD_URL
        srv.STATE.api = None
        srv.STATE.boot_error = "OperationalError: failed to resolve host"
        # A real degraded boot always recorded a reference (via
        # `_record_boot_error`), and health/readiness no longer mint one --
        # they are exempted BEFORE any connect attempt, by design.
        srv.STATE.boot_error_ref = "fixture0"
        srv.STATE._next_retry_at = 0.0
        srv.STATE._retry_backoff = 0.0

    # -- 1: the import itself ------------------------------------------------
    def test_importing_the_server_survives_an_unreachable_store(self):
        """The exact production crash: it must not happen in a subprocess.

        Run out-of-process because the failure being reproduced is an IMPORT
        failure -- this module has already imported the server, so an in-process
        check could never observe it.
        """
        code = ("from hockey_scheduler.web.server import serve, STATE;"
                "print('AVAILABLE' if STATE.store_available else 'DEGRADED')")
        proc = subprocess.run(
            [sys.executable, "-c", code], cwd=str(BACKEND), capture_output=True,
            text=True, timeout=120,
            env={"PATH": "/usr/bin:/bin", "APP_MODE": "production",
                 "DATABASE_URL": DEAD_URL, "PYTHONPATH": str(BACKEND)})
        self.assertEqual(
            proc.returncode, 0,
            f"importing the server with an unreachable store still kills the "
            f"process -- this is the production outage:\n{proc.stderr[-900:]}")
        self.assertIn("DEGRADED", proc.stdout,
                      "the store should be reported unavailable, not healthy")

    # -- 2 & 3 & 4: bound, and honest about why ------------------------------
    def test_the_port_binds_and_every_route_is_honest_about_the_store(self):
        self._break_store()
        port = self._serve()          # binding at all is assertion 2

        for path in ("/api/health", "/api/readiness"):
            status, body = self._get(port, path)
            self.assertEqual(status, 503, f"{path} answered {status}")
            self.assertEqual(body.get("status"), "unavailable", body)
            # THE PROPERTY IS "a cause is reported", NOT which cause.
            # An earlier version asserted the substring "resolve host" and
            # passed only where psycopg happens to be installed: CI's
            # Memory/SQLite job has no driver, so the same unreachable URL fails
            # with ModuleNotFoundError instead, and the assertion failed on a
            # server that was behaving perfectly. Pinning the incidental text of
            # one environment's failure is not the contract — not hiding the
            # cause is.
            # THE CONTRACT CHANGED DELIBERATELY (#203 review), and this
            # assertion changed with it rather than being deleted.
            #
            # It used to require the public reason to name the underlying
            # exception. That was wrong on a PUBLIC endpoint: the exception text
            # can carry an internal hostname, a username, a database name or
            # path, or a DSN — measured on this branch, it published
            # "failed to resolve host 'db-internal.example'".
            #
            # What an operator actually needs is preserved, and is what is
            # asserted now: a STABLE CATEGORY they can alert on, plus a
            # REFERENCE that correlates to the full cause in the server log.
            # Both must be present — a category with no reference would leave
            # them unable to find the diagnostic, which is how sanitizing turns
            # into hiding.
            store = body.get("store") or {}
            self.assertEqual(
                store.get("reason"), "store_unreachable",
                f"{path} should report a stable category: {body}")
            self.assertTrue(
                (store.get("reference") or "").strip(),
                f"{path} gave no reference, so the full cause in the log cannot "
                f"be correlated to this response: {body}")

        for path in ("/api/demo/overview", "/api/auth/roles"):
            status, body = self._get(port, path)
            self.assertEqual(status, 503, f"{path} answered {status}")
            self.assertEqual(body.get("error", {}).get("code"),
                             "store_unavailable", body)

    # -- 5: recovery without a redeploy --------------------------------------
    def test_the_instance_recovers_when_the_store_returns(self):
        self._break_store()
        port = self._serve()
        self.assertEqual(self._get(port, "/api/auth/roles")[0], 503)

        import os
        original = os.environ.get("DATABASE_URL")
        self.addCleanup(
            lambda: os.environ.__setitem__("DATABASE_URL", original)
            if original is not None else os.environ.pop("DATABASE_URL", None))
        os.environ["DATABASE_URL"] = "sqlite:///" + tempfile.mktemp(".db")
        # THE CONTRACT IS "next PERMITTED attempt", not "next request" (#203
        # review). The 503 above consumed an attempt and opened a backoff
        # window, which is the entire point of blocker 1: without it every
        # request drives its own connect and an outage becomes a connection
        # storm. Clearing the deadline here stands in for the clock advancing
        # past that window -- a real instance recovers within RETRY_BACKOFF_MAX
        # without anyone touching it.
        srv.STATE._next_retry_at = 0.0

        self.assertEqual(
            self._get(port, "/api/auth/roles")[0], 200,
            "the instance did not recover on the next permitted attempt after "
            "the store returned -- it would need a manual redeploy, which is "
            "the whole cost this fix exists to remove")
        self.assertTrue(srv.STATE.store_available)
        self.assertIsNone(srv.STATE.boot_error)

    # -- 6: the control ------------------------------------------------------
    def test_control_a_healthy_store_is_completely_unaffected(self):
        """Anti-vacuity: without this, cases 2-4 would pass on a server that
        answered 503 to everything, always."""
        port = self._serve()
        self.assertTrue(srv.STATE.store_available)
        status, _ = self._get(port, "/api/auth/roles")
        self.assertEqual(status, 200)
        status, body = self._get(port, "/api/health")
        self.assertEqual(status, 200, body)
        self.assertNotEqual(body.get("status"), "unavailable", body)


if __name__ == "__main__":
    unittest.main()


class DegradedBootHardeningTest(BootWithoutStoreTest):
    """The four review defects, each with its own case (#203 review).

    Kept as a SUBCLASS so every case above still runs against the same server:
    a correction that quietly broke the original contract would show up here.
    """

    # -- D1: single-flight, atomic publish -----------------------------------
    def test_a_burst_of_first_requests_causes_exactly_one_reconnect(self):
        """32 threads released together must build ONE store, not 32.

        Barrier-controlled, not sleep-controlled: every client waits on the
        same `threading.Barrier`, so they are genuinely simultaneous rather
        than merely close together. Without the reconnect lock each thread runs
        `create_store()` independently -- a connection storm at exactly the
        moment the database has just come back, and every loser leaked because
        each captured the old value before any published.
        """
        import os, threading as th
        self._break_store()
        port = self._serve()
        self.assertEqual(self._get(port, "/api/auth/roles")[0], 503)

        builds = []
        build_lock = th.Lock()
        real_create = srv.create_store

        def counting_create(*a, **kw):
            store = real_create(*a, **kw)
            with build_lock:
                builds.append(store)
            return store

        srv.create_store = counting_create
        self.addCleanup(setattr, srv, "create_store", real_create)

        os.environ["DATABASE_URL"] = "sqlite:///" + tempfile.mktemp(".db")
        srv.STATE._next_retry_at = 0.0    # stand in for the backoff elapsing

        n = 32
        barrier = th.Barrier(n)
        results, rlock = [], th.Lock()

        def hammer():
            barrier.wait()
            code = self._get(port, "/api/auth/roles")[0]
            with rlock:
                results.append(code)

        threads = [th.Thread(target=hammer, daemon=True) for _ in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(30)

        self.assertEqual(
            len(builds), 1,
            f"{len(builds)} stores were built for one recovery -- the reconnect "
            f"is not single-flight, so a burst of first requests storms the "
            f"database and leaks every loser")
        self.assertTrue(srv.STATE.store_available)
        self.assertEqual(
            sorted(set(results)), [200],
            f"not every request converged on the recovered facade: {sorted(set(results))}")

    def test_no_request_can_observe_a_facade_before_bootstrap_finishes(self):
        """`store_available` must never be True while the facade is half-built.

        `reset()`'s production branch assigns `self.api` BEFORE
        `bootstrap_admin_from_env` runs, which is exactly the window this
        asserts is closed. The probe runs INSIDE bootstrap: if recovery
        published early, the flag is already True while we are still in here.
        """
        import os
        self._break_store()
        port = self._serve()
        seen = {}
        real_bootstrap = srv.bootstrap_admin_from_env

        def probing_bootstrap(api, env):
            seen["available_during_bootstrap"] = srv.STATE.store_available
            return real_bootstrap(api, env)

        srv.bootstrap_admin_from_env = probing_bootstrap
        self.addCleanup(setattr, srv, "bootstrap_admin_from_env", real_bootstrap)
        os.environ["DATABASE_URL"] = "sqlite:///" + tempfile.mktemp(".db")
        srv.STATE._next_retry_at = 0.0    # stand in for the backoff elapsing

        self.assertEqual(self._get(port, "/api/auth/roles")[0], 200)
        if "available_during_bootstrap" in seen:
            self.assertFalse(
                seen["available_during_bootstrap"],
                "the facade was PUBLISHED while its bootstrap was still "
                "running -- a concurrent request could dispatch through a "
                "half-initialized facade")

    # -- D2: recovery starts the worker --------------------------------------
    def test_recovery_starts_the_delivery_loop_exactly_once(self):
        """A recovered instance must not silently leave delivery stopped.

        The first version of this fix recovered HTTP routes only, so queued
        mail and push stayed stopped until someone restarted the process by
        hand -- while the PR text claimed full recovery.
        """
        import os, threading as th
        self._break_store()
        port = self._serve()
        srv.STATE._runtime_started = False
        starts = []
        real_from_env = srv.delivery_loop_from_env

        def counting_from_env(delivery, env):
            loop = real_from_env(delivery, env)
            starts.append(loop)
            return loop

        srv.delivery_loop_from_env = counting_from_env
        self.addCleanup(setattr, srv, "delivery_loop_from_env", real_from_env)
        os.environ["DATABASE_URL"] = "sqlite:///" + tempfile.mktemp(".db")
        srv.STATE._next_retry_at = 0.0    # stand in for the backoff elapsing

        barrier = th.Barrier(8)

        def hammer():
            barrier.wait()
            self._get(port, "/api/auth/roles")

        threads = [th.Thread(target=hammer, daemon=True) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(30)

        self.assertEqual(
            len(starts), 1,
            f"the runtime was initialized {len(starts)} times under concurrent "
            f"first requests -- it must happen exactly once")
        self.addCleanup(lambda: [l.stop() for l in starts
                                 if hasattr(l, "stop")])

    # -- D3: no secrets on the public surface --------------------------------
    def test_public_endpoints_never_echo_the_store_failure_text(self):
        """health/readiness/503 are public: a DSN must not travel on them."""
        SENTINELS = ("hunter2", "svcuser", "db-internal.example",
                     "prod_hockey", "/var/secret/path")
        self._break_store()
        srv.STATE.boot_error = (
            "OperationalError: connection to "
            "postgresql://svcuser:hunter2@db-internal.example:5432/prod_hockey"
            " failed; tried /var/secret/path")
        srv.STATE.boot_error_ref = "abc12345"
        port = self._serve()

        for path in ("/api/health", "/api/readiness", "/api/auth/roles",
                     "/api/demo/overview"):
            status, body = self._get(port, path)
            self.assertEqual(status, 503, path)
            blob = json.dumps(body)
            for sentinel in SENTINELS:
                self.assertNotIn(
                    sentinel, blob,
                    f"{path} leaked {sentinel!r} on a PUBLIC response: {blob}")
            # A reference must exist, but NOT the one this test set: the
            # guard retries first, that retry fails against the dead URL, and
            # recording that failure mints a fresh reference. Asserting the
            # hand-set value would be asserting that the retry did not happen.
            ref = (body.get("store") or body.get("error", {})
                   .get("details", {})).get("reference") or ""
            self.assertTrue(
                ref.strip(),
                f"{path} gave no reference to correlate with the server log, "
                f"so sanitization removed the operator's only diagnostic: {blob}")

    # -- D4: degraded HEAD is bodyless ---------------------------------------
    def test_degraded_head_mirrors_the_503_with_zero_bytes(self):
        """RAW SOCKET, deliberately.

        `http.client` knows a HEAD response carries no body and never reads
        one, so `response.read()` returns b"" whether or not the server wrote
        bytes. An earlier version of this test used it and passed even with the
        defect restored -- it was measuring the client's politeness, not the
        server's behaviour. Reading the socket directly is the only way to see
        what was actually sent.
        """
        import socket
        self._break_store()
        port = self._serve()

        sock = socket.create_connection(("127.0.0.1", port), timeout=10)
        self.addCleanup(sock.close)
        sock.sendall(b"HEAD /api/auth/roles HTTP/1.1\r\n"
                     b"Host: 127.0.0.1\r\nConnection: close\r\n\r\n")
        chunks = []
        while True:
            piece = sock.recv(65536)
            if not piece:
                break
            chunks.append(piece)
        raw = b"".join(chunks)

        self.assertIn(b" 503 ", raw.split(b"\r\n", 1)[0] + b" ",
                      f"degraded HEAD did not answer 503: {raw[:120]!r}")
        head, _, body = raw.partition(b"\r\n\r\n")
        self.assertEqual(
            body, b"",
            f"degraded HEAD wrote {len(body)} body bytes on the wire -- HEAD "
            f"must mirror the status and headers with no payload. Sent: "
            f"{body[:200]!r}")
        self.assertIn(b"Content-Length:", head,
                      "the mirrored headers should still be present")


class DegradedBootSecondRoundTest(BootWithoutStoreTest):
    """The four blockers from the second review round (#203)."""

    # -- B1: health never blocks; attempts are bounded by a clock ------------
    def test_health_answers_promptly_and_attempts_are_bounded(self):
        """No sleeps: a fake clock drives the schedule, a barrier drives load.

        Every public request used to force its own DNS/connect, so routine
        probing became a connection storm and a slow TCP failure made the
        health endpoint hang for the connect timeout -- the one endpoint that
        must always answer.
        """
        import threading as th
        self._break_store()
        now = {"t": 1000.0}
        srv.STATE._clock = lambda: now["t"]
        attempts = []
        real_create = srv.create_store

        def counting_create(*a, **kw):
            attempts.append(1)
            return real_create(*a, **kw)

        srv.create_store = counting_create
        self.addCleanup(setattr, srv, "create_store", real_create)
        port = self._serve()

        n = 16
        barrier = th.Barrier(n)
        codes, lock = [], th.Lock()

        def hit(path):
            barrier.wait()
            code = self._get(port, path)[0]
            with lock:
                codes.append(code)

        paths = ["/api/health", "/api/readiness"] * 4 + ["/api/auth/roles"] * 8
        threads = [th.Thread(target=hit, args=(p,), daemon=True) for p in paths]
        for t in threads:
            t.start()
        for t in threads:
            t.join(30)

        self.assertEqual(sorted(set(codes)), [503],
                         f"every request should answer 503 promptly: {codes}")
        # NOTE: this bounds the SCHEDULE, not the exemption ordering -- the
        # backoff caps attempts either way. The ordering is proven separately by
        # test_health_never_blocks_on_a_slow_connector.
        self.assertLessEqual(
            len(attempts), 1,
            f"{len(attempts)} connect attempts for {n} concurrent requests -- "
            f"the retry is not bounded, so an outage becomes a connection "
            f"storm against a recovering database")
        first_ref = srv.STATE.boot_error_ref
        self.assertTrue(first_ref, "no incident reference was recorded")

        # inside the backoff window: still no new attempt, SAME reference
        before = len(attempts)
        self.assertEqual(self._get(port, "/api/auth/roles")[0], 503)
        self.assertEqual(len(attempts), before,
                         "a request inside the backoff window still attempted")
        self.assertEqual(srv.STATE.boot_error_ref, first_ref,
                         "the incident reference changed, so it cannot "
                         "correlate a log line to a response")

        # advance the clock past the window: exactly one further attempt
        now["t"] += 120.0
        self.assertEqual(self._get(port, "/api/auth/roles")[0], 503)
        self.assertEqual(
            len(attempts), before + 1,
            "advancing past the backoff window did not permit a retry -- the "
            "instance would never recover")

    def test_health_never_blocks_on_a_slow_connector(self):
        """THE ordering property, and the one a bounded-attempt count cannot see.

        An earlier version of this test asserted only "at most one connect
        attempt", which the backoff satisfies whether the exemption sits before
        or after `try_reconnect()` -- so it passed with the defect restored and
        proved nothing. What the ordering actually governs is whether health
        BLOCKS: with the exemption after the retry, a slow TCP failure makes the
        health endpoint hang for the connect timeout, which is exactly when a
        platform most needs an answer from it.

        No sleeps as proof: the connector waits on an Event this test controls,
        so "did health answer while the connector was still stuck" is a
        deterministic question.
        """
        import threading as th
        self._break_store()
        release = th.Event()
        entered = th.Event()
        real_create = srv.create_store

        def blocking_create(*a, **kw):
            entered.set()
            release.wait(20)          # held until this test says otherwise
            return real_create(*a, **kw)

        srv.create_store = blocking_create
        self.addCleanup(release.set)
        self.addCleanup(setattr, srv, "create_store", real_create)
        port = self._serve()

        # a data route enters the connector and is stuck there
        stuck = []
        t = th.Thread(target=lambda: stuck.append(
            self._get(port, "/api/auth/roles")[0]), daemon=True)
        t.start()
        self.assertTrue(entered.wait(10), "the connector was never reached")

        # ...and while it is stuck, health must still answer, promptly
        started = time.monotonic()
        status, body = self._get(port, "/api/health")
        elapsed = time.monotonic() - started
        self.assertEqual(status, 503, body)
        self.assertLess(
            elapsed, 5.0,
            f"/api/health took {elapsed:.1f}s while a connect was in flight -- "
            f"it is waiting behind the database instead of answering from "
            f"process state, so a slow TCP failure hangs the health check")
        self.assertFalse(release.is_set(), "the connector finished too early "
                                           "for this to have proven anything")
        release.set()
        t.join(20)

    # -- B2: the worker really runs, and a failed start stays retryable ------
    def test_an_enabled_worker_is_actually_running_after_recovery(self):
        import os
        self._break_store()
        os.environ["DELIVERY_WORKER_ENABLED"] = "1"
        self.addCleanup(os.environ.pop, "DELIVERY_WORKER_ENABLED", None)
        srv.STATE._runtime_started = False
        port = self._serve()
        os.environ["DATABASE_URL"] = "sqlite:///" + tempfile.mktemp(".db")
        srv.STATE._next_retry_at = 0.0    # stand in for the backoff elapsing

        self.assertEqual(self._get(port, "/api/auth/roles")[0], 200)
        loop = srv.STATE.api.delivery_loop
        self.addCleanup(lambda: getattr(loop, "stop", lambda: None)())
        self.assertTrue(
            loop.is_running(),
            "the delivery loop is not RUNNING after recovery -- the previous "
            "version of this test only proved a loop object was constructed, "
            "with the worker disabled by default")

    def test_a_failed_runtime_start_does_not_advertise_recovery(self):
        """A crash during runtime init must stay retryable, not stick."""
        import os
        self._break_store()
        srv.STATE._runtime_started = False
        calls = []
        real_from_env = srv.delivery_loop_from_env

        def exploding(delivery, env):
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("worker init blew up")
            return real_from_env(delivery, env)

        srv.delivery_loop_from_env = exploding
        self.addCleanup(setattr, srv, "delivery_loop_from_env", real_from_env)
        port = self._serve()
        os.environ["DATABASE_URL"] = "sqlite:///" + tempfile.mktemp(".db")

        self.assertEqual(
            self._get(port, "/api/auth/roles")[0], 503,
            "a failed runtime start was published as a successful recovery")
        self.assertFalse(srv.STATE.store_available)
        self.assertFalse(
            srv.STATE._runtime_started,
            "the runtime was marked started even though it raised, so no later "
            "attempt would ever retry it")

        srv.STATE._next_retry_at = 0.0     # allow the next controlled attempt
        self.assertEqual(
            self._get(port, "/api/auth/roles")[0], 200,
            "the next attempt could not recover -- the failure was sticky")

    # -- B3: credentials must not reach the LOG either ----------------------
    def test_no_credentials_reach_the_log(self):
        import io, contextlib
        SENTINELS = ("hunter2", "s3cr3t-token", "svcuser")
        self._break_store()
        buf = io.StringIO()

        class Boom(Exception):
            pass

        with contextlib.redirect_stderr(buf):
            srv.STATE._record_boot_error(Boom(
                "connection to postgresql://svcuser:hunter2@h:5432/db failed; "
                "password=hunter2 token=s3cr3t-token"))
        logged = buf.getvalue()

        for sentinel in SENTINELS:
            self.assertNotIn(
                sentinel, logged,
                f"{sentinel!r} was written to the LOG. Sanitizing only the "
                f"response moves the leak; #203 says credentials must never be "
                f"logged. Captured: {logged!r}")
        self.assertIn("Boom", logged,
                      f"the exception type was lost, so the log is no longer a "
                      f"useful diagnostic: {logged!r}")
        self.assertIn(srv.STATE.boot_error_ref or "\0", logged,
                      "the log line carries no reference to correlate with the "
                      "public response")

    # -- B4: degraded OPTIONS is bodyless -----------------------------------
    def test_degraded_options_is_bodyless_on_the_wire(self):
        import socket
        self._break_store()
        port = self._serve()
        sock = socket.create_connection(("127.0.0.1", port), timeout=10)
        self.addCleanup(sock.close)
        sock.sendall(b"OPTIONS /api/auth/roles HTTP/1.1\r\n"
                     b"Host: 127.0.0.1\r\nConnection: close\r\n\r\n")
        chunks = []
        while True:
            piece = sock.recv(65536)
            if not piece:
                break
            chunks.append(piece)
        raw = b"".join(chunks)
        head, _, body = raw.partition(b"\r\n\r\n")
        self.assertIn(b"503", head.split(b"\r\n")[0])
        self.assertEqual(
            body, b"",
            f"degraded OPTIONS wrote {len(body)} body bytes -- its contract is "
            f"'Always bodyless', outage or not. Sent: {body[:200]!r}")
