"""get_setup_progress's League-narrowing axis (#367, on top of #345's
Program/Season active-context selection).

``test_setup_progress.py`` already has full coverage for the pre-existing
Program/Season scoping (cross-Program isolation, per-Season participation/
facilities narrowing, role-aware ``next``/redaction, etc.) — this file adds
ONLY the third, League, axis #367 layered on top: a selected League further
narrows "Permanent teams", "Clubs, players and staff" (roster), and "Season
participation and divisions" to that League's own Teams/LeagueSeason, while
explicit "No League" keeps the full Program/Season-wide union (byte-identical
to pre-#367 behavior), and "League profile and seasons" / "Venues, rinks and
ice" are Program-wide/Season-wide facts that must never move with League
selection at all.

Run on Memory, SQLite, and (when ``TEST_DATABASE_URL`` is set) PostgreSQL via
the shared ``_backends()`` idiom (see ``test_active_context_league.py``) — a
review round on the four League-filtered "read contract" files flagged that
they exercised Memory only. This file also carries the full role matrix:
League Admin / Arena Manager / Viewer (the global roles, #211/#345) alongside
Coach / Player / Official / Guardian (scoped roles) — plus League-axis
negative cases (deleted, cross-Program, unbound, archived) that must fall back
to a sane default rather than error or leak, a stateless-read proof (no shared
mutable state survives between two different callers' reads), and a Viewer
no-mutation proof.

Only League Admin holds BOTH ``Permission.MANAGE_SETUP`` and
``Permission.MANAGE_ARENA`` (see ``hockey_scheduler/domain/roles.py``), so it
is the only role whose ``workflows`` response is ever non-empty for the six
#204 Setup workflows; Arena Manager holds only ``MANAGE_ARENA`` (sees
"facilities" alone). Coach, Player, Official, Guardian, and Viewer hold
NEITHER permission, so every one of them gets a permission-redacted EMPTY
``workflows`` list, `next`/`next_blocked` both None, and `complete` None
(never a real bool — see ``get_setup_progress``'s own docstring on why a role
that cannot see the full list must not be handed one) REGARDLESS of League
selection or of their own Program/Season resolution being perfectly correct.
For these roles the meaningful proof is therefore NOT "workflows narrow by
League" (there is nothing in ``workflows`` for them to narrow) but: their own
Program still resolves correctly, the redaction is stable across every League
selection, and an invalid/missing/revoked scoped identity fails CLOSED (a
named empty state, `complete: False`) rather than raising or leaking another
Program's data.
"""

import os
import unittest
from datetime import datetime, timezone

from helpers import BACKEND, fresh_sql_store  # noqa: F401  (ensures sys.path is set up)

from hockey_scheduler.api import ApiService
from hockey_scheduler.domain import (
    ActiveContext, GameType, GuardianLink, OfficialRole, Role, SeasonStatus)
from hockey_scheduler.domain.models import Game
from hockey_scheduler.domain.setup_models import OfficialAssignment
from hockey_scheduler.store import InMemoryStore, SqlStore

ADMIN = (Role.LEAGUE_ADMIN, {})
ARENA = (Role.ARENA_MANAGER, {})
VIEWER = (Role.VIEWER, {})
_TS = datetime(2027, 5, 1, 9, 0, 0, tzinfo=timezone.utc)


def _backends():
    yield "memory", InMemoryStore()
    yield "sqlite", SqlStore(":memory:")
    url = os.environ.get("TEST_DATABASE_URL")
    if url:
        # fresh_sql_store (not a bare SqlStore) so a database polluted by an
        # earlier module in this same serial worker is recovered rather than
        # failing to open at all -- see helpers.fresh_sql_store (#369).
        yield "postgres", fresh_sql_store(url)


def _close(store):
    if isinstance(store, SqlStore):
        store.close()


def _workflow(progress, key):
    return next(w for w in progress["workflows"] if w["key"] == key)


def _unbind(store, league_id, season_id):
    """Remove a LeagueSeason binding directly (the store-level effect of an
    authorized unbind), leaving the permanent League itself intact."""
    ls = store.league_season_for(league_id, season_id)
    if ls is not None:
        with store.transaction():
            store.delete_league_season(ls.id)


def _assign_game(api, official_id, *, season_id, team_id, league_id=None,
                 league_season_id=None, game_type=GameType.REGULAR.value):
    """An Official assignment to a Game with an EXPLICIT competition identity
    (mirrors ``test_active_context_league.py``'s own builder) — every field
    an Official's League derivation reads (``season_id``/``league_id``/
    ``league_season_id``/``game_type``) is passed in deliberately so a test
    can construct the exact valid/missing/drifted shape it means to exercise."""
    store = api.store
    with store.transaction():
        gid = store.next_id("game")
        store.add_game(Game(id=gid, home_team_id=team_id, away_team_id=team_id,
                            start_time=None, season_id=season_id,
                            league_id=league_id,
                            league_season_id=league_season_id,
                            game_type=game_type))
        store.add_official_assignment(OfficialAssignment(
            id=store.next_id("assign"), game_id=gid, official_id=official_id,
            role=OfficialRole.REFEREE))
    return gid


def _assigned_official(api, season_id, team_id, league_id):
    """An Official assigned to a well-formed REGULAR Game in ``league_id``/
    ``season_id``, carrying that pair's real frozen LeagueSeason — the only
    shape that grants League scope (#345 review, ``_official_league_ids``)."""
    oid = api.create_official("Ref")["id"]
    ls = api.store.league_season_for(league_id, season_id)
    gid = _assign_game(api, oid, season_id=season_id, team_id=team_id,
                       league_id=league_id, league_season_id=ls.id)
    return oid, gid


def _verified_guardian_link(store, guardian_user_id, player_id):
    with store.transaction():
        store.add_guardian_link(GuardianLink(
            id=store.next_id("glink"), guardian_user_id=guardian_user_id,
            player_id=player_id, created_at=_TS, verified=True))


