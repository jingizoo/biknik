"""PART A — the ENROLLMENT-side durable snapshot (#205, owner comment 5391127041).

THE RULING

    "Choose the durable enrollment-side snapshot. When ``enroll_substitute``
    accepts the exact game membership context, record that context's
    ``team_id`` on the enrollment in the same transaction. For an ENROLLED
    row, that stored side — not a later live membership lookup — is the
    Coach-authorization authority for withdrawal. Once OFFERED, the existing
    offer-owner snapshot remains authoritative for that phase."

WHAT WAS MEASURED RED, at head 22bd6de, on Memory, SQLite and real
PostgreSQL::

    after enroll : status='enrolled' team_id=None    position='forward'
    after offer  : status='offered'  team_id='team_1' (HOME='team_1')

``SubstituteEnrollment.team_id`` was written ONLY by ``offer_substitute``
(migration 060), so an ENROLLED-but-never-OFFERED row named NO side at all.
``withdraw_substitute`` therefore had nothing durable to authorize a Coach
against, and the only remaining answer would have been a LIVE membership
lookup — the exact substitution the ruling forbids, because it hands the row
to whichever coach the player belongs to NOW rather than to the one who owns
it.

NO SCHEMA WORK WAS NEEDED. ``substitute_enrollments.team_id`` is already a
nullable column (migration 060) and the SQL store's insert path is
spec-driven (``SqlStore._insert`` writes every column of the dataclass), so
the value lands on INSERT on both SQL backends with no migration at all. The
ruling's forward-only requirement is met by construction: nothing here
backfills, and legacy rows stay NULL.

WHAT THIS FILE PINS

 1. the snapshot is taken from the SAME resolved context the eligibility gate
    accepted — proven with the "Mover" shape, whose permanent pointer names a
    DIFFERENT team than its seasonal membership, so a pointer-derived value
    could not pass;
 2. it is written ON THE INSERT, inside ``enroll_substitute``'s own
    transaction — proven by a store spy that captures the value AT THE MOMENT
    ``add_substitute`` is called, which a later second write could not satisfy;
 3. the OFFERED phase is unchanged: ``offer_substitute`` replaces the value
    with the side IT validated, and that offer-owner snapshot stays
    authoritative (the standing #205 blocker-3 contract ``decline_substitute``
    depends on);
 4. UNBOUND games keep the permanent-pointer context, so exhibitions record
    the only side they have ever had;
 5. NO BACKFILL: a legacy NULL row is never repaired by any subsequent
    substitute transition.

TRI-STORE, PROVEN. ``_stores`` yields Memory, SQLite and — when
TEST_DATABASE_URL is set — real PostgreSQL; ``_assert_backend`` PROVES each
one rather than trusting the env var, and ``_assert_ran`` fails a loop that
silently covered fewer backends than were configured. A SKIP IS NOT A PASS.
"""

import contextlib
import os
import unittest

from helpers import BACKEND, FakeClock, fresh_sql_store  # noqa: F401
from test_substitute_membership_cutover import ADMIN, _at

from hockey_scheduler.api import ApiService
from hockey_scheduler.domain import Player, Position
from hockey_scheduler.store import InMemoryStore, SqlStore

_PG_SKIP = ("PostgreSQL not configured (TEST_DATABASE_URL); this assertion "
            "is NOT covered on the backend whose durability it is about.")


