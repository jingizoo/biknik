"""Season archive/read-only lifecycle (#159 Slice 1).

A Season gains an explicit lifecycle state (``active`` | ``archived``). Archiving
turns it into a read-only historical record: no new registrations, venue access,
Leagues, Divisions, Games, or roll-forward-target rows may be written until an
authorized, *reasoned* reopen. Archived Seasons stay fully readable and retain
all prior history. Transitions are audited. Covered on Memory, SQLite, and
PostgreSQL, plus the HTTP contract (authz + reason + strict body + write-guard).
"""

import json
import os
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from helpers import BACKEND, commit_fresh_draft  # noqa: F401  (BACKEND: sys.path)

from hockey_scheduler.api import ApiService
from hockey_scheduler.domain.enums import SeasonStatus
from hockey_scheduler.domain.errors import (
    HasDependenciesError,
    NotFoundError,
    ValidationError,
)
from hockey_scheduler.domain.setup_models import Program, Season
from hockey_scheduler.store import InMemoryStore, SqlStore
from hockey_scheduler.web.server import STATE, Handler


def _backends():
    stores = [("memory", InMemoryStore()), ("sqlite", SqlStore(":memory:"))]
    url = os.environ.get("TEST_DATABASE_URL")
    if url:
        pg = SqlStore(url)
        pg.reset_schema()
        stores.append(("postgres", pg))
    return stores


def _fixture(api, season_name="S1"):
    """Program → Season → League → Division → Club → two registered Teams →
    Venue, all built through the real (guarded) create paths while active."""
    pid = api.create_program("Prog", "US", "UTC")["id"]
    sid = api.create_season(pid, season_name)["id"]
    lid = api.create_league(sid, "Gold")["id"]
    did = api.create_division(sid, "D1", league_id=lid)["id"]
    club = api.create_club("Club")["id"]
    t1 = api.create_team(club_id=club, name="Alpha", league_id=lid)["id"]
    t2 = api.create_team(club_id=club, name="Bravo", league_id=lid)["id"]
    api.register_team_for_season(sid, t1, division_id=did)
    api.register_team_for_season(sid, t2, division_id=did)
    ven = api.create_venue("Rink")["id"]
    return {"pid": pid, "sid": sid, "lid": lid, "did": did, "club": club,
            "t1": t1, "t2": t2, "ven": ven}


def _season_audits(store):
    return [a.action for a in store.all_setup_audit()
            if a.action in ("season_archived", "season_reopened")]


class SeasonLifecycleTest(unittest.TestCase):
    # -- lifecycle transitions + audit --------------------------------------
    def test_archive_then_reopen_roundtrip(self):
        for label, store in _backends():
            api = ApiService(store)
            fx = _fixture(api)
            sid = fx["sid"]
            self.assertEqual(store.get_season(sid).status, SeasonStatus.ACTIVE, label)

            arch = api.setup.archive_season(sid, actor_id="admin", reason="season over")
            self.assertEqual(arch.status, SeasonStatus.ARCHIVED, label)
            self.assertIsNotNone(arch.archived_at, label)
            # DTO exposes the lifecycle state as a plain string.
            self.assertEqual(api.get_setup_overview_v2()["seasons"][0]["status"],
                             "archived", label)

            reopen = api.setup.reopen_season(sid, actor_id="admin", reason="correction")
            self.assertEqual(reopen.status, SeasonStatus.ACTIVE, label)
            self.assertIsNone(reopen.archived_at, label)
            self.assertEqual(_season_audits(store),
                             ["season_archived", "season_reopened"], label)

    def test_reopen_requires_reason(self):
        for label, store in _backends():
            api = ApiService(store)
            sid = _fixture(api)["sid"]
            api.setup.archive_season(sid, actor_id="admin")
            for bad in (None, "", "   "):
                with self.assertRaises(ValidationError) as ctx:
                    api.setup.reopen_season(sid, actor_id="admin", reason=bad)
                self.assertEqual(ctx.exception.details.get("reason"),
                                 "reason_required", (label, bad))
            self.assertEqual(store.get_season(sid).status,
                             SeasonStatus.ARCHIVED, label)  # unchanged

    def test_double_transitions_rejected(self):
        for label, store in _backends():
            api = ApiService(store)
            sid = _fixture(api)["sid"]
            api.setup.archive_season(sid, actor_id="admin")
            with self.assertRaises(ValidationError) as ctx:
                api.setup.archive_season(sid, actor_id="admin")
            self.assertEqual(ctx.exception.details.get("reason"),
                             "season_already_archived", label)
            api.setup.reopen_season(sid, actor_id="admin", reason="x")
            with self.assertRaises(ValidationError) as ctx:
                api.setup.reopen_season(sid, actor_id="admin", reason="x")
            self.assertEqual(ctx.exception.details.get("reason"),
                             "season_not_archived", label)

    # -- read-only enforcement (zero mutation) ------------------------------
    def test_archived_season_blocks_writes(self):
        for label, store in _backends():
            api = ApiService(store)
            fx = _fixture(api)
            sid = fx["sid"]
            regs0 = len(store.registrations_for_season(sid))
            divs0 = len(store.all_divisions())
            ls0 = len(store.all_league_seasons())
            sva0 = len(store.all_season_venue_access())
            games0 = len(store.all_games())
            api.setup.archive_season(sid, actor_id="admin", reason="done")

            def blocked(fn, note):
                with self.assertRaises(ValidationError) as ctx:
                    fn()
                self.assertEqual(ctx.exception.details.get("reason"),
                                 "season_archived", (label, note))

            blocked(lambda: api.setup.register_team_for_season(
                sid, fx["t1"], division_id=fx["did"]), "register")
            blocked(lambda: api.setup.create_division(sid, "D2"), "division")
            blocked(lambda: api.setup.create_league(sid, "Silver"), "league")
            blocked(lambda: api.setup.create_league_season(fx["lid"], sid),
                    "league_season")
            blocked(lambda: api.setup.grant_season_venue_access(sid, fx["ven"]),
                    "venue_access")
            # create_game guards first, before any team/slot resolution.
            blocked(lambda: api.setup.create_game(
                sid, fx["did"], fx["t1"], fx["t2"], "slot_x"), "game")

            # Nothing was written by any blocked call.
            self.assertEqual(len(store.registrations_for_season(sid)), regs0, label)
            self.assertEqual(len(store.all_divisions()), divs0, label)
            self.assertEqual(len(store.all_league_seasons()), ls0, label)
            self.assertEqual(len(store.all_season_venue_access()), sva0, label)
            self.assertEqual(len(store.all_games()), games0, label)

    def test_rollforward_into_archived_target_blocked(self):
        for label, store in _backends():
            api = ApiService(store)
            src = _fixture(api, season_name="From")
            # A second Season in the SAME program, archived, as the target.
            dst_sid = api.create_season(src["pid"], "To")["id"]
            api.setup.archive_season(dst_sid, actor_id="admin", reason="closed")
            regs0 = len(store.registrations_for_season(dst_sid))
            with self.assertRaises(ValidationError) as ctx:
                api.setup.roll_forward_registrations(src["sid"], dst_sid,
                                                     actor_id="admin")
            self.assertEqual(ctx.exception.details.get("reason"),
                             "season_archived", label)
            self.assertEqual(len(store.registrations_for_season(dst_sid)),
                             regs0, label)  # zero copied into the archived target

    def test_archived_history_readable_and_other_seasons_writable(self):
        for label, store in _backends():
            api = ApiService(store)
            fx = _fixture(api, season_name="Archived")
            other = _fixture(api, season_name="Live")
            api.setup.archive_season(fx["sid"], actor_id="admin", reason="done")

            # Archived Season's history is intact and still readable.
            self.assertEqual(len(store.registrations_for_season(fx["sid"])), 2, label)
            self.assertIsNotNone(store.get_season(fx["sid"]), label)
            names = {s["name"]: s["status"]
                     for s in api.get_setup_overview_v2()["seasons"]}
            self.assertEqual(names["Archived"], "archived", label)
            self.assertEqual(names["Live"], "active", label)

            # A different active Season is unaffected — writes still succeed.
            api.setup.create_division(other["sid"], "D2")  # no raise
            self.assertTrue(any(d.name == "D2" for d in store.all_divisions()), label)


