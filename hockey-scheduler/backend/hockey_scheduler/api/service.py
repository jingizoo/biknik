"""API facade.

Each method maps 1:1 to an endpoint in docs/architecture/api-contract.md and
returns plain JSON-serializable dicts. Domain exceptions are caught and
returned as the structured ``{"error": {...}}`` shape so callers (and a future
web framework) never see Python tracebacks across the boundary.
"""

from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Callable, List, Optional

from ..domain import (
    AvailabilityStatus,
    CalendarFeedToken,
    ContactDestination,
    DeliveryStatus,
    Game,
    DeviceToken,
    IceSlotStatus,
    IceSlotType,
    NotificationAudience,
    NotificationChannel,
    NotificationKind,
    NotificationPreference,
    NotificationRecipient,
    Role,
    OfficialRole,
    Position,
    ResultStatus,
    RosterEntryStatus,
    SlotType,
    SubstituteStatus,
    intervals_overlap,
)
from ..domain.errors import (
    DomainError,
    NotAuthorizedError,
    NotFoundError,
    ValidationError,
)
from ..services import (
    ACTOR_TYPES,
    AccountService,
    GuardianService,
    DeliveryLoop,
    DeliveryWorker,
    RosterService,
    SetupService,
    build_ics,
    draft_schedule,
    hash_feed_token,
    new_feed_token,
    parse_csv_text,
    validate_import,
)
from ..services.league_scope import team_registration_valid
from ..services.notifier import push as _push_notification
from ..store import InMemoryStore
from .v1_setup_adapter import program_to_v1, season_to_v1, team_to_v1


