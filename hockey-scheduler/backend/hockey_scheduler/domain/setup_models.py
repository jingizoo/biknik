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
    MembershipStatus,
    NotificationAudience,
    NotificationChannel,
    NotificationKind,
    OfficialAssignmentStatus,
    OfficialAvailabilityStatus,
    OfficialRole,
    PolicyScopeType,
    Position,
    RescheduleStatus,
    ResultStatus,
    SeasonStatus,
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
    # Lifecycle state (#159). Defaults to ACTIVE; archiving flips this to
    # ARCHIVED and stamps ``archived_at``, making the Season a read-only
    # historical record (no new registrations/venue access/divisions/games/
    # roll-forward target) until an authorized, reasoned reopen clears it.
    status: SeasonStatus = SeasonStatus.ACTIVE
    archived_at: Optional[datetime] = None


@dataclass
class League:
    """A permanent competitive league — the hard competition boundary (#283).

    A League is a permanent child of a **Program** (``program_id``), not of a
    Season: ``Platinum``/``Bronze`` exist once per Program and persist across
    Seasons, rather than being recreated each Season. A League's participation
    in a particular Season is expressed by a :class:`LeagueSeason` row, and a
    Team belongs permanently to exactly one League (``Team.league_id``).
    """
    id: str
    program_id: str
    name: str
    sort_order: int = 0
    # Stable import-matching key (#174 PR E) — leagues are matched across
    # repeat hierarchy uploads by this code (the sheet's level_code).
    external_ref: Optional[str] = None


@dataclass
class LeagueSeason:
    """A permanent :class:`League`'s participation in one :class:`Season` (#283).

    The season overlay on the permanent Program → League → Team structure:
    Divisions and team registrations for a Season hang off a ``LeagueSeason``,
    not off the permanent League or Season directly. Uniqueness is
    ``(league_id, season_id)`` — at most one row per League per Season. The
    invariant ``league.program_id == season.program_id`` is enforced in the
    service layer (a League and Season joined here must share a Program).
    """
    id: str
    league_id: str
    season_id: str


@dataclass
class Division:
    """An optional grouping inside a League for one Season (#283).

    Divisions are season-specific (``North``/``South`` may differ in team count
    and placement each Season), so a Division belongs to a
    :class:`LeagueSeason` (``league_season_id``), not permanently to a Team or
    League. Prior-Season Divisions and their history are never rewritten when a
    Team moves.
    """
    id: str
    league_season_id: str
    name: str
    age_group: str = ""
    # Stable import-matching key (#174 PR E) — divisions are matched across
    # repeat hierarchy uploads by this code (the sheet's division_code).
    external_ref: Optional[str] = None


@dataclass
class SeasonTeamRegistration:
    """A permanent League team's participation in one Season (#180/#283).

    A Team belongs permanently to its League (``Team.league_id``); each Season
    it plays in is a separate registration against a :class:`LeagueSeason`,
    carrying the season-specific optional Division assignment. The same Team can
    be Division A one Season and Division B the next without the Team record or
    prior-Season history changing. Uniqueness is ``(team_id, league_season_id)``
    — exactly one registration per Team per LeagueSeason, so changing Division
    updates this row rather than creating a second simultaneous participation.
    The invariant ``team.league_id == league_season.league_id`` (a Team may only
    register in its own League) is enforced in the service layer.
    """
    id: str
    league_season_id: str
    team_id: str
    division_id: Optional[str] = None
    active: bool = True


