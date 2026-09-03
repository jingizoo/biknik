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
    SetupAuditLog,
    SubtreeDeletionChallenge,
    UserAccount,
)
from ..domain.errors import NotAuthorizedError, NotFoundError, ValidationError
from ..domain.roles import Permission, can
from ..store.sql_store import SPECS
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


def _preview_payload(preview: SubtreePreview) -> dict:
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
        "fingerprint": preview.fingerprint,
        "delete_count": preview.delete_count,
        "delete_groups": groups(preview.delete_groups),
        "retained_groups": groups(preview.retained_groups),
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
                 challenge_ttl_seconds: int = DEFAULT_CHALLENGE_TTL_SECONDS,
                 stage_hook: Optional[Callable[[str], None]] = None):
        self.store = store
        self.clock = clock
        self._challenge_ttl = challenge_ttl_seconds
        # Fixture-only failure/concurrency seam.  ApiService never exposes it.
        self._stage_hook = stage_hook or (lambda _stage: None)

    def _require_admin(self, actor_id: Optional[str]):
        account = self.store.get_user_account(actor_id) if actor_id else None
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

    def _project(self, actor_id: str, root_type, root_id: str) -> _Projection:
        kind = self._root_type(root_type)
        if not isinstance(root_id, str) or not root_id.strip() \
                or root_id != root_id.strip():
            raise ValidationError(
                "A root id is required.", {"reason": "root_id_required"})

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
        return _Projection(preview, {key: rows[key] for key in relevant_keys},
                           relevant_edges)

    def preview(self, actor_id: str, root_type, root_id: str) -> dict:
        self._require_admin(actor_id)
        with self.store.transaction(isolation="REPEATABLE READ"):
            projection = self._project(actor_id, root_type, root_id)
            token = secrets.token_urlsafe(24)
            now = self.clock()
            expires_at = now + timedelta(seconds=self._challenge_ttl)
            self.store.set_subtree_deletion_challenge(
                SubtreeDeletionChallenge(
                    id=actor_id, token_hash=_hash_token(token),
                    actor_id=actor_id,
                    fingerprint=projection.preview.fingerprint,
                    root_type=projection.preview.root_type.value,
                    root_id=projection.preview.root_id,
                    confirmation_name=projection.preview.confirmation_name,
                    expires_at=expires_at, created_at=now))
        payload = _preview_payload(projection.preview)
        payload.update({"challenge_token": token,
                        "expires_at": expires_at.isoformat()})
        return payload

    def _consume_challenge(self, actor_id, token):
        supplied_hash = _hash_token(token or "")
        challenge = self.store.consume_subtree_deletion_challenge(
            actor_id, supplied_hash)
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
                    if scope.get(scope_key) == edge.target_id:
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
                # The facility is gone: the fixture survives but is no longer a
                # published placement.  Preserve its teams/history, clear the
                # stale rink display, and return it to a schedulable draft.
                row.published = False
                row.is_draft = True
                row.rink = ""
            self.store.subtree_save_row(row)
            self._stage_hook("after_detach")

    def _release_retained_slots(self, projection: _Projection):
        deleted_keys = {
            (group.entity_type.value, record_id)
            for group in projection.preview.delete_groups
            for record_id in group.record_ids
        }
        slot_ids = {
            edge.target_id for edge in projection.preview.removed_edges
            if edge.inventory_key == "games.ice_slot_id"
            and (EntityType.ICE_SLOT.value, edge.target_id) not in deleted_keys
        }
        for slot_id in sorted(slot_ids):
            slot_key = (EntityType.ICE_SLOT.value, slot_id)
            slot = copy.copy(projection.rows[slot_key])
            if not isinstance(slot, IceSlot):
                raise ValidationError(
                    "The projected ice relationship is invalid.",
                    {"reason": "graph_projection_invalid"})
            remaining = [row for key, row in projection.rows.items()
                         if key[0] == EntityType.GAME.value
                         and key not in deleted_keys
                         and not row.cancelled
                         and row.ice_slot_id == slot_id]
            if not remaining and slot.status is IceSlotStatus.ALLOCATED:
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
        challenge = self._consume_challenge(actor_id, challenge_token)
        if typed_name != challenge.confirmation_name:
            raise ValidationError(
                "Typed confirmation name did not match.",
                {"reason": "confirmation_mismatch"})

        with self.store.transaction(isolation="SERIALIZABLE"):
            self.store.lock_subtree_graph()
            # The request/session gate ran before challenge consumption, but
            # account state can change while this rare operation waits for the
            # installation-wide graph lock.  Re-read it under that lock before
            # trusting the actor or touching the projected graph.
            self._require_admin(actor_id)
            self._stage_hook("after_lock")
            projection = self._project(
                actor_id, challenge.root_type, challenge.root_id)
            if not hmac.compare_digest(
                    projection.preview.fingerprint, challenge.fingerprint):
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
            audit_id = self.store.next_id("setupaudit")
            self.store.add_setup_audit(SetupAuditLog(
                id=audit_id,
                action="subtree_deleted",
                entity_type=projection.preview.root_type.value,
                entity_id=projection.preview.root_id,
                at=self.clock(), actor_id=actor_id,
                detail={
                    "reason": reason,
                    "preview_fingerprint": projection.preview.fingerprint,
                    "deleted_counts": deleted_counts,
                    "detached_counts": detached_counts,
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
            "audit_id": audit_id,
        }
