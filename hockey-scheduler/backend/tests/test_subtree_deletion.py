"""Executable #429 subtree deletion contract across every store."""

from __future__ import annotations

import os
import copy
import json
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer

from helpers import cookie_from_set_cookie

from hockey_scheduler.domain import (
    ActiveContext,
    AvailabilityStatus,
    Club,
    Game,
    GameAvailability,
    IceSlot,
    IceSlotStatus,
    League,
    LeagueSeason,
    Division,
    Official,
    OfficialAssignment,
    OfficialRole,
    Organization,
    Player,
    Position,
    Program,
    Rink,
    Role,
    Season,
    SeasonStatus,
    SeasonTeamRegistration,
    SeasonVenueAccess,
    Team,
    UserAccount,
    Venue,
    SetupAuditLog,
)
from hockey_scheduler.api.service import ApiService
from hockey_scheduler.domain.errors import (
    ActiveContextRequiredError,
    ConcurrencyConflictError,
    NotAuthorizedError,
    NotFoundError,
    ValidationError,
)
from hockey_scheduler.services.subtree_deletion_service import (
    ALLOWED_ROOT_TYPES,
    DETACH_SOURCE_AUTHORIZATION,
    RETAINED_EFFECT_AUTHORIZATION,
    ROOT_TARGET_KIND,
    RetainedChangeEffect,
    RetainedEffectAuthority,
    SubtreeDeletionService,
)
from hockey_scheduler.services.subtree_preview import (
    EntityType,
    ProjectedEdge,
    REFERENCE_INVENTORY,
    RecordRef,
    TargetRemoval,
)
from hockey_scheduler.services.context_gate import LIFECYCLE_GATE
from hockey_scheduler.services.epoch_fence import EPOCH_FENCE_GLOBAL_KEY
from hockey_scheduler.services.roster_service import RosterService
from hockey_scheduler.services.setup_service import SetupService
from hockey_scheduler.store import InMemoryStore
from hockey_scheduler.store.sql_store import SPECS, SqlStore
from hockey_scheduler.web import server as srv


NOW = datetime(2026, 8, 29, 12, 0, tzinfo=timezone.utc)


