"""Coach-scope fail-closed regression suite (#266).

The expert review (finding F-1) found that a Coach account created without a
valid ``scope.team_id`` was treated as *unbound* and therefore NOT
resource-scoped — so it could mutate any team's roster — and that the
account-creation path silently dropped a misplaced top-level ``team_id`` into an
empty scope. This suite pins the fail-closed behavior:

* the service refuses to create/reactivate a Coach without a real team, on
  Memory, SQLite and (when ``TEST_DATABASE_URL`` is set) PostgreSQL;
* the HTTP layer rejects unknown top-level account fields;
* an existing unscoped Coach (a pre-fix account, simulated by a direct store
  insert) is refused 403 on every roster mutation and private game read;
* the deployment readiness endpoint surfaces unscoped active coaches instead of
  silently grandfathering them.
"""

import json
import os
import threading
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timezone
from http.cookiejar import CookieJar
from http.server import ThreadingHTTPServer

from helpers import BACKEND  # noqa: F401  (ensures sys.path is set up)

from hockey_scheduler.api import ApiService
from hockey_scheduler.domain import (
    Official, Player, Position, Role, Team, UserAccount)
from hockey_scheduler.domain.errors import ValidationError
from hockey_scheduler.services.account_service import AccountService
from hockey_scheduler.services.passwords import hash_password
from hockey_scheduler.store import InMemoryStore, SqlStore
from hockey_scheduler.web import server as srv


def _clock():
    return datetime(2026, 7, 16, tzinfo=timezone.utc)


