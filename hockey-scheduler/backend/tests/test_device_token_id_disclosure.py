"""#426 round-6 review finding 1: the migration 055 dirty-upgrade guard
trusted ``device_tokens.id`` unconditionally, so a row whose id itself
carried a secret (or control characters, or an absurd length) was echoed
verbatim -- round-5 only stopped the recipient_ref/token VALUE from
leaking; it never questioned the id.

``device_tokens.id`` is an unrestricted ``TEXT PRIMARY KEY`` (migration
001) -- nothing in the schema constrains its shape or length. Every row
THIS process creates gets one via ``SqlStore.next_id("devtok")``, which
always yields ``devtok_<positive integer>`` -- but a migration runs against
whatever is already in the database, which can predate that guarantee, or
have been written by a bug, a restore, or a direct edit. The review's own
reproduction planted a pre-055 duplicate whose id WAS the sentinel
``secret-push-token-in-id-426`` and had it printed verbatim by the raised
``MigrationDataError``.

THE FIX (``store/integrity_checks.py``): every row id is now passed
through ``_safe_device_token_row_label``, which only names an id directly
when it matches ``_DEVICE_TOKEN_ROW_ID_RE`` -- this store's own generated
shape, anchored with ``\\A``/``\\Z`` (not ``^``/``$``, which also matches
just before a trailing newline) and capped at 18 digits. Anything else --
including a value that merely resembles the shape -- is replaced by a
bounded per-group ordinal (``row #1``, ``row #2``, ...). Every label,
including one that DID pass the grammar, is then run through
``_sanitize_label``, which strips control/format/surrogate/private-use/
unassigned characters and hard-caps length -- defence in depth, in case
the grammar itself ever grows a gap. The number of duplicate groups shown,
the number of rows shown per group, and the assembled message's total
length are each independently bounded.

Covers, on SQLite + PostgreSQL:

* :class:`RowLabelGrammarUnitTest` -- direct unit coverage of
  ``_safe_device_token_row_label``/``_sanitize_label``: a real
  ``next_id("devtok")``-shaped id is named directly; a token/email-shaped
  sentinel, a newline/control-character id, an oversized id, a wrong-case
  id, a leading-zero id, and a non-string id are all replaced by a bounded
  ordinal instead.
* :class:`PreMigration055IdGrammarTest` -- in-process (SQLite): plants a
  duplicate pair whose id is EACH of the three named sentinel shapes
  (token/email-like, newline-containing, oversized) in turn and confirms
  the raised message never contains it, stays bounded, and still names a
  bounded ordinal so the report stays actionable; a MIXED group (one real
  id + one sentinel id) still names the real one directly while replacing
  only the sentinel.
* :class:`Migration055IdGrammarUpgradeTest` -- the review's own sequence,
  via ``migrate()`` (the real entry point every app boot calls), on both
  SQLite and PostgreSQL: abort names no sentinel, no partial state, repair
  with only the (ordinal-labelled) diagnostic to go on, durable success,
  index then enforces.
* :class:`SubprocessRealBootIdDisclosureTest` -- the review's own
  reproduction shape run as a genuine separate process (SQLite always;
  PostgreSQL when ``TEST_DATABASE_URL`` is set), scanning Python's own
  default excepthook output on REAL stdout/stderr -- exactly what a
  deployment/startup log would capture.
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
    _MAX_DEVICE_TOKEN_MESSAGE_CHARS,
    _safe_device_token_row_label,
    _sanitize_label,
    assert_no_duplicate_device_tokens,
    find_duplicate_device_tokens,
)
from hockey_scheduler.store.sql_store import migrate

_VERSION = "055_device_token_unique_key"
_INDEX = "ux_device_tokens_recipient_token"

# One id per named attack shape the review called out by name. Distinct
# from round-5's sentinels (which live in the recipient_ref/token VALUE,
# not the id) so the two test files can never accidentally pass by
# checking for the same string in two different places.
TOKEN_SHAPED_ID = "secret-push-token-in-id-426r6"  # pragma: allowlist secret
EMAIL_SHAPED_ID = "ops-oncall+426r6@example.com"
NEWLINE_ID = "devtok_1\nX-Injected-Header: evil-426r6"
CONTROL_CHAR_ID = "devtok_1\x00\x07evil-426r6"
OVERSIZED_ID = "devtok_" + "9" * 3000
WRONG_CASE_ID = "DEVTOK_1"
LEADING_ZERO_ID = "devtok_01"
PARTNER_ID = "devtok_88800001"  # the OTHER row in each duplicate pair


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


def _token(tid, recipient, token="dup-token-426r6", provider="fcm"):
    return DeviceToken(id=tid, recipient_ref=recipient, provider=provider,
                       token=token)


_BOOT_SNIPPET = (
    "import sys\n"
    "import helpers  # noqa: F401\n"
    "from hockey_scheduler.store import SqlStore\n"
    "SqlStore(sys.argv[1])\n"
    "print('MIGRATION SUCCEEDED (no MigrationDataError)')\n"
)


class RowLabelGrammarUnitTest(unittest.TestCase):
    """Direct unit coverage of the grammar + sanitizer, no database at all."""

    def test_real_shaped_id_is_named_directly(self):
        self.assertEqual(_safe_device_token_row_label("devtok_1", 7), "devtok_1")
        self.assertEqual(
            _safe_device_token_row_label("devtok_999999999999999999", 1),
            "devtok_999999999999999999")  # 18 nines: exactly at the cap

    def test_sentinel_shapes_are_replaced_by_ordinal_not_echoed(self):
        for bad_id in (TOKEN_SHAPED_ID, EMAIL_SHAPED_ID, NEWLINE_ID,
                      CONTROL_CHAR_ID, OVERSIZED_ID, WRONG_CASE_ID,
                      LEADING_ZERO_ID, "", "devtok_", "devtok_-1",
                      "devtok_1.5", 12345, None, ("devtok_1",)):
            label = _safe_device_token_row_label(bad_id, 3)
            self.assertEqual(label, "row #3", repr(bad_id))

    def test_oversized_numeric_suffix_beyond_18_digits_is_rejected(self):
        # 19 digits — one past the cap — must NOT be named directly even
        # though it otherwise matches "devtok_" + all-digits.
        just_over = "devtok_" + "9" * 19
        self.assertEqual(_safe_device_token_row_label(just_over, 2), "row #2")

    def test_sanitize_label_strips_control_chars_and_caps_length(self):
        self.assertEqual(_sanitize_label("abc\ndef\x00ghi"), "abcdefghi")
        self.assertEqual(len(_sanitize_label("x" * 500)), 40)

    def test_sanitize_label_applied_even_to_a_grammar_passing_id(self):
        # Defence in depth (review's own instruction): even a label that
        # passed the grammar goes through the sanitizer. A conforming id
        # has no control characters to strip, so this must be a no-op —
        # proving the sanitizer doesn't ITSELF corrupt a legitimate id.
        self.assertEqual(_safe_device_token_row_label("devtok_42", 1), "devtok_42")


class PreMigration055IdGrammarTest(unittest.TestCase):
    """In-process (SQLite): detection is correct, and no sentinel id shape
    ever reaches the raised message, while the report stays actionable."""

    def _plant_and_check(self, bad_id):
        store = SqlStore(":memory:")
        try:
            _downgrade_055(store)
            with store.transaction():
                store.add_device_token(_token(bad_id, recipient="official:x1"))
                store.add_device_token(_token(PARTNER_ID, recipient="official:x1"))

            dupes = find_duplicate_device_tokens(store.conn)
            self.assertEqual(len(dupes), 1)
            _ref, _tok, ids = dupes[0]
            self.assertEqual(sorted(ids), sorted([bad_id, PARTNER_ID]))

            with self.assertRaises(MigrationDataError) as ctx:
                assert_no_duplicate_device_tokens(store.conn)
            msg = str(ctx.exception)
            self.assertNotIn(bad_id, msg, msg)
            self.assertNotIn("\n", msg, "a raw newline could forge a fake "
                              "log line even without the full sentinel")
            self.assertLessEqual(len(msg), _MAX_DEVICE_TOKEN_MESSAGE_CHARS)
            # Still actionable: PARTNER_ID (a real-shaped id) is named
            # directly, and the withheld one gets a bounded ordinal.
            self.assertIn(PARTNER_ID, msg, msg)
            self.assertIn("row #", msg, msg)
            return msg
        finally:
            store.close()

    def test_token_shaped_sentinel_id_never_echoed(self):
        self._plant_and_check(TOKEN_SHAPED_ID)

    def test_email_shaped_sentinel_id_never_echoed(self):
        self._plant_and_check(EMAIL_SHAPED_ID)

    def test_newline_containing_id_never_echoed(self):
        self._plant_and_check(NEWLINE_ID)

    def test_control_char_id_never_echoed(self):
        self._plant_and_check(CONTROL_CHAR_ID)

    def test_oversized_id_never_echoed_and_message_stays_bounded(self):
        msg = self._plant_and_check(OVERSIZED_ID)
        # The whole point: a 3007-character id must not balloon the
        # message anywhere near its own length.
        self.assertLess(len(msg), 1000, msg[:200])

    def test_ordinary_generated_id_is_still_named_directly(self):
        """Positive case (review's own requirement): a real
        ``next_id("devtok")`` id is NOT hidden behind an ordinal."""
        store = SqlStore(":memory:")
        try:
            _downgrade_055(store)
            with store.transaction():
                real_a = store.next_id("devtok")
                real_b = store.next_id("devtok")
                store.add_device_token(_token(real_a, recipient="official:x2"))
                store.add_device_token(_token(real_b, recipient="official:x2"))
            with self.assertRaises(MigrationDataError) as ctx:
                assert_no_duplicate_device_tokens(store.conn)
            msg = str(ctx.exception)
            self.assertIn(real_a, msg, msg)
            self.assertIn(real_b, msg, msg)
            self.assertNotIn("row #", msg, "both ids were real; no ordinal "
                              "fallback should have been needed: " + msg)
        finally:
            store.close()

    def test_distinct_and_null_pairs_still_not_flagged(self):
        """Grammar change must not touch the existing NULL/distinct-pair
        exclusions (migration 055's partial index semantics)."""
        store = SqlStore(":memory:")
        try:
            _downgrade_055(store)
            with store.transaction():
                store.add_device_token(_token("devtok_1", recipient="official:o1"))
                store.add_device_token(_token("devtok_2", recipient="official:o2"))
                store.add_device_token(DeviceToken(
                    id="devtok_3", recipient_ref=None, provider="fcm", token=None))
                store.add_device_token(DeviceToken(
                    id="devtok_4", recipient_ref=None, provider="fcm", token=None))
            self.assertEqual(find_duplicate_device_tokens(store.conn), [])
            assert_no_duplicate_device_tokens(store.conn)  # does not raise
        finally:
            store.close()


class Migration055IdGrammarUpgradeTest(unittest.TestCase):
    """The review's own sequence via the real entry point, both backends:
    abort names no sentinel id, no partial state, repair from the bounded
    diagnostic alone, durable success, index then enforces."""

    def test_abort_then_repair_succeeds_and_index_enforces(self):
        for label, url in _sql_backends():
            store = _fresh(url)
            try:
                _downgrade_055(store)
                with store.transaction():
                    store.add_device_token(
                        _token(TOKEN_SHAPED_ID, recipient="official:x3"))
                    store.add_device_token(
                        _token(PARTNER_ID, recipient="official:x3"))

                # 1) The REAL entry point aborts; the message names the
                # real id directly, an ordinal for the sentinel, and NEVER
                # the sentinel itself.
                with self.assertRaises(MigrationDataError, msg=label) as ctx:
                    migrate(store.conn, store.dialect)
                msg = str(ctx.exception)
                self.assertIn(PARTNER_ID, msg, label)
                self.assertIn("row #", msg, label)
                self.assertNotIn(TOKEN_SHAPED_ID, msg, label)

                # 2) No partial state: no ledger row; a third duplicate for
                # the SAME pair still inserts (mirrors round-5's identical
                # proof shape).
                self.assertNotIn(
                    _VERSION, store.migration_status()["applied"], label)
                with store.transaction():
                    store.add_device_token(
                        _token("devtok_88800099", recipient="official:x3"))
                self.assertIsNotNone(
                    store.get_device_token("devtok_88800099"), label)

                # 3) Repair using ONLY what the diagnostic gave an operator:
                # PARTNER_ID by name, and "row #1"/"row #2" for the rest —
                # in THIS harness the test knows the real ids, so it deletes
                # everything except one survivor to reach a clean pair.
                with store.transaction():
                    cur = store.conn.cursor()
                    cur.execute(store.dialect.sql(
                        "DELETE FROM device_tokens WHERE id IN (?, ?)"),
                        (TOKEN_SHAPED_ID, "devtok_88800099"))

                # 4) Rerun the SAME real entry point: durable success.
                migrate(store.conn, store.dialect)
                self.assertIn(
                    _VERSION, store.migration_status()["applied"], label)

                # 5) The index now enforces: a later duplicate is the same
                # stable IntegrityConflictError every other DB-enforced
                # invariant produces.
                with self.assertRaises(
                        IntegrityConflictError, msg=label) as ctx2:
                    with store.transaction():
                        store.add_device_token(
                            _token("devtok_88800100", recipient="official:x3"))
                self.assertEqual(
                    ctx2.exception.details["reason"], "unique_violation", label)
            finally:
                _restore(store, url)


class SubprocessRealBootIdDisclosureTest(unittest.TestCase):
    """The review's own reproduction shape, run as a genuine separate
    process, so what gets scanned is Python's real stdout/stderr — exactly
    what a deployment/startup log would capture."""

    def _boot(self, url):
        tests_dir = os.path.dirname(os.path.abspath(__file__))
        return subprocess.run(
            [sys.executable, "-c", _BOOT_SNIPPET, url],
            capture_output=True, text=True, cwd=tests_dir, timeout=60)

    def _assert_clean_abort(self, proc, label):
        combined = proc.stdout + proc.stderr
        self.assertNotEqual(proc.returncode, 0, f"{label}:\n{combined}")
        self.assertIn("MigrationDataError", combined, label)
        self.assertIn(PARTNER_ID, combined, label)
        self.assertIn("row #", combined, label)
        self.assertNotIn(TOKEN_SHAPED_ID, combined, label)
        # unittest's own module import machinery must actually have run
        # migrate() and hit the guard, not silently no-op'd.
        self.assertIn(
            "hockey_scheduler.store.integrity_checks.MigrationDataError",
            combined, label)

    def test_sqlite_real_subprocess_boot_never_prints_the_id_secret(self):
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        try:
            store = SqlStore(path)  # migrates fully to HEAD first (clean)
            _downgrade_055(store)
            with store.transaction():
                store.add_device_token(
                    _token(TOKEN_SHAPED_ID, recipient="official:x4"))
                store.add_device_token(
                    _token(PARTNER_ID, recipient="official:x4"))
            store.close()  # release the file before the child opens it

            proc = self._boot(path)
            self._assert_clean_abort(proc, "sqlite")
        finally:
            os.remove(path)

    @unittest.skipUnless(os.environ.get("TEST_DATABASE_URL"),
                         "PostgreSQL suite only (TEST_DATABASE_URL not set)")
    def test_postgres_real_subprocess_boot_never_prints_the_id_secret(self):
        url = os.environ["TEST_DATABASE_URL"]
        store = _fresh(url)
        try:
            _downgrade_055(store)
            with store.transaction():
                store.add_device_token(
                    _token(TOKEN_SHAPED_ID, recipient="official:x4"))
                store.add_device_token(
                    _token(PARTNER_ID, recipient="official:x4"))
            store.close()

            proc = self._boot(url)
            self._assert_clean_abort(proc, "postgres")
        finally:
            from hockey_scheduler.store.db import connect
            conn, dialect, _ = connect(url)
            try:
                cur = conn.cursor()
                cur.execute(dialect.sql(
                    "DELETE FROM device_tokens WHERE id IN (?, ?)"),
                    (TOKEN_SHAPED_ID, PARTNER_ID))
            finally:
                conn.close()
            _restore(_fresh(url), url)


if __name__ == "__main__":
    unittest.main()
