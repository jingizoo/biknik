"""#426 round-5 review finding 1: the migration 055 dirty-upgrade guard
printed the raw push token it exists to protect.

``find_duplicate_device_tokens``/``assert_no_duplicate_device_tokens``
(``store/integrity_checks.py``) used to return and then interpolate the
literal ``(recipient_ref, token)`` VALUE into ``MigrationDataError`` —
planting two pre-055 rows with a real-shaped token and booting made startup
fail with a traceback containing that exact token, i.e. a live push
credential copied verbatim into whatever captures a deployment/startup
log. The diagnostic also gave an operator nothing bounded to act on (no
row ids).

THE FIX: the row ids alone are both sufficient to repair (look the id up,
deactivate or delete it) and safe to disclose, so the exception now names
ONLY the offending row ids — never the recipient_ref/token value, and no
fingerprint or other reversible encoding of it either, because the ids
already suffice. ``find_duplicate_device_tokens`` still returns the
concrete value alongside the ids (this module's own tests need it, to
prove detection itself is still correct) — the value simply never reaches
any exception, log line, or other captured output. See both functions'
docstrings and migration 055's own top-of-file comment for the same
reasoning restated at each boundary.

Covers, on SQLite + PostgreSQL:

* :class:`PreMigration055ValidationTest` — in-process (SQLite): detection
  still finds the exact duplicate pair and its row ids; the raised message
  names the row ids and never the value; distinct/NULL-bearing pairs are
  still correctly never flagged (migration 055's partial index excludes
  NULLs, matching every sibling check in this module).
* :class:`Migration055UpgradeTest` — the review's own exact sequence, via
  ``migrate()`` (the SAME function ``_PRE_MIGRATION_CHECKS`` and every real
  app boot calls): downgrade past 055, plant two rows sharing a pair whose
  recipient_ref and token are DISTINCT sentinels, assert the abort's
  message contains the row ids and neither sentinel, assert no partial
  state (no ledger row, no index — a third duplicate for the same pair
  still inserts), repair down to one row, rerun the same real entry point
  to a durable success, then prove the now-live index rejects a later
  duplicate as the SAME stable ``IntegrityConflictError`` every other
  DB-enforced invariant in this module already produces (never a raw
  driver error, which — on PostgreSQL especially — could itself embed the
  conflicting values in its own DETAIL text; db_errors.translate_db_exception
  already keeps that generic, see its module docstring).
* :class:`SubprocessRealBootDisclosureTest` — the review's own reproduction
  shape run as a genuine separate process (SQLite always; PostgreSQL when
  ``TEST_DATABASE_URL`` is set), so what gets scanned for the sentinels is
  Python's own default excepthook output on REAL stdout/stderr — exactly
  what a deployment/startup log would capture — not just an in-process
  exception object's ``str()``, which :class:`Migration055UpgradeTest`
  already covers directly.
"""

import os
import subprocess
import sys
import tempfile
import unittest

from helpers import BACKEND  # noqa: F401  (ensures sys.path is set up)

from hockey_scheduler.domain.errors import IntegrityConflictError
from hockey_scheduler.domain.setup_models import DeviceToken
from hockey_scheduler.store import SqlStore
from hockey_scheduler.store.integrity_checks import (
    MigrationDataError,
    assert_no_duplicate_device_tokens,
    find_duplicate_device_tokens,
)
from hockey_scheduler.store.sql_store import migrate

_VERSION = "055_device_token_unique_key"
_INDEX = "ux_device_tokens_recipient_token"

# Distinct on purpose (review's own instruction): a fix that redacted only
# ONE of the two fields would still pass a test that reused one sentinel
# for both.
SENT_RECIPIENT = "official:o1-dup-disclosure-426r5"
SENT_TOKEN = "secret-push-token-dup-disclosure-426r5"  # pragma: allowlist secret
# Shaped like a REAL ``SqlStore.next_id("devtok")`` output (#426 round-6
# review finding 1 requires row ids to pass a strict ``devtok_<n>`` grammar
# before they are named directly in the diagnostic — see
# ``integrity_checks._DEVICE_TOKEN_ROW_ID_RE``). Large, arbitrary-looking
# counter values so they can never collide with an id this test's own
# fresh, isolated store would organically allocate via ``next_id`` (none of
# these tests call the real upsert path, but the margin costs nothing).
DT1_ID, DT2_ID = "devtok_94261001", "devtok_94261002"

# Run as a genuine separate process (see SubprocessRealBootDisclosureTest):
# the exact real entry point every app boot calls, letting a raised
# exception propagate UNCAUGHT so Python's own default excepthook prints
# it to real stderr, never suppressed here.
_BOOT_SNIPPET = (
    "import sys\n"
    "import helpers  # noqa: F401\n"
    "from hockey_scheduler.store import SqlStore\n"
    "SqlStore(sys.argv[1])\n"
    "print('MIGRATION SUCCEEDED (no MigrationDataError)')\n"
)


