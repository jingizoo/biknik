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
# Bounds (safe-by-config): every env-tunable knob is clamped to a fixed
# ``[lo, hi]`` range, so a misconfigured value can only move within safe limits —
# it can neither disable the protection (a huge ``max_failures`` is capped) nor
# lock users out effectively forever (a huge ``window`` is capped). The clock is
# injectable for deterministic tests.
_LOGIN_WINDOW_DEFAULT = 900.0        # 15 minutes
_LOGIN_WINDOW_MIN = 30.0
_LOGIN_WINDOW_MAX = 86_400.0         # cap a lock at 24h so a misconfig can't wedge
_LOGIN_USER_MAX_DEFAULT = 5          # failed attempts per username per window
_LOGIN_USER_MAX_MIN = 1
_LOGIN_USER_MAX_CEILING = 100        # above this the per-username throttle is a no-op
_LOGIN_IP_MAX_DEFAULT = 50           # failed attempts per source IP per window
_LOGIN_IP_MAX_MIN = 1
_LOGIN_IP_MAX_CEILING = 5_000        # coarse across usernames, but still bounded

# A caller who fails once and is never seen again would otherwise leave a dead
# ``(scope, key)`` deque parked in ``_fail`` forever (nothing revisits it once its
# failures age out), so an attacker spraying many usernames/IPs grows memory
# without bound. An amortized sweep every ``_LOGIN_SWEEP_EVERY`` mutating calls
# drops any bucket whose newest failure is older than the window (mirrors the
# anonymous ``RateLimiter`` sweep, self-review #267).
_LOGIN_SWEEP_EVERY = 500


def _env_float(name: str, default: float, lo: float, hi: float) -> float:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        return min(hi, max(lo, float(raw)))
    except ValueError:
        return default


def _env_int(name: str, default: int, lo: int, hi: int) -> int:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        return min(hi, max(lo, int(raw)))
    except ValueError:
        return default


