import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from http.server import ThreadingHTTPServer

from helpers import BACKEND, cookie_from_set_cookie  # noqa: F401

from hockey_scheduler.api import ApiService
from hockey_scheduler.bootstrap import (
    bootstrap_admin,
    claim_installation,
    installation_claim_status,
)
from hockey_scheduler.domain.errors import (
    AlreadyClaimedError,
    ClaimUnavailableError,
    InvalidSetupCodeError,
)
from hockey_scheduler.store import InMemoryStore, SqlStore
from hockey_scheduler.web import server as srv


SETUP_CODE = "fixture-only-setup-code-with-enough-entropy-7f29"


class InstallationClaimServiceTest(unittest.TestCase):
    def _durable_api(self):
        fd, path = tempfile.mkstemp(suffix=".sqlite")
        os.close(fd)
        return path, ApiService(SqlStore(path))

    def test_status_is_public_safe_and_requires_production_durable_ready_store(self):
        memory = ApiService(InMemoryStore())
        self.assertEqual(
            installation_claim_status(memory, {"INITIAL_SETUP_CODE": SETUP_CODE}, "demo"),
            {"claim_available": False, "reason": "not_production"})
        self.assertEqual(
            installation_claim_status(memory, {"INITIAL_SETUP_CODE": SETUP_CODE},
                                      "production"),
            {"claim_available": False, "reason": "durable_store_required"})

        transient = ApiService(SqlStore(":memory:"))
        self.assertEqual(
            installation_claim_status(transient,
                                      {"INITIAL_SETUP_CODE": SETUP_CODE},
                                      "production")["reason"],
            "durable_store_required")

        path, api = self._durable_api()
        try:
            missing = installation_claim_status(api, {}, "production")
            self.assertEqual(missing,
                             {"claim_available": False,
                              "reason": "setup_code_missing"})
            available = installation_claim_status(
                api, {"INITIAL_SETUP_CODE": SETUP_CODE}, "production")
            self.assertEqual(set(available), {"claim_available", "reason"})
            self.assertEqual(available,
                             {"claim_available": True, "reason": None})
            self.assertNotIn("backend", available)
            self.assertNotIn("accounts", available)
        finally:
            api.store.close()
            os.remove(path)

    def test_wrong_code_is_generic_and_writes_nothing(self):
        path, api = self._durable_api()
        try:
            with self.assertRaises(InvalidSetupCodeError) as ctx:
                claim_installation(
                    api, {"INITIAL_SETUP_CODE": SETUP_CODE}, "production",
                    "wrong", "client-admin", "client-password")
            self.assertNotIn(SETUP_CODE, str(ctx.exception))
            self.assertNotIn("client-password", str(ctx.exception))
            self.assertEqual(api.store.all_user_accounts(), [])
            self.assertIsNone(api.store.get_installation_state("primary"))
            self.assertEqual(api.store.all_setup_audit(), [])
        finally:
            api.store.close()
            os.remove(path)

    def test_correct_code_claims_once_and_persists_without_secrets(self):
        path, api = self._durable_api()
        env = {"INITIAL_SETUP_CODE": SETUP_CODE}
        try:
            account = claim_installation(
                api, env, "production", SETUP_CODE,
                "Client-Admin", "client-password")
            self.assertEqual(account.username, "client-admin")
            self.assertEqual(account.role.value, "league_admin")
            marker = api.store.get_installation_state("primary")
            self.assertEqual(marker.claimed_by_user_id, account.id)
            self.assertEqual(marker.claim_method, "browser")
            self.assertEqual(
                installation_claim_status(api, env, "production"),
                {"claim_available": False, "reason": "already_claimed"})
            with self.assertRaises(AlreadyClaimedError):
                claim_installation(
                    api, env, "production", SETUP_CODE,
                    "second-admin", "another-password")

            audit_blob = json.dumps([
                {"action": a.action, "actor_id": a.actor_id,
                 "detail": a.detail}
                for a in api.store.all_setup_audit()
            ])
            self.assertNotIn(SETUP_CODE, audit_blob)
            self.assertNotIn("client-password", audit_blob)
            self.assertIn("installation_claimed", audit_blob)

            api.store.close()
            with open(path, "rb") as database_file:
                database_bytes = database_file.read()
            self.assertNotIn(SETUP_CODE.encode(), database_bytes)
            self.assertNotIn(b"client-password", database_bytes)

            reloaded = ApiService(SqlStore(path))
            try:
                self.assertIsNotNone(reloaded.verify_login(
                    "client-admin", "client-password"))
                self.assertEqual(
                    reloaded.store.get_installation_state("primary")
                    .claimed_by_user_id,
                    account.id)
            finally:
                reloaded.store.close()
        finally:
            try:
                api.store.close()
            except Exception:
                pass
            os.remove(path)

    def test_operations_bootstrap_consumes_the_same_claim(self):
        path, api = self._durable_api()
        try:
            created, _ = bootstrap_admin(api, "ops-root", "ops-password")
            self.assertTrue(created)
            self.assertEqual(
                installation_claim_status(
                    api, {"INITIAL_SETUP_CODE": SETUP_CODE}, "production"),
                {"claim_available": False, "reason": "already_claimed"})
            with self.assertRaises(AlreadyClaimedError):
                claim_installation(
                    api, {"INITIAL_SETUP_CODE": SETUP_CODE}, "production",
                    SETUP_CODE, "client-admin", "client-password")
        finally:
            api.store.close()
            os.remove(path)

    def test_claim_unavailable_does_not_validate_or_echo_submitted_secrets(self):
        api = ApiService(InMemoryStore())
        with self.assertRaises(ClaimUnavailableError) as ctx:
            claim_installation(
                api, {"INITIAL_SETUP_CODE": SETUP_CODE}, "production",
                "submitted-secret", "admin", "password-secret")
        text = str(ctx.exception)
        self.assertNotIn("submitted-secret", text)
        self.assertNotIn("password-secret", text)

    def test_two_concurrent_claims_create_exactly_one_admin(self):
        api = ApiService(InMemoryStore())
        barrier = threading.Barrier(2)

        def attempt(username):
            barrier.wait()
            try:
                return api.accounts.create_first_admin_if_unclaimed(
                    username, "password", claim_method="browser").username
            except AlreadyClaimedError:
                return "already_claimed"

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(attempt, ("admin-a", "admin-b")))
        self.assertEqual(results.count("already_claimed"), 1)
        self.assertEqual(len(api.store.all_user_accounts()), 1)
        self.assertIsNotNone(api.store.get_installation_state("primary"))


