"""#273 review round 2 finding 1: every Player-carrying facade payload must
route through ``_player_dto`` (never a bare ``_serialize(Player)``), and the
``include_identity`` opt-in must never be reachable positionally.

Two holes, both closed in ``ApiService`` (``hockey_scheduler/api/service.py``):

* ``delete_player()`` and ``assign_player_team()`` returned a raw
  ``_serialize(Player)`` — the private ``birthdate``/``registration_number``
  rode straight through on a delete's or a move's echoed row, on every store.
* ``list_players()`` inserted the new ``include_identity`` parameter BEFORE
  the pre-existing ``user_id`` argument, so an unchanged legacy positional
  call such as ``list_players("t1", False, "legacy-user")`` silently
  reinterpreted its own third argument as a truthy ``include_identity`` —
  opting into both private fields AND losing ``user_id`` (which defeats the
  #369 Program-scoping gate downstream, since ``role`` then never arrives
  either).

Covers, on Memory/SQLite/[PostgreSQL]:

* a table over create/update/deactivate/reactivate/move/list/delete with
  populated PII, proving none of the seven ever leaks it by default;
* the positive control (``include_identity=True`` still returns the values,
  proving the DTO machinery itself is intact, not merely never exercised);
* every legacy positional call shape ``list_players`` supported before
  ``include_identity`` existed, including the reviewer's own example;
* ``include_identity`` is genuinely keyword-only (a 6th positional argument
  is a hard ``TypeError``, not a silent misassignment).

Plus one real-HTTP class exercising v1 AND v2 player move, and v2 player
delete (v1 player delete redirects callers to v2 — see
``_handle_setup``'s ``moved_to_v2`` branch — so only v2 ever writes), over a
REAL ``ThreadingHTTPServer`` running the REAL ``Handler``, proving the wire
response — not just the facade return value — never carries the private
fields either.

Scope note: HTTP-level opt-in wiring (an operator ``include_identity=true``
query param) is explicitly deferred, like ``include_email`` was before #268
wired it — no route in ``web/server.py`` passes ``include_identity`` yet, so
every HTTP player response is necessarily the default-minimized shape today.
Synchronous access auditing of a privileged identity read is CLOSED as of
#424 audit-wiring: ``include_identity`` now routes through the same #426
policy+audit boundary ``include_email`` already used — BIRTHDATE and
REGISTRATION_NUMBER are independent ``_sensitive_read_allowed`` checks
(mask-not-refuse on denial, one durable DENIED row per masked category, one
ALLOWED row per disclosed player per allowed category), proved in
``test_athlete_identity.py``'s ``FacadePrivacyTest`` and exercised again
here in ``test_explicit_identity_opt_in_still_returns_the_values`` /
``test_include_identity_keyword_still_works`` (now both authorized calls,
since a bare call is masked). HTTP role enforcement and the operator UI
remain separate, later follow-up items.
"""

import inspect
import json
import os
import threading
import unittest
import urllib.error
import urllib.request
from http.cookiejar import CookieJar
from http.server import ThreadingHTTPServer

from helpers import BACKEND  # noqa: F401  (ensures sys.path is set up)

from hockey_scheduler.api.service import ApiService
from hockey_scheduler.domain import Program, Role, Team
from hockey_scheduler.store import InMemoryStore, SqlStore

BIRTHDATE = "1999-05-05"
REGISTRATION = "SECRET-REG-1"


def _backends():
    stores = [("memory", InMemoryStore()), ("sqlite", SqlStore(":memory:"))]
    url = os.environ.get("TEST_DATABASE_URL")
    if url:
        pg = SqlStore(url)
        pg.reset_schema()
        stores.append(("postgres", pg))
    return stores