class LeagueFilteredSetupProgressTest(unittest.TestCase):

    def test_teams_and_roster_narrow_to_selected_league_and_flip_on_switch(self):
        """Two Leagues in the SAME Program+Season, each with a different
        number of permanent Teams and Players on those Teams: selecting
        League X must reflect ONLY League X's counts in "teams"/"roster",
        and switching to League Y must flip both counts to Y's — proving the
        narrowing is live per-selection, not merely correct on first read."""
        for label, store in _backends():
            with self.subTest(backend=label):
                api = ApiService(store)
                api.create_user_account("admin", "pw", "league_admin")
                program = api.create_program("Prog", actor_id="admin")
                season = api.create_season(program["id"], "Fall", actor_id="admin")
                league_x = api.create_league(season["id"], "League X", actor_id="admin")
                league_y = api.create_league(season["id"], "League Y", actor_id="admin")
                club = api.create_club("C", actor_id="admin")

                team_x1 = api.create_team(club["id"], None, "X1", actor_id="admin",
                                          program_id=program["id"], league_id=league_x["id"])
                team_x2 = api.create_team(club["id"], None, "X2", actor_id="admin",
                                          program_id=program["id"], league_id=league_x["id"])
                team_y1 = api.create_team(club["id"], None, "Y1", actor_id="admin",
                                          program_id=program["id"], league_id=league_y["id"])
                self.assertNotIn("error", team_x1, team_x1)
                self.assertNotIn("error", team_y1, team_y1)

                # League X: 2 teams, 3 players (2 on X1, 1 on X2).
                api.create_player(team_x1["id"], "X1 Player A", "forward", actor_id="admin")
                api.create_player(team_x1["id"], "X1 Player B", "forward", actor_id="admin")
                api.create_player(team_x2["id"], "X2 Player A", "forward", actor_id="admin")
                # League Y: 1 team, 1 player.
                api.create_player(team_y1["id"], "Y1 Player A", "forward", actor_id="admin")

                api.set_active_context("admin", Role.LEAGUE_ADMIN, {},
                                       program["id"], season["id"], league_x["id"])
                progress_x = api.get_setup_progress("admin", *ADMIN)
                self.assertEqual(progress_x["program_id"], program["id"], label)
                teams_x = _workflow(progress_x, "teams")
                self.assertEqual(teams_x["status"], "done", label)
                self.assertEqual(teams_x["detail"], "2 team(s)", label)
                roster_x = _workflow(progress_x, "roster")
                self.assertEqual(roster_x["status"], "done", label)
                self.assertEqual(roster_x["detail"], "3 player(s)", label)

                api.set_active_context("admin", Role.LEAGUE_ADMIN, {},
                                       program["id"], season["id"], league_y["id"])
                progress_y = api.get_setup_progress("admin", *ADMIN)
                teams_y = _workflow(progress_y, "teams")
                self.assertEqual(teams_y["detail"], "1 team(s)",
                                 f"{label}: switching to League Y must flip the count "
                                 "to Y's own teams, not keep League X's")
                roster_y = _workflow(progress_y, "roster")
                self.assertEqual(roster_y["detail"], "1 player(s)",
                                 f"{label}: switching to League Y must flip the count "
                                 "to Y's own players, not keep League X's")

                # Switching back to League X reproduces the original counts —
                # proves neither read mutated shared state.
                api.set_active_context("admin", Role.LEAGUE_ADMIN, {},
                                       program["id"], season["id"], league_x["id"])
                progress_x_again = api.get_setup_progress("admin", *ADMIN)
                self.assertEqual(_workflow(progress_x_again, "teams")["detail"],
                                 "2 team(s)", label)
                self.assertEqual(_workflow(progress_x_again, "roster")["detail"],
                                 "3 player(s)", label)
                _close(store)

    def test_no_league_selected_shows_union_of_both_leagues(self):
        """Explicit "No League" (never selecting one, or selecting one and
        then clearing it back to None) must show the BROADER union across
        every League's Teams/Players — a strict superset of either League's
        own narrowed view, matching pre-#367 (Program/Season-only) behavior
        exactly."""
        for label, store in _backends():
            with self.subTest(backend=label):
                api = ApiService(store)
                api.create_user_account("admin", "pw", "league_admin")
                program = api.create_program("Prog", actor_id="admin")
                season = api.create_season(program["id"], "Fall", actor_id="admin")
                league_x = api.create_league(season["id"], "League X", actor_id="admin")
                league_y = api.create_league(season["id"], "League Y", actor_id="admin")
                club = api.create_club("C", actor_id="admin")

                team_x1 = api.create_team(club["id"], None, "X1", actor_id="admin",
                                          program_id=program["id"], league_id=league_x["id"])
                team_x2 = api.create_team(club["id"], None, "X2", actor_id="admin",
                                          program_id=program["id"], league_id=league_x["id"])
                team_y1 = api.create_team(club["id"], None, "Y1", actor_id="admin",
                                          program_id=program["id"], league_id=league_y["id"])
                api.create_player(team_x1["id"], "X1 Player A", "forward", actor_id="admin")
                api.create_player(team_x1["id"], "X1 Player B", "forward", actor_id="admin")
                api.create_player(team_x2["id"], "X2 Player A", "forward", actor_id="admin")
                api.create_player(team_y1["id"], "Y1 Player A", "forward", actor_id="admin")

                # Case A: a League is never explicitly selected at all — only
                # Program+Season are set (5-arg legacy call, league_id omitted).
                api.set_active_context("admin", Role.LEAGUE_ADMIN, {},
                                       program["id"], season["id"])
                never_selected = api.get_setup_progress("admin", *ADMIN)
                self.assertEqual(_workflow(never_selected, "teams")["detail"],
                                 "3 team(s)", label)
                self.assertEqual(_workflow(never_selected, "roster")["detail"],
                                 "4 player(s)", label)

                # Case B: a League WAS selected, then explicitly cleared back
                # to None — proves clearing genuinely widens the view again
                # rather than leaving a stale narrowed selection in place.
                api.set_active_context("admin", Role.LEAGUE_ADMIN, {},
                                       program["id"], season["id"], league_x["id"])
                narrowed = api.get_setup_progress("admin", *ADMIN)
                self.assertEqual(_workflow(narrowed, "teams")["detail"],
                                 "2 team(s)", label)

                api.set_active_context("admin", Role.LEAGUE_ADMIN, {},
                                       program["id"], season["id"], None)
                cleared = api.get_setup_progress("admin", *ADMIN)
                self.assertEqual(_workflow(cleared, "teams")["detail"], "3 team(s)",
                                 f"{label}: clearing back to No League must widen to "
                                 "the union, not keep League X's narrowed count")
                self.assertEqual(_workflow(cleared, "roster")["detail"],
                                 "4 player(s)", label)
                _close(store)

    def test_participation_narrows_by_league_and_no_league_shows_combined(self):
        """"Season participation and divisions": one Team registered under
        League X's LeagueSeason, a different Team under League Y's, same
        Season. Selecting League X shows only X's schedulable registration
        count, League Y only Y's, and No League shows both combined — the
        same narrow/union contract as "teams"/"roster", applied to
        registrations instead of Team/Player rows."""
        for label, store in _backends():
            with self.subTest(backend=label):
                api = ApiService(store)
                api.create_user_account("admin", "pw", "league_admin")
                program = api.create_program("Prog", actor_id="admin")
                season = api.create_season(program["id"], "Fall", actor_id="admin")
                league_x = api.create_league(season["id"], "League X", actor_id="admin")
                league_y = api.create_league(season["id"], "League Y", actor_id="admin")
                club = api.create_club("C", actor_id="admin")

                team_x = api.create_team(club["id"], None, "TeamX", actor_id="admin",
                                         program_id=program["id"], league_id=league_x["id"])
                team_y = api.create_team(club["id"], None, "TeamY", actor_id="admin",
                                         program_id=program["id"], league_id=league_y["id"])
                reg_x = api.register_team_for_season(season["id"], team_x["id"],
                                                     actor_id="admin", league_id=league_x["id"])
                reg_y = api.register_team_for_season(season["id"], team_y["id"],
                                                     actor_id="admin", league_id=league_y["id"])
                self.assertNotIn("error", reg_x, reg_x)
                self.assertNotIn("error", reg_y, reg_y)

                api.set_active_context("admin", Role.LEAGUE_ADMIN, {},
                                       program["id"], season["id"], league_x["id"])
                progress_x = api.get_setup_progress("admin", *ADMIN)
                participation_x = _workflow(progress_x, "participation")
                self.assertEqual(participation_x["status"], "done", label)
                self.assertEqual(participation_x["detail"],
                                 "1 schedulable registration(s)", label)

                api.set_active_context("admin", Role.LEAGUE_ADMIN, {},
                                       program["id"], season["id"], league_y["id"])
                progress_y = api.get_setup_progress("admin", *ADMIN)
                participation_y = _workflow(progress_y, "participation")
                self.assertEqual(participation_y["status"], "done", label)
                self.assertEqual(participation_y["detail"],
                                 "1 schedulable registration(s)",
                                 f"{label}: League Y's own view must not include "
                                 "League X's registration")

                api.set_active_context("admin", Role.LEAGUE_ADMIN, {},
                                       program["id"], season["id"], None)
                progress_none = api.get_setup_progress("admin", *ADMIN)
                participation_none = _workflow(progress_none, "participation")
                self.assertEqual(participation_none["detail"],
                                 "2 schedulable registration(s)",
                                 f"{label}: No League selected must show both "
                                 "registrations combined, matching pre-#367 "
                                 "full-season behavior")
                _close(store)

    def test_league_selection_never_leaks_across_programs(self):
        """Two Programs, each with their own two Leagues, every one of the
        four (Program, League) combinations carrying a DIFFERENT team count
        (1/2/3/4) so any leak — wrong Program OR wrong League — is caught
        precisely. Switches BOTH axes together (not just Program, like
        ``test_cross_program_isolation`` does for the pre-existing two-axis
        case) and revisits an earlier combination to prove no residual state
        survives a round trip through the others."""
        for label, store in _backends():
            with self.subTest(backend=label):
                api = ApiService(store)
                api.create_user_account("admin", "pw", "league_admin")
                club = api.create_club("C", actor_id="admin")

                program_a = api.create_program("Prog A", actor_id="admin")
                season_a = api.create_season(program_a["id"], "Season A", actor_id="admin")
                league_ax = api.create_league(season_a["id"], "AX", actor_id="admin")
                league_ay = api.create_league(season_a["id"], "AY", actor_id="admin")

                program_b = api.create_program("Prog B", actor_id="admin")
                season_b = api.create_season(program_b["id"], "Season B", actor_id="admin")
                league_bx = api.create_league(season_b["id"], "BX", actor_id="admin")
                league_by = api.create_league(season_b["id"], "BY", actor_id="admin")

                def _team(name, program, league):
                    t = api.create_team(club["id"], None, name, actor_id="admin",
                                        program_id=program["id"], league_id=league["id"])
                    self.assertNotIn("error", t, t)
                    return t

                _team("AX1", program_a, league_ax)                      # A/AX: 1
                _team("AY1", program_a, league_ay)                      # A/AY: 2
                _team("AY2", program_a, league_ay)
                _team("BX1", program_b, league_bx)                      # B/BX: 3
                _team("BX2", program_b, league_bx)
                _team("BX3", program_b, league_bx)
                _team("BY1", program_b, league_by)                      # B/BY: 4
                _team("BY2", program_b, league_by)
                _team("BY3", program_b, league_by)
                _team("BY4", program_b, league_by)

                def _select_and_read(program, season, league):
                    api.set_active_context("admin", Role.LEAGUE_ADMIN, {},
                                           program["id"], season["id"], league["id"])
                    progress = api.get_setup_progress("admin", *ADMIN)
                    self.assertEqual(progress["program_id"], program["id"], label)
                    return _workflow(progress, "teams")["detail"]

                self.assertEqual(_select_and_read(program_a, season_a, league_ax),
                                 "1 team(s)", label)
                self.assertEqual(_select_and_read(program_a, season_a, league_ay),
                                 "2 team(s)", label)
                self.assertEqual(_select_and_read(program_b, season_b, league_bx),
                                 "3 team(s)", label)
                self.assertEqual(_select_and_read(program_b, season_b, league_by),
                                 "4 team(s)", label)

                # Revisit combinations already read, out of order, to rule out
                # any residual state leaking from a PRIOR selection.
                self.assertEqual(_select_and_read(program_a, season_a, league_ax),
                                 "1 team(s)", label)
                self.assertEqual(_select_and_read(program_b, season_b, league_by),
                                 "4 team(s)", label)
                self.assertEqual(_select_and_read(program_a, season_a, league_ay),
                                 "2 team(s)", label)
                self.assertEqual(_select_and_read(program_b, season_b, league_bx),
                                 "3 team(s)", label)
                _close(store)

    def test_league_profile_and_facilities_are_invariant_to_league_selection(self):
        """"League profile and seasons" (a Program-wide integrity check) and
        "Venues, rinks and ice" (a physical-resource workflow with no
        competition-League axis at all) must report the SAME value
        regardless of which League — or none — is active. Directly asserted
        for both League Admin (who sees both workflows) and Arena Manager
        (whose only visible workflow is facilities), since this is the one
        thing most likely to accidentally regress if a future change narrows
        too aggressively."""
        for label, store in _backends():
            with self.subTest(backend=label):
                api = ApiService(store)
                api.create_user_account("admin", "pw", "league_admin")
                program = api.create_program("Prog", actor_id="admin")
                season = api.create_season(program["id"], "Fall", actor_id="admin")
                league_x = api.create_league(season["id"], "League X", actor_id="admin")
                league_y = api.create_league(season["id"], "League Y", actor_id="admin")

                venue = api.create_venue("V", league_id=program["id"], actor_id="admin")
                rink = api.create_rink(venue["id"], "R", actor_id="admin")
                api.create_ice_slot(rink["id"], "2026-09-01T18:30:00+00:00",
                                    "2026-09-01T20:00:00+00:00", actor_id="admin")
                api.grant_season_venue_access(season["id"], venue["id"], actor_id="admin")

                def _read(role_tuple, league):
                    if league is None:
                        api.set_active_context("admin", Role.LEAGUE_ADMIN, {},
                                               program["id"], season["id"], None)
                    else:
                        api.set_active_context("admin", Role.LEAGUE_ADMIN, {},
                                               program["id"], season["id"], league["id"])
                    progress = api.get_setup_progress("admin", *role_tuple)
                    return progress

                # League Admin sees both "league_season" and "facilities".
                admin_x = _read(ADMIN, league_x)
                admin_y = _read(ADMIN, league_y)
                admin_none = _read(ADMIN, None)

                league_season_x = _workflow(admin_x, "league_season")
                self.assertEqual(league_season_x["status"], "done", label)
                self.assertEqual(league_season_x, _workflow(admin_y, "league_season"),
                                 f"{label}: league_season must be identical for "
                                 "League Y as for League X")
                self.assertEqual(league_season_x, _workflow(admin_none, "league_season"),
                                 f"{label}: league_season must be identical for "
                                 "No League too")

                facilities_x = _workflow(admin_x, "facilities")
                self.assertEqual(facilities_x["status"], "done", label)
                self.assertEqual(facilities_x["detail"], "1 available game slot(s)",
                                 label)
                self.assertEqual(facilities_x, _workflow(admin_y, "facilities"),
                                 f"{label}: facilities must be identical for League "
                                 "Y as for League X")
                self.assertEqual(facilities_x, _workflow(admin_none, "facilities"),
                                 f"{label}: facilities must be identical for No "
                                 "League too")

                # Arena Manager's own (redacted-to-facilities-only) view must
                # show the exact same invariance.
                arena_x = _read(ARENA, league_x)
                arena_y = _read(ARENA, league_y)
                arena_none = _read(ARENA, None)
                self.assertEqual([w["key"] for w in arena_x["workflows"]],
                                 ["facilities"], label)
                self.assertEqual(_workflow(arena_x, "facilities"), facilities_x, label)
                self.assertEqual(_workflow(arena_y, "facilities"), facilities_x,
                                 f"{label}: Arena Manager's facilities view must "
                                 "also be unaffected by League selection")
                self.assertEqual(_workflow(arena_none, "facilities"), facilities_x,
                                 f"{label}: Arena Manager's facilities view must "
                                 "also be unaffected by clearing League selection")
                _close(store)


