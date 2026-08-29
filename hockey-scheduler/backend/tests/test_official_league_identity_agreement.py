"""A GAME'S OWN LEAGUE IDENTITY MUST AGREE WITH ITS FROZEN BINDING (#205).

THE BLOCKER (owner comment 5439211082), reproduced on exact head 63db78f
through the public context-options API on Memory, SQLite and PostgreSQL

    "`services/context_scope.py::_official_league_ids` validates the frozen
     `LeagueSeason` against `Game.season_id` unconditionally, but the adjacent
     League check is still guarded by `if game.league_id and ...`. I assigned an
     Official to a coherent target Game and to a separate coherent anchor Game,
     then store-wrote the target Game's schema-permitted nullable `league_id` to
     `None` and separately to the empty string. `ApiService.get_context_options`
     continued to offer both the target and anchor Leagues in all four runs. The
     target League should disappear; the anchor proves the remaining
     Program/Season authorization is non-vacuous."

THE RULE, which is the standing one on this PR applied a third time: IDENTITY
IS COMPARED WITH PLAIN EQUALITY, NEVER WITH TRUTHINESS.
`season_guard.game_is_league_season_bound` spells it `league_season_id is not
None` for exactly this reason. A bound regular Game whose own League identity
is missing or falsy is internally INCONSISTENT; treating that shape as an
EXEMPTION lets a scoped Official keep League visibility from corrupt, restored,
legacy or direct-written data instead of failing closed. The supported
write-side revalidation already applies plain equality for this same threat.

WHAT WAS MEASURED RED at head 63db78f, through public `get_context_options`::

    target game.league_id      leagues offered          expected
    (coherent)                 Target + Anchor          (correct)
    None                       Target + Anchor          Anchor only
    ""                         Target + Anchor          Anchor only
    sibling League (truthy)    Anchor only              Anchor only  (already OK)

THE ANCHOR IS LOAD-BEARING. Every fixture gives the Official a SECOND coherent
active assignment whose Program, Season and League must SURVIVE. Without it a
negative result would be satisfied by all context collapsing for an unrelated
reason, and the assertions would prove nothing. The sibling-League case is kept
as a positive control: it was already refused before this change, so it shows
the check is non-vacuous rather than newly-invented.

THE STORE WRITE IS DELIBERATE AND IS THE OWNER'S OWN REPRO. No supported
product write path produces `league_id=None`/`""` on a bound Game — that is the
entire point. The shape is reachable through restore, legacy rows or direct
SQL, and the READ side must not trust it. Every other state change in this file
goes through the product's own API.

TRI-STORE, PROVEN, plus the authenticated `GET /api/context/options` route.
`_assert_backend` proves each backend and `_assert_ran` fails a loop that
silently covered fewer than were configured. A SKIP IS NOT A PASS.

THE FALSIFIER. `RestoringTheTruthyLeagueGuardMustRedden` puts `if
game.league_id and ...` back and requires the None/"" assertions to fail — while
requiring the sibling-mismatch assertion to STILL hold, which is what shows the
falsifier targets the falsy exemption specifically rather than simply disabling
the check.

THE HARNESS AND THE ROUTE BASE ARE IMPORTED from
`test_official_scope_grant_predicate.py` (blocker C, the declined-assignment
grant) rather than redefined. The two blockers are separate rulings that live in
the same two projection helpers, so one harness keeps their tri-store and route
conventions identical instead of letting two copies drift — and the falsifier
below calls C's centralized predicate, so that module is the one that stands
alone.
"""

import json
import unittest

from helpers import BACKEND  # noqa: F401  (ensures sys.path is set up)
from test_active_context_league import _assign_game, _registered_team
from test_official_scope_grant_predicate import (
    _ContextOptionsRouteBase, _OfficialScopeHarness)

from hockey_scheduler.api.service import ApiService
from hockey_scheduler.domain import Role
from hockey_scheduler.services import context_scope
from hockey_scheduler.store import InMemoryStore
from hockey_scheduler.web.auth import DEMO_PASSWORD, DEMO_USERS


