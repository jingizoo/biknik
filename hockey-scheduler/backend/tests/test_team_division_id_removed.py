"""#180: SeasonTeamRegistration is the only source of truth for a Team's Season
and Division participation. Team.division_id survives only as legacy
persistence/migration compatibility and must not drive any current behavior.

Two guarantees are proven here:
  1. a repository guard — no operational service module *reads* Team.division_id;
  2. behavior — participation, scheduling and League resolution follow
     registrations (and permanent league_id), never the legacy field.
The behavior contract runs against the in-memory store and a durable SQL store
(SQLite locally, PostgreSQL via TEST_DATABASE_URL in CI).
"""

import ast
import os
import unittest
from pathlib import Path

from helpers import BACKEND  # noqa: F401  (ensures sys.path is set up)

from hockey_scheduler.api.service import ApiService
from hockey_scheduler.domain import (
    LeagueSeason, Season, SeasonTeamRegistration, Team)
from hockey_scheduler.services.league_scope import (
    league_id_for_game, team_registration_valid)
from hockey_scheduler.store import InMemoryStore, SqlStore

_PKG = Path(__file__).resolve().parents[1] / "hockey_scheduler"

# The ONLY files allowed to touch a Team's legacy division_id/division fields —
# the compat model + the persistence layer that maps the retained column and the
# (SQL) migration. Every other module must be free of any Team.division_id read
# OR write (#180 review). Migrations themselves are .sql and are never scanned.
_COMPAT_ALLOWLIST = {
    "domain/models.py",        # the field declaration itself
    "store/memory_store.py",   # in-memory persistence of the retained column
    "store/sql_store.py",      # SQL persistence of the retained column
}
# Names that denote a Team object at an access site.
_TEAM_NAMES = {"team", "t", "home", "away", "home_team", "away_team", "tm",
               "hometeam", "awayteam", "existing_team", "new_team"}
_TEAM_GETTERS = {"get_team", "get_home_team", "get_away_team"}
_LEGACY_ATTRS = {"division_id", "division"}


class NoTeamDivisionIdUsageGuardTest(unittest.TestCase):
    """Whole-package guard: no non-compat module reads OR writes a Team's legacy
    division_id/division. Covers attribute reads/writes on team-named variables
    and on get_team(...) chains, and Team(...) constructions carrying the legacy
    fields — so the "no operational usage" claim is proven structurally, not by
    a narrow name allowlist."""

    def _team_legacy_usages(self, path):
        tree = ast.parse(path.read_text())
        hits = []
        for node in ast.walk(tree):
            # Attribute access (READ or WRITE) of a legacy field on a Team.
            if isinstance(node, ast.Attribute) and node.attr in _LEGACY_ATTRS:
                base = node.value
                is_team_name = (isinstance(base, ast.Name)
                                and base.id in _TEAM_NAMES)
                is_getter = (isinstance(base, ast.Call)
                             and isinstance(base.func, ast.Attribute)
                             and base.func.attr in _TEAM_GETTERS)
                if is_team_name or is_getter:
                    hits.append((node.lineno, node.attr))
            # Team(...) construction carrying a legacy field.
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                    and node.func.id == "Team"):
                for kw in node.keywords:
                    if kw.arg in _LEGACY_ATTRS:
                        hits.append((node.lineno, f"Team(..{kw.arg}=..)"))
            # String-keyed access aliases the attribute past the check above:
            #   _group(teams, "division_id")  /  getattr(team, "division_id")
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                fn = node.func.id
                args = node.args
                if (fn in {"_group", "group_by", "groupBy"} and len(args) >= 2
                        and isinstance(args[0], ast.Name)
                        and args[0].id in {"teams", "all_teams"}
                        and isinstance(args[1], ast.Constant)
                        and args[1].value in _LEGACY_ATTRS):
                    hits.append((node.lineno, f"_group(teams, {args[1].value!r})"))
                if fn == "getattr" and len(args) >= 2:
                    base = args[0]
                    base_is_team = (
                        (isinstance(base, ast.Name) and base.id in _TEAM_NAMES)
                        or (isinstance(base, ast.Call)
                            and isinstance(base.func, ast.Attribute)
                            and base.func.attr in _TEAM_GETTERS))
                    if (base_is_team and isinstance(args[1], ast.Constant)
                            and args[1].value in _LEGACY_ATTRS):
                        hits.append((node.lineno, f"getattr(team, {args[1].value!r})"))
        return hits

    def test_no_module_outside_compat_uses_team_division_id(self):
        offenders = {}
        for path in _PKG.rglob("*.py"):
            rel = path.relative_to(_PKG).as_posix()
            if rel in _COMPAT_ALLOWLIST:
                continue
            usages = self._team_legacy_usages(path)
            if usages:
                offenders[rel] = usages
        self.assertEqual(
            offenders, {},
            f"non-compat code still uses Team.division_id/division: {offenders}")


