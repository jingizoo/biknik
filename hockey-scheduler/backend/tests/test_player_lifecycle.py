"""Player deactivate/reactivate lifecycle (#270).

`Player.is_active` was accepted at creation and respected by roster logic, but
had no supported operation to change it later — leaving delete as the only exit
for an injured-reserve, a mid-season move, or a departure. This adds an
explicit, audited active-state operation that preserves the Player id and all
attached history (roster/game rows, availability, guardian, contact, audit),
removes the Player from FUTURE roster selection and substitute candidacy while
historical rows still render, blocks reactivation that would violate jersey
integrity (#269), and forbids binding/reactivating a scoped account (#266/#282)
to an inactive Player. Covered on Memory, SQLite, and PostgreSQL + HTTP.
"""

import json
import os
import threading
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from http.cookiejar import CookieJar
from http.server import ThreadingHTTPServer

from helpers import BACKEND  # noqa: F401  (ensures sys.path is set up)

from hockey_scheduler.api import ApiService
from hockey_scheduler.domain import (
    Division, Game, GameRosterEntry, LeagueSeason, Position, Role, Session,
    RosterEntryStatus, RosterRole, SelectionSource, SubstituteStatus, Team)
from hockey_scheduler.domain.errors import (
    IntegrityConflictError, NotEligibleError, ValidationError)
from hockey_scheduler.store import InMemoryStore, SqlStore

UTC = timezone.utc


def _service_backends():
    stores = [("memory", InMemoryStore()), ("sqlite", SqlStore(":memory:"))]
    url = os.environ.get("TEST_DATABASE_URL")
    if url:
        pg = SqlStore(url)
        pg.reset_schema()
        stores.append(("postgres", pg))
    return stores


def _seed(store):
    store.add_league_season(LeagueSeason(id="ls", league_id="l", season_id="se"))
    store.add_division(Division(id="d", league_season_id="ls", name="D1"))
    store.add_team(Team(id="home", name="Home", division="D1", division_id="d"))
    return ApiService(store)


def _lifecycle_audits(store):
    return [a for a in store.all_setup_audit()
            if a.action in ("player_activated", "player_deactivated")]


