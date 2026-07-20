"""Concurrency regressions for the Player active-state lifecycle (#270 review).

Two races the earlier commits opened and this suite pins closed:

1. Active-state vs the mutating roster/substitute transitions. ``set_player_active``
   row-locks the Player, but ``select_roster`` and the substitute transitions
   read ``is_active`` too — without the SAME row lock, a transaction could read
   ``is_active=True``, a concurrent deactivation could then commit
   ``is_active=False``, and the first could still insert/advance a roster row —
   an inactive Player newly selected/offered after deactivation won. The fixed
   code row-locks the Player in the eligibility read, so the transition either
   goes first (Player still active) or observes the committed deactivation and
   fails closed. These forced PostgreSQL tests hold the deactivation's Player
   lock, let the transition begin, then commit the deactivation FIRST and prove
   the transition cannot commit from a stale active read.

2. Account authority reconcile vs a concurrent rebind / account reactivation.
   ``_retire_player_account_authority`` re-reads each account under its row lock
   before mutating and skips one rebound away, and every account+player path now
   locks Player→Account, so a Player deactivation can't clobber a completed
   rebind, can't leave an active account bound to the inactive Player, and can't
   deadlock account reactivation. Barrier-controlled PostgreSQL races assert the
   deterministic final invariants; Memory/SQLite carry the sequential parity.
"""

import os
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone

from helpers import BACKEND  # noqa: F401  (ensures sys.path is set up)

from hockey_scheduler.api import ApiService
from hockey_scheduler.domain import (
    Division, Game, LeagueSeason, Position, Role, Session, SubstituteStatus,
    Team, UserAccount)
from hockey_scheduler.domain.errors import NotEligibleError
from hockey_scheduler.services.account_service import AccountService
from hockey_scheduler.services.passwords import hash_password
from hockey_scheduler.store import InMemoryStore, SqlStore

UTC = timezone.utc


def _clock():
    return datetime(2026, 1, 1, tzinfo=UTC)


def _seed_chain(store):
    store.add_league_season(LeagueSeason(id="ls", league_id="l", season_id="se"))
    store.add_division(Division(id="d", league_season_id="ls", name="D1"))
    store.add_team(Team(id="home", name="Home", division="D1", division_id="d"))


def _seed_game(store):
    """A published, upcoming game with a single open skater slot on 'home'."""
    store.add_game(Game(
        id="g1", home_team_id="home",
        start_time=datetime(2026, 1, 2, tzinfo=UTC),
        division_id="d", published=True, target_goalies=0, target_skaters=1))


# =====================================================================
# 1. Active-state vs roster/substitute transitions (forced PostgreSQL)
# =====================================================================
@unittest.skipUnless(os.environ.get("TEST_DATABASE_URL"),
                     "PostgreSQL required (set TEST_DATABASE_URL)")
