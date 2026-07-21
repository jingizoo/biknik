"""#201 slice 1 — forced-PostgreSQL races for the re-fetch-under-lock hardening
of the roster / result / official write paths (handoff from the #297 review).

Each victim is paused at its pre-lock *locator* read of the Game (or the
assignment); the racer commits under the shared Season row lock; the victim then
resumes, re-fetches under the lock, and must act on the FRESH state rather than
saving a stale snapshot. PostgreSQL-only (real row locks); Memory/SQLite hold a
process-wide transaction lock so the interleaving cannot occur there.
"""

import os
import threading
import unittest

from helpers import BACKEND  # noqa: F401  (ensures sys.path is set up)

from hockey_scheduler.api import ApiService
from hockey_scheduler.domain.enums import OfficialRole
from hockey_scheduler.domain.errors import NotFoundError, ValidationError
from hockey_scheduler.store import SqlStore


def _seed_published_game(url):
    """Program → Season → League → Division → two registered Teams → Venue/Rink/
    game slot → one PUBLISHED Game. Returns the ids the races need."""
    api = ApiService(SqlStore(url))
    pid = api.create_program("P", "US", "UTC")["id"]
    sid = api.create_season(pid, "S")["id"]
    lid = api.create_league(sid, "Gold")["id"]
    did = api.create_division(sid, "D1", league_id=lid)["id"]
    club = api.create_club("Club")["id"]
    home = api.create_team(club_id=club, name="Home", league_id=lid)["id"]
    away = api.create_team(club_id=club, name="Away", league_id=lid)["id"]
    api.register_team_for_season(sid, home, division_id=did)
    api.register_team_for_season(sid, away, division_id=did)
    venue = api.create_venue("Arena")["id"]
    api.setup.grant_season_venue_access(sid, venue, actor_id="seed")
    rink = api.create_rink(venue, "Rink 1")["id"]
    slot = api.create_ice_slot(rink, "2031-01-05T18:00:00+00:00",
                               "2031-01-05T20:00:00+00:00",
                               slot_type="game")["id"]
    gid = api.create_game(sid, did, home, away, slot, actor_id="seed")["id"]
    api.setup.publish_game(gid, actor_id="seed")
    return {"sid": sid, "lid": lid, "did": did, "home": home, "away": away,
            "gid": gid}


@unittest.skipUnless(os.environ.get("TEST_DATABASE_URL"),
                     "PostgreSQL required (set TEST_DATABASE_URL)")