class SetPlayerActiveServiceTest(unittest.TestCase):
    def _each(self):
        for label, store in _service_backends():
            with self.subTest(backend=label):
                api = _seed(store)
                setup = api.setup
                player = setup.add_player("home", "Jordan Lee", Position.FORWARD,
                                          jersey_number=17, actor_id="a")
                try:
                    yield label, store, api, setup, player
                finally:
                    if isinstance(store, SqlStore):
                        store.close()

    def test_deactivate_reactivate_preserves_id_and_history(self):
        for label, store, api, setup, player in self._each():
            pid = player.id
            now = datetime(2026, 1, 1, tzinfo=UTC)
            store.add_game(Game(id="g1", home_team_id="home", start_time=now))
            store.add_roster_entry(GameRosterEntry(
                id="re1", game_id="g1", player_id=pid,
                roster_role=RosterRole.SELECTED,
                selection_source=SelectionSource.COACH_SELECTED,
                status=RosterEntryStatus.SELECTED, selected_at=now,
                updated_at=now, selected_by="a"))
            setup.set_player_active(pid, False, actor_id="a")
            self.assertFalse(store.get_player(pid).is_active, label)
            # History survives deactivation.
            self.assertEqual(
                [e.id for e in store.roster_entries_for_player(pid)], ["re1"],
                label)
            # Reactivate — id unchanged, history still attached.
            setup.set_player_active(pid, True, actor_id="a")
            self.assertTrue(store.get_player(pid).is_active, label)
            self.assertEqual(store.get_player(pid).id, pid, label)
            self.assertEqual(
                [e.id for e in store.roster_entries_for_player(pid)], ["re1"],
                label)

    def test_idempotent_no_op_writes_no_audit(self):
        for label, store, api, setup, player in self._each():
            # Already active → active is a no-op.
            setup.set_player_active(player.id, True, actor_id="a")
            self.assertEqual(len(_lifecycle_audits(store)), 0, label)
            setup.set_player_active(player.id, False, actor_id="a")
            n = len(_lifecycle_audits(store))
            # Deactivate again → no duplicate audit noise.
            setup.set_player_active(player.id, False, actor_id="a")
            self.assertEqual(len(_lifecycle_audits(store)), n, label)

    def test_audit_records_actor_prior_new_and_reason(self):
        for label, store, api, setup, player in self._each():
            setup.set_player_active(player.id, False, actor_id="admin1",
                                    reason="Season-ending injury")
            audit = _lifecycle_audits(store)[-1]
            self.assertEqual(audit.action, "player_deactivated", label)
            self.assertEqual(audit.actor_id, "admin1", label)
            self.assertEqual(audit.detail["from_active"], True, label)
            self.assertEqual(audit.detail["to_active"], False, label)
            self.assertEqual(audit.detail["reason"], "Season-ending injury", label)

    def test_reactivation_blocked_by_jersey_collision(self):
        for label, store, api, setup, player in self._each():
            pid = player.id
            setup.set_player_active(pid, False, actor_id="a")
            # While inactive, #17 is free — an active teammate takes it.
            setup.add_player("home", "Newbie", Position.FORWARD,
                             jersey_number=17, actor_id="a")
            with self.assertRaises(IntegrityConflictError, msg=label) as ctx:
                setup.set_player_active(pid, True, actor_id="a")
            self.assertEqual(ctx.exception.details.get("reason"),
                             "duplicate_jersey_number", label)
            # Still inactive — no silent resurrection.
            self.assertFalse(store.get_player(pid).is_active, label)

    def test_unknown_player_raises_not_found(self):
        from hockey_scheduler.domain.errors import NotFoundError
        for label, store, api, setup, player in self._each():
            with self.assertRaises(NotFoundError, msg=label):
                setup.set_player_active("player_missing", False, actor_id="a")

    def test_inactive_blocked_from_new_selection_and_substitution(self):
        for label, store, api, setup, player in self._each():
            now = datetime(2026, 1, 1, tzinfo=UTC)
            store.add_game(Game(id="g1", home_team_id="home", start_time=now))
            # Active: the eligibility gate does not block on active-state.
            reason = api.roster.substitute_block_reason(player.id, "g1")
            self.assertNotEqual(reason, "You are not an active player.", label)
            # Deactivate → the single-source eligibility gate blocks selection
            # and substitute candidacy (roster_service already gates on
            # is_active; deactivation removes the player from FUTURE use).
            setup.set_player_active(player.id, False, actor_id="a")
            self.assertEqual(
                api.roster.substitute_block_reason(player.id, "g1"),
                "You are not an active player.", label)

    def test_non_bool_active_is_field_error_and_no_change(self):
        # ApiService.set_player_active forwards raw values, so the service must
        # validate `active` as an ACTUAL bool — never bool()-coerce "false"/"0"/
        # a collection into a silent state flip (#270 review).
        for label, store, api, setup, player in self._each():
            for bad in ("false", "0", "true", [], {}, ["x"], 1, 0, None):
                with self.assertRaises(ValidationError, msg=(label, repr(bad))) as ctx:
                    setup.set_player_active(player.id, bad, actor_id="a")
                self.assertEqual(ctx.exception.details.get("reason"),
                                 "invalid_active", (label, repr(bad)))
                self.assertEqual(ctx.exception.details.get("field"), "active",
                                 (label, repr(bad)))
            self.assertTrue(store.get_player(player.id).is_active, label)  # unchanged
            self.assertEqual(len(_lifecycle_audits(store)), 0, label)

    def test_non_string_reason_is_field_error_and_no_change(self):
        for label, store, api, setup, player in self._each():
            for bad in (5, ["x"], {"r": 1}, True, 0):
                with self.assertRaises(ValidationError, msg=(label, repr(bad))) as ctx:
                    setup.set_player_active(player.id, False, actor_id="a",
                                            reason=bad)
                self.assertEqual(ctx.exception.details.get("reason"),
                                 "invalid_reason", (label, repr(bad)))
                self.assertEqual(ctx.exception.details.get("field"), "reason",
                                 (label, repr(bad)))
            self.assertTrue(store.get_player(player.id).is_active, label)  # unchanged
            self.assertEqual(len(_lifecycle_audits(store)), 0, label)

    def _sub_game(self, store, api):
        # A published, upcoming game the home player can substitute for, with a
        # single open skater slot. A deterministic clock keeps it "upcoming".
        from helpers import FakeClock
        api.roster.clock = FakeClock()
        store.add_game(Game(
            id="g1", home_team_id="home",
            start_time=datetime(2026, 1, 2, tzinfo=UTC),
            division_id="d", published=True,
            target_goalies=0, target_skaters=1))

    def test_deactivation_hides_candidate_and_blocks_offer(self):
        # An ENROLLED substitute who is then deactivated drops out of the live
        # candidate queue and cannot be offered — the enrollment stays as
        # history, but no roster/audit mutation happens on the rejected path.
        for label, store, api, setup, player in self._each():
            self._sub_game(store, api)
            api.roster.enroll_substitute("g1", player.id, actor_id="a")
            self.assertIn(player.id, [c["player_id"] for c in
                          api.roster.list_substitute_candidates("g1", "home")],
                          label)
            setup.set_player_active(player.id, False, actor_id="a")
            self.assertNotIn(player.id, [c["player_id"] for c in
                             api.roster.list_substitute_candidates("g1", "home")],
                             label)                                # candidate hidden
            with self.assertRaises(NotEligibleError, msg=label):
                api.roster.offer_substitute("g1", player.id, actor_id="a")
            self.assertEqual(
                store.substitute_for_player("g1", player.id).status,
                SubstituteStatus.ENROLLED, label)                 # unchanged

    def test_deactivation_blocks_accept_and_coach_add(self):
        # A player OFFERED a slot, then deactivated, can neither accept nor be
        # coach-added — and no roster row is created on the rejected paths.
        for label, store, api, setup, player in self._each():
            self._sub_game(store, api)
            api.roster.enroll_substitute("g1", player.id, actor_id="a")
            api.roster.offer_substitute("g1", player.id, actor_id="a")
            setup.set_player_active(player.id, False, actor_id="a")
            with self.assertRaises(NotEligibleError, msg=label):
                api.roster.accept_substitute("g1", player.id, actor_id="a")
            with self.assertRaises(NotEligibleError, msg=label):
                api.roster.add_substitute_to_roster("g1", player.id, actor_id="a")
            self.assertIsNone(
                store.roster_entry_for_player("g1", player.id), label)

    def test_blank_reason_is_omitted_from_audit(self):
        for label, store, api, setup, player in self._each():
            setup.set_player_active(player.id, False, actor_id="a",
                                    reason="   ")
            audit = _lifecycle_audits(store)[-1]
            self.assertNotIn("reason", audit.detail, label)   # blank → omitted

    def test_facade_rejects_non_bool_active_with_field_error(self):
        # The facade is a supported boundary: a bad value must be a structured
        # error dict, not a silent success.
        for label, store, api, setup, player in self._each():
            res = api.set_player_active(player.id, "nope", actor_id="a")
            self.assertEqual(res["error"]["details"]["reason"], "invalid_active",
                             label)
            self.assertTrue(store.get_player(player.id).is_active, label)