class ActiveStateTransitionRaceTest(unittest.TestCase):
    def setUp(self):
        self.url = os.environ["TEST_DATABASE_URL"]
        SqlStore(self.url).clear_all_data()

    def _deactivation_wins_then(self, prepare, transition):
        """Hold the deactivation's Player row lock, start the transition, then
        commit the deactivation FIRST; return (transition_error, deact_error).

        ``prepare(api)`` seeds any enrollment the transition needs (on a store
        that shares the same database). ``transition(api)`` performs the
        mutating roster/substitute call that must fail closed.
        """
        seed = SqlStore(self.url)
        _seed_chain(seed)
        api_seed = ApiService(seed)
        player = api_seed.setup.add_player("home", "Racer", Position.FORWARD,
                                           actor_id="a")
        _seed_game(seed)
        prepare(api_seed, player.id)

        deact_store = SqlStore(self.url)
        api_deact = ApiService(deact_store)
        api_txn = ApiService(SqlStore(self.url))

        locked = threading.Event()
        go = threading.Event()
        orig_save_player = deact_store.save_player
        state = {"paused": False}

        def paused_save_player(p):
            # Pause INSIDE the deactivation's transaction, after it has row-locked
            # the Player (get_player_for_update) and set is_active=False but
            # BEFORE the write/commit — so it holds the lock while the transition
            # runs, then commits first when released.
            if not state["paused"] and p.id == player.id and not p.is_active:
                state["paused"] = True
                locked.set()
                go.wait(10)
            return orig_save_player(p)

        deact_store.save_player = paused_save_player

        errors = {}

        def deactivate():
            try:
                api_deact.setup.set_player_active(player.id, False, actor_id="a")
            except Exception as exc:                       # pragma: no cover
                errors["deact"] = exc

        def run_transition():
            try:
                transition(api_txn, player.id)
                errors["txn"] = None
            except Exception as exc:
                errors["txn"] = exc

        td = threading.Thread(target=deactivate)
        td.start()
        self.assertTrue(locked.wait(10), "deactivation never reached its lock")
        tt = threading.Thread(target=run_transition)
        tt.start()
        # Give the transition time to reach its is_active read while the
        # deactivation is still paused (uncommitted) — this is the window an
        # unlocked read would sample stale-active in.
        time.sleep(0.5)
        go.set()  # deactivation commits is_active=False FIRST, releases the lock
        td.join(15)
        tt.join(15)
        self.assertIsNone(errors.get("deact"))
        return errors["txn"], player.id

    def test_roster_selection_cannot_commit_from_stale_active_read(self):
        err, pid = self._deactivation_wins_then(
            prepare=lambda api, p: None,
            transition=lambda api, p: api.roster.select_roster(
                "g1", [p], actor_id="a"))
        self.assertIsInstance(err, NotEligibleError)
        check = SqlStore(self.url)
        self.assertFalse(check.get_player(pid).is_active)
        self.assertIsNone(check.roster_entry_for_player("g1", pid),
                          "an inactive Player was selected after deactivation won")

    def test_substitute_offer_cannot_commit_from_stale_active_read(self):
        err, pid = self._deactivation_wins_then(
            prepare=lambda api, p: api.roster.enroll_substitute(
                "g1", p, actor_id="a"),
            transition=lambda api, p: api.roster.offer_substitute(
                "g1", p, actor_id="a"))
        self.assertIsInstance(err, NotEligibleError)
        check = SqlStore(self.url)
        self.assertFalse(check.get_player(pid).is_active)
        # The enrollment stays ENROLLED history — never advanced to OFFERED.
        self.assertEqual(check.substitute_for_player("g1", pid).status,
                         SubstituteStatus.ENROLLED,
                         "an inactive Player was offered after deactivation won")


class ActiveStateTransitionParityTest(unittest.TestCase):
    """Memory + SQLite deterministic parity for the final invariant: once a
    Player is inactive, the mutating roster/substitute transitions fail closed
    and write nothing (the sequential shape of the raced PostgreSQL outcome)."""

    def _backends(self):
        yield "memory", InMemoryStore()
        yield "sqlite", SqlStore(":memory:")

    def test_transitions_fail_closed_after_deactivation(self):
        for label, store in self._backends():
            with self.subTest(backend=label):
                _seed_chain(store)
                api = ApiService(store)
                player = api.setup.add_player("home", "Racer", Position.FORWARD,
                                              actor_id="a")
                _seed_game(store)
                api.roster.enroll_substitute("g1", player.id, actor_id="a")
                api.setup.set_player_active(player.id, False, actor_id="a")

                with self.assertRaises(NotEligibleError, msg=label):
                    api.roster.select_roster("g1", [player.id], actor_id="a")
                self.assertIsNone(
                    store.roster_entry_for_player("g1", player.id), label)
                with self.assertRaises(NotEligibleError, msg=label):
                    api.roster.offer_substitute("g1", player.id, actor_id="a")
                self.assertEqual(
                    store.substitute_for_player("g1", player.id).status,
                    SubstituteStatus.ENROLLED, label)
                if isinstance(store, SqlStore):
                    store.close()


# =====================================================================
# 2. Player deactivation vs account rebind / reactivation (PostgreSQL)
# =====================================================================
@unittest.skipUnless(os.environ.get("TEST_DATABASE_URL"),
                     "PostgreSQL required (set TEST_DATABASE_URL)")
