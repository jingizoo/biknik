"""Reschedule request / approval workflow (#29).

A controlled way to move a PUBLISHED game — distinct from the operator-only
`move_game` drag/drop. A coach on either team requests a reschedule with a
reason; the OPPONENT team's coach must accept before it ever reaches league
review; a league admin/arena manager then approves (supplying a replacement
ice slot — reusing move_game's own one-game-per-slot/team-overlap guarantees
verbatim) or denies. Republishing on approval is immediate: by that point
both the opponent and the league have signed off, unlike a raw move_game
drag/drop which conservatively unpublishes pending human review.

Two layers are covered: the service (state machine, eligibility gates,
audit, notifications) and the HTTP routes (permission gating, the
opponent-only respond guard, server-resolved actor attribution, and a full
create -> accept -> approve -> republished flow over real HTTP).
"""

import itertools
import json
import threading
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timezone
from http.cookiejar import CookieJar
from http.server import ThreadingHTTPServer

from helpers import BACKEND, FakeClock  # noqa: F401  (BACKEND sets up sys.path)

from hockey_scheduler.domain.errors import (
    InvalidTransitionError,
    ScheduleConflictError,
    ValidationError,
)
from hockey_scheduler.services import SetupService
from hockey_scheduler.store import InMemoryStore
from hockey_scheduler.web import server as srv

UTC = timezone.utc


def dt(h, m=0):
    return datetime(2026, 9, 1, h, m, tzinfo=UTC)


def universe():
    store = InMemoryStore()
    svc = SetupService(store, clock=FakeClock())
    league = svc.create_league("L")
    season = svc.create_season(league.id, "S")
    div = svc.create_division(season.id, "U16")
    home = svc.create_team(svc.create_club("LH").id, div.id, "Lions")
    away = svc.create_team(svc.create_club("FH").id, div.id, "Falcons")
    other = svc.create_team(svc.create_club("OH").id, div.id, "Otters")
    venue = svc.create_venue("Arena")
    rink = svc.create_rink(venue.id, "Rink 1")
    slot_a = svc.create_ice_slot(rink.id, dt(18, 30), dt(20))       # game lives here
    slot_b = svc.create_ice_slot(rink.id, dt(20, 30), dt(22))       # free target
    game = svc.create_game(season.id, div.id, home.id, away.id, slot_a.id)
    svc.publish_game(game.id)
    return dict(store=store, svc=svc, season=season, div=div, home=home,
               away=away, other=other, rink=rink, slot_a=slot_a, slot_b=slot_b,
               game=game)


class RequestRescheduleTest(unittest.TestCase):
    def test_request_starts_pending_opponent(self):
        c = universe()
        req = c["svc"].request_reschedule(c["game"].id, c["home"].id, "weather")
        self.assertEqual(req.status.value, "pending_opponent")
        self.assertEqual(req.requested_by_team_id, c["home"].id)
        self.assertEqual(req.reason, "weather")

    def test_away_team_may_also_request(self):
        c = universe()
        req = c["svc"].request_reschedule(c["game"].id, c["away"].id, "travel")
        self.assertEqual(req.requested_by_team_id, c["away"].id)

    def test_blocks_a_team_not_in_this_game(self):
        c = universe()
        with self.assertRaises(ValidationError):
            c["svc"].request_reschedule(c["game"].id, c["other"].id, "x")

    def test_blocks_an_unpublished_game(self):
        c = universe()
        draft = c["svc"].create_game(c["season"].id, c["div"].id, c["home"].id,
                                     c["away"].id, c["slot_b"].id)
        with self.assertRaises(ValidationError):
            c["svc"].request_reschedule(draft.id, c["home"].id, "x")

    def test_blocks_a_cancelled_game(self):
        c = universe()
        c["store"].get_game(c["game"].id).cancelled = True
        with self.assertRaises(ValidationError):
            c["svc"].request_reschedule(c["game"].id, c["home"].id, "x")

    def test_requires_a_reason(self):
        c = universe()
        with self.assertRaises(ValidationError):
            c["svc"].request_reschedule(c["game"].id, c["home"].id, "   ")

    def test_blocks_a_second_open_request_on_the_same_game(self):
        c = universe()
        c["svc"].request_reschedule(c["game"].id, c["home"].id, "first")
        with self.assertRaises(ScheduleConflictError) as ex:
            c["svc"].request_reschedule(c["game"].id, c["away"].id, "second")
        self.assertEqual(ex.exception.details.get("reason"), "reschedule_already_open")

    def test_a_new_request_is_allowed_after_a_terminal_rejection(self):
        c = universe()
        req = c["svc"].request_reschedule(c["game"].id, c["home"].id, "first")
        c["svc"].respond_to_reschedule(req.id, False)
        req2 = c["svc"].request_reschedule(c["game"].id, c["home"].id, "second")
        self.assertEqual(req2.status.value, "pending_opponent")

    def test_requested_notifies_the_opponent_coach(self):
        c = universe()
        c["svc"].request_reschedule(c["game"].id, c["home"].id, "weather")
        kinds = [n.kind.value for n in c["store"].all_notifications_feed()]
        self.assertIn("reschedule_requested", kinds)
        n = next(n for n in c["store"].all_notifications_feed()
                if n.kind.value == "reschedule_requested")
        self.assertEqual(n.audience_ref, c["away"].id)

    def test_request_is_audited(self):
        c = universe()
        c["svc"].request_reschedule(c["game"].id, c["home"].id, "weather",
                                    actor_id="coach_home")
        entry = next(a for a in c["store"].all_setup_audit()
                    if a.action == "reschedule_requested")
        self.assertEqual(entry.actor_id, "coach_home")
        self.assertEqual(entry.detail["requested_by_team_id"], c["home"].id)


