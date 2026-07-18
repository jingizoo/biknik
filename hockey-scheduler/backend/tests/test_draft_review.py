"""Draft schedule review + publish (#86).

Committing a generated draft persists it as draft games (is_draft=True,
unpublished) that stay out of the public schedule until published. Publishing
makes them public; discarding deletes them. Commit/publish/discard are
operator-only.
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

from hockey_scheduler.api import ApiService
from hockey_scheduler.domain import (
    Division, Game, IceSlot, IceSlotStatus, League, LeagueSeason, Official,
    Organization, Program, Rink, Season, SeasonTeamRegistration,
    SeasonVenueAccess, Team, Venue)
from hockey_scheduler.store import InMemoryStore
from hockey_scheduler.web import server as srv

UTC = timezone.utc


def _seeded_api():
    s = InMemoryStore()
    s.add_organization(Organization(id="org", name="Owner"))
    s.add_program(Program(id="league", name="League", operator_organization_id="org"))
    s.add_season(Season(id="se", program_id="league", name="Season"))
    s.add_league(League(id="lg", program_id="league", name="Div League"))
    s.add_league_season(LeagueSeason(id="ls", league_id="lg", season_id="se"))
    s.add_division(Division(id="d", league_season_id="ls", name="D1"))
    s.add_venue(Venue(id="v", name="Arena", organization_id="org",
                      league_id="league"))
    s.add_season_venue_access(SeasonVenueAccess(
        id="sva1", season_id="se", venue_id="v", active=True))
    s.add_rink(Rink(id="r1", venue_id="v", name="Main"))
    for i in range(4):
        s.add_team(Team(id=f"t{i}", name=f"T{i}", division="D1", division_id="d",
                        program_id="league", league_id="lg"))
        # Participation is per-season (#180): draft scheduling reads the
        # registration, so register each seeded team into the season+division.
        s.add_season_team_registration(SeasonTeamRegistration(
            id=f"streg_t{i}", league_season_id="ls", team_id=f"t{i}",
            division_id="d", active=True))
    base = datetime(2026, 1, 5, 18, tzinfo=UTC)
    for i in range(6):
        s.add_ice_slot(IceSlot(id=f"s{i}", rink_id="r1",
                               start_time=base + timedelta(days=i),
                               end_time=base + timedelta(days=i, hours=1)))

    # #283 Slice E: a regular Game must reference the exact LeagueSeason it
    # belongs to, and publish now fails closed on any regular game missing it.
    # Games are persisted here with only (division_id, league_id, season_id), so
    # backfill league_season_id from the game's Division — the same derivation
    # migration 037 applies to pre-#283 rows — as each Game is stored, keeping
    # this fixture focused on the draft commit/publish/discard workflow.
    _add_game = s.add_game

    def _add_game_with_league_season(game):
        if getattr(game, "league_season_id", None) is None and game.division_id:
            division = s.get_division(game.division_id)
            if division is not None:
                game.league_season_id = division.league_season_id
        return _add_game(game)

    s.add_game = _add_game_with_league_season
    return ApiService(s), s


class DraftReviewServiceTest(unittest.TestCase):
    def setUp(self):
        self.api, self.store = _seeded_api()

    def test_commit_persists_drafts_not_public(self):
        res = self.api.commit_draft_schedule("d")
        self.assertEqual(len(res["created"]), 6)
        # Persisted as draft games...
        self.assertEqual(len(self.api.list_draft_games()["draft_games"]), 6)
        self.assertTrue(all(g.is_draft and not g.published
                            for g in self.store.all_games()))
        # ...and invisible to the public schedule.
        self.assertEqual(self.api.get_public_schedule()["fixtures"], [])

    def test_publish_makes_drafts_public(self):
        self.api.commit_draft_schedule("d")
        res = self.api.publish_draft_games(all_drafts=True)
        self.assertEqual(res["published"], 6)
        self.assertEqual(self.api.list_draft_games()["draft_games"], [])
        self.assertEqual(len(self.api.get_public_schedule()["fixtures"]), 6)
        self.assertTrue(all(g.published and not g.is_draft
                            for g in self.store.all_games()))

    def test_discard_removes_drafts(self):
        self.api.commit_draft_schedule("d")
        res = self.api.discard_draft_games(all_drafts=True)
        self.assertEqual(res["discarded"], 6)
        self.assertEqual(self.store.all_games(), [])

    def test_publish_selected_only(self):
        self.api.commit_draft_schedule("d")
        drafts = self.api.list_draft_games()["draft_games"]
        one = drafts[0]["game_id"]
        self.api.publish_draft_games(game_ids=[one])
        self.assertEqual(len(self.api.get_public_schedule()["fixtures"]), 1)
        self.assertEqual(len(self.api.list_draft_games()["draft_games"]), 5)

    def test_discard_never_touches_published_games(self):
        self.api.commit_draft_schedule("d")
        self.api.publish_draft_games(all_drafts=True)  # all now real
        self.api.discard_draft_games(all_drafts=True)  # nothing is a draft
        self.assertEqual(len(self.store.all_games()), 6)

    def test_committed_drafts_stay_out_of_operator_overview(self):
        # The leak: draft games must not appear in the operator schedule or
        # claim a slot in the grid until they are published (#86).
        self.api.commit_draft_schedule("d")
        ov = self.api.get_demo_overview()
        self.assertEqual(ov["schedule"], [])
        self.assertTrue(all(s["game_id"] is None for s in ov["ice_slots"]))
        # After publishing they DO appear in the operator schedule.
        self.api.publish_draft_games(all_drafts=True)
        ov2 = self.api.get_demo_overview()
        self.assertEqual(len(ov2["schedule"]), 6)

    def test_commit_and_discard_are_audited(self):
        self.api.commit_draft_schedule("d", actor_id="user_admin")
        commit = [a for a in self.store.all_setup_audit()
                  if a.action == "draft_schedule_committed"]
        self.assertEqual(len(commit), 1)
        self.assertEqual(commit[0].actor_id, "user_admin")
        self.assertEqual(commit[0].detail["created_count"], 6)
        self.api.discard_draft_games(all_drafts=True, actor_id="user_admin")
        discarded = [a for a in self.store.all_setup_audit()
                     if a.action == "draft_game_discarded"]
        self.assertEqual(len(discarded), 6)

    def test_publish_uses_the_audited_single_game_publish_path(self):
        self.api.commit_draft_schedule("d", actor_id="user_admin")
        before = sum(1 for a in self.store.all_setup_audit()
                     if a.action == "game_published")
        self.api.publish_draft_games(all_drafts=True, actor_id="user_admin")
        published = [a for a in self.store.all_setup_audit()
                     if a.action == "game_published"]
        # One game_published audit per published draft — the same path as
        # single-game publish, not a silent direct save that bypasses the trail.
        self.assertEqual(len(published) - before, 6)
        self.assertTrue(all(a.actor_id == "user_admin" for a in published))

    # -- slot allocation invariant (#86) -----------------------------------
    def _slot_ids_of_drafts(self):
        return [g.ice_slot_id for g in self.store.all_games() if g.is_draft]

    def test_commit_leaves_slots_available(self):
        self.api.commit_draft_schedule("d")
        # A draft must not claim its slot — the grid still shows it available.
        self.assertTrue(all(s.status == IceSlotStatus.AVAILABLE
                            for s in self.store.all_ice_slots()))

    def test_publish_allocates_the_slot(self):
        self.api.commit_draft_schedule("d")
        slot_ids = self._slot_ids_of_drafts()
        self.assertTrue(slot_ids)
        self.api.publish_draft_games(all_drafts=True)
        for sid in slot_ids:
            self.assertEqual(self.store.get_ice_slot(sid).status,
                             IceSlotStatus.ALLOCATED)

    def test_discard_leaves_slots_available(self):
        self.api.commit_draft_schedule("d")
        self.api.discard_draft_games(all_drafts=True)
        self.assertTrue(all(s.status == IceSlotStatus.AVAILABLE
                            for s in self.store.all_ice_slots()))

    def test_published_draft_matches_manual_game_in_slot_grid(self):
        # Regression: after publish, the operator slot grid shows the game AND
        # marks the slot allocated — exactly like a manually-created game, so
        # the calendar no longer treats an occupied slot as an open drop target.
        res = self.api.commit_draft_schedule("d")
        self.api.publish_draft_games(all_drafts=True)
        ov = self.api.get_demo_overview()
        occupied = [s for s in ov["ice_slots"] if s["game_id"]]
        self.assertEqual(len(occupied), len(res["created"]))
        self.assertTrue(all(s["status"] == "allocated" for s in occupied))


class SchedulerReviewIssuesTest(unittest.TestCase):
    """Scheduler review polish (#106): summary counts + per-game issues on
    top of the same commit/publish/discard workflow #86 already covers."""

    def setUp(self):
        self.api, self.store = _seeded_api()

    def test_summary_counts_and_default_issues(self):
        self.api.commit_draft_schedule("d")
        res = self.api.list_draft_games()
        summary = res["summary"]
        self.assertEqual(summary["draft_count"], 6)
        self.assertEqual(summary["published_count"], 0)
        self.assertEqual(summary["by_division"], {"D1": 6})
        self.assertEqual(summary["by_rink"], {"Main": 6})
        # No officials assigned and no roster set on any of the seeded teams
        # — every draft game starts out flagged for both.
        self.assertEqual(summary["issue_count"], 6)
        for row in res["draft_games"]:
            self.assertEqual(set(row["issues"]),
                             {"missing_officials", "roster_not_ready"})

    def test_officials_pending_until_accepted(self):
        self.store.add_official(Official(id="o1", name="Ref One"))
        self.api.commit_draft_schedule("d")
        gid = self.api.list_draft_games()["draft_games"][0]["game_id"]
        assignment = self.api.assign_official(gid, "o1", "referee")
        row = next(r for r in self.api.list_draft_games()["draft_games"]
                   if r["game_id"] == gid)
        self.assertEqual(row["officials_assigned"], 1)
        self.assertEqual(row["officials_accepted"], 0)
        self.assertNotIn("missing_officials", row["issues"])
        self.assertIn("officials_pending", row["issues"])
        self.api.respond_assignment(assignment["id"], True)
        row = next(r for r in self.api.list_draft_games()["draft_games"]
                   if r["game_id"] == gid)
        self.assertEqual(row["officials_accepted"], 1)
        self.assertNotIn("officials_pending", row["issues"])
        self.assertNotIn("missing_officials", row["issues"])
        # Roster is still unset — that issue is independent of officials.
        self.assertIn("roster_not_ready", row["issues"])

    def test_published_count_reflects_publish(self):
        self.api.commit_draft_schedule("d")
        drafts = self.api.list_draft_games()["draft_games"]
        self.api.publish_draft_games(game_ids=[drafts[0]["game_id"]])
        summary = self.api.list_draft_games()["summary"]
        self.assertEqual(summary["draft_count"], 5)
        self.assertEqual(summary["published_count"], 1)

    def test_slot_conflict_flagged_defensively(self):
        # The generator itself never proposes an already-used slot; this
        # simulates data mutated out of band so the review screen still
        # surfaces it rather than silently publishing a clash.
        self.api.commit_draft_schedule("d")
        drafts = self.api.list_draft_games()["draft_games"]
        clashing_slot = drafts[0]["game_id"]
        slot_id = next(g.ice_slot_id for g in self.store.all_games()
                       if g.id == clashing_slot)
        self.store.add_game(Game(
            id="game_extra", home_team_id="t0", away_team_id="t1",
            start_time=datetime(2030, 1, 1, tzinfo=UTC),
            end_time=datetime(2030, 1, 1, 1, tzinfo=UTC),
            ice_slot_id=slot_id, division_id="d"))
        row = next(r for r in self.api.list_draft_games()["draft_games"]
                   if r["game_id"] == clashing_slot)
        self.assertIn("slot_conflict", row["issues"])

    def test_team_double_booking_flagged_defensively(self):
        self.api.commit_draft_schedule("d")
        drafts = self.api.list_draft_games()["draft_games"]
        target = drafts[0]
        overlap_start = datetime.fromisoformat(target["start_time"])
        overlap_end = datetime.fromisoformat(target["end_time"])
        self.store.add_game(Game(
            id="game_extra", home_team_id=target["home_team_id"],
            away_team_id="t3" if target["home_team_id"] != "t3" else "t2",
            start_time=overlap_start, end_time=overlap_end,
            division_id="d"))
        row = next(r for r in self.api.list_draft_games()["draft_games"]
                   if r["game_id"] == target["game_id"])
        self.assertIn("team_double_booked", row["issues"])


class DraftReviewHttpTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        srv.STATE.reset()
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), srv.Handler)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()
        cls.div = srv.STATE.api.store.all_divisions()[0].id

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

    def test_only_operator_can_commit_publish_discard(self):
        for who in ("coach", "player", "viewer"):
            c = self._client()
            self._req(c, "POST", "/api/auth/login", {"username": who, "password": "demo"})
            for path in ("/api/scheduler/commit", "/api/scheduler/drafts/publish",
                         "/api/scheduler/drafts/discard"):
                status, _ = self._req(c, "POST", path, {"division_id": self.div, "all": True})
                self.assertEqual(status, 403, f"{who} {path}")
            status, _ = self._req(c, "GET", "/api/scheduler/drafts")
            self.assertEqual(status, 403, who)

    def test_operator_commit_and_discard_roundtrip(self):
        c = self._client()
        self._req(c, "POST", "/api/auth/login", {"username": "admin", "password": "demo"})
        status, body = self._req(c, "POST", "/api/scheduler/commit",
                                 {"division_id": self.div})
        self.assertEqual(status, 200)
        _, listed = self._req(c, "GET", "/api/scheduler/drafts")
        self.assertEqual(len(listed["draft_games"]), len(body["created"]))
        status, disc = self._req(c, "POST", "/api/scheduler/drafts/discard", {"all": True})
        self.assertEqual(disc["discarded"], len(body["created"]))

    def test_publish_all_drafts_atomic_on_one_invalid_target(self):
        # #283 review: batch publish is all-or-nothing over HTTP. Commit a
        # division's drafts, break ONE (null league_season_id), then publish-all
        # must be rejected with NO partial publish — every draft stays a draft on
        # an AVAILABLE slot and no game_published audit is written.
        c = self._client()
        self._req(c, "POST", "/api/auth/login",
                  {"username": "admin", "password": "demo"})
        status, _ = self._req(c, "POST", "/api/scheduler/commit",
                              {"division_id": self.div})
        self.assertEqual(status, 200)
        _, listed = self._req(c, "GET", "/api/scheduler/drafts")
        draft_ids = [r["game_id"] for r in listed["draft_games"]]
        self.assertGreaterEqual(len(draft_ids), 2, listed)

        store = srv.STATE.api.store
        bad = store.get_game(draft_ids[0])
        bad.league_season_id = None  # break exactly one target
        store.save_game(bad)
        slot_ids = [store.get_game(i).ice_slot_id for i in draft_ids
                    if store.get_game(i).ice_slot_id]
        audit_before = len(store.all_setup_audit())

        status, resp = self._req(c, "POST", "/api/scheduler/drafts/publish",
                                 {"all": True})
        self.assertNotEqual(status, 200, resp)   # rejected, no partial success

        for i in draft_ids:
            g = store.get_game(i)
            self.assertTrue(g.is_draft, i)         # still a draft
            self.assertFalse(g.published, i)       # still unpublished
        for sid in slot_ids:
            self.assertEqual(store.get_ice_slot(sid).status,
                             IceSlotStatus.AVAILABLE)
        self.assertEqual(len(store.all_setup_audit()), audit_before)


if __name__ == "__main__":
    unittest.main()
