"""Season date-boundary timezone semantics (#272).

A season boundary is primarily a calendar date. `create_season` accepts a
date-only `YYYY-MM-DD` (anchored to local midnight in the Program's timezone and
stored as the equivalent UTC instant) as well as the existing timezone-aware
ISO-8601 form (stored as that instant, no drift). Naive timestamps that carry a
time stay rejected. See docs/architecture/season-dates.md.
"""

import json
import os
import threading
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timezone
from http.cookiejar import CookieJar
from http.server import ThreadingHTTPServer

from helpers import BACKEND  # noqa: F401  (ensures sys.path is set up)

from hockey_scheduler.api import ApiService
from hockey_scheduler.services.setup_service import (
    parse_season_boundary, resolve_timezone)
from hockey_scheduler.store import InMemoryStore, SqlStore

UTC = timezone.utc


def _backends():
    yield "memory", InMemoryStore()
    yield "sqlite", SqlStore(":memory:")
    url = os.environ.get("TEST_DATABASE_URL")
    if url:
        pg = SqlStore(url)
        pg.reset_schema()
        yield "postgres", pg


class SeasonDateBoundaryTest(unittest.TestCase):
    def _each(self, timezone_name="America/New_York"):
        for label, store in _backends():
            with self.subTest(backend=label):
                api = ApiService(store)
                prog = api.create_program("Prog", "US", timezone_name)
                try:
                    yield label, store, api, prog["id"]
                finally:
                    if isinstance(store, SqlStore):
                        store.close()

    # -- date-only anchoring ------------------------------------------------
    def test_date_only_anchored_to_program_timezone(self):
        # A date-only bound resolves to LOCAL MIDNIGHT in the Program tz, stored
        # as the UTC instant — and round-trips to the same calendar day.
        for label, store, api, prog in self._each("America/New_York"):
            res = api.create_season(prog, "2026-27",
                                    start_date="2026-09-15", end_date="2027-03-15")
            self.assertNotIn("error", res, label)
            # Midnight EDT (UTC-4) on 2026-09-15 == 04:00Z.
            self.assertEqual(res["start_date"], "2026-09-15T04:00:00+00:00", label)
            # Midnight EDT (UTC-4) on 2027-03-15 (DST already begun) == 04:00Z.
            self.assertEqual(res["end_date"], "2027-03-15T04:00:00+00:00", label)
            # Round-trips to the intended calendar day in the Program tz.
            back = store.get_season(res["id"]).start_date.astimezone(
                resolve_timezone("America/New_York"))
            self.assertEqual((back.year, back.month, back.day), (2026, 9, 15), label)

    def test_date_only_positive_offset_timezone(self):
        # A positive-offset zone anchors midnight to the PREVIOUS UTC day; the
        # displayed local day is still the entered date (no adjacent-day bug).
        for label, store, api, prog in self._each("Asia/Tokyo"):
            res = api.create_season(prog, "2026-27", start_date="2026-09-15")
            self.assertEqual(res["start_date"], "2026-09-14T15:00:00+00:00", label)
            back = store.get_season(res["id"]).start_date.astimezone(
                resolve_timezone("Asia/Tokyo"))
            self.assertEqual((back.year, back.month, back.day), (2026, 9, 15), label)

    def test_dst_boundary_is_deterministic(self):
        # Same calendar date, opposite sides of the US spring-forward (2026-03-08
        # 02:00): a Jan date anchors at midnight EST (UTC-5 → 05:00Z), a July date
        # at midnight EDT (UTC-4 → 04:00Z). The offset follows the zone db.
        for label, store, api, prog in self._each("America/New_York"):
            winter = api.create_season(prog, "W", start_date="2026-01-10")
            summer = api.create_season(prog, "S", start_date="2026-07-10")
            self.assertEqual(winter["start_date"], "2026-01-10T05:00:00+00:00", label)
            self.assertEqual(summer["start_date"], "2026-07-10T04:00:00+00:00", label)

    # -- timezone-aware round-trip -----------------------------------------
    def test_aware_iso_roundtrips_without_drift(self):
        for label, store, api, prog in self._each("America/New_York"):
            # A UTC instant is stored verbatim.
            r1 = api.create_season(prog, "A", start_date="2026-09-15T12:30:00+00:00")
            self.assertEqual(r1["start_date"], "2026-09-15T12:30:00+00:00", label)
            # A non-UTC offset is converted to the SAME instant in UTC.
            r2 = api.create_season(prog, "B", start_date="2026-09-15T00:00:00-04:00")
            self.assertEqual(r2["start_date"], "2026-09-15T04:00:00+00:00", label)

    def test_mixed_date_only_and_aware_bounds(self):
        for label, store, api, prog in self._each("America/New_York"):
            res = api.create_season(
                prog, "M", start_date="2026-09-15",
                end_date="2027-03-15T23:00:00+00:00")
            self.assertEqual(res["start_date"], "2026-09-15T04:00:00+00:00", label)
            self.assertEqual(res["end_date"], "2027-03-15T23:00:00+00:00", label)

    # -- validation ---------------------------------------------------------
    def test_naive_timestamp_with_time_is_rejected(self):
        for label, store, api, prog in self._each():
            res = api.create_season(prog, "N", start_date="2026-09-15T18:30:00")
            self.assertEqual(res["error"]["details"]["reason"],
                             "invalid_start_date", label)
            self.assertEqual(res["error"]["details"]["field"], "start_date", label)
            self.assertEqual(len(store.all_seasons()), 0, label)  # nothing written

    def test_garbage_date_is_rejected(self):
        for label, store, api, prog in self._each():
            res = api.create_season(prog, "G", start_date="2026-13-40")
            self.assertEqual(res["error"]["details"]["reason"],
                             "invalid_start_date", label)
            self.assertEqual(len(store.all_seasons()), 0, label)

    def test_end_before_start_is_field_error(self):
        for label, store, api, prog in self._each("America/New_York"):
            res = api.create_season(prog, "E",
                                    start_date="2026-09-15", end_date="2026-09-01")
            self.assertEqual(res["error"]["details"]["reason"],
                             "end_before_start", label)
            self.assertEqual(res["error"]["details"]["field"], "end_date", label)
            self.assertEqual(len(store.all_seasons()), 0, label)

    def test_equal_start_and_end_is_allowed(self):
        for label, store, api, prog in self._each("America/New_York"):
            res = api.create_season(prog, "One-day",
                                    start_date="2026-09-15", end_date="2026-09-15")
            self.assertNotIn("error", res, label)
            self.assertEqual(res["start_date"], res["end_date"], label)

    def test_unset_bounds_stay_none(self):
        for label, store, api, prog in self._each():
            res = api.create_season(prog, "Dateless")
            self.assertIsNone(res["start_date"], label)
            self.assertIsNone(res["end_date"], label)


