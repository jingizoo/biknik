"""#428 — cancellation releases live ice and keeps immutable facility history.

The same behavioral oracle runs on Memory, file-backed SQLite, and real
PostgreSQL when ``TEST_DATABASE_URL`` is set.  SQLite deliberately uses a file
rather than ``:memory:`` so the history and released occupancy are re-read from
a second connection, matching the persistence boundary production uses.
"""

import os
import sqlite3
import tempfile
import threading
import unittest
from datetime import datetime, timezone

from helpers import BACKEND, fresh_sql_store  # noqa: F401

from hockey_scheduler.api import ApiService
from hockey_scheduler.domain import AuditAction, IceSlotStatus
from hockey_scheduler.full_demo import build_full_demo_store
from hockey_scheduler.store import InMemoryStore, SqlStore


SEED_NOW = datetime(2031, 1, 6, 12, tzinfo=timezone.utc)

CANCELLATION_COLUMNS = (
    "cancelled_ice_slot_id", "cancelled_venue_id", "cancelled_venue_name",
    "cancelled_venue_timezone", "cancelled_rink_id", "cancelled_rink_name",
    "cancelled_scheduled_start_time", "cancelled_scheduled_end_time",
    "cancelled_ice_start_time", "cancelled_ice_end_time",
)


def _store_cases():
    yield "memory", InMemoryStore(), None

    fd, path = tempfile.mkstemp(prefix="biknik428_", suffix=".sqlite3")
    os.close(fd)
    sqlite = None
    try:
        sqlite = SqlStore(path)
        yield "sqlite", sqlite, path
    finally:
        if sqlite is not None:
            sqlite.close()
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass

    url = os.environ.get("TEST_DATABASE_URL")
    if url:
        postgres = fresh_sql_store(url)
        try:
            yield "postgres", postgres, url
        finally:
            postgres.close()


def _seed(store):
    store, game_id, _ids = build_full_demo_store(
        store, seed_instant=SEED_NOW)
    game = store.get_game(game_id)
    slot = store.get_ice_slot(game.ice_slot_id)
    rink = store.get_rink(slot.rink_id)
    venue = store.get_venue(rink.venue_id)
    return ApiService(store), game, slot, rink, venue


def _cancel_audits(store, game_id):
    return [a for a in store.audit_for_game(game_id)
            if a.action == AuditAction.GAME_CANCELLED]


def _program_serialized_cancel_race(testcase, store, api, game_id, contender,
                                    *, contender_may_finish=False):
    """Park cancellation after its Program lock and start ``contender``.

    Placement/move/another cancel take that same first lock and must wait.
    IceSlot delete is intentionally different: it may observe the pre-cancel
    dependency and refuse before the cancellation commits, which is also a
    valid serialized result.  Exceptions are captured for the test thread so
    no background failure can be lost.
    """
    program_locked = threading.Event()
    release_cancel = threading.Event()
    contender_started = threading.Event()
    contender_done = threading.Event()
    outcomes = {}
    original_program_lock = store.get_program_for_update

    def pause_after_program_lock(program_id):
        row = original_program_lock(program_id)
        program_locked.set()
        if not release_cancel.wait(20):
            raise AssertionError("cancel race pause timed out")
        return row

    def run_cancel():
        try:
            outcomes["cancel"] = api.cancel_game(
                game_id, actor_id="operator")
        except BaseException as exc:  # surfaced in the main thread
            outcomes["cancel_error"] = exc

    def run_contender():
        contender_started.set()
        try:
            outcomes["contender"] = contender()
        except BaseException as exc:
            outcomes["contender_error"] = exc
        finally:
            contender_done.set()

    store.get_program_for_update = pause_after_program_lock
    cancel_thread = threading.Thread(target=run_cancel)
    contender_thread = threading.Thread(target=run_contender)
    try:
        cancel_thread.start()
        testcase.assertTrue(program_locked.wait(20),
                            "cancellation never reached Program lock")
        contender_thread.start()
        testcase.assertTrue(contender_started.wait(5))
        if not contender_may_finish:
            # It has attempted the competing transaction, but cannot finish
            # until cancellation releases the canonical Program lock.
            testcase.assertFalse(contender_done.wait(0.05))
        release_cancel.set()
        cancel_thread.join(30)
        contender_thread.join(30)
    finally:
        release_cancel.set()
        store.get_program_for_update = original_program_lock
        if cancel_thread.is_alive():
            cancel_thread.join(1)
        if contender_thread.is_alive():
            contender_thread.join(1)

    testcase.assertFalse(cancel_thread.is_alive())
    testcase.assertFalse(contender_thread.is_alive())
    testcase.assertNotIn("cancel_error", outcomes, outcomes)
    testcase.assertNotIn("contender_error", outcomes, outcomes)
    testcase.assertNotIn("error", outcomes["cancel"], outcomes)
    return outcomes


