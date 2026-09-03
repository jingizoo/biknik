"""Regression coverage for #205 blocker 1 (web/scope.py permanent-pointer
authorization defect).

THE DEFECT THIS FILE PINS. On top of the step-1/step-2 cutover
(``resolve_membership``/``team_for_game``/``resolve_memberships_for_game`` --
test_substitute_membership_cutover.py / test_slot_overfill_regression.py),
two HTTP authorization gates in ``web/scope.py`` still answered "which team
does this Player/Coach act for" from the PERMANENT ``player.team_id``
pointer, never the game-scoped ``SeasonRosterMembership`` the rest of the
substitute workflow already resolves through:

  * ``can_read_private_game_data`` (PLAYER branch) -- the private-read gate
    behind every ``/api/games/{gid}/<board|roster|substitutes|...>`` route --
    read ``player_team_id`` (the permanent pointer), so it was WRONG in BOTH
    directions for a LeagueSeason-bound game whose permanent pointer and
    season membership disagree (a mid-season transfer, or any import-shaped
    row where they were never the same to begin with):

      - OVER-GRANT: pointer HOME, membership moved to THIRD (not a side of
        the game) -- still granted HOME's private data.
      - UNDER-GRANT: pointer THIRD (unrelated), membership on HOME (a real
        side) -- a legitimate Mover wrongly DENIED.

  * ``scope_violation`` (COACH branch, the gate behind every coach-initiated
    ``POST /api/games/{gid}/...``) compared a target player's PERMANENT
    pointer against the coach's own team for the substitute-workflow actions
    (``substitutes/add-candidate``, ``substitutes/{pid}/offer``,
    ``substitutes/{pid}/add-to-roster``) -- the same defect class, mirrored
    onto the write side:

      - FALSE DENIAL: a coach whose team matches a Mover's RESOLVED side
        (membership) but not their stale pointer could not manage that
        legitimate Mover at all -- even though the SAME player, routed
        through ``api.enroll_substitute`` directly (membership-resolved),
        succeeds.
      - Symmetric OVER-GRANT (found here, not named in the owner's recon
        text but the same defect class on the same surface): a coach whose
        team matches a player's stale pointer, but whose membership has
        since moved OFF that team, could still manage a player who no
        longer belongs to them.

THE FIX. Both gates now resolve a Player's/Coach-target's team through
``RosterService.team_for_game`` -- the SAME game-scoped resolver the
substitute workflow's own business logic (enroll/offer/accept, slot
counting) already uses -- via the new ``game_scoped_own_team_id`` helper in
``web/scope.py``. A Coach's OWN team stays the permanent
``scope["team_id"]`` (there is no ``CoachSeasonMembership`` anywhere in this
codebase -- a coach's team assignment genuinely is permanent); only the
resolution of a TARGET PLAYER's team changed. For a game with NO
LeagueSeason binding (exhibitions, legacy unbound rows), both gates fall
back to the permanent pointer exactly as before -- ``team_for_game``'s own
documented behavior, unchanged.

TRI-STORE. Memory + SQLite in every contract test; the PostgreSQL class
runs the same arc against a real engine. A SKIP IS NOT A PASS: the
PostgreSQL class announces loudly when TEST_DATABASE_URL is unset.

HTTP. ``ScopeAuthorizationOverHttp`` exercises every case through a REAL
dispatched HTTP request against ``web/server.py``, matching the
``ResolverFixOverHttp``/``SlotOverfillOverHttp`` pattern steps 1/5 already
established.
"""

import json
import os
import threading
import unittest
import urllib.error
import urllib.request
from http.cookiejar import CookieJar
from http.server import ThreadingHTTPServer

from helpers import BACKEND, fresh_sql_store  # noqa: F401  (sets up sys.path)

from test_substitute_membership_cutover import ADMIN, _Fixture

from hockey_scheduler.domain import Role
from hockey_scheduler.store import InMemoryStore, SqlStore
from hockey_scheduler.web import server as srv
from hockey_scheduler.web.scope import (
    can_read_private_game_data, game_scoped_own_team_id, scope_violation)


class _CutoverContract(_Fixture):
    """Shared Memory + SQLite contract body (mirrors
    test_substitute_membership_cutover._CutoverContract)."""

    def _stores(self):
        yield "memory", InMemoryStore()
        yield "sqlite", SqlStore(":memory:")

    def _each(self):
        for label, store in self._stores():
            api, season, league, teams, game, ls_id = self._build(store)
            yield label, api, season, league, teams, game, ls_id


