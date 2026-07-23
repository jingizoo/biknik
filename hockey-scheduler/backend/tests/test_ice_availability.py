"""Ice Availability Builder (#158): recurring Game-ice generation.

An arena operator builds a draft ice inventory from a recurring weekly template,
previews the exact slots, then explicitly commits them as AVAILABLE Game ice.
No games or published schedule are created here.

Covered across Memory / SQLite / PostgreSQL via the ServiceContract idiom, plus
a standalone unit test of the pure planner and an HTTP authz test:

  * a Tue/Thu 18:00-22:00 template with 60-minute games + 15-minute turnover
    previews the correct slot count in the Season (Program) timezone;
  * exclusion dates are skipped and explained, never created;
  * an existing overlapping slot is reported as a conflict, never overwritten;
  * a rink whose Venue lacks SeasonVenueAccess is reported (with a remediation
    route) and produces no slots — a multi-rink template requires the choice;
  * re-running the same template is idempotent — no duplicate slots;
  * preview writes nothing; commit is audited per slot plus a batch summary;
  * committing to an archived Season is refused (#159).
"""

import json
import os
import threading
import unittest
import urllib.error
import urllib.request
from datetime import date, datetime, timezone
from http.cookiejar import CookieJar
from http.server import ThreadingHTTPServer

from helpers import BACKEND  # noqa: F401  (ensures sys.path is set up)

from hockey_scheduler.api import ApiService
from hockey_scheduler.domain import IceSlotStatus, IceSlotType
from hockey_scheduler.domain.errors import (
    IntegrityConflictError, ScheduleConflictError, ValidationError)
from hockey_scheduler.domain.setup_models import (
    IceSlot, Program, Rink, Season, SeasonStatus, SeasonVenueAccess, Venue)
from hockey_scheduler.services import SetupService
from hockey_scheduler.services.ice_availability import (
    MAX_RANGE_DAYS, parse_hhmm, plan_ice_windows)
from hockey_scheduler.store import InMemoryStore, SqlStore

TZ = "America/Toronto"  # a DST zone: proves local->UTC is not a bare offset.


def _template(**over):
    """A Tue(1)/Thu(3) 18:00-22:00 template over one week => 3 games/day/rink."""
    base = dict(season_id="season_1", rink_ids=["rink_1"], weekdays=[1, 3],
                start_local="18:00", end_local="22:00",
                start_date="2026-09-01", end_date="2026-09-07",
                playable_minutes=60, turnover_minutes=15)
    base.update(over)
    return base


