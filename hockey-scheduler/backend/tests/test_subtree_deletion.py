"""Executable #429 subtree deletion contract across every store."""

from __future__ import annotations

import os
import copy
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer

from helpers import cookie_from_set_cookie

from hockey_scheduler.domain import (
    Game,
    IceSlot,
    IceSlotStatus,
    League,
    LeagueSeason,
    Division,
    Organization,
    Player,
    Position,
    Program,
    Rink,
    Role,
    Season,
    SeasonTeamRegistration,
    Team,
    UserAccount,
    Venue,
)
from hockey_scheduler.domain.errors import (
    NotAuthorizedError,
    NotFoundError,
    ValidationError,
)
from hockey_scheduler.services.subtree_deletion_service import (
    SubtreeDeletionService,
)
from hockey_scheduler.services.roster_service import RosterService
from hockey_scheduler.services.setup_service import SetupService
from hockey_scheduler.store import InMemoryStore
from hockey_scheduler.store.sql_store import SqlStore
from hockey_scheduler.web import server as srv


NOW = datetime(2026, 8, 29, 12, 0, tzinfo=timezone.utc)


def _seed(store):
    store.add_user_account(UserAccount(
        "admin", "admin", "opaque-hash", Role.LEAGUE_ADMIN, NOW, {}, True))
    store.add_user_account(UserAccount(
        "admin2", "admin2", "opaque-hash", Role.LEAGUE_ADMIN, NOW, {}, True))
    store.add_user_account(UserAccount(
        "viewer", "viewer", "opaque-hash", Role.VIEWER, NOW, {}, True))
    store.add_organization(Organization("org_delete", "North Facility"))
    store.add_organization(Organization("org_keep", "Shared Facility"))
    store.add_program(Program(
        "program_delete", "Adult Hockey",
        operator_organization_id="org_delete"))
    store.add_program(Program(
        "program_keep", "Junior Hockey", external_ref="KEEP-PROGRAM"))
    store.add_season(Season("season_delete", "program_delete", "2026"))
    store.add_league(League("league_delete", "program_delete", "A"))
    store.add_league_season(LeagueSeason(
        "league_season_delete", "league_delete", "season_delete"))
    store.add_division(Division(
        "division_delete", "league_season_delete", "Delete Division"))
    store.add_season(Season(
        "season_keep", "program_keep", "2026", external_ref="KEEP-SEASON"))
    store.add_league(League(
        "league_keep", "program_keep", "B", external_ref="KEEP-LEAGUE"))
    store.add_league_season(LeagueSeason(
        "league_season_keep", "league_keep", "season_keep"))
    store.add_division(Division(
        "division_keep", "league_season_keep", "Keep Division"))
    store.add_team(Team(
        "team_home", "Home", program_id="program_delete",
        league_id="league_delete"))
    store.add_team(Team(
        "team_away", "Away", program_id="program_delete",
        league_id="league_delete"))
    store.add_team(Team(
        "team_keep_home", "Keep Home", program_id="program_keep",
        league_id="league_keep", division_id="division_delete",
        external_ref="KEEP-TEAM-HOME"))
    store.add_team(Team(
        "team_keep_away", "Keep Away", program_id="program_keep",
        league_id="league_keep", external_ref="KEEP-TEAM-AWAY"))
    store.add_season_team_registration(SeasonTeamRegistration(
        "registration_keep", "league_season_keep", "team_keep_home",
        division_id="division_keep"))
    store.add_player(Player(
        "player_private", "team_home", "Private Player", Position.FORWARD,
        birthdate="2001-02-03", registration_number="SECRET-123"))

    store.add_venue(Venue(
        "venue_delete", "North Arena", organization_id="org_delete"))
    store.add_rink(Rink("rink_delete", "venue_delete", "North Ice"))
    store.add_ice_slot(IceSlot(
        "slot_delete", "rink_delete", NOW, NOW + timedelta(hours=1),
        status=IceSlotStatus.ALLOCATED))

    store.add_venue(Venue(
        "venue_keep", "Shared Arena", organization_id="org_keep",
        league_id="program_delete"))
    store.add_rink(Rink("rink_keep", "venue_keep", "Shared Ice"))
    store.add_ice_slot(IceSlot(
        "slot_keep", "rink_keep", NOW + timedelta(days=1),
        NOW + timedelta(days=1, hours=1), status=IceSlotStatus.ALLOCATED))

    # A wholly unrelated facility tree is a byte-for-byte survivor control;
    # unlike venue_keep/slot_keep it has no edge crossing the deletion boundary.
    store.add_organization(Organization("org_unrelated", "South Facility"))
    store.add_venue(Venue(
        "venue_unrelated", "South Arena", organization_id="org_unrelated"))
    store.add_rink(Rink("rink_unrelated", "venue_unrelated", "South Ice"))
    store.add_ice_slot(IceSlot(
        "slot_unrelated", "rink_unrelated", NOW + timedelta(days=3),
        NOW + timedelta(days=3, hours=1), status=IceSlotStatus.AVAILABLE))

    # This Game is outside a facility deletion and must be unscheduled rather
    # than erased when its slot disappears.
    store.add_game(Game(
        "game_facility", "team_home", NOW, away_team_id="team_away",
        rink="North Ice", end_time=NOW + timedelta(hours=1),
        season_id="season_delete", league_id="league_delete",
        ice_slot_id="slot_delete", published=True, is_draft=False))
    # This Game is inside a Program deletion; its shared slot survives and is
    # released back to available.
    store.add_game(Game(
        "game_program", "team_home", NOW + timedelta(days=1),
        away_team_id="team_away", rink="Shared Ice",
        end_time=NOW + timedelta(days=1, hours=1),
        season_id="season_delete", league_id="league_delete",
        ice_slot_id="slot_keep", published=True, is_draft=False))
    store.add_game(Game(
        "game_survivor", "team_keep_home", NOW + timedelta(days=2),
        away_team_id="team_keep_away", season_id="season_keep",
        league_id="league_keep", published=False, is_draft=True))
    return store