class PlayerPrivateReadResolvesThroughMembership(_CutoverContract,
                                                 unittest.TestCase):
    """``can_read_private_game_data``, PLAYER branch: both directions of
    the owner's demonstrated defect, plus the unaffected ordinary case."""

    def test_over_grant_stale_pointer_no_longer_grants(self):
        """Pointer HOME, membership moved to THIRD (not a side) -- must now
        be DENIED (pre-fix: wrongly granted)."""
        for label, api, season, league, teams, game, ls_id in self._each():
            with self.subTest(backend=label):
                mover = self._pointer_only_player(
                    api, teams["home"]["id"], "OverGrant")
                self._membership(api, mover["id"], ls_id,
                                 teams["third"]["id"])
                self.assertFalse(can_read_private_game_data(
                    Role.PLAYER, {"player_id": mover["id"]},
                    game["id"], api.store), label)

    def test_under_grant_real_mover_is_now_granted(self):
        """Pointer THIRD (unrelated), membership on HOME (a real side) --
        must now be GRANTED (pre-fix: a legitimate Mover wrongly denied)."""
        for label, api, season, league, teams, game, ls_id in self._each():
            with self.subTest(backend=label):
                mover = self._pointer_only_player(
                    api, teams["third"]["id"], "UnderGrant")
                self._membership(api, mover["id"], ls_id,
                                 teams["home"]["id"])
                self.assertTrue(can_read_private_game_data(
                    Role.PLAYER, {"player_id": mover["id"]},
                    game["id"], api.store), label)

    def test_ordinary_case_pointer_matches_membership_is_unaffected(self):
        for label, api, season, league, teams, game, ls_id in self._each():
            with self.subTest(backend=label):
                home_p = self._player(api, teams["home"]["id"], "Ordinary")
                third_p = self._player(api, teams["third"]["id"], "Unrelated")
                self.assertTrue(can_read_private_game_data(
                    Role.PLAYER, {"player_id": home_p["id"]},
                    game["id"], api.store), label)
                self.assertFalse(can_read_private_game_data(
                    Role.PLAYER, {"player_id": third_p["id"]},
                    game["id"], api.store), label)

    def test_coach_branch_of_the_same_gate_is_unaffected(self):
        # Coach's OWN team stays the permanent scope["team_id"] -- no
        # CoachSeasonMembership exists in this codebase.
        for label, api, season, league, teams, game, ls_id in self._each():
            with self.subTest(backend=label):
                self.assertTrue(can_read_private_game_data(
                    Role.COACH, {"team_id": teams["home"]["id"]},
                    game["id"], api.store), label)
                self.assertFalse(can_read_private_game_data(
                    Role.COACH, {"team_id": teams["third"]["id"]},
                    game["id"], api.store), label)