# --------------------------------------------------------------------------- #
# Service layer, run against every backend (criteria 1 and 5).                 #
# --------------------------------------------------------------------------- #
class CoachScopeContract:
    """Backend-agnostic service assertions. Subclasses provide ``make_store``."""

    def setUp(self):
        self.store = self.make_store()
        self.accounts = AccountService(self.store, _clock)
        self.store.add_team(Team(id="team_home", name="Lions"))

    # -- create --------------------------------------------------------------
    def test_create_coach_without_team_scope_rejected(self):
        with self.assertRaises(ValidationError) as cm:
            self.accounts.create_account("c1", "pw", Role.COACH, scope={})
        self.assertEqual(cm.exception.details.get("reason"), "scope_required")
        # No account and no audit row were written (the method is transactional).
        self.assertEqual(self.store.all_user_accounts(), [])
        self.assertEqual(
            [a for a in self.store.all_setup_audit()
             if a.action == "user_account_created"], [])

    def test_create_coach_with_nonexistent_team_rejected(self):
        with self.assertRaises(ValidationError) as cm:
            self.accounts.create_account(
                "c2", "pw", Role.COACH, scope={"team_id": "team_ghost"})
        self.assertEqual(cm.exception.details.get("reason"),
                         "scope_subject_missing")
        self.assertEqual(self.store.all_user_accounts(), [])

    def test_create_coach_with_valid_team_succeeds(self):
        acct = self.accounts.create_account(
            "c3", "pw", Role.COACH, scope={"team_id": "team_home"})
        self.assertEqual(acct.role, Role.COACH)
        self.assertEqual(acct.scope, {"team_id": "team_home"})
        self.assertTrue(acct.active)

    def test_non_coach_roles_need_no_team(self):
        # A viewer/admin carries no team scope and must still create cleanly —
        # the requirement is coach-specific.
        self.assertIsNotNone(
            self.accounts.create_account("v1", "pw", Role.VIEWER))

    # -- reactivate ----------------------------------------------------------
    def _insert_inactive_coach(self, username, scope):
        acct = UserAccount(
            id=f"user_{username}", username=username,
            password_hash=hash_password("pw"), role=Role.COACH,
            created_at=_clock(), scope=dict(scope), active=False)
        self.store.add_user_account(acct)
        return acct

    def test_reactivate_unscoped_coach_rejected(self):
        acct = self._insert_inactive_coach("legacy1", {})
        with self.assertRaises(ValidationError) as cm:
            self.accounts.set_active(acct.id, True)
        self.assertEqual(cm.exception.details.get("reason"), "scope_required")
        self.assertFalse(self.store.get_user_account(acct.id).active)

    def test_reactivate_coach_with_dangling_team_rejected(self):
        acct = self._insert_inactive_coach("legacy2", {"team_id": "team_ghost"})
        with self.assertRaises(ValidationError) as cm:
            self.accounts.set_active(acct.id, True)
        self.assertEqual(cm.exception.details.get("reason"),
                         "scope_subject_missing")
        self.assertFalse(self.store.get_user_account(acct.id).active)

    def test_reactivate_coach_with_valid_team_succeeds(self):
        acct = self._insert_inactive_coach("legacy3", {"team_id": "team_home"})
        reactivated = self.accounts.set_active(acct.id, True)
        self.assertTrue(reactivated.active)

    # -- malformed scope shape (#266 review blocker 3) -----------------------
    def test_create_coach_with_non_dict_scope_is_stable_validation_error(self):
        # A string/array scope must surface as a domain ValidationError (stable
        # 400), never a raw ValueError/TypeError from dict(scope) (a 500).
        with self.assertRaises(ValidationError) as cm:
            self.accounts.create_account("c", "pw", Role.COACH, scope="team_home")
        self.assertEqual(cm.exception.details.get("reason"), "invalid_scope")
        self.assertEqual(self.store.all_user_accounts(), [])

    # -- per-role scope-key allowlist (#266 review) --------------------------
    def test_coach_scope_rejects_extra_key(self):
        with self.assertRaises(ValidationError) as cm:
            self.accounts.create_account(
                "c", "pw", Role.COACH,
                scope={"team_id": "team_home", "player_id": "p1"})
        self.assertEqual(cm.exception.details.get("reason"), "unknown_scope_key")
        self.assertEqual(cm.exception.details.get("keys"), ["player_id"])
        self.assertEqual(self.store.all_user_accounts(), [])

    def test_official_scope_rejects_team_id(self):
        self.store.add_official(Official(id="off1", name="Ref One"))
        with self.assertRaises(ValidationError) as cm:
            self.accounts.create_account(
                "o", "pw", Role.OFFICIAL,
                scope={"official_id": "off1", "team_id": "team_home"})
        self.assertEqual(cm.exception.details.get("reason"), "unknown_scope_key")

    def test_admin_rejects_any_scope_key(self):
        with self.assertRaises(ValidationError) as cm:
            self.accounts.create_account(
                "a", "pw", Role.LEAGUE_ADMIN, scope={"team_id": "team_home"})
        self.assertEqual(cm.exception.details.get("reason"), "unknown_scope_key")

    def test_player_scope_allows_player_id_and_team_id(self):
        self.store.add_player(Player(id="p_ok", team_id="team_home", name="P",
                                     position=Position.FORWARD))
        acct = self.accounts.create_account(
            "p", "pw", Role.PLAYER,
            scope={"player_id": "p_ok", "team_id": "team_home"})
        self.assertEqual(acct.scope, {"player_id": "p_ok", "team_id": "team_home"})

    # -- player team scope must be the player's OWN team (#266 review) --------
    def test_create_player_with_foreign_team_rejected(self):
        self.store.add_team(Team(id="team_other", name="Bears"))
        self.store.add_player(Player(id="pA", team_id="team_home", name="A",
                                     position=Position.FORWARD))
        with self.assertRaises(ValidationError) as cm:
            self.accounts.create_account(
                "p", "pw", Role.PLAYER,
                scope={"player_id": "pA", "team_id": "team_other"})
        self.assertEqual(cm.exception.details.get("reason"), "scope_team_mismatch")
        self.assertEqual(self.store.all_user_accounts(), [])

    def test_create_player_team_only_scope_rejected(self):
        # A team_id with no player_id has no ownership to check (#266 review),
        # so it can't be trusted — rejected, zero writes.
        with self.assertRaises(ValidationError) as cm:
            self.accounts.create_account(
                "p", "pw", Role.PLAYER, scope={"team_id": "team_home"})
        self.assertEqual(cm.exception.details.get("reason"), "scope_required")
        self.assertEqual(self.store.all_user_accounts(), [])

    def test_rebind_player_to_team_only_scope_rejected_zero_audit(self):
        self.store.add_player(Player(id="pC", team_id="team_home", name="C",
                                     position=Position.FORWARD))
        acct = UserAccount(
            id="user_pC", username="playerc", password_hash=hash_password("pw"),
            role=Role.PLAYER, created_at=_clock(),
            scope={"player_id": "pC", "team_id": "team_home"}, active=True)
        self.store.add_user_account(acct)
        with self.assertRaises(ValidationError) as cm:
            self.accounts.rebind_account_scope(acct.id, {"team_id": "team_home"})
        self.assertEqual(cm.exception.details.get("reason"), "scope_required")
        self.assertEqual(self.store.get_user_account(acct.id).scope,
                         {"player_id": "pC", "team_id": "team_home"})
        self.assertEqual(self._scope_change_audits(), [])

    def test_rebind_player_to_foreign_team_rejected_zero_audit(self):
        self.store.add_team(Team(id="team_other", name="Bears"))
        self.store.add_player(Player(id="pB", team_id="team_home", name="B",
                                     position=Position.FORWARD))
        acct = UserAccount(
            id="user_pB", username="playerb", password_hash=hash_password("pw"),
            role=Role.PLAYER, created_at=_clock(),
            scope={"player_id": "pB", "team_id": "team_home"}, active=True)
        self.store.add_user_account(acct)
        with self.assertRaises(ValidationError) as cm:
            self.accounts.rebind_account_scope(
                acct.id, {"player_id": "pB", "team_id": "team_other"})
        self.assertEqual(cm.exception.details.get("reason"), "scope_team_mismatch")
        # Unchanged scope and no audit row.
        self.assertEqual(self.store.get_user_account(acct.id).scope,
                         {"player_id": "pB", "team_id": "team_home"})
        self.assertEqual(self._scope_change_audits(), [])

    # -- audit detail is sanitized (#266 review) -----------------------------
    def _created_audits(self):
        return [a for a in self.store.all_setup_audit()
                if a.action == "user_account_created"]

    def test_creation_audit_records_only_sanitized_scope(self):
        self.accounts.create_account("c", "pw", Role.COACH,
                                     scope={"team_id": "team_home"})
        audits = self._created_audits()
        self.assertEqual(len(audits), 1)
        self.assertEqual(audits[0].detail["scope"], {"team_id": "team_home"})

    def test_rebind_audit_sanitizes_legacy_from_scope(self):
        # A pre-fix account whose stored scope carries unsupported keys (a
        # coach with a stray key) must not leak that data into the audit's
        # `from` — only the role's supported keys are recorded.
        acct = self._insert_inactive_coach(
            "legacy_dirty", {"team_id": "team_home", "roster_secret": "leak"})
        self.accounts.rebind_account_scope(acct.id, {"team_id": "team_home"})
        audits = self._scope_change_audits()
        self.assertEqual(len(audits), 1)
        self.assertEqual(audits[0].detail["from"], {"team_id": "team_home"})
        self.assertNotIn("roster_secret", audits[0].detail["from"])

    def test_rebind_rolls_back_when_audit_fails(self):
        acct = self._insert_inactive_coach("legacy_rbatomic", {})
        original = self.store.add_setup_audit

        def boom(_entry):
            raise RuntimeError("audit sink down")

        self.store.add_setup_audit = boom
        try:
            with self.assertRaises(RuntimeError):
                self.accounts.rebind_account_scope(acct.id, {"team_id": "team_home"})
        finally:
            self.store.add_setup_audit = original
        # The scope update did NOT persist — the transaction rolled back.
        self.assertEqual(self.store.get_user_account(acct.id).scope, {})

    # -- audited scope rebind remediation (#266 review blocker 4) ------------
    def _scope_change_audits(self):
        return [a for a in self.store.all_setup_audit()
                if a.action == "user_account_scope_changed"]

    def test_rebind_unscoped_coach_to_team_is_audited(self):
        acct = self._insert_inactive_coach("legacy_rb", {})
        rebound = self.accounts.rebind_account_scope(
            acct.id, {"team_id": "team_home"}, actor_id="admin")
        self.assertEqual(rebound.scope, {"team_id": "team_home"})
        audits = self._scope_change_audits()
        self.assertEqual(len(audits), 1)
        self.assertEqual(audits[0].detail["to"], {"team_id": "team_home"})
        self.assertEqual(audits[0].actor_id, "admin")

    def test_rebind_to_nonexistent_team_rejected(self):
        acct = self._insert_inactive_coach("legacy_rb2", {})
        with self.assertRaises(ValidationError) as cm:
            self.accounts.rebind_account_scope(acct.id, {"team_id": "team_ghost"})
        self.assertEqual(cm.exception.details.get("reason"),
                         "scope_subject_missing")
        self.assertEqual(self._scope_change_audits(), [])

    def test_rebind_non_dict_scope_rejected(self):
        acct = self._insert_inactive_coach("legacy_rb3", {})
        with self.assertRaises(ValidationError) as cm:
            self.accounts.rebind_account_scope(acct.id, "team_home")
        self.assertEqual(cm.exception.details.get("reason"), "invalid_scope")

    def test_rebind_then_reactivate_repairs_legacy_coach(self):
        # The end-to-end remediation the readiness check points at: a legacy
        # unscoped coach can't be reactivated, but after a rebind to a real team
        # it can.
        acct = self._insert_inactive_coach("legacy_rb4", {})
        with self.assertRaises(ValidationError):
            self.accounts.set_active(acct.id, True)
        self.accounts.rebind_account_scope(acct.id, {"team_id": "team_home"})
        reactivated = self.accounts.set_active(acct.id, True)
        self.assertTrue(reactivated.active)

    # -- set_active atomicity (#266 review blocker 2) ------------------------
    def test_set_active_rolls_back_when_audit_fails(self):
        # The account save and its audit row must commit together: if the audit
        # write fails, the activation must roll back rather than persist
        # unaudited.
        acct = self._insert_inactive_coach("legacy_atomic", {"team_id": "team_home"})
        original = self.store.add_setup_audit

        def boom(_entry):
            raise RuntimeError("audit sink down")

        self.store.add_setup_audit = boom
        try:
            with self.assertRaises(RuntimeError):
                self.accounts.set_active(acct.id, True)
        finally:
            self.store.add_setup_audit = original
        # The activation did NOT persist — the transaction rolled back.
        self.assertFalse(self.store.get_user_account(acct.id).active)


