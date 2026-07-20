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
from hockey_scheduler.domain.errors import (
    HasDependenciesError,
    NotFoundError,
    ValidationError,
)
from hockey_scheduler.domain.setup_models import League
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


    def test_archive_vs_delete_season_is_linearizable(self):
        # An empty Season can be deleted while active. A delete racing an archive
        # serializes on the same row lock: either delete wins (season active when
        # it ran → removed, archive then finds nothing) or archive wins (season
        # archived → delete fails season_archived, history retained). An archived
        # Season is NEVER deleted.
        store0 = SqlStore(self.url)
        api0 = ApiService(store0)
        pid = api0.create_program("Prog", "US", "UTC")["id"]
        sid = api0.create_season(pid, "S1")["id"]  # empty → deletable
        api_arch = ApiService(SqlStore(self.url))
        api_del = ApiService(SqlStore(self.url))
        barrier = threading.Barrier(2)
        results = {}

        def run(fn, key):
            barrier.wait()
            try:
                fn(); results[key] = "ok"
            except ValidationError as exc:
                results[key] = exc.details.get("reason")
            except NotFoundError:
                results[key] = "not_found"
            except Exception as exc:
                results[key] = f"ERR:{exc}"

        ta = threading.Thread(target=run, args=(
            lambda: api_arch.setup.archive_season(sid, actor_id="a", reason="x"),
            "archive"))
        tb = threading.Thread(target=run, args=(
            lambda: api_del.setup.delete_season(sid, actor_id="b"), "delete"))
        ta.start(); tb.start(); ta.join(15); tb.join(15)

        check = SqlStore(self.url)
        season = check.get_season(sid)
        if results["delete"] == "ok":
            # Delete won the row first (season was active) → gone; archive found
            # nothing.
            self.assertIsNone(season, results)
            self.assertEqual(results["archive"], "not_found", results)
        else:
            # Archive won → archived, and the delete fails closed (history kept).
            self.assertEqual(results["archive"], "ok", results)
            self.assertEqual(results["delete"], "season_archived", results)
            self.assertIsNotNone(season, results)
            self.assertEqual(season.status, SeasonStatus.ARCHIVED, results)

    def test_archive_vs_transfer_freezes_registration_linearizably(self):
        # A Team registered in a Season is transferred to a NEW League while an
        # archive races. The transfer locks the Season row before deciding, so
        # its move-or-freeze decision is serialized against the archive and the
        # persisted registration state always agrees with the transfer audit —
        # the archived registration is never rewritten out from under the lock.
        store0 = SqlStore(self.url)
        api0 = ApiService(store0)
        pid = api0.create_program("Prog", "US", "UTC")["id"]
        sid = api0.create_season(pid, "S1")["id"]
        l1 = api0.create_league(sid, "Gold")["id"]
        l2 = api0.create_league(sid, "Silver")["id"]
        did = api0.create_division(sid, "D1", league_id=l1)["id"]
        club = api0.create_club("Club")["id"]
        tid = api0.create_team(club_id=club, name="Alpha", league_id=l1)["id"]
        api0.register_team_for_season(sid, tid, division_id=did)
        reg = store0.registration_for_team_in_season(sid, tid)
        reg_id, old_ls = reg.id, reg.league_season_id

        api_arch = ApiService(SqlStore(self.url))
        api_tx = ApiService(SqlStore(self.url))
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
            lambda: api_tx.setup.transfer_team_to_league(tid, l2, actor_id="b"),
            "transfer"))
        ta.start(); tb.start(); ta.join(15); tb.join(15)

        # Archive always commits (nothing here archives/removes the Season under
        # it); the transfer always succeeds (it either moves the reg or freezes
        # it), leaving the Season archived.
        self.assertEqual(results["archive"], "ok", results)
        self.assertEqual(results["transfer"], "ok", results)
        check = SqlStore(self.url)
        self.assertEqual(check.get_season(sid).status, SeasonStatus.ARCHIVED)
        reg2 = check.get_season_team_registration(reg_id)
        moved = reg2.league_season_id != old_ls
        tx_audit = [a for a in check.all_setup_audit()
                    if a.action == "team_league_transferred"
                    and a.entity_id == tid][0]
        audit_moved = reg_id in tx_audit.detail.get("registrations_moved", [])
        # The persisted registration and the transfer's own audit agree — the
        # move-or-freeze decision and the write are one atomic, serialized unit.
        self.assertEqual(moved, audit_moved, (moved, tx_audit.detail))
        if moved:
            # Moved before the archive linearization point → into the target
            # League's LeagueSeason for the same Season, never a torn value.
            new_ls = check.league_season_for(l2, sid)
            self.assertIsNotNone(new_ls, results)
            self.assertEqual(reg2.league_season_id, new_ls.id, results)
        else:
            # Archive won the row first → the registration stayed frozen.
            self.assertEqual(reg2.league_season_id, old_ls, results)
        # Whichever order, the Team's permanent League still moved.
        self.assertEqual(check.get_team(tid).league_id, l2, results)


    def test_archive_vs_unbind_league_season_is_linearizable(self):
        # Unbinding a League↔Season binding (the explicit, audited step that
        # clears a League's binding dependency) races an archive on that Season.
        # Both lock the same Season row: either the unbind wins (Season active →
        # binding removed, archive then archives the now league-less Season) or
        # the archive wins (unbind fails season_archived, the binding survives as
        # frozen history). A binding is NEVER removed from an archived Season.
        store0 = SqlStore(self.url)
        api0 = ApiService(store0)
        pid = api0.create_program("Prog", "US", "UTC")["id"]
        sid = api0.create_season(pid, "S1")["id"]
        lid = api0.create_league(sid, "Gold")["id"]
        ls_id = store0.league_seasons_for_league(lid)[0].id
        api_arch = ApiService(SqlStore(self.url))
        api_unbind = ApiService(SqlStore(self.url))
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
            lambda: api_unbind.setup.delete_league_season(ls_id, actor_id="b"),
            "unbind"))
        ta.start(); tb.start(); ta.join(15); tb.join(15)

        # Archive always commits (the unbind never archives/removes the Season).
        self.assertEqual(results["archive"], "ok", results)
        check = SqlStore(self.url)
        self.assertEqual(check.get_season(sid).status, SeasonStatus.ARCHIVED)
        if results["unbind"] == "ok":
            # Unbind won the row first (Season active) → binding gone.
            self.assertIsNone(check.get_league_season(ls_id), results)
        else:
            # Archive won → unbind fails closed; the binding is frozen history.
            self.assertEqual(results["unbind"], "season_archived", results)
            self.assertIsNotNone(check.get_league_season(ls_id), results)

    def test_delete_league_vs_team_create_is_linearizable(self):
        # An UNBOUND permanent League is deleted while a Team is concurrently
        # created into it. Both lock the same League row: either the create wins
        # (Team exists → delete blocks on the Team) or the delete wins (League
        # gone → the create fails not-found). A Team is never orphaned onto a
        # deleted League.
        store0 = SqlStore(self.url)
        api0 = ApiService(store0)
        pid = api0.create_program("Prog", "US", "UTC")["id"]
        lid = store0.next_id("league")
        store0.add_league(League(id=lid, program_id=pid, name="Unbound",
                                 sort_order=0))
        club = api0.create_club("Club")["id"]
        api_del = ApiService(SqlStore(self.url))
        api_new = ApiService(SqlStore(self.url))
        barrier = threading.Barrier(2)
        results = {}

        def run(fn, key):
            barrier.wait()
            try:
                fn(); results[key] = "ok"
            except HasDependenciesError:
                results[key] = "blocked"
            except NotFoundError:
                results[key] = "not_found"
            except Exception as exc:
                results[key] = f"ERR:{exc}"

        ta = threading.Thread(target=run, args=(
            lambda: api_del.setup.delete_league(lid, actor_id="a"), "delete"))
        tb = threading.Thread(target=run, args=(
            lambda: api_new.setup.create_team(
                club_id=club, name="Alpha", league_id=lid, actor_id="b"),
            "create"))
        ta.start(); tb.start(); ta.join(15); tb.join(15)

        check = SqlStore(self.url)
        teams = [t for t in check.all_teams() if t.league_id == lid]
        if results["create"] == "ok":
            # Create won the row first → Team exists, delete blocked on it.
            self.assertEqual(results["delete"], "blocked", results)
            self.assertIsNotNone(check.get_league(lid), results)
            self.assertEqual(len(teams), 1, results)
        else:
            # Delete won → League gone, create fails not-found, no orphan Team.
            self.assertEqual(results["create"], "not_found", results)
            self.assertEqual(results["delete"], "ok", results)
            self.assertIsNone(check.get_league(lid), results)
            self.assertEqual(teams, [], results)


    def test_archive_vs_legacy_import_is_linearizable(self):
        # The teams+players import holds the archived-Season row lock through ALL
        # its writes (guard is the first statement inside the single write
        # transaction). Racing an archive on PostgreSQL: either the whole import
        # commits before the archive (Teams/Players present), or the archive wins
        # and the import returns season_archived with zero Team/Player mutation —
        # never a partial import into an already-archived Season.
        csv = {
            "teams_csv": ("team_code,team_name,club_name,division_name\n"
                          "T1,Team One,Lions Club,U16\n"),
            "players_csv": ("player_code,first_name,last_name,team_code,"
                            "jersey_number,position,email\n"
                            "P1,Aarav,M,T1,9,forward,a@example.com\n"),
        }
        store0 = SqlStore(self.url)
        api0 = ApiService(store0)
        pid = api0.create_program("Prog", "US", "UTC")["id"]
        sid = api0.create_season(pid, "S1")["id"]
        api0.create_league(sid, "Gold")  # a League bound to the import target
        api_arch = ApiService(SqlStore(self.url))
        api_imp = ApiService(SqlStore(self.url))
        barrier = threading.Barrier(2)
        results = {}

        def run(fn, key):
            barrier.wait()
            try:
                r = fn()
                if isinstance(r, dict) and r.get("error"):
                    results[key] = (r["error"].get("details", {}).get("reason")
                                    or r["error"].get("code"))
                elif isinstance(r, dict) and r.get("committed") is False:
                    results[key] = f"not_committed:{r.get('errors')}"
                else:
                    results[key] = "ok"
            except ValidationError as exc:
                results[key] = exc.details.get("reason")
            except Exception as exc:
                results[key] = f"ERR:{exc}"

        ta = threading.Thread(target=run, args=(
            lambda: api_arch.setup.archive_season(sid, actor_id="a", reason="x"),
            "archive"))
        tb = threading.Thread(target=run, args=(
            lambda: api_imp.commit_teams_players_import(sid, csv, actor_id="b"),
            "import"))
        ta.start(); tb.start(); ta.join(20); tb.join(20)

        # Archive always commits (the import never archives/removes the Season).
        self.assertEqual(results["archive"], "ok", results)
        check = SqlStore(self.url)
        self.assertEqual(check.get_season(sid).status, SeasonStatus.ARCHIVED)
        teams = check.all_teams()
        players = check.all_players()
        if results["import"] == "ok":
            # Import committed before the archive → its rows are all present.
            self.assertEqual(len(teams), 1, results)
            self.assertEqual(len(players), 1, results)
        else:
            # Archive won the row → import fails closed, zero Team/Player writes.
            self.assertEqual(results["import"], "season_archived", results)
            self.assertEqual(teams, [], results)
            self.assertEqual(players, [], results)


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