class LeagueFilteredSetupProgressRoleMatrixTest(unittest.TestCase):
    """The full #367 role matrix: the two global operators plus the global
    read-only Viewer, and every scoped role (Coach, Player, Official,
    Guardian). Only League Admin (MANAGE_SETUP + MANAGE_ARENA) and Arena
    Manager (MANAGE_ARENA) ever see a non-empty ``workflows`` list — Viewer
    and every scoped role hold neither permission, so their meaningful proof
    is correct Program resolution plus a stable, permission-redacted empty
    response across the League axis, and fail-closed behavior for an
    invalid/revoked scoped identity."""

    def _assert_scoped_ok(self, progress, program_id, label):
        """The shape a role whose visible workflow slice (0 of 6) is
        narrower than the full list must always get, whenever its own
        Program DOES resolve: real program identity, but `complete` held to
        `None` (never a real bool) per ``get_setup_progress``'s own
        redaction contract."""
        self.assertEqual(progress["program_id"], program_id, label)
        self.assertEqual(progress["workflows"], [], label)
        self.assertIsNone(progress["next"], label)
        self.assertIsNone(progress["next_blocked"], label)
        self.assertIsNone(progress["complete"], label)

    def _assert_no_program(self, progress, label):
        """The NAMED empty state ``get_setup_progress`` returns when no
        Program resolves at all (unauthorized/missing/revoked scope) —
        `complete` is `False` here, NOT `None`: a real, meaningful claim
        ("no Program, so nothing is complete"), distinct from the
        redaction-driven `None` above."""
        self.assertIsNone(progress["program_id"], label)
        self.assertIsNone(progress["program"], label)
        self.assertEqual(progress["workflows"], [], label)
        self.assertIsNone(progress["next"], label)
        self.assertIsNone(progress["next_blocked"], label)
        self.assertFalse(progress["complete"], label)

    def test_global_roles_including_viewer_resolve_correctly_across_league_selection(self):
        """League Admin, Arena Manager, and Viewer (#211/#345's three GLOBAL
        roles) all resolve the SAME Program regardless of role, across
        League X, League Y, and No League. League Admin's `workflows`
        narrows by League as usual; Arena Manager's stays pinned to
        "facilities" only; Viewer — never covered by this file before — gets
        the SAME permission-redacted empty list as a scoped role, proving
        "sees every Program" (global scope) and "sees any workflow"
        (MANAGE_SETUP/MANAGE_ARENA) are independent axes."""
        for label, store in _backends():
            with self.subTest(backend=label):
                api = ApiService(store)
                api.create_user_account("admin", "pw", "league_admin")
                program = api.create_program("Prog", actor_id="admin")
                season = api.create_season(program["id"], "Fall", actor_id="admin")
                league_x = api.create_league(season["id"], "League X", actor_id="admin")
                league_y = api.create_league(season["id"], "League Y", actor_id="admin")
                club = api.create_club("C", actor_id="admin")
                team_x1 = api.create_team(club["id"], None, "X1", actor_id="admin",
                                          program_id=program["id"], league_id=league_x["id"])
                api.create_team(club["id"], None, "X2", actor_id="admin",
                                program_id=program["id"], league_id=league_x["id"])
                api.create_team(club["id"], None, "Y1", actor_id="admin",
                                program_id=program["id"], league_id=league_y["id"])
                self.assertNotIn("error", team_x1, team_x1)

                for user_id, role_tuple in (("admin1", ADMIN), ("arena1", ARENA),
                                            ("viewer1", VIEWER)):
                    for league, expected_teams in ((league_x, "2 team(s)"),
                                                   (league_y, "1 team(s)"),
                                                   (None, "3 team(s)")):
                        league_id = league["id"] if league else None
                        api.set_active_context(user_id, *role_tuple,
                                               program["id"], season["id"], league_id)
                        progress = api.get_setup_progress(user_id, *role_tuple)
                        self.assertEqual(progress["program_id"], program["id"],
                                         (label, user_id, league_id))
                        if role_tuple is ADMIN:
                            self.assertEqual(
                                [w["key"] for w in progress["workflows"]],
                                ["league_season", "teams", "participation",
                                 "roster", "facilities", "import"],
                                (label, league_id))
                            self.assertEqual(
                                _workflow(progress, "teams")["detail"],
                                expected_teams, (label, league_id))
                        elif role_tuple is ARENA:
                            self.assertEqual(
                                [w["key"] for w in progress["workflows"]],
                                ["facilities"], (label, league_id))
                            self.assertIsNone(progress["complete"], (label, league_id))
                        else:   # VIEWER — global scope, but zero manage permissions
                            self.assertEqual(progress["workflows"], [],
                                             (label, league_id))
                            self.assertIsNone(progress["next"], (label, league_id))
                            self.assertIsNone(progress["complete"], (label, league_id))
                _close(store)

    def test_scoped_roles_resolve_own_program_with_redacted_workflows_regardless_of_league(self):
        """Coach, Player, Official, and Guardian each resolve their OWN
        Program correctly whether their authorized League is explicitly
        selected or cleared to "No League" — proving the empty-`workflows`
        redaction these roles always get is a PERMISSION fact (neither holds
        MANAGE_SETUP nor MANAGE_ARENA) rather than something that varies
        with the League axis, and that the League-aware context resolution
        underneath never errors or misresolves for a scoped caller."""
        for label, store in _backends():
            with self.subTest(backend=label):
                api = ApiService(store)
                api.create_user_account("admin", "pw", "league_admin")
                program = api.create_program("Prog", actor_id="admin")
                season = api.create_season(program["id"], "Fall", actor_id="admin")
                league_x = api.create_league(season["id"], "League X", actor_id="admin")
                club = api.create_club("C", actor_id="admin")
                team_x = api.create_team(club["id"], None, "TeamX", actor_id="admin",
                                         program_id=program["id"], league_id=league_x["id"])
                reg = api.register_team_for_season(
                    season["id"], team_x["id"], actor_id="admin",
                    league_id=league_x["id"])
                self.assertNotIn("error", reg, reg)
                player = api.create_player(
                    team_x["id"], "Kid", "forward", actor_id="admin")
                oid, _gid = _assigned_official(
                    api, season["id"], team_x["id"], league_x["id"])
                _verified_guardian_link(store, "guardian1", player["id"])

                roles = (
                    ("coach1", Role.COACH, {"team_id": team_x["id"]}),
                    ("player1", Role.PLAYER, {"player_id": player["id"]}),
                    ("official1", Role.OFFICIAL, {"official_id": oid}),
                    ("guardian1", Role.GUARDIAN, {}),
                )
                for user_id, role, scope in roles:
                    for league_id in (league_x["id"], None):
                        api.set_active_context(user_id, role, scope,
                                               program["id"], season["id"], league_id)
                        progress = api.get_setup_progress(user_id, role, scope)
                        self._assert_scoped_ok(
                            progress, program["id"], (label, role, league_id))
                _close(store)

    def test_coach_league_authorization_revoked_by_transfer_fails_closed_without_leaking(self):
        """A Coach's own League authorization is revoked when their Team
        transfers to a different League (mirrors
        ``test_active_context_league.py``'s
        ``test_revoked_league_authorization_falls_back_and_restores`` at the
        ContextService layer) — this proves that revocation flows into
        ``get_setup_progress`` without error, AND that it is a fact about
        the Coach's OWN authorization bookkeeping, not about the actual data
        League Admin reads: switching the transferred Team's League for real
        moves it out of League X's count and into League Y's, observed
        independently through League Admin's own (unaffected) narrowed
        view."""
        for label, store in _backends():
            with self.subTest(backend=label):
                api = ApiService(store)
                api.create_user_account("admin", "pw", "league_admin")
                program = api.create_program("Prog", actor_id="admin")
                season = api.create_season(program["id"], "Fall", actor_id="admin")
                league_x = api.create_league(season["id"], "League X", actor_id="admin")
                league_y = api.create_league(season["id"], "League Y", actor_id="admin")
                club = api.create_club("C", actor_id="admin")
                team_x = api.create_team(club["id"], None, "TeamX", actor_id="admin",
                                         program_id=program["id"], league_id=league_x["id"])
                api.create_team(club["id"], None, "TeamY", actor_id="admin",
                                program_id=program["id"], league_id=league_y["id"])

                coach = (Role.COACH, {"team_id": team_x["id"]})
                api.set_active_context("coach1", *coach,
                                       program["id"], season["id"], league_x["id"])
                before = api.get_setup_progress("coach1", *coach)
                self._assert_scoped_ok(before, program["id"], label)

                # The Coach's Team transfers out of League X into League Y —
                # same Program, so Program-level authorization survives; only
                # the League axis is revoked.
                t = store.get_team(team_x["id"])
                t.league_id = league_y["id"]
                store.save_team(t)

                after = api.get_setup_progress("coach1", *coach)
                self._assert_scoped_ok(after, program["id"], label)

                # Independently: League Admin's OWN narrowed view reflects the
                # real transfer — League X now has zero teams, League Y two —
                # proving the Coach's revoked bookkeeping never leaked into
                # (or was confused with) the actual Program data.
                api.set_active_context("admin", *ADMIN,
                                       program["id"], season["id"], league_x["id"])
                admin_x = api.get_setup_progress("admin", *ADMIN)
                self.assertEqual(_workflow(admin_x, "teams")["status"], "todo", label)
                self.assertEqual(_workflow(admin_x, "teams")["detail"],
                                 "No team added yet.", label)

                api.set_active_context("admin", *ADMIN,
                                       program["id"], season["id"], league_y["id"])
                admin_y = api.get_setup_progress("admin", *ADMIN)
                self.assertEqual(_workflow(admin_y, "teams")["detail"],
                                 "2 team(s)", label)
                _close(store)

    def test_invalid_or_missing_scoped_identities_fail_closed_without_error(self):
        """A dangling/missing Coach ``team_id``, a deactivated Player, an
        unassigned Official, and an unverified Guardian link must each fail
        CLOSED to the named empty state (no Program resolves) rather than
        raising or leaking — proven as a real before/after transition for
        Player/Official/Guardian (authorization genuinely held, then
        genuinely revoked), since a single-shot check could pass by
        accident on a role that was never authorized at all."""
        for label, store in _backends():
            with self.subTest(backend=label):
                api = ApiService(store)
                api.create_user_account("admin", "pw", "league_admin")
                program = api.create_program("Prog", actor_id="admin")
                season = api.create_season(program["id"], "Fall", actor_id="admin")
                league_x = api.create_league(season["id"], "League X", actor_id="admin")
                club = api.create_club("C", actor_id="admin")
                team_x = api.create_team(club["id"], None, "TeamX", actor_id="admin",
                                         program_id=program["id"], league_id=league_x["id"])
                reg = api.register_team_for_season(
                    season["id"], team_x["id"], actor_id="admin",
                    league_id=league_x["id"])
                self.assertNotIn("error", reg, reg)

                # Coach: a team_id that never existed — a dangling reference,
                # never even authorized, so no active-context write is needed.
                ghost_progress = api.get_setup_progress(
                    "coach_ghost", Role.COACH, {"team_id": "team_does_not_exist"})
                self._assert_no_program(ghost_progress, label)

                # Player: active and authorized, then deactivated (#270).
                player = api.create_player(
                    team_x["id"], "Kid", "forward", actor_id="admin")
                pl = (Role.PLAYER, {"player_id": player["id"]})
                api.set_active_context("player1", *pl,
                                       program["id"], season["id"], league_x["id"])
                self._assert_scoped_ok(
                    api.get_setup_progress("player1", *pl), program["id"], label)
                p = store.get_player(player["id"])
                p.is_active = False
                store.save_player(p)
                self._assert_no_program(
                    api.get_setup_progress("player1", *pl), label)

                # Official: assigned and authorized, then the assignment is
                # removed entirely.
                oid, gid = _assigned_official(
                    api, season["id"], team_x["id"], league_x["id"])
                off = (Role.OFFICIAL, {"official_id": oid})
                api.set_active_context("official1", *off,
                                       program["id"], season["id"], league_x["id"])
                self._assert_scoped_ok(
                    api.get_setup_progress("official1", *off), program["id"], label)
                with store.transaction():
                    store.remove_official_assignment(
                        store.assignments_for_official(oid)[0].id)
                self._assert_no_program(
                    api.get_setup_progress("official1", *off), label)

                # Guardian: verified and authorized, then the link is revoked
                # (verified -> False), NOT deleted.
                junior = api.create_player(
                    team_x["id"], "Junior", "forward", actor_id="admin")
                _verified_guardian_link(store, "guardian1", junior["id"])
                api.set_active_context("guardian1", Role.GUARDIAN, {},
                                       program["id"], season["id"], league_x["id"])
                self._assert_scoped_ok(
                    api.get_setup_progress("guardian1", Role.GUARDIAN, {}),
                    program["id"], label)
                link = store.guardian_links_for("guardian1")[0]
                link.verified = False
                store.save_guardian_link(link)
                self._assert_no_program(
                    api.get_setup_progress("guardian1", Role.GUARDIAN, {}), label)
                _close(store)


