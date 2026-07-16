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
    OfficialAvailabilityStatus,
    OfficialRole,
    RescheduleStatus,
    ResultStatus,
)
from .roles import Role


@dataclass
class Program:
    """The umbrella competition entity (#233, formerly ``League``). A Program is
    operated by a facility Organization and holds Seasons."""
    id: str
    name: str
    country: str = ""
    timezone: str = "UTC"
    # Operating facility organization (#173/#233). Nullable — pre-existing
    # programs and programs created without an operator have none until one is
    # assigned.
    operator_organization_id: Optional[str] = None
    # Stable import-matching key (#174 PR E), mirroring Rink/Team/Player/
    # Official's external_ref (#93-#95): programs are matched across repeat
    # hierarchy uploads by this code (the sheet's league_code), not by name.
    external_ref: Optional[str] = None


@dataclass
class Season:
    id: str
    program_id: str
    name: str
    start_date: Optional[datetime] = None
    end_date: Optional[datetime] = None
    # Stable import-matching key (#174 PR E) — seasons are matched across
    # repeat hierarchy uploads by this code (the sheet's season_code).
    external_ref: Optional[str] = None


@dataclass
class League:
    """A competitive league/grouping between Season and Division (#233, formerly
    ``Level``). A season like "Over 55" can run several leagues ("Level 1",
    "Level 2"), each holding its own divisions. Divisions link up via
    ``league_id``."""
    id: str
    season_id: str
    name: str
    sort_order: int = 0
    # Stable import-matching key (#174 PR E) — leagues are matched across
    # repeat hierarchy uploads by this code (the sheet's level_code).
    external_ref: Optional[str] = None


@dataclass
class Division:
    id: str
    season_id: str
    name: str
    age_group: str = ""
    # Owning competitive league/grouping (#166/#233). Nullable at the model
    # level; the reparent migration (#233 Slice C1b) backfills a league for
    # every division from its season's sole league.
    league_id: Optional[str] = None
    # Stable import-matching key (#174 PR E) — divisions are matched across
    # repeat hierarchy uploads by this code (the sheet's division_code).
    external_ref: Optional[str] = None


@dataclass
class SeasonTeamRegistration:
    """A permanent league team's participation in one season (#180).

    A team belongs permanently to its league; each season it plays in is a
    separate registration that carries the season-specific division/group
    assignment. This is what makes a team "FIFA-group-like" — the same team can
    be Division A one season and Division B the next without the Team record or
    prior-season history changing. Uniqueness is ``(season_id, team_id)``:
    exactly one registration per team per season, so changing division updates
    this row rather than creating a second simultaneous participation.
    """
    id: str
    season_id: str
    team_id: str
    division_id: Optional[str] = None
    # Owning competition league (#233 Slice C1b). Nullable at the model level;
    # the reparent migration derives it from the validated same-Season chain
    # (division's league, else the season's sole league).
    league_id: Optional[str] = None
    active: bool = True


@dataclass
class SeasonVenueAccess:
    """A Season's access to a Venue for scheduling ice (#233 Slice E).

    Physical structure (facility Organization -> Venue -> Rink -> Ice Slot) and
    competition structure (Program -> Season -> League -> optional Division) are
    independent trees — this is the explicit many-to-many join between them,
    replacing the permanent, one-to-one ``Venue.league_id`` bridge. One Season
    may use several Venues; one Venue may host several Seasons, even across
    different Programs/operators. Uniqueness is one ACTIVE row per
    ``(season_id, venue_id)`` pair: revoking access deactivates the row rather
    than deleting it (history preserved), and re-granting reactivates the
    existing row rather than duplicating it.
    """
    id: str
    season_id: str
    venue_id: str
    active: bool = True


@dataclass
class Club:
    id: str
    name: str
    country: str = ""
    # Stable CSV-import matching key (#260 Slice F), mirroring every other
    # setup entity's external_ref (migrations 009-011, 020). Club was the one
    # entity still matched by name only — see migration 031's comment for why
    # that was deferred and why it's closed here. Nullable: manually-created
    # Clubs simply have no code.
    external_ref: Optional[str] = None


@dataclass
class Organization:
    """A facility owner/operator that owns venues (#166). Distinct from Club,
    which owns hockey teams — an organization like a rink company can own many
    arenas across a region. Venues link up to an organization via a nullable
    ``organization_id`` so existing venues need no backfill."""
    id: str
    name: str
    short_name: str = ""
    # Stable import-matching key (#174 PR E) — organizations are matched across
    # repeat hierarchy uploads by this code (the sheet's organization_code).
    external_ref: Optional[str] = None


