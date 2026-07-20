"""Audited in-place Player profile edit (#268).

An operator can correct a Player's name, position, jersey number, shooting
hand, and email through a supported edit flow — without changing the Player id
or losing roster/availability/guardian/audit history, and without a
delete/recreate (which is correctly blocked once history exists). Team
reassignment and the active/inactive lifecycle stay separate operations.

Covers the service edit (reusing #269 jersey validation), the email
ContactDestination create/update/retire lifecycle (never exposing the raw
address to coach/public payloads, never orphaning notification preferences),
the no-op-no-audit rule, atomic field-level errors, and the HTTP method/error
contract (#271) — on Memory, SQLite, and PostgreSQL.
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
    Division, Game, GameRosterEntry, LeagueSeason, NotificationChannel,
    Position, RosterEntryStatus, RosterRole, SelectionSource, Team)
from hockey_scheduler.services import SetupService
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
    store.add_team(Team(id="away", name="Away", division="D1", division_id="d"))
    return ApiService(store)


def _player_updated_audits(store):
    return [a for a in store.all_setup_audit() if a.action == "player_updated"]


class UpdatePlayerServiceTest(unittest.TestCase):
    def _each(self):
        for label, store in _service_backends():
            with self.subTest(backend=label):
                api = _seed(store)
                setup = api.setup
                player = setup.add_player("home", "Jordn Le", Position.FORWARD,
                                          jersey_number=17, email="j@x.com",
                                          actor_id="a")
                try:
                    yield label, store, api, setup, player
                finally:
                    if isinstance(store, SqlStore):
                        store.close()

    def test_edit_corrects_fields_without_changing_id_or_history(self):
        for label, store, api, setup, player in self._each():
            pid = player.id
            # Attach real history: a roster entry the player must keep.
            now = datetime(2026, 1, 1, tzinfo=UTC)
            store.add_game(Game(id="g1", home_team_id="home", start_time=now))
            store.add_roster_entry(GameRosterEntry(
                id="re1", game_id="g1", player_id=pid,
                roster_role=RosterRole.SELECTED,
                selection_source=SelectionSource.COACH_SELECTED,
                status=RosterEntryStatus.SELECTED, selected_at=now,
                updated_at=now, selected_by="a"))
            updated = setup.update_player(
                pid, name="Jordan Lee", position=Position.DEFENSE,
                jersey_number=8, shoots="L", actor_id="a")
            self.assertEqual(updated.id, pid, label)              # id unchanged
            self.assertEqual(updated.name, "Jordan Lee", label)
            self.assertEqual(updated.position, Position.DEFENSE, label)
            self.assertEqual(updated.jersey_number, 8, label)
            self.assertEqual(updated.shoots, "L", label)
            # History survives.
            self.assertEqual(
                [e.id for e in store.roster_entries_for_player(pid)], ["re1"],
                label)

    def test_noop_update_writes_no_audit(self):
        for label, store, api, setup, player in self._each():
            before = len(_player_updated_audits(store))
            setup.update_player(player.id, name="Jordn Le",
                                position=Position.FORWARD, jersey_number=17,
                                email="j@x.com", actor_id="a")
            self.assertEqual(len(_player_updated_audits(store)), before, label)

    def test_audit_lists_only_changed_fields_never_raw_email(self):
        for label, store, api, setup, player in self._each():
            setup.update_player(player.id, name="Jordan Lee",
                                email="new@x.com", actor_id="a")
            audit = _player_updated_audits(store)[-1]
            self.assertEqual(set(audit.detail["changed_fields"]),
                             {"name", "email"}, label)
            self.assertNotIn("new@x.com", json.dumps(audit.detail), label)

    def test_invalid_jersey_is_field_error_and_atomic(self):
        for label, store, api, setup, player in self._each():
            from hockey_scheduler.domain.errors import ValidationError
            with self.assertRaises(ValidationError, msg=label) as ctx:
                setup.update_player(player.id, name="Renamed",
                                    jersey_number=131, actor_id="a")
            self.assertEqual(ctx.exception.details.get("field"),
                             "jersey_number", label)
            # Atomic: the name change did not land either.
            self.assertEqual(store.get_player(player.id).name, "Jordn Le", label)

    def test_duplicate_jersey_on_team_is_rejected(self):
        for label, store, api, setup, player in self._each():
            from hockey_scheduler.domain.errors import IntegrityConflictError
            setup.add_player("home", "Other", Position.FORWARD,
                             jersey_number=9, actor_id="a")
            with self.assertRaises(IntegrityConflictError, msg=label):
                setup.update_player(player.id, jersey_number=9, actor_id="a")

    def test_email_create_update_and_retire(self):
        for label, store, api, setup, player in self._each():
            pid = player.id
            ref = f"player:{pid}"
            # Update the address in place (not duplicated).
            setup.update_player(pid, email="second@x.com", actor_id="a")
            self.assertEqual(setup.active_player_email(pid), "second@x.com",
                             label)
            contacts = [c for c in store.all_contact_destinations()
                        if c.recipient_ref == ref]
            self.assertEqual(len(contacts), 1, label)
            # Clear it → the contact is retired (inactive), never deleted.
            setup.update_player(pid, email=None, actor_id="a")
            self.assertIsNone(setup.active_player_email(pid), label)
            still = [c for c in store.all_contact_destinations()
                     if c.recipient_ref == ref]
            self.assertEqual(len(still), 1, label)        # kept, not deleted
            self.assertFalse(still[0].active, label)       # retired

    def test_retiring_email_preserves_notification_preferences(self):
        for label, store, api, setup, player in self._each():
            pid = player.id
            ref = f"player:{pid}"
            api.set_notification_preference(ref, "email", False)
            setup.update_player(pid, email=None, actor_id="a")
            prefs = [p for p in store.all_notification_preferences()
                     if p.recipient_ref == ref]
            self.assertTrue(prefs, label)                  # not orphaned/removed

    def test_team_and_active_state_are_not_editable_here(self):
        for label, store, api, setup, player in self._each():
            # update_player accepts no team/active kwargs; the player's team and
            # active flag are untouched by a profile edit.
            setup.update_player(player.id, name="Renamed", actor_id="a")
            fresh = store.get_player(player.id)
            self.assertEqual(fresh.team_id, "home", label)
            self.assertTrue(fresh.is_active, label)


class UpdatePlayerHttpTest(unittest.TestCase):
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
        return players[0]

    def test_edit_via_v2_route_succeeds_and_email_only_on_operator_read(self):
        c = self._admin()
        p = self._a_player(c)
        s, _h, body = self._req(
            c, "POST", f"/api/v2/setup/player/{p['id']}/update",
            {"name": "Renamed Player", "email": "op@x.com"})
        self.assertEqual(s, 200)
        self.assertEqual(body["name"], "Renamed Player")
        # The operator /api/players read carries the email for the drawer...
        _s, _h, players = self._req(c, "GET", "/api/players")
        row = next(x for x in players if x["id"] == p["id"])
        self.assertEqual(row.get("email"), "op@x.com")
        # ...but the player DTO itself has no email field baked in.
        self.assertNotIn("email", body)

    def test_out_of_range_jersey_is_400(self):
        c = self._admin()
        p = self._a_player(c)
        s, _h, body = self._req(
            c, "POST", f"/api/v2/setup/player/{p['id']}/update",
            {"jersey_number": 131})
        self.assertEqual(s, 400)
        self.assertEqual(body["error"]["details"]["reason"],
                         "invalid_jersey_number")

    def test_unknown_field_is_rejected(self):
        c = self._admin()
        p = self._a_player(c)
        s, _h, body = self._req(
            c, "POST", f"/api/v2/setup/player/{p['id']}/update",
            {"team_id": "home"})   # not an editable field here
        self.assertEqual(s, 400)
        self.assertEqual(body["error"]["details"]["reason"], "unknown_field")

    def test_put_and_patch_on_update_route_are_405_json(self):
        c = self._admin()
        p = self._a_player(c)
        for method in ("PUT", "PATCH"):
            s, headers, body = self._req(
                c, method, f"/api/v2/setup/player/{p['id']}/update")
            self.assertEqual(s, 405, method)
            self.assertEqual(body["error"]["code"], "method_not_allowed", method)
            self.assertIn("POST", headers.get("Allow", ""))


if __name__ == "__main__":
    unittest.main()
