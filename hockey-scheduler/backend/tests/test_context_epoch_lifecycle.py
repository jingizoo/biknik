"""THE SELECTED SEASON'S LIFECYCLE IS PART OF THE EPOCH — an archive or reopen
landing between the context read and the scoped read moves the token and
DISCARDS the in-flight read, exactly as a context switch does (#159).

THE GAP THIS FILE CLOSES has an in-tree paper trail rather than a theory:

* ``tests/test_hierarchy_season_read_only.py`` (merged with the client-freshness
  fix) documents that the hierarchy read and the follow-up read can straddle an
  archive, and that "closing that window needs a server-side binding (a
  version/epoch on the follow-up read), which is deliberately NOT in this
  change".
* The context-switch half of the epoch (``test_context_read_cancel_handoff.py``)
  hashes the ``ActiveContext`` row. Archiving a Season writes
  ``Season.status``/``archived_at`` and touches NO ``ActiveContext`` row, so a
  row-only token is byte-identical either side of the transition — the archive
  is invisible to it by construction, and a read rendered before the archive is
  ADMITTED after it as if nothing had happened.

WHAT "ADMITTED" COSTS, measured on ``origin/main`` (no epoch at all) before this
slice — the same two shapes either side of one archive:

    GET /api/standings/<division>          -> 200, rows byte-identical to the
                                              pre-archive answer: served as if
                                              fresh, with the client still
                                              rendering ``read_only: false``
    GET .../seasons/<id>/venue-candidates  -> the generic 404, indistinguishable
                                              from a page error, for a read the
                                              client dispatched believing the
                                              Season active

Neither answer is wrong in isolation; what is wrong is that the server cannot
tell either read was RENDERED UNDER a context view the archive had superseded.
The fix is the same fact that closes the switch dimension: the selected Season's
lifecycle joins the epoch material (``services/context_epoch.py``,
``_selected_season_lifecycle``), asked of ``season_guard.season_is_read_only``
— the ONE predicate the write refusal, the candidate-read refusal and the
hierarchy's published ``read_only`` already answer to — plus the raw ``status``
and ``archived_at`` beside it, so archive -> reopen -> archive is three distinct
tokens even though the refusal's answer returns to its starting value.

WHAT A DISCARD MUST AND MUST NOT DO, in the lifecycle dimension:

* a stale-epoch read is answered ``204 No Content`` with the service NEVER
  reached — the negative observation is measured (``_watch_service``), not
  inferred from the status code;
* a FRESH epoch, learned after the archive, proceeds to the UNCHANGED ceiling:
  candidates for an archived selected Season still answer the same generic 404,
  and standings still answer THAT Season's rows — archived history stays
  readable (owner contract 1) and the refusal stays byte-identical to main's;
* an absent header behaves exactly as it did before the epoch existed, so no
  client is required to participate;
* the deliberate round trip: archive -> reopen returns ``archived_at`` to None,
  so the PRE-ARCHIVE token matches again and the read is admitted — to the
  exact ceiling it was rendered under, which now answers exactly what the
  render expected. Deliberate, and pinned here so it stays a decision.

EVERY HTTP CASE RUNS ON MEMORY, SQLITE AND POSTGRESQL (the last when
``TEST_DATABASE_URL`` is set), through the same fixture as the switch-dimension
suite — ``ContextReadEpochBase`` carries no test methods, so importing it does
not re-run that file's cases here.
"""

import os
import tempfile
import unittest
from datetime import datetime, timezone

from helpers import BACKEND  # noqa: F401  (ensures sys.path is set up)

from hockey_scheduler.domain.enums import SeasonStatus
from hockey_scheduler.services.context_epoch import context_epoch

from test_context_read_cancel_handoff import (
    _HEX32, _FakeAxis, _FakeSeason, ContextReadEpochBase)
from test_context_switch_server_exit import PATIENCE

REOPEN_REASON = "Late roster corrections approved by the League."


