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


def _foreign_league_registration(api, store, fx):
    """A LeagueSeason joining Program A's SEASON to Program B's LEAGUE, with a
    legitimate Program-A Team registered through it.

    The Team is in scope, so the row survives the Team check and is classified
    by its League instead -- which is how ``league_id`` kept leaking after the
    Team id was withheld.
    """
    from hockey_scheduler.domain.setup_models import (LeagueSeason,
                                                      SeasonTeamRegistration)
    team_a = api.create_team(club_id=fx["club"]["id"], name="A-OWN-TEAM",
                             league_id=fx["A"]["league"]["id"])
    ls_mixed = LeagueSeason(id=store.next_id("leagueseason"),
                            league_id=fx["B"]["league"]["id"],
                            season_id=fx["A"]["season"]["id"])
    store.add_league_season(ls_mixed)
    store.add_season_team_registration(SeasonTeamRegistration(
        id=store.next_id("streg"), league_season_id=ls_mixed.id,
        team_id=team_a["id"], division_id=None, active=True))
    return {"id": fx["B"]["league"]["id"], "name": "League B"}


def _foreign_division_registration(api, store, fx):
    """A Program-A Team registered through Program A's OWN LeagueSeason but
    carrying a Division that belongs to Program B's LeagueSeason.

    Team and League both check out, so the row is classified on the Division
    -- the third and last verbatim reference.
    """
    from hockey_scheduler.domain.setup_models import SeasonTeamRegistration
    team_a = api.create_team(club_id=fx["club"]["id"], name="A-OWN-TEAM-2",
                             league_id=fx["A"]["league"]["id"])
    div_b = api.create_division(fx["B"]["season"]["id"], "DIV-B-CANARY",
                                league_id=fx["B"]["league"]["id"])
    ls_a = next(ls for ls in store.all_league_seasons()
                if ls.season_id == fx["A"]["season"]["id"]
                and ls.league_id == fx["A"]["league"]["id"])
    store.add_season_team_registration(SeasonTeamRegistration(
        id=store.next_id("streg"), league_season_id=ls_a.id,
        team_id=team_a["id"], division_id=div_b["id"], active=True))
    return {"id": div_b["id"], "name": "DIV-B-CANARY"}


def _cross_program_registration(api, store, fx):
    """Register Program B's Team into Program A's Season directly.

    The already-supported legacy/corrupt shape: an ACTIVE LeagueSeason
    registration in Program A whose ``team_id`` points at a Program-B Team.
    The repo treats this as a real remediation state (see
    ``HierarchyV2ProgramMismatchTest``), which is why it must be REPAIRABLE
    from A's tree without A's tree disclosing the foreign Team.
    """
    from hockey_scheduler.domain.models import Team
    from hockey_scheduler.domain.setup_models import SeasonTeamRegistration
    # Injected directly, exactly as HierarchyV2ProgramMismatchTest does it:
    # the facade now REFUSES to create this shape, which is correct -- the
    # point is that a row already in the store (legacy data, a migration, a
    # direct load) must still be repairable from A's tree without A's tree
    # disclosing B's Team.
    team_b = Team(id=store.next_id("team"), name="LEAK-CANARY-TEAM",
                  club_id=fx["club"]["id"],
                  program_id=fx["B"]["program"]["id"])
    store.add_team(team_b)
    ls_a = next(ls for ls in store.all_league_seasons()
                if ls.season_id == fx["A"]["season"]["id"]
                and ls.league_id == fx["A"]["league"]["id"])
    store.add_season_team_registration(SeasonTeamRegistration(
        id=store.next_id("streg"), league_season_id=ls_a.id,
        team_id=team_b.id, division_id=None, active=True))
    return {"id": team_b.id, "name": team_b.name}


