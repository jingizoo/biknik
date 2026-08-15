"""A context switch cannot commit while an exact-Season scoped read is still
INSIDE THE SERVER (#159 follow-up to #412's client-side barrier).

THE DEFECT, AS CI FOUND IT. Run 31504917446, browser shard 1, PHONE leg:
``GET /api/v2/setup/seasons/season_3/venue-candidates`` answered a real 404 and
raised a console error. The app's abort ledger records that exact read as
``dispatched=true`` -- it WAS enrolled in the client barrier, so enrolment was
never the problem.

WHY THE SHIPPED BARRIER CANNOT CLOSE IT. ``awaitContextScopedReadSettlement()``
(``web/static/app.js``) awaits ``entry.settled`` -- the CLIENT's fetch promise.
``AbortController.abort()`` settles that promise IMMEDIATELY, while the SERVER
goes on running the request it already accepted. The switch's POST then commits
the NEW tuple, and the still-running old read evaluates its Season id against
the NEW exact-Season selection: ``season_id != active_season.id`` -> generic
404. An AbortController cannot un-send a request the server already holds, so
no purely client-side barrier can know when a handler EXITS. The ordering has
to be enforced server-side, which is what ``services/context_gate.py`` does.

WHAT THIS FILE PROVES, and how it is falsifiable rather than narrated. Every
case parks a REAL request inside the REAL ``Handler`` on a REAL
``ThreadingHTTPServer`` with REAL session cookies, and then observes PERSISTED
STATE -- ``store.get_active_context(user_id)`` -- while it is parked. No
assertion here is satisfied by a sleep: sleeps only bound waits, and the
load-bearing assertions are (a) the persisted tuple is still the OLD one while
the read is held, (b) the read answers 200 for the Season it named, and (c) the
switch's commit timestamp is strictly after the read handler's exit timestamp.

ONE CASE PER LISTED ROUTE, because the route table is a claim about coverage
and is tested as one. ``CONTEXT_SCOPED_READ_ROUTES`` states its own admission
criterion, and applying that criterion to the code rather than to the CI
incident finds four routes, not the two the incident happened to hit: the two
venue reads, ``GET /api/scheduler/scenarios/<id>`` (whose ceiling is
``_scenario_in_active_tuple`` and whose mismatch is the generic
``_scenario_not_found``), and ``GET /api/standings/<division_id>`` (whose
ceiling is ``_division_matches_active_context`` and whose mismatch is the
generic EMPTY standings shape — a wrong answer that looks like a real one).
A FIFTH joined in #202: ``GET /api/standings/league-season/<l>/<s>``, whose
mismatch is the generic ``not_found``. It did NOT qualify when this file was
written and was correctly left out then — the route resolved no tuple at all,
because it passed no role/scope and answered ANONYMOUS callers. #202 gave it its
per-Division sibling's contract, and acquiring the ceiling is what earned it a
row here. Each has a held-read case AND its own unweakened-ceiling control.

TWO DISTINCT OPERATORS, because nothing else in this file can see cross-user
coupling. Every other case opens all of its sessions for ONE username, so a gate
that made unrelated operators wait for each other would pass all of them
byte-identically. ``test_one_operators_switch_is_not_stalled_by_anothers_
scoped_read`` drives the interleaving where the bound the gate promises
("cross-user coupling is bounded by one session lookup") is load-bearing, and
asserts the timeout counter too — because a wait that ends at its deadline with
the predicate already true reports SUCCESS, so the failure is otherwise
indistinguishable from a healthy gate.

THE GATE'S OWN FAILURE MODES have their own class, ``ContextGateInternalsTest``,
driven directly with no store and no HTTP: a bounded wait that raises, an expiry
notice whose stderr write raises, and one whose stderr write BLOCKS while
another thread tries to ``arrive()``. Through a request those are reachable only
by luck; the invariant they defend is that the gate's handling of a degraded
wait must not itself degrade the gate.

THE PARK POINTS ARE THE TWO PHASES OF THE GATE, and each has its own case:

  * PHASE B (bound) -- parked inside ``ApiService.get_venue_grant_candidates``
    /``list_season_venue_access``, i.e. after the handler knows whose request
    this is. ``test_a_switch_cannot_commit_while_a_scoped_read_is_inside_the_
    server`` and its venue-access twin.
  * PHASE A (unbound, pre-identity) -- parked inside ``SESSIONS.resolve``,
    i.e. inside ``_resolve_role()``, before any identity exists. This is a
    store read that takes the store's ``_lock`` and is the most plausible place
    the CI read was actually parked. A gate that only knew about identified
    readers would let the switch straight past it.
    ``test_a_read_parked_before_identity_still_orders_the_switch``.

THE CEILING IS NOT WEAKENED, and that is a control here rather than a claim:
``test_control_a_non_selected_season_read_still_gets_the_generic_404`` issues an
UNRACED read for a sibling Season of the active Program and requires the same
generic 404 with no Season/Venue names in the body. If a future "fix" made the
gate admit reads by widening the comparison, that control goes red.

THE FOUR CLASSES THE OWNER REQUIRED BY NAME each have their own test, and each
asserts on the gate's own counters (``ContextSwitchGate.stats()``) so "nothing
was left behind" is measured, not asserted in prose:

  1. ``test_concurrent_switches_for_one_user_stay_context_coherent``
  2. ``test_a_switch_that_fails_or_is_cancelled_while_waiting_leaks_nothing``
  3. ``test_repeated_switching_accumulates_no_waiters``
  4. ``test_a_waiter_cannot_block_forever_on_a_read_that_never_returns``

Every class runs on Memory, SQLite and PostgreSQL (the last only when
``TEST_DATABASE_URL`` is set), because the ordering is enforced in-process
above the store and must therefore behave identically on all three -- which is
also the reason it could NOT have been built on the #386 PostgreSQL advisory
mutex, a documented no-op on the other two.
"""

import json
import os
import socket
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from http.cookiejar import CookieJar
from http.server import ThreadingHTTPServer

from helpers import BACKEND  # noqa: F401  (ensures sys.path is set up)

from hockey_scheduler.domain import Role
from hockey_scheduler.domain.setup_models import ScheduleScenario
from hockey_scheduler.services.context_gate import ContextSwitchGate

TZ = "America/Toronto"

# How long a case is willing to wait for a genuinely-expected event before it
# declares the server wedged. Generous: it bounds waits, it never times one.
PATIENCE = 20.0

# The window a case gives a switch to commit while a read is held. On the
# broken build the POST commits in single-digit milliseconds, so this is three
# orders of magnitude more than the defect needs to show itself.
COMMIT_WINDOW = 1.0


