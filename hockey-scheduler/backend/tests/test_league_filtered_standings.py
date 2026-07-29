"""Regression tests for ``get_standings``'s Program/Season/League-ownership
check (#367, corrected by #369).

#367 originally required only that the Division's owning Program be a member
of the caller's full ``authorized_program_ids`` set (ANY Program the caller
was authorized for anywhere). A #369 review round flagged the real leak that
left open: a global League Admin (or Arena Manager, or Viewer) active in
Program A/Season A/League A could still request a Division from Program B,
a different Season of the SAME Program, or a different League of the SAME
Program+Season, and receive its real standings -- just by being globally
authorized somewhere.

#369 corrects this: ``get_standings`` now resolves the caller's ACTIVE tuple
via ``ContextService.resolve_with_league`` -- ``(program, season, league)`` --
and requires the requested Division's own validated
``LeagueSeason -> Season -> Program`` chain to match THAT SPECIFIC ACTIVE
TUPLE, not merely be some Program/Season/League the caller is broadly
authorized for:

  * ``program`` must not be None, and the Division's resolved Season's
    ``program_id`` must equal ``program.id``;
  * if ``season`` is not None (a specific Season IS currently active), the
    Division's own ``LeagueSeason.season_id`` must equal ``season.id``
    exactly -- a Division in a DIFFERENT Season of the SAME Program is
    REJECTED;
  * if ``league`` is not None (a League IS currently selected), the
    Division's own ``LeagueSeason.league_id`` must equal ``league.id`` --
    a Division under a different League of the SAME Program+Season is
    REJECTED.

Any mismatch (missing Division, dangling/unbound ``league_season_id``, wrong
Program, wrong Season when one is active, wrong League when one is active)
collapses to the SAME generic empty shape (``{"division_id": ...,
"standings": []}``) a nonexistent ``division_id`` already returns --
deliberately non-oracle, so an inaccessible-from-here Division looks
identical to one that doesn't exist at all.

Called with no role at all (the historical 1-arg shape), no ownership check
runs -- unchanged pre-#367 behavior, since dozens of existing direct/internal
call sites across the suite rely on the default.

Coverage:
  * the no-role legacy call performs no ownership check at all (sanity);
  * a Coach/Player-scoped caller (one Program only) is rejected for a
    foreign Program's division with the exact same shape a nonexistent id
    produces -- proven by direct comparison, not just "no error";
  * that SAME caller's own Program's division still returns REAL computed
    standings (positive control -- the check isn't rejecting everyone);
  * a global role (League Admin) pinned to a SPECIFIC active Program via
    ``set_active_context`` is REJECTED for a different Program it is only
    abstractly/globally authorized for -- the #369 correction's core proof,
    contrasted with the same rejection a scoped Coach already gets;
  * the full #369 negative matrix -- cross-Program, cross-Season,
    cross-League, deleted, unbound, revoked-caller-authorization, archived
    (non-active), and unauthorized -- each collapsing to the identical
    generic empty shape, on Memory/SQLite/PostgreSQL stores and over real
    authenticated HTTP, plus the positive controls (own active Division, and
    an ARCHIVED Season honored as read-only history when it IS the
    explicitly-selected active Season);
  * every supported role (League Admin, Coach, Player, Guardian, Official,
    Arena Manager, Viewer) is bound to its own active tuple the same way;
  * the real HTTP route: 401 signed-out (previously fully unauthenticated),
    200 real standings for a session whose active tuple matches, 200 generic
    empty shape (never 403/404) for one that doesn't.
"""

import copy
import json
import os
import threading
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timezone
from http.cookiejar import CookieJar
from http.server import ThreadingHTTPServer

from helpers import BACKEND, fresh_sql_store  # noqa: F401  (ensures sys.path is set up)

from hockey_scheduler.api import ApiService
from hockey_scheduler.domain import GuardianLink, OfficialRole, Role
from hockey_scheduler.domain.setup_models import OfficialAssignment
from hockey_scheduler.store import InMemoryStore, SqlStore

_TS = datetime(2026, 7, 1, 9, 0, 0, tzinfo=timezone.utc)


def _backends():
    """Memory/SQLite always; PostgreSQL only when ``TEST_DATABASE_URL`` is set
    -- the exact idiom ``test_active_context_league.py`` uses so the #369
    negative matrix runs on every real backend, not just Memory."""
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