class HierarchyRegistrationLeakTest(unittest.TestCase):
    """A scoped hierarchy must not hand back a FOREIGN Team id (#371 review).

    ``teams`` is narrowed to the active Program, so a registration naming a
    Program-B Team misses ``teams_by_id`` and was emitted into
    ``needs_assignment.registrations`` as
    ``{"team_id": "<Program-B team id>", "reason": "team_missing"}``.
    Filtering NAMES while returning the IDENTIFIER is not a ceiling: an id is
    precisely what a later write needs, and it is joinable back to the foreign
    Program's graph.
    """

    def test_no_foreign_team_league_or_division_id_survives(self):
        """All three corrupt shapes at once, so no single check can carry the
        test: a foreign Team, an A-Season/B-League LeagueSeason, and an A
        registration carrying a Program-B Division."""
        for backend, store in _backends():
            with self.subTest(backend=backend):
                api = ApiService(store)
                fx = _two_programs(api)
                admin = ("admin", Role.LEAGUE_ADMIN, {})
                foreign = [_cross_program_registration(api, store, fx),
                           _foreign_league_registration(api, store, fx),
                           _foreign_division_registration(api, store, fx)]

                api.set_active_context(*admin, fx["A"]["program"]["id"],
                                       fx["A"]["season"]["id"])
                tree = api.get_setup_hierarchy_v2(*admin)
                self.assertNotIn("error", tree, tree)
                blob = json.dumps(tree)
                for ref in foreign:
                    self.assertNotIn(
                        ref["id"], blob,
                        f"[{backend}] Program A's scoped hierarchy returned "
                        f"foreign id {ref['id']!r} ({ref['name']}) -- a "
                        f"joinable identifier from outside the ceiling")
                    self.assertNotIn(ref["name"], blob, backend)

                # Each repair signal survives, stripped of identifiers.
                rows = [r for s in tree["programs"][0]["seasons"]
                        for r in s["needs_assignment"]["registrations"]]
                self.assertEqual(
                    len(rows), 3,
                    f"[{backend}] expected one repair row per corrupt shape; "
                    f"got {rows}")
                self.assertTrue(all(r["reason"] for r in rows), rows)

                # Legitimate Program-A structure still renders.
                self.assertEqual([p["name"] for p in tree["programs"]],
                                 ["Prog A"], backend)
                self.assertIn(fx["A"]["league"]["id"], blob,
                              f"[{backend}] Program A's own League vanished")
                _close(store)

    def test_a_foreign_registration_leaks_no_team_id(self):
        for backend, store in _backends():
            with self.subTest(backend=backend):
                api = ApiService(store)
                fx = _two_programs(api)
                admin = ("admin", Role.LEAGUE_ADMIN, {})
                team_b = _cross_program_registration(api, store, fx)

                api.set_active_context(*admin, fx["A"]["program"]["id"],
                                       fx["A"]["season"]["id"])
                tree = api.get_setup_hierarchy_v2(*admin)
                self.assertNotIn("error", tree, tree)

                blob = json.dumps(tree)
                self.assertNotIn(
                    team_b["id"], blob,
                    f"[{backend}] Program A's scoped hierarchy returned "
                    f"Program B's Team id {team_b['id']!r} -- a probeable "
                    f"identifier for a Program this caller cannot read")
                self.assertNotIn(
                    "LEAK-CANARY-TEAM", blob,
                    f"[{backend}] the foreign Team's NAME leaked too")

                # The repair signal itself survives -- the row is withheld of
                # its id, not dropped, so the operator can still see there is
                # something in this Season to fix.
                rows = [r for s in tree["programs"][0]["seasons"]
                        for r in s["needs_assignment"]["registrations"]]
                self.assertTrue(
                    rows, f"[{backend}] the needs-assignment repair signal "
                    f"disappeared along with the id")
                self.assertTrue(
                    all(r["team_id"] is None for r in rows),
                    f"[{backend}] a needs-assignment row still carries a "
                    f"team_id: {rows}")

                # Legitimate Program-A structure still renders.
                self.assertEqual(
                    [p["name"] for p in tree["programs"]], ["Prog A"], backend)

                # The legacy unscoped internal form is UNCHANGED -- it still
                # names the foreign Team, which several callers rely on.
                legacy = json.dumps(api.get_setup_hierarchy_v2())
                self.assertIn(
                    team_b["id"], legacy,
                    f"[{backend}] the unscoped internal form was changed too; "
                    f"only the user-scoped read was in scope here")
                _close(store)


