"""TEST HARNESS (#426 round-7 review): run ``migrate()`` past a no-op
migration-055 atomic check, as a genuinely UNCAUGHT top-level script -- so
Python's own default excepthook prints whatever the current code produces,
exactly as a real boot-time crash would. Mirrors ``_cross_replica_server.py``
and ``run_parallel.py``'s own "run the real thing as an independent OS
process" convention.

Deliberately named WITHOUT a ``test_`` prefix: ``tests/run_parallel.py``'s
own discovery is ``glob.glob("test_*.py")`` (see
``.github/workflows/hockey-scheduler-ci.yml``), so this file is never
collected or run directly by the suite -- only spawned via
``subprocess.run`` by ``test_device_token_migration_race.py``'s
``AtomicCheckDdlUncaughtSubprocessTest``.

Seeds a real duplicate ``(recipient_ref, token)`` row pair directly
(bypassing every application layer, exactly like the review's own
reproduction and this module's sibling deterministic unit test,
``AtomicCheckDdlTranslationUnitTest``), monkeypatches migration 055's
registered atomic check to a no-op so its own DDL is reached
unconditionally (forcing the belt-and-suspenders translator in
``_apply_migration`` to be what actually stops the raw driver exception,
not the lock), then calls ``migrate()`` with NO try/except anywhere in
this file. If the translator ever lets the raw driver exception (or a
live, walkable reference to it via ``__cause__``/``__context__``) escape,
Python's own default excepthook is what prints it here -- to real
stdout/stderr, not a test harness's own formatting, which is the exact
gap the review found: an in-process ``assertRaises`` can inspect
``str(exception)`` but never the real, uncaught startup rendering.

Usage:
    python3 _migration_atomic_check_uncaught_child.py <database_url> \\
        <sentinel_token> <sentinel_recipient>

Prints ``ABOUT_TO_MIGRATE`` right before the deliberately-uncaught call, and
(only if the bug this guards against has regressed and migration 055
somehow succeeds despite the forced duplicate) ``MIGRATE_SUCCEEDED_
UNEXPECTEDLY`` after it -- both plain markers the caller greps stdout for,
never the sentinel values themselves.
"""
import sys
from pathlib import Path

# Same convention as helpers.BACKEND: make hockey_scheduler importable
# regardless of this script's invocation cwd.
_BACKEND = Path(__file__).resolve().parents[1]
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from hockey_scheduler.store import SqlStore  # noqa: E402
from hockey_scheduler.store.sql_store import (  # noqa: E402
    _ATOMIC_PRE_MIGRATION_CHECKS,
    migrate,
)

_VERSION = "055_device_token_unique_key"
_INDEX = "ux_device_tokens_recipient_token"

if __name__ == "__main__":
    url, sentinel_token, sentinel_recipient = sys.argv[1], sys.argv[2], sys.argv[3]

    store = SqlStore(url)  # migrates to HEAD first (clean, real check passes)

    with store.transaction():
        cur = store.conn.cursor()
        cur.execute(f"DROP INDEX IF EXISTS {_INDEX}")
        cur.execute(store.dialect.sql(
            "DELETE FROM schema_migrations WHERE version = ?"), (_VERSION,))

    with store.transaction():
        cur = store.conn.cursor()
        for row_id in ("devtok_r7_uncaught_a", "devtok_r7_uncaught_b"):
            cur.execute(store.dialect.sql(
                "INSERT INTO device_tokens (id, recipient_ref, provider, "
                "token, label, active) VALUES (?, ?, ?, ?, ?, ?)"),
                (row_id, sentinel_recipient, "fcm", sentinel_token, None, 1))

    # Force the DDL path: bypass the real duplicate check with a no-op,
    # exactly like AtomicCheckDdlTranslationUnitTest -- see its docstring
    # in test_device_token_migration_race.py.
    _ATOMIC_PRE_MIGRATION_CHECKS[_VERSION] = (lambda _conn: None, "device_tokens")

    print("ABOUT_TO_MIGRATE", flush=True)
    migrate(store.conn, store.dialect)  # deliberately UNCAUGHT -- see module docstring
    print("MIGRATE_SUCCEEDED_UNEXPECTEDLY", flush=True)  # should never print
