"""Chronological store reads return rows in the order they were written (#451).

Ids are ``{prefix}_{n}`` TEXT minted from a monotonic counter, so ordering by
the bare id is lexicographic: ``audit_10`` sorted before ``audit_9`` and a
game's eleventh audit row was returned ahead of its second. InMemoryStore
returns insertion order, which is why this stayed invisible in most suites.

Each read below is written with twelve rows so the counter crosses the
``_9`` -> ``_10`` boundary, and every row shares one timestamp the way a
pinned clock produces them, so nothing but the id can carry the order.
Runs on InMemoryStore, SqlStore on a SQLite file, and SqlStore on PostgreSQL
when ``TEST_DATABASE_URL`` is set.
"""

import os
import tempfile
import unittest
from datetime import datetime, timezone

from helpers import BACKEND, fresh_sql_store  # noqa: F401

from hockey_scheduler.domain.enums import AuditAction, NotificationType
from hockey_scheduler.domain.models import AuditLog, NotificationEvent
from hockey_scheduler.domain.setup_models import (
    Notification,
    NotificationAudience,
    NotificationChannel,
    NotificationDelivery,
    NotificationKind,
    SetupAuditLog,
)
from hockey_scheduler.store import InMemoryStore, SqlStore

NOW = datetime(2026, 2, 1, 18, 0, tzinfo=timezone.utc)
ROWS = 12  # crosses the _9 -> _10 boundary where lexicographic order breaks
GAME = "game_order"
FEED = "notif_feed_order"


class _ChronologicalReadContract:
    """Shared assertions; subclasses provide ``self.store``."""

    def _write(self, prefix, build, add):
        written = []
        for _ in range(ROWS):
            row_id = self.store.next_id(prefix)
            add(build(row_id))
            written.append(row_id)
        return written

    def assertWriteOrder(self, written, rows):
        self.assertEqual([r.id for r in rows], written)

    def test_audit_for_game_returns_write_order(self):
        written = self._write(
            "audit",
            lambda i: AuditLog(id=i, game_id=GAME,
                               action=AuditAction.ROSTER_SELECTED, at=NOW),
            self.store.add_audit)
        self.assertWriteOrder(written, self.store.audit_for_game(GAME))

    def test_notifications_for_game_returns_write_order(self):
        written = self._write(
            "notif",
            lambda i: NotificationEvent(
                id=i, game_id=GAME, type=NotificationType.PLAYER_BACKED_OUT,
                audience="coach", message="m", at=NOW),
            self.store.add_notification)
        self.assertWriteOrder(written, self.store.notifications_for_game(GAME))

    def test_all_setup_audit_returns_write_order(self):
        written = self._write(
            "setupaudit",
            lambda i: SetupAuditLog(id=i, action="test_action",
                                    entity_type="team", entity_id="team_x",
                                    at=NOW),
            self.store.add_setup_audit)
        self.assertWriteOrder(written, self.store.all_setup_audit())

    def test_all_notifications_feed_returns_write_order(self):
        written = self._write(
            "notif_feed",
            lambda i: Notification(
                id=i, kind=NotificationKind.ASSIGNMENT_OFFERED,
                audience=NotificationAudience.PUBLIC, title="t",
                message="m", at=NOW),
            self.store.add_notification_feed)
        self.assertWriteOrder(written, self.store.all_notifications_feed())

    def test_deliveries_return_write_order(self):
        written = self._write(
            "notif_delivery",
            lambda i: NotificationDelivery(
                id=i, notification_id=FEED,
                channel=NotificationChannel.EMAIL),
            self.store.add_notification_delivery)
        self.assertWriteOrder(
            written, self.store.deliveries_for_notification(FEED))
        self.assertWriteOrder(
            written, self.store.all_notification_deliveries())


class MemoryChronologicalReadTest(_ChronologicalReadContract,
                                  unittest.TestCase):
    def setUp(self):
        self.store = InMemoryStore()


class SqliteChronologicalReadTest(_ChronologicalReadContract,
                                  unittest.TestCase):
    def setUp(self):
        fd, self._tmp = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.store = SqlStore(self._tmp)

    def tearDown(self):
        self.store.close()
        os.remove(self._tmp)


@unittest.skipUnless(os.environ.get("TEST_DATABASE_URL"),
                     "PostgreSQL suite only (TEST_DATABASE_URL not set)")
class PostgresChronologicalReadTest(_ChronologicalReadContract,
                                    unittest.TestCase):
    def setUp(self):
        self.store = fresh_sql_store(os.environ["TEST_DATABASE_URL"])

    def tearDown(self):
        self.store.close()


if __name__ == "__main__":
    unittest.main()