def _build_scored_division(api, tag, actor_id=None):
    """A minimal Program -> Season -> League -> Division with two permanent
    Teams (each's real competition ``Team.league_id`` bound to that League,
    per the #367 vocabulary landmine -- never the ``get_demo_overview`` team
    row's ``league_id``, which means Program there instead), registered into
    the Season under that League, with one FINAL (approved) result recorded
    so the Division's standings carry real, non-empty numbers (home wins
    4-2: home w=1/pts=2/gd=+2, away l=1/pts=0/gd=-2).

    Mirrors the quickest fixture shape in test_results.py (ResultsTest),
    built through the ApiService facade per #367's required fixture
    signatures rather than the lower-level SetupService that file uses
    directly.
    """
    program = api.create_program(f"Program {tag}", actor_id=actor_id)
    assert "error" not in program, program
    season = api.create_season(program["id"], "Season", actor_id=actor_id)
    assert "error" not in season, season
    league = api.create_league(season["id"], f"League {tag}", actor_id=actor_id)
    assert "error" not in league, league
    division = api.create_division(season["id"], f"Division {tag}",
                                   league_id=league["id"], actor_id=actor_id)
    assert "error" not in division, division
    club = api.create_club(f"Club {tag}", actor_id=actor_id)
    assert "error" not in club, club
    home = api.create_team(club["id"], None, f"{tag} Home", actor_id=actor_id,
                           program_id=program["id"], league_id=league["id"])
    assert "error" not in home, home
    away = api.create_team(club["id"], None, f"{tag} Away", actor_id=actor_id,
                           program_id=program["id"], league_id=league["id"])
    assert "error" not in away, away
    for team in (home, away):
        reg = api.register_team_for_season(season["id"], team["id"],
                                           division["id"], actor_id=actor_id,
                                           league_id=league["id"])
        assert "error" not in reg, reg
    venue = api.create_venue(f"Venue {tag}", league_id=program["id"],
                             actor_id=actor_id)
    assert "error" not in venue, venue
    rink = api.create_rink(venue["id"], f"Rink {tag}", actor_id=actor_id)
    assert "error" not in rink, rink
    access = api.grant_season_venue_access(season["id"], venue["id"],
                                           actor_id=actor_id)
    assert "error" not in access, access
    slot = api.create_ice_slot(rink["id"], "2026-09-01T18:00:00+00:00",
                               "2026-09-01T20:00:00+00:00", actor_id=actor_id)
    assert "error" not in slot, slot
    game = api.create_game(season["id"], division["id"], home["id"], away["id"],
                           slot["id"], league_id=league["id"], actor_id=actor_id)
    assert "error" not in game, game
    rec = api.record_result(game["id"], 4, 2, actor_id=actor_id)
    assert "error" not in rec, rec
    approved = api.approve_result(game["id"], actor_id=actor_id)
    assert "error" not in approved, approved
    return {"program": program, "season": season, "league": league,
            "division": division, "club": club, "home": home, "away": away,
            "game": game}


def _make_program(api, tag, actor_id=None):
    """A bare Program + Club + Venue/Rink, shared by every Season/League/
    Division built under it in ``_build_matrix_fixture``."""
    program = api.create_program(f"Program {tag}", actor_id=actor_id)
    assert "error" not in program, program
    club = api.create_club(f"Club {tag}", actor_id=actor_id)
    assert "error" not in club, club
    venue = api.create_venue(f"Venue {tag}", league_id=program["id"],
                             actor_id=actor_id)
    assert "error" not in venue, venue
    rink = api.create_rink(venue["id"], f"Rink {tag}", actor_id=actor_id)
    assert "error" not in rink, rink
    return program, club, venue, rink


def _scored_division_in(api, season_id, league_id, club_id, rink_id, tag,
                        slot_date, actor_id=None):
    """A Division under an EXISTING ``(season_id, league_id)`` with two
    permanent Teams and one FINAL result -- so its standings carry real,
    non-empty numbers whenever it IS reachable. A gate that fails to reject
    a negative case built this way surfaces as NON-empty standings, never a
    coincidentally-empty table from a division with no teams/games at all."""
    division = api.create_division(season_id, f"Division {tag}",
                                   league_id=league_id, actor_id=actor_id)
    assert "error" not in division, division
    program_id = api.store.get_season(season_id).program_id
    home = api.create_team(club_id, None, f"{tag} Home", actor_id=actor_id,
                           program_id=program_id, league_id=league_id)
    assert "error" not in home, home
    away = api.create_team(club_id, None, f"{tag} Away", actor_id=actor_id,
                           program_id=program_id, league_id=league_id)
    assert "error" not in away, away
    for team in (home, away):
        reg = api.register_team_for_season(season_id, team["id"], division["id"],
                                           actor_id=actor_id, league_id=league_id)
        assert "error" not in reg, reg
    slot = api.create_ice_slot(rink_id, f"{slot_date}T18:00:00+00:00",
                               f"{slot_date}T20:00:00+00:00", actor_id=actor_id)
    assert "error" not in slot, slot
    game = api.create_game(season_id, division["id"], home["id"], away["id"],
                           slot["id"], league_id=league_id, actor_id=actor_id)
    assert "error" not in game, game
    rec = api.record_result(game["id"], 4, 2, actor_id=actor_id)
    assert "error" not in rec, rec
    approved = api.approve_result(game["id"], actor_id=actor_id)
    assert "error" not in approved, approved
    return {"division": division, "home": home, "away": away, "game": game}


