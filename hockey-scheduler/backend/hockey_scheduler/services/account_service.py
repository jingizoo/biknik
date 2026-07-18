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
from .passwords import (
    DUMMY_PASSWORD_HASH,
    hash_password,
    password_policy_error,
    verify_password,
)


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

    # The ONLY scope keys each role may carry (#266 review). Any other key is
    # rejected so a scope can never smuggle in an authority binding the role's
    # session resolution/gate doesn't honor (e.g. a coach carrying a stray
    # player_id, or an admin/guardian carrying any scope at all). A player may
    # carry team_id in addition to player_id — it gates their private game reads
    # (web/scope.py). Roles absent from this map allow NO scope keys.
    _ALLOWED_SCOPE_KEYS = {
        Role.COACH: frozenset({"team_id"}),
        Role.PLAYER: frozenset({"player_id", "team_id"}),
        Role.OFFICIAL: frozenset({"official_id"}),
    }

    def _sanitized_scope(self, role, scope) -> dict:
        """The role's supported scope keys with non-empty values only (#266
        review). Used for audit details so a legacy/malformed scope — a
        pre-existing account whose ``scope`` carries unsupported keys — can never
        leak that stray data into an audit row, and so the creation audit records
        exactly the binding that took effect."""
        allowed = self._ALLOWED_SCOPE_KEYS.get(role, frozenset())
        return {key: scope[key] for key in allowed if scope.get(key)}

    def _validate_scope_subjects(self, role, scope) -> None:
        """Resolve every role-scoped subject in ``scope`` to a real record, or
        raise a stable ``ValidationError``. Shared by account creation and the
        scope-rebind remediation path (#266) so both enforce the identical rule.
        Must run inside a ``transaction()``: the Coach team is row-locked
        (``get_team_for_update``) so a concurrent Team deletion can't strand a
        coach against a deleted team.

        A role-scoped Player/Official reference (#232 review 7) must resolve too:
        deleting a Player/Official then binding the old id would recreate the
        exact dangling live identity that issue set out to prevent.

        Beyond validating, this **canonicalizes a Player scope in place** (#160):
        a Player's private-game reads gate on ``team_id`` (web/scope.py), so the
        resolved player's own team is written into ``scope["team_id"]`` even when
        the caller supplied ``player_id`` only. Both callers pass a scope dict
        they own and then persist it, so the account/session carries both keys —
        keeping the account consistent with the read gate and the frontend helper.
        """
        # Reject any scope key the role does not explicitly support (#266
        # review) — an unexpected key is a misconfiguration, not silently kept.
        allowed = self._ALLOWED_SCOPE_KEYS.get(role, frozenset())
        unexpected = sorted(k for k in scope if k not in allowed)
        if unexpected:
            raise ValidationError(
                f"Scope key(s) not allowed for role {role.value}: "
                + ", ".join(unexpected) + ".",
                {"reason": "unknown_scope_key", "role": role.value,
                 "keys": unexpected})
        # Row-lock the Player/Official subject (#282 review), exactly as the
        # Coach team is locked below: bind and delete_player/delete_official
        # otherwise resolve the subject with unlocked reads, so on PostgreSQL a
        # concurrent delete could commit between this check and the account
        # write and strand an active login against a deleted Player/Official.
        # delete_* takes the same lock before its account scan, so the two
        # serialize either way (bind-then-delete blocks on the live account
        # reference; delete-then-bind sees the subject gone here).
        official_id = scope.get("official_id")
        if official_id and self.store.get_official_for_update(official_id) is None:
            raise ValidationError(
                "That official does not exist.",
                {"reason": "scope_subject_missing", "official_id": official_id})
        player_id = scope.get("player_id")
        player = self.store.get_player_for_update(player_id) if player_id else None
        if player_id and player is None:
            raise ValidationError(
                "That player does not exist.",
                {"reason": "scope_subject_missing", "player_id": player_id})
        # A Player account MUST carry its own player_id (#266/#282 review): a
        # Player holds RESPOND_AVAILABILITY and substitute self-service, and the
        # scope gate fails closed for an unscoped Player, so an account with no
        # player_id has no identity to act as and would be refused at every
        # turn — reject it at creation/rebind rather than mint a dead login.
        # The team_id, when present, is only ever valid as that player's OWN
        # team (team_id gates private game reads in web/scope.py), so it is
        # verified against the resolved Player and never trusted on its own.
        if role == Role.PLAYER:
            if not player_id:
                raise ValidationError(
                    "A player account must be assigned a player.",
                    {"reason": "scope_required", "field": "player_id"})
            team_id = scope.get("team_id")
            if team_id and team_id != player.team_id:
                raise ValidationError(
                    "A player's team scope must be the player's own team.",
                    {"reason": "scope_team_mismatch",
                     "player_id": player_id, "team_id": team_id})
            # Canonicalize (#160): carry the resolved player's own team so an
            # account created/rebound with player_id ONLY still gates private
            # game reads correctly. If the player has no team yet, drop any
            # stale team_id (the mismatch check above already rejected a
            # non-matching one, so nothing valid is discarded here).
            if player.team_id:
                scope["team_id"] = player.team_id
            else:
                scope.pop("team_id", None)
        # A Coach's authority is entirely its team scope (#266): an account with
        # no team is refused at the scope gate and can manage no roster, so it
        # must be bound to a real, non-deleted Team.
        if role == Role.COACH:
            team_id = scope.get("team_id")
            if not team_id:
                raise ValidationError(
                    "A coach account must be assigned a team.",
                    {"reason": "scope_required", "field": "team_id"})
            if self.store.get_team_for_update(team_id) is None:
                raise ValidationError(
                    "That team does not exist.",
                    {"reason": "scope_subject_missing", "team_id": team_id})

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
        # Minimum credential policy (#267): enforced for every new account in
        # production; non-empty everywhere. Applies at creation only — existing
        # stored hashes are never invalidated.
        policy_error = password_policy_error(password)
        if policy_error:
            raise ValidationError(policy_error, {"reason": "weak_password"})
        role = self._parse_role(role)
        # Scope must be a JSON object (#266 review): a string or array would let
        # ``dict(scope)`` raise a raw ValueError/TypeError — a 500 rather than a
        # stable 400 — so reject a non-object shape as a domain ValidationError.
        if scope is not None and not isinstance(scope, dict):
            raise ValidationError(
                "Account scope must be a JSON object.",
                {"reason": "invalid_scope"})
        scope = dict(scope or {})
        # A role-scoped subject must resolve to a real record before any write
        # (checked inside this transaction), so a rejected creation writes
        # neither an account nor an audit row.
        self._validate_scope_subjects(role, scope)
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
                    detail={"username": username, "role": role.value,
                            "scope": self._sanitized_scope(role, scope)})
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
        # Same minimum credential policy as create_account (#267) — checked
        # before the installation marker is written, so a weak-password claim
        # attempt writes nothing.
        policy_error = password_policy_error(password)
        if policy_error:
            raise ValidationError(policy_error, {"reason": "weak_password"})
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

    @_transactional
    def set_active(self, account_id: str, active: bool,
                   actor_id: Optional[str] = None) -> UserAccount:
        # Transactional (#266 review): the account save and its audit row must
        # commit together — an audit failure after the save would otherwise
        # leave an unaudited activation/deactivation. Row-lock the account so a
        # concurrent rebind can't lost-update this activation change (#266
        # review): both read-modify-write the whole row, so without the lock one
        # would silently clobber the other (e.g. a rebind restoring active=True
        # over a deactivation, with no activation audit).
        account = self.store.get_user_account_for_update(account_id)
        if account is None:
            raise NotFoundError("User account not found.")
        if active:
            # Reactivating an account whose scoped subject was deleted while
            # the account sat deactivated (#232 review) would resurrect a
            # login pointing at a nonexistent Official/Player — the exact
            # dangling-identity hole scoping delete_official/delete_player's
            # account blocker to active-only accounts opened up. Refuse
            # until the account is rebound to a valid subject.
            # Row-lock the Player/Official subject (#282 review) so a concurrent
            # delete can't strand this reactivated login against a deleted
            # subject — same lock delete_player/delete_official take.
            scope = account.scope or {}
            official_id = scope.get("official_id")
            if official_id and self.store.get_official_for_update(official_id) is None:
                raise ValidationError(
                    "This account's official no longer exists; rebind it to "
                    "a valid official before reactivating.",
                    {"reason": "scope_subject_missing", "account_id": account_id,
                     "official_id": official_id})
            player_id = scope.get("player_id")
            # A Player account must carry a valid player_id to be reactivated
            # (#282 review): the scope gate fails closed for an unscoped Player,
            # so reactivating one would resurrect a login that can act for no
            # one — refuse until it is rebound to a real player.
            if account.role == Role.PLAYER and not player_id:
                raise ValidationError(
                    "This player account has no assigned player; assign a "
                    "player before reactivating.",
                    {"reason": "scope_required", "account_id": account_id,
                     "field": "player_id"})
            if player_id and self.store.get_player_for_update(player_id) is None:
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
                if self.store.get_team_for_update(team_id) is None:
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

    @_transactional
    def rebind_account_scope(self, account_id: str, scope,
                             actor_id: Optional[str] = None) -> UserAccount:
        """Change an account's ``scope`` binding (#266 remediation path).

        The supported admin action to *repair* a legacy or misconfigured
        account — e.g. rebind an unscoped Coach (which readiness flags and the
        scope gate refuses) to a real team, or move a coach to a different team —
        without deleting and recreating the login. The new scope is validated by
        the same rule as creation (a Coach needs a real, row-locked team; a
        Player/Official subject must resolve), and the change is audited
        (``user_account_scope_changed``) with the before/after scope. Works on
        active or inactive accounts, so a deactivated legacy coach can be rebound
        and then reactivated.
        """
        # Row-lock the account (#266 review): rebind and set_active both
        # read-modify-write the whole row, so locking serializes them and a
        # concurrent deactivation can't be clobbered by this scope change.
        account = self.store.get_user_account_for_update(account_id)
        if account is None:
            raise NotFoundError("User account not found.")
        if scope is not None and not isinstance(scope, dict):
            raise ValidationError(
                "Account scope must be a JSON object.",
                {"reason": "invalid_scope"})
        scope = dict(scope or {})
        self._validate_scope_subjects(account.role, scope)
        old_scope = dict(account.scope or {})
        account.scope = scope
        self.store.save_user_account(account)
        # Sanitize BOTH sides (#266 review): a pre-existing legacy account's
        # old_scope may carry unsupported keys, which must not leak into the
        # audit's `from`.
        self._audit("user_account_scope_changed", account.id, actor_id=actor_id,
                    detail={"from": self._sanitized_scope(account.role, old_scope),
                            "to": self._sanitized_scope(account.role, scope)})
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

    @_transactional
    def audit_login_throttled(self, ip: Optional[str], scopes) -> SetupAuditLog:
        """Record a login-throttle lockout at a safe, aggregate level (#267).

        Written once when a bucket first locks (not per blocked request), so the
        audit can't be flooded. Records the abuse source IP and which bucket(s)
        locked (``ip``/``username``) — never the submitted username value or the
        password, so the audit trail is not a username oracle and holds no raw
        credential.
        """
        return self.store.add_setup_audit(SetupAuditLog(
            id=self.store.next_id("setupaudit"), action="login_throttled",
            entity_type="auth", entity_id=(ip or "unknown")[:64],
            at=self.clock(), actor_id=None,
            detail={"scopes": sorted(scopes)}))

    def list_accounts(self):
        return sorted(self.store.all_user_accounts(), key=lambda a: a.username)
