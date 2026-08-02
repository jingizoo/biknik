"""Login throttling + minimum credential policy (#267).

Three layers:
  * ``LoginThrottleTest`` — the failed-attempt lockout keyed on BOTH source IP
    and normalized username, driven by an injected clock (no wall-clock sleeps).
  * ``PasswordPolicy*Test`` — the minimum credential policy on the account
    creation / first-admin claim paths, on the in-memory and SQLite stores,
    proving it is enforced in production, relaxed for development credentials,
    configurable within safe bounds, and never invalidates existing hashes.
  * ``LoginSecurityHttpTest`` — the login route end to end: throttled after the
    ceiling with a generic 429 + Retry-After, identical responses for unknown
    username vs wrong password, success resets the lockout, and X-Forwarded-For
    is trusted only with the explicit opt-in.
"""

import json
import math
import os
import threading
import unittest
import urllib.error
import urllib.request

from helpers import BACKEND  # noqa: F401  (ensures sys.path is set up)

from hockey_scheduler.api import ApiService
from hockey_scheduler.domain import Role
from hockey_scheduler.domain.errors import ValidationError
from hockey_scheduler.services import passwords
from hockey_scheduler.store import InMemoryStore, SqlStore
from hockey_scheduler.web import server as srv
from hockey_scheduler.web.rate_limit import LoginThrottle


class _FakeClock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += seconds