class MemoryCoachScopeTest(CoachScopeContract, unittest.TestCase):
    def make_store(self):
        return InMemoryStore()


class DurableCoachScopeTest(CoachScopeContract, unittest.TestCase):
    def make_store(self):
        return SqlStore(":memory:")


@unittest.skipUnless(os.environ.get("TEST_DATABASE_URL"),
                     "PostgreSQL required (set TEST_DATABASE_URL)")
class PostgresCoachScopeTest(CoachScopeContract, unittest.TestCase):
    def make_store(self):
        store = SqlStore(os.environ["TEST_DATABASE_URL"])
        store.clear_all_data()  # isolate from any prior run's rows
        return store


# --------------------------------------------------------------------------- #
# HTTP layer + live scope gate (criteria 1, 2, 3).                             #
# --------------------------------------------------------------------------- #
class CoachScopeHttpTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        srv.STATE.reset()
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), srv.Handler)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()
        cls.gid = srv.STATE.game_id
        cls.home = srv.STATE.ids["home_team_id"]
        cls.away = srv.STATE.ids["away_team_id"]

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.thread.join(timeout=5)

    def _client(self):
        return urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(CookieJar()))

    def _req(self, opener, method, path, body=None):
        url = f"http://127.0.0.1:{self.port}{path}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            url, data=data, method=method,
            headers={"Content-Type": "application/json"})
        try:
            with opener.open(req) as r:
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def _admin(self):
        c = self._client()
        self._req(c, "POST", "/api/auth/login",
                  {"username": "admin", "password": "demo"})
        return c

    def test_create_coach_without_team_is_rejected(self):
        admin = self._admin()
        status, body = self._req(admin, "POST", "/api/accounts",
                                 {"username": "coach_noteam", "password": "pw",
                                  "role": "coach", "scope": {}})
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "validation_error")
        self.assertEqual(body["error"]["details"].get("reason"), "scope_required")

    def test_create_coach_with_toplevel_team_id_is_unknown_field(self):
        admin = self._admin()
        status, body = self._req(admin, "POST", "/api/accounts",
                                 {"username": "coach_misplaced", "password": "pw",
                                  "role": "coach", "team_id": self.home})
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "validation_error")
        self.assertEqual(body["error"]["details"].get("reason"), "unknown_field")
        self.assertIn("team_id", body["error"]["details"]["fields"])
        # Nothing was created.
        self.assertIsNone(
            srv.STATE.api.store.get_user_account_by_username("coach_misplaced"))

    def test_create_coach_with_valid_team_succeeds(self):
        admin = self._admin()
        status, body = self._req(admin, "POST", "/api/accounts",
                                 {"username": "coach_ok", "password": "pw",
                                  "role": "coach", "scope": {"team_id": self.home}})
        self.assertEqual(status, 200)
        self.assertEqual(body["scope"], {"team_id": self.home})

    def test_create_coach_with_unknown_scope_key_is_400_zero_writes(self):
        # A disallowed nested scope key is rejected with a stable 400 and writes
        # nothing — not the account, not an audit row.
        admin = self._admin()
        store = srv.STATE.api.store
        audits_before = len(store.all_setup_audit())
        status, body = self._req(
            admin, "POST", "/api/accounts",
            {"username": "coach_badscope", "password": "pw", "role": "coach",
             "scope": {"team_id": self.home, "roster_secret": "leak"}})
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "validation_error")
        self.assertEqual(body["error"]["details"].get("reason"), "unknown_scope_key")
        self.assertIsNone(store.get_user_account_by_username("coach_badscope"))
        self.assertEqual(len(store.all_setup_audit()), audits_before)

    def _insert_unscoped_coach(self, username):
        # Simulate a pre-fix legacy account: an ACTIVE coach with no team scope,
        # inserted directly (the service now refuses to create one).
        store = srv.STATE.api.store
        store.add_user_account(UserAccount(
            id=f"user_{username}", username=username,
            password_hash=hash_password("pw"), role=Role.COACH,
            created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            scope={}, active=True))

    def test_existing_unscoped_coach_forbidden_on_roster_mutation(self):
        self._insert_unscoped_coach("legacy_coach_mut")
        c = self._client()
        self._req(c, "POST", "/api/auth/login",
                  {"username": "legacy_coach_mut", "password": "pw"})
        status, body = self._req(
            c, "POST", f"/api/games/{self.gid}/build-roster",
            {"team_id": self.home})
        self.assertEqual(status, 403)
        self.assertEqual(body["error"]["code"], "forbidden")

    def test_existing_unscoped_coach_forbidden_on_private_read(self):
        self._insert_unscoped_coach("legacy_coach_read")
        c = self._client()
        self._req(c, "POST", "/api/auth/login",
                  {"username": "legacy_coach_read", "password": "pw"})
        status, _ = self._req(c, "GET", f"/api/games/{self.gid}/board")
        self.assertEqual(status, 403)

    def test_rebind_scope_repairs_unscoped_coach_over_http(self):
        # The remediation route: an admin rebinds a legacy unscoped coach to a
        # real team, and it can then read/act.
        self._insert_unscoped_coach("legacy_coach_rebind")
        acct_id = "user_legacy_coach_rebind"
        admin = self._admin()
        status, body = self._req(admin, "POST", f"/api/accounts/{acct_id}/scope",
                                 {"scope": {"team_id": self.home}})
        self.assertEqual(status, 200)
        self.assertEqual(body["scope"], {"team_id": self.home})
        # The rebound (now home-scoped) coach can read its own team's game.
        c = self._client()
        self._req(c, "POST", "/api/auth/login",
                  {"username": "legacy_coach_rebind", "password": "pw"})
        status, _ = self._req(c, "GET", f"/api/games/{self.gid}/board")
        self.assertEqual(status, 200)

    def test_player_cannot_be_rebound_to_foreign_team_over_http(self):
        # Privacy: a player bound to their own team can read that team's game;
        # an admin cannot rebind them onto another team (which would grant a
        # private read of that team's game). Zero writes/audits on rejection,
        # and the live session stays scoped to the player's own team.
        store = srv.STATE.api.store
        home_player = store.players_for_team(self.home)[0].id
        store.add_user_account(UserAccount(
            id="user_player_priv", username="player_priv",
            password_hash=hash_password("pw"), role=Role.PLAYER,
            created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            scope={"player_id": home_player, "team_id": self.home}, active=True))
        # The player can read their own team's game.
        c = self._client()
        self._req(c, "POST", "/api/auth/login",
                  {"username": "player_priv", "password": "pw"})
        status, _ = self._req(c, "GET", f"/api/games/{self.gid}/board")
        self.assertEqual(status, 200)
        # An admin rebind onto the AWAY team is refused with zero writes/audits.
        admin = self._admin()
        audits_before = len(store.all_setup_audit())
        status, body = self._req(
            admin, "POST", "/api/accounts/user_player_priv/scope",
            {"scope": {"player_id": home_player, "team_id": self.away}})
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["details"].get("reason"), "scope_team_mismatch")
        self.assertEqual(len(store.all_setup_audit()), audits_before)
        # The live session is unchanged — still scoped to the home team only.
        self.assertEqual(store.get_user_account("user_player_priv").scope,
                         {"player_id": home_player, "team_id": self.home})

    def test_rebind_player_to_team_only_scope_is_400_zero_writes(self):
        # A player rebind to a team_id-only scope (no player_id) is refused —
        # no ownership to validate the team against.
        store = srv.STATE.api.store
        home_player = store.players_for_team(self.home)[0].id
        store.add_user_account(UserAccount(
            id="user_player_toonly", username="player_toonly",
            password_hash=hash_password("pw"), role=Role.PLAYER,
            created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            scope={"player_id": home_player, "team_id": self.home}, active=True))
        admin = self._admin()
        before = len(store.all_setup_audit())
        status, body = self._req(
            admin, "POST", "/api/accounts/user_player_toonly/scope",
            {"scope": {"team_id": self.home}})
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["details"].get("reason"), "scope_required")
        self.assertEqual(len(store.all_setup_audit()), before)
        self.assertEqual(store.get_user_account("user_player_toonly").scope,
                         {"player_id": home_player, "team_id": self.home})

    def test_rebind_non_object_scope_is_400(self):
        self._insert_unscoped_coach("legacy_coach_badscope")
        admin = self._admin()
        status, body = self._req(
            admin, "POST", "/api/accounts/user_legacy_coach_badscope/scope",
            {"scope": "team_home"})
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "validation_error")
        self.assertEqual(body["error"]["details"].get("reason"), "invalid_scope")

    def test_rebind_requires_authenticated_admin(self):
        # The rebind route is gated on MANAGE_USERS — an unauthenticated request
        # (no session) is refused before it can touch any scope.
        self._insert_unscoped_coach("legacy_coach_authz")
        anon = self._client()  # never logs in
        status, _ = self._req(
            anon, "POST", "/api/accounts/user_legacy_coach_authz/scope",
            {"scope": {"team_id": self.home}})
        self.assertEqual(status, 401)