class SeasonArchivedGameWriteGuardTest(unittest.TestCase):
    """Every Game/roster/substitute/reschedule/venue/import write into an
    archived Season fails closed (season_archived) with zero mutation (#159)."""

    def _game_fixture(self, api):
        from hockey_scheduler.domain.enums import Position
        pid = api.create_program("Prog", "US", "UTC")["id"]
        sid = api.create_season(pid, "S1")["id"]
        lid = api.create_league(sid, "Gold")["id"]
        did = api.create_division(sid, "D1", league_id=lid)["id"]
        club = api.create_club("Club")["id"]
        t1 = api.create_team(club_id=club, name="Alpha", league_id=lid)["id"]
        t2 = api.create_team(club_id=club, name="Bravo", league_id=lid)["id"]
        api.register_team_for_season(sid, t1, division_id=did)
        api.register_team_for_season(sid, t2, division_id=did)
        ven = api.create_venue("Rink")["id"]
        access = api.grant_season_venue_access(sid, ven)["id"]
        rink = api.create_rink(ven, "Sheet 1")["id"]
        slot = api.create_ice_slot(
            rink, "2026-10-01T18:00:00+00:00", "2026-10-01T19:00:00+00:00")["id"]
        game = api.create_game(sid, did, t1, t2, slot)["id"]
        p1 = api.setup.add_player(t1, "Skater One", Position.FORWARD,
                                  actor_id="a").id
        return {"sid": sid, "did": did, "t1": t1, "t2": t2, "ven": ven,
                "access": access, "slot": slot, "game": game, "p1": p1}

    def test_all_game_owned_writes_blocked_on_archived_season(self):
        for label, store in _backends():
            api = ApiService(store)
            fx = self._game_fixture(api)
            sid, gid = fx["sid"], fx["game"]
            api.setup.archive_season(sid, actor_id="admin", reason="done")

            games0 = [(g.id, g.published, g.cancelled, g.ice_slot_id)
                      for g in store.all_games()]
            audits0 = len(store.all_setup_audit())
            regs0 = len(store.registrations_for_season(sid))

            def blocked(fn, note):
                with self.assertRaises(ValidationError) as ctx:
                    fn()
                self.assertEqual(ctx.exception.details.get("reason"),
                                 "season_archived", (label, note))

            blocked(lambda: api.setup.publish_game(gid, actor_id="a"), "publish")
            blocked(lambda: api.setup.move_game(gid, fx["slot"], actor_id="a"),
                    "move")
            blocked(lambda: api.roster.cancel_game(gid, actor_id="a"), "cancel")
            blocked(lambda: api.setup.record_result(gid, 3, 2, actor_id="a"),
                    "record_result")
            blocked(lambda: api.setup.request_reschedule(
                gid, fx["t1"], "x", actor_id="a"), "reschedule")
            blocked(lambda: api.roster.select_roster(
                gid, [fx["p1"]], actor_id="a"), "select_roster")
            blocked(lambda: api.roster.enroll_substitute(
                gid, fx["p1"], actor_id="a"), "enroll_substitute")
            blocked(lambda: api.roster.lock_roster(gid, actor_id="a"), "lock")
            blocked(lambda: api.setup.delete_game(gid, actor_id="a"), "delete_game")
            blocked(lambda: api.setup.revoke_season_venue_access(
                fx["access"], actor_id="a"), "revoke_venue")

            # Zero mutation from any blocked write.
            self.assertEqual([(g.id, g.published, g.cancelled, g.ice_slot_id)
                              for g in store.all_games()], games0, label)
            self.assertEqual(len(store.all_setup_audit()), audits0, label)
            self.assertEqual(len(store.registrations_for_season(sid)), regs0, label)
            self.assertIsNone(store.result_for_game(gid), label)
            self.assertEqual(store.roster_for_game(gid), [], label)


