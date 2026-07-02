"""Organization & arena domain models (League + Arena setup slice).

These describe the scheduling universe a league/arena operator builds before
games exist: league → season → division, club → team, and venue → rink →
ice slot. Plain data holders; all rules live in the service layer.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from .enums import (
    DeliveryStatus,
    IceSlotStatus,
    IceSlotType,
    NotificationAudience,
    NotificationChannel,
    NotificationKind,
    OfficialAssignmentStatus,
    OfficialRole,
    ResultStatus,
)


@dataclass
class League:
    id: str
    name: str
    country: str = ""
    timezone: str = "UTC"


@dataclass
class Season:
    id: str
    league_id: str
    name: str
    start_date: Optional[datetime] = None
    end_date: Optional[datetime] = None


@dataclass
class Division:
    id: str
    season_id: str
    name: str
    age_group: str = ""


@dataclass
class Club:
    id: str
    name: str
    country: str = ""


@dataclass
class Venue:
    id: str
    name: str
    address: str = ""
    timezone: str = "UTC"


@dataclass
class Rink:
    id: str
    venue_id: str
    name: str


@dataclass
class IceSlot:
    id: str
    rink_id: str
    start_time: datetime
    end_time: datetime
    slot_type: IceSlotType = IceSlotType.GAME
    status: IceSlotStatus = IceSlotStatus.AVAILABLE


@dataclass
class Official:
    """A match official who can be assigned to games (#30)."""
    id: str
    name: str
    home_club_id: Optional[str] = None   # for conflict-of-interest checks
    is_active: bool = True


@dataclass
class OfficialAssignment:
    id: str
    game_id: str
    official_id: str
    role: OfficialRole
    status: OfficialAssignmentStatus = OfficialAssignmentStatus.PROPOSED
    assigned_at: Optional[datetime] = None
    responded_at: Optional[datetime] = None
    assigned_by: Optional[str] = None
    note: str = ""


@dataclass
class GameResult:
    """Final (or draft) score for a game (#31). Only FINAL affects standings."""
    id: str
    game_id: str
    home_score: int
    away_score: int
    status: ResultStatus = ResultStatus.DRAFT
    recorded_by: Optional[str] = None
    recorded_at: Optional[datetime] = None
    approved_by: Optional[str] = None
    approved_at: Optional[datetime] = None


@dataclass
class Notification:
    """A feed notification addressed to an audience (#32).

    Read state is tracked per actor via :class:`NotificationRecipient` (#57),
    so a shared/public notification can be read by one recipient without
    changing anyone else's unread count.
    """
    id: str
    kind: NotificationKind
    audience: NotificationAudience
    title: str
    message: str
    at: datetime
    audience_ref: Optional[str] = None   # official_id / team_id (None = whole audience)
    game_id: Optional[str] = None
    assignment_id: Optional[str] = None


@dataclass
class NotificationRecipient:
    """Per-actor read state for a feed notification (#57).

    A row exists only once an actor has read the notification; its presence
    (and ``read_at``) is what marks that actor's copy read. ``actor_key`` is a
    stable identity derived from the signed-in role/scope (e.g.
    ``official:<id>``, ``team:<id>``, ``role:<role>``).
    """
    id: str
    notification_id: str
    actor_key: str
    read_at: datetime


@dataclass
class NotificationDelivery:
    """A single queued out-of-app delivery of a notification (#58).

    One row per (notification, channel). ``recipient_ref`` is who the delivery
    targets (derived from the notification's audience, e.g. ``official:<id>``,
    ``team:<id>``, ``scheduler``, ``public``) and ``destination`` is the
    per-channel address to reach them — a placeholder in this slice (#59), no
    real mailbox or device token. The delivery worker moves the row from
    ``pending`` → ``sent`` (or ``failed``) via a mock sender, tracking
    ``attempts`` and the ``last_error`` so failures can be retried until the
    attempt budget is exhausted.
    """
    id: str
    notification_id: str
    channel: NotificationChannel
    status: DeliveryStatus = DeliveryStatus.PENDING
    attempts: int = 0
    last_error: Optional[str] = None
    sent_at: Optional[datetime] = None
    recipient_ref: Optional[str] = None
    destination: Optional[str] = None


@dataclass
class ContactDestination:
    """A stored, real destination for a recipient on a channel (#60).

    Overrides the synthesized ``.invalid`` placeholder for its
    ``(recipient_ref, channel)`` when a delivery is enqueued. There is still no
    real transport in this slice — this just lets operators register where a
    notification *would* be sent (an official's email, a team contact, the
    scheduler group inbox, a push-token placeholder).
    """
    id: str
    recipient_ref: str
    channel: NotificationChannel
    destination: str
    label: Optional[str] = None


@dataclass
class SetupAuditLog:
    """Audit entry for organization/arena create/update/delete operations."""
    id: str
    action: str
    entity_type: str
    entity_id: str
    at: datetime
    actor_id: Optional[str] = None
    detail: dict = field(default_factory=dict)