class _SnapshotFixture:
    """One Program/Season/LeagueSeason, three teams, a bound HOME-vs-AWAY
    game with an open skater slot, plus an unbound exhibition."""

    def _stores(self):
        yield "memory", InMemoryStore()
        yield "sqlite", SqlStore(":memory:")
        url = os.environ.get("TEST_DATABASE_URL")
        if url:
            yield "postgres", fresh_sql_store(url)

    def _assert_backend(self, label, store):
        if label == "postgres":
            self.assertIsInstance(store, SqlStore, label)
            self.assertEqual(store.backend, "postgres", store.backend)
        elif label == "sqlite":
            self.assertIsInstance(store, SqlStore, label)
            self.assertEqual(store.backend, "sqlite", store.backend)
        else:
            self.assertIsInstance(store, InMemoryStore, label)

    def _close(self, label, store):
        if isinstance(store, SqlStore):
            if label == "postgres":
                store.reset_schema()
            store.close()

    def _assert_ran(self, ran, banner):
        expected = {"memory", "sqlite"}
        if os.environ.get("TEST_DATABASE_URL"):
            expected.add("postgres")
        else:
            print(f"\n[{banner}] " + _PG_SKIP)
        self.assertEqual(set(ran), expected, sorted(ran))

    def _build(self, store):
        api = ApiService(store)
        api.roster.clock = FakeClock()
        org = api.create_organization("Org", "O", actor_id=ADMIN)
        program = api.create_program("Prog", operator_organization_id=org["id"],
                                     actor_id=ADMIN)
        season = api.create_season(program["id"], "Fall 2026", actor_id=ADMIN)
        league = api.create_league(season["id"], "Elite", actor_id=ADMIN)
        club = api.create_club("Club", actor_id=ADMIN)
        teams = {}
        for name in ("Home", "Away", "Third"):
            t = api.create_team(club["id"], None, name, actor_id=ADMIN,
                                league_id=league["id"])
            api.register_team_for_season(season["id"], t["id"], actor_id=ADMIN,
                                         league_id=league["id"])
            teams[name.lower()] = t
        venue = api.create_venue("V", organization_id=org["id"],
                                 league_id=program["id"], actor_id=ADMIN)
        api.grant_season_venue_access(season["id"], venue["id"], actor_id=ADMIN)
        rink = api.create_rink(venue["id"], "R", actor_id=ADMIN)
        slot = api.create_ice_slot(rink["id"], _at(18).isoformat(),
                                   _at(19).isoformat(), "game", actor_id=ADMIN)
        game = api.create_game(season["id"], None, teams["home"]["id"],
                               teams["away"]["id"], slot["id"],
                               target_goalies=0, target_skaters=2,
                               actor_id=ADMIN, league_id=league["id"])
        self.assertNotIn("error", game, game)
        self.assertTrue(game["league_season_id"], game)
        api.publish_game(game["id"], actor_id=ADMIN)
        return {"api": api, "season": season, "league": league, "rink": rink,
                "game": game, "gid": game["id"],
                "ls_id": game["league_season_id"],
                "home": teams["home"]["id"], "away": teams["away"]["id"],
                "third": teams["third"]["id"]}

    def _exhibition(self, fx):
        api = fx["api"]
        slot = api.create_ice_slot(fx["rink"]["id"], _at(20).isoformat(),
                                   _at(21).isoformat(), "game", actor_id=ADMIN)
        ex = api.create_game(fx["season"]["id"], None, fx["home"], fx["away"],
                             slot["id"], target_goalies=0, target_skaters=2,
                             actor_id=ADMIN, league_id=fx["league"]["id"],
                             game_type="exhibition")
        self.assertNotIn("error", ex, ex)
        # The defining shape of the unbound branch.
        self.assertIsNone(ex["league_season_id"], ex)
        api.publish_game(ex["id"], actor_id=ADMIN)
        return ex

    def _player(self, fx, team_id, name):
        p = fx["api"].create_player(team_id, name, "forward", actor_id=ADMIN)
        self.assertNotIn("error", p, p)
        return p

    def _mover(self, fx, name="Mo Mover"):
        """Permanent pointer on THIRD, seasonal membership on HOME.

        The whole point: a snapshot taken from ``Player.team_id`` would
        record THIRD, so only a value taken from the resolved
        GameMembershipContext can pass.
        """
        api = fx["api"]
        p = Player(id=api.store.next_id("player"), team_id=fx["third"],
                   name=name, position=Position.FORWARD)
        api.store.add_player(p)
        m = api.create_season_roster_membership(
            p.id, fx["ls_id"], fx["home"], status="active", actor_id=ADMIN)
        self.assertNotIn("error", m, m)
        return {"id": p.id, "name": name}

    def _reread(self, fx, pid, gid=None):
        """Read the row back THROUGH THE STORE, so a real database round trip
        (and its column mapping) is what is asserted, never an in-process
        object the service happens to still hold."""
        return fx["api"].store.substitute_for_player(gid or fx["gid"], pid)

    @contextlib.contextmanager
    def _insert_values(self, store):
        """Capture ``team_id`` AT THE MOMENT ``add_substitute`` is called.

        A SNAPSHOT TAKEN AFTER THE CALL CANNOT PROVE WHAT IS ASSERTED HERE.
        The ruling requires the side to be recorded "in the same
        transaction" as the enrollment; reading the row afterwards would be
        satisfied just as well by a second write, or by a write in a later
        transaction. Only a spy on the INSERT itself can tell the two apart.
        """
        seen = []
        original = store.add_substitute

        def spy(sub):
            seen.append((sub.status.value, sub.team_id, sub.position.value))
            return original(sub)

        store.add_substitute = spy
        try:
            yield seen
        finally:
            del store.add_substitute