class LoginThrottleTest(unittest.TestCase):
    def setUp(self):
        self.clock = _FakeClock()
        self.throttle = LoginThrottle(
            clock=self.clock, user_max=3, ip_max=5, window_seconds=100)

    def test_username_locks_after_max_failures(self):
        for _ in range(2):
            self.assertEqual(self.throttle.record_failure("ip1", "bob"), [])
            self.assertEqual(self.throttle.retry_after("ip1", "bob"), 0.0)
        # The 3rd failure crosses the username ceiling and reports the lock.
        self.assertEqual(self.throttle.record_failure("ip1", "bob"), ["username"])
        self.assertGreater(self.throttle.retry_after("ip1", "bob"), 0.0)

    def test_ip_locks_after_ip_max_across_distinct_usernames(self):
        # Distinct usernames never lock any username bucket (max 3 each), but
        # they share one IP bucket (max 5) — so the IP locks on the 5th.
        for i in range(4):
            self.assertEqual(self.throttle.record_failure("ip1", f"u{i}"), [])
        self.assertEqual(self.throttle.record_failure("ip1", "u4"), ["ip"])
        self.assertGreater(self.throttle.retry_after("ip1", "u4"), 0.0)
        # A brand-new username from the SAME ip is still blocked — by IP.
        self.assertGreater(self.throttle.retry_after("ip1", "fresh"), 0.0)

    def test_retry_after_is_a_pure_peek(self):
        for _ in range(3):
            self.throttle.record_failure("ip1", "bob")
        # Peeking many times while locked must not extend the lock.
        first = self.throttle.retry_after("ip1", "bob")
        for _ in range(10):
            self.throttle.retry_after("ip1", "bob")
        self.assertEqual(self.throttle.retry_after("ip1", "bob"), first)

    def test_success_clears_the_username_bucket(self):
        for _ in range(2):
            self.throttle.record_failure("ip1", "bob")
        self.throttle.record_success("ip1", "bob")
        # After a success, it takes a full fresh run of failures to lock the
        # username again. The post-success failures come from distinct IPs so
        # no single IP bucket reaches its own (independent) ceiling here — this
        # test isolates the username bucket; IP persistence is covered
        # separately by test_ip_locks_after_ip_max_across_distinct_usernames.
        self.assertEqual(self.throttle.record_failure("ipB", "bob"), [])
        self.assertEqual(self.throttle.record_failure("ipC", "bob"), [])
        self.assertEqual(self.throttle.record_failure("ipD", "bob"), ["username"])

    def test_window_expiry_unlocks(self):
        for _ in range(3):
            self.throttle.record_failure("ip1", "bob")
        self.assertGreater(self.throttle.retry_after("ip1", "bob"), 0.0)
        self.clock.advance(100.1)
        self.assertEqual(self.throttle.retry_after("ip1", "bob"), 0.0)

    def test_ip_and_username_scopes_are_independent(self):
        for _ in range(3):
            self.throttle.record_failure("ipA", "bob")  # locks bob
        # A different username from a different IP is unaffected.
        self.assertEqual(self.throttle.retry_after("ipB", "carol"), 0.0)
        # bob from a different IP is still locked — the username bucket travels.
        self.assertGreater(self.throttle.retry_after("ipB", "bob"), 0.0)

    def test_env_config_has_safe_floors(self):
        for key, val in (("HS_LOGIN_MAX_FAILURES", "0"),
                         ("HS_LOGIN_IP_MAX_FAILURES", "0"),
                         ("HS_LOGIN_WINDOW_SECONDS", "1")):
            os.environ[key] = val
        try:
            t = LoginThrottle(clock=self.clock)
            self.assertGreaterEqual(t._user_max, 1)
            self.assertGreaterEqual(t._ip_max, 1)
            self.assertGreaterEqual(t._window, 30.0)
        finally:
            for key in ("HS_LOGIN_MAX_FAILURES", "HS_LOGIN_IP_MAX_FAILURES",
                        "HS_LOGIN_WINDOW_SECONDS"):
                os.environ.pop(key, None)

    def test_env_config_has_safe_ceilings(self):
        # A huge value must not effectively disable the throttle or wedge a lock
        # open forever — every knob is clamped to a safe upper bound.
        for key, val in (("HS_LOGIN_MAX_FAILURES", "999999"),
                         ("HS_LOGIN_IP_MAX_FAILURES", "999999"),
                         ("HS_LOGIN_WINDOW_SECONDS", "99999999")):
            os.environ[key] = val
        try:
            t = LoginThrottle(clock=self.clock)
            self.assertLessEqual(t._user_max, 100)
            self.assertLessEqual(t._ip_max, 5000)
            self.assertLessEqual(t._window, 86_400.0)
        finally:
            for key in ("HS_LOGIN_MAX_FAILURES", "HS_LOGIN_IP_MAX_FAILURES",
                        "HS_LOGIN_WINDOW_SECONDS"):
                os.environ.pop(key, None)

    def test_begin_reserves_slots_so_concurrency_cannot_overshoot(self):
        # Three attempts reserve all three username slots while "in flight"
        # (before any records a failure). The 4th is rejected up front — this is
        # the overshoot a bare check-then-record gap would let through.
        self.assertEqual(self.throttle.begin("ip1", "bob"), 0.0)
        self.assertEqual(self.throttle.begin("ip1", "bob"), 0.0)
        self.assertEqual(self.throttle.begin("ip1", "bob"), 0.0)
        self.assertGreater(self.throttle.begin("ip1", "bob"), 0.0)
        # Resolving the three in-flight attempts as failures keeps the net count
        # at the ceiling (reservation converts to a recorded failure, 1:1).
        for _ in range(3):
            self.throttle.record_failure("ip1", "bob")
        self.assertGreater(self.throttle.retry_after("ip1", "bob"), 0.0)

    def test_cancel_releases_a_reserved_slot(self):
        t = LoginThrottle(clock=self.clock, user_max=2, ip_max=100,
                          window_seconds=100)
        self.assertEqual(t.begin("ip1", "bob"), 0.0)   # reserve 1/2
        self.assertEqual(t.begin("ip1", "bob"), 0.0)   # reserve 2/2
        self.assertGreater(t.begin("ip1", "bob"), 0.0)  # full → rejected
        t.cancel("ip1", "bob")                          # release one
        self.assertEqual(t.begin("ip1", "bob"), 0.0)    # room again

    def test_success_releases_a_reserved_slot(self):
        t = LoginThrottle(clock=self.clock, user_max=2, ip_max=100,
                          window_seconds=100)
        t.begin("ip1", "bob")
        t.begin("ip1", "bob")
        t.record_success("ip1", "bob")                  # releases + clears failures
        self.assertEqual(t.begin("ip1", "bob"), 0.0)

    def test_state_is_bounded_by_an_amortized_sweep(self):
        # Each of many one-off usernames leaves a bucket behind; once they age
        # out of the window the amortized sweep must reclaim them instead of
        # letting a username/IP spray grow memory without bound.
        t = LoginThrottle(clock=self.clock, user_max=3, ip_max=10_000_000,
                          window_seconds=100)
        for i in range(600):
            t.record_failure("ipX", f"user{i}")
        self.clock.advance(200)                         # all age out of the window
        for i in range(600, 1100):
            t.record_failure("ipX", f"user{i}")         # crosses a sweep boundary
        # Without the sweep this would hold 1100+ dead username buckets.
        self.assertLess(len(t._fail), 700)

    def test_active_window_spray_is_capped_without_aging(self):
        # The sweep can't help here: NO time advances, so nothing ages out. The
        # only thing that can bound memory against a spray of fresh identities
        # inside one window is the hard cardinality cap.
        t = LoginThrottle(clock=self.clock, user_max=100, ip_max=100,
                          window_seconds=1000, max_keys=10)
        rejected_fresh = 0
        for i in range(500):
            if t.begin(f"ip{i}", f"user{i}") > 0:
                rejected_fresh += 1
                continue
            t.record_failure(f"ip{i}", f"user{i}")
        self.assertLessEqual(t._tracked_size(), 10)     # never grew past the cap
        self.assertGreater(rejected_fresh, 0)           # fresh identities failed closed

    def test_pending_reservations_count_toward_capacity(self):
        # Reservations that never resolve (all held in-flight) must also count
        # toward the cap — pending state cannot be an unbounded side channel.
        t = LoginThrottle(clock=self.clock, user_max=100, ip_max=100,
                          window_seconds=1000, max_keys=6)
        admitted = 0
        for i in range(50):
            if t.begin(f"ip{i}", f"user{i}") == 0.0:     # reserved, never recorded
                admitted += 1
        self.assertLessEqual(admitted, 3)               # 6 keys / 2 per admit
        self.assertLessEqual(t._tracked_size(), 6)

    def test_known_identity_still_served_when_table_is_full(self):
        # Fail-closed applies only to *previously unseen* identities; an already
        # tracked one keeps working under a full table (serving it adds no key).
        t = LoginThrottle(clock=self.clock, user_max=100, ip_max=100,
                          window_seconds=1000, max_keys=4)
        self.assertEqual(t.begin("known_ip", "known_user"), 0.0)
        t.record_failure("known_ip", "known_user")
        for i in range(50):                             # fill the rest of the table
            if t.begin(f"ip{i}", f"user{i}") == 0.0:
                t.record_failure(f"ip{i}", f"user{i}")
        self.assertEqual(t.begin("known_ip", "known_user"), 0.0)      # still served
        self.assertGreater(t.begin("brand_new_ip", "brand_new_user"), 0.0)  # closed

    def test_oversized_identities_use_fixed_size_keys(self):
        # A megabyte-long username/IP must not become a megabyte-long dict key.
        t = LoginThrottle(clock=self.clock, user_max=3, ip_max=3,
                          window_seconds=100)
        huge = "x" * 1_000_000
        t.record_failure(huge, huge)
        for scope, digest in t._fail:
            self.assertIn(scope, ("ip", "user"))
            self.assertIsInstance(digest, bytes)
            self.assertEqual(len(digest), 16)           # fixed-size, input-independent

    def test_non_finite_window_config_falls_back_safely(self):
        for bad in ("inf", "-inf", "nan", "1e999"):
            os.environ["HS_LOGIN_WINDOW_SECONDS"] = bad
            try:
                t = LoginThrottle(clock=self.clock)
                self.assertTrue(math.isfinite(t._window), bad)
                self.assertGreaterEqual(t._window, 30.0)
                self.assertLessEqual(t._window, 86_400.0)
            finally:
                os.environ.pop("HS_LOGIN_WINDOW_SECONDS", None)

    def test_non_finite_int_config_falls_back_safely(self):
        for key in ("HS_LOGIN_MAX_FAILURES", "HS_LOGIN_IP_MAX_FAILURES"):
            for bad in ("inf", "nan", "1e999"):
                os.environ[key] = bad
                try:
                    t = LoginThrottle(clock=self.clock)
                    # Falls back to the safe default, never an unbounded/off value.
                    self.assertGreaterEqual(t._user_max, 1)
                    self.assertLessEqual(t._user_max, 100)
                    self.assertGreaterEqual(t._ip_max, 1)
                    self.assertLessEqual(t._ip_max, 5000)
                finally:
                    os.environ.pop(key, None)


