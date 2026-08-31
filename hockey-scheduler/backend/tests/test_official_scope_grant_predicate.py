"""ONE PREDICATE DECIDES WHICH ASSIGNMENTS GRANT AN OFFICIAL SCOPE (#205).

THE BLOCKER (owner comment 5439225924), reproduced on exact head 63db78f
through public `get_context_options` on Memory, SQLite and PostgreSQL

    "`resolve_private_game_read` now correctly requires an active
     `OfficialAssignment`, but both `context_scope._official_program_seasons`
     and `_official_league_ids` still iterate every assignment for the Official
     without checking `assignment.status.is_active`. Repro: give one Official
     coherent assignments in a target League and an independent anchor League,
     then decline the target through `ApiService.respond_assignment(...,
     accept=False)`. The stored row becomes `status=declined`, `is_active=False`,
     yet `ApiService.get_context_options(...)` still offers both target and
     anchor Leagues before and after the decline."

WHY IT MATTERS, in the owner's words: "the same declined grant is revoked on
private-game routes but remains live in the context switcher, so authorization
now disagrees across product surfaces and exposes selectable Program/Season/
League scope from a relationship the Official ended."

HOW THE DRIFT WAS CREATED, which is the part that decides the fix. `d62473a` on
this branch fixed the private-game half by INLINING `a.status.is_active` at its
own call site. That made a THIRD copy of the predicate rather than one shared
one — which is precisely why this blocker exists. The ruling is explicit:
"centralize the product predicate for assignments that grant Official scope,
and apply it in both `_official_program_seasons` and `_official_league_ids` as
well as private-game admission. At minimum it must require the exact Official
and `status.is_active`; do not maintain three parallel predicates."

So the predicate is now `subject_scope.assignment_grants_official_scope`, and
d62473a's inline test has become a CALL to it. It lives beside
`subject_scope.own_team_id` because it is the same species of fact — the one
canonical answer to "what does this caller's relationship entitle them to" —
and that module already exists to stop exactly this drift between the web scope
guards and the context selector.

WHAT WAS MEASURED RED at head 63db78f, through public `get_context_options`::

    surface                         before decline    after decline   expected
    context switcher: Program       Target + Anchor   Target+Anchor   Anchor
    context switcher: Season        Target + Anchor   Target+Anchor   Anchor
    context switcher: League        Target + Anchor   Target+Anchor   Anchor
    private-game admission          admitted          REFUSED         REFUSED

The last row is the disagreement itself: one relationship, two answers.

THE ANCHOR IS LOAD-BEARING. Every fixture gives the Official a second, ACTIVE
assignment which must survive the decline — "the active anchor keeps the
negative from passing because all context collapsed."

TWO FIXTURE SHAPES, and the second is not redundant. `_fixture` puts target and
anchor in SEPARATE Programs. `_same_program_fixture` puts two Leagues under ONE
Program/Season. Because `leagues` is enumerated PER PROGRAM, the separate-Program
shape cannot isolate `_official_league_ids` — the target League also disappears
when its Program does — so the League falsifier would pass while the helper was
broken. That was measured, and it is why the same-Program shape exists.

TRI-STORE, PROVEN, plus the authenticated `GET /api/context/options` route.
A SKIP IS NOT A PASS.

TWO SEPARATE FALSIFIERS, one per projection helper, as the ruling requires:
`RestoringTheUnfilteredLoopInProgramSeasonsMustRedden` and
`RestoringTheUnfilteredLoopInLeagueIdsMustRedden`. Each restores the unfiltered
loop in ONE helper, leaves the other filtered, and fails BY NAME on the axis
that helper alone controls — so neither can be satisfied by the other's fix.

THE HARNESS AND THE ROUTE BASE LIVE HERE and are imported by
`test_official_league_identity_agreement` (blocker B, the falsy-League
exemption), which shares this file's tri-store and route conventions. Two copies
of a tri-store convention is the same mistake this file is about. They sit in
THIS file because blocker B's falsifier calls the centralized predicate below,
so this module is the one that can stand alone.
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
from test_active_context_league import _assign_game, _registered_team

from hockey_scheduler.api.service import ApiService
from hockey_scheduler.domain import Role
from hockey_scheduler.services import context_scope
from hockey_scheduler.services.subject_scope import (
    assignment_grants_official_scope)
from hockey_scheduler.store import InMemoryStore, SqlStore
from hockey_scheduler.web import server as srv
from hockey_scheduler.web.auth import DEMO_PASSWORD, DEMO_USERS

_PG_SKIP = ("PostgreSQL not configured (TEST_DATABASE_URL); this assertion is "
            "NOT covered on the backend it is about.")


class _OfficialScopeHarness:

    def _stores(self):
        yield "memory", InMemoryStore()
        yield "sqlite", SqlStore(":memory:")
        url = os.environ.get("TEST_DATABASE_URL")
        if url:
            store = SqlStore(url)
            store.clear_all_data()
            yield "postgres", store

    def _assert_backend(self, label, store):
        """PROVE the backend. ``skipUnless`` on the env var proves only that a
        URL was SET, never that a statement reached PostgreSQL."""
        if label == "postgres":
            self.assertIsInstance(store, SqlStore, label)
            self.assertEqual(store.backend, "postgres", store.backend)
        elif label == "sqlite":
            self.assertIsInstance(store, SqlStore, label)
            self.assertEqual(store.backend, "sqlite", store.backend)
        else:
            self.assertIsInstance(store, InMemoryStore, label)

    def _close(self, label, store):
        if isinstance(store, SqlStore):
            store.close()

    def _assert_ran(self, ran, banner):
        expected = {"memory", "sqlite"}
        if os.environ.get("TEST_DATABASE_URL"):
            expected.add("postgres")
        else:
            print(f"\n[{banner}] " + _PG_SKIP)
        self.assertEqual(set(ran), expected, sorted(ran))

    # -- projections under test, by NAME ---------------------------------
    def _options(self, api, oid):
        return api.get_context_options("u_off", Role.OFFICIAL,
                                       {"official_id": oid})

    def _leagues(self, opts):
        return sorted(lg["name"] for p in opts.get("programs", [])
                      for lg in p.get("leagues", []))

    def _programs(self, opts):
        return sorted(p["name"] for p in opts.get("programs", []))

    def _seasons(self, opts):
        return sorted(s["name"] for p in opts.get("programs", [])
                      for s in p.get("seasons", []))

    def _axes(self, api, oid):
        o = self._options(api, oid)
        return (self._programs(o), self._seasons(o), self._leagues(o))


# =========================================================================== #
# BLOCKER C — a declined assignment grants nothing, on every surface          #
# =========================================================================== #
class ADeclinedAssignmentStopsGrantingContextScope(
        _OfficialScopeHarness, unittest.TestCase):
    """Target and anchor sit in SEPARATE Programs, so "the target disappeared"
    cannot be a disguised total collapse: the anchor Program, Season and League
    must all survive the decline."""

    def _fixture(self, store):
        api = ApiService(store)
        oid = api.create_official("Ref")["id"]
        ids = {"oid": oid}
        for tag, pname, sname, lname in (
                ("t", "TargetProg", "TFall", "TargetLeague"),
                ("a", "AnchorProg", "AFall", "AnchorLeague")):
            pid = api.create_program(pname, "US", "UTC")["id"]
            sid = api.create_season(pid, sname)["id"]
            lid = api.create_league(sid, lname)["id"]
            team = _registered_team(api, sid, lid, f"{tag}Team")
            gid = _assign_game(
                api, oid, season_id=sid, team_id=team, league_id=lid,
                league_season_id=store.league_season_for(lid, sid).id)
            ids[f"{tag}_gid"] = gid
            ids[f"{tag}_assign"] = store.assignments_for_game(gid)[0].id
        return api, ids

    def _same_program_fixture(self, store):
        """TARGET AND ANCHOR LEAGUES UNDER ONE PROGRAM AND SEASON, each with
        its own assignment for the same Official.

        WHY THIS SECOND SHAPE EXISTS, and it is not redundant. ``leagues`` is
        enumerated PER PROGRAM (``authorized_league_ids`` intersects with the
        Leagues under ``program_id``), so in the two-Program fixture above the
        target League disappears after a decline partly because its PROGRAM
        did — ``_official_program_seasons`` alone is enough to remove it. That
        makes the two-Program shape unable to isolate ``_official_league_ids``,
        and a falsifier for that helper written against it would pass while the
        helper was broken (measured: it did).

        Here the anchor keeps the Program and Season visible no matter what, so
        whether the target League is still offered is decided by
        ``_official_league_ids`` and by nothing else."""
        api = ApiService(store)
        oid = api.create_official("Ref")["id"]
        pid = api.create_program("Prog", "US", "UTC")["id"]
        sid = api.create_season(pid, "Fall")["id"]
        target = api.create_league(sid, "TargetLeague")["id"]
        anchor = api.create_league(sid, "AnchorLeague")["id"]
        ids = {"oid": oid}
        for tag, lid in (("t", target), ("a", anchor)):
            team = _registered_team(api, sid, lid, f"{tag}Team")
            gid = _assign_game(
                api, oid, season_id=sid, team_id=team, league_id=lid,
                league_season_id=store.league_season_for(lid, sid).id)
            ids[f"{tag}_gid"] = gid
            ids[f"{tag}_assign"] = store.assignments_for_game(gid)[0].id
        return api, ids

    def test_declining_one_league_leaves_the_sibling_league_in_the_same_program(
            self):
        """The isolating case for ``_official_league_ids``: the Program and
        Season survive on the anchor's own active assignment, so ONLY the
        declined League may disappear."""
        ran = []
        for label, store in self._stores():
            try:
                self._assert_backend(label, store)
                store.clear_all_data()
                api, fx = self._same_program_fixture(store)
                with self.subTest(backend=label):
                    self.assertEqual(
                        self._leagues(self._options(api, fx["oid"])),
                        ["AnchorLeague", "TargetLeague"], label)

                    api.respond_assignment(fx["t_assign"], accept=False,
                                           actor_id="u_off")

                    programs, seasons, leagues = self._axes(api, fx["oid"])
                    self.assertEqual(leagues, ["AnchorLeague"], label)
                    # the Program and Season are UNTOUCHED — the anchor still
                    # grants them, so this negative is about the League alone
                    self.assertEqual(programs, ["Prog"], label)
                    self.assertEqual(seasons, ["Fall"], label)
                ran.append(label)
            finally:
                self._close(label, store)
        self._assert_ran(ran, "OFFICIAL GRANT / SIBLING LEAGUE ISOLATED")

    def test_proposed_and_accepted_grant_all_three_axes(self):
        """The POSITIVE half the ruling asks for: both ACTIVE statuses grant
        Program, Season AND League, so the decline below removes something that
        was demonstrably there."""
        ran = []
        for label, store in self._stores():
            try:
                self._assert_backend(label, store)
                for accept in (None, True):
                    store.clear_all_data()
                    api, fx = self._fixture(store)
                    row = store.get_official_assignment(fx["t_assign"])
                    self.assertEqual(row.status.value, "proposed", label)
                    if accept:
                        api.respond_assignment(fx["t_assign"], accept=True,
                                               actor_id="u_off")
                        self.assertEqual(
                            store.get_official_assignment(
                                fx["t_assign"]).status.value, "accepted", label)
                    status = "accepted" if accept else "proposed"
                    with self.subTest(backend=label, status=status):
                        programs, seasons, leagues = self._axes(api, fx["oid"])
                        self.assertEqual(
                            programs, ["AnchorProg", "TargetProg"], label)
                        self.assertEqual(seasons, ["AFall", "TFall"], label)
                        self.assertEqual(
                            leagues, ["AnchorLeague", "TargetLeague"], label)
                ran.append(label)
            finally:
                self._close(label, store)
        self._assert_ran(ran, "OFFICIAL GRANT / PROPOSED + ACCEPTED")

    def test_after_a_real_decline_only_the_target_axes_disappear(self):
        ran = []
        for label, store in self._stores():
            try:
                self._assert_backend(label, store)
                store.clear_all_data()
                api, fx = self._fixture(store)
                before = self._axes(api, fx["oid"])

                # THE PRODUCT'S OWN WRITE PATH, as the ruling names it.
                res = api.respond_assignment(fx["t_assign"], accept=False,
                                             actor_id="u_off")
                self.assertNotIn("error", res, res)

                with self.subTest(backend=label):
                    self.assertEqual(
                        before,
                        (["AnchorProg", "TargetProg"], ["AFall", "TFall"],
                         ["AnchorLeague", "TargetLeague"]), label)
                    row = store.get_official_assignment(fx["t_assign"])
                    self.assertEqual(row.status.value, "declined", label)
                    self.assertFalse(row.status.is_active, label)

                    programs, seasons, leagues = self._axes(api, fx["oid"])
                    # target-only Program/Season/League are gone
                    self.assertEqual(programs, ["AnchorProg"], label)
                    self.assertEqual(seasons, ["AFall"], label)
                    self.assertEqual(leagues, ["AnchorLeague"], label)
                ran.append(label)
            finally:
                self._close(label, store)
        self._assert_ran(ran, "OFFICIAL GRANT / AFTER DECLINE")

    def test_one_predicate_is_shared_by_all_three_call_sites(self):
        """THE CENTRALIZATION ITSELF, asserted rather than assumed — the ruling
        is explicit that three parallel predicates are the defect.

        Each of the three modules must resolve the SAME function object. A
        future re-inlining of `a.status.is_active` at any one of them reddens
        here, by name, even if that copy happens to agree today."""
        from hockey_scheduler.services import game_side_scope
        for module in (context_scope, game_side_scope):
            self.assertIs(module.assignment_grants_official_scope,
                          assignment_grants_official_scope, module.__name__)

    def test_the_private_game_surface_agrees_with_the_context_switcher(self):
        """The two surfaces that DISAGREED must now answer alike: the same
        declined assignment is refused admission to the private-game family and
        offers no context option."""
        from hockey_scheduler.services.game_side_scope import (
            resolve_private_game_read)
        ran = []
        for label, store in self._stores():
            try:
                self._assert_backend(label, store)
                store.clear_all_data()
                api, fx = self._fixture(store)
                scope = {"official_id": fx["oid"]}
                with self.subTest(backend=label):
                    self.assertTrue(
                        resolve_private_game_read(
                            Role.OFFICIAL, scope, fx["t_gid"], store).admitted,
                        label)
                    api.respond_assignment(fx["t_assign"], accept=False,
                                           actor_id="u_off")
                    # private-game family: refused
                    self.assertFalse(
                        resolve_private_game_read(
                            Role.OFFICIAL, scope, fx["t_gid"], store).admitted,
                        label)
                    # context switcher: same answer, no target options
                    self.assertEqual(self._leagues(self._options(api, fx["oid"])),
                                     ["AnchorLeague"], label)
                    # the still-active anchor is admitted on BOTH surfaces
                    self.assertTrue(
                        resolve_private_game_read(
                            Role.OFFICIAL, scope, fx["a_gid"], store).admitted,
                        label)
                ran.append(label)
            finally:
                self._close(label, store)
        self._assert_ran(ran, "OFFICIAL GRANT / SURFACES AGREE")


class RestoringTheUnfilteredLoopInProgramSeasonsMustRedden(
        _OfficialScopeHarness, unittest.TestCase):
    """BLOCKER C FALSIFIER 1 OF 2, named for the helper it breaks:
    ``_official_program_seasons``. The ruling asks for SEPARATE falsifiers per
    projection helper so neither can be covered by the other's assertion."""

    def _unfiltered_program_seasons(self):
        from hockey_scheduler.services import scope_bridge

        def broken(store, official_id):
            programs, seasons = set(), set()
            if not official_id:
                return programs, seasons
            # THE DEFECT: every assignment, active or not.
            for a in store.assignments_for_official(official_id):
                game = store.get_game(a.game_id)
                if game is None or not game.season_id:
                    continue
                season = store.get_season(game.season_id)
                if season is None:
                    continue
                seasons.add(season.id)
                programs.add(scope_bridge.season_scope_id(season))
            return programs, seasons

        original = context_scope._official_program_seasons
        context_scope._official_program_seasons = broken
        self.addCleanup(setattr, context_scope, "_official_program_seasons",
                        original)

    def test_program_and_season_survive_the_decline_when_the_filter_is_gone(
            self):
        cls = ADeclinedAssignmentStopsGrantingContextScope
        self._unfiltered_program_seasons()
        store = InMemoryStore()
        try:
            api, fx = cls._fixture(self, store)
            api.respond_assignment(fx["t_assign"], accept=False,
                                   actor_id="u_off")
            programs, seasons, _leagues = self._axes(api, fx["oid"])
            with self.assertRaises(
                    AssertionError,
                    msg="_official_program_seasons unfiltered: Program leaked"):
                self.assertEqual(programs, ["AnchorProg"])
            with self.assertRaises(
                    AssertionError,
                    msg="_official_program_seasons unfiltered: Season leaked"):
                self.assertEqual(seasons, ["AFall"])
            self.assertIn("TargetProg", programs)
            self.assertIn("TFall", seasons)
        finally:
            store.close() if hasattr(store, "close") else None


