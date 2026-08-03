"""Bounded deterministic explanations for unplaced schedule pairings (#379).

Each regression is intentionally falsifiable on one guarantee: simultaneous
causes, canonical ordering, per-pair/global caps, scope privacy, honest
alternatives, and observer-only placement equivalence.
"""

import copy
import itertools
import json
import os
import threading
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from http.cookiejar import CookieJar
from http.server import ThreadingHTTPServer

from helpers import BACKEND  # noqa: F401  (ensures package path is set up)

from hockey_scheduler.api import ApiService
from hockey_scheduler.domain import (
    Division,
    Game,
    IceSlot,
    League,
    LeagueSeason,
    Organization,
    Program,
    Rink,
    Season,
    SeasonTeamRegistration,
    SeasonVenueAccess,
    Team,
    Venue,
)
from hockey_scheduler.services import scheduler as sched
from hockey_scheduler.services.schedule_explanations import (
    MAX_ALTERNATIVES_PER_PAIRING,
    MAX_CANDIDATE_WINDOWS_PER_PAIRING,
    MAX_CANDIDATE_WINDOWS_PER_PREVIEW,
    MAX_REJECTIONS_PER_CANDIDATE,
    build_unplaced_explanation,
)
from hockey_scheduler.store import InMemoryStore, SqlStore
from hockey_scheduler.web import server as srv


UTC = timezone.utc
BASE = datetime(2026, 10, 5, 18, tzinfo=UTC)


def _seed(store, *, team_count=2, active_access=True):
    """Two authorized venues plus a completely separate private scope."""
    store.add_organization(Organization(id="org", name="Owner"))
    store.add_program(Program(
        id="pg", name="Program", operator_organization_id="org",
        timezone="UTC"))
    store.add_season(Season(id="se", program_id="pg", name="Season"))
    store.add_league(League(id="lg", program_id="pg", name="League"))
    store.add_league_season(LeagueSeason(
        id="ls", league_id="lg", season_id="se"))
    store.add_division(Division(id="d", league_season_id="ls", name="D"))
    for i in range(team_count):
        store.add_team(Team(
            id=f"t{i}", name=f"Team {i}", program_id="pg", league_id="lg"))
        store.add_season_team_registration(SeasonTeamRegistration(
            id=f"reg{i}", league_season_id="ls", team_id=f"t{i}",
            division_id="d", active=True))

    for venue_id, rink_id in (("v1", "r1"), ("v2", "r2"), ("v2", "r3")):
        if store.get_venue(venue_id) is None:
            store.add_venue(Venue(
                id=venue_id, name=f"Arena {venue_id}", organization_id="org",
                league_id="pg", timezone="UTC"))
        store.add_rink(Rink(
            id=rink_id, venue_id=venue_id, name=f"Rink {rink_id}"))
    if active_access:
        for index, venue_id in enumerate(("v1", "v2"), start=1):
            store.add_season_venue_access(SeasonVenueAccess(
                id=f"sva{index}", season_id="se", venue_id=venue_id,
                active=True))

    # Another Program/League/Season's identifiers and operator text must never
    # enter target-scope candidate evidence, even when its ice is earlier.
    store.add_organization(Organization(id="org_private", name="Private Owner"))
    store.add_program(Program(
        id="pg_private", name="Secret Program",
        operator_organization_id="org_private", timezone="UTC"))
    store.add_season(Season(
        id="se_private", program_id="pg_private", name="Secret Season"))
    store.add_league(League(
        id="lg_private", program_id="pg_private", name="Secret League"))
    store.add_league_season(LeagueSeason(
        id="ls_private", league_id="lg_private", season_id="se_private"))
    store.add_division(Division(
        id="d_private", league_season_id="ls_private", name="Secret Division"))
    store.add_team(Team(
        id="t_private", name="Private Team", program_id="pg_private",
        league_id="lg_private"))
    store.add_season_team_registration(SeasonTeamRegistration(
        id="reg_private", league_season_id="ls_private", team_id="t_private",
        division_id="d_private", active=True))
    store.add_venue(Venue(
        id="v_private", name="Private Arena", organization_id="org_private",
        league_id="pg_private", timezone="UTC"))
    store.add_rink(Rink(
        id="r_private", venue_id="v_private", name="Private Rink"))
    store.add_season_venue_access(SeasonVenueAccess(
        id="sva_private", season_id="se_private", venue_id="v_private",
        active=True))
    _slot(store, "slot_private", "r_private", BASE - timedelta(days=1))
    return ApiService(store)


def _slot(store, slot_id, rink_id, start, minutes=60):
    store.add_ice_slot(IceSlot(
        id=slot_id, rink_id=rink_id, start_time=start,
        end_time=start + timedelta(minutes=minutes)))
    return slot_id


def _explanation(preview, index=0):
    return preview["unscheduled"][index]["explanation"]


def _neighbouring_season(store, venue_id="v1"):
    """A SECOND Season of the same Program holding access to the same Venue.

    Two Seasons with active access to one Venue is precisely what
    ``SeasonVenueAccess`` exists to express -- arenas are shared -- and it is
    also a corner the caller's own active tuple is REFUSED (#386/#388;
    ``test_the_two_near_miss_corners_are_refused`` in
    ``test_draft_context_scope``). Its Games are therefore out-of-context rows
    that happen to sit on in-context ice, which is the only way a foreign
    identifier can reach this observer at all: the candidate scanner never
    returns another Season's slots, but ``_active_game_slot_pairs`` is
    deliberately unfiltered (#373) so the preview and the commit gate measure
    the same edges.
    """
    store.add_season(Season(
        id="se_nb", program_id="pg", name="Neighbour Season"))
    store.add_league(League(
        id="lg_nb", program_id="pg", name="Neighbour League"))
    store.add_league_season(LeagueSeason(
        id="ls_nb", league_id="lg_nb", season_id="se_nb"))
    store.add_division(Division(
        id="d_nb", league_season_id="ls_nb", name="Neighbour Division"))
    store.add_season_venue_access(SeasonVenueAccess(
        id="sva_nb", season_id="se_nb", venue_id=venue_id, active=True))
    for index in range(2):
        store.add_team(Team(
            id=f"t_nb{index}", name=f"Neighbour {index}", program_id="pg",
            league_id="lg_nb"))
        store.add_season_team_registration(SeasonTeamRegistration(
            id=f"reg_nb{index}", league_season_id="ls_nb",
            team_id=f"t_nb{index}", division_id="d_nb", active=True))