# --------------------------------------------------------------------------- #
# Readiness remediation path (criterion: no silent grandfathering).           #
# --------------------------------------------------------------------------- #
class CoachScopeReadinessTest(unittest.TestCase):
    def _api_with_team(self):
        api = ApiService(SqlStore(":memory:"))
        api.store.add_team(Team(id="team_home", name="Lions"))
        return api

    def _check(self, api):
        checks = api.get_readiness("production", cookie_hardened=True)["checks"]
        return next(c for c in checks if c["name"] == "coach_scope_bound")

    def test_bound_coach_passes(self):
        api = self._api_with_team()
        api.accounts.create_account("c", "pw", Role.COACH,
                                    scope={"team_id": "team_home"})
        self.assertTrue(self._check(api)["ok"])

    def test_unscoped_active_coach_fails_readiness(self):
        api = self._api_with_team()
        # A pre-fix legacy account: active coach with no team scope.
        api.store.add_user_account(UserAccount(
            id="user_legacy", username="legacy", password_hash="x",
            role=Role.COACH, created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            scope={}, active=True))
        check = self._check(api)
        self.assertFalse(check["ok"])
        self.assertIn("1", check["detail"])

    def test_dangling_team_coach_fails_readiness(self):
        api = self._api_with_team()
        api.store.add_user_account(UserAccount(
            id="user_dangle", username="dangle", password_hash="x",
            role=Role.COACH, created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            scope={"team_id": "team_ghost"}, active=True))
        self.assertFalse(self._check(api)["ok"])

    def test_non_production_never_blocks(self):
        api = self._api_with_team()
        api.store.add_user_account(UserAccount(
            id="user_legacy2", username="legacy2", password_hash="x",
            role=Role.COACH, created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            scope={}, active=True))
        checks = api.get_readiness("demo", cookie_hardened=False)["checks"]
        check = next(c for c in checks if c["name"] == "coach_scope_bound")
        self.assertTrue(check["ok"])


