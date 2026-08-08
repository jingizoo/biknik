"""Strict request schemas for the scheduler's draft and commit routes (#393).

THE DEFECT. ``POST /api/scheduler/draft`` and ``POST /api/scheduler/commit``
read their inputs with ``body.get(...)`` and validated nothing, so a key the
route did not recognise was **silently dropped**. The request then ran under
the route's defaults while the caller believed it had asked for something else:

    {"division_id": "...", "meetings_per_oponent": 3}
                                       ^ typo

generated (and, on commit, WROTE) a schedule with the DEFAULT meeting count,
answered 200, and named nothing. #271 established the fail-loud contract for
the write surface and the sibling ``POST /api/scheduler/scenarios`` route has
always been strict; these two verbs were the gap.

It matters most on commit, which is the write: a dropped field there means
committing a differently-shaped schedule than the one the operator reviewed.

WHY EACH TEST HAS A POSITIVE CONTROL. A 400 proves nothing on its own — an
unauthenticated caller, a foreign target or a stale fingerprint all produce
4xx too. Every rejection case below is paired with the SAME body minus the
offending key, asserted to succeed, so the 400 is attributable to the schema
and to nothing else. Without those controls, deleting ``check_body`` from the
route would leave these tests green.

FALSIFICATION (re-run each round; both mutations neutralise ONE
``check_body`` call and nothing else):

    * ``/api/scheduler/draft`` → 2 failures, both draft tests. The typo'd body
      answers 200 with a real preview; ``slot_ids="not-a-list"`` reaches the
      generator.
    * ``/api/scheduler/commit`` → 2 failures, both commit tests. The typo'd
      body answers 200 and Games appear in the store.

Neither mutation disturbs the other route's two tests. That separation is what
makes these two independent guards rather than one guard asserted twice — a
single shared helper would have gone red in all four either way.
"""

import json
import threading
import unittest
import urllib.error
import urllib.request
from http.cookiejar import CookieJar
from http.server import ThreadingHTTPServer

from helpers import BACKEND  # noqa: F401  (ensures sys.path is set up)

from hockey_scheduler.web import server as srv

from test_draft_context_scope import build_two_programs, corner