def _team_node(hierarchy, team_id):
    for lg in hierarchy["leagues"]:
        for se in lg["seasons"]:
            for lv in se["levels"]:
                for d in lv["divisions"]:
                    for t in d["teams"]:
                        if t["id"] == team_id:
                            return t
    return None


class ActiveDirectoryCountTest(unittest.TestCase):
    """The team roster count in the setup hierarchy is an ACTIVE-directory
    count: an inactive Player is excluded from it, while its historical rows
    still render elsewhere (#270)."""

    def test_player_count_excludes_inactive(self):
        for label, store in _service_backends():
            with self.subTest(backend=label):
                api = ApiService(store)
                program = api.create_program("Over 55")
                season = api.create_season(program["id"], "Fall 2026")
                level = api.create_league(season["id"], "Level 1", sort_order=1)
                div = api.create_division(season["id"], "Div A",
                                          league_id=level["id"])
                club = api.create_club("Club X")
                team = api.create_team(club["id"], div["id"], "Team1")
                api.register_team_for_season(season["id"], team["id"], div["id"])
                p = api.create_player(team["id"], "Vince Skater", "forward")
                node = _team_node(api.get_setup_hierarchy(), team["id"])
                self.assertEqual(node["player_count"], 1, label)
                # Deactivate → excluded from the active-directory count.
                api.set_player_active(p["id"], False, actor_id="a")
                node2 = _team_node(api.get_setup_hierarchy(), team["id"])
                self.assertEqual(node2["player_count"], 0, label)
                # The Player row itself still exists (history renders).
                self.assertIsNotNone(store.get_player(p["id"]), label)
                if isinstance(store, SqlStore):
                    store.close()


