"""Concurrency regressions for the Season archive/read-only lifecycle (#159 review).

`archive_season`/`reopen_season` and every Season-owned write acquire the SAME
Season-row lock (`get_season_for_update`) before checking state, so the outcome
is linearizable on PostgreSQL:

- two concurrent archives → exactly one commits (one `season_archived` audit),
  the loser observes the committed archive and returns `season_already_archived`
  with zero extra mutation;
- archive racing reopen → a deterministic final state and audit set regardless
  of lock order;
- archive racing a Season-owned write (e.g. registration) → either the write
  commits before the archive (frozen history) or the archive wins and the writer
  returns `season_archived` with zero mutation — never a write into an already
  archived Season.

The forced barrier tests need real row locks, so they are PostgreSQL-only;
Memory/SQLite carry the sequential parity (their `transaction()` already holds a
process-wide lock for the whole body, so the same invariants hold).
"""

import os
import threading
import unittest

from helpers import BACKEND  # noqa: F401  (ensures sys.path is set up)

from hockey_scheduler.api import ApiService
from hockey_scheduler.domain.enums import SeasonStatus
from hockey_scheduler.domain.errors import ValidationError
from hockey_scheduler.store import InMemoryStore, SqlStore


def _seed(store, season_name="S1"):
    """Program → Season → League → Division → Club → Team (unregistered).
    Returns (season_id, team_id, division_id)."""
    api = ApiService(store)
    pid = api.create_program("Prog", "US", "UTC")["id"]
    sid = api.create_season(pid, season_name)["id"]
    lid = api.create_league(sid, "Gold")["id"]
    did = api.create_division(sid, "D1", league_id=lid)["id"]
    club = api.create_club("Club")["id"]
    tid = api.create_team(club_id=club, name="Alpha", league_id=lid)["id"]
    return sid, tid, did


def _season_audits(store, sid):
    return [a.action for a in store.all_setup_audit()
            if a.entity_id == sid
            and a.action in ("season_archived", "season_reopened")]


# =====================================================================
# PostgreSQL barrier races (real row locks)
# =====================================================================
@unittest.skipUnless(os.environ.get("TEST_DATABASE_URL"),
                     "PostgreSQL required (set TEST_DATABASE_URL)")
