"""Enumerations for the roster + substitute domain.

Values are lowercase strings so they serialize directly into the JSON API
contract (see docs/architecture/api-contract.md).
"""

from enum import Enum


class Position(str, Enum):
    GOALIE = "goalie"
    DEFENSE = "defense"
    FORWARD = "forward"
    SKATER = "skater"  # generic non-goalie

    @property
    def slot_type(self) -> "SlotType":
        """Map a player's position to the slot it fills.

        Goalies fill goalie slots; everyone else fills skater slots.
        """
        return SlotType.GOALIE if self is Position.GOALIE else SlotType.SKATER


class SlotType(str, Enum):
    GOALIE = "goalie"
    SKATER = "skater"


class SlotStatus(str, Enum):
    FULL = "full"
    OPEN = "open"
    NEEDS_COACH_DECISION = "needs_coach_decision"


class RosterRole(str, Enum):
    SELECTED = "selected"
    SUBSTITUTE_ADDED = "substitute_added"


class SelectionSource(str, Enum):
    COACH_SELECTED = "coach_selected"
    SUBSTITUTE_POOL = "substitute_pool"
    MANUAL_OVERRIDE = "manual_override"
    AUTO_FILL = "auto_fill"


class RosterEntryStatus(str, Enum):
    SELECTED = "selected"
    CONFIRMED = "confirmed"
    UNAVAILABLE = "unavailable"
    REMOVED = "removed"
    OFFERED = "offered"
    ACCEPTED = "accepted"

    @property
    def occupies_slot(self) -> bool:
        """True when this entry is holding a roster slot."""
        return self in {
            RosterEntryStatus.SELECTED,
            RosterEntryStatus.CONFIRMED,
            RosterEntryStatus.OFFERED,
            RosterEntryStatus.ACCEPTED,
        }

    @property
    def is_confirmed_body(self) -> bool:
        """True when this entry counts as a confirmed, attending player."""
        return self in {RosterEntryStatus.CONFIRMED, RosterEntryStatus.ACCEPTED}


class AvailabilityStatus(str, Enum):
    PENDING = "pending"
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"
    MAYBE = "maybe"


class SubstituteStatus(str, Enum):
    ENROLLED = "enrolled"
    OFFERED = "offered"
    ACCEPTED = "accepted"
    DECLINED = "declined"
    EXPIRED = "expired"
    WITHDRAWN = "withdrawn"
    CANCELLED = "cancelled"


class GameStatus(str, Enum):
    DRAFT = "draft"
    SELECTED = "selected"
    AWAITING_RESPONSES = "awaiting_responses"
    ROSTER_CONFIRMED = "roster_confirmed"
    NEEDS_SUBSTITUTE = "needs_substitute"
    OPEN_SLOT = "open_slot"
    LOCKED = "locked"
    FINAL = "final"


class AuditAction(str, Enum):
    ROSTER_SELECTED = "roster_selected"
    AVAILABILITY_SET = "availability_set"
    PLAYER_BACKED_OUT = "player_backed_out"
    SUBSTITUTE_ENROLLED = "substitute_enrolled"
    SUBSTITUTE_WITHDRAWN = "substitute_withdrawn"
    SUBSTITUTE_OFFERED = "substitute_offered"
    SUBSTITUTE_ACCEPTED = "substitute_accepted"
    SUBSTITUTE_DECLINED = "substitute_declined"
    SUBSTITUTE_ADDED_TO_ROSTER = "substitute_added_to_roster"
    PLAYER_REMOVED = "player_removed"
    ROSTER_LOCKED = "roster_locked"
    ROSTER_UNLOCKED = "roster_unlocked"
    GAME_CANCELLED = "game_cancelled"


class ResultStatus(str, Enum):
    DRAFT = "draft"      # score entered, not yet approved
    FINAL = "final"      # approved — affects standings


class OfficialRole(str, Enum):
    REFEREE = "referee"
    LINESPERSON = "linesperson"
    SCOREKEEPER = "scorekeeper"


