"""Real user accounts: creation, activation, and login verification (#67).

Operator-created accounts only — no self-service signup, password reset,
magic links, or 2FA in this slice. A ``UserAccount`` binds a hashed password
to a ``Role`` and an optional ``scope`` (the same shape a session carries:
``team_id`` for a coach, ``player_id`` for a player, ``official_id`` for an
official), so signing in as that account produces exactly the session the
role/scope model already enforces.
"""

import functools
from datetime import datetime, timezone
from typing import Callable, Optional

from ..domain import InstallationState, Role, SetupAuditLog, UserAccount
from ..domain.errors import (
    AlreadyClaimedError,
    IntegrityConflictError,
    NotFoundError,
    ValidationError,
)
from ..store import InMemoryStore
from .passwords import DUMMY_PASSWORD_HASH, hash_password, verify_password


_INSTALLATION_STATE_ID = "primary"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _transactional(fn):
    """Wrap a mutating service method in a single store transaction."""
    @functools.wraps(fn)
    def wrapper(self, *args, **kwargs):
        with self.store.transaction():
            return fn(self, *args, **kwargs)
    return wrapper


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

    @_transactional
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
        scope = dict(scope or {})
        # A role-scoped Player/Official reference must resolve to a real
        # subject *at creation*, not only at reactivation (#232 review 7):
        # without this, deleting a Player/Official and then creating a fresh
        # account with the old id recreates the exact dangling live identity
        # this issue set out to prevent. Checked — and the whole method
        # transactional (#232 review 7) — before any store write, so a
        # rejected creation writes neither an account nor an audit row.
        official_id = scope.get("official_id")
        if official_id and self.store.get_official(official_id) is None:
            raise ValidationError(
                "That official does not exist.",
                {"reason": "scope_subject_missing", "official_id": official_id})
        player_id = scope.get("player_id")
        if player_id and self.store.get_player(player_id) is None:
            raise ValidationError(
                "That player does not exist.",
                {"reason": "scope_subject_missing", "player_id": player_id})
        # A Coach's authority is entirely its team scope (#266): an account with
        # no ``team_id`` is refused at the scope gate and can manage no roster,
        # so creating one is a silent dead end at best and a fail-open hole if
        # the gate ever regressed. Require the team AND prove it resolves to a
        # real (non-deleted) Team before any write, mirroring the official/player
        # subject checks above — a rejected creation writes neither account nor
        # audit row (the method is transactional).
        if role == Role.COACH:
            team_id = scope.get("team_id")
            if not team_id:
                raise ValidationError(
                    "A coach account must be assigned a team.",
                    {"reason": "scope_required", "field": "team_id"})
            if self.store.get_team(team_id) is None:
                raise ValidationError(
                    "That team does not exist.",
                    {"reason": "scope_subject_missing", "team_id": team_id})
        account = UserAccount(
            id=account_id or self.store.next_id("user"),
            username=username,
            password_hash=hash_password(password),
            role=role,
            created_at=self.clock(),
            scope=scope,
            active=True,
        )
        self.store.add_user_account(account)
        self._audit("user_account_created", account.id, actor_id=actor_id,
                    detail={"username": username, "role": role.value})
        return account

    def create_first_admin_if_unclaimed(
            self, username: str, password: str,
            actor_id: Optional[str] = None,
            claim_method: str = "operations") -> UserAccount:
        """Atomically create the installation's one and only first admin (#174).

        The durable ``InstallationState`` marker is inserted *before* the account
        row inside the same transaction. Its single primary key serializes two
        concurrent claimers even across separate application processes: one
        transaction wins, and the losing transaction rolls back before creating
        an account. An existing account also closes the claim path for databases
        created before the marker migration.
        """
        username = (username or "").strip().lower()
        if not username:
            raise ValidationError("A username is required.")
        if not isinstance(password, str) or not password:
            raise ValidationError("A password is required.")
        method = (claim_method or "operations").strip()[:40] or "operations"

        try:
            with self.store.transaction():
                if (self.store.get_installation_state(_INSTALLATION_STATE_ID)
                        is not None or self.store.all_user_accounts()):
                    raise AlreadyClaimedError(
                        "This installation has already been claimed.")

                account_id = self.store.next_id("user")
                effective_actor = actor_id or account_id
                self.store.add_installation_state(InstallationState(
                    id=_INSTALLATION_STATE_ID,
                    claimed_at=self.clock(),
                    claimed_by_user_id=account_id,
                    claim_method=method,
                ))
                account = self.create_account(
                    username, password, Role.LEAGUE_ADMIN,
                    actor_id=effective_actor, account_id=account_id)
                self._audit(
                    "installation_claimed", account.id,
                    actor_id=effective_actor,
                    detail={"claim_method": method})
                return account
        except AlreadyClaimedError:
            raise
        except IntegrityConflictError:
            # A second process won the unique installation-marker insert while
            # this transaction was waiting. The store already translated that
            # DB-specific unique violation into a stable conflict (#201 Slice 2);
            # re-check and surface the domain-specific claimed error instead of
            # inspecting driver exceptions here.
            claimed = self.store.get_installation_state(
                _INSTALLATION_STATE_ID) is not None
            has_accounts = bool(self.store.all_user_accounts())
            if claimed or has_accounts:
                raise AlreadyClaimedError(
                    "This installation has already been claimed.") from None
            raise

    def set_active(self, account_id: str, active: bool,
                   actor_id: Optional[str] = None) -> UserAccount:
        account = self.store.get_user_account(account_id)
        if account is None:
            raise NotFoundError("User account not found.")
        if active:
            # Reactivating an account whose scoped subject was deleted while
            # the account sat deactivated (#232 review) would resurrect a
            # login pointing at a nonexistent Official/Player — the exact
            # dangling-identity hole scoping delete_official/delete_player's
            # account blocker to active-only accounts opened up. Refuse
            # until the account is rebound to a valid subject.
            scope = account.scope or {}
            official_id = scope.get("official_id")
            if official_id and self.store.get_official(official_id) is None:
                raise ValidationError(
                    "This account's official no longer exists; rebind it to "
                    "a valid official before reactivating.",
                    {"reason": "scope_subject_missing", "account_id": account_id,
                     "official_id": official_id})
            player_id = scope.get("player_id")
            if player_id and self.store.get_player(player_id) is None:
                raise ValidationError(
                    "This account's player no longer exists; rebind it to "
                    "a valid player before reactivating.",
                    {"reason": "scope_subject_missing", "account_id": account_id,
                     "player_id": player_id})
            # A Coach account must still carry a valid team scope to be
            # reactivated (#266) — reactivating an unscoped or dangling-team
            # coach would resurrect a login the scope gate now refuses, or (if
            # the team was deleted meanwhile) a coach bound to a nonexistent
            # team. Refuse until it is rebound to a real team.
            if account.role == Role.COACH:
                team_id = scope.get("team_id")
                if not team_id:
                    raise ValidationError(
                        "This coach account has no assigned team; assign a "
                        "team before reactivating.",
                        {"reason": "scope_required", "account_id": account_id,
                         "field": "team_id"})
                if self.store.get_team(team_id) is None:
                    raise ValidationError(
                        "This account's team no longer exists; rebind it to "
                        "a valid team before reactivating.",
                        {"reason": "scope_subject_missing",
                         "account_id": account_id, "team_id": team_id})
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
        # A real-cost dummy so an unknown username pays the same PBKDF2 work
        # as a known one with a wrong password (timing-safe existence check).
        stored_hash = account.password_hash if account is not None else DUMMY_PASSWORD_HASH
        ok = verify_password(password or "", stored_hash)
        if account is None or not account.active or not ok:
            return None
        return account

    def list_accounts(self):
        return sorted(self.store.all_user_accounts(), key=lambda a: a.username)