@dataclass
class SeasonCopyForwardCommit:
    """Idempotency ledger for new-Season copy-forward commits (#159 review
    round 2: double-submit/retry blocker). One row per SUCCESSFUL
    ``commit_new_season_copy_forward`` call, keyed by the exact
    ``copy_forward_fingerprint`` that produced it -- migration 053's
    ``ux_season_copy_forward_commits_fingerprint`` UNIQUE index is the
    atomic backstop: a second commit (sequential replay OR a genuine
    concurrent race) that reuses the same fingerprint loses this row's
    INSERT, and the service treats that as an idempotent request for the
    SAME result this row already records -- never a second Season (mirrors
    ``commit_ice_availability``'s own idempotent-duplicate handling, #158,
    adapted for a write target -- a brand-new Season -- that has no natural
    dedup key of its own).

    ``registration_ids`` is the EXACT set of SeasonTeamRegistration rows
    this commit created, snapshotted so a later idempotent replay returns
    precisely what the original commit produced -- not whatever a season's
    registrations happen to look like by the time the replay lands (an
    operator could legitimately register more teams into the new Season in
    between). ``rolled_forward``/``skipped`` are copied from the same
    original call for the identical reason: ``skipped`` in particular has
    no row of its own to re-derive from current state.

    ``response_snapshot`` (#159 review round 3, owner P1) is the
    FULLY-RESOLVED response payload itself -- ``{"season": {...},
    "registrations": [{...}, ...], "totals": {...}}``, every value already
    JSON-safe -- captured in the SAME transaction as the Season and
    registrations it describes. ``registration_ids`` above still names
    which rows were created (kept for that record and for any external
    reader that wants the raw ids), but it is no longer how a replay is
    built: re-deriving the response by re-fetching those ids' CURRENT rows
    was the root cause of a whole class of bugs -- the source Team getting
    unregistered, the target registration getting deleted, or the target
    Season itself getting deleted all silently changed what a replay of an
    old, already-successful commit returned, or broke it outright. A
    replay now deserializes ``response_snapshot`` directly and touches
    neither the ``seasons`` nor ``season_team_registrations`` tables at
    all, so it is immune to every later mutation of either. Each
    registration entry ALSO freezes its ``season_id``/``league_id`` (#159
    review round 4, owner P1 finding 2), resolved via its LeagueSeason
    ONCE at commit time -- see ``SetupService._copy_forward_result_from_
    ledger_row``, which stops re-deriving them through a live LeagueSeason
    lookup on every replay.

    ``request_identity`` (#159 review round 4, owner P1 finding 1) is the
    RAW caller-supplied identity this commit was actually validated
    against -- ``{"actor_id":, "program_id":, "name":, "start_date":,
    "end_date":, "source_season_id":, "selections":}`` -- captured
    verbatim, with no store lookup and no normalization, in the SAME
    transaction as ``response_snapshot``. Before this round, the early-
    replay shortcut trusted ANY caller-supplied ``copy_forward_fingerprint``
    that happened to match this row, regardless of who was asking or what
    they actually submitted: a second actor, submitting an entirely
    different Program/Season/selections/name, who reused another actor's
    already-committed fingerprint received that OTHER actor's full
    response verbatim -- the submitted request was never checked against
    what the fingerprint was actually bound to. A replay must now match
    this stored identity, canonicalized the same way, before the
    ``response_snapshot`` is ever returned; see
    ``SetupService._copy_forward_request_identity_matches``.

    Stored as the CANONICAL JSON STRING itself (#159 review round 5, owner
    P1 finding 1) -- ``SetupService._copy_forward_canonical_json`` applied
    to the raw dict above, the SAME canonicalization ``copy_forward_
    fingerprint`` is already hashed from -- rather than the raw, still-
    mutable dict a naive reading of "captured verbatim" two paragraphs up
    might suggest. A plain Python ``str`` is immutable by construction, so
    once this field is set it can never be affected by a later mutation of
    whatever mutable ``selections`` list/dicts the ORIGINAL caller passed
    to ``commit_new_season_copy_forward`` and may still hold a reference to
    -- closing a real bug where ``InMemoryStore`` retained that dict BY
    REFERENCE: mutating the caller's own list after commit silently
    changed what the "frozen" ledger identity appeared to be, so replaying
    the original fingerprint with the mutated list wrongly matched. SQLite/
    PostgreSQL never had this hazard (their JSON write already copied the
    value); this collapses Memory onto the exact same guarantee by
    construction, with no reliance on any store remembering to copy on
    every read/write boundary. See ``SetupService._copy_forward_request_
    identity`` and ``_copy_forward_request_identity_matches``.
    """
    id: str
    copy_forward_fingerprint: str
    season_id: str
    actor_id: Optional[str] = None
    registration_ids: list = field(default_factory=list)
    rolled_forward: int = 0
    skipped: int = 0
    committed_at: Optional[datetime] = None
    response_snapshot: dict = field(default_factory=dict)
    request_identity: str = ""


