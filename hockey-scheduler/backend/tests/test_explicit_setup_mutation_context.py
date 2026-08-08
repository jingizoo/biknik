"""Guarded SETUP mutations must require an EXPLICIT persisted context (#409).

THE DEFECT. ``ContextService._resolve_locked`` falls through to ``_fallback()``,
which AUTO-SELECTS a Program (and its latest active Season) whenever nothing
usable is saved. ``LEAGUE_ADMIN`` / ``ARENA_MANAGER`` / ``VIEWER`` are
``context_scope._GLOBAL_ROLES``, so ``authorized_program_ids`` returns EVERY
Program in the installation and the fallback always finds a candidate. A
resolved ``(program, season)`` tuple therefore does NOT mean the operator chose
anything — it may be a Program they have never looked at.

``ApiService.setup_target_accessible`` (#369) judges every guarded setup
mutation against exactly that resolved tuple. So the same machinery that
correctly refuses a cross-Program delete happily COMMITS one the moment the
resolve falls back, because the target now sits inside the Program the fallback
just picked on the operator's behalf.

THE OWNER'S RULES, encoded here:

  * read-only landing/navigation MAY keep the deterministic fallback;
  * MUTATIONS must require an EXPLICIT persisted Program/Season tuple;
  * a STALE persisted selection must FAIL CLOSED, never silently retarget.

THE THREE STATES, per guarded surface:

  1. NO saved selection at all — a brand-new operator who has never POSTed
     ``/api/context``. Refuse; the target row must still exist and no audit row
     may be written.
  2. STALE saved selection — the row names a Season that no longer exists.
     Reached ONLY through the proven, operator-visible API recipe below; a
     direct ``store`` mutation would prove nothing about reachability. Refuse.
     The premise is asserted before the refusal is judged
     (``_assert_state_is_stale``): the saved row SURVIVED its Season, still
     names it, and the resolve now DIVERGES from it. Without that, a future
     in which Season deletion cleans up the saved row would quietly turn
     this state into a second copy of state 1 — still green, no longer about
     staleness at all.
  3. EXPLICIT correct selection — the POSITIVE CONTROL. Must succeed, and must
     keep succeeding after the fix. Without it every refusal above is vacuous:
     a route that refuses everything would satisfy states 1 and 2 perfectly.

THE PROVEN REACHABILITY RECIPE for state 2 (four ordinary authenticated calls,
no direct store access anywhere):

    POST /api/v2/setup/program              -> 200   fresh Program B
    POST /api/v2/setup/season               -> 200   fresh, EMPTY Season SB
    POST /api/context {program_id, season_id} -> 200 the saved row names them
    POST /api/v2/setup/season/<SB>/delete   -> 200   the saved row now DANGLES

The fourth call is the load-bearing one and it is why the Season must be
FRESH and EMPTY: a seeded Season is refused with ``has_dependencies`` and
cannot be destroyed this way. An operator tidying up a Season they created by
mistake performs this exact sequence.

WHAT THE STALE TESTS PIN is not merely "it is allowed" but the FLIP. The
identical request, from the identical session, is sent twice: once while the
operator's own Season still exists (refused, 404 — the #369 gate working), and
once after it is gone (today: 200 COMMITTED, against a Program the operator
never chose). Measured on this branch:

    archive Home-A's Season under an explicit (B, SB)  -> 404 not_found
    ... the operator deletes their own Season SB       -> 200
    archive Home-A's Season again, byte-identical      -> 200 ARCHIVED

PRECEDENT. PR #410 fixed exactly one mutation (the ice-availability commit)
this way: ``ApiService._selection_is_explicit`` compares the resolved tuple
against the SAVED ``ActiveContext`` row, and the route answers a structured
409 ``active_context_required`` BEFORE any target lookup. These tests assert
that same wire contract, because a second shape for the same rule is how
contracts drift. Every refusal assertion leads with the PERSISTED-STATE claim
(row still present, status unchanged, audit unchanged), so a fix that chooses
a different code still fails for the right reason and reads clearly.

BOTH STORES. ``InMemoryStore`` and ``SqlStore`` (SQLite; PostgreSQL too when
``TEST_DATABASE_URL`` is set). The decision reads an ``ActiveContext`` row and
compares it to a resolve — a dict lookup on one store, a real query with real
NULL semantics on the other — so agreement between them is part of the claim.

PRESERVED BEHAVIOUR. ``ReadOnlyNavigationFallbackTest`` is NOT a red test. The
owner explicitly kept the fallback for read-only navigation, so ``GET
/api/context`` and ``GET /api/v2/setup/overview`` must keep answering with a
resolved context for an operator who has selected nothing. It passes today and
a fix that breaks it would be wrong.
"""