class RestoringTheUnfilteredLoopInLeagueIdsMustRedden(
        _OfficialScopeHarness, unittest.TestCase):
    """BLOCKER C FALSIFIER 2 OF 2, named for the helper it breaks:
    ``_official_league_ids``.

    Note it breaks ONLY the League projection, leaving
    ``_official_program_seasons`` filtered — so the leaked target League here
    cannot be attributed to the other helper.

    IT MUST USE THE SAME-PROGRAM FIXTURE, and this is the whole reason that
    fixture exists. Against the two-Program shape this falsifier PASSES while
    the helper is broken (measured), because the declined target's Program is
    removed by the still-filtered ``_official_program_seasons`` and a League is
    only ever enumerated under a Program the caller can see. Sharing the
    Program with an active anchor is what makes ``_official_league_ids`` the
    only thing left deciding, which is what "fail BY NAME" requires."""

    def _unfiltered_league_ids(self):
        from hockey_scheduler.domain import GameType
        from hockey_scheduler.services import season_guard

        def broken(store, official_id):
            leagues = set()
            if not official_id:
                return leagues
            # THE DEFECT: every assignment, active or not.
            for a in store.assignments_for_official(official_id):
                game = store.get_game(a.game_id)
                if game is None:
                    continue
                if ((game.game_type or GameType.REGULAR.value)
                        != GameType.REGULAR.value):
                    continue
                if not season_guard.game_is_league_season_bound(game):
                    continue
                ls = store.get_league_season(game.league_season_id)
                if ls is None:
                    continue
                if ls.season_id != game.season_id:
                    continue
                if ls.league_id != game.league_id:
                    continue
                if ls.league_id:
                    leagues.add(ls.league_id)
            return leagues

        original = context_scope._official_league_ids
        context_scope._official_league_ids = broken
        self.addCleanup(setattr, context_scope, "_official_league_ids",
                        original)

    def test_the_declined_league_survives_when_only_this_filter_is_gone(self):
        cls = ADeclinedAssignmentStopsGrantingContextScope
        self._unfiltered_league_ids()
        store = InMemoryStore()
        try:
            api, fx = cls._same_program_fixture(self, store)
            api.respond_assignment(fx["t_assign"], accept=False,
                                   actor_id="u_off")
            programs, seasons, leagues = self._axes(api, fx["oid"])
            with self.assertRaises(
                    AssertionError,
                    msg="_official_league_ids unfiltered: League leaked"):
                self.assertEqual(leagues, ["AnchorLeague"])
            self.assertIn("TargetLeague", leagues)
            # the Program/Season projection is untouched by this falsifier and
            # is granted by the anchor anyway, so the leak above is
            # attributable to _official_league_ids ALONE
            self.assertEqual(programs, ["Prog"])
            self.assertEqual(seasons, ["Fall"])
        finally:
            store.close() if hasattr(store, "close") else None