def _wait(predicate, timeout=PATIENCE, interval=0.005):
    """Poll ``predicate`` until true or ``timeout``. Returns the final value —
    callers assert on it, so a timeout fails the assertion rather than passing
    silently."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    return predicate()


class _Park:
    """A one-shot, thread-safe park: the victim announces it has arrived and
    then blocks until the test releases it (or the bound expires)."""

    def __init__(self):
        self.arrived = threading.Event()
        self.release = threading.Event()
        self.released_at = None

    def hold(self, timeout=PATIENCE):
        self.arrived.set()
        self.release.wait(timeout)
        self.released_at = time.monotonic()

    def let_go(self):
        self.release.set()


class ContextGateFixtureBase:
    """Shared FIXTURE ONLY — a real ``ThreadingHTTPServer``, real sessions, the
    Program/Season fixtures, the park seams and the gate assertions. It carries
    NO test methods, so a sibling suite can reuse the machinery without
    silently re-running (and re-timing) every case below on its own classes;
    ``tests/test_context_read_cancel_handoff.py`` does exactly that.

    Subclasses set ``STORE_URL`` (None => InMemoryStore)."""

    STORE_URL = None

    @classmethod
    def setUpClass(cls):
        srv = __import__("hockey_scheduler.web.server", fromlist=["x"])
        cls.srv = srv
        cls._prior_database_url = os.environ.get("DATABASE_URL")
        if cls.STORE_URL:
            os.environ["DATABASE_URL"] = cls.STORE_URL
        else:
            os.environ.pop("DATABASE_URL", None)
        srv.STATE.reset(seed=False)
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), srv.Handler)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.thread.join(timeout=5)
        cls.httpd.server_close()
        if cls._prior_database_url is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = cls._prior_database_url
        cls.srv.STATE.reset(seed=False)

    def setUp(self):
        self.api = self.srv.STATE.api
        self._restores = []
        self.addCleanup(self._restore_all)

    def _restore_all(self):
        for undo in reversed(self._restores):
            undo()
        self._restores = []

    # -- seam installation --------------------------------------------------
    def _wrap(self, obj, name, wrapper_factory):
        """Shadow a BOUND method on the LIVE instance with a wrapper built from
        the original. Instance attributes win over class attributes, so no
        production module is patched and the undo is a plain ``delattr``.

        round-N+1: ALSO shadows the underlying CLASS's method, for a call
        made through a DIFFERENT instance of the SAME class — specifically
        ``web/server.py``'s ``_read_under_context_gate_sqlite``, which runs a
        scoped read's service call against its OWN, fully independent
        ``ApiService`` (bound to a fresh, independent ``SqlStore``, never
        ``STATE.api``) for FILE-BACKED SQLite. Before this, every SQLite case
        in this file (and in ``test_context_read_cancel_handoff.py``/
        ``test_context_epoch_lifecycle.py``, which reuse this exact method)
        parked or spied on ``self.api.<method>`` and then waited forever: the
        call it needed to see was happening on a DIFFERENT, freshly-
        constructed instance the instance-level shadow above cannot reach.

        THIS IS PURELY ADDITIVE for every EXISTING caller. Python resolves an
        INSTANCE attribute before a CLASS one, so the instance-level shadow
        above is still what actually runs for every call through ``obj``
        itself — the class-level dispatch below is only ever reached for a
        call through some OTHER instance of ``type(obj)``.

        ONE SET OF CLOSURE STATE backs BOTH paths, not two independent ones:
        every wrapper factory in this codebase's test suite (a spy's
        accumulating ``calls`` list, a park's ``_Park``/``exited`` dict) is
        already written to hold its OWN mutable state OUTSIDE
        ``wrapper_factory``'s own body — ``wrapper_factory`` itself only ever
        closes over ``original`` — so calling it a SECOND time below (once
        per newly-seen instance, with THAT instance's own true original bound
        method) still reads and writes the exact same outer `calls`/`park`/
        `exited`, never a disconnected copy. A spy therefore sees every call
        regardless of which instance received it, and a park catches
        whichever instance's call happens to arrive.
        """
        original = getattr(obj, name)
        setattr(obj, name, wrapper_factory(original))
        cls = type(obj)
        class_original = getattr(cls, name)

        def class_dispatch(instance, *a, **kw):
            if instance is obj:
                # Unreachable in practice — attribute lookup never falls
                # through to the class for `obj` once the instance shadow
                # above exists — kept so this stays CORRECT even if that
                # invariant is ever violated, rather than silently calling
                # `original` twice for the one call the instance shadow
                # already serves.
                return getattr(obj, name)(*a, **kw)
            bound = class_original.__get__(instance, cls)
            return wrapper_factory(bound)(*a, **kw)

        setattr(cls, name, class_dispatch)

        def undo():
            if name in vars(obj):
                delattr(obj, name)
            setattr(cls, name, class_original)

        self._restores.append(undo)
        return original

    # -- HTTP ---------------------------------------------------------------
    def _client(self):
        return urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(CookieJar()))

    def _req(self, opener, method, path, body=None, timeout=PATIENCE,
             headers=None):
        url = f"http://127.0.0.1:{self.port}{path}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        # ADDITIVE and defaulted (#159 late-arrival follow-up): a scoped read
        # echoes the context epoch it was rendered under in a REQUEST HEADER, so
        # the sibling suite in tests/test_context_read_cancel_handoff.py needs
        # to set one. Every existing call site passes none and is byte-identical
        # — and "passes none" is itself a case there, since an absent epoch must
        # behave exactly as it did before the epoch existed.
        for name, value in (headers or {}).items():
            req.add_header(name, value)
        try:
            with opener.open(req, timeout=timeout) as r:
                raw = r.read()
                return r.status, raw.decode(), json.loads(raw or b"{}")
        except urllib.error.HTTPError as e:
            raw = e.read()
            return e.code, raw.decode(), json.loads(raw or b"{}")

    def _login(self, username, password="demo"):
        c = self._client()
        status, raw, _ = self._req(c, "POST", "/api/auth/login",
                                   {"username": username, "password": password})
        self.assertEqual(status, 200, raw)
        return c

    def _session_cookie(self, username, password="demo"):
        """A raw ``Cookie:`` header for a real session — needed by
        ``_abandoned_post``, which speaks HTTP on a bare socket precisely so it
        can hang up mid-request in a way urllib will not do."""
        jar = CookieJar()
        opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(jar))
        status, raw, _ = self._req(opener, "POST", "/api/auth/login",
                                   {"username": username, "password": password})
        self.assertEqual(status, 200, raw)
        pairs = "; ".join(f"{c.name}={c.value}" for c in jar)
        self.assertTrue(pairs, "login produced no session cookie")
        return pairs

    def _abandoned_post(self, path, cookie, body):
        """Send a complete, VALID POST and then close the socket without
        reading the response — a client that navigated away or lost its
        network. The server accepts and runs the request; the response write
        lands on a dead socket. Returns once the bytes are on the wire."""
        payload = json.dumps(body).encode()
        sock = socket.create_connection(("127.0.0.1", self.port), timeout=5)
        request = (
            f"POST {path} HTTP/1.1\r\n"
            f"Host: 127.0.0.1:{self.port}\r\n"
            f"Cookie: {cookie}\r\n"
            f"Content-Type: application/json\r\n"
            f"Content-Length: {len(payload)}\r\n"
            f"Connection: close\r\n\r\n").encode() + payload
        sock.sendall(request)
        sock.close()

    def _operator(self, tag):
        """A brand-new League Admin with NO persisted selection, so no earlier
        case's context can be inherited and make a case vacuous."""
        username = f"{tag}_{uuid.uuid4().hex[:10]}"
        acct = self.api.accounts.create_account(username, "demo", Role.LEAGUE_ADMIN)
        return username, acct.id

    def _select(self, c, program_id, season_id, expect=200):
        status, raw, body = self._req(c, "POST", "/api/context",
                                      {"program_id": program_id,
                                       "season_id": season_id})
        self.assertEqual(status, expect, raw)
        return body

    # -- fixture ------------------------------------------------------------
    def _program_with_two_seasons(self, tag):
        """One Program, two Seasons (S1, S2), one Venue granted to S1. Both
        Seasons are legitimately selectable by this operator — only the EXACT
        selected-Season rule distinguishes them, which is the rule under test."""
        svc = self.api.setup
        program = svc.create_program(f"{tag} Program", timezone_name=TZ)
        s1 = svc.create_season(program.id, f"{tag} Season One")
        s2 = svc.create_season(program.id, f"{tag} Season Two")
        s3 = svc.create_season(program.id, f"{tag} Season Three")
        venue = svc.create_venue(f"{tag} Venue", league_id=program.id)
        svc.grant_season_venue_access(s1.id, venue.id)
        return {"tag": tag, "program_id": program.id,
                "s1": s1.id, "s2": s2.id, "s3": s3.id, "venue_id": venue.id}

    def _league_with_teams(self, fx, season_id, tag="Div"):
        """A League in ``season_id`` with one Division and two registered
        Teams, so BOTH standings tables built over it — per-Division and
        LeagueSeason-wide — are NON-EMPTY. That is what makes the two standings
        cases falsifiable: each roster comes from active registrations, so a
        correct answer has rows and a raced one has none.

        Returns both ids because the two routes name different levels of the
        same hierarchy; building one fixture for both keeps the pair honest,
        since a divergence in what they can see would show up as one case
        passing on a fixture the other could not use."""
        svc = self.api.setup
        suffix = uuid.uuid4().hex[:6]
        league = svc.create_league(season_id, f"{fx['tag']} {tag} L {suffix}")
        division = svc.create_division(season_id, f"{tag} D {suffix}",
                                       league_id=league.id)
        club = svc.create_club(f"{tag} Club {suffix}")
        for team_name in (f"{tag} Alpha {suffix}", f"{tag} Bravo {suffix}"):
            team = svc.create_team(club_id=club.id, name=team_name,
                                   league_id=league.id,
                                   division_id=division.id)
            svc.register_team_for_season(season_id, team.id, division.id)
        return {"league_id": league.id, "division_id": division.id}

    def _division_with_teams(self, fx, season_id, tag="Div"):
        """The Division of ``_league_with_teams`` — the per-Division route's
        named target."""
        return self._league_with_teams(fx, season_id, tag)["division_id"]

    def _scenario_in(self, fx, season_id, name="Named run"):
        """One stored ``ScheduleScenario`` bound to ``season_id``.

        Written straight at the store rather than through
        ``create_schedule_scenario``: the route under test READS a stored row
        back and judges it against the active tuple, and the generator's own
        inputs are not what is being measured. Every FK the SQL stores enforce
        is real — permanent League, LeagueSeason, Program, Season.
        """
        svc = self.api.setup
        league = svc.create_league(season_id, f"{fx['tag']} League {name}")
        ls = self.api.store.league_season_for(league.id, season_id)
        self.assertIsNotNone(ls, "the League was not bound to the Season")
        scenario = ScheduleScenario(
            id=f"scenario_{uuid.uuid4().hex[:12]}",
            name=name, program_id=fx["program_id"], season_id=season_id,
            league_id=league.id, league_season_id=ls.id, division_id=None,
            planner_version="round-robin-v1", input_fingerprint="f",
            proposal_fingerprint="g", request_input={}, proposal={},
            generation_snapshot={},
            created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            created_by=None)
        with self.api.store.transaction():
            self.api.store.add_schedule_scenario(scenario)
        return scenario.id

    def _persisted(self, user_id):
        ctx = self.api.store.get_active_context(user_id)
        return (None, None) if ctx is None else (ctx.program_id, ctx.season_id)

    def _gate(self):
        return self.srv.CONTEXT_GATE

    def _set_epoch_fence_timeout_env(self, value: str):
        """PR #423: set HS_CONTEXT_GATE_TIMEOUT for the duration of one test,
        restoring the prior value (or absence) on cleanup -- the env-var
        half of "the same knob" a `gate.wait_timeout = X` override needs
        alongside it now that the store-layer epoch fence ALSO reads this
        var and has no gate object a test could mutate an attribute on
        directly."""
        original = os.environ.get("HS_CONTEXT_GATE_TIMEOUT")

        def restore():
            if original is None:
                os.environ.pop("HS_CONTEXT_GATE_TIMEOUT", None)
            else:
                os.environ["HS_CONTEXT_GATE_TIMEOUT"] = original

        os.environ["HS_CONTEXT_GATE_TIMEOUT"] = value
        self.addCleanup(restore)

    def _assert_gate_is_clean(self, why):
        """No hold, no waiter, no arrival ticket left registered. Polled
        briefly because a request's ``finally`` runs a hair after its response
        is delivered — but it is an assertion on COUNTERS, never a sleep."""
        def settled():
            snapshot = self._gate().stats()
            return snapshot if (snapshot["readers"] == 0
                                and snapshot["writers"] == 0) else None

        stats = _wait(settled, timeout=5.0) or self._gate().stats()
        self.assertEqual(stats["readers"], 0, f"{why}: {stats}")
        self.assertEqual(stats["writers"], 0, f"{why}: {stats}")
        self.assertEqual(stats["waiting_readers"], 0, f"{why}: {stats}")
        self.assertEqual(stats["waiting_writers"], 0, f"{why}: {stats}")
        return stats

    def _read(self, client, season_id, suffix="venue-candidates"):
        return self._req(client, "GET",
                         f"/api/v2/setup/seasons/{season_id}/{suffix}")

    def _assert_reads_agree_with(self, client, fx, selected):
        """CONTEXT COHERENCE, measured rather than asserted in prose: the
        selected Season answers 200 and every sibling answers the generic 404.
        A tuple that committed while reads settled against another one cannot
        satisfy both halves."""
        for season in (fx["s1"], fx["s2"], fx["s3"]):
            status, raw, _ = self._read(client, season)
            expected = 200 if season == selected else 404
            self.assertEqual(status, expected,
                             f"selected={selected} read={season}: {raw}")

    # -- the reusable race --------------------------------------------------
    @contextmanager
    def _read_parked_in(self, method_name, season_id):
        """Park the named ApiService read INSIDE the server for ``season_id``.

        The park is at the START of the call, before the tuple is resolved —
        the shape the CI failure had, where the read wakes up and resolves a
        tuple that moved underneath it.
        """
        park = _Park()
        exited = {}

        def factory(original):
            def wrapper(sid, *a, **kw):
                if sid == season_id:
                    park.hold()
                try:
                    return original(sid, *a, **kw)
                finally:
                    if sid == season_id:
                        exited["at"] = time.monotonic()
            return wrapper

        self._wrap(self.api, method_name, factory)
        try:
            yield park, exited
        finally:
            park.let_go()

    def _switch_thread(self, client, program_id, season_id, out):
        """Run POST /api/context on its own thread.

        ``out["returned"]`` is when the RESPONSE arrived — useful for "has the
        switch answered yet", and nothing more. The COMMIT instant is a
        different fact and is recorded separately by ``_instrument_commit``,
        because a response can lag a commit by an arbitrary amount and only the
        commit is what the ordering claim is about."""
        def run():
            out["started"] = time.monotonic()
            out["result"] = self._req(client, "POST", "/api/context",
                                      {"program_id": program_id,
                                       "season_id": season_id})
            out["returned"] = time.monotonic()
        t = threading.Thread(target=run, daemon=True)
        t.start()
        return t

    def _instrument_commit(self, sink):
        def factory(original):
            def wrapper(*a, **kw):
                try:
                    return original(*a, **kw)
                finally:
                    sink["committed_at"] = time.monotonic()
            return wrapper
        self._wrap(self.api, "set_active_context", factory)


