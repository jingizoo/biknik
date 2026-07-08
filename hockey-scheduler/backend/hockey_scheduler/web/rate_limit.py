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