def _assign_official_to_game(store, official_id, game):
    """An Official assignment against an EXISTING (well-formed, real)
    REGULAR Game -- the store-level effect of an authorized assignment,
    without going through the scheduling API's own assignment workflow.
    Returns the assignment id, so a test can later revoke it directly."""
    assignment_id = store.next_id("assign")
    with store.transaction():
        store.add_official_assignment(OfficialAssignment(
            id=assignment_id, game_id=game["id"], official_id=official_id,
            role=OfficialRole.REFEREE))
    return assignment_id


def _build_matrix_fixture(api, actor_id=None):
    """Two Programs -- A (the caller's eventual ACTIVE Program) and B
    (foreign) -- with Program A carrying THREE Seasons and several Leagues,
    enough concrete Division ids to exercise the full #369 negative matrix
    against ONE active tuple, Program A / Season A1 / League Aa:

      active         -- Program A / Season A1 / League Aa (the tuple itself)
      other_league   -- Program A / Season A1 / League Ab   (cross-League)
      other_season   -- Program A / Season A2 / League Aa2  (cross-Season)
      archived       -- Program A / Season A3 / League A3a, A3 ARCHIVED
                        after being fully built (archived, non-active)
      deleted        -- built under the ACTIVE tuple, then hard-deleted
      unbound        -- built under a dedicated League, then its
                        LeagueSeason binding is removed (dangling
                        ``league_season_id``)
      foreign        -- Program B / Season B1 / League Ba (cross-Program)

    Every Division is "scored" (real registered Teams + one FINAL result),
    so a gate that fails to reject a negative case shows up as non-empty
    standings rather than a coincidentally-empty table.
    """
    program_a, club_a, venue_a, rink_a = _make_program(api, "A", actor_id)
    pid_a = program_a["id"]

    season_a1 = api.create_season(pid_a, "A1", actor_id=actor_id)
    assert "error" not in season_a1, season_a1
    access = api.grant_season_venue_access(season_a1["id"], venue_a["id"],
                                           actor_id=actor_id)
    assert "error" not in access, access
    league_aa = api.create_league(season_a1["id"], "Aa", actor_id=actor_id)
    league_ab = api.create_league(season_a1["id"], "Ab", actor_id=actor_id)
    active = _scored_division_in(api, season_a1["id"], league_aa["id"],
                                 club_a["id"], rink_a["id"], "Active",
                                 "2026-09-01", actor_id=actor_id)
    other_league = _scored_division_in(api, season_a1["id"], league_ab["id"],
                                       club_a["id"], rink_a["id"], "OtherLeague",
                                       "2026-09-02", actor_id=actor_id)

    # -- deleted: built under the ACTIVE tuple itself, then hard-deleted -----
    deleted = _scored_division_in(api, season_a1["id"], league_aa["id"],
                                  club_a["id"], rink_a["id"], "Deleted",
                                  "2026-09-03", actor_id=actor_id)
    with api.store.transaction():
        api.store.delete_division(deleted["division"]["id"])

    # -- unbound: its LeagueSeason binding is removed, leaving a dangling
    # ``league_season_id`` on the Division row itself -------------------------
    league_unbound = api.create_league(season_a1["id"], "Unbound",
                                       actor_id=actor_id)
    unbound = _scored_division_in(api, season_a1["id"], league_unbound["id"],
                                  club_a["id"], rink_a["id"], "Unbound",
                                  "2026-09-04", actor_id=actor_id)
    ls_unbound = api.store.league_season_for(league_unbound["id"], season_a1["id"])
    with api.store.transaction():
        api.store.delete_league_season(ls_unbound.id)

    season_a2 = api.create_season(pid_a, "A2", actor_id=actor_id)
    assert "error" not in season_a2, season_a2
    access = api.grant_season_venue_access(season_a2["id"], venue_a["id"],
                                           actor_id=actor_id)
    assert "error" not in access, access
    league_aa2 = api.create_league(season_a2["id"], "Aa2", actor_id=actor_id)
    other_season = _scored_division_in(api, season_a2["id"], league_aa2["id"],
                                       club_a["id"], rink_a["id"], "OtherSeason",
                                       "2026-09-05", actor_id=actor_id)

    season_a3 = api.create_season(pid_a, "A3", actor_id=actor_id)
    assert "error" not in season_a3, season_a3
    access = api.grant_season_venue_access(season_a3["id"], venue_a["id"],
                                           actor_id=actor_id)
    assert "error" not in access, access
    league_a3a = api.create_league(season_a3["id"], "A3a", actor_id=actor_id)
    archived = _scored_division_in(api, season_a3["id"], league_a3a["id"],
                                   club_a["id"], rink_a["id"], "Archived",
                                   "2026-09-06", actor_id=actor_id)
    archived_season = api.archive_season(season_a3["id"], actor_id=actor_id)
    assert "error" not in archived_season, archived_season

    program_b, club_b, venue_b, rink_b = _make_program(api, "B", actor_id)
    pid_b = program_b["id"]
    season_b1 = api.create_season(pid_b, "B1", actor_id=actor_id)
    assert "error" not in season_b1, season_b1
    access = api.grant_season_venue_access(season_b1["id"], venue_b["id"],
                                           actor_id=actor_id)
    assert "error" not in access, access
    league_ba = api.create_league(season_b1["id"], "Ba", actor_id=actor_id)
    foreign = _scored_division_in(api, season_b1["id"], league_ba["id"],
                                  club_b["id"], rink_b["id"], "Foreign",
                                  "2026-09-07", actor_id=actor_id)

    return {
        "program_a": program_a, "season_a1": season_a1, "season_a2": season_a2,
        "season_a3": season_a3, "league_aa": league_aa, "league_ab": league_ab,
        "league_aa2": league_aa2, "league_a3a": league_a3a,
        "program_b": program_b, "season_b1": season_b1, "league_ba": league_ba,
        "active": active, "other_league": other_league,
        "other_season": other_season, "archived": archived, "deleted": deleted,
        "unbound": unbound, "foreign": foreign,
    }


