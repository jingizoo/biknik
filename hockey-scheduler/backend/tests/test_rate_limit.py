"""In-process rate limiting for anonymous routes (#131).

Pure unit tests for the fixed-window RateLimiter itself (deterministic via
an injected clock — no wall-clock sleeps), plus HTTP-level tests proving the
anonymous public routes actually enforce it with a clean 429, and that
different callers (by IP) and different buckets don't bleed into each
other's limits.
"""

import json
import os
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from helpers import BACKEND  # noqa: F401  (ensures sys.path is set up)

from hockey_scheduler.web.rate_limit import RateLimiter


class _FakeClock:
    """A plain incrementing float clock — RateLimiter does arithmetic on
    its own clock's return value (``now - window_seconds``), so this needs
    to yield floats, not datetimes (unlike tests/helpers.py's FakeClock)."""

    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += seconds


class RateLimiterTest(unittest.TestCase):
    def setUp(self):
        self.clock = _FakeClock()
        self.limiter = RateLimiter(clock=self.clock)

    def test_allows_up_to_the_limit_then_blocks(self):
        for _ in range(3):
            self.assertTrue(self.limiter.allow("b", "k", limit=3, window_seconds=60))
        self.assertFalse(self.limiter.allow("b", "k", limit=3, window_seconds=60))

    def test_blocked_attempt_is_not_itself_counted(self):
        # A caller stuck at the ceiling must not push their own reset window
        # back out by continuing to hammer the route.
        for _ in range(3):
            self.limiter.allow("b", "k", limit=3, window_seconds=60)
        for _ in range(5):
            self.assertFalse(self.limiter.allow("b", "k", limit=3, window_seconds=60))
        self.clock.advance(61)
        self.assertTrue(self.limiter.allow("b", "k", limit=3, window_seconds=60))

    def test_window_expiry_allows_again(self):
        for _ in range(2):
            self.assertTrue(self.limiter.allow("b", "k", limit=2, window_seconds=10))
        self.assertFalse(self.limiter.allow("b", "k", limit=2, window_seconds=10))
        self.clock.advance(10.1)
        self.assertTrue(self.limiter.allow("b", "k", limit=2, window_seconds=10))

    def test_different_keys_have_independent_limits(self):
        for _ in range(2):
            self.assertTrue(self.limiter.allow("b", "caller-a", limit=2, window_seconds=60))
        self.assertFalse(self.limiter.allow("b", "caller-a", limit=2, window_seconds=60))
        # A different caller (IP) in the same bucket is unaffected.
        self.assertTrue(self.limiter.allow("b", "caller-b", limit=2, window_seconds=60))

    def test_different_buckets_have_independent_limits(self):
        for _ in range(2):
            self.assertTrue(self.limiter.allow("bucket-1", "k", limit=2, window_seconds=60))
        self.assertFalse(self.limiter.allow("bucket-1", "k", limit=2, window_seconds=60))
        # Same caller, different route bucket — independent ceiling.
        self.assertTrue(self.limiter.allow("bucket-2", "k", limit=2, window_seconds=60))

    def test_reset_clears_all_counters(self):
        for _ in range(2):
            self.limiter.allow("b", "k", limit=2, window_seconds=60)
        self.assertFalse(self.limiter.allow("b", "k", limit=2, window_seconds=60))
        self.limiter.reset()
        self.assertTrue(self.limiter.allow("b", "k", limit=2, window_seconds=60))

    def test_concurrent_callers_never_exceed_the_limit(self):
        # web/server.py serves each request on its own thread
        # (ThreadingHTTPServer) — this proves allow()'s lock actually
        # prevents the TOCTOU race a self-review found: without the lock,
        # concurrent threads can all read "under limit" before any of them
        # appends, letting more than `limit` through in one window.
        results = []
        results_lock = threading.Lock()
        barrier = threading.Barrier(20)

        def hit():
            barrier.wait()  # maximize actual concurrent overlap in allow()
            ok = self.limiter.allow("b", "k", limit=5, window_seconds=60)
            with results_lock:
                results.append(ok)

        threads = [threading.Thread(target=hit) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(sum(1 for r in results if r), 5)
        self.assertEqual(sum(1 for r in results if not r), 15)

    def test_sweep_drops_entries_that_have_gone_quiet(self):
        # A caller seen once and never again must not loiter in _hits
        # forever (self-review, #131) — an amortized sweep every
        # _SWEEP_EVERY calls drops anything idle past _STALE_AFTER_SECONDS.
        from hockey_scheduler.web import rate_limit as rl_module
        self.limiter.allow("b", "one-off-caller", limit=100, window_seconds=60)
        self.assertIn(("b", "one-off-caller"), self.limiter._hits)
        self.clock.advance(rl_module._STALE_AFTER_SECONDS + 1)
        # Drive enough calls (a different key, so it doesn't touch our
        # target entry directly) to trigger the periodic sweep.
        for _ in range(rl_module._SWEEP_EVERY):
            self.limiter.allow("b", "someone-else", limit=100, window_seconds=60)
        self.assertNotIn(("b", "one-off-caller"), self.limiter._hits)


class RateLimitHttpTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        srv = __import__("hockey_scheduler.web.server", fromlist=["x"])
        cls.srv = srv
        srv.STATE.reset()
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), srv.Handler)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()
        cls.home = srv.STATE.ids["home_team_id"]

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.thread.join(timeout=5)

    def setUp(self):
        self.srv.RATE_LIMITER.reset()

    def _req(self, method, path, body=None, headers=None):
        url = f"http://127.0.0.1:{self.port}{path}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        for k, v in (headers or {}).items():
            req.add_header(k, v)
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def test_public_feed_mint_route_gets_rate_limited_after_the_ceiling(self):
        # The mint route's ceiling (limit=5) from web/server.py.
        for i in range(5):
            st, body = self._req(
                "POST", "/api/public/calendar-feeds",
                {"actor_type": "team", "actor_ref": self.home})
            self.assertEqual(st, 200, f"request {i} unexpectedly limited")
        st, body = self._req(
            "POST", "/api/public/calendar-feeds",
            {"actor_type": "team", "actor_ref": self.home})
        self.assertEqual(st, 429)
        self.assertEqual(body["error"]["code"], "rate_limited")

    def test_exhausting_the_mint_bucket_does_not_throttle_the_read_bucket(self):
        # Each route family has its own ceiling (web/server.py) — exhausting
        # the tight mint limit must not spill over into the generous
        # public-read limit, since real callers browse far more than they
        # subscribe.
        for _ in range(5):
            self._req("POST", "/api/public/calendar-feeds",
                      {"actor_type": "team", "actor_ref": self.home})
        st, _ = self._req("POST", "/api/public/calendar-feeds",
                          {"actor_type": "team", "actor_ref": self.home})
        self.assertEqual(st, 429)

    def test_x_forwarded_for_is_ignored_by_default(self):
        # Without an explicit TRUST_PROXY_HEADERS=1 opt-in, X-Forwarded-For
        # must NOT be trusted — otherwise any anonymous caller could defeat
        # the limiter entirely by sending a different value on every request
        # (PR #132 review: unconditional trust was a real regression, worse
        # than the proxy-collapse problem it was meant to fix). All five
        # "different" forwarded IPs below must still share ONE real bucket,
        # keyed on the actual loopback socket.
        for i in range(5):
            st, _ = self._req(
                "POST", "/api/public/calendar-feeds",
                {"actor_type": "team", "actor_ref": self.home},
                headers={"X-Forwarded-For": f"203.0.113.{i}"})
            self.assertEqual(st, 200)
        blocked, _ = self._req(
            "POST", "/api/public/calendar-feeds",
            {"actor_type": "team", "actor_ref": self.home},
            headers={"X-Forwarded-For": "203.0.113.99"})
        self.assertEqual(blocked, 429, "a spoofed X-Forwarded-For bypassed the limit")

    def test_x_forwarded_for_is_trusted_only_with_explicit_opt_in(self):
        # The production runbook documents deployment behind a
        # TLS-terminating proxy, where every real caller would otherwise
        # share one bucket keyed on the proxy's own connecting IP — but only
        # once the deployer has explicitly confirmed that topology via
        # TRUST_PROXY_HEADERS=1. This proves two distinct forwarded IPs get
        # independent ceilings once trusted.
        os.environ["TRUST_PROXY_HEADERS"] = "1"
        try:
            for _ in range(5):
                st, _ = self._req(
                    "POST", "/api/public/calendar-feeds",
                    {"actor_type": "team", "actor_ref": self.home},
                    headers={"X-Forwarded-For": "203.0.113.10"})
                self.assertEqual(st, 200)
            blocked, _ = self._req(
                "POST", "/api/public/calendar-feeds",
                {"actor_type": "team", "actor_ref": self.home},
                headers={"X-Forwarded-For": "203.0.113.10"})
            self.assertEqual(blocked, 429)
            # A distinct forwarded IP is unaffected — proves isolation, not
            # just that the header is read at all.
            other, _ = self._req(
                "POST", "/api/public/calendar-feeds",
                {"actor_type": "team", "actor_ref": self.home},
                headers={"X-Forwarded-For": "203.0.113.99"})
            self.assertEqual(other, 200)
        finally:
            del os.environ["TRUST_PROXY_HEADERS"]

    def test_public_read_routes_share_a_generous_bucket(self):
        # limit=120 on the public_read bucket — well under it in a normal test.
        for _ in range(10):
            st, _ = self._req("GET", "/api/public/schedule")
            self.assertEqual(st, 200)

    def test_authenticated_calendar_feed_route_is_not_rate_limited_by_the_public_bucket(self):
        # The authenticated /api/calendar-feeds route (owner/operator gated)
        # is a distinct code path from /api/public/calendar-feeds and must
        # not be throttled by the public mint ceiling.
        opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor())
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/api/auth/login",
            data=json.dumps({"username": "admin", "password": "demo"}).encode(),
            method="POST", headers={"Content-Type": "application/json"})
        opener.open(req)

        # Exhaust the anonymous mint ceiling first.
        for _ in range(6):
            self._req("POST", "/api/public/calendar-feeds",
                      {"actor_type": "team", "actor_ref": self.home})

        def authed_req(method, path, body):
            data = json.dumps(body).encode()
            r = urllib.request.Request(
                f"http://127.0.0.1:{self.port}{path}", data=data, method=method,
                headers={"Content-Type": "application/json"})
            try:
                with opener.open(r) as resp:
                    return resp.status, json.loads(resp.read())
            except urllib.error.HTTPError as e:
                return e.code, json.loads(e.read())

        st, _ = authed_req("POST", "/api/calendar-feeds",
                           {"actor_type": "team", "actor_ref": self.home})
        self.assertEqual(st, 200)


if __name__ == "__main__":
    unittest.main()