class LeagueFilteredSetupProgressInvalidLeagueTest(unittest.TestCase):
    """League-axis negative cases: a deleted League, a League bound to a
    DIFFERENT Program somehow persisted on the active context, a League with
    no LeagueSeason binding to the resolved Season, and an archived Season's
    League — each must fall back to a sane default (ignore the invalid
    League, widen to the full Program/Season union) or, for the archived
    case, be honored read-only, and never error or leak cross-Program data.
    Mirrors ``ContextService.resolve_with_league``'s own tested
    "ignore-don't-rewrite" contract (``test_active_context_league.py``),
    proven here at the ``get_setup_progress`` facade instead of the context
    layer directly."""

    def test_deleted_league_after_selection_falls_back_to_union_without_error(self):
        for label, store in _backends():
            with self.subTest(backend=label):
                api = ApiService(store)
                api.create_user_account("admin", "pw", "league_admin")
                program = api.create_program("Prog", actor_id="admin")
                season = api.create_season(program["id"], "Fall", actor_id="admin")
                league_x = api.create_league(season["id"], "League X", actor_id="admin")
                league_y = api.create_league(season["id"], "League Y", actor_id="admin")
                club = api.create_club("C", actor_id="admin")
                api.create_team(club["id"], None, "X1", actor_id="admin",
                                program_id=program["id"], league_id=league_x["id"])
                api.create_team(club["id"], None, "X2", actor_id="admin",
                                program_id=program["id"], league_id=league_x["id"])
                api.create_team(club["id"], None, "Y1", actor_id="admin",
                                program_id=program["id"], league_id=league_y["id"])

                api.set_active_context("admin", *ADMIN,
                                       program["id"], season["id"], league_x["id"])
                narrowed = api.get_setup_progress("admin", *ADMIN)
                self.assertEqual(_workflow(narrowed, "teams")["detail"],
                                 "2 team(s)", label)

                _unbind(store, league_x["id"], season["id"])
                with store.transaction():
                    store.delete_league(league_x["id"])

                progress = api.get_setup_progress("admin", *ADMIN)
                self.assertEqual(progress["program_id"], program["id"], label)
                self.assertEqual(_workflow(progress, "teams")["detail"],
                                 "3 team(s)",
                                 f"{label}: a deleted selected League must fall "
                                 "back to the full union, not error")
                # ignore-don't-rewrite: the saved row still names the deleted
                # League — resolution never rewrites it.
                self.assertEqual(
                    store.get_active_context("admin").league_id, league_x["id"],
                    label)
                _close(store)

    def test_league_bound_to_a_different_program_on_active_context_never_leaks(self):
        """A corrupted/raced active-context row naming a League that belongs
        to a DIFFERENT Program than the resolved Program+Season (never
        reachable through the validated ``set_active_context`` API, which
        already refuses a cross-Program League at selection time — this
        simulates the row existing anyway, e.g. a prior valid selection later
        orphaned). ``get_setup_progress`` must resolve the correct (own)
        Program, drop the invalid League, and show ONLY that Program's own
        union — never Program B's team count, and never an error."""
        for label, store in _backends():
            with self.subTest(backend=label):
                api = ApiService(store)
                api.create_user_account("admin", "pw", "league_admin")
                club = api.create_club("C", actor_id="admin")

                program_a = api.create_program("Prog A", actor_id="admin")
                season_a = api.create_season(program_a["id"], "Season A", actor_id="admin")
                league_a = api.create_league(season_a["id"], "League A", actor_id="admin")
                api.create_team(club["id"], None, "A1", actor_id="admin",
                                program_id=program_a["id"], league_id=league_a["id"])
                api.create_team(club["id"], None, "A2", actor_id="admin",
                                program_id=program_a["id"], league_id=league_a["id"])
                # A leagueless Program A team too, so the union genuinely
                # counts every Program A team, not just League A's.
                api.create_team(club["id"], None, "A_noleague", actor_id="admin",
                                program_id=program_a["id"])

                program_b = api.create_program("Prog B", actor_id="admin")
                season_b = api.create_season(program_b["id"], "Season B", actor_id="admin")
                league_b = api.create_league(season_b["id"], "League B", actor_id="admin")
                for i in range(5):        # a deliberately distinct count
                    api.create_team(club["id"], None, f"B{i}", actor_id="admin",
                                    program_id=program_b["id"], league_id=league_b["id"])

                # A legitimate baseline selection (Program A, no League)...
                api.set_active_context("admin", *ADMIN,
                                       program_a["id"], season_a["id"], None)
                # ...then the row is overwritten directly at the store level
                # with Program B's League while Program/Season stay A's —
                # the shape this test exists to prove is handled.
                store.set_active_context(ActiveContext(
                    id="admin", program_id=program_a["id"], season_id=season_a["id"],
                    updated_at=_TS, league_id=league_b["id"]))

                progress = api.get_setup_progress("admin", *ADMIN)
                self.assertEqual(progress["program_id"], program_a["id"], label)
                self.assertEqual(_workflow(progress, "teams")["detail"],
                                 "3 team(s)",
                                 f"{label}: must show Program A's own union "
                                 "(never Program B's 5 teams, never an error)")
                # The corrupted row is left as-is (not silently repaired) —
                # resolution ignores it without rewriting it.
                self.assertEqual(
                    store.get_active_context("admin").league_id, league_b["id"],
                    label)
                _close(store)

    def test_league_unbound_from_active_season_falls_back_to_union(self):
        """A League that still exists (unlike the deleted-League case above)
        but whose ``LeagueSeason`` binding to the RESOLVED Season has been
        removed — e.g. an administrative unbind after the selection was
        made — must be treated exactly like an invalid selection: fall back
        to the full union rather than continuing to (wrongly) narrow by a
        League no longer bound here."""
        for label, store in _backends():
            with self.subTest(backend=label):
                api = ApiService(store)
                api.create_user_account("admin", "pw", "league_admin")
                program = api.create_program("Prog", actor_id="admin")
                season = api.create_season(program["id"], "Fall", actor_id="admin")
                league_x = api.create_league(season["id"], "League X", actor_id="admin")
                league_y = api.create_league(season["id"], "League Y", actor_id="admin")
                club = api.create_club("C", actor_id="admin")
                api.create_team(club["id"], None, "X1", actor_id="admin",
                                program_id=program["id"], league_id=league_x["id"])
                api.create_team(club["id"], None, "X2", actor_id="admin",
                                program_id=program["id"], league_id=league_x["id"])
                api.create_team(club["id"], None, "Y1", actor_id="admin",
                                program_id=program["id"], league_id=league_y["id"])

                api.set_active_context("admin", *ADMIN,
                                       program["id"], season["id"], league_x["id"])
                narrowed = api.get_setup_progress("admin", *ADMIN)
                self.assertEqual(_workflow(narrowed, "teams")["detail"],
                                 "2 team(s)", label)

                # The League itself and its Teams stay intact -- only the
                # binding to THIS Season is removed.
                _unbind(store, league_x["id"], season["id"])

                progress = api.get_setup_progress("admin", *ADMIN)
                self.assertEqual(progress["program_id"], program["id"], label)
                self.assertEqual(_workflow(progress, "teams")["detail"],
                                 "3 team(s)",
                                 f"{label}: an unbound League must fall back to "
                                 "the union, not keep narrowing")
                self.assertEqual(
                    store.get_active_context("admin").league_id, league_x["id"],
                    f"{label}: the saved row must not be rewritten")
                _close(store)

    def test_archived_season_league_is_honored_read_only_and_still_narrows(self):
        """An ARCHIVED Season's League selection is HONORED as read-only
        history (matches ``test_active_context_league.py``'s
        ``test_archived_season_keeps_its_league_as_read_only_history``) —
        this is the one shape from the missing/deleted/unbound/archived/
        cross-Program list that must NOT fall back: ``get_setup_progress``
        must keep narrowing by the archived Season's own League exactly as
        it did while the Season was active, proving archival doesn't blank
        out or corrupt the League-narrowing computation."""
        for label, store in _backends():
            with self.subTest(backend=label):
                api = ApiService(store)
                api.create_user_account("admin", "pw", "league_admin")
                program = api.create_program("Prog", actor_id="admin")
                season = api.create_season(program["id"], "Fall", actor_id="admin")
                league_x = api.create_league(season["id"], "League X", actor_id="admin")
                league_y = api.create_league(season["id"], "League Y", actor_id="admin")
                club = api.create_club("C", actor_id="admin")
                team_x1 = api.create_team(club["id"], None, "X1", actor_id="admin",
                                          program_id=program["id"], league_id=league_x["id"])
                team_x2 = api.create_team(club["id"], None, "X2", actor_id="admin",
                                          program_id=program["id"], league_id=league_x["id"])
                api.create_team(club["id"], None, "Y1", actor_id="admin",
                                program_id=program["id"], league_id=league_y["id"])
                api.create_player(team_x1["id"], "A", "forward", actor_id="admin")
                api.create_player(team_x1["id"], "B", "forward", actor_id="admin")
                api.create_player(team_x2["id"], "C", "forward", actor_id="admin")

                api.set_active_context("admin", *ADMIN,
                                       program["id"], season["id"], league_x["id"])
                before = api.get_setup_progress("admin", *ADMIN)
                self.assertEqual(_workflow(before, "teams")["detail"],
                                 "2 team(s)", label)

                s = store.get_season(season["id"])
                s.status = SeasonStatus.ARCHIVED
                store.save_season(s)

                after = api.get_setup_progress("admin", *ADMIN)
                self.assertEqual(after["program_id"], program["id"], label)
                self.assertEqual(_workflow(after, "teams")["detail"], "2 team(s)",
                                 f"{label}: an archived Season's League must "
                                 "still narrow exactly as before archival")
                self.assertEqual(_workflow(after, "roster")["detail"],
                                 "3 player(s)", label)
                _close(store)