class SeasonArchivedRegistrationDivisionGuardTest(unittest.TestCase):
    """Registration reassignment/removal, Division delete, transfer-freeze,
    reminders, and the draft batches are all blocked on an archived Season with
    zero mutation (#159 re-review — completeness beyond the game paths)."""

    def _reg_id(self, store, sid, tid):
        r = store.registration_for_team_in_season(sid, tid)
        return r.id if r else None

    def test_registration_and_division_writes_blocked(self):
        for label, store in _backends():
            api = ApiService(store)
            fx = SeasonArchivedGameWriteGuardTest()._game_fixture(api)
            sid = fx["sid"]
            reg = self._reg_id(store, sid, fx["t1"])
            lid = store.all_league_seasons()[0].league_id
            api.setup.archive_season(sid, actor_id="admin", reason="done")
            regs0 = [(r.id, r.active, r.league_season_id, r.division_id)
                     for r in store.all_season_team_registrations()]
            divs0 = {d.id for d in store.all_divisions()}
            audits0 = len(store.all_setup_audit())

            def blocked(fn, note):
                with self.assertRaises(ValidationError) as ctx:
                    fn()
                self.assertEqual(ctx.exception.details.get("reason"),
                                 "season_archived", (label, note))

            blocked(lambda: api.setup.assign_season_team_league(
                reg, lid, actor_id="a"), "assign_league")
            blocked(lambda: api.setup.assign_season_team_division(
                reg, fx["did"], actor_id="a"), "assign_division")
            blocked(lambda: api.setup.unregister_team_from_season(
                reg, actor_id="a"), "unregister")
            blocked(lambda: api.setup.delete_season_team_registration(
                reg, actor_id="a"), "delete_registration")
            blocked(lambda: api.setup.delete_division(fx["did"], actor_id="a"),
                    "delete_division")

            self.assertEqual([(r.id, r.active, r.league_season_id, r.division_id)
                              for r in store.all_season_team_registrations()],
                             regs0, label)
            self.assertEqual({d.id for d in store.all_divisions()}, divs0, label)
            self.assertEqual(len(store.all_setup_audit()), audits0, label)

    def test_transfer_freezes_archived_registration(self):
        # A team registered in an ARCHIVED Season keeps that (frozen) League even
        # when the team is transferred to a new League — only end_date used to
        # count as historical; ARCHIVED now does too.
        for label, store in _backends():
            api = ApiService(store)
            pid = api.create_program("P", "US", "UTC")["id"]
            sid = api.create_season(pid, "S1")["id"]
            l1 = api.create_league(sid, "Gold")["id"]
            l2 = api.create_league(sid, "Silver")["id"]
            did = api.create_division(sid, "D1", league_id=l1)["id"]
            club = api.create_club("Club")["id"]
            team = api.create_team(club_id=club, name="Alpha", league_id=l1)["id"]
            api.register_team_for_season(sid, team, division_id=did)
            reg = self._reg_id(store, sid, team)
            ls_before = store.get_season_team_registration(reg).league_season_id
            api.setup.archive_season(sid, actor_id="admin", reason="done")

            # Transfer the team to L2 — the archived Season's registration must
            # NOT move (stays in L1's LeagueSeason); the transfer itself succeeds.
            api.setup.transfer_team_to_league(team, l2, actor_id="a")
            self.assertEqual(
                store.get_season_team_registration(reg).league_season_id,
                ls_before, label)  # frozen

    def test_reminders_and_draft_batches_blocked(self):
        for label, store in _backends():
            api = ApiService(store)
            fx = SeasonArchivedGameWriteGuardTest()._game_fixture(api)
            sid = fx["sid"]
            notifs0 = len(store.all_notifications_feed())
            # A third team (#206 slice 1): the fixture's only pairing (t1 vs
            # t2) already has a real game, so the scheduler would otherwise
            # find nothing left to (re)propose onto the fresh slot below.
            t3 = api.create_team(name="Charlie", division_id=fx["did"])["id"]
            api.register_team_for_season(sid, t3, division_id=fx["did"])
            # The fixture's only slot is taken by its manual game, so give the
            # scheduler a free slot; otherwise the draft is empty and the batch
            # guards would have nothing to act on (a vacuous pass).
            rink_id = store.all_rinks()[0].id
            api.create_ice_slot(
                rink_id, "2026-10-08T18:00:00+00:00",
                "2026-10-08T19:00:00+00:00")
            # A draft exists to discard/commit-race; build one while active.
            draft = commit_fresh_draft(api, division_id=fx["did"], actor_id="a")
            self.assertNotIn("error", draft, (label, draft))
            self.assertTrue(draft["created"], (label, "expected a draft game"))
            games_after_draft = len(store.all_games())
            api.setup.archive_season(sid, actor_id="admin", reason="done")

            # Facade batches + reminder return the structured error, no mutation.
            r1 = api.remind_unresponded(fx["game"], fx["t1"], actor_id="a")
            self.assertEqual(r1["error"]["details"]["reason"], "season_archived",
                             label)
            r2 = commit_fresh_draft(api, division_id=fx["did"], actor_id="a")
            self.assertEqual(r2["error"]["details"]["reason"], "season_archived",
                             label)
            r3 = api.discard_draft_games(all_drafts=True, actor_id="a")
            self.assertEqual(r3["error"]["details"]["reason"], "season_archived",
                             label)
            self.assertEqual(len(store.all_notifications_feed()), notifs0, label)
            self.assertEqual(len(store.all_games()), games_after_draft, label)


