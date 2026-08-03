"""Home/Tasks hub setup-progress — Program-scoped six-workflow completion
state (#204/#330).

Unlike the installation-wide ``get_setup_overview_v2`` / ``get_onboarding_
status_v2``, ``get_setup_progress`` resolves the ACTING Program from the
caller's active context (#159) and reports completion for the six Setup
workflows #204 names (league profile/seasons, permanent teams, season
participation/divisions, clubs/players/staff, venues/rinks/ice, imports/
onboarding) — so the Home/Tasks hub can compute "Continue setup" without the
operator inferring gaps from the data model.

Coverage: per-workflow done/todo boundaries mirroring ``get_onboarding_
status_v2``'s own steps (scoped here instead of installation-wide);
next-incomplete ordering as data is added; the always-"optional" (never
done/todo) "imports and onboarding" step; cross-Program isolation (the whole
point of this endpoint vs. its installation-wide siblings); the empty
no-Program state; and the HTTP route/authz contract (401 signed-out, 403
wrong role, 200 for both League Admin and Arena Manager, 405 on the wrong
method).
"""

import json
import threading
import unittest
import urllib.error
import urllib.request
from http.cookiejar import CookieJar
from http.server import ThreadingHTTPServer

from helpers import BACKEND  # noqa: F401  (ensures sys.path is set up)

from hockey_scheduler.api import ApiService
from hockey_scheduler.domain import Role, SeasonTeamRegistration
from hockey_scheduler.store import InMemoryStore

ADMIN = (Role.LEAGUE_ADMIN, {})
ARENA = (Role.ARENA_MANAGER, {})
_WORKFLOW_KEYS = ["league_season", "teams", "participation", "roster",
                  "facilities", "import"]


def _statuses(progress):
    return {w["key"]: w["status"] for w in progress["workflows"]}