class _FacadeTestBase(unittest.TestCase):
    def _each(self):
        for label, store in _backends():
            with self.subTest(backend=label):
                store.add_team(Team(id="t1", name="Team One"))
                store.add_team(Team(id="t2", name="Team Two"))
                api = ApiService(store)
                try:
                    yield label, api
                finally:
                    if isinstance(store, SqlStore):
                        store.close()

    def _each_with_program(self):
        """Like ``_each()``, but ``t1``/``t2`` belong to a real Program a
        LEAGUE_ADMIN can be authorized into (#424 audit-wiring): supplying
        a ``role`` to ``list_players`` also triggers the pre-existing #369
        Program-scoping gate, which resolves to zero Teams on a Program-
        less fixture regardless of privacy grants."""
        for label, store in _backends():
            with self.subTest(backend=label):
                store.add_program(Program(id="pr", name="P"))
                store.add_team(Team(id="t1", name="Team One",
                                    program_id="pr"))
                store.add_team(Team(id="t2", name="Team Two",
                                    program_id="pr"))
                api = ApiService(store)
                try:
                    yield label, api
                finally:
                    if isinstance(store, SqlStore):
                        store.close()


class PlayerPayloadTableTest(_FacadeTestBase):
    """A table over every Player-returning facade method, populated PII."""

    def test_create_update_deactivate_reactivate_move_list_delete_never_leak(self):
        for label, api in self._each():
            created = api.create_player(
                "t1", first_name="Priv", last_name="Fields",
                position="forward", birthdate=BIRTHDATE,
                registration_number=REGISTRATION, skill_rating=4,
                actor_id="op")
            self.assertNotIn("error", created, (label, created))
            pid = created["id"]

            ordered_rows = [
                ("create", created),
                ("update", api.update_player(
                    pid, preferred_name="P", actor_id="op")),
                ("deactivate", api.set_player_active(
                    pid, False, actor_id="op")),
                ("reactivate", api.set_player_active(
                    pid, True, actor_id="op")),
                ("move", api.assign_player_team(pid, "t2", actor_id="op")),
                ("list", api.list_players(team_id="t2")[0]),
                ("delete", api.delete_player(pid, actor_id="op")),
            ]
            for op_name, row in ordered_rows:
                self.assertNotIn("error", row, (label, op_name, row))
                self.assertNotIn("birthdate", row, (label, op_name))
                self.assertNotIn("registration_number", row, (label, op_name))
                # Raw-substring check too: a leak riding under an unexpected
                # key (not just the two documented ones) is caught either way.
                dumped = json.dumps(row)
                self.assertNotIn(REGISTRATION, dumped, (label, op_name))
                self.assertNotIn(BIRTHDATE, dumped, (label, op_name))
                # Non-private identity stays visible — this is minimization,
                # not a blanket data blackout.
                if op_name != "delete":
                    self.assertEqual(row.get("first_name"), "Priv",
                                     (label, op_name))

    def test_explicit_identity_opt_in_still_returns_the_values(self):
        """Positive control: proves the DTO plumbing is exercised, not just
        never triggered by the table above. #424 audit-wiring: an
        authorized role is now required to actually see the values (a
        bare call masks them, see test_athlete_identity.py's
        FacadePrivacyTest for that path) -- _each_with_program supplies a
        real Program so the pre-existing #369 Program-scoping gate a
        supplied `role` also triggers does not itself hide the row."""
        for label, api in self._each_with_program():
            api.create_player(
                "t1", first_name="Priv", last_name="Fields",
                position="forward", birthdate=BIRTHDATE,
                registration_number=REGISTRATION, actor_id="op")
            row = api.list_players(
                team_id="t1", include_identity=True,
                role=Role.LEAGUE_ADMIN, user_id="admin1")[0]
            self.assertEqual(row["birthdate"], BIRTHDATE, label)
            self.assertEqual(row["registration_number"], REGISTRATION, label)