@dataclass
class AgeEligibilityRule:
    """A VERSIONED age-tier/cutoff rule for one :class:`LeagueSeason` (#273).

    The structured replacement for the free-text ``Division.age_group``
    convention: a cutoff (month/day in the Season's start year) plus tier
    definitions (``[{"code": "U10", "max_age": 10}, ...]``; ``max_age`` None =
    open tier). Rows are immutable — updating the rule appends the next
    ``version`` for the same ``league_season_id`` — so an eligibility answer
    can always name (and reproduce under) the exact rule version it used,
    matching the owner's versioned-policy ruling on epic #205.

    ``enforcement`` is the #273 warn-first switch: ``"warn"`` (default;
    eligibility is surfaced, nothing is blocked) or ``"block"`` (consumers may
    hard-refuse). No consumer hard-blocks in this slice; the mode is stored
    and reported so the later wiring is a policy flip, not a schema change.
    """
    id: str
    league_season_id: str
    version: int
    cutoff_month: int
    cutoff_day: int
    tiers: list = field(default_factory=list)
    enforcement: str = "warn"
    created_at: Optional[datetime] = None
    actor_id: Optional[str] = None


@dataclass
class TeamLeagueMigrationDecision:
    """An operator-supplied permanent-League assignment for a Team whose
    historical registrations span more than one League (#283 migration 035).

    Migration tooling only — never read by normal application flow. The
    competition-hierarchy-reset migration computes each Team's candidate
    Leagues from its historical registrations; a Team resolving to a single
    League migrates automatically, but a Team with rows across multiple Leagues
    is *ambiguous* and halts the migration with an exception report until an
    operator records the intended permanent ``league_id`` here. Entering the
    missing decisions and re-running the migration is idempotent: the migration
    only records once its pre-check passes, so it safely re-runs each startup
    until every ambiguous Team is resolved, then applies.
    """
    id: str
    team_id: str
    league_id: str
    note: str = ""


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
class SeasonRosterMembership:
    """One athlete's participation stint for one Team in one Season (#205
    Slice A).

    Hangs off the permanent Team + LeagueSeason spine: ``league_season_id``
    names the Team's own League's participation in the Season (never a
    re-created League), and ``team_id`` must be a Team of that League with an
    active :class:`SeasonTeamRegistration` there — both enforced in the
    service layer, mirroring registration rule 7. ``season_id`` is carried
    denormalized (service-enforced equal to the LeagueSeason's ``season_id``)
    so the ONE database-expressible uniqueness rule — at most one
    ``active`` membership per (player, Season), migration 059's partial
    unique index — needs no join. ``affiliate`` rows are the governed call-up
    exception and sit outside that rule.

    ``player_id`` keys the membership to the existing Player identity: #205's
    durable-athlete substrate arrives with #273, which preserves Player ids,
    so this column is the forward-compatible identity key today and the
    compatibility map's join column for backfilled rows (``srm_legacy_*``
    ids, migration 059).

    ``position`` / ``jersey_number`` / ``shoots`` are SEASON-SCOPED (#269
    becomes (LeagueSeason, Team)-scoped for memberships): they describe this
    stint, not the permanent Player row, which keeps its own fields until the
    consumer cutover retires them. ``effective_from``/``effective_to`` bound
    the stint; a backfilled row has ``effective_from`` NULL — first-class
    "predates membership tracking", never a fabricated date. A row whose
    status ``is_terminal`` (released/transferred) is immutable history; a
    later stint for the same player is a NEW row, so rows accumulate as the
    transfer/release history the epic requires. Fine-grained changes append
    :class:`SeasonRosterMembershipEvent` rows via the service layer.
    """
    id: str
    player_id: str
    league_season_id: str
    season_id: str
    team_id: str
    status: MembershipStatus = MembershipStatus.ACTIVE
    position: Optional[Position] = None
    jersey_number: Optional[int] = None
    shoots: Optional[str] = None
    effective_from: Optional[datetime] = None
    effective_to: Optional[datetime] = None