class SubtreeDeletionContract:
    def make_store(self):
        raise NotImplementedError

    def setUp(self):
        self.store = _seed(self.make_store())
        self.service = SubtreeDeletionService(self.store, lambda: NOW)

    def tearDown(self):
        self.store.close()

    def preview(self, kind="programs", root_id="program_delete", actor="admin"):
        return self.service.preview(actor, kind, root_id)

    def execute(self, preview, name="Adult Hockey", reason="Retire old league"):
        return self.service.execute(
            "admin", preview["challenge_token"], name, reason)

    def make_peer_store(self):
        """A second process-equivalent connection to the same durable state."""
        return self.store

    def _race_delete(self, action_builder, *, kind="programs",
                     root_id="program_delete", name="Adult Hockey"):
        """Park execution after graph locks, start a competing real writer."""
        preview = self.service.preview("admin", kind, root_id)
        peer = self.make_peer_store()
        locked = threading.Event()
        release = threading.Event()
        attempted = threading.Event()
        finished = threading.Event()
        outcomes = {}

        def hook(stage):
            if stage == "after_lock":
                locked.set()
                if not release.wait(10):
                    raise RuntimeError("race harness did not release deletion")

        deleter = SubtreeDeletionService(self.store, lambda: NOW,
                                         stage_hook=hook)
        action = action_builder(peer)

        def run_delete():
            try:
                outcomes["delete"] = deleter.execute(
                    "admin", preview["challenge_token"], name, "race test")
            except Exception as exc:  # assertion inspects the exact outcome
                outcomes["delete_error"] = exc

        def run_action():
            attempted.set()
            try:
                outcomes["action"] = action()
            except Exception as exc:  # a serialized loser is expected often
                outcomes["action_error"] = exc
            finally:
                finished.set()

        delete_thread = threading.Thread(target=run_delete)
        action_thread = threading.Thread(target=run_action)
        delete_thread.start()
        self.assertTrue(locked.wait(10), "delete never acquired graph locks")
        action_thread.start()
        self.assertTrue(attempted.wait(5), "competing writer never started")
        # Every store must keep the writer behind the held graph lock.  This is
        # a bounded negative assertion, not a scheduling sleep: completion is
        # the event under test and the deleter remains explicitly parked.
        self.assertFalse(finished.wait(0.15),
                         "competing writer escaped the graph lock")
        release.set()
        delete_thread.join(20)
        action_thread.join(20)
        self.assertFalse(delete_thread.is_alive(), "delete thread hung")
        self.assertFalse(action_thread.is_alive(), "writer thread hung")
        if peer is not self.store:
            peer.close()
        self.assertNotIn("delete_error", outcomes, outcomes)
        return outcomes

    def test_preview_is_exact_and_carries_no_child_payload(self):
        preview = self.preview()
        deleted = {
            (group["entity_type"], record_id)
            for group in preview["delete_groups"]
            for record_id in group["record_ids"]
        }
        self.assertIn(("programs", "program_delete"), deleted)
        self.assertIn(("players", "player_private"), deleted)
        self.assertIn(("games", "game_program"), deleted)
        self.assertIn(("games", "game_facility"), deleted)
        self.assertNotIn(("venues", "venue_keep"), deleted)
        rendered = repr(preview)
        self.assertNotIn("Private Player", rendered)
        self.assertNotIn("2001-02-03", rendered)
        self.assertNotIn("SECRET-123", rendered)
        self.assertNotIn(
            preview["challenge_token"], repr(self.store.subtree_all_rows()),
            "the raw bearer token must never be durable")
        self.assertEqual(preview["root"]["confirmation_name"], "Adult Hockey")

    def test_facility_subtree_detaches_shared_authority_and_preserves_game(self):
        preview = self.preview("organizations", "org_delete")
        self.assertEqual(
            {g["entity_type"] for g in preview["delete_groups"]},
            {"organizations", "venues", "rinks", "ice_slots"})
        self.assertEqual(
            {g["inventory_key"] for g in
             preview["detached_relationship_groups"]},
            {"programs.operator_organization_id", "games.ice_slot_id"})
        result = self.execute(preview, "North Facility", "Facility closed")
        self.assertEqual(result["result"], "success")
        self.assertIsNone(self.store.get_organization("org_delete"))
        self.assertIsNone(self.store.get_venue("venue_delete"))
        self.assertIsNone(self.store.get_rink("rink_delete"))
        self.assertIsNone(self.store.get_ice_slot("slot_delete"))
        self.assertIsNotNone(self.store.get_organization("org_keep"))
        self.assertIsNone(
            self.store.get_program("program_delete").operator_organization_id)
        game = self.store.get_game("game_facility")
        self.assertIsNotNone(game)
        self.assertIsNone(game.ice_slot_id)
        self.assertFalse(game.published)
        self.assertTrue(game.is_draft)
        self.assertEqual(game.rink, "")

        audit = self.store.all_setup_audit()[-1]
        self.assertEqual(audit.action, "subtree_deleted")
        self.assertEqual(audit.actor_id, "admin")
        self.assertEqual(audit.detail["reason"], "Facility closed")
        self.assertEqual(
            audit.detail["preview_fingerprint"], preview["fingerprint"])
        self.assertNotIn("North Arena", repr(audit.detail))

    def test_competition_delete_releases_but_does_not_delete_shared_ice(self):
        unrelated_before = copy.deepcopy((
            self.store.get_program("program_keep"),
            self.store.get_season("season_keep"),
            self.store.get_league("league_keep"),
            self.store.get_league_season("league_season_keep"),
            self.store.get_division("division_keep"),
            self.store.get_season_team_registration("registration_keep"),
            self.store.get_team("team_keep_home"),
            self.store.get_team("team_keep_away"),
            self.store.get_game("game_survivor"),
            self.store.get_organization("org_unrelated"),
            self.store.get_venue("venue_unrelated"),
            self.store.get_rink("rink_unrelated"),
            self.store.get_ice_slot("slot_unrelated"),
        ))
        preview = self.preview()
        self.execute(preview)
        self.assertIsNone(self.store.get_program("program_delete"))
        self.assertIsNone(self.store.get_game("game_program"))
        self.assertIsNotNone(self.store.get_venue("venue_keep"))
        self.assertIsNone(self.store.get_venue("venue_keep").league_id)
        self.assertEqual(
            self.store.get_ice_slot("slot_keep").status,
            IceSlotStatus.AVAILABLE)
        unrelated_after = (
            self.store.get_program("program_keep"),
            self.store.get_season("season_keep"),
            self.store.get_league("league_keep"),
            self.store.get_league_season("league_season_keep"),
            self.store.get_division("division_keep"),
            self.store.get_season_team_registration("registration_keep"),
            self.store.get_team("team_keep_home"),
            self.store.get_team("team_keep_away"),
            self.store.get_game("game_survivor"),
            self.store.get_organization("org_unrelated"),
            self.store.get_venue("venue_unrelated"),
            self.store.get_rink("rink_unrelated"),
            self.store.get_ice_slot("slot_unrelated"),
        )
        self.assertEqual(unrelated_after, unrelated_before)

    def test_retained_slot_with_surviving_active_game_is_not_released(self):
        # Model a persisted cancelled attachment while a surviving active Game
        # owns the same slot (the partial unique invariant permits only this
        # active/cancelled pairing).  The survivor is outside the ownership
        # closure but must join the preview fingerprint because it decides
        # whether the retained slot may be released.
        deleted_game = self.store.get_game("game_program")
        deleted_game.cancelled = True
        self.store.save_game(deleted_game)
        survivor = self.store.get_game("game_survivor")
        survivor.ice_slot_id = "slot_keep"
        survivor.rink = "Shared Ice"
        survivor.published = True
        survivor.is_draft = False
        self.store.save_game(survivor)
        slot = self.store.get_ice_slot("slot_keep")
        slot.status = IceSlotStatus.ALLOCATED
        self.store.save_ice_slot(slot)

        preview = self.preview()
        survivor.rink = "Changed after preview"
        self.store.save_game(survivor)
        with self.assertRaises(ValidationError) as changed:
            self.execute(preview)
        self.assertEqual(changed.exception.details["reason"], "preview_stale")

        self.execute(self.preview())
        self.assertEqual(
            self.store.get_game("game_survivor").ice_slot_id, "slot_keep")
        self.assertEqual(
            self.store.get_ice_slot("slot_keep").status,
            IceSlotStatus.ALLOCATED)

    def test_authorization_root_and_confirmation_fail_closed(self):
        with self.assertRaises(NotAuthorizedError):
            self.service.preview("viewer", "programs", "program_delete")
        with self.assertRaises(ValidationError) as unsupported:
            self.service.preview("admin", "games", "game_program")
        self.assertEqual(
            unsupported.exception.details["reason"], "unsupported_root_type")
        with self.assertRaises(NotFoundError):
            self.service.preview("admin", "programs", "absent")

        preview = self.preview()
        with self.assertRaises(ValidationError) as mismatch:
            self.execute(preview, "adult hockey")
        self.assertEqual(
            mismatch.exception.details["reason"], "confirmation_mismatch")
        self.assertIsNotNone(self.store.get_program("program_delete"))

        preview = self.preview()
        admin = self.store.get_user_account("admin")
        admin.active = False
        self.store.save_user_account(admin)
        with self.assertRaises(NotAuthorizedError):
            self.execute(preview)
        self.assertIsNotNone(self.store.get_program("program_delete"))

    def test_wrong_expired_replayed_and_stale_tokens_delete_nothing(self):
        preview = self.preview()
        with self.assertRaises(ValidationError) as wrong:
            self.service.execute("admin", "wrong", "Adult Hockey", "reason")
        self.assertEqual(wrong.exception.details["reason"], "invalid_challenge")
        self.assertIsNotNone(self.store.get_program("program_delete"))

        expiring = SubtreeDeletionService(
            self.store, lambda: NOW, challenge_ttl_seconds=-1)
        expired = expiring.preview("admin", "programs", "program_delete")
        with self.assertRaises(ValidationError) as expiry:
            expiring.execute(
                "admin", expired["challenge_token"], "Adult Hockey", "reason")
        self.assertEqual(expiry.exception.details["reason"], "invalid_challenge")

        stale = self.preview()
        self.store.add_team(Team(
            "late_team", "Late", program_id="program_delete",
            league_id="league_delete"))
        with self.assertRaises(ValidationError) as changed:
            self.execute(stale)
        self.assertEqual(changed.exception.details["reason"], "preview_stale")
        self.assertIsNotNone(self.store.get_program("program_delete"))
        self.assertIsNotNone(self.store.get_team("late_team"))

    def test_failure_at_every_execution_stage_rolls_back_the_whole_removal(self):
        stages = (
            "after_lock", "after_revalidation", "after_detach",
            "after_slot_release", "after_delete", "after_audit",
        )
        for failed_stage in stages:
            with self.subTest(stage=failed_stage):
                preview = self.preview()

                def fail(stage):
                    if stage == failed_stage:
                        raise RuntimeError("injected:" + stage)

                self.service._stage_hook = fail
                with self.assertRaisesRegex(RuntimeError, failed_stage):
                    self.execute(preview)
                self.service._stage_hook = lambda _stage: None
                self.assertIsNotNone(self.store.get_program("program_delete"))
                self.assertIsNotNone(self.store.get_team("team_home"))
                self.assertIsNotNone(self.store.get_game("game_program"))
                self.assertEqual(
                    self.store.get_venue("venue_keep").league_id,
                    "program_delete")
                self.assertEqual(
                    self.store.get_ice_slot("slot_keep").status,
                    IceSlotStatus.ALLOCATED)
                self.assertFalse(any(a.action == "subtree_deleted"
                                     for a in self.store.all_setup_audit()))
                # Capability consumption is deliberately one-shot even when
                # the destructive transaction rolls back: an uncertain or
                # failed attempt must be re-previewed, never replayed.
                with self.assertRaises(ValidationError):
                    self.execute(preview)

    def test_new_preview_supersedes_only_the_same_actor(self):
        other_actor = self.preview(
            "organizations", "org_unrelated", actor="admin2")
        first = self.preview()
        second = self.preview()
        with self.assertRaises(ValidationError):
            self.execute(first)
        self.execute(second)
        # Admin's replacement and execution did not consume admin2's unrelated
        # capability; challenge identity is actor-scoped, not global.
        self.service.execute(
            "admin2", other_actor["challenge_token"], "South Facility",
            "retire unrelated test facility")
        self.assertIsNone(self.store.get_organization("org_unrelated"))

    def test_challenge_is_actor_bound_and_single_use(self):
        preview = self.preview()
        with self.assertRaises(ValidationError) as wrong_actor:
            self.service.execute(
                "admin2", preview["challenge_token"], "Adult Hockey",
                "wrong actor")
        self.assertEqual(
            wrong_actor.exception.details["reason"], "invalid_challenge")
        self.execute(preview)
        with self.assertRaises(ValidationError) as replay:
            self.execute(preview)
        self.assertEqual(replay.exception.details["reason"], "invalid_challenge")

    def test_concurrent_child_creation_waits_then_refuses_without_orphan(self):
        def build(peer):
            setup = SetupService(peer)
            return lambda: setup.create_team(
                None, None, "Concurrent Child", "admin2",
                program_id="program_delete", league_id="league_delete")

        outcomes = self._race_delete(build)
        self.assertIn("action_error", outcomes, outcomes)
        self.assertFalse(any(t.name == "Concurrent Child"
                             for t in self.store.all_teams()))

    def test_concurrent_reassignment_waits_then_refuses_deleted_parent(self):
        def build(peer):
            setup = SetupService(peer)
            return lambda: setup.assign_rink_venue(
                "rink_keep", "venue_delete", "admin2")

        outcomes = self._race_delete(
            build, kind="organizations", root_id="org_delete",
            name="North Facility")
        self.assertIn("action_error", outcomes, outcomes)
        self.assertEqual(self.store.get_rink("rink_keep").venue_id,
                         "venue_keep")

    def test_concurrent_cancellation_waits_then_cannot_resurrect_game(self):
        def build(peer):
            roster = RosterService(peer, clock=lambda: NOW)
            return lambda: roster.cancel_game("game_program", "admin2")

        outcomes = self._race_delete(build)
        self.assertIn("action_error", outcomes, outcomes)
        self.assertIsNone(self.store.get_game("game_program"))

    def test_concurrent_slot_allocation_runs_after_release_without_lost_update(self):
        def build(peer):
            def allocate():
                with peer.transaction():
                    game = peer.get_game_for_update("game_survivor")
                    slot = peer.get_ice_slot_for_update("slot_keep")
                    if game is None or slot is None:
                        raise AssertionError("surviving placement rows vanished")
                    if slot.status is not IceSlotStatus.AVAILABLE:
                        raise AssertionError("delete did not release shared slot")
                    game.ice_slot_id = slot.id
                    game.rink = "Shared Ice"
                    slot.status = IceSlotStatus.ALLOCATED
                    peer.save_game(game)
                    peer.save_ice_slot(slot)
                    return game.id
            return allocate

        outcomes = self._race_delete(build)
        self.assertEqual(outcomes.get("action"), "game_survivor", outcomes)
        self.assertEqual(self.store.get_game("game_survivor").ice_slot_id,
                         "slot_keep")
        self.assertEqual(self.store.get_ice_slot("slot_keep").status,
                         IceSlotStatus.ALLOCATED)

    def test_two_competing_subtree_deletes_have_one_winner_and_one_audit(self):
        def build(peer):
            service = SubtreeDeletionService(peer, lambda: NOW)
            preview = service.preview("admin2", "programs", "program_delete")
            return lambda: service.execute(
                "admin2", preview["challenge_token"], "Adult Hockey",
                "competing delete")

        outcomes = self._race_delete(build)
        self.assertIn("action_error", outcomes, outcomes)
        audits = [a for a in self.store.all_setup_audit()
                  if a.action == "subtree_deleted"]
        self.assertEqual(len(audits), 1)


