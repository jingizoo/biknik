"""Migration 064's one-active-substitute-row database backstop (#287).

The service rejects a second active enrollment for the same ``(game, player)``
before writing, but two processes can race that check.  Migration 064 therefore
adds a partial unique index covering exactly ENROLLED/OFFERED rows, independent
of target team.  These tests exercise that last-line constraint through the
real store write sites, including their stable error translation and rollback.

The same contract runs on SQLite and, when ``TEST_DATABASE_URL`` is configured,
PostgreSQL.  PostgreSQL tests use UUID-namespaced rows and delete only those
exact ids afterward; they never reset or clear the shared test database.  The
pre-migration dirty-data simulation is intentionally SQLite ``:memory:`` only,
because it must temporarily drop the index and alter the migration ledger.
"""

import os
import unittest
import uuid
from datetime import datetime, timezone

from helpers import BACKEND  # noqa: F401  (ensures sys.path is set up)

from hockey_scheduler.domain import Position, SubstituteEnrollment, SubstituteStatus
from hockey_scheduler.domain.errors import IntegrityConflictError
from hockey_scheduler.store import InMemoryStore, SqlStore
from hockey_scheduler.store.integrity_checks import (
    MigrationDataError,
    assert_no_duplicate_active_substitute_players,
    find_duplicate_active_substitute_players,
)
from hockey_scheduler.store.sql_store import migrate
from hockey_scheduler.store.sql_store import _ATOMIC_PRE_MIGRATION_CHECKS


UTC = timezone.utc
_VERSION = "064_cross_team_substitute_provenance"
_INDEX = "ux_substitute_active_game_player"
_MESSAGE = "Player already has an active substitute enrollment for this game."
_ACTIVE = frozenset({SubstituteStatus.ENROLLED, SubstituteStatus.OFFERED})


def _sub(eid, *, game, player, team, status):
    return SubstituteEnrollment(
        id=eid,
        game_id=game,
        player_id=player,
        position=Position.DEFENSE,
        status=status,
        enrolled_at=datetime(2026, 9, 4, tzinfo=UTC),
        team_id=team,
        source_membership_id=f"membership-{eid}",
        source_team_id=f"source-{eid}",
    )


def _rows(store, ids):
    """Return this test's rows without relying on a single-row convenience API."""
    if not ids:
        return []
    placeholders = ", ".join("?" for _ in ids)
    cur = store.conn.cursor()
    cur.execute(store.dialect.sql(
        "SELECT id, game_id, player_id, team_id, status "
        f"FROM substitute_enrollments WHERE id IN ({placeholders}) ORDER BY id"),
        tuple(ids))
    return [dict(row) for row in cur.fetchall()]


def _delete_exact_rows(store, ids):
    """Remove only rows whose UUID-prefixed ids this test allocated."""
    if not ids:
        return
    with store.transaction():
        cur = store.conn.cursor()
        for row_id in ids:
            cur.execute(store.dialect.sql(
                "DELETE FROM substitute_enrollments WHERE id = ?"), (row_id,))


