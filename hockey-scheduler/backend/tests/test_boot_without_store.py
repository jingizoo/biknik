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
            self.assertIn("resolve host",
                          json.dumps(body.get("store") or {}),
                          f"{path} did not name the real reason: {body}")

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