class ListPlayersPositionalCompatibilityTest(_FacadeTestBase):
    """The pre-#273 positional shape (team_id, include_email, user_id, role,
    scope) must still bind to the SAME parameters, and no positional slot
    may ever reach ``include_identity`` again."""

    def test_legacy_positional_shapes_never_leak_or_raise(self):
        for label, api in self._each():
            api.create_player(
                "t1", first_name="Priv", last_name="Fields",
                position="forward", birthdate=BIRTHDATE,
                registration_number=REGISTRATION, actor_id="op")
            variants = [
                ("team_id only", ("t1",)),
                ("+include_email False", ("t1", False)),
                ("+include_email True", ("t1", True)),
                # The reviewer's own example: an unchanged legacy call whose
                # third positional argument used to mean user_id.
                ("+user_id string", ("t1", False, "legacy-user")),
                ("+role None", ("t1", False, "legacy-user", None)),
                ("+scope None", ("t1", False, "legacy-user", None, None)),
            ]
            for name, args in variants:
                rows = api.list_players(*args)
                self.assertIsInstance(rows, list, (label, name, rows))
                for row in rows:
                    self.assertNotIn("birthdate", row, (label, name))
                    self.assertNotIn("registration_number", row, (label, name))

    def test_include_identity_rejects_a_positional_argument(self):
        """A 6th positional argument has no slot to bind to any more — it
        must raise TypeError, never silently become ``include_identity``."""
        for label, api in self._each():
            with self.assertRaises(TypeError, msg=label):
                api.list_players("t1", False, None, None, None, True)

    def test_include_identity_keyword_still_works(self):
        # #424 audit-wiring: positions 3/4 (user_id/role) now need to name
        # an authorized principal for include_identity's values to survive
        # the gate -- _each_with_program supplies the Program that role
        # needs to be authorized into (see its docstring).
        for label, api in self._each_with_program():
            api.create_player(
                "t1", first_name="Priv", last_name="Fields",
                position="forward", birthdate=BIRTHDATE,
                registration_number=REGISTRATION, actor_id="op")
            rows = api.list_players("t1", False, "admin1", Role.LEAGUE_ADMIN,
                                    None, include_identity=True)
            self.assertEqual(rows[0]["birthdate"], BIRTHDATE, label)