class LegacyFieldIsInertContract:
    """Behavior: registrations, not Team.division_id, drive participation."""

    def make_store(self):
        raise NotImplementedError

    ACTOR = "user_admin"

    def setUp(self):
        self.store = self.make_store()
        self.api = ApiService(self.store)
        self.league = self.api.create_program("L", actor_id=self.ACTOR)["id"]
        self.club = self.api.create_club("C", actor_id=self.ACTOR)["id"]

    def _season(self, name):
        return self.api.create_season(self.league, name, actor_id=self.ACTOR)["id"]

    def _division(self, season_id, name):
        return self.api.create_division(season_id, name, actor_id=self.ACTOR)["id"]

    def _team(self, name, division_id=None, program_id="_default",
              league_id=None):
        # #283 Slice E: a Team needs a resolvable permanent League. Callers
        # supply it via a ``division_id`` (League derived from its LeagueSeason)
        # or an explicit ``league_id``; the Team's legacy ``division_id`` field
        # is never set either way, so the "no legacy division" intent is kept.
        pid = self.league if program_id == "_default" else program_id
        return self.api.create_team(
            self.club, division_id, name, actor_id=self.ACTOR, program_id=pid,
            league_id=league_id)["id"]

    def _slot(self, season_id):
        venue = self.api.create_venue("V", league_id=self.league, actor_id=self.ACTOR)["id"]
        self.api.grant_season_venue_access(season_id, venue, actor_id=self.ACTOR)
        rink = self.api.create_rink(venue, "R", actor_id=self.ACTOR)["id"]
        return self.api.create_ice_slot(
            rink, "2027-05-01T18:00:00+00:00", "2027-05-01T19:00:00+00:00",
            actor_id=self.ACTOR)["id"]

    def _register(self, season_id, team_id, division_id):
        return self.api.register_team_for_season(
            season_id, team_id, division_id, actor_id=self.ACTOR)["id"]

    def test_stale_legacy_division_is_ignored_for_scheduling(self):
        # A team whose legacy division_id points at dB but is REGISTERED in dA
        # plays in dA, and cannot be scheduled in dB.
        season = self._season("S")
        dA = self._division(season, "A")
        dB = self._division(season, "B")
        home = self._team("Home", division_id=dB)   # stale legacy pointer at dB
        away = self._team("Away", division_id=dB)
        self._register(season, home, dA)             # truth: both play dA
        self._register(season, away, dA)
        slot = self._slot(season)
        # Scheduling in the REGISTERED division succeeds despite the legacy dB.
        game = self.api.create_game(season, dA, home, away, slot, actor_id=self.ACTOR)
        self.assertNotIn("error", game)
        # Scheduling in the LEGACY division is rejected — the field is inert.
        bad = self.api.create_game(season, dB, home, away, self._slot(season),
                                   actor_id=self.ACTOR)
        self.assertIn("error", bad)

    def test_one_team_two_seasons_resolves_each_division(self):
        s1 = self._season("2026")
        s2 = self._season("2027")
        # #283: one permanent League spanning both Seasons (a League may span
        # Seasons); each Season's Division hangs off that shared League, so the
        # single team can legitimately register in both (rule 7).
        league = self.api.create_league(s1, "L League", actor_id=self.ACTOR)["id"]
        d1 = self.api.create_division(
            s1, "D1", league_id=league, actor_id=self.ACTOR)["id"]
        d2 = self.api.create_division(
            s2, "D2", league_id=league, actor_id=self.ACTOR)["id"]
        team = self._team("Nomads", league_id=league)  # no legacy division at all
        self._register(s1, team, d1)
        self._register(s2, team, d2)
        r1 = self.store.registration_for_team_in_season(s1, team)
        r2 = self.store.registration_for_team_in_season(s2, team)
        self.assertEqual(r1.division_id, d1)
        self.assertEqual(r2.division_id, d2)
        # The shared resolver agrees per-season.
        self.assertIsNotNone(team_registration_valid(
            self.store, self.store.get_season(s1), team, d1))
        self.assertIsNotNone(team_registration_valid(
            self.store, self.store.get_season(s2), team, d2))
        self.assertIsNone(team_registration_valid(
            self.store, self.store.get_season(s1), team, d2))

    def test_changing_registration_leaves_legacy_field_and_history_untouched(self):
        s1 = self._season("2026")
        s2 = self._season("2027")
        # #283: one permanent League spanning both Seasons so the single team
        # can register in each (rule 7); dA/dB live in s1, d2 in s2.
        league = self.api.create_league(s1, "L League", actor_id=self.ACTOR)["id"]
        dA = self.api.create_division(
            s1, "A", league_id=league, actor_id=self.ACTOR)["id"]
        dB = self.api.create_division(
            s1, "B", league_id=league, actor_id=self.ACTOR)["id"]
        d2 = self.api.create_division(
            s2, "D2", league_id=league, actor_id=self.ACTOR)["id"]
        team = self._team("T", league_id=league)     # legacy division_id = None
        reg1 = self._register(s1, team, dA)
        reg2 = self._register(s2, team, d2)          # a second-season history row
        self.api.assign_season_team_division(reg1, dB, actor_id=self.ACTOR)
        # The registration moved…
        self.assertEqual(
            self.store.registration_for_team_in_season(s1, team).division_id, dB)
        # …but the Team's compatibility field did NOT change…
        self.assertIsNone(self.store.get_team(team).division_id)
        # …and the other season's registration is untouched.
        self.assertEqual(
            self.store.registration_for_team_in_season(s2, team).division_id, d2)

    def test_team_without_league_id_is_rejected_not_guessed(self):
        # A team with no concrete league_id but a legacy division_id must NOT be
        # resolved through that division — it is simply invalid for scheduling.
        season = self._season("S")
        dA = self._division(season, "A")
        # create_team derives league_id from a division, so build the invalid
        # league-less shape directly in the store.
        orphan = Team(id=self.store.next_id("team"), name="Orphan",
                      division_id=dA, program_id=None)
        self.store.add_team(orphan)
        # #283: a registration hangs off the division's LeagueSeason.
        dA_ls = self.store.get_division(dA).league_season_id
        self.store.add_season_team_registration(SeasonTeamRegistration(
            id=self.store.next_id("streg"), league_season_id=dA_ls,
            team_id=orphan.id, division_id=dA, active=True))
        other = self._team("Other")
        self._register(season, other, dA)
        # The shared resolver refuses the league-less team even though its legacy
        # division sits in this season's league.
        self.assertIsNone(team_registration_valid(
            self.store, self.store.get_season(season), orphan.id, dA))
        bad = self.api.create_game(season, dA, orphan.id, other, self._slot(season),
                                   actor_id=self.ACTOR)
        self.assertIn("error", bad)

    def test_overview_exposes_only_operationally_valid_registrations(self):
        # The scheduling wizard reads overview["registrations"]; a corrupt or
        # retained row must never appear there (#180 review) so it can't be
        # offered in the picker or reach a create request.
        season = self._season("S")
        league = self.api.create_league(season, "Adult League", actor_id=self.ACTOR)["id"]
        dA = self._division(season, "A")
        self.api.assign_division_league(dA, league, actor_id=self.ACTOR)
        good = self._team("Good")
        self.api.register_team_for_season(
            season, good, dA, actor_id=self.ACTOR, league_id=league)

        # #283: every injected row hangs off this season's LeagueSeason (for the
        # created league); operational validity is judged from there.
        season_ls = self.store.league_season_for(league, season).id

        def bad_reg(team_id, division_id):
            self.store.add_season_team_registration(SeasonTeamRegistration(
                id=self.store.next_id("streg"), league_season_id=season_ls,
                team_id=team_id, division_id=division_id, active=True))

        # cross-league team
        other = self.api.create_program("Other", actor_id=self.ACTOR)["id"]
        cross = Team(id=self.store.next_id("team"), name="Cross", program_id=other)
        self.store.add_team(cross)
        bad_reg(cross.id, dA)
        # missing team
        bad_reg("team_missing", dA)
        # league-less team
        orphan = Team(id=self.store.next_id("team"), name="Orphan", program_id=None)
        self.store.add_team(orphan)
        bad_reg(orphan.id, dA)
        # wrong-season: the division belongs to a different season than the row
        s2 = self._season("S2")
        d2 = self._division(s2, "D2")
        wrong = self._team("Wrong", division_id=d2)
        bad_reg(wrong, d2)

        exposed = {r["team_id"]
                   for r in self.api.get_demo_overview()["registrations"]}
        self.assertIn(good, exposed)
        for hidden in (cross.id, "team_missing", orphan.id, wrong):
            self.assertNotIn(hidden, exposed)

    def test_registration_with_a_dangling_shared_league_is_excluded(self):
        # Season and Team agree on a league id that has NO League row (#180
        # review): the resolver must still reject it, not trust matching-but-
        # dangling ids.
        ghost = "league_ghost"
        season = Season(id=self.store.next_id("season"), program_id=ghost, name="S")
        self.store.add_season(season)
        team = Team(id=self.store.next_id("team"), name="Ghost", program_id=ghost)
        self.store.add_team(team)
        # #283: the row resolves its Season/League via a LeagueSeason; bind one to
        # this dangling program so the row is found yet still trusts nothing.
        ghost_ls = self.store.add_league_season(LeagueSeason(
            id=self.store.next_id("leagueseason"), league_id=ghost,
            season_id=season.id))
        self.store.add_season_team_registration(SeasonTeamRegistration(
            id=self.store.next_id("streg"), league_season_id=ghost_ls.id,
            team_id=team.id, division_id=None, active=True))
        self.assertIsNone(
            team_registration_valid(self.store, season, team.id, None))
        exposed = {r["team_id"]
                   for r in self.api.get_demo_overview()["registrations"]}
        self.assertNotIn(team.id, exposed)

    def test_overview_excludes_registration_with_no_league_id(self):
        # #233 B2c review r1: every registration is required to carry a
        # League — a row with league_id=None (e.g. a stale pre-#233 v1 row,
        # or a derivation that couldn't determine one) must be excluded from
        # the overview, never silently passed through as league-less.
        season = self._season("S")
        league = self.api.create_league(season, "L League", actor_id=self.ACTOR)["id"]
        team = self._team("NoLeague", league_id=league)
        # #283: a registration always references a LeagueSeason; a row whose
        # League doesn't resolve (bound to a non-existent League) is the
        # league-less corruption that must be excluded.
        ghost_ls = self.store.add_league_season(LeagueSeason(
            id=self.store.next_id("leagueseason"), league_id="league_ghost",
            season_id=season))
        self.store.add_season_team_registration(SeasonTeamRegistration(
            id=self.store.next_id("streg"), league_season_id=ghost_ls.id,
            team_id=team, division_id=None, active=True))
        exposed = {r["team_id"]
                  for r in self.api.get_demo_overview()["registrations"]}
        self.assertNotIn(team, exposed)

    def test_overview_registration_exposes_league_id_for_wizard(self):
        # #233 B2c: the scheduling wizard's League picker needs the
        # registration's competition league_id — a v2 (league-required)
        # registration must expose it verbatim in the overview.
        season = self._season("S")
        league = self.api.create_league(season, "Adult League", actor_id=self.ACTOR)["id"]
        dA = self._division(season, "A")
        self.api.assign_division_league(dA, league, actor_id=self.ACTOR)
        team = self._team("Reg")
        reg = self.api.register_team_for_season(
            season, team, dA, actor_id=self.ACTOR, league_id=league)["id"]
        row = next(r for r in self.api.get_demo_overview()["registrations"]
                  if r["team_id"] == team)
        self.assertEqual(row["league_id"], league)
        self.assertEqual(row["division_id"], dA)

        # A league-only registration (no Division) exposes league_id with a
        # null division_id — never silently dropped from the picker's data.
        team2 = self._team("LeagueOnly")
        reg2 = self.api.register_team_for_season(
            season, team2, None, actor_id=self.ACTOR, league_id=league)["id"]
        row2 = next(r for r in self.api.get_demo_overview()["registrations"]
                   if r["team_id"] == team2)
        self.assertEqual(row2["league_id"], league)
        self.assertIsNone(row2["division_id"])

    def test_overview_excludes_registration_with_dangling_league_season(self):
        # A registration whose LeagueSeason link is dangling (points at no
        # LeagueSeason row at all) must never reach the wizard — the same
        # "missing-League" corruption class the division check already guards
        # against (#233 B2c). #283: a League may legitimately span Seasons, so
        # the corruption is an unresolvable LeagueSeason, not a "cross-season"
        # league.
        season = self._season("S")
        league = self.api.create_league(season, "L League", actor_id=self.ACTOR)["id"]
        team = self._team("Dangling", league_id=league)
        self.store.add_season_team_registration(SeasonTeamRegistration(
            id=self.store.next_id("streg"),
            league_season_id="leagueseason_missing",
            team_id=team, division_id=None, active=True))
        exposed = {r["team_id"]
                  for r in self.api.get_demo_overview()["registrations"]}
        self.assertNotIn(team, exposed)

    def test_overview_excludes_registration_with_division_league_mismatch(self):
        # A registration whose Division belongs to a DIFFERENT League than the
        # registration's own league_id is corrupt and must never be exposed
        # (#233 B2c) — the wizard's division picker is scoped by league, so a
        # mismatched row would otherwise silently offer the wrong division.
        season = self._season("S")
        league = self.api.create_league(season, "Adult League", actor_id=self.ACTOR)["id"]
        other_league = self.api.create_league(
            season, "Other League", actor_id=self.ACTOR)["id"]
        # The division belongs to other_league; the registration below claims
        # `league`, so its LeagueSeason and the division's disagree.
        dA = self.api.create_division(
            season, "A", league_id=other_league, actor_id=self.ACTOR)["id"]
        team = self._team("Mismatch", league_id=league)
        league_ls = self.store.league_season_for(league, season).id
        self.store.add_season_team_registration(SeasonTeamRegistration(
            id=self.store.next_id("streg"), league_season_id=league_ls,
            team_id=team, division_id=dA, active=True))
        exposed = {r["team_id"]
                  for r in self.api.get_demo_overview()["registrations"]}
        self.assertNotIn(team, exposed)

    def test_delete_division_ignores_stale_legacy_team_pointer(self):
        # A division with no registrations/games deletes cleanly even if a team's
        # legacy division_id still points at it (#180 — that pointer is inert).
        season = self._season("S")
        dA = self._division(season, "A")
        self._team("Legacy", division_id=dA)   # only a legacy pointer, no reg
        result = self.api.delete_division(dA, actor_id=self.ACTOR)
        self.assertNotIn("error", result)
        self.assertIsNone(self.store.get_division(dA))


class MemoryLegacyFieldInertTest(LegacyFieldIsInertContract, unittest.TestCase):
    def make_store(self):
        return InMemoryStore()


class DurableLegacyFieldInertTest(LegacyFieldIsInertContract, unittest.TestCase):
    def make_store(self):
        url = os.environ.get("TEST_DATABASE_URL") or ":memory:"
        store = SqlStore(url)
        store.reset_schema()
        return store


if __name__ == "__main__":
    unittest.main()