# ==========================================================================
# THE DERIVATION — no HTTP, no threads: the lifecycle is IN the material.
# ==========================================================================
class ContextEpochLifecycleDerivationTest(unittest.TestCase):
    """The properties the lifecycle material adds to the token. Store-free for
    the same reason as the switch-dimension derivation class
    (``ContextEpochDerivationTest``): these are claims about a pure function
    of ``(user_id, generation, program, season, league)``, and all three
    backends must agree on it. ``season`` here is always the EFFECTIVE
    resolved object (or ``None``) a caller already resolved — see that
    class's docstring and ``services/context_epoch.py``'s module docstring
    for why (#159 review finding 2)."""

    USER_ID = "user_a"
    GENERATION = 1
    PROGRAM = _FakeAxis("program_1")
    LEAGUE = _FakeAxis("league_1")
    ARCHIVED_AT = datetime(2026, 8, 13, 7, 0, 0, 1, tzinfo=timezone.utc)

    def _token(self, season):
        return context_epoch(self.USER_ID, self.GENERATION, self.PROGRAM,
                             season, self.LEAGUE)

    def test_archiving_the_selected_season_moves_the_token(self):
        """THE HEADLINE. The user/generation/Program/League are identical on
        both sides — only the Season's lifecycle moved. A token that ignored
        the lifecycle would be byte-identical here, which is precisely the
        measured blindness this slice closes: the archive was invisible, so
        the stale read was admitted."""
        active = self._token(_FakeSeason("season_1", SeasonStatus.ACTIVE))
        archived = self._token(
            _FakeSeason("season_1", SeasonStatus.ARCHIVED, self.ARCHIVED_AT))
        self.assertRegex(active, _HEX32)
        self.assertNotEqual(
            active, archived,
            "archiving the selected Season did not move the token, so a read "
            "rendered before the archive would be admitted after it — the "
            "lifecycle is not in the epoch material")

    def test_reopening_moves_it_again_and_a_full_round_trip_restores_it(self):
        """Archive -> reopen is TWO moves, and active -> archived -> active is
        a DELIBERATE restore: ``reopen_season`` clears ``archived_at``, so the
        two active states are byte-identical and a read rendered before the
        archive is readmitted — to the exact ceiling it was rendered under.
        The GENERATION axis differs (it moves on every context WRITE, and
        archiving/reopening a Season is not one); the lifecycle axis rounds a
        trip on purpose."""
        active = self._token(_FakeSeason("season_1", SeasonStatus.ACTIVE))
        archived = self._token(
            _FakeSeason("season_1", SeasonStatus.ARCHIVED, self.ARCHIVED_AT))
        reopened = self._token(_FakeSeason("season_1", SeasonStatus.ACTIVE))
        self.assertNotEqual(archived, reopened,
                            "the reopen did not move the token")
        self.assertEqual(active, reopened,
                         "a full archive -> reopen round trip must restore "
                         "the token: the deliberate readmission is a decision, "
                         "not an accident, and this pins it")

    def test_two_archives_of_the_same_season_are_distinct_states(self):
        """``status`` alone rounds archive -> reopen -> archive back to its
        first value; ``archived_at`` is what distinguishes the two archived
        states. Hashing it is deliberately WIDER than the refusal consults — a
        field that moves without the refusal moving can only cause a DISCARD,
        never a serve, so erring wide fails in the safe direction."""
        first = self._token(
            _FakeSeason("season_1", SeasonStatus.ARCHIVED, self.ARCHIVED_AT))
        second = self._token(_FakeSeason(
            "season_1", SeasonStatus.ARCHIVED,
            self.ARCHIVED_AT.replace(microsecond=2)))
        self.assertNotEqual(
            first, second,
            "two distinct archives hash identically, so a read rendered "
            "under the first archived state would be admitted in the second")

    def test_no_season_is_a_well_defined_state_distinct_from_any_real_one(self):
        """Program-only selection is a legitimate, first-class effective
        resolution. Its token is well-defined and stable, and distinct from
        every real Season's — including one sharing the same fixture's other
        axes. (A raw saved id that fails to resolve is no longer a state THIS
        function can even be asked about: the caller already collapsed it to
        ``None`` before calling here — see ``ContextService._resolve_locked``
        — which is the fix for #159 review finding 2's exact gap: the epoch
        used to be blind to an EFFECTIVE resolution changing underneath an
        unchanged raw row.)"""
        no_season = self._token(None)
        live = self._token(_FakeSeason("season_1", SeasonStatus.ACTIVE))
        self.assertRegex(no_season, _HEX32)
        self.assertNotEqual(no_season, live,
                            "a Program-only resolution hashes like a live "
                            "Season, so Program-only is invisible to the "
                            "epoch")
        # Deterministic — a discard-and-retry must converge.
        self.assertEqual(no_season, self._token(None))

    def test_a_malformed_season_object_fails_loudly(self):
        """``_season_lifecycle_fields`` reads ``season.status`` through
        ``season_guard.season_is_read_only`` UNGUARDED. This module no longer
        touches a store at all (#159 review finding 2 moved resolution to
        ``ContextService``), so the analogous silent-blindness risk moved
        from 'a store without get_season' to 'a season object missing the
        field the decision needs' — a confident-looking token computed from a
        malformed season would be blind to whatever transition the caller
        failed to resolve correctly, and that must be loud, not silent."""
        class _Malformed:
            pass  # no .status at all

        with self.assertRaises(AttributeError):
            context_epoch(self.USER_ID, self.GENERATION, self.PROGRAM,
                         _Malformed(), self.LEAGUE)


