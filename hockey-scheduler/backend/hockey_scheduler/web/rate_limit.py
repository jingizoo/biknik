"""In-process rate limiting for anonymous routes (#131).

A fixed-window counter per (bucket, caller key). Deliberately in-memory and
single-process — this app has no distributed-deployment story yet and
CLAUDE.md rules out new third-party dependencies for this slice, so a plain
dict is the right amount of machinery. Not persisted: a process restart
resets every counter, which is fine for abuse mitigation (the goal here) and
not meant to be billing-grade limiting.

The clock is injectable (mirrors ``web.auth.SessionManager``) so tests don't
depend on wall-clock sleeps.

``web.server.Handler`` serves each request on its own thread
(``ThreadingHTTPServer``), so ``allow()`` guards its whole read-check-append
sequence with a lock — without one, two concurrent requests from the same
caller can both read the same "under limit" count before either appends,
letting more than ``limit`` through in a window (self-review, #131).

A caller who is never seen again (a one-off scanner, not someone actually
using the app) would otherwise leave a dead ``(bucket, key)`` entry parked in
``_hits`` forever — nothing ever revisits it to trim it, since trimming only
happens when THAT key calls ``allow()`` again. An amortized sweep every
``_SWEEP_EVERY`` calls drops any entry that's gone quiet for longer than any
real caller-facing window in this app, bounding memory to "callers active
recently" instead of "every caller ever seen" (self-review, #131).
"""

import os
import threading
import time
from collections import defaultdict, deque

_SWEEP_EVERY = 500
# Comfortably larger than any window_seconds actually used by a caller
# (web/server.py's buckets all use 60s) — just needs to be "clearly stale",
# not tight, since it only bounds how long a dead entry can loiter.
_STALE_AFTER_SECONDS = 3600


class RateLimiter:
    def __init__(self, clock=time.monotonic):
        self._clock = clock
        self._hits = defaultdict(deque)  # (bucket, key) -> deque[timestamps]
        self._lock = threading.Lock()
        self._calls_since_sweep = 0

    def allow(self, bucket: str, key: str, limit: int, window_seconds: float) -> bool:
        """Record one attempt for ``key`` in ``bucket`` and report whether it's
        within the last ``limit`` hits inside ``window_seconds``. Returns
        False (and does NOT count the attempt) once the caller is over limit,
        so a caller stuck at the ceiling doesn't push their reset window back
        out by continuing to hammer the route."""
        now = self._clock()
        cutoff = now - window_seconds
        with self._lock:
            dq = self._hits[(bucket, key)]
            while dq and dq[0] < cutoff:
                dq.popleft()
            allowed = len(dq) < limit
            if allowed:
                dq.append(now)
            self._calls_since_sweep += 1
            if self._calls_since_sweep >= _SWEEP_EVERY:
                self._sweep_locked(now)
            return allowed

    def _sweep_locked(self, now) -> None:
        """Drop entries that have gone quiet — caller already holds ``_lock``."""
        self._calls_since_sweep = 0
        stale_cutoff = now - _STALE_AFTER_SECONDS
        dead = [k for k, dq in self._hits.items()
                if not dq or dq[-1] < stale_cutoff]
        for k in dead:
            del self._hits[k]

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()
            self._calls_since_sweep = 0