@dataclass
class SeasonRosterMembershipEvent:
    """Append-only history entry for one :class:`SeasonRosterMembership`
    (#205 Slice A) — the immutable record of Team/jersey/position/status
    changes the epic requires, kept per-membership (queryable by workflow
    slices) alongside the global ``SetupAuditLog`` trail.

    Append-only BY CONSTRUCTION: no store exposes an update or delete for
    these rows, and the service layer writes one on every membership
    mutation it performs. ``detail`` carries the machine-readable
    field-by-field before/after values; ``reason`` is the operator-supplied
    justification (required by the future transfer/release workflows, opt-in
    here). Backfilled memberships (migration 059) start with an EMPTY
    history rather than a fabricated "created" event, because the migration
    has no honest timestamp or actor for one.

    ``seq`` (#205 review round 1 finding 4) is a real monotonic integer
    ordering key, minted alongside ``id`` from the same counter
    (``store.next_seq("srme")`` — ``id`` is ``f"srme_{seq}"``). ``at`` alone
    cannot order history: an injected clock (tests) or a fast operator can
    produce several events with an IDENTICAL timestamp, and ``id`` cannot
    stand in either — it is TEXT, so a lexical ``ORDER BY id`` puts
    ``srme_10`` before ``srme_2``. ``events_for_membership`` orders by
    ``seq`` so Memory/SQLite/PostgreSQL agree on true creation order past 9
    events, durably (persisted, survives a restart).
    """
    id: str
    membership_id: str
    action: str
    at: datetime
    actor_id: Optional[str] = None
    reason: Optional[str] = None
    detail: dict = field(default_factory=dict)
    seq: int = 0


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
class SchedulingPolicy:
    """Operational turnover/curfew policy for game placement (#277 Slice B).

    One row per ``(scope_type, scope_id)`` — a Program, Season, or Rink. A
    placement resolves its EFFECTIVE policy field by field with Rink
    overriding Season overriding Program; a ``None`` field always means
    "inherit from the next scope up", and a field unset at every scope
    falls back to the no-op default (no buffer, no minimum, no curfew), so
    installs that never configure a policy keep today's behavior exactly.

    An ``IceSlot``'s stored ``[start_time, end_time]`` remains the PLAYABLE
    span; ``warmup_minutes`` (pre-game ice reservation) and
    ``resurfacing_minutes`` (post-game resurfacing/bench turnover) extend it
    to the RESERVED facility span at read/check time only — imported
    contracted-ice rows are never rewritten (#277: no silent time shifts).
    ``curfew_local`` is a "HH:MM" wall-clock end-by bound evaluated in the
    slot's venue timezone (Program timezone fallback).
    """
    id: str
    scope_type: PolicyScopeType
    scope_id: str
    warmup_minutes: Optional[int] = None
    resurfacing_minutes: Optional[int] = None
    min_playable_minutes: Optional[int] = None
    curfew_local: Optional[str] = None


@dataclass
class ScheduleScenario:
    """An immutable, named scheduler generation (#206/#378).

    ``proposal`` is the exact generator response the operator reviews.  It is
    intentionally opaque here: the scheduler owns that payload's fields and
    ordering, while this record only preserves it field-for-field and in order.
    The canonical ``generation_snapshot`` describes every authoritative material
    input read for that generation and ``input_fingerprint`` binds the snapshot
    to the later atomic commit.

    There is deliberately no mutable lifecycle/status field.  Committing a
    scenario creates separate draft Games; publishing those Games remains the
    existing distinct operation.  A scenario itself is historical evidence and
    is never rewritten to reflect either action.
    """
    id: str
    name: str
    program_id: str
    season_id: str
    league_id: str
    league_season_id: str
    division_id: Optional[str]
    planner_version: str
    input_fingerprint: str
    proposal_fingerprint: str
    request_input: dict
    proposal: dict
    generation_snapshot: dict
    created_at: datetime
    created_by: Optional[str] = None


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
class ActiveContext:
    """A user's currently-selected Program + Season (+ League) working context.

    Operators run several Programs and Seasons; this is the one they are
    *looking at* — persisted so future slices' screens/deep links can restore it.
    It is a VIEW preference, NOT an authorization grant: switching context never
    widens what a role/scope may do (roles are enforced independently, #211).
    ``id`` is the owning ``user_id`` (one context per user). ``season_id`` may be
    null (a Program-only context for new/empty Programs). A saved selection is
    filtered through the caller's authorized scope on every resolve: an ARCHIVED
    Season is honored as a read-only HISTORICAL context (never silently swapped
    for an unrelated active one); a DELETED or no-longer-authorized Program/Season
    is ignored in favor of a deterministic authorized fallback. The stored row is
    NOT rewritten on resolve, so if authorization is later restored the saved
    choice resolves again.

    ``league_id`` (#345) is the optional third context axis, promoted into the
    persistent bar alongside Program/Season. It is null for every pre-#345 row
    (migration 049 backfills nothing) and stays null for the two states that
    remain first-class: Program-only, and Season-without-League. When BOTH a
    Season and a League are selected they must name an EXISTING ``LeagueSeason``
    binding — the context never creates one implicitly, and never stands in for
    the Program/Season-shared invariant enforced elsewhere.

    ``generation`` (#159 review findings 2+5, migration 051) is a MONOTONIC
    counter, atomically incremented by ``ContextService.set``/``set_with_
    league`` on EVERY commit for this user — independent of the wall clock,
    unlike ``updated_at``, which two commits landing in the same tick can
    share. It backfills to 0 for every pre-migration row. Consulted by
    ``services/context_epoch.py`` alongside the EFFECTIVE resolved tuple so
    an A -> B -> A round trip moves the epoch even when the tuple returns to
    where it started; see that module for why the tuple alone is not enough.

    **Field order is deliberately append-only.** ``league_id`` and
    ``generation`` are declared LAST, after ``updated_at``, rather than in
    hierarchy order after ``season_id``: callers already construct this
    dataclass with four POSITIONAL arguments (``ActiveContext("u", "p", "s",
    ts)``), so inserting a field ahead of ``updated_at`` would silently
    reinterpret an existing caller's timestamp as a League id or a
    generation. Read the logical hierarchy from the docstring, not from the
    field order.
    """
    id: str
    program_id: Optional[str]
    season_id: Optional[str]
    updated_at: datetime
    league_id: Optional[str] = None
    generation: int = 0


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