class StandingsAuthorizationComputationTest(unittest.TestCase):
    """Facade-level, Memory-backed -- mirrors SetupProgressComputationTest's
    scope in test_setup_progress.py (a pure read composed from store methods
    that already carry their own Memory/SQLite/PostgreSQL parity coverage
    elsewhere, so one backend suffices for the authorization logic itself)."""

    def _api(self):
        return ApiService(InMemoryStore())

    def test_legacy_call_with_no_role_performs_no_ownership_check(self):
        """The historical 1-arg shape (``user_id``/``role``/``scope`` all
        omitted, defaulting to None) is exactly pre-#367 behavior: no
        ownership check runs at all. Many pre-existing direct call sites
        across the suite (e.g. test_results.py) rely on this default."""
        api = self._api()
        fixture = _build_scored_division(api, "B")

        result = api.get_standings(fixture["division"]["id"])

        self.assertNotIn("error", result, result)
        self.assertEqual(result["division_id"], fixture["division"]["id"])
        self.assertTrue(result["standings"], result)
        names = {row["team_name"] for row in result["standings"]}
        self.assertEqual(names, {fixture["home"]["name"], fixture["away"]["name"]})

    def test_foreign_program_division_is_indistinguishable_from_nonexistent(self):
        """The core #367 regression: a Coach scoped to Program A's own team
        requesting Program B's division must get the SAME generic empty
        shape a nonexistent division_id already returns -- compared directly,
        not just checked for "no error", so a response-content-only
        attacker gains no signal distinguishing the two cases."""
        api = self._api()
        fixture_a = _build_scored_division(api, "A")
        fixture_b = _build_scored_division(api, "B")
        coach_scope = {"team_id": fixture_a["home"]["id"]}

        foreign = api.get_standings(fixture_b["division"]["id"], "coach-1",
                                    Role.COACH, coach_scope)
        nonexistent = api.get_standings("division_does_not_exist", "coach-1",
                                        Role.COACH, coach_scope)

        self.assertEqual(foreign, {"division_id": fixture_b["division"]["id"],
                                   "standings": []})
        self.assertEqual(nonexistent,
                         {"division_id": "division_does_not_exist",
                          "standings": []})
        # Identical shape -- the only difference is each echoing back its
        # OWN queried division_id, which is not itself a signal about
        # whether that id ever existed.
        self.assertEqual(set(foreign), set(nonexistent))
        self.assertEqual(foreign["standings"], nonexistent["standings"])

    def test_own_program_division_returns_real_standings_positive_control(self):
        """Positive control: the SAME Coach requesting THEIR OWN Program's
        division gets the REAL computed standings, not the empty shape --
        proving the check isn't accidentally rejecting everyone."""
        api = self._api()
        fixture_a = _build_scored_division(api, "A")
        coach_scope = {"team_id": fixture_a["home"]["id"]}

        own = api.get_standings(fixture_a["division"]["id"], "coach-1",
                                Role.COACH, coach_scope)

        self.assertNotIn("error", own, own)
        self.assertEqual(own["division_id"], fixture_a["division"]["id"])
        self.assertNotEqual(own["standings"], [],
                           "the caller's own Program must not be rejected")
        home_row = next(row for row in own["standings"]
                        if row["team_name"] == fixture_a["home"]["name"])
        self.assertEqual(home_row["w"], 1)
        self.assertEqual(home_row["pts"], 2)
        self.assertEqual(home_row["gd"], 2)
        away_row = next(row for row in own["standings"]
                        if row["team_name"] == fixture_a["away"]["name"])
        self.assertEqual(away_row["l"], 1)
        self.assertEqual(away_row["pts"], 0)

    def test_global_role_is_bound_to_its_active_tuple_not_any_authorized_program(self):
        """#369's core correction, replacing the OLD (now-wrong) assertion
        that a global League Admin reaches ANY authorized Program regardless
        of its active context.

        League Admin is a "global" role (``context_scope._GLOBAL_ROLES``) --
        authorized for EVERY Program in the abstract. Pinning the active
        context at Program A via ``set_active_context`` and then requesting
        Program B's standings must now FAIL, exactly like a Coach scoped to
        Program A's own team requesting that same foreign Program B -- the
        gate is the caller's ACTIVE resolved tuple, never merely "any
        Program I am broadly authorized for somewhere". The Admin's OWN
        active Program's division still returns real standings, so this
        isn't rejecting everyone either."""
        api = self._api()
        fixture_a = _build_scored_division(api, "A")
        fixture_b = _build_scored_division(api, "B")

        api.set_active_context("admin-1", Role.LEAGUE_ADMIN, {},
                               fixture_a["program"]["id"],
                               fixture_a["season"]["id"],
                               fixture_a["league"]["id"])

        admin_own = api.get_standings(fixture_a["division"]["id"], "admin-1",
                                      Role.LEAGUE_ADMIN, {})
        self.assertNotIn("error", admin_own, admin_own)
        self.assertNotEqual(
            admin_own["standings"], [],
            "League Admin's own ACTIVE division must not be rejected")

        admin_b = api.get_standings(fixture_b["division"]["id"], "admin-1",
                                    Role.LEAGUE_ADMIN, {})
        self.assertEqual(
            admin_b, {"division_id": fixture_b["division"]["id"],
                     "standings": []},
            "League Admin is GLOBALLY authorized for Program B, but its "
            "ACTIVE tuple is Program A -- #369 requires it to be bound to "
            "the active tuple, not any Program it is broadly authorized for")

        coach_scope = {"team_id": fixture_a["home"]["id"]}
        coach_b = api.get_standings(fixture_b["division"]["id"], "coach-1",
                                    Role.COACH, coach_scope)
        self.assertEqual(coach_b, {"division_id": fixture_b["division"]["id"],
                                   "standings": []},
                        "a Coach scoped to Program A only must be rejected "
                        "for Program B, exactly like the global Admin above")


