"""Archived standings use the explicitly requested Season's registered team set.

SCOPE — this module pins exactly ONE property: an ARCHIVED Season's standings
are built from the team set that Season had, so a later legitimate Team transfer
cannot rewrite them. It does NOT claim general reproducibility of archived
output. The points/tiebreak/eligibility rules are still read LIVE and are not
version-pinned to the Season, so changing a live points rule does still alter
archived output; that is true identically with and without this change, is not a
regression introduced here, and pinning rule versions is a separate #159 child.
Nothing below asserts rule-version reproducibility.

`test_blocker_regressions._assert_ended_season_history_unchanged` already pins
this property for a Season that has ENDED BY DATE (#283 rule 10). #159 added a
SECOND, independent route into history — the explicit ARCHIVED lifecycle state —
and `archive_season` deliberately does NOT invent an `end_date`, so an archived
Season is routinely undated (or even future-dated).

The transfer WRITE path already knew that: it freezes a registration whose
Season is `ARCHIVED or end-dated`. The two standings READERS
(`ApiService._standings_for_division` and `._standings_for_league_season`) tested
only the date, so an archived-but-undated Season was read as if it were LIVE and
the live rule-7 cross-League filter still applied to it. A later, entirely
legitimate Team transfer therefore reached back and deleted the Team from — and
zeroed its opponent's record in — an archived Season's standings, in the operator
AND public tables of both the Division and LeagueSeason views.

All three sites now share one predicate, `SetupService.season_is_historical`.
This module pins both directions:

* `ArchivedSeasonHistoryReproducible` — archived history survives a later
  transfer, in all four tables, over Memory/SQLite/PostgreSQL;
* `ActiveSeasonStaysLive` — the inverse guard, so the fix can never be read as
  "archived is now the default": an ACTIVE, undated Season still applies the
  live rule-7 cross-League filter in both readers;
* `ArchiveChangesOnlyTheRowSet` — archiving still refuses writes
  (`season_archived`) and does not change any refusal shape or turn a read into
  an existence oracle;
* `ExplicitArchivedSeasonIsNotTheFallback` — contract 2b, on the one read this
  PR touches: `get_standings` is bound to the caller's ACTIVE resolved tuple
  (#369/#367), so "which Season am I answering about?" is decided by the
  resolver. An EXPLICIT archived selection must be answered with THAT Season's
  history, never quietly re-aimed at the deterministic fallback's Season. The
  assertion is the discriminating one: the fallback's Season is built, proven to
  be what an unsaved caller resolves to, and the answer is shown to be the
  archived Season's rows and NOT those — "a 200 came back" passes in both worlds.
"""

import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timezone
from http.cookiejar import CookieJar
from http.server import ThreadingHTTPServer

from helpers import BACKEND  # noqa: F401  (ensures sys.path is set up)

import hockey_scheduler.web.server as srv
from hockey_scheduler.api.service import ApiService
from hockey_scheduler.domain import Role, SeasonStatus
from hockey_scheduler.store import InMemoryStore, SqlStore
from hockey_scheduler.web.server import STATE, Handler

ADMIN = "admin"
OPERATOR = (Role.LEAGUE_ADMIN, {})   # (role, scope) for a global operator
UTC = timezone.utc


def _backends():
    yield "memory", InMemoryStore()
    yield "sqlite", SqlStore(":memory:")
    url = os.environ.get("TEST_DATABASE_URL")
    if url:
        SqlStore(url).clear_all_data()
        yield "postgres", SqlStore(url)


def _close(store):
    if isinstance(store, SqlStore):
        store.close()


