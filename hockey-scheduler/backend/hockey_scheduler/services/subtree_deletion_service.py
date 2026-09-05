"""Authenticated, preview-bound destructive subtree execution (#429).

The pure ownership inventory lives in :mod:`subtree_preview`.  This module is
the I/O boundary which projects real store rows into that contract, persists a
short-lived single-use challenge, and executes the exact reviewed plan under
one transaction and one deterministic graph lock order.
"""

from __future__ import annotations

import copy
import hashlib
import hmac
import json
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Callable, Iterable, Optional

from ..domain import (
    ActiveContext,
    Game,
    IceSlot,
    IceSlotStatus,
    Role,
    Season,
    SetupAuditLog,
    SubtreeDeletionChallenge,
    UserAccount,
)
from ..domain.errors import NotAuthorizedError, NotFoundError, ValidationError
from ..domain.roles import Permission, can
from ..store.sql_store import SPECS
from . import season_guard
from .epoch_fence import EPOCH_FENCE_GLOBAL_KEY
from .subtree_preview import (
    EntityType,
    ProjectedEdge,
    REFERENCE_BY_KEY,
    REFERENCE_INVENTORY,
    RecordRef,
    SubtreePreview,
    TargetRemoval,
    build_subtree_preview,
)

DEFAULT_CHALLENGE_TTL_SECONDS = 5 * 60
EXECUTION_CONTRACT_VERSION = 2


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _hash_token(token: str) -> str:
    return hashlib.sha256((token or "").encode("utf-8")).hexdigest()


_ENTITY_BY_MODEL = {
    model: EntityType(spec.table) for model, spec in SPECS.items()
}
_MODEL_BY_ENTITY = {kind: model for model, kind in _ENTITY_BY_MODEL.items()}


# Explicit product surface.  These are named setup records with meaningful
# operator-facing confirmation text.  Relationship/history rows never become
# deletion roots merely because they happen to have an id.
ALLOWED_ROOT_TYPES = frozenset({
    EntityType.PROGRAM,
    EntityType.SEASON,
    EntityType.LEAGUE,
    EntityType.DIVISION,
    EntityType.CLUB,
    EntityType.TEAM,
    EntityType.PLAYER,
    EntityType.ORGANIZATION,
    EntityType.VENUE,
    EntityType.RINK,
    EntityType.OFFICIAL,
})

def _snake(name: str) -> str:
    out = []
    for index, char in enumerate(name):
        if char.isupper() and index:
            out.append("_")
        out.append(char.lower())
    return "".join(out)


# The ordinary setup-target gate speaks canonical singular model names.  Derive
# those names from the same model map which defines EntityType and assert the
# mapping is total so a new destructive root can never arrive ungated.
ROOT_TARGET_KIND = {
    kind: _snake(_MODEL_BY_ENTITY[kind].__name__)
    for kind in ALLOWED_ROOT_TYPES
}


# A subtree root owns its exclusively deleted descendants, but it does not
# confer authority to mutate a surviving record merely because that record
# points into the subtree.  Derive the complete source-type axis from the
# inventory's DETACH rules, then classify how each source reaches the ordinary
# setup-target gate.  ``None`` is deliberate only for principal/context rows:
# their mutation is already guarded by the service's exact League Admin +
# MANAGE_USERS requirement and is disclosed as a retained effect.
#
# The totality check is executable production validation, not a test-only pin:
# adding a new DETACH source cannot silently create an ungated survivor write.
DETACH_SOURCE_AUTHORIZATION = {
    EntityType.PROGRAM: ("program", "id"),
    EntityType.SEASON_TEAM_REGISTRATION: ("registration", "id"),
    EntityType.TEAM: ("team", "id"),
    EntityType.VENUE: ("venue", "id"),
    EntityType.GAME: ("game", "id"),
    EntityType.OFFICIAL: ("official", "id"),
    EntityType.RESCHEDULE_REQUEST: ("game", "game_id"),
    EntityType.USER_ACCOUNT: None,
    EntityType.ACTIVE_CONTEXT: None,
}
_DETACH_SOURCE_TYPES = frozenset(
    relation.source for relation in REFERENCE_INVENTORY
    if relation.on_target_delete is TargetRemoval.DETACH)
if set(DETACH_SOURCE_AUTHORIZATION) != set(_DETACH_SOURCE_TYPES):
    missing = sorted(
        kind.value for kind in
        _DETACH_SOURCE_TYPES - set(DETACH_SOURCE_AUTHORIZATION))
    stale = sorted(
        kind.value for kind in
        set(DETACH_SOURCE_AUTHORIZATION) - _DETACH_SOURCE_TYPES)
    raise RuntimeError(
        "subtree DETACH authorization inventory drift: "
        f"missing={missing!r}, stale={stale!r}")

DESCENDANT_VENUE_PROGRAM_RULE = "owned_descendant_programs"
DELETED_GAME_RESERVATION_RULE = "deleted_game_reservation_programs"
SHARED_OFFICIAL_PROGRAM_RULE = "shared_official_programs"


class RetainedChangeEffect(str, Enum):
    DRAFT_GAME_UNPLACED = "draft_game_unplaced"
    USER_ACCOUNT_DEACTIVATED = "user_account_deactivated"
    ICE_SLOT_RELEASED = "ice_slot_released"


