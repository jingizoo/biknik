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


# Closed complement to REFERENCE_INVENTORY.  This is intentionally explicit:
# every persisted field must be classified as either a relationship/reference
# or a non-reference value.  A new field therefore fails the partition test no
# matter what it is named; there is no suffix convention or silent default.
_NON_REFERENCE_FIELD_MANIFEST = """
season_roster_membership_events id action at reason seq
season_roster_memberships id status position jersey_number shoots effective_from effective_to
schedule_scenarios id name planner_version input_fingerprint proposal_fingerprint created_at
programs id name country timezone
seasons id name start_date end_date status archived_at
leagues id name sort_order
league_seasons id
divisions id name age_group
age_eligibility_rules id version cutoff_month cutoff_day tiers enforcement created_at
season_team_registrations id active
season_copy_forward_commits id copy_forward_fingerprint rolled_forward skipped committed_at
team_league_migration_decisions id note
season_venue_access id active
clubs id name country
teams id name division
players id name position shoots jersey_number is_active first_name last_name preferred_name birthdate registration_number skill_rating
organizations id name short_name
venues id name address timezone
rinks id name
ice_slots id start_time end_time slot_type status
scheduling_policies id scope_type warmup_minutes resurfacing_minutes min_playable_minutes curfew_local
games id start_time target_goalies target_skaters max_skaters rink end_time roster_lock_time locked cancelled published is_draft game_type cancelled_venue_name cancelled_venue_timezone cancelled_rink_name cancelled_scheduled_start_time cancelled_scheduled_end_time cancelled_ice_start_time cancelled_ice_end_time
game_roster_entries id roster_role selection_source status selected_at updated_at seated_position
game_availability id availability_status response_source responded_at notes
substitute_enrollments id position status enrolled_at priority_rank offered_at offer_expires_at accepted_at declined_at
audit_logs id action at
notification_events id type message at
setup_audit_logs id action entity_type at
data_access_logs category subject_type purpose at actor_role outcome id seq
factory_reset_events id environment started_at result completed_at failure_reason
factory_reset_challenges id token_hash expires_at created_at
factory_reset_locks id token acquired_at expires_at
officials id name is_active
official_assignments id role status assigned_at responded_at note
game_results id home_score away_score status recorded_at approved_at
notifications_feed id kind title message at
notification_recipients id read_at
notification_deliveries id channel status attempts last_error sent_at destination last_attempt_at next_attempt_at dead_lettered_at
contact_destinations id channel destination label active
device_tokens id provider token label active
notification_preferences id channel enabled digest active
installation_state id claimed_at claim_method
user_accounts id username password_hash role created_at active
sessions id token_hash issued_at expires_at revoked_at user_agent
user_active_context updated_at generation
guardian_links id created_at verified consent_method consented_at
reschedule_requests id reason status created_at opponent_responded_at league_decided_at decision_note
calendar_feed_tokens id token_hash actor_type created_at revoked_at label last_used_at
official_availability id start_time end_time status note
""".strip()

_NON_REFERENCE_FIELDS = {
    f"{parts[0]}.{name}"
    for line in _NON_REFERENCE_FIELD_MANIFEST.splitlines()
    for parts in (line.split(),)
    for name in parts[1:]
}


def _persisted_fields():
    return {
        f"{spec.table}.{field.name}"
        for model, spec in SPECS.items()
        for field in fields(model)
    }


def _partition_gaps(persisted):
    references = set(REFERENCE_BY_KEY)
    overlap = references & _NON_REFERENCE_FIELDS
    classified = references | _NON_REFERENCE_FIELDS
    return persisted - classified, classified - persisted, overlap


def _declared_foreign_keys(store):
    cur = store.conn.cursor()
    if store.backend == "sqlite":
        actual = set()
        cur.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        )
        for row in cur.fetchall():
            table = row[0] if not hasattr(row, "keys") else row["name"]
            cur.execute(f"PRAGMA foreign_key_list('{table}')")
            for fk in cur.fetchall():
                actual.add((table, fk[3], fk[2]))
        return actual

    cur.execute(
        "SELECT src.relname AS source_table, source_col.attname AS source_col, "
        "       target.relname AS target_table "
        "FROM pg_constraint constraint_row "
        "JOIN pg_class src ON src.oid = constraint_row.conrelid "
        "JOIN pg_namespace source_ns ON source_ns.oid = src.relnamespace "
        "JOIN pg_class target ON target.oid = constraint_row.confrelid "
        "JOIN LATERAL generate_subscripts(constraint_row.conkey, 1) pos(i) "
        "  ON TRUE "
        "JOIN pg_attribute source_col "
        "  ON source_col.attrelid = constraint_row.conrelid "
        " AND source_col.attnum = constraint_row.conkey[pos.i] "
        "WHERE constraint_row.contype = 'f' "
        "  AND source_ns.nspname = current_schema()"
    )
    return {
        (row["source_table"], row["source_col"], row["target_table"])
        for row in cur.fetchall()
    }


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

    def test_every_persisted_field_has_exactly_one_explicit_classification(self):
        missing, unknown, overlap = _partition_gaps(_persisted_fields())
        self.assertEqual(missing, set(), "unclassified persisted fields")
        self.assertEqual(unknown, set(), "declarations for absent fields")
        self.assertEqual(overlap, set(), "fields classified both ways")
        self.assertEqual(len(REFERENCE_INVENTORY), len(REFERENCE_BY_KEY))

    def test_unconventionally_named_new_field_cannot_evade_the_partition(self):
        mutated = _persisted_fields() | {"games.shadow_parent"}
        missing, unknown, overlap = _partition_gaps(mutated)
        self.assertEqual(missing, {"games.shadow_parent"})
        self.assertEqual(unknown, set())
        self.assertEqual(overlap, set())

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
        stores = [("sqlite", SqlStore(path))]
        postgres_url = os.environ.get("TEST_DATABASE_URL")
        if postgres_url:
            stores.append(("postgres", SqlStore(postgres_url)))
        try:
            constraints = {}
            for backend, store in stores:
                actual = _declared_foreign_keys(store)
                constraints[backend] = actual
                for table, field, target_table in sorted(actual):
                    with self.subTest(backend=backend, table=table, field=field):
                        relation = REFERENCE_BY_KEY[f"{table}.{field}"]
                        self.assertIn(relation.on_target_delete, {
                            TargetRemoval.DELETE_SOURCE,
                            TargetRemoval.DETACH,
                        })
                        self.assertIn(
                            target_table,
                            {target.value for target in relation.targets},
                        )
            if "postgres" in constraints:
                self.assertEqual(
                    constraints["postgres"], constraints["sqlite"],
                    "SQLite and PostgreSQL declare different foreign keys",
                )
        finally:
            for _, store in stores:
                store.close()
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