class LeagueFilteredSetupProgressSeasonCeilingTest(unittest.TestCase):
    """The #367 owner ruling: the active-Season ceiling from #330 review
    round 1 finding 2 is not merely a narrowing convenience — it is a hard,
    non-optional ceiling for every Season-BOUND workflow here, exactly as
    ``get_demo_overview``'s own
    ``in_scope_season_ids = {season.id} if season is not None else set()``
    idiom already is for its own Season-scoped collections
    (``docs/architecture/active-context-scoping.md``). "Season participation
    and divisions" and "Venues, rinks and ice" are the two Season-bound
    workflows (each counts rows that reach a Season only through a
    LeagueSeason or a SeasonVenueAccess grant): both must narrow to the
    ACTIVE Season alone, flip when the active Season switches, and go EMPTY
    under a Program-only context — never fall back to the union of every
    Season the Program has. "League profile and seasons" (Program-wide over
    every Season collectively) and "Permanent teams"/"Clubs, players and
    staff" (Team/Player carry no Season field at all) are the deliberate
    contrast: their counts must NOT move with the Season selection at all —
    included here so a future change that accidentally narrows them by
    Season, or accidentally widens the two Season-bound workflows back to
    every Season, is caught either way."""

    def test_two_seasons_ceiling_flips_on_switch_and_empties_program_only(self):
        """Two Seasons in one Program, each with two Leagues, each League
        carrying its own registered Team and its own granted ice, sized so
        every count is distinguishable (1 vs 2 registrations/slots per
        Season, per League — and 4 Teams total across both Seasons). With
        Season 1 active, every Season-bound count reflects Season 1 ONLY;
        switching the active context to Season 2 flips both to Season 2's
        own counts; and resolving to Program-only (no Season selected)
        empties both regardless of which Season has real data — covering
        both the League-selected and the No-League ("union within the
        active Season") case at each step."""
        for label, store in _backends():
            with self.subTest(backend=label):
                api = ApiService(store)
                api.create_user_account("admin", "pw", "league_admin")
                program = api.create_program("Prog", actor_id="admin")
                club = api.create_club("C", actor_id="admin")

                # -- Season 1: two Leagues, one registered Team each, one
                # granted Venue/Rink/available game slot.
                season1 = api.create_season(program["id"], "S1", actor_id="admin")
                league_1a = api.create_league(season1["id"], "1A", actor_id="admin")
                league_1b = api.create_league(season1["id"], "1B", actor_id="admin")
                team_1a = api.create_team(club["id"], None, "T1A", actor_id="admin",
                                          program_id=program["id"],
                                          league_id=league_1a["id"])
                team_1b = api.create_team(club["id"], None, "T1B", actor_id="admin",
                                          program_id=program["id"],
                                          league_id=league_1b["id"])
                reg_1a = api.register_team_for_season(
                    season1["id"], team_1a["id"], actor_id="admin",
                    league_id=league_1a["id"])
                reg_1b = api.register_team_for_season(
                    season1["id"], team_1b["id"], actor_id="admin",
                    league_id=league_1b["id"])
                self.assertNotIn("error", reg_1a, reg_1a)
                self.assertNotIn("error", reg_1b, reg_1b)
                venue1 = api.create_venue("V1", league_id=program["id"],
                                          actor_id="admin")
                rink1 = api.create_rink(venue1["id"], "R1", actor_id="admin")
                api.create_ice_slot(rink1["id"], "2026-09-01T18:00:00+00:00",
                                    "2026-09-01T19:30:00+00:00", actor_id="admin")
                api.grant_season_venue_access(season1["id"], venue1["id"],
                                              actor_id="admin")

                # -- Season 2: same shape but with 2 available slots (instead
                # of 1) so a leaked Season 1 count is caught either way.
                season2 = api.create_season(program["id"], "S2", actor_id="admin")
                league_2a = api.create_league(season2["id"], "2A", actor_id="admin")
                league_2b = api.create_league(season2["id"], "2B", actor_id="admin")
                team_2a = api.create_team(club["id"], None, "T2A", actor_id="admin",
                                          program_id=program["id"],
                                          league_id=league_2a["id"])
                team_2b = api.create_team(club["id"], None, "T2B", actor_id="admin",
                                          program_id=program["id"],
                                          league_id=league_2b["id"])
                reg_2a = api.register_team_for_season(
                    season2["id"], team_2a["id"], actor_id="admin",
                    league_id=league_2a["id"])
                reg_2b = api.register_team_for_season(
                    season2["id"], team_2b["id"], actor_id="admin",
                    league_id=league_2b["id"])
                self.assertNotIn("error", reg_2a, reg_2a)
                self.assertNotIn("error", reg_2b, reg_2b)
                venue2 = api.create_venue("V2", league_id=program["id"],
                                          actor_id="admin")
                rink2 = api.create_rink(venue2["id"], "R2", actor_id="admin")
                api.create_ice_slot(rink2["id"], "2026-09-02T18:00:00+00:00",
                                    "2026-09-02T19:30:00+00:00", actor_id="admin")
                api.create_ice_slot(rink2["id"], "2026-09-02T20:00:00+00:00",
                                    "2026-09-02T21:30:00+00:00", actor_id="admin")
                api.grant_season_venue_access(season2["id"], venue2["id"],
                                              actor_id="admin")

                def _read(season, league_id):
                    api.set_active_context(
                        "admin", Role.LEAGUE_ADMIN, {}, program["id"],
                        season["id"] if season else None, league_id)
                    return api.get_setup_progress("admin", *ADMIN)

                # --- Season 1 active, No League: union of 1A+1B. ---
                p = _read(season1, None)
                self.assertEqual(
                    _workflow(p, "participation")["detail"],
                    "2 schedulable registration(s)",
                    f"{label}: S1/No-League must show BOTH of S1's own "
                    "registrations, never S2's")
                self.assertEqual(
                    _workflow(p, "facilities")["detail"],
                    "1 available game slot(s)",
                    f"{label}: S1/No-League must show S1's own slot count, "
                    "never S2's 2")
                # "teams" is NOT Season-bound: the full Program+League-union
                # set includes every Season's Teams (4), proving Season
                # selection alone never narrows it.
                self.assertEqual(
                    _workflow(p, "teams")["detail"], "4 team(s)",
                    f"{label}: teams must stay Program-wide across every "
                    "Season, S1 active or not")

                # --- Season 1 active, League 1A selected: narrows further,
                # WITHIN Season 1, to 1A's own registration only. ---
                p = _read(season1, league_1a["id"])
                self.assertEqual(
                    _workflow(p, "participation")["detail"],
                    "1 schedulable registration(s)",
                    f"{label}: S1/League 1A must show ONLY 1A's own "
                    "registration")
                self.assertEqual(
                    _workflow(p, "facilities")["detail"],
                    "1 available game slot(s)",
                    f"{label}: facilities has no League axis -- selecting "
                    "1A must not change S1's count")

                # --- Season 2 active, No League: union of 2A+2B -- proves
                # switching the active Season flips BOTH counts to Season
                # 2's OWN data, not stuck on Season 1's. ---
                p = _read(season2, None)
                self.assertEqual(
                    _workflow(p, "participation")["detail"],
                    "2 schedulable registration(s)",
                    f"{label}: S2/No-League must show BOTH of S2's own "
                    "registrations, never S1's")
                self.assertEqual(
                    _workflow(p, "facilities")["detail"],
                    "2 available game slot(s)",
                    f"{label}: switching to S2 must flip facilities to S2's "
                    "OWN 2 slots, not stay on S1's 1")

                # --- Season 2 active, League 2B selected. ---
                p = _read(season2, league_2b["id"])
                self.assertEqual(
                    _workflow(p, "participation")["detail"],
                    "1 schedulable registration(s)",
                    f"{label}: S2/League 2B must show ONLY 2B's own "
                    "registration")
                self.assertEqual(
                    _workflow(p, "facilities")["detail"],
                    "2 available game slot(s)", label)

                # --- Program-only (no Season resolved): BOTH Season-bound
                # workflows must go EMPTY -- neither Season's real,
                # non-empty data may leak through. ---
                p = _read(None, None)
                participation_none = _workflow(p, "participation")
                self.assertEqual(
                    participation_none["status"], "todo",
                    f"{label}: Program-only must not read participation as "
                    "done from EITHER season")
                self.assertEqual(
                    participation_none["detail"],
                    "No team registered to play yet.",
                    f"{label}: Program-only participation must be empty, "
                    "not fall back to every Season's registrations")
                facilities_none = _workflow(p, "facilities")
                self.assertEqual(
                    facilities_none["status"], "todo",
                    f"{label}: Program-only must not read facilities as "
                    "done from EITHER season")
                self.assertEqual(
                    facilities_none["detail"],
                    "No available game ice slot yet.",
                    f"{label}: Program-only facilities must be empty, not "
                    "fall back to every Season's ice")
                # "league_season" (Program-wide over every Season
                # collectively) and "teams" (no Season field at all) must
                # both stay fully populated and "done" even Program-only --
                # a contrast proof that the ceiling above is Season-
                # SPECIFIC, not an accidental blanket empty-out of the whole
                # response.
                self.assertEqual(
                    _workflow(p, "league_season")["status"], "done",
                    f"{label}: league_season is Program-wide and must stay "
                    "done Program-only")
                self.assertEqual(
                    _workflow(p, "teams")["detail"], "4 team(s)",
                    f"{label}: teams must stay Program-wide and fully "
                    "populated Program-only too")

                # --- Switching back to Season 1/No League reproduces the
                # ORIGINAL counts -- proves no residual state (from Season
                # 2, League selections, or the Program-only read) survived.
                p = _read(season1, None)
                self.assertEqual(_workflow(p, "participation")["detail"],
                                 "2 schedulable registration(s)", label)
                self.assertEqual(_workflow(p, "facilities")["detail"],
                                 "1 available game slot(s)", label)
                _close(store)