@dataclass
class Venue:
    id: str
    name: str
    address: str = ""
    timezone: str = "UTC"
    # Owning facility organization (#166). Nullable — pre-existing venues and
    # venues created without an owner simply have no organization.
    organization_id: Optional[str] = None
    # Owning league (#173). Nullable — a venue belongs to at most one league;
    # legacy/unassigned inventory has none. When set, the venue's owner must
    # match the league's owner (enforced in the service layer).
    league_id: Optional[str] = None
    # Stable import-matching key (#174 PR E), mirroring Rink's external_ref —
    # venues are matched across repeat hierarchy uploads by this code (the
    # sheet's venue_code), not by name.
    external_ref: Optional[str] = None


@dataclass
class Rink:
    id: str
    venue_id: str
    name: str
    # CSV-import matching key (#95), mirroring Team/Player (#93) and Official
    # (#94): rinks are matched across repeat uploads by this code (the
    # sheet's rink_code), not by name.
    external_ref: Optional[str] = None


@dataclass
class IceSlot:
    id: str
    rink_id: str
    start_time: datetime
    end_time: datetime
    slot_type: IceSlotType = IceSlotType.GAME
    status: IceSlotStatus = IceSlotStatus.AVAILABLE


@dataclass
class RescheduleRequest:
    """A controlled request→approval→republish flow to move a PUBLISHED
    game (#29) — an uncontrolled move is the operator-only `move_game`
    drag/drop; this is the coach-initiated, opponent-and-league-gated path.

    ``requested_by_team_id`` must be one of the game's two teams; the other
    team is "the opponent" who must accept before it ever reaches league
    review. ``new_ice_slot_id``/``decision_note`` are set once the league
    decides. Reuses `move_game`/`publish_game` verbatim for the actual slot
    swap once approved, so it inherits the same one-game-per-slot and
    team-overlap guarantees rather than re-implementing them.
    """
    id: str
    game_id: str
    requested_by_team_id: str
    reason: str
    status: RescheduleStatus
    created_at: datetime
    opponent_responded_at: Optional[datetime] = None
    league_decided_at: Optional[datetime] = None
    new_ice_slot_id: Optional[str] = None
    decision_note: Optional[str] = None


@dataclass
class Official:
    """A match official who can be assigned to games (#30)."""
    id: str
    name: str
    home_club_id: Optional[str] = None   # for conflict-of-interest checks
    is_active: bool = True
    # CSV-import matching key (#94), mirroring Team/Player's external_ref
    # (#93): officials are matched across repeat uploads by this code (the
    # sheet's official_code), not by name.
    external_ref: Optional[str] = None


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
    # Dead-letter operations (#80): when the last attempt happened, when the
    # next retry is eligible, and when attempts were exhausted (parked).
    last_attempt_at: Optional[datetime] = None
    next_attempt_at: Optional[datetime] = None
    dead_lettered_at: Optional[datetime] = None


@dataclass
class ContactDestination:
    """A stored, real destination for a recipient on a channel (#60).

    Overrides the synthesized ``.invalid`` placeholder for its
    ``(recipient_ref, channel)`` when a delivery is enqueued. There is still no
    real transport in this slice — this just lets operators register where a
    notification *would* be sent (an official's email, a team contact, the
    scheduler group inbox, a push-token placeholder).

    ``active`` (#232 review 4) is a reactivatable lifecycle flag, not a
    delivery-resolution one: an inactive row is retired history that no
    longer blocks Player/Official deletion, but its stored destination is
    never erased. Mirrors ``DeviceToken.active``.
    """
    id: str
    recipient_ref: str
    channel: NotificationChannel
    destination: str
    label: Optional[str] = None
    active: bool = True


@dataclass
class OfficialAvailability:
    """A window an official declares available or unavailable to officiate (#88).

    Used to warn/block an operator from assigning an official to a game that
    overlaps an ``unavailable`` window. An official manages only their own; an
    operator can view all.
    """
    id: str
    official_id: str
    start_time: datetime
    end_time: datetime
    status: OfficialAvailabilityStatus
    note: Optional[str] = None


@dataclass
class CalendarFeedToken:
    """A revocable, bearer token for a scoped iCal subscription feed (#82).

    Possession of the raw token grants read access to one actor's calendar
    (a team, a division, an official, or a player) and nothing else. Only the
    SHA-256 ``token_hash`` is stored — the raw token lives in the subscription
    URL the user pastes into their calendar app. ``actor_type`` is
    team/division/official/player and ``actor_ref`` the corresponding id.

    ``created_by``/``revoked_by`` (#131) are the signed-in user id that
    minted/revoked it, or the literal string ``"anonymous"`` for a public
    team/division feed minted with no session (#33) — never a forgeable
    client-supplied value; the web layer always resolves these server-side.
    ``last_used_at`` is bumped on every successful ``.ics`` fetch so an
    operator can tell a live subscription from an abandoned one.
    """
    id: str
    token_hash: str
    actor_type: str
    actor_ref: str
    created_at: datetime
    revoked_at: Optional[datetime] = None
    label: Optional[str] = None
    created_by: Optional[str] = None
    last_used_at: Optional[datetime] = None
    revoked_by: Optional[str] = None


