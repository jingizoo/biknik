"""Contract tests for #429's non-production subtree preview slice."""

from __future__ import annotations

import hashlib
import os
import random
import tempfile
import unittest
from dataclasses import fields

from hockey_scheduler.services.subtree_preview import (
    EdgePreview,
    EntityType,
    PreviewContractError,
    ProjectedEdge,
    REFERENCE_BY_KEY,
    REFERENCE_INVENTORY,
    RecordRef,
    ReferenceRole,
    TargetRemoval,
    build_subtree_preview,
)
from hockey_scheduler.store.sql_store import SPECS, SqlStore


_REFERENCE_LOOKING_FIELDS = {
    "scope", "detail", "request_input", "proposal", "generation_snapshot",
    "response_snapshot", "request_identity", "counts", "pre_reset_counts",
    "audience", "team_side", "actor_key",
}

_REFERENCE_PRIMARY_KEYS = {("user_active_context", "id")}


def _is_reference_looking(table: str, name: str) -> bool:
    return (
        (table, name) in _REFERENCE_PRIMARY_KEYS
        or (
            name != "id"
            and (
                name.endswith(("_id", "_ids", "_ref", "_by"))
                or name in _REFERENCE_LOOKING_FIELDS
            )
        )
    )


def _state(kind: EntityType, record_id: str, version: str = "1") -> str:
    return hashlib.sha256(
        f"{kind.value}:{record_id}:{version}".encode("utf-8")
    ).hexdigest()


def _record(kind: EntityType, record_id: str, version: str = "1") -> RecordRef:
    return RecordRef(kind, record_id, _state(kind, record_id, version))


class TestReferenceInventory(unittest.TestCase):
    def test_entity_types_are_exactly_the_persisted_store_specs(self):
        self.assertEqual(
            {kind.value for kind in EntityType},
            {spec.table for spec in SPECS.values()},
        )

    def test_every_reference_looking_field_is_classified_exactly_once(self):
        derived = {
            f"{spec.table}.{field.name}"
            for model, spec in SPECS.items()
            for field in fields(model)
            if _is_reference_looking(spec.table, field.name)
        }
        self.assertEqual(set(REFERENCE_BY_KEY), derived)
        self.assertEqual(len(REFERENCE_INVENTORY), len(REFERENCE_BY_KEY))

    def test_every_inventory_field_exists_on_its_persisted_model(self):
        model_by_table = {spec.table: model for model, spec in SPECS.items()}
        for relation in REFERENCE_INVENTORY:
            with self.subTest(relation=relation.key):
                model = model_by_table[relation.source.value]
                self.assertIn(relation.field, {field.name for field in fields(model)})

    def test_reference_roles_and_removal_actions_are_coherent(self):
        role_actions = {
            ReferenceRole.OWNERSHIP: {TargetRemoval.DELETE_SOURCE},
            ReferenceRole.ASSOCIATION: {TargetRemoval.DELETE_SOURCE},
            ReferenceRole.SHARED: {TargetRemoval.DETACH},
            ReferenceRole.HISTORICAL: {TargetRemoval.RETAIN},
            ReferenceRole.PRINCIPAL: {TargetRemoval.NOT_GRAPH},
            ReferenceRole.EXTERNAL_KEY: {TargetRemoval.NOT_GRAPH},
            ReferenceRole.OPAQUE_SNAPSHOT: {TargetRemoval.NOT_GRAPH},
            ReferenceRole.TRACE: {TargetRemoval.NOT_GRAPH},
        }
        for relation in REFERENCE_INVENTORY:
            with self.subTest(relation=relation.key):
                self.assertIn(relation.on_target_delete,
                              role_actions[relation.role])
                if relation.on_target_delete is TargetRemoval.NOT_GRAPH:
                    self.assertEqual(relation.targets, ())
                else:
                    self.assertTrue(relation.targets)
                if len(relation.targets) > 1:
                    self.assertTrue(relation.discriminator)

    def test_every_database_foreign_key_is_a_live_inventory_edge(self):
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        os.unlink(path)
        store = SqlStore(path)
        try:
            actual = set()
            for (table,) in store.conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            ):
                for row in store.conn.execute(f"PRAGMA foreign_key_list({table})"):
                    actual.add((table, row[3], row[2]))

            for table, field, target_table in sorted(actual):
                with self.subTest(table=table, field=field):
                    relation = REFERENCE_BY_KEY[f"{table}.{field}"]
                    self.assertIn(relation.on_target_delete, {
                        TargetRemoval.DELETE_SOURCE,
                        TargetRemoval.DETACH,
                    })
                    self.assertIn(target_table,
                                  {target.value for target in relation.targets})
        finally:
            store.conn.close()
            if os.path.exists(path):
                os.unlink(path)