class TestSubtreeDeletionMemory(SubtreeDeletionContract, unittest.TestCase):
    def make_store(self):
        return InMemoryStore()

    def test_live_authorization_is_rechecked_after_graph_lock(self):
        preview = self.preview()
        original_lock = self.store.lock_subtree_graph

        def revoke_before_returning_from_lock():
            original_lock()
            admin = self.store.get_user_account("admin")
            admin.active = False
            self.store.save_user_account(admin)

        self.store.lock_subtree_graph = revoke_before_returning_from_lock
        with self.assertRaises(NotAuthorizedError):
            self.execute(preview)
        self.assertIsNotNone(self.store.get_program("program_delete"))


class TestSubtreeDeletionSQLite(SubtreeDeletionContract, unittest.TestCase):
    def make_store(self):
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        os.unlink(self.path)
        return SqlStore(self.path)

    def tearDown(self):
        super().tearDown()
        if os.path.exists(self.path):
            os.unlink(self.path)

    def make_peer_store(self):
        return SqlStore(self.path)


class TestSubtreeDeletionPostgres(SubtreeDeletionContract, unittest.TestCase):
    def make_store(self):
        dsn = os.environ.get("TEST_DATABASE_URL")
        if not dsn:
            self.skipTest("TEST_DATABASE_URL not configured")
        store = SqlStore(dsn)
        store.reset_schema()
        return store

    def make_peer_store(self):
        return SqlStore(os.environ["TEST_DATABASE_URL"])

    def test_real_postgres_backend_was_not_silently_substituted(self):
        self.assertEqual(self.store.backend, "postgres")


