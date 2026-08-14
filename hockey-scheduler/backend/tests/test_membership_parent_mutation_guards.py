"""SeasonRosterMembership parent-mutation stranding guards (#205 review
round 1 finding 2).

Before this module's fixes, every one of these already-shipped Slice A
mutations could strand a live membership:

  * ``unregister_team_from_season`` deactivated a Team's registration while
    a LIVE (non-terminal) membership still named that exact
    (LeagueSeason, Team) pair — the pair ``create_season_roster_membership``
    itself REQUIRES be actively registered to create a NEW membership, so an
    EXISTING one silently outliving that requirement is the same gap.
  * ``delete_season_team_registration`` / ``delete_team`` / ``delete_league``
    / ``delete_league_season`` / ``delete_season`` / ``delete_player``
    permanently removed a row a membership's REQUIRED (non-nullable) foreign
    key still named — an orphaned pointer on InMemoryStore, and on SQLite/
    PostgreSQL either a generic, untranslated FK-conflict (no dependents
    block at all) or (for Season/League/LeagueSeason, whose FK the
    membership store write never round-trips through) nothing at all.
  * ``transfer_team_to_league`` moved/superseded a Team's registration onto a
    NEW LeagueSeason while a live membership kept naming the OLD one — a
    silent Team<->LeagueSeason League disagreement.
  * ``set_season_roster_membership_status`` reactivating a PARKED membership
    (inactive/injured -> active) re-checked only the uniqueness rules, never
    whether its Team/LeagueSeason/registration spine still holds.

Every fix below is a BLOCK (``has_dependencies`` / ``validation_error``),
never a silent cascade, auto-release or auto-migration — an atomic lifecycle
rewrite is explicitly out of THIS slice's bounded scope (see the PR body).

Zero-write/audit proof: every "blocked" test re-reads the parent row (and,
where relevant, the audit trail) after the refusal and asserts it is
UNCHANGED — a caught exception alone does not prove nothing was written.
"""

import os
import threading
import unittest

from helpers import BACKEND  # noqa: F401  (ensures sys.path is set up)
from helpers import end_membership_directly as _end_membership_directly

from hockey_scheduler.api import ApiService
from hockey_scheduler.domain import MembershipStatus, Position, SeasonRosterMembership
from hockey_scheduler.domain.errors import HasDependenciesError, ValidationError
from hockey_scheduler.store import InMemoryStore, SqlStore

ADMIN = "setup_admin"

# Every non-terminal status the invariant must protect equally.
_LIVE_STATUSES = ("applicant", "active", "affiliate", "inactive", "injured")


def _fixture(api):
    """Program -> Season -> Division -> Club -> registered Team (+ a second
    League/Team for transfer targets), plus one Player. Returns a dict of
    ids/objects so callers can pick what they need by name."""
    program = api.create_program("Prog", actor_id=ADMIN)
    season = api.create_season(program["id"], "Season", actor_id=ADMIN)
    division = api.create_division(season["id"], "Div A", actor_id=ADMIN)
    club = api.create_club("Club X", actor_id=ADMIN)
    team = api.create_team(club["id"], division["id"], "Lions", actor_id=ADMIN)
    reg = api.register_team_for_season(season["id"], team["id"], division["id"],
                                       actor_id=ADMIN)
    ls_id = api.store.get_season_team_registration(reg["id"]).league_season_id
    league_id = api.store.get_league_season(ls_id).league_id
    player = api.create_player(team["id"], "Skater", "forward",
                               jersey_number=9, actor_id=ADMIN)
    other_league = api.create_league(season["id"], "Bronze", actor_id=ADMIN)
    return {"program": program, "season": season, "division": division,
           "club": club, "team": team, "reg": reg, "ls_id": ls_id,
           "league_id": league_id, "player": player,
           "other_league_id": other_league["id"]}


def _each_store():
    yield "memory", InMemoryStore()
    yield "sqlite", SqlStore(":memory:")


def _make_membership(api, fx, status="applicant"):
    return api.create_season_roster_membership(
        fx["player"]["id"], fx["ls_id"], fx["team"]["id"], status=status,
        jersey_number=None, actor_id=ADMIN)


