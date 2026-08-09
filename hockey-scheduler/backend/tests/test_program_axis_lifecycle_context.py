"""The PROGRAM-AXIS half of #409: the two lifecycles a Season-shaped rule breaks.

WHY THIS FILE EXISTS. ``test_explicit_setup_mutation_context.py`` pins the
SEASON-OWNED half of the owner's rule — a mutation on a Season, a LeagueSeason,
a Division or a Game must be aimed at an EXPLICIT persisted Program AND Season.
Applying that same two-axis demand to every mutation looks stricter and is
actually BROKEN: it makes two ordinary operator lifecycles UNSATISFIABLE rather
than merely tighter, because neither of them has a Season to name.

  * **LIFECYCLE A — the last Season is gone.** An operator creates Program P,
    creates its only Season S, selects (P, S), then deletes S while tidying up.
    Their saved row now names a Program that is still CORRECT and a Season that
    is STALE. Deleting P is the obvious next step and it consumes no Season —
    a Program's own link triple is ``(itself, None, None)``. Under a two-axis
    rule there is no Season left to select, so P could never be deleted again.

  * **LIFECYCLE B — the Program has no Season yet.** An operator creates a
    brand-new Program and selects it, then links it to the Organization that
    operates it before building any Season. That is the FIRST thing you do with
    a new Program. Under a two-axis rule it is impossible, because a Season
    cannot exist until the Program is set up.

THE OWNER'S RULE, and the reason both of these must SUCCEED:

    FALLBACK NEVER AUTHORIZES A MUTATION.
    SEASON-OWNED mutations require an explicit persisted Program AND Season.
    PROGRAM-ONLY mutations require an explicit persisted PROGRAM. A missing or
    stale SEASON is IRRELEVANT — that mutation does not consume the Season axis.

So a Program-only mutation is judged by ``_program_selection_is_explicit``,
which reads the SAME saved ``ActiveContext`` row as the two-axis predicate and
asks one axis less of it. Not a softening: the axis table.

WHAT "THE FALLBACK WAS NOT CONSULTED" MEANS HERE, and why it needs its own
assertion. ``ContextService._resolve_locked`` falls through to ``_fallback()``
the moment the saved SEASON stops resolving, and ``_fallback()`` then picks a
Program of its own — in this fixture, HOME A, which the operator has never
seen. A 200 on LIFECYCLE A is therefore only meaningful if we first prove the
fallback really would have retargeted the write:

  1. the READ side (``GET /api/context``) is shown resolving HOME A, not the
     operator's own P — that is literally what ``_fallback()`` picks;
  2. HOME A is asserted to be a DIFFERENT Program from P;
  3. the delete is then shown to have destroyed P and left HOME A untouched.

Step 3 could not happen under the fallback: #369's target gate judges a
``program`` target against the RESOLVED Program, so a delete of P resolved onto
HOME A is refused with a generic not-found. A 200 that removes P is only
reachable if the WRITE side kept the operator's own explicitly saved Program.

THE TWO REFUSALS, for both lifecycles, each asserted to leave ZERO domain
effect and ZERO audit rows:

  * NO saved Program — a brand-new operator who has POSTed no context at all;
  * a saved Program that DIFFERS from the one the locked resolve yields. This
    is the Program-axis form of staleness and it is reachable through ordinary
    calls: the operator selects their own Program and then deletes it, so the
    saved row survives naming a Program that no longer exists and the resolve
    silently lands on HOME A. That divergence is exactly what
    ``_program_selection_is_explicit`` compares, and it is asserted as a
    PREMISE before the refusal is judged, so this case can never quietly
    degrade into a duplicate of the no-selection case above.

A THIRD SHAPE IS RECORDED RATHER THAN ASSUMED. When the saved Program is a
DIFFERENT one that still EXISTS — the operator genuinely chose Program R and
then aimed a mutation at Program P — the answer is NOT this 409. It is #369's
generic 404, because "you have not CHOSEN a context" and "that record is not IN
your context" are two different questions with two different codes, decided in
that order (``ApiService._mutation_context_error`` states this, and pins that
the #409 decision deliberately reads no target row so it can never vary with
whether the supplied id exists). The case is asserted here explicitly, with the
same zero-mutation/zero-audit claim, so that boundary is covered rather than
silently omitted.

THREE BACKENDS. ``InMemoryStore``, ``SqlStore`` on SQLite, and real PostgreSQL
whenever ``TEST_DATABASE_URL`` is exported. The decision reads an
``ActiveContext`` row whose ``season_id`` is NULL for a Program-only selection
and compares it to a resolve — a dict lookup on one store and a real query with
real three-valued NULL semantics on the others — so agreement between them is
part of the claim, not a formality. A SKIP IS NEVER A PASS: the PostgreSQL
classes below are only defined when the URL is set, and their absence is
reported by the module-level notice so an unconfigured run cannot read as a
green one.
"""

import contextlib
import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
import uuid
from http.cookiejar import CookieJar
from http.server import ThreadingHTTPServer

from helpers import BACKEND  # noqa: F401  (ensures sys.path is set up)

from hockey_scheduler.domain import Role
from hockey_scheduler.services.context_service import ContextService
from hockey_scheduler.web import server as srv

TZ = "America/Toronto"

_HTTPD = None
_THREAD = None
_PORT = None
_TMP_FILES = []
_SAVED_DATABASE_URL = None


def setUpModule():
    global _HTTPD, _THREAD, _PORT, _SAVED_DATABASE_URL
    _SAVED_DATABASE_URL = os.environ.get("DATABASE_URL")
    _HTTPD = ThreadingHTTPServer(("127.0.0.1", 0), srv.Handler)
    _PORT = _HTTPD.server_address[1]
    _THREAD = threading.Thread(target=_HTTPD.serve_forever, daemon=True)
    _THREAD.start()
    if not os.environ.get("TEST_DATABASE_URL"):
        print("[test_program_axis_lifecycle_context] PostgreSQL not configured "
              "(TEST_DATABASE_URL) — the Program-only lifecycles were NOT "
              "exercised against real NULL semantics. A SKIP IS NOT A PASS.",
              file=sys.stderr)


