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
import unittest

from helpers import BACKEND  # noqa: F401  (ensures sys.path is set up)
from helpers import end_membership_directly as _end_membership_directly
from helpers import race_with_forced_order

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



# --------------------------------------------------------------------------- #
# #205 review round 3 blocker 2 — reviving a PARKED membership on a broken     #
# spine. ReactivationSpineTest above proves the -> ``active`` path; the guard  #
# it tests was written as ``if status is ACTIVE``, so EVERY OTHER target left  #
# the spine unchecked. The owner reproduced inactive -> applicant and          #
# inactive -> affiliate on Memory AND SQLite: both succeeded and wrote the new #
# status onto a registration that had been deactivated through the store.      #
# --------------------------------------------------------------------------- #

# The PARKED statuses a stint can be set aside in, and the targets
# ``create_season_roster_membership`` demands a full valid spine for. Every
# (parked, target) pair below is a REVIVAL and must re-prove the spine.
_PARKED = ("inactive", "injured")
_REVIVE_TARGETS = ("applicant", "affiliate", "active")


def _suspend_membership_fks(store):
    """Let a test repoint a membership at a parent id that does not exist.

    Only the ``season_roster_memberships`` table's OWN outbound foreign keys
    are suspended — never the whole schema — so the corruption stays exactly
    as narrow as the state under test. SQLite has no per-constraint DROP for
    inline FKs, so enforcement is disabled on the connection; PostgreSQL's
    are looked up from ``pg_constraint`` rather than assumed by name.

    Repointing the MEMBERSHIP is deliberate, and is why this works
    identically on all three stores: deleting the parent Team/Player row
    instead would collide with the FKs that OTHER tables (players.team_id,
    game_roster_entries, ...) hold on it, which is why
    ReactivationSpineTest's own delete-based probes above can only run on
    Memory. The membership's view of its spine — a required pointer naming
    a row that is not there — is identical either way, and it is that view
    ``_assert_membership_spine_valid`` reads."""
    if not isinstance(store, SqlStore):
        return
    cur = store.conn.cursor()
    if store.backend == "sqlite":
        cur.execute("PRAGMA foreign_keys = OFF")
        return
    cur.execute("SELECT conname FROM pg_constraint WHERE conrelid = "
                "'season_roster_memberships'::regclass AND contype = 'f'")
    for row in cur.fetchall():
        cur.execute("ALTER TABLE season_roster_memberships "
                    f"DROP CONSTRAINT {row['conname']}")


def _repoint_membership(api, membership_id, **fields):
    """Write a raw pointer straight onto the membership row, bypassing the
    service — the out-of-band write (restored backup, direct SQL) that is
    the only remaining way to reach a broken spine now that this module's
    other fixes block every service-level parent mutation."""
    _suspend_membership_fks(api.store)
    row = api.store.get_season_roster_membership(membership_id)
    for name, value in fields.items():
        setattr(row, name, value)
    if isinstance(api.store, SqlStore):
        with api.store.transaction():
            api.store.save_season_roster_membership(row)
    else:
        api.store.save_season_roster_membership(row)


# Each breaker plants ONE broken-spine shape and returns the stable
# ``reason`` the revival must fail with. Keyed by the kinds the owner named:
# an inactive registration, a missing parent (one entry per parent the spine
# actually has), and a Team<->League mismatch.
def _break_registration_inactive(api, fx, m):
    reg = api.store.get_season_team_registration(fx["reg"]["id"])
    reg.active = False
    if isinstance(api.store, SqlStore):
        with api.store.transaction():
            api.store.save_season_team_registration(reg)
    else:
        api.store.save_season_team_registration(reg)
    return "team_not_registered"


def _break_team_missing(api, fx, m):
    _repoint_membership(api, m["id"], team_id="ghost_team")
    return "membership_team_missing"


def _break_league_season_missing(api, fx, m):
    _repoint_membership(api, m["id"], league_season_id="ghost_ls")
    return "membership_league_season_missing"


def _break_player_missing(api, fx, m):
    _repoint_membership(api, m["id"], player_id="ghost_player")
    return "membership_player_missing"


def _break_team_league_mismatch(api, fx, m):
    team = api.store.get_team(fx["team"]["id"])
    team.league_id = fx["other_league_id"]
    if isinstance(api.store, SqlStore):
        with api.store.transaction():
            api.store.save_team(team)
    else:
        api.store.save_team(team)
    return "membership_league_mismatch"


def _break_team_league_missing(api, fx, m):
    """#205 review round 3 blocker 3 — the Team's permanent League is GONE,
    not merely different. The guard used to read ``if team.league_id and
    ls.league_id != team.league_id``, so this shape SKIPPED the coherence
    check instead of failing it, and every revival below succeeded on it."""
    team = api.store.get_team(fx["team"]["id"])
    team.league_id = None
    if isinstance(api.store, SqlStore):
        with api.store.transaction():
            api.store.save_team(team)
    else:
        api.store.save_team(team)
    assert api.store.get_team(fx["team"]["id"]).league_id is None
    return "membership_league_mismatch"


_BREAKERS = {
    "registration_inactive": _break_registration_inactive,
    "missing_parent_team": _break_team_missing,
    "missing_parent_league_season": _break_league_season_missing,
    "missing_parent_player": _break_player_missing,
    "team_league_mismatch": _break_team_league_mismatch,
    "team_league_missing": _break_team_league_missing,
}


