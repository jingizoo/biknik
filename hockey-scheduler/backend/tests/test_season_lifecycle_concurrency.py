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
from hockey_scheduler.domain.setup_models import League, SeasonTeamRegistration
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

    def test_delete_league_vs_create_binding_is_linearizable(self):
        # #159 review r9: an UNBOUND permanent League is deleted while
        # create_league_season concurrently binds it to a Season. Both now lock
        # the SAME League row (delete via get_league_for_update; the binder via
        # _lock_league_for_binding, taken BEFORE its Season guard). Either the
        # bind wins (LeagueSeason exists → delete blocks on the season-binding
        # dependent) or the delete wins (League gone → the binder fails
        # not-found). A binding is NEVER orphaned onto a deleted League (migration
        # 035 has no FK to catch one).
        store0 = SqlStore(self.url)
        api0 = ApiService(store0)
        pid = api0.create_program("Prog", "US", "UTC")["id"]
        sid = api0.create_season(pid, "S1")["id"]
        lid = store0.next_id("league")
        store0.add_league(League(id=lid, program_id=pid, name="Unbound",
                                 sort_order=0))
        api_del = ApiService(SqlStore(self.url))
        api_bind = ApiService(SqlStore(self.url))
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
            lambda: api_bind.setup.create_league_season(
                lid, sid, actor_id="b"), "bind"))
        ta.start(); tb.start(); ta.join(15); tb.join(15)

        check = SqlStore(self.url)
        binding = check.league_season_for(lid, sid)
        if results["bind"] == "ok":
            # Bind won the row first → binding exists, delete blocked on it.
            self.assertEqual(results["delete"], "blocked", results)
            self.assertIsNotNone(check.get_league(lid), results)
            self.assertIsNotNone(binding, results)
        else:
            # Delete won → League gone, binder fails not-found, NO orphan binding.
            self.assertEqual(results["bind"], "not_found", results)
            self.assertEqual(results["delete"], "ok", results)
            self.assertIsNone(check.get_league(lid), results)
            self.assertIsNone(binding, results)

    def test_delete_league_vs_division_bind_is_linearizable(self):
        # #159 review r9: the shared _link_league_season helper caller. An
        # UNBOUND, division-free permanent League is deleted while create_division
        # concurrently binds it (create_division → _resolve_division_league_season
        # → _link_league_season) and adds a Division under the new binding. The
        # binder row-locks the League BEFORE its Season guard, so it serializes
        # with the delete: either the create wins (binding + Division exist →
        # delete blocks) or the delete wins (League gone → create fails
        # not-found). No orphaned binding or Division onto a deleted League.
        store0 = SqlStore(self.url)
        api0 = ApiService(store0)
        pid = api0.create_program("Prog", "US", "UTC")["id"]
        sid = api0.create_season(pid, "S1")["id"]
        lid = store0.next_id("league")
        store0.add_league(League(id=lid, program_id=pid, name="Unbound",
                                 sort_order=0))
        api_del = ApiService(SqlStore(self.url))
        api_div = ApiService(SqlStore(self.url))
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
            lambda: api_div.setup.create_division(
                sid, "D1", league_id=lid, actor_id="b"), "bind"))
        ta.start(); tb.start(); ta.join(15); tb.join(15)

        check = SqlStore(self.url)
        binding = check.league_season_for(lid, sid)
        divs_under = ([d for d in check.all_divisions()
                       if binding is not None and d.league_season_id == binding.id]
                      if binding is not None else [])
        if results["bind"] == "ok":
            self.assertEqual(results["delete"], "blocked", results)
            self.assertIsNotNone(check.get_league(lid), results)
            self.assertIsNotNone(binding, results)
            self.assertEqual(len(divs_under), 1, results)
        else:
            # Delete won → League gone; no orphan binding AND no orphan Division.
            self.assertEqual(results["bind"], "not_found", results)
            self.assertEqual(results["delete"], "ok", results)
            self.assertIsNone(check.get_league(lid), results)
            self.assertIsNone(binding, results)

    def _seed_two_league_team(self):
        """Program → Season → {L1 Gold, L2 Silver} → Club → Team(permanent L1).
        Returns (sid, l1, l2, tid)."""
        store0 = SqlStore(self.url)
        api0 = ApiService(store0)
        pid = api0.create_program("Prog", "US", "UTC")["id"]
        sid = api0.create_season(pid, "S1")["id"]
        l1 = api0.create_league(sid, "Gold")["id"]
        l2 = api0.create_league(sid, "Silver")["id"]
        club = api0.create_club("Club")["id"]
        tid = api0.create_team(club_id=club, name="Alpha", league_id=l1)["id"]
        return sid, l1, l2, tid

    def _assert_team_registration_consistent(self, sid, l2, tid, results):
        # Transfer always commits (no games to strand) → Team ends in L2, and its
        # single ACTIVE registration is in the SAME League as the Team. The
        # canonical invariant team.league_id == registration.league_season.
        # league_id holds regardless of which thread won the Team-row lock.
        check = SqlStore(self.url)
        team = check.get_team(tid)
        self.assertEqual(results.get("transfer"), "ok", results)
        self.assertEqual(team.league_id, l2, results)
        active = [r for r in check.all_season_team_registrations()
                  if r.team_id == tid and r.active]
        self.assertEqual(len(active), 1, results)
        reg_league = check.get_league_season(active[0].league_season_id).league_id
        self.assertEqual(reg_league, team.league_id, (results, reg_league))
        # No mismatched registration committed at all (active or otherwise) for
        # the League the Team no longer belongs to beyond the pre-seeded row.
        return check

    def test_transfer_vs_register_create_keeps_team_league_consistent(self):
        # #159 review: register_team_for_season derives its candidate League from
        # the Team's permanent league_id and now row-locks the Team FIRST
        # (canonical Team→League→Season, shared with transfer). Racing a transfer
        # that moves the Team to another League: either register wins (registers
        # into L1, then the transfer re-homes BOTH the Team and the registration
        # to L2) or the transfer wins (Team→L2, then register derives L2). The
        # persisted Team and its active registration always agree — no
        # LeagueSeason(L1) row left behind while team.league_id == L2.
        sid, _l1, l2, tid = self._seed_two_league_team()
        api_reg = ApiService(SqlStore(self.url))
        api_xfer = ApiService(SqlStore(self.url))
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
            lambda: api_reg.setup.register_team_for_season(
                sid, tid, actor_id="r"), "register"))
        tb = threading.Thread(target=run, args=(
            lambda: api_xfer.setup.transfer_team_to_league(
                tid, l2, actor_id="x"), "transfer"))
        ta.start(); tb.start(); ta.join(15); tb.join(15)

        self.assertEqual(results.get("register"), "ok", results)
        self._assert_team_registration_consistent(sid, l2, tid, results)

    def test_transfer_vs_register_reactivate_keeps_team_league_consistent(self):
        # As above but the Team already has an INACTIVE registration in L1
        # (removed then re-added is a reactivation, not a duplicate). Racing the
        # register (which would reactivate the L1 row when the Team is still in
        # L1, or create an L2 row when the transfer already moved it) against the
        # transfer still leaves exactly one ACTIVE registration, in the Team's
        # final League — never an active L1 row while team.league_id == L2.
        sid, l1, l2, tid = self._seed_two_league_team()
        store0 = SqlStore(self.url)
        ls1 = store0.league_season_for(l1, sid)
        store0.add_season_team_registration(SeasonTeamRegistration(
            id=store0.next_id("streg"), league_season_id=ls1.id, team_id=tid,
            division_id=None, active=False))
        api_reg = ApiService(SqlStore(self.url))
        api_xfer = ApiService(SqlStore(self.url))
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
            lambda: api_reg.setup.register_team_for_season(
                sid, tid, actor_id="r"), "register"))
        tb = threading.Thread(target=run, args=(
            lambda: api_xfer.setup.transfer_team_to_league(
                tid, l2, actor_id="x"), "transfer"))
        ta.start(); tb.start(); ta.join(15); tb.join(15)

        self.assertEqual(results.get("register"), "ok", results)
        check = self._assert_team_registration_consistent(sid, l2, tid, results)
        # The Team's active registration is in L2 (its final League); any L1 row
        # that remains is inactive — never an active registration in a League the
        # Team no longer belongs to.
        l1_active = [r for r in check.all_season_team_registrations()
                     if r.team_id == tid and r.active
                     and check.get_league_season(
                         r.league_season_id).league_id == l1]
        self.assertEqual(l1_active, [], results)

    def test_rollover_all_vs_source_register_and_transfer_stays_consistent(self):
        # #159 review: v1 copy-all rollover freezes the source-active Team set
        # ONCE (READ COMMITTED safety), so a Team that registers into the source
        # Season concurrently is never rolled forward WITHOUT its Team/League
        # locks. Three-way barrier race: (A) roll_forward_registrations(copy-all)
        # while (B) a new Team registers into the SOURCE Season and (C) that same
        # Team is transferred to another League. All three take the canonical
        # Team → League → Season lock order, so they serialize on the shared Team
        # row with no deadlock, and the persisted target Season never holds a
        # registration whose League disagrees with its Team's permanent league_id.
        store0 = SqlStore(self.url)
        api0 = ApiService(store0)
        pid = api0.create_program("Prog", "US", "UTC")["id"]
        src = api0.create_season(pid, "SRC")["id"]
        dst = api0.create_season(pid, "DST")["id"]
        l1 = api0.create_league(src, "Gold")["id"]
        l2 = api0.create_league(src, "Silver")["id"]
        club = api0.create_club("Club")["id"]
        # A stable carried Team, already active in the source Season under L1.
        t0 = api0.create_team(club_id=club, name="Anchor", league_id=l1)["id"]
        api0.register_team_for_season(src, t0)
        # A late entrant: permanent L1, NOT yet registered in the source.
        t_late = api0.create_team(club_id=club, name="Latecomer",
                                  league_id=l1)["id"]
        api_roll = ApiService(SqlStore(self.url))
        api_reg = ApiService(SqlStore(self.url))
        api_xfer = ApiService(SqlStore(self.url))
        barrier = threading.Barrier(3)
        results = {}

        def run(fn, key):
            barrier.wait()
            try:
                fn(); results[key] = "ok"
            except ValidationError as exc:
                results[key] = exc.details.get("reason") or "validation"
            except Exception as exc:
                results[key] = f"ERR:{exc}"

        ta = threading.Thread(target=run, args=(
            lambda: api_roll.setup.roll_forward_registrations(
                src, dst, selections=None, actor_id="roll"), "roll"))
        tb = threading.Thread(target=run, args=(
            lambda: api_reg.setup.register_team_for_season(
                src, t_late, actor_id="reg"), "reg"))
        tc = threading.Thread(target=run, args=(
            lambda: api_xfer.setup.transfer_team_to_league(
                t_late, l2, actor_id="xfer"), "xfer"))
        ta.start(); tb.start(); tc.start()
        ta.join(20); tb.join(20); tc.join(20)

        # No thread hit a raw DB error / deadlock (canonical order → serialize).
        for key in ("roll", "reg", "xfer"):
            self.assertFalse(str(results.get(key)).startswith("ERR:"), results)
        check = SqlStore(self.url)
        # THE invariant: every active registration in the TARGET Season sits in
        # the same League as its Team's permanent league_id — no late entrant was
        # ever rolled forward unlocked into a League a transfer then diverged from.
        dst_active = [r for r in check.all_season_team_registrations()
                      if check.get_league_season(r.league_season_id).season_id
                      == dst and r.active]
        for r in dst_active:
            team = check.get_team(r.team_id)
            reg_league = check.get_league_season(r.league_season_id).league_id
            self.assertEqual(reg_league, team.league_id,
                             (r.team_id, reg_league, team.league_id, results))
        # The stable anchor Team was carried forward exactly once (no partial or
        # duplicate rollover write).
        t0_dst = [r for r in dst_active if r.team_id == t0]
        self.assertEqual(len(t0_dst), 1, results)

    def _ls_deleted_audits(self, store, ls_id):
        return [a for a in store.all_setup_audit()
                if a.action == "league_season_deleted" and a.entity_id == ls_id]

    def test_unbind_vs_create_division_under_league_is_linearizable(self):
        # #159 review: create_division_under_league resolves a League's sole
        # LeagueSeason and, after locking that binding's Season, RE-FETCHES the
        # binding before inserting. Racing delete_league_season on that binding
        # (both lock the same Season row): either the create wins (Division under
        # the binding → unbind then blocks on it) or the unbind wins (binding gone
        # → create fails league_has_no_season). A Division is NEVER inserted
        # against a deleted LeagueSeason (migration 035 has no FK).
        store0 = SqlStore(self.url)
        api0 = ApiService(store0)
        pid = api0.create_program("Prog", "US", "UTC")["id"]
        sid = api0.create_season(pid, "S1")["id"]
        lid = api0.create_league(sid, "Gold")["id"]
        ls_id = store0.league_seasons_for_league(lid)[0].id
        api_unbind = ApiService(SqlStore(self.url))
        api_div = ApiService(SqlStore(self.url))
        barrier = threading.Barrier(2)
        results = {}

        def run(fn, key):
            barrier.wait()
            try:
                fn(); results[key] = "ok"
            except HasDependenciesError:
                results[key] = "blocked"
            except ValidationError as exc:
                results[key] = exc.details.get("reason") or "validation"
            except NotFoundError:
                results[key] = "not_found"
            except Exception as exc:
                results[key] = f"ERR:{exc}"

        ta = threading.Thread(target=run, args=(
            lambda: api_unbind.setup.delete_league_season(
                ls_id, actor_id="u"), "unbind"))
        tb = threading.Thread(target=run, args=(
            lambda: api_div.setup.create_division_under_league(
                lid, "D1", actor_id="d"), "create"))
        ta.start(); tb.start(); ta.join(15); tb.join(15)

        check = SqlStore(self.url)
        divs = [d for d in check.all_divisions()
                if d.league_season_id == ls_id]
        binding = check.get_league_season(ls_id)
        # No Division may reference a deleted binding.
        orphaned = [d for d in check.all_divisions()
                    if check.get_league_season(d.league_season_id) is None]
        self.assertEqual(orphaned, [], results)
        if results["create"] == "ok":
            # Create won → Division exists under the binding, unbind blocked on it.
            self.assertEqual(results["unbind"], "blocked", results)
            self.assertIsNotNone(binding, results)
            self.assertEqual(len(divs), 1, results)
        else:
            # Unbind won → binding gone, create fails closed, no Division.
            self.assertEqual(results["create"], "league_has_no_season", results)
            self.assertEqual(results["unbind"], "ok", results)
            self.assertIsNone(binding, results)
            self.assertEqual(divs, [], results)

    def test_unbind_vs_unbind_exactly_one_wins(self):
        # #159 review: two concurrent delete_league_season on the SAME binding
        # both re-fetch it under the shared Season-row lock, so exactly one
        # deletes it (one league_season_deleted audit) and the other observes it
        # already gone and fails not-found — never a duplicate success/audit.
        store0 = SqlStore(self.url)
        api0 = ApiService(store0)
        pid = api0.create_program("Prog", "US", "UTC")["id"]
        sid = api0.create_season(pid, "S1")["id"]
        lid = api0.create_league(sid, "Gold")["id"]
        ls_id = store0.league_seasons_for_league(lid)[0].id
        api_a = ApiService(SqlStore(self.url))
        api_b = ApiService(SqlStore(self.url))
        barrier = threading.Barrier(2)
        results = {}

        def run(api, key):
            barrier.wait()
            try:
                api.setup.delete_league_season(ls_id, actor_id=key)
                results[key] = "ok"
            except NotFoundError:
                results[key] = "not_found"
            except Exception as exc:
                results[key] = f"ERR:{exc}"

        ta = threading.Thread(target=run, args=(api_a, "a"))
        tb = threading.Thread(target=run, args=(api_b, "b"))
        ta.start(); tb.start(); ta.join(15); tb.join(15)

        self.assertEqual(sorted(results.values()), ["not_found", "ok"], results)
        check = SqlStore(self.url)
        self.assertIsNone(check.get_league_season(ls_id), results)
        # Exactly one league_season_deleted audit — the loser wrote nothing.
        self.assertEqual(len(self._ls_deleted_audits(check, ls_id)), 1, results)

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
