"""Guarded production factory reset (#256).

Contract tests run against both InMemoryStore and a durable SQL backend
(SQLite by default, PostgreSQL via TEST_DATABASE_URL) so the atomic wipe,
its rollback-on-failure, and the surviving audit event behave identically
everywhere. A separate HTTP test class drives the real routes end to end.
"""

import json
import os
import threading
import unittest
import urllib.error
import urllib.request
from datetime import timedelta
from http.server import ThreadingHTTPServer

from helpers import BACKEND, cookie_from_set_cookie  # noqa: F401

from hockey_scheduler.domain import Organization, Role, Team
from hockey_scheduler.services.factory_reset_service import CONFIRMATION_PHRASE
from hockey_scheduler.store import InMemoryStore, SqlStore
from hockey_scheduler.api import ApiService
from hockey_scheduler.web import server as srv


class FactoryResetContract:
    """Shared fixtures + tests, run against Memory and a durable SQL backend."""

    def make_store(self):
        raise NotImplementedError

    def setUp(self):
        self.store = self.make_store()
        self.api = ApiService(self.store)
        self.admin = self.api.accounts.create_account(
            "boss", "hunter22", Role.LEAGUE_ADMIN)
        self.coach = self.api.accounts.create_account(
            "coach1", "coachpass", Role.COACH)

    def tearDown(self):
        conn = getattr(self.store, "conn", None)
        if conn is not None:
            conn.close()

    def _add_some_data(self):
        self.store.add_organization(Organization(id="org1", name="Test Org"))
        self.store.add_team(Team(id="t1", name="Team 1", program_id=None))

    def _preview(self, actor_id=None):
        return self.api.factory_reset.preview(actor_id or self.admin.id)

    def _execute(self, actor_id=None, password="hunter22",
                phrase=CONFIRMATION_PHRASE, token=None, backup=True,
                environment="production"):
        return self.api.factory_reset.execute(
            actor_id or self.admin.id, password, phrase, token, backup,
            environment=environment)

    # -- preview --------------------------------------------------------
    def test_preview_returns_counts_and_challenge(self):
        self._add_some_data()
        prev = self._preview()
        self.assertEqual(prev["counts"].get("organizations"), 1)
        self.assertIn("challenge_token", prev)
        self.assertIn("expires_at", prev)

    def test_preview_requires_admin_role(self):
        from hockey_scheduler.domain.errors import NotAuthorizedError
        with self.assertRaises(NotAuthorizedError):
            self.api.factory_reset.preview(self.coach.id)

    def test_preview_rejects_unknown_actor(self):
        from hockey_scheduler.domain.errors import NotAuthorizedError
        with self.assertRaises(NotAuthorizedError):
            self.api.factory_reset.preview("nope")

    def test_preview_counts_match_row_counts_definition(self):
        self._add_some_data()
        prev = self._preview()
        self.assertEqual(prev["counts"], self.store.row_counts())

    # -- rejection paths perform zero writes -----------------------------
    def test_execute_wrong_password_zero_writes(self):
        from hockey_scheduler.domain.errors import NotAuthorizedError
        self._add_some_data()
        token = self._preview()["challenge_token"]
        with self.assertRaises(NotAuthorizedError):
            self._execute(password="wrong", token=token)
        self.assertEqual(len(self.store.all_organizations()), 1)
        self.assertEqual(len(self.store.all_factory_reset_events()), 0)

    def test_execute_wrong_phrase_zero_writes(self):
        from hockey_scheduler.domain.errors import ValidationError
        self._add_some_data()
        token = self._preview()["challenge_token"]
        with self.assertRaises(ValidationError) as cm:
            self._execute(phrase="not the phrase", token=token)
        self.assertEqual(cm.exception.details["reason"], "phrase_mismatch")
        self.assertEqual(len(self.store.all_organizations()), 1)
        self.assertEqual(len(self.store.all_factory_reset_events()), 0)

    def test_execute_missing_backup_ack_zero_writes(self):
        from hockey_scheduler.domain.errors import ValidationError
        self._add_some_data()
        token = self._preview()["challenge_token"]
        with self.assertRaises(ValidationError) as cm:
            self._execute(token=token, backup=False)
        self.assertEqual(cm.exception.details["reason"], "backup_not_acknowledged")
        self.assertEqual(len(self.store.all_organizations()), 1)
        self.assertEqual(len(self.store.all_factory_reset_events()), 0)

    def test_execute_missing_challenge_zero_writes(self):
        from hockey_scheduler.domain.errors import ValidationError
        self._add_some_data()
        with self.assertRaises(ValidationError) as cm:
            self._execute(token=None)
        self.assertEqual(cm.exception.details["reason"], "invalid_challenge")
        self.assertEqual(len(self.store.all_organizations()), 1)
        self.assertEqual(len(self.store.all_factory_reset_events()), 0)

    def test_execute_garbage_challenge_zero_writes(self):
        from hockey_scheduler.domain.errors import ValidationError
        self._add_some_data()
        self._preview()  # sets an outstanding challenge, but we send junk
        with self.assertRaises(ValidationError) as cm:
            self._execute(token="totally-wrong-token")
        self.assertEqual(cm.exception.details["reason"], "invalid_challenge")
        self.assertEqual(len(self.store.all_organizations()), 1)

    def test_execute_challenge_bound_to_actor(self):
        from hockey_scheduler.domain.errors import ValidationError
        self._add_some_data()
        second_admin = self.api.accounts.create_account(
            "boss2", "hunter33", Role.LEAGUE_ADMIN)
        token = self._preview(actor_id=self.admin.id)["challenge_token"]
        with self.assertRaises(ValidationError) as cm:
            self._execute(actor_id=second_admin.id, password="hunter33",
                          token=token)
        self.assertEqual(cm.exception.details["reason"], "invalid_challenge")

    def test_execute_stale_challenge_rejected(self):
        from hockey_scheduler.domain.errors import ValidationError
        self._add_some_data()
        clock = [self.api.roster.clock()]
        self.api.factory_reset.clock = lambda: clock[0]
        token = self._preview()["challenge_token"]
        clock[0] = clock[0] + timedelta(seconds=999)
        with self.assertRaises(ValidationError) as cm:
            self._execute(token=token)
        self.assertEqual(cm.exception.details["reason"], "invalid_challenge")

    def test_execute_wrong_password_leaves_challenge_valid_for_retry(self):
        # A rejection before challenge validation (wrong password, phrase, or
        # missing backup ack) must not burn the challenge — an operator who
        # mistypes one field should be able to retry immediately rather than
        # fetch a whole new preview.
        from hockey_scheduler.domain.errors import NotAuthorizedError
        self._add_some_data()
        token = self._preview()["challenge_token"]
        with self.assertRaises(NotAuthorizedError):
            self._execute(password="wrong", token=token)
        result = self._execute(token=token)  # retried with the same token
        self.assertEqual(result["result"], "success")

    def test_execute_reused_challenge_rejected(self):
        from hockey_scheduler.domain.errors import ValidationError
        self._add_some_data()
        token = self._preview()["challenge_token"]
        result = self._execute(token=token)  # consumes the challenge
        self.assertEqual(result["result"], "success")
        with self.assertRaises(ValidationError) as cm:
            self._execute(token=token)
        self.assertEqual(cm.exception.details["reason"], "invalid_challenge")

    def test_execute_second_reset_while_in_progress_rejected(self):
        from hockey_scheduler.domain.errors import ValidationError
        from hockey_scheduler.domain import FactoryResetLock
        self._add_some_data()
        self.store.acquire_factory_reset_lock(FactoryResetLock(
            id="singleton", actor_id="someone-else",
            acquired_at=self.api.roster.clock()))
        try:
            token = self._preview()["challenge_token"]
            with self.assertRaises(ValidationError) as cm:
                self._execute(token=token)
            self.assertEqual(cm.exception.details["reason"], "reset_in_progress")
        finally:
            self.store.release_factory_reset_lock()

    def test_execute_lock_is_store_backed_across_two_service_instances(self):
        # #256 review blocker 5: the single-in-flight guard must hold across
        # two separate FactoryResetService instances sharing one store — the
        # process-equivalent of two server workers, not just one Python
        # object's own threading.Lock.
        from hockey_scheduler.domain.errors import ValidationError
        from hockey_scheduler.services.factory_reset_service import FactoryResetService
        self._add_some_data()
        other_service = FactoryResetService(self.store, self.api.accounts,
                                            self.api.roster.clock)
        token = other_service.preview(self.admin.id)["challenge_token"]
        from hockey_scheduler.domain import FactoryResetLock
        held = self.store.acquire_factory_reset_lock(FactoryResetLock(
            id="singleton", actor_id=self.admin.id,
            acquired_at=self.api.roster.clock()))
        self.assertTrue(held)
        try:
            with self.assertRaises(ValidationError) as cm:
                self.api.factory_reset.execute(
                    self.admin.id, "hunter22", CONFIRMATION_PHRASE, token, True)
            self.assertEqual(cm.exception.details["reason"], "reset_in_progress")
        finally:
            self.store.release_factory_reset_lock()

    def test_preview_and_execute_across_two_service_instances(self):
        # Same cross-process-equivalence concern as above, for the
        # challenge: preview() on one instance, execute() on another,
        # sharing only the store.
        from hockey_scheduler.services.factory_reset_service import FactoryResetService
        self._add_some_data()
        service_a = FactoryResetService(self.store, self.api.accounts,
                                        self.api.roster.clock)
        service_b = FactoryResetService(self.store, self.api.accounts,
                                        self.api.roster.clock)
        token = service_a.preview(self.admin.id)["challenge_token"]
        result = service_b.execute(
            self.admin.id, "hunter22", CONFIRMATION_PHRASE, token, True)
        self.assertEqual(result["result"], "success")

    # -- successful reset -------------------------------------------------
    def test_execute_success_wipes_domains_preserves_one_admin(self):
        self._add_some_data()
        token = self._preview()["challenge_token"]
        result = self._execute(token=token)
        self.assertEqual(result["result"], "success")
        self.assertEqual(len(self.store.all_organizations()), 0)
        self.assertEqual(len(self.store.all_teams()), 0)
        accounts = self.store.all_user_accounts()
        self.assertEqual(len(accounts), 1)
        self.assertEqual(accounts[0].id, self.admin.id)
        self.assertEqual(accounts[0].username, "boss")
        self.assertTrue(accounts[0].active)
        self.assertEqual(result["preserved_account_id"], self.admin.id)

    def test_execute_success_installation_state_present(self):
        token = self._preview()["challenge_token"]
        self._execute(token=token)
        state = self.store.get_installation_state("primary")
        self.assertIsNotNone(state)
        self.assertEqual(state.claimed_by_user_id, self.admin.id)

    def test_execute_success_preserved_admin_can_log_in(self):
        token = self._preview()["challenge_token"]
        self._execute(token=token)
        verified = self.api.accounts.verify_login("boss", "hunter22")
        self.assertIsNotNone(verified)
        self.assertEqual(verified.id, self.admin.id)

    def test_execute_success_all_sessions_revoked(self):
        from hockey_scheduler.web.auth import SessionManager
        sm = SessionManager()
        sm.login(self.store, self.admin.id)
        self.assertEqual(len(self.store.sessions_for_user(self.admin.id)), 1)
        token = self._preview()["challenge_token"]
        self._execute(token=token)
        self.assertEqual(len(self.store.all_user_accounts()), 1)
        # Every session row (including the one just issued) is gone — the
        # preserved admin's own prior session does not survive either.
        remaining = [s for s in self.store.sessions_for_user(self.admin.id)]
        self.assertEqual(remaining, [])

    def test_execute_success_writes_durable_event(self):
        self._add_some_data()
        prev = self._preview()
        result = self._execute(token=prev["challenge_token"])
        events = self.store.all_factory_reset_events()
        self.assertEqual(len(events), 1)
        event = events[0]
        self.assertEqual(event.id, result["event_id"])
        self.assertEqual(event.actor_id, self.admin.id)
        self.assertEqual(event.result, "success")
        self.assertIsNone(event.failure_reason)
        self.assertEqual(event.pre_reset_counts, prev["counts"])
        self.assertIsNotNone(event.completed_at)

    def test_execute_durable_event_survives_being_the_only_row_left(self):
        token = self._preview()["challenge_token"]
        self._execute(token=token)
        # The event row itself must never be swept by its own wipe, nor by a
        # second, unrelated call to clear_all_data().
        self.store.clear_all_data()
        self.assertEqual(len(self.store.all_factory_reset_events()), 1)

    def test_forced_failure_rolls_back_completely_and_records_failed_event(self):
        self._add_some_data()
        token = self._preview()["challenge_token"]
        orig = self.store.clear_all_data

        def boom():
            orig()
            raise RuntimeError("forced failure")

        self.store.clear_all_data = boom
        try:
            with self.assertRaises(RuntimeError):
                self._execute(token=token)
        finally:
            self.store.clear_all_data = orig
        self.assertEqual(len(self.store.all_organizations()), 1)
        self.assertEqual(len(self.store.all_teams()), 1)
        accounts = self.store.all_user_accounts()
        self.assertEqual(len(accounts), 2)  # boss + coach1, untouched
        events = self.store.all_factory_reset_events()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].result, "failed")
        self.assertEqual(events[0].failure_reason, "RuntimeError")

    # -- PR #264 review fixes --------------------------------------------
    def test_execute_rejects_stale_preview_snapshot(self):
        # #256 review blocker 1: data created after preview but before
        # execute must never be silently swept into the wipe the operator
        # never saw or confirmed.
        from hockey_scheduler.domain.errors import ValidationError
        self._add_some_data()
        token = self._preview()["challenge_token"]
        self.store.add_organization(Organization(id="org2", name="Sneaked In"))
        with self.assertRaises(ValidationError) as cm:
            self._execute(token=token)
        self.assertEqual(cm.exception.details["reason"], "preview_stale")
        # Zero writes from the rejection itself — both orgs still present,
        # nothing wiped, no event logged for a pre-flight rejection.
        self.assertEqual(len(self.store.all_organizations()), 2)
        self.assertEqual(len(self.store.all_factory_reset_events()), 0)

    def test_execute_succeeds_when_snapshot_unchanged(self):
        self._add_some_data()
        token = self._preview()["challenge_token"]
        result = self._execute(token=token)
        self.assertEqual(result["result"], "success")

    def test_event_insert_failure_rolls_back_wipe_and_records_failed_event(self):
        # #256 review blocker 2: the durable success event is written
        # inside the same transaction as the wipe. If that insert itself
        # fails, the whole wipe must roll back — not leave production data
        # gone with no surviving record.
        self._add_some_data()
        token = self._preview()["challenge_token"]
        orig = self.store.add_factory_reset_event
        calls = {"n": 0}

        def flaky(event):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("event insert failed")
            return orig(event)

        self.store.add_factory_reset_event = flaky
        try:
            with self.assertRaises(RuntimeError):
                self._execute(token=token)
        finally:
            self.store.add_factory_reset_event = orig
        # The wipe rolled back completely, including the admin re-insert
        # attempted inside the same transaction.
        self.assertEqual(len(self.store.all_organizations()), 1)
        self.assertEqual(len(self.store.all_teams()), 1)
        self.assertEqual(len(self.store.all_user_accounts()), 2)
        # Exactly one durable event survives: the "failed" record written
        # in a fresh operation after the rollback completed.
        events = self.store.all_factory_reset_events()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].result, "failed")

    def test_execute_rejects_non_boolean_backup_acknowledgement(self):
        # #256 review blocker 3: a truthy-but-not-`True` JSON value must
        # never count as acknowledgement. A rejection here happens before
        # challenge consumption (same as wrong password/phrase), so the same
        # preview/token is reused across every value in the loop.
        from hockey_scheduler.domain.errors import ValidationError
        self._add_some_data()
        token = self._preview()["challenge_token"]
        for bad_value in ("false", "no", "0", 1, 0, None, "true"):
            with self.subTest(bad_value=bad_value):
                with self.assertRaises(ValidationError) as cm:
                    self._execute(token=token, backup=bad_value)
                self.assertEqual(
                    cm.exception.details["reason"], "backup_not_acknowledged")
                self.assertEqual(len(self.store.all_organizations()), 1)

    def test_execute_requires_exact_league_admin_role(self):
        # #256 review blocker 4: the role itself is checked explicitly, not
        # only the two permissions — even if a future permission-matrix
        # change granted both to a different role, that role must still be
        # refused. Simulated here by temporarily granting Arena Manager
        # both permissions and confirming the explicit role check still
        # blocks it.
        from hockey_scheduler.domain.errors import NotAuthorizedError
        from hockey_scheduler.domain.roles import Permission, ROLE_PERMISSIONS, Role
        arena = self.api.accounts.create_account(
            "arena1", "arenapass", Role.ARENA_MANAGER)
        original = ROLE_PERMISSIONS[Role.ARENA_MANAGER]
        ROLE_PERMISSIONS[Role.ARENA_MANAGER] = original | {
            Permission.MANAGE_SETUP, Permission.MANAGE_USERS}
        try:
            with self.assertRaises(NotAuthorizedError) as cm:
                self.api.factory_reset.preview(arena.id)
            self.assertEqual(
                cm.exception.details["reason"], "insufficient_permission")
        finally:
            ROLE_PERMISSIONS[Role.ARENA_MANAGER] = original