class _RepresentativeGraph:
    """A small graph containing every relationship family #429 names."""

    def __init__(self):
        self.records: list[RecordRef] = []
        self.by_name: dict[str, RecordRef] = {}
        self.edges: list[ProjectedEdge] = []

        def add(name: str, kind: EntityType, version: str = "1") -> RecordRef:
            value = _record(kind, name, version)
            self.records.append(value)
            self.by_name[name] = value
            return value

        self.org = add("org_shared", EntityType.ORGANIZATION)
        self.program = add("program_delete", EntityType.PROGRAM)
        self.season = add("season_delete", EntityType.SEASON)
        self.league = add("league_delete", EntityType.LEAGUE)
        self.league_season = add("ls_delete", EntityType.LEAGUE_SEASON)
        self.division = add("division_delete", EntityType.DIVISION)
        self.team = add("team_delete", EntityType.TEAM)
        self.opponent = add("team_keep", EntityType.TEAM)
        self.player = add("player_delete", EntityType.PLAYER)
        self.membership = add("membership_delete",
                              EntityType.SEASON_ROSTER_MEMBERSHIP)
        self.membership_event = add(
            "membership_event_delete",
            EntityType.SEASON_ROSTER_MEMBERSHIP_EVENT,
        )
        self.scenario = add("scenario_delete", EntityType.SCHEDULE_SCENARIO)
        self.registration = add(
            "registration_delete", EntityType.SEASON_TEAM_REGISTRATION)
        self.policy = add("policy_delete", EntityType.SCHEDULING_POLICY)

        self.venue = add("venue_keep", EntityType.VENUE)
        self.rink = add("rink_keep", EntityType.RINK)
        self.slot = add("slot_keep", EntityType.ICE_SLOT)
        self.access = add("access_delete", EntityType.SEASON_VENUE_ACCESS)

        self.game = add("game_delete", EntityType.GAME)
        self.cancelled_game = add("cancelled_game_delete", EntityType.GAME,
                                  "cancelled-with-snapshot")
        self.roster = add("roster_delete", EntityType.GAME_ROSTER_ENTRY)
        self.availability = add("availability_delete",
                                EntityType.GAME_AVAILABILITY)
        self.substitute = add("substitute_delete",
                              EntityType.SUBSTITUTE_ENROLLMENT)
        self.game_audit = add("game_audit_delete", EntityType.AUDIT_LOG)
        self.notification_event = add("event_delete",
                                      EntityType.NOTIFICATION_EVENT)
        self.result = add("result_delete", EntityType.GAME_RESULT)
        self.reschedule = add("reschedule_delete",
                              EntityType.RESCHEDULE_REQUEST)

        self.official = add("official_keep", EntityType.OFFICIAL)
        self.assignment = add("assignment_delete",
                              EntityType.OFFICIAL_ASSIGNMENT)
        self.notification = add("notification_delete", EntityType.NOTIFICATION)
        self.notification_recipient = add(
            "notification_recipient_delete", EntityType.NOTIFICATION_RECIPIENT)
        self.delivery = add("delivery_delete", EntityType.NOTIFICATION_DELIVERY)
        self.feed = add("feed_delete", EntityType.CALENDAR_FEED_TOKEN)

        self.user = add("user_keep", EntityType.USER_ACCOUNT)
        self.guardian = add("guardian_delete", EntityType.GUARDIAN_LINK)
        self.context = add("context_keep", EntityType.ACTIVE_CONTEXT)
        self.setup_audit = add("setup_audit_keep", EntityType.SETUP_AUDIT_LOG)

        def edge(key: str, source: RecordRef, target: RecordRef) -> None:
            self.edges.append(ProjectedEdge(key, source, target))

        edge("programs.operator_organization_id", self.program, self.org)
        edge("seasons.program_id", self.season, self.program)
        edge("leagues.program_id", self.league, self.program)
        edge("teams.program_id", self.team, self.program)
        edge("teams.league_id", self.team, self.league)
        edge("players.team_id", self.player, self.team)
        edge("league_seasons.league_id", self.league_season, self.league)
        edge("league_seasons.season_id", self.league_season, self.season)
        edge("divisions.league_season_id", self.division, self.league_season)
        edge("season_team_registrations.league_season_id",
             self.registration, self.league_season)
        edge("season_team_registrations.team_id", self.registration, self.team)
        edge("season_team_registrations.division_id",
             self.registration, self.division)
        edge("season_roster_memberships.player_id", self.membership, self.player)
        edge("season_roster_memberships.league_season_id",
             self.membership, self.league_season)
        edge("season_roster_memberships.season_id", self.membership, self.season)
        edge("season_roster_memberships.team_id", self.membership, self.team)
        edge("season_roster_membership_events.membership_id",
             self.membership_event, self.membership)
        edge("schedule_scenarios.program_id", self.scenario, self.program)
        edge("schedule_scenarios.season_id", self.scenario, self.season)
        edge("schedule_scenarios.league_id", self.scenario, self.league)
        edge("schedule_scenarios.league_season_id",
             self.scenario, self.league_season)
        edge("schedule_scenarios.division_id", self.scenario, self.division)
        edge("scheduling_policies.scope_id", self.policy, self.season)

        edge("venues.organization_id", self.venue, self.org)
        edge("rinks.venue_id", self.rink, self.venue)
        edge("ice_slots.rink_id", self.slot, self.rink)
        edge("venues.league_id", self.venue, self.program)
        edge("season_venue_access.season_id", self.access, self.season)
        edge("season_venue_access.venue_id", self.access, self.venue)

        for game in (self.game, self.cancelled_game):
            edge("games.home_team_id", game, self.team)
            edge("games.away_team_id", game, self.opponent)
            edge("games.season_id", game, self.season)
            edge("games.league_id", game, self.league)
            edge("games.league_season_id", game, self.league_season)
            edge("games.division_id", game, self.division)
        # A cancelled Game has no live IceSlot edge.  Its cancelled_* ids are
        # opaque historical snapshots and therefore cannot be projected here.
        edge("games.ice_slot_id", self.game, self.slot)

        edge("game_roster_entries.game_id", self.roster, self.game)
        edge("game_roster_entries.player_id", self.roster, self.player)
        edge("game_roster_entries.team_side", self.roster, self.team)
        edge("game_availability.game_id", self.availability, self.game)
        edge("game_availability.player_id", self.availability, self.player)
        edge("substitute_enrollments.game_id", self.substitute, self.game)
        edge("substitute_enrollments.player_id", self.substitute, self.player)
        edge("substitute_enrollments.team_id", self.substitute, self.team)
        edge("audit_logs.game_id", self.game_audit, self.game)
        edge("audit_logs.subject_player_id", self.game_audit, self.player)
        edge("notification_events.game_id", self.notification_event, self.game)
        edge("notification_events.subject_player_id",
             self.notification_event, self.player)
        edge("game_results.game_id", self.result, self.game)
        edge("reschedule_requests.game_id", self.reschedule, self.game)
        edge("reschedule_requests.requested_by_team_id",
             self.reschedule, self.team)

        edge("official_assignments.game_id", self.assignment, self.game)
        edge("official_assignments.official_id", self.assignment, self.official)
        edge("notifications_feed.game_id", self.notification, self.game)
        edge("notifications_feed.assignment_id",
             self.notification, self.assignment)
        edge("notification_recipients.notification_id",
             self.notification_recipient, self.notification)
        edge("notification_recipients.actor_key",
             self.notification_recipient, self.user)
        edge("notification_deliveries.notification_id",
             self.delivery, self.notification)
        edge("calendar_feed_tokens.actor_ref", self.feed, self.team)

        edge("guardian_links.player_id", self.guardian, self.player)
        edge("guardian_links.guardian_user_id", self.guardian, self.user)
        edge("user_accounts.scope", self.user, self.team)
        edge("user_active_context.id", self.context, self.user)
        edge("user_active_context.program_id", self.context, self.program)
        edge("setup_audit_logs.entity_id", self.setup_audit, self.game)