class SeasonArchivedDeleteAndTransferGuardTest(unittest.TestCase):
    """Deleting an archived Season is blocked (read-only history must be
    retained), and a Team transfer — direct or import-driven — freezes an
    archived Season's registration by reading the Season under its row lock, so
    an archived registration is never rewritten (#159 re-review)."""

    def _reg_id(self, store, sid, tid):
        r = store.registration_for_team_in_season(sid, tid)
        return r.id if r else None

    def test_delete_archived_season_blocked_zero_mutation(self):
        for label, store in _backends():
            api = ApiService(store)
            pid = api.create_program("P", "US", "UTC")["id"]
            sid = api.create_season(pid, "S1")["id"]  # empty → otherwise deletable
            api.setup.archive_season(sid, actor_id="admin", reason="done")
            audits0 = len(store.all_setup_audit())
            with self.assertRaises(ValidationError) as ctx:
                api.setup.delete_season(sid, actor_id="a")
            self.assertEqual(ctx.exception.details.get("reason"),
                             "season_archived", label)
            self.assertIsNotNone(store.get_season(sid), label)  # history retained
            self.assertEqual(len(store.all_setup_audit()), audits0, label)
            self.assertNotIn("season_deleted",
                             [a.action for a in store.all_setup_audit()], label)

    def test_delete_active_empty_season_still_works(self):
        # The guard doesn't over-block: an ACTIVE empty Season still deletes.
        for label, store in _backends():
            api = ApiService(store)
            pid = api.create_program("P", "US", "UTC")["id"]
            sid = api.create_season(pid, "S1")["id"]
            api.setup.delete_season(sid, actor_id="a")
            self.assertIsNone(store.get_season(sid), label)

    def test_delete_league_bound_to_archived_season_blocked(self):
        # An otherwise-empty permanent League bound to an archived Season can't
        # be deleted — that would drop the archived Season's LeagueSeason
        # history. season_archived, with zero League/binding/audit mutation.
        for label, store in _backends():
            api = ApiService(store)
            pid = api.create_program("P", "US", "UTC")["id"]
            sid = api.create_season(pid, "S1")["id"]
            lid = api.create_league(sid, "Gold")["id"]  # binds a LeagueSeason
            api.setup.archive_season(sid, actor_id="admin", reason="done")
            bindings0 = {ls.id for ls in store.all_league_seasons()}
            audits0 = len(store.all_setup_audit())
            with self.assertRaises(ValidationError) as ctx:
                api.setup.delete_league(lid, actor_id="a")
            self.assertEqual(ctx.exception.details.get("reason"),
                             "season_archived", label)
            self.assertIsNotNone(store.get_league(lid), label)
            self.assertEqual({ls.id for ls in store.all_league_seasons()},
                             bindings0, label)  # binding history intact
            self.assertEqual(len(store.all_setup_audit()), audits0, label)
            self.assertNotIn("level_deleted",
                             [a.action for a in store.all_setup_audit()], label)

    def test_delete_league_archived_game_history_blocked(self):
        # Game-backed participation in an archived Season is frozen: the delete
        # fails season_archived before any scan, leaving League, binding, Game,
        # and audit untouched.
        for label, store in _backends():
            api = ApiService(store)
            fx = SeasonArchivedGameWriteGuardTest()._game_fixture(api)
            sid = fx["sid"]
            lid = store.all_league_seasons()[0].league_id
            api.setup.archive_season(sid, actor_id="admin", reason="done")
            games0 = {g.id for g in store.all_games()}
            bindings0 = {ls.id for ls in store.all_league_seasons()}
            audits0 = len(store.all_setup_audit())
            with self.assertRaises(ValidationError) as ctx:
                api.setup.delete_league(lid, actor_id="a")
            self.assertEqual(ctx.exception.details.get("reason"),
                             "season_archived", label)
            self.assertIsNotNone(store.get_league(lid), label)
            self.assertEqual({g.id for g in store.all_games()}, games0, label)
            self.assertEqual({ls.id for ls in store.all_league_seasons()},
                             bindings0, label)
            self.assertEqual(len(store.all_setup_audit()), audits0, label)

    def test_delete_league_active_game_blocks_on_game_dependency(self):
        # In an ACTIVE Season, a League with Game history blocks on the Game (not
        # season_archived) so the operator resolves it first — zero mutation.
        for label, store in _backends():
            api = ApiService(store)
            fx = SeasonArchivedGameWriteGuardTest()._game_fixture(api)
            lid = store.all_league_seasons()[0].league_id
            with self.assertRaises(HasDependenciesError) as ctx:
                api.setup.delete_league(lid, actor_id="a")
            types = {g["type"] for g in ctx.exception.details["dependencies"]}
            self.assertIn("game", types, (label, types))
            self.assertIsNotNone(store.get_league(lid), label)

    def test_bound_league_blocks_then_deletes_after_explicit_unbind(self):
        # A League bound to an ACTIVE Season blocks on its LeagueSeason binding
        # (itemized dependency, never a silent cascade). The explicit, audited
        # unbind then clears it and the now-unbound League deletes.
        for label, store in _backends():
            api = ApiService(store)
            pid = api.create_program("P", "US", "UTC")["id"]
            sid = api.create_season(pid, "S1")["id"]
            lid = api.create_league(sid, "Gold")["id"]
            ls_id = store.league_seasons_for_league(lid)[0].id
            r = api.delete_league(lid, actor_id="a")
            self.assertEqual(r["error"]["code"], "has_dependencies", label)
            self.assertIn("season binding",
                          {g["type"] for g in r["error"]["details"]["dependencies"]},
                          label)
            self.assertIsNotNone(store.get_league(lid), label)  # zero mutation
            self.assertIsNotNone(store.get_league_season(ls_id), label)
            # Explicit unbind, then the League deletes cleanly.
            api.setup.delete_league_season(ls_id, actor_id="a")
            self.assertIsNone(store.get_league_season(ls_id), label)
            api.setup.delete_league(lid, actor_id="a")
            self.assertIsNone(store.get_league(lid), label)

    def test_delete_league_season_blocked_on_archived_season(self):
        # Unbinding is itself read-only-guarded: an archived Season's binding is
        # frozen history — season_archived with zero mutation.
        for label, store in _backends():
            api = ApiService(store)
            pid = api.create_program("P", "US", "UTC")["id"]
            sid = api.create_season(pid, "S1")["id"]
            lid = api.create_league(sid, "Gold")["id"]
            ls_id = store.league_seasons_for_league(lid)[0].id
            api.setup.archive_season(sid, actor_id="admin", reason="done")
            audits0 = len(store.all_setup_audit())
            with self.assertRaises(ValidationError) as ctx:
                api.setup.delete_league_season(ls_id, actor_id="a")
            self.assertEqual(ctx.exception.details.get("reason"),
                             "season_archived", label)
            self.assertIsNotNone(store.get_league_season(ls_id), label)
            self.assertEqual(len(store.all_setup_audit()), audits0, label)

    def test_delete_league_season_blocked_by_division(self):
        # A binding that still owns a Division is refused (dependency-gated),
        # zero mutation.
        for label, store in _backends():
            api = ApiService(store)
            pid = api.create_program("P", "US", "UTC")["id"]
            sid = api.create_season(pid, "S1")["id"]
            lid = api.create_league(sid, "Gold")["id"]
            api.create_division(sid, "D1", league_id=lid)
            ls_id = store.league_seasons_for_league(lid)[0].id
            r = api.delete_league_season(ls_id, actor_id="a")
            self.assertEqual(r["error"]["code"], "has_dependencies", label)
            self.assertIn("division",
                          {g["type"] for g in r["error"]["details"]["dependencies"]},
                          label)
            self.assertIsNotNone(store.get_league_season(ls_id), label)

    def test_reunbind_and_division_after_unbind_fail_closed(self):
        # Deterministic parity (all backends) for the re-fetch-under-lock
        # invariant behind the unbind-vs-unbind / unbind-vs-create_division PG
        # barrier races: unbinding an already-unbound binding fails not-found
        # with NO second audit, and creating a Division under a League whose sole
        # binding was unbound fails closed (league_has_no_season) — never an
        # orphaned Division on a deleted LeagueSeason.
        for label, store in _backends():
            api = ApiService(store)
            pid = api.create_program("P", "US", "UTC")["id"]
            sid = api.create_season(pid, "S1")["id"]
            lid = api.create_league(sid, "Gold")["id"]
            ls_id = store.league_seasons_for_league(lid)[0].id

            def _del_audits():
                return [a for a in store.all_setup_audit()
                        if a.action == "league_season_deleted"
                        and a.entity_id == ls_id]

            api.setup.delete_league_season(ls_id, actor_id="a")
            self.assertIsNone(store.get_league_season(ls_id), label)
            self.assertEqual(len(_del_audits()), 1, label)
            # Re-unbind the now-gone binding: not-found, zero extra audit.
            with self.assertRaises(NotFoundError):
                api.setup.delete_league_season(ls_id, actor_id="b")
            self.assertEqual(len(_del_audits()), 1, label)
            # Division create against the League's absent sole binding fails
            # closed rather than orphaning a Division on the deleted binding.
            with self.assertRaises(ValidationError) as ctx:
                api.setup.create_division_under_league(lid, "D1", actor_id="c")
            self.assertEqual(ctx.exception.details.get("reason"),
                             "league_has_no_season", label)
            self.assertEqual(store.all_divisions(), [], label)

    def test_delete_league_blocked_by_permanent_team(self):
        # A League with a permanent Team (Team.league_id) but no seasonal records
        # blocks on the Team — never orphaned — with zero mutation.
        for label, store in _backends():
            api = ApiService(store)
            pid = api.create_program("P", "US", "UTC")["id"]
            sid = api.create_season(pid, "S1")["id"]
            lid = api.create_league(sid, "Gold")["id"]
            club = api.create_club("Club")["id"]
            tid = api.create_team(club_id=club, name="Alpha", league_id=lid)["id"]
            teams0 = {t.id for t in store.all_teams()}
            audits0 = len(store.all_setup_audit())
            r = api.delete_league(lid, actor_id="a")
            self.assertEqual(r["error"]["code"], "has_dependencies", label)
            types = {g["type"] for g in r["error"]["details"]["dependencies"]}
            self.assertIn("team", types, (label, types))
            self.assertIsNotNone(store.get_league(lid), label)
            self.assertEqual({t.id for t in store.all_teams()}, teams0, label)
            self.assertEqual(len(store.all_setup_audit()), audits0, label)
            self.assertIsNotNone(store.get_team(tid), label)

    def test_rollover_all_reads_source_registrations_once(self):
        # Structural parity (all backends) for the copy-all freeze (#159 review):
        # the source-active Team set is queried EXACTLY ONCE, so it cannot diverge
        # under READ COMMITTED and carry an unlocked late entrant. The former
        # double-read implementation queried the source twice and would fail this
        # on every backend — a deterministic regression guard independent of the
        # PostgreSQL barrier timing.
        for label, store in _backends():
            api = ApiService(store)
            pid = api.create_program("P", "US", "UTC")["id"]
            src = api.create_season(pid, "SRC")["id"]
            dst = api.create_season(pid, "DST")["id"]
            lid = api.create_league(src, "Gold")["id"]
            club = api.create_club("Club")["id"]
            tid = api.create_team(club_id=club, name="Alpha", league_id=lid)["id"]
            api.register_team_for_season(src, tid)
            orig = store.registrations_for_season
            reads = []

            def _counting(season_id, _orig=orig, _reads=reads):
                _reads.append(season_id)
                return _orig(season_id)

            store.registrations_for_season = _counting
            try:
                api.setup.roll_forward_registrations(
                    src, dst, selections=None, actor_id="roll")
            finally:
                store.registrations_for_season = orig
            # Exactly one read of the SOURCE season's registrations (the frozen
            # snapshot); target-season reads are a separate concern.
            self.assertEqual([s for s in reads if s == src], [src], label)

    def test_stale_registration_write_guards_parity(self):
        # Parity (all backends) for the re-fetch-under-lock registration guards
        # (#159 r14). Memory/SQLite serialize the whole transaction, so the race
        # the PostgreSQL barrier tests force can't occur here — but the guard
        # SEMANTICS must match: a no-op League/Division assignment never
        # resurrects an unregistered row or writes an audit, and a permanent
        # delete refuses a (re)active row.
        for label, store in _backends():
            api = ApiService(store)
            pid = api.create_program("P", "US", "UTC")["id"]
            sid = api.create_season(pid, "S")["id"]
            l1 = api.create_league(sid, "Gold")["id"]
            did = api.create_division(sid, "D1", league_id=l1)["id"]
            club = api.create_club("Club")["id"]
            tid = api.create_team(club_id=club, name="Alpha", league_id=l1)["id"]
            reg_id = api.register_team_for_season(sid, tid, division_id=did)["id"]
            api.setup.unregister_team_from_season(reg_id, actor_id="u")
            base_audits = len(store.all_setup_audit())
            # No-op League then Division assignment on the INACTIVE row: neither
            # reactivates it nor writes an audit.
            api.setup.assign_season_team_league(reg_id, l1, actor_id="a")
            api.setup.assign_season_team_division(reg_id, did, actor_id="a")
            reg = store.get_season_team_registration(reg_id)
            self.assertFalse(reg.active, label)                       # no revival
            self.assertEqual(len(store.all_setup_audit()), base_audits, label)
            # Reactivate, then a permanent delete must refuse the now-active row.
            api.register_team_for_season(sid, tid, division_id=did, actor_id="r")
            with self.assertRaises(ValidationError) as ctx:
                api.setup.delete_season_team_registration(reg_id, actor_id="d")
            self.assertEqual(ctx.exception.details.get("reason"),
                             "registration_active", label)
            self.assertIsNotNone(
                store.get_season_team_registration(reg_id), label)

    def test_transfer_freezes_an_already_unregistered_row_parity(self):
        # Parity (all backends) for the transfer re-fetch (#159 r15). Once a
        # registration is unregistered (inactive), a subsequent Team transfer
        # leaves it FROZEN in its historical LeagueSeason and excludes it from
        # registrations_moved — the Team's permanent League still moves. On
        # PostgreSQL the barrier test forces the concurrent ordering; here the
        # observable frozen-history contract must match on every backend.
        for label, store in _backends():
            api = ApiService(store)
            pid = api.create_program("P", "US", "UTC")["id"]
            s1 = api.create_season(pid, "S1")["id"]
            l1 = api.create_league(s1, "Gold")["id"]
            l2 = api.create_league(s1, "Silver")["id"]
            club = api.create_club("Club")["id"]
            tid = api.create_team(club_id=club, name="Alpha", league_id=l1)["id"]
            reg_id = api.register_team_for_season(s1, tid)["id"]
            api.setup.unregister_team_from_season(reg_id, actor_id="u")
            api.setup.transfer_team_to_league(tid, l2, actor_id="x")
            reg = store.get_season_team_registration(reg_id)
            self.assertFalse(reg.active, label)                  # stayed inactive
            self.assertEqual(
                store.get_league_season(reg.league_season_id).league_id, l1,
                label)                                           # frozen in L1
            self.assertEqual(store.get_team(tid).league_id, l2, label)  # moved
            moved = [a.detail.get("registrations_moved", [])
                     for a in store.all_setup_audit()
                     if a.action == "team_league_transferred"]
            self.assertTrue(all(reg_id not in m for m in moved), label)

    def test_import_driven_transfer_freezes_archived_registration(self):
        # The import path routes a permanent-League change through the same
        # locked _transfer_team_to_league_inner, so it too must freeze an
        # archived Season's registration rather than rewrite it.
        for label, store in _backends():
            api = ApiService(store)
            pid = api.create_program("P", "US", "UTC")["id"]
            sid = api.create_season(pid, "S1")["id"]
            l1 = api.create_league(sid, "Gold")["id"]
            l2 = api.create_league(sid, "Silver")["id"]
            did = api.create_division(sid, "D1", league_id=l1)["id"]
            club = api.create_club("Club")["id"]
            team = api.create_team(club_id=club, name="Alpha", league_id=l1)["id"]
            api.register_team_for_season(sid, team, division_id=did)
            reg = self._reg_id(store, sid, team)
            ls_before = store.get_season_team_registration(reg).league_season_id
            api.setup.archive_season(sid, actor_id="admin", reason="done")

            # Route the permanent-League change through the import helper.
            api.setup._transfer_team_to_league_inner(
                store.get_team(team), l2, actor_id="import")
            self.assertEqual(
                store.get_season_team_registration(reg).league_season_id,
                ls_before, label)  # frozen — never moved into the archived Season
            self.assertEqual(store.get_team(team).league_id, l2, label)  # team moved