def tearDownModule():
    if _HTTPD is not None:
        _HTTPD.shutdown()
        _THREAD.join(timeout=5)
        _HTTPD.server_close()
    if _SAVED_DATABASE_URL is None:
        os.environ.pop("DATABASE_URL", None)
    else:
        os.environ["DATABASE_URL"] = _SAVED_DATABASE_URL
    try:
        srv.STATE.reset(seed=False)   # leave the shared global on a clean store
    except Exception:
        pass
    for path in _TMP_FILES:
        if os.path.exists(path):
            os.remove(path)


def _sqlite_url():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    _TMP_FILES.append(path)
    return path


class _ProgramAxisHarness:
    """Shared fixture + harness, parameterised on ``DATABASE_URL``.

    ``setUp`` rebuilds the shared ``srv.STATE`` on the chosen backend for EVERY
    test, for the same reason the sibling module does: what ``_fallback()``
    picks depends on which Programs and active Seasons exist, so a world
    inherited from a previous case would make "the fallback picks HOME A" an
    accident of execution order rather than a controlled fixture.
    """

    DATABASE_URL = None       # None -> InMemoryStore

    # -- harness -----------------------------------------------------------
    def setUp(self):
        if self.DATABASE_URL is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = self.DATABASE_URL
        srv.STATE.reset(seed=False)      # clean slate on the selected backend
        self.store = srv.STATE.api.store
        self.world = self._world()

    def _client(self):
        return urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(CookieJar()))

    def _req(self, opener, method, path, body=None):
        """``(status, raw_text, parsed)``. The RAW body is kept because a
        refusal must not name another Program's records."""
        url = f"http://127.0.0.1:{_PORT}{path}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with opener.open(req) as r:
                raw = r.read()
                return r.status, raw.decode(), json.loads(raw or b"{}")
        except urllib.error.HTTPError as e:
            raw = e.read()
            e.close()      # keep the run free of ResourceWarning noise
            return e.code, raw.decode(), json.loads(raw or b"{}")

    def _operator(self):
        """A BRAND-NEW League Admin with NO persisted ActiveContext, signed in
        over real HTTP with a real session cookie. A fresh identity per case
        makes "nothing is selected" a property of the account rather than of
        execution order."""
        username = f"op_{uuid.uuid4().hex[:10]}"
        account = srv.STATE.api.accounts.create_account(
            username, "demo", Role.LEAGUE_ADMIN)
        c = self._client()
        status, raw, _ = self._req(c, "POST", "/api/auth/login",
                                   {"username": username, "password": "demo"})
        self.assertEqual(status, 200, raw)
        self.assertIsNone(
            self.store.get_active_context(account.id),
            "the fixture operator already has a saved selection — every "
            "'no selection' assertion below would be vacuous")
        return c, account.id

    # -- fixture -----------------------------------------------------------
    def _world(self):
        """HOME A (Program + active Season + League + Division) — what
        ``_fallback()`` will pick for an operator who has chosen nothing — plus
        EMPTY B, a second Program with NO Season of its own.

        EMPTY B is the target of every refusal case. It has to be Season-less
        so that a refusal can never be confused with a Season-owned verdict,
        and it has to be built through the service rather than over HTTP
        because it is the FIXTURE, not the surface under test: nobody has
        selected it, which is precisely the state being probed.
        """
        svc = srv.STATE.api.setup
        program = svc.create_program("Home A", timezone_name=TZ)
        season = svc.create_season(program.id, "Home A Season")
        league = svc.create_league(season.id, "Home A League")
        svc.create_division(season.id, "Home A U16", league_id=league.id)
        empty = svc.create_program("Empty B", timezone_name=TZ)
        return {"program_id": program.id, "season_id": season.id,
                "empty_program_id": empty.id}

    # -- observation -------------------------------------------------------
    def _audit(self):
        return len(self.store.all_setup_audit())

    def _fallback_tuple(self, c):
        """What the READ side resolves for this session. Read-only navigation
        keeps the deterministic fallback by the owner's ruling, so this is a
        direct, honest readout of what ``_fallback()`` would pick — which is
        what makes "the fallback was not consulted" a checkable claim rather
        than an inference."""
        status, raw, body = self._req(c, "GET", "/api/context")
        self.assertEqual(status, 200, raw)
        return body.get("program_id"), body.get("season_id")

    # -- SENTINELS ---------------------------------------------------------
    # Both of these exist because the claims they back CANNOT be made from
    # return values. "The fallback was not consulted" and "the target was not
    # looked up" are statements about what the code DID, and a value that
    # happens to match is not evidence about its provenance — which is the
    # exact confusion #409's owner tightening exists to remove. So they are
    # instrumented directly, and instrumented to FAIL LOUDLY: the spy raises at
    # the call site (turning a violation into an immediate, located failure
    # inside the request) AND records, so the zero-call claim is also asserted
    # explicitly afterwards and cannot be lost if a handler swallows the raise.

    @contextlib.contextmanager
    def _fallback_must_not_be_consulted(self, why):
        """``ContextService._fallback`` must not run for the duration.

        ``_fallback()`` is the auto-selector: it walks the caller's authorized
        Programs in id order and hands back one they never chose. It is the
        owner's ruling for read-only landing, and it must NEVER authorize a
        mutation. Asserting that from the outside is impossible — on this
        fixture the fallback would pick HOME A, but on an installation with a
        single Program it would pick the very Program the operator saved, and a
        200 would look identical either way. A COINCIDENTAL MATCH WOULD BE A
        FALSE PROOF, so the call itself is what is measured.

        Patched on the CLASS, so it covers whichever ``ContextService`` the
        request thread reaches, and restored in ``finally`` so a failure inside
        the block cannot leak the spy into another test.
        """
        calls = []
        original = ContextService._fallback

        def spy(ctx_self, role, scope, user_id, programs):
            calls.append({"role": role, "user_id": user_id,
                          "programs": sorted(programs or ())})
            raise AssertionError(
                f"#409: ContextService._fallback() was CONSULTED during "
                f"{why}. A mutation may never be authorized by a context the "
                f"operator did not choose.")

        ContextService._fallback = spy
        try:
            yield calls
        finally:
            ContextService._fallback = original
        self.assertEqual(
            calls, [],
            f"ContextService._fallback() ran {len(calls)} time(s) during "
            f"{why} — the write path consulted the auto-selector, so a 200 "
            f"here proves nothing about WHICH Program the mutation used: "
            f"{calls}")

    @contextlib.contextmanager
    def _count_saved_row_reads(self):
        """Count the two ways the saved ``ActiveContext`` row can be read.

        Yields a dict with ``plain`` (``get_active_context``, no lock) and
        ``locked`` (``get_active_context_for_update``, ``SELECT ... FOR
        UPDATE`` on the SQL stores). The #409 contract is TWO-PHASE and the
        phases are not interchangeable: the preflight decides the refusal
        before any target is disclosed, and the locked check re-decides it
        inside the writing transaction. Each must READ THE ROW ITSELF —
        carrying the preflight's snapshot into the transaction would reopen
        exactly the check/use window the lock exists to close.

        Counting the calls is the only way to tell a re-read from a reused
        value, because both produce the same answer whenever nothing raced.
        """
        counts = {"plain": 0, "locked": 0}
        real_plain = self.store.get_active_context
        real_locked = self.store.get_active_context_for_update

        def plain(user_id, *a, **kw):
            counts["plain"] += 1
            return real_plain(user_id, *a, **kw)

        def locked(user_id, *a, **kw):
            counts["locked"] += 1
            return real_locked(user_id, *a, **kw)

        self.store.get_active_context = plain
        self.store.get_active_context_for_update = locked
        try:
            yield counts
        finally:
            del self.store.get_active_context
            del self.store.get_active_context_for_update

    @contextlib.contextmanager
    def _target_lookup_must_not_run(self, *record_ids):
        """``store.get_program`` must not be called with any of ``record_ids``.

        The #409 refusal is answered BEFORE TARGET DISCLOSURE, and that is a
        claim about ordering, not about wording: if the gate read the target
        row first, the refusal could vary with whether the id exists and the
        409 would be an existence oracle for records the caller may not see.
        Equal response bytes alone do not establish it — a lookup whose result
        is discarded is still a lookup, and the next refactor makes it
        observable. So the lookup is spied.

        Only the PROBE ids raise: the same method legitimately serves the
        caller's own saved Program elsewhere in a request, and gagging that
        would measure something other than target disclosure.
        """
        probes = set(record_ids)
        seen = []
        original = self.store.get_program

        def spy(program_id, *args, **kwargs):
            seen.append(program_id)
            if program_id in probes:
                raise AssertionError(
                    f"#409: the TARGET {program_id!r} was looked up before "
                    f"the refusal was decided — the 409 is disclosing "
                    f"whether the id exists.")
            return original(program_id, *args, **kwargs)

        self.store.get_program = spy
        try:
            yield seen
        finally:
            del self.store.get_program
        touched = [rid for rid in seen if rid in probes]
        self.assertEqual(
            touched, [],
            f"the pre-disclosure refusal looked the target up: {touched}")

    # -- reachable recipes -------------------------------------------------
    def _new_program(self, c, name):
        status, raw, body = self._req(
            c, "POST", "/api/v2/setup/program", {"name": name, "timezone": TZ})
        self.assertEqual(status, 200, f"create {name}: {raw}")
        return body["id"]

    def _select(self, c, program_id, season_id=None):
        payload = {"program_id": program_id}
        if season_id is not None:
            payload["season_id"] = season_id
        status, raw, body = self._req(c, "POST", "/api/context", payload)
        self.assertEqual(status, 200, f"select {payload}: {raw}")
        self.assertEqual(body.get("program_id"), program_id, raw)
        self.assertEqual(body.get("season_id"), season_id, raw)

    def _stale_program_selection(self, c, uid):
        """THE reachable recipe for "the saved Program is a DIFFERENT one from
        the locked resolve", in three ordinary authenticated calls:

            POST /api/v2/setup/program            -> 200  the operator's own Q
            POST /api/context {program_id: Q}     -> 200  the saved row names Q
            POST /api/v2/setup/program/Q/delete   -> 200  Q ceases to exist

        The third call is itself a LIFECYCLE B-shaped Program-only mutation, so
        it is only accepted because the rule under test is right — which makes
        this recipe a second, independent exercise of the positive case as well
        as the setup for the negative one.

        Afterwards the saved row survives naming a Program that is gone, so
        ``_setup_gate_tuple`` has no explicit choice left to honour and the
        resolve lands on HOME A instead. Every part of that is asserted here,
        because each can fail independently and each failure would turn the
        case that follows into a weaker test.
        """
        q = self._new_program(c, "Operator Q")
        self._select(c, q)
        status, raw, _ = self._req(
            c, "POST", f"/api/v2/setup/program/{q}/delete", {})
        self.assertEqual(
            status, 200,
            f"the operator could not delete the Program they had just "
            f"explicitly selected, so this recipe never reaches the stale "
            f"state it exists to build: {raw}")

        saved = self.store.get_active_context(uid)
        self.assertIsNotNone(
            saved,
            "the saved selection disappeared when its Program was deleted, so "
            "there is no stale row left and this case would silently become a "
            "duplicate of the no-selection case")
        self.assertEqual(
            saved.program_id, q,
            "the saved row no longer names the deleted Program — it was "
            "rewritten, so what follows is not a stale-selection test")
        self.assertIsNone(
            self.store.get_program(q),
            "the deleted Program still exists, so the saved row is not "
            "dangling and this case proves nothing")

        resolved_program, _resolved_season = self._fallback_tuple(c)
        self.assertEqual(
            resolved_program, self.world["program_id"],
            "the stale saved row did not fall back onto HOME A, so this case "
            "is not exercising the silent retarget it is about")
        self.assertNotEqual(
            resolved_program, saved.program_id,
            "the resolve still returns the operator's own saved Program, so "
            "saved and resolved AGREE and the selection is not stale")
        return q

    # -- shared assertion ---------------------------------------------------
    def _assert_refused(self, label, status, raw, *, before_audit,
                        expect_code="active_context_required",
                        expect_status=409):
        report = f"\n  case   : {label}\n  reply  : {status} {raw}"
        # (1) THE claim, first: nothing may have happened. The caller has
        #     already captured whatever domain state this mutation would have
        #     changed; here we pin the audit trail, which every guarded
        #     mutation writes and a rolled-back one must not.
        self.assertEqual(
            self._audit(), before_audit,
            f"a refused mutation still wrote a setup-audit row.{report}")
        self.assertNotEqual(status, 200,
                            f"the mutation was accepted.{report}")
        # (2) The WIRE CONTRACT.
        self.assertEqual(status, expect_status, f"unexpected status.{report}")
        body = json.loads(raw or "{}")
        self.assertEqual(body.get("error", {}).get("code"), expect_code,
                         f"unexpected error code.{report}")


