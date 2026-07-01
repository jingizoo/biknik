"""Organization & arena domain models (League + Arena setup slice).

These describe the scheduling universe a league/arena operator builds before
games exist: league → season → division, club → team, and venue → rink →
ice slot. Plain data holders; all rules live in the service layer.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from .enums import (
    IceSlotStatus,
    IceSlotType,
    OfficialAssignmentStatus,
    OfficialRole,
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
class SetupAuditLog:
    """Audit entry for organization/arena create/update/delete operations."""
    id: str
    action: str
    entity_type: str
    entity_id: str
    at: datetime
    actor_id: Optional[str] = None
    detail: dict = field(default_factory=dict)