# ==========================================================================
# THE WIRING — real server, real sessions, real archive/reopen over HTTP.
# ==========================================================================
class ContextEpochLifecycleBase(ContextReadEpochBase):
    """The lifecycle dimension of the epoch, through the whole request path.
    Reuses the switch-dimension fixture (real ``ThreadingHTTPServer``, real
    cookies, the arrival park, the service watch, the gate assertions) so the
    two dimensions cannot drift apart about what a scoped read is."""

    def _archive(self, client, season_id):
        status, raw, _ = self._req(
            client, "POST", f"/api/v2/setup/seasons/{season_id}/archive", {})
        self.assertEqual(status, 200, raw)

    def _reopen(self, client, season_id):
        status, raw, _ = self._req(
            client, "POST", f"/api/v2/setup/seasons/{season_id}/reopen",
            {"reason": REOPEN_REASON})
        self.assertEqual(status, 200, raw)

    def _selected_context(self, client):
        status, raw, body = self._req(client, "GET", "/api/context")
        self.assertEqual(status, 200, raw)
        return body

    def _fixture_on_s1(self, tag):
        """One operator selected on (Program, S1) with a granted Venue and a
        standings-bearing Division in S1 — every read this file issues answers
        200 RIGHT NOW, so a later 204 or 404 cannot be explained by the
        fixture, the route or the role."""
        fx = self._program_with_two_seasons(tag)
        fx["division_id"] = self._division_with_teams(fx, fx["s1"], tag=tag)
        username, user_id = self._operator(tag.lower())
        client = self._login(username)
        self._select(client, fx["program_id"], fx["s1"])
        ctx = self._selected_context(client)
        self.assertEqual(ctx.get("season_id"), fx["s1"])
        self.assertFalse(ctx.get("read_only"),
                         "the fixture Season must start live")
        return fx, user_id, client

    # ======================================================================
    # 1. THE DEFECT: an archive between the reads is invisible to a
    #    row-only epoch.
    # ======================================================================
    def test_an_archive_between_the_context_read_and_the_scoped_read_discards(self):
        """THE HEADLINE, on the wire. The client learns its context (epoch E,
        ``read_only: false``); the Season is archived — a second tab, another
        operator, this operator; the scoped read arrives still echoing E.

        RED while the epoch hashed only the ``ActiveContext`` row: the archive
        touches no such row, E still matched, the read was ADMITTED, and the
        unchanged ceiling answered the generic 404 — a bare page error for a
        question the client asked in good faith under a view the server itself
        had rendered moments before. GREEN with the lifecycle in the material:
        ``204``, service never reached, and the client re-reads its context.
        """
        for kind in self.VENUE_ROUTES:
            with self.subTest(route=kind):
                fx, user_id, client = self._fixture_on_s1(
                    f"LcArc{kind[:5].title()}")
                epoch = self._epoch_from_api(client)
                baseline, raw, _ = self._scoped_read(
                    client, fx["s1"], kind, epoch)
                self.assertEqual(baseline, 200,
                                 f"the epoch-carrying read must work before "
                                 f"the archive: {raw}")

                self._archive(client, fx["s1"])

                service = self._watch_service(self._SERVICE_FOR[kind])
                status, raw, body = self._scoped_read(
                    client, fx["s1"], kind, epoch)
                self.assertEqual(
                    status, 204,
                    f"a {kind} read rendered under the pre-archive context "
                    f"view was answered {status} — the archive between the "
                    f"context read and the scoped read is invisible to the "
                    f"epoch: {raw!r}")
                self.assertEqual(raw, "", "a discard must carry no body")
                self.assertEqual(
                    [s for s in service if s == fx["s1"]], [],
                    "the stale read reached ApiService, so the ceiling WAS "
                    "evaluated for it; the discard must short-circuit in "
                    "front of the ceiling")
                self._assert_gate_is_clean(f"after the {kind} discard")

    def test_standings_across_an_archive_are_discarded_not_served_as_fresh(self):
        """The OTHER wrong answer, measured on main before this slice: the
        stale-view standings read is served ``200`` with rows byte-identical
        to the pre-archive answer — as if fresh — while the client still
        renders ``read_only: false`` and offers write affordances against a
        Season that refuses writes. With the lifecycle in the material it is
        discarded like every other stale read, and the re-read that follows
        carries ``read_only: true`` beside the new epoch in ONE payload."""
        fx, user_id, client = self._fixture_on_s1("LcStand")
        epoch = self._epoch_from_api(client)
        baseline, raw, before = self._req(
            client, "GET", f"/api/standings/{fx['division_id']}",
            headers={"X-Context-Epoch": epoch})
        self.assertEqual(baseline, 200, raw)
        self.assertTrue(before.get("standings"),
                        "the fixture must produce non-empty standings")

        self._archive(client, fx["s1"])

        service = self._watch_service("get_standings")
        status, raw, body = self._req(
            client, "GET", f"/api/standings/{fx['division_id']}",
            headers={"X-Context-Epoch": epoch})
        self.assertEqual(
            status, 204,
            f"standings rendered under the pre-archive view were answered "
            f"{status} as if fresh — the archive is invisible: {raw!r}")
        self.assertEqual(
            [s for s in service if s == fx["division_id"]], [],
            "the stale standings read reached ApiService")
        # The recovery the discard prescribes: re-read the context, get the
        # read-only fact and the new epoch TOGETHER, re-issue, get history.
        ctx = self._selected_context(client)
        self.assertTrue(ctx.get("read_only"),
                        "the re-read context must say read-only")
        self.assertNotEqual(ctx.get("context_epoch"), epoch,
                            "the re-read context must carry a NEW epoch")
        status, raw, after = self._req(
            client, "GET", f"/api/standings/{fx['division_id']}",
            headers={"X-Context-Epoch": ctx["context_epoch"]})
        self.assertEqual(status, 200, raw)
        self.assertEqual(after.get("standings"), before.get("standings"),
                         "archived history must reproduce the same rows "
                         "(owner contract 1), just under the fresh epoch")

    def test_an_in_flight_read_parked_at_arrival_is_discarded_by_an_archive(self):
        """The forced interleaving, lifecycle edition: the read is DISPATCHED
        under the live Season's epoch, parked above the gate (no ticket held —
        asserted), the ARCHIVE commits while it is parked, and the read then
        proceeds. Dispatched-before, arrived-after — the exact class the
        switch dimension already covers, driven here by ``archive_season``,
        which holds no gate ticket and touches no ``ActiveContext`` row."""
        fx, user_id, client = self._fixture_on_s1("LcPark")
        # The archive arrives on a SECOND connection of the same operator — a
        # second tab. (Another operator would work identically; the epoch is
        # compared against the reader's own persisted row and selected Season,
        # and the Season is shared state.)
        archiver = client
        epoch = self._epoch_from_api(client)
        kind = "venue-candidates"
        service = self._watch_service(self._SERVICE_FOR[kind])
        path = f"/api/v2/setup/seasons/{fx['s1']}/{kind}"
        out = {}
        with self._arrival_park(path) as (park, state):
            thread = self._read_thread(client, fx["s1"], kind, epoch, out)
            self.assertTrue(park.arrived.wait(PATIENCE),
                            "the read never reached the server")
            self._assert_no_ticket_while_parked("while the read was parked")

            self._archive(archiver, fx["s1"])
            season = self.api.store.get_season(fx["s1"])
            self.assertEqual(season.status, SeasonStatus.ARCHIVED,
                             "the archive did not commit while the read was "
                             "held — this case measured nothing")

            park.let_go()
            thread.join(PATIENCE)

        self.assertIn("result", out, "the parked read never returned")
        status, raw, _ = out["result"]
        self.assertEqual(
            status, 204,
            f"the in-flight read dispatched before the archive was answered "
            f"{status} after it: {raw!r}")
        self.assertEqual([s for s in service if s == fx["s1"]], [],
                         "the discarded read reached ApiService")
        self.assertTrue(state["reached_gate"],
                        "the parked read never continued into do_GET")
        self._assert_gate_is_clean("after the parked-read discard")

    def test_the_epoch_moves_on_archive_and_again_on_reopen(self):
        """The client-visible statement of the whole mechanism: three context
        reads — live, archived, reopened — must carry three DISTINCT epochs,
        with the reopened one equal to the live one (the deliberate round
        trip, asserted at the derivation level too). A row-only epoch answers
        all three identically, which is the measured blindness."""
        fx, user_id, client = self._fixture_on_s1("LcMoves")
        live = self._epoch_from_api(client)
        self._archive(client, fx["s1"])
        archived = self._epoch_from_api(client)
        self.assertNotEqual(live, archived,
                            "the archive did not move the epoch")
        self._reopen(client, fx["s1"])
        reopened = self._epoch_from_api(client)
        self.assertNotEqual(archived, reopened,
                            "the reopen did not move the epoch")
        self.assertEqual(live, reopened,
                         "archive -> reopen must restore the pre-archive "
                         "epoch: the readmission is deliberate")

    # ======================================================================
    # 2. THE CONTROLS: what a discard must NOT change.
    # ======================================================================
    def test_a_fresh_epoch_after_the_archive_proceeds_to_the_unchanged_ceiling(self):
        """MATCH IS NOT SERVE. A client that re-read its context after the
        archive echoes the CURRENT epoch; its reads are admitted to exactly
        the ceiling main has today: candidates for an archived selected Season
        answer the same generic 404 (the offer-to-mutate is refused — the
        grant it feeds would fail ``season_archived``), and standings answer
        THAT Season's rows, because archived history stays readable."""
        fx, user_id, client = self._fixture_on_s1("LcFresh")
        self._archive(client, fx["s1"])
        fresh = self._epoch_from_api(client)

        service = self._watch_service(self._SERVICE_FOR["venue-candidates"])
        status, raw, body = self._scoped_read(
            client, fx["s1"], "venue-candidates", fresh)
        self.assertEqual(status, 404,
                         f"the archived-selected-Season refusal must be "
                         f"byte-identical to main's: {raw}")
        self.assertEqual(body.get("error", {}).get("code"), "not_found", raw)
        self.assertNotIn(fx["tag"], raw,
                         "the generic refusal leaked a fixture name")
        self.assertTrue([s for s in service if s == fx["s1"]],
                        "the fresh-epoch read never reached ApiService, so "
                        "the 404 did not come from the ceiling")

        status, raw, body = self._req(
            client, "GET", f"/api/standings/{fx['division_id']}",
            headers={"X-Context-Epoch": fresh})
        self.assertEqual(status, 200,
                         f"archived history must stay readable under a fresh "
                         f"epoch (owner contract 1): {raw}")
        self.assertTrue(body.get("standings"), raw)

    def test_an_absent_header_across_an_archive_behaves_exactly_as_main(self):
        """No client is required to participate: a read with NO epoch header
        issued after the archive gets main's exact behaviour — the generic 404
        for candidates, THAT Season's rows for standings — so a pre-epoch
        client is unimproved but never worse off. This is the control that
        keeps every 204 above falsifiable: withhold the header and the old
        answers must come back."""
        fx, user_id, client = self._fixture_on_s1("LcAbsent")
        self._archive(client, fx["s1"])

        status, raw, body = self._scoped_read(
            client, fx["s1"], "venue-candidates", epoch=None)
        self.assertEqual(status, 404, raw)
        self.assertEqual(body.get("error", {}).get("code"), "not_found", raw)

        status, raw, body = self._req(
            client, "GET", f"/api/standings/{fx['division_id']}")
        self.assertEqual(status, 200, raw)
        self.assertTrue(body.get("standings"), raw)

    def test_the_pre_archive_epoch_is_readmitted_after_a_full_round_trip(self):
        """THE DELIBERATE READMISSION, end to end: a read rendered under the
        live Season, delayed across an archive AND its reopen, is admitted —
        the Season it was rendered under is back, ``archived_at`` is None
        again, and the ceiling answers exactly what the render expected. If a
        later change makes the lifecycle material one-way (a generation
        counter, a hashed audit id), this goes red and the change has to be
        argued for on the record."""
        fx, user_id, client = self._fixture_on_s1("LcRound")
        live = self._epoch_from_api(client)
        self._archive(client, fx["s1"])
        self._reopen(client, fx["s1"])
        status, raw, body = self._scoped_read(
            client, fx["s1"], "venue-candidates", live)
        self.assertEqual(status, 200,
                         f"the round-trip readmission is deliberate and must "
                         f"stay: {raw}")
        self.assertIn("candidates", body, raw)


class MemoryContextEpochLifecycleTest(ContextEpochLifecycleBase,
                                      unittest.TestCase):
    STORE_URL = None


class SqliteContextEpochLifecycleTest(ContextEpochLifecycleBase,
                                      unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fd, path = tempfile.mkstemp(suffix=".db", prefix="hs_ctxlife_")
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
class PostgresContextEpochLifecycleTest(ContextEpochLifecycleBase,
                                        unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.STORE_URL = os.environ["TEST_DATABASE_URL"]
        super().setUpClass()


if __name__ == "__main__":
    unittest.main(verbosity=2)