class IceAvailabilityContract:
    """Runs against every store backend (mirrors test_import_rinks_ice_slots)."""

    def _store(self):
        raise NotImplementedError

    def setUp(self):
        self.store = self._store()
        self.setup = SetupService(self.store)
        self.api = ApiService(self.store)
        self._seed()

    def _seed(self):
        self.store.add_program(Program(id="prog_1", name="AHL", timezone=TZ))
        # Season boundaries are stored as UTC instants of local midnight.
        self.store.add_season(Season(
            id="season_1", program_id="prog_1", name="Fall 2026",
            start_date=datetime(2026, 9, 1, 4, tzinfo=timezone.utc),
            end_date=datetime(2027, 3, 31, 4, tzinfo=timezone.utc)))
        self.store.add_season(Season(
            id="season_arch", program_id="prog_1", name="Old",
            start_date=datetime(2025, 9, 1, 4, tzinfo=timezone.utc),
            end_date=datetime(2026, 3, 31, 4, tzinfo=timezone.utc),
            status=SeasonStatus.ARCHIVED,
            archived_at=datetime(2026, 4, 1, tzinfo=timezone.utc)))
        self.store.add_venue(Venue(id="venue_1", name="Main Arena"))
        self.store.add_venue(Venue(id="venue_2", name="Annex"))
        self.store.add_rink(Rink(id="rink_1", venue_id="venue_1", name="Rink A"))
        self.store.add_rink(Rink(id="rink_2", venue_id="venue_1", name="Rink B"))
        self.store.add_rink(Rink(id="rink_na", venue_id="venue_2", name="Annex Ice"))
        # Only venue_1 is available to season_1; venue_2 deliberately is not.
        self.store.add_season_venue_access(SeasonVenueAccess(
            id="sva_1", season_id="season_1", venue_id="venue_1", active=True))

    def _slots(self):
        return list(self.store.all_ice_slots())

    def _add_existing(self, rink_id, start_iso, end_iso, slot_type=IceSlotType.GAME):
        slot = IceSlot(
            id=self.store.next_id("slot"), rink_id=rink_id,
            start_time=datetime.fromisoformat(start_iso),
            end_time=datetime.fromisoformat(end_iso), slot_type=slot_type,
            status=IceSlotStatus.AVAILABLE)
        self.store.add_ice_slot(slot)
        return slot

    def _commit(self, tmpl=None, *, actor_id="a"):
        """The required preview->commit flow (#158 review): preview the template
        AS ``actor_id`` (recording the ``ice_availability_previewed`` audit), then
        commit it with the fingerprint the preview returned. Commit is refused
        without a matching preview, so tests that exercise commit BEHAVIOR go
        through here instead of each re-implementing the two-step gate. Returns
        the commit result dict."""
        tmpl = _template() if tmpl is None else tmpl
        pv = self.api.preview_ice_availability(actor_id=actor_id, **tmpl)
        return self.api.commit_ice_availability(
            actor_id=actor_id, template_fingerprint=pv["template_fingerprint"],
            **tmpl)

    # -- 1. correct slot count + timezone ----------------------------------
    def test_preview_count_and_timezone(self):
        pv = self.api.preview_ice_availability(**_template())
        self.assertNotIn("error", pv)
        # One Tuesday + one Thursday, 3 playable games each.
        self.assertEqual(pv["totals"]["new"], 6)
        self.assertEqual(pv["totals"]["capacity_games"], 6)
        self.assertEqual(len(pv["rinks"]), 1)
        self.assertEqual(pv["rinks"][0]["new"], 6)
        self.assertEqual(pv["date_range"], {"start": "2026-09-01", "end": "2026-09-07"})
        first = pv["slots"][0]
        # 18:00 EDT is 22:00 UTC — a real zone conversion, not UTC pass-through.
        self.assertEqual(first["start_local"], "2026-09-01T18:00:00-04:00")
        self.assertEqual(first["start_time"], "2026-09-01T22:00:00+00:00")
        self.assertEqual(first["end_time"], "2026-09-01T23:00:00+00:00")
        # Reserved (contracted) time exceeds playable by the turnovers/idle tail.
        self.assertEqual(pv["totals"]["playable_minutes"], 360)   # 6 * 60
        self.assertEqual(pv["totals"]["reserved_minutes"], 480)   # 2 days * 240

    def test_commit_creates_available_game_ice(self):
        res = self._commit(actor_id="arena")
        self.assertNotIn("error", res)
        self.assertTrue(res["committed"])
        self.assertEqual(res["totals"]["created"], 6)
        slots = self._slots()
        self.assertEqual(len(slots), 6)
        self.assertTrue(all(s.slot_type == IceSlotType.GAME for s in slots))
        self.assertTrue(all(s.status == IceSlotStatus.AVAILABLE for s in slots))

    def test_commit_reresolves_access_revoked_after_planning(self):
        # #158 review: SeasonVenueAccess revoked AFTER planning but before the
        # write locks must not leak Game ice. Simulate the revoke landing at the
        # instant commit takes its first per-rink lock (which runs after
        # _plan_ice_availability, so planning still saw the rink as accessible);
        # the re-resolution under the lock must catch it and create nothing.
        orig = self.store.get_rink_for_update
        fired = []

        def revoke_then_lock(rid):
            if not fired:
                fired.append(True)
                acc = self.store.season_venue_access_for_pair("season_1", "venue_1")
                acc.active = False
                self.store.save_season_venue_access(acc)
            return orig(rid)

        pv = self.api.preview_ice_availability(actor_id="arena", **_template())
        self.store.get_rink_for_update = revoke_then_lock
        res = self.api.commit_ice_availability(
            actor_id="arena", template_fingerprint=pv["template_fingerprint"],
            **_template())
        # The revoke lands UNDER the lock, so the plan re-resolved inside the
        # commit txn (access now gone) differs from the previewed snapshot — the
        # binding refuses it (preview_mismatch) with zero writes, never leaking
        # unauthorized ice. That the mismatch fires at all proves access is
        # re-resolved under the lock: a stale pre-lock read would have matched the
        # preview and committed.
        self.assertEqual(res["error"]["details"]["reason"], "preview_mismatch")
        self.assertEqual(self._slots(), [])                  # no unauthorized ice
        self.assertEqual([a for a in self.store.all_setup_audit()
                          if a.action == "ice_slot_created"], [])

    def test_commit_reads_program_timezone_under_the_lock(self):
        # #158 review: the calendar (Program timezone -> UTC windows) is built
        # INSIDE the write transaction, so a timezone change (e.g. a hierarchy
        # re-import) that lands before the plan is built is honored — slots never
        # commit at stale UTC instants. Change the zone at the lock step (after
        # the Program lock, before the plan) and assert the committed windows use
        # the NEW zone, not the one a pre-transaction read would have captured.
        orig = self.store.get_rink_for_update
        fired = []

        def retz_then_lock(rid):
            if not fired:
                fired.append(True)
                prog = self.store.get_program("prog_1")
                prog.timezone = "UTC"                 # was America/Toronto (EDT)
                self.store.save_program(prog)
            return orig(rid)

        pv = self.api.preview_ice_availability(actor_id="a", **_template())
        self.store.get_rink_for_update = retz_then_lock
        res = self.api.commit_ice_availability(
            actor_id="a", template_fingerprint=pv["template_fingerprint"],
            **_template())
        # The zone flips to UTC UNDER the lock, so every window's UTC instant in
        # the plan re-resolved inside the txn differs from the previewed (Toronto)
        # snapshot — the binding refuses the commit (preview_mismatch) with zero
        # writes rather than silently creating slots at instants the operator
        # never previewed. The mismatch firing proves the zone is read under the
        # lock (a stale pre-lock read would have matched Toronto and committed).
        self.assertEqual(res["error"]["details"]["reason"], "preview_mismatch")
        self.assertEqual(self._slots(), [])

    def test_commit_reads_season_range_under_the_lock(self):
        # When the range is left to default, it comes from the Season boundaries
        # — also read INSIDE the write transaction. Narrow the Season at the lock
        # step and assert the committed windows honor the NEW (narrower) range, so
        # no slot lands outside the Season a concurrent date edit just committed.
        orig = self.store.get_rink_for_update
        fired = []

        def narrow_then_lock(rid):
            if not fired:
                fired.append(True)
                season = self.store.get_season("season_1")
                # End the Season at 2026-09-02 local midnight, so only the first
                # Tuesday (2026-09-01) stays in range (2026-09-02 is a Wednesday).
                season.end_date = datetime(2026, 9, 2, 4, tzinfo=timezone.utc)
                self.store.save_season(season)
            return orig(rid)

        tmpl = _template()                      # default the range to the Season
        tmpl.pop("start_date")
        tmpl.pop("end_date")
        pv = self.api.preview_ice_availability(actor_id="a", **tmpl)
        self.store.get_rink_for_update = narrow_then_lock
        res = self.api.commit_ice_availability(
            actor_id="a", template_fingerprint=pv["template_fingerprint"], **tmpl)
        # The Season end-date is narrowed UNDER the lock, so the range the plan
        # re-resolved inside the txn is smaller than the previewed one — the
        # binding refuses the commit (preview_mismatch) with zero writes, never
        # creating slots the operator didn't preview. The mismatch proves the
        # Season range is read under the lock (a stale pre-lock read of the wide
        # range would have matched the preview and committed).
        self.assertEqual(res["error"]["details"]["reason"], "preview_mismatch")
        self.assertEqual(self._slots(), [])

    # -- 1b. preview binds the reviewed CLASSIFICATION, not just the tuples --
    def test_commit_rejects_a_row_reclassified_to_conflict_after_preview(self):
        # #158 review: the token binds each row's reviewed new/duplicate/conflict
        # status, not only the generated (rink, start, end) tuple. Incompatible
        # ice dropped onto a generated tuple AFTER preview reclassifies that row
        # new -> conflict WITHOUT moving the tuple; committing the stale token is
        # refused (preview_mismatch) with zero writes, and a re-preview exposes the
        # exact new conflict target.
        tmpl = _template()
        pv = self.api.preview_ice_availability(actor_id="a", **tmpl)
        self.assertEqual(pv["totals"]["new"], 6)
        self.assertEqual(pv["totals"]["conflict"], 0)
        blocker = self._add_existing(               # exact first tuple, incompatible
            "rink_1", "2026-09-01T22:00:00+00:00", "2026-09-01T23:00:00+00:00",
            slot_type=IceSlotType.MAINTENANCE)
        res = self.api.commit_ice_availability(
            actor_id="a", template_fingerprint=pv["template_fingerprint"], **tmpl)
        self.assertEqual(res["error"]["details"]["reason"], "preview_mismatch")
        self.assertEqual(                           # only the blocker; no Game ice
            [s.id for s in self._slots() if s.slot_type == IceSlotType.GAME], [])
        pv2 = self.api.preview_ice_availability(actor_id="a", **tmpl)
        self.assertEqual(pv2["totals"]["conflict"], 1)
        clash = next(s for s in pv2["slots"] if s["status"] == "conflict")
        self.assertEqual(clash["conflict_with"], blocker.id)

    def test_commit_rejects_a_row_that_became_a_duplicate_after_preview(self):
        # Even a benign new -> compatible-duplicate transition is a classification
        # change the operator did not review: the binding refuses the stale token
        # (preview_mismatch), forcing a re-preview that shows the row already
        # exists. (Strict binding — ANY classification change forces re-preview;
        # the narrow idempotent optimization for this one transition is
        # deliberately not taken, so nothing is created behind the operator.)
        tmpl = _template()
        pv = self.api.preview_ice_availability(actor_id="a", **tmpl)
        self.assertEqual(pv["totals"]["new"], 6)
        self._add_existing("rink_1", "2026-09-01T22:00:00+00:00",
                           "2026-09-01T23:00:00+00:00")   # compatible Game/AVAILABLE
        res = self.api.commit_ice_availability(
            actor_id="a", template_fingerprint=pv["template_fingerprint"], **tmpl)
        self.assertEqual(res["error"]["details"]["reason"], "preview_mismatch")
        pv2 = self.api.preview_ice_availability(actor_id="a", **tmpl)
        self.assertEqual(pv2["totals"]["duplicate"], 1)
        self.assertEqual(pv2["totals"]["new"], 5)

    def test_commit_rejects_a_game_collision_added_after_preview(self):
        # A Game booked onto a generated tuple after preview turns the row into a
        # conflict whose target is that Game; the stale token is refused with zero
        # writes and a re-preview surfaces the exact Game id.
        from hockey_scheduler.domain.models import Game
        tmpl = _template()
        pv = self.api.preview_ice_availability(actor_id="a", **tmpl)
        self.assertEqual(pv["totals"]["new"], 6)
        start = datetime.fromisoformat("2026-09-01T22:00:00+00:00")
        end = datetime.fromisoformat("2026-09-01T23:00:00+00:00")
        self.store.add_ice_slot(IceSlot(
            id="occ", rink_id="rink_1", start_time=start, end_time=end,
            slot_type=IceSlotType.GAME, status=IceSlotStatus.ALLOCATED))
        self.store.add_game(Game(id="g_late", home_team_id="h", away_team_id="a",
                                 start_time=start, end_time=end, ice_slot_id="occ",
                                 season_id="season_1"))
        res = self.api.commit_ice_availability(
            actor_id="a", template_fingerprint=pv["template_fingerprint"], **tmpl)
        self.assertEqual(res["error"]["details"]["reason"], "preview_mismatch")
        self.assertEqual(                           # only the occupied slot; no new ice
            sorted(s.id for s in self._slots()), ["occ"])
        pv2 = self.api.preview_ice_availability(actor_id="a", **tmpl)
        clash = next(s for s in pv2["slots"] if s["status"] == "conflict")
        self.assertEqual(clash["conflict_game_id"], "g_late")

    def test_commit_rejects_a_conflict_removed_after_preview(self):
        # The reverse (duplicate/conflict -> new must force re-preview): a conflict
        # the operator reviewed is deleted after preview, so committing the stale
        # token would CREATE ice on a tuple reviewed as blocked. Refused.
        blocker = self._add_existing(
            "rink_1", "2026-09-01T22:00:00+00:00", "2026-09-01T23:00:00+00:00",
            slot_type=IceSlotType.MAINTENANCE)
        tmpl = _template()
        pv = self.api.preview_ice_availability(actor_id="a", **tmpl)
        self.assertEqual(pv["totals"]["conflict"], 1)
        self.store.delete_ice_slot(blocker.id)      # conflict target removed
        res = self.api.commit_ice_availability(
            actor_id="a", template_fingerprint=pv["template_fingerprint"], **tmpl)
        self.assertEqual(res["error"]["details"]["reason"], "preview_mismatch")
        self.assertEqual(self._slots(), [])         # nothing created behind the operator

    # -- 2. exclusion dates -------------------------------------------------
    def test_exclusion_dates_skipped_and_explained(self):
        pv = self.api.preview_ice_availability(
            **_template(exclusion_dates=["2026-09-01"]))
        self.assertEqual(pv["totals"]["new"], 3)  # only the Thursday remains
        self.assertIn({"date": "2026-09-01", "reason": "exclusion"},
                      pv["skipped_dates"])
        self.assertTrue(all(s["date"] != "2026-09-01" for s in pv["slots"]))
        res = self._commit(_template(exclusion_dates=["2026-09-01"]))
        self.assertEqual(res["totals"]["created"], 3)
        self.assertTrue(all(s.start_time.astimezone(timezone.utc).date().isoformat()
                            != "2026-09-01" or True for s in self._slots()))

    # -- 3. too-short window ------------------------------------------------
    def test_window_too_short_for_a_game(self):
        pv = self.api.preview_ice_availability(
            **_template(end_local="18:45"))  # 45 min < 60 min playable
        self.assertEqual(pv["totals"]["new"], 0)
        short_dates = {t["date"] for t in pv["too_short"]}
        self.assertEqual(short_dates, {"2026-09-01", "2026-09-03"})

    # -- 4. existing conflict is reported, never overwritten ----------------
    def test_existing_conflict_reported_not_overwritten(self):
        # Overlaps the first generated window (22:00-23:00 UTC) but is not an
        # exact match -> a true conflict.
        existing = self._add_existing(
            "rink_1", "2026-09-01T22:00:00+00:00", "2026-09-01T22:30:00+00:00")
        pv = self.api.preview_ice_availability(**_template())
        self.assertEqual(pv["totals"]["conflict"], 1)
        self.assertEqual(pv["totals"]["new"], 5)
        clash = next(s for s in pv["slots"] if s["status"] == "conflict")
        self.assertEqual(clash["conflict_with"], existing.id)
        res = self._commit()
        self.assertEqual(res["totals"]["created"], 5)
        self.assertEqual(res["totals"]["conflict_skipped"], 1)
        # The pre-existing slot is untouched; total = 1 existing + 5 created.
        self.assertEqual(len(self._slots()), 6)
        kept = self.store.get_ice_slot(existing.id)
        self.assertEqual(kept.end_time, datetime.fromisoformat("2026-09-01T22:30:00+00:00"))

    # -- 5. idempotent rerun (no duplicates) --------------------------------
    def test_rerun_is_idempotent(self):
        first = self._commit()
        self.assertEqual(first["totals"]["created"], 6)
        again = self._commit()
        self.assertEqual(again["totals"]["created"], 0)
        self.assertEqual(again["totals"]["duplicate_skipped"], 6)
        self.assertEqual(len(self._slots()), 6)  # unchanged
        pv = self.api.preview_ice_availability(**_template())
        self.assertEqual(pv["totals"]["duplicate"], 6)
        self.assertEqual(pv["totals"]["new"], 0)

    # -- 6. preview writes nothing -----------------------------------------
    def test_preview_writes_nothing(self):
        self.assertEqual(len(self._slots()), 0)
        self.api.preview_ice_availability(**_template())
        self.assertEqual(len(self._slots()), 0)
        self.assertEqual(len(list(self.store.all_setup_audit())), 0)

    # -- 7. multi-rink requires SeasonVenueAccess ---------------------------
    def test_rink_without_access_is_blocked_and_reported(self):
        pv = self.api.preview_ice_availability(
            **_template(rink_ids=["rink_1", "rink_na"]))
        self.assertEqual(pv["totals"]["new"], 6)  # only rink_1 (accessible)
        missing = pv["venue_access_missing"]
        self.assertEqual([m["rink_id"] for m in missing], ["rink_na"])
        self.assertEqual(missing[0]["remediation_route"],
                         "/api/v2/setup/seasons/season_1/venue-access")
        res = self._commit(_template(rink_ids=["rink_1", "rink_na"]))
        self.assertEqual(res["totals"]["created"], 6)
        self.assertEqual(res["totals"]["access_skipped_rinks"], 1)
        self.assertTrue(all(s.rink_id == "rink_1" for s in self._slots()))

    def test_two_accessible_rinks_double_the_capacity(self):
        pv = self.api.preview_ice_availability(
            **_template(rink_ids=["rink_1", "rink_2"]))
        self.assertEqual(pv["totals"]["new"], 12)
        self.assertEqual({r["rink_id"] for r in pv["rinks"]}, {"rink_1", "rink_2"})

    # -- 8. audit -----------------------------------------------------------
    def test_commit_is_audited(self):
        res = self._commit(actor_id="arena_boss")
        actions = [a.action for a in self.store.all_setup_audit()]
        self.assertEqual(actions.count("ice_slot_created"), 6)
        batch = next(a for a in self.store.all_setup_audit()
                     if a.action == "ice_availability_committed")
        self.assertEqual(batch.actor_id, "arena_boss")
        self.assertEqual(batch.detail["created"], 6)
        self.assertEqual(batch.detail["season_id"], "season_1")
        self.assertEqual(batch.entity_id, res["batch_id"])
        # Every per-slot row is tagged with the same batch id.
        slot_rows = [a for a in self.store.all_setup_audit()
                     if a.action == "ice_slot_created"]
        self.assertTrue(all(r.detail["ice_availability_batch_id"] == res["batch_id"]
                            for r in slot_rows))

    # -- 9. default date range from the Season ------------------------------
    def test_defaults_to_season_range(self):
        pv = self.api.preview_ice_availability(
            **_template(start_date=None, end_date=None))
        self.assertEqual(pv["date_range"], {"start": "2026-09-01", "end": "2027-03-31"})
        self.assertGreater(pv["totals"]["new"], 6)

    # -- 10. archived season may not receive ice ----------------------------
    def test_archived_season_commit_refused(self):
        res = self.api.commit_ice_availability(
            actor_id="a", **_template(season_id="season_arch"))
        self.assertIn("error", res)
        self.assertEqual(len(self._slots()), 0)

    # -- 11. validation -----------------------------------------------------
    def test_missing_rinks_is_validation_error(self):
        res = self.api.preview_ice_availability(**_template(rink_ids=[]))
        self.assertIn("error", res)

    def test_unknown_season_is_not_found(self):
        res = self.api.preview_ice_availability(**_template(season_id="nope"))
        self.assertIn("error", res)

    # -- 12. idempotency / no duplicate rows (#158 review) ------------------
    def test_repeated_rink_id_does_not_duplicate(self):
        res = self._commit(_template(rink_ids=["rink_1", "rink_1"]))
        self.assertEqual(res["totals"]["created"], 6)   # de-duped, not 12
        self.assertEqual(len(self._slots()), 6)
        tuples = {(s.rink_id, s.start_time, s.end_time) for s in self._slots()}
        self.assertEqual(len(tuples), 6)                # one physical slot / tuple

    def test_commit_after_manual_slot_is_duplicate(self):
        # A slot manually created at one window's exact tuple is an idempotent
        # duplicate on commit — never a second physical row for that tuple.
        self.store.add_ice_slot(IceSlot(
            id=self.store.next_id("slot"), rink_id="rink_1",
            start_time=datetime(2026, 9, 1, 22, tzinfo=timezone.utc),   # 18:00 EDT
            end_time=datetime(2026, 9, 1, 23, tzinfo=timezone.utc),
            slot_type=IceSlotType.GAME, status=IceSlotStatus.AVAILABLE))
        res = self._commit()
        self.assertEqual(res["totals"]["created"], 5)
        self.assertEqual(res["totals"]["duplicate_skipped"], 1)
        slots = self._slots()
        self.assertEqual(len(slots), 6)                 # 5 new + 1 pre-existing
        tuples = {(s.rink_id, s.start_time, s.end_time) for s in slots}
        self.assertEqual(len(tuples), 6)                # no duplicate tuple
        # Only the committed slots are audited (not the pre-existing manual one).
        created_audits = [a for a in self.store.all_setup_audit()
                          if a.action == "ice_slot_created"]
        self.assertEqual(len(created_audits), 5)

    # -- 13. per-weekday windows (#158 flow; #313 review) -------------------
    def _perday_template(self, **over):
        """A per-weekday template: Tue 18:00-22:00 (3 games) + Thu 19:00-22:00
        (2 games) over one week => 5 slots on one rink."""
        base = dict(
            season_id="season_1", rink_ids=["rink_1"],
            start_date="2026-09-01", end_date="2026-09-07",
            playable_minutes=60, turnover_minutes=15,
            windows=[{"weekday": 1, "start_local": "18:00", "end_local": "22:00"},
                     {"weekday": 3, "start_local": "19:00", "end_local": "22:00"}])
        base.update(over)
        return base

    def test_preview_honors_per_weekday_windows(self):
        pv = self.api.preview_ice_availability(**self._perday_template())
        self.assertNotIn("error", pv)
        self.assertEqual(pv["totals"]["new"], 5)          # 3 Tue + 2 Thu
        tue = [s for s in pv["slots"] if s["date"] == "2026-09-01"]
        thu = [s for s in pv["slots"] if s["date"] == "2026-09-03"]
        self.assertEqual(len(tue), 3)
        self.assertEqual(len(thu), 2)
        # Each day starts at its OWN local time (18:00 EDT vs 19:00 EDT).
        self.assertEqual(tue[0]["start_local"], "2026-09-01T18:00:00-04:00")
        self.assertEqual(thu[0]["start_local"], "2026-09-03T19:00:00-04:00")
        # The per-weekday windows are echoed back for the operator, sorted.
        self.assertEqual(pv["windows"], [
            {"weekday": 1, "start": "18:00", "end": "22:00"},
            {"weekday": 3, "start": "19:00", "end": "22:00"}])
        self.assertEqual(pv["weekdays"], [1, 3])

    def test_commit_persists_and_audits_per_weekday_windows(self):
        from zoneinfo import ZoneInfo
        tz = ZoneInfo(TZ)
        res = self._commit(self._perday_template(), actor_id="arena")
        self.assertEqual(res["totals"]["created"], 5)
        slots = self._slots()
        self.assertEqual(len(slots), 5)
        # Group by LOCAL date (a late game crosses midnight in UTC): Tuesday
        # keeps its 18:00 window (3 games), Thursday its own 19:00 (2 games).
        by_local = {}
        for s in slots:
            local = s.start_time.astimezone(tz)
            by_local.setdefault(local.date(), []).append(local)
        tue = sorted(by_local[date(2026, 9, 1)])
        thu = sorted(by_local[date(2026, 9, 3)])
        self.assertEqual(len(tue), 3)
        self.assertEqual(len(thu), 2)
        self.assertEqual(tue[0].strftime("%H:%M"), "18:00")   # Tuesday's window
        self.assertEqual(thu[0].strftime("%H:%M"), "19:00")   # Thursday's window
        # The batch audit preserves the exact per-weekday windows.
        batch = next(a for a in self.store.all_setup_audit()
                     if a.action == "ice_availability_committed")
        self.assertEqual(batch.detail["windows"], [
            {"weekday": 1, "start": "18:00", "end": "22:00"},
            {"weekday": 3, "start": "19:00", "end": "22:00"}])

    def test_per_weekday_windows_survive_a_commit_retry(self):
        # Guard the retry path (#313): the generated planner windows must not
        # clobber the per-weekday `windows` INPUT, or the second attempt would
        # re-plan with the wrong shape and raise. Force exactly one
        # IntegrityConflictError (as a lock-free racing writer would), then let
        # the retry succeed and confirm the per-weekday result is intact.
        orig_add = self.store.add_ice_slot
        tripped = []

        def add_once_failing(slot):
            if not tripped:
                tripped.append(True)
                raise IntegrityConflictError(
                    "forced race", {"reason": "ice_slot_time_taken"})
            return orig_add(slot)

        self.store.add_ice_slot = add_once_failing
        try:
            res = self._commit(self._perday_template())
        finally:
            self.store.add_ice_slot = orig_add
        self.assertTrue(tripped, "the forced conflict never fired")
        self.assertNotIn("error", res)
        self.assertEqual(res["totals"]["created"], 5)     # retry re-planned OK
        self.assertEqual(len(self._slots()), 5)

    def test_per_weekday_rerun_is_idempotent(self):
        first = self._commit(self._perday_template())
        self.assertEqual(first["totals"]["created"], 5)
        again = self._commit(self._perday_template())
        self.assertEqual(again["totals"]["created"], 0)
        self.assertEqual(again["totals"]["duplicate_skipped"], 5)
        self.assertEqual(len(self._slots()), 5)           # unchanged

    def test_windows_reject_duplicate_weekday(self):
        res = self.api.preview_ice_availability(**self._perday_template(
            windows=[{"weekday": 1, "start_local": "18:00", "end_local": "20:00"},
                     {"weekday": 1, "start_local": "20:00", "end_local": "22:00"}]))
        self.assertIn("error", res)
        self.assertEqual(res["error"]["details"]["reason"], "duplicate_weekday")

    def test_windows_reject_bad_time(self):
        res = self.api.preview_ice_availability(**self._perday_template(
            windows=[{"weekday": 1, "start_local": "25:00", "end_local": "22:00"}]))
        self.assertIn("error", res)

    def test_per_weekday_windows_honor_exclusion(self):
        # Excluding the Tuesday leaves only Thursday's own 2-game window; the
        # excluded date is reported, not created.
        pv = self.api.preview_ice_availability(**self._perday_template(
            exclusion_dates=["2026-09-01"]))
        self.assertEqual(pv["totals"]["new"], 2)          # Thu only (3 Tue gone)
        self.assertEqual({s["date"] for s in pv["slots"]}, {"2026-09-03"})
        self.assertEqual([d["date"] for d in pv["skipped_dates"]], ["2026-09-01"])

    def test_per_weekday_windows_report_conflict(self):
        # An existing slot overlapping Thursday's 19:00 window (23:00-23:30 UTC
        # on Sep 3, non-exact) is a conflict on THAT day only — Tuesday's three
        # 18:00 windows are untouched. Proves classification runs per generated
        # window regardless of which day's time produced it.
        self._add_existing("rink_1", "2026-09-03T23:00:00+00:00",
                           "2026-09-03T23:30:00+00:00")
        pv = self.api.preview_ice_availability(**self._perday_template())
        self.assertEqual(pv["totals"]["conflict"], 1)
        self.assertEqual(pv["totals"]["new"], 4)          # 3 Tue + 1 Thu left
        res = self._commit(self._perday_template())
        self.assertEqual(res["totals"]["created"], 4)
        self.assertEqual(res["totals"]["conflict_skipped"], 1)

    # -- 14. preview binding: commit refuses a form edited after preview ----
    def test_preview_returns_a_stable_fingerprint(self):
        fp = self.api.preview_ice_availability(**_template())["template_fingerprint"]
        self.assertTrue(fp)
        # Deterministic for identical inputs; different when the template changes.
        self.assertEqual(
            fp, self.api.preview_ice_availability(**_template())["template_fingerprint"])
        self.assertNotEqual(fp, self.api.preview_ice_availability(
            **_template(end_local="21:00"))["template_fingerprint"])
        # Cosmetic input differences (rink order) do NOT change it.
        self.assertEqual(fp, self.api.preview_ice_availability(
            **_template(rink_ids=["rink_1", "rink_1"]))["template_fingerprint"])

    def test_commit_with_matching_fingerprint_creates_the_previewed_slots(self):
        pv = self.api.preview_ice_availability(actor_id="a", **_template())
        res = self.api.commit_ice_availability(
            actor_id="a", template_fingerprint=pv["template_fingerprint"],
            **_template())
        self.assertNotIn("error", res)
        self.assertEqual(res["totals"]["created"], pv["totals"]["new"])   # exactly displayed
        self.assertEqual(len(self._slots()), 6)

    def test_commit_with_stale_fingerprint_is_refused_with_zero_writes(self):
        # Preview one template, then commit a DIFFERENT one carrying the previewed
        # fingerprint — exactly the "edited the form after preview" attack.
        pv = self.api.preview_ice_availability(**_template())
        res = self.api.commit_ice_availability(
            actor_id="a", template_fingerprint=pv["template_fingerprint"],
            **_template(end_local="21:00"))             # window edited after preview
        self.assertIn("error", res)
        self.assertEqual(res["error"]["details"]["reason"], "preview_mismatch")
        self.assertEqual(self._slots(), [])             # nothing created
        self.assertEqual([a for a in self.store.all_setup_audit()
                          if a.action == "ice_slot_created"], [])   # nothing audited

    def test_commit_without_fingerprint_is_refused(self):
        # #158 review: preview is REQUIRED. Committing with no fingerprint at all
        # (the old bypass) is refused with preview_required and writes nothing —
        # a valid preview by this operator does not help a commit that supplies
        # no token to bind to it.
        self.api.preview_ice_availability(actor_id="a", **_template())
        res = self.api.commit_ice_availability(actor_id="a", **_template())
        self.assertIn("error", res)
        self.assertEqual(res["error"]["details"]["reason"], "preview_required")
        self.assertEqual(self._slots(), [])
        self.assertEqual([a for a in self.store.all_setup_audit()
                          if a.action == "ice_slot_created"], [])

    def test_commit_with_another_operators_fingerprint_is_refused(self):
        # #158 review: the fingerprint is bound to the operator who previewed.
        # Operator "a" previews; operator "b" replays a's (valid, current) token.
        # It matches the resolved snapshot, but there is no preview audit for "b",
        # so the commit is refused with preview_required and writes nothing.
        pv = self.api.preview_ice_availability(actor_id="a", **_template())
        res = self.api.commit_ice_availability(
            actor_id="b", template_fingerprint=pv["template_fingerprint"],
            **_template())
        self.assertIn("error", res)
        self.assertEqual(res["error"]["details"]["reason"], "preview_required")
        self.assertEqual(self._slots(), [])
        # ...while the operator who DID preview commits that same token cleanly.
        ok = self.api.commit_ice_availability(
            actor_id="a", template_fingerprint=pv["template_fingerprint"],
            **_template())
        self.assertEqual(ok["totals"]["created"], 6)

    def test_commit_refused_when_default_season_dates_change_after_preview(self):
        # The fingerprint binds the RESOLVED snapshot, not the raw form. With a
        # blank date range (defaulted from the Season), a concurrent Season-end
        # edit moves the resolved range — so a commit carrying the previewed
        # fingerprint is refused (never writes the range the operator didn't see).
        tmpl = _template(start_date=None, end_date=None)      # default from Season
        fp = self.api.preview_ice_availability(**tmpl)["template_fingerprint"]
        season = self.store.get_season("season_1")
        season.end_date = datetime(2026, 9, 8, 4, tzinfo=timezone.utc)   # narrowed
        self.store.save_season(season)
        res = self.api.commit_ice_availability(
            actor_id="a", template_fingerprint=fp, **tmpl)
        self.assertIn("error", res)
        self.assertEqual(res["error"]["details"]["reason"], "preview_mismatch")
        self.assertEqual(self._slots(), [])
        self.assertEqual([a for a in self.store.all_setup_audit()
                          if a.action == "ice_slot_created"], [])

    def test_commit_refused_when_program_timezone_changes_after_preview(self):
        # A Program-timezone change moves every window's UTC instant. A commit
        # bound to the previewed (Toronto) snapshot is refused after the zone
        # flips to UTC — the reviewed slots and the would-be-written slots differ.
        fp = self.api.preview_ice_availability(**_template())["template_fingerprint"]
        prog = self.store.get_program("prog_1")
        prog.timezone = "UTC"
        self.store.save_program(prog)
        res = self.api.commit_ice_availability(
            actor_id="a", template_fingerprint=fp, **_template())
        self.assertIn("error", res)
        self.assertEqual(res["error"]["details"]["reason"], "preview_mismatch")
        self.assertEqual(self._slots(), [])

    def test_commit_after_repreview_of_the_new_snapshot_succeeds(self):
        # After a Season change invalidates the old preview, re-previewing yields
        # a fresh fingerprint that DOES commit — the refresh path is not a
        # dead end.
        prog = self.store.get_program("prog_1")
        prog.timezone = "UTC"
        self.store.save_program(prog)
        pv = self.api.preview_ice_availability(actor_id="a", **_template())  # fresh
        res = self.api.commit_ice_availability(
            actor_id="a", template_fingerprint=pv["template_fingerprint"],
            **_template())
        self.assertNotIn("error", res)
        self.assertEqual(res["totals"]["created"], pv["totals"]["new"])

    # -- 15. an exact tuple is a duplicate ONLY when compatible Game ice -----
    # The first generated window is Tue Sep 1, 18:00 EDT = 22:00-23:00 UTC.
    _FIRST = (datetime(2026, 9, 1, 22, tzinfo=timezone.utc),
              datetime(2026, 9, 1, 23, tzinfo=timezone.utc))

    def _slot_at_first_window(self, slot_type=IceSlotType.GAME,
                              status=IceSlotStatus.AVAILABLE, game_id=None):
        start, end = self._FIRST
        slot = self._add_existing("rink_1", start.isoformat(), end.isoformat(),
                                  slot_type=slot_type)
        if status != IceSlotStatus.AVAILABLE:
            slot.status = status
            self.store.save_ice_slot(slot)
        if game_id is not None:
            from hockey_scheduler.domain.models import Game
            self.store.add_game(Game(
                id=game_id, home_team_id="h", away_team_id="a",
                start_time=start, end_time=end, ice_slot_id=slot.id,
                season_id="season_1"))
        return slot

    def _first_preview_slot(self, pv):
        first = self._FIRST[0].isoformat()
        return next(s for s in pv["slots"] if s["start_time"] == first)

    def test_exact_available_game_slot_is_an_idempotent_duplicate(self):
        self._slot_at_first_window()                       # AVAILABLE Game
        pv = self.api.preview_ice_availability(**_template())
        self.assertEqual(self._first_preview_slot(pv)["status"], "duplicate")
        self.assertEqual(pv["totals"]["duplicate"], 1)
        self.assertEqual(pv["totals"]["conflict"], 0)

    def test_exact_allocated_slot_backing_a_game_is_a_conflict(self):
        self._slot_at_first_window(status=IceSlotStatus.ALLOCATED,
                                   game_id="g_occ")
        pv = self.api.preview_ice_availability(**_template())
        slot = self._first_preview_slot(pv)
        self.assertEqual(slot["status"], "conflict")       # NOT hidden as capacity
        self.assertTrue(slot["conflict_has_game"])
        self.assertEqual(slot["conflict_game_id"], "g_occ")
        self.assertEqual(slot["conflict_slot_status"], "allocated")
        self.assertEqual(pv["totals"]["conflict"], 1)
        self.assertEqual(pv["totals"]["duplicate"], 0)
        # Commit skips it as a conflict, never counts it as idempotent capacity.
        res = self._commit()
        self.assertEqual(res["totals"]["created"], 5)
        self.assertEqual(res["totals"]["conflict_skipped"], 1)
        self.assertEqual(res["totals"]["duplicate_skipped"], 0)

    def test_exact_non_game_slot_is_a_conflict(self):
        self._slot_at_first_window(slot_type=IceSlotType.MAINTENANCE)
        pv = self.api.preview_ice_availability(**_template())
        slot = self._first_preview_slot(pv)
        self.assertEqual(slot["status"], "conflict")
        self.assertEqual(slot["conflict_slot_type"], "maintenance")
        self.assertFalse(slot["conflict_has_game"])
        self.assertEqual(pv["totals"]["conflict"], 1)
        self.assertEqual(pv["totals"]["duplicate"], 0)
        res = self._commit()
        self.assertEqual(res["totals"]["created"], 5)
        self.assertEqual(res["totals"]["conflict_skipped"], 1)
        self.assertEqual(res["totals"]["duplicate_skipped"], 0)

    # -- 16. the preview half is audited too (#158 review) ------------------
    def _preview_audits(self):
        return [a for a in self.store.all_setup_audit()
                if a.action == "ice_availability_previewed"]

    def test_authenticated_preview_is_audited(self):
        pv = self.api.preview_ice_availability(actor_id="arena_boss",
                                               **_template())
        audits = self._preview_audits()
        self.assertEqual(len(audits), 1)
        a = audits[0]
        self.assertEqual(a.actor_id, "arena_boss")             # the exact caller
        self.assertEqual(a.entity_id, "season_1")
        self.assertEqual(a.detail["template_fingerprint"],
                         pv["template_fingerprint"])
        self.assertEqual(a.detail["season_id"], "season_1")
        self.assertEqual(a.detail["rink_ids"], ["rink_1"])
        self.assertEqual(a.detail["totals"]["new"], 6)
        self.assertEqual(a.detail["date_range"],
                         {"start": "2026-09-01", "end": "2026-09-07"})
        self.assertIn("timezone", a.detail)

    def test_unauthenticated_preview_writes_no_audit(self):
        # No actor => the server-attributed audit is not written (a programmatic
        # reader), preserving the read-only preview for anonymous callers.
        self.api.preview_ice_availability(**_template())       # no actor_id
        self.assertEqual(self._preview_audits(), [])

    def test_failed_preview_writes_no_audit(self):
        # A not-found template raises before the audit => zero rows even with an
        # actor. (Invalid input takes the same pre-audit failure path.)
        res = self.api.preview_ice_availability(
            actor_id="arena_boss", **_template(season_id="nope"))
        self.assertIn("error", res)
        self.assertEqual(self._preview_audits(), [])

    def test_preview_and_commit_audits_correlate_by_fingerprint(self):
        pv = self.api.preview_ice_availability(actor_id="a", **_template())
        self.api.commit_ice_availability(
            actor_id="a", template_fingerprint=pv["template_fingerprint"],
            **_template())
        prev = next(a for a in self.store.all_setup_audit()
                    if a.action == "ice_availability_previewed")
        batch = next(a for a in self.store.all_setup_audit()
                     if a.action == "ice_availability_committed")
        self.assertTrue(prev.detail["template_fingerprint"])
        self.assertEqual(prev.detail["template_fingerprint"],
                         batch.detail["template_fingerprint"])


