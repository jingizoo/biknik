"""Server-owned canonical (Program, Season, League) over authenticated HTTP (#364).

#360 wired the League axis through ``/api/context``. #364 is the OWNER RULING on
what a League selection *means* when the browser submits a Season and a League
that do not agree: the SERVER resolves the canonical tuple, and the response may
legitimately name a DIFFERENT Season than the request did.

``test_active_context_league.py`` proves that ruling at the service layer and
``test_context_league_http.py`` proves the #360 transport contract. This file
proves the part neither can: that the ruling survives the REAL request/response
boundary a browser actually talks to — a ``ThreadingHTTPServer`` running the real
``Handler`` against a real ``ApiService``, driven with real session cookies.

Why each group exists:

* **Legacy compatibility** — the wire contract gained a field, so the exact
  pre-#345 body (``program_id``/``season_id``, with NO ``league_id`` key at all)
  must still be answered byte-identically to an explicit ``league_id: null``,
  must select "no League", and — critically for #364 — must NOT canonicalize
  anything. Season resolution is a League-selection rule; a two-field caller
  never opts into it. A regression here breaks every client that predates the
  field, which is the whole point of making it additive.
* **Exact tuple** — the headline #364 case: acting in S2, selecting a League
  bound only to S1 returns S1 IN THE RESPONSE. Asserting the response alone
  would pass against a server that answered correctly and persisted nothing, so
  every success is re-read through ``GET /api/context`` and through the stored
  row, which is the state the next page load renders from.
* **Never disagree** — the response is not a suggestion: it is what the browser
  renders instead of what it sent (the ruling's own instruction to callers). So
  the response, a subsequent GET, and the persisted row are compared after every
  step of a realistic multi-step session, including a refused one.
* **Refusal** — one generic ``league_not_accessible``, indistinguishable across
  causes (an existence oracle would leak records the caller may not see), and
  GET is byte-identical before and after. A refusal that quietly moved the
  Season would be far worse than the blocker #364 fixed.
* **Authentication** — a per-user context still requires a real session; the
  identity-less demo fallbacks must fail at the boundary and write nothing.

Run on Memory, SQLite (a real temp FILE, since the server is threaded) and
PostgreSQL when ``TEST_DATABASE_URL`` is set — the canonical move is a
read-then-write inside one serializable snapshot, and that is exactly the kind of
guarantee a store's transaction semantics can differ on. PostgreSQL SKIPS
visibly when unconfigured rather than silently passing.
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
from hockey_scheduler.domain import SeasonStatus
from hockey_scheduler.web.server import STATE, Handler

PASSWORD = "demo"
ADMIN_USER = "user_admin"          # the demo seed's deterministic account id
ACTOR = "test_seed"
# The whole rendered key set, asserted as a SET so a silently widened payload
# fails here instead of at some client.
CONTEXT_KEYS = {"program_id", "season_id", "league_id", "read_only",
                "program", "season", "league"}
# The ONE reason every League refusal must carry (#364 owner ruling).
REFUSED = (404, "not_found", "league_not_accessible")


class CanonicalLeagueContextHttpContract:
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
        # Registered BEFORE the first thing that can fail: DATABASE_URL and
        # STATE are process-global, so a setUp that raised past this point (an
        # unreachable PostgreSQL, say) would leave every later test in the
        # process — including other modules — pointing at this test's store.
        # addCleanup runs even when setUp fails, so the pollution cannot escape.
        self.addCleanup(self._restore_environment)
        STATE.reset()              # demo world + the real persona accounts
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
        self.httpd.server_close()          # release the listening socket

    def _restore_environment(self):
        """Undo every process-global effect in reverse order. Rebuilding STATE
        on the RESTORED url both closes this test's store and leaves the shared
        module-level singleton usable for other modules; a failure there must
        not mask the test result nor skip the temp-file removal."""
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

    # -- transport ---------------------------------------------------------
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
    def _world(self):
        """One Program with TWO active Seasons where the League is bound to the
        OLDER one only — the exact shape #364 was filed about: the context bar
        offers Gold while the operator sits in S2, and before the ruling that
        selection could never be committed."""
        pid = self.api.create_program("Canon", "US", "UTC")["id"]
        s1 = self.api.create_season(pid, "S1")["id"]
        gold = self.api.create_league(s1, "Gold")["id"]     # bound to S1 only
        s2 = self.api.create_season(pid, "S2")["id"]        # active, unbound
        return pid, s1, s2, gold

    def _bindings(self, league_id):
        return {ls.season_id for ls in self.store.league_seasons_for_league(
            league_id)}

    def _saved(self, user_id=ADMIN_USER):
        """The persisted context as a plain triple, treating "no row at all" as
        None. A refused FIRST selection leaves no row, so reading attributes
        directly would raise instead of proving the zero-write."""
        row = self.store.get_active_context(user_id)
        if row is None:
            return None
        return (row.program_id, row.season_id, row.league_id)

    @staticmethod
    def _triple(payload):
        return (payload["program_id"], payload["season_id"],
                payload["league_id"])

    def _assert_coherent(self, payload, label):
        """Every id in the payload carries its object, and ``read_only`` agrees
        with the serialized Season status — so "the response" is one internally
        consistent thing to compare against, not three loose fields."""
        self.assertEqual(set(payload), CONTEXT_KEYS, (label, payload))
        for axis in ("program", "season", "league"):
            if payload[f"{axis}_id"] is None:
                self.assertIsNone(payload[axis], (label, payload))
            else:
                self.assertIsNotNone(payload[axis], (label, payload))
                self.assertEqual(payload[axis]["id"], payload[f"{axis}_id"],
                                 (label, payload))
        expected_read_only = (
            payload["season"] is not None
            and payload["season"]["status"] == SeasonStatus.ARCHIVED.value)
        self.assertEqual(payload["read_only"], expected_read_only,
                         (label, payload))

    def _assert_agrees(self, opener, response, label, user_id=ADMIN_USER):
        """The response, a FRESH GET, and the stored row all name the same
        tuple. The GET is what the next page load renders; the row is what
        survives a restart. Asserting only the response would pass against a
        server that answered correctly and persisted nothing."""
        self._assert_coherent(response, f"{label}/response")
        status, got = self._req("GET", "/api/context", opener=opener)
        self.assertEqual(status, 200, (label, got))
        self._assert_coherent(got, f"{label}/get")
        self.assertEqual(self._triple(got), self._triple(response),
                         f"{label}: GET disagrees with the POST response")
        self.assertEqual(self._saved(user_id), self._triple(response),
                         f"{label}: the stored row disagrees with the response")

    # -- legacy compatibility ---------------------------------------------
    def test_legacy_two_field_body_selects_no_league_and_never_canonicalizes(self):
        """A body with NO ``league_id`` key behaves exactly as it did before the
        field existed: League null, and the requested Season kept verbatim.

        The Season here is deliberately S2, which the Program's only League is
        NOT bound to. If canonicalization ever leaked out of the League path
        into the two-axis path, a legacy caller would be silently moved to S1 —
        so this fixture is what makes the "unchanged" claim falsifiable rather
        than vacuous."""
        admin = self._login("admin")
        pid, s1, s2, gold = self._world()

        status, resp = self._req("POST", "/api/context",
                                 {"program_id": pid, "season_id": s2},
                                 opener=admin)
        self.assertEqual(status, 200, resp)
        self.assertEqual(self._triple(resp), (pid, s2, None), resp)
        self._assert_agrees(admin, resp, "legacy-two-field")
        # The two new keys are always PRESENT (never absent), so a client can
        # never have to distinguish "missing" from "null".
        self.assertIsNone(resp["league"], resp)

    def test_legacy_body_is_byte_identical_to_an_explicit_null_league(self):
        """Omitting the key and sending ``league_id: null`` must render the SAME
        payload — the additive field cannot introduce a second dialect — and
        both must CLEAR a League that a canonical #364 move had just saved."""
        admin = self._login("admin")
        pid, s1, s2, gold = self._world()
        # Establish a real saved League via the canonical move, so "clears"
        # is proven against a populated row rather than an empty one.
        status, moved = self._req(
            "POST", "/api/context",
            {"program_id": pid, "season_id": s2, "league_id": gold},
            opener=admin)
        self.assertEqual(status, 200, moved)
        self.assertEqual(self._saved(), (pid, s1, gold), moved)

        s_legacy, legacy = self._req("POST", "/api/context",
                                     {"program_id": pid, "season_id": s1},
                                     opener=admin)
        self.assertEqual(s_legacy, 200, legacy)
        self.assertEqual(self._saved(), (pid, s1, None),
                         "a legacy body must clear the saved League")
        s_null, explicit = self._req(
            "POST", "/api/context",
            {"program_id": pid, "season_id": s1, "league_id": None},
            opener=admin)
        self.assertEqual(s_null, 200, explicit)
        self.assertEqual(explicit, legacy,
                         "omitting league_id must render exactly as null")
        self._assert_agrees(admin, explicit, "explicit-null")

    # -- the #364 headline: the exact canonical tuple ----------------------
    def test_selecting_a_league_bound_to_another_season_moves_the_response(self):
        """Acting in S2, selecting a League bound only to S1 answers with S1.

        This is the ruling's core reversal: the returned Season DIFFERS from the
        requested one and that is correct. Proven end to end — the POST response
        carries S1, a fresh GET carries S1, and the persisted row carries the
        whole canonical tuple — because a browser that rendered what it SENT
        would show a Season the server did not select."""
        admin = self._login("admin")
        pid, s1, s2, gold = self._world()
        # Begin genuinely ACTIVE in S2, through the boundary, so the move is a
        # transition from a real prior state and not merely a first write.
        _s, start = self._req("POST", "/api/context",
                              {"program_id": pid, "season_id": s2},
                              opener=admin)
        self.assertEqual(self._triple(start), (pid, s2, None), start)
        bindings_before = self._bindings(gold)

        status, resp = self._req(
            "POST", "/api/context",
            {"program_id": pid, "season_id": s2, "league_id": gold},
            opener=admin)
        self.assertEqual(status, 200, resp)
        self.assertEqual(resp["season_id"], s1,
                         "the response must carry the canonical Season, not "
                         "the requested one")
        self.assertEqual(self._triple(resp), (pid, s1, gold), resp)
        self.assertEqual(resp["season"]["id"], s1, resp)   # object, not just id
        self.assertFalse(resp["read_only"], resp)          # S1 is active
        self._assert_agrees(admin, resp, "canonical-move")
        # Resolution reads an EXISTING binding and invents none: the League is
        # still bound to S1 only, and nothing was manufactured for S2.
        self.assertEqual(self._bindings(gold), bindings_before, "binding created")
        self.assertIsNone(self.store.league_season_for(gold, s2))

    def test_a_league_bound_to_the_current_season_keeps_that_season(self):
        """Rule 1 over HTTP: when the current Season genuinely binds, the server
        must NOT "helpfully" move a context that was already canonical — even
        though another valid binding exists to move to."""
        admin = self._login("admin")
        pid, s1, s2, gold = self._world()
        self.api.setup.create_league_season(gold, s2, actor_id=ACTOR)

        status, resp = self._req(
            "POST", "/api/context",
            {"program_id": pid, "season_id": s2, "league_id": gold},
            opener=admin)
        self.assertEqual(status, 200, resp)
        self.assertEqual(self._triple(resp), (pid, s2, gold),
                         "a bound current Season must be kept")
        self._assert_agrees(admin, resp, "keep-current")

    def test_program_only_with_a_league_canonicalizes_over_http(self):
        """``season_id: null`` + a League is canonicalized like any other League
        selection (#364 re-review), through the real authenticated boundary.

        **Reversed from an earlier revision** which asserted the null Season was
        left as-is. That codified the defect: it let the endpoint persist and
        render an active League with no `LeagueSeason` behind it. Rule 1 cannot
        match without a current Season, so rule 2 takes the unique valid
        binding."""
        admin = self._login("admin")
        pid, s1, s2, gold = self._world()

        status, resp = self._req(
            "POST", "/api/context",
            {"program_id": pid, "season_id": None, "league_id": gold},
            opener=admin)
        self.assertEqual(status, 200, resp)
        self.assertEqual(self._triple(resp), (pid, s1, gold), resp)
        self._assert_agrees(admin, resp, "program-only-league")

    def test_program_only_with_an_ambiguous_league_is_refused_over_http(self):
        """Rule 3 from a null Season: several valid bindings and no current
        Season to prefer must refuse generically and mutate nothing."""
        admin = self._login("admin")
        pid, s1, s2, gold = self._world()
        # Bind Gold to BOTH seasons, so a null Season has no unique candidate.
        self.api.setup.create_league_season(gold, s2)
        ok, base = self._req(
            "POST", "/api/context",
            {"program_id": pid, "season_id": s1, "league_id": gold}, opener=admin)
        self.assertEqual(ok, 200, base)
        before = self.store.get_active_context(ADMIN_USER)

        status, resp = self._req(
            "POST", "/api/context",
            {"program_id": pid, "season_id": None, "league_id": gold},
            opener=admin)
        self.assertEqual(status, 404, resp)
        self.assertEqual(resp["error"]["details"].get("reason"),
                         "league_not_accessible", resp)
        after = self.store.get_active_context(ADMIN_USER)
        # updated_at included deliberately: a compensating "rewrite the same
        # values with a fresh clock" is invisible to the id triple alone.
        self.assertEqual(
            (after.program_id, after.season_id, after.league_id, after.updated_at),
            (before.program_id, before.season_id, before.league_id,
             before.updated_at),
            "an ambiguous Program-only League selection mutated the saved row")

    def test_an_explicitly_chosen_archived_season_and_league_still_commits(self):
        """Archived is never AUTO-entered, but an explicit archived pair remains
        selectable read-only history — the response and GET both say so."""
        admin = self._login("admin")
        pid, s1, s2, gold = self._world()
        self.api.setup.archive_season(s1, actor_id=ACTOR)

        status, resp = self._req(
            "POST", "/api/context",
            {"program_id": pid, "season_id": s1, "league_id": gold},
            opener=admin)
        self.assertEqual(status, 200, resp)
        self.assertEqual(self._triple(resp), (pid, s1, gold), resp)
        self.assertTrue(resp["read_only"], resp)
        self._assert_agrees(admin, resp, "archived-explicit")

    # -- response and persisted state can never disagree -------------------
    def test_a_whole_session_never_lets_the_response_and_the_state_disagree(self):
        """Drive a realistic multi-step session — legacy, canonical move, keep,
        refusal, Program-only — and after EVERY step require response == GET ==
        stored row. A single-request test cannot catch a server whose canonical
        move is right in the response but persisted from the request, because
        the very next read is where that shows up."""
        admin = self._login("admin")
        pid, s1, s2, gold = self._world()
        s3 = self.api.create_season(pid, "S3")["id"]
        silver = self.api.create_league(s1, "Silver")["id"]
        self.api.setup.create_league_season(silver, s2, actor_id=ACTOR)

        steps = [
            ("legacy", {"program_id": pid, "season_id": s2}, (pid, s2, None)),
            ("move-to-s1", {"program_id": pid, "season_id": s2,
                            "league_id": gold}, (pid, s1, gold)),
            ("keep-s1", {"program_id": pid, "season_id": s1,
                         "league_id": gold}, (pid, s1, gold)),
            ("ambiguous-refused", {"program_id": pid, "season_id": s3,
                                   "league_id": silver}, None),
            # #364 re-review: a null Season is canonicalized like any other
            # League selection. `silver` binds BOTH s1 and s2, so from a null
            # Season it is genuinely ambiguous and must refuse; `gold` binds
            # only s1, so it resolves to that unique binding. Covering both
            # here is what proves the null-Season path runs the SAME rules
            # rather than a permissive shortcut.
            ("program-only-ambiguous", {"program_id": pid, "season_id": None,
                                        "league_id": silver}, None),
            ("program-only-unique", {"program_id": pid, "season_id": None,
                                     "league_id": gold}, (pid, s1, gold)),
            ("clear-all", {"program_id": pid, "season_id": s2}, (pid, s2, None)),
        ]
        last_good = None
        for label, body, expected in steps:
            status, resp = self._req("POST", "/api/context", body, opener=admin)
            if expected is None:               # refusal: state must not move
                self.assertEqual(status, 404, (label, resp))
                self.assertEqual(self._saved(), last_good,
                                 f"{label}: a refusal changed the saved row")
                _s, got = self._req("GET", "/api/context", opener=admin)
                self.assertEqual(self._triple(got), last_good, (label, got))
                continue
            self.assertEqual(status, 200, (label, resp))
            self.assertEqual(self._triple(resp), expected, (label, resp))
            self._assert_agrees(admin, resp, label)
            last_good = expected

    # -- refusal: one generic reason, zero movement ------------------------
    def test_every_refusal_is_generic_and_leaves_the_context_untouched(self):
        """Ambiguous / archived-only / unbound-and-nonexistent / cross-Program
        all answer IDENTICALLY, and GET is byte-identical before and after.

        Sameness is the security property: a distinguishable refusal would turn
        the context bar into an existence oracle for records the caller may not
        see. Zero movement is the #364 property: a refusal that moved the Season
        anyway would be worse than the blocker the ruling fixed."""
        admin = self._login("admin")
        pid, s1, s2, gold = self._world()
        s3 = self.api.create_season(pid, "S3")["id"]
        # Ambiguous: bound to S1 and S2, selected from S3 — the ruling forbids
        # tie-breaking, because a silent pick moves the operator into a Season
        # they never chose and never saw.
        silver = self.api.create_league(s1, "Silver")["id"]
        self.api.setup.create_league_season(silver, s2, actor_id=ACTOR)
        # Archived-only: its single binding is read-only history, never entered.
        old = self.api.create_season(pid, "S-old")["id"]
        bronze = self.api.create_league(old, "Bronze")["id"]
        self.api.setup.archive_season(old, actor_id=ACTOR)
        # Cross-Program: a perfectly valid League somewhere the caller isn't.
        other_p = self.api.create_program("Other", "US", "UTC")["id"]
        other_s = self.api.create_season(other_p, "OS")["id"]
        other_l = self.api.create_league(other_s, "Other Gold")["id"]

        # A known-good prior context, so "unchanged" is proven against a real
        # saved row rather than against absence.
        _s, baseline = self._req(
            "POST", "/api/context",
            {"program_id": pid, "season_id": s2, "league_id": gold},
            opener=admin)
        self.assertEqual(self._triple(baseline), (pid, s1, gold), baseline)
        _s, before_get = self._req("GET", "/api/context", opener=admin)
        before_row = self._saved()
        bindings_before = {lg: self._bindings(lg)
                           for lg in (gold, silver, bronze, other_l)}

        attempts = {
            "ambiguous": {"program_id": pid, "season_id": s3,
                          "league_id": silver},
            "archived_only": {"program_id": pid, "season_id": s2,
                              "league_id": bronze},
            "cross_program": {"program_id": pid, "season_id": s2,
                              "league_id": other_l},
            "nonexistent": {"program_id": pid, "season_id": s2,
                            "league_id": "league_does_not_exist"},
        }
        seen = set()
        for label, body in attempts.items():
            status, resp = self._req("POST", "/api/context", body, opener=admin)
            err = resp.get("error", {})
            seen.add((status, err.get("code"),
                      err.get("details", {}).get("reason"), err.get("message")))
            self.assertEqual(
                (status, err.get("code"),
                 err.get("details", {}).get("reason")), REFUSED, (label, resp))
            self.assertEqual(self._saved(), before_row,
                             f"{label} moved the saved context")
            _s, after_get = self._req("GET", "/api/context", opener=admin)
            self.assertEqual(after_get, before_get,
                             f"{label} changed what GET renders")
        self.assertEqual(len(seen), 1,
                         f"refusals are distinguishable: {seen}")
        # And no refusal manufactured the binding that would have made it valid.
        self.assertEqual({lg: self._bindings(lg)
                          for lg in (gold, silver, bronze, other_l)},
                         bindings_before, "a refusal created a LeagueSeason")

    # -- authentication ----------------------------------------------------
    def test_unauthenticated_post_is_rejected_and_persists_nothing(self):
        """A per-user context needs a real SESSION. No cookie, the demo
        ``X-Demo-Role`` header, and ``DEMO_HEADERLESS_ADMIN=1`` are all 401 —
        and none of them may create OR overwrite a row, so the check runs once
        against an empty context table and once against a saved canonical
        tuple."""
        pid, s1, s2, gold = self._world()
        body = {"program_id": pid, "season_id": s2, "league_id": gold}

        def _all_identity_less_are_rejected(label):
            self.assertEqual(self._req("GET", "/api/context")[0], 401, label)
            self.assertEqual(self._req("POST", "/api/context", body)[0], 401,
                             label)
            self.assertEqual(
                self._req("POST", "/api/context", body,
                          role="league_admin")[0], 401, label)
            os.environ["DEMO_HEADERLESS_ADMIN"] = "1"
            try:
                self.assertEqual(
                    self._req("POST", "/api/context", body)[0], 401, label)
            finally:
                os.environ.pop("DEMO_HEADERLESS_ADMIN", None)

        _all_identity_less_are_rejected("no-row")
        self.assertIsNone(self._saved(), "an unauthenticated POST wrote a row")

        admin = self._login("admin")
        _s, saved = self._req(
            "POST", "/api/context",
            {"program_id": pid, "season_id": s1, "league_id": gold},
            opener=admin)
        self.assertEqual(self._triple(saved), (pid, s1, gold), saved)
        before = self._saved()
        # The same bodies again, now with something to (fail to) overwrite —
        # proving zero CHANGE, not merely zero creation.
        _all_identity_less_are_rejected("with-row")
        self.assertEqual(self._saved(), before,
                         "an unauthenticated POST overwrote a saved context")
        # ...and the signed-in session still sees exactly what it committed.
        _s, got = self._req("GET", "/api/context", opener=admin)
        self.assertEqual(self._triple(got), (pid, s1, gold), got)


class MemoryCanonicalLeagueContextHttpTest(CanonicalLeagueContextHttpContract,
                                           unittest.TestCase):
    def database_url(self):
        return None                     # the in-memory demo store


class SqliteCanonicalLeagueContextHttpTest(CanonicalLeagueContextHttpContract,
                                           unittest.TestCase):
    def database_url(self):
        # A real file, not ":memory:" — the server is threaded, and a
        # file-backed database is what an operator actually runs against.
        fd, path = tempfile.mkstemp(suffix=".sqlite")
        os.close(fd)
        self._tmp_path = path
        return path


@unittest.skipUnless(os.environ.get("TEST_DATABASE_URL"),
                     "PostgreSQL not configured (TEST_DATABASE_URL)")
class PostgresCanonicalLeagueContextHttpTest(CanonicalLeagueContextHttpContract,
                                             unittest.TestCase):
    def database_url(self):
        return os.environ["TEST_DATABASE_URL"]


if __name__ == "__main__":
    unittest.main()
