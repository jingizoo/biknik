"""#201 slice 1 — forced-PostgreSQL races for the re-fetch-under-lock hardening
of the roster / result / official write paths (handoff from the #297 review).

Each victim is paused at its pre-lock *locator* read of the Game (or the
assignment); the racer commits under the shared Season row lock; the victim then
resumes, re-fetches under the lock, and must act on the FRESH state rather than
saving a stale snapshot. PostgreSQL-only (real row locks); Memory/SQLite hold a
process-wide transaction lock so the interleaving cannot occur there.

PR #423 NOTE (``test_unassign_vs_unassign_single_effect`` only): that one race
now resolves through the NEW database-coordinated epoch fence's global
exclusive lock (``unassign_official``'s new, literal-first
``epoch_fence_acquire_exclusive`` call) rather than through the Season row
lock described above -- the fence is acquired BEFORE this file's pause point,
so it, not the row lock, is what the racer now blocks on. See that test's own
docstring for the full account of what changed and why the safety property
(exactly one effect, no corruption, a well-typed error for the loser) still
holds. The other three races in this file are unaffected -- none of their
writers (``cancel_game``, ``lock_roster``, ``record_result``,
``approve_result``) are epoch-material.
"""

import os
import threading
import unittest

from helpers import BACKEND  # noqa: F401  (ensures sys.path is set up)

from hockey_scheduler.api import ApiService
from hockey_scheduler.domain.enums import OfficialRole
from hockey_scheduler.domain.errors import (
    ConcurrencyConflictError, NotFoundError, ValidationError)
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
                # PR #423: stash the raw exception TOO (not just its stringified
                # form above, kept unchanged for the other races in this file),
                # so a caller that needs to assert on its TYPE/``details`` (a
                # global-fence writer-vs-writer conflict, see
                # test_unassign_vs_unassign_single_effect) can do so robustly
                # rather than pattern-matching free text.
                results["racer_exc"] = exc
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
        """PR #423 UPDATE (read this before touching the assertions below):

        Before PR #423, this race was resolved ENTIRELY by the #201
        Season-row-lock re-fetch this file's module docstring describes: the
        racer (unpaused) ran to completion first; the victim (paused at its
        UNLOCKED locator read, before taking any lock) resumed, re-fetched
        under the lock, found nothing, and cleanly raised NotFoundError. The
        original assertions here (racer="ok", victim="not_found") pinned
        exactly that resolution.

        PR #423 gives ``unassign_official`` a NEW, EARLIER synchronization
        point: ``epoch_fence_acquire_exclusive(EPOCH_FENCE_GLOBAL_KEY)`` is
        now the method's literal FIRST statement (design §8.5, the owner's
        explicit "at the top of each method's transactional body"
        requirement) — BEFORE the locator read this test's ``_pause_race``
        pauses on. So by the time the victim is paused, it already holds the
        GLOBAL exclusive fence for the remainder of its transaction. The
        racer's OWN first statement is the SAME acquisition on the SAME key,
        which now genuinely BLOCKS (a real ``pg_advisory_xact_lock`` held on
        the victim's still-open connection) rather than proceeding
        uncontended — for up to the fence's bound (``HS_CONTEXT_GATE_TIMEOUT``,
        default 10s), then fails CLOSED with a retryable
        ``ConcurrencyConflictError`` (design §4.5's deliberate writer-side
        choice — a writer must never silently proceed past a fence it could
        not confirm, unlike the reader's own fail-OPEN choice). The victim,
        once resumed, still re-fetches under lock and finds the row exactly
        as it left it (the racer never reached it), so the victim — not the
        racer — is now the one that succeeds.

        This is a DELIBERATE, newly-introduced consequence of widening the
        pre-existing global lifecycle lock (previously Season archive/reopen
        ONLY) to cover all 14 "global-class" epoch-material writers,
        including this one (§4.1/§4.2/§11.4 of the PR's design): two
        `unassign_official` calls now ALSO serialize against EACH OTHER
        (exclusive-vs-exclusive on the same key), not merely against a
        concurrent scoped READ (the fence's primary purpose) — a real,
        reported tradeoff of the coarser, widened key, not an oversight
        being papered over here. THE SAFETY PROPERTY THIS TEST EXISTS TO
        PROVE IS UNCHANGED AND STILL ASSERTED BELOW, exactly as strictly as
        before: exactly one of the two racing calls succeeds, the loser gets
        a well-typed, non-corrupting, explicitly-retryable error rather than
        a wrong answer or a silent double-effect, exactly one row is removed,
        and exactly one audit row is written — only WHICH side wins and WHAT
        the loser's error looks like have changed, and both are pinned
        precisely (not loosely) below.
        """
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
        # The victim already held the exclusive global fence (acquired before
        # its paused locator read) for its whole pause, so it is the one that
        # completes the removal; the racer's OWN fence acquisition blocked on
        # that same held lock and timed out.
        self.assertEqual(results.get("victim"), "ok", results)       # winner
        racer_exc = results.get("racer_exc")
        self.assertIsInstance(
            racer_exc, ConcurrencyConflictError,
            f"the racer's loss must be a well-typed, retryable conflict, "
            f"not a silent success or an unclassified error: {results}")
        self.assertEqual(racer_exc.details.get("reason"), "lock_not_available",
                         results)
        self.assertTrue(racer_exc.details.get("retryable"), results)
        check = SqlStore(self.url)
        self.assertIsNone(check.get_official_assignment(aid), results)
        audits = [a for a in check.all_setup_audit()
                  if a.action == "official_unassigned"
                  and a.entity_id == aid]
        self.assertEqual(len(audits), 1, results)   # exactly one, no duplicate