class CoachSubstituteActionsResolveThroughMembership(_CutoverContract,
                                                      unittest.TestCase):
    """``scope_violation``, COACH branch, on the substitute-workflow
    actions: the false-denial recon demonstrated, its symmetric over-grant
    counterpart, and the unaffected ordinary/unrelated cases."""

    def _violations(self, api, coach_scope, gid, pid):
        return {
            "add-candidate": scope_violation(
                Role.COACH, coach_scope,
                f"/api/games/{gid}/substitutes/add-candidate",
                {"player_id": pid}, api.store),
            "offer": scope_violation(
                Role.COACH, coach_scope,
                f"/api/games/{gid}/substitutes/{pid}/offer", {}, api.store),
            "add-to-roster": scope_violation(
                Role.COACH, coach_scope,
                f"/api/games/{gid}/substitutes/{pid}/add-to-roster", {},
                api.store),
            # #205 blocker 1 round-2 review finding: the first pass here
            # only covered the (pid)-in-PATH substitute actions and the
            # separate literal "add-candidate" -- enroll/withdraw take
            # player_id in the BODY with no {pid} path segment at all
            # (`/api/games/{gid}/substitutes/enroll`), a different URL
            # shape the original whitelist regex never matched, so these
            # two fell through to the stale-pointer fallback exactly like
            # the pre-fix defect on every other action here.
            "enroll": scope_violation(
                Role.COACH, coach_scope,
                f"/api/games/{gid}/substitutes/enroll",
                {"player_id": pid}, api.store),
            "withdraw": scope_violation(
                Role.COACH, coach_scope,
                f"/api/games/{gid}/substitutes/withdraw",
                {"player_id": pid}, api.store),
            # Same body-only-player_id shape, MANAGE_ROSTER instead of
            # RESPOND_AVAILABILITY, but the identical fallback -- swept in
            # alongside enroll/withdraw rather than left for a next round.
            "roster/remove": scope_violation(
                Role.COACH, coach_scope,
                f"/api/games/{gid}/roster/remove",
                {"player_id": pid}, api.store),
            "roster/select": scope_violation(
                Role.COACH, coach_scope,
                f"/api/games/{gid}/roster/select",
                {"player_ids": [pid]}, api.store),
            "availability": scope_violation(
                Role.COACH, coach_scope,
                f"/api/games/{gid}/availability",
                {"player_id": pid, "availability_status": "available"},
                api.store),
        }

    def test_false_denial_on_a_legitimate_mover_is_fixed(self):
        """Coach = HOME. Player pointer THIRD, membership HOME (a real
        Mover already resolved onto the coach's own team for this game) --
        must now be ALLOWED (pre-fix: wrongly denied on all three actions)."""
        for label, api, season, league, teams, game, ls_id in self._each():
            with self.subTest(backend=label):
                mover = self._pointer_only_player(
                    api, teams["third"]["id"], "Mover")
                self._membership(api, mover["id"], ls_id,
                                 teams["home"]["id"])
                coach_scope = {"team_id": teams["home"]["id"]}
                violations = self._violations(
                    api, coach_scope, game["id"], mover["id"])
                for action, v in violations.items():
                    self.assertIsNone(v, (label, action, v))
                # Cross-checked against the membership-resolved facade
                # method directly, which was ALWAYS correct (bypasses this
                # HTTP gate) -- confirms the gate now agrees with it.
                enrolled = api.enroll_substitute(game["id"], mover["id"])
                self.assertEqual(enrolled.get("status"), "enrolled",
                                 (label, enrolled))

    def test_symmetric_over_grant_is_closed(self):
        """Coach = HOME. Player pointer HOME (matches the coach's team),
        membership moved to THIRD (no longer a real member) -- must now be
        DENIED (pre-fix: wrongly allowed on the stale pointer match)."""
        for label, api, season, league, teams, game, ls_id in self._each():
            with self.subTest(backend=label):
                left = self._pointer_only_player(
                    api, teams["home"]["id"], "Left")
                self._membership(api, left["id"], ls_id,
                                 teams["third"]["id"])
                coach_scope = {"team_id": teams["home"]["id"]}
                violations = self._violations(
                    api, coach_scope, game["id"], left["id"])
                for action, v in violations.items():
                    self.assertIsNotNone(v, (label, action))

    def test_ordinary_case_is_unaffected(self):
        for label, api, season, league, teams, game, ls_id in self._each():
            with self.subTest(backend=label):
                own = self._player(api, teams["home"]["id"], "Own")
                outsider = self._player(api, teams["away"]["id"], "Outsider")
                coach_scope = {"team_id": teams["home"]["id"]}
                self.assertIsNone(scope_violation(
                    Role.COACH, coach_scope,
                    f"/api/games/{game['id']}/substitutes/add-candidate",
                    {"player_id": own["id"]}, api.store), label)
                self.assertIsNotNone(scope_violation(
                    Role.COACH, coach_scope,
                    f"/api/games/{game['id']}/substitutes/add-candidate",
                    {"player_id": outsider["id"]}, api.store), label)

    def test_unbound_game_keeps_the_permanent_pointer_gate(self):
        """A game with no LeagueSeason binding (exhibitions, legacy rows)
        has no membership to resolve -- scope_violation's coach branch
        falls back to the permanent pointer exactly as before #205."""
        for label, store in self._stores():
            with self.subTest(backend=label):
                from hockey_scheduler.domain import Player, Position, Team
                store.add_team(Team(id="t_home", name="Home"))
                store.add_player(Player(id="p_home", team_id="t_home",
                                        name="P", position=Position.FORWARD))
                coach_scope = {"team_id": "t_home"}
                self.assertIsNone(scope_violation(
                    Role.COACH, coach_scope,
                    "/api/games/unbound_g/substitutes/add-candidate",
                    {"player_id": "p_home"}, store), label)

    def test_decline_and_withdraw_follow_durable_owner_after_transfer(self):
        """Response/removal rows do not change coach ownership with a stint."""
        for label, api, season, league, teams, game, ls_id in self._each():
            with self.subTest(backend=label):
                home_scope = {"team_id": teams["home"]["id"]}
                away_scope = {"team_id": teams["away"]["id"]}

                enrolled = self._player(
                    api, teams["home"]["id"], "Transferred enrollment")
                self.assertNotIn(
                    "error", api.enroll_substitute(game["id"], enrolled["id"]))
                self._transfer(
                    api, enrolled["id"], ls_id, ls_id,
                    teams["away"]["id"])
                withdraw_path = f"/api/games/{game['id']}/substitutes/withdraw"
                withdraw_body = {"player_id": enrolled["id"]}
                self.assertIsNone(scope_violation(
                    Role.COACH, home_scope, withdraw_path, withdraw_body,
                    api.store), label)
                self.assertIsNotNone(scope_violation(
                    Role.COACH, away_scope, withdraw_path, withdraw_body,
                    api.store), label)

                offered = self._player(
                    api, teams["home"]["id"], "Transferred offer")
                self.assertNotIn(
                    "error", api.enroll_substitute(game["id"], offered["id"]))
                self.assertNotIn(
                    "error", api.offer_substitute(game["id"], offered["id"]))
                self._transfer(
                    api, offered["id"], ls_id, ls_id,
                    teams["away"]["id"])
                decline_path = (
                    f"/api/games/{game['id']}/substitutes/"
                    f"{offered['id']}/decline")
                self.assertIsNone(scope_violation(
                    Role.COACH, home_scope, decline_path, {}, api.store),
                    label)
                self.assertIsNotNone(scope_violation(
                    Role.COACH, away_scope, decline_path, {}, api.store),
                    label)