class RetainedEffectAuthority(str, Enum):
    DETACH_SOURCE = "detach_source"
    MANAGE_USERS_SPECIAL = "manage_users_special"
    DELETED_GAME_RESERVATION = "deleted_game_reservation"


# Keep the effect axis total and record why each surviving-row mutation is
# authorized.  Releasing a slot is the inverse of removing an authorized
# deleted Game's reservation, not authority over the facility itself; below we
# derive and Program-authorize every deleted Game which owned that reservation.
# The sibling-Game projection separately prevents another live reservation
# from being released.  A future effect must choose an explicit disposition
# before the module can load.
RETAINED_EFFECT_AUTHORIZATION = {
    RetainedChangeEffect.DRAFT_GAME_UNPLACED:
        (EntityType.GAME, RetainedEffectAuthority.DETACH_SOURCE),
    RetainedChangeEffect.USER_ACCOUNT_DEACTIVATED:
        (EntityType.USER_ACCOUNT,
         RetainedEffectAuthority.MANAGE_USERS_SPECIAL),
    RetainedChangeEffect.ICE_SLOT_RELEASED:
        (EntityType.ICE_SLOT,
         RetainedEffectAuthority.DELETED_GAME_RESERVATION),
}
if set(RETAINED_EFFECT_AUTHORIZATION) != set(RetainedChangeEffect):
    raise RuntimeError("subtree retained-effect authorization drift")


_ENTITY_ALIASES: dict[str, EntityType] = {}
for _model, _kind in _ENTITY_BY_MODEL.items():
    _ENTITY_ALIASES[_kind.value] = _kind
    _ENTITY_ALIASES[_kind.value.removesuffix("s")] = _kind
    _ENTITY_ALIASES[_snake(_model.__name__)] = _kind

# Durable audit labels and typed actor references use these stable singulars.
_ENTITY_ALIASES.update({
    "program": EntityType.PROGRAM,
    "season": EntityType.SEASON,
    "league": EntityType.LEAGUE,
    "league_season": EntityType.LEAGUE_SEASON,
    "division": EntityType.DIVISION,
    "team": EntityType.TEAM,
    "player": EntityType.PLAYER,
    "official": EntityType.OFFICIAL,
    "organization": EntityType.ORGANIZATION,
    "venue": EntityType.VENUE,
    "rink": EntityType.RINK,
    "ice_slot": EntityType.ICE_SLOT,
    "user": EntityType.USER_ACCOUNT,
    "user_account": EntityType.USER_ACCOUNT,
})


@dataclass(frozen=True)
class _Projection:
    preview: SubtreePreview
    rows: dict[tuple[str, str], object]
    edges: tuple[ProjectedEdge, ...]
    retained_changes: tuple["_RetainedChangeGroup", ...]
    fingerprint: str


@dataclass(frozen=True, order=True)
class _RetainedChangeGroup:
    effect: RetainedChangeEffect
    entity_type: EntityType
    record_ids: tuple[str, ...]

    @property
    def count(self) -> int:
        return len(self.record_ids)