class LeagueFilteredSetupProgressStatelessReadTest(unittest.TestCase):
    """A facade-level approximation of the client-side delayed/stale-response
    race guard (properly proven end-to-end at the Playwright/e2e layer):
    since ``get_setup_progress`` is a pure per-call read with no request-level
    ordering of its own, the strongest backend-level guarantee is that it is
    STATELESS — two callers with different active contexts, read in any
    interleaving, never contaminate each other through shared mutable state
    (a memoization cache, a class-level accumulator, etc)."""

    def test_alternating_reads_for_two_users_never_cross_contaminate(self):
        for label, store in _backends():
            with self.subTest(backend=label):
                api = ApiService(store)
                api.create_user_account("admin", "pw", "league_admin")
                program = api.create_program("Prog", actor_id="admin")
                season = api.create_season(program["id"], "Fall", actor_id="admin")
                league_x = api.create_league(season["id"], "League X", actor_id="admin")
                league_y = api.create_league(season["id"], "League Y", actor_id="admin")
                club = api.create_club("C", actor_id="admin")
                api.create_team(club["id"], None, "X1", actor_id="admin",
                                program_id=program["id"], league_id=league_x["id"])
                api.create_team(club["id"], None, "X2", actor_id="admin",
                                program_id=program["id"], league_id=league_x["id"])
                api.create_team(club["id"], None, "Y1", actor_id="admin",
                                program_id=program["id"], league_id=league_y["id"])

                api.set_active_context("u_x", *ADMIN,
                                       program["id"], season["id"], league_x["id"])
                api.set_active_context("u_y", *ADMIN,
                                       program["id"], season["id"], league_y["id"])

                # Alternate reads between two users/contexts many times, in
                # both orders, asserting each call still reflects only ITS
                # OWN user's saved League selection.
                for i in range(6):
                    progress_x = api.get_setup_progress("u_x", *ADMIN)
                    self.assertEqual(_workflow(progress_x, "teams")["detail"],
                                     "2 team(s)", (label, "x", i))
                    progress_y = api.get_setup_progress("u_y", *ADMIN)
                    self.assertEqual(_workflow(progress_y, "teams")["detail"],
                                     "1 team(s)", (label, "y", i))
                    # Reverse order too, to rule out an ordering-dependent leak.
                    progress_y2 = api.get_setup_progress("u_y", *ADMIN)
                    self.assertEqual(_workflow(progress_y2, "teams")["detail"],
                                     "1 team(s)", (label, "y2", i))
                    progress_x2 = api.get_setup_progress("u_x", *ADMIN)
                    self.assertEqual(_workflow(progress_x2, "teams")["detail"],
                                     "2 team(s)", (label, "x2", i))
                _close(store)