# _end_membership_directly (imported above as helpers.end_membership_directly)
# — #205 review round 2 (owner product ruling): set_season_roster_membership_
# status now hard-refuses EVERY terminal transition, unconditionally — no
# caller, actor_id or reason can reach one through it any more. Several tests
# in this module need an ALREADY-terminal membership only as a PRECONDITION
# for something ELSE they exercise (a terminal membership does not block
# unregister/transfer, unlike a live one) — not to test the transition
# method's own authorization, which is exactly what the owner ruling says
# must be reconstructed this way rather than weakened back open. Mirrors this
# file's own pre-existing convention for reaching an out-of-band state
# (ReactivationSpineTest's direct api.store.delete_team/save_team calls,
# below).


class UnregisterStrandingTest(unittest.TestCase):
    """``unregister_team_from_season`` must refuse while a LIVE membership
    still names this exact (LeagueSeason, Team) — zero write, zero audit."""

    def test_live_membership_blocks_unregister_zero_write(self):
        for label, store in _each_store():
            api = ApiService(store)
            fx = _fixture(api)
            for status in _LIVE_STATUSES:
                with self.subTest(backend=label, status=status):
                    m = _make_membership(api, fx, status)
                    self.assertNotIn("error", m, (label, status, m))
                    before_audit = len(api.store.all_setup_audit())
                    res = api.unregister_team_from_season(
                        fx["reg"]["id"], actor_id=ADMIN)
                    self.assertEqual(res["error"]["details"]["reason"],
                                     "team_has_live_memberships",
                                     (label, status, res))
                    self.assertIn(
                        m["id"],
                        res["error"]["details"]["affected_membership_ids"],
                        (label, status))
                    # Zero write: the registration is still active.
                    reg = api.store.get_season_team_registration(
                        fx["reg"]["id"])
                    self.assertTrue(reg.active, (label, status))
                    # Zero audit: no new setup_audit_logs row.
                    self.assertEqual(len(api.store.all_setup_audit()),
                                     before_audit, (label, status))
                    # Clean up for the next status in this loop. Direct
                    # store write (#205 review round 2 owner ruling):
                    # set_season_roster_membership_status now hard-refuses
                    # every terminal transition, and this is a precondition
                    # for the next loop iteration, not the thing under test.
                    _end_membership_directly(api.store, m["id"], "released")

    def test_terminal_membership_does_not_block_unregister(self):
        for label, store in _each_store():
            with self.subTest(backend=label):
                api = ApiService(store)
                fx = _fixture(api)
                m = _make_membership(api, fx, "applicant")
                # Direct store write, not the now-unconditionally-refused
                # set_season_roster_membership_status (#205 review round 2
                # owner ruling) — this test's subject is unregister's
                # behavior against an ALREADY-terminal membership, not the
                # transition method's own (now removed) authorization.
                _end_membership_directly(api.store, m["id"], "released")
                res = api.unregister_team_from_season(
                    fx["reg"]["id"], actor_id=ADMIN)
                self.assertNotIn("error", res, (label, res))
                self.assertFalse(res["active"], label)


class RegistrationDeleteStrandingTest(unittest.TestCase):
    """``delete_season_team_registration`` must refuse while ANY membership
    (even released/transferred history) still names this row — it is a
    REQUIRED foreign key, the same shape games/other registrations already
    block on regardless of status."""

    def test_any_membership_blocks_permanent_delete_zero_write(self):
        # A permanent delete needs an INACTIVE registration (its own
        # pre-existing #251 guard), which needs unregister — which finding
        # 2's OWN fix now refuses while a LIVE membership exists (tested in
        # UnregisterStrandingTest). So the only reachable path here is: end
        # the membership (released, terminal history) FIRST, unregister,
        # THEN prove that terminal-history row still blocks the PERMANENT
        # delete — the "required FK, any status" half of finding 2.
        for label, store in _each_store():
            with self.subTest(backend=label):
                api = ApiService(store)
                fx = _fixture(api)
                m = _make_membership(api, fx, "applicant")
                self.assertNotIn("error", m, (label, m))
                # Direct store write, not the now-unconditionally-refused
                # set_season_roster_membership_status (#205 review round 2
                # owner ruling) — this test's subject is the PERMANENT
                # delete's "any status, even terminal history" FK block,
                # not the transition method's own (now removed)
                # authorization.
                released = _end_membership_directly(
                    api.store, m["id"], "released")
                self.assertIs(released.status, MembershipStatus.RELEASED,
                              (label, released))
                unreg = api.unregister_team_from_season(
                    fx["reg"]["id"], actor_id=ADMIN)
                self.assertNotIn("error", unreg, (label, unreg))
                before_audit = len(api.store.all_setup_audit())
                res = api.delete_season_team_registration(
                    fx["reg"]["id"], actor_id=ADMIN)
                self.assertEqual(res["error"]["code"], "has_dependencies",
                                 (label, res))
                groups = res["error"]["details"]["dependencies"]
                membership_group = next(
                    g for g in groups if g["type"] == "roster membership")
                self.assertEqual(membership_group["count"], 1,
                                 (label, groups))
                # Zero write: the registration row is still there.
                self.assertIsNotNone(
                    api.store.get_season_team_registration(fx["reg"]["id"]),
                    label)
                self.assertEqual(len(api.store.all_setup_audit()),
                                 before_audit, label)