# Every neighbouring-Season identifier that must not reach candidate evidence.
# The Game id is NOT listed here -- it is assigned by the store counter, so a
# literal would be a guess that never appears and the assertion would pass for
# the wrong reason. It is added from the real created row by the test below.
#
# ``slot_nb`` is deliberately absent too, for the opposite reason: it sits on a
# Rink whose Venue the caller's own Season holds active access to, so it is the
# caller's OWN candidate inventory and naming it discloses nothing across the
# boundary.
NEIGHBOUR_SECRETS = (
    "se_nb", "lg_nb", "ls_nb", "d_nb", "t_nb0", "t_nb1",
    "Neighbour Season", "Neighbour League", "Neighbour Division",
    "Neighbour 0", "Neighbour 1",
)


class _ExplanationContract:
    def make_store(self):
        raise NotImplementedError

    def setUp(self):
        self.store = self.make_store()

    def tearDown(self):
        conn = getattr(self.store, "conn", None)
        if conn is not None:
            conn.close()

    def test_no_available_ice_has_input_correction_not_invented_slot(self):
        api = _seed(self.store)
        preview = api.draft_season_schedule("d")
        explanation = _explanation(preview)
        self.assertEqual(
            explanation["blocking_constraint_codes"], ["no_ice_available"])
        self.assertEqual(explanation["candidate_windows"], [])
        self.assertEqual(
            explanation["alternatives"][0]["action_code"],
            "increase_available_game_ice")
        self.assertNotIn("ice_slot_id", explanation["alternatives"][0])

    def test_zero_access_is_fail_closed_without_leaking_inaccessible_ice(self):
        api = _seed(self.store, active_access=False)
        preview = api.draft_season_schedule("d")
        explanation = _explanation(preview)
        self.assertEqual(explanation["candidate_windows"], [])
        self.assertIn(
            "venue_access_missing", explanation["blocking_constraint_codes"])
        self.assertEqual(
            explanation["alternatives"][0], {
                "action_code": "review_season_venue_access",
                "reason_code": "venue_access_missing",
                "season_id": "se",
            })
        serialized = json.dumps(explanation, sort_keys=True)
        for private_value in (
                "slot_private", "r_private", "v_private", "Private Arena",
                "Private Rink", "Secret Program", "Private Team"):
            self.assertNotIn(private_value, serialized)

    def test_explicit_out_of_scope_slot_fails_before_candidate_evidence(self):
        """Venue eligibility stays the existing server-side fail-closed gate."""
        api = _seed(self.store)
        result = api.draft_season_schedule("d", slot_ids=["slot_private"])
        self.assertEqual(
            result["error"]["details"]["reason"], "venue_access_missing")
        serialized = json.dumps(result, sort_keys=True)
        self.assertNotIn("candidate_windows", serialized)
        for private_text in (
                "Private Arena", "Private Rink", "Secret Program", "Private Team"):
            self.assertNotIn(private_text, serialized)

    def test_simultaneous_blackout_causes_are_all_retained_and_identifier_only(self):
        """Dropping any one cause makes this independent mutation proof fail."""
        api = _seed(self.store)
        _slot(self.store, "candidate", "r1", BASE)
        day = BASE.date().isoformat()
        preview = api.draft_season_schedule("d", constraints={
            "season_blackout_dates": [day],
            "holiday_dates": [day],
            "team_blackouts": {"t0": [day]},
            "rink_blackouts": {"r1": [day]},
        })
        row = preview["unscheduled"][0]
        # Backward compatibility: legacy first-cause semantics do not change.
        self.assertEqual(row["reason_codes"], ["season_blackout"])
        candidate = row["explanation"]["candidate_windows"][0]
        self.assertEqual(
            [r["code"] for r in candidate["rejections"]],
            ["season_blackout", "holiday", "team_blackout", "rink_blackout"])
        self.assertEqual(candidate["ice_slot_id"], "candidate")
        self.assertEqual(candidate["rink_id"], "r1")
        self.assertEqual(candidate["venue_id"], "v1")
        serialized = json.dumps(candidate, sort_keys=True)
        for forbidden in ("Team 0", "Rink r1", "Arena v1", "message", "name"):
            self.assertNotIn(forbidden, serialized)
        # Every suggested action corrects a real input; none claims this hard-
        # rejected candidate is playable.
        for alternative in row["explanation"]["alternatives"]:
            self.assertNotIn("ice_slot_id", alternative)
            self.assertNotEqual(alternative["action_code"], "use_ice_slot")
        self.assertEqual(
            len(row["explanation"]["alternatives"]),
            MAX_ALTERNATIVES_PER_PAIRING)
        self.assertEqual(
            row["explanation"]["bounds"]["alternative_omitted_count"], 1)

    def test_minimum_rest_and_max_per_day_are_both_explained(self):
        api = _seed(self.store, team_count=3)
        for index, hour in enumerate((0, 2, 4)):
            _slot(self.store, f"same_day_{index}", "r1",
                  BASE + timedelta(hours=hour))
        preview = api.draft_season_schedule("d", constraints={
            "max_games_per_team_per_day": 1,
            "min_rest_hours": 48,
        })
        rejections = [
            rejection
            for row in preview["unscheduled"]
            for candidate in row["explanation"]["candidate_windows"]
            for rejection in candidate["rejections"]
        ]
        codes = {rejection["code"] for rejection in rejections}
        self.assertIn("max_per_day", codes)
        self.assertIn("min_rest", codes)
        rest = next(r for r in rejections if r["code"] == "min_rest")
        self.assertEqual(rest["details"]["min_rest_hours"], 48.0)
        self.assertTrue(rest["details"]["conflicts"])

    def test_team_overlap_names_only_safe_ids_and_never_changes_selection(self):
        api = _seed(self.store, team_count=3)
        _slot(self.store, "simultaneous_r1", "r1", BASE)
        _slot(self.store, "simultaneous_r2", "r2", BASE)
        preview = api.draft_season_schedule("d")
        self.assertEqual(len(preview["draft_games"]), 1)
        overlap_rows = [
            row for row in preview["unscheduled"]
            if "team_overlap" in row["explanation"]["blocking_constraint_codes"]
        ]
        self.assertTrue(overlap_rows, preview)
        explanation = overlap_rows[0]["explanation"]
        overlap = next(
            rejection
            for candidate in explanation["candidate_windows"]
            for rejection in candidate["rejections"]
            if rejection["code"] == "team_overlap")
        self.assertTrue(overlap["details"]["team_ids"])
        self.assertNotIn("team_name", json.dumps(overlap))
        self.assertIn(
            "provide_non_overlapping_game_ice",
            {a["action_code"] for a in explanation["alternatives"]})

    # -- the #386/#388 boundary, applied to THIS slice's own fields --------
    def _shared_rink_neighbour(self, api):
        """Neighbour Season booked on the shared rink; one candidate of ours.

        Returns ``(candidate_slot_id, neighbour_game_id)``. The explicit
        ``slot_ids`` at the draft call keeps the pairing unplaced and the
        evidence a single window: ``slot_nb`` is on the same Rink and is
        therefore in the caller's own scanned inventory, so leaving the
        selection open would let the greedy loop simply place the pairing
        there.
        """
        _neighbouring_season(self.store)
        _slot(self.store, "slot_nb", "r1", BASE)
        booked = api.create_game(
            "se_nb", "d_nb", "t_nb0", "t_nb1", "slot_nb",
            league_id="lg_nb", actor_id="admin")
        self.assertNotIn("error", booked, booked)
        mine = _slot(self.store, "mine", "r1", BASE + timedelta(minutes=30))
        return mine, booked["id"]

    def test_a_neighbouring_seasons_game_is_never_named_in_evidence(self):
        """#386/#388 bound the draft surface to the caller's active tuple.
        The evidence this slice adds must obey the SAME boundary.

        The caller is standing in ``(pg, se, lg)``. The conflicting Game
        belongs to ``(pg, se_nb, lg_nb)`` -- a corner this very principal is
        refused from drafting, listing, or reviewing. Its Game id is not the
        caller's to learn from a Season it may not select.

        Two shapes, because they fail for DIFFERENT reasons:

        * overlap is the first cause -- the established decision path reaches
          the policy advisory too, so the legacy ``reason`` prose names the
          same Game and this is a restatement of an existing disclosure; and
        * a blackout short-circuits the legacy path at its FIRST cause, so
          ``reason`` names nothing of the neighbour at all -- and only this
          slice's all-causes observer runs the advisory. That shape is a NEW
          disclosure, not a restructured one.
        """
        api = _seed(self.store)
        mine, neighbour_game_id = self._shared_rink_neighbour(api)
        # The real assigned id, never a literal guess: this is the single
        # most important string in the haystack below.
        secrets = NEIGHBOUR_SECRETS + (neighbour_game_id,)
        blackout = (BASE + timedelta(minutes=30)).date().isoformat()
        causes = (
            ("overlap is the first cause", None),
            ("a blackout short-circuits the legacy path",
             {"season_blackout_dates": [blackout]}),
        )
        # Both request shapes: the two entry points seed the observer
        # independently, so a rule wired into only one of them is a hole.
        shapes = (
            ("division", {"division_id": "d"}),
            ("league-wide", {"season_id": "se", "league_id": "lg"}),
        )
        for (cause, constraints), (shape, target) in itertools.product(
                causes, shapes):
            with self.subTest(cause=cause, shape=shape):
                preview = api.draft_season_schedule(
                    slot_ids=[mine], constraints=constraints, **target)
                self.assertNotIn("error", preview, preview)
                explanation = _explanation(preview)
                codes = {rejection["code"]
                         for candidate in explanation["candidate_windows"]
                         for rejection in candidate["rejections"]}
                # ANTI-VACUITY: the branch that carries the id really ran.
                self.assertIn("slot_overlap_conflict", codes, explanation)
                # The privacy clause first, over the WHOLE object: a leak
                # that moved to another field is still a leak.
                serialized = json.dumps(explanation, sort_keys=True)
                for secret in secrets:
                    self.assertNotIn(
                        secret, serialized,
                        f"candidate evidence named {secret!r} from a Season "
                        "this caller's active tuple is refused")
                overlap = next(
                    rejection
                    for candidate in explanation["candidate_windows"]
                    for rejection in candidate["rejections"]
                    if rejection["code"] == "slot_overlap_conflict")
                self.assertNotIn("conflict_game_id", overlap["details"])
                # ...and it still says WHICH of the caller's own windows is
                # unusable, so withholding the foreign id costs no in-scope
                # signal.
                self.assertEqual(overlap["details"]["rink_id"], "r1")
                self.assertEqual(
                    overlap["details"]["conflict_slot_id"], "slot_nb")
                # An operator with no authority over the other Season cannot
                # reschedule its Game, so the honest correction is ice.
                self.assertEqual(
                    {a["action_code"] for a in explanation["alternatives"]}
                    & {"reschedule_conflicting_game",
                       "provide_non_overlapping_game_ice"},
                    {"provide_non_overlapping_game_ice"}, explanation)

    def test_a_teams_other_season_game_is_named_only_inside_the_tuple(self):
        """The same boundary on the OTHER code that carries a Game id.

        ``team_overlap`` reaches its conflicting Game through the team, not
        the rink, so it is a genuinely independent path into the same field:
        a club team playing on in two Seasons is ordinary, and the Game that
        double-books it belongs to whichever Season booked it.

        The corner here is the SHARPER of #388's two near misses -- same
        Program AND same League, different Season -- because a scoping rule
        that compared only the League, or only the Program, would wave it
        straight through.

        The legacy ``team_conflicts`` row on the same response is deliberately
        left exactly as ``main`` computes it -- this slice does not narrow an
        existing field -- so this pins the NEW evidence only.
        """
        api = _seed(self.store)
        # A second Season of the caller's OWN League. `Team.league_id` binds a
        # team to one League, so this is the shape in which one team really
        # can appear in two Seasons' Games.
        self.store.add_season(Season(
            id="se2", program_id="pg", name="Next Season"))
        self.store.add_league_season(LeagueSeason(
            id="ls2", league_id="lg", season_id="se2"))
        self.store.add_division(Division(
            id="d2", league_season_id="ls2", name="D2"))
        self.store.add_season_venue_access(SeasonVenueAccess(
            id="sva_next", season_id="se2", venue_id="v2", active=True))
        self.store.add_team(Team(
            id="t_other", name="Other", program_id="pg", league_id="lg"))
        for reg_id, team_id in (("reg_next0", "t0"), ("reg_next1", "t_other")):
            self.store.add_season_team_registration(SeasonTeamRegistration(
                id=reg_id, league_season_id="ls2", team_id=team_id,
                division_id="d2", active=True))
        _slot(self.store, "slot_nb", "r2", BASE)
        booked = api.create_game(
            "se2", "d2", "t0", "t_other", "slot_nb",
            league_id="lg", actor_id="admin")
        self.assertNotIn("error", booked, booked)
        # A candidate on a DIFFERENT rink, so only the team collides.
        mine = _slot(self.store, "mine", "r1", BASE)

        preview = api.draft_season_schedule("d", slot_ids=[mine])
        explanation = _explanation(preview)
        overlap = next(
            rejection
            for candidate in explanation["candidate_windows"]
            for rejection in candidate["rejections"]
            if rejection["code"] == "team_overlap")
        # ANTI-VACUITY: the collision really was detected, and against the
        # PERSISTED game rather than a same-run pick.
        self.assertEqual(overlap["details"]["team_ids"], ["t0"])
        self.assertEqual(
            [row["conflict_source"] for row in overlap["details"]["conflicts"]],
            ["existing_game"])
        for row in overlap["details"]["conflicts"]:
            self.assertNotIn("conflict_game_id", row)
        self.assertNotIn(
            booked["id"], json.dumps(explanation, sort_keys=True),
            "team_overlap evidence named a Game from a Season this caller's "
            "active tuple is refused")

    def test_a_sibling_leagues_game_in_this_season_is_withheld_too(self):
        """#388's OTHER near miss: same Program, same Season, different
        League.

        Stated separately from the different-Season corner because the two
        halves of the comparison fail independently: a rule that checked only
        the Season would pass that test and this one would still hand over a
        sibling League's Game id.
        """
        api = _seed(self.store)
        self.store.add_league(League(
            id="lg_x", program_id="pg", name="Sibling League"))
        self.store.add_league_season(LeagueSeason(
            id="ls_x", league_id="lg_x", season_id="se"))
        self.store.add_division(Division(
            id="d_x", league_season_id="ls_x", name="DX"))
        for index in range(2):
            self.store.add_team(Team(
                id=f"t_x{index}", name=f"Sibling {index}", program_id="pg",
                league_id="lg_x"))
            self.store.add_season_team_registration(SeasonTeamRegistration(
                id=f"reg_x{index}", league_season_id="ls_x",
                team_id=f"t_x{index}", division_id="d_x", active=True))
        _slot(self.store, "slot_x", "r1", BASE)
        booked = api.create_game(
            "se", "d_x", "t_x0", "t_x1", "slot_x",
            league_id="lg_x", actor_id="admin")
        self.assertNotIn("error", booked, booked)
        mine = _slot(self.store, "mine", "r1", BASE + timedelta(minutes=30))

        preview = api.draft_season_schedule("d", slot_ids=[mine])
        explanation = _explanation(preview)
        overlap = next(
            rejection
            for candidate in explanation["candidate_windows"]
            for rejection in candidate["rejections"]
            if rejection["code"] == "slot_overlap_conflict")
        # ANTI-VACUITY: the Season really does match, so only the League can
        # be doing the work here.
        self.assertEqual(self.store.get_game(booked["id"]).season_id, "se")
        self.assertNotIn("conflict_game_id", overlap["details"])
        self.assertNotIn(
            booked["id"], json.dumps(explanation, sort_keys=True),
            "candidate evidence named a sibling League's Game")

    def test_an_in_scope_conflicting_game_is_still_named(self):
        """The anti-vacuity control for the refusal above.

        If the two ids simply vanished from the payload, the negative would
        pass with the whole scoping rule reverted -- and the operator would
        lose the one correction they CAN act on. The same physical collision,
        with the conflicting Game inside the caller's own tuple, still names
        it and still offers the reschedule action.
        """
        api = _seed(self.store, team_count=4)
        self.store.add_division(Division(
            id="host_div", league_season_id="ls", name="Host"))
        for index in range(2):
            tid = f"host_t{index}"
            self.store.add_team(Team(
                id=tid, name=f"Host {index}", program_id="pg", league_id="lg"))
            self.store.add_season_team_registration(SeasonTeamRegistration(
                id=f"host_reg{index}", league_season_id="ls", team_id=tid,
                division_id="host_div", active=True))
        _slot(self.store, "host_slot", "r1", BASE)
        booked = api.create_game(
            "se", "host_div", "host_t0", "host_t1", "host_slot",
            league_id="lg", actor_id="admin")
        self.assertNotIn("error", booked, booked)
        mine = _slot(self.store, "mine", "r1", BASE + timedelta(minutes=30))

        # Both request shapes, so neither entry point can quietly stop seeding
        # the scope and take the fail-closed path for every caller.
        for shape, target in (
                ("division", {"division_id": "d"}),
                ("league-wide", {"season_id": "se", "league_id": "lg"})):
            with self.subTest(shape=shape):
                preview = api.draft_season_schedule(slot_ids=[mine], **target)
                self.assertNotIn("error", preview, preview)
                explanation = _explanation(preview)
                overlap = next(
                    rejection
                    for candidate in explanation["candidate_windows"]
                    for rejection in candidate["rejections"]
                    if rejection["code"] == "slot_overlap_conflict")
                self.assertEqual(
                    overlap["details"]["conflict_game_id"], booked["id"])
                self.assertEqual(
                    overlap["details"]["conflict_slot_id"], "host_slot")
                self.assertIn(
                    "reschedule_conflicting_game",
                    {a["action_code"] for a in explanation["alternatives"]})

    def test_turnover_curfew_and_playable_time_use_shared_policy_codes(self):
        api = _seed(self.store)
        self.store.add_division(Division(
            id="host_div", league_season_id="ls", name="Host"))
        for index in range(2):
            tid = f"host_t{index}"
            self.store.add_team(Team(
                id=tid, name=f"Host {index}", program_id="pg", league_id="lg"))
            self.store.add_season_team_registration(SeasonTeamRegistration(
                id=f"host_reg{index}", league_season_id="ls", team_id=tid,
                division_id="host_div", active=True))

        _slot(self.store, "host", "r1", BASE)
        created = api.create_game(
            "se", "host_div", "host_t0", "host_t1", "host",
            league_id="lg", actor_id="admin")
        self.assertNotIn("error", created, created)
        _slot(self.store, "turnover", "r1", BASE + timedelta(minutes=65))
        _slot(self.store, "curfew", "r2", BASE + timedelta(days=1, hours=1))
        _slot(self.store, "sliver", "r3", BASE + timedelta(days=2), minutes=40)
        self.assertNotIn("error", api.set_scheduling_policy(
            scope_type="rink", scope_id="r1", warmup_minutes=10,
            resurfacing_minutes=10, actor_id="admin"))
        self.assertNotIn("error", api.set_scheduling_policy(
            scope_type="rink", scope_id="r2", curfew_local="19:00",
            actor_id="admin"))
        self.assertNotIn("error", api.set_scheduling_policy(
            scope_type="rink", scope_id="r3", min_playable_minutes=60,
            actor_id="admin"))

        preview = api.draft_season_schedule("d")
        explanation = _explanation(preview)
        codes = {
            rejection["code"]
            for candidate in explanation["candidate_windows"]
            for rejection in candidate["rejections"]
        }
        self.assertEqual(codes, {
            "turnover_buffer_conflict", "curfew_violation",
            "insufficient_playable_time",
        })
        for candidate in explanation["candidate_windows"]:
            self.assertTrue(candidate["rejections"])
        self.assertEqual(
            {a["action_code"] for a in explanation["alternatives"]},
            {"review_rink_scheduling_policy"})

    def test_ice_taken_by_an_earlier_pairing_says_so_not_no_ice_available(self):
        """The one case where the legacy prose is actively misleading.

        Three teams, one window: the first pairing takes it and the other two
        are told ``No available ice slot for this pairing`` — which reads as
        "this Season has no ice" when the truth is "this proposal already
        spent the ice it has". ``ice_already_selected`` is the only reason
        code this slice invents and it exists for exactly this row, so it is
        pinned against the real greedy loop rather than the formatter.
        """
        api = _seed(self.store, team_count=3)
        _slot(self.store, "only", "r1", BASE)
        preview = api.draft_season_schedule("d")
        self.assertEqual(len(preview["draft_games"]), 1)
        taken = preview["draft_games"][0]["ice_slot_id"]
        self.assertEqual(len(preview["unscheduled"]), 2)
        for row in preview["unscheduled"]:
            # The legacy field keeps its established, coarser answer.
            self.assertEqual(row["reason_codes"], ["no_ice_available"])
            explanation = row["explanation"]
            self.assertEqual(
                explanation["blocking_constraint_codes"],
                ["no_ice_available", "ice_already_selected"])
            self.assertEqual(
                [(c["ice_slot_id"], [r["code"] for r in c["rejections"]])
                 for c in explanation["candidate_windows"]],
                [(taken, ["ice_already_selected"])])

    def test_cap_order_and_scope_are_stable_across_multi_venue_inventory(self):
        """Reordering, over-cap, or out-of-scope mutations fail separately."""
        api = _seed(self.store)
        self.store.add_league(League(
            id="lg_cross", program_id="pg", name="Other League"))
        self.store.add_league_season(LeagueSeason(
            id="ls_cross", league_id="lg_cross", season_id="se"))
        self.store.add_division(Division(
            id="d_cross", league_season_id="ls_cross", name="Other Division"))
        self.store.add_team(Team(
            id="t_cross", name="Cross League Team", program_id="pg",
            league_id="lg_cross"))
        self.store.add_season_team_registration(SeasonTeamRegistration(
            id="reg_cross", league_season_id="ls_cross", team_id="t_cross",
            division_id="d_cross", active=True))
        expected = []
        # Insert in reverse chronological/id order; the response must use the
        # scheduler's canonical (start_time, id) order, not store order.
        for index in reversed(range(MAX_CANDIDATE_WINDOWS_PER_PAIRING + 1)):
            slot_id = f"candidate_{index:02d}"
            rink_id = "r1" if index % 2 == 0 else "r2"
            _slot(self.store, slot_id, rink_id, BASE + timedelta(days=index))
            expected.append((BASE + timedelta(days=index), slot_id))
        days = [(BASE + timedelta(days=index)).date().isoformat()
                for index in range(MAX_CANDIDATE_WINDOWS_PER_PAIRING + 1)]
        preview = api.draft_season_schedule(
            "d", constraints={"season_blackout_dates": days})
        explanation = _explanation(preview)
        ids = [c["ice_slot_id"] for c in explanation["candidate_windows"]]
        canonical = [slot_id for _start, slot_id in sorted(expected)]
        self.assertEqual(ids, canonical[:MAX_CANDIDATE_WINDOWS_PER_PAIRING])
        self.assertEqual(len(ids), MAX_CANDIDATE_WINDOWS_PER_PAIRING)
        self.assertEqual(
            explanation["bounds"]["candidate_window_total"],
            MAX_CANDIDATE_WINDOWS_PER_PAIRING + 1)
        self.assertEqual(
            explanation["bounds"]["candidate_window_omitted_count"], 1)
        self.assertTrue(explanation["bounds"]["candidate_windows_truncated"])
        serialized = json.dumps(explanation, sort_keys=True)
        for private_id in (
                "slot_private", "r_private", "v_private", "t_private",
                "t_cross", "Cross League Team"):
            self.assertNotIn(private_id, serialized)