class _LifecycleAMixin(_ProgramAxisHarness):
    """LIFECYCLE A — delete a Program's LAST Season, then delete the Program."""

    def _reach_lifecycle_a(self, c, uid):
        """Program P with its only Season S, both selected, then S deleted.
        Returns ``(p, s)``. Every call is one an operator makes in the UI."""
        p = self._new_program(c, "Operator P")
        status, raw, body = self._req(
            c, "POST", "/api/v2/setup/season",
            {"program_id": p, "name": "Operator P Season"})
        self.assertEqual(status, 200, f"create Season S: {raw}")
        s = body["id"]
        self._select(c, p, s)
        status, raw, _ = self._req(
            c, "POST", f"/api/v2/setup/season/{s}/delete", {})
        self.assertEqual(
            status, 200,
            f"the operator could not delete their own fresh, EMPTY Season, so "
            f"the stale state this case is about is never reached: {raw}")
        self.assertIsNone(
            self.store.get_season(s),
            "the Season survived its own delete — the saved selection is not "
            "stale on the Season axis and this proves nothing")
        return p, s

    def test_program_delete_succeeds_with_a_correct_program_and_a_stale_season(self):
        """The lifecycle itself. The saved row's PROGRAM axis is still correct
        and its SEASON axis dangles; the delete consumes only the Program axis,
        so it must succeed — and it must succeed by honouring the saved
        Program rather than by falling back onto HOME A."""
        c, uid = self._operator()
        p, s = self._reach_lifecycle_a(c, uid)

        # -- THE PREMISE, asserted before the mutation is sent --------------
        saved = self.store.get_active_context(uid)
        self.assertIsNotNone(saved, "the saved selection vanished with its "
                                    "Season; there is no lifecycle left")
        self.assertEqual(
            saved.program_id, p,
            "the saved row's PROGRAM axis is not still correct, so this case "
            "is not the 'correct Program, stale Season' lifecycle at all")
        self.assertEqual(
            saved.season_id, s,
            "the saved row's SEASON axis no longer names the deleted Season, "
            "so it is not STALE and nothing here is being exercised")
        self.assertIsNone(
            self.store.get_season(saved.season_id),
            "the Season the saved row names still exists — not stale")

        # ...and what the FALLBACK would pick instead, read off the read side
        # that still honours it. This is the value the write must NOT use.
        fallback_program, fallback_season = self._fallback_tuple(c)
        self.assertEqual(
            (fallback_program, fallback_season),
            (self.world["program_id"], self.world["season_id"]),
            "the stale row did not fall back onto HOME A, so 'the fallback "
            "was not consulted' is not a claim this case can make")
        self.assertNotEqual(
            fallback_program, saved.program_id,
            "the fallback picked the operator's OWN Program, so a 200 below "
            "would prove nothing about which one the write used")

        # -- THE MUTATION ---------------------------------------------------
        # Instrumented, not inferred: the delete must complete WITHOUT
        # `_fallback()` running at all. See `_fallback_must_not_be_consulted`
        # for why a returned value cannot carry this claim.
        before_audit = self._audit()
        with self._fallback_must_not_be_consulted(
                "the Program-only delete of a Program whose last Season is "
                "gone"):
            status, raw, body = self._req(
                c, "POST", f"/api/v2/setup/program/{p}/delete", {})
        self.assertEqual(
            status, 200,
            f"a Program whose last Season was deleted could not be deleted: "
            f"the Program-only rule is being judged on the Season axis it "
            f"does not consume.{chr(10)}  reply: {raw}")

        # -- THE FALLBACK WAS NOT CONSULTED ---------------------------------
        # The response names the SAVED Program, not the one _fallback() picked.
        self.assertEqual(body.get("id"), p, raw)
        self.assertNotEqual(body.get("id"), fallback_program, raw)
        # And the store agrees: P is gone, HOME A is untouched. This is the
        # decisive half — under the fallback, #369's target gate judges a
        # `program` target against the RESOLVED Program, so a delete of P
        # resolved onto HOME A is refused with a generic not-found. A 200 that
        # actually removed P is unreachable unless the write kept the saved
        # Program.
        self.assertIsNone(self.store.get_program(p),
                          "the delete answered 200 but P still exists")
        self.assertIsNotNone(
            self.store.get_program(fallback_program),
            "the write landed on the Program the FALLBACK picked — the "
            "operator's delete was silently retargeted, which is the whole "
            "defect #409 exists to close")
        self.assertIsNotNone(
            self.store.get_season(self.world["season_id"]),
            "HOME A's Season was touched by a delete aimed at another Program")
        self.assertEqual(self._audit(), before_audit + 1,
                         "expected exactly one audit row for the delete")

    def test_program_delete_with_no_saved_program_is_refused(self):
        """State 1: a brand-new operator has chosen NOTHING. The fallback
        resolves HOME A for them and #369's gate would be satisfied by it, so
        without #409 the delete of a Program they never chose commits."""
        c, _uid = self._operator()
        target = self.world["empty_program_id"]
        before_audit = self._audit()
        status, raw, _ = self._req(
            c, "POST", f"/api/v2/setup/program/{target}/delete", {})
        self.assertIsNotNone(
            self.store.get_program(target),
            f"a Program was DELETED without any explicit persisted "
            f"selection.{chr(10)}  reply: {status} {raw}")
        self._assert_refused("program delete, no saved selection", status, raw,
                             before_audit=before_audit)

    def test_program_delete_with_a_different_saved_program_is_refused(self):
        """State 2: the saved row names a DIFFERENT Program from the one the
        locked resolve yields — the Program-axis form of staleness, reached by
        the operator deleting the very Program they had selected."""
        c, uid = self._operator()
        self._stale_program_selection(c, uid)
        target = self.world["empty_program_id"]
        before_audit = self._audit()
        status, raw, _ = self._req(
            c, "POST", f"/api/v2/setup/program/{target}/delete", {})
        self.assertIsNotNone(
            self.store.get_program(target),
            f"a Program was DELETED under a saved selection that no longer "
            f"resolves to itself.{chr(10)}  reply: {status} {raw}")
        self._assert_refused("program delete, stale saved Program",
                             status, raw, before_audit=before_audit)

    def test_a_live_different_saved_program_is_the_369_not_found(self):
        """THE BOUNDARY, recorded rather than assumed.

        When the operator really HAS chosen a context and simply aims the
        mutation at another Program, the question is no longer "have you chosen
        one" — it is "is that record in it". #409 hands over to #369 and the
        answer is the generic not-found, byte-identical to an id that never
        existed. ``_mutation_context_error`` states this explicitly and pins
        that the #409 decision reads no target row, which is what keeps the
        409 from ever varying with whether the supplied id exists.
        """
        c, _uid = self._operator()
        r = self._new_program(c, "Operator R")
        self._select(c, r)
        target = self.world["empty_program_id"]
        before_audit = self._audit()
        status, raw, _ = self._req(
            c, "POST", f"/api/v2/setup/program/{target}/delete", {})
        self.assertIsNotNone(
            self.store.get_program(target),
            f"a Program outside the caller's chosen context was DELETED."
            f"{chr(10)}  reply: {status} {raw}")
        self._assert_refused(
            "program delete, live different saved Program", status, raw,
            before_audit=before_audit, expect_code="not_found",
            expect_status=404)
        # ...and it is indistinguishable from an id that never existed.
        ghost_status, ghost_raw, _ = self._req(
            c, "POST", "/api/v2/setup/program/program_never_existed/delete", {})
        self.assertEqual(
            (ghost_status, ghost_raw.replace("program_never_existed", target)),
            (status, raw),
            "the inaccessible-Program refusal is distinguishable from the "
            "nonexistent one")