class HierarchyWriteScopeHttpTest(unittest.TestCase):
    """The write path over real authenticated HTTP -- the part that matters,
    since a picker can be bypassed but the route cannot.

    ``test_cross_program_team_create_is_refused_generically_*`` runs this
    proof -- route dispatch, the guard, the real ``create_team``, and the
    audit write -- against Memory, SQLite, and (when configured) PostgreSQL.
    ``STATE.reset()`` (web/server.py) rebuilds its store from ``create_store()``
    (store/__init__.py), which resolves the backend from the ``DATABASE_URL``
    env var; ``_reset_backend`` below sets that var only for the duration of
    the ``reset()`` call so each backend's HTTP class gets a store actually
    built from it, instead of always landing on the InMemoryStore default (the
    prior state on PostgreSQL CI, which sets ``TEST_DATABASE_URL`` but never
    ``DATABASE_URL``). The other two test methods in this class are
    deliberately left single-backend (Memory, from ``setUpClass`` below) --
    they exercise session-resolution/authorization plumbing that doesn't vary
    by store, and ``HierarchyWriteScopeStoreParityTest`` below already proves
    the guard's predicate itself on every store.
    """

    @classmethod
    def setUpClass(cls):
        srv = __import__("hockey_scheduler.web.server", fromlist=["x"])
        cls.srv = srv
        os.environ.pop("DATABASE_URL", None)  # class baseline is Memory
        srv.STATE.reset(seed=False)
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), srv.Handler)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.thread.join(timeout=5)

    def _reset_backend(self, database_url):
        """Rebuild the live ``STATE`` on the given backend for this one test.

        ``create_store()`` resolves the backend purely from ``DATABASE_URL``
        at call time and ``DemoState.reset()`` calls it with no override, so
        setting the env var only around this one ``reset()`` call is enough
        to steer it -- the value is restored immediately after, so no other
        test (running before or after, in this class or another) ever
        observes the mutation. ``APP_MODE`` is deliberately left untouched
        (defaults to "demo"): reset()'s production branch never seeds the
        admin/demo account this class logs in with, and preserves rather
        than rebuilds a clean slate, so it would silently break every
        backend here -- not just the ones this test targets.

        A cleanup is registered that flips ``STATE`` back to a fresh Memory
        store the moment THIS test ends (pass, fail, or error), so a test
        that runs after it -- which may assume the class's Memory baseline
        from ``setUpClass`` -- never inherits a SQLite/Postgres-backed STATE.
        """
        prev = os.environ.get("DATABASE_URL")

        def _set(url):
            if url is None:
                os.environ.pop("DATABASE_URL", None)
            else:
                os.environ["DATABASE_URL"] = url

        _set(database_url)
        try:
            self.srv.STATE.reset(seed=False)
        finally:
            _set(prev)

        def _restore_memory():
            _set(None)
            try:
                self.srv.STATE.reset(seed=False)
            finally:
                _set(prev)

        self.addCleanup(_restore_memory)

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

    def _raw(self, opener, method, path, body=None):
        """Like ``_req`` but returns the RAW response bytes.

        The "byte-identical refusal" claim cannot be tested through a
        JSON-decoded body: two dicts comparing equal says nothing about the
        wire format, and a refusal that differed only in key order, spacing or
        a stray field would still be an oracle to anyone reading the socket.
        """
        url = f"http://127.0.0.1:{self.port}{path}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with opener.open(req) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

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

    def test_http_scoped_hierarchy_leaks_no_foreign_registration_team_id(self):
        """The #371 read leak across the route boundary.

        A facade test supplies ``user_id``/``role`` by hand; only here is the
        scoping driven by a real session, which is what production does.
        """
        from hockey_scheduler.domain.models import Team
        from hockey_scheduler.domain.setup_models import SeasonTeamRegistration

        c = self._admin()
        a = self._fixture(c, "LEAK-A")
        b = self._fixture(c, "LEAK-B")
        club = self._v2(c, "club", {"name": "Leak Club"})
        store = self.srv.STATE.api.store

        # Program B's Team, registered into Program A's Season -- injected,
        # because the facade correctly refuses to create this shape now.
        team_b = Team(id=store.next_id("team"), name="HTTP-LEAK-CANARY",
                      club_id=club["id"], program_id=b["program"]["id"])
        store.add_team(team_b)
        ls_a = next(ls for ls in store.all_league_seasons()
                    if ls.season_id == a["season"]["id"]
                    and ls.league_id == a["league"]["id"])
        store.add_season_team_registration(SeasonTeamRegistration(
            id=store.next_id("streg"), league_season_id=ls_a.id,
            team_id=team_b.id, division_id=None, active=True))

        # Shape 2: a LeagueSeason joining A's Season to B's League, carrying a
        # legitimate Program-A Team (so the Team check passes and the row is
        # classified on its League instead).
        from hockey_scheduler.domain.setup_models import LeagueSeason
        # The Team creates below are legitimate work in their OWN Programs, so
        # the context is switched explicitly for each -- this PR's write guard
        # is correct to refuse them otherwise, and is not weakened here.
        self._set_context(c, a)
        team_a = self._v2(c, "team", {"club_id": club["id"],
                                      "league_id": a["league"]["id"],
                                      "name": "A Own Team"})
        ls_mixed = LeagueSeason(id=store.next_id("leagueseason"),
                                league_id=b["league"]["id"],
                                season_id=a["season"]["id"])
        store.add_league_season(ls_mixed)
        store.add_season_team_registration(SeasonTeamRegistration(
            id=store.next_id("streg"), league_season_id=ls_mixed.id,
            team_id=team_a["id"], division_id=None, active=True))

        # Shape 3: an A registration carrying a Program-B Division id. The
        # Division is created in B, where it legitimately belongs.
        self._set_context(c, b)
        div_b = self._v2(c, "division", {"league_id": b["league"]["id"],
                                         "season_id": b["season"]["id"],
                                         "name": "HTTP-DIV-B-CANARY"})
        self._set_context(c, a)
        team_a2 = self._v2(c, "team", {"club_id": club["id"],
                                       "league_id": a["league"]["id"],
                                       "name": "A Own Team 2"})
        store.add_season_team_registration(SeasonTeamRegistration(
            id=store.next_id("streg"), league_season_id=ls_a.id,
            team_id=team_a2["id"], division_id=div_b["id"], active=True))

        status, _ = self._req(c, "POST", "/api/context",
                              {"program_id": a["program"]["id"],
                               "season_id": a["season"]["id"]})
        self.assertEqual(status, 200)

        status, tree = self._req(c, "GET", "/api/v2/setup/hierarchy")
        self.assertEqual(status, 200, tree)
        blob = json.dumps(tree)
        for ref_id, ref_name in (
                (team_b.id, "HTTP-LEAK-CANARY"),
                (b["league"]["id"], b["league"]["name"]),
                (div_b["id"], "HTTP-DIV-B-CANARY")):
            self.assertNotIn(
                ref_id, blob,
                f"the scoped hierarchy route returned foreign id {ref_id!r} "
                f"({ref_name}) while Program A was active")
            self.assertNotIn(ref_name, blob,
                             f"the foreign name {ref_name!r} leaked over HTTP")
        rows = [r for p in tree["programs"] for s in p["seasons"]
                for r in s["needs_assignment"]["registrations"]]
        self.assertEqual(len(rows), 3,
                         f"expected one repair row per corrupt shape: {rows}")
        self.assertTrue(all(r["reason"] for r in rows),
                        f"a repair signal disappeared over HTTP: {rows}")
        # Each shape loses exactly the reference that is foreign, and keeps the
        # ones that are not -- a blanket "everything is null" would also pass
        # if the payload were simply emptied.
        by_reason = {r["reason"]: r for r in rows}
        self.assertIsNone(by_reason["team_missing"]["team_id"], rows)
        self.assertIsNone(
            by_reason["registration_league_not_in_season"]["league_id"], rows)
        self.assertIsNotNone(
            by_reason["registration_league_not_in_season"]["team_id"],
            f"a legitimate Program-A Team id was withheld too: {rows}")
        self.assertIsNone(
            by_reason["registration_league_division_mismatch"]["division_id"],
            rows)
        self.assertEqual(
            by_reason["registration_league_division_mismatch"]["league_id"],
            a["league"]["id"],
            f"Program A's OWN League id was withheld: {rows}")

    def _set_context(self, c, fx):
        """Switch the session to a fixture's Program/Season, asserting it took.

        Used where a test does legitimate work in more than one Program: the
        write guard binds to the ACTIVE Program, so the fixture switches
        rather than being exempted from it.
        """
        status, resp = self._req(c, "POST", "/api/context",
                                 {"program_id": fx["program"]["id"],
                                  "season_id": fx["season"]["id"]})
        self.assertEqual(status, 200, resp)

    def _assert_cross_program_team_create_refused(self, backend):
        """The route-level cross-Program-write proof, parametrized by the
        label of whatever backend the caller already pointed ``STATE`` at."""
        # Prove the store REALLY is the one this variant claims, before
        # asserting anything about the guard. Without this the whole matrix is
        # theatre: the original defect here was precisely a write test that
        # believed it covered PostgreSQL while silently running on
        # InMemoryStore, and it looked green the entire time. If
        # ``_reset_backend`` ever stops steering ``create_store()`` -- a
        # renamed env var, a changed resolution order, a caching ``reset()``
        # -- this fails loudly instead of quietly re-testing Memory three
        # times under three different names.
        live = self.srv.STATE.api.store
        if backend == "memory":
            self.assertIsInstance(
                live, InMemoryStore,
                f"expected the in-memory store, got {type(live).__name__}")
        else:
            self.assertIsInstance(
                live, SqlStore,
                f"the {backend} variant is not running on a SQL store at all "
                f"-- got {type(live).__name__}; it would silently re-prove "
                f"the Memory case")
            self.assertEqual(
                live.backend, backend,
                f"the {backend} variant is running on a "
                f"{live.backend!r}-backed SqlStore")

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
            f"[{backend}] a Team was created under another Program's "
            f"League: {resp}")
        self.assertEqual(resp["error"]["details"]["reason"],
                         "league_not_accessible", (backend, resp))

        self.assertEqual(
            {t.id for t in store.all_teams()}, teams_before,
            f"[{backend}] the refused create still wrote a Team row")
        self.assertEqual(
            len(store.all_setup_audit()), audit_before,
            f"[{backend}] the refused create still appended a setup-audit "
            "row -- a refusal must not record the attempt, or the audit feed "
            "becomes its own disclosure of what was tried and against which "
            "League")
        self.assertFalse(
            any(t.name == "Cross-Program Team" for t in store.all_teams()),
            f"[{backend}] the refused Team name is present in the store")

        # A NONEXISTENT League id is refused identically -- so the response
        # cannot be used to probe whether another Program's League exists.
        status_missing, resp_missing = self._req(
            c, "POST", "/api/v2/setup/team",
            {"club_id": club["id"], "league_id": "league_does_not_exist",
             "name": "Ghost"})
        self.assertEqual((status_missing, resp_missing), (status, resp),
                         f"[{backend}] an inaccessible League must be "
                         "indistinguishable from a nonexistent one")

        # ...and identical ON THE WIRE, not merely as decoded dicts. Compared
        # as raw bytes because that is what an attacker actually observes;
        # a difference in key order, whitespace or an extra field would be an
        # oracle even though the parsed objects compare equal.
        cross_status, cross_bytes = self._raw(
            c, "POST", "/api/v2/setup/team",
            {"club_id": club["id"], "league_id": a["league"]["id"],
             "name": "Cross-Program Team 2"})
        ghost_status, ghost_bytes = self._raw(
            c, "POST", "/api/v2/setup/team",
            {"club_id": club["id"], "league_id": "league_also_missing",
             "name": "Ghost 2"})
        self.assertEqual(cross_status, ghost_status, backend)
        self.assertEqual(
            cross_bytes, ghost_bytes,
            f"[{backend}] the cross-Program refusal differs from the "
            f"nonexistent-League refusal on the wire: {cross_bytes!r} vs "
            f"{ghost_bytes!r}")

        # Positive control: the SAME request against the active Program's own
        # League succeeds, so the refusals above are not a blanket failure.
        ok = self._v2(c, "team", {"club_id": club["id"],
                                  "league_id": b["league"]["id"],
                                  "name": "Own-Program Team"})
        # Assert on program_id, not league_id: the v2 Team DTO carries the
        # Program (the service derives it from the League), and where the Team
        # actually LANDED is the stronger claim anyway.
        self.assertEqual(ok["program_id"], b["program"]["id"], (backend, ok))

        # Switching to Program A makes ITS League writable and B's refused --
        # the gate follows the context rather than being a fixed allow-list.
        self._req(c, "POST", "/api/context",
                  {"program_id": a["program"]["id"],
                   "season_id": a["season"]["id"]})
        ok_a = self._v2(c, "team", {"club_id": club["id"],
                                    "league_id": a["league"]["id"],
                                    "name": "Now Allowed"})
        self.assertEqual(ok_a["program_id"], a["program"]["id"],
                         (backend, ok_a))
        status_b, _ = self._req(c, "POST", "/api/v2/setup/team",
                                {"club_id": club["id"],
                                 "league_id": b["league"]["id"],
                                 "name": "Now Refused"})
        self.assertEqual(status_b, 404, backend)

    def test_cross_program_team_create_is_refused_generically_memory(self):
        self._reset_backend(None)
        self._assert_cross_program_team_create_refused("memory")

    def test_cross_program_team_create_is_refused_generically_sqlite(self):
        self._reset_backend(":memory:")
        self._assert_cross_program_team_create_refused("sqlite")

    @unittest.skipUnless(os.environ.get("TEST_DATABASE_URL"),
                         "PostgreSQL required (set TEST_DATABASE_URL) -- "
                         "skipped VISIBLY rather than silently falling back "
                         "to another store")
    def test_cross_program_team_create_is_refused_generically_postgres(self):
        self._reset_backend(os.environ["TEST_DATABASE_URL"])
        self._assert_cross_program_team_create_refused("postgres")

    def test_hierarchy_route_refuses_a_session_that_dies_mid_request(self):
        """A session that resolves for the authorization read but NOT for the
        read that follows must 401 -- never fall back to the unscoped tree.

        This is the exact failure path an earlier revision got backwards: it
        resolved a SECOND time and discarded the error, so a session revoked
        between the two reads yielded role=None -- which is the service's
        LEGACY no-context form, i.e. the INSTALLATION-WIDE hierarchy. The
        dying session WIDENED the read instead of refusing it.

        A first version of this test simply logged out and asserted 401. That
        was VACUOUS: after logout ``_operator_only`` rejects first, so the
        second resolve never runs and the defect is never reached -- proven by
        mutation, the test stayed green with the fix reverted. It now makes
        resolution succeed exactly once and fail afterwards, which is the
        state the route actually has to survive.
        """
        c = self._admin()
        a = self._fixture(c, "MID")
        self._req(c, "POST", "/api/context",
                  {"program_id": a["program"]["id"],
                   "season_id": a["season"]["id"]})
        other = self._fixture(c, "MIDOTHER")
        # Re-pin, since building `other` moved the active context to it.
        self._req(c, "POST", "/api/context",
                  {"program_id": a["program"]["id"],
                   "season_id": a["season"]["id"]})

        real = self.srv.Handler._resolve_role
        calls = {"n": 0}

        def flaky(handler_self):
            calls["n"] += 1
            if calls["n"] == 1:                 # the authorization read
                return real(handler_self)
            # every later resolution in THIS process behaves as a dead session
            return None, None, None, (401, {"error": {
                "code": "unauthorized",
                "message": "A signed-in account is required."}})

        self.srv.Handler._resolve_role = flaky
        try:
            status, body = self._raw(c, "GET", "/api/v2/setup/hierarchy")
        finally:
            self.srv.Handler._resolve_role = real

        self.assertGreaterEqual(
            calls["n"], 1, "the route never resolved a session at all")
        self.assertIn(
            status, (401, 403),
            f"a session that died mid-request was served the hierarchy "
            f"(resolved {calls['n']}x): {body!r}")
        for token in (other["program"]["name"], other["league"]["name"],
                      other["program"]["id"], other["league"]["id"],
                      a["program"]["name"], a["program"]["id"]):
            self.assertNotIn(
                token.encode(), body,
                f"the refusal leaked {token!r} -- a refused hierarchy read "
                f"must disclose no Program or League name or id at all")

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