class LeagueFilteredSetupProgressViewerNoMutationTest(unittest.TestCase):
    """``get_setup_progress`` is a read; calling it as Viewer must never
    mutate store state. Real mutation-ENDPOINT permission gating for Viewer
    (rejecting a write attempt outright) is enforced at the HTTP/web layer,
    not this facade — this is a lower-level, always-true guarantee: the
    facade method itself performs no writes, whoever calls it."""

    def _snapshot(self, store):
        return (
            [vars(t) for t in store.all_teams()],
            [vars(p) for p in store.all_players()],
            [vars(d) for d in store.all_divisions()],
            [vars(r) for r in store.all_season_team_registrations()],
            [vars(ls) for ls in store.all_league_seasons()],
            [vars(s) for s in store.all_ice_slots()],
            [vars(a) for a in store.all_season_venue_access()],
            [vars(lg) for lg in store.all_leagues()],
            store.get_active_context("viewer1"),
        )

    def test_viewer_read_never_mutates_store_state(self):
        for label, store in _backends():
            with self.subTest(backend=label):
                api = ApiService(store)
                api.create_user_account("admin", "pw", "league_admin")
                program = api.create_program("Prog", actor_id="admin")
                season = api.create_season(program["id"], "Fall", actor_id="admin")
                league_x = api.create_league(season["id"], "League X", actor_id="admin")
                league_y = api.create_league(season["id"], "League Y", actor_id="admin")
                club = api.create_club("C", actor_id="admin")
                team_x = api.create_team(club["id"], None, "X1", actor_id="admin",
                                         program_id=program["id"], league_id=league_x["id"])
                api.create_team(club["id"], None, "Y1", actor_id="admin",
                                program_id=program["id"], league_id=league_y["id"])
                api.create_player(team_x["id"], "P", "forward", actor_id="admin")
                reg = api.register_team_for_season(
                    season["id"], team_x["id"], actor_id="admin",
                    league_id=league_x["id"])
                self.assertNotIn("error", reg, reg)
                venue = api.create_venue("V", league_id=program["id"], actor_id="admin")
                rink = api.create_rink(venue["id"], "R", actor_id="admin")
                api.create_ice_slot(rink["id"], "2026-09-01T18:30:00+00:00",
                                    "2026-09-01T20:00:00+00:00", actor_id="admin")
                api.grant_season_venue_access(season["id"], venue["id"], actor_id="admin")

                # For each League selection: the context SELECTION itself
                # (``set_active_context``) is a legitimate write and stays
                # OUTSIDE the snapshotted window -- only the repeated
                # ``get_setup_progress`` READS in between are asserted to
                # mutate nothing, across every League selection this file's
                # other tests prove narrows for League Admin.
                for league_id in (league_x["id"], league_y["id"], None):
                    api.set_active_context("viewer1", *VIEWER,
                                           program["id"], season["id"], league_id)
                    before = self._snapshot(store)
                    for _ in range(3):
                        progress = api.get_setup_progress("viewer1", *VIEWER)
                        self.assertEqual(progress["workflows"], [],
                                         (label, league_id))
                    after = self._snapshot(store)
                    self.assertEqual(
                        before, after,
                        f"{label}/{league_id}: a Viewer read mutated store state")
                _close(store)


if __name__ == "__main__":
    unittest.main()