class LeagueSeasonDeleteStrandingTest(unittest.TestCase):
    def test_any_membership_blocks_delete_zero_write(self):
        for label, store in _each_store():
            with self.subTest(backend=label):
                api = ApiService(store)
                fx = _fixture(api)
                m = _make_membership(api, fx, "applicant")
                res = api.delete_league_season(fx["ls_id"], actor_id=ADMIN)
                self.assertEqual(res["error"]["code"], "has_dependencies",
                                 (label, res))
                groups = res["error"]["details"]["dependencies"]
                self.assertTrue(
                    any(g["type"] == "roster membership" and g["count"] == 1
                       for g in groups), (label, groups))
                self.assertIsNotNone(
                    api.store.get_league_season(fx["ls_id"]), label)


class LeagueDeleteStrandingTest(unittest.TestCase):
    def test_any_membership_blocks_delete_zero_write(self):
        for label, store in _each_store():
            with self.subTest(backend=label):
                api = ApiService(store)
                fx = _fixture(api)
                m = _make_membership(api, fx, "applicant")
                res = api.delete_league(fx["league_id"], actor_id=ADMIN)
                self.assertEqual(res["error"]["code"], "has_dependencies",
                                 (label, res))
                groups = res["error"]["details"]["dependencies"]
                self.assertTrue(
                    any(g["type"] == "roster membership" and g["count"] == 1
                       for g in groups), (label, groups))
                self.assertIsNotNone(
                    api.store.get_league(fx["league_id"]), label)


class SeasonDeleteStrandingTest(unittest.TestCase):
    def test_any_membership_blocks_delete_zero_write(self):
        for label, store in _each_store():
            with self.subTest(backend=label):
                api = ApiService(store)
                fx = _fixture(api)
                m = _make_membership(api, fx, "applicant")
                res = api.delete_season(fx["season"]["id"], actor_id=ADMIN)
                self.assertEqual(res["error"]["code"], "has_dependencies",
                                 (label, res))
                groups = res["error"]["details"]["dependencies"]
                self.assertTrue(
                    any(g["type"] == "roster membership" and g["count"] == 1
                       for g in groups), (label, groups))
                self.assertIsNotNone(
                    api.store.get_season(fx["season"]["id"]), label)


class TeamDeleteStrandingTest(unittest.TestCase):
    def test_any_membership_blocks_delete_zero_write(self):
        for label, store in _each_store():
            with self.subTest(backend=label):
                api = ApiService(store)
                fx = _fixture(api)
                m = _make_membership(api, fx, "applicant")
                res = api.delete_team(fx["team"]["id"], actor_id=ADMIN)
                self.assertEqual(res["error"]["code"], "has_dependencies",
                                 (label, res))
                groups = res["error"]["details"]["dependencies"]
                self.assertTrue(
                    any(g["type"] == "roster membership" and g["count"] == 1
                       for g in groups), (label, groups))
                self.assertIsNotNone(
                    api.store.get_team(fx["team"]["id"]), label)


