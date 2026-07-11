"""Season scheduler engine v1 (#84).

Deterministic single round-robin pairings assigned to the earliest available
game ice slots. Produces a draft proposal only — nothing is persisted or
published.
"""

import json
import threading
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from http.cookiejar import CookieJar
from http.server import ThreadingHTTPServer

from helpers import BACKEND  # noqa: F401  (ensures sys.path is set up)

from hockey_scheduler.domain import (
    Division,
    IceSlot,
    IceSlotStatus,
    IceSlotType,
    League,
    Organization,
    Rink,
    Season,
    SeasonTeamRegistration,
    Team,
    Venue,
)
from hockey_scheduler.store import InMemoryStore
from hockey_scheduler.services import draft_schedule, round_robin_pairings
from hockey_scheduler.web import server as srv

UTC = timezone.utc


class RoundRobinTest(unittest.TestCase):
    def test_even_team_count_full_round_robin(self):
        pairs = round_robin_pairings(["a", "b", "c", "d"])
        self.assertEqual(len(pairs), 6)  # C(4,2)
        matchups = {frozenset(p) for p in pairs}
        self.assertEqual(len(matchups), 6)  # each pair exactly once

    def test_odd_team_count_has_byes(self):
        pairs = round_robin_pairings(["a", "b", "c", "d", "e"])
        self.assertEqual(len(pairs), 10)  # C(5,2)
        # Every team plays 4 games (one bye round each).
        counts = {}
        for h, a in pairs:
            counts[h] = counts.get(h, 0) + 1
            counts[a] = counts.get(a, 0) + 1
        self.assertTrue(all(c == 4 for c in counts.values()))

    def test_deterministic_regardless_of_input_order(self):
        self.assertEqual(round_robin_pairings(["d", "c", "b", "a"]),
                         round_robin_pairings(["a", "b", "c", "d"]))

    def test_degenerate_counts(self):
        self.assertEqual(round_robin_pairings([]), [])
        self.assertEqual(round_robin_pairings(["a"]), [])
        self.assertEqual(round_robin_pairings(["a", "b"]), [("a", "b")])


class DraftScheduleTest(unittest.TestCase):
    def _store(self, n_teams=4, n_slots=6):
        s = InMemoryStore()
        s.add_organization(Organization(id="org1", name="Owner"))
        s.add_league(League(id="league1", name="League",
                            organization_id="org1"))
        s.add_season(Season(id="se1", league_id="league1", name="Season"))
        s.add_division(Division(id="div1", season_id="se1", name="D1"))
        s.add_venue(Venue(id="v1", name="Arena", organization_id="org1",
                          league_id="league1"))
        s.add_rink(Rink(id="r1", venue_id="v1", name="Main"))
        for i in range(n_teams):
            s.add_team(Team(id=f"t{i}", name=f"Team {i}", division="D1",
                            division_id="div1"))
            # Draft scheduling reads the season registration, not division_id (#180).
            s.add_season_team_registration(SeasonTeamRegistration(
                id=f"streg_t{i}", season_id="se1", team_id=f"t{i}",
                division_id="div1", active=True))
        base = datetime(2026, 1, 5, 18, tzinfo=UTC)
        for i in range(n_slots):
            s.add_ice_slot(IceSlot(
                id=f"s{i}", rink_id="r1",
                start_time=base + timedelta(days=i),
                end_time=base + timedelta(days=i, hours=1)))
        return s

    def test_assigns_earliest_slots_without_reuse(self):
        s = self._store(4, 6)
        res = draft_schedule(s, "div1")
        self.assertEqual(len(res["draft_games"]), 6)
        self.assertEqual(res["unscheduled"], [])
        used = [d["ice_slot_id"] for d in res["draft_games"]]
        self.assertEqual(len(used), len(set(used)))  # no slot reuse
        self.assertEqual(used, sorted(used))  # earliest-first

    def test_too_few_slots_yields_unscheduled_with_reason(self):
        s = self._store(4, 4)  # 6 pairings, only 4 slots
        res = draft_schedule(s, "div1")
        self.assertEqual(len(res["draft_games"]), 4)
        self.assertEqual(len(res["unscheduled"]), 2)
        self.assertIn("reason", res["unscheduled"][0])

    def test_does_not_persist_or_publish_any_game(self):
        s = self._store(4, 6)
        draft_schedule(s, "div1")
        self.assertEqual(s.all_games(), [])  # nothing created

    def test_skips_non_available_and_non_game_slots(self):
        s = self._store(2, 0)
        base = datetime(2026, 2, 1, 18, tzinfo=UTC)
        s.add_ice_slot(IceSlot(id="prac", rink_id="r1", start_time=base,
                               end_time=base + timedelta(hours=1),
                               slot_type=IceSlotType.PRACTICE))
        s.add_ice_slot(IceSlot(id="blocked", rink_id="r1",
                               start_time=base + timedelta(days=1),
                               end_time=base + timedelta(days=1, hours=1),
                               status=IceSlotStatus.BLOCKED))
        res = draft_schedule(s, "div1")
        # One pairing, but no usable slot → unscheduled.
        self.assertEqual(res["draft_games"], [])
        self.assertEqual(len(res["unscheduled"]), 1)

    def test_deterministic_output(self):
        a = draft_schedule(self._store(4, 6), "div1")
        b = draft_schedule(self._store(4, 6), "div1")
        self.assertEqual(a, b)


class SchedulerHttpTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
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

    def test_non_operator_cannot_draft(self):
        div = srv.STATE.api.store.all_divisions()[0].id
        for who in ("coach", "player", "viewer"):
            c = self._client()
            self._req(c, "POST", "/api/auth/login", {"username": who, "password": "demo"})
            status, _ = self._req(c, "POST", "/api/scheduler/draft",
                                  {"division_id": div})
            self.assertEqual(status, 403, who)

    def test_operator_gets_a_draft_proposal(self):
        div = srv.STATE.api.store.all_divisions()[0].id
        c = self._client()
        self._req(c, "POST", "/api/auth/login", {"username": "admin", "password": "demo"})
        status, body = self._req(c, "POST", "/api/scheduler/draft",
                                 {"division_id": div})
        self.assertEqual(status, 200)
        self.assertIn("draft_games", body)
        self.assertIn("unscheduled", body)

    def test_unknown_division_is_404(self):
        c = self._client()
        self._req(c, "POST", "/api/auth/login", {"username": "admin", "password": "demo"})
        status, body = self._req(c, "POST", "/api/scheduler/draft",
                                 {"division_id": "nope"})
        self.assertEqual(body["error"]["code"], "not_found")


if __name__ == "__main__":
    unittest.main()