class StandingsActiveContextMatrixTest(unittest.TestCase):
    """The full #369 negative matrix against ONE global admin active tuple,
    on Memory/SQLite/PostgreSQL (``_backends()``): cross-Program,
    cross-Season, cross-League, deleted, unbound, and archived-non-active
    Division ids must ALL collapse to the identical generic empty shape,
    while the active Division itself -- and an ARCHIVED Season explicitly
    selected as the active one -- still return real standings."""

    def test_admin_active_tuple_gates_every_negative_case(self):
        for label, store in _backends():
            with self.subTest(backend=label):
                api = ApiService(store)
                fixture = _build_matrix_fixture(api)
                admin = ("admin-1", Role.LEAGUE_ADMIN, {})
                pid_a = fixture["program_a"]["id"]

                set_result = api.set_active_context(
                    admin[0], admin[1], admin[2], pid_a,
                    fixture["season_a1"]["id"], fixture["league_aa"]["id"])
                self.assertNotIn("error", set_result, set_result)

                active_id = fixture["active"]["division"]["id"]
                positive = api.get_standings(active_id, *admin)
                self.assertNotIn("error", positive, positive)
                self.assertNotEqual(
                    positive["standings"], [],
                    f"{label}: the admin's own active Division was rejected")

                negative_cases = {
                    "cross_program": fixture["foreign"]["division"]["id"],
                    "cross_season": fixture["other_season"]["division"]["id"],
                    "cross_league": fixture["other_league"]["division"]["id"],
                    "deleted": fixture["deleted"]["division"]["id"],
                    "unbound": fixture["unbound"]["division"]["id"],
                    "archived_non_active": fixture["archived"]["division"]["id"],
                }
                # #369 self-audit: the response-shape check alone is VACUOUS
                # for `deleted` and `unbound`. Deleting a Division (or its
                # LeagueSeason binding) leaves the registration lookup
                # resolving to nothing, so those two ids return the empty
                # shape even with the gate removed entirely -- mutation
                # confirmed dropping the chain-missing clause left this file
                # green. So assert the GATE ITSELF rejects each id, which is
                # the actual claim ("the requested division's validated
                # LeagueSeason -> Season -> Program chain must match"), and
                # keep the shape check as the non-disclosure half.
                program, season, league = api.context.resolve_with_league(*admin)
                for case, div_id in negative_cases.items():
                    with self.subTest(backend=label, case=case):
                        self.assertFalse(
                            api._division_matches_active_context(
                                div_id, program, season, league),
                            f"{label}/{case}: the active-context gate ACCEPTED "
                            f"this division -- any empty result is then "
                            f"incidental, not enforced")
                        result = api.get_standings(div_id, *admin)
                        self.assertEqual(
                            result, {"division_id": div_id, "standings": []},
                            f"{label}/{case}: must return the identical "
                            f"generic empty shape, never a distinguishable "
                            f"error or partial disclosure")
                # Control: the gate ACCEPTS the active Division, so the
                # assertions above cannot pass by rejecting everything.
                self.assertTrue(
                    api._division_matches_active_context(
                        fixture["active"]["division"]["id"], program, season,
                        league),
                    f"{label}: the gate rejected the admin's OWN active "
                    f"Division -- the negative assertions are meaningless")

                # Positive: an ARCHIVED Season explicitly SELECTED as the
                # active tuple is honored as read-only history -- it is not
                # itself a rejection reason, only being NON-active is.
                set_result = api.set_active_context(
                    admin[0], admin[1], admin[2], pid_a,
                    fixture["season_a3"]["id"], fixture["league_a3a"]["id"])
                self.assertNotIn("error", set_result, set_result)
                archived_id = fixture["archived"]["division"]["id"]
                archived_positive = api.get_standings(archived_id, *admin)
                self.assertNotIn("error", archived_positive, archived_positive)
                self.assertNotEqual(
                    archived_positive["standings"], [],
                    f"{label}: an explicitly-selected ARCHIVED Season must "
                    f"still resolve its own real standings")
                _close(store)

    def test_unauthorized_scoped_role_cannot_reach_a_program_it_has_no_authorization_for(self):
        """The "unauthorized" case, distinct from "cross_program" above: a
        Coach scoped to Program A's own team has NO authorization for
        Program B at all (unlike the global Admin, which IS broadly
        authorized for B in the abstract but merely isn't active there)."""
        for label, store in _backends():
            with self.subTest(backend=label):
                api = ApiService(store)
                fixture = _build_matrix_fixture(api)
                coach = ("coach-1", Role.COACH,
                        {"team_id": fixture["active"]["home"]["id"]})
                api.set_active_context(
                    coach[0], coach[1], coach[2], fixture["program_a"]["id"],
                    fixture["season_a1"]["id"], fixture["league_aa"]["id"])

                foreign_id = fixture["foreign"]["division"]["id"]
                result = api.get_standings(foreign_id, *coach)
                self.assertEqual(
                    result, {"division_id": foreign_id, "standings": []},
                    label)
                _close(store)

    def test_revoked_official_assignment_loses_access_it_previously_had(self):
        """The "revoked" case: the CALLER's own authorization is revoked (an
        Official's assignment is unassigned) rather than the Division itself
        changing -- a Division that WAS reachable is no longer, once the
        assignment that granted it is gone."""
        for label, store in _backends():
            with self.subTest(backend=label):
                api = ApiService(store)
                fixture = _build_matrix_fixture(api)
                official_id = api.create_official("Ref")["id"]
                assignment_id = _assign_official_to_game(
                    store, official_id, fixture["active"]["game"])
                official = ("official-1", Role.OFFICIAL,
                           {"official_id": official_id})
                api.set_active_context(
                    official[0], official[1], official[2],
                    fixture["program_a"]["id"], fixture["season_a1"]["id"],
                    fixture["league_aa"]["id"])

                active_id = fixture["active"]["division"]["id"]
                before = api.get_standings(active_id, *official)
                self.assertNotIn("error", before, before)
                self.assertNotEqual(
                    before["standings"], [],
                    f"{label}: the official could not reach its own "
                    f"assigned Division before revocation")

                store.remove_official_assignment(assignment_id)

                after = api.get_standings(active_id, *official)
                self.assertEqual(
                    after, {"division_id": active_id, "standings": []},
                    f"{label}: revoking the official's only assignment must "
                    f"revoke its access to that Division's standings too")
                _close(store)