class ParkedRevivalSpineTest(unittest.TestCase):
    """#205 review round 3 blocker 2 — a PARKED membership must re-prove its
    FULL Player/Team/LeagueSeason/active-registration spine before it is
    revived into ANY of ``applicant``, ``affiliate`` or ``active``, not only
    ``active``.

    ``_assert_membership_spine_valid``'s own contract has always named all
    three statuses; its single call site in
    ``set_season_roster_membership_status`` fired on ``active`` alone. So a
    membership created as ``applicant``, parked to ``inactive``, and whose
    SeasonTeamRegistration was then deactivated through the store, could be
    moved inactive -> applicant or inactive -> affiliate and the new status
    was written onto that dead spine — reproduced by the owner on Memory and
    SQLite, and by this branch on PostgreSQL as well. It matters because
    ``create_season_roster_membership`` REQUIRES an active registration for
    those very statuses, and the parent-mutation guards in this module treat
    every non-terminal membership as live, so restored/direct-write
    corruption could be re-exposed through a mere status change.

    THE MATRIX, in full — the owner asked for the whole thing, not a sample:
    3 stores (Memory, SQLite, PostgreSQL) x 2 parked sources (inactive,
    injured) x 3 targets (applicant, affiliate, active) x 6 broken-spine
    shapes = 108 refusals, each asserting the stable reason AND zero writes
    across ALL THREE write surfaces (the membership row, the per-membership
    event history, and the global audit log) — a caught exception alone does
    not prove nothing was written, this module's own standing rule.

    ``active`` is carried through the matrix alongside the two targets the
    blocker names because it is the behaviour that was already correct: if a
    future edit ever narrows the guard back to a subset, these cases fail
    too, rather than silently ceding ground that was already won.
    """

    def _stores(self):
        yield "memory", InMemoryStore()
        yield "sqlite", SqlStore(":memory:")
        url = os.environ.get("TEST_DATABASE_URL")
        if url:
            store = SqlStore(url)
            store.reset_schema()
            yield "postgres", store

    def _close(self, label, store):
        if label == "postgres":
            store.reset_schema()
        if isinstance(store, SqlStore):
            store.close()

    def _parked(self, api, fx, parked):
        """The owner's exact opening: CREATE APPLICANT, then park it."""
        m = _make_membership(api, fx, "applicant")
        self.assertNotIn("error", m, m)
        res = api.set_season_roster_membership_status(
            m["id"], parked, actor_id=ADMIN)
        self.assertNotIn("error", res, res)
        return m

    def _snapshot(self, api, membership_id):
        """All THREE write surfaces: the row's own status, the append-only
        per-membership event history, and the global setup audit log."""
        row = api.store.get_season_roster_membership(membership_id)
        events = api.store.events_for_membership(membership_id)
        audits = [a for a in api.store.all_setup_audit()
                  if a.entity_id == membership_id]
        return (row.status.value, len(events), len(audits))

    def test_broken_spine_refuses_every_revival_zero_write(self):
        checked = 0
        for label, store in self._stores():
            try:
                for parked in _PARKED:
                    for kind, breaker in _BREAKERS.items():
                        for target in _REVIVE_TARGETS:
                            case = (label, parked, target, kind)
                            with self.subTest(backend=label, parked=parked,
                                              target=target, spine=kind):
                                # A FRESH fixture per cell, deliberately:
                                # sharing one planted corruption across the
                                # three targets would let a single leaked
                                # write contaminate the cells after it, so a
                                # falsification run could not say WHICH cell
                                # the guard actually covers.
                                api = ApiService(store)
                                fx = _fixture(api)
                                m = self._parked(api, fx, parked)
                                expected_reason = breaker(api, fx, m)
                                before = self._snapshot(api, m["id"])
                                self.assertEqual(before[0], parked, case)
                                res = api.set_season_roster_membership_status(
                                    m["id"], target, actor_id=ADMIN)
                                self.assertIn("error", res, (case, res))
                                self.assertEqual(
                                    res["error"]["details"]["reason"],
                                    expected_reason, (case, res))
                                # ZERO WRITE on all three surfaces.
                                self.assertEqual(
                                    self._snapshot(api, m["id"]), before,
                                    case)
                            checked += 1
            finally:
                self._close(label, store)
        # 2 parked x 3 targets x 6 spines = 36 per store (the sixth spine is
        # blocker 3's MISSING Team.league_id). Memory + SQLite always;
        # PostgreSQL when configured. Pinned against the ENVIRONMENT so a
        # store silently dropping out of self._stores() cannot shrink the
        # matrix unnoticed -- never against len(_BREAKERS) or the store
        # list, which would move with the loop and could not fire.
        expected = 36 * (3 if os.environ.get("TEST_DATABASE_URL") else 2)
        self.assertEqual(checked, expected)

    def test_intact_spine_revival_succeeds_from_every_parked_status(self):
        """The guard must REFUSE a dead spine, not refuse everything: with
        the spine untouched, every parked -> revival transition still
        succeeds and writes its event + audit row exactly as before."""
        checked = 0
        for label, store in self._stores():
            try:
                for parked in _PARKED:
                    for target in _REVIVE_TARGETS:
                        with self.subTest(backend=label, parked=parked,
                                          target=target):
                            api = ApiService(store)
                            fx = _fixture(api)
                            m = self._parked(api, fx, parked)
                            before = self._snapshot(api, m["id"])
                            res = api.set_season_roster_membership_status(
                                m["id"], target, actor_id=ADMIN)
                            self.assertNotIn("error", res,
                                             (label, parked, target, res))
                            self.assertEqual(res["status"], target,
                                             (label, parked, target))
                            after = self._snapshot(api, m["id"])
                            self.assertEqual(
                                after,
                                (target, before[1] + 1, before[2] + 1),
                                (label, parked, target))
                            checked += 1
            finally:
                self._close(label, store)
        expected = 6 * (3 if os.environ.get("TEST_DATABASE_URL") else 2)
        self.assertEqual(checked, expected)

    def test_terminal_refusal_still_precedes_the_spine_check(self):
        """A parked row targeting a TERMINAL status must still get the
        unconditional ``terminal_transition_not_authorized`` refusal (#205
        review round 2 owner ruling) — NOT a spine error — even when the
        spine is broken. The revival set and the terminal set are disjoint,
        so widening the spine guard must not reorder these two."""
        for label, store in self._stores():
            try:
                for parked in _PARKED:
                    for target in ("released", "transferred"):
                        with self.subTest(backend=label, parked=parked,
                                          target=target):
                            api = ApiService(store)
                            fx = _fixture(api)
                            m = self._parked(api, fx, parked)
                            _break_registration_inactive(api, fx, m)
                            before = self._snapshot(api, m["id"])
                            res = api.set_season_roster_membership_status(
                                m["id"], target, actor_id=ADMIN)
                            self.assertEqual(res["error"]["code"], "forbidden",
                                             (label, parked, target, res))
                            self.assertEqual(
                                res["error"]["details"]["reason"],
                                "terminal_transition_not_authorized",
                                (label, parked, target, res))
                            self.assertEqual(self._snapshot(api, m["id"]),
                                             before, (label, parked, target))
            finally:
                self._close(label, store)

    def test_revival_to_applicant_affiliate_keeps_active_only_uniqueness(self):
        """Widening the SPINE check must not widen the UNIQUENESS rules. A
        second player already holds the same jersey on an ACTIVE membership
        of this (LeagueSeason, Team); reviving a parked row to applicant or
        affiliate on an INTACT spine must still succeed, because the jersey
        and one-active-per-Season rules remain ``active``-only."""
        for label, store in self._stores():
            try:
                for target in ("applicant", "affiliate"):
                    with self.subTest(backend=label, target=target):
                        api = ApiService(store)
                        fx = _fixture(api)
                        # The parked row carries jersey 7.
                        m = api.create_season_roster_membership(
                            fx["player"]["id"], fx["ls_id"], fx["team"]["id"],
                            status="applicant", jersey_number=7,
                            actor_id=ADMIN)
                        self.assertNotIn("error", m, m)
                        self.assertNotIn(
                            "error", api.set_season_roster_membership_status(
                                m["id"], "inactive", actor_id=ADMIN))
                        # Another player takes jersey 7 ACTIVE on the same
                        # (LeagueSeason, Team).
                        other = api.create_player(
                            fx["team"]["id"], "Rival", "forward",
                            jersey_number=7, actor_id=ADMIN)
                        rival = api.create_season_roster_membership(
                            other["id"], fx["ls_id"], fx["team"]["id"],
                            status="active", jersey_number=7, actor_id=ADMIN)
                        self.assertNotIn("error", rival, rival)
                        # Reviving to applicant/affiliate is NOT blocked by
                        # that active jersey — the rule is active-only.
                        res = api.set_season_roster_membership_status(
                            m["id"], target, actor_id=ADMIN)
                        self.assertNotIn("error", res, (label, target, res))
                        self.assertEqual(res["status"], target, (label, target))
                        # ...and reviving the SAME row to ACTIVE still IS
                        # blocked by it, unchanged.
                        conflict = api.set_season_roster_membership_status(
                            m["id"], "active", actor_id=ADMIN)
                        self.assertEqual(
                            conflict["error"]["details"]["reason"],
                            "duplicate_membership_jersey_number",
                            (label, target, conflict))
            finally:
                self._close(label, store)