# =========================================================================== #
# Real HTTP: v1 + v2 player move, v2 player delete, over the real Handler.    #
# =========================================================================== #
class PlayerMoveDeleteHttpLeakTest(unittest.TestCase):
    """v1/v2 HTTP move + v2 HTTP delete: the wire response — not just the
    facade return value — must never carry the private fields either.

    Setup goes through the FACADE directly (``srv.STATE.api``, the exact
    object instance the HTTP server dispatches onto) rather than HTTP: the
    v2 player CREATE route's strict body schema does not accept identity
    cells yet (HTTP wiring is explicitly out of this slice's scope), so a
    populated-PII fixture can only be built one layer down. Only login,
    context selection, and the two calls under test go over real HTTP.
    """

    @classmethod
    def setUpClass(cls):
        srv = __import__("hockey_scheduler.web.server", fromlist=["x"])
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
        cls.httpd.server_close()

    # -- harness (copied idiom from test_players_http_scope.py) -------------
    def _client(self):
        return urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(CookieJar()))

    def _req(self, opener, method, path, body=None):
        url = f"http://127.0.0.1:{self.port}{path}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with opener.open(req) as r:
                raw = r.read()
                return r.status, raw.decode(), (json.loads(raw) if raw else {})
        except urllib.error.HTTPError as e:
            raw = e.read()
            return e.code, raw.decode(), (json.loads(raw) if raw else {})

    def _admin(self):
        c = self._client()
        status, _text, resp = self._req(
            c, "POST", "/api/auth/login",
            {"username": "admin", "password": "demo"})
        self.assertEqual(status, 200, resp)
        return c

    def _use(self, c, program_id, season_id):
        status, _text, resp = self._req(
            c, "POST", "/api/context",
            {"program_id": program_id, "season_id": season_id})
        self.assertEqual(status, 200, resp)

    def _fixture(self):
        """Program -> Season -> League -> Club -> two Teams, built through
        the facade directly, plus one Player with populated PII on team_a."""
        api = self.srv.STATE.api
        program = api.create_program("HTTP-Leak Program", actor_id="fixture")
        season = api.create_season(
            program["id"], "HTTP-Leak Season", actor_id="fixture")
        league = api.create_league(
            season["id"], "HTTP-Leak League", actor_id="fixture")
        club = api.create_club("HTTP-Leak Club", actor_id="fixture")
        team_a = api.create_team(club_id=club["id"], name="Team A",
                                 league_id=league["id"], actor_id="fixture")
        team_b = api.create_team(club_id=club["id"], name="Team B",
                                 league_id=league["id"], actor_id="fixture")
        player = api.create_player(
            team_a["id"], first_name="Priv", last_name="Fields",
            position="forward", birthdate=BIRTHDATE,
            registration_number=REGISTRATION, actor_id="fixture")
        for row in (program, season, league, club, team_a, team_b, player):
            self.assertNotIn("error", row, row)
        return program, season, team_a, team_b, player

    def test_v1_and_v2_move_never_leak_identity_over_the_wire(self):
        program, season, team_a, team_b, player = self._fixture()
        c = self._admin()
        self._use(c, program["id"], season["id"])

        status, raw, resp = self._req(
            c, "POST", f"/api/setup/player/{player['id']}/assign-team",
            {"team_id": team_b["id"]})
        self.assertEqual(status, 200, (raw, resp))
        self.assertNotIn(REGISTRATION, raw, raw)
        self.assertNotIn(BIRTHDATE, raw, raw)
        self.assertNotIn("birthdate", resp, resp)
        self.assertNotIn("registration_number", resp, resp)
        self.assertEqual(resp.get("team_id"), team_b["id"], resp)

        status, raw, resp = self._req(
            c, "POST", f"/api/v2/setup/player/{player['id']}/assign-team",
            {"team_id": team_a["id"]})
        self.assertEqual(status, 200, (raw, resp))
        self.assertNotIn(REGISTRATION, raw, raw)
        self.assertNotIn(BIRTHDATE, raw, raw)
        self.assertNotIn("birthdate", resp, resp)
        self.assertNotIn("registration_number", resp, resp)
        self.assertEqual(resp.get("team_id"), team_a["id"], resp)

    def test_v1_player_delete_redirects_to_v2_with_no_leak(self):
        # #232/#271: v1 player delete is a 409 moved_to_v2 pointer, never a
        # real delete -- trivially leak-free, but pinned so a future
        # regression that DID wire it up would be caught by every other
        # assertion in this file, not silently by this one going stale.
        program, season, team_a, _team_b, player = self._fixture()
        c = self._admin()
        self._use(c, program["id"], season["id"])
        status, raw, resp = self._req(
            c, "POST", f"/api/setup/player/{player['id']}/delete", {})
        self.assertEqual(status, 409, (raw, resp))
        self.assertEqual(resp.get("error", {}).get("code"), "moved_to_v2", resp)
        self.assertNotIn(REGISTRATION, raw, raw)
        self.assertNotIn(BIRTHDATE, raw, raw)

    def test_v2_player_delete_never_leaks_identity_over_the_wire(self):
        program, season, team_a, _team_b, player = self._fixture()
        c = self._admin()
        self._use(c, program["id"], season["id"])
        status, raw, resp = self._req(
            c, "POST", f"/api/v2/setup/player/{player['id']}/delete", {})
        self.assertEqual(status, 200, (raw, resp))
        self.assertNotIn(REGISTRATION, raw, raw)
        self.assertNotIn(BIRTHDATE, raw, raw)
        self.assertNotIn("birthdate", resp, resp)
        self.assertNotIn("registration_number", resp, resp)
        self.assertTrue(resp.get("deleted") or resp.get("id") == player["id"],
                        resp)


class ListPlayersSignatureShapeTest(unittest.TestCase):
    """Documents the intended shape directly (belt-and-suspenders alongside
    the behavioral proofs above): ``include_identity`` is the LAST parameter
    in source order. ``@catch`` does not use ``functools.wraps``, so this
    reads the real function object off the module rather than the
    decorator-wrapped bound method (which would only ever show
    ``(*args, **kwargs)``)."""

    def test_include_identity_is_the_last_declared_parameter(self):
        import hockey_scheduler.api.service as service_module
        source = inspect.getsource(service_module)
        # Locate the real def (not the decorator) and confirm textually that
        # every pre-existing positional parameter precedes a keyword-only
        # include_identity, matching the runtime proofs above.
        start = source.index("def list_players(")
        end = source.index(") -> List[dict]:", start)
        signature_text = source[start:end]
        self.assertIn("*,", signature_text)
        self.assertLess(signature_text.index("*,"),
                        signature_text.index("include_identity"))
        for name in ("team_id", "include_email", "user_id", "role", "scope"):
            self.assertLess(signature_text.index(name),
                            signature_text.index("*,"),
                            f"{name} must precede the keyword-only marker")


if __name__ == "__main__":
    unittest.main()