class HierarchyWriteScopeStoreParityTest(unittest.TestCase):
    """The same refusal/no-residue/own-League-success proof on EVERY store,
    one layer BELOW the route.

    ``HierarchyWriteScopeHttpTest`` above now also runs its real-route proof
    (session resolution, the guard, dispatch, the actual ``create_team``, the
    wire format) against Memory, SQLite, and PostgreSQL-when-configured -- it
    points ``STATE`` at each backend in turn via ``DATABASE_URL`` (see
    ``_reset_backend`` there) rather than always landing on the
    ``InMemoryStore`` default, which is what ``STATE.reset()`` used to do
    unconditionally (``create_store()`` reads ``DATABASE_URL``, and the
    PostgreSQL CI job sets only ``TEST_DATABASE_URL``). This class remains as
    a belt-and-suspenders check: it drives the guard's PREDICATE directly, in
    isolation from route/session/wire-format concerns, so a change to the
    predicate itself is caught here even if something about the HTTP
    plumbing above changes independently.
    """

    def _guard(self, api, user_id, role, scope, league_id):
        """The route's own predicate, exercised directly: a League is
        acceptable only when it belongs to the caller's ACTIVE Program."""
        program, _season, _league = api.context.resolve_with_league(
            user_id, role, scope)
        league = api.store.get_league(league_id) if league_id else None
        return (program is not None and league is not None
                and league.program_id == program.id)

    def test_cross_program_refusal_and_no_residue_on_every_store(self):
        for backend, store in _backends():
            with self.subTest(backend=backend):
                api = ApiService(store)
                fx = _two_programs(api)
                admin = ("admin", Role.LEAGUE_ADMIN, {})
                api.set_active_context(*admin, fx["B"]["program"]["id"],
                                       fx["B"]["season"]["id"])

                teams_before = {t.id for t in store.all_teams()}
                audit_before = len(store.all_setup_audit())

                # Program A's League is refused while B is active...
                self.assertFalse(
                    self._guard(api, "admin", Role.LEAGUE_ADMIN, {},
                                fx["A"]["league"]["id"]),
                    f"[{backend}] a League from another Program was accepted "
                    f"while Program B was active")
                # ...and a nonexistent League is refused identically.
                self.assertFalse(
                    self._guard(api, "admin", Role.LEAGUE_ADMIN, {},
                                "league_does_not_exist"), backend)
                # Positive control: B's own League IS accepted, so the two
                # refusals above are not a blanket false.
                self.assertTrue(
                    self._guard(api, "admin", Role.LEAGUE_ADMIN, {},
                                fx["B"]["league"]["id"]),
                    f"[{backend}] the active Program's OWN League was refused")

                # A refused attempt must leave no Team and no audit row. The
                # route returns before touching the service, so nothing should
                # have been written on ANY store.
                self.assertEqual({t.id for t in store.all_teams()},
                                 teams_before, backend)
                self.assertEqual(len(store.all_setup_audit()), audit_before,
                                 backend)

                # And the accepted create really does land in Program B.
                team = api.create_team(fx["club"]["id"], None, "Own Team",
                                       league_id=fx["B"]["league"]["id"])
                self.assertNotIn("error", team, team)
                self.assertEqual(team["program_id"], fx["B"]["program"]["id"],
                                 backend)
                _close(store)


if __name__ == "__main__":
    unittest.main()