class MemoryFactoryResetTest(FactoryResetContract, unittest.TestCase):
    def make_store(self):
        return InMemoryStore()


class DurableFactoryResetTest(FactoryResetContract, unittest.TestCase):
    def make_store(self):
        url = os.environ.get("TEST_DATABASE_URL") or ":memory:"
        store = SqlStore(url)
        store.reset_schema()
        return store


class FactoryResetHttpTest(unittest.TestCase):
    """Drives the real HTTP routes with APP_MODE=production."""

    @classmethod
    def setUpClass(cls):
        cls._env_backup = {
            k: os.environ.get(k) for k in
            ("APP_MODE", "ALLOW_PRODUCTION_FACTORY_RESET",
             "BOOTSTRAP_ADMIN_USER", "BOOTSTRAP_ADMIN_PASSWORD",
             "DATABASE_URL")
        }
        os.environ["APP_MODE"] = "production"
        os.environ["ALLOW_PRODUCTION_FACTORY_RESET"] = "true"
        os.environ["BOOTSTRAP_ADMIN_USER"] = "boss"
        os.environ["BOOTSTRAP_ADMIN_PASSWORD"] = "hunter22"
        os.environ.pop("DATABASE_URL", None)
        srv.STATE.reset()
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), srv.Handler)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.thread.join(timeout=5)
        for k, v in cls._env_backup.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def setUp(self):
        srv.STATE.reset()

    def _req(self, method, path, body=None, cookie=None):
        url = f"http://127.0.0.1:{self.port}{path}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        if cookie:
            req.add_header("Cookie", cookie)
        try:
            with urllib.request.urlopen(req) as r:
                return (r.status, json.loads(r.read() or b"{}"),
                        r.headers.get("Set-Cookie"))
        except urllib.error.HTTPError as e:
            return (e.code, json.loads(e.read() or b"{}"),
                    e.headers.get("Set-Cookie"))

    def _login(self, username="boss", password="hunter22"):
        status, _, sc = self._req(
            "POST", "/api/auth/login",
            {"username": username, "password": password})
        self.assertEqual(status, 200)
        return cookie_from_set_cookie(sc, "hs_sid")

    def test_unauthenticated_is_401(self):
        status, body, _ = self._req("POST", "/api/admin/factory-reset/preview", {})
        self.assertEqual(status, 401)

    def test_non_admin_role_is_403(self):
        srv.STATE.api.accounts.create_account("coach1", "coachpass", "coach")
        cookie = self._login("coach1", "coachpass")
        status, body, _ = self._req(
            "POST", "/api/admin/factory-reset/preview", {}, cookie=cookie)
        self.assertEqual(status, 403)
        self.assertEqual(body["error"]["code"], "forbidden")

    def test_flag_disabled_is_403(self):
        cookie = self._login()
        os.environ["ALLOW_PRODUCTION_FACTORY_RESET"] = "false"
        try:
            status, body, _ = self._req(
                "POST", "/api/admin/factory-reset/preview", {}, cookie=cookie)
        finally:
            os.environ["ALLOW_PRODUCTION_FACTORY_RESET"] = "true"
        self.assertEqual(status, 403)
        self.assertEqual(body["error"]["code"], "factory_reset_not_available")

    def test_full_flow_success_invalidates_session(self):
        cookie = self._login()
        status, body, _ = self._req(
            "POST", "/api/admin/factory-reset/preview", {}, cookie=cookie)
        self.assertEqual(status, 200)
        self.assertIn("challenge_token", body)
        token = body["challenge_token"]

        status, body, _ = self._req(
            "POST", "/api/admin/factory-reset/execute",
            {"password": "wrong", "typed_phrase": CONFIRMATION_PHRASE,
             "challenge_token": token, "backup_acknowledged": True},
            cookie=cookie)
        self.assertEqual(status, 403)

        status, body, _ = self._req(
            "POST", "/api/admin/factory-reset/preview", {}, cookie=cookie)
        token = body["challenge_token"]
        status, body, sc = self._req(
            "POST", "/api/admin/factory-reset/execute",
            {"password": "hunter22", "typed_phrase": CONFIRMATION_PHRASE,
             "challenge_token": token, "backup_acknowledged": True},
            cookie=cookie)
        self.assertEqual(status, 200)
        self.assertEqual(body["result"], "success")

        # The stale cookie no longer resolves to a live session.
        status, body, _ = self._req(
            "POST", "/api/admin/factory-reset/preview", {}, cookie=cookie)
        self.assertEqual(status, 401)

    def test_invalid_typed_phrase_is_rejected(self):
        cookie = self._login()
        status, body, _ = self._req(
            "POST", "/api/admin/factory-reset/preview", {}, cookie=cookie)
        token = body["challenge_token"]
        status, body, _ = self._req(
            "POST", "/api/admin/factory-reset/execute",
            {"password": "hunter22", "typed_phrase": "not it",
             "challenge_token": token, "backup_acknowledged": True},
            cookie=cookie)
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["details"]["reason"], "phrase_mismatch")

    def test_non_boolean_backup_acknowledged_rejected_over_http(self):
        # #256 review blocker 3: JSON string/number values must not coerce
        # to acknowledged, and rejecting them performs zero writes. A single
        # preview/token is reused for both values (a rejection here happens
        # before challenge consumption) to stay well under the route's rate
        # limit (5 requests / 300s) within one test.
        cookie = self._login()
        before = srv.STATE.api.store.row_counts()
        status, body, _ = self._req(
            "POST", "/api/admin/factory-reset/preview", {}, cookie=cookie)
        token = body["challenge_token"]
        for bad_value in ("false", 1):
            with self.subTest(bad_value=bad_value):
                status, body, _ = self._req(
                    "POST", "/api/admin/factory-reset/execute",
                    {"password": "hunter22", "typed_phrase": CONFIRMATION_PHRASE,
                     "challenge_token": token, "backup_acknowledged": bad_value},
                    cookie=cookie)
                self.assertEqual(status, 400, (bad_value, body))
                self.assertEqual(
                    body["error"]["details"]["reason"], "backup_not_acknowledged")
        self.assertEqual(srv.STATE.api.store.row_counts(), before)


if __name__ == "__main__":
    unittest.main()