class _ActiveSubstituteConstraintContract:
    """Shared live-schema contract; concrete subclasses select the backend."""

    URL = None
    EXPECTED_BACKEND = None

    def setUp(self):
        self.store = SqlStore(self.URL)
        self.assertEqual(self.store.backend, self.EXPECTED_BACKEND)
        self.prefix = f"t063-{uuid.uuid4().hex}"
        self.owned_ids = []

    def tearDown(self):
        try:
            _delete_exact_rows(self.store, self.owned_ids)
        finally:
            self.store.close()

    def _id(self, suffix):
        row_id = f"{self.prefix}-{suffix}"
        self.owned_ids.append(row_id)
        return row_id

    def test_opposite_target_active_duplicate_is_stable_conflict_and_rolls_back(self):
        game = f"{self.prefix}-game"
        player = f"{self.prefix}-player"
        first_id = self._id("first")
        unrelated_id = self._id("unrelated")
        duplicate_id = self._id("duplicate")

        with self.store.transaction():
            self.store.add_substitute(_sub(
                first_id, game=game, player=player,
                team=f"{self.prefix}-team-4", status=SubstituteStatus.ENROLLED))

        with self.assertRaises(IntegrityConflictError) as caught:
            with self.store.transaction():
                # A successful write earlier in the same transaction must be
                # rolled back with the losing opposite-target enrollment.
                self.store.add_substitute(_sub(
                    unrelated_id, game=f"{self.prefix}-other-game", player=player,
                    team=f"{self.prefix}-team-4",
                    status=SubstituteStatus.ENROLLED))
                self.store.add_substitute(_sub(
                    duplicate_id, game=game, player=player,
                    team=f"{self.prefix}-team-5",
                    status=SubstituteStatus.OFFERED))

        error = caught.exception
        self.assertEqual(error.code, "conflict")
        self.assertEqual(str(error), _MESSAGE)
        self.assertEqual(error.details, {
            "reason": "active_substitute_conflict",
            "game_id": game,
            "player_id": player,
        })
        self.assertNotIn(_INDEX, error.to_dict()["error"]["message"])
        self.assertEqual(_rows(
            self.store, [first_id, unrelated_id, duplicate_id]), [{
                "id": first_id,
                "game_id": game,
                "player_id": player,
                "team_id": f"{self.prefix}-team-4",
                "status": SubstituteStatus.ENROLLED.value,
            }])

    def test_every_non_active_status_can_remain_as_history(self):
        # Pin the predicate and the partial-index literals to the same derived
        # domain axis.  A new enum member cannot silently join the live set.
        self.assertEqual(
            {status for status in SubstituteStatus
             if status.is_active_enrollment}, _ACTIVE)
        historical = [status for status in SubstituteStatus
                      if not status.is_active_enrollment]
        game = f"{self.prefix}-game"
        player = f"{self.prefix}-player"

        with self.store.transaction():
            for ordinal, status in enumerate(historical):
                self.store.add_substitute(_sub(
                    self._id(f"history-{status.value}"),
                    game=game,
                    player=player,
                    team=f"{self.prefix}-team-{4 + ordinal % 2}",
                    status=status))
            live_id = self._id("fresh-live")
            self.store.add_substitute(_sub(
                live_id, game=game, player=player,
                team=f"{self.prefix}-team-4", status=SubstituteStatus.ENROLLED))

        rows = _rows(self.store, self.owned_ids)
        self.assertEqual(len(rows), len(historical) + 1)
        self.assertEqual(
            {row["status"] for row in rows},
            {status.value for status in historical} | {"enrolled"})
        self.assertEqual(
            [row["id"] for row in rows if row["status"] in
             {status.value for status in _ACTIVE}], [live_id])

    def test_null_bearing_keys_stay_outside_the_partial_unique_index(self):
        rows = (
            _sub(self._id("null-player-one"), game=f"{self.prefix}-game",
                 player=None, team=f"{self.prefix}-team-4",
                 status=SubstituteStatus.ENROLLED),
            _sub(self._id("null-player-two"), game=f"{self.prefix}-game",
                 player=None, team=f"{self.prefix}-team-5",
                 status=SubstituteStatus.OFFERED),
            _sub(self._id("null-game-one"), game=None,
                 player=f"{self.prefix}-player", team=f"{self.prefix}-team-4",
                 status=SubstituteStatus.ENROLLED),
            _sub(self._id("null-game-two"), game=None,
                 player=f"{self.prefix}-player", team=f"{self.prefix}-team-5",
                 status=SubstituteStatus.OFFERED),
        )
        with self.store.transaction():
            for row in rows:
                self.store.add_substitute(row)
        self.assertEqual(len(_rows(self.store, self.owned_ids)), len(rows))

    def test_save_that_reactivates_history_uses_same_translation(self):
        game = f"{self.prefix}-game"
        player = f"{self.prefix}-player"
        history_id = self._id("history")
        live_id = self._id("live")
        history = _sub(
            history_id, game=game, player=player,
            team=f"{self.prefix}-team-4", status=SubstituteStatus.WITHDRAWN)

        with self.store.transaction():
            self.store.add_substitute(history)
            self.store.add_substitute(_sub(
                live_id, game=game, player=player,
                team=f"{self.prefix}-team-5", status=SubstituteStatus.ENROLLED))

        history.status = SubstituteStatus.OFFERED
        with self.assertRaises(IntegrityConflictError) as caught:
            with self.store.transaction():
                self.store.save_substitute(history)

        self.assertEqual(str(caught.exception), _MESSAGE)
        self.assertEqual(caught.exception.details, {
            "reason": "active_substitute_conflict",
            "game_id": game,
            "player_id": player,
        })
        persisted = {row["id"]: row for row in
                     _rows(self.store, [history_id, live_id])}
        self.assertEqual(persisted[history_id]["status"], "withdrawn")
        self.assertEqual(persisted[live_id]["status"], "enrolled")


class SQLiteActiveSubstituteConstraintTest(
        _ActiveSubstituteConstraintContract, unittest.TestCase):
    URL = ":memory:"
    EXPECTED_BACKEND = "sqlite"