# =========================================================================== #
# BLOCKER B — the Game and its frozen binding must AGREE, unconditionally     #
# =========================================================================== #
class AGamesOwnLeagueIdentityMustAgreeWithItsFrozenBinding(
        _OfficialScopeHarness, unittest.TestCase):
    """One Program and Season carrying TWO Leagues, so a target that vanishes
    cannot be confused with the whole Program falling away."""

    def _fixture(self, store):
        api = ApiService(store)
        pid = api.create_program("Prog", "US", "UTC")["id"]
        sid = api.create_season(pid, "Fall")["id"]
        target = api.create_league(sid, "TargetLeague")["id"]
        anchor = api.create_league(sid, "AnchorLeague")["id"]
        t_team = _registered_team(api, sid, target, "TargetTeam")
        a_team = _registered_team(api, sid, anchor, "AnchorTeam")
        oid = api.create_official("Ref")["id"]
        t_gid = _assign_game(
            api, oid, season_id=sid, team_id=t_team, league_id=target,
            league_season_id=store.league_season_for(target, sid).id)
        a_gid = _assign_game(
            api, oid, season_id=sid, team_id=a_team, league_id=anchor,
            league_season_id=store.league_season_for(anchor, sid).id)
        return api, dict(oid=oid, t_gid=t_gid, a_gid=a_gid, sid=sid,
                         target=target, anchor=anchor)

    def _write_league_id(self, store, gid, value):
        """The owner's repro writes the corrupt value at the STORE, because no
        supported product write path can produce it — that is the whole point:
        the shape is reachable through restore, legacy data or direct SQL, and
        the read side must not trust it."""
        with store.transaction():
            g = store.get_game(gid)
            g.league_id = value
            store.save_game(g)
        return store.get_game(gid).league_id

    def test_a_coherent_assignment_grants_its_target_league(self):
        """The POSITIVE control. Without it every assertion below could be
        satisfied by the target never having been granted at all."""
        ran = []
        for label, store in self._stores():
            try:
                self._assert_backend(label, store)
                store.clear_all_data()
                api, fx = self._fixture(store)
                with self.subTest(backend=label):
                    self.assertEqual(self._leagues(self._options(api, fx["oid"])),
                                     ["AnchorLeague", "TargetLeague"], label)
                ran.append(label)
            finally:
                self._close(label, store)
        self._assert_ran(ran, "OFFICIAL LEAGUE / COHERENT BASELINE")

    def test_none_empty_and_sibling_mismatch_each_remove_only_the_target(self):
        ran = []
        for label, store in self._stores():
            try:
                self._assert_backend(label, store)
                for case, value in (("None", None), ("empty string", ""),
                                    ("sibling mismatch", "SENTINEL")):
                    store.clear_all_data()
                    api, fx = self._fixture(store)
                    corrupt = fx["anchor"] if value == "SENTINEL" else value
                    readback = self._write_league_id(store, fx["t_gid"], corrupt)
                    if value != "SENTINEL":
                        # prove the corrupt shape actually PERSISTED — on a
                        # backend that coerced "" to NULL (or dropped the
                        # write) this test would otherwise pass vacuously
                        self.assertEqual(readback, value, (label, case))

                    with self.subTest(backend=label, case=case):
                        programs, seasons, leagues = self._axes(api, fx["oid"])
                        # ONLY the target is gone
                        self.assertEqual(leagues, ["AnchorLeague"],
                                         (label, case))
                        # the anchor keeps Program and Season visible, so the
                        # negative above is not "all context collapsed"
                        self.assertEqual(programs, ["Prog"], (label, case))
                        self.assertEqual(seasons, ["Fall"], (label, case))
                ran.append(label)
            finally:
                self._close(label, store)
        self._assert_ran(ran, "OFFICIAL LEAGUE / FALSY + MISMATCH")


