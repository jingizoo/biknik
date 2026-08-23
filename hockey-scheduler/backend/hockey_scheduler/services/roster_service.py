"""Roster status engine and substitute workflow.

This is the heart of the slice. It is pure business logic over the store: it
never performs network/disk I/O and it never calls ``datetime.now()`` itself
(a clock is injected) so every rule is deterministic and unit-testable.

Every state-changing method appends an :class:`AuditLog` entry and, where the
use case calls for it, emits a :class:`NotificationEvent`.
"""

import functools
from datetime import datetime, timezone
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
from .season_guard import require_active_season


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


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


def _transactional(fn):
    """Wrap a mutating service method in a single store transaction."""
    @functools.wraps(fn)
    def wrapper(self, *args, **kwargs):
        with self.store.transaction():
            return fn(self, *args, **kwargs)
    return wrapper


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


class RosterService:
    def __init__(self, store: InMemoryStore, clock: Callable[[], datetime] = _utcnow):
        self.store = store
        self.clock = clock

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
        cancelled Game or clobbers a relocation."""
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
        if player is None:
            raise NotFoundError(f"Player {player_id} not found.")
        if not player.is_active:
            raise NotEligibleError(f"{player.name} is not an active player.")
        return player

    def _guard_active_season(self, game: Game) -> None:
        """An archived Season is read-only (#159): its Games accept no roster,
        availability, substitute, lock, or cancel changes. Row-locks the Season
        (must run inside the caller's transaction) so the check is linearizable
        with ``archive_season``. Shared by ``_guard_mutable`` and the few
        mutations that legitimately bypass the cancelled/locked guard."""
        if game.season_id:
            require_active_season(self.store, game.season_id)

    def _guard_mutable(self, game: Game) -> None:
        """Guard for operations that change the committed roster."""
        self._guard_active_season(game)  # #159 — archived Season is read-only
        if game.cancelled:
            raise GameCancelledError("Game is cancelled.")
        if game.locked:
            raise RosterLockedError("Roster is locked. Unlock to make changes.")

    def _audit(
        self,
        game_id: str,
        action: AuditAction,
        actor_id: Optional[str] = None,
        subject_player_id: Optional[str] = None,
        detail: Optional[dict] = None,
    ) -> AuditLog:
        entry = AuditLog(
            id=self.store.next_id("audit"),
            game_id=game_id,
            action=action,
            at=self.clock(),
            actor_id=actor_id,
            subject_player_id=subject_player_id,
            detail=detail or {},
        )
        return self.store.add_audit(entry)

    def _notify(
        self,
        game_id: str,
        type_: NotificationType,
        audience: str,
        message: str,
        subject_player_id: Optional[str] = None,
    ) -> NotificationEvent:
        event = NotificationEvent(
            id=self.store.next_id("notif"),
            game_id=game_id,
            type=type_,
            audience=audience,
            message=message,
            at=self.clock(),
            subject_player_id=subject_player_id,
        )
        return self.store.add_notification(event)

    # ====================================================================
    # roster selection
    # ====================================================================
    @_transactional
    def select_roster(
        self, game_id: str, player_ids: List[str], actor_id: Optional[str] = None
    ) -> List[GameRosterEntry]:
        game = self._require_game(game_id)
        self._guard_mutable(game)

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
        bound = bool(game.league_season_id)
        contexts = (self.resolve_membership_contexts_for_game(game)
                    if bound else {})

        entries: List[GameRosterEntry] = []
        for player_id in player_ids:
            player = locked_players[player_id]
            if player is None:
                raise NotFoundError(f"Player {player_id} not found.")
            ctx = (contexts.get(player_id) if bound
                   else self.resolve_membership_context(game, player))
            if ctx is None:
                raise NotEligibleError(
                    f"{player.name} is not on either team in this game."
                )
            if not player.is_active:
                raise NotEligibleError(f"{player.name} is not an active player.")

            now = self.clock()
            existing = self.store.roster_entry_for_player(game_id, player_id)
            if existing is not None:
                if existing.status.occupies_slot:
                    # idempotent: already selected. The attribution it is
                    # ALREADY seated on stands — re-selecting an occupying
                    # row is a no-op, not a re-seat, so it must not silently
                    # re-attribute a row (including a pre-061 row, whose
                    # NULL attribution stays NULL and stays fail-closed).
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
    ) -> GameAvailability:
        game = self._require_game(game_id)
        self._guard_active_season(game)  # #159 read-only guard
        if game.cancelled:
            raise GameCancelledError("Game is cancelled.")
        if game.locked:
            raise RosterLockedError("Roster is locked. Unlock to make changes.")
        player = self._require_player(player_id)

        # Validate the roster-entry transition BEFORE persisting anything so a
        # rejected re-confirm leaves no partial availability/audit state.
        entry = self.store.roster_entry_for_player(game_id, player_id)
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
                attribution = self._authorize_seated_side(
                    game, player, entry, "re-confirm")
                if attribution is None:
                    self._refuse_unattributed(player, "re-confirm")
                side, st = attribution
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
                attribution = self._authorize_seated_side(
                    game, player, entry, "confirm")
                if attribution is None:
                    # NULL DURABLE ATTRIBUTION FAILS CLOSED HERE TOO —
                    # owner ruling, PR #427, 2026-08-22 (Decision 3): "A
                    # selected legacy row with no team_side/seated_position
                    # cannot prove that its live team matches the side it
                    # holds. Reject with attribution_missing before any
                    # attempted write, without adding an open-slot check."
                    #
                    # This REVERSES the previous round, which let a pre-061
                    # row through on this path on the reasoning that
                    # confirming "takes nothing back". That reasoning is
                    # overruled: the question this branch asks is not "is a
                    # slot being taken?" but "can this player be shown to
                    # be eligible for the side this row sits on?", and a row
                    # that names no side can never answer it. Silence is
                    # not a match.
                    #
                    # Still NO open-slot check — the refusal is about
                    # unprovable authorization, not about occupancy — and
                    # still raised BEFORE the GameAvailability upsert below,
                    # which is ``set_availability``'s first write.
                    self._refuse_unattributed(player, "confirm")

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
    ) -> Optional[Tuple[str, SlotType]]:
        """AUTHORIZE — "MAY THIS PLAYER still hold the SIDE this row is
        seated on?" — for every PLAYER-DRIVEN transition into CONFIRMED.

        ONE gate, called from both of ``set_availability``'s routes to
        CONFIRMED (the re-confirm of a backed-out row, and the confirm of a
        row that never backed out), because they ask the identical question
        and a second copy would be free to answer it differently. Written
        as a helper for exactly that reason: a change here is a change to
        both, and a mutation here breaks both.

        The answer comes from the LIVE context and from nothing else.
        Durable attribution records what the row HOLDS; it says nothing
        about who is still entitled to hold it. If the stored value could
        authorize its own re-use, the row would be a standing permission
        that outlives the participation that granted it. Resolution fails
        CLOSED (``_require_membership_context`` — the #270 deactivation
        gate), so a player with no live context at all is refused before
        any comparison is reached.

        Returns the row's ``(team_side, slot_type)`` attribution, or
        ``None`` for a pre-061 row that carries none — because the
        re-confirm caller needs the pair to run its slot gate, and the
        confirm caller does not. What the ``None`` MEANS is no longer
        caller-specific: both callers now refuse it, through the shared
        ``_refuse_unattributed`` (owner ruling, PR #427, Decision 3).

        ``action`` is the caller's verb, so the message names the refused
        transition; the machine-readable ``details`` are identical on both
        paths by construction.
        """
        ctx = self._require_membership_context(game, player)
        attribution = entry.attribution
        if attribution is not None and ctx.team_id != attribution[0]:
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
        by calling here — a mutation to this message or reason breaks both
        paths' tests together, which is the property the shared
        ``_authorize_seated_side`` already gives the mismatch refusal.

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
        self._guard_mutable(game)
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
        )
        if not removed:
            self._notify(
                game.id,
                NotificationType.PLAYER_BACKED_OUT,
                audience="coach",
                message="A selected player is unavailable.",
                subject_player_id=entry.player_id,
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
        self, game_id: str, player_id: str, actor_id: Optional[str] = None
    ) -> SubstituteEnrollment:
        game = self._require_game(game_id)
        self._guard_mutable(game)
        player = self._require_player(player_id)

        # #205 cutover: membership-resolved for a LeagueSeason-bound game,
        # permanent-pointer for an unbound one — see
        # resolve_membership_context. ONE resolution serves both the gate and
        # the enrolled row's season-scoped position below (#205 review
        # blocker 2: team and position are never re-read independently).
        ctx = self.resolve_membership_context(game, player)
        if ctx is None:
            raise NotEligibleError(
                f"{player.name} is not eligible (no membership with a team "
                f"in this game; cross-team borrowing is off)."
            )
        if not player.is_active:
            raise NotEligibleError(f"{player.name} is not an active player.")

        # A player who already has any roster entry for this game — selected,
        # confirmed, or even backed out/removed — is not part of the
        # "not selected" substitute pool and may not enroll.
        entry = self.store.roster_entry_for_player(game_id, player_id)
        if entry is not None:
            raise AlreadySelectedError(
                f"{player.name} was already selected for this game."
            )

        existing = self.store.substitute_for_player(game_id, player_id)
        if existing and existing.status in (
            SubstituteStatus.ENROLLED,
            SubstituteStatus.OFFERED,
        ):
            raise ValidationError(f"{player.name} is already enrolled as a substitute.")

        sub = SubstituteEnrollment(
            id=self.store.next_id("sub"),
            game_id=game_id,
            player_id=player_id,
            # #205 review blocker 2: the season-scoped position for THIS
            # stint, taken off the SAME context the gate above accepted —
            # never a second, independently-resolved read.
            position=ctx.position,
            status=SubstituteStatus.ENROLLED,
            enrolled_at=self.clock(),
        )
        self.store.add_substitute(sub)
        self._audit(
            game_id,
            AuditAction.SUBSTITUTE_ENROLLED,
            actor_id=actor_id,
            subject_player_id=player_id,
        )
        self._notify(
            game_id,
            NotificationType.SUBSTITUTE_ENROLLED,
            audience="coach",
            message="A player enrolled as substitute.",
            subject_player_id=player_id,
        )
        return sub

    @_transactional
    def withdraw_substitute(
        self, game_id: str, player_id: str, actor_id: Optional[str] = None
    ) -> SubstituteEnrollment:
        game = self._require_game(game_id)
        self._guard_mutable(game)
        sub = self._require_active_enrollment(game_id, player_id)
        was_offered = sub.status == SubstituteStatus.OFFERED
        sub.status = SubstituteStatus.WITHDRAWN
        self.store.save_substitute(sub)
        self._audit(
            game_id,
            AuditAction.SUBSTITUTE_WITHDRAWN,
            actor_id=actor_id,
            subject_player_id=player_id,
        )
        if was_offered:
            # An offered substitute backing out: notify the coach.
            self._notify(
                game_id,
                NotificationType.PLAYER_BACKED_OUT,
                audience="coach",
                message="An offered substitute withdrew. The slot is open again.",
                subject_player_id=player_id,
            )
        return sub

    @_transactional
    def offer_substitute(
        self,
        game_id: str,
        player_id: str,
        actor_id: Optional[str] = None,
        offer_expires_at: Optional[datetime] = None,
    ) -> SubstituteEnrollment:
        game = self._require_game(game_id)
        self._guard_mutable(game)
        player = self._require_active_player(player_id)   # fail closed on deactivation
        sub = self.store.substitute_for_player(game_id, player_id)
        if sub is None or sub.status != SubstituteStatus.ENROLLED:
            raise InvalidTransitionError(
                "Only an enrolled substitute can be offered a slot."
            )
        team_id = self._require_membership_context(game, player).team_id
        self._require_open_slot(game_id, sub.slot_type, team_id)
        sub.status = SubstituteStatus.OFFERED
        sub.offered_at = self.clock()
        sub.offer_expires_at = offer_expires_at
        # #205 blocker 3: snapshot the team this offer was validated against
        # — free, _require_team_for_game already resolved it above — so a
        # later decline can read a durable value instead of re-resolving
        # membership that may have ended by then (same pattern `position`
        # already uses via position_for_game at enroll time).
        sub.team_id = team_id
        self.store.save_substitute(sub)
        self._audit(
            game_id,
            AuditAction.SUBSTITUTE_OFFERED,
            actor_id=actor_id,
            subject_player_id=player_id,
        )
        self._notify(
            game_id,
            NotificationType.SUBSTITUTE_OFFERED,
            audience="player",
            message="A game slot is available. Accept?",
            subject_player_id=player_id,
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
        self, game_id: str, player_id: str, actor_id: Optional[str] = None
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
            self._guard_mutable(game)
            self._require_active_player(player_id)   # fail closed on deactivation
            sub = self.store.substitute_for_player(game_id, player_id)
            if sub is None or sub.status != SubstituteStatus.OFFERED:
                raise InvalidTransitionError("No active offer to accept.")
            # A game whose start has passed can't be joined — an offer with no
            # expiry (offer_expires_at=None) would otherwise stay acceptable
            # forever, letting a player onto a game that already happened (#112).
            if game.start_time is not None and self.clock() > game.start_time:
                raise InvalidTransitionError("This game is no longer upcoming.")
            # Offers can expire: a lapsed offer returns the player to the pool.
            if sub.offer_expires_at and self.clock() > sub.offer_expires_at:
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
        self, game, sub, player_id: str, actor_id: Optional[str]
    ) -> GameRosterEntry:
        # Runs inside accept_substitute's transaction (the game/sub were fetched
        # and validated within that same unit, so no interleaving is possible).
        # First-accepted-wins: the slot must still be open. The side the offer
        # counts against is the game-resolved team (#205 cutover) — and
        # resolution failing here (membership ended since the offer) fails
        # closed, same posture as the #270 deactivation gate.
        team_id = self._require_membership_context(
            game, self.store.get_player(player_id)).team_id
        self._require_open_slot(game.id, sub.slot_type, team_id)
        game_id = game.id
        sub.status = SubstituteStatus.ACCEPTED
        sub.accepted_at = self.clock()
        self.store.save_substitute(sub)
        # #205 blocker 5 round 2: the row records the EXACT (side, bucket)
        # pair the gate one line above just accepted — never a second
        # resolution that could answer differently.
        entry = self._add_to_roster_entry(game, player_id, team_id,
                                          sub.position)
        self._audit(
            game_id,
            AuditAction.SUBSTITUTE_ACCEPTED,
            actor_id=actor_id,
            subject_player_id=player_id,
        )
        self._notify(
            game_id,
            NotificationType.SUBSTITUTE_ACCEPTED,
            audience="coach",
            message="Substitute accepted and was added to roster.",
            subject_player_id=player_id,
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
        self, game_id: str, player_id: str, actor_id: Optional[str] = None
    ) -> SubstituteEnrollment:
        game = self._require_game(game_id)
        self._guard_mutable(game)
        sub = self.store.substitute_for_player(game_id, player_id)
        if sub is None or sub.status != SubstituteStatus.OFFERED:
            raise InvalidTransitionError("No active offer to decline.")
        sub.status = SubstituteStatus.DECLINED
        sub.declined_at = self.clock()
        self.store.save_substitute(sub)
        self._audit(
            game_id,
            AuditAction.SUBSTITUTE_DECLINED,
            actor_id=actor_id,
            subject_player_id=player_id,
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
        self, game_id: str, player_id: str, actor_id: Optional[str] = None
    ) -> GameRosterEntry:
        """Coach override: offer + accept in one step (audited)."""
        game = self._require_game(game_id)
        self._guard_mutable(game)
        player = self._require_active_player(player_id)   # fail closed on deactivation
        sub = self.store.substitute_for_player(game_id, player_id)
        if sub is None or sub.status not in (
            SubstituteStatus.ENROLLED,
            SubstituteStatus.OFFERED,
        ):
            raise NotEnrolledError(
                "Player must be an enrolled/offered substitute to be added."
            )
        # ONE resolution, held in a name, so the gate below and the durable
        # attribution written into the row are provably the same decision
        # (#205 blocker 5 round 2) rather than two independent lookups.
        ctx = self._require_membership_context(game, player)
        self._require_open_slot(game_id, sub.slot_type, ctx.team_id)
        sub.status = SubstituteStatus.ACCEPTED
        sub.accepted_at = self.clock()
        self.store.save_substitute(sub)
        entry = self._add_to_roster_entry(game, player_id, ctx.team_id,
                                          sub.position)
        self._audit(
            game_id,
            AuditAction.SUBSTITUTE_ADDED_TO_ROSTER,
            actor_id=actor_id,
            subject_player_id=player_id,
            detail={"override": True},
        )
        self._notify(
            game_id,
            NotificationType.SUBSTITUTE_ACCEPTED,
            audience="coach",
            message="Substitute accepted and was added to roster.",
            subject_player_id=player_id,
        )
        return entry

    def _add_to_roster_entry(
        self, game: Game, player_id: str, team_side: str, position: Position
    ) -> GameRosterEntry:
        """Seat a substitute, recording the DURABLE attribution it occupies.

        ``team_side``/``position`` are NOT resolved here — they are handed
        down by the caller, and they must be THE PAIR that caller just fed
        to :meth:`_require_open_slot` (``ctx.team_id`` from the validated
        :class:`GameMembershipContext`, and the ENROLLMENT's own
        season-scoped ``position``). Resolving them a second time inside
        this method would reintroduce exactly the two-reads-that-can-
        disagree defect #205 blocker 2 closed: the slot the gate checked and
        the slot the row is counted in have to be one slot, provably, not
        two answers that usually agree.
        """
        now = self.clock()
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
        sub = self.store.substitute_for_player(game_id, player_id)
        if sub is None or sub.status not in (
            SubstituteStatus.ENROLLED,
            SubstituteStatus.OFFERED,
        ):
            raise NotEnrolledError("Player is not currently enrolled as a substitute.")
        return sub

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

    def _resolve_context_with_reason(self, game, player):
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
        sides = tuple(t for t in (game.home_team_id, game.away_team_id) if t)
        if not sides:
            return None, NO_ELIGIBLE_MEMBERSHIP
        if not game.league_season_id:
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
        keeps) or a bound game whose LeagueSeason row itself dangles — fail
        closed, the same posture as the single form."""
        result: Dict[str, GameMembershipContext] = {}
        if game is None or not game.league_season_id:
            return result
        ls = self.store.get_league_season(game.league_season_id)
        if ls is None:
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
        posture the permanent gate had; #287 open question 4 — who may
        substitute across League/Division boundaries — is an unruled owner
        question this cutover does not answer).

        For a game with NO LeagueSeason binding the context is the permanent
        ``player.team_id`` pointer, so exhibitions and unbound legacy games
        keep pre-#205 behavior exactly."""
        ctx = self.resolve_membership_context(game, player)
        return ctx.team_id if ctx is not None else None

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
        if not game.league_season_id:
            return [(p, self.resolve_membership_context(game, p))
                    for p in self.store.players_for_team(team_id)]
        contexts = self.resolve_membership_contexts_for_game(game)
        return [(ctx.player, ctx) for _pid, ctx in sorted(contexts.items())
                if ctx.team_id == team_id]

    # ====================================================================
    # coach controls
    # ====================================================================
    @_transactional
    def remove_player(
        self, game_id: str, player_id: str, actor_id: Optional[str] = None
    ) -> GameRosterEntry:
        game = self._require_game(game_id)
        self._guard_mutable(game)
        entry = self.store.roster_entry_for_player(game_id, player_id)
        if entry is None:
            raise NotFoundError("Player is not on this game's roster.")
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
    # A game with no ``season_id`` at all (an unbound legacy row) takes no
    # Season lock — there is no Season to lock and no membership to change;
    # its eligibility is the permanent pointer, which the Player lock
    # covers.
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

    def _ordered_candidates(self, player_ids) -> List[str]:
        """De-duplicate and order candidate ids by ``(name, player_id)``.

        ORDERING IS IMPOSED BY THE SERVICE, NEVER INHERITED FROM THE STORE.
        Ids are ``f"{prefix}_{seq}"`` and ``SqlStore`` orders by a TEXT
        column, so ``players_for_team``/``roster_for_game``/
        ``memberships_for_team`` hand back ``player_1, player_10, player_11,
        …, player_2`` on SQL and insertion order in memory. Measured
        tri-store at head 4de9452, that was not a cosmetic difference:
        ``auto_build_roster`` TRUNCATES its pool at the game's targets, so
        the ORDER DECIDED SET MEMBERSHIP — from one identical 12-player
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

    def _partition_candidates(self, game, candidates, locked,
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
        bound = bool(game.league_season_id)
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
                    limits=None, preclassified=None) -> dict:
        """THE unit of work both batch entry points share: lock, revalidate,
        partition, seat, audit.

        MUST run inside the caller's ``transaction()``, AFTER the caller's
        ``_guard_mutable`` has taken the Season row lock — see the section
        header above for why those two locks are the relevant ones.
        Deliberately NOT ``@_transactional`` itself: decorating it would
        advertise a self-sufficiency it does not have (the Season lock and
        the mutability guard are the caller's, and taking them after
        discovery would be too late).

        ``limits`` (auto-fill only) is ``{SlotType: count}``; when given, at
        most that many SEATABLE candidates are seated per bucket, taken in
        the ordered pool's order, and the eligible remainder is reported as
        ``deferred`` with reason :data:`TARGET_MET`. ``confirm``
        additionally marks each seated player AVAILABLE inside this SAME
        transaction — previously that was N separate ``set_availability``
        transactions after a separate ``select_roster`` one, so a failure
        mid-loop left players seated but unconfirmed with nothing to roll
        back to.

        ZERO SEATS IS A SUCCESSFUL RESULT: ``select_roster`` is not called
        at all, so NO roster write of any kind happens, and the audit row is
        still written because it is the only durable record that the
        operation ran.

        Returns identity, never counts: ``{"team_id", "source",
        "from_game_id", "candidate_count", "seated", "skipped",
        "deferred"}``."""
        locked = self._lock_candidates(candidates)
        seatable, skipped, contexts = self._partition_candidates(
            game, candidates, locked, preclassified)
        deferred: List[Tuple[str, str]] = []
        if limits is not None:
            room = dict(limits)
            capped = []
            for pid in seatable:
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
        }

    def _batch_team(self, game, team_id) -> str:
        """The side a batch entry point acts on: the caller's, or the home
        side by default (#25). A team not playing in this game is refused —
        that is a bad REQUEST, not an ineligible candidate, so it keeps
        raising rather than becoming a skip."""
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
        self._guard_mutable(game)          # <- the SEASON ROW LOCK
        team_id = self._batch_team(game, team_id)
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
                          actor_id: Optional[str] = None) -> dict:
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

        EVERY INELIGIBLE CANDIDATE IS REPORTED, including ones the targets
        would never have reached: the cohort is the coach's own bench, the
        reasons are facts about it, and reporting only the first N would
        make the warning depend on how many slots happened to be open. The
        eligible remainder the targets had no room for is reported
        separately as ``deferred`` — nothing is wrong with those players.

        ``ValidationError`` still means "this team has nobody at all"; an
        empty COHORT (no pointers and no membership rows) is an empty state
        to fix in Setup, not a partial outcome."""
        game = self._require_game(game_id)
        self._guard_mutable(game)          # <- the SEASON ROW LOCK
        team_id = self._batch_team(game, team_id)
        candidates = self._auto_build_candidates(game, team_id)
        if not candidates:
            raise ValidationError(
                "Team has no players yet. Add or import players first."
            )
        return self._seat_batch(
            game, team_id, candidates, source="auto_build_roster",
            actor_id=actor_id, confirm=True,
            limits={SlotType.GOALIE: game.target_goalies,
                    SlotType.SKATER: game.target_skaters})

    @staticmethod
    def _is_visible_game(g) -> bool:
        """Published, non-draft, non-cancelled, scheduled game — the
        player-visible half of :meth:`_is_visible_team_game`, split out so
        the membership-resolved offer scan (#205 cutover) can pair it with
        :meth:`team_for_game` instead of the permanent-pointer team test."""
        return (not g.cancelled and g.published and not g.is_draft
                and g.start_time is not None)

    @staticmethod
    def _is_visible_team_game(g, team_id) -> bool:
        """Published, non-draft, non-cancelled game involving ``team_id`` —
        the shared "counts for the Player Home Page" predicate (#107), so
        next-game, today-count, and substitute-opportunity scans can never
        drift apart on what a player-visible game is."""
        return (RosterService._is_visible_game(g)
                and team_id in (g.home_team_id, g.away_team_id))

    def find_next_game_for_player(self, player_id: str) -> Optional[Game]:
        """The player's next published, non-cancelled game in chronological
        order — the Player Home Page's "next game" card (#107). A pure read
        helper — must NOT be @_transactional."""
        player = self.store.get_player(player_id)
        if player is None or not player.is_active:
            return None
        now = self.clock()
        upcoming = [
            g for g in self.store.all_games()
            if self._is_visible_team_game(g, player.team_id)
            and g.start_time >= now
        ]
        upcoming.sort(key=lambda g: g.start_time)
        return upcoming[0] if upcoming else None

    def count_games_today_for_player(self, player_id: str) -> int:
        """How many of the player's team's games fall on today's date — the
        Player Home Page's "Tonight" summary card (#107). A pure read
        helper — must NOT be @_transactional."""
        player = self.store.get_player(player_id)
        if player is None or not player.is_active:
            return 0
        today = self.clock().date()
        return sum(1 for g in self.store.all_games()
                   if self._is_visible_team_game(g, player.team_id)
                   and g.start_time.date() == today)

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

    def list_substitute_opportunities(self, player_id: str) -> List[Game]:
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
        opportunities = []
        for g in self.store.all_games():
            # Already enrolled/offered here → don't re-advertise (the detail
            # view still shows it, with a Withdraw action). Distinct from the
            # ineligibility reasons substitute_block_reason covers.
            existing_sub = self.store.substitute_for_player(g.id, player_id)
            if existing_sub is not None and existing_sub.status in (
                    SubstituteStatus.ENROLLED, SubstituteStatus.OFFERED):
                continue
            if self.substitute_block_reason(player_id, g.id) is None:
                opportunities.append(g)
        opportunities.sort(key=lambda g: g.start_time)
        return opportunities

    def substitute_offer_block_reason(self, player_id: str, game_id: str,
                                      enrollment, rstatus=None,
                                      ctx=None) -> Optional[str]:
        """Why an OFFERED player cannot ACCEPT the offer right now, or None if
        they can (#112). Builds on substitute_block_reason (which already covers
        cancelled / unpublished / past / locked / no-open-slot for the player's
        position — the same guards accept_substitute enforces) and adds the
        offer-specific expiry check, so the detail view's pre-disable logic
        can't drift from what accept_substitute actually permits."""
        base = self.substitute_block_reason(player_id, game_id, rstatus,
                                            ctx=ctx)
        if base is not None:
            return base
        if (enrollment.offer_expires_at is not None
                and self.clock() > enrollment.offer_expires_at):
            return "This offer has expired."
        return None

    def list_player_offers(self, player_id: str) -> List[Game]:
        """Games where this player currently has an OFFERED substitute slot —
        a coach has offered them the spot and they must accept/decline (#112).
        Distinct from list_substitute_opportunities (which is the self-enrol
        pool and excludes already-offered games). Applies the same visible +
        upcoming filter so a stale offer on a past/unpublished/cancelled game
        never clutters Player Home. A pure read helper — must NOT be
        @_transactional."""
        player = self.store.get_player(player_id)
        if player is None or not player.is_active:
            return []
        now = self.clock()
        offers = []
        for g in self.store.all_games():
            # #205 cutover: an offer surfaces when the player RESOLVES to a
            # team in the game (membership for LeagueSeason-bound games,
            # permanent pointer for unbound ones) — otherwise an offered
            # membership-only substitute could never see their own offer.
            if (not self._is_visible_game(g) or g.start_time < now
                    or self.team_for_game(g, player) is None):
                continue
            sub = self.store.substitute_for_player(g.id, player_id)
            if sub is not None and sub.status == SubstituteStatus.OFFERED:
                offers.append(g)
        offers.sort(key=lambda g: g.start_time)
        return offers

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
        — must NOT be @_transactional. Returns plain dicts."""
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
            # A deactivated player's enrollment stays as history but drops out
            # of the live outreach queue (#270 review) — never offer-able. The
            # same live-fail-closed rule applies to the #205 resolution: a
            # membership ended after enrollment drops the row from the queue.
            if (player is None or not player.is_active
                    or self.team_for_game(game, player) != team_id):
                continue
            can_offer = (sub.status == SubstituteStatus.ENROLLED
                         and not game.locked and not game.cancelled
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
        check). A pure read helper — must NOT be @_transactional."""
        game = self._require_game(game_id)
        team_id = team_id or game.home_team_id
        if rstatus is None:
            rstatus = self.compute_roster_status(game_id, team_id)
        already_sub = {
            s.player_id for s in self.store.substitutes_for_game(game_id)
            if s.status in (SubstituteStatus.ENROLLED, SubstituteStatus.OFFERED)
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
        self, game_id: str, player_id: str, actor_id: Optional[str] = None
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
        (matches copy_previous_roster's call to select_roster, above)."""
        reason = self.substitute_block_reason(player_id, game_id)
        if reason is not None:
            raise NotEligibleError(reason)
        return self.enroll_substitute(game_id, player_id, actor_id=actor_id)

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
        self._guard_active_season(game)  # #159 read-only guard
        game = self._refetch_under_season_lock(game_id)  # #201
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
        self._guard_active_season(game)  # #159 read-only guard
        game = self._refetch_under_season_lock(game_id)  # #201
        was_locked = game.locked
        game.locked = False
        self.store.save_game(game)
        self._audit(game_id, AuditAction.ROSTER_UNLOCKED, actor_id=actor_id)
        if was_locked:  # only on the transition (#87 idempotency)
            self._notify_game_change(
                game, NotificationKind.ROSTER_UNLOCKED, "Roster unlocked",
                f"The roster is unlocked for {self._game_label(game)}.")
        return game

    @_transactional
    def cancel_game(self, game_id: str, actor_id: Optional[str] = None) -> Game:
        game = self._require_game(game_id)
        self._guard_active_season(game)  # #159 read-only guard
        game = self._refetch_under_season_lock(game_id)  # #201
        was_cancelled = game.cancelled
        game.cancelled = True
        self.store.save_game(game)
        # Cancel any active substitute enrollments.
        for sub in self.store.substitutes_for_game(game_id):
            if sub.status in (SubstituteStatus.ENROLLED, SubstituteStatus.OFFERED):
                sub.status = SubstituteStatus.CANCELLED
                self.store.save_substitute(sub)
        self._audit(game_id, AuditAction.GAME_CANCELLED, actor_id=actor_id)
        if not was_cancelled:  # only on the transition (#87 idempotency)
            self._notify_game_change(
                game, NotificationKind.GAME_CANCELLED, "Game cancelled",
                f"{self._game_label(game)} has been cancelled.",
                include_public=True)
        return game

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
        bound = bool(game.league_season_id)
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
            ctx = context_of(sub.player_id, player)
            if ctx is not None and ctx.team_id == team_id:
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
    def _open_slot_phrase(goalie: SlotSummary, skater: SlotSummary) -> str:
        parts = []
        if goalie.open_count > 0:
            unit = "goalie slot" if goalie.open_count == 1 else "goalie slots"
            parts.append(f"{goalie.open_count} {unit} open.")
        if skater.open_count > 0:
            unit = "skater slot" if skater.open_count == 1 else "skater slots"
            parts.append(f"{skater.open_count} {unit} open.")
        return " ".join(parts)