class PlayerDeleteStrandingTest(unittest.TestCase):
    def test_any_membership_blocks_delete_zero_write(self):
        for label, store in _each_store():
            with self.subTest(backend=label):
                api = ApiService(store)
                fx = _fixture(api)
                m = _make_membership(api, fx, "applicant")
                before_audit = len(api.store.all_setup_audit())
                res = api.delete_player(fx["player"]["id"], actor_id=ADMIN)
                self.assertEqual(res["error"]["code"], "has_dependencies",
                                 (label, res))
                groups = res["error"]["details"]["dependencies"]
                self.assertTrue(
                    any(g["type"] == "roster membership" and g["count"] == 1
                       for g in groups), (label, groups))
                self.assertIsNotNone(
                    api.store.get_player(fx["player"]["id"]), label)
                self.assertEqual(len(api.store.all_setup_audit()),
                                 before_audit, label)


class TransferStrandingTest(unittest.TestCase):
    """``transfer_team_to_league`` must refuse to move/supersede a
    registration out from under a LIVE membership still naming its OLD
    LeagueSeason — zero Team/registration/membership/audit mutation."""

    def test_live_membership_blocks_transfer_zero_write(self):
        for label in ("memory", "sqlite"):
            for status in _LIVE_STATUSES:
                with self.subTest(backend=label, status=status):
                    api = ApiService(InMemoryStore() if label == "memory"
                                     else SqlStore(":memory:"))
                    fx = _fixture(api)
                    m = _make_membership(api, fx, status)
                    self.assertNotIn("error", m, (label, status, m))
                    before_audit = len(api.store.all_setup_audit())
                    res = api.transfer_team_to_league(
                        fx["team"]["id"], fx["other_league_id"],
                        actor_id=ADMIN)
                    self.assertEqual(res["error"]["details"]["reason"],
                                     "team_transfer_strands_memberships",
                                     (label, status, res))
                    blocked = res["error"]["details"]["blocked"]
                    self.assertEqual(len(blocked), 1, (label, status, blocked))
                    self.assertIn(m["id"],
                                 blocked[0]["affected_membership_ids"],
                                 (label, status))
                    # Zero write: the Team's permanent League is unchanged.
                    team = api.store.get_team(fx["team"]["id"])
                    self.assertEqual(team.league_id, fx["league_id"],
                                     (label, status))
                    reg = api.store.get_season_team_registration(
                        fx["reg"]["id"])
                    self.assertEqual(reg.league_season_id, fx["ls_id"],
                                     (label, status))
                    self.assertEqual(len(api.store.all_setup_audit()),
                                     before_audit, (label, status))

    def test_terminal_membership_does_not_block_transfer(self):
        for label, store in _each_store():
            with self.subTest(backend=label):
                api = ApiService(store)
                fx = _fixture(api)
                m = _make_membership(api, fx, "applicant")
                # Direct store write, not the now-unconditionally-refused
                # set_season_roster_membership_status (#205 review round 2
                # owner ruling) — this test's subject is transfer's
                # behavior against an ALREADY-terminal membership, not the
                # transition method's own (now removed) authorization.
                _end_membership_directly(api.store, m["id"], "released")
                res = api.transfer_team_to_league(
                    fx["team"]["id"], fx["other_league_id"], actor_id=ADMIN)
                self.assertNotIn("error", res, (label, res))
                self.assertEqual(res["league_id"] if "league_id" in res
                                 else fx["other_league_id"],
                                 fx["other_league_id"], label)