# --------------------------------------------------------------------------- #
# Two-connection PostgreSQL race: coach creation vs Team deletion (#266        #
# review blocker 1). Without the FOR UPDATE row lock, both could pass their    #
# checks under READ COMMITTED and orphan a coach against a deleted team.       #
# --------------------------------------------------------------------------- #
@unittest.skipUnless(os.environ.get("TEST_DATABASE_URL"),
                     "PostgreSQL required (set TEST_DATABASE_URL)")
class CoachTeamDeleteRaceTest(unittest.TestCase):
    def setUp(self):
        self.url = os.environ["TEST_DATABASE_URL"]
        SqlStore(self.url).clear_all_data()

    def test_create_coach_racing_team_delete_never_orphans(self):
        import threading
        seed = SqlStore(self.url)
        seed.add_team(Team(id="race_team", name="Race Team"))

        api_create = ApiService(SqlStore(self.url))
        api_delete = ApiService(SqlStore(self.url))
        barrier = threading.Barrier(2)

        def create():
            barrier.wait()
            try:
                api_create.accounts.create_account(
                    "racecoach", "pw", Role.COACH, scope={"team_id": "race_team"})
            except Exception:  # a rejected create (team already gone) is fine
                pass

        def delete():
            barrier.wait()
            # Facade swallows a HasDependenciesError into an error dict; either
            # way the team is only removed when no coach is bound.
            api_delete.delete_team("race_team", actor_id="admin")

        ta, tb = threading.Thread(target=create), threading.Thread(target=delete)
        ta.start(); tb.start(); ta.join(15); tb.join(15)

        check = SqlStore(self.url)
        coach = check.get_user_account_by_username("racecoach")
        team = check.get_team("race_team")
        # The invariant: a coach that exists is never bound to a deleted team.
        if coach is not None and (coach.scope or {}).get("team_id") == "race_team":
            self.assertIsNotNone(
                team, "a coach was left bound to a concurrently-deleted team")


