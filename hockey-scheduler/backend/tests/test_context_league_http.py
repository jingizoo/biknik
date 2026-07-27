"""Persistent League context over authenticated HTTP (#345 / #360).

PR #356 landed the League axis in the service/store/scope layer and deliberately
left it unwired. This is the TRANSPORT slice: ``GET``/``POST /api/context`` and
``GET /api/context/options`` now carry an additive, nullable ``league_id`` +
``league``, reusing ``ContextService.resolve_with_league`` /
``set_with_league`` / ``options_with_league`` unchanged. No browser asset, Setup
rendering, or navigation is touched.

Everything here goes through a REAL ``ThreadingHTTPServer`` running the real
``Handler`` against a real ``ApiService``, driven with real session cookies — so
what is proven is the wired contract, not a facade call that merely resembles
it. The same body runs on Memory, SQLite (temp file) and PostgreSQL (when
``TEST_DATABASE_URL`` is set), because the guarantees under test — rejection
atomicity, ignore-don't-rewrite, and the unbind race — are exactly the ones a
store's transaction semantics can differ on.

What each group is really for:

* **Contract** — the two pre-existing axes are byte-identical and the two new
  keys are ALWAYS present, so a client never distinguishes "absent" from "null".
* **Rejection** — every invalid League (nonexistent, cross-Program,
  unauthorized, deleted, Season-unbound, ambiguous) returns ONE indistinguishable
  response and changes ZERO context rows. Distinguishing them would turn the
  endpoint into an existence oracle for records the caller may not see.
* **Never creates a binding** — the context endpoint resolves a ``LeagueSeason``
  read-only. Creating or "repairing" one here would let a view preference
  silently manufacture competition structure.
* **Fallback** — a saved League that later becomes invalid resolves as null but
  is NOT rewritten, so restoring the League/binding/authorization restores the
  choice. Proven by round-tripping through HTTP, not by inspecting the service.
* **Scope** — a scoped account can neither select nor ENUMERATE a League outside
  its scope, with role/scope threaded from the real session.
"""

import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.cookiejar import CookieJar
from http.server import ThreadingHTTPServer

from helpers import BACKEND  # noqa: F401  (ensures sys.path is set up)

import hockey_scheduler.web.server as srv
from hockey_scheduler.domain import GameType, OfficialRole, Role
from hockey_scheduler.domain.models import Game
from hockey_scheduler.domain.setup_models import LeagueSeason, OfficialAssignment
from hockey_scheduler.store import InMemoryStore
from hockey_scheduler.web.server import STATE, Handler

PASSWORD = "demo"
# The exact key set the wired payload must expose — asserted whole so an
# accidental extra field is a failure, not a silent contract widening.
CONTEXT_KEYS = {"program_id", "season_id", "league_id", "read_only",
                "program", "season", "league"}


