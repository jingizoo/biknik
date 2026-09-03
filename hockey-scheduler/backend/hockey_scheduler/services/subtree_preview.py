"""Pure ownership inventory and preview contract for destructive subtrees.

Issue #429 deliberately requires this contract to land before any destructive
command, route, schema, or UI is wired.  This module therefore performs no I/O
and deletes nothing.  It gives the later store projection one closed inventory
to project into, then turns that immutable graph into a deterministic preview.

The inventory answers two different questions which must not be conflated:

* ``DELETE_SOURCE`` means the source row is part of the target's destructive
  subtree.  For example, a Season is deleted with its Program.
* ``DETACH`` means the source is shared and survives while only this edge is
  removed.  For example, deleting a Program must not delete the Organization
  that operates it, and deleting a Season must not delete a shared Venue.
* ``RETAIN`` is a historical or principal reference.  It remains as evidence;
  it is not current scheduling authority and cannot grow the subtree.
* ``NOT_GRAPH`` names a reference-looking field which is actually an external
  key, opaque snapshot, or trace value.  Listing these explicitly makes a new
  ``*_id``/``*_ref``/JSON carrier fail the completeness test until somebody
  classifies it.

The authenticated store adapter resolves polymorphic fields and projects only
records the operator may name.  The pure planner validates that every projected
edge agrees with this inventory; a caller cannot smuggle a friendlier cascade
rule into the preview.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import Enum
from typing import Iterable, Optional

class PreviewContractError(ValueError):
    """The projected graph is incomplete, contradictory, or untrusted."""


class EntityType(str, Enum):
    """Every persisted application collection in ``SqlStore.SPECS``.

    Values intentionally equal physical table names.  The inventory test pins
    this enum bidirectionally to ``SPECS`` so a newly persisted model cannot be
    omitted from destructive-delete analysis.
    """

    SEASON_ROSTER_MEMBERSHIP_EVENT = "season_roster_membership_events"
    SEASON_ROSTER_MEMBERSHIP = "season_roster_memberships"
    SCHEDULE_SCENARIO = "schedule_scenarios"
    PROGRAM = "programs"
    SEASON = "seasons"
    LEAGUE = "leagues"
    LEAGUE_SEASON = "league_seasons"
    DIVISION = "divisions"
    AGE_ELIGIBILITY_RULE = "age_eligibility_rules"
    SEASON_TEAM_REGISTRATION = "season_team_registrations"
    SEASON_COPY_FORWARD_COMMIT = "season_copy_forward_commits"
    TEAM_LEAGUE_MIGRATION_DECISION = "team_league_migration_decisions"
    SEASON_VENUE_ACCESS = "season_venue_access"
    CLUB = "clubs"
    TEAM = "teams"
    PLAYER = "players"
    ORGANIZATION = "organizations"
    VENUE = "venues"
    RINK = "rinks"
    ICE_SLOT = "ice_slots"
    SCHEDULING_POLICY = "scheduling_policies"
    GAME = "games"
    GAME_ROSTER_ENTRY = "game_roster_entries"
    GAME_AVAILABILITY = "game_availability"
    SUBSTITUTE_ENROLLMENT = "substitute_enrollments"
    AUDIT_LOG = "audit_logs"
    NOTIFICATION_EVENT = "notification_events"
    SETUP_AUDIT_LOG = "setup_audit_logs"
    DATA_ACCESS_LOG = "data_access_logs"
    FACTORY_RESET_EVENT = "factory_reset_events"
    FACTORY_RESET_CHALLENGE = "factory_reset_challenges"
    FACTORY_RESET_LOCK = "factory_reset_locks"
    SUBTREE_DELETION_CHALLENGE = "subtree_deletion_challenges"
    OFFICIAL = "officials"
    OFFICIAL_ASSIGNMENT = "official_assignments"
    GAME_RESULT = "game_results"
    NOTIFICATION = "notifications_feed"
    NOTIFICATION_RECIPIENT = "notification_recipients"
    NOTIFICATION_DELIVERY = "notification_deliveries"
    CONTACT_DESTINATION = "contact_destinations"
    DEVICE_TOKEN = "device_tokens"
    NOTIFICATION_PREFERENCE = "notification_preferences"
    INSTALLATION_STATE = "installation_state"
    USER_ACCOUNT = "user_accounts"
    SESSION = "sessions"
    ACTIVE_CONTEXT = "user_active_context"
    GUARDIAN_LINK = "guardian_links"
    RESCHEDULE_REQUEST = "reschedule_requests"
    CALENDAR_FEED_TOKEN = "calendar_feed_tokens"
    OFFICIAL_AVAILABILITY = "official_availability"


class ReferenceRole(str, Enum):
    """Why a reference-looking field exists in the current data model."""

    OWNERSHIP = "ownership"
    ASSOCIATION = "association"
    SHARED = "shared"
    HISTORICAL = "historical"
    PRINCIPAL = "principal"
    EXTERNAL_KEY = "external_key"
    OPAQUE_SNAPSHOT = "opaque_snapshot"
    TRACE = "trace"


class TargetRemoval(str, Enum):
    """Effect on a source row when its referenced target is deleted."""

    DELETE_SOURCE = "delete_source"
    DETACH = "detach"
    RETAIN = "retain"
    NOT_GRAPH = "not_graph"


@dataclass(frozen=True)
class ReferenceSpec:
    source: EntityType
    field: str
    targets: tuple[EntityType, ...]
    role: ReferenceRole
    on_target_delete: TargetRemoval
    discriminator: Optional[str] = None

    @property
    def key(self) -> str:
        return f"{self.source.value}.{self.field}"


def _r(
    source: EntityType,
    field: str,
    targets: tuple[EntityType, ...],
    role: ReferenceRole,
    action: TargetRemoval,
    discriminator: Optional[str] = None,
) -> ReferenceSpec:
    return ReferenceSpec(source, field, targets, role, action, discriminator)


# This is the current ownership/dependency inventory, not a proposed database
# cascade.  Ordinary deletes remain dependency-gated.  ``DELETE_SOURCE`` is
# consulted only by the explicit #429 subtree projection.
REFERENCE_INVENTORY: tuple[ReferenceSpec, ...] = (
    _r(EntityType.SEASON_ROSTER_MEMBERSHIP_EVENT, "membership_id",
       (EntityType.SEASON_ROSTER_MEMBERSHIP,), ReferenceRole.OWNERSHIP,
       TargetRemoval.DELETE_SOURCE),
    _r(EntityType.SEASON_ROSTER_MEMBERSHIP_EVENT, "actor_id", (),
       ReferenceRole.PRINCIPAL, TargetRemoval.NOT_GRAPH),
    _r(EntityType.SEASON_ROSTER_MEMBERSHIP_EVENT, "detail", (),
       ReferenceRole.OPAQUE_SNAPSHOT, TargetRemoval.NOT_GRAPH),

    _r(EntityType.SEASON_ROSTER_MEMBERSHIP, "player_id", (EntityType.PLAYER,),
       ReferenceRole.ASSOCIATION, TargetRemoval.DELETE_SOURCE),
    _r(EntityType.SEASON_ROSTER_MEMBERSHIP, "league_season_id",
       (EntityType.LEAGUE_SEASON,), ReferenceRole.ASSOCIATION,
       TargetRemoval.DELETE_SOURCE),
    _r(EntityType.SEASON_ROSTER_MEMBERSHIP, "season_id", (EntityType.SEASON,),
       ReferenceRole.ASSOCIATION, TargetRemoval.DELETE_SOURCE),
    _r(EntityType.SEASON_ROSTER_MEMBERSHIP, "team_id", (EntityType.TEAM,),
       ReferenceRole.ASSOCIATION, TargetRemoval.DELETE_SOURCE),

    _r(EntityType.SCHEDULE_SCENARIO, "program_id", (EntityType.PROGRAM,),
       ReferenceRole.OWNERSHIP, TargetRemoval.DELETE_SOURCE),
    _r(EntityType.SCHEDULE_SCENARIO, "season_id", (EntityType.SEASON,),
       ReferenceRole.OWNERSHIP, TargetRemoval.DELETE_SOURCE),
    _r(EntityType.SCHEDULE_SCENARIO, "league_id", (EntityType.LEAGUE,),
       ReferenceRole.OWNERSHIP, TargetRemoval.DELETE_SOURCE),
    _r(EntityType.SCHEDULE_SCENARIO, "league_season_id",
       (EntityType.LEAGUE_SEASON,), ReferenceRole.OWNERSHIP,
       TargetRemoval.DELETE_SOURCE),
    _r(EntityType.SCHEDULE_SCENARIO, "division_id", (EntityType.DIVISION,),
       ReferenceRole.OWNERSHIP, TargetRemoval.DELETE_SOURCE),
    _r(EntityType.SCHEDULE_SCENARIO, "request_input", (),
       ReferenceRole.OPAQUE_SNAPSHOT, TargetRemoval.NOT_GRAPH),
    _r(EntityType.SCHEDULE_SCENARIO, "proposal", (),
       ReferenceRole.OPAQUE_SNAPSHOT, TargetRemoval.NOT_GRAPH),
    _r(EntityType.SCHEDULE_SCENARIO, "generation_snapshot", (),
       ReferenceRole.OPAQUE_SNAPSHOT, TargetRemoval.NOT_GRAPH),
    _r(EntityType.SCHEDULE_SCENARIO, "created_by", (),
       ReferenceRole.PRINCIPAL, TargetRemoval.NOT_GRAPH),

    _r(EntityType.PROGRAM, "operator_organization_id",
       (EntityType.ORGANIZATION,), ReferenceRole.SHARED, TargetRemoval.DETACH),
    _r(EntityType.PROGRAM, "external_ref", (), ReferenceRole.EXTERNAL_KEY,
       TargetRemoval.NOT_GRAPH),
    _r(EntityType.SEASON, "program_id", (EntityType.PROGRAM,),
       ReferenceRole.OWNERSHIP, TargetRemoval.DELETE_SOURCE),
    _r(EntityType.SEASON, "external_ref", (), ReferenceRole.EXTERNAL_KEY,
       TargetRemoval.NOT_GRAPH),
    _r(EntityType.LEAGUE, "program_id", (EntityType.PROGRAM,),
       ReferenceRole.OWNERSHIP, TargetRemoval.DELETE_SOURCE),
    _r(EntityType.LEAGUE, "external_ref", (), ReferenceRole.EXTERNAL_KEY,
       TargetRemoval.NOT_GRAPH),
    _r(EntityType.LEAGUE_SEASON, "league_id", (EntityType.LEAGUE,),
       ReferenceRole.ASSOCIATION, TargetRemoval.DELETE_SOURCE),
    _r(EntityType.LEAGUE_SEASON, "season_id", (EntityType.SEASON,),
       ReferenceRole.ASSOCIATION, TargetRemoval.DELETE_SOURCE),
    _r(EntityType.DIVISION, "league_season_id", (EntityType.LEAGUE_SEASON,),
       ReferenceRole.OWNERSHIP, TargetRemoval.DELETE_SOURCE),
    _r(EntityType.DIVISION, "external_ref", (), ReferenceRole.EXTERNAL_KEY,
       TargetRemoval.NOT_GRAPH),
    _r(EntityType.AGE_ELIGIBILITY_RULE, "league_season_id",
       (EntityType.LEAGUE_SEASON,), ReferenceRole.OWNERSHIP,
       TargetRemoval.DELETE_SOURCE),
    _r(EntityType.AGE_ELIGIBILITY_RULE, "actor_id", (),
       ReferenceRole.PRINCIPAL, TargetRemoval.NOT_GRAPH),
    _r(EntityType.SEASON_TEAM_REGISTRATION, "league_season_id",
       (EntityType.LEAGUE_SEASON,), ReferenceRole.ASSOCIATION,
       TargetRemoval.DELETE_SOURCE),
    _r(EntityType.SEASON_TEAM_REGISTRATION, "team_id", (EntityType.TEAM,),
       ReferenceRole.ASSOCIATION, TargetRemoval.DELETE_SOURCE),
    _r(EntityType.SEASON_TEAM_REGISTRATION, "division_id",
       (EntityType.DIVISION,), ReferenceRole.SHARED, TargetRemoval.DETACH),
    _r(EntityType.SEASON_COPY_FORWARD_COMMIT, "season_id",
       (EntityType.SEASON,), ReferenceRole.OWNERSHIP,
       TargetRemoval.DELETE_SOURCE),
    _r(EntityType.SEASON_COPY_FORWARD_COMMIT, "actor_id", (),
       ReferenceRole.PRINCIPAL, TargetRemoval.NOT_GRAPH),
    _r(EntityType.SEASON_COPY_FORWARD_COMMIT, "registration_ids", (),
       ReferenceRole.OPAQUE_SNAPSHOT, TargetRemoval.NOT_GRAPH),
    _r(EntityType.SEASON_COPY_FORWARD_COMMIT, "response_snapshot", (),
       ReferenceRole.OPAQUE_SNAPSHOT, TargetRemoval.NOT_GRAPH),
    _r(EntityType.SEASON_COPY_FORWARD_COMMIT, "request_identity", (),
       ReferenceRole.OPAQUE_SNAPSHOT, TargetRemoval.NOT_GRAPH),
    _r(EntityType.TEAM_LEAGUE_MIGRATION_DECISION, "team_id",
       (EntityType.TEAM,), ReferenceRole.ASSOCIATION,
       TargetRemoval.DELETE_SOURCE),
    _r(EntityType.TEAM_LEAGUE_MIGRATION_DECISION, "league_id",
       (EntityType.LEAGUE,), ReferenceRole.ASSOCIATION,
       TargetRemoval.DELETE_SOURCE),
    _r(EntityType.SEASON_VENUE_ACCESS, "season_id", (EntityType.SEASON,),
       ReferenceRole.ASSOCIATION, TargetRemoval.DELETE_SOURCE),
    _r(EntityType.SEASON_VENUE_ACCESS, "venue_id", (EntityType.VENUE,),
       ReferenceRole.ASSOCIATION, TargetRemoval.DELETE_SOURCE),

    _r(EntityType.CLUB, "external_ref", (), ReferenceRole.EXTERNAL_KEY,
       TargetRemoval.NOT_GRAPH),
    _r(EntityType.TEAM, "club_id", (EntityType.CLUB,), ReferenceRole.SHARED,
       TargetRemoval.DETACH),
    _r(EntityType.TEAM, "division_id", (EntityType.DIVISION,),
       ReferenceRole.HISTORICAL, TargetRemoval.RETAIN),
    _r(EntityType.TEAM, "external_ref", (), ReferenceRole.EXTERNAL_KEY,
       TargetRemoval.NOT_GRAPH),
    _r(EntityType.TEAM, "program_id", (EntityType.PROGRAM,),
       ReferenceRole.OWNERSHIP, TargetRemoval.DELETE_SOURCE),
    _r(EntityType.TEAM, "league_id", (EntityType.LEAGUE,),
       ReferenceRole.OWNERSHIP, TargetRemoval.DELETE_SOURCE),
    _r(EntityType.PLAYER, "team_id", (EntityType.TEAM,),
       ReferenceRole.OWNERSHIP, TargetRemoval.DELETE_SOURCE),
    _r(EntityType.PLAYER, "external_ref", (), ReferenceRole.EXTERNAL_KEY,
       TargetRemoval.NOT_GRAPH),
    _r(EntityType.ORGANIZATION, "external_ref", (), ReferenceRole.EXTERNAL_KEY,
       TargetRemoval.NOT_GRAPH),
    _r(EntityType.VENUE, "organization_id", (EntityType.ORGANIZATION,),
       ReferenceRole.OWNERSHIP, TargetRemoval.DELETE_SOURCE),
    _r(EntityType.VENUE, "league_id", (EntityType.PROGRAM,),
       ReferenceRole.SHARED, TargetRemoval.DETACH),
    _r(EntityType.VENUE, "external_ref", (), ReferenceRole.EXTERNAL_KEY,
       TargetRemoval.NOT_GRAPH),
    _r(EntityType.RINK, "venue_id", (EntityType.VENUE,),
       ReferenceRole.OWNERSHIP, TargetRemoval.DELETE_SOURCE),
    _r(EntityType.RINK, "external_ref", (), ReferenceRole.EXTERNAL_KEY,
       TargetRemoval.NOT_GRAPH),
    _r(EntityType.ICE_SLOT, "rink_id", (EntityType.RINK,),
       ReferenceRole.OWNERSHIP, TargetRemoval.DELETE_SOURCE),
    _r(EntityType.SCHEDULING_POLICY, "scope_id",
       (EntityType.PROGRAM, EntityType.SEASON, EntityType.RINK),
       ReferenceRole.OWNERSHIP, TargetRemoval.DELETE_SOURCE,
       discriminator="scope_type"),

    _r(EntityType.GAME, "home_team_id", (EntityType.TEAM,),
       ReferenceRole.ASSOCIATION, TargetRemoval.DELETE_SOURCE),
    _r(EntityType.GAME, "away_team_id", (EntityType.TEAM,),
       ReferenceRole.ASSOCIATION, TargetRemoval.DELETE_SOURCE),
    _r(EntityType.GAME, "season_id", (EntityType.SEASON,),
       ReferenceRole.OWNERSHIP, TargetRemoval.DELETE_SOURCE),
    _r(EntityType.GAME, "division_id", (EntityType.DIVISION,),
       ReferenceRole.OWNERSHIP, TargetRemoval.DELETE_SOURCE),
    _r(EntityType.GAME, "ice_slot_id", (EntityType.ICE_SLOT,),
       ReferenceRole.SHARED, TargetRemoval.DETACH),
    _r(EntityType.GAME, "league_id", (EntityType.LEAGUE,),
       ReferenceRole.OWNERSHIP, TargetRemoval.DELETE_SOURCE),
    _r(EntityType.GAME, "league_season_id", (EntityType.LEAGUE_SEASON,),
       ReferenceRole.OWNERSHIP, TargetRemoval.DELETE_SOURCE),
    _r(EntityType.GAME, "cancelled_ice_slot_id", (),
       ReferenceRole.OPAQUE_SNAPSHOT, TargetRemoval.NOT_GRAPH),
    _r(EntityType.GAME, "cancelled_venue_id", (),
       ReferenceRole.OPAQUE_SNAPSHOT, TargetRemoval.NOT_GRAPH),
    _r(EntityType.GAME, "cancelled_rink_id", (),
       ReferenceRole.OPAQUE_SNAPSHOT, TargetRemoval.NOT_GRAPH),
    _r(EntityType.GAME_ROSTER_ENTRY, "game_id", (EntityType.GAME,),
       ReferenceRole.OWNERSHIP, TargetRemoval.DELETE_SOURCE),
    _r(EntityType.GAME_ROSTER_ENTRY, "player_id", (EntityType.PLAYER,),
       ReferenceRole.ASSOCIATION, TargetRemoval.DELETE_SOURCE),
    _r(EntityType.GAME_ROSTER_ENTRY, "team_side", (EntityType.TEAM,),
       ReferenceRole.ASSOCIATION, TargetRemoval.DELETE_SOURCE),
    _r(EntityType.GAME_ROSTER_ENTRY, "selected_by", (),
       ReferenceRole.PRINCIPAL, TargetRemoval.NOT_GRAPH),
    _r(EntityType.GAME_AVAILABILITY, "game_id", (EntityType.GAME,),
       ReferenceRole.OWNERSHIP, TargetRemoval.DELETE_SOURCE),
    _r(EntityType.GAME_AVAILABILITY, "player_id", (EntityType.PLAYER,),
       ReferenceRole.ASSOCIATION, TargetRemoval.DELETE_SOURCE),
    _r(EntityType.SUBSTITUTE_ENROLLMENT, "game_id", (EntityType.GAME,),
       ReferenceRole.OWNERSHIP, TargetRemoval.DELETE_SOURCE),
    _r(EntityType.SUBSTITUTE_ENROLLMENT, "player_id", (EntityType.PLAYER,),
       ReferenceRole.ASSOCIATION, TargetRemoval.DELETE_SOURCE),
    _r(EntityType.SUBSTITUTE_ENROLLMENT, "team_id", (EntityType.TEAM,),
       ReferenceRole.ASSOCIATION, TargetRemoval.DELETE_SOURCE),
    # Cross-team opt-in provenance is frozen history, deliberately without
    # foreign keys (migration 063). Deleting a former source membership or
    # team must not rewrite or cascade the substitute record.
    _r(EntityType.SUBSTITUTE_ENROLLMENT, "source_membership_id",
       (EntityType.SEASON_ROSTER_MEMBERSHIP,), ReferenceRole.HISTORICAL,
       TargetRemoval.RETAIN),
    _r(EntityType.SUBSTITUTE_ENROLLMENT, "source_team_id", (EntityType.TEAM,),
       ReferenceRole.HISTORICAL, TargetRemoval.RETAIN),
    _r(EntityType.AUDIT_LOG, "game_id", (EntityType.GAME,),
       ReferenceRole.OWNERSHIP, TargetRemoval.DELETE_SOURCE),
    _r(EntityType.AUDIT_LOG, "actor_id", (), ReferenceRole.PRINCIPAL,
       TargetRemoval.NOT_GRAPH),
    _r(EntityType.AUDIT_LOG, "subject_player_id", (EntityType.PLAYER,),
       ReferenceRole.ASSOCIATION, TargetRemoval.DELETE_SOURCE),
    _r(EntityType.AUDIT_LOG, "detail", (), ReferenceRole.OPAQUE_SNAPSHOT,
       TargetRemoval.NOT_GRAPH),
    _r(EntityType.NOTIFICATION_EVENT, "game_id", (EntityType.GAME,),
       ReferenceRole.OWNERSHIP, TargetRemoval.DELETE_SOURCE),
    _r(EntityType.NOTIFICATION_EVENT, "audience", (), ReferenceRole.TRACE,
       TargetRemoval.NOT_GRAPH),
    _r(EntityType.NOTIFICATION_EVENT, "subject_player_id", (EntityType.PLAYER,),
       ReferenceRole.ASSOCIATION, TargetRemoval.DELETE_SOURCE),

    _r(EntityType.SETUP_AUDIT_LOG, "entity_id", (
       EntityType.AGE_ELIGIBILITY_RULE, EntityType.CLUB, EntityType.DIVISION,
       EntityType.GAME, EntityType.GAME_RESULT, EntityType.ICE_SLOT,
       EntityType.LEAGUE, EntityType.LEAGUE_SEASON, EntityType.OFFICIAL,
       EntityType.OFFICIAL_ASSIGNMENT, EntityType.OFFICIAL_AVAILABILITY,
       EntityType.ORGANIZATION, EntityType.PLAYER, EntityType.PROGRAM,
       EntityType.RINK, EntityType.SCHEDULING_POLICY, EntityType.SEASON,
       EntityType.SEASON_ROSTER_MEMBERSHIP,
       EntityType.SEASON_TEAM_REGISTRATION, EntityType.SEASON_VENUE_ACCESS,
       EntityType.TEAM, EntityType.USER_ACCOUNT, EntityType.VENUE,
    ),
       ReferenceRole.HISTORICAL, TargetRemoval.RETAIN,
       discriminator="entity_type"),
    _r(EntityType.SETUP_AUDIT_LOG, "actor_id", (), ReferenceRole.PRINCIPAL,
       TargetRemoval.NOT_GRAPH),
    _r(EntityType.SETUP_AUDIT_LOG, "detail", (),
       ReferenceRole.OPAQUE_SNAPSHOT, TargetRemoval.NOT_GRAPH),
    _r(EntityType.DATA_ACCESS_LOG, "subject_id",
       (EntityType.TEAM, EntityType.PLAYER, EntityType.OFFICIAL),
       ReferenceRole.HISTORICAL, TargetRemoval.RETAIN,
       discriminator="category"),
    _r(EntityType.DATA_ACCESS_LOG, "actor_user_id", (),
       ReferenceRole.PRINCIPAL, TargetRemoval.NOT_GRAPH),
    _r(EntityType.DATA_ACCESS_LOG, "request_id", (), ReferenceRole.TRACE,
       TargetRemoval.NOT_GRAPH),
    _r(EntityType.FACTORY_RESET_EVENT, "actor_id", (),
       ReferenceRole.PRINCIPAL, TargetRemoval.NOT_GRAPH),
    _r(EntityType.FACTORY_RESET_EVENT, "pre_reset_counts", (),
       ReferenceRole.OPAQUE_SNAPSHOT, TargetRemoval.NOT_GRAPH),
    _r(EntityType.FACTORY_RESET_CHALLENGE, "actor_id", (),
       ReferenceRole.PRINCIPAL, TargetRemoval.NOT_GRAPH),
    _r(EntityType.FACTORY_RESET_CHALLENGE, "counts", (),
       ReferenceRole.OPAQUE_SNAPSHOT, TargetRemoval.NOT_GRAPH),
    _r(EntityType.FACTORY_RESET_LOCK, "actor_id", (),
       ReferenceRole.PRINCIPAL, TargetRemoval.NOT_GRAPH),
    _r(EntityType.SUBTREE_DELETION_CHALLENGE, "actor_id", (),
       ReferenceRole.PRINCIPAL, TargetRemoval.NOT_GRAPH),
    _r(EntityType.SUBTREE_DELETION_CHALLENGE, "root_id", (),
       ReferenceRole.TRACE, TargetRemoval.NOT_GRAPH),

    _r(EntityType.OFFICIAL, "home_club_id", (EntityType.CLUB,),
       ReferenceRole.SHARED, TargetRemoval.DETACH),
    _r(EntityType.OFFICIAL, "external_ref", (), ReferenceRole.EXTERNAL_KEY,
       TargetRemoval.NOT_GRAPH),
    _r(EntityType.OFFICIAL_ASSIGNMENT, "game_id", (EntityType.GAME,),
       ReferenceRole.OWNERSHIP, TargetRemoval.DELETE_SOURCE),
    _r(EntityType.OFFICIAL_ASSIGNMENT, "official_id", (EntityType.OFFICIAL,),
       ReferenceRole.ASSOCIATION, TargetRemoval.DELETE_SOURCE),
    _r(EntityType.OFFICIAL_ASSIGNMENT, "assigned_by", (),
       ReferenceRole.PRINCIPAL, TargetRemoval.NOT_GRAPH),
    _r(EntityType.GAME_RESULT, "game_id", (EntityType.GAME,),
       ReferenceRole.OWNERSHIP, TargetRemoval.DELETE_SOURCE),
    _r(EntityType.GAME_RESULT, "recorded_by", (), ReferenceRole.PRINCIPAL,
       TargetRemoval.NOT_GRAPH),
    _r(EntityType.GAME_RESULT, "approved_by", (), ReferenceRole.PRINCIPAL,
       TargetRemoval.NOT_GRAPH),
    _r(EntityType.NOTIFICATION, "audience", (), ReferenceRole.TRACE,
       TargetRemoval.NOT_GRAPH),
    _r(EntityType.NOTIFICATION, "audience_ref",
       (EntityType.TEAM, EntityType.OFFICIAL, EntityType.PLAYER),
       ReferenceRole.ASSOCIATION,
       TargetRemoval.DELETE_SOURCE, discriminator="audience"),
    _r(EntityType.NOTIFICATION, "game_id", (EntityType.GAME,),
       ReferenceRole.OWNERSHIP, TargetRemoval.DELETE_SOURCE),
    _r(EntityType.NOTIFICATION, "assignment_id",
       (EntityType.OFFICIAL_ASSIGNMENT,), ReferenceRole.OWNERSHIP,
       TargetRemoval.DELETE_SOURCE),
    _r(EntityType.NOTIFICATION_RECIPIENT, "notification_id",
       (EntityType.NOTIFICATION,), ReferenceRole.OWNERSHIP,
       TargetRemoval.DELETE_SOURCE),
    _r(EntityType.NOTIFICATION_RECIPIENT, "actor_key",
       (EntityType.USER_ACCOUNT, EntityType.TEAM, EntityType.OFFICIAL,
        EntityType.PLAYER), ReferenceRole.ASSOCIATION,
       TargetRemoval.DELETE_SOURCE, discriminator="actor_key prefix"),
    _r(EntityType.NOTIFICATION_DELIVERY, "notification_id",
       (EntityType.NOTIFICATION,), ReferenceRole.OWNERSHIP,
       TargetRemoval.DELETE_SOURCE),
    _r(EntityType.NOTIFICATION_DELIVERY, "recipient_ref",
       (EntityType.TEAM, EntityType.OFFICIAL, EntityType.PLAYER),
       ReferenceRole.ASSOCIATION, TargetRemoval.DELETE_SOURCE,
       discriminator="recipient_ref prefix"),
    _r(EntityType.CONTACT_DESTINATION, "recipient_ref",
       (EntityType.TEAM, EntityType.OFFICIAL, EntityType.PLAYER),
       ReferenceRole.ASSOCIATION, TargetRemoval.DELETE_SOURCE,
       discriminator="recipient_ref prefix"),
    _r(EntityType.DEVICE_TOKEN, "recipient_ref",
       (EntityType.TEAM, EntityType.OFFICIAL, EntityType.PLAYER),
       ReferenceRole.ASSOCIATION, TargetRemoval.DELETE_SOURCE,
       discriminator="recipient_ref prefix"),
    _r(EntityType.NOTIFICATION_PREFERENCE, "recipient_ref",
       (EntityType.TEAM, EntityType.OFFICIAL, EntityType.PLAYER),
       ReferenceRole.ASSOCIATION, TargetRemoval.DELETE_SOURCE,
       discriminator="recipient_ref prefix"),
    _r(EntityType.INSTALLATION_STATE, "claimed_by_user_id", (),
       ReferenceRole.PRINCIPAL, TargetRemoval.NOT_GRAPH),
    _r(EntityType.USER_ACCOUNT, "scope",
       (EntityType.TEAM, EntityType.PLAYER, EntityType.OFFICIAL),
       ReferenceRole.SHARED, TargetRemoval.DETACH,
       discriminator="role and scope key"),
    _r(EntityType.SESSION, "user_id", (EntityType.USER_ACCOUNT,),
       ReferenceRole.OWNERSHIP, TargetRemoval.DELETE_SOURCE),
    _r(EntityType.ACTIVE_CONTEXT, "id", (EntityType.USER_ACCOUNT,),
       ReferenceRole.ASSOCIATION, TargetRemoval.DELETE_SOURCE),
    _r(EntityType.ACTIVE_CONTEXT, "program_id", (EntityType.PROGRAM,),
       ReferenceRole.SHARED, TargetRemoval.DETACH),
    _r(EntityType.ACTIVE_CONTEXT, "season_id", (EntityType.SEASON,),
       ReferenceRole.SHARED, TargetRemoval.DETACH),
    _r(EntityType.ACTIVE_CONTEXT, "league_id", (EntityType.LEAGUE,),
       ReferenceRole.SHARED, TargetRemoval.DETACH),
    _r(EntityType.GUARDIAN_LINK, "guardian_user_id", (EntityType.USER_ACCOUNT,),
       ReferenceRole.ASSOCIATION, TargetRemoval.DELETE_SOURCE),
    _r(EntityType.GUARDIAN_LINK, "player_id", (EntityType.PLAYER,),
       ReferenceRole.ASSOCIATION, TargetRemoval.DELETE_SOURCE),
    _r(EntityType.RESCHEDULE_REQUEST, "game_id", (EntityType.GAME,),
       ReferenceRole.OWNERSHIP, TargetRemoval.DELETE_SOURCE),
    _r(EntityType.RESCHEDULE_REQUEST, "requested_by_team_id", (EntityType.TEAM,),
       ReferenceRole.ASSOCIATION, TargetRemoval.DELETE_SOURCE),
    _r(EntityType.RESCHEDULE_REQUEST, "new_ice_slot_id", (EntityType.ICE_SLOT,),
       ReferenceRole.SHARED, TargetRemoval.DETACH),
    _r(EntityType.CALENDAR_FEED_TOKEN, "actor_ref",
       (EntityType.TEAM, EntityType.DIVISION, EntityType.OFFICIAL,
        EntityType.PLAYER), ReferenceRole.ASSOCIATION,
       TargetRemoval.DELETE_SOURCE, discriminator="actor_type"),
    _r(EntityType.CALENDAR_FEED_TOKEN, "created_by", (),
       ReferenceRole.PRINCIPAL, TargetRemoval.NOT_GRAPH),
    _r(EntityType.CALENDAR_FEED_TOKEN, "revoked_by", (),
       ReferenceRole.PRINCIPAL, TargetRemoval.NOT_GRAPH),
    _r(EntityType.OFFICIAL_AVAILABILITY, "official_id", (EntityType.OFFICIAL,),
       ReferenceRole.OWNERSHIP, TargetRemoval.DELETE_SOURCE),
)


def _validate_inventory() -> dict[str, ReferenceSpec]:
    by_key: dict[str, ReferenceSpec] = {}
    role_action = {
        ReferenceRole.OWNERSHIP: TargetRemoval.DELETE_SOURCE,
        ReferenceRole.ASSOCIATION: TargetRemoval.DELETE_SOURCE,
        ReferenceRole.SHARED: TargetRemoval.DETACH,
        ReferenceRole.HISTORICAL: TargetRemoval.RETAIN,
        ReferenceRole.PRINCIPAL: TargetRemoval.NOT_GRAPH,
        ReferenceRole.EXTERNAL_KEY: TargetRemoval.NOT_GRAPH,
        ReferenceRole.OPAQUE_SNAPSHOT: TargetRemoval.NOT_GRAPH,
        ReferenceRole.TRACE: TargetRemoval.NOT_GRAPH,
    }
    for spec in REFERENCE_INVENTORY:
        if spec.key in by_key:
            raise RuntimeError(
                f"duplicate destructive-subtree inventory field {spec.key}")
        if role_action[spec.role] is not spec.on_target_delete:
            raise RuntimeError(
                f"{spec.key} has contradictory role/removal semantics")
        if len(set(spec.targets)) != len(spec.targets):
            raise RuntimeError(f"{spec.key} repeats a target type")
        if spec.on_target_delete is TargetRemoval.NOT_GRAPH:
            if spec.targets:
                raise RuntimeError(f"non-graph field {spec.key} has targets")
        elif not spec.targets:
            raise RuntimeError(f"live relationship {spec.key} has no target")
        if len(spec.targets) > 1 and not spec.discriminator:
            raise RuntimeError(
                f"polymorphic relationship {spec.key} has no discriminator")
        by_key[spec.key] = spec
    return by_key


REFERENCE_BY_KEY = _validate_inventory()


@dataclass(frozen=True, order=True)
class RecordRef:
    """One privacy-filtered record in the projected graph.

    ``state_fingerprint`` is a lowercase SHA-256 digest of every material field
    the projector read.  The digest, rather than a child payload, lets execution
    detect row changes without returning private data.
    """

    entity_type: EntityType
    record_id: str
    state_fingerprint: str

    def __post_init__(self) -> None:
        if not isinstance(self.entity_type, EntityType):
            raise PreviewContractError("record entity_type must be EntityType")
        if not isinstance(self.record_id, str) or not self.record_id.strip() \
                or self.record_id != self.record_id.strip():
            raise PreviewContractError("record_id must be a non-empty string")
        if len(self.record_id) > 200 or any(ord(c) < 32 for c in self.record_id):
            raise PreviewContractError("record_id is not safe for preview output")
        fp = self.state_fingerprint
        if not isinstance(fp, str) or len(fp) != 64 \
                or any(c not in "0123456789abcdef" for c in fp):
            raise PreviewContractError(
                "state_fingerprint must be a lowercase SHA-256 hex digest")

    @property
    def key(self) -> tuple[str, str]:
        return self.entity_type.value, self.record_id


@dataclass(frozen=True, order=True)
class ProjectedEdge:
    """One resolved instance of a catalogued relationship."""

    inventory_key: str
    source: RecordRef
    target: RecordRef

    def __post_init__(self) -> None:
        spec = REFERENCE_BY_KEY.get(self.inventory_key)
        if spec is None:
            raise PreviewContractError(
                f"unknown relationship inventory key {self.inventory_key!r}")
        if spec.on_target_delete is TargetRemoval.NOT_GRAPH:
            raise PreviewContractError(
                f"{self.inventory_key} is not a live graph relationship")
        if self.source.entity_type is not spec.source:
            raise PreviewContractError(
                f"{self.inventory_key} source must be {spec.source.value}")
        if self.target.entity_type not in spec.targets:
            raise PreviewContractError(
                f"{self.inventory_key} cannot target "
                f"{self.target.entity_type.value}")

    @property
    def key(self) -> tuple[str, tuple[str, str], tuple[str, str]]:
        return self.inventory_key, self.source.key, self.target.key


@dataclass(frozen=True)
class PreviewGroup:
    entity_type: EntityType
    count: int
    record_ids: tuple[str, ...]


@dataclass(frozen=True)
class EdgePreview:
    inventory_key: str
    source_type: EntityType
    source_id: str
    target_type: EntityType
    target_id: str


@dataclass(frozen=True)
class EdgeGroup:
    inventory_key: str
    count: int
    edges: tuple[EdgePreview, ...]


@dataclass(frozen=True)
class SubtreePreview:
    root_type: EntityType
    root_id: str
    confirmation_name: str
    fingerprint: str
    delete_groups: tuple[PreviewGroup, ...]
    retained_groups: tuple[PreviewGroup, ...]
    removed_relationship_groups: tuple[EdgeGroup, ...]
    detached_relationship_groups: tuple[EdgeGroup, ...]
    retained_relationship_groups: tuple[EdgeGroup, ...]

    @property
    def delete_count(self) -> int:
        return sum(group.count for group in self.delete_groups)

    @staticmethod
    def _flatten(groups: tuple[EdgeGroup, ...]) -> tuple[EdgePreview, ...]:
        return tuple(edge for group in groups for edge in group.edges)

    @property
    def removed_edges(self) -> tuple[EdgePreview, ...]:
        return self._flatten(self.removed_relationship_groups)

    @property
    def detached_edges(self) -> tuple[EdgePreview, ...]:
        return self._flatten(self.detached_relationship_groups)

    @property
    def retained_edges(self) -> tuple[EdgePreview, ...]:
        return self._flatten(self.retained_relationship_groups)


def _unique_records(records: Iterable[RecordRef]) -> dict[tuple[str, str], RecordRef]:
    by_key: dict[tuple[str, str], RecordRef] = {}
    for record in records:
        if not isinstance(record, RecordRef):
            raise PreviewContractError("records must contain RecordRef values")
        if record.key in by_key:
            raise PreviewContractError(
                f"duplicate projected record {record.entity_type.value}:"
                f"{record.record_id}")
        by_key[record.key] = record
    return by_key


def _group(records: Iterable[RecordRef]) -> tuple[PreviewGroup, ...]:
    grouped: dict[EntityType, list[str]] = {}
    for record in records:
        grouped.setdefault(record.entity_type, []).append(record.record_id)
    return tuple(
        PreviewGroup(kind, len(ids), tuple(sorted(ids)))
        for kind, ids in sorted(grouped.items(), key=lambda item: item[0].value)
    )


def _edge_preview(edge: ProjectedEdge) -> EdgePreview:
    return EdgePreview(
        edge.inventory_key,
        edge.source.entity_type,
        edge.source.record_id,
        edge.target.entity_type,
        edge.target.record_id,
    )


def _group_edges(edges: Iterable[ProjectedEdge]) -> tuple[EdgeGroup, ...]:
    grouped: dict[str, list[ProjectedEdge]] = {}
    for edge in edges:
        grouped.setdefault(edge.inventory_key, []).append(edge)
    groups = []
    for inventory_key, values in sorted(grouped.items()):
        previews = tuple(
            _edge_preview(edge)
            for edge in sorted(values, key=lambda item: item.key)
        )
        groups.append(EdgeGroup(inventory_key, len(previews), previews))
    return tuple(groups)


def _canonical_payload(
    actor_id: str,
    root: RecordRef,
    confirmation_name: str,
    records: Iterable[RecordRef],
    edges: Iterable[ProjectedEdge],
) -> bytes:
    payload = {
        "contract": 1,
        "actor_id": actor_id,
        "root": [root.entity_type.value, root.record_id,
                 root.state_fingerprint],
        "confirmation_name": confirmation_name,
        "records": [
            [r.entity_type.value, r.record_id, r.state_fingerprint]
            for r in sorted(records, key=lambda item: item.key)
        ],
        "edges": [
            [e.inventory_key, *e.source.key, *e.target.key]
            for e in sorted(edges, key=lambda item: item.key)
        ],
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")


def build_subtree_preview(
    *,
    actor_id: str,
    root: RecordRef,
    confirmation_name: str,
    records: Iterable[RecordRef],
    edges: Iterable[ProjectedEdge],
) -> SubtreePreview:
    """Build a deterministic, actor-bound preview from a complete graph.

    This is a planning operation only.  The execution boundary re-projects the
    graph under its transaction locks and compares the resulting fingerprint
    before performing any write; this function does not make a token single-use
    and does not authorize its caller.
    """

    if not isinstance(actor_id, str) or not actor_id.strip() \
            or actor_id != actor_id.strip():
        raise PreviewContractError("actor_id must be a non-empty trusted value")
    if len(actor_id) > 200 or any(ord(c) < 32 for c in actor_id):
        raise PreviewContractError("actor_id is not safe for fingerprint input")
    if not isinstance(confirmation_name, str) or not confirmation_name.strip():
        raise PreviewContractError("confirmation_name must be non-empty")
    confirmation_name = confirmation_name.strip()
    if len(confirmation_name) > 200 or any(ord(c) < 32 for c in confirmation_name):
        raise PreviewContractError("confirmation_name is not safe for output")

    record_by_key = _unique_records(records)
    if root.key not in record_by_key:
        raise PreviewContractError("the selected root is absent from the graph")
    if record_by_key[root.key] != root:
        raise PreviewContractError("the selected root state does not match the graph")

    edge_values = tuple(edges)
    edge_keys: set[tuple[str, tuple[str, str], tuple[str, str]]] = set()
    for edge in edge_values:
        if not isinstance(edge, ProjectedEdge):
            raise PreviewContractError("edges must contain ProjectedEdge values")
        if edge.key in edge_keys:
            raise PreviewContractError(f"duplicate projected edge {edge.key!r}")
        edge_keys.add(edge.key)
        if edge.source.key not in record_by_key or edge.target.key not in record_by_key:
            raise PreviewContractError(
                f"edge {edge.inventory_key} has an endpoint outside the graph")
        if record_by_key[edge.source.key] != edge.source \
                or record_by_key[edge.target.key] != edge.target:
            raise PreviewContractError(
                f"edge {edge.inventory_key} endpoint state disagrees with graph")

    deleting = {root.key}
    changed = True
    while changed:
        changed = False
        for edge in edge_values:
            spec = REFERENCE_BY_KEY[edge.inventory_key]
            if spec.on_target_delete is TargetRemoval.DELETE_SOURCE \
                    and edge.target.key in deleting \
                    and edge.source.key not in deleting:
                deleting.add(edge.source.key)
                changed = True

    removed: list[ProjectedEdge] = []
    detached: list[ProjectedEdge] = []
    retained_links: list[ProjectedEdge] = []
    retained_keys: set[tuple[str, str]] = set()
    for edge in edge_values:
        spec = REFERENCE_BY_KEY[edge.inventory_key]
        source_deleted = edge.source.key in deleting
        target_deleted = edge.target.key in deleting
        if source_deleted:
            removed.append(edge)
            if not target_deleted:
                retained_keys.add(edge.target.key)
        elif target_deleted and spec.on_target_delete is TargetRemoval.DETACH:
            detached.append(edge)
            retained_keys.add(edge.source.key)
        elif target_deleted and spec.on_target_delete is TargetRemoval.RETAIN:
            retained_links.append(edge)
            retained_keys.add(edge.source.key)

    deleting_records = [record_by_key[key] for key in deleting]
    retained_records = [record_by_key[key] for key in retained_keys
                        if key not in deleting]
    canonical = _canonical_payload(actor_id, root, confirmation_name,
                                   record_by_key.values(), edge_values)

    return SubtreePreview(
        root.entity_type,
        root.record_id,
        confirmation_name,
        hashlib.sha256(canonical).hexdigest(),
        _group(deleting_records),
        _group(retained_records),
        _group_edges(removed),
        _group_edges(detached),
        _group_edges(retained_links),
    )