class SeasonArchiveRaceTest(unittest.TestCase):
    def setUp(self):
        self.url = os.environ["TEST_DATABASE_URL"]
        SqlStore(self.url).clear_all_data()

    def test_archive_vs_archive_exactly_one_wins(self):
        sid, _tid, _did = _seed(SqlStore(self.url))
        api_a = ApiService(SqlStore(self.url))
        api_b = ApiService(SqlStore(self.url))
        barrier = threading.Barrier(2)
        results = {}

        def arch(api, key):
            barrier.wait()
            try:
                api.setup.archive_season(sid, actor_id=key, reason="race")
                results[key] = "ok"
            except ValidationError as exc:
                results[key] = exc.details.get("reason")
            except Exception as exc:  # a raw DB error would land here
                results[key] = f"ERR:{exc}"

        ta = threading.Thread(target=arch, args=(api_a, "a"))
        tb = threading.Thread(target=arch, args=(api_b, "b"))
        ta.start(); tb.start(); ta.join(15); tb.join(15)

        # Exactly one winner; the loser gets the stable lifecycle error.
        outcomes = sorted(results.values())
        self.assertEqual(outcomes, ["ok", "season_already_archived"], results)
        check = SqlStore(self.url)
        self.assertEqual(check.get_season(sid).status, SeasonStatus.ARCHIVED)
        self.assertIsNotNone(check.get_season(sid).archived_at)
        # Exactly one transition audit — the loser wrote nothing.
        self.assertEqual(_season_audits(check, sid), ["season_archived"])

    def test_archive_vs_reopen_serializes_to_a_valid_order(self):
        # From an ACTIVE season, archive and reopen race. The row lock forces a
        # serial order; the outcome is whichever order won, but it is always a
        # VALID linearization (never a torn/duplicate state):
        #   * reopen-first  → reopen fails season_not_archived, archive wins →
        #     final ARCHIVED, audits == [season_archived];
        #   * archive-first → archive wins, reopen then runs on the archived
        #     season and succeeds → final ACTIVE, audits == [archived, reopened].
        sid, _tid, _did = _seed(SqlStore(self.url))
        api_arch = ApiService(SqlStore(self.url))
        api_reopen = ApiService(SqlStore(self.url))
        barrier = threading.Barrier(2)
        results = {}

        def run(fn, key):
            barrier.wait()
            try:
                fn(); results[key] = "ok"
            except ValidationError as exc:
                results[key] = exc.details.get("reason")
            except Exception as exc:
                results[key] = f"ERR:{exc}"

        ta = threading.Thread(target=run, args=(
            lambda: api_arch.setup.archive_season(sid, actor_id="a", reason="x"),
            "archive"))
        tb = threading.Thread(target=run, args=(
            lambda: api_reopen.setup.reopen_season(sid, actor_id="b", reason="y"),
            "reopen"))
        ta.start(); tb.start(); ta.join(15); tb.join(15)

        # Archive always commits (the season is active when it runs, whichever
        # order); reopen either wins after it or is rejected before it.
        self.assertEqual(results["archive"], "ok", results)
        check = SqlStore(self.url)
        status = check.get_season(sid).status
        audits = _season_audits(check, sid)
        if results["reopen"] == "ok":            # archive-first ordering
            self.assertEqual(status, SeasonStatus.ACTIVE, results)
            self.assertEqual(audits, ["season_archived", "season_reopened"])
        else:                                     # reopen-first, rejected
            self.assertEqual(results["reopen"], "season_not_archived", results)
            self.assertEqual(status, SeasonStatus.ARCHIVED, results)
            self.assertEqual(audits, ["season_archived"])

    def test_archive_vs_registration_write_is_linearizable(self):
        # A Season-owned write (register a Team) races an archive. Either the
        # registration commits before the archive, or the archive wins and the
        # writer returns season_archived — never a registration written into an
        # already-archived Season.
        sid, tid, did = _seed(SqlStore(self.url))
        api_arch = ApiService(SqlStore(self.url))
        api_reg = ApiService(SqlStore(self.url))
        barrier = threading.Barrier(2)
        results = {}

        def run(fn, key):
            barrier.wait()
            try:
                fn(); results[key] = "ok"
            except ValidationError as exc:
                results[key] = exc.details.get("reason")
            except Exception as exc:
                results[key] = f"ERR:{exc}"

        ta = threading.Thread(target=run, args=(
            lambda: api_arch.setup.archive_season(sid, actor_id="a", reason="x"),
            "archive"))
        tb = threading.Thread(target=run, args=(
            lambda: api_reg.setup.register_team_for_season(
                sid, tid, division_id=did, actor_id="b"), "register"))
        ta.start(); tb.start(); ta.join(15); tb.join(15)

        self.assertEqual(results["archive"], "ok", results)
        # The register either committed BEFORE the archive, or was rejected.
        self.assertIn(results["register"], ("ok", "season_archived"), results)
        check = SqlStore(self.url)
        self.assertEqual(check.get_season(sid).status, SeasonStatus.ARCHIVED)
        regs = [r for r in check.registrations_for_season(sid) if r.active]
        if results["register"] == "season_archived":
            # Archive won the row first: no registration slipped in afterward.
            self.assertEqual(regs, [], "write leaked into an archived Season")
        else:
            # Registration committed before the archive — frozen history.
            self.assertEqual(len(regs), 1)


# =====================================================================
# Memory + SQLite sequential parity
# =====================================================================
class SeasonArchiveParityTest(unittest.TestCase):
    def _backends(self):
        yield "memory", InMemoryStore()
        yield "sqlite", SqlStore(":memory:")

    def test_double_archive_writes_exactly_one_audit(self):
        for label, store in self._backends():
            with self.subTest(backend=label):
                sid, _tid, _did = _seed(store)
                api = ApiService(store)
                api.setup.archive_season(sid, actor_id="a", reason="x")
                with self.assertRaises(ValidationError) as ctx:
                    api.setup.archive_season(sid, actor_id="a", reason="x")
                self.assertEqual(ctx.exception.details.get("reason"),
                                 "season_already_archived", label)
                self.assertEqual(_season_audits(store, sid), ["season_archived"],
                                 label)
                if isinstance(store, SqlStore):
                    store.close()


if __name__ == "__main__":
    unittest.main()
