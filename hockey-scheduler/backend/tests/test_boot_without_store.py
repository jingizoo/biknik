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
  4. data routes answer 503 ``store_unavailable``; HEAD sends zero bytes while
     mirroring GET's representation metadata, and OPTIONS is truly zero-length;
  5. credentials reach neither the response NOR the log;
  6. a degraded instance shuts down cleanly;
  7. none of this changes anything when the store is healthy.
"""

import contextlib
import http.client
import io
import itertools
import json
import os
import socket
import string
import subprocess
import sys
import threading
import unittest
import urllib.error
import urllib.request
from unittest import mock

from helpers import BACKEND  # noqa: F401  (ensures sys.path is set up)

from hockey_scheduler.store import StoreConnectionError
from hockey_scheduler.store import db as db_module
from hockey_scheduler.web import server as srv

DEAD_URL = "postgresql://u@dpg-doesnotexist-a/db"


class BootWithoutStoreTest(unittest.TestCase):
    maxDiff = None

    def _start_production_server(self, *, protocol_version=None):
        """Run the real ``serve()`` lifecycle and return ``(port, stop)``.

        No test in this file may hand-build an HTTP server: the production
        outage was in setup *around* ``serve_forever``, so bypassing ``serve``
        makes the most important assertions vacuous.  The subclass changes
        only observability; ``super().__init__`` performs the real kernel bind
        and ``serve()`` still owns loop setup, serving, and loop teardown. The
        helper closes the captured listening socket only after ``serve`` has
        returned, because the production function itself does not own that
        final ``server_close()`` call.
        """
        case = self
        instances = []
        errors = []
        ready = threading.Event()

        class CapturingServer(srv.Server):
            def __init__(inner, *args, **kwargs):
                super().__init__(*args, **kwargs)
                instances.append(inner)
                ready.set()

        def run():
            try:
                with contextlib.ExitStack() as stack:
                    stack.enter_context(mock.patch.object(
                        srv, "Server", CapturingServer))
                    stack.enter_context(mock.patch.object(
                        srv, "epoch_secret", return_value=b"test"))
                    stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
                    stack.enter_context(contextlib.redirect_stderr(io.StringIO()))
                    if protocol_version is not None:
                        stack.enter_context(mock.patch.object(
                            srv.Handler, "protocol_version", protocol_version))
                    srv.serve("127.0.0.1", 0)
            except BaseException as exc:  # surfaced on the test thread below
                errors.append(exc)
                ready.set()

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        self.assertTrue(ready.wait(10), "serve() never reached the bind")
        if errors:
            raise errors[0]
        self.assertEqual(len(instances), 1)
        httpd = instances[0]
        self.assertGreater(httpd.server_address[1], 0)
        stopped = False

        def stop():
            nonlocal stopped
            if stopped:
                return
            stopped = True
            httpd.shutdown()
            thread.join(10)
            httpd.server_close()
            case.assertFalse(thread.is_alive(), "serve() did not shut down")
            if errors:
                raise errors[0]

        self.addCleanup(stop)
        return httpd.server_address[1], stop

    def _serve(self):
        port, _stop = self._start_production_server()
        return port

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
        try:
            import psycopg  # noqa: F401  (the child needs the real driver)
        except ImportError:
            self.skipTest("the PostgreSQL CI job carries the psycopg driver")
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

    def test_only_a_real_postgres_connectivity_error_enters_degraded_mode(self):
        with mock.patch.dict(os.environ, {
                "APP_MODE": "production", "DATABASE_URL": DEAD_URL}), \
                mock.patch.object(
                    srv.DemoState, "reset",
                    side_effect=StoreConnectionError(
                        "failed to resolve host 'db.invalid'")), \
                contextlib.redirect_stderr(io.StringIO()):
            state = srv.DemoState()
        self.assertFalse(state.store_available)
        self.assertIn("StoreConnectionError", state.boot_error)

    def test_connect_boundary_translates_only_positive_availability_signals(self):
        try:
            import psycopg
        except ImportError:
            self.skipTest("the PostgreSQL CI job carries the psycopg driver")

        for message in (
                "failed to resolve host 'db.invalid': Name or service not known",
                'connection to server at "127.0.0.1", port 5432 failed: '
                'Connection refused',
                'connection to server at "db", port 5432 failed: timeout '
                'expired'):
            unavailable = psycopg.OperationalError(message)
            with self.subTest(message=message), \
                    mock.patch.object(
                        psycopg, "connect", side_effect=unavailable), \
                    self.assertRaises(StoreConnectionError) as caught:
                db_module.connect(DEAD_URL)
            self.assertIs(caught.exception.__cause__, unavailable)

        # Psycopg/libpq gives these configuration errors the SAME broad class
        # and no SQLSTATE.  They must fail boot with their real category rather
        # than being mislabeled as an unreachable database.
        for message in (
                'connection is bad: invalid sslmode value: "bogus"',
                'connection is bad: invalid integer value "no" for '
                'connection option "port"',
                'connection failed: connection to server at "db", port 5432 '
                'failed: root certificate file "/missing/root.crt" does not '
                'exist'):
            configured_wrong = psycopg.OperationalError(message)
            with self.subTest(message=message), \
                    mock.patch.object(
                        psycopg, "connect", side_effect=configured_wrong), \
                    self.assertRaises(psycopg.OperationalError) as caught:
                db_module.connect(DEAD_URL)
            self.assertIs(caught.exception, configured_wrong)

        for configured_wrong in (
                psycopg.errors.InvalidPassword("bad password"),
                psycopg.errors.SerializationFailure("bad transaction")):
            with self.subTest(error=type(configured_wrong).__name__), \
                    mock.patch.object(
                        psycopg, "connect", side_effect=configured_wrong), \
                    self.assertRaises(type(configured_wrong)) as caught:
                db_module.connect(DEAD_URL)
            self.assertIs(caught.exception, configured_wrong)

    def test_real_libpq_configuration_errors_fail_fast(self):
        try:
            import psycopg
        except ImportError:
            self.skipTest("the PostgreSQL CI job carries the psycopg driver")

        urls = (
            "postgresql://u:p@127.0.0.1:1/db?sslmode=bogus",
            "postgresql://u:p@127.0.0.1:notaport/db",
        )
        for url in urls:
            with self.subTest(url=url), \
                    self.assertRaises(psycopg.OperationalError) as caught:
                db_module.connect(url)
            self.assertNotIsInstance(caught.exception, StoreConnectionError)

    def test_programming_and_configuration_errors_still_fail_boot(self):
        for error in (ValueError("bad bootstrap"),
                      RuntimeError("bad migration"),
                      ModuleNotFoundError("missing driver")):
            with self.subTest(error=type(error).__name__), \
                    mock.patch.object(srv.DemoState, "reset", side_effect=error), \
                    self.assertRaises(type(error)):
                srv.DemoState()

    def test_every_failed_prepublication_stage_closes_the_candidate_store(self):
        for failed_stage in (
                "make_api", "bootstrap", "rate_limiter", "login_throttle"):
            with self.subTest(failed_stage=failed_stage):
                store = mock.Mock()
                api = mock.Mock()
                state = object.__new__(srv.DemoState)
                failure = RuntimeError(f"{failed_stage} failed")
                with contextlib.ExitStack() as stack:
                    stack.enter_context(mock.patch.dict(
                        os.environ, {"APP_MODE": "production"}))
                    stack.enter_context(mock.patch.object(
                        srv, "create_store", return_value=store))
                    stages = {
                        "make_api": stack.enter_context(mock.patch.object(
                            srv.DemoState, "_make_api", return_value=api)),
                        "bootstrap": stack.enter_context(mock.patch.object(
                            srv, "bootstrap_admin_from_env")),
                        "rate_limiter": stack.enter_context(mock.patch.object(
                            srv.RATE_LIMITER, "reset")),
                        "login_throttle": stack.enter_context(mock.patch.object(
                            srv.LOGIN_THROTTLE, "reset")),
                    }
                    stages[failed_stage].side_effect = failure
                    with self.assertRaises(RuntimeError) as caught:
                        state.reset(seed=False)
                self.assertIs(caught.exception, failure)
                store.close.assert_called_once_with()
                self.assertFalse(hasattr(state, "api"))

    def test_failed_migration_closes_the_constructor_owned_connection(self):
        from hockey_scheduler.store import sql_store as sql_store_module

        connection = mock.Mock()
        dialect = mock.Mock(paramstyle="qmark")
        with mock.patch.object(
                sql_store_module, "connect",
                return_value=(connection, dialect, None)), \
                mock.patch.object(
                    sql_store_module, "migrate",
                    side_effect=RuntimeError("migration failed")), \
                self.assertRaisesRegex(RuntimeError, "migration failed"):
            sql_store_module.SqlStore("sqlite://")
        connection.close.assert_called_once_with()

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

    def test_real_serve_reaches_an_actual_bind_and_tears_down_degraded(self):
        """Exercise the production entrypoint, not a hand-built test server.

        ``serve()`` owns both outage-critical guards: it must skip delivery-loop
        setup before constructing ``Server``, then skip loop teardown after the
        live server shuts down.  The capturing subclass retains the real
        ``Server.__init__`` socket bind and the real ``serve_forever`` loop; the
        test probes health over that socket, stops it, and surfaces any exception
        raised on either side of the bind.
        """
        self._break_store()
        port, stop = self._start_production_server()
        status, body = self._get(port, "/api/health")
        self.assertEqual(status, 503)
        self.assertEqual(body.get("store", {}).get("reason"),
                         "store_unreachable")
        stop()

    # -- 4, the bodyless verbs ------------------------------------------------
    def test_head_mirrors_get_metadata_and_options_is_truly_zero_length(self):
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
        # Production currently advertises HTTP/1.0, whose default close makes
        # HTTPConnection transparently reconnect and turns a framing check
        # vacuous.  Run the SAME production Handler/serve path in its supported
        # HTTP/1.1 mode, then assert socket identity so reuse is proved rather
        # than inferred from a second successful request.
        port, _stop = self._start_production_server(protocol_version="HTTP/1.1")

        get_conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        self.addCleanup(get_conn.close)
        get_conn.request("GET", "/api/auth/roles")
        get_response = get_conn.getresponse()
        get_payload = get_response.read()
        self.assertEqual(get_response.status, 503)
        self.assertTrue(json.loads(get_payload))

        head_conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        self.addCleanup(head_conn.close)
        head_conn.request("HEAD", "/api/auth/roles")
        head = head_conn.getresponse()
        self.assertEqual(head.status, 503)
        self.assertEqual(head.version, 11)
        self.assertFalse(head.will_close)
        self.assertEqual(head.getheader("Content-Type"),
                         get_response.getheader("Content-Type"))
        self.assertEqual(head.getheader("Content-Length"),
                         get_response.getheader("Content-Length"))
        self.assertGreater(int(head.getheader("Content-Length")), 0)
        self.assertEqual(head.read(), b"")
        head_socket = head_conn.sock
        self.assertIsNotNone(head_socket)

        # HEAD's declared GET length must not poison a persistent connection.
        head_conn.request("GET", "/api/health")
        after_head = head_conn.getresponse()
        self.assertEqual(after_head.status, 503)
        self.assertTrue(json.loads(after_head.read()))
        self.assertIs(head_conn.sock, head_socket)

        options_conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        self.addCleanup(options_conn.close)
        options_conn.request("OPTIONS", "/api/auth/roles")
        options = options_conn.getresponse()
        self.assertEqual(options.status, 503)
        self.assertEqual(options.version, 11)
        self.assertFalse(options.will_close)
        self.assertEqual(options.getheader("Content-Length"), "0")
        self.assertEqual(options.read(), b"")
        options_socket = options_conn.sock
        self.assertIsNotNone(options_socket)

        # OPTIONS is a genuine zero-length response and must also frame reuse.
        options_conn.request("GET", "/api/health")
        after_options = options_conn.getresponse()
        self.assertEqual(after_options.status, 503)
        self.assertTrue(json.loads(after_options.read()))
        self.assertIs(options_conn.sock, options_socket)

    def test_every_concrete_http_verb_is_guarded_while_degraded(self):
        """Derive the axis from Handler's MRO so a new ``do_*`` cannot hide."""
        self._break_store()
        port = self._serve()
        concrete_methods = sorted({
            name[3:]
            for handler_type in srv.Handler.__mro__
            for name in handler_type.__dict__
            if name.startswith("do_") and len(name) > 3
        })
        self.assertGreater(len(concrete_methods), 0)

        for method in concrete_methods:
            with self.subTest(method=method):
                conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
                self.addCleanup(conn.close)
                conn.request(method, "/api/auth/roles")
                response = conn.getresponse()
                response.read()
                self.assertEqual(response.status, 503)

    def test_every_synthesized_http_token_is_guarded_while_degraded(self):
        """The #438 dynamic bridge must not bypass the degraded-store guard."""
        self._break_store()
        token_chars = (
            string.ascii_letters + string.digits + "!#$%&'*+-.^_`|~"
        )
        widths = (1, 2)
        concrete_methods = {
            name[3:]
            for handler_type in srv.Handler.__mro__
            for name in handler_type.__dict__
            if name.startswith("do_")
        }
        expected = sum(len(token_chars) ** width for width in widths) - sum(
            1
            for method in concrete_methods
            if len(method) in widths
            and all(char in token_chars for char in method)
        )

        handler = object.__new__(srv.Handler)
        handler.path = "/api/auth/roles"
        statuses = []
        handler._send_json = lambda _body, status=200, **_kw: statuses.append(status)
        handler._send_status = lambda status, *_a, **_kw: statuses.append(status)
        checked = 0

        for width in widths:
            for chars in itertools.product(token_chars, repeat=width):
                method = "".join(chars)
                if method in concrete_methods:
                    continue
                with self.subTest(method=method):
                    handler.command = method
                    statuses.clear()
                    getattr(handler, f"do_{method}")()
                    self.assertEqual(statuses, [503])
                    checked += 1

        self.assertEqual(checked, expected)

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

    def test_redaction_consumes_quoted_values_and_driver_authority_leaks(self):
        self._break_store()
        database_url = (
            "postgresql://svc:prefix@TAIL@dpg-private-a:5432/db")
        os.environ["DATABASE_URL"] = database_url
        try:
            from psycopg.conninfo import conninfo_to_dict
        except ImportError:
            driver_host = "TAIL@dpg-private-a"
        else:
            driver_host = conninfo_to_dict(database_url)["host"]
            self.assertEqual(driver_host, "TAIL@dpg-private-a")
        buf = io.StringIO()

        class Boom(Exception):
            pass

        with contextlib.redirect_stderr(buf):
            srv.STATE._record_boot_error(Boom(
                f"failed to resolve host '{driver_host}'; "
                "password='hunter \\'two\\'' marker=kept; "
                'token="double \\"word\\" token" tail=kept'))

        logged = buf.getvalue()
        for secret in (
                driver_host, "hunter", 'double \\"word\\" token'):
            self.assertNotIn(secret, logged)
        self.assertIn("marker=kept", logged)
        self.assertIn("tail=kept", logged)

    def test_redaction_uses_the_real_driver_parse_for_malformed_ipv6_authority(self):
        try:
            import psycopg
            from psycopg.conninfo import conninfo_to_dict
        except ImportError:
            self.skipTest("the PostgreSQL CI job carries the psycopg driver")

        database_url = (
            "postgresql://svc:prefix@TAIL@[::1]:1/db?connect_timeout=1")
        driver_host = conninfo_to_dict(database_url)["host"]
        self.assertEqual(driver_host, "TAIL@[")
        try:
            psycopg.connect(database_url)
        except psycopg.OperationalError as exc:
            raw = str(exc)
        else:  # pragma: no cover - malformed authority cannot be a real server
            self.fail("malformed authority unexpectedly connected")
        self.assertIn(driver_host, raw, "the real-driver leak oracle was vacuous")
        redacted = srv.DemoState._redact(raw, database_url)
        self.assertNotIn(driver_host, redacted)
        self.assertNotIn("TAIL", redacted)

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