def _state_fingerprint(row) -> str:
    """Digest the exact portable column representation SqlStore persists."""
    spec = SPECS[type(row)]
    values = []
    for col in spec.cols:
        value = col.to_db(getattr(row, col.name))
        # Defensive normalization for a future converter returning an Enum or
        # datetime instead of the strings current converters produce.
        if isinstance(value, Enum):
            value = value.value
        elif isinstance(value, datetime):
            value = value.isoformat()
        values.append([col.name, value])
    payload = json.dumps(values, sort_keys=False, separators=(",", ":"),
                         ensure_ascii=False, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _record_ref(row) -> RecordRef:
    kind = _ENTITY_BY_MODEL.get(type(row))
    record_id = getattr(row, "id", None)
    if kind is None or not isinstance(record_id, str) or not record_id:
        raise ValidationError(
            "The persisted graph contains an unsupported row.",
            {"reason": "graph_projection_invalid"})
    return RecordRef(kind, record_id, _state_fingerprint(row))


def _one_existing_target(
    targets: Iterable[EntityType], value, records
) -> tuple[tuple[EntityType, str], ...]:
    if value is None or value == "":
        return ()
    value = str(value)
    matches = tuple((kind, value) for kind in targets
                    if (kind.value, value) in records)
    if len(matches) > 1:
        raise ValidationError(
            "A polymorphic relationship is ambiguous.",
            {"reason": "graph_projection_ambiguous"})
    return matches


def _prefixed_target(value, targets, records):
    if value is None or value == "":
        return ()
    raw = str(value)
    if ":" in raw:
        prefix, record_id = raw.split(":", 1)
        kind = _ENTITY_ALIASES.get(prefix)
        if kind in targets and (kind.value, record_id) in records:
            return ((kind, record_id),)
        return ()
    return _one_existing_target(targets, raw, records)


def _relationship_targets(relation, row, records):
    """Resolve one catalogued live relationship without guessing its type."""
    value = getattr(row, relation.field)
    discriminator = relation.discriminator

    if discriminator == "scope_type":
        raw = getattr(getattr(row, "scope_type", None), "value",
                      getattr(row, "scope_type", None))
        kind = _ENTITY_ALIASES.get(str(raw))
        if kind in relation.targets and value not in (None, "") \
                and (kind.value, str(value)) in records:
            return ((kind, str(value)),)
        return ()

    if discriminator == "entity_type":
        kind = _ENTITY_ALIASES.get(str(getattr(row, "entity_type", "")))
        if kind in relation.targets and value not in (None, "") \
                and (kind.value, str(value)) in records:
            return ((kind, str(value)),)
        return ()

    if discriminator == "category":
        subject_type = str(getattr(row, "subject_type", ""))
        kind = _ENTITY_ALIASES.get(subject_type)
        if kind in relation.targets and value not in (None, "") \
                and (kind.value, str(value).split(":", 1)[-1]) in records:
            return ((kind, str(value).split(":", 1)[-1]),)
        # Contact-destination audit subjects carry a typed recipient_ref.
        return _prefixed_target(value, relation.targets, records)

    if discriminator == "audience":
        raw = getattr(getattr(row, "audience", None), "value",
                      getattr(row, "audience", None))
        kind = {"coach": EntityType.TEAM,
                "official": EntityType.OFFICIAL,
                "player": EntityType.PLAYER}.get(raw)
        if kind in relation.targets and value not in (None, "") \
                and (kind.value, str(value)) in records:
            return ((kind, str(value)),)
        return ()

    if discriminator in ("actor_key prefix", "recipient_ref prefix"):
        return _prefixed_target(value, relation.targets, records)

    if discriminator == "role and scope key":
        if not isinstance(value, dict):
            raise ValidationError(
                "An account scope is not a JSON object.",
                {"reason": "graph_projection_invalid"})
        found = []
        for key, kind in (("team_id", EntityType.TEAM),
                          ("player_id", EntityType.PLAYER),
                          ("official_id", EntityType.OFFICIAL)):
            record_id = value.get(key)
            if record_id and (kind.value, str(record_id)) in records:
                found.append((kind, str(record_id)))
        return tuple(found)

    if discriminator == "actor_type":
        kind = _ENTITY_ALIASES.get(str(getattr(row, "actor_type", "")))
        if kind in relation.targets and value not in (None, "") \
                and (kind.value, str(value)) in records:
            return ((kind, str(value)),)
        return ()

    return _one_existing_target(relation.targets, value, records)


def _execution_fingerprint(
    graph_fingerprint: str,
    retained_changes: tuple[_RetainedChangeGroup, ...],
) -> str:
    """Bind a challenge to both the graph and disclosed survivor effects."""
    payload = {
        "contract": EXECUTION_CONTRACT_VERSION,
        "graph_fingerprint": graph_fingerprint,
        "retained_changes": [
            [group.effect.value, group.entity_type.value, *group.record_ids]
            for group in retained_changes
        ],
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"),
                         ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _preview_payload(projection: _Projection) -> dict:
    preview = projection.preview

    def groups(values):
        return [{"entity_type": group.entity_type.value,
                 "count": group.count,
                 "record_ids": list(group.record_ids)} for group in values]

    def edge_groups(values):
        return [{
            "inventory_key": group.inventory_key,
            "count": group.count,
            "edges": [{
                "source_type": edge.source_type.value,
                "source_id": edge.source_id,
                "target_type": edge.target_type.value,
                "target_id": edge.target_id,
            } for edge in group.edges],
        } for group in values]

    return {
        "root": {"entity_type": preview.root_type.value,
                 "record_id": preview.root_id,
                 "confirmation_name": preview.confirmation_name},
        "fingerprint": projection.fingerprint,
        "delete_count": preview.delete_count,
        "delete_groups": groups(preview.delete_groups),
        "retained_groups": groups(preview.retained_groups),
        "retained_change_groups": [{
            "effect": group.effect.value,
            "entity_type": group.entity_type.value,
            "count": group.count,
            "record_ids": list(group.record_ids),
        } for group in projection.retained_changes],
        "removed_relationship_groups": edge_groups(
            preview.removed_relationship_groups),
        "detached_relationship_groups": edge_groups(
            preview.detached_relationship_groups),
        "retained_relationship_groups": edge_groups(
            preview.retained_relationship_groups),
    }


class SubtreeDeletionService:
    """High-privilege projector and all-or-nothing execution boundary."""

    def __init__(self, store, clock: Callable[[], datetime] = _utcnow,
                 *, root_authorizer: Callable[[str, str, UserAccount, str],
                                              bool],
                 boundary_authorizer: Callable[
                     [tuple[tuple[str, str, str], ...], UserAccount, str],
                     bool],
                 challenge_ttl_seconds: int = DEFAULT_CHALLENGE_TTL_SECONDS,
                 stage_hook: Optional[Callable[[str], None]] = None):
        self.store = store
        self.clock = clock
        if root_authorizer is None:
            raise ValueError("subtree deletion requires a root authorizer")
        if boundary_authorizer is None:
            raise ValueError("subtree deletion requires a boundary authorizer")
        self._root_authorizer = root_authorizer
        self._boundary_authorizer = boundary_authorizer
        self._challenge_ttl = challenge_ttl_seconds
        # Fixture-only failure/concurrency seam.  ApiService never exposes it.
        self._stage_hook = stage_hook or (lambda _stage: None)

    def _require_admin(self, actor_id: Optional[str], *, lock=False):
        getter = (self.store.get_user_account_for_update
                  if lock else self.store.get_user_account)
        account = getter(actor_id) if actor_id else None
        if account is None or not account.active:
            raise NotAuthorizedError(
                "Not authorized.", {"reason": "not_authorized"})
        if account.role is not Role.LEAGUE_ADMIN or not (
                can(account.role, Permission.MANAGE_SETUP)
                and can(account.role, Permission.MANAGE_USERS)):
            raise NotAuthorizedError(
                "Delete subtree requires a League Admin with manage_setup "
                "and manage_users.",
                {"reason": "insufficient_permission"})
        return account

    def _authorize_root(self, account: UserAccount, kind: EntityType,
                        root_id: str, phase: str) -> None:
        """Apply #409 context and #369 target authority before disclosure."""
        allowed = self._root_authorizer(
            ROOT_TARGET_KIND[kind], root_id, account, phase)
        if allowed is not True:
            # One response for absent and inaccessible roots.  In particular,
            # never reveal the stored confirmation name or descendant ids.
            raise NotFoundError(
                f"{_MODEL_BY_ENTITY[kind].__name__} {root_id} not found.",
                {"reason": "root_not_found"})

    @staticmethod
    def _boundary_authorization_checks(rows, edges, deleting,
                                       retained_changes):
        """Canonical checks for cross-boundary survivor writes and Venues.

        Deleted descendants inherit root authority, which is what permits one
        Program deletion to span all of its Seasons.  Two boundaries do not:

        * a surviving DETACH source will be mutated, so authorize that source;
        * an owned Venue below an Organization root may itself bridge to one or
          more Programs, so authorize its complete Program-link set with the
          dedicated Program-axis rule (not the ordinary Season-sensitive Venue
          gate).
        """
        checks = set()
        for edge in edges:
            relation = REFERENCE_BY_KEY[edge.inventory_key]
            if (relation.on_target_delete is not TargetRemoval.DETACH
                    or edge.target.key not in deleting
                    or edge.source.key in deleting):
                continue
            binding = DETACH_SOURCE_AUTHORIZATION[edge.source.entity_type]
            if binding is None:
                continue
            kind, id_field = binding
            source_row = rows.get(edge.source.key)
            record_id = (getattr(source_row, id_field, None)
                         if source_row is not None else None)
            if not isinstance(record_id, str) or not record_id:
                raise ValidationError(
                    "The persisted graph contains an invalid authorization "
                    "target.", {"reason": "graph_projection_invalid"})
            checks.add((kind, record_id, "scope"))

        for key in deleting:
            if key[0] == EntityType.VENUE.value:
                checks.add((
                    "venue", key[1], DESCENDANT_VENUE_PROGRAM_RULE))
            elif key[0] == EntityType.RINK.value:
                rink = rows.get(key)
                venue_id = getattr(rink, "venue_id", None)
                if not isinstance(venue_id, str) or not venue_id:
                    raise ValidationError(
                        "The persisted graph contains an invalid Rink owner.",
                        {"reason": "graph_projection_invalid"})
                checks.add((
                    "venue", venue_id, DESCENDANT_VENUE_PROGRAM_RULE))
            elif key[0] == EntityType.OFFICIAL.value:
                checks.add((
                    "official", key[1], SHARED_OFFICIAL_PROGRAM_RULE))

        for group in retained_changes:
            policy = RETAINED_EFFECT_AUTHORIZATION.get(group.effect)
            if policy is None or group.entity_type is not policy[0]:
                raise ValidationError(
                    "The projected retained effect is invalid.",
                    {"reason": "graph_projection_invalid"})
            if policy[1] is RetainedEffectAuthority.DELETED_GAME_RESERVATION:
                for slot_id in group.record_ids:
                    game_ids = {
                        edge.source.record_id for edge in edges
                        if (edge.inventory_key == "games.ice_slot_id"
                            and edge.target.record_id == slot_id
                            and edge.source.key in deleting)
                    }
                    if not game_ids:
                        raise ValidationError(
                            "The projected retained effect has no authorized "
                            "reservation owner.",
                            {"reason": "graph_projection_invalid"})
                    checks.update(
                        ("game", game_id, DELETED_GAME_RESERVATION_RULE)
                        for game_id in game_ids)
        return tuple(sorted(checks))

    def _authorize_retained_detaches(
        self, account: UserAccount, checks, phase: str,
        root_kind: EntityType, root_id: str,
    ) -> None:
        if not checks:
            return
        if self._boundary_authorizer(checks, account, phase) is not True:
            # Collapse a foreign survivor and an absent root into the same
            # response.  This runs before archived/game diagnostics, so those
            # details cannot become a cross-context oracle either.
            raise NotFoundError(
                f"{_MODEL_BY_ENTITY[root_kind].__name__} {root_id} not found.",
                {"reason": "root_not_found"})

    @staticmethod
    def _root_type(value) -> EntityType:
        try:
            kind = value if isinstance(value, EntityType) else EntityType(value)
        except (TypeError, ValueError):
            raise ValidationError(
                "Unsupported subtree root type.",
                {"reason": "unsupported_root_type"})
        if kind not in ALLOWED_ROOT_TYPES:
            raise ValidationError(
                "Unsupported subtree root type.",
                {"reason": "unsupported_root_type"})
        return kind

    @staticmethod
    def _root_id(value) -> str:
        if not isinstance(value, str) or not value.strip() \
                or value != value.strip():
            raise ValidationError(
                "A root id is required.", {"reason": "root_id_required"})
        return value

    @staticmethod
    def _deletion_closure(root_key, edges):
        deleting = {root_key}
        changed = True
        while changed:
            changed = False
            for edge in edges:
                relation = REFERENCE_BY_KEY[edge.inventory_key]
                if (relation.on_target_delete is TargetRemoval.DELETE_SOURCE
                        and edge.target.key in deleting
                        and edge.source.key not in deleting):
                    deleting.add(edge.source.key)
                    changed = True
        return deleting

    @staticmethod
    def _game_is_clean_draft(game, edges) -> bool:
        """Whether facility removal may unplace this retained Game.

        Published, cancelled, legacy-attached, and result-bearing fixtures are
        history.  They must go through #428 cancellation, which snapshots the
        facility facts and releases the live ice edge explicitly.  #429 may
        only clear placement from a generated draft which has never committed.

        The operational-state axis is derived from the relationship inventory,
        not copied from ``SetupService.delete_game``.  Every inbound
        ``DELETE_SOURCE`` edge is state owned by the Game (today: the canonical
        result/roster/assignment/availability/substitute/reschedule groups plus
        notification and audit evidence).  One such edge means this is no
        longer a pristine proposal, and a future Game-owned record joins the
        refusal automatically when it is added to the inventory.
        """
        game_key = (EntityType.GAME.value, game.id)
        has_owned_state = any(
            edge.target.key == game_key
            and REFERENCE_BY_KEY[edge.inventory_key].on_target_delete
            is TargetRemoval.DELETE_SOURCE
            for edge in edges)
        return (game.is_draft is True
                and game.published is False
                and game.cancelled is False
                and not has_owned_state)

    def _assert_retained_game_detaches_are_safe(self, rows, edges,
                                                deleting) -> None:
        blocked = []
        for edge in edges:
            if (edge.inventory_key != "games.ice_slot_id"
                    or edge.target.key not in deleting
                    or edge.source.key in deleting):
                continue
            game = rows.get(edge.source.key)
            if not isinstance(game, Game) \
                    or not self._game_is_clean_draft(game, edges):
                blocked.append(edge.source.record_id)
        if blocked:
            raise ValidationError(
                "Cancel scheduled games before deleting this facility. "
                "Cancellation preserves their history and releases the ice; "
                "then build a new subtree preview.",
                {"reason": "game_cancellation_required",
                 "game_count": len(set(blocked))})

    def _assert_no_archived_season_impact(self, rows, edges,
                                          deleting) -> None:
        mutation_keys = set(deleting)
        delete_parents_by_source = {}
        for edge in edges:
            removal = REFERENCE_BY_KEY[
                edge.inventory_key].on_target_delete
            if removal is TargetRemoval.DELETE_SOURCE:
                # Deletion closure flows target -> source.  The archived-
                # Season question is the inverse: which possible owners can
                # reach a row we plan to mutate?  Index source -> target once
                # so that answer is linear in this projection rather than one
                # full closure scan per archived Season.
                delete_parents_by_source.setdefault(
                    edge.source.key, set()).add(edge.target.key)
            elif (removal is TargetRemoval.DETACH
                  and edge.target.key in deleting
                  and edge.source.key not in deleting):
                mutation_keys.add(edge.source.key)

        governing_keys = set(mutation_keys)
        pending = list(mutation_keys)
        while pending:
            source_key = pending.pop()
            for target_key in delete_parents_by_source.get(source_key, ()):
                if target_key in governing_keys:
                    continue
                governing_keys.add(target_key)
                pending.append(target_key)

        affected = [
            row.id for key, row in rows.items()
            if (key in governing_keys
                and isinstance(row, Season)
                and season_guard.season_is_read_only(row))
        ]
        if affected:
            raise ValidationError(
                "Archived Seasons are read-only. Reopen the affected Season "
                "before deleting this subtree.",
                {"reason": season_guard.SEASON_ARCHIVED,
                 "season_ids": sorted(set(affected))})

    @staticmethod
    def _planned_released_slot_ids(preview, rows) -> tuple[str, ...]:
        deleted_keys = {
            (group.entity_type.value, record_id)
            for group in preview.delete_groups
            for record_id in group.record_ids
        }
        candidate_ids = {
            edge.target_id for edge in preview.removed_edges
            if edge.inventory_key == "games.ice_slot_id"
            and (EntityType.ICE_SLOT.value, edge.target_id) not in deleted_keys
        }
        released = []
        for slot_id in sorted(candidate_ids):
            slot = rows.get((EntityType.ICE_SLOT.value, slot_id))
            if not isinstance(slot, IceSlot):
                raise ValidationError(
                    "The projected ice relationship is invalid.",
                    {"reason": "graph_projection_invalid"})
            remaining = [row for key, row in rows.items()
                         if key[0] == EntityType.GAME.value
                         and key not in deleted_keys
                         and not row.cancelled
                         and row.ice_slot_id == slot_id]
            if not remaining and slot.status is IceSlotStatus.ALLOCATED:
                released.append(slot_id)
        return tuple(released)

    def _retained_changes(self, preview, rows):
        values: dict[tuple[RetainedChangeEffect, EntityType], set[str]] = {}

        def add(effect, kind, record_id):
            if not isinstance(effect, RetainedChangeEffect):
                raise ValidationError(
                    "The projected retained effect is invalid.",
                    {"reason": "graph_projection_invalid"})
            values.setdefault((effect, kind), set()).add(record_id)

        for edge in preview.detached_edges:
            row = rows.get((edge.source_type.value, edge.source_id))
            if (edge.inventory_key == "games.ice_slot_id"
                    and isinstance(row, Game)):
                add(RetainedChangeEffect.DRAFT_GAME_UNPLACED,
                    EntityType.GAME, row.id)
            elif (edge.inventory_key == "user_accounts.scope"
                  and isinstance(row, UserAccount) and row.active):
                add(RetainedChangeEffect.USER_ACCOUNT_DEACTIVATED,
                    EntityType.USER_ACCOUNT, row.id)
        for slot_id in self._planned_released_slot_ids(preview, rows):
            add(RetainedChangeEffect.ICE_SLOT_RELEASED,
                EntityType.ICE_SLOT, slot_id)
        return tuple(
            _RetainedChangeGroup(effect, kind, tuple(sorted(record_ids)))
            for (effect, kind), record_ids in sorted(
                values.items(), key=lambda item: (item[0][0], item[0][1].value)))

    def _project(self, actor_id: str, root_type, root_id: str, *,
                 account: UserAccount, authorization_phase: str) -> _Projection:
        kind = self._root_type(root_type)
        root_id = self._root_id(root_id)

        all_rows = self.store.subtree_all_rows()
        rows: dict[tuple[str, str], object] = {}
        refs: dict[tuple[str, str], RecordRef] = {}
        for row in all_rows:
            ref = _record_ref(row)
            if ref.key in rows:
                raise ValidationError(
                    "The persisted graph contains duplicate records.",
                    {"reason": "graph_projection_invalid"})
            rows[ref.key] = row
            refs[ref.key] = ref

        root_key = (kind.value, root_id)
        root = refs.get(root_key)
        if root is None:
            # A missing and an inaccessible target deliberately share one
            # refusal; this route never becomes an existence oracle.
            raise NotFoundError(
                "Subtree root not found.", {"reason": "root_not_found"})
        root_row = rows[root_key]
        confirmation_name = getattr(root_row, "name", None)
        if not isinstance(confirmation_name, str) or not confirmation_name.strip():
            raise ValidationError(
                "This record has no safe typed-confirmation name.",
                {"reason": "root_confirmation_unavailable"})

        edges = []
        for relation in REFERENCE_INVENTORY:
            if relation.on_target_delete is TargetRemoval.NOT_GRAPH:
                continue
            for source_key, source_row in rows.items():
                if source_key[0] != relation.source.value:
                    continue
                for target_kind, target_id in _relationship_targets(
                        relation, source_row, rows):
                    target_key = (target_kind.value, target_id)
                    edges.append(ProjectedEdge(
                        relation.key, refs[source_key], refs[target_key]))

        # Find the deletion closure on the full graph first.  Then narrow the
        # fingerprint to edges which actually cross/touch that closure and to
        # their endpoints.  Unrelated writes do not stale this capability.
        deleting = self._deletion_closure(root_key, edges)
        relevant_edges = tuple(edge for edge in edges
                               if edge.source.key in deleting
                               or edge.target.key in deleting)

        # A retained IceSlot is a shared scheduling resource.  If a deleted
        # (possibly cancelled) Game points at it, every other Game occupying
        # that same slot is material to the release decision even though
        # neither that survivor nor the slot belongs to the deletion closure.
        # Pull those sibling edges and rows into both the preview and its
        # fingerprint so execution cannot release live ice based on a partial
        # projection.
        retained_slot_keys = {
            edge.target.key for edge in relevant_edges
            if edge.inventory_key == "games.ice_slot_id"
            and edge.source.key in deleting
            and edge.target.key not in deleting
        }
        if retained_slot_keys:
            by_key = {edge.key: edge for edge in relevant_edges}
            for edge in edges:
                if (edge.inventory_key == "games.ice_slot_id"
                        and edge.target.key in retained_slot_keys):
                    by_key[edge.key] = edge
            relevant_edges = tuple(
                by_key[key] for key in sorted(by_key))
        relevant_keys = set(deleting)
        for edge in relevant_edges:
            relevant_keys.add(edge.source.key)
            relevant_keys.add(edge.target.key)
        relevant_refs = tuple(refs[key] for key in sorted(relevant_keys))
        preview = build_subtree_preview(
            actor_id=actor_id, root=root,
            confirmation_name=confirmation_name,
            records=relevant_refs, edges=relevant_edges)
        relevant_rows = {key: rows[key] for key in relevant_keys}
        retained_changes = self._retained_changes(preview, relevant_rows)
        boundary_checks = self._boundary_authorization_checks(
            rows, edges, deleting, retained_changes)
        self._authorize_retained_detaches(
            account, boundary_checks, authorization_phase, kind, root_id)
        self._assert_no_archived_season_impact(rows, edges, deleting)
        self._assert_retained_game_detaches_are_safe(rows, edges, deleting)
        fingerprint = _execution_fingerprint(
            preview.fingerprint, retained_changes)
        return _Projection(preview, {key: rows[key] for key in relevant_keys},
                           relevant_edges, retained_changes, fingerprint)

    def preview(self, actor_id: str, root_type, root_id: str) -> dict:
        kind = self._root_type(root_type)
        root_id = self._root_id(root_id)
        with self.store.transaction(isolation="SERIALIZABLE"):
            account = self._require_admin(actor_id)
            self._authorize_root(account, kind, root_id, "context")
            self._authorize_root(account, kind, root_id, "target")
            projection = self._project(
                actor_id, kind, root_id, account=account,
                authorization_phase="preview")
            token = secrets.token_urlsafe(24)
            now = self.clock()
            expires_at = now + timedelta(seconds=self._challenge_ttl)
            self.store.set_subtree_deletion_challenge(
                SubtreeDeletionChallenge(
                    id=actor_id, token_hash=_hash_token(token),
                    actor_id=actor_id,
                    fingerprint=projection.fingerprint,
                    root_type=projection.preview.root_type.value,
                    root_id=projection.preview.root_id,
                    confirmation_name=projection.preview.confirmation_name,
                    expires_at=expires_at, created_at=now))
        payload = _preview_payload(projection)
        payload.update({"challenge_token": token,
                        "expires_at": expires_at.isoformat()})
        return payload

    def _validate_challenge(self, actor_id, token, challenge):
        supplied_hash = _hash_token(token or "")
        if challenge is None:
            raise ValidationError(
                "No active subtree preview. Request a new preview.",
                {"reason": "invalid_challenge"})
        valid = (challenge.expires_at >= self.clock()
                 and challenge.actor_id == actor_id
                 and hmac.compare_digest(challenge.token_hash, supplied_hash))
        if not valid:
            raise ValidationError(
                "The subtree preview challenge is invalid or expired.",
                {"reason": "invalid_challenge"})
        return challenge

    def _inspect_challenge(self, actor_id, token):
        challenge = self.store.get_subtree_deletion_challenge(actor_id)
        return self._validate_challenge(actor_id, token, challenge)

    def _consume_challenge(self, actor_id, token):
        supplied_hash = _hash_token(token or "")
        challenge = self.store.consume_subtree_deletion_challenge(
            actor_id, supplied_hash)
        return self._validate_challenge(actor_id, token, challenge)

    @staticmethod
    def _delete_order(projection: _Projection):
        delete_keys = {
            (group.entity_type.value, record_id)
            for group in projection.preview.delete_groups
            for record_id in group.record_ids
        }
        before = {key: set() for key in delete_keys}
        after = {key: set() for key in delete_keys}
        for edge in projection.edges:
            if (REFERENCE_BY_KEY[edge.inventory_key].on_target_delete
                    is TargetRemoval.DELETE_SOURCE
                    and edge.source.key in delete_keys
                    and edge.target.key in delete_keys):
                after[edge.source.key].add(edge.target.key)
                before[edge.target.key].add(edge.source.key)
        ready = sorted(key for key, deps in before.items() if not deps)
        ordered = []
        while ready:
            key = ready.pop(0)
            ordered.append(key)
            for target in sorted(after[key]):
                before[target].discard(key)
                if not before[target] and target not in ordered \
                        and target not in ready:
                    ready.append(target)
                    ready.sort()
        if len(ordered) != len(delete_keys):
            raise ValidationError(
                "The subtree contains a cyclic ownership relationship.",
                {"reason": "graph_projection_invalid"})
        return ordered

    def _apply_detaches(self, projection: _Projection):
        by_source = {}
        for edge in projection.preview.detached_edges:
            key = (edge.source_type.value, edge.source_id)
            by_source.setdefault(key, []).append(edge)
        for key in sorted(by_source):
            row = copy.copy(projection.rows[key])
            account_scope_changed = False
            context_changed = False
            for edge in by_source[key]:
                field = edge.inventory_key.split(".", 1)[1]
                if isinstance(row, UserAccount) and field == "scope":
                    scope = dict(row.scope)
                    scope_key = {
                        EntityType.TEAM: "team_id",
                        EntityType.PLAYER: "player_id",
                        EntityType.OFFICIAL: "official_id",
                    }[edge.target_type]
                    # Projection resolves JSON scope ids through ``str`` so a
                    # legacy numeric value can still name the canonical string
                    # id. Execution must use that same normalization; otherwise
                    # it would advertise a deactivation but leave the account
                    # active and still bound.
                    raw_scope_id = scope.get(scope_key)
                    if raw_scope_id is not None \
                            and str(raw_scope_id) == edge.target_id:
                        scope.pop(scope_key, None)
                        account_scope_changed = True
                    row.scope = scope
                else:
                    if getattr(row, field) != edge.target_id:
                        raise ValidationError(
                            "The graph changed during subtree execution.",
                            {"reason": "preview_stale"})
                    setattr(row, field, None)
                    context_changed = context_changed or isinstance(
                        row, ActiveContext)
            if isinstance(row, UserAccount) and account_scope_changed:
                # A subject-bound principal cannot stay active after losing its
                # binding.  Session resolution re-reads account.active, so this
                # invalidates existing sessions without copying them into the
                # deletion subtree.
                row.active = False
            if isinstance(row, ActiveContext) and context_changed:
                row.generation += 1
                row.updated_at = self.clock()
            if isinstance(row, Game) and any(
                    edge.inventory_key == "games.ice_slot_id"
                    for edge in by_source[key]):
                # Projection admits only a never-committed generated draft.
                # Do not change its lifecycle flags; #429 owns placement
                # detachment, while #428 alone owns fixture cancellation and
                # immutable cancellation history.
                row.rink = ""
            self.store.subtree_save_row(row)
            self._stage_hook("after_detach")

    def _release_retained_slots(self, projection: _Projection):
        planned = {
            record_id
            for group in projection.retained_changes
            if group.effect is RetainedChangeEffect.ICE_SLOT_RELEASED
            for record_id in group.record_ids
        }
        for slot_id in sorted(planned):
            slot_key = (EntityType.ICE_SLOT.value, slot_id)
            slot = copy.copy(projection.rows[slot_key])
            if not isinstance(slot, IceSlot):
                raise ValidationError(
                    "The projected ice relationship is invalid.",
                    {"reason": "graph_projection_invalid"})
            if slot.status is not IceSlotStatus.ALLOCATED:
                raise ValidationError(
                    "The data changed since the preview. Request a new preview.",
                    {"reason": "preview_stale"})
            slot.status = IceSlotStatus.AVAILABLE
            self.store.subtree_save_row(slot)
            self._stage_hook("after_slot_release")

    def execute(self, actor_id: str, challenge_token: str,
                typed_name: str, reason: str) -> dict:
        self._require_admin(actor_id)
        if not isinstance(typed_name, str):
            raise ValidationError(
                "Typed confirmation name is required.",
                {"reason": "confirmation_mismatch"})
        if not isinstance(reason, str) or not reason.strip():
            raise ValidationError(
                "A deletion reason is required.",
                {"reason": "reason_required"})
        reason = reason.strip()
        if len(reason) > 500:
            raise ValidationError(
                "Deletion reason is too long.",
                {"reason": "reason_too_long"})
        inspected = self._inspect_challenge(actor_id, challenge_token)
        if typed_name != inspected.confirmation_name:
            raise ValidationError(
                "Typed confirmation name did not match.",
                {"reason": "confirmation_mismatch"})

        with self.store.transaction(isolation="SERIALIZABLE"):
            # Join the same global cross-replica fence held SHARED by every
            # scoped read.  This is deliberately the transaction's first
            # store operation: it bumps the persisted epoch and, on
            # PostgreSQL, takes the global advisory lock before the canonical
            # ActiveContext -> graph -> target lock order begins.
            self.store.epoch_fence_acquire_exclusive(
                EPOCH_FENCE_GLOBAL_KEY)
            # ActiveContext is the repo's first mutation lock.  A graph
            # lock must never be acquired before it.
            account = self._require_admin(actor_id)
            kind = self._root_type(inspected.root_type)
            root_id = self._root_id(inspected.root_id)
            self._authorize_root(account, kind, root_id, "context")

            # PostgreSQL takes this as one all-table NOWAIT statement.  A
            # contended graph is retryable and, because the capability has
            # not yet been consumed, the operator can retry the same exact
            # preview rather than rebuilding it after a harmless conflict.
            self.store.lock_subtree_graph()
            self._stage_hook("after_lock")

            # Re-read live identity and authorize the locked target before
            # consuming the token.  A context switch, permission change,
            # missing root, or foreign root therefore reveals no graph and
            # does not destroy an otherwise valid preview.
            account = self._require_admin(actor_id, lock=True)
            self._authorize_root(account, kind, root_id, "target")
            challenge = self._consume_challenge(actor_id, challenge_token)
            if challenge != inspected:
                raise ValidationError(
                    "The subtree preview challenge changed. Request a new "
                    "preview.", {"reason": "invalid_challenge"})

            projection = self._project(
                actor_id, kind, root_id, account=account,
                authorization_phase="target")
            if not hmac.compare_digest(
                    projection.fingerprint, challenge.fingerprint):
                raise ValidationError(
                    "The data changed since the preview. Request a new preview.",
                    {"reason": "preview_stale"})
            self._stage_hook("after_revalidation")
            self._apply_detaches(projection)
            self._release_retained_slots(projection)
            for key in self._delete_order(projection):
                self.store.subtree_delete_row(projection.rows[key])
                self._stage_hook("after_delete")

            deleted_counts = {
                group.entity_type.value: group.count
                for group in projection.preview.delete_groups
            }
            detached_counts = {
                group.inventory_key: group.count
                for group in projection.preview.detached_relationship_groups
            }
            retained_change_counts = {}
            for group in projection.retained_changes:
                retained_change_counts[group.effect.value] = (
                    retained_change_counts.get(group.effect.value, 0)
                    + group.count)
            audit_id = self.store.next_id("setupaudit")
            self.store.add_setup_audit(SetupAuditLog(
                id=audit_id,
                action="subtree_deleted",
                entity_type=projection.preview.root_type.value,
                entity_id=projection.preview.root_id,
                at=self.clock(), actor_id=actor_id,
                detail={
                    "reason": reason,
                    "preview_fingerprint": projection.fingerprint,
                    "deleted_counts": deleted_counts,
                    "detached_counts": detached_counts,
                    "retained_change_counts": retained_change_counts,
                    "root_type": projection.preview.root_type.value,
                    "root_id": projection.preview.root_id,
                }))
            self._stage_hook("after_audit")

        return {
            "result": "success",
            "root": {"entity_type": projection.preview.root_type.value,
                     "record_id": projection.preview.root_id},
            "deleted_counts": deleted_counts,
            "detached_counts": detached_counts,
            "retained_change_counts": retained_change_counts,
            "audit_id": audit_id,
        }
