"""Demo identity + sessions for the web layer (#50).

A tiny, server-issued session layer so the acting role comes from an
authenticated session instead of a client-asserted header. This is still a
*demo*: a fixed set of obviously-fictional accounts share one demo password,
and sessions live in memory. It is deliberately not production auth (no real
password hashing, no persistence) — it establishes the identity/session
boundary the RBAC policy (#24) sits behind.

Kept out of ``domain/`` on purpose: identity/session is a transport concern,
while the role→permission policy stays a pure domain module.
"""

import secrets
import time

from ..domain import ROLE_LABELS, Role

# Obviously-fictional shared demo password — no real secret. One account per
# role so the demo can sign in as each persona.
DEMO_PASSWORD = "demo"
DEMO_USERS = {
    "admin": Role.LEAGUE_ADMIN,
    "arena": Role.ARENA_MANAGER,
    "coach": Role.COACH,
    "player": Role.PLAYER,
    "viewer": Role.VIEWER,
}

SESSION_COOKIE = "hs_sid"
DEFAULT_TTL_SECONDS = 8 * 60 * 60  # 8h


class SessionManager:
    def __init__(self, ttl_seconds: int = DEFAULT_TTL_SECONDS, clock=time.time):
        self._sessions = {}          # token -> {username, role, expires}
        self._ttl = ttl_seconds
        self._clock = clock

    def login(self, username: str, password: str):
        """Return a new session token, or None on bad credentials."""
        role = DEMO_USERS.get((username or "").strip().lower())
        if role is None or password != DEMO_PASSWORD:
            return None
        token = secrets.token_urlsafe(24)
        self._sessions[token] = {
            "username": username.strip().lower(),
            "role": role,
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


def user_view(session) -> dict:
    """Public shape for a resolved session (no token)."""
    role = session["role"]
    return {
        "username": session["username"],
        "role": role.value,
        "label": ROLE_LABELS[role],
    }


def demo_accounts() -> list:
    """The pickable demo accounts for the sign-in UI."""
    return [
        {"username": u, "role": r.value, "label": ROLE_LABELS[r]}
        for u, r in DEMO_USERS.items()
    ]