class InactivePlayerAccountBindingTest(unittest.TestCase):
    def _each(self):
        for label, store in _service_backends():
            with self.subTest(backend=label):
                api = _seed(store)
                player = api.setup.add_player("home", "Jordan Lee",
                                              Position.FORWARD, actor_id="a")
                try:
                    yield label, store, api, player
                finally:
                    if isinstance(store, SqlStore):
                        store.close()

    def test_cannot_create_scoped_account_for_inactive_player(self):
        for label, store, api, player in self._each():
            api.setup.set_player_active(player.id, False, actor_id="a")
            with self.assertRaises(ValidationError, msg=label) as ctx:
                api.accounts.create_account(
                    "player_login", "pw", Role.PLAYER,
                    scope={"player_id": player.id})
            self.assertEqual(ctx.exception.details.get("reason"),
                             "player_inactive", label)

    def test_cannot_reactivate_account_onto_deactivated_player(self):
        for label, store, api, player in self._each():
            acct = api.accounts.create_account(
                "player_login", "pw", Role.PLAYER,
                scope={"player_id": player.id})
            api.accounts.set_active(acct.id, False, actor_id="a")
            api.setup.set_player_active(player.id, False, actor_id="a")
            with self.assertRaises(ValidationError, msg=label) as ctx:
                api.accounts.set_active(acct.id, True, actor_id="a")
            self.assertEqual(ctx.exception.details.get("reason"),
                             "player_inactive", label)

    def test_deactivation_retires_account_and_sessions_no_silent_restore(self):
        # Deactivating a Player atomically retires its ACTIVE scoped account and
        # revokes live sessions (a login must not outlive the roster exit), and
        # reactivating the Player does NOT silently restore that authority
        # (#270 review).
        for label, store, api, player in self._each():
            acct = api.accounts.create_account(
                "player_login", "pw", Role.PLAYER,
                scope={"player_id": player.id})
            now = datetime(2026, 1, 1, tzinfo=UTC)
            store.add_session(Session(
                id="sess1", token_hash="hash1", user_id=acct.id,
                issued_at=now, expires_at=now + timedelta(days=1)))
            self.assertTrue(store.get_user_account(acct.id).active, label)

            api.setup.set_player_active(player.id, False, actor_id="a")
            self.assertFalse(store.get_user_account(acct.id).active, label)  # retired
            self.assertIsNotNone(
                store.sessions_for_user(acct.id)[0].revoked_at, label)   # revoked
            # The account-deactivation audit cause is a REAL deactivation.
            acct_audit = next(a for a in store.all_setup_audit()
                              if a.action == "user_account_deactivated"
                              and a.entity_id == acct.id)
            self.assertEqual(acct_audit.detail.get("reason"),
                             "player_deactivated", label)

            # Reactivating the Player must leave the account inactive — the
            # operator reactivates it explicitly (which re-checks player active).
            api.setup.set_player_active(player.id, True, actor_id="a")
            self.assertFalse(store.get_user_account(acct.id).active, label)

    def test_deactivate_reconciles_already_inactive_player_authority(self):
        # Legacy data can carry an inactive Player that still has an active
        # scoped account + live session (inactive players were once creatable
        # and binding didn't reject them). A deactivate request must reconcile
        # that authority even though the Player row is already inactive — the
        # idempotent short-circuit must not skip the account/session retire
        # (#270 review). No Player state/history change, no duplicate Player
        # lifecycle audit; a repeat call mutates nothing further.
        for label, store, api, player in self._each():
            acct = api.accounts.create_account(
                "player_login", "pw", Role.PLAYER,
                scope={"player_id": player.id})
            now = datetime(2026, 1, 1, tzinfo=UTC)
            store.add_session(Session(
                id="sess1", token_hash="hash1", user_id=acct.id,
                issued_at=now, expires_at=now + timedelta(days=1)))
            # Simulate the legacy state: flip the Player inactive DIRECTLY,
            # bypassing set_player_active, so the account/session stay live.
            legacy = store.get_player_for_update(player.id)
            legacy.is_active = False
            store.save_player(legacy)
            life_before = len(_lifecycle_audits(store))

            api.setup.set_player_active(player.id, False, actor_id="a")
            self.assertFalse(store.get_player(player.id).is_active, label)   # unchanged
            self.assertFalse(store.get_user_account(acct.id).active, label)  # retired
            self.assertIsNotNone(
                store.sessions_for_user(acct.id)[0].revoked_at, label)       # revoked
            self.assertEqual(len(_lifecycle_audits(store)), life_before,
                             label)                                          # no player audit
            acct_audits = [a for a in store.all_setup_audit()
                           if a.action == "user_account_deactivated"
                           and a.entity_id == acct.id]
            self.assertEqual(len(acct_audits), 1, label)   # real account audit written
            # The cause is the legacy already-inactive reconcile — NOT a real
            # deactivation (no player lifecycle event accompanies it).
            self.assertEqual(acct_audits[0].detail.get("reason"),
                             "player_deactivation_reconcile", label)

            # Repeat the request → no further account/session/audit mutation.
            all_before = len(store.all_setup_audit())
            api.setup.set_player_active(player.id, False, actor_id="a")
            self.assertEqual(len(store.all_setup_audit()), all_before, label)

    def test_reactivation_retires_dangling_account_no_silent_restore(self):
        # Legacy data can carry an INACTIVE Player that still has an active
        # scoped account + un-revoked session (inactive players were once
        # creatable and binding didn't reject them). Reactivating the Player
        # (inactive->active) must retire that dangling authority BEFORE flipping
        # the row active, so once is_active is true the stale account/session
        # does NOT silently regain access — the operator restores login only
        # through the separate account-lifecycle reactivation (#270 review).
        for label, store, api, player in self._each():
            acct = api.accounts.create_account(
                "player_login", "pw", Role.PLAYER,
                scope={"player_id": player.id})
            now = datetime(2026, 1, 1, tzinfo=UTC)
            store.add_session(Session(
                id="sess1", token_hash="hash1", user_id=acct.id,
                issued_at=now, expires_at=now + timedelta(days=1)))
            # Legacy state: flip the Player inactive DIRECTLY so the account and
            # session stay live and the row is already inactive.
            legacy = store.get_player_for_update(player.id)
            legacy.is_active = False
            store.save_player(legacy)
            self.assertTrue(store.get_user_account(acct.id).active, label)

            # Reactivate the Player.
            reactivated = api.setup.set_player_active(
                player.id, True, actor_id="a")
            self.assertTrue(reactivated.is_active, label)               # player back
            self.assertFalse(
                store.get_user_account(acct.id).active, label)          # login retired
            self.assertIsNotNone(
                store.sessions_for_user(acct.id)[0].revoked_at, label)  # session revoked

            # Exactly one Player reactivation audit + one real account audit.
            acts = [a for a in _lifecycle_audits(store)
                    if a.action == "player_activated"]
            self.assertEqual(len(acts), 1, label)
            acct_audits = [a for a in store.all_setup_audit()
                           if a.action == "user_account_deactivated"
                           and a.entity_id == acct.id]
            self.assertEqual(len(acct_audits), 1, label)
            # The cause is the reactivation safety reconcile — recorded beside a
            # player_activated event, NOT a false "player_deactivated".
            self.assertEqual(acct_audits[0].detail.get("reason"),
                             "player_reactivation_reconcile", label)

            # Repeat active=true on the now-active Player → no further mutation
            # (the legitimate-account guard: an already-active Player is not
            # re-reconciled, so nothing is touched).
            all_before = len(store.all_setup_audit())
            api.setup.set_player_active(player.id, True, actor_id="a")
            self.assertEqual(len(store.all_setup_audit()), all_before, label)

            # Only the explicit account-lifecycle reactivation restores login —
            # and it succeeds now that the Player is active again.
            api.accounts.set_active(acct.id, True, actor_id="a")
            self.assertTrue(store.get_user_account(acct.id).active, label)

    def test_reactivating_active_player_keeps_live_login(self):
        # Guard: an idempotent active=true on an ALREADY-active Player must NOT
        # nuke a legitimate live login. Reconciliation only runs on a real
        # inactive->active transition (or any deactivate) — never on a no-op
        # reactivation (#270 review).
        for label, store, api, player in self._each():
            acct = api.accounts.create_account(
                "player_login", "pw", Role.PLAYER,
                scope={"player_id": player.id})
            now = datetime(2026, 1, 1, tzinfo=UTC)
            store.add_session(Session(
                id="sess1", token_hash="hash1", user_id=acct.id,
                issued_at=now, expires_at=now + timedelta(days=1)))
            self.assertTrue(player.is_active, label)

            api.setup.set_player_active(player.id, True, actor_id="a")
            self.assertTrue(store.get_user_account(acct.id).active, label)  # preserved
            self.assertIsNone(
                store.sessions_for_user(acct.id)[0].revoked_at, label)      # live


class SetPlayerActiveHttpTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from hockey_scheduler.web import server as srv
        cls.srv = srv
        srv.STATE.reset()
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), srv.Handler)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.thread.join(timeout=5)

    def _admin(self):
        c = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(CookieJar()))
        self._req(c, "POST", "/api/auth/login",
                  {"username": "admin", "password": "demo"})
        self._req(c, "POST", "/api/demo/load", {})
        return c

    def _req(self, opener, method, path, body=None):
        url = f"http://127.0.0.1:{self.port}{path}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method,
                                     headers={"Content-Type": "application/json"})
        try:
            with opener.open(req) as r:
                return r.status, dict(r.headers), json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            return e.code, dict(e.headers), json.loads(e.read() or b"{}")

    def _a_player(self, c):
        _s, _h, players = self._req(c, "GET", "/api/players")
        return next(p for p in players if p.get("is_active", True))

    def test_deactivate_and_reactivate_via_v2_route(self):
        c = self._admin()
        p = self._a_player(c)
        s, _h, body = self._req(
            c, "POST", f"/api/v2/setup/player/{p['id']}/active",
            {"active": False, "reason": "moved away"})
        self.assertEqual(s, 200)
        self.assertFalse(body["is_active"])
        # Reactivate.
        s, _h, body = self._req(
            c, "POST", f"/api/v2/setup/player/{p['id']}/active",
            {"active": True})
        self.assertEqual(s, 200)
        self.assertTrue(body["is_active"])

    def test_http_deactivate_reconciles_already_inactive_player(self):
        # HTTP regression for the idempotent-early-return blocker (#270 review):
        # a legacy inactive Player still carrying an ACTIVE scoped account + live
        # session must have that authority reconciled when the operator POSTs
        # active=false over the wire, even though the Player row is already
        # inactive. No Player state/history change, no duplicate Player lifecycle
        # audit; a repeat POST mutates nothing further.
        c = self._admin()
        p = self._a_player(c)
        store = self.srv.STATE.api.store
        acct = self.srv.STATE.api.accounts.create_account(
            "u_http_legacy", "scoped-account-pw", Role.PLAYER,
            scope={"player_id": p["id"]})
        now = datetime(2026, 1, 1, tzinfo=UTC)
        store.add_session(Session(
            id="sess_http_legacy", token_hash="hash_http_legacy",
            user_id=acct.id, issued_at=now, expires_at=now + timedelta(days=1)))
        # Simulate legacy state: flip the Player inactive DIRECTLY so the
        # account/session stay live and the row is already inactive.
        legacy = store.get_player_for_update(p["id"])
        legacy.is_active = False
        store.save_player(legacy)
        self.assertTrue(store.get_user_account(acct.id).active)
        life_before = len(_lifecycle_audits(store))

        s, _h, body = self._req(
            c, "POST", f"/api/v2/setup/player/{p['id']}/active",
            {"active": False})
        self.assertEqual(s, 200)
        self.assertFalse(body["is_active"])                       # unchanged
        self.assertFalse(store.get_user_account(acct.id).active)  # retired
        self.assertIsNotNone(
            store.sessions_for_user(acct.id)[0].revoked_at)       # revoked
        self.assertEqual(len(_lifecycle_audits(store)), life_before)  # no dup
        acct_audits = [a for a in store.all_setup_audit()
                       if a.action == "user_account_deactivated"
                       and a.entity_id == acct.id]
        self.assertEqual(len(acct_audits), 1)                     # real audit
        self.assertEqual(acct_audits[0].detail.get("reason"),
                         "player_deactivation_reconcile")         # accurate cause

        # Repeat over the wire → no further account/session/audit mutation.
        all_before = len(store.all_setup_audit())
        s, _h, _b = self._req(
            c, "POST", f"/api/v2/setup/player/{p['id']}/active",
            {"active": False})
        self.assertEqual(s, 200)
        self.assertEqual(len(store.all_setup_audit()), all_before)

    def test_missing_active_is_400(self):
        c = self._admin()
        p = self._a_player(c)
        s, _h, body = self._req(
            c, "POST", f"/api/v2/setup/player/{p['id']}/active", {})
        self.assertEqual(s, 400)
        self.assertEqual(body["error"]["details"]["reason"], "field_required")

    def test_wrong_type_active_is_400(self):
        c = self._admin()
        p = self._a_player(c)
        s, _h, body = self._req(
            c, "POST", f"/api/v2/setup/player/{p['id']}/active",
            {"active": "yes"})
        self.assertEqual(s, 400)
        self.assertEqual(body["error"]["details"]["reason"], "wrong_type")
        self.assertEqual(body["error"]["details"]["field"], "active")

    def test_unknown_field_is_400(self):
        c = self._admin()
        p = self._a_player(c)
        s, _h, body = self._req(
            c, "POST", f"/api/v2/setup/player/{p['id']}/active",
            {"active": False, "bogus": 1})
        self.assertEqual(s, 400)
        self.assertEqual(body["error"]["details"]["reason"], "unknown_field")

    def test_wrong_type_reason_is_400(self):
        c = self._admin()
        p = self._a_player(c)
        for bad in (5, ["x"], True):
            s, _h, body = self._req(
                c, "POST", f"/api/v2/setup/player/{p['id']}/active",
                {"active": False, "reason": bad})
            self.assertEqual(s, 400, repr(bad))
            self.assertEqual(body["error"]["details"]["reason"], "wrong_type", repr(bad))
            self.assertEqual(body["error"]["details"]["field"], "reason", repr(bad))

    def test_put_and_patch_on_active_route_are_405_json(self):
        c = self._admin()
        p = self._a_player(c)
        for method in ("PUT", "PATCH", "DELETE"):
            s, headers, body = self._req(
                c, method, f"/api/v2/setup/player/{p['id']}/active")
            self.assertEqual(s, 405, method)
            self.assertEqual(body["error"]["code"], "method_not_allowed", method)
            self.assertIn("POST", headers.get("Allow", ""))


if __name__ == "__main__":
    unittest.main()
