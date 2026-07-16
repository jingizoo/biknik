"""Guarded production factory reset (#256).

A separate, exceptional whole-installation wipe workflow — distinct from the
ordinary dependency-aware record deletion (#232) and from the demo
load/reset/clear lifecycle (#215, ``web.server.DemoState``). Neither of those
is reused here: demo reset is blocked outright in production and rebuilds the
schema from scratch, while ordinary deletion is scoped to one record at a
time. This service wipes every row across the whole installation atomically,
gated by a chain of independent safety controls that must ALL pass before a
single write happens.

Deliberately NOT wired to any HTTP route by this module — ``web/server.py``
owns the feature-flag check (``ALLOW_PRODUCTION_FACTORY_RESET``), rate
limiting, and request parsing; this service enforces the checks that must
hold even for a direct, non-HTTP caller (role/permission, password
reauthentication, typed confirmation, challenge validity, backup
acknowledgement, and the single-in-flight guard).
"""

import hashlib
import hmac
import secrets
import threading
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

from ..domain import FactoryResetEvent, InstallationState, Role, UserAccount
from ..domain.errors import NotAuthorizedError, ValidationError
from ..domain.roles import Permission, can
from .account_service import AccountService

# Exact phrase an operator must type (issue #256) — not a secret, so a plain
# comparison is fine; the security property here is "typed deliberately",
# not "resistant to guessing".
CONFIRMATION_PHRASE = "DELETE ALL PRODUCTION DATA"

DEFAULT_CHALLENGE_TTL_SECONDS = 5 * 60


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _hash_token(token: str) -> str:
    return hashlib.sha256((token or "").encode("utf-8")).hexdigest()


def _sanitize_failure(exc: Exception) -> str:
    """A short, secret-free failure label for the durable event row — never
    the raw exception text, which could carry a DB connection string, table
    name detail, or other operational internals."""
    return type(exc).__name__[:80]


class FactoryResetService:
    def __init__(self, store, accounts: AccountService,
                 clock: Callable[[], datetime] = _utcnow,
                 challenge_ttl_seconds: int = DEFAULT_CHALLENGE_TTL_SECONDS):
        self.store = store
        self.accounts = accounts
        self.clock = clock
        self._challenge_ttl = challenge_ttl_seconds
        self._challenge: Optional[dict] = None
        self._challenge_lock = threading.Lock()
        self._execute_lock = threading.Lock()

    def _require_admin(self, actor_id: Optional[str]) -> UserAccount:
        account = self.store.get_user_account(actor_id) if actor_id else None
        if account is None or not account.active:
            raise NotAuthorizedError(
                "Not authorized.", {"reason": "not_authorized"})
        if not (can(account.role, Permission.MANAGE_SETUP)
                and can(account.role, Permission.MANAGE_USERS)):
            raise NotAuthorizedError(
                "Factory reset requires manage_setup and manage_users.",
                {"reason": "insufficient_permission"})
        return account

    def preview(self, actor_id: str) -> dict:
        """Row-count snapshot plus a short-lived, single-use challenge token
        bound to this actor and this exact snapshot. Generating a preview is
        read-only — it writes nothing to the store."""
        self._require_admin(actor_id)
        counts = self.store.row_counts()
        token = secrets.token_urlsafe(24)
        expires_at = self.clock() + timedelta(seconds=self._challenge_ttl)
        with self._challenge_lock:
            self._challenge = {
                "token_hash": _hash_token(token),
                "actor_id": actor_id,
                "counts": counts,
                "expires_at": expires_at,
            }
        return {"counts": counts, "challenge_token": token,
                "expires_at": expires_at.isoformat()}

    def _consume_challenge(self, challenge_token: Optional[str],
                           actor_id: str) -> dict:
        """Validate and immediately invalidate the outstanding challenge —
        single-use regardless of whether it turns out to be valid, so a
        replayed token (stale or reused) is always rejected."""
        with self._challenge_lock:
            challenge = self._challenge
            self._challenge = None
        if challenge is None:
            raise ValidationError(
                "No active factory-reset challenge. Request a new preview.",
                {"reason": "invalid_challenge"})
        if challenge["expires_at"] < self.clock():
            raise ValidationError(
                "The factory-reset challenge has expired. Request a new "
                "preview.", {"reason": "invalid_challenge"})
        if challenge["actor_id"] != actor_id:
            raise ValidationError(
                "This challenge does not belong to the current actor.",
                {"reason": "invalid_challenge"})
        if not hmac.compare_digest(
                challenge["token_hash"], _hash_token(challenge_token or "")):
            raise ValidationError(
                "Invalid factory-reset challenge token.",
                {"reason": "invalid_challenge"})
        return challenge

    def execute(self, actor_id: str, password: str, typed_phrase: str,
               challenge_token: str, backup_acknowledged: bool,
               environment: str = "production") -> dict:
        """Every check below must pass, in order, before the first write.
        Any rejection performs zero writes — not even the durable event row,
        since nothing worth auditing happened yet. Only once every guard has
        passed does the atomic wipe run, followed unconditionally by exactly
        one durable ``FactoryResetEvent`` recording success or failure.
        """
        if not self._execute_lock.acquire(blocking=False):
            raise ValidationError(
                "A factory reset is already in progress.",
                {"reason": "reset_in_progress"})
        try:
            account = self._require_admin(actor_id)
            verified = self.accounts.verify_login(account.username, password or "")
            if verified is None or verified.id != account.id:
                raise NotAuthorizedError(
                    "Password re-authentication failed.",
                    {"reason": "reauth_failed"})
            if typed_phrase != CONFIRMATION_PHRASE:
                raise ValidationError(
                    "Typed confirmation phrase did not match.",
                    {"reason": "phrase_mismatch"})
            if not backup_acknowledged:
                raise ValidationError(
                    "Backup acknowledgement is required.",
                    {"reason": "backup_not_acknowledged"})
            challenge = self._consume_challenge(challenge_token, actor_id)

            # Every guard passed — this is the first point any write happens.
            event_id = self.store.next_id("factoryreset")
            started_at = self.clock()
            result, failure_reason = "success", None
            try:
                with self.store.transaction():
                    self.store.clear_all_data()
                    preserved = self._reinsert_admin(account)
            except Exception as exc:
                result, failure_reason = "failed", _sanitize_failure(exc)
                raise
            finally:
                self.store.add_factory_reset_event(FactoryResetEvent(
                    id=event_id, actor_id=actor_id, environment=environment,
                    started_at=started_at, result=result,
                    pre_reset_counts=challenge["counts"],
                    completed_at=self.clock(), failure_reason=failure_reason))
            return {"result": "success", "event_id": event_id,
                    "preserved_account_id": preserved.id}
        finally:
            self._execute_lock.release()

    def _reinsert_admin(self, account: UserAccount) -> UserAccount:
        """Re-create exactly the acting admin's account (preferred default
        per #256: preserve the actor, not a fresh bootstrap claim) and a
        fresh ``InstallationState`` marker, inside the same transaction as
        the wipe — so a reset either preserves one working admin atomically
        or leaves the previous installation completely untouched. Scope is
        reset to empty: a team/official binding from before the reset may no
        longer resolve to anything once every row is gone."""
        preserved = UserAccount(
            id=account.id, username=account.username,
            password_hash=account.password_hash, role=account.role,
            created_at=account.created_at, scope={}, active=True)
        self.store.add_user_account(preserved)
        self.store.add_installation_state(InstallationState(
            id="primary", claimed_at=self.clock(),
            claimed_by_user_id=preserved.id,
            claim_method="factory_reset_preserved"))
        return preserved