class _Fixture:
    """One Program / Season / two Leagues (Elite + Rec) / one Division, with A
    beating B 3-1 in a FINAL, PUBLISHED regular game — published so the two
    public tables have something to lose as well as the two operator ones."""

    def build(self, api):
        org = api.create_organization("Org", "O", actor_id=ADMIN)["id"]
        prog = api.create_program("Prog", operator_organization_id=org,
                                  actor_id=ADMIN)["id"]
        season = api.create_season(prog, "S1", actor_id=ADMIN)["id"]
        elite = api.create_league(season, "Elite", actor_id=ADMIN)["id"]
        rec = api.create_league(season, "Rec", actor_id=ADMIN)["id"]
        div = api.create_division_v2(elite, "DA", actor_id=ADMIN)["id"]
        club = api.create_club("Club", actor_id=ADMIN)["id"]
        a = api.create_team(club, None, "A", actor_id=ADMIN,
                            league_id=elite)["id"]
        b = api.create_team(club, None, "B", actor_id=ADMIN,
                            league_id=elite)["id"]
        for t in (a, b):
            reg = api.register_team_for_season(season, t, div, actor_id=ADMIN,
                                               league_id=elite)
            assert "error" not in reg, reg
        ven = api.create_venue("V", organization_id=org, league_id=prog,
                               actor_id=ADMIN)["id"]
        api.grant_season_venue_access(season, ven, actor_id=ADMIN)
        rink = api.create_rink(ven, "R", actor_id=ADMIN)["id"]
        slot = api.create_ice_slot(
            rink, datetime(2026, 9, 1, 18, tzinfo=UTC).isoformat(),
            datetime(2026, 9, 1, 20, tzinfo=UTC).isoformat(), "game",
            actor_id=ADMIN)["id"]
        game = api.create_game(season, div, a, b, slot, actor_id=ADMIN,
                               league_id=elite)["id"]
        assert "error" not in api.publish_game(game, actor_id=ADMIN)
        api.record_result(game, 3, 1, actor_id=ADMIN)
        api.approve_result(game, actor_id=ADMIN)
        return dict(org=org, prog=prog, season=season, elite=elite, rec=rec,
                    div=div, club=club, a=a, b=b, game=game, venue=ven)


def _tables(api, fx):
    """The four standings tables an archived Season's history lives in: the
    operator and public Division views, and the operator and public
    LeagueSeason views."""
    return {
        "division": api.get_standings(fx["div"]),
        "league_season": api.get_league_season_standings(fx["elite"],
                                                         fx["season"]),
        "public_division": api.get_public_standings(fx["div"]),
        "public_league_season": api.get_public_league_season_standings(
            fx["elite"], fx["season"]),
    }


class ArchivedSeasonHistoryReproducible(unittest.TestCase):
    maxDiff = None

    def test_archived_season_standings_survive_a_later_team_transfer(self):
        for label, store in _backends():
            with self.subTest(backend=label):
                api = ApiService(store)
                try:
                    fx = _Fixture().build(api)

                    api.setup.archive_season(fx["season"], actor_id=ADMIN,
                                             reason="season over")
                    season = api.store.get_season(fx["season"])
                    self.assertEqual(season.status, SeasonStatus.ARCHIVED)
                    # The heart of the bug: archiving is a lifecycle state, NOT
                    # a date. A reader that tests only end_date sees a LIVE
                    # Season here.
                    self.assertIsNone(
                        season.end_date,
                        f"{label}: archive must not invent an end_date")

                    before = _tables(api, fx)
                    self.assertEqual(
                        [r["team_id"] for r in before["division"]["standings"]],
                        [fx["a"], fx["b"]], label)
                    self.assertEqual(
                        {r["team_id"]: r["pts"]
                         for r in before["league_season"]["standings"]},
                        {fx["a"]: 2, fx["b"]: 0}, label)

                    # A transfer the app still PERMITS while the Season is
                    # archived: #159/#283 freeze the archived registration
                    # rather than moving it, so this is not a write INTO the
                    # archived Season — A's *permanent* League changes going
                    # forward, and its history must not move with it.
                    moved = api.transfer_team_to_league(fx["a"], fx["rec"],
                                                        actor_id=ADMIN)
                    self.assertNotIn("error", moved, moved)
                    self.assertEqual(api.store.get_team(fx["a"]).league_id,
                                     fx["rec"], label)

                    after = _tables(api, fx)
                    for key in before:
                        self.assertEqual(
                            after[key], before[key],
                            f"{label}: archived history changed in {key} "
                            f"after a later transfer")
                finally:
                    _close(store)

    def test_archived_season_freezes_the_registration_it_reads(self):
        """The reader's row set and the writer's decision are two halves of one
        property: the registration is still ACTIVE in Elite's LeagueSeason
        after the transfer, which is exactly why the table must still show it.
        """
        for label, store in _backends():
            with self.subTest(backend=label):
                api = ApiService(store)
                try:
                    fx = _Fixture().build(api)
                    api.setup.archive_season(fx["season"], actor_id=ADMIN,
                                             reason="season over")
                    ls = api.store.league_season_for(fx["elite"], fx["season"])
                    frozen = [(r.id, r.team_id, r.league_season_id,
                               r.division_id, r.active)
                              for r in api.store
                              .registrations_for_league_season(ls.id)]

                    self.assertNotIn(
                        "error",
                        api.transfer_team_to_league(fx["a"], fx["rec"],
                                                    actor_id=ADMIN))

                    self.assertEqual(
                        [(r.id, r.team_id, r.league_season_id, r.division_id,
                          r.active)
                         for r in api.store
                         .registrations_for_league_season(ls.id)],
                        frozen,
                        f"{label}: an archived Season's registrations moved")
                finally:
                    _close(store)