class _LifecycleBMixin(_ProgramAxisHarness):
    """LIFECYCLE B — link a brand-new, Season-less Program to an Organization."""

    def _new_org(self, c, name):
        status, raw, body = self._req(
            c, "POST", "/api/v2/setup/organization", {"name": name})
        self.assertEqual(status, 200, f"create {name}: {raw}")
        return body["id"]

    def _link(self, c, program_id, org_id):
        return self._req(
            c, "POST", f"/api/v2/setup/program/{program_id}/assign-organization",
            {"operator_organization_id": org_id})

    def test_linking_a_season_less_program_to_an_organization_succeeds(self):
        """The lifecycle itself: create a Program, select it (a Program-ONLY
        context — the saved row's ``season_id`` is NULL), and link it to the
        Organization that operates it BEFORE it has any Season."""
        c, uid = self._operator()
        p = self._new_program(c, "Operator B2")
        self._select(c, p)
        org = self._new_org(c, "Operator B2 Org")

        # -- THE PREMISE ----------------------------------------------------
        saved = self.store.get_active_context(uid)
        self.assertIsNotNone(saved, "nothing was persisted by POST /api/context")
        self.assertEqual(saved.program_id, p, "the saved row names another "
                                              "Program than the one selected")
        self.assertFalse(
            saved.season_id,
            "the saved selection carries a Season, so this is not the "
            "Program-ONLY context the lifecycle is about")
        self.assertEqual(
            [s.id for s in self.store.all_seasons() if s.program_id == p], [],
            "the Program already has a Season, so the 'no Season yet' "
            "lifecycle is not being exercised")
        self.assertIsNone(self.store.get_program(p).operator_organization_id,
                          "the Program is already linked; nothing to prove")

        # -- THE MUTATION ---------------------------------------------------
        # Same sentinel as LIFECYCLE A, and it carries more here: the saved row
        # is a Program-ONLY selection, so there is no Season for the resolve to
        # fail on and the fallback is that much easier to reach by accident.
        before_audit = self._audit()
        with self._fallback_must_not_be_consulted(
                "the Program-only link of a Season-less Program to its "
                "Organization"):
            status, raw, body = self._req(c, "POST",
                                          f"/api/v2/setup/program/{p}/"
                                          "assign-organization",
                                          {"operator_organization_id": org})
        self.assertEqual(
            status, 200,
            f"a brand-new Program could not be linked to its Organization "
            f"before it had a Season: the Program-only rule is being judged "
            f"on the Season axis it does not consume.{chr(10)}  reply: {raw}")
        self.assertEqual(body.get("operator_organization_id"), org, raw)
        self.assertEqual(self.store.get_program(p).operator_organization_id,
                         org, "the 200 did not persist the link")
        self.assertGreater(self._audit(), before_audit,
                           "the link wrote no audit row")

    def test_link_with_no_saved_program_is_refused(self):
        c, _uid = self._operator()
        p = self._new_program(c, "Operator B3")
        org = self._new_org(c, "Operator B3 Org")
        # Creating a Program does not select it: the operator still has no
        # persisted choice, which is the state under test.
        self.assertIsNone(self.store.get_active_context(_uid),
                          "creating a Program PERSISTED a selection")
        before_audit = self._audit()
        status, raw, _ = self._link(c, p, org)
        self.assertIsNone(
            self.store.get_program(p).operator_organization_id,
            f"a Program was LINKED without any explicit persisted "
            f"selection.{chr(10)}  reply: {status} {raw}")
        self._assert_refused("program link, no saved selection", status, raw,
                             before_audit=before_audit)

    def test_link_with_a_different_saved_program_is_refused(self):
        c, uid = self._operator()
        self._stale_program_selection(c, uid)
        p = self._new_program(c, "Operator B4")
        org = self._new_org(c, "Operator B4 Org")
        # The saved row still dangles: creating rows does not repair it.
        saved = self.store.get_active_context(uid)
        self.assertIsNone(
            self.store.get_program(saved.program_id),
            "the saved Program came back to life; the premise is gone")
        before_audit = self._audit()
        status, raw, _ = self._link(c, p, org)
        self.assertIsNone(
            self.store.get_program(p).operator_organization_id,
            f"a Program was LINKED under a saved selection that no longer "
            f"resolves to itself.{chr(10)}  reply: {status} {raw}")
        self._assert_refused("program link, stale saved Program", status, raw,
                             before_audit=before_audit)