class StandingsRoleMatrixTest(unittest.TestCase):
    """Every supported role, on every backend.

    #369 self-audit: this was Memory-only, justified by pointing at
    test_active_context_league.py's LeagueScopeMatrixTest for store parity of
    the role-scope computation. That covers how a role RESOLVES, not how
    #369's active-tuple gate composes with it, which is what this file exists
    to prove -- and the review asks for the seven roles across Memory, SQLite
    and PostgreSQL in as many words. Note the roles here all use scopes that
    genuinely RESOLVE: a role "covered" by an identity that silently resolves
    to nothing proves only that the empty case is empty.
    """

    def test_every_role_is_bound_to_its_active_tuple_and_rejects_a_foreign_program(self):
        for backend, store in _backends():
            with self.subTest(backend=backend):
                self._role_matrix(ApiService(store))
                _close(store)

    def _role_matrix(self, api):
        fixture = _build_matrix_fixture(api)
        pid = fixture["program_a"]["id"]
        sid = fixture["season_a1"]["id"]
        lid = fixture["league_aa"]["id"]
        own_division_id = fixture["active"]["division"]["id"]
        foreign_division_id = fixture["foreign"]["division"]["id"]
        home_team_id = fixture["active"]["home"]["id"]

        player_id = api.create_player(team_id=home_team_id, name="Kid",
                                      position="forward")["id"]
        official_id = api.create_official("Ref")["id"]
        _assign_official_to_game(api.store, official_id,
                                 fixture["active"]["game"])
        guardian_user = "guardian-1"
        with api.store.transaction():
            api.store.add_guardian_link(GuardianLink(
                id=api.store.next_id("glink"), guardian_user_id=guardian_user,
                player_id=player_id, created_at=_TS, verified=True))

        roles = {
            "league_admin": (Role.LEAGUE_ADMIN, {}, "admin-1"),
            "arena_manager": (Role.ARENA_MANAGER, {}, "arena-1"),
            "viewer": (Role.VIEWER, {}, "viewer-1"),
            "coach": (Role.COACH, {"team_id": home_team_id}, "coach-1"),
            "player": (Role.PLAYER, {"player_id": player_id}, "player-1"),
            "official": (Role.OFFICIAL, {"official_id": official_id},
                        "official-1"),
            "guardian": (Role.GUARDIAN, {}, guardian_user),
        }

        for label, (role, scope, user_id) in roles.items():
            with self.subTest(role=label):
                set_result = api.set_active_context(
                    user_id, role, scope, pid, sid, lid)
                self.assertNotIn("error", set_result, (label, set_result))

                own = api.get_standings(own_division_id, user_id, role, scope)
                self.assertNotIn("error", own, (label, own))
                self.assertNotEqual(
                    own["standings"], [],
                    f"{label}: rejected its own active Division")

                foreign = api.get_standings(foreign_division_id, user_id,
                                            role, scope)
                self.assertEqual(
                    foreign, {"division_id": foreign_division_id,
                             "standings": []},
                    f"{label}: a foreign Program disclosed standings -- "
                    f"global roles must still be bound to their ACTIVE "
                    f"tuple, and scoped roles were never authorized for it "
                    f"at all")


