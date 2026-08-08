"""A resolved active context is not an EXPLICIT one — the scheduler half (#409).

THE DEFECT. ``ContextService._resolve_locked`` falls through to
``ContextService._fallback`` whenever the caller has no usable saved selection,
and ``_fallback`` AUTO-SELECTS: the first authorized Program by id that owns an
authorized non-archived Season, together with that Season. For
``LEAGUE_ADMIN`` / ``ARENA_MANAGER`` / ``VIEWER`` — ``context_scope._GLOBAL_ROLES``,
authorized for EVERY Program in the installation — that fallback is never empty
while any Program exists. So ``resolve_with_league`` returning a non-``None``
Program proves only that the installation HAS one; it does not prove the
operator ever chose it.

Every consumer that treats the resolved tuple as the operator's decision
inherits that. ``ApiService._locked_draft_targets`` is one such consumer: it
calls ``resolve_with_league(..., lock=True)``, refuses only when ``program is
None``, and then treats every draft Game inside the resolved tuple as the
caller's own. ``POST /api/scheduler/drafts/publish`` and ``/discard`` with
``{"all": true}`` therefore act on the FALLBACK Program's drafts — a Program
the caller may never have opened, and in the stale case one they demonstrably
navigated AWAY from.

THE OWNER'S CONTRACT, which is what this file encodes:

  * read-only landing/navigation MAY keep the deterministic fallback — nothing
    here asserts otherwise, and no read route is touched;
  * a MUTATION must require an EXPLICIT persisted Program/Season tuple;
  * a STALE persisted selection must FAIL CLOSED, never silently retarget.

WHY THIS IS NOT ALREADY COVERED. ``test_draft_context_scope.py`` (#386) proves
these same two routes are bound to the resolved tuple: a Program-B-selected
operator cannot reach Program A's drafts. Every assertion there stays green
while the hole below is open, because #386 asks "does the resolved tuple
CONFINE the batch?" and the answer is yes. This file asks the prior question —
"is the resolved tuple the operator's CHOICE?" — and the answer is no. A
caller with no selection at all, and a caller whose selection has been
destroyed underneath them, both resolve a tuple #386 is perfectly happy to
confine them to. Confinement to a Program nobody picked is not authorization.

REACHABILITY OF THE STALE ROW — the load-bearing part of case 2. The dangling
``ActiveContext`` is produced ONLY by the four ordinary, authorized,
200-returning API calls an operator can make from the UI, in this order:

    POST /api/v2/setup/program              -> 200  (a fresh Program)
    POST /api/v2/setup/season               -> 200  (a fresh, EMPTY Season)
    POST /api/context {program_id,season_id}-> 200  (the saved row names them)
    POST /api/v2/setup/season/<that>/delete -> 200  (the row now DANGLES)

Nothing here reaches past the HTTP boundary to plant that row. That matters:
``store.delete_season()`` called directly from a test would prove only that an
inconsistent database misbehaves, which is not a defect anyone has to fix. The
EMPTY Season is equally load-bearing — a seeded Season is refused with
``has_dependencies``, so only a freshly created one deletes cleanly, and that
single distinction is the entire reachability argument. ``set_active_context``
does not cascade, so the saved row survives its Season's deletion; then
``_resolve_locked`` finds ``get_season(saved.season_id) is None``, declines to
dangle — and hands the caller the FALLBACK instead of failing.

MEASURED CONSEQUENCES this file pins (verified on Memory and SQLite before it
was written), for an operator standing in their own Program:

  * ``/discard`` returns ``200 {"discarded": 6}`` and DELETES all six draft
    Games of the fallback Program, releasing all six ALLOCATED ice slots back
    to AVAILABLE and writing six ``draft_game_discarded`` audit rows;
  * ``/publish`` returns ``200 {"published": 6}`` and flips those same six
    Games to non-draft + published, leaving their slots ALLOCATED and writing
    six ``game_published`` audit rows.

SHAPE OF THE EXPECTED REFUSAL — PR #410's already-merged precedent, followed
rather than reinvented: ``ApiService._selection_is_explicit`` compares the
RESOLVED tuple against the SAVED ``ActiveContext`` row, and the route answers a
structured ``409 active_context_required`` BEFORE any target lookup. Both cases
below land on that one code by construction: with no saved row there is nothing
to compare, and with a stale row the resolved tuple (the fallback Program) is
not the saved one, so neither is explicit.

TEST DESIGN — the positive control is INSIDE each negative test, on purpose.
Every case drives the SAME account and the SAME request twice: once in the
un-chosen/stale state (must refuse, zero side effects) and once after an
explicit ``POST /api/context`` naming the drafts' own tuple (must succeed).
The only variable between the two halves is the selection, so a refusal cannot
be explained by the role, the route, the payload, an archived Season, or an
empty draft set — and the success half is what proves the fix must not simply
break these routes. The success half passes TODAY and must keep passing; the
refusal half fails today, which is this phase's deliverable.

Assertions are on PERSISTED STATE, never on the status code alone: a full
snapshot of every Game's ``(is_draft, published, ice_slot_id)``, every ice
slot's status, and the whole setup-audit trail is compared before and after the
refused call. A 409 that still deleted the Games would pass a status-only test.

Runs over a real ``ThreadingHTTPServer`` + ``srv.Handler`` with real login
cookies, on BOTH the in-memory store and a file-backed ``SqlStore`` (and
PostgreSQL when ``TEST_DATABASE_URL`` is set), because the resolution happens
under real transaction isolation that only a real store exercises.
"""

