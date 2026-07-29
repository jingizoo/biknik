"""The setup hierarchy is Program-scoped, and its writes cannot cross (#367 prereq).

``get_setup_hierarchy_v2`` was installation-wide. That was not merely a display
inconsistency with the already-scoped Setup Records: ``app.js`` builds
``allPermLeagues`` from this payload, and that array is the option list for the
Team drawer's "Permanent league" select — whose options are labelled
"<program> · <league>" precisely because they spanned Programs. An operator
working in Program B could therefore pick Program A's League and create a Team
under it: a cross-Program WRITE, not just a read leak.

Both halves are covered here, and they are deliberately independent:

* the READ narrows what the picker can offer;
* the WRITE is refused at the route regardless, because ``league_id`` arrives in
  a request body and a client can send any value it likes. A picker is a
  convenience; it is never the authorization boundary. (The same lesson as the
  player IDOR, where an explicit ``team_id`` bypassed a ceiling that the
  unfiltered form applied.)

The refusal reuses the context layer's existing generic League message, so an
inaccessible League is indistinguishable from a nonexistent one and this never
becomes an existence oracle for another Program's Leagues.
"""

import json
import os
import threading
import unittest
import urllib.error
import urllib.request
from http.cookiejar import CookieJar
from http.server import ThreadingHTTPServer

from helpers import BACKEND  # noqa: F401  (ensures sys.path is set up)

from hockey_scheduler.api import ApiService
from hockey_scheduler.domain import Role
from hockey_scheduler.store import InMemoryStore, SqlStore


def _backends():
    yield "memory", InMemoryStore()
    yield "sqlite", SqlStore(":memory:")
    url = os.environ.get("TEST_DATABASE_URL")
    if url:
        store = SqlStore(url)
        store.clear_all_data()
        yield "postgres", store


def _close(store):
    if isinstance(store, SqlStore):
        store.close()


def _two_programs(api):
    """Two complete Programs, each with its own Season + League, plus one
    shared Club (Clubs are Program-independent, so the same Club is legitimately
    usable from either side -- which keeps the Club out of the way of what these
    tests actually measure)."""
    out = {}
    for tag in ("A", "B"):
        program = api.create_program(f"Prog {tag}", "US", "UTC")
        season = api.create_season(program["id"], f"Season {tag}")
        league = api.create_league(season["id"], f"League {tag}")
        out[tag] = {"program": program, "season": season, "league": league}
    out["club"] = api.create_club("Shared Club")
    return out


class HierarchyReadScopeTest(unittest.TestCase):
    """The tree itself collapses to the ACTIVE Program."""

    def test_hierarchy_shows_only_the_active_program(self):
        for backend, store in _backends():
            with self.subTest(backend=backend):
                api = ApiService(store)
                fx = _two_programs(api)
                admin = ("admin", Role.LEAGUE_ADMIN, {})

                api.set_active_context(*admin, fx["A"]["program"]["id"],
                                       fx["A"]["season"]["id"])
                tree = api.get_setup_hierarchy_v2(*admin)
                self.assertNotIn("error", tree, tree)
                names = [p["name"] for p in tree["programs"]]
                self.assertEqual(
                    names, ["Prog A"],
                    f"[{backend}] the hierarchy must collapse to the ACTIVE "
                    f"Program; got {names}")

                # ...and it FLIPS, rather than merely adding to the first view.
                api.set_active_context(*admin, fx["B"]["program"]["id"],
                                       fx["B"]["season"]["id"])
                names = [p["name"]
                         for p in api.get_setup_hierarchy_v2(*admin)["programs"]]
                self.assertEqual(names, ["Prog B"], backend)

                # The no-context form is deliberately unchanged: several
                # internal callers and the hierarchy tests inspect whole-store
                # state through it.
                legacy = [p["name"]
                          for p in api.get_setup_hierarchy_v2()["programs"]]
                self.assertEqual(sorted(legacy), ["Prog A", "Prog B"], backend)
                _close(store)