class MemoryExplanationTest(_ExplanationContract, unittest.TestCase):
    def make_store(self):
        return InMemoryStore()


class SqliteExplanationTest(_ExplanationContract, unittest.TestCase):
    def make_store(self):
        return SqlStore(":memory:")


@unittest.skipUnless(os.environ.get("TEST_DATABASE_URL"),
                     "PostgreSQL required (set TEST_DATABASE_URL)")
class PostgresExplanationTest(_ExplanationContract, unittest.TestCase):
    def make_store(self):
        store = SqlStore(os.environ["TEST_DATABASE_URL"])
        store.clear_all_data()
        return store


class InScopeGameIdTest(unittest.TestCase):
    """``_in_scope_game_ids``' own rules, driven directly.

    The league-scoped facade rejects a dangling Division before the base
    scheduler ever runs, so the FAIL-CLOSED branch — an unresolved Season or
    League scope — is unreachable through the API and would survive deletion
    with the rest of this file green. It is the branch that decides what a
    legacy or half-resolved target discloses, so it is pinned here.
    """

    def _pairs(self, *specs):
        return [
            (Game(id=game_id, home_team_id="t0", away_team_id="t1",
                  ice_slot_id="s", season_id=season_id, league_id=league_id,
                  start_time=BASE, end_time=BASE + timedelta(hours=1)),
             object())
            for game_id, season_id, league_id in specs]

    def test_only_the_exact_season_and_league_pair_is_in_scope(self):
        pairs = self._pairs(
            ("g_match", "se", "lg"),
            ("g_other_season", "se2", "lg"),
            ("g_other_league", "se", "lg2"),
            ("g_other_both", "se2", "lg2"),
        )
        self.assertEqual(
            sched._in_scope_game_ids(
                pairs, {"season_id": "se", "league_id": "lg"}),
            {"g_match"})

    def test_an_unresolved_scope_withholds_every_id(self):
        """Fail-closed: a missing half of the tuple is not a wildcard.

        Each unresolved scope below is paired with a Game that a plain
        equality comparison WOULD match once the guard is gone -- a
        ``None``-Season row for the Season-less scope, and so on. Without
        them the guard could be deleted and every assertion here would still
        pass on an empty result, which is the shape of vacuity this file has
        to avoid.
        """
        pairs = self._pairs(
            ("g_match", "se", "lg"),
            ("g_no_season", None, "lg"),
            ("g_no_league", "se", None),
            ("g_legacy", None, None),
        )
        for label, scope in (
                ("no season", {"season_id": None, "league_id": "lg"}),
                ("no league", {"season_id": "se", "league_id": None}),
                ("neither", {}),
                ("no scope at all", None)):
            with self.subTest(scope=label):
                self.assertEqual(
                    sched._in_scope_game_ids(pairs, scope), frozenset(),
                    "an unresolved scope defaulted OPEN")
        # ...and the control: the same pairs really do yield an id when the
        # scope resolves, so the assertions above are not empty-in/empty-out.
        self.assertEqual(
            sched._in_scope_game_ids(
                pairs, {"season_id": "se", "league_id": "lg"}),
            {"g_match"})


