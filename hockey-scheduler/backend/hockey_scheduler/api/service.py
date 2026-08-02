"""API facade.

Each method maps 1:1 to an endpoint in docs/architecture/api-contract.md and
returns plain JSON-serializable dicts. Domain exceptions are caught and
returned as the structured ``{"error": {...}}`` shape so callers (and a future
web framework) never see Python tracebacks across the boundary.
"""

import copy
from dataclasses import asdict, is_dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Callable, List, Optional

from ..domain import (
    AvailabilityStatus,
    CalendarFeedToken,
    ContactDestination,
    DeliveryStatus,
    Game,
    GameType,
    DeviceToken,
    IceSlotStatus,
    IceSlotType,
    NotificationAudience,
    NotificationChannel,
    NotificationKind,
    NotificationPreference,
    NotificationRecipient,
    Permission,
    Role,
    ScheduleScenario,
    OfficialRole,
    ResultStatus,
    RosterEntryStatus,
    SeasonStatus,
    SlotType,
    SubstituteStatus,
    can,
    intervals_overlap,
)
from ..domain.errors import (
    ConcurrencyConflictError,
    DomainError,
    NotAuthorizedError,
    NotFoundError,
    ValidationError,
)
from ..services import (
    ACTOR_TYPES,
    AccountService,
    ContextService,
    FactoryResetService,
    GuardianService,
    DeliveryLoop,
    DeliveryWorker,
    RosterService,
    SetupService,
    build_ics,
    draft_schedule,
    draft_schedule_for_league,
    hash_feed_token,
    new_feed_token,
    parse_csv_text,
    validate_import,
    SCENARIO_PLANNER_VERSION,
    canonical_fingerprint,
    changed_snapshot_sections,
    material_input_snapshot,
    resolve_scenario_scope,
)
from ..services.scheduler import (
    _existing_pairing_games,
    raced_pairing_game_id,
    reviewed_existing_counts,
)
from ..services.league_scoped_scheduler import season_candidate_rink_ids
from ..services.league_scope import (
    require_slots_belong_to_locked_season,
    team_registration_valid,
)
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


class _SetupTargetRefused(Exception):
    """Internal signal (#369): a gated target is not this caller's to touch.

    Raised INSIDE ``setup_guarded_mutation``'s transaction purely so the
    transaction rolls back and the refusal provably leaves no row, no audit
    entry and no serialized field behind. Deliberately NOT a ``DomainError``:
    it carries the target's INDEX, never wire wording, so the transport layer
    keeps sole ownership of the generic ``"<Label> <id> not found."`` body that
    makes a refusal byte-identical to a nonexistent id."""

    def __init__(self, index: int):
        super().__init__(f"setup target {index} is outside the caller's scope")
        self.index = index