class WriteRaceHardeningTest(unittest.TestCase):
    def setUp(self):
        self.url = os.environ["TEST_DATABASE_URL"]
        SqlStore(self.url).clear_all_data()

    def _pause_race(self, victim_store, method_name, victim_fn, racer_fn,
                    pause_on_call=1):
        """Pause victim_store.<method_name> on its Nth call (the pre-lock
        locator), run racer_fn to completion on another connection, then resume
        the victim. Returns the {victim, racer} outcome map."""
        orig = getattr(victim_store, method_name)
        paused = threading.Event()
        resume = threading.Event()
        calls = [0]

        def instrumented(*args, **kwargs):
            result = orig(*args, **kwargs)
            calls[0] += 1
            if calls[0] == pause_on_call:
                paused.set()
                resume.wait(15)
            return result

        setattr(victim_store, method_name, instrumented)
        results = {}

        def run_victim():
            try:
                victim_fn(); results["victim"] = "ok"
            except ValidationError as exc:
                results["victim"] = exc.details.get("reason") or "validation"
            except NotFoundError:
                results["victim"] = "not_found"
            except Exception as exc:
                results["victim"] = type(exc).__name__

        def run_racer():
            if not paused.wait(15):
                results["racer"] = "ERR:victim-never-paused"
                return
            try:
                racer_fn(); results["racer"] = "ok"
            except Exception as exc:
                results["racer"] = f"ERR:{exc}"
            finally:
                resume.set()

        tv = threading.Thread(target=run_victim)
        trc = threading.Thread(target=run_racer)
        tv.start(); trc.start()
        trc.join(25); resume.set(); tv.join(25)
        return results

    # -- cancel vs lock_roster (RosterService) -----------------------------
    def test_cancel_vs_lock_roster_no_resurrection(self):
        ids = _seed_published_game(self.url)
        gid = ids["gid"]
        victim_store = SqlStore(self.url)
        api_victim = ApiService(victim_store)
        api_racer = ApiService(SqlStore(self.url))
        results = self._pause_race(
            victim_store, "get_game",
            victim_fn=lambda: api_victim.roster.lock_roster(gid, actor_id="lock"),
            racer_fn=lambda: api_racer.roster.cancel_game(gid, actor_id="cancel"))
        self.assertEqual(results.get("racer"), "ok", results)
        check = SqlStore(self.url)
        game = check.get_game(gid)
        self.assertTrue(game.cancelled, results)     # cancellation stands
        self.assertFalse(game.locked, results)       # never resurrected as locked

    # -- cancel vs record_result (SetupService) ----------------------------
    def test_cancel_vs_record_result_blocks(self):
        ids = _seed_published_game(self.url)
        gid = ids["gid"]
        victim_store = SqlStore(self.url)
        api_victim = ApiService(victim_store)
        api_racer = ApiService(SqlStore(self.url))
        results = self._pause_race(
            victim_store, "get_game",
            victim_fn=lambda: api_victim.setup.record_result(
                gid, 3, 2, actor_id="rec"),
            racer_fn=lambda: api_racer.roster.cancel_game(gid, actor_id="cancel"))
        self.assertEqual(results.get("racer"), "ok", results)
        self.assertEqual(results.get("victim"), "validation", results)
        check = SqlStore(self.url)
        self.assertTrue(check.get_game(gid).cancelled, results)
        self.assertIsNone(check.result_for_game(gid), results)  # no result written

    # -- cancel vs approve_result (SetupService) ---------------------------
    def test_cancel_vs_approve_result_blocks(self):
        ids = _seed_published_game(self.url)
        gid = ids["gid"]
        ApiService(SqlStore(self.url)).setup.record_result(
            gid, 3, 2, actor_id="rec")            # a DRAFT result exists
        victim_store = SqlStore(self.url)
        api_victim = ApiService(victim_store)
        api_racer = ApiService(SqlStore(self.url))
        results = self._pause_race(
            victim_store, "get_game",
            victim_fn=lambda: api_victim.setup.approve_result(
                gid, actor_id="app"),
            racer_fn=lambda: api_racer.roster.cancel_game(gid, actor_id="cancel"))
        self.assertEqual(results.get("racer"), "ok", results)
        self.assertEqual(results.get("victim"), "validation", results)
        check = SqlStore(self.url)
        self.assertTrue(check.get_game(gid).cancelled, results)
        self.assertEqual(check.result_for_game(gid).status.value, "draft",
                         results)                   # never finalized

    # -- unassign vs unassign (SetupService) -------------------------------
    def test_unassign_vs_unassign_single_effect(self):
        ids = _seed_published_game(self.url)
        gid = ids["gid"]
        seed = ApiService(SqlStore(self.url))
        official = seed.setup.create_official("Ref One", actor_id="s")
        assignment = seed.setup.assign_official(
            gid, official.id, OfficialRole.REFEREE, actor_id="s")
        aid = assignment.id
        victim_store = SqlStore(self.url)
        api_victim = ApiService(victim_store)
        api_racer = ApiService(SqlStore(self.url))
        results = self._pause_race(
            victim_store, "get_official_assignment",
            victim_fn=lambda: api_victim.setup.unassign_official(
                aid, actor_id="v"),
            racer_fn=lambda: api_racer.setup.unassign_official(aid, actor_id="r"))
        self.assertEqual(results.get("racer"), "ok", results)       # one removes
        self.assertEqual(results.get("victim"), "not_found", results)  # loser
        check = SqlStore(self.url)
        self.assertIsNone(check.get_official_assignment(aid), results)
        audits = [a for a in check.all_setup_audit()
                  if a.action == "official_unassigned"
                  and a.entity_id == aid]
        self.assertEqual(len(audits), 1, results)   # exactly one, no duplicate