class CreateSpineMissingLeagueTest(unittest.TestCase):
    """#205 review round 3 blocker 3 — ``create_season_roster_membership``
    must REFUSE to mint a membership on a Team with NO permanent League,
    not skip the check.

    Both membership League guards were spelled ``if team.league_id and
    ls.league_id != team.league_id``. The leading conjunct is a FALSY-SKIP:
    a league-less Team never reached the comparison, so the guard passed by
    omission. That is the service-layer twin of the NULL evasion blocker 1
    fixed in migration 059's preflight, where ``a != b`` evaluated UNKNOWN
    (not TRUE) against NULL and the row was filtered out — and it left the
    two layers openly disagreeing: 059 REFUSES to backfill a league-less
    Team while the live service happily minted memberships on one.

    Reproduced on this branch's prior head 3ee1952 with ``Team.league_id``
    NULLed out of band: 12/12 probes succeeded across Memory, SQLite and
    PostgreSQL — ``create``, and revival of a parked membership into
    applicant, affiliate AND active — each writing a status, a membership
    event and an audit row onto a League-less spine.

    The REVIVAL half of that matrix lives in ParkedRevivalSpineTest above
    (its ``team_league_missing`` breaker). This class covers the CREATE
    half: 3 stores x 3 spine-validated statuses, each asserting the stable
    ``membership_league_mismatch`` reason and ZERO writes on all three
    surfaces (no membership row, no event, no audit) — plus the intact-
    spine controls proving create still SUCCEEDS when the spine is whole.
    A guard that refused everything would pass the refusal half alone.
    """

    def _stores(self):
        yield "memory", InMemoryStore()
        yield "sqlite", SqlStore(":memory:")
        url = os.environ.get("TEST_DATABASE_URL")
        if url:
            store = SqlStore(url)
            store.reset_schema()
            yield "postgres", store

    def _close(self, label, store):
        if label == "postgres":
            store.reset_schema()
        if isinstance(store, SqlStore):
            store.close()

    def _strip_league(self, api, fx):
        team = api.store.get_team(fx["team"]["id"])
        team.league_id = None
        if isinstance(api.store, SqlStore):
            with api.store.transaction():
                api.store.save_team(team)
        else:
            api.store.save_team(team)
        self.assertIsNone(api.store.get_team(fx["team"]["id"]).league_id)

    def _surfaces(self, api, fx):
        """All THREE write surfaces, keyed to this fixture's player: any
        membership row for them, every event on any such row, and the
        global setup audit log."""
        rows = [m for m in api.store.all_season_roster_memberships()
                if m.player_id == fx["player"]["id"]]
        events = sum(len(api.store.events_for_membership(m.id)) for m in rows)
        return (len(rows), events, len(api.store.all_setup_audit()))

    def test_league_less_team_refuses_create_zero_write(self):
        checked = 0
        for label, store in self._stores():
            try:
                for status in _REVIVE_TARGETS:
                    with self.subTest(backend=label, status=status):
                        api = ApiService(store)
                        fx = _fixture(api)
                        self._strip_league(api, fx)
                        before = self._surfaces(api, fx)
                        self.assertEqual(before[0], 0, (label, status))
                        res = api.create_season_roster_membership(
                            fx["player"]["id"], fx["ls_id"], fx["team"]["id"],
                            status=status, jersey_number=None, actor_id=ADMIN)
                        self.assertIn("error", res, (label, status, res))
                        self.assertEqual(res["error"]["details"]["reason"],
                                         "membership_league_mismatch",
                                         (label, status, res))
                        # The details name the missing key honestly rather
                        # than omitting it.
                        self.assertIsNone(
                            res["error"]["details"]["team_league_id"],
                            (label, status, res))
                        # ZERO WRITE on all three surfaces.
                        self.assertEqual(self._surfaces(api, fx), before,
                                         (label, status))
                    checked += 1
            finally:
                self._close(label, store)
        # 3 statuses per store. Pinned against the ENVIRONMENT, the way
        # ParkedRevivalSpineTest pins its own matrix -- never against the
        # length of the list the loop iterates, which moves with it and
        # could never fire.
        expected = 3 * (3 if os.environ.get("TEST_DATABASE_URL") else 2)
        self.assertEqual(checked, expected)

    def test_intact_spine_create_still_succeeds(self):
        """The control: the guard must refuse a BROKEN spine, not refuse
        everything. With the Team's League untouched, create still succeeds
        for every spine-validated status and writes its event + audit row."""
        checked = 0
        for label, store in self._stores():
            try:
                for status in _REVIVE_TARGETS:
                    with self.subTest(backend=label, status=status):
                        api = ApiService(store)
                        fx = _fixture(api)
                        res = api.create_season_roster_membership(
                            fx["player"]["id"], fx["ls_id"], fx["team"]["id"],
                            status=status, jersey_number=None, actor_id=ADMIN)
                        self.assertNotIn("error", res, (label, status, res))
                        self.assertEqual(res["status"], status,
                                         (label, status))
                        rows, events, _ = self._surfaces(api, fx)
                        self.assertEqual((rows, events), (1, 1),
                                         (label, status))
                    checked += 1
            finally:
                self._close(label, store)
        expected = 3 * (3 if os.environ.get("TEST_DATABASE_URL") else 2)
        self.assertEqual(checked, expected)

    def test_both_sides_missing_is_a_violation_not_agreement(self):
        """Team.league_id AND LeagueSeason.league_id both missing is TWO
        absent scope keys, not agreement — the exact case ``IS DISTINCT
        FROM`` would wave through, and the reason
        ``integrity_checks._MISSING_OR_UNEQUAL`` is spelled out longhand
        (see its docstring). MEMORY ONLY, deliberately: SQLite and
        PostgreSQL both declare ``league_seasons.league_id`` NOT NULL, so
        this shape is unreachable there — the probe that established that
        got ``IntegrityConflictError: A required value is missing`` from
        both engines rather than a planted row."""
        api = ApiService(InMemoryStore())
        fx = _fixture(api)
        self._strip_league(api, fx)
        ls = api.store.get_league_season(fx["ls_id"])
        ls.league_id = None
        api.store.save_league_season(ls)
        before = self._surfaces(api, fx)
        res = api.create_season_roster_membership(
            fx["player"]["id"], fx["ls_id"], fx["team"]["id"],
            status="applicant", jersey_number=None, actor_id=ADMIN)
        self.assertEqual(res["error"]["details"]["reason"],
                         "membership_league_mismatch", res)
        self.assertEqual(self._surfaces(api, fx), before)