def _subtree_service(store, clock=lambda: NOW, **kwargs):
    """Construct the service with the real ApiService authorization gate."""
    api = ApiService(store)
    return SubtreeDeletionService(
        store, clock, root_authorizer=api._authorize_subtree_root,
        boundary_authorizer=api._authorize_subtree_boundary, **kwargs)


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
    store.set_active_context(ActiveContext(
        "admin", "program_delete", "season_delete", NOW,
        league_id="league_delete"))
    store.set_active_context(ActiveContext(
        "admin2", "program_delete", "season_delete", NOW,
        league_id="league_delete"))
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
    store.add_user_account(UserAccount(
        "player_account", "private-player", "opaque-player-hash",
        Role.PLAYER, NOW, {"player_id": "player_private"}, True))

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
    store.add_setup_audit(SetupAuditLog(
        "audit_org_unrelated", "organization_created", "organization",
        "org_unrelated", NOW, actor_id="admin2"))
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
        self.service = _subtree_service(self.store)

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

        deleter = _subtree_service(self.store, stage_hook=hook)
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

    def test_facility_subtree_requires_cancellation_then_preserves_history(self):
        with self.assertRaises(ValidationError) as blocked:
            self.preview("organizations", "org_delete")
        self.assertEqual(
            blocked.exception.details["reason"],
            "game_cancellation_required")

        RosterService(self.store, clock=lambda: NOW).cancel_game(
            "game_facility", "admin")
        cancelled_before = copy.deepcopy(self.store.get_game("game_facility"))
        self.assertTrue(cancelled_before.cancelled)
        self.assertIsNone(cancelled_before.ice_slot_id)
        self.assertEqual(cancelled_before.cancelled_venue_name, "North Arena")

        preview = self.preview("organizations", "org_delete")
        self.assertEqual(
            {g["entity_type"] for g in preview["delete_groups"]},
            {"organizations", "venues", "rinks", "ice_slots"})
        self.assertEqual(
            {g["inventory_key"] for g in
             preview["detached_relationship_groups"]},
            {"programs.operator_organization_id"})
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
        self.assertEqual(game, cancelled_before)

        audit = self.store.all_setup_audit()[-1]
        self.assertEqual(audit.action, "subtree_deleted")
        self.assertEqual(audit.actor_id, "admin")
        self.assertEqual(audit.detail["reason"], "Facility closed")
        self.assertEqual(
            audit.detail["preview_fingerprint"], preview["fingerprint"])
        self.assertNotIn("North Arena", repr(audit.detail))

    def test_facility_subtree_unplaces_only_a_clean_draft_and_discloses_it(self):
        game = self.store.get_game("game_facility")
        game.published = False
        game.is_draft = True
        self.store.save_game(game)

        preview = self.preview("organizations", "org_delete")
        self.assertIn({
            "effect": "draft_game_unplaced",
            "entity_type": "games",
            "count": 1,
            "record_ids": ["game_facility"],
        }, preview["retained_change_groups"])
        self.execute(preview, "North Facility", "Facility closed")
        game = self.store.get_game("game_facility")
        self.assertIsNone(game.ice_slot_id)
        self.assertEqual(game.rink, "")
        self.assertFalse(game.published)
        self.assertTrue(game.is_draft)

    def test_facility_subtree_refuses_a_draft_with_availability_history(self):
        game = self.store.get_game("game_facility")
        game.published = False
        game.is_draft = True
        self.store.save_game(game)
        self.store.save_availability(GameAvailability(
            "availability_facility", game.id, "player_private",
            AvailabilityStatus.AVAILABLE, responded_at=NOW))

        with self.assertRaises(ValidationError) as blocked:
            self.preview("organizations", "org_delete")
        self.assertEqual(
            blocked.exception.details["reason"],
            "game_cancellation_required")
        self.assertEqual(
            self.store.get_game(game.id).ice_slot_id, "slot_delete")
        self.assertIsNotNone(
            self.store.availability_for_player(
                game.id, "player_private"))

    def test_clean_draft_owned_state_axis_is_inventory_derived(self):
        game = self.store.get_game("game_facility")
        game.published = False
        game.is_draft = True
        target = RecordRef(EntityType.GAME, game.id, "0" * 64)
        relations = tuple(
            relation for relation in REFERENCE_INVENTORY
            if (EntityType.GAME in relation.targets
                and relation.on_target_delete
                is TargetRemoval.DELETE_SOURCE))
        self.assertGreater(len(relations), 0)
        self.assertTrue(self.service._game_is_clean_draft(game, ()))
        checked = 0
        for relation in relations:
            with self.subTest(inventory_key=relation.key):
                source = RecordRef(
                    relation.source, f"dependent-{checked}", "1" * 64)
                edge = ProjectedEdge(relation.key, source, target)
                self.assertFalse(
                    self.service._game_is_clean_draft(game, (edge,)))
                checked += 1
        self.assertEqual(checked, len(relations))

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
        effects = {
            (group["effect"], group["entity_type"]): group["record_ids"]
            for group in preview["retained_change_groups"]
        }
        self.assertEqual(
            effects[("ice_slot_released", "ice_slots")],
            ["slot_delete", "slot_keep"])
        self.assertEqual(
            effects[("user_account_deactivated", "user_accounts")],
            ["player_account"])
        result = self.execute(preview)
        self.assertIsNone(self.store.get_program("program_delete"))
        self.assertIsNone(self.store.get_game("game_program"))
        self.assertIsNotNone(self.store.get_venue("venue_keep"))
        self.assertIsNone(self.store.get_venue("venue_keep").league_id)
        self.assertEqual(
            self.store.get_ice_slot("slot_keep").status,
            IceSlotStatus.AVAILABLE)
        account = self.store.get_user_account("player_account")
        self.assertFalse(account.active)
        self.assertNotIn("player_id", account.scope)
        self.assertEqual(result["retained_change_counts"], {
            "ice_slot_released": 2,
            "user_account_deactivated": 1,
        })
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

    def test_deleted_game_releases_its_reservation_on_shared_foreign_ice(self):
        venue = self.store.get_venue("venue_keep")
        venue.league_id = "program_keep"
        self.store.save_venue(venue)

        preview = self.preview()
        released = next(
            group for group in preview["retained_change_groups"]
            if group["effect"] == "ice_slot_released")
        self.assertIn("slot_keep", released["record_ids"])
        result = self.execute(preview)

        self.assertEqual(result["result"], "success")
        self.assertIsNone(self.store.get_game("game_program"))
        self.assertIsNotNone(self.store.get_venue("venue_keep"))
        self.assertEqual(
            self.store.get_venue("venue_keep").league_id,
            "program_keep")
        self.assertEqual(
            self.store.get_ice_slot("slot_keep").status,
            IceSlotStatus.AVAILABLE)

    def test_seasonless_exhibition_reservation_cleanup_uses_team_program(self):
        venue = self.store.get_venue("venue_keep")
        venue.league_id = "program_keep"
        self.store.save_venue(venue)
        self.store.add_team(Team(
            "team_exhibition", "Exhibition Team",
            program_id="program_delete"))
        self.store.add_ice_slot(IceSlot(
            "slot_exhibition", "rink_keep", NOW + timedelta(days=7),
            NOW + timedelta(days=7, hours=1),
            status=IceSlotStatus.ALLOCATED))
        self.store.add_game(Game(
            "game_exhibition", "team_exhibition", NOW + timedelta(days=7),
            away_team_id="team_exhibition", rink="Shared Ice",
            end_time=NOW + timedelta(days=7, hours=1),
            ice_slot_id="slot_exhibition", published=False, is_draft=True))

        preview = self.preview("teams", "team_exhibition")
        result = self.execute(
            preview, "Exhibition Team", "retire exhibition team")

        self.assertEqual(result["result"], "success")
        self.assertIsNone(self.store.get_team("team_exhibition"))
        self.assertIsNone(self.store.get_game("game_exhibition"))
        self.assertEqual(
            self.store.get_ice_slot("slot_exhibition").status,
            IceSlotStatus.AVAILABLE)
        self.assertEqual(
            self.store.get_venue("venue_keep").league_id,
            "program_keep")

    def test_retained_slot_release_refuses_a_cross_program_game_graph(self):
        game = self.store.get_game("game_program")
        game.season_id = "season_keep"
        self.store.save_game(game)

        with self.assertRaises(NotFoundError) as refused:
            self.preview()
        self.assertEqual(refused.exception.details["reason"],
                         "root_not_found")
        self.assertIsNone(
            self.store.get_subtree_deletion_challenge("admin"))
        self.assertIsNotNone(self.store.get_program("program_delete"))
        self.assertIsNotNone(self.store.get_game("game_program"))
        self.assertEqual(
            self.store.get_ice_slot("slot_keep").status,
            IceSlotStatus.ALLOCATED)

    def test_retained_slot_release_rechecks_game_program_before_execute(self):
        preview = self.preview()
        game = self.store.get_game("game_program")
        game.season_id = "season_keep"
        self.store.save_game(game)

        with self.assertRaises(NotFoundError) as refused:
            self.execute(preview)
        self.assertEqual(refused.exception.details["reason"],
                         "root_not_found")
        self.assertIsNotNone(
            self.store.get_subtree_deletion_challenge("admin"))
        self.assertIsNotNone(self.store.get_program("program_delete"))
        self.assertIsNotNone(self.store.get_game("game_program"))
        self.assertEqual(
            self.store.get_ice_slot("slot_keep").status,
            IceSlotStatus.ALLOCATED)

    def test_legacy_numeric_account_scope_applies_disclosed_deactivation(self):
        self.store.add_player(Player(
            "123", "team_home", "Numeric Scope Player", Position.FORWARD))
        self.store.add_user_account(UserAccount(
            "numeric_scope_account", "numeric-scope", "opaque-hash",
            Role.PLAYER, NOW, {"player_id": 123}, True))

        preview = self.preview()
        effect = next(
            group for group in preview["retained_change_groups"]
            if group["effect"] == "user_account_deactivated")
        self.assertIn("numeric_scope_account", effect["record_ids"])

        self.execute(preview)
        account = self.store.get_user_account("numeric_scope_account")
        self.assertFalse(account.active)
        self.assertNotIn("player_id", account.scope)

    def test_every_changed_survivor_is_disclosed_by_edge_or_effect(self):
        preview = self.preview()
        before = {
            (type(row).__name__, row.id): copy.deepcopy(row)
            for row in self.store.subtree_all_rows()
        }
        deleted = {
            (group["entity_type"], record_id)
            for group in preview["delete_groups"]
            for record_id in group["record_ids"]
        }
        allowed_ids = {
            (edge["source_type"], edge["source_id"])
            for group in preview["detached_relationship_groups"]
            for edge in group["edges"]
        }
        allowed_ids.update(
            (group["entity_type"], record_id)
            for group in preview["retained_change_groups"]
            for record_id in group["record_ids"])

        self.execute(preview)
        after = {
            (type(row).__name__, row.id): row
            for row in self.store.subtree_all_rows()
        }
        table_by_model = {
            model.__name__: spec.table
            for model, spec in SPECS.items()
        }
        changed = {
            (table_by_model[model_name], record_id)
            for (model_name, record_id), old in before.items()
            if (model_name, record_id) in after
            and after[(model_name, record_id)] != old
        }
        self.assertFalse(changed & deleted)
        self.assertEqual(changed, allowed_ids)

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

        with self.assertRaises(NotFoundError) as foreign:
            self.service.preview("admin", "programs", "program_keep")
        with self.assertRaises(NotFoundError) as missing:
            self.service.preview("admin", "programs", "missing_program")
        self.assertEqual(
            str(foreign.exception).replace("program_keep", "<id>"),
            str(missing.exception).replace("missing_program", "<id>"))

        preview = self.preview()
        with self.assertRaises(ValidationError) as mismatch:
            self.execute(preview, "adult hockey")
        self.assertEqual(
            mismatch.exception.details["reason"], "confirmation_mismatch")
        self.assertIsNotNone(self.store.get_program("program_delete"))
        self.assertIsNotNone(
            self.store.get_subtree_deletion_challenge("admin"),
            "a correctable confirmation typo must not burn the preview")

        preview = self.preview()
        admin = self.store.get_user_account("admin")
        admin.active = False
        self.store.save_user_account(admin)
        with self.assertRaises(NotAuthorizedError):
            self.execute(preview)
        self.assertIsNotNone(self.store.get_program("program_delete"))

    def test_context_and_target_refusals_precede_graph_disclosure(self):
        original = self.store.subtree_all_rows
        calls = []

        def observed():
            calls.append(True)
            return original()

        self.store.subtree_all_rows = observed
        self.store.set_active_context(ActiveContext(
            "admin", None, None, NOW))
        with self.assertRaises(ActiveContextRequiredError):
            self.preview()
        self.assertEqual(calls, [])

        self.store.set_active_context(ActiveContext(
            "admin", "program_delete", "season_delete", NOW,
            league_id="league_delete"))
        with self.assertRaises(NotFoundError) as foreign:
            self.service.preview("admin", "programs", "program_keep")
        with self.assertRaises(NotFoundError) as missing:
            self.service.preview("admin", "programs", "missing_program")
        self.assertEqual(calls, [])
        self.assertEqual(
            str(foreign.exception).replace("program_keep", "<id>"),
            str(missing.exception).replace("missing_program", "<id>"))

    def test_execute_rechecks_current_context_and_keeps_unstarted_token(self):
        preview = self.preview()
        self.store.set_active_context(ActiveContext(
            "admin", "program_keep", "season_keep", NOW,
            league_id="league_keep"))
        with self.assertRaises(NotFoundError):
            self.execute(preview)
        self.assertIsNotNone(self.store.get_program("program_delete"))
        self.assertIsNotNone(
            self.store.get_subtree_deletion_challenge("admin"))

    def test_execute_rechecks_a_root_moved_outside_scope_after_preview(self):
        preview = self.preview("teams", "team_home")
        team = self.store.get_team("team_home")
        team.program_id = "program_keep"
        team.league_id = "league_keep"
        self.store.save_team(team)

        with self.assertRaises(NotFoundError):
            self.execute(preview, "Home")
        self.assertIsNotNone(self.store.get_team("team_home"))
        self.assertIsNotNone(
            self.store.get_subtree_deletion_challenge("admin"),
            "a target-scope refusal must not consume the preview")

    def test_execute_fences_before_context_graph_and_live_identity(self):
        preview = self.preview()
        events = []
        before = self.store.current_epoch_fence_version(
            EPOCH_FENCE_GLOBAL_KEY)
        original_fence = self.store.epoch_fence_acquire_exclusive
        original_authorizer = self.service._root_authorizer
        original_graph_lock = self.store.lock_subtree_graph
        original_account_lock = self.store.get_user_account_for_update

        def fence(key):
            events.append(("fence", key))
            return original_fence(key)

        def authorize(kind, record_id, account, phase):
            events.append(("authorize", phase))
            return original_authorizer(
                kind, record_id, account, phase)

        def graph_lock():
            events.append(("graph", None))
            return original_graph_lock()

        def account_lock(account_id):
            events.append(("account", account_id))
            return original_account_lock(account_id)

        self.store.epoch_fence_acquire_exclusive = fence
        self.service._root_authorizer = authorize
        self.store.lock_subtree_graph = graph_lock
        self.store.get_user_account_for_update = account_lock

        self.execute(preview)
        self.assertEqual(events, [
            ("fence", EPOCH_FENCE_GLOBAL_KEY),
            ("authorize", "context"),
            ("graph", None),
            ("account", "admin"),
            ("authorize", "target"),
        ])
        self.assertEqual(
            self.store.current_epoch_fence_version(
                EPOCH_FENCE_GLOBAL_KEY),
            before + 1)

    def test_api_execute_waits_for_the_global_scoped_read_gate(self):
        api = ApiService(self.store)
        preview = api.subtree_deletion_preview(
            "programs", "program_delete", actor_id="admin")
        self.assertNotIn("error", preview)
        outcome = {}
        finished = threading.Event()

        def run_execute():
            try:
                outcome["result"] = api.subtree_deletion_execute(
                    preview["challenge_token"], "Adult Hockey",
                    "global gate regression", actor_id="admin")
            except Exception as exc:
                outcome["error"] = exc
            finally:
                finished.set()

        ticket = LIFECYCLE_GATE.arrive()
        thread = threading.Thread(target=run_execute)
        try:
            with ticket.bind(EPOCH_FENCE_GLOBAL_KEY):
                thread.start()
                deadline = time.monotonic() + 2.0
                while (LIFECYCLE_GATE.stats()["waiting_writers"] < 1
                       and time.monotonic() < deadline):
                    time.sleep(0.01)
                self.assertEqual(
                    LIFECYCLE_GATE.stats()["waiting_writers"], 1,
                    "subtree deletion never joined the global lifecycle gate")
                self.assertFalse(
                    finished.is_set(),
                    "subtree deletion committed inside a scoped read hold")
                self.assertIsNotNone(
                    self.store.get_program("program_delete"))
        finally:
            thread.join(20)

        self.assertFalse(thread.is_alive(), "subtree deletion thread hung")
        self.assertNotIn("error", outcome, outcome)
        self.assertEqual(outcome["result"]["result"], "success")
        self.assertIsNone(self.store.get_program("program_delete"))

    def test_root_kind_authorization_mapping_is_total_and_canonical(self):
        self.assertEqual(set(ROOT_TARGET_KIND), set(ALLOWED_ROOT_TYPES))
        self.assertTrue(set(ROOT_TARGET_KIND.values()).issubset(
            ApiService._SETUP_TARGET_KINDS))

    def test_detach_source_authorization_mapping_is_total_and_canonical(self):
        detach_sources = {
            relation.source for relation in REFERENCE_INVENTORY
            if relation.on_target_delete is TargetRemoval.DETACH
        }
        self.assertEqual(
            set(DETACH_SOURCE_AUTHORIZATION), detach_sources)
        accepted = (ApiService._SETUP_TARGET_KINDS
                    | set(ApiService._SETUP_BRIDGE_TARGETS))
        for binding in DETACH_SOURCE_AUTHORIZATION.values():
            if binding is not None:
                self.assertIn(binding[0], accepted)

    def test_retained_effect_authorization_mapping_is_total_and_canonical(self):
        self.assertEqual(
            set(RETAINED_EFFECT_AUTHORIZATION), set(RetainedChangeEffect))
        expected_types = {
            RetainedChangeEffect.DRAFT_GAME_UNPLACED: EntityType.GAME,
            RetainedChangeEffect.USER_ACCOUNT_DEACTIVATED:
                EntityType.USER_ACCOUNT,
            RetainedChangeEffect.ICE_SLOT_RELEASED: EntityType.ICE_SLOT,
        }
        self.assertEqual(
            {effect: policy[0] for effect, policy in
             RETAINED_EFFECT_AUTHORIZATION.items()},
            expected_types)
        self.assertEqual(
            RETAINED_EFFECT_AUTHORIZATION[
                RetainedChangeEffect.ICE_SLOT_RELEASED][1],
            RetainedEffectAuthority.DELETED_GAME_RESERVATION)

    def test_foreign_surviving_detach_refuses_before_preview_disclosure(self):
        self.store.add_club(Club("club_cross_scope", "Shared Club"))
        local_team = self.store.get_team("team_home")
        local_team.club_id = "club_cross_scope"
        self.store.save_team(local_team)
        foreign_team = self.store.get_team("team_keep_home")
        foreign_team.club_id = "club_cross_scope"
        self.store.save_team(foreign_team)

        with self.assertRaises(NotFoundError) as refused:
            self.preview("clubs", "club_cross_scope")
        self.assertEqual(refused.exception.details["reason"],
                         "root_not_found")
        self.assertIsNone(
            self.store.get_subtree_deletion_challenge("admin"))
        self.assertEqual(
            self.store.get_team("team_keep_home").club_id,
            "club_cross_scope")

    def test_new_foreign_detach_after_preview_refuses_and_preserves_token(self):
        self.store.add_club(Club("club_boundary_move", "Boundary Club"))
        local_team = self.store.get_team("team_home")
        local_team.club_id = "club_boundary_move"
        self.store.save_team(local_team)
        preview = self.preview("clubs", "club_boundary_move")

        foreign_team = self.store.get_team("team_keep_home")
        foreign_team.club_id = "club_boundary_move"
        self.store.save_team(foreign_team)
        with self.assertRaises(NotFoundError) as refused:
            self.execute(preview, "Boundary Club", "retire shared club")
        self.assertEqual(refused.exception.details["reason"],
                         "root_not_found")
        self.assertIsNotNone(
            self.store.get_subtree_deletion_challenge("admin"),
            "a target-authority refusal must roll back token consumption")
        self.assertIsNotNone(self.store.get_club("club_boundary_move"))
        self.assertEqual(
            self.store.get_team("team_home").club_id,
            "club_boundary_move")
        self.assertEqual(
            self.store.get_team("team_keep_home").club_id,
            "club_boundary_move")

    def test_deleted_descendant_cannot_cross_into_a_foreign_program(self):
        self.store.add_organization(Organization(
            "org_cross_program", "Cross Program Facility"))
        program = self.store.get_program("program_delete")
        program.operator_organization_id = "org_cross_program"
        self.store.save_program(program)
        self.store.add_venue(Venue(
            "venue_foreign_program", "Foreign Arena",
            organization_id="org_cross_program", league_id="program_keep"))
        self.store.add_rink(Rink(
            "rink_foreign_program", "venue_foreign_program", "Foreign Ice"))
        self.store.add_ice_slot(IceSlot(
            "slot_foreign_program", "rink_foreign_program",
            NOW + timedelta(days=4), NOW + timedelta(days=4, hours=1),
            status=IceSlotStatus.AVAILABLE))

        with self.assertRaises(NotFoundError) as refused:
            self.preview("organizations", "org_cross_program")
        self.assertEqual(refused.exception.details["reason"],
                         "root_not_found")
        self.assertIsNone(
            self.store.get_subtree_deletion_challenge("admin"))
        self.assertIsNotNone(
            self.store.get_organization("org_cross_program"))
        self.assertIsNotNone(self.store.get_venue("venue_foreign_program"))
        self.assertEqual(
            self.store.get_venue("venue_foreign_program").league_id,
            "program_keep")

    def test_deleted_venue_cannot_cross_into_a_foreign_season_access(self):
        self.store.add_organization(Organization(
            "org_foreign_access", "Foreign Access Facility"))
        program = self.store.get_program("program_delete")
        program.operator_organization_id = "org_foreign_access"
        self.store.save_program(program)
        self.store.add_venue(Venue(
            "venue_foreign_access", "Foreign Access Arena",
            organization_id="org_foreign_access"))
        self.store.add_season_venue_access(SeasonVenueAccess(
            "access_foreign_program", "season_keep",
            "venue_foreign_access"))

        with self.assertRaises(NotFoundError) as refused:
            self.preview("organizations", "org_foreign_access")
        self.assertEqual(refused.exception.details["reason"],
                         "root_not_found")
        self.assertIsNone(
            self.store.get_subtree_deletion_challenge("admin"))
        self.assertIsNotNone(self.store.get_venue("venue_foreign_access"))
        self.assertIsNotNone(
            self.store.get_season_venue_access("access_foreign_program"))

    def test_deleted_venue_refuses_any_partially_dangling_access_shape(self):
        for suffix, legacy_program in (
                ("grant_only", None),
                ("legacy_and_grant", "program_delete")):
            with self.subTest(shape=suffix):
                organization_id = f"org_dangling_{suffix}"
                venue_id = f"venue_dangling_{suffix}"
                self.store.add_organization(Organization(
                    organization_id, f"Dangling {suffix} Facility"))
                program = self.store.get_program("program_delete")
                program.operator_organization_id = organization_id
                self.store.save_program(program)
                self.store.add_venue(Venue(
                    venue_id, f"Dangling {suffix} Arena",
                    organization_id=organization_id,
                    league_id=legacy_program))
                self.store.add_season_venue_access(SeasonVenueAccess(
                    f"access_valid_{suffix}", "season_delete", venue_id))
                self.store.add_season_venue_access(SeasonVenueAccess(
                    f"access_dangling_{suffix}", "missing_season", venue_id))

                with self.assertRaises(NotFoundError) as refused:
                    self.preview("organizations", organization_id)
                self.assertEqual(refused.exception.details["reason"],
                                 "root_not_found")
                self.assertIsNone(
                    self.store.get_subtree_deletion_challenge("admin"))
                self.assertIsNotNone(self.store.get_venue(venue_id))
                self.assertIsNotNone(self.store.get_season_venue_access(
                    f"access_dangling_{suffix}"))

    def test_deleted_venue_requires_every_legacy_and_access_link_in_scope(self):
        for suffix, legacy_program, access_season in (
                ("legacy_local_access_foreign",
                 "program_delete", "season_keep"),
                ("legacy_foreign_access_local",
                 "program_keep", "season_delete")):
            with self.subTest(shape=suffix):
                organization_id = f"org_mixed_{suffix}"
                venue_id = f"venue_mixed_{suffix}"
                self.store.add_organization(Organization(
                    organization_id, f"Mixed {suffix} Facility"))
                program = self.store.get_program("program_delete")
                program.operator_organization_id = organization_id
                self.store.save_program(program)
                self.store.add_venue(Venue(
                    venue_id, f"Mixed {suffix} Arena",
                    organization_id=organization_id,
                    league_id=legacy_program))
                self.store.add_season_venue_access(SeasonVenueAccess(
                    f"access_mixed_{suffix}", access_season, venue_id))

                with self.assertRaises(NotFoundError) as refused:
                    self.preview("organizations", organization_id)
                self.assertEqual(refused.exception.details["reason"],
                                 "root_not_found")
                self.assertIsNone(
                    self.store.get_subtree_deletion_challenge("admin"))
                self.assertIsNotNone(self.store.get_venue(venue_id))
                self.assertIsNotNone(self.store.get_season_venue_access(
                    f"access_mixed_{suffix}"))

    def test_direct_venue_root_with_only_local_links_can_be_deleted(self):
        self.store.add_venue(Venue(
            "venue_root_local", "Local Root Arena",
            organization_id="org_delete"))
        self.store.add_season_venue_access(SeasonVenueAccess(
            "access_root_local", "season_delete", "venue_root_local"))

        preview = self.preview("venues", "venue_root_local")
        result = self.execute(
            preview, "Local Root Arena", "retire local arena")
        self.assertEqual(result["result"], "success")
        self.assertIsNone(self.store.get_venue("venue_root_local"))
        self.assertIsNone(
            self.store.get_season_venue_access("access_root_local"))

    def test_direct_venue_root_refuses_a_foreign_or_dangling_sibling_link(self):
        for suffix, second_season in (
                ("foreign", "season_keep"),
                ("dangling", "missing_season")):
            with self.subTest(link=suffix):
                venue_id = f"venue_root_{suffix}"
                self.store.add_venue(Venue(
                    venue_id, f"{suffix.title()} Root Arena",
                    organization_id="org_delete"))
                self.store.add_season_venue_access(SeasonVenueAccess(
                    f"access_root_local_{suffix}", "season_delete", venue_id))
                self.store.add_season_venue_access(SeasonVenueAccess(
                    f"access_root_second_{suffix}", second_season, venue_id))

                with self.assertRaises(NotFoundError) as refused:
                    self.preview("venues", venue_id)
                self.assertEqual(refused.exception.details["reason"],
                                 "root_not_found")
                self.assertIsNone(
                    self.store.get_subtree_deletion_challenge("admin"))
                self.assertIsNotNone(self.store.get_venue(venue_id))
                self.assertIsNotNone(self.store.get_season_venue_access(
                    f"access_root_second_{suffix}"))

    def test_direct_venue_root_rechecks_new_foreign_link_on_execute(self):
        self.store.add_venue(Venue(
            "venue_root_move", "Moving Root Arena",
            organization_id="org_delete"))
        self.store.add_season_venue_access(SeasonVenueAccess(
            "access_root_move_local", "season_delete", "venue_root_move"))
        preview = self.preview("venues", "venue_root_move")

        self.store.add_season_venue_access(SeasonVenueAccess(
            "access_root_move_foreign", "season_keep", "venue_root_move"))
        with self.assertRaises(NotFoundError) as refused:
            self.execute(preview, "Moving Root Arena", "retire arena")
        self.assertEqual(refused.exception.details["reason"],
                         "root_not_found")
        self.assertIsNotNone(
            self.store.get_subtree_deletion_challenge("admin"),
            "root authority changes must not consume the challenge")
        self.assertIsNotNone(self.store.get_venue("venue_root_move"))
        self.assertIsNotNone(
            self.store.get_season_venue_access("access_root_move_foreign"))

    def test_direct_rink_root_with_only_local_venue_links_can_be_deleted(self):
        self.store.add_venue(Venue(
            "venue_rink_local", "Local Rink Venue",
            organization_id="org_delete"))
        self.store.add_season_venue_access(SeasonVenueAccess(
            "access_rink_local", "season_delete", "venue_rink_local"))
        self.store.add_rink(Rink(
            "rink_root_local", "venue_rink_local", "Local Root Ice"))
        self.store.add_ice_slot(IceSlot(
            "slot_rink_local", "rink_root_local", NOW + timedelta(days=6),
            NOW + timedelta(days=6, hours=1),
            status=IceSlotStatus.AVAILABLE))

        preview = self.preview("rinks", "rink_root_local")
        result = self.execute(
            preview, "Local Root Ice", "retire local rink")
        self.assertEqual(result["result"], "success")
        self.assertIsNone(self.store.get_rink("rink_root_local"))
        self.assertIsNone(self.store.get_ice_slot("slot_rink_local"))
        self.assertIsNotNone(self.store.get_venue("venue_rink_local"))
        self.assertIsNotNone(
            self.store.get_season_venue_access("access_rink_local"))

    def test_direct_rink_root_refuses_foreign_or_dangling_venue_links(self):
        for suffix, second_season in (
                ("foreign", "season_keep"),
                ("dangling", "missing_season")):
            with self.subTest(link=suffix):
                venue_id = f"venue_rink_{suffix}"
                rink_id = f"rink_root_{suffix}"
                self.store.add_venue(Venue(
                    venue_id, f"{suffix.title()} Rink Venue",
                    organization_id="org_delete"))
                self.store.add_season_venue_access(SeasonVenueAccess(
                    f"access_rink_local_{suffix}",
                    "season_delete", venue_id))
                self.store.add_season_venue_access(SeasonVenueAccess(
                    f"access_rink_second_{suffix}", second_season, venue_id))
                self.store.add_rink(Rink(
                    rink_id, venue_id, f"{suffix.title()} Root Ice"))

                with self.assertRaises(NotFoundError) as refused:
                    self.preview("rinks", rink_id)
                self.assertEqual(refused.exception.details["reason"],
                                 "root_not_found")
                self.assertIsNone(
                    self.store.get_subtree_deletion_challenge("admin"))
                self.assertIsNotNone(self.store.get_rink(rink_id))

    def test_direct_rink_root_rechecks_new_foreign_venue_link(self):
        self.store.add_venue(Venue(
            "venue_rink_move", "Moving Rink Venue",
            organization_id="org_delete"))
        self.store.add_season_venue_access(SeasonVenueAccess(
            "access_rink_move_local", "season_delete", "venue_rink_move"))
        self.store.add_rink(Rink(
            "rink_root_move", "venue_rink_move", "Moving Root Ice"))
        preview = self.preview("rinks", "rink_root_move")

        self.store.add_season_venue_access(SeasonVenueAccess(
            "access_rink_move_foreign", "season_keep", "venue_rink_move"))
        with self.assertRaises(NotFoundError) as refused:
            self.execute(preview, "Moving Root Ice", "retire rink")
        self.assertEqual(refused.exception.details["reason"],
                         "root_not_found")
        self.assertIsNotNone(
            self.store.get_subtree_deletion_challenge("admin"))
        self.assertIsNotNone(self.store.get_rink("rink_root_move"))
        self.assertIsNotNone(
            self.store.get_season_venue_access("access_rink_move_foreign"))

    def test_direct_official_root_with_only_local_assignments_can_be_deleted(self):
        self.store.add_official(Official("official_root_local", "Local Ref"))
        self.store.add_official_assignment(OfficialAssignment(
            "assignment_root_local", "game_program", "official_root_local",
            OfficialRole.REFEREE))

        preview = self.preview("officials", "official_root_local")
        result = self.execute(preview, "Local Ref", "retire official")
        self.assertEqual(result["result"], "success")
        self.assertIsNone(self.store.get_official("official_root_local"))
        self.assertIsNone(
            self.store.get_official_assignment("assignment_root_local"))
        self.assertIsNotNone(self.store.get_game("game_program"))

    def test_direct_official_root_may_span_seasons_in_the_same_program(self):
        self.store.add_season(Season(
            "season_official_other", "program_delete", "2027"))
        self.store.add_game(Game(
            "game_official_other", "team_home", NOW + timedelta(days=8),
            away_team_id="team_away", season_id="season_official_other",
            published=True, is_draft=False))
        self.store.add_official(Official(
            "official_root_multi_season", "Multi Season Ref"))
        self.store.add_official_assignment(OfficialAssignment(
            "assignment_multi_active", "game_program",
            "official_root_multi_season", OfficialRole.REFEREE))
        self.store.add_official_assignment(OfficialAssignment(
            "assignment_multi_other", "game_official_other",
            "official_root_multi_season", OfficialRole.REFEREE))

        preview = self.preview("officials", "official_root_multi_season")
        result = self.execute(
            preview, "Multi Season Ref", "retire official")
        self.assertEqual(result["result"], "success")
        self.assertIsNone(
            self.store.get_official("official_root_multi_season"))
        self.assertIsNone(
            self.store.get_official_assignment("assignment_multi_other"))
        self.assertIsNotNone(self.store.get_game("game_official_other"))

    def test_direct_official_root_refuses_foreign_or_dangling_assignment(self):
        for suffix, second_game in (
                ("foreign", "game_survivor"),
                ("dangling", "missing_game")):
            with self.subTest(link=suffix):
                official_id = f"official_root_{suffix}"
                self.store.add_official(Official(
                    official_id, f"{suffix.title()} Ref"))
                self.store.add_official_assignment(OfficialAssignment(
                    f"assignment_local_{suffix}", "game_program",
                    official_id, OfficialRole.REFEREE))
                self.store.add_official_assignment(OfficialAssignment(
                    f"assignment_second_{suffix}", second_game,
                    official_id, OfficialRole.REFEREE))

                with self.assertRaises(NotFoundError) as refused:
                    self.preview("officials", official_id)
                self.assertEqual(refused.exception.details["reason"],
                                 "root_not_found")
                self.assertIsNone(
                    self.store.get_subtree_deletion_challenge("admin"))
                self.assertIsNotNone(self.store.get_official(official_id))
                self.assertIsNotNone(self.store.get_official_assignment(
                    f"assignment_second_{suffix}"))

    def test_direct_official_root_refuses_foreign_or_missing_home_club(self):
        self.store.add_club(Club("club_official_foreign", "Foreign Club"))
        foreign_team = self.store.get_team("team_keep_home")
        foreign_team.club_id = "club_official_foreign"
        self.store.save_team(foreign_team)
        for suffix, club_id in (
                ("foreign", "club_official_foreign"),
                ("missing", "missing_club")):
            with self.subTest(link=suffix):
                official_id = f"official_club_{suffix}"
                self.store.add_official(Official(
                    official_id, f"{suffix.title()} Club Ref",
                    home_club_id=club_id))
                self.store.add_official_assignment(OfficialAssignment(
                    f"assignment_club_{suffix}", "game_program",
                    official_id, OfficialRole.REFEREE))

                with self.assertRaises(NotFoundError) as refused:
                    self.preview("officials", official_id)
                self.assertEqual(refused.exception.details["reason"],
                                 "root_not_found")
                self.assertIsNone(
                    self.store.get_subtree_deletion_challenge("admin"))
                self.assertIsNotNone(self.store.get_official(official_id))

    def test_direct_official_root_rechecks_new_foreign_assignment(self):
        self.store.add_official(Official("official_root_move", "Moving Ref"))
        self.store.add_official_assignment(OfficialAssignment(
            "assignment_move_local", "game_program", "official_root_move",
            OfficialRole.REFEREE))
        preview = self.preview("officials", "official_root_move")

        self.store.add_official_assignment(OfficialAssignment(
            "assignment_move_foreign", "game_survivor",
            "official_root_move", OfficialRole.REFEREE))
        with self.assertRaises(NotFoundError) as refused:
            self.execute(preview, "Moving Ref", "retire official")
        self.assertEqual(refused.exception.details["reason"],
                         "root_not_found")
        self.assertIsNotNone(
            self.store.get_subtree_deletion_challenge("admin"))
        self.assertIsNotNone(self.store.get_official("official_root_move"))
        self.assertIsNotNone(
            self.store.get_official_assignment("assignment_move_foreign"))

    def test_owned_venue_may_span_seasons_inside_the_active_program(self):
        self.store.add_organization(Organization(
            "org_same_program", "Same Program Facility"))
        program = self.store.get_program("program_delete")
        program.operator_organization_id = "org_same_program"
        self.store.save_program(program)
        self.store.add_season(Season(
            "season_same_program", "program_delete", "2027"))
        self.store.add_venue(Venue(
            "venue_same_program", "Same Program Arena",
            organization_id="org_same_program"))
        self.store.add_season_venue_access(SeasonVenueAccess(
            "access_same_program", "season_same_program",
            "venue_same_program"))

        preview = self.preview("organizations", "org_same_program")
        result = self.execute(
            preview, "Same Program Facility", "retire facility")
        self.assertEqual(result["result"], "success")
        self.assertIsNone(self.store.get_organization("org_same_program"))
        self.assertIsNone(self.store.get_venue("venue_same_program"))
        self.assertIsNone(
            self.store.get_season_venue_access("access_same_program"))
        self.assertIsNotNone(self.store.get_season("season_same_program"))

    def test_execute_rechecks_a_descendant_venue_moved_to_foreign_program(self):
        self.store.add_organization(Organization(
            "org_boundary_move", "Boundary Move Facility"))
        program = self.store.get_program("program_delete")
        program.operator_organization_id = "org_boundary_move"
        self.store.save_program(program)
        self.store.add_venue(Venue(
            "venue_boundary_move", "Boundary Move Arena",
            organization_id="org_boundary_move"))
        preview = self.preview("organizations", "org_boundary_move")

        self.store.add_season_venue_access(SeasonVenueAccess(
            "access_boundary_move", "season_keep",
            "venue_boundary_move"))
        with self.assertRaises(NotFoundError) as refused:
            self.execute(
                preview, "Boundary Move Facility", "retire facility")
        self.assertEqual(refused.exception.details["reason"],
                         "root_not_found")
        self.assertIsNotNone(
            self.store.get_subtree_deletion_challenge("admin"),
            "descendant authority must be rechecked before token consumption")
        self.assertIsNotNone(self.store.get_organization("org_boundary_move"))
        self.assertIsNotNone(self.store.get_venue("venue_boundary_move"))
        self.assertIsNotNone(
            self.store.get_season_venue_access("access_boundary_move"))

    def test_execute_rechecks_a_new_dangling_descendant_venue_access(self):
        self.store.add_organization(Organization(
            "org_dangling_move", "Dangling Move Facility"))
        program = self.store.get_program("program_delete")
        program.operator_organization_id = "org_dangling_move"
        self.store.save_program(program)
        self.store.add_venue(Venue(
            "venue_dangling_move", "Dangling Move Arena",
            organization_id="org_dangling_move"))
        self.store.add_season_venue_access(SeasonVenueAccess(
            "access_valid_move", "season_delete", "venue_dangling_move"))
        preview = self.preview("organizations", "org_dangling_move")

        self.store.add_season_venue_access(SeasonVenueAccess(
            "access_dangling_move", "missing_season",
            "venue_dangling_move"))
        with self.assertRaises(NotFoundError) as refused:
            self.execute(
                preview, "Dangling Move Facility", "retire facility")
        self.assertEqual(refused.exception.details["reason"],
                         "root_not_found")
        self.assertIsNotNone(
            self.store.get_subtree_deletion_challenge("admin"),
            "partial graph corruption must not consume the challenge")
        self.assertIsNotNone(self.store.get_organization("org_dangling_move"))
        self.assertIsNotNone(self.store.get_venue("venue_dangling_move"))
        self.assertIsNotNone(
            self.store.get_season_venue_access("access_dangling_move"))

    def test_wrong_expired_replayed_and_stale_tokens_delete_nothing(self):
        preview = self.preview()
        with self.assertRaises(ValidationError) as wrong:
            self.service.execute("admin", "wrong", "Adult Hockey", "reason")
        self.assertEqual(wrong.exception.details["reason"], "invalid_challenge")
        self.assertIsNotNone(self.store.get_program("program_delete"))

        expiring = _subtree_service(
            self.store, challenge_ttl_seconds=-1)
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
                # Challenge consumption is part of the same atomic transaction
                # as every domain and audit write.  A rollback therefore leaves
                # the exact preview reusable; a replacement still supersedes it
                # actor-locally.  Supersede here without executing so the next
                # subtest keeps its fixture.
                self.assertIsNotNone(
                    self.store.get_subtree_deletion_challenge("admin"))
                self.preview()
                with self.assertRaises(ValidationError):
                    self.execute(preview)

    def test_rolled_back_execution_can_retry_the_same_exact_challenge(self):
        preview = self.preview()

        def fail(stage):
            if stage == "after_revalidation":
                raise RuntimeError("injected:after_revalidation")

        self.service._stage_hook = fail
        with self.assertRaisesRegex(RuntimeError, "after_revalidation"):
            self.execute(preview)
        self.service._stage_hook = lambda _stage: None

        result = self.execute(preview)
        self.assertEqual(result["result"], "success")
        self.assertIsNone(self.store.get_program("program_delete"))
        self.assertIsNone(
            self.store.get_subtree_deletion_challenge("admin"),
            "a successful retry must consume the challenge atomically")

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
        self.store.set_active_context(ActiveContext(
            "admin2", "program_keep", "season_keep", NOW,
            league_id="league_keep"))
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

    def test_confirmation_typo_can_be_corrected_with_the_same_token(self):
        preview = self.preview(
            "organizations", "org_unrelated", actor="admin2")
        with self.assertRaises(ValidationError) as typo:
            self.service.execute(
                "admin2", preview["challenge_token"], "south facility",
                "retire test facility")
        self.assertEqual(typo.exception.details["reason"],
                         "confirmation_mismatch")
        result = self.service.execute(
            "admin2", preview["challenge_token"], "South Facility",
            "retire test facility")
        self.assertEqual(result["result"], "success")
        self.assertIsNone(self.store.get_organization("org_unrelated"))

    def test_preconsume_lock_failure_can_retry_the_same_token(self):
        preview = self.preview(
            "organizations", "org_unrelated", actor="admin2")
        failed_once = False

        def fail_once(stage):
            nonlocal failed_once
            if stage == "after_lock" and not failed_once:
                failed_once = True
                raise RuntimeError("simulated lock acquisition failure")

        self.service._stage_hook = fail_once
        with self.assertRaisesRegex(RuntimeError, "lock acquisition"):
            self.service.execute(
                "admin2", preview["challenge_token"], "South Facility",
                "retire test facility")
        self.service._stage_hook = lambda _stage: None
        result = self.service.execute(
            "admin2", preview["challenge_token"], "South Facility",
            "retire test facility")
        self.assertEqual(result["result"], "success")

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
        RosterService(self.store, clock=lambda: NOW).cancel_game(
            "game_facility", "admin")

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

    def test_archived_season_refuses_direct_and_ancestor_mutation(self):
        season = self.store.get_season("season_delete")
        season.status = SeasonStatus.ARCHIVED
        self.store.save_season(season)
        with self.assertRaises(ValidationError) as blocked:
            self.preview()
        self.assertEqual(blocked.exception.details["reason"],
                         "season_archived")

        # A shared facility deletion would mutate the retained Game governed
        # by that archived Season. Make it a clean draft so the archived guard,
        # rather than the cancellation guard, is the decisive refusal.
        game = self.store.get_game("game_facility")
        game.published = False
        game.is_draft = True
        self.store.save_game(game)
        with self.assertRaises(ValidationError) as cross_edge:
            self.preview("organizations", "org_delete")
        self.assertEqual(cross_edge.exception.details["reason"],
                         "season_archived")

    def test_archived_boundary_precedes_impossible_cancel_instruction(self):
        season = self.store.get_season("season_delete")
        season.status = SeasonStatus.ARCHIVED
        self.store.save_season(season)

        with self.assertRaises(ValidationError) as archived:
            self.preview("organizations", "org_delete")
        self.assertEqual(archived.exception.details["reason"],
                         "season_archived")
        self.assertIsNone(
            self.store.get_subtree_deletion_challenge("admin"))
        self.assertEqual(
            self.store.get_game("game_facility").ice_slot_id,
            "slot_delete")

        season.status = SeasonStatus.ACTIVE
        self.store.save_season(season)
        with self.assertRaises(ValidationError) as cancellation:
            self.preview("organizations", "org_delete")
        self.assertEqual(cancellation.exception.details["reason"],
                         "game_cancellation_required")

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
            service = _subtree_service(peer)
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

    def test_contended_graph_try_lock_is_fast_and_preserves_same_token(self):
        preview = self.preview()
        peer = self.make_peer_store()
        hold = peer.transaction()
        hold.__enter__()
        try:
            self.assertIsNotNone(peer.get_team_for_update("team_home"))
            started = time.monotonic()
            with self.assertRaises(ConcurrencyConflictError) as conflict:
                self.execute(preview)
            self.assertLess(time.monotonic() - started, 2.0)
            self.assertEqual(conflict.exception.details["reason"],
                             "lock_not_available")
            self.assertIsNotNone(
                self.store.get_subtree_deletion_challenge("admin"))
            self.assertIsNotNone(self.store.get_program("program_delete"))
        finally:
            hold.__exit__(None, None, None)
            peer.close()

        self.assertEqual(self.execute(preview)["result"], "success")

    def test_graph_lock_statement_is_complete_and_nonblocking(self):
        captured = []
        original = self.store._exec
        self.store._exec = lambda sql, *args, **kwargs: captured.append(sql)
        try:
            self.store.lock_subtree_graph()
        finally:
            self.store._exec = original
        expected_tables = sorted(spec.table for spec in SPECS.values())
        self.assertEqual(captured, [
            f"LOCK TABLE {', '.join(expected_tables)} "
            "IN EXCLUSIVE MODE NOWAIT"
        ])

    def test_account_deactivation_after_context_check_aborts_locked_recheck(self):
        preview = self.preview()
        context_checked = threading.Event()
        resume = threading.Event()
        outcome = {}
        original_authorizer = self.service._root_authorizer

        def authorize(kind, record_id, account, phase):
            allowed = original_authorizer(
                kind, record_id, account, phase)
            if phase == "context":
                context_checked.set()
                if not resume.wait(10):
                    raise RuntimeError(
                        "authorization race harness did not resume")
            return allowed

        deleter = SubtreeDeletionService(
            self.store, lambda: NOW, root_authorizer=authorize,
            boundary_authorizer=self.service._boundary_authorizer)

        def run_delete():
            try:
                outcome["result"] = deleter.execute(
                    "admin", preview["challenge_token"], "Adult Hockey",
                    "authorization race")
            except Exception as exc:
                outcome["error"] = exc

        thread = threading.Thread(target=run_delete)
        peer = self.make_peer_store()
        try:
            thread.start()
            self.assertTrue(
                context_checked.wait(10),
                "delete never completed its context authorization")
            with peer.transaction(isolation="SERIALIZABLE"):
                account = peer.get_user_account_for_update("admin")
                self.assertIsNotNone(account)
                account.active = False
                peer.save_user_account(account)
        finally:
            resume.set()
            thread.join(20)
            peer.close()

        self.assertFalse(thread.is_alive(), "subtree deletion thread hung")
        self.assertNotIn("result", outcome, outcome)
        self.assertIsInstance(
            outcome.get("error"), ConcurrencyConflictError, outcome)
        self.assertEqual(
            outcome["error"].details["reason"], "serialization_failure")
        self.assertIsNotNone(self.store.get_program("program_delete"))
        self.assertIsNotNone(
            self.store.get_subtree_deletion_challenge("admin"),
            "the retryable authorization race must preserve the preview")
        with self.assertRaises(NotAuthorizedError):
            deleter.execute(
                "admin", preview["challenge_token"], "Adult Hockey",
                "authorization race retry")


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
        srv.STATE.api.store.add_program(Program(
            "http_program", "HTTP Program",
            operator_organization_id="http_org"))
        srv.STATE.api.store.add_season(Season(
            "http_season", "http_program", "HTTP Season"))
        srv.STATE.api.store.set_active_context(ActiveContext(
            self.admin.id, "http_program", "http_season", NOW))
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