class ProgramTimezoneValidationTest(unittest.TestCase):
    # Non-string / non-null values that must be rejected with a stable field
    # error and NO Program/audit mutation (#272 review): a falsy non-string must
    # not silently become UTC, a truthy non-string must not 500 inside ZoneInfo.
    BAD_TYPES = [False, True, 0, 1, 3.5, [], ["x"], {}, {"a": 1}]
    BAD_NAMES = ["Not/AZone", "utc/utc", "America/Nowhere"]

    def test_bad_type_timezone_rejected_no_mutation(self):
        for label, store in _backends():
            with self.subTest(backend=label):
                api = ApiService(store)
                for bad in self.BAD_TYPES:
                    res = api.create_program("Bad", "US", bad)
                    self.assertEqual(res["error"]["details"]["reason"],
                                     "invalid_timezone", (label, repr(bad)))
                    self.assertEqual(res["error"]["details"]["field"],
                                     "timezone", (label, repr(bad)))
                self.assertEqual(len(store.all_programs()), 0, label)  # no writes
                if isinstance(store, SqlStore):
                    store.close()

    def test_malformed_name_rejected(self):
        for label, store in _backends():
            with self.subTest(backend=label):
                api = ApiService(store)
                for bad in self.BAD_NAMES:
                    res = api.create_program("Bad", "US", bad)
                    self.assertEqual(res["error"]["details"]["reason"],
                                     "invalid_timezone", (label, bad))
                self.assertEqual(len(store.all_programs()), 0, label)
                if isinstance(store, SqlStore):
                    store.close()

    def test_valid_iana_timezone_accepted(self):
        for label, store in _backends():
            with self.subTest(backend=label):
                api = ApiService(store)
                res = api.create_program("Good", "US", "America/Chicago")
                self.assertEqual(res["timezone"], "America/Chicago", label)
                if isinstance(store, SqlStore):
                    store.close()

    def test_null_and_blank_default_to_utc(self):
        for label, store in _backends():
            with self.subTest(backend=label):
                api = ApiService(store)
                self.assertEqual(api.create_program("N", "US", None)["timezone"],
                                 "UTC", label)
                self.assertEqual(api.create_program("B", "US", "   ")["timezone"],
                                 "UTC", label)
                if isinstance(store, SqlStore):
                    store.close()


