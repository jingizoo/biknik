"""Season start/end boundaries via the canonical hierarchy import (#272).

The import path shares the interactive path's parser (`parse_season_boundary`)
and the effective Program timezone, so a date-only `season_start`/`season_end`
column on the `competition` sheet normalizes identically to manual setup. See
docs/architecture/season-dates.md.
"""

import os
import unittest

from helpers import BACKEND  # noqa: F401

import hierarchy_fixtures as fx
from hockey_scheduler.api import ApiService
from hockey_scheduler.store import InMemoryStore, SqlStore

# competition column order:
# program_code,season_code,season_name,league_code,league_name,
# league_sort_order,division_code,division_name,age_group,
# season_start,season_end
_OLD_COMPETITION_HEADER = (
    "program_code,season_code,season_name,league_code,league_name,"
    "league_sort_order,division_code,division_name,age_group")


def _comp(season_code="FALL26", start="", end="", program="OVER55",
          season_name="Fall 2026", league="L1", division="DIVA",
          division_name="Division A"):
    return (f"{program},{season_code},{season_name},{league},Adult League,1,"
            f"{division},{division_name},Adult,{start},{end}")


def _programs(timezone_name):
    return fx.programs_csv(rows=(f"OVER55,CANLON,Over 55,US,{timezone_name}",))


def _backends():
    yield "memory", InMemoryStore()
    yield "sqlite", SqlStore(":memory:")
    url = os.environ.get("TEST_DATABASE_URL")
    if url:
        pg = SqlStore(url)
        pg.reset_schema()
        yield "postgres", pg