def _jsonify(value):
    """Recursively convert a value into JSON-safe primitives.

    Enums → their ``.value``; datetimes → ISO-8601 strings; dataclasses,
    dicts, and lists are walked so nested timestamps/enums are converted too.
    """
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        return value.isoformat()
    if is_dataclass(value):
        return _jsonify(asdict(value))
    if isinstance(value, dict):
        return {k: _jsonify(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonify(v) for v in value]
    return value


def _serialize(obj) -> dict:
    """Convert a domain dataclass to a fully JSON-safe canonical dict.

    Purely canonical (#233 Slice C1b): a direct facade call returns registration
    and game dicts WITH the competition ``league_id``. The v1 HTTP boundary drops
    it via ``v1_setup_adapter.registration_to_v1`` / ``game_to_v1`` so the legacy
    contract is unchanged, while non-v1 consumers see the canonical field."""
    return _jsonify(obj)


def _group(rows, attr):
    """Index dataclass rows by a foreign-key attribute → {key: [rows]}.

    Preserves input order within each bucket. Used to build the nested setup
    hierarchy (#166) without an O(n²) re-scan per parent node.
    """
    out = {}
    for r in rows:
        out.setdefault(getattr(r, attr), []).append(r)
    return out


def _parse_enum(enum_cls, value, field_name: str):
    """Parse a client-supplied enum string, raising a structured error."""
    try:
        return enum_cls(value)
    except ValueError:
        allowed = ", ".join(e.value for e in enum_cls)
        raise ValidationError(
            f"Invalid {field_name}: {value!r}. Allowed values: {allowed}."
        )


def _parse_dt(value, field_name: str):
    """Parse an optional ISO-8601 *UTC* timestamp into a timezone-aware datetime."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(value)
        except (ValueError, TypeError):
            raise ValidationError(
                f"Invalid {field_name}: {value!r}. Expected an ISO-8601 timestamp."
            )
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValidationError(
            f"Invalid {field_name}: expected a timezone-aware ISO-8601 UTC timestamp."
        )
    return parsed.astimezone(timezone.utc)


def catch(fn: Callable):
    """Wrap a facade method so domain errors become structured dicts."""

    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except DomainError as exc:
            return exc.to_dict()

    return wrapper


class ApiService:
    def __init__(self, store: Optional[InMemoryStore] = None,
                 email_transport=None, push_transport=None):
        self.store = store or InMemoryStore()
        self.roster = RosterService(self.store)
        self.setup = SetupService(self.store)
        # Email/push delivery use the configured transports (#63/#64); both
        # default to dry-run so nothing is ever sent for real unless
        # explicitly configured.
        self.delivery = DeliveryWorker(self.store, self.roster.clock,
                                       email_transport=email_transport,
                                       push_transport=push_transport)
        # Opt-in worker loop (#79): disabled by default; the server enables it
        # from env at boot. Always available for run-once drains and status.
        self.delivery_loop = DeliveryLoop(self.delivery)
        self.accounts = AccountService(self.store, self.roster.clock)
        self.guardians = GuardianService(self.store, self.roster.clock)

    # -- games -------------------------------------------------------------
    @catch
    def get_game(self, game_id: str) -> dict:
        game = self.roster._require_game(game_id)
        return _serialize(game)

    @catch
    def get_roster(self, game_id: str) -> List[dict]:
        self.roster._require_game(game_id)
        return [_serialize(e) for e in self.store.roster_for_game(game_id)]

    @catch
    def select_roster(self, game_id: str, player_ids: List[str],
                      actor_id: Optional[str] = None) -> List[dict]:
        entries = self.roster.select_roster(game_id, player_ids, actor_id)
        return [_serialize(e) for e in entries]

    @catch
    def remove_player(self, game_id: str, player_id: str,
                      actor_id: Optional[str] = None) -> dict:
        return _serialize(self.roster.remove_player(game_id, player_id, actor_id))

    @catch
    def copy_previous_roster(self, game_id: str, team_id: Optional[str] = None,
                             actor_id: Optional[str] = None) -> dict:
        return self.roster.copy_previous_roster(game_id, team_id, actor_id)

    @catch
    def set_roster_status(self, game_id: str, player_id: str, status: str,
                          actor_id: Optional[str] = None) -> dict:
        entry = self.roster.set_roster_entry_status(
            game_id, player_id, _parse_enum(RosterEntryStatus, status, "status"),
            actor_id,
        )
        return _serialize(entry)

    # -- availability ------------------------------------------------------
    @catch
    def get_availability(self, game_id: str) -> List[dict]:
        self.roster._require_game(game_id)
        return [_serialize(a) for a in self.store.availability_for_game(game_id)]

    @catch
    def get_availability_summary(self, game_id: str, team_id: str) -> dict:
        """Per-player availability for a team in a game (#89), bucketed into
        available / unavailable / maybe / no_response, with counts. Private
        (player names) — callers are gated by the same #73 access check."""
        game = self.roster._require_game(game_id)
        if team_id not in (game.home_team_id, game.away_team_id):
            raise ValidationError("That team is not playing in this game.")
        avail = {a.player_id: a
                 for a in self.store.availability_for_game(game_id)}
        counts = {"available": 0, "unavailable": 0, "maybe": 0, "no_response": 0}
        players = []
        for p in sorted(self.store.players_for_team(team_id), key=lambda x: x.name):
            a = avail.get(p.id)
            status = a.availability_status.value if a else "no_response"
            if status == "pending":  # never-responded reads as no_response
                status = "no_response"
            counts[status if status in counts else "no_response"] += 1
            players.append({"player_id": p.id, "name": p.name, "status": status})
        return {"game_id": game_id, "team_id": team_id,
                "counts": counts, "players": players}

    @catch
    def remind_unresponded(self, game_id: str, team_id: str,
                           actor_id: Optional[str] = None) -> dict:
        """Nudge the players who haven't set availability (#89): emit one
        player-targeted AVAILABILITY_REMINDER per no-response player, so the
        reminder actually reaches them (delivery honors each player's channel
        preferences, #81). Returns the number of players reminded — a no-op
        (emitting nothing) when everyone has already responded."""
        summary = self.get_availability_summary(game_id, team_id)
        if isinstance(summary, dict) and summary.get("error"):
            return summary
        unresponded = [p for p in summary["players"]
                       if p["status"] == "no_response"]
        for p in unresponded:
            _push_notification(
                self.store, self.roster.clock,
                NotificationKind.AVAILABILITY_REMINDER,
                NotificationAudience.PLAYER, "Availability reminder",
                "Please confirm your availability for this game.",
                audience_ref=p["player_id"], game_id=game_id)
        return {"reminded": len(unresponded)}

    @catch
    def set_availability(self, game_id: str, player_id: str,
                         availability_status: str, response_source: str = "player",
                         actor_id: Optional[str] = None) -> dict:
        av = self.roster.set_availability(
            game_id, player_id,
            _parse_enum(AvailabilityStatus, availability_status,
                        "availability_status"),
            response_source, actor_id,
        )
        return _serialize(av)

    # -- substitutes -------------------------------------------------------
    @catch
    def get_substitutes(self, game_id: str) -> List[dict]:
        self.roster._require_game(game_id)
        return [_serialize(s) for s in self.store.substitutes_for_game(game_id)]

    @catch
    def enroll_substitute(self, game_id: str, player_id: str,
                          actor_id: Optional[str] = None) -> dict:
        return _serialize(self.roster.enroll_substitute(game_id, player_id, actor_id))

    @catch
    def withdraw_substitute(self, game_id: str, player_id: str,
                            actor_id: Optional[str] = None) -> dict:
        return _serialize(self.roster.withdraw_substitute(game_id, player_id, actor_id))

    @catch
    def offer_substitute(self, game_id: str, player_id: str,
                         actor_id: Optional[str] = None,
                         expires_at: Optional[str] = None) -> dict:
        return _serialize(self.roster.offer_substitute(
            game_id, player_id, actor_id,
            offer_expires_at=_parse_dt(expires_at, "expires_at"),
        ))

    @catch
    def accept_substitute(self, game_id: str, player_id: str,
                          actor_id: Optional[str] = None) -> dict:
        return _serialize(self.roster.accept_substitute(game_id, player_id, actor_id))

    @catch
    def decline_substitute(self, game_id: str, player_id: str,
                           actor_id: Optional[str] = None) -> dict:
        return _serialize(self.roster.decline_substitute(game_id, player_id, actor_id))

    @catch
    def add_substitute_to_roster(self, game_id: str, player_id: str,
                                 actor_id: Optional[str] = None) -> dict:
        return _serialize(
            self.roster.add_substitute_to_roster(game_id, player_id, actor_id)
        )

    # -- roster status -----------------------------------------------------
    @catch
    def get_roster_status(self, game_id: str) -> dict:
        return self.roster.compute_roster_status(game_id).to_dict()

    @catch
    def auto_build_roster(self, game_id: str, team_id: Optional[str] = None,
                          actor_id: Optional[str] = None) -> dict:
        """Demo helper: select + confirm a full roster for one side.

        Picks the team's goalies and skaters up to the game's targets so a
        newly-scheduled game becomes immediately playable by the roster flow.
        ``team_id`` defaults to the home side (#25); a team not playing in the
        game is rejected. Raises if the team has no players (empty state).
        """
        game = self.roster._require_game(game_id)
        team_id = team_id or game.home_team_id
        if team_id not in (game.home_team_id, game.away_team_id):
            raise ValidationError("That team is not playing in this game.")
        players = self.store.players_for_team(team_id)
        if not players:
            raise ValidationError(
                "Team has no players yet. Add or import players first."
            )
        goalies = [p for p in players if p.slot_type == SlotType.GOALIE]
        skaters = [p for p in players if p.slot_type == SlotType.SKATER]
        selected = ([g.id for g in goalies[:game.target_goalies]]
                    + [s.id for s in skaters[:game.target_skaters]])
        self.roster.select_roster(game_id, selected, actor_id)
        for pid in selected:
            self.roster.set_availability(game_id, pid, AvailabilityStatus.AVAILABLE)
        status = self.roster.compute_roster_status(game_id, team_id).to_dict()
        # Coach-friendly classification of a short roster.
        status["missing_goalies"] = status["open_goalie_slots"]
        status["missing_skaters"] = status["open_skater_slots"]
        status["short_roster"] = (status["open_goalie_slots"] > 0
                                  or status["open_skater_slots"] > 0)
        return status

    @catch
    def publish_game(self, game_id: str, actor_id: Optional[str] = None) -> dict:
        return _serialize(self.setup.publish_game(game_id, True, actor_id))

    @catch
    def move_game(self, game_id: str, ice_slot_id: str, reason: str = "",
                  actor_id: Optional[str] = None) -> dict:
        game = self.setup.move_game(game_id, ice_slot_id, reason, actor_id)
        # Surface the move's side effects so the calendar's conflict side panel
        # can explain *consequences* (a published fixture reverted to draft, a
        # locked roster reopened) — the audit log is the authoritative record.
        moved = next(
            (a.detail for a in reversed(self.store.all_setup_audit())
             if a.action == "game_moved" and a.entity_id == game.id),
            {},
        )
        return {
            **_serialize(game),
            "moved": {
                "old_slot_id": moved.get("old_slot_id"),
                "new_slot_id": moved.get("new_slot_id"),
                "unpublished": bool(moved.get("unpublished")),
                "roster_unlocked": bool(moved.get("roster_unlocked")),
            },
        }

    # -- reschedule request / approval workflow (#29) -----------------------
    @catch
    def request_reschedule(self, game_id: str, team_id: str, reason: str,
                           actor_id: Optional[str] = None) -> dict:
        return _serialize(self.setup.request_reschedule(
            game_id, team_id, reason, actor_id=actor_id))

    @catch
    def respond_to_reschedule(self, request_id: str, accept: bool,
                              actor_id: Optional[str] = None) -> dict:
        return _serialize(self.setup.respond_to_reschedule(
            request_id, accept, actor_id=actor_id))

    @catch
    def decide_reschedule(self, request_id: str, approve: bool,
                          new_ice_slot_id: Optional[str] = None,
                          note: Optional[str] = None,
                          actor_id: Optional[str] = None) -> dict:
        return _serialize(self.setup.decide_reschedule(
            request_id, approve, new_ice_slot_id=new_ice_slot_id,
            note=note, actor_id=actor_id))

    @catch
    def list_reschedule_requests(self, game_id: Optional[str] = None) -> List[dict]:
        return [_serialize(r) for r in self.setup.list_reschedule_requests(game_id)]

    # -- screen view-model -------------------------------------------------
    @catch
    def get_board(self, game_id: str) -> dict:
        """Everything the Game Detail screen needs in one call.

        Groups every team player into selected / substitute / available so
        the iPhone UI can render Coach and Player views without extra round
        trips. This is a UI convenience over the contract endpoints; it does
        not introduce new domain rules.
        """
        game = self.roster._require_game(game_id)
        status = self.roster.compute_roster_status(game_id).to_dict()

        rows = self._lineup_rows(game_id, game.home_team_id)

        notifications = [
            {"type": n.type.value, "audience": n.audience, "message": n.message,
             "at": n.at.isoformat(), "subject_player_id": n.subject_player_id}
            for n in self.store.notifications_for_game(game_id)
        ]
        audit = [
            {"action": a.action.value, "actor_id": a.actor_id,
             "subject_player_id": a.subject_player_id, "at": a.at.isoformat(),
             "detail": a.detail}
            for a in self.store.audit_for_game(game_id)
        ]
        return {
            "game": _serialize(game),
            "status": status,
            "players": rows,
            "notifications": notifications,
            "audit": audit,
            "audit_count": len(audit),
        }

    def _lineup_rows(self, game_id: str, team_id: str) -> list:
        """Group a team's players into selected / substitute / available."""
        roster = {e.player_id: e for e in self.store.roster_for_game(game_id)}
        avail = {a.player_id: a for a in self.store.availability_for_game(game_id)}
        subs = {s.player_id: s for s in self.store.substitutes_for_game(game_id)}
        rows = []
        for p in self.store.players_for_team(team_id):
            entry = roster.get(p.id)
            a = avail.get(p.id)
            s = subs.get(p.id)
            backed_out = entry is not None and not entry.status.occupies_slot
            active_sub = s is not None and s.status in (
                SubstituteStatus.ENROLLED, SubstituteStatus.OFFERED
            )
            if entry is not None:
                group = "selected"
            elif active_sub:
                group = "substitute"
            else:
                group = "available"
            rows.append({
                "id": p.id,
                "name": p.name,
                "position": p.position.value,
                "slot_type": p.slot_type.value,
                "jersey_number": p.jersey_number,
                "group": group,
                "roster_status": entry.status.value if entry else None,
                "backed_out": backed_out,
                "availability": a.availability_status.value if a else "pending",
                "sub_status": s.status.value if s else None,
            })
        return rows

    @catch
    def get_lineups(self, game_id: str) -> dict:
        """Both sides' lineups + independent status for a game (#25).

        Home and away rosters are managed separately; this returns each side's
        team, roster status, and player groups in one call for the roster UI.
        """
        game = self.roster._require_game(game_id)

        def side(team_id):
            team = self.store.get_team(team_id)
            return {
                "team_id": team_id,
                "team_name": team.name if team else team_id,
                "status": self.roster.compute_roster_status(game_id, team_id).to_dict(),
                "players": self._lineup_rows(game_id, team_id),
            }

        result = self.store.result_for_game(game_id)
        return {
            "game": _serialize(game),
            "home": side(game.home_team_id),
            "away": side(game.away_team_id),
            "officials": self._official_rows(game_id),
            "result": _serialize(result) if result is not None else None,
        }

    def _official_rows(self, game_id: str) -> list:
        """Assigned officials for a game, with names, for the game sheet (#30)."""
        rows = []
        for a in self.store.assignments_for_game(game_id):
            off = self.store.get_official(a.official_id)
            rows.append({
                "assignment_id": a.id,
                "official_id": a.official_id,
                "official_name": off.name if off else a.official_id,
                "role": a.role.value,
                "status": a.status.value,
            })
        return rows

    # -- officials (#30) ---------------------------------------------------
    @catch
    def create_official(self, name: str, home_club_id: Optional[str] = None,
                        actor_id: Optional[str] = None) -> dict:
        return _serialize(self.setup.create_official(name, home_club_id, actor_id))

    @catch
    def get_officials(self) -> List[dict]:
        return [_serialize(o) for o in self.store.all_officials()]

    @catch
    def get_officials_for_game(self, game_id: str) -> List[dict]:
        self.roster._require_game(game_id)
        return self._official_rows(game_id)

    # -- notifications feed (#32) ------------------------------------------
    def _notif_visible(self, n, role, scope) -> bool:
        """Is a feed notification visible to this signed-in role/scope?"""
        if role == "league_admin":
            return True  # demo god view — sees the whole feed
        aud = n.audience.value
        if aud == "public":
            return True
        if aud == "scheduler":
            return role in ("league_admin", "arena_manager")
        if aud == "official":
            oid = scope.get("official_id")
            return oid is not None and n.audience_ref == oid
        if aud == "coach":
            return role == "coach" and (
                n.audience_ref is None or n.audience_ref == scope.get("team_id"))
        if aud == "player":
            pid = scope.get("player_id")
            return pid is not None and n.audience_ref == pid
        return False

    @staticmethod
    def _actor_key(role: str, scope: dict, user_id: Optional[str] = None) -> str:
        """A stable per-actor identity for read state (#57/#69).

        A real signed-in account (``user_id`` set — always true once #68
        production mode is in effect, since it has no other way to
        authenticate) gets its own bucket: two different accounts with the
        same role/scope — e.g. two officials, or a demo persona logged in
        twice — never share read state.

        Without a backing account (the X-Demo-Role/headerless demo-mode
        fallback, which has no identity at all) we fall back to the coarser
        role/scope-derived key from #57: officials by official id, coaches by
        team, players by player id, everyone else by role. The role guard
        there still matters — a player session carries both ``team_id`` and
        ``player_id``, and without it a player and their coach would share
        the team bucket.
        """
        if user_id:
            return "user:" + user_id
        scope = scope or {}
        if role == "official" and scope.get("official_id"):
            return "official:" + scope["official_id"]
        if role == "coach" and scope.get("team_id"):
            return "coach-team:" + scope["team_id"]
        if role == "player" and scope.get("player_id"):
            return "player:" + scope["player_id"]
        return "role:" + role

    def _recipient_id(self, notification_id: str, actor_key: str) -> str:
        return notification_id + "::" + actor_key

    @staticmethod
    def _notif_row(n, read: bool) -> dict:
        return {"id": n.id, "kind": n.kind.value, "audience": n.audience.value,
                "title": n.title, "message": n.message, "at": n.at.isoformat(),
                "read": read, "game_id": n.game_id,
                "assignment_id": n.assignment_id}

    @catch
    def get_notifications(self, role: str, scope: dict,
                          user_id: Optional[str] = None) -> dict:
        scope = scope or {}
        actor_key = self._actor_key(role, scope, user_id)
        read_ids = {r.notification_id
                    for r in self.store.recipients_for_actor(actor_key)}
        items = [n for n in self.store.all_notifications_feed()
                 if self._notif_visible(n, role, scope)]
        items.sort(key=lambda n: n.at, reverse=True)
        rows = [self._notif_row(n, n.id in read_ids) for n in items]
        return {"notifications": rows,
                "unread": sum(1 for r in rows if not r["read"])}

    def _mark_read(self, n, actor_key: str) -> bool:
        """Record that ``actor_key`` has read ``n``; True if newly marked."""
        rid = self._recipient_id(n.id, actor_key)
        if self.store.get_notification_recipient(rid) is not None:
            return False
        self.store.save_notification_recipient(NotificationRecipient(
            id=rid, notification_id=n.id, actor_key=actor_key,
            read_at=self.roster.clock()))
        return True

    @catch
    def mark_notification_read(self, notification_id: str, role: str,
                               scope: dict,
                               user_id: Optional[str] = None) -> dict:
        scope = scope or {}
        n = self.store.get_notification_feed(notification_id)
        if n is None:
            raise NotFoundError("Notification not found.")
        if not self._notif_visible(n, role, scope):
            raise NotAuthorizedError("You cannot mark this notification read.")
        self._mark_read(n, self._actor_key(role, scope, user_id))
        return self._notif_row(n, True)

    @catch
    def mark_all_notifications_read(self, role: str, scope: dict,
                                    user_id: Optional[str] = None) -> dict:
        scope = scope or {}
        actor_key = self._actor_key(role, scope, user_id)
        count = 0
        for n in self.store.all_notifications_feed():
            if self._notif_visible(n, role, scope) and self._mark_read(n, actor_key):
                count += 1
        return {"marked": count}

    # -- notification delivery queue (#58) ---------------------------------
    @staticmethod
    def _is_placeholder_destination(channel, destination) -> bool:
        dest = destination or ""
        if channel == NotificationChannel.PUSH:
            return dest.startswith("push-token:")
        return dest.endswith(".invalid")

    @staticmethod
    def _iso(dt):
        return dt.isoformat() if dt else None

    @staticmethod
    def _delivery_row(d) -> dict:
        return {"id": d.id, "notification_id": d.notification_id,
                "channel": d.channel.value, "status": d.status.value,
                "attempts": d.attempts, "last_error": d.last_error,
                "sent_at": ApiService._iso(d.sent_at),
                "last_attempt_at": ApiService._iso(d.last_attempt_at),
                "next_attempt_at": ApiService._iso(d.next_attempt_at),
                "dead_lettered_at": ApiService._iso(d.dead_lettered_at),
                "recipient_ref": d.recipient_ref, "destination": d.destination,
                "placeholder": ApiService._is_placeholder_destination(
                    d.channel, d.destination)}

    @catch
    def process_notification_deliveries(self) -> dict:
        """Drain the pending delivery queue through the mock sender."""
        return self.delivery.process_pending()

    @catch
    def retry_notification_delivery(self, delivery_id: str) -> dict:
        """Requeue a failed/dead-lettered delivery for another attempt (#80).

        Resets the attempt budget and clears the dead-letter/error state so the
        worker will pick it up again. A sent delivery is not requeued (nothing
        to retry); an ignored one is — the operator explicitly asked for it.
        """
        d = self.store.get_notification_delivery(delivery_id)
        if d is None:
            raise NotFoundError("Delivery not found.")
        if d.status == DeliveryStatus.SENT:
            raise ValidationError("A delivered notification has nothing to retry.")
        d.status = DeliveryStatus.PENDING
        d.attempts = 0
        d.last_error = None
        d.dead_lettered_at = None
        d.next_attempt_at = self.roster.clock()
        self.store.save_notification_delivery(d)
        return self._delivery_row(d)

    @catch
    def ignore_notification_delivery(self, delivery_id: str) -> dict:
        """Mark a delivery as ignored so the worker never retries it (#80)."""
        d = self.store.get_notification_delivery(delivery_id)
        if d is None:
            raise NotFoundError("Delivery not found.")
        if d.status == DeliveryStatus.SENT:
            # A completed delivery is history; rewriting it to "won't deliver"
            # would corrupt the record. Mirror retry's sent-row guard.
            raise ValidationError("A delivered notification cannot be ignored.")
        d.status = DeliveryStatus.IGNORED
        d.next_attempt_at = None
        self.store.save_notification_delivery(d)
        return self._delivery_row(d)

    @catch
    def get_delivery_overview(self) -> dict:
        """Delivery-queue counts by status and channel, for observability."""
        rows = self.store.all_notification_deliveries()
        by_status: dict = {}
        by_channel: dict = {}
        for d in rows:
            by_status[d.status.value] = by_status.get(d.status.value, 0) + 1
            by_channel[d.channel.value] = by_channel.get(d.channel.value, 0) + 1
        return {"total": len(rows), "by_status": by_status,
                "by_channel": by_channel,
                "email_mode": self.delivery.email_transport.mode,
                "email_sender": getattr(self.delivery.email_transport, "sender", None),
                "push_mode": self.delivery.push_transport.mode,
                "push_provider": getattr(self.delivery.push_transport, "provider", None),
                "worker": self.delivery_loop.status(),
                "deliveries": [self._delivery_row(d) for d in rows]}

    @catch
    def runtime_status(self) -> dict:
        """Non-sensitive deployment posture for the UI status chips (#72):
        which store backs the app and whether email/push are live or dry-run.
        No accounts, data, or secrets — safe to expose without auth.
        """
        return {
            "store": getattr(self.store, "backend", "memory"),
            "email_mode": self.delivery.email_transport.mode,
            "push_mode": self.delivery.push_transport.mode,
        }

    # -- operational health / readiness (#90) ------------------------------
    def _active_admin_count(self) -> int:
        return sum(1 for a in self.accounts.list_accounts()
                   if a.role == Role.LEAGUE_ADMIN and a.active)

    def get_health(self) -> dict:
        """Liveness + dependency snapshot (#90). Public and non-sensitive: no
        accounts, secrets, connection strings, or env values — only posture."""
        return {
            "status": "ok",
            "store": getattr(self.store, "backend", "memory"),
            "database_reachable": self.store.db_reachable(),
            "migrations": self.store.migration_status(),
            "delivery": {
                "email_mode": self.delivery.email_transport.mode,
                "push_mode": self.delivery.push_transport.mode,
                "worker": self.delivery_loop.status(),
            },
        }

    def get_readiness(self, app_mode: str, cookie_hardened: bool) -> dict:
        """Deployment readiness checks (#90). In production, requires at least
        one active admin, a reachable DB, current migrations, cookie
        hardening, and a durable store. Non-sensitive: booleans + counts
        only."""
        production = (app_mode == "production")
        mig = self.store.migration_status()
        admins = self._active_admin_count()
        # InMemoryStore.db_reachable()/migration_status() are both trivially
        # always-true (#143) — a production deployment with a missing or
        # typo'd DATABASE_URL would otherwise report ready:true while
        # silently running on storage that resets on every restart. A
        # SqlStore whose DATABASE_URL resolved to SQLite ":memory:" (or an
        # empty path — sqlite3's temp-file mode) is exactly as ephemeral,
        # so being a SqlStore *instance* isn't sufficient on its own
        # (review finding) — also exclude that via is_memory_backed.
        persistent = (not isinstance(self.store, InMemoryStore)
                      and not getattr(self.store, "is_memory_backed", False))
        checks = [
            {"name": "database_reachable", "ok": self.store.db_reachable(),
             "detail": f"store={getattr(self.store, 'backend', 'memory')}"},
            {"name": "migrations_current", "ok": mig["current"],
             "detail": f"{len(mig['applied'])}/{len(mig['expected'])} applied"},
            {"name": "active_admin",
             "ok": (admins > 0) if production else True,
             "detail": f"{admins} active league admin(s)"},
            {"name": "cookie_hardening",
             "ok": cookie_hardened if production else True,
             "detail": "Secure cookies" if cookie_hardened else "not enforced"},
            {"name": "persistent_store",
             "ok": persistent if production else True,
             "detail": (f"store={getattr(self.store, 'backend', 'memory')}"
                        if persistent else "in-memory or ephemeral (no durable DATABASE_URL)")},
        ]
        return {"ready": all(c["ok"] for c in checks),
                "app_mode": app_mode, "checks": checks}

    # -- guided onboarding status (#174 PR C) ------------------------------
    def get_onboarding_status(self, app_mode: str = "demo") -> dict:
        """Server-derived onboarding progress for the guided Setup wizard (#174).

        Reports where a fresh client installation stands on the path to
        scheduling its first game, computed entirely from *persisted* domain
        records (never client-supplied hints) plus the #143 deployment checks
        and the #173 owner/league/venue rules. The shape is::

            {complete, ready_to_schedule, steps[], blocking[], warnings[]}

        ``steps`` is the ordered milestone list the wizard renders, each
        ``{key, label, status: done|todo, blocking, detail}``. ``blocking`` is
        the flat list of hard gaps that keep the installation from scheduling —
        each ``{code, message}`` is individually actionable, and downstream
        gaps stay suppressed until their prerequisite exists so the operator
        always sees the *next* real step, not a wall of consequences.
        ``warnings`` are soft gaps (no players/officials yet, orphaned records)
        that don't block scheduling but leave onboarding incomplete.

        ``ready_to_schedule`` is true when ``blocking`` is empty — every hard
        requirement for a first game is met. ``complete`` additionally requires
        ``warnings`` to be empty, i.e. the installation is fully populated.

        Privacy: this returns counts and structural names only. Player names
        are PII and are never included (players are counted, never named);
        account usernames, secrets, and the setup code never appear. Gated to a
        League Admin at the HTTP boundary.
        """
        production = (app_mode == "production")

        admins = self._active_admin_count()
        mig = self.store.migration_status()
        persistent = (not isinstance(self.store, InMemoryStore)
                      and not getattr(self.store, "is_memory_backed", False))

        orgs = self.store.all_organizations()
        leagues = self.store.all_programs()
        venues = self.store.all_venues()
        rinks = self.store.all_rinks()
        seasons = self.store.all_seasons()
        divisions = self.store.all_divisions()
        teams = self.store.all_teams()
        slots = self.store.all_ice_slots()
        players = self.store.all_players()
        officials = self.store.all_officials()

        org_ids = {o.id for o in orgs}
        league_ids = {lg.id for lg in leagues}
        season_ids = {s.id for s in seasons}
        division_ids = {d.id for d in divisions}
        team_ids = {t.id for t in teams}
        league_owner = {lg.id: (lg.operator_organization_id or None) for lg in leagues}

        # #173: a venue truly belongs to a league only when its league_id points
        # at a real league AND the venue's own owner agrees with that league's
        # owner. A mismatch is legacy-data-only (create/assign enforce
        # agreement) but must be reconciled before the venue's ice is usable.
        venues_in_league = [v for v in venues if v.league_id in league_ids]
        venue_mismatches = [
            v for v in venues_in_league
            if (v.organization_id or None) != league_owner.get(v.league_id)]
        mismatch_ids = {v.id for v in venue_mismatches}
        sound_league_venue_ids = {
            v.id for v in venues_in_league if v.id not in mismatch_ids}
        # Only ice on a rink under a soundly-assigned league venue is
        # schedulable (#173 isolation), and only an AVAILABLE GAME slot can
        # hold a first game.
        schedulable_rink_ids = {
            r.id for r in rinks if r.venue_id in sound_league_venue_ids}
        available_game_slots = [
            s for s in slots
            if s.rink_id in schedulable_rink_ids
            and s.slot_type == IceSlotType.GAME
            and s.status == IceSlotStatus.AVAILABLE]

        # Leagues that exist but aren't tied to a real organization (#173).
        leagues_without_org = [
            lg for lg in leagues
            if not lg.operator_organization_id
            or lg.operator_organization_id not in org_ids]

        # Dangling required parents — a record whose mandatory parent link is
        # missing or points at a deleted row. These are hard gaps: a season on a
        # deleted league, a division on a deleted season, or a team on a deleted
        # division can never be scheduled.
        seasons_dangling = [s for s in seasons if s.program_id not in league_ids]
        divisions_dangling = [d for d in divisions
                              if d.season_id not in season_ids]
        # A Team belongs permanently to a Program via program_id (#180/#233); a
        # team with no valid program is the invalid-legacy-data case, not a
        # missing-division one.
        teams_dangling = [t for t in teams
                          if not t.program_id or t.program_id not in league_ids]

        blocking = []
        warnings = []
        steps = []

        def step(key, label, done, *, blocking_step=True, detail=""):
            steps.append({"key": key, "label": label,
                          "status": "done" if done else "todo",
                          "blocking": blocking_step and not done,
                          "detail": detail})

        def block(done, code, message):
            if not done:
                blocking.append({"code": code, "message": message})

        # 1. Deployment foundation — an admin to operate, durable storage in
        #    production, and current migrations.
        step("league_admin", "Create a League Admin account", admins > 0,
             detail=f"{admins} active league admin(s)")
        block(admins > 0, "no_active_admin",
              "No active League Admin account exists to operate the installation.")

        storage_ok = persistent or not production
        step("durable_storage", "Run on durable storage", storage_ok,
             blocking_step=production,
             detail=("durable store" if storage_ok
                     else "in-memory or ephemeral (no durable DATABASE_URL)"))
        if production:
            block(storage_ok, "non_durable_store",
                  "Production is running on non-durable storage; data would be "
                  "lost on restart.")

        step("migrations", "Apply database migrations", mig["current"],
             detail=f"{len(mig['applied'])}/{len(mig['expected'])} applied")
        block(mig["current"], "migrations_stale",
              "Database migrations are not up to date.")

        # 2. Facility + program chain: organization → program → venue → rink → ice.
        has_org = len(orgs) > 0
        step("organization", "Add a facility organization", has_org,
             detail=f"{len(orgs)} organization(s)")
        block(has_org, "no_organization",
              "No facility organization has been created yet.")

        # Display noun is "program" (#233): the internal `league` entity is the
        # umbrella Program. Keys/codes stay frozen; only the shown text changes.
        has_league = len(leagues) > 0
        step("league", "Create a program", has_league,
             detail=f"{len(leagues)} program(s)")
        block(has_league, "no_league", "No program has been created yet.")

        # Program-operator link only becomes an actionable gap once one exists.
        ownership_ok = has_league and not leagues_without_org
        step("league_ownership", "Tie every program to an operating organization",
             ownership_ok,
             detail=(f"{len(leagues_without_org)} program(s) without an "
                     "operating organization" if leagues_without_org
                     else "all programs have an operating organization"))
        if has_league:
            block(not leagues_without_org, "league_without_organization",
                  f"{len(leagues_without_org)} program(s) are not tied to an "
                  "operating organization.")

        # A venue is only schedulable once it's soundly linked to a program; gate
        # the gap on a program existing so we don't ask for a venue before its
        # parent. The venue→program link is a temporary v1 compatibility relation
        # (Season-to-Venue access replaces it in Slice E); venues stay org-owned.
        venue_ok = bool(sound_league_venue_ids)
        step("venue", "Link a venue to a program (temporary v1)", venue_ok,
             detail=(f"{len(sound_league_venue_ids)} venue(s) linked to a program"
                     if venue_ok else f"{len(venues)} venue(s), "
                     f"{len(venues_in_league)} linked"))
        if has_league:
            block(bool(venues_in_league), "no_venue_assigned_to_league",
                  "No venue is linked to a program yet.")
            # Surfaced independently so an operator can reconcile the exact venues
            # whose facility owner disagrees with the program's operating org.
            block(not venue_mismatches, "venue_owner_mismatch",
                  f"{len(venue_mismatches)} venue(s) have a facility owner that "
                  "disagrees with their program's operating organization.")

        has_rink = bool(schedulable_rink_ids)
        step("rink", "Add a rink to a configured venue", has_rink,
             detail=f"{len(schedulable_rink_ids)} rink(s) at a configured venue")
        if venue_ok:
            block(has_rink, "no_rink",
                  "No rink exists at a venue linked to a program.")

        has_ice = bool(available_game_slots)
        step("ice", "Open an available game ice slot", has_ice,
             detail=f"{len(available_game_slots)} available game slot(s)")
        if has_rink:
            block(has_ice, "no_available_ice",
                  "No available game ice slot exists to schedule on.")

        # 3. Competition structure: season → division → team.
        has_season = bool(season_ids) and any(
            s.program_id in league_ids for s in seasons)
        step("season", "Create a season", has_season,
             detail=f"{len(seasons)} season(s)")
        if has_league:
            block(has_season, "no_season", "No season has been created yet.")
        if seasons_dangling:
            block(False, "seasons_without_league",
                  f"{len(seasons_dangling)} season(s) reference a program that "
                  "no longer exists.")

        has_division = any(d.season_id in season_ids for d in divisions)
        step("division", "Create a division", has_division,
             detail=f"{len(divisions)} division(s)")
        if has_season:
            block(has_division, "no_division",
                  "No division has been created yet.")
        if divisions_dangling:
            block(False, "divisions_without_season",
                  f"{len(divisions_dangling)} division(s) reference a season "
                  "that no longer exists.")

        has_team = any(t.program_id in league_ids for t in teams)
        step("team", "Add a team", has_team,
             detail=f"{len(teams)} team(s)")
        if has_division:
            block(has_team, "no_team", "No team has been added yet.")
        if teams_dangling:
            block(False, "teams_without_league",
                  f"{len(teams_dangling)} team(s) are not tied to a valid program.")

        # 4. Soft gaps — recommended but not required to schedule a first game.
        if not players:
            warnings.append({"code": "no_players",
                             "message": "No players have been added yet."})
        if not officials:
            warnings.append({"code": "no_officials",
                             "message": "No officials have been added yet."})
        orphan_players = [p for p in players if p.team_id not in team_ids]
        if orphan_players:
            warnings.append({
                "code": "players_without_team",
                "message": f"{len(orphan_players)} player(s) are not on any "
                           "team."})

        ready = not blocking
        return {
            "complete": ready and not warnings,
            "ready_to_schedule": ready,
            "steps": steps,
            "blocking": blocking,
            "warnings": warnings,
        }

    def get_onboarding_status_v2(self, app_mode: str = "demo") -> dict:
        """Canonical v2 onboarding readiness (#233 Slice C2).

        Enforces the TARGET competition model: Program → Season → **League** →
        optional Division → Team. The key difference from the FROZEN v1
        ``get_onboarding_status``: a Season needs at least one grouping **League**
        (blocker ``no_league``), and **Division is optional** — there is NO
        ``no_division`` blocker; the division step is informational only.

        Canonical vocabulary is used for the codes: the umbrella is a *Program*
        (``no_program`` — v1's ``no_league`` meant the umbrella), and ``no_league``
        here means the season-scoped grouping League that v1 has no requirement
        for. Same shape as v1 (``complete``/``ready_to_schedule``/``steps``/
        ``blocking``/``warnings``) and the same count-only privacy invariant.
        """
        production = (app_mode == "production")

        admins = self._active_admin_count()
        mig = self.store.migration_status()
        persistent = (not isinstance(self.store, InMemoryStore)
                      and not getattr(self.store, "is_memory_backed", False))

        orgs = self.store.all_organizations()
        programs = self.store.all_programs()
        venues = self.store.all_venues()
        rinks = self.store.all_rinks()
        seasons = self.store.all_seasons()
        leagues = self.store.all_leagues()  # the grouping League (was Level)
        divisions = self.store.all_divisions()
        teams = self.store.all_teams()
        slots = self.store.all_ice_slots()
        players = self.store.all_players()
        officials = self.store.all_officials()

        org_ids = {o.id for o in orgs}
        program_ids = {p.id for p in programs}
        season_ids = {s.id for s in seasons}
        team_ids = {t.id for t in teams}
        program_owner = {p.id: (p.operator_organization_id or None)
                         for p in programs}

        # Venue/ice soundness — identical rules to v1 (venue↔program is still the
        # temporary v1 relation until Slice E), just under canonical names.
        venues_in_program = [v for v in venues if v.league_id in program_ids]
        venue_mismatches = [
            v for v in venues_in_program
            if (v.organization_id or None) != program_owner.get(v.league_id)]
        mismatch_ids = {v.id for v in venue_mismatches}
        sound_program_venue_ids = {
            v.id for v in venues_in_program if v.id not in mismatch_ids}
        schedulable_rink_ids = {
            r.id for r in rinks if r.venue_id in sound_program_venue_ids}
        available_game_slots = [
            s for s in slots
            if s.rink_id in schedulable_rink_ids
            and s.slot_type == IceSlotType.GAME
            and s.status == IceSlotStatus.AVAILABLE]

        programs_without_org = [
            p for p in programs
            if not p.operator_organization_id
            or p.operator_organization_id not in org_ids]

        seasons_dangling = [s for s in seasons if s.program_id not in program_ids]
        leagues_dangling = [lg for lg in leagues if lg.season_id not in season_ids]
        teams_dangling = [t for t in teams
                          if not t.program_id or t.program_id not in program_ids]

        blocking = []
        warnings = []
        steps = []

        def step(key, label, done, *, blocking_step=True, detail=""):
            steps.append({"key": key, "label": label,
                          "status": "done" if done else "todo",
                          "blocking": blocking_step and not done,
                          "detail": detail})

        def block(done, code, message):
            if not done:
                blocking.append({"code": code, "message": message})

        # 1. Deployment foundation.
        step("league_admin", "Create a League Admin account", admins > 0,
             detail=f"{admins} active league admin(s)")
        block(admins > 0, "no_active_admin",
              "No active League Admin account exists to operate the installation.")

        storage_ok = persistent or not production
        step("durable_storage", "Run on durable storage", storage_ok,
             blocking_step=production,
             detail=("durable store" if storage_ok
                     else "in-memory or ephemeral (no durable DATABASE_URL)"))
        if production:
            block(storage_ok, "non_durable_store",
                  "Production is running on non-durable storage; data would be "
                  "lost on restart.")

        step("migrations", "Apply database migrations", mig["current"],
             detail=f"{len(mig['applied'])}/{len(mig['expected'])} applied")
        block(mig["current"], "migrations_stale",
              "Database migrations are not up to date.")

        # 2. Facility + program chain.
        has_org = len(orgs) > 0
        step("organization", "Add a facility organization", has_org,
             detail=f"{len(orgs)} organization(s)")
        block(has_org, "no_organization",
              "No facility organization has been created yet.")

        has_program = len(programs) > 0
        step("program", "Create a program", has_program,
             detail=f"{len(programs)} program(s)")
        block(has_program, "no_program", "No program has been created yet.")

        ownership_ok = has_program and not programs_without_org
        step("program_ownership",
             "Tie every program to an operating organization", ownership_ok,
             detail=(f"{len(programs_without_org)} program(s) without an "
                     "operating organization" if programs_without_org
                     else "all programs have an operating organization"))
        if has_program:
            block(not programs_without_org, "program_without_organization",
                  f"{len(programs_without_org)} program(s) are not tied to an "
                  "operating organization.")

        venue_ok = bool(sound_program_venue_ids)
        step("venue", "Link a venue to a program (temporary v1)", venue_ok,
             detail=(f"{len(sound_program_venue_ids)} venue(s) linked to a program"
                     if venue_ok else f"{len(venues)} venue(s), "
                     f"{len(venues_in_program)} linked"))
        if has_program:
            block(bool(venues_in_program), "no_venue_assigned_to_program",
                  "No venue is linked to a program yet.")
            block(not venue_mismatches, "venue_owner_mismatch",
                  f"{len(venue_mismatches)} venue(s) have a facility owner that "
                  "disagrees with their program's operating organization.")

        has_rink = bool(schedulable_rink_ids)
        step("rink", "Add a rink to a configured venue", has_rink,
             detail=f"{len(schedulable_rink_ids)} rink(s) at a configured venue")
        if venue_ok:
            block(has_rink, "no_rink",
                  "No rink exists at a venue linked to a program.")

        has_ice = bool(available_game_slots)
        step("ice", "Open an available game ice slot", has_ice,
             detail=f"{len(available_game_slots)} available game slot(s)")
        if has_rink:
            block(has_ice, "no_available_ice",
                  "No available game ice slot exists to schedule on.")

        # 3. Competition structure: season → LEAGUE (required PER season) →
        #    team → schedulable participation. Division is OPTIONAL in v2 — no
        #    no_division blocker.
        seasons_by_id = {s.id: s for s in seasons}
        leagues_by_id = {lg.id: lg for lg in leagues}
        divisions_by_id = {d.id: d for d in divisions}
        teams_by_id = {t.id: t for t in teams}

        # A Season is "valid" once its Program resolves.
        valid_seasons = [s for s in seasons if s.program_id in program_ids]
        has_season = bool(valid_seasons)
        step("season", "Create a season", has_season,
             detail=f"{len(seasons)} season(s)")
        if has_program:
            block(has_season, "no_season", "No season has been created yet.")
        if seasons_dangling:
            block(False, "seasons_without_program",
                  f"{len(seasons_dangling)} season(s) reference a program that "
                  "no longer exists.")

        # League is required PER valid Season: EVERY valid Season must carry at
        # least one grouping League. The gap fires if ANY such Season lacks one
        # (a single Season with a League no longer masks the others), and reports
        # exactly which Seasons are missing it.
        league_season_ids = {lg.season_id for lg in leagues}
        seasons_without_league = [s for s in valid_seasons
                                  if s.id not in league_season_ids]
        all_seasons_have_league = has_season and not seasons_without_league
        step("league", "Give every season a grouping league",
             all_seasons_have_league,
             detail=(f"{len(seasons_without_league)} season(s) without a league"
                     if seasons_without_league
                     else f"{len(leagues)} league(s)"))
        if has_season:
            missing_names = ", ".join(s.name for s in seasons_without_league)
            block(not seasons_without_league, "no_league",
                  f"{len(seasons_without_league)} season(s) have no grouping "
                  f"league: {missing_names}." if seasons_without_league
                  else "Every season has a grouping league.")
        if leagues_dangling:
            block(False, "leagues_without_season",
                  f"{len(leagues_dangling)} league(s) reference a season that "
                  "no longer exists.")

        # Division is optional (#233): show the milestone but never block on it.
        has_division = any(d.season_id in season_ids for d in divisions)
        step("division", "Create a division (optional)", has_division,
             blocking_step=False,
             detail=f"{len(divisions)} division(s)")

        has_team = any(t.program_id in program_ids for t in teams)
        step("team", "Add a team", has_team,
             detail=f"{len(teams)} team(s)")
        if all_seasons_have_league:
            block(has_team, "no_team", "No team has been added yet.")
        if teams_dangling:
            block(False, "teams_without_program",
                  f"{len(teams_dangling)} team(s) are not tied to a valid program.")

        # Participation validity (#233 Slice C2 review): a first game needs at
        # least one SCHEDULABLE active registration — its League resolves to its
        # own Season, its Team is in that Season's Program, and its optional
        # Division belongs to that League and Season. Invalid registrations
        # (cross-Season league_id, cross-Program team, or a Division outside the
        # League/Season) are reported as a blocker so they can't hide behind a
        # single good row.
        schedulable = 0
        invalid_regs = 0
        for reg in self.store.all_season_team_registrations():
            if not reg.active:
                continue
            season = seasons_by_id.get(reg.season_id)
            if season is None or season.program_id not in program_ids:
                invalid_regs += 1
                continue
            league = leagues_by_id.get(reg.league_id) if reg.league_id else None
            league_ok = league is not None and league.season_id == reg.season_id
            team = teams_by_id.get(reg.team_id)
            team_ok = team is not None and team.program_id == season.program_id
            div_ok = True
            if reg.division_id:
                division = divisions_by_id.get(reg.division_id)
                div_ok = (division is not None
                          and division.season_id == reg.season_id
                          and division.league_id == reg.league_id)
            if league_ok and team_ok and div_ok:
                schedulable += 1
            else:
                invalid_regs += 1

        step("participation", "Register a team to play (league-consistent)",
             schedulable > 0,
             detail=f"{schedulable} schedulable registration(s)")
        if has_team and all_seasons_have_league:
            block(schedulable > 0, "no_participation",
                  "No league-consistent team registration exists to schedule a "
                  "game with.")
        if invalid_regs:
            block(False, "invalid_registrations",
                  f"{invalid_regs} team registration(s) are invalid: a league "
                  "not in the season, a team in another program, or a division "
                  "outside its league/season.")

        # 4. Soft gaps — recommended but not required to schedule.
        if not players:
            warnings.append({"code": "no_players",
                             "message": "No players have been added yet."})
        if not officials:
            warnings.append({"code": "no_officials",
                             "message": "No officials have been added yet."})
        orphan_players = [p for p in players if p.team_id not in team_ids]
        if orphan_players:
            warnings.append({
                "code": "players_without_team",
                "message": f"{len(orphan_players)} player(s) are not on any "
                           "team."})

        ready = not blocking
        return {
            "complete": ready and not warnings,
            "ready_to_schedule": ready,
            "steps": steps,
            "blocking": blocking,
            "warnings": warnings,
        }

    # -- contact registry (#60) --------------------------------------------
    @staticmethod
    def _contact_row(c) -> dict:
        return {"id": c.id, "recipient_ref": c.recipient_ref,
                "channel": c.channel.value, "destination": c.destination,
                "label": c.label}

    @catch
    def list_contact_destinations(self) -> dict:
        rows = [self._contact_row(c)
                for c in self.store.all_contact_destinations()]
        rows.sort(key=lambda r: (r["recipient_ref"], r["channel"]))
        return {"contacts": rows}

    @catch
    def set_contact_destination(self, recipient_ref: str, channel: str,
                                destination: str, label=None) -> dict:
        """Register (or update) the real destination for a recipient/channel."""
        if not recipient_ref:
            raise ValidationError("A recipient_ref is required.")
        try:
            ch = NotificationChannel(channel)
        except ValueError:
            raise ValidationError(f"Unknown channel '{channel}'.")
        destination = (destination or "").strip()
        if not destination:
            raise ValidationError("A destination is required.")
        if ch == NotificationChannel.EMAIL and "@" not in destination:
            raise ValidationError("An email destination must contain '@'.")
        existing = self.store.get_contact_destination(recipient_ref, ch)
        if existing is not None:
            existing.destination = destination
            existing.label = label
            self.store.save_contact_destination(existing)
            return self._contact_row(existing)
        c = ContactDestination(
            id=self.store.next_id("contact"), recipient_ref=recipient_ref,
            channel=ch, destination=destination, label=label)
        self.store.add_contact_destination(c)
        return self._contact_row(c)

    # -- notification preferences (#81) ------------------------------------
    # The delivery channels a recipient can opt out of (in-app feed is always on).
    PREF_CHANNELS = (NotificationChannel.EMAIL, NotificationChannel.PUSH)

    @catch
    def get_notification_preferences(self, recipient_ref: str) -> dict:
        """A recipient's per-channel preferences, with defaults filled in for
        any channel that has no stored row (enabled)."""
        if not recipient_ref:
            raise ValidationError("A recipient_ref is required.")
        stored = {p.channel: p
                  for p in self.store.preferences_for_recipient(recipient_ref)}
        prefs = []
        for ch in self.PREF_CHANNELS:
            p = stored.get(ch)
            prefs.append({"channel": ch.value,
                          "enabled": p.enabled if p else True,
                          "digest": p.digest if p else None})
        return {"recipient_ref": recipient_ref, "preferences": prefs}

    @catch
    def set_notification_preference(self, recipient_ref: str, channel: str,
                                    enabled: bool, digest=None,
                                    actor_id=None) -> dict:
        """Enable/disable a delivery channel for a recipient (#81)."""
        if not recipient_ref:
            raise ValidationError("A recipient_ref is required.")
        try:
            ch = NotificationChannel(channel)
        except ValueError:
            raise ValidationError(f"Unknown channel '{channel}'.")
        if ch not in self.PREF_CHANNELS:
            raise ValidationError(f"Channel '{channel}' is not configurable.")
        existing = self.store.get_notification_preference(recipient_ref, ch)
        prior_enabled = existing.enabled if existing is not None else None
        if existing is not None:
            existing.enabled = bool(enabled)
            if digest is not None:
                existing.digest = digest
            self.store.save_notification_preference(existing)
            pref = existing
        else:
            pref = NotificationPreference(
                id=self.store.next_id("notif_pref"), recipient_ref=recipient_ref,
                channel=ch, enabled=bool(enabled), digest=digest)
            self.store.save_notification_preference(pref)
        # Muting/unmuting a delivery channel is a state change that must be
        # auditable (#81): who changed which recipient's channel, and from
        # what prior value. No secret/token material is involved.
        self.setup._audit(
            "notification_preference_set", "notification_preference", pref.id,
            actor_id,
            {"recipient_ref": recipient_ref, "channel": ch.value,
             "enabled": pref.enabled, "prior_enabled": prior_enabled,
             "digest": pref.digest})
        return {"recipient_ref": recipient_ref, "channel": ch.value,
                "enabled": pref.enabled, "digest": pref.digest}

    # -- calendar feed tokens (#82) ----------------------------------------
    @staticmethod
    def _feed_token_row(t) -> dict:
        # Never include token_hash or the raw token — only lifecycle metadata.
        return {"id": t.id, "actor_type": t.actor_type, "actor_ref": t.actor_ref,
                "created_at": ApiService._iso(t.created_at),
                "revoked_at": ApiService._iso(t.revoked_at),
                "label": t.label, "revoked": t.revoked_at is not None,
                "path": f"/calendar/{t.actor_type}/{{token}}.ics",
                "created_by": t.created_by,
                "last_used_at": ApiService._iso(t.last_used_at),
                "revoked_by": t.revoked_by}

    def _feed_actor_exists(self, actor_type: str, actor_ref: str) -> bool:
        if actor_type == "team":
            return self.store.get_team(actor_ref) is not None
        if actor_type == "division":
            return self.store.get_division(actor_ref) is not None
        if actor_type == "player":
            return self.store.get_player(actor_ref) is not None
        if actor_type == "official":
            return self.store.get_official(actor_ref) is not None
        return False

    @catch
    def create_calendar_feed_token(self, actor_type: str, actor_ref: str,
                                   label=None, actor_id=None) -> dict:
        """Issue a feed token for an actor and return the raw token ONCE
        (only its hash is stored). The caller builds the subscription URL."""
        if actor_type not in ACTOR_TYPES:
            raise ValidationError(f"Unknown actor_type '{actor_type}'.")
        if not actor_ref or not isinstance(actor_ref, str):
            raise ValidationError("An actor_ref is required.")
        if label is not None and not isinstance(label, str):
            raise ValidationError("A label must be a string.")
        if not self._feed_actor_exists(actor_type, actor_ref):
            raise NotFoundError(f"{actor_type.title()} not found.")
        raw = new_feed_token()
        tok = CalendarFeedToken(
            id=self.store.next_id("calfeed"), token_hash=hash_feed_token(raw),
            actor_type=actor_type, actor_ref=actor_ref,
            created_at=self.roster.clock(), label=label,
            # A public team/division mint (#33) carries no session, hence no
            # actor_id — "anonymous" records that plainly rather than leaving
            # created_by blank, which would be ambiguous with "not tracked
            # yet" on a pre-#131 row (#131).
            created_by=actor_id or "anonymous")
        self.store.add_calendar_feed_token(tok)
        # Minting a feed token grants standing read access to an actor's
        # schedule, so it is auditable (#82). Record only lifecycle metadata —
        # never the raw token or its hash.
        self.setup._audit(
            "calendar_feed_token_created", "calendar_feed_token", tok.id,
            actor_id,
            {"actor_type": actor_type, "actor_ref": actor_ref, "label": label})
        row = self._feed_token_row(tok)
        row["token"] = raw  # returned once; not stored, not returned again
        row["url"] = f"/calendar/{actor_type}/{raw}.ics"
        return row

    @catch
    def list_calendar_feed_tokens(self, actor_type: str, actor_ref: str) -> dict:
        rows = [self._feed_token_row(t) for t in
                self.store.calendar_feed_tokens_for(actor_type, actor_ref)]
        return {"feed_tokens": rows}

    @catch
    def revoke_calendar_feed_token(self, token_id: str, actor_id=None) -> dict:
        tok = self.store.get_calendar_feed_token(token_id)
        if tok is None:
            raise NotFoundError("Feed token not found.")
        already_revoked = tok.revoked_at is not None
        if tok.revoked_at is None:
            tok.revoked_at = self.roster.clock()
            tok.revoked_by = actor_id  # (#131) — revoke always requires a session
            self.store.save_calendar_feed_token(tok)
        # Revoking a feed token cuts off that read access, so it is auditable
        # (#82). Only lifecycle metadata — no token material. A repeat revoke of
        # an already-revoked token is recorded too (idempotent no-op flagged).
        self.setup._audit(
            "calendar_feed_token_revoked", "calendar_feed_token", tok.id,
            actor_id,
            {"actor_type": tok.actor_type, "actor_ref": tok.actor_ref,
             "label": tok.label, "already_revoked": already_revoked})
        return self._feed_token_row(tok)

    def calendar_feed_ics(self, actor_type: str, raw_token: str):
        """Resolve a raw feed token and render its ICS, or None if the token is
        unknown, revoked, or its actor_type doesn't match the route (#82).

        Not @catch-wrapped: the caller returns text/calendar or a 404, not a
        JSON error envelope.
        """
        tok = self.store.get_calendar_feed_token_by_hash(
            hash_feed_token(raw_token or ""))
        if tok is None or tok.revoked_at is not None:
            return None
        if tok.actor_type != actor_type:
            return None
        # Bumped on every successful resolution (#131) so an operator can
        # tell a live subscription from an abandoned one — a calendar app
        # polls this route repeatedly with no session, so this is the only
        # "still in use" signal available.
        tok.last_used_at = self.roster.clock()
        self.store.save_calendar_feed_token(tok)
        name = f"{actor_type.title()} calendar"
        return build_ics(self.store, tok.actor_type, tok.actor_ref,
                         self.roster.clock(), calendar_name=name)

    # -- device token registry (#65) ---------------------------------------
    @staticmethod
    def _device_token_row(t) -> dict:
        return {"id": t.id, "recipient_ref": t.recipient_ref,
                "provider": t.provider, "token": t.token,
                "label": t.label, "active": t.active}

    @catch
    def list_device_tokens(self) -> dict:
        rows = [self._device_token_row(t)
                for t in self.store.all_device_tokens()]
        rows.sort(key=lambda r: (r["recipient_ref"], not r["active"], r["id"]))
        return {"device_tokens": rows}

    @catch
    def register_device_token(self, recipient_ref: str, provider: str,
                              token: str, label=None) -> dict:
        """Register (or reactivate) a real push device token for a recipient."""
        if not recipient_ref:
            raise ValidationError("A recipient_ref is required.")
        provider = (provider or "").strip()
        if not provider:
            raise ValidationError("A provider is required.")
        token = (token or "").strip()
        if not token:
            raise ValidationError("A device token is required.")
        # Reject the synthesized placeholder scheme — real tokens only (#65).
        if token.startswith("push-token:"):
            raise ValidationError(
                "That looks like a placeholder token — register a real device "
                "token from the provider.")
        existing = self.store.get_device_token_by_value(recipient_ref, token)
        if existing is not None:
            existing.provider = provider
            existing.label = label
            existing.active = True
            self.store.save_device_token(existing)
            return self._device_token_row(existing)
        t = DeviceToken(
            id=self.store.next_id("devtok"), recipient_ref=recipient_ref,
            provider=provider, token=token, label=label, active=True)
        self.store.add_device_token(t)
        return self._device_token_row(t)

    @catch
    def set_device_token_active(self, token_id: str, active: bool) -> dict:
        t = self.store.get_device_token(token_id)
        if t is None:
            raise NotFoundError("Device token not found.")
        t.active = bool(active)
        self.store.save_device_token(t)
        return self._device_token_row(t)

    # -- user accounts (#67) ------------------------------------------------
    @staticmethod
    def _account_row(a) -> dict:
        # Never include password_hash — this row is safe to send to a client.
        return {"id": a.id, "username": a.username, "role": a.role.value,
                "scope": dict(a.scope), "active": a.active,
                "created_at": a.created_at.isoformat()}

    @catch
    def create_user_account(self, username: str, password: str, role: str,
                            scope: Optional[dict] = None,
                            actor_id: Optional[str] = None) -> dict:
        account = self.accounts.create_account(
            username, password, role, scope=scope, actor_id=actor_id)
        return self._account_row(account)

    @catch
    def set_user_account_active(self, account_id: str, active: bool,
                                actor_id: Optional[str] = None) -> dict:
        account = self.accounts.set_active(account_id, active, actor_id=actor_id)
        return self._account_row(account)

    @catch
    def list_user_accounts(self) -> dict:
        return {"user_accounts":
                [self._account_row(a) for a in self.accounts.list_accounts()]}

    # -- account sessions (#78) --------------------------------------------
    @staticmethod
    def _session_row(s, now) -> dict:
        """Operator-safe view of a session. NEVER includes the raw token (which
        is not stored anyway) or the token_hash — only lifecycle metadata."""
        if s.revoked_at is not None:
            status = "revoked"
        elif s.expires_at < now:
            status = "expired"
        else:
            status = "active"
        return {"id": s.id, "issued_at": s.issued_at.isoformat(),
                "expires_at": s.expires_at.isoformat(),
                "revoked_at": s.revoked_at.isoformat() if s.revoked_at else None,
                "user_agent": s.user_agent, "status": status}

    @catch
    def list_account_sessions(self, account_id: str) -> dict:
        """List an account's sessions for a League Admin (#78), newest first.
        No token material is exposed — only id/timestamps/user_agent/status."""
        if self.store.get_user_account(account_id) is None:
            raise NotFoundError("User account not found.")
        now = self.roster.clock()
        rows = sorted(self.store.sessions_for_user(account_id),
                      key=lambda s: s.issued_at, reverse=True)
        return {"sessions": [self._session_row(s, now) for s in rows]}

    @catch
    def revoke_account_session(self, account_id: str, session_id: str,
                               actor_id: Optional[str] = None) -> dict:
        """Revoke a single session belonging to an account (#78). Idempotent:
        an already-revoked session keeps its original revoked_at.

        Every accepted call is audited — a force-logout is a security-sensitive
        admin action, so the record must show who did it, even when the target
        was already revoked. The audit detail never carries the raw token or
        its hash (neither is available here: Session only stores the hash, and
        that is deliberately excluded from the logged detail).
        """
        if self.store.get_user_account(account_id) is None:
            raise NotFoundError("User account not found.")
        sess = self.store.get_session(session_id)
        if sess is None or sess.user_id != account_id:
            raise NotFoundError("Session not found.")
        prior_status = self._session_row(sess, self.roster.clock())["status"]
        if sess.revoked_at is None:
            sess.revoked_at = self.roster.clock()
            self.store.save_session(sess)
        self.setup._audit(
            "session_revoked", "user_session", session_id, actor_id,
            {"account_id": account_id, "session_id": session_id,
             "prior_status": prior_status, "user_agent": sess.user_agent})
        return self._session_row(sess, self.roster.clock())

    def verify_login(self, username: str, password: str) -> Optional[dict]:
        """Return the account row for valid, active credentials, else None.

        Not wrapped in ``@catch``: this is a boolean-shaped check consumed
        directly by the login route, not a REST endpoint returning a
        structured error.
        """
        account = self.accounts.verify_login(username, password)
        return self._account_row(account) if account is not None else None

    @catch
    def get_official_inbox(self, official_id: str) -> dict:
        """An official's own assignments with game context, for the inbox (#55)."""
        rows = []
        for a in self.store.assignments_for_official(official_id):
            g = self.store.get_game(a.game_id)
            if g is None:
                continue
            home = self.store.get_team(g.home_team_id)
            away = self.store.get_team(g.away_team_id) if g.away_team_id else None
            rows.append({
                "assignment_id": a.id, "game_id": a.game_id,
                "role": a.role.value, "status": a.status.value,
                "home_team_name": home.name if home else g.home_team_id,
                "away_team_name": away.name if away else None,
                "start_time": g.start_time.isoformat() if g.start_time else None,
                "rink": g.rink, "venue_name": self._venue_name_for_game(g),
                "cancelled": g.cancelled,
            })
        rows.sort(key=lambda r: r["start_time"] or "")
        return {"official_id": official_id, "assignments": rows}

    # -- player home (#107) --------------------------------------------------
    # Collapses the roster-status engine's GameStatus into the four labels
    # the Player Home Page shows — the engine itself needs no changes, this
    # is presentation-layer relabeling only.
    _PLAYER_TEAM_STATUS = {
        "roster_confirmed": "full", "locked": "full", "final": "full",
        "open_slot": "short", "needs_substitute": "sub_search",
        "draft": "not_responded", "selected": "not_responded",
        "awaiting_responses": "not_responded",
    }

    @staticmethod
    def _player_attendance_status(row: Optional[dict]) -> str:
        """Collapse a lineup row's availability/backed_out fields into the
        Player Home Page's four attendance labels (#107)."""
        if row is None:
            return "not_responded"
        if row["backed_out"]:
            return "checked_out"
        avail = row["availability"]
        if avail == "available":
            return "confirmed"
        if avail == "unavailable":
            return "checked_out"
        if avail == "maybe":
            return "pending"
        return "not_responded"

    def _venue_name_for_game(self, g) -> Optional[str]:
        slot = self.store.get_ice_slot(g.ice_slot_id) if g.ice_slot_id else None
        if slot is None:
            return None
        rink = self.store.get_rink(slot.rink_id) if slot.rink_id else None
        if rink is None or not rink.venue_id:
            return None
        venue = self.store.get_venue(rink.venue_id)
        return venue.name if venue else None

    @staticmethod
    def _opponent_team_id(g, team_id):
        """The other side of a game from ``team_id``'s point of view."""
        return g.away_team_id if g.home_team_id == team_id else g.home_team_id

    @staticmethod
    def _mutable_block(game) -> Optional[str]:
        """The reason a game's roster can't be mutated (cancelled/locked), or
        None — mirrors the service's _guard_mutable so a withdraw/decline
        button isn't offered when it would dead-end (#110/#112)."""
        if game.cancelled:
            return "This game has been cancelled."
        if game.locked:
            return "The roster for this game is locked."
        return None

    def _opportunity_base_dict(self, g, player) -> dict:
        """The fields a substitute opportunity carries in both the Home list
        (#107) and the detail view (#110) — kept in one place so the two
        surfaces can't drift on the shared shape."""
        team = self.store.get_team(player.team_id) if player.team_id else None
        opp_id = self._opponent_team_id(g, player.team_id)
        opp_team = self.store.get_team(opp_id) if opp_id else None
        return {
            "game_id": g.id,
            "team_name": team.name if team else player.team_id,
            "opponent_name": opp_team.name if opp_team else None,
            "start_time": g.start_time.isoformat() if g.start_time else None,
            "venue_name": self._venue_name_for_game(g),
            "rink_name": g.rink,
            "position_needed": player.position.slot_type.value,
        }

    @catch
    def get_player_home(self, player_id: str, user_id: Optional[str] = None) -> dict:
        """The signed-in player's home screen (#107): next game, attendance
        status, team roster status, substitute opportunities, and unread
        notification count — all scoped to this player only."""
        player = self.store.get_player(player_id)
        if player is None:
            raise NotFoundError("Player not found.")
        team = self.store.get_team(player.team_id) if player.team_id else None

        next_game = self.roster.find_next_game_for_player(player_id)
        next_game_dto = None
        if next_game is not None:
            my_team_id = player.team_id
            opponent_id = self._opponent_team_id(next_game, my_team_id)
            opponent = self.store.get_team(opponent_id) if opponent_id else None
            rstatus = self.roster.compute_roster_status(next_game.id, my_team_id)
            # Only this player's own roster entry + availability are needed —
            # single-player lookups, not a full _lineup_rows pass over the team.
            entry = self.store.roster_entry_for_player(next_game.id, player_id)
            avail = self.store.availability_for_player(next_game.id, player_id)
            my_row = {
                "backed_out": entry is not None and not entry.status.occupies_slot,
                "availability": (avail.availability_status.value
                                 if avail else "pending"),
            }
            next_game_dto = {
                "game_id": next_game.id, "team_id": my_team_id,
                "team_name": team.name if team else my_team_id,
                "opponent_name": opponent.name if opponent else None,
                "start_time": next_game.start_time.isoformat()
                             if next_game.start_time else None,
                "venue_name": self._venue_name_for_game(next_game),
                "rink_name": next_game.rink,
                "attendance_status": self._player_attendance_status(my_row),
                "team_status": self._PLAYER_TEAM_STATUS.get(
                    rstatus.status.value, "not_responded"),
            }

        opportunities = [self._opportunity_base_dict(g, player)
                         for g in self.roster.list_substitute_opportunities(player_id)]
        # Slots a coach has OFFERED this player (#112): shown separately so
        # they can accept/decline — the self-enrol opportunities list excludes
        # already-offered games.
        offers = [self._opportunity_base_dict(g, player)
                  for g in self.roster.list_player_offers(player_id)]

        notif = self.get_notifications("player", {"player_id": player_id},
                                       user_id=user_id)
        unread = (notif.get("unread", 0)
                 if isinstance(notif, dict) and "error" not in notif else 0)

        return {
            "player_id": player_id, "player_name": player.name,
            "next_game": next_game_dto,
            "today_count": self.roster.count_games_today_for_player(player_id),
            "substitute_offers": offers,
            "substitute_opportunities": opportunities,
            "unread_notifications": unread,
        }

    @catch
    def get_guardian_home(self, guardian_user_id: str) -> dict:
        """A guardian's linked-junior surface (#26): for each junior this
        guardian is *verified* to act for, the same Player Home payload the
        junior would see, so the guardian can respond on their behalf. Only
        verified links are included — an unverified or absent link surfaces
        nothing. Carries no guardian PII: the shape is entirely player-scoped."""
        juniors = []
        for jid in self.guardians.verified_junior_ids(guardian_user_id):
            home = self.get_player_home(jid)
            if isinstance(home, dict) and "error" not in home:
                juniors.append(home)
        juniors.sort(key=lambda h: (h.get("player_name") or "").lower())
        return {"guardian_user_id": guardian_user_id, "juniors": juniors}

    @catch
    def create_guardian_link(self, guardian_user_id: str, player_id: str,
                             actor_id: Optional[str] = None) -> dict:
        """Operator creates an unverified guardian↔junior link (#35) — the
        first HTTP-reachable path for this; previously only deterministic
        demo seeding could create one."""
        return _serialize(self.guardians.link_guardian(
            guardian_user_id, player_id, actor_id=actor_id))

    @catch
    def verify_guardian_link(self, link_id: str, consent_method: str,
                             actor_id: Optional[str] = None) -> dict:
        """Operator verifies a guardian link, recording a real consent
        record (#35 — GDPR Art. 8). Unlike the underlying service method
        (which leaves ``consent_method`` optional for internal/seed use),
        every real operator-facing verification through this route must
        record HOW authorization was obtained."""
        consent_method = (consent_method or "").strip()
        if not consent_method:
            raise ValidationError(
                "consent_method is required to verify a guardian link "
                "(e.g. 'signed_form', 'verbal_confirmed', 'email_reply').")
        return _serialize(self.guardians.verify_link(
            link_id, actor_id=actor_id, consent_method=consent_method))

    @catch
    def list_guardian_links(self) -> List[dict]:
        return [_serialize(l) for l in self.guardians.all_links()]

    @catch
    def get_substitute_opportunity(self, player_id: str, game_id: str) -> dict:
        """Detail for one substitute opportunity, scoped to the signed-in
        player (#110): the game context, this player's current relationship to
        it (can accept / can withdraw), and a plain-language reason when they
        cannot respond."""
        player = self.store.get_player(player_id)
        if player is None:
            raise NotFoundError("Player not found.")
        game = self.store.get_game(game_id)
        # Only a player on one of the participating teams may view the
        # opportunity — don't confirm another team's game to a non-participant.
        if game is None or player.team_id not in (
                game.home_team_id, game.away_team_id):
            raise NotFoundError("Opportunity not found.")
        rstatus = self.roster.compute_roster_status(game_id, player.team_id)
        enrollment = self.store.substitute_for_player(game_id, player_id)
        status = enrollment.status if enrollment else None
        can_accept = can_withdraw = can_accept_offer = can_decline_offer = False
        blocked = None
        if status == SubstituteStatus.OFFERED:
            # A coach has offered this player the slot (#112): respond with
            # Accept/Decline, not Enroll/Withdraw. The accept-eligibility rules
            # (cancelled/locked/past/expired/slot-filled) live in one service
            # predicate so this pre-disable can't drift from accept_substitute;
            # decline only needs a mutable game.
            offer_block = self.roster.substitute_offer_block_reason(
                player_id, game_id, enrollment, rstatus)
            can_accept_offer = offer_block is None
            can_decline_offer = self._mutable_block(game) is None
            blocked = offer_block
        elif status == SubstituteStatus.ENROLLED:
            # Withdrawal routes through _guard_mutable, so only a locked or
            # cancelled game blocks it — not the enrol-eligibility reasons.
            withdraw_block = self._mutable_block(game)
            can_withdraw, blocked = withdraw_block is None, withdraw_block
        else:
            reason = self.roster.substitute_block_reason(player_id, game_id, rstatus)
            can_accept, blocked = reason is None, reason
        return {
            **self._opportunity_base_dict(game, player),
            "roster_status": rstatus.status.value,
            "team_status": self._PLAYER_TEAM_STATUS.get(
                rstatus.status.value, "not_responded"),
            "open_goalie_slots": rstatus.open_goalie_slots,
            "open_skater_slots": rstatus.open_skater_slots,
            "enrollment_status": status.value if status else None,
            "can_accept": can_accept,
            "can_withdraw": can_withdraw,
            "can_accept_offer": can_accept_offer,
            "can_decline_offer": can_decline_offer,
            "blocked_reason": blocked,
        }

    @catch
    def get_substitute_candidates(self, game_id: str,
                                  team_id: Optional[str] = None) -> dict:
        """The coach outreach queue for a game/team (#112): open slots by
        position plus the ordered substitute candidates (who can be offered
        right now). Operator-facing — gated at the route by MANAGE_ROSTER +
        team scope."""
        game = self.store.get_game(game_id)
        if game is None:
            raise NotFoundError("Game not found.")
        team_id = team_id or game.home_team_id
        rstatus = self.roster.compute_roster_status(game_id, team_id)
        return {
            "game_id": game_id, "team_id": team_id,
            "open_goalie_slots": rstatus.open_goalie_slots,
            "open_skater_slots": rstatus.open_skater_slots,
            "locked": game.locked, "cancelled": game.cancelled,
            "candidates": self.roster.list_substitute_candidates(
                game_id, team_id, rstatus),
        }

    @catch
    def get_addable_substitutes(self, game_id: str,
                                team_id: Optional[str] = None) -> dict:
        """Active same-team players a coach could add as a substitute
        candidate right now (#114) — the roster the outreach queue's own
        Enroll doesn't cover, since enroll_substitute only ever runs from the
        player's own self-service action. Operator-facing — gated at the
        route the same way get_substitute_candidates is."""
        game = self.store.get_game(game_id)
        if game is None:
            raise NotFoundError("Game not found.")
        team_id = team_id or game.home_team_id
        rstatus = self.roster.compute_roster_status(game_id, team_id)
        return {
            "game_id": game_id, "team_id": team_id,
            "locked": game.locked, "cancelled": game.cancelled,
            "addable": self.roster.list_addable_players(
                game_id, team_id, rstatus),
        }

    @catch
    def add_substitute_candidate(self, game_id: str, player_id: str,
                                 actor_id: Optional[str] = None) -> dict:
        return _serialize(self.roster.add_substitute_candidate(
            game_id, player_id, actor_id))

    @catch
    def assign_official(self, game_id: str, official_id: str, role: str,
                        actor_id: Optional[str] = None,
                        override_unavailable: bool = False) -> dict:
        a = self.setup.assign_official(
            game_id, official_id, _parse_enum(OfficialRole, role, "role"),
            actor_id, override_unavailable=override_unavailable)
        return _serialize(a)

    # -- official availability (#88) ---------------------------------------
    @staticmethod
    def _availability_row(a) -> dict:
        return {"id": a.id, "official_id": a.official_id,
                "start_time": a.start_time.isoformat(),
                "end_time": a.end_time.isoformat(),
                "status": a.status.value, "note": a.note}

    @catch
    def set_official_availability(self, official_id: str, start_time: str,
                                  end_time: str, status: str, note=None,
                                  actor_id: Optional[str] = None) -> dict:
        a = self.setup.set_official_availability(
            official_id, _parse_dt(start_time, "start_time"),
            _parse_dt(end_time, "end_time"), status, note=note, actor_id=actor_id)
        return self._availability_row(a)

    @catch
    def list_official_availability(self, official_id: str) -> dict:
        return {"official_id": official_id,
                "availability": [self._availability_row(a)
                                 for a in self.setup.official_availabilities(official_id)]}

    @catch
    def delete_official_availability(self, avail_id: str,
                                     actor_id: Optional[str] = None) -> dict:
        self.setup.delete_official_availability(avail_id, actor_id=actor_id)
        return {"deleted": avail_id}

    @catch
    def respond_assignment(self, assignment_id: str, accept: bool,
                           actor_id: Optional[str] = None) -> dict:
        return _serialize(self.setup.respond_assignment(assignment_id, accept, actor_id))

    @catch
    def unassign_official(self, assignment_id: str,
                          actor_id: Optional[str] = None) -> dict:
        return _serialize(self.setup.unassign_official(assignment_id, actor_id))

    # -- results & standings (#31) -----------------------------------------
    @catch
    def record_result(self, game_id: str, home_score, away_score,
                      actor_id: Optional[str] = None) -> dict:
        return _serialize(self.setup.record_result(
            game_id, home_score, away_score, actor_id))

    @catch
    def approve_result(self, game_id: str, actor_id: Optional[str] = None) -> dict:
        return _serialize(self.setup.approve_result(game_id, actor_id))

    @catch
    def get_result(self, game_id: str) -> dict:
        self.roster._require_game(game_id)
        r = self.store.result_for_game(game_id)
        return _serialize(r) if r is not None else {"game_id": game_id, "status": None}

    @catch
    def get_standings(self, division_id: str) -> dict:
        """Standings for a division from FINAL results only (#31).

        Points: win = 2, tie = 1, loss = 0. Ranked by points, then goal
        difference, then goals for, then name. Counts every division game
        (operator view); the public variant is filtered to published games.
        """
        return self._standings_for_division(division_id, public_only=False)

    def _standings_for_division(self, division_id: str,
                                public_only: bool = False) -> dict:
        """Compute a division's standings table.

        ``public_only`` skips unpublished games so the public standings cannot
        reveal a hidden/draft game's outcome by aggregation (#83) — the public
        schedule and game-detail routes already hide unpublished games, and the
        standings must stay consistent with them.
        """
        # The division roster comes from active SeasonTeamRegistrations (#180
        # shared guard), not the legacy Team.division_id — a team plays in a
        # division only for the season(s) it is registered there.
        team_ids = self.setup.registered_team_ids_in_division(division_id)
        teams = [t for t in (self.store.get_team(tid) for tid in team_ids)
                 if t is not None]
        rows = {t.id: {"team_id": t.id, "team_name": t.name, "gp": 0,
                       "w": 0, "l": 0, "t": 0, "gf": 0, "ga": 0, "gd": 0, "pts": 0}
                for t in teams}
        for g in self.store.all_games():
            if g.division_id != division_id or g.cancelled:
                continue
            if public_only and not g.published:
                continue
            r = self.store.result_for_game(g.id)
            if r is None or r.status != ResultStatus.FINAL:
                continue
            home, away = rows.get(g.home_team_id), rows.get(g.away_team_id)
            if home is None or away is None:
                continue
            self._apply_result(home, r.home_score, r.away_score)
            self._apply_result(away, r.away_score, r.home_score)
        ranked = sorted(rows.values(),
                        key=lambda x: (-x["pts"], -x["gd"], -x["gf"], x["team_name"]))
        return {"division_id": division_id, "standings": ranked}

    # -- public, no-auth surface (#83) -------------------------------------
    # A clean public web surface (schedule / standings / result detail) built
    # from public-safe fields only — team names, division, rink, date/time,
    # score. Never player names, rosters, availability, or officials.
    def _public_game_dto(self, g) -> dict:
        venue_name = self._venue_name_for_game(g)
        div = self.store.get_division(g.division_id) if g.division_id else None
        result = self.store.result_for_game(g.id)
        final = result is not None and result.status == ResultStatus.FINAL
        if g.cancelled:
            status = "Cancelled"
        elif final:
            status = "Final"
        else:
            status = "Scheduled"
        return {
            "game_id": g.id,
            "division_id": g.division_id,
            "division_name": div.name if div else None,
            "home_team_name": self._team_name(g.home_team_id),
            "away_team_name": self._team_name(g.away_team_id),
            "rink_name": g.rink, "venue_name": venue_name,
            "start_time": g.start_time.isoformat() if g.start_time else None,
            "status": status,
            "home_score": result.home_score if final else None,
            "away_score": result.away_score if final else None,
        }

    @catch
    def get_public_schedule(self) -> dict:
        """Published, non-cancelled-hidden fixtures for the public schedule."""
        leagues = self.store.all_programs()
        league = leagues[0] if leagues else None
        divisions = [{"id": d.id, "name": d.name}
                     for d in self.store.all_divisions()]
        fixtures = [self._public_game_dto(g)
                    for g in sorted(self.store.all_games(),
                                    key=lambda x: x.start_time or "")
                    if g.published]
        return {
            "league_name": league.name if league else None,
            "divisions": divisions,
            "fixtures": fixtures,
        }

    @catch
    def get_public_standings(self, division_id: str) -> dict:
        """Public division standings — published games only, so an unpublished
        game's final result cannot leak into the public table by aggregation."""
        return self._standings_for_division(division_id, public_only=True)

    @catch
    def get_public_game(self, game_id: str) -> dict:
        """Public-safe detail for one published game, else not found."""
        g = self.store.get_game(game_id)
        if g is None or not g.published:
            raise NotFoundError("Game not found.")
        return self._public_game_dto(g)

    # -- season scheduler v1 (#84) -----------------------------------------
    @catch
    def draft_season_schedule(self, division_id: str, slot_ids=None,
                              constraints=None) -> dict:
        """Generate a draft round-robin schedule for a division (#84/#85).

        Returns a proposal only — no games are created or published here. The
        result is deterministic and safe to regenerate. ``constraints`` may
        carry blackout dates, minimum rest, and a max games/team/day cap (#85).
        """
        if not division_id:
            raise ValidationError("A division_id is required.")
        if self.store.get_division(division_id) is None:
            raise NotFoundError("Division not found.")
        return draft_schedule(self.store, division_id, slot_ids=slot_ids,
                              constraints=constraints)

    def _team_name(self, team_id) -> Optional[str]:
        """Shared by every game DTO builder (public/draft review) so a
        missing/unknown team resolves to None the same way everywhere."""
        t = self.store.get_team(team_id) if team_id else None
        return t.name if t else None

    # -- draft review + publish (#86) --------------------------------------
    def _active_officials(self, game_id: str):
        """Active (proposed/accepted) assignments for a game — a declined
        assignment frees the official (#30 review). Shared by the demo
        overview and the scheduler review list (#106) so both report the
        same officials posture."""
        return [a for a in self.store.assignments_for_game(game_id)
                if a.status.is_active]

    def _draft_game_dto(self, g) -> dict:
        div = self.store.get_division(g.division_id) if g.division_id else None
        return {"game_id": g.id, "division_id": g.division_id,
                "division_name": div.name if div else None,
                "home_team_name": self._team_name(g.home_team_id),
                "away_team_name": self._team_name(g.away_team_id),
                "rink_name": g.rink,
                "start_time": g.start_time.isoformat() if g.start_time else None,
                "is_draft": g.is_draft, "published": g.published}

    # A roster in either of these states is ready to play — the same bar the
    # operator dashboard's gameTriage() already holds games to client-side;
    # kept here as the single source of truth for the review issue below.
    _ROSTER_READY_STATUSES = frozenset({"roster_confirmed", "locked"})

    def _draft_review_row(self, g, slot_games: dict, double_booked: bool) -> dict:
        """Enriched per-draft-game row for the scheduler review screen (#106):
        officials/roster posture and any review issues, so an operator can
        spot problems before publishing rather than discovering them after."""
        div = self.store.get_division(g.division_id) if g.division_id else None
        slot = self.store.get_ice_slot(g.ice_slot_id) if g.ice_slot_id else None
        active = self._active_officials(g.id)
        accepted = sum(1 for a in active if a.status.value == "accepted")
        roster_status = self.roster.compute_roster_status(g.id).status.value

        issues = []
        if not active:
            issues.append("missing_officials")
        elif accepted < len(active):
            issues.append("officials_pending")
        if roster_status not in self._ROSTER_READY_STATUSES:
            issues.append("roster_not_ready")
        if g.ice_slot_id and len(slot_games.get(g.ice_slot_id, ())) > 1:
            issues.append("slot_conflict")
        if double_booked:
            issues.append("team_double_booked")

        return {
            "game_id": g.id, "division_id": g.division_id,
            "division_name": div.name if div else None,
            "rink_id": slot.rink_id if slot else None, "rink_name": g.rink,
            "home_team_id": g.home_team_id, "away_team_id": g.away_team_id,
            "home_team_name": self._team_name(g.home_team_id),
            "away_team_name": self._team_name(g.away_team_id),
            "start_time": g.start_time.isoformat() if g.start_time else None,
            "end_time": g.end_time.isoformat() if g.end_time else None,
            "is_draft": g.is_draft, "published": g.published,
            "officials_assigned": len(active), "officials_accepted": accepted,
            "roster_status": roster_status, "issues": issues,
        }

    @catch
    def commit_draft_schedule(self, division_id: str, slot_ids=None,
                              constraints=None, actor_id=None) -> dict:
        """Persist a generated draft as draft games (is_draft=True, unpublished),
        so they can be reviewed and then published (#86). Regenerates the
        proposal server-side (deterministic) and returns the created drafts +
        any unscheduled pairings."""
        proposal = self.draft_season_schedule(
            division_id, slot_ids=slot_ids, constraints=constraints)
        if isinstance(proposal, dict) and proposal.get("error"):
            return proposal
        created = []
        for d in proposal["draft_games"]:
            g = Game(
                id=self.store.next_id("game"),
                home_team_id=d["home_team_id"], away_team_id=d["away_team_id"],
                start_time=datetime.fromisoformat(d["start_time"]),
                end_time=datetime.fromisoformat(d["end_time"]) if d.get("end_time") else None,
                rink=d.get("rink_name"), division_id=division_id,
                ice_slot_id=d.get("ice_slot_id"),
                published=False, is_draft=True)
            self.store.add_game(g)
            created.append(self._draft_game_dto(g))
        # Committing a draft creates real (unpublished) rows — a state change,
        # so it is audited (#86).
        self.setup._audit(
            "draft_schedule_committed", "division", division_id, actor_id,
            {"created_count": len(created),
             "game_ids": [c["game_id"] for c in created],
             "unscheduled_count": len(proposal["unscheduled"])})
        return {"division_id": division_id, "created": created,
                "unscheduled": proposal["unscheduled"]}

    @catch
    def list_draft_games(self) -> dict:
        """Draft games plus a review summary (#106): counts by division/rink,
        published-vs-draft context, and a per-game issues list (missing
        officials, roster not ready, or a slot/team conflict) so an operator
        can review before publishing rather than discovering problems after.
        """
        all_games = self.store.all_games()
        drafts = [g for g in all_games if g.is_draft]

        # Slot-conflict detection: the generator (services/scheduler.py) never
        # proposes a slot another game already holds, so this should rarely
        # fire in practice — a defensive check for any two non-cancelled
        # games somehow sharing a slot.
        slot_games = {}
        for g in all_games:
            if g.ice_slot_id and not g.cancelled:
                slot_games.setdefault(g.ice_slot_id, []).append(g.id)

        # Team double-booking: the same overlap formula create_game already
        # enforces at manual-creation time (setup_service.py) — applied here
        # read-only, across ALL non-cancelled games, since a draft could in
        # principle collide with a game outside its own division.
        team_intervals = {}
        for g in all_games:
            if g.cancelled or g.start_time is None or g.end_time is None:
                continue
            for tid in (g.home_team_id, g.away_team_id):
                if tid:
                    team_intervals.setdefault(tid, []).append(
                        (g.id, g.start_time, g.end_time))

        def is_double_booked(g) -> bool:
            if g.start_time is None or g.end_time is None:
                return False
            for tid in (g.home_team_id, g.away_team_id):
                if not tid:
                    continue
                for gid, s, e in team_intervals.get(tid, ()):
                    if gid != g.id and intervals_overlap(g.start_time, g.end_time, s, e):
                        return True
            return False

        rows = [self._draft_review_row(g, slot_games, is_double_booked(g))
                for g in drafts]
        rows.sort(key=lambda r: r["start_time"] or "")

        by_division, by_rink = {}, {}
        for r in rows:
            dkey = r["division_name"] or "Unassigned"
            rkey = r["rink_name"] or "Unassigned"
            by_division[dkey] = by_division.get(dkey, 0) + 1
            by_rink[rkey] = by_rink.get(rkey, 0) + 1
        summary = {
            "draft_count": len(rows),
            "published_count": sum(1 for g in all_games if g.published),
            "issue_count": sum(1 for r in rows if r["issues"]),
            "by_division": by_division,
            "by_rink": by_rink,
        }
        return {"draft_games": rows, "summary": summary}

    def _draft_targets(self, game_ids, all_drafts):
        drafts = [g for g in self.store.all_games() if g.is_draft]
        if all_drafts:
            return drafts
        wanted = set(game_ids or [])
        return [g for g in drafts if g.id in wanted]

    @catch
    def publish_draft_games(self, game_ids=None, all_drafts=False,
                            actor_id=None) -> dict:
        """Publish draft games (#86).

        Clears the draft flag, then routes each game through
        ``setup.publish_game`` so bulk publish uses the *same* audited publish
        path as single-game publish (a ``game_published`` audit entry per
        game) rather than a silent direct save that bypasses the trail.
        """
        published = 0
        for g in self._draft_targets(game_ids, all_drafts):
            # Allocate the ice slot, matching the manual create_game invariant
            # (a game's slot is ALLOCATED, not left AVAILABLE) — otherwise a
            # published game sits on a slot the grid still treats as an open
            # drop target.
            slot = (self.store.get_ice_slot(g.ice_slot_id)
                    if g.ice_slot_id else None)
            if slot is not None:
                slot.status = IceSlotStatus.ALLOCATED
                self.store.save_ice_slot(slot)
            # Persist the draft→real transition first so it survives the
            # re-fetch inside publish_game (SqlStore returns fresh instances).
            g.is_draft = False
            self.store.save_game(g)
            self.setup.publish_game(g.id, True, actor_id)  # published + audit
            published += 1
        return {"published": published}

    @catch
    def discard_draft_games(self, game_ids=None, all_drafts=False,
                            actor_id=None) -> dict:
        """Delete draft games (never touches published/real games) (#86).

        Each discard is audited before deletion so the review action leaves a
        trail (a draft is state; discarding it is a state change)."""
        discarded = 0
        for g in self._draft_targets(game_ids, all_drafts):
            self.setup._audit("draft_game_discarded", "game", g.id, actor_id,
                              {"division_id": g.division_id,
                               "ice_slot_id": g.ice_slot_id})
            self.store.delete_game(g.id)
            discarded += 1
        return {"discarded": discarded}

    @staticmethod
    def _apply_result(row: dict, gf: int, ga: int) -> None:
        row["gp"] += 1
        row["gf"] += gf
        row["ga"] += ga
        row["gd"] = row["gf"] - row["ga"]
        if gf > ga:
            row["w"] += 1
            row["pts"] += 2
        elif gf == ga:
            row["t"] += 1
            row["pts"] += 1
        else:
            row["l"] += 1

    # -- coach controls ----------------------------------------------------
    @catch
    def lock_roster(self, game_id: str, actor_id: Optional[str] = None) -> dict:
        return _serialize(self.roster.lock_roster(game_id, actor_id))

    @catch
    def unlock_roster(self, game_id: str, actor_id: Optional[str] = None) -> dict:
        return _serialize(self.roster.unlock_roster(game_id, actor_id))

    @catch
    def cancel_game(self, game_id: str, actor_id: Optional[str] = None) -> dict:
        return _serialize(self.roster.cancel_game(game_id, actor_id))

    # ====================================================================
    # Full E2E demo overview (League / Arena / Schedule / Public)
    # ====================================================================
    @catch
    def _registration_is_operational(self, r) -> bool:
        """True only for a registration safe to act on (#180 review).

        Reuses the shared scheduling guard (season has a league, Team exists and
        its permanent league matches the season) and additionally requires any
        named division to actually belong to the registration's season — so a
        wrong-season / cross-league / missing-Team / missing-League row is never
        exposed to the operational UI.
        """
        season = self.store.get_season(r.season_id)
        if team_registration_valid(
                self.store, season, r.team_id, r.division_id) is None:
            return False
        if r.division_id is not None:
            division = self.store.get_division(r.division_id)
            if division is None or division.season_id != r.season_id:
                return False
        return True

    def get_demo_overview(self) -> dict:
        """Assemble the League/Arena/Schedule/Public view for the E2E demo.

        The ``public`` section deliberately contains NO player names or any
        personal data — only fixture information that is safe to show fans.
        """
        divisions = {d.id: d for d in self.store.all_divisions()}
        levels = {lv.id: lv for lv in self.store.all_leagues()}
        clubs = {c.id: c for c in self.store.all_clubs()}
        teams = {t.id: t for t in self.store.all_teams()}
        orgs = {o.id: o for o in self.store.all_organizations()}
        leagues_by_id = {lg.id: lg for lg in self.store.all_programs()}
        venues = {v.id: v for v in self.store.all_venues()}
        rinks = {r.id: r for r in self.store.all_rinks()}

        def is_junior(div):
            if div is None:
                return False
            tag = (div.age_group or div.name or "").upper()
            return tag.startswith("U")

        def team_name(tid):
            t = teams.get(tid)
            return t.name if t else tid

        # Divisions now carry an optional owning level/tier (#166) — surface the
        # id + resolved name so the Setup hierarchy can group divisions by level.
        division_rows = [
            {"id": d.id, "season_id": d.season_id, "name": d.name,
             "age_group": d.age_group, "is_junior": is_junior(d),
             "level_id": d.league_id,
             "level_name": levels[d.league_id].name
             if d.league_id in levels else None}
            for d in divisions.values()
        ]
        # A Team is a permanent member of a League (#180); its season/division
        # participation is NOT on the Team — it lives in SeasonTeamRegistration,
        # exposed as `registrations` below. The legacy Team.division_id is no
        # longer surfaced here so no operational UI can key off it.
        team_rows = [
            {"id": t.id, "name": t.name, "club_id": t.club_id,
             "league_id": t.program_id,
             "club_name": clubs[t.club_id].name if t.club_id in clubs else None}
            for t in teams.values()
        ]
        rink_rows = [
            {"id": r.id, "venue_id": r.venue_id, "name": r.name,
             "venue_name": venues[r.venue_id].name if r.venue_id in venues else None}
            for r in rinks.values()
        ]
        # Venues carry an optional owning organization (#166) and league (#173)
        # — surface both ids + resolved names so the Setup UI can label and
        # group venues by owner and league.
        venue_rows = [
            {"id": v.id, "name": v.name, "address": v.address, "timezone": v.timezone,
             "organization_id": v.organization_id,
             "organization_name": orgs[v.organization_id].name
             if v.organization_id in orgs else None,
             "league_id": v.league_id,
             "league_name": leagues_by_id[v.league_id].name
             if v.league_id in leagues_by_id else None}
            for v in venues.values()
        ]

        # Draft games (#86) are proposals under review — they must never surface
        # in the operator slot grid / schedule / calendar until published, so
        # they are excluded here. The dedicated draft-review view lists them.
        game_by_slot = {g.ice_slot_id: g for g in self.store.all_games()
                        if g.ice_slot_id and not g.is_draft}
        slot_rows = []
        for s in sorted(self.store.all_ice_slots(),
                        key=lambda x: (x.rink_id, x.start_time)):
            g = game_by_slot.get(s.id)
            slot_rows.append({
                "id": s.id, "rink_id": s.rink_id,
                "rink_name": rinks[s.rink_id].name if s.rink_id in rinks else None,
                "start_time": s.start_time.isoformat(),
                "end_time": s.end_time.isoformat(),
                "slot_type": s.slot_type.value, "status": s.status.value,
                "game_id": g.id if g else None,
                "game_label": f"{team_name(g.home_team_id)} vs "
                              f"{team_name(g.away_team_id)}" if g else None,
            })

        schedule, public_fixtures = [], []
        for g in self.store.all_games():
            if g.is_draft:
                continue  # unpublished draft — kept out of normal views (#86)
            div = divisions.get(g.division_id)
            rstatus = self.roster.compute_roster_status(g.id)
            venue_name = None
            slot = self.store.get_ice_slot(g.ice_slot_id) if g.ice_slot_id else None
            if slot and slot.rink_id in rinks:
                rk = rinks[slot.rink_id]
                venue_name = venues[rk.venue_id].name if rk.venue_id in venues else None
            g_active = self._active_officials(g.id)
            g_result = self.store.result_for_game(g.id)
            schedule.append({
                "game_id": g.id,
                "home_team_id": g.home_team_id,
                "away_team_id": g.away_team_id,
                "home_team_name": team_name(g.home_team_id),
                "away_team_name": team_name(g.away_team_id) if g.away_team_id else None,
                "division_id": g.division_id,
                "division_name": div.name if div else None,
                "ice_slot_id": g.ice_slot_id,
                "rink_name": g.rink, "venue_name": venue_name,
                "start_time": g.start_time.isoformat(),
                "roster_status": rstatus.status.value,
                "published": g.published,
                "cancelled": g.cancelled,  # Games view offers Cancel, not Delete (#215)
                # Officials summary for the Games operations checklist (#30).
                "officials_assigned": len(g_active),
                "officials_accepted": sum(
                    1 for a in g_active if a.status.value == "accepted"),
                # Result lifecycle for the operations checklist (#31): None/draft/final.
                "result_status": g_result.status.value if g_result else None,
            })
            # PUBLIC: only PUBLISHED games, fixture info only — no players/PII.
            if g.published and not g.cancelled:
                public_fixtures.append({
                    "division_name": div.name if div else None,
                    "home_team_name": team_name(g.home_team_id),
                    "away_team_name": team_name(g.away_team_id) if g.away_team_id else None,
                    "venue_name": venue_name, "rink_name": g.rink,
                    "start_time": g.start_time.isoformat(),
                    "status": "Scheduled",
                    "is_junior": is_junior(div),
                })

        leagues = [program_to_v1(_serialize(x)) for x in self.store.all_programs()]
        seasons = [season_to_v1(_serialize(x)) for x in self.store.all_seasons()]
        # `/api/demo/overview` is unauthenticated (do_GET in web/server.py
        # serves it with no session/permission check) — actor_id/detail must
        # NOT be exposed for every setup-audit action, only for import
        # batches (#102's Activity drill-down needs them there). Other
        # actions' detail dicts can carry things this endpoint has no
        # business handing to an anonymous caller (e.g. user_account_created
        # stores {"username", "role"}). Scope the extra fields to exactly
        # the import-batch summary row and its linked per-row children.
        def _is_import_related(a):
            return a.entity_type == "import_batch" or (a.detail or {}).get(
                "import_batch_id") is not None
        setup_audit = []
        for a in self.store.all_setup_audit():
            entry = {"action": a.action, "entity_type": a.entity_type,
                     "entity_id": a.entity_id, "at": a.at.isoformat()}
            if _is_import_related(a):
                entry["actor_id"] = a.actor_id
                entry["detail"] = a.detail
            setup_audit.append(entry)
        return {
            "league": leagues[0] if leagues else None,
            "leagues": leagues,
            "seasons": seasons,
            "levels": [_serialize(lv) for lv in levels.values()],
            "divisions": division_rows,
            "clubs": [_serialize(c) for c in clubs.values()],
            "teams": team_rows,
            "organizations": [_serialize(o) for o in orgs.values()],
            "venues": venue_rows,
            "rinks": rink_rows,
            "ice_slots": slot_rows,
            "officials": [
                {"id": o.id, "name": o.name,
                 "home_club_name": (clubs[o.home_club_id].name
                                    if o.home_club_id in clubs else None)}
                for o in self.store.all_officials()
            ],
            "schedule": schedule,
            "public_fixtures": public_fixtures,
            # Active season/division participation (#180): the source of truth
            # for which Team plays which Division in which Season. Operational UI
            # (e.g. the scheduling wizard) filters teams through these, never the
            # legacy Team.division_id. Only OPERATIONALLY-VALID rows are exposed
            # (review): a corrupt/retained row — wrong season, cross-league or
            # missing Team/League, or a division that isn't in the row's season —
            # is filtered out here so it can never be offered in the picker.
            "registrations": [
                {"team_id": r.team_id, "season_id": r.season_id,
                 "division_id": r.division_id}
                for r in self.store.all_season_team_registrations()
                if r.active and self._registration_is_operational(r)
            ],
            "setup_audit": setup_audit,
            "setup_audit_count": len(setup_audit),
        }

    @catch
    def get_setup_hierarchy(self) -> dict:
        """Nested, UI-ready setup tree (#166 PR C, extended in #173 PR B).

        Two trees — facility ownership (Organization → League → Venue → Rink,
        since a venue now belongs to a league under an owner) and competition
        structure (League → Season → Level → Division → Team) — plus a
        ``missing_assignments`` block listing records whose parent link is
        absent or dangling, so an operator can find and fix them. Leaves carry
        counts (``ice_slot_count``, ``player_count``), never rosters, so the
        structural payload stays light. This route is operator-gated
        (MANAGE_SETUP) at the HTTP boundary; player *names* still never appear.
        """
        orgs = self.store.all_organizations()
        venues = self.store.all_venues()
        rinks = self.store.all_rinks()
        leagues = self.store.all_programs()
        seasons = self.store.all_seasons()
        levels = self.store.all_leagues()
        divisions = self.store.all_divisions()
        teams = self.store.all_teams()
        clubs = {c.id: c for c in self.store.all_clubs()}

        # Counts indexed by parent id — one pass each, no per-node re-scan.
        slot_count = {}
        for s in self.store.all_ice_slots():
            slot_count[s.rink_id] = slot_count.get(s.rink_id, 0) + 1
        player_count = {}
        for p in self.store.all_players():
            player_count[p.team_id] = player_count.get(p.team_id, 0) + 1

        rinks_by_venue = _group(rinks, "venue_id")
        venues_by_league = _group(venues, "league_id")
        leagues_by_org = _group(leagues, "operator_organization_id")
        seasons_by_league = _group(seasons, "program_id")
        levels_by_season = _group(levels, "season_id")
        divs_by_season = _group(divisions, "season_id")
        # Teams nest under a Division via their active SeasonTeamRegistration
        # (#180), never the legacy Team.division_id — so a permanent team with a
        # null legacy division still shows under the division it's registered in.
        teams_by_id = {t.id: t for t in teams}
        teams_by_div = {}
        for _reg in self.store.all_season_team_registrations():
            if _reg.active and _reg.division_id:
                _tm = teams_by_id.get(_reg.team_id)
                if _tm is not None:
                    teams_by_div.setdefault(_reg.division_id, []).append(_tm)

        def rink_node(r):
            return {"id": r.id, "name": r.name,
                    "ice_slot_count": slot_count.get(r.id, 0)}

        def venue_node(v):
            vr = rinks_by_venue.get(v.id, [])
            return {"id": v.id, "name": v.name,
                    "rinks": [rink_node(r) for r in vr]}

        # Facility tree now flows through the league (#173): Organization →
        # League → Venue → Rink. Venues not yet under a league appear only in
        # the missing-assignment queue, not in this tree.
        def league_facility_node(lg):
            return {"id": lg.id, "name": lg.name,
                    "venues": [venue_node(v) for v in venues_by_league.get(lg.id, [])]}

        organizations = [
            {"id": o.id, "name": o.name, "short_name": o.short_name,
             "leagues": [league_facility_node(lg) for lg in leagues_by_org.get(o.id, [])]}
            for o in orgs
        ]

        def team_node(t):
            return {"id": t.id, "name": t.name, "club_id": t.club_id,
                    "club_name": clubs[t.club_id].name if t.club_id in clubs else None,
                    "player_count": player_count.get(t.id, 0)}

        def division_node(d):
            return {"id": d.id, "name": d.name,
                    "teams": [team_node(t) for t in teams_by_div.get(d.id, [])]}

        league_tree = []
        for lg in leagues:
            season_nodes = []
            for s in seasons_by_league.get(lg.id, []):
                season_levels = sorted(
                    levels_by_season.get(s.id, []),
                    key=lambda lv: (lv.sort_order or 0, lv.name))
                level_ids = {lv.id for lv in season_levels}
                divs_by_level = _group(divs_by_season.get(s.id, []), "league_id")
                level_nodes = [
                    {"id": lv.id, "name": lv.name, "sort_order": lv.sort_order,
                     "divisions": [division_node(d) for d in divs_by_level.get(lv.id, [])]}
                    for lv in season_levels
                ]
                # Divisions in this season with no level (or a dangling one).
                no_level = [d for d in divs_by_season.get(s.id, [])
                            if not d.league_id or d.league_id not in level_ids]
                season_nodes.append({
                    "id": s.id, "name": s.name, "levels": level_nodes,
                    "divisions_without_level": [division_node(d) for d in no_level],
                })
            league_tree.append({"id": lg.id, "name": lg.name, "seasons": season_nodes})

        org_ids = {o.id for o in orgs}
        venue_ids = {v.id for v in venues}
        league_ids_all = {lg.id for lg in leagues}
        level_ids_all = {lv.id for lv in levels}
        team_ids = {t.id for t in teams}
        league_owner = {lg.id: lg.operator_organization_id for lg in leagues}
        idname = lambda x: {"id": x.id, "name": x.name}
        missing = {
            # League↔facility relationship gaps (#173).
            "leagues_without_organization":
                [idname(lg) for lg in leagues
                 if not lg.operator_organization_id
                 or lg.operator_organization_id not in org_ids],
            "venues_without_league":
                [idname(v) for v in venues if not v.league_id or v.league_id not in league_owner],
            # A venue assigned to a league whose owner differs from the venue's
            # own owner — only reachable via legacy data, since create/assign
            # enforce agreement; surfaced so an operator can reconcile it.
            "venue_owner_mismatches":
                [idname(v) for v in venues
                 if v.league_id in league_owner
                 and (v.organization_id or None) != (league_owner.get(v.league_id) or None)],
            "venues_without_organization":
                [idname(v) for v in venues if not v.organization_id or v.organization_id not in org_ids],
            "rinks_without_venue":
                [idname(r) for r in rinks if not r.venue_id or r.venue_id not in venue_ids],
            "divisions_without_level":
                [idname(d) for d in divisions if not d.league_id or d.league_id not in level_ids_all],
            "teams_without_club":
                [idname(t) for t in teams if not t.club_id or t.club_id not in clubs],
            # A Team must belong to a valid League (#180); a missing/invalid
            # league_id is the real "needs assignment", not a missing division.
            "teams_without_league":
                [idname(t) for t in teams if not t.program_id or t.program_id not in league_ids_all],
            # Player *name* is PII and deliberately omitted even here — an
            # orphan is surfaced by id only, keeping this tree name-free so the
            # count-only privacy invariant holds end to end (#166).
            "players_without_team":
                [{"id": p.id} for p in self.store.all_players()
                 if not p.team_id or p.team_id not in team_ids],
        }

        return {
            "organizations": organizations,
            "leagues": league_tree,
            "missing_assignments": missing,
        }

    @catch
    def get_setup_hierarchy_v2(self) -> dict:
        """Canonical Program→Season→League→Division setup tree (#233 Slice C2).

        The v2 counterpart of ``get_setup_hierarchy``: canonical keys throughout
        (``program_id`` / ``operator_organization_id`` / competition
        ``league_id``), Program as the umbrella and the grouping League between
        Season and Division. The v1 ``get_setup_hierarchy`` is untouched. Leaves
        carry counts only (``player_count``) — never rosters or player names —
        so this operator-gated read stays name-free like its v1 sibling.

        "Division optional" is realized structurally (#233 Slice C2 review): a
        division-less ACTIVE registered Team hangs directly off its registration
        League in a ``teams_without_division`` list, rather than being dropped. A
        Team nests under a Division only when everything AGREES — the
        registration's ``league_id`` equals the Division's ``league_id`` equals
        the owning League. Parentless/dangling Divisions and inconsistent
        registrations (League not in the Season, or League≠Division's League) are
        surfaced in a per-Season ``needs_assignment`` block as INVALID, never as a
        valid branch of the tree."""
        programs = self.store.all_programs()
        seasons = self.store.all_seasons()
        leagues = self.store.all_leagues()  # the grouping League (was Level)
        divisions = self.store.all_divisions()
        teams = self.store.all_teams()

        player_count = {}
        for p in self.store.all_players():
            player_count[p.team_id] = player_count.get(p.team_id, 0) + 1

        seasons_by_program = _group(seasons, "program_id")
        leagues_by_season = _group(leagues, "season_id")
        divs_by_season = _group(divisions, "season_id")
        divisions_by_id = {d.id: d for d in divisions}
        teams_by_id = {t.id: t for t in teams}
        # Active registrations grouped by their Season, so each Season resolves
        # participation from its own rows.
        regs_by_season = {}
        for reg in self.store.all_season_team_registrations():
            if reg.active:
                regs_by_season.setdefault(reg.season_id, []).append(reg)

        def team_node(t):
            return {"id": t.id, "name": t.name, "club_id": t.club_id,
                    "program_id": t.program_id,
                    "player_count": player_count.get(t.id, 0)}

        program_tree = []
        for prog in programs:
            season_nodes = []
            for s in seasons_by_program.get(prog.id, []):
                season_leagues = sorted(
                    leagues_by_season.get(s.id, []),
                    key=lambda lv: (lv.sort_order or 0, lv.name))
                league_ids = {lv.id for lv in season_leagues}
                divs_by_league = _group(divs_by_season.get(s.id, []), "league_id")

                # Resolve every active registration in this Season into exactly
                # one bucket: a valid Division nest, a valid League-direct team,
                # or an invalid (needs-assignment) row.
                teams_by_div = {}                 # division_id -> [team]
                teams_direct_by_league = {}       # league_id  -> [team]
                needs_assignment_regs = []
                for reg in regs_by_season.get(s.id, []):
                    tm = teams_by_id.get(reg.team_id)
                    if tm is None:
                        needs_assignment_regs.append(
                            {"registration_id": reg.id, "team_id": reg.team_id,
                             "league_id": reg.league_id,
                             "division_id": reg.division_id,
                             "reason": "team_missing"})
                        continue
                    if reg.division_id:
                        div = divisions_by_id.get(reg.division_id)
                        # A Team nests under its Division only when the Division is
                        # in this Season, sits under a real League here, and the
                        # registration's League agrees with the Division's League.
                        if (div is not None and div.season_id == s.id
                                and div.league_id in league_ids
                                and reg.league_id == div.league_id):
                            teams_by_div.setdefault(div.id, []).append(tm)
                        else:
                            needs_assignment_regs.append(
                                {"registration_id": reg.id, "team_id": reg.team_id,
                                 "league_id": reg.league_id,
                                 "division_id": reg.division_id,
                                 "reason": "registration_league_division_mismatch"})
                    else:
                        # Division-less: hangs directly off its registration
                        # League when that League is a real League in this Season.
                        if reg.league_id in league_ids:
                            teams_direct_by_league.setdefault(
                                reg.league_id, []).append(tm)
                        else:
                            needs_assignment_regs.append(
                                {"registration_id": reg.id, "team_id": reg.team_id,
                                 "league_id": reg.league_id,
                                 "division_id": reg.division_id,
                                 "reason": "registration_league_not_in_season"})

                def division_node(d):
                    return {"id": d.id, "name": d.name, "age_group": d.age_group,
                            "league_id": d.league_id,
                            "teams": [team_node(t)
                                      for t in teams_by_div.get(d.id, [])]}

                league_nodes = [
                    {"id": lv.id, "name": lv.name, "sort_order": lv.sort_order,
                     "divisions": [division_node(d)
                                   for d in divs_by_league.get(lv.id, [])],
                     # Division-optional: teams registered directly under this
                     # League with no Division.
                     "teams_without_division": [
                         team_node(t)
                         for t in teams_direct_by_league.get(lv.id, [])]}
                    for lv in season_leagues
                ]
                # Parentless / dangling Divisions (no League, or a League that
                # doesn't resolve in this Season) are INVALID structure — surfaced
                # for reassignment, never presented as a valid branch.
                dangling_divs = [d for d in divs_by_season.get(s.id, [])
                                 if not d.league_id or d.league_id not in league_ids]
                season_nodes.append({
                    "id": s.id, "name": s.name, "leagues": league_nodes,
                    "needs_assignment": {
                        "divisions_without_league": [
                            {"id": d.id, "name": d.name, "age_group": d.age_group,
                             "league_id": d.league_id} for d in dangling_divs],
                        "registrations": needs_assignment_regs,
                    },
                })
            program_tree.append({
                "id": prog.id, "name": prog.name,
                "operator_organization_id": prog.operator_organization_id,
                "seasons": season_nodes,
            })

        return {"programs": program_tree}

    # ====================================================================
    # League + Arena setup
    # ====================================================================
    @catch
    def create_program(self, name: str, country: str = "", timezone: str = "UTC",
                       operator_organization_id: Optional[str] = None,
                       actor_id: Optional[str] = None) -> dict:
        return _serialize(self.setup.create_program(
            name, country, timezone, operator_organization_id, actor_id))

    @catch
    def list_programs(self) -> List[dict]:
        return [_serialize(x) for x in self.setup.list_programs()]

    @catch
    def create_season(self, program_id: str, name: str,
                      start_date: Optional[str] = None, end_date: Optional[str] = None,
                      actor_id: Optional[str] = None) -> dict:
        return _serialize(self.setup.create_season(
            program_id, name, _parse_dt(start_date, "start_date"),
            _parse_dt(end_date, "end_date"), actor_id))

    @catch
    def create_league(self, season_id: str, name: str, sort_order: int = 0,
                      actor_id: Optional[str] = None) -> dict:
        return _serialize(self.setup.create_league(season_id, name, sort_order, actor_id))

    @catch
    def create_division(self, season_id: str, name: str, age_group: str = "",
                        league_id: Optional[str] = None,
                        actor_id: Optional[str] = None) -> dict:
        return _serialize(self.setup.create_division(
            season_id, name, age_group, league_id, actor_id))

    @catch
    def create_division_v2(self, league_id: str, name: str, age_group: str = "",
                           actor_id: Optional[str] = None) -> dict:
        """Canonical v2 division create (#233 Slice C2): parented by a grouping
        League (REQUIRED); Season is derived from the league."""
        return _serialize(self.setup.create_division_under_league(
            league_id, name, age_group, actor_id))

    @catch
    def create_club(self, name: str, country: str = "",
                    actor_id: Optional[str] = None) -> dict:
        return _serialize(self.setup.create_club(name, country, actor_id))

    @catch
    def create_team(self, club_id: str, division_id: Optional[str] = None,
                    name: str = "", actor_id: Optional[str] = None,
                    program_id: Optional[str] = None) -> dict:
        return _serialize(self.setup.create_team(
            club_id, division_id, name, actor_id, program_id=program_id))

    # -- permanent teams + season registrations (#180) ---------------------
    @catch
    def register_team_for_season(self, season_id: str, team_id: str,
                                 division_id: Optional[str] = None,
                                 actor_id: Optional[str] = None,
                                 league_id: Optional[str] = None) -> dict:
        # ``league_id`` is the v2 canonical path (#233 Slice C2): when supplied
        # it is required-and-validated; when omitted (v1) the C1b derivation runs.
        return _serialize(self.setup.register_team_for_season(
            season_id, team_id, division_id, actor_id, league_id=league_id))

    @catch
    def assign_season_team_division(self, registration_id: str,
                                    division_id: Optional[str] = None,
                                    actor_id: Optional[str] = None,
                                    v2: bool = False) -> dict:
        return _serialize(self.setup.assign_season_team_division(
            registration_id, division_id, actor_id, v2=v2))

    @catch
    def assign_season_team_league(self, registration_id: str,
                                  league_id: Optional[str] = None,
                                  actor_id: Optional[str] = None) -> dict:
        """Canonical v2 (#233 Slice C2): reassign a registration's League."""
        return _serialize(self.setup.assign_season_team_league(
            registration_id, league_id, actor_id))

    @catch
    def unregister_team_from_season(self, registration_id: str,
                                    actor_id: Optional[str] = None) -> dict:
        return _serialize(self.setup.unregister_team_from_season(
            registration_id, actor_id))

    @catch
    def roll_forward_registrations(self, from_season_id: str, to_season_id: str,
                                   selections: Optional[list] = None,
                                   actor_id: Optional[str] = None) -> dict:
        result = self.setup.roll_forward_registrations(
            from_season_id, to_season_id, selections, actor_id)
        return {"rolled_forward": result["rolled_forward"],
                "skipped": result["skipped"],
                "registrations": [_serialize(r) for r in result["registrations"]]}

    @catch
    def roll_forward_registrations_v2(self, from_season_id: str,
                                      to_season_id: str,
                                      selections: Optional[list] = None,
                                      actor_id: Optional[str] = None) -> dict:
        """Canonical v2 rollover (#233 Slice C2): each selection carries a
        required target league_id, written verbatim onto the registration."""
        result = self.setup.roll_forward_registrations_v2(
            from_season_id, to_season_id, selections, actor_id)
        return {"rolled_forward": result["rolled_forward"],
                "skipped": result["skipped"],
                "registrations": [_serialize(r) for r in result["registrations"]]}

    @catch
    def list_season_team_registrations(self, season_id: str) -> dict:
        rows = [_serialize(r)
                for r in self.store.registrations_for_season(season_id)]
        return {"registrations": rows}

    @catch
    def list_program_teams(self, program_id: str) -> dict:
        # v1 boundary (#233 C1b): this backs the v1 read route
        # GET /api/setup/leagues/{id}/teams (and the frontend's permanent-team
        # panel), so each team row is mapped back to its legacy key
        # (program_id → league_id) to keep the v1 contract's same JSON
        # keys/shape and values.
        rows = [team_to_v1(_serialize(t))
                for t in self.store.teams_for_program(program_id)]
        return {"teams": rows}

    @catch
    def list_program_teams_v2(self, program_id: str) -> dict:
        """Canonical v2 (#233 Slice C2): a program's permanent teams with
        canonical keys (program_id, not the legacy league_id)."""
        rows = [_serialize(t) for t in self.store.teams_for_program(program_id)]
        return {"teams": rows}

    # -- safe destructive deletion (#215) ---------------------------------
    # Each returns the serialized deleted record on success, or the structured
    # dependency error (code "has_dependencies", with a details breakdown) when
    # blocked. The service runs a pre-write dependency gate, so a blocked delete
    # writes nothing.
    @catch
    def delete_organization(self, org_id: str,
                            actor_id: Optional[str] = None) -> dict:
        return _serialize(self.setup.delete_organization(org_id, actor_id))

    @catch
    def delete_program(self, program_id: str, actor_id: Optional[str] = None) -> dict:
        return _serialize(self.setup.delete_program(program_id, actor_id))

    @catch
    def delete_league(self, league_id: str, actor_id: Optional[str] = None) -> dict:
        return _serialize(self.setup.delete_league(league_id, actor_id))

    @catch
    def delete_season(self, season_id: str, actor_id: Optional[str] = None) -> dict:
        return _serialize(self.setup.delete_season(season_id, actor_id))

    @catch
    def delete_division(self, division_id: str,
                        actor_id: Optional[str] = None) -> dict:
        return _serialize(self.setup.delete_division(division_id, actor_id))

    @catch
    def delete_club(self, club_id: str, actor_id: Optional[str] = None) -> dict:
        return _serialize(self.setup.delete_club(club_id, actor_id))

    @catch
    def delete_team(self, team_id: str, actor_id: Optional[str] = None) -> dict:
        return _serialize(self.setup.delete_team(team_id, actor_id))

    @catch
    def delete_venue(self, venue_id: str, actor_id: Optional[str] = None) -> dict:
        return _serialize(self.setup.delete_venue(venue_id, actor_id))

    @catch
    def delete_rink(self, rink_id: str, actor_id: Optional[str] = None) -> dict:
        return _serialize(self.setup.delete_rink(rink_id, actor_id))

    @catch
    def delete_ice_slot(self, slot_id: str, actor_id: Optional[str] = None) -> dict:
        return _serialize(self.setup.delete_ice_slot(slot_id, actor_id))

    @catch
    def delete_game(self, game_id: str, actor_id: Optional[str] = None) -> dict:
        return _serialize(self.setup.delete_game(game_id, actor_id))

    # -- reassignment: move a record under a new parent (#166 PR D) --------
    @catch
    def assign_venue_organization(self, venue_id: str,
                                  organization_id: Optional[str] = None,
                                  actor_id: Optional[str] = None) -> dict:
        return _serialize(self.setup.assign_venue_organization(
            venue_id, organization_id, actor_id))

    @catch
    def assign_rink_venue(self, rink_id: str, venue_id: str,
                          actor_id: Optional[str] = None) -> dict:
        return _serialize(self.setup.assign_rink_venue(rink_id, venue_id, actor_id))

    @catch
    def assign_division_league(self, division_id: str,
                               league_id: Optional[str] = None,
                               actor_id: Optional[str] = None,
                               v2: bool = False) -> dict:
        return _serialize(self.setup.assign_division_league(
            division_id, league_id, actor_id, v2=v2))

    @catch
    def assign_team_club(self, team_id: str, club_id: Optional[str] = None,
                         actor_id: Optional[str] = None) -> dict:
        return _serialize(self.setup.assign_team_club(team_id, club_id, actor_id))

    # assign_team_division removed (#180) — see SetupService; a Team's seasonal
    # division lives in SeasonTeamRegistration (assign_season_team_division).

    @catch
    def assign_player_team(self, player_id: str, team_id: str,
                           actor_id: Optional[str] = None) -> dict:
        return _serialize(self.setup.assign_player_team(player_id, team_id, actor_id))

    @catch
    def assign_program_organization(self, program_id: str,
                                    organization_id: Optional[str] = None,
                                    actor_id: Optional[str] = None) -> dict:
        return _serialize(self.setup.assign_program_organization(
            program_id, organization_id, actor_id))

    @catch
    def assign_venue_league(self, venue_id: str, league_id: Optional[str] = None,
                            actor_id: Optional[str] = None) -> dict:
        return _serialize(self.setup.assign_venue_league(venue_id, league_id, actor_id))

    @catch
    def create_organization(self, name: str, short_name: str = "",
                            actor_id: Optional[str] = None) -> dict:
        return _serialize(self.setup.create_organization(name, short_name, actor_id))

    @catch
    def create_venue(self, name: str, address: str = "", timezone: str = "UTC",
                     organization_id: Optional[str] = None,
                     league_id: Optional[str] = None,
                     actor_id: Optional[str] = None) -> dict:
        return _serialize(self.setup.create_venue(
            name, address, timezone, organization_id, league_id, actor_id))

    @catch
    def create_rink(self, venue_id: str, name: str,
                    actor_id: Optional[str] = None) -> dict:
        return _serialize(self.setup.create_rink(venue_id, name, actor_id))

    @catch
    def create_ice_slot(self, rink_id: str, start_time: str, end_time: str,
                        slot_type: str = "game", actor_id: Optional[str] = None) -> dict:
        return _serialize(self.setup.create_ice_slot(
            rink_id, _parse_dt(start_time, "start_time"),
            _parse_dt(end_time, "end_time"),
            _parse_enum(IceSlotType, slot_type, "slot_type"), actor_id))

    @catch
    def create_player(self, team_id: str, name: str, position: str,
                      jersey_number: Optional[int] = None,
                      email: Optional[str] = None,
                      is_active: bool = True,
                      actor_id: Optional[str] = None) -> dict:
        return _serialize(self.setup.add_player(
            team_id, name, _parse_enum(Position, position, "position"),
            jersey_number=jersey_number, email=email, is_active=is_active,
            actor_id=actor_id))

    @catch
    def list_players(self, team_id: Optional[str] = None) -> List[dict]:
        players = (self.store.players_for_team(team_id) if team_id
                  else self.store.all_players())
        return [_serialize(p) for p in players]

    @catch
    def create_game(self, season_id: str, division_id: str, home_team_id: str,
                    away_team_id: str, ice_slot_id: str, target_goalies: int = 1,
                    target_skaters: int = 15, max_skaters: int = 18,
                    allow_division_override: bool = False,
                    actor_id: Optional[str] = None,
                    league_id: Optional[str] = None) -> dict:
        # ``league_id`` is the v2 canonical scope (#233 Slice C2): when supplied
        # it is required-and-validated and division_id is optional; when omitted
        # (v1) division_id stays mandatory and the league is derived from it.
        return _serialize(self.setup.create_game(
            season_id, division_id, home_team_id, away_team_id, ice_slot_id,
            target_goalies, target_skaters, max_skaters,
            allow_division_override, actor_id, league_id=league_id))

    # ====================================================================
    # Pilot onboarding import — dry-run validator (#92)
    # ====================================================================
    @catch
    def get_import_dry_run(self, sheets_csv: dict) -> dict:
        """Validate a CSV-shaped onboarding import without writing anything.

        ``sheets_csv`` maps ``"<sheet>_csv"`` -> raw CSV text for any of
        ``teams``, ``players``, ``officials``, ``rinks``, ``ice_slots``; any
        key may be absent (treated as an empty sheet). Parses each present
        sheet, then delegates to the pure :func:`validate_import` — this
        method (and everything it calls) never touches ``self.store``.
        Row-level problems are collected into the returned report rather than
        raised; ``@catch`` here only guards a malformed request itself (e.g. a
        non-string CSV value).
        """
        sheets_csv = sheets_csv or {}
        sheets = {}
        for name in ("teams", "players", "officials", "rinks", "ice_slots"):
            text = sheets_csv.get(f"{name}_csv")
            if not text:
                continue
            if not isinstance(text, str):
                raise ValidationError(f"{name}_csv must be a CSV text string.")
            sheets[name] = parse_csv_text(text)
        return validate_import(sheets)

    # ====================================================================
    # Pilot onboarding import — teams + players commit (#93)
    # ====================================================================
    @catch
    def commit_teams_players_import(self, season_id: str, sheets_csv: dict,
                                    actor_id: Optional[str] = None) -> dict:
        """Commit step 2 of the pilot onboarding import wizard.

        Parses the present ``teams_csv``/``players_csv`` text (same shape as
        :meth:`get_import_dry_run`) and delegates to
        ``SetupService.commit_teams_players_import``, which re-validates via
        the same pure ``validate_import`` gate before writing anything.
        Officials/rinks/ice_slots commit is out of scope for this slice
        (#94/#95) — reject the request outright rather than silently
        dropping operator-submitted data.
        """
        sheets_csv = sheets_csv or {}
        unsupported = [key for key in
                      ("officials_csv", "rinks_csv", "ice_slots_csv")
                      if sheets_csv.get(key)]
        if unsupported:
            raise ValidationError(
                f"{', '.join(unsupported)} not supported by this commit "
                f"endpoint yet — see #94.")

        sheets = {}
        for name in ("teams", "players"):
            text = sheets_csv.get(f"{name}_csv")
            if not text:
                continue
            if not isinstance(text, str):
                raise ValidationError(f"{name}_csv must be a CSV text string.")
            sheets[name] = parse_csv_text(text)
        return self.setup.commit_teams_players_import(
            season_id, sheets, actor_id=actor_id)

    # ====================================================================
    # Pilot onboarding import — officials + availability commit (#94)
    # ====================================================================
    @catch
    def commit_officials_availability_import(self, sheets_csv: dict,
                                              actor_id: Optional[str] = None
                                              ) -> dict:
        """Commit step 3 of the pilot onboarding import wizard.

        Parses the present ``officials_csv``/``official_availability_csv``
        text (note the latter key matches the sheet name
        ``official_availability``, not ``officials_availability_csv``) and
        delegates to ``SetupService.commit_officials_availability_import``,
        which re-validates before writing anything. Teams/players/rinks/
        ice_slots commit is out of scope here (#93 already owns
        teams/players; rinks/ice_slots are #95) — reject the request
        outright rather than silently dropping operator-submitted data.
        """
        sheets_csv = sheets_csv or {}
        unsupported = [key for key in
                      ("teams_csv", "players_csv", "rinks_csv", "ice_slots_csv")
                      if sheets_csv.get(key)]
        if unsupported:
            raise ValidationError(
                f"{', '.join(unsupported)} not supported by this commit "
                f"endpoint — see #93/#95.")

        sheets = {}
        for name, key in (("officials", "officials_csv"),
                          ("official_availability", "official_availability_csv")):
            text = sheets_csv.get(key)
            if not text:
                continue
            if not isinstance(text, str):
                raise ValidationError(f"{key} must be a CSV text string.")
            sheets[name] = parse_csv_text(text)
        return self.setup.commit_officials_availability_import(
            sheets, actor_id=actor_id)

    # ====================================================================
    # Pilot onboarding import — rinks + ice slots commit (#95)
    # ====================================================================
    @catch
    def commit_rinks_ice_slots_import(self, sheets_csv: dict,
                                      actor_id: Optional[str] = None) -> dict:
        """Commit step 4 of the pilot onboarding import wizard.

        Parses the present ``rinks_csv``/``ice_slots_csv`` text (same shape
        as :meth:`get_import_dry_run`) and delegates to
        ``SetupService.commit_rinks_ice_slots_import``, which re-validates
        via the same pure ``validate_import`` gate before writing anything.
        Teams/players (#93) and officials/availability (#94) are out of
        scope here — reject the request outright rather than silently
        dropping operator-submitted data.
        """
        sheets_csv = sheets_csv or {}
        unsupported = [key for key in
                      ("teams_csv", "players_csv", "officials_csv",
                       "official_availability_csv")
                      if sheets_csv.get(key)]
        if unsupported:
            raise ValidationError(
                f"{', '.join(unsupported)} not supported by this commit "
                f"endpoint — see #93/#94.")

        sheets = {}
        for name in ("rinks", "ice_slots"):
            text = sheets_csv.get(f"{name}_csv")
            if not text:
                continue
            if not isinstance(text, str):
                raise ValidationError(f"{name}_csv must be a CSV text string.")
            sheets[name] = parse_csv_text(text)
        return self.setup.commit_rinks_ice_slots_import(
            sheets, actor_id=actor_id)