class LeagueContextHttpContract:
    """Shared body; each subclass supplies the store the server runs on."""

    def database_url(self):
        raise NotImplementedError

    # -- harness -----------------------------------------------------------
    def setUp(self):
        self._prev_db = os.environ.get("DATABASE_URL")
        self._tmp_path = None
        url = self.database_url()
        if url:
            os.environ["DATABASE_URL"] = url
        else:
            os.environ.pop("DATABASE_URL", None)
        # Registered BEFORE the first thing that can fail, because DATABASE_URL
        # and STATE are process-global: if setUp raised past this point (an
        # unreachable PostgreSQL, say) tearDown would never run, and every later
        # test in the process — including ones in other modules — would inherit
        # a DATABASE_URL pointing at this test's store. addCleanup runs even
        # when setUp fails, so the pollution cannot escape.
        self.addCleanup(self._restore_environment)
        # Seeds the demo world AND the six real persona accounts, so every
        # request below authenticates through the ordinary login route.
        STATE.reset()
        srv.RATE_LIMITER.reset()
        srv.LOGIN_THROTTLE.reset()
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever,
                                       daemon=True)
        self.thread.start()
        self.api = STATE.api
        self.store = self.api.store

    def tearDown(self):
        self.httpd.shutdown()
        self.thread.join(timeout=5)
        self.httpd.server_close()      # release the listening socket per test

    def _restore_environment(self):
        """Undo every process-global effect, in the reverse order they were
        applied. Runs via addCleanup, so it fires whether the test passed,
        failed, or never started."""
        if self._prev_db is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = self._prev_db
        # Rebuild on the RESTORED url: this both closes the store this test ran
        # against and leaves the module-level singleton usable for other test
        # modules, which share it. A failure here must not mask the test's own
        # result, nor skip the temp-file cleanup below.
        try:
            STATE.reset()
        except Exception:
            pass
        if self._tmp_path:
            try:
                os.remove(self._tmp_path)
            except OSError:
                pass

    def _req(self, method, path, body=None, opener=None, role=None):
        url = f"http://127.0.0.1:{self.port}{path}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        if role is not None:
            req.add_header("X-Demo-Role", role)
        op = opener or urllib.request.build_opener()
        try:
            with op.open(req) as r:
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def _login(self, username):
        op = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(CookieJar()))
        status, body = self._req(
            "POST", "/api/auth/login",
            {"username": username, "password": PASSWORD}, opener=op)
        self.assertEqual(status, 200, body)
        return op

    # -- fixtures ----------------------------------------------------------
    def _program_season_league(self, pname="P1", sname="S1", lname="Gold"):
        """Program + Season + a League BOUND to that Season."""
        pid = self.api.create_program(pname, "US", "UTC")["id"]
        sid = self.api.create_season(pid, sname)["id"]
        lid = self.api.create_league(sid, lname)["id"]
        return pid, sid, lid

    def _account(self, username, role, scope=None):
        """A real account with a DETERMINISTIC id, so a test can assert against
        the persisted context row (keyed by account id) the same way it can for
        the seeded personas."""
        self.api.accounts.create_account(
            username, PASSWORD, role, scope=scope or {}, actor_id="test_seed",
            account_id=f"user_{username}")
        return self._login(username)

    def _saved_row(self, user_id):
        return self.store.get_active_context(user_id)

    def _saved_league(self, user_id):
        """The persisted League id, treating "no context row at all" and "a row
        with no League" alike — both mean nothing was selected. A rejected FIRST
        request leaves no row, so a plain attribute read would mask the
        zero-write proof behind an AttributeError."""
        row = self._saved_row(user_id)
        return row.league_id if row is not None else None

    def _league_season_count(self):
        return len(self.store.all_league_seasons())

    def _unbind(self, league_id, season_id):
        ls = self.store.league_season_for(league_id, season_id)
        if ls is not None:
            with self.store.transaction():
                self.store.delete_league_season(ls.id)

    # -- contract ----------------------------------------------------------
    def test_get_carries_the_league_axis_and_post_round_trips_it(self):
        admin = self._login("admin")
        pid, sid, lid = self._program_season_league()

        status, got = self._req("GET", "/api/context", opener=admin)
        self.assertEqual(status, 200, got)
        self.assertEqual(set(got), CONTEXT_KEYS, got)

        status, posted = self._req(
            "POST", "/api/context",
            {"program_id": pid, "season_id": sid, "league_id": lid},
            opener=admin)
        self.assertEqual(status, 200, posted)
        self.assertEqual(set(posted), CONTEXT_KEYS, posted)
        self.assertEqual(posted["league_id"], lid, posted)
        # The League is rendered as a full object, exactly like Program/Season —
        # not merely an id the client would have to resolve separately.
        self.assertEqual(posted["league"]["id"], lid, posted)
        self.assertEqual((posted["program_id"], posted["season_id"]),
                         (pid, sid), posted)

        # PERSISTED: a fresh request on a fresh session reads the same League.
        status, reread = self._req("GET", "/api/context", opener=self._login("admin"))
        self.assertEqual(status, 200, reread)
        self.assertEqual(reread["league_id"], lid, reread)
        self.assertEqual(reread["league"]["id"], lid, reread)

    def test_league_keys_are_present_and_null_when_no_league_is_selected(self):
        """A null League is a first-class state (Program-only, and
        Season-without-League), never an omission or an error."""
        admin = self._login("admin")
        pid, sid, _lid = self._program_season_league()
        for body in ({"program_id": pid, "season_id": sid},
                     {"program_id": pid, "season_id": None},
                     {"program_id": pid, "season_id": sid, "league_id": None}):
            status, got = self._req("POST", "/api/context", body, opener=admin)
            self.assertEqual(status, 200, got)
            self.assertEqual(set(got), CONTEXT_KEYS, got)
            self.assertIsNone(got["league_id"], (body, got))
            self.assertIsNone(got["league"], (body, got))

    def test_omitting_league_id_clears_a_previously_saved_league(self):
        """The two-field body keeps its exact pre-#360 meaning: it selects "no
        League" rather than carrying one onto a Program/Season it was not chosen
        for. Proving it over HTTP is the point — every existing client sends
        exactly this body."""
        admin = self._login("admin")
        pid, sid, lid = self._program_season_league()
        self._req("POST", "/api/context",
                  {"program_id": pid, "season_id": sid, "league_id": lid},
                  opener=admin)
        self.assertEqual(self._saved_row("user_admin").league_id, lid)

        status, got = self._req("POST", "/api/context",
                                {"program_id": pid, "season_id": sid},
                                opener=admin)
        self.assertEqual(status, 200, got)
        self.assertIsNone(got["league_id"], got)
        self.assertIsNone(self._saved_row("user_admin").league_id)

    def test_options_offer_leagues_and_mark_the_current_selection(self):
        admin = self._login("admin")
        pid, sid, lid = self._program_season_league()
        self._req("POST", "/api/context",
                  {"program_id": pid, "season_id": sid, "league_id": lid},
                  opener=admin)

        status, opts = self._req("GET", "/api/context/options", opener=admin)
        self.assertEqual(status, 200, opts)
        mine = next(p for p in opts["programs"] if p["id"] == pid)
        self.assertIn("leagues", mine, mine)
        self.assertIn(lid, [lg["id"] for lg in mine["leagues"]], mine)
        self.assertEqual({"id", "name"}, set(mine["leagues"][0]), mine)
        self.assertEqual(opts["selected"]["league_id"], lid, opts)

    def test_options_offer_a_league_not_bound_to_the_selected_season(self):
        """Program-scoped by design: selecting such a League is a legitimate way
        to move to a Season+League pair, so it is offered here and the binding
        rule is enforced at selection time with a precise reason instead of
        silently by omission."""
        admin = self._login("admin")
        pid, sid, lid = self._program_season_league()
        s2 = self.api.create_season(pid, "S2")["id"]
        self._req("POST", "/api/context", {"program_id": pid, "season_id": s2},
                  opener=admin)

        _status, opts = self._req("GET", "/api/context/options", opener=admin)
        mine = next(p for p in opts["programs"] if p["id"] == pid)
        self.assertIn(lid, [lg["id"] for lg in mine["leagues"]], mine)
        # ...and actually selecting it against the unbound Season is refused.
        status, _ = self._req(
            "POST", "/api/context",
            {"program_id": pid, "season_id": s2, "league_id": lid}, opener=admin)
        self.assertEqual(status, 404)

    # -- rejection: one stable response, zero writes -----------------------
    def test_every_invalid_league_is_rejected_identically_and_writes_nothing(self):
        """Nonexistent / cross-Program / Season-unbound are INDISTINGUISHABLE.
        Any difference in status, code or reason would leak whether a record the
        caller may not see exists."""
        admin = self._login("admin")
        pid, sid, lid = self._program_season_league()
        other_p, other_s, other_l = self._program_season_league(
            "P2", "S2", "Silver")
        unbound_season = self.api.create_season(pid, "S-unbound")["id"]

        # A valid baseline selection, so a rejected attempt has something to
        # (fail to) overwrite — proving zero-change, not merely zero-creation.
        self._req("POST", "/api/context",
                  {"program_id": pid, "season_id": sid, "league_id": lid},
                  opener=admin)
        before = self._saved_row("user_admin")
        bindings_before = self._league_season_count()

        attempts = {
            "nonexistent": {"program_id": pid, "season_id": sid,
                            "league_id": "league_does_not_exist"},
            "cross_program": {"program_id": pid, "season_id": sid,
                              "league_id": other_l},
            "season_unbound": {"program_id": pid, "season_id": unbound_season,
                               "league_id": lid},
        }
        seen = set()
        for label, body in attempts.items():
            status, resp = self._req("POST", "/api/context", body, opener=admin)
            self.assertEqual(status, 404, (label, resp))
            seen.add((status, resp["error"]["code"],
                      resp["error"].get("details", {}).get("reason")))
            after = self._saved_row("user_admin")
            self.assertEqual(
                (after.program_id, after.season_id, after.league_id),
                (before.program_id, before.season_id, before.league_id),
                f"{label} changed the saved context row")
        self.assertEqual(len(seen), 1,
                         f"rejections are distinguishable: {seen}")
        self.assertEqual(self._league_season_count(), bindings_before)

    def test_ambiguous_binding_fails_closed_over_http(self):
        """A duplicate LeagueSeason must refuse rather than pick a winner.

        Two rows at one exact (league, season) key is CORRUPTED state, and SQL's
        ``ux_league_season`` blocks the duplicate INSERT outright — so, exactly
        as the service-level sibling test documents, this shape is only
        constructible on InMemoryStore. Skipped rather than silently omitted on
        the SQL backends, so the coverage gap is visible in the run output
        instead of looking like it passed everywhere."""
        if not isinstance(self.store, InMemoryStore):
            self.skipTest(
                "ux_league_season makes a duplicate binding unconstructible on "
                "SQL; the guard is exercised on the in-memory store")
        admin = self._login("admin")
        pid, sid, lid = self._program_season_league()
        with self.store.transaction():
            self.store.save_league_season(LeagueSeason(
                id="ls_duplicate", league_id=lid, season_id=sid))
        before = self._league_season_count()

        status, resp = self._req(
            "POST", "/api/context",
            {"program_id": pid, "season_id": sid, "league_id": lid},
            opener=admin)
        self.assertEqual(status, 404, resp)
        # Nothing was written at all — this was the user's first selection, so
        # the rejection must leave the context table untouched, not merely leave
        # league_id null on a row it created anyway.
        self.assertIsNone(self._saved_row("user_admin"))
        self.assertIsNone(self._saved_league("user_admin"))
        self.assertEqual(self._league_season_count(), before)

    def test_context_endpoint_never_creates_or_repairs_a_binding(self):
        """Selecting an unbound Season+League pair must not manufacture the
        LeagueSeason that would make it valid — binding stays the authorized,
        audited job of setup_service."""
        admin = self._login("admin")
        pid, sid, lid = self._program_season_league()
        s2 = self.api.create_season(pid, "S2")["id"]
        before = self._league_season_count()

        status, _ = self._req(
            "POST", "/api/context",
            {"program_id": pid, "season_id": s2, "league_id": lid}, opener=admin)
        self.assertEqual(status, 404)
        self.assertEqual(self._league_season_count(), before)
        self.assertIsNone(self.store.league_season_for(lid, s2))
        # Retrying does not accumulate anything either.
        self._req("POST", "/api/context",
                  {"program_id": pid, "season_id": s2, "league_id": lid},
                  opener=admin)
        self.assertEqual(self._league_season_count(), before)

    # -- authentication + strict schema ------------------------------------
    def test_identity_less_callers_cannot_read_or_set_a_league(self):
        pid, sid, lid = self._program_season_league()
        body = {"program_id": pid, "season_id": sid, "league_id": lid}
        self.assertEqual(self._req("GET", "/api/context")[0], 401)
        self.assertEqual(self._req("POST", "/api/context", body)[0], 401)
        self.assertEqual(
            self._req("GET", "/api/context", role="league_admin")[0], 401)
        self.assertEqual(
            self._req("POST", "/api/context", body, role="league_admin")[0], 401)
        os.environ["DEMO_HEADERLESS_ADMIN"] = "1"
        try:
            self.assertEqual(self._req("GET", "/api/context")[0], 401)
            self.assertEqual(self._req("POST", "/api/context", body)[0], 401)
        finally:
            os.environ.pop("DEMO_HEADERLESS_ADMIN", None)
        self.assertIsNone(self._saved_row("user_admin"))

    def test_strict_schema_still_applies_with_league_id(self):
        admin = self._login("admin")
        pid, _sid, _lid = self._program_season_league()
        status, body = self._req(
            "POST", "/api/context",
            {"program_id": pid, "league_id": "l", "extra": 1}, opener=admin)
        self.assertEqual(status, 400, body)
        self.assertEqual(body["error"]["details"]["reason"], "unknown_field")

        status, body = self._req("POST", "/api/context",
                                 {"program_id": pid, "league_id": 5},
                                 opener=admin)
        self.assertEqual(status, 400, body)
        self.assertEqual(body["error"]["details"]["reason"], "wrong_type")

    # -- a permanent League across Seasons ---------------------------------
    def test_one_permanent_league_selectable_in_each_bound_season(self):
        """A League is permanent and participates in many Seasons; each
        Season+League pair is independently selectable, and the pair that was
        never bound is refused."""
        admin = self._login("admin")
        pid, s1, lid = self._program_season_league()
        s2 = self.api.create_season(pid, "S2")["id"]
        s3 = self.api.create_season(pid, "S3")["id"]   # deliberately unbound
        self.api.setup.create_league_season(lid, s2, actor_id="test_seed")

        for season in (s1, s2):
            status, got = self._req(
                "POST", "/api/context",
                {"program_id": pid, "season_id": season, "league_id": lid},
                opener=admin)
            self.assertEqual(status, 200, got)
            self.assertEqual((got["season_id"], got["league_id"]), (season, lid))

        status, _ = self._req(
            "POST", "/api/context",
            {"program_id": pid, "season_id": s3, "league_id": lid}, opener=admin)
        self.assertEqual(status, 404)

    def test_multi_program_selection_stays_isolated(self):
        admin = self._login("admin")
        p1, s1, l1 = self._program_season_league("P1", "S1", "Gold")
        p2, s2, l2 = self._program_season_league("P2", "S2", "Silver")

        status, got = self._req(
            "POST", "/api/context",
            {"program_id": p2, "season_id": s2, "league_id": l2}, opener=admin)
        self.assertEqual(status, 200, got)
        self.assertEqual(got["league_id"], l2)
        # P1's League is invisible to a P2 context, and the P2 selection stands.
        status, _ = self._req(
            "POST", "/api/context",
            {"program_id": p2, "season_id": s2, "league_id": l1}, opener=admin)
        self.assertEqual(status, 404)
        _s, still = self._req("GET", "/api/context", opener=admin)
        self.assertEqual(still["league_id"], l2, still)

    # -- fallback: ignore, never rewrite -----------------------------------
    def test_unbinding_drops_the_league_to_null_without_rewriting_it(self):
        """The saved id survives an invalidating change, so re-binding restores
        the operator's choice. Read back over HTTP, written back by the store."""
        admin = self._login("admin")
        pid, sid, lid = self._program_season_league()
        self._req("POST", "/api/context",
                  {"program_id": pid, "season_id": sid, "league_id": lid},
                  opener=admin)

        self._unbind(lid, sid)
        _s, got = self._req("GET", "/api/context", opener=admin)
        self.assertIsNone(got["league_id"], got)
        self.assertIsNone(got["league"], got)
        # NOT rewritten — the row still remembers the choice.
        self.assertEqual(self._saved_row("user_admin").league_id, lid)

        self.api.setup.create_league_season(lid, sid, actor_id="test_seed")
        _s, restored = self._req("GET", "/api/context", opener=admin)
        self.assertEqual(restored["league_id"], lid, restored)

    def test_deleted_league_drops_to_null_and_is_no_longer_selectable(self):
        """Deletion of the permanent League itself, as distinct from unbinding
        it from a Season: the resolved context drops the League but keeps the
        Program/Season the operator validly chose, the saved id is still not
        rewritten, and re-selecting the dead id is refused like any other
        invalid League."""
        admin = self._login("admin")
        pid, sid, lid = self._program_season_league()
        self._req("POST", "/api/context",
                  {"program_id": pid, "season_id": sid, "league_id": lid},
                  opener=admin)

        self._unbind(lid, sid)                 # FK-safe order: binding, then row
        with self.store.transaction():
            self.store.delete_league(lid)

        _s, got = self._req("GET", "/api/context", opener=admin)
        self.assertEqual((got["program_id"], got["season_id"]), (pid, sid), got)
        self.assertIsNone(got["league_id"], got)
        self.assertIsNone(got["league"], got)
        self.assertEqual(self._saved_league("user_admin"), lid)   # not rewritten

        _s, opts = self._req("GET", "/api/context/options", opener=admin)
        offered = {lg["id"] for p in opts["programs"] for lg in p["leagues"]}
        self.assertNotIn(lid, offered, opts)
        status, _ = self._req(
            "POST", "/api/context",
            {"program_id": pid, "season_id": sid, "league_id": lid}, opener=admin)
        self.assertEqual(status, 404)

    def test_options_and_context_agree_after_the_league_becomes_invalid(self):
        """The switcher must never offer, nor mark selected, a League the
        endpoint would now refuse."""
        admin = self._login("admin")
        pid, sid, lid = self._program_season_league()
        self._req("POST", "/api/context",
                  {"program_id": pid, "season_id": sid, "league_id": lid},
                  opener=admin)
        self._unbind(lid, sid)

        _s, opts = self._req("GET", "/api/context/options", opener=admin)
        _s2, ctx = self._req("GET", "/api/context", opener=admin)
        self.assertEqual(opts["selected"]["league_id"], ctx["league_id"],
                         (opts["selected"], ctx))
        self.assertIsNone(opts["selected"]["league_id"], opts)

    # -- scope: a scoped account cannot select or enumerate outside it ------
    def test_scoped_coach_sees_and_selects_only_its_own_league(self):
        pid = self.api.create_program("P1", "US", "UTC")["id"]
        sid = self.api.create_season(pid, "S1")["id"]
        mine = self.api.create_league(sid, "Gold")["id"]
        theirs = self.api.create_league(sid, "Silver")["id"]
        did = self.api.create_division(sid, "D1", league_id=mine)["id"]
        club = self.api.create_club("Club")["id"]
        team = self.api.create_team(club_id=club, name="Alpha",
                                    league_id=mine, division_id=did)["id"]
        self.api.setup.register_team_for_season(sid, team, did)
        coach = self._account("coach2", Role.COACH, {"team_id": team})

        _s, opts = self._req("GET", "/api/context/options", opener=coach)
        offered = {lg["id"] for p in opts["programs"] for lg in p["leagues"]}
        self.assertEqual(offered, {mine}, opts)

        status, got = self._req(
            "POST", "/api/context",
            {"program_id": pid, "season_id": sid, "league_id": mine},
            opener=coach)
        self.assertEqual(status, 200, got)
        self.assertEqual(got["league_id"], mine, got)

        status, _ = self._req(
            "POST", "/api/context",
            {"program_id": pid, "season_id": sid, "league_id": theirs},
            opener=coach)
        self.assertEqual(status, 404)

    def test_official_league_comes_from_assignments_and_revocation_drops_it(self):
        """An Official's League is derived from the frozen competition identity
        of the Games they are assigned to (the rule PR #356 established). Over
        HTTP that means: selectable while assigned; resolves to null once the
        assignment is gone, WITHOUT rewriting the saved choice."""
        pid = self.api.create_program("P1", "US", "UTC")["id"]
        sid = self.api.create_season(pid, "S1")["id"]
        lid = self.api.create_league(sid, "Gold")["id"]
        did = self.api.create_division(sid, "D1", league_id=lid)["id"]
        club = self.api.create_club("Club")["id"]
        team = self.api.create_team(club_id=club, name="Alpha",
                                    league_id=lid, division_id=did)["id"]
        self.api.setup.register_team_for_season(sid, team, did)
        oid = self.api.create_official("Ref")["id"]
        ls = self.store.league_season_for(lid, sid)
        with self.store.transaction():
            gid = self.store.next_id("game")
            self.store.add_game(Game(
                id=gid, home_team_id=team, away_team_id=team, start_time=None,
                season_id=sid, league_id=lid, league_season_id=ls.id,
                game_type=GameType.REGULAR.value))
            assign_id = self.store.next_id("assign")
            self.store.add_official_assignment(OfficialAssignment(
                id=assign_id, game_id=gid, official_id=oid,
                role=OfficialRole.REFEREE))
        official = self._account("official2", Role.OFFICIAL,
                                 {"official_id": oid})

        status, got = self._req(
            "POST", "/api/context",
            {"program_id": pid, "season_id": sid, "league_id": lid},
            opener=official)
        self.assertEqual(status, 200, got)
        self.assertEqual(got["league_id"], lid, got)

        # Revoke by removing the assignment that conferred the League.
        with self.store.transaction():
            self.store.remove_official_assignment(assign_id)
        _s, after = self._req("GET", "/api/context", opener=official)
        self.assertIsNone(after["league_id"], after)
        # Ignored, not rewritten: restoring the assignment restores the choice.
        self.assertEqual(self._saved_league("user_official2"), lid)

    # -- races -------------------------------------------------------------
    def test_unbind_racing_a_post_never_persists_an_unbound_pair(self):
        """The invariant is not "who wins" — it is that no outcome leaves a
        saved context naming a pair with no binding."""
        admin = self._login("admin")
        pid, sid, lid = self._program_season_league()
        barrier = threading.Barrier(2)
        outcome = {}

        def select():
            op = self._login("admin")
            barrier.wait()
            outcome["status"] = self._req(
                "POST", "/api/context",
                {"program_id": pid, "season_id": sid, "league_id": lid},
                opener=op)[0]

        def unbind():
            barrier.wait()
            self._unbind(lid, sid)

        threads = [threading.Thread(target=select),
                   threading.Thread(target=unbind)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(20)

        self.assertIn(outcome.get("status"), (200, 404), outcome)
        saved = self._saved_row("user_admin")
        if saved is not None and saved.league_id is not None:
            # A League was recorded — it may only be one whose binding existed
            # when the write committed.
            self.assertEqual(outcome["status"], 200, outcome)
        # Whatever happened, what the endpoint REPORTS must agree with what the
        # binding table now says — no dangling selection is ever rendered.
        _s, rendered = self._req("GET", "/api/context", opener=admin)
        if rendered["league_id"] is not None:
            self.assertIsNotNone(
                self.store.league_season_for(rendered["league_id"],
                                             rendered["season_id"]),
                rendered)


class MemoryLeagueContextHttpTest(LeagueContextHttpContract, unittest.TestCase):
    def database_url(self):
        return None                     # in-memory demo store


class SqliteLeagueContextHttpTest(LeagueContextHttpContract, unittest.TestCase):
    def database_url(self):
        # A real file, not ":memory:" — the server is threaded, and a file-backed
        # database is what an operator actually runs against.
        fd, path = tempfile.mkstemp(suffix=".sqlite")
        os.close(fd)
        self._tmp_path = path
        return path


@unittest.skipUnless(os.environ.get("TEST_DATABASE_URL"),
                     "PostgreSQL not configured (TEST_DATABASE_URL)")
class PostgresLeagueContextHttpTest(LeagueContextHttpContract,
                                    unittest.TestCase):
    def database_url(self):
        return os.environ["TEST_DATABASE_URL"]


if __name__ == "__main__":
    unittest.main()