class _PasswordPolicyContract:
    def make_store(self):
        raise NotImplementedError

    def setUp(self):
        self.api = ApiService(self.make_store())
        os.environ.pop("APP_MODE", None)
        os.environ.pop("HS_MIN_PASSWORD_LENGTH", None)

    def tearDown(self):
        os.environ.pop("APP_MODE", None)
        os.environ.pop("HS_MIN_PASSWORD_LENGTH", None)

    def _create(self, username, password):
        return self.api.accounts.create_account(username, password, Role.LEAGUE_ADMIN)

    def test_empty_password_always_rejected(self):
        with self.assertRaises(ValidationError):
            self._create("a", "")

    def test_demo_mode_allows_short_password(self):
        acct = self._create("shorty", "demo")   # 4 chars, non-production
        self.assertEqual(acct.username, "shorty")

    def test_production_rejects_below_minimum(self):
        os.environ["APP_MODE"] = "production"
        with self.assertRaises(ValidationError) as ctx:
            self._create("weak", "abc")
        self.assertEqual(ctx.exception.details.get("reason"), "weak_password")

    def test_production_allows_at_minimum(self):
        os.environ["APP_MODE"] = "production"
        acct = self._create("strong", "a" * passwords.min_password_length())
        self.assertEqual(acct.username, "strong")

    def test_configurable_minimum_can_be_raised(self):
        os.environ["APP_MODE"] = "production"
        os.environ["HS_MIN_PASSWORD_LENGTH"] = "16"
        with self.assertRaises(ValidationError):
            self._create("mid", "a" * 12)
        self.assertTrue(self._create("ok", "a" * 16))

    def test_production_floor_cannot_be_lowered(self):
        # A stray/misconfigured override below the floor must never weaken the
        # production policy — the floor still applies.
        os.environ["APP_MODE"] = "production"
        os.environ["HS_MIN_PASSWORD_LENGTH"] = "2"
        self.assertGreaterEqual(passwords.min_password_length(), 8)
        with self.assertRaises(ValidationError):
            self._create("tiny", "abc")

    def test_first_admin_claim_enforces_policy_and_writes_nothing_on_reject(self):
        os.environ["APP_MODE"] = "production"
        with self.assertRaises(ValidationError):
            self.api.accounts.create_first_admin_if_unclaimed("admin", "short")
        # Rejected before the installation marker or account is written.
        self.assertEqual(self.api.accounts.list_accounts(), [])

    def test_existing_hash_still_verifies_after_policy_would_reject(self):
        # Created while short passwords were allowed (demo); the account keeps
        # working even once production would reject that password at creation.
        self._create("legacy", "demo")
        os.environ["APP_MODE"] = "production"
        self.assertIsNotNone(self.api.accounts.verify_login("legacy", "demo"))


