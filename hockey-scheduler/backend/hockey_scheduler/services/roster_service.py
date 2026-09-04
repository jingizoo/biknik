"""Roster status engine and substitute workflow.

This is the heart of the slice. It is pure business logic over the store: it
never performs network/disk I/O and it never calls ``datetime.now()`` itself
(a clock is injected) so every rule is deterministic and unit-testable.

Every state-changing method appends an :class:`AuditLog` entry and, where the
use case calls for it, emits a :class:`NotificationEvent`.
"""

import functools
from datetime import datetime, timedelta, timezone
from typing import Callable, Dict, List, NamedTuple, Optional, Tuple

from ..domain import (
    AuditAction,
    AuditLog,
    AvailabilityStatus,
    NotificationAudience,
    NotificationKind,
    Game,
    GameAvailability,
    GameRosterEntry,
    GameStatus,
    IceSlotStatus,
    MembershipStatus,
    NotificationEvent,
    Player,
    Position,
    RosterEntryStatus,
    RosterRole,
    RosterStatus,
    SeasonRosterMembership,
    SelectionSource,
    SlotStatus,
    SlotSummary,
    SlotType,
    SubstituteEnrollment,
    SubstituteStatus,
)
from ..domain.enums import NotificationType
from ..domain.errors import (
    AlreadySelectedError,
    GameCancelledError,
    InvalidTransitionError,
    IntegrityConflictError,
    ConcurrencyConflictError,
    NotAuthorizedError,
    NotEligibleError,
    NotEnrolledError,
    NotFoundError,
    RosterLockedError,
    SlotAlreadyFilledError,
    ValidationError,
)
from ..store import InMemoryStore
from .membership_spine import (
    DENORMALIZED_SEASON_MISMATCH,
    LEAGUE_SEASON_MISSING,
    MEMBERSHIP_OTHER_LEAGUE_SEASON,
    MEMBERSHIP_OTHER_SIDE,
    MEMBERSHIP_OTHER_TEAM,
    NO_ELIGIBLE_MEMBERSHIP,
    PLAYER_INACTIVE,
    PLAYER_MISSING,
    PRIOR_SEAT_UNATTRIBUTED,
    missing_or_unequal,
    reason_rank,
    side_spine_break,
    status_ineligible_reason,
)
from .notifier import push as _push_notification
from . import season_guard
from .season_guard import GAME_LEAGUE_SEASON_MISMATCH


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ``None`` is a meaningful observation (no active enrollment).  This distinct
# sentinel lets a multi-section Player Home projection pass that observation
# through every predicate without a helper silently performing a newer read.
_ACTIVE_ENROLLMENT_NOT_OBSERVED = object()


# ======================================================================
# THE STATES A PLAYER-DRIVEN TRANSITION *INTO* CONFIRMED CAN START FROM
# ======================================================================
# The source states from which ``set_availability`` may move a roster row
# INTO ``CONFIRMED`` on the player's own say-so, and therefore the exact
# set of states ``_authorize_seated_side`` governs.
#
# WHY THIS IS AN EXPLICIT LITERAL AND NOT ``entry.status.occupies_slot``
# (owner ruling, PR #427, 2026-08-22): "Use an explicit transition-state
# set rather than entry.status.occupies_slot; occupancy is not
# authorization, and future enum changes must not silently widen this
# gate."
#
#   * OCCUPANCY IS NOT AUTHORIZATION. ``occupies_slot`` answers "is this
#     row holding a seat?" — a COUNTING question, owned by the slot
#     arithmetic. This set answers "may this player move this row into
#     CONFIRMED?" — an AUTHORIZATION question. They coincide today by
#     accident, not by definition, and reading one to decide the other
#     makes every future change to the counting rule a silent change to
#     the security rule.
#   * NO SILENT WIDENING. Deriving the gate from the enum means adding a
#     value to ``occupies_slot`` also enrols it here, unreviewed. Spelled
#     out, a new state is refused until somebody puts it in this literal
#     on purpose. ``PlayerConfirmSourceStatesArePartitionedDeliberately``
#     in tests/test_roster_attribution_durability.py fails the moment a
#     new occupying state appears that nobody has classified.
#
# WHAT IS *DELIBERATELY OUT*, and why:
#
#   CONFIRMED — owner ruling, PR #427, 2026-08-22: "Preserve the existing
#     behavior for an already-CONFIRMED row. Reaffirming availability must
#     remain idempotent for this slice; do not introduce the new live-side
#     refusal there. This ruling concerns transitions INTO CONFIRMED, not
#     a row already in that state." A row already in CONFIRMED is not
#     TRANSITIONING into it, so it is not this gate's business; it falls
#     through to the availability sync below and re-writes the status it
#     already has.
#   UNAVAILABLE — the BACKED-OUT row. It is authorized by the identical
#     ``_authorize_seated_side`` call, but through the ``reconfirming``
#     branch, which additionally re-takes the slot and so must also run
#     ``_require_open_slot``. Two different transitions, one gate.
#   REMOVED — refused outright and earlier: a coach removal is not
#     self-reversible.
#   OFFERED — NOT REACHABLE, and excluded for that reason (owner ruling:
#     "Include OFFERED only if that state is genuinely reachable or
#     supported here"). The evidence, at head 5b26758:
#       (1) ``RosterEntryStatus.OFFERED`` occurs exactly ONCE in the whole
#           repository — in ``occupies_slot``'s own set (domain/enums.py).
#           Nothing reads it and nothing compares against it.
#       (2) A roster row's ``status`` is written at exactly five sites,
#           all in this module: the two ``select_roster`` sites (SELECTED),
#           the two ``_add_to_roster_entry`` sites (ACCEPTED), and
#           ``_set_entry_status``. ``_set_entry_status``'s callers pass
#           CONFIRMED (``set_availability``, ``set_roster_entry_status``)
#           or UNAVAILABLE/REMOVED (``_back_out_entry``) — except
#           ``set_roster_entry_status``'s trailing ``else``, which passes
#           its argument through.
#       (3) That ``else`` is the only theoretical door, and it is not
#           connected to anything: its sole caller
#           ``ApiService.set_roster_status`` has NO HTTP route (there is
#           no ``PATCH /roster/{playerId}/status`` handler in web/server.py
#           and no frontend caller — owner ruling, PR #427), no product
#           caller, and one test caller that passes ``"bad"`` to assert the
#           parse rejects it.
#       (4) No importer, seed, migration, fixture or backup/restore path
#           writes a roster status at all: ``full_demo`` seeds through
#           ``select_roster``/``set_availability``, and every raw-SQL
#           INSERT into ``game_roster_entries`` (all of them in tests)
#           writes ``'selected'`` or NULL. The column is untyped TEXT, so
#           a hand-written UPDATE could store ``'offered'`` — that is not
#           a product path, and guessing an authorization answer for a
#           state no code can produce is exactly the speculative widening
#           the ruling forbids.
#     If OFFERED ever becomes writable, the partition test named above
#     fails and forces this decision to be made explicitly.
_PLAYER_CONFIRM_SOURCE_STATES = frozenset({
    RosterEntryStatus.SELECTED,
    RosterEntryStatus.ACCEPTED,
})


# ==========================================================================
# COACH-TEAM AUTHORIZATION (#205, owner comments 5373064375 + 5391127041)
# ==========================================================================
# The two machine-readable reasons every Coach-authorization refusal in this
# module carries. Both are raised by ONE function
# (:meth:`RosterService._require_authorized_team`), so a change to either
# message or reason breaks every surface's tests together.
#
# ``attribution_missing`` is DELIBERATELY THE EXISTING STRING already minted
# by :meth:`RosterService._refuse_unattributed` for the player-driven
# confirm gate — the ruling asks for "structured ``attribution_missing``",
# and a row that cannot name its side is the same fact whichever gate
# observes it. The two raise different ERROR CODES on purpose, because they
# answer different questions: the confirm gate's is ``not_eligible`` (about
# the PLAYER), this one's is ``forbidden`` (about the COACH, who is refused
# even though the player may be perfectly eligible).
ATTRIBUTION_MISSING = "attribution_missing"
TEAM_SCOPE_VIOLATION = "team_scope_violation"

# THE TWO HUMAN MESSAGES `attribution_missing` CAN CARRY, and why there are
# two of them but only ONE machine-readable reason (PR #427 review, finding
# F-5).
#
# A NULL comparand fails closed for a Coach either way — that posture is not
# up for negotiation here and neither message weakens it. What differs is
# WHAT THE NULL MEANS, and the single message this gate used to raise
# described only one of the two cases:
#
#   DURABLE comparand (withdraw / decline / remove): the row was FOUND and
#   cannot name its side, which happens only for a row written before
#   migrations 060/061, neither of which backfills. "Predates durable team
#   attribution" is then literally true and tells an operator exactly what to
#   go fix.
#
#   LIVE comparand (select_roster, and set_availability): NOTHING WAS FOUND
#   TO ATTRIBUTE. The player id may name nobody; the player may exist with no
#   membership resolving onto either side of this game. Neither is a
#   legacy-attribution problem, and the old wording sent an operator to
#   repair a migration artefact that does not exist — in set_availability's
#   case, describing a "roster row" that does not exist at all.
#
# WHY THE CASES STAY MERGED BEHIND ONE REASON AND ONE FIXED STRING — the
# existence-disclosure tension, decided rather than dodged. Splitting the
# LIVE case further, into `not_found` for an unknown player id versus
# "exists but not yours", would answer "does this player id exist?" for any
# coach who can post to these routes: a player-id enumeration oracle handed
# to precisely the caller this gate has just decided is not entitled to the
# answer. The same applies inside set_availability, where a distinct string
# for the no-row branch would tell an unauthorized coach whether the player
# holds a roster row in this game. So: ONE reason (`attribution_missing`),
# and a LIVE message that is deliberately INVARIANT — it interpolates no
# subject noun, because letting it say "player" in one branch and "roster
# row" in another would rebuild the same oracle out of prose. What the
# refusal now describes is the DECISION ("your team could not be confirmed
# as this player's"), never the cause, and it is identical for every input
# that reaches it.
_ATTRIBUTION_MISSING_DURABLE = (
    "This {what} predates durable team attribution, so the team that owns it "
    "can't be identified — ask a league admin.")
_ATTRIBUTION_MISSING_LIVE = (
    "This player can't be confirmed as one of your team's players for this "
    "game, so a coach can't act on them here — ask a league admin.")


def _transactional(fn):
    """Wrap a mutating service method in a single store transaction."""
    @functools.wraps(fn)
    def wrapper(self, *args, **kwargs):
        with self.store.transaction():
            return fn(self, *args, **kwargs)
    return wrapper


class _CancelGameRaced(Exception):
    """Internal retry signal when cancellation's pre-lock ice plan drifted."""


class GameMembershipContext(NamedTuple):
    """THE single resolved membership context for one ``(game, player)``
    pair (#205 review blocker 2, owner comment 5368386042).

    Every substitute decision — which SIDE the player counts against, which
    POSITION/slot they occupy, whether a private read may show them, whether
    a transition may commit — is taken from ONE of these, resolved ONCE by
    :meth:`RosterService.resolve_membership_context`. Before it existed, team
    and position were re-read INDEPENDENTLY (``team_for_game`` and
    ``position_for_game`` each ran their own ``resolve_membership``), so two
    reads of "the same" eligibility could disagree, and each carried its own
    silent fallback to the permanent ``Player`` row.

    A context EXISTS only when the whole spine held, so holding one is proof
    of eligibility; ``None`` is the only other answer, and it is fail-closed.

    ``membership`` is ``None`` for — and ONLY for — a game with no
    LeagueSeason binding (exhibitions, unbound legacy rows), where the
    permanent ``player.team_id``/``player.position`` pointers are the only
    source there has ever been. ``league_season``/``season``/``team``/
    ``registration`` are the real rows the spine resolved to, carried so a
    caller can inspect the participation it was granted on rather than
    re-reading (and possibly re-deciding) it.
    """
    game_id: str
    player: Player
    team_id: str
    position: Position
    membership: Optional[SeasonRosterMembership]
    league_season: object = None
    season: object = None
    team: object = None
    registration: object = None

    @property
    def bound(self) -> bool:
        """Whether this context came from a real seasonal membership rather
        than an unbound game's permanent pointer."""
        return self.membership is not None

    @property
    def slot_type(self) -> SlotType:
        return self.position.slot_type


class SubstituteTargetContext(NamedTuple):
    """One validated substitute relationship.

    ``source`` is the player's live season membership and
    ``target_team_id`` is the game side they volunteered to help.  Keeping
    those values separate is essential for cross-team substitution: the
    source supplies eligibility and position, while the target owns the
    enrollment, coach offer/seating actions, slot and eventual roster seat.
    The player or verified guardian owns accept/decline; a Coach can seat the
    player only through the explicit audited override.
    """
    source: GameMembershipContext
    target_team_id: str
    target_team: object
    target_registration: object

    @property
    def cross_team(self) -> bool:
        return self.source.team_id != self.target_team_id


class SubstituteGameChoice(NamedTuple):
    """A Player Home availability choice for one concrete game side."""
    game: Game
    # ``target`` is the live eligibility proof for a fresh choice.  An
    # already-persisted opt-in remains the player's own withdrawable record
    # even if that proof later disappears, so stale rows deliberately carry
    # ``None`` here and are rendered from their durable target/slot snapshot.
    target: Optional[SubstituteTargetContext]
    enrollment: Optional[SubstituteEnrollment]


class SubstituteOfferChoice(NamedTuple):
    """One player-visible offer and the enrollment observed with it.

    Keeping these two records together prevents a terminal response racing a
    Player Home read from turning the second enrollment lookup into ``None``.
    The command boundary still revalidates current state before every write.
    """
    game: Game
    enrollment: SubstituteEnrollment


class LineupRow(NamedTuple):
    """ONE player on ONE side's lineup screen, with the authority that put
    them there (#427 blocker, owner comments 5390696775 / 5394947899).

    Produced only by :meth:`RosterService.lineup_population`, which is the
    place to read for what each field means and why. In short:

    ``source``  which of the four populations this row came from —
                ``"roster"`` (durable ``GameRosterEntry.attribution``),
                ``"substitute"`` (durable ``SubstituteEnrollment.team_id``),
                or ``"candidate"`` (live game-season membership). The
                unbound-exhibition branch reuses the same three labels over
                permanent-pointer data.

    ``position``/``jersey_number``  ALREADY RESOLVED to the authority the
                ruling assigns to this row's source. A caller must render
                these and must not re-read ``player.position`` /
                ``player.jersey_number`` — doing so is the defect. ``player``
                is carried for identity (id, name, ``is_active``) only.

    ``eligible``  whether this side has a LIVE membership context for this
                player right now. ``False`` is reachable on a durable row
                whose occupant's participation has ended: the row stays
                visible so the owning Coach can clean it up, but it is not
                seatable and the service would refuse an add/seat on it, so
                a UI that offers one is offering a rejected action. Distinct
                from ``source``, which says how the row got here, not
                whether it may still be acted on.
    """
    player: Player
    source: str
    position: Position
    jersey_number: Optional[int]
    entry: Optional[GameRosterEntry]
    enrollment: Optional[SubstituteEnrollment]
    context: Optional[GameMembershipContext]
    eligible: bool