class SeasonLifecycleReasonValidationTest(unittest.TestCase):
    """The lifecycle ``reason`` is type-validated and normalized BEFORE any
    mutation (#159 r3): archive accepts ``null`` or a string; reopen requires a
    nonblank string; every OTHER JSON type is a stable ``invalid_reason`` /
    ``field="reason"`` error with zero Season/audit change (never a 500 from
    calling ``.strip()`` on a non-string, never a silent coercion of a falsy
    non-string to "missing"). A valid string is trimmed and that trimmed value
    is what the audit records. Covered on Memory, SQLite, and PostgreSQL."""

    # JSON types that are neither null nor a string — every one is rejected,
    # including the *falsy* ones (False/0/[]/{}) that a truthiness test would
    # have silently swallowed and the *truthy* ones that would have 500'd.
    BAD_TYPES = [("boolean_true", True), ("boolean_false", False),
                 ("integer", 5), ("zero", 0), ("float", 1.5),
                 ("array", ["x"]), ("empty_array", []),
                 ("object", {"k": "v"}), ("empty_object", {})]

    def _seed(self, api):
        pid = api.create_program("P", "US", "UTC")["id"]
        return api.create_season(pid, "S1")["id"]

    def _archive_audit(self, store, sid):
        return [a for a in store.all_setup_audit()
                if a.action == "season_archived" and a.entity_id == sid][0]

    def _reopen_audit(self, store, sid):
        return [a for a in store.all_setup_audit()
                if a.action == "season_reopened" and a.entity_id == sid][0]

    def test_archive_rejects_nonstring_reason_zero_mutation(self):
        for label, store in _backends():
            api = ApiService(store)
            for tlabel, bad in self.BAD_TYPES:
                sid = self._seed(api)
                audits0 = len(store.all_setup_audit())
                with self.assertRaises(ValidationError) as ctx:
                    api.setup.archive_season(sid, actor_id="a", reason=bad)
                self.assertEqual(ctx.exception.details.get("reason"),
                                 "invalid_reason", (label, tlabel))
                self.assertEqual(ctx.exception.details.get("field"),
                                 "reason", (label, tlabel))
                s = store.get_season(sid)
                self.assertEqual(s.status, SeasonStatus.ACTIVE, (label, tlabel))
                self.assertIsNone(s.archived_at, (label, tlabel))
                self.assertEqual(len(store.all_setup_audit()), audits0,
                                 (label, tlabel))

    def test_archive_accepts_null_blank_and_trims(self):
        for label, store in _backends():
            api = ApiService(store)
            for reason in (None, "", "   "):
                sid = self._seed(api)
                api.setup.archive_season(sid, actor_id="a", reason=reason)
                self.assertEqual(store.get_season(sid).status,
                                 SeasonStatus.ARCHIVED, (label, repr(reason)))
                self.assertIsNone(self._archive_audit(store, sid).detail
                                  .get("reason"), (label, repr(reason)))
            sid = self._seed(api)
            api.setup.archive_season(sid, actor_id="a", reason="  done  ")
            self.assertEqual(self._archive_audit(store, sid).detail.get("reason"),
                             "done", label)

    def test_reopen_rejects_nonstring_reason_zero_mutation(self):
        for label, store in _backends():
            api = ApiService(store)
            for tlabel, bad in self.BAD_TYPES:
                sid = self._seed(api)
                api.setup.archive_season(sid, actor_id="a", reason="close")
                at0 = store.get_season(sid).archived_at
                audits0 = len(store.all_setup_audit())
                with self.assertRaises(ValidationError) as ctx:
                    api.setup.reopen_season(sid, actor_id="a", reason=bad)
                self.assertEqual(ctx.exception.details.get("reason"),
                                 "invalid_reason", (label, tlabel))
                self.assertEqual(ctx.exception.details.get("field"),
                                 "reason", (label, tlabel))
                s = store.get_season(sid)
                self.assertEqual(s.status, SeasonStatus.ARCHIVED, (label, tlabel))
                self.assertEqual(s.archived_at, at0, (label, tlabel))
                self.assertEqual(len(store.all_setup_audit()), audits0,
                                 (label, tlabel))

    def test_reopen_null_blank_requires_reason_zero_mutation(self):
        for label, store in _backends():
            api = ApiService(store)
            for reason in (None, "", "   "):
                sid = self._seed(api)
                api.setup.archive_season(sid, actor_id="a", reason="close")
                audits0 = len(store.all_setup_audit())
                with self.assertRaises(ValidationError) as ctx:
                    api.setup.reopen_season(sid, actor_id="a", reason=reason)
                self.assertEqual(ctx.exception.details.get("reason"),
                                 "reason_required", (label, repr(reason)))
                self.assertEqual(store.get_season(sid).status,
                                 SeasonStatus.ARCHIVED, (label, repr(reason)))
                self.assertEqual(len(store.all_setup_audit()), audits0,
                                 (label, repr(reason)))

    def test_reopen_trims_valid_reason(self):
        for label, store in _backends():
            api = ApiService(store)
            sid = self._seed(api)
            api.setup.archive_season(sid, actor_id="a", reason="close")
            api.setup.reopen_season(sid, actor_id="a", reason="  mistake  ")
            self.assertEqual(store.get_season(sid).status,
                             SeasonStatus.ACTIVE, label)
            self.assertEqual(self._reopen_audit(store, sid).detail.get("reason"),
                             "mistake", label)


class SeasonLifecycleHttpTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.thread.join(timeout=5)

    def _req(self, method, path, body=None, role="league_admin"):
        url = f"http://127.0.0.1:{self.port}{path}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method,
                                     headers={"Content-Type": "application/json"})
        if role is not None:
            req.add_header("X-Demo-Role", role)
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def _seed_season(self, name="S1"):
        """Seed a Program + Season straight into the live server store and
        return the season id, avoiding a long HTTP build for lifecycle tests.

        ``seed=False`` (clean slate) rather than the full Alpine dataset, so
        Program "p" is the ONLY Program in the store (#369). Every request in
        this class is an identity-less ``X-Demo-Role`` call with no session and
        therefore no persisted active context, so the context resolver falls
        back to the first authorized Program by id — with the demo dataset
        loaded that is one of its ``league_*`` Programs, and every archive /
        reopen / league-delete below then correctly refuses "p"'s records as
        outside the active context. Making "p" the only Program is how a
        context-less caller states which Program it is working in; the guard
        itself is untouched."""
        STATE.reset(seed=False)
        store = STATE.api.store
        store.add_program(Program(id="p", name="P"))
        store.add_season(Season(id="s", program_id="p", name=name))
        return "s"

    def test_archive_reopen_contract(self):
        sid = self._seed_season()
        # Archive (League Admin) → 200 + archived status.
        st, body = self._req("POST", f"/api/v2/setup/seasons/{sid}/archive",
                             {"reason": "season over"})
        self.assertEqual(st, 200, body)
        self.assertEqual(body["status"], "archived")
        # Reopen without a reason → 400 reason_required.
        st, body = self._req("POST", f"/api/v2/setup/seasons/{sid}/reopen", {})
        self.assertEqual(st, 400, body)
        self.assertEqual(body["error"]["details"]["reason"], "reason_required")
        # Reopen with a reason → 200 active.
        st, body = self._req("POST", f"/api/v2/setup/seasons/{sid}/reopen",
                             {"reason": "fix"})
        self.assertEqual(st, 200, body)
        self.assertEqual(body["status"], "active")

    def test_archive_requires_manage_setup(self):
        sid = self._seed_season()
        st, body = self._req("POST", f"/api/v2/setup/seasons/{sid}/archive",
                             {"reason": "x"}, role="coach")
        self.assertEqual(st, 403, body)
        self.assertEqual(STATE.api.store.get_season(sid).status,
                         SeasonStatus.ACTIVE)  # unchanged

    def test_archive_rejects_unknown_body_key(self):
        sid = self._seed_season()
        st, body = self._req("POST", f"/api/v2/setup/seasons/{sid}/archive",
                             {"reason": "x", "bogus": 1})
        self.assertEqual(st, 400, body)
        self.assertEqual(STATE.api.store.get_season(sid).status,
                         SeasonStatus.ACTIVE)

    def test_archive_unknown_season_404(self):
        self._seed_season()
        st, body = self._req("POST", "/api/v2/setup/seasons/missing/archive",
                             {"reason": "x"})
        self.assertEqual(st, 404, body)

    def test_archive_rejects_nonstring_reason_over_http(self):
        # boolean / number / array / object reasons → 400 invalid_reason with a
        # stable field, and the Season is untouched (no 500, no silent archive).
        for bad in (True, False, 5, 0, 1.5, ["x"], [], {"k": "v"}, {}):
            sid = self._seed_season()
            st, body = self._req("POST", f"/api/v2/setup/seasons/{sid}/archive",
                                 {"reason": bad})
            self.assertEqual(st, 400, (bad, body))
            self.assertEqual(body["error"]["details"]["reason"],
                             "invalid_reason", (bad, body))
            self.assertEqual(body["error"]["details"]["field"], "reason",
                             (bad, body))
            self.assertEqual(STATE.api.store.get_season(sid).status,
                             SeasonStatus.ACTIVE, bad)

    def test_archive_accepts_null_reason_over_http(self):
        sid = self._seed_season()
        st, body = self._req("POST", f"/api/v2/setup/seasons/{sid}/archive",
                             {"reason": None})
        self.assertEqual(st, 200, body)
        self.assertEqual(body["status"], "archived")
        aud = [a for a in STATE.api.store.all_setup_audit()
               if a.action == "season_archived"][0]
        self.assertIsNone(aud.detail.get("reason"))

    def test_reopen_rejects_nonstring_reason_over_http(self):
        for bad in (True, 5, ["x"], {"k": "v"}):
            sid = self._seed_season()
            STATE.api.setup.archive_season(sid, actor_id="admin", reason="c")
            st, body = self._req("POST", f"/api/v2/setup/seasons/{sid}/reopen",
                                 {"reason": bad})
            self.assertEqual(st, 400, (bad, body))
            self.assertEqual(body["error"]["details"]["reason"],
                             "invalid_reason", (bad, body))
            self.assertEqual(body["error"]["details"]["field"], "reason",
                             (bad, body))
            self.assertEqual(STATE.api.store.get_season(sid).status,
                             SeasonStatus.ARCHIVED, bad)

    def test_reopen_trims_reason_over_http(self):
        sid = self._seed_season()
        STATE.api.setup.archive_season(sid, actor_id="admin", reason="c")
        st, body = self._req("POST", f"/api/v2/setup/seasons/{sid}/reopen",
                             {"reason": "  fixed  "})
        self.assertEqual(st, 200, body)
        self.assertEqual(body["status"], "active")
        aud = [a for a in STATE.api.store.all_setup_audit()
               if a.action == "season_reopened"][0]
        self.assertEqual(aud.detail.get("reason"), "fixed")

    def test_delete_league_bound_to_archived_season_blocked_over_http(self):
        sid = self._seed_season()
        store = STATE.api.store
        lid = STATE.api.create_league(sid, "Gold")["id"]  # binds a LeagueSeason
        STATE.api.setup.archive_season(sid, actor_id="admin", reason="done")
        bindings0 = {ls.id for ls in store.all_league_seasons()}
        st, body = self._req("POST", f"/api/v2/setup/league/{lid}/delete")
        self.assertEqual(st, 400, body)
        self.assertEqual(body["error"]["details"]["reason"], "season_archived")
        self.assertIsNotNone(store.get_league(lid))  # zero mutation
        self.assertEqual({ls.id for ls in store.all_league_seasons()}, bindings0)

    def test_unbind_league_season_over_http(self):
        # The explicit unbind route is MANAGE_SETUP-gated; once unbound the
        # League deletes.
        sid = self._seed_season()
        lid = STATE.api.create_league(sid, "Gold")["id"]
        ls_id = STATE.api.store.league_seasons_for_league(lid)[0].id
        st, _ = self._req("POST", f"/api/v2/setup/league-season/{ls_id}/delete",
                          role="coach")
        self.assertEqual(st, 403)  # non-admin refused
        self.assertIsNotNone(STATE.api.store.get_league_season(ls_id))
        st, body = self._req("POST",
                             f"/api/v2/setup/league-season/{ls_id}/delete")
        self.assertEqual(st, 200, body)
        self.assertIsNone(STATE.api.store.get_league_season(ls_id))
        st, body = self._req("POST", f"/api/v2/setup/league/{lid}/delete")
        self.assertEqual(st, 200, body)
        self.assertIsNone(STATE.api.store.get_league(lid))

    def test_reunbind_league_season_over_http_is_not_found_single_audit(self):
        # HTTP parity for the re-fetch-under-lock invariant (#159 review):
        # unbinding an already-unbound binding over HTTP returns 404 not-found
        # and writes NO second league_season_deleted audit — exactly one unbind
        # succeeds, matching the delete_league_season service + PostgreSQL
        # unbind-vs-unbind barrier behaviour.
        sid = self._seed_season()
        lid = STATE.api.create_league(sid, "Gold")["id"]
        ls_id = STATE.api.store.league_seasons_for_league(lid)[0].id
        st, body = self._req("POST",
                             f"/api/v2/setup/league-season/{ls_id}/delete")
        self.assertEqual(st, 200, body)
        self.assertIsNone(STATE.api.store.get_league_season(ls_id))
        # Second unbind of the now-gone binding → 404, zero mutation.
        st, body = self._req("POST",
                             f"/api/v2/setup/league-season/{ls_id}/delete")
        self.assertEqual(st, 404, body)
        self.assertEqual(body["error"]["code"], "not_found", body)
        audits = [a for a in STATE.api.store.all_setup_audit()
                  if a.action == "league_season_deleted" and a.entity_id == ls_id]
        self.assertEqual(len(audits), 1, body)  # exactly one, no duplicate

    def test_delete_league_blocked_by_team_over_http(self):
        sid = self._seed_season()
        lid = STATE.api.create_league(sid, "Gold")["id"]
        club = STATE.api.create_club("Club")["id"]
        STATE.api.create_team(club_id=club, name="Alpha", league_id=lid)
        teams0 = len(STATE.api.store.all_teams())
        st, body = self._req("POST", f"/api/v2/setup/league/{lid}/delete")
        self.assertEqual(st, 409, body)
        self.assertEqual(body["error"]["code"], "has_dependencies")
        types = {g["type"] for g in body["error"]["details"]["dependencies"]}
        self.assertIn("team", types)
        self.assertIsNotNone(STATE.api.store.get_league(lid))  # zero mutation
        self.assertEqual(len(STATE.api.store.all_teams()), teams0)

    def test_write_into_archived_season_blocked_over_http(self):
        sid = self._seed_season(name="From")
        store = STATE.api.store
        # A second, archived Season as a roll-forward target (no team setup
        # needed — the guard fires before any copy).
        store.add_season(Season(id="s2", program_id="p", name="To",
                                status=SeasonStatus.ARCHIVED))
        st, body = self._req(
            "POST", "/api/v2/setup/seasons/s2/roll-forward",
            {"from_season_id": sid})
        self.assertEqual(st, 400, body)
        self.assertEqual(body["error"]["details"]["reason"], "season_archived")


if __name__ == "__main__":
    unittest.main()