class SetupProgressComputationTest(unittest.TestCase):
    """Facade-level, Memory-backed: the workflow-completion logic itself has
    no concurrency angle (a pure read composed from store methods that each
    already carry their own Memory/SQLite/PostgreSQL parity coverage), so one
    backend is sufficient here — the same scope test_v2_onboarding_status.py
    takes for the sibling logic this mirrors."""

    def _api(self):
        return ApiService(InMemoryStore())

    def test_no_program_is_a_named_empty_state_not_an_error(self):
        api = self._api()
        progress = api.get_setup_progress("u1", *ADMIN)
        self.assertNotIn("error", progress, progress)
        self.assertIsNone(progress["program_id"])
        self.assertEqual(progress["workflows"], [])
        self.assertIsNone(progress["next"])
        self.assertIsNone(progress["next_blocked"])
        self.assertFalse(progress["complete"])

    def test_fresh_program_lists_all_six_workflows_todo_league_first(self):
        api = self._api()
        api.create_user_account("admin", "pw", "league_admin")
        program = api.create_program("Prog", actor_id="admin")
        progress = api.get_setup_progress("admin", *ADMIN)
        self.assertEqual(progress["program_id"], program["id"])
        self.assertEqual(progress["program"]["name"], "Prog")
        self.assertEqual([w["key"] for w in progress["workflows"]],
                         _WORKFLOW_KEYS)
        statuses = _statuses(progress)
        # "import" ("Imports and onboarding") is "optional" from the very
        # start, never "todo" — see test_import_workflow_is_always_optional_
        # never_next_or_complete_blocking for why it carries no done/todo
        # signal of its own (#330 review round 1 finding 5).
        self.assertTrue(all(statuses[k] == "todo" for k in _WORKFLOW_KEYS
                            if k != "import"))
        self.assertEqual(statuses["import"], "optional")
        self.assertEqual(progress["next"]["key"], "league_season")
        self.assertFalse(progress["complete"])

    def test_workflows_flip_done_in_order_as_data_is_added(self):
        api = self._api()
        api.create_user_account("admin", "pw", "league_admin")
        program = api.create_program("Prog", actor_id="admin")
        pid = program["id"]

        def next_key():
            return api.get_setup_progress("admin", *ADMIN)["next"]["key"]

        self.assertEqual(next_key(), "league_season")

        season = api.create_season(pid, "Fall", actor_id="admin")
        league = api.create_league(season["id"], "Adult League", actor_id="admin")
        self.assertEqual(next_key(), "teams")

        club = api.create_club("C", actor_id="admin")
        team = api.create_team(club["id"], None, "T", actor_id="admin",
                               program_id=pid)
        self.assertEqual(next_key(), "participation")

        reg = api.register_team_for_season(season["id"], team["id"],
                                           actor_id="admin",
                                           league_id=league["id"])
        self.assertNotIn("error", reg, reg)
        self.assertEqual(next_key(), "roster")

        api.create_player(team["id"], "Vince Skater", "forward",
                          actor_id="admin")
        # Facilities needs more than an active Season now (#331 review round
        # 5 finding 1): with no Rink holding active Season venue access yet,
        # the Ice Availability Builder's own preview provably yields zero
        # slots -- `next` must stay blocked rather than offer a dead-end
        # "Add Ice" CTA, even though facilities is otherwise correctly next
        # in order.
        blocked = api.get_setup_progress("admin", *ADMIN)
        self.assertIsNone(blocked["next"], blocked)
        self.assertEqual(blocked["next_blocked"]["key"], "facilities")
        self.assertEqual(blocked["next_blocked"]["reason"], "venue_access_missing")

        venue = api.create_venue("V", league_id=pid, actor_id="admin")
        rink = api.create_rink(venue["id"], "R", actor_id="admin")
        # A Rink now exists, but access has not been GRANTED yet -- proves
        # the gate checks the grant itself, not mere Rink existence.
        still_blocked = api.get_setup_progress("admin", *ADMIN)
        self.assertIsNone(still_blocked["next"], still_blocked)
        self.assertEqual(still_blocked["next_blocked"]["reason"],
                         "venue_access_missing")

        # Granting access (with no ice slot yet) makes facilities genuinely
        # SAFE -- `next` finally names it -- while it is still "todo" (no
        # slot exists yet to flip the "done" status).
        api.grant_season_venue_access(season["id"], venue["id"],
                                      actor_id="admin")
        self.assertEqual(next_key(), "facilities")

        api.create_ice_slot(rink["id"], "2026-09-01T18:30:00+00:00",
                            "2026-09-01T20:00:00+00:00", actor_id="admin")

        final = api.get_setup_progress("admin", *ADMIN)
        self.assertIsNone(final["next"], final)
        self.assertTrue(final["complete"], final)
        # "Imports and onboarding" reports "optional", not "done" — it has
        # no real completion signal of its own, so completing 1-5 must never
        # flip it (see test_import_workflow_is_always_optional_never_next_
        # or_complete_blocking) — but it must still be LISTED so it stays
        # reachable as its own hub entry point regardless (#330).
        self.assertEqual(_statuses(final)["import"], "optional")
        self.assertEqual([w["key"] for w in final["workflows"]],
                         _WORKFLOW_KEYS)

    def test_import_workflow_is_always_optional_never_next_or_complete_blocking(self):
        """#330 review round 1 finding 5: the prior shape derived "imports
        and onboarding"'s done/todo state from whether workflows 1-5 all
        happened to be done — an invented rule with no grounding (two of the
        three import-commit paths write no season/program-derivable field
        into their own audit summary row, so there is no real Program-scoped
        "has an import run here" signal to compute a state from), and as a
        side effect meant this workflow could never itself be offered as
        `next`. It now reports a third status, "optional", from the very
        first call on a brand new Program through to a fully complete one —
        never "todo" (so it can never become `next`, regardless of a role
        holding the MANAGE_SETUP permission mapped to it in
        ``_WORKFLOW_PERMISSION``) and never "done" (so it can never silently
        satisfy `complete` on its own, and completing 1-5 must never flip
        it)."""
        api = self._api()
        api.create_user_account("admin", "pw", "league_admin")
        program = api.create_program("Prog", actor_id="admin")
        pid = program["id"]

        fresh = api.get_setup_progress("admin", *ADMIN)
        self.assertEqual(_statuses(fresh)["import"], "optional")
        self.assertEqual(fresh["next"]["key"], "league_season",
                         "import must never be offered as next, even on a "
                         "fresh Program with nothing else done yet")

        season = api.create_season(pid, "Fall", actor_id="admin")
        league = api.create_league(season["id"], "Adult League",
                                   actor_id="admin")
        club = api.create_club("C", actor_id="admin")
        team = api.create_team(club["id"], None, "T", actor_id="admin",
                               program_id=pid)
        reg = api.register_team_for_season(season["id"], team["id"],
                                           actor_id="admin",
                                           league_id=league["id"])
        self.assertNotIn("error", reg, reg)
        api.create_player(team["id"], "Vince Skater", "forward",
                          actor_id="admin")
        venue = api.create_venue("V", league_id=pid, actor_id="admin")
        rink = api.create_rink(venue["id"], "R", actor_id="admin")
        api.create_ice_slot(rink["id"], "2026-09-01T18:30:00+00:00",
                            "2026-09-01T20:00:00+00:00", actor_id="admin")
        api.grant_season_venue_access(season["id"], venue["id"],
                                      actor_id="admin")

        final = api.get_setup_progress("admin", *ADMIN)
        self.assertTrue(final["complete"], final)
        self.assertIsNone(final["next"], final)
        self.assertEqual(_statuses(final)["import"], "optional",
                         "completing 1-5 must never flip import to done")

    def test_league_required_per_season_even_with_another_season_ok(self):
        """Mirrors get_onboarding_status_v2's own per-Season League rule: a
        SECOND Season with no grouping League keeps "league_season" todo even
        though the first Season has one."""
        api = self._api()
        api.create_user_account("admin", "pw", "league_admin")
        program = api.create_program("Prog", actor_id="admin")
        season1 = api.create_season(program["id"], "Fall", actor_id="admin")
        api.create_league(season1["id"], "Adult League", actor_id="admin")
        api.create_season(program["id"], "Spring", actor_id="admin")  # no league

        progress = api.get_setup_progress("admin", *ADMIN)
        self.assertEqual(_statuses(progress)["league_season"], "todo")
        self.assertEqual(progress["next"]["key"], "league_season")

    def test_cross_program_isolation(self):
        """The whole point of this endpoint vs. its installation-wide
        siblings: Program B's team must never make Program A's "teams"
        workflow read done."""
        api = self._api()
        api.create_user_account("admin", "pw", "league_admin")
        program_a = api.create_program("Prog A", actor_id="admin")
        program_b = api.create_program("Prog B", actor_id="admin")
        season_b = api.create_season(program_b["id"], "Fall", actor_id="admin")
        api.create_league(season_b["id"], "B League", actor_id="admin")
        club = api.create_club("C", actor_id="admin")
        team_b = api.create_team(club["id"], None, "T-B", actor_id="admin",
                                 program_id=program_b["id"])
        self.assertNotIn("error", team_b, team_b)

        # Force resolution onto Program A specifically via an explicit
        # active-context selection (#159) rather than relying on fallback
        # ordering between two equally-authorized Programs.
        api.set_active_context("admin", Role.LEAGUE_ADMIN, {},
                               program_a["id"], None)
        progress_a = api.get_setup_progress("admin", *ADMIN)
        self.assertEqual(progress_a["program_id"], program_a["id"])
        self.assertEqual(_statuses(progress_a)["teams"], "todo")

        api.set_active_context("admin", Role.LEAGUE_ADMIN, {},
                               program_b["id"], None)
        progress_b = api.get_setup_progress("admin", *ADMIN)
        self.assertEqual(progress_b["program_id"], program_b["id"])
        self.assertEqual(_statuses(progress_b)["teams"], "done")

    def test_next_action_is_role_aware(self):
        """#330 review round 1 finding 1: an Arena Manager (MANAGE_ARENA
        only, no MANAGE_SETUP) must never be handed a MANAGE_SETUP-only
        action like "Add Season" — with a Season already selected AND
        venue access already granted (so facilities is genuinely
        executable, not blocked on season_missing or, since #331 review
        round 5 finding 1, venue_access_missing — see
        test_facilities_next_is_blocked_without_a_season and
        test_facilities_next_is_blocked_without_venue_access for those two
        cases) they're routed straight to facilities, the one workflow
        their role can actually execute; League Admin's own ordering is
        unaffected.

        #331 review round 3 finding 1's redaction half: unlike the prior
        contract ("the GLOBAL workflow list stays identical across roles"),
        the response's `workflows` is now filtered to what each role can
        actually manage — Arena Manager's list holds only "facilities" (the
        one workflow keyed to MANAGE_ARENA), never the League-Admin-only
        completion signals/counts for league_season/teams/participation/
        roster/import. League Admin (who holds both MANAGE_SETUP and
        MANAGE_ARENA) still sees all six."""
        api = self._api()
        api.create_user_account("admin", "pw", "league_admin")
        program = api.create_program("Prog", actor_id="admin")
        season = api.create_season(program["id"], "Fall", actor_id="admin")
        venue = api.create_venue("V", league_id=program["id"], actor_id="admin")
        api.create_rink(venue["id"], "R", actor_id="admin")
        api.grant_season_venue_access(season["id"], venue["id"], actor_id="admin")

        admin_progress = api.get_setup_progress("admin", *ADMIN)
        self.assertEqual(admin_progress["next"]["key"], "league_season")
        self.assertEqual(admin_progress["next"]["primary_action"], "Add Season")
        self.assertEqual([w["key"] for w in admin_progress["workflows"]],
                         _WORKFLOW_KEYS)

        arena_progress = api.get_setup_progress("admin", *ARENA)
        self.assertEqual(arena_progress["next"]["key"], "facilities",
                         f"Arena Manager must get an executable action, "
                         f"got {arena_progress['next']}")
        self.assertEqual(arena_progress["next"]["primary_action"], "Add Ice")
        self.assertEqual([w["key"] for w in arena_progress["workflows"]],
                         ["facilities"],
                         "Arena Manager must never receive League-Admin-only "
                         f"workflow detail, got {arena_progress['workflows']}")

    def test_next_action_is_none_but_not_complete_when_nothing_actionable_for_role(self):
        """Once facilities (the only Arena-Manager-actionable workflow) is
        done but League-Admin-only workflows remain, Arena Manager's `next`
        must go None WITHOUT `complete` becoming true — "nothing more for
        you" is a different claim from "the Program's setup is finished"."""
        api = self._api()
        api.create_user_account("admin", "pw", "league_admin")
        program = api.create_program("Prog", actor_id="admin")
        season = api.create_season(program["id"], "Fall", actor_id="admin")
        venue = api.create_venue("V", league_id=program["id"], actor_id="admin")
        rink = api.create_rink(venue["id"], "R", actor_id="admin")
        api.create_ice_slot(rink["id"], "2026-09-01T18:30:00+00:00",
                            "2026-09-01T20:00:00+00:00", actor_id="admin")
        api.grant_season_venue_access(season["id"], venue["id"], actor_id="admin")
        # No league/team/registration/player yet — all League-Admin-only.

        arena_progress = api.get_setup_progress("admin", *ARENA)
        self.assertEqual(_statuses(arena_progress)["facilities"], "done")
        self.assertIsNone(arena_progress["next"], arena_progress)
        self.assertFalse(arena_progress["complete"])

        admin_progress = api.get_setup_progress("admin", *ADMIN)
        self.assertEqual(admin_progress["next"]["key"], "league_season")

    def test_arena_manager_complete_is_none_and_unaffected_by_league_admin_only_changes(self):
        """#331 review round 5 finding 3: `complete` must not vary based on
        workflows an Arena Manager cannot even see -- exposing the FULL
        list's real boolean unconditionally let a change to a
        League-Admin-only workflow (invisible to Arena Manager) flip a bit
        in Arena Manager's OWN response, an information leak through the
        very redaction boundary `workflows` itself holds. Arena Manager's
        `complete` must read `None` (never a real True/False -- they can
        never verify a whole-Program claim from a partial view), and a
        NONINTERFERENCE property must hold: mutating ONLY League-Admin-only
        workflow state -- even a mutation that flips the REAL,
        League-Admin-visible `complete` all the way from False to True --
        must leave Arena Manager's entire payload provably unchanged."""
        api = self._api()
        api.create_user_account("admin", "pw", "league_admin")
        program = api.create_program("Prog", actor_id="admin")
        season = api.create_season(program["id"], "Fall", actor_id="admin")
        venue = api.create_venue("V", league_id=program["id"], actor_id="admin")
        rink = api.create_rink(venue["id"], "R", actor_id="admin")
        api.create_ice_slot(rink["id"], "2026-09-01T18:30:00+00:00",
                            "2026-09-01T20:00:00+00:00", actor_id="admin")
        api.grant_season_venue_access(season["id"], venue["id"], actor_id="admin")

        before = api.get_setup_progress("admin", *ARENA)
        self.assertEqual(_statuses(before)["facilities"], "done")
        self.assertIsNone(
            before["complete"],
            "Arena Manager can never verify a whole-Program claim from a "
            "partial view -- must be None, not a real True/False")
        self.assertIsNone(before["next"])
        self.assertIsNone(before["next_blocked"])

        # Mutate ONLY League-Admin-only workflows (league_season, teams,
        # participation, roster) -- all invisible to Arena Manager. This is
        # enough to flip the WHOLE Program to genuinely complete.
        league = api.create_league(season["id"], "Adult League", actor_id="admin")
        club = api.create_club("C", actor_id="admin")
        team = api.create_team(club["id"], None, "T", actor_id="admin",
                               league_id=league["id"])
        reg = api.register_team_for_season(season["id"], team["id"],
                                           actor_id="admin",
                                           league_id=league["id"])
        self.assertNotIn("error", reg, reg)
        api.create_player(team["id"], "Vince Skater", "forward", actor_id="admin")

        admin_now = api.get_setup_progress("admin", *ADMIN)
        self.assertTrue(admin_now["complete"], admin_now)

        after = api.get_setup_progress("admin", *ARENA)
        self.assertEqual(
            after, before,
            f"Arena Manager's entire payload must be unchanged by "
            f"League-Admin-only workflow mutations -- before={before}, "
            f"after={after}")

    def test_participation_and_facilities_scope_to_selected_season_only(self):
        """#330 review round 1 finding 2: an OLDER Season's registrations/
        venue-access/ice must never make participation/facilities read done
        for a DIFFERENT, newly-selected Season that has none of its own."""
        api = self._api()
        api.create_user_account("admin", "pw", "league_admin")
        program = api.create_program("Prog", actor_id="admin")

        # Old season: fully set up (league, team, registration, venue+ice).
        old_season = api.create_season(program["id"], "Old", actor_id="admin")
        old_league = api.create_league(old_season["id"], "Old League",
                                       actor_id="admin")
        club = api.create_club("C", actor_id="admin")
        team = api.create_team(club["id"], None, "T", actor_id="admin",
                               program_id=program["id"])
        reg = api.register_team_for_season(old_season["id"], team["id"],
                                           actor_id="admin",
                                           league_id=old_league["id"])
        self.assertNotIn("error", reg, reg)
        old_venue = api.create_venue("OldV", league_id=program["id"],
                                     actor_id="admin")
        old_rink = api.create_rink(old_venue["id"], "OldR", actor_id="admin")
        api.create_ice_slot(old_rink["id"], "2026-09-01T18:30:00+00:00",
                            "2026-09-01T20:00:00+00:00", actor_id="admin")
        api.grant_season_venue_access(old_season["id"], old_venue["id"],
                                      actor_id="admin")

        # New season: has a league (so league_season stays satisfied
        # Program-wide) but NOTHING of its own for participation/facilities.
        new_season = api.create_season(program["id"], "New", actor_id="admin")
        api.create_league(new_season["id"], "New League", actor_id="admin")

        # Select the NEW season explicitly.
        api.set_active_context("admin", Role.LEAGUE_ADMIN, {},
                               program["id"], new_season["id"])
        progress = api.get_setup_progress("admin", *ADMIN)
        self.assertEqual(progress["program_id"], program["id"])
        statuses = _statuses(progress)
        self.assertEqual(
            statuses["participation"], "todo",
            "the OLD season's registration must not satisfy the NEW season")
        self.assertEqual(
            statuses["facilities"], "todo",
            "the OLD season's ice/venue-access must not satisfy the NEW season")
        self.assertEqual(
            statuses["league_season"], "done",
            "both seasons DO have a league — this workflow stays Program-wide")

        # Selecting the OLD season again correctly reads it as fully done.
        api.set_active_context("admin", Role.LEAGUE_ADMIN, {},
                               program["id"], old_season["id"])
        progress_old = api.get_setup_progress("admin", *ADMIN)
        statuses_old = _statuses(progress_old)
        self.assertEqual(statuses_old["participation"], "done")
        self.assertEqual(statuses_old["facilities"], "done")

    def test_facilities_next_is_blocked_without_a_season(self):
        """#331 review round 3 finding 1: on a fresh Program with no Season
        at all, Arena Manager's only permitted workflow (facilities) is not
        safe to execute yet — the real Ice Availability Builder write fails
        `season_missing` (league_scope.assign_game_ice) with nothing to
        attach ice to. `next` must be None, not the dead-end "Add Ice" CTA
        the pre-round-3 endpoint handed out here, and `next_blocked` must
        name facilities with actionable guidance so the operator (who, as
        Arena Manager, cannot create a Season themselves either) is told
        what's missing rather than routed into a silent failure."""
        api = self._api()
        api.create_user_account("admin", "pw", "league_admin")
        api.create_program("Prog", actor_id="admin")

        arena_progress = api.get_setup_progress("admin", *ARENA)
        self.assertIsNone(arena_progress["next"], arena_progress)
        self.assertEqual(arena_progress["next_blocked"],
                         {"key": "facilities", "label": "Venues, rinks and ice",
                          "reason": "season_missing",
                          "detail": "Create or select a Season before adding ice."})
        self.assertFalse(arena_progress["complete"])

        # League Admin is UNAFFECTED: "Add Season" (league_season) has no
        # Season prerequisite of its own -- it's the thing that creates one.
        admin_progress = api.get_setup_progress("admin", *ADMIN)
        self.assertEqual(admin_progress["next"]["key"], "league_season")
        self.assertIsNone(admin_progress["next_blocked"])

    def test_facilities_next_is_blocked_when_program_selected_but_no_season_chosen(self):
        """The same season_missing gap applies even when the Program DOES
        have Seasons, as long as none is the currently-selected active
        context (#159) -- `next`/`next_blocked` are computed from the
        session's resolved Season, not "does any Season exist somewhere".
        Distinct from the fresh-Program case above (named separately in
        #331 review round 3's required regression contexts: "no-Season" vs
        "Program-only")."""
        api = self._api()
        api.create_user_account("admin", "pw", "league_admin")
        program = api.create_program("Prog", actor_id="admin")
        api.create_season(program["id"], "Fall", actor_id="admin")
        api.set_active_context("admin", Role.LEAGUE_ADMIN, {},
                               program["id"], None)  # Program-only, no Season chosen

        arena_progress = api.get_setup_progress("admin", *ARENA)
        self.assertIsNone(arena_progress["next"], arena_progress)
        self.assertEqual(arena_progress["next_blocked"]["key"], "facilities")
        self.assertEqual(arena_progress["next_blocked"]["reason"], "season_missing")

    def test_facilities_next_is_blocked_without_venue_access(self):
        """#331 review round 5 finding 1: an active, resolved Season alone
        is not enough for facilities to be safe -- with a Venue and Rink
        but NO active SeasonVenueAccess granted, the Ice Availability
        Builder's real preview provably yields zero slots (every requested
        rink lands in venue_access_missing), and Arena Manager -- who
        holds MANAGE_ARENA but not MANAGE_SETUP -- cannot grant that
        access themselves, making this a true dead end rather than a
        same-role-solvable gap. `next` must stay None with `next_blocked`
        naming facilities and venue_access_missing, not the dead-end "Add
        Ice" CTA. Once a League Admin grants access, the workflow must
        genuinely advance for Arena Manager -- not just flip a status
        bit."""
        api = self._api()
        api.create_user_account("admin", "pw", "league_admin")
        program = api.create_program("Prog", actor_id="admin")
        season = api.create_season(program["id"], "Fall", actor_id="admin")
        venue = api.create_venue("V", league_id=program["id"], actor_id="admin")
        api.create_rink(venue["id"], "R", actor_id="admin")
        api.set_active_context("admin", Role.LEAGUE_ADMIN, {},
                               program["id"], season["id"])

        arena_progress = api.get_setup_progress("admin", *ARENA)
        self.assertIsNone(arena_progress["next"], arena_progress)
        self.assertEqual(
            arena_progress["next_blocked"],
            {"key": "facilities", "label": "Venues, rinks and ice",
             "reason": "venue_access_missing",
             "detail": "No rink has venue access granted for Season 'Fall' "
                       "yet — a League Admin must grant access to at least "
                       "one rink before ice can be added."})

        api.grant_season_venue_access(season["id"], venue["id"], actor_id="admin")
        advanced = api.get_setup_progress("admin", *ARENA)
        self.assertEqual(advanced["next"]["key"], "facilities", advanced)
        self.assertIsNone(advanced["next_blocked"])

    def test_participation_next_is_blocked_when_program_selected_but_no_season_chosen(self):
        """#331 review round 4: "participation" is blocked by season_missing
        exactly like "facilities" is when no Season is resolved -- not just
        because ``register_team_for_season`` itself needs one, but because
        its real destination (``focusParticipationRegisterControl()``) needs
        an exact selected Season to deep-link/focus the specific Register
        control; with none resolved it can only fall back to a generic,
        unbound landing, which is not the precise binding #330's round-2
        review already required. League Admin whose League/Teams are
        already done (a realistic "just needs to register a team" state)
        must not receive an enabled Register Team CTA here."""
        api = self._api()
        api.create_user_account("admin", "pw", "league_admin")
        program = api.create_program("Prog", actor_id="admin")
        season = api.create_season(program["id"], "Fall", actor_id="admin")
        api.create_league(season["id"], "Adult League", actor_id="admin")
        club = api.create_club("C", actor_id="admin")
        api.create_team(club["id"], None, "T", actor_id="admin",
                        program_id=program["id"])
        api.set_active_context("admin", Role.LEAGUE_ADMIN, {},
                               program["id"], None)  # Program-only, no Season chosen

        progress = api.get_setup_progress("admin", *ADMIN)
        statuses = _statuses(progress)
        self.assertEqual(statuses["league_season"], "done")
        self.assertEqual(statuses["teams"], "done")
        self.assertEqual(statuses["participation"], "todo")
        self.assertIsNone(progress["next"], progress)
        self.assertEqual(progress["next_blocked"]["key"], "participation")
        self.assertEqual(progress["next_blocked"]["reason"], "season_missing")

    def test_participation_next_is_blocked_when_selected_season_is_archived(self):
        """#331 review round 3 finding 1: with an ARCHIVED Season selected,
        League Admin's "participation" is permitted (MANAGE_SETUP) but not
        safe -- the real write fails `season_archived`
        (season_guard.require_active_season, read-only until an authorized
        reopen). Every other workflow is already done, so participation is
        the only remaining todo+permitted candidate: `next` must be None
        with `next_blocked` naming participation and the archived Season by
        name, not the dead-end "Register Team" CTA the pre-round-3 endpoint
        handed out here."""
        api = self._api()
        api.create_user_account("admin", "pw", "league_admin")
        program = api.create_program("Prog", actor_id="admin")
        season = api.create_season(program["id"], "Fall", actor_id="admin")
        api.create_league(season["id"], "Adult League", actor_id="admin")
        club = api.create_club("C", actor_id="admin")
        team = api.create_team(club["id"], None, "T", actor_id="admin",
                               program_id=program["id"])
        api.create_player(team["id"], "Vince Skater", "forward", actor_id="admin")
        venue = api.create_venue("V", league_id=program["id"], actor_id="admin")
        rink = api.create_rink(venue["id"], "R", actor_id="admin")
        api.create_ice_slot(rink["id"], "2026-09-01T18:30:00+00:00",
                            "2026-09-01T20:00:00+00:00", actor_id="admin")
        api.grant_season_venue_access(season["id"], venue["id"], actor_id="admin")
        # league_season/teams/roster/facilities are all done now; only
        # participation is left -- archive the Season before registering
        # anyone, so participation stays "todo" AND becomes unsafe.
        archived = api.archive_season(season["id"], reason="year-end close",
                                      actor_id="admin")
        self.assertNotIn("error", archived, archived)
        api.set_active_context("admin", Role.LEAGUE_ADMIN, {},
                               program["id"], season["id"])

        progress = api.get_setup_progress("admin", *ADMIN)
        statuses = _statuses(progress)
        self.assertEqual(statuses["participation"], "todo")
        self.assertTrue(all(statuses[k] == "done"
                            for k in ("league_season", "teams", "roster", "facilities")))
        self.assertIsNone(progress["next"], progress)
        self.assertEqual(progress["next_blocked"]["key"], "participation")
        self.assertEqual(progress["next_blocked"]["reason"], "season_archived")
        self.assertIn("Fall", progress["next_blocked"]["detail"])
        self.assertFalse(progress["complete"],
                         "participation is still todo -- archiving must not "
                         "fake completion")

    def test_participation_next_is_blocked_when_no_team_matches_the_seasons_league(self):
        """#331 review round 5 finding 2: an active, resolved Season alone
        is not enough for participation to be safe -- a Team permanently
        bound to League A (from an earlier registration, the realistic way
        a Team acquires a permanent League) cannot register into a
        DIFFERENT Season whose only League is B; register_team_for_season's
        own rule 7 rejects with team_league_mismatch regardless of which
        team the operator picks, since this Program has no OTHER, eligible
        team. `next` must stay None with `next_blocked` naming
        participation and team_league_mismatch, not a dead-end "Register
        Team" CTA. Establishing an eligible Team must genuinely unblock it,
        and the focused control must then write into exactly this Season."""
        api = self._api()
        api.create_user_account("admin", "pw", "league_admin")
        program = api.create_program("Prog", actor_id="admin")

        # Team becomes permanently bound to League A via an earlier
        # Season's registration.
        season_a = api.create_season(program["id"], "Season A", actor_id="admin")
        league_a = api.create_league(season_a["id"], "League A", actor_id="admin")
        club = api.create_club("C", actor_id="admin")
        team = api.create_team(club["id"], None, "Team", actor_id="admin",
                               program_id=program["id"])
        reg_a = api.register_team_for_season(season_a["id"], team["id"],
                                             actor_id="admin",
                                             league_id=league_a["id"])
        self.assertNotIn("error", reg_a, reg_a)

        # Season B has only League B -- Team is now permanently ineligible.
        season_b = api.create_season(program["id"], "Season B", actor_id="admin")
        league_b = api.create_league(season_b["id"], "League B", actor_id="admin")
        api.set_active_context("admin", Role.LEAGUE_ADMIN, {},
                               program["id"], season_b["id"])

        progress = api.get_setup_progress("admin", *ADMIN)
        statuses = _statuses(progress)
        self.assertEqual(statuses["league_season"], "done")
        self.assertEqual(statuses["teams"], "done")
        self.assertEqual(statuses["participation"], "todo")
        self.assertIsNone(progress["next"], progress)
        self.assertEqual(
            progress["next_blocked"],
            {"key": "participation", "label": "Season participation and divisions",
             "reason": "team_league_mismatch",
             "detail": "No permanent team is eligible to register in Season "
                       "'Season B' yet — add a team under a matching "
                       "league, or add a league to this season that "
                       "matches an existing team, before registering "
                       "teams."})

        # A SECOND Team, created explicitly under League B (the realistic
        # remediation the guidance message itself suggests: "add a team
        # under a matching league"), must genuinely unblock it -- not just
        # flip a status bit.
        team2 = api.create_team(club["id"], None, "Team2", actor_id="admin",
                                league_id=league_b["id"])
        self.assertNotIn("error", team2, team2)
        advanced = api.get_setup_progress("admin", *ADMIN)
        self.assertEqual(advanced["next"]["key"], "participation", advanced)
        self.assertIsNone(advanced["next_blocked"])

        # And the focused control writes into exactly Season B -- Season A's
        # own registration for the mismatched Team is untouched.
        reg_b = api.register_team_for_season(season_b["id"], team2["id"],
                                             actor_id="admin",
                                             league_id=league_b["id"])
        self.assertNotIn("error", reg_b, reg_b)
        final = api.get_setup_progress("admin", *ADMIN)
        self.assertEqual(_statuses(final)["participation"], "done")
        # #331 review round 19: no stray/invalid row exists in this scenario
        # -- `attention` must be absent entirely, not present-but-empty, the
        # same "omitted when irrelevant" contract `next_blocked` follows.
        final_participation = next(w for w in final["workflows"]
                                   if w["key"] == "participation")
        self.assertNotIn("attention", final_participation)
        regs_a = api.list_season_team_registrations(season_a["id"])
        self.assertEqual([r["team_id"] for r in regs_a["registrations"]
                          if r["active"]], [team["id"]],
                         "Season A's own registration must be untouched")

    def test_stale_wrong_league_active_registration_never_counts_as_done(self):
        """#331 review round 18: a Team can hold more than one registration
        row in one Season across different LeagueSeasons (migration 035:
        unique only on (team_id, league_season_id)). transfer_team_to_league
        deliberately leaves a Season's active registration frozen at the
        Team's OLD League while Team.league_id moves on -- typically ended-
        Season history, but the same shape can occur through any write path
        predating Rule 7. Whatever its cause, an active registration whose
        LeagueSeason no longer matches the Team's current permanent League
        is not operationally schedulable (create_game/team_registration_valid
        both reject it), so it must never report "participation: done" --
        unlike test_participation_next_is_blocked_when_no_team_matches_the_
        seasons_league above, a genuinely eligible Team DOES exist here, so
        this must surface as an actionable `next`, not a `next_blocked`."""
        api = self._api()
        api.create_user_account("admin", "pw", "league_admin")
        program = api.create_program("Prog", actor_id="admin")
        season = api.create_season(program["id"], "Season", actor_id="admin")
        league_a = api.create_league(season["id"], "League A", actor_id="admin")
        league_b = api.create_league(season["id"], "League B", actor_id="admin")
        club = api.create_club("C", actor_id="admin")
        team = api.create_team(club["id"], None, "Team", actor_id="admin",
                               league_id=league_a["id"])
        # A stale ACTIVE row under League B -- NOT the Team's current
        # permanent League A -- injected directly (no current write path
        # can leave this behind in a fresh season; it reproduces the
        # historical-transfer/legacy-drift shape this fix defends against).
        ls_b = api.store.league_season_for(league_b["id"], season["id"])
        stale = SeasonTeamRegistration(
            id=api.store.next_id("streg"), league_season_id=ls_b.id,
            team_id=team["id"], division_id=None, active=True)
        api.store.add_season_team_registration(stale)

        api.set_active_context("admin", Role.LEAGUE_ADMIN, {},
                               program["id"], season["id"])
        progress = api.get_setup_progress("admin", *ADMIN)
        statuses = _statuses(progress)
        self.assertEqual(statuses["league_season"], "done")
        self.assertEqual(statuses["teams"], "done")
        self.assertEqual(statuses["participation"], "todo", progress)
        # Team A IS genuinely eligible (its own permanent league), so this
        # is actionable, not blocked.
        self.assertIsNotNone(progress["next"], progress)
        self.assertEqual(progress["next"]["key"], "participation", progress)
        self.assertIsNone(progress["next_blocked"], progress)
        # #331 review round 19: the stray row isn't just excluded from
        # `schedulable` -- it must be surfaced as needing attention too, so
        # an operator can discover it exists at all.
        participation = next(w for w in progress["workflows"]
                             if w["key"] == "participation")
        self.assertEqual(
            participation["attention"],
            {"reason": "invalid_registrations", "count": 1,
             "affected_registration_ids": [stale.id],
             "detail": "1 registration(s) in this season don't match "
                       "their team's permanent league or division; "
                       "resolve them in Season participation."})

        # #331 review round 20: registering the Team into its OWN permanent
        # League A is no longer accepted while the stale League B row is
        # still active -- live participation means EXACTLY one active
        # registration this Season, full stop, so this must reject before
        # any write, naming the stray as the conflict.
        blocked = api.register_team_for_season(season["id"], team["id"],
                                               actor_id="admin",
                                               league_id=league_a["id"])
        self.assertEqual(blocked["error"]["details"]["reason"],
                         "team_registration_conflict", blocked)
        self.assertEqual(blocked["error"]["details"]["affected_registration_ids"],
                         [stale.id])
        self.assertEqual(len(api.store.registrations_for_season(season["id"])),
                         1)  # zero mutation -- only the stray still exists

        # The operator explicitly resolves it first -- deactivating the
        # stray via the same "Remove" action Season participation's own UI
        # already offers -- and only THEN does League A's registration
        # succeed.
        removed = api.unregister_team_from_season(stale.id, actor_id="admin")
        self.assertNotIn("error", removed, removed)
        reg_a = api.register_team_for_season(season["id"], team["id"],
                                             actor_id="admin",
                                             league_id=league_a["id"])
        self.assertNotIn("error", reg_a, reg_a)
        after = api.get_setup_progress("admin", *ADMIN)
        self.assertEqual(_statuses(after)["participation"], "done")
        still_stale = api.store.get_season_team_registration(stale.id)
        self.assertFalse(still_stale.active)
        self.assertEqual(still_stale.league_season_id, ls_b.id)
        # The stray is inactive now (history, not live participation) -- it
        # no longer needs attention.
        after_participation = next(w for w in after["workflows"]
                                   if w["key"] == "participation")
        self.assertNotIn("attention", after_participation, after_participation)

    def test_valid_registration_plus_active_stray_is_not_reported_complete(self):
        """#331 review round 21 finding 2: unlike the test above (a Team NEVER
        successfully registered at its own permanent League), this Team DOES
        hold a genuinely valid row at League A -- exactly the shape the OLD
        per-row check counted as `schedulable` on its own, since that row's
        League matches the Team's in isolation. The shared season-wide
        resolver (`team_registration_valid`) rejects it anyway: this Team
        has TWO active rows this Season (League A's real one, League B's
        stray), so neither is trusted as live participation. With every
        OTHER workflow complete, the reproduction isolates the bug exactly:
        the OLD code reported `status: "done"`, `complete: True`, `next:
        None` here -- an operator steered toward Schedule for a Team the
        production create_game/move_game/publish_game resolver would reject
        outright."""
        api = self._api()
        api.create_user_account("admin", "pw", "league_admin")
        program = api.create_program("Prog", actor_id="admin")
        season = api.create_season(program["id"], "Season", actor_id="admin")
        league_a = api.create_league(season["id"], "League A", actor_id="admin")
        league_b = api.create_league(season["id"], "League B", actor_id="admin")
        club = api.create_club("C", actor_id="admin")
        team = api.create_team(club["id"], None, "Team", actor_id="admin",
                               league_id=league_a["id"])
        reg = api.register_team_for_season(season["id"], team["id"],
                                           actor_id="admin",
                                           league_id=league_a["id"])
        self.assertNotIn("error", reg, reg)
        api.create_player(team["id"], "Vince Skater", "forward", actor_id="admin")
        venue = api.create_venue("V", league_id=program["id"], actor_id="admin")
        rink = api.create_rink(venue["id"], "R", actor_id="admin")
        api.create_ice_slot(rink["id"], "2026-09-01T18:30:00+00:00",
                            "2026-09-01T20:00:00+00:00", actor_id="admin")
        api.grant_season_venue_access(season["id"], venue["id"], actor_id="admin")

        # Sanity check the fixture: without the stray, this is genuinely
        # complete already.
        api.set_active_context("admin", Role.LEAGUE_ADMIN, {},
                               program["id"], season["id"])
        clean = api.get_setup_progress("admin", *ADMIN)
        self.assertTrue(clean["complete"], clean)

        ls_b = api.store.league_season_for(league_b["id"], season["id"])
        stray = SeasonTeamRegistration(
            id=api.store.next_id("streg"), league_season_id=ls_b.id,
            team_id=team["id"], division_id=None, active=True)
        api.store.add_season_team_registration(stray)

        progress = api.get_setup_progress("admin", *ADMIN)
        statuses = _statuses(progress)
        self.assertEqual(statuses["participation"], "todo", progress)
        self.assertFalse(progress["complete"], progress)
        self.assertIsNotNone(progress["next"], progress)
        self.assertEqual(progress["next"]["key"], "participation", progress)
        participation = next(w for w in progress["workflows"]
                             if w["key"] == "participation")
        self.assertEqual(participation["attention"]["count"], 2, participation)
        self.assertEqual(
            set(participation["attention"]["affected_registration_ids"]),
            {reg["id"], stray.id})

    def test_inactive_sibling_at_a_different_league_season_still_reads_complete(self):
        """#331 review round 21 finding 2 sanity check -- the required
        correction's OWN stated boundary: "exactly one expected active row
        with inactive historical siblings must remain complete." A Team
        transferred from League B to its current permanent League A leaves
        an INACTIVE row behind at League B (history, not live participation)
        -- this must never be confused with the active-stray case above."""
        api = self._api()
        api.create_user_account("admin", "pw", "league_admin")
        program = api.create_program("Prog", actor_id="admin")
        season = api.create_season(program["id"], "Season", actor_id="admin")
        league_a = api.create_league(season["id"], "League A", actor_id="admin")
        league_b = api.create_league(season["id"], "League B", actor_id="admin")
        club = api.create_club("C", actor_id="admin")
        team = api.create_team(club["id"], None, "Team", actor_id="admin",
                               league_id=league_a["id"])
        reg = api.register_team_for_season(season["id"], team["id"],
                                           actor_id="admin",
                                           league_id=league_a["id"])
        self.assertNotIn("error", reg, reg)
        api.create_player(team["id"], "Vince Skater", "forward", actor_id="admin")
        venue = api.create_venue("V", league_id=program["id"], actor_id="admin")
        rink = api.create_rink(venue["id"], "R", actor_id="admin")
        api.create_ice_slot(rink["id"], "2026-09-01T18:30:00+00:00",
                            "2026-09-01T20:00:00+00:00", actor_id="admin")
        api.grant_season_venue_access(season["id"], venue["id"], actor_id="admin")

        ls_b = api.store.league_season_for(league_b["id"], season["id"])
        api.store.add_season_team_registration(SeasonTeamRegistration(
            id=api.store.next_id("streg"), league_season_id=ls_b.id,
            team_id=team["id"], division_id=None, active=False))

        api.set_active_context("admin", Role.LEAGUE_ADMIN, {},
                               program["id"], season["id"])
        progress = api.get_setup_progress("admin", *ADMIN)
        self.assertEqual(_statuses(progress)["participation"], "done", progress)
        self.assertTrue(progress["complete"], progress)
        participation = next(w for w in progress["workflows"]
                             if w["key"] == "participation")
        self.assertNotIn("attention", participation, participation)

    def test_facilities_next_is_also_blocked_when_selected_season_is_archived(self):
        """Not just "participation" -- ``commit_ice_availability`` (the Ice
        Availability Builder's real write behind "facilities") itself
        "requires an active Season (#159)" and fails `season_archived` the
        same way `register_team_for_season` does. Arena Manager's only
        permitted workflow must be recognized as blocked here too, not just
        on the no-Season case."""
        api = self._api()
        api.create_user_account("admin", "pw", "league_admin")
        program = api.create_program("Prog", actor_id="admin")
        season = api.create_season(program["id"], "Fall", actor_id="admin")
        api.archive_season(season["id"], reason="test", actor_id="admin")
        api.set_active_context("admin", Role.LEAGUE_ADMIN, {},
                               program["id"], season["id"])

        arena_progress = api.get_setup_progress("admin", *ARENA)
        self.assertEqual(_statuses(arena_progress)["facilities"], "todo")
        self.assertIsNone(arena_progress["next"], arena_progress)
        self.assertEqual(arena_progress["next_blocked"]["key"], "facilities")
        self.assertEqual(arena_progress["next_blocked"]["reason"], "season_archived")
        self.assertIn("Fall", arena_progress["next_blocked"]["detail"])

    def test_next_stays_blocked_even_when_a_later_workflow_is_safe(self):
        """#331 review round 4: `next` must never skip AHEAD of a blocked
        workflow to a later, incidentally-safe one -- #330's "actual next
        incomplete step" is a strictly ordered contract (round 3's original
        fix got this wrong: it scanned past a blocked candidate for a safe
        later one, which silently reordered the sequence and could read as
        the blocked step being skipped/forgotten rather than blocked). With
        the selected Season archived (blocking participation, which comes
        before roster in the fixed #204 order) but roster itself still
        genuinely open and prerequisite-free, League Admin must still see
        `next: None` / `next_blocked: participation` -- NOT get routed
        ahead to roster."""
        api = self._api()
        api.create_user_account("admin", "pw", "league_admin")
        program = api.create_program("Prog", actor_id="admin")
        season = api.create_season(program["id"], "Fall", actor_id="admin")
        api.create_league(season["id"], "Adult League", actor_id="admin")
        club = api.create_club("C", actor_id="admin")
        api.create_team(club["id"], None, "T", actor_id="admin",
                        program_id=program["id"])
        # No player added -- roster stays todo and has no Season prerequisite
        # of its own, but must NOT be surfaced ahead of blocked participation.
        api.archive_season(season["id"], reason="test", actor_id="admin")
        api.set_active_context("admin", Role.LEAGUE_ADMIN, {},
                               program["id"], season["id"])

        progress = api.get_setup_progress("admin", *ADMIN)
        statuses = _statuses(progress)
        self.assertEqual(statuses["participation"], "todo")
        self.assertEqual(statuses["roster"], "todo")
        self.assertIsNone(progress["next"], progress)
        self.assertEqual(progress["next_blocked"]["key"], "participation",
                         f"expected next_blocked to name participation (the "
                         f"FIRST blocked todo workflow in order), not skip "
                         f"ahead to roster: {progress}")
        self.assertEqual(progress["next_blocked"]["reason"], "season_archived")

    # -- the facilities card's OWN venue-access prerequisite (#365 review) --
    # `next_blocked` above describes exactly ONE workflow: the first permitted
    # TODO one. These assert the ADDITIVE per-workflow contract that exists
    # because of that -- a Facilities card that derived its own prerequisite
    # from `next_blocked` failed OPEN for League Admin, whose permitted list
    # starts four workflows earlier.

    def _facilities_prereq(self, progress):
        row = next(w for w in progress["workflows"] if w["key"] == "facilities")
        prereqs = {p["key"]: p for p in row.get("prerequisites", [])}
        return prereqs.get("venue_access")

    def _revoked_grant_fixture(self, api):
        """A Program whose Venue+Rink are VISIBLE but not schedulable: the
        grant to the selected Season existed and was revoked. This is the
        reviewer's reproduction -- the scoped overview still reports the
        Venue and Rink (revoked history is deliberately in scope), so any
        check that asks only "is a Rink visible" answers yes."""
        api.create_user_account("admin", "pw", "league_admin")
        program = api.create_program("Prog", actor_id="admin")
        # Real Season boundaries: the Ice Builder cross-check below plans
        # against them, and every other assertion is indifferent to them.
        season = api.create_season(program["id"], "Fall",
                                   start_date="2026-09-01", end_date="2027-03-31",
                                   actor_id="admin")
        venue = api.create_venue("V", actor_id="admin")
        rink = api.create_rink(venue["id"], "R", actor_id="admin")
        api.set_active_context("admin", Role.LEAGUE_ADMIN, {},
                               program["id"], season["id"])
        access = api.grant_season_venue_access(season["id"], venue["id"],
                                               actor_id="admin")
        api.revoke_season_venue_access(access["id"], actor_id="admin")
        return program, season, venue, rink

    def test_facilities_prerequisite_is_unmet_for_a_revoked_grant(self):
        """A Venue and Rink whose grant to the SELECTED Season was revoked
        are still visible (correctly -- revoked history stays in scope so the
        cleanup section can name it) and are NOT schedulable. BOTH roles that
        can see the workflow receive the identical unmet fact: it is a
        statement about the Season's data, not about the caller, and the role
        that cannot grant access needs it just as much (it is precisely the
        role that would otherwise be handed a dead-end Add Ice)."""
        api = self._api()
        program, season, venue, rink = self._revoked_grant_fixture(api)

        # Non-vacuous: the rows really are visible, so this is not merely an
        # empty Program. Same read the client's own counts come from.
        overview = api.get_setup_overview_v2("admin", Role.LEAGUE_ADMIN, {})
        self.assertTrue([v for v in overview["venues"] if v["id"] == venue["id"]],
                        f"the revoked-grant Venue must stay visible: {overview['venues']}")
        self.assertTrue([r for r in overview["rinks"] if r["id"] == rink["id"]],
                        f"the revoked-grant Rink must stay visible: {overview['rinks']}")

        rows = {}
        for role, label in ((ADMIN, "league admin"), (ARENA, "arena manager")):
            progress = api.get_setup_progress("admin", *role)
            prereq = self._facilities_prereq(progress)
            self.assertIsNotNone(prereq, f"{label} received no venue_access "
                                         f"prerequisite at all: {progress}")
            self.assertFalse(prereq["met"], f"{label}: {prereq}")
            self.assertEqual(prereq["reason"], "venue_access_missing", label)
            self.assertIn(season["name"], prereq["detail"], label)
            rows[label] = prereq
        self.assertEqual(rows["league admin"], rows["arena manager"],
                         f"the fact is about the Season's data, not the caller, "
                         f"so both roles must receive an identical row: {rows}")

    def test_facilities_prerequisite_is_unmet_for_an_ungranted_venue(self):
        """The second reproduction: a Venue+Rink that never had a grant at
        all. Same answer as the revoked case -- what matters is ACTIVE
        access for this Season, not the history that produced its absence."""
        api = self._api()
        api.create_user_account("admin", "pw", "league_admin")
        program = api.create_program("Prog", actor_id="admin")
        season = api.create_season(program["id"], "Fall", actor_id="admin")
        venue = api.create_venue("V", actor_id="admin")
        api.create_rink(venue["id"], "R", actor_id="admin")
        api.set_active_context("admin", Role.LEAGUE_ADMIN, {},
                               program["id"], season["id"])

        for role, label in ((ADMIN, "league admin"), (ARENA, "arena manager")):
            prereq = self._facilities_prereq(api.get_setup_progress("admin", *role))
            self.assertFalse(prereq["met"], f"{label}: {prereq}")
            self.assertEqual(prereq["reason"], "venue_access_missing", label)

    def test_facilities_prerequisite_is_met_once_access_is_granted(self):
        """The converse, so the assertions above cannot pass by reporting
        every Facilities card blocked forever. Re-granting the revoked
        Venue -- the real recovery path -- must flip the same fact."""
        api = self._api()
        program, season, venue, _rink = self._revoked_grant_fixture(api)
        api.grant_season_venue_access(season["id"], venue["id"], actor_id="admin")

        for role, label in ((ADMIN, "league admin"), (ARENA, "arena manager")):
            prereq = self._facilities_prereq(api.get_setup_progress("admin", *role))
            self.assertTrue(prereq["met"], f"{label}: {prereq}")
            self.assertIsNone(prereq.get("reason"), f"{label}: {prereq}")
            self.assertIsNone(prereq.get("detail"), f"{label}: {prereq}")

    def test_facilities_prerequisite_is_reported_when_next_blocked_names_nothing(self):
        """THE fail-open this contract exists for. For a League Admin the
        first permitted TODO workflow is `league_season`/`teams`/… long
        before facilities, so `next`/`next_blocked` say nothing about
        facilities at all -- here `next` is a perfectly safe EARLIER
        workflow. The facilities row must STILL carry its own unmet
        prerequisite, or the card has no scoped fact to bind and falls back
        to "a Rink is visible", which is exactly the dead-end Add Ice."""
        api = self._api()
        program, season, venue, _rink = self._revoked_grant_fixture(api)
        # Leave `teams` genuinely todo, so `next` is an earlier, unblocked
        # workflow and `next_blocked` is None.
        progress = api.get_setup_progress("admin", *ADMIN)
        self.assertIsNotNone(progress["next"], progress)
        self.assertNotEqual(progress["next"]["key"], "facilities", progress)
        self.assertIsNone(progress["next_blocked"], progress)

        prereq = self._facilities_prereq(progress)
        self.assertFalse(
            prereq["met"],
            f"facilities must assert its own venue-access gap even when "
            f"`next`/`next_blocked` are about another workflow: {progress}")
        self.assertEqual(prereq["reason"], "venue_access_missing")

    def test_facilities_prerequisite_is_scoped_to_the_selected_season(self):
        """Season-bound exactly as the workflow's own done/todo check is: a
        grant held by ANOTHER Season of the same Program never satisfies the
        selected one. Otherwise the card would advance on a neighbouring
        tuple's inventory -- the thing per-card identity exists to prevent."""
        api = self._api()
        api.create_user_account("admin", "pw", "league_admin")
        program = api.create_program("Prog", actor_id="admin")
        granted_season = api.create_season(program["id"], "Fall", actor_id="admin")
        other_season = api.create_season(program["id"], "Spring", actor_id="admin")
        venue = api.create_venue("V", actor_id="admin")
        api.create_rink(venue["id"], "R", actor_id="admin")
        api.set_active_context("admin", Role.LEAGUE_ADMIN, {},
                               program["id"], granted_season["id"])
        api.grant_season_venue_access(granted_season["id"], venue["id"],
                                      actor_id="admin")

        met = self._facilities_prereq(api.get_setup_progress("admin", *ADMIN))
        self.assertTrue(met["met"], met)

        api.set_active_context("admin", Role.LEAGUE_ADMIN, {},
                               program["id"], other_season["id"])
        unmet = self._facilities_prereq(api.get_setup_progress("admin", *ADMIN))
        self.assertFalse(unmet["met"], unmet)
        self.assertIn(other_season["name"], unmet["detail"], unmet)

    def test_facilities_prerequisite_matches_the_ice_builder_refusal(self):
        """The prerequisite and the real write must never disagree. With the
        grant revoked, the Ice Availability Builder's own preview -- the
        exact destination "Add Ice" opens -- refuses the Rink with
        `venue_access_missing` and generates zero slots. That is what makes
        the withdrawn CTA a correction rather than a style choice."""
        api = self._api()
        program, season, _venue, rink = self._revoked_grant_fixture(api)
        prereq = self._facilities_prereq(api.get_setup_progress("admin", *ADMIN))
        self.assertFalse(prereq["met"], prereq)

        preview = api.preview_ice_availability(
            season_id=season["id"], rink_ids=[rink["id"]], weekdays=[1],
            start_local="18:00", end_local="19:00",
            start_date="2026-09-01", end_date="2026-09-07",
            playable_minutes=60, turnover_minutes=0, actor_id="admin")
        self.assertNotIn("error", preview, preview)
        self.assertEqual(preview["slots"], [],
                         f"a revoked grant must yield zero usable slots: {preview}")
        self.assertEqual([m["rink_id"] for m in preview["venue_access_missing"]],
                         [rink["id"]], preview)


    # -- the COMPLETE ordered hard-prerequisite set (#365 review round 3) ----
    # Round 2 published ONE fact (venue_access) and called it the workflow's
    # capability. These assert the whole set, in order, and that it is the
    # SAME computation `_workflow_prerequisite_gap` refuses with -- so the
    # server can never refuse a workflow for a reason its own card never
    # received.

    @staticmethod
    def _prereqs(progress, key):
        """The ORDERED prerequisite rows for `key`, as published."""
        row = next(w for w in progress["workflows"] if w["key"] == key)
        return row.get("prerequisites", [])

    @staticmethod
    def _prereq_keys(progress, key):
        return [p["key"] for p in
                SetupProgressComputationTest._prereqs(progress, key)]

    @staticmethod
    def _prereq(progress, key, prereq_key):
        for p in SetupProgressComputationTest._prereqs(progress, key):
            if p["key"] == prereq_key:
                return p
        return None

    def _archived_season_with_live_grant(self, api):
        """THE round-3 reproduction, verbatim from the review: Program +
        active Season + Venue + Rink, grant the Venue to that Season, then
        ARCHIVE the selected Season.

        Deliberately NON-VACUOUS in two independent ways, because both are
        how round 2's fix could have appeared to cover this:
        * the Venue and Rink are really VISIBLE (counts non-zero), so the
          card is not merely empty; and
        * the grant is really ACTIVE, so `venue_access` asserts met -- the
          card is not already blocked by round 2's own floor.
        What is left is exactly the new hole: a read-only Season."""
        api.create_user_account("admin", "pw", "league_admin")
        program = api.create_program("Prog", actor_id="admin")
        season = api.create_season(program["id"], "Fall",
                                   start_date="2026-09-01", end_date="2027-03-31",
                                   actor_id="admin")
        venue = api.create_venue("V", actor_id="admin")
        rink = api.create_rink(venue["id"], "R", actor_id="admin")
        api.set_active_context("admin", Role.LEAGUE_ADMIN, {},
                               program["id"], season["id"])
        api.grant_season_venue_access(season["id"], venue["id"], actor_id="admin")
        api.archive_season(season["id"], reason="round 3", actor_id="admin")
        return program, season, venue, rink

    def test_facilities_publishes_every_floor_for_an_archived_season(self):
        """THE round-3 fail-open. An archived selected Season with a LIVE
        grant used to publish `[{venue_access, met: true}]` and nothing
        else, so the card settled with no blocker and a dead-end "Add Ice"
        while `commit_ice_availability` would refuse the same workflow with
        `season_archived`. The complete ordered set has to arrive."""
        api = self._api()
        _program, season, venue, rink = self._archived_season_with_live_grant(api)
        progress = api.get_setup_progress("admin", *ADMIN)

        # Non-vacuity 1: the rows really are visible, so this is a card with
        # real inventory rather than an empty one that offers nothing anyway.
        overview = api.get_setup_overview_v2("admin", Role.LEAGUE_ADMIN, {})
        self.assertTrue([v for v in overview["venues"] if v["id"] == venue["id"]],
                        overview["venues"])
        self.assertTrue([r for r in overview["rinks"] if r["id"] == rink["id"]],
                        overview["rinks"])

        self.assertEqual(
            self._prereq_keys(progress, "facilities"),
            ["season_selected", "season_active", "venue_access"],
            f"facilities must publish the COMPLETE set, in the order the real "
            f"writes fail it: {progress}")
        self.assertTrue(self._prereq(progress, "facilities", "season_selected")["met"])
        # Non-vacuity 2: the round-2 floor is genuinely MET here, so nothing
        # below can be passing because of it.
        self.assertTrue(
            self._prereq(progress, "facilities", "venue_access")["met"],
            f"the grant is active, so venue_access must assert met -- otherwise "
            f"this fixture is testing round 2's hole again: {progress}")

        archived = self._prereq(progress, "facilities", "season_active")
        self.assertFalse(archived["met"], archived)
        self.assertEqual(archived["reason"], "season_archived", archived)
        self.assertIn(season["name"], archived["detail"], archived)

        # ...and the hole this closes: `next_blocked` cannot report it,
        # because an EARLIER workflow owns that single slot (or nothing does).
        self.assertNotEqual((progress["next_blocked"] or {}).get("key"),
                            "facilities", progress)

    def test_participation_publishes_every_floor_for_an_archived_season(self):
        """The same audit for participation: an archived selected Season and
        an otherwise perfectly valid League/Division/Team. Its own capability
        floor (team_league_eligible) is MET, so the archived Season is the
        only thing left -- and it was absent from the published set."""
        api = self._api()
        api.create_user_account("admin", "pw", "league_admin")
        program = api.create_program("Prog", actor_id="admin")
        season = api.create_season(program["id"], "Fall", actor_id="admin")
        league = api.create_league(season["id"], "AL", actor_id="admin")
        club = api.create_club("C", actor_id="admin")
        api.set_active_context("admin", Role.LEAGUE_ADMIN, {},
                               program["id"], season["id"])
        api.create_team(club["id"], None, "T", actor_id="admin",
                        program_id=program["id"], league_id=league["id"])
        api.create_division(season["id"], league["id"], "D", actor_id="admin")
        api.archive_season(season["id"], reason="round 3", actor_id="admin")

        progress = api.get_setup_progress("admin", *ADMIN)
        self.assertEqual(
            self._prereq_keys(progress, "participation"),
            ["season_selected", "season_active", "team_league_eligible"],
            progress)
        self.assertTrue(
            self._prereq(progress, "participation", "team_league_eligible")["met"],
            f"the Team's permanent League really is one this Season runs, so "
            f"the eligibility floor must assert met -- otherwise the archived "
            f"assertion below could pass for the wrong reason: {progress}")
        archived = self._prereq(progress, "participation", "season_active")
        self.assertFalse(archived["met"], archived)
        self.assertEqual(archived["reason"], "season_archived", archived)
        self.assertIn(season["name"], archived["detail"], archived)

    def test_participation_publishes_its_team_league_eligibility_floor(self):
        """The second participation floor, published for the first time in
        round 3: an ACTIVE Season whose only Team is permanently bound to
        another League. Every registration here is a guaranteed rule-7
        rejection, and the card had no way to know."""
        api = self._api()
        api.create_user_account("admin", "pw", "league_admin")
        program = api.create_program("Prog", actor_id="admin")
        season_a = api.create_season(program["id"], "Season A", actor_id="admin")
        league_a = api.create_league(season_a["id"], "League A", actor_id="admin")
        club = api.create_club("C", actor_id="admin")
        api.set_active_context("admin", Role.LEAGUE_ADMIN, {},
                               program["id"], season_a["id"])
        api.create_team(club["id"], None, "T", actor_id="admin",
                        program_id=program["id"], league_id=league_a["id"])
        season_b = api.create_season(program["id"], "Season B", actor_id="admin")
        api.create_league(season_b["id"], "League B", actor_id="admin")
        api.set_active_context("admin", Role.LEAGUE_ADMIN, {},
                               program["id"], season_b["id"])

        progress = api.get_setup_progress("admin", *ADMIN)
        rows = self._prereqs(progress, "participation")
        self.assertEqual([p["key"] for p in rows],
                         ["season_selected", "season_active",
                          "team_league_eligible"], rows)
        # The Season floors are MET -- it is active and selected -- so the
        # eligibility row is the only thing this asserts.
        self.assertTrue(rows[0]["met"], rows)
        self.assertTrue(rows[1]["met"], rows)
        self.assertFalse(rows[2]["met"], rows)
        self.assertEqual(rows[2]["reason"], "team_league_mismatch", rows)
        self.assertIn(season_b["name"], rows[2]["detail"], rows)

    def test_no_selected_season_publishes_both_season_rows_unmet(self):
        """A Program-only context. The set stays COMPLETE rather than being
        truncated at the first failure: a fail-closed client treats a missing
        row as unmet anyway, but it would then explain it in the client's own
        words instead of the server's."""
        api = self._api()
        api.create_user_account("admin", "pw", "league_admin")
        program = api.create_program("Prog", actor_id="admin")
        api.create_season(program["id"], "Fall", actor_id="admin")
        api.set_active_context("admin", Role.LEAGUE_ADMIN, {}, program["id"], None)

        progress = api.get_setup_progress("admin", *ADMIN)
        for key, last in (("facilities", "venue_access"),
                          ("participation", "team_league_eligible")):
            self.assertEqual(self._prereq_keys(progress, key),
                             ["season_selected", "season_active", last], progress)
            for prereq_key in ("season_selected", "season_active"):
                row = self._prereq(progress, key, prereq_key)
                self.assertFalse(row["met"], f"{key}/{prereq_key}: {row}")
                self.assertEqual(row["reason"], "season_missing",
                                 f"{key}/{prereq_key}: {row}")
                self.assertTrue(row["detail"], row)

    def test_published_rows_and_the_refusal_are_one_computation(self):
        """The structural claim: `_workflow_prerequisite_gap` -- the server's
        own authority on why a workflow is refused -- is a PROJECTION of the
        published rows (its first unmet one), never an independent second
        opinion. Asserted across every combination of the three inputs, so a
        future floor added to one and not the other fails here."""
        from hockey_scheduler.domain import SeasonStatus

        class _S:
            def __init__(self, status):
                self.name = "Fall"
                self.status = status

        service_cls = self._api().__class__
        seasons = [None, _S(SeasonStatus.ACTIVE), _S(SeasonStatus.ARCHIVED)]
        for key in ("facilities", "participation"):
            for season in seasons:
                for rinks in (set(), {"rink_1"}):
                    for eligible in (False, True):
                        rows = service_cls._workflow_prerequisites(
                            key, season, rinks, eligible)
                        gap = service_cls._workflow_prerequisite_gap(
                            key, season, rinks, eligible)
                        unmet = [r for r in rows if not r["met"]]
                        case = (f"{key} season={season and season.status} "
                                f"rinks={bool(rinks)} eligible={eligible}")
                        if not unmet:
                            self.assertIsNone(gap, f"{case}: {rows} -> {gap}")
                        else:
                            self.assertIsNotNone(gap, f"{case}: {rows}")
                            self.assertEqual(gap[0], unmet[0]["reason"],
                                             f"{case}: {rows} -> {gap}")
                            self.assertTrue(gap[1], f"{case}: {gap}")

    def test_only_the_two_audited_workflows_publish_prerequisites(self):
        """The audit itself, as an assertion. `_workflow_prerequisite_gap`
        refuses ONLY facilities and participation, and the four other
        workflows' real writes (create_season/create_team/create_player/the
        import commits) take no Season guard -- so their absent
        `prerequisites` key is a positive statement, not an omission. If a
        floor is ever added to one of them this fails until it is published
        too."""
        api = self._api()
        _program, season, _venue, _rink = self._archived_season_with_live_grant(api)
        progress = api.get_setup_progress("admin", *ADMIN)
        with_floors, without = [], []
        for w in progress["workflows"]:
            (with_floors if "prerequisites" in w else without).append(w["key"])
        self.assertEqual(with_floors, ["participation", "facilities"], progress)
        self.assertEqual(sorted(without),
                         ["import", "league_season", "roster", "teams"], progress)

        service_cls = api.__class__
        for key in without:
            self.assertIsNone(
                service_cls._workflow_prerequisite_gap(key, season, set(), False),
                f"{key} publishes no prerequisites, so the server must not be "
                f"able to refuse it either")
            self.assertEqual(
                service_cls._workflow_prerequisites(key, season, set(), False), [])

    def test_the_new_facilities_rows_are_role_invariant(self):
        """Same discipline round 2 established for venue_access, extended to
        the whole set: these are statements about the selected Season's data,
        not about the caller, so both roles that can see the workflow receive
        byte-identical rows. WHO may reopen the Season is a permission
        question the caller's own permission set already answers."""
        api = self._api()
        self._archived_season_with_live_grant(api)
        rows = {label: self._prereqs(api.get_setup_progress("admin", *role),
                                     "facilities")
                for role, label in ((ADMIN, "league admin"), (ARENA, "arena"))}
        self.assertEqual(rows["league admin"], rows["arena"], rows)

    def test_reopening_the_season_clears_only_the_season_floor(self):
        """The converse, so the assertions above cannot pass by reporting
        every archived card blocked forever: the REAL reopen -- the same
        write /api/v2/setup/seasons/<id>/reopen performs -- flips
        season_active and leaves the other rows exactly as they were."""
        api = self._api()
        _program, season, _venue, _rink = self._archived_season_with_live_grant(api)
        before = self._prereqs(api.get_setup_progress("admin", *ADMIN), "facilities")
        api.reopen_season(season["id"], reason="back to work", actor_id="admin")
        after = self._prereqs(api.get_setup_progress("admin", *ADMIN), "facilities")

        self.assertEqual([p["key"] for p in after],
                         [p["key"] for p in before], after)
        self.assertTrue(all(p["met"] for p in after),
                        f"every facilities floor must be met once the Season is "
                        f"reopened (the grant was never revoked): {after}")
        progress = api.get_setup_progress("admin", *ARENA)
        self.assertEqual(progress["next"]["key"], "facilities", progress)
        self.assertIsNone(progress["next_blocked"], progress)


