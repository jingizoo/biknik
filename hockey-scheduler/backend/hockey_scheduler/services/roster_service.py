"""Roster status engine and substitute workflow.

This is the heart of the slice. It is pure business logic over the store: it
never performs network/disk I/O and it never calls ``datetime.now()`` itself
(a clock is injected) so every rule is deterministic and unit-testable.

Every state-changing method appends an :class:`AuditLog` entry and, where the
use case calls for it, emits a :class:`NotificationEvent`.
"""

import functools
from datetime import datetime, timezone
from typing import Callable, List, Optional

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
    NotificationEvent,
    Player,
    RosterEntryStatus,
    RosterRole,
    RosterStatus,
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
from .notifier import push as _push_notification
from .season_guard import require_active_season


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _transactional(fn):
    """Wrap a mutating service method in a single store transaction."""
    @functools.wraps(fn)
    def wrapper(self, *args, **kwargs):
        with self.store.transaction():
            return fn(self, *args, **kwargs)
    return wrapper


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

        entries: List[GameRosterEntry] = []
        for player_id in player_ids:
            player = locked_players[player_id]
            if player is None:
                raise NotFoundError(f"Player {player_id} not found.")
            if player.team_id not in (game.home_team_id, game.away_team_id):
                raise NotEligibleError(
                    f"{player.name} is not on either team in this game."
                )
            if not player.is_active:
                raise NotEligibleError(f"{player.name} is not an active player.")

            now = self.clock()
            existing = self.store.roster_entry_for_player(game_id, player_id)
            if existing is not None:
                if existing.status.occupies_slot:
                    # idempotent: already selected
                    entries.append(existing)
                    continue
                # Revive a removed/unavailable row instead of inserting a
                # duplicate — one row per (game_id, player_id), now also enforced
                # by a unique index (#201 Slice 3B, migration 023).
                existing.roster_role = RosterRole.SELECTED
                existing.selection_source = SelectionSource.COACH_SELECTED
                existing.status = RosterEntryStatus.SELECTED
                existing.selected_by = actor_id
                existing.updated_at = now
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
                # Re-confirming after a back-out only works while the slot is
                # still open (a substitute may have already filled it).
                self._require_open_slot(game_id, player.slot_type, player.team_id)

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
        if entry is not None:
            if availability_status == AvailabilityStatus.AVAILABLE and (
                entry.status.occupies_slot or reconfirming
            ):
                self._set_entry_status(entry, RosterEntryStatus.CONFIRMED)
            elif (availability_status == AvailabilityStatus.UNAVAILABLE
                  and entry.status.occupies_slot):
                self._back_out_entry(game, entry, actor_id)
        return av

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
        player = self.store.get_player(entry.player_id)
        team_id = player.team_id if player else None
        status = self.compute_roster_status(game.id, team_id)
        if status.status == GameStatus.OPEN_SLOT:
            self._notify(
                game.id,
                NotificationType.SLOT_OPEN,
                audience="coach",
                message=status.message,
            )
            # Feed notification to that team's coach (#32).
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

        if player.team_id not in (game.home_team_id, game.away_team_id):
            raise NotEligibleError(
                f"{player.name} is not eligible (cross-team borrowing is off)."
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
            position=player.position,
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
        self._require_active_player(player_id)   # fail closed on deactivation
        sub = self.store.substitute_for_player(game_id, player_id)
        if sub is None or sub.status != SubstituteStatus.ENROLLED:
            raise InvalidTransitionError(
                "Only an enrolled substitute can be offered a slot."
            )
        self._require_open_slot(game_id, sub.slot_type, self._player_team(sub.player_id))
        sub.status = SubstituteStatus.OFFERED
        sub.offered_at = self.clock()
        sub.offer_expires_at = offer_expires_at
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
        # First-accepted-wins: the slot must still be open.
        self._require_open_slot(game.id, sub.slot_type, self._player_team(sub.player_id))
        game_id = game.id
        sub.status = SubstituteStatus.ACCEPTED
        sub.accepted_at = self.clock()
        self.store.save_substitute(sub)
        entry = self._add_to_roster_entry(game, player_id)
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
            audience_ref=self._player_team(player_id), game_id=game_id)
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
        _push_notification(
            self.store, self.clock,
            NotificationKind.SUBSTITUTE_DECLINED, NotificationAudience.COACH,
            "Substitute declined",
            "A substitute declined the offer — you can offer the next candidate.",
            audience_ref=self._player_team(player_id), game_id=game_id)
        return sub

    @_transactional
    def add_substitute_to_roster(
        self, game_id: str, player_id: str, actor_id: Optional[str] = None
    ) -> GameRosterEntry:
        """Coach override: offer + accept in one step (audited)."""
        game = self._require_game(game_id)
        self._guard_mutable(game)
        self._require_active_player(player_id)   # fail closed on deactivation
        sub = self.store.substitute_for_player(game_id, player_id)
        if sub is None or sub.status not in (
            SubstituteStatus.ENROLLED,
            SubstituteStatus.OFFERED,
        ):
            raise NotEnrolledError(
                "Player must be an enrolled/offered substitute to be added."
            )
        self._require_open_slot(game_id, sub.slot_type, self._player_team(sub.player_id))
        sub.status = SubstituteStatus.ACCEPTED
        sub.accepted_at = self.clock()
        self.store.save_substitute(sub)
        entry = self._add_to_roster_entry(game, player_id)
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

    def _add_to_roster_entry(self, game: Game, player_id: str) -> GameRosterEntry:
        now = self.clock()
        existing = self.store.roster_entry_for_player(game.id, player_id)
        if existing:
            existing.roster_role = RosterRole.SUBSTITUTE_ADDED
            existing.selection_source = SelectionSource.SUBSTITUTE_POOL
            existing.status = RosterEntryStatus.ACCEPTED
            existing.updated_at = now
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

    def _player_team(self, player_id: str) -> Optional[str]:
        p = self.store.get_player(player_id)
        return p.team_id if p else None

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

    def copy_previous_roster(
        self, game_id: str, team_id: Optional[str] = None,
        actor_id: Optional[str] = None,
    ) -> dict:
        """Seed one side's roster from that team's most recent earlier game.

        A time-saver for coaches: find the newest non-cancelled game the team
        played (as home *or* away) that had players occupying slots, then
        re-select those players (skipping any no longer active on the team). The
        selection goes through :meth:`select_roster`, so all eligibility, lock,
        and audit rules still apply. ``team_id`` defaults to the home side.
        """
        game = self._require_game(game_id)
        self._guard_mutable(game)
        team_id = team_id or game.home_team_id
        if team_id not in (game.home_team_id, game.away_team_id):
            raise ValidationError("That team is not playing in this game.")
        earlier = [
            g for g in self.store.all_games()
            if g.id != game_id and team_id in (g.home_team_id, g.away_team_id)
            and not g.cancelled and g.start_time is not None
            and (game.start_time is None or g.start_time < game.start_time)
        ]
        earlier.sort(key=lambda g: g.start_time, reverse=True)
        for src in earlier:
            eligible = [
                e.player_id for e in self.store.roster_for_game(src.id)
                if e.status.occupies_slot
                and (p := self.store.get_player(e.player_id)) is not None
                and p.is_active and p.team_id == team_id
            ]
            if eligible:
                self.select_roster(game_id, eligible, actor_id)
                return {"copied": len(eligible), "from_game_id": src.id,
                        "team_id": team_id}
        raise ValidationError("No previous roster to copy for this team.")

    @staticmethod
    def _is_visible_team_game(g, team_id) -> bool:
        """Published, non-draft, non-cancelled game involving ``team_id`` —
        the shared "counts for the Player Home Page" predicate (#107), so
        next-game, today-count, and substitute-opportunity scans can never
        drift apart on what a player-visible game is."""
        return (not g.cancelled and g.published and not g.is_draft
                and g.start_time is not None
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
                                rstatus=None) -> Optional[str]:
        """Why ``player_id`` cannot enrol as a substitute for ``game_id`` right
        now, or ``None`` if they can. The single source of truth shared by the
        Home opportunity list (#107, skip silently when blocked) and the #110
        opportunity-detail view (show the reason). Does NOT consider an existing
        enrolment — being already enrolled is a separate state (it offers a
        Withdraw action), not a reason the player is ineligible.

        ``rstatus`` may be a pre-computed :class:`RosterStatus` for this
        game/team so a caller that already has one (the detail view) avoids a
        second ``compute_roster_status`` pass. A pure read helper — must NOT be
        @_transactional.
        """
        player = self.store.get_player(player_id)
        if player is None:
            return "Player not found."
        if not player.is_active:
            return "You are not an active player."
        game = self.store.get_game(game_id)
        if game is None:
            return "Game not found."
        if player.team_id not in (game.home_team_id, game.away_team_id):
            return "You are not on a team in this game."
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
        needed = player.position.slot_type
        if rstatus is None:
            rstatus = self.compute_roster_status(game_id, player.team_id)
        open_slots = (rstatus.open_goalie_slots if needed == SlotType.GOALIE
                      else rstatus.open_skater_slots)
        if open_slots <= 0:
            return "There is no open slot for your position right now."
        return None

    def list_substitute_opportunities(self, player_id: str) -> List[Game]:
        """Games where this player's own team has an open slot matching
        their position and the player isn't already selected/enrolled — the
        Player Home Page's substitute-opportunities section (#107).

        Cross-team borrowing is off (the same constraint enroll_substitute
        enforces at line 363), so this only ever surfaces the player's own
        team's games. A pure read helper — must NOT be @_transactional.
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
                                      enrollment, rstatus=None) -> Optional[str]:
        """Why an OFFERED player cannot ACCEPT the offer right now, or None if
        they can (#112). Builds on substitute_block_reason (which already covers
        cancelled / unpublished / past / locked / no-open-slot for the player's
        position — the same guards accept_substitute enforces) and adds the
        offer-specific expiry check, so the detail view's pre-disable logic
        can't drift from what accept_substitute actually permits."""
        base = self.substitute_block_reason(player_id, game_id, rstatus)
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
            if not self._is_visible_team_game(g, player.team_id) or g.start_time < now:
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
            # of the live outreach queue (#270 review) — never offer-able.
            if player is None or player.team_id != team_id or not player.is_active:
                continue
            can_offer = (sub.status == SubstituteStatus.ENROLLED
                         and not game.locked and not game.cancelled
                         and open_for.get(sub.slot_type, 0) > 0)
            rows.append({
                "player_id": sub.player_id, "name": player.name,
                "position": player.position.value,
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
        """Active same-team players a coach could add as a substitute
        candidate right now (#114) — substitute_block_reason (the SAME gate
        the player-side opportunity list uses) returns None for them, and
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
        for player in self.store.players_for_team(team_id):
            if not player.is_active or player.id in already_sub:
                continue
            if self.substitute_block_reason(
                    player.id, game_id, rstatus=rstatus) is not None:
                continue
            rows.append({
                "player_id": player.id, "name": player.name,
                "position": player.position.value,
                "slot_type": player.position.slot_type.value,
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
    def _slot_summaries(self, game_id: str, team_id: str):
        game = self._require_game(game_id)
        entries = self.store.roster_for_game(game_id)
        subs = self.store.substitutes_for_game(game_id)

        targets = {
            SlotType.GOALIE: game.target_goalies,
            SlotType.SKATER: game.target_skaters,
        }
        occupied = {SlotType.GOALIE: 0, SlotType.SKATER: 0}
        confirmed = {SlotType.GOALIE: 0, SlotType.SKATER: 0}
        for entry in entries:
            player = self.store.get_player(entry.player_id)
            if player is None or player.team_id != team_id:
                continue  # each side is counted independently (home vs away)
            st = player.slot_type
            if entry.status.occupies_slot:
                occupied[st] += 1
            if entry.status.is_confirmed_body:
                confirmed[st] += 1

        subs_available = {SlotType.GOALIE: 0, SlotType.SKATER: 0}
        for sub in subs:
            player = self.store.get_player(sub.player_id)
            if player is None or player.team_id != team_id:
                continue
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
        return result

    def compute_roster_status(
        self, game_id: str, team_id: Optional[str] = None
    ) -> RosterStatus:
        game = self._require_game(game_id)
        # Home and away lineups are computed independently (#25). Default to the
        # home side so existing callers/behaviour are unchanged.
        team_id = team_id or game.home_team_id
        entries = [
            e for e in self.store.roster_for_game(game_id)
            if (p := self.store.get_player(e.player_id)) is not None
            and p.team_id == team_id
        ]
        summaries = self._slot_summaries(game_id, team_id)
        goalie = summaries[SlotType.GOALIE]
        skater = summaries[SlotType.SKATER]

        subs_enrolled = sum(
            1
            for s in self.store.substitutes_for_game(game_id)
            if s.status == SubstituteStatus.ENROLLED
            and (p := self.store.get_player(s.player_id)) is not None
            and p.team_id == team_id
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