def _ids(groups) -> set[tuple[EntityType, str]]:
    return {
        (group.entity_type, record_id)
        for group in groups
        for record_id in group.record_ids
    }


class TestSubtreePreview(unittest.TestCase):
    def setUp(self):
        self.graph = _RepresentativeGraph()

    def _preview(self, root=None, records=None, edges=None, actor="owner-1",
                 confirmation="Adult Men"):
        return build_subtree_preview(
            actor_id=actor,
            root=root or self.graph.program,
            confirmation_name=confirmation,
            records=records if records is not None else self.graph.records,
            edges=edges if edges is not None else self.graph.edges,
        )

    def test_program_preview_deletes_owned_competition_and_preserves_shared_edges(self):
        preview = self._preview()
        deleted = _ids(preview.delete_groups)
        retained = _ids(preview.retained_groups)

        for record in (
            self.graph.program,
            self.graph.season,
            self.graph.league,
            self.graph.league_season,
            self.graph.division,
            self.graph.team,
            self.graph.player,
            self.graph.membership,
            self.graph.membership_event,
            self.graph.scenario,
            self.graph.registration,
            self.graph.access,
            self.graph.game,
            self.graph.cancelled_game,
            self.graph.roster,
            self.graph.availability,
            self.graph.substitute,
            self.graph.game_audit,
            self.graph.notification_event,
            self.graph.assignment,
            self.graph.result,
            self.graph.reschedule,
            self.graph.notification,
            self.graph.notification_recipient,
            self.graph.delivery,
            self.graph.feed,
            self.graph.guardian,
            self.graph.policy,
        ):
            self.assertIn(record.key, deleted, record)

        for record in (
            self.graph.org,
            self.graph.venue,
            self.graph.slot,
            self.graph.opponent,
            self.graph.official,
            self.graph.user,
            self.graph.context,
            self.graph.setup_audit,
        ):
            self.assertNotIn(record.key, deleted, record)
            self.assertIn(record.key, retained, record)

        detached = {(edge.inventory_key, edge.source_id, edge.target_id)
                    for edge in preview.detached_edges}
        self.assertIn(("venues.league_id", "venue_keep", "program_delete"),
                      detached)
        self.assertIn(("user_accounts.scope", "user_keep", "team_delete"),
                      detached)
        self.assertIn(("user_active_context.program_id", "context_keep",
                       "program_delete"), detached)

        retained_edges = {(edge.inventory_key, edge.source_id, edge.target_id)
                          for edge in preview.retained_edges}
        self.assertIn(("setup_audit_logs.entity_id", "setup_audit_keep",
                       "game_delete"), retained_edges)

        removed = {(edge.inventory_key, edge.source_id, edge.target_id)
                   for edge in preview.removed_edges}
        self.assertIn(("games.ice_slot_id", "game_delete", "slot_keep"), removed)
        self.assertFalse(any(edge.source_id == "cancelled_game_delete"
                             and edge.inventory_key == "games.ice_slot_id"
                             for edge in preview.removed_edges))

        for group in preview.delete_groups + preview.retained_groups:
            self.assertEqual(group.count, len(group.record_ids))
            self.assertEqual(group.record_ids, tuple(sorted(group.record_ids)))
        for group in (
            preview.removed_relationship_groups
            + preview.detached_relationship_groups
            + preview.retained_relationship_groups
        ):
            self.assertEqual(group.count, len(group.edges))
            self.assertTrue(all(edge.inventory_key == group.inventory_key
                                for edge in group.edges))
        self.assertEqual(
            tuple(group.entity_type.value for group in preview.delete_groups),
            tuple(sorted(group.entity_type.value for group in preview.delete_groups)),
        )

    def test_facility_root_deletes_facility_tree_but_detaches_external_consumers(self):
        preview = self._preview(root=self.graph.org,
                                confirmation="Shared Facilities")
        deleted = _ids(preview.delete_groups)
        retained = _ids(preview.retained_groups)
        self.assertTrue({self.graph.org.key, self.graph.venue.key,
                         self.graph.rink.key, self.graph.slot.key,
                         self.graph.access.key}.issubset(deleted))
        self.assertTrue({self.graph.program.key, self.graph.season.key,
                         self.graph.game.key}.issubset(retained))
        self.assertNotIn(self.graph.game.key, deleted)

        detached = {(edge.inventory_key, edge.source_id, edge.target_id)
                    for edge in preview.detached_edges}
        self.assertIn(("programs.operator_organization_id", "program_delete",
                       "org_shared"), detached)
        self.assertIn(("games.ice_slot_id", "game_delete", "slot_keep"),
                      detached)

    def test_league_season_root_preserves_permanent_parents_and_team(self):
        preview = self._preview(root=self.graph.league_season,
                                confirmation="2026 Adult")
        deleted = _ids(preview.delete_groups)
        retained = _ids(preview.retained_groups)
        self.assertTrue({self.graph.league_season.key, self.graph.division.key,
                         self.graph.registration.key, self.graph.membership.key,
                         self.graph.game.key, self.graph.cancelled_game.key,
                         self.graph.scenario.key}.issubset(deleted))
        self.assertTrue({self.graph.league.key, self.graph.season.key,
                         self.graph.team.key}.issubset(retained))
        self.assertNotIn(self.graph.team.key, deleted)

    def test_preview_is_order_independent_and_canonical(self):
        expected = self._preview()
        records = list(self.graph.records)
        edges = list(self.graph.edges)
        random.Random(7719).shuffle(records)
        random.Random(9921).shuffle(edges)
        actual = self._preview(records=records, edges=edges)
        self.assertEqual(actual, expected)

    def test_fingerprint_binds_actor_confirmation_row_state_and_graph(self):
        baseline = self._preview()
        self.assertNotEqual(self._preview(actor="owner-2").fingerprint,
                            baseline.fingerprint)
        self.assertNotEqual(self._preview(confirmation="Adult Women").fingerprint,
                            baseline.fingerprint)

        changed_program = _record(EntityType.PROGRAM, "program_delete", "2")
        changed_records = [changed_program if r.key == changed_program.key else r
                           for r in self.graph.records]
        changed_edges = [
            ProjectedEdge(
                edge.inventory_key,
                changed_program if edge.source.key == changed_program.key else edge.source,
                changed_program if edge.target.key == changed_program.key else edge.target,
            )
            for edge in self.graph.edges
        ]
        changed = self._preview(root=changed_program, records=changed_records,
                                edges=changed_edges)
        self.assertNotEqual(changed.fingerprint, baseline.fingerprint)

        extra = _record(EntityType.SEASON, "season_added_after_preview")
        records = [*self.graph.records, extra]
        edges = [*self.graph.edges,
                 ProjectedEdge("seasons.program_id", extra, self.graph.program)]
        self.assertNotEqual(self._preview(records=records, edges=edges).fingerprint,
                            baseline.fingerprint)

    def test_confirmation_name_is_normalized_before_hash_and_output(self):
        plain = self._preview(confirmation="Adult Men")
        padded = self._preview(confirmation="  Adult Men  ")
        self.assertEqual(padded.confirmation_name, "Adult Men")
        self.assertEqual(padded.fingerprint, plain.fingerprint)

    def test_missing_root_duplicate_record_and_external_endpoint_fail_closed(self):
        with self.assertRaisesRegex(PreviewContractError, "root is absent"):
            self._preview(records=[r for r in self.graph.records
                                   if r.key != self.graph.program.key])
        with self.assertRaisesRegex(PreviewContractError, "duplicate projected"):
            self._preview(records=[*self.graph.records, self.graph.program])
        with self.assertRaisesRegex(PreviewContractError,
                                    "endpoint outside the graph"):
            self._preview(records=[r for r in self.graph.records
                                   if r.key != self.graph.org.key])

    def test_edge_cannot_override_catalogued_source_target_or_non_graph_field(self):
        with self.assertRaisesRegex(PreviewContractError, "source must be"):
            ProjectedEdge("seasons.program_id", self.graph.team,
                          self.graph.program)
        with self.assertRaisesRegex(PreviewContractError, "cannot target"):
            ProjectedEdge("seasons.program_id", self.graph.season,
                          self.graph.venue)
        with self.assertRaisesRegex(PreviewContractError, "not a live graph"):
            ProjectedEdge("games.cancelled_ice_slot_id", self.graph.game,
                          self.graph.slot)
        with self.assertRaisesRegex(PreviewContractError, "unknown relationship"):
            ProjectedEdge("games.made_up_id", self.graph.game, self.graph.slot)

    def test_invalid_public_identifiers_and_state_digests_are_rejected(self):
        bad_values = (
            (EntityType.PROGRAM, "", "0" * 64),
            (EntityType.PROGRAM, " padded ", "0" * 64),
            (EntityType.PROGRAM, "bad\nname", "0" * 64),
            (EntityType.PROGRAM, "ok", "ABC"),
            (EntityType.PROGRAM, "ok", "g" * 64),
        )
        for kind, record_id, digest in bad_values:
            with self.subTest(record_id=record_id, digest=digest):
                with self.assertRaises(PreviewContractError):
                    RecordRef(kind, record_id, digest)

    def test_preview_contains_no_model_payload_or_display_label_fields(self):
        preview = self._preview()
        public_fields = set(preview.__dataclass_fields__)
        self.assertNotIn("records", public_fields)
        self.assertNotIn("payload", public_fields)
        self.assertNotIn("state_fingerprint", public_fields)
        for edge in (preview.removed_edges + preview.detached_edges
                     + preview.retained_edges):
            self.assertIsInstance(edge, EdgePreview)
            self.assertNotIn("source", edge.__dataclass_fields__)
            self.assertNotIn("target", edge.__dataclass_fields__)


if __name__ == "__main__":
    unittest.main()
