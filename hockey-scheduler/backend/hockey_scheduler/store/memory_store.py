"""In-memory persistence for the first slice.

This is deliberately small and synchronous. The service layer depends only on
the public methods here, so a SQL-backed implementation can be substituted
later without touching domain logic.
"""

import copy
from contextlib import contextmanager
from datetime import datetime
import threading
from typing import Dict, List, Optional

from ..domain import (
    ActiveContext,
    AuditLog,
    CalendarFeedToken,
    Club,
    OfficialAvailability,
    Division,
    Game,
    GameAvailability,
    GameRosterEntry,
    IceSlot,
    SchedulingPolicy,
    GameResult,
    ContactDestination,
    DataAccessLog,
    DeliveryStatus,
    DeviceToken,
    FactoryResetChallenge,
    FactoryResetEvent,
    FactoryResetLock,
    League,
    LeagueSeason,
    Program,
    Notification,
    NotificationDelivery,
    GuardianLink,
    InstallationState,
    NotificationEvent,
    NotificationPreference,
    NotificationRecipient,
    Official,
    OfficialAssignment,
    Player,
    RescheduleRequest,
    Rink,
    ScheduleScenario,
    Season,
    SeasonCopyForwardCommit,
    SeasonTeamRegistration,
    SeasonVenueAccess,
    SensitiveFieldCategory,
    TeamLeagueMigrationDecision,
    Session,
    SetupAuditLog,
    SubstituteEnrollment,
    Team,
    UserAccount,
    Organization,
    VALID_OUTCOMES,
    Venue,
)
from ..domain.errors import IntegrityConflictError, ValidationError