class _AxisChangesUnderTheLockMixin(_ProgramAxisHarness):
    """LIFECYCLE C — the mutation whose AXIS CLASS CHANGES mid-transaction.

    ``POST /api/v2/setup/team/<id>/assign-league`` names two targets, a Team
    and a League. Both are PROGRAM-AXIS records: a Team's Season lives on its
    registration, and a League is permanent across Seasons. Phase 1 — the
    pre-disclosure refusal — classifies by KIND and reads NO store row at all,
    which is exactly what lets it answer before any target lookup, so phase 1
    structurally CANNOT see anything else.

    Under its locks, though, ``transfer_team_to_league`` scans the Team's
    ACTIVE ``SeasonTeamRegistration`` rows, locks every Season they touch, and
    REWRITES them onto the target League's LeagueSeason for that same Season. A
    registration is a bridge judged by its LeagueSeason, i.e. SEASON-OWNED. So
    at that instant — and only then, and only when such rows exist — the
    operation becomes cross-axis.

    Both ends are asserted, because neither predicate alone is correct:

      * a Team with ZERO active registrations consumes no Season, and a
        Program-only selection must still MOVE it. Demanding a Season at phase
        1 would refuse a legitimate transfer and would force phase 1 to read
        the store, destroying the no-oracle property;
      * a Team WITH an active registration must be refused under that same
        Program-only selection, because accepting it would let a caller rewrite
        registrations in a Season they never chose.

    The refusal is asserted to leave the Team's League unchanged, every
    registration unchanged, and ZERO audit rows — it is RAISED inside the
    guarded transaction, so the whole unit rolls back.
    """

    def _competition(self, program_id, season_id):
        """Two Leagues bound to ``season_id``, and their LeagueSeasons."""
        svc = srv.STATE.api.setup
        home = svc.create_league(season_id, "Lifecycle C Home")
        away = svc.create_league(season_id, "Lifecycle C Away")
        return home.id, away.id

    def _team(self, program_id, league_id, name):
        return srv.STATE.api.setup.create_team(
            name=name, program_id=program_id, league_id=league_id).id

    def _registration_league_ids(self, team_id):
        """``{registration_id: league_season_id}`` for this Team's ACTIVE rows —
        the state a refused transfer must leave byte-identical."""
        return {r.id: (r.league_season_id, r.division_id, r.active)
                for r in self.store.all_season_team_registrations()
                if r.team_id == team_id}

    def _transfer(self, c, team_id, league_id):
        return self._req(
            c, "POST", f"/api/v2/setup/team/{team_id}/assign-league",
            {"league_id": league_id})

    def _own_program_with_season(self, c):
        """The operator's OWN Program and Season, built over real HTTP."""
        p = self._new_program(c, "Lifecycle C Program")
        status, raw, body = self._req(
            c, "POST", "/api/v2/setup/season",
            {"program_id": p, "name": "Lifecycle C Season"})
        self.assertEqual(status, 200, raw)
        return p, body["id"]

    def test_a_team_with_no_registrations_moves_under_a_program_only_context(self):
        """The Program-axis end. Nothing about this transfer consumes a Season,
        so a Program-ONLY explicit selection must be enough — this is the case
        a two-axis phase 1 would wrongly refuse."""
        c, uid = self._operator()
        p, s = self._own_program_with_season(c)
        home, away = self._competition(p, s)
        team = self._team(p, home, "Lifecycle C Rovers")
        self.assertEqual(self._registration_league_ids(team), {},
                         "the fixture Team already has registrations, so this "
                         "case is not the zero-registration one it is about")
        self._select(c, p)                      # Program-ONLY selection
        self.assertFalse(self.store.get_active_context(uid).season_id,
                         "the saved selection carries a Season")

        status, raw, _ = self._transfer(c, team, away)
        self.assertEqual(
            status, 200,
            f"a Team with NO active registrations could not be moved under a "
            f"Program-only selection: the Season axis is being demanded by a "
            f"mutation that does not consume it.{chr(10)}  reply: {raw}")
        self.assertEqual(self.store.get_team(team).league_id, away,
                         "the 200 did not persist the move")

    def test_a_registered_team_is_refused_under_a_program_only_context(self):
        """The Season-axis end, discovered only under the locks. The Team has
        an ACTIVE registration, so the transfer will rewrite a Season-owned
        row — and the caller has chosen no Season."""
        c, uid = self._operator()
        p, s = self._own_program_with_season(c)
        home, away = self._competition(p, s)
        team = self._team(p, home, "Lifecycle C Wanderers")
        srv.STATE.api.setup.register_team_for_season(s, team, league_id=home)
        before_regs = self._registration_league_ids(team)
        self.assertTrue(
            before_regs,
            "the fixture Team has no registration, so the axis never changes "
            "and this case cannot distinguish the two predicates")

        self._select(c, p)                      # Program-ONLY selection
        self.assertFalse(self.store.get_active_context(uid).season_id,
                         "the saved selection carries a Season")
        before_audit = self._audit()
        status, raw, _ = self._transfer(c, team, away)

        self.assertEqual(
            self.store.get_team(team).league_id, home,
            f"a Team's permanent League was CHANGED under a selection that "
            f"names no Season, and the move rewrites Season-owned "
            f"registrations.{chr(10)}  reply: {status} {raw}")
        self.assertEqual(
            self._registration_league_ids(team), before_regs,
            f"a Season-owned registration row was rewritten in a Season the "
            f"caller never chose.{chr(10)}  reply: {status} {raw}")
        self._assert_refused("team transfer, registered, Program-only context",
                             status, raw, before_audit=before_audit)

    def test_a_registered_team_moves_once_the_season_is_also_chosen(self):
        """THE POSITIVE CONTROL for the case above, and it is what makes the
        refusal meaningful: the identical request from the identical session
        succeeds the moment the operator also chooses the Season. Without it a
        gate that refused every transfer would satisfy the case above
        perfectly."""
        c, _uid = self._operator()
        p, s = self._own_program_with_season(c)
        home, away = self._competition(p, s)
        team = self._team(p, home, "Lifecycle C Comets")
        reg = srv.STATE.api.setup.register_team_for_season(
            s, team, league_id=home)
        # Read the VALUE, not the row object: InMemoryStore hands back the very
        # instance it stores, so holding `reg` and comparing it afterwards
        # would compare the rewritten row against itself and pass vacuously.
        reg_id, before_ls = reg.id, reg.league_season_id
        self._select(c, p, s)                   # BOTH axes chosen

        status, raw, _ = self._transfer(c, team, away)
        self.assertEqual(
            status, 200,
            f"the transfer was refused even with both axes explicitly "
            f"selected — the gate is refusing more than the rule.{chr(10)}"
            f"  reply: {raw}")
        self.assertEqual(self.store.get_team(team).league_id, away,
                         "the 200 did not persist the move")
        moved = self.store.get_season_team_registration(reg_id)
        self.assertNotEqual(
            moved.league_season_id, before_ls,
            "the registration was NOT rewritten, so this case never actually "
            "consumed the Season axis and proves nothing about it")