class ReactivationSpineTest(unittest.TestCase):
    """``set_season_roster_membership_status`` reactivation (-> active) must
    recheck the SAME Player/Team/LeagueSeason/active-registration spine
    ``create_season_roster_membership`` required at birth (#205 review round
    1 finding 2). The other fixes in this module now BLOCK the parent
    mutations that would strand a live membership going forward, so these
    tests reach the broken-spine state the only way still possible: a
    direct store write that bypasses the service (a restored backup, or any
    future write path this store itself does not forbid)."""

    def _parked(self, api, fx):
        m = _make_membership(api, fx, "applicant")
        self.assertNotIn("error", m)
        parked = api.set_season_roster_membership_status(
            m["id"], "inactive", actor_id=ADMIN)
        self.assertNotIn("error", parked)
        return parked

    def test_blocked_when_team_missing_out_of_band(self):
        for label, store in _each_store():
            with self.subTest(backend=label):
                api = ApiService(store)
                fx = _fixture(api)
                m = self._parked(api, fx)
                if label == "sqlite":
                    # SQLite enforces the FK — a direct store delete_team()
                    # while a live membership still names it is itself a
                    # driver-level integrity violation there (the review's
                    # "generic FK conflict" symptom, one level lower: the
                    # STORE method, not the SERVICE's already-fixed
                    # delete_team, which never reaches this). Memory has no
                    # FK enforcement at all — the dangling-pointer half of
                    # the same finding — so it is covered directly below.
                    with self.assertRaises(Exception, msg=label):
                        api.store.delete_team(fx["team"]["id"])
                    continue
                api.store.delete_team(fx["team"]["id"])  # bypasses the service
                res = api.set_season_roster_membership_status(
                    m["id"], "active", actor_id=ADMIN)
                self.assertEqual(res["error"]["details"]["reason"],
                                 "membership_team_missing", (label, res))

    def test_blocked_when_registration_inactive_out_of_band(self):
        for label, store in _each_store():
            with self.subTest(backend=label):
                api = ApiService(store)
                fx = _fixture(api)
                m = self._parked(api, fx)
                reg = api.store.get_season_team_registration(fx["reg"]["id"])
                reg.active = False
                api.store.save_season_team_registration(reg)
                res = api.set_season_roster_membership_status(
                    m["id"], "active", actor_id=ADMIN)
                self.assertEqual(res["error"]["details"]["reason"],
                                 "team_not_registered", (label, res))

    def test_blocked_when_team_league_mismatched_out_of_band(self):
        for label, store in _each_store():
            with self.subTest(backend=label):
                api = ApiService(store)
                fx = _fixture(api)
                m = self._parked(api, fx)
                team = api.store.get_team(fx["team"]["id"])
                team.league_id = fx["other_league_id"]
                api.store.save_team(team)
                res = api.set_season_roster_membership_status(
                    m["id"], "active", actor_id=ADMIN)
                self.assertEqual(res["error"]["details"]["reason"],
                                 "membership_league_mismatch", (label, res))

    def test_blocked_when_player_missing_out_of_band(self):
        for label, store in _each_store():
            with self.subTest(backend=label):
                api = ApiService(store)
                fx = _fixture(api)
                m = self._parked(api, fx)
                if label == "sqlite":
                    # SQLite enforces the FK — deleting the Player out from
                    # under a live membership is itself a driver-level
                    # integrity violation there, proving the SAME gap a
                    # different way (an untranslated raw error) rather than
                    # letting this specific probe run; Memory has no FK
                    # enforcement at all, which is exactly finding 2's
                    # dangling-pointer evidence, so it is covered directly.
                    with self.assertRaises(Exception, msg=label):
                        api.store.delete_player(fx["player"]["id"])
                    continue
                api.store.delete_player(fx["player"]["id"])
                res = api.set_season_roster_membership_status(
                    m["id"], "active", actor_id=ADMIN)
                self.assertEqual(res["error"]["details"]["reason"],
                                 "membership_player_missing", (label, res))

    def test_reactivation_succeeds_when_spine_intact(self):
        for label, store in _each_store():
            with self.subTest(backend=label):
                api = ApiService(store)
                fx = _fixture(api)
                m = self._parked(api, fx)
                res = api.set_season_roster_membership_status(
                    m["id"], "active", actor_id=ADMIN)
                self.assertNotIn("error", res, (label, res))
                self.assertEqual(res["status"], "active", label)


_PG_SKIP = ("PostgreSQL not configured (TEST_DATABASE_URL) or psycopg "
            "missing — the #205 review round 1 finding 2 parent-mutation "
            "races were NOT exercised on PostgreSQL. A SKIP HERE IS NOT A "
            "PASS. Set TEST_DATABASE_URL (run_parallel.py --postgres does) "
            "to run it.")


def _pg_fixture(url):
    store = SqlStore(url)
    store.reset_schema()
    api = ApiService(store)
    fx = _fixture(api)
    store.close()
    return fx


