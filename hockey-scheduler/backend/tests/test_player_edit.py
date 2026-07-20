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

    def test_shoots_canonicalized_on_create_and_edit(self):
        # Canonical L/R persist; case is normalized to uppercase; an explicit
        # None clears the hand. The same rule holds on create and edit.
        for label, store, api, setup, player in self._each():
            created = setup.add_player("home", "Lefty", Position.FORWARD,
                                       shoots="l", actor_id="a")
            self.assertEqual(created.shoots, "L", label)   # normalized on create
            updated = setup.update_player(player.id, shoots=" r ", actor_id="a")
            self.assertEqual(updated.shoots, "R", label)   # trimmed + upper
            cleared = setup.update_player(player.id, shoots=None, actor_id="a")
            self.assertIsNone(cleared.shoots, label)        # explicit clear

    def test_invalid_shoots_is_field_error_and_atomic(self):
        from hockey_scheduler.domain.errors import ValidationError
        for label, store, api, setup, player in self._each():
            # Create rejects a non-canonical hand before persisting anything.
            with self.assertRaises(ValidationError, msg=label) as cc:
                setup.add_player("home", "Bad", Position.FORWARD,
                                 shoots="left", actor_id="a")
            self.assertEqual(cc.exception.details.get("field"), "shoots", label)
            self.assertEqual(cc.exception.details.get("reason"),
                             "invalid_shoots", label)
            before = store.get_player(player.id).shoots
            # Edit rejects "banana" AND leaves the player untouched (atomic):
            # the name change in the same call must not land.
            with self.assertRaises(ValidationError, msg=label) as ec:
                setup.update_player(player.id, name="Renamed", shoots="banana",
                                    actor_id="a")
            self.assertEqual(ec.exception.details.get("field"), "shoots", label)
            fresh = store.get_player(player.id)
            self.assertEqual(fresh.shoots, before, label)
            self.assertEqual(fresh.name, "Jordn Le", label)   # rolled back
            self.assertEqual(len(_player_updated_audits(store)), 0, label)

    def test_non_string_shoots_is_field_error(self):
        from hockey_scheduler.domain.errors import ValidationError
        for label, store, api, setup, player in self._each():
            # A boolean is not a string handedness (and bool-is-int must not
            # sneak through) — a clean field-level error, nothing persisted.
            with self.assertRaises(ValidationError, msg=label) as ctx:
                setup.update_player(player.id, shoots=True, actor_id="a")
            self.assertEqual(ctx.exception.details.get("field"), "shoots", label)

    def test_non_string_email_on_edit_is_field_error_and_atomic(self):
        # A direct (non-HTTP) caller must not slip a non-string email past the
        # helper: False/0 must NOT coerce to "" and silently retire, and a
        # list/object must NOT raise a bare AttributeError. Each is a clean
        # field-level invalid_email, and — being one transaction — the
        # same-call name change and the existing contact are left untouched.
        from hockey_scheduler.domain.errors import ValidationError
        for label, store, api, setup, player in self._each():
            pid = player.id
            for bad in (False, 0, ["a@b.co"], {"e": "a@b.co"}, "no-at-sign"):
                with self.assertRaises(ValidationError, msg=(label, bad)) as ctx:
                    setup.update_player(pid, name="Should Not Land",
                                        email=bad, actor_id="a")
                self.assertEqual(ctx.exception.details.get("field"), "email",
                                 (label, bad))
                self.assertEqual(ctx.exception.details.get("reason"),
                                 "invalid_email", (label, bad))
                fresh = store.get_player(pid)
                self.assertEqual(fresh.name, "Jordn Le", (label, bad))  # rolled back
                # The email set at creation ("j@x.com") is untouched — not
                # retired by a falsy value.
                self.assertEqual(setup.active_player_email(pid), "j@x.com",
                                 (label, bad))

    def test_non_string_email_on_create_is_field_error_and_no_player(self):
        # The same gate on create: a bad-typed email rejects BEFORE the player
        # is added, so no orphan player is created (atomic create).
        from hockey_scheduler.domain.errors import ValidationError
        for label, store, api, setup, player in self._each():
            before = len(store.players_for_team("home"))
            for bad in (False, 0, ["a@b.co"], {"e": 1}, "bogus"):
                with self.assertRaises(ValidationError, msg=(label, bad)) as ctx:
                    setup.add_player("home", "Ghost", Position.FORWARD,
                                     email=bad, actor_id="a")
                self.assertEqual(ctx.exception.details.get("reason"),
                                 "invalid_email", (label, bad))
            self.assertEqual(len(store.players_for_team("home")), before, label)

    def test_invalid_position_on_edit_is_field_error_and_atomic(self):
        # A direct service edit must not persist a non-Position: an invalid
        # string, an explicit None (position is required, not nullable), or a
        # wrong type is a field-level invalid_position — and because the edit is
        # one transaction, the same-call name change and the audit are unchanged.
        from hockey_scheduler.domain.errors import ValidationError
        for label, store, api, setup, player in self._each():
            pid = player.id
            for bad in ("wing", None, 5, ["forward"]):
                with self.assertRaises(ValidationError, msg=(label, repr(bad))) as ctx:
                    setup.update_player(pid, name="Should Not Land",
                                        position=bad, actor_id="a")
                self.assertEqual(ctx.exception.details.get("field"), "position",
                                 (label, repr(bad)))
                self.assertEqual(ctx.exception.details.get("reason"),
                                 "invalid_position", (label, repr(bad)))
                fresh = store.get_player(pid)
                self.assertEqual(fresh.name, "Jordn Le", (label, repr(bad)))   # rolled back
                self.assertEqual(fresh.position, Position.FORWARD, (label, repr(bad)))
            self.assertEqual(len(_player_updated_audits(store)), 0, label)

    def test_invalid_position_on_create_is_field_error_and_no_player(self):
        from hockey_scheduler.domain.errors import ValidationError
        for label, store, api, setup, player in self._each():
            before = len(store.players_for_team("home"))
            for bad in ("wing", None, 5):
                with self.assertRaises(ValidationError, msg=(label, repr(bad))) as ctx:
                    setup.add_player("home", "Ghost", bad, actor_id="a")
                self.assertEqual(ctx.exception.details.get("reason"),
                                 "invalid_position", (label, repr(bad)))
                self.assertEqual(ctx.exception.details.get("field"), "position",
                                 (label, repr(bad)))
            self.assertEqual(len(store.players_for_team("home")), before, label)

    def test_valid_position_string_accepted_on_create_and_edit(self):
        # The canonical validator also parses a valid position string (the
        # import-aligned form), so a direct string caller still works.
        for label, store, api, setup, player in self._each():
            created = setup.add_player("home", "Skater", "defense", actor_id="a")
            self.assertEqual(created.position, Position.DEFENSE, label)
            updated = setup.update_player(player.id, position="goalie",
                                          actor_id="a")
            self.assertEqual(updated.position, Position.GOALIE, label)

    def test_invalid_name_on_edit_is_field_error_and_atomic(self):
        # A direct service edit must not str()-coerce a non-string name into
        # "False"/"0"/"[]"/"{}", nor accept a blank one — each is a field-level
        # invalid_name, and being one transaction the same-call jersey change
        # and the audit stay unchanged.
        from hockey_scheduler.domain.errors import ValidationError
        for label, store, api, setup, player in self._each():
            pid = player.id
            for bad in (False, 0, [], {}, "", "   ", None):
                with self.assertRaises(ValidationError, msg=(label, repr(bad))) as ctx:
                    setup.update_player(pid, name=bad, jersey_number=8,
                                        actor_id="a")
                self.assertEqual(ctx.exception.details.get("field"), "name",
                                 (label, repr(bad)))
                self.assertEqual(ctx.exception.details.get("reason"),
                                 "invalid_name", (label, repr(bad)))
                fresh = store.get_player(pid)
                self.assertEqual(fresh.name, "Jordn Le", (label, repr(bad)))
                self.assertEqual(fresh.jersey_number, 17, (label, repr(bad)))  # rolled back
            self.assertEqual(len(_player_updated_audits(store)), 0, label)

    def test_invalid_name_on_create_is_field_error_and_no_player(self):
        from hockey_scheduler.domain.errors import ValidationError
        for label, store, api, setup, player in self._each():
            before = len(store.players_for_team("home"))
            for bad in (False, 0, [], {}, "", "   ", None):
                with self.assertRaises(ValidationError, msg=(label, repr(bad))) as ctx:
                    setup.add_player("home", bad, Position.FORWARD, actor_id="a")
                self.assertEqual(ctx.exception.details.get("reason"),
                                 "invalid_name", (label, repr(bad)))
                self.assertEqual(ctx.exception.details.get("field"), "name",
                                 (label, repr(bad)))
            self.assertEqual(len(store.players_for_team("home")), before, label)


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

    def _shoots_of(self, c, pid):
        _s, _h, players = self._req(c, "GET", "/api/players")
        return next(x for x in players if x["id"] == pid).get("shoots")

    def test_edit_valid_shoots_normalizes_and_null_clears(self):
        c = self._admin()
        p = self._a_player(c)
        # Lowercase "l" over the wire is stored canonically as "L".
        s, _h, _b = self._req(
            c, "POST", f"/api/v2/setup/player/{p['id']}/update", {"shoots": "l"})
        self.assertEqual(s, 200)
        self.assertEqual(self._shoots_of(c, p["id"]), "L")
        # Explicit null clears the hand.
        s, _h, _b = self._req(
            c, "POST", f"/api/v2/setup/player/{p['id']}/update", {"shoots": None})
        self.assertEqual(s, 200)
        self.assertIsNone(self._shoots_of(c, p["id"]))

    def test_edit_invalid_shoots_string_is_400_field_error(self):
        c = self._admin()
        p = self._a_player(c)
        before = self._shoots_of(c, p["id"])
        s, _h, body = self._req(
            c, "POST", f"/api/v2/setup/player/{p['id']}/update",
            {"name": "Renamed Too", "shoots": "banana"})
        self.assertEqual(s, 400)
        self.assertEqual(body["error"]["details"]["reason"], "invalid_shoots")
        self.assertEqual(body["error"]["details"]["field"], "shoots")
        # Atomic: neither the hand nor the name in the same request landed.
        self.assertEqual(self._shoots_of(c, p["id"]), before)
        _s, _h, players = self._req(c, "GET", "/api/players")
        self.assertNotEqual(
            next(x for x in players if x["id"] == p["id"])["name"], "Renamed Too")

    def test_edit_wrong_type_shoots_is_400(self):
        c = self._admin()
        p = self._a_player(c)
        # A number is the wrong JSON type entirely — caught at the HTTP schema.
        s, _h, body = self._req(
            c, "POST", f"/api/v2/setup/player/{p['id']}/update", {"shoots": 5})
        self.assertEqual(s, 400)
        self.assertEqual(body["error"]["details"]["reason"], "wrong_type")
        self.assertEqual(body["error"]["details"]["field"], "shoots")

    def test_edit_wrong_type_email_is_400(self):
        c = self._admin()
        p = self._a_player(c)
        # A non-string/non-null email (number, bool, array) is the wrong JSON
        # type — rejected at the HTTP schema with a stable field-level 400,
        # never coerced into a silent retire.
        for bad in (5, True, ["a@b.co"]):
            s, _h, body = self._req(
                c, "POST", f"/api/v2/setup/player/{p['id']}/update",
                {"email": bad})
            self.assertEqual(s, 400, bad)
            self.assertEqual(body["error"]["details"]["reason"], "wrong_type", bad)
            self.assertEqual(body["error"]["details"]["field"], "email", bad)

    def test_edit_invalid_position_string_is_400_field_error(self):
        c = self._admin()
        p = self._a_player(c)
        # A well-typed but non-canonical position string reaches the service's
        # canonical validator → invalid_position on field "position".
        s, _h, body = self._req(
            c, "POST", f"/api/v2/setup/player/{p['id']}/update",
            {"name": "Kept", "position": "wing"})
        self.assertEqual(s, 400)
        self.assertEqual(body["error"]["details"]["reason"], "invalid_position")
        self.assertEqual(body["error"]["details"]["field"], "position")

    def test_edit_null_and_wrong_type_position_are_400_on_field(self):
        c = self._admin()
        p = self._a_player(c)
        # Explicit null and a wrong JSON type are caught at the HTTP schema
        # (position is typed str) — a stable 400 that still names field
        # "position", not a fieldless enum error.
        for bad in (None, 5):
            s, _h, body = self._req(
                c, "POST", f"/api/v2/setup/player/{p['id']}/update",
                {"position": bad})
            self.assertEqual(s, 400, repr(bad))
            self.assertEqual(body["error"]["details"]["field"], "position", repr(bad))

    def test_edit_blank_name_is_400_field_error(self):
        c = self._admin()
        p = self._a_player(c)
        # A blank string is well-typed but empty → the service's canonical
        # name validator makes it invalid_name on field "name".
        s, _h, body = self._req(
            c, "POST", f"/api/v2/setup/player/{p['id']}/update",
            {"name": "   ", "jersey_number": 8})
        self.assertEqual(s, 400)
        self.assertEqual(body["error"]["details"]["reason"], "invalid_name")
        self.assertEqual(body["error"]["details"]["field"], "name")

    def test_edit_null_and_wrong_type_name_are_400_on_field(self):
        c = self._admin()
        p = self._a_player(c)
        # Explicit null and a wrong JSON type are caught at the HTTP schema
        # (name is typed str) — a stable 400 that still names field "name".
        for bad in (None, 5, ["x"]):
            s, _h, body = self._req(
                c, "POST", f"/api/v2/setup/player/{p['id']}/update",
                {"name": bad})
            self.assertEqual(s, 400, repr(bad))
            self.assertEqual(body["error"]["details"]["field"], "name", repr(bad))

    def test_create_invalid_shoots_is_400_field_error(self):
        c = self._admin()
        team_id = self._a_player(c)["team_id"]
        s, _h, body = self._req(c, "POST", "/api/v2/setup/player", {
            "team_id": team_id, "name": "New Skater", "position": "forward",
            "shoots": "both"})
        self.assertEqual(s, 400)
        self.assertEqual(body["error"]["details"]["reason"], "invalid_shoots")
        self.assertEqual(body["error"]["details"]["field"], "shoots")


if __name__ == "__main__":
    unittest.main()