class _SetupMutationRefused(Exception):
    """Internal signal (#369): the MUTATION itself returned a domain error.

    The facade's ``@catch`` converts a DomainError into a dict at a level BELOW
    the guard's transaction, and a nested ``transaction()`` only joins — it does
    not roll back. So a swallowed error would otherwise let a half-applied
    mutation commit under an error response. Raising here restores exactly what
    the facade got when it owned the outermost transaction: error ⇒ rollback."""

    def __init__(self, payload: dict):
        super().__init__("mutation refused")
        self.payload = payload


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
        self.factory_reset = FactoryResetService(
            self.store, self.accounts, self.roster.clock)
        self.guardians = GuardianService(self.store, self.roster.clock)
        self.context = ContextService(self.store, self.roster.clock)

    # -- active Program/Season/League context (#159, League axis #345) ------
    def _context_view(self, program, season, league=None) -> dict:
        """Render the *exact* Program/Season/League the service validated in one
        transactional snapshot — never a re-fetch. Each object is serialized
        exactly ONCE, and every payload field (``program_id``/``season_id``/
        ``league_id`` and ``read_only``) is derived from that single serialized
        DTO, so they can never disagree: a non-null id always has its object, and
        ``read_only`` always matches the serialized Season ``status``. The service
        also hands back objects detached from the store's live rows, so even a
        concurrent in-place archive/reopen cannot mutate them between these
        reads (#159).

        ``league`` is the third axis (#345), added ADDITIVELY: it defaults to
        None so the Program/Season rendering is byte-identical when no League is
        selected, and ``league_id``/``league`` are simply null then. A null
        League is a first-class state (Program-only, and Season-without-League),
        never an error — so the two new keys are ALWAYS present, and only their
        value varies."""
        program_dto = _serialize(program) if program else None
        season_dto = _serialize(season) if season else None
        league_dto = _serialize(league) if league else None
        read_only = (season_dto is not None
                     and season_dto.get("status") == SeasonStatus.ARCHIVED.value)
        return {
            "program_id": program_dto["id"] if program_dto else None,
            "season_id": season_dto["id"] if season_dto else None,
            "league_id": league_dto["id"] if league_dto else None,
            "read_only": read_only,
            "program": program_dto,
            "season": season_dto,
            "league": league_dto,
        }

    @catch
    def get_active_context(self, user_id, role, scope) -> dict:
        """The caller's active context, all three axes (#159, League #345).

        Resolved through ``resolve_with_league``, whose Program/Season half is
        documented to be exactly what the two-axis ``resolve`` returns for the
        same arguments — so this stays a pure ADDITION to the payload, not a
        change to the existing two axes. Deliberately ONE method rather than a
        separate League-aware variant: a non-League read would have to render
        ``league_id: null`` even for a user who HAS a League saved, which is
        worse than not exposing it at all. All three come from one serializable
        snapshot, so the League can never contradict the Program/Season beside
        it."""
        program, season, league = self.context.resolve_with_league(
            user_id, role, scope)
        return self._context_view(program, season, league)

    @catch
    def set_active_context(self, user_id, role, scope,
                           program_id, season_id, league_id=None) -> dict:
        """Record a Program/Season(/League) selection.

        ``league_id`` is appended LAST and defaults to None (#345) so the
        pre-existing five-positional-argument callers keep their exact meaning —
        their fourth/fifth arguments stay program_id/season_id and can never be
        silently reinterpreted. Passing None routes to the same written row the
        two-axis ``ContextService.set`` produced (``league_id`` NULL), so the
        legacy behavior — including CLEARING a previously-saved League rather
        than carrying it onto a Program/Season it was not chosen for — is
        preserved exactly."""
        program, season, league = self.context.set_with_league(
            user_id, role, scope, program_id, season_id, league_id)
        return self._context_view(program, season, league)

    def _season_option(self, season) -> dict:
        """A Season as the switcher needs it: id + name + lifecycle, with the
        derived ``read_only`` for an archived (historical) Season."""
        dto = _serialize(season)
        return {
            "id": dto["id"],
            "name": dto["name"],
            "start_date": dto.get("start_date"),
            "status": dto.get("status"),
            "read_only": dto.get("status") == SeasonStatus.ARCHIVED.value,
        }

    def _league_option(self, league) -> dict:
        """A League as the switcher needs it: id + name. No ``read_only`` — that
        is a property of the SEASON's lifecycle, not of the permanent League."""
        dto = _serialize(league)
        return {"id": dto["id"], "name": dto["name"]}

    @catch
    def get_context_options(self, user_id, role, scope) -> dict:
        """The AUTHORIZED Program/Season/League options for the context switcher
        (#159, League axis #345), filtered through the SAME scope rules as
        get/set — never the unfiltered overview, so a scoped account can neither
        select nor enumerate an unrelated context. Each Program lists only its
        authorized Seasons (active + archived-as-read-only) and its authorized
        Leagues, and is itself Program-only-selectable. ``selected`` is the
        current resolved context, guaranteed to be one of these options.

        ``leagues`` is Program-scoped, not Season-scoped — deliberately, and
        documented on ``ContextService.options_with_league``: a League that
        exists under the Program but is not bound to the currently-selected
        Season is still OFFERED, because selecting it is a legitimate way to move
        to a Season+League pair. The binding requirement is enforced at selection
        time, where it can report a precise reason, rather than silently by
        omission here."""
        programs, sel_program, sel_season, sel_league = (
            self.context.options_with_league(user_id, role, scope))
        return {
            "programs": [{
                "id": _serialize(program)["id"],
                "name": _serialize(program)["name"],
                "seasons": [self._season_option(s) for s in seasons],
                "leagues": [self._league_option(lg) for lg in leagues],
            } for (program, seasons, leagues) in programs],
            "selected": {
                "program_id": sel_program.id if sel_program else None,
                "season_id": sel_season.id if sel_season else None,
                "league_id": sel_league.id if sel_league else None,
                "read_only": (sel_season is not None
                              and sel_season.status == SeasonStatus.ARCHIVED),
            },
        }

    # -- setup TARGET-RECORD authorization (#369) --------------------------
    #
    # Holding a setup CAPABILITY (MANAGE_SETUP / MANAGE_ARENA) says the caller
    # may use a setup verb. It says nothing about WHICH records that verb may
    # touch. The two are checked in different places and must both pass:
    # `web/authz.py` gates the role, and this predicate gates the target record.
    #
    # `entity_type` names in the SETUP AUDIT TRAIL are historical, not canonical
    # (see `_SETUP_CREATION_AUDIT_KEYS`). Only these CANONICAL kind strings are
    # accepted here; a transport-layer alias ("league" meaning a Program on the
    # v1 route, "level" meaning a League, the hyphenated v2 "ice-slot" /
    # "league-season") MUST be translated by its caller. Hyphens are folded to
    # underscores as a convenience; nothing else is guessed, because guessing
    # between the two meanings of "league" is exactly how #369's sibling defects
    # were introduced.
    _SETUP_TARGET_KINDS = frozenset({
        "program", "season", "league", "league_season", "division", "team",
        "player", "game", "club", "official", "venue", "rink", "ice_slot",
        "organization",
    })

    # Join rows that carry NO Program of their own: a SeasonTeamRegistration IS
    # the (Team, LeagueSeason) join and a SeasonVenueAccess IS the (Season,
    # Venue) join, so neither is a `_SETUP_TARGET_KINDS` member. Each is judged
    # by the parent that names its Program, while still being REPORTED under its
    # own id and label by the transport layer. Maps pseudo-kind → (the plain
    # store getter, the ROW-LOCKING one, the attribute holding the parent id,
    # the canonical kind of that parent).
    #
    # #369 review: this lookup used to live in `web/server.py` and ran OUTSIDE
    # even the authorization transaction, so the parent a request was judged
    # against could be re-pointed before the mutation ran. It is domain
    # knowledge, not transport, and the authoritative pass belongs inside the
    # one transaction — hence the second, *_for_update getter, which reads the
    # bridge row and locks it in a single statement. The plain getter is for the
    # unlocked preflight only.
    _SETUP_BRIDGE_TARGETS = {
        "registration": ("get_season_team_registration",
                         "get_season_team_registration_for_update",
                         "league_season_id", "league_season"),
        "season_venue_access": ("get_season_venue_access",
                                "get_season_venue_access_for_update",
                                "season_id", "season"),
    }

    # Canonical kind → the store getter that reads the row AND takes its write
    # lock (`SELECT ... FOR UPDATE` on PostgreSQL; the process-wide lock already
    # covers Memory/SQLite). `setup_guarded_mutation` takes these before it
    # authorizes, so the authorization snapshot is established no earlier than
    # the lock and nothing that touches a named row can commit between the
    # decision and the write.
    _SETUP_TARGET_LOCKS = {
        "program": "get_program_for_update",
        "season": "get_season_for_update",
        "league": "get_league_for_update",
        "league_season": "get_league_season_for_update",
        "division": "get_division_for_update",
        "team": "get_team_for_update",
        "player": "get_player_for_update",
        "game": "get_game_for_update",
        "club": "get_club_for_update",
        "official": "get_official_for_update",
        "venue": "get_venue_for_update",
        "rink": "get_rink_for_update",
        "ice_slot": "get_ice_slot_for_update",
        "organization": "get_organization_for_update",
    }

    # (action, entity_type) pairs a "this user created this record" audit row
    # can legitimately carry, per canonical kind. NOT simply
    # f"{kind}_created" — the trail predates #233's vocabulary and was never
    # rewritten:
    #
    #   * `create_program` writes ("league_created", "league") against the
    #     PROGRAM's id, because pre-#233 "League" meant today's Program. A
    #     lookup for ("program_created", "program") finds NOTHING for every
    #     interactively-created Program.
    #   * `upsert_imported_program` (the hierarchy import) DOES write
    #     ("program_created", "program"), so both spellings are live and a
    #     Program may legitimately carry either.
    #   * `create_league` writes ("level_created", "level") against the
    #     competition LEAGUE's id — "level" was the pre-#283 name — while
    #     `upsert_imported_league` writes ("league_created", "league") against
    #     that same kind of id. So ("league_created", "league") is ambiguous
    #     BETWEEN Program and League on its face.
    #   * `add_player` — the INTERACTIVE Player create, i.e. every Player not
    #     imported — writes ("player_added", "player"); only the CSV import's
    #     `upsert_imported_player` writes ("player_created", "player"). Both
    #     spellings are live, exactly like the Program pair above, so both are
    #     listed. Omitting "player_added" made `_setup_target_created_by`
    #     answer False for every hand-entered Player, which fails CLOSED today
    #     (a Player is always bound to a Team, so its chain never empties and
    #     rule 6 is unreachable) but would deny an operator their own record
    #     the moment an unbound Player became representable.
    #
    # The ambiguity is resolved by `entity_id`, which is always matched too:
    # `next_id` hands out one monotonic sequence per prefix, and both
    # `create_program` and `create_league` draw from the SAME "league" prefix,
    # so a Program id and a League id can never be equal. Matching the id (not
    # just the action) is therefore what makes the overlapping row safe.
    _SETUP_CREATION_AUDIT_KEYS = {
        "program": {("league_created", "league"),
                    ("program_created", "program")},
        "league": {("level_created", "level"), ("league_created", "league")},
        "season": {("season_created", "season")},
        "league_season": {("league_season_created", "league_season")},
        "division": {("division_created", "division")},
        "team": {("team_created", "team")},
        "player": {("player_added", "player"), ("player_created", "player")},
        "game": {("game_created", "game")},
        "club": {("club_created", "club")},
        "official": {("official_created", "official")},
        "venue": {("venue_created", "venue")},
        "rink": {("rink_created", "rink")},
        "ice_slot": {("ice_slot_created", "ice_slot")},
        "organization": {("organization_created", "organization")},
    }

    def _setup_target_record(self, kind, record_id):
        """The stored record for a canonical setup ``kind``, or None.

        None means "no such record" for EVERY reason — unknown kind, falsy id,
        or a genuinely absent row — because the caller renders one generic
        not-found for all of them. An unrecognized kind failing closed here is
        deliberate: a mis-spelled or untranslated kind must refuse, never
        silently skip the gate."""
        if not record_id or not isinstance(record_id, str):
            return None
        getter = {
            "program": self.store.get_program,
            "season": self.store.get_season,
            "league": self.store.get_league,
            "league_season": self.store.get_league_season,
            "division": self.store.get_division,
            "team": self.store.get_team,
            "player": self.store.get_player,
            "game": self.store.get_game,
            "club": self.store.get_club,
            "official": self.store.get_official,
            "venue": self.store.get_venue,
            "rink": self.store.get_rink,
            "ice_slot": self.store.get_ice_slot,
            "organization": self.store.get_organization,
        }.get(kind)
        return getter(record_id) if getter else None

    # -- the LINK TRIPLE ("edge") every chain walk below produces ----------
    #
    # #369 re-review. The first cut of this gate resolved all three axes and
    # then THREW TWO AWAY: it reduced every record to a set of Program ids and
    # authorized on `program.id in program_ids`. With Program P / Season S /
    # League A persisted, `POST /api/v2/setup/player/<League-B-player>/update`
    # returned 200 and renamed a Player in a League the caller had not
    # selected — a same-Program, cross-League IDOR — and the identical
    # construction let a Season-A caller mutate Season-B rows.
    #
    # So a chain walk no longer yields Program ids. It yields EDGES: one
    # ``(program_id, season_id | None, league_id | None)`` triple per real link
    # the record carries. A ``None`` component means "this link genuinely names
    # no such axis" — a permanent League has no Season, a Venue has no League —
    # NOT "unknown" and NOT "any". Nothing is invented: the facility tree
    # (Organization → Venue → Rink → IceSlot) reaches the competition tree only
    # through ``SeasonVenueAccess``, which names a Season and never a League, so
    # a facility edge's League component is always None.
    #
    # Judging EDGE BY EDGE rather than by three independent unions is the point.
    # A record with several links (a Club owning Teams in two Leagues, an
    # Organization that both operates a Program and owns a granted Venue) must
    # be judged by whether ONE WHOLE link matches the caller's context, never by
    # a cross product of components taken from different links — that cross
    # product would authorize a (Season S, League B) context against a record
    # whose only links are (S, A) and (S', B).
    #
    # ``saw_link`` keeps its exact pre-existing meaning: a linking row existed
    # even if it could not be resolved, so corrupt data can never demote a
    # linked record into the permissive creator branch.

    def _team_edges(self, team):
        """Link edges for a Team: ``(program, None, league)``.

        ``Team.program_id`` is the permanent owner and is authoritative whenever
        it is set. It is NULLABLE, though, and a Team also belongs permanently to
        exactly one competition ``League`` whose ``program_id`` names the same
        Program (the service layer enforces the invariant). So when — and ONLY
        when — ``program_id`` is null, the League edge is walked instead for the
        PROGRAM component: that Team is still part of a Program's competition
        chain, and the owner's rule is that a LINKED record is judged by its
        chain, not by who typed it in. The League is never consulted FOR THE
        PROGRAM COMPONENT while ``program_id`` is set — but it IS resolved and
        checked for AGREEMENT (#369 re-review 2): the two columns are
        independent FKs with no database constraint tying the League's Program
        to ``Team.program_id``, so legacy data, imports or a prior partial
        write can persist a Team whose ``league_id`` names a League in ANOTHER
        Program. Emitting that pair as one edge lets an explicit-No-League
        caller (whose League comparison is legitimately skipped) mutate a
        record whose persisted graph crosses the Program ceiling. A found-but-
        disagreeing pair is therefore LINKED BUT UNAUTHORIZED — ``(set(),
        True)`` — never an edge and never creator-claimable.

        The LEAGUE component is ``Team.league_id`` verbatim — the REAL
        competition League (never ``Venue.league_id``, which is a Program id
        under a legacy name). It is taken UNRESOLVED into the edge on purpose:
        a dangling League id can then never equal the caller's selected League,
        so it fails closed under a specific League instead of decaying into "no
        League axis", while under No League (the League comparison skipped) the
        Team stays manageable by its authoritative Program. A Team with no
        League at all is a real, supported state (#345: "Teams with a Program
        but no League yet"), and it carries no League axis to check — refusing
        it whenever any League is selected would make a league-less Team
        permanently unmanageable.

        The disagreement check is therefore SURGICAL: it fires ONLY when the
        League RESOLVES to a real League in ANOTHER Program (the owner's exact
        repro: ``program_id`` = A, ``league_id`` → a valid League in B). A
        merely dangling id is not a cross-Program handle — it points at nothing
        — so it is left to the verbatim-edge behavior above rather than
        rejected outright, which keeps a program-owned Team with broken League
        data repairable instead of permanently frozen.

        The Season axis is deliberately ABSENT: a Team is PERMANENT. Its
        participation in a Season is a separate SeasonTeamRegistration record,
        which carries the Season axis itself. Deriving a Season for the Team
        from its registrations would make a newly created, not-yet-registered
        Team unmanageable in every context.
        """
        if team is None:
            return set(), False
        league_id = team.league_id or None
        if team.program_id:
            if league_id is not None:
                league = self.store.get_league(league_id)
                if (league is not None and league.program_id
                        and league.program_id != team.program_id):
                    # #369 re-review 2: independently persisted axes DISAGREE —
                    # the named League resolves into another Program. Linked but
                    # unauthorized, so No-League union semantics can't reach it.
                    return set(), True
            return {(team.program_id, None, league_id)}, True
        if team.league_id:
            league = self.store.get_league(team.league_id)
            if league is None:
                return set(), True          # dangling edge → linked, unresolvable
            if league.program_id:
                return {(league.program_id, None, league_id)}, True
        return set(), False

    def _game_parent_constraints(self, game):
        """``(constraints, broken)`` — the ``(program, season, league)`` each
        NON-NULL Game parent names, every one resolved INDEPENDENTLY.

        #372. The previous cut resolved ONE League for a Game by PRECEDENCE —
        the explicit ``league_id``, else via ``league_season_id``, else via
        ``division_id`` — and the gate then validated only that single winner.
        A Game carries FOUR independent, nullable foreign keys (``season_id``,
        ``league_id``, ``league_season_id``, ``division_id``) and the schema
        ties none of them to any other, so a legitimate Program-A ``league_id``
        HID the rest: a ``league_season_id`` or ``division_id`` pointing into
        Program B was never examined and the Game was authorized. First-match
        precedence is the wrong instrument for an authorization decision —
        every parent that is really persisted is really reachable (the
        scheduler, the standings walk and the league-scope helpers each follow
        a DIFFERENT one of these columns), so every one of them must be judged.

        What each non-null parent contributes:

        * ``season_id``        → its Season's Program, and that Season.
        * ``league_id``        → that League's Program, and that League.
        * ``league_season_id`` → BOTH ends of the binding, as two SEPARATE
          constraints: its Season (Program + Season) and its League (Program +
          League). A binding whose own two ends disagree is therefore caught by
          the very same fold as any other disagreeing pair, with no extra rule.
        * ``division_id``      → its LeagueSeason, judged identically.

        A NULL parent contributes NOTHING. It names no axis, and inventing one
        would refuse both real supported states this file already protects: the
        Season-less exhibition (#283 Slice D) and the not-yet-parented draft.
        This is the "no invented League axis" rule, unchanged.

        ``broken`` is True when a non-null parent does NOT resolve — a dangling
        id, or a Season/League row carrying no Program. That is exactly the
        ``saw_link`` treatment used throughout this file: the link exists, it
        cannot be judged, so it fails closed. Silently skipping an unresolvable
        parent instead would make CORRUPTING the graph (or racing a delete) a
        way to erase a constraint, i.e. a privilege gain earned by breaking
        data rather than by holding rights."""
        constraints, broken = [], False
        bindings = []                        # LeagueSeason ids still to judge

        season_id = getattr(game, "season_id", None) or None
        league_id = getattr(game, "league_id", None) or None
        league_season_id = getattr(game, "league_season_id", None) or None
        division_id = getattr(game, "division_id", None) or None

        if season_id:
            season = self.store.get_season(season_id)
            if season is None or not season.program_id:
                broken = True
            else:
                constraints.append((season.program_id, season.id, None))

        if league_id:
            league = self.store.get_league(league_id)
            if league is None or not league.program_id:
                broken = True
            else:
                constraints.append((league.program_id, None, league.id))

        if league_season_id:
            bindings.append(league_season_id)

        if division_id:
            # A Division is a child of a LeagueSeason, so it imposes precisely
            # the constraints that binding does — no more, no less.
            division = self.store.get_division(division_id)
            if division is None or not division.league_season_id:
                broken = True
            else:
                bindings.append(division.league_season_id)

        for binding_id in bindings:
            ls = self.store.get_league_season(binding_id)
            if ls is None:
                broken = True
                continue
            season = (self.store.get_season(ls.season_id)
                      if ls.season_id else None)
            if season is None or not season.program_id:
                broken = True
            else:
                constraints.append((season.program_id, season.id, None))
            league = (self.store.get_league(ls.league_id)
                      if ls.league_id else None)
            if league is None or not league.program_id:
                broken = True
            else:
                constraints.append((league.program_id, None, league.id))

        return constraints, broken

    def _venue_program_ids(self, venue):
        """Program ids a Venue is linked to — the Program component of
        :meth:`_venue_edges`, for the venue-grant EXCEPTION only.

        The exception deliberately judges the Program axis and nothing else (a
        shared arena is grantable across Programs), so it reads the edges'
        Program component rather than re-walking the Venue itself."""
        edges, saw_link = self._venue_edges(venue)
        return {edge[0] for edge in edges}, saw_link

    def _venue_edges(self, venue):
        """Link edges for a Venue — the union of BOTH bridges, and NO League.

        1. ``SeasonVenueAccess`` is the only real join between the facility tree
           (Organization → Venue → Rink → IceSlot) and the competition tree
           (Program → Season → League → Division). It names a SEASON, so each
           grant is a ``(program, season, None)`` edge — the facility tree has a
           Season axis and no League axis whatever, and none is invented here.
           EVERY grant counts, ACTIVE OR REVOKED: revoking deactivates the row
           rather than deleting it, so a revoked grant is durable evidence that
           this Venue was used by that Program's Season. Treating a revoked
           grant as "no link" would let a Program revoke its own access and
           thereby turn a shared Venue into an unlinked record that some
           unrelated account could claim by creator ownership — a privilege
           *gain* produced by giving something up.
        2. ``Venue.league_id`` is the LEGACY pre-#233 bridge and, despite the
           name, holds a PROGRAM id — `store/integrity_checks.py` joins it to
           ``seasons.program_id``. It is NEVER a competition League id, and it
           names no Season, so it is a ``(program, None, None)`` edge. It is
           taken verbatim and is deliberately NOT filtered against existing
           Programs: a dangling legacy value must keep the Venue LINKED (and so
           refuse everyone) rather than decay into "unlinked" and become
           claimable.

        Returns ``(edges, saw_link)``; ``saw_link`` is True when a linking row
        existed even if its Program could not be resolved, so corrupt data can
        never demote a linked record into the permissive creator branch."""
        if venue is None:
            return set(), False
        edges, saw_link = set(), False
        for grant in self.store.season_venue_access_for_venue(venue.id):
            saw_link = True                 # active or revoked — both are links
            season = self.store.get_season(grant.season_id)
            if season is not None and season.program_id:
                edges.add((season.program_id, season.id, None))
        if venue.league_id:
            saw_link = True
            # LEGACY: this IS a Program id, and it binds no Season.
            edges.add((venue.league_id, None, None))
        return edges, saw_link

    def _setup_target_edges(self, kind, record):
        """``(edges, saw_link)`` — every ``(program, season, league)`` link
        ``record`` carries. See the block comment above for what an edge is and
        why the three axes must travel together.

        ``saw_link`` distinguishes "this record has no chain at all" (empty set,
        False → the pending-link/creator contract applies) from "this record HAS
        a chain but a row in it is missing" (empty set, True → refuse). Without
        that distinction a dangling FK would silently promote a linked record
        into the permissive branch, which is a privilege escalation reachable by
        breaking data rather than by holding rights.

        Which axes each kind really carries:

        ==================  =======  =========================  ==============
        kind                Program  Season                     League
        ==================  =======  =========================  ==============
        program             itself   —                          —
        season              parent   ITSELF                     —
        league              parent   — (permanent across        ITSELF
                                     Seasons)
        league_season       season   its Season                 its League
        division            chain    its LeagueSeason's Season  ditto League
        team                own/     — (permanent; the Season   Team.league_id
                            League   lives on its registration)
        player              its Team — (as Team)                as its Team
        game                all its  all its parents' Seasons   all its
                            parents  — they must AGREE (#372)   parents'
                                                                Leagues
        club                Teams    — (as Teams)               its Teams'
        official            club +   its Games' Seasons         club + Games'
                            Games
        venue               grants + its grants' Seasons        — (facility
                            legacy                              tree: none)
        rink                its      as its Venue               —
                            Venue
        ice_slot            its Rink as its Rink                —
        organization        operated its Venues' Seasons        —
                            + Venues
        ==================  =======  =========================  ==============

        The two BRIDGE rows the transport layer gates (SeasonTeamRegistration,
        SeasonVenueAccess) carry no chain of their own and are judged by the
        parent that names one — a registration by its LeagueSeason (Program +
        Season + League), a venue-access grant by its Season (Program + Season,
        and no League, because the facility join has none)."""
        edges, saw_link = set(), False

        if kind == "program":
            return {(record.id, None, None)}, True

        if kind == "season":
            if record.program_id:
                return {(record.program_id, record.id, None)}, True
            return edges, saw_link

        if kind == "league":
            # The REAL competition League — its parent is a Program directly.
            # A League is PERMANENT (it outlives every Season it plays in), so
            # it carries no Season axis; its per-Season participation is the
            # separate LeagueSeason row, which does.
            if record.program_id:
                return {(record.program_id, None, record.id)}, True
            return edges, saw_link

        if kind == "league_season":
            season = self.store.get_season(record.season_id)
            if season is None:
                return edges, True
            if not season.program_id:
                return edges, True
            # #369 re-review 2: ``season_id`` and ``league_id`` are independent
            # FKs — nothing in the schema forces the named League to belong to
            # the same Program as the named Season. When the League RESOLVES
            # into a DIFFERENT Program (the owner's repro: an A-Season
            # LeagueSeason whose league_id names a valid B League), the No-League
            # union — which rightly skips the League comparison — would otherwise
            # authorize a graph that crosses the Program ceiling; reject it as
            # linked-but-unauthorized. A DANGLING league_id is left in the edge
            # verbatim (as the Team edge does): it is not a cross-Program handle,
            # it fails closed under a selected League, and leaving it judged by
            # its Season keeps a corrupt-but-own-Program registration
            # REPAIRABLE (the ``registration_league_not_in_season`` fix flow
            # relies on exactly this). A Division inherits this verdict through
            # its delegation below.
            if record.league_id:
                league = self.store.get_league(record.league_id)
                if (league is not None and league.program_id
                        and league.program_id != season.program_id):
                    return edges, True      # real cross-Program disagreement
            return ({(season.program_id, season.id,
                      record.league_id or None)}, True)

        if kind == "division":
            ls = self.store.get_league_season(record.league_season_id)
            if ls is None:
                return edges, True
            return self._setup_target_edges("league_season", ls)

        if kind == "team":
            return self._team_edges(record)

        if kind == "player":
            team = self.store.get_team(record.team_id) if record.team_id else None
            if record.team_id and team is None:
                return edges, True
            return self._team_edges(team)

        if kind == "game":
            # EVERY non-null parent, judged INDEPENDENTLY, failing closed on
            # ANY disagreement (#372).
            #
            # A Game is the one setup record with four independent parent FKs,
            # and the earlier gate validated only the highest-precedence League
            # SOURCE: a valid Program-A ``league_id`` masked a
            # ``league_season_id`` or ``division_id`` in Program B — or in a
            # different Season of the same Program — and the row answered 200.
            # Two independently persisted parents that disagree mean this row's
            # parent graph STRADDLES the ceiling. That is linked-but-
            # UNAUTHORIZED: never an edge, so the No-League union (whose League
            # comparison is legitimately skipped) can never rescue it, and
            # never creator-claimable either.
            #
            # The Program and Season axes are the ceiling itself. The League
            # axis is folded on the same terms because a Game's League sources
            # are REDUNDANT by construction rather than independent facts —
            # ``league_season_id`` is documented as "its single source of
            # competition identity, so season_id and league_id can never drift
            # apart", and `move`/`publish` already reject the drift outright
            # (``game_league_season_mismatch``). Two parents naming DIFFERENT
            # Leagues inside one Program/Season is the same cross-League IDOR
            # #369's re-review closed for Players, reached through a Game.
            #
            # A NULL parent constrains nothing, so a Season-less exhibition
            # (#283 Slice D) still genuinely has no Season and no League axis
            # is invented for it.
            constraints, broken = self._game_parent_constraints(record)
            if broken:
                # A non-null parent that does not resolve: linked, unjudgeable.
                return edges, True
            if not constraints:
                # No parent at all — a genuinely unparented draft. The
                # pending-link/creator contract applies, exactly as before.
                return edges, saw_link
            programs = {axes[0] for axes in constraints}
            seasons = {axes[1] for axes in constraints if axes[1] is not None}
            leagues = {axes[2] for axes in constraints if axes[2] is not None}
            if len(programs) > 1 or len(seasons) > 1 or len(leagues) > 1:
                return edges, True           # the parents DISAGREE
            return ({(programs.pop(), next(iter(seasons), None),
                      next(iter(leagues), None))}, True)

        if kind == "club":
            # A Club owns Teams across Programs and Leagues; its links are its
            # Teams' links, EDGE BY EDGE. A Club with no Team is genuinely
            # unlinked.
            for team in self.store.all_teams():
                if team.club_id == record.id:
                    team_edges, team_link = self._team_edges(team)
                    edges |= team_edges
                    saw_link = saw_link or team_link
            return edges, saw_link

        if kind == "official":
            # Two independent links: the home Club used for conflict-of-interest
            # checks (Program + League, no Season), and the Games actually
            # officiated (which carry a Season too).
            if record.home_club_id:
                club = self.store.get_club(record.home_club_id)
                if club is None:
                    saw_link = True
                else:
                    club_edges, club_link = self._setup_target_edges(
                        "club", club)
                    edges |= club_edges
                    saw_link = saw_link or club_link
            for assignment in self.store.assignments_for_official(record.id):
                game = self.store.get_game(assignment.game_id)
                if game is None:
                    saw_link = True
                    continue
                game_edges, game_link = self._setup_target_edges("game", game)
                edges |= game_edges
                saw_link = saw_link or game_link
            return edges, saw_link

        if kind == "venue":
            return self._venue_edges(record)

        if kind == "rink":
            venue = self.store.get_venue(record.venue_id) if record.venue_id else None
            if record.venue_id and venue is None:
                return edges, True
            return self._venue_edges(venue)

        if kind == "ice_slot":
            rink = self.store.get_rink(record.rink_id) if record.rink_id else None
            if record.rink_id and rink is None:
                return edges, True
            if rink is None:
                return edges, saw_link
            return self._setup_target_edges("rink", rink)

        if kind == "organization":
            # A facility Organization reaches the competition tree two ways: the
            # Programs it OPERATES — a whole-Program link that names no Season —
            # and the Programs its Venues serve, which carry their Venue's
            # Season edges verbatim.
            for program in self.store.all_programs():
                if program.operator_organization_id == record.id:
                    edges.add((program.id, None, None))
                    saw_link = True
            for venue in self.store.all_venues():
                if venue.organization_id == record.id:
                    venue_edges, venue_link = self._venue_edges(venue)
                    edges |= venue_edges
                    saw_link = saw_link or venue_link
            return edges, saw_link

        return edges, saw_link

    @staticmethod
    def _setup_target_edge_allows(edge, program, season, league):
        """Does this ONE link put the record inside the caller's whole context?

        The three comparisons the first cut of #369 was missing two of. Each
        component is checked against the axis the caller has actually
        persisted, and a link is only satisfied when EVERY axis it names is:

        * **Program** — the hard ceiling, always compared. A record whose link
          names another Program is out of scope no matter what else matches.
        * **Season** — compared whenever the link names one. ``season is None``
          (a Program-only selection) therefore FAILS CLOSED against a
          Season-bound link: nothing has been validated to compare against, and
          a Program-only caller must not reach Season-bound rows.
        * **League** — compared whenever the link names one AND a League is
          selected. An explicit NO LEAGUE (``league is None``) is a first-class
          selection meaning the approved Program + active-Season union, so it
          permits every League inside that already-validated Program/Season and
          nothing outside it. A link that names no League (the whole facility
          tree, a permanent League-less Team) has no League axis to check —
          inventing one would refuse records that legitimately have none.
        """
        program_id, season_id, league_id = edge
        if program_id != program.id:
            return False
        if season_id is not None and (season is None
                                      or season_id != season.id):
            return False
        if (league_id is not None and league is not None
                and league_id != league.id):
            return False
        return True

    def setup_venue_grantable(self, venue_id, user_id, role, scope):
        """The FACILITY-TREE EXCEPTION for the venue-access write (#369 ruling).

        ``setup_target_accessible`` is the right rule for every other target,
        but applying it to the Venue end of a venue-access grant DEADLOCKS the
        shared-arena capability: an arena serves several leagues, and the moment
        Program A grants itself access the Venue is linked to A, so it fails the
        active-context check for every other Program -- which can then never
        obtain the grant that would have made it accessible. The capability
        fails on its own first use.

        The owner's ruling: the ceiling governs the COMPETITION tree, while a
        Venue stays grantable across Programs. So this predicate replaces the
        generic one for that ONE argument, and nothing else -- the Season end of
        the same grant stays strictly ceilinged, and Rinks, IceSlots and every
        competition record keep the generic rule.

        Grantable means:

        * the Venue is linked to SOME Program (any grant, active or revoked, or
          the legacy Program link) -- an established facility; or
        * it is linked to nothing AND this caller created it -- its own
          create-then-grant draft.

        The second clause is the load-bearing one. "Any Venue at all" was the
        first attempt and leaked: on an installation with no Programs yet it
        offered one operator the other's never-linked arena by name. An unlinked
        Venue is somebody's private draft and stays creator-only.
        """
        if role is None:
            return None
        program, _season, _league = self.context.resolve_with_league(
            user_id, role, scope)
        if program is None:
            return False
        venue = self.store.get_venue(venue_id) if venue_id else None
        if venue is None:
            return False
        # `_venue_program_ids` returns (ids, saw_link) -- unpack it. Testing
        # the 2-tuple directly is ALWAYS truthy, which made this "any Venue at
        # all" and silently reinstated the exact leak the docstring above
        # records: on a Program-less install one operator was offered the
        # other's never-linked arena by name.
        program_ids, saw_link = self._venue_program_ids(venue)
        if program_ids:
            return True                      # an established shared facility
        if saw_link:
            # A link that exists but does not resolve (a dangling season or
            # Program reference) is NOT "unlinked" -- treating it as such would
            # hand a corrupt row to the creator clause below. Fail closed.
            return False
        return self._setup_target_created_by("venue", venue.id, user_id)

    def setup_league_in_active_program(self, league_id, user_id, role, scope):
        """The #367 CREATE-side rule: does ``league_id`` belong to the caller's
        ACTIVE Program? True / False / None (no identity — do not gate).

        The write-side half of scoping the setup hierarchy. The read fix stops
        another Program's Leagues being OFFERED; this stops them being
        ACCEPTED, which is the part that actually matters — the id arrives in a
        request body and a client can send any value it likes.

        Deliberately binds to the ACTIVE Program rather than the caller's whole
        authorized set: a global League Admin is authorized for every Program,
        so an authorization-only check would not stop the reported case (an
        operator working in Program B creating into Program A). You create where
        you are working; switching Program is the context bar's job.

        Lives here, beside the other two target rules, so
        ``setup_guarded_mutation`` can run it under the same lock and inside the
        same transaction as the create it guards (#369 re-review): resolving the
        context and reading the League in one transaction and then creating in
        another is the very check/use gap that was the blocker."""
        if role is None:
            return None
        program, _season, _league = self.context.resolve_with_league(
            user_id, role, scope)
        if program is None:
            return False
        league = self.store.get_league(league_id) if league_id else None
        return league is not None and league.program_id == program.id

    def _setup_target_created_by(self, kind, record_id, user_id) -> bool:
        """True when ``user_id`` is the recorded CREATOR of this record.

        Reads the setup audit trail through ``_SETUP_CREATION_AUDIT_KEYS``,
        matching the (action, entity_type) pair AND the entity id AND the actor
        — see that map for why f"{kind}_created" alone is wrong and why the id
        is what disambiguates the one overlapping pair. A falsy ``user_id``
        (an unauthenticated or identity-less caller) owns nothing, ever."""
        if not user_id:
            return False
        keys = self._SETUP_CREATION_AUDIT_KEYS.get(kind)
        if not keys:
            return False
        for row in self.store.all_setup_audit():
            if (row.entity_id == record_id
                    and row.actor_id == user_id
                    and (row.action, row.entity_type) in keys):
                return True
        return False

    def setup_target_accessible(self, kind, record_id, user_id, role, scope):
        """May this caller act on THIS setup record? True / False / None.

        #369. The blocker this closes: an Arena Manager POSTed
        ``/api/v2/setup/venue/<foreign-id>/delete`` for a Venue created by a
        different account in a different Program. The route returned 200, echoed
        the foreign record's own fields back (including its name — an
        information leak on top of the write), deleted it, and wrote a
        ``venue_deleted`` audit row. Nothing was wrong with the role gate: an
        Arena Manager genuinely may delete Venues. What was missing is any check
        of the TARGET. *Permission to use a setup capability is not authority
        over every record in the installation.*

        This is the ONE predicate every setup mutation that accepts an EXISTING
        entity id routes through, so the rule cannot drift between v1 and v2, or
        between delete / reassign / in-place edit. Callers must apply it BEFORE
        calling the facade — i.e. before any lookup-derived field is serialized
        into a response and before any save or audit row — and must refuse with
        the facade's own byte-identical ``"<Label> <id> not found."`` wording, so
        an inaccessible record is indistinguishable from a nonexistent one and
        this never becomes an existence oracle. A falsy id is not this gate's
        business; leave the facade's own validation error alone.

        Rules, applied in order:

        1. ``role is None`` → **None**, meaning "no user context was supplied,
           do not gate". Returned rather than True so the caller can tell
           "legacy identity-less call" from "authorized", and so no call site
           can accidentally read a skipped gate as an approval. Very many
           internal call sites, the demo/full seeds, and the acceptance
           harnesses drive this facade with no identity at all; they must be
           completely unaffected. This is NOT a bypass for synthetic actor
           labels or for the seed — an identified caller with a real role is
           gated no matter what its actor string looks like.

        2. Resolve the caller's persisted active context via
           ``resolve_with_league`` — **all three axes**, not just the Program.
           **A null Program fails CLOSED (False).** No active Program means
           nothing has been validated to compare against, so there is no record
           this caller may mutate. Failing open here would reinstate the exact
           blocker for any account that has not selected a context.

        3. Look the record up. Missing → False, the same value an inaccessible
           record gets, because the caller emits one generic not-found for both.

        4. Walk the record's chain into ``(program, season, league)`` LINK
           TRIPLES (see ``_setup_target_edges`` for the per-kind axis table).

        5. **Any edge → the record is LINKED: return whether ONE WHOLE edge
           satisfies the caller's whole context** (see
           ``_setup_target_edge_allows``). The first cut of this gate resolved
           all three axes and then discarded Season and League, authorizing on
           the Program alone: with Program P / Season S / League A persisted, a
           League-B Player in the SAME Program could be renamed through
           ``/api/v2/setup/player/<id>/update``, and the same construction let
           a Season-A caller mutate Season-B rows. Every axis a record really
           carries is compared, a Program-only context fails closed against a
           Season-bound row, and an explicit No League means the approved
           Program + active-Season union rather than "no ceiling".

           Creator ownership deliberately does NOT apply here. Permanent creator
           authority surviving Program linking was ruled a blocker in its own
           right: whoever first typed a record in would keep delete rights over
           it forever, across every Program it was later bound into and every
           operator handover — an unrevokable, invisible back door that no
           Program admin could see or remove. Once a record joins a chain, the
           chain is the sole authority.

        6. **No edge at all → the record is genuinely UNLINKED**, the pending-link
           state a two-step setup flow legitimately passes through (create a
           Venue, then grant a Season access to it). Only its authenticated
           CREATOR may act on it, read from the setup audit trail. This is the
           narrowest rule that keeps that flow working: without it the operator
           could not finish, or even delete, the record they just made.

        A record whose chain EXISTS but cannot be resolved (a dangling FK) is
        treated as linked-and-unmatched → False, never as unlinked. Corrupting
        data must not be a way into the permissive branch.

        ``kind`` must be one of ``_SETUP_TARGET_KINDS`` (hyphens are folded to
        underscores). Transport aliases — v1's "league" for a Program and
        "level" for a League — MUST be translated by the caller; they are
        rejected here rather than guessed, because guessing between the two
        meanings of "league" is precisely the vocabulary trap that produced
        this class of defect.
        """
        # -- rule 1: no identity → no gate ---------------------------------
        if role is None:
            return None

        normalized = (kind or "").replace("-", "_")
        if normalized not in self._SETUP_TARGET_KINDS:
            return False                     # untranslated/unknown → fail closed

        # -- rule 2: the persisted active context, failing closed ----------
        program, season, league = self.context.resolve_with_league(
            user_id, role, scope)
        if program is None:
            return False

        # ONE read snapshot for the whole chain walk. Rules 5 and 6 branch on
        # whether a set assembled from MANY separate reads is empty (an
        # Organization alone walks all_programs + all_venues + one grant query
        # per venue), so a torn read is a correctness question, not a cosmetic
        # one. Plain `transaction()` would be READ COMMITTED on PostgreSQL —
        # every statement re-snapshots — so the isolation is requested
        # explicitly; on memory/SQLite it is a documented no-op because the
        # process-wide lock already serializes the block.
        #
        # Requesting isolation is legal on the outermost transaction, and also
        # on a join whose open transaction ALREADY guarantees at least this much
        # (#369) — which is how `setup_guarded_mutation` runs this predicate,
        # the context resolve above and the mutation itself as ONE SERIALIZABLE
        # unit. Standalone (the read-only predicate call), this is the outermost
        # transaction and behaves exactly as before. READ-ONLY at REPEATABLE
        # READ cannot raise a serialization conflict, so no retry loop is needed
        # here (unlike the context service's `_snapshot`); when nested, the
        # guard's own bounded retry owns that.
        #
        # NOTE this predicate is NOT a security boundary on its own: it answers
        # for the instant it read, and by the time a caller acts on the answer
        # the chain may have moved. Only `setup_guarded_mutation`, which holds
        # the row locks and the transaction across the mutation, is.
        with self.store.transaction(isolation="REPEATABLE READ"):
            # -- rule 3: existence (indistinguishable from inaccessible) ---
            record = self._setup_target_record(normalized, record_id)
            if record is None:
                return False

            # -- rule 4: the record's chain, as WHOLE link triples ----------
            edges, saw_link = self._setup_target_edges(normalized, record)

            # -- rule 5: LINKED → the chain decides, creator is irrelevant --
            # ONE whole link must place the record inside the caller's whole
            # persisted context. Judging edge by edge (rather than testing each
            # axis against a union of components taken from DIFFERENT links) is
            # what stops a (Season S, League B) caller from being authorized by
            # a record whose only links are (S, League A) and (S', League B).
            if edges:
                return any(
                    self._setup_target_edge_allows(edge, program, season,
                                                   league)
                    for edge in edges)
            if saw_link:
                return False                 # linked but unresolvable → closed

            # -- rule 6: genuinely UNLINKED → creator only -----------------
            return self._setup_target_created_by(
                normalized, record_id, user_id)

    def setup_target_allowed(self, kind, record_id, user_id, role, scope,
                             rule="scope"):
        """The bridge-aware, UNLOCKED answer to "may this caller act on this
        record": True / False / None (no identity — do not gate).

        One entry point covering both rules and both bridge rows, so the
        transport layer never re-derives any of it:

        * ``rule="scope"`` → the generic ``setup_target_accessible`` ceiling;
        * ``rule="grantable"`` → the facility-tree exception
          (``setup_venue_grantable``), which governs ONLY the Venue argument of
          the venue-access grant;
        * ``rule="active_program_league"`` → #367's create-side rule
          (``setup_league_in_active_program``), for a League id supplied in a
          CREATE body;
        * ``rule="writable_parent"`` → #369 review's WRITE-side parent rule
          (``setup_parent_writable``), for a destination parent id supplied in
          a reassign body;
        * a bridge pseudo-kind is resolved to the parent that names its Program
          first; an unresolvable bridge row returns None (nothing to judge — the
          facade emits the byte-identical wording a step later).

        This is a PREFLIGHT, for a cheap, lock-free, early, well-worded refusal.
        It is NOT the security boundary: its answer is already stale by the time
        anyone acts on it. ``setup_guarded_mutation`` is the boundary.
        """
        if rule == "writable_parent":
            # Deliberately BEFORE the `role is None` pass-through below. The
            # parent gate fails CLOSED on an unresolvable identity (see
            # ``setup_parent_writable``) — the create routes have always
            # refused an identity-less caller rather than letting a
            # caller-supplied parent id through, and the reassign that reaches
            # the same relation by the other verb must not be looser. A parent
            # is never a bridge pseudo-kind, so no resolution is needed first.
            return self.setup_parent_writable(
                (kind or "").replace("-", "_"), record_id, user_id, role,
                scope)
        if role is None:
            return None
        normalized = (kind or "").replace("-", "_")
        bridge = self._SETUP_BRIDGE_TARGETS.get(normalized)
        if bridge is not None:
            getter, _locking, attr, parent_kind = bridge
            row = getattr(self.store, getter)(record_id)
            parent_id = getattr(row, attr, None) if row is not None else None
            if not parent_id:
                return None
            normalized, record_id = parent_kind, parent_id
        if rule == "grantable":
            return self.setup_venue_grantable(record_id, user_id, role, scope)
        if rule == "active_program_league":
            return self.setup_league_in_active_program(
                record_id, user_id, role, scope)
        return self.setup_target_accessible(
            normalized, record_id, user_id, role, scope)

    # -- the ATOMIC guard: authorize + lock + mutate + audit, one txn (#369) --
    #
    # The blocker this closes. `setup_target_accessible` above is a PREDICATE:
    # it snapshots, decides, and CLOSES its transaction. The transport layer
    # then called the facade in a second, independent transaction. Between the
    # two the target's chain could move, and it did: an authorized Program-A
    # delete of Venue V raced a commit that moved V into Program B, and the
    # Program-A request returned 200 and deleted B's row. Snapshot isolation
    # inside the predicate never helped — it makes the WALK coherent, but it
    # ends before the write, so the check and the use were never one unit.
    # Source/destination reassigns had the same gap, and the bridge-row parent
    # lookup sat outside even the predicate's transaction.
    #
    # The rule now: target lookup, bridge-parent resolution, active-tuple
    # authorization, source AND destination validation, the mutation and its
    # audit all run inside ONE `store.transaction(isolation="SERIALIZABLE")`,
    # and every named row is ROW-LOCKED before the decision is taken.
    #
    # Why SERIALIZABLE and not REPEATABLE READ: the participants inside this
    # transaction ask for their own isolation — `ContextService._snapshot`
    # needs SERIALIZABLE (#159's linearizability with scope-changing writes)
    # and the chain walk needs REPEATABLE READ. A nested join may not RAISE
    # isolation, so the outer unit adopts the strongest level any participant
    # needs. Nothing is silently downgraded.
    #
    # Why LOCK rather than only re-read: under snapshot isolation a re-read
    # inside the same transaction returns the SAME data, so "re-validate just
    # before the write" is worthless on its own — it cannot see what committed
    # after the snapshot. The locks are taken as the transaction's FIRST
    # statements, which does two things at once: it establishes the snapshot no
    # earlier than the lock (so anything committed before the lock IS visible to
    # the authorization that follows), and it holds those rows until commit (so
    # nothing that touches them can commit before the mutation). A writer that
    # updated a named row after our snapshot makes the lock raise 40001, which
    # the store translates into ConcurrencyConflictError and the bounded retry
    # below re-runs from a fresh snapshot — at which point the authorization
    # sees the moved record and refuses generically.
    #
    # What this locks, and why that is the right set: the rows the request
    # NAMES — every gated target plus a bridge row's resolved parent. Locking
    # the transitive chain (a Club's every Team, an Organization's every Venue
    # and grant) is not expressible as a row lock at all — those are predicates,
    # not rows. It is also unnecessary: every OTHER way to change a target's
    # chain is itself a setup mutation that NAMES the record whose chain it
    # changes (a venue-access grant names the Venue, a registration's
    # league/division reassign names the registration), so it runs through this
    # same guard and contends for the same lock.
    _GUARDED_MUTATION_RETRIES = 10

    def setup_guarded_mutation(self, targets, mutation, user_id, role, scope):
        """Run ``mutation`` only if every target is still authorized, with the
        authorization, the locks, the write and its audit in ONE transaction.

        ``targets`` is a sequence of ``(kind, record_id, rule)`` triples in the
        caller's own order. ``rule`` is ``"scope"`` for the generic
        ``setup_target_accessible`` gate, ``"grantable"`` for the facility-tree
        exception (``setup_venue_grantable``), which governs ONLY the Venue
        argument of the venue-access grant, ``"active_program_league"`` for
        #367's create-side League rule, or ``"writable_parent"`` for #369
        review's write-side parent rule (``setup_parent_writable``) — a
        reassign's DESTINATION parent, which must be re-decided under that
        row's lock rather than only preflighted. The same record may appear
        twice under two different rules; both must pass. ``kind`` may be a
        bridge pseudo-kind (``registration`` / ``season_venue_access``);
        hyphens are folded.

        Returns ``(payload, refused_index)``:

        * ``(payload, None)`` — the mutation ran and COMMITTED; ``payload`` is
          whatever ``mutation()`` returned.
        * ``(None, i)`` — target ``i`` is not this caller's to touch. NOTHING
          ran: no lookup-derived field was serialized, no row written, no audit
          row. The caller renders its own generic not-found for
          ``targets[i]`` — this method deliberately does not know the wire
          wording, so a refusal stays byte-identical to a nonexistent id.
        * ``(error_dict, None)`` — the mutation itself refused (a domain error).
          The transaction is ROLLED BACK first: the facade's ``@catch`` turns a
          DomainError into a dict at a level BELOW this transaction, so without
          an explicit rollback a half-applied mutation would commit under an
          error response. Before this guard the facade owned the outermost
          transaction and got that rollback from the store; it still does.

        ``role is None`` (a legacy identity-less internal caller, the demo/full
        seeds, the acceptance harnesses) is completely ungated AND completely
        untouched: no transaction is opened and no lock is taken, so such a
        caller cannot be made to wait on, or deadlock with, anybody.
        """
        checks = [(index, (kind or "").replace("-", "_"), record_id, rule)
                  for index, (kind, record_id, rule) in enumerate(targets)
                  if isinstance(record_id, str) and record_id]
        if role is None or not checks:
            return mutation(), None
        try:
            for attempt in range(self._GUARDED_MUTATION_RETRIES):
                try:
                    return self._guarded_attempt(
                        checks, mutation, user_id, role, scope)
                except _SetupTargetRefused as exc:
                    return None, exc.index      # rolled back: nothing happened
                except _SetupMutationRefused as exc:
                    return exc.payload, None    # rolled back: nothing happened
                except ConcurrencyConflictError:
                    # A named row moved under us (or the DB detected a cycle).
                    # Everything rolled back, so a fresh attempt re-reads a
                    # snapshot that INCLUDES the winner and either authorizes
                    # against the new truth or refuses. Bounded, like
                    # ContextService._snapshot; the last attempt surfaces the
                    # stable retryable conflict instead of spinning.
                    if attempt == self._GUARDED_MUTATION_RETRIES - 1:
                        raise
        except DomainError as exc:
            # The facade's own @catch sits INSIDE this transaction, so a domain
            # error raised by the transaction boundary itself (a translated DB
            # conflict, the itemised has-dependencies re-resolution) would
            # otherwise escape as an exception and become a 500.
            return exc.to_dict(), None
        raise AssertionError("guarded mutation retry loop exited without a "
                             "result")

    def _guarded_attempt(self, checks, mutation, user_id, role, scope):
        """One all-or-nothing attempt. See ``setup_guarded_mutation``."""
        with self.store.transaction(isolation="SERIALIZABLE"):
            refused = self._authorize_setup_targets(
                checks, user_id, role, scope)
            if refused is not None:
                raise _SetupTargetRefused(refused)
            payload = mutation()
            if isinstance(payload, dict) and "error" in payload:
                raise _SetupMutationRefused(payload)
            return payload, None

    def _authorize_setup_targets(self, checks, user_id, role, scope):
        """Lock every named row, then authorize every target under those locks.

        Returns the index of the FIRST target this caller may not act on, or
        None when all pass. Must run inside ``setup_guarded_mutation``'s
        transaction — the locks are meaningless outside one.
        """
        # -- phase 1: lock the NAMED rows, resolving each bridge row's parent
        # FROM THE LOCKED ROW (one statement reads and locks it, so the parent
        # cannot be re-pointed between the two). This runs first so the
        # transaction's snapshot is established by a locking statement rather
        # than by a bare read: anything committed before the lock is therefore
        # visible to the authorization below, and anything after it is excluded.
        #
        # Locks are taken in a canonical (kind, id) order in both phases, so two
        # guarded mutations naming an overlapping set can never take them in
        # opposite orders. (PostgreSQL would detect a deadlock and the retry
        # loop would recover, but not provoking one is cheaper.)
        resolved, parents = [], []
        for index, kind, record_id, rule in sorted(
                checks, key=lambda c: (c[1], c[2])):
            bridge = self._SETUP_BRIDGE_TARGETS.get(kind)
            if bridge is None:
                self._lock_setup_row(kind, record_id)
                resolved.append((index, kind, record_id, rule))
                continue
            _plain, getter, attr, parent_kind = bridge
            row = getattr(self.store, getter)(record_id)   # locks the join row
            parent_id = getattr(row, attr, None) if row is not None else None
            if not parent_id:
                # An unresolvable bridge row has no parent to judge. Not this
                # gate's business: the facade emits the byte-identical wording
                # a step later, inside this same transaction.
                continue
            resolved.append((index, parent_kind, parent_id, rule))
            parents.append((parent_kind, parent_id))

        # -- phase 2: lock the resolved bridge PARENTS (the rows that actually
        # name the Program a bridge row is judged by).
        for kind, record_id in sorted(set(parents)):
            self._lock_setup_row(kind, record_id)

        # -- phase 3: decide, under the locks, on the post-lock snapshot. In the
        # CALLER's order, so the target reported back is the first one the
        # caller listed, never whichever happened to sort first.
        for index, kind, record_id, rule in sorted(resolved):
            if rule == "grantable":
                # The facility-tree exception: a shared arena stays grantable
                # across Programs (#369 owner ruling). One argument, one route.
                allowed = self.setup_venue_grantable(
                    record_id, user_id, role, scope)
            elif rule == "active_program_league":
                allowed = self.setup_league_in_active_program(
                    record_id, user_id, role, scope)
            elif rule == "writable_parent":
                # #369 review's write-side parent rule, re-decided HERE under
                # the destination row's lock so a reassign's parent check is
                # atomic with the move it authorizes — not merely preflighted.
                allowed = self.setup_parent_writable(
                    kind, record_id, user_id, role, scope)
            else:
                allowed = self.setup_target_accessible(
                    kind, record_id, user_id, role, scope)
            if allowed is False:
                return index
        return None

    def _lock_setup_row(self, kind, record_id):
        """Take the write lock on one setup row (a no-op read on Memory/SQLite,
        ``SELECT ... FOR UPDATE`` on PostgreSQL). An unknown kind locks nothing
        — it cannot be authorized either, so the decision still fails closed."""
        getter = self._SETUP_TARGET_LOCKS.get(kind)
        if getter is not None:
            getattr(self.store, getter)(record_id)

    # The permission each Setup workflow's own primary action needs (#330
    # review round 1 finding 1). Facilities is MANAGE_ARENA — the one
    # permission both League Admin and Arena Manager hold — like its own
    # underlying routes (ice-slot create, the Ice Availability Builder); every
    # other workflow's primary action (season/team/player create, season
    # team-registration, the full import surface) requires MANAGE_SETUP,
    # which only League Admin holds.
    _WORKFLOW_PERMISSION = {
        "league_season": Permission.MANAGE_SETUP,
        "teams": Permission.MANAGE_SETUP,
        "participation": Permission.MANAGE_SETUP,
        "roster": Permission.MANAGE_SETUP,
        "facilities": Permission.MANAGE_ARENA,
        "import": Permission.MANAGE_SETUP,
    }

    @catch
    def get_setup_progress(self, user_id, role, scope) -> dict:
        """Program-scoped completion state for the six Setup workflows #204/
        #330 name, for the Home/Tasks hub's "Continue setup" primary action:
        which of the six is next incomplete AND actually actionable by the
        caller's role, so the operator is told rather than left to infer it
        from the data model or steered into a CTA they cannot execute (#330
        review round 1 finding 1 — an Arena Manager, who holds MANAGE_ARENA
        but not MANAGE_SETUP, must never be handed "Add Season").

        Resolves the acting Program AND Season from the SAME active-context
        selection as ``get_active_context`` (#159) — this is a per-user,
        session-scoped view, unlike the installation-wide
        ``get_setup_overview_v2``/``get_onboarding_status_v2``. No Program yet
        (or none authorized) is a legitimate empty state, not an error: an
        empty ``workflows`` list with ``next: None``.

        Each workflow's own "done" boundary mirrors the matching
        ``get_onboarding_status_v2`` step, scoped to this Program instead of
        the whole installation. Season participation and facilities are
        further scoped to the ACTUAL resolved Season, not every Season the
        Program has (#330 review round 1 finding 2) — see the comment at
        their computation for why "league profile and seasons" stays
        deliberately Program-wide instead.

        The response's ``workflows`` is filtered to entries this caller's
        role can actually manage (#331 review round 3 finding 1): an Arena
        Manager reading this alongside "facilities" must never also receive
        League-Admin-only completion signals or exact team/registration/
        player counts — that crosses the same role/privacy boundary
        ``next``'s own permission filter exists to hold. ``complete`` and
        ``next`` are still DERIVED from the FULL, unfiltered internal list
        computed below, BEFORE that filtering step: ``complete`` means the
        WHOLE Program's setup is done, independent of role, and must not
        flip true just because the one workflow a caller can see happens to
        be done.

        Whether that derived value is actually EXPOSED, though, depends on
        whether this caller's role can see the full list it was derived
        from (#331 review round 5 finding 3): a role whose ``workflows`` is
        narrower than the full set (today, Arena Manager) can never
        truthfully verify a whole-Program claim, so it receives ``None``
        rather than the real boolean — exposing the real value
        unconditionally let a change to a workflow this caller cannot even
        see flip a bit in their own response, leaking through the same
        redaction boundary ``workflows`` itself exists to hold. ``None`` is
        neither an overclaimed ``true`` (round 3's bug) nor an
        independently meaningful ``false`` (round 5's bug) — it is constant
        regardless of invisible state, so by construction it carries none
        of it. A role that CAN see everything (today, only League Admin)
        is unaffected and keeps receiving the real value. A caller with
        nothing left THEY can act on (e.g. an Arena Manager once facilities
        is done, while League-Admin-only workflows remain) gets
        ``next: None`` alongside this non-``true`` ``complete`` — the
        caller distinguishes "genuinely done" (League Admin only, real
        ``true``) from "nothing more for you" by checking ``complete``.

        ``next`` is the FIRST todo workflow, in the fixed #204 order, that
        this caller's role can manage — filtered to what's actually safe to
        execute right now, not merely permitted (#331 review rounds 3-5):
        both "facilities" and "participation" need a resolved, ACTIVE
        Season (their real writes both route through
        ``season_guard.require_active_season`` and fail ``season_missing``
        with none resolved, ``season_archived`` if the resolved one is
        archived), AND each has one more hard floor beyond the Season alone
        that would otherwise leave its CTA a guaranteed dead end —
        "facilities" needs at least one Rink with active Season venue
        access (``venue_access_missing`` — a preview with none provably
        yields zero slots, and an Arena Manager cannot grant that access
        themselves) and "participation" needs at least one Program Team
        eligible for the resolved Season's league(s)
        (``team_league_mismatch`` — a Team with a permanent League can only
        register into a LeagueSeason of that same League, so with no
        eligible Team every registration attempt is a guaranteed rejection)
        — see ``_workflow_prerequisite_gap`` for the full detail on both.
        The FIRST permitted-todo workflow is the one this applies to; a
        prerequisite gap there blocks it IN PLACE rather than falling
        through to a later, incidentally-safe workflow — #330 names "the
        actual next incomplete step" as a strictly ordered contract, and
        skipping ahead would silently reorder it and could read as the
        blocked step being forgotten rather than blocked. When that first
        workflow is safe, it is ``next``; when it is blocked, ``next`` is
        None and ``next_blocked`` names it with a reason code and
        human-readable guidance, so the operator is told what to resolve
        first instead of being left to infer it (or, for a role that
        cannot resolve it themselves — an Arena Manager blocked on a
        Season only a League Admin can create, or on venue access only a
        League Admin can grant — at least told clearly rather than handed
        a CTA that silently fails).

        "Imports and onboarding" reports a third ``status``, ``"optional"``,
        instead of ``"done"``/``"todo"`` (#330 review round 1 finding 5): it
        is a standing, always-available alternative entry point into 1-5
        (bulk-import teams/players, officials/availability, or rinks/ice-
        slots), not an independently gated step — unlike 1-5, there is no
        reliable Program-scoped "has an import ever run here" signal to
        compute a real done/todo state from (two of the three import-commit
        paths write only aggregate counts into their own audit summary row,
        no season- or program-derivable field — see
        ``SetupService.commit_officials_availability_import``/
        ``commit_rinks_ice_slots_import``). Deriving "done" from whether 1-5
        happen to all be done (the prior shape) was an invented rule with no
        such grounding, and made it impossible for this step to ever be
        surfaced as ``next`` on its own. ``"optional"`` is never a candidate
        for ``next`` and never blocks ``complete``, but the workflow stays
        fully visible and reachable the whole time, per #330. Recorded as
        decision 9 in ``docs/product/operator-ux-requirements.md``'s
        "Product decisions and sign-off" section.

        #367: also resolves the persisted League (the third #345 context
        axis, previously ignored here). A selected League narrows
        "Permanent teams", "Clubs, players and staff", and "Season
        participation and divisions" to that League's own Teams/LeagueSeason
        (``Team.league_id`` is the real, permanent competition-League field
        (#283) -- unrelated to this file's OWN legacy ``league_id``-means-
        Program naming elsewhere, e.g. ``get_demo_overview``'s team rows).
        "League profile and seasons" (a Program-wide integrity check: does
        EVERY Season have a League) and "Venues, rinks and ice" (physical
        facility resources with no competition-League axis by design, see
        ``SeasonVenueAccess``) are unaffected by League selection -- narrowing
        either would not correspond to anything real in the domain model.
        Explicit "No League" keeps the full Program/Season-wide view,
        byte-identical to pre-#367 behavior.

        The #367 owner ruling also makes the active-Season ceiling explicit
        and non-optional here, exactly as it already is for
        ``get_demo_overview``: a workflow is **Season-bound** when it counts
        or gates on rows that reach a Season only through a
        SeasonTeamRegistration/Division's LeagueSeason, or a
        SeasonVenueAccess grant -- "Season participation and divisions" and
        "Venues, rinks and ice" are the two here, and BOTH must go EMPTY
        under a Program-only context (no Season resolved), never fall back
        to the union of every Season the Program has. See
        ``in_scope_season_ids`` at their shared computation below, which
        mirrors ``get_demo_overview``'s own
        ``in_scope_season_ids = {season.id} if season is not None else set()``
        idiom byte-for-byte (docs/architecture/active-context-scoping.md).
        "League profile and seasons" (a Program-WIDE integrity check ABOUT
        every Season collectively, not a per-Season fact) and "Permanent
        teams"/"Clubs, players and staff" (Team/Player carry no Season field
        at all -- see ``domain.models.Team``/``Player``) are NOT Season-bound
        by this test, and correctly stay invariant to the active Season,
        Program-only included.
        """
        program, season, league = self.context.resolve_with_league(
            user_id, role, scope)
        if program is None:
            return {"program_id": None, "program": None, "workflows": [],
                    "next": None, "next_blocked": None, "complete": False}

        seasons = self.store.seasons_for_program(program.id)
        season_ids = {s.id for s in seasons}
        leagues = self.store.leagues_for_program(program.id)
        program_league_seasons = [ls for ls in self.store.all_league_seasons()
                                  if ls.season_id in season_ids]
        teams = self.store.teams_for_program(program.id)
        team_ids = {t.id for t in teams}
        # #367: "Permanent teams" and "Clubs, players and staff" narrow to the
        # selected League's own Teams once one is active -- league-less teams
        # never included in this narrowed set (unlike the eligibility check
        # below, which deliberately still counts them). "No League" selected
        # keeps the full Program-wide set, identical to pre-#367 behavior.
        league_teams = (
            [t for t in teams if t.league_id == league.id] if league
            else teams)
        league_team_ids = {t.id for t in league_teams}
        # Participation and facilities are inherently per-Season concepts —
        # narrowed to the RESOLVED active Season's own LeagueSeasons/venue
        # access below, never every Season the Program has, so an older
        # Season's registrations or granted ice can't mask required work in
        # a newly-selected Season (#330 review round 1 finding 2). No Season
        # resolved (a Program-only context) means neither can be done yet.
        # #367 owner ruling: this is the active-Season CEILING, not merely a
        # convenience narrowing -- a Program-only context MUST make both
        # workflows empty, never silently widen back to "every Season the
        # Program has". `in_scope_season_ids` names the ceiling as its own
        # value (instead of two independent `if season else ...` ternaries)
        # so it matches, textually, the identical
        # `in_scope_season_ids = {season.id} if season is not None else set()`
        # idiom `get_demo_overview` uses for its own Season-scoped
        # collections (docs/architecture/active-context-scoping.md) -- the
        # cross-check the #367 review round used to confirm this ceiling is
        # real and not merely coincidentally empty. "League profile and
        # seasons" (Program-wide over every Season COLLECTIVELY, computed
        # above from `seasons`/`program_league_seasons`) and "Permanent
        # teams"/"Clubs, players and staff" (`league_teams`/
        # `program_players` -- Team/Player have no Season field at all) are
        # NOT Season-bound and deliberately never read `in_scope_season_ids`.
        in_scope_season_ids = {season.id} if season is not None else set()
        # #367: a selected League further narrows the candidate LeagueSeason
        # set to that League's own binding for the resolved Season -- "No
        # League" considers every League's binding, identical to pre-#367
        # behavior.
        season_league_seasons = [
            ls for ls in program_league_seasons
            if ls.season_id in in_scope_season_ids]
        if league:
            season_league_seasons = [
                ls for ls in season_league_seasons
                if ls.league_id == league.id]
        season_ls_ids = {ls.id for ls in season_league_seasons}
        # The resolved Season's (and, when selected, League's) own League ids
        # (via its LeagueSeasons) -- used below by
        # _workflow_prerequisite_gap to check whether ANY Program Team is
        # even eligible to register here (#331 review round 5 finding 2): a
        # Team with a permanent League can only ever register into a
        # LeagueSeason of that same League (register_team_for_season rule 7,
        # team_league_mismatch), so if no Team's permanent League appears
        # here (and none is league-less), every possible registration
        # attempt in this Season is a guaranteed rejection.
        season_league_ids = {ls.league_id for ls in season_league_seasons}

        # ---- the server's OWN hard-prerequisite inputs (#365 review) -------
        # Hoisted ABOVE the workflow list so ONE computation feeds three
        # consumers that must never disagree: facilities' own done/todo count
        # below, the per-workflow ASSERTED `prerequisites` each card reads, and
        # `_workflow_prerequisite_gap`'s refusal reason for `next_blocked`.
        # Previously `team_league_eligible` was computed AFTER the workflows
        # were built (so participation's card could not publish it at all) and
        # `schedulable_rink_ids` was computed inside step 5 -- which is exactly
        # how the client ended up holding ONE of the server's floors
        # (venue_access) and none of the others.
        #
        # Season-bound under the same `in_scope_season_ids` ceiling as the
        # workflows that read them: a Program-only context grants no venue
        # access and binds no LeagueSeason, so both go empty rather than
        # widening to every Season the Program has.
        venue_access_venue_ids = {
            a.venue_id for a in self.store.all_season_venue_access()
            if a.active and a.season_id in in_scope_season_ids}
        schedulable_rink_ids = {
            r.id for r in self.store.all_rinks()
            if r.venue_id in venue_access_venue_ids}
        # Mirrors register_team_for_season's own rule 7 (#331 review round 5
        # finding 2): a Team with a permanent League can only register into a
        # LeagueSeason of that same League, so a Program whose Teams all sit
        # outside this Season's League(s) has no possible registration.
        team_league_eligible = any(
            t.league_id is None or t.league_id in season_league_ids
            for t in teams)

        workflows = []

        def add(key, label, done, detail, primary_action, *, attention=None,
                prerequisites=None):
            entry = {
                "key": key, "label": label,
                "status": "done" if done else "todo",
                "detail": detail, "primary_action": primary_action}
            # #331 review round 19: additive and independent of `status` --
            # a workflow can be legitimately "done" (some row IS valid and
            # schedulable) while ALSO having other row(s) that need cleanup.
            # Folding that into `status`/`detail` would either falsely
            # reopen a genuinely complete workflow or silently bury the
            # signal in prose a screen reader user can't act on; a caller
            # that doesn't know this field simply never sees it, same as
            # `next_blocked` being absent when nothing is blocked.
            if attention:
                entry["attention"] = attention
            # #365 review (Facilities fail-open): the per-workflow, ASSERTED
            # prerequisite facts for THIS resolved tuple -- additive and
            # independent of `status`/`attention` exactly as those are of
            # each other. The COMPLETE ordered set for the workflow, built by
            # _workflow_prerequisite_rows below -- see _workflow_prerequisites
            # for why this cannot be left to `next_blocked`, and the audit on
            # _WORKFLOW_HARD_PREREQUISITES for which workflows have floors.
            if prerequisites:
                entry["prerequisites"] = prerequisites
            workflows.append(entry)

        # 1. League profile and seasons: every Season this Program has must
        # carry at least one grouping League. Deliberately Program-wide, NOT
        # scoped to the selected Season like participation/facilities below:
        # this is an integrity check ("does EVERY Season have a League"),
        # mirroring get_onboarding_status_v2's own "league" step exactly, not
        # a per-selected-Season fact. NOT Season-bound under the #367 owner
        # ruling (it is precisely the "Program-wide structural check ABOUT
        # Seasons collectively" that ruling names as the non-Season-bound
        # case) -- stays identical across every Season selection and
        # Program-only alike.
        seasons_without_league = [
            s for s in seasons
            if s.id not in {ls.season_id for ls in program_league_seasons}]
        league_done = bool(seasons) and not seasons_without_league
        add("league_season", "League profile and seasons", league_done,
            (f"{len(seasons)} season(s), {len(leagues)} league(s)"
             if seasons else "No season created yet."),
            "Add Season")

        # 2. Permanent teams — Program-level, no Season dimension at all.
        # NOT Season-bound (``domain.models.Team`` has no season_id field to
        # gate on) -- identical across every Season selection, Program-only
        # included. #367: narrowed to the selected League's own Teams when
        # one is active (league_teams); "No League" keeps the full Program
        # set.
        add("teams", "Permanent teams", bool(league_teams),
            f"{len(league_teams)} team(s)" if league_teams
            else "No team added yet.",
            "Add Team")

        # 3. Season participation/divisions: at least one active
        # registration, IN THE SELECTED SEASON, whose League resolves and
        # whose Team is this Program's, with any Division agreeing with the
        # registration's League+Season — same validity rule as
        # get_onboarding_status_v2's "participation" step, scoped further to
        # one Season here.
        divisions_by_id = {d.id: d for d in self.store.all_divisions()}
        schedulable = 0
        # #331 review round 19: a registration this loop excludes from
        # `schedulable` isn't necessarily irrelevant -- an active row that
        # fails the Rule 7 League match is exactly the shape of stray
        # cross-League row `get_onboarding_status_v2`'s own "participation"
        # step already tracks as `invalid_regs` and reports via its
        # "invalid_registrations" blocker. This step previously dropped the
        # identical row with no signal at all: a Team with one genuinely
        # valid registration (elsewhere) reports "done" here with no way
        # for an operator to discover the OTHER row still needs cleanup.
        # Counted separately from `schedulable` (never merged into it, and
        # never changes `status`/`done`) -- an operator who has already
        # achieved real, valid participation must still see "done", just
        # ALSO see that something else needs attention.
        needs_attention = 0
        # #331 review round 21 finding 2: resolved per-TEAM through the
        # shared `team_registration_valid` resolver, not per-row against its
        # own League alone (round 18's check, replaced here). A row that
        # matches its Team's permanent League in isolation is NOT
        # schedulable if the Team ALSO holds another active row elsewhere
        # this Season, in any League -- the identical unconditional
        # season-wide conflict `create_game`/`move_game`/`publish_game`
        # already fail closed on (round 20). The per-row check asked only
        # "does THIS row's League match its Team" and had no way to see a
        # SECOND active row at a different LeagueSeason, so it counted a
        # Team's conflicted row as `schedulable` while the live-scheduling
        # resolver would reject every game that Team plays in -- exactly
        # the reproduction this round names. Memoized per Team (not
        # recomputed per row): a Team can hold more than one row among this
        # Season's LeagueSeasons -- the active stray this fixes, or a
        # historical inactive one already excluded by the `.active` guard
        # above, which must keep reading complete on its own.
        resolved_by_team = {}
        attention_ids = []
        for reg in self.store.all_season_team_registrations():
            if not reg.active or reg.team_id not in team_ids:
                continue
            if reg.league_season_id not in season_ls_ids:
                continue
            if reg.team_id not in resolved_by_team:
                resolved_by_team[reg.team_id] = team_registration_valid(
                    self.store, season, reg.team_id, require_division=False)
            live = resolved_by_team[reg.team_id]
            if live is None or live.id != reg.id:
                needs_attention += 1
                attention_ids.append(reg.id)
                continue
            if reg.division_id:
                division = divisions_by_id.get(reg.division_id)
                if division is None or division.league_season_id != reg.league_season_id:
                    needs_attention += 1
                    attention_ids.append(reg.id)
                    continue
            schedulable += 1
        add("participation", "Season participation and divisions",
            schedulable > 0,
            (f"{schedulable} schedulable registration(s)" if schedulable
             else "No team registered to play yet."),
            "Register Team",
            # The COMPLETE ordered hard-prerequisite set for this workflow
            # (#365 review round 3): selected Season present, that Season
            # active, and at least one Team eligible for its League(s) --
            # every reason `_workflow_prerequisite_gap` can refuse
            # "participation" with, in the order it tests them. Publishing
            # only some of them is what let an archived Season reach a card
            # that still offered "Register Team"/"Add division".
            prerequisites=self._workflow_prerequisites(
                "participation", season, schedulable_rink_ids,
                team_league_eligible),
            attention=(
                {"reason": "invalid_registrations", "count": needs_attention,
                 "affected_registration_ids": attention_ids,
                 "detail": (
                     f"{needs_attention} registration(s) in this season "
                     "don't match their team's permanent league or "
                     "division; resolve them in Season participation.")}
                if needs_attention else None))

        # 4. Clubs, players and staff: at least one player on one of this
        # Program's teams. Program-level like Teams — a Player belongs to a
        # Team, never a Season directly. NOT Season-bound for the same
        # reason as "Permanent teams" above (``domain.models.Player`` has no
        # season_id field either, only ``team_id``) -- identical across
        # every Season selection, Program-only included.
        # #367: narrowed to the selected League's own Teams (league_team_ids)
        # when one is active, matching "Permanent teams" above; "No League"
        # keeps the full Program set.
        program_players = [p for p in self.store.all_players()
                           if p.team_id in league_team_ids]
        add("roster", "Clubs, players and staff", bool(program_players),
            (f"{len(program_players)} player(s)" if program_players
             else "No player added yet."),
            "Add Player")

        # 5. Venues, rinks and ice: at least one available GAME slot at a
        # rink whose Venue holds active SeasonVenueAccess to the SELECTED
        # Season specifically (not any of the Program's Seasons). Season-
        # bound under the #367 owner ruling (same `in_scope_season_ids`
        # ceiling computed above for participation) -- empty under a
        # Program-only context, never any OTHER Season's grant. Deliberately
        # has NO League axis anywhere in this computation: Venue/Rink/IceSlot
        # reach a Season only through SeasonVenueAccess and carry no
        # competition-League field at all (see "Venues have no League axis",
        # docs/architecture/active-context-scoping.md).
        # `venue_access_venue_ids`/`schedulable_rink_ids` are computed ONCE,
        # above, and read here -- so this card's done/todo count, its published
        # prerequisites and the Ice Builder's own `venue_access_missing`
        # refusal are all answers from the same set.
        available_game_slots = [
            s for s in self.store.all_ice_slots()
            if s.rink_id in schedulable_rink_ids
            and s.slot_type == IceSlotType.GAME
            and s.status == IceSlotStatus.AVAILABLE]
        add("facilities", "Venues, rinks and ice", bool(available_game_slots),
            (f"{len(available_game_slots)} available game slot(s)"
             if available_game_slots else "No available game ice slot yet."),
            "Add Ice",
            # The COMPLETE ordered hard-prerequisite set (#365 review round
            # 3): selected Season present, that Season ACTIVE, and only then
            # the venue-access capability. Round 2 published the third fact
            # alone, so an ARCHIVED selected Season with a live grant settled
            # READY with `prerequisites: [{venue_access, met: true}]`,
            # `blockedBecause: null` and a dead-end "Add Ice" -- while
            # `_workflow_prerequisite_gap` was independently refusing the same
            # workflow with `season_archived`.
            prerequisites=self._workflow_prerequisites(
                "facilities", season, schedulable_rink_ids,
                team_league_eligible))

        # 6. Imports and onboarding — see docstring: "optional", not
        # done/todo, since there is no real Program-scoped completion signal
        # to compute either from.
        workflows.append({
            "key": "import", "label": "Imports and onboarding",
            "status": "optional",
            "detail": "Bulk-import league, team, or ice data.",
            "primary_action": "Import data"})

        complete = all(w["status"] == "done" for w in workflows
                       if w["status"] != "optional")

        # #331 review round 3/4 finding 1: `next` is the FIRST todo workflow
        # this role can manage, in the fixed #204 order -- #330's "actual
        # next incomplete step" is a strictly ordered contract, not "the
        # first one that happens to be safe". A prerequisite gap on that
        # one workflow blocks it in place; it is never skipped in favor of
        # a LATER todo workflow that happens to be unblocked (round 4
        # review: doing so silently reordered the sequence and could read
        # as "participation was skipped/forgotten" instead of "blocked,
        # here's why"). Permission still filters candidacy exactly as
        # before (round 1) -- only the FIRST permitted one is ever
        # considered, whether that turns out safe or blocked.
        # Prerequisite context for _workflow_prerequisite_gap (#331 review
        # round 5 findings 1/2) is `schedulable_rink_ids`/
        # `team_league_eligible`, both computed ONCE near the top of this
        # method (#365 review round 3) and shared with the per-workflow
        # `prerequisites` each card publishes -- so the roll-up's refusal and
        # the card's own assertions are literally the same facts.
        next_incomplete = None
        next_blocked = None
        for w in workflows:
            if w["status"] != "todo" or not can(role, self._WORKFLOW_PERMISSION[w["key"]]):
                continue
            gap = self._workflow_prerequisite_gap(
                w["key"], season, schedulable_rink_ids, team_league_eligible)
            if gap is None:
                next_incomplete = w
            else:
                reason, detail = gap
                next_blocked = {"key": w["key"], "label": w["label"],
                                 "reason": reason, "detail": detail}
            break

        # Redact workflows this caller's role cannot manage from the
        # response (#331 review round 3 finding 1) -- computed from the full
        # list above only AFTER complete/next_incomplete are already
        # resolved internally, so an Arena Manager's narrower view can never
        # change either of those INTERNAL values.
        visible_workflows = [w for w in workflows
                              if can(role, self._WORKFLOW_PERMISSION[w["key"]])]

        # #331 review round 5 finding 3: `complete` (computed above from the
        # FULL, unfiltered list) must not be EXPOSED to a role whose visible
        # slice is narrower than that full list -- round 3 already held its
        # raw value to the full list so it could never overclaim true just
        # because the caller's own narrower slice happened to be done, but
        # exposing that value unconditionally still let a change to a
        # workflow this caller cannot even see flip a bit in THEIR own
        # response -- an information leak through the very redaction
        # boundary `workflows` itself exists to hold. A role that cannot
        # see the full list can also never truthfully VERIFY a claim about
        # the whole Program, so it gets `None` instead: neither `true`
        # (would overclaim, the round 3 bug) nor a real, independently
        # meaningful `false` (would still leak the invisible signal) --
        # `None` is constant regardless of invisible state, so by
        # construction it can carry none of it. A role that CAN see
        # everything (today, only League Admin -- MANAGE_SETUP AND
        # MANAGE_ARENA) is completely unaffected: its `visible_workflows`
        # already equals the full list, so it keeps receiving the real,
        # verifiable value exactly as before.
        role_sees_every_workflow = len(visible_workflows) == len(workflows)

        return {
            "program_id": program.id, "program": _serialize(program),
            "workflows": visible_workflows,
            "next": next_incomplete,
            "next_blocked": next_blocked,
            "complete": complete if role_sees_every_workflow else None,
        }

    # The COMPLETE, ORDERED hard-prerequisite set each workflow's primary
    # action needs, by workflow key (#365 review round 3). This mapping is
    # documentation of the audit, not a second authority: the rows themselves
    # are built by ``_workflow_prerequisite_rows`` below, which is the ONE
    # computation both the published ``workflows[].prerequisites`` and
    # ``_workflow_prerequisite_gap`` are projections of.
    #
    # THE AUDIT (#365 review round 3, verbatim requirement: "if
    # ``_workflow_prerequisite_gap`` can refuse a workflow for a reason, that
    # reason must be an asserted prerequisite"). Every one of the six
    # workflows was re-checked against the real write behind its primary
    # action:
    #
    # * "facilities"  -> ``SetupService.commit_ice_availability``: "Requires
    #   an active Season (#159)" (season_missing / season_archived), and every
    #   candidate rink must be reachable through active ``SeasonVenueAccess``
    #   (venue_access_missing). THREE floors.
    # * "participation" -> ``register_team_for_season``: takes
    #   ``_require_active_season`` (season_missing / season_archived) and
    #   enforces rule 7 (team_league_mismatch). THREE floors. Its own
    #   "Add Division" resolution step is ``create_division``, which takes the
    #   SAME ``_require_active_season`` guard -- which is why the Season floors
    #   have to precede it on the client chain too, not merely precede
    #   "Register Team".
    # * "league_season" -> ``create_season``: needs only the Program, and
    #   ``get_setup_progress`` returns the empty no-Program shape before any
    #   workflow exists. No Season guard anywhere on the path. NO floors.
    # * "teams" -> ``create_team``: a Team is permanent and carries no Season
    #   field at all (#283); the method takes no Season guard. NO floors.
    # * "roster" -> ``create_player``: a Player hangs off a Team, never a
    #   Season; no Season guard. NO floors.
    # * "import" -> the import commits are standing entry points with no
    #   Season prerequisite (and the workflow is "optional", so it is never a
    #   `next` candidate). NO floors.
    #
    # A workflow with no floors publishes no ``prerequisites`` key at all,
    # which is a positive statement backed by
    # ``test_only_the_two_audited_workflows_publish_prerequisites``.
    _WORKFLOW_HARD_PREREQUISITES = {
        "facilities": ("season_selected", "season_active", "venue_access"),
        "participation": ("season_selected", "season_active",
                          "team_league_eligible"),
    }

    @classmethod
    def _workflow_prerequisite_rows(cls, key, season, schedulable_rink_ids,
                                    team_league_eligible):
        """THE one ordered computation of `key`'s hard prerequisites (#365
        review round 3). Returns a list of rows, in the order the real writes
        fail, each ``{"key", "met", "reason", "detail", "guidance"}``.

        Round 2 had TWO computations: ``_venue_access_prerequisite`` published
        ONE fact to the Facilities card, while ``_workflow_prerequisite_gap``
        independently knew three. That is precisely how an ARCHIVED selected
        Season with a live grant reached a card reporting
        ``prerequisites: [{key: "venue_access", met: true}]`` and a primary
        "Add Ice" the Ice Builder would refuse with ``season_archived``. One
        asserted capability fact was being treated as the WHOLE workflow
        capability. Both consumers are now projections of THIS list:

        * ``_workflow_prerequisites`` publishes every row (minus ``guidance``)
          as the card's ASSERTED, ordered set, so a client that consumes them
          fail-closed cannot be missing a floor the server enforces.
        * ``_workflow_prerequisite_gap`` returns the FIRST unmet row's
          ``(reason, guidance)``, which is byte-identical to the round-2
          behaviour it replaces.

        ``detail`` and ``guidance`` are two audiences for the SAME row, not
        two authorities: ``detail`` is the sentence a workflow CARD renders
        beside its own counts ("what is true about this workflow"), while
        ``guidance`` is the roll-up's ``next_blocked`` instruction ("what to
        resolve first"). They can never disagree about WHICH prerequisite is
        unmet -- that is decided once, here -- and the split keeps #331's
        settled ``next_blocked`` copy verbatim while letting the card say
        something scoped to itself.

        Why the ORDER is load-bearing and not merely tidy: the client walks
        the chain and the FIRST unmet step chooses the one action the landing
        offers. "Season present" before "Season active" before the workflow's
        own capability floor is the order the real writes fail in
        (``season_guard.require_active_season`` runs before any body check),
        so the action offered is always the one that unblocks the NEXT
        failure rather than a later one that would still be refused.

        Read-only, like the guards it mirrors: never mutates and never
        row-locks."""
        if key not in cls._WORKFLOW_HARD_PREREQUISITES:
            return []
        action = "adding ice" if key == "facilities" else "registering teams"
        rows = []

        def row(prereq_key, met, reason=None, detail=None, guidance=None):
            entry = {"key": prereq_key, "met": bool(met)}
            if not met:
                entry["reason"] = reason
                entry["detail"] = detail
                entry["guidance"] = guidance
            rows.append(entry)

        # 1. A Season must be RESOLVED at all. Both real writes route through
        # season_guard.require_active_season, which needs a Season id; with a
        # Program-only context there is nothing to write into. The card cannot
        # resolve this itself -- picking a Season is a context-bar action, not
        # a create -- so the detail says so rather than naming a control.
        no_season_guidance = f"Create or select a Season before {action}."
        if season is None:
            row("season_selected", False, "season_missing",
                "No season is selected, so this workflow has nothing to work "
                "on yet — pick a season in the context bar to set one up.",
                no_season_guidance)
            # Ordered set, not a truncated one: an unresolved Season also
            # means there is no ACTIVE Season, and a fail-closed client must
            # receive that row rather than infer its absence as "met".
            row("season_active", False, "season_missing",
                "No season is selected, so there is no active season to work "
                "in yet — pick a season in the context bar to set one up.",
                no_season_guidance)
        else:
            row("season_selected", True)
            # 2. ...and it must be ACTIVE. An archived Season is read-only
            # (#159): every write this workflow owns fails `season_archived`
            # at the shared guard, so a card that offers ANY of them is a
            # guaranteed dead end no matter how much visible inventory it has.
            row("season_active", season.status != SeasonStatus.ARCHIVED,
                "season_archived",
                f"Season '{season.name}' is archived and read-only, so "
                f"nothing in it can be changed — it has to be reopened "
                f"before {action}.",
                f"Season '{season.name}' is archived and read-only — "
                f"reopen it or select an active Season before {action}.")

        where = f"Season '{season.name}'" if season is not None else "this season"
        if key == "facilities":
            # 3. The capability floor round 2 already published, unchanged in
            # both wording and meaning. `schedulable_rink_ids` is the SAME set
            # facilities' own done/todo check reads, so the card, the roll-up
            # and the Ice Builder's `venue_access_missing` refusal can never
            # disagree.
            #
            # Deliberately ROLE-INVARIANT, like every other row here: this is
            # a statement about the selected Season's data, and both roles
            # that can see the workflow (League Admin and Arena Manager both
            # hold MANAGE_ARENA) must receive byte-identical rows for it. WHO
            # may resolve a gap is a permission question the caller's own
            # permission set already answers; restating it here would create a
            # second authority on permissions that could drift from the first.
            row("venue_access", bool(schedulable_rink_ids),
                "venue_access_missing",
                f"No rink is reachable through active venue access for "
                f"{where} yet, so ice can't be added to it — a League Admin "
                f"must allow a venue with a rink for this season.",
                f"No rink has venue access granted for Season "
                f"'{season.name}' yet — a League Admin must grant "
                f"access to at least one rink before ice can be added."
                if season is not None else no_season_guidance)
        else:
            # 3. participation's own capability floor (#331 review round 5
            # finding 2), published for the first time here: a Team with a
            # permanent League can only register into a LeagueSeason of that
            # same League (register_team_for_season rule 7), so with no
            # eligible Team every registration attempt is a guaranteed
            # `team_league_mismatch` rejection regardless of which team the
            # operator picks.
            row("team_league_eligible", bool(team_league_eligible),
                "team_league_mismatch",
                f"No permanent team is eligible to register in {where} yet, "
                f"so registering one can't succeed — a team needs a league "
                f"that this season actually runs.",
                f"No permanent team is eligible to register in Season "
                f"'{season.name}' yet — add a team under a matching "
                f"league, or add a league to this season that matches "
                f"an existing team, before registering teams."
                if season is not None else no_season_guidance)
        return rows

    @classmethod
    def _workflow_prerequisites(cls, key, season, schedulable_rink_ids,
                                team_league_eligible):
        """The per-workflow ASSERTED prerequisite rows published on
        ``workflows[].prerequisites`` -- the COMPLETE ordered set above, with
        the roll-up-only ``guidance`` sentence dropped.

        Why this has to exist as a per-workflow field rather than being left
        to ``next_blocked`` or inferred by the caller:

        * ``next_blocked`` describes only the FIRST permitted TODO workflow
          (see the `next`/`next_blocked` loop above). For a League Admin,
          "facilities" is the fifth of six, so an earlier TODO workflow --
          participation, roster, anything -- consumes that one slot and the
          facilities gap is simply never reported. A card that reads its own
          prerequisites off `next_blocked` therefore fails OPEN for the exact
          role that CAN resolve them.
        * The Setup overview's Venue/Rink lists deliberately include
          revoked-grant history and creator-owned pending rows
          (``get_setup_overview_v2``), and its Season/League lists are equally
          a READ contract rather than a capability one. So "a row is VISIBLE"
          and "this workflow can write" are different claims, and only this
          computation answers the second one. A client that asked the first
          question offered a dead-end "Add Ice" against a revoked grant
          (round 2) and then against an archived Season (round 3).

        Emitted for EVERY role that can see the workflow at all, because the
        facts are exactly as load-bearing for the role that cannot fix them:
        an Arena Manager handed "Add Ice" reaches a builder that can only
        refuse."""
        return [{k: v for k, v in row.items() if k != "guidance"}
                for row in cls._workflow_prerequisite_rows(
                    key, season, schedulable_rink_ids, team_league_eligible)]

    @classmethod
    def _workflow_prerequisite_gap(cls, key, season, schedulable_rink_ids,
                                   team_league_eligible):
        """None if `key`'s primary action is safe to execute given the
        resolved Season context; otherwise (reason, detail) describing what
        must change first (#331 review rounds 3-5). Now a projection of
        ``_workflow_prerequisite_rows`` -- the FIRST unmet row's reason and
        roll-up guidance -- so the server cannot refuse a workflow for a
        reason the same workflow's card never received (#365 review round 3).

        Both "facilities" (``SetupService.commit_ice_availability``, behind
        the Ice Availability Builder) and "participation"
        (``register_team_for_season``) route through
        ``season_guard.require_active_season``, so both fail identically at
        the Season level: ``season_missing`` with no Season resolved (nothing
        to generate ice into / no season to register a team for -- also true
        for "participation" specifically because its own real destination,
        ``focusParticipationRegisterControl()``, needs an exact selected
        Season to deep-link/focus a specific Register control), and
        ``season_archived`` if the resolved one is archived (read-only until
        an authorized reopen). Beyond the Season itself each has one more hard
        floor -- ``venue_access_missing`` and ``team_league_mismatch`` -- both
        existence checks the real write also has no way around. Every other
        workflow's primary action (Add Season, Add Team, Add Player, Import
        data) has no Season prerequisite of its own; see the audit recorded on
        ``_WORKFLOW_HARD_PREREQUISITES``."""
        for row in cls._workflow_prerequisite_rows(
                key, season, schedulable_rink_ids, team_league_eligible):
            if not row["met"]:
                return (row["reason"], row["guidance"])
        return None

    # -- competition-hierarchy resolution (#283) ---------------------------
    # After the #283 model change a League is a permanent child of a Program
    # and its per-Season participation lives in a LeagueSeason; Divisions and
    # SeasonTeamRegistrations hang off a LeagueSeason (``league_season_id``)
    # rather than carrying their own ``season_id``/``league_id``. These helpers
    # resolve the dropped fields back so the response JSON keys (and the v1
    # boundary adapters that rename/drop ``league_id``) are unchanged.
    def _resolve_ls(self, league_season_id):
        """The LeagueSeason for an id, or None (guards a missing/empty link)."""
        if not league_season_id:
            return None
        return self.store.get_league_season(league_season_id)

    @staticmethod
    def _season_id_via(ls_by_id, league_season_id):
        ls = ls_by_id.get(league_season_id)
        return ls.season_id if ls else None

    @staticmethod
    def _league_id_via(ls_by_id, league_season_id):
        ls = ls_by_id.get(league_season_id)
        return ls.league_id if ls else None

    def _division_dict(self, division) -> dict:
        """A Division serialized with legacy competition keys restored (#283):
        ``season_id``/``league_id`` resolved via its LeagueSeason, and the
        internal ``league_season_id`` kept out of the v1-shaped payload so the
        ``division_to_v1`` boundary (league_id → level_id) is unchanged."""
        ls = self._resolve_ls(division.league_season_id)
        return {"id": division.id,
                "season_id": ls.season_id if ls else None,
                "league_id": ls.league_id if ls else None,
                "name": division.name,
                "age_group": division.age_group,
                "external_ref": division.external_ref}

    def _registration_dict(self, reg) -> dict:
        """A SeasonTeamRegistration serialized with legacy ``season_id``/
        ``league_id`` restored via its LeagueSeason (#283); ``league_season_id``
        is not exposed so the ``registration_to_v1`` boundary is unchanged."""
        ls = self._resolve_ls(reg.league_season_id)
        return {"id": reg.id,
                "season_id": ls.season_id if ls else None,
                "league_id": ls.league_id if ls else None,
                "team_id": reg.team_id,
                "division_id": reg.division_id,
                "active": reg.active}

    def _league_dict(self, league) -> dict:
        """A grouping League serialized in its frozen season-scoped v1 shape
        (#283): the permanent ``program_id`` is replaced by the ``season_id`` of
        its LeagueSeason, matching the v1 'level' contract (no adapter maps it)."""
        lss = self.store.league_seasons_for_league(league.id)
        return {"id": league.id,
                "season_id": lss[0].season_id if lss else None,
                "name": league.name,
                "sort_order": league.sort_order,
                "external_ref": league.external_ref}

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
        _rg = self.store.get_game(game_id)
        with self.store.transaction():
            # #159 — no reminders may be generated for an archived Season's
            # Game; lock the Season and emit inside one transaction.
            if _rg is not None and _rg.season_id:
                self.setup._require_active_season(_rg.season_id)
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
        # Active Coach accounts with no valid team scope (#266): after the
        # fail-closed scope gate these accounts can do nothing (every roster
        # mutation and private read is refused), so they are a rollout defect to
        # remediate — an operator must rebind them to a real team or deactivate
        # them. Surfaced here rather than silently grandfathered.
        unscoped_coaches = sum(
            1 for a in self.accounts.list_accounts()
            if a.active and a.role == Role.COACH
            and (not (a.scope or {}).get("team_id")
                 or self.store.get_team((a.scope or {}).get("team_id")) is None))
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
            {"name": "coach_scope_bound",
             "ok": (unscoped_coaches == 0) if production else True,
             "detail": (f"{unscoped_coaches} active coach account(s) without a "
                        "valid team — rebind or deactivate them"
                        if unscoped_coaches else "all active coaches bound to a team")},
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
        # Divisions no longer carry season_id directly (#283) — resolve it via
        # their LeagueSeason.
        ls_by_id = {ls.id: ls for ls in self.store.all_league_seasons()}

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
        divisions_dangling = [
            d for d in divisions
            if self._season_id_via(ls_by_id, d.league_season_id) not in season_ids]
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

        has_division = any(
            self._season_id_via(ls_by_id, d.league_season_id) in season_ids
            for d in divisions)
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
        # A League's Season participation (and a Division/registration's Season +
        # League) now resolve through LeagueSeason (#283).
        all_league_seasons = self.store.all_league_seasons()
        ls_by_id = {ls.id: ls for ls in all_league_seasons}

        # Venue/ice soundness (#233 Slice E): a venue is schedulable once ANY of
        # this program's seasons has granted it active SeasonVenueAccess — the
        # legacy venue<->program bridge no longer gates readiness. There is no
        # owner-match concept here: a facility owner and a program operator are
        # deliberately independent.
        program_season_ids = {s.id for s in seasons if s.program_id in program_ids}
        venue_access_venue_ids = {
            a.venue_id for a in self.store.all_season_venue_access()
            if a.active and a.season_id in program_season_ids}
        sound_program_venue_ids = venue_access_venue_ids & {v.id for v in venues}
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
        # A League's Season link is now a LeagueSeason row; a LeagueSeason
        # pointing at a missing Season is the dangling case (#283).
        leagues_dangling = [ls for ls in all_league_seasons
                            if ls.season_id not in season_ids]
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

        # An operating organization is OPTIONAL on the canonical Program
        # (#233 B2a/ADR 0001 — operator_organization_id is nullable): a
        # Program with no operator is a complete, valid Program, so this
        # step never blocks readiness (mirrors the "division" step below).
        # v1's get_onboarding_status is untouched and keeps this blocking.
        ownership_ok = has_program and not programs_without_org
        step("program_ownership",
             "Tie every program to an operating organization (optional)",
             ownership_ok, blocking_step=False,
             detail=(f"{len(programs_without_org)} program(s) without an "
                     "operating organization" if programs_without_org
                     else "all programs have an operating organization"))
        if has_program and programs_without_org:
            warnings.append({
                "code": "program_without_organization",
                "message": f"{len(programs_without_org)} program(s) are not "
                           "tied to an operating organization."})

        venue_ok = bool(sound_program_venue_ids)
        step("venue", "Grant a season venue access", venue_ok,
             detail=(f"{len(sound_program_venue_ids)} venue(s) with active "
                     "season access" if venue_ok
                     else f"{len(venues)} venue(s), 0 with active season access"))
        if has_program:
            block(venue_ok, "no_venue_access_granted",
                  "No venue has been granted access to a season yet.")

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
        # Which Seasons have at least one grouping League participating (#283):
        # the set of Seasons named by a LeagueSeason row.
        league_season_ids = {ls.season_id for ls in all_league_seasons}
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
        has_division = any(
            self._season_id_via(ls_by_id, d.league_season_id) in season_ids
            for d in divisions)
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
            # A registration's Season + League now come from its LeagueSeason
            # (#283); an unresolvable link is invalid structure.
            ls = ls_by_id.get(reg.league_season_id)
            if ls is None:
                invalid_regs += 1
                continue
            season = seasons_by_id.get(ls.season_id)
            if season is None or season.program_id not in program_ids:
                invalid_regs += 1
                continue
            league = leagues_by_id.get(ls.league_id)
            # The LeagueSeason bundles League + Season, so cross-season league_id
            # is structurally impossible now — the League need only resolve.
            league_ok = league is not None
            team = teams_by_id.get(reg.team_id)
            # #331 review round 18: Program membership alone is not enough --
            # the registration must sit in the Team's OWN permanent League
            # (Rule 7), the same invariant register_team_for_season and
            # team_registration_valid (the shared live-scheduling resolver)
            # both enforce. transfer_team_to_league deliberately leaves an
            # archived/ended Season's active registration frozen at its OLD
            # League while Team.league_id moves on (history preservation) --
            # exactly the same-Program cross-League drift that must never be
            # counted as schedulable here, since create_game/move/publish
            # would reject it outright.
            team_ok = (team is not None and team.program_id == season.program_id
                      and team.league_id and team.league_id == ls.league_id)
            div_ok = True
            if reg.division_id:
                division = divisions_by_id.get(reg.division_id)
                # A Division belongs to the SAME LeagueSeason iff it agrees on
                # both Season and League with the registration.
                div_ok = (division is not None
                          and division.league_season_id == reg.league_season_id)
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

    def _reject_dangling_recipient(self, recipient_ref: str) -> None:
        """Reject a structured ``player:<id>``/``official:<id>``
        ``recipient_ref`` whose subject no longer exists (#232 review 2 & 3).

        Closes the same dangling-identity hole the account reactivation
        guard closes (``AccountService.set_active``): once a Player/Official
        is deleted, nothing should be able to (re)point a live integration
        row — a device token, a contact destination, or a notification
        preference — at that now-nonexistent record. Applied to
        register_device_token / set_device_token_active(active=True) and to
        set_contact_destination / set_notification_preference. Any other
        ``recipient_ref`` shape (``team:<id>``, ``guardian:<user_id>``, …)
        is untouched.
        """
        if recipient_ref.startswith("player:"):
            player_id = recipient_ref[len("player:"):]
            if self.store.get_player(player_id) is None:
                raise ValidationError(
                    "This player no longer exists.",
                    {"reason": "scope_subject_missing", "player_id": player_id})
        elif recipient_ref.startswith("official:"):
            official_id = recipient_ref[len("official:"):]
            if self.store.get_official(official_id) is None:
                raise ValidationError(
                    "This official no longer exists.",
                    {"reason": "scope_subject_missing", "official_id": official_id})

    # -- contact registry (#60) --------------------------------------------
    @staticmethod
    def _contact_row(c) -> dict:
        return {"id": c.id, "recipient_ref": c.recipient_ref,
                "channel": c.channel.value, "destination": c.destination,
                "label": c.label, "active": c.active}

    @catch
    def list_contact_destinations(self) -> dict:
        rows = [self._contact_row(c)
                for c in self.store.all_contact_destinations()]
        rows.sort(key=lambda r: (r["recipient_ref"], r["channel"]))
        return {"contacts": rows}

    @catch
    def set_contact_destination(self, recipient_ref: str, channel: str,
                                destination: str, label=None) -> dict:
        """Register (or update the value of) a recipient/channel's real
        destination."""
        if not recipient_ref:
            raise ValidationError("A recipient_ref is required.")
        self._reject_dangling_recipient(recipient_ref)
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
            # Deliberately does NOT touch `active` (#232 review 6): a
            # retired row must stay retired through an ordinary value edit —
            # only the MANAGE_SETUP-gated set_contact_destination_active can
            # reactivate one. Without this, a wider-permissioned caller
            # could silently undo a retirement by editing the destination.
            existing.destination = destination
            existing.label = label
            self.store.save_contact_destination(existing)
            return self._contact_row(existing)
        c = ContactDestination(
            id=self.store.next_id("contact"), recipient_ref=recipient_ref,
            channel=ch, destination=destination, label=label)
        self.store.add_contact_destination(c)
        return self._contact_row(c)

    @catch
    def set_contact_destination_active(self, contact_id: str, active: bool,
                                       actor_id: Optional[str] = None) -> dict:
        """Retire (or reactivate) a contact destination (#232 review 4).

        A durable, audited lifecycle toggle — never a delete. Retiring a row
        (``active=False``) is the supported way to clear a Player/Official
        delete's contact-destination dependency: the stored destination and
        its history are preserved, just no longer counted as live. Mirrors
        `set_device_token_active`; reactivating one whose Player/Official no
        longer exists is rejected the same way re-registering a device token
        is. Restricted to Player/Official-scoped rows, same as the delete
        lifecycle it serves — not a general contact-management surface for
        other recipient kinds (team, guardian, …).
        """
        c = next((row for row in self.store.all_contact_destinations()
                  if row.id == contact_id), None)
        if c is None:
            raise NotFoundError(f"Contact destination {contact_id} not found.")
        if not (c.recipient_ref.startswith("player:")
                or c.recipient_ref.startswith("official:")):
            raise ValidationError(
                "Only Player/Official-scoped contact destinations can be "
                "retired through this action.",
                {"reason": "recipient_not_cleanup_eligible",
                 "recipient_ref": c.recipient_ref})
        if active:
            self._reject_dangling_recipient(c.recipient_ref)
        # The mutation itself must happen INSIDE the transaction (#232 review
        # 6): the in-memory store snapshots state at entry, so mutating `c`
        # beforehand would already be reflected in that snapshot — a forced
        # audit failure would then roll back to the already-mutated state
        # instead of the true pre-image.
        with self.store.transaction():
            c.active = bool(active)
            self.store.save_contact_destination(c)
            self.setup._audit(
                "contact_destination_activated" if c.active
                else "contact_destination_retired",
                "contact_destination", contact_id, actor_id,
                {"recipient_ref": c.recipient_ref, "channel": c.channel.value})
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
            # id is required (#232 review 4): retiring a row via
            # set_notification_preference_active needs its id, and this read
            # is the only supported way a client can discover it — there is
            # no other route that exposes it.
            prefs.append({"id": p.id if p else None, "channel": ch.value,
                          "enabled": p.enabled if p else True,
                          "digest": p.digest if p else None,
                          "active": p.active if p else True})
        return {"recipient_ref": recipient_ref, "preferences": prefs}

    @catch
    def set_notification_preference(self, recipient_ref: str, channel: str,
                                    enabled: bool, digest=None,
                                    actor_id=None) -> dict:
        """Enable/disable (or update) a delivery channel for a
        recipient (#81)."""
        if not recipient_ref:
            raise ValidationError("A recipient_ref is required.")
        self._reject_dangling_recipient(recipient_ref)
        try:
            ch = NotificationChannel(channel)
        except ValueError:
            raise ValidationError(f"Unknown channel '{channel}'.")
        if ch not in self.PREF_CHANNELS:
            raise ValidationError(f"Channel '{channel}' is not configurable.")
        existing = self.store.get_notification_preference(recipient_ref, ch)
        prior_enabled = existing.enabled if existing is not None else None
        if existing is not None:
            # Deliberately does NOT touch `active` (#232 review 6): see
            # set_contact_destination's identical comment — only the
            # MANAGE_SETUP-gated set_notification_preference_active can
            # reactivate a retired row.
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
        return {"id": pref.id, "recipient_ref": recipient_ref, "channel": ch.value,
                "enabled": pref.enabled, "digest": pref.digest}

    @catch
    def set_notification_preference_active(self, pref_id: str, active: bool,
                                           actor_id: Optional[str] = None) -> dict:
        """Retire (or reactivate) a notification preference (#232 review 4).

        A durable, audited lifecycle toggle — never a delete. Retiring a row
        (``active=False``) is the supported way to clear a Player/Official
        delete's notification-preference dependency: the stored ``enabled``
        opt-out and its history are preserved — and still govern delivery
        exactly as before, via `services/delivery.channel_enabled` — just no
        longer counted as a live blocker. Mirrors
        `set_contact_destination_active`, including the Player/Official-only
        scope restriction.
        """
        p = next((row for row in self.store.all_notification_preferences()
                  if row.id == pref_id), None)
        if p is None:
            raise NotFoundError(f"Notification preference {pref_id} not found.")
        if not (p.recipient_ref.startswith("player:")
                or p.recipient_ref.startswith("official:")):
            raise ValidationError(
                "Only Player/Official-scoped notification preferences can "
                "be retired through this action.",
                {"reason": "recipient_not_cleanup_eligible",
                 "recipient_ref": p.recipient_ref})
        if active:
            self._reject_dangling_recipient(p.recipient_ref)
        # See set_contact_destination_active: the mutation must happen
        # INSIDE the transaction so a forced audit failure rolls back to the
        # true pre-image, not an already-mutated snapshot (#232 review 6).
        with self.store.transaction():
            p.active = bool(active)
            self.store.save_notification_preference(p)
            self.setup._audit(
                "notification_preference_activated" if p.active
                else "notification_preference_retired",
                "notification_preference", pref_id, actor_id,
                {"recipient_ref": p.recipient_ref, "channel": p.channel.value})
        return {"recipient_ref": p.recipient_ref, "channel": p.channel.value,
                "enabled": p.enabled, "active": p.active}

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
        self._reject_dangling_recipient(recipient_ref)
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
        if active:
            self._reject_dangling_recipient(t.recipient_ref)
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
    def rebind_user_account_scope(self, account_id: str, scope,
                                  actor_id: Optional[str] = None) -> dict:
        """Repair/change an account's scope binding (#266) — e.g. rebind an
        unscoped or dangling-team Coach to a real team. Audited."""
        account = self.accounts.rebind_account_scope(
            account_id, scope, actor_id=actor_id)
        return self._account_row(account)

    @catch
    def list_user_accounts(self) -> dict:
        return {"user_accounts":
                [self._account_row(a) for a in self.accounts.list_accounts()]}

    # -- production factory reset (#256) ------------------------------------
    @catch
    def factory_reset_preview(self, actor_id: str = None) -> dict:
        return self.factory_reset.preview(actor_id)

    @catch
    def factory_reset_execute(self, actor_id: str = None, password: str = None,
                              typed_phrase: str = None,
                              challenge_token: str = None,
                              backup_acknowledged=False,
                              environment: str = "production") -> dict:
        # backup_acknowledged is passed through UNCOERCED (#256 review
        # blocker 3) — FactoryResetService requires the exact JSON boolean
        # `true`, so a bool(...) here would wrongly accept "false"/"no"/1.
        return self.factory_reset.execute(
            actor_id, password, typed_phrase, challenge_token,
            backup_acknowledged, environment=environment)

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

    def _game_matches_active(self, game, season_ids, league_id, team_ids):
        """Whether ``game`` belongs to the active ``(Season, League)`` — the
        ONE Game-scoping predicate, shared by every scoped read that has to
        answer it (the Dashboard's schedule/ice/audit, and the Setup
        surface's Official-by-assignment edge). Two copies of this rule drifted
        apart once already inside a single method, so it lives here.

        A Game's own ``season_id``/``league_id`` (real, permanent fields —
        #233 Slice C1b, unrelated to any legacy naming elsewhere) are the
        direct join.

        #367 owner ruling on the league-less EXHIBITION: it "may appear under
        a selected League only when at least one participating Team validates
        into that League; otherwise omit it". An earlier revision treated
        every ``league_id is None`` game as universally eligible — "a
        league-less Team is universally eligible, so a league-less Game must
        be too". That analogy does not hold: a league-less Team has no League
        to disagree with, while an exhibition between two League B teams is
        concretely League B's fixture. It surfaced League B-vs-C exhibitions
        (with both teams' NAMES, venue and time) on League A's Dashboard.

        ``team_ids`` is the caller's ALREADY Program+League-narrowed Team
        collection (any container supporting ``in``), so membership in it IS
        the validation — the caller never has to re-derive it.
        """
        if game.season_id not in season_ids:
            return False
        if league_id is None:
            return True                          # "No League": the union
        if game.league_id is not None:
            return game.league_id == league_id
        return any(tid in team_ids
                   for tid in (game.home_team_id, game.away_team_id) if tid)

    def _division_league_season_chain(self, division_id):
        """The Division's own ``(league_season, season)`` — ``(None, None)``
        if the Division, its LeagueSeason, or Season is missing/dangling
        (#367/#369)."""
        division = self.store.get_division(division_id)
        if division is None or not division.league_season_id:
            return None, None
        league_season = self.store.get_league_season(division.league_season_id)
        if league_season is None:
            return None, None
        season = self.store.get_season(league_season.season_id)
        if season is None:
            return None, None
        return league_season, season

    def _division_matches_active_context(self, division_id, program, season,
                                         league):
        """Whether ``division_id``'s validated LeagueSeason -> Season ->
        Program chain matches the ACTIVE resolved tuple.

        Program is the hard ceiling, and a Season MUST be active and match
        EXACTLY (#367 owner ruling: "Season-bound rows must match the active
        Season; a Program-only context returns EMPTY for Season-bound data").
        Standings are Season-bound by construction — a Division reaches its
        Season only through its LeagueSeason — so a Program-only context can
        never satisfy this and returns the generic empty shape for EVERY
        Division, indistinguishably from a nonexistent one.

        An earlier revision enforced the Season axis only ``if season is not
        None``, which silently WIDENED the read exactly where it should have
        closed: a caller whose active context resolved to Program-only (a
        saved Program-only selection, or a Program whose Seasons are all
        unauthorized) could read the standings of ANY Division in that
        Program, in any Season. "No Season selected" is not "every Season" —
        the same rule ``get_demo_overview`` already applies to its
        Season-scoped collections.

        The League axis stays conditional, and that is deliberate rather than
        an inconsistency: "No League" is a first-class SELECTION meaning the
        Program+active-Season union across every League, whereas "no Season"
        is the absence of the ceiling this read is bound to.

        A caller authorized for many Programs/Seasons/Leagues must still be
        bound to the ONE it is currently active in, not merely "any of them" —
        otherwise a global League Admin active in Program A could pull
        Program B's (or another League's) standings just by being globally
        authorized, which is exactly the leak #369 review flags."""
        if program is None or season is None:
            return False
        league_season, div_season = self._division_league_season_chain(
            division_id)
        if league_season is None or div_season.program_id != program.id:
            return False
        if league_season.season_id != season.id:
            return False
        if league is not None and league_season.league_id != league.id:
            return False
        return True

    @catch
    def get_standings(self, division_id: str, user_id=None, role=None,
                      scope=None) -> dict:
        """Standings for a division from FINAL results only (#31).

        Points: win = 2, tie = 1, loss = 0. Ranked by points, then goal
        difference, then goals for, then name. Counts every division game
        (operator view); the public variant is filtered to published games.

        #369 review correction: when a real user context is supplied
        (``role`` is not ``None`` — the HTTP route always supplies one
        now), the Division's validated LeagueSeason -> Season -> Program
        chain must match the caller's ACTIVE resolved tuple
        (``ContextService.resolve_with_league``), not merely be *some*
        Program the caller is broadly authorized for — a global League
        Admin active in Program A/Season A/League A must not be able to
        pull a division from Program B, a different Season, or a different
        League just by asking for its id. A mismatch returns the same empty
        shape a nonexistent ``division_id`` already does — generic and
        non-oracle, so an inaccessible-from-here Division looks identical
        to one that doesn't exist at all. Called with no arguments beyond
        ``division_id`` (the default), performs no ownership check —
        unchanged pre-#367 behavior for existing direct/internal callers.

        #367 owner ruling: a scoped read REQUIRES an active Season and must
        match it exactly. A Program-only context is therefore
        indistinguishable from "the Division does not exist" for EVERY
        Division of that Program — see
        ``_division_matches_active_context``, which is where the whole
        tuple check lives.
        """
        if role is not None:
            program, season, league = self.context.resolve_with_league(
                user_id, role, scope)
            if not self._division_matches_active_context(
                    division_id, program, season, league):
                return {"division_id": division_id, "standings": []}
        return self._standings_for_division(division_id, public_only=False)

    def _league_season_mismatch_error(self, game, expected_ls_id) -> dict:
        """The shared fail-closed error both standings views return for a regular
        Game whose LeagueSeason identity is missing or disagrees with the
        expected one (#283) — one exact Game→LeagueSeason integrity check, so
        Division and LeagueSeason standings can never tell contradictory
        histories about the same Game."""
        return {"error": {
            "code": "data_integrity_error",
            "message": "A regular game's league-season disagrees with its "
                       "league/season; standings cannot be computed until it "
                       "is repaired.",
            "details": {"reason": "game_league_season_mismatch",
                        "game_id": game.id,
                        "league_season_id": getattr(game, "league_season_id",
                                                    None),
                        "expected_league_season_id": expected_ls_id}}}

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
        # #283 rule 10: on a DEFINITELY-ended Season, historical standings keep a
        # validly-transferred Team (its permanent League has since changed), so
        # the current-ownership check that live scheduling/draft applies is
        # skipped here — matching _standings_for_league_season. A live Season
        # still excludes a same-Program cross-League drift.
        division = self.store.get_division(division_id)
        league_season = (self.store.get_league_season(division.league_season_id)
                         if division and division.league_season_id else None)
        season = (self.store.get_season(league_season.season_id)
                  if league_season else None)
        season_ended = (season is not None and season.end_date is not None
                        and season.end_date < self.setup.clock())
        team_ids = self.setup.registered_team_ids_in_division(
            division_id, enforce_team_league=not season_ended)
        teams = [t for t in (self.store.get_team(tid) for tid in team_ids)
                 if t is not None]
        rows = {t.id: {"team_id": t.id, "team_name": t.name, "gp": 0,
                       "w": 0, "l": 0, "t": 0, "gf": 0, "ga": 0, "gd": 0, "pts": 0}
                for t in teams}
        for g in self.store.all_games():
            if g.division_id != division_id or g.cancelled:
                continue
            # #283 Slice D: EXHIBITION games are friendlies that never affect
            # standings (they also carry no division, so this is belt-and-braces
            # against a future division-tagged friendly).
            if g.game_type != GameType.REGULAR.value:
                continue
            # Public standings must never inspect, count, or disclose a hidden
            # (unpublished) Game — skip it BEFORE any integrity evaluation or
            # result lookup, so a draft Game can't leak its existence/identifiers
            # through a public data_integrity_error (#83 public-only contract).
            if public_only and not g.published:
                continue
            # #283 blocker: a regular Game claiming this Division must belong to
            # the Division's exact LeagueSeason — the SAME Game→LeagueSeason
            # integrity check LeagueSeason standings apply. A null/wrong
            # league_season_id or a disagreeing legacy (league_id, season_id)
            # pair is drift: fail closed so Division and LeagueSeason standings
            # can never count-vs-reject the same Game and tell contradictory
            # histories. The operator path sees every Game; the public path only
            # its published Games. (A dangling Division with no LeagueSeason
            # yields an empty roster, so no Game is countable there anyway.)
            if league_season is not None and not (
                    getattr(g, "league_season_id", None) == league_season.id
                    and g.league_id == league_season.league_id
                    and g.season_id == league_season.season_id):
                return self._league_season_mismatch_error(g, league_season.id)
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

    @catch
    def get_league_season_standings(self, league_id: str,
                                    season_id: str) -> dict:
        """LeagueSeason-wide standings (#283 Slice D): one table across ALL of a
        LeagueSeason's Divisions (and its division-less teams), from FINAL
        results of REGULAR games only. The per-Division ``get_standings`` view
        stays available; this is the League-level aggregate the permanent model
        makes first-class. Same points model (win=2/tie=1/loss=0) and ordering.
        """
        return self._standings_for_league_season(
            league_id, season_id, public_only=False)

    @catch
    def get_public_league_season_standings(self, league_id: str,
                                           season_id: str) -> dict:
        """Public LeagueSeason standings — published games only, so an
        unpublished final result cannot leak into the public table."""
        return self._standings_for_league_season(
            league_id, season_id, public_only=True)

    def _standings_for_league_season(self, league_id: str, season_id: str,
                                     public_only: bool = False) -> dict:
        """Compute a LeagueSeason's standings across all its Divisions.

        Roster = every active, league-consistent registration in the
        LeagueSeason (whatever its Division, or none). Games counted = REGULAR,
        non-cancelled games whose ``league_id``/``season_id`` are this
        LeagueSeason's, with a FINAL result. EXHIBITION games are excluded (they
        never affect standings). ``public_only`` skips unpublished games so the
        public table stays consistent with the public schedule.
        """
        ls = self.store.league_season_for(league_id, season_id)
        if ls is None:
            return {"error": {
                "code": "not_found",
                "message": "No such league in that season."}}
        # Roster membership in THIS LeagueSeason is the registration's canonical
        # ``league_season_id`` (every row from ``registrations_for_league_season``
        # already has it). Whether the Team's CURRENT permanent ``league_id`` must
        # still agree depends on the Season:
        #   * A DEFINITELY-ENDED Season is history (#283 rule 10): a Team validly
        #     transferred to another League afterward keeps its historical
        #     registration/Games/results here and must still appear in this table,
        #     so current ownership is NOT re-checked (same "ended" test the
        #     transfer uses to leave the registration in place).
        #   * An undated/current/future Season is live: an active registration
        #     whose Team's current permanent League differs from this League is a
        #     rule-7 violation (e.g. a migration-preserved current registration in
        #     L1 while an operator decision moved the Team to L2) and is EXCLUDED,
        #     never counted in the wrong League's standings.
        # A registration whose Team no longer exists is an orphan and is skipped.
        season = self.store.get_season(season_id)
        season_ended = (season is not None and season.end_date is not None
                        and season.end_date < self.setup.clock())
        team_ids = set()
        for reg in self.store.registrations_for_league_season(ls.id):
            if not reg.active:
                continue
            team = self.store.get_team(reg.team_id)
            if team is None:
                continue
            if not season_ended and team.league_id != league_id:
                continue  # live-Season rule-7 mismatch — never counted here
            team_ids.add(reg.team_id)
        teams = [t for t in (self.store.get_team(tid) for tid in team_ids)
                 if t is not None]
        rows = {t.id: {"team_id": t.id, "team_name": t.name, "gp": 0,
                       "w": 0, "l": 0, "t": 0, "gf": 0, "ga": 0, "gd": 0,
                       "pts": 0}
                for t in teams}
        for g in self.store.all_games():
            if g.cancelled or g.game_type != GameType.REGULAR.value:
                continue
            # #283 blocker: the LeagueSeason is a regular Game's single
            # competition identity — count by ``league_season_id``, never the
            # redundant legacy (league_id, season_id) pair. A regular Game that
            # concerns this LeagueSeason by EITHER identity but whose
            # league_season_id is missing or disagrees with its legacy fields is
            # drift: fail closed (never silently count it in the wrong table or
            # omit it) so the row is repaired before standings are trusted.
            ls_id = getattr(g, "league_season_id", None)
            by_ls = ls_id == ls.id
            by_legacy = g.league_id == league_id and g.season_id == season_id
            if not (by_ls or by_legacy):
                continue  # belongs to some other LeagueSeason
            # Public standings must never inspect, count, or disclose a hidden
            # (unpublished) Game — skip it BEFORE the integrity check or result
            # lookup, so a draft Game can't leak via a public
            # data_integrity_error (#83). The operator path still fails closed on
            # every drifted Game.
            if public_only and not g.published:
                continue
            if by_ls != by_legacy:
                return self._league_season_mismatch_error(g, ls.id)
            r = self.store.result_for_game(g.id)
            if r is None or r.status != ResultStatus.FINAL:
                continue
            home, away = rows.get(g.home_team_id), rows.get(g.away_team_id)
            if home is None or away is None:
                continue
            self._apply_result(home, r.home_score, r.away_score)
            self._apply_result(away, r.away_score, r.home_score)
        ranked = sorted(rows.values(),
                        key=lambda x: (-x["pts"], -x["gd"], -x["gf"],
                                       x["team_name"]))
        return {"league_id": league_id, "season_id": season_id,
                "standings": ranked}

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

    # -- season scheduler v1 (#84, extended #233 Slice G) -------------------
    @catch
    def draft_season_schedule(self, division_id: str = None,
                              season_id: str = None, league_id: str = None,
                              slot_ids=None, constraints=None,
                              meetings_per_opponent=None) -> dict:
        """Generate a draft round-robin schedule for a Division, or for a
        whole League within a Season optionally narrowed to one Division
        (#84/#85, extended #233 Slice G).

        Returns a proposal only — no games are created or published here. The
        result is deterministic and safe to regenerate. ``constraints`` may
        carry team/rink/season blackout dates, holiday dates, minimum rest,
        and a max games/team/day cap. Pass ``season_id`` and ``league_id``
        (the canonical grouping League, ``store.get_league``) for the
        League-wide entry point instead of ``division_id`` alone;
        ``division_id`` then optionally narrows that League-wide draft to one
        Division rather than switching to the Division-only entry point.

        ``meetings_per_opponent`` (#375) is the configurable regular-season
        format: how many times each team plays every other. Omitted (or
        ``None``) keeps the historical single round-robin. Existing
        non-cancelled REGULAR Games count toward the requirement — cancelled
        and exhibition games do not — so regenerating after a partial
        schedule proposes only the meetings still missing.
        """
        if season_id and league_id:
            return draft_schedule_for_league(
                self.store, season_id, league_id, division_id=division_id,
                slot_ids=slot_ids, constraints=constraints,
                meetings_per_opponent=meetings_per_opponent)
        if not division_id:
            raise ValidationError(
                "A division_id, or a season_id and league_id, is required.")
        if self.store.get_division(division_id) is None:
            raise NotFoundError("Division not found.")
        return draft_schedule(self.store, division_id, slot_ids=slot_ids,
                              constraints=constraints,
                              meetings_per_opponent=meetings_per_opponent)

    # -- immutable named schedule scenarios (#206/#378) ------------------
    @staticmethod
    def _scenario_dto(scenario) -> dict:
        """A detached JSON view; callers can never mutate stored evidence."""
        return {
            "scenario_id": scenario.id,
            "name": scenario.name,
            "scope": {
                "program_id": scenario.program_id,
                "season_id": scenario.season_id,
                "league_id": scenario.league_id,
                "league_season_id": scenario.league_season_id,
                "division_id": scenario.division_id,
            },
            "planner": {
                "version": scenario.planner_version,
                "input_fingerprint": scenario.input_fingerprint,
                "proposal_fingerprint": scenario.proposal_fingerprint,
                "draft_fingerprint": scenario.proposal.get(
                    "draft_fingerprint"),
            },
            "request_input": copy.deepcopy(scenario.request_input),
            # Opaque by contract: scenario persistence does not interpret or
            # reorder proposal/explanation fields owned by the generator.
            "proposal": copy.deepcopy(scenario.proposal),
            "generation_snapshot": copy.deepcopy(
                scenario.generation_snapshot),
            "created_at": scenario.created_at.isoformat(),
            "created_by": scenario.created_by,
        }

    @catch
    def create_schedule_scenario(self, name: str, division_id: str = None,
                                 season_id: str = None,
                                 league_id: str = None, slot_ids=None,
                                 constraints=None, actor_id=None) -> dict:
        """Generate and persist review evidence without writing any Game.

        REPEATABLE READ makes proposal generation, the material-input snapshot,
        and the one scenario INSERT observe a single database snapshot.  The
        scenario remains immutable afterward; creating another generation is a
        new named record with a new id, never an update in place.
        """
        if not isinstance(name, str) or not name.strip():
            raise ValidationError(
                "A scenario name is required.",
                {"reason": "scenario_name_required"})
        clean_name = name.strip()
        if len(clean_name) > 120:
            raise ValidationError(
                "A scenario name must be 120 characters or fewer.",
                {"reason": "scenario_name_too_long", "max_length": 120})
        if slot_ids is not None and (
                not isinstance(slot_ids, (list, tuple))
                or any(not isinstance(value, str) for value in slot_ids)):
            raise ValidationError(
                "slot_ids must be a list of strings.",
                {"reason": "scenario_slot_ids_invalid"})
        if constraints is not None and not isinstance(constraints, dict):
            raise ValidationError(
                "constraints must be an object.",
                {"reason": "scenario_constraints_invalid"})

        request_input = {
            "scope_mode": "league" if season_id and league_id else "division",
            "slot_ids": copy.deepcopy(list(slot_ids))
            if slot_ids is not None else None,
            "constraints": copy.deepcopy(constraints),
        }
        with self.store.transaction(isolation="REPEATABLE READ"):
            scope = resolve_scenario_scope(
                self.store, division_id=division_id, season_id=season_id,
                league_id=league_id)
            proposal = self.draft_season_schedule(
                division_id=division_id, season_id=season_id,
                league_id=league_id, slot_ids=request_input["slot_ids"],
                constraints=request_input["constraints"])
            if isinstance(proposal, dict) and proposal.get("error"):
                return proposal
            snapshot = material_input_snapshot(
                self.store, scope, request_input,
                planner_version=SCENARIO_PLANNER_VERSION)
            scenario = ScheduleScenario(
                id=self.store.next_id("schedule_scenario"),
                name=clean_name,
                program_id=scope["program_id"],
                season_id=scope["season_id"],
                league_id=scope["league_id"],
                league_season_id=scope["league_season_id"],
                division_id=scope["division_id"],
                planner_version=SCENARIO_PLANNER_VERSION,
                input_fingerprint=canonical_fingerprint(snapshot),
                # Whole opaque output, not only the v1 commit token.  Future
                # explainability fields stay bound without this persistence
                # layer learning or redesigning any of them.
                proposal_fingerprint=canonical_fingerprint(proposal),
                request_input=request_input,
                proposal=copy.deepcopy(proposal),
                generation_snapshot=snapshot,
                created_at=self.setup.clock(),
                created_by=actor_id,
            )
            stored = self.store.add_schedule_scenario(scenario)
            self.setup._audit(
                "schedule_scenario_created", "schedule_scenario",
                scenario.id, actor_id,
                {"scenario_name": scenario.name,
                 "program_id": scenario.program_id,
                 "season_id": scenario.season_id,
                 "league_id": scenario.league_id,
                 "division_id": scenario.division_id,
                 "input_fingerprint": scenario.input_fingerprint,
                 "proposal_fingerprint": scenario.proposal_fingerprint})
        return self._scenario_dto(stored)

    @catch
    def get_schedule_scenario(self, scenario_id: str) -> dict:
        scenario = self.store.get_schedule_scenario(scenario_id)
        if scenario is None:
            raise NotFoundError(
                "Schedule scenario not found.",
                details={"reason": "schedule_scenario_missing",
                         "scenario_id": scenario_id})
        return self._scenario_dto(scenario)

    @catch
    def list_schedule_scenarios(self) -> dict:
        scenarios = sorted(
            self.store.all_schedule_scenarios(),
            key=lambda value: (value.created_at, value.id))
        return {"scenarios": [self._scenario_dto(value)
                              for value in scenarios]}

    def _commit_schedule_scenario_attempt(self, scenario_id, actor_id=None):
        """One lock-linearized, all-or-nothing stale-check + draft commit."""
        with self.store.transaction():
            scenario = self.store.get_schedule_scenario_for_update(scenario_id)
            if scenario is None:
                raise NotFoundError(
                    "Schedule scenario not found.",
                    details={"reason": "schedule_scenario_missing",
                             "scenario_id": scenario_id})

            scope = {
                "program_id": scenario.program_id,
                "season_id": scenario.season_id,
                "league_id": scenario.league_id,
                "league_season_id": scenario.league_season_id,
                "division_id": scenario.division_id,
            }
            integrity_fields = []
            if (canonical_fingerprint(scenario.generation_snapshot)
                    != scenario.input_fingerprint):
                integrity_fields.append("generation_snapshot")
            if (canonical_fingerprint(scenario.proposal)
                    != scenario.proposal_fingerprint):
                integrity_fields.append("proposal")
            if integrity_fields:
                raise ValidationError(
                    "This schedule scenario failed its immutable-record "
                    "integrity check and cannot be committed. Contact an "
                    "administrator before retrying.",
                    {"reason": "schedule_scenario_integrity_error",
                     "scenario_id": scenario.id,
                     "fields": integrity_fields})

            def _guard_material_inputs():
                # This callback runs only after the existing scheduler commit
                # gate holds its Program/Team/Rink/Season lock plan.  That
                # gives the comparison a shared, explicit linearization point:
                # a material writer either committed before these reads or is
                # blocked until the all-or-nothing draft transaction finishes.
                current_snapshot = material_input_snapshot(
                    self.store, scope, scenario.request_input,
                    planner_version=SCENARIO_PLANNER_VERSION)
                current_fingerprint = canonical_fingerprint(current_snapshot)
                changed = changed_snapshot_sections(
                    scenario.generation_snapshot, current_snapshot)
                if (scenario.planner_version != SCENARIO_PLANNER_VERSION
                        or current_fingerprint != scenario.input_fingerprint):
                    if (scenario.planner_version != SCENARIO_PLANNER_VERSION
                            and "planner_version" not in changed):
                        changed.insert(0, "planner_version")
                    raise ConcurrencyConflictError(
                        "This schedule scenario is stale because material "
                        "inputs changed after generation. Generate a new "
                        "named scenario, review it, and commit that new "
                        "scenario instead.",
                        {"reason": "schedule_scenario_stale",
                         "scenario_id": scenario.id,
                         "changed_inputs": changed,
                         "required_action": "generate_new_scenario",
                         "generated_input_fingerprint":
                             scenario.input_fingerprint,
                         "current_input_fingerprint": current_fingerprint})

            guard_team_ids = tuple(
                row.get("team_id")
                for row in (scenario.generation_snapshot
                            .get("registrations", {}).get("rows", ()))
                if row.get("team_id"))
            guard_venue_ids = tuple(
                row.get("id")
                for row in (scenario.generation_snapshot
                            .get("venue_access", {}).get("venues", ()))
                if row.get("id"))
            guard_games = scenario.generation_snapshot.get("games", ())
            guard_game_ids = tuple(
                row.get("id") for row in guard_games if row.get("id"))
            guard_season_ids = tuple(
                row.get("season_id")
                for row in guard_games if row.get("season_id"))

            request_input = scenario.request_input
            league_mode = request_input.get("scope_mode") == "league"
            # The existing commit gate remains authoritative for per-row
            # placement, participation, pairing, and lock-order checks.  Passing
            # the immutable proposal prevents a fresh regeneration from silently
            # substituting a different batch after review.
            result = self._commit_draft_schedule_attempt(
                division_id=scenario.division_id,
                season_id=scenario.season_id if league_mode else None,
                league_id=scenario.league_id if league_mode else None,
                slot_ids=copy.deepcopy(request_input.get("slot_ids")),
                constraints=copy.deepcopy(request_input.get("constraints")),
                draft_fingerprint=scenario.proposal.get("draft_fingerprint"),
                actor_id=actor_id,
                reviewed_proposal=scenario.proposal,
                scenario_guard=_guard_material_inputs,
                guard_team_ids=guard_team_ids,
                guard_venue_ids=guard_venue_ids,
                guard_game_ids=guard_game_ids,
                guard_season_ids=guard_season_ids)
            self.setup._audit(
                "schedule_scenario_committed", "schedule_scenario",
                scenario.id, actor_id,
                {"scenario_name": scenario.name,
                 "input_fingerprint": scenario.input_fingerprint,
                 "created_count": len(result.get("created") or ())})
            return {"scenario_id": scenario.id, **result}

    @catch
    def commit_schedule_scenario(self, scenario_id: str,
                                 actor_id=None) -> dict:
        """Commit one stored scenario; retry only transaction-level races.

        Transient lock-plan races retry from a fresh transaction. A material
        input mutation that won before the scheduler locks is reported as
        ``schedule_scenario_stale`` with section-level guidance.
        """
        transient = {
            "placement_raced", "serialization_failure",
            "deadlock_detected", "lock_not_available",
        }
        for attempt in range(3):
            try:
                return self._commit_schedule_scenario_attempt(
                    scenario_id, actor_id=actor_id)
            except ConcurrencyConflictError as exc:
                reason = (exc.details or {}).get("reason")
                if reason not in transient or attempt == 2:
                    raise

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

    def _reserved_span(self, slot, game, policy_cache) -> Optional[dict]:
        """#277 Slice B — the derived reserved facility span around a slot
        HOSTING a game: warm-up before + resurfacing after, from the
        effective Rink>Season>Program policy resolved via the hosting
        game's OWN season (cached per (rink, season) in the caller's
        per-request dict). Committed drafts reserve ice exactly like
        published games — the placement gate counts them — while a free
        slot, a cancelled game, and a zero-buffer policy all stay ``None``:
        the calendar and the review views paint real blocked ice, never a
        guess about a future booking's season. ONE derivation shared by the
        operator calendar, the schedule rows, and the draft-review rows, so
        the surfaces cannot disagree."""
        if (slot is None or game is None or game.cancelled
                or not game.season_id):
            return None
        key = (slot.rink_id, game.season_id)
        if key not in policy_cache:
            policy_cache[key] = self.setup._effective_policy(*key)[0]
        values = policy_cache[key]
        warmup = values["warmup_minutes"] or 0
        resurf = values["resurfacing_minutes"] or 0
        if not warmup and not resurf:
            return None
        return {
            "warmup_minutes": warmup,
            "resurfacing_minutes": resurf,
            "reserved_start_time":
                (slot.start_time - timedelta(minutes=warmup)).isoformat(),
            "reserved_end_time":
                (slot.end_time + timedelta(minutes=resurf)).isoformat(),
        }

    # A roster in either of these states is ready to play — the same bar the
    # operator dashboard's gameTriage() already holds games to client-side;
    # kept here as the single source of truth for the review issue below.
    _ROSTER_READY_STATUSES = frozenset({"roster_confirmed", "locked"})

    def _draft_review_row(self, g, slot_games: dict, double_booked: bool,
                          policy_cache=None) -> dict:
        """Enriched per-draft-game row for the scheduler review screen (#106):
        officials/roster posture and any review issues, so an operator can
        spot problems before publishing rather than discovering them after.
        Carries the same derived ``reserved`` span as the operator calendar
        (#277 Slice B) — a committed draft physically blocks warm-up +
        resurfacing ice, and the review screen must show it."""
        if policy_cache is None:
            policy_cache = {}
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
            "reserved": self._reserved_span(slot, g, policy_cache),
        }

    def _guard_active_seasons(self, season_ids) -> None:
        """Row-lock every distinct Season in canonical (sorted) order and
        guard it read-only (#159). Sorted order avoids lock-order deadlocks
        across a multi-Season batch; MUST run inside a store.transaction()."""
        for sid in sorted({s for s in season_ids if s}):
            self.setup._require_active_season(sid)

    @catch
    def commit_draft_schedule(self, division_id: str = None,
                              season_id: str = None, league_id: str = None,
                              slot_ids=None, constraints=None,
                              draft_fingerprint: str = None,
                              meetings_per_opponent=None,
                              actor_id=None) -> dict:
        """Retry shell (#318): ``placement_raced`` marks the batch's
        pre-lock scope locator invalidated by a concurrent commit; each
        retry regenerates the proposal and re-plans in a FRESH transaction,
        so callers receive the precise terminal answer — mirroring
        ``create_game``'s loop and ``move_game``'s ``_retry_on_move_race``.
        The whole body lives in ``_commit_draft_schedule_attempt``; the
        league-scoped facade overrides the attempt and inherits this
        shell.

        ``meetings_per_opponent`` (#375) must be the SAME format the
        reviewed preview was generated with — it is bound into
        ``draft_fingerprint``, so a mismatch is refused as ``preview_stale``
        rather than silently committing a different-sized schedule.

        ``draft_fingerprint`` (#328 review round 5) must be the
        ``draft_fingerprint`` returned by the ``draft_season_schedule`` call
        the caller actually reviewed — see ``_commit_draft_schedule_attempt``
        for what it guards against. It is passed through unchanged on every
        retry: a retry only re-runs the SAME reviewed commit against a fresh
        transaction, it does not re-open review of a new proposal."""
        for _attempt in range(3):
            try:
                return self._commit_draft_schedule_attempt(
                    division_id=division_id, season_id=season_id,
                    league_id=league_id, slot_ids=slot_ids,
                    constraints=constraints,
                    draft_fingerprint=draft_fingerprint,
                    meetings_per_opponent=meetings_per_opponent,
                    actor_id=actor_id)
            except ConcurrencyConflictError as exc:
                if ((exc.details or {}).get("reason") != "placement_raced"
                        or _attempt == 2):
                    raise

    def _commit_draft_schedule_attempt(self, division_id: str = None,
                                       season_id: str = None,
                                       league_id: str = None,
                                       slot_ids=None, constraints=None,
                                       draft_fingerprint: str = None,
                                       meetings_per_opponent=None,
                                       actor_id=None,
                                       reviewed_proposal=None,
                                       scenario_guard=None,
                                       guard_team_ids=(),
                                       guard_venue_ids=(),
                                       guard_game_ids=(),
                                       guard_season_ids=()) -> dict:
        """Persist a generated draft as draft games (is_draft=True, unpublished),
        so they can be reviewed and then published (#86). Regenerates the
        proposal server-side (deterministic) and returns the created drafts +
        any unscheduled pairings. Accepts the same Division-only or
        Season+League(+optional Division) scope as ``draft_season_schedule``
        (#233 Slice G).

        Every created Game's ``season_id``/``league_id``/``division_id`` are
        read off the regenerated PROPOSAL, not the request params: a
        League-wide draft's rows can span several Divisions (or none), so
        each row carries its own resolved ``division_id`` while
        ``season_id``/``league_id`` are the same canonical values across the
        whole batch (#233 Slice G — previously these were left unset
        entirely, so a committed draft game had no queryable competition
        scope of its own).

        #328 review round 5 — ``draft_fingerprint`` binds this commit to the
        EXACT ``already_scheduled``/``draft_games`` split the caller
        reviewed via a prior ``draft_season_schedule`` call, mirroring the
        ice-availability builder's preview-token binding
        (``SetupService.commit_ice_availability``). Scope+constraints alone
        are not enough: the operator may review a preview on screen for
        seconds or minutes before clicking Commit, and this method's own
        regeneration below only ever reflects the CURRENT instant — without
        this check, a Game created for one of the reviewed pairings in that
        window would silently shrink the committed batch (quietly moved
        into ``already_scheduled``), and a previously-blocking Game
        cancelled in that window would silently grow it (a pairing the
        operator never reviewed newly appears and gets committed). Missing
        entirely is a caller error (``preview_required``, mirroring
        ice-availability); present but not matching the freshly regenerated
        proposal's own fingerprint means the world moved and the reviewed
        batch is no longer valid (``preview_stale``) — both refuse before
        any lock or write. This is deliberately checked BEFORE the
        transaction: it is a coarse, wide-window gate (the whole
        preview-to-commit-click gap); the narrower race in the gap between
        THIS regeneration and taking the locks below remains
        ``_existing_now``'s job, unaffected by this check.
        """
        # Named scenarios (#378) pass their exact immutable reviewed proposal
        # after independently revalidating the material-input snapshot in the
        # same outer transaction.  The legacy route leaves this unset and keeps
        # its existing regenerate-from-request behavior byte-for-byte.
        proposal = (copy.deepcopy(reviewed_proposal)
                    if reviewed_proposal is not None
                    else self.draft_season_schedule(
                        division_id=division_id, season_id=season_id,
                        league_id=league_id, slot_ids=slot_ids,
                        constraints=constraints,
                        meetings_per_opponent=meetings_per_opponent))
        if isinstance(proposal, dict) and proposal.get("error"):
            return proposal
        if draft_fingerprint is None:
            raise ValidationError(
                "Generate a preview before committing this schedule.",
                {"reason": "preview_required"})
        if draft_fingerprint != proposal.get("draft_fingerprint"):
            raise ConcurrencyConflictError(
                "This preview is out of date — a game may have been added, "
                "cancelled, or otherwise changed since you generated it. "
                "Generate a fresh preview and review it before committing.",
                {"reason": "preview_stale"})
        resolved_season_id = proposal["season_id"]
        # The Division-only proposal's own "league_id" is this tenancy
        # layer's frozen Program-scoped vocabulary (league_scope.py's
        # league_id_for_division -> season.program_id, see scope_bridge.py's
        # docstring) — NOT the canonical grouping League Game.league_id
        # actually stores. Resolve the CANONICAL league straight from the
        # Division for that path; the League-wide path already received the
        # canonical league_id as an explicit param, so its own proposal
        # value is correct as-is.
        if season_id and league_id:
            canonical_league_id = proposal["league_id"]
        else:
            division = self.store.get_division(division_id) if division_id else None
            # Division.league_id is now resolved through its LeagueSeason (#283).
            ls = self._resolve_ls(division.league_season_id) if division else None
            canonical_league_id = ls.league_id if ls else None
        # #159 — lock the target Season and do EVERY Game/audit write in one
        # transaction, so a concurrent archive cannot commit between the guard
        # and the writes (autocommit would release the FOR UPDATE lock at the
        # end of the check) and the batch stays all-or-nothing.
        with self.store.transaction():
            # #277/#313/#318 — the global Program→Team→Rink→Season lock
            # order: the Program rows (policy scopes the per-row gate reads —
            # the batch's own and every neighbor's on the target rinks) come
            # FIRST, matching the ice-availability builder's
            # Program→Rink→Season (Program-last was an ABBA deadlock against
            # it); then every team the batch places, every target rink, and
            # every involved Season in one sorted batch, so each per-row
            # check + ALLOCATE is atomic against a concurrent placement
            # sharing a team, against the builder on those rinks, and
            # against any scheduling-policy edit the gate would read. The
            # scope locator is a plain pre-lock read, re-verified under the
            # locks. See SetupService._policy_scope_lock_plan / _lock_teams /
            # _lock_rinks for the ordering contract.
            # #328 review round 13 -- the candidate Rink set is not just the
            # rinks of THIS proposal's placed rows: when the caller omits
            # slot_ids (the real Scheduler UI's own call), draft_season_schedule
            # considers every Rink with active SeasonVenueAccess for this
            # Season (`season_candidate_rink_ids`, computed the same way,
            # Rink-first so a Rink with ZERO existing slots is still
            # included) -- so an eligible-but-currently-unplaced Rink
            # gaining its first or another usable slot, having a candidate
            # slot allocated, or having its effective policy change is
            # invisible to a lock plan built only from placed rows.
            # Recomputing the SAME candidate pool the generator itself
            # scans, not just its output, keeps the locked set and the
            # generator's read set identical by construction -- current and
            # future, not one more hand-picked dimension.
            _pre_rinks = season_candidate_rink_ids(
                self.store, resolved_season_id, slot_ids)
            _plan = self.setup._policy_scope_lock_plan(
                _pre_rinks, (resolved_season_id,))
            _plan["seasons"].update(
                sid for sid in guard_season_ids if sid)
            for _sid in _plan["seasons"]:
                _season = self.store.get_season(_sid)
                if _season is not None and _season.program_id:
                    _plan["programs"].add(_season.program_id)
            self.setup._lock_programs(_plan["programs"])
            # Scenario snapshots include the Venue timezone used for local
            # policy/blackout interpretation. Existing hierarchy writes take
            # the Venue row's UPDATE lock, so lock those exact rows before the
            # Team/Rink/Season chain and hold them through the snapshot guard.
            for _venue_id in sorted({v for v in guard_venue_ids if v}):
                self.store.get_venue_for_update(_venue_id)
            # #328 review round 8/9 -- also lock every already_scheduled
            # row's Teams, not just draft_games': the revalidation below
            # (whether that row's existing_game_id is still the current
            # non-cancelled Game for its pairing) is only genuinely
            # race-free, not just incidentally so, if a concurrent write
            # touching one of those Teams (a cancel, a re-pairing) is
            # forced to serialize against this transaction the same way
            # draft_games' own teams already are.
            self.setup._lock_teams(
                set(guard_team_ids) | {
                    t for d in (proposal["draft_games"]
                                + proposal["already_scheduled"])
                    for t in (d["home_team_id"], d["away_team_id"])
                })
            # #328 review round 13 -- recomputed fresh (not reused from
            # `_pre_rinks` above) under the Team locks just acquired, exactly
            # mirroring the pre-existing draft_games-only pattern this
            # replaces: the candidate pool itself, not just which rows placed
            # against it, can have changed in the gap since the locator read.
            _batch_rinks = season_candidate_rink_ids(
                self.store, resolved_season_id, slot_ids)
            self.setup._lock_rinks(_batch_rinks)
            self.setup._lock_seasons(_plan["seasons"])
            self._guard_active_seasons([resolved_season_id])
            self.setup._verify_policy_scope_plan(
                _plan, _batch_rinks, season_ids=(resolved_season_id,))
            # #328 review round 14 -- _batch_rinks above is still computed
            # and locked BEFORE the Season lock (Program->Team->Rink->Season
            # order), so a concurrent grant_season_venue_access (which takes
            # only the Season lock -- see SetupService.grant_season_venue_access)
            # can make a new Rink season-eligible in the exact gap between
            # that computation and the Season lock just acquired above.
            # That Rink's row was never in _batch_rinks and so was never
            # locked by _lock_rinks -- a concurrent create_ice_slot (which
            # takes only a Rink lock) can then give it its first candidate
            # slot at any point up to and including after the locked-regen
            # fingerprint recheck below (which sees no change while that
            # Rink still has zero slots) but before this commit's writes
            # complete. Re-verify the candidate set now that the Season
            # lock closes that gap for good: from here on our own Season
            # lock blocks any further grant, so any drift here proves the
            # inventory changed strictly before we started holding it, and
            # the whole attempt must restart against a fresh lock plan
            # rather than proceed against a scope we never actually locked.
            if season_candidate_rink_ids(
                    self.store, resolved_season_id, slot_ids) != _batch_rinks:
                raise ConcurrencyConflictError(
                    "A scheduling-policy scope changed while processing "
                    "the request; please retry.",
                    {"reason": "placement_raced"})
            # Existing Game mutations lock their Season before writing the
            # Game. Locking the generated snapshot's exact rows after all
            # Seasons matches that order and closes legacy/row-only updates;
            # new relevant Games are already serialized by the registered
            # Team or candidate Rink locks above.
            for _game_id in sorted({g for g in guard_game_ids if g}):
                self.store.get_game_for_update(_game_id)
            if scenario_guard is not None:
                scenario_guard()
            # Resolve the exact competition identity only after the Season lock.
            # If a concurrent unbind won first, fail closed; if it is queued
            # behind us, its dependency scan will see the Games created below.
            draft_ls = self.store.league_season_for(
                canonical_league_id, resolved_season_id
            ) if canonical_league_id else None
            if draft_ls is None:
                raise ValidationError(
                    "The draft's League is not linked to this Season.",
                    {"reason": "draft_league_season_missing",
                     "league_id": canonical_league_id,
                     "season_id": resolved_season_id})
            draft_ls_id = draft_ls.id
            # #328 review round 12 finding 1 -- #314's participation check
            # and round 11's general fingerprint recheck (both below) must
            # run AFTER `_existing_now`, not before: with
            # `_require_batch_team_participation` first (the original
            # order), a team unregistering in the exact race window round 11
            # closed surfaced as `team_not_registered` -- a
            # DivisionMismatchError reason app.js's stale-preview recovery
            # does not recognise, leaving the operator a generic toast with
            # no clear-preview/refocus UX, instead of the terminal,
            # recoverable `preview_stale` every OTHER cause of staleness in
            # this same window produces. Symmetrically, a winning
            # exact-pairing race landing in that window surfaced as the
            # general recheck's generic `preview_stale` instead of the
            # specific, product-confirmed `pairing_already_scheduled` naming
            # the pairing and winning Game -- a regression against round
            # 2/3/4's own accepted contract for that exact scenario, which
            # this reordering also restores.
            # #206 slice 1 — freshly computed HERE, under the Team locks just
            # acquired, so the read is race-free against any other writer
            # touching these exact teams. Checked per row BELOW, BEFORE that
            # row's own slot/team-overlap check runs (#328 review round 4 —
            # reversed from the original design): a row whose exact pairing
            # already has a real Game is a terminal, product-confirmed fact
            # that must win regardless of whether the SAME row would ALSO
            # fail the physical check — the winning Game happens to sit on
            # this row's own slot, or a slot that overlaps it — because
            # `pairing_already_scheduled` names the specific pairing and
            # winning Game, which the physical-feasibility diagnoses
            # (`team_overlap`, `slot_unavailable`) cannot. This is a residual
            # check for what those checks structurally can't see either way
            # — a genuinely free, non-conflicting slot proposed for a
            # pairing that already has a real Game elsewhere, created by a
            # concurrent write between this proposal's generation and this
            # commit (the proposal already excluded pairings that existed AT
            # PREVIEW time — scheduler.py's already_scheduled split — so
            # only such a race can slip one through here). An UNRELATED
            # pairing's physical conflict is not in this index for that
            # row's own key, so it still falls through to the unchanged
            # physical check below. #328 review round 2 — this is a TERMINAL
            # fact, not transient contention: the operator reviewed a
            # specific proposal, and a winning commit already changed what
            # "missing" means, so silently substituting a different pairing
            # into the SAME commit would diverge from what was reviewed. The
            # batch rolls back atomically (raising inside this transaction)
            # and the caller must regenerate and re-review before retrying —
            # `pairing_already_scheduled` is deliberately NOT
            # `placement_raced` (the retry shell only retries that one
            # reason), so this reaches the caller unretried. #328 review
            # round 12 -- also reused below by the general fingerprint
            # recheck's winning-pairing carve-out, under this identical
            # snapshot.
            _existing_now = _existing_pairing_games(
                self.store,
                {(draft_ls_id, d.get("division_id"))
                 for d in proposal["draft_games"]}
                | {(draft_ls_id, a.get("division_id"))
                   for a in proposal["already_scheduled"]})
            # #375 — the race checks below ask "does this pairing have MORE
            # existing Games than the reviewed proposal accounted for?",
            # not the pre-#375 "does it have any?". With a configurable
            # format a pairing can legitimately have K existing Games AND
            # still be in this batch for its remaining N - K, so the bare
            # existence test would refuse every N > 1 commit against a
            # partially-scheduled Division. At N = 1 the reviewed count for
            # any pairing with a draft_games row is 0 and this reduces
            # exactly to the old predicate.
            _reviewed_counts = reviewed_existing_counts(
                proposal["already_scheduled"], draft_ls_id)
            # #328 review round 11 finding 2 -- the checks immediately below
            # only revalidate draft_games/already_scheduled row identity and
            # participation; a team's ELIGIBILITY changing in the narrow gap
            # between this method's own pre-lock regeneration/fingerprint
            # compare and the locks just acquired is invisible to all of
            # them, since a newly-registered (or newly-unregistered) team
            # need not touch any row actually in this batch to change what a
            # fresh preview would show. Regenerating the complete current
            # proposal HERE -- now that every lock a proposal's inputs depend
            # on (Program/Team/Rink/Season) is held -- and comparing its own
            # fingerprint against the one the operator's Generate call
            # actually returned is one general check covering every
            # fingerprint-bound dimension at once (current and future),
            # rather than hand-listing each one. A concurrent
            # register_team_for_season/unregister_team_from_season also
            # locks the Season row (require_active_season), so by the time
            # this transaction holds it, any such write has either already
            # committed (and this regeneration observes it) or is blocked
            # behind this transaction (and cannot land before the writes
            # below).
            _locked_proposal = self.draft_season_schedule(
                division_id=division_id, season_id=season_id,
                league_id=league_id, slot_ids=slot_ids,
                constraints=constraints,
                meetings_per_opponent=meetings_per_opponent)
            if _locked_proposal.get("draft_fingerprint") != draft_fingerprint:
                # #328 review round 12 finding 1 -- a mismatch here can be
                # fully explained by a winning exact-pairing race: one of
                # THIS proposal's own draft_games rows now has a real Game
                # for its exact pairing in `_existing_now` above, the same
                # terminal fact the per-row loop below independently
                # detects. When that's the case, raise the specific,
                # product-confirmed `pairing_already_scheduled` (naming the
                # pairing and winning Game) instead of the generic
                # `preview_stale`, matching round 2/3/4's accepted contract
                # for this scenario exactly rather than silently downgrading
                # it just because this general recheck now runs first.
                # #375 — "raced" is now a COUNT comparison, not a presence
                # test: the pairing gained a Game beyond the ones the
                # reviewed proposal already reported as already-scheduled.
                _raced_gid, _raced_row = None, None
                for d in proposal["draft_games"]:
                    _gid = raced_pairing_game_id(
                        _existing_now, _reviewed_counts,
                        (draft_ls_id, d.get("division_id"),
                         frozenset((d["home_team_id"], d["away_team_id"]))))
                    if _gid is not None:
                        _raced_gid, _raced_row = _gid, d
                        break
                if _raced_row is not None:
                    raise ConcurrencyConflictError(
                        f"{_raced_row['home_team_name']} vs "
                        f"{_raced_row['away_team_name']} is already "
                        f"scheduled as Game {_raced_gid} — generate a "
                        "fresh preview before committing again.",
                        {"reason": "pairing_already_scheduled",
                         "home_team_id": _raced_row["home_team_id"],
                         "away_team_id": _raced_row["away_team_id"],
                         "existing_game_id": _raced_gid})
                raise ConcurrencyConflictError(
                    "This preview is out of date — a game may have been "
                    "added, cancelled, or otherwise changed since you "
                    "generated it. Generate a fresh preview and review it "
                    "before committing.",
                    {"reason": "preview_stale"})
            # #314 review — also re-validate every proposed row's competition
            # participation HERE, under the same locks: a concurrent
            # unregister_team_from_season or a team-to-league transfer can
            # commit in the SAME gap a stale pre-lock proposal would miss, after
            # which the write would persist a Game for a team no longer a valid
            # participant. Reuses the identical check create_game enforces.
            # #328 review round 12 finding 1 -- now redundant-but-harmless
            # defense-in-depth: the general fingerprint recheck above already
            # classifies any such change as `preview_stale` (or the more
            # specific `pairing_already_scheduled`) first, since a changed
            # participant is itself part of what the regenerated proposal's
            # fingerprint binds (team_ids / unschedulable_teams, round 10).
            # Left in place on the same established precedent as the other
            # narrower checks below: more specific where it can still add
            # anything, harmless where it can't.
            self.setup._require_batch_team_participation(
                resolved_season_id, draft_ls_id, proposal["draft_games"])
            # #314 review — re-validate every proposed slot's Season
            # eligibility HERE, under the Rink+Season locks just acquired: a
            # concurrent SeasonVenueAccess revoke or Rink→Venue reassignment
            # can only land before these locks or after we release them
            # (never during, since its own write needs the very locks we
            # hold) — shared with move_game and the league-scoped commit via
            # the same locked helper.
            # #328 review round 15 finding 2 -- also moved to run AFTER the
            # general fingerprint recheck above (previously ran right after
            # draft_ls_id resolution, before it): this narrower check's own
            # terminal reason (`venue_access_missing`) is not one of the
            # Scheduler UI's recognised stale-preview recovery reasons, so
            # the exact race this call guards against must not reach the
            # operator as this narrower, unrecognised reason before the
            # general recheck gets a chance to classify it as
            # `preview_stale` first — the identical reordering rationale
            # round 12 finding 1 already applied to
            # `_require_batch_team_participation` above. Any slot-scope
            # drift this call could still catch necessarily also changes
            # the Season-scoped candidate pool `_locked_proposal`'s own
            # regeneration draws from (`_season_scoped_slot_ids` runs the
            # identical `require_slot_belongs_to_season` check, directly
            # for an explicit `slot_ids` selection or via
            # `slot_belongs_to_season` for an omitted one) — already caught
            # above, leaving this now redundant-but-harmless
            # defense-in-depth, on the same established precedent as the
            # check above it.
            require_slots_belong_to_locked_season(
                self.store, [d["ice_slot_id"] for d in proposal["draft_games"]],
                resolved_season_id)
            # #328 review round 8 finding 1 -- an already_scheduled row's
            # Game is not part of this batch's writes, so nothing else ever
            # re-examines it under the lock: the wide draft_fingerprint gate
            # above only catches a cancellation that already happened by the
            # time THIS method's own regeneration ran, and the per-row loop
            # below only rechecks pairings that are actually in the batch.
            # If the blocking Game for an already_scheduled row is cancelled
            # in the narrow gap between that regeneration/fingerprint compare
            # and here, this batch is what the operator reviewed BEFORE that
            # cancellation -- it no longer reflects reality (that pairing is
            # now genuinely open too), and committing it anyway would persist
            # an incomplete schedule with no error. Reusing the SAME
            # `_existing_now` snapshot the per-row loop below relies on keeps
            # this race-free against the same locks; a mismatch means the
            # reviewed premise "this pairing already has Game G" no longer
            # holds, so refuse before any write exactly like any other form
            # of staleness.
            # #375 — two conditions, because the reviewed premise for an
            # already-scheduled row has two halves once a pairing can hold
            # several Games. The Game this row named must still be a live
            # Regular fixture for this pairing (membership, replacing the
            # pre-#375 equality against the single id a pairing could
            # have); AND the pairing must not have GAINED a Game since the
            # proposal was built, which a pairing with no draft_games row
            # of its own would otherwise have nothing checking it.
            #
            # The baseline for "gained" is the pairing's own reviewed count,
            # not the format N. #382 compared against N and so refused any
            # commit touching a Division that legitimately held MORE Games
            # for some pairing than the format asks for — a pre-existing
            # longer series, imported history, or a format later reduced.
            # Nothing had raced; the count was simply never equal to N.
            # Counting the proposal's own rows is not the fix either: they
            # are capped at the requested meetings, so an over-scheduled
            # pairing reports min(K, N) and still trips. The true count
            # travels on the row itself as `existing_game_count`.
            # At N = 1 with one existing Game this is exactly the old
            # equality.
            for a in proposal["already_scheduled"]:
                _as_key = (draft_ls_id, a.get("division_id"),
                           frozenset((a["home_team_id"], a["away_team_id"])))
                _as_now = _existing_now.get(_as_key, ())
                if (a["existing_game_id"] not in _as_now
                        or len(_as_now) > a["existing_game_count"]):
                    raise ConcurrencyConflictError(
                        "This preview is out of date — a game may have "
                        "been added, cancelled, or otherwise changed since "
                        "you generated it. Generate a fresh preview and "
                        "review it before committing.",
                        {"reason": "preview_stale"})
            created = []
            for d in proposal["draft_games"]:
                # #328 review round 4 -- checked BEFORE the physical gate
                # below, not after: a row whose pairing already has a real
                # Game is a terminal, product-confirmed fact (#326) that
                # must win regardless of whether the SAME row would also
                # fail the physical check (e.g. the winning Game happens to
                # sit on this row's own slot, or a slot that overlaps it) —
                # `pairing_already_scheduled` names the specific pairing and
                # winning Game, which `slot_unavailable`/`team_overlap`
                # cannot. An UNRELATED pairing's physical conflict (a
                # different pairing merely sharing one team, or an
                # unrelated slot collision) is not in `_existing_now` for
                # THIS row's key and so still falls through to the
                # unchanged physical check below.
                # #375 — count comparison, not presence: this row is one of
                # the pairing's N meetings, and the reviewed proposal
                # already told us how many of them existing Games covered.
                # Only a Game beyond that is a race.
                _pairing_key = (draft_ls_id, d.get("division_id"),
                                frozenset((d["home_team_id"], d["away_team_id"])))
                _existing_gid = raced_pairing_game_id(
                    _existing_now, _reviewed_counts, _pairing_key)
                if _existing_gid is not None:
                    # #328 review round 3 -- the message itself (not just
                    # details) must be actionable: post()'s generic toast in
                    # app.js surfaces error.message alone, never
                    # error.details, so a vague message here would leave the
                    # operator with no idea which pairing/Game raced. The
                    # proposal row already carries both team names.
                    raise ConcurrencyConflictError(
                        f"{d['home_team_name']} vs {d['away_team_name']} is "
                        f"already scheduled as Game {_existing_gid} — "
                        "generate a fresh preview before committing again.",
                        {"reason": "pairing_already_scheduled",
                         "home_team_id": d["home_team_id"],
                         "away_team_id": d["away_team_id"],
                         "existing_game_id": _existing_gid})
                # #277: the draft-commit path runs the SAME final conflict check
                # as create/move — the slot is free (exists, GAME, AVAILABLE, not
                # already held) AND neither team is put on an overlapping fixture
                # — so a regenerated proposal that would double-book a slot OR a
                # team fails atomically (the whole batch rolls back) instead of
                # silently persisting a bad fixture. Per #277's acceptance,
                # schedule commits enforce the identical check as manual moves;
                # there is no draft-only exception. Returns the resolved slot to
                # allocate below.
                slot = self.setup._assert_slot_free_for_game(
                    d["ice_slot_id"], d["home_team_id"], d["away_team_id"],
                    season_id=resolved_season_id)
                g = Game(
                    id=self.store.next_id("game"),
                    home_team_id=d["home_team_id"], away_team_id=d["away_team_id"],
                    start_time=datetime.fromisoformat(d["start_time"]),
                    end_time=datetime.fromisoformat(d["end_time"]) if d.get("end_time") else None,
                    rink=d.get("rink_name"), division_id=d.get("division_id"),
                    season_id=resolved_season_id, league_id=canonical_league_id,
                    ice_slot_id=d.get("ice_slot_id"),
                    league_season_id=draft_ls_id,
                    published=False, is_draft=True)
                self.store.add_game(g)
                # Mark the slot ALLOCATED, exactly as create_game/move_game do
                # (#277): a committed draft occupies its ice, so later scheduling
                # and the shared checker read it as taken instead of offering the
                # same slot again. The checker above already rejected any slot a
                # game already holds, so this only ever flips AVAILABLE -> taken.
                slot.status = IceSlotStatus.ALLOCATED
                self.store.save_ice_slot(slot)
                created.append(self._draft_game_dto(g))
            # Committing a draft creates real (unpublished) rows — a state change,
            # so it is audited (#86).
            if season_id and league_id:
                scope_type, scope_id = "league", league_id
            else:
                scope_type, scope_id = "division", division_id
            self.setup._audit(
                "draft_schedule_committed", scope_type, scope_id, actor_id,
                {"created_count": len(created),
                 "game_ids": [c["game_id"] for c in created],
                 "unscheduled_count": len(proposal["unscheduled"]),
                 "season_id": resolved_season_id,
                 "league_id": proposal["league_id"]})
        return {"division_id": division_id, "season_id": resolved_season_id,
                "league_id": proposal["league_id"], "created": created,
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

        _pcache = {}  # (rink, season) -> effective policy, one per request
        rows = [self._draft_review_row(g, slot_games, is_double_booked(g),
                                       policy_cache=_pcache)
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
        """Publish draft games (#86) — atomically for the whole batch (#283 review).

        Slot allocation, the draft→real transition, and the audited publish for
        every targeted game run inside ONE store transaction. ``publish_game``
        re-validates each game's #283 participation (a regular game's exact
        LeagueSeason, an exhibition's active Season participation) and RAISES on
        an invalid target; the enclosing transaction then rolls the WHOLE batch
        back. So a single bad target (a missing/mismatched LeagueSeason, an
        inactive registration) can never leave a non-draft unpublished game on an
        allocated slot, nor partially publish earlier rows — every targeted game
        stays a draft, every slot stays AVAILABLE, and no ``game_published``
        audit is written. Each game is routed through the same audited
        ``setup.publish_game`` as single-game publish.
        """
        targets = self._draft_targets(game_ids, all_drafts)
        # Validate EVERY target BEFORE any mutation: run publish_game's own #283
        # participation revalidation (exact LeagueSeason for a regular game,
        # active Season participation for an exhibition) read-only up front, so a
        # single invalid target aborts with ZERO writes — never a partial
        # publish. The transaction below is a second, independent guarantee.
        for g in targets:
            self.setup._revalidate_game_participation(g)
        published = 0
        with self.store.transaction():
            # #159 — lock every target Season (sorted) FIRST, before any
            # slot/Game write, so the lock order is Season-before-slot/Game
            # (matching every other path) and no write precedes the guard.
            self._guard_active_seasons([g.season_id for g in targets])
            for g in targets:
                # Allocate the ice slot, matching the manual create_game
                # invariant (a game's slot is ALLOCATED, not left AVAILABLE) —
                # otherwise a published game sits on a slot the grid still treats
                # as an open drop target.
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
        targets = self._draft_targets(game_ids, all_drafts)
        discarded = 0
        # #159 — lock every distinct target Season (sorted) and delete inside
        # ONE transaction: an archived-Season draft aborts the whole batch
        # with zero deletes/audits, and the lock is held through the writes.
        with self.store.transaction():
            self._guard_active_seasons([g.season_id for g in targets])
            for g in targets:
                self.setup._audit("draft_game_discarded", "game", g.id,
                                  actor_id, {"division_id": g.division_id,
                                             "ice_slot_id": g.ice_slot_id})
                # #277: a committed draft occupied its ice (its slot was flipped
                # to ALLOCATED at commit), so discarding it must release the slot
                # back to AVAILABLE — otherwise a discarded draft strands ice the
                # grid and scheduler would never offer again. Mirrors the slot
                # release in setup.delete_game.
                if g.ice_slot_id:
                    slot = self.store.get_ice_slot(g.ice_slot_id)
                    if slot is not None and slot.status == IceSlotStatus.ALLOCATED:
                        slot.status = IceSlotStatus.AVAILABLE
                        self.store.save_ice_slot(slot)
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
        """True only for a registration safe to act on (#180 review, #233 B2c
        review r1).

        Reuses the shared scheduling guard (season has a league, Team exists and
        its permanent league matches the season) and additionally requires any
        named division to actually belong to the registration's season. The
        registration's own (competition) league_id is REQUIRED (#233 — every
        registration carries a League) and must resolve, belong to the season,
        and agree with a named division's league — a wrong-season / cross-league
        / missing-Team / missing-League / **league-less** row is never exposed
        to the operational UI (e.g. the game-scheduling wizard, #233 B2c).

        #331 review round 19: ``team_registration_valid`` answers "does this
        TEAM have a valid registration in this season", resolved via the
        Team's OWN permanent League -- not "is THIS row (``r``) the valid
        one". For a Team with a genuinely valid registration under its
        permanent League PLUS a stray active row under a different League
        (legacy data / a write path predating Rule 7 -- the same shape
        ``league_scope.team_registration_valid``'s own docstring names),
        calling this with the STRAY row still resolves the OTHER, valid row
        and returns non-``None`` -- so both rows were reported operational,
        each under its own (different) ``league_id``, in a view whose whole
        purpose is deciding what's safe to schedule against. Comparing the
        resolved row's identity back to ``r`` makes this answer "is THIS
        row" rather than "does the Team have SOME row", the same row-vs-
        team distinction ``exact_registration_or_conflict`` draws for exact-
        key lookups.
        """
        # Season + competition League now resolve through the LeagueSeason (#283).
        ls = self._resolve_ls(r.league_season_id)
        if ls is None:
            return False
        season = self.store.get_season(ls.season_id)
        valid_reg = team_registration_valid(
            self.store, season, r.team_id, r.division_id)
        if valid_reg is None or valid_reg.id != r.id:
            return False
        if r.division_id is not None:
            division = self.store.get_division(r.division_id)
            # Same LeagueSeason ⟹ same Season AND same League as the registration.
            if division is None or division.league_season_id != r.league_season_id:
                return False
        if not ls.league_id:
            return False
        league = self.store.get_league(ls.league_id)
        if league is None:
            return False
        return True

    def get_demo_overview(self, user_id=None, role=None, scope=None) -> dict:
        """Assemble the League/Arena/Schedule/Public view for the E2E demo.

        The ``public`` section deliberately contains NO player names or any
        personal data — only fixture information that is safe to show fans.

        #369 review correction: when a real user context is supplied
        (``role`` is not ``None`` — the HTTP route at ``/api/demo/overview``
        always supplies one now), every collection with a real Program/
        Season/League join is scoped to the resolved active context:
        mandatory Program, the resolved Season as a HARD ceiling (an
        operator active in Season S1 never sees S2's divisions,
        registrations, games, ice or standings-snapshot data — even
        archived S2 history, even under the same League), and to the
        selected League (else every League's data within that Season — the
        "No League" broader view, matching the #364 ruling's own
        semantics). An earlier revision of this method deliberately treated
        Season as NOT a hard filter, reasoning that two existing journeys
        needed cross-Season visibility; #369 review correctly rejected
        that as silently widening the released active-context contract —
        those journeys were fixed instead (explicitly switch Season via the
        context bar before the cross-Season action, exactly as switching
        Program/League already requires). A Program-only active context
        (no Season resolved — a brand-new/empty Program, or no authorized
        active Season exists) narrows every Season-scoped collection to
        EMPTY rather than falling back to "every Season", so a missing
        Season selection can never silently re-widen the read.

        Clubs/Organizations/Officials carry no direct Program FK, but they
        are NOT therefore unfiltered — #369 review's "apply the same
        deny-by-default rule to other unjoinable global reference
        collections" applies here, and each is scoped through the strongest
        relationship that actually exists (#367 owner ruling), never a
        synthetic League foreign key:

        * **Clubs** are League-bound *through their Teams*, so they derive
          from the already Program+League-narrowed ``teams`` set.
        * **Officials** are League-bound through an in-scope home Club or an
          assignment whose Game passes the COMPLETE active Season+League
          predicate (``_in_scope_game``).
        * **Organizations** follow the FACILITY axis, which has a Season axis
          (SeasonVenueAccess) but no competition-League axis: owners of the
          active-Season venues, plus the active Program's own operating
          Organization (permanent Program structure).

        Two successive revisions got this wrong: the first returned all three
        wholesale whenever a Program resolved ("no foreign key" meaning "no
        scope to apply"), the second narrowed them to the Program but not the
        League — so with Program A / Season A1 / League Aa active, sibling
        League A2's Club and Official still came back, ``home_club_name`` and
        all. Unlike the Setup surface there is no bootstrap counterpart: this
        is an operational read, so a record linked to nothing is simply not
        Dashboard data.

        ``setup_audit`` resolves each row through its own ``entity_type``
        against that SAME COMPLETE tuple — reusing the very sets that decide
        whether the row's entity is returned at all — so activity can never
        stay in the feed for an entity the payload withholds.

        When no Program resolves at all the read fails CLOSED with NO
        exception: a scoped account with a missing/revoked/unassigned link
        gets the same empty shape whether or not other Programs exist in
        the installation. (Pre-Program bootstrap reference data is served
        from the explicitly authorized Setup-management contract,
        ``get_setup_overview_v2``.) Called with no
        arguments (``role`` left ``None``, the default), returns the full,
        unfiltered installation view exactly as before #367 — this is what
        every existing internal caller that inspects whole-store state
        (tests exercising other subsystems) keeps using unchanged; only the
        HTTP route's own per-user Dashboard read opts into scoping.
        """
        program = league = None
        in_scope_season_ids = None  # None == no Program scoping active (legacy)
        if role is not None:
            program, season, league = self.context.resolve_with_league(
                user_id, role, scope)
            if program is None:
                # #369 review correction: fail CLOSED with NO exception --
                # a scoped account with no authorized active Program must
                # never see installation-wide Club/Organization/Official
                # names (that bootstrap need is served by
                # get_setup_overview_v2 instead, not this operational read).
                return {
                    "league": None, "leagues": [], "seasons": [], "levels": [],
                    "divisions": [], "clubs": [], "teams": [],
                    "organizations": [], "venues": [], "rinks": [],
                    "ice_slots": [], "officials": [],
                    "schedule": [], "public_fixtures": [], "registrations": [],
                    "setup_audit": [], "setup_audit_count": 0,
                }
            # #369 review correction: the resolved Season is now a HARD
            # ceiling -- {} (never "every Season") when Program-only (no
            # Season resolved), {season.id} otherwise. See docstring.
            in_scope_season_ids = {season.id} if season is not None else set()

        all_ls = self.store.all_league_seasons()
        # #367: the League(+Season)-scoped LeagueSeason ids, when Program
        # scoping is active — narrows Divisions/registrations/games below to
        # the resolved context. `None` (scoping inactive) keeps every
        # LeagueSeason, identical to pre-#367 behavior.
        in_scope_ls_ids = None
        if in_scope_season_ids is not None:
            in_scope_ls_ids = {
                ls.id for ls in all_ls
                if ls.season_id in in_scope_season_ids
                and (league is None or ls.league_id == league.id)}

        all_divisions = self.store.all_divisions()
        if in_scope_ls_ids is not None:
            all_divisions = [d for d in all_divisions
                             if d.league_season_id in in_scope_ls_ids]
        divisions = {d.id: d for d in all_divisions}
        all_levels = self.store.all_leagues()
        if program is not None:
            all_levels = [lv for lv in all_levels if lv.program_id == program.id]
        levels = {lv.id: lv for lv in all_levels}
        # Division/registration Season + League resolve via LeagueSeason (#283).
        ls_by_id = {ls.id: ls for ls in all_ls}
        clubs = {c.id: c for c in self.store.all_clubs()}
        all_teams = self.store.all_teams()
        if program is not None:
            all_teams = [
                t for t in all_teams if t.program_id == program.id
                and (league is None or t.league_id == league.id)]
        teams = {t.id: t for t in all_teams}
        orgs = {o.id: o for o in self.store.all_organizations()}
        leagues_by_id = {lg.id: lg for lg in self.store.all_programs()}
        # #369: Venues/Rinks/Ice slots have no competition-League axis at all
        # (SeasonVenueAccess is the ONLY join between the physical and
        # competition trees), so they are never narrowed by League. They ARE
        # narrowed by Season: `in_scope_season_ids` is the single ACTIVE
        # Season (empty for a Program-only context), so only a Venue granted
        # to THAT Season is in scope. An earlier revision scoped these to any
        # of the Program's Seasons; #369 review rejected that as silently
        # widening the released contract, and this comment described it for
        # one commit after the code had already stopped doing it.
        in_scope_venue_ids = None
        if in_scope_season_ids is not None:
            in_scope_venue_ids = {
                a.venue_id for a in self.store.all_season_venue_access()
                if a.active and a.season_id in in_scope_season_ids}
        all_venues = self.store.all_venues()
        if in_scope_venue_ids is not None:
            all_venues = [v for v in all_venues if v.id in in_scope_venue_ids]
        venues = {v.id: v for v in all_venues}
        all_rinks = self.store.all_rinks()
        if in_scope_venue_ids is not None:
            all_rinks = [r for r in all_rinks if r.venue_id in in_scope_venue_ids]
        rinks = {r.id: r for r in all_rinks}

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
        def _division_row(d):
            sid = self._season_id_via(ls_by_id, d.league_season_id)
            lid = self._league_id_via(ls_by_id, d.league_season_id)
            return {"id": d.id, "season_id": sid, "name": d.name,
                    "age_group": d.age_group, "is_junior": is_junior(d),
                    "level_id": lid,
                    "level_name": levels[lid].name if lid in levels else None}
        division_rows = [_division_row(d) for d in divisions.values()]
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

        # `None` season ids == no scoping active (the legacy no-role call).
        def _in_scope_game(g):
            if in_scope_season_ids is None:
                return True
            return self._game_matches_active(
                g, in_scope_season_ids,
                league.id if league is not None else None, teams)

        # Draft games (#86) are proposals under review — they must never surface
        # in the operator slot grid / schedule / calendar until published, so
        # they are excluded from the LABELS here. The dedicated draft-review
        # view lists them.
        game_by_slot = {g.ice_slot_id: g for g in self.store.all_games()
                        if g.ice_slot_id and not g.is_draft
                        and _in_scope_game(g)}
        # Reserved ice, however, is PHYSICAL (#277 Slice B review): an active
        # committed draft blocks warm-up/resurfacing facility time exactly
        # like a published game — the placement gate counts it — so the
        # reserved derivation looks at EVERY active occupant while the grid
        # label above stays published-only.
        occupant_by_slot = {g.ice_slot_id: g for g in self.store.all_games()
                            if g.ice_slot_id and not g.cancelled
                            and _in_scope_game(g)}
        slot_rows = []
        # #277 Slice B — reserved-vs-playable visibility: a slot's stored span
        # is PLAYABLE time; the derived reserved facility span around a
        # hosting game comes from ONE shared helper (_reserved_span, also
        # used by the schedule and draft-review rows below/elsewhere), so
        # the operator surfaces cannot disagree.
        _policy_cache = {}

        all_slots = self.store.all_ice_slots()
        if in_scope_venue_ids is not None:
            all_slots = [s for s in all_slots if s.rink_id in rinks]
        for s in sorted(all_slots, key=lambda x: (x.rink_id, x.start_time)):
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
                "reserved": self._reserved_span(
                    s, occupant_by_slot.get(s.id), _policy_cache),
            })

        schedule, public_fixtures = [], []
        for g in self.store.all_games():
            if g.is_draft:
                continue  # unpublished draft — kept out of normal views (#86)
            if not _in_scope_game(g):
                continue
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
                # #277 Slice B — the schedule review shows the same derived
                # reserved span as the calendar (one shared derivation).
                "reserved": self._reserved_span(slot, g, _policy_cache),
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

        # #367: the "leagues"/"league" (legacy-vocabulary Program list) and
        # "seasons" lists scope to the resolved Program when active — this
        # is the per-user Dashboard's own single-Program view, not a
        # cross-Program picker. `None` scoping (role not supplied) keeps
        # every Program/Season, identical to pre-#367 behavior.
        all_programs = self.store.all_programs()
        if program is not None:
            all_programs = [p for p in all_programs if p.id == program.id]
        leagues = [program_to_v1(_serialize(x)) for x in all_programs]
        all_seasons = self.store.all_seasons()
        if in_scope_season_ids is not None:
            all_seasons = [s for s in all_seasons if s.id in in_scope_season_ids]
        seasons = [season_to_v1(_serialize(x)) for x in all_seasons]
        # `/api/demo/overview` requires a signed-in session as of #367, but
        # this per-row redaction predates that and is kept as defense in
        # depth: actor_id/detail must NOT be exposed for every setup-audit
        # action, only for import batches (#102's Activity drill-down needs
        # them there). Other actions' detail dicts can carry things no
        # caller of this endpoint needs (e.g. user_account_created stores
        # {"username", "role"}). Scope the extra fields to exactly the
        # import-batch summary row and its linked per-row children.
        def _is_import_related(a):
            return a.entity_type == "import_batch" or (a.detail or {}).get(
                "import_batch_id") is not None
        registration_rows = [
            {"team_id": r.team_id,
             "season_id": self._season_id_via(ls_by_id, r.league_season_id),
             "division_id": r.division_id,
             "league_id": self._league_id_via(ls_by_id, r.league_season_id)}
            for r in self.store.all_season_team_registrations()
            if r.active and self._registration_is_operational(r)
            and (in_scope_ls_ids is None or r.league_season_id in in_scope_ls_ids)
        ]
        # -- the derived-join reference sets (Clubs / Organizations /
        #    Officials), computed BEFORE the audit filter because the audit
        #    filter resolves `club`/`organization`/`official` rows through
        #    exactly these sets. Keeping one definition per axis is the point:
        #    two parallel derivations drifted apart once already (the audit
        #    filter's own `audit_club_ids` was Program-wide while the returned
        #    `clubs` was not), which is how an out-of-tuple Club id reached
        #    the Activity feed while its name was correctly withheld from the
        #    Clubs list.
        #
        # #367 owner ruling: Clubs are LEAGUE-bound "through their Teams", so
        # they derive from `teams` — already narrowed to the active
        # Program AND selected League — not from every Team in the Program.
        # Officials are League-bound too, "through an in-scope home Club or
        # assignment", and an assignment only counts when its Game passes the
        # COMPLETE active Season+League predicate (`_in_scope_game`, which
        # also decides the league-less exhibition case identically).
        # Organizations follow the FACILITY axis instead: Venue/Rink/IceSlot
        # and their owning Organization have a Season axis (SeasonVenueAccess)
        # but NO competition-League axis, so `venues` is already
        # active-Season-wide-under-a-League and the owners derive from it;
        # the active Program's own operating Organization is permanent
        # Program structure and stays in scope regardless of any Venue.
        #
        # `in_scope_season_ids is None` is precisely "no scoping active",
        # i.e. the legacy no-role call. Do NOT branch on `program is None`
        # here: that is ALSO true for the legacy path (nothing was ever
        # resolved), so keying off it silently emptied the unfiltered
        # contract every internal caller depends on. The scoped-role-with-
        # no-Program case cannot reach this point at all -- it already
        # returned the fully-empty shape far above.
        def _official_has_in_scope_assignment(official_id):
            if not official_id:
                return False
            for a in self.store.assignments_for_official(official_id):
                g = self.store.get_game(a.game_id)
                if g is not None and _in_scope_game(g):
                    return True
            return False

        if in_scope_season_ids is None:
            ref_clubs = list(clubs.values())
            ref_orgs = list(orgs.values())
            ref_officials = list(self.store.all_officials())
        else:
            scoped_club_ids = {t.club_id for t in teams.values() if t.club_id}
            ref_clubs = [c for c in clubs.values() if c.id in scoped_club_ids]
            scoped_org_ids = {v.organization_id for v in venues.values()
                              if v.organization_id}
            if program is not None and program.operator_organization_id:
                scoped_org_ids.add(program.operator_organization_id)
            ref_orgs = [o for o in orgs.values() if o.id in scoped_org_ids]
            ref_officials = [
                o for o in self.store.all_officials()
                if o.home_club_id in scoped_club_ids
                or _official_has_in_scope_assignment(o.id)]
        ref_club_ids = {c.id for c in ref_clubs}
        ref_org_ids = {o.id for o in ref_orgs}
        ref_official_ids = {o.id for o in ref_officials}

        # #369 review correction: a pre-#369 revision left the audit log
        # UNFILTERED regardless of Program scoping, reasoning that a
        # "best-effort" id-membership filter risked hiding legitimate
        # same-Program activity worse than showing everything. Review
        # correctly rejected that: an unfiltered audit log is a real
        # cross-Program leak (actor id + detail dict for another Program's
        # activity). Every entity_type the audit log currently records
        # resolves through its own real parent chain below; a row that
        # cannot be validated (an unjoinable row, or a genuinely
        # Program-agnostic event type — a bare account/auth action) is
        # OMITTED entirely when Program scoping is active, never guessed
        # or shown globally.
        #
        # #367 owner ruling: that chain resolves against the COMPLETE active
        # tuple, not the Program alone. A Program-level-only audit filter
        # re-widened every axis the rest of this method narrows — with
        # Program A / Season A1 / League Aa active it still listed Season A2's
        # divisions, registrations, games and venue grants, and League Ab's
        # teams, by id and action. Each branch below therefore reuses the very
        # set that decides whether the row's own entity is returned at all
        # (`divisions`, `teams`, `in_scope_ls_ids`, `venues`, `rinks`,
        # `ref_*_ids`, `_in_scope_game`), so an entity can never be filtered
        # out of the payload while its activity stays in the feed.
        def _feed_actor_matches_active(actor_type, actor_ref):
            """A CalendarFeedToken's own actor (team/division/official/
            player) resolved against the active tuple, mirroring the
            equivalent entity_type branches below -- this is exactly the
            "legitimate same-Program activity a partial filter would hide"
            case a pre-#369 revision cited as its reason to leave the whole
            audit log unfiltered; handled correctly here instead of
            omitted."""
            if actor_type == "team":
                return actor_ref in teams
            if actor_type == "division":
                return actor_ref in divisions
            if actor_type == "official":
                return actor_ref in ref_official_ids
            if actor_type == "player":
                p = self.store.get_player(actor_ref)
                return p is not None and p.team_id in teams
            return False

        def _audit_matches_active(a):
            et, eid, store = a.entity_type, a.entity_id, self.store
            if et in ("program", "league"):        # legacy vocabulary: Program
                return eid == program.id
            if et == "season":
                # Season-BOUND: the active Season itself, never a sibling
                # Season of the same Program (empty set when Program-only).
                return eid in in_scope_season_ids
            if et == "level":                       # legacy vocabulary: League
                # A League is permanent Program structure, so the Program is
                # its ceiling -- but a SELECTED League narrows to itself, the
                # same way `teams`/`divisions` narrow.
                lv = store.get_league(eid)
                return (lv is not None and lv.program_id == program.id
                        and (league is None or eid == league.id))
            if et == "league_season":
                return eid in in_scope_ls_ids
            if et == "division":
                return eid in divisions
            if et == "team":
                return eid in teams
            if et == "season_team_registration":
                r = store.get_season_team_registration(eid)
                return r is not None and r.league_season_id in in_scope_ls_ids
            if et == "season_venue_access":
                sva = store.get_season_venue_access(eid)
                return sva is not None and sva.season_id in in_scope_season_ids
            if et in ("game", "game_result"):
                g = store.get_game(eid)
                return g is not None and _in_scope_game(g)
            if et == "official_assignment":
                oa = store.get_official_assignment(eid)
                g = store.get_game(oa.game_id) if oa else None
                return g is not None and _in_scope_game(g)
            if et == "official_availability":
                av = store.get_official_availability(eid)
                return av is not None and av.official_id in ref_official_ids
            if et == "official":
                return eid in ref_official_ids
            if et == "club":
                return eid in ref_club_ids
            if et == "organization":
                return eid in ref_org_ids
            if et == "venue":
                return eid in venues
            if et == "rink":
                return eid in rinks
            if et == "ice_slot":
                slot = store.get_ice_slot(eid)
                return slot is not None and slot.rink_id in rinks
            if et == "calendar_feed_token":
                tok = store.get_calendar_feed_token(eid)
                return tok is not None and _feed_actor_matches_active(
                    tok.actor_type, tok.actor_ref)
            # user_account, auth, guardian_link, import_batch, and any
            # future/unrecognized type: no reliable Program chain at all --
            # omit rather than guess.
            return False

        setup_audit = []
        for a in self.store.all_setup_audit():
            if program is not None and not _audit_matches_active(a):
                continue
            entry = {"action": a.action, "entity_type": a.entity_type,
                     "entity_id": a.entity_id, "at": a.at.isoformat()}
            if _is_import_related(a):
                entry["actor_id"] = a.actor_id
                entry["detail"] = a.detail
            setup_audit.append(entry)
        # (`ref_clubs`/`ref_orgs`/`ref_officials` are derived far above, next
        # to the audit filter that shares their sets -- see the comment
        # there. `clubs`/`orgs` stay whole as NAME-RESOLUTION lookups:
        # labelling a row that is already independently authorized leaks
        # nothing; only what is RETURNED is scoped. Unlike the Setup surface
        # there is no bootstrap counterpart here -- this is an operational
        # read, so a record linked to nothing is simply not Dashboard data.)
        return {
            "league": leagues[0] if leagues else None,
            "leagues": leagues,
            "seasons": seasons,
            "levels": [self._league_dict(lv) for lv in levels.values()],
            "divisions": division_rows,
            "clubs": [_serialize(c) for c in ref_clubs],
            "teams": team_rows,
            "organizations": [_serialize(o) for o in ref_orgs],
            "venues": venue_rows,
            "rinks": rink_rows,
            "ice_slots": slot_rows,
            "officials": [
                {"id": o.id, "name": o.name,
                 "home_club_name": (clubs[o.home_club_id].name
                                    if o.home_club_id in clubs else None)}
                for o in ref_officials
            ],
            "schedule": schedule,
            "public_fixtures": public_fixtures,
            # Active season/division participation (#180): the source of truth
            # for which Team plays which League/Division in which Season.
            # Operational UI (e.g. the scheduling wizard, #233 B2c) filters
            # teams through these, never the legacy Team.division_id. Only
            # OPERATIONALLY-VALID rows are exposed (review): a corrupt/retained
            # row — wrong season, cross-league or missing Team/League, or a
            # division that isn't in the row's season or doesn't agree with its
            # league — is filtered out here so it can never be offered in the
            # picker. `league_id` is the competition (grouping) League the
            # wizard's League picker needs — required on the registration since
            # #233 Slice C2, so always present for an operational row.
            "registrations": registration_rows,
            "setup_audit": setup_audit,
            "setup_audit_count": len(setup_audit),
        }

    # The six entity kinds that can exist before any link to a Program does,
    # and are therefore eligible for the creator-owned pending-link contract
    # (#367 owner ruling). Every one of them records a `<entity>_created`
    # audit entry carrying the acting account id at create time
    # (SetupService.create_club/_organization/_venue/_rink/_ice_slot/
    # _official), which is the installation's own authoritative answer to
    # "who made this".
    _PENDING_LINK_ENTITY_TYPES = ("club", "organization", "official",
                                  "venue", "rink", "ice_slot")

    # The four setup-create PARENT relations a caller supplies by id
    # (rink->venue, ice-slot->rink, venue->organization, official->club),
    # mapped to the `get_setup_overview_v2` lists that name what this caller
    # may already reach for that entity kind. See `writable_setup_parent_ids`.
    _SETUP_PARENT_KEYS = {
        "venue": ("venues", "pending_link_venues"),
        "rink": ("rinks", "pending_link_rinks"),
        "organization": ("organizations", "pending_link_organizations"),
        "club": ("clubs", "pending_link_clubs"),
        # A Program has no pending-link list: it IS the thing everything else
        # links TO, so it is never "linked to no Program". The legacy v1-only
        # `Venue.league_id` names one (that field stores a PROGRAM id despite
        # its name -- see the vocabulary note in the module docstring).
        "program": ("programs", None),
    }

    # Setup-audit entity types differ from the parent-kind names above where
    # the LEGACY vocabulary survives in the audit trail: `create_program`
    # writes action "league_created" / entity_type "league", because pre-#233
    # "League" meant what is now a Program. Looking up "program" finds nothing
    # and would silently deny a creator its own Program. Only kinds that
    # actually differ appear here.
    _SETUP_PARENT_AUDIT_TYPE = {"program": "league"}

    def writable_setup_parent_ids(self, kind, user_id, role, scope):
        """Ids of `kind` this caller may attach a NEW child to, or ``None``
        if that cannot be determined (the caller must then refuse).

        The write gate for the four parent-by-id creates (#369 review). It
        lives here, beside the read it mirrors, so the set a caller may WRITE
        under is computed from the same pass that decides what it may READ
        and cannot drift into a second, weaker policy.

        Three sources, and each is load-bearing:

        * the ACTIVE scoped list -- the ordinary case, the active-tuple
          ceiling exactly as the read applies it;
        * this caller's own ``pending_link_*`` -- its create-then-link chain,
          where a parent is linked to no Program yet (a Rink under a Venue it
          just made, before any grant exists);
        * rows this caller CREATED, whatever their link state. Without this
          the gate refuses a legitimate flow it must allow: create an
          Organization, create a Program it OPERATES, then add that
          Organization's first Venue. Operating a Program is a real Program
          link, so the row is correctly absent from ``pending_link_*``; and
          while the caller's context still points at some OTHER Program it is
          equally correctly absent from the scoped list. It falls between the
          two -- visible in neither, though the caller made it moments ago.
          Creator-ownership is the same authenticated, unforgeable signal the
          pending-link contract already rests on (``_creator_created_ids``:
          CREATE actions only, real account ids only, so a probe can never
          become permission), and it is strictly narrower than authorization:
          a global role is authorized for every Program but has still created
          only its own rows, which is why the ruling's rejected
          authorized-set alternative is not what this is.

        What stays refused is the whole of the reported IDOR: another
        creator's pending row, a foreign Program's linked row, a guessed id
        that never existed, and a deactivated creator's orphans. None of
        those are in any of the three sources.
        """
        overview = self.get_setup_overview_v2(user_id, role, scope)
        if not isinstance(overview, dict) or "error" in overview:
            return None
        scoped_key, pending_key = self._SETUP_PARENT_KEYS[kind]
        ids = {row["id"] for row in overview.get(scoped_key) or []}
        if pending_key:
            ids |= {row["id"] for row in overview.get(pending_key) or []}
        ids |= self._created_ids_of_type(
            self._SETUP_PARENT_AUDIT_TYPE.get(kind, kind), user_id)
        return ids

    def setup_parent_writable(self, kind, parent_id, user_id, role, scope):
        """May this caller attach a child to THIS parent? True / False / None.

        The predicate form of ``writable_setup_parent_ids`` (#369 review), so
        the ONE write-side parent decision can be taken in either place it is
        needed and can never fork into two policies:

        * as the transport preflight (``Handler._reject_parent_outside_scope``),
          where a create's caller-supplied parent id is refused before the
          facade is called at all;
        * as a ``setup_guarded_mutation`` target with ``rule="writable_parent"``
          (#369 re-review's atomicity requirement), where a REASSIGN's
          destination parent is re-decided under that row's own lock, inside
          the same transaction as the move and its audit. A reassign is the
          same attachment reached by the other verb, so leaving its parent
          check outside the transaction would reopen, for
          ``assign-<target>``, exactly the check/use gap the guard closes for
          everything else.

        Fails CLOSED, unlike the generic ``setup_target_accessible`` ceiling:
        ``role is None`` and an errored overview both return **False** rather
        than "do not gate". A caller-supplied parent id is an attachment point
        for a NEW row; there is no legitimate identity-less client that needs
        to name one, and the create routes have refused that case since this
        gate was written. A falsy ``parent_id`` is not this gate's business
        (nothing to leak, nothing to probe) — it returns None so the facade's
        own validation/not-found error is left exactly as it was.
        """
        if not parent_id:
            return None
        accessible = self.writable_setup_parent_ids(
            kind, user_id, role, scope) if role is not None else None
        if accessible is None:
            return False
        return parent_id in accessible

    def _created_ids_of_type(self, entity_type, user_id):
        """``{entity_id, ...}`` of `entity_type` rows ``user_id`` CREATED.

        The same ownership signal as ``_creator_created_ids`` and with the
        same two restrictions (CREATE actions only, so probing an id can
        never become permission to use it; a real account id only, so an
        identity-less caller owns nothing) -- but not restricted to the
        pending-link entity types, because a Program is a valid write parent
        and is never a pending-link row.
        """
        if not user_id:
            return set()
        return {a.entity_id for a in self.store.all_setup_audit()
                if a.actor_id == user_id and a.entity_type == entity_type
                and a.action == f"{entity_type}_created"}

    def _creator_created_ids(self, user_id):
        """``{entity_type: {entity_id, ...}}`` for the rows ``user_id``
        CREATED, from the audit trail (#367 owner ruling's "separately
        authorized creator-owned contract").

        Two deliberate restrictions make this an ownership signal rather than
        a second disclosure path:

        * only ``<entity_type>_created`` actions count. Any later write
          (rename, reassign, delete) is ignored, so poking an id you were
          never shown can never turn into permission to enumerate it
          afterwards.
        * ``user_id`` must be a real account id. An identity-less caller
          (``None``) owns nothing and gets empty sets, so the contract fails
          closed rather than matching every ``actor_id=None`` audit row ever
          written by an internal/seed call path.

        The caller intersects these with "has no validated link to any
        Program" — ownership alone never surfaces a row that belongs to some
        Program, which stays governed by the active-tuple ceiling.
        """
        out = {et: set() for et in self._PENDING_LINK_ENTITY_TYPES}
        if not user_id:
            return out
        for a in self.store.all_setup_audit():
            bucket = out.get(a.entity_type)
            if (bucket is not None and a.actor_id == user_id
                    and a.action == f"{a.entity_type}_created"):
                bucket.add(a.entity_id)
        return out

    @catch
    def get_venue_grant_candidates(self, season_id, user_id=None, role=None,
                                   scope=None) -> dict:
        """Venues that MAY be granted to ``season_id`` -- the grant-only
        facility contract (#369 review).

        This exists because the alternative failed review. The candidate list
        was first emitted as ``grantable_venues`` on ``get_setup_overview_v2``,
        and that turned the ordinary scoped overview into an
        installation-wide Venue directory: ``/api/v2/setup/overview`` is gated
        MANAGE_ARENA, the grant POST needs MANAGE_SETUP, so an Arena Manager --
        a role that cannot perform the sharing action at all -- received every
        linked Venue's id and name, independent of its active Program. Ordinary
        read visibility is not write authorization, and the ceiling cannot be
        widened for every reader to serve one capability.

        So the disclosure is bound to the feature that needs it, three ways:

        1. **Its own route**, gated MANAGE_SETUP -- the same permission as the
           grant it feeds, so no role learns anything it could not already act
           on. The overview stays Program/active-Season scoped.
        2. **The SELECTED destination Season** -- ``season_id`` must BE the
           caller's persisted active Season, not merely some Season of the
           active Program (#369 owner ruling). A foreign or nonexistent
           ``season_id`` is refused identically, so this never becomes a
           Season oracle either.
        3. **Candidates only**: id and name, for Venues that are either linked
           to some Program (an established shared facility) or unlinked and
           created by THIS caller (its own create-then-grant draft). Another
           operator's unlinked draft is never a candidate -- that case leaked
           in an earlier round of this work.

        On (2) the ceiling was first written as a Program comparison
        (``season.program_id == program.id``), which answered 200 for any
        SIBLING Season of the active Program. The owner ruled that incorrect:
        the active tuple is what the operator actually selected, and a
        same-Program/other-Season 200 hands out a grant surface for a Season
        the caller is not working in. So the check is now identity against the
        resolved active Season, which also makes a Program-only context (no
        Season selected) fail closed -- there is no selected Season for the
        request to equal, and inventing one would be exactly the widening the
        ruling forbids.

        The ceiling being tightened is the DESTINATION Season, never WHICH
        Venues may be offered: the candidate set is still deliberately NOT
        filtered to the active Program, because a Venue already linked to
        another Program is exactly what sharing means, and filtering it out is
        what deadlocked the capability (once one Program holds the grant, no
        other could obtain it). Rinks, IceSlots and every competition record
        keep the ordinary ceiling.

        4. **An ARCHIVED destination is refused outright** (#369 owner ruling,
           follow-up). An archived Season is read-only history: every write it
           owns -- ``grant_season_venue_access`` included -- fails
           ``season_archived`` at :func:`require_active_season`. This method
           exists ONLY to feed that write, so answering it for an archived
           Season advertised a mutation nobody could perform, and did so by
           handing out the one facility list that deliberately reaches ACROSS
           the Program ceiling: a cross-Program Venue directory disclosed for a
           capability the destination cannot exercise. The refusal is
           deliberately GENERIC -- the same ``NotFoundError`` as a foreign,
           sibling, nonexistent or Program-only miss -- so an archived Season is
           not distinguishable from any of them, and it is raised BEFORE any
           Venue is read or serialized, so no candidate is even computed.

        :meth:`list_season_venue_access` is deliberately NOT narrowed the same
        way: it reports the Season's OWN grant history (active and revoked,
        each naming its own Venue), which is precisely what a read-only
        historical Season is FOR. Reading what a Season used is not being
        offered something to change.
        """
        if role is None:
            return {"error": {"code": "forbidden",
                              "message": "An identity is required."}}
        program, active_season, _league = self.context.resolve_with_league(
            user_id, role, scope)
        # The EXACT selected-Season ceiling (#369 owner ruling). One generic
        # refusal for every miss -- foreign Program, sibling Season of the
        # active Program, nonexistent id, and Program-only context alike -- so
        # none of them is distinguishable from the others.
        if (program is None or active_season is None
                or not season_id or season_id != active_season.id):
            raise NotFoundError(f"Season {season_id} not found.")
        # ...and an ARCHIVED selected Season joins them, down the SAME path.
        # Positioned here deliberately: AFTER the equality check (so it can
        # never become an archived-vs-active oracle for a Season the caller did
        # not select) and BEFORE any Venue is resolved or serialized (so the
        # cross-Program candidate directory is never even built for a
        # destination whose grant would fail `season_archived` anyway).
        if active_season.status == SeasonStatus.ARCHIVED:
            raise NotFoundError(f"Season {season_id} not found.")
        season = active_season
        granted = {a.venue_id for a in
                   self.store.season_venue_access_for_season(season.id)
                   if a.active}
        own = self._created_ids_of_type("venue", user_id)
        out = [{"id": v.id, "name": v.name}
               for v in self.store.all_venues()
               if v.id not in granted            # already allowed here
               and (self._venue_program_ids_for_grant(v) or v.id in own)]
        return {"season_id": season.id, "candidates": out}

    def _venue_program_ids_for_grant(self, venue):
        """Program ids this Venue is linked to, for candidacy purposes.

        Every ``SeasonVenueAccess`` grant counts, ACTIVE OR REVOKED -- revoking
        deactivates the row rather than deleting it, so history still makes the
        Venue an established facility rather than a private draft -- plus the
        legacy ``Venue.league_id``, which despite its name stores a PROGRAM id.
        """
        ids = {a.season_id for a in
               self.store.season_venue_access_for_venue(venue.id)}
        out = set()
        for sid in ids:
            s = self.store.get_season(sid)
            if s is not None:
                out.add(s.program_id)
        if venue.league_id:
            out.add(venue.league_id)
        return out

    @catch
    def get_setup_overview_v2(self, user_id=None, role=None, scope=None) -> dict:
        """Canonical flat setup-entity lists for the Setup/Records UI (#233 B2a).

        The v1 ``get_demo_overview`` stays the (legacy-shaped) source for the
        schedule / games / public views; this is the canonical READ the
        operator's Setup surface renders from. Every field is canonical — a
        Program's ``operator_organization_id``, a Season/Team's ``program_id``, a
        Division's grouping ``league_id`` — and the canonical Venue is
        Organization-owned only, carrying NO ``league_id``. Which Seasons may
        use a Venue's ice is a separate SeasonVenueAccess grant (#233 Slice
        E), not part of this structural read. It exposes only structural
        records plus resolved parent names — no rosters, schedule, games or
        PII — so it is gated like the hierarchy read.

        #369 review correction: when a real user context is supplied
        (``role`` is not ``None``), every collection scopes to the persisted
        ACTIVE tuple (``ContextService.resolve_with_league`` — the SAME
        ceiling the Dashboard and Setup Progress reads already use), never
        the caller's full authorized set. ``programs`` itself collapses to
        ``[active_program]`` (or ``[]``) — the context BAR
        (``options_with_league``, #366) is the cross-Program picker; this
        structural surface only ever operates within whichever one Program
        is currently active.

        **The active Season is a hard ceiling here too** (#367 owner
        ruling), for the Setup hub, all six workflow landings and Setup
        Records alike. An earlier revision resolved the Season and then
        deliberately ignored it, returning every Season of the Program and
        every Season-derived record under it, reasoning that a management
        surface "needs to see and create against every Season". The ruling
        rejects that: Season-bound rows must match the active Season, and a
        Program-only context returns EMPTY for Season-bound data rather than
        falling back to the union. Concretely, ``seasons`` is the active
        Season alone (``[]`` when Program-only), and ``divisions``, the
        facility tree and Official assignments narrow with it. Seeing
        another Season means SELECTING it in the context bar — the same
        thing switching Program or League already requires.

        Two axes are deliberately NOT narrowed, and the ruling names both:

        * ``leagues`` — a League is PERMANENT Program structure (#283), not
          Season-bound data, so it stays Program-scoped and is not narrowed
          by the League selection either (the selection narrows what is
          *under* a League; the pickers still need the Program's set). The
          per-League ``season_ids`` binding list is likewise reported whole.
        * The FACILITY tree (Venue → Rink → IceSlot and the owning
          Organization) has a Season axis via SeasonVenueAccess but NO
          competition-League axis, so a League selection never narrows it:
          under any League it stays active-Season-wide. "No League" is
          therefore exactly the Program + active-Season union. Do not invent
          a schema edge here — ``Venue.league_id`` is legacy vocabulary
          storing a PROGRAM id, never a competition League.

        Club/Organization/Official/Venue/Rink/IceSlot carry no direct
        Program FK, but each has a real, validatable chain into the active
        tuple: a Club with >=1 Team in it (Clubs reach a League through
        their Teams), an Official whose home Club is in-scope or who has an
        assignment in the active Season, an Organization owning an in-scope
        Venue or operating the active Program, a Venue granted to the active
        Season (Rinks/IceSlots cascade from Venue).

        **An unjoinable record is omitted, not disclosed.** A record with no
        chain to ANY Program is NOT thereby "nobody's data": the previous
        revision published every such row to every scoped caller in additive
        ``unassigned_*`` lists, which let ANY Arena Manager — including one
        authorized for zero Programs — enumerate every never-linked Club,
        Organization, Venue, Rink, IceSlot and Official in the installation
        by name, Officials' personal names included. That mechanism is
        REVERSED. What replaces it is the narrow, separately-authorized
        creator-owned contract the ruling permits: the ``pending_link_*``
        lists carry exactly the rows that (a) have no validated link to any
        Program AND (b) THIS caller created, per the installation's own
        audit trail (``<entity>_created`` with ``actor_id == user_id``).
        That keeps every create-then-link flow working for the operator who
        is actually performing it — a just-created Club has no Team yet, a
        just-created Venue no grant — while a second operator, or an
        identity with no authorized Program at all, sees nothing. Ownership
        is only ever read from a CREATE entry, never from any later write,
        so touching a record cannot become a way to start seeing it; and
        ``user_id=None`` (an identity-less caller) owns nothing.

        **A zero-Program installation is not an exception to any of that**,
        and an earlier revision's carve-out saying otherwise is REVERSED.
        That revision let any authenticated caller whose context resolved no
        Program fall through to the fully unfiltered legacy shape whenever
        the store happened to hold zero Programs, reasoning that a brand-new
        install has no "other Program" to leak from. The reasoning does not
        hold: an install with no Program still accumulates pre-Program setup
        rows — Clubs, Organizations, Venues, Rinks, IceSlots and Officials,
        the last of which carry people's personal names — so two
        setup-capable accounts on the same fresh install could each
        enumerate everything the other had created, and an account holding
        the read permission but authorized for nothing could enumerate all
        of it. Absence of a Program is not an authorization, and this was
        neither the owner-approved creator-owned contract nor a separately
        authorized install-admin boundary.

        So an authenticated caller with zero Programs now gets exactly what
        an authenticated caller denied by scope gets: the scoped-empty shape
        plus its OWN ``pending_link_*`` rows. That is all a bootstrap ever
        needed — the operator building a brand-new install IS the account
        that created the rows it is working on, so create-then-link keeps
        working end to end, and the first Program's creation immediately
        resolves a real active context (``ContextService._fallback`` selects
        the only authorized Program) from which the normal scoped chains
        take over. No install-bootstrap authorization is introduced, because
        none is needed to keep the flow working; a second operator's
        pre-Program rows simply stay that operator's.

        A role authorized for zero Programs while OTHER Programs exist gets
        the identical answer, deliberately: the two cases are now one code
        path, so no future "but the install is empty" special case can
        reopen the hole.

        Called with no arguments (``role`` left ``None``, the default),
        returns the full, unfiltered installation view exactly as before
        #367 — unchanged for existing direct/internal callers. Additive DTO
        change regardless of scoping: ``teams`` rows also carry the real
        competition ``league_id`` (``Team.league_id``, #283) alongside
        ``program_id``.
        """
        scoped = role is not None
        active_program = active_season = active_league = None
        if scoped:
            # An authenticated caller is ALWAYS scoped -- including on an
            # installation with zero Programs. There is deliberately no
            # "brand-new install" fall-through to the unfiltered shape here:
            # a Program-less install still holds pre-Program Clubs,
            # Organizations, Venues, Rinks, IceSlots and Officials (personal
            # names), and the absence of a Program never authorized a second
            # setup-capable account to enumerate them. Such a caller gets the
            # scoped-empty shape plus its OWN pending_link_* rows, which is
            # exactly what a create-then-link bootstrap needs. See the
            # docstring; the regression lives in
            # tests/test_zero_program_bootstrap_scoping.py.
            active_program, active_season, active_league = (
                self.context.resolve_with_league(user_id, role, scope))

        if not scoped:
            programs = self.store.all_programs()
            seasons = self.store.all_seasons()
            leagues = self.store.all_leagues()
            in_scope_season_ids = None
            active_league_id = None
        else:
            programs = [active_program] if active_program is not None else []
            # #367 owner ruling: the ACTIVE Season is the ceiling, not the
            # Program's whole set -- and a Program-only context (no Season
            # resolved) is EMPTY here, never a silent fall-back to "every
            # Season". `leagues` stays the Program's own set: a League is
            # permanent Program structure, not Season-bound data.
            in_scope_season_ids = (
                {active_season.id} if active_season is not None else set())
            seasons = ([active_season] if active_season is not None else [])
            leagues = (self.store.leagues_for_program(active_program.id)
                      if active_program is not None else [])
            active_league_id = (
                active_league.id if active_league is not None else None)

        ls_by_id = {ls.id: ls for ls in self.store.all_league_seasons()}
        in_scope_ls_ids = None
        if in_scope_season_ids is not None:
            in_scope_ls_ids = {
                ls.id for ls in ls_by_id.values()
                if ls.season_id in in_scope_season_ids
                and (active_league_id is None
                     or ls.league_id == active_league_id)}
        divisions = self.store.all_divisions()
        if in_scope_ls_ids is not None:
            divisions = [d for d in divisions
                        if d.league_season_id in in_scope_ls_ids]

        # A Team is PERMANENT Program+League structure (#283) -- not
        # Season-bound -- so the Season ceiling deliberately does not narrow
        # it; the selected League does.
        all_teams = self.store.all_teams()
        if not scoped:
            teams = all_teams
        elif active_program is not None:
            teams = [t for t in all_teams
                    if t.program_id == active_program.id
                    and (active_league_id is None
                         or t.league_id == active_league_id)]
        else:
            teams = []

        # -- Club/Organization/Official/Venue/Rink/IceSlot: derived joins --
        # (see docstring). Each entity gets a SCOPED list (a validated chain
        # into the active tuple) and, only when `scoped`, a `pending_link_*`
        # list: rows with NO validated link to ANY Program that THIS caller
        # created, so its own create-then-link flow can still reach them.
        all_clubs = self.store.all_clubs()
        all_orgs = self.store.all_organizations()
        all_venues = self.store.all_venues()
        all_rinks = self.store.all_rinks()
        all_officials = self.store.all_officials()
        all_slots = self.store.all_ice_slots()
        pending_clubs = pending_orgs = []
        pending_venues = pending_rinks = []
        pending_officials = pending_slots = []

        if not scoped:
            clubs, orgs, venues, rinks = all_clubs, all_orgs, all_venues, all_rinks
            officials, ice_slots = all_officials, all_slots
        else:
            # Rows this CALLER created, by entity type -- the ownership half
            # of the pending-link contract (the other half being "linked to
            # no Program at all"). See _creator_created_ids: CREATE entries
            # only, and nothing at all for an identity-less caller.
            created = self._creator_created_ids(user_id)

            # #367 owner ruling: a Club reaches a League THROUGH ITS TEAMS, so
            # the in-scope set derives from `teams` (already Program+League
            # narrowed), not from every Team in the Program.
            club_ids_any = {t.club_id for t in all_teams if t.club_id}
            club_ids_active = {t.club_id for t in teams if t.club_id}
            clubs = [c for c in all_clubs if c.id in club_ids_active]
            pending_clubs = [c for c in all_clubs
                             if c.id not in club_ids_any
                             and c.id in created["club"]]

            # "Linked to SOME Program" must be computed over EVERY edge into
            # Program, or a record that belongs to another Program falls
            # through into the pending-link list and leaks there -- to its
            # creator only now, but a leak is a leak. For a Venue that means
            # three edges, not one:
            #   * an ACTIVE grant (in scope only when it names the ACTIVE
            #     Season);
            #   * an INACTIVE/revoked grant -- history still ties the Venue to
            #     that Program, so it is not "nobody's data";
            #   * the legacy `Venue.league_id`, which despite its name stores
            #     a PROGRAM id (integrity_checks.py joins it to
            #     seasons.program_id). It is in scope for the active Program
            #     and a foreign link otherwise.
            all_sva = self.store.all_season_venue_access()
            venue_ids_any = {a.venue_id for a in all_sva}       # active OR revoked
            # A REVOKED grant to the ACTIVE Season keeps the Venue in scope,
            # deliberately: revoking only deactivates the row (history is
            # preserved), the Setup tree renders a "Revoked venue access"
            # section to manage exactly those rows, and delete_venue still
            # blocks on them regardless of active status. Scoping to ACTIVE
            # grants alone made such a Venue vanish from the payload
            # entirely -- unresolvable name in the very section that exists
            # to clean it up, and unreachable for the cleanup the
            # Season/Venue delete path requires first.
            #
            # #367 owner ruling: the facility tree has a SEASON axis
            # (SeasonVenueAccess) and NO competition-League axis, so
            # `in_scope_season_ids` -- the ACTIVE Season alone, empty for a
            # Program-only context -- is the whole filter here. It stays
            # active-Season-wide under ANY League selection, deliberately.
            venue_ids_active = {
                a.venue_id for a in all_sva
                if a.season_id in in_scope_season_ids}
            # `active_program` is None here whenever a SCOPED role resolves no
            # Program at all (a Coach with a dangling team_id) -- that path
            # still runs this branch, with every set already empty, so every
            # active-Program edge below must be guarded rather than assumed.
            venues = [v for v in all_venues
                     if v.id in venue_ids_active
                     or (active_program is not None and v.league_id
                         and v.league_id == active_program.id)]
            venue_ids_active = {v.id for v in venues}
            pending_venues = [v for v in all_venues
                              if v.id not in venue_ids_any and not v.league_id
                              and v.id in created["venue"]]

            rinks = [r for r in all_rinks if r.venue_id in venue_ids_active]
            # A Rink/IceSlot has no Program edge of its own: it is linked
            # exactly as far as its Venue/Rink is, so the pending-link
            # cascade runs over the UNLINKED venue set (every Venue with no
            # Program edge -- NOT just this caller's own), while ownership is
            # still required on the Rink/Slot row itself. Cascading over
            # `pending_venues` instead would hide a Rink the caller added to
            # an unlinked Venue somebody else created.
            unlinked_venue_ids = {v.id for v in all_venues
                                  if v.id not in venue_ids_any and not v.league_id}
            pending_rinks = [r for r in all_rinks
                             if r.venue_id in unlinked_venue_ids
                             and r.id in created["rink"]]

            rink_ids_scoped = {r.id for r in rinks}
            unlinked_rink_ids = {r.id for r in all_rinks
                                 if r.venue_id in unlinked_venue_ids}
            ice_slots = [s for s in all_slots if s.rink_id in rink_ids_scoped]
            pending_slots = [s for s in all_slots
                             if s.rink_id in unlinked_rink_ids
                             and s.id in created["ice_slot"]]

            # An Organization reaches a Program by TWO edges, and missing
            # either one leaks: it can own a Venue the Program uses, and it
            # can BE a Program's `operator_organization_id` (permanent Program
            # structure, which the ruling keeps Program-scoped). Leaving the
            # operator edge out put every other Program's operating
            # organization into the pending-link list -- i.e. disclosed its
            # name while the caller was working in a different Program,
            # exactly the leak that list must never reintroduce.
            #
            # Note the venue edge for "linked to SOME Program" uses
            # `venue_ids_any` (any grant, active or revoked, plus the legacy
            # Program link above); owning only a genuinely unlinked Venue is
            # NOT a Program link, and treating it as one previously put such
            # an Organization in NEITHER list -- invisible on the Setup
            # facility tree, taking its Venues down with it, since a Venue
            # renders under its owner and only a NULL-owner Venue reaches
            # the orphan section.
            linked_venue_ids = venue_ids_any | {v.id for v in all_venues
                                                if v.league_id}
            operator_org_ids_any = {p.operator_organization_id
                                    for p in self.store.all_programs()
                                    if p.operator_organization_id}
            org_ids_any = {v.organization_id for v in all_venues
                          if v.organization_id and v.id in linked_venue_ids}
            org_ids_any |= operator_org_ids_any
            org_ids_active = {v.organization_id for v in all_venues
                              if v.organization_id and v.id in venue_ids_active}
            if (active_program is not None
                    and active_program.operator_organization_id):
                org_ids_active.add(active_program.operator_organization_id)
            orgs = [o for o in all_orgs if o.id in org_ids_active]
            pending_orgs = [o for o in all_orgs
                            if o.id not in org_ids_any
                            and o.id in created["organization"]]

            # The Official edges, in the two forms this method needs them:
            # "assigned to a Game in the ACTIVE tuple" (in scope) and
            # "assigned to any resolvable Game at all" (linked to SOME
            # Program, and therefore never a pending-link row). The first
            # runs through the SHARED `_game_matches_active` -- the same
            # predicate the Dashboard applies, exhibition rule included --
            # because the ruling makes Officials League-bound "through an
            # in-scope home Club or assignment", so an assignment in a
            # sibling League's game is no more this League's data than that
            # game is.
            team_ids_active = {t.id for t in teams}

            def _official_games(official_id):
                for a in self.store.assignments_for_official(official_id):
                    g = self.store.get_game(a.game_id)
                    if g is not None and g.season_id:
                        yield g

            officials, pending_officials = [], []
            for o in all_officials:
                o_games = list(_official_games(o.id))
                in_active = ((o.home_club_id in club_ids_active)
                            or any(self._game_matches_active(
                                g, in_scope_season_ids, active_league_id,
                                team_ids_active) for g in o_games))
                has_any_link = ((o.home_club_id in club_ids_any)
                                or bool(o_games))
                if in_active:
                    officials.append(o)
                elif not has_any_link and o.id in created["official"]:
                    pending_officials.append(o)

        # Name-resolution lookups read from the FULL unfiltered store data
        # (never leaks anything -- these only resolve a name/label onto a
        # row that is already independently authorized to be shown).
        orgs_by_id = {o.id: o for o in all_orgs}
        clubs_by_id = {c.id: c for c in all_clubs}
        leagues_by_id = {lg.id: lg for lg in leagues}
        venues_by_id = {v.id: v for v in all_venues}
        rinks_by_id = {r.id: r for r in all_rinks}
        # A League's Season and a Division's Season + League resolve through
        # LeagueSeason now (#283); ``season_by_league`` maps a League to A
        # participating Season so the DTO keeps its legacy ``season_id`` (a
        # League bound to several Seasons reports only the first one found —
        # fine for that single display field, but ``seasons_by_league`` (#345)
        # additionally carries EVERY binding, because a consumer that needs to
        # know whether a League participates in a SPECIFIC Season (not just
        # "a" Season) cannot answer that from the lossy singular field). Built
        # from every LeagueSeason (not the Program-narrowed set above) — an
        # in-scope League's own bindings are already guaranteed in-Program by
        # the League/Season/Program invariant, so no extra narrowing changes
        # the result here.
        season_by_league = {}
        seasons_by_league = {}
        for ls in ls_by_id.values():
            season_by_league.setdefault(ls.league_id, ls.season_id)
            seasons_by_league.setdefault(ls.league_id, []).append(ls.season_id)

        def is_junior(div):
            tag = (div.age_group or div.name or "").upper()
            return tag.startswith("U")

        def _division_row_v2(d):
            lid = self._league_id_via(ls_by_id, d.league_season_id)
            return {"id": d.id,
                    "season_id": self._season_id_via(ls_by_id, d.league_season_id),
                    "name": d.name, "age_group": d.age_group,
                    "is_junior": is_junior(d), "league_id": lid,
                    "league_name": (leagues_by_id[lid].name
                                    if lid in leagues_by_id else None)}

        # Row-shape builders, applied identically to a main (scoped/
        # unfiltered) list and its `pending_link_*` counterpart, so both read
        # as the exact same DTO shape client-side.
        #
        # `visible` is the ownership contract's second half (#367 owner
        # ruling: visibility "cannot be gained by probing/replaying an id").
        # A pending-link row is offered because the caller CREATED it — and
        # that authorizes THE ROW, never the foreign records it happens to
        # point at. Every create route takes its parent id verbatim from the
        # request body and does not check that the caller may see that
        # parent, so resolving a parent's NAME from the full store turned the
        # create routes into a name oracle for exactly the identity the
        # ruling names: an Arena Manager (MANAGE_ARENA covers venue/rink/
        # ice-slot/organization creates AND this read, at zero authorized
        # Programs) POSTs a Rink at `venue_N`, an IceSlot at `rink_N`, a
        # Venue at `org_N` or an Official homed at `club_N` — ids are
        # sequential — and reads another operator's never-linked
        # `venue_name` / `rink_name` / `organization_name` / `home_club_name`
        # straight back out of its own pending row. Verified by probe before
        # this guard existed. So on a pending row every parent LABEL resolves
        # only against what this caller can already see (its scoped list plus
        # its own pending list) and is None otherwise; the parent id the
        # caller itself supplied stays, since it is not new information.
        # `visible=None` (every main list, and the whole unfiltered shape)
        # keeps the previous full resolution.
        def _resolved_name(by_id, parent_id, visible):
            if visible is not None and parent_id not in visible:
                return None
            parent = by_id.get(parent_id)
            return parent.name if parent is not None else None

        def _club_row(c):
            return {"id": c.id, "name": c.name}

        def _org_row(o):
            return {"id": o.id, "name": o.name, "short_name": o.short_name}

        def _official_row(o, visible=None):
            return {"id": o.id, "name": o.name,
                    "home_club_name": _resolved_name(
                        clubs_by_id, o.home_club_id, visible)}

        def _venue_row(v, visible=None):
            return {"id": v.id, "name": v.name, "address": v.address,
                    "timezone": v.timezone,
                    "organization_id": v.organization_id,
                    "organization_name": _resolved_name(
                        orgs_by_id, v.organization_id, visible)}

        def _rink_row(r, visible=None):
            return {"id": r.id, "venue_id": r.venue_id, "name": r.name,
                    "venue_name": _resolved_name(
                        venues_by_id, r.venue_id, visible)}

        def _slot_row(ic, visible=None):
            return {"id": ic.id, "rink_id": ic.rink_id,
                    "start_time": ic.start_time.isoformat(),
                    "end_time": ic.end_time.isoformat(),
                    "slot_type": ic.slot_type.value, "status": ic.status.value,
                    "rink_name": _resolved_name(
                        rinks_by_id, ic.rink_id, visible)}

        # What a PENDING row may resolve a parent name against: this caller's
        # scoped list for that kind, plus its own pending list for that kind
        # (a Rink the caller created under a Venue it also created must still
        # render the Venue's name — that is the create-then-link flow itself).
        # Anything else is a record this caller was never shown, so its name
        # is withheld. Built unconditionally: in the unfiltered shape every
        # pending list is empty, so these sets are never consulted there.
        visible_club_ids = {c.id for c in clubs} | {c.id for c in pending_clubs}
        visible_org_ids = {o.id for o in orgs} | {o.id for o in pending_orgs}
        visible_venue_ids = ({v.id for v in venues}
                             | {v.id for v in pending_venues})
        visible_rink_ids = {r.id for r in rinks} | {r.id for r in pending_rinks}

        return {
            "programs": [
                {"id": p.id, "name": p.name, "country": p.country,
                 "timezone": p.timezone,
                 "operator_organization_id": p.operator_organization_id,
                 "operator_organization_name": (
                     orgs_by_id[p.operator_organization_id].name
                     if p.operator_organization_id in orgs_by_id else None)}
                for p in programs],
            "seasons": [
                {"id": s.id, "program_id": s.program_id, "name": s.name,
                 "start_date": s.start_date.isoformat() if s.start_date else None,
                 "end_date": s.end_date.isoformat() if s.end_date else None,
                 # Lifecycle state (#159): archived Seasons stay in the read
                 # payload (history remains visible) but carry their status so
                 # the UI can flag them and exclude them from active-work pickers.
                 "status": s.status.value,
                 "archived_at": (s.archived_at.isoformat()
                                 if s.archived_at else None)}
                for s in seasons],
            "leagues": [
                {"id": lg.id, "season_id": season_by_league.get(lg.id),
                 "season_ids": seasons_by_league.get(lg.id, []),
                 "name": lg.name, "sort_order": lg.sort_order}
                for lg in leagues],
            "divisions": [_division_row_v2(d) for d in divisions],
            "teams": [
                {"id": t.id, "name": t.name, "club_id": t.club_id,
                 "program_id": t.program_id, "league_id": t.league_id,
                 "club_name": (clubs_by_id[t.club_id].name
                               if t.club_id in clubs_by_id else None)}
                for t in teams],
            "clubs": [_club_row(c) for c in clubs],
            "organizations": [_org_row(o) for o in orgs],
            # Officials are shown on the Setup surface (no legacy field rename);
            # sourced here so the whole Setup page reads from this canonical
            # endpoint rather than the v1 demo overview.
            "officials": [_official_row(o) for o in officials],
            "venues": [_venue_row(v) for v in venues],
            "rinks": [_rink_row(r) for r in rinks],
            "ice_slots": [_slot_row(ic) for ic in
                         sorted(ice_slots, key=lambda x: (x.rink_id, x.start_time))],
            # #367 owner ruling: the CREATOR-OWNED create-then-link lists,
            # replacing the reversed installation-wide `unassigned_*`
            # mechanism (see docstring). A row appears here only when it has
            # no validated link to ANY Program AND this caller created it, so
            # a freshly-created Club/Venue/Organization/Official/Rink/IceSlot
            # stays pickable and manageable by the operator performing the
            # flow, and by nobody else. Always empty for the unfiltered
            # (`role=None` / zero-Program bootstrap) case, where the main
            # lists above are already unfiltered.
            # Parent LABELS on these rows resolve only against what this
            # caller can already see -- its own scoped list plus its own
            # pending list -- so creating a child under a guessed parent id
            # cannot read that parent's name back out (see `_resolved_name`).
            "pending_link_clubs": [_club_row(c) for c in pending_clubs],
            "pending_link_organizations": [_org_row(o) for o in pending_orgs],
            "pending_link_officials": [
                _official_row(o, visible_club_ids)
                for o in pending_officials],
            "pending_link_venues": [
                _venue_row(v, visible_org_ids) for v in pending_venues],
            "pending_link_rinks": [
                _rink_row(r, visible_venue_ids) for r in pending_rinks],
            "pending_link_ice_slots": [
                _slot_row(ic, visible_rink_ids) for ic in
                sorted(pending_slots,
                       key=lambda x: (x.rink_id, x.start_time))],
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
            # Active-directory count (#270): inactive players are excluded from
            # the team roster count shown in the setup hierarchy; historical
            # rows still render, but the directory reflects the active roster.
            if not p.is_active:
                continue
            player_count[p.team_id] = player_count.get(p.team_id, 0) + 1

        rinks_by_venue = _group(rinks, "venue_id")
        venues_by_league = _group(venues, "league_id")
        leagues_by_org = _group(leagues, "operator_organization_id")
        seasons_by_league = _group(seasons, "program_id")
        # Levels (grouping Leagues) and Divisions no longer carry season_id
        # directly (#283) — group them by Season through LeagueSeason.
        ls_by_id = {ls.id: ls for ls in self.store.all_league_seasons()}
        levels_by_id = {lv.id: lv for lv in levels}
        levels_by_season = {}
        for ls in ls_by_id.values():
            lv = levels_by_id.get(ls.league_id)
            if lv is not None:
                levels_by_season.setdefault(ls.season_id, []).append(lv)
        divs_by_season = {}
        for d in divisions:
            sid = self._season_id_via(ls_by_id, d.league_season_id)
            if sid is not None:
                divs_by_season.setdefault(sid, []).append(d)
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
                divs_by_level = {}
                for d in divs_by_season.get(s.id, []):
                    lid = self._league_id_via(ls_by_id, d.league_season_id)
                    if lid is not None:
                        divs_by_level.setdefault(lid, []).append(d)
                # v1 hierarchy shape is frozen (#233) — the LeagueSeason binding
                # id needed for the #159 unbind is exposed on the v2 hierarchy
                # only (get_setup_hierarchy_v2), never broadened into v1.
                level_nodes = [
                    {"id": lv.id, "name": lv.name, "sort_order": lv.sort_order,
                     "divisions": [division_node(d) for d in divs_by_level.get(lv.id, [])]}
                    for lv in season_levels
                ]
                # Divisions in this season with no level (or a dangling one).
                no_level = [
                    d for d in divs_by_season.get(s.id, [])
                    if self._league_id_via(ls_by_id, d.league_season_id)
                    not in level_ids]
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
                [idname(d) for d in divisions
                 if self._league_id_via(ls_by_id, d.league_season_id)
                 not in level_ids_all],
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
    def get_setup_hierarchy_v2(self, user_id=None, role=None, scope=None) -> dict:
        """Canonical Program→Season→League→Division setup tree (#233 Slice C2).

        #367 prerequisite: when a real user context is supplied (``role`` is
        not ``None`` — the HTTP route always supplies one now), the tree is
        ceilinged to the caller's ACTIVE Program, the same ceiling every other
        scoped Setup read uses. It was previously installation-wide, and that
        was not merely a display inconsistency with the now-scoped Setup
        Records: ``app.js`` builds ``allPermLeagues`` from THIS payload, and
        that array is the option list for the Team drawer's "Permanent league"
        select — its options are labelled "<program> · <league>" precisely
        because they spanned Programs. An operator working in Program B could
        therefore pick Program A's League and create a Team under it. Scoping
        the read narrows what the picker can offer; the write is refused
        independently at the route, because a picker is a convenience and
        never the authorization boundary.

        Called with no arguments (``role`` left ``None``, the default) the full
        unfiltered tree is returned exactly as before — several internal
        callers and the hierarchy tests inspect whole-store state that way.

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
        # #371 review: whether this payload is Program-ceilinged at all. The
        # legacy identity-less internal form (role is None) stays unfiltered.
        scoped_hierarchy = role is not None
        if role is not None:
            active, _season, _league = self.context.resolve_with_league(
                user_id, role, scope)
            # Season/League are NOT further ceilings here: this is the
            # structural management tree, so every Season and League of the
            # active Program stays visible (same rule get_setup_overview_v2
            # applies). Only the Program axis narrows.
            pid = active.id if active is not None else None
            programs = [p for p in programs if p.id == pid]
            season_ids = {s.id for s in seasons if s.program_id == pid}
            seasons = [s for s in seasons if s.id in season_ids]
            leagues = [lg for lg in leagues if lg.program_id == pid]
            league_ids = {lg.id for lg in leagues}
            ls_by_id = {ls.id: ls for ls in self.store.all_league_seasons()}
            divisions = [
                d for d in divisions
                if (ls := ls_by_id.get(d.league_season_id)) is not None
                and ls.season_id in season_ids and ls.league_id in league_ids]
            teams = [t for t in teams if t.program_id == pid]

        player_count = {}
        for p in self.store.all_players():
            # Active-directory count (#270): inactive players are excluded from
            # the team roster count shown in the setup hierarchy; historical
            # rows still render, but the directory reflects the active roster.
            if not p.is_active:
                continue
            player_count[p.team_id] = player_count.get(p.team_id, 0) + 1

        seasons_by_program = _group(seasons, "program_id")
        # Leagues, Divisions and registrations resolve their Season (and a
        # Division/registration its League) via LeagueSeason now (#283).
        ls_by_id = {ls.id: ls for ls in self.store.all_league_seasons()}
        leagues_by_id = {lv.id: lv for lv in leagues}
        leagues_by_season = {}
        for ls in ls_by_id.values():
            lv = leagues_by_id.get(ls.league_id)
            if lv is not None:
                leagues_by_season.setdefault(ls.season_id, []).append(lv)
        divs_by_season = {}
        for d in divisions:
            sid = self._season_id_via(ls_by_id, d.league_season_id)
            if sid is not None:
                divs_by_season.setdefault(sid, []).append(d)
        divisions_by_id = {d.id: d for d in divisions}
        teams_by_id = {t.id: t for t in teams}
        # The ACTIVE PROGRAM's League set, named apart from the per-Season
        # `league_ids` rebound inside the Season loop below -- that one is the
        # leagues of ONE Season, and validating against it would blank a
        # perfectly legitimate League of this Program merely because the row
        # belongs to a different Season of it.
        scoped_league_ids = {lg.id for lg in leagues}

        def _scoped_league_ref(league_id):
            """A League id only when it is one of the ACTIVE Program's.

            `dangling_divs` is built from the already-narrowed `divisions`, so
            today its League always resolves in scope and this is a no-op --
            it is here because the ruling requires every emitted reference to
            be validated independently, and because the Team id leak began
            exactly as a field that "could not" be foreign until the narrowing
            around it changed.
            """
            if scoped_hierarchy and league_id not in scoped_league_ids:
                return None
            return league_id

        def _needs_assignment_row(reg, reason, reg_league_id):
            """One ``needs_assignment.registrations`` row, with EVERY foreign
            reference withheld on a scoped read (#371 review).

            Three independent references, three independent checks. The first
            round of this fix validated only ``team_id`` and shipped green,
            leaving ``league_id`` and ``division_id`` verbatim -- and a repair
            row naming a foreign League or Division discloses precisely what
            the Team fix existed to stop. Both remaining paths were reachable:
            a LeagueSeason joining this Program's Season to ANOTHER Program's
            League, and a registration carrying another Program's Division id.

            ``teams_by_id`` is already narrowed to the active Program above, so
            a registration whose team is missing from it names either a Team
            that does not exist or one belonging to ANOTHER Program. Emitting
            that id handed the caller a probeable identifier for a Program it
            cannot read -- the same disclosure the name filtering exists to
            prevent, and worse, because an id is exactly what a later write
            needs. Filtering names while returning the identifier is not a
            ceiling.

            The row is KEPT rather than dropped: the operator still has to see
            that this Season holds a registration needing repair. Only the id
            is replaced, by ``None`` -- a repair signal that cannot be joined
            back to the foreign Team. Both cases collapse to the same
            ``team_missing`` reason here, so the response also cannot
            distinguish "belongs to another Program" from "does not exist".

            The legacy unscoped internal form is untouched: several call sites
            read the whole installation through it.
            """
            team_id, league_id = reg.team_id, reg_league_id
            division_id = reg.division_id
            if scoped_hierarchy:
                # EVERY reference is validated independently. Withholding only
                # the Team id left the other two verbatim, and a repair row
                # naming a foreign League or Division discloses exactly the
                # same thing: a joinable identifier from outside the ceiling.
                # Membership in `divisions_by_id` already implies the Division
                # resolves through a LeagueSeason of this Program's Seasons and
                # Leagues (that is how `divisions` was narrowed above), so it
                # carries the consistency requirement with it.
                if team_id not in teams_by_id:
                    team_id = None
                if league_id not in scoped_league_ids:
                    league_id = None
                if division_id not in divisions_by_id:
                    division_id = None
            return {"registration_id": reg.id, "team_id": team_id,
                    "league_id": league_id,
                    "division_id": division_id, "reason": reason}

        # Active registrations grouped by their Season, so each Season resolves
        # participation from its own rows.
        regs_by_season = {}
        for reg in self.store.all_season_team_registrations():
            if reg.active:
                sid = self._season_id_via(ls_by_id, reg.league_season_id)
                if sid is not None:
                    regs_by_season.setdefault(sid, []).append(reg)

        def team_node(t, registration_id=None):
            # #331 review round 19: ``registration_id`` is the SPECIFIC active
            # SeasonTeamRegistration this node represents -- ``None`` for the
            # permanent Program->League->Team tree below (no Season/
            # registration involved there), and the exact row's id for every
            # Season-participation node (division-nested or league-direct).
            # A Team with two active registrations in one Season (a Rule 7
            # violation legacy data/a write path predating Rule 7 can leave
            # behind) previously produced two structurally-identical nodes a
            # consumer could only re-associate with a lossy (team_id,
            # league_id) reconstruction -- exactly the shape
            # renderSeasonParticipation's own regByTeamLeague keying (#331
            # review round 18) has to guess at. Carrying the row's own id
            # lets a consumer key off it directly instead of guessing.
            return {"id": t.id, "name": t.name, "club_id": t.club_id,
                    "program_id": t.program_id,
                    "registration_id": registration_id,
                    "player_count": player_count.get(t.id, 0)}

        # #283 Slice B: the PERMANENT Program → League → Team structure (the
        # competition membership that persists across Seasons), surfaced
        # alongside the per-Season participation tree below. A Team belongs to
        # exactly one permanent League (Team.league_id); a Team with a Program
        # but no League yet is surfaced under ``teams_without_league`` so an
        # operator can assign one (rule 2 remediation).
        leagues_by_program = _group(leagues, "program_id")
        teams_by_league = {}
        teams_without_league_by_program = {}
        for t in teams:
            if t.league_id:
                teams_by_league.setdefault(t.league_id, []).append(t)
            elif t.program_id:
                teams_without_league_by_program.setdefault(
                    t.program_id, []).append(t)
        ls_count_by_league = {}
        for ls in ls_by_id.values():
            ls_count_by_league[ls.league_id] = \
                ls_count_by_league.get(ls.league_id, 0) + 1

        program_tree = []
        for prog in programs:
            season_nodes = []
            for s in seasons_by_program.get(prog.id, []):
                season_leagues = sorted(
                    leagues_by_season.get(s.id, []),
                    key=lambda lv: (lv.sort_order or 0, lv.name))
                league_ids = {lv.id for lv in season_leagues}
                divs_by_league = {}
                for d in divs_by_season.get(s.id, []):
                    lid = self._league_id_via(ls_by_id, d.league_season_id)
                    if lid is not None:
                        divs_by_league.setdefault(lid, []).append(d)

                # Resolve every active registration in this Season into exactly
                # one bucket: a valid Division nest, a valid League-direct team,
                # or an invalid (needs-assignment) row.
                teams_by_div = {}                 # division_id -> [(team, reg)]
                teams_direct_by_league = {}       # league_id  -> [(team, reg)]
                needs_assignment_regs = []
                for reg in regs_by_season.get(s.id, []):
                    # The registration's competition League now resolves via its
                    # LeagueSeason (#283); the DTO keeps the legacy league_id key.
                    reg_league_id = self._league_id_via(
                        ls_by_id, reg.league_season_id)
                    tm = teams_by_id.get(reg.team_id)
                    if tm is None:
                        needs_assignment_regs.append(
                            _needs_assignment_row(
                                reg, "team_missing", reg_league_id))
                        continue
                    # A registration's Team must belong to THIS Program — a
                    # cross-Program (or program-less legacy) Team is invalid
                    # structure and is surfaced for reassignment, never shown as a
                    # valid branch under this Program's tree (#233 Slice C2 review).
                    if tm.program_id != prog.id:
                        needs_assignment_regs.append(
                            _needs_assignment_row(
                                reg, "team_program_mismatch", reg_league_id))
                        continue
                    if reg.division_id:
                        div = divisions_by_id.get(reg.division_id)
                        # A Team nests under its Division only when the Division
                        # shares the registration's LeagueSeason (same Season AND
                        # League) and that League is a real League in this Season.
                        if (div is not None
                                and div.league_season_id == reg.league_season_id
                                and reg_league_id in league_ids):
                            teams_by_div.setdefault(div.id, []).append((tm, reg))
                        else:
                            needs_assignment_regs.append(
                                _needs_assignment_row(
                                reg, "registration_league_division_mismatch", reg_league_id))
                    else:
                        # Division-less: hangs directly off its registration
                        # League when that League is a real League in this Season.
                        if reg_league_id in league_ids:
                            teams_direct_by_league.setdefault(
                                reg_league_id, []).append((tm, reg))
                        else:
                            needs_assignment_regs.append(
                                _needs_assignment_row(
                                reg, "registration_league_not_in_season", reg_league_id))

                def division_node(d):
                    return {"id": d.id, "name": d.name, "age_group": d.age_group,
                            "league_id": self._league_id_via(
                                ls_by_id, d.league_season_id),
                            "teams": [team_node(t, reg.id)
                                      for t, reg in teams_by_div.get(d.id, [])]}

                league_nodes = [
                    {"id": lv.id, "name": lv.name, "sort_order": lv.sort_order,
                     # #159 — the LeagueSeason binding id, so the UI can drive
                     # the explicit unbind (delete_league_season) that clears a
                     # League's binding dependency before deletion.
                     "league_season_id": getattr(
                         self.store.league_season_for(lv.id, s.id), "id", None),
                     "divisions": [division_node(d)
                                   for d in divs_by_league.get(lv.id, [])],
                     # Division-optional: teams registered directly under this
                     # League with no Division.
                     "teams_without_division": [
                         team_node(t, reg.id)
                         for t, reg in teams_direct_by_league.get(lv.id, [])]}
                    for lv in season_leagues
                ]
                # Parentless / dangling Divisions (no League, or a League that
                # doesn't resolve in this Season) are INVALID structure — surfaced
                # for reassignment, never presented as a valid branch.
                dangling_divs = [
                    d for d in divs_by_season.get(s.id, [])
                    if self._league_id_via(ls_by_id, d.league_season_id)
                    not in league_ids]
                season_nodes.append({
                    "id": s.id, "name": s.name, "leagues": league_nodes,
                    "needs_assignment": {
                        "divisions_without_league": [
                            {"id": d.id, "name": d.name, "age_group": d.age_group,
                             "league_id": _scoped_league_ref(
                                 self._league_id_via(
                                     ls_by_id, d.league_season_id))}
                            for d in dangling_divs],
                        "registrations": needs_assignment_regs,
                    },
                })
            # #283 Slice B: the permanent League tree for this Program — the
            # competition membership that persists across Seasons. Each League
            # carries how many Seasons it participates in (season_count) and its
            # currently-assigned Teams. Teams with a Program but no League yet
            # are surfaced separately so an operator can assign one.
            perm_leagues = sorted(
                leagues_by_program.get(prog.id, []),
                key=lambda lv: (lv.sort_order or 0, lv.name))
            program_tree.append({
                "id": prog.id, "name": prog.name,
                "operator_organization_id": prog.operator_organization_id,
                "leagues": [
                    {"id": lv.id, "name": lv.name, "sort_order": lv.sort_order,
                     "season_count": ls_count_by_league.get(lv.id, 0),
                     "teams": [team_node(t)
                               for t in sorted(teams_by_league.get(lv.id, []),
                                               key=lambda t: t.name)]}
                    for lv in perm_leagues],
                "teams_without_league": [
                    team_node(t)
                    for t in sorted(
                        teams_without_league_by_program.get(prog.id, []),
                        key=lambda t: t.name)],
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
        # Season boundaries accept a date-only 'YYYY-MM-DD' or a timezone-aware
        # ISO-8601 timestamp (#272). Parsing happens in the service where the
        # Program's timezone (the anchor for a date-only value) is known, so the
        # raw values are forwarded unparsed rather than pre-validated here.
        return _serialize(self.setup.create_season(
            program_id, name, start_date, end_date, actor_id))

    @catch
    def archive_season(self, season_id: str, reason: Optional[str] = None,
                       actor_id: Optional[str] = None) -> dict:
        """Archive a Season into read-only historical mode (#159)."""
        return _serialize(self.setup.archive_season(
            season_id, actor_id=actor_id, reason=reason))

    @catch
    def reopen_season(self, season_id: str, reason: Optional[str] = None,
                      actor_id: Optional[str] = None) -> dict:
        """Reopen an archived Season back to active (#159). Requires a reason."""
        return _serialize(self.setup.reopen_season(
            season_id, actor_id=actor_id, reason=reason))

    @catch
    def create_league(self, season_id: str, name: str, sort_order: int = 0,
                      actor_id: Optional[str] = None) -> dict:
        return self._league_dict(
            self.setup.create_league(season_id, name, sort_order, actor_id))

    @catch
    def create_division(self, season_id: str, name: str, age_group: str = "",
                        league_id: Optional[str] = None,
                        actor_id: Optional[str] = None) -> dict:
        return self._division_dict(self.setup.create_division(
            season_id, name, age_group, league_id, actor_id))

    @catch
    def create_division_v2(self, league_id: str, name: str, age_group: str = "",
                           actor_id: Optional[str] = None, *,
                           season_id: Optional[str] = None) -> dict:
        """Canonical v2 division create (#233 Slice C2): parented by a grouping
        League (REQUIRED); Season is derived from the league, unless the
        optional ``season_id`` (#345) selects one exact binding for a League
        bound to several Seasons. ``season_id`` is keyword-only, added AFTER
        ``actor_id`` in the existing (league_id, name, age_group, actor_id)
        order -- a legacy positional caller's fourth argument must stay the
        actor, never silently become a Season id."""
        return self._division_dict(self.setup.create_division_under_league(
            league_id, name, age_group, actor_id, season_id=season_id))

    @catch
    def create_club(self, name: str, country: str = "",
                    actor_id: Optional[str] = None) -> dict:
        return _serialize(self.setup.create_club(name, country, actor_id))

    @catch
    def create_team(self, club_id: Optional[str] = None,
                    division_id: Optional[str] = None,
                    name: str = "", actor_id: Optional[str] = None,
                    program_id: Optional[str] = None,
                    league_id: Optional[str] = None) -> dict:
        # #283 Slice B: ``league_id`` is the permanent-League assignment (rule 2)
        # the Setup UI supplies when creating a Team under its competition
        # League. The setup service validates it and keeps Program consistent.
        team = _serialize(self.setup.create_team(
            club_id, division_id, name, actor_id, program_id=program_id,
            league_id=league_id))
        # Drop the new competition Team.league_id from this shared response
        # shape — the canonical v2 team excludes it, and the v1 adapter derives
        # its league_id from program_id (team_to_v1). Neither exposes the raw
        # field on the wire.
        team.pop("league_id", None)
        return team

    # -- permanent teams + season registrations (#180) ---------------------
    @catch
    def register_team_for_season(self, season_id: str, team_id: str,
                                 division_id: Optional[str] = None,
                                 actor_id: Optional[str] = None,
                                 league_id: Optional[str] = None) -> dict:
        # ``league_id`` is the v2 canonical path (#233 Slice C2): when supplied
        # it is required-and-validated; when omitted (v1) the C1b derivation runs.
        return self._registration_dict(self.setup.register_team_for_season(
            season_id, team_id, division_id, actor_id, league_id=league_id))

    @catch
    def assign_season_team_division(self, registration_id: str,
                                    division_id: Optional[str] = None,
                                    actor_id: Optional[str] = None,
                                    v2: bool = False) -> dict:
        return self._registration_dict(self.setup.assign_season_team_division(
            registration_id, division_id, actor_id, v2=v2))

    @catch
    def assign_season_team_league(self, registration_id: str,
                                  league_id: Optional[str] = None,
                                  actor_id: Optional[str] = None) -> dict:
        """Canonical v2 (#233 Slice C2): reassign a registration's League."""
        return self._registration_dict(self.setup.assign_season_team_league(
            registration_id, league_id, actor_id))

    @catch
    def unregister_team_from_season(self, registration_id: str,
                                    actor_id: Optional[str] = None) -> dict:
        return self._registration_dict(self.setup.unregister_team_from_season(
            registration_id, actor_id))

    @catch
    def delete_season_team_registration(self, registration_id: str,
                                        actor_id: Optional[str] = None) -> dict:
        # setup.delete_season_team_registration already returns a rich v1 dict
        # (with season_name/team_name/league_name/division_name); return as-is.
        return self.setup.delete_season_team_registration(
            registration_id, actor_id)

    # -- season venue access (#233 Slice E) ---------------------------------
    @catch
    def grant_season_venue_access(self, season_id: str, venue_id: str,
                                  actor_id: Optional[str] = None) -> dict:
        return _serialize(self.setup.grant_season_venue_access(
            season_id, venue_id, actor_id))

    @catch
    def revoke_season_venue_access(self, access_id: str,
                                   actor_id: Optional[str] = None) -> dict:
        return _serialize(self.setup.revoke_season_venue_access(
            access_id, actor_id))

    @catch
    def delete_season_venue_access(self, access_id: str,
                                   actor_id: Optional[str] = None) -> dict:
        return _serialize(self.setup.delete_season_venue_access(
            access_id, actor_id))

    @catch
    def list_season_venue_access(self, season_id: str, user_id=None, role=None,
                                 scope=None) -> dict:
        """This Season's venue grants, each naming its own Venue.

        SCOPED exactly like :meth:`get_venue_grant_candidates` (#369 review):
        ``season_id`` must BE the caller's persisted active Season, and a
        foreign, sibling or nonexistent ``season_id`` takes the same generic
        not-found path -- so this is neither a Season oracle nor a way to name
        another Program's facilities.

        That check is the whole point of the identity parameters. This read had
        only a role-level MANAGE_SETUP gate on its route and took the requested
        id on trust, which was survivable while it echoed opaque grant rows but
        stopped being so the moment ``venue_name`` was added below: with
        Program A active, asking for Program B's Season answered 200 with B's
        Venue id AND its name, while a guessed id answered 200 with an empty
        list -- a cross-Program facility disclosure and an existence oracle in
        one route. Role-level permission says the caller may manage SOME
        Season's grants; it never says which Season, and only the persisted
        active tuple can say that.

        ``venue_name`` itself is additive and stays, but only past that check. A
        Season may hold a grant to a Venue belonging to ANOTHER Program -- that
        is what shared arenas are -- and once cross-Program Venues stopped
        appearing in the ordinary scoped overview, there was nowhere left for
        the client to resolve that name from, so the Allowed-venues row
        rendered a bare id. Naming the Venue on a grant the caller's OWN active
        Program already holds is not a cross-Program directory: it discloses
        only a facility that Season demonstrably already uses, to a caller who
        has been proven entitled to that Season's grants.

        Ceilinged to the EXACT selected Season, deliberately and identically to
        the candidate route (#369 owner ruling). The first version of this
        check compared Programs, so any SIBLING Season of the active Program
        answered 200 with its grants and its Venue names; the owner ruled that
        same-Program/other-Season 200 incorrect. It was written that way to
        serve the client, which walked every Season of the active Program in
        ``get_setup_hierarchy_v2`` and read allowed venues for each -- i.e.
        used this route as an all-Seasons inventory. That use has stopped:
        ``app.js`` now reads this route for the selected Season only, and
        renders a non-identifying placeholder for the rest. A Program-only
        context (no Season selected) fails closed for the same reason -- there
        is no selected Season for the request to equal.
        """
        if role is None:
            return {"error": {"code": "forbidden",
                              "message": "An identity is required."}}
        program, active_season, _league = self.context.resolve_with_league(
            user_id, role, scope)
        # One generic refusal for every miss -- foreign Program, sibling Season
        # of the active Program, nonexistent id, Program-only context -- so no
        # one of them is distinguishable from the others.
        if (program is None or active_season is None
                or not season_id or season_id != active_season.id):
            raise NotFoundError(f"Season {season_id} not found.")
        season = active_season
        names = {v.id: v.name for v in self.store.all_venues()}
        rows = []
        for a in self.store.season_venue_access_for_season(season.id):
            row = _serialize(a)
            row["venue_name"] = names.get(a.venue_id)
            rows.append(row)
        return {"venue_access": rows}

    @catch
    def roll_forward_registrations(self, from_season_id: str, to_season_id: str,
                                   selections: Optional[list] = None,
                                   actor_id: Optional[str] = None) -> dict:
        result = self.setup.roll_forward_registrations(
            from_season_id, to_season_id, selections, actor_id)
        return {"rolled_forward": result["rolled_forward"],
                "skipped": result["skipped"],
                "registrations": [self._registration_dict(r)
                                  for r in result["registrations"]]}

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
                "registrations": [self._registration_dict(r)
                                  for r in result["registrations"]]}

    @catch
    def list_season_team_registrations(self, season_id: str) -> dict:
        rows = [self._registration_dict(r)
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
        return self._league_dict(self.setup.delete_league(league_id, actor_id))

    @catch
    def delete_league_season(self, league_season_id: str,
                             actor_id: Optional[str] = None) -> dict:
        # Explicit, authorized, audited unbind of a League↔Season binding (#159)
        # — the operator step that clears a League's binding dependency before
        # the League itself can be deleted (no silent cascades).
        return self.setup.delete_league_season(league_season_id, actor_id)

    @catch
    def delete_season(self, season_id: str, actor_id: Optional[str] = None) -> dict:
        return _serialize(self.setup.delete_season(season_id, actor_id))

    @catch
    def delete_division(self, division_id: str,
                        actor_id: Optional[str] = None) -> dict:
        # setup.delete_division already returns a rich v1-shaped dict (with
        # inactive_registrations_cleaned); return it as-is (#283).
        return self.setup.delete_division(division_id, actor_id)

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

    @catch
    def delete_official(self, official_id: str,
                        actor_id: Optional[str] = None) -> dict:
        return _serialize(self.setup.delete_official(official_id, actor_id))

    @catch
    def delete_player(self, player_id: str,
                      actor_id: Optional[str] = None) -> dict:
        return _serialize(self.setup.delete_player(player_id, actor_id))

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
        return self._division_dict(self.setup.assign_division_league(
            division_id, league_id, actor_id, v2=v2))

    @catch
    def assign_team_club(self, team_id: str, club_id: Optional[str] = None,
                         actor_id: Optional[str] = None) -> dict:
        return _serialize(self.setup.assign_team_club(
            team_id, club_id, actor_id))

    @catch
    def transfer_team_to_league(self, team_id: str, league_id: str,
                                actor_id: Optional[str] = None) -> dict:
        """#283 Slice B: move a Team to a different permanent League
        (promotion/relegation/transfer, rule 10). History is untouched.

        The raw competition ``Team.league_id`` is dropped from the response —
        the shared team shape never exposes it (see ``create_team``)."""
        team = _serialize(self.setup.transfer_team_to_league(
            team_id, league_id, actor_id))
        team.pop("league_id", None)
        return team

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

    # Ice Availability Builder (#158): preview then idempotently commit a
    # recurring block of AVAILABLE Game ice. The service returns plain dicts.
    @catch
    def preview_ice_availability(self, season_id: str = None, rink_ids=None,
                                 weekdays=None, start_local: str = None,
                                 end_local: str = None, start_date: str = None,
                                 end_date: str = None, playable_minutes=None,
                                 turnover_minutes=None, exclusion_dates=None,
                                 windows=None,
                                 actor_id: Optional[str] = None) -> dict:
        return self.setup.preview_ice_availability(
            season_id=season_id, rink_ids=rink_ids, weekdays=weekdays,
            start_local=start_local, end_local=end_local,
            start_date=start_date, end_date=end_date,
            playable_minutes=playable_minutes, turnover_minutes=turnover_minutes,
            exclusion_dates=exclusion_dates, windows=windows, actor_id=actor_id)

    @catch
    def commit_ice_availability(self, season_id: str = None, rink_ids=None,
                                weekdays=None, start_local: str = None,
                                end_local: str = None, start_date: str = None,
                                end_date: str = None, playable_minutes=None,
                                turnover_minutes=None, exclusion_dates=None,
                                windows=None, template_fingerprint: str = None,
                                actor_id: Optional[str] = None) -> dict:
        return self.setup.commit_ice_availability(
            season_id=season_id, rink_ids=rink_ids, weekdays=weekdays,
            start_local=start_local, end_local=end_local,
            start_date=start_date, end_date=end_date,
            playable_minutes=playable_minutes, turnover_minutes=turnover_minutes,
            exclusion_dates=exclusion_dates, windows=windows,
            template_fingerprint=template_fingerprint, actor_id=actor_id)

    @catch
    def set_scheduling_policy(self, scope_type=None, scope_id=None,
                              warmup_minutes=None, resurfacing_minutes=None,
                              min_playable_minutes=None, curfew_local=None,
                              actor_id: Optional[str] = None) -> dict:
        """Upsert (or, with every value ``None``, clear) one scope's
        scheduling policy (#277 Slice B). The response echoes the stored row
        (``policy: None`` after a clear) so the settings form can re-render
        from the write's own result."""
        policy = self.setup.set_scheduling_policy(
            scope_type, scope_id,
            warmup_minutes=warmup_minutes,
            resurfacing_minutes=resurfacing_minutes,
            min_playable_minutes=min_playable_minutes,
            curfew_local=curfew_local, actor_id=actor_id)
        return {"scope_type": scope_type, "scope_id": scope_id,
                "policy": _serialize(policy) if policy is not None else None}

    @catch
    def get_scheduling_policy(self, scope_type=None, scope_id=None,
                              season_id=None) -> dict:
        """One scope's stored policy row plus, for a RINK scope with a
        ``season_id``, the RESOLVED effective values with each set field's
        source scope — the "inherited from Season" affordance the settings
        UI renders (#277 Slice B)."""
        policy = self.setup.get_scheduling_policy(scope_type, scope_id)
        out = {"scope_type": scope_type, "scope_id": scope_id,
               "policy": _serialize(policy) if policy is not None else None}
        if scope_type == "rink":
            values, sources = self.setup._effective_policy(
                scope_id, season_id)
            out["effective"] = values
            out["effective_sources"] = sources
        return out

    @catch
    def create_player(self, team_id: str, name: str, position: str,
                      jersey_number: Optional[int] = None,
                      email: Optional[str] = None,
                      shoots: Optional[str] = None,
                      is_active: bool = True,
                      actor_id: Optional[str] = None) -> dict:
        # Pass position through raw: the service's canonical _validate_position
        # parses/validates it with a field-level invalid_position error (#268
        # review), so create and edit share one validator and the same field.
        return _serialize(self.setup.add_player(
            team_id, name, position,
            jersey_number=jersey_number, email=email, shoots=shoots,
            is_active=is_active, actor_id=actor_id))

    @catch
    def list_players(self, team_id: Optional[str] = None,
                     include_email: bool = False, user_id=None, role=None,
                     scope=None) -> List[dict]:
        """#369 self-audit: when a real user context is supplied, the
        unfiltered list narrows to Teams in the caller's ACTIVE Program.

        This is the Setup "Clubs, players and staff" list, and it was the one
        Setup collection still answering installation-wide after the rest of
        the surface was ceilinged — it returned every Player NAME (and, on
        this MANAGE_SETUP route, every email) in the installation, including
        other Programs'. Names here are usually juniors, which is why the
        route already refuses to be bundled into an unauthenticated payload;
        the same reasoning makes a cross-Program answer wrong. Called with no
        user context (the default), behavior is unchanged for existing
        internal callers.
        """
        players = (self.store.players_for_team(team_id) if team_id
                  else self.store.all_players())
        # The Program ceiling applies to BOTH forms. An earlier revision
        # scoped only the unfiltered form (`team_id is None and role is not
        # None`), which left an IDOR: `?team_id=<another Program's team>`
        # skipped the gate entirely and returned that Team's players -- and,
        # on this MANAGE_SETUP route, their emails. `team_id` is a caller-
        # supplied identifier, so it selects WHICH rows to consider; it can
        # never be evidence of authorization for them.
        if role is not None:
            program, _season, league = self.context.resolve_with_league(
                user_id, role, scope)
            if program is None:
                players = []
            else:
                # A selected League narrows here too, via the Team's REAL
                # permanent competition League (`Team.league_id`, #283 -- not
                # any of the legacy `league_id` fields that store a Program).
                # Players are League-narrowable in a way Clubs/Organizations/
                # Venues are not, and the rest of the surface already treats
                # them that way: get_setup_progress's "roster" workflow and
                # the Setup roster summary both League-filter players, and
                # get_setup_overview_v2 League-narrows teams. Leaving the
                # Players card Program-wide contradicted its own screen.
                in_scope = {t.id for t in self.store.all_teams()
                            if t.program_id == program.id
                            and (league is None or t.league_id == league.id)}
                players = [p for p in players if p.team_id in in_scope]
        rows = [_serialize(p) for p in players]
        # The Player DTO deliberately carries no email (it reaches coach/roster
        # views). Only the MANAGE_SETUP-gated operator list opts in, so the edit
        # drawer (#268) can prefill the current address without ever exposing it
        # on a coach/public payload.
        if include_email:
            for player, row in zip(players, rows):
                row["email"] = self.setup.active_player_email(player.id)
        return rows

    @catch
    def update_player(self, player_id: str,
                      actor_id: Optional[str] = None, **fields) -> dict:
        """Audited in-place Player profile edit (#268).

        Accepts only the correctable fields (``name``, ``position``,
        ``jersey_number``, ``shoots``, ``email``); a caller passes just the ones
        it wants to change, and any absent field is left untouched by the
        service. Each value (including ``position``) is validated by the service
        with a field-level error before any write, so create and edit share one
        canonical validator per field (#268 review).
        """
        kwargs = {key: fields[key]
                  for key in ("name", "position", "jersey_number", "shoots",
                              "email")
                  if key in fields}
        return _serialize(self.setup.update_player(
            player_id, actor_id=actor_id, **kwargs))

    @catch
    def set_player_active(self, player_id: str, active: bool,
                          actor_id: Optional[str] = None,
                          reason: Optional[str] = None) -> dict:
        """Deactivate/reactivate a Player without deleting history (#270)."""
        return _serialize(self.setup.set_player_active(
            player_id, active, actor_id=actor_id, reason=reason))

    @catch
    def create_game(self, season_id: str, division_id: str, home_team_id: str,
                    away_team_id: str, ice_slot_id: str, target_goalies: int = 1,
                    target_skaters: int = 15, max_skaters: int = 18,
                    allow_division_override: bool = False,
                    actor_id: Optional[str] = None,
                    league_id: Optional[str] = None,
                    game_type: str = "regular") -> dict:
        # ``league_id`` is the v2 canonical scope (#233 Slice C2): when supplied
        # it is required-and-validated and division_id is optional; when omitted
        # (v1) division_id stays mandatory and the league is derived from it.
        # ``game_type`` (#283 Slice D): "regular" (standings, one LeagueSeason)
        # or "exhibition" (a cross-League-allowed friendly, never in standings).
        # #318 — ``placement_raced`` marks a pre-lock locator (slot rink or
        # policy-scope plan) invalidated by a concurrent commit; each retry is
        # a FRESH transaction (the setup method owns the boundary) that
        # re-plans against the new topology and reports the precise terminal
        # answer, mirroring move_game's _retry_on_move_race.
        for _attempt in range(3):
            try:
                return _serialize(self.setup.create_game(
                    season_id, division_id, home_team_id, away_team_id,
                    ice_slot_id, target_goalies, target_skaters, max_skaters,
                    allow_division_override, actor_id, league_id=league_id,
                    game_type=game_type))
            except ConcurrencyConflictError as exc:
                if ((exc.details or {}).get("reason") != "placement_raced"
                        or _attempt == 2):
                    raise

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
        The validator receives the store read-only so a Player preview can
        evaluate the post-import jersey state against existing active players;
        it never writes. Row-level problems are collected into the returned
        report rather than raised; ``@catch`` here only guards a malformed
        request itself (e.g. a non-string CSV value).
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
        return validate_import(sheets, store=self.store)

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