class HierarchyWriteScopeHttpTest(unittest.TestCase):
    """The write path over real authenticated HTTP -- the part that matters,
    since a picker can be bypassed but the route cannot."""

    @classmethod
    def setUpClass(cls):
        srv = __import__("hockey_scheduler.web.server", fromlist=["x"])
        cls.srv = srv
        srv.STATE.reset(seed=False)
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), srv.Handler)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

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
        req = urllib.request.Request(url, data=data, method=method)
        if data is not None:
            req.add_header("Content-Type", "application/json")
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

    def _v2(self, c, entity, body):
        status, resp = self._req(c, "POST", f"/api/v2/setup/{entity}", body)
        self.assertEqual(status, 200, (entity, resp))
        return resp

    def _fixture(self, c, tag):
        program = self._v2(c, "program", {"name": f"WS {tag} Prog"})
        season = self._v2(c, "season",
                          {"program_id": program["id"], "name": f"WS {tag} S"})
        league = self._v2(c, "league",
                          {"season_id": season["id"], "name": f"WS {tag} L"})
        return {"program": program, "season": season, "league": league}

    def test_cross_program_team_create_is_refused_generically(self):
        c = self._admin()
        a = self._fixture(c, "A")
        b = self._fixture(c, "B")
        club = self._v2(c, "club", {"name": "WS Club"})

        # Working in Program B...
        status, _ = self._req(c, "POST", "/api/context",
                              {"program_id": b["program"]["id"],
                               "season_id": b["season"]["id"]})
        self.assertEqual(status, 200)

        # Snapshot BEFORE the refused attempt: the required proof is not only
        # that the response is a refusal, but that the attempt left NOTHING
        # behind -- no Team, and no audit row either. A guard that rejects the
        # response while still writing (or still recording an actor + entity in
        # the activity feed) would satisfy a status-code assertion and still
        # leak that the attempt happened, and to what.
        store = self.srv.STATE.api.store
        teams_before = {t.id for t in store.all_teams()}
        audit_before = len(store.all_setup_audit())

        # ...creating under Program A's League is refused.
        status, resp = self._req(c, "POST", "/api/v2/setup/team",
                                 {"club_id": club["id"],
                                  "league_id": a["league"]["id"],
                                  "name": "Cross-Program Team"})
        self.assertEqual(
            status, 404,
            f"a Team was created under another Program's League: {resp}")
        self.assertEqual(resp["error"]["details"]["reason"],
                         "league_not_accessible", resp)

        self.assertEqual(
            {t.id for t in store.all_teams()}, teams_before,
            "the refused create still wrote a Team row")
        self.assertEqual(
            len(store.all_setup_audit()), audit_before,
            "the refused create still appended a setup-audit row -- a refusal "
            "must not record the attempt, or the audit feed becomes its own "
            "disclosure of what was tried and against which League")
        self.assertFalse(
            any(t.name == "Cross-Program Team" for t in store.all_teams()),
            "the refused Team name is present in the store")

        # A NONEXISTENT League id is refused identically -- so the response
        # cannot be used to probe whether another Program's League exists.
        status_missing, resp_missing = self._req(
            c, "POST", "/api/v2/setup/team",
            {"club_id": club["id"], "league_id": "league_does_not_exist",
             "name": "Ghost"})
        self.assertEqual((status_missing, resp_missing), (status, resp),
                         "an inaccessible League must be indistinguishable "
                         "from a nonexistent one")

        # Positive control: the SAME request against the active Program's own
        # League succeeds, so the refusals above are not a blanket failure.
        ok = self._v2(c, "team", {"club_id": club["id"],
                                  "league_id": b["league"]["id"],
                                  "name": "Own-Program Team"})
        # Assert on program_id, not league_id: the v2 Team DTO carries the
        # Program (the service derives it from the League), and where the Team
        # actually LANDED is the stronger claim anyway.
        self.assertEqual(ok["program_id"], b["program"]["id"], ok)

        # Switching to Program A makes ITS League writable and B's refused --
        # the gate follows the context rather than being a fixed allow-list.
        self._req(c, "POST", "/api/context",
                  {"program_id": a["program"]["id"],
                   "season_id": a["season"]["id"]})
        ok_a = self._v2(c, "team", {"club_id": club["id"],
                                    "league_id": a["league"]["id"],
                                    "name": "Now Allowed"})
        self.assertEqual(ok_a["program_id"], a["program"]["id"], ok_a)
        status_b, _ = self._req(c, "POST", "/api/v2/setup/team",
                                {"club_id": club["id"],
                                 "league_id": b["league"]["id"],
                                 "name": "Now Refused"})
        self.assertEqual(status_b, 404)

    def test_hierarchy_route_is_scoped_to_the_active_program(self):
        c = self._admin()
        a = self._fixture(c, "RA")
        self._fixture(c, "RB")
        self._req(c, "POST", "/api/context",
                  {"program_id": a["program"]["id"],
                   "season_id": a["season"]["id"]})
        status, tree = self._req(c, "GET", "/api/v2/setup/hierarchy")
        self.assertEqual(status, 200, tree)
        names = [p["name"] for p in tree["programs"]]
        self.assertEqual(
            names, ["WS RA Prog"],
            f"the hierarchy route leaked other Programs: {names}")


if __name__ == "__main__":
    unittest.main()
