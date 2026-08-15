"""Round-N review finding 1, the REAL-HTTP half: the version-counter check
(``SqlStore``/``InMemoryStore.epoch_fence_acquire_exclusive`` bumping a
persisted counter; ``web/server.py``'s ``_read_under_context_gate`` sampling
it before/after) proven through the ACTUAL production path -- a real
``ThreadingHTTPServer``, a real session, a real scoped-read route -- rather
than only the store-level reproduction in ``test_epoch_fence.py``.

WHY A SEPARATE FILE FROM ``test_epoch_fence.py``. That file's falsifiability
mixin drives ``SqlStore``/``InMemoryStore`` methods directly (matching design
§10.6's own "reproduce the exact CI-observed defect" framing); this file
drives the same mechanism through ``Handler.do_GET`` -> ``_read_under_context_
gate`` -> the real ``ApiService`` method, so the claim covers the WIRING
between the store primitive and the HTTP layer, not only the primitive
itself. Reuses ``ContextGateFixtureBase`` (``test_context_switch_server_
exit.py``) for the server/session/park machinery, exactly as ``test_context_
read_cancel_handoff.py`` already does for the epoch mechanism's own HTTP
coverage.

WHAT "ZERO-CALL SERVICE SPY" MEANS FOR THIS DESIGN, stated precisely rather
than assumed. The review's finding 1 guidance offers two possible closures:
"implement backend-safe ordering ... OR atomically validate the same
persisted version inside each dependent read before exposing a result" --
this PR took the SECOND path for SQLite/Memory specifically because the
FIRST (a real lock held across ``produce()``) was measured to deadlock there
(see ``epoch_fence_acquire_shared``'s own docstring). The version-check
design's whole point is that ``produce()`` DOES run -- there is no lock to
stop it, by design, which is what avoids the deadlock -- and what is thrown
away afterward is the RESPONSE, not the call. So the spy assertion here is
not "called zero times" (which would be FALSE for this design, and asserting
it would make this file lie about what it proved); it is "the service call
provably ran to completion under torn state, and the CLIENT still received
204 with nothing describing that state" -- the actual guarantee the version
check buys, backed by watching the real ``ApiService`` method execute.
"""

import os
import threading
import time
import unittest
import uuid

from helpers import BACKEND  # noqa: F401  (ensures sys.path is set up)

from hockey_scheduler.services.context_epoch import CONTEXT_EPOCH_HEADER
from test_context_switch_server_exit import (
    PATIENCE, ContextGateFixtureBase)


class EpochFenceVersionCheckHttpBase(ContextGateFixtureBase):
    """Fixture-derived cases only — subclasses below set ``STORE_URL``."""

    def _epoch_for(self, client):
        _status, _raw, body = self._req(client, "GET", "/api/context")
        return body["context_epoch"]

    def test_a_torn_read_is_discarded_not_served(self):
        """THE REAL-HTTP REPRO. An operator selects Season S1 and renders
        under epoch E0. A ``GET .../venue-candidates`` for S1, echoing E0, is
        parked by the test INSIDE ``ApiService.get_venue_grant_candidates`` --
        i.e. AFTER ``_read_under_context_gate`` already compared E0 to the
        CURRENT epoch and found it MATCHING (produce() is only ever called on
        a match), so this is exactly the window the version check exists to
        protect: state that was fresh at the moment of the check, observed
        again later inside the dependent read.

        While parked, the SAME operator (whose active context IS S1, so the
        write is genuinely authorized -- not a scope refusal) archives S1
        through the real guarded-mutation path, which is one of the 17 fenced
        writers and bumps the persisted version counter on EVERY backend
        (Memory included, unconditionally -- see ``epoch_fence_acquire_
        exclusive``'s own docstring). Releasing the park lets ``produce()``
        finish; the service spy proves it genuinely ran and genuinely
        observed the archived Season (a fact ``get_venue_grant_candidates``
        would otherwise turn into its own generic 404 for an archived
        selection) — but the client never sees that 404, or any other
        content: ``_read_under_context_gate``'s post-``produce()`` version
        sample disagrees with its pre-check sample, so the response actually
        written to the socket is the SAME 204 an outright epoch mismatch
        produces.
        """
        fx = self._program_with_two_seasons("Torn" + uuid.uuid4().hex[:6])
        username, user_id = self._operator("torn")
        client = self._login(username)
        self._select(client, fx["program_id"], fx["s1"])
        epoch = self._epoch_for(client)

        service_calls = []

        def spy_factory(original):
            def wrapper(sid, *a, **kw):
                service_calls.append(sid)
                return original(sid, *a, **kw)
            return wrapper

        self._wrap(self.api, "get_venue_grant_candidates", spy_factory)

        with self._read_parked_in(
                "get_venue_grant_candidates", fx["s1"]) as (park, exited):
            result = {}

            def do_read():
                result["response"] = self._req(
                    client, "GET",
                    f"/api/v2/setup/seasons/{fx['s1']}/venue-candidates",
                    headers={CONTEXT_EPOCH_HEADER: epoch})

            t = threading.Thread(target=do_read, daemon=True)
            t.start()
            self.assertTrue(park.arrived.wait(PATIENCE),
                            "the read never reached produce() -- the park "
                            "seam did not fire")
            self.assertEqual(
                service_calls, [],
                "the service must not have run YET -- it is parked at its "
                "own top, before any row read")

            # The SAME operator, authorized via its own active-context match
            # on S1 -- archive_season is design §3 row 4/global-keyed.
            payload, refused = self.api.setup_guarded_mutation(
                [("season", fx["s1"], "scope")],
                lambda: self.api.setup.archive_season(
                    fx["s1"], reason="torn-read HTTP repro",
                    actor_id=user_id),
                user_id, *self._operator_role())
            self.assertIsNone(refused, (payload, refused))
            self.assertFalse(
                isinstance(payload, dict) and "error" in payload,
                f"the archive itself must succeed, or nothing was proven: "
                f"{payload}")

            park.let_go()
            t.join(PATIENCE)

        self.assertIn("at", exited, "produce() never exited")
        self.assertEqual(
            service_calls, [fx["s1"]],
            "the service call must have genuinely run exactly once -- this "
            "design does not (and structurally cannot, without "
            "reintroducing the SQLite/Memory deadlock class) prevent "
            "produce() from being invoked; it discards the RESPONSE, not "
            "the call")
        status, raw, body = result["response"]
        self.assertEqual(
            status, 204,
            f"a response computed from torn state must never reach the "
            f"client: {raw}")
        self.assertEqual(raw, "", f"a 204 must carry no body: {raw!r}")
        self.assertNotIn("venue", raw.lower())
        self.assertNotIn(fx["s1"].lower(), raw.lower())

    def _operator_role(self):
        from hockey_scheduler.domain import Role
        return Role.LEAGUE_ADMIN, {}


class MemoryEpochFenceVersionCheckHttpTest(
        EpochFenceVersionCheckHttpBase, unittest.TestCase):
    STORE_URL = None


class SqliteEpochFenceVersionCheckHttpTest(
        EpochFenceVersionCheckHttpBase, unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import tempfile
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        os.remove(path)
        cls._sqlite_path = path
        cls.STORE_URL = path
        super().setUpClass()

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        try:
            os.remove(cls._sqlite_path)
        except OSError:
            pass


if __name__ == "__main__":
    unittest.main()