class RespondToRescheduleTest(unittest.TestCase):
    def setUp(self):
        self.c = universe()
        self.req = self.c["svc"].request_reschedule(
            self.c["game"].id, self.c["home"].id, "weather")

    def test_accept_moves_to_pending_league_approval(self):
        req = self.c["svc"].respond_to_reschedule(self.req.id, True)
        self.assertEqual(req.status.value, "pending_league_approval")
        self.assertIsNotNone(req.opponent_responded_at)

    def test_reject_is_terminal(self):
        req = self.c["svc"].respond_to_reschedule(self.req.id, False)
        self.assertEqual(req.status.value, "opponent_rejected")

    def test_cannot_respond_twice(self):
        self.c["svc"].respond_to_reschedule(self.req.id, True)
        with self.assertRaises(InvalidTransitionError):
            self.c["svc"].respond_to_reschedule(self.req.id, True)

    def test_accept_notifies_the_scheduler(self):
        self.c["svc"].respond_to_reschedule(self.req.id, True)
        kinds = [n.kind.value for n in self.c["store"].all_notifications_feed()]
        self.assertIn("reschedule_accepted", kinds)

    def test_reject_notifies_the_requesting_coach(self):
        self.c["svc"].respond_to_reschedule(self.req.id, False)
        n = next(n for n in self.c["store"].all_notifications_feed()
                if n.kind.value == "reschedule_rejected")
        self.assertEqual(n.audience_ref, self.c["home"].id)