import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from http.cookiejar import CookieJar
from http.server import ThreadingHTTPServer

from helpers import BACKEND, commit_fresh_draft  # noqa: F401  (sets up sys.path)

import hockey_scheduler.web.server as srv
from hockey_scheduler.domain import Role

PASSWORD = "demo"

# The operator who has NEVER saved a context. Its own account precisely so that
# no other case's `POST /api/context` can create the row whose ABSENCE is the
# whole point of case 1.
NEWCOMER = "newcomer"
NEWCOMER_ID = "user_newcomer"
# The operator who saves a selection and then destroys its Season.
OPERATOR = "operator"
OPERATOR_ID = "user_operator"

ICE_BASE = datetime(2026, 9, 7, 18, tzinfo=timezone.utc)

# Four teams => C(4,2) = 6 single round-robin fixtures. A non-trivial count on
# purpose: "6 drafts destroyed" and "6 drafts untouched" are distinguishable
# from each other AND from an empty fixture, so no assertion below can pass
# vacuously because there was nothing to act on.
TEAMS = 4
EXPECTED_DRAFTS = 6

# The refusal the owner's contract requires, in PR #410's already-merged shape.
EXPECTED_STATUS = 409
EXPECTED_CODE = "active_context_required"


def _ok(result, label=""):
    assert isinstance(result, dict) and "error" not in result, (label, result)
    return result