class EnrollmentRecordsTheAcceptedContextsSide(_SnapshotFixture,
                                               unittest.TestCase):

    def test_enroll_snapshots_the_contexts_team_not_the_pointer(self):
        ran = []
        for label, store in self._stores():
            try:
                self._assert_backend(label, store)
                store.clear_all_data()
                fx = self._build(store)
                mover = self._mover(fx)
                with self.subTest(backend=label):
                    sub = fx["api"].enroll_substitute(fx["gid"], mover["id"],
                                                      ADMIN)
                    self.assertNotIn("error", sub, sub)
                    row = self._reread(fx, mover["id"])
                    # THE side the eligibility gate accepted...
                    self.assertEqual(row.team_id, fx["home"],
                                     (label, row.team_id))
                    # ...and emphatically NOT the permanent pointer, which
                    # still names THIRD.
                    self.assertEqual(
                        fx["api"].store.get_player(mover["id"]).team_id,
                        fx["third"], label)
                    self.assertNotEqual(row.team_id, fx["third"], label)
                ran.append(label)
            finally:
                self._close(label, store)
        self._assert_ran(ran, "ENROLL SNAPSHOT / MOVER")

    def test_the_side_is_written_on_the_insert_itself(self):
        ran = []
        for label, store in self._stores():
            try:
                self._assert_backend(label, store)
                store.clear_all_data()
                fx = self._build(store)
                mover = self._mover(fx)
                with self.subTest(backend=label):
                    with self._insert_values(fx["api"].store) as seen:
                        res = fx["api"].enroll_substitute(
                            fx["gid"], mover["id"], ADMIN)
                    self.assertNotIn("error", res, res)
                    # Exactly one insert, and it already carried the side —
                    # so the value cannot have come from a later write.
                    self.assertEqual(len(seen), 1, seen)
                    self.assertEqual(seen[0], ("enrolled", fx["home"],
                                               "forward"), (label, seen))
                ran.append(label)
            finally:
                self._close(label, store)
        self._assert_ran(ran, "ENROLL SNAPSHOT / ON INSERT")

    def test_unbound_game_records_the_permanent_pointers_side(self):
        """For an exhibition the context IS the permanent pointer (see
        ``resolve_membership_context``), so the recorded side is the only one
        that has ever existed there. The unbound branch is preserved, not
        widened."""
        ran = []
        for label, store in self._stores():
            try:
                self._assert_backend(label, store)
                store.clear_all_data()
                fx = self._build(store)
                ex = self._exhibition(fx)
                p = self._player(fx, fx["home"], "Ella Exhibition")
                with self.subTest(backend=label):
                    res = fx["api"].enroll_substitute(ex["id"], p["id"], ADMIN)
                    self.assertNotIn("error", res, res)
                    row = self._reread(fx, p["id"], gid=ex["id"])
                    self.assertEqual(row.team_id, fx["home"],
                                     (label, row.team_id))
                ran.append(label)
            finally:
                self._close(label, store)
        self._assert_ran(ran, "ENROLL SNAPSHOT / UNBOUND")