class DecideRescheduleTest(unittest.TestCase):
    def setUp(self):
        self.c = universe()
        req = self.c["svc"].request_reschedule(
            self.c["game"].id, self.c["home"].id, "weather")
        self.req = self.c["svc"].respond_to_reschedule(req.id, True)

    def test_approve_republishes_the_game_on_the_new_slot(self):
        req = self.c["svc"].decide_reschedule(
            self.req.id, True, new_ice_slot_id=self.c["slot_b"].id,
            actor_id="league_admin")
        self.assertEqual(req.status.value, "republished")
        self.assertEqual(req.new_ice_slot_id, self.c["slot_b"].id)
        game = self.c["store"].get_game(self.c["game"].id)
        self.assertEqual(game.ice_slot_id, self.c["slot_b"].id)
        self.assertTrue(game.published)

    def test_approve_frees_the_old_slot(self):
        self.c["svc"].decide_reschedule(
            self.req.id, True, new_ice_slot_id=self.c["slot_b"].id)
        old_slot = self.c["store"].get_ice_slot(self.c["slot_a"].id)
        self.assertEqual(old_slot.status.value, "available")

    def test_approve_without_a_slot_is_rejected(self):
        with self.assertRaises(ValidationError):
            self.c["svc"].decide_reschedule(self.req.id, True)

    def test_approve_into_an_occupied_slot_is_a_conflict_and_leaves_request_pending(self):
        # Allocate slot_b with an unrelated game first.
        self.c["svc"].create_game(self.c["season"].id, self.c["div"].id,
                                  self.c["home"].id, self.c["away"].id,
                                  self.c["slot_b"].id)
        with self.assertRaises(ScheduleConflictError):
            self.c["svc"].decide_reschedule(
                self.req.id, True, new_ice_slot_id=self.c["slot_b"].id)
        # The request is untouched — retriable with a different slot.
        reloaded = self.c["store"].get_reschedule_request(self.req.id)
        self.assertEqual(reloaded.status.value, "pending_league_approval")
        self.assertIsNone(reloaded.new_ice_slot_id)

    def test_reapproving_after_a_partial_failure_completes_instead_of_erroring(self):
        # move_game/publish_game are each their own committed transaction —
        # simulate a crash between them landing the game moved-but-unpublished
        # while the request is still PENDING_LEAGUE_APPROVAL (as if the first
        # decide_reschedule call died right after move_game returned).
        self.c["svc"].move_game(self.c["game"].id, self.c["slot_b"].id,
                                reason="Reschedule approved: weather")
        reloaded = self.c["store"].get_reschedule_request(self.req.id)
        self.assertEqual(reloaded.status.value, "pending_league_approval")  # still stuck
        game = self.c["store"].get_game(self.c["game"].id)
        self.assertFalse(game.published)  # move_game unpublished it

        # Re-approving with the SAME slot must complete, not hit move_game's
        # "already in that slot" guard.
        req = self.c["svc"].decide_reschedule(
            self.req.id, True, new_ice_slot_id=self.c["slot_b"].id)
        self.assertEqual(req.status.value, "republished")
        game = self.c["store"].get_game(self.c["game"].id)
        self.assertTrue(game.published)

    def test_deny_is_terminal_and_records_a_note(self):
        req = self.c["svc"].decide_reschedule(
            self.req.id, False, note="no ice available")
        self.assertEqual(req.status.value, "denied")
        self.assertEqual(req.decision_note, "no ice available")
        game = self.c["store"].get_game(self.c["game"].id)
        self.assertEqual(game.ice_slot_id, self.c["slot_a"].id)  # untouched

    def test_cannot_decide_twice(self):
        self.c["svc"].decide_reschedule(self.req.id, False)
        with self.assertRaises(InvalidTransitionError):
            self.c["svc"].decide_reschedule(self.req.id, False)

    def test_cannot_decide_before_opponent_responds(self):
        c = universe()
        req = c["svc"].request_reschedule(c["game"].id, c["home"].id, "weather")
        with self.assertRaises(InvalidTransitionError):
            c["svc"].decide_reschedule(req.id, True, new_ice_slot_id=c["slot_b"].id)

    def test_deny_notifies_both_coaches(self):
        self.c["svc"].decide_reschedule(self.req.id, False)
        kinds = [n.kind.value for n in self.c["store"].all_notifications_feed()]
        self.assertIn("reschedule_denied", kinds)

    def test_approve_reuses_game_moved_and_published_notifications(self):
        self.c["svc"].decide_reschedule(
            self.req.id, True, new_ice_slot_id=self.c["slot_b"].id)
        kinds = [n.kind.value for n in self.c["store"].all_notifications_feed()]
        self.assertIn("game_moved", kinds)
        self.assertIn("game_published", kinds)

    def test_republished_is_audited(self):
        self.c["svc"].decide_reschedule(
            self.req.id, True, new_ice_slot_id=self.c["slot_b"].id,
            actor_id="league_admin")
        entry = next(a for a in self.c["store"].all_setup_audit()
                    if a.action == "reschedule_republished")
        self.assertEqual(entry.actor_id, "league_admin")
        self.assertEqual(entry.detail["new_ice_slot_id"], self.c["slot_b"].id)


class ListRescheduleRequestsTest(unittest.TestCase):
    def test_lists_newest_first(self):
        c = universe()
        r1 = c["svc"].request_reschedule(c["game"].id, c["home"].id, "first")
        c["svc"].respond_to_reschedule(r1.id, False)
        r2 = c["svc"].request_reschedule(c["game"].id, c["home"].id, "second")
        rows = c["svc"].list_reschedule_requests(c["game"].id)
        self.assertEqual([r.id for r in rows], [r2.id, r1.id])

    def test_scoped_to_one_game(self):
        c = universe()
        other_game = c["svc"].create_game(c["season"].id, c["div"].id,
                                          c["home"].id, c["other"].id,
                                          c["slot_b"].id)
        c["svc"].publish_game(other_game.id)
        c["svc"].request_reschedule(c["game"].id, c["home"].id, "a")
        c["svc"].request_reschedule(other_game.id, c["home"].id, "b")
        self.assertEqual(len(c["svc"].list_reschedule_requests(c["game"].id)), 1)
        self.assertEqual(len(c["svc"].list_reschedule_requests()), 2)