class ExplanationFormatterContractTest(unittest.TestCase):
    """The formatter's own guarantees, driven directly.

    ``_assign_ice`` only ever feeds this formatter candidates it has ALREADY
    rejected, and only ever feeds it the detail shapes the two in-repo
    producers happen to emit today. Three contract clauses are therefore
    unreachable through the scheduler — the rejection-free-candidate guard,
    the detail allowlist, and the canonical code rank — and every one of them
    could be deleted with the rest of this file still green. They are the
    clauses that decide whether a future producer can leak, so they are
    exercised here against the public entry point instead.
    """

    PAIRING = {"home_team_id": "t0", "away_team_id": "t1"}
    SCOPE = {"season_id": "se", "league_id": "lg"}

    def _build(self, rejections, *, legacy=(), candidate_total=1,
               slot_id="window"):
        return build_unplaced_explanation(
            pairing=dict(self.PAIRING),
            scope=dict(self.SCOPE),
            legacy_reason_codes=list(legacy),
            raw_candidates=[{
                "ice_slot_id": slot_id, "rink_id": "r1", "venue_id": "v1",
                "start_time": BASE.isoformat(),
                "end_time": (BASE + timedelta(hours=1)).isoformat(),
                "rejections": list(rejections),
            }],
            candidate_total=candidate_total,
        )

    def test_a_candidate_with_no_rejection_is_never_reported_as_evidence(self):
        """An unplaced pairing's evidence may not include a playable window."""
        explanation = build_unplaced_explanation(
            pairing=dict(self.PAIRING),
            scope=dict(self.SCOPE),
            legacy_reason_codes=["no_ice_available"],
            raw_candidates=[
                {
                    "ice_slot_id": "wide_open", "rink_id": "r1",
                    "venue_id": "v1", "start_time": BASE.isoformat(),
                    "end_time": (BASE + timedelta(hours=1)).isoformat(),
                    "rejections": [],
                },
                {
                    "ice_slot_id": "blocked", "rink_id": "r1",
                    "venue_id": "v1",
                    "start_time": (BASE + timedelta(days=1)).isoformat(),
                    "end_time": (BASE + timedelta(days=1, hours=1))
                    .isoformat(),
                    "rejections": [{
                        "code": "holiday", "details": {"date": "2026-10-06"}}],
                },
            ],
            candidate_total=2,
        )
        self.assertEqual(
            [c["ice_slot_id"] for c in explanation["candidate_windows"]],
            ["blocked"])
        self.assertNotIn("wide_open", json.dumps(explanation, sort_keys=True))
        self.assertEqual(explanation["bounds"]["candidate_window_count"], 1)
        self.assertEqual(
            explanation["bounds"]["candidate_window_omitted_count"], 1)

    def test_candidate_details_are_an_allowlist_not_a_pass_through(self):
        """Only the named safe fields survive, whatever the producer sends."""
        explanation = self._build([{
            "code": "curfew_violation",
            "details": {
                "rink_id": "r1",
                "curfew_local": "19:00",
                "slot_end_local": "19:30",
                # None of the rest is allowlisted. ``reason`` is what the
                # shared policy helper actually puts in every details dict;
                # the other two stand in for the operator-entered text a
                # future violation could start carrying.
                "reason": "curfew_violation",
                "rink_name": "Arena v1 Rink r1",
                "operator_note": "call Dana about the late key",
            },
        }])
        details = explanation["candidate_windows"][0]["rejections"][0][
            "details"]
        self.assertEqual(details, {
            "rink_id": "r1",
            "curfew_local": "19:00",
            "slot_end_local": "19:30",
        })
        self.assertNotIn("reason", details)
        serialized = json.dumps(explanation, sort_keys=True)
        for leaked in ("rink_name", "Arena v1", "operator_note",
                       "call Dana"):
            self.assertNotIn(leaked, serialized)

    def test_the_allowlist_reaches_inside_nested_evidence_rows(self):
        """A top-level field allowlist says nothing about a list of rows.

        ``team_overlap.conflicts`` rows are assembled beside a producer whose
        own conflict records carry ``team_name``; ``min_rest.conflicts`` rows
        sit next to the tentative proposal state. Copying a nested dict
        verbatim would make the privacy rule "the allowlist, plus whatever
        today's producer happens to pass".
        """
        explanation = self._build([
            {
                "code": "team_overlap",
                "details": {
                    "team_ids": ["t0"],
                    "conflicts": [{
                        "team_id": "t0",
                        "conflict_source": "existing_game",
                        "conflict_game_id": "g1",
                        "team_name": "Team 0",
                        "note": "coach asked to avoid Tuesdays",
                    }],
                },
            },
            {
                "code": "min_rest",
                "details": {
                    "team_ids": ["t0"],
                    "min_rest_hours": 24,
                    "conflicts": [{
                        "team_id": "t0",
                        "start_time": BASE.isoformat(),
                        "opponent_name": "Team 1",
                    }],
                    "omitted_conflict_count": 0,
                },
            },
        ])
        by_code = {r["code"]: r["details"]
                   for r in explanation["candidate_windows"][0]["rejections"]}
        self.assertEqual(by_code["team_overlap"]["conflicts"], [{
            "team_id": "t0",
            "conflict_source": "existing_game",
            "conflict_game_id": "g1",
        }])
        self.assertEqual(by_code["min_rest"]["conflicts"], [{
            "team_id": "t0",
            "start_time": BASE.isoformat(),
        }])
        serialized = json.dumps(explanation, sort_keys=True)
        for leaked in ("team_name", "Team 0", "opponent_name", "Team 1",
                       "coach asked"):
            self.assertNotIn(leaked, serialized)

    def test_nested_evidence_rows_are_capped_and_count_what_they_dropped(self):
        """The two NESTED caps, which no other test in this file pins.

        The per-candidate and per-pairing caps are enforced by the formatter
        and covered above; these two live in the observer and bound the rows
        INSIDE one rejection. ``min_rest`` is driven directly because the
        scheduler cannot reach the cap: accepted starts for one team are at
        least ``min_rest_hours`` apart by construction, so a candidate window
        can only ever collide with two or three of them. That is exactly the
        kind of clause that survives deletion with a whole suite green.
        """
        con = sched._normalize_constraints({"min_rest_hours": 24})
        slot = IceSlot(id="candidate", rink_id="r1", start_time=BASE,
                       end_time=BASE + timedelta(hours=1))
        # Six starts per team inside the rest window: twelve conflicts.
        team_slots = {
            tid: [BASE + timedelta(minutes=offset)
                  for offset in range(0, 360, 60)]
            for tid in ("t0", "t1")}
        rest = next(r for r in sched._slot_constraint_rejections(
            slot, "t0", "t1", con, team_slots) if r["code"] == "min_rest")
        self.assertEqual(len(rest["details"]["conflicts"]), 4)
        self.assertEqual(rest["details"]["omitted_conflict_count"], 8)
        # Which four is not arbitrary -- canonical order, so two runs over the
        # same facts keep the same rows.
        self.assertEqual(
            rest["details"]["conflicts"],
            sorted(rest["details"]["conflicts"], key=sched._canonical_sort_key))

    def test_team_overlap_reports_at_most_one_row_per_team(self):
        """The second nested cap: two teams, so never more than two rows,
        however many bookings each of them collides with."""
        store = InMemoryStore()
        _seed(store)  # only so team-name lookup resolves like production
        # Four prior games, two for each team of the pairing, all overlapping
        # the candidate window.
        spans = {}
        for index, tid in enumerate(("t0", "t0", "t1", "t1")):
            spans.setdefault(tid, []).append(
                (BASE, BASE + timedelta(hours=1), f"g{index}"))
        slot = IceSlot(id="candidate", rink_id="r1", start_time=BASE,
                       end_time=BASE + timedelta(hours=1))
        code, _message, conflicts = sched._team_overlap_reason(
            store, slot, "t0", "t1", spans)
        self.assertEqual(code, "team_overlap")
        # ANTI-VACUITY: four candidate collisions really were available.
        self.assertEqual(sum(len(v) for v in spans.values()), 4)
        self.assertEqual(len(conflicts), 2)
        self.assertEqual(sorted(c["team_id"] for c in conflicts),
                         ["t0", "t1"])

    def test_blocking_codes_use_the_canonical_rank_not_the_alphabet(self):
        """Scope, then inventory, then the scheduler's evaluation order."""
        codes = ["team_overlap", "holiday", "no_ice_available",
                 "season_blackout"]
        explanation = self._build(
            [{"code": code, "details": {}} for code in codes])
        self.assertEqual(
            explanation["blocking_constraint_codes"],
            ["no_ice_available", "season_blackout", "holiday",
             "team_overlap"])
        # Alphabetical would be holiday, no_ice_available, season_blackout,
        # team_overlap — which changes WHICH three corrections an operator is
        # shown, not merely their order.
        self.assertEqual(
            [a["reason_code"] for a in explanation["alternatives"]],
            ["no_ice_available", "season_blackout", "holiday"])

    def test_an_unknown_future_code_sorts_after_every_known_one(self):
        """A new code must not need a formatter change to stay deterministic."""
        explanation = self._build([
            {"code": "aaa_unknown_future_code", "details": {}},
            {"code": "team_overlap", "details": {}},
        ])
        self.assertEqual(
            explanation["blocking_constraint_codes"],
            ["team_overlap", "aaa_unknown_future_code"])
        self.assertEqual(
            explanation["alternatives"][-1],
            {"action_code": "review_scheduling_input",
             "reason_code": "aaa_unknown_future_code"})