class ImportSeasonBoundaryTest(unittest.TestCase):
    def _each(self, timezone_name="America/New_York"):
        for label, store in _backends():
            with self.subTest(backend=label):
                api = ApiService(store)
                try:
                    yield label, store, api, timezone_name
                finally:
                    conn = getattr(store, "conn", None)
                    if conn is not None:
                        conn.close()

    def _season(self, store, code="FALL26"):
        return fx.by_ref(store.all_seasons(), code)

    def _payload(self, timezone_name, rows):
        # Focused on the competition/season sheets: empty the registration and
        # venue-access sheets so custom season_codes don't trip their default
        # FALL26 cross-references.
        return fx.full_payload(
            programs_csv=_programs(timezone_name),
            competition_csv=fx.competition_csv(rows=rows),
            registrations_csv="", season_venue_access_csv="")

    def _commit(self, api, timezone_name, rows):
        return api.commit_hierarchy_import(
            self._payload(timezone_name, rows), actor_id="admin")

    def _preview(self, api, timezone_name, rows):
        return api.get_hierarchy_import_dry_run(
            self._payload(timezone_name, rows))

    # -- date-only anchoring ------------------------------------------------
    def test_date_only_anchored_to_program_timezone(self):
        for label, store, api, tz in self._each("America/New_York"):
            res = self._commit(api, tz, (_comp(start="2026-09-15",
                                               end="2027-03-15"),))
            self.assertTrue(res["committed"], res.get("errors"))
            season = self._season(store)
            self.assertEqual(season.start_date.isoformat(),
                             "2026-09-15T04:00:00+00:00", label)  # midnight EDT
            self.assertEqual(season.end_date.isoformat(),
                             "2027-03-15T04:00:00+00:00", label)

    def test_positive_offset_timezone(self):
        for label, store, api, tz in self._each("Asia/Tokyo"):
            self._commit(api, tz, (_comp(start="2026-09-15"),))
            self.assertEqual(self._season(store).start_date.isoformat(),
                             "2026-09-14T15:00:00+00:00", label)  # midnight JST

    def test_dst_determinism(self):
        for label, store, api, tz in self._each("America/New_York"):
            self._commit(api, tz, (
                _comp(season_code="WIN", season_name="Winter",
                      start="2026-01-10", division="DW", division_name="DW"),
                _comp(season_code="SUM", season_name="Summer",
                      start="2026-07-10", division="DS", division_name="DS")))
            self.assertEqual(self._season(store, "WIN").start_date.isoformat(),
                             "2026-01-10T05:00:00+00:00", label)  # EST
            self.assertEqual(self._season(store, "SUM").start_date.isoformat(),
                             "2026-07-10T04:00:00+00:00", label)  # EDT

    def test_aware_iso_roundtrips(self):
        for label, store, api, tz in self._each("America/New_York"):
            self._commit(api, tz, (_comp(start="2026-09-15T12:30:00+00:00"),))
            self.assertEqual(self._season(store).start_date.isoformat(),
                             "2026-09-15T12:30:00+00:00", label)

    # -- validation (preview) + zero-write rollback -------------------------
    def test_invalid_date_preview_error_and_no_write(self):
        for label, store, api, tz in self._each():
            prev = self._preview(api, tz, (_comp(start="2026-13-40"),))
            self.assertFalse(prev["ok"], label)
            self.assertTrue(any(e["code"] == "invalid_season_start"
                                for e in prev["errors"]), prev["errors"])
            res = self._commit(api, tz, (_comp(start="2026-13-40"),))
            self.assertFalse(res["committed"], label)
            self.assertEqual(len(store.all_seasons()), 0, label)  # zero writes

    def test_naive_timestamp_rejected(self):
        for label, store, api, tz in self._each():
            prev = self._preview(api, tz, (_comp(start="2026-09-15T18:30:00"),))
            self.assertTrue(any(e["code"] == "invalid_season_start"
                                for e in prev["errors"]), prev["errors"])

    def test_reversed_range_preview_error_and_no_write(self):
        for label, store, api, tz in self._each("America/New_York"):
            prev = self._preview(api, tz, (_comp(start="2026-09-15",
                                                 end="2026-09-01"),))
            self.assertTrue(any(e["code"] == "season_end_before_start"
                                for e in prev["errors"]), prev["errors"])
            res = self._commit(api, tz, (_comp(start="2026-09-15",
                                               end="2026-09-01"),))
            self.assertFalse(res["committed"], label)
            self.assertEqual(len(store.all_seasons()), 0, label)

    # -- repeated rows, compared AFTER normalization ------------------------
    def test_repeated_rows_conflicting_dates_rejected(self):
        for label, store, api, tz in self._each("America/New_York"):
            prev = self._preview(api, tz, (
                _comp(start="2026-09-15", division="DIVA"),
                _comp(start="2026-09-16", division="DIVB",
                      division_name="Division B")))
            self.assertTrue(any(e["code"] == "inconsistent_season_dates"
                                for e in prev["errors"]), prev["errors"])

    def test_repeated_rows_normalized_equal_are_consistent(self):
        # A date-only and its equivalent NY-midnight aware timestamp resolve to
        # the same UTC instant → NOT a conflict.
        for label, store, api, tz in self._each("America/New_York"):
            res = self._commit(api, tz, (
                _comp(start="2026-09-15", division="DIVA"),
                _comp(start="2026-09-15T00:00:00-04:00", division="DIVB",
                      division_name="Division B")))
            self.assertTrue(res["committed"], res.get("errors"))
            self.assertEqual(self._season(store).start_date.isoformat(),
                             "2026-09-15T04:00:00+00:00", label)

    def test_blank_plus_supplied_is_not_a_conflict(self):
        for label, store, api, tz in self._each("America/New_York"):
            res = self._commit(api, tz, (
                _comp(start="", division="DIVA"),
                _comp(start="2026-09-15", division="DIVB",
                      division_name="Division B")))
            self.assertTrue(res["committed"], res.get("errors"))
            self.assertEqual(self._season(store).start_date.isoformat(),
                             "2026-09-15T04:00:00+00:00", label)

    # -- re-import: blank preservation, idempotency, merged range -----------
    def test_blank_reimport_preserves_stored_boundaries(self):
        for label, store, api, tz in self._each("America/New_York"):
            self._commit(api, tz, (_comp(start="2026-09-15", end="2027-03-15"),))
            # Re-import the same season with BLANK date cells.
            res = self._commit(api, tz, (_comp(start="", end=""),))
            self.assertTrue(res["committed"], res.get("errors"))
            self.assertEqual(res["summary"]["seasons"]["updated"], 0, label)
            season = self._season(store)
            self.assertEqual(season.start_date.isoformat(),
                             "2026-09-15T04:00:00+00:00", label)  # preserved
            self.assertEqual(season.end_date.isoformat(),
                             "2027-03-15T04:00:00+00:00", label)

    def test_idempotent_equivalent_reimport_makes_no_update(self):
        for label, store, api, tz in self._each("America/New_York"):
            self._commit(api, tz, (_comp(start="2026-09-15"),))
            # An equivalent aware form of the same instant → no false update.
            res = self._commit(api, tz, (_comp(start="2026-09-15T00:00:00-04:00"),))
            self.assertEqual(res["summary"]["seasons"]["updated"], 0, label)

    def test_partial_update_reversed_against_stored_side_rejected(self):
        for label, store, api, tz in self._each("America/New_York"):
            self._commit(api, tz, (_comp(start="2026-09-15", end="2027-03-15"),))
            # Supply ONLY a new end that is before the STORED start.
            prev = self._preview(api, tz, (_comp(start="", end="2026-09-01"),))
            self.assertTrue(any(e["code"] == "season_end_before_start"
                                for e in prev["errors"]), prev["errors"])

    def test_partial_update_supplies_one_side_preserves_other(self):
        for label, store, api, tz in self._each("America/New_York"):
            self._commit(api, tz, (_comp(start="2026-09-15", end="2027-03-15"),))
            # Move only the end forward; start is preserved.
            res = self._commit(api, tz, (_comp(start="", end="2027-04-01"),))
            self.assertTrue(res["committed"], res.get("errors"))
            season = self._season(store)
            self.assertEqual(season.start_date.isoformat(),
                             "2026-09-15T04:00:00+00:00", label)  # preserved
            self.assertEqual(season.end_date.isoformat(),
                             "2027-04-01T04:00:00+00:00", label)  # updated

    # -- timezone change on a Program with dated Seasons is rejected --------
    def test_timezone_change_with_dated_seasons_rejected_zero_writes(self):
        # A UTC Program with a dated S1; a same-upload timezone change to
        # New_York (plus a new S2) must be rejected with zero writes — changing
        # the zone would silently reinterpret S1's stored instant (#272 review).
        for label, store, api, tz in self._each("UTC"):
            self._commit(api, "UTC", (_comp(season_code="S1", start="2026-09-15",
                                            division="D1", division_name="D1"),))
            s1_start = self._season(store, "S1").start_date
            audits_before = len(store.all_setup_audit())
            prev = self._preview(api, "America/New_York",
                                 (_comp(season_code="S1", start="2026-09-15",
                                        division="D1", division_name="D1"),
                                  _comp(season_code="S2", start="2026-10-01",
                                        division="D2", division_name="D2")))
            self.assertTrue(any(e["code"] == "program_timezone_in_use"
                                and e["sheet"] == "programs"
                                and e.get("field") == "timezone"
                                for e in prev["errors"]), prev["errors"])
            res = self._commit(api, "America/New_York",
                               (_comp(season_code="S1", start="2026-09-15",
                                      division="D1", division_name="D1"),
                                _comp(season_code="S2", start="2026-10-01",
                                      division="D2", division_name="D2")))
            self.assertFalse(res["committed"], label)
            # Program zone, S1 instant, audits, and absence of S2 all unchanged.
            self.assertEqual(fx.by_ref(store.all_programs(), "OVER55").timezone,
                             "UTC", label)
            self.assertEqual(self._season(store, "S1").start_date, s1_start, label)
            self.assertEqual(len(store.all_setup_audit()), audits_before, label)
            self.assertEqual([s for s in store.all_seasons()
                              if s.external_ref == "S2"], [], label)

    def test_same_zone_reimport_with_dated_seasons_is_allowed(self):
        for label, store, api, tz in self._each("America/New_York"):
            self._commit(api, "America/New_York", (_comp(start="2026-09-15"),))
            res = self._commit(api, "America/New_York", (_comp(start="2026-09-15"),))
            self.assertTrue(res["committed"], res.get("errors"))
            self.assertEqual(res["summary"]["programs"]["updated"], 0, label)

    def test_timezone_change_allowed_when_no_dated_seasons(self):
        # A Program whose only Season has NO stored boundary may still change
        # timezone, and a NEW date-only value anchors in the new zone.
        for label, store, api, tz in self._each("UTC"):
            self._commit(api, "UTC", (_comp(season_code="S1", division="D1",
                                            division_name="D1"),))  # no dates
            self.assertIsNone(self._season(store, "S1").start_date, label)
            res = self._commit(api, "America/New_York",
                               (_comp(season_code="S1", division="D1",
                                      division_name="D1"),
                                _comp(season_code="S2", start="2026-09-15",
                                      division="D2", division_name="D2")))
            self.assertTrue(res["committed"], res.get("errors"))
            self.assertEqual(fx.by_ref(store.all_programs(), "OVER55").timezone,
                             "America/New_York", label)
            self.assertEqual(self._season(store, "S2").start_date.isoformat(),
                             "2026-09-15T04:00:00+00:00", label)  # New_York

    # -- uploaded Program timezone must be a real IANA zone -----------------
    def test_new_program_invalid_timezone_rejected_zero_writes(self):
        for label, store, api, tz in self._each("Not/AZone"):
            rows = (_comp(start="2026-09-15"),)
            prev = self._preview(api, "Not/AZone", rows)
            self.assertFalse(prev["ok"], label)
            self.assertTrue(any(e["code"] == "invalid_timezone"
                                and e["sheet"] == "programs"
                                and e.get("field") == "timezone"
                                for e in prev["errors"]), prev["errors"])
            res = self._commit(api, "Not/AZone", rows)
            self.assertFalse(res["committed"], label)
            self.assertEqual(len(store.all_programs()), 0, label)  # no program
            self.assertEqual(len(store.all_seasons()), 0, label)   # no season
            self.assertEqual(  # no fallback-created boundary, no audit
                [a for a in store.all_setup_audit()
                 if a.action in ("program_created", "season_created")], [], label)

    def test_existing_program_changed_to_invalid_timezone_rejected(self):
        for label, store, api, tz in self._each("America/New_York"):
            # First a valid import establishes the Program + a dated Season.
            self._commit(api, "America/New_York", (_comp(start="2026-09-15"),))
            season = self._season(store)
            stored_start = season.start_date
            # Re-import changing ONLY the Program timezone to an invalid zone.
            prev = self._preview(api, "Not/AZone", (_comp(start="2026-09-15"),))
            self.assertTrue(any(e["code"] == "invalid_timezone"
                                and e["sheet"] == "programs"
                                for e in prev["errors"]), prev["errors"])
            res = self._commit(api, "Not/AZone", (_comp(start="2026-09-15"),))
            self.assertFalse(res["committed"], label)
            # The stored Program zone + Season instant are untouched.
            self.assertEqual(fx.by_ref(store.all_programs(), "OVER55").timezone,
                             "America/New_York", label)
            self.assertEqual(self._season(store).start_date, stored_start, label)

    def test_blank_program_timezone_defaults_to_utc(self):
        for label, store, api, tz in self._each(""):
            res = self._commit(api, "", (_comp(start="2026-09-15"),))
            self.assertTrue(res["committed"], res.get("errors"))
            self.assertEqual(fx.by_ref(store.all_programs(), "OVER55").timezone,
                             "UTC", label)
            self.assertEqual(self._season(store).start_date.isoformat(),
                             "2026-09-15T00:00:00+00:00", label)  # midnight UTC

    # -- old template without the optional columns --------------------------
    def test_old_template_without_columns_is_accepted(self):
        for label, store, api, tz in self._each("America/New_York"):
            old_comp = (_OLD_COMPETITION_HEADER + "\n"
                        "OVER55,FALL26,Fall 2026,L1,Adult League,1,"
                        "DIVA,Division A,Adult\n")
            res = api.commit_hierarchy_import(
                fx.full_payload(programs_csv=_programs(tz),
                                competition_csv=old_comp,
                                registrations_csv="",
                                season_venue_access_csv=""), actor_id="admin")
            self.assertTrue(res["committed"], res.get("errors"))
            season = self._season(store)
            self.assertIsNone(season.start_date, label)
            self.assertIsNone(season.end_date, label)


if __name__ == "__main__":
    unittest.main()