@dataclass
class NotificationPreference:
    """A recipient's opt-out for a delivery channel (#81).

    Gates whether a delivery row is created for ``(recipient_ref, channel)``
    when a notification is enqueued: an ``enabled=False`` row suppresses that
    channel. Absent rows mean the channel is on by default (existing behavior).
    The in-app feed is always delivered and is not represented here. ``digest``
    is a placeholder for a future batching preference — unused this slice.

    ``active`` (#232 review 4) is a separate, reactivatable lifecycle flag —
    NOT the opt-out itself. An inactive row no longer blocks Player/Official
    deletion, but ``enabled`` and every other stored value is untouched and
    still governs delivery exactly as before: retiring a row preserves the
    recipient's opt-out history rather than erasing it. Mirrors
    ``DeviceToken.active``.
    """
    id: str
    recipient_ref: str
    channel: NotificationChannel
    enabled: bool = True
    digest: Optional[str] = None
    active: bool = True


@dataclass
class DeviceToken:
    """A registered push device token for a recipient (#65).

    A proper registry for push destinations: a recipient can have one or more
    devices, each with its own ``token`` and ``provider`` (fcm/apns/…). Push
    delivery resolves to the recipient's first *active* token; deactivated
    tokens are kept for history but never used. Placeholder ``push-token:``
    values are rejected at registration.
    """
    id: str
    recipient_ref: str
    provider: str
    token: str
    label: Optional[str] = None
    active: bool = True


@dataclass
class InstallationState:
    """Durable one-time installation-claim marker (#174).

    The row contains only operational metadata. The setup code and the client's
    password are never persisted. A single primary-key id (``primary``) is the
    cross-process concurrency guard for first-admin creation.
    """
    id: str
    claimed_at: datetime
    claimed_by_user_id: str
    claim_method: str


@dataclass
class UserAccount:
    """A real, operator-created login for the app (#67).

    Replaces the fixed shared-password demo personas with actual accounts:
    a hashed password, a bound ``role`` + ``scope`` (the same shape used to
    bind a session — ``team_id`` for a coach, ``player_id`` for a player,
    ``official_id`` for an official), and an ``active`` flag an operator can
    flip to revoke access without deleting history. There is no self-service
    signup, password reset, or 2FA in this slice — every account is created
    by an operator (or the demo seed, acting as one).

    ``password_hash`` is opaque (see ``services/passwords.py``) and must
    never be sent to a client.
    """
    id: str
    username: str
    password_hash: str
    role: Role
    created_at: datetime
    scope: dict = field(default_factory=dict)
    active: bool = True


@dataclass
class Session:
    """A persisted login session (#74).

    Only the SHA-256 ``token_hash`` is stored — never the raw token — so a
    store dump can't be replayed. Role/scope are NOT duplicated here; they are
    resolved from the backing ``UserAccount`` on each lookup, so the session
    always reflects the account's current role/scope and active state. A
    session is live when it is un-revoked and not past ``expires_at``.
    """
    id: str
    token_hash: str
    user_id: str
    issued_at: datetime
    expires_at: datetime
    revoked_at: Optional[datetime] = None
    user_agent: Optional[str] = None


@dataclass
class GuardianLink:
    """A guardian ↔ junior-player authorization link (#26/#35).

    Binds a guardian's login (``guardian_user_id``) to a junior ``player_id``
    they may act for. ``verified`` gates real authority: an unverified link
    grants nothing (a guardian may only respond for a junior once the link is
    verified). No guardian contact/PII lives here — only the two opaque ids —
    so this record can never leak personal data into an operator view.

    ``consent_method``/``consented_at`` are the GDPR Art. 8 consent record
    (#35): how an operator obtained/confirmed guardian authorization (e.g.
    "signed_form", "verbal_confirmed", "email_reply") and when. They are
    optional at the model level — internal/seed callers may still flip
    ``verified`` without them — but the operator-facing HTTP verify route
    requires a real ``consent_method`` on every verification it performs.
    """
    id: str
    guardian_user_id: str
    player_id: str
    created_at: datetime
    verified: bool = False
    consent_method: Optional[str] = None
    consented_at: Optional[datetime] = None


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
