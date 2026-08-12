"""The web process must BOOT and BIND when its database is unreachable (#203).

THE PRODUCTION FAILURE. A managed Postgres whose host stopped resolving took the
entire web service down::

    File ".../web/server.py", line 571, in <module>
        STATE = DemoState()
    psycopg.OperationalError: failed to resolve host 'dpg-...-a'
    ==> Exited with status 1
    ==> No open ports detected, continuing to scan...

``STATE = DemoState()`` runs at MODULE IMPORT, so the exception escaped
``from .server import serve`` and the process died before binding a port. The
platform then had nothing to talk to: no ``/api/health``, no ``/api/readiness``,
no logs from a running instance.

That defeated guards this server already had. ``/api/health`` answers 503
deliberately "or a bricked instance is never restarted", and ``/api/readiness``
exists to report deployment problems -- neither can answer if the module cannot
finish importing.

SCOPE, DELIBERATELY SMALL. This change records the failure and keeps serving. It
does NOT recover: no retry, no backoff, no worker restart. Self-healing was
split out after review, because each piece of it adds contract (a retry
schedule, single-flight semantics, worker lifecycle, a database-vs-worker
readiness distinction) that this change does not need in order to make an outage
diagnosable. A degraded instance here stays degraded until redeployed -- which
is what it did before, except now it says why.

THE CONTRACT, in the order it matters:

  1. importing with an unreachable store SUCCEEDS;
  2. the server BINDS its port anyway -- the difference between an instance you
     can diagnose and `No open ports detected`;
  3. ``/api/health`` and ``/api/readiness`` ANSWER, 503, with a stable category
     and a reference, never the raw exception;
  4. data routes answer 503 ``store_unavailable``, and BODYLESS verbs get a TRUE
     zero-length response rather than a declared-then-truncated one;
  5. credentials reach neither the response NOR the log;
  6. a degraded instance shuts down cleanly;
  7. none of this changes anything when the store is healthy.
"""

import contextlib
import http.client
import io
import json
import socket
import subprocess
import sys
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from helpers import BACKEND  # noqa: F401  (ensures sys.path is set up)