class SetupProgressHttpTest(unittest.TestCase):
    """Route/authz contract over real HTTP — mirrors test_v2_setup_contract.
    py's harness. The demo seed (STATE.reset()) provisions the standard
    admin/arena/coach accounts this reuses."""

    @classmethod
    def setUpClass(cls):
        srv = __import__("hockey_scheduler.web.server", fromlist=["x"])
        cls.srv = srv
        srv.STATE.reset()
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), srv.Handler)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.thread.join(timeout=5)
        cls.httpd.server_close()

    def _client(self):
        return urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(CookieJar()))

    def _req(self, opener, method, path, body=None):
        url = f"http://127.0.0.1:{self.port}{path}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with opener.open(req) as r:
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def _login(self, username):
        c = self._client()
        self._req(c, "POST", "/api/auth/login",
                  {"username": username, "password": "demo"})
        return c

    def test_requires_signed_in_session(self):
        c = self._client()
        status, _ = self._req(c, "GET", "/api/v2/setup/progress")
        self.assertIn(status, (401, 403))

    def test_denies_role_without_manage_arena(self):
        c = self._login("coach")
        status, _ = self._req(c, "GET", "/api/v2/setup/progress")
        self.assertEqual(status, 403)

    def test_allows_league_admin(self):
        c = self._login("admin")
        status, resp = self._req(c, "GET", "/api/v2/setup/progress")
        self.assertEqual(status, 200, resp)
        self.assertIn("workflows", resp)
        self.assertIn("program_id", resp)

    def test_allows_arena_manager(self):
        # #330: the Home/Tasks hub is also the Arena Manager's landing, so
        # this must be MANAGE_ARENA like /api/v2/setup/overview, not the
        # League-Admin-only MANAGE_SETUP /api/v2/onboarding/status uses.
        c = self._login("arena")
        status, resp = self._req(c, "GET", "/api/v2/setup/progress")
        self.assertEqual(status, 200, resp)
        self.assertIn("workflows", resp)

    def test_wrong_method_is_405_not_500(self):
        c = self._login("admin")
        status, resp = self._req(c, "POST", "/api/v2/setup/progress", {})
        self.assertEqual(status, 405, resp)

    def test_next_action_is_role_aware_over_real_http(self):
        """#330 review round 1 finding 1, over the real route: League Admin
        and Arena Manager viewing the SAME Program (with a Season already
        selected AND venue access already granted, so facilities is
        genuinely executable — see
        test_no_season_blocks_facilities_over_real_http and
        test_no_venue_access_blocks_facilities_over_real_http for those two
        blocked cases) must get DIFFERENT primary actions — League Admin
        the normal ordering, Arena Manager an executable one
        (facilities/Add Ice), never a MANAGE_SETUP-only action they cannot
        perform. Coach stays denied entirely (unchanged). #331 review round
        3 finding 1's redaction half: Arena Manager's `workflows` must
        carry only "facilities", never League-Admin-only completion
        detail."""
        admin = self._login("admin")
        status, program = self._req(admin, "POST", "/api/v2/setup/program",
                                    {"name": "Round1F1 HTTP Prog", "country": "US"})
        self.assertEqual(status, 200, program)
        status, season = self._req(admin, "POST", "/api/v2/setup/season",
                                   {"program_id": program["id"], "name": "Fall"})
        self.assertEqual(status, 200, season)
        # Select the new context BEFORE the venue-access grant instead of after
        # (#369): granting access names two EXISTING records — the Season and
        # the Venue — and is authorized against the caller's active Program, so
        # while admin is still in whichever Program resolved first the grant is
        # correctly refused. The selection was always part of this test; it just
        # has to happen before the first mutation that depends on it.
        status, _ = self._req(admin, "POST", "/api/context",
                              {"program_id": program["id"], "season_id": season["id"]})
        self.assertEqual(status, 200)
        status, venue = self._req(admin, "POST", "/api/v2/setup/venue",
                                  {"name": "V"})
        self.assertEqual(status, 200, venue)
        status, _ = self._req(admin, "POST", "/api/v2/setup/rink",
                              {"venue_id": venue["id"], "name": "R"})
        self.assertEqual(status, 200)
        status, _ = self._req(
            admin, "POST", f"/api/v2/setup/seasons/{season['id']}/venue-access",
            {"venue_id": venue["id"]})
        self.assertEqual(status, 200)

        status, admin_progress = self._req(admin, "GET", "/api/v2/setup/progress")
        self.assertEqual(status, 200, admin_progress)
        self.assertEqual(admin_progress["next"]["key"], "league_season")
        self.assertEqual([w["key"] for w in admin_progress["workflows"]],
                         _WORKFLOW_KEYS)

        arena = self._login("arena")
        status, _ = self._req(arena, "POST", "/api/context",
                              {"program_id": program["id"], "season_id": season["id"]})
        self.assertEqual(status, 200,
                         "Arena Manager must be able to select the same Program")
        status, arena_progress = self._req(arena, "GET", "/api/v2/setup/progress")
        self.assertEqual(status, 200, arena_progress)
        self.assertEqual(
            arena_progress["next"]["key"], "facilities",
            f"Arena Manager must get an executable action, not the League "
            f"Admin-only one: {arena_progress}")
        self.assertEqual(arena_progress["next"]["primary_action"], "Add Ice")
        self.assertEqual([w["key"] for w in arena_progress["workflows"]],
                         ["facilities"],
                         "Arena Manager must never receive League-Admin-only "
                         f"workflow detail over HTTP either: {arena_progress}")

        coach = self._login("coach")
        status, _ = self._req(coach, "GET", "/api/v2/setup/progress")
        self.assertEqual(status, 403)

    def test_no_season_blocks_facilities_over_real_http(self):
        """#331 review round 3 finding 1, over the real route: on a fresh
        Program with no Season yet, Arena Manager's only permitted workflow
        (facilities) is not safe to execute — the real Ice Builder write
        would fail season_missing (league_scope.assign_game_ice) — so `next`
        must be None with `next_blocked` explaining why, never the dead-end
        "Add Ice" CTA the pre-round-3 endpoint handed out here."""
        admin = self._login("admin")
        status, program = self._req(admin, "POST", "/api/v2/setup/program",
                                    {"name": "Round3F1 HTTP Prog", "country": "US"})
        self.assertEqual(status, 200, program)
        status, _ = self._req(admin, "POST", "/api/context",
                              {"program_id": program["id"], "season_id": None})
        self.assertEqual(status, 200)

        arena = self._login("arena")
        status, _ = self._req(arena, "POST", "/api/context",
                              {"program_id": program["id"], "season_id": None})
        self.assertEqual(status, 200)
        status, arena_progress = self._req(arena, "GET", "/api/v2/setup/progress")
        self.assertEqual(status, 200, arena_progress)
        self.assertIsNone(arena_progress["next"], arena_progress)
        self.assertEqual(arena_progress["next_blocked"]["key"], "facilities")
        self.assertEqual(arena_progress["next_blocked"]["reason"], "season_missing")

    def test_no_season_blocks_participation_over_real_http(self):
        """#331 review round 4, over the real route: League Admin with
        league_season/teams already done but no Season selected must not
        receive an enabled Register Team CTA -- the real destination
        (focusParticipationRegisterControl) cannot bind/focus an exact
        control without a resolved Season, the same class of dead end as
        facilities' season_missing."""
        admin = self._login("admin")
        status, program = self._req(admin, "POST", "/api/v2/setup/program",
                                    {"name": "Round4F1 HTTP Prog", "country": "US"})
        self.assertEqual(status, 200, program)
        status, season = self._req(admin, "POST", "/api/v2/setup/season",
                                   {"program_id": program["id"], "name": "Fall"})
        self.assertEqual(status, 200, season)
        status, league = self._req(admin, "POST", "/api/v2/setup/league",
                                   {"season_id": season["id"], "name": "Adult League"})
        self.assertEqual(status, 200, league)
        status, club = self._req(admin, "POST", "/api/v2/setup/club", {"name": "Club"})
        self.assertEqual(status, 200, club)
        # #367 prerequisite: a Team's League must belong to the ACTIVE Program,
        # so move there before populating it. The deliberate Program-ONLY
        # context (season_id: None) this test actually exercises is still set
        # below -- that is the state under test, not this setup step.
        status, _ = self._req(admin, "POST", "/api/context",
                              {"program_id": program["id"],
                               "season_id": season["id"]})
        self.assertEqual(status, 200)
        status, _ = self._req(admin, "POST", "/api/v2/setup/team",
                              {"club_id": club["id"], "league_id": league["id"], "name": "Team"})
        self.assertEqual(status, 200)
        status, _ = self._req(admin, "POST", "/api/context",
                              {"program_id": program["id"], "season_id": None})
        self.assertEqual(status, 200)

        status, progress = self._req(admin, "GET", "/api/v2/setup/progress")
        self.assertEqual(status, 200, progress)
        self.assertIsNone(progress["next"], progress)
        self.assertEqual(progress["next_blocked"]["key"], "participation")
        self.assertEqual(progress["next_blocked"]["reason"], "season_missing")

    def test_no_venue_access_blocks_facilities_over_real_http(self):
        """#331 review round 5 finding 1, over the real route: with an
        active Season and a Rink but NO granted venue access, Arena
        Manager's only permitted workflow (facilities) is still not safe --
        the real Ice Builder preview would yield zero slots
        (venue_access_missing), and Arena Manager cannot grant access
        themselves. `next` must stay None with `next_blocked` naming
        facilities/venue_access_missing, never the dead-end "Add Ice" CTA;
        granting access as League Admin must genuinely unblock Arena
        Manager's view."""
        admin = self._login("admin")
        status, program = self._req(admin, "POST", "/api/v2/setup/program",
                                    {"name": "Round5F1 HTTP Prog", "country": "US"})
        self.assertEqual(status, 200, program)
        status, season = self._req(admin, "POST", "/api/v2/setup/season",
                                   {"program_id": program["id"], "name": "Fall"})
        self.assertEqual(status, 200, season)
        status, venue = self._req(admin, "POST", "/api/v2/setup/venue", {"name": "V"})
        self.assertEqual(status, 200, venue)
        status, _ = self._req(admin, "POST", "/api/v2/setup/rink",
                              {"venue_id": venue["id"], "name": "R"})
        self.assertEqual(status, 200)
        status, _ = self._req(admin, "POST", "/api/context",
                              {"program_id": program["id"], "season_id": season["id"]})
        self.assertEqual(status, 200)

        arena = self._login("arena")
        status, _ = self._req(arena, "POST", "/api/context",
                              {"program_id": program["id"], "season_id": season["id"]})
        self.assertEqual(status, 200)
        status, arena_progress = self._req(arena, "GET", "/api/v2/setup/progress")
        self.assertEqual(status, 200, arena_progress)
        self.assertIsNone(arena_progress["next"], arena_progress)
        self.assertEqual(arena_progress["next_blocked"]["key"], "facilities")
        self.assertEqual(arena_progress["next_blocked"]["reason"],
                         "venue_access_missing")

        status, _ = self._req(
            admin, "POST", f"/api/v2/setup/seasons/{season['id']}/venue-access",
            {"venue_id": venue["id"]})
        self.assertEqual(status, 200)
        status, advanced = self._req(arena, "GET", "/api/v2/setup/progress")
        self.assertEqual(status, 200, advanced)
        self.assertEqual(advanced["next"]["key"], "facilities", advanced)
        self.assertIsNone(advanced["next_blocked"])

    def test_team_league_mismatch_blocks_participation_over_real_http(self):
        """#331 review round 5 finding 2, over the real route: a Team
        permanently bound to League A cannot register into a Season whose
        only League is B -- register_team_for_season's own rule 7 rejects
        with team_league_mismatch regardless of which team the operator
        picks. `next` must stay None with `next_blocked` naming
        participation/team_league_mismatch, never a dead-end "Register
        Team" CTA; adding an eligible team must genuinely unblock it."""
        admin = self._login("admin")
        status, program = self._req(admin, "POST", "/api/v2/setup/program",
                                    {"name": "Round5F2 HTTP Prog", "country": "US"})
        self.assertEqual(status, 200, program)
        status, season_a = self._req(admin, "POST", "/api/v2/setup/season",
                                     {"program_id": program["id"], "name": "Season A"})
        self.assertEqual(status, 200, season_a)
        status, league_a = self._req(admin, "POST", "/api/v2/setup/league",
                                     {"season_id": season_a["id"], "name": "League A"})
        self.assertEqual(status, 200, league_a)
        status, club = self._req(admin, "POST", "/api/v2/setup/club", {"name": "Club"})
        self.assertEqual(status, 200, club)
        # #367 prerequisite: move to the Program being built before populating
        # it -- a Team's League must belong to the ACTIVE Program, and this
        # class shares one store across test methods.
        status, _ = self._req(admin, "POST", "/api/context",
                              {"program_id": program["id"],
                               "season_id": season_a["id"]})
        self.assertEqual(status, 200)
        status, team = self._req(admin, "POST", "/api/v2/setup/team",
                                 {"club_id": club["id"], "league_id": league_a["id"],
                                  "name": "Team"})
        self.assertEqual(status, 200, team)
        status, _ = self._req(
            admin, "POST",
            f"/api/v2/setup/seasons/{season_a['id']}/team-registrations",
            {"team_id": team["id"], "league_id": league_a["id"]})
        self.assertEqual(status, 200)

        status, season_b = self._req(admin, "POST", "/api/v2/setup/season",
                                     {"program_id": program["id"], "name": "Season B"})
        self.assertEqual(status, 200, season_b)
        status, league_b = self._req(admin, "POST", "/api/v2/setup/league",
                                     {"season_id": season_b["id"], "name": "League B"})
        self.assertEqual(status, 200, league_b)
        status, _ = self._req(admin, "POST", "/api/context",
                              {"program_id": program["id"], "season_id": season_b["id"]})
        self.assertEqual(status, 200)

        status, progress = self._req(admin, "GET", "/api/v2/setup/progress")
        self.assertEqual(status, 200, progress)
        self.assertIsNone(progress["next"], progress)
        self.assertEqual(progress["next_blocked"]["key"], "participation")
        self.assertEqual(progress["next_blocked"]["reason"], "team_league_mismatch")

        status, team2 = self._req(admin, "POST", "/api/v2/setup/team",
                                  {"club_id": club["id"], "league_id": league_b["id"],
                                   "name": "Team2"})
        self.assertEqual(status, 200, team2)
        status, advanced = self._req(admin, "GET", "/api/v2/setup/progress")
        self.assertEqual(status, 200, advanced)
        self.assertEqual(advanced["next"]["key"], "participation", advanced)
        self.assertIsNone(advanced["next_blocked"])

    @staticmethod
    def _prereqs(progress, key):
        row = next(w for w in progress["workflows"] if w["key"] == key)
        return row.get("prerequisites", [])

    def test_archived_season_publishes_every_facilities_floor_over_real_http(self):
        """The round-3 reproduction over the REAL authenticated route, for
        BOTH roles that can see the workflow. The grant stays ACTIVE and the
        Venue/Rink stay visible, so `venue_access` asserts met and the only
        remaining floor is the read-only Season -- which is exactly the state
        that used to publish a single met row and a dead-end "Add Ice".

        For League Admin `next_blocked` names an EARLIER workflow (or
        nothing), which is why the per-workflow rows have to carry the fact:
        the roll-up's one slot is already spoken for."""
        admin = self._login("admin")
        status, program = self._req(admin, "POST", "/api/v2/setup/program",
                                    {"name": "R3 Archived HTTP Prog", "country": "US"})
        self.assertEqual(status, 200, program)
        status, season = self._req(admin, "POST", "/api/v2/setup/season",
                                   {"program_id": program["id"], "name": "R3 Fall"})
        self.assertEqual(status, 200, season)
        status, venue = self._req(admin, "POST", "/api/v2/setup/venue", {"name": "R3 V"})
        self.assertEqual(status, 200, venue)
        status, rink = self._req(admin, "POST", "/api/v2/setup/rink",
                                 {"venue_id": venue["id"], "name": "R3 R"})
        self.assertEqual(status, 200, rink)
        status, _ = self._req(admin, "POST", "/api/context",
                              {"program_id": program["id"], "season_id": season["id"]})
        self.assertEqual(status, 200)
        status, grant = self._req(
            admin, "POST", f"/api/v2/setup/seasons/{season['id']}/venue-access",
            {"venue_id": venue["id"]})
        self.assertEqual(status, 200, grant)
        status, archived = self._req(
            admin, "POST", f"/api/v2/setup/seasons/{season['id']}/archive",
            {"reason": "round 3"})
        self.assertEqual(status, 200, archived)

        arena = self._login("arena")
        status, _ = self._req(arena, "POST", "/api/context",
                              {"program_id": program["id"], "season_id": season["id"]})
        self.assertEqual(status, 200)

        seen = {}
        for opener, label in ((admin, "league admin"), (arena, "arena manager")):
            status, progress = self._req(opener, "GET", "/api/v2/setup/progress")
            self.assertEqual(status, 200, progress)
            rows = self._prereqs(progress, "facilities")
            seen[label] = rows
            self.assertEqual([p["key"] for p in rows],
                             ["season_selected", "season_active", "venue_access"],
                             f"{label}: {progress}")
            # Non-vacuity: the grant is live, so the round-2 floor is met and
            # cannot be what is blocking here.
            self.assertTrue(rows[2]["met"], f"{label}: {rows}")
            self.assertFalse(rows[1]["met"], f"{label}: {rows}")
            self.assertEqual(rows[1]["reason"], "season_archived", f"{label}: {rows}")
            self.assertIn(season["name"], rows[1]["detail"], f"{label}: {rows}")
        self.assertEqual(seen["league admin"], seen["arena manager"], seen)

        status, progress = self._req(admin, "GET", "/api/v2/setup/progress")
        self.assertNotEqual((progress["next_blocked"] or {}).get("key"),
                            "facilities",
                            f"this fixture exists because the roll-up cannot "
                            f"carry the fact for League Admin: {progress}")

        # The REAL reopen entry point, over the same route the card fires.
        status, reopened = self._req(
            admin, "POST", f"/api/v2/setup/seasons/{season['id']}/reopen",
            {"reason": "back to work"})
        self.assertEqual(status, 200, reopened)
        status, after = self._req(arena, "GET", "/api/v2/setup/progress")
        self.assertEqual(status, 200, after)
        self.assertTrue(all(p["met"] for p in self._prereqs(after, "facilities")),
                        after)
        self.assertEqual(after["next"]["key"], "facilities", after)
        self.assertIsNone(after["next_blocked"], after)

    def test_reopen_route_is_refused_for_a_role_without_manage_setup(self):
        """Why the card offers the reopen control to League Admin ONLY: the
        route itself is MANAGE_SETUP, so an Arena Manager pressing it would
        receive a 403. A control that can only fail is the same dead end this
        whole contract exists to remove."""
        admin = self._login("admin")
        status, program = self._req(admin, "POST", "/api/v2/setup/program",
                                    {"name": "R3 Reopen Authz Prog", "country": "US"})
        self.assertEqual(status, 200, program)
        status, season = self._req(admin, "POST", "/api/v2/setup/season",
                                   {"program_id": program["id"], "name": "R3 Authz"})
        self.assertEqual(status, 200, season)
        status, _ = self._req(admin, "POST", "/api/context",
                              {"program_id": program["id"], "season_id": season["id"]})
        self.assertEqual(status, 200)
        status, _ = self._req(
            admin, "POST", f"/api/v2/setup/seasons/{season['id']}/archive",
            {"reason": "authz check"})
        self.assertEqual(status, 200)

        arena = self._login("arena")
        status, _ = self._req(arena, "POST", "/api/context",
                              {"program_id": program["id"], "season_id": season["id"]})
        self.assertEqual(status, 200)
        status, denied = self._req(
            arena, "POST", f"/api/v2/setup/seasons/{season['id']}/reopen",
            {"reason": "let me in"})
        self.assertEqual(status, 403, denied)

        # ...and the Season really is still archived, so the refusal was not a
        # 403 handed back after a partial write.
        status, still = self._req(arena, "GET", "/api/v2/setup/progress")
        self.assertEqual(status, 200, still)
        rows = self._prereqs(still, "facilities")
        self.assertFalse(rows[1]["met"], rows)
        self.assertEqual(rows[1]["reason"], "season_archived", rows)

    def test_archived_season_publishes_every_participation_floor_over_real_http(self):
        """Participation's own audit over the real route, with an otherwise
        valid League/Division/Team so its eligibility floor asserts met and
        the archived Season is the only blocker left."""
        admin = self._login("admin")
        status, program = self._req(admin, "POST", "/api/v2/setup/program",
                                    {"name": "R3 Part HTTP Prog", "country": "US"})
        self.assertEqual(status, 200, program)
        status, season = self._req(admin, "POST", "/api/v2/setup/season",
                                   {"program_id": program["id"], "name": "R3 Part"})
        self.assertEqual(status, 200, season)
        status, league = self._req(admin, "POST", "/api/v2/setup/league",
                                   {"season_id": season["id"], "name": "R3 League"})
        self.assertEqual(status, 200, league)
        status, club = self._req(admin, "POST", "/api/v2/setup/club",
                                 {"name": "R3 Club"})
        self.assertEqual(status, 200, club)
        status, _ = self._req(admin, "POST", "/api/context",
                              {"program_id": program["id"], "season_id": season["id"]})
        self.assertEqual(status, 200)
        status, team = self._req(admin, "POST", "/api/v2/setup/team",
                                 {"club_id": club["id"], "league_id": league["id"],
                                  "name": "R3 Team"})
        self.assertEqual(status, 200, team)
        status, division = self._req(admin, "POST", "/api/v2/setup/division",
                                     {"season_id": season["id"],
                                      "league_id": league["id"], "name": "R3 Div"})
        self.assertEqual(status, 200, division)
        status, _ = self._req(
            admin, "POST", f"/api/v2/setup/seasons/{season['id']}/archive",
            {"reason": "round 3"})
        self.assertEqual(status, 200)

        status, progress = self._req(admin, "GET", "/api/v2/setup/progress")
        self.assertEqual(status, 200, progress)
        rows = self._prereqs(progress, "participation")
        self.assertEqual([p["key"] for p in rows],
                         ["season_selected", "season_active",
                          "team_league_eligible"], progress)
        self.assertTrue(rows[2]["met"],
                        f"the Team's league really is one this Season runs: {rows}")
        self.assertFalse(rows[1]["met"], rows)
        self.assertEqual(rows[1]["reason"], "season_archived", rows)
        self.assertIn(season["name"], rows[1]["detail"], rows)


if __name__ == "__main__":
    unittest.main()
