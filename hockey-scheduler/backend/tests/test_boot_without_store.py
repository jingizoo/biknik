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

        self.addCleanup(_restore)
        self.addCleanup(setattr, srv.STATE, "boot_error", original_error)
        self.addCleanup(setattr, srv.STATE, "api", original_api)
        os.environ["APP_MODE"] = "production"
        os.environ["DATABASE_URL"] = DEAD_URL
        srv.STATE.api = None
        srv.STATE.boot_error = "OperationalError: failed to resolve host"

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

        self.assertEqual(
            self._get(port, "/api/auth/roles")[0], 200,
            "the instance did not recover on the first request after the store "
            "returned -- it would need a manual redeploy, which is the whole "
            "cost this fix exists to remove")
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