import json
import os
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
from hockey_scheduler.web import server as srv

TZ = "America/Toronto"

# The two guarded surfaces under test. Both dispatch through
# `_handle_setup_v2` -> `_guarded_mutation` -> `setup_target_accessible`.
DELETE = "/api/v2/setup/division/{id}/delete"
ARCHIVE = "/api/v2/setup/seasons/{id}/archive"

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


class _ContextHarness:
    """Shared fixture + harness, parameterised on ``DATABASE_URL`` by the
    concrete subclasses.

    ``setUp`` rebuilds the shared ``srv.STATE`` on the chosen backend for EVERY
    test. That is deliberate and load-bearing: the fallback's choice depends on
    which Programs and active Seasons exist in the store, so a world inherited
    from a previous case would make "the fallback picks Home A" an accident of
    execution order rather than a controlled fixture. It also means an archive
    in one case cannot make a later case's Season read-only.
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
        """(status, raw_text, parsed). The RAW body is kept because the
        no-selection refusal must not name another Program's records."""
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
        over real HTTP with a real session cookie.

        A fresh identity per case (rather than clearing a shared one) makes
        "nothing is selected" a property of the account instead of a property
        of execution order, and there is no cleanup a later edit can forget.
        """
        username = f"op_{uuid.uuid4().hex[:10]}"
        account = srv.STATE.api.accounts.create_account(
            username, "demo", Role.LEAGUE_ADMIN)
        c = self._client()
        status, raw, _ = self._req(c, "POST", "/api/auth/login",
                                   {"username": username, "password": "demo"})
        self.assertEqual(status, 200, raw)
        user_id = account.id
        self.assertTrue(user_id, "no user id to key the ActiveContext on")
        self.assertIsNone(
            self.store.get_active_context(user_id),
            "the fixture operator already has a saved selection — every "
            "'no selection' assertion below would be vacuous")
        return c, user_id

    # -- fixture -----------------------------------------------------------
    def _world(self):
        """Program HOME A, its active Season, a League and an EMPTY Division.

        Built through the setup service, not HTTP: this is the FIXTURE, not
        the surface under test. Nobody has selected any of it — it exists only
        so the fallback has something to pick, which is precisely the defect.
        """
        svc = srv.STATE.api.setup
        program = svc.create_program("Home A", timezone_name=TZ)
        season = svc.create_season(program.id, "Home A Season")
        league = svc.create_league(season.id, "Home A League")
        division = svc.create_division(season.id, "Home A U16",
                                       league_id=league.id)
        return {"program_id": program.id, "season_id": season.id,
                "league_id": league.id, "division_id": division.id}

    # -- observation -------------------------------------------------------
    def _audit(self):
        return len(self.store.all_setup_audit())

    def _audit_ids(self):
        return {(a.action, a.entity_id) for a in self.store.all_setup_audit()}

    def _division_exists(self):
        return self.store.get_division(self.world["division_id"]) is not None

    def _season_status(self):
        season = self.store.get_season(self.world["season_id"])
        return None if season is None else season.status.value

    def _fallback_tuple(self, c):
        """What the READ side resolves for this session — proof that the
        fallback really is pointing at Home A, so a 200 on the mutation below
        is the fallback being honoured and not some other accident."""
        status, raw, body = self._req(c, "GET", "/api/context")
        self.assertEqual(status, 200, raw)
        return body.get("program_id"), body.get("season_id")

    # -- the reachability recipe (state 2) ---------------------------------
    def _select_own_fresh_season(self, c):
        """Calls 1-3 of the PROVEN recipe, over real authenticated HTTP: the
        operator creates their OWN Program B and a fresh, EMPTY Season SB and
        selects them. Returns ``(program_b, season_b)``.

        No direct ``store`` access anywhere: every step is something the
        operator can do in the UI.
        """
        status, raw, body = self._req(
            c, "POST", "/api/v2/setup/program",
            {"name": "Operator B", "timezone": TZ})
        self.assertEqual(status, 200, f"create Program B: {raw}")
        program_b = body["id"]

        status, raw, body = self._req(
            c, "POST", "/api/v2/setup/season",
            {"program_id": program_b, "name": "Operator B Season"})
        self.assertEqual(status, 200, f"create Season B: {raw}")
        season_b = body["id"]

        status, raw, body = self._req(
            c, "POST", "/api/context",
            {"program_id": program_b, "season_id": season_b})
        self.assertEqual(status, 200, f"select (B, SB): {raw}")
        self.assertEqual((body.get("program_id"), body.get("season_id")),
                         (program_b, season_b), raw)
        return program_b, season_b

    def _destroy_own_season(self, c, season_b):
        """Call 4: the operator deletes the very Season their saved row names,
        leaving that row DANGLING.

        This is the load-bearing step and it is why the Season has to be FRESH
        and EMPTY: a seeded Season is refused with ``has_dependencies`` and
        cannot be destroyed this way. Substituting a direct
        ``store.delete_season()`` would skip the whole reachability argument.
        """
        status, raw, _ = self._req(
            c, "POST", f"/api/v2/setup/season/{season_b}/delete", {})
        self.assertEqual(status, 200, f"delete the operator's own Season: {raw}")
        self.assertIsNone(self.store.get_season(season_b),
                          "the Season survived its own delete — the saved "
                          "selection is not stale and this proves nothing")

    def _assert_state_is_stale(self, c, uid, program_b, season_b):
        """THE PREMISE of state 2, asserted before the second mutation is sent.

        "Stale" is a THREE-part claim and every part has to be checked here,
        because each one can fail independently and each failure would turn
        the case that follows into a different, weaker test:

          1. the saved row SURVIVED its Season. Season deletion does not touch
             ``user_active_context`` today, but nothing in the schema forces
             that — no FK, no cascade — and a later slice that cleaned the row
             up on delete would leave this operator with NO saved selection at
             all. The case below would then be a duplicate of the
             ``no_selection`` case one screen up: still refused, still green,
             and no longer exercising staleness at all. Asserting the row is
             present, and that it still NAMES (B, SB), is what keeps this case
             about a DANGLING selection rather than an absent one.
          2. the Season it names is really GONE, so the row genuinely dangles.
          3. the resolve DIVERGES from it — the operator's saved (B, SB) is
             silently replaced by Home A, a Program they never chose.

        Part 3 is the whole defect: saved != resolved is the definition of a
        stale context, and the fix is the comparison of those two values. It
        is asserted as an inequality against the SAVED row and not only as an
        equality against the Home-A fixture, so a future where the resolve
        starts echoing the dangling selection back is caught here rather than
        silently satisfying "the mutation was refused" for the wrong reason.
        """
        saved = self.store.get_active_context(uid)
        self.assertIsNotNone(
            saved,
            "the operator's SAVED selection disappeared when their Season was "
            "deleted, so there is no stale row left to test: this case would "
            "silently degrade into a duplicate of the no-selection case")
        saved_tuple = (saved.program_id, saved.season_id)
        self.assertEqual(
            saved_tuple, (program_b, season_b),
            "the saved row no longer names the deleted (B, SB) — it was "
            "rewritten, so what follows is not a stale-selection test")

        self.assertIsNone(
            self.store.get_season(season_b),
            "the Season the saved row names still exists, so the row is not "
            "dangling and this case proves nothing")

        resolved = self._fallback_tuple(c)
        self.assertEqual(
            resolved, (self.world["program_id"], self.world["season_id"]),
            "the stale saved row did not fall back onto Home A, so this case "
            "is not exercising the silent retarget it is about")
        self.assertNotEqual(
            resolved, saved_tuple,
            "the resolve still returns the operator's own saved tuple, so "
            "saved and resolved AGREE and the context is not stale")

    # -- shared assertions --------------------------------------------------
    def _assert_refused(self, label, status, raw, *, before_audit,
                        division_must_exist=None, season_must_be=None):
        report = f"\n  case   : {label}\n  reply  : {status} {raw}"

        # (1) THE claim: nothing may have happened.
        if division_must_exist is not None:
            self.assertEqual(
                self._division_exists(), division_must_exist,
                f"a guarded delete COMMITTED without an explicit persisted "
                f"selection.{report}")
        if season_must_be is not None:
            self.assertEqual(
                self._season_status(), season_must_be,
                f"a guarded archive COMMITTED without an explicit persisted "
                f"selection.{report}")
        self.assertEqual(
            self._audit(), before_audit,
            f"a refused mutation still wrote a setup-audit row.{report}")
        self.assertNotEqual(
            status, 200,
            f"the mutation was accepted.{report}")

        # (2) The WIRE CONTRACT, matching PR #410's shape (#410 is NOT merged;
        #     it is held behind #345). Asserted as a contract, not imported: a
        # structured 409 decided BEFORE any target lookup, so the refusal is
        # about the caller's context and never an existence oracle.
        self.assertEqual(
            status, 409,
            f"expected the #410 `active_context_required` 409.{report}")
        body = json.loads(raw or "{}")
        self.assertEqual(
            body.get("error", {}).get("code"), "active_context_required",
            f"expected the #410 `active_context_required` code.{report}")


class _GuardedSetupContextMixin(_ContextHarness):
    """The three-state matrix for the two guarded setup surfaces. States 1 and
    2 are RED against this branch; state 3 is the positive control."""

    # == STATE 1: nothing selected =========================================
    def test_delete_with_no_selection_is_refused_with_no_side_effects(self):
        """A brand-new operator has chosen NOTHING, yet the fallback resolves
        Home A for them and the #369 target gate is satisfied by it. The
        Division is destroyed in a Program they have never seen."""
        c, _uid = self._operator()
        self.assertEqual(
            self._fallback_tuple(c),
            (self.world["program_id"], self.world["season_id"]),
            "the fallback did not resolve Home A, so this case is not "
            "exercising the auto-selection it is about")

        before = self._audit()
        status, raw, _ = self._req(
            c, "POST", DELETE.format(id=self.world["division_id"]), {})
        self._assert_refused("no selection / delete", status, raw,
                             before_audit=before, division_must_exist=True)
        # A refusal must not hand back the un-chosen Program's inventory.
        self.assertNotIn("Home A U16", raw)

    def test_archive_with_no_selection_is_refused_with_no_side_effects(self):
        c, _uid = self._operator()
        self.assertEqual(self._season_status(), "active")

        before = self._audit()
        status, raw, _ = self._req(
            c, "POST", ARCHIVE.format(id=self.world["season_id"]), {})
        self._assert_refused("no selection / archive", status, raw,
                             before_audit=before, season_must_be="active")
        self.assertNotIn("Home A Season", raw)

    # == STATE 2: the saved selection went stale ===========================
    def test_delete_flips_from_refused_to_committed_when_the_selection_goes_stale(self):
        """The FLIP, pinned end to end in one session.

        The operator selects their own (B, SB) and is correctly refused the
        foreign Division — that refusal is the #369 gate working, and it is
        the anti-vacuity control for the second half. They then delete their
        own empty Season. Nothing about their intent changed, they still hold
        no authority over Home A, and they never selected it — but the saved
        row now dangles, `_fallback()` silently hands them Home A, and the
        BYTE-IDENTICAL request is accepted.
        """
        c, uid = self._operator()
        route = DELETE.format(id=self.world["division_id"])

        # -- half 1: the gate WORKS while the selection is real ------------
        program_b, season_b = self._select_own_fresh_season(c)
        before = self._audit()
        first_status, first_raw, _ = self._req(c, "POST", route, {})
        self.assertNotEqual(
            first_status, 200,
            f"the foreign Division was deletable even under an EXPLICIT "
            f"selection of (B, SB) — the #369 gate is not working, so the "
            f"second half proves nothing: {first_raw}")
        self.assertTrue(self._division_exists(), first_raw)
        self.assertEqual(self._audit(), before,
                         "the refused delete wrote an audit row")

        # -- half 2: destroy the operator's own Season, change nothing else -
        self._destroy_own_season(c, season_b)
        self._assert_state_is_stale(c, uid, program_b, season_b)

        before = self._audit()
        status, raw, _ = self._req(c, "POST", route, {})
        self._assert_refused(
            f"stale selection ({program_b}/{season_b} deleted) / delete",
            status, raw, before_audit=before, division_must_exist=True)
        self.assertNotIn("Home A U16", raw)

    def test_archive_flips_from_refused_to_committed_when_the_selection_goes_stale(self):
        """The same flip on ``seasons/<id>/archive``.

        Measured on this branch, in one session, byte-identical requests:
        404 ``not_found`` while (B, SB) is real, then 200 with the Season
        ARCHIVED once SB is deleted.
        """
        c, uid = self._operator()
        route = ARCHIVE.format(id=self.world["season_id"])

        program_b, season_b = self._select_own_fresh_season(c)
        before = self._audit()
        first_status, first_raw, _ = self._req(c, "POST", route, {})
        self.assertNotEqual(
            first_status, 200,
            f"Home A's Season was archivable even under an EXPLICIT selection "
            f"of (B, SB) — the #369 gate is not working, so the second half "
            f"proves nothing: {first_raw}")
        self.assertEqual(self._season_status(), "active", first_raw)
        self.assertEqual(self._audit(), before,
                         "the refused archive wrote an audit row")

        self._destroy_own_season(c, season_b)
        self._assert_state_is_stale(c, uid, program_b, season_b)

        before = self._audit()
        status, raw, _ = self._req(c, "POST", route, {})
        self._assert_refused(
            f"stale selection ({program_b}/{season_b} deleted) / archive",
            status, raw, before_audit=before, season_must_be="active")
        self.assertNotIn("Home A Season", raw)

    # == STATE 3: the POSITIVE CONTROLS ====================================
    def test_delete_under_an_explicit_correct_selection_succeeds(self):
        """ANTI-VACUITY. The very same operator, the very same Division, the
        very same route — but with Home A explicitly selected. This must be a
        200 today AND after the fix; a guard that refuses everything is not a
        fix, it is an outage."""
        c, _uid = self._operator()
        status, raw, _ = self._req(
            c, "POST", "/api/context",
            {"program_id": self.world["program_id"],
             "season_id": self.world["season_id"]})
        self.assertEqual(status, 200, raw)

        before = self._audit_ids()
        status, raw, _ = self._req(
            c, "POST", DELETE.format(id=self.world["division_id"]), {})
        self.assertEqual(
            status, 200,
            f"an EXPLICITLY selected Program/Season could not delete its own "
            f"Division — the guard is over-broad: {raw}")
        self.assertFalse(self._division_exists(),
                         "the 200 did not actually delete the Division")
        self.assertTrue(
            self._audit_ids() - before,
            "the successful delete wrote no audit row, so the audit-count "
            "assertions in the refusal cases prove nothing")

    def test_archive_under_an_explicit_correct_selection_succeeds(self):
        c, _uid = self._operator()
        status, raw, _ = self._req(
            c, "POST", "/api/context",
            {"program_id": self.world["program_id"],
             "season_id": self.world["season_id"]})
        self.assertEqual(status, 200, raw)

        before = self._audit_ids()
        status, raw, _ = self._req(
            c, "POST", ARCHIVE.format(id=self.world["season_id"]), {})
        self.assertEqual(
            status, 200,
            f"an EXPLICITLY selected Season could not be archived — the guard "
            f"is over-broad: {raw}")
        self.assertEqual(self._season_status(), "archived",
                         "the 200 did not actually archive the Season")
        self.assertTrue(self._audit_ids() - before,
                        "the successful archive wrote no audit row")


class _ReadOnlyNavigationFallbackMixin(_ContextHarness):
    """PRESERVED BEHAVIOUR — **this class passes today and must keep passing.**

    The owner's rules split read from write: *read-only landing/navigation MAY
    keep the deterministic fallback.* A landing page that 409s the moment an
    operator has not yet chosen a context would be a worse regression than the
    defect this file is about — there would be no way to see what is there in
    order to choose it.

    So these two GETs, for the same brand-new operator whose MUTATIONS the
    cases above demand be refused, must still answer 200 with the fallback's
    resolved Program/Season. They are listed here as the boundary of the fix,
    not as evidence of a bug.
    """

    def test_get_context_still_answers_under_the_fallback(self):
        c, uid = self._operator()
        self.assertIsNone(self.store.get_active_context(uid),
                          "nothing may be persisted for this operator")

        status, raw, body = self._req(c, "GET", "/api/context")
        self.assertEqual(
            status, 200,
            f"read-only navigation was refused for an operator with no saved "
            f"selection — the fix went too far: {raw}")
        self.assertEqual(body.get("program_id"), self.world["program_id"], raw)
        self.assertEqual(body.get("season_id"), self.world["season_id"], raw)

        # Reading must not become writing: the fallback is computed, never
        # persisted, so the operator still has no explicit selection.
        self.assertIsNone(
            self.store.get_active_context(uid),
            "a read-only resolve PERSISTED a selection the operator never "
            "made — that would launder the fallback into an explicit choice "
            "and defeat the whole guard")

    def test_setup_overview_still_answers_under_the_fallback(self):
        c, uid = self._operator()
        status, raw, body = self._req(c, "GET", "/api/v2/setup/overview")
        self.assertEqual(
            status, 200,
            f"the setup overview was refused for an operator with no saved "
            f"selection — the fix went too far: {raw}")
        self.assertIsInstance(body, dict, raw)
        self.assertIn(self.world["season_id"], raw,
                      f"the overview answered but showed nothing from the "
                      f"fallback's Program: {raw}")
        self.assertIsNone(self.store.get_active_context(uid),
                          "the overview PERSISTED a selection")


# -- the two backends ------------------------------------------------------
# InMemoryStore and SqlStore, and (when TEST_DATABASE_URL is exported) real
# PostgreSQL as well. `get_active_context` is a dict lookup on one and a real
# query with real NULL semantics on the others, so agreement between them is
# part of what is being claimed.

class GuardedSetupMutationMemoryTest(_GuardedSetupContextMixin,
                                     unittest.TestCase):
    DATABASE_URL = None


class GuardedSetupMutationSqliteTest(_GuardedSetupContextMixin,
                                     unittest.TestCase):
    DATABASE_URL = None      # set in setUpClass (one temp file per class)

    @classmethod
    def setUpClass(cls):
        cls.DATABASE_URL = _sqlite_url()


class ReadOnlyNavigationFallbackMemoryTest(_ReadOnlyNavigationFallbackMixin,
                                           unittest.TestCase):
    DATABASE_URL = None


class ReadOnlyNavigationFallbackSqliteTest(_ReadOnlyNavigationFallbackMixin,
                                           unittest.TestCase):
    DATABASE_URL = None

    @classmethod
    def setUpClass(cls):
        cls.DATABASE_URL = _sqlite_url()


if os.environ.get("TEST_DATABASE_URL"):
    class GuardedSetupMutationPostgresTest(_GuardedSetupContextMixin,
                                           unittest.TestCase):
        DATABASE_URL = os.environ["TEST_DATABASE_URL"]

    class ReadOnlyNavigationFallbackPostgresTest(
            _ReadOnlyNavigationFallbackMixin, unittest.TestCase):
        DATABASE_URL = os.environ["TEST_DATABASE_URL"]


if __name__ == "__main__":
    unittest.main()