class CancelledGameIceReleaseParityTest(unittest.TestCase):
    def test_sqlite_upgrade_repairs_old_cancelled_attachment(self):
        """Migration 062 upgrades the exact old persisted defect, not only
        newly-cancelled Games created after deployment."""
        fd, path = tempfile.mkstemp(prefix="biknik428_upgrade_",
                                    suffix=".sqlite3")
        os.close(fd)
        store = None
        upgraded = None
        try:
            store = SqlStore(path)
            _api, game0, slot0, rink0, venue0 = _seed(store)
            # Reconstruct the pre-062 schema and data shape: cancellation was
            # a flag only, while the Game stayed attached to ALLOCATED ice.
            store._exec("UPDATE games SET cancelled = 1 WHERE id = ?",
                        (game0.id,))
            store.conn.commit()
            store.close()
            store = None

            raw = sqlite3.connect(path)
            try:
                for column in CANCELLATION_COLUMNS:
                    raw.execute(f"ALTER TABLE games DROP COLUMN {column}")
                raw.execute(
                    "DELETE FROM schema_migrations WHERE version = ?",
                    ("062_cancelled_game_ice_history",))
                raw.commit()
            finally:
                raw.close()

            upgraded = SqlStore(path)  # applies 062 + its data repair
            game = upgraded.get_game(game0.id)
            self.assertTrue(game.cancelled)
            self.assertIsNone(game.ice_slot_id)
            self.assertEqual(game.cancelled_ice_slot_id, slot0.id)
            self.assertEqual(game.cancelled_rink_id, rink0.id)
            self.assertEqual(game.cancelled_rink_name, rink0.name)
            self.assertEqual(game.cancelled_venue_id, venue0.id)
            self.assertEqual(game.cancelled_venue_name, venue0.name)
            self.assertEqual(game.cancelled_scheduled_start_time,
                             game0.start_time)
            self.assertEqual(game.cancelled_ice_start_time, slot0.start_time)
            self.assertEqual(upgraded.get_ice_slot(slot0.id).status,
                             IceSlotStatus.AVAILABLE)
        finally:
            if store is not None:
                store.close()
            if upgraded is not None:
                upgraded.close()
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass

    def test_snapshot_detach_release_and_idempotency(self):
        exercised = []
        for backend, store, reopen_url in _store_cases():
            with self.subTest(backend=backend):
                exercised.append(backend)
                api, game0, slot0, rink0, venue0 = _seed(store)
                response = api.cancel_game(game0.id, actor_id="operator")
                self.assertNotIn("error", response, response)

                # File SQLite is re-read from an independent connection, not
                # from the connection/object that performed cancellation.
                check = (SqlStore(reopen_url)
                         if backend == "sqlite" else store)
                try:
                    game = check.get_game(game0.id)
                    slot = check.get_ice_slot(slot0.id)
                    self.assertTrue(game.cancelled)
                    self.assertIsNone(game.ice_slot_id)
                    self.assertEqual(slot.status, IceSlotStatus.AVAILABLE)
                    self.assertEqual(game.cancelled_ice_slot_id, slot0.id)
                    self.assertEqual(game.cancelled_rink_id, rink0.id)
                    self.assertEqual(game.cancelled_rink_name, rink0.name)
                    self.assertEqual(game.cancelled_venue_id, venue0.id)
                    self.assertEqual(game.cancelled_venue_name, venue0.name)
                    self.assertEqual(game.cancelled_venue_timezone,
                                     venue0.timezone)
                    self.assertEqual(game.cancelled_scheduled_start_time,
                                     game0.start_time)
                    self.assertEqual(game.cancelled_scheduled_end_time,
                                     game0.end_time)
                    self.assertEqual(game.cancelled_ice_start_time,
                                     slot0.start_time)
                    self.assertEqual(game.cancelled_ice_end_time,
                                     slot0.end_time)
                    self.assertEqual(len(_cancel_audits(check, game0.id)), 1)
                    detail = _cancel_audits(check, game0.id)[0].detail
                    self.assertEqual(detail["released_ice_slot_id"], slot0.id)
                    self.assertEqual(detail["venue_name"], venue0.name)
                    self.assertEqual(detail["rink_name"], rink0.name)
                finally:
                    if check is not store:
                        check.close()

                # A retry is a true no-op: no duplicate audit and no history
                # overwrite after the live slot has already been detached.
                again = api.cancel_game(game0.id, actor_id="operator-2")
                self.assertNotIn("error", again, again)
                self.assertEqual(len(_cancel_audits(store, game0.id)), 1)

                row = next(r for r in api.get_demo_overview()["schedule"]
                           if r["game_id"] == game0.id)
                self.assertTrue(row["cancelled"])
                self.assertIsNone(row["ice_slot_id"])
                self.assertIsNone(row["reserved"])
                self.assertEqual(row["venue_name"], venue0.name)
                self.assertEqual(row["rink_name"], rink0.name)
                self.assertEqual(row["historical_ice"]["ice_slot_id"],
                                 slot0.id)
                live_slot = next(
                    r for r in api.get_demo_overview()["ice_slots"]
                    if r["id"] == slot0.id)
                self.assertEqual(live_slot["status"], "available")
                self.assertIsNone(live_slot["game_id"])
                self.assertIsNone(live_slot["game_label"])
                self.assertIsNone(live_slot["reserved"])
        self.assertIn("memory", exercised)
        self.assertIn("sqlite", exercised)
        if os.environ.get("TEST_DATABASE_URL"):
            self.assertIn("postgres", exercised)

    def test_released_slot_accepts_exactly_one_replacement_game(self):
        for backend, store, _reopen_url in _store_cases():
            with self.subTest(backend=backend):
                api, game0, slot0, _rink0, _venue0 = _seed(store)
                self.assertNotIn("error", api.cancel_game(game0.id))
                replacement = api.create_game(
                    game0.season_id, game0.division_id,
                    game0.home_team_id, game0.away_team_id, slot0.id,
                    league_id=game0.league_id, actor_id="scheduler")
                self.assertNotIn("error", replacement, replacement)
                refused = api.create_game(
                    game0.season_id, game0.division_id,
                    game0.home_team_id, game0.away_team_id, slot0.id,
                    league_id=game0.league_id, actor_id="scheduler-2")
                self.assertIn("error", refused, refused)
                active = [g for g in store.all_games()
                          if not g.cancelled and g.ice_slot_id == slot0.id]
                self.assertEqual([g.id for g in active], [replacement["id"]])
                self.assertEqual(store.get_ice_slot(slot0.id).status,
                                 IceSlotStatus.ALLOCATED)
                historical = store.get_game(game0.id)
                self.assertIsNone(historical.ice_slot_id)
                self.assertEqual(historical.cancelled_ice_slot_id, slot0.id)

    def test_released_slot_can_be_deleted_without_erasing_history(self):
        for backend, store, _reopen_url in _store_cases():
            with self.subTest(backend=backend):
                api, game0, slot0, rink0, venue0 = _seed(store)
                historical_rink_name = rink0.name
                historical_venue_name = venue0.name
                self.assertNotIn("error", api.cancel_game(game0.id))
                # Live facility edits after cancellation must never rewrite
                # the denormalized historical display facts.
                rink0.name = "Renamed live rink"
                venue0.name = "Renamed live venue"
                store.save_rink(rink0)
                store.save_venue(venue0)
                deleted = api.delete_ice_slot(slot0.id, actor_id="operator")
                self.assertNotIn("error", deleted, deleted)
                self.assertIsNone(store.get_ice_slot(slot0.id))
                history = store.get_game(game0.id)
                self.assertEqual(history.cancelled_ice_slot_id, slot0.id)
                self.assertEqual(history.cancelled_rink_name,
                                 historical_rink_name)
                self.assertEqual(history.cancelled_venue_name,
                                 historical_venue_name)
                row = next(r for r in api.get_demo_overview()["schedule"]
                           if r["game_id"] == game0.id)
                self.assertEqual(row["historical_ice"]["rink_name"],
                                 historical_rink_name)
                self.assertEqual(row["historical_ice"]["venue_name"],
                                 historical_venue_name)

    def test_failure_after_release_rolls_back_every_effect(self):
        for backend, store, _reopen_url in _store_cases():
            with self.subTest(backend=backend):
                api, game0, slot0, _rink0, _venue0 = _seed(store)
                original_add_audit = store.add_audit

                def fail_audit(_entry):
                    raise RuntimeError("forced audit failure")

                store.add_audit = fail_audit
                try:
                    with self.assertRaisesRegex(RuntimeError,
                                                "forced audit failure"):
                        api.roster.cancel_game(game0.id, actor_id="operator")
                finally:
                    store.add_audit = original_add_audit

                game = store.get_game(game0.id)
                self.assertFalse(game.cancelled)
                self.assertEqual(game.ice_slot_id, slot0.id)
                self.assertIsNone(game.cancelled_ice_slot_id)
                self.assertEqual(store.get_ice_slot(slot0.id).status,
                                 IceSlotStatus.ALLOCATED)
                self.assertEqual(len(_cancel_audits(store, game0.id)), 0)

    def test_partial_or_preexisting_snapshot_fails_closed(self):
        """No corrupt/manual history is completed from today's facility
        names or used to detach an active Game."""
        for backend, store, _reopen_url in _store_cases():
            with self.subTest(backend=backend):
                api, game0, slot0, _rink0, _venue0 = _seed(store)
                game0.cancelled_ice_slot_id = slot0.id
                store.save_game(game0)
                result = api.cancel_game(game0.id, actor_id="operator")
                self.assertEqual(result["error"]["details"]["reason"],
                                 "cancellation_history_incomplete")
                game = store.get_game(game0.id)
                self.assertFalse(game.cancelled)
                self.assertEqual(game.ice_slot_id, slot0.id)
                self.assertEqual(game.cancelled_ice_slot_id, slot0.id)
                self.assertIsNone(game.cancelled_venue_name)
                self.assertEqual(store.get_ice_slot(slot0.id).status,
                                 IceSlotStatus.ALLOCATED)
                self.assertEqual(len(_cancel_audits(store, game0.id)), 0)

    def test_cancel_before_and_after_publication_has_one_contract(self):
        """Publication changes visibility, never release/history semantics."""
        for published in (False, True):
            for backend, store, _reopen_url in _store_cases():
                with self.subTest(backend=backend, published=published):
                    api, game0, slot0, _rink0, _venue0 = _seed(store)
                    api.setup.publish_game(game0.id, published=published,
                                           actor_id="operator")
                    self.assertEqual(store.get_game(game0.id).published,
                                     published)
                    result = api.cancel_game(game0.id, actor_id="operator")
                    self.assertNotIn("error", result, result)
                    game = store.get_game(game0.id)
                    self.assertTrue(game.cancelled)
                    self.assertIsNone(game.ice_slot_id)
                    self.assertEqual(game.cancelled_ice_slot_id, slot0.id)
                    self.assertEqual(store.get_ice_slot(slot0.id).status,
                                     IceSlotStatus.AVAILABLE)
                    overview = api.get_demo_overview()
                    self.assertFalse(any(
                        row.get("game_id") == game0.id
                        for row in overview["public_fixtures"]))

    def test_concurrent_cancel_then_reassign_serializes_on_every_store(self):
        """The placement waits behind cancellation and then claims released
        ice exactly once.  The pause is after the canonical Program row lock,
        so Memory's process lock, SQLite's BEGIN IMMEDIATE, and PostgreSQL's
        FOR UPDATE are all exercised at their real serialization boundary."""
        exercised = []
        for backend, store, reopen_url in _store_cases():
            peer = None
            with self.subTest(backend=backend):
                exercised.append(backend)
                api, game0, slot0, _rink0, _venue0 = _seed(store)
                peer = store if backend == "memory" else SqlStore(reopen_url)
                peer_api = ApiService(peer)
                outcomes = _program_serialized_cancel_race(
                    self, store, api, game0.id,
                    lambda: peer_api.create_game(
                        game0.season_id, game0.division_id,
                        game0.home_team_id, game0.away_team_id, slot0.id,
                        league_id=game0.league_id,
                        actor_id="scheduler"))
                self.assertNotIn("error", outcomes["contender"], outcomes)

                check = peer
                cancelled = check.get_game(game0.id)
                replacement_id = outcomes["contender"]["id"]
                active = [g for g in check.all_games()
                          if not g.cancelled and g.ice_slot_id == slot0.id]
                self.assertTrue(cancelled.cancelled)
                self.assertIsNone(cancelled.ice_slot_id)
                self.assertEqual(cancelled.cancelled_ice_slot_id, slot0.id)
                self.assertEqual([g.id for g in active], [replacement_id])
                self.assertEqual(check.get_ice_slot(slot0.id).status,
                                 IceSlotStatus.ALLOCATED)
                self.assertEqual(len(_cancel_audits(check, game0.id)), 1)
            if peer is not None and peer is not store:
                peer.close()
        self.assertIn("memory", exercised)
        self.assertIn("sqlite", exercised)
        if os.environ.get("TEST_DATABASE_URL"):
            self.assertIn("postgres", exercised)

    def test_concurrent_cancel_and_move_never_reclaims_detached_ice(self):
        for backend, store, reopen_url in _store_cases():
            peer = None
            with self.subTest(backend=backend):
                api, game0, slot0, _rink0, _venue0 = _seed(store)
                target = next(
                    slot for slot in store.all_ice_slots()
                    if slot.id != slot0.id
                    and slot.status == IceSlotStatus.AVAILABLE
                    and slot.slot_type.value == "game")
                peer = store if backend == "memory" else SqlStore(reopen_url)
                peer_api = ApiService(peer)
                outcomes = _program_serialized_cancel_race(
                    self, store, api, game0.id,
                    lambda: peer_api.move_game(
                        game0.id, target.id, reason="concurrent move",
                        actor_id="scheduler"))
                self.assertIn("error", outcomes["contender"], outcomes)
                self.assertEqual(outcomes["contender"]["error"].get(
                    "details", {}).get("reason"), "game_cancelled")
                cancelled = peer.get_game(game0.id)
                self.assertTrue(cancelled.cancelled)
                self.assertIsNone(cancelled.ice_slot_id)
                self.assertEqual(cancelled.cancelled_ice_slot_id, slot0.id)
                self.assertEqual(peer.get_ice_slot(slot0.id).status,
                                 IceSlotStatus.AVAILABLE)
                self.assertEqual(peer.get_ice_slot(target.id).status,
                                 IceSlotStatus.AVAILABLE)
                self.assertEqual(len(_cancel_audits(peer, game0.id)), 1)
            if peer is not None and peer is not store:
                peer.close()

    def test_concurrent_cancel_and_delete_have_one_explainable_order(self):
        for backend, store, reopen_url in _store_cases():
            peer = None
            with self.subTest(backend=backend):
                api, game0, slot0, rink0, venue0 = _seed(store)
                peer = store if backend == "memory" else SqlStore(reopen_url)
                peer_api = ApiService(peer)
                outcomes = _program_serialized_cancel_race(
                    self, store, api, game0.id,
                    lambda: peer_api.delete_ice_slot(
                        slot0.id, actor_id="operator"),
                    contender_may_finish=True)

                # PostgreSQL may serialize the dependency read before the
                # cancellation and refuse; Memory/SQLite normally wait and
                # delete after it.  A refusal is retryable once cancellation
                # has committed.  Both orders end with the same preserved
                # Game history and no dangling live slot reference.
                deleted = outcomes["contender"]
                if "error" in deleted:
                    deleted = peer_api.delete_ice_slot(
                        slot0.id, actor_id="operator-retry")
                self.assertNotIn("error", deleted, deleted)
                self.assertIsNone(peer.get_ice_slot(slot0.id))
                cancelled = peer.get_game(game0.id)
                self.assertTrue(cancelled.cancelled)
                self.assertIsNone(cancelled.ice_slot_id)
                self.assertEqual(cancelled.cancelled_ice_slot_id, slot0.id)
                self.assertEqual(cancelled.cancelled_rink_name, rink0.name)
                self.assertEqual(cancelled.cancelled_venue_name, venue0.name)
                self.assertEqual(len(_cancel_audits(peer, game0.id)), 1)
            if peer is not None and peer is not store:
                peer.close()

    def test_two_concurrent_cancellations_write_one_audit(self):
        for backend, store, reopen_url in _store_cases():
            peer = None
            with self.subTest(backend=backend):
                api, game0, slot0, _rink0, _venue0 = _seed(store)
                peer = store if backend == "memory" else SqlStore(reopen_url)
                peer_api = ApiService(peer)
                outcomes = _program_serialized_cancel_race(
                    self, store, api, game0.id,
                    lambda: peer_api.cancel_game(
                        game0.id, actor_id="operator-2"))
                self.assertNotIn("error", outcomes["contender"], outcomes)
                cancelled = peer.get_game(game0.id)
                self.assertTrue(cancelled.cancelled)
                self.assertIsNone(cancelled.ice_slot_id)
                self.assertEqual(cancelled.cancelled_ice_slot_id, slot0.id)
                self.assertEqual(len(_cancel_audits(peer, game0.id)), 1)
            if peer is not None and peer is not store:
                peer.close()


if __name__ == "__main__":
    unittest.main()
