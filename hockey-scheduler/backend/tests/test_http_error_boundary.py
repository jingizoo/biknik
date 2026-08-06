"""Every request gets an ANSWER, even when the handler raises (#302).

`do_GET`/`do_POST` had no catch-all boundary. `api/service.py`'s ``catch()``
converts only ``DomainError``; anything else — a driver error the translator
does not recognise, a lost database connection, a plain bug — unwound out of
the handler, and ``ThreadingMixIn`` closed the socket in its ``finally`` with
**zero bytes written**. There is no 500 path in that shape: the client sees a
dropped connection, and a reverse proxy in front of it reports **502 Bad
Gateway**. That is the delivery mechanism behind the #302 report ("several ice
slots added successfully, then a later create returns 502").

The boundary is deliberately at ``handle_one_request``, the single point
through which ``BaseHTTPRequestHandler`` dispatches EVERY verb, so a future
``do_*`` cannot be added outside it — seven per-verb copies would drift, which
is the failure this file exists to prevent.

What it must NOT do:
  * swallow the traceback — a real bug still has to be diagnosable in the logs;
  * convert ``DomainError``s, which already have correct structured statuses;
  * write to a socket the client has already closed;
  * answer twice when the handler already sent a response before failing.
"""

import contextlib
import io
import json
import socket
import sys
import threading
import unittest
import urllib.error
import urllib.request
from http.client import RemoteDisconnected
from http.cookiejar import CookieJar

from helpers import BACKEND  # noqa: F401  (ensures sys.path is set up)

from hockey_scheduler.domain import ValidationError
from hockey_scheduler.web import server as web


DROPPED = object()   # the pre-#302 shape: no answer at all