class TestSubtreeDeletionHttp(unittest.TestCase):
    """Real authenticated route, payload, and status-code contract."""

    @classmethod
    def setUpClass(cls):
        cls._database_url = os.environ.pop("DATABASE_URL", None)
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
        if cls._database_url is not None:
            os.environ["DATABASE_URL"] = cls._database_url

    def setUp(self):
        srv.STATE.reset()
        self.admin = srv.STATE.api.accounts.create_account(
            "subtree-admin", "subtree-password", Role.LEAGUE_ADMIN)
        self.viewer = srv.STATE.api.accounts.create_account(
            "subtree-viewer", "viewer-password", Role.VIEWER)
        srv.STATE.api.store.add_organization(
            Organization("http_org", "HTTP Facility"))
        srv.STATE.api.store.add_venue(
            Venue("http_venue", "Private child label",
                  organization_id="http_org"))

    def _req(self, path, body, cookie=None):
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=json.dumps(body).encode(), method="POST",
            headers={"Content-Type": "application/json"})
        if cookie:
            req.add_header("Cookie", cookie)
        try:
            with urllib.request.urlopen(req) as response:
                return response.status, json.loads(response.read() or b"{}")
        except urllib.error.HTTPError as exc:
            with exc:
                return exc.code, json.loads(exc.read() or b"{}")

    def _login(self, username, password):
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/api/auth/login",
            data=json.dumps({"username": username, "password": password}).encode(),
            method="POST", headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req) as response:
            return cookie_from_set_cookie(
                response.headers.get("Set-Cookie"), "hs_sid")

    def test_authentication_and_exact_admin_role_are_both_enforced(self):
        path = "/api/admin/subtree-deletion/preview"
        status, _ = self._req(
            path, {"root_type": "organizations", "root_id": "http_org"})
        self.assertEqual(status, 401)
        cookie = self._login("subtree-viewer", "viewer-password")
        status, body = self._req(
            path, {"root_type": "organizations", "root_id": "http_org"},
            cookie)
        self.assertEqual(status, 403)
        self.assertEqual(body["error"]["code"], "forbidden")

    def test_preview_and_execute_over_http_expose_no_child_payload(self):
        cookie = self._login("subtree-admin", "subtree-password")
        status, preview = self._req(
            "/api/admin/subtree-deletion/preview",
            {"root_type": "organizations", "root_id": "http_org"}, cookie)
        self.assertEqual(status, 200, preview)
        self.assertNotIn("Private child label", repr(preview))
        status, result = self._req(
            "/api/admin/subtree-deletion/execute",
            {"challenge_token": preview["challenge_token"],
             "typed_name": "HTTP Facility", "reason": "Facility closed"},
            cookie)
        self.assertEqual(status, 200, result)
        self.assertEqual(result["result"], "success")
        self.assertIsNone(srv.STATE.api.store.get_organization("http_org"))
        self.assertIsNone(srv.STATE.api.store.get_venue("http_venue"))


if __name__ == "__main__":
    unittest.main()