class _PreDisclosureEquivalenceMixin(_ProgramAxisHarness):
    """PHASE 1: the ``active_context_required`` 409 is answered BEFORE TARGET
    DISCLOSURE, and that is asserted two independent ways.

    An operator who has chosen nothing must not learn anything about the
    installation's records from being refused. There are two separable ways to
    leak that, and passing one of them is not passing the other:

      1. THE ANSWER VARIES. Three ids are sent down the SAME route by the SAME
         session — one that really exists and would be in scope under the
         fallback (HOME A, the Program ``_fallback()`` itself would pick), one
         that exists but is not (EMPTY B), and one that never existed. All
         three must be indistinguishable ON THE WIRE. Compared as RAW STATUS +
         RAW BODY BYTES, not as parsed dicts: two equal dicts say nothing about
         what a reader of the socket can distinguish — key order, spacing, a
         stray field all survive ``==`` on the parsed form. The id token is
         substituted out first, exactly as ``test_setup_target_authorization``
         normalises the echoed id, so the comparison is about everything
         EXCEPT the id the caller already knows.

      2. THE LOOKUP HAPPENS ANYWAY. Byte-equal answers today do not prove the
         decision was taken without reading the target; a lookup whose result
         is discarded is still a lookup, and it is one refactor away from being
         observable (a timing difference, a log line, a ``NotFoundError``
         raised from a deeper layer). So ``store.get_program`` is spied and the
         three probe ids are asserted never to reach it.

    Both run against the Program-only branch, because that is the branch the
    owner tightening rewrote. ``_mutation_context_error`` chooses its wording
    from the target KINDS — route shape alone, no store row — which is what
    makes property 1 true by construction; this proves it rather than assuming
    it.
    """

    ROUTE = "/api/v2/setup/program/{id}/delete"
    _GHOST = "program_id_that_never_existed"

    def _probe(self, c, record_id):
        return self._req(c, "POST", self.ROUTE.format(id=record_id), {})

    def _normalise(self, raw, record_id):
        """The raw body with the id token replaced by a fixed placeholder — the
        ONLY permitted difference between the three answers."""
        return raw.replace(record_id, "<TARGET-ID>")

    def test_the_409_is_byte_identical_for_real_foreign_and_absent_targets(self):
        c, uid = self._operator()
        self.assertIsNone(self.store.get_active_context(uid),
                          "the operator already has a saved selection, so "
                          "there is no un-chosen context to probe")
        # The fallback WOULD resolve HOME A, so "real, in scope" is a real
        # claim about this request and not just about the fixture.
        self.assertEqual(
            self._fallback_tuple(c),
            (self.world["program_id"], self.world["season_id"]),
            "the fallback did not resolve HOME A, so the 'real in-scope "
            "target' probe is not in scope for anything")

        real = self.world["program_id"]           # exists, fallback's own pick
        foreign = self.world["empty_program_id"]  # exists, not the fallback's
        ghost = self._GHOST                       # never existed
        self.assertNotEqual(real, foreign)
        self.assertIsNotNone(self.store.get_program(real))
        self.assertIsNotNone(self.store.get_program(foreign))
        self.assertIsNone(self.store.get_program(ghost))

        before_audit = self._audit()
        answers = {}
        for label, record_id in (("real", real), ("foreign", foreign),
                                 ("nonexistent", ghost)):
            status, raw, _ = self._probe(c, record_id)
            self._assert_refused(f"pre-disclosure probe / {label}", status,
                                 raw, before_audit=before_audit)
            answers[label] = (status, self._normalise(raw, record_id),
                              raw.encode())

        self.assertEqual(
            answers["real"][:2], answers["foreign"][:2],
            f"a real in-scope target and a foreign one are DISTINGUISHABLE "
            f"before any context was chosen:\n  real   : {answers['real']}\n"
            f"  foreign: {answers['foreign']}")
        self.assertEqual(
            answers["real"][:2], answers["nonexistent"][:2],
            f"an existing target and one that never existed are "
            f"DISTINGUISHABLE — the refusal is an existence oracle:\n"
            f"  real       : {answers['real']}\n"
            f"  nonexistent: {answers['nonexistent']}")
        # Byte-level, on the untouched bodies: with no id echoed there is
        # nothing left to normalise, so the raw bytes must match outright. If a
        # future body starts echoing the id, the normalised comparison above is
        # still the binding one and this line is what will say so.
        self.assertEqual(
            {answers[k][2] for k in answers}, {answers["real"][2]},
            "the three refusals differ in their RAW BYTES")

        # Nothing was destroyed by any of the three.
        self.assertIsNotNone(self.store.get_program(real))
        self.assertIsNotNone(self.store.get_program(foreign))
        self.assertEqual(self._audit(), before_audit,
                         "a refused probe wrote a setup-audit row")

    def test_the_target_is_never_looked_up_before_the_409(self):
        """Property 2, and it is the one that makes property 1 durable."""
        c, _uid = self._operator()
        real = self.world["program_id"]
        foreign = self.world["empty_program_id"]
        ghost = self._GHOST

        before_audit = self._audit()
        with self._target_lookup_must_not_run(real, foreign, ghost) as seen:
            for record_id in (real, foreign, ghost):
                status, raw, _ = self._probe(c, record_id)
                self._assert_refused(
                    f"lookup-free refusal / {record_id}", status, raw,
                    before_audit=before_audit)
        # Recorded for the report: on this path the gate reads the saved row,
        # finds nothing, and refuses — so no Program row is fetched at all.
        self.assertEqual(
            seen, [],
            f"the pre-disclosure refusal read Program rows it had no business "
            f"reading: {seen}")

    def test_both_phases_read_the_saved_row_and_phase_two_reads_it_locked(self):
        """PHASE 1 and PHASE 2 each read the ``ActiveContext`` row, and only
        phase 2 reads it under the LOCK.

        The refusal is decided twice on purpose. Phase 1 is the pre-disclosure
        preflight and it is ALREADY STALE when the caller acts on it — it runs
        outside the writing transaction, so a context switch can land between
        it and the write. Phase 2 is the boundary: it re-reads the row through
        ``get_active_context_for_update`` inside the transaction that performs
        the write, so a concurrent ``POST /api/context`` either orders wholly
        before the decision or waits for the write it authorized.

        Asserting the OUTCOME cannot distinguish "re-read" from "reused the
        preflight's answer" — with nothing racing, both give the same verdict.
        So the reads themselves are counted, and the LOCKING one is required.
        """
        c, _uid = self._operator()
        target = self.world["empty_program_id"]
        self._select(c, target)

        with self._count_saved_row_reads() as counts:
            status, raw, _ = self._probe(c, target)
        self.assertEqual(status, 200, f"the positive control failed, so the "
                                      f"counts below describe a refusal path "
                                      f"instead of a write path: {raw}")

        self.assertGreaterEqual(
            counts["plain"], 1,
            f"the PRE-DISCLOSURE preflight never read the saved row, so the "
            f"409 cannot have been decided before target disclosure: {counts}")
        self.assertGreaterEqual(
            counts["locked"], 1,
            f"no LOCKING read of the saved row happened during the mutation — "
            f"phase 2 is trusting the preflight's snapshot, which reopens the "
            f"check/use window the ActiveContext row lock exists to close: "
            f"{counts}")

    def test_a_chosen_context_makes_the_same_route_succeed(self):
        """ANTI-VACUITY for both properties above. A route that refused
        everything would satisfy them perfectly, so the identical request from
        the identical session must succeed once the operator chooses the
        target's own Program."""
        c, _uid = self._operator()
        target = self.world["empty_program_id"]
        self._select(c, target)
        before_audit = self._audit()
        with self._fallback_must_not_be_consulted(
                "the Program-only delete under an explicit selection"):
            status, raw, _ = self._probe(c, target)
        self.assertEqual(
            status, 200,
            f"an explicitly selected Program could not be deleted — the "
            f"pre-disclosure gate is refusing more than the rule.{chr(10)}"
            f"  reply: {raw}")
        self.assertIsNone(self.store.get_program(target),
                          "the 200 did not delete the Program")
        self.assertGreater(self._audit(), before_audit,
                           "the successful delete wrote no audit row, so the "
                           "zero-audit claims above prove nothing")