class ContextSwitchServerExitBase(ContextGateFixtureBase):
    """The cases themselves. Split from the fixture above only so the fixture
    can be reused elsewhere; every case, every assertion and every store class
    below is unchanged."""

    # ======================================================================
    # THE DEFECT
    # ======================================================================
    def _assert_switch_waits_for(self, method_name, path_suffix):
        fx = self._program_with_two_seasons("Hold")
        username, user_id = self._operator("hold")
        reader = self._login(username)
        switcher = self._login(username)
        self._select(reader, fx["program_id"], fx["s1"])
        self.assertEqual(self._persisted(user_id), (fx["program_id"], fx["s1"]))

        commit = {}
        self._instrument_commit(commit)
        read_out = {}

        with self._read_parked_in(method_name, fx["s1"]) as (park, exited):
            def do_read():
                read_out["result"] = self._req(
                    reader, "GET",
                    f"/api/v2/setup/seasons/{fx['s1']}/{path_suffix}")
            rt = threading.Thread(target=do_read, daemon=True)
            rt.start()
            self.assertTrue(park.arrived.wait(PATIENCE),
                            "the read never reached the server")

            switch = {}
            st = self._switch_thread(switcher, fx["program_id"], fx["s2"], switch)

            # THE LOAD-BEARING OBSERVATION. The switch has been given a full
            # second, on a server that commits one in milliseconds. If the
            # persisted tuple has moved while a dispatched read is still inside
            # the handler, the read is about to be judged against a selection
            # it never saw — which is the CI 404, exactly.
            time.sleep(COMMIT_WINDOW)
            persisted_during = self._persisted(user_id)
            still_running = st.is_alive()

            park.let_go()
            rt.join(PATIENCE)
            st.join(PATIENCE)

        self.assertEqual(
            persisted_during, (fx["program_id"], fx["s1"]),
            f"the context switch COMMITTED while a dispatched "
            f"{path_suffix} read was still inside the server "
            f"(persisted tuple moved to {persisted_during}); the read is now "
            f"evaluated against a selection it never saw")
        self.assertTrue(
            still_running,
            "the switch's POST had already returned while the read was held — "
            "it did not wait for the handler to exit")

        status, raw, body = read_out["result"]
        self.assertEqual(status, 200,
                         f"the held read was refused after the switch: {raw}")
        self.assertEqual(body.get("season_id", fx["s1"]), fx["s1"], raw)

        s_status, s_raw, _ = switch["result"]
        self.assertEqual(s_status, 200, f"the switch itself failed: {s_raw}")
        self.assertEqual(self._persisted(user_id), (fx["program_id"], fx["s2"]))

        self.assertIn("at", exited, "the read handler never exited")
        self.assertLess(
            exited["at"], commit["committed_at"],
            "the switch committed BEFORE the read handler exited")

    def test_a_switch_cannot_commit_while_a_scoped_read_is_inside_the_server(self):
        self._assert_switch_waits_for("get_venue_grant_candidates",
                                      "venue-candidates")

    def test_a_switch_cannot_commit_while_a_venue_access_read_is_inside(self):
        self._assert_switch_waits_for("list_season_venue_access",
                                      "venue-access")

    # ----------------------------------------------------------------------
    # THE ROUTE TABLE IS A CLAIM ABOUT COVERAGE, so it is tested as one.
    #
    # `CONTEXT_SCOPED_READ_ROUTES` states its own admission criterion: a route
    # belongs there when it has the exact-selected-Season ceiling
    # (`season_id != active_season.id`). Two routes were listed; three more
    # GET routes reach that comparison, and two of them apply it to a
    # CALLER-NAMED record and answer generically when it does not match — the
    # identical defect shape, on a different noun. Each gets the same case.
    # ----------------------------------------------------------------------
    def test_a_switch_cannot_commit_while_a_scenario_read_is_inside(self):
        """``GET /api/scheduler/scenarios/<id>`` — the same defect, one noun
        over.

        Its ceiling is ``_scenario_in_active_tuple``, which calls
        ``resolve_with_league`` INSIDE the request, after the stored row has
        been read, and hands the result to ``_setup_target_edge_allows`` — the
        very ``season_id != season.id`` comparison the route table names as its
        admission criterion. A switch that commits while this read is inside
        the server makes it answer ``_scenario_not_found``: the same generic
        404 a scenario of another Program gets, for evidence the operator was
        legitimately looking at a moment earlier.
        """
        fx = self._program_with_two_seasons("Scen")
        username, user_id = self._operator("scen")
        reader = self._login(username)
        switcher = self._login(username)
        self._select(reader, fx["program_id"], fx["s1"])
        scenario_id = self._scenario_in(fx, fx["s1"])

        commit = {}
        self._instrument_commit(commit)
        read_out = {}

        with self._read_parked_in("get_schedule_scenario", scenario_id) as (
                park, exited):
            def do_read():
                read_out["result"] = self._req(
                    reader, "GET", f"/api/scheduler/scenarios/{scenario_id}")

            rt = threading.Thread(target=do_read, daemon=True)
            rt.start()
            self.assertTrue(park.arrived.wait(PATIENCE),
                            "the scenario read never reached the server")

            switch = {}
            st = self._switch_thread(switcher, fx["program_id"], fx["s2"],
                                     switch)
            time.sleep(COMMIT_WINDOW)
            persisted_during = self._persisted(user_id)
            still_running = st.is_alive()

            park.let_go()
            rt.join(PATIENCE)
            st.join(PATIENCE)

        self.assertEqual(
            persisted_during, (fx["program_id"], fx["s1"]),
            f"the context switch COMMITTED while a dispatched scenario read "
            f"was still inside the server (persisted tuple moved to "
            f"{persisted_during}); that read is now judged against a selection "
            f"it never saw and answers the generic scenario 404")
        self.assertTrue(
            still_running,
            "the switch's POST had already returned while the scenario read "
            "was held — the route is not ordered against it at all")

        status, raw, body = read_out["result"]
        self.assertEqual(status, 200,
                         f"the held scenario read was refused after the "
                         f"switch: {raw}")
        self.assertEqual(body.get("scenario_id"), scenario_id, raw)
        self.assertEqual(switch["result"][0], 200, switch["result"][1])
        self.assertEqual(self._persisted(user_id), (fx["program_id"], fx["s2"]))
        self.assertIn("at", exited, "the scenario read handler never exited")
        self.assertLess(exited["at"], commit["committed_at"],
                        "the switch committed BEFORE the scenario read "
                        "handler exited")

    def test_control_a_non_selected_seasons_scenario_still_gets_the_404(self):
        """The ceiling this route enforces is NOT weakened by ordering it: an
        UNRACED read of a scenario belonging to a sibling Season of the active
        Program is still refused, and still names nothing about it."""
        fx = self._program_with_two_seasons("ScenCeil")
        username, _ = self._operator("scenceil")
        c = self._login(username)
        self._select(c, fx["program_id"], fx["s1"])
        mine = self._scenario_in(fx, fx["s1"], name="Mine")
        sibling = self._scenario_in(fx, fx["s2"], name="Sibling run")

        status, raw, body = self._req(c, "GET",
                                      f"/api/scheduler/scenarios/{mine}")
        self.assertEqual(status, 200, f"the selected Season's scenario: {raw}")
        self.assertEqual(body["scenario_id"], mine, raw)

        status, raw, body = self._req(c, "GET",
                                      f"/api/scheduler/scenarios/{sibling}")
        self.assertEqual(status, 404,
                         f"a sibling Season's scenario was not refused: {raw}")
        self.assertNotIn("Sibling run", raw)
        status, raw, _ = self._req(
            c, "GET", "/api/scheduler/scenarios/scenario_does_not_exist_9")
        self.assertEqual(status, 404, raw)

    def test_a_switch_cannot_commit_while_a_standings_read_is_inside(self):
        """``GET /api/standings/<division_id>`` — the third route with the
        ceiling, found by re-auditing rather than by resemblance.

        ``_division_matches_active_context`` compares the Division's validated
        LeagueSeason against the resolved active tuple with
        ``league_season.season_id != season.id``: literally the admission
        criterion, against the same ``resolve_with_league`` result, resolved
        INSIDE the request. What differs is only the SHAPE of the wrong answer
        — an empty standings table rather than a 404 — and that difference
        makes it worse, not better: the operator is shown a Division that
        exists, that they selected, and that appears to have no teams.
        """
        fx = self._program_with_two_seasons("Stand")
        username, user_id = self._operator("stand")
        reader = self._login(username)
        switcher = self._login(username)
        self._select(reader, fx["program_id"], fx["s1"])
        division_id = self._division_with_teams(fx, fx["s1"])

        # Positive control FIRST: this Division really does answer with rows
        # under the selected Season, so an empty answer below would be the
        # race and not an empty fixture.
        status, raw, body = self._req(reader, "GET",
                                      f"/api/standings/{division_id}")
        self.assertEqual(status, 200, raw)
        self.assertTrue(body["standings"],
                        f"the fixture Division has no standings rows at all, "
                        f"so this case could not tell a raced read from a "
                        f"healthy one: {raw}")

        read_out = {}
        with self._read_parked_in("get_standings", division_id) as (
                park, _exited):
            def do_read():
                read_out["result"] = self._req(
                    reader, "GET", f"/api/standings/{division_id}")

            rt = threading.Thread(target=do_read, daemon=True)
            rt.start()
            self.assertTrue(park.arrived.wait(PATIENCE),
                            "the standings read never reached the server")

            switch = {}
            st = self._switch_thread(switcher, fx["program_id"], fx["s2"],
                                     switch)
            time.sleep(COMMIT_WINDOW)
            persisted_during = self._persisted(user_id)
            still_running = st.is_alive()

            park.let_go()
            rt.join(PATIENCE)
            st.join(PATIENCE)

        self.assertEqual(
            persisted_during, (fx["program_id"], fx["s1"]),
            f"the context switch COMMITTED while a dispatched standings read "
            f"was still inside the server (persisted tuple moved to "
            f"{persisted_during})")
        self.assertTrue(
            still_running,
            "the switch's POST had already returned while the standings read "
            "was held — the route is not ordered against it at all")

        status, raw, body = read_out["result"]
        self.assertEqual(status, 200, raw)
        self.assertTrue(
            body["standings"],
            f"the held standings read came back EMPTY — it was judged against "
            f"the Season the switch installed underneath it, and an operator "
            f"is now looking at a Division that appears to have no teams: "
            f"{raw}")
        self.assertEqual(switch["result"][0], 200, switch["result"][1])

    def test_control_a_non_selected_seasons_division_still_reads_empty(self):
        """The standings ceiling is unweakened: an UNRACED read of a Division
        in a sibling Season of the active Program is still the generic empty
        shape, with no team names in it."""
        fx = self._program_with_two_seasons("StandCeil")
        username, _ = self._operator("standceil")
        c = self._login(username)
        self._select(c, fx["program_id"], fx["s1"])
        mine = self._division_with_teams(fx, fx["s1"], tag="Mine")
        sibling = self._division_with_teams(fx, fx["s2"], tag="Sibling")

        status, raw, body = self._req(c, "GET", f"/api/standings/{mine}")
        self.assertEqual(status, 200, raw)
        self.assertTrue(body["standings"], raw)

        status, raw, body = self._req(c, "GET", f"/api/standings/{sibling}")
        self.assertEqual(status, 200, raw)
        self.assertEqual(body["standings"], [],
                         f"a sibling Season's Division answered with rows: "
                         f"{raw}")
        self.assertNotIn("Sibling", raw)

    def test_a_switch_cannot_commit_while_a_league_season_read_is_inside(self):
        """``GET /api/standings/league-season/<l>/<s>`` — the fifth route, and
        the only one that joined the table by ACQUIRING the ceiling rather than
        by an audit finding one already there.

        Before #202 it resolved no tuple: it passed no ``user_id``/``role``/
        ``scope`` and answered anonymous callers, so it failed the admission
        criterion outright and was correctly absent from this file. It now runs
        the SAME comparison as its per-Division sibling
        (``_league_season_matches_active_context``, shared by both), one level
        of the hierarchy up, so a switch landing mid-read turns a table the
        operator explicitly asked for into the generic ``not_found``.
        """
        fx = self._program_with_two_seasons("LsStand")
        username, user_id = self._operator("lsstand")
        reader = self._login(username)
        switcher = self._login(username)
        self._select(reader, fx["program_id"], fx["s1"])
        ids = self._league_with_teams(fx, fx["s1"])
        path = (f"/api/standings/league-season/{ids['league_id']}/"
                f"{fx['s1']}")

        # Positive control FIRST, for the same reason the sibling has one: this
        # LeagueSeason really does answer with rows under the selected Season,
        # so a refusal below is the race and not an empty fixture.
        status, raw, body = self._req(reader, "GET", path)
        self.assertEqual(status, 200, raw)
        self.assertTrue(body["standings"],
                        f"the fixture LeagueSeason has no standings rows at "
                        f"all, so this case could not tell a raced read from a "
                        f"healthy one: {raw}")

        read_out = {}
        with self._read_parked_in("get_league_season_standings",
                                  ids["league_id"]) as (park, _exited):
            def do_read():
                read_out["result"] = self._req(reader, "GET", path)

            rt = threading.Thread(target=do_read, daemon=True)
            rt.start()
            self.assertTrue(park.arrived.wait(PATIENCE),
                            "the LeagueSeason standings read never reached the "
                            "server")

            switch = {}
            st = self._switch_thread(switcher, fx["program_id"], fx["s2"],
                                     switch)
            time.sleep(COMMIT_WINDOW)
            persisted_during = self._persisted(user_id)
            still_running = st.is_alive()

            park.let_go()
            rt.join(PATIENCE)
            st.join(PATIENCE)

        self.assertEqual(
            persisted_during, (fx["program_id"], fx["s1"]),
            f"the context switch COMMITTED while a dispatched LeagueSeason "
            f"standings read was still inside the server (persisted tuple "
            f"moved to {persisted_during})")
        self.assertTrue(
            still_running,
            "the switch's POST had already returned while the LeagueSeason "
            "standings read was held — the route is not ordered against it "
            "at all")

        status, raw, body = read_out["result"]
        self.assertEqual(
            status, 200,
            f"the held LeagueSeason standings read was REFUSED — it was judged "
            f"against the Season the switch installed underneath it: {raw}")
        self.assertTrue(body["standings"], raw)
        self.assertEqual(switch["result"][0], 200, switch["result"][1])

    def test_control_a_non_selected_seasons_league_season_still_refuses(self):
        """The LeagueSeason ceiling is unweakened: an UNRACED read of a League
        in a sibling Season of the active Program is still the generic
        ``not_found``, with no team names in it — and is the SAME answer a
        nonexistent (league, season) pair takes, so it is not an oracle."""
        fx = self._program_with_two_seasons("LsCeil")
        username, _ = self._operator("lsceil")
        c = self._login(username)
        self._select(c, fx["program_id"], fx["s1"])
        mine = self._league_with_teams(fx, fx["s1"], tag="Mine")
        sibling = self._league_with_teams(fx, fx["s2"], tag="Sibling")

        status, raw, body = self._req(
            c, "GET",
            f"/api/standings/league-season/{mine['league_id']}/{fx['s1']}")
        self.assertEqual(status, 200, raw)
        self.assertTrue(body["standings"], raw)

        status, raw, _ = self._req(
            c, "GET",
            f"/api/standings/league-season/{sibling['league_id']}/{fx['s2']}")
        self.assertEqual(status, 404,
                         f"a sibling Season's LeagueSeason answered: {raw}")
        self.assertNotIn("Sibling", raw)
        missing_status, missing_raw, _ = self._req(
            c, "GET",
            "/api/standings/league-season/league_nope_9/season_nope_9")
        self.assertEqual(missing_status, 404, missing_raw)
        self.assertEqual(raw, missing_raw,
                         "a sibling Season's LeagueSeason is distinguishable "
                         "from one that does not exist")

    def test_a_read_parked_before_identity_still_orders_the_switch(self):
        """PHASE A. The read is parked inside ``SESSIONS.resolve`` — inside
        ``_resolve_role()``, before the request has any identity at all. A gate
        that only tracked identified readers would see nothing to wait for and
        let the switch commit straight past it."""
        fx = self._program_with_two_seasons("Phase")
        username, user_id = self._operator("phase")
        reader = self._login(username)
        switcher = self._login(username)
        self._select(reader, fx["program_id"], fx["s1"])

        park = _Park()
        armed = threading.Event()
        sessions = self.srv.SESSIONS
        original = sessions.resolve

        def wrapper(store, sid):
            if armed.is_set():
                armed.clear()
                park.hold()
            return original(store, sid)

        sessions.resolve = wrapper
        self._restores.append(lambda: setattr(sessions, "resolve", original))

        read_out = {}

        def do_read():
            armed.set()
            read_out["result"] = self._req(
                reader, "GET",
                f"/api/v2/setup/seasons/{fx['s1']}/venue-candidates")

        rt = threading.Thread(target=do_read, daemon=True)
        rt.start()
        self.assertTrue(park.arrived.wait(PATIENCE),
                        "the read never reached _resolve_role")

        switch = {}
        st = self._switch_thread(switcher, fx["program_id"], fx["s2"], switch)
        time.sleep(COMMIT_WINDOW)
        persisted_during = self._persisted(user_id)
        park.let_go()
        rt.join(PATIENCE)
        st.join(PATIENCE)

        self.assertEqual(
            persisted_during, (fx["program_id"], fx["s1"]),
            "the switch committed while a dispatched read sat pre-identity "
            "inside the server")
        status, raw, body = read_out["result"]
        self.assertEqual(status, 200, raw)
        self.assertEqual(body.get("season_id", fx["s1"]), fx["s1"], raw)

    # ======================================================================
    # THE CEILING CONTROL — must stay exactly as strict as it is today
    # ======================================================================
    def test_control_a_non_selected_season_read_still_gets_the_generic_404(self):
        """An INDEPENDENTLY issued read (no switch in flight, nothing held) for
        a Season that is not the selected one is still refused with the same
        generic 404, and still names nothing. The gate admits it; the ceiling
        refuses it."""
        fx = self._program_with_two_seasons("Ceiling")
        username, _ = self._operator("ceiling")
        c = self._login(username)
        self._select(c, fx["program_id"], fx["s1"])

        # Positive control first: the SELECTED Season really does answer 200,
        # so the refusals below are a scope decision, not a broken fixture.
        for suffix in ("venue-candidates", "venue-access"):
            status, raw, _ = self._req(
                c, "GET", f"/api/v2/setup/seasons/{fx['s1']}/{suffix}")
            self.assertEqual(status, 200, f"selected {suffix}: {raw}")

        for suffix in ("venue-candidates", "venue-access"):
            status, raw, body = self._req(
                c, "GET", f"/api/v2/setup/seasons/{fx['s2']}/{suffix}")
            self.assertEqual(status, 404,
                             f"sibling Season {suffix} was not refused: {raw}")
            self.assertEqual(body["error"]["code"], "not_found", raw)
            self.assertNotIn(f"{fx['tag']} Season Two", raw)
            self.assertNotIn(f"{fx['tag']} Venue", raw)

        # ...and a nonexistent Season is byte-identical in shape, so the
        # refusal is not an existence oracle.
        ghost = "season_does_not_exist_999999"
        status, raw, body = self._req(
            c, "GET", f"/api/v2/setup/seasons/{ghost}/venue-candidates")
        self.assertEqual(status, 404, raw)
        self.assertEqual(body["error"]["code"], "not_found", raw)

    # ======================================================================
    # OWNER CLASS 1 — CONCURRENT SWITCHES
    # ======================================================================
    def test_concurrent_switches_for_one_user_stay_context_coherent(self):
        """TWO switches for the SAME user, issued at once, while a scoped read
        for the ORIGINAL Season is held inside the server.

        What would be incoherent: one switch's tuple committing while the read
        settles against the other's, or the two switches interleaving into a
        tuple neither of them asked for. What is asserted:

          * NEITHER switch commits while the read is inside the handler;
          * the held read still answers 200 for the Season it named;
          * the final persisted tuple is EXACTLY one of the two requested ones
            — never a mixture, never the original;
          * afterwards, reads agree with that tuple and only that tuple: the
            selected Season answers 200 and BOTH siblings answer the generic
            404. This is the coherence claim, measured on the wire;
          * the gate is left empty.
        """
        fx = self._program_with_two_seasons("Concur")
        username, user_id = self._operator("concur")
        reader = self._login(username)
        a = self._login(username)
        b = self._login(username)
        self._select(reader, fx["program_id"], fx["s1"])

        with self._read_parked_in("get_venue_grant_candidates", fx["s1"]) as (
                park, _exited):
            read_out = {}

            def do_read():
                read_out["result"] = self._read(reader, fx["s1"])

            rt = threading.Thread(target=do_read, daemon=True)
            rt.start()
            self.assertTrue(park.arrived.wait(PATIENCE),
                            "the read never reached the server")

            sa, sb = {}, {}
            ta = self._switch_thread(a, fx["program_id"], fx["s2"], sa)
            tb = self._switch_thread(b, fx["program_id"], fx["s3"], sb)

            time.sleep(COMMIT_WINDOW)
            persisted_during = self._persisted(user_id)

            park.let_go()
            rt.join(PATIENCE)
            ta.join(PATIENCE)
            tb.join(PATIENCE)

        self.assertEqual(
            persisted_during, (fx["program_id"], fx["s1"]),
            "a concurrent switch committed while the read was inside the "
            "server — the read is being judged against a tuple it never saw")

        status, raw, body = read_out["result"]
        self.assertEqual(status, 200, f"the held read was refused: {raw}")
        self.assertEqual(body.get("season_id"), fx["s1"], raw)

        for label, out in (("A->s2", sa), ("B->s3", sb)):
            self.assertEqual(out["result"][0], 200,
                             f"switch {label} failed: {out['result'][1]}")

        program, season = self._persisted(user_id)
        self.assertEqual(program, fx["program_id"])
        self.assertIn(season, (fx["s2"], fx["s3"]),
                      "the two switches interleaved into a tuple neither of "
                      "them requested")
        self._assert_reads_agree_with(reader, fx, season)
        self._assert_gate_is_clean("after two concurrent switches")

    def test_a_second_switch_cannot_commit_inside_the_first(self):
        """The writer-vs-writer half of class 1, isolated. Two switches for one
        user are ordered by ARRIVAL, so the second cannot commit while the
        first is still inside ``set_active_context``."""
        fx = self._program_with_two_seasons("Serial")
        username, user_id = self._operator("serial")
        a = self._login(username)
        b = self._login(username)
        self._select(a, fx["program_id"], fx["s1"])

        park = _Park()
        armed = threading.Event()

        def factory(original):
            def wrapper(*args, **kw):
                if armed.is_set():
                    armed.clear()
                    park.hold()
                return original(*args, **kw)
            return wrapper

        self._wrap(self.api, "set_active_context", factory)

        sa, sb = {}, {}
        armed.set()
        ta = self._switch_thread(a, fx["program_id"], fx["s2"], sa)
        self.assertTrue(park.arrived.wait(PATIENCE),
                        "the first switch never reached set_active_context")
        tb = self._switch_thread(b, fx["program_id"], fx["s3"], sb)
        time.sleep(COMMIT_WINDOW)
        self.assertEqual(
            self._persisted(user_id), (fx["program_id"], fx["s1"]),
            "the second switch committed while the first was still inside its "
            "own commit")
        park.let_go()
        ta.join(PATIENCE)
        tb.join(PATIENCE)

        self.assertEqual(sa["result"][0], 200, sa["result"][1])
        self.assertEqual(sb["result"][0], 200, sb["result"][1])
        self.assertEqual(self._persisted(user_id), (fx["program_id"], fx["s3"]),
                         "the later-arriving switch did not win")
        self._assert_gate_is_clean("after two serialized switches")

    # ======================================================================
    # OWNER CLASS 2 — FAILURE / CANCELLATION WHILE WAITING
    # ======================================================================
    def test_a_switch_that_fails_or_is_cancelled_while_waiting_leaks_nothing(
            self):
        """A blocked writer holds NOTHING but the gate's own registration — it
        has not opened a transaction, not taken the #386 mutex, not touched the
        store. Both ways of not finishing are exercised:

        PHASE 1 — the switch WAITS for a held read and then FAILS at the
        service (it names a Program that does not exist). Nothing commits, the
        exclusive hold is released by ``finally``, and the operator's old
        context is still usable.

        PHASE 2 — the client DISCONNECTS while its switch is blocked waiting.
        The response write raises on a dead socket; the hold must still be
        released. Asserted on the gate's own counters, then by a subsequent
        switch for the same user completing PROMPTLY (a leaked exclusive hold
        would make it wait out the whole timeout, and a leaked shared hold
        would do the same to the next switch).
        """
        fx = self._program_with_two_seasons("Cancel")
        username, user_id = self._operator("cancel")
        reader = self._login(username)
        failer = self._login(username)
        self._select(reader, fx["program_id"], fx["s1"])

        # -- PHASE 1: the switch fails after waiting ------------------------
        with self._read_parked_in("get_venue_grant_candidates", fx["s1"]) as (
                park, _exited):
            read_out = {}

            def do_read():
                read_out["result"] = self._read(reader, fx["s1"])

            rt = threading.Thread(target=do_read, daemon=True)
            rt.start()
            self.assertTrue(park.arrived.wait(PATIENCE))

            bad = {}
            tb = self._switch_thread(failer, "program_does_not_exist_999",
                                     fx["s1"], bad)
            time.sleep(COMMIT_WINDOW)
            self.assertTrue(tb.is_alive(),
                            "the doomed switch did not even wait for the read")
            park.let_go()
            rt.join(PATIENCE)
            tb.join(PATIENCE)

        self.assertEqual(read_out["result"][0], 200, read_out["result"][1])
        self.assertNotEqual(bad["result"][0], 200,
                            "a switch to a nonexistent Program succeeded")
        self.assertEqual(
            self._persisted(user_id), (fx["program_id"], fx["s1"]),
            "a FAILED switch moved the persisted tuple")
        self._assert_gate_is_clean("after a switch failed while waiting")

        # The old context is still usable, not merely still persisted.
        self._assert_reads_agree_with(reader, fx, fx["s1"])

        # -- PHASE 2: the client vanishes while its switch is waiting -------
        cookie = self._session_cookie(username)
        with self._read_parked_in("get_venue_grant_candidates", fx["s1"]) as (
                park, _exited):
            read_out2 = {}

            def do_read2():
                read_out2["result"] = self._read(reader, fx["s1"])

            rt = threading.Thread(target=do_read2, daemon=True)
            rt.start()
            self.assertTrue(park.arrived.wait(PATIENCE))

            self._abandoned_post("/api/context", cookie,
                                 {"program_id": fx["program_id"],
                                  "season_id": fx["s2"]})
            time.sleep(COMMIT_WINDOW)
            self.assertEqual(
                self._persisted(user_id), (fx["program_id"], fx["s1"]),
                "the abandoned switch committed while the read was held")
            park.let_go()
            rt.join(PATIENCE)

        self.assertEqual(read_out2["result"][0], 200, read_out2["result"][1])
        self._assert_gate_is_clean("after a switch's client vanished mid-wait")

        # NOTHING IS PERMANENTLY BLOCKED: a fresh switch for the same user
        # completes promptly. If either hold had leaked this would sit out the
        # gate's full wait_timeout instead.
        survivor = self._login(username)
        started = time.monotonic()
        self._select(survivor, fx["program_id"], fx["s3"])
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, self._gate().wait_timeout / 2,
                        f"a later switch waited {elapsed:.2f}s — a hold leaked")
        self.assertEqual(self._persisted(user_id), (fx["program_id"], fx["s3"]))
        self._assert_gate_is_clean("after the recovery switch")

    # ======================================================================
    # OWNER CLASS 3 — REPEATED SWITCHING
    # ======================================================================
    def test_repeated_switching_accumulates_no_waiters(self):
        """Twelve switches back and forth, each with a real scoped read against
        the tuple that is current at that moment.

        Drift is what this looks for: a ticket that is registered and never
        removed, a waiter counter that only ever goes up, a timeout budget that
        is silently being spent. After EVERY iteration the gate must be back to
        exactly zero on all four counters, the timeout count must not have
        moved off its starting value, and every read must have agreed with the
        tuple that was selected when it was issued.
        """
        fx = self._program_with_two_seasons("Repeat")
        username, user_id = self._operator("repeat")
        c = self._login(username)
        self._select(c, fx["program_id"], fx["s1"])
        timeouts_before = self._gate().stats()["timeouts"]

        rounds = 12
        target = None
        for i in range(rounds):
            target = fx["s2"] if i % 2 == 0 else fx["s1"]
            self._select(c, fx["program_id"], target)
            self.assertEqual(self._persisted(user_id),
                             (fx["program_id"], target),
                             f"round {i}: the tuple did not land")
            for suffix in ("venue-candidates", "venue-access"):
                status, raw, _ = self._read(c, target, suffix)
                self.assertEqual(status, 200, f"round {i} {suffix}: {raw}")
            other = fx["s1"] if target == fx["s2"] else fx["s2"]
            status, raw, _ = self._read(c, other)
            self.assertEqual(status, 404,
                             f"round {i}: the unselected Season answered "
                             f"{status}: {raw}")
            stats = self._assert_gate_is_clean(f"after round {i}")
            self.assertEqual(
                stats["timeouts"], timeouts_before,
                f"round {i}: a bounded wait EXPIRED during ordinary "
                f"switching — the gate is blocking where it should not")

        # THE FINAL STATE IS THE LAST ONE ASKED FOR — not an average, not a
        # leftover from an earlier round.
        self.assertEqual(self._persisted(user_id), (fx["program_id"], target))
        self._assert_reads_agree_with(c, fx, target)

    # ======================================================================
    # OWNER CLASS 4 — NO INDEFINITE BLOCKING
    # ======================================================================
    def test_a_waiter_cannot_block_forever_on_a_read_that_never_returns(self):
        """The pathological case, FORCED — not a prose claim that a timeout
        exists.

        A scoped read is parked inside the server and is NEVER released while
        the assertions run. Both wait directions are then driven into the bound
        and observed to hit it:

          * A SWITCH waiting on that read is answered in BOUNDED time, in
            roughly ``wait_timeout`` (now compounded once through the new
            store-layer fence's OWN bound too — see the env-var note below),
            and ``stats()["timeouts"]`` goes up — a wedged read cannot lock
            an operator out FOREVER. PR #423 (design §4.5, deliberate):
            the answer is now a retryable 409, not a silent 200 — the new
            fence's writer side fails CLOSED on timeout rather than open,
            specifically so a writer can never silently readmit the exact
            TOCTOU the fence exists to close. Bounded-and-actionable, not
            indefinite, is the property this test actually proves; the
            OLD gate's silent-success wire shape was never the safety
            property itself.
          * A LATER scoped read waiting on a switch that never finishes is
            bounded the same way, and answers rather than hanging — this
            direction is UNCHANGED, because the park in direction 2 sits
            before the new fence is ever acquired (see that block's own
            comment), so only the old, unchanged reader-fails-open path is
            exercised there.

        The gate is left empty once the parked participants finally exit, so
        hitting the bound is a HANDLED outcome and not a corrupted one.
        """
        gate = self._gate()
        original_timeout = gate.wait_timeout
        gate.wait_timeout = 0.4
        self.addCleanup(setattr, gate, "wait_timeout", original_timeout)
        # PR #423: the store-layer epoch fence reads HS_CONTEXT_GATE_TIMEOUT
        # directly (it has no gate OBJECT to mutate an attribute on) and is
        # ALSO held by the same parked read this test never releases, so it
        # must be given the SAME bound as `gate.wait_timeout` above or this
        # test's own PATIENCE budget is exceeded by a mechanism the test
        # predates -- see services/epoch_fence.py / SqlStore.epoch_fence_
        # acquire_shared's own docstring for why this is "the same knob."
        self._set_epoch_fence_timeout_env("0.4")

        fx = self._program_with_two_seasons("Bound")
        username, user_id = self._operator("bound")
        reader = self._login(username)
        switcher = self._login(username)
        self._select(reader, fx["program_id"], fx["s1"])

        # -- direction 1: a switch waiting on a read that never returns ----
        timeouts_before = gate.stats()["timeouts"]
        with self._read_parked_in("get_venue_grant_candidates", fx["s1"]) as (
                park, _exited):
            read_out = {}

            def do_read():
                read_out["result"] = self._read(reader, fx["s1"])

            rt = threading.Thread(target=do_read, daemon=True)
            rt.start()
            self.assertTrue(park.arrived.wait(PATIENCE))

            switch = {}
            started = time.monotonic()
            st = self._switch_thread(switcher, fx["program_id"], fx["s2"],
                                     switch)
            st.join(PATIENCE)
            elapsed = time.monotonic() - started

            self.assertFalse(st.is_alive(),
                             "the switch never completed — a wedged read "
                             "blocked it INDEFINITELY")
            # PR #423 (design §4.5, deliberate and documented): the OLD gate
            # alone fails OPEN on both sides, so a switch racing a wedged
            # read used to succeed silently once its bound passed. The new
            # store-layer fence is asymmetric ON PURPOSE — reader fails
            # open, WRITER fails CLOSED (raises, retryable) — because a
            # writer failing open would silently readmit the exact TOCTOU
            # the fence exists to close, for every concurrent reader, which
            # the design's own §4.5 judges a worse trade than a bounded,
            # actionable 409 for the rare pathological case this test
            # forces. The switch is NOT locked out INDEFINITELY (this
            # assertion, above, still holds: it completes, bounded, and
            # this is the numeric bound checked below) — it is told,
            # correctly, that the read it raced is still live and to retry.
            #
            # round-N+1 CORRECTION: this used to read "ONLY on the backend
            # where the fence is REAL (PostgreSQL)" and expect 200 (silent,
            # unordered success) on BOTH Memory and file-backed SQLite,
            # because BOTH of `epoch_fence_acquire_exclusive`/`_shared` were
            # genuine no-ops there before this round. That premise no longer
            # holds for file-backed SQLite specifically:
            # `_read_under_context_gate_sqlite` now runs a scoped read's
            # epoch-check-through-produce() window against its OWN,
            # independent `SqlStore` connection, holding a REAL SQLite file
            # lock (`BEGIN IMMEDIATE`) for the read's whole parked duration —
            # so a WRITER's connection (`STATE.api.store`, a SEPARATE
            # connection to the SAME file) now genuinely CONTENDS with it,
            # exactly as it always has against PostgreSQL's real advisory
            # lock. The store-layer retry loop
            # (`ContextService._snapshot`'s `_MAX_SNAPSHOT_RETRIES`)
            # exhausts against that real, busy-timeout-bounded contention
            # and surfaces the SAME translated, retryable
            # `ConcurrencyConflictError` Postgres's timeout already does —
            # measured directly: this case now returns 409 on file-backed
            # SQLite too, not 200. Memory is UNCHANGED (still 200): it has no
            # database-level lock at all to contend on, and row 1 (context
            # switch) was ALREADY wired into `CONTEXT_GATE` before this
            # round, so nothing about ITS behavior for this exact race
            # differs — the OLD, unchanged, fails-open-on-both-sides gate
            # still governs it alone.
            is_durable_store = bool(self.STORE_URL)
            if is_durable_store:
                self.assertEqual(switch["result"][0], 409, switch["result"][1])
                self.assertEqual(
                    switch["result"][2].get("error", {}).get("details", {})
                        .get("retryable"),
                    True, switch["result"][1])
            else:
                self.assertEqual(switch["result"][0], 200, switch["result"][1])
            self.assertGreaterEqual(
                elapsed, gate.wait_timeout * 0.5,
                "the switch did not actually wait — the bound was not the "
                "thing that released it, so this proves nothing")
            self.assertLess(
                elapsed, gate.wait_timeout + 5.0,
                f"the switch took {elapsed:.2f}s against a {gate.wait_timeout}s "
                f"bound")
            self.assertGreater(
                gate.stats()["timeouts"], timeouts_before,
                "the wait ended without the bound being recorded — it was not "
                "the timeout that released it")
            # THE READ IS STILL PARKED. The bound is what moved, nothing else.
            self.assertTrue(park.arrived.is_set())
            self.assertFalse(park.release.is_set())
            park.let_go()
            rt.join(PATIENCE)

        self._assert_gate_is_clean("after a bounded switch wait expired")

        # -- direction 2: a read waiting on a switch that never finishes ---
        self._select(reader, fx["program_id"], fx["s1"])
        timeouts_before = gate.stats()["timeouts"]
        switch_park = _Park()
        armed = threading.Event()

        def factory(original):
            def wrapper(*args, **kw):
                if armed.is_set():
                    armed.clear()
                    switch_park.hold()
                return original(*args, **kw)
            return wrapper

        self._wrap(self.api, "set_active_context", factory)
        armed.set()
        stuck = {}
        st = self._switch_thread(switcher, fx["program_id"], fx["s2"], stuck)
        self.assertTrue(switch_park.arrived.wait(PATIENCE),
                        "the switch never reached its commit")

        started = time.monotonic()
        status, raw, _ = self._read(reader, fx["s1"])
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, gate.wait_timeout + 5.0,
                        f"a scoped read waited {elapsed:.2f}s behind a wedged "
                        f"switch — it was not bounded")
        self.assertIn(status, (200, 404),
                      f"the bounded read answered neither way: {status} {raw}")
        self.assertGreater(gate.stats()["timeouts"], timeouts_before,
                           "the read's wait ended without recording the bound")
        switch_park.let_go()
        st.join(PATIENCE)
        self.assertEqual(stuck["result"][0], 200, stuck["result"][1])
        self._assert_gate_is_clean("after a bounded read wait expired")

    # ======================================================================
    # TWO DISTINCT OPERATORS — the axis every case above is blind to
    # ======================================================================
    #
    # Every other case in this file opens all its sessions for ONE username,
    # so a gate that coupled unrelated operators to each other would pass all
    # of them byte-identically. The gate's own docstring makes a CROSS-USER
    # promise ("cross-user coupling is bounded by one session lookup") and
    # nothing here could falsify it. These two cases can.
    def _park_next_session_resolve(self):
        """Park the NEXT server-side ``SESSIONS.resolve`` — i.e. the next
        request to reach ``_resolve_role()``, before it has any identity and
        therefore while its gate ticket is still an UNBOUND arrival.

        Returns ``(park, arm)``; the caller arms it immediately before issuing
        the one request it wants caught, exactly as PHASE A above does."""
        park = _Park()
        armed = threading.Event()
        sessions = self.srv.SESSIONS
        original = sessions.resolve

        def wrapper(store, sid):
            if armed.is_set():
                armed.clear()
                park.hold()
            return original(store, sid)

        sessions.resolve = wrapper
        self._restores.append(lambda: setattr(sessions, "resolve", original))
        # Released even when an assertion aborts the case mid-park: a victim
        # left parked keeps its gate ticket registered and would charge the
        # NEXT case with a leak it did not cause.
        self.addCleanup(park.let_go)
        return park, armed

    def _park_next_commit(self):
        """Park the NEXT ``set_active_context`` BEFORE it runs, so the switch
        holds the gate's EXCLUSIVE ticket and nothing else — no transaction,
        no store lock, no #386 mutex. Returns ``(park, arm)``."""
        park = _Park()
        armed = threading.Event()

        def factory(original):
            def wrapper(*args, **kw):
                if armed.is_set():
                    armed.clear()
                    park.hold()
                return original(*args, **kw)
            return wrapper

        self._wrap(self.api, "set_active_context", factory)
        self.addCleanup(park.let_go)          # see _park_next_session_resolve
        return park, armed

    def test_one_operators_switch_is_not_stalled_by_anothers_scoped_read(self):
        """CROSS-USER COUPLING, measured on two REAL operators.

        The gate's docstring promises that an unbound arrival ticket couples a
        foreign switch to it for ONE SESSION LOOKUP and no longer: the instant
        the ticket resolves to somebody else, it leaves that switch's wait set.
        This drives the exact interleaving where that promise is load-bearing:

          1. BOB's switch registers and parks INSIDE its own commit, so Bob has
             an exclusive hold outstanding and nothing else.
          2. BOB's scoped read arrives and parks PRE-IDENTITY. Its ticket is an
             UNBOUND arrival with a seq above Bob's switch.
          3. ALICE — a DIFFERENT operator, sharing nothing with Bob but this
             process — switches. Her writer registers, sees an unidentified
             request that arrived before it, and waits. THAT MUCH IS CORRECT.
          4. Bob's read is released, resolves BOB, and narrows its ticket away
             from Alice. Alice is now blocked by nothing. Bob's read then waits
             for BOB's own parked switch — which is Bob's business, not hers.

        The measurement is step 4: how long Alice stays parked AFTER she has
        stopped having anything to wait for. The promise is "one session
        lookup". If the narrowing is not ANNOUNCED before the narrowing thread
        goes to sleep, Alice sleeps out the whole wait bound instead — and does
        so SILENTLY, because a bounded wait whose predicate came true while it
        slept reports success at the deadline and records no timeout. That is
        why the timeout counter is asserted too: without it, the failure looks
        identical to a healthy gate from the outside.
        """
        gate = self._gate()
        original_timeout = gate.wait_timeout
        gate.wait_timeout = 4.0
        self.addCleanup(setattr, gate, "wait_timeout", original_timeout)
        # PR #423: see the identical note in
        # test_a_waiter_cannot_block_forever_on_a_read_that_never_returns.
        self._set_epoch_fence_timeout_env("4.0")

        fx = self._program_with_two_seasons("Cross")
        alice_name, alice_id = self._operator("cross_alice")
        bob_name, bob_id = self._operator("cross_bob")
        alice = self._login(alice_name)
        bob_reader = self._login(bob_name)
        bob_switcher = self._login(bob_name)
        self._select(alice, fx["program_id"], fx["s1"])
        self._select(bob_reader, fx["program_id"], fx["s1"])

        # 1. Bob's switch takes the exclusive hold and parks inside its commit.
        commit_park, arm_commit = self._park_next_commit()
        arm_commit.set()
        bob_switch = {}
        bst = self._switch_thread(bob_switcher, fx["program_id"], fx["s2"],
                                  bob_switch)
        self.assertTrue(commit_park.arrived.wait(PATIENCE),
                        "Bob's switch never reached its commit")

        # 2. Bob's scoped read arrives and parks BEFORE it has an identity.
        read_park, arm_resolve = self._park_next_session_resolve()
        bob_read = {}

        def do_read():
            arm_resolve.set()
            bob_read["result"] = self._read(bob_reader, fx["s2"])

        rt = threading.Thread(target=do_read, daemon=True)
        rt.start()
        self.assertTrue(read_park.arrived.wait(PATIENCE),
                        "Bob's read never reached _resolve_role")

        # 3. Alice registers behind that unbound arrival and waits for it.
        alice_switch = {}
        at = self._switch_thread(alice, fx["program_id"], fx["s2"],
                                 alice_switch)
        self.assertTrue(
            _wait(lambda: self._gate().stats()["waiting_writers"] >= 1),
            "Alice's switch never became a waiter, so this case never set up "
            f"the coupling it measures: {self._gate().stats()}")

        # 4. The session lookup completes. Bob's ticket narrows to Bob and
        #    goes to sleep behind Bob's own switch; Alice must be released by
        #    that narrowing, not by the expiry of her bound.
        timeouts_before = gate.stats()["timeouts"]
        read_park.let_go()
        self.assertTrue(
            _wait(lambda: self._gate().stats()["waiting_readers"] >= 1),
            "Bob's read never reached its own wait behind his switch, so the "
            "narrowing-then-sleep interleaving was never constructed: "
            f"{self._gate().stats()}")
        freed_at = time.monotonic()
        at.join(PATIENCE)
        elapsed = time.monotonic() - freed_at

        self.assertFalse(
            at.is_alive(),
            f"Alice's switch never returned at all: {self._gate().stats()}")
        self.assertLess(
            elapsed, gate.wait_timeout / 2,
            f"Alice's switch stayed parked {elapsed:.2f}s against a "
            f"{gate.wait_timeout}s bound AFTER the only ticket it was waiting "
            f"for had resolved to another operator. Cross-user coupling is "
            f"NOT bounded by one session lookup — it is bounded by the other "
            f"operator's whole switch")
        self.assertEqual(
            gate.stats()["timeouts"], timeouts_before,
            "a bounded wait EXPIRED during a two-operator interleaving that "
            "should not have waited at all — and had it merely been slow "
            "rather than expired, nothing here would have said so")
        self.assertEqual(alice_switch["result"][0], 200,
                         alice_switch["result"][1])

        # Bob unwinds normally, and his read — which registered AFTER his
        # switch — is ordered BEHIND it and answers under the tuple that
        # switch installed.
        commit_park.let_go()
        bst.join(PATIENCE)
        rt.join(PATIENCE)
        self.assertEqual(bob_switch["result"][0], 200, bob_switch["result"][1])
        b_status, b_raw, b_body = bob_read["result"]
        self.assertEqual(b_status, 200,
                         f"Bob's read, ordered behind his own switch, was "
                         f"refused under the tuple that switch installed: "
                         f"{b_raw}")
        self.assertEqual(b_body.get("season_id", fx["s2"]), fx["s2"], b_raw)
        self.assertEqual(self._persisted(alice_id), (fx["program_id"], fx["s2"]))
        self.assertEqual(self._persisted(bob_id), (fx["program_id"], fx["s2"]))
        self._assert_gate_is_clean("after a two-operator interleaving")

    def test_two_operators_hold_independent_contexts_across_a_held_read(self):
        """The structural companion to the case above: whatever the gate makes
        one operator wait for, it must never make the OTHER's tuple move.

        Bob holds a scoped read inside the server for his selected Season while
        Alice switches. Alice's tuple moves and Bob's does not; Bob's switch is
        still ordered behind his own read; and afterwards each operator's reads
        agree with their OWN tuple and only their own. Two distinct persisted
        contexts, one process, one gate.

        round-N+1 CORRECTION, file-backed SQLite only: this file's own gates
        (``CONTEXT_GATE``/``LIFECYCLE_GATE``) are keyed per-user, so Alice's
        exclusive hold never waits on Bob's ticket THERE — that half of the
        claim is unchanged, on every backend. But for FILE-BACKED SQLite
        specifically, Bob's held read ALSO now holds a REAL SQLite file lock
        for its whole parked duration (``_read_under_context_gate_sqlite``'s
        own independent connection — see that method's own docstring), and
        SQLite's native locking is FILE-granular: it has no primitive
        analogous to the gate's or PostgreSQL's advisory locks' per-user
        keying, so it cannot distinguish "Alice's write" from "Bob's read"
        the way either of those can. An unrelated operator's write CAN
        therefore now be briefly delayed — and, if the contention outlasts
        the guarded write's own bounded retry budget, cleanly REFUSED with a
        retryable 409 rather than silently starved — by another operator's
        held scoped read, on this ONE backend. This is a new, DOCUMENTED
        (not silently absorbed) consequence of using the file's own native
        lock as the durable, engine-level primitive finding 1 asked for, not
        a defect in this test's original claim, which still holds exactly as
        written for Memory and PostgreSQL — both have real per-key
        coordination and never pay this cost.
        """
        # Bounds a genuinely contended SQLite file lock to a small multiple
        # of 1s rather than the 10s default, so this test's own artificial
        # park cannot inflate ANY guarded write's bounded retry budget into
        # a multi-minute wait — see `SqlStore.transaction`'s SQLite branch
        # for why this must be set (or changed) before the write it governs
        # opens its transaction, not merely before the test starts.
        self._set_epoch_fence_timeout_env("1")
        fx = self._program_with_two_seasons("Pair")
        alice_name, alice_id = self._operator("pair_alice")
        bob_name, bob_id = self._operator("pair_bob")
        alice = self._login(alice_name)
        bob_reader = self._login(bob_name)
        bob_switcher = self._login(bob_name)
        self._select(alice, fx["program_id"], fx["s1"])
        self._select(bob_reader, fx["program_id"], fx["s1"])

        is_sqlite_file = bool(self.STORE_URL) and not self.STORE_URL.startswith(
            ("postgres://", "postgresql://"))

        with self._read_parked_in("get_venue_grant_candidates", fx["s1"]) as (
                park, _exited):
            bob_read = {}

            def do_read():
                bob_read["result"] = self._read(bob_reader, fx["s1"])

            rt = threading.Thread(target=do_read, daemon=True)
            rt.start()
            self.assertTrue(park.arrived.wait(PATIENCE),
                            "Bob's read never reached the server")

            # ALICE switches while Bob's read is held. Always on a thread —
            # not inline — because on the SQLite-file branch below she may
            # genuinely have to wait for Bob's read to release before her
            # own guarded write's retry budget lets her proceed, and this
            # test must not deadlock itself waiting for her synchronously
            # while ALSO being the thing that eventually releases Bob.
            started = time.monotonic()
            alice_switch = {}
            ast = self._switch_thread(alice, fx["program_id"], fx["s3"],
                                      alice_switch)
            if not is_sqlite_file:
                # Memory/PostgreSQL: her switch is not Bob's read's business
                # AT ALL (no file lock, no shared advisory-lock key) — it
                # must commit, and promptly.
                ast.join(self._gate().wait_timeout / 2)
                alice_elapsed = time.monotonic() - started
                self.assertFalse(
                    ast.is_alive(),
                    f"Alice's switch waited more than "
                    f"{self._gate().wait_timeout / 2:.2f}s behind ANOTHER "
                    f"operator's held scoped read")
                self.assertEqual(alice_switch["result"][0], 200,
                                 alice_switch["result"][1])

            # BOB's switch, by contrast, IS his read's business.
            bob_switch = {}
            bst = self._switch_thread(bob_switcher, fx["program_id"],
                                      fx["s2"], bob_switch)
            time.sleep(COMMIT_WINDOW)
            self.assertEqual(
                self._persisted(bob_id), (fx["program_id"], fx["s1"]),
                "Bob's switch committed while Bob's read was inside the "
                "server")
            if not is_sqlite_file:
                self.assertEqual(
                    self._persisted(alice_id), (fx["program_id"], fx["s3"]),
                    "Alice's committed tuple did not survive Bob's quiesce")
            park.let_go()
            rt.join(PATIENCE)
            bst.join(PATIENCE)
            if is_sqlite_file:
                # Released now, not before: Alice's retries could only ever
                # succeed once Bob's read (and its file lock) is gone.
                ast.join(PATIENCE)
                self.assertFalse(
                    ast.is_alive(),
                    "Alice's switch never completed even once Bob's read "
                    "released — the contended file lock left it wedged, "
                    "not merely delayed")
                self.assertIn(
                    alice_switch["result"][0], (200, 409),
                    f"Alice's switch against a contended SQLite file lock "
                    f"must resolve cleanly, one way or the other: "
                    f"{alice_switch['result'][1]}")

        self.assertEqual(bob_read["result"][0], 200, bob_read["result"][1])
        self.assertEqual(bob_switch["result"][0], 200, bob_switch["result"][1])
        if alice_switch["result"][0] == 200:
            self.assertEqual(self._persisted(alice_id),
                             (fx["program_id"], fx["s3"]))
            self._assert_reads_agree_with(alice, fx, fx["s3"])
        else:
            # Only reachable on the SQLite-file branch (Memory/PostgreSQL
            # already asserted 200 above): a cleanly-refused write must
            # leave Alice's PRE-race tuple untouched, never half-applied.
            self.assertEqual(self._persisted(alice_id),
                             (fx["program_id"], fx["s1"]))
        self.assertEqual(self._persisted(bob_id), (fx["program_id"], fx["s2"]))
        self._assert_reads_agree_with(bob_reader, fx, fx["s2"])
        self._assert_gate_is_clean("after two operators interleaved")

    def test_control_the_park_seam_is_inert_when_unarmed(self):
        """ANTI-VACUITY for the whole file: with no seam installed, the same
        read and the same switch both complete promptly and correctly, so a
        green race above is the gate and not an accidentally serialized
        server.

        THE THRESHOLD IS RELATIVE TO THE BOUND, not to ``PATIENCE`` (#159
        review). ``PATIENCE / 2`` is 10.0 and the default bound is also 10.0, so
        the two were numerically EQUAL and this control only caught an
        always-blocking gate because one full bound plus HTTP overhead happened
        to exceed it. Under ``HS_CONTEXT_GATE_TIMEOUT=3`` — a documented,
        supported knob, and the whole module is ``Ran 54 tests / OK`` under it —
        a gate whose every wait predicate returned ``True`` finished this case
        in 3.5s and the control said OK.

        Measured, not assumed, about the smaller end of that knob: at ``=1`` the
        module is NOT green (15-16 of 54 fail across the three backends, with 28
        ``proceeding UNORDERED`` notices). That is the bound working as designed
        — 1s is shorter than the seams these tests deliberately park reads
        behind — not a defect, but it means ``=1`` is no evidence for anything
        here and is not claimed as such. The repair does not need it: M8 kills
        the repaired control at the default bound and at ``=3``. It is the only
        absolute timing threshold in the file; every other one (the bounded-wait
        cases, the leak recovery) is already expressed against
        ``wait_timeout``, and so is this now.
        """
        fx = self._program_with_two_seasons("Inert")
        username, user_id = self._operator("inert")
        c = self._login(username)
        self._select(c, fx["program_id"], fx["s1"])
        started = time.monotonic()
        status, raw, _ = self._req(
            c, "GET", f"/api/v2/setup/seasons/{fx['s1']}/venue-candidates")
        self.assertEqual(status, 200, raw)
        self._select(c, fx["program_id"], fx["s2"])
        elapsed = time.monotonic() - started
        ceiling = min(PATIENCE / 2, self._gate().wait_timeout / 2)
        self.assertLess(
            elapsed, ceiling,
            f"an UNRACED read and an UNRACED switch together took "
            f"{elapsed:.2f}s against a {self._gate().wait_timeout}s bound. "
            f"Nothing here should wait at all, so this is a gate that blocks "
            f"where it should not — and every green race in this file above "
            f"would then be an accidentally serialized server rather than the "
            f"ordering it claims to measure")
        self.assertEqual(self._persisted(user_id), (fx["program_id"], fx["s2"]))