class ActiveSeasonStaysLive(unittest.TestCase):
    """The inverse guard. Widening "history" to include ARCHIVED must not be
    readable as "everything is history now": an ACTIVE, undated Season keeps
    applying the live rule-7 cross-League filter in BOTH readers, so a Team
    whose current permanent League has drifted away is still excluded."""

    maxDiff = None

    def test_active_undated_season_still_applies_the_live_league_filter(self):
        for label, store in _backends():
            with self.subTest(backend=label):
                api = ApiService(store)
                try:
                    fx = _Fixture().build(api)
                    season = api.store.get_season(fx["season"])
                    self.assertNotEqual(season.status, SeasonStatus.ARCHIVED)
                    self.assertIsNone(season.end_date, label)

                    before = _tables(api, fx)
                    self.assertEqual(
                        [r["team_id"] for r in before["division"]["standings"]],
                        [fx["a"], fx["b"]], label)

                    # The same cross-League drift the archived case tolerates,
                    # planted directly because a LIVE Season refuses the
                    # transfer that would create it (`team_transfer_strands_
                    # games`): A's permanent League is now Rec while its Elite
                    # registration is still active — the migration-preserved
                    # rule-7 violation. On a live Season A must be EXCLUDED.
                    team = api.store.get_team(fx["a"])
                    team.league_id = fx["rec"]
                    api.store.save_team(team)

                    after = _tables(api, fx)
                    for key, table in after.items():
                        ids = [r["team_id"] for r in table["standings"]]
                        self.assertNotIn(
                            fx["a"], ids,
                            f"{label}: a live Season must still exclude a "
                            f"cross-League Team from {key}")
                        self.assertIn(fx["b"], ids, f"{label}: {key}")
                finally:
                    _close(store)


class ArchiveChangesOnlyTheRowSet(unittest.TestCase):
    """Archiving must change WHICH ROWS a standings table contains and nothing
    else: no refusal shape moves, no read becomes an existence oracle, and the
    archived Season stays write-refused."""

    maxDiff = None

    def test_archived_season_is_still_write_refused(self):
        for label, store in _backends():
            with self.subTest(backend=label):
                api = ApiService(store)
                try:
                    fx = _Fixture().build(api)
                    api.setup.archive_season(fx["season"], actor_id=ADMIN,
                                             reason="season over")
                    c = api.create_team(fx["club"], None, "C", actor_id=ADMIN,
                                        league_id=fx["elite"])["id"]
                    res = api.register_team_for_season(
                        fx["season"], c, fx["div"], actor_id=ADMIN,
                        league_id=fx["elite"])
                    self.assertIn("error", res, res)
                    self.assertEqual(res["error"]["details"]["reason"],
                                     "season_archived", res)
                    # Reading history is legitimate; writing it is not. The
                    # read fix must not have unlocked the write path (#409:
                    # fallback/read leniency never authorizes a mutation).
                    self.assertNotIn(
                        c,
                        [r["team_id"]
                         for r in api.get_standings(fx["div"])["standings"]],
                        label)
                finally:
                    _close(store)

    def test_refusal_shapes_are_identical_before_and_after_archiving(self):
        """Every refusal/empty payload these readers can produce is byte-equal
        across the archive transition, so the lifecycle state of a Season can
        never be inferred from the SHAPE of an answer — only from the rows the
        caller was already entitled to see."""
        for label, store in _backends():
            with self.subTest(backend=label):
                api = ApiService(store)
                try:
                    fx = _Fixture().build(api)

                    def refusals():
                        return {
                            # A division that does not exist reads exactly like
                            # an empty one — never an existence oracle.
                            "unknown_division": api.get_standings("nope"),
                            "unknown_public_division":
                                api.get_public_standings("nope"),
                            "unknown_league_season":
                                api.get_league_season_standings("nope",
                                                                fx["season"]),
                            "unknown_public_league_season":
                                api.get_public_league_season_standings(
                                    fx["elite"], "nope"),
                        }

                    before = refusals()
                    api.setup.archive_season(fx["season"], actor_id=ADMIN,
                                             reason="season over")
                    self.assertEqual(refusals(), before, label)
                    # And the not_found refusal for an unknown LeagueSeason
                    # carries no identifiers back.
                    err = before["unknown_league_season"]["error"]
                    self.assertEqual(err["code"], "not_found", err)
                    self.assertNotIn("details", err, err)
                finally:
                    _close(store)


