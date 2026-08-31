"""Round-N+1 finding 1 REWORK, the REAL-HTTP half: a genuine PRE-``produce()``
gate (``services/context_gate.py``'s ``CONTEXT_GATE``/``LIFECYCLE_GATE``, now
wired to all 17 writers, plus, on file-backed SQLite, an additional
independent-``SqlStore`` mechanism — ``web/server.py``'s
``_read_under_context_gate``/``_read_under_context_gate_sqlite``) proven
through the ACTUAL production path: a real ``ThreadingHTTPServer``, a real
session, a real scoped-read route, a real spy on the actual ``ApiService``
scoped-read method.

WHY THIS FILE WAS REWRITTEN, NOT MERELY EXTENDED. The round-N version of this
file asserted the OPPOSITE of what round-N+1 requires: its one case PARKED THE
READER mid-``produce()`` and asserted the racing writer's service call
"must have genuinely run exactly once -- this design does not (and
structurally cannot...) prevent produce() from being invoked; it discards the
RESPONSE, not the call". That was an honest description of the round-N
version-counter-only design, and the owner's follow-up correction is exact:
"a post-hoc discard is not closure". Once every writer takes the SAME gate a
scoped read already holds SHARED across its whole window (round-N+1's actual
fix), that OLD test's own premise stops being reachable — parking the READER
first now just reproduces #415's ordinary, CORRECT "read admitted ahead of
the writer" case (the writer BLOCKS, the read is served with pre-write data,
nothing is torn). The genuinely interesting case is now the other arrival
order: the WRITER already holds the gate's EXCLUSIVE side (mid-transaction,
parked here to make the window observable) when the READ arrives echoing a
now-stale epoch — the read must BLOCK behind the writer, and once released,
its freshly-derived epoch must mismatch and discard BEFORE ``produce()`` is
ever reached. That is what every case below proves, with a real spy showing
ZERO calls, not merely a 204.

SCOPE, stated precisely. The review's original ask is 5 CONTEXT_SCOPED_READ_
ROUTES x {context-switch, selected-Season-delete, authorization-withdrawal,
archive-reopen} x every backend:

* context-switch (row 1), selected-Season-delete (row 6) and archive (rows
  4/5) are each run against ALL FIVE routes, Memory AND file-backed SQLite —
  the full matrix for those three dimensions.
* authorization-withdrawal is proven against BOTH routes an Official can
  actually reach (``standings-division`` and ``standings-league-season``) --
  NOT the other three (venue-candidates, venue-access, scenario), which are
  ALL `_operator_only`-gated to MANAGE_SETUP even as GETs (confirmed
  directly against `web/server.py`'s own dispatch, not assumed -- so an
  Official reading any of the three gets a bare 403 before ever reaching
  the scoped-read machinery this file exercises, at which point a race
  would prove nothing) -- Memory AND SQLite, using ``unassign_official``
  (row 14) -- literally, per that method's own docstring, "an authorization
  WITHDRAWAL for the affected Official's own scoped reads". This needs a
  DIFFERENT acting/reading identity (the Official themselves, not a League
  Admin: `context_scope.authorized_program_ids` for `Role.OFFICIAL` is
  derived from `store.assignments_for_official`, so it is the affected
  party's OWN read that has to race the unassignment, not an admin's) and
  its own multi-record fixture (account + official + game + assignment,
  rebuilt fresh per route since the writer under test consumes the one
  assignment it races), so it covers every route an Official can reach
  rather than every route in the abstract -- the three operator-only routes
  are structurally out of THIS scenario's reach, not merely unexercised.
  reopen is not separately run: it
  takes the identical `LIFECYCLE_GATE`/`_guarded_attempt` path as archive
  (same target kind, same gate key), which `tests/test_epoch_fence.py`'s
  `EpochFenceWriterOrderingTest.test_row5_season_reopen` already confirms at
  the fence-acquisition level.
* reopen (row 5), the twelve remaining LIFECYCLE_GATE-routed writers (rows
  7-13, 15-17) and PostgreSQL are NOT re-proven at the HTTP zero-call level
  here: PostgreSQL's zero-call guarantee rests on its OWN, already-reviewed
  real-advisory-lock mechanism (`epoch_fence_acquire_shared`/`_exclusive`),
  covered by `tests/test_epoch_fence_timeout.py`'s existing real-spy proof
  and `tests/test_epoch_fence.py`'s Pg falsifiability class -- a DIFFERENT
  mechanism than this file exercises, not a gap in this one. The remaining
  in-process-gate rows are covered at the STORE level by
  `tests/test_epoch_fence.py`'s `EpochFenceWriterOrderingTest` (every writer
  takes the fence) and are the SAME `LIFECYCLE_GATE.exclusive(...)` primitive
  this file already exercises end-to-end for six of the seventeen rows
  (1, 4, 5's sibling reopen, 6, 14) across two backends -- extending this
  file's own HTTP+multi-actor fixture machinery to the other eleven is
  flagged as follow-on coverage, not silently declared complete.
"""

