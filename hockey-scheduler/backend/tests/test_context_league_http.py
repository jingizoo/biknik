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
from hockey_scheduler.services import context_scope
from hockey_scheduler.store import InMemoryStore, SqlStore
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

    def is_postgres(self):
        """True only where a SECOND, independent connection can commit while a
        first one is paused mid-transaction. Memory/SQLite serialize every
        writer behind one process lock, so that shape is unreachable there by
        construction, not merely untested."""
        return False

    def _offered_leagues(self, opener, program_id):
        _s, opts = self._req("GET", "/api/context/options", opener=opener)
        program = next((p for p in opts["programs"] if p["id"] == program_id),
                       None)
        return {lg["id"] for lg in (program or {}).get("leagues", [])}

    def _transfer_scenario(self):
        """One Program/Season with TWO bound Leagues, a Team permanently in
        Gold, and every scoped role attached to it — the only shape that can
        falsify the frozen-Game-vs-live-Team rule.

        Coach/Player/Guardian derive their League from the Team's CURRENT
        ``team.league_id``, so a transfer must move them. An Official derives
        theirs from each assigned Game's FROZEN ``league_season_id``, so the
        same transfer must NOT move them. A fixture that never transfers
        cannot tell those two rules apart — both look identical before it.
        """
        pid = self.api.create_program("P", "US", "UTC")["id"]
        sid = self.api.create_season(pid, "S1")["id"]
        gold = self.api.create_league(sid, "Gold")["id"]
        silver = self.api.create_league(sid, "Silver")["id"]
        did = self.api.create_division(sid, "D1", league_id=gold)["id"]
        club = self.api.create_club("Club")["id"]
        team = self.api.create_team(club_id=club, name="Alpha",
                                    league_id=gold, division_id=did)["id"]
        self.api.setup.register_team_for_season(sid, team, did)
        player = self.api.create_player(team, "Junior", "skater")["id"]
        oid = self.api.create_official("Ref")["id"]
        ls = self.store.league_season_for(gold, sid)
        with self.store.transaction():
            gid = self.store.next_id("game")
            # CANCELLED on purpose. A Team with live scheduled games cannot be
            # transferred at all (`team_transfer_strands_games`), so cancelling
            # is exactly how an operator unblocks a real transfer — and it does
            # NOT change what the Official derives, because the derivation keys
            # on the Game's game_type and frozen league_season_id, never on its
            # status. That is what lets one fixture exercise a genuine transfer
            # and still hold the Official's frozen League fixed.
            self.store.add_game(Game(
                id=gid, home_team_id=team, away_team_id=team, start_time=None,
                season_id=sid, league_id=gold, league_season_id=ls.id,
                game_type=GameType.REGULAR.value, cancelled=True))
            self.store.add_official_assignment(OfficialAssignment(
                id=self.store.next_id("assign"), game_id=gid, official_id=oid,
                role=OfficialRole.REFEREE))
        return {"pid": pid, "sid": sid, "gold": gold, "silver": silver,
                "team": team, "player": player, "oid": oid}

    def _transfer_team(self, team_id, new_league_id):
        """Run the REAL production transfer (`setup_service.transfer_team_to_
        league`), not a hand-written `save_team`.

        It atomically moves the Team's ACTIVE registration to the target
        League's LeagueSeason for the same Season, which is what keeps the
        Team-derived roles authorized afterwards — a bare `team.league_id`
        write leaves the registration behind and revokes their Season instead,
        proving nothing about the League axis. Historical Games are deliberately
        NOT rewritten, which is exactly why the Official's frozen derivation and
        the live Team-derived one diverge here."""
        self.api.setup.transfer_team_to_league(
            team_id, new_league_id, actor_id="test_seed")

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

    # -- frozen Game identity vs live Team membership ----------------------
    def test_team_transfer_moves_live_roles_but_never_the_officials_league(self):
        """The critical rule, falsified rather than assumed.

        After the Team moves Gold -> Silver in the SAME Program and Season:
        Coach / Player / Guardian follow it (their entitlement IS the Team), and
        the Official does NOT (their entitlement is per-Game, resolved against
        the Game's frozen LeagueSeason). Reading the Team for an Official would
        both grant Silver — a League no assignment covers — and revoke Gold,
        which is a scoped role enumerating outside its own scope.
        """
        fx = self._transfer_scenario()
        official = self._account("off_t", Role.OFFICIAL,
                                 {"official_id": fx["oid"]})
        coach = self._account("coach_t", Role.COACH, {"team_id": fx["team"]})
        player = self._account("player_t", Role.PLAYER,
                               {"team_id": fx["team"],
                                "player_id": fx["player"]})
        guardian = self._account("guard_t", Role.GUARDIAN, {})
        self.api.guardians.link_guardian(
            "user_guard_t", fx["player"], verified=True, actor_id="test_seed")

        live = (("coach", coach), ("player", player), ("guardian", guardian))
        # BEFORE: every scoped role sees exactly Gold — so the assertions after
        # the transfer are the ONLY thing that can distinguish the two rules.
        for name, opener in live + (("official", official),):
            self.assertEqual(self._offered_leagues(opener, fx["pid"]),
                             {fx["gold"]}, name)

        self._transfer_team(fx["team"], fx["silver"])

        # The Official is FROZEN: still Gold, never Silver.
        self.assertEqual(self._offered_leagues(official, fx["pid"]),
                         {fx["gold"]}, "official League followed the Team")
        before = self._saved_league("user_off_t")
        bindings = self._league_season_count()
        status, resp = self._req(
            "POST", "/api/context",
            {"program_id": fx["pid"], "season_id": fx["sid"],
             "league_id": fx["silver"]}, opener=official)
        self.assertEqual(status, 404, resp)
        self.assertEqual(resp["error"]["details"].get("reason"),
                         "league_not_accessible", resp)
        self.assertEqual(self._saved_league("user_off_t"), before)
        self.assertEqual(self._league_season_count(), bindings)
        # ...and the League the assignment really grants still commits.
        status, ok = self._req(
            "POST", "/api/context",
            {"program_id": fx["pid"], "season_id": fx["sid"],
             "league_id": fx["gold"]}, opener=official)
        self.assertEqual(status, 200, ok)
        self.assertEqual(ok["league_id"], fx["gold"], ok)

        # The Team-derived roles moved WITH the Team.
        for name, opener in live:
            self.assertEqual(self._offered_leagues(opener, fx["pid"]),
                             {fx["silver"]}, f"{name} did not follow the Team")
            status, got = self._req(
                "POST", "/api/context",
                {"program_id": fx["pid"], "season_id": fx["sid"],
                 "league_id": fx["silver"]}, opener=opener)
            self.assertEqual(status, 200, (name, got))
            self.assertEqual(got["league_id"], fx["silver"], (name, got))
            # Gold is now outside their scope, refused with the same reason.
            status, denied = self._req(
                "POST", "/api/context",
                {"program_id": fx["pid"], "season_id": fx["sid"],
                 "league_id": fx["gold"]}, opener=opener)
            self.assertEqual(status, 404, (name, denied))
            self.assertEqual(denied["error"]["details"].get("reason"),
                             "league_not_accessible", (name, denied))

    # -- idempotency, concurrency, restoration -----------------------------
    def test_league_bearing_post_is_idempotent_and_concurrency_safe(self):
        admin = self._login("admin")
        pid, sid, lid = self._program_season_league()
        body = {"program_id": pid, "season_id": sid, "league_id": lid}

        first = self._req("POST", "/api/context", body, opener=admin)
        second = self._req("POST", "/api/context", body, opener=admin)
        self.assertEqual((first[0], second[0]), (200, 200), (first, second))
        self.assertEqual(first[1]["league_id"], second[1]["league_id"])

        results = {}
        barrier = threading.Barrier(2)

        def post(key):
            op = self._login("admin")
            barrier.wait()
            results[key] = self._req("POST", "/api/context", body, opener=op)

        threads = [threading.Thread(target=post, args=(k,)) for k in ("a", "b")]
        for t in threads:
            t.start()
        for t in threads:
            t.join(20)
        # Both succeed (no primary-key race surfacing as a 500) and exactly one
        # row survives, still naming the League.
        self.assertEqual({r[0] for r in results.values()}, {200}, results)
        self.assertEqual(self._saved_league("user_admin"), lid)

    def test_revoked_authorization_restores_the_saved_league_unrewritten(self):
        """Authorization revocation — distinct from unbinding — must IGNORE the
        saved League, never rewrite it, so restoring authority restores the
        operator's own choice rather than silently losing it."""
        fx = self._transfer_scenario()
        official = self._account("off_r", Role.OFFICIAL,
                                 {"official_id": fx["oid"]})
        status, got = self._req(
            "POST", "/api/context",
            {"program_id": fx["pid"], "season_id": fx["sid"],
             "league_id": fx["gold"]}, opener=official)
        self.assertEqual(status, 200, got)

        assignments = self.store.assignments_for_official(fx["oid"])
        self.assertTrue(assignments)
        with self.store.transaction():
            for a in assignments:
                self.store.remove_official_assignment(a.id)

        _s, revoked = self._req("GET", "/api/context", opener=official)
        self.assertIsNone(revoked["league_id"], revoked)
        self.assertIsNone(revoked["league"], revoked)
        self.assertEqual(self._offered_leagues(official, fx["pid"]), set())
        self.assertEqual(self._saved_league("user_off_r"), fx["gold"])  # kept

        with self.store.transaction():
            for a in assignments:
                self.store.add_official_assignment(a)

        _s, restored = self._req("GET", "/api/context", opener=official)
        self.assertEqual(restored["league_id"], fx["gold"], restored)
        self.assertEqual(restored["league"]["id"], fx["gold"], restored)

    # -- races -------------------------------------------------------------
    def _mutate_after_league_check(self, mutate):
        """Land ``mutate`` at the REAL boundary — immediately after
        ``authorized_league_ids`` has validated the League, before the enclosing
        transaction writes. Returns a ``fired`` dict the caller MUST assert, so
        the barrier can never silently stop engaging (the exact failure mode
        that made a sibling race test vacuous earlier in this branch).

        A start-barrier alone is NOT a race: two threads released together can
        still run entirely sequentially and the assertions pass regardless.
        Injecting at the validate->write seam is what makes the window real and
        deterministic on every backend.
        """
        original = context_scope.authorized_league_ids
        fired = {"done": False}

        def wrapper(*a, **k):
            result = original(*a, **k)
            if not fired["done"]:
                fired["done"] = True
                mutate()
            return result

        context_scope.authorized_league_ids = wrapper
        self.addCleanup(
            setattr, context_scope, "authorized_league_ids", original)
        return fired

    def _assert_no_dangling_league(self, opener, label):
        """What the endpoint RENDERS must agree with the binding/authorization
        state — never a hybrid payload, never a dangling id, never enumeration
        of something the caller could not have selected."""
        _s, rendered = self._req("GET", "/api/context", opener=opener)
        self.assertNotIn("error", rendered, (label, rendered))
        if rendered["league_id"] is None:
            self.assertIsNone(rendered["league"], (label, rendered))
        else:
            self.assertIsNotNone(rendered["league"], (label, rendered))
            self.assertEqual(rendered["league"]["id"], rendered["league_id"],
                             (label, rendered))
            self.assertIsNotNone(
                self.store.league_season_for(rendered["league_id"],
                                             rendered["season_id"]),
                (label, "rendered a League with no binding", rendered))
        return rendered

    def _assert_race_outcome(self, status, resp, label):
        """Either the wholly-authorized pre-change result, or a STABLE
        post-change rejection. Never a 500, and never an integrity error
        escaping as one."""
        self.assertIn(status, (200, 404), (label, resp))
        if status == 200:
            self.assertNotIn("error", resp, (label, resp))
            if resp["league_id"] is None:
                self.assertIsNone(resp["league"], (label, resp))
            else:
                self.assertEqual(resp["league"]["id"], resp["league_id"],
                                 (label, resp))
        else:
            self.assertEqual(resp["error"]["details"].get("reason"),
                             "league_not_accessible", (label, resp))

    def test_unbind_at_the_validation_write_boundary(self):
        """The unbind lands AFTER the League validated, BEFORE the write."""
        admin = self._login("admin")
        pid, sid, lid = self._program_season_league()
        bindings = self._league_season_count()
        fired = self._mutate_after_league_check(lambda: self._unbind(lid, sid))

        status, resp = self._req(
            "POST", "/api/context",
            {"program_id": pid, "season_id": sid, "league_id": lid},
            opener=admin)

        self.assertTrue(fired["done"], "barrier never engaged — vacuous race")
        self._assert_race_outcome(status, resp, "unbind@boundary")
        # The context endpoint must never create or repair the binding it has
        # just failed to find.
        self.assertEqual(self._league_season_count(), bindings - 1)
        self.assertIsNone(self.store.league_season_for(lid, sid))
        self._assert_no_dangling_league(admin, "unbind@boundary")

    def test_authorization_revocation_at_the_validation_write_boundary(self):
        """Same seam, different invalidation: the caller's AUTHORITY is revoked
        between validation and write. A scoped role must never end up having
        enumerated or persisted a League it may no longer see."""
        fx = self._transfer_scenario()
        official = self._account("off_race", Role.OFFICIAL,
                                 {"official_id": fx["oid"]})
        assignments = self.store.assignments_for_official(fx["oid"])

        def revoke():
            with self.store.transaction():
                for a in assignments:
                    self.store.remove_official_assignment(a.id)

        bindings = self._league_season_count()
        fired = self._mutate_after_league_check(revoke)
        status, resp = self._req(
            "POST", "/api/context",
            {"program_id": fx["pid"], "season_id": fx["sid"],
             "league_id": fx["gold"]}, opener=official)

        self.assertTrue(fired["done"], "barrier never engaged — vacuous race")
        self._assert_race_outcome(status, resp, "revoke@boundary")
        self.assertEqual(self._league_season_count(), bindings)
        # Post-revocation the League is neither offered nor rendered.
        self.assertEqual(self._offered_leagues(official, fx["pid"]), set())
        rendered = self._assert_no_dangling_league(official, "revoke@boundary")
        self.assertIsNone(rendered["league_id"], rendered)

    def test_get_is_snapshot_consistent_at_its_league_boundary(self):
        """The READ path gets the same treatment: a mutation landing inside the
        resolve window must still yield a consistent payload, and must never
        rewrite the saved preference."""
        admin = self._login("admin")
        pid, sid, lid = self._program_season_league()
        self._req("POST", "/api/context",
                  {"program_id": pid, "season_id": sid, "league_id": lid},
                  opener=admin)
        fired = self._mutate_after_league_check(lambda: self._unbind(lid, sid))

        status, resp = self._req("GET", "/api/context", opener=admin)
        self.assertTrue(fired["done"], "barrier never engaged — vacuous race")
        self.assertEqual(status, 200, resp)
        if resp["league_id"] is not None:
            self.assertEqual(resp["league"]["id"], resp["league_id"], resp)
        self.assertEqual(self._saved_league("user_admin"), lid)  # not rewritten

    def test_revocation_at_the_get_league_boundary(self):
        """The 4th boundary leg: revocation against a READ. Completes
        {unbind, revocation} x {GET, POST} at the validate->render seam."""
        fx = self._transfer_scenario()
        official = self._account("off_getrace", Role.OFFICIAL,
                                 {"official_id": fx["oid"]})
        status, got = self._req(
            "POST", "/api/context",
            {"program_id": fx["pid"], "season_id": fx["sid"],
             "league_id": fx["gold"]}, opener=official)
        self.assertEqual(status, 200, got)
        assignments = self.store.assignments_for_official(fx["oid"])

        def revoke():
            with self.store.transaction():
                for a in assignments:
                    self.store.remove_official_assignment(a.id)

        fired = self._mutate_after_league_check(revoke)
        status, resp = self._req("GET", "/api/context", opener=official)

        self.assertTrue(fired["done"], "barrier never engaged — vacuous race")
        self.assertEqual(status, 200, resp)
        if resp["league_id"] is not None:
            self.assertEqual(resp["league"]["id"], resp["league_id"], resp)
        # A read never rewrites the preference, even when authority vanished
        # inside its own window.
        self.assertEqual(self._saved_league("user_off_getrace"), fx["gold"])

    # -- genuinely competing writer ----------------------------------------
    def _writer_store(self):
        """The store the COMPETING writer thread uses.

        PostgreSQL gets a second, INDEPENDENT connection — the only backend
        where another writer can actually commit while this request holds an
        open transaction. Memory/SQLite deliberately reuse the live store,
        because that IS their production shape: one process-wide store shared
        by every request thread, serialized by one lock. Handing them a second
        connection would fake a concurrency their engine does not have.
        """
        if self.is_postgres():
            store = SqlStore(self.database_url())
            self.addCleanup(store.close)
            return store
        return self.store

    def _competing_writer_race(self, do_request, mutate, label):
        """Pause the request at the League validate->write seam, then run a REAL
        second thread that mutates concurrently.

        Three separate facts are established, because any one of them alone can
        be satisfied vacuously:

        1. the writer thread actually STARTED, and actually REACHED the point
           immediately before its first locking store operation. Without this,
           "it didn't finish in N seconds" is indistinguishable from "it was
           never scheduled" — which is the vacuous-scheduling class this work
           exists to eliminate;
        2. the backend exhibited its real concurrency SHAPE — blocked on
           Memory/SQLite, committed-while-paused on PostgreSQL;
        3. the mutation actually took EFFECT (asserted per-leg by the caller
           against exact pre/post state, not inferred from the callback
           returning).

        ``mutate`` is called as ``mutate(store, attempting)`` and MUST set
        ``attempting`` immediately before its first locking operation.
        """
        paused, release = threading.Event(), threading.Event()
        started, attempting, completed = (threading.Event(), threading.Event(),
                                          threading.Event())
        fired = {"done": False}
        original = context_scope.authorized_league_ids

        def wrapper(*a, **k):
            result = original(*a, **k)
            if not fired["done"]:
                fired["done"] = True
                paused.set()
                release.wait(25)
            return result

        context_scope.authorized_league_ids = wrapper
        self.addCleanup(
            setattr, context_scope, "authorized_league_ids", original)

        out, writer_error = {}, {}

        def request():
            out["r"] = do_request()

        def writer():
            started.set()
            try:
                mutate(self._writer_store(), attempting)
            except Exception as exc:            # recorded, never swallowed
                writer_error["e"] = exc
            finally:
                completed.set()

        rt = threading.Thread(target=request, name=f"req-{label}")
        wt = threading.Thread(target=writer, name=f"writer-{label}")
        rt.start()
        try:
            self.assertTrue(paused.wait(25),
                            f"[{label}] request never reached the League seam")
            wt.start()
            # (1) The writer genuinely ran and genuinely got to the lock.
            self.assertTrue(started.wait(15),
                            f"[{label}] competing writer thread never started")
            self.assertTrue(
                attempting.wait(15),
                f"[{label}] competing writer never reached its first locking "
                f"store operation — a 'blocked' verdict here would be vacuous")
            # (2) Only NOW is a timeout meaningful: the writer is provably at
            # the lock, so not-completing means contention, not non-scheduling.
            completed_while_paused = completed.wait(2.0)
        finally:
            release.set()
            rt.join(30)
        wt.join(30)
        self.assertFalse(rt.is_alive(), f"[{label}] request thread hung")
        self.assertFalse(wt.is_alive(), f"[{label}] writer thread hung")
        self.assertNotIn("e", writer_error,
                         f"[{label}] competing writer raised: "
                         f"{writer_error.get('e')}")
        self.assertIn("r", out, f"[{label}] request produced no result")
        self.assertTrue(fired["done"], f"[{label}] seam never engaged")
        return completed_while_paused, out["r"]

    def _assert_competing_writer_shape(self, completed_while_paused, label):
        """The backend's real concurrency shape, asserted in both directions.
        Only meaningful because the helper already proved the writer reached
        the lock."""
        if self.is_postgres():
            self.assertTrue(
                completed_while_paused,
                f"[{label}] PostgreSQL: the independent connection did NOT "
                f"commit while the request was paused, so this leg never "
                f"exercised a concurrent commit")
        else:
            self.assertFalse(
                completed_while_paused,
                f"[{label}] Memory/SQLite: a writer that had reached the lock "
                f"completed while a transaction was open, breaking the "
                f"process-lock serialization this backend relies on")

    # -- writer callbacks: each signals `attempting`, none can silently no-op
    def _unbind_writer(self, league_season_id):
        def mutate(store, attempting):
            attempting.set()                    # about to take the lock
            with store.transaction():
                store.delete_league_season(league_season_id)
        return mutate

    def _revoke_writer(self, assignment_ids):
        def mutate(store, attempting):
            attempting.set()
            with store.transaction():
                for aid in assignment_ids:
                    store.remove_official_assignment(aid)
        return mutate

    # -- per-leg pre/post state proofs -------------------------------------
    def _unbind_race(self, do_request, lid, sid, label):
        """Run an unbind race and prove the unbind ACTUALLY happened."""
        ls = self.store.league_season_for(lid, sid)
        self.assertIsNotNone(ls, f"[{label}] fixture: binding missing up front")
        before = self._league_season_count()
        completed, result = self._competing_writer_race(
            do_request, self._unbind_writer(ls.id), label)
        self._assert_competing_writer_shape(completed, label)
        # (3) The mutation took effect — exactly once.
        self.assertIsNone(self.store.league_season_for(lid, sid),
                          f"[{label}] the binding was never actually removed")
        self.assertEqual(self._league_season_count(), before - 1,
                         f"[{label}] binding count did not change exactly once")
        return result

    def _revoke_race(self, do_request, official_id, label):
        """Run a revocation race and prove the revocation ACTUALLY happened."""
        ids = [a.id for a in self.store.assignments_for_official(official_id)]
        self.assertTrue(ids, f"[{label}] fixture: no assignments up front")
        bindings = self._league_season_count()
        completed, result = self._competing_writer_race(
            do_request, self._revoke_writer(ids), label)
        self._assert_competing_writer_shape(completed, label)
        remaining = {a.id for a in self.store.assignments_for_official(official_id)}
        self.assertEqual(remaining & set(ids), set(),
                         f"[{label}] assignments were never actually removed")
        # Revocation must not touch competition structure.
        self.assertEqual(self._league_season_count(), bindings,
                         f"[{label}] a revocation changed the binding table")
        return result

    def _assert_post_unbind_http(self, opener, label, saved_user=None,
                                 saved_expected=None):
        """Independent read-back through real HTTP after an unbind race."""
        rendered = self._assert_no_dangling_league(opener, label)
        self.assertIsNone(rendered["league_id"],
                          f"[{label}] a League rendered after its binding was "
                          f"removed: {rendered}")
        self.assertIsNone(rendered["league"], (label, rendered))
        if saved_user is not None:      # a read must never rewrite the choice
            self.assertEqual(self._saved_league(saved_user), saved_expected,
                             f"[{label}] the saved preference was rewritten")

    def _assert_post_revoke_http(self, opener, program_id, label,
                                 saved_user=None, saved_expected=None):
        """Independent read-back through real HTTP after a revocation race."""
        self.assertEqual(self._offered_leagues(opener, program_id), set(),
                         f"[{label}] a revoked League is still ENUMERATED")
        rendered = self._assert_no_dangling_league(opener, label)
        self.assertIsNone(rendered["league_id"],
                          f"[{label}] a revoked League still renders: {rendered}")
        if saved_user is not None:
            self.assertEqual(self._saved_league(saved_user), saved_expected,
                             f"[{label}] the saved preference was rewritten")

    def test_competing_unbind_versus_post(self):
        admin = self._login("admin")
        pid, sid, lid = self._program_season_league()
        poster = self._login("admin")
        result = self._unbind_race(
            lambda: self._req(
                "POST", "/api/context",
                {"program_id": pid, "season_id": sid, "league_id": lid},
                opener=poster),
            lid, sid, "unbind/POST")
        self._assert_race_outcome(result[0], result[1], "unbind/POST")
        self._assert_post_unbind_http(admin, "unbind/POST")

    def test_competing_unbind_versus_get(self):
        admin = self._login("admin")
        pid, sid, lid = self._program_season_league()
        self._req("POST", "/api/context",
                  {"program_id": pid, "season_id": sid, "league_id": lid},
                  opener=admin)
        reader = self._login("admin")
        result = self._unbind_race(
            lambda: self._req("GET", "/api/context", opener=reader),
            lid, sid, "unbind/GET")
        self.assertEqual(result[0], 200, result[1])
        if result[1]["league_id"] is not None:
            self.assertEqual(result[1]["league"]["id"], result[1]["league_id"])
        self._assert_post_unbind_http(admin, "unbind/GET",
                                      saved_user="user_admin",
                                      saved_expected=lid)

    def test_competing_revocation_versus_post(self):
        fx = self._transfer_scenario()
        official = self._account("off_cw_post", Role.OFFICIAL,
                                 {"official_id": fx["oid"]})
        result = self._revoke_race(
            lambda: self._req(
                "POST", "/api/context",
                {"program_id": fx["pid"], "season_id": fx["sid"],
                 "league_id": fx["gold"]}, opener=official),
            fx["oid"], "revoke/POST")
        self._assert_race_outcome(result[0], result[1], "revoke/POST")
        self._assert_post_revoke_http(official, fx["pid"], "revoke/POST")

    def test_competing_revocation_versus_get(self):
        fx = self._transfer_scenario()
        official = self._account("off_cw_get", Role.OFFICIAL,
                                 {"official_id": fx["oid"]})
        status, got = self._req(
            "POST", "/api/context",
            {"program_id": fx["pid"], "season_id": fx["sid"],
             "league_id": fx["gold"]}, opener=official)
        self.assertEqual(status, 200, got)
        result = self._revoke_race(
            lambda: self._req("GET", "/api/context", opener=official),
            fx["oid"], "revoke/GET")
        self.assertEqual(result[0], 200, result[1])
        if result[1]["league_id"] is not None:
            self.assertEqual(result[1]["league"]["id"], result[1]["league_id"])
        self._assert_post_revoke_http(official, fx["pid"], "revoke/GET",
                                      saved_user="user_off_cw_get",
                                      saved_expected=fx["gold"])


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

    def is_postgres(self):
        # Without this override the independent-connection race would skip on
        # EVERY backend and the suite would still report OK — the same silent
        # no-coverage failure this file's barriers are written to prevent.
        return True


if __name__ == "__main__":
    unittest.main()
