"""Real user accounts: creation, activation, and login verification (#67).

Operator-created accounts only — no self-service signup, password reset,
magic links, or 2FA in this slice. A ``UserAccount`` binds a hashed password
to a ``Role`` and an optional ``scope`` (the same shape a session carries:
``team_id`` for a coach, ``player_id`` for a player, ``official_id`` for an
official), so signing in as that account produces exactly the session the
role/scope model already enforces.
"""

from datetime import datetime, timezone
from typing import Callable, Optional

from ..domain import Role, SetupAuditLog, UserAccount
from ..domain.errors import NotFoundError, ValidationError
from ..store import InMemoryStore
from .passwords import hash_password, verify_password


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class AccountService:
    def __init__(self, store: InMemoryStore, clock: Callable[[], datetime] = _utcnow):
        self.store = store
        self.clock = clock

    def _audit(self, action: str, account_id: str,
              actor_id: Optional[str] = None, detail: Optional[dict] = None
              ) -> SetupAuditLog:
        return self.store.add_setup_audit(SetupAuditLog(
            id=self.store.next_id("setupaudit"), action=action,
            entity_type="user_account", entity_id=account_id, at=self.clock(),
            actor_id=actor_id, detail=detail or {}))

    @staticmethod
    def _parse_role(role) -> Role:
        if isinstance(role, Role):
            return role
        try:
            return Role(role)
        except ValueError:
            raise ValidationError(f"Unknown role '{role}'.")

    def create_account(self, username: str, password: str, role,
                       scope: Optional[dict] = None,
                       actor_id: Optional[str] = None,
                       account_id: Optional[str] = None) -> UserAccount:
        """Create a new login. ``account_id`` is for deterministic demo seeding
        only — the HTTP-facing facade method never accepts a caller-supplied id.
        """
        username = (username or "").strip().lower()
        if not username:
            raise ValidationError("A username is required.")
        if self.store.get_user_account_by_username(username) is not None:
            raise ValidationError(f"Username '{username}' is already taken.")
        if not password:
            raise ValidationError("A password is required.")
        role = self._parse_role(role)
        account = UserAccount(
            id=account_id or self.store.next_id("user"),
            username=username,
            password_hash=hash_password(password),
            role=role,
            created_at=self.clock(),
            scope=dict(scope or {}),
            active=True,
        )
        self.store.add_user_account(account)
        self._audit("user_account_created", account.id, actor_id=actor_id,
                   detail={"username": username, "role": role.value})
        return account

    def set_active(self, account_id: str, active: bool,
                   actor_id: Optional[str] = None) -> UserAccount:
        account = self.store.get_user_account(account_id)
        if account is None:
            raise NotFoundError("User account not found.")
        account.active = bool(active)
        self.store.save_user_account(account)
        self._audit(
            "user_account_activated" if account.active else "user_account_deactivated",
            account.id, actor_id=actor_id)
        return account

    def verify_login(self, username: str, password: str) -> Optional[UserAccount]:
        """Return the account if the credentials are valid and it is active.

        Always runs a hash comparison, even for an unknown username, so the
        response time does not reveal whether an account exists.
        """
        username = (username or "").strip().lower()
        account = self.store.get_user_account_by_username(username)
        stored_hash = account.password_hash if account is not None else (
            "pbkdf2_sha256$1$00$00")  # dummy, deliberately fails verify_password
        ok = verify_password(password or "", stored_hash)
        if account is None or not account.active or not ok:
            return None
        return account

    def list_accounts(self):
        return sorted(self.store.all_user_accounts(), key=lambda a: a.username)
