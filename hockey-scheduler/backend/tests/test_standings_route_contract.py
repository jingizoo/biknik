"""The two OPERATOR standings routes answer the same question about auth (#202).

    GET /api/standings/<division_id>                    per-Division
    GET /api/standings/league-season/<league>/<season>  LeagueSeason-wide

Both live in the authenticated namespace and both are the OPERATOR view of the
same competition, one level of the hierarchy apart. They had drifted: the
per-Division route required a session and bound the answer to the caller's
ACTIVE tuple, while the LeagueSeason route passed no identity at all and served
any anonymous caller.

WHY THAT WAS NOT MERELY UNTIDY, since it was first read as harmless on the
grounds that the payload matched the deliberate public route
(`/api/public/standings/league-season/...`) byte for byte. It does not, and
`test_the_operator_table_is_not_the_public_table` is the measurement: the
operator view is `public_only=False`, so it counts UNPUBLISHED games' final
results, and on a drifted Game it returns a `data_integrity_error` naming that
Game's id. The public variant skips unpublished Games BEFORE the integrity check
precisely so neither can escape (#83). The two payloads agree only until one
unpublished Game carries a final result — so the observed sameness was a
property of the seeded fixture, not of the route, and it was a real disclosure
of draft results to anonymous callers rather than a rate-limit issue alone.

WHAT IS PINNED, for BOTH routes in the same assertion loop wherever the answer
is meant to be the same, so the pair cannot silently diverge again:

  * signed out -> 401 on both, while both PUBLIC siblings still answer 200;
  * signed in with the target inside the ACTIVE tuple -> 200 with real rows;
  * signed in with a FOREIGN active tuple -> each route's own generic miss, and
    each is byte-identical to what a nonexistent target already returns, so
    neither becomes an existence oracle;
  * the operator/public divergence itself, in both directions, so a future
    "these are the same, just make it public" simplification fails here;
  * membership of both routes in the `RouteSpec.context_read_fence` class — the
    LeagueSeason route only earned that by acquiring the ceiling in #202.
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

from hockey_scheduler.web import server as srv
from hockey_scheduler.web.server import STATE, Handler, is_context_scoped_read

ADMIN = "admin"
UTC = timezone.utc

# The two operator routes under test, and the two public routes that must keep
# answering anonymously throughout — this change must not touch the deliberate
# public surface.
NO_SUCH_DIVISION = "division_does_not_exist"
NO_SUCH_LEAGUE = "league_does_not_exist"
NO_SUCH_SEASON = "season_does_not_exist"


def _at(hour):
    return datetime(2026, 9, 1, hour, tzinfo=UTC)


class _Fixture:
    """One Program / Season / League / Division with teams A and B, plus a
    SECOND Program+Season the same admin is equally authorized for (the foreign
    active tuple — authorization alone must not be enough).

    Two FINAL games between A and B: one PUBLISHED 2-0, one UNPUBLISHED 9-0.
    The draft game is what makes the operator and public tables differ, so it is
    load-bearing for `test_the_operator_table_is_not_the_public_table` rather
    than decoration.
    """

    def build(self, api):
        org = api.create_organization("Org", "O", actor_id=ADMIN)["id"]
        prog = api.create_program("Prog", operator_organization_id=org,
                                  actor_id=ADMIN)["id"]
        season = api.create_season(prog, "S1", actor_id=ADMIN)["id"]
        elite = api.create_league(season, "Elite", actor_id=ADMIN)["id"]
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

        def slot(hour):
            return api.create_ice_slot(
                rink, _at(hour).isoformat(), _at(hour + 1).isoformat(),
                "game", actor_id=ADMIN)["id"]

        def final(game_id, home, away):
            assert "error" not in api.record_result(
                game_id, home, away, actor_id=ADMIN)
            assert "error" not in api.approve_result(game_id, actor_id=ADMIN)

        published = api.create_game(season, div, a, b, slot(18), actor_id=ADMIN,
                                    league_id=elite)
        assert "error" not in published, published
        assert "error" not in api.publish_game(published["id"], actor_id=ADMIN)
        final(published["id"], 2, 0)

        draft = api.create_game(season, div, a, b, slot(20), actor_id=ADMIN,
                                league_id=elite)
        assert "error" not in draft, draft
        final(draft["id"], 9, 0)
        assert not api.store.get_game(draft["id"]).published

        # The foreign tuple: a second Program+Season, same admin.
        other_org = api.create_organization("Other", "OT", actor_id=ADMIN)["id"]
        other_prog = api.create_program(
            "Other Prog", operator_organization_id=other_org,
            actor_id=ADMIN)["id"]
        other_season = api.create_season(other_prog, "S1", actor_id=ADMIN)["id"]

        return dict(prog=prog, season=season, elite=elite, div=div, a=a, b=b,
                    published=published["id"], draft=draft["id"],
                    other_prog=other_prog, other_season=other_season)


class StandingsRouteContract:
    """Shared body; subclasses supply the store the server runs on."""

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
        self.fx = _Fixture().build(self.api)

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

    # -- HTTP -------------------------------------------------------------
    def _req(self, method, path, body=None, opener=None):
        """Returns (status, raw_bytes, parsed). The RAW body is returned too:
        the non-oracle assertions below are about bytes, not about a dict that
        happens to compare equal after parsing."""
        url = f"http://127.0.0.1:{self.port}{path}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        op = opener or urllib.request.build_opener()
        try:
            with op.open(req) as r:
                raw = r.read() or b"{}"
                return r.status, raw, json.loads(raw)
        except urllib.error.HTTPError as e:
            raw = e.read() or b"{}"
            return e.code, raw, json.loads(raw)

    def _login(self, username=ADMIN, password="demo"):
        op = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(CookieJar()))
        status, _raw, body = self._req(
            "POST", "/api/auth/login",
            {"username": username, "password": password}, opener=op)
        self.assertEqual(status, 200, body)
        return op

    def _select(self, opener, program_id, season_id):
        status, _raw, body = self._req(
            "POST", "/api/context",
            {"program_id": program_id, "season_id": season_id}, opener=opener)
        self.assertEqual(status, 200, body)
        self.assertEqual(body["season_id"], season_id, body)
        return body

    # -- paths ------------------------------------------------------------
    def _division_path(self, division_id=None):
        return f"/api/standings/{division_id or self.fx['div']}"

    def _league_season_path(self, league_id=None, season_id=None):
        return (f"/api/standings/league-season/"
                f"{league_id or self.fx['elite']}/"
                f"{season_id or self.fx['season']}")

    def _operator_paths(self):
        """The pair under test, always exercised together."""
        return (("division", self._division_path()),
                ("league_season", self._league_season_path()))

    def _public_paths(self):
        return (("public_division",
                 f"/api/public/standings/{self.fx['div']}"),
                ("public_league_season",
                 f"/api/public/standings/league-season/"
                 f"{self.fx['elite']}/{self.fx['season']}"))

    def _row(self, body, team_id):
        return {r["team_id"]: r for r in body["standings"]}.get(team_id)

    # -- the contract -----------------------------------------------------
    def test_signed_out_both_operator_routes_refuse(self):
        """No cookie, no X-Demo-Role: BOTH operator routes are 401. This is the
        assertion the LeagueSeason route used to fail while its sibling passed —
        it answered 200 with a full table."""
        for key, path in self._operator_paths():
            status, _raw, body = self._req("GET", path)
            self.assertEqual(status, 401, (key, body))
            self.assertEqual(body["error"]["code"], "unauthorized", (key, body))
            self.assertNotIn("standings", body, (key, body))

    def test_signed_out_public_siblings_still_answer(self):
        """The inverse guard, so requiring a session on the operator pair can
        never be read as "standings are private now": the deliberate public
        surface is untouched and still anonymous."""
        for key, path in self._public_paths():
            status, _raw, body = self._req("GET", path)
            self.assertEqual(status, 200, (key, body))
            self.assertIn("standings", body, (key, body))

    def test_signed_in_and_active_both_operator_routes_answer(self):
        admin = self._login()
        self._select(admin, self.fx["prog"], self.fx["season"])
        for key, path in self._operator_paths():
            status, _raw, body = self._req("GET", path, opener=admin)
            self.assertEqual(status, 200, (key, body))
            self.assertEqual({r["team_id"] for r in body["standings"]},
                             {self.fx["a"], self.fx["b"]}, (key, body))

    def test_foreign_active_tuple_gets_each_routes_own_generic_miss(self):
        """Authorization is not enough: this admin may administer BOTH Programs,
        but is ACTIVE in the other one. Each route answers exactly what it
        already answers for a target that does not exist (#369), so an
        out-of-context target is not an existence oracle."""
        admin = self._login()
        self._select(admin, self.fx["other_prog"], self.fx["other_season"])

        # Per-Division: the generic EMPTY standings shape, differing from a
        # nonexistent Division's answer only in the id it echoes back.
        status, _raw, body = self._req("GET", self._division_path(),
                                       opener=admin)
        self.assertEqual(status, 200, body)
        self.assertEqual(body, {"division_id": self.fx["div"],
                                "standings": []}, body)
        missing_status, _raw, missing = self._req(
            "GET", self._division_path(NO_SUCH_DIVISION), opener=admin)
        self.assertEqual(missing_status, 200, missing)
        self.assertEqual(missing, {"division_id": NO_SUCH_DIVISION,
                                   "standings": []}, missing)

        # LeagueSeason: byte-identical to a nonexistent (league, season) pair —
        # this route echoes nothing back, so the two answers are the same bytes.
        status, raw, body = self._req("GET", self._league_season_path(),
                                      opener=admin)
        self.assertEqual(status, 404, body)
        self.assertEqual(body["error"]["code"], "not_found", body)
        missing_status, missing_raw, _missing = self._req(
            "GET", self._league_season_path(NO_SUCH_LEAGUE, NO_SUCH_SEASON),
            opener=admin)
        self.assertEqual(missing_status, 404, missing_raw)
        self.assertEqual(raw, missing_raw,
                         "an out-of-context LeagueSeason is distinguishable "
                         "from one that does not exist")

    def test_the_operator_table_is_not_the_public_table(self):
        """The measurement behind the product call. The operator tables count
        the UNPUBLISHED 9-0; the public tables see only the published 2-0. Had
        the LeagueSeason route been made a rate-limited public alias instead,
        this is the row an anonymous caller would still be reading."""
        admin = self._login()
        self._select(admin, self.fx["prog"], self.fx["season"])

        for key, path in self._operator_paths():
            status, _raw, body = self._req("GET", path, opener=admin)
            self.assertEqual(status, 200, (key, body))
            row = self._row(body, self.fx["a"])
            self.assertEqual((row["gp"], row["gf"], row["pts"]), (2, 11, 4),
                             (key, "operator view must count the draft", row))

        for key, path in self._public_paths():
            status, _raw, body = self._req("GET", path)
            self.assertEqual(status, 200, (key, body))
            row = self._row(body, self.fx["a"])
            self.assertEqual((row["gp"], row["gf"], row["pts"]), (1, 2, 2),
                             (key, "public view must not see the draft", row))

    def test_drifted_draft_games_identifier_never_reaches_an_anonymous_caller(
            self):
        """The second half of the disclosure, and the reason a rate limit was
        not the fix: on a drifted Game the operator view fails closed with a
        `data_integrity_error` NAMING that Game. When the drifted Game is an
        unpublished one, serving the operator view anonymously handed out a
        draft Game's id — the exact leak the public view's
        skip-before-integrity-check prevents (#83)."""
        stored = self.api.store.get_game(self.fx["draft"])
        stored.league_season_id = None      # legacy pair still says Elite
        self.api.store.save_game(stored)
        draft_id = self.fx["draft"]

        for key, path in self._operator_paths():
            status, raw, body = self._req("GET", path)
            self.assertEqual(status, 401, (key, body))
            self.assertNotIn(draft_id, raw.decode(), key)

        for key, path in self._public_paths():
            status, raw, body = self._req("GET", path)
            self.assertEqual(status, 200, (key, body))
            self.assertNotIn(draft_id, raw.decode(), key)

        # Signed in and in context, the fail-closed contract is unchanged: the
        # operator is told exactly which Game to repair, on BOTH routes.
        admin = self._login()
        self._select(admin, self.fx["prog"], self.fx["season"])
        for key, path in self._operator_paths():
            status, _raw, body = self._req("GET", path, opener=admin)
            self.assertEqual(status, 400, (key, body))
            self.assertEqual(body["error"]["details"]["reason"],
                             "game_league_season_mismatch", (key, body))
            self.assertEqual(body["error"]["details"]["game_id"], draft_id,
                             (key, body))

    def test_both_operator_routes_are_context_scoped_reads(self):
        """Structural pin of the `RouteSpec.context_read_fence` pairing. Both
        routes now resolve the active tuple inside the request and refuse a
        caller-named target against it, so both need the #159 gate's arrival
        ordering; the public siblings resolve no tuple and must stay out."""
        for _key, path in self._operator_paths():
            self.assertTrue(is_context_scoped_read(path), path)
        for _key, path in self._public_paths():
            self.assertFalse(is_context_scoped_read(path), path)


class MemoryStandingsRouteContractTest(StandingsRouteContract,
                                       unittest.TestCase):
    def database_url(self):
        return None


class SqliteStandingsRouteContractTest(StandingsRouteContract,
                                       unittest.TestCase):
    def database_url(self):
        fd, path = tempfile.mkstemp(suffix=".sqlite")
        os.close(fd)
        self._tmp_path = path
        return path


@unittest.skipUnless(os.environ.get("TEST_DATABASE_URL"),
                     "PostgreSQL not configured (TEST_DATABASE_URL)")
class PostgresStandingsRouteContractTest(StandingsRouteContract,
                                         unittest.TestCase):
    def database_url(self):
        return os.environ["TEST_DATABASE_URL"]


if __name__ == "__main__":
    unittest.main()