class RestoringTheTruthyLeagueGuardMustRedden(
        _OfficialScopeHarness, unittest.TestCase):
    """BLOCKER B'S RULED FALSIFIER: put `if game.league_id and ...` back and
    require the None/"" assertions to fail.

    The sibling-mismatch case is deliberately checked too, and must STILL pass
    under the truthy guard — that is what proves the falsifier is targeting the
    falsy exemption specifically and not simply disabling the check."""

    def _with_truthy_guard(self):
        from hockey_scheduler.domain import GameType
        from hockey_scheduler.services import season_guard
        from hockey_scheduler.services.subject_scope import (
            assignment_grants_official_scope as grants)

        def broken(store, official_id):
            leagues = set()
            if not official_id:
                return leagues
            for a in store.assignments_for_official(official_id):
                if not grants(a, official_id):
                    continue
                game = store.get_game(a.game_id)
                if game is None:
                    continue
                if ((game.game_type or GameType.REGULAR.value)
                        != GameType.REGULAR.value):
                    continue
                if not season_guard.game_is_league_season_bound(game):
                    continue
                ls = store.get_league_season(game.league_season_id)
                if ls is None:
                    continue
                if ls.season_id != game.season_id:
                    continue
                # THE DEFECT, restored verbatim.
                if game.league_id and ls.league_id != game.league_id:
                    continue
                if ls.league_id:
                    leagues.add(ls.league_id)
            return leagues

        original = context_scope._official_league_ids
        context_scope._official_league_ids = broken
        self.addCleanup(setattr, context_scope, "_official_league_ids",
                        original)

    def test_the_falsy_cases_fail_but_the_sibling_mismatch_still_holds(self):
        fx_cls = AGamesOwnLeagueIdentityMustAgreeWithItsFrozenBinding
        self._with_truthy_guard()
        for value in (None, ""):
            store = InMemoryStore()
            try:
                api, fx = fx_cls._fixture(self, store)
                fx_cls._write_league_id(self, store, fx["t_gid"], value)
                leagues = self._leagues(self._options(api, fx["oid"]))
                with self.assertRaises(AssertionError,
                                       msg=f"league_id={value!r}"):
                    self.assertEqual(leagues, ["AnchorLeague"])
                self.assertIn("TargetLeague", leagues,
                              "the truthy guard grants the corrupt target")
            finally:
                store.close() if hasattr(store, "close") else None

        # ...while a TRUTHY wrong value is still refused, so the falsifier is
        # aimed at the falsy exemption and nothing wider.
        store = InMemoryStore()
        try:
            api, fx = fx_cls._fixture(self, store)
            fx_cls._write_league_id(self, store, fx["t_gid"], fx["anchor"])
            self.assertEqual(self._leagues(self._options(api, fx["oid"])),
                             ["AnchorLeague"])
        finally:
            store.close() if hasattr(store, "close") else None



class OverTheAuthenticatedContextOptionsRoute(
        _ContextOptionsRouteBase, unittest.TestCase):

    def test_a_falsy_target_league_disappears_from_the_route(self):
        cls = AGamesOwnLeagueIdentityMustAgreeWithItsFrozenBinding
        ran = []
        for label, store in self._stores():
            try:
                self._assert_backend(label, store)
                for value in (None, ""):
                    store.clear_all_data()
                    api, fx = cls._fixture(self, store)
                    api.accounts.create_account(
                        "admin", DEMO_PASSWORD, DEMO_USERS["admin"], scope={},
                        actor_id="test_seed", account_id="user_admin")
                    api.accounts.create_account(
                        "theref", DEMO_PASSWORD, Role.OFFICIAL,
                        scope={"official_id": fx["oid"]}, actor_id="test_seed",
                        account_id="user_theref")
                    ref = self._login(api, "theref")

                    status, before = self._req(ref, "GET",
                                               "/api/context/options")
                    self.assertEqual(status, 200, before)
                    self.assertEqual(self._leagues(before),
                                     ["AnchorLeague", "TargetLeague"], before)

                    cls._write_league_id(self, store, fx["t_gid"], value)

                    with self.subTest(backend=label, value=repr(value)):
                        status, after = self._req(ref, "GET",
                                                  "/api/context/options")
                        self.assertEqual(status, 200, after)
                        self.assertEqual(self._leagues(after),
                                         ["AnchorLeague"], after)
                        self.assertNotIn("TargetLeague", json.dumps(after),
                                         after)
                ran.append(label)
            finally:
                self._close(label, store)
        self._assert_ran(ran, "CONTEXT OPTIONS HTTP / FALSY LEAGUE")



if __name__ == "__main__":
    unittest.main(verbosity=2)