class StandingsReadOnlyMutationTest(unittest.TestCase):
    """``get_standings`` is a pure read (#369): a Viewer's request must never
    mutate ANY store state, checked here via a before/after deep-copy
    snapshot of every collection the read touches. Mutation-GATING for a
    Viewer's write endpoints is enforced at the HTTP/web layer (``authorize``/
    ``scope_violation`` in ``web/server.py``), not here -- this is only a
    facade-level sanity check that the read path itself has no side effect
    to gate in the first place."""

    def test_viewer_read_does_not_mutate_the_store(self):
        api = ApiService(InMemoryStore())
        fixture = _build_scored_division(api, "RO")
        api.set_active_context("viewer-1", Role.VIEWER, {},
                               fixture["program"]["id"], fixture["season"]["id"])
        store = api.store

        def snapshot():
            return copy.deepcopy({
                "games": store.games, "teams": store.teams,
                "divisions": store.divisions,
                "season_team_registrations": store.season_team_registrations,
                "game_results": store.game_results,
                "user_active_context": store.user_active_context,
            })

        before = snapshot()
        result = api.get_standings(fixture["division"]["id"], "viewer-1",
                                   Role.VIEWER, {})
        self.assertNotIn("error", result, result)
        self.assertNotEqual(result["standings"], [])
        after = snapshot()
        self.assertEqual(before, after,
                         "get_standings must never mutate store state")