# -- the three backends ----------------------------------------------------
# InMemoryStore, SqlStore on SQLite, and real PostgreSQL when TEST_DATABASE_URL
# is exported. A Program-only selection stores a NULL season_id, so the two SQL
# backends exercise real three-valued NULL semantics that the dict store cannot.

class LifecycleAMemoryTest(_LifecycleAMixin, unittest.TestCase):
    DATABASE_URL = None


class LifecycleASqliteTest(_LifecycleAMixin, unittest.TestCase):
    DATABASE_URL = None      # set in setUpClass (one temp file per class)

    @classmethod
    def setUpClass(cls):
        cls.DATABASE_URL = _sqlite_url()


class LifecycleBMemoryTest(_LifecycleBMixin, unittest.TestCase):
    DATABASE_URL = None


class LifecycleBSqliteTest(_LifecycleBMixin, unittest.TestCase):
    DATABASE_URL = None

    @classmethod
    def setUpClass(cls):
        cls.DATABASE_URL = _sqlite_url()


class AxisChangesUnderTheLockMemoryTest(_AxisChangesUnderTheLockMixin,
                                        unittest.TestCase):
    DATABASE_URL = None


class AxisChangesUnderTheLockSqliteTest(_AxisChangesUnderTheLockMixin,
                                        unittest.TestCase):
    DATABASE_URL = None

    @classmethod
    def setUpClass(cls):
        cls.DATABASE_URL = _sqlite_url()


class PreDisclosureEquivalenceMemoryTest(_PreDisclosureEquivalenceMixin,
                                         unittest.TestCase):
    DATABASE_URL = None


class PreDisclosureEquivalenceSqliteTest(_PreDisclosureEquivalenceMixin,
                                         unittest.TestCase):
    DATABASE_URL = None

    @classmethod
    def setUpClass(cls):
        cls.DATABASE_URL = _sqlite_url()


if os.environ.get("TEST_DATABASE_URL"):
    class LifecycleAPostgresTest(_LifecycleAMixin, unittest.TestCase):
        DATABASE_URL = os.environ["TEST_DATABASE_URL"]

    class LifecycleBPostgresTest(_LifecycleBMixin, unittest.TestCase):
        DATABASE_URL = os.environ["TEST_DATABASE_URL"]

    class AxisChangesUnderTheLockPostgresTest(_AxisChangesUnderTheLockMixin,
                                              unittest.TestCase):
        DATABASE_URL = os.environ["TEST_DATABASE_URL"]

    class PreDisclosureEquivalencePostgresTest(
            _PreDisclosureEquivalenceMixin, unittest.TestCase):
        DATABASE_URL = os.environ["TEST_DATABASE_URL"]


if __name__ == "__main__":
    unittest.main()