class ExplicitArchivedSeasonIsNotTheFallback(unittest.TestCase):
    """Contract 2b, on the read this PR touches.

    ``get_standings`` is not a bare id lookup: when a real caller context is
    supplied it is bound to the ACTIVE resolved tuple (#369/#367), so the
    RESOLVER decides which Season the answer is about. That makes it the exact
    place #409's silent-retarget hazard reappears on the read side — an operator
    who NAMES an archived Season must be answered about THAT Season, never
    quietly re-aimed at the deterministic fallback's live Season.

    Both halves are needed. Answering about the archived Season is what the
    fix in this PR delivers (without it the tuple resolves fine but the table
    it returns is wrong after a later transfer). Answering about the RIGHT
    Season is #410/#411 behavior this read must not undo — and it is only a
    real assertion if the fallback's Season is built, shown to be what an
    unsaved caller actually resolves to, and shown to be distinguishable
    through this very read. A test that stopped at "no error came back" would
    pass in the retargeted world too.
    """

    maxDiff = None

    def _fallback_sibling(self, api, fx):
        """An ACTIVE Season in the SAME Program with its own League, Division
        and Teams — what the deterministic fallback resolves to once S1 is
        archived, and whose rows must never be served as S1's history."""
        s2 = api.create_season(fx["prog"], "S2", actor_id=ADMIN)["id"]
        l2 = api.create_league(s2, "Elite2", actor_id=ADMIN)["id"]
        d2 = api.create_division_v2(l2, "DB", actor_id=ADMIN)["id"]
        teams = []
        for name in ("C", "D"):
            t = api.create_team(fx["club"], None, name, actor_id=ADMIN,
                                league_id=l2)["id"]
            reg = api.register_team_for_season(s2, t, d2, actor_id=ADMIN,
                                               league_id=l2)
            assert "error" not in reg, reg
            teams.append(t)
        return dict(season=s2, league=l2, div=d2, teams=teams)

    def test_named_archived_season_is_answered_not_replaced_by_the_fallback(self):
        for label, store in _backends():
            with self.subTest(backend=label):
                api = ApiService(store)
                try:
                    fx = _Fixture().build(api)
                    api.setup.archive_season(fx["season"], actor_id=ADMIN,
                                             reason="season over")
                    sib = self._fallback_sibling(api, fx)
                    self.assertNotEqual(sib["season"], fx["season"], label)

                    # The fallback really is the OTHER Season: a caller with no
                    # saved selection resolves to it. Everything below is a
                    # claim about telling these two apart.
                    unsaved = api.get_active_context("unsaved", *OPERATOR)
                    self.assertEqual(unsaved["season_id"], sib["season"],
                                     (label, unsaved))
                    # ...and this read can actually tell them apart: from the
                    # fallback's context the archived Division is the generic
                    # empty shape while the fallback's own Division is not.
                    self.assertEqual(
                        api.get_standings(fx["div"], "unsaved", *OPERATOR),
                        {"division_id": fx["div"], "standings": []}, label)
                    self.assertEqual(
                        [r["team_id"] for r in api.get_standings(
                            sib["div"], "unsaved", *OPERATOR)["standings"]],
                        sib["teams"], label)

                    # 2b: NAMING the archived Season opens THAT Season, read-only.
                    sel = api.set_active_context("hist", *OPERATOR, fx["prog"],
                                                 fx["season"])
                    self.assertEqual(sel["season_id"], fx["season"],
                                     (label, sel))
                    self.assertTrue(sel["read_only"], (label, sel))
                    self.assertEqual(
                        api.get_active_context("hist", *OPERATOR)["season_id"],
                        fx["season"], label)

                    def named_history():
                        """Every table reached by NAMING the archived Season —
                        the context-scoped Division read and the two
                        LeagueSeason reads that carry the Season id in-band."""
                        return {
                            "scoped_division": api.get_standings(
                                fx["div"], "hist", *OPERATOR),
                            "league_season": api.get_league_season_standings(
                                fx["elite"], fx["season"]),
                            "public_league_season":
                                api.get_public_league_season_standings(
                                    fx["elite"], fx["season"]),
                        }

                    before = named_history()
                    scoped = before["scoped_division"]
                    # It is THAT Season's history — the real archived table,
                    # identical to the unscoped read of the same Division...
                    self.assertEqual(scoped, api.get_standings(fx["div"]),
                                     label)
                    self.assertEqual([r["team_id"] for r in scoped["standings"]],
                                     [fx["a"], fx["b"]], label)
                    self.assertEqual({r["team_id"]: r["pts"]
                                      for r in scoped["standings"]},
                                     {fx["a"]: 2, fx["b"]: 0}, label)
                    # ...and NOT the fallback's Season: none of S2's teams
                    # appear, and S2's own Division is empty from here, which is
                    # exactly what a silent retarget would have inverted.
                    self.assertTrue(
                        set(sib["teams"]).isdisjoint(
                            r["team_id"] for r in scoped["standings"]), label)
                    self.assertEqual(
                        api.get_standings(sib["div"], "hist", *OPERATOR),
                        {"division_id": sib["div"], "standings": []}, label)
                    for key in ("league_season", "public_league_season"):
                        self.assertEqual(before[key]["season_id"],
                                         fx["season"], (label, key))

                    # And the whole point of #159: a later legitimate transfer
                    # leaves the NAMED history byte-for-byte identical.
                    self.assertNotIn(
                        "error",
                        api.transfer_team_to_league(fx["a"], fx["rec"],
                                                    actor_id=ADMIN))
                    after = named_history()
                    for key in before:
                        self.assertEqual(
                            after[key], before[key],
                            f"{label}: explicitly-requested archived history "
                            f"changed in {key} after a later transfer")
                finally:
                    _close(store)