class LegacyTimezoneFallbackTest(unittest.TestCase):
    """A pre-existing Program with an unresolved timezone (legacy data that
    predates create_program validation) still creates seasons — a date-only
    bound falls back to UTC midnight rather than 500ing."""

    def test_date_only_falls_back_to_utc_for_unknown_zone(self):
        from hockey_scheduler.domain import Program
        for label, store in _backends():
            with self.subTest(backend=label):
                api = ApiService(store)
                store.add_program(Program(id="p_legacy", name="Legacy",
                                          timezone="Mars/Phobos"))
                res = api.create_season("p_legacy", "L", start_date="2026-09-15")
                self.assertEqual(res["start_date"], "2026-09-15T00:00:00+00:00",
                                 label)
                if isinstance(store, SqlStore):
                    store.close()


class ParseSeasonBoundaryUnitTest(unittest.TestCase):
    def test_none_and_empty(self):
        tz = resolve_timezone("UTC")
        self.assertIsNone(parse_season_boundary(None, "start_date", tz))
        self.assertIsNone(parse_season_boundary("", "start_date", tz))

    def test_aware_datetime_object_passthrough(self):
        tz = resolve_timezone("America/New_York")
        dt = datetime(2026, 9, 15, 12, 0, tzinfo=UTC)
        self.assertEqual(parse_season_boundary(dt, "start_date", tz), dt)

    def test_naive_datetime_object_rejected(self):
        from hockey_scheduler.domain.errors import ValidationError
        tz = resolve_timezone("UTC")
        with self.assertRaises(ValidationError):
            parse_season_boundary(datetime(2026, 9, 15, 12, 0), "start_date", tz)


class SeasonDateHttpTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from hockey_scheduler.web import server as srv
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

    def _admin(self):
        c = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(CookieJar()))
        self._req(c, "POST", "/api/auth/login",
                  {"username": "admin", "password": "demo"})
        return c

    def _req(self, opener, method, path, body=None):
        url = f"http://127.0.0.1:{self.port}{path}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method,
                                     headers={"Content-Type": "application/json"})
        try:
            with opener.open(req) as r:
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def test_date_only_season_create_over_the_wire(self):
        # Manual setup and canonical import share this v2 route → same parser.
        c = self._admin()
        _s, prog = self._req(c, "POST", "/api/v2/setup/program",
                             {"name": "HTTP Prog", "country": "US",
                              "timezone": "America/New_York"})
        s, season = self._req(c, "POST", "/api/v2/setup/season",
                              {"program_id": prog["id"], "name": "2026-27",
                               "start_date": "2026-09-15", "end_date": "2027-03-15"})
        self.assertEqual(s, 200, season)
        self.assertEqual(season["start_date"], "2026-09-15T04:00:00+00:00")
        self.assertEqual(season["end_date"], "2027-03-15T04:00:00+00:00")

    def test_naive_timestamp_season_create_is_400(self):
        c = self._admin()
        _s, prog = self._req(c, "POST", "/api/v2/setup/program",
                             {"name": "HTTP Prog 2", "timezone": "UTC"})
        s, err = self._req(c, "POST", "/api/v2/setup/season",
                          {"program_id": prog["id"], "name": "Bad",
                           "start_date": "2026-09-15T18:30:00"})
        self.assertEqual(s, 400, err)
        self.assertEqual(err["error"]["details"]["reason"], "invalid_start_date")

    def test_end_before_start_over_the_wire_is_400(self):
        c = self._admin()
        _s, prog = self._req(c, "POST", "/api/v2/setup/program",
                             {"name": "HTTP Prog 3", "timezone": "America/New_York"})
        s, err = self._req(c, "POST", "/api/v2/setup/season",
                          {"program_id": prog["id"], "name": "Rev",
                           "start_date": "2026-09-15", "end_date": "2026-09-01"})
        self.assertEqual(s, 400, err)
        self.assertEqual(err["error"]["details"]["reason"], "end_before_start")

    def test_bad_type_program_timezone_over_the_wire_is_400(self):
        # A JSON bool/number/array/object timezone must be a stable 400, never a
        # 500 from ZoneInfo or a silent UTC coercion (#272 review).
        c = self._admin()
        for bad in [False, 0, 123, [], {}, "Not/AZone"]:
            s, err = self._req(c, "POST", "/api/v2/setup/program",
                              {"name": "HTTP Bad TZ", "timezone": bad})
            self.assertEqual(s, 400, (bad, err))
            self.assertEqual(err["error"]["details"]["reason"],
                             "invalid_timezone", (bad, err))
            self.assertEqual(err["error"]["details"]["field"], "timezone",
                             (bad, err))


if __name__ == "__main__":
    unittest.main()