import threading
import time
import unittest
import uuid
from contextlib import contextmanager

from helpers import BACKEND  # noqa: F401  (ensures sys.path is set up)

from hockey_scheduler.domain import OfficialRole, Role, SeasonStatus
from hockey_scheduler.domain.models import Game
from hockey_scheduler.services.context_epoch import CONTEXT_EPOCH_HEADER
from test_context_read_cancel_handoff import ContextReadEpochBase
from test_context_switch_server_exit import PATIENCE, _Park

ADMIN = (Role.LEAGUE_ADMIN, {})

# How long a case waits, after starting the racing read, to confirm it has
# NOT yet returned -- i.e. that it is genuinely blocked behind the parked
# writer's still-open exclusive hold, not merely fast. Short (this is a
# negative check bounding a real wait, not the SUT's own timeout) but well
# above scheduling noise.
_BLOCKED_CHECK = 0.25


class EpochFenceZeroCallHttpBase(ContextReadEpochBase):
    """Fixture-derived cases only -- subclasses below set ``STORE_URL``.

    Inherits ``ContextReadEpochBase`` (fixture/seams only, no test methods —
    see that class's own docstring) for ``_program_with_two_seasons``,
    ``_operator``, ``_select``, ``_league_with_teams``,
    ``_division_with_teams``, ``_scenario_in`` and the HTTP/session helpers,
    so this file's routes cannot silently drift from
    ``test_context_read_cancel_handoff.py``'s own idea of what a scoped read
    is.
    """

    # -- generic race machinery ----------------------------------------
    @contextmanager
    def _writer_parked_in(self, obj, name):
        """Park EVERY call to ``obj.name`` at its own top. Unlike
        ``_read_parked_in`` (which matches a specific ``season_id`` because
        several reads could interleave), each case here drives exactly ONE
        writer call, so no argument matching is needed.

        THE PLACEMENT THIS RELIES ON: every writer this file races
        (``ApiService.set_active_context``, ``SetupService.archive_season``/
        ``delete_season``/``unassign_official``) is called from INSIDE the
        gate's own ``with ... .exclusive(...):`` block -- see
        ``services/context_gate.py``'s "process-wide instances" comment and
        ``api/service.py``'s ``_guarded_attempt`` -- so parking here means
        the writer has ALREADY registered (and, per ``ContextSwitchGate``'s
        own admission rule, is now HOLDING) its exclusive ticket by the time
        ``park.arrived`` fires below.
        """
        park = _Park()
        exited = {}

        def factory(original):
            def wrapper(*a, **kw):
                park.hold()
                try:
                    return original(*a, **kw)
                finally:
                    exited["at"] = time.monotonic()
            return wrapper

        self._wrap(obj, name, factory)
        try:
            yield park, exited
        finally:
            park.let_go()

    def _epoch_from_api(self, client):
        status, raw, body = self._req(client, "GET", "/api/context")
        self.assertEqual(status, 200, raw)
        self.assertIn("context_epoch", body, raw)
        return body["context_epoch"]

    def _spy(self, method_name):
        calls = []

        def factory(original):
            def wrapper(*a, **kw):
                calls.append((a, kw))
                return original(*a, **kw)
            return wrapper

        self._wrap(self.api, method_name, factory)
        return calls

    def _prove_writer_first_zero_call_discard(
            self, *, park_obj, park_name, run_writer, route_path,
            spy_method, client, pre_epoch, label):
        """THE SHARED PROOF. ``run_writer`` is a zero-arg callable performing
        the ACTUAL guarded write (the exact production call shape -- e.g.
        ``setup_guarded_mutation`` with the same ``targets`` shape
        ``web/server.py`` builds, matching this codebase's own established
        "not a shortcut" convention, see ``EpochFenceWriterOrderingTest``'s
        own comment). ``park_obj``/``park_name`` name the method to park
        INSIDE the writer's own gate hold (see ``_writer_parked_in``).

        Sequencing:

          1. writer starts, registers + holds its EXCLUSIVE ticket, parks;
          2. ``park.arrived`` confirms (1);
          3. the scoped read starts, echoing ``pre_epoch`` -- it must try to
             take its SHARED hold on the SAME key and BLOCK, since the
             writer's ticket already has a lower ``seq``;
          4. a BOUNDED negative check confirms the read has NOT returned
             yet -- proving (3) is a genuine block, not a coincidence of
             thread scheduling;
          5. the writer is released, completes, its exclusive hold drops;
          6. the read, now unblocked, derives a FRESH epoch (reflecting the
             writer's commit) which MUST mismatch ``pre_epoch`` -- discarding
             BEFORE the scoped-read service method is ever reached.

        Returns the writer's own result, for callers that need to assert on
        it too (e.g. that the guarded mutation was not itself refused).
        """
        calls = self._spy(spy_method)
        writer_result = {}
        read_result = {}
        with self._writer_parked_in(park_obj, park_name) as (park, _exited):
            def do_write():
                writer_result["value"] = run_writer()
            wt = threading.Thread(target=do_write, daemon=True)
            wt.start()
            self.assertTrue(
                park.arrived.wait(PATIENCE),
                f"{label}: the writer never reached its parked point -- it "
                f"did not take the gate before this park seam, or the park "
                f"seam did not fire")

            def do_read():
                read_result["response"] = self._req(
                    client, "GET", route_path,
                    headers={CONTEXT_EPOCH_HEADER: pre_epoch})
            rt = threading.Thread(target=do_read, daemon=True)
            rt.start()
            time.sleep(_BLOCKED_CHECK)
            self.assertNotIn(
                "response", read_result,
                f"{label}: the scoped read was NOT blocked by the writer's "
                f"still-open exclusive hold -- the gate did not order them, "
                f"so this case proves nothing about a genuine pre-produce() "
                f"gate")
            park.let_go()
            wt.join(PATIENCE)
            rt.join(PATIENCE)

        self.assertIn("response", read_result,
                      f"{label}: the scoped read never returned")
        status, raw, _body = read_result["response"]
        self.assertEqual(
            status, 204,
            f"{label}: a response computed from a torn window must never "
            f"reach the client: {raw}")
        self.assertEqual(raw, "", f"{label}: a 204 must carry no body: {raw!r}")
        self.assertEqual(
            calls, [],
            f"{label}: the scoped-read service method must NEVER have been "
            f"called -- a genuine pre-produce() gate discards BEFORE "
            f"produce(), not after: {calls}")
        return writer_result.get("value")

    # -- route table (kept independent of `_all_scoped_route_cases`; see the
    # module docstring's SCOPE section for why the delete/archive-racing
    # scenarios use a DELIBERATELY minimal season rather than that table's
    # richly-populated one) ----------------------------------------------
    def _routes_for(self, season_id):
        """The five ``RouteSpec.context_read_fence`` entries against
        ``season_id``.
        Deliberately-nonexistent scenario/division/league ids for the three
        routes that need one: this file only needs the scoped-read SERVICE
        METHOD to be genuinely CALLABLE (and therefore genuinely observable
        by the spy) in the unraced case, never a REALISTIC payload -- a
        `NotFoundError`/empty-standings answer for a made-up id is still a
        real call to the service method, which is the only fact any case
        here asserts on."""
        filler = uuid.uuid4().hex[:8]
        return [
            {"label": "venue-candidates",
             "path": f"/api/v2/setup/seasons/{season_id}/venue-candidates",
             "service": "get_venue_grant_candidates"},
            {"label": "venue-access",
             "path": f"/api/v2/setup/seasons/{season_id}/venue-access",
             "service": "list_season_venue_access"},
            {"label": "scenario",
             "path": f"/api/scheduler/scenarios/scn_nonexistent_{filler}",
             "service": "get_schedule_scenario"},
            {"label": "standings-division",
             "path": f"/api/standings/div_nonexistent_{filler}",
             "service": "get_standings"},
            {"label": "standings-league-season",
             "path": (f"/api/standings/league-season/"
                      f"lg_nonexistent_{filler}/{season_id}"),
             "service": "get_league_season_standings"},
        ]

    # -- context-switch (row 1) -----------------------------------------
    def test_context_switch_discards_with_zero_calls(self):
        fx = self._program_with_two_seasons("CtxSwZero" + uuid.uuid4().hex[:6])
        username, _uid = self._operator("ctxswzero")
        client = self._login(username)
        self._select(client, fx["program_id"], fx["s1"])
        pre_epoch = self._epoch_from_api(client)
        for route in self._routes_for(fx["s1"]):
            with self.subTest(route=route["label"]):
                self._prove_writer_first_zero_call_discard(
                    park_obj=self.api, park_name="set_active_context",
                    run_writer=lambda fx=fx, client=client: self._req(
                        client, "POST", "/api/context",
                        {"program_id": fx["program_id"],
                         "season_id": fx["s2"]}),
                    route_path=route["path"], spy_method=route["service"],
                    client=client, pre_epoch=pre_epoch,
                    label=f"context-switch/{route['label']}")
                # Each subTest consumes the ONE pre-race epoch/selection
                # above; re-select S1 and re-capture so the NEXT route's
                # race starts from the same, freshly-valid precondition
                # rather than compounding on the prior route's post-switch
                # state.
                self._select(client, fx["program_id"], fx["s1"])
                pre_epoch = self._epoch_from_api(client)

    # -- selected-Season-delete (row 6) ----------------------------------
    def test_selected_season_delete_discards_with_zero_calls(self):
        """THE REVIEW'S OWN NAMED REPRO, at the HTTP layer. A BARE Program +
        Season (no League/Division/Team) -- `delete_season` REFUSES
        (`has_dependencies`) when a Season has any, so this is not merely a
        simplification, it is the only fixture shape a delete-season race
        can use at all. Matches `EpochFenceMemorySameProcessTornReadTest`'s
        own fixture exactly (a bare `_program_season`). A FRESH bare
        Program/Season per route (``_selected_season_delete_matrix``): the
        first route's case genuinely deletes its Season, so the next route's
        case needs its own, not-yet-deleted one."""
        username, user_id = self._operator("delzero")
        self._selected_season_delete_matrix(username, user_id)

    def _selected_season_delete_matrix(self, username, user_id):
        client = self._login(username)
        for route_label in ("venue-candidates", "venue-access", "scenario",
                            "standings-division", "standings-league-season"):
            with self.subTest(route=route_label):
                program = self.api.create_program(
                    f"DelZero{route_label[:3]}{uuid.uuid4().hex[:6]}",
                    "US", "UTC")
                season = self.api.create_season(program["id"], "DelSeason")
                self._select(client, program["id"], season["id"])
                pre_epoch = self._epoch_from_api(client)
                routes = {r["label"]: r for r in
                          self._routes_for(season["id"])}
                route = routes[route_label]
                result = self._prove_writer_first_zero_call_discard(
                    park_obj=self.api.setup, park_name="delete_season",
                    run_writer=lambda season=season, user_id=user_id: (
                        self.api.setup_guarded_mutation(
                            [("season", season["id"], "scope")],
                            lambda: self.api.setup.delete_season(
                                season["id"], actor_id=user_id),
                            user_id, *ADMIN)),
                    route_path=route["path"], spy_method=route["service"],
                    client=client, pre_epoch=pre_epoch,
                    label=f"selected-Season-delete/{route_label}")
                payload, refused = result
                self.assertIsNone(refused, (payload, refused))
                self.assertFalse(
                    isinstance(payload, dict) and "error" in payload,
                    f"the delete itself must succeed, or nothing is "
                    f"proven: {payload}")

    # -- archive (row 4; reopen/row 5 shares the identical gate path, see
    #    the module docstring) --------------------------------------------
    def test_archive_discards_with_zero_calls(self):
        username, user_id = self._operator("archzero")
        client = self._login(username)
        for route_label in ("venue-candidates", "venue-access", "scenario",
                            "standings-division", "standings-league-season"):
            with self.subTest(route=route_label):
                fx = self._program_with_two_seasons(
                    f"ArchZero{route_label[:3]}" + uuid.uuid4().hex[:6])
                # Populate S1 so the 3 richer routes have a real target --
                # unlike delete, archive does not touch children at all, so
                # this is safe (`archive_season` only flips `status`).
                division_id = self._division_with_teams(
                    fx, fx["s1"], tag="AZ")
                league_id = self._league_with_teams(
                    fx, fx["s1"], tag="AZL")["league_id"]
                scenario_id = self._scenario_in(fx, fx["s1"], name="AZ run")
                self._select(client, fx["program_id"], fx["s1"])
                pre_epoch = self._epoch_from_api(client)
                route_by_label = {
                    "venue-candidates": {
                        "path": f"/api/v2/setup/seasons/{fx['s1']}/"
                                f"venue-candidates",
                        "service": "get_venue_grant_candidates"},
                    "venue-access": {
                        "path": f"/api/v2/setup/seasons/{fx['s1']}/"
                                f"venue-access",
                        "service": "list_season_venue_access"},
                    "scenario": {
                        "path": f"/api/scheduler/scenarios/{scenario_id}",
                        "service": "get_schedule_scenario"},
                    "standings-division": {
                        "path": f"/api/standings/{division_id}",
                        "service": "get_standings"},
                    "standings-league-season": {
                        "path": f"/api/standings/league-season/"
                                f"{league_id}/{fx['s1']}",
                        "service": "get_league_season_standings"},
                }
                route = route_by_label[route_label]
                result = self._prove_writer_first_zero_call_discard(
                    park_obj=self.api.setup, park_name="archive_season",
                    run_writer=lambda fx=fx, user_id=user_id: (
                        self.api.setup_guarded_mutation(
                            [("season", fx["s1"], "scope")],
                            lambda: self.api.setup.archive_season(
                                fx["s1"], reason="zero-call HTTP proof",
                                actor_id=user_id),
                            user_id, *ADMIN)),
                    route_path=route["path"], spy_method=route["service"],
                    client=client, pre_epoch=pre_epoch,
                    label=f"archive/{route_label}")
                payload, refused = result
                self.assertIsNone(refused, (payload, refused))
                self.assertFalse(
                    isinstance(payload, dict) and "error" in payload,
                    f"the archive itself must succeed, or nothing is "
                    f"proven: {payload}")

    # -- authorization-withdrawal (row 14, official unassign) -------------
    def test_authorization_withdrawal_discards_with_zero_calls(self):
        """``unassign_official`` -- literally, per that method's own
        docstring, "an authorization WITHDRAWAL for the affected Official's
        own scoped reads". The AFFECTED and READING identity must be the
        Official themselves: `context_scope.authorized_program_ids` for
        `Role.OFFICIAL` is derived entirely from
        `store.assignments_for_official` (`services/context_scope.py`'s
        `_official_program_seasons`), so an Official with exactly ONE
        assignment, unassigned, loses their ONLY authorized Program/Season —
        the effective resolved tuple (and therefore the epoch) genuinely
        moves, to `(None, None, None)`.

        RUN AGAINST BOTH ROUTES an Official can actually reach -- not just
        one. Of the five `RouteSpec.context_read_fence` entries, THREE
        (venue-candidates, venue-access, scenario) are gated `_operator_only`
        (MANAGE_SETUP)
        even as GETs (confirmed directly against `web/server.py`'s own
        dispatch -- an Official reading any of them gets a bare 403 before
        ever reaching the scoped-read machinery this file exercises, at
        which point a race would prove nothing). Only the two standings
        routes carry no such operator-only gate (only `user_id is None` is
        refused), so only those two are reachable by the affected Official
        and are what this case can, and does, cover.
        """
        admin_username, admin_id = self._operator("withdrawadmin")
        admin_client = self._login(admin_username)
        program = self.api.create_program(
            f"Withdraw{uuid.uuid4().hex[:6]}", "US", "UTC")
        season = self.api.create_season(program["id"], "WithdrawSeason")
        league = self.api.create_league(season["id"], "WithdrawLeague")
        division = self.api.create_division(
            season["id"], "WithdrawDiv", league_id=league["id"])
        club = self.api.create_club("WithdrawClub")
        team = self.api.create_team(
            club_id=club["id"], name="WithdrawTeam",
            league_id=league["id"], division_id=division["id"])
        self.api.setup.register_team_for_season(
            season["id"], team["id"], division["id"])

        routes = {
            "standings-division": {
                "path": f"/api/standings/{division['id']}",
                "service": "get_standings"},
            "standings-league-season": {
                "path": f"/api/standings/league-season/"
                        f"{league['id']}/{season['id']}",
                "service": "get_league_season_standings"},
        }
        for route_label, route in routes.items():
            with self.subTest(route=route_label):
                # A FRESH Official/game/assignment/account per route: the
                # writer under test (`unassign_official`) consumes the ONE
                # assignment it races, so the next route's race needs its
                # own, not-yet-withdrawn authorization -- Program/Season/
                # League/Division/Team are shared (never mutated by this
                # writer) so only the per-Official spine is rebuilt.
                official = self.api.create_official(
                    f"Withdraw Official {route_label}")
                with self.api.store.transaction():
                    gid = self.api.store.next_id("game")
                    self.api.store.add_game(Game(
                        id=gid, home_team_id=team["id"],
                        away_team_id=team["id"], start_time=None,
                        season_id=season["id"]))
                assignment = self.api.assign_official(
                    gid, official["id"], OfficialRole.REFEREE.value,
                    actor_id=admin_id)
                off_username = f"withdrawoff_{uuid.uuid4().hex[:8]}"
                self.api.accounts.create_account(
                    off_username, "demo", Role.OFFICIAL,
                    scope={"official_id": official["id"]}, actor_id=admin_id)
                off_client = self._login(off_username)
                # No explicit selection -- an Official with exactly one
                # authorized (Program, Season) resolves it via `_fallback()`
                # with nothing saved, exactly as a first-run operator would.
                pre_epoch = self._epoch_from_api(off_client)
                selected = self._req(off_client, "GET", "/api/context")[2]
                self.assertEqual(
                    selected.get("season_id"), season["id"],
                    f"fixture error: the Official did not resolve the "
                    f"assignment's own Season: {selected}")

                result = self._prove_writer_first_zero_call_discard(
                    park_obj=self.api.setup, park_name="unassign_official",
                    run_writer=lambda assignment=assignment: (
                        self.api.unassign_official(
                            assignment["id"], admin_id)),
                    route_path=route["path"], spy_method=route["service"],
                    client=off_client, pre_epoch=pre_epoch,
                    label=f"authorization-withdrawal/{route_label}")
                self.assertFalse(
                    isinstance(result, dict) and "error" in result,
                    f"the unassign itself must succeed, or nothing is "
                    f"proven: {result}")


class MemoryEpochFenceZeroCallHttpTest(
        EpochFenceZeroCallHttpBase, unittest.TestCase):
    STORE_URL = None


class SqliteEpochFenceZeroCallHttpTest(
        EpochFenceZeroCallHttpBase, unittest.TestCase):
    """File-backed (real path, not ``:memory:``): exercises BOTH round-N+1
    layers together -- the in-process gates (identical to Memory's) AND
    ``_read_under_context_gate_sqlite``'s independent-``SqlStore``, engine-
    level file lock. See that method's own docstring for why the two are not
    separated in this HTTP-level proof (the store-level falsifiability class
    in ``tests/test_epoch_fence.py`` is where the file-lock mechanism's OWN
    marginal contribution, isolated from the gates, would be demonstrated)."""

    @classmethod
    def setUpClass(cls):
        import os
        import tempfile
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        os.remove(path)
        cls._sqlite_path = path
        cls.STORE_URL = path
        super().setUpClass()

    @classmethod
    def tearDownClass(cls):
        import os
        super().tearDownClass()
        try:
            os.remove(cls._sqlite_path)
        except OSError:
            pass


if __name__ == "__main__":
    unittest.main()