class ExplanationBudgetAndObserverTest(unittest.TestCase):
    def test_active_game_snapshot_is_canonical_not_store_order(self):
        """The shared snapshot both observers stop at the FIRST match in.

        ``_slot_policy_violation`` returns the first conflicting Game it
        meets, so an unsorted snapshot would let store insertion order — not
        a contract on any backend — choose the ``conflict_game_id`` an
        explanation reports.
        """
        store = InMemoryStore()
        _seed(store, team_count=4)
        # Inserted latest-first, so insertion order is the exact reverse of
        # the canonical (start_time, end_time, slot_id, game_id) order.
        for index in reversed(range(4)):
            start = BASE + timedelta(days=index)
            slot_id = _slot(store, f"snap_{index}", "r1", start)
            store.add_game(Game(
                id=f"game_{index}", season_id="se", division_id="d",
                home_team_id="t0", away_team_id="t1", ice_slot_id=slot_id,
                start_time=start, end_time=start + timedelta(minutes=60)))
        pairs = sched._active_game_slot_pairs(store)
        self.assertEqual(
            [(game.id, slot.id) for game, slot in pairs],
            [(f"game_{i}", f"snap_{i}") for i in range(4)])

    def test_explanation_stays_out_of_the_commit_preview_fingerprint(self):
        """#328's ``_draft_fingerprint`` allowlists unscheduled fields by name.

        The commit gate REGENERATES the proposal and refuses on a changed
        ``draft_fingerprint``. This observer reaches deeper into live state
        than the bound fields do (a policy conflict's ``conflict_game_id``,
        which candidate windows the shared budget paid for), so binding it
        would turn an unrelated neighbouring Game into a spurious
        ``preview_stale`` refusal of a batch whose placements, reasons, and
        team conflicts are byte-for-byte identical. Pinned rather than
        assumed: nothing else in the suite fails if it starts being bound.
        """
        store = InMemoryStore()
        api = _seed(store)
        _slot(store, "fp_candidate", "r1", BASE)
        preview = api.draft_season_schedule("d", constraints={
            "season_blackout_dates": [BASE.date().isoformat()]})
        rows = preview["unscheduled"]
        self.assertTrue(rows[0]["explanation"]["candidate_windows"])
        args = ("ls", ["t0", "t1"], preview["draft_games"], rows,
                preview["unschedulable_teams"], preview["already_scheduled"],
                preview["meetings_per_opponent"])
        self.assertEqual(
            preview["draft_fingerprint"], sched._draft_fingerprint(*args))
        rewritten = copy.deepcopy(rows)
        rewritten[0]["explanation"] = {
            "value_object_version": 99,
            "blocking_constraint_codes": ["team_overlap"],
            "candidate_windows": [],
            "alternatives": [],
            "bounds": {},
        }
        self.assertEqual(
            sched._draft_fingerprint(
                args[0], args[1], args[2], rewritten, args[4], args[5],
                args[6]),
            preview["draft_fingerprint"])
        # Control: the fields the gate DOES bind still move it, so the
        # equality above is a statement about `explanation`, not about a
        # fingerprint that ignores `unscheduled` altogether.
        rewritten[0]["reason_codes"] = ["team_overlap"]
        self.assertNotEqual(
            sched._draft_fingerprint(
                args[0], args[1], args[2], rewritten, args[4], args[5],
                args[6]),
            preview["draft_fingerprint"])

    def test_rejection_and_alternative_caps_report_exact_omitted_counts(self):
        codes = [
            "season_blackout", "holiday", "team_blackout", "rink_blackout",
            "max_per_day", "min_rest", "insufficient_playable_time",
            "curfew_violation", "team_overlap",
        ]
        explanation = build_unplaced_explanation(
            pairing={"home_team_id": "t0", "away_team_id": "t1"},
            scope={"season_id": "se", "league_id": "lg"},
            legacy_reason_codes=[codes[0]],
            raw_candidates=[{
                "ice_slot_id": "s", "rink_id": "r", "venue_id": "v",
                "start_time": BASE.isoformat(),
                "end_time": (BASE + timedelta(hours=1)).isoformat(),
                "rejections": [{"code": code, "details": {}}
                               for code in codes],
            }],
            candidate_total=1,
        )
        candidate = explanation["candidate_windows"][0]
        self.assertEqual(
            len(candidate["rejections"]), MAX_REJECTIONS_PER_CANDIDATE)
        self.assertEqual(candidate["omitted_rejection_count"], 1)
        self.assertEqual(
            len(explanation["alternatives"]), MAX_ALTERNATIVES_PER_PAIRING)
        self.assertGreater(explanation["bounds"]["alternative_omitted_count"], 0)

    def test_whole_preview_candidate_budget_stops_at_exact_boundary(self):
        store = InMemoryStore()
        api = _seed(store)
        slots = []
        for index in range(MAX_CANDIDATE_WINDOWS_PER_PAIRING):
            _slot(store, f"budget_{index}", "r1", BASE + timedelta(days=index))
            slots.append(store.get_ice_slot(f"budget_{index}"))
        pair_count = MAX_CANDIDATE_WINDOWS_PER_PREVIEW \
            // MAX_CANDIDATE_WINDOWS_PER_PAIRING + 1
        pairings = [("t0", "t1", "d") for _ in range(pair_count)]
        days = [slot.start_time.date().isoformat() for slot in slots]
        _draft, unscheduled = sched._assign_ice(
            store, pairings, slots,
            {"season_blackout_dates": days},
            policy_check=sched._policy_advisor(store, "se"),
            explanation_context={"season_id": "se", "league_id": "lg"},
        )
        evidence_count = sum(
            len(row["explanation"]["candidate_windows"])
            for row in unscheduled)
        self.assertEqual(evidence_count, MAX_CANDIDATE_WINDOWS_PER_PREVIEW)
        self.assertEqual(
            unscheduled[-1]["explanation"]["candidate_windows"], [])
        self.assertTrue(
            unscheduled[-1]["explanation"]["bounds"]
            ["preview_candidate_budget_limited"])

    def test_explanation_toggle_cannot_change_scheduler_decisions(self):
        """Any observer mutation that changes selection fails this golden."""
        store = InMemoryStore()
        _seed(store, team_count=3)
        _slot(store, "toggle_0", "r1", BASE)
        _slot(store, "toggle_1", "r2", BASE)
        slots = sched._available_game_slots(store)
        pairings = [("t1", "t2", "d"), ("t1", "t0", "d"),
                    ("t0", "t2", "d")]
        active = sched._active_game_slot_pairs(store)
        common = dict(
            policy_check=sched._policy_advisor(store, "se", active),
            team_spans=sched._persisted_team_spans(active),
            explanation_context={"season_id": "se", "league_id": "lg"},
        )
        explained = sched._assign_ice(
            store, pairings, slots, None, explain=True, **common)
        plain = sched._assign_ice(
            store, pairings, slots, None, explain=False, **common)
        self.assertEqual(explained[0], plain[0])
        explained_rows = [
            {key: value for key, value in row.items() if key != "explanation"}
            for row in explained[1]
        ]
        self.assertEqual(explained_rows, plain[1])


class SchedulerExplanationHttpTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), srv.Handler)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(
            target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.thread.join(timeout=5)
        # #384: shutdown() only stops serve_forever; the listening socket is
        # released by server_close(), and the suite has a structural guard.
        cls.httpd.server_close()
        srv.STATE.reset(seed=False)

    def setUp(self):
        srv.STATE.reset(seed=False)
        _seed(srv.STATE.api.store)
        _slot(srv.STATE.api.store, "http_candidate", "r1", BASE)

    def _request(self, opener, method, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}", data=data, method=method)
        if data is not None:
            request.add_header("Content-Type", "application/json")
        try:
            with opener.open(request) as response:
                return response.status, json.loads(response.read() or b"{}")
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read() or b"{}")

    def test_authenticated_preview_serializes_additive_value_object(self):
        client = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(CookieJar()))
        self._request(client, "POST", "/api/auth/login", {
            "username": "admin", "password": "demo"})
        status, preview = self._request(
            client, "POST", "/api/scheduler/draft", {
                "division_id": "d",
                "constraints": {
                    "season_blackout_dates": [BASE.date().isoformat()]},
            })
        self.assertEqual(status, 200, preview)
        explanation = _explanation(preview)
        self.assertEqual(explanation["value_object_version"], 1)
        self.assertEqual(
            explanation["candidate_windows"][0]["ice_slot_id"],
            "http_candidate")


if __name__ == "__main__":
    unittest.main()