_PG_SKIP = ("PostgreSQL not configured (TEST_DATABASE_URL) or psycopg "
            "missing -- the #205 blocker 1 (web/scope.py authorization) fix "
            "was NOT exercised on PostgreSQL. A SKIP HERE IS NOT A PASS: "
            "the membership reads behind both gates are real SQL here. Set "
            "TEST_DATABASE_URL (run_parallel.py --postgres does) to run it.")


@unittest.skipUnless(os.environ.get("TEST_DATABASE_URL"), _PG_SKIP)
class ScopeAuthorizationPostgresTest(_Fixture, unittest.TestCase):
    """Both gates, both directions, against real PostgreSQL."""

    def setUp(self):
        self.store = fresh_sql_store(os.environ["TEST_DATABASE_URL"])
        self.addCleanup(self.store.close)
        (self.api, self.season, self.league, self.teams, self.game,
         self.ls_id) = self._build(self.store)

    def test_both_gates_both_directions_on_postgres(self):
        api, teams, game, ls_id = self.api, self.teams, self.game, self.ls_id
        over = self._pointer_only_player(api, teams["home"]["id"], "PG Over")
        self._membership(api, over["id"], ls_id, teams["third"]["id"])
        self.assertFalse(can_read_private_game_data(
            Role.PLAYER, {"player_id": over["id"]}, game["id"], api.store))

        under = self._pointer_only_player(api, teams["third"]["id"], "PG Under")
        self._membership(api, under["id"], ls_id, teams["home"]["id"])
        self.assertTrue(can_read_private_game_data(
            Role.PLAYER, {"player_id": under["id"]}, game["id"], api.store))

        coach_scope = {"team_id": teams["home"]["id"]}
        self.assertIsNone(scope_violation(
            Role.COACH, coach_scope,
            f"/api/games/{game['id']}/substitutes/add-candidate",
            {"player_id": under["id"]}, api.store))
        self.assertIsNotNone(scope_violation(
            Role.COACH, coach_scope,
            f"/api/games/{game['id']}/substitutes/add-candidate",
            {"player_id": over["id"]}, api.store))

        # #205 blocker 1 round-2 review finding: enroll/withdraw take
        # player_id in the BODY with no {pid} path segment
        # (`/api/games/{gid}/substitutes/enroll`), a URL shape the
        # substitute-action whitelist regex never matched -- pinned
        # directly against real PostgreSQL, not just Memory/SQLite.
        self.assertIsNone(scope_violation(
            Role.COACH, coach_scope,
            f"/api/games/{game['id']}/substitutes/enroll",
            {"player_id": under["id"]}, api.store))
        self.assertIsNotNone(scope_violation(
            Role.COACH, coach_scope,
            f"/api/games/{game['id']}/substitutes/enroll",
            {"player_id": over["id"]}, api.store))
        self.assertIsNone(scope_violation(
            Role.COACH, coach_scope,
            f"/api/games/{game['id']}/substitutes/withdraw",
            {"player_id": under["id"]}, api.store))
        self.assertIsNotNone(scope_violation(
            Role.COACH, coach_scope,
            f"/api/games/{game['id']}/substitutes/withdraw",
            {"player_id": over["id"]}, api.store))