@unittest.skipUnless(os.environ.get("TEST_DATABASE_URL"), _PG_SKIP)
class CreateVsUnregisterRaceTest(unittest.TestCase):
    """Real TWO-CONNECTION PostgreSQL race: create_season_roster_membership
    vs unregister_team_from_season on the SAME registration, both commit
    orders. Both lock the Season row (create via _require_active_season,
    unregister via the identical guard), so the engine always linearizes
    them — the required outcome is that the two NEVER both succeed in a way
    that leaves an active membership on an unregistered Team; the loser
    always observes the winner's committed state and fails closed with a
    stable reason."""

    def setUp(self):
        self.url = os.environ["TEST_DATABASE_URL"]

    @staticmethod
    def _sync_at_shared_prefix(store, lock_barrier):
        """Make ``store``'s ``get_league_season`` call the rendezvous, for
        exactly its ONE race-relevant invocation, instead of only
        synchronizing thread start.

        Root-caused with direct instrumentation (see the PR body/commit) in
        two stages — a plain start-of-thread ``threading.Barrier(2)`` (the
        pattern the other three real-Postgres race tests in this PR already
        get away with) is not enough here, and neither is the first, more
        obvious attempt to fix it:

        1. Unlike ``CreateVsTransferRaceTest`` (both sides' FIRST statement
           is the identical ``get_team_for_update`` call, so a start-of-
           thread barrier already puts them shoulder-to-shoulder at the real
           contention point), this race's two operations reach their SHARED
           lock (``_require_active_season`` -> ``get_season_for_update``,
           both via the identical guard — see the class docstring) via
           UNEQUAL-COST prior work: ``create_season_roster_membership``'s
           first statement is ``get_team_for_update`` (a
           ``SELECT ... FOR UPDATE`` row lock, a heavier round trip) plus a
           plain read; ``unregister_team_from_season``'s is two plain reads.
           Measured directly (10/10 rounds), the cheaper side (unregister)
           consistently reached a start-of-thread barrier FIRST and
           therefore BLOCKED on it, while create arrived last and
           consequently proceeded WITHOUT blocking — CPython's
           ``threading.Barrier`` releases whichever thread's ``wait()`` call
           completes the party count immediately, still holding the GIL,
           while the other waiter needs a real OS-level wakeup to resume —
           so create's lock statement reached Postgres first in all 10/10
           measured rounds purely from that handicap, not real DB/OS
           scheduling: the flake this test showed.
        2. The obvious fix — synchronize at the lock statement itself
           (``get_season_for_update``) instead of thread start — swaps WHO
           the handicap favors but does not remove it (measured: ~85-90% of
           rounds still won by one fixed side across repeated process runs,
           still well within a single 8-round run's failure zone). The
           actual lever that makes the other three tests fair isn't barrier
           PLACEMENT by itself — it's that their shared statement has ZERO
           divergent work before it on either side, so which thread the
           barrier's own release-order quirk favors is incidental, not tied
           to a structural cost difference. ``create`` and ``unregister``
           genuinely share a statement with that same property: their
           SECOND call is the identical ``get_league_season`` (create calls
           ``self.store.get_league_season`` directly; unregister's
           ``_season_of_league_season`` is exactly that same call) — and
           from there, BOTH paths take the SAME next step (one negligible
           in-Python truthy check, then ``_require_active_season``) with NO
           further asymmetric work before the real lock. Synchronizing
           there removes the structural handicap the same way the other
           tests' natural first-statement symmetry does.
        3. Naively wrapping ``get_league_season`` for the store's whole
           lifetime, though, reintroduces a DIFFERENT, correctness-breaking
           bug: ``ApiService.unregister_team_from_season``'s facade wrapper
           calls ``self._registration_dict(...)`` on the result, which makes
           its OWN unguarded ``get_league_season`` call to resolve
           ``season_id``/``league_id`` for the legacy v1 response shape —
           AFTER the transaction, with no matching call on create's side
           (``create_season_roster_membership``'s facade wrapper is a plain
           ``_serialize(...)``). A barrier wrap left in place sees
           unregister call ``get_league_season`` TWICE per round while
           create calls it once, and the unmatched second call blocks until
           the barrier's timeout, raising ``BrokenBarrierError``
           (reproduced directly).

        The fix: un-wrap immediately on first use, so the barrier only ever
        gates the ONE race-relevant, in-transaction, matched call on each
        side — any later call (the facade's post-transaction serialization)
        runs the plain, unwrapped method.
        """
        original = store.get_league_season

        def synced(league_season_id):
            store.get_league_season = original  # one-shot: never re-gate
            lock_barrier.wait(timeout=5)
            return original(league_season_id)

        store.get_league_season = synced

    def _race_once(self, fx):
        start_barrier = threading.Barrier(2)
        lock_barrier = threading.Barrier(2)
        results = {}

        def do_create():
            store = SqlStore(self.url)
            self._sync_at_shared_prefix(store, lock_barrier)
            api = ApiService(store)
            try:
                start_barrier.wait(timeout=5)
                results["create"] = api.create_season_roster_membership(
                    fx["player"]["id"], fx["ls_id"], fx["team"]["id"],
                    status="applicant", jersey_number=None, actor_id=ADMIN)
            finally:
                store.close()

        def do_unregister():
            store = SqlStore(self.url)
            self._sync_at_shared_prefix(store, lock_barrier)
            api = ApiService(store)
            try:
                start_barrier.wait(timeout=5)
                results["unregister"] = api.unregister_team_from_season(
                    fx["reg"]["id"], actor_id=ADMIN)
            finally:
                store.close()

        threads = [threading.Thread(target=do_create),
                  threading.Thread(target=do_unregister)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=15)
        return results

    def test_never_leaves_active_membership_on_unregistered_team(self):
        winners = {"create": 0, "unregister": 0}
        # 80 rounds, not 8 (a DELIBERATE, DOCUMENTED gap from this file's
        # other race tests, which all use 8-10) — a stated limitation, not
        # an oversight. _sync_at_shared_prefix (above) synchronizes the two
        # racers on their genuinely shared statement, which measurably fixed
        # the WORST failure mode this test originally had: a start-of-thread-
        # only barrier let ``create`` win the Season-row-lock race 8/8 EVERY
        # observed round (confirmed by direct instrumentation — see that
        # method's docstring), because its heavier first statement
        # (``get_team_for_update``, a row lock) made it consistently the
        # LAST thread to reach a 2-party barrier, and CPython releases the
        # last arriver without blocking. That specific, deterministic
        # mechanism is now removed.
        # What remains, even after that fix, is a REAL residual skew — not a
        # Python synchronization artifact, but two structurally different
        # operations (create locks a Team row before the shared LeagueSeason
        # read; unregister does not) whose actual PostgreSQL-side costs
        # differ enough that even a matched, shared release point does not
        # land the two sides at the Season-row lock at genuinely equal odds.
        # Measured directly, over many independent 30-round samples on real
        # PostgreSQL: ``create`` won roughly 70-93% of individual rounds
        # (never 100%, so the underlying contest is real, not fixed) — i.e.
        # a per-round skew, not a per-run one. At 8 rounds that skew still
        # produces an all-one-side sweep often enough to flake. Forcing
        # genuinely equal PER-ROUND odds between two operations with
        # different real costs, without touching production code, was tried
        # multiple ways (synchronizing at the lock statement itself; a
        # 3-party rendezvous removing the "last arrival" advantage entirely,
        # at both the lock statement and the shared prefix) and every
        # variant left a comparable double-digit-percent residual skew, just
        # favoring whichever side a given mechanism's own release-order
        # quirk happened to help — evidence the remaining gap is genuine
        # DB/OS scheduling noise on top of a real cost asymmetry, not
        # something more synchronization engineering removes. So this falls
        # back to (b): keep the real fix above, and raise the round count
        # far enough that "both orderings occur" holds with overwhelming
        # probability despite that residual skew, rather than dropping the
        # requirement. Even at a conservative 90% single-side skew, 80
        # rounds puts the chance of an all-one-side sweep under 0.03% (was
        # ~100% at 8 rounds pre-fix); this was verified empirically with 20
        # consecutive full runs at this round count (see the PR body) before
        # landing.
        for i in range(80):
            fx = _pg_fixture(self.url)
            results = self._race_once(fx)
            create_ok = "error" not in results["create"]
            unregister_ok = "error" not in results["unregister"]
            winners["create"] += create_ok
            winners["unregister"] += unregister_ok
            checker = SqlStore(self.url)
            try:
                reg = checker.get_season_team_registration(fx["reg"]["id"])
                memberships = [
                    m for m in checker.all_season_roster_memberships()
                    if m.player_id == fx["player"]["id"]
                    and not m.status.is_terminal]
                if create_ok:
                    # create committed: unregister MUST have lost and the
                    # registration MUST still be active (or unregister also
                    # "succeeded" as a no-op AFTER seeing the fresh
                    # membership block it — either way never both truly won).
                    self.assertEqual(len(memberships), 1, (i, results))
                    if not unregister_ok:
                        self.assertEqual(
                            results["unregister"]["error"]["details"]
                            ["reason"], "team_has_live_memberships",
                            (i, results))
                else:
                    self.assertEqual(
                        results["create"]["error"]["details"]["reason"],
                        "team_not_registered", (i, results))
                    self.assertFalse(reg.active, (i, results))
                    self.assertEqual(len(memberships), 0, (i, results))
            finally:
                checker.close()
        # Both orderings actually happened across the 80 rounds.
        self.assertGreater(winners["create"], 0, winners)
        self.assertGreater(winners["unregister"], 0, winners)