class OfficialAssignmentStatus(str, Enum):
    PROPOSED = "proposed"      # offered to the official, awaiting response
    ACCEPTED = "accepted"
    DECLINED = "declined"

    @property
    def is_active(self) -> bool:
        # Proposed or accepted assignments hold the official's time.
        return self in {OfficialAssignmentStatus.PROPOSED,
                        OfficialAssignmentStatus.ACCEPTED}


class IceSlotType(str, Enum):
    GAME = "game"
    PRACTICE = "practice"
    TOURNAMENT = "tournament"
    MAINTENANCE = "maintenance"
    PUBLIC_SKATE = "public_skate"


class IceSlotStatus(str, Enum):
    AVAILABLE = "available"
    ALLOCATED = "allocated"
    BLOCKED = "blocked"


class OfficialAvailabilityStatus(str, Enum):
    """Whether an official's declared window is available or unavailable (#88)."""
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"


class NotificationType(str, Enum):
    PLAYER_BACKED_OUT = "player_backed_out"
    SLOT_OPEN = "slot_open"
    SUBSTITUTE_ENROLLED = "substitute_enrolled"
    SUBSTITUTE_OFFERED = "substitute_offered"
    SUBSTITUTE_ACCEPTED = "substitute_accepted"
    ROSTER_LOCKED = "roster_locked"


class NotificationKind(str, Enum):
    """Feed notification categories delivered to a recipient (#32)."""
    ASSIGNMENT_OFFERED = "assignment_offered"      # → the official
    ASSIGNMENT_ACCEPTED = "assignment_accepted"    # → schedulers
    ASSIGNMENT_DECLINED = "assignment_declined"    # → schedulers
    ROSTER_OPEN_SLOT = "roster_open_slot"          # → the coach
    # Substitute outreach (#112) → the offered player / the team's coach.
    SUBSTITUTE_OFFERED = "substitute_offered"      # → the player
    SUBSTITUTE_ACCEPTED = "substitute_accepted"    # → the coach
    SUBSTITUTE_DECLINED = "substitute_declined"    # → the coach
    RESULT_APPROVED = "result_approved"            # → public feed
    # Schedule-change notifications (#87) → affected teams / officials / public.
    GAME_PUBLISHED = "game_published"
    GAME_MOVED = "game_moved"
    GAME_CANCELLED = "game_cancelled"
    ASSIGNMENT_UNASSIGNED = "assignment_unassigned"
    ROSTER_LOCKED = "roster_locked"
    ROSTER_UNLOCKED = "roster_unlocked"
    AVAILABILITY_REMINDER = "availability_reminder"  # → each unresponded player (#89)


class NotificationAudience(str, Enum):
    """Who a feed notification is for (matched against the signed-in session)."""
    PUBLIC = "public"        # everyone
    SCHEDULER = "scheduler"  # league admin / arena manager
    COACH = "coach"          # a team's coach (audience_ref = team_id)
    OFFICIAL = "official"    # a specific official (audience_ref = official_id)
    PLAYER = "player"        # a specific player (audience_ref = player_id)


class NotificationChannel(str, Enum):
    """Out-of-app delivery channels for the mock delivery worker (#58).

    Delivery is mocked in this slice — no real SMTP / APNs / FCM. These name
    the queues a notification fans out to when it is emitted.
    """
    EMAIL = "email"
    PUSH = "push"


class DeliveryStatus(str, Enum):
    """Lifecycle of a single :class:`NotificationDelivery` (#58)."""
    PENDING = "pending"  # queued, not yet attempted (or awaiting retry)
    SENT = "sent"        # delivered by the (mock) sender
    FAILED = "failed"    # last attempt failed; retried until attempts exhausted
    DEAD_LETTERED = "dead_lettered"  # attempts exhausted; parked for operator (#80)
    IGNORED = "ignored"  # operator marked it as won't-deliver (#80)