from hockey_scheduler.web import server as srv

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
        """Put STATE into the failed-boot condition and restore every field.

        Restoring only ``api`` leaves ``boot_error``/``boot_error_ref`` behind
        and makes later tests order-dependent.
        """
        import os
        saved = {k: getattr(srv.STATE, k, None)
                 for k in ("api", "boot_error", "boot_error_ref")}
        env = {k: os.environ.get(k) for k in ("DATABASE_URL", "APP_MODE")}

        def restore():
            for key, value in saved.items():
                setattr(srv.STATE, key, value)
            for key, value in env.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

        self.addCleanup(restore)
        os.environ["APP_MODE"] = "production"
        os.environ["DATABASE_URL"] = DEAD_URL
        srv.STATE.api = None
        srv.STATE.boot_error = "OperationalError: failed to resolve host"
        srv.STATE.boot_error_ref = "ref01234"

    # -- 1 --------------------------------------------------------------------
    def test_importing_the_server_survives_an_unreachable_store(self):
        """Out of process: the failure reproduced IS an import failure, so an
        in-process check could never observe it."""
        code = ("from hockey_scheduler.web.server import serve, STATE;"
                "print('AVAILABLE' if STATE.store_available else 'DEGRADED')")
        proc = subprocess.run(
            [sys.executable, "-c", code], cwd=str(BACKEND), capture_output=True,
            text=True, timeout=120,
            env={"PATH": "/usr/bin:/bin", "APP_MODE": "production",
                 "DATABASE_URL": DEAD_URL, "PYTHONPATH": str(BACKEND)})
        self.assertEqual(
            proc.returncode, 0,
            f"importing with an unreachable store still kills the process -- "
            f"this is the production outage:\n{proc.stderr[-800:]}")
        self.assertIn("DEGRADED", proc.stdout)

    # -- 2, 3, 4 --------------------------------------------------------------
    def test_the_port_binds_and_routes_are_honest_about_the_store(self):
        self._break_store()
        port = self._serve()                    # binding at all is assertion 2

        for path in ("/api/health", "/api/readiness"):
            status, body = self._get(port, path)
            self.assertEqual(status, 503, f"{path} answered {status}")
            store = body.get("store") or {}
            self.assertEqual(store.get("reason"), "store_unreachable", body)
            self.assertTrue(
                (store.get("reference") or "").strip(),
                f"{path} gave no reference, so the log cannot be correlated to "
                f"this response: {body}")

        for path in ("/api/demo/overview", "/api/auth/roles"):
            status, body = self._get(port, path)
            self.assertEqual(status, 503, f"{path} answered {status}")
            self.assertEqual(body.get("error", {}).get("code"),
                             "store_unavailable", body)

    # -- 4, the bodyless verbs ------------------------------------------------
    def test_bodyless_verbs_get_a_true_zero_length_503(self):
        """A PERSISTENT connection, deliberately.

        An earlier version forced ``Connection: close`` and asserted only that
        no bytes arrived -- which a response declaring ``Content-Length: N`` and
        then omitting N bytes also satisfies, so the test blessed a truncated
        response. Truncation is only observable when the connection is REUSED:
        a client that trusts the declared length waits for bytes that never
        come, or misframes whatever arrives next. So this sends a second request
        on the same socket and requires a well-formed answer to it.
        """
        self._break_store()
        port = self._serve()

        for verb in ("HEAD", "OPTIONS"):
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
            self.addCleanup(conn.close)
            conn.request(verb, "/api/auth/roles")
            first = conn.getresponse()
            first.read()
            self.assertEqual(first.status, 503, verb)
            self.assertEqual(
                first.getheader("Content-Length"), "0",
                f"degraded {verb} declared Content-Length "
                f"{first.getheader('Content-Length')!r} and then sent no body. "
                f"That framing is correct only for HEAD; on any other verb a "
                f"keep-alive client waits for bytes that never arrive")

            # the connection must still be usable and correctly framed
            conn.request("GET", "/api/health")
            second = conn.getresponse()
            payload = second.read()
            self.assertEqual(
                second.status, 503,
                f"the request after a degraded {verb} was misframed")
            self.assertTrue(json.loads(payload or b"{}"))

    # -- 5 --------------------------------------------------------------------
    def test_credentials_reach_neither_the_response_nor_the_log(self):
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
        port = self._serve()

        for sentinel in SENTINELS:
            self.assertNotIn(
                sentinel, logged,
                f"{sentinel!r} was written to the LOG -- sanitizing only the "
                f"response moves the leak; #203 forbids logging credentials. "
                f"Captured: {logged!r}")
        self.assertIn("Boom", logged, "the exception type was lost")

        for path in ("/api/health", "/api/auth/roles"):
            _, body = self._get(port, path)
            blob = json.dumps(body)
            for sentinel in SENTINELS:
                self.assertNotIn(sentinel, blob,
                                 f"{path} leaked {sentinel!r} publicly: {blob}")

    # -- 6 --------------------------------------------------------------------
    def test_a_degraded_instance_shuts_down_cleanly(self):
        """`serve()` permits `api is None`, so its teardown must too."""
        self._break_store()
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), srv.Handler)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            loop = getattr(getattr(srv.STATE, "api", None),
                           "delivery_loop", None)
            self.assertIsNone(loop, "the degraded instance owns no loop")
            if loop is not None:
                loop.stop()
        finally:
            httpd.shutdown()
            thread.join(5)
            httpd.server_close()
        self.assertFalse(thread.is_alive(), "the server thread did not stop")

    # -- 7, the control -------------------------------------------------------
    def test_control_a_healthy_store_is_completely_unaffected(self):
        """Without this, the 503 assertions would pass on a server that
        answered 503 to everything, always."""
        port = self._serve()
        self.assertTrue(srv.STATE.store_available)
        self.assertEqual(self._get(port, "/api/auth/roles")[0], 200)
        status, body = self._get(port, "/api/health")
        self.assertEqual(status, 200, body)
        self.assertNotEqual(body.get("status"), "unavailable", body)

        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        self.addCleanup(conn.close)
        conn.request("OPTIONS", "/api/auth/roles")
        response = conn.getresponse()
        response.read()
        self.assertIn(response.status, (200, 204),
                      "healthy OPTIONS changed behaviour")


if __name__ == "__main__":
    unittest.main()
