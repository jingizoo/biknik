"""Sessions for the web layer, tied to a real user account (#50, #67).

A tiny, server-issued session layer so the acting role comes from an
authenticated session instead of a client-asserted header. As of #67,
credentials are verified against a hashed :class:`UserAccount` in the store
(via ``AccountService.verify_login``); this module only manages the session
that results — it never sees a password. Sessions still live in memory and
are not persisted, which is fine for a single-process demo.

Kept out of ``domain/`` on purpose: identity/session is a transport concern,
while the role→permission policy stays a pure domain module.
"""

import hashlib
import secrets
from datetime import datetime, timedelta, timezone

from ..domain import ROLE_LABELS, Role, Session

# The demo personas seeded as real UserAccount rows on every reset (#67) —
# see DemoState.reset(). Password is obviously-fictional and shared only for
# the demo; a real deployment would never seed accounts this way. The
# "guardian" persona (#26) is linked+verified to a junior player at seed time
# so the guardian linked-junior workflow is demoable out of the box.
DEMO_PASSWORD = "demo"
DEMO_USERS = {
    "admin": Role.LEAGUE_ADMIN,
    "arena": Role.ARENA_MANAGER,
    "coach": Role.COACH,
    "player": Role.PLAYER,
    "official": Role.OFFICIAL,
    "viewer": Role.VIEWER,
    "guardian": Role.GUARDIAN,
}

SESSION_COOKIE = "hs_sid"
DEFAULT_TTL_SECONDS = 8 * 60 * 60  # 8h

# How long a finished (expired or revoked) session is retained before cleanup
# prunes it — a grace window so recently-revoked sessions stay visible for
# audit/debugging (#77). Active sessions are never touched by cleanup.
DEFAULT_RETENTION_SECONDS = 7 * 24 * 60 * 60  # 7 days


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def hash_token(token: str) -> str:
    """SHA-256 of a session token. Tokens are 24 bytes of ``secrets`` entropy,
    so a fast unsalted hash is fine (no brute-force surface, unlike passwords)
    — the point is that a store dump holds only hashes, never usable tokens.
    """
    return hashlib.sha256((token or "").encode("utf-8")).hexdigest()


class SessionManager:
    """Issues and resolves persistent sessions backed by the store (#74).

    Only the token *hash* is persisted (``Session.token_hash``); the raw token
    lives solely in the client cookie. Role and scope are NOT stored on the
    session — they are resolved from the backing :class:`UserAccount` on every
    lookup, so a session always reflects the account's current role/scope and
    active state. Store handles are passed per call so a single module-global
    manager works across demo resets (which rebuild the store).
    """

    def __init__(self, ttl_seconds: int = DEFAULT_TTL_SECONDS, clock=_utcnow,
                 retention_seconds: int = DEFAULT_RETENTION_SECONDS):
        self._ttl = ttl_seconds
        self._clock = clock
        self._retention = retention_seconds

    def login(self, store, user_id: str, user_agent=None) -> str:
        """Issue a new session for an already-authenticated account; return the
        raw token (only its hash is persisted).

        Opportunistically prunes long-finished sessions so the table can't grow
        unbounded (#77) — cheap, piggy-backed on a naturally infrequent event,
        and never touches active or recently-finished rows.
        """
        token = secrets.token_urlsafe(24)
        now = self._clock()
        store.add_session(Session(
            id=store.next_id("session"),
            token_hash=hash_token(token),
            user_id=user_id,
            issued_at=now,
            expires_at=now + timedelta(seconds=self._ttl),
            user_agent=(user_agent or None),
        ))
        self.cleanup(store)
        return token

    def cleanup(self, store) -> int:
        """Delete sessions that finished (expired or were revoked) more than the
        retention window ago. Returns the number pruned. Safe to call anytime;
        deterministic under the injected clock."""
        cutoff = self._clock() - timedelta(seconds=self._retention)
        return store.delete_sessions_before(cutoff)

    def resolve(self, store, token):
        """Return ``{user_id, role, scope}`` for a live session, else None.

        A session is invalid if unknown, revoked, expired, or if its backing
        account is missing or deactivated (belt-and-suspenders with explicit
        revocation on deactivation).
        """
        if not token:
            return None
        sess = store.get_session_by_hash(hash_token(token))
        if sess is None or sess.revoked_at is not None:
            return None
        if sess.expires_at < self._clock():
            return None
        account = store.get_user_account(sess.user_id)
        if account is None or not account.active:
            return None
        return {"user_id": sess.user_id, "role": account.role,
                "scope": dict(account.scope or {})}

    def logout(self, store, token) -> None:
        if not token:
            return
        sess = store.get_session_by_hash(hash_token(token))
        if sess is not None and sess.revoked_at is None:
            sess.revoked_at = self._clock()
            store.save_session(sess)

    def revoke_for_user(self, store, user_id: str) -> None:
        """Revoke every live session for a user — e.g. on deactivation."""
        now = self._clock()
        for sess in store.sessions_for_user(user_id):
            if sess.revoked_at is None:
                sess.revoked_at = now
                store.save_session(sess)


def user_view(session, store=None) -> dict:
    """Public shape for a resolved session (no token).

    When a ``store`` is given, the username comes from the backing
    :class:`UserAccount` and the scope's team/player ids are enriched with
    display names so the UI can show what the account is bound to (#51).
    """
    role = session["role"]
    scope = dict(session.get("scope") or {})
    username = session.get("user_id")  # fallback if the account can't be found
    if store is not None:
        account = store.get_user_account(session.get("user_id"))
        if account is not None:
            username = account.username
        # A Player's stored scope is player_id only (#160); resolve the team LIVE
        # from the player so the client's coarse "can read private games" hint
        # tracks the player's CURRENT team — reflecting a later team assignment or
        # a transfer, and showing none when teamless — instead of a stale copy.
        if role == Role.PLAYER:
            pid = scope.get("player_id")
            player = store.get_player(pid) if pid else None
            if player is not None and player.team_id:
                scope["team_id"] = player.team_id
            else:
                scope.pop("team_id", None)
        tid = scope.get("team_id")
        if tid:
            team = store.get_team(tid)
            scope["team_name"] = team.name if team else tid
        pid = scope.get("player_id")
        if pid:
            player = store.get_player(pid)
            scope["player_name"] = player.name if player else pid
        oid = scope.get("official_id")
        if oid:
            official = store.get_official(oid)
            scope["official_name"] = official.name if official else oid
    return {
        "id": session.get("user_id"),
        "username": username,
        "role": role.value,
        "label": ROLE_LABELS[role],
        "scope": scope,
    }