class _RaisingStderr:
    """A stderr whose write FAILS — the non-blocking pipe whose buffer is full,
    which is what a container's log collector produces when it stops reading."""

    def write(self, _text):
        raise BlockingIOError(11, "Resource temporarily unavailable")

    def flush(self):
        pass


class _WedgedStderr:
    """A stderr whose write BLOCKS — the same collector stalled rather than
    gone. Records that it was entered so a case can observe the wedge instead
    of sleeping and hoping."""

    def __init__(self):
        self.entered = threading.Event()
        self.release = threading.Event()

    def write(self, text):
        if "[context-gate]" in text:
            self.entered.set()
            self.release.wait(PATIENCE)
        return len(text)

    def flush(self):
        pass

    def let_go(self):
        self.release.set()


class ContextGateInternalsTest(unittest.TestCase):
    """The gate's OWN failure modes, driven directly rather than through HTTP.

    Deliberately not parameterized over the three stores: ``ContextSwitchGate``
    is pure ``threading`` and touches no store at all — the same property that
    lets the HTTP cases above behave identically on all three. Driving it
    directly is also the only way these cases are constructible: an exception
    out of the bounded wait, and a stderr that has stopped accepting writes,
    are reachable through HTTP only by luck.

    What they defend is narrow and specific: the gate's failure handling must
    not itself be a failure. A wait that raises must not leave a registration
    behind, and the line that ANNOUNCES a degraded wait must not be able to
    take the whole gate down with it.
    """

    def _stderr(self, replacement):
        original = sys.stderr
        sys.stderr = replacement
        self.addCleanup(setattr, sys, "stderr", original)
        return replacement

    # -- D2: a wait that raises must not leak the registration -------------
    def test_a_writer_whose_wait_raises_leaves_no_registration_behind(self):
        """``HS_CONTEXT_GATE_TIMEOUT='inf'`` is ACCEPTED by the module's own
        parser — it is a positive float — and makes ``Condition.wait_for``
        raise ``OverflowError`` on every platform whose ``time_t`` cannot hold
        it. The raise lands inside the bounded wait: the one window where a
        writer is already in ``self._writers`` but the ``finally`` that removes
        it has not been entered.

        A writer leaked there is leaked for the LIFE OF THE PROCESS, and every
        later switch AND every later scoped read for that user then waits the
        full bound behind a participant that will never finish. The recovery
        assertion is the one that matters: it is not enough for the counter to
        read zero, a subsequent switch must actually be prompt.
        """
        gate = ContextSwitchGate(wait_timeout=float("inf"))
        arrival = gate.arrive()   # an unbound arrival, so the writer really waits
        with self.assertRaises(OverflowError):
            with gate.exclusive("user_a"):
                self.fail("the bounded wait raised; the body must not run")
        self.assertEqual(
            gate.stats()["writers"], 0,
            f"the writer's registration outlived the exception that killed "
            f"its wait: {gate.stats()}")

        # ...and the gate still WORKS, which a counter alone does not say. The
        # arrival above is released first so that what the recovery switch is
        # measured against is the LEAK and nothing else — an outstanding
        # arrival is a legitimate reason to wait, and leaving it registered
        # would make this step pass or fail for the wrong reason.
        arrival.release()
        gate.wait_timeout = 0.5
        started = time.monotonic()
        with gate.exclusive("user_a"):
            pass
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, gate.wait_timeout,
                        f"a later switch waited {elapsed:.2f}s behind the "
                        f"leaked writer")
        self.assertEqual(gate.stats()["writers"], 0, gate.stats())
        self.assertEqual(gate.stats()["timeouts"], 0,
                         f"the recovery switch WAITED rather than proceeding "
                         f"straight through: {gate.stats()}")

    def test_a_writer_whose_expiry_notice_raises_leaves_nothing_behind(self):
        """THE WEDGE HANDLER MUST NOT BE A WEDGE. The expiry notice is the one
        piece of I/O this module performs, and it is performed precisely when
        things are already going wrong. On a full non-blocking stderr pipe the
        write raises — and at that moment the writer is registered.
        """
        gate = ContextSwitchGate(wait_timeout=0.05)
        gate.arrive()                        # forces the wait to reach expiry
        self._stderr(_RaisingStderr())
        with self.assertRaises(BlockingIOError):
            with gate.exclusive("user_a"):
                self.fail("the expiry notice raised; the body must not run")
        self.assertEqual(
            gate.stats()["writers"], 0,
            f"a failed stderr write left a permanent writer registration: "
            f"{gate.stats()}")

    def test_a_reader_whose_wait_raises_leaves_no_registration_behind(self):
        """The reader half of the same invariant. ``do_GET`` does wrap the
        arrival ticket in an outer ``finally``, so this is today a belt on top
        of braces — but a registration whose removal depends on a CALLER's
        discipline is one refactor away from the writer's bug, and the writer
        had no such caller."""
        gate = ContextSwitchGate(wait_timeout=float("inf"))
        with gate.exclusive("user_a"):       # a switch the reader must wait for
            ticket = gate.arrive()
            with self.assertRaises(OverflowError):
                with ticket.bind("user_a"):
                    self.fail("the bounded wait raised; the body must not run")
            self.assertEqual(
                gate.stats()["readers"], 0,
                f"the reader's arrival ticket outlived the exception that "
                f"killed its wait: {gate.stats()}")

    # -- D3: the expiry notice must not be emitted under the gate mutex ----
    def test_a_wedged_stderr_cannot_freeze_every_other_gate_operation(self):
        """``_await`` runs with ``self._cv`` held — the single process-global
        gate mutex. Doing blocking I/O there means one stalled stderr freezes
        EVERY gate operation, including ``arrive()``, which is the FIRST
        statement ``do_GET`` runs for every scoped read. The whole server stops
        answering scoped reads because one ``print`` is waiting on a pipe.

        The observation is direct: with the notice wedged mid-write, a brand
        new arrival must still register. `arrive()` never blocks BY DESIGN —
        that is its entire docstring — so any wait here is the mutex, not the
        gate's logic.
        """
        gate = ContextSwitchGate(wait_timeout=0.05)
        gate.arrive()                        # forces the wait to reach expiry
        wedged = self._stderr(_WedgedStderr())
        self.addCleanup(wedged.let_go)

        expired = threading.Event()

        def switch():
            with gate.exclusive("user_a"):
                pass
            expired.set()

        t = threading.Thread(target=switch, daemon=True)
        t.start()
        self.assertTrue(wedged.entered.wait(PATIENCE),
                        "the expiry notice never reached stderr, so this case "
                        "never wedged anything")

        arrived = threading.Event()

        def new_scoped_read():
            gate.arrive()
            arrived.set()

        threading.Thread(target=new_scoped_read, daemon=True).start()
        self.assertTrue(
            arrived.wait(2.0),
            "arrive() did not complete in 2.00s while the expiry notice was "
            "wedged on stderr — every scoped read on the server is frozen "
            "behind one print(), and arrive() is documented never to block")

        wedged.let_go()
        t.join(PATIENCE)
        self.assertTrue(expired.is_set(), "the switch never completed")

    def test_the_expiry_is_still_said_out_loud_exactly_once(self):
        """Moving the notice out from under the lock must not lose it, and must
        not double it: one expiry, one line, and the machine-readable counter
        agreeing with the human-readable one."""
        gate = ContextSwitchGate(wait_timeout=0.05)
        gate.arrive()
        written = []

        class _Capture:
            def write(self, text):
                written.append(text)
                return len(text)

            def flush(self):
                pass

        self._stderr(_Capture())
        with gate.exclusive("user_a"):
            pass
        sys.stderr = sys.__stderr__          # so a failure below is visible

        said = [t for t in written if "[context-gate]" in t]
        self.assertEqual(len(said), 1,
                         f"the expiry was said {len(said)} times: {written}")
        self.assertIn("switch", said[0],
                      f"the notice lost which side expired: {said[0]}")
        self.assertEqual(gate.stats()["timeouts"], 1, gate.stats())

    def test_a_reader_expiry_is_said_out_loud_too(self):
        """The reader side has its own emit path now that the notice travels
        back out of ``_await``; an expiry that is only COUNTED is the silent
        failure mode this module's docstring rules out by name."""
        gate = ContextSwitchGate(wait_timeout=0.05)
        written = []

        class _Capture:
            def write(self, text):
                written.append(text)
                return len(text)

            def flush(self):
                pass

        with gate.exclusive("user_a"):       # a switch the reader must wait for
            ticket = gate.arrive()
            self._stderr(_Capture())
            with ticket.bind("user_a"):
                pass
            sys.stderr = sys.__stderr__

        said = [t for t in written if "[context-gate]" in t]
        self.assertEqual(len(said), 1,
                         f"the reader's expiry was said {len(said)} times: "
                         f"{written}")
        self.assertIn("scoped read", said[0],
                      f"the notice lost which side expired: {said[0]}")
        self.assertEqual(gate.stats()["timeouts"], 1, gate.stats())


class MemoryContextSwitchServerExitTest(
        ContextSwitchServerExitBase, unittest.TestCase):
    STORE_URL = None


class SqliteContextSwitchServerExitTest(
        ContextSwitchServerExitBase, unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fd, path = tempfile.mkstemp(suffix=".db", prefix="hs_ctxgate_")
        os.close(fd)
        cls._db_path = path
        cls.STORE_URL = path
        super().setUpClass()

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        try:
            os.unlink(cls._db_path)
        except OSError:
            pass


@unittest.skipUnless(os.environ.get("TEST_DATABASE_URL"),
                     "PostgreSQL not configured (TEST_DATABASE_URL)")
class PostgresContextSwitchServerExitTest(
        ContextSwitchServerExitBase, unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.STORE_URL = os.environ["TEST_DATABASE_URL"]
        super().setUpClass()