def _sql_backends():
    """(label, url) for each SQL backend available here (Postgres in CI)."""
    backends = [("sqlite", ":memory:")]
    url = os.environ.get("TEST_DATABASE_URL")
    if url:
        backends.append(("postgres", url))
    return backends


def _fresh(url):
    store = SqlStore(url)
    if url != ":memory:":
        store.reset_schema()  # isolate on a shared DB
    return store


def _restore(store, url):
    """Leave a shared PostgreSQL database CONSTRUCTABLE for the next test —
    same reasoning as test_ice_slot_time_unique.py's identical helper."""
    try:
        if url != ":memory:":
            store.reset_schema()
    finally:
        store.close()


def _downgrade_055(store):
    """Simulate a pre-055 database: drop the index and un-record the migration."""
    with store.transaction():
        cur = store.conn.cursor()
        cur.execute(f"DROP INDEX IF EXISTS {_INDEX}")
        cur.execute(store.dialect.sql(
            "DELETE FROM schema_migrations WHERE version = ?"), (_VERSION,))


def _token(tid, recipient=SENT_RECIPIENT, token=SENT_TOKEN, provider="fcm"):
    return DeviceToken(id=tid, recipient_ref=recipient, provider=provider,
                       token=token)


def _bare_cleanup_dirty_postgres(url):
    """After SubprocessRealBootDisclosureTest's PostgreSQL case deliberately
    leaves the shared database mid-downgrade WITH the duplicate rows still
    present (that is what proves the abort — see the class docstring),
    ``SqlStore(url)`` can't be constructed to clean up: its own constructor
    would immediately re-hit the very guard under test. Connect beneath
    that layer — ``store.db.connect`` alone does not call ``migrate()`` —
    just long enough to delete the duplicates, so the standard
    ``_fresh()``/``_restore()`` dance works normally for whichever test
    opens this shared database next."""
    from hockey_scheduler.store.db import connect
    conn, dialect, _ = connect(url)
    try:
        cur = conn.cursor()
        cur.execute(dialect.sql(
            "DELETE FROM device_tokens WHERE id IN (?, ?)"), (DT1_ID, DT2_ID))
    finally:
        conn.close()


class PreMigration055ValidationTest(unittest.TestCase):
    """Detection is still correct, and the report never carries the value
    (SQLite)."""

    def test_duplicate_pair_detected_row_ids_named_value_withheld(self):
        store = SqlStore(":memory:")
        try:
            _downgrade_055(store)
            with store.transaction():
                store.add_device_token(_token(DT1_ID))
                store.add_device_token(_token(DT2_ID))
                store.add_device_token(_token(
                    "distinct", recipient="other:x", token="other-token"))

            dupes = find_duplicate_device_tokens(store.conn)
            self.assertEqual(len(dupes), 1)
            ref, tok, ids = dupes[0]
            # find_duplicate_device_tokens is the ONE function allowed to
            # hand back the value (its own docstring explains why: tests
            # like this one need to confirm DETECTION is correct) — the
            # boundary under test is assert_no_duplicate_device_tokens,
            # checked below.
            self.assertEqual((ref, tok), (SENT_RECIPIENT, SENT_TOKEN))
            self.assertEqual(ids, sorted([DT1_ID, DT2_ID]))

            with self.assertRaises(MigrationDataError) as ctx:
                assert_no_duplicate_device_tokens(store.conn)
            msg = str(ctx.exception)
            self.assertIn(DT1_ID, msg)
            self.assertIn(DT2_ID, msg)
            self.assertNotIn(SENT_RECIPIENT, msg)
            self.assertNotIn(SENT_TOKEN, msg)
        finally:
            store.close()

    def test_distinct_and_null_pairs_not_flagged(self):
        store = SqlStore(":memory:")
        try:
            _downgrade_055(store)
            with store.transaction():
                store.add_device_token(_token("a", recipient="official:o1",
                                              token="tok-a"))
                store.add_device_token(_token("b", recipient="official:o2",
                                              token="tok-b"))
                # NULL recipient_ref/token: the partial index keeps these
                # distinct, so repeats must NOT be reported.
                store.add_device_token(DeviceToken(
                    id="n1", recipient_ref=None, provider="fcm", token=None))
                store.add_device_token(DeviceToken(
                    id="n2", recipient_ref=None, provider="fcm", token=None))
            self.assertEqual(find_duplicate_device_tokens(store.conn), [])
            assert_no_duplicate_device_tokens(store.conn)  # does not raise
        finally:
            store.close()