# --- #205 review round 4 (owner ruling): the PROGRAM leg of the spine -----
#
# The membership guards validated Player/Team/LeagueSeason existence, Team
# <-> League coherence and an active registration -- and had NO Program
# clause at all (an absence, not a falsy-skip), while migration 059's
# preflight refused every incoherent Program shape via
# ``integrity_checks._MISSING_OR_UNEQUAL``. Reproduced on this branch's head
# 488d1c8: all six shapes below, on Memory, SQLite AND PostgreSQL, had 059's
# preflight REFUSING while the service ACCEPTED create and all twelve parked
# revivals -- 126 accepted probes, each writing a row/status, an event and an
# audit entry onto a spine whose Team, League and Season disagreed about
# which Program they belong to.


def _plant_program(api, obj, kind, value):
    obj.program_id = value
    saver = getattr(api.store, f"save_{kind}")
    if isinstance(api.store, SqlStore):
        with api.store.transaction():
            saver(obj)
    else:
        saver(obj)


def _break_team_program(api, fx, value):
    _plant_program(api, api.store.get_team(fx["team"]["id"]), "team", value)
    return api.store.get_team(fx["team"]["id"]).program_id


def _break_league_program(api, fx, value):
    _plant_program(api, api.store.get_league(fx["league_id"]), "league", value)
    return api.store.get_league(fx["league_id"]).program_id


def _break_season_program(api, fx, value):
    _plant_program(api, api.store.get_season(fx["season"]["id"]), "season",
                   value)
    return api.store.get_season(fx["season"]["id"]).program_id


# The three Program keys the spine is made of, each broken two ways. MISSING
# and UNEQUAL are ONE rule (``_missing_or_unequal`` / ``_MISSING_OR_UNEQUAL``),
# so both halves must produce the same refusal.
_PROGRAM_KEYS = {
    "Team.program_id": _break_team_program,
    "League.program_id": _break_league_program,
    "Season.program_id": _break_season_program,
}
_PROGRAM_MODES = ("missing", "unequal")
_PROGRAM_SHAPES = tuple((key, mode) for key in _PROGRAM_KEYS
                        for mode in _PROGRAM_MODES)
