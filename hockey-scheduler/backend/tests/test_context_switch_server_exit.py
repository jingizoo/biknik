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
  2. ``test_a_switch_cancelled_while_waiting_leaks_nothing``
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
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
import uuid
from contextlib import contextmanager
from http.cookiejar import CookieJar
from http.server import ThreadingHTTPServer

from helpers import BACKEND  # noqa: F401  (ensures sys.path is set up)

from hockey_scheduler.domain import Role

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


class ContextSwitchServerExitBase:
    """Shared fixture. Subclasses set ``STORE_URL`` (None => InMemoryStore)."""

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
        production module is patched and the undo is a plain ``delattr``."""
        original = getattr(obj, name)
        setattr(obj, name, wrapper_factory(original))
        self._restores.append(lambda: (delattr(obj, name)
                                       if name in vars(obj) else None))
        return original

    # -- HTTP ---------------------------------------------------------------
    def _client(self):
        return urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(CookieJar()))

    def _req(self, opener, method, path, body=None, timeout=PATIENCE):
        url = f"http://127.0.0.1:{self.port}{path}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        if data is not None:
            req.add_header("Content-Type", "application/json")
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

    def _persisted(self, user_id):
        ctx = self.api.store.get_active_context(user_id)
        return (None, None) if ctx is None else (ctx.program_id, ctx.season_id)

    def _gate(self):
        return self.srv.CONTEXT_GATE

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

          * A SWITCH waiting on that read completes anyway, in roughly
            ``wait_timeout``, and ``stats()["timeouts"]`` goes up — a wedged
            read cannot lock an operator out of switching context.
          * A LATER scoped read waiting on a switch that never finishes is
            bounded the same way, and answers rather than hanging.

        The gate is left empty once the parked participants finally exit, so
        hitting the bound is a HANDLED outcome and not a corrupted one.
        """
        gate = self._gate()
        original_timeout = gate.wait_timeout
        gate.wait_timeout = 0.4
        self.addCleanup(setattr, gate, "wait_timeout", original_timeout)

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

    def test_control_the_park_seam_is_inert_when_unarmed(self):
        """ANTI-VACUITY for the whole file: with no seam installed, the same
        read and the same switch both complete promptly and correctly, so a
        green race above is the gate and not an accidentally serialized
        server."""
        fx = self._program_with_two_seasons("Inert")
        username, user_id = self._operator("inert")
        c = self._login(username)
        self._select(c, fx["program_id"], fx["s1"])
        started = time.monotonic()
        status, raw, _ = self._req(
            c, "GET", f"/api/v2/setup/seasons/{fx['s1']}/venue-candidates")
        self.assertEqual(status, 200, raw)
        self._select(c, fx["program_id"], fx["s2"])
        self.assertLess(time.monotonic() - started, PATIENCE / 2)
        self.assertEqual(self._persisted(user_id), (fx["program_id"], fx["s2"]))


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