@dataclass
class FactoryResetEvent:
    """Append-only record of a production factory-reset attempt (#256).

    Deliberately outside the application audit trail (``SetupAuditLog``):
    ``clear_all_data()`` wipes every ordinary table, including
    ``setup_audit_logs`` itself, so this is the only record that survives a
    completed reset. Never contains secrets or player PII — only operational
    metadata (actor id, environment, pre-reset row counts by table, and the
    outcome). ``environment`` is the deployment identifier (``APP_MODE`` plus
    an optional operator-supplied label), not a secret.
    """
    id: str
    actor_id: str
    environment: str
    started_at: datetime
    result: str  # "success" | "failed"
    pre_reset_counts: dict = field(default_factory=dict)
    completed_at: Optional[datetime] = None
    failure_reason: Optional[str] = None


@dataclass
class FactoryResetChallenge:
    """The single outstanding factory-reset preview challenge (#256 review
    blocker 5) — durable in the store rather than held in one Python
    process's memory, so a preview() and a later execute() reaching
    different server instances/processes still see the same challenge. Uses
    a fixed singleton ``id`` so creating a new challenge always REPLACES any
    prior one: only the latest preview's challenge is ever valid, and a
    fresh preview implicitly invalidates an older, unused one.
    """
    id: str
    token_hash: str
    actor_id: str
    counts: dict
    expires_at: datetime
    created_at: datetime


@dataclass
class FactoryResetLock:
    """Installation-wide mutual-exclusion marker for an in-progress factory
    reset (#256 review round 1 blocker 5, hardened in round 2 blocker 3). A
    fixed singleton ``id`` makes acquisition a single unique-constraint
    insert — the same cross-process race-safe pattern ``InstallationState``
    already uses for the first-admin claim — so "one reset in progress at a
    time" holds across every process sharing this store, not only within
    one Python object.

    ``token`` is a random, unguessable value generated at acquisition and
    held only by the acquirer — release is compare-and-delete on this
    token (never an unconditional delete of "whatever singleton row is
    there"), so a delayed release from a process that no longer holds the
    current lock can never delete a different process's active one.

    ``expires_at`` is a lease: if a process crashes after acquiring the
    lock and never releases it, the lock would otherwise disable factory
    reset permanently. A caller may reclaim an expired lock without manual
    database intervention (see ``release_stale_factory_reset_lock``).
    """
    id: str
    actor_id: str
    token: str
    acquired_at: datetime
    expires_at: datetime


@dataclass
class SubtreeDeletionChallenge:
    """One actor's outstanding destructive-subtree preview challenge (#429).

    Only a SHA-256 token digest is durable.  The row binds that token to the
    authenticated actor, selected root, exact graph fingerprint and typed
    confirmation name.  ``id`` is the actor id, so a newer preview atomically
    supersedes that actor's older one without disturbing another operator's
    challenge.
    """

    id: str
    token_hash: str
    actor_id: str
    fingerprint: str
    root_type: str
    root_id: str
    confirmation_name: str
    expires_at: datetime
    created_at: datetime