class _ErrorBoundaryContract:
    """Shared body. Subclasses pick the server class under test."""

    @classmethod
    def setUpClass(cls):
        web.STATE.reset()
        cls.httpd = cls.make_server()
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever,
                                      daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.thread.join(timeout=5)
        cls.httpd.server_close()

    # -- helpers ---------------------------------------------------------
    def _client(self):
        opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(CookieJar()))
        self._req(opener, "POST", "/api/auth/login",
                  {"username": "admin", "password": "demo"})
        return opener

    def _rink(self, c):
        """A REAL rink, built through the real routes.

        Necessary, not incidental: the ice-slot route runs
        ``_reject_parent_outside_scope`` first, so a made-up ``rink_id`` 404s
        before the facade is ever called — the injected fault would never fire
        and the test would pass for the wrong reason.
        """
        if getattr(self.__class__, "_rink_id", None):
            return self.__class__._rink_id

        def post(path, body):
            status, resp = self._req(c, "POST", path, body)
            self.assertEqual(status, 200, f"{path} -> {resp!r}")
            return resp
        program = post("/api/setup/league", {"name": "EB Program"})
        self._req(c, "POST", "/api/context", {"program_id": program["id"]})
        season = post("/api/setup/season",
                      {"league_id": program["id"], "name": "EB Season"})
        self._req(c, "POST", "/api/context",
                  {"program_id": program["id"], "season_id": season["id"]})
        venue = post("/api/setup/venue",
                     {"name": "EB Arena", "league_id": program["id"]})
        self._req(c, "POST",
                  f"/api/v2/setup/seasons/{season['id']}/venue-access",
                  {"venue_id": venue["id"]})
        rink = post("/api/setup/rink",
                    {"venue_id": venue["id"], "name": "EB Rink"})
        self.__class__._rink_id = rink["id"]
        return rink["id"]

    def _raw_login(self):
        """Sign in and return the raw ``Cookie`` header value."""
        url = f"http://127.0.0.1:{self.port}/api/auth/login"
        data = json.dumps({"username": "admin", "password": "demo"}).encode()
        req = urllib.request.Request(
            url, data=data, method="POST",
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req) as resp:
            return resp.headers.get("Set-Cookie", "").split(";", 1)[0]

    def _raw_request(self, cookie, method, path):
        """Every byte the server puts on the wire, unparsed.

        A parsing client cannot see a second status line spliced behind the
        first — it reads one response and calls the rest body. Only raw bytes
        can distinguish "one reply" from "two".
        """
        s = socket.create_connection(("127.0.0.1", self.port), timeout=5)
        try:
            s.sendall(
                f"{method} {path} HTTP/1.1\r\nHost: 127.0.0.1\r\n"
                f"Cookie: {cookie}\r\nConnection: close\r\n\r\n".encode())
            chunks = []
            while True:
                try:
                    b = s.recv(65536)
                except (ConnectionResetError, socket.timeout):
                    break
                if not b:
                    break
                chunks.append(b)
            return b"".join(chunks)
        finally:
            s.close()

    def _slot_body(self, rink_id, hour=18):
        return {"rink_id": rink_id,
                "start_time": f"2026-11-02T{hour:02d}:00:00+00:00",
                "end_time": f"2026-11-02T{hour + 1:02d}:00:00+00:00"}

    def _req(self, opener, method, path, body=None):
        """``(status, payload)``, or ``(DROPPED, None)`` when the server closed
        the connection without writing a single byte — the defect's signature."""
        url = f"http://127.0.0.1:{self.port}{path}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with opener.open(req) as r:
                raw = r.read()
                return r.status, (json.loads(raw) if raw else None)
        except urllib.error.HTTPError as e:
            raw = e.read()
            return e.code, (json.loads(raw) if raw else None)
        except (RemoteDisconnected, ConnectionResetError):
            return DROPPED, None
        except urllib.error.URLError as e:
            if isinstance(e.reason, (RemoteDisconnected, ConnectionResetError)):
                return DROPPED, None
            raise

    @contextlib.contextmanager
    def _raising(self, name, exc):
        """Make one facade method raise a NON-domain error, capturing stderr so
        the assertion about logging is made on real captured output."""
        api = web.STATE.api
        original = getattr(api, name)

        def boom(*a, **kw):
            raise exc
        setattr(api, name, boom)
        buf = io.StringIO()
        real_stderr = sys.stderr
        sys.stderr = buf
        try:
            yield buf
        finally:
            sys.stderr = real_stderr
            setattr(api, name, original)

    # -- the contract ----------------------------------------------------
    def test_an_unhandled_post_error_is_answered_not_dropped(self):
        c = self._client()
        rink = self._rink(c)
        # ANTI-VACUITY: the route genuinely works and REACHES the facade, so a
        # later DROPPED/500 is caused by the injected fault and not by the
        # request being rejected upstream all along.
        ok_status, ok_body = self._req(
            c, "POST", "/api/setup/ice-slot", self._slot_body(rink, 18))
        self.assertEqual(ok_status, 200, ok_body)

        with self._raising("create_ice_slot", RuntimeError("boom")) as err:
            status, payload = self._req(
                c, "POST", "/api/setup/ice-slot", self._slot_body(rink, 20))

        self.assertIsNot(
            status, DROPPED,
            "the handler raised and the server answered with NOTHING — a "
            "reverse proxy renders this as 502 (#302)")
        self.assertEqual(status, 500)
        self.assertEqual(payload["error"]["code"], "internal_error")
        # The operator must not be handed internals to act on.
        self.assertNotIn("boom", json.dumps(payload))
        self.assertNotIn("Traceback", json.dumps(payload))
        # ...but a real bug must still be diagnosable from the logs.
        self.assertIn("RuntimeError", err.getvalue())
        self.assertIn("Traceback", err.getvalue())

    def test_an_unhandled_get_error_is_answered_not_dropped(self):
        c = self._client()
        ok_status, _ = self._req(c, "GET", "/api/setup/hierarchy")
        self.assertNotEqual(ok_status, DROPPED, "baseline GET already dropped")

        with self._raising("get_setup_hierarchy_v2", RuntimeError("boom")):
            status, payload = self._req(c, "GET", "/api/v2/setup/hierarchy")

        self.assertIsNot(
            status, DROPPED, "an unhandled GET error answered with nothing")
        self.assertEqual(status, 500)
        self.assertEqual(payload["error"]["code"], "internal_error")

    def test_the_process_survives_and_serves_the_next_request(self):
        """The boundary must answer, not restart. A handler that took the whole
        worker down would show up here as a second failure."""
        c = self._client()
        rink = self._rink(c)
        with self._raising("create_ice_slot", RuntimeError("boom")):
            self._req(c, "POST", "/api/setup/ice-slot",
                      self._slot_body(rink, 6))
        status, _ = self._req(c, "GET", "/api/setup/hierarchy")
        self.assertEqual(status, 200, "server did not survive the raise")

    def test_a_lost_database_connection_is_answered_too(self):
        """#302's matching trigger: the process holds ONE connection with no
        pool and no reconnect, so a managed-Postgres restart turns every
        store-touching route into a dropped socket. Simulated here by the
        driver error such a loss actually raises — the point is that an
        UNRECOGNISED driver error still produces an answer."""
        c = self._client()
        rink = self._rink(c)
        import sqlite3
        with self._raising(
                "create_ice_slot",
                sqlite3.ProgrammingError("Cannot operate on a closed database.")):
            status, payload = self._req(
                c, "POST", "/api/setup/ice-slot", self._slot_body(rink, 8))
        self.assertIsNot(status, DROPPED)
        self.assertEqual(status, 500)
        self.assertEqual(payload["error"]["code"], "internal_error")

    def test_domain_errors_are_untouched_by_the_boundary(self):
        """The boundary must not flatten the structured 4xx contract into 500.

        Asserted two ways: a real validation path, and a DomainError raised
        from inside the facade — the exact shape the boundary could swallow.
        """
        c = self._client()
        rink = self._rink(c)
        bad = self._slot_body(rink, 12)
        bad["end_time"] = bad["start_time"]     # end must be after start
        status, payload = self._req(c, "POST", "/api/setup/ice-slot", bad)
        self.assertIn(status, (400, 404), payload)
        self.assertNotEqual(payload["error"]["code"], "internal_error")

        with self._raising("create_ice_slot", ValidationError("nope")):
            status, payload = self._req(
                c, "POST", "/api/setup/ice-slot", self._slot_body(rink, 10))
        self.assertNotEqual(
            status, 500,
            "a DomainError raised past the facade was converted to 500")
        self.assertNotEqual(payload["error"]["code"], "internal_error")

    def test_a_failure_after_the_response_started_does_not_answer_twice(self):
        """The boundary must not append a SECOND status line to a response the
        handler already committed.

        Forced by making ``_security_headers`` raise: ``_send_json`` calls
        ``send_response`` first, so a status line is already buffered by then.
        A boundary that answered anyway would call ``send_response`` again on
        the same ``_headers_buffer``, and both status lines would flush — one
        HTTP message containing two.

        Asserted on RAW BYTES on purpose. ``urllib`` parses the first status
        line and treats the rest as body, so it reports a perfectly ordinary
        200 either way — an earlier version of this test used it and could not
        tell the two apart, passing while the branch was unguarded.
        """
        cookie = self._raw_login()
        original = web.Handler._security_headers

        # Raises EXACTLY ONCE. A fault that fired on every call would also
        # break the boundary's own recovery write, so no second status line
        # could ever be emitted and this test would pass against a boundary
        # with the double-answer guard removed — which is precisely what an
        # earlier version of it did.
        fired = []

        def boom(self_):
            if not fired:
                fired.append(True)
                raise RuntimeError("after the status line")
            return original(self_)
        web.Handler._security_headers = boom
        buf, real = io.StringIO(), sys.stderr
        sys.stderr = buf
        try:
            raw = self._raw_request(cookie, "GET", "/api/setup/hierarchy")
        finally:
            sys.stderr = real
            web.Handler._security_headers = original

        starts = raw.count(b"HTTP/1.")
        self.assertLessEqual(
            starts, 1,
            f"{starts} status lines in ONE response — the boundary answered a "
            f"request that had already started replying: {raw[:200]!r}")
        # The failure is still reported, never silently dropped.
        self.assertIn("RuntimeError", buf.getvalue())
        # ...and the server is still serving.
        c = self._client()
        again, _ = self._req(c, "GET", "/api/setup/hierarchy")
        self.assertEqual(again, 200)

    def test_the_raw_probe_sees_a_normal_response(self):
        """Anti-vacuity for the raw probe above: on a healthy request it really
        does observe exactly one status line, so ``<= 1`` is a live assertion
        and not something an empty read satisfies for free."""
        cookie = self._raw_login()
        raw = self._raw_request(cookie, "GET", "/api/setup/hierarchy")
        self.assertEqual(raw.count(b"HTTP/1."), 1, raw[:200])
        self.assertIn(b"200", raw.split(b"\r\n", 1)[0])

    def test_an_unknown_route_still_404s(self):
        """Control: the boundary must not turn ordinary routing into 500."""
        c = self._client()
        status, _ = self._req(c, "GET", "/api/definitely-not-a-route")
        self.assertEqual(status, 404)


class ThreadingServerErrorBoundaryTest(_ErrorBoundaryContract,
                                       unittest.TestCase):
    @classmethod
    def make_server(cls):
        from http.server import ThreadingHTTPServer
        return ThreadingHTTPServer(("127.0.0.1", 0), web.Handler)


class ProductionServerErrorBoundaryTest(_ErrorBoundaryContract,
                                        unittest.TestCase):
    """The class `serve()` actually uses, so the contract is pinned on the
    server that runs in production rather than only on a stand-in."""

    @classmethod
    def make_server(cls):
        return web.Server(("127.0.0.1", 0), web.Handler)


if __name__ == "__main__":
    unittest.main()