class PlayerDeactivateAccountRaceTest(unittest.TestCase):
    def setUp(self):
        self.url = os.environ["TEST_DATABASE_URL"]
        SqlStore(self.url).clear_all_data()

    def _seed_two_players_and_account(self, seed):
        _seed_chain(seed)
        api = ApiService(seed)
        p1 = api.setup.add_player("home", "P One", Position.FORWARD, actor_id="a")
        p2 = api.setup.add_player("home", "P Two", Position.DEFENSE, actor_id="a")
        acct = api.accounts.create_account(
            "p1login", "scoped-account-pw", Role.PLAYER,
            scope={"player_id": p1.id})
        seed.add_session(Session(
            id="sess1", token_hash="h1", user_id=acct.id,
            issued_at=_clock(), expires_at=_clock() + timedelta(days=1)))
        return p1.id, p2.id, acct.id

    def test_deactivation_racing_rebind_away_loses_no_rebind(self):
        # Player deactivation (which retires the bound account) races a rebind of
        # that account AWAY to another player. Deterministic outcome regardless
        # of who wins the account row: the rebind is never lost, and no ACTIVE
        # account is left bound to the now-inactive Player.
        seed = SqlStore(self.url)
        p1, p2, acct_id = self._seed_two_players_and_account(seed)

        deact = ApiService(SqlStore(self.url))
        rebind = AccountService(SqlStore(self.url), _clock)
        barrier = threading.Barrier(2)
        errs = {}

        def do_deactivate():
            barrier.wait()
            try:
                deact.setup.set_player_active(p1, False, actor_id="a")
            except Exception as exc:                       # pragma: no cover
                errs["deact"] = exc

        def do_rebind():
            barrier.wait()
            try:
                rebind.rebind_account_scope(acct_id, {"player_id": p2})
            except Exception as exc:                       # pragma: no cover
                errs["rebind"] = exc

        t1 = threading.Thread(target=do_deactivate)
        t2 = threading.Thread(target=do_rebind)
        t1.start(); t2.start(); t1.join(15); t2.join(15)
        self.assertEqual(errs, {}, f"unexpected error/deadlock: {errs}")

        check = SqlStore(self.url)
        self.assertFalse(check.get_player(p1).is_active)
        acct = check.get_user_account(acct_id)
        # The rebind is never lost — the account ends bound to p2.
        self.assertEqual((acct.scope or {}).get("player_id"), p2,
                         "the rebind was clobbered by the deactivation")
        # No ACTIVE account may remain bound to the inactive Player p1.
        self.assertFalse(acct.active and (acct.scope or {}).get("player_id") == p1)

    def test_deactivation_racing_account_reactivation_never_strands(self):
        # Player deactivation races an attempt to REACTIVATE the bound account.
        # Deterministic: the Player ends inactive and no active account is left
        # bound to it — either reactivation is refused (deactivation won) or it
        # is undone by the retire (reactivation won then deactivation retired).
        seed = SqlStore(self.url)
        p1, _p2, acct_id = self._seed_two_players_and_account(seed)
        # Start from a DEACTIVATED account so there is something to reactivate.
        AccountService(SqlStore(self.url), _clock).set_active(acct_id, False)

        deact = ApiService(SqlStore(self.url))
        react = AccountService(SqlStore(self.url), _clock)
        barrier = threading.Barrier(2)
        errs = {}

        def do_deactivate():
            barrier.wait()
            try:
                deact.setup.set_player_active(p1, False, actor_id="a")
            except Exception as exc:                       # pragma: no cover
                errs["deact"] = exc

        def do_reactivate():
            barrier.wait()
            try:
                react.set_active(acct_id, True, actor_id="a")
            except Exception as exc:
                # A refusal (player_inactive) or a retryable conflict is fine;
                # a deadlock / raw DB error is NOT (it would surface here).
                errs["react"] = exc

        t1 = threading.Thread(target=do_deactivate)
        t2 = threading.Thread(target=do_reactivate)
        t1.start(); t2.start(); t1.join(15); t2.join(15)
        self.assertIsNone(errs.get("deact"), f"deactivation failed: {errs}")

        check = SqlStore(self.url)
        self.assertFalse(check.get_player(p1).is_active)
        acct = check.get_user_account(acct_id)
        self.assertFalse(acct.active and (acct.scope or {}).get("player_id") == p1,
                         "an active login was left bound to the inactive Player")


class PlayerDeactivateAccountParityTest(unittest.TestCase):
    """Memory + SQLite parity for the final invariants of the account races."""

    def _backends(self):
        yield "memory", InMemoryStore()
        yield "sqlite", SqlStore(":memory:")

    def test_deactivation_leaves_rebound_account_untouched(self):
        for label, store in self._backends():
            with self.subTest(backend=label):
                _seed_chain(store)
                api = ApiService(store)
                p1 = api.setup.add_player("home", "P One", Position.FORWARD,
                                          actor_id="a")
                p2 = api.setup.add_player("home", "P Two", Position.DEFENSE,
                                          actor_id="a")
                acct = api.accounts.create_account(
                    "p1login", "scoped-account-pw", Role.PLAYER,
                    scope={"player_id": p1.id})
                # Rebind the account away to p2 BEFORE deactivating p1.
                api.accounts.rebind_account_scope(acct.id, {"player_id": p2.id})
                api.setup.set_player_active(p1.id, False, actor_id="a")
                # The rebound account is untouched — still active, still on p2.
                final = store.get_user_account(acct.id)
                self.assertTrue(final.active, label)
                self.assertEqual((final.scope or {}).get("player_id"), p2.id, label)
                if isinstance(store, SqlStore):
                    store.close()


if __name__ == "__main__":
    unittest.main()