# -- login throttle (#267) -------------------------------------------------
#
# The generic RateLimiter above counts EVERY attempt per (bucket, IP). Login
# needs more: throttle by BOTH source IP and normalized username, and drive the
# lockout off FAILED attempts only (a legitimate user signing in repeatedly must
# never be locked out; a successful sign-in clears their username's failures).
#
# Model: a sliding window of failure timestamps per key. Once a key has
# ``max_failures`` failures inside ``window_seconds`` it is LOCKED, and stays
# locked until the oldest failure ages out of the window — a short temporary
# lock, not a permanent one. ``retry_after`` is a pure peek (records nothing),
# so a caller hammering a locked account cannot push their own unlock further
# out. The same generic 429 + Retry-After is returned whether or not the
# username exists, so the throttle is not a username oracle.
#
# Bounds (safe-by-config): the window has a floor and the per-key ceilings have
# a floor of 1, so a misconfigured env value can tighten but never disable the
# protection. The clock is injectable for deterministic tests.
_LOGIN_WINDOW_DEFAULT = 900.0        # 15 minutes
_LOGIN_WINDOW_FLOOR = 30.0
_LOGIN_USER_MAX_DEFAULT = 5          # failed attempts per username per window
_LOGIN_IP_MAX_DEFAULT = 50           # failed attempts per source IP per window


def _env_float(name: str, default: float, floor: float) -> float:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        return max(floor, float(raw))
    except ValueError:
        return default


def _env_int(name: str, default: int, floor: int) -> int:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        return max(floor, int(raw))
    except ValueError:
        return default


class LoginThrottle:
    def __init__(self, clock=time.monotonic, window_seconds=None,
                 user_max=None, ip_max=None):
        self._clock = clock
        self._window = (window_seconds if window_seconds is not None
                        else _env_float("HS_LOGIN_WINDOW_SECONDS",
                                        _LOGIN_WINDOW_DEFAULT, _LOGIN_WINDOW_FLOOR))
        self._user_max = (user_max if user_max is not None
                          else _env_int("HS_LOGIN_MAX_FAILURES",
                                        _LOGIN_USER_MAX_DEFAULT, 1))
        self._ip_max = (ip_max if ip_max is not None
                        else _env_int("HS_LOGIN_IP_MAX_FAILURES",
                                      _LOGIN_IP_MAX_DEFAULT, 1))
        self._fail = defaultdict(deque)   # (scope, key) -> deque[timestamps]
        self._lock = threading.Lock()

    def _trim(self, dq, now) -> None:
        cutoff = now - self._window
        while dq and dq[0] < cutoff:
            dq.popleft()

    def _retry_after_locked(self, key, limit, now) -> float:
        dq = self._fail.get(key)
        if not dq:
            return 0.0
        self._trim(dq, now)
        if len(dq) < limit:
            return 0.0
        # Locked until the oldest in-window failure ages out.
        return max(0.0, dq[0] + self._window - now)

    def retry_after(self, ip: str, username: str) -> float:
        """Seconds the caller must wait before another login attempt is allowed
        (0.0 if allowed). Considers BOTH the source-IP and the normalized-
        username buckets and returns the larger remaining lock. Records nothing.
        """
        now = self._clock()
        with self._lock:
            return max(
                self._retry_after_locked(("ip", ip or "unknown"), self._ip_max, now),
                self._retry_after_locked(("user", username or ""), self._user_max, now))

    def record_failure(self, ip: str, username: str):
        """Record one failed attempt against both buckets. Returns the list of
        bucket scopes (``"ip"``/``"username"``) that JUST crossed into the locked
        state on this failure (empty if none) — the caller audits a lockout once,
        when it engages, rather than on every subsequent blocked request."""
        now = self._clock()
        newly_locked = []
        with self._lock:
            for scope, key, limit in (
                    ("ip", ("ip", ip or "unknown"), self._ip_max),
                    ("username", ("user", username or ""), self._user_max)):
                dq = self._fail[key]
                self._trim(dq, now)
                was_locked = len(dq) >= limit
                dq.append(now)
                if not was_locked and len(dq) >= limit:
                    newly_locked.append(scope)
        return newly_locked

    def record_success(self, ip: str, username: str) -> None:
        """A correct login clears that username's failure history so a legitimate
        user is never punished. The IP bucket is left to decay by time, so one
        valid credential can't reset the coarse IP limiter for an attacker
        interleaving a known-good login."""
        with self._lock:
            self._fail.pop(("user", username or ""), None)

    def reset(self) -> None:
        with self._lock:
            self._fail.clear()