class TheOfferPhaseKeepsItsOwnSnapshot(_SnapshotFixture, unittest.TestCase):
    """"Once OFFERED, the existing offer-owner snapshot remains
    authoritative for that phase." The enroll-time value is a starting
    point for the ENROLLED phase only; ``offer_substitute`` still overwrites
    it with the side IT validated, which is what the standing #205 blocker-3
    contract (``decline_substitute``'s audience) depends on."""

    def test_offer_replaces_the_enroll_time_side_with_the_offer_owner(self):
        ran = []
        for label, store in self._stores():
            try:
                self._assert_backend(label, store)
                store.clear_all_data()
                fx = self._build(store)
                mover = self._mover(fx)
                with self.subTest(backend=label):
                    api = fx["api"]
                    api.enroll_substitute(fx["gid"], mover["id"], ADMIN)
                    self.assertEqual(self._reread(fx, mover["id"]).team_id,
                                     fx["home"], label)
                    res = api.offer_substitute(fx["gid"], mover["id"], ADMIN)
                    self.assertNotIn("error", res, res)
                    row = self._reread(fx, mover["id"])
                    self.assertEqual(row.status.value, "offered", label)
                    # Same side here (the membership did not move), but the
                    # value is now the OFFER's, written by offer_substitute.
                    self.assertEqual(row.team_id, fx["home"], label)
                ran.append(label)
            finally:
                self._close(label, store)
        self._assert_ran(ran, "OFFER PHASE SNAPSHOT")


class LegacyNullAttributionIsNeverBackfilled(_SnapshotFixture,
                                             unittest.TestCase):
    """"Legacy enrollment attribution must remain honest: do not backfill
    from ``Player.team_id`` or current membership."

    The on-disk legacy shape is modelled the way migration 060 actually
    leaves it — the column present and NULL — and nothing in the substitute
    workflow may quietly repair it."""

    def _legacy_null(self, fx, pid):
        store = fx["api"].store
        with store.transaction():
            row = store.substitute_for_player(fx["gid"], pid)
            row.team_id = None
            store.save_substitute(row)
        reread = store.substitute_for_player(fx["gid"], pid)
        # The NULL must survive the round trip on a real database, or this
        # shape would be testing an in-process object.
        self.assertIsNone(reread.team_id)
        return reread

    def test_withdraw_does_not_repair_a_legacy_null(self):
        ran = []
        for label, store in self._stores():
            try:
                self._assert_backend(label, store)
                store.clear_all_data()
                fx = self._build(store)
                p = self._player(fx, fx["home"], "Lena Legacy")
                with self.subTest(backend=label):
                    api = fx["api"]
                    api.enroll_substitute(fx["gid"], p["id"], ADMIN)
                    self._legacy_null(fx, p["id"])
                    # An unscoped operator may still withdraw it (its
                    # authority never came from this column) — and the
                    # withdrawal must not invent a side on the way through.
                    res = api.withdraw_substitute(fx["gid"], p["id"], ADMIN)
                    self.assertNotIn("error", res, res)
                    row = self._reread(fx, p["id"])
                    self.assertEqual(row.status.value, "withdrawn", label)
                    self.assertIsNone(row.team_id, (label, row.team_id))
                ran.append(label)
            finally:
                self._close(label, store)
        self._assert_ran(ran, "LEGACY NULL / NO BACKFILL")


if __name__ == "__main__":
    unittest.main()