class StandingsAuthorizationHttpTest(unittest.TestCase):
    """Route/authz contract over real HTTP -- mirrors test_setup_progress.py's
    SetupProgressHttpTest harness. The demo seed (STATE.reset()) provisions
    the standard admin/arena/coach accounts this reuses; "coach" is scoped
    to a team in the demo's OWN seeded Program, distinct from any fresh
    Program built per-test via ``_build_scored_division``/
    ``_build_matrix_fixture``."""

    @classmethod
    def setUpClass(cls):
        srv = __import__("hockey_scheduler.web.server", fromlist=["x"])
        cls.srv = srv
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), srv.Handler)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.thread.join(timeout=5)

    def setUp(self):
        # Per-test reset (not just once per class): a clean full demo seed
        # (including the admin/arena/coach accounts) so no test's fixture
        # writes leak into the next one.
        self.srv.STATE.reset()

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
        status, body = self._req(c, "POST", "/api/auth/login",
                                 {"username": username, "password": "demo"})
        self.assertEqual(status, 200, body)
        return c

    def _set_context(self, opener, program_id, season_id, league_id=None):
        status, body = self._req(opener, "POST", "/api/context", {
            "program_id": program_id, "season_id": season_id,
            "league_id": league_id})
        self.assertEqual(status, 200, body)
        return body

    def test_requires_signed_in_session(self):
        """This route was previously completely unauthenticated -- a bare
        GET with no session must now be rejected, not silently computed."""
        fixture = _build_scored_division(self.srv.STATE.api, "HTTP")
        c = self._client()
        status, _ = self._req(c, "GET",
                              f"/api/standings/{fixture['division']['id']}")
        self.assertEqual(status, 401)

    def test_authorized_session_with_matching_active_context_returns_real_standings(self):
        """League Admin is a global role, authorized for every Program
        including one freshly built mid-test -- but #369 requires its ACTIVE
        tuple to actually be pinned there via ``POST /api/context`` first;
        the default fallback is not guaranteed to land on a Program built
        after the demo's own seed."""
        fixture = _build_scored_division(self.srv.STATE.api, "HTTP")
        admin = self._login("admin")
        self._set_context(admin, fixture["program"]["id"],
                          fixture["season"]["id"], fixture["league"]["id"])

        status, resp = self._req(
            admin, "GET", f"/api/standings/{fixture['division']['id']}")

        self.assertEqual(status, 200, resp)
        self.assertNotEqual(resp["standings"], [])
        names = {row["team_name"] for row in resp["standings"]}
        self.assertEqual(names, {fixture["home"]["name"], fixture["away"]["name"]})

    def test_unauthorized_session_returns_generic_empty_shape_not_403_or_404(self):
        """A real session for a role NOT authorized for this Program (the
        seeded "coach" persona, scoped to a team in the demo's own separate
        Program) must get 200 with the SAME generic empty-standings shape a
        nonexistent division_id returns -- never 403/404, which would leak
        "this division exists, you're just not allowed" to an unauthorized
        caller. Compared directly against the nonexistent-id response too,
        the same non-oracle proof as the facade-level test, over HTTP."""
        fixture = _build_scored_division(self.srv.STATE.api, "HTTP")
        coach = self._login("coach")

        status, resp = self._req(
            coach, "GET", f"/api/standings/{fixture['division']['id']}")
        self.assertEqual(status, 200, resp)
        self.assertEqual(resp, {"division_id": fixture["division"]["id"],
                               "standings": []}, resp)

        status_missing, resp_missing = self._req(
            coach, "GET", "/api/standings/division_does_not_exist")
        self.assertEqual(status_missing, 200, resp_missing)
        self.assertEqual(set(resp), set(resp_missing))
        self.assertEqual(resp["standings"], resp_missing["standings"])

    def test_admin_active_tuple_gates_every_negative_case_over_http(self):
        """The #369 required regression, over real authenticated HTTP: for a
        global League Admin session pinned to Program A / Season A1 /
        League Aa, cross-Program, cross-Season, cross-League, deleted,
        unbound, and archived-non-active Division ids all collapse to the
        identical generic empty shape -- never 403/404 -- while the active
        Division, and an ARCHIVED Season explicitly selected as active,
        still return real standings."""
        fixture = _build_matrix_fixture(self.srv.STATE.api)
        admin = self._login("admin")
        self._set_context(admin, fixture["program_a"]["id"],
                          fixture["season_a1"]["id"], fixture["league_aa"]["id"])

        active_id = fixture["active"]["division"]["id"]
        status, resp = self._req(admin, "GET", f"/api/standings/{active_id}")
        self.assertEqual(status, 200, resp)
        self.assertNotEqual(resp["standings"], [])

        negative_cases = {
            "cross_program": fixture["foreign"]["division"]["id"],
            "cross_season": fixture["other_season"]["division"]["id"],
            "cross_league": fixture["other_league"]["division"]["id"],
            "deleted": fixture["deleted"]["division"]["id"],
            "unbound": fixture["unbound"]["division"]["id"],
            "archived_non_active": fixture["archived"]["division"]["id"],
        }
        for case, div_id in negative_cases.items():
            with self.subTest(case=case):
                status, resp = self._req(admin, "GET",
                                         f"/api/standings/{div_id}")
                self.assertEqual(status, 200, resp)
                self.assertEqual(
                    resp, {"division_id": div_id, "standings": []}, resp)

        # Positive: an ARCHIVED Season explicitly selected as the active
        # tuple is honored as read-only history, not rejected for being
        # archived per se -- only for NOT being active is a rejection.
        self._set_context(admin, fixture["program_a"]["id"],
                          fixture["season_a3"]["id"], fixture["league_a3a"]["id"])
        archived_id = fixture["archived"]["division"]["id"]
        status, resp = self._req(admin, "GET", f"/api/standings/{archived_id}")
        self.assertEqual(status, 200, resp)
        self.assertNotEqual(resp["standings"], [])


if __name__ == "__main__":
    unittest.main()