class InstallationClaimHttpTest(unittest.TestCase):
    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".sqlite")
        os.close(fd)
        os.environ["APP_MODE"] = "production"
        os.environ["DATABASE_URL"] = self.db_path
        os.environ["INITIAL_SETUP_CODE"] = SETUP_CODE
        os.environ.pop("BOOTSTRAP_ADMIN_USER", None)
        os.environ.pop("BOOTSTRAP_ADMIN_PASSWORD", None)
        srv.STATE.reset()
        srv.RATE_LIMITER.reset()
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), srv.Handler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.httpd.shutdown()
        self.thread.join(timeout=5)
        self.httpd.server_close()
        try:
            srv.STATE.api.store.close()
        except Exception:
            pass
        for key in ("APP_MODE", "DATABASE_URL", "INITIAL_SETUP_CODE",
                    "BOOTSTRAP_ADMIN_USER", "BOOTSTRAP_ADMIN_PASSWORD"):
            os.environ.pop(key, None)
        try:
            os.remove(self.db_path)
        except OSError:
            pass
        srv.STATE.reset()

    def _req(self, method, path, body=None, return_headers=False):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}", data=data, method=method)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req) as response:
                payload = json.loads(response.read() or b"{}")
                result = (response.status, payload, response.headers)
        except urllib.error.HTTPError as error:
            result = (error.code, json.loads(error.read() or b"{}"), error.headers)
        return result if return_headers else result[:2]

    def test_setup_page_and_status_are_public(self):
        with urllib.request.urlopen(
                f"http://127.0.0.1:{self.port}/setup") as response:
            html = response.read().decode()
        self.assertIn("Claim this installation", html)
        status, payload = self._req("GET", "/api/bootstrap/status")
        self.assertEqual(status, 200)
        self.assertEqual(payload,
                         {"claim_available": True, "reason": None})
        self.assertNotIn(SETUP_CODE, json.dumps(payload))

    def test_claim_establishes_session_then_closes_endpoint(self):
        status, payload, headers = self._req(
            "POST", "/api/bootstrap/claim",
            {"setup_code": SETUP_CODE,
             "username": "client-admin",
             "password": "client-password"},
            return_headers=True)
        self.assertEqual(status, 200)
        self.assertEqual(payload["user"]["role"], "league_admin")
        self.assertNotIn("password", json.dumps(payload).lower())
        cookie = cookie_from_set_cookie(
            headers.get("Set-Cookie"), srv.SESSION_COOKIE)
        self.assertIsNotNone(cookie)

        _, after = self._req("GET", "/api/bootstrap/status")
        self.assertEqual(after,
                         {"claim_available": False,
                          "reason": "already_claimed"})
        status, repeated = self._req(
            "POST", "/api/bootstrap/claim",
            {"setup_code": SETUP_CODE,
             "username": "second",
             "password": "second-password"})
        self.assertEqual(status, 409)
        self.assertEqual(repeated["error"]["code"], "already_claimed")
        self.assertEqual(len(srv.STATE.api.store.all_user_accounts()), 1)

    def test_wrong_code_is_generic_and_rate_limited(self):
        for _ in range(5):
            status, payload = self._req(
                "POST", "/api/bootstrap/claim",
                {"setup_code": "wrong-code",
                 "username": "admin",
                 "password": "password-secret"})
            self.assertEqual(status, 401)
            blob = json.dumps(payload)
            self.assertNotIn("wrong-code", blob)
            self.assertNotIn("password-secret", blob)
        status, payload = self._req(
            "POST", "/api/bootstrap/claim",
            {"setup_code": "wrong-again",
             "username": "admin",
             "password": "password-secret"})
        self.assertEqual(status, 429)
        self.assertEqual(payload["error"]["code"], "rate_limited")
        self.assertEqual(srv.STATE.api.store.all_user_accounts(), [])


if __name__ == "__main__":
    unittest.main()
