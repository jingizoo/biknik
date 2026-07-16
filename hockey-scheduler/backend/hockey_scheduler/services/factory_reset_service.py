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

The challenge and the "one reset in progress" lock both live in the store
(``FactoryResetChallenge``/``FactoryResetLock``), not in this Python
object's memory (#256 review round 1 blocker 5) — a preview() and a later
execute() reaching different ``FactoryResetService`` instances, processes,
or server workers that share the same store still see the same outstanding
challenge and correctly serialize against each other. The lock additionally
carries a lease (``expires_at``) and an owner ``token`` (round 2 blocker 3):
a crashed process's lock can be reclaimed after it expires, and release is
compare-and-delete so a delayed release can never remove a different
process's active lock.
"""

import hashlib
import hmac
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

from ..domain import (
    FactoryResetChallenge,
    FactoryResetEvent,
    FactoryResetLock,
    InstallationState,
    Role,
    UserAccount,
)
from ..domain.errors import NotAuthorizedError, ValidationError
from ..domain.roles import Permission, can
from .account_service import AccountService

# Exact phrase an operator must type (issue #256) — not a secret, so a plain
# comparison is fine; the security property here is "typed deliberately",
# not "resistant to guessing".
CONFIRMATION_PHRASE = "DELETE ALL PRODUCTION DATA"

DEFAULT_CHALLENGE_TTL_SECONDS = 5 * 60
DEFAULT_LOCK_LEASE_SECONDS = 5 * 60

_CHALLENGE_ID = "singleton"
_LOCK_ID = "singleton"


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
                 challenge_ttl_seconds: int = DEFAULT_CHALLENGE_TTL_SECONDS,
                 lock_lease_seconds: int = DEFAULT_LOCK_LEASE_SECONDS):
        self.store = store
        self.accounts = accounts
        self.clock = clock
        self._challenge_ttl = challenge_ttl_seconds
        self._lock_lease = lock_lease_seconds

    def _require_admin(self, actor_id: Optional[str]) -> UserAccount:
        """Require an authenticated, active League Admin holding both
        manage_setup and manage_users (#256 review blocker 4) — the exact
        role is checked explicitly, not just the two permissions, so a
        future change to the permission matrix can never silently grant
        wipe authority to some other role that happens to gain both."""
        account = self.store.get_user_account(actor_id) if actor_id else None
        if account is None or not account.active:
            raise NotAuthorizedError(
                "Not authorized.", {"reason": "not_authorized"})
        if account.role != Role.LEAGUE_ADMIN or not (
                can(account.role, Permission.MANAGE_SETUP)
                and can(account.role, Permission.MANAGE_USERS)):
            raise NotAuthorizedError(
                "Factory reset requires a League Admin with manage_setup "
                "and manage_users.", {"reason": "insufficient_permission"})
        return account

    def preview(self, actor_id: str) -> dict:
        """Row-count snapshot plus a short-lived, single-use challenge token
        bound to this actor and this exact snapshot. Generating a preview is
        read-only — it writes nothing but the durable challenge row itself,
        which always replaces any prior, unconsumed one."""
        self._require_admin(actor_id)
        counts = self.store.row_counts()
        token = secrets.token_urlsafe(24)
        now = self.clock()
        expires_at = now + timedelta(seconds=self._challenge_ttl)
        self.store.set_factory_reset_challenge(FactoryResetChallenge(
            id=_CHALLENGE_ID, token_hash=_hash_token(token), actor_id=actor_id,
            counts=counts, expires_at=expires_at, created_at=now))
        return {"counts": counts, "challenge_token": token,
                "expires_at": expires_at.isoformat()}

    def _consume_challenge(self, challenge_token: Optional[str],
                           actor_id: str) -> FactoryResetChallenge:
        """Validate and immediately invalidate the outstanding challenge —
        single-use regardless of whether it turns out to be valid, so a
        replayed token (stale or reused) is always rejected."""
        challenge = self.store.get_factory_reset_challenge()
        self.store.clear_factory_reset_challenge()
        if challenge is None:
            raise ValidationError(
                "No active factory-reset challenge. Request a new preview.",
                {"reason": "invalid_challenge"})
        if challenge.expires_at < self.clock():
            raise ValidationError(
                "The factory-reset challenge has expired. Request a new "
                "preview.", {"reason": "invalid_challenge"})
        if challenge.actor_id != actor_id:
            raise ValidationError(
                "This challenge does not belong to the current actor.",
                {"reason": "invalid_challenge"})
        if not hmac.compare_digest(
                challenge.token_hash, _hash_token(challenge_token or "")):
            raise ValidationError(
                "Invalid factory-reset challenge token.",
                {"reason": "invalid_challenge"})
        return challenge

    def execute(self, actor_id: str, password: str, typed_phrase: str,
               challenge_token: str, backup_acknowledged,
               environment: str = "production") -> dict:
        """Every check below must pass, in order, before the first write.
        Any rejection performs zero writes — not even the durable event row,
        since nothing worth auditing happened yet. Only once every guard has
        passed does the atomic wipe run, and the durable success event is
        written inside that same transaction (#256 review round 1 blocker
        2) so an event-write failure rolls the whole wipe back rather than
        leaving production data gone with no surviving record. A failure
        past that point gets its own durable "failed" event, written in a
        fresh operation after the rollback completes.
        """
        # A crashed process's lock is reclaimable once its lease expires
        # (#256 review round 2 blocker 3) — try this before acquiring so a
        # stale lock never blocks forever.
        self.store.release_stale_factory_reset_lock(self.clock())
        lock_token = secrets.token_urlsafe(24)
        now = self.clock()
        acquired = self.store.acquire_factory_reset_lock(FactoryResetLock(
            id=_LOCK_ID, actor_id=actor_id or "", token=lock_token,
            acquired_at=now,
            expires_at=now + timedelta(seconds=self._lock_lease)))
        if not acquired:
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
            # Require the JSON value to be exactly `true` (#256 review
            # round 1 blocker 3) — a truthiness check would accept the
            # strings "false"/"no" or the number 1, silently treating an
            # unset or malformed acknowledgement as given.
            if backup_acknowledged is not True:
                raise ValidationError(
                    "Backup acknowledgement is required.",
                    {"reason": "backup_not_acknowledged"})
            challenge = self._consume_challenge(challenge_token, actor_id)

            # A random id, not store.next_id() (#256 review round 2 blocker
            # 2): next_id()'s counter increment is a write inside the wipe
            # transaction below, so it would roll back on failure while the
            # separately-written "failed" event keeps the id it names — the
            # next successful reset would then reissue and collide with
            # that same counter value. A random id has no such dependency
            # on the transaction that might roll back.
            event_id = f"factoryreset_{uuid.uuid4().hex}"
            started_at = self.clock()
            try:
                with self.store.transaction():
                    # Block ordinary concurrent writes on every clearable
                    # table BEFORE re-checking counts (#256 review round 2
                    # blocker 1) — otherwise a write that lands between the
                    # count and clear_all_data() would be silently wiped
                    # without ever appearing in the confirmed preview.
                    self.store.lock_clearable_tables_for_wipe()
                    current_counts = self.store.row_counts()
                    if current_counts != challenge.counts:
                        raise ValidationError(
                            "The data has changed since the preview was "
                            "generated. Request a new preview and try "
                            "again.", {"reason": "preview_stale"})
                    self.store.clear_all_data()
                    preserved = self._reinsert_admin(account)
                    self.store.add_factory_reset_event(FactoryResetEvent(
                        id=event_id, actor_id=actor_id, environment=environment,
                        started_at=started_at, result="success",
                        pre_reset_counts=challenge.counts,
                        completed_at=self.clock(), failure_reason=None))
            except ValidationError:
                # Rejected before (or as part of) the atomic re-check — the
                # transaction rolled back with nothing durable staged, so
                # there is no failure to record.
                raise
            except Exception as exc:
                self.store.add_factory_reset_event(FactoryResetEvent(
                    id=event_id, actor_id=actor_id, environment=environment,
                    started_at=started_at, result="failed",
                    pre_reset_counts=challenge.counts,
                    completed_at=self.clock(),
                    failure_reason=_sanitize_failure(exc)))
                raise
            return {"result": "success", "event_id": event_id,
                    "preserved_account_id": preserved.id}
        finally:
            self.store.release_factory_reset_lock(lock_token)

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