class MemoryPasswordPolicyTest(_PasswordPolicyContract, unittest.TestCase):
    def make_store(self):
        return InMemoryStore()


class DurablePasswordPolicyTest(_PasswordPolicyContract, unittest.TestCase):
    def make_store(self):
        url = os.environ.get("TEST_DATABASE_URL") or ":memory:"
        store = SqlStore(url)
        store.reset_schema()
        return store


class LoginSecurityHttpTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        srv.STATE.reset()
        cls.httpd = __import__("http.server", fromlist=["ThreadingHTTPServer"]) \
            .ThreadingHTTPServer(("127.0.0.1", 0), srv.Handler)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()
        cls._orig_throttle = srv.LOGIN_THROTTLE

    @classmethod
    def tearDownClass(cls):
        srv.LOGIN_THROTTLE = cls._orig_throttle
        cls.httpd.shutdown()
        cls.thread.join(timeout=5)
        cls.httpd.server_close()

    def setUp(self):
        # A small, real-clock throttle so a handful of requests exercises the
        # lockout deterministically. High ip_max keeps username tests from
        # tripping the IP bucket; the proxy test installs its own low ip_max.
        srv.LOGIN_THROTTLE = LoginThrottle(user_max=3, ip_max=100)

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
                return r.status, dict(r.headers), json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            return e.code, dict(e.headers), json.loads(e.read() or b"{}")

    def test_repeated_failures_are_throttled_with_retry_after(self):
        for i in range(3):
            st, _h, body = self._req(
                "POST", "/api/auth/login",
                {"username": "admin", "password": "wrong"})
            self.assertEqual(st, 401, f"attempt {i}")
        st, headers, body = self._req(
            "POST", "/api/auth/login", {"username": "admin", "password": "wrong"})
        self.assertEqual(st, 429)
        self.assertEqual(body["error"]["code"], "rate_limited")
        self.assertIn("Retry-After", headers)
        self.assertGreaterEqual(int(headers["Retry-After"]), 1)

    def test_concurrent_failures_do_not_overshoot_the_ceiling(self):
        # Fire many simultaneous wrong-password attempts at one username. Only
        # `user_max` may reach the verify path (401); the rest are gated up front
        # (429). Without the atomic reserve in `begin`, all would slip through the
        # check-then-record gap and overshoot the ceiling.
        results = []
        barrier = threading.Barrier(10)

        def attempt():
            barrier.wait()  # release all threads together for real contention
            st, _h, _b = self._req(
                "POST", "/api/auth/login",
                {"username": "admin", "password": "wrong"})
            results.append(st)

        threads = [threading.Thread(target=attempt) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
        self.assertLessEqual(sum(1 for s in results if s == 401), 3)  # user_max
        self.assertTrue(any(s == 429 for s in results))

    def test_unknown_username_and_wrong_password_are_indistinguishable(self):
        _st1, _h1, unknown = self._req(
            "POST", "/api/auth/login",
            {"username": "no-such-user", "password": "wrong"})
        _st2, _h2, wrongpw = self._req(
            "POST", "/api/auth/login", {"username": "admin", "password": "wrong"})
        self.assertEqual(_st1, 401)
        self.assertEqual(_st2, 401)
        self.assertEqual(unknown, wrongpw)  # byte-identical error body

    def test_successful_login_resets_the_lockout(self):
        for _ in range(2):  # under the ceiling of 3
            self._req("POST", "/api/auth/login",
                      {"username": "admin", "password": "wrong"})
        st, _h, _b = self._req(
            "POST", "/api/auth/login", {"username": "admin", "password": "demo"})
        self.assertEqual(st, 200)
        # The success cleared admin's failures, so the next wrong attempt is a
        # plain 401, not an immediate throttle.
        st, _h, _b = self._req(
            "POST", "/api/auth/login", {"username": "admin", "password": "wrong"})
        self.assertEqual(st, 401)

    def test_spoofed_x_forwarded_for_cannot_dodge_the_ip_lockout(self):
        srv.LOGIN_THROTTLE = LoginThrottle(user_max=100, ip_max=3)
        # Distinct usernames (so no username bucket locks) with a different
        # spoofed forwarded IP each time — all share ONE real IP bucket by
        # default, so the 4th is throttled.
        for i in range(3):
            st, _h, _b = self._req(
                "POST", "/api/auth/login",
                {"username": f"user{i}", "password": "wrong"},
                headers={"X-Forwarded-For": f"203.0.113.{i}"})
            self.assertEqual(st, 401)
        st, _h, _b = self._req(
            "POST", "/api/auth/login",
            {"username": "user9", "password": "wrong"},
            headers={"X-Forwarded-For": "203.0.113.9"})
        self.assertEqual(st, 429)

    def test_x_forwarded_for_gives_independent_ip_buckets_when_trusted(self):
        srv.LOGIN_THROTTLE = LoginThrottle(user_max=100, ip_max=3)
        os.environ["TRUST_PROXY_HEADERS"] = "1"
        try:
            for i in range(3):
                st, _h, _b = self._req(
                    "POST", "/api/auth/login",
                    {"username": f"user{i}", "password": "wrong"},
                    headers={"X-Forwarded-For": "203.0.113.10"})
                self.assertEqual(st, 401)
            st, _h, _b = self._req(
                "POST", "/api/auth/login",
                {"username": "userX", "password": "wrong"},
                headers={"X-Forwarded-For": "203.0.113.10"})
            self.assertEqual(st, 429)  # that forwarded IP is locked
            # A different forwarded IP has its own bucket — not throttled.
            st, _h, _b = self._req(
                "POST", "/api/auth/login",
                {"username": "userY", "password": "wrong"},
                headers={"X-Forwarded-For": "203.0.113.99"})
            self.assertEqual(st, 401)
        finally:
            os.environ.pop("TRUST_PROXY_HEADERS", None)


if __name__ == "__main__":
    unittest.main()