class LoginThrottle:
    def __init__(self, clock=time.monotonic, window_seconds=None,
                 user_max=None, ip_max=None):
        self._clock = clock
        self._window = (window_seconds if window_seconds is not None
                        else _env_float("HS_LOGIN_WINDOW_SECONDS",
                                        _LOGIN_WINDOW_DEFAULT,
                                        _LOGIN_WINDOW_MIN, _LOGIN_WINDOW_MAX))
        self._user_max = (user_max if user_max is not None
                          else _env_int("HS_LOGIN_MAX_FAILURES",
                                        _LOGIN_USER_MAX_DEFAULT,
                                        _LOGIN_USER_MAX_MIN, _LOGIN_USER_MAX_CEILING))
        self._ip_max = (ip_max if ip_max is not None
                        else _env_int("HS_LOGIN_IP_MAX_FAILURES",
                                      _LOGIN_IP_MAX_DEFAULT,
                                      _LOGIN_IP_MAX_MIN, _LOGIN_IP_MAX_CEILING))
        self._fail = defaultdict(deque)   # (scope, key) -> deque[timestamps]
        # In-flight reservations per key. ``begin`` reserves a slot inside the
        # lock and counts it against the ceiling, so N concurrent attempts can't
        # all pass a check-then-record gap and collectively overshoot the limit
        # (self-review #267). Released by record_failure/record_success/cancel.
        self._pending = defaultdict(int)  # (scope, key) -> in-flight count
        self._lock = threading.Lock()
        self._calls_since_sweep = 0

    def _trim(self, dq, now) -> None:
        cutoff = now - self._window
        while dq and dq[0] < cutoff:
            dq.popleft()

    def _maybe_sweep_locked(self, now) -> None:
        """Drop buckets that have gone quiet — caller already holds ``_lock``."""
        self._calls_since_sweep += 1
        if self._calls_since_sweep < _LOGIN_SWEEP_EVERY:
            return
        self._calls_since_sweep = 0
        stale_cutoff = now - self._window
        dead = [k for k, dq in self._fail.items()
                if (not dq or dq[-1] < stale_cutoff) and not self._pending.get(k)]
        for k in dead:
            del self._fail[k]

    def _release_pending_locked(self, key) -> None:
        n = self._pending.get(key, 0)
        if n <= 1:
            self._pending.pop(key, None)
        else:
            self._pending[key] = n - 1

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
        (0.0 if allowed), based on recorded failures only. Considers BOTH the
        source-IP and normalized-username buckets and returns the larger
        remaining lock. Records nothing — a pure peek."""
        now = self._clock()
        with self._lock:
            return max(
                self._retry_after_locked(("ip", ip or "unknown"), self._ip_max, now),
                self._retry_after_locked(("user", username or ""), self._user_max, now))

    def begin(self, ip: str, username: str) -> float:
        """Atomically decide whether an attempt may proceed and, if so, RESERVE an
        in-flight slot against both buckets. Returns 0.0 when the caller may
        proceed (it MUST then call exactly one of record_failure/record_success/
        cancel to release the reservation), or the seconds to wait when locked
        (no slot reserved — nothing to release).

        Reserving inside the lock is what bounds concurrency: a bucket is locked
        once ``recorded failures + in-flight reservations`` reaches its ceiling,
        so simultaneous attempts can't slip through the window between a separate
        check and record (the overshoot ``retry_after``+``record_failure`` alone
        would allow). A lock from real failures reports its time-based wait; a
        lock from concurrent reservations alone reports a short momentary backoff.
        """
        now = self._clock()
        ip_key = ("ip", ip or "unknown")
        user_key = ("user", username or "")
        with self._lock:
            wait = 0.0
            for key, limit in ((ip_key, self._ip_max), (user_key, self._user_max)):
                dq = self._fail.get(key)
                if dq:
                    self._trim(dq, now)
                failures = len(dq) if dq else 0
                if failures + self._pending.get(key, 0) >= limit:
                    w = (dq[0] + self._window - now) if failures >= limit else 1.0
                    wait = max(wait, w)
            if wait > 0.0:
                return wait
            self._pending[ip_key] += 1
            self._pending[user_key] += 1
            self._maybe_sweep_locked(now)
            return 0.0

    def record_failure(self, ip: str, username: str):
        """Record one failed attempt against both buckets, releasing any in-flight
        reservation held for them. Returns the list of bucket scopes
        (``"ip"``/``"username"``) that JUST crossed into the locked state on this
        failure (empty if none) — the caller audits a lockout once, when it
        engages, rather than on every subsequent blocked request."""
        now = self._clock()
        newly_locked = []
        with self._lock:
            for scope, key, limit in (
                    ("ip", ("ip", ip or "unknown"), self._ip_max),
                    ("username", ("user", username or ""), self._user_max)):
                self._release_pending_locked(key)
                dq = self._fail[key]
                self._trim(dq, now)
                was_locked = len(dq) >= limit
                dq.append(now)
                if not was_locked and len(dq) >= limit:
                    newly_locked.append(scope)
            self._maybe_sweep_locked(now)
        return newly_locked

    def record_success(self, ip: str, username: str) -> None:
        """A correct login clears that username's failure history so a legitimate
        user is never punished, and releases the in-flight reservation on both
        buckets. The IP bucket's failure history is left to decay by time, so one
        valid credential can't reset the coarse IP limiter for an attacker
        interleaving a known-good login."""
        with self._lock:
            self._release_pending_locked(("ip", ip or "unknown"))
            self._release_pending_locked(("user", username or ""))
            self._fail.pop(("user", username or ""), None)

    def cancel(self, ip: str, username: str) -> None:
        """Release a reservation from ``begin`` without recording an outcome — used
        when the verify step raises before a pass/fail is known, so an error can't
        leak an in-flight slot."""
        with self._lock:
            self._release_pending_locked(("ip", ip or "unknown"))
            self._release_pending_locked(("user", username or ""))

    def reset(self) -> None:
        with self._lock:
            self._fail.clear()
            self._pending.clear()
            self._calls_since_sweep = 0