class InMemoryActiveSubstituteConstraintTest(unittest.TestCase):
    """Memory mirrors migration 064 rather than accepting impossible state."""

    def setUp(self):
        self.store = InMemoryStore()

    def test_opposite_target_duplicate_is_same_conflict_and_rolls_back(self):
        first = _sub(
            "memory-first", game="memory-game", player="memory-player",
            team="memory-team-4", status=SubstituteStatus.ENROLLED)
        unrelated = _sub(
            "memory-unrelated", game="memory-other", player="memory-player",
            team="memory-team-4", status=SubstituteStatus.ENROLLED)
        duplicate = _sub(
            "memory-duplicate", game="memory-game", player="memory-player",
            team="memory-team-5", status=SubstituteStatus.OFFERED)
        with self.store.transaction():
            self.store.add_substitute(first)

        with self.assertRaises(IntegrityConflictError) as caught:
            with self.store.transaction():
                self.store.add_substitute(unrelated)
                self.store.add_substitute(duplicate)

        self.assertEqual(str(caught.exception), _MESSAGE)
        self.assertEqual(caught.exception.details, {
            "reason": "active_substitute_conflict",
            "game_id": "memory-game",
            "player_id": "memory-player",
        })
        self.assertEqual(set(self.store.substitutes), {first.id})

    def test_reactivating_history_is_refused_but_same_row_transition_is_valid(self):
        history = _sub(
            "memory-history", game="memory-game", player="memory-player",
            team="memory-team-4", status=SubstituteStatus.WITHDRAWN)
        live = _sub(
            "memory-live", game="memory-game", player="memory-player",
            team="memory-team-5", status=SubstituteStatus.ENROLLED)
        with self.store.transaction():
            self.store.add_substitute(history)
            self.store.add_substitute(live)

        with self.assertRaises(IntegrityConflictError):
            with self.store.transaction():
                stored = self.store.substitutes[history.id]
                stored.status = SubstituteStatus.OFFERED
                self.store.save_substitute(stored)
        self.assertEqual(
            self.store.substitutes[history.id].status,
            SubstituteStatus.WITHDRAWN)

        with self.store.transaction():
            live.status = SubstituteStatus.OFFERED
            self.store.save_substitute(live)
        self.assertEqual(
            self.store.substitutes[live.id].status,
            SubstituteStatus.OFFERED)

    def test_null_bearing_keys_match_sql_partial_index_semantics(self):
        rows = (
            _sub("memory-null-player-one", game="memory-game", player=None,
                 team="memory-team-4", status=SubstituteStatus.ENROLLED),
            _sub("memory-null-player-two", game="memory-game", player=None,
                 team="memory-team-5", status=SubstituteStatus.OFFERED),
            _sub("memory-null-game-one", game=None, player="memory-player",
                 team="memory-team-4", status=SubstituteStatus.ENROLLED),
            _sub("memory-null-game-two", game=None, player="memory-player",
                 team="memory-team-5", status=SubstituteStatus.OFFERED),
        )
        with self.store.transaction():
            for row in rows:
                self.store.add_substitute(row)
        self.assertEqual(set(self.store.substitutes), {row.id for row in rows})


@unittest.skipUnless(
    os.environ.get("TEST_DATABASE_URL"),
    "PostgreSQL not configured: migration 064's active substitute constraint "
    "was NOT exercised on PostgreSQL; a skip is not a pass.")
class PostgreSQLActiveSubstituteConstraintTest(
        _ActiveSubstituteConstraintContract, unittest.TestCase):
    URL = os.environ.get("TEST_DATABASE_URL")
    EXPECTED_BACKEND = "postgres"


class ActiveSubstitutePreMigrationTest(unittest.TestCase):
    def test_migration_064_check_is_registered_behind_the_right_writer_lock(self):
        check_fn, lock_table = _ATOMIC_PRE_MIGRATION_CHECKS[_VERSION]
        self.assertIs(check_fn, assert_no_duplicate_active_substitute_players)
        self.assertEqual(lock_table, "substitute_enrollments")

    def test_dirty_active_duplicates_abort_before_migration_064_ddl(self):
        # This schema surgery is safe only on the private in-memory database.
        store = SqlStore(":memory:")
        prefix = f"t063-preflight-{uuid.uuid4().hex}"
        game = f"{prefix}-game"
        player = f"{prefix}-player"
        try:
            with store.transaction():
                cur = store.conn.cursor()
                cur.execute(f"DROP INDEX {_INDEX}")
                cur.execute(store.dialect.sql(
                    "DELETE FROM schema_migrations WHERE version = ?"),
                    (_VERSION,))
                store.add_substitute(_sub(
                    f"{prefix}-one", game=game, player=player,
                    team=f"{prefix}-team-4", status=SubstituteStatus.ENROLLED))
                store.add_substitute(_sub(
                    f"{prefix}-two", game=game, player=player,
                    team=f"{prefix}-team-5", status=SubstituteStatus.OFFERED))

            self.assertEqual(
                find_duplicate_active_substitute_players(store.conn),
                [(game, player)])
            with self.assertRaises(MigrationDataError) as direct:
                assert_no_duplicate_active_substitute_players(store.conn)
            self.assertIn("1 duplicate pair(s)", str(direct.exception))
            self.assertNotIn(game, str(direct.exception))
            self.assertNotIn(player, str(direct.exception))

            with self.assertRaises(MigrationDataError) as upgrade:
                migrate(store.conn, store.dialect)
            self.assertEqual(str(upgrade.exception), str(direct.exception))
            self.assertNotIn(game, str(upgrade.exception))
            self.assertNotIn(player, str(upgrade.exception))
            self.assertNotIn(_VERSION, store.migration_status()["applied"])
            self.assertEqual(
                find_duplicate_active_substitute_players(store.conn),
                [(game, player)])
        finally:
            store.close()


if __name__ == "__main__":
    unittest.main()