class ScopeAuthorizationOverHttp(unittest.TestCase):
    """The #205 blocker 1 fix exercised through a REAL HTTP request against
    ``web/server.py`` -- not just the facade/gate functions in isolation,
    matching the ``ResolverFixOverHttp``/``SlotOverfillOverHttp`` pattern
    steps 1/5 established. Builds its own LeagueSeason-bound fixture on the
    live demo server state (``srv.STATE``) via the setup facade."""

    def setUp(self):
        srv.STATE.reset(seed=False)
        self.api = srv.STATE.api
        f = _Fixture()
        (self.api, self.season, self.league, self.teams, self.game,
         self.ls_id) = f._build(self.api.store)
        self.f = f
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), srv.Handler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(
            target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self._teardown_server)

    def _teardown_server(self):
        self.httpd.shutdown()
        self.thread.join(timeout=5)
        self.httpd.server_close()

    def _login(self, username):
        c = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(CookieJar()))
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/api/auth/login",
            data=json.dumps({"username": username,
                            "password": "demo"}).encode(),
            method="POST", headers={"Content-Type": "application/json"})
        with c.open(req) as r:
            self.assertEqual(r.status, 200)
        return c

    def _get(self, opener, path):
        try:
            with opener.open(
                    f"http://127.0.0.1:{self.port}{path}") as r:
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            with e:
                return e.code, json.loads(e.read() or b"{}")

    def _post(self, opener, path, body):
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=json.dumps(body).encode(), method="POST",
            headers={"Content-Type": "application/json"})
        try:
            with opener.open(req) as r:
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            with e:
                return e.code, json.loads(e.read() or b"{}")

    def test_player_over_grant_is_denied_over_http(self):
        over = self.f._pointer_only_player(
            self.api, self.teams["home"]["id"], "HTTP Over")
        self.f._membership(self.api, over["id"], self.ls_id,
                           self.teams["third"]["id"])
        self.api.accounts.create_account(
            "httpover", "demo", "player",
            scope={"player_id": over["id"]}, actor_id="test_seed")
        c = self._login("httpover")
        status, body = self._get(
            c, f"/api/games/{self.game['id']}/board")
        self.assertEqual(status, 403, body)

    def test_player_under_grant_mover_is_granted_over_http(self):
        under = self.f._pointer_only_player(
            self.api, self.teams["third"]["id"], "HTTP Under")
        self.f._membership(self.api, under["id"], self.ls_id,
                           self.teams["home"]["id"])
        self.api.accounts.create_account(
            "httpunder", "demo", "player",
            scope={"player_id": under["id"]}, actor_id="test_seed")
        c = self._login("httpunder")
        status, body = self._get(
            c, f"/api/games/{self.game['id']}/board")
        self.assertEqual(status, 200, body)

    def test_player_ordinary_case_is_unaffected_over_http(self):
        ordinary = self.f._player(self.api, self.teams["home"]["id"],
                                  "HTTP Ordinary")
        self.api.accounts.create_account(
            "httpordinary", "demo", "player",
            scope={"player_id": ordinary["id"]}, actor_id="test_seed")
        c = self._login("httpordinary")
        status, body = self._get(
            c, f"/api/games/{self.game['id']}/board")
        self.assertEqual(status, 200, body)

    def test_coach_false_denial_is_fixed_over_http(self):
        mover = self.f._pointer_only_player(
            self.api, self.teams["third"]["id"], "HTTP Mover")
        self.f._membership(self.api, mover["id"], self.ls_id,
                           self.teams["home"]["id"])
        self.api.accounts.create_account(
            "httpcoach1", "demo", "coach",
            scope={"team_id": self.teams["home"]["id"]}, actor_id="test_seed")
        c = self._login("httpcoach1")
        status, body = self._post(
            c, f"/api/games/{self.game['id']}/substitutes/add-candidate",
            {"player_id": mover["id"]})
        self.assertEqual(status, 200, body)
        self.assertEqual(body.get("status"), "enrolled", body)

    def test_coach_symmetric_over_grant_is_closed_over_http(self):
        # NOTE: the deeper business logic (`add_substitute_candidate` ->
        # `_require_team_for_game`, already membership-resolved since steps
        # 1-2) ALSO denies this exact case, with a DIFFERENT error
        # (``not_eligible``, "You are not on a team in this game.") --
        # so status alone (403 either way) cannot discriminate "denied by
        # the scope_violation gate under test" from "denied downstream for
        # an unrelated reason". Asserting the exact `error.code`/message is
        # what makes this test actually pin the scope.py fix: pre-fix, this
        # request reaches the facade at all (scope_violation returns None)
        # and gets the `not_eligible` body; post-fix, scope_violation itself
        # refuses it BEFORE the facade call, with THIS message.
        left = self.f._pointer_only_player(
            self.api, self.teams["home"]["id"], "HTTP Left")
        self.f._membership(self.api, left["id"], self.ls_id,
                           self.teams["third"]["id"])
        self.api.accounts.create_account(
            "httpcoach2", "demo", "coach",
            scope={"team_id": self.teams["home"]["id"]}, actor_id="test_seed")
        c = self._login("httpcoach2")
        status, body = self._post(
            c, f"/api/games/{self.game['id']}/substitutes/add-candidate",
            {"player_id": left["id"]})
        self.assertEqual(status, 403, body)
        self.assertEqual(body["error"]["code"], "forbidden", body)
        self.assertEqual(body["error"]["message"],
                         "A coach can only manage their own team's players.",
                         body)

    def test_coach_enroll_false_denial_is_fixed_over_http(self):
        # #205 blocker 1 round-2 review finding: substitutes/enroll takes
        # player_id in the BODY, no {pid} path segment -- a URL shape the
        # first-pass whitelist regex never matched, so a coach managing a
        # legitimate Mover (real membership HOME, stale pointer THIRD) was
        # wrongly denied here exactly like the already-fixed actions.
        mover = self.f._pointer_only_player(
            self.api, self.teams["third"]["id"], "HTTP EnrollMover")
        self.f._membership(self.api, mover["id"], self.ls_id,
                           self.teams["home"]["id"])
        self.api.accounts.create_account(
            "httpenroll1", "demo", "coach",
            scope={"team_id": self.teams["home"]["id"]}, actor_id="test_seed")
        c = self._login("httpenroll1")
        status, body = self._post(
            c, f"/api/games/{self.game['id']}/substitutes/enroll",
            {"player_id": mover["id"]})
        self.assertEqual(status, 200, body)
        self.assertEqual(body.get("status"), "enrolled", body)

    def test_coach_enroll_symmetric_over_grant_is_closed_over_http(self):
        # The reconfirmed pre-fix bypass: HOME coach, target player's
        # permanent pointer HOME (stale), real membership AWAY (the genuine
        # OPPOSING side of the same game) -- pre-fix this was HTTP 200,
        # silently enrolling the player as an AWAY substitute on a HOME
        # coach's say-so. enroll_substitute's own eligibility check
        # (team_for_game is not None) does NOT catch this on its own --
        # AWAY is a real, eligible side of the game -- so this is a pure
        # scope_violation gap, not masked by a downstream business refusal.
        mover = self.f._pointer_only_player(
            self.api, self.teams["home"]["id"], "HTTP EnrollOverGrant")
        self.f._membership(self.api, mover["id"], self.ls_id,
                           self.teams["away"]["id"])
        self.api.accounts.create_account(
            "httpenroll2", "demo", "coach",
            scope={"team_id": self.teams["home"]["id"]}, actor_id="test_seed")
        c = self._login("httpenroll2")
        status, body = self._post(
            c, f"/api/games/{self.game['id']}/substitutes/enroll",
            {"player_id": mover["id"]})
        self.assertEqual(status, 403, body)
        self.assertEqual(body["error"]["code"], "forbidden", body)
        self.assertEqual(body["error"]["message"],
                         "A coach can only manage their own team's players.",
                         body)

    def test_coach_withdraw_false_denial_is_fixed_over_http(self):
        mover = self.f._pointer_only_player(
            self.api, self.teams["third"]["id"], "HTTP WithdrawMover")
        self.f._membership(self.api, mover["id"], self.ls_id,
                           self.teams["home"]["id"])
        # Facade enroll (unscoped, matches the existing false-denial cross-
        # check pattern) to give this Mover something to withdraw.
        enrolled = self.api.enroll_substitute(self.game["id"], mover["id"])
        self.assertEqual(enrolled.get("status"), "enrolled", enrolled)
        self.api.accounts.create_account(
            "httpwithdraw1", "demo", "coach",
            scope={"team_id": self.teams["home"]["id"]}, actor_id="test_seed")
        c = self._login("httpwithdraw1")
        status, body = self._post(
            c, f"/api/games/{self.game['id']}/substitutes/withdraw",
            {"player_id": mover["id"]})
        self.assertEqual(status, 200, body)
        self.assertEqual(body.get("status"), "withdrawn", body)

    def test_coach_withdraw_symmetric_over_grant_is_closed_over_http(self):
        # Same reconfirmed bypass as enroll, on withdraw: withdraw_substitute
        # itself checks NOTHING about which team the caller represents --
        # only that an active enrollment exists -- so pre-fix a HOME coach
        # could unilaterally withdraw an AWAY player's real substitute
        # enrollment on the strength of a stale HOME pointer alone.
        mover = self.f._pointer_only_player(
            self.api, self.teams["home"]["id"], "HTTP WithdrawOverGrant")
        self.f._membership(self.api, mover["id"], self.ls_id,
                           self.teams["away"]["id"])
        enrolled = self.api.enroll_substitute(self.game["id"], mover["id"])
        self.assertEqual(enrolled.get("status"), "enrolled", enrolled)
        self.api.accounts.create_account(
            "httpwithdraw2", "demo", "coach",
            scope={"team_id": self.teams["home"]["id"]}, actor_id="test_seed")
        c = self._login("httpwithdraw2")
        status, body = self._post(
            c, f"/api/games/{self.game['id']}/substitutes/withdraw",
            {"player_id": mover["id"]})
        self.assertEqual(status, 403, body)
        self.assertEqual(body["error"]["code"], "forbidden", body)
        self.assertEqual(body["error"]["message"],
                         "A coach can only manage their own team's players.",
                         body)

    def test_availability_summary_own_team_resolves_through_membership(self):
        # #205 blocker 1 also covers the availability-summary sub-scope
        # (server.py, own_team resolved via game_scoped_own_team_id): a
        # Mover's default (no ?team_id=) summary must be their RESOLVED
        # side, and naming the opponent must not reach it.
        #
        # #427 final blocker, round 2: the opponent probe used to assert 403.
        # This leaf now IGNORES a scoped caller's side hint rather than
        # refusing it, matching its four private-game siblings -- so the
        # assertion is the STRONGER one that the hinted response IS the
        # Mover's own resolved side, byte-for-byte the un-hinted answer. The
        # property this test exists for (a Mover's side comes from their
        # membership, and the opponent's summary is not reachable) is
        # unchanged and now pinned on the body rather than on a status code.
        mover = self.f._pointer_only_player(
            self.api, self.teams["third"]["id"], "HTTP AvailMover")
        self.f._membership(self.api, mover["id"], self.ls_id,
                           self.teams["home"]["id"])
        self.api.accounts.create_account(
            "httpavail", "demo", "player",
            scope={"player_id": mover["id"]}, actor_id="test_seed")
        c = self._login("httpavail")
        status, body = self._get(
            c, f"/api/games/{self.game['id']}/availability-summary")
        self.assertEqual(status, 200, body)
        self.assertEqual(body.get("team_id"), self.teams["home"]["id"], body)
        status, hinted = self._get(
            c, f"/api/games/{self.game['id']}/availability-summary"
              f"?team_id={self.teams['away']['id']}")
        self.assertEqual(status, 200, hinted)
        self.assertEqual(
            hinted, body,
            "?team_id=<opponent> changed the response -- a client hint "
            "selected a side")
        self.assertEqual(hinted.get("team_id"), self.teams["home"]["id"],
                         hinted)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
