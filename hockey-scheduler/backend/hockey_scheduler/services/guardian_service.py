"""Guardian ↔ junior-player authorization links (#26).

A guardian may respond (attendance, substitute-offer accept/decline) for a
junior only through a **verified** link. Creating a link is an operator/admin
action; verification is what grants authority — an unverified link is inert.
This service is the single source of truth the guardian-scoped routes consult
before letting a guardian act for a player, so the rule ("a linked, verified
guardian can respond for a junior; an unlinked user cannot") lives in one place
and is unit-testable.

No guardian contact/PII is modelled here — a link is two opaque ids plus a
verified flag — so nothing here can leak personal data into an operator view.
"""

import functools
from datetime import datetime, timezone
from typing import Callable, List, Optional

from ..domain import GuardianLink, Role, SetupAuditLog
from ..domain.errors import NotFoundError, ValidationError
from ..store import InMemoryStore
from .epoch_fence import EPOCH_FENCE_GLOBAL_KEY


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _transactional(fn):
    """Wrap a mutating service method in a single store transaction.

    PR #423 review prerequisite (design §8.7/§11.6): before this, neither
    ``link_guardian`` nor ``verify_link`` had ANY transaction wrapper — the
    write (``add_guardian_link``/``save_guardian_link``) and its audit row
    (``_audit``) were two separate, non-atomic store operations, so an audit
    failure could already leave an unaudited guardian link, independent of
    this redesign. It is also the literal prerequisite for the epoch fence:
    "acquire the fence in the same transaction as the write" is not
    satisfiable until these two methods run inside one. Same shape as
    ``account_service.py``'s own ``_transactional`` (duplicated, not
    imported/shared, matching this codebase's existing per-service-module
    convention — see e.g. ``setup_service.py``'s own separate copy)."""
    @functools.wraps(fn)
    def wrapper(self, *args, **kwargs):
        with self.store.transaction():
            return fn(self, *args, **kwargs)
    return wrapper


class GuardianService:
    def __init__(self, store: InMemoryStore, clock: Callable[[], datetime] = _utcnow):
        self.store = store
        self.clock = clock

    def _audit(self, action: str, link_id: str, actor_id: Optional[str] = None,
               detail: Optional[dict] = None) -> SetupAuditLog:
        return self.store.add_setup_audit(SetupAuditLog(
            id=self.store.next_id("setupaudit"), action=action,
            entity_type="guardian_link", entity_id=link_id, at=self.clock(),
            actor_id=actor_id, detail=detail or {}))

    @_transactional
    def link_guardian(self, guardian_user_id: str, player_id: str,
                      verified: bool = False, actor_id: Optional[str] = None,
                      link_id: Optional[str] = None) -> GuardianLink:
        """Create (or return the existing) guardian↔junior link. New links are
        unverified by default — verification is a separate, deliberate step.
        ``link_id``/``verified`` are for deterministic demo seeding only.

        ``guardian_user_id`` must reference an existing account with the
        GUARDIAN role (#35) — before this, nothing stopped an arbitrary
        opaque string from being recorded as a "guardian," which was safe
        only because this method had no HTTP-reachable caller yet.

        PR #423: acquires the epoch fence's GLOBAL exclusive hold first,
        inside this method's own transaction (row 16 of the design's writer
        table) — a verified guardian link is part of ``context_scope``'s
        authorization surface (it can change what a Guardian's own scoped
        reads are allowed to resolve), so it is epoch-material exactly like
        an Official assignment or a Player/Team reassignment, and the
        affected user (the guardian) can only be found by the lookup this
        method itself performs, which is why it takes the GLOBAL key rather
        than a per-user one — see the design's §4.2 classification rule."""
        self.store.epoch_fence_acquire_exclusive(EPOCH_FENCE_GLOBAL_KEY)
        if not guardian_user_id or not player_id:
            raise ValidationError("A guardian and a player are required.")
        account = self.store.get_user_account(guardian_user_id)
        if account is None or account.role != Role.GUARDIAN:
            raise ValidationError(
                "guardian_user_id must be an existing account with the "
                "guardian role.")
        if self.store.get_player(player_id) is None:
            raise NotFoundError("Player not found.")
        existing = self.store.guardian_link_for(guardian_user_id, player_id)
        if existing is not None:
            return existing
        link = GuardianLink(
            id=link_id or self.store.next_id("guardian"),
            guardian_user_id=guardian_user_id, player_id=player_id,
            created_at=self.clock(), verified=bool(verified))
        self.store.add_guardian_link(link)
        self._audit("guardian_link_created", link.id, actor_id=actor_id,
                    detail={"guardian_user_id": guardian_user_id,
                            "player_id": player_id, "verified": bool(verified)})
        return link

    @_transactional
    def verify_link(self, link_id: str, actor_id: Optional[str] = None,
                    consent_method: Optional[str] = None) -> GuardianLink:
        """Grant authority. ``consent_method`` is the GDPR Art. 8 consent
        record (#35) — how the operator obtained/confirmed authorization
        (e.g. "signed_form", "verbal_confirmed", "email_reply"). Optional
        here (internal/seed callers may flip ``verified`` without one), but
        the operator-facing HTTP route requires it on every real
        verification — see ``ApiService.verify_guardian_link``.

        PR #423: acquires the epoch fence's GLOBAL exclusive hold first
        (row 17 of the design's writer table) — see ``link_guardian``'s
        docstring for why this is epoch-material and why the GLOBAL key."""
        self.store.epoch_fence_acquire_exclusive(EPOCH_FENCE_GLOBAL_KEY)
        link = self.store.get_guardian_link(link_id)
        if link is None:
            raise NotFoundError("Guardian link not found.")
        link.verified = True
        detail = {"guardian_user_id": link.guardian_user_id,
                  "player_id": link.player_id}
        if consent_method:
            link.consent_method = consent_method
            link.consented_at = self.clock()
            detail["consent_method"] = consent_method
        self.store.save_guardian_link(link)
        self._audit("guardian_link_verified", link.id, actor_id=actor_id,
                    detail=detail)
        return link

    # -- read helpers the routes gate on -----------------------------------
    def verified_junior_ids(self, guardian_user_id: str) -> List[str]:
        """The player ids this guardian is verified to act for."""
        return [g.player_id for g in self.store.guardian_links_for(guardian_user_id)
                if g.verified]

    def is_verified_guardian(self, guardian_user_id: str, player_id: str) -> bool:
        """True iff a verified link binds this guardian to this player — the
        authority check every guardian action must pass."""
        link = self.store.guardian_link_for(guardian_user_id, player_id)
        return link is not None and link.verified

    def all_links(self) -> List[GuardianLink]:
        """Every guardian link, for the operator-facing admin list (#35)."""
        return self.store.all_guardian_links()