@unittest.skipUnless(os.environ.get("TEST_DATABASE_URL"),
                     "PostgreSQL required (set TEST_DATABASE_URL)")
class AccountRebindDeactivateRaceTest(unittest.TestCase):
    """Two-connection race: a scope rebind vs a deactivation of the SAME account
    (#266 review). Both read-modify-write the whole row, so without the account
    row lock one would lost-update the other — e.g. a rebind silently restoring
    active=True over a committed deactivation. With the lock they serialize, and
    the deterministic final state is BOTH applied: active=False AND the new
    scope, regardless of which wins the lock."""

    def setUp(self):
        self.url = os.environ["TEST_DATABASE_URL"]
        SqlStore(self.url).clear_all_data()

    def test_rebind_and_deactivate_never_lost_update(self):
        import threading
        seed = SqlStore(self.url)
        seed.add_team(Team(id="tA", name="A"))
        seed.add_team(Team(id="tB", name="B"))
        seed.add_user_account(UserAccount(
            id="acct", username="coachx", password_hash=hash_password("pw"),
            role=Role.COACH, created_at=_clock(),
            scope={"team_id": "tA"}, active=True))

        rebind_svc = AccountService(SqlStore(self.url), _clock)
        deact_svc = AccountService(SqlStore(self.url), _clock)
        barrier = threading.Barrier(2)

        def rebind():
            barrier.wait()
            rebind_svc.rebind_account_scope("acct", {"team_id": "tB"})

        def deactivate():
            barrier.wait()
            deact_svc.set_active("acct", False)

        t1 = threading.Thread(target=rebind)
        t2 = threading.Thread(target=deactivate)
        t1.start(); t2.start(); t1.join(15); t2.join(15)

        final = SqlStore(self.url).get_user_account("acct")
        self.assertFalse(final.active,
                         "deactivation was lost-updated by the concurrent rebind")
        self.assertEqual(final.scope, {"team_id": "tB"},
                         "rebind was lost-updated by the concurrent deactivation")


if __name__ == "__main__":
    unittest.main()