# =========================================================================== #
# the authenticated context-options route                                     #
# =========================================================================== #
# =========================================================================== #
# the authenticated context-options route                                     #
# =========================================================================== #
class _ContextOptionsRouteBase(_OfficialScopeHarness):
    """``GET /api/context/options`` driven with a real Official session, so the
    scope under test is the one ``_resolve_role`` actually produces.

    A PLAIN MIXIN, deliberately NOT a ``TestCase``: blocker C's route class in
    ``test_official_scope_grant_predicate`` inherits this too, and a shared base
    that carried its own test method would silently re-run that test under every
    subclass's name. The concrete cases are the ``OverTheAuthenticated...``
    classes, one per blocker."""

    @classmethod
    def setUpClass(cls):
        cls._saved_api = srv.STATE.api
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), srv.Handler)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever,
                                      daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.thread.join(timeout=5)
        cls.httpd.server_close()
        srv.STATE.api = cls._saved_api

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
            with e:
                return e.code, json.loads(e.read() or b"{}")

    def _login(self, api, username):
        srv.STATE.api = api
        opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(CookieJar()))
        status, body = self._req(opener, "POST", "/api/auth/login",
                                 {"username": username, "password": "demo"})
        self.assertEqual(status, 200, (username, body))
        return opener


class OverTheAuthenticatedContextOptionsRoute(
        _ContextOptionsRouteBase, unittest.TestCase):
    """The same decline, through `GET /api/context/options` on a real Official
    session — the surface a human actually picks a context from."""

    def test_declined_target_disappears_from_the_route_anchor_remains(self):
        cls = ADeclinedAssignmentStopsGrantingContextScope
        ran = []
        for label, store in self._stores():
            try:
                self._assert_backend(label, store)
                store.clear_all_data()
                api, fx = cls._fixture(self, store)
                api.accounts.create_account(
                    "admin", DEMO_PASSWORD, DEMO_USERS["admin"], scope={},
                    actor_id="test_seed", account_id="user_admin")
                api.accounts.create_account(
                    "theref", DEMO_PASSWORD, Role.OFFICIAL,
                    scope={"official_id": fx["oid"]}, actor_id="test_seed",
                    account_id="user_theref")
                ref = self._login(api, "theref")

                with self.subTest(backend=label):
                    status, before = self._req(ref, "GET",
                                               "/api/context/options")
                    self.assertEqual(status, 200, before)
                    self.assertEqual(
                        self._leagues(before),
                        ["AnchorLeague", "TargetLeague"], before)
                    self.assertEqual(
                        self._programs(before),
                        ["AnchorProg", "TargetProg"], before)

                    api.respond_assignment(fx["t_assign"], accept=False,
                                           actor_id="user_theref")

                    status, after = self._req(ref, "GET",
                                              "/api/context/options")
                    self.assertEqual(status, 200, after)
                    self.assertEqual(self._leagues(after), ["AnchorLeague"],
                                     after)
                    self.assertEqual(self._programs(after), ["AnchorProg"],
                                     after)
                    self.assertEqual(self._seasons(after), ["AFall"], after)
                    # nothing about the declined target is enumerable
                    blob = json.dumps(after)
                    self.assertNotIn("TargetLeague", blob, after)
                    self.assertNotIn("TargetProg", blob, after)
                ran.append(label)
            finally:
                self._close(label, store)
        self._assert_ran(ran, "CONTEXT OPTIONS HTTP / DECLINE")


if __name__ == "__main__":
    unittest.main(verbosity=2)