# The columns each shape writes, for the reachability probe below.
_PROGRAM_COLUMNS = {"Team.program_id": ("teams", "program_id"),
                    "League.program_id": ("leagues", "program_id"),
                    "Season.program_id": ("seasons", "program_id")}


class MembershipProgramSpineTest(unittest.TestCase):
    """#205 review round 4, owner ruling — a membership may only be MINTED
    or REVIVED on a spine whose Team, League and Season all name the SAME
    Program.

    THE MATRIX, in full: 6 shapes ({Team, League, Season}.program_id x
    {missing, unequal}) x 3 stores (Memory, SQLite, PostgreSQL) x both
    paths the ruling names — ``create_season_roster_membership``, and parked
    revival through ``set_season_roster_membership_status`` for each of 2
    parked sources (inactive, injured) into each of 3 targets (applicant,
    affiliate, active). 6 create + 36 revival = 42 refusals per store, each
    asserting the stable ``membership_program_mismatch`` reason AND zero
    writes across ALL THREE write surfaces (the membership row/status, the
    append-only per-membership event history, and the global audit log) — a
    caught exception alone does not prove nothing was written.

    INTACT-SPINE CONTROLS ride alongside: with the Program leg untouched,
    create still succeeds for every spine-validated status and every parked
    -> revival transition still succeeds and still writes its event + audit
    row. A guard that refused everything would pass the refusal half alone.

    WHY UNCONDITIONAL, given ``register_team_for_season``'s deliberately
    legacy-permissive rule 4 (``if team.program_id and team.program_id !=
    season.program_id``): that branch tolerates a PROGRAM-LESS Team, but no
    supported flow PRODUCES one. Established by execution over every public
    entry point — ``create_team`` derives the Program from the resolved
    League and refuses a disagreeing one; ``transfer_team_to_league`` HEALS
    a program-less Team; ``roll_forward_registrations`` (both v1 and v2)
    refuse one outright; ``commit_hierarchy_import`` refuses a cross-Program
    team/league pair and both imports heal a program-less Team on re-import;
    and the canonical ``league_id`` registration path refuses one. See
    ``SetupService._assert_membership_program_spine``'s docstring for the
    full finding. Registration semantics are untouched by this guard.
    """

    def _stores(self):
        yield "memory", InMemoryStore()
        yield "sqlite", SqlStore(":memory:")
        url = os.environ.get("TEST_DATABASE_URL")
        if url:
            store = SqlStore(url)
            store.reset_schema()
            yield "postgres", store

    def _close(self, label, store):
        if label == "postgres":
            store.reset_schema()
        if isinstance(store, SqlStore):
            store.close()

    def _break(self, api, fx, key, mode):
        """Plant one shape and PROVE the write stuck. A store whose column
        is NOT NULL would make the shape unreachable; this asserts that
        rather than skipping it silently (see
        ``test_no_program_shape_is_unreachable_on_any_store``)."""
        wanted = (None if mode == "missing"
                  else api.create_program("OtherProg", actor_id=ADMIN)["id"])
        got = _PROGRAM_KEYS[key](api, fx, wanted)
        self.assertEqual(got, wanted, (key, mode))
        return wanted

    def _surfaces(self, api, player_id):
        """All THREE write surfaces for this player: any membership row,
        every event on any such row, and the global setup audit log."""
        rows = [m for m in api.store.all_season_roster_memberships()
                if m.player_id == player_id]
        events = sum(len(api.store.events_for_membership(m.id)) for m in rows)
        return (len(rows), events, len(api.store.all_setup_audit()))

    def _membership_surfaces(self, api, membership_id):
        row = api.store.get_season_roster_membership(membership_id)
        return (row.status.value,
                len(api.store.events_for_membership(membership_id)),
                len([a for a in api.store.all_setup_audit()
                     if a.entity_id == membership_id]))

    def _parked(self, api, fx, parked):
        m = _make_membership(api, fx, "applicant")
        self.assertNotIn("error", m, m)
        res = api.set_season_roster_membership_status(
            m["id"], parked, actor_id=ADMIN)
        self.assertNotIn("error", res, res)
        return m

    # -- CREATE ----------------------------------------------------------
    def test_incoherent_program_refuses_create_zero_write(self):
        checked = 0
        for label, store in self._stores():
            try:
                for key, mode in _PROGRAM_SHAPES:
                    with self.subTest(backend=label, key=key, mode=mode):
                        api = ApiService(store)
                        fx = _fixture(api)
                        planted = self._break(api, fx, key, mode)
                        before = self._surfaces(api, fx["player"]["id"])
                        self.assertEqual(before[0], 0, (label, key, mode))
                        res = api.create_season_roster_membership(
                            fx["player"]["id"], fx["ls_id"], fx["team"]["id"],
                            status="applicant", jersey_number=None,
                            actor_id=ADMIN)
                        self.assertIn("error", res, (label, key, mode, res))
                        details = res["error"]["details"]
                        self.assertEqual(details["reason"],
                                         "membership_program_mismatch",
                                         (label, key, mode, res))
                        # The details name the offending key honestly —
                        # including a MISSING one, rather than omitting it.
                        field = {"Team.program_id": "team_program_id",
                                 "League.program_id": "league_program_id",
                                 "Season.program_id": "season_program_id"}[key]
                        self.assertEqual(details[field], planted,
                                         (label, key, mode, res))
                        # ZERO WRITE on all three surfaces.
                        self.assertEqual(
                            self._surfaces(api, fx["player"]["id"]), before,
                            (label, key, mode))
                    checked += 1
            finally:
                self._close(label, store)
        # 6 shapes per store. Pinned against the ENVIRONMENT -- an
        # INDEPENDENT source -- never against the length of the list the
        # loop iterates, which moves with it and could never fire.
        expected = 6 * (3 if os.environ.get("TEST_DATABASE_URL") else 2)
        self.assertEqual(checked, expected)

    def test_intact_program_spine_create_still_succeeds(self):
        """The control: refuse an INCOHERENT Program spine, not every
        spine. With Team/League/Season agreeing, create still succeeds for
        every spine-validated status and writes its event + audit row."""
        checked = 0
        for label, store in self._stores():
            try:
                for status in _REVIVE_TARGETS:
                    with self.subTest(backend=label, status=status):
                        api = ApiService(store)
                        fx = _fixture(api)
                        res = api.create_season_roster_membership(
                            fx["player"]["id"], fx["ls_id"], fx["team"]["id"],
                            status=status, jersey_number=None, actor_id=ADMIN)
                        self.assertNotIn("error", res, (label, status, res))
                        self.assertEqual(res["status"], status,
                                         (label, status))
                        rows, events, _ = self._surfaces(
                            api, fx["player"]["id"])
                        self.assertEqual((rows, events), (1, 1),
                                         (label, status))
                    checked += 1
            finally:
                self._close(label, store)
        expected = 3 * (3 if os.environ.get("TEST_DATABASE_URL") else 2)
        self.assertEqual(checked, expected)

    # -- PARKED REVIVAL ---------------------------------------------------
    def test_incoherent_program_refuses_every_revival_zero_write(self):
        checked = 0
        for label, store in self._stores():
            try:
                for parked in _PARKED:
                    for key, mode in _PROGRAM_SHAPES:
                        for target in _REVIVE_TARGETS:
                            case = (label, parked, target, key, mode)
                            with self.subTest(backend=label, parked=parked,
                                              target=target, key=key,
                                              mode=mode):
                                # A FRESH fixture per cell, deliberately: one
                                # leaked write must not contaminate the cells
                                # after it, or a falsification run could not
                                # say WHICH cell the guard covers.
                                api = ApiService(store)
                                fx = _fixture(api)
                                m = self._parked(api, fx, parked)
                                self._break(api, fx, key, mode)
                                before = self._membership_surfaces(
                                    api, m["id"])
                                self.assertEqual(before[0], parked, case)
                                res = api.set_season_roster_membership_status(
                                    m["id"], target, actor_id=ADMIN)
                                self.assertIn("error", res, (case, res))
                                self.assertEqual(
                                    res["error"]["details"]["reason"],
                                    "membership_program_mismatch", (case, res))
                                self.assertEqual(
                                    res["error"]["details"]["membership_id"],
                                    m["id"], (case, res))
                                # ZERO WRITE on all three surfaces.
                                self.assertEqual(
                                    self._membership_surfaces(api, m["id"]),
                                    before, case)
                            checked += 1
            finally:
                self._close(label, store)
        # 2 parked x 6 shapes x 3 targets = 36 per store. Pinned against the
        # ENVIRONMENT, the same independent source ParkedRevivalSpineTest
        # uses -- never against len(_PROGRAM_SHAPES) or the store list.
        expected = 36 * (3 if os.environ.get("TEST_DATABASE_URL") else 2)
        self.assertEqual(checked, expected)

    def test_intact_program_spine_revival_still_succeeds(self):
        """The revival control: with the Program leg whole, every parked ->
        revival transition still succeeds and writes its event + audit."""
        checked = 0
        for label, store in self._stores():
            try:
                for parked in _PARKED:
                    for target in _REVIVE_TARGETS:
                        with self.subTest(backend=label, parked=parked,
                                          target=target):
                            api = ApiService(store)
                            fx = _fixture(api)
                            m = self._parked(api, fx, parked)
                            before = self._membership_surfaces(api, m["id"])
                            res = api.set_season_roster_membership_status(
                                m["id"], target, actor_id=ADMIN)
                            self.assertNotIn("error", res,
                                             (label, parked, target, res))
                            self.assertEqual(res["status"], target,
                                             (label, parked, target))
                            self.assertEqual(
                                self._membership_surfaces(api, m["id"]),
                                (target, before[1] + 1, before[2] + 1),
                                (label, parked, target))
                        checked += 1
            finally:
                self._close(label, store)
        expected = 6 * (3 if os.environ.get("TEST_DATABASE_URL") else 2)
        self.assertEqual(checked, expected)

    # -- REACHABILITY -----------------------------------------------------
    def test_no_program_shape_is_unreachable_on_any_store(self):
        """Every one of the six shapes must be REACHABLE on every store, so
        no matrix cell above is silently vacuous.

        A NOT NULL column would make the ``missing`` half of a shape
        unplantable — the way ``league_seasons.league_id`` makes
        ``CreateSpineMissingLeagueTest``'s both-sides-missing case
        Memory-only. All three Program columns are currently NULLABLE on
        both engines (``teams.program_id`` and ``seasons.program_id`` are
        migration 028 renames of the nullable legacy ``league_id``;
        ``leagues.program_id`` is migration 035's ``ADD COLUMN ... TEXT``),
        so all six shapes are reachable everywhere. This asserts that
        explicitly against the live catalogue rather than assuming it: if a
        future migration adds NOT NULL, this fails and NAMES the shape,
        instead of the matrix quietly shrinking."""
        checked = 0
        for label, store in self._stores():
            try:
                if isinstance(store, SqlStore):
                    cur = store.conn.cursor()
                    for key, (table, column) in _PROGRAM_COLUMNS.items():
                        if store.backend == "sqlite":
                            cur.execute(f"PRAGMA table_info({table})")
                            notnull = {r["name"]: r["notnull"]
                                       for r in cur.fetchall()}[column]
                            self.assertEqual(
                                notnull, 0,
                                (label, key, "NOT NULL -- the 'missing' half "
                                 "of this shape is now unreachable"))
                        else:
                            cur.execute(
                                "SELECT is_nullable FROM "
                                "information_schema.columns WHERE "
                                "table_name = %s AND column_name = %s",
                                (table, column))
                            self.assertEqual(
                                cur.fetchone()["is_nullable"], "YES",
                                (label, key, "NOT NULL -- the 'missing' half "
                                 "of this shape is now unreachable"))
                for key, mode in _PROGRAM_SHAPES:
                    with self.subTest(backend=label, key=key, mode=mode):
                        api = ApiService(store)
                        fx = _fixture(api)
                        # _break asserts the planted value actually stuck.
                        self._break(api, fx, key, mode)
                    checked += 1
            finally:
                self._close(label, store)
        expected = 6 * (3 if os.environ.get("TEST_DATABASE_URL") else 2)
        self.assertEqual(checked, expected)

    def test_terminal_refusal_still_precedes_the_program_check(self):
        """A parked row targeting a TERMINAL status still gets the
        unconditional ``terminal_transition_not_authorized`` refusal — NOT a
        Program error — even on an incoherent Program spine. Adding the
        Program clause must not reorder those two."""
        for label, store in self._stores():
            try:
                for target in ("released", "transferred"):
                    with self.subTest(backend=label, target=target):
                        api = ApiService(store)
                        fx = _fixture(api)
                        m = self._parked(api, fx, "inactive")
                        self._break(api, fx, "Team.program_id", "missing")
                        before = self._membership_surfaces(api, m["id"])
                        res = api.set_season_roster_membership_status(
                            m["id"], target, actor_id=ADMIN)
                        self.assertEqual(res["error"]["code"], "forbidden",
                                         (label, target, res))
                        self.assertEqual(
                            res["error"]["details"]["reason"],
                            "terminal_transition_not_authorized",
                            (label, target, res))
                        self.assertEqual(
                            self._membership_surfaces(api, m["id"]), before,
                            (label, target))
            finally:
                self._close(label, store)

    def test_missing_program_keys_are_a_violation_not_agreement(self):
        """MISSING keys are never agreement — ``_missing_or_unequal`` treats
        ``not a or not b`` as a violation, matching the preflight's
        ``_MISSING_OR_UNEQUAL`` rather than the null-safe operators (SQL's
        ``IS DISTINCT FROM`` calls NULL/NULL agreement; so does a plain
        Python ``==``).

        THE THIRD GROUP IS THE ONE THAT DISCRIMINATES, and it was added
        because the first two did not. Falsifier, measured: replacing both
        ``_missing_or_unequal`` calls in
        ``_assert_membership_program_spine`` with plain ``!=`` left the
        ENTIRE rest of this module GREEN, because Python's ``!=`` — unlike
        SQL's ``!=`` — already reports ``None != 'program_1'`` as True, so
        every SINGLE missing key is still caught, and in each ADJACENT PAIR
        the other leg still compares NULL to a real Program and fires. Only
        when ALL THREE keys are missing do both legs degenerate to
        ``None != None`` and an equality-only guard ACCEPTS. That cell is
        the sole load-bearing difference between the two spellings at this
        layer, so it is asserted explicitly; with it present the mutation
        reddens, per store."""
        for label, store in self._stores():
            try:
                for pair in (("Team.program_id", "League.program_id"),
                             ("League.program_id", "Season.program_id"),
                             ("Team.program_id", "League.program_id",
                              "Season.program_id")):
                    with self.subTest(backend=label, pair=pair):
                        api = ApiService(store)
                        fx = _fixture(api)
                        for key in pair:
                            self.assertIsNone(
                                _PROGRAM_KEYS[key](api, fx, None), (label, key))
                        before = self._surfaces(api, fx["player"]["id"])
                        res = api.create_season_roster_membership(
                            fx["player"]["id"], fx["ls_id"], fx["team"]["id"],
                            status="applicant", jersey_number=None,
                            actor_id=ADMIN)
                        self.assertEqual(res["error"]["details"]["reason"],
                                         "membership_program_mismatch",
                                         (label, pair, res))
                        self.assertEqual(
                            self._surfaces(api, fx["player"]["id"]), before,
                            (label, pair))
            finally:
                self._close(label, store)

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
    vs unregister_team_from_season on the SAME registration. Both lock the
    Season row (create via _require_active_season, unregister via the
    identical guard) — the SAME row — so the engine always linearizes them:
    the required outcome is that the two NEVER both succeed in a way that
    leaves an active membership on an unregistered Team; the loser always
    observes the winner's committed state and fails closed with a stable
    reason.

    #205 review round 2 (owner ruling + review finding 3) — REDESIGNED,
    replacing the round-1 approach entirely (a start-of-thread barrier that
    measurably handicapped one side, then a shared-prefix barrier that
    still left a real per-round cost-asymmetry skew, "fixed" by widening 8
    rounds to 80 so both orderings occurred often enough not to flake): two
    test methods, each calling helpers.race_with_forced_order to
    DETERMINISTICALLY force one specific ordering at the shared Season-row
    lock (``get_season_for_update``), once each, rather than launching both
    simultaneously and hoping N rounds sample both orderings. See that
    helper's docstring for why the forcing is exact (not just "probably
    first"): because the lock is held for the whole transaction, the
    LOSER's gated call cannot even be dispatched until the WINNER's entire
    operation — commit included — has already resolved, so which side
    "wins" is no longer a race at all once forced; it is a fact established
    by construction, checked here."""

    def setUp(self):
        self.url = os.environ["TEST_DATABASE_URL"]

    def _create_op(self, fx):
        def op(store):
            api = ApiService(store)
            return api.create_season_roster_membership(
                fx["player"]["id"], fx["ls_id"], fx["team"]["id"],
                status="applicant", jersey_number=None, actor_id=ADMIN)
        return op

    def _unregister_op(self, fx):
        def op(store):
            api = ApiService(store)
            return api.unregister_team_from_season(
                fx["reg"]["id"], actor_id=ADMIN)
        return op

    def test_create_forced_first_wins_unregister_loses(self):
        fx = _pg_fixture(self.url)
        create_res, unregister_res = race_with_forced_order(
            self.url, "get_season_for_update",
            self._create_op(fx), self._unregister_op(fx))
        # Winner: create committed a live membership.
        self.assertNotIn("error", create_res, create_res)
        # Loser: unregister's under-lock re-read sees that FRESH, committed
        # membership and refuses — the exact stable reason, zero write.
        self.assertIn("error", unregister_res, unregister_res)
        self.assertEqual(unregister_res["error"]["details"]["reason"],
                         "team_has_live_memberships", unregister_res)
        checker = SqlStore(self.url)
        try:
            reg = checker.get_season_team_registration(fx["reg"]["id"])
            self.assertTrue(reg.active)
            memberships = [
                m for m in checker.all_season_roster_memberships()
                if m.player_id == fx["player"]["id"]
                and not m.status.is_terminal]
            self.assertEqual(len(memberships), 1, memberships)
            events = checker.events_for_membership(memberships[0].id)
            self.assertEqual([e.action for e in events], ["created"])
        finally:
            checker.close()

    def test_unregister_forced_first_wins_create_loses(self):
        fx = _pg_fixture(self.url)
        unregister_res, create_res = race_with_forced_order(
            self.url, "get_season_for_update",
            self._unregister_op(fx), self._create_op(fx))
        # Winner: unregister committed — the registration is now inactive.
        self.assertNotIn("error", unregister_res, unregister_res)
        # Loser: create's under-lock re-read sees the FRESH, committed
        # inactive registration and refuses — the exact stable reason, zero
        # write (never a dangling active membership on an unregistered Team).
        self.assertIn("error", create_res, create_res)
        self.assertEqual(create_res["error"]["details"]["reason"],
                         "team_not_registered", create_res)
        checker = SqlStore(self.url)
        try:
            reg = checker.get_season_team_registration(fx["reg"]["id"])
            self.assertFalse(reg.active)
            memberships = [
                m for m in checker.all_season_roster_memberships()
                if m.player_id == fx["player"]["id"]]
            self.assertEqual(memberships, [])
        finally:
            checker.close()


@unittest.skipUnless(os.environ.get("TEST_DATABASE_URL"), _PG_SKIP)
class CreateVsTransferRaceTest(unittest.TestCase):
    """Real TWO-CONNECTION PostgreSQL race: create_season_roster_membership
    vs transfer_team_to_league moving the SAME Team to a different League,
    starting from NO existing membership (so, unlike reactivating an
    ALREADY-live row, either side can genuinely commit first). Both lock the
    Team row (create via get_team_for_update; transfer via the identical
    call) — the SAME row — so the engine linearizes them: required outcome
    is never an active membership whose league_season_id disagrees with the
    Team's CURRENT League, regardless of which commits first.

    #205 review round 2 (owner ruling + review finding 3) — REDESIGNED: two
    test methods, each calling helpers.race_with_forced_order to
    DETERMINISTICALLY force one ordering at the shared Team-row lock
    (``get_team_for_update``), once each, instead of a single simultaneous-
    start barrier sampled across 10 rounds. See CreateVsUnregisterRaceTest's
    docstring (this file) and race_with_forced_order's for why the forcing
    is exact, not probabilistic."""

    def setUp(self):
        self.url = os.environ["TEST_DATABASE_URL"]

    def _create_op(self, fx):
        def op(store):
            api = ApiService(store)
            return api.create_season_roster_membership(
                fx["player"]["id"], fx["ls_id"], fx["team"]["id"],
                status="applicant", jersey_number=None, actor_id=ADMIN)
        return op

    def _transfer_op(self, fx):
        def op(store):
            api = ApiService(store)
            return api.transfer_team_to_league(
                fx["team"]["id"], fx["other_league_id"], actor_id=ADMIN)
        return op

    def _assert_team_league_coherent(self, fx):
        checker = SqlStore(self.url)
        try:
            team = checker.get_team(fx["team"]["id"])
            memberships = [m for m in checker.all_season_roster_memberships()
                          if m.player_id == fx["player"]["id"]]
            for m in memberships:
                ls = checker.get_league_season(m.league_season_id)
                self.assertEqual(
                    ls.league_id, team.league_id,
                    (fx, "Team<->LeagueSeason League disagreement", m.id))
            return memberships
        finally:
            checker.close()

    def test_create_forced_first_wins_transfer_loses(self):
        fx = _pg_fixture(self.url)
        create_res, transfer_res = race_with_forced_order(
            self.url, "get_team_for_update",
            self._create_op(fx), self._transfer_op(fx))
        # Winner: create committed a live membership on the Team's
        # (unchanged) League.
        self.assertNotIn("error", create_res, create_res)
        # Loser: transfer's under-lock candidate scan sees that FRESH,
        # committed membership blocking the move — the exact stable
        # reason, zero Team/registration mutation.
        self.assertIn("error", transfer_res, transfer_res)
        self.assertEqual(transfer_res["error"]["details"]["reason"],
                         "team_transfer_strands_memberships", transfer_res)
        memberships = self._assert_team_league_coherent(fx)
        self.assertEqual(len(memberships), 1, memberships)

    def test_transfer_forced_first_wins_create_loses(self):
        fx = _pg_fixture(self.url)
        transfer_res, create_res = race_with_forced_order(
            self.url, "get_team_for_update",
            self._transfer_op(fx), self._create_op(fx))
        # Winner: transfer committed — Team.league_id now names the NEW
        # League.
        self.assertNotIn("error", transfer_res, transfer_res)
        # Loser: create's under-lock re-read of the Team sees the FRESH,
        # committed League change; the (unchanged) league_season_id this
        # create still targets now names the OLD League — the coherence
        # check fires before create even reaches the registration lookup
        # (create_season_roster_membership checks League agreement first).
        self.assertIn("error", create_res, create_res)
        self.assertEqual(create_res["error"]["details"]["reason"],
                         "membership_league_mismatch", create_res)
        memberships = self._assert_team_league_coherent(fx)
        self.assertEqual(memberships, [])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