class InMemoryStore:
    backend = "memory"  # store kind, for the runtime status endpoint (#72)

    def __init__(self) -> None:
        self.teams: Dict[str, Team] = {}
        self.players: Dict[str, Player] = {}
        self.games: Dict[str, Game] = {}
        self.roster_entries: Dict[str, GameRosterEntry] = {}
        self.availability: Dict[str, GameAvailability] = {}
        self.substitutes: Dict[str, SubstituteEnrollment] = {}
        self.audit: List[AuditLog] = []
        self.notifications: List[NotificationEvent] = []
        # Organization & arena setup collections.
        self.organizations: Dict[str, Organization] = {}
        self.programs: Dict[str, Program] = {}
        self.seasons: Dict[str, Season] = {}
        self.leagues: Dict[str, League] = {}
        self.league_seasons: Dict[str, LeagueSeason] = {}
        self.divisions: Dict[str, Division] = {}
        self.season_team_registrations: Dict[str, SeasonTeamRegistration] = {}
        self.season_copy_forward_commits: Dict[
            str, SeasonCopyForwardCommit] = {}
        self.team_league_migration_decisions: Dict[
            str, TeamLeagueMigrationDecision] = {}
        self.season_venue_access: Dict[str, SeasonVenueAccess] = {}
        self.clubs: Dict[str, Club] = {}
        self.venues: Dict[str, Venue] = {}
        self.rinks: Dict[str, Rink] = {}
        self.ice_slots: Dict[str, IceSlot] = {}
        self.scheduling_policies: Dict[str, SchedulingPolicy] = {}
        self.schedule_scenarios: Dict[str, ScheduleScenario] = {}
        self.officials: Dict[str, Official] = {}
        self.official_assignments: Dict[str, OfficialAssignment] = {}
        self.game_results: Dict[str, GameResult] = {}
        self.feed_notifications: Dict[str, Notification] = {}
        self.notif_recipients: Dict[str, NotificationRecipient] = {}
        self.notif_deliveries: Dict[str, NotificationDelivery] = {}
        self.contact_destinations: Dict[str, ContactDestination] = {}
        self.device_tokens: Dict[str, DeviceToken] = {}
        self.notification_preferences: Dict[str, NotificationPreference] = {}
        self.calendar_feed_tokens: Dict[str, CalendarFeedToken] = {}
        self.official_availability: Dict[str, OfficialAvailability] = {}
        self.installation_state: Dict[str, InstallationState] = {}
        self.user_accounts: Dict[str, UserAccount] = {}
        self.sessions: Dict[str, Session] = {}
        self.user_active_context: Dict[str, ActiveContext] = {}
        self.guardian_links: Dict[str, GuardianLink] = {}
        self.reschedule_requests: Dict[str, RescheduleRequest] = {}
        self.setup_audit: List[SetupAuditLog] = []
        # Durable sensitive-read audit (#124). A plain list attribute so the
        # generic transaction snapshot, clear_all_data() and row_counts()
        # machinery all cover it exactly like the other audit surfaces.
        self.data_access: List[DataAccessLog] = []
        # Never cleared by clear_all_data() (#256) — the durable record of a
        # production factory-reset attempt must survive the wipe it describes.
        self.factory_reset_events: List[FactoryResetEvent] = []
        # Durable, cross-process-equivalent challenge/lock (#256 review
        # blocker 5) — see acquire_factory_reset_lock()/
        # set_factory_reset_challenge() below. Also never cleared by
        # clear_all_data(): the challenge is always consumed before a wipe
        # begins, and the lock is held for the wipe's whole duration.
        self._factory_reset_challenge: Optional[FactoryResetChallenge] = None
        self._factory_reset_lock: Optional[FactoryResetLock] = None
        # Plain int counters (not itertools.count): a count object is not
        # copyable on Python 3.14 (copy.copy raises "cannot pickle
        # 'itertools.count'"), which would break the transaction snapshot; an
        # int copies trivially on every version and rolls back the same way.
        self._counters: Dict[str, int] = {}
        self._lock = threading.RLock()
        # Depth of the current (possibly nested) transaction; 0 = none open.
        self._txn_depth = 0
        # The epoch fence's persisted version counters (PR #423 round-N
        # review finding 1) — bumped by epoch_fence_acquire_exclusive, on the
        # SAME transaction as everything else that call's writer does, so it
        # rolls back with the rest via _snapshot()/_restore() (a plain dict
        # attribute, not in _NON_SNAPSHOT — dicts are already handled
        # generically by _snapshot_value's shallow-copy branch). Keyed PER
        # FENCE KEY (mirroring the advisory lock's own user:<id>/global
        # split — migration 052's own docstring explains why a single shared
        # counter was tried and reverted: it broke cross-user independence).
        # A key with no entry has never been bumped and reads as version 0 —
        # see SqlStore.current_epoch_fence_version's own docstring for the
        # read-side contract this mirrors.
        self._epoch_fence_versions: Dict[str, int] = {}
        # DataAccessLog rows queued by add_data_access_durable() while nested
        # inside an ambient transaction, to be flushed once that transaction
        # has FULLY concluded regardless of outcome (#426 review finding 4)
        # — see transaction()'s finally and add_data_access_durable()'s own
        # docstring. Bookkeeping, not application data: excluded from
        # _NON_SNAPSHOT below like _lock/_txn_depth, never itself rolled
        # back.
        self._pending_durable_data_access: List[DataAccessLog] = []

    # Instance attributes that are NOT part of the persisted state and so must
    # never be snapshotted/restored by a transaction (the lock is unpicklable
    # and the depth counter drives the rollback machinery itself).
    _NON_SNAPSHOT = frozenset(
        {"_lock", "_txn_depth", "_pending_durable_data_access"})

    # Snapshot invariant: a stored dataclass's nested mutable fields (the only
    # ones today are AuditLog.detail, SetupAuditLog.detail and
    # UserAccount.scope) are always replaced WHOLESALE, never mutated in place.
    # The snapshot shallow-copies each element (see _snapshot_value), so a field
    # REASSIGNMENT rolls back but an in-place edit of a shared nested dict would
    # not. If a future change edits such a dict in place, switch that field to a
    # deep copy here (and extend the rollback tests).

    @staticmethod
    def _snapshot_value(value):
        # Copy a state attribute for the pre-image. Collections are rebuilt with
        # a shallow copy of every element: services mutate a stored dataclass by
        # reassigning its fields (`game.published = True`), and copy.copy gives
        # the pre-image its own instance so that reassignment can't reach it.
        # A shallow element copy (not deepcopy) keeps this cheap — see the
        # snapshot invariant above for the nested-dict constraint this relies on.
        # Every value here (dataclasses, primitives, and the plain-int id
        # counters) is copy.copy-safe on all supported Python versions.
        if isinstance(value, dict):
            return {k: copy.copy(v) for k, v in value.items()}
        if isinstance(value, list):
            return [copy.copy(v) for v in value]
        return copy.copy(value)

    def _snapshot(self) -> Dict[str, object]:
        return {k: self._snapshot_value(v) for k, v in self.__dict__.items()
                if k not in self._NON_SNAPSHOT}

    def _restore(self, snapshot: Dict[str, object]) -> None:
        # Restore in place so the identity of each collection is preserved — any
        # caller holding a reference to, say, ``store.games`` still sees the
        # rolled-back contents rather than a detached old container.
        for key, value in snapshot.items():
            current = getattr(self, key, None)
            if isinstance(current, dict) and isinstance(value, dict):
                current.clear()
                current.update(value)
            elif isinstance(current, list) and isinstance(value, list):
                current[:] = value
            else:
                setattr(self, key, value)

    # Attributes clear_all_data()/row_counts() never touch (#256): the lock
    # and depth counter aren't data, ``_counters`` preserves id uniqueness
    # across a reset, ``factory_reset_events`` is the durable record of the
    # reset attempt itself, and the challenge/lock are the reset's own
    # in-flight orchestration state (not application data). The latter two
    # are single objects, not dict/list, so row_counts()/clear_all_data()'s
    # isinstance check already skips them — listed here anyway for clarity.
    _UNCLEARABLE = frozenset({"_lock", "_txn_depth", "_counters",
                              "factory_reset_events",
                              "_factory_reset_challenge", "_factory_reset_lock"})

    def row_counts(self) -> dict:
        """Row count per collection that ``clear_all_data()`` would empty
        (#256 preview) — same attribute set, so a preview count can never
        overstate or understate what an execute() would actually wipe."""
        return {key: len(value) for key, value in self.__dict__.items()
               if key not in self._UNCLEARABLE and isinstance(value, (dict, list))}

    def clear_all_data(self) -> None:
        """Delete every row from every collection (#256 production factory
        reset). There is no schema to preserve for this store, but the id
        counters are left untouched for the same reason as
        ``SqlStore.clear_all_data()``: reusing an id after a reset could
        collide with anything a durable ``factory_reset_events`` row, or an
        external system, still references. ``factory_reset_events`` itself
        is the one collection deliberately excluded — it is the durable
        record of the reset attempt and must survive the wipe it describes.
        Generic over every dict/list attribute rather than a hardcoded list,
        so a future new entity collection is wiped automatically instead of
        silently surviving a reset. Call within ``transaction()`` for
        atomicity, exactly like ``SqlStore.clear_all_data()``.
        """
        for key, value in self.__dict__.items():
            if key in self._UNCLEARABLE:
                continue
            if isinstance(value, (dict, list)):
                value.clear()

    @contextmanager
    def transaction(self, isolation=None, read_only=False):
        """Atomic, reentrant unit of work — the shared store transaction contract.

        ``isolation`` (e.g. ``"SERIALIZABLE"``) is accepted for parity with
        :class:`SqlStore` but is a **no-op** here: the process-wide re-entrant
        lock already fully serializes every transaction, so all reads inside one
        transaction observe a single consistent snapshot with no concurrent
        interleaving — the strongest isolation, for free.

        ``read_only`` (round-N+2 regression fix, PR #423) is likewise accepted
        for parity with :class:`SqlStore` and is likewise a **no-op** here: the
        re-entrant lock this method takes below (``self._lock``, a single
        per-instance ``threading.RLock`` — see its many "transaction() holds
        self._lock for its whole body" call-site comments throughout this
        file) is already the ONLY thing serializing access to this store, at
        full strength, regardless of whether the caller intends to write —
        there is no second, weaker lock tier for a promise of read-only-ness
        to relax. The SQLite store's own ``read_only`` fixes a real contention
        bug specific to ITS two-tier SHARED/RESERVED FILE locking, which this
        store has no analogue of at all (there is no second connection, and no
        file); so nothing here needed to change — the parameter exists purely
        so a caller (e.g. ``ContextService._snapshot``) can pass the identical
        keyword to whichever backend it happens to be holding.

        Contract (identical observable behavior in every store implementation):

        * All writes between entering and exiting the outermost ``transaction()``
          commit together, or — if the body raises — none of them persist. A
          failure after the first write leaves zero partial state.
        * Transactions are reentrant: a nested ``transaction()`` joins the
          enclosing one (it does not open a second unit), so a service method
          that itself opens a transaction can be called from within another.
          Only the outermost context commits or rolls back.
        * The block is serialized against other writers (a re-entrant lock here;
          a real DB transaction in :class:`SqlStore`).

        The SQL store gets rollback from the database. This store gets it by
        snapshotting all state on entry to the outermost transaction and
        restoring that snapshot if the body raises — so the in-memory store is a
        faithful atomicity reference for tests, not merely a lock (#201).
        """
        with self._lock:
            outermost = self._txn_depth == 0
            snapshot = self._snapshot() if outermost else None
            self._txn_depth += 1
            try:
                yield
            except BaseException:
                if outermost:
                    self._restore(snapshot)
                raise
            finally:
                self._txn_depth -= 1
                # #426 review finding 4: flush any DataAccessLog rows queued
                # by add_data_access_durable() while nested inside the
                # transaction that JUST concluded. Runs AFTER _restore()
                # above (so a flushed row is never itself wiped by the
                # rollback it is surviving) and regardless of commit vs
                # rollback — a durable row is never contingent on the
                # ambient unit's own outcome. Guarded by `outermost` (via
                # the depth check) so a nested transaction() never triggers
                # a premature flush of its enclosing caller's still-open
                # unit.
                if self._txn_depth == 0 and self._pending_durable_data_access:
                    self._flush_pending_durable_data_access()

    def close(self) -> None:
        pass

    # -- operational health (#90) ------------------------------------------
    def db_reachable(self) -> bool:
        return True  # the in-memory store is always reachable

    def migration_status(self) -> dict:
        # No SQL migrations for the in-memory store; trivially current.
        return {"backend": self.backend, "applied": [], "expected": [],
                "current": True}

    # -- id generation -----------------------------------------------------
    def next_id(self, prefix: str) -> str:
        # itertools.count's next() was a single atomic step; a plain
        # read-modify-write is not, and next_id() may be called outside a
        # transaction on the threaded server, so serialize it to avoid two
        # requests handing out the same id. The lock is the same re-entrant one
        # transaction() holds, so calling this from within a transaction is safe.
        with self._lock:
            value = self._counters.get(prefix, 0) + 1
            self._counters[prefix] = value
            return f"{prefix}_{value}"

    # -- teams / players ---------------------------------------------------
    def add_team(self, team: Team) -> Team:
        self.teams[team.id] = team
        return team

    def add_player(self, player: Player) -> Player:
        self.players[player.id] = player
        return player

    def get_player(self, player_id: str) -> Optional[Player]:
        return self.players.get(player_id)

    def get_player_for_update(self, player_id: str) -> Optional[Player]:
        # See get_team_for_update: transaction() holds self._lock for its whole
        # body, so a plain read already serializes with a concurrent delete.
        return self.players.get(player_id)

    def players_for_team(self, team_id: str) -> List[Player]:
        return [p for p in self.players.values() if p.team_id == team_id]

    def all_players(self) -> List[Player]:
        return list(self.players.values())

    # -- games -------------------------------------------------------------
    def add_game(self, game: Game) -> Game:
        self.games[game.id] = game
        return game

    def get_game_for_update(self, game_id: str) -> Optional[Game]:
        # #369 row-lock parity with SqlStore's SELECT ... FOR UPDATE:
        # transaction() holds self._lock for its whole body here, so a
        # plain read already serializes against every concurrent writer.
        return self.games.get(game_id)

    def get_game(self, game_id: str) -> Optional[Game]:
        return self.games.get(game_id)

    # -- roster entries ----------------------------------------------------
    def add_roster_entry(self, entry: GameRosterEntry) -> GameRosterEntry:
        self.roster_entries[entry.id] = entry
        return entry

    def roster_for_game(self, game_id: str) -> List[GameRosterEntry]:
        return [e for e in self.roster_entries.values() if e.game_id == game_id]

    def roster_entry_for_player(
        self, game_id: str, player_id: str
    ) -> Optional[GameRosterEntry]:
        for e in self.roster_entries.values():
            if e.game_id == game_id and e.player_id == player_id:
                return e
        return None

    def roster_entries_for_player(self, player_id: str) -> List[GameRosterEntry]:
        return [e for e in self.roster_entries.values() if e.player_id == player_id]

    # -- availability ------------------------------------------------------
    def upsert_availability(self, av: GameAvailability) -> GameAvailability:
        self.availability[av.id] = av
        return av

    def availability_for_game(self, game_id: str) -> List[GameAvailability]:
        return [a for a in self.availability.values() if a.game_id == game_id]

    def availability_for_player(
        self, game_id: str, player_id: str
    ) -> Optional[GameAvailability]:
        for a in self.availability.values():
            if a.game_id == game_id and a.player_id == player_id:
                return a
        return None

    def availability_entries_for_player(self, player_id: str) -> List[GameAvailability]:
        return [a for a in self.availability.values() if a.player_id == player_id]

    # -- substitutes -------------------------------------------------------
    def add_substitute(self, sub: SubstituteEnrollment) -> SubstituteEnrollment:
        self.substitutes[sub.id] = sub
        return sub

    def substitutes_for_game(self, game_id: str) -> List[SubstituteEnrollment]:
        return [s for s in self.substitutes.values() if s.game_id == game_id]

    def substitute_for_player(
        self, game_id: str, player_id: str
    ) -> Optional[SubstituteEnrollment]:
        for s in self.substitutes.values():
            if s.game_id == game_id and s.player_id == player_id:
                return s
        return None

    def substitute_enrollments_for_player(self, player_id: str) -> List[SubstituteEnrollment]:
        return [s for s in self.substitutes.values() if s.player_id == player_id]

    # -- audit / notifications --------------------------------------------
    def add_audit(self, entry: AuditLog) -> AuditLog:
        self.audit.append(entry)
        return entry

    # -- sensitive-read audit (#124; ordering/validation/durability #426) ---
    #: Dedicated counter key for the audit log's own monotonic ordering key
    #: — deliberately NOT an id prefix (``next_id("daccess")`` already owns
    #: row ids; this is a separate counter, see domain/privacy.py's
    #: ORDERING section and SqlStore's identical ``_DATA_ACCESS_SEQ_PREFIX``).
    _DATA_ACCESS_SEQ_KEY = "__data_access_seq__"

    def _next_data_access_seq(self) -> int:
        with self._lock:
            value = self._counters.get(self._DATA_ACCESS_SEQ_KEY, 0) + 1
            self._counters[self._DATA_ACCESS_SEQ_KEY] = value
            return value

    def _validate_data_access(self, entry: DataAccessLog) -> None:
        """Closed-vocabulary + uniqueness guard (#426 review finding 5),
        mirroring SqlStore's Python-side check and migration 053's CHECK/
        UNIQUE constraints — so a MemoryStore-backed demo/test run rejects
        exactly what a real database would, rather than silently accepting
        an invalid outcome/category or a duplicate id list.append() never
        would have caught on its own (a plain list has no primary key).

        The uniqueness half is skipped while ``entry.id`` is still ``None``
        (#426 round-2 review finding 1): a durable write nested inside an
        ambient transaction is validated once EAGERLY, before an id has
        been assigned (id allocation is deferred to flush time — see
        ``add_data_access_durable``), and re-validated in full, id
        included, once flushed. Comparing on ``id`` while it is still
        unset would be worse than a no-op — TWO SEPARATE unassigned
        entries queued in the same ambient transaction both carry
        ``id=None``, and ``None == None`` is True, so an unguarded
        comparison here would misreport the second of any two legitimate
        nested denials as a duplicate of the first.
        """
        cat = entry.category
        cat_value = cat.value if isinstance(cat, SensitiveFieldCategory) else cat
        if cat_value not in {c.value for c in SensitiveFieldCategory}:
            raise ValidationError(
                f"Unknown sensitive-field category {cat_value!r}.")
        if entry.outcome not in VALID_OUTCOMES:
            raise ValidationError(
                f"Unknown DataAccessLog outcome {entry.outcome!r}.")
        if entry.id is not None and (
                any(r.id == entry.id for r in self.data_access)
                or any(r.id == entry.id
                       for r in self._pending_durable_data_access)):
            raise IntegrityConflictError(
                "The change conflicts with an existing record.",
                details={"reason": "unique_violation"})

    def _assign_data_access_id(self, entry: DataAccessLog) -> None:
        """Assign ``entry.id`` if the caller left it unset (#426 round-2
        review finding 1). Called at the SAME moment ``entry.seq`` is
        assigned in every path below — never earlier — so an id is never
        handed out by a counter state an enclosing rollback could later
        erase; see ``domain/privacy.py``'s DURABLE ID ALLOCATION section.
        """
        if entry.id is None:
            entry.id = self.next_id("daccess")

    def add_data_access(self, entry: DataAccessLog) -> DataAccessLog:
        """Append one durable row and return it.

        The CALLER's ``entry`` object is mutated with its real id/seq (a
        courtesy — existing callers already read them back off the object
        they passed in), but neither the STORED row nor the RETURNED value
        is that same object (#426 round-2 review finding 4: "the exact
        caller-owned DataAccessLog object" stayed mutable through the write
        API — mutating it, or the return value, after insertion silently
        corrupted the durable record). Three distinct objects exist once
        this returns: the caller's ``entry``, the private copy appended to
        ``self.data_access``, and the private copy handed back — mutating
        any one of them can never reach either of the other two, matching
        SQL's inherent immutability (a Python object mutated after
        ``INSERT`` cannot retroactively change what was already written).
        """
        with self._lock:
            self._assign_data_access_id(entry)
            self._validate_data_access(entry)
            entry.seq = self._next_data_access_seq()
            self.data_access.append(copy.copy(entry))
            return copy.copy(entry)

    def add_data_access_durable(self, entry: DataAccessLog) -> DataAccessLog:
        """Write a row that survives regardless of what an AMBIENT
        (already-open, possibly nested) transaction later does — including a
        later rollback (#426 review finding 4). See SqlStore's identical
        method for the full rationale; this store's version is the SAME
        "failure-safe transaction lifecycle hook" shape, just against
        ``_txn_depth``/``_restore()`` instead of a real database connection.

        Not nested (``_txn_depth == 0``): id/seq are assigned right here,
        exactly like ``add_data_access`` — including that method's own
        caller/stored/returned detachment (#426 round-2 review finding 4).
        Nested: id/seq are deliberately NOT assigned here (#426 round-2
        review finding 1) — this entry's row is queued and both are
        assigned at FLUSH time instead, by
        ``_flush_pending_durable_data_access``, once the ambient transaction
        that could still roll back has FULLY concluded. Allocating either
        one now, inside that still-open ambient transaction, is the exact
        bug that closes: the queued row survives a rollback by construction
        (see below), but ``next_id()``'s counter does NOT — it rolls back
        with everything else — so an id allocated here would already be
        orphaned from the counter by the time this row is flushed, and the
        NEXT allocation would hand out the identical id again and collide
        with the durable row already sitting there. Category/outcome are
        still checked eagerly, below, so a malformed entry fails loudly at
        the ORIGINAL call site rather than much later during an unrelated
        flush; the id-uniqueness half of that same check is there today a
        no-op (no id exists to compare yet) and is re-run in full, id
        included, at flush time.

        The QUEUED row is ALSO a private detached copy, made HERE before it
        is ever appended to ``_pending_durable_data_access`` (#426 round-2
        review finding 4's "including durable queued rows") — so mutating
        the caller's ``entry`` (or the value this method returns) ANY time
        between queueing and the eventual flush can never reach the row
        that actually gets persisted.
        """
        with self._lock:
            if self._txn_depth == 0:
                self._assign_data_access_id(entry)
                self._validate_data_access(entry)
                entry.seq = self._next_data_access_seq()
                self.data_access.append(copy.copy(entry))
            else:
                self._validate_data_access(entry)
                self._pending_durable_data_access.append(copy.copy(entry))
        return copy.copy(entry)

    def _flush_pending_durable_data_access(self) -> None:
        # Each queued entry is ALREADY a private copy add_data_access_durable
        # made before queueing it (#426 round-2 review finding 4) — no
        # caller holds a reference to it, so assigning id/seq in place and
        # appending it directly (no further copy) is safe.
        pending, self._pending_durable_data_access = (
            self._pending_durable_data_access, [])
        for entry in pending:
            self._assign_data_access_id(entry)
            self._validate_data_access(entry)
            entry.seq = self._next_data_access_seq()
            self.data_access.append(entry)

    def list_data_access(self, subject_type=None, subject_id=None,
                         category=None) -> List[DataAccessLog]:
        """Sensitive-read rows, optionally filtered to one subject and/or one
        category — the "who read this person's data" query (#124).

        Returns immutable snapshots (#426 review finding 5), NOT the live
        stored objects: this store used to hand back the SAME dataclass
        instances it holds internally, so a caller mutating one field of a
        "returned" row silently corrupted the durable record — an audit
        trail that is not append-only is not an audit trail. Ordered by
        ``seq``, matching SqlStore's ``ORDER BY seq`` (never by ``id``,
        which is a textual label sorted lexicographically, not
        chronologically — see domain/privacy.py's ORDERING section).
        """
        rows = self.data_access
        if subject_type is not None:
            rows = [r for r in rows if r.subject_type == subject_type]
        if subject_id is not None:
            rows = [r for r in rows if r.subject_id == subject_id]
        if category is not None:
            cat_value = (category.value
                        if isinstance(category, SensitiveFieldCategory)
                        else category)
            rows = [r for r in rows
                   if (r.category.value
                       if isinstance(r.category, SensitiveFieldCategory)
                       else r.category) == cat_value]
        return [copy.copy(r) for r in sorted(rows, key=lambda r: r.seq)]

    def audit_for_game(self, game_id: str) -> List[AuditLog]:
        return [a for a in self.audit if a.game_id == game_id]

    def add_notification(self, event: NotificationEvent) -> NotificationEvent:
        self.notifications.append(event)
        return event

    def notifications_for_game(self, game_id: str) -> List[NotificationEvent]:
        return [n for n in self.notifications if n.game_id == game_id]

    # -- organization & arena setup ---------------------------------------
    # Umbrella competition entity: Program (#233, formerly League).
    def add_program(self, program: Program) -> Program:
        self.programs[program.id] = program
        return program

    def get_program(self, program_id: str) -> Optional[Program]:
        return self.programs.get(program_id)

    def get_program_for_update(self, program_id: str) -> Optional[Program]:
        # Interface parity with SqlStore (#158): transaction() holds self._lock
        # for its whole body, so no row lock is needed to keep a concurrent
        # timezone update from interleaving with the caller's read-then-write.
        return self.programs.get(program_id)

    def add_season(self, season: Season) -> Season:
        self.seasons[season.id] = season
        return season

    def get_season(self, season_id: str) -> Optional[Season]:
        return self.seasons.get(season_id)

    def get_season_for_update(self, season_id: str) -> Optional[Season]:
        # No row locking needed (#159): transaction() holds self._lock for its
        # entire body, so a concurrent archive/reopen can't interleave with the
        # caller's check-then-write. Provided for interface parity with SqlStore.
        return self.seasons.get(season_id)

    def seasons_for_program(self, program_id: str) -> List[Season]:
        return [s for s in self.seasons.values() if s.program_id == program_id]

    # Permanent competition grouping: League (#233/#283). Now a permanent
    # child of a Program (``program_id``), not of a Season.
    def add_league(self, league: League) -> League:
        self.leagues[league.id] = league
        return league

    def get_league(self, league_id: str) -> Optional[League]:
        return self.leagues.get(league_id)

    def get_league_for_update(self, league_id: str) -> Optional[League]:
        # No row locking needed (#159): transaction() holds self._lock for its
        # whole body, so a concurrent Team create/rebind or delete can't
        # interleave with the caller's check-then-write. Interface parity with
        # SqlStore, whose implementation takes SELECT ... FOR UPDATE.
        return self.leagues.get(league_id)

    def leagues_for_program(self, program_id: str) -> List[League]:
        return [lg for lg in self.leagues.values()
                if lg.program_id == program_id]

    # LeagueSeason (#283): a permanent League's participation in one Season.
    def add_league_season(self, ls: LeagueSeason) -> LeagueSeason:
        self.league_seasons[ls.id] = ls
        return ls

    def get_league_season_for_update(
            self, ls_id: str) -> Optional[LeagueSeason]:
        # #369 row-lock parity with SqlStore's SELECT ... FOR UPDATE:
        # transaction() holds self._lock for its whole body here, so a
        # plain read already serializes against every concurrent writer.
        return self.league_seasons.get(ls_id)

    def get_league_season(self, ls_id: str) -> Optional[LeagueSeason]:
        return self.league_seasons.get(ls_id)

    def all_league_seasons(self) -> List[LeagueSeason]:
        return list(self.league_seasons.values())

    def save_league_season(self, ls: LeagueSeason) -> LeagueSeason:
        self.league_seasons[ls.id] = ls
        return ls

    def delete_league_season(self, ls_id: str) -> None:
        self.league_seasons.pop(ls_id, None)

    def league_seasons_for_season(self, season_id: str) -> List[LeagueSeason]:
        return [ls for ls in self.league_seasons.values()
                if ls.season_id == season_id]

    def league_seasons_for_league(self, league_id: str) -> List[LeagueSeason]:
        return [ls for ls in self.league_seasons.values()
                if ls.league_id == league_id]

    def league_season_for(self, league_id: str,
                          season_id: str) -> Optional[LeagueSeason]:
        return next((ls for ls in self.league_seasons.values()
                     if ls.league_id == league_id
                     and ls.season_id == season_id), None)

    def add_division(self, division: Division) -> Division:
        self.divisions[division.id] = division
        return division

    def get_division_for_update(
            self, division_id: str) -> Optional[Division]:
        # #369 row-lock parity with SqlStore's SELECT ... FOR UPDATE:
        # transaction() holds self._lock for its whole body here, so a
        # plain read already serializes against every concurrent writer.
        return self.divisions.get(division_id)

    def get_division(self, division_id: str) -> Optional[Division]:
        return self.divisions.get(division_id)

    def divisions_for_league_season(
            self, league_season_id: str) -> List[Division]:
        return [d for d in self.divisions.values()
                if d.league_season_id == league_season_id]

    def divisions_for_season(self, season_id: str) -> List[Division]:
        """Every Division in a Season, across all its LeagueSeasons (#283)."""
        ls_ids = {ls.id for ls in self.league_seasons.values()
                  if ls.season_id == season_id}
        return [d for d in self.divisions.values()
                if d.league_season_id in ls_ids]

    def add_club(self, club: Club) -> Club:
        self.clubs[club.id] = club
        return club

    def get_club(self, club_id: str) -> Optional[Club]:
        return self.clubs.get(club_id)

    def get_club_for_update(self, club_id: str) -> Optional[Club]:
        # No row locking needed (#266/#201): transaction() holds self._lock for
        # its entire body, so a concurrent delete can't interleave with the
        # caller's check-then-write. Provided for interface parity with SqlStore.
        return self.clubs.get(club_id)

    def save_club(self, club: Club) -> Club:
        self.clubs[club.id] = club
        return club

    def get_team(self, team_id: str) -> Optional[Team]:
        return self.teams.get(team_id)

    def get_team_for_update(self, team_id: str) -> Optional[Team]:
        # No row locking needed (#266): transaction() holds self._lock for its
        # entire body, so a concurrent delete can't interleave with the caller's
        # check-then-write. Provided for interface parity with SqlStore.
        return self.teams.get(team_id)

    def teams_for_program(self, program_id: str) -> List[Team]:
        return [t for t in self.teams.values() if t.program_id == program_id]

    # -- season team registrations (#180) ----------------------------------
    def add_season_team_registration(
            self, reg: SeasonTeamRegistration) -> SeasonTeamRegistration:
        self.season_team_registrations[reg.id] = reg
        return reg

    def get_season_team_registration_for_update(
            self, reg_id: str) -> Optional[SeasonTeamRegistration]:
        # #369 row-lock parity with SqlStore's SELECT ... FOR UPDATE:
        # transaction() holds self._lock for its whole body here, so a
        # plain read already serializes against every concurrent writer.
        return self.season_team_registrations.get(reg_id)

    def get_season_team_registration(
            self, reg_id: str) -> Optional[SeasonTeamRegistration]:
        return self.season_team_registrations.get(reg_id)

    def save_season_team_registration(
            self, reg: SeasonTeamRegistration) -> SeasonTeamRegistration:
        self.season_team_registrations[reg.id] = reg
        return reg

    def all_season_team_registrations(self) -> List[SeasonTeamRegistration]:
        return list(self.season_team_registrations.values())

    def registrations_for_league_season(
            self, league_season_id: str) -> List[SeasonTeamRegistration]:
        return [r for r in self.season_team_registrations.values()
                if r.league_season_id == league_season_id]

    def registration_for_team_in_league_season(
            self, league_season_id: str,
            team_id: str) -> Optional[SeasonTeamRegistration]:
        return next((r for r in self.season_team_registrations.values()
                     if r.league_season_id == league_season_id
                     and r.team_id == team_id), None)

    def registrations_for_team_in_league_season(
            self, league_season_id: str,
            team_id: str) -> List[SeasonTeamRegistration]:
        """Every row at this EXACT (team, LeagueSeason) key (#331 review
        round 19). Normally 0 or 1 -- SQL's ``ux_team_league_season`` unique
        index (migration 035) guarantees it there -- but this store has no
        equivalent enforcement on add/save, so legacy/corrupted data (or a
        write path predating that guarantee) can leave more than one row
        here. Callers that need "the" row use the shared conflict-detecting
        wrapper built on this, never assume a bare lookup is unambiguous."""
        return [r for r in self.season_team_registrations.values()
                if r.league_season_id == league_season_id
                and r.team_id == team_id]

    def registrations_for_season(
            self, season_id: str) -> List[SeasonTeamRegistration]:
        """Every registration in a Season, across all its LeagueSeasons (#283)."""
        ls_ids = {ls.id for ls in self.league_seasons.values()
                  if ls.season_id == season_id}
        return [r for r in self.season_team_registrations.values()
                if r.league_season_id in ls_ids]

    def registration_for_team_in_season(
            self, season_id: str, team_id: str
    ) -> Optional[SeasonTeamRegistration]:
        """A team's registration in a Season, across its LeagueSeasons (#283
        back-compat convenience)."""
        return next((r for r in self.registrations_for_season(season_id)
                     if r.team_id == team_id), None)

    # -- team → permanent League migration decisions (#283 migration 035) ---
    def add_team_league_migration_decision(
            self, decision: TeamLeagueMigrationDecision
    ) -> TeamLeagueMigrationDecision:
        self.team_league_migration_decisions[decision.id] = decision
        return decision

    def all_team_league_migration_decisions(
            self) -> List[TeamLeagueMigrationDecision]:
        return list(self.team_league_migration_decisions.values())

    def team_league_migration_decision_for(
            self, team_id: str) -> Optional[TeamLeagueMigrationDecision]:
        return next((d for d in self.team_league_migration_decisions.values()
                     if d.team_id == team_id), None)

    def delete_team_league_migration_decision(self, decision_id: str) -> None:
        self.team_league_migration_decisions.pop(decision_id, None)

    # -- season venue access (#233 Slice E) ---------------------------------
    def add_season_venue_access(
            self, sva: SeasonVenueAccess) -> SeasonVenueAccess:
        self.season_venue_access[sva.id] = sva
        return sva

    def get_season_venue_access_for_update(
            self, sva_id: str) -> Optional[SeasonVenueAccess]:
        # #369 row-lock parity with SqlStore's SELECT ... FOR UPDATE:
        # transaction() holds self._lock for its whole body here, so a
        # plain read already serializes against every concurrent writer.
        return self.season_venue_access.get(sva_id)

    def get_season_venue_access(
            self, sva_id: str) -> Optional[SeasonVenueAccess]:
        return self.season_venue_access.get(sva_id)

    def save_season_venue_access(
            self, sva: SeasonVenueAccess) -> SeasonVenueAccess:
        self.season_venue_access[sva.id] = sva
        return sva

    def all_season_venue_access(self) -> List[SeasonVenueAccess]:
        return list(self.season_venue_access.values())

    def season_venue_access_for_season(
            self, season_id: str) -> List[SeasonVenueAccess]:
        return [a for a in self.season_venue_access.values()
                if a.season_id == season_id]

    def season_venue_access_for_venue(
            self, venue_id: str) -> List[SeasonVenueAccess]:
        return [a for a in self.season_venue_access.values()
                if a.venue_id == venue_id]

    def season_venue_access_for_pair(
            self, season_id: str, venue_id: str) -> Optional[SeasonVenueAccess]:
        return next((a for a in self.season_venue_access.values()
                     if a.season_id == season_id and a.venue_id == venue_id), None)

    def add_organization(self, org: Organization) -> Organization:
        self.organizations[org.id] = org
        return org

    def get_organization_for_update(
            self, org_id: str) -> Optional[Organization]:
        # #369 row-lock parity with SqlStore's SELECT ... FOR UPDATE:
        # transaction() holds self._lock for its whole body here, so a
        # plain read already serializes against every concurrent writer.
        return self.organizations.get(org_id)

    def get_organization(self, org_id: str) -> Optional[Organization]:
        return self.organizations.get(org_id)

    def add_venue(self, venue: Venue) -> Venue:
        self.venues[venue.id] = venue
        return venue

    def get_venue_for_update(self, venue_id: str) -> Optional[Venue]:
        # #369 row-lock parity with SqlStore's SELECT ... FOR UPDATE:
        # transaction() holds self._lock for its whole body here, so a
        # plain read already serializes against every concurrent writer.
        return self.venues.get(venue_id)

    def get_venue(self, venue_id: str) -> Optional[Venue]:
        return self.venues.get(venue_id)

    def add_rink(self, rink: Rink) -> Rink:
        self.rinks[rink.id] = rink
        return rink

    def get_rink(self, rink_id: str) -> Optional[Rink]:
        return self.rinks.get(rink_id)

    def get_rink_for_update(self, rink_id: str) -> Optional[Rink]:
        # No row locking needed: transaction() holds self._lock for its entire
        # body, so ice-slot writes on a rink already serialize. Provided for
        # interface parity with SqlStore (#158 review).
        return self.rinks.get(rink_id)

    def add_ice_slot(self, slot: IceSlot) -> IceSlot:
        self.ice_slots[slot.id] = slot
        return slot

    def add_scheduling_policy(self, policy: SchedulingPolicy) -> SchedulingPolicy:
        self.scheduling_policies[policy.id] = policy
        return policy

    def get_scheduling_policy(self, policy_id: str) -> Optional[SchedulingPolicy]:
        return self.scheduling_policies.get(policy_id)

    def save_scheduling_policy(self, policy: SchedulingPolicy) -> SchedulingPolicy:
        self.scheduling_policies[policy.id] = policy
        return policy

    def delete_scheduling_policy(self, policy_id: str) -> None:
        self.scheduling_policies.pop(policy_id, None)

    def all_scheduling_policies(self) -> List[SchedulingPolicy]:
        return list(self.scheduling_policies.values())

    # -- immutable schedule scenarios (#378) -----------------------------
    def add_schedule_scenario(self, scenario: ScheduleScenario) -> ScheduleScenario:
        # SQL persistence naturally serializes nested JSON.  Deep-copy here for
        # parity so a caller mutating the response object cannot mutate the
        # historical snapshot held by the in-memory store.
        stored = copy.deepcopy(scenario)
        self.schedule_scenarios[stored.id] = stored
        return copy.deepcopy(stored)

    def get_schedule_scenario(self, scenario_id: str) -> Optional[ScheduleScenario]:
        scenario = self.schedule_scenarios.get(scenario_id)
        return copy.deepcopy(scenario) if scenario is not None else None

    def get_schedule_scenario_for_update(
            self, scenario_id: str) -> Optional[ScheduleScenario]:
        return self.get_schedule_scenario(scenario_id)

    def all_schedule_scenarios(self) -> List[ScheduleScenario]:
        return [copy.deepcopy(s) for s in self.schedule_scenarios.values()]

    def find_scheduling_policy(self, scope_type, scope_id):
        scope_type = getattr(scope_type, "value", scope_type)
        return next(
            (p for p in self.scheduling_policies.values()
             if getattr(p.scope_type, "value", p.scope_type) == scope_type
             and p.scope_id == scope_id), None)

    def get_ice_slot_for_update(self, slot_id: str) -> Optional[IceSlot]:
        # #369 row-lock parity with SqlStore's SELECT ... FOR UPDATE:
        # transaction() holds self._lock for its whole body here, so a
        # plain read already serializes against every concurrent writer.
        return self.ice_slots.get(slot_id)

    def get_ice_slot(self, slot_id: str) -> Optional[IceSlot]:
        return self.ice_slots.get(slot_id)

    def game_using_ice_slot(self, slot_id: str) -> Optional[Game]:
        for g in self.games.values():
            if g.ice_slot_id == slot_id and not g.cancelled:
                return g
        return None

    # -- officials (#30) ---------------------------------------------------
    def add_official(self, official: Official) -> Official:
        self.officials[official.id] = official
        return official

    def get_official(self, official_id: str) -> Optional[Official]:
        return self.officials.get(official_id)

    def get_official_for_update(self, official_id: str) -> Optional[Official]:
        # See get_team_for_update: transaction() holds self._lock for its whole
        # body, so a plain read already serializes with a concurrent delete.
        return self.officials.get(official_id)

    def all_officials(self) -> List[Official]:
        return list(self.officials.values())

    def save_official(self, official: Official) -> Official:
        self.officials[official.id] = official
        return official

    def add_official_assignment(self, a: OfficialAssignment) -> OfficialAssignment:
        self.official_assignments[a.id] = a
        return a

    def save_official_assignment(self, a: OfficialAssignment) -> OfficialAssignment:
        self.official_assignments[a.id] = a
        return a

    def get_official_assignment(self, assignment_id: str) -> Optional[OfficialAssignment]:
        return self.official_assignments.get(assignment_id)

    def all_official_assignments(self) -> List[OfficialAssignment]:
        return list(self.official_assignments.values())

    def assignments_for_game(self, game_id: str) -> List[OfficialAssignment]:
        return [a for a in self.official_assignments.values() if a.game_id == game_id]

    def assignments_for_official(self, official_id: str) -> List[OfficialAssignment]:
        return [a for a in self.official_assignments.values()
                if a.official_id == official_id]

    def remove_official_assignment(self, assignment_id: str) -> None:
        self.official_assignments.pop(assignment_id, None)

    # -- game results (#31) ------------------------------------------------
    def add_game_result(self, result: GameResult) -> GameResult:
        self.game_results[result.id] = result
        return result

    def save_game_result(self, result: GameResult) -> GameResult:
        self.game_results[result.id] = result
        return result

    def result_for_game(self, game_id: str) -> Optional[GameResult]:
        for r in self.game_results.values():
            if r.game_id == game_id:
                return r
        return None

    def all_game_results(self) -> List[GameResult]:
        return list(self.game_results.values())

    # -- feed notifications (#32) ------------------------------------------
    def add_notification_feed(self, n: Notification) -> Notification:
        self.feed_notifications[n.id] = n
        return n

    def save_notification_feed(self, n: Notification) -> Notification:
        self.feed_notifications[n.id] = n
        return n

    def get_notification_feed(self, notification_id: str) -> Optional[Notification]:
        return self.feed_notifications.get(notification_id)

    def all_notifications_feed(self) -> List[Notification]:
        return list(self.feed_notifications.values())

    # -- per-recipient read state (#57) ------------------------------------
    def get_notification_recipient(
            self, recipient_id: str) -> Optional[NotificationRecipient]:
        return self.notif_recipients.get(recipient_id)

    def save_notification_recipient(
            self, r: NotificationRecipient) -> NotificationRecipient:
        self.notif_recipients[r.id] = r
        return r

    def recipients_for_actor(
            self, actor_key: str) -> List[NotificationRecipient]:
        return [r for r in self.notif_recipients.values()
                if r.actor_key == actor_key]

    # -- notification delivery queue (#58) ---------------------------------
    def add_notification_delivery(
            self, d: NotificationDelivery) -> NotificationDelivery:
        self.notif_deliveries[d.id] = d
        return d

    def save_notification_delivery(
            self, d: NotificationDelivery) -> NotificationDelivery:
        self.notif_deliveries[d.id] = d
        return d

    def get_notification_delivery(
            self, delivery_id: str) -> Optional[NotificationDelivery]:
        return self.notif_deliveries.get(delivery_id)

    def deliveries_for_notification(
            self, notification_id: str) -> List[NotificationDelivery]:
        return [d for d in self.notif_deliveries.values()
                if d.notification_id == notification_id]

    def all_notification_deliveries(self) -> List[NotificationDelivery]:
        return list(self.notif_deliveries.values())

    def pending_deliveries(
            self, max_attempts: int) -> List[NotificationDelivery]:
        """Deliverable rows: still pending, or failed with attempts to spare."""
        return [d for d in self.notif_deliveries.values()
                if d.status == DeliveryStatus.PENDING
                or (d.status == DeliveryStatus.FAILED
                    and d.attempts < max_attempts)]

    # -- contact registry (#60) --------------------------------------------
    def add_contact_destination(
            self, c: ContactDestination) -> ContactDestination:
        self.contact_destinations[c.id] = c
        return c

    def save_contact_destination(
            self, c: ContactDestination) -> ContactDestination:
        self.contact_destinations[c.id] = c
        return c

    def get_contact_destination(
            self, recipient_ref: str, channel) -> Optional[ContactDestination]:
        for c in self.contact_destinations.values():
            if c.recipient_ref == recipient_ref and c.channel == channel:
                return c
        return None

    def get_contact_destination_for_update(
            self, contact_id: str) -> Optional[ContactDestination]:
        # No row locking needed (#426 review finding 4, mirroring
        # get_team_for_update's identical comment): transaction() holds
        # self._lock for its entire body, so a concurrent upsert can't
        # interleave with the caller's fetch-then-write. Provided for
        # interface parity with SqlStore.
        return self.contact_destinations.get(contact_id)

    def all_contact_destinations(self) -> List[ContactDestination]:
        return list(self.contact_destinations.values())

    # -- device token registry (#65) ---------------------------------------
    def add_device_token(self, t: DeviceToken) -> DeviceToken:
        self.device_tokens[t.id] = t
        return t

    def save_device_token(self, t: DeviceToken) -> DeviceToken:
        self.device_tokens[t.id] = t
        return t

    def get_device_token(self, token_id: str) -> Optional[DeviceToken]:
        return self.device_tokens.get(token_id)

    def get_device_token_for_update(
            self, token_id: str) -> Optional[DeviceToken]:
        # No row locking needed (#426 round-4 review finding 1, mirroring
        # get_contact_destination_for_update's identical comment):
        # transaction() holds self._lock for its entire body, so a
        # concurrent upsert can't interleave with the caller's
        # fetch-then-write. Provided for interface parity with SqlStore.
        return self.device_tokens.get(token_id)

    def upsert_device_token(
            self, recipient_ref: str, provider: str, token: str,
            label: Optional[str]) -> DeviceToken:
        """Insert-or-update a DeviceToken keyed by its natural key
        (recipient_ref, token) — the Memory-store sibling of
        SqlStore.upsert_device_token (#426 round-4 review finding 1), same
        docstring contract. Callers wrap this in ``store.transaction()``
        (same requirement as every other multi-step Memory mutation), whose
        ``self._lock`` fully serializes the find-then-mutate-or-create
        below against any concurrent store access — the SAME "strongest
        isolation, for free" guarantee ``transaction()``'s own docstring
        describes, not a per-call lock here."""
        existing = self.get_device_token_by_value(recipient_ref, token)
        if existing is not None:
            existing.provider = provider
            existing.label = label
            existing.active = True
            return existing
        t = DeviceToken(
            id=self.next_id("devtok"), recipient_ref=recipient_ref,
            provider=provider, token=token, label=label, active=True)
        self.device_tokens[t.id] = t
        return t

    def get_device_token_by_value(
            self, recipient_ref: str, token: str) -> Optional[DeviceToken]:
        for t in self.device_tokens.values():
            if t.recipient_ref == recipient_ref and t.token == token:
                return t
        return None

    def device_tokens_for(self, recipient_ref: str) -> List[DeviceToken]:
        return [t for t in self.device_tokens.values()
                if t.recipient_ref == recipient_ref]

    def active_device_token_for(
            self, recipient_ref: str) -> Optional[DeviceToken]:
        for t in self.device_tokens.values():
            if t.recipient_ref == recipient_ref and t.active:
                return t
        return None

    def all_device_tokens(self) -> List[DeviceToken]:
        return list(self.device_tokens.values())

    def all_calendar_feed_tokens(self) -> List[CalendarFeedToken]:
        return list(self.calendar_feed_tokens.values())

    def all_notification_preferences(self) -> List[NotificationPreference]:
        return list(self.notification_preferences.values())

    # -- official availability (#88) ---------------------------------------
    def add_official_availability(self, a: OfficialAvailability) -> OfficialAvailability:
        self.official_availability[a.id] = a
        return a

    def get_official_availability(self, avail_id: str) -> Optional[OfficialAvailability]:
        return self.official_availability.get(avail_id)

    def delete_official_availability(self, avail_id: str) -> None:
        self.official_availability.pop(avail_id, None)

    def availability_for_official(self, official_id: str) -> List[OfficialAvailability]:
        return [a for a in self.official_availability.values()
                if a.official_id == official_id]

    def save_official_availability(
            self, a: OfficialAvailability) -> OfficialAvailability:
        self.official_availability[a.id] = a
        return a

    # -- calendar feed tokens (#82) ----------------------------------------
    def add_calendar_feed_token(self, t: CalendarFeedToken) -> CalendarFeedToken:
        self.calendar_feed_tokens[t.id] = t
        return t

    def save_calendar_feed_token(self, t: CalendarFeedToken) -> CalendarFeedToken:
        self.calendar_feed_tokens[t.id] = t
        return t

    def get_calendar_feed_token(self, token_id: str) -> Optional[CalendarFeedToken]:
        return self.calendar_feed_tokens.get(token_id)

    def get_calendar_feed_token_by_hash(
            self, token_hash: str) -> Optional[CalendarFeedToken]:
        for t in self.calendar_feed_tokens.values():
            if t.token_hash == token_hash:
                return t
        return None

    def calendar_feed_tokens_for(
            self, actor_type: str, actor_ref: str) -> List[CalendarFeedToken]:
        return [t for t in self.calendar_feed_tokens.values()
                if t.actor_type == actor_type and t.actor_ref == actor_ref]

    # -- notification preferences (#81) ------------------------------------
    def save_notification_preference(
            self, p: NotificationPreference) -> NotificationPreference:
        self.notification_preferences[p.id] = p
        return p

    def get_notification_preference(
            self, recipient_ref: str, channel) -> Optional[NotificationPreference]:
        for p in self.notification_preferences.values():
            if p.recipient_ref == recipient_ref and p.channel == channel:
                return p
        return None

    def preferences_for_recipient(
            self, recipient_ref: str) -> List[NotificationPreference]:
        return [p for p in self.notification_preferences.values()
                if p.recipient_ref == recipient_ref]

    # -- installation claim state (#174) -----------------------------------
    def add_installation_state(
            self, state: InstallationState) -> InstallationState:
        self.installation_state[state.id] = state
        return state

    def get_installation_state(
            self, state_id: str) -> Optional[InstallationState]:
        return self.installation_state.get(state_id)

    # -- user accounts (#67) -------------------------------------------------
    def add_user_account(self, a: UserAccount) -> UserAccount:
        self.user_accounts[a.id] = a
        return a

    def save_user_account(self, a: UserAccount) -> UserAccount:
        self.user_accounts[a.id] = a
        return a

    def get_user_account(self, account_id: str) -> Optional[UserAccount]:
        return self.user_accounts.get(account_id)

    def get_user_account_for_update(self, account_id: str) -> Optional[UserAccount]:
        # No row lock needed (#266 review): transaction() holds self._lock for
        # its whole body, so set_active/rebind can't interleave. Interface parity
        # with SqlStore.
        return self.user_accounts.get(account_id)

    def get_user_account_by_username(self, username: str) -> Optional[UserAccount]:
        for a in self.user_accounts.values():
            if a.username == username:
                return a
        return None

    def all_user_accounts(self) -> List[UserAccount]:
        return list(self.user_accounts.values())

    # -- guardian ↔ junior links (#26) -------------------------------------
    def add_guardian_link(self, link: GuardianLink) -> GuardianLink:
        self.guardian_links[link.id] = link
        return link

    def save_guardian_link(self, link: GuardianLink) -> GuardianLink:
        self.guardian_links[link.id] = link
        return link

    def get_guardian_link(self, link_id: str) -> Optional[GuardianLink]:
        return self.guardian_links.get(link_id)

    def guardian_links_for(self, guardian_user_id: str) -> List[GuardianLink]:
        return [g for g in self.guardian_links.values()
                if g.guardian_user_id == guardian_user_id]

    def guardian_link_for(self, guardian_user_id: str,
                          player_id: str) -> Optional[GuardianLink]:
        for g in self.guardian_links.values():
            if g.guardian_user_id == guardian_user_id and g.player_id == player_id:
                return g
        return None

    def guardian_links_for_player(self, player_id: str) -> List[GuardianLink]:
        return [g for g in self.guardian_links.values() if g.player_id == player_id]

    def all_guardian_links(self) -> List[GuardianLink]:
        return list(self.guardian_links.values())

    # -- reschedule requests (#29) ------------------------------------------
    def add_reschedule_request(self, r: RescheduleRequest) -> RescheduleRequest:
        self.reschedule_requests[r.id] = r
        return r

    def save_reschedule_request(self, r: RescheduleRequest) -> RescheduleRequest:
        self.reschedule_requests[r.id] = r
        return r

    def get_reschedule_request(self, request_id: str) -> Optional[RescheduleRequest]:
        return self.reschedule_requests.get(request_id)

    def reschedule_requests_for_game(self, game_id: str) -> List[RescheduleRequest]:
        return [r for r in self.reschedule_requests.values() if r.game_id == game_id]

    def all_reschedule_requests(self) -> List[RescheduleRequest]:
        return list(self.reschedule_requests.values())

    # -- sessions (#74) ----------------------------------------------------
    def add_session(self, s: Session) -> Session:
        self.sessions[s.id] = s
        return s

    def save_session(self, s: Session) -> Session:
        self.sessions[s.id] = s
        return s

    def get_session(self, session_id: str) -> Optional[Session]:
        return self.sessions.get(session_id)

    def get_session_by_hash(self, token_hash: str) -> Optional[Session]:
        for s in self.sessions.values():
            if s.token_hash == token_hash:
                return s
        return None

    def sessions_for_user(self, user_id: str) -> List[Session]:
        return [s for s in self.sessions.values() if s.user_id == user_id]

    # -- per-user active Program/Season context (#159) ---------------------
    @contextmanager
    def active_context_mutex(self, user_id: str):
        """#386 — a documented no-op, matching ``SqlStore``'s spelling.

        ``transaction()`` holds the process-wide lock for its whole block, so
        a mutation unit here is already serialized against every context write
        — strictly stronger than the cross-transaction mutex PostgreSQL needs.
        """
        yield

    # -- epoch fence (PR #423 redesign) -------------------------------------
    def epoch_fence_acquire_exclusive(self, key: str) -> None:
        """The LOCK side stays a documented no-op, matching ``SqlStore``'s
        spelling, and for the analogous reason: ``self._lock`` (this store's
        process-wide, per-INSTANCE RLock) already wraps the WHOLE
        ``transaction()`` body this method is always called from inside, on
        the only Python object that could ever be racing (``InMemoryStore``
        cannot participate in independent workers at all -- two separate
        instances share zero state, so "independent Memory workers
        coordinating" is not a deployment shape this store needs to defend
        against; see the design's §5). That serialization already gives every
        OTHER ``transaction()``-wrapped writer full mutual exclusion against
        this one.

        A REAL BLOCKING acquire here would still be wrong, for the same
        reason it is on SQLite: waiting on any lock/condition while
        ``self._lock`` is already held by this same call frame (it always is;
        this method's contract requires it be called from inside an open
        ``transaction()``) can deadlock against a scoped read whose shared
        hold spans a ``produce()`` call that itself needs ``self._lock``:
        this thread holds ``self._lock`` and blocks waiting for the read to
        release; the read, mid-``produce()``, blocks waiting for
        ``self._lock``. Proven directly (a real ``ContextSwitchGate``-backed
        attempt at this method reproduces the deadlock on demand) before
        settling on the no-op, exactly mirroring the AB-BA hazard
        ``context_gate.py:107-115``'s LOCK ORDER note already documents for
        the in-process gate.

        THE VERSION BUMP (round-N review finding 1) is NOT a no-op, though,
        and is what actually closes the same-process torn-read window this
        docstring used to just document as an accepted gap. Every one of the
        17 fenced writers already calls this method as its first fence
        action; bumping ``self._epoch_fence_versions[key]`` here — a plain
        dict write, already covered by ``self._lock`` (held by the caller's
        open ``transaction()``) and by this store's own
        ``_snapshot()``/``_restore()`` rollback machinery — needs no NEW lock
        of any kind, so it cannot reintroduce the AB-BA hazard above. A
        scoped read samples ``current_epoch_fence_version(key)`` — for its
        OWN user key and separately for the global key, mirroring the
        advisory lock's own two acquisitions — before deriving its epoch and
        again after ``produce()`` returns, with NOTHING held in between; if a
        writer's bump for that SAME key landed inside that window, the two
        samples disagree and the read is discarded rather than served — see
        ``current_epoch_fence_version``'s own docstring and
        ``web/server.py``'s ``_read_under_context_gate``. This closes exactly
        the gap this docstring previously named as the accepted CONSEQUENCE
        of the lock staying a no-op: the newly-listed writers (account scope
        rebind/activate, Season/Program/League/LeagueSeason delete,
        venue-access revoke/delete, Team transfer, Official assign/unassign,
        Player/Guardian reassignment, selected-Season delete among them) now
        DO have a real backstop on Memory, without a lock ever crossing a
        ``produce()`` call.
        """
        self._epoch_fence_versions[key] = (
            self._epoch_fence_versions.get(key, 0) + 1)

    @contextmanager
    def epoch_fence_acquire_shared(self, key: str):
        """A documented no-op, for the same reason as the exclusive side
        above: since nothing on Memory ever holds this primitive's exclusive
        side as a genuine LOCK (the version bump it now also does is not a
        hold this shared side needs to wait for), there is never a genuine
        holder for this method to defer to — a real gate object here would be
        exercised but never actually contended, which is observationally
        identical to not having one. Yields ``True`` (not ``False``/fail-open)
        since this call never times out — it never even tries to wait. The
        real protection on Memory is the version counter
        (``current_epoch_fence_version``), sampled by the caller OUTSIDE this
        hold — see ``web/server.py``'s ``_read_under_context_gate``."""
        yield True

    def current_epoch_fence_version(self, key: str) -> int:
        """The CURRENT value of the persisted epoch-fence version counter FOR
        ``key`` (round-N review finding 1) — see ``SqlStore``'s own copy for
        the full contract this mirrors, including why this is keyed rather
        than a single shared counter. On Memory this is simply the live dict
        entry (``0`` if ``key`` has never been bumped), read under
        ``self._lock`` for the same reason every other read here is: two
        concurrent callers on this ONE shared instance (the web server's
        shape) must never tear each other's read of it."""
        with self._lock:
            return self._epoch_fence_versions.get(key, 0)

    def get_active_context(self, user_id: str) -> Optional[ActiveContext]:
        return self.user_active_context.get(user_id)

    def get_active_context_for_update(
            self, user_id: str) -> Optional[ActiveContext]:
        """#386 — the row-locking read, a documented no-op here.

        The process-wide lock this store takes for the whole
        ``transaction()`` block already serializes every writer, so the
        ordering guarantee ``SqlStore``'s ``SELECT ... FOR UPDATE`` buys is
        already held. Present so call sites can use ONE spelling on every
        backend rather than branching on the store type."""
        return self.user_active_context.get(user_id)

    def set_active_context(self, ctx: ActiveContext) -> ActiveContext:
        self.user_active_context[ctx.id] = ctx
        return ctx

    def delete_sessions_before(self, cutoff: datetime) -> int:
        """Delete finished sessions whose terminal time is before ``cutoff`` —
        revoked sessions by ``revoked_at``, otherwise expired ones by
        ``expires_at``. Active and recently-finished sessions are kept. Returns
        the number removed (#77)."""
        doomed = [sid for sid, s in self.sessions.items()
                  if (s.revoked_at is not None and s.revoked_at < cutoff)
                  or (s.revoked_at is None and s.expires_at < cutoff)]
        for sid in doomed:
            del self.sessions[sid]
        return len(doomed)

    def add_setup_audit(self, entry: SetupAuditLog) -> SetupAuditLog:
        self.setup_audit.append(entry)
        return entry

    # -- copy-forward commit idempotency ledger (#159 review round 2) ------
    def add_season_copy_forward_commit(
            self, row: SeasonCopyForwardCommit) -> SeasonCopyForwardCommit:
        """One committed Season per copy_forward_fingerprint, mirroring
        SqlStore's migration-053 UNIQUE index: a second attempt to record
        the SAME fingerprint raises the IDENTICAL IntegrityConflictError
        shape SqlStore's translated unique-violation raises (same message,
        same ``reason``), so ``commit_new_season_copy_forward``'s
        idempotent-replay handling (pre-check + retryable
        ConcurrencyConflictError on this residual race) is backend-
        agnostic. Safe under this store's own concurrency model without a
        real index: every call runs inside ``transaction()``'s process-wide
        lock (see its docstring), so the check-then-set below can never
        itself race, and a failed caller's writes are undone by
        ``transaction()``'s own snapshot/restore — the same atomicity
        SqlStore gets from the database rolling back an aborted
        transaction."""
        for existing in self.season_copy_forward_commits.values():
            if (existing.copy_forward_fingerprint
                    == row.copy_forward_fingerprint):
                raise IntegrityConflictError(
                    "This copy-forward was already committed.",
                    details={"reason": "copy_forward_already_committed"})
        self.season_copy_forward_commits[row.id] = row
        return row

    def get_season_copy_forward_commit_by_fingerprint(
            self, fingerprint: str) -> Optional[SeasonCopyForwardCommit]:
        for row in self.season_copy_forward_commits.values():
            if row.copy_forward_fingerprint == fingerprint:
                return row
        return None

    def season_copy_forward_commits_for_season(
            self, season_id: str) -> List[SeasonCopyForwardCommit]:
        """Every ledger row naming ``season_id`` as the Season a copy-forward
        commit produced (#159 review round 3) -- at most one in practice
        (each commit mints a brand-new Season and writes exactly one row
        for it, in the same transaction), but returned as a list like every
        other ``*_for_season`` dependency query so ``delete_season`` can
        itemize it the same way as team registrations/games/venue access."""
        return [row for row in self.season_copy_forward_commits.values()
                if row.season_id == season_id]

    def add_factory_reset_event(self, event: FactoryResetEvent) -> FactoryResetEvent:
        self.factory_reset_events.append(event)
        return event

    def all_factory_reset_events(self) -> List[FactoryResetEvent]:
        return sorted(self.factory_reset_events, key=lambda e: e.started_at)

    def get_factory_reset_challenge(self) -> Optional[FactoryResetChallenge]:
        with self._lock:
            return self._factory_reset_challenge

    def set_factory_reset_challenge(
            self, challenge: FactoryResetChallenge) -> FactoryResetChallenge:
        """Replace the single outstanding challenge (#256 review blocker
        5) — a new preview always supersedes any prior, unconsumed one."""
        with self._lock:
            self._factory_reset_challenge = challenge
        return challenge

    def clear_factory_reset_challenge(self) -> None:
        with self._lock:
            self._factory_reset_challenge = None

    def acquire_factory_reset_lock(self, lock: FactoryResetLock) -> bool:
        """Try to become the sole in-progress factory reset (#256 review
        round 1 blocker 5). Check-then-set under this store's own lock so
        two concurrent callers against the same in-memory store (the
        process-equivalent of two instances sharing one durable database)
        can never both win. Call ``release_stale_factory_reset_lock`` first
        so a lock an owner never released doesn't block forever (#256
        review round 2 blocker 3)."""
        with self._lock:
            if self._factory_reset_lock is not None:
                return False
            self._factory_reset_lock = lock
            return True

    def release_stale_factory_reset_lock(self, now) -> bool:
        """Reclaim the lock if its lease has expired (#256 review round 2
        blocker 3). Returns True if a stale lock was cleared."""
        with self._lock:
            existing = self._factory_reset_lock
            if existing is None or existing.expires_at >= now:
                return False
            self._factory_reset_lock = None
            return True

    def release_factory_reset_lock(self, token: str) -> None:
        """Compare-and-delete: only clear the lock if ``token`` matches the
        one currently held (#256 review round 2 blocker 3) — an
        unconditional clear would let a delayed release from a caller that
        no longer holds the current lock destroy a different caller's
        active one."""
        with self._lock:
            existing = self._factory_reset_lock
            if existing is not None and existing.token == token:
                self._factory_reset_lock = None

    def lock_clearable_tables_for_wipe(self) -> None:
        """No-op for the in-memory store (#256 review round 2 blocker 1):
        ``transaction()`` already holds ``self._lock`` for its ENTIRE body
        (see its docstring), so every mutating call this codebase makes
        through ``store.transaction()`` is already fully serialized against
        the recount-then-wipe sequence below — there is no separate
        per-table lock to take."""

    # -- listings (interface shared with the SQL store) -------------------
    def all_programs(self) -> List[Program]:
        return list(self.programs.values())

    def all_seasons(self) -> List[Season]:
        return list(self.seasons.values())

    def all_leagues(self) -> List[League]:
        return list(self.leagues.values())

    def all_divisions(self) -> List[Division]:
        return list(self.divisions.values())

    def all_clubs(self) -> List[Club]:
        return list(self.clubs.values())

    def all_teams(self) -> List[Team]:
        return list(self.teams.values())

    def all_organizations(self) -> List[Organization]:
        return list(self.organizations.values())

    def all_venues(self) -> List[Venue]:
        return list(self.venues.values())

    def all_rinks(self) -> List[Rink]:
        return list(self.rinks.values())

    def all_ice_slots(self) -> List[IceSlot]:
        return list(self.ice_slots.values())

    def all_games(self) -> List[Game]:
        return list(self.games.values())

    def delete_game(self, game_id: str) -> None:
        self.games.pop(game_id, None)

    # -- setup-entity deletion (#215 safe destructive actions) -------------
    # Hard deletes of a single record. The service layer runs a pre-write
    # dependency gate before calling these, so they never cascade.
    def delete_organization(self, org_id: str) -> None:
        self.organizations.pop(org_id, None)

    def delete_program(self, program_id: str) -> None:
        self.programs.pop(program_id, None)

    def delete_season(self, season_id: str) -> None:
        self.seasons.pop(season_id, None)

    def delete_league(self, league_id: str) -> None:
        self.leagues.pop(league_id, None)

    def delete_division(self, division_id: str) -> None:
        self.divisions.pop(division_id, None)

    def delete_club(self, club_id: str) -> None:
        self.clubs.pop(club_id, None)

    def delete_team(self, team_id: str) -> None:
        self.teams.pop(team_id, None)

    def delete_season_team_registration(self, registration_id: str) -> None:
        self.season_team_registrations.pop(registration_id, None)

    def delete_venue(self, venue_id: str) -> None:
        self.venues.pop(venue_id, None)

    def delete_season_venue_access(self, sva_id: str) -> None:
        self.season_venue_access.pop(sva_id, None)

    def delete_rink(self, rink_id: str) -> None:
        self.rinks.pop(rink_id, None)

    def delete_ice_slot(self, slot_id: str) -> None:
        self.ice_slots.pop(slot_id, None)

    def delete_official(self, official_id: str) -> None:
        self.officials.pop(official_id, None)

    def delete_player(self, player_id: str) -> None:
        self.players.pop(player_id, None)

    def all_setup_audit(self) -> List[SetupAuditLog]:
        return list(self.setup_audit)

    # -- saves (persist in-place mutations) -------------------------------
    # For the in-memory store these are effectively no-ops (the object is
    # already held by reference); they exist so the service layer can be
    # written backend-agnostically and the SQL store can persist updates.
    def save_game(self, game: Game) -> Game:
        self.games[game.id] = game
        return game

    def save_program(self, program: Program) -> Program:
        self.programs[program.id] = program
        return program

    def save_league(self, league: League) -> League:
        self.leagues[league.id] = league
        return league

    def save_venue(self, venue: Venue) -> Venue:
        self.venues[venue.id] = venue
        return venue

    def save_division(self, division: Division) -> Division:
        self.divisions[division.id] = division
        return division

    def save_organization(self, org: Organization) -> Organization:
        self.organizations[org.id] = org
        return org

    def save_season(self, season: Season) -> Season:
        self.seasons[season.id] = season
        return season

    def save_team(self, team: Team) -> Team:
        self.teams[team.id] = team
        return team

    def save_player(self, player: Player) -> Player:
        self.players[player.id] = player
        return player

    def save_ice_slot(self, slot: IceSlot) -> IceSlot:
        self.ice_slots[slot.id] = slot
        return slot

    def save_rink(self, rink: Rink) -> Rink:
        self.rinks[rink.id] = rink
        return rink

    def save_substitute(self, sub: SubstituteEnrollment) -> SubstituteEnrollment:
        self.substitutes[sub.id] = sub
        return sub

    def save_roster_entry(self, entry: GameRosterEntry) -> GameRosterEntry:
        self.roster_entries[entry.id] = entry
        return entry

    def save_availability(self, av: GameAvailability) -> GameAvailability:
        self.availability[av.id] = av
        return av
