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
from ..services.notifier import push as _push_notification
from ..store import InMemoryStore


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
    """Convert a domain dataclass to a fully JSON-safe dict."""
    return _jsonify(obj)


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
        one active admin, a reachable DB, current migrations, and cookie
        hardening. Non-sensitive: booleans + counts only."""
        production = (app_mode == "production")
        mig = self.store.migration_status()
        admins = self._active_admin_count()
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
        ]
        return {"ready": all(c["ok"] for c in checks),
                "app_mode": app_mode, "checks": checks}

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
                "path": f"/calendar/{t.actor_type}/{{token}}.ics"}

    @catch
    def create_calendar_feed_token(self, actor_type: str, actor_ref: str,
                                   label=None, actor_id=None) -> dict:
        """Issue a feed token for an actor and return the raw token ONCE
        (only its hash is stored). The caller builds the subscription URL."""
        if actor_type not in ACTOR_TYPES:
            raise ValidationError(f"Unknown actor_type '{actor_type}'.")
        if not actor_ref:
            raise ValidationError("An actor_ref is required.")
        raw = new_feed_token()
        tok = CalendarFeedToken(
            id=self.store.next_id("calfeed"), token_hash=hash_feed_token(raw),
            actor_type=actor_type, actor_ref=actor_ref,
            created_at=self.roster.clock(), label=label)
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
            rink = self.store.get_ice_slot(g.ice_slot_id) if g.ice_slot_id else None
            venue = None
            if rink is not None:
                rk = self.store.get_rink(rink.rink_id)
                venue = self.store.get_venue(rk.venue_id).name if (
                    rk and self.store.get_venue(rk.venue_id)) else None
            rows.append({
                "assignment_id": a.id, "game_id": a.game_id,
                "role": a.role.value, "status": a.status.value,
                "home_team_name": home.name if home else g.home_team_id,
                "away_team_name": away.name if away else None,
                "start_time": g.start_time.isoformat() if g.start_time else None,
                "rink": g.rink, "venue_name": venue, "cancelled": g.cancelled,
            })
        rows.sort(key=lambda r: r["start_time"] or "")
        return {"official_id": official_id, "assignments": rows}

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
        teams = [t for t in self.store.all_teams() if t.division_id == division_id]
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
        venue_name = None
        slot = self.store.get_ice_slot(g.ice_slot_id) if g.ice_slot_id else None
        if slot is not None:
            rink = self.store.get_rink(slot.rink_id) if slot.rink_id else None
            if rink is not None:
                venue = self.store.get_venue(rink.venue_id) if rink.venue_id else None
                venue_name = venue.name if venue else None
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
        leagues = self.store.all_leagues()
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
    def get_demo_overview(self) -> dict:
        """Assemble the League/Arena/Schedule/Public view for the E2E demo.

        The ``public`` section deliberately contains NO player names or any
        personal data — only fixture information that is safe to show fans.
        """
        divisions = {d.id: d for d in self.store.all_divisions()}
        clubs = {c.id: c for c in self.store.all_clubs()}
        teams = {t.id: t for t in self.store.all_teams()}
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

        division_rows = [
            {"id": d.id, "season_id": d.season_id, "name": d.name,
             "age_group": d.age_group, "is_junior": is_junior(d)}
            for d in divisions.values()
        ]
        team_rows = [
            {"id": t.id, "name": t.name, "club_id": t.club_id,
             "division_id": t.division_id,
             "club_name": clubs[t.club_id].name if t.club_id in clubs else None,
             "division_name": divisions[t.division_id].name
             if t.division_id in divisions else t.division}
            for t in teams.values()
        ]
        rink_rows = [
            {"id": r.id, "venue_id": r.venue_id, "name": r.name,
             "venue_name": venues[r.venue_id].name if r.venue_id in venues else None}
            for r in rinks.values()
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

        leagues = [_serialize(x) for x in self.store.all_leagues()]
        seasons = [_serialize(x) for x in self.store.all_seasons()]
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
            "divisions": division_rows,
            "clubs": [_serialize(c) for c in clubs.values()],
            "teams": team_rows,
            "venues": [_serialize(v) for v in venues.values()],
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
            "setup_audit": setup_audit,
            "setup_audit_count": len(setup_audit),
        }

    # ====================================================================
    # League + Arena setup
    # ====================================================================
    @catch
    def create_league(self, name: str, country: str = "", timezone: str = "UTC",
                      actor_id: Optional[str] = None) -> dict:
        return _serialize(self.setup.create_league(name, country, timezone, actor_id))

    @catch
    def list_leagues(self) -> List[dict]:
        return [_serialize(x) for x in self.setup.list_leagues()]

    @catch
    def create_season(self, league_id: str, name: str,
                      start_date: Optional[str] = None, end_date: Optional[str] = None,
                      actor_id: Optional[str] = None) -> dict:
        return _serialize(self.setup.create_season(
            league_id, name, _parse_dt(start_date, "start_date"),
            _parse_dt(end_date, "end_date"), actor_id))

    @catch
    def create_division(self, season_id: str, name: str, age_group: str = "",
                        actor_id: Optional[str] = None) -> dict:
        return _serialize(self.setup.create_division(season_id, name, age_group, actor_id))

    @catch
    def create_club(self, name: str, country: str = "",
                    actor_id: Optional[str] = None) -> dict:
        return _serialize(self.setup.create_club(name, country, actor_id))

    @catch
    def create_team(self, club_id: str, division_id: str, name: str,
                    actor_id: Optional[str] = None) -> dict:
        return _serialize(self.setup.create_team(club_id, division_id, name, actor_id))

    @catch
    def create_venue(self, name: str, address: str = "", timezone: str = "UTC",
                     actor_id: Optional[str] = None) -> dict:
        return _serialize(self.setup.create_venue(name, address, timezone, actor_id))

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
                      actor_id: Optional[str] = None) -> dict:
        return _serialize(self.setup.add_player(
            team_id, name, _parse_enum(Position, position, "position"),
            jersey_number, actor_id))

    @catch
    def create_game(self, season_id: str, division_id: str, home_team_id: str,
                    away_team_id: str, ice_slot_id: str, target_goalies: int = 1,
                    target_skaters: int = 15, max_skaters: int = 18,
                    allow_division_override: bool = False,
                    actor_id: Optional[str] = None) -> dict:
        return _serialize(self.setup.create_game(
            season_id, division_id, home_team_id, away_team_id, ice_slot_id,
            target_goalies, target_skaters, max_skaters,
            allow_division_override, actor_id))

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