class RescheduleHttpTest(unittest.TestCase):
    """Route-level permission/scope gating, actor attribution, and the full
    request -> accept -> approve -> republished flow over real HTTP."""

    @classmethod
    def setUpClass(cls):
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

    def _login(self, username):
        c = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(CookieJar()))
        self._post(c, "/api/auth/login", {"username": username, "password": "demo"})
        return c

    def _post(self, opener, path, body):
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=json.dumps(body).encode(), method="POST",
            headers={"Content-Type": "application/json"})
        try:
            with opener.open(req) as r:
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def _get(self, opener, path):
        try:
            with opener.open(f"http://127.0.0.1:{self.port}{path}") as r:
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    _game_counter = itertools.count(1)

    def _fresh_published_game(self, admin):
        """A brand-new published game, distinct per call (its own ice slot on
        its own day) so each test's reschedule request never collides with
        another test's "one open request per game" state — setUpClass resets
        the demo store only once for the whole class, so state persists
        across test methods."""
        day = 10 + next(self._game_counter)
        _, slot = self._post(admin, "/api/setup/ice-slot", {
            "rink_id": srv.STATE.ids["main_rink_id"],
            "start_time": f"2026-10-{day:02d}T18:30:00+00:00",
            "end_time": f"2026-10-{day:02d}T20:00:00+00:00"})
        _, game = self._post(admin, "/api/setup/game", {
            "season_id": srv.STATE.ids["season_id"],
            "division_id": srv.STATE.ids["division_id"],
            "home_team_id": srv.STATE.ids["home_team_id"],
            "away_team_id": srv.STATE.ids["away_team_id"],
            "ice_slot_id": slot["id"]})
        self._post(admin, f"/api/games/{game['id']}/publish", {})
        return game["id"]

    def test_coach_can_request_for_their_own_team_only(self):
        admin = self._login("admin")
        gid = self._fresh_published_game(admin)
        home_team = srv.STATE.ids["home_team_id"]
        away_team = srv.STATE.ids["away_team_id"]
        coach = self._login("coach")  # bound to home_team_id
        status, body = self._post(
            coach, f"/api/games/{gid}/reschedule/request",
            {"team_id": away_team, "reason": "x"})
        self.assertEqual(status, 403)
        status, req = self._post(
            coach, f"/api/games/{gid}/reschedule/request",
            {"team_id": home_team, "reason": "weather"})
        self.assertEqual(status, 200)
        self.assertEqual(req["status"], "pending_opponent")

    def test_player_role_cannot_request(self):
        gid = srv.STATE.ids["game_id"]
        home_team = srv.STATE.ids["home_team_id"]
        player = self._login("player")
        status, _ = self._post(
            player, f"/api/games/{gid}/reschedule/request",
            {"team_id": home_team, "reason": "x"})
        self.assertEqual(status, 403)

    def test_requesting_coach_cannot_respond_to_their_own_request(self):
        admin = self._login("admin")
        gid = self._fresh_published_game(admin)
        home_team = srv.STATE.ids["home_team_id"]
        coach = self._login("coach")
        _, req = self._post(coach, f"/api/games/{gid}/reschedule/request",
                            {"team_id": home_team, "reason": "weather"})
        status, body = self._post(
            coach, f"/api/games/{gid}/reschedule/{req['id']}/respond",
            {"accept": True})
        self.assertEqual(status, 403)

    def test_league_admin_cannot_be_blocked_from_responding(self):
        # Operators are unscoped — an operator may respond on the
        # opponent's behalf if needed (e.g. the opponent coach is unreachable).
        admin = self._login("admin")
        gid = self._fresh_published_game(admin)
        home_team = srv.STATE.ids["home_team_id"]
        coach = self._login("coach")
        _, req = self._post(coach, f"/api/games/{gid}/reschedule/request",
                            {"team_id": home_team, "reason": "weather"})
        status, body = self._post(
            admin, f"/api/games/{gid}/reschedule/{req['id']}/respond",
            {"accept": True})
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "pending_league_approval")

    def test_arena_manager_can_also_respond_as_operator_fallback(self):
        # Arena Manager holds MANAGE_SCHEDULE but NOT MANAGE_ROSTER — this
        # would previously 403 before even reaching _reschedule_opponent_guard
        # because the coarse per-path permission check only accepted
        # MANAGE_ROSTER for this action. Caught in review.
        admin = self._login("admin")
        gid = self._fresh_published_game(admin)
        home_team = srv.STATE.ids["home_team_id"]
        coach = self._login("coach")
        arena = self._login("arena")
        _, req = self._post(coach, f"/api/games/{gid}/reschedule/request",
                            {"team_id": home_team, "reason": "weather"})
        status, body = self._post(
            arena, f"/api/games/{gid}/reschedule/{req['id']}/respond",
            {"accept": True})
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "pending_league_approval")

    def test_coach_cannot_decide(self):
        admin = self._login("admin")
        gid = self._fresh_published_game(admin)
        home_team = srv.STATE.ids["home_team_id"]
        coach = self._login("coach")
        _, req = self._post(coach, f"/api/games/{gid}/reschedule/request",
                            {"team_id": home_team, "reason": "weather"})
        status, _ = self._post(
            coach, f"/api/games/{gid}/reschedule/{req['id']}/decide",
            {"approve": False})
        self.assertEqual(status, 403)

    def test_reschedule_list_requires_private_game_data_access(self):
        gid = srv.STATE.ids["game_id"]
        anon = urllib.request.build_opener()
        status, _ = self._get(anon, f"/api/games/{gid}/reschedule")
        self.assertEqual(status, 401)
        viewer = self._login("viewer")
        status, _ = self._get(viewer, f"/api/games/{gid}/reschedule")
        self.assertEqual(status, 403)
        admin = self._login("admin")
        status, body = self._get(admin, f"/api/games/{gid}/reschedule")
        self.assertEqual(status, 200)
        self.assertIsInstance(body["requests"], list)

    def test_pending_queue_is_operator_only(self):
        coach = self._login("coach")
        status, _ = self._get(coach, "/api/reschedule/pending")
        self.assertEqual(status, 403)
        admin = self._login("admin")
        status, body = self._get(admin, "/api/reschedule/pending")
        self.assertEqual(status, 200)
        self.assertIsInstance(body["requests"], list)

    def test_full_flow_request_accept_approve_republished(self):
        admin = self._login("admin")
        gid = self._fresh_published_game(admin)
        home_team = srv.STATE.ids["home_team_id"]
        rink_id = srv.STATE.ids["main_rink_id"]
        coach = self._login("coach")

        _, req = self._post(coach, f"/api/games/{gid}/reschedule/request",
                            {"team_id": home_team, "reason": "weather"})
        self.assertEqual(req["status"], "pending_opponent")

        _, accepted = self._post(
            admin, f"/api/games/{gid}/reschedule/{req['id']}/respond",
            {"accept": True})
        self.assertEqual(accepted["status"], "pending_league_approval")

        status, pending = self._get(admin, "/api/reschedule/pending")
        self.assertEqual(status, 200)
        self.assertIn(req["id"], [r["id"] for r in pending["requests"]])

        _, new_slot = self._post(admin, "/api/setup/ice-slot", {
            "rink_id": rink_id, "start_time": "2026-09-20T18:30:00+00:00",
            "end_time": "2026-09-20T20:00:00+00:00"})

        status, decided = self._post(
            admin, f"/api/games/{gid}/reschedule/{req['id']}/decide",
            {"approve": True, "new_ice_slot_id": new_slot["id"]})
        self.assertEqual(status, 200)
        self.assertEqual(decided["status"], "republished")

        status, game = self._get(admin, f"/api/games/{gid}")
        self.assertEqual(status, 200)
        self.assertEqual(game["ice_slot_id"], new_slot["id"])
        self.assertTrue(game["published"])

    def test_decide_actor_is_server_resolved_not_client_supplied(self):
        admin = self._login("admin")
        gid = self._fresh_published_game(admin)
        home_team = srv.STATE.ids["home_team_id"]
        coach = self._login("coach")
        _, req = self._post(coach, f"/api/games/{gid}/reschedule/request",
                            {"team_id": home_team, "reason": "weather"})
        self._post(admin, f"/api/games/{gid}/reschedule/{req['id']}/respond",
                  {"accept": True})
        self._post(admin, f"/api/games/{gid}/reschedule/{req['id']}/decide",
                  {"approve": False, "actor_id": "spoofed_someone_else"})
        entry = next(a for a in srv.STATE.api.store.all_setup_audit()
                    if a.action == "reschedule_denied" and a.entity_id == gid)
        _, accts = self._get(admin, "/api/accounts")
        admin_id = next(a["id"] for a in accts["user_accounts"]
                        if a["username"] == "admin")
        self.assertEqual(entry.actor_id, admin_id)
        self.assertNotEqual(entry.actor_id, "spoofed_someone_else")


if __name__ == "__main__":
    unittest.main()