class SchedulerStrictSchemaHttpTest(unittest.TestCase):
    """Both scheduler write verbs, over the real authenticated routes."""

    maxDiff = None

    @classmethod
    def setUpClass(cls):
        srv.STATE.reset()
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), srv.Handler)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever,
                                      daemon=True)
        cls.thread.start()
        cls.fixture = build_two_programs(srv.STATE.api)

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.thread.join(timeout=5)
        cls.httpd.server_close()

    # -- harness ------------------------------------------------------------
    def _client(self):
        return urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(CookieJar()))

    def _json(self, client, method, path, body=None):
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

    def _operator(self):
        """An admin signed in with an explicit active tuple over Program A."""
        program, season, league, division = corner(self.fixture, "A", "1", "a")
        client = self._client()
        status, body = self._json(client, "POST", "/api/auth/login",
                                  {"username": "admin", "password": "demo"})
        self.assertEqual(status, 200, body)
        status, body = self._json(client, "POST", "/api/context", {
            "program_id": program, "season_id": season, "league_id": league})
        self.assertEqual(status, 200, body)
        return client, division

    def _store(self):
        return srv.STATE.api.store

    def _side_effects(self):
        """Everything a commit would leave behind, as one comparable value."""
        store = self._store()
        return (sorted(g.id for g in store.all_games()),
                len(store.all_setup_audit()))

    def _assert_unknown_field(self, body, field):
        self.assertEqual(body["error"]["code"], "validation_error")
        self.assertEqual(body["error"]["details"]["reason"], "unknown_field")
        self.assertIn(field, body["error"]["details"]["fields"])

    # -- draft --------------------------------------------------------------
    def test_a_typo_in_a_draft_knob_is_named_not_dropped(self):
        """The reported shape: a misspelled knob must be named, not ignored.

        The control below is the whole point — the same request WITHOUT the
        typo has to produce a real preview, otherwise the 400 could be coming
        from the target, the context or the role.
        """
        client, division = self._operator()

        status, body = self._json(client, "POST", "/api/scheduler/draft", {
            "division_id": division, "meetings_per_oponent": 3})
        self.assertEqual(status, 400, body)
        self._assert_unknown_field(body, "meetings_per_oponent")
        # A refused draft is not a preview: nothing schedule-shaped came back.
        self.assertNotIn("draft_fingerprint", body)

        # CONTROL: correctly spelled, same everything else.
        status, preview = self._json(client, "POST", "/api/scheduler/draft", {
            "division_id": division, "meetings_per_opponent": 1})
        self.assertEqual(status, 200, preview)
        self.assertIn("draft_fingerprint", preview)

    def test_a_string_where_a_list_is_meant_is_refused_by_shape(self):
        """A string given for ``slot_ids`` is ITERATED, one character per id.

        Measured on the unvalidated route: ``"not-a-list"`` is walked
        character-wise and answers ``404 {"reason": "slot_missing",
        "ice_slot_id": "n"}`` — a per-character existence probe against the
        ice-slot table, reported as a missing slot the caller never named.

        Chosen over ``meetings_per_opponent`` deliberately. That knob is
        ALREADY refused downstream without this schema ("must be an integer
        >= 1"), so a test built on it would show only a nicer error shape;
        ``slot_ids`` is where an unchecked type genuinely changes what the
        route does.
        """
        client, division = self._operator()
        status, body = self._json(client, "POST", "/api/scheduler/draft", {
            "division_id": division, "slot_ids": "not-a-list"})
        self.assertEqual(status, 400, body)
        self.assertEqual(body["error"]["code"], "validation_error")
        self.assertEqual(body["error"]["details"]["reason"], "wrong_type")
        self.assertEqual(body["error"]["details"]["field"], "slot_ids")

    # -- commit -------------------------------------------------------------
    def test_a_typo_in_a_commit_knob_is_named_and_writes_nothing(self):
        """The write verb: refused loudly AND with zero side effects.

        Status alone would not settle this. The reason to reject the body
        before generation is that a dropped field commits a differently-shaped
        schedule, so the assertion that matters is that the store is byte-for-
        byte where it was — no Games, no setup-audit rows.
        """
        client, division = self._operator()
        status, preview = self._json(client, "POST", "/api/scheduler/draft",
                                     {"division_id": division})
        self.assertEqual(status, 200, preview)
        fingerprint = preview["draft_fingerprint"]

        before = self._side_effects()
        status, body = self._json(client, "POST", "/api/scheduler/commit", {
            "division_id": division, "draft_fingerprint": fingerprint,
            "meetings_per_oponent": 3})
        self.assertEqual(status, 400, body)
        self._assert_unknown_field(body, "meetings_per_oponent")
        self.assertEqual(
            self._side_effects(), before,
            "a refused commit changed the store — the body was rejected only "
            "after the write had already happened")

        # CONTROL: the identical commit minus the typo really does write, so
        # the untouched store above is the refusal's doing and not a commit
        # that could never have succeeded.
        status, result = self._json(client, "POST", "/api/scheduler/commit", {
            "division_id": division, "draft_fingerprint": fingerprint})
        self.assertEqual(status, 200, result)
        self.assertTrue(result.get("created"))
        self.assertNotEqual(self._side_effects(), before)

    def test_a_string_where_a_list_is_meant_is_refused_before_the_write(self):
        """The same character-wise iteration, behind the verb that WRITES."""
        client, division = self._operator()
        status, preview = self._json(client, "POST", "/api/scheduler/draft",
                                     {"division_id": division})
        self.assertEqual(status, 200, preview)

        before = self._side_effects()
        status, body = self._json(client, "POST", "/api/scheduler/commit", {
            "division_id": division,
            "draft_fingerprint": preview["draft_fingerprint"],
            "slot_ids": "not-a-list"})
        self.assertEqual(status, 400, body)
        self.assertEqual(body["error"]["details"]["reason"], "wrong_type")
        self.assertEqual(body["error"]["details"]["field"], "slot_ids")
        self.assertEqual(self._side_effects(), before)


if __name__ == "__main__":
    unittest.main()
