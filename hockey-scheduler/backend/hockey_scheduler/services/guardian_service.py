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

from datetime import datetime, timezone
from typing import Callable, List, Optional

from ..domain import GuardianLink, SetupAuditLog
from ..domain.errors import NotFoundError, ValidationError
from ..store import InMemoryStore


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


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

    def link_guardian(self, guardian_user_id: str, player_id: str,
                      verified: bool = False, actor_id: Optional[str] = None,
                      link_id: Optional[str] = None) -> GuardianLink:
        """Create (or return the existing) guardian↔junior link. New links are
        unverified by default — verification is a separate, deliberate step.
        ``link_id``/``verified`` are for deterministic demo seeding only."""
        if not guardian_user_id or not player_id:
            raise ValidationError("A guardian and a player are required.")
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

    def verify_link(self, link_id: str,
                    actor_id: Optional[str] = None) -> GuardianLink:
        link = self.store.get_guardian_link(link_id)
        if link is None:
            raise NotFoundError("Guardian link not found.")
        link.verified = True
        self.store.save_guardian_link(link)
        self._audit("guardian_link_verified", link.id, actor_id=actor_id,
                    detail={"guardian_user_id": link.guardian_user_id,
                            "player_id": link.player_id})
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
