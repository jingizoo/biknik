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

import secrets
import time

from ..domain import ROLE_LABELS, Role

# The six demo personas seeded as real UserAccount rows on every reset (#67) —
# see DemoState.reset(). Password is obviously-fictional and shared only for
# the demo; a real deployment would never seed accounts this way.
DEMO_PASSWORD = "demo"
DEMO_USERS = {
    "admin": Role.LEAGUE_ADMIN,
    "arena": Role.ARENA_MANAGER,
    "coach": Role.COACH,
    "player": Role.PLAYER,
    "official": Role.OFFICIAL,
    "viewer": Role.VIEWER,
}

SESSION_COOKIE = "hs_sid"
DEFAULT_TTL_SECONDS = 8 * 60 * 60  # 8h


class SessionManager:
    """Issues and resolves session tokens for an already-verified account.

    Sessions live outside the demo data store so signing in survives a demo
    reset (which rebuilds the store, including its UserAccount rows).
    """

    def __init__(self, ttl_seconds: int = DEFAULT_TTL_SECONDS, clock=time.time):
        self._sessions = {}          # token -> {user_id, role, scope, expires}
        self._ttl = ttl_seconds
        self._clock = clock

    def login(self, user_id: str, role: Role, scope=None) -> str:
        """Issue a new session token for an already-authenticated account."""
        token = secrets.token_urlsafe(24)
        self._sessions[token] = {
            "user_id": user_id,
            "role": role,
            "scope": dict(scope or {}),
            "expires": self._clock() + self._ttl,
        }
        return token

    def resolve(self, token):
        """Return the live session dict for a token, or None if unknown/expired."""
        if not token:
            return None
        sess = self._sessions.get(token)
        if sess is None:
            return None
        if sess["expires"] < self._clock():
            self._sessions.pop(token, None)
            return None
        return sess

    def logout(self, token) -> None:
        self._sessions.pop(token, None)

    def revoke_for_user(self, user_id: str) -> None:
        """Kill every live session for a user — e.g. on account deactivation."""
        for token, sess in list(self._sessions.items()):
            if sess["user_id"] == user_id:
                self._sessions.pop(token, None)


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
        "username": username,
        "role": role.value,
        "label": ROLE_LABELS[role],
        "scope": scope,
    }