class Migration055UpgradeTest(unittest.TestCase):
    """The review's own exact sequence via the real entry point: abort with
    no partial state, repair, durable success, index then enforces."""

    def test_abort_then_repair_succeeds_and_index_enforces(self):
        for label, url in _sql_backends():
            store = _fresh(url)
            try:
                _downgrade_055(store)
                with store.transaction():
                    store.add_device_token(_token(DT1_ID))
                    store.add_device_token(_token(DT2_ID))

                # 1) The REAL entry point (migrate(), exactly what every
                # app boot and _PRE_MIGRATION_CHECKS call) aborts, and its
                # message names the row ids but never the sentinel value.
                with self.assertRaises(MigrationDataError, msg=label) as ctx:
                    migrate(store.conn, store.dialect)
                msg = str(ctx.exception)
                self.assertIn(DT1_ID, msg, label)
                self.assertIn(DT2_ID, msg, label)
                self.assertNotIn(SENT_RECIPIENT, msg, label)
                self.assertNotIn(SENT_TOKEN, msg, label)

                # 2) No partial state: no ledger row, and the constraint
                # the aborted DDL would have added does not exist yet — a
                # THIRD duplicate for the SAME pair still inserts (mirrors
                # test_ice_slot_time_unique.py's identical proof shape).
                self.assertNotIn(
                    _VERSION, store.migration_status()["applied"], label)
                with store.transaction():
                    store.add_device_token(_token("dtdup-3-proof"))
                self.assertIsNotNone(
                    store.get_device_token("dtdup-3-proof"), label)

                # 3) Repair: an operator armed with ONLY the row ids from
                # the diagnostic (dt1/dt2 — "dtdup-3-proof" was step 2's own
                # doing, not the operator's, and is cleaned up here too)
                # deletes the extras, keeping exactly one row for the pair.
                with store.transaction():
                    cur = store.conn.cursor()
                    cur.execute(store.dialect.sql(
                        "DELETE FROM device_tokens WHERE id IN (?, ?)"),
                        (DT2_ID, "dtdup-3-proof"))

                # 4) Rerun the SAME real entry point: now a durable success.
                migrate(store.conn, store.dialect)
                self.assertIn(
                    _VERSION, store.migration_status()["applied"], label)

                # 5) The index now enforces: a LATER duplicate is the same
                # stable, value-free IntegrityConflictError every other
                # DB-enforced invariant in this module already produces —
                # never a raw driver error (whose own DETAIL text could
                # itself embed the conflicting values on PostgreSQL).
                with self.assertRaises(
                        IntegrityConflictError, msg=label) as ctx2:
                    with store.transaction():
                        store.add_device_token(_token("dtdup-new"))
                self.assertEqual(
                    ctx2.exception.details["reason"], "unique_violation",
                    label)
                self.assertNotIn(SENT_TOKEN, str(ctx2.exception), label)
            finally:
                _restore(store, url)


class SubprocessRealBootDisclosureTest(unittest.TestCase):
    """The review's own repro shape, run as a genuine separate process, so
    what gets scanned is Python's real stdout/stderr — exactly what a
    deployment/startup log captures — never just an in-process exception's
    str() (already covered directly above)."""

    def _boot(self, url):
        tests_dir = os.path.dirname(os.path.abspath(__file__))
        return subprocess.run(
            [sys.executable, "-c", _BOOT_SNIPPET, url],
            capture_output=True, text=True, cwd=tests_dir, timeout=60)

    def _assert_clean_abort(self, proc, label):
        combined = proc.stdout + proc.stderr
        self.assertNotEqual(proc.returncode, 0, f"{label}:\n{combined}")
        self.assertIn("MigrationDataError", combined, label)
        self.assertIn(
            "Cannot enforce one DeviceToken per (recipient_ref, token)",
            combined, label)
        self.assertIn(DT1_ID, combined, label)
        self.assertIn(DT2_ID, combined, label)
        self.assertNotIn(SENT_RECIPIENT, combined, label)
        self.assertNotIn(SENT_TOKEN, combined, label)
        # unittest's own module import machinery must actually have run
        # migrate() and hit the guard, not silently no-op'd.
        self.assertIn(
            "hockey_scheduler.store.integrity_checks.MigrationDataError",
            combined, label)

    def test_sqlite_real_subprocess_boot_never_prints_the_secret(self):
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        try:
            store = SqlStore(path)  # migrates fully to HEAD first (clean)
            _downgrade_055(store)
            with store.transaction():
                store.add_device_token(_token(DT1_ID))
                store.add_device_token(_token(DT2_ID))
            store.close()  # release the file before the child opens it

            proc = self._boot(path)
            self._assert_clean_abort(proc, "sqlite")
        finally:
            os.remove(path)

    @unittest.skipUnless(os.environ.get("TEST_DATABASE_URL"),
                         "PostgreSQL suite only (TEST_DATABASE_URL not set)")
    def test_postgres_real_subprocess_boot_never_prints_the_secret(self):
        url = os.environ["TEST_DATABASE_URL"]
        store = _fresh(url)
        try:
            _downgrade_055(store)
            with store.transaction():
                store.add_device_token(_token(DT1_ID))
                store.add_device_token(_token(DT2_ID))
            store.close()

            proc = self._boot(url)
            self._assert_clean_abort(proc, "postgres")
        finally:
            _bare_cleanup_dirty_postgres(url)
            _restore(_fresh(url), url)


if __name__ == "__main__":
    unittest.main()