class ExplicitMutationContextContract:
    """Shared body; each subclass supplies the store the server runs on.

    Fixture construction goes through ``STATE.api`` directly (``role is None``,
    the ungated internal path) rather than over HTTP. That is deliberate and is
    NOT the shortcut the stale row is forbidden to take: what must be reachable
    by ordinary authorized use is the DEFECT (the dangling saved row), and that
    is built over real HTTP in ``_make_saved_selection_stale``. The victim
    Program is just scenery, and building it internally keeps it out of every
    account's context history — an HTTP-built Program would be created by some
    logged-in operator and muddy which selections exist.
    """

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
        # Registered BEFORE anything that can fail: DATABASE_URL and STATE are
        # process-global, so a setUp that raised past this point would leave
        # every later module in this worker pointed at this test's store.
        # addCleanup runs even when setUp itself fails.
        self.addCleanup(self._restore_environment)
        # `seed=False` — a CLEAN SLATE. The canonical demo seed would create
        # its own Program, and `_fallback` picks the first authorized Program
        # by id: the fixture below must be the one it lands on for the
        # retargeting to be observable at all, and on a clean slate it is the
        # only candidate that owns an active Season.
        srv.STATE.reset(seed=False)
        srv.RATE_LIMITER.reset()
        srv.LOGIN_THROTTLE.reset()
        self.api = srv.STATE.api
        self.store = self.api.store
        self._build_victim_program()
        self._create_operator_accounts()
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), srv.Handler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever,
                                       daemon=True)
        self.thread.start()

    def tearDown(self):
        self.httpd.shutdown()
        self.thread.join(timeout=5)
        self.httpd.server_close()

    def _restore_environment(self):
        if self._prev_db is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = self._prev_db
        # Rebuild on the RESTORED url: closes this test's store and leaves the
        # process-global singleton usable for the modules that share it. A
        # failure here must not mask this test's own verdict nor skip the
        # temp-file removal below.
        try:
            srv.STATE.reset(seed=False)
        except Exception:
            pass
        if self._tmp_path:
            try:
                os.remove(self._tmp_path)
            except OSError:
                pass

    # -- fixture -----------------------------------------------------------
    def _build_victim_program(self):
        """One fully schedulable Program holding six COMMITTED draft Games.

        This is the Program ``_fallback`` auto-selects, and therefore the one
        an un-chosen or stale caller silently mutates. Committed rather than
        merely proposed, because a committed draft has already flipped its ice
        slot to ALLOCATED (#277) — which is what makes "the slots were freed"
        an observable consequence of a wrongly-targeted discard rather than an
        invisible one.
        """
        api = self.api
        program = _ok(api.create_program("Victim Program"), "program")
        club = _ok(api.create_club("Victim Club"), "club")
        # `Venue.league_id` is LEGACY vocabulary holding a PROGRAM id, not a
        # competition League (integrity_checks joins seasons.program_id =
        # venues.league_id). A League id here builds a Venue tied to nothing
        # this fixture can grant against and every corner comes back empty.
        venue = _ok(api.create_venue("Victim Venue", league_id=program["id"]),
                    "venue")
        rink = _ok(api.create_rink(venue["id"], "Victim Rink"), "rink")
        season = _ok(api.create_season(program["id"], "Victim Season",
                                       "2026-09-01", "2027-03-31"), "season")
        _ok(api.grant_season_venue_access(season["id"], venue["id"]), "grant")
        league = _ok(api.create_league(season["id"], "Victim League"), "league")
        division = _ok(api.create_division(season["id"], "Victim Division",
                                           league_id=league["id"]), "division")
        for index in range(TEAMS):
            team = _ok(api.create_team(club["id"], None, f"Victim Team {index}",
                                       program_id=program["id"],
                                       league_id=league["id"]), "team")
            _ok(api.register_team_for_season(season["id"], team["id"],
                                             division["id"],
                                             league_id=league["id"]),
                "registration")
        # Two slots more than the six fixtures need, so the discard's slot
        # release is measured against a rink that also holds ice it must NOT
        # touch — "every slot is AVAILABLE" would otherwise be trivially true.
        for index in range(EXPECTED_DRAFTS + 2):
            start = ICE_BASE + timedelta(days=index)
            _ok(api.create_ice_slot(rink["id"], start.isoformat(),
                                    (start + timedelta(hours=2)).isoformat()),
                "slot")
        _ok(commit_fresh_draft(self.api, division_id=division["id"]), "commit")
        self.victim = {"program": program["id"], "season": season["id"],
                       "league": league["id"], "division": division["id"]}
        drafts = [g for g in self.store.all_games() if g.is_draft]
        # Anti-vacuity: assert the fixture really produced the drafts every
        # case below measures, before any case can pass by finding nothing.
        self.assertEqual(
            len(drafts), EXPECTED_DRAFTS,
            "the fixture did not commit the expected draft schedule, so every "
            "assertion below would be vacuous")

    def _create_operator_accounts(self):
        """Two real LEAGUE_ADMIN accounts.

        LEAGUE_ADMIN is a ``context_scope._GLOBAL_ROLES`` member holding
        MANAGE_SCHEDULE, so both accounts are fully entitled to publish and
        discard — the positive control in every case proves exactly that. The
        only thing either of them lacks is an explicit selection.
        """
        for username, account_id in ((NEWCOMER, NEWCOMER_ID),
                                     (OPERATOR, OPERATOR_ID)):
            self.api.accounts.create_account(
                username, PASSWORD, Role.LEAGUE_ADMIN, scope={},
                actor_id="test_seed", account_id=account_id)

    # -- HTTP --------------------------------------------------------------
    def _client(self):
        return urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(CookieJar()))

    def _req(self, client, method, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}", data=data, method=method)
        if data is not None:
            request.add_header("Content-Type", "application/json")
        try:
            with client.open(request) as response:
                return response.status, json.loads(response.read() or b"{}")
        except urllib.error.HTTPError as error:
            with error:
                return error.code, json.loads(error.read() or b"{}")

    def _login(self, username):
        client = self._client()
        status, body = self._req(client, "POST", "/api/auth/login",
                                 {"username": username, "password": PASSWORD})
        self.assertEqual(status, 200, body)
        return client

    def _select(self, client, program_id, season_id, league_id=None):
        status, body = self._req(client, "POST", "/api/context", {
            "program_id": program_id, "season_id": season_id,
            "league_id": league_id})
        self.assertEqual(status, 200, body)
        return body

    # -- the reachable stale row -------------------------------------------
    def _make_saved_selection_stale(self, client):
        """The proven four-call recipe, over real authorized HTTP only.

        Returns the ``(program_id, season_id)`` the saved row is left naming.
        Each call is asserted to return 200: if the platform ever closes this
        path — by cascading the saved selection on Season delete, or by
        refusing to delete a Season some ActiveContext still names — this
        helper fails loudly at the step that changed, rather than the case
        below quietly degrading into a duplicate of case 1.
        """
        status, program = self._req(client, "POST", "/api/v2/setup/program",
                                    {"name": "Operator's Own Program"})
        self.assertEqual(status, 200, program)
        status, season = self._req(client, "POST", "/api/v2/setup/season", {
            "program_id": program["id"], "name": "Operator's Own Season",
            "start_date": "2026-09-01", "end_date": "2027-03-31"})
        self.assertEqual(status, 200, season)
        self._select(client, program["id"], season["id"])
        # The EMPTY Season is what makes this deletable: a seeded one is
        # refused with `has_dependencies`, and substituting a direct
        # `store.delete_season()` would forfeit the reachability proof.
        status, deleted = self._req(
            client, "POST", f"/api/v2/setup/season/{season['id']}/delete")
        self.assertEqual(
            status, 200, f"the empty Season did not delete cleanly: {deleted}")
        self.assertIsNone(
            self.store.get_season(season["id"]),
            "the Season row survived its own 200 delete")
        return program["id"], season["id"]

    # -- persisted-state snapshot ------------------------------------------
    def _snapshot(self):
        """Everything a wrongly-targeted publish or discard would disturb.

        Games, ice slots and the setup-audit trail together, compared as ONE
        value: a refusal that returned 409 and still deleted the Games, or that
        wrote an audit row for a mutation it declined, has to be a failure.
        """
        return {
            "games": {g.id: (g.is_draft, g.published, g.ice_slot_id)
                      for g in self.store.all_games()},
            "slots": {s.id: s.status.value
                      for s in self.store.all_ice_slots()},
            "audit": [(a.action, a.entity_type, a.entity_id)
                      for a in self.store.all_setup_audit()],
        }

    def _assert_drafts_intact(self, before):
        """The fixture's six drafts are still drafts, on their own ALLOCATED
        ice. Stated separately from the whole-snapshot equality so a failure
        reports WHAT was destroyed, not just that two large dicts differ."""
        drafts = [g for g in self.store.all_games() if g.is_draft]
        self.assertEqual(
            len(drafts), EXPECTED_DRAFTS,
            f"the refused call changed the draft Game count from "
            f"{EXPECTED_DRAFTS} to {len(drafts)}")
        allocated = [s for s in self.store.all_ice_slots()
                     if s.status.value == "allocated"]
        self.assertEqual(
            len(allocated), EXPECTED_DRAFTS,
            "the refused call released ice slots the drafts still occupy")
        for action in ("draft_game_discarded", "game_published"):
            self.assertEqual(
                [row for row in self._snapshot()["audit"]
                 if row[0] == action and row not in before["audit"]], [],
                f"the refused call wrote a {action} audit row")

    # -- the shared case body ----------------------------------------------
    def _refuses_then_succeeds(self, client, verb, why):
        """Drive ``verb`` twice on ONE account: refused now, allowed after an
        explicit selection.

        The second half is the positive control and it is not optional — it is
        what makes the first half's refusal mean "no explicit selection"
        rather than "this route never works here".
        """
        count_key = {"publish": "published", "discard": "discarded"}[verb]
        path = f"/api/scheduler/drafts/{verb}"

        before = self._snapshot()
        status, body = self._req(client, "POST", path, {"all": True})

        # 1. the refusal itself
        self.assertEqual(
            status, EXPECTED_STATUS,
            f"POST {path} with {why} returned {status} {body} — a mutation "
            f"ran against the fallback Program the operator never chose")
        self.assertEqual(
            (body.get("error") or {}).get("code"), EXPECTED_CODE,
            f"POST {path} with {why} did not refuse with the structured "
            f"'{EXPECTED_CODE}' contract: {body}")
        self.assertNotIn(count_key, body, body)

        # 2. and ZERO side effects — the status code alone proves nothing
        self._assert_drafts_intact(before)
        self.assertEqual(
            self._snapshot(), before,
            f"POST {path} with {why} refused but still changed persisted state")

        # 3. POSITIVE CONTROL: same account, same request, explicit selection
        self._select(client, self.victim["program"], self.victim["season"],
                     self.victim["league"])
        status, body = self._req(client, "POST", path, {"all": True})
        self.assertEqual(
            status, 200,
            f"the positive control failed: {verb} was refused even under an "
            f"explicit selection of the drafts' own tuple ({body})")
        self.assertEqual(
            body.get(count_key), EXPECTED_DRAFTS,
            f"the positive control acted on {body.get(count_key)} of "
            f"{EXPECTED_DRAFTS} drafts: {body}")

    # -- case 1: no saved selection at all ---------------------------------
    def test_publish_without_any_saved_selection_is_refused(self):
        client = self._login(NEWCOMER)
        self.assertIsNone(
            self.store.get_active_context(NEWCOMER_ID),
            "this account was supposed to have no saved context row")
        self._refuses_then_succeeds(client, "publish", "no saved selection")

    def test_discard_without_any_saved_selection_is_refused(self):
        client = self._login(NEWCOMER)
        self.assertIsNone(
            self.store.get_active_context(NEWCOMER_ID),
            "this account was supposed to have no saved context row")
        self._refuses_then_succeeds(client, "discard", "no saved selection")

    # -- case 2: a stale saved selection, built only over HTTP -------------
    def test_publish_with_a_stale_saved_selection_fails_closed(self):
        client = self._login(OPERATOR)
        own_program, own_season = self._make_saved_selection_stale(client)
        self._assert_saved_row_dangles(own_program, own_season)
        self._refuses_then_succeeds(client, "publish", "a STALE saved selection")

    def test_discard_with_a_stale_saved_selection_fails_closed(self):
        client = self._login(OPERATOR)
        own_program, own_season = self._make_saved_selection_stale(client)
        self._assert_saved_row_dangles(own_program, own_season)
        self._refuses_then_succeeds(client, "discard", "a STALE saved selection")

    def _assert_saved_row_dangles(self, own_program, own_season):
        """The precondition both stale cases rest on, asserted rather than
        assumed: the saved row still names the operator's OWN Program and its
        now-deleted Season, and the tuple the platform resolves for them is a
        DIFFERENT Program — the fixture's. Without this, a stale case that
        happened to resolve back to the operator's own Program would prove
        nothing about retargeting."""
        saved = self.store.get_active_context(OPERATOR_ID)
        self.assertIsNotNone(saved, "the saved selection vanished")
        self.assertEqual(saved.program_id, own_program)
        self.assertEqual(
            saved.season_id, own_season,
            "the saved row no longer dangles, so this case no longer models a "
            "stale selection")
        program, season, _league = self.api.context.resolve_with_league(
            OPERATOR_ID, Role.LEAGUE_ADMIN, {})
        self.assertEqual(
            (program.id if program else None), self.victim["program"],
            "the fallback did not retarget the operator into the victim "
            "Program, so this case cannot observe the defect")
        self.assertEqual(
            (season.id if season else None), self.victim["season"],
            "the fallback did not retarget the operator into the victim Season")

    # -- case 3: the explicit selection, standing alone --------------------
    def test_an_explicit_selection_publishes_and_is_the_only_difference(self):
        """The positive control as its OWN case, ahead of any refusal.

        Cases 1 and 2 each end with this same call, but only after a refused
        one. Running it first and alone proves the route works on a completely
        clean path — so if the fix ever over-corrects into refusing everything,
        this fails on its own terms rather than being written off as fallout
        from whatever the earlier half did.
        """
        client = self._login(OPERATOR)
        self._select(client, self.victim["program"], self.victim["season"],
                     self.victim["league"])
        before = self._snapshot()
        status, body = self._req(client, "POST",
                                 "/api/scheduler/drafts/publish", {"all": True})
        self.assertEqual(status, 200, body)
        self.assertEqual(body.get("published"), EXPECTED_DRAFTS, body)
        # Persisted state, not just the count the route reported back.
        self.assertEqual([g for g in self.store.all_games() if g.is_draft], [])
        self.assertEqual(
            len([g for g in self.store.all_games() if g.published]),
            EXPECTED_DRAFTS)
        self.assertEqual(
            len([s for s in self.store.all_ice_slots()
                 if s.status.value == "allocated"]), EXPECTED_DRAFTS,
            "published games must keep their ice ALLOCATED")
        published_rows = [row for row in self._snapshot()["audit"]
                          if row[0] == "game_published"
                          and row not in before["audit"]]
        self.assertEqual(len(published_rows), EXPECTED_DRAFTS)

    def test_an_explicit_selection_discards_and_is_the_only_difference(self):
        client = self._login(OPERATOR)
        self._select(client, self.victim["program"], self.victim["season"],
                     self.victim["league"])
        before = self._snapshot()
        status, body = self._req(client, "POST",
                                 "/api/scheduler/drafts/discard", {"all": True})
        self.assertEqual(status, 200, body)
        self.assertEqual(body.get("discarded"), EXPECTED_DRAFTS, body)
        self.assertEqual([g for g in self.store.all_games() if g.is_draft], [])
        self.assertEqual(
            len([s for s in self.store.all_ice_slots()
                 if s.status.value == "allocated"]), 0,
            "a discarded draft must release its ice back to AVAILABLE")
        discarded_rows = [row for row in self._snapshot()["audit"]
                          if row[0] == "draft_game_discarded"
                          and row not in before["audit"]]
        self.assertEqual(len(discarded_rows), EXPECTED_DRAFTS)


class MemoryExplicitMutationContextTest(ExplicitMutationContextContract,
                                        unittest.TestCase):
    def database_url(self):
        return None                     # the in-memory demo store


class SqliteExplicitMutationContextTest(ExplicitMutationContextContract,
                                        unittest.TestCase):
    def database_url(self):
        # A real FILE, not ":memory:" — the server is threaded, and a
        # file-backed database is both what an operator actually runs and the
        # only SQLite shape where the SERIALIZABLE context read this defect
        # lives under is exercised across connections.
        fd, path = tempfile.mkstemp(suffix=".sqlite")
        os.close(fd)
        self._tmp_path = path
        return path


@unittest.skipUnless(os.environ.get("TEST_DATABASE_URL"),
                     "PostgreSQL not configured (TEST_DATABASE_URL)")
class PostgresExplicitMutationContextTest(ExplicitMutationContextContract,
                                          unittest.TestCase):
    def database_url(self):
        return os.environ["TEST_DATABASE_URL"]


if __name__ == "__main__":
    unittest.main()