class RosterService:
    def __init__(
        self, store: InMemoryStore, clock: Callable[[], datetime] = _utcnow,
        *, cross_team_response_window: timedelta = timedelta(minutes=30),
    ):
        if (not isinstance(cross_team_response_window, timedelta)
                or cross_team_response_window <= timedelta(0)):
            raise ValueError("cross_team_response_window must be positive")
        self.store = store
        self.clock = clock
        # Trusted process configuration, never a request/body value.  A future
        # League-policy slice may supply a League-specific duration here; the
        # production default is deliberately explicit and testable now.
        self.cross_team_response_window = cross_team_response_window

    # ====================================================================
    # internal helpers
    # ====================================================================
    def _require_game(self, game_id: str) -> Game:
        game = self.store.get_game(game_id)
        if game is None:
            raise NotFoundError(f"Game {game_id} not found.")
        return game

    def _refetch_under_season_lock(self, game_id: str) -> Game:
        """Re-fetch a Game AFTER ``_guard_active_season`` has taken its Season
        row lock (#201): the pre-lock ``_require_game`` read is only a locator.
        A concurrent ``cancel_game`` / ``move_game`` / ``publish_game`` commits
        under the same Season lock, so a Game-state mutation must act on the
        FRESH row — otherwise saving the stale object silently resurrects a
        cancelled Game or clobbers a relocation.

        CALLED BY THE GUARD ITSELF since #427, not by individual callers.
        Three of the fifteen roster mutations remembered to call it; the other
        twelve did not, and "remember to re-fetch" is not an invariant. Now
        ``_guard_active_season`` returns this row and every caller rebinds, so
        the lock and the read that follows it cannot be separated."""
        game = self.store.get_game(game_id)
        if game is None:
            raise NotFoundError(f"Game {game_id} not found.")
        return game

    def _require_player(self, player_id: str) -> Player:
        player = self.store.get_player(player_id)
        if player is None:
            raise NotFoundError(f"Player {player_id} not found.")
        return player

    def _require_active_player(self, player_id: str) -> Player:
        """Fail closed on a deactivated player at every substitute transition
        (#270 review). A player enrolled while active, then deactivated, must
        not be offer-able, accept, or be coach-added — the enrollment row stays
        as history but can no longer act. Same message the enroll gate uses.

        The read takes the Player ROW LOCK (``get_player_for_update``) so the
        ``is_active`` check is serialized with a concurrent ``set_player_active``
        deactivation, which locks the same row (#270 review concurrency). On
        PostgreSQL the ``SELECT … FOR UPDATE`` is held to commit: if a
        deactivation locks and commits ``is_active=False`` first, this
        transaction blocks on the lock and then re-reads the committed value and
        fails closed — it cannot proceed on a stale active read, then insert or
        revive a row. MUST be called inside the caller's ``transaction()`` for
        the lock to persist across the subsequent write."""
        player = self.store.get_player_for_update(player_id)
        return self._require_locked_active_player(player_id, player)

    def _require_locked_active_player(
        self, player_id: str, player: Optional[Player],
    ) -> Player:
        """Validate a Player row the caller has already locked.

        Cross-team acceptance must take the Player lock before it samples the
        decision clock, then let expiry win at equality before consulting
        mutable eligibility.  Separating validation avoids a second read while
        preserving the canonical errors used by other substitute transitions.
        """
        if player is None:
            raise NotFoundError(f"Player {player_id} not found.")
        if not player.is_active:
            raise NotEligibleError(f"{player.name} is not an active player.")
        return player

    def _guard_active_season(self, game: Game) -> Game:
        """An archived Season is read-only (#159): its Games accept no roster,
        availability, substitute, lock, or cancel changes. Row-locks the Season
        (must run inside the caller's transaction) so the check is linearizable
        with ``archive_season``. Shared by ``_guard_mutable`` and the few
        mutations that legitimately bypass the cancelled/locked guard.

        WHICH Season is the authority is decided in ONE place for both guard
        families — :func:`season_guard.guard_game_season` (PR #427 blocker).
        This method used to answer it itself with ``if game.season_id:``, which
        skipped the guard on a NULL and locked a SIBLING Season on drift; see
        that function for the full precedence (dangling binding, then the
        canonical Season's archive state, then the denormalized column).

        RETURNS THE GAME RE-FETCHED UNDER THAT LOCK, and every caller rebinds.
        The pre-lock ``_require_game`` read is only a LOCATOR: until the Season
        row is held, a concurrent ``cancel_game``/``move_game``/``publish_game``
        can commit underneath it, so both this guard's own cancelled/locked
        checks and the caller's subsequent write must act on the FRESH row. Only
        three call sites re-fetched before #427; making the guard return the
        row rolls that out everywhere by construction rather than by
        remembering.

        The identity is then RE-VERIFIED on the fresh row, but only when the
        fresh row's identity columns actually differ from the ones just
        judged. Re-running unconditionally would re-lock the same Season on
        every single mutation for nothing; skipping it entirely would let a
        Game whose binding changed between the locator read and the lock be
        written under a Season that is no longer its authority. When they DO
        differ the guard runs again and locks the new canonical Season, which
        is the only correct row to hold — and that second lock is reachable
        only on a row production cannot produce, since every writer that could
        move these columns must itself hold the Season lock this transaction
        is already holding."""
        season_guard.guard_game_season(self.store, game)
        fresh = self._refetch_under_season_lock(game.id)
        if (fresh.league_season_id != game.league_season_id
                or fresh.season_id != game.season_id):
            season_guard.guard_game_season(self.store, fresh)
        return fresh

    def _guard_mutable(self, game: Game) -> Game:
        """Guard for operations that change the committed roster.

        Returns the Game RE-FETCHED under the Season lock (see
        :meth:`_guard_active_season`) — and note the cancelled/locked checks
        below are made on that fresh row, not on the caller's locator read, so
        a cancel or a lock that commits between the two is observed rather than
        clobbered."""
        game = self._guard_active_season(game)  # #159 + #427 — see above
        if game.cancelled:
            raise GameCancelledError("Game is cancelled.")
        if game.locked:
            raise RosterLockedError("Roster is locked. Unlock to make changes.")
        return game

    def _require_authorized_team(
        self, authorized_team_id: Optional[str],
        owning_team_id: Optional[str], what: str,
        *, comparand: str = "durable",
    ) -> None:
        """THE Coach-team authorization gate, re-checked UNDER THE LOCK.

        The owner's transactional blocker (comment 5373064375): "bind
        coach-team authorization and the mutation to one transaction and one
        locked, re-fetched membership/registration context (or pass the
        expected authorized team into a service command that revalidates it
        under those locks before any write). The scope preflight may remain
        for fast denial, but it cannot be the authoritative write gate."

        WHY THE ANSWER IS PASSED IN rather than derived here. These methods
        receive only ``actor_id``, an ACCOUNT id, and nothing in this service
        can turn that into "the team this actor may act for" — the
        coach<->team binding lives in the session scope resolved by
        ``web/server.py``, and ``web/scope.py``'s layering note deliberately
        keeps that arrow pointing the other way. So the caller passes the
        team it already holds and this revalidates it. The ruling is explicit:
        "Do not make the service infer an account's team from ``actor_id``."

        WHY THIS IS ALREADY ATOMIC, with no new store primitives.
        ``season_guard.guard_game_season`` locks the canonical Season row and
        holds it to commit, and EVERY production membership mutation
        (``create_season_roster_membership``,
        ``set_season_roster_membership_status``,
        ``update_season_roster_membership``) goes through
        ``_require_active_season`` and takes that same row. The schema half
        agrees: ``season_roster_memberships.season_id -> seasons(id)`` is a
        real FK, so even a raw INSERT takes ``FOR KEY SHARE`` on the Season
        row and blocks behind ``FOR UPDATE``. A check placed anywhere AFTER
        ``_guard_mutable`` inside the existing ``@_transactional`` body is
        therefore already serialized against every membership writer —
        Memory on its per-instance RLock, SQLite on ``BEGIN IMMEDIATE``'s
        RESERVED lock, PostgreSQL on the Season row.

        ``authorized_team_id is None`` MEANS NO COACH CONSTRAINT, and it is
        the default so that no call site is silently gated by omission. It is
        the correct answer for the paths whose authority was established
        elsewhere and never came from a team at all: Player self-service
        (``/api/me/substitute-opportunities/...``, guarded by
        ``_require_player_scope``), Guardian (``/api/me/guardian/...``,
        guarded by ``_require_guardian_scope`` + ``_guardian_link_or_403``),
        and the unscoped League Admin/operator (``web/scope.py``: "League
        admins and arena managers are not resource-scoped here"). This
        mirrors ``setup_guarded_mutation``'s own convention, where a ``None``
        role is completely ungated and completely untouched. A Coach HTTP
        route always passes its scoped team.

        ``owning_team_id`` IS THE COMPARAND EACH CALLER CHOOSES, and the
        choice is the ruling's: "Row-removal/response commands compare
        against durable row attribution; commands creating new state compare
        against the locked live context." Each call site names which it is
        passing and why.

        A NULL comparand FAILS CLOSED for a Coach, whichever comparand
        produced it. For a DURABLE one it is reachable only for a LEGACY row
        written before durable attribution existed (migration 060/061, both
        additive with no backfill), and such a row cannot prove whose it is.
        For a LIVE one it means no side could be resolved for this player in
        this game at all. Guessing from ``Player.team_id`` or current
        membership is exactly what the ruling forbids in either case, so the
        Coach is refused with zero writes while player self-service and an
        unscoped League Admin — neither of whom needs this column to be
        authorized — keep working.

        ``comparand`` selects only WHICH SENTENCE that refusal carries, never
        whether it happens: see ``_ATTRIBUTION_MISSING_DURABLE`` /
        ``_ATTRIBUTION_MISSING_LIVE`` above for the two cases, and for why
        both keep the SAME machine-readable ``attribution_missing`` reason
        rather than splitting into one an unauthorized caller could mine for
        player/row existence.
        """
        if comparand not in ("durable", "live"):
            raise ValueError(f"unknown comparand {comparand!r}")
        if authorized_team_id is None:
            return
        if owning_team_id is None:
            raise NotAuthorizedError(
                _ATTRIBUTION_MISSING_DURABLE.format(what=what)
                if comparand == "durable" else _ATTRIBUTION_MISSING_LIVE,
                details={"reason": ATTRIBUTION_MISSING,
                         "authorized_team_id": authorized_team_id})
        if owning_team_id != authorized_team_id:
            raise NotAuthorizedError(
                "A coach can only manage their own team's players.",
                details={"reason": TEAM_SCOPE_VIOLATION,
                         "authorized_team_id": authorized_team_id,
                         "owning_team_id": owning_team_id})

    def _require_attributed_enrollment(
        self, sub, authorized_team_id: Optional[str],
    ) -> None:
        """A TEAM-SCOPED actor may not transition an enrollment whose
        ADMITTING SIDE IS UNKNOWN (#427 final blocker, round 2).

        WHY THIS IS SEPARATE FROM :meth:`_require_authorized_team` and not a
        change to it. That gate compares the actor's team against a COMPARAND
        each call site chooses, and the standing ruling (#205 blocker 3) sets
        which comparand: "Row-removal/response commands compare against
        durable row attribution; commands creating new state compare against
        the locked live context." ``withdraw_substitute`` and
        ``decline_substitute`` therefore pass ``sub.team_id`` and ALREADY
        fail closed on a NULL. The three CREATE-STATE transitions —
        ``offer_substitute``, ``accept_substitute``,
        ``add_substitute_to_roster`` — correctly pass the LIVE context, and
        that must stay live: it is what lets the coach a player has genuinely
        transferred TO act, and what keeps the side authorized to offer
        identical to the side the offer is recorded against.

        THE HOLE THAT LEFT. For an enrollment with ``team_id IS NULL`` the
        live comparand does not merely permit the action, it INVENTS the
        answer the row could not give: ``offer_substitute`` writes
        ``sub.team_id = team_id`` three lines on, so the transition MINTS an
        admitting side out of today's membership. Reproduced over an
        authenticated HTTP session at ae21c40 — the HOME Coach offered a
        NULL-owner enrollment and the row came back ``"team_id": "team_1"``,
        durably, and then appeared in that Coach's ``/substitutes``. If the
        row was really the opponent's bench, it has just changed hands.
        That is exactly the guess this blocker forbids, made permanent.

        SO THE TWO QUESTIONS ARE ASKED SEPARATELY. This one runs FIRST and
        asks only "does this row name a side at all?"; the live comparand
        then asks, unchanged, "is that side yours?". Composing them this way
        (rather than switching the create-state comparand to durable) keeps
        the transfer case the standing ruling protects: a durably attributed
        row whose occupant has moved is still resolved live.

        ``authorized_team_id is None`` MEANS NO COACH CONSTRAINT, the same
        convention :meth:`_require_authorized_team` uses and for the same
        reason — player self-service and an unscoped League Admin claim no
        side, so neither is guessing when they act, and an operator remains
        the path by which a legacy row can be repaired at all. Same
        ``attribution_missing`` reason and same message as the durable
        comparand's own NULL refusal, because it is the same fact about the
        same column observed by a different gate."""
        if authorized_team_id is None:
            return
        if sub.team_id is None:
            raise NotAuthorizedError(
                _ATTRIBUTION_MISSING_DURABLE.format(
                    what="substitute enrollment"),
                details={"reason": ATTRIBUTION_MISSING,
                         "authorized_team_id": authorized_team_id})

    def _audit(
        self,
        game_id: str,
        action: AuditAction,
        actor_id: Optional[str] = None,
        subject_player_id: Optional[str] = None,
        detail: Optional[dict] = None,
        team_id: Optional[str] = None,
    ) -> AuditLog:
        entry = AuditLog(
            id=self.store.next_id("audit"),
            game_id=game_id,
            action=action,
            at=self.clock(),
            actor_id=actor_id,
            subject_player_id=subject_player_id,
            detail=detail or {},
            team_id=team_id,
        )
        return self.store.add_audit(entry)

    def _notify(
        self,
        game_id: str,
        type_: NotificationType,
        audience: str,
        message: str,
        subject_player_id: Optional[str] = None,
        team_id: Optional[str] = None,
    ) -> NotificationEvent:
        event = NotificationEvent(
            id=self.store.next_id("notif"),
            game_id=game_id,
            type=type_,
            audience=audience,
            message=message,
            at=self.clock(),
            subject_player_id=subject_player_id,
            team_id=team_id,
        )
        return self.store.add_notification(event)

    # ====================================================================
    # roster selection
    # ====================================================================
    @_transactional
    def select_roster(
        self, game_id: str, player_ids: List[str], actor_id: Optional[str] = None,
        authorized_team_id: Optional[str] = None,
    ) -> List[GameRosterEntry]:
        game = self._require_game(game_id)
        game = self._guard_mutable(game)

        # Row-lock every referenced Player so each is_active eligibility check is
        # serialized with a concurrent set_player_active deactivation (which
        # locks the same row) — otherwise a stale is_active=True read could
        # insert/revive a roster row after deactivation committed is_active=False
        # (#270 review concurrency). Acquire the locks UP FRONT in a
        # deterministic canonical order (sorted unique ids) so two concurrent
        # selections of the same Players in opposite caller order can't AB-BA
        # deadlock on PostgreSQL (#270 review). The locks are held to commit by
        # this @_transactional method; the per-player eligibility errors are
        # still raised below in the CALLER's order, and the output preserves it.
        locked_players = {pid: self.store.get_player_for_update(pid)
                          for pid in sorted(set(player_ids))}

        # #205 blocker 5 round 2: coach selection now resolves the SAME ONE
        # membership context every other seating surface resolves, because
        # the row it writes must carry a DURABLE side/bucket and there is no
        # honest source for that but the context which authorized the
        # seating.
        #
        # THIS TIGHTENS THE COACH-SELECTION GATE, deliberately. The old gate
        # was `player.team_id in (home, away)` — the permanent pointer, the
        # very authority #205 is retiring. It admitted two wrong shapes at
        # once on a LeagueSeason-BOUND game: a "Mover" whose pointer still
        # names a team in this game but whose seasonal record names the
        # OTHER side (seated on the wrong side), and a membership-LESS
        # player whose pointer matches (seated on NO side at all — measured
        # tri-store at head 580a09f: two occupying rows against
        # target_skaters=2 and `open_skater_slots=2` reported, i.e. the
        # owner's "occupying in storage and absent from the governed count"
        # reached with no membership mutation whatsoever). ``enroll_
        # substitute`` — the substitute entry point — has resolved a context
        # and refused both shapes since the step-1 cutover; this makes the
        # coach entry point agree with it instead of being strictly weaker
        # than the list (``list_addable_players``) that feeds it.
        #
        # UNBOUND GAMES ARE UNCHANGED: for a game with no LeagueSeason the
        # context IS the permanent pointer (see resolve_membership_context),
        # so exhibitions and unbound legacy rows keep exactly the old gate
        # and the old attribution.
        #
        # Resolved ONCE for the whole selection (two queries, home + away)
        # rather than per player, so a 20-player roster does not pay 20
        # membership lookups — the same batching _side_data uses, and the
        # same precedence, via the shared _pick_eligible_membership.
        bound = season_guard.game_is_league_season_bound(game)
        contexts = (self.resolve_membership_contexts_for_game(game)
                    if bound else {})

        # AUTHORIZE EVERY TARGET BEFORE SEATING ANY OF THEM (#205, the
        # transactional blocker). Selection is a BATCH, and the write loop
        # below seats each player as it goes: a Coach check made inside that
        # loop would let player 1 be written before player 2's refusal, and
        # while `@_transactional` rolls the row back, "zero write ATTEMPTS on
        # refusal" is an ORDERING property a rollback cannot satisfy. So the
        # contexts are resolved ONCE here, up front, and reused below —
        # which also removes the second per-player resolution the unbound
        # path used to pay.
        #
        # COMPARAND: THE LOCKED LIVE CONTEXT, per player. Selection CREATES
        # NEW STATE (it seats rows on `ctx.team_id`), so the ruling's create
        # half applies; the same `ctx` becomes the row's durable attribution
        # below, so authorization and attribution are one decision.
        #
        # A player who resolves to NO context passes `None` and fails closed
        # for a Coach — an unresolvable player cannot be shown to be theirs.
        # RAISING NOTHING ELSE HERE IS DELIBERATE: with
        # `authorized_team_id=None` this pass is a pure read and every
        # existing error and its ORDER are preserved exactly, because the
        # ctx-missing / not-found / inactive refusals still happen in the
        # caller's player order in the write loop below.
        resolved: Dict[str, Optional["GameMembershipContext"]] = {}
        for player_id in player_ids:
            player = locked_players.get(player_id)
            ctx = None
            if player is not None:
                ctx = (contexts.get(player_id) if bound
                       else self.resolve_membership_context(game, player))
            resolved[player_id] = ctx
            # ``comparand="live"``: a NULL here is NEVER a pre-061 row — this
            # site reads no row at all. It means the id names nobody, or names
            # a player with no membership resolving onto either side of this
            # game. Both are refused identically and worded as what they are;
            # see ``_ATTRIBUTION_MISSING_LIVE`` for why they stay merged.
            self._require_authorized_team(
                authorized_team_id, ctx.team_id if ctx else None, "player",
                comparand="live")

        # CLASSIFY EVERY EXISTING ROW BEFORE SEATING ANY OF THEM (#205, the
        # cross-team idempotency blocker).
        #
        # THE DEFECT THIS CLOSES, measured tri-store and over authenticated
        # HTTP at head 63db78f. The loop above authorizes each target against
        # the LIVE membership context, which is the right comparand for the
        # half of this method that CREATES state. But the write loop below
        # then treated ANY occupying row as idempotent and returned it
        # without ever consulting the row's own durable `team_side`. So: a
        # HOME Coach seats a player (entry_1, team_side=HOME); the player's
        # membership transfers to AWAY; the AWAY Coach calls select_roster
        # with authorized_team_id=AWAY. The live gate PASSES — the player
        # really is theirs now — and the method returned entry_1 itself, HOME
        # attribution, HOME `selected_by` and all, with 200 over
        # `POST /api/games/{id}/roster/select`. Storage stayed HOME-occupied
        # and AWAY open, and a second `roster_selected` audit was appended
        # although no roster state had changed. An opposing Coach received
        # another side's durable row plus a false success while their own
        # side stayed unfilled.
        #
        # THE RULE, and why it takes a comparand the loop above cannot.
        # Durable attribution answers WHICH slot or side a row holds; the
        # live context authorizes WHO may act on it. Idempotency is a claim
        # about an EXISTING row, so it must be judged against that row's
        # durable side — the same `entry.team_side` (migration 061)
        # `remove_player` and `set_availability` already compare against, and
        # for the same reason. Judging it live is what made idempotency
        # CROSS-TEAM instead of side-owned.
        #
        # WHY IT IS A WHOLE-BATCH PREFLIGHT AND NOT A CHECK IN THE WRITE
        # LOOP. Selection is a batch and the loop below seats each player as
        # it goes, so a check made there would let player 1 be written before
        # player 2's foreign row was noticed. `@_transactional` would roll
        # that write back, but "zero write ATTEMPTS on refusal" is an
        # ORDERING property no rollback can satisfy — the same argument that
        # put the live gate above the write loop in the first place. So both
        # gates are preflights and the write loop is reached only once every
        # target has cleared both.
        #
        # THE THREE OUTCOMES, all delegated to the ONE existing gate rather
        # than to a second reason vocabulary invented here:
        #
        #   FOREIGN occupying row -> `team_scope_violation`, raised by
        #   `_require_authorized_team` with the same structured details every
        #   other surface raises. Nothing about the row is returned: no id,
        #   no `selected_by`, no `seated_position`, no status, and no serialized
        #   entry — the refusal happens before the write loop can append it to
        #   `entries`.
        #
        #   NULL attribution -> fails CLOSED under that same gate's existing
        #   typed rule (`attribution_missing`, `comparand="durable"`). The row
        #   was FOUND and cannot name its side, which is reachable only for a
        #   pre-060/061 row, so the DURABLE wording is the literally true one
        #   here — unlike the live loop above, which found no row at all.
        #
        #   OWN occupying row -> falls through to the write loop, where the
        #   pre-existing idempotent return is now correct BY CONSTRUCTION:
        #   every occupying row that reaches it has already been proven to
        #   carry `team_side == authorized_team_id`.
        #
        # UNSCOPED OPERATORS ARE PRESERVED EXPLICITLY. With
        # `authorized_team_id=None` — League Admin, Arena Manager, and the
        # in-process/self-service call paths whose authority never came from a
        # team — `_require_authorized_team` returns on its first line, so this
        # whole pass is a pure read and the previous unconditional
        # `occupies_slot` behaviour is byte-for-byte unchanged.
        #
        # THIS PASS IS PURELY CLASSIFICATORY AND THE WRITE LOOP STILL DOES ITS
        # OWN READ. Caching the row here and reusing it below looks like a free
        # halving of the lookups, and it is wrong: `player_ids` MAY CONTAIN THE
        # SAME PLAYER TWICE (``test_lifecycle_concurrency``'s
        # ``RosterSelectionOrderParityTest`` passes ``[p3, p1, p2, p1]`` for
        # exactly this reason, and the output is required to echo the caller's
        # order duplicates included). The write loop relies on re-reading to see
        # the row IT JUST INSERTED for an earlier occurrence of the same id and
        # take the idempotent branch; served from a snapshot taken before any
        # write, the second occurrence would insert a second row and violate the
        # one-row-per-(game, player) unique index (migration 023). Measured: the
        # cached version failed that test on Memory (2 rows) and SQLite
        # (IntegrityError).
        for player_id in player_ids:
            existing = self.store.roster_entry_for_player(game_id, player_id)
            if existing is None or not existing.status.occupies_slot:
                # Missing or non-occupying: this is a CREATE or a REVIVE, the
                # live context authorized it above, and it is that context
                # which (re-)attributes the row below. Nothing durable is
                # being claimed, so there is nothing to classify.
                continue
            self._require_authorized_team(
                authorized_team_id, existing.team_side, "roster row",
                comparand="durable")

        # Whether this call actually CHANGED any roster state. A selection in
        # which every requested row was already seated on this side is a true
        # no-op, and the audit must not claim a second selection that never
        # happened (#205: the duplicate `roster_selected` the blocker
        # measured). Set by the revive and insert branches only.
        changed = False

        entries: List[GameRosterEntry] = []
        for player_id in player_ids:
            player = locked_players[player_id]
            if player is None:
                raise NotFoundError(f"Player {player_id} not found.")
            ctx = resolved[player_id]
            if ctx is None:
                raise NotEligibleError(
                    f"{player.name} is not on either team in this game."
                )
            if not player.is_active:
                raise NotEligibleError(f"{player.name} is not an active player.")

            now = self.clock()
            # Re-read, deliberately: a repeated player id in this same batch
            # must see the row an earlier iteration wrote. See the
            # classification pass above for why this is not cached.
            existing = self.store.roster_entry_for_player(game_id, player_id)
            if existing is not None:
                if existing.status.occupies_slot:
                    # idempotent: already selected. The attribution it is
                    # ALREADY seated on stands — re-selecting an occupying
                    # row is a no-op, not a re-seat, so it must not silently
                    # re-attribute a row (including a pre-061 row, whose
                    # NULL attribution stays NULL and stays fail-closed).
                    #
                    # FOR A COACH this row has already been PROVEN to be
                    # theirs by the durable preflight above — a foreign or
                    # unattributed row never reaches this line. For an
                    # unscoped operator the preflight abstained and this
                    # stays the unconditional pre-existing behaviour.
                    # Either way nothing is written, so `changed` is not set
                    # and this player alone cannot justify an audit row.
                    entries.append(existing)
                    continue
                # Revive a removed/unavailable row instead of inserting a
                # duplicate — one row per (game_id, player_id), now also enforced
                # by a unique index (#201 Slice 3B, migration 023). A revive IS
                # a re-seat, so it re-writes the durable attribution from the
                # context that just authorized it.
                existing.roster_role = RosterRole.SELECTED
                existing.selection_source = SelectionSource.COACH_SELECTED
                existing.status = RosterEntryStatus.SELECTED
                existing.selected_by = actor_id
                existing.updated_at = now
                existing.team_side = ctx.team_id
                existing.seated_position = ctx.position
                self.store.save_roster_entry(existing)
                entries.append(existing)
                changed = True
                continue

            entry = GameRosterEntry(
                id=self.store.next_id("entry"),
                game_id=game_id,
                player_id=player_id,
                roster_role=RosterRole.SELECTED,
                selection_source=SelectionSource.COACH_SELECTED,
                status=RosterEntryStatus.SELECTED,
                selected_at=now,
                updated_at=now,
                selected_by=actor_id,
                team_side=ctx.team_id,
                seated_position=ctx.position,
            )
            self.store.add_roster_entry(entry)
            entries.append(entry)
            changed = True

        # NOTHING CHANGED MEANS NOTHING TO AUDIT (#205). Every requested row
        # was already seated on this side, so no roster state moved and an
        # audit row here would assert a selection that did not occur — the
        # duplicate `roster_selected` the blocker measured when the AWAY
        # Coach's call "succeeded" against a HOME row. `_audit` also mints an
        # id, so suppressing the row also removes the `next_id` write attempt
        # a true no-op has no business making.
        #
        # This is deliberately about CHANGE, not about refusal: a batch that
        # revives or inserts even one row still audits once, with the full
        # requested `player_ids` as the detail, exactly as before.
        if changed:
            self._audit(
                game_id,
                AuditAction.ROSTER_SELECTED,
                actor_id=actor_id,
                detail={"player_ids": player_ids},
            )
        return entries

    # ====================================================================
    # availability / confirm / back out
    # ====================================================================
    @_transactional
    def set_availability(
        self,
        game_id: str,
        player_id: str,
        availability_status: AvailabilityStatus,
        response_source: str = "player",
        actor_id: Optional[str] = None,
        notes: Optional[str] = None,
        authorized_team_id: Optional[str] = None,
    ) -> GameAvailability:
        game = self._require_game(game_id)
        game = self._guard_active_season(game)  # #159/#427 guard + re-fetch
        if game.cancelled:
            raise GameCancelledError("Game is cancelled.")
        if game.locked:
            raise RosterLockedError("Roster is locked. Unlock to make changes.")
        player = self._require_player(player_id)

        # Validate the roster-entry transition BEFORE persisting anything so a
        # rejected re-confirm leaves no partial availability/audit state.
        entry = self.store.roster_entry_for_player(game_id, player_id)

        # COMPARAND: THE ROW WHEN THERE IS ONE, THE LIVE CONTEXT WHEN THERE
        # IS NOT — this surface spans both halves of the ruling, so it takes
        # both answers rather than forcing one.
        #
        #   AN EXISTING ROW makes this a RESPONSE command: it confirms or
        #   backs out a seat that already exists, so "is this seat MINE?" is
        #   answered by `entry.team_side`, the durable attribution
        #   (migration 061) — the same source `remove_player` uses, and for
        #   the same reason. This is what preserves the coach's ordinary
        #   cleanup path: marking a transferred player unavailable still
        #   works for the side whose slot they are still occupying.
        #
        #   NO ROW makes it a CREATE: nothing is seated, only a
        #   GameAvailability is recorded, and there is no durable side to
        #   consult. The honest question is then whether the player is on
        #   this coach's side right now, under the Season lock — so the live
        #   context answers, and it fails closed when there is none.
        #
        # Placed BEFORE the transition-validation block and therefore before
        # `upsert_availability`, this method's first write. Note this is a
        # COACH gate and is entirely separate from `_authorize_seated_side`
        # below, which asks the PLAYER question ("may this player still hold
        # the side this row sits on?") — two different subjects, two gates.
        # THE UNATTRIBUTED WORDING IS THE SAME ON BOTH BRANCHES, DELIBERATELY
        # (PR #427 review, finding F-5). This site is the one place where a
        # durable NULL (a pre-061 row that was found) and a live NULL (no row
        # at all, and no resolvable side) can BOTH reach the gate, so letting
        # the sentence differ would tell an unauthorized coach whether the
        # player holds a roster row in this game — the exact disclosure the
        # gate is refusing to make. ``comparand="live"`` is therefore passed
        # unconditionally: it is the honest description of the merged case
        # ("your team could not be confirmed as this player's"), and it never
        # claims a nonexistent row "predates durable team attribution", which
        # is what the previous single message said about a row that does not
        # exist. The reason code is `attribution_missing` on both branches,
        # unchanged, and both still fail closed with zero writes.
        if authorized_team_id is not None:
            if entry is not None:
                owning = entry.team_side
            else:
                ctx = self.resolve_membership_context(game, player)
                owning = ctx.team_id if ctx is not None else None
            self._require_authorized_team(authorized_team_id, owning,
                                          "player", comparand="live")

        reconfirming = (
            entry is not None
            and availability_status == AvailabilityStatus.AVAILABLE
            and entry.status == RosterEntryStatus.UNAVAILABLE
        )
        if entry is not None and availability_status == AvailabilityStatus.AVAILABLE:
            if entry.status == RosterEntryStatus.REMOVED:
                raise InvalidTransitionError(
                    "Player was removed by the coach and cannot self re-confirm."
                )
            if reconfirming:
                # RE-CONFIRM ASKS TWO DIFFERENT QUESTIONS, AND THEY HAVE TWO
                # DIFFERENT ANSWERERS (#205 blocker 5 round 3 — owner ruling
                # on PR #427, comment 5379885403). Round 2 conflated them and
                # shipped a hole: it proved the player had SOME valid context
                # and then gated the ROW's durable side, so a player whose
                # membership had moved HOME->AWAY passed the eligibility call
                # on their AWAY context and self-confirmed straight back onto
                # the HOME row they were no longer eligible for.
                #
                #   IDENTIFY — "WHICH slot is being taken back?" is answered
                #   by the ROW'S DURABLE ATTRIBUTION and by nothing else. A
                #   re-confirm is not a seating: the row never left its slot,
                #   so the only slot in question is the one it holds. That is
                #   what keeps enforcement (_require_open_slot via
                #   _slot_summaries) and reporting (compute_roster_status)
                #   ONE rule, and it must not be re-resolved — a fresh
                #   context could name a different bucket (a season-scoped
                #   position that changed) or a different side than the row
                #   is actually counted in.
                #
                #   AUTHORIZE — "MAY THIS PLAYER re-occupy that slot's SIDE?"
                #   is answered by the LIVE context and by nothing else.
                #   Durable attribution records what the row holds; it says
                #   nothing about who is still entitled to hold it. A stored
                #   value can never authorize its own re-use, or the row
                #   would be a standing permission that outlives the
                #   participation that granted it.
                #
                # So: resolve LIVE (fails closed exactly as before — the #270
                # deactivation gate / the accept_substitute re-resolution),
                # read the slot off the ROW, and refuse unless the live
                # context's team IS the side that row occupies. That last
                # step is _authorize_seated_side, which the SELECTED branch
                # below calls too — ONE gate, so the two player-driven
                # routes to CONFIRMED cannot drift apart.
                #
                # A ROW THAT NAMES NO SIDE NEVER GETS THAT FAR: the gate
                # refuses NULL durable attribution with
                # ``attribution_missing`` BEFORE it resolves anything live
                # (owner ruling, PR #427, comment 5384676215 — see the
                # gate's STEP 1). This branch used to make that decision
                # itself, on the gate's ``None`` return, which meant the
                # live-context lookup inside the gate ran first and a
                # player whose membership had ALSO ended was diagnosed by
                # that unrelated second fact instead.
                side, st = self._authorize_seated_side(
                    game, player, entry, "re-confirm")
                # ...and only once the player is authorized for that side do
                # we ask whether the slot itself is still free (a substitute
                # may have filled it while they were out). The BUCKET is the
                # durable one, never ctx.position: re-confirming puts THIS
                # ROW back in the slot it held, so a season-scoped position
                # change must not silently move it to a different bucket.
                self._require_open_slot(game_id, st, side)
            elif entry.status in _PLAYER_CONFIRM_SOURCE_STATES:
                # THE SAME SPLIT, for the transitions that never back out
                # (#205, owner ruling on PR #427, 2026-08-22): "For player
                # self-service, the durable roster row identifies the side
                # and bucket; the player's current live context authorizes
                # the transition. Therefore, SELECTED -> CONFIRMED must
                # reject when the player's live team no longer matches the
                # durable row's side." Refined 2026-08-22: "Apply it to
                # every actual player-driven transition into CONFIRMED,
                # including ACCEPTED -> CONFIRMED. Use an explicit
                # transition-state set rather than entry.status.
                # occupies_slot."
                #
                # WHICH STATES, and why they are named rather than derived:
                # see ``_PLAYER_CONFIRM_SOURCE_STATES`` at module level.
                # SELECTED is the coach-selected row; ACCEPTED is the
                # substitute who took an offer. Both are seated bodies that
                # never backed out, so the ``reconfirming`` gate above
                # (which requires UNAVAILABLE) never saw either: a player
                # seated on HOME who then moved HOME->AWAY could
                # self-confirm on the durable HOME row and become a
                # confirmed HOME body while being live-eligible only for
                # AWAY.
                #
                # NOT here, deliberately: CONFIRMED. A row already in that
                # state is not transitioning into it, and reaffirming
                # availability stays the idempotent no-op it has always
                # been (owner ruling, Decision 2).
                #
                # NO OPEN-SLOT CHECK HERE, deliberately. This row ALREADY
                # occupies its slot and CONFIRMED occupies the same one, so
                # nothing is being taken: requiring an open slot would
                # refuse every legitimate confirmation of the last seat.
                # Occupancy is not the question — authorization is.
                #
                # NULL DURABLE ATTRIBUTION FAILS CLOSED HERE TOO — owner
                # ruling, PR #427, 2026-08-22 (Decision 3): "A selected
                # legacy row with no team_side/seated_position cannot prove
                # that its live team matches the side it holds. Reject with
                # attribution_missing before any attempted write, without
                # adding an open-slot check."
                #
                # That REVERSED the round before it, which let a pre-061
                # row through on this path on the reasoning that confirming
                # "takes nothing back". Overruled: the question this branch
                # asks is not "is a slot being taken?" but "can this player
                # be shown to be eligible for the side this row sits on?",
                # and a row that names no side can never answer it. Silence
                # is not a match.
                #
                # THE REFUSAL LIVES IN THE GATE, and is now taken BEFORE
                # the live resolution rather than after it (comment
                # 5384676215 — see ``_authorize_seated_side``'s STEP 1), so
                # a NULL row whose membership has ALSO ended is still
                # diagnosed ``attribution_missing`` instead of by the
                # unrelated second fact. Still no open-slot check, and
                # still raised BEFORE the GameAvailability upsert below,
                # which is ``set_availability``'s first write.
                self._authorize_seated_side(
                    game, player, entry, "confirm")

        existing = self.store.availability_for_player(game_id, player_id)
        av = GameAvailability(
            id=existing.id if existing else self.store.next_id("avail"),
            game_id=game_id,
            player_id=player_id,
            availability_status=availability_status,
            response_source=response_source,
            responded_at=self.clock(),
            notes=notes,
        )
        self.store.upsert_availability(av)
        self._audit(
            game_id,
            AuditAction.AVAILABILITY_SET,
            actor_id=actor_id,
            subject_player_id=player_id,
            detail={"availability_status": availability_status.value,
                    "response_source": response_source},
            # A seated row is the durable authority for the side this answer
            # affects.  In particular, an accepted cross-team substitute has
            # no membership on the borrowing side, so live resolution cannot
            # recover this attribution later.
            team_id=(entry.attribution[0]
                     if entry is not None and entry.attribution is not None
                     else None),
        )

        # Keep the roster entry in sync so the status engine reacts.
        #
        # This predicate is ``occupies_slot`` on purpose and is NOT the
        # authorization set above: it asks "does this row hold a seat that
        # should track the player's answer?", which is the counting
        # question. It is what keeps an already-CONFIRMED row's reaffirm
        # the idempotent no-op Decision 2 preserves — CONFIRMED occupies,
        # so it lands here and re-writes the status it already has, while
        # never having been treated as a transition INTO CONFIRMED.
        if entry is not None:
            if availability_status == AvailabilityStatus.AVAILABLE and (
                entry.status.occupies_slot or reconfirming
            ):
                self._set_entry_status(entry, RosterEntryStatus.CONFIRMED)
            elif (availability_status == AvailabilityStatus.UNAVAILABLE
                  and entry.status.occupies_slot):
                self._back_out_entry(game, entry, actor_id)
        return av

    def _authorize_seated_side(
        self, game, player, entry, action: str
    ) -> Tuple[str, SlotType]:
        """AUTHORIZE — "MAY THIS PLAYER still hold the SIDE this row is
        seated on?" — for every PLAYER-DRIVEN transition into CONFIRMED.

        ONE gate, called from both of ``set_availability``'s routes to
        CONFIRMED (the re-confirm of a backed-out row, and the confirm of a
        row that never backed out), because they ask the identical question
        and a second copy would be free to answer it differently. Written
        as a helper for exactly that reason: a change here is a change to
        both, and a mutation here breaks both.

        ------------------------------------------------------------------
        STEP 1 — CAN THE ROW NAME A SIDE AT ALL? Asked FIRST, and answered
        without consulting anything live (owner ruling, PR #427, comment
        5384676215):

          "a selected row with NULL durable attribution and no live
           membership does not return attribution_missing.
           _authorize_seated_side calls _require_membership_context before
           reading entry.attribution. […] The request is safely refused,
           but it returns only not_eligible: … no membership with a team in
           it with no details.reason, instead of the ruled
           attribution_missing outcome. […] NULL attribution is
           independently sufficient to prove that the row cannot identify
           the side being confirmed. Letting the live-context lookup win
           changes the structured operator/API diagnosis according to an
           unrelated second defect and means the shared NULL contract is
           not actually authoritative."

        Measured tri-store at head 04a4b11 — a SELECTED row on HOME with
        ``team_side``/``seated_position`` NULLed and its membership ENDED,
        on both routes into CONFIRMED:

          [memory] selected_confirm:      code='not_eligible' details=None
          [memory] backed_out_reconfirm:  code='not_eligible' details=None
          message='… is not eligible for this game (no membership with a
                   team in it; cross-team borrowing is off).'

        …identically on SQLite and PostgreSQL. The refusal was safe but
        UNSTRUCTURED, and it named the wrong condition: whether the player
        still has a live membership is a SECOND, independent fact, and it
        cannot decide a question the row has already answered by naming no
        side. So :meth:`_refuse_unattributed` is raised here, before the
        resolution — which also keeps it before every attempted write,
        since the resolution itself is the first thing this gate did.

        STEP 2 — MAY THIS PLAYER HOLD THAT SIDE? There are exactly two
        authorities, and both fail closed:

        * an ordinary seated player must still have a LIVE membership
          context for the row's side; or
        * an ACCEPTED cross-team substitute must still have the one matched
          accepted enrollment + substitute-pool row that
          :meth:`accepted_cross_team_roster_entry` validates.  A backed-out
          UNAVAILABLE row may use that authority only after the original
          cross-team relationship is revalidated from live, locked state.

        The second authority is deliberately narrow. Accepting a borrowed
        seat makes that game a current player commitment; choosing MAYBE
        afterwards must not strand the player in a state where the same Home
        card's ``I'm In`` action can never confirm that seat. A bare row, a
        bare enrollment, a terminal row, half provenance, or a target/slot
        mismatch does not pass. This does not make the borrower a member of
        the target side or authorize any private game read.

        Everyone else is resolved from live membership exactly as before.
        Durable attribution records what the row HOLDS; it says nothing by
        itself about who is entitled to hold it. Resolution fails CLOSED
        (``_require_membership_context`` — the #270 deactivation gate), so
        a player with no live context at all is refused before any
        comparison is reached.

        NO OPEN-SLOT CHECK IS ADDED by either step — the ruling forbids
        one here, and the re-confirm caller keeps running its own after this
        authorization gate.

        Returns the row's ``(team_side, slot_type)`` attribution, never
        ``None``: the re-confirm caller needs the pair for its slot gate,
        and an unattributed row raises above rather than handing back a
        value each caller would have to re-interpret.

        ``action`` is the caller's verb, so the message names the refused
        transition; the machine-readable ``details`` are identical on both
        paths by construction.
        """
        attribution = entry.attribution
        if attribution is None:
            self._refuse_unattributed(player, action)

        # A cross-team acceptance is its own narrow seat authority. Reuse the
        # paired-row oracle that exposes this commitment on Player Home so
        # rendering and the Home attendance action cannot disagree. ACCEPTED
        # handles first confirmation; UNAVAILABLE is the exact paired row's
        # recovery path after the player backs out. REMOVED and every
        # unmatched/corrupt row remain outside this exception.
        borrowed = self.accepted_cross_team_roster_entry(
            game, player, include_unavailable=True)
        if (entry.status in {
                    RosterEntryStatus.ACCEPTED,
                    RosterEntryStatus.UNAVAILABLE,
                }
                and borrowed is not None
                and borrowed.id == entry.id
                and borrowed.attribution == attribution):
            # ACCEPTED is durable history, not standing permission.  The
            # paired-row oracle above proves which historical relationship
            # created this seat; before a player can confirm/re-confirm it,
            # lock and revalidate every live fact which originally made that
            # cross-team relationship eligible.  This deliberately leaves a
            # post-subtree-deletion row visible on Home for honest history,
            # while refusing to turn its frozen source ids into authorization.
            accepted = [
                sub for sub in
                self.store.substitute_enrollments_for_player(player.id)
                if (sub.game_id == game.id
                    and sub.status == SubstituteStatus.ACCEPTED
                    and self._is_cross_team_enrollment(sub)
                    and sub.team_id == attribution[0]
                    and sub.slot_type == attribution[1])
            ]
            if len(accepted) != 1:
                raise NotEligibleError(
                    f"{player.name} cannot {action}: the accepted "
                    "cross-team seat could not be verified.")
            locked_player = self.store.get_player_for_update(player.id)
            decision_at = self.clock()
            player = self._require_locked_active_player(
                player.id, locked_player)
            self._require_cross_team_game_actionable(
                game, as_of=decision_at)
            self._require_cross_team_enrollment_context(
                game, player, accepted[0])
            return attribution

        ctx = self._require_membership_context(game, player)
        if ctx.team_id != attribution[0]:
            # AUTHORIZATION, not occupancy — deliberately raised BEFORE any
            # slot gate and with its own reason so the two refusals can
            # never be mistaken for each other. "That side's slot happens
            # to be free" is not a licence to take it; a player who now
            # plays for the other side has no claim on this row's slot
            # whether it is open or full. (Their route back onto a roster
            # is a coach re-selection or a substitute accept — both of
            # which RE-SEAT the row from the live context that authorized
            # them, so identify and authorize agree by construction.)
            raise NotEligibleError(
                f"{player.name} cannot {action}: this roster row holds a "
                f"slot on a side they are no longer eligible for.",
                details={"reason": "seated_side_not_live_eligible",
                         "seated_team_id": attribution[0],
                         "eligible_team_id": ctx.team_id})
        return attribution

    def _refuse_unattributed(self, player, action: str) -> None:
        """REFUSE a pre-061 row that carries no durable attribution, for
        EVERY player-driven transition into CONFIRMED.

        Owner ruling, PR #427, 2026-08-22 (Decision 3): "NULL durable
        attribution must fail closed. A selected legacy row with no
        team_side/seated_position cannot prove that its live team matches
        the side it holds. Reject with attribution_missing before any
        attempted write, without adding an open-slot check."

        ONE function, so the two paths cannot drift: the round before this
        one refused on the re-confirm path and allowed on the confirm
        path, and the two answers lived in two places precisely because
        they were allowed to differ. Now the ``reason`` string
        (``attribution_missing``) is written once and both paths raise it
        — a mutation to this message or reason breaks both paths' tests
        together, which is the property the shared
        ``_authorize_seated_side`` already gives the mismatch refusal.

        AND IT IS ``_authorize_seated_side`` THAT CALLS IT, as its first
        step, rather than each caller acting on a ``None`` return (owner
        ruling, PR #427, comment 5384676215). While the decision lived in
        the callers, the gate had already resolved the live membership by
        the time they could take it, so a NULL row belonging to a player
        whose participation had ALSO ended was refused by that unrelated
        second fact and answered ``not_eligible`` with no ``details``.
        One call site inside the gate makes the NULL contract
        authoritative for both routes at once; this function always
        raises.

        ``action`` is the caller's verb so the human message names the
        refused transition; the machine-readable ``details`` are identical
        on both paths by construction."""
        raise NotEligibleError(
            f"{player.name} cannot {action}: this roster row predates "
            f"durable game-side attribution, so the slot it holds cannot "
            f"be identified.",
            details={"reason": "attribution_missing"})

    @_transactional
    def set_roster_entry_status(
        self,
        game_id: str,
        player_id: str,
        status: RosterEntryStatus,
        actor_id: Optional[str] = None,
    ) -> GameRosterEntry:
        """PATCH /roster/{playerId}/status — confirm or back out."""
        game = self._require_game(game_id)
        game = self._guard_mutable(game)
        entry = self.store.roster_entry_for_player(game_id, player_id)
        if entry is None:
            raise NotFoundError(f"Player {player_id} is not on this game's roster.")

        if status == RosterEntryStatus.CONFIRMED:
            self._set_entry_status(entry, RosterEntryStatus.CONFIRMED)
            self._sync_availability(game_id, player_id, AvailabilityStatus.AVAILABLE)
        elif status in (RosterEntryStatus.UNAVAILABLE, RosterEntryStatus.REMOVED):
            self._back_out_entry(game, entry, actor_id,
                                 removed=status == RosterEntryStatus.REMOVED)
            self._sync_availability(game_id, player_id,
                                    AvailabilityStatus.UNAVAILABLE)
        else:
            self._set_entry_status(entry, status)
        return entry

    def _sync_availability(
        self, game_id: str, player_id: str, status: AvailabilityStatus
    ) -> None:
        existing = self.store.availability_for_player(game_id, player_id)
        av = GameAvailability(
            id=existing.id if existing else self.store.next_id("avail"),
            game_id=game_id,
            player_id=player_id,
            availability_status=status,
            response_source="player",
            responded_at=self.clock(),
        )
        self.store.upsert_availability(av)

    def _set_entry_status(
        self, entry: GameRosterEntry, status: RosterEntryStatus
    ) -> None:
        entry.status = status
        entry.updated_at = self.clock()
        self.store.save_roster_entry(entry)

    def _back_out_entry(
        self,
        game: Game,
        entry: GameRosterEntry,
        actor_id: Optional[str],
        removed: bool = False,
    ) -> None:
        event_team_id = (entry.attribution[0]
                         if entry.attribution is not None else None)
        new_status = (
            RosterEntryStatus.REMOVED if removed else RosterEntryStatus.UNAVAILABLE
        )
        self._set_entry_status(entry, new_status)
        action = AuditAction.PLAYER_REMOVED if removed else AuditAction.PLAYER_BACKED_OUT
        self._audit(
            game.id,
            action,
            actor_id=actor_id,
            subject_player_id=entry.player_id,
            team_id=event_team_id,
        )
        if not removed:
            self._notify(
                game.id,
                NotificationType.PLAYER_BACKED_OUT,
                audience="coach",
                message="A selected player is unavailable.",
                subject_player_id=entry.player_id,
                team_id=event_team_id,
            )
        # Recalculate and, if a slot is now open with no substitutes, alert.
        # #205 blocker 5 round 2: the side that just LOST a player is the
        # side the row was SEATED on — read straight off the row's durable
        # attribution, never re-resolved. The previous version called
        # team_for_game(game, player) here, a LIVE lookup, so the moment the
        # player's membership ended the open-slot alert was addressed to
        # None (push suppressed — the real coach who now has a hole in the
        # roster heard nothing) or, after a move, could name the OPPOSITE
        # team. The slot that just reopened belongs to whoever was holding
        # it, which is exactly what team_side records.
        #
        # Still tolerant, never raising: this is a post-hoc read for a
        # notification, not a gate. A pre-061 row carries no attribution, so
        # team_id is None, the message falls back to compute_roster_status's
        # own home default, and the TARGETED push is skipped below rather
        # than sent to a guessed audience.
        team_id = entry.team_side
        status = self.compute_roster_status(game.id, team_id)
        if status.status == GameStatus.OPEN_SLOT:
            self._notify(
                game.id,
                NotificationType.SLOT_OPEN,
                audience="coach",
                message=status.message,
                team_id=event_team_id,
            )
            # Feed notification to that team's coach (#32).
            # #205 blocker 3 (sibling): unlike decline_substitute's offer,
            # a roster entry has no earlier "offer" moment to have
            # snapshotted a team onto — the comment above already documents
            # why team_id is resolved fresh, tolerantly, right here. That
            # tolerance was incomplete: a None team_id (membership lapsed by
            # back-out time) was still fed as audience_ref into this
            # COACH-audience push, which delivery.recipient_ref's #60
            # fail-closed invariant refuses — raising and rolling the whole
            # @_transactional back-out back, the same crash-and-revert bug
            # decline_substitute had. The back-out itself must never be
            # undone by a notification with no honest audience to reach, so
            # the push is skipped (never sent to a guessed/broadened
            # audience — #60 stays intact) rather than attempted with
            # audience_ref=None.
            if team_id is not None:
                _push_notification(
                    self.store, self.clock,
                    NotificationKind.ROSTER_OPEN_SLOT, NotificationAudience.COACH,
                    "Open roster slot", status.message,
                    audience_ref=team_id, game_id=game.id)

    # ====================================================================
    # substitute workflow
    # ====================================================================
    @_transactional
    def enroll_substitute(
        self, game_id: str, player_id: str, actor_id: Optional[str] = None,
        authorized_team_id: Optional[str] = None,
        target_team_id: Optional[str] = None,
    ) -> SubstituteEnrollment:
        game = self.store.get_game(game_id)
        if game is None:
            # The cross-team endpoint addresses a public compound resource;
            # do not distinguish an unknown game from a hidden one.  The
            # legacy omitted-target command keeps its established diagnostic.
            if target_team_id is not None:
                raise NotFoundError("Opportunity not found.")
            game = self._require_game(game_id)
        # Resolve identity first, but keep the active-state check behind the
        # team-authorization decision below.  A scoped coach must not be able
        # to probe whether a player on another side is active by comparing two
        # different refusal envelopes.
        player = self._require_player(player_id)

        # A cross-team opt-in is a public Player-Home resource, not a private
        # game probe.  Hide every game state in which that choice would not be
        # advertised behind the same 404 as an unknown compound key.  Do the
        # check once on the locator row (so an already-hidden game never
        # reaches an integrity/lock diagnostic), then again on the row
        # re-fetched under the Season lock so a concurrent unpublish, cancel,
        # roster lock or puck drop cannot turn the POST into an oracle.
        direct = self.resolve_membership_context(game, player)
        cross_request = (
            target_team_id is not None
            and (direct is None or direct.team_id != target_team_id))
        if cross_request and not self._cross_team_opt_in_visible(game):
            raise NotFoundError("Opportunity not found.")
        if cross_request:
            game = self._guard_active_season(game)
            player = self._require_player(player_id)
            direct = self.resolve_membership_context(game, player)
            cross_request = (
                target_team_id is not None
                and (direct is None or direct.team_id != target_team_id))
            if cross_request and not self._cross_team_opt_in_visible(game):
                raise NotFoundError("Opportunity not found.")
            if game.cancelled:
                raise GameCancelledError("Game is cancelled.")
            if game.locked:
                raise RosterLockedError(
                    "Roster is locked. Unlock to make changes.")
        else:
            game = self._guard_mutable(game)

        # ONE validated relationship serves both the gate and the row below:
        # the source context supplies the seasonal position, while the
        # requested game side is the durable target. Omitted-target same-team
        # enrollment still resolves exactly as it did before #287.
        target_ctx = self._require_substitute_target_context(
            game, player, target_team_id)
        ctx = target_ctx.source
        # COMPARAND: THE LOCKED LIVE CONTEXT. Enrolling CREATES NEW STATE, so
        # the ruling's second half applies — "commands creating new state
        # compare against the locked live context". The row this method is
        # about to write does not exist yet and has no durable side to
        # consult; the only honest question is whether the player is on this
        # coach's side RIGHT NOW, under the Season lock `_guard_mutable`
        # already holds. It is the SAME `ctx` the gate above accepted and
        # that the row's `position`/`team_id` are written from, so the side
        # authorized and the side recorded are provably one decision.
        #
        # Placed before the is_active/already-selected/already-enrolled
        # checks so an unauthorized coach learns nothing about a player who
        # is not theirs.
        self._require_authorized_team(
                                      authorized_team_id,
                                      target_ctx.target_team_id,
                                      "player")
        # Serialize the final activity decision with deactivation. The earlier
        # plain read is only for non-disclosing target resolution; this locked
        # row is the authority for the insert. Sample the transition time only
        # after that lock, so waiting across puck drop cannot persist a row
        # whose own enrolled_at is already outside the eligibility window.
        player = self.store.get_player_for_update(player_id)
        if player is None:
            raise NotFoundError(f"Player {player_id} not found.")
        decision_at = self.clock()
        target_ctx = self._require_substitute_target_context(
            game, player, target_team_id)
        ctx = target_ctx.source
        self._require_authorized_team(
            authorized_team_id, target_ctx.target_team_id, "player")

        # The cross-team surface is proactive: the player records willingness
        # before a vacancy exists.  The offer/accept transitions still require
        # an actual open slot.  Only published future games are advertised or
        # writable through this broader boundary.
        if target_ctx.cross_team:
            if not game.published or game.is_draft:
                raise NotEligibleError("This game has not been published yet.")
            if game.start_time is None or game.start_time <= decision_at:
                raise NotEligibleError("This game is no longer upcoming.")
        player = self._require_locked_active_player(player_id, player)

        # A player who already has any roster entry for this game — selected,
        # confirmed, or even backed out/removed — is not part of the
        # "not selected" substitute pool and may not enroll.
        entry = self.store.roster_entry_for_player(game_id, player_id)
        if entry is not None:
            raise AlreadySelectedError(
                f"{player.name} was already selected for this game."
            )

        existing = self._active_substitute_for_player(game_id, player_id)
        if existing is not None:
            raise ValidationError(f"{player.name} is already enrolled as a substitute.")

        sub = SubstituteEnrollment(
            id=self.store.next_id("sub"),
            game_id=game_id,
            player_id=player_id,
            # #205 review blocker 2: the season-scoped position for THIS
            # stint, taken off the SAME context the gate above accepted —
            # never a second, independently-resolved read.
            position=ctx.position,
            # PART A (owner ruling, PR #427, comment 5391127041): "When
            # `enroll_substitute` accepts the exact game membership
            # context, record that context's `team_id` on the enrollment in
            # the same transaction. For an ENROLLED row, that stored side —
            # not a later live membership lookup — is the Coach-
            # authorization authority for withdrawal."
            #
            # Free, and taken off the SAME `ctx` the eligibility gate above
            # accepted and that `position` is already read from — never a
            # second, independently-resolved read that could name a
            # different side than the one this enrollment was admitted on.
            #
            # Before this, `team_id` was written ONLY by `offer_substitute`
            # (migration 060), so an ENROLLED-but-never-OFFERED row carried
            # NO durable side at all and `withdraw_substitute` had nothing
            # to authorize a Coach against. Measured on this branch at head
            # 22bd6de, tri-store: after `enroll_substitute` the row read
            # back `status='enrolled' team_id=None`, and only
            # `offer_substitute` gave it a team.
            #
            # For same-team rows this may be refreshed by offer_substitute's
            # live-side snapshot. A cross-team row keeps this explicit target
            # for its whole lifecycle while its source is revalidated.
            team_id=target_ctx.target_team_id,
            source_membership_id=(
                ctx.membership.id if target_ctx.cross_team else None),
            source_team_id=(ctx.team_id if target_ctx.cross_team else None),
            status=SubstituteStatus.ENROLLED,
            enrolled_at=(decision_at if target_ctx.cross_team
                         else self.clock()),
        )
        self.store.add_substitute(sub)
        self._audit(
            game_id,
            AuditAction.SUBSTITUTE_ENROLLED,
            actor_id=actor_id,
            subject_player_id=player_id,
            detail={"target_team_id": target_ctx.target_team_id,
                    "cross_team": target_ctx.cross_team},
            team_id=target_ctx.target_team_id,
        )
        self._notify(
            game_id,
            NotificationType.SUBSTITUTE_ENROLLED,
            audience="coach",
            message="A player enrolled as substitute.",
            subject_player_id=player_id,
            team_id=target_ctx.target_team_id,
        )
        return sub

    @_transactional
    def withdraw_substitute(
        self, game_id: str, player_id: str, actor_id: Optional[str] = None,
        authorized_team_id: Optional[str] = None,
        expected_target_team_id: Optional[str] = None,
        require_target_identity: bool = False,
        allow_cross_team_response: bool = True,
    ) -> SubstituteEnrollment:
        game = self._require_game(game_id)
        game = self._guard_mutable(game)
        sub = self._require_active_enrollment(game_id, player_id)
        self._require_substitute_action_identity(
            sub, expected_target_team_id,
            require_target_identity=require_target_identity)
        is_cross_team = self._has_cross_team_provenance(sub)
        if is_cross_team and not allow_cross_team_response:
            raise NotAuthorizedError(
                "Cross-team substitute availability is controlled by the "
                "player or their verified guardian.")
        # COMPARAND: THE ROW'S DURABLE ATTRIBUTION, never a live lookup.
        # Withdrawing is a ROW-REMOVAL/RESPONSE command, so the ruling's
        # first half applies — and it is the case the ruling was written
        # around: "For an ENROLLED row, that stored side — not a later live
        # membership lookup — is the Coach-authorization authority for
        # withdrawal. Once OFFERED, the existing offer-owner snapshot
        # remains authoritative for that phase."
        #
        # ONE FIELD ANSWERS BOTH PHASES. `sub.team_id` is the enroll-time
        # snapshot while ENROLLED (Part A) and the offer-owner snapshot once
        # OFFERED (migration 060) — in both phases it is the side that owns
        # this row, which is exactly the question "is this row MINE?".
        #
        # LIVE RESOLUTION HERE WOULD BE WRONG IN BOTH DIRECTIONS, which is
        # why the ruling names it: it would refuse the HOME coach cleaning up
        # a HOME enrollment after the player transferred away (the row is
        # still HOME's to clean up — "This preserves the ordinary cleanup
        # path after transfer/inactivation"), and it would hand that same row
        # to the AWAY coach the player has just moved to ("prevents a row
        # from silently changing owners because eligibility later changed").
        #
        # A LEGACY NULL fails closed for a Coach with `attribution_missing`
        # and zero writes; player self-service (`authorized_team_id=None`)
        # withdraws its own enrollment exactly as before.
        self._require_authorized_team(authorized_team_id, sub.team_id,
                                      "substitute enrollment")
        was_offered = sub.status == SubstituteStatus.OFFERED
        if is_cross_team and was_offered:
            # Cross-team OFFERED rows have an explicit response contract.
            # Before the deadline callers must use accept/decline rather than
            # choosing the legacy WITHDRAWN terminal attribution through this
            # sibling endpoint. At/after the trusted deadline, expiry wins and
            # is durably recorded so the unique active row cannot strand the
            # player or the target team's queue.
            self.store.get_player_for_update(player_id)
            if self.cross_team_offer_deadline_passed(
                    game, sub, as_of=self.clock()):
                return self._expire_cross_team_offer(
                    game_id, sub, actor_id,
                    reason="withdraw_after_deadline")
            raise InvalidTransitionError(
                "Respond to a cross-team offer with accept or decline.")
        sub.status = SubstituteStatus.WITHDRAWN
        self.store.save_substitute(sub)
        self._audit(
            game_id,
            AuditAction.SUBSTITUTE_WITHDRAWN,
            actor_id=actor_id,
            subject_player_id=player_id,
            team_id=sub.team_id,
        )
        if was_offered:
            # An offered substitute backing out: notify the coach.
            self._notify(
                game_id,
                NotificationType.PLAYER_BACKED_OUT,
                audience="coach",
                message="An offered substitute withdrew. The slot is open again.",
                subject_player_id=player_id,
                team_id=sub.team_id,
            )
        return sub

    @_transactional
    def offer_substitute(
        self,
        game_id: str,
        player_id: str,
        actor_id: Optional[str] = None,
        offer_expires_at: Optional[datetime] = None,
        authorized_team_id: Optional[str] = None,
    ) -> SubstituteEnrollment:
        game = self._require_game(game_id)
        game = self._guard_mutable(game)
        sub = self._active_substitute_for_player(game_id, player_id)
        if sub is None or sub.status != SubstituteStatus.ENROLLED:
            raise InvalidTransitionError(
                "Only an enrolled substitute can be offered a slot."
            )
        # FIRST: does this row name a side AT ALL? Because the write below
        # MINTS `sub.team_id` from the live context, a NULL-owner row would
        # otherwise have an admitting side invented for it out of today's
        # membership — durably. A team-scoped actor is refused; an operator
        # (no constraint) can still repair the row. See
        # `_require_attributed_enrollment` for why this is a separate
        # question from the comparand check below and not a change to it.
        self._require_attributed_enrollment(sub, authorized_team_id)
        is_cross_team = self._has_cross_team_provenance(sub)
        decision_at = None
        effective_offer_expires_at = offer_expires_at
        if is_cross_team:
            # The stored TARGET is the coach-authorization authority for a
            # cross-team row.  Check it before reading whether the player is
            # active or whether their private SOURCE stint still exists;
            # otherwise the opponent coach can distinguish those source
            # states by comparing 403 with a named eligibility failure.
            self._require_authorized_team(
                authorized_team_id, sub.team_id, "player")
            # Sample the trusted decision time only after the Player lock is
            # held. A request that waited on concurrent membership/player work
            # across puck drop must not offer from its pre-wait timestamp.
            player = self.store.get_player_for_update(player_id)
            decision_at = self.clock()
            # Cross-team availability is advertised only for a published,
            # upcoming game. Re-check that public game-state contract under
            # the same transaction that writes OFFERED: an opt-in can outlive
            # publication or puck drop, but it must never become a dead offer
            # after either boundary moves underneath it.
            self._require_cross_team_game_actionable(
                game, as_of=decision_at)
            player = self._require_locked_active_player(player_id, player)
            self._require_cross_team_enrollment_context(game, player, sub)
            team_id = sub.team_id
            # #287 ruling 5: the cross-team response interval is server-owned
            # and half-open.  A request may not shorten or extend it; the
            # legacy caller-supplied expiry remains supported only for the
            # pre-existing same-team workflow below.
            if offer_expires_at is not None:
                raise ValidationError(
                    "Cross-team offer expiry is server-controlled.")
            effective_offer_expires_at = min(
                decision_at + self.cross_team_response_window,
                game.start_time)
            if effective_offer_expires_at <= decision_at:
                raise InvalidTransitionError(
                    "Offer expiry must be after it is issued.")
        else:
            player = self._require_active_player(player_id)
            # Legacy/same-team enrollment keeps the established live-side
            # retarget behaviour after a genuine membership move.
            team_id = self._require_membership_context(game, player).team_id
        # The same ``team_id`` drives authorization and slot accounting. For
        # a same-team row it is the locked live context and refreshes the
        # offer-owner snapshot below; for a cross-team row it is the durable
        # target whose exact source relationship was just revalidated.
        if not is_cross_team:
            self._require_authorized_team(
                authorized_team_id, team_id, "player")
        self._require_open_slot(game_id, sub.slot_type, team_id)
        sub.status = SubstituteStatus.OFFERED
        sub.offered_at = decision_at if is_cross_team else self.clock()
        sub.offer_expires_at = effective_offer_expires_at
        # #205 blocker 3: same-team offers snapshot the live side so a later
        # decline never re-resolves membership. Cross-team rows already carry
        # their explicitly selected target and must not be retargeted.
        if not is_cross_team:
            sub.team_id = team_id
        self.store.save_substitute(sub)
        self._audit(
            game_id,
            AuditAction.SUBSTITUTE_OFFERED,
            actor_id=actor_id,
            subject_player_id=player_id,
            team_id=team_id,
        )
        self._notify(
            game_id,
            NotificationType.SUBSTITUTE_OFFERED,
            audience="player",
            message="A game slot is available. Accept?",
            subject_player_id=player_id,
            team_id=team_id,
        )
        # Deliver to the offered player's feed + delivery queue (#112) so it
        # drives their Player Home unread count and any push/email channel.
        _push_notification(
            self.store, self.clock,
            NotificationKind.SUBSTITUTE_OFFERED, NotificationAudience.PLAYER,
            "Substitute offer",
            "A game slot is available — accept or decline it.",
            audience_ref=player_id, game_id=game_id)
        return sub

    def accept_substitute(
        self, game_id: str, player_id: str, actor_id: Optional[str] = None,
        authorized_team_id: Optional[str] = None,
        expected_target_team_id: Optional[str] = None,
        require_target_identity: bool = False,
        allow_cross_team_response: bool = True,
        response_source: str = "player",
    ) -> GameRosterEntry:
        # The whole read → validate → decide → write path runs inside ONE
        # transaction, so no concurrent request can cancel/lock the game or
        # change the offer between the checks and the write — restoring the
        # method-level atomicity the pre-#201 @_transactional gave.
        #
        # A lapsed offer is a durable EXPIRED transition that must persist even
        # though acceptance fails. We therefore do NOT raise inside the
        # transaction (that would roll the EXPIRED write back); the outcome is
        # recorded and InvalidTransitionError is raised only after the
        # transaction commits (#201).
        expired = False
        entry = None
        with self.store.transaction():
            game = self._require_game(game_id)
            game = self._guard_mutable(game)
            sub = self._active_substitute_for_player(game_id, player_id)
            if sub is None or sub.status != SubstituteStatus.OFFERED:
                raise InvalidTransitionError("No active offer to accept.")
            self._require_substitute_action_identity(
                sub, expected_target_team_id,
                require_target_identity=require_target_identity)
            is_cross_team = self._has_cross_team_provenance(sub)
            if is_cross_team and not allow_cross_team_response:
                raise NotAuthorizedError(
                    "Cross-team substitute offers are answered by the player "
                    "or their verified guardian. A coach can use the explicit "
                    "add-to-roster override instead.")
            # COMPARAND: THE LOCKED LIVE CONTEXT. Accepting CREATES NEW STATE
            # — it seats a roster row — and `_accept_offered_substitute`
            # below deliberately re-resolves live for exactly that reason
            # (see its comment, and decline_substitute's on why the two
            # transitions differ). For the legacy same-team Coach path, the
            # authorized live side and the side the seat counts on remain
            # identical. Cross-team accept/decline is player/guardian-owned;
            # its target Coach uses the explicit add-to-roster override.
            #
            # RESOLVED HERE, NOT INSIDE THE SEATING HELPER, because the
            # EXPIRED transition below is itself a WRITE and the ruling
            # requires the refusal to precede every write. Guarded on
            # `is not None` so the player/guardian paths — which carry no
            # Coach constraint — resolve nothing extra and keep the
            # expire-then-raise behaviour byte-for-byte: a lapsed offer must
            # still durably record EXPIRED for them even when membership has
            # ended, which an unconditional resolution here would prevent.
            if authorized_team_id is not None:
                # Same two questions, same order, same reason as
                # `offer_substitute`: a row that names no side is not any
                # side's to seat from, and accepting SEATS it. Inside the
                # same `is not None` guard so the player/guardian paths --
                # which must still durably record EXPIRED below -- resolve
                # nothing extra, exactly as before.
                self._require_attributed_enrollment(sub, authorized_team_id)
                if is_cross_team:
                    # Durable target ownership is decidable from the row and
                    # must be checked before either player activity or source
                    # provenance, neither of which an opponent coach may
                    # probe through this command.
                    self._require_authorized_team(
                        authorized_team_id, sub.team_id, "player")
                else:
                    player = self._require_active_player(player_id)
                    live_team_id = self._require_membership_context(
                        game, player).team_id
                    self._require_authorized_team(
                        authorized_team_id, live_team_id, "player")
            elif not is_cross_team:
                player = self._require_active_player(player_id)

            if is_cross_team:
                # Lock first, then sample exactly one trusted timestamp for
                # every cross-team deadline decision. At equality expiry wins
                # and is committed before the public API raises; this happens
                # before active/source revalidation so an offer cannot be
                # accepted or stranded at its deadline.
                locked_player = self.store.get_player_for_update(player_id)
                decision_at = self.clock()
                # A later schedule move may not extend an already-issued
                # offer, while an earlier move must close it at the new puck
                # drop.  Both clocks are therefore upper bounds.
                effective_deadline = self.cross_team_offer_deadline(
                    game, sub)
                if (effective_deadline is not None
                        and decision_at >= effective_deadline):
                    self._expire_cross_team_offer(
                        game_id, sub, actor_id,
                        reason="accept_after_deadline")
                    expired = True
                else:
                    player = self._require_locked_active_player(
                        player_id, locked_player)
                    self._require_cross_team_game_actionable(
                        game, as_of=decision_at)
                    self._require_cross_team_enrollment_context(
                        game, player, sub)
                    entry = self._accept_offered_substitute(
                        game, sub, player_id, actor_id,
                        accepted_at=decision_at,
                        response_source=response_source)
            else:
                # Preserve the established same-team contract exactly: game
                # start and explicit expiry remain independent legacy checks.
                if (game.start_time is not None
                        and self.clock() > game.start_time):
                    raise InvalidTransitionError(
                        "This game is no longer upcoming.")
                if (sub.offer_expires_at
                        and self.clock() > sub.offer_expires_at):
                    sub.status = SubstituteStatus.EXPIRED
                    self.store.save_substitute(sub)
                    expired = True
                else:
                    entry = self._accept_offered_substitute(
                        game, sub, player_id, actor_id)
        if expired:
            raise InvalidTransitionError("This substitute offer has expired.")
        return entry

    def _accept_offered_substitute(
        self, game, sub, player_id: str, actor_id: Optional[str],
        accepted_at: Optional[datetime] = None,
        response_source: Optional[str] = None,
    ) -> GameRosterEntry:
        # Runs inside accept_substitute's transaction (the game/sub were fetched
        # and validated within that same unit, so no interleaving is possible).
        # First-accepted-wins: the slot must still be open. The side the offer
        # counts against is the game-resolved team (#205 cutover) — and
        # resolution failing here (membership ended since the offer) fails
        # closed, same posture as the #270 deactivation gate.
        player = self.store.get_player(player_id)
        if self._has_cross_team_provenance(sub):
            self._require_cross_team_enrollment_context(game, player, sub)
            team_id = sub.team_id
        else:
            team_id = self._require_membership_context(game, player).team_id
        self._require_open_slot(game.id, sub.slot_type, team_id)
        game_id = game.id
        sub.status = SubstituteStatus.ACCEPTED
        sub.accepted_at = (
            accepted_at if accepted_at is not None else self.clock())
        self.store.save_substitute(sub)
        # #205 blocker 5 round 2: the row records the EXACT (side, bucket)
        # pair the gate one line above just accepted — never a second
        # resolution that could answer differently.
        entry = self._add_to_roster_entry(
            game, player_id, team_id, sub.position,
            seated_at=accepted_at)
        if response_source is not None:
            # Cross-team offers are accepted only through the signed-in
            # Player/verified-Guardian routes. Persist that consent as the
            # same GameAvailability fact the Home attendance control writes,
            # inside this acceptance transaction. The explicit coach
            # add-to-roster override never reaches this branch, so seating a
            # player cannot manufacture an answer on their behalf.
            existing = self.store.availability_for_player(game_id, player_id)
            self.store.upsert_availability(GameAvailability(
                id=(existing.id if existing is not None
                    else self.store.next_id("avail")),
                game_id=game_id,
                player_id=player_id,
                availability_status=AvailabilityStatus.AVAILABLE,
                response_source=response_source,
                responded_at=sub.accepted_at,
            ))
        self._audit(
            game_id,
            AuditAction.SUBSTITUTE_ACCEPTED,
            actor_id=actor_id,
            subject_player_id=player_id,
            team_id=team_id,
        )
        self._notify(
            game_id,
            NotificationType.SUBSTITUTE_ACCEPTED,
            audience="coach",
            message="Substitute accepted and was added to roster.",
            subject_player_id=player_id,
            team_id=team_id,
        )
        # Notify the team's coach via the feed + delivery queue (#112).
        _push_notification(
            self.store, self.clock,
            NotificationKind.SUBSTITUTE_ACCEPTED, NotificationAudience.COACH,
            "Substitute accepted",
            "A substitute accepted the open slot and joined the roster.",
            audience_ref=team_id, game_id=game_id)
        return entry

    @_transactional
    def decline_substitute(
        self, game_id: str, player_id: str, actor_id: Optional[str] = None,
        authorized_team_id: Optional[str] = None,
        expected_target_team_id: Optional[str] = None,
        require_target_identity: bool = False,
        allow_cross_team_response: bool = True,
    ) -> SubstituteEnrollment:
        game = self._require_game(game_id)
        game = self._guard_mutable(game)
        sub = self._active_substitute_for_player(game_id, player_id)
        if sub is None or sub.status != SubstituteStatus.OFFERED:
            raise InvalidTransitionError("No active offer to decline.")
        self._require_substitute_action_identity(
            sub, expected_target_team_id,
            require_target_identity=require_target_identity)
        is_cross_team = self._has_cross_team_provenance(sub)
        if is_cross_team and not allow_cross_team_response:
            raise NotAuthorizedError(
                "Cross-team substitute offers are answered by the player "
                "or their verified guardian.")
        # COMPARAND: `sub.team_id`, THE OFFER-OWNER SNAPSHOT — never a live
        # re-resolution. Declining is the TERMINAL RESPONSE to an offer, the
        # ruling's row-removal/response half, and this method's own standing
        # ruling (#205 blocker 3, round 3) already makes that snapshot
        # authoritative for the ENTIRE LIFETIME of the offer — it is the
        # value the coach notification below is addressed from.
        #
        # AUTHORIZING LIVE HERE WOULD RE-OPEN THE EXACT LEAK THAT RULING
        # CLOSED, one layer up: after a reassignment the live side is the
        # OPPONENT, so a live comparand would let the opponent's coach
        # decline — and refuse — an offer that was never theirs, while the
        # coach who actually issued it and must advance their queue would be
        # locked out of their own row. Same value, same reason, one authority.
        self._require_authorized_team(authorized_team_id, sub.team_id,
                                      "substitute offer")
        decision_at = None
        if is_cross_team:
            # Serialize the response timestamp behind the same Player lock as
            # accept. At equality the offer is EXPIRED, never DECLINED; return
            # the persisted row so the facade can report the refusal only
            # after this @_transactional method commits it.
            self.store.get_player_for_update(player_id)
            decision_at = self.clock()
            if self.cross_team_offer_deadline_passed(
                    game, sub, as_of=decision_at):
                return self._expire_cross_team_offer(
                    game_id, sub, actor_id,
                    reason="decline_after_deadline")
        sub.status = SubstituteStatus.DECLINED
        sub.declined_at = decision_at if is_cross_team else self.clock()
        self.store.save_substitute(sub)
        self._audit(
            game_id,
            AuditAction.SUBSTITUTE_DECLINED,
            actor_id=actor_id,
            subject_player_id=player_id,
            team_id=sub.team_id,
        )
        # Notify the team's coach so they can advance to the next candidate
        # (#112) — the pre-#112 decline path emitted no notification at all.
        #
        # THE OFFER OWNER IS THE AUDIENCE (#205 blocker 3, round-3 owner
        # ruling). ``sub.team_id`` — the side ``offer_substitute``'s own
        # ``_require_team_for_game`` validated this offer against, snapshotted
        # there (migration 060) — is AUTHORITATIVE for the ENTIRE LIFETIME of
        # the offer. It is never overridden by, and never yields precedence
        # to, a team resolved fresh at decline time.
        #
        # WHY ``accept`` IS NOT A PRECEDENT, and the symmetry argument is
        # wrong. ``_accept_offered_substitute`` deliberately re-resolves live
        # (``_require_team_for_game``) because acceptance REVALIDATES current
        # eligibility and seats the player in a roster slot that must be
        # counted against whichever side they are genuinely on NOW. Decline
        # revalidates nothing and seats nobody: it is the TERMINAL RESPONSE to
        # an offer that was already issued, and the audience of a response is
        # whoever OWNS the thing being responded to — the coach/team that made
        # the offer and is waiting to advance their queue. The two transitions
        # have different contracts, so mirroring accept's order here is not
        # "consistency", it is a leak.
        #
        # What live-first actually did (the defect this replaces, reproduced
        # tri-store on the previous head): offer an exhibition substitute
        # while on HOME (row snapshots HOME), reassign the player to AWAY,
        # decline the still-HOME offer — ``team_for_game(...) or sub.team_id``
        # resolved AWAY and short-circuited the snapshot, so AWAY's coach
        # received ``substitute_declined`` for an offer that was never theirs
        # (leaking the opponent's outstanding-offer state) and HOME's coach,
        # who is the one that must now offer the next candidate, was told
        # nothing at all.
        #
        # LEGACY ROWS. Migration 060 is additive with NO backfill, so a row
        # OFFERED by pre-060 code has ``team_id`` NULL and there is nothing to
        # read. No historically safe substitute exists to consult in that case
        # (investigated and ruled out: NOTHING written at offer time records
        # the offer-owner team — the offer-time push is PLAYER-audience with
        # ``audience_ref=player_id``, the SUBSTITUTE_OFFERED AuditLog row
        # carries an EMPTY detail, and no SetupAuditLog row is written at all;
        # replaying the permanent ``player.team_id`` pointer answers a team
        # that was never the offer owner on a LeagueSeason-bound game, and
        # membership history cannot be time-travelled either — backfilled
        # memberships carry no ``effective_from`` and no events by design, and
        # terminal rows do not record an ``effective_to``). A live
        # ``team_for_game`` lookup is emphatically NOT such a source; it is
        # the very substitution this rule forbids. So the decline COMMITS and
        # the targeted push is SUPPRESSED: never a guessed audience, and never
        # a ``None`` audience_ref handed to a COACH push — delivery.
        # recipient_ref's #60 fail-closed invariant stays intact (it would
        # raise and roll the whole @_transactional decline back, leaving the
        # player holding an undeclinable offer). Same "skip the targeted push,
        # keep the outcome" posture as the sibling in ``_back_out_entry``.
        audience_ref = sub.team_id
        if audience_ref is not None:
            _push_notification(
                self.store, self.clock,
                NotificationKind.SUBSTITUTE_DECLINED, NotificationAudience.COACH,
                "Substitute declined",
                "A substitute declined the offer — you can offer the next candidate.",
                audience_ref=audience_ref,
                game_id=game_id)
        return sub

    @_transactional
    def add_substitute_to_roster(
        self, game_id: str, player_id: str, actor_id: Optional[str] = None,
        authorized_team_id: Optional[str] = None,
    ) -> GameRosterEntry:
        """Coach override: offer + accept in one step (audited)."""
        game = self._require_game(game_id)
        game = self._guard_mutable(game)
        sub = self._active_substitute_for_player(game_id, player_id)
        if sub is None or not sub.status.is_active_enrollment:
            raise NotEnrolledError(
                "Player must be an enrolled/offered substitute to be added."
            )
        # Same two questions, same order, same reason as `offer_substitute`:
        # this is the coach-override SEATING of an existing enrollment, and a
        # row that names no side is not any side's to seat from.
        self._require_attributed_enrollment(sub, authorized_team_id)
        # ONE resolution, held in a name, so the gate below and the durable
        # attribution written into the row are provably the same decision
        # (#205 blocker 5 round 2) rather than two independent lookups.
        is_cross_team = self._has_cross_team_provenance(sub)
        decision_at = None
        if is_cross_team:
            # As on offer/coach-override, authorize the stored target before
            # inspecting the source player's active/provenance state.
            self._require_authorized_team(
                authorized_team_id, sub.team_id, "player")
            player = self.store.get_player_for_update(player_id)
            decision_at = self.clock()
            if (sub.status == SubstituteStatus.OFFERED
                    and self.cross_team_offer_deadline_passed(
                        game, sub, as_of=decision_at)):
                return self._expire_cross_team_offer(
                    game_id, sub, actor_id,
                    reason="override_after_deadline")
            self._require_cross_team_game_actionable(
                game, as_of=decision_at)
            player = self._require_locked_active_player(player_id, player)
            target_ctx = self._require_cross_team_enrollment_context(
                game, player, sub)
            ctx = target_ctx.source
            team_id = sub.team_id
        else:
            player = self._require_active_player(player_id)
            ctx = self._require_membership_context(game, player)
            team_id = ctx.team_id
        # The coach may seat only into the resolved target side. For legacy
        # same-team rows that target is ``ctx.team_id``; for cross-team rows
        # it is the durable enrollment target after exact source revalidation.
        if not is_cross_team:
            self._require_authorized_team(authorized_team_id, team_id,
                                          "player")
        self._require_open_slot(game_id, sub.slot_type, team_id)
        sub.status = SubstituteStatus.ACCEPTED
        sub.accepted_at = decision_at if is_cross_team else self.clock()
        self.store.save_substitute(sub)
        entry = self._add_to_roster_entry(game, player_id, team_id,
                                          sub.position,
                                          seated_at=decision_at)
        self._audit(
            game_id,
            AuditAction.SUBSTITUTE_ADDED_TO_ROSTER,
            actor_id=actor_id,
            subject_player_id=player_id,
            detail={"override": True},
            team_id=team_id,
        )
        self._notify(
            game_id,
            NotificationType.SUBSTITUTE_ACCEPTED,
            audience="coach",
            message="Substitute accepted and was added to roster.",
            subject_player_id=player_id,
            team_id=team_id,
        )
        return entry

    def _expire_cross_team_offer(
        self, game_id: str, sub: SubstituteEnrollment,
        actor_id: Optional[str], *, reason: str,
    ) -> SubstituteEnrollment:
        """Persist one auditable cross-team OFFERED -> EXPIRED transition.

        Callers invoke this only inside their existing transaction and after
        the Player lock/time decision. Retrying cannot duplicate the audit:
        EXPIRED is terminal, so no response entry point can resolve it as the
        active OFFERED row again.
        """
        sub.status = SubstituteStatus.EXPIRED
        self.store.save_substitute(sub)
        self._audit(
            game_id,
            AuditAction.SUBSTITUTE_EXPIRED,
            actor_id=actor_id,
            subject_player_id=sub.player_id,
            detail={"reason": reason, "target_team_id": sub.team_id},
            team_id=sub.team_id,
        )
        return sub

    def _add_to_roster_entry(
        self, game: Game, player_id: str, team_side: str, position: Position,
        seated_at: Optional[datetime] = None,
    ) -> GameRosterEntry:
        """Seat a substitute, recording the DURABLE attribution it occupies.

        ``team_side``/``position`` are NOT resolved here — they are handed
        down by the caller, and they must be THE PAIR that caller just fed
        to :meth:`_require_open_slot` (the validated target side and the
        ENROLLMENT's source-season position). Resolving them a second time inside
        this method would reintroduce exactly the two-reads-that-can-
        disagree defect #205 blocker 2 closed: the slot the gate checked and
        the slot the row is counted in have to be one slot, provably, not
        two answers that usually agree.
        """
        now = seated_at if seated_at is not None else self.clock()
        existing = self.store.roster_entry_for_player(game.id, player_id)
        if existing:
            existing.roster_role = RosterRole.SUBSTITUTE_ADDED
            existing.selection_source = SelectionSource.SUBSTITUTE_POOL
            existing.status = RosterEntryStatus.ACCEPTED
            existing.updated_at = now
            existing.team_side = team_side
            existing.seated_position = position
            return self.store.save_roster_entry(existing)
        entry = GameRosterEntry(
            id=self.store.next_id("entry"),
            game_id=game.id,
            player_id=player_id,
            roster_role=RosterRole.SUBSTITUTE_ADDED,
            selection_source=SelectionSource.SUBSTITUTE_POOL,
            status=RosterEntryStatus.ACCEPTED,
            selected_at=now,
            updated_at=now,
            team_side=team_side,
            seated_position=position,
        )
        return self.store.add_roster_entry(entry)

    def _require_active_enrollment(
        self, game_id: str, player_id: str
    ) -> SubstituteEnrollment:
        sub = self._active_substitute_for_player(game_id, player_id)
        if sub is None:
            raise NotEnrolledError("Player is not currently enrolled as a substitute.")
        return sub

    def _require_substitute_action_identity(
        self,
        sub: SubstituteEnrollment,
        expected_target_team_id: Optional[str],
        *,
        require_target_identity: bool,
    ) -> None:
        """Bind a player/guardian response to the cross-team choice shown.

        ``game_id`` and ``player_id`` are not a complete identity once a
        terminal Team 4 row can be followed by an active Team 5 row. Scoped
        browser actions therefore carry the target they rendered and compare
        it to the active row inside the mutation transaction. Legacy
        same-team clients keep their established empty request body.
        """
        if not require_target_identity:
            return
        marked_cross = self._has_cross_team_provenance(sub)
        exact_cross = self._is_cross_team_enrollment(sub)
        if marked_cross:
            if (not exact_cross
                    or expected_target_team_id is None
                    or expected_target_team_id != sub.team_id):
                raise InvalidTransitionError(
                    "This substitute choice has changed. Refresh and try again.")
            return
        if expected_target_team_id is not None:
            raise InvalidTransitionError(
                "This substitute choice has changed. Refresh and try again.")

    def _active_substitute_for_player(
        self, game_id: str, player_id: str,
    ) -> Optional[SubstituteEnrollment]:
        """The one live enrollment at ``(game, player)``, or ``None``.

        Terminal attempts remain history, so the old first-row lookup cannot
        answer this question after a player withdraws and opts in again.
        Multiple live rows are corrupt/ambiguous and fail closed.
        """
        rows = [s for s in self.store.substitute_enrollments_for_player(
                    player_id)
                if s.game_id == game_id
                and s.status.is_active_enrollment]
        if len(rows) > 1:
            raise IntegrityConflictError(
                "Player has more than one active substitute enrollment for "
                "this game.",
                details={"reason": "active_substitute_conflict",
                         "game_id": game_id, "player_id": player_id})
        return rows[0] if rows else None

    def substitute_action_team_for_coach_scope(
        self, game, player, *, durable_owner: bool = False,
        allow_cross_team: bool = True,
    ) -> Optional[str]:
        """Resolve the side a coach may preflight for a substitute action.

        A cross-team volunteer is intentionally not a member of either game
        side, so ``team_for_game`` cannot identify the coach who owns the
        resulting enrollment.  Once that relationship exists, its durable
        target is the only correct preflight comparand.  Legacy/same-team
        rows continue to use the live game membership exactly as before.

        This is only the HTTP fast-denial projection.  Every command receives
        the coach's scoped team and revalidates the same ownership under its
        transaction and locks before writing.
        """
        if game is None or player is None:
            return None
        sub = self._active_substitute_for_player(game.id, player.id)
        if sub is not None:
            sides = {game.home_team_id, game.away_team_id}
            marked_cross = self._has_cross_team_provenance(sub)
            # Any provenance marker opts the row into the strict shape. A
            # half-written pair cannot fall back to live membership and
            # accidentally acquire an owner.
            if marked_cross and not self._is_cross_team_enrollment(sub):
                return None
            if marked_cross and not allow_cross_team:
                return None
            # Removal/response commands that permit this row use its durable
            # owner. Cross-team coach decline/withdraw has already failed the
            # allow_cross_team gate above; create-state actions still use the
            # durable target for an allowed cross-team volunteer.
            if durable_owner or marked_cross:
                return (sub.team_id if sub.team_id in sides else None)
        return self.team_for_game(game, player)

    def roster_row_team_for_coach_scope(
        self, game, player,
    ) -> Optional[str]:
        """Resolve a response/removal action through durable row ownership."""
        if game is None or player is None:
            return None
        entry = self.store.roster_entry_for_player(game.id, player.id)
        if entry is None:
            return self.team_for_game(game, player)
        if entry.team_side not in {game.home_team_id, game.away_team_id}:
            return None
        return entry.team_side

    def active_substitute_snapshot(
        self, player_id: str,
    ) -> Dict[str, SubstituteEnrollment]:
        """One coherent active-enrollment partition for a Player Home read.

        Opportunity and offer sections must consume the same observation. If
        a response commits between two independent scans, the page may be a
        moment behind but must never omit both states or dereference a missing
        second lookup. Mutations always revalidate current state separately.
        """
        grouped: Dict[str, List[SubstituteEnrollment]] = {}
        for row in self.store.substitute_enrollments_for_player(player_id):
            if row.status.is_active_enrollment:
                grouped.setdefault(row.game_id, []).append(row)
        conflicts = [game_id for game_id, rows in grouped.items()
                     if len(rows) > 1]
        if conflicts:
            game_id = sorted(conflicts)[0]
            raise IntegrityConflictError(
                "Player has more than one active substitute enrollment for "
                "this game.",
                details={"reason": "active_substitute_conflict",
                         "game_id": game_id, "player_id": player_id})
        return {game_id: rows[0] for game_id, rows in grouped.items()}

    def _lineup_substitutes(self, game_id: str):
        """One deterministic enrollment per player for lineup rendering.

        Active ENROLLED/OFFERED state always wins over terminal history.  A
        player with two active rows is corrupt and must not be assigned to a
        side by store iteration order.  When only history remains, the newest
        enrollment is selected by its persisted timestamp and stable id so
        Memory, SQLite, and PostgreSQL render the same status badge.
        """
        grouped = {}
        for sub in self.store.substitutes_for_game(game_id):
            grouped.setdefault(sub.player_id, []).append(sub)
        selected = {}
        for player_id, rows in grouped.items():
            active = [row for row in rows
                      if row.status.is_active_enrollment]
            if len(active) > 1:
                raise IntegrityConflictError(
                    "Player has more than one active substitute enrollment "
                    "for this game.",
                    details={"reason": "active_substitute_conflict",
                             "game_id": game_id,
                             "player_id": player_id})
            selected[player_id] = (
                active[0] if active else max(
                    rows,
                    key=lambda row: (
                        row.enrolled_at.isoformat()
                        if row.enrolled_at is not None else "",
                        row.id)))
        return selected

    @staticmethod
    def _is_cross_team_enrollment(sub: SubstituteEnrollment) -> bool:
        return (sub.source_membership_id is not None
                and sub.source_team_id is not None)

    @staticmethod
    def _has_cross_team_provenance(sub: SubstituteEnrollment) -> bool:
        return (sub.source_membership_id is not None
                or sub.source_team_id is not None)

    def _cross_team_enrollment_context(
        self, game, player, sub: SubstituteEnrollment,
    ) -> Optional["SubstituteTargetContext"]:
        """Revalidate a marked cross-team enrollment's exact provenance."""
        if (not self._is_cross_team_enrollment(sub)
                or sub.team_id is None):
            return None
        return self.resolve_substitute_target_context(
            game, player, sub.team_id,
            source_membership_id=sub.source_membership_id,
            source_team_id=sub.source_team_id)

    def _require_cross_team_enrollment_context(
        self, game, player, sub: SubstituteEnrollment,
    ) -> "SubstituteTargetContext":
        ctx = self._cross_team_enrollment_context(game, player, sub)
        if ctx is None:
            raise NotEligibleError(
                "This cross-team substitute enrollment is no longer "
                "eligible in its original league season and division.")
        return ctx

    def _require_open_slot(
        self, game_id: str, slot_type: SlotType, team_id: str
    ) -> None:
        summaries = self._slot_summaries(game_id, team_id)
        summary = summaries[slot_type]
        if summary.open_count <= 0:
            raise SlotAlreadyFilledError(
                f"The {slot_type.value} slot is already filled."
            )

    # ====================================================================
    # game-scoped team resolution (#205 substitute eligibility cutover)
    # ====================================================================
    # An ACTIVE membership is the authoritative stint; AFFILIATE is the
    # governed call-up exception the #205 model defines (secondary
    # participation with exactly that Team). applicant/inactive/injured hold
    # no current participation, and terminal rows are immutable history —
    # none of them grants eligibility.
    _ELIGIBLE_MEMBERSHIP_STATUSES = (MembershipStatus.ACTIVE,
                                     MembershipStatus.AFFILIATE)

    # The order in which a NON-participating membership at the right key is
    # REPORTED (PR #427) — never an eligibility order, only a tie-break for
    # "which of this player's parked rows explains the skip". Terminal first
    # (the stint genuinely ended, and that is the more final fact about the
    # player), then the open-but-not-authoritative states. Together with
    # ``_ELIGIBLE_MEMBERSHIP_STATUSES`` this must partition ``MembershipStatus``
    # exactly — ``MembershipReasonsCoverEveryStatus`` fails if a new value
    # lands in neither tuple, so a status nobody has classified can never be
    # silently reported (or silently seated).
    _INELIGIBLE_MEMBERSHIP_STATUSES = (MembershipStatus.TRANSFERRED,
                                       MembershipStatus.RELEASED,
                                       MembershipStatus.INACTIVE,
                                       MembershipStatus.INJURED,
                                       MembershipStatus.APPLICANT)

    def resolve_membership_context(
        self, game, player
    ) -> Optional["GameMembershipContext"]:
        """THE #205 eligibility primitive: the ONE coherent
        :class:`GameMembershipContext` that makes ``player`` eligible for
        ``game``, or ``None``.

        THE EXACT LeagueSeason, NEVER A DERIVED SEASON. Matching is on
        ``game.league_season_id`` directly. ``LeagueSeason``'s own uniqueness
        is ``(league_id, season_id)``, one row per League *per* Season: a
        Season with two Leagues ("Elite" and "House" both running in "Fall
        2026") is the ordinary shape, not an edge case, and produces two
        different ``LeagueSeason`` rows that share one ``season_id``.
        Deriving the game's ``season_id`` and matching memberships on that
        column alone would therefore accept a membership scoped to a SIBLING
        LeagueSeason of the same Season.

        EXACTNESS IS NECESSARY AND NOT SUFFICIENT (#205 review blocker 2,
        owner comment 5368386042). The previous version of this resolver
        stopped there: it checked that the ``LeagueSeason`` row EXISTED and
        that the membership named that id, that team and an eligible status.
        It never re-checked the PARTICIPATION the membership hangs off. Every
        such check lived on the WRITE side only
        (``SetupService._assert_membership_program_spine`` /
        ``_assert_membership_spine_valid``), so a restored backup, a direct/
        bulk writer, or a parent-mutation race left a membership that still
        granted participation after its parent participation had ENDED —
        reproduced tri-store by deactivating the HOME
        ``SeasonTeamRegistration`` at the store and watching resolve, enroll,
        offer and accept all succeed.

        WHAT IS VALIDATED, all of it, before ANY context is returned:

        * **membership/player identity** — the ``Player`` row still exists;
        * **the exact LeagueSeason AND the denormalized Season** — the
          ``LeagueSeason`` row exists and ``membership.season_id`` still
          agrees with ``LeagueSeason.season_id`` under
          :func:`~.membership_spine.missing_or_unequal`. That column is
          service-enforced equal at BIRTH only (it exists so migration 059's
          ``ux_srm_active_player_season`` can hold "one ACTIVE membership per
          (player, Season)" without a join); nothing re-checked it after, and
          on SQL both columns are FK-constrained to EXISTING rows without
          ever being constrained to the SAME Season;
        * **the participating Team, the Team-League-Season/Program spine and
          a CURRENT ACTIVE SeasonTeamRegistration** — delegated whole to
          :func:`~.membership_spine.side_spine_break`, the single predicate
          the write-time guards now share, so a read-time refusal and a
          write-time refusal can never disagree about what a broken key is.

        The spine is validated BEFORE precedence is applied, not after: a
        membership on a side whose participation has ended was never a
        candidate, so it must not shadow a still-valid membership on the
        other side. Within the surviving candidates ACTIVE outranks AFFILIATE
        (the governed call-up exception), home side before away, for
        determinism in the pathological both-sides case.

        UNBOUND GAMES ARE UNCHANGED. A game with NO LeagueSeason binding
        (exhibitions by design, plus unbound legacy rows) has no membership
        to resolve, so the permanent ``player.team_id`` pointer is the only
        source and the returned context carries ``membership=None`` — exactly
        pre-#205 behavior, and the reason
        ``UnboundGamesKeepThePermanentGate`` keeps passing. A BOUND game
        whose context fails returns ``None`` and NEVER falls back to the
        permanent team or position.
        """
        ctx, _reason = self._resolve_context_with_reason(game, player)
        return ctx

    def resolve_substitute_target_context(
        self, game, player, target_team_id: Optional[str] = None,
        source_membership_id: Optional[str] = None,
        source_team_id: Optional[str] = None,
    ) -> Optional["SubstituteTargetContext"]:
        """Resolve the player's source membership and requested game side.

        The ordinary game-membership resolver deliberately remains a
        participating-side authority for private game reads.  This separate
        projection is the narrow #287 borrowing rule: a player may volunteer
        for another team only inside the exact same LeagueSeason and only
        when the source and target registrations share a non-null Division.
        A player may never borrow into a game involving their own source team.
        """
        if game is None or player is None:
            return None
        if bool(source_membership_id) != bool(source_team_id):
            return None
        sides = tuple(t for t in (game.home_team_id, game.away_team_id) if t)
        if not sides:
            return None

        direct = self.resolve_membership_context(game, player)
        if target_team_id is None:
            if direct is None:
                return None
            return SubstituteTargetContext(
                source=direct, target_team_id=direct.team_id,
                target_team=direct.team,
                target_registration=direct.registration)

        if target_team_id not in sides:
            return None
        if direct is not None:
            # A persisted cross-team enrollment is bound to its original
            # source stint. If the athlete later joins a participating side,
            # that new live context must not resurrect or retarget the old
            # opt-in by taking this same-team shortcut.
            if source_membership_id is not None:
                return None
            # Same-side calls preserve the existing workflow.  The opposite
            # side is never a borrowing target in the player's own game.
            if direct.team_id != target_team_id:
                return None
            return SubstituteTargetContext(
                source=direct, target_team_id=target_team_id,
                target_team=direct.team,
                target_registration=direct.registration)

        # Cross-team borrowing is a seasonal rule.  Exhibitions and legacy
        # unbound games have no competition boundary to prove and stay shut.
        if not season_guard.game_is_league_season_bound(game):
            return None
        ls = self.store.get_league_season(game.league_season_id)
        if ls is None or game.season_id != ls.season_id:
            return None
        if self.store.get_player(player.id) is None:
            return None

        target_reason, target_spine = side_spine_break(
            self.store, ls, target_team_id)
        if target_reason is not None or target_spine is None:
            return None
        target_division = target_spine.registration.division_id
        if target_division is None:
            return None
        division = self.store.get_division(target_division)
        if (division is None
                or division.league_season_id != ls.id):
            return None

        # Derive the allowed source-team axis from the LeagueSeason's active
        # registrations, then delegate the membership/status/spine decision to
        # the exact #205 resolver.  This avoids a second implementation of
        # what makes a seasonal membership live.
        source_team_ids = tuple(sorted({
            registration.team_id
            for registration in
            self.store.registrations_for_league_season(ls.id)
            if (registration.active
                and registration.division_id == target_division
                and registration.team_id not in sides)
        }))
        source_ctx, _reason = self._resolve_context_with_reason(
            game, player, eligible_team_ids=source_team_ids)
        if source_ctx is None:
            return None
        if (source_membership_id is not None
                and (source_ctx.membership is None
                     or source_ctx.membership.id != source_membership_id
                     or source_ctx.team_id != source_team_id)):
            return None
        return SubstituteTargetContext(
            source=source_ctx, target_team_id=target_team_id,
            target_team=target_spine.team,
            target_registration=target_spine.registration)

    def _require_substitute_target_context(
        self, game, player, target_team_id: Optional[str] = None,
        source_membership_id: Optional[str] = None,
        source_team_id: Optional[str] = None,
    ) -> "SubstituteTargetContext":
        ctx = self.resolve_substitute_target_context(
            game, player, target_team_id,
            source_membership_id=source_membership_id,
            source_team_id=source_team_id)
        if ctx is None:
            name = player.name if player is not None else "Player"
            raise NotEligibleError(
                f"{name} is not eligible to substitute for that team. "
                "Cross-team substitutes must be active in the same league "
                "season and division, and cannot join their own game.")
        return ctx

    def membership_spine_break_reason(self, game, player) -> Optional[str]:
        """The STABLE reason string naming the FIRST broken spine edge for
        the membership ``player`` would otherwise resolve on for ``game``, or
        ``None`` when a context resolves cleanly.

        A DIAGNOSTIC view of the very same resolution — it shares
        :meth:`_resolve_context_with_reason` with
        :meth:`resolve_membership_context`, so it can never disagree with the
        gate and the gate never consults it. It exists so each spine leg is
        falsifiable ON ITS OWN: several legs correctly BACKSTOP one another
        (a duplicated registration key makes
        ``exact_registration_or_conflict`` return no row, so the
        "not registered" check would close the gate even if the conflict
        check were deleted), and without a reason to assert, deleting the
        redundant one would leave every test still passing."""
        _ctx, reason = self._resolve_context_with_reason(game, player)
        return reason

    def seating_block_reason(self, game, player) -> Optional[str]:
        """The STABLE reason ``player`` may not be SEATED on ``game`` right
        now, or ``None`` — the whole eligibility answer for one candidate, in
        one non-raising call (PR #427).

        :meth:`membership_spine_break_reason` answers only the MEMBERSHIP
        question. Seating asks one more: ``select_roster`` refuses a
        deactivated ``Player`` (#270) after the context resolves, and that
        refusal needs a reason of its own or the #427 ruling's "a stable
        reason for each skip" has a hole in it for the single most ordinary
        shape (a player who left the club entirely).

        THE ORDER IS THE GATE'S ORDER, deliberately. ``select_roster`` tests
        the context FIRST and ``player.is_active`` second, so a candidate
        failing both is reported under the context reason — the reason names
        the gate that would actually refuse, not whichever check this method
        happens to run first. Deactivation is NOT folded into
        ``_resolve_context_with_reason``: that resolver also feeds
        ``compute_roster_status``/``_slot_summaries``/the private reads, and
        closing it on ``is_active`` would newly hide rows that are visible
        today — a behaviour change this ruling does not authorize.

        PURE and NON-RAISING, which is what lets
        :meth:`_partition_candidates` call it to NAME a skip *inside* the
        batch's transaction. Non-raising is the point: catching
        ``NotEligibleError`` there instead would have nothing to unwind to
        (``transaction()`` is reentrant with no savepoints) and would leave
        a PostgreSQL connection in InFailedSqlTransaction — see the batch
        seating section header."""
        _ctx, reason = self._resolve_context_with_reason(game, player)
        if reason is not None:
            return reason
        if player is not None and not player.is_active:
            return PLAYER_INACTIVE
        return None

    def _resolve_context_with_reason(
        self, game, player, eligible_team_ids: Optional[Tuple[str, ...]] = None,
    ):
        """``(context, None)`` or ``(None, reason)`` — the ONE resolution
        both public forms are views of.

        The candidate loop is a CLASSIFIER, not a filter (PR #427). It used
        to ``continue`` past every membership row that was "simply not about
        this game" without recording anything, so a player who had
        TRANSFERRED, whose membership had gone INACTIVE, who was registered
        in a DIFFERENT LeagueSeason, or who had no membership at all were all
        reported identically as ``no_eligible_membership``. The owner's #427
        ruling requires each skipped player to carry a stable reason an
        operator can act on, so each of those discards is now NAMED — see the
        reason block in ``membership_spine``. The GATE is unchanged: the same
        rows are candidates, the same precedence picks among them, and the
        same contexts are returned. Only the ``None`` answer got more
        specific."""
        if game is None or player is None:
            return None, NO_ELIGIBLE_MEMBERSHIP
        sides = (tuple(t for t in (game.home_team_id, game.away_team_id) if t)
                 if eligible_team_ids is None else tuple(eligible_team_ids))
        if not sides:
            return None, NO_ELIGIBLE_MEMBERSHIP
        if not season_guard.game_is_league_season_bound(game):
            if eligible_team_ids is not None:
                return None, NO_ELIGIBLE_MEMBERSHIP
            # An UNBOUND game has no membership rows in play at all, so the
            # narrowed NO_ELIGIBLE_MEMBERSHIP ("nothing to resolve") is
            # exactly right for a permanent pointer naming another team.
            if player.team_id not in sides:
                return None, NO_ELIGIBLE_MEMBERSHIP
            return GameMembershipContext(
                game_id=game.id, player=player, team_id=player.team_id,
                position=player.position, membership=None), None
        # A dangling pointer (the LeagueSeason itself deleted/never existed
        # while the game still names it) fails closed rather than silently
        # matching any membership that happens to carry the same id string.
        ls = self.store.get_league_season(game.league_season_id)
        if ls is None:
            return None, LEAGUE_SEASON_MISSING
        # THE GAME'S OWN IDENTITY, before any membership is considered (PR #427
        # blocker 5379031499). ``ls.season_id`` is the competition this game
        # belongs to; ``game.season_id`` is a nullable, FK-less denormalization
        # of it. When the two disagree the game's identity is not trustworthy,
        # so NOTHING resolves on it — the same conclusion ``context_scope``'s
        # read-only Official gate already reaches for the identical drift.
        #
        # This is the READ half of the fix and it is deliberately not a copy of
        # the WRITE half: ``season_guard.guard_game_season`` additionally LOCKS
        # the canonical Season and refuses an ARCHIVED one, because a write must
        # serialize against ``archive_season``. A read must not — an archived
        # Season is read-only, not invisible, and closing this resolver on
        # archive state would blank out every historical roster view. So the
        # resolver checks IDENTITY only, and the write guard adds the lock and
        # the lifecycle check on top of it.
        #
        # Unconditional equality, matching the write guard: ``None`` is a
        # disagreement, not an exemption. Without this the whole read surface
        # (substitute_block_reason -> the opportunity list, the addable-player
        # list, compute_roster_status) answered from a context whose Season was
        # LS1's while the game claimed a sibling's, or none at all.
        if game.season_id != ls.season_id:
            return None, GAME_LEAGUE_SEASON_MISMATCH
        # Identity: the membership's Player must still exist. Callers hand in
        # a Player OBJECT, which a long-lived caller (or a batch that read it
        # a moment ago) may still hold across a delete; the ROW is the
        # authority, not the object in hand.
        if self.store.get_player(player.id) is None:
            return None, PLAYER_MISSING
        spines: Dict[str, Tuple[Optional[str], object]] = {}
        valid: Dict[MembershipStatus, Dict[str, SeasonRosterMembership]] = {}
        raw: Dict[MembershipStatus, Dict[str, SeasonRosterMembership]] = {}
        parked: Dict[MembershipStatus, Dict[str, SeasonRosterMembership]] = {}
        why: Dict[str, str] = {}
        any_row = False          # the player holds ANY membership at all
        any_here = False         # …and at least one at THIS LeagueSeason
        for m in self.store.memberships_for_player(player.id):
            # The membership's OWN keys first: the exact LeagueSeason, one of
            # this game's two sides, and a status that grants current
            # participation. A row failing these is not a broken spine — but
            # it is not nothing either, so each discard is CLASSIFIED rather
            # than dropped (#427: a skipped player owes the operator a
            # reason).
            any_row = True
            if m.league_season_id != game.league_season_id:
                continue
            any_here = True
            if m.team_id not in sides:
                continue
            if m.status not in self._ELIGIBLE_MEMBERSHIP_STATUSES:
                self._keep_lowest_id(parked, m)
                continue
            # ``raw`` collects only rows that FAILED, which is not a
            # narrowing: it is read exactly once, after the ``valid`` pick
            # below has already returned for any player holding a row that
            # succeeded, so at that point every right-keyed,
            # participation-granting row carries a reason.
            if missing_or_unequal(m.season_id, ls.season_id):
                row_reason = DENORMALIZED_SEASON_MISMATCH
            else:
                row_reason, _spine = self._side_spine(spines, ls, m.team_id)
            if row_reason is None:
                self._keep_lowest_id(valid, m)
                continue
            why[m.id] = row_reason
            self._keep_best_reason(raw, m, row_reason, why)
        # THE SEATING PICK. Precedence runs over the SPINE-VALIDATED
        # candidates: a membership on a side whose participation has ended
        # was never a candidate, so it must not shadow a still-valid
        # membership on the other side. ACTIVE-over-AFFILIATE then
        # home-before-away, and this is the ONLY pick on this path whose
        # answer becomes a CONTEXT — see ``_pick_eligible_membership`` for
        # why that order is load-bearing and must not be retuned.
        m = self._pick_eligible_membership(valid, sides)
        if m is not None:
            return self._context_for(game, player, ls, spines[m.team_id][1],
                                     m), None
        # MOST SPECIFIC FIRST. The row that came CLOSEST to seating the
        # player names the reason: a broken spine on a right-keyed,
        # participation-granting row outranks a parked row at the same key,
        # which outranks a row on the wrong bench, which outranks a row in
        # another competition, which outranks having no rows at all. Each
        # step is decided by a deterministic pick, never by store iteration
        # order.
        #
        # THE REASON-NAMING PICK, and it is deliberately NOT the seating
        # pick's order. Every row in ``raw`` is equally close to seating the
        # player — all of them are right-keyed, participation-granting rows
        # on a side of this game — so they differ ONLY in WHY they failed,
        # and "which reason wins when several apply" is precisely what
        # ``SKIP_REASON_PRECEDENCE`` answers. Ranking these by
        # ACTIVE-before-AFFILIATE/home-before-away would answer a question
        # nobody asked: the player is not being seated on this path, so
        # which STINT names the failure is uninteresting next to which
        # FAILURE is named.
        #
        # ``why[m.id]`` is a direct subscript, not a defaulted ``get``: the
        # pick above has already read the reason of every row it considered,
        # so a survivor without one is unreachable and would be a bug in the
        # bucket rather than a player owed a fallback string.
        m = self._pick_reason_membership(raw, sides, why)
        if m is not None:
            return None, why[m.id]
        m = self._pick_membership(parked, sides,
                                  self._INELIGIBLE_MEMBERSHIP_STATUSES)
        if m is not None:
            return None, status_ineligible_reason(m.status)
        if any_here:
            return None, MEMBERSHIP_OTHER_TEAM
        if any_row:
            return None, MEMBERSHIP_OTHER_LEAGUE_SEASON
        return None, NO_ELIGIBLE_MEMBERSHIP

    def _side_spine(self, cache, ls, team_id):
        """``(reason, SideSpine)`` for one of the game's sides — the spine is
        ``None`` exactly when the reason is not. Memoized per call because a
        game has at most two sides and every candidate membership on a side
        asks the same question, so the Team/League/Season/registration reads
        are paid AT MOST TWICE per resolution, never once per membership
        row."""
        if team_id not in cache:
            cache[team_id] = side_spine_break(self.store, ls, team_id)
        return cache[team_id]

    @staticmethod
    def _context_for(game, player, ls, spine, m) -> "GameMembershipContext":
        return GameMembershipContext(
            game_id=game.id, player=player, team_id=m.team_id,
            # The SEASON-scoped position for THIS stint, not the permanent
            # Player row. A legacy/backfilled membership may carry no
            # position of its own; substituting a wrong season-scoped value
            # would be worse than the permanent one, and every
            # substitute-facing read needs SOME position to compute a slot
            # type from. That is a fallback WITHIN a valid context — never
            # the fallback a FAILED context must not have.
            position=(m.position if m.position is not None
                      else player.position),
            membership=m, league_season=ls, season=spine.season,
            team=spine.team, registration=spine.registration)

    def resolve_membership(
        self, game, player
    ) -> Optional[SeasonRosterMembership]:
        """The ``SeasonRosterMembership`` of :meth:`resolve_membership_
        context`, or ``None`` — the row-shaped view of the same single
        resolution, kept for callers that want the membership itself.
        ``None`` for an unbound game (whose context carries no membership)
        exactly as before."""
        ctx = self.resolve_membership_context(game, player)
        return ctx.membership if ctx is not None else None

    @classmethod
    def _pick_eligible_membership(
        cls,
        matched: Dict[MembershipStatus, Dict[str, SeasonRosterMembership]],
        sides: Tuple[str, ...],
    ) -> Optional[SeasonRosterMembership]:
        """THE SEATING PICK: ACTIVE-over-AFFILIATE, home-before-away
        precedence over a ``{status: {team_id: membership}}`` dict already
        narrowed to ONE player's SPINE-VALIDATED memberships against
        ``sides`` — the exact tie-break both ``resolve_membership_context``
        (per player) and ``resolve_membership_contexts_for_game`` (batched,
        #205 blocker 5) need, factored once here so the batch form is a thin
        wrapper over the same rule rather than a parallel reimplementation.

        THIS ORDER IS LOAD-BEARING AND IS NOT A DIAGNOSTIC PREFERENCE. Its
        answer becomes a ``GameMembershipContext``: the side the player is
        attributed to, and the season-scoped ``position`` the slot arithmetic
        buckets them by. It exists because a player pathologically eligible
        on BOTH sides at once must resolve to exactly ONE — the defect
        ``test_slot_overfill_regression`` was written for, where one occupied
        roster row was counted into both sides' summaries and a target of one
        skater seated two. ACTIVE beats AFFILIATE because the authoritative
        stint outranks the governed call-up; home beats away only to make the
        remaining tie total.

        DO NOT rank this by ``reason_rank``. A seating pick has no reason to
        rank — every row reaching it SUCCEEDED — and re-ordering it would
        silently move which membership seats a player. The reason ladder
        governs :meth:`_pick_reason_membership`, which is a separate call
        site on a branch that returns no context at all."""
        return cls._pick_membership(matched, sides,
                                    cls._ELIGIBLE_MEMBERSHIP_STATUSES)

    @classmethod
    def _pick_reason_membership(
        cls,
        matched: Dict[MembershipStatus, Dict[str, SeasonRosterMembership]],
        sides: Tuple[str, ...],
        why: Dict[str, str],
    ) -> Optional[SeasonRosterMembership]:
        """THE REASON-NAMING PICK: of one player's right-keyed,
        participation-granting rows that all FAILED, the one whose reason
        ranks earliest in ``SKIP_REASON_PRECEDENCE``.

        WHY THIS IS NOT :meth:`_pick_eligible_membership`. ``_keep_best_
        reason`` already resolves the rows sharing ONE ``(status, team)`` key
        by the ladder, but the survivor ACROSS keys used to be chosen by the
        SEATING order — so a player with a denormalized ``season_id``
        mismatch (rank 3) on one key and a lapsed registration (rank 9) on
        another was reported under rank 9, purely because that row's stint
        happened to be ACTIVE, or on the home bench. The answer was stable,
        deterministic and true; it was just not the ladder's answer, which
        made ``SKIP_REASON_PRECEDENCE``'s claim to be "the SAME order the
        code actually applies" hold only inside a key. It now holds across
        the whole bucket.

        THE ROWS CONSIDERED ARE EXACTLY ``_pick_membership``'s — the same
        status walk over ``_ELIGIBLE_MEMBERSHIP_STATUSES``, the same sides —
        so only the ORDER differs, and status-then-side survive as the
        secondary keys. Two rows carrying the SAME reason therefore still
        answer with the row the seating order would have named, and the
        membership id makes the order total, so nothing here can fall back
        to store iteration order.

        SAID PRECISELY, BECAUSE THE PARAGRAPH ABOVE READS STRONGER THAN IT
        IS: ``rung`` and ``side_rank`` are UNFALSIFIABLE, exactly as the
        ``parked`` collapse is and for the same reason. This pick's return
        value is consumed only as ``why[m.id]`` — a string — so rows sharing
        a reason yield the same answer whichever of them wins, and dropping
        both secondary keys leaves every test green. They are kept because
        a stable, explainable survivor is worth having when someone later
        makes the winning ROW observable; they are not load-bearing today,
        and no test can fail on them. Only ``reason_rank`` and ``m.id`` do
        observable work here.

        ``why`` must name every row in ``matched``; the direct subscript is
        deliberate, so a row that reached the ``raw`` bucket without a
        recorded reason fails loudly here rather than being ranked as if it
        had one. ``reason_rank`` is likewise fail-loud for a reason nobody
        has placed in the ladder."""
        best = best_key = None
        for rung, status in enumerate(cls._ELIGIBLE_MEMBERSHIP_STATUSES):
            by_team = matched.get(status, {})
            for side_rank, side in enumerate(sides):
                m = by_team.get(side)
                if m is None:
                    continue
                key = (reason_rank(why[m.id]), rung, side_rank, m.id)
                if best_key is None or key < best_key:
                    best, best_key = m, key
        return best

    # -- the {status: {team_id: membership}} COLLAPSE ---------------------
    #
    # Both resolvers narrow a player's membership rows into a
    # ``{status: {team_id: membership}}`` dict before ``_pick_membership``
    # walks it. That dict holds ONE row per (status, team) key, so when a
    # player holds SEVERAL rows sharing a key — repeated historical stints
    # on one bench are ordinary history — the others are DISCARDED. The two
    # helpers below decide WHICH survives, by an explicit key, because the
    # obvious ``setdefault`` spelling decides it by STORE ITERATION ORDER:
    # ``InMemoryStore`` hands rows back in insertion order and ``SqlStore``
    # orders by a TEXT id column, so ``srm_10`` precedes ``srm_2`` on SQL
    # and follows it in memory.
    #
    # THAT WAS NOT COSMETIC. Measured at head e2bde17 on ``InMemoryStore``,
    # a player holding two participation-granting rows at one (status, team)
    # key that fail for DIFFERENT reasons — one carrying a denormalized
    # ``season_id`` mismatch, one on a side whose registration has lapsed —
    # was reported as ``team_not_registered`` in one row order and
    # ``membership_denormalized_season_mismatch`` in the other. The #427
    # acceptance bar asks for "a stable reason for each skip"; a reason that
    # flips with insertion order is stable only by luck, and the ladder in
    # ``membership_spine`` claims to be "the SAME order the code actually
    # applies", which at that head it was not.
    #
    # WHY A DATABASE INDEX IS NOT THE ANSWER. Migration 059's
    # ``ux_srm_open_player_league_season`` and ``ux_srm_active_player_season``
    # do bound the multiplicity that produced that exact flip — between them,
    # at most one NON-TERMINAL row per (player, LeagueSeason) — so on SQLite
    # and PostgreSQL the two-open-row shape is engine-refused
    # (``TwoOpenStintsAtOneLeagueSeasonAreEngineRefused`` pins it, and pins
    # that BOTH indexes are load-bearing). But ``InMemoryStore`` enforces no
    # uniqueness and is a first-class backend of this suite, TERMINAL rows
    # are deliberately outside those indexes and may repeat without limit,
    # and determinism that holds only because some other layer forbids the
    # input is not determinism. The tie-break is imposed HERE, where the
    # reason is chosen.
    #
    # WHICH row wins is deliberately uninteresting WHERE THE CHOICE IS FREE;
    # that it is the SAME row on every backend and in every row order is the
    # whole point. Membership id is the final key because it is the one
    # totally-ordered, store-independent value every row carries.
    #
    # The choice is NOT free everywhere, and the two helpers say so
    # individually: ``_keep_best_reason``'s survivor names a reason and is
    # ranked by the ladder, and ``_keep_lowest_id``'s survivor at the
    # ``valid``/``per_player`` call sites becomes a resolved context and
    # carries its ``position`` into the slot arithmetic. Only the ``parked``
    # call site is genuinely indifferent.
    #
    # NARROWING THE BUCKET IS NOT THE SAME AS PICKING FROM IT. Both helpers
    # collapse rows at ONE ``(status, team)`` key. Which key then answers is
    # a separate decision, and the two picks that make it — the SEATING pick
    # and the REASON-NAMING pick — order the keys by different rules on
    # purpose. See ``_pick_eligible_membership`` and
    # ``_pick_reason_membership``.

    @staticmethod
    def _keep_lowest_id(bucket, m) -> None:
        """Collapse ``m`` into ``bucket`` keeping the LOWEST membership id at
        its (status, team) key — the tie-break for the buckets the reason
        ladder does NOT govern. Three call sites: ``parked`` here,
        ``valid`` here, and ``per_player`` in
        ``resolve_membership_contexts_for_game``.

        WHAT EACH ONE IS WORTH, stated precisely because a previous round of
        this PR described the helper as simply "not mutation-observable" and
        that is true of only ONE of the three.

        ``valid`` and ``per_player`` ARE observable, and the observation is
        not cosmetic: the surviving row becomes the resolved
        ``GameMembershipContext``, supplying its ``position`` — hence the
        GOALIE/SKATER bucket the slot arithmetic counts the player in. Two
        rows disagreeing there under the old ``setdefault`` spelling seat the
        same player into a DIFFERENT SLOT TYPE depending on the order the
        store listed them, which
        ``TheLowestIdTieBreakIsObservableWhereItSeats`` pins by
        permuting the store read each resolver actually calls (one answer
        here, two under ``setdefault``). No PRE-EXISTING test could see it,
        which is what the earlier round measured: on ``InMemoryStore`` the
        natural insertion order already puts the low id first, and on SQL
        migration 059's ``ux_srm_open_player_league_season`` refuses the
        second open row outright. Agreement on the natural order is not
        order-independence. This call site also keeps the single resolver and
        the batch form picking the SAME row, so the form that DECIDES and the
        form that NAMES can never diverge on which stint they mean.

        ``parked`` is the genuinely unfalsifiable one, and by CONSTRUCTION
        rather than for want of a test: its survivor is consumed as
        ``status_ineligible_reason(m.status)``, and ``status`` is half the
        key the rows collapsed onto, so every row at one parked key yields
        the identical string whichever survives. It is hardening against a
        future reader of that bucket, nothing more, and no test can redden on
        it — ``test_the_parked_call_site_cannot_be_observed_at_all`` asserts
        exactly that, passing under both spellings on purpose."""
        by_team = bucket.setdefault(m.status, {})
        current = by_team.get(m.team_id)
        if current is None or m.id < current.id:
            by_team[m.team_id] = m

    @staticmethod
    def _keep_best_reason(bucket, m, reason, why) -> None:
        """Collapse ``m`` into ``bucket`` keeping the row whose REASON ranks
        earliest in ``SKIP_REASON_PRECEDENCE``, id-tie-broken.

        This is the bucket whose survivor decides a SERIALIZED REASON
        STRING, so its tie-break is the written ladder rather than an
        arbitrary-but-stable key: among rows that came equally close to
        seating the player, the reported reason is the one the ladder
        already says outranks the other.

        HALF THE STORY, and the half that is easy to overstate. This
        resolves the rows sharing ONE ``(status, team)`` key; the survivor
        ACROSS keys is chosen by :meth:`_pick_reason_membership`, which ranks
        by the same ladder for the same reason. BOTH are required for
        ``SKIP_REASON_PRECEDENCE`` to be authoritative within a rung as well
        as between rungs. With only this one, a player whose two applicable
        reasons sat at DIFFERENT keys — a different status, or the other
        bench — was still answered by the seating order, and the ladder's
        promise held only inside a key. See
        ``TheLadderGovernsAcrossKeysNotOnlyWithinOne``, whose control case is
        exactly the shape this helper alone already handled.

        ``reason_rank`` raises for an unlisted reason, so a new skip reason
        nobody has placed in the ladder fails loudly here instead of sorting
        wherever it happens to land."""
        by_team = bucket.setdefault(m.status, {})
        current = by_team.get(m.team_id)
        if current is None or ((reason_rank(reason), m.id)
                               < (reason_rank(why[current.id]), current.id)):
            by_team[m.team_id] = m

    @staticmethod
    def _pick_membership(
        matched: Dict[MembershipStatus, Dict[str, SeasonRosterMembership]],
        sides: Tuple[str, ...],
        status_order: Tuple[MembershipStatus, ...],
    ) -> Optional[SeasonRosterMembership]:
        """status-order-then-home-before-away pick over a
        ``{status: {team_id: membership}}`` dict — the shared mechanism
        behind :meth:`_pick_eligible_membership` (which picks the row that
        SEATS a player) and the #427 classifier's PARKED-row pick (whose
        answer is read for its ``status`` alone). The two differ only in
        which status order they hand in, so factoring the walk keeps "home
        before away, deterministically" a single rule.

        THE ``raw`` BUCKET IS NOT ONE OF THEM, deliberately. Its survivor
        names a REASON, which is ranked by ``SKIP_REASON_PRECEDENCE`` in
        :meth:`_pick_reason_membership` instead. Keeping that a separate
        method rather than another ``status_order`` argument here is the
        whole guard: this walk's order is load-bearing for SEATING — it is
        what makes a player eligible on both sides resolve to exactly one —
        and a reason preference must never be able to reach it."""
        for status in status_order:
            by_team = matched.get(status, {})
            for side in sides:
                m = by_team.get(side)
                if m is not None:
                    return m
        return None

    def resolve_membership_contexts_for_game(
        self, game
    ) -> Dict[str, "GameMembershipContext"]:
        """Batch form of :meth:`resolve_membership_context` (#205 blocker 5):
        every player eligible for EITHER of ``game``'s two sides, resolved to
        AT MOST ONE side, in a single pass —
        ``{player_id: GameMembershipContext}``.

        ``_slot_summaries``/``compute_roster_status`` need to attribute a
        whole roster + substitute pool to "this side" without paying one
        per-row resolution — and its ``memberships_for_player`` +
        ``get_league_season`` reads — PER ROW, which would be an N+1 for a
        SQL store. This is backed by ``memberships_for_league_season_team``,
        called ONCE PER SIDE, and the spine is validated ONCE PER SIDE too
        (a side whose participation has ended contributes nobody at all, so
        its membership rows are never even scanned).

        Precedence is applied ONCE PER PLAYER, across BOTH sides together
        (via ``_pick_eligible_membership``) — never as two independently
        computed side-scoped sets. A player pathologically eligible on both
        sides at once must resolve to exactly ONE side, or a single occupied
        roster row could get counted into BOTH sides' summaries.

        Returns ``{}`` for a game with no LeagueSeason binding (see
        :meth:`team_for_game` for the permanent-pointer fallback such a game
        keeps), a bound game whose LeagueSeason row itself dangles, or a bound
        game whose own ``season_id`` disagrees with that LeagueSeason (PR #427)
        — fail closed, the same posture and the same three checks, in the same
        order, as the single form."""
        result: Dict[str, GameMembershipContext] = {}
        if game is None or not season_guard.game_is_league_season_bound(game):
            return result
        ls = self.store.get_league_season(game.league_season_id)
        if ls is None:
            return result
        # The same unconditional game-identity check the single form applies
        # (see ``_resolve_context_with_reason``). It has to be repeated here
        # rather than inherited because this batch form is a genuinely
        # independent resolution — ``compute_roster_status``/``_slot_summaries``
        # /``_partition_candidates`` reach the membership rows through it and
        # never through the single form — and a gate present in only one of two
        # resolvers is the split authority this blocker is about.
        if game.season_id != ls.season_id:
            return result
        sides = tuple(t for t in (game.home_team_id, game.away_team_id) if t)
        if not sides:
            return result
        spines: Dict[str, Tuple[Optional[str], object]] = {}
        per_player: Dict[str, Dict[MembershipStatus,
                                   Dict[str, SeasonRosterMembership]]] = {}
        for side in sides:
            if self._side_spine(spines, ls, side)[1] is None:
                continue
            for m in self.store.memberships_for_league_season_team(
                    game.league_season_id, side):
                if m.status not in self._ELIGIBLE_MEMBERSHIP_STATUSES:
                    continue
                if missing_or_unequal(m.season_id, ls.season_id):
                    continue
                self._keep_lowest_id(
                    per_player.setdefault(m.player_id, {}), m)
        for player_id, matched in per_player.items():
            m = self._pick_eligible_membership(matched, sides)
            if m is None:
                continue
            player = self.store.get_player(player_id)
            if player is None:   # identity leg, same as the single form
                continue
            result[player_id] = self._context_for(
                game, player, ls, spines[m.team_id][1], m)
        return result

    def resolve_memberships_for_game(
        self, game
    ) -> Dict[str, SeasonRosterMembership]:
        """The membership-shaped view of
        :meth:`resolve_membership_contexts_for_game`."""
        return {pid: ctx.membership
                for pid, ctx in
                self.resolve_membership_contexts_for_game(game).items()}

    def team_for_game(self, game, player) -> Optional[str]:
        """Which of ``game``'s two teams ``player`` belongs to, or ``None``.

        THE team-eligibility resolution of the #205 substitute cutover,
        shared by every substitute surface (enroll gate, block-reason,
        outreach queue, addable pool, offer/accept slot accounting, view
        scoping) — and now simply the ``team_id`` of the ONE context
        :meth:`resolve_membership_context` resolves, so team and position can
        never be re-read independently and disagree.

        A player whose only eligible memberships name OTHER teams, or whose
        own side's participation has ended, resolves ``None``:
        cross-boundary substitution stays CLOSED (fail-closed, the same
        posture the permanent gate had). The approved #287 design limits
        future matching candidates to the same LeagueSeason, requires explicit
        League policy for cross-Division projection, and excludes cross-League
        candidates. That future matching projection is not wired or authorized
        at this current-participation boundary, so every other-team membership
        remains a skip.

        For a game with NO LeagueSeason binding the context is the permanent
        ``player.team_id`` pointer, so exhibitions and unbound legacy games
        keep pre-#205 behavior exactly."""
        ctx = self.resolve_membership_context(game, player)
        return ctx.team_id if ctx is not None else None

    def accepted_cross_team_roster_entry(
        self, game, player, *, include_unavailable: bool = False,
    ) -> Optional[GameRosterEntry]:
        """The player's one current accepted borrowed seat, or ``None``.

        This is deliberately *not* membership and therefore is not folded
        into :meth:`team_for_game` or the private-game authorization family.
        It exists for the player's own schedule projection: once a player has
        accepted a cross-team offer, Player Home must count and render the
        game so they can back out, without granting access to the borrowing
        side's private roster workflow. ``include_unavailable`` admits only
        that same exact paired seat after the player backs out, so Home can
        offer the ordinary re-confirm action. A coach-REMOVED row and every
        other non-occupying state remain terminal from player self-service.

        The roster row and enrollment must independently agree on the exact
        game side and slot.  While the source Team/Membership rows still
        exist, they must also agree with the frozen source provenance.  Both
        may be absent after an explicitly authorised subtree deletion (the
        provenance columns deliberately have no foreign keys so that history
        survives), but one missing row or a contradictory survivor is corrupt
        and fails closed.  A bare seat, an enrollment without a seat,
        unsupported history, half-written provenance, conflicting rows, or a
        player who is no longer active all fail closed.  Paired source-row
        deletion remains displayable history, but this read helper does not
        authorize re-seating; :meth:`_authorize_seated_side` revalidates the
        live relationship before that mutation.
        """
        if game is None or player is None or not player.is_active:
            return None
        entry = self.store.roster_entry_for_player(game.id, player.id)
        if (entry is None
                or entry.roster_role != RosterRole.SUBSTITUTE_ADDED
                or entry.selection_source != SelectionSource.SUBSTITUTE_POOL
                or not (entry.status.is_confirmed_body
                        or (include_unavailable
                            and entry.status == RosterEntryStatus.UNAVAILABLE))
                or entry.attribution is None):
            return None
        side, slot_type = entry.attribution
        if side not in {game.home_team_id, game.away_team_id}:
            return None
        accepted = [
            sub for sub in self.store.substitute_enrollments_for_player(
                player.id)
            if (sub.game_id == game.id
                and sub.status == SubstituteStatus.ACCEPTED
                and self._is_cross_team_enrollment(sub))
        ]
        if len(accepted) != 1:
            return None
        sub = accepted[0]
        if sub.team_id != side or sub.slot_type != slot_type:
            return None

        # Source provenance is historical after acceptance, but it is not an
        # unchecked marker.  Cross-team enrollment can never originate from
        # either participating side, and any surviving source Team and
        # Membership must describe the exact frozen source stint.  #429's
        # explicit subtree deletion may remove both rows; accepting that
        # paired absence is why this is not a foreign-key/existence gate.
        game_sides = {game.home_team_id, game.away_team_id}
        if (sub.source_team_id in game_sides
                or sub.source_team_id == sub.team_id):
            return None
        source_team = self.store.get_team(sub.source_team_id)
        source_membership = self.store.get_season_roster_membership(
            sub.source_membership_id)
        if (source_team is None) != (source_membership is None):
            return None
        if source_membership is not None:
            league_season = self.store.get_league_season(
                game.league_season_id)
            if (league_season is None
                    or source_team.league_id != league_season.league_id
                    or source_membership.player_id != player.id
                    or source_membership.team_id != sub.source_team_id
                    or source_membership.league_season_id
                    != game.league_season_id
                    or source_membership.season_id != game.season_id):
                return None
        return entry

    def player_home_team_for_game(self, game, player) -> Optional[str]:
        """The side used only to select a player's own schedule games.

        Seasonal membership remains the ordinary answer. A fully matched
        accepted cross-team seat is also a real commitment and therefore
        belongs on Player Home while occupying and after a player back-out,
        when the card is the recovery path. If both authorities exist and
        disagree, the state is ambiguous and the schedule projection exposes
        neither side.
        """
        member_side = self.team_for_game(game, player)
        borrowed = self.accepted_cross_team_roster_entry(
            game, player, include_unavailable=True)
        borrowed_side = borrowed.team_side if borrowed is not None else None
        if (member_side is not None and borrowed_side is not None
                and member_side != borrowed_side):
            return None
        return member_side or borrowed_side

    def position_for_game(self, game, player) -> Position:
        """The position ``player`` plays FOR ``game`` — the resolved
        context's, which is the SEASON-SCOPED membership position for a bound
        game and the permanent ``Player.position`` for an unbound one.

        RAISES ``NotEligibleError`` when no context resolves. It deliberately
        does NOT fall back to ``player.position`` for a BOUND game whose
        context failed (#205 review blocker 2, owner ruling: "a bound game
        must not fall back to permanent team/position when the context
        fails"). It used to, which meant a caller could read a
        permanent-pointer position off a player whose participation had
        ended and hand it to the slot engine. Every in-tree caller already
        holds a resolved context by the time it needs a position, so the
        raise is unreachable from them — it is the guard that keeps a future
        caller from reintroducing the fallback."""
        return self._require_membership_context(game, player).position

    def _require_membership_context(
        self, game, player
    ) -> "GameMembershipContext":
        """:meth:`resolve_membership_context` or a ``NotEligibleError`` — the
        raising form the state-machine transitions use so a player whose
        participation ended after enrollment (their own membership, or their
        Team's registration/League/Program/Season spine) fails CLOSED at the
        next transition (mirrors the #270 deactivation gate)."""
        ctx = self.resolve_membership_context(game, player)
        if ctx is None:
            name = player.name if player is not None else "Player"
            raise NotEligibleError(
                f"{name} is not eligible for this game (no membership with "
                f"a team in it; cross-team borrowing is off).")
        return ctx

    def _require_team_for_game(self, game, player) -> str:
        """The ``team_id`` of :meth:`_require_membership_context`."""
        return self._require_membership_context(game, player).team_id

    def _players_for_game_team(
        self, game, team_id
    ) -> List[Tuple[Player, Optional["GameMembershipContext"]]]:
        """The candidate pool "players of ``team_id`` for ``game``" (#205
        cutover), each paired with THE context that put them there.

        For a LeagueSeason-bound game that is every player the batched
        resolver seats on this side — which means the side's own spine
        (active registration, Team-League, Program, Season) had to hold and
        each membership's denormalized Season had to agree, exactly as the
        per-player gate demands. The context travels WITH the player so the
        caller reuses this one resolution instead of re-deriving a second
        one that could disagree (#205 review blocker 2).

        For an UNBOUND game there is no membership to resolve, so the
        permanent roster is the pool and the context is resolved the same
        (pointer-based) way the rest of that path always has. Ordering is
        the caller's job."""
        if not season_guard.game_is_league_season_bound(game):
            return [(p, self.resolve_membership_context(game, p))
                    for p in self.store.players_for_team(team_id)]
        contexts = self.resolve_membership_contexts_for_game(game)
        return [(ctx.player, ctx) for _pid, ctx in sorted(contexts.items())
                if ctx.team_id == team_id]

    def durable_game_sides(self, game_id: str) -> Dict[str, str]:
        """``{player_id: team_side}`` for every player THIS GAME can attribute
        to exactly ONE side from a DURABLE record (#427 final blocker, owner
        ruling: "Omit them unless an event can be durably attributed to the
        permitted side").

        THE TWO DURABLE AUTHORITIES, and only these two — the same pair
        :meth:`lineup_population` keys its (a) and (b) populations on:

        * ``GameRosterEntry.attribution[0]`` — the side a seat was written
          against, from the validated context that authorized the seating;
        * ``SubstituteEnrollment.team_id`` — the side an enrollment was
          admitted on.

        NEITHER LIVE MEMBERSHIP NOR THE PERMANENT POINTER, deliberately.
        This map exists to decide who may READ an event that has ALREADY
        happened, and a live membership answers a different question — "whose
        player is this NOW" — which a mid-season transfer moves. Attributing a
        past event by present membership would hand an event about the
        opponent's game to whichever coach the subject happens to belong to
        today. Migration 060/061 wrote these columns precisely so the question
        "which side was this row admitted on" has a stored answer.

        NULL IS OMITTED, NEVER GUESSED. A legacy pre-060/061 row cannot name
        its owner, so its occupant simply does not appear here and every event
        that names them is withheld from BOTH sides — the standing ruling
        ("Legacy NULL attribution is omitted, never guessed"), and the same
        rule ``lineup_population`` already applies to the rows themselves.

        AMBIGUITY IS OMITTED TOO. A player carrying durable records that name
        DIFFERENT sides (a substitute enrolled for one side and later seated
        by the other) has no single durable side, so they are dropped rather
        than resolved by precedence. Picking a winner here would be guessing
        with extra steps, and this map's whole job is to be the thing that
        does not guess.
        """
        claims: Dict[str, set] = {}
        for entry in self.store.roster_for_game(game_id):
            attribution = entry.attribution
            if attribution is not None:
                claims.setdefault(entry.player_id, set()).add(attribution[0])
        for sub in self.store.substitutes_for_game(game_id):
            if sub.team_id is not None:
                claims.setdefault(sub.player_id, set()).add(sub.team_id)
        return {player_id: next(iter(sides))
                for player_id, sides in claims.items() if len(sides) == 1}

    # -- the lineup population (#427 blocker, comments 5390696775 /
    #    5394947899) -------------------------------------------------------
    def lineup_population(self, game, team_id: str) -> List["LineupRow"]:
        """WHO is on ONE side's lineup screen for ``game`` — the whole
        population, each row carrying the AUTHORITY that put it there.

        THE DEFECT THIS REPLACES. ``ApiService._lineup_rows`` enumerated
        ``store.players_for_team(team_id)`` — the permanent ``Player.team_id``
        pointer — for both ``GET /api/games/{id}/lineups`` and ``GET
        /api/games/{id}/board``. Reproduced tri-store (Memory, SQLite, real
        PostgreSQL) over a real authenticated Coach session at head 337374a,
        in TWO contradictions at once on ONE fixture:

        * ACROSS ENDPOINTS — the already-cut-over ``/availability-summary``
          named ``[Current Member, Enrolled Sub, Legacy Seat, Legacy Sub]``
          while ``/lineups`` and ``/board`` named
          ``[Departed Player, Away Member, Pointer Ghost]``. Not an overlap
          problem: the two sets were DISJOINT.
        * WITHIN ONE RESPONSE — ``home.status`` reported
          ``open_skater_slots=1`` against ``target_skaters=3`` (two durably
          seated bodies) and ``substitutes_enrolled=2``, while
          ``home.players`` listed ZERO selected and ZERO substitute rows and
          three strangers marked "available". ``status`` came from
          ``_side_data``'s durable/live authorities; ``players`` came from the
          pointer. One JSON document, two irreconcilable answers.

        FOUR POPULATIONS, FOUR AUTHORITIES, and they are combined — never
        substituted for one another:

        (a) SELECTED ROWS -> ``GameRosterEntry.attribution``, the durable
            ``(team_side, seated_position)`` written at seating time from the
            context that authorized it (migration 061). Tested STRICTLY:
            ``attribution is not None and attribution[0] == team_id``.

            *** NOT ``_side_data.matched_entries``, DELIBERATELY. *** That
            list is built for SLOT ACCOUNTING and charges a pre-061
            NULL-attribution row to EVERY side of its game, in BOTH buckets,
            on purpose — over-refusing can only close slots, never reopen
            them. Verified tri-store, ``_side_data(HOME).matched_entries ==
            _side_data(AWAY).matched_entries`` for such a row. Feeding that
            into a READ would put ONE player in BOTH ``home.players`` AND
            ``away.players`` of a single ``/lineups`` response — a new
            cross-side leak manufactured out of the fix for one. A count that
            fails closed and an identity that names a side are different
            questions; only the second one is asked here.

        (b) ACTIVE SUBSTITUTE ROWS -> ``SubstituteEnrollment.team_id``, the
            durable authorizing-side snapshot (OFFERED since migration 060,
            ENROLLED since a90f314). "Active" is
            :attr:`SubstituteStatus.is_active_enrollment` — ENROLLED and
            OFFERED only; an ACCEPTED row is a SEATED row and arrives through
            (a) instead. Tested STRICTLY: ``sub.team_id == team_id``.

            LEGACY NULL OWNERSHIP: OMITTED FROM BOTH SIDES (owner ruling,
            comment 5394947899). A row that cannot name its owner belongs to
            neither side response. It is not placed on both, and no
            attribution marker is attached to one guessed side — including
            the ``sub_status`` badge, which is itself a side assertion about a
            private workflow. If that player independently holds a current
            valid membership they may still surface through (c), as an
            ordinary live candidate and nothing more. Operator repair
            visibility, if it is ever needed, is a separate unattributed
            collection, never a side assertion here.

        (c) UNSEATED CANDIDATES -> the LIVE exact game-season membership,
            ``resolve_membership_contexts_for_game`` filtered to
            ``ctx.team_id == team_id`` — the same batched resolution
            ``_players_for_game_team`` and ``_availability_candidates``
            already consume, so the pool a coach reads here and the pool they
            are asked about on the availability screen cannot drift.

        (d) UNBOUND EXHIBITION -> :meth:`_unbound_lineup_population`, an
            EXPLICITLY SEPARATE branch, never a fallback. A BOUND game never
            reaches the permanent pointer by any path through this method.

        DEDUP AND PRECEDENCE. A player can hold a roster row AND an active
        enrollment AND a live membership at once, so the union is keyed by
        ``player_id`` with precedence (a) > (b) > (c) — the SAME order the
        old grouping used, which is what keeps ``group``/``roster_status``/
        ``sub_status``/``backed_out`` unambiguous on the one row that
        survives.

        ORDERING IS IMPOSED, NEVER INHERITED. :meth:`_ordered_candidates`,
        reused rather than copied — ``(name, player_id)``, the same total
        order ``list_addable_players`` and ``auto_build_roster`` already use.
        The old code sorted NOTHING and inherited store order, which is not
        cosmetic: measured tri-store, Memory yielded ``player_1, player_2,
        …, player_10, player_11`` (dict insertion) while SQLite and
        PostgreSQL both yielded ``player_1, player_10, player_11, player_2,
        …`` (lexicographic TEXT id).

        SEASONAL FIELDS ONLY, ON A BOUND GAME (owner ruling 2). ``position``
        is the row's own durable/live source — ``entry.seated_position`` for
        (a), ``enrollment.position`` for (b), ``ctx.position`` for (c) — never
        ``Player.position``. ``jersey_number`` is the exact bound
        membership's, and ``None`` when no authoritative seasonal value
        exists; it NEVER falls back to ``Player.jersey_number``. A seated row
        whose occupant's membership has since ended keeps its seat and its
        durable position and reports ``jersey_number=None``, because there is
        no longer a seasonal record to read one from and the permanent
        pointer is not an answer to a seasonal question.

        Raises ``ValidationError`` if ``team_id`` is not one of this game's
        two sides — the same participation check ``_availability_candidates``
        applies, so a caller cannot ask for a third team's private pool.
        """
        if team_id not in (game.home_team_id, game.away_team_id):
            raise ValidationError("That team is not playing in this game.")
        entries = {e.player_id: e
                   for e in self.store.roster_for_game(game.id)}
        subs = self._lineup_substitutes(game.id)
        if not season_guard.game_is_league_season_bound(game):
            return self._unbound_lineup_population(
                game, team_id, entries, subs)

        contexts = self.resolve_membership_contexts_for_game(game)

        def side_context(player_id):
            """The live context ONLY when it names THIS side. A player whose
            membership has moved to the opponent has no seasonal record on
            this side, so this side has no seasonal jersey to report and no
            live eligibility to claim for them."""
            ctx = contexts.get(player_id)
            return ctx if ctx is not None and ctx.team_id == team_id else None

        def owned_sub(player_id):
            """The enrollment row ONLY when it is durably owned by THIS side.
            A NULL-owner legacy row resolves ``None`` here, which is what
            keeps its ``sub_status`` off a guessed side's response."""
            sub = subs.get(player_id)
            return sub if sub is not None and sub.team_id == team_id else None

        rows: Dict[str, "LineupRow"] = {}

        # (a) durable seated rows
        for player_id, entry in entries.items():
            attribution = entry.attribution
            if attribution is None or attribution[0] != team_id:
                continue
            player = self.store.get_player(player_id)
            if player is None:
                continue
            ctx = side_context(player_id)
            rows[player_id] = LineupRow(
                player=player, source="roster",
                # Durable, and guaranteed present: `attribution` is None
                # unless BOTH halves were written.
                position=entry.seated_position,
                jersey_number=self._season_jersey(ctx),
                entry=entry, enrollment=owned_sub(player_id),
                context=ctx, eligible=ctx is not None)

        # (b) durably owned ACTIVE enrollments
        for player_id, sub in subs.items():
            if player_id in rows or not sub.status.is_active_enrollment:
                continue
            if sub.team_id is None or sub.team_id != team_id:
                continue
            player = self.store.get_player(player_id)
            if player is None:
                continue
            if self._has_cross_team_provenance(sub):
                target_ctx = self._cross_team_enrollment_context(
                    game, player, sub)
                ctx = target_ctx.source if target_ctx is not None else None
                jersey_number = None
                eligible = (
                    player.is_active
                    and target_ctx is not None
                    and self.cross_team_game_block_reason(game) is None)
            else:
                ctx = side_context(player_id)
                jersey_number = self._season_jersey(ctx)
                eligible = ctx is not None
            rows[player_id] = LineupRow(
                player=player, source="substitute",
                position=sub.position,
                # A source-team jersey is not a target-team jersey and is not
                # disclosed across the boundary.
                jersey_number=jersey_number,
                entry=None, enrollment=sub,
                context=ctx, eligible=eligible)

        # (c) live unseated candidates
        for player_id, ctx in contexts.items():
            if player_id in rows or ctx.team_id != team_id:
                continue
            rows[player_id] = LineupRow(
                player=ctx.player, source="candidate",
                position=ctx.position,
                jersey_number=self._season_jersey(ctx),
                # NO ROSTER ROW, BY CONSTRUCTION AND BY RULE. Any row durably
                # attributed to this side already claimed this player in (a),
                # so the only entry that could be attached here is one this
                # side does NOT own — a pre-061 NULL-attribution row, or a row
                # seated on the OPPONENT. Attaching either would publish
                # `roster_status: "selected"` on a side that cannot show the
                # row is hers, which is the guessed-side attribution marker
                # the ruling forbids, in the field a Coach reads as "this
                # player is on my roster". Such a player appears here as what
                # this side can actually prove they are: a live candidate.
                entry=None,
                # Same rule, same reason: `owned_sub` is `None` for a legacy
                # NULL-owner enrollment, so no `sub_status` badge is asserted
                # on a guessed side either.
                enrollment=owned_sub(player_id),
                context=ctx, eligible=True)

        return [rows[pid] for pid in self._ordered_candidates(rows)]

    @staticmethod
    def _season_jersey(ctx) -> Optional[int]:
        """The SEASON-scoped jersey for a bound game, or ``None``.

        ``None`` when there is no context on this side, and ``None`` again
        when the membership itself carries no number — a legacy/backfilled
        stint may genuinely have none. Both answers are "no authoritative
        seasonal value exists", and the ruling's instruction for that case is
        ``null``, not ``Player.jersey_number``. Deliberately unlike
        :meth:`_context_for`'s treatment of POSITION, which does fall back to
        the permanent value INSIDE a valid context because every slot
        computation needs some position to bucket by; a jersey number has no
        such consumer, so the honest answer is simply the absent one."""
        if ctx is None or ctx.membership is None:
            return None
        return ctx.membership.jersey_number

    def _unbound_lineup_population(
        self, game, team_id, entries, subs
    ) -> List["LineupRow"]:
        """Population (d): the EXPLICIT unbound-exhibition branch.

        An unbound game has no LeagueSeason and therefore no membership
        authority to consult at all, so the permanent roster IS the pool and
        the permanent ``Player.position``/``Player.jersey_number`` ARE the
        fields — exactly pre-#205 behaviour, and the only place in this
        method family where the pointer is read. It is reached by an
        explicit ``game_is_league_season_bound`` test, never as a fallback
        from a bound game whose resolution failed.

        The pool comes from :meth:`_players_for_game_team`'s own unbound
        branch rather than a second ``players_for_team`` call, so the one
        rule about what "unbound" means lives in one place.

        ORDERING IS THE ONE THING THAT CHANGES HERE. ``_players_for_game_team``
        sorts its BOUND branch and not its unbound one, so delegating alone
        would leave this path inheriting store order — the same tri-store
        divergence :meth:`_ordered_candidates` exists to remove. The ruling
        asks for deterministic ordering of the combined result without
        carving out exhibitions, so the sort is applied here too."""
        rows: Dict[str, "LineupRow"] = {}
        for player, ctx in self._players_for_game_team(game, team_id):
            entry = entries.get(player.id)
            sub = subs.get(player.id)
            if entry is not None:
                source = "roster"
            elif sub is not None and sub.status.is_active_enrollment:
                source = "substitute"
            else:
                source = "candidate"
            rows[player.id] = LineupRow(
                player=player, source=source,
                position=player.position,
                jersey_number=player.jersey_number,
                entry=entry, enrollment=sub,
                context=ctx, eligible=ctx is not None)
        return [rows[pid] for pid in self._ordered_candidates(rows)]

    # ====================================================================
    # coach controls
    # ====================================================================
    @_transactional
    def remove_player(
        self, game_id: str, player_id: str, actor_id: Optional[str] = None,
        authorized_team_id: Optional[str] = None,
    ) -> GameRosterEntry:
        game = self._require_game(game_id)
        game = self._guard_mutable(game)
        entry = self.store.roster_entry_for_player(game_id, player_id)
        if entry is None:
            raise NotFoundError("Player is not on this game's roster.")
        # COMPARAND: `entry.team_side`, THE ROW'S DURABLE ATTRIBUTION
        # (migration 061). Removal is the ruling's ROW-REMOVAL case, and the
        # question a coach asks here is "is this seat MINE?" — which only the
        # side the row is actually counted in can answer.
        #
        # LIVE RESOLUTION WOULD BREAK THE ORDINARY CLEANUP PATH the ruling
        # names: a HOME coach removing a HOME row from a player who has since
        # transferred away would be refused, even though the seat is still
        # HOME's and still occupying HOME's slot. Symmetrically it would let
        # the AWAY coach the player just moved to delete HOME's row.
        #
        # A pre-061 row with NULL `team_side` cannot identify its seat, so a
        # Coach is refused `attribution_missing` with zero writes; an
        # unscoped League Admin still removes it under its existing authority.
        self._require_authorized_team(authorized_team_id, entry.team_side,
                                      "roster row")
        self._back_out_entry(game, entry, actor_id, removed=True)
        return entry

    # ====================================================================
    # BATCH SEATING — copy-previous and auto-fill (owner ruling, PR #427,
    # comment 5379885403, plus the 2026-08-22 candidate-discovery
    # correction)
    # ====================================================================
    # "On a LeagueSeason-bound game, copy_previous_roster and
    # auto_build_roster must skip each currently ineligible player and
    # continue seating the eligible remainder. […] This is not permission
    # for a silent partial success."
    #
    # ------------------------------------------------------------------
    # 1. WHERE CANDIDATES COME FROM — the correction's whole point
    # ------------------------------------------------------------------
    # Candidate discovery must NOT run through the CURRENT membership
    # spine, for either entry point. A transferred/parked/deregistered
    # player resolves onto NO side of this game, so a spine-derived pool
    # would not contain them, so they could never be REPORTED as skipped —
    # the silent drop reappearing in a new form, one layer up. The two
    # pools are therefore built from sources that are true regardless of
    # today's eligibility, and the spine is applied AFTERWARDS, only to
    # CLASSIFY what discovery already found:
    #
    #   copy-previous  ->  the newest prior game's DURABLE
    #                      ``GameRosterEntry.team_side`` (migration 061) —
    #                      the historical record of who that team seated,
    #                      written from the context that authorized each
    #                      seating. See :meth:`_prior_side_candidates`.
    #   auto-fill      ->  the UNION of the legacy ``Player.team_id``
    #                      pointers and EVERY ``SeasonRosterMembership``
    #                      naming this Team (terminal, inactive,
    #                      wrong-LeagueSeason and divergent-pointer rows
    #                      included), de-duplicated. See
    #                      :meth:`_auto_build_candidates`.
    #
    # ------------------------------------------------------------------
    # 2. ONE OUTER TRANSACTION, WITH LOCKS, CLASSIFICATION INSIDE IT
    # ------------------------------------------------------------------
    # The owner's correction: "Keep classification and seating inside one
    # outer transaction: acquire the relevant locks, revalidate every
    # candidate, partition eligible/skipped before the first write, then
    # seat. Partition-before-write must not become classify-before-
    # transaction, or membership changes can race the batch."
    #
    # So the order, in both entry points, is exactly:
    #
    #   open transaction (@_transactional)
    #     -> _guard_mutable            = the SEASON ROW LOCK
    #     -> discover candidates
    #     -> _lock_candidates          = every candidate's PLAYER ROW LOCK
    #     -> _partition_candidates     = revalidate + partition, no writes
    #     -> select_roster / set_availability
    #     -> ONE audit row
    #
    # WHY THOSE TWO LOCKS ARE "THE RELEVANT LOCKS". Between them they cover
    # every governed mutation that can change a candidate's answer:
    #
    #   * ``SetupService.set_season_roster_membership_status`` (park,
    #     revive, end a stint) locks the membership row AND, via
    #     ``_require_active_season``, the SEASON row — the same row
    #     ``_guard_mutable`` -> ``_guard_active_season`` ->
    #     ``require_active_season`` takes here, first, and holds to commit;
    #   * ``SetupService.create_season_roster_membership`` (open a new
    #     stint) locks the PLAYER row;
    #   * ``SetupService.set_player_active`` (#270 deactivation) locks the
    #     PLAYER row;
    #   * ``SetupService.assign_player_team`` (the permanent pointer, which
    #     the auto-fill pool reads) locks the PLAYER row.
    #
    # WHICH Season lock, precisely — and this is where an earlier version of
    # this comment taught the wrong rule (PR #427 blocker). It said "a game
    # with no season_id at all (an unbound legacy row) takes no Season lock",
    # which conflates two different rows: a game is UNBOUND when
    # ``league_season_id is None``, never because its denormalized
    # ``season_id`` happens to be empty. A LeagueSeason-BOUND game with a
    # missing or drifted ``season_id`` is a corrupted bound row, not a legacy
    # one, and ``season_guard.guard_game_season`` refuses it outright rather
    # than letting it through unlocked.
    #
    # So: a BOUND game locks the Season its LEAGUESEASON names — the shared
    # row every writer on that competition, and ``archive_season`` itself,
    # serializes on. Only a genuinely unbound row (an exhibition, or a
    # pre-#283 legacy game) uses its own ``season_id``, and only such a row
    # naming no Season at all takes no Season lock: there is then no
    # competition and no membership to change, and its eligibility is the
    # permanent pointer, which the Player lock covers.
    #
    # WHY THE PARTITION CANNOT BE A try/except AROUND ``select_roster``.
    # ``transaction()`` is REENTRANT WITH NO SAVEPOINTS in both backends:
    # only the outermost context commits or rolls back, and a nested block
    # has nothing to unwind to. Catching ``NotEligibleError`` mid-batch
    # would therefore KEEP every write made before it, and on PostgreSQL an
    # exception raised after any statement leaves the connection in
    # InFailedSqlTransaction so the NEXT statement fails too. The partition
    # is a decision taken from NON-RAISING classification
    # (:meth:`seating_block_reason`), which is what makes the ruling's last
    # requirement true: the transaction contains only writes expected to
    # succeed, so "unexpected persistence/transaction failures remain
    # all-or-nothing" and an ELIGIBILITY SKIP rolls nothing back.
    #
    # AND WHY THERE IS NO ``skip=True`` FLAG ON ``select_roster``. The
    # ruling closes with "This ruling does not relax the live-eligibility
    # requirement for individual mutations." A mode flag on the shared
    # seating primitive is one careless call site away from relaxing
    # enroll/offer/accept/coach-add/re-confirm too. ``select_roster`` is
    # called here with an already-validated list and still fails closed on
    # every one of them — under the locks above it cannot disagree, and if
    # it ever did, it RAISES and the whole batch rolls back rather than
    # silently seating something the partition did not authorize.

    # An eligible candidate the auto-fill targets simply had no room for.
    # NOT an eligibility reason and deliberately NOT in
    # ``SKIP_REASON_PRECEDENCE``: nothing is wrong with this player, the
    # roster is just full. Kept as its own reported bucket rather than
    # folded into ``skipped`` so the operator warning stays about players
    # who CANNOT be seated, while the audit row still accounts for every
    # candidate the batch examined.
    TARGET_MET = "roster_target_met"

    # A candidate who is ALREADY sitting in an occupying roster row on this
    # game before the batch ran (owner ruling, PR #427, comment 5385783876).
    #
    # NEITHER A SEAT NOR A SKIP, so it gets its own reported bucket:
    #
    #   * not ``seated`` — no row was written for them, and telling the
    #     operator "Auto-fill added Zulu" when Zulu was already on the
    #     roster is exactly the false report the ruling calls out;
    #   * not ``deferred`` — ``roster_target_met`` means "eligible, but the
    #     roster had no room"; this player HAS room, they are in it;
    #   * not ``skipped`` — nothing is wrong with them, so they must not
    #     appear in the operator warning about players who CANNOT be
    #     seated. Like :data:`TARGET_MET` this is deliberately NOT in
    #     ``SKIP_REASON_PRECEDENCE``.
    #
    # The audit row still accounts for them, so every candidate the batch
    # examined is present in exactly one bucket.
    ALREADY_SEATED = "already_on_roster"

    def _ordered_candidates(self, player_ids) -> List[str]:
        """De-duplicate and order candidate ids by ``(name, player_id)``.

        ORDERING IS IMPOSED BY THE SERVICE, NEVER INHERITED FROM THE STORE.
        Ids are ``f"{prefix}_{seq}"`` and ``SqlStore`` orders by a TEXT
        column, so ``players_for_team``/``roster_for_game``/
        ``memberships_for_team`` hand back ``player_1, player_10, player_11,
        …, player_2`` on SQL and insertion order in memory. Measured
        tri-store at head 4de9452, that was not a cosmetic difference:
        ``auto_build_roster`` TRUNCATES its pool at the room the side has
        left, so the ORDER DECIDED SET MEMBERSHIP — from one identical 12-player
        fixture with ``target_skaters=3``, Memory seated "Player 00/01/02"
        while SQLite and PostgreSQL both seated "Player 00/09/10".

        ``(name, player_id)`` is the convention ``list_addable_players``
        already uses, so the pool a coach reads and the pool auto-fill draws
        from agree; the id tail makes it a TOTAL order, so two players
        sharing a name still sort deterministically. A candidate whose
        Player row is missing sorts under ``""`` — it is going to be skipped
        as ``membership_player_missing`` anyway, and it still needs a
        defined position so the SKIPPED list is ordered too."""
        seen = set()
        rows = []
        for pid in player_ids:
            if pid in seen:
                continue
            seen.add(pid)
            player = self.store.get_player(pid)
            rows.append(((player.name if player is not None else ""), pid))
        rows.sort()
        return [pid for _name, pid in rows]

    def _lock_candidates(self, player_ids) -> Dict[str, Optional[Player]]:
        """Row-lock every candidate Player, UP FRONT and in canonical
        (sorted-unique-id) order, and return ``{player_id: Player|None}``.

        Sorted so two concurrent batches over overlapping pools cannot AB-BA
        deadlock on PostgreSQL — the identical discipline (and the identical
        comment) ``select_roster`` applies to its own list. MUST run inside
        the caller's ``transaction()``: the locks are held to commit, and
        they are the whole reason the classification below cannot be raced
        by a concurrent ``set_player_active`` /
        ``create_season_roster_membership`` / ``assign_player_team``."""
        return {pid: self.store.get_player_for_update(pid)
                for pid in sorted(set(player_ids))}

    def _partition_candidates(self, game, team_id, candidates, locked,
                              preclassified=None):
        """``(seatable, skipped, contexts)`` — the REVALIDATION, run under
        the locks and BEFORE the first write.

        ``seatable`` and ``skipped`` both preserve ``candidates`` order (the
        caller has already imposed a deterministic one). ``skipped`` is
        ``[(player_id, reason)]`` with each reason one of the stable strings
        in :mod:`~.membership_spine`. ``contexts`` carries THE resolved
        :class:`GameMembershipContext` for each seatable id, so the caller
        buckets by the SEASON-scoped slot type the seat will actually be
        written with rather than re-deriving a second, possibly disagreeing
        one.

        ``preclassified`` is ``{player_id: reason}`` for candidates already
        refused at DISCOVERY time — today only copy-previous's
        ``prior_seat_unattributed``. Those reasons win outright, which is
        rank 0 of ``SKIP_REASON_PRECEDENCE``: a candidate whose provenance
        cannot be proven was never established as a candidate for this side,
        so today's eligibility is not consulted at all.

        IT DECIDES WITH THE SAME RESOLUTION ``select_roster`` DECIDES WITH —
        the batched ``resolve_membership_contexts_for_game`` for a bound
        game, the per-player form for an unbound one — so a player this
        method calls seatable cannot then be refused inside the transaction.
        The batched form produces no reasons, so a MISS is re-asked of
        :meth:`seating_block_reason` purely to NAME it; the DECISION is
        never taken from that second call.

        NON-RAISING, by construction. Everything it consults is a read."""
        bound = season_guard.game_is_league_season_bound(game)
        contexts = (self.resolve_membership_contexts_for_game(game)
                    if bound else {})
        preclassified = preclassified or {}
        seatable: List[str] = []
        skipped: List[Tuple[str, str]] = []
        chosen: Dict[str, GameMembershipContext] = {}
        for pid in candidates:
            forced = preclassified.get(pid)
            if forced is not None:
                skipped.append((pid, forced))
                continue
            player = locked.get(pid)
            if player is None:
                skipped.append((pid, PLAYER_MISSING))
                continue
            ctx = (contexts.get(pid) if bound
                   else self.resolve_membership_context(game, player))
            if ctx is None:
                # ``or NO_ELIGIBLE_MEMBERSHIP``: the two resolutions agree by
                # construction, so this fallback is unreachable — it exists
                # so a future divergence degrades to the narrowest honest
                # answer instead of putting ``None`` in an operator's face.
                skipped.append((pid, self.seating_block_reason(game, player)
                                or NO_ELIGIBLE_MEMBERSHIP))
                continue
            if not player.is_active:
                # The GATE's order, and therefore the ladder's: the context
                # is tested first and deactivation second, so a candidate
                # failing both is reported under the context reason.
                skipped.append((pid, PLAYER_INACTIVE))
                continue
            if ctx.team_id != team_id:
                # ONE BATCH SEATS ONE SIDE (#205 Part C). Discovery is
                # deliberately NOT spine-derived (see the section header), so
                # both pools can surface a candidate whose CURRENT context
                # resolves onto the OTHER side of this same game: a
                # copy-previous candidate seated on HOME last game who has
                # since moved to AWAY, or an auto-fill candidate whose
                # permanent pointer still names HOME while their membership
                # names AWAY.
                #
                # Until now a context merely EXISTING made such a candidate
                # seatable, and `select_roster` then wrote the row on
                # `ctx.team_id` — so a batch that reported `team_id=HOME`
                # durably seated `team_side=AWAY`, counted against HOME's
                # remaining capacity. Measured on Memory and SQLite at head
                # a90f314; see `MEMBERSHIP_OTHER_SIDE`.
                #
                # It is a REPORTED SKIP, not a raise: the ruling's "never a
                # silent partial success" applies, and an opposing-side
                # candidate is a fact about the cohort, not a bad request.
                skipped.append((pid, MEMBERSHIP_OTHER_SIDE))
                continue
            seatable.append(pid)
            chosen[pid] = ctx
        return seatable, skipped, chosen

    def _batch_rows(self, entries) -> List[dict]:
        """``[(player_id, reason)]`` -> the operator-facing row shape.

        ``name`` travels with the reason because the UI must NAME the
        skipped players and the reason codes alone cannot; it is the
        display name the coach already sees on this very screen, not a
        privacy-gated field (``SensitiveFieldCategory`` covers birthdate,
        registration number and contact/medical/discipline data — not the
        roster name), and both batch routes are MANAGE_ROSTER-gated."""
        rows = []
        for pid, reason in entries:
            player = self.store.get_player(pid)
            rows.append({"player_id": pid,
                         "name": player.name if player is not None else "",
                         "reason": reason})
        return rows

    def _seat_batch(self, game, team_id, candidates, source,
                    from_game_id=None, actor_id=None, confirm=False,
                    cap_to_open_slots=False, preclassified=None) -> dict:
        """THE unit of work both batch entry points share: lock, revalidate,
        partition, seat, audit.

        MUST run inside the caller's ``transaction()``, AFTER the caller's
        ``_guard_mutable`` has taken the Season row lock — see the section
        header above for why those two locks are the relevant ones.
        Deliberately NOT ``@_transactional`` itself: decorating it would
        advertise a self-sufficiency it does not have (the Season lock and
        the mutability guard are the caller's, and taking them after
        discovery would be too late).

        ``cap_to_open_slots`` (auto-fill only) bounds the number of NEW
        seats per bucket by THE ROOM THAT IS ACTUALLY LEFT — see
        "REMAINING CAPACITY" below — and reports the eligible remainder as
        ``deferred`` with reason :data:`TARGET_MET`. ``confirm``
        additionally marks each seated player AVAILABLE inside this SAME
        transaction — previously that was N separate ``set_availability``
        transactions after a separate ``select_roster`` one, so a failure
        mid-loop left players seated but unconfirmed with nothing to roll
        back to.

        ------------------------------------------------------------------
        REMAINING CAPACITY (owner ruling, PR #427, comment 5385783876)
        ------------------------------------------------------------------
        "auto-fill currently overfills a partially occupied roster. […] The
        defect is at RosterService.auto_build_roster: it passes the full
        targets as limits (target_skaters=2) rather than the remaining
        durable capacity. […] derive each bucket's remaining capacity from
        the current durable side/bucket occupancy, exclude or explicitly
        treat already-occupying rows idempotently, and cap only genuinely
        new seats against that remaining room. Do not infer capacity from
        confirmed counts or live membership; existing durable occupants
        still consume their recorded slot."

        Measured tri-store at head 04a4b11, with ``target_skaters=2``, an
        occupying "Zulu Existing" and eligible "Alpha New"/"Beta New": the
        response said ``seated=[Alpha, Beta]``, ``deferred=[(Zulu,
        roster_target_met)]`` and ``open_skater_slots=0`` while storage
        held THREE occupying rows — the truncation dropped the occupant
        (who sorts last) instead of the newcomer, and ``open_count``'s
        ``max(0, …)`` clipped the extra row out of the report.

        SO THE ROOM IS READ, NOT ASSUMED, and it is read from the ONE place
        the slot gate reads it: :meth:`_slot_summaries` -> :meth:`_side_data`,
        whose occupancy comes off each row's DURABLE
        ``GameRosterEntry.attribution`` (migration 061) and from nothing
        else. That satisfies the ruling's two prohibitions by construction:

          * NOT confirmed counts — ``open_count`` is
            ``max(0, target - OCCUPIED)``; ``confirmed_count`` is a separate
            field this never touches, so a seated-but-unanswered row still
            consumes its slot;
          * NOT live membership — ``_side_data`` re-resolves nothing for a
            seated row. An occupant whose participation has since ended,
            or who has moved to the other side, STILL consumes the slot
            their row records (and a pre-061 NULL-attribution row is
            charged on every side and in both buckets, fail-closed).

        Read AFTER ``_lock_candidates``/``_partition_candidates`` and inside
        the caller's transaction, so the Season and Player row locks that
        make the partition unraceable also cover this arithmetic.

        ALREADY-OCCUPYING CANDIDATES ARE A NO-OP, AND SAID SO. A candidate
        whose row on this game already occupies a slot is excluded from the
        new-seat cap and reported in ``already_seated``
        (:data:`ALREADY_SEATED`) rather than seated:

          * they need no new seat — the row is already there, and it is
            already counted in the occupancy the room was derived from, so
            charging them against the room would refuse a genuine newcomer
            for a seat nobody takes;
          * they must not be re-seated. ``select_roster`` is idempotent for
            an occupying row (it returns it untouched, keeping the
            attribution that authorized the original seating), so passing
            them through would write nothing to the roster — but under
            ``confirm`` it WOULD drive a ``set_availability`` that flips a
            SELECTED row to CONFIRMED and writes an availability row and an
            audit row. "Auto-fill the remaining slots" must not answer
            availability on behalf of players who were already on the
            roster;
          * so the batch is idempotent: run twice, the second run seats
            nobody, writes no roster/availability row, and reports every
            candidate it found already in place.

        A candidate whose live context resolves to the OTHER side of this
        game is charged against THIS side's room exactly as before —
        unchanged, deliberately: it is conservative (it can only refuse a
        seat, never admit one) and which side such a candidate belongs on
        is the cohort question the 2026-08-22 correction governs, not this
        one.

        ZERO SEATS IS A SUCCESSFUL RESULT: ``select_roster`` is not called
        at all, so NO roster write of any kind happens, and the audit row is
        still written because it is the only durable record that the
        operation ran.

        Returns identity, never counts: ``{"team_id", "source",
        "from_game_id", "candidate_count", "seated", "skipped", "deferred",
        "already_seated"}``."""
        locked = self._lock_candidates(candidates)
        seatable, skipped, contexts = self._partition_candidates(
            game, team_id, candidates, locked, preclassified)
        deferred: List[Tuple[str, str]] = []
        already: List[Tuple[str, str]] = []
        if cap_to_open_slots:
            summaries = self._slot_summaries(game.id, team_id)
            room = {st: summaries[st].open_count for st in summaries}
            occupying = {e.player_id
                         for e in self.store.roster_for_game(game.id)
                         if e.status.occupies_slot}
            capped = []
            for pid in seatable:
                if pid in occupying:
                    already.append((pid, self.ALREADY_SEATED))
                    continue
                slot = contexts[pid].slot_type
                if room.get(slot, 0) <= 0:
                    deferred.append((pid, self.TARGET_MET))
                    continue
                room[slot] -= 1
                capped.append(pid)
            seatable = capped
        if seatable:
            self.select_roster(game.id, seatable, actor_id)
            if confirm:
                for pid in seatable:
                    self.set_availability(
                        game.id, pid, AvailabilityStatus.AVAILABLE)
        skipped_rows = self._batch_rows(skipped)
        deferred_rows = self._batch_rows(deferred)
        already_rows = self._batch_rows(already)
        # ONE audit row per batch, inside this transaction, present even on a
        # zero-seat run. It records IDS AND REASONS — never counts — so the
        # durable trail answers "which players, and why" without a join
        # against state that has since moved on.
        self._audit(
            game.id,
            AuditAction.ROSTER_BATCH_SEATED,
            actor_id=actor_id,
            detail={
                "source": source,
                "team_id": team_id,
                "from_game_id": from_game_id,
                "candidate_count": len(candidates),
                "selected_player_ids": list(seatable),
                "skipped": [{"player_id": r["player_id"],
                             "reason": r["reason"]} for r in skipped_rows],
                "deferred": [{"player_id": r["player_id"],
                              "reason": r["reason"]} for r in deferred_rows],
                "already_seated": [{"player_id": r["player_id"],
                                    "reason": r["reason"]}
                                   for r in already_rows],
            },
        )
        return {
            "team_id": team_id,
            "source": source,
            "from_game_id": from_game_id,
            "candidate_count": len(candidates),
            "seated": list(seatable),
            "skipped": skipped_rows,
            "deferred": deferred_rows,
            "already_seated": already_rows,
        }

    def _batch_team(self, game, team_id,
                    authorized_team_id: Optional[str] = None) -> str:
        """THE ONE EFFECTIVE TEAM a batch entry point acts on, resolved
        BEFORE authorization and before any candidate discovery or write.

        THE DEFECT THIS CLOSES (owner ruling, PR #427, comment 5391127041) —
        a STEADY-STATE hole, not a race:

            "``scope_violation`` checks ``team_id`` only when the body value
             is truthy, while ``_batch_team`` turns an omitted value into
             ``game.home_team_id``. I reproduced this through authenticated
             HTTP on this head: an AWAY Coach posted ``{}`` to
             ``/api/games/{id}/build-roster``, received 200, the response
             named HOME, and a current HOME player was durably written as
             ``confirmed`` with ``team_side=HOME``. ``roster/copy-previous``
             has the same target-selection shape."

        Reproduced again here at head 22bd6de on Memory, SQLite and real
        PostgreSQL before the fix, on BOTH routes. That reproduction is now
        re-runnable in a stronger form than the scratch harness it used to
        cite: rewriting the branch below to ``team_id = authorized_team_id``
        unconditionally — the silent rewrite this refusal prevents —
        reddens ``AnExplicitForeignTeamIsRefusedBelowThePreflight`` in
        ``tests/test_batch_effective_team.py``, which calls this method's
        callers DIRECTLY and never touches ``scope_violation``.

        The two halves each looked reasonable alone: the
        preflight abstained because a falsy ``team_id`` is "no target to
        constrain", and the service defaulted because HOME is the documented
        default (#25). Together they let an opposing coach create and confirm
        another team's roster with an empty body and no interleaving at all.

        SO THE DEFAULT AND THE AUTHORIZATION ARE DECIDED IN ONE PLACE, and
        the caller passes the result straight into the locked mutation:

        * FOR A COACH (``authorized_team_id`` set), OMISSION MEANS THEIR OWN
          SIDE — never HOME by fallback. PINNED BEHAVIOUR, and the ruling
          requires the choice to be pinned either way: an empty body seats
          the coach's own team rather than refusing. That is the action they
          are unambiguously authorized for, it is what every existing
          one-click coach flow already intends, and a refusal would trade a
          security hole for a usability one while making the AWAY coach's
          ``{}`` and the HOME coach's ``{}`` behave differently for no reason
          a user could see.
        * AN EXPLICIT DIFFERENT TEAM IS FORBIDDEN for a Coach, with the same
          structured ``team_scope_violation`` every other surface raises —
          not silently rewritten to their own side, which would turn a
          mistaken (or malicious) request into a successful one.
        * FOR AN UNSCOPED ROLE (``None`` — League Admin/operator) THE HOME
          DEFAULT IS PRESERVED EXACTLY, byte-for-byte the pre-existing #25
          behaviour the ruling asks to keep.

        A team not playing in this game is still refused as a bad REQUEST,
        not an ineligible candidate, so it keeps raising rather than becoming
        a skip.
        """
        if authorized_team_id is not None:
            if team_id is not None and team_id != authorized_team_id:
                self._require_authorized_team(authorized_team_id, team_id,
                                              "team")
            team_id = authorized_team_id
        else:
            team_id = team_id or game.home_team_id
        if team_id not in (game.home_team_id, game.away_team_id):
            raise ValidationError("That team is not playing in this game.")
        return team_id

    # -- copy previous roster --------------------------------------------
    def _prior_side_candidates(self, src, team_id):
        """``(candidates, preclassified)`` for one source game and one side —
        DISCOVERED EXCLUSIVELY FROM THE SOURCE GAME'S DURABLE ATTRIBUTION.

        WHICH SIDE a historical row was seated on is answered by the row's
        own ``GameRosterEntry.team_side`` (migration 061, written at every
        seat and re-seat) and by NOTHING ELSE. That is the owner's
        correction, and the reason is exact: re-deriving the side from the
        player's CURRENT membership would answer "no side at all" for
        precisely the transferred/parked/deregistered players this ruling
        exists to REPORT, so they would disappear from the candidate pool
        before they could be reported again; and re-deriving it from the
        permanent ``Player.team_id`` pointer is the silent-drop defect
        itself (measured tri-store at head 4de9452: a genuinely transferred
        player was absent from ``copied`` and from the response entirely,
        with no reason anywhere).

        PRE-061 ROWS — the NULL-attribution decision, stated. A row written
        before migration 061 carries ``team_side IS NULL``; 061 performs no
        backfill because no honest backfill value exists. Such a row is
        admitted as a candidate on EVERY side and immediately refused with
        :data:`~.membership_spine.PRIOR_SEAT_UNATTRIBUTED`. It is never
        seated, its current eligibility is never consulted, and it is never
        silently omitted. This is exactly symmetric with the rule already
        shipped on this branch for the slot arithmetic
        (``LegacyRowsWithNoAttributionFailClosed``): a NULL row is charged
        on every side and in both buckets, consulting nothing, accepting
        OVER-refusal as the price of never guessing. Here the same trade
        costs OVER-reporting — a NULL row that was really on the away bench
        is reported as unprovable when copying home — which is strictly
        better than the alternative, since the operator can see the name and
        re-select by hand."""
        candidates = []
        preclassified = {}
        for e in self.store.roster_for_game(src.id):
            if not e.status.occupies_slot:
                continue
            if e.team_side is None:
                candidates.append(e.player_id)
                preclassified[e.player_id] = PRIOR_SEAT_UNATTRIBUTED
            elif e.team_side == team_id:
                candidates.append(e.player_id)
        return self._ordered_candidates(candidates), preclassified

    def _newest_prior_source(self, game, team_id):
        """The single AUTHORITATIVE source game for a copy, or ``None``.

        The newest non-cancelled earlier game this team played in which
        ANYBODY occupied a slot on this side. ORDERING IS TOTAL AND
        SERVICE-IMPOSED: ``(start_time, id)`` descending. The previous
        version sorted on ``start_time`` alone and broke a TIE on
        ``all_games()`` order — insertion order on Memory, TEXT id order on
        SQL — so two backends could pick DIFFERENT source games for the same
        fixture, and (because the copy then seats a different roster) reach
        different final state.

        AUTHORITATIVE MEANS IT DOES NOT FALL THROUGH. Once this game is
        chosen, a copy whose candidates are ALL ineligible is a successful
        zero-seat result naming every one of them — never a walk further
        back to an older game. Walking on would seat a lineup the coach
        never asked for and would HIDE the fact that this team's last roster
        has entirely aged out, which is the opposite of what "never a silent
        partial success" asks for."""
        earlier = [
            g for g in self.store.all_games()
            if g.id != game.id and team_id in (g.home_team_id, g.away_team_id)
            and not g.cancelled and g.start_time is not None
            and (game.start_time is None or g.start_time < game.start_time)
        ]
        earlier.sort(key=lambda g: (g.start_time, g.id), reverse=True)
        for src in earlier:
            candidates, preclassified = self._prior_side_candidates(
                src, team_id)
            if candidates:
                return src, candidates, preclassified
        return None, [], {}

    @_transactional
    def copy_previous_roster(
        self, game_id: str, team_id: Optional[str] = None,
        actor_id: Optional[str] = None,
        authorized_team_id: Optional[str] = None,
    ) -> dict:
        """Seed one side's roster from that team's most recent earlier game —
        SKIPPING each currently ineligible player, seating the eligible
        remainder, and REPORTING every skip (owner ruling, PR #427).

        Candidates come from the newest prior game's durable
        ``team_side`` attribution (:meth:`_prior_side_candidates`); the
        target game's spine then classifies each of them
        (:meth:`_partition_candidates`), inside this method's transaction
        and under its locks. Seating goes through :meth:`select_roster`, so
        every eligibility, lock and audit rule still applies to the players
        that ARE seated. ``team_id`` defaults to the home side.

        WHAT THIS METHOD USED TO DO, and why both halves were wrong —
        measured tri-store at head 4de9452:

        * its candidate filter was ``p.team_id == team_id``, the PERMANENT
          pointer that #205 is retiring. A genuinely TRANSFERRED player was
          **silently dropped** by it: absent from ``copied``, absent from
          the response, with no reason anywhere. "Not permission for a
          silent partial success" was already violated, today, for the very
          first shape the owner names;
        * and a "Mover" (pointer still on the team, seasonal record
          elsewhere) SURVIVED that filter, reached ``select_roster`` and
          raised ``NotEligibleError``, **aborting the whole batch** — the
          still-eligible team-mates were not seated either.

        ZERO-SEAT IS A SUCCESS. ``ValidationError`` is reserved for its true
        meaning: there is no earlier game with ANY occupying roster on this
        side at all. "A source game existed but every one of its players is
        now ineligible" returns a successful zero-seat result naming all of
        them and makes no roster writes.

        The response keeps ``copied``/``from_game_id``/``team_id`` with
        their exact previous meanings and ADDS the identity keys, so every
        existing consumer of this plain dict keeps working."""
        game = self._require_game(game_id)
        game = self._guard_mutable(game)          # <- the SEASON ROW LOCK
        # ONE effective team, resolved AND authorized under that lock, BEFORE
        # candidate discovery and before any write (#205 Part C).
        team_id = self._batch_team(game, team_id, authorized_team_id)
        src, candidates, preclassified = self._newest_prior_source(
            game, team_id)
        if src is None:
            raise ValidationError("No previous roster to copy for this team.")
        result = self._seat_batch(
            game, team_id, candidates, source="copy_previous_roster",
            from_game_id=src.id, actor_id=actor_id,
            preclassified=preclassified)
        return {**result, "copied": len(result["seated"])}

    # -- auto-fill --------------------------------------------------------
    def _auto_build_candidates(self, game, team_id) -> List[str]:
        """The auto-fill candidate COHORT: the UNION the owner's correction
        specifies — "legacy team pointers plus the team's season-membership
        rows — including terminal, inactive, wrong-LeagueSeason, and
        divergent-pointer cases — then deduplicated and classified".

        Both halves are load-bearing, and each covers what the other misses:

        * the POINTER half (``players_for_team``) keeps the ruling's named
          shapes reportable. A membership-less, parked or deregistered
          player resolves onto no side at all, so a purely spine-derived
          pool would make them invisible rather than skipped-with-a-reason,
          and the operator would be told nothing about the bench they can
          see in front of them;
        * the MEMBERSHIP half (``memberships_for_team``, unfiltered by
          status and by LeagueSeason) keeps a "Mover" — pointer elsewhere,
          seasonal record here — SEATABLE, and keeps a player whose only
          stint on this team has ENDED or belongs to a DIFFERENT
          competition reportable rather than absent.

        Nothing here decides eligibility; every id this returns is
        classified afterwards, inside the transaction."""
        ids = [p.id for p in self.store.players_for_team(team_id)]
        ids += [m.player_id
                for m in self.store.memberships_for_team(team_id)]
        return self._ordered_candidates(ids)

    @_transactional
    def auto_build_roster(self, game_id: str, team_id: Optional[str] = None,
                          actor_id: Optional[str] = None,
                          authorized_team_id: Optional[str] = None) -> dict:
        """Select + confirm a roster for one side up to the game's targets,
        skipping each currently ineligible candidate and reporting every one
        of them (owner ruling, PR #427).

        THE SEATING LIVES HERE, not in the facade. It used to live in
        ``ApiService.auto_build_roster``, and that placement caused two of
        the defects the ruling names: the method could not be
        ``@_transactional`` (the decorator is a ``RosterService`` concern),
        so one ``select_roster`` transaction was followed by N SEPARATE
        ``set_availability`` transactions and a failure mid-loop left
        players seated but unconfirmed with nothing to roll back; and its
        response carried NO PLAYER IDENTITY AT ALL — only slot counts — so
        "identify the players seated" was unmet even on the happy path. The
        facade keeps only the presentation it has always added on top (the
        resulting roster status and the coach-friendly short-roster
        classification).

        BUCKETING IS BY THE RESOLVED CONTEXT'S SLOT TYPE, never the
        permanent ``Player.position``. The seat this method writes carries
        ``ctx.position`` (``select_roster``, migration 061) and the slot
        arithmetic counts THAT, so choosing a goalie by the permanent
        pointer and then seating them as a season-scoped skater would fill
        the wrong bucket. For an unbound game the context IS the permanent
        pointer, so that path is unchanged.

        IT FILLS THE REMAINING SLOTS, NOT THE WHOLE TARGET (owner ruling,
        PR #427, comment 5385783876). The cap this passes down is the room
        DERIVED FROM CURRENT DURABLE OCCUPANCY on this side, never the raw
        ``game.target_*``; a partially occupied roster is topped up rather
        than overfilled, and a candidate who already occupies a slot is a
        reported no-op instead of a seat. See ``_seat_batch``'s "REMAINING
        CAPACITY" section for the derivation, the two prohibited sources
        (confirmed counts, live membership) and the measured overfill this
        replaces.

        EVERY INELIGIBLE CANDIDATE IS REPORTED, including ones the targets
        would never have reached: the cohort is the coach's own bench, the
        reasons are facts about it, and reporting only the first N would
        make the warning depend on how many slots happened to be open. The
        eligible remainder there was no room for is reported separately as
        ``deferred``, and the candidates already on the roster as
        ``already_seated`` — nothing is wrong with either group.

        ``ValidationError`` still means "this team has nobody at all"; an
        empty COHORT (no pointers and no membership rows) is an empty state
        to fix in Setup, not a partial outcome."""
        game = self._require_game(game_id)
        game = self._guard_mutable(game)          # <- the SEASON ROW LOCK
        # ONE effective team, resolved AND authorized under that lock, BEFORE
        # candidate discovery and before any write (#205 Part C).
        team_id = self._batch_team(game, team_id, authorized_team_id)
        candidates = self._auto_build_candidates(game, team_id)
        if not candidates:
            raise ValidationError(
                "Team has no players yet. Add or import players first."
            )
        return self._seat_batch(
            game, team_id, candidates, source="auto_build_roster",
            actor_id=actor_id, confirm=True, cap_to_open_slots=True)

    @staticmethod
    def _is_visible_game(g) -> bool:
        """Published, non-draft, non-cancelled, scheduled game — the
        GAME-SHAPE half of "counts for the Player Home Page" (#107), with no
        team in it at all.

        Every Player Home scan pairs it with
        :meth:`player_home_team_for_game` (see :meth:`_plays_in`). Ordinary
        participation is still decided by the one membership authority; the
        only additional selector is a fully matched accepted cross-team seat
        used solely for the player's own schedule. A player-backed-out seat
        remains visible as the route to re-confirm it."""
        return (not g.cancelled and g.published and not g.is_draft
                and g.start_time is not None)

    def _plays_in(self, g, player) -> bool:
        """Is ``g`` a game ``player`` is committed to on Player Home?

        THE SELECTION HALF OF THE SIDE RULE. This used to be
        ``_is_visible_team_game(g, player.team_id)`` — the permanent pointer
        — which is the same guessed side the private-game family spent five
        rounds removing from its READS, one step earlier: it decides WHICH
        GAME the Player Home Page is about. The pointer is stale for a Mover
        in BOTH directions, and both were live at b1cc02d on Memory, SQLite
        and PostgreSQL:

        * the pointer names a team NOT in the game while the seasonal
          membership names one that IS — a real Mover shown no next game at
          all (measured: pointer ``team_3``, membership ``team_1``,
          ``next_game: null``);
        * the pointer names a team IN the game while the membership has moved
          off it — a departed player shown a game they have no part in, and
          (before the caller's own fix) shown that team's private per-side
          state with it (measured: pointer ``team_1``, membership ``team_3``,
          ``team_status: "sub_search"``).

        Resolving SELECTION here rather than only at the read is deliberate:
        a Mover handed the WRONG GAME cannot be rescued by resolving the side
        correctly within it, and the availability POST the Player Home screen
        offers is addressed to ``next_game.game_id``.

        ``player_home_team_for_game`` keeps that membership rule, including
        ``team_for_game``'s permanent-pointer fallback for a game with NO
        LeagueSeason binding. It additionally recognizes only a fully
        matched accepted cross-team seat. That exception does not feed
        private-game authorization. A player-backed-out UNAVAILABLE row stays
        visible only as the ordinary re-confirm route; coach-REMOVED and
        malformed rows disappear.

        Same predicate, same order and the same authority
        :meth:`list_player_offers` has used since the cutover, so the Player
        Home scans cannot drift on what a player-visible game is."""
        return (self._is_visible_game(g)
                and self.player_home_team_for_game(g, player) is not None)

    def find_next_game_for_player(self, player_id: str) -> Optional[Game]:
        """The player's next published, non-cancelled game in chronological
        order — the Player Home Page's "next game" card (#107).
        Membership- or accepted-borrowed-seat-resolved: see
        :meth:`_plays_in`. A pure read helper — must NOT be
        @_transactional."""
        player = self.store.get_player(player_id)
        if player is None or not player.is_active:
            return None
        now = self.clock()
        upcoming = [
            g for g in self.store.all_games()
            if g.start_time is not None and g.start_time >= now
            and self._plays_in(g, player)
        ]
        upcoming.sort(key=lambda g: g.start_time)
        return upcoming[0] if upcoming else None

    def count_games_today_for_player(self, player_id: str) -> int:
        """How many of THIS PLAYER's games fall on today's date — the Player
        Home Page's "Tonight" summary card (#107). Membership- or
        accepted-borrowed-seat-resolved: see :meth:`_plays_in`. A pure read
        helper — must NOT be
        @_transactional."""
        player = self.store.get_player(player_id)
        if player is None or not player.is_active:
            return 0
        today = self.clock().date()
        return sum(1 for g in self.store.all_games()
                   if g.start_time is not None
                   and g.start_time.date() == today
                   and self._plays_in(g, player))

    def substitute_block_reason(self, player_id: str, game_id: str,
                                rstatus=None, ctx=None) -> Optional[str]:
        """Why ``player_id`` cannot enrol as a substitute for ``game_id`` right
        now, or ``None`` if they can. The single source of truth shared by the
        Home opportunity list (#107, skip silently when blocked) and the #110
        opportunity-detail view (show the reason). Does NOT consider an existing
        enrolment — being already enrolled is a separate state (it offers a
        Withdraw action), not a reason the player is ineligible.

        ``rstatus`` may be a pre-computed :class:`RosterStatus` for this
        game/team so a caller that already has one (the detail view) avoids a
        second ``compute_roster_status`` pass. ``ctx`` may likewise be the
        :class:`GameMembershipContext` a caller has ALREADY resolved for this
        exact ``(game, player)`` pair (``list_addable_players`` has one per
        row), so the whole surface answers from ONE resolution instead of
        re-deriving a second one that could disagree (#205 review blocker 2).
        A pure read helper — must NOT be @_transactional.
        """
        player = self.store.get_player(player_id)
        if player is None:
            return "Player not found."
        if not player.is_active:
            return "You are not an active player."
        game = self.store.get_game(game_id)
        if game is None:
            return "Game not found."
        # #205 cutover: membership-resolved for a LeagueSeason-bound game,
        # permanent-pointer for an unbound one — see
        # resolve_membership_context. ONE resolution answers BOTH the team
        # question here and the position question below.
        if ctx is None:
            ctx = self.resolve_membership_context(game, player)
        if ctx is None:
            return "You are not on a team in this game."
        team_id = ctx.team_id
        if game.cancelled:
            return "This game has been cancelled."
        if not game.published or game.is_draft:
            return "This game has not been published yet."
        # Preserve the established same-team boundary: the opportunity closes
        # only *after* puck drop. #287's half-open equality/expiry rule applies
        # to the new cross-team path, whose separate gate uses ``>=``.
        if game.start_time is None or game.start_time < self.clock():
            return "This game is no longer upcoming."
        # A locked roster rejects enrolment (enroll_substitute goes through
        # _guard_mutable) — surface that rather than advertise a dead end.
        if game.locked:
            return "The roster for this game is locked."
        if self.store.roster_entry_for_player(game_id, player_id) is not None:
            return "You are already on the roster for this game."
        # #205 review blocker 2: the season-scoped position for THIS game,
        # off the SAME context — never a second, independent resolution.
        needed = ctx.slot_type
        if rstatus is None:
            rstatus = self.compute_roster_status(game_id, team_id)
        open_slots = (rstatus.open_goalie_slots if needed == SlotType.GOALIE
                      else rstatus.open_skater_slots)
        if open_slots <= 0:
            return "There is no open slot for your position right now."
        return None

    def list_substitute_opportunities(
        self, player_id: str,
        active_by_game: Optional[Dict[str, SubstituteEnrollment]] = None,
    ) -> List[Game]:
        """Games where a team this player belongs to has an open slot
        matching their position and the player isn't already
        selected/enrolled — the Player Home Page's substitute-opportunities
        section (#107).

        "Belongs to" is the #205 membership resolution (via
        substitute_block_reason -> team_for_game): cross-boundary borrowing
        stays off, so this only ever surfaces games of teams the player
        resolves to. A pure read helper — must NOT be @_transactional.
        """
        player = self.store.get_player(player_id)
        if player is None or not player.is_active:
            return []
        if active_by_game is None:
            active_by_game = self.active_substitute_snapshot(player_id)
        opportunities = []
        for g in self.store.all_games():
            # Already enrolled/offered here → don't re-advertise (the detail
            # view still shows it, with a Withdraw action). Distinct from the
            # ineligibility reasons substitute_block_reason covers.
            existing_sub = active_by_game.get(g.id)
            if existing_sub is not None:
                continue
            if self.substitute_block_reason(player_id, g.id) is None:
                opportunities.append(g)
        opportunities.sort(key=lambda g: g.start_time)
        return opportunities

    def cross_team_substitute_block_reason(
        self, player_id: str, game_id: str, target_team_id: str,
        target_ctx: Optional["SubstituteTargetContext"] = None,
        observed_enrollment=_ACTIVE_ENROLLMENT_NOT_OBSERVED,
    ) -> Optional[str]:
        """Why a player cannot proactively volunteer for another game side.

        Unlike :meth:`substitute_block_reason`, this intentionally does not
        require an open slot.  Availability is recorded first; a coach may
        offer only after the target side actually has a matching vacancy.
        """
        player = self.store.get_player(player_id)
        game = self.store.get_game(game_id)
        if player is None or not player.is_active:
            return "You are not an active player."
        if game is None:
            return "Game not found."
        if target_ctx is None:
            target_ctx = self.resolve_substitute_target_context(
                game, player, target_team_id)
        if target_ctx is None or not target_ctx.cross_team:
            return ("You can only volunteer for another team in your exact "
                    "league season and division.")
        game_block = self.cross_team_game_block_reason(game)
        if game_block is not None:
            return game_block
        if self.store.roster_entry_for_player(game_id, player_id) is not None:
            return "You are already on the roster for this game."
        existing = (
            self._active_substitute_for_player(game_id, player_id)
            if observed_enrollment is _ACTIVE_ENROLLMENT_NOT_OBSERVED
            else observed_enrollment)
        if existing is not None and existing.team_id != target_team_id:
            return "You are already available for the other team in this game."
        return None

    def game_mutation_block_reason(self, game: Game) -> Optional[str]:
        """Read-only mirror of the canonical game write guard.

        This powers buttons and coach queue hints only; every mutation still
        takes the Season row lock through :meth:`_guard_mutable`. Resolving
        the Season through the LeagueSeason is essential: ``game.season_id``
        is only a denormalized value and may not be used to make an archived
        competition look writable.
        """
        try:
            season_id = season_guard.game_season_authority_id(
                self.store, game)
        except (NotFoundError, ValidationError):
            return "This game's season context needs repair before changes."
        if season_id is not None:
            season = self.store.get_season(season_id)
            if season is None:
                return "This game's season context needs repair before changes."
            if season_guard.season_is_read_only(season):
                return (
                    f"Season '{season.name}' is archived and read-only. "
                    "Reopen it before making changes.")
            if (season_guard.game_is_league_season_bound(game)
                    and game.season_id != season_id):
                return "This game's season context needs repair before changes."
        if game.cancelled:
            return "This game has been cancelled."
        if game.locked:
            return "The roster for this game is locked."
        return None

    def cross_team_game_block_reason(
        self, game: Game, *, as_of: Optional[datetime] = None,
    ) -> Optional[str]:
        """Why a cross-team opt-in cannot advance to an offer/accept.

        Existing availability and offers stay reachable for cleanup after a
        game leaves this state; only forward transitions are refused.
        """
        mutation_block = self.game_mutation_block_reason(game)
        if mutation_block is not None:
            return mutation_block
        if not game.published or game.is_draft:
            return "This game has not been published yet."
        decision_at = as_of if as_of is not None else self.clock()
        if game.start_time is None or game.start_time <= decision_at:
            return "This game is no longer upcoming."
        return None

    def _require_cross_team_game_actionable(
        self, game: Game, *, as_of: Optional[datetime] = None,
    ) -> None:
        reason = self.cross_team_game_block_reason(game, as_of=as_of)
        if reason is not None:
            raise NotEligibleError(reason)

    def _cross_team_opt_in_visible(self, game: Game) -> bool:
        """Whether a fresh cross-team availability choice is public now.

        This is intentionally the complete game-state half of
        :meth:`cross_team_substitute_block_reason`, without resolving either
        source or target.  Callers can therefore reject a hidden game before
        a guessed target reveals whether any private relationship exists.
        """
        return self.cross_team_game_block_reason(game) is None

    def list_cross_team_substitute_opportunities(
        self, player_id: str,
        active_by_game: Optional[Dict[str, SubstituteEnrollment]] = None,
    ) -> List["SubstituteGameChoice"]:
        """Future same-Division games where this player may volunteer.

        A game can yield two rows because availability belongs to a concrete
        target side.  Once one side is selected, only that checked choice is
        returned until it is withdrawn.
        """
        player = self.store.get_player(player_id)
        if player is None:
            return []
        if active_by_game is None:
            active_by_game = self.active_substitute_snapshot(player_id)
        choices = []
        for game in self.store.all_games():
            existing = active_by_game.get(game.id)
            if existing is not None:
                # OFFERED choices move to the dedicated Accept/Decline card.
                # Keeping one here as a checked availability box would expose
                # Withdraw as a second, conflicting terminal response.
                if (not self._is_cross_team_enrollment(existing)
                        or existing.status == SubstituteStatus.OFFERED):
                    continue
                target_ctx = self._cross_team_enrollment_context(
                    game, player, existing)
                # The row is the player's own durable opt-in.  Losing the
                # source membership or target registration must stop an
                # offer/accept, but must never make the row disappear and
                # strand the unique active enrollment.  The API renders a
                # stale row only from its target/slot snapshot and still lets
                # the player withdraw while the game is mutable.
                choices.append(SubstituteGameChoice(
                    game=game, target=target_ctx,
                    enrollment=existing))
                continue
            if not player.is_active:
                continue
            for target_team_id in (
                    game.home_team_id, game.away_team_id):
                if target_team_id is None:
                    continue
                target_ctx = self.resolve_substitute_target_context(
                    game, player, target_team_id)
                if (target_ctx is None or not target_ctx.cross_team
                        or self.cross_team_substitute_block_reason(
                            player_id, game.id, target_team_id,
                            target_ctx=target_ctx,
                            observed_enrollment=existing) is not None):
                    continue
                choices.append(SubstituteGameChoice(
                    game=game, target=target_ctx, enrollment=None))
        choices.sort(key=lambda c: (
            c.game.start_time, c.game.id,
            (c.target.target_team_id if c.target is not None
             else c.enrollment.team_id if c.enrollment is not None
             else "")))
        return choices

    def substitute_offer_block_reason(self, player_id: str, game_id: str,
                                      enrollment, rstatus=None,
                                      ctx=None) -> Optional[str]:
        """Why an OFFERED player cannot ACCEPT the offer right now, or None if
        they can (#112). Builds on substitute_block_reason (which already covers
        cancelled / unpublished / past / locked / no-open-slot for the player's
        position — the same guards accept_substitute enforces) and adds the
        offer-specific expiry check, so the detail view's pre-disable logic
        can't drift from what accept_substitute actually permits."""
        if self._has_cross_team_provenance(enrollment):
            player = self.store.get_player(player_id)
            game = self.store.get_game(game_id)
            target_ctx = self._cross_team_enrollment_context(
                game, player, enrollment)
            # ``None`` here means the exact PERSISTED source stint no longer
            # validates.  It must not be passed through the optional
            # ``target_ctx`` API, where None means "resolve a fresh context";
            # a replacement membership on the same team would otherwise make
            # the UI advertise Accept even though the mutation correctly
            # rejects the stale provenance.
            if target_ctx is None:
                return (
                    "This cross-team substitute enrollment is no longer "
                    "eligible in its original league season and division.")
            base = self.cross_team_substitute_block_reason(
                player_id, game_id, enrollment.team_id,
                target_ctx=target_ctx,
                observed_enrollment=enrollment)
            if base is None:
                if rstatus is None:
                    rstatus = self.compute_roster_status(
                        game_id, enrollment.team_id)
                open_slots = (
                    rstatus.open_goalie_slots
                    if enrollment.slot_type == SlotType.GOALIE
                    else rstatus.open_skater_slots)
                if open_slots <= 0:
                    base = "There is no open slot for your position right now."
        else:
            base = self.substitute_block_reason(
                player_id, game_id, rstatus, ctx=ctx)
        if base is not None:
            return base
        if enrollment.offer_expires_at is not None:
            decision_at = self.clock()
            expired = (
                decision_at >= enrollment.offer_expires_at
                if self._has_cross_team_provenance(enrollment)
                else decision_at > enrollment.offer_expires_at)
            if expired:
                return "This offer has expired."
        return None

    @staticmethod
    def cross_team_offer_deadline(
        game: Game, enrollment: SubstituteEnrollment,
    ) -> Optional[datetime]:
        """The immutable upper bound for one issued cross-team offer."""
        deadlines = tuple(value for value in (
            enrollment.offer_expires_at, game.start_time)
            if value is not None)
        return min(deadlines) if deadlines else None

    def cross_team_offer_deadline_passed(
        self, game: Game, enrollment: SubstituteEnrollment,
        *, as_of: Optional[datetime] = None,
    ) -> bool:
        deadline = self.cross_team_offer_deadline(game, enrollment)
        decision_at = as_of if as_of is not None else self.clock()
        return deadline is not None and decision_at >= deadline

    def list_player_offer_choices(
        self, player_id: str,
        active_by_game: Optional[Dict[str, SubstituteEnrollment]] = None,
    ) -> List["SubstituteOfferChoice"]:
        """Games where this player currently has an OFFERED substitute slot —
        a coach has offered them the spot and they must accept/decline (#112).
        Distinct from list_substitute_opportunities (which is the self-enrol
        pool and excludes already-offered games). Same-team offers retain the
        established visible/upcoming/member filter. A persisted cross-team
        offer is the player's own response row and remains visible for cleanup
        after publication/time/eligibility drift; its detail disables any
        transition the canonical service would refuse. A pure read helper —
        must NOT be @_transactional."""
        player = self.store.get_player(player_id)
        if player is None:
            return []
        if active_by_game is None:
            active_by_game = self.active_substitute_snapshot(player_id)
        now = self.clock()
        offers = []
        for g in self.store.all_games():
            sub = active_by_game.get(g.id)
            if sub is None or sub.status != SubstituteStatus.OFFERED:
                continue
            # A persisted cross-team offer is the player's own response row,
            # not discovery of somebody else's fixture.  Keep it visible for
            # Decline even after unpublish/puck-drop/eligibility drift; the
            # detail predicate disables Accept and mirrors lock/cancel for
            # Decline.  Otherwise an active unique row becomes unreachable.
            if self._has_cross_team_provenance(sub):
                offers.append(SubstituteOfferChoice(g, sub))
                continue
            # #205 cutover: an offer surfaces when the player RESOLVES to a
            # team in the game (membership for LeagueSeason-bound games,
            # permanent pointer for unbound ones) — otherwise an offered
            # membership-only substitute could never see their own offer.
            if (not player.is_active or not self._is_visible_game(g)
                    or g.start_time < now):
                continue
            if self.team_for_game(g, player) is None:
                continue
            offers.append(SubstituteOfferChoice(g, sub))
        offers.sort(key=lambda choice: choice.game.start_time)
        return offers

    def list_player_offers(self, player_id: str) -> List[Game]:
        """Compatibility projection of :meth:`list_player_offer_choices`."""
        return [choice.game
                for choice in self.list_player_offer_choices(player_id)]

    # Order substitutes for the coach outreach queue (#112): an enrolled sub
    # the coach can offer right now ranks ahead of one already offered, ahead
    # of terminal states (accepted/declined/…). Within a tier, an explicit
    # priority_rank wins, then name/id for a stable deterministic sort. This is
    # the deliberately-simple V1 ordering — no fairness/travel/skill scoring.
    _CANDIDATE_TIER = {"enrolled": 0, "offered": 1}

    def list_substitute_candidates(self, game_id: str,
                                   team_id: Optional[str] = None,
                                   rstatus=None) -> List[dict]:
        """The substitute outreach queue for a game/team (#112): each enrolled
        (and offered/terminal) substitute with whether the coach can offer them
        a slot right now, ordered enrolled-first. ``rstatus`` may be a
        pre-computed RosterStatus for this game/team so a caller that already
        has one avoids a second compute_roster_status pass. A pure read helper
        — must NOT be @_transactional. Returns plain dicts.

        THE HOME DEFAULT BELOW IS AN OPERATOR DEFAULT, NOT A FALLBACK FOR A
        SCOPED CALLER (#427 final blocker, round 3). ``team_id or
        game.home_team_id`` is the same silent shape that made ``get_board``
        hand an AWAY Coach the HOME pool, and it is retained here for the
        one caller it is correct for: an unscoped operator who named no
        side and may read either. A SCOPED caller never reaches it — the
        facade (:meth:`ApiService._workflow_side`) resolves the audience
        first and passes the TRUSTED server-resolved side for a Coach, so
        ``team_id`` is never empty on that path and HOME can never be served
        by default to a caller whose own side is AWAY. Proven by
        ``tests/test_private_game_sibling_routes.py``'s
        ``candidates_home_defaulted`` falsifier, which drops the trusted side
        on the way in and reddens exactly that assertion."""
        game = self._require_game(game_id)
        team_id = team_id or game.home_team_id
        if rstatus is None:
            rstatus = self.compute_roster_status(game_id, team_id)
        open_for = {
            SlotType.GOALIE: rstatus.open_goalie_slots,
            SlotType.SKATER: rstatus.open_skater_slots,
        }
        rows = []
        for sub in self.store.substitutes_for_game(game_id):
            player = self.store.get_player(sub.player_id)
            # ATTRIBUTION IS DURABLE; LIVENESS IS LIVE. Two different
            # questions, and this loop used to answer BOTH with the live
            # membership (#427 final blocker, round 2).
            #
            # THE DEFECT THAT FIXES. `/substitutes` correctly omits a pre-060
            # NULL-owner enrollment from BOTH Coaches — it cannot name the
            # side it was admitted on, and `durable_game_sides` refuses to
            # guess one. This queue served the SAME ROW to whichever Coach
            # the occupant happens to belong to TODAY, with `can_offer: True`.
            # Measured tri-store over real authenticated sessions at ae21c40:
            # the AWAY Coach's queue carried a NULL-owner row its own
            # `/substitutes` had just withheld, and the HOME Coach's carried
            # the mirrored one — two routes in one family, two authorities,
            # contradicting this blocker's own "never guess" rule.
            #
            # So WHICH SIDE this row belongs to is `sub.team_id`, the side it
            # was ADMITTED on (migration 060) — the same authority
            # `lineup_population`'s (b) population and `get_substitutes`
            # already key on, so all three cannot drift. A NULL owner names
            # no side and appears in NEITHER queue.
            if sub.team_id is None or sub.team_id != team_id:
                continue
            # WHETHER IT IS STILL LIVE stays a live question, unchanged: a
            # deactivated player's enrollment remains as history but drops out
            # of the outreach queue (#270 review) — never offer-able — and so
            # does one whose membership ended after enrollment. The durably
            # owned row is still visible to its owning Coach for cleanup on
            # the lineup screen (2f8eb73's "Needs cleanup" block, labelled
            # Ineligible and carrying only Withdraw); it simply cannot be
            # OFFERED from here.
            if player is None or not player.is_active:
                continue
            if self._has_cross_team_provenance(sub):
                live_for_target = (
                    self._cross_team_enrollment_context(
                        game, player, sub) is not None)
                # The owning lineup still carries the durable row as
                # cleanup-only state, but it leaves the live outreach queue
                # once the cross-team game can no longer advance.
                if self.cross_team_game_block_reason(game) is not None:
                    continue
            else:
                live_for_target = self.team_for_game(game, player) == team_id
            if not live_for_target:
                continue
            cross_team_game_ready = (
                not self._has_cross_team_provenance(sub)
                or self.cross_team_game_block_reason(game) is None)
            can_offer = (sub.status == SubstituteStatus.ENROLLED
                         and not game.locked and not game.cancelled
                         and cross_team_game_ready
                         and open_for.get(sub.slot_type, 0) > 0)
            rows.append({
                "player_id": sub.player_id, "name": player.name,
                # #205 review blocker 2: the position/slot_type PAIR must
                # agree — read both off the enrolled SubstituteEnrollment
                # itself (already season-scoped at enroll time, see
                # enroll_substitute), never re-derive one from the
                # permanent Player row and the other from the stint.
                "position": sub.position.value,
                "slot_type": sub.slot_type.value,
                "status": sub.status.value,
                "priority_rank": sub.priority_rank,
                "can_offer": can_offer,
            })
        rows.sort(key=lambda r: (
            self._CANDIDATE_TIER.get(r["status"], 2),
            r["priority_rank"] if r["priority_rank"] is not None else float("inf"),
            r["name"], r["player_id"]))
        return rows

    def list_addable_players(self, game_id: str, team_id: Optional[str] = None,
                             rstatus=None) -> List[dict]:
        """Active players of this game-team a coach could add as a
        substitute candidate right now (#114) — the pool is
        membership-resolved for a LeagueSeason-bound game and the permanent
        roster for an unbound one (#205 cutover, _players_for_game_team),
        then substitute_block_reason (the SAME gate the player-side
        opportunity list uses) returns None for them, and
        they aren't already an enrolled/offered substitute (that pair of
        states already show up in the outreach queue with an Offer action;
        re-adding them here would just hit enroll_substitute's own duplicate
        check). A pure read helper — must NOT be @_transactional.

        Same home default, same rule, same reason as
        :meth:`list_substitute_candidates` above: it is the UNSCOPED
        OPERATOR's default for an un-hinted read, and a scoped caller's side
        arrives already resolved by :meth:`ApiService._workflow_side`, so this
        line can never answer an AWAY Coach with HOME's pool (#427 final
        blocker, round 3)."""
        game = self._require_game(game_id)
        team_id = team_id or game.home_team_id
        if rstatus is None:
            rstatus = self.compute_roster_status(game_id, team_id)
        already_sub = {
            s.player_id for s in self.store.substitutes_for_game(game_id)
            if s.status.is_active_enrollment
        }
        rows = []
        for player, ctx in self._players_for_game_team(game, team_id):
            if ctx is None or not player.is_active or player.id in already_sub:
                continue
            if self.substitute_block_reason(
                    player.id, game_id, rstatus=rstatus,
                    ctx=ctx) is not None:
                continue
            # #205 review blocker 2: the season-scoped position for THIS
            # game, off the SAME context the pool and the gate above used.
            rows.append({
                "player_id": player.id, "name": player.name,
                "position": ctx.position.value,
                "slot_type": ctx.slot_type.value,
            })
        rows.sort(key=lambda r: (r["name"], r["player_id"]))
        return rows

    def add_substitute_candidate(
        self, game_id: str, player_id: str, actor_id: Optional[str] = None,
        authorized_team_id: Optional[str] = None,
    ) -> SubstituteEnrollment:
        """Coach/operator adds an eligible same-team player directly to the
        substitute pool (#114) — reuses enroll_substitute (the same method
        the player-side self-enroll flow calls), after first running the
        FULL eligibility gate substitute_block_reason already encodes (open
        slot, published/non-draft, not-yet-started, roster lock, cancelled,
        team membership, active, not already rostered). enroll_substitute
        itself does not check open-slot/publish-state/past-game, because a
        player only ever reaches it through their own pre-filtered
        opportunity list — a coach picking an arbitrary roster member has no
        such pre-filter, so this method supplies it instead of loosening
        enroll_substitute (which would change the player-facing path too).

        NOT @_transactional itself: enroll_substitute already is, and the
        in-memory/SQL transaction() context managers are not reentrant
        (matches copy_previous_roster's call to select_roster, above).

        AND THAT IS WHY ``authorized_team_id`` IS ONLY FORWARDED HERE, never
        checked here (#205, the transactional blocker). ``substitute_block_
        reason`` above is an UNLOCKED read OUTSIDE any transaction, so a
        Coach-team check placed beside it would be a SECOND PREFLIGHT — the
        very shape the ruling refuses ("The scope preflight may remain for
        fast denial, but it cannot be the authoritative write gate"). The
        authoritative comparison happens inside ``enroll_substitute``, after
        ``_guard_mutable`` has taken the canonical Season lock and before any
        write."""
        reason = self.substitute_block_reason(player_id, game_id)
        if reason is not None:
            raise NotEligibleError(reason)
        return self.enroll_substitute(game_id, player_id, actor_id=actor_id,
                                      authorized_team_id=authorized_team_id)

    def _game_label(self, game) -> str:
        # A pure read helper — must NOT be @_transactional. It is called from
        # inside the transactional lock/unlock/cancel methods (via
        # _notify_game_change); decorating it would open a nested transaction
        # and crash on SqlStore ("cannot start a transaction within a
        # transaction"). InMemoryStore's no-op transaction hid this (#87).
        def name(tid):
            t = self.store.get_team(tid) if tid else None
            return t.name if t else "TBD"
        return f"{name(game.home_team_id)} vs {name(game.away_team_id)}"

    def _notify_game_change(self, game, kind, title, message,
                            include_public=False):
        """Delivery-backed schedule-change notification to affected parties
        (both teams, active officials, optional public), honoring channel
        preferences (#81/#87)."""
        for tid in (game.home_team_id, game.away_team_id):
            if tid:
                _push_notification(self.store, self.clock, kind,
                                   NotificationAudience.COACH, title, message,
                                   audience_ref=tid, game_id=game.id)
        for a in self.store.assignments_for_game(game.id):
            if a.status.is_active:
                _push_notification(self.store, self.clock, kind,
                                   NotificationAudience.OFFICIAL, title, message,
                                   audience_ref=a.official_id, game_id=game.id)
        if include_public:
            _push_notification(self.store, self.clock, kind,
                               NotificationAudience.PUBLIC, title, message,
                               game_id=game.id)

    @_transactional
    def lock_roster(self, game_id: str, actor_id: Optional[str] = None) -> Game:
        game = self._require_game(game_id)
        game = self._guard_active_season(game)  # #159/#427 guard + re-fetch
        if game.cancelled:
            raise GameCancelledError("Game is cancelled.")
        was_locked = game.locked
        game.locked = True
        self.store.save_game(game)
        self._audit(game_id, AuditAction.ROSTER_LOCKED, actor_id=actor_id)
        self._notify(
            game_id,
            NotificationType.ROSTER_LOCKED,
            audience="team",
            message="Roster is locked for this game.",
        )
        if not was_locked:  # only on the transition (#87 idempotency)
            self._notify_game_change(
                game, NotificationKind.ROSTER_LOCKED, "Roster locked",
                f"The roster is locked for {self._game_label(game)}.")
        return game

    @_transactional
    def unlock_roster(self, game_id: str, actor_id: Optional[str] = None) -> Game:
        game = self._require_game(game_id)
        game = self._guard_active_season(game)  # #159/#427 guard + re-fetch
        was_locked = game.locked
        game.locked = False
        self.store.save_game(game)
        self._audit(game_id, AuditAction.ROSTER_UNLOCKED, actor_id=actor_id)
        if was_locked:  # only on the transition (#87 idempotency)
            self._notify_game_change(
                game, NotificationKind.ROSTER_UNLOCKED, "Roster unlocked",
                f"The roster is unlocked for {self._game_label(game)}.")
        return game

    def cancel_game(self, game_id: str, actor_id: Optional[str] = None) -> Game:
        """Cancel a fixture, preserve its ice history, and release occupancy.

        #428 makes cancellation one atomic state transition: snapshot the
        display-critical facility facts, detach the Game from its live IceSlot,
        release an ALLOCATED slot, cancel active substitute enrollments, and
        write exactly one audit.  Retrying an already-cancelled Game is a true
        no-op; legacy in-memory rows that are cancelled-but-attached are repaired
        without fabricating a second cancellation audit.

        The lock order matches every placement writer: Program -> Team -> Rink
        -> Season, with the IceSlot row locked only after the Season.  A move
        that changes the source between the locator read and these locks causes
        a clean transaction retry rather than snapshotting or freeing stale ice.
        """
        for attempt in range(3):
            try:
                with self.store.transaction():
                    return self._cancel_game_locked(game_id, actor_id)
            except _CancelGameRaced:
                if attempt == 2:
                    raise ConcurrencyConflictError(
                        "This game's ice changed while cancellation was being "
                        "processed; please retry.",
                        {"reason": "cancel_raced", "game_id": game_id})

    def _cancel_game_locked(self, game_id: str,
                            actor_id: Optional[str]) -> Game:
        game = self._require_game(game_id)  # pre-lock locator only
        authority_season_id = season_guard.game_season_authority_id(
            self.store, game)
        season = (self.store.get_season(authority_season_id)
                  if authority_season_id else None)
        if season is not None and season.program_id:
            self.store.get_program_for_update(season.program_id)
        for team_id in sorted({tid for tid in
                               (game.home_team_id, game.away_team_id) if tid}):
            self.store.get_team_for_update(team_id)

        # move_game shares these Team locks.  Once they are held, its current
        # source slot is stable; verify it still matches the locator used to
        # plan the Rink lock, otherwise retry from a fresh transaction.
        fresh = self._require_game(game_id)
        if (fresh.ice_slot_id != game.ice_slot_id
                or fresh.home_team_id != game.home_team_id
                or fresh.away_team_id != game.away_team_id
                or fresh.league_season_id != game.league_season_id
                or fresh.season_id != game.season_id):
            raise _CancelGameRaced()
        slot = (self.store.get_ice_slot(fresh.ice_slot_id)
                if fresh.ice_slot_id else None)
        rink_id = slot.rink_id if slot is not None else None
        if rink_id:
            self.store.get_rink_for_update(rink_id)

        # Season comes after Rink in the global placement lock order.  The
        # shared guard both checks archive state and re-fetches the Game under
        # the Season lock; verify that neither its authority nor its ice moved.
        fresh = self._guard_active_season(fresh)
        if (fresh.ice_slot_id != game.ice_slot_id
                or season_guard.game_season_authority_id(self.store, fresh)
                   != authority_season_id):
            raise _CancelGameRaced()

        was_cancelled = fresh.cancelled
        snapshot_values = (
            fresh.cancelled_ice_slot_id,
            fresh.cancelled_venue_id,
            fresh.cancelled_venue_name,
            fresh.cancelled_venue_timezone,
            fresh.cancelled_rink_id,
            fresh.cancelled_rink_name,
            fresh.cancelled_scheduled_start_time,
            fresh.cancelled_scheduled_end_time,
            fresh.cancelled_ice_start_time,
            fresh.cancelled_ice_end_time,
        )
        if any(value is not None for value in snapshot_values):
            if not all(value is not None for value in snapshot_values):
                raise IntegrityConflictError(
                    "The game's cancellation history is incomplete; no live "
                    "ice was changed.",
                    {"reason": "cancellation_history_incomplete",
                     "game_id": fresh.id})
            if not was_cancelled:
                raise IntegrityConflictError(
                    "An active game cannot already carry cancellation "
                    "history; no live ice was changed.",
                    {"reason": "unexpected_cancellation_history",
                     "game_id": fresh.id})
        if was_cancelled and fresh.ice_slot_id is None:
            return fresh

        released_slot_id = fresh.ice_slot_id
        if released_slot_id:
            locked_slot = self.store.get_ice_slot_for_update(released_slot_id)
            if locked_slot is None and fresh.cancelled_ice_slot_id is None:
                raise IntegrityConflictError(
                    "The game's ice slot no longer exists, so cancellation "
                    "cannot preserve its history.",
                    {"reason": "ice_slot_not_found",
                     "ice_slot_id": released_slot_id})

            # A prior partial snapshot can only come from legacy/manual data;
            # never overwrite it with whichever live facility facts happen to
            # exist now.  A new cancellation requires the complete live chain.
            if fresh.cancelled_ice_slot_id is None:
                rink = (self.store.get_rink(locked_slot.rink_id)
                        if locked_slot is not None else None)
                venue = (self.store.get_venue(rink.venue_id)
                         if rink is not None else None)
                if rink is None or venue is None:
                    raise IntegrityConflictError(
                        "The game's facility hierarchy is incomplete, so "
                        "cancellation cannot preserve its history.",
                        {"reason": "facility_history_unresolvable",
                         "ice_slot_id": released_slot_id})
                fresh.cancelled_ice_slot_id = locked_slot.id
                fresh.cancelled_venue_id = venue.id
                fresh.cancelled_venue_name = venue.name
                fresh.cancelled_venue_timezone = venue.timezone
                fresh.cancelled_rink_id = rink.id
                fresh.cancelled_rink_name = rink.name
                fresh.cancelled_scheduled_start_time = fresh.start_time
                fresh.cancelled_scheduled_end_time = fresh.end_time
                fresh.cancelled_ice_start_time = locked_slot.start_time
                fresh.cancelled_ice_end_time = locked_slot.end_time

            fresh.ice_slot_id = None
            # A legacy cancelled row can coexist with a replacement active
            # Game on the same slot.  Repairing the legacy row must not mark
            # that occupied slot AVAILABLE.
            other_active = any(
                g.id != fresh.id and not g.cancelled
                and g.ice_slot_id == released_slot_id
                for g in self.store.all_games())
            if (locked_slot is not None and not other_active
                    and locked_slot.status == IceSlotStatus.ALLOCATED):
                locked_slot.status = IceSlotStatus.AVAILABLE
                self.store.save_ice_slot(locked_slot)

        fresh.cancelled = True
        self.store.save_game(fresh)

        # Only the actual false -> true transition owns lifecycle effects.
        # The legacy repair path above intentionally adds no second audit,
        # notification, or substitute cancellation.
        if not was_cancelled:
            for sub in self.store.substitutes_for_game(game_id):
                if sub.status.is_active_enrollment:
                    sub.status = SubstituteStatus.CANCELLED
                    self.store.save_substitute(sub)
            self._audit(
                game_id, AuditAction.GAME_CANCELLED, actor_id=actor_id,
                detail={
                    "released_ice_slot_id": fresh.cancelled_ice_slot_id,
                    "venue_id": fresh.cancelled_venue_id,
                    "venue_name": fresh.cancelled_venue_name,
                    "rink_id": fresh.cancelled_rink_id,
                    "rink_name": fresh.cancelled_rink_name,
                    "scheduled_start_time": (
                        fresh.cancelled_scheduled_start_time.isoformat()
                        if fresh.cancelled_scheduled_start_time else None),
                    "scheduled_end_time": (
                        fresh.cancelled_scheduled_end_time.isoformat()
                        if fresh.cancelled_scheduled_end_time else None),
                })
            self._notify_game_change(
                fresh, NotificationKind.GAME_CANCELLED, "Game cancelled",
                f"{self._game_label(fresh)} has been cancelled.",
                include_public=True)
        return fresh

    # ====================================================================
    # roster status engine
    # ====================================================================
    def _side_data(self, game_id: str, team_id: str):
        """Everything ``_slot_summaries``/``compute_roster_status`` need for
        ONE side of a game.

        A SEATED ROW'S SIDE AND BUCKET COME OFF THE ROW (#205 blocker 5,
        round 2 — owner comment 5370391045). Round 1 moved this method off
        the permanent ``player.team_id`` pointer and onto the game-scoped
        membership resolution, which fixed the pointer-THIRD/current-HOME
        happy path but left the blocker open in a second direction: it
        re-derived the side of EVERY historical/accepted row from the
        player's CURRENT eligible membership, on every read. A legitimate
        status change (``set_season_roster_membership_status`` to inactive/
        injured/applicant, a release/transfer, or the loss of the Team's
        registration) therefore ERASED a seated row's attribution without
        removing or transitioning the row — the row stayed ACCEPTED and
        occupying in storage while dropping out of the governed count, the
        side degraded to ``draft``/"No players selected yet.", and a second
        player's offer AND accept then both succeeded past the target.

        So ``entry.attribution`` — written at seating time from the
        validated context that authorized it (migration 061) — decides both
        the SIDE this row counts against and the BUCKET it counts in.
        Nothing is re-derived here, which is what makes the gate
        (``_require_open_slot`` via ``_slot_summaries``) and the report
        (``compute_roster_status``) ONE rule rather than two that can
        disagree.

        SUBSTITUTE ENROLLMENTS STAY LIVE, deliberately. An enrollment is a
        CANDIDACY, not a seating: a player whose participation just ended
        must drop out of the outreach queue immediately, which is exactly
        what re-resolving does. Only bodies already in a slot became
        durable. So ``matched_subs`` still resolves through
        ``resolve_membership_contexts_for_game`` (batched, two queries,
        never one per row).

        NULL ATTRIBUTION FAILS CLOSED, WITHOUT GUESSING (owner ruling,
        2026-08-22). A row written before migration 061 has no attribution
        and there is no honest way to reconstruct one — see that migration's
        header for why neither the permanent pointer nor membership history
        can answer. Such a row is charged as occupying on EVERY side of its
        game and in BOTH buckets, for as long as its status occupies a slot.
        That names no side (so it can never attribute the row to the WRONG
        one), consults no live state at all (so no lookup can answer the
        opposite team), and can only ever REDUCE an open count — so it is
        incapable of reopening a slot and therefore incapable of admitting
        the overfill this blocker is about. It over-refuses instead, which
        is loud and recoverable; silently reopening the slot is neither.

        Returns ``(summaries, matched_entries, matched_subs)`` — the
        ``SlotType -> SlotSummary`` dict, the list of
        ``(GameRosterEntry, Player)`` pairs charged to ``team_id``, and the
        list of ``SubstituteEnrollment`` rows resolved to ``team_id``.
        """
        game = self._require_game(game_id)
        entries = self.store.roster_for_game(game_id)
        subs = self.store.substitutes_for_game(game_id)
        bound = season_guard.game_is_league_season_bound(game)
        contexts = (self.resolve_membership_contexts_for_game(game)
                    if bound else {})

        def context_of(player_id, player):
            if bound:
                return contexts.get(player_id)
            # Unbound game: no membership to resolve — the permanent
            # pointer is the only source, exactly the fallback
            # resolve_membership_context itself applies there.
            return self.resolve_membership_context(game, player)

        targets = {
            SlotType.GOALIE: game.target_goalies,
            SlotType.SKATER: game.target_skaters,
        }
        occupied = {SlotType.GOALIE: 0, SlotType.SKATER: 0}
        confirmed = {SlotType.GOALIE: 0, SlotType.SKATER: 0}
        both = (SlotType.GOALIE, SlotType.SKATER)

        matched_entries = []
        for entry in entries:
            player = self.store.get_player(entry.player_id)
            if player is None:
                continue
            attribution = entry.attribution
            if attribution is None:
                buckets = both          # pre-061 row: fail closed everywhere
            elif attribution[0] == team_id:
                buckets = (attribution[1],)
            else:
                continue                # seated on the other side
            matched_entries.append((entry, player))
            for st in buckets:
                if entry.status.occupies_slot:
                    occupied[st] += 1
                if entry.status.is_confirmed_body:
                    confirmed[st] += 1

        matched_subs = []
        for sub in subs:
            player = self.store.get_player(sub.player_id)
            if player is None:
                continue
            # SIDE FROM THE ROW, LIVENESS FROM THE MEMBERSHIP (#427 final
            # blocker, round 2). This charged an enrollment to whichever side
            # its occupant's CURRENT membership named and consulted
            # `sub.team_id` not at all, so a pre-060 NULL-owner row — one that
            # `get_substitutes`, `lineup_population` and `durable_game_sides`
            # all refuse to place on any side — was still COUNTED into one
            # side's `substitutes_enrolled` and `substitutes_available`.
            # Measured tri-store: `substitutes_enrolled` read 2 for HOME and 2
            # for AWAY where only one durably owned ENROLLED row existed on
            # each, and a Coach's own /roster-status therefore disagreed with
            # their own /substitutes about how many enrollments they had.
            #
            # Both clauses are load-bearing and they are NOT the same test.
            # `sub.team_id == team_id` answers "was this row admitted on this
            # side" and is durable, so a transfer cannot move an existing row
            # to the opponent's count. `ctx.team_id == team_id` answers "is
            # this candidacy still real" and stays live, which is the
            # deliberate choice this method's own docstring records
            # ("SUBSTITUTE ENROLLMENTS STAY LIVE"): an enrollment is a
            # CANDIDACY, not a seating, so a participation that has ended
            # must drop out of the available count immediately — while the
            # row itself stays visible to its owning Coach for cleanup.
            #
            # This can only ever REDUCE `substitutes_available`, so like the
            # NULL-attribution seat rule above it can never reopen a slot and
            # is incapable of admitting overfill.
            if sub.team_id is None or sub.team_id != team_id:
                continue
            if self._has_cross_team_provenance(sub):
                target_ctx = self._cross_team_enrollment_context(
                    game, player, sub)
                live_for_target = target_ctx is not None
            else:
                ctx = context_of(sub.player_id, player)
                live_for_target = (
                    ctx is not None and ctx.team_id == team_id)
            if live_for_target:
                matched_subs.append(sub)

        subs_available = {SlotType.GOALIE: 0, SlotType.SKATER: 0}
        for sub in matched_subs:
            if sub.status == SubstituteStatus.ENROLLED:
                subs_available[sub.slot_type] += 1

        result = {}
        for st in (SlotType.GOALIE, SlotType.SKATER):
            open_count = max(0, targets[st] - occupied[st])
            if open_count == 0:
                slot_status = SlotStatus.FULL
            elif subs_available[st] > 0:
                slot_status = SlotStatus.NEEDS_COACH_DECISION
            else:
                slot_status = SlotStatus.OPEN
            result[st] = SlotSummary(
                slot_type=st,
                target_count=targets[st],
                confirmed_count=confirmed[st],
                occupied_count=occupied[st],
                open_count=open_count,
                substitutes_available=subs_available[st],
                status=slot_status,
            )
        return result, matched_entries, matched_subs

    def _slot_summaries(self, game_id: str, team_id: str):
        summaries, _entries, _subs = self._side_data(game_id, team_id)
        return summaries

    def compute_roster_status(
        self, game_id: str, team_id: Optional[str] = None
    ) -> RosterStatus:
        game = self._require_game(game_id)
        # Home and away lineups are computed independently (#25). Default to the
        # home side so existing callers/behaviour are unchanged.
        team_id = team_id or game.home_team_id
        summaries, matched_entries, matched_subs = self._side_data(
            game_id, team_id)
        entries = [e for (e, _player) in matched_entries]
        goalie = summaries[SlotType.GOALIE]
        skater = summaries[SlotType.SKATER]

        subs_enrolled = sum(
            1 for s in matched_subs if s.status == SubstituteStatus.ENROLLED
        )

        open_total = goalie.open_count + skater.open_count
        subs_for_open = (
            (goalie.open_count > 0 and goalie.substitutes_available > 0)
            or (skater.open_count > 0 and skater.substitutes_available > 0)
        )

        status, action_required, message = self._derive_status(
            game, entries, goalie, skater, open_total, subs_for_open
        )

        return RosterStatus(
            game_id=game_id,
            team_id=team_id,
            target_goalies=goalie.target_count,
            confirmed_goalies=goalie.confirmed_count,
            open_goalie_slots=goalie.open_count,
            target_skaters=skater.target_count,
            confirmed_skaters=skater.confirmed_count,
            open_skater_slots=skater.open_count,
            substitutes_enrolled=subs_enrolled,
            status=status,
            action_required=action_required,
            message=message,
        )

    def _derive_status(
        self, game, entries, goalie, skater, open_total, subs_for_open
    ):
        if game.cancelled:
            return GameStatus.FINAL, False, "Game cancelled."
        occupying = [e for e in entries if e.status.occupies_slot]
        if not occupying:
            return GameStatus.DRAFT, False, "No players selected yet."
        if game.locked:
            return GameStatus.LOCKED, False, "Roster is locked for this game."

        if open_total > 0:
            slot_phrase = self._open_slot_phrase(goalie, skater)
            if subs_for_open:
                return (
                    GameStatus.NEEDS_SUBSTITUTE,
                    True,
                    f"{slot_phrase} Substitutes are available — coach decision needed.",
                )
            return (
                GameStatus.OPEN_SLOT,
                True,
                f"{slot_phrase} No substitutes enrolled.",
            )

        # All target slots are occupied.
        all_confirmed = all(e.status.is_confirmed_body for e in occupying)
        if all_confirmed:
            return GameStatus.ROSTER_CONFIRMED, False, "Roster confirmed."
        return (
            GameStatus.AWAITING_RESPONSES,
            False,
            "Awaiting player responses.",
        )

    @staticmethod
    def open_slot_phrase(open_goalies: int, open_skaters: int) -> str:
        """"2 skater slots open." — the SUBSTITUTE-FREE half of an
        open-slot message, from the two counts alone.

        Split out of :meth:`_derive_status`'s two open-slot branches (#427)
        because both of the messages it builds go on to assert SUBSTITUTE
        state — "Substitutes are available — coach decision needed." and
        "No substitutes enrolled." — and the assigned-official projection may
        report that a side is short without disclosing either. Numeric
        arguments rather than :class:`SlotSummary` for exactly that reason:
        the projection has the counts and deliberately does not have the
        substitute-bearing summaries."""
        parts = []
        if open_goalies > 0:
            unit = "goalie slot" if open_goalies == 1 else "goalie slots"
            parts.append(f"{open_goalies} {unit} open.")
        if open_skaters > 0:
            unit = "skater slot" if open_skaters == 1 else "skater slots"
            parts.append(f"{open_skaters} {unit} open.")
        return " ".join(parts)

    @classmethod
    def _open_slot_phrase(cls, goalie: SlotSummary, skater: SlotSummary) -> str:
        return cls.open_slot_phrase(goalie.open_count, skater.open_count)