@unittest.skipUnless(os.environ.get("TEST_DATABASE_URL"), _PG_SKIP)
class CreateVsTransferRaceTest(unittest.TestCase):
    """Real TWO-CONNECTION PostgreSQL race: create_season_roster_membership
    vs transfer_team_to_league moving the SAME Team to a different League,
    starting from NO existing membership (so, unlike reactivating an
    ALREADY-live row, either side can genuinely commit first). Both lock the
    Team row (create via get_team_for_update; transfer via the identical
    call), so the engine linearizes them — required outcome: never an
    active membership whose league_season_id disagrees with the Team's
    CURRENT League, regardless of which commits first."""

    def setUp(self):
        self.url = os.environ["TEST_DATABASE_URL"]

    def _race_once(self, fx):
        barrier = threading.Barrier(2)
        results = {}

        def do_create():
            store = SqlStore(self.url)
            api = ApiService(store)
            try:
                barrier.wait(timeout=5)
                results["create"] = api.create_season_roster_membership(
                    fx["player"]["id"], fx["ls_id"], fx["team"]["id"],
                    status="applicant", jersey_number=None, actor_id=ADMIN)
            finally:
                store.close()

        def do_transfer():
            store = SqlStore(self.url)
            api = ApiService(store)
            try:
                barrier.wait(timeout=5)
                results["transfer"] = api.transfer_team_to_league(
                    fx["team"]["id"], fx["other_league_id"], actor_id=ADMIN)
            finally:
                store.close()

        threads = [threading.Thread(target=do_create),
                  threading.Thread(target=do_transfer)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=15)
        return results

    def test_never_leaves_active_membership_disagreeing_with_team_league(self):
        winners = {"create": 0, "transfer": 0}
        for i in range(10):
            fx = _pg_fixture(self.url)
            results = self._race_once(fx)
            create_ok = "error" not in results["create"]
            transfer_ok = "error" not in results["transfer"]
            winners["create"] += create_ok
            winners["transfer"] += transfer_ok

            checker = SqlStore(self.url)
            try:
                team = checker.get_team(fx["team"]["id"])
                memberships = [
                    m for m in checker.all_season_roster_memberships()
                    if m.player_id == fx["player"]["id"]]
                for m in memberships:
                    ls = checker.get_league_season(m.league_season_id)
                    self.assertEqual(
                        ls.league_id, team.league_id,
                        (i, results, "Team<->LeagueSeason League "
                         "disagreement", m.id))
                if create_ok and transfer_ok:
                    # Only reachable if create committed BEFORE transfer's
                    # candidate scan ran — transfer then legitimately found
                    # the fresh membership and must have blocked, not both
                    # truly "succeeded" independently.
                    self.fail((i, "create and transfer both reported "
                              "success", results))
                elif not create_ok:
                    # Transfer won first: Team.league_id now names the NEW
                    # League while the (unchanged) ls_id this create still
                    # targets names the OLD one — the league-mismatch check
                    # fires before create even reaches the registration
                    # lookup (create_season_roster_membership checks League
                    # coherence first).
                    self.assertEqual(
                        results["create"]["error"]["details"]["reason"],
                        "membership_league_mismatch", (i, results))
                elif not transfer_ok:
                    self.assertEqual(
                        results["transfer"]["error"]["details"]["reason"],
                        "team_transfer_strands_memberships", (i, results))
            finally:
                checker.close()
        # Both orderings actually happened across the 10 rounds.
        self.assertGreater(winners["create"], 0, winners)
        self.assertGreater(winners["transfer"], 0, winners)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