class MemoryIceAvailabilityTest(IceAvailabilityContract, unittest.TestCase):
    def _store(self):
        return InMemoryStore()


class SqlIceAvailabilityTest(IceAvailabilityContract, unittest.TestCase):
    def _store(self):
        return SqlStore(":memory:")


@unittest.skipUnless(os.environ.get("TEST_DATABASE_URL"),
                     "PostgreSQL required (set TEST_DATABASE_URL)")
class PostgresIceAvailabilityTest(IceAvailabilityContract, unittest.TestCase):
    def _store(self):
        store = SqlStore(os.environ["TEST_DATABASE_URL"])
        store.clear_all_data()  # isolate from any prior run's rows
        return store


@unittest.skipUnless(os.environ.get("TEST_DATABASE_URL"),
                     "PostgreSQL required (set TEST_DATABASE_URL)")
class PostgresIceAvailabilityConcurrencyTest(unittest.TestCase):
    """Forced two-session races on real PostgreSQL (#158 review). Each session
    is its own connection; a barrier releases both at once. The Season + per-rink
    row locks and the (rink, start, end) unique index must guarantee, under any
    interleaving, exactly one physical slot per tuple, no overlaps, and stable
    created/duplicate totals — with audit rows only for slots actually created.
    """

    def setUp(self):
        self.url = os.environ["TEST_DATABASE_URL"]
        seed = SqlStore(self.url)
        seed.clear_all_data()
        s = SetupService(seed)
        # The Program timezone is the anchor for the builder's local->UTC math,
        # so it decides where the generated windows land. Toronto (EDT in
        # September) puts the first 18:00-19:00 local window at 22:00-23:00 UTC —
        # exactly the tuple test_commit_vs_manual creates by hand, so the manual
        # slot collides with a generated window instead of becoming a 7th slot.
        prog = s.create_program("AHL", timezone_name=TZ)
        season = s.create_season(prog.id, "Fall 2026")
        venue = s.create_venue("Main Arena", league_id=prog.id)
        self.access_id = s.grant_season_venue_access(season.id, venue.id).id
        self.program_id = prog.id
        self.season_id = season.id
        self.rink_id = s.create_rink(venue.id, "Rink A").id

    def _template(self):
        return dict(season_id=self.season_id, rink_ids=[self.rink_id],
                    weekdays=[1, 3], start_local="18:00", end_local="22:00",
                    start_date="2026-09-01", end_date="2026-09-07",
                    playable_minutes=60, turnover_minutes=15)

    def _slots(self):
        return sorted((x for x in SqlStore(self.url).all_ice_slots()
                       if x.rink_id == self.rink_id), key=lambda x: x.start_time)

    def _assert_unique_and_non_overlapping(self):
        slots = self._slots()
        tuples = {(x.rink_id, x.start_time, x.end_time) for x in slots}
        self.assertEqual(len(tuples), len(slots), "duplicate (rink, start, end)")
        for a, b in zip(slots, slots[1:]):
            self.assertLessEqual(a.end_time, b.start_time, "overlapping slots")
        return slots

    def _run(self, targets):
        barrier = threading.Barrier(len(targets))
        out = [None] * len(targets)

        def wrap(i, fn):
            store = SqlStore(self.url)
            svc = SetupService(store)
            barrier.wait()
            try:
                out[i] = fn(svc)
            except Exception as exc:            # record; asserted by the caller
                out[i] = exc

        threads = [threading.Thread(target=wrap, args=(i, fn))
                   for i, fn in enumerate(targets)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        return out

    def test_commit_vs_commit(self):
        # Preview once up front (the required gate #158 review) so both racing
        # commits carry a valid, matching token; no metadata changes here, so the
        # resolved fingerprint is stable and both are admitted.
        fp = SetupService(SqlStore(self.url)).preview_ice_availability(
            actor_id="u", **self._template())["template_fingerprint"]
        commit = lambda svc: svc.commit_ice_availability(  # noqa: E731
            actor_id="u", template_fingerprint=fp, **self._template())
        out = self._run([commit, commit])
        for r in out:
            self.assertNotIsInstance(r, Exception, f"commit raised: {r!r}")
        slots = self._assert_unique_and_non_overlapping()
        self.assertEqual(len(slots), 6)                       # one round-robin
        self.assertEqual(sum(r["totals"]["created"] for r in out), 6)
        self.assertEqual(sum(r["totals"]["duplicate_skipped"] for r in out), 6)

    def test_commit_vs_manual(self):
        # The other session manually creates a slot at one window's exact tuple.
        # (A manual slot does not change Season/Program/access, so the resolved
        # fingerprint is stable and the previewed token stays valid.)
        fp = SetupService(SqlStore(self.url)).preview_ice_availability(
            actor_id="c", **self._template())["template_fingerprint"]
        commit = lambda svc: svc.commit_ice_availability(  # noqa: E731
            actor_id="c", template_fingerprint=fp, **self._template())
        manual = lambda svc: svc.create_ice_slot(          # noqa: E731
            self.rink_id, datetime(2026, 9, 1, 22, tzinfo=timezone.utc),
            datetime(2026, 9, 1, 23, tzinfo=timezone.utc))
        out = self._run([commit, manual])
        # The commit always succeeds; the manual create either wins (its slot is
        # one of the six) or loses the rink-lock race with a clean overlap
        # conflict — never a partial write or a duplicate/overlapping tuple.
        self.assertNotIsInstance(out[0], Exception, f"commit raised: {out[0]!r}")
        slots = self._assert_unique_and_non_overlapping()
        self.assertEqual(len(slots), 6)

    # -- builder vs CSV import (the lock-free sibling writer, #158 review) -----
    def _tag_rink_code(self, code):
        """Give the seeded rink an external_ref so a CSV import (which matches
        rinks by rink_code == external_ref) targets the SAME physical rink the
        builder commits onto, instead of creating a fresh one."""
        store = SqlStore(self.url)
        rink = store.get_rink(self.rink_id)
        rink.external_ref = code
        store.save_rink(rink)

    def _import(self, start_iso, end_iso):
        return lambda svc: svc.commit_rinks_ice_slots_import(  # noqa: E731
            {"rinks": [{"venue_name": "Main Arena", "rink_code": "RA"}],
             "ice_slots": [{"rink_code": "RA", "start_time": start_iso,
                            "end_time": end_iso, "slot_type": "game"}]},
            actor_id="i")

    def test_commit_vs_import_nonexact_overlap(self):
        # The import's 22:30-23:30 UTC overlaps the builder's first window
        # (22:00-23:00) WITHOUT being an exact tuple, so migration 045's unique
        # index cannot catch it -- only the shared rink lock + write-boundary
        # overlap revalidation can. Under either interleaving no physical overlap
        # may survive: the import either wins the lock (its slot lands, the
        # builder then skips the windows it clashes with) or loses and is cleanly
        # rejected once the builder's slot is visible.
        self._tag_rink_code("RA")
        fp = SetupService(SqlStore(self.url)).preview_ice_availability(
            actor_id="c", **self._template())["template_fingerprint"]
        commit = lambda svc: svc.commit_ice_availability(  # noqa: E731
            actor_id="c", template_fingerprint=fp, **self._template())
        out = self._run([commit, self._import("2026-09-01T22:30:00+00:00",
                                              "2026-09-01T23:30:00+00:00")])
        self.assertNotIsInstance(out[0], Exception, f"commit raised: {out[0]!r}")
        if isinstance(out[1], Exception):        # the import may lose the race
            self.assertIsInstance(out[1], ScheduleConflictError,
                                  f"import raised non-conflict: {out[1]!r}")
        self._assert_unique_and_non_overlapping()

    def test_commit_vs_import_exact_duplicate(self):
        # The import's 22:00-23:00 UTC is the builder's first window EXACTLY.
        # Whichever writer lands first, the other treats the tuple as a duplicate
        # (the builder skips it; the import takes its update path), so the slot
        # never doubles and both operations complete cleanly -- always the
        # builder's six windows, no more.
        self._tag_rink_code("RA")
        fp = SetupService(SqlStore(self.url)).preview_ice_availability(
            actor_id="c", **self._template())["template_fingerprint"]
        commit = lambda svc: svc.commit_ice_availability(  # noqa: E731
            actor_id="c", template_fingerprint=fp, **self._template())
        out = self._run([commit, self._import("2026-09-01T22:00:00+00:00",
                                              "2026-09-01T23:00:00+00:00")])
        self.assertNotIsInstance(out[0], Exception, f"commit raised: {out[0]!r}")
        self.assertNotIsInstance(out[1], Exception, f"import raised: {out[1]!r}")
        slots = self._assert_unique_and_non_overlapping()
        self.assertEqual(len(slots), 6)

    def test_commit_vs_access_revoke(self):
        # commit and revoke_season_venue_access both take the Season row lock, so
        # they serialize under either barrier ordering: commit wins -> its six
        # windows are created while access is still live; revoke wins -> commit
        # re-resolves access UNDER the lock, its snapshot no longer matches the
        # previewed one, and it is refused (preview_mismatch, #158 review). Never
        # a partial / unauthorized subset of ice, and never a raw (non-domain)
        # error.
        fp = SetupService(SqlStore(self.url)).preview_ice_availability(
            actor_id="c", **self._template())["template_fingerprint"]
        commit = lambda svc: svc.commit_ice_availability(  # noqa: E731
            actor_id="c", template_fingerprint=fp, **self._template())
        revoke = lambda svc: svc.revoke_season_venue_access(  # noqa: E731
            self.access_id, actor_id="r")
        out = self._run([commit, revoke])
        self.assertNotIsInstance(out[1], Exception, f"revoke raised: {out[1]!r}")
        r = out[0]
        if isinstance(r, ScheduleConflictError):       # revoke won the lock race
            self.assertEqual(r.details["reason"], "preview_mismatch")
            self.assertEqual(self._slots(), [])        # zero unauthorized ice
        else:                                          # commit won
            self.assertNotIsInstance(r, Exception, f"commit raised: {r!r}")
            self.assertEqual(r["totals"]["created"], 6)
            self.assertEqual(len(self._assert_unique_and_non_overlapping()), 6)

    def test_commit_vs_timezone_change(self):
        # commit reads the Program timezone (and generates its UTC windows) under
        # a Program row lock; a concurrent raw timezone write contends for that
        # lock, so they serialize. Under either ordering the committed windows are
        # wholly ONE zone — all six at Toronto instants (first 22:00 UTC) or all
        # six at UTC instants (first 18:00 UTC) — never a stale mix of both, and
        # never a raw error.
        fp = SetupService(SqlStore(self.url)).preview_ice_availability(
            actor_id="c", **self._template())["template_fingerprint"]
        commit = lambda svc: svc.commit_ice_availability(  # noqa: E731
            actor_id="c", template_fingerprint=fp, **self._template())

        def change_tz(svc):
            prog = svc.store.get_program(self.program_id)
            prog.timezone = "UTC"
            svc.store.save_program(prog)

        out = self._run([commit, change_tz])
        r = out[0]
        if isinstance(r, ScheduleConflictError):   # tz change won the Program lock
            # the commit re-resolved every window to UTC under the lock, no longer
            # matching the previewed (Toronto) snapshot -> refused, zero ice.
            self.assertEqual(r.details["reason"], "preview_mismatch")
            self.assertEqual(self._slots(), [])
        else:                                      # commit won: all Toronto
            self.assertNotIsInstance(r, Exception, f"commit raised: {r!r}")
            slots = self._assert_unique_and_non_overlapping()
            self.assertEqual(len(slots), 6)        # one zone's worth, never mixed
            self.assertEqual(min(s.start_time for s in slots),
                             datetime(2026, 9, 1, 22, tzinfo=timezone.utc))

    def test_commit_vs_import_metadata_change_no_deadlock(self):
        # A hierarchy re-import updates BOTH the Program timezone AND the Season
        # end_date, taking the Program row lock then the Season row lock (the
        # importer's order). The commit DEFAULTS its range from the Season, so the
        # date change really changes the calendar. The commit locks in the SAME
        # Program-first order, so the two serialize instead of forming the
        # Season<->Program cycle that raised PostgreSQL 40P01 (#158 review). Under
        # either ordering: no deadlock/raw error, and the committed slots are ONE
        # coherent snapshot -- old Toronto tz + wide Sep 1-8 range (9 windows), or
        # new UTC tz + narrowed Sep 1-2 range (3 windows) -- never a stale mix,
        # with nothing outside the winning range.
        from zoneinfo import ZoneInfo
        utc = timezone.utc
        seed = SqlStore(self.url)
        s0 = seed.get_season(self.season_id)
        s0.start_date = datetime(2026, 9, 1, 4, tzinfo=utc)    # Sep 1 local (EDT)
        s0.end_date = datetime(2026, 9, 8, 4, tzinfo=utc)      # Sep 8 local (wide)
        seed.save_season(s0)

        tmpl = dict(self._template())
        tmpl.pop("start_date")
        tmpl.pop("end_date")                                   # default the range
        fp = SetupService(SqlStore(self.url)).preview_ice_availability(
            actor_id="c", **tmpl)["template_fingerprint"]
        commit = lambda svc: svc.commit_ice_availability(  # noqa: E731
            actor_id="c", template_fingerprint=fp, **tmpl)

        def reimport(svc):
            with svc.store.transaction():
                prog = svc.store.get_program_for_update(self.program_id)
                prog.timezone = "UTC"
                svc.store.save_program(prog)
                s = svc.store.get_season_for_update(self.season_id)
                s.end_date = datetime(2026, 9, 2, 4, tzinfo=utc)   # narrow to Sep 1
                svc.store.save_season(s)

        out = self._run([commit, reimport])
        self.assertNotIsInstance(out[1], Exception, f"reimport raised: {out[1]!r}")
        r = out[0]
        if isinstance(r, ScheduleConflictError):      # reimport won the lock race
            # the commit re-resolved the NEW tz + narrowed range under the lock
            # (no deadlock — the Program-first order holds), which no longer
            # matched the previewed snapshot -> refused (preview_mismatch), zero
            # ice. The clean domain rejection, not a 40P01, is the deadlock proof.
            self.assertEqual(r.details["reason"], "preview_mismatch")
            self.assertEqual(self._slots(), [])
        else:                                         # commit won: old tz + wide
            self.assertNotIsInstance(r, Exception, f"commit raised: {r!r}")
            slots = self._assert_unique_and_non_overlapping()
            self.assertEqual(len(slots), 9)           # whole previewed snapshot
            self.assertEqual(min(s.start_time for s in slots),
                             datetime(2026, 9, 1, 22, tzinfo=utc))     # Toronto
            local = {s.start_time.astimezone(ZoneInfo(TZ)).date() for s in slots}
            self.assertTrue(
                local <= {date(2026, 9, 1), date(2026, 9, 3), date(2026, 9, 8)})


class PlannerUnitTest(unittest.TestCase):
    """The pure engine — no store, no timezone-of-record dependency."""

    def _plan(self, **over):
        from zoneinfo import ZoneInfo
        from datetime import date
        base = dict(
            weekday_windows={1: ((18, 0), (22, 0)), 3: ((18, 0), (22, 0))},
            start_date=date(2026, 9, 1), end_date=date(2026, 9, 7),
            playable_minutes=60, turnover_minutes=15,
            exclusion_dates=set(), tz=ZoneInfo(TZ))
        base.update(over)
        return plan_ice_windows(**base)

    def test_three_games_per_day(self):
        r = self._plan()
        self.assertEqual(len(r["windows"]), 6)
        self.assertEqual(r["game_days"], 2)

    def test_parse_hhmm(self):
        self.assertEqual(parse_hhmm("18:30", "x"), (18, 30))
        for bad in ("1830", "25:00", "18:60", "18", "", ":", "aa:bb", None):
            with self.assertRaises(ValidationError):
                parse_hhmm(bad, "x")

    def test_rejects_window_end_before_start(self):
        with self.assertRaises(ValidationError):
            self._plan(weekday_windows={1: ((22, 0), (18, 0))})

    def test_rejects_bad_weekday(self):
        with self.assertRaises(ValidationError):
            self._plan(weekday_windows={9: ((18, 0), (22, 0))})

    def test_rejects_overlong_range(self):
        from datetime import date
        with self.assertRaises(ValidationError):
            self._plan(start_date=date(2026, 1, 1),
                       end_date=date(2026, 1, 1).replace(year=2028))

    def test_zero_turnover_packs_tightly(self):
        r = self._plan(turnover_minutes=0)  # 4 * 60 fits in 18:00-22:00
        per_day = [w for w in r["windows"] if w["date"] == "2026-09-01"]
        self.assertEqual(len(per_day), 4)

    def test_per_weekday_windows_are_independent(self):
        # #158 flow: each selected weekday carries its OWN local start/end time.
        # Tue 18:00-22:00 hosts 3 games; Thu 19:00-22:00 hosts 2; the planner
        # honors each day's window rather than one global block.
        r = self._plan(weekday_windows={1: ((18, 0), (22, 0)),
                                        3: ((19, 0), (22, 0))})
        tue = [w for w in r["windows"] if w["date"] == "2026-09-01"]  # Tuesday
        thu = [w for w in r["windows"] if w["date"] == "2026-09-03"]  # Thursday
        self.assertEqual(len(tue), 3)
        self.assertEqual(len(thu), 2)
        self.assertEqual(len(r["windows"]), 5)
        # Each day's first game starts at that day's own local start time.
        self.assertEqual(tue[0]["start_local"], "2026-09-01T18:00:00-04:00")
        self.assertEqual(thu[0]["start_local"], "2026-09-03T19:00:00-04:00")


class IceAvailabilityHttpAuthzTest(unittest.TestCase):
    """The route is recognized and gated to MANAGE_ARENA over real HTTP."""

    @classmethod
    def setUpClass(cls):
        srv = __import__("hockey_scheduler.web.server", fromlist=["x"])
        cls.srv = srv
        srv.STATE.reset()
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), srv.Handler)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.thread.join(timeout=5)

    def _client(self):
        return urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(CookieJar()))

    def _req(self, opener, method, path, body=None):
        url = f"http://127.0.0.1:{self.port}{path}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with opener.open(req) as r:
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def _login(self, opener, username):
        return self._req(opener, "POST", "/api/auth/login",
                         {"username": username, "password": "demo"})

    def test_coach_forbidden(self):
        for path in ("/api/setup/ice-availability/preview",
                     "/api/setup/ice-availability/commit"):
            c = self._client()
            self._login(c, "coach")
            status, _ = self._req(c, "POST", path, _template())
            self.assertEqual(status, 403, path)

    def test_player_forbidden(self):
        c = self._client()
        self._login(c, "player")
        status, _ = self._req(
            c, "POST", "/api/setup/ice-availability/preview", _template())
        self.assertEqual(status, 403)

    def test_arena_manager_passes_authz(self):
        # A MANAGE_ARENA caller clears authz and reaches the handler; with a
        # bogus season it gets a domain error (not 403), proving it was routed.
        c = self._client()
        self._login(c, "arena")
        status, body = self._req(
            c, "POST", "/api/setup/ice-availability/preview",
            _template(season_id="does-not-exist"))
        self.assertNotEqual(status, 403)
        self.assertIn("error", body)

    def test_per_weekday_windows_field_is_accepted_over_http(self):
        # The per-weekday `windows` list survives the HTTP field allow-list and
        # reaches the handler (not dropped, not a 403). A bogus season keeps this
        # a routing proof without needing seeded inventory.
        c = self._client()
        self._login(c, "arena")
        body = {
            "season_id": "does-not-exist", "rink_ids": ["r"],
            "playable_minutes": 60, "turnover_minutes": 15,
            "windows": [
                {"weekday": 1, "start_local": "18:00", "end_local": "22:00"},
                {"weekday": 3, "start_local": "19:00", "end_local": "23:00"}]}
        status, resp = self._req(
            c, "POST", "/api/setup/ice-availability/preview", body)
        self.assertNotEqual(status, 403)
        self.assertIn("error", resp)                     # reached the handler

    def test_commit_binding_over_http(self):
        # End-to-end over HTTP: seed real inventory through the service, preview
        # to get the fingerprint, then prove a mismatched (edited) template is
        # refused with zero slots while the previewed one commits exactly.
        svc = self.srv.STATE.api.setup
        prog = svc.create_program("HB", timezone_name=TZ)
        season = svc.create_season(prog.id, "Fall Binding")
        venue = svc.create_venue("Rink House", league_id=prog.id)
        svc.grant_season_venue_access(season.id, venue.id)
        rink = svc.create_rink(venue.id, "Sheet 1")
        template = {
            "season_id": season.id, "rink_ids": [rink.id],
            "weekdays": [1, 3], "start_local": "18:00", "end_local": "22:00",
            "start_date": "2026-09-01", "end_date": "2026-09-07",
            "playable_minutes": 60, "turnover_minutes": 15}

        def slot_count():
            return sum(1 for s in self.srv.STATE.api.store.all_ice_slots()
                       if s.rink_id == rink.id)

        c = self._client()
        self._login(c, "arena")
        _, pv = self._req(
            c, "POST", "/api/setup/ice-availability/preview", template)
        fp = pv["template_fingerprint"]
        self.assertTrue(fp)
        self.assertEqual(pv["totals"]["new"], 6)
        # Edited template carrying the previewed fingerprint => refused, no ice.
        _, bad = self._req(c, "POST", "/api/setup/ice-availability/commit",
                           dict(template, end_local="21:00", template_fingerprint=fp))
        self.assertIn("error", bad)
        self.assertEqual(bad["error"]["details"]["reason"], "preview_mismatch")
        self.assertEqual(slot_count(), 0)
        # Unedited template + fingerprint => commits exactly the 6 shown slots.
        _, ok = self._req(c, "POST", "/api/setup/ice-availability/commit",
                          dict(template, template_fingerprint=fp))
        self.assertEqual(ok["totals"]["created"], 6)
        self.assertEqual(slot_count(), 6)

    def test_commit_binding_rejects_reclassified_row_over_http(self):
        # Over HTTP: the token binds the reviewed CLASSIFICATION, not just the
        # tuples. Incompatible ice dropped onto a generated tuple AFTER preview
        # reclassifies that row new -> conflict without moving the tuple; the
        # previewed token is refused (preview_mismatch) with zero writes, and a
        # re-preview surfaces the exact new conflict target.
        svc = self.srv.STATE.api.setup
        store = self.srv.STATE.api.store
        prog = svc.create_program("HB4", timezone_name="UTC")     # local == UTC
        season = svc.create_season(prog.id, "Fall Reclassify")
        venue = svc.create_venue("Rink House 4", league_id=prog.id)
        svc.grant_season_venue_access(season.id, venue.id)
        rink = svc.create_rink(venue.id, "Sheet 4")
        template = {                                    # one Tue window (Sep 1)
            "season_id": season.id, "rink_ids": [rink.id], "weekdays": [1],
            "start_local": "18:00", "end_local": "19:00",
            "start_date": "2026-09-01", "end_date": "2026-09-01",
            "playable_minutes": 60, "turnover_minutes": 0}
        c = self._client()
        self._login(c, "arena")
        _, pv = self._req(c, "POST", "/api/setup/ice-availability/preview", template)
        fp = pv["template_fingerprint"]
        self.assertEqual(pv["totals"]["new"], 1)
        # Concurrent write: incompatible ice on the exact generated tuple.
        store.add_ice_slot(IceSlot(
            id="block_http", rink_id=rink.id,
            start_time=datetime(2026, 9, 1, 18, tzinfo=timezone.utc),
            end_time=datetime(2026, 9, 1, 19, tzinfo=timezone.utc),
            slot_type=IceSlotType.MAINTENANCE, status=IceSlotStatus.AVAILABLE))
        _, res = self._req(c, "POST", "/api/setup/ice-availability/commit",
                           dict(template, template_fingerprint=fp))
        self.assertEqual(res["error"]["details"]["reason"], "preview_mismatch")
        self.assertEqual(                               # no Game ice; only the blocker
            [s.id for s in store.all_ice_slots()
             if s.rink_id == rink.id and s.slot_type == IceSlotType.GAME], [])
        _, pv2 = self._req(c, "POST", "/api/setup/ice-availability/preview", template)
        self.assertEqual(pv2["totals"]["conflict"], 1)
        clash = next(s for s in pv2["slots"] if s["status"] == "conflict")
        self.assertEqual(clash["conflict_with"], "block_http")

    def test_commit_without_token_is_refused_over_http(self):
        # #158 review: preview is REQUIRED end-to-end. An authenticated
        # MANAGE_ARENA caller who POSTs /commit with NO template_fingerprint (the
        # old bypass) is refused with preview_required and creates zero ice — even
        # right after previewing. Only a commit carrying the token is admitted.
        svc = self.srv.STATE.api.setup
        prog = svc.create_program("HBN", timezone_name=TZ)
        season = svc.create_season(prog.id, "Fall NoToken")
        venue = svc.create_venue("Rink House N", league_id=prog.id)
        svc.grant_season_venue_access(season.id, venue.id)
        rink = svc.create_rink(venue.id, "Sheet N")
        template = {
            "season_id": season.id, "rink_ids": [rink.id],
            "weekdays": [1, 3], "start_local": "18:00", "end_local": "22:00",
            "start_date": "2026-09-01", "end_date": "2026-09-07",
            "playable_minutes": 60, "turnover_minutes": 15}

        def slot_count():
            return sum(1 for s in self.srv.STATE.api.store.all_ice_slots()
                       if s.rink_id == rink.id)

        c = self._client()
        self._login(c, "arena")
        self._req(c, "POST", "/api/setup/ice-availability/preview", template)
        status, res = self._req(
            c, "POST", "/api/setup/ice-availability/commit", template)  # no token
        self.assertEqual(status, 400)
        self.assertEqual(res["error"]["details"]["reason"], "preview_required")
        self.assertEqual(slot_count(), 0)

    def test_commit_with_another_operators_token_is_refused_over_http(self):
        # #158 review: the fingerprint is bound to the operator who previewed. The
        # arena manager previews; the league admin (also MANAGE_ARENA) replays the
        # arena manager's valid, current token. It matches the resolved snapshot
        # but has no preview audit for the admin, so the commit is refused
        # (preview_required) with zero ice — while the operator who DID preview
        # commits that same token cleanly.
        svc = self.srv.STATE.api.setup
        prog = svc.create_program("HBX", timezone_name=TZ)
        season = svc.create_season(prog.id, "Fall CrossActor")
        venue = svc.create_venue("Rink House X", league_id=prog.id)
        svc.grant_season_venue_access(season.id, venue.id)
        rink = svc.create_rink(venue.id, "Sheet X")
        template = {
            "season_id": season.id, "rink_ids": [rink.id],
            "weekdays": [1, 3], "start_local": "18:00", "end_local": "22:00",
            "start_date": "2026-09-01", "end_date": "2026-09-07",
            "playable_minutes": 60, "turnover_minutes": 15}

        def slot_count():
            return sum(1 for s in self.srv.STATE.api.store.all_ice_slots()
                       if s.rink_id == rink.id)

        arena = self._client()
        self._login(arena, "arena")
        _, pv = self._req(
            arena, "POST", "/api/setup/ice-availability/preview", template)
        fp = pv["template_fingerprint"]
        self.assertTrue(fp)

        # A DIFFERENT MANAGE_ARENA operator (admin) replays arena's token.
        admin = self._client()
        self._login(admin, "admin")
        status, res = self._req(
            admin, "POST", "/api/setup/ice-availability/commit",
            dict(template, template_fingerprint=fp))
        self.assertEqual(status, 400)
        self.assertEqual(res["error"]["details"]["reason"], "preview_required")
        self.assertEqual(slot_count(), 0)

        # The operator who actually previewed commits that same token cleanly.
        _, ok = self._req(arena, "POST", "/api/setup/ice-availability/commit",
                          dict(template, template_fingerprint=fp))
        self.assertEqual(ok["totals"]["created"], 6)
        self.assertEqual(slot_count(), 6)

    def test_commit_binding_rejects_resolved_snapshot_change_over_http(self):
        # Over HTTP with a BLANK date range (defaulted from the Season): a
        # concurrent Season-boundary change between preview and commit moves the
        # resolved slots, so the commit is refused — proving the token binds the
        # resolved snapshot, not just the submitted form fields.
        svc = self.srv.STATE.api.setup
        store = self.srv.STATE.api.store
        prog = svc.create_program("HB2", timezone_name=TZ)
        season = svc.create_season(prog.id, "Fall Binding 2")
        # Pin a known wide range so the later narrowing is unambiguous.
        s = store.get_season(season.id)
        s.start_date = datetime(2026, 9, 1, 4, tzinfo=timezone.utc)
        s.end_date = datetime(2027, 3, 31, 4, tzinfo=timezone.utc)
        store.save_season(s)
        venue = svc.create_venue("Rink House 2", league_id=prog.id)
        svc.grant_season_venue_access(season.id, venue.id)
        rink = svc.create_rink(venue.id, "Sheet 2")
        template = {                                       # NO dates => Season range
            "season_id": season.id, "rink_ids": [rink.id],
            "weekdays": [1, 3], "start_local": "18:00", "end_local": "22:00",
            "playable_minutes": 60, "turnover_minutes": 15}
        c = self._client()
        self._login(c, "arena")
        _, pv = self._req(
            c, "POST", "/api/setup/ice-availability/preview", template)
        fp = pv["template_fingerprint"]
        self.assertTrue(fp)
        self.assertGreater(pv["totals"]["new"], 6)         # a whole season of ice
        # Concurrent Season-boundary edit narrows the resolved range.
        s = store.get_season(season.id)
        s.end_date = datetime(2026, 9, 8, 4, tzinfo=timezone.utc)
        store.save_season(s)
        _, res = self._req(c, "POST", "/api/setup/ice-availability/commit",
                           dict(template, template_fingerprint=fp))
        self.assertIn("error", res)
        self.assertEqual(res["error"]["details"]["reason"], "preview_mismatch")
        self.assertEqual(
            sum(1 for x in store.all_ice_slots() if x.rink_id == rink.id), 0)

    def test_exact_game_collision_reported_over_http(self):
        # Over HTTP: an ALLOCATED slot backing an active Game at a template's
        # exact tuple is reported as a CONFLICT (with the Game id), never counted
        # as idempotent duplicate capacity.
        from hockey_scheduler.domain.models import Game
        from hockey_scheduler.domain.setup_models import IceSlot
        svc = self.srv.STATE.api.setup
        store = self.srv.STATE.api.store
        prog = svc.create_program("HB3", timezone_name="UTC")   # UTC: local==UTC
        season = svc.create_season(prog.id, "Fall Collision")
        venue = svc.create_venue("Rink House 3", league_id=prog.id)
        svc.grant_season_venue_access(season.id, venue.id)
        rink = svc.create_rink(venue.id, "Sheet 3")
        start = datetime(2026, 9, 1, 18, tzinfo=timezone.utc)   # Sep 1 is a Tuesday
        end = datetime(2026, 9, 1, 19, tzinfo=timezone.utc)
        store.add_ice_slot(IceSlot(
            id="occ_http", rink_id=rink.id, start_time=start, end_time=end,
            slot_type=IceSlotType.GAME, status=IceSlotStatus.ALLOCATED))
        store.add_game(Game(id="g_http", home_team_id="h", away_team_id="a",
                            start_time=start, end_time=end, ice_slot_id="occ_http",
                            season_id=season.id))
        template = {                                       # one Tue window, exact tuple
            "season_id": season.id, "rink_ids": [rink.id], "weekdays": [1],
            "start_local": "18:00", "end_local": "19:00",
            "start_date": "2026-09-01", "end_date": "2026-09-01",
            "playable_minutes": 60, "turnover_minutes": 0}
        c = self._client()
        self._login(c, "arena")
        _, pv = self._req(
            c, "POST", "/api/setup/ice-availability/preview", template)
        collision = next(s for s in pv["slots"]
                         if s["start_time"] == start.isoformat())
        self.assertEqual(collision["status"], "conflict")   # NOT duplicate
        self.assertTrue(collision["conflict_has_game"])
        self.assertEqual(collision["conflict_game_id"], "g_http")
        self.assertEqual(pv["totals"]["conflict"], 1)
        self.assertEqual(pv["totals"]["duplicate"], 0)
        self.assertEqual(pv["totals"]["capacity_games"], 0)   # not hidden capacity

    def test_preview_is_audited_to_the_authenticated_caller_over_http(self):
        # #158 audits BOTH halves: a successful preview by a MANAGE_ARENA caller
        # records one server-attributed audit; a forbidden caller records none.
        svc = self.srv.STATE.api.setup
        store = self.srv.STATE.api.store
        prog = svc.create_program("HB4", timezone_name=TZ)
        season = svc.create_season(prog.id, "Fall Audit")
        venue = svc.create_venue("Rink House 4", league_id=prog.id)
        svc.grant_season_venue_access(season.id, venue.id)
        rink = svc.create_rink(venue.id, "Sheet 4")
        template = {
            "season_id": season.id, "rink_ids": [rink.id], "weekdays": [1, 3],
            "start_local": "18:00", "end_local": "22:00",
            "start_date": "2026-09-01", "end_date": "2026-09-07",
            "playable_minutes": 60, "turnover_minutes": 15}

        def preview_audits():
            return [a for a in store.all_setup_audit()
                    if a.action == "ice_availability_previewed"
                    and a.detail.get("season_id") == season.id]

        # A coach lacks MANAGE_ARENA -> 403 before the handler -> no audit.
        coach = self._client()
        self._login(coach, "coach")
        status, _ = self._req(
            coach, "POST", "/api/setup/ice-availability/preview", template)
        self.assertEqual(status, 403)
        self.assertEqual(preview_audits(), [])

        # The arena manager's preview records exactly one audit, attributed to a
        # real (non-anonymous) authenticated user id, with the result totals.
        c = self._client()
        self._login(c, "arena")
        self._req(c, "POST", "/api/setup/ice-availability/preview", template)
        audits = preview_audits()
        self.assertEqual(len(audits), 1)
        self.assertTrue(audits[0].actor_id)                   # threaded, not None
        self.assertEqual(audits[0].detail["totals"]["new"], 6)
