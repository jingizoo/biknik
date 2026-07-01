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

    def _require_player(self, player_id: str) -> Player:
        player = self.store.get_player(player_id)
        if player is None:
            raise NotFoundError(f"Player {player_id} not found.")
        return player

    def _guard_mutable(self, game: Game) -> None:
        """Guard for operations that change the committed roster."""
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

        entries: List[GameRosterEntry] = []
        for player_id in player_ids:
            player = self._require_player(player_id)
            if player.team_id != game.home_team_id:
                raise NotEligibleError(
                    f"{player.name} is not on this team's roster."
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
                # duplicate (there is no unique (game_id, player_id) constraint).
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
                self._require_open_slot(game_id, player.slot_type)

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
        status = self.compute_roster_status(game.id)
        if status.status == GameStatus.OPEN_SLOT:
            self._notify(
                game.id,
                NotificationType.SLOT_OPEN,
                audience="coach",
                message=status.message,
            )

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

        if player.team_id != game.home_team_id:
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
        sub = self.store.substitute_for_player(game_id, player_id)
        if sub is None or sub.status != SubstituteStatus.ENROLLED:
            raise InvalidTransitionError(
                "Only an enrolled substitute can be offered a slot."
            )
        self._require_open_slot(game_id, sub.slot_type)
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
        return sub

    @_transactional
    def accept_substitute(
        self, game_id: str, player_id: str, actor_id: Optional[str] = None
    ) -> GameRosterEntry:
        game = self._require_game(game_id)
        self._guard_mutable(game)
        sub = self.store.substitute_for_player(game_id, player_id)
        if sub is None or sub.status != SubstituteStatus.OFFERED:
            raise InvalidTransitionError("No active offer to accept.")
        # Offers can expire: a lapsed offer returns the player to the pool.
        if sub.offer_expires_at and self.clock() > sub.offer_expires_at:
            sub.status = SubstituteStatus.EXPIRED
            self.store.save_substitute(sub)
            raise InvalidTransitionError("This substitute offer has expired.")
        # First-accepted-wins: the slot must still be open.
        self._require_open_slot(game_id, sub.slot_type)
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
        return sub

    @_transactional
    def add_substitute_to_roster(
        self, game_id: str, player_id: str, actor_id: Optional[str] = None
    ) -> GameRosterEntry:
        """Coach override: offer + accept in one step (audited)."""
        game = self._require_game(game_id)
        self._guard_mutable(game)
        sub = self.store.substitute_for_player(game_id, player_id)
        if sub is None or sub.status not in (
            SubstituteStatus.ENROLLED,
            SubstituteStatus.OFFERED,
        ):
            raise NotEnrolledError(
                "Player must be an enrolled/offered substitute to be added."
            )
        self._require_open_slot(game_id, sub.slot_type)
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

    def _require_open_slot(self, game_id: str, slot_type: SlotType) -> None:
        summaries = self._slot_summaries(game_id)
        summary = summaries[slot_type]
        if summary.open_count <= 0:
            raise SlotAlreadyFilledError(
                f"The {slot_type.value} slot is already filled."
            )

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
        self, game_id: str, actor_id: Optional[str] = None
    ) -> dict:
        """Seed this game's roster from the team's most recent earlier game.

        A time-saver for coaches: find the newest non-cancelled game where the
        same team was home and had players occupying slots, then re-select those
        players (skipping any who are no longer active on the team). The actual
        selection goes through :meth:`select_roster`, so all eligibility, lock,
        and audit rules still apply.
        """
        game = self._require_game(game_id)
        self._guard_mutable(game)
        earlier = [
            g for g in self.store.all_games()
            if g.id != game_id and g.home_team_id == game.home_team_id
            and not g.cancelled and g.start_time is not None
            and (game.start_time is None or g.start_time < game.start_time)
        ]
        earlier.sort(key=lambda g: g.start_time, reverse=True)
        for src in earlier:
            eligible = [
                e.player_id for e in self.store.roster_for_game(src.id)
                if e.status.occupies_slot
                and (p := self.store.get_player(e.player_id)) is not None
                and p.is_active and p.team_id == game.home_team_id
            ]
            if eligible:
                self.select_roster(game_id, eligible, actor_id)
                return {"copied": len(eligible), "from_game_id": src.id}
        raise ValidationError("No previous roster to copy for this team.")

    @_transactional
    def lock_roster(self, game_id: str, actor_id: Optional[str] = None) -> Game:
        game = self._require_game(game_id)
        if game.cancelled:
            raise GameCancelledError("Game is cancelled.")
        game.locked = True
        self.store.save_game(game)
        self._audit(game_id, AuditAction.ROSTER_LOCKED, actor_id=actor_id)
        self._notify(
            game_id,
            NotificationType.ROSTER_LOCKED,
            audience="team",
            message="Roster is locked for this game.",
        )
        return game

    @_transactional
    def unlock_roster(self, game_id: str, actor_id: Optional[str] = None) -> Game:
        game = self._require_game(game_id)
        game.locked = False
        self.store.save_game(game)
        self._audit(game_id, AuditAction.ROSTER_UNLOCKED, actor_id=actor_id)
        return game

    @_transactional
    def cancel_game(self, game_id: str, actor_id: Optional[str] = None) -> Game:
        game = self._require_game(game_id)
        game.cancelled = True
        self.store.save_game(game)
        # Cancel any active substitute enrollments.
        for sub in self.store.substitutes_for_game(game_id):
            if sub.status in (SubstituteStatus.ENROLLED, SubstituteStatus.OFFERED):
                sub.status = SubstituteStatus.CANCELLED
                self.store.save_substitute(sub)
        self._audit(game_id, AuditAction.GAME_CANCELLED, actor_id=actor_id)
        return game

    # ====================================================================
    # roster status engine
    # ====================================================================
    def _slot_summaries(self, game_id: str):
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
            if player is None:
                continue
            st = player.slot_type
            if entry.status.occupies_slot:
                occupied[st] += 1
            if entry.status.is_confirmed_body:
                confirmed[st] += 1

        subs_available = {SlotType.GOALIE: 0, SlotType.SKATER: 0}
        for sub in subs:
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

    def compute_roster_status(self, game_id: str) -> RosterStatus:
        game = self._require_game(game_id)
        entries = self.store.roster_for_game(game_id)
        summaries = self._slot_summaries(game_id)
        goalie = summaries[SlotType.GOALIE]
        skater = summaries[SlotType.SKATER]

        subs_enrolled = sum(
            1
            for s in self.store.substitutes_for_game(game_id)
            if s.status == SubstituteStatus.ENROLLED
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
            team_id=game.home_team_id,
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