class ArchivedHistoryOverHttpContract:
    """The same property at the boundary a browser actually talks to.

    The in-process classes above prove the rule inside `ApiService`; this proves
    the four standings ROUTES serve it, so the fix cannot be true in the service
    and lost in the handler. Each subclass supplies the store the server runs
    on."""

    maxDiff = None

    def database_url(self):
        raise NotImplementedError

    def setUp(self):
        self._prev_db = os.environ.get("DATABASE_URL")
        self._tmp_path = None
        url = self.database_url()
        if url:
            os.environ["DATABASE_URL"] = url
        else:
            os.environ.pop("DATABASE_URL", None)
        self.addCleanup(self._restore_environment)
        STATE.reset()
        srv.RATE_LIMITER.reset()
        srv.LOGIN_THROTTLE.reset()
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever,
                                       daemon=True)
        self.thread.start()
        self.api = STATE.api

    def tearDown(self):
        self.httpd.shutdown()
        self.thread.join(timeout=5)
        self.httpd.server_close()

    def _restore_environment(self):
        if self._prev_db is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = self._prev_db
        try:
            STATE.reset()
        except Exception:
            pass
        if self._tmp_path:
            try:
                os.remove(self._tmp_path)
            except OSError:
                pass

    def _req(self, method, path, body=None, opener=None):
        url = f"http://127.0.0.1:{self.port}{path}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        op = opener or urllib.request.build_opener()
        try:
            with op.open(req) as r:
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def _login(self, username="admin"):
        op = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(CookieJar()))
        status, body = self._req("POST", "/api/auth/login",
                                 {"username": username, "password": "demo"},
                                 opener=op)
        self.assertEqual(status, 200, body)
        return op

    def _select(self, fx, opener, expect_read_only):
        """NAME this Program+Season over HTTP. The operator standings route is
        bound to the ACTIVE resolved tuple (#369/#367), so this is contract 2b
        at the boundary: an EXPLICIT selection of the archived Season must be
        accepted and answered about THAT Season."""
        status, body = self._req(
            "POST", "/api/context",
            {"program_id": fx["prog"], "season_id": fx["season"]},
            opener=opener)
        self.assertEqual(status, 200, body)
        self.assertEqual(body["season_id"], fx["season"], body)
        self.assertEqual(bool(body["read_only"]), expect_read_only, body)
        return body

    def _http_tables(self, fx, opener):
        """The four standings ROUTES: operator + public, Division +
        LeagueSeason. Status is asserted alongside the body so a route that
        started refusing cannot pass as 'unchanged'."""
        out = {}
        for key, path, op in (
                ("division", f"/api/standings/{fx['div']}", opener),
                ("league_season",
                 f"/api/standings/league-season/{fx['elite']}/{fx['season']}",
                 opener),
                ("public_division", f"/api/public/standings/{fx['div']}", None),
                ("public_league_season",
                 f"/api/public/standings/league-season/{fx['elite']}/"
                 f"{fx['season']}", None)):
            out[key] = self._req("GET", path, opener=op)
        return out

    def test_archived_standings_over_http_survive_a_later_transfer(self):
        api = self.api
        fx = _Fixture().build(api)
        admin = self._login()
        self._select(fx, admin, expect_read_only=False)

        live = self._http_tables(fx, admin)
        for key, (status, body) in live.items():
            self.assertEqual(status, 200, (key, body))
            self.assertEqual([r["team_id"] for r in body["standings"]],
                             [fx["a"], fx["b"]], (key, body))

        api.setup.archive_season(fx["season"], actor_id=ADMIN,
                                 reason="season over")
        self.assertIsNone(api.store.get_season(fx["season"]).end_date)
        # 2b over HTTP: re-NAME the now-archived Season. It must be accepted
        # (read-only), not silently swapped for the fallback's live Season.
        self._select(fx, admin, expect_read_only=True)

        before = self._http_tables(fx, admin)
        self.assertEqual(before, live,
                         "archiving alone changed a standings ROUTE's answer")

        self.assertNotIn("error", api.transfer_team_to_league(
            fx["a"], fx["rec"], actor_id=ADMIN))
        self.assertEqual(api.store.get_team(fx["a"]).league_id, fx["rec"])

        after = self._http_tables(fx, admin)
        for key in before:
            self.assertEqual(after[key], before[key],
                             f"archived history changed over HTTP in {key} "
                             f"after a later transfer")

    def test_current_season_standings_over_http_are_unaffected(self):
        """Item 2 at the boundary: on a CURRENT (unarchived) Season every route
        still returns exactly the live answer, including the rule-7 exclusion of
        a Team whose permanent League has drifted away."""
        api = self.api
        fx = _Fixture().build(api)
        admin = self._login()
        self._select(fx, admin, expect_read_only=False)
        before = self._http_tables(fx, admin)

        team = api.store.get_team(fx["a"])
        team.league_id = fx["rec"]
        api.store.save_team(team)

        after = self._http_tables(fx, admin)
        for key, (status, body) in after.items():
            self.assertEqual(status, 200, (key, body))
            ids = [r["team_id"] for r in body["standings"]]
            self.assertNotIn(fx["a"], ids,
                             f"a live Season must still exclude a cross-League "
                             f"Team from {key} over HTTP")
            self.assertIn(fx["b"], ids, key)
            self.assertNotEqual(after[key], before[key], key)


class MemoryArchivedHistoryOverHttpTest(ArchivedHistoryOverHttpContract,
                                        unittest.TestCase):
    def database_url(self):
        return None


class SqliteArchivedHistoryOverHttpTest(ArchivedHistoryOverHttpContract,
                                        unittest.TestCase):
    def database_url(self):
        fd, path = tempfile.mkstemp(suffix=".sqlite")
        os.close(fd)
        self._tmp_path = path
        return path


@unittest.skipUnless(os.environ.get("TEST_DATABASE_URL"),
                     "PostgreSQL not configured (TEST_DATABASE_URL)")
class PostgresArchivedHistoryOverHttpTest(ArchivedHistoryOverHttpContract,
                                          unittest.TestCase):
    def database_url(self):
        return os.environ["TEST_DATABASE_URL"]


if __name__ == "__main__":
    unittest.main()
