"""PR #427 blocker 5379031499 — the LeagueSeason's Season is the ONE authority.

THE DEFECT THIS FILE PINS (owner comment 5379031499, and the 2026-08-23
implementation ruling that followed).

    "the same authority chain still fails open when the bound Game's own
    denormalized Season is missing or disagrees with its LeagueSeason. […] The
    reason is split authority: ``_guard_active_season`` checks and locks only
    ``game.season_id``, so NULL skips the archive guard and S2 locks the wrong
    Season; ``_resolve_context_with_reason`` validates ``membership.season_id``
    against ``LS1.season_id`` but never validates ``game.season_id`` against
    it."

REPRODUCED RED at head 57f9107 on Memory, SQLite and real PostgreSQL, both
variants, with S1 archived THROUGH THE REAL FACADE (``api.archive_season``
performs no dependency scan, so this needs no planted lifecycle state):

    build a valid regular Game on LS1/S1; store-write ``game.season_id`` to
    NULL (variant A) or to a sibling Season S2 (variant B), leaving
    ``game.league_season_id = LS1``; archive S1.

    => resolve_membership_context returned the HOME context with
       ``ctx.season.id == S1``;
    => substitute_block_reason -> None, list_substitute_opportunities -> [game],
       list_addable_players -> all three players;
    => enroll -> offer -> accept ALL succeeded, writing an ACCEPTED
       SubstituteEnrollment and an ACCEPTED GameRosterEntry;
    => Coach-add succeeded and auto_build_roster seated the remainder;
       8 audit rows against an ARCHIVED competition.

THE DECISIVE CONTROL, and the reason "it fails open" understates it: with
``game.season_id = S2`` and BOTH Seasons archived, the refusal NAMED S2 while
``ctx.season.id`` was S1 all along. The guard was not merely skipping a check,
it was locking and judging a DIFFERENT ROW than the one the resolution used —
so two writers on the same competition shared no serialization point with each
other or with ``archive_season``.

THE FIX, per the ruling, is ONE shared guard
(:func:`season_guard.guard_game_season`) that both families call, with a FIXED
precedence: resolve the LeagueSeason; a dangling one is
``regular_game_missing_league_season``; LOCK and archive-check the Season IT
names FIRST (``season_archived``, naming the CANONICAL id); and only when that
Season is ACTIVE compare ``game.season_id`` unconditionally
(``game_league_season_mismatch``). No new reason codes were minted.

WHAT EACH SECTION PROVES

 1. the READ surface — resolver, spine reason, block reason, opportunity list,
    addable list, batch resolver — fails closed on both variants;
 2. every BOUND MUTATION SURFACE of RosterService fails closed with EXACT ZERO
    write ATTEMPTS (``_write_attempts``, not a snapshot diff: these methods are
    ``@_transactional``, so a diff cannot tell "refused first" from "rolled
    back after writing");
 3. a representative spread of SetupService's ELEVEN ``_guard_game_season``
    callers fails closed too — the second guard family, which had its own copy
    of the same defect;
 4. THE PRECEDENCE: archived-canonical outranks mismatch, and the reported id
    is the canonical one. Pinned in both directions so it cannot silently
    invert;
 5. TRI-STORE FUNCTIONAL PARITY — the same input compared ACROSS backends,
    not merely asserted separately on each;
 6. DETERMINISTIC ARCHIVE RACES in BOTH commit orders, proving the canonical
    Season row is the shared lock (instrumented-store hook on all three
    backends; ``pg_stat_activity`` and ``FOR UPDATE NOWAIT`` probes from a
    third connection on PostgreSQL for the positive proof);
 7. EXHIBITIONS keep their own Season and still refuse when it is archived —
    the unbound branch the ruling requires preserved;
 8. the two OTHER call sites the same defect had reached:
    ``_revalidate_game_participation``'s season comparison and the batch
    draft-guard helper.

TRI-STORE, PROVEN. ``_stores`` yields Memory, SQLite and — when
TEST_DATABASE_URL is set — real PostgreSQL; ``_assert_backend`` PROVES each one
rather than trusting the env var, and ``_assert_matrix_ran`` fails a loop that
silently covered fewer backends than were configured. A SKIP IS NOT A PASS.
"""

import contextlib
import copy
import os
import threading
import unittest

from helpers import BACKEND, FakeClock, fresh_sql_store  # noqa: F401
from test_substitute_membership_cutover import ADMIN, _at

from hockey_scheduler.api import ApiService
from hockey_scheduler.domain import Player, Position
from hockey_scheduler.domain import (Game, OfficialAssignment, OfficialRole,
                                     Role)
from hockey_scheduler.domain.enums import SeasonStatus
from hockey_scheduler.domain.errors import ValidationError
from hockey_scheduler.services import season_guard
from hockey_scheduler.store import InMemoryStore, SqlStore

_PG_SKIP = ("PostgreSQL not configured (TEST_DATABASE_URL); this assertion "
            "is NOT covered on the backend whose row locks it is about.")

# The two ways a bound Game's denormalized Season can disagree with the
# LeagueSeason that actually owns it. Both are reachable by a direct store
# write / a restore, and `games.season_id` is nullable TEXT with no FK and no
# CHECK, so neither is hypothetical.
VARIANTS = ("null", "sibling")


def _announce_pg_skip(banner):
    print(f"\n[{banner}] " + _PG_SKIP)


class _Authority:
    """The fixture every section shares: ONE Program carrying TWO sibling
    Seasons, a LeagueSeason on S1, three teams registered into S1, and a
    published regular Game bound to that LeagueSeason.

    The SIBLING Season is what makes the drift variant meaningful: S2 is a
    real, valid, same-Program Season, so a Game pointed at it is not obviously
    corrupt to any check that merely resolves `game.season_id` and finds a row.
    """

    # -- backends --------------------------------------------------------
    def _stores(self):
        yield "memory", InMemoryStore()
        yield "sqlite", SqlStore(":memory:")
        url = os.environ.get("TEST_DATABASE_URL")
        if url:
            yield "postgres", fresh_sql_store(url)

    def _assert_backend(self, label, store):
        """PROVE the backend. ``skipUnless`` on the env var proves only that a
        URL was SET, never that a statement reached PostgreSQL."""
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

    def _assert_matrix_ran(self, ran, expected_cases):
        """The loop is never silently empty, PostgreSQL is never silently
        absent when it WAS configured, and every case ran on every backend."""
        backends = {b for b, _c in ran}
        expected = {"memory", "sqlite"}
        if os.environ.get("TEST_DATABASE_URL"):
            expected.add("postgres")
        else:
            _announce_pg_skip("LEAGUE-SEASON AUTHORITY MATRIX")
        self.assertEqual(backends, expected, sorted(backends))
        for backend in expected:
            cases = {c for b, c in ran if b == backend}
            self.assertEqual(cases, set(expected_cases),
                             (backend, sorted(cases)))

    # -- fixture ---------------------------------------------------------
    def _build(self, store, target_skaters=3):
        api = ApiService(store)
        api.roster.clock = FakeClock()
        org = api.create_organization("Org", "O", actor_id=ADMIN)
        program = api.create_program(
            "Prog", operator_organization_id=org["id"], actor_id=ADMIN)
        s1 = api.create_season(program["id"], "Fall 2026", actor_id=ADMIN)
        # The SIBLING: same Program, equally real, and the row a drifted
        # `game.season_id` points at.
        s2 = api.create_season(program["id"], "Spring 2027", actor_id=ADMIN)
        league = api.create_league(s1["id"], "Elite", actor_id=ADMIN)
        club = api.create_club("Club", actor_id=ADMIN)
        teams = {}
        for name in ("Home", "Away", "Third"):
            t = api.create_team(club["id"], None, name, actor_id=ADMIN,
                                league_id=league["id"])
            api.register_team_for_season(s1["id"], t["id"], actor_id=ADMIN,
                                         league_id=league["id"])
            teams[name.lower()] = t
        venue = api.create_venue("V", organization_id=org["id"],
                                 league_id=program["id"], actor_id=ADMIN)
        api.grant_season_venue_access(s1["id"], venue["id"], actor_id=ADMIN)
        rink = api.create_rink(venue["id"], "R", actor_id=ADMIN)
        slot = api.create_ice_slot(rink["id"], _at(18).isoformat(),
                                   _at(19).isoformat(), "game", actor_id=ADMIN)
        game = api.create_game(s1["id"], None, teams["home"]["id"],
                               teams["away"]["id"], slot["id"],
                               target_goalies=0, target_skaters=target_skaters,
                               actor_id=ADMIN, league_id=league["id"])
        assert "error" not in game, game
        # The premise: production DERIVES both columns from one another, so a
        # freshly built Game is always coherent. Everything below has to break
        # that coherence deliberately.
        assert game["league_season_id"], game
        assert api.store.get_game(game["id"]).season_id == s1["id"], game
        api.publish_game(game["id"], actor_id=ADMIN)
        return {"api": api, "program": program, "s1": s1, "s2": s2,
                "league": league, "teams": teams, "game": game,
                "gid": game["id"], "ls_id": game["league_season_id"],
                "home": teams["home"]["id"], "away": teams["away"]["id"],
                "third": teams["third"]["id"], "rink": rink}

    def _player(self, fx, name, team=None):
        """A player whose PERMANENT pointer names THIRD but whose seasonal
        membership is on HOME — the "Mover" shape, so no assertion here can be
        satisfied by the permanent pointer accidentally agreeing."""
        api = fx["api"]
        p = Player(id=api.store.next_id("player"),
                   team_id=team or fx["third"], name=name,
                   position=Position.FORWARD)
        api.store.add_player(p)
        m = api.create_season_roster_membership(
            p["id"] if isinstance(p, dict) else p.id, fx["ls_id"], fx["home"],
            status="active", actor_id=ADMIN)
        assert "error" not in m, m
        return {"id": p.id, "name": name}

    def _exhibition(self, fx):
        api = fx["api"]
        slot = api.create_ice_slot(fx["rink"]["id"], _at(20).isoformat(),
                                   _at(21).isoformat(), "game",
                                   actor_id=ADMIN)
        ex = api.create_game(fx["s1"]["id"], None, fx["home"], fx["away"],
                             slot["id"], actor_id=ADMIN,
                             league_id=fx["league"]["id"],
                             game_type="exhibition")
        assert "error" not in ex, ex
        # The defining shape of the UNBOUND branch: no LeagueSeason, a real
        # Season of its own.
        assert ex["league_season_id"] is None, ex
        assert api.store.get_game(ex["id"]).season_id == fx["s1"]["id"], ex
        api.publish_game(ex["id"], actor_id=ADMIN)
        return ex

    # -- the corruption --------------------------------------------------
    def _drift(self, fx, variant, game_id=None):
        """The owner's exact store-write: retarget ``game.season_id`` while
        leaving ``game.league_season_id`` naming LS1. Returns the new value."""
        api = fx["api"]
        gid = game_id or fx["gid"]
        target = None if variant == "null" else fx["s2"]["id"]
        with api.store.transaction():
            g = api.store.get_game(gid)
            g.season_id = target
            api.store.save_game(g)
        g = api.store.get_game(gid)
        assert g.season_id == target, g
        assert g.league_season_id == fx["ls_id"], g
        return target

    def _archive(self, fx, season):
        """Archive through the REAL FACADE. ``api.archive_season`` runs no
        dependency scan, so this is a route a real operator has and the test
        never has to plant the archived state at the store."""
        r = fx["api"].archive_season(season["id"], reason="season over",
                                     actor_id=ADMIN)
        assert "error" not in r, r
        assert (fx["api"].store.get_season(season["id"]).status
                == SeasonStatus.ARCHIVED)

    # -- write-ATTEMPT spy (identical discipline to
    #    test_roster_attribution_durability's) -----------------------------
    _WRITE_PREFIXES = ("save_", "add_", "upsert_", "insert_", "update_",
                       "delete_", "remove_", "clear_", "next_id")

    @contextlib.contextmanager
    def _write_attempts(self, store):
        """Record every STORE WRITE METHOD CALLED, whether or not it survived.

        A SNAPSHOT DIFF CANNOT PROVE WHAT IS ASSERTED HERE. Every method under
        test is ``@_transactional``, so a guard placed AFTER the first write
        still leaves an empty diff — the raise rolls it back on Memory, SQLite
        and PostgreSQL alike. The ruling requires the refusal to happen BEFORE
        any mutation, which is an ORDERING property, and only a spy on the
        attempts can see it.

        Patched on the INSTANCE and restored in ``finally``; the number of
        methods actually wrapped is asserted so a rename that empties the
        prefix list fails loudly instead of turning this into a spy that
        watches nothing."""
        calls = []
        patched = {}
        for name in dir(type(store)):
            if not name.startswith(self._WRITE_PREFIXES):
                continue
            attr = getattr(store, name, None)
            if not callable(attr):
                continue
            patched[name] = attr

            def _spy(*a, _n=name, _f=attr, **kw):
                calls.append(_n)
                return _f(*a, **kw)

            setattr(store, name, _spy)
        self.assertGreater(len(patched), 20, sorted(patched))
        try:
            yield calls
        finally:
            for name in patched:
                delattr(store, name)

    def _writes(self, fx, gid=None):
        """The four write classes the owner names, as comparable IDENTITY
        tuples — never bare counts, which a same-cardinality row SWAP would
        satisfy."""
        store = fx["api"].store
        gid = gid or fx["gid"]
        return {
            "substitutes": sorted((s.id, s.player_id, s.status.value)
                                  for s in store.substitutes_for_game(gid)),
            "roster": sorted((e.id, e.player_id, e.status.value)
                             for e in store.roster_for_game(gid)),
            "availability": sorted((a.id, a.player_id)
                                   for a in store.availability_for_game(gid)),
            "audit": sorted((a.id, a.action.value)
                            for a in store.audit_for_game(gid)),
            "notifications": sorted((n.id, n.type.value) for n in
                                    store.notifications_for_game(gid)),
        }

    def _error(self, res):
        self.assertIn("error", res, res)
        return res["error"]

    def _reason(self, res):
        return (self._error(res).get("details") or {}).get("reason")


# ======================================================================
# 1. THE READ SURFACE
# ======================================================================
class TheReadSurfaceFailsClosedOnGameSeasonDrift(_Authority,
                                                 unittest.TestCase):
    """Every read the blocker names — "the whole READ surface fails open too"
    — answers from ONE resolution, so closing that resolution closes all of
    them at once. Asserted individually anyway: a shared root is a reason to
    expect agreement, not a licence to test only one caller.

    These reads take NO Season lock and make NO archive check, deliberately:
    an archived Season is read-only, not invisible, and closing the resolver
    on lifecycle state would blank out every historical roster view. What is
    checked here is IDENTITY — does the Game agree with its own competition —
    which is exactly the half a read can and must answer. The Seasons are left
    ACTIVE throughout this section so that separation is the thing under
    test."""

    def test_the_resolver_and_every_read_off_it_fail_closed(self):
        ran = []
        for label, store in self._stores():
            try:
                self._assert_backend(label, store)
                for variant in VARIANTS:
                    store.clear_all_data()
                    fx = self._build(store)
                    api = fx["api"]
                    p = self._player(fx, "Mover")
                    game = api.store.get_game(fx["gid"])
                    player = api.store.get_player(p["id"])

                    # THE CONTROL: coherent, this player resolves and every
                    # read surfaces them. Without it a "fails closed" assertion
                    # is satisfied by a fixture that never worked.
                    ctx = api.roster.resolve_membership_context(game, player)
                    self.assertIsNotNone(ctx, (label, variant))
                    self.assertEqual(ctx.season.id, fx["s1"]["id"])
                    self.assertIsNone(api.roster.substitute_block_reason(
                        p["id"], fx["gid"]), (label, variant))
                    self.assertEqual(
                        [g.id for g in
                         api.roster.list_substitute_opportunities(p["id"])],
                        [fx["gid"]], (label, variant))
                    self.assertEqual(
                        [r["player_id"] for r in
                         api.roster.list_addable_players(
                             fx["gid"], fx["home"])],
                        [p["id"]], (label, variant))

                    self._drift(fx, variant)
                    game = api.store.get_game(fx["gid"])

                    with self.subTest(backend=label, variant=variant):
                        # the resolver itself
                        self.assertIsNone(
                            api.roster.resolve_membership_context(
                                game, player))
                        self.assertIsNone(
                            api.roster.resolve_membership(game, player))
                        self.assertIsNone(
                            api.roster.team_for_game(game, player))
                        # …and it NAMES the edge, reusing the existing code
                        self.assertEqual(
                            api.roster.membership_spine_break_reason(
                                game, player),
                            season_guard.GAME_LEAGUE_SEASON_MISMATCH)
                        self.assertEqual(
                            api.roster.seating_block_reason(game, player),
                            season_guard.GAME_LEAGUE_SEASON_MISMATCH)
                        # the BATCH resolver is a second, independent
                        # resolution — compute_roster_status/_slot_summaries/
                        # _partition_candidates reach memberships through it
                        # and never through the single form, so a gate in only
                        # one of the two would be the same split authority.
                        self.assertEqual(
                            api.roster.resolve_membership_contexts_for_game(
                                game), {})
                        self.assertEqual(
                            api.roster.resolve_memberships_for_game(game), {})
                        # the opportunity / candidate reads
                        self.assertIsNotNone(
                            api.roster.substitute_block_reason(
                                p["id"], fx["gid"]))
                        self.assertEqual(
                            api.roster.list_substitute_opportunities(p["id"]),
                            [])
                        self.assertEqual(
                            api.roster.list_addable_players(
                                fx["gid"], fx["home"]), [])
                    ran.append((label, variant))
            finally:
                self._close(label, store)
        self._assert_matrix_ran(ran, VARIANTS)


# ======================================================================
# 2. EVERY BOUND MUTATION SURFACE, WITH EXACT ZERO WRITE ATTEMPTS
# ======================================================================
# Each entry is (name, callable(fx) -> facade result). The whole point of
# enumerating them is that the guard is reached through THREE different
# routes -- `_guard_mutable`, the three direct `_guard_active_season` calls,
# and the batch entry points -- and a fix applied to only one route leaves a
# partially protected tree, which the ruling explicitly forbids.
ROSTER_SURFACES = (
    ("enroll_substitute",
     lambda fx, p: fx["api"].enroll_substitute(fx["gid"], p["id"]),
     "validation_error", season_guard.SEASON_ARCHIVED),
    ("offer_substitute",
     lambda fx, p: fx["api"].offer_substitute(fx["gid"], p["id"]),
     "validation_error", season_guard.SEASON_ARCHIVED),
    ("accept_substitute",
     lambda fx, p: fx["api"].accept_substitute(fx["gid"], p["id"]),
     "validation_error", season_guard.SEASON_ARCHIVED),
    ("decline_substitute",
     lambda fx, p: fx["api"].decline_substitute(fx["gid"], p["id"]),
     "validation_error", season_guard.SEASON_ARCHIVED),
    ("withdraw_substitute",
     lambda fx, p: fx["api"].withdraw_substitute(fx["gid"], p["id"]),
     "validation_error", season_guard.SEASON_ARCHIVED),
    # THE ONE SURFACE WHOSE REFUSAL COMES FROM THE READ GATE, and it is
    # deliberate rather than an oversight. `add_substitute_candidate` runs the
    # pure, non-transactional `substitute_block_reason` BEFORE delegating to
    # `enroll_substitute`, so on a drifted Game the now-closed resolver answers
    # first and the code is `not_eligible`. It still fails closed with zero
    # write attempts, which is what the ruling requires of Coach-add; the code
    # differs only because a different (earlier, lock-free) gate got there
    # first. `CoachAddOnACoherentArchivedGame` below pins that this surface
    # still reports `season_archived` when the Game is coherent, so the
    # divergence stays confined to the drifted shape.
    ("add_substitute_candidate",
     lambda fx, p: fx["api"].add_substitute_candidate(fx["gid"], p["id"],
                                                      actor_id=ADMIN),
     "not_eligible", None),
    ("add_substitute_to_roster",
     lambda fx, p: fx["api"].add_substitute_to_roster(fx["gid"], p["id"],
                                                      actor_id=ADMIN),
     "validation_error", season_guard.SEASON_ARCHIVED),
    ("select_roster",
     lambda fx, p: fx["api"].select_roster(fx["gid"], [p["id"]],
                                           actor_id=ADMIN),
     "validation_error", season_guard.SEASON_ARCHIVED),
    ("set_availability",
     lambda fx, p: fx["api"].set_availability(fx["gid"], p["id"], "available",
                                              actor_id=ADMIN),
     "validation_error", season_guard.SEASON_ARCHIVED),
    ("set_roster_status",
     lambda fx, p: fx["api"].set_roster_status(fx["gid"], p["id"], "confirmed",
                                               actor_id=ADMIN),
     "validation_error", season_guard.SEASON_ARCHIVED),
    ("remove_player",
     lambda fx, p: fx["api"].remove_player(fx["gid"], p["id"],
                                           actor_id=ADMIN),
     "validation_error", season_guard.SEASON_ARCHIVED),
    ("copy_previous_roster",
     lambda fx, p: fx["api"].copy_previous_roster(fx["gid"], fx["home"],
                                                  actor_id=ADMIN),
     "validation_error", season_guard.SEASON_ARCHIVED),
    ("auto_build_roster",
     lambda fx, p: fx["api"].auto_build_roster(fx["gid"], fx["home"],
                                               actor_id=ADMIN),
     "validation_error", season_guard.SEASON_ARCHIVED),
    ("lock_roster",
     lambda fx, p: fx["api"].lock_roster(fx["gid"], actor_id=ADMIN),
     "validation_error", season_guard.SEASON_ARCHIVED),
    ("unlock_roster",
     lambda fx, p: fx["api"].unlock_roster(fx["gid"], actor_id=ADMIN),
     "validation_error", season_guard.SEASON_ARCHIVED),
    ("cancel_game",
     lambda fx, p: fx["api"].cancel_game(fx["gid"], actor_id=ADMIN),
     "validation_error", season_guard.SEASON_ARCHIVED),
)


class EveryBoundRosterMutationFailsClosedWithZeroWrites(_Authority,
                                                        unittest.TestCase):
    """The owner's required coverage, exactly: "assert the resolver,
    opportunity/candidate reads, enroll, offer, accept, and Coach-add all fail
    closed with exact zero substitute/roster/audit/notification writes".

    S1 is ARCHIVED here (through the facade), so each surface must answer
    ``season_archived`` NAMING S1 — the canonical Season — even though the
    Game's own column says NULL or S2. That single assertion carries the whole
    blocker: the refusal proves the guard ran, and the reported id proves it
    locked the right row.
    """

    def test_every_surface_refuses_naming_the_canonical_season(self):
        ran = []
        for label, store in self._stores():
            try:
                self._assert_backend(label, store)
                for variant in VARIANTS:
                    for name, call, code, reason in ROSTER_SURFACES:
                        store.clear_all_data()
                        fx = self._build(store)
                        p = self._player(fx, "Mover")
                        self._drift(fx, variant)
                        self._archive(fx, fx["s1"])
                        before = self._writes(fx)

                        with self.subTest(backend=label, variant=variant,
                                          surface=name):
                            with self._write_attempts(fx["api"].store) as calls:
                                res = call(fx, p)
                            err = self._error(res)
                            self.assertEqual(err["code"], code, (name, res))
                            if reason is not None:
                                self.assertEqual(
                                    (err["details"] or {}).get("reason"),
                                    reason, (name, res))
                                # THE canonical id, not the drifted one.
                                # Before this fix the sibling variant reported
                                # S2 -- the row the guard had wrongly locked.
                                self.assertEqual(
                                    (err["details"] or {}).get("season_id"),
                                    fx["s1"]["id"], (name, res))
                            # EXACT ZERO write ATTEMPTS -- not "nothing
                            # survived", which a late guard inside a
                            # transaction also satisfies.
                            self.assertEqual(calls, [], (name, calls))
                            self.assertEqual(self._writes(fx), before, name)
                        ran.append((label, (variant, name)))
            finally:
                self._close(label, store)
        self._assert_matrix_ran(
            ran, [(v, e[0]) for v in VARIANTS for e in ROSTER_SURFACES])


class CoachAddOnACoherentArchivedGame(_Authority, unittest.TestCase):
    """The control that CONFINES the one divergence in ``ROSTER_SURFACES``.

    ``add_substitute_candidate`` answers ``not_eligible`` on a DRIFTED archived
    Game because the (now-closed) read gate runs before the season guard. That
    is only acceptable if the surface still reports the actionable
    ``season_archived`` when the Game is COHERENT — otherwise the fix would
    have quietly downgraded every Coach-add refusal on an archived competition
    to a generic one. Asserted here so the divergence cannot spread."""

    def test_coach_add_still_reports_season_archived_when_coherent(self):
        ran = []
        for label, store in self._stores():
            try:
                self._assert_backend(label, store)
                store.clear_all_data()
                fx = self._build(store)
                p = self._player(fx, "Mover")
                # NO drift: the Game and its LeagueSeason agree.
                self._archive(fx, fx["s1"])
                with self.subTest(backend=label):
                    res = fx["api"].add_substitute_candidate(
                        fx["gid"], p["id"], actor_id=ADMIN)
                    err = self._error(res)
                    self.assertEqual(err["code"], "validation_error", res)
                    self.assertEqual((err["details"] or {}).get("reason"),
                                     season_guard.SEASON_ARCHIVED, res)
                ran.append((label, "coherent"))
            finally:
                self._close(label, store)
        self._assert_matrix_ran(ran, ["coherent"])


# ======================================================================
# 3. THE SECOND GUARD FAMILY — SetupService
# ======================================================================
SETUP_SURFACES = (
    "publish_game", "request_reschedule", "assign_official", "record_result",
    "approve_result", "delete_game", "unassign_official",
    # NOT a `_guard_game_season` caller at all — a FOURTH copy of the same
    # falsy-skip, on the API facade, guarding the notification writes
    # `remind_unresponded` performs. Included because "both guard families"
    # describes where the defect was FOUND, not the limit of where it could
    # be, and a notification is a Season-owned write like any other (see
    # season_guard's module docstring).
    "remind_unresponded",
)


class TheSetupServiceGuardFamilyFailsClosed(_Authority, unittest.TestCase):
    """``SetupService._guard_game_season`` carried a VERBATIM COPY of the
    defect across eleven callers. A representative spread of them is exercised
    here — publish/reschedule/officials/results/delete — rather than one, so a
    fix applied to a single call path cannot pass.

    ``unassign_official`` is included specifically because it reaches the
    guard through ``self.store.get_game(a.game_id)`` rather than through a
    ``game`` local, which is the call shape most likely to be missed by a
    mechanical rollout."""

    def _setup_call(self, fx, name):
        api = fx["api"]
        if name == "publish_game":
            return api.publish_game(fx["gid"], actor_id=ADMIN)
        if name == "request_reschedule":
            return api.request_reschedule(fx["gid"], fx["home"], "ice lost",
                                          actor_id=ADMIN)
        if name == "assign_official":
            o = api.create_official("Ref", actor_id=ADMIN)
            return api.assign_official(fx["gid"], o["id"], "referee",
                                       actor_id=ADMIN)
        if name == "record_result":
            return api.record_result(fx["gid"], 3, 2, actor_id=ADMIN)
        if name == "approve_result":
            return api.approve_result(fx["gid"], actor_id=ADMIN)
        if name == "delete_game":
            return api.delete_game(fx["gid"], actor_id=ADMIN)
        if name == "unassign_official":
            # The assignment is created BEFORE the drift/archive, so the
            # refusal below is the guard's and not a missing row.
            return api.unassign_official(fx["assignment_id"], actor_id=ADMIN)
        if name == "remind_unresponded":
            return api.remind_unresponded(fx["gid"], fx["home"],
                                          actor_id=ADMIN)
        raise AssertionError(name)

    def test_a_spread_of_setup_mutations_refuse_naming_the_canonical_season(
            self):
        ran = []
        for label, store in self._stores():
            try:
                self._assert_backend(label, store)
                for variant in VARIANTS:
                    for name in SETUP_SURFACES:
                        store.clear_all_data()
                        fx = self._build(store)
                        if name == "unassign_official":
                            o = fx["api"].create_official("Ref",
                                                          actor_id=ADMIN)
                            a = fx["api"].assign_official(
                                fx["gid"], o["id"], "referee", actor_id=ADMIN)
                            self.assertNotIn("error", a, a)
                            fx["assignment_id"] = a["id"]
                        self._drift(fx, variant)
                        self._archive(fx, fx["s1"])
                        before = self._writes(fx)

                        with self.subTest(backend=label, variant=variant,
                                          surface=name):
                            res = self._setup_call(fx, name)
                            err = self._error(res)
                            self.assertEqual(err["code"], "validation_error",
                                             (name, res))
                            self.assertEqual(
                                (err["details"] or {}).get("reason"),
                                season_guard.SEASON_ARCHIVED, (name, res))
                            self.assertEqual(
                                (err["details"] or {}).get("season_id"),
                                fx["s1"]["id"], (name, res))
                            self.assertEqual(self._writes(fx), before, name)
                            # The Game itself is still there and still bound —
                            # delete_game in particular must not have run.
                            g = fx["api"].store.get_game(fx["gid"])
                            self.assertIsNotNone(g, name)
                            self.assertEqual(g.league_season_id, fx["ls_id"])
                        ran.append((label, (variant, name)))
            finally:
                self._close(label, store)
        self._assert_matrix_ran(
            ran, [(v, n) for v in VARIANTS for n in SETUP_SURFACES])


# ======================================================================
# 4. THE PRECEDENCE
# ======================================================================
class ArchivedCanonicalOutranksMismatch(_Authority, unittest.TestCase):
    """The ruling fixes the ORDER, and the order is falsifiable only if BOTH
    arms are pinned:

      * canonical Season ARCHIVED  -> ``season_archived``, naming the CANONICAL
        id, whatever ``game.season_id`` says;
      * canonical Season ACTIVE    -> ``game_league_season_mismatch``.

    Pinning only the first would let an implementation answer
    ``season_archived`` for everything; pinning only the second would let one
    answer ``game_league_season_mismatch`` for an archived competition, which
    reports "repair this row" for a problem repairing the row will not fix.
    """

    def test_an_active_canonical_season_reports_the_mismatch(self):
        ran = []
        for label, store in self._stores():
            try:
                self._assert_backend(label, store)
                for variant in VARIANTS:
                    store.clear_all_data()
                    fx = self._build(store)
                    p = self._player(fx, "Mover")
                    target = self._drift(fx, variant)
                    # NOTHING is archived: S1 and S2 are both ACTIVE.
                    with self.subTest(backend=label, variant=variant):
                        res = fx["api"].enroll_substitute(fx["gid"], p["id"])
                        err = self._error(res)
                        self.assertEqual(err["code"], "validation_error", res)
                        details = err["details"] or {}
                        self.assertEqual(
                            details.get("reason"),
                            season_guard.GAME_LEAGUE_SEASON_MISMATCH, res)
                        # BOTH sides of the disagreement are reported as-is
                        # (including None) so remediation sees the actual row.
                        self.assertEqual(details.get("season_id"), target, res)
                        self.assertEqual(
                            details.get("league_season_season_id"),
                            fx["s1"]["id"], res)
                    ran.append((label, variant))
            finally:
                self._close(label, store)
        self._assert_matrix_ran(ran, VARIANTS)

    def test_the_decisive_control_both_archived_names_the_canonical_season(
            self):
        """THE CONTROL THAT MADE THE DEFECT UNAMBIGUOUS. ``game.season_id`` is
        S2 and BOTH Seasons are archived, so BOTH candidate rows would produce
        a ``season_archived`` refusal and the only thing that differs is WHICH
        ID IS NAMED.

        At head 57f9107 the refusal named S2 — the guard had locked and judged
        the sibling — while ``ctx.season.id`` was S1 all along. That is not a
        missing check, it is the wrong row, and no assertion about "does it
        refuse" can see it. This one can."""
        ran = []
        for label, store in self._stores():
            try:
                self._assert_backend(label, store)
                store.clear_all_data()
                fx = self._build(store)
                p = self._player(fx, "Mover")
                self._drift(fx, "sibling")
                self._archive(fx, fx["s1"])
                self._archive(fx, fx["s2"])
                with self.subTest(backend=label):
                    res = fx["api"].enroll_substitute(fx["gid"], p["id"])
                    details = self._error(res)["details"] or {}
                    self.assertEqual(details.get("reason"),
                                     season_guard.SEASON_ARCHIVED, res)
                    self.assertEqual(details.get("season_id"),
                                     fx["s1"]["id"], res)
                    self.assertNotEqual(details.get("season_id"),
                                        fx["s2"]["id"], res)
                ran.append((label, "decisive"))
            finally:
                self._close(label, store)
        self._assert_matrix_ran(ran, ["decisive"])


# ======================================================================
# 5. TRI-STORE FUNCTIONAL PARITY
# ======================================================================
class TheSameInputGivesTheSameAnswerOnEveryBackend(_Authority,
                                                   unittest.TestCase):
    """PARITY COMPARED ACROSS BACKENDS, not merely asserted separately on each.

    Every other section asserts a literal expectation per backend, which
    proves each backend is right but would not notice two of them being right
    in DIFFERENT ways (a reason code that differs only on PostgreSQL, say,
    because a NULL comparison went through SQL rather than Python). This
    collects the answer from every configured backend and asserts the
    collected answers are IDENTICAL to one another."""

    CASES = ("null_archived", "sibling_archived", "null_active",
             "sibling_active", "coherent_archived", "coherent_active",
             "exhibition_archived")

    def _enroll_outcome(self, fx, case):
        api = fx["api"]
        p = self._player(fx, "Mover")
        gid = fx["gid"]
        if case.startswith("exhibition"):
            ex = self._exhibition(fx)
            gid = ex["id"]
            self._archive(fx, fx["s1"])
        else:
            if case.startswith("null"):
                self._drift(fx, "null")
            elif case.startswith("sibling"):
                self._drift(fx, "sibling")
            if case.endswith("_archived"):
                self._archive(fx, fx["s1"])
        res = api.enroll_substitute(gid, p["id"])
        if "error" not in res:
            return ("ok", res["status"])
        err = res["error"]
        return ("error", err["code"],
                (err.get("details") or {}).get("reason"))

    def test_outcomes_are_identical_across_memory_sqlite_and_postgres(self):
        collected = {}
        ran = []
        for label, store in self._stores():
            try:
                self._assert_backend(label, store)
                for case in self.CASES:
                    store.clear_all_data()
                    fx = self._build(store)
                    collected.setdefault(case, {})[label] = (
                        self._enroll_outcome(fx, case))
                    ran.append((label, case))
            finally:
                self._close(label, store)
        self._assert_matrix_ran(ran, self.CASES)

        # The parity assertion itself: one distinct answer per case.
        for case, by_backend in sorted(collected.items()):
            answers = set(by_backend.values())
            self.assertEqual(len(answers), 1,
                             (case, sorted(by_backend.items())))

        # …and the answers are the RIGHT ones, so parity cannot be achieved by
        # every backend being identically wrong.
        expect = {
            "null_archived": ("error", "validation_error",
                              season_guard.SEASON_ARCHIVED),
            "sibling_archived": ("error", "validation_error",
                                 season_guard.SEASON_ARCHIVED),
            "null_active": ("error", "validation_error",
                            season_guard.GAME_LEAGUE_SEASON_MISMATCH),
            "sibling_active": ("error", "validation_error",
                               season_guard.GAME_LEAGUE_SEASON_MISMATCH),
            "coherent_archived": ("error", "validation_error",
                                  season_guard.SEASON_ARCHIVED),
            "coherent_active": ("ok", "enrolled"),
            "exhibition_archived": ("error", "validation_error",
                                    season_guard.SEASON_ARCHIVED),
        }
        for case, by_backend in sorted(collected.items()):
            for label, answer in sorted(by_backend.items()):
                self.assertEqual(answer, expect[case], (case, label))


# ======================================================================
# 6. THE CANONICAL SEASON ROW IS THE SHARED LOCK
# ======================================================================
class TheCanonicalSeasonRowIsTheSerializationPoint(_Authority,
                                                   unittest.TestCase):
    """"provides no shared Season-row serialization point against a concurrent
    archive" — the half of the blocker that an outcome assertion cannot reach.

    THE INSTRUMENTED HOOK, tri-store. ``get_season_for_update`` is wrapped on
    the store INSTANCE and keyed on the CANONICAL Season id. If the guard were
    still locking ``game.season_id``, the hook would never fire for the NULL
    variant (no lock at all) and would fire for S2 in the sibling variant. That
    it fires for S1 in BOTH is the direct, positive observation that the
    canonical row is the one being locked — on Memory and SQLite too, where a
    second connection does not exist and a wall-clock race is unconstructible.

    THE LATCH IS ONE-SHOT AND MANDATORY: the Season lock is re-taken by nested
    ``select_roster``/``set_availability`` inside a batch, so a hook that fired
    every time would run its interleaving repeatedly. It is installed ONLY
    around the call under test and torn down in ``finally`` — ``setup_service``
    takes this same lock from about eight other places, and a hook left live
    during fixture construction would fire inside them."""

    def _instrument(self, store, target_season_id, when_locked, seen):
        """Wrap the LOCK on this store instance, keyed on ``target_season_id``,
        one-shot, so the interleaving happens exactly once and only for the row
        this test is about."""
        real = store.get_season_for_update
        state = {"n": 0}

        def wrapped(season_id):
            seen.append(season_id)
            if season_id == target_season_id and not state["n"]:
                state["n"] += 1
                when_locked()
            return real(season_id)

        store.get_season_for_update = wrapped
        return state

    def test_the_lock_taken_is_the_leagueseasons_season_not_the_games(self):
        """The tri-store property, both variants: the row locked by a bound
        mutation is S1, and S2/None is never locked on its behalf."""
        ran = []
        for label, store in self._stores():
            try:
                self._assert_backend(label, store)
                for variant in VARIANTS:
                    store.clear_all_data()
                    fx = self._build(store)
                    p = self._player(fx, "Mover")
                    self._drift(fx, variant)
                    seen = []
                    fired = self._instrument(fx["api"].store, fx["s1"]["id"],
                                             lambda: None, seen)
                    try:
                        fx["api"].enroll_substitute(fx["gid"], p["id"])
                    finally:
                        del fx["api"].store.get_season_for_update
                    with self.subTest(backend=label, variant=variant):
                        self.assertEqual(fired["n"], 1, (seen, variant))
                        self.assertIn(fx["s1"]["id"], seen, seen)
                        # The wrong row is never locked on this Game's behalf.
                        self.assertNotIn(fx["s2"]["id"], seen, seen)
                        self.assertNotIn(None, seen, seen)
                    ran.append((label, variant))
            finally:
                self._close(label, store)
        self._assert_matrix_ran(ran, VARIANTS)

    def test_an_archive_interleaved_at_lock_acquisition_is_observed(self):
        """COMMIT ORDER A — the archive commits FIRST, while the mutation is
        parked at the exact moment it takes the canonical Season lock. The
        mutation must then observe ARCHIVED and refuse with zero writes.

        On a coherent Game this is #159's existing property. What is new is
        that it now holds for a DRIFTED Game: the archive is applied to S1,
        which the Game's own column does not name, and the mutation still sees
        it — because the row it locks is S1's."""
        ran = []
        for label, store in self._stores():
            try:
                self._assert_backend(label, store)
                for variant in VARIANTS:
                    store.clear_all_data()
                    fx = self._build(store)
                    p = self._player(fx, "Mover")
                    self._drift(fx, variant)
                    before = self._writes(fx)

                    def archive_now():
                        # Applied to the CANONICAL Season, at the instant the
                        # guard takes its lock. On Memory/SQLite this is the
                        # deterministic stand-in for a concurrent committer.
                        s = fx["api"].store.get_season(fx["s1"]["id"])
                        s.status = SeasonStatus.ARCHIVED
                        fx["api"].store.save_season(s)

                    seen = []
                    fired = self._instrument(fx["api"].store, fx["s1"]["id"],
                                             archive_now, seen)
                    try:
                        res = fx["api"].enroll_substitute(fx["gid"], p["id"])
                    finally:
                        del fx["api"].store.get_season_for_update
                    with self.subTest(backend=label, variant=variant):
                        self.assertEqual(fired["n"], 1, seen)
                        details = self._error(res)["details"] or {}
                        self.assertEqual(details.get("reason"),
                                         season_guard.SEASON_ARCHIVED, res)
                        self.assertEqual(details.get("season_id"),
                                         fx["s1"]["id"], res)
                        self.assertEqual(self._writes(fx), before)
                    ran.append((label, variant))
            finally:
                self._close(label, store)
        self._assert_matrix_ran(ran, VARIANTS)

    def test_a_mutation_committing_first_leaves_the_archive_to_win_after(self):
        """COMMIT ORDER B — the mutation commits FIRST (against a still-active
        canonical Season) and the archive lands afterwards. The mutation is
        legitimate frozen history; the NEXT mutation must be refused.

        Both orders are asserted because the invariant is not "the archive
        always wins", it is "the two serialize on one row". An implementation
        that refused everything would satisfy order A alone."""
        ran = []
        for label, store in self._stores():
            try:
                self._assert_backend(label, store)
                store.clear_all_data()
                fx = self._build(store)
                p1 = self._player(fx, "First")
                p2 = self._player(fx, "Second")
                with self.subTest(backend=label):
                    ok = fx["api"].enroll_substitute(fx["gid"], p1["id"])
                    self.assertNotIn("error", ok, ok)
                    self._archive(fx, fx["s1"])
                    after = self._writes(fx)
                    res = fx["api"].enroll_substitute(fx["gid"], p2["id"])
                    details = self._error(res)["details"] or {}
                    self.assertEqual(details.get("reason"),
                                     season_guard.SEASON_ARCHIVED, res)
                    self.assertEqual(details.get("season_id"),
                                     fx["s1"]["id"], res)
                    # The first enrollment SURVIVES: it is history, not
                    # something the archive retroactively undoes.
                    self.assertEqual(self._writes(fx), after)
                ran.append((label, "order_b"))
            finally:
                self._close(label, store)
        self._assert_matrix_ran(ran, ["order_b"])


class ThePostgresLockIsGenuinelyHeldAgainstASecondConnection(
        _Authority, unittest.TestCase):
    """THE POSITIVE LOCK PROOF, PostgreSQL only — the only backend where a
    second connection exists at all (Memory serializes on a process-wide
    RLock, and ``SqlStore(":memory:")`` is one private database per handle, so
    on both of those a concurrent writer is not merely unlikely, it is
    unconstructible).

    An outcome assertion can be satisfied by a re-read; only observing a real
    heavyweight lock WAIT proves the row is held. Two independent probes, from
    a THIRD connection, so neither one alone carries the claim:

      * ``pg_stat_activity``: the archiver's backend is ``state='active'`` AND
        ``wait_event_type='Lock'`` while the mutation holds S1;
      * ``SELECT … FOR UPDATE NOWAIT`` on ``seasons`` where ``id = S1``: fails
        immediately, and the SAME statement against S2 SUCCEEDS — which is what
        makes it a proof about S1 specifically rather than about locking in
        general.

    Deterministic: two ``threading.Event`` handoffs and a per-PID lock-state
    poll, no sleeps standing in for ordering, and an ``errors`` list so a
    failure inside the thread surfaces instead of being swallowed."""

    def setUp(self):
        url = os.environ.get("TEST_DATABASE_URL")
        if not url:
            _announce_pg_skip("CANONICAL SEASON LOCK (POSTGRES)")
            self.skipTest(_PG_SKIP)
        self.url = url

    def _backend_pid(self, store):
        with store.conn.cursor() as cur:
            cur.execute("SELECT pg_backend_pid() AS pid")
            return cur.fetchone()["pid"]

    def _wait_until_blocked_on_lock(self, backend_pid, timeout=20.0):
        """Poll until ``backend_pid`` is ACTIVELY waiting on a heavyweight
        lock. The interval only bounds busy-spin; correctness comes from the
        per-PID lock state, so an unrelated waiter can never satisfy it."""
        import time

        import psycopg
        deadline = time.monotonic() + timeout
        with psycopg.connect(self.url, autocommit=True) as mon:
            while time.monotonic() < deadline:
                with mon.cursor() as cur:
                    cur.execute(
                        "SELECT state, wait_event_type FROM pg_stat_activity "
                        "WHERE pid = %s", (backend_pid,))
                    row = cur.fetchone()
                    if (row is not None and row[0] == "active"
                            and row[1] == "Lock"):
                        return True
                time.sleep(0.02)
        return False

    def _nowait_probe(self, season_id):
        """``True`` when a THIRD connection can take the row lock immediately.

        ``FOR UPDATE NOWAIT`` raises rather than queueing, so this is a
        non-blocking question with no timeout to tune."""
        import psycopg
        with psycopg.connect(self.url, autocommit=True) as probe:
            try:
                with probe.cursor() as cur:
                    cur.execute("SELECT id FROM seasons WHERE id = %s "
                                "FOR UPDATE NOWAIT", (season_id,))
                    cur.fetchall()
                return True
            except psycopg.errors.LockNotAvailable:
                return False

    def _run_locked_probe(self, fx, store, drift):
        """Park the mutation at the instant it takes the canonical Season lock,
        let a SECOND connection try to archive that Season, and probe from a
        THIRD. Returns ``(result, probes, seen, archive_result)``."""
        other = SqlStore(self.url)
        try:
            self.assertEqual(other.backend, "postgres", other.backend)
            other_api = ApiService(other)
            archiver_pid = self._backend_pid(other)
            p = self._player(fx, "Mover")
            if drift:
                self._drift(fx, "sibling")

            locked = threading.Event()
            release = threading.Event()
            errors = []
            outcome = {}

            def archiver():
                try:
                    self.assertTrue(locked.wait(20), "mutation never locked")
                    # Blocks on S1's row until the mutation's transaction ends.
                    outcome["archive"] = other_api.archive_season(
                        fx["s1"]["id"], reason="over", actor_id=ADMIN)
                except BaseException as exc:
                    errors.append(repr(exc))

            probes = {}

            def when_locked():
                locked.set()
                # The archiver must reach a REAL heavyweight lock wait…
                probes["blocked"] = self._wait_until_blocked_on_lock(
                    archiver_pid)
                # …and an independent third connection must find S1 taken and
                # S2 free. "S2 free" is what makes this a proof about S1
                # specifically rather than about locking in general.
                probes["s1_free"] = self._nowait_probe(fx["s1"]["id"])
                probes["s2_free"] = self._nowait_probe(fx["s2"]["id"])
                release.set()

            seen = []
            state = {"n": 0}
            real = store.get_season_for_update

            def wrapped(season_id):
                row = real(season_id)
                seen.append(season_id)
                if season_id == fx["s1"]["id"] and not state["n"]:
                    state["n"] += 1
                    when_locked()
                return row

            store.get_season_for_update = wrapped
            t = threading.Thread(target=archiver, daemon=True)
            t.start()
            try:
                res = fx["api"].enroll_substitute(fx["gid"], p["id"])
            finally:
                del store.get_season_for_update
            self.assertTrue(release.wait(30), "probe never completed")
            t.join(timeout=40)
            self.assertEqual(errors, [], errors)
            self.assertFalse(t.is_alive())
            self.assertEqual(state["n"], 1, seen)
            return res, probes, seen, outcome.get("archive")
        finally:
            other.close()

    def _assert_locked_the_canonical_row(self, fx, probes, seen):
        # The mutation locked S1 — the LeagueSeason's Season — and never the
        # sibling. Before this fix the sibling variant locked S2 and the NULL
        # variant locked nothing at all.
        self.assertIn(fx["s1"]["id"], seen, seen)
        self.assertNotIn(fx["s2"]["id"], seen, seen)
        # THE PROOF, from a THIRD connection: the archiver genuinely waited on
        # a heavyweight lock, S1's row was not obtainable, and S2's was.
        self.assertTrue(probes["blocked"],
                        "archiver never entered a heavyweight lock wait")
        self.assertFalse(probes["s1_free"],
                         "S1 was not row-locked by the mutation")
        self.assertTrue(probes["s2_free"],
                        "S2 was locked, which is the wrong row")

    def test_a_drifted_game_locks_s1_and_only_then_reports_the_mismatch(self):
        """THE LOCK TARGET, proven on a DRIFTED Game — the case where the old
        guard demonstrably locked the wrong row.

        ``game.season_id`` names the SIBLING. A concurrent ``archive_season``
        on S1 blocks, and a third connection finds S1 held and S2 free, so the
        row this transaction is holding is unambiguously S1's. The refusal is
        then ``game_league_season_mismatch``, which is itself the PRECEDENCE
        observed under a real lock: the canonical Season was locked and found
        ACTIVE first, and only then was ``game.season_id`` compared."""
        store = fresh_sql_store(self.url)
        try:
            self.assertEqual(store.backend, "postgres", store.backend)
            store.clear_all_data()
            fx = self._build(store)
            before = self._writes(fx)
            res, probes, seen, archived = self._run_locked_probe(
                fx, store, drift=True)
            self._assert_locked_the_canonical_row(fx, probes, seen)
            details = self._error(res)["details"] or {}
            self.assertEqual(details.get("reason"),
                             season_guard.GAME_LEAGUE_SEASON_MISMATCH, res)
            self.assertEqual(details.get("league_season_season_id"),
                             fx["s1"]["id"], res)
            self.assertEqual(self._writes(fx), before)
            # The archiver was released by the refusal's rollback and won.
            self.assertNotIn("error", archived or {}, archived)
        finally:
            store.reset_schema()
            store.close()

    def test_a_coherent_mutation_commits_first_and_the_archive_follows(self):
        """COMMIT ORDER B on two REAL connections: the mutation holds S1, the
        archiver blocks on it, the mutation COMMITS, the archive then commits,
        and the NEXT mutation is refused.

        Both orders are asserted because the invariant is not "the archive
        always wins" — it is "the two serialize on ONE row". An implementation
        that refused everything would satisfy order A on its own."""
        store = fresh_sql_store(self.url)
        try:
            self.assertEqual(store.backend, "postgres", store.backend)
            store.clear_all_data()
            fx = self._build(store)
            # Created BEFORE the race: opening a membership is itself a
            # Season-owned write, so a player minted after the archive commits
            # would be refused by #159 and could never reach the gate this
            # test is about.
            later_player = self._player(fx, "After")
            res, probes, seen, archived = self._run_locked_probe(
                fx, store, drift=False)
            self._assert_locked_the_canonical_row(fx, probes, seen)
            # The mutation won the row and committed.
            self.assertNotIn("error", res, res)
            self.assertNotIn("error", archived or {}, archived)
            self.assertEqual(
                store.get_season(fx["s1"]["id"]).status,
                SeasonStatus.ARCHIVED)
            # …and the enrollment it wrote is frozen history, not something the
            # archive retroactively undoes.
            self.assertEqual(
                [(x.player_id, x.status.value)
                 for x in store.substitutes_for_game(fx["gid"])],
                [(res["player_id"], "enrolled")])
            after = self._writes(fx)
            later = fx["api"].enroll_substitute(fx["gid"],
                                                later_player["id"])
            self.assertEqual(
                (self._error(later)["details"] or {}).get("reason"),
                season_guard.SEASON_ARCHIVED, later)
            self.assertEqual(self._writes(fx), after)
        finally:
            store.reset_schema()
            store.close()

    def test_an_archive_committing_first_is_observed_by_the_mutation(self):
        """COMMIT ORDER A on two real connections: the archive commits while
        the mutation is parked BEFORE taking the lock, so the mutation's
        ``SELECT … FOR UPDATE`` reads the committed ARCHIVED row and refuses
        with zero writes."""
        store = fresh_sql_store(self.url)
        other = None
        try:
            self.assertEqual(store.backend, "postgres", store.backend)
            store.clear_all_data()
            fx = self._build(store)
            p = self._player(fx, "Mover")
            self._drift(fx, "null")
            before = self._writes(fx)

            other = SqlStore(self.url)
            other_api = ApiService(other)

            parked = threading.Event()
            committed = threading.Event()
            errors = []

            def archiver():
                try:
                    self.assertTrue(parked.wait(20), "never parked")
                    r = other_api.archive_season(fx["s1"]["id"], reason="over",
                                                 actor_id=ADMIN)
                    self.assertNotIn("error", r, r)
                except BaseException as exc:
                    errors.append(repr(exc))
                finally:
                    committed.set()

            # Park BEFORE the lock is taken, on the LeagueSeason resolve that
            # immediately precedes it, so the archive is fully committed by the
            # time this transaction issues its FOR UPDATE.
            real = store.get_league_season
            state = {"n": 0}

            def wrapped(ls_id):
                if ls_id == fx["ls_id"] and not state["n"]:
                    state["n"] += 1
                    parked.set()
                    if not committed.wait(30):
                        errors.append("archiver never committed")
                return real(ls_id)

            store.get_league_season = wrapped
            t = threading.Thread(target=archiver, daemon=True)
            t.start()
            try:
                res = fx["api"].enroll_substitute(fx["gid"], p["id"])
            finally:
                del store.get_league_season
            t.join(timeout=40)

            self.assertEqual(errors, [], errors)
            self.assertEqual(state["n"], 1)
            details = self._error(res)["details"] or {}
            self.assertEqual(details.get("reason"),
                             season_guard.SEASON_ARCHIVED, res)
            self.assertEqual(details.get("season_id"), fx["s1"]["id"], res)
            self.assertEqual(self._writes(fx), before)
        finally:
            if other is not None:
                other.close()
            store.reset_schema()
            store.close()


# ======================================================================
# 7. EXHIBITIONS — the unbound branch, preserved
# ======================================================================
class ExhibitionsKeepTheirOwnSeasonAsAuthority(_Authority, unittest.TestCase):
    """The ruling: "Preserve exhibition behavior using ``game.season_id``."

    An exhibition carries ``league_season_id = None`` AND a real
    ``season_id``, so it is genuinely unbound and its own Season is genuinely
    its authority. Both directions are pinned — it still WORKS while that
    Season is active, and it is still REFUSED once that Season is archived —
    because the risk of this fix is in either direction: routing exhibitions
    down the bound branch would break them outright, and "fixing" that by
    skipping the guard for anything unbound would silently reopen #159 for
    every exhibition."""

    def test_an_exhibition_enrolls_while_its_own_season_is_active(self):
        ran = []
        for label, store in self._stores():
            try:
                self._assert_backend(label, store)
                store.clear_all_data()
                fx = self._build(store)
                ex = self._exhibition(fx)
                local = Player(id=fx["api"].store.next_id("player"),
                               team_id=fx["home"], name="Local",
                               position=Position.FORWARD)
                fx["api"].store.add_player(local)
                with self.subTest(backend=label):
                    res = fx["api"].enroll_substitute(ex["id"], local.id)
                    self.assertNotIn("error", res, res)
                ran.append((label, "active"))
            finally:
                self._close(label, store)
        self._assert_matrix_ran(ran, ["active"])

    def test_an_exhibition_is_refused_once_its_own_season_is_archived(self):
        ran = []
        for label, store in self._stores():
            try:
                self._assert_backend(label, store)
                store.clear_all_data()
                fx = self._build(store)
                ex = self._exhibition(fx)
                local = Player(id=fx["api"].store.next_id("player"),
                               team_id=fx["home"], name="Local",
                               position=Position.FORWARD)
                fx["api"].store.add_player(local)
                self._archive(fx, fx["s1"])
                before = self._writes(fx, ex["id"])
                with self.subTest(backend=label):
                    with self._write_attempts(fx["api"].store) as calls:
                        res = fx["api"].enroll_substitute(ex["id"], local.id)
                    details = self._error(res)["details"] or {}
                    self.assertEqual(details.get("reason"),
                                     season_guard.SEASON_ARCHIVED, res)
                    # Its OWN Season, which for an exhibition is the right
                    # authority.
                    self.assertEqual(details.get("season_id"),
                                     fx["s1"]["id"], res)
                    self.assertEqual(calls, [], calls)
                    self.assertEqual(self._writes(fx, ex["id"]), before)
                ran.append((label, "archived"))
            finally:
                self._close(label, store)
        self._assert_matrix_ran(ran, ["archived"])


# ======================================================================
# 8. A DANGLING BINDING IS BROKEN, NOT LEGACY
# ======================================================================
class ADanglingLeagueSeasonIsRefusedNotTreatedAsUnbound(_Authority,
                                                        unittest.TestCase):
    """THE DELIBERATE DECISION, stated where it can be falsified.

    "Do not treat a bound Game with a missing/mismatched Season as an unbound
    legacy case." A bound Game whose LeagueSeason row does not resolve has NO
    canonical Season, so there is nothing to lock and no authority to check.
    The tempting shortcut — fall back to ``game.season_id`` — is precisely the
    split authority this blocker is about, so the guard refuses outright.

    THE CODE IS ``regular_game_missing_league_season``, reused rather than
    minted, and chosen over ``game_league_season_mismatch`` on purpose:
    ``_revalidate_game_participation`` has raised exactly that code for exactly
    this shape since #331 review round 22, so the read guard and the write
    guard now name the SAME edge, and "the binding does not exist" stays
    distinguishable from "the binding exists and disagrees" — two different
    repairs.

    WHY THE ANSWER CHANGED, and why it is a strengthening rather than a
    weakening. Before this commit the dangling case was closed ONLY by the
    ELIGIBILITY gate, as ``not_eligible``. That gate is not reached by nine of
    the fifteen roster mutations — ``lock_roster``, ``cancel_game``,
    ``remove_player``, ``set_roster_entry_status`` and the rest consult no
    membership at all — so those ran on a dangling-bound Game having taken NO
    Season lock whatsoever. The refusal now comes from the guard, earlier, on
    every surface, and carries an actionable reason instead of a generic one.
    The surfaces that never resolve eligibility are asserted here precisely
    because ``not_eligible`` was never available to them."""

    def _pointer_on_home(self, fx, name):
        """A player whose PERMANENT pointer names HOME — a real side of this
        game — and who holds NO seasonal membership.

        Deliberately this shape, for two reasons. It is what makes the
        anti-fallback assertions SHARP: a bound-to-unbound fallback would judge
        by exactly this pointer, find it on a side, and let the write through,
        so a player the fallback WOULD have accepted is the only kind that can
        catch one. And it keeps the fixture free of
        ``season_roster_memberships`` rows, whose ``league_season_id`` carries a
        real FOREIGN KEY (migration 059) that deleting the LeagueSeason would
        otherwise violate on SQLite and PostgreSQL."""
        api = fx["api"]
        p = Player(id=api.store.next_id("player"), team_id=fx["home"],
                   name=name, position=Position.FORWARD)
        api.store.add_player(p)
        return {"id": p.id, "name": name}

    def _dangle(self, fx):
        """Delete the LeagueSeason row the Game still names, leaving
        ``game.league_season_id`` pointing at nothing."""
        store = fx["api"].store
        if isinstance(store, InMemoryStore):
            del store.league_seasons[fx["ls_id"]]
        else:
            ph = "%s" if store.backend == "postgres" else "?"
            with store.transaction():
                cur = store.conn.cursor()
                cur.execute(
                    f"DELETE FROM league_seasons WHERE id = {ph}",
                    (fx["ls_id"],))
        self.assertIsNone(store.get_league_season(fx["ls_id"]))

    def test_every_bound_surface_refuses_with_the_missing_binding_reason(self):
        ran = []
        surfaces = ("enroll_substitute", "lock_roster", "cancel_game",
                    "select_roster", "publish_game")
        for label, store in self._stores():
            try:
                self._assert_backend(label, store)
                for name in surfaces:
                    store.clear_all_data()
                    fx = self._build(store)
                    p = self._pointer_on_home(fx, "Pointer")
                    self._dangle(fx)
                    before = self._writes(fx)
                    api = fx["api"]
                    with self.subTest(backend=label, surface=name):
                        with self._write_attempts(api.store) as calls:
                            if name == "enroll_substitute":
                                res = api.enroll_substitute(fx["gid"], p["id"])
                            elif name == "lock_roster":
                                res = api.lock_roster(fx["gid"],
                                                      actor_id=ADMIN)
                            elif name == "cancel_game":
                                res = api.cancel_game(fx["gid"],
                                                      actor_id=ADMIN)
                            elif name == "select_roster":
                                res = api.select_roster(fx["gid"], [p["id"]],
                                                        actor_id=ADMIN)
                            else:
                                res = api.publish_game(fx["gid"],
                                                       actor_id=ADMIN)
                        err = self._error(res)
                        self.assertEqual(err["code"], "validation_error",
                                         (name, res))
                        self.assertEqual(
                            (err["details"] or {}).get("reason"),
                            season_guard.MISSING_LEAGUE_SEASON, (name, res))
                        self.assertEqual(calls, [], (name, calls))
                        self.assertEqual(self._writes(fx), before, name)
                        # AND it did NOT quietly become an unbound game. This
                        # player's permanent pointer names HOME, a real side of
                        # this game, so an unbound fallback would resolve them
                        # and the write would land. It must not.
                        self.assertIsNone(api.roster.resolve_membership_context(
                            api.store.get_game(fx["gid"]),
                            api.store.get_player(p["id"])), name)
                    ran.append((label, name))
            finally:
                self._close(label, store)
        self._assert_matrix_ran(ran, surfaces)

    def test_a_dangling_binding_is_refused_even_with_a_valid_own_season(self):
        """THE ANTI-FALLBACK ASSERTION. ``game.season_id`` still names S1, a
        perfectly real and ACTIVE Season. An implementation that "helpfully"
        fell back to it would find an active Season, pass, and let the write
        through — so this is the case that catches a bound-to-unbound
        fallback, and it must refuse."""
        ran = []
        for label, store in self._stores():
            try:
                self._assert_backend(label, store)
                store.clear_all_data()
                fx = self._build(store)
                p = self._pointer_on_home(fx, "Pointer")
                self._dangle(fx)
                game = fx["api"].store.get_game(fx["gid"])
                # The precondition that makes this test mean something.
                self.assertEqual(game.season_id, fx["s1"]["id"])
                self.assertEqual(
                    fx["api"].store.get_season(fx["s1"]["id"]).status,
                    SeasonStatus.ACTIVE)
                with self.subTest(backend=label):
                    res = fx["api"].enroll_substitute(fx["gid"], p["id"])
                    self.assertEqual(
                        (self._error(res)["details"] or {}).get("reason"),
                        season_guard.MISSING_LEAGUE_SEASON, res)
                ran.append((label, "anti_fallback"))
            finally:
                self._close(label, store)
        self._assert_matrix_ran(ran, ["anti_fallback"])


# ======================================================================
# 9. THE OTHER TWO SITES THE SAME DEFECT REACHED
# ======================================================================
class TheParticipationRevalidationSeasonCheckIsUnconditional(
        _Authority, unittest.TestCase):
    """``_revalidate_game_participation`` already held this invariant, behind
    an escape hatch: ``if game.season_id and ls.season_id != game.season_id``.

    The ruling: "Make ``_revalidate_game_participation``'s season comparison
    unconditional, matching its league comparison." That league comparison one
    line below was itself made unconditional by #331 review round 24, for
    exactly this NULL-evasion reason, and its comment says so — this is that
    precedent applied to the column round 23 left guarded.

    THE HELPER IS INVOKED DIRECTLY, and that is the 2026-08-23 ruling 1
    correction to this class. The first version of this test drove
    ``publish_game``, was green, and pinned NOTHING: ``publish_game`` calls
    ``_guard_game_season`` FIRST, so on a drifted Game the shared season guard
    raises ``game_league_season_mismatch`` and returns before
    ``_revalidate_game_participation`` is ever entered. Restoring the
    truthiness guard left that test green — the right reason code produced by
    the wrong mechanism, under a name and a docstring that claimed otherwise.
    The line is unreachable behind the guard at all three of its call sites
    (``setup_service`` publish/move and ``api/service``'s reschedule approval),
    so a DIRECT invocation is the only falsifiable way to pin it, and the
    guard's own boundary behaviour is pinned separately below.

    Reached with the canonical Season ACTIVE so nothing in the helper's own
    earlier ladder (the dangling-binding refusal) can answer first."""

    def test_the_helper_itself_refuses_a_null_or_sibling_game_season(self):
        """THE DIRECT, FALSIFIABLE TEST. Restoring ``if game.season_id and
        ls.season_id != game.season_id`` at setup_service ~1981 reddens the
        ``null`` variant, and the way it reddens is itself the argument for
        the unconditional form: the skipped comparison does not merely lose a
        refusal, it hands the NULL onward to
        ``_require_team_in_league_season(game.season_id, …)``, which raises
        ``DivisionMismatchError("Home is not registered in this season.")``.
        A corrupted Game identity is then reported as a Team-registration
        problem — a different exception class, a different remediation, and an
        operator sent to repair a registration that is perfectly correct."""
        ran = []
        for label, store in self._stores():
            try:
                self._assert_backend(label, store)
                for variant in VARIANTS:
                    store.clear_all_data()
                    fx = self._build(store)
                    self._drift(fx, variant)
                    api = fx["api"]
                    game = api.store.get_game(fx["gid"])
                    ls = api.store.get_league_season(fx["ls_id"])
                    with self.subTest(backend=label, variant=variant):
                        # THE PRECONDITIONS that make this test mean
                        # something: the binding RESOLVES (so the dangling
                        # refusal above cannot fire) and its Season is ACTIVE
                        # (so no lifecycle rule is doing the work).
                        self.assertIsNotNone(ls)
                        self.assertEqual(ls.season_id, fx["s1"]["id"])
                        self.assertEqual(
                            api.store.get_season(ls.season_id).status,
                            SeasonStatus.ACTIVE)
                        self.assertEqual(
                            game.season_id,
                            None if variant == "null" else fx["s2"]["id"])
                        with self._write_attempts(api.store) as calls:
                            with self.assertRaises(ValidationError) as caught:
                                api.setup._revalidate_game_participation(game)
                        details = caught.exception.details or {}
                        self.assertEqual(
                            details.get("reason"),
                            season_guard.GAME_LEAGUE_SEASON_MISMATCH, details)
                        # The STORED value is reported as-is, including
                        # ``None`` — remediation has to see the actual row.
                        self.assertEqual(
                            details.get("season_id"),
                            None if variant == "null" else fx["s2"]["id"],
                            details)
                        self.assertEqual(details.get("league_season_season_id"),
                                         fx["s1"]["id"], details)
                        # A pure pre-write check: it mutates nothing, which is
                        # the property "checked before any write" depends on.
                        self.assertEqual(calls, [], calls)
                    ran.append((label, variant))
            finally:
                self._close(label, store)
        self._assert_matrix_ran(ran, VARIANTS)

    def test_the_publish_boundary_refuses_but_from_the_shared_guard(self):
        """The BOUNDARY outcome, named for what actually produces it.

        Kept because the refusal reaching the facade is worth pinning, and
        renamed because the mechanism is the SEASON GUARD, not the helper
        above: ``_guard_game_season`` runs first and never lets
        ``_revalidate_game_participation`` see this row. Asserted here so no
        later reader mistakes this for coverage of that line — that is what
        the direct test is for."""
        ran = []
        for label, store in self._stores():
            try:
                self._assert_backend(label, store)
                for variant in VARIANTS:
                    store.clear_all_data()
                    fx = self._build(store)
                    self._drift(fx, variant)
                    api = fx["api"]
                    reached = []
                    real = api.setup._revalidate_game_participation

                    def spy(game, _r=real, _seen=reached):
                        _seen.append(game.id)
                        return _r(game)

                    api.setup._revalidate_game_participation = spy
                    try:
                        res = api.publish_game(fx["gid"], actor_id=ADMIN)
                    finally:
                        del api.setup._revalidate_game_participation
                    with self.subTest(backend=label, variant=variant):
                        self.assertEqual(
                            (self._error(res)["details"] or {}).get("reason"),
                            season_guard.GAME_LEAGUE_SEASON_MISMATCH, res)
                        # THE POINT: the helper was never entered. This is the
                        # assertion the old test lacked, and it is why the old
                        # test could not fail when the helper's guard was
                        # restored.
                        self.assertEqual(reached, [], reached)
                    ran.append((label, variant))
            finally:
                self._close(label, store)
        self._assert_matrix_ran(ran, VARIANTS)


class TheDraftBatchGuardsTheLeagueSeasonsSeason(_Authority,
                                                unittest.TestCase):
    """``ApiService._guard_active_seasons`` was fed ``[g.season_id for g in …]``
    and dropped falsy ids, so the BATCH draft paths carried the same defect in
    a third place: a planned draft whose denormalized column was NULL fell out
    of the set entirely and its archived competition went unguarded for the
    whole batch.

    Exercised through the ungated (``role=None``) branch, which is the one that
    passes its Games straight to the helper."""

    def _draft(self, fx):
        api = fx["api"]
        slot = api.create_ice_slot(fx["rink"]["id"], _at(22).isoformat(),
                                   _at(23).isoformat(), "game",
                                   actor_id=ADMIN)
        # Injected at the store, the same way test_blocker_regressions builds
        # its draft batches: the facade's create_game has no `is_draft`
        # parameter (drafts are produced by the scheduler's commit path).
        # Both columns are set COHERENTLY here; `_drift` breaks them afterwards
        # so the corruption is the test's single deliberate step.
        from hockey_scheduler.domain import Game
        g = Game(id=api.store.next_id("game"), home_team_id=fx["home"],
                 away_team_id=fx["away"], start_time=_at(22),
                 season_id=fx["s1"]["id"], league_id=fx["league"]["id"],
                 division_id=None, ice_slot_id=slot["id"],
                 league_season_id=fx["ls_id"], is_draft=True,
                 published=False)
        with api.store.transaction():
            api.store.add_game(g)
        assert api.store.get_game(g.id).is_draft
        return {"id": g.id}

    def test_a_null_game_season_draft_is_still_guarded_by_the_batch(self):
        ran = []
        for label, store in self._stores():
            try:
                self._assert_backend(label, store)
                for variant in VARIANTS:
                    store.clear_all_data()
                    fx = self._build(store)
                    draft = self._draft(fx)
                    self._drift(fx, variant, game_id=draft["id"])
                    self._archive(fx, fx["s1"])
                    with self.subTest(backend=label, variant=variant):
                        res = fx["api"].publish_draft_games(
                            game_ids=[draft["id"]], actor_id=ADMIN)
                        err = self._error(res)
                        self.assertEqual(
                            (err["details"] or {}).get("reason"),
                            season_guard.SEASON_ARCHIVED, res)
                        self.assertEqual(
                            (err["details"] or {}).get("season_id"),
                            fx["s1"]["id"], res)
                        # Still a draft: nothing was published.
                        self.assertTrue(
                            fx["api"].store.get_game(draft["id"]).is_draft)
                    ran.append((label, variant))
            finally:
                self._close(label, store)
        self._assert_matrix_ran(ran, VARIANTS)


# ======================================================================
# 10. BOUND-NESS IS AN IDENTITY TEST, NOT A TRUTHINESS TEST
# ======================================================================
class AFalsyButPresentBindingIsBoundNotUnbound(_Authority,
                                               unittest.TestCase):
    """``season_guard.game_is_league_season_bound`` answers ``is not None``,
    and the difference from ``bool(...)`` is a live authority hole rather than
    a style preference (2026-08-23 ruling 2).

    THE SHAPE IS REACHABLE. ``games.league_season_id`` is TEXT with no FOREIGN
    KEY and no CHECK (migration ``037_game_league_season.sql``), and ``""``
    round-trips unchanged through Memory, SQLite and PostgreSQL — asserted
    below on every configured backend rather than assumed. Nothing in the tree
    CONSTRUCTS one, which is exactly why no existing test caught the
    difference: `_stores` here writes it directly, the same way every other
    corruption in this file is planted.

    WHY IT MATTERS. ``""`` is not "no competition" — a LeagueSeason id is an
    opaque string, so an empty one is a CORRUPTED binding. Under ``bool(...)``
    such a row takes the UNBOUND branch, which hands it back the legacy
    permanent-pointer authority the #427 cutover took away: its own
    ``season_id`` still names a real, ACTIVE Season, so both guard families
    would find that Season, pass, and let the write land — a Game escaping its
    competition by having its binding blanked. Under ``is not None`` it is
    BOUND, the binding fails to resolve, and the refusal is
    ``regular_game_missing_league_season`` — "repair this row", which is the
    truth.

    ``None`` IS THE CONTROL, in this same test, because a rule that refused
    both would be indistinguishable from a rule that refuses everything: an
    EXHIBITION and a pre-#283 legacy row genuinely are unbound and must keep
    their own ``season_id`` as authority. The two values must part company
    here, and they do.

    BOTH GUARD FAMILIES ARE INVOKED DIRECTLY. That is deliberate and it is
    what the ruling names: the public boundaries CANNOT distinguish the two
    values, because ``_revalidate_game_participation`` raises the SAME
    ``regular_game_missing_league_season`` for a genuinely unbound REGULAR
    Game one step later. Driving ``publish_game`` for both values would show
    one code twice and prove nothing about bound-ness. The facade refusal for
    ``""`` is asserted separately, below, where it is unambiguous.
    """

    def _bind(self, fx, value):
        """Store-write ``game.league_season_id`` and PROVE the round-trip."""
        store = fx["api"].store
        with store.transaction():
            g = store.get_game(fx["gid"])
            g.league_season_id = value
            store.save_game(g)
        fresh = store.get_game(fx["gid"])
        # The premise of the whole class: the backend really did keep ``""``
        # as ``""`` and did not coerce it to NULL.
        self.assertEqual(fresh.league_season_id, value)
        self.assertIs(fresh.league_season_id is None, value is None)
        # ``season_id`` is untouched and still names a real ACTIVE Season —
        # the authority a truthiness test would fall back to.
        self.assertEqual(fresh.season_id, fx["s1"]["id"])
        self.assertEqual(store.get_season(fx["s1"]["id"]).status,
                         SeasonStatus.ACTIVE)
        return fresh

    def test_both_guard_families_treat_an_empty_binding_as_bound(self):
        """M1 — flipping ``game_is_league_season_bound`` to ``bool(...)`` must
        redden this: ``""`` would take the unbound branch, find the ACTIVE S1
        through ``game.season_id``, and return a Season instead of raising."""
        ran = []
        families = ("roster", "setup")
        for label, store in self._stores():
            try:
                self._assert_backend(label, store)
                for family in families:
                    store.clear_all_data()
                    fx = self._build(store)
                    api = fx["api"]

                    # -- the CORRUPTED binding: present, falsy, BOUND -------
                    game = self._bind(fx, "")
                    with self.subTest(backend=label, family=family,
                                      binding="empty"):
                        with self._write_attempts(api.store) as calls:
                            with self.assertRaises(ValidationError) as caught:
                                with api.store.transaction():
                                    if family == "roster":
                                        api.roster._guard_active_season(game)
                                    else:
                                        api.setup._guard_game_season(game)
                        details = caught.exception.details or {}
                        self.assertEqual(
                            details.get("reason"),
                            season_guard.MISSING_LEAGUE_SEASON, details)
                        # The stored value is reported as-is so remediation
                        # can see WHICH binding is broken.
                        self.assertEqual(details.get("league_season_id"), "",
                                         details)
                        self.assertEqual(details.get("game_id"), fx["gid"],
                                         details)
                        # ZERO write ATTEMPTS: the refusal precedes any
                        # mutation, not merely survives a rollback.
                        self.assertEqual(calls, [], calls)

                    # -- the UNBOUND CONTROL, same fixture, same guard ------
                    game = self._bind(fx, None)
                    with self.subTest(backend=label, family=family,
                                      binding="none"):
                        if family == "roster":
                            with api.store.transaction():
                                got = api.roster._guard_active_season(game)
                        else:
                            with api.store.transaction():
                                got = api.setup._guard_game_season(game)
                        # No refusal at all — the unbound branch keeps its own
                        # ``season_id`` as authority, exactly as the ruling
                        # requires preserved. Both families return the Game
                        # re-fetched under that Season's lock.
                        self.assertIsNotNone(got)
                        self.assertEqual(got.id, fx["gid"])
                        self.assertIsNone(got.league_season_id)
                    ran.append((label, family))
            finally:
                self._close(label, store)
        self._assert_matrix_ran(ran, families)

    def test_the_facade_refuses_an_empty_binding_with_zero_writes(self):
        """The same invariant where an operator meets it. Both families'
        public entry points are exercised — a roster mutation and a setup
        mutation — and both must answer ``regular_game_missing_league_season``
        having attempted no write.

        Only the ``""`` value is driven here, and the class docstring says
        why: for a genuinely unbound REGULAR Game the setup boundary raises
        the same code from ``_revalidate_game_participation``, so the facade
        is simply not a surface on which the two values differ. Bound-ness is
        distinguished at the guards, above."""
        ran = []
        surfaces = ("enroll_substitute", "lock_roster", "publish_game",
                    "delete_game")
        for label, store in self._stores():
            try:
                self._assert_backend(label, store)
                for name in surfaces:
                    store.clear_all_data()
                    fx = self._build(store)
                    api = fx["api"]
                    p = self._pointer_on_home_of(fx, "Pointer")
                    self._bind(fx, "")
                    before = self._writes(fx)
                    with self.subTest(backend=label, surface=name):
                        with self._write_attempts(api.store) as calls:
                            if name == "enroll_substitute":
                                res = api.enroll_substitute(fx["gid"], p["id"])
                            elif name == "lock_roster":
                                res = api.lock_roster(fx["gid"],
                                                      actor_id=ADMIN)
                            elif name == "publish_game":
                                res = api.publish_game(fx["gid"],
                                                       actor_id=ADMIN)
                            else:
                                res = api.delete_game(fx["gid"],
                                                      actor_id=ADMIN)
                        err = self._error(res)
                        self.assertEqual(err["code"], "validation_error",
                                         (name, res))
                        self.assertEqual(
                            (err["details"] or {}).get("reason"),
                            season_guard.MISSING_LEAGUE_SEASON, (name, res))
                        self.assertEqual(calls, [], (name, calls))
                        self.assertEqual(self._writes(fx), before, name)
                        # delete_game in particular must not have run.
                        self.assertIsNotNone(api.store.get_game(fx["gid"]),
                                             name)
                    ran.append((label, name))
            finally:
                self._close(label, store)
        self._assert_matrix_ran(ran, surfaces)

    def _pointer_on_home_of(self, fx, name):
        """A player whose PERMANENT pointer names HOME and who holds no
        seasonal membership — the shape a bound-to-unbound fallback WOULD
        accept, so ``enroll_substitute`` refusing is meaningful. Kept free of
        ``season_roster_memberships`` rows, whose ``league_season_id`` carries
        a real FOREIGN KEY (migration 059)."""
        api = fx["api"]
        p = Player(id=api.store.next_id("player"), team_id=fx["home"],
                   name=name, position=Position.FORWARD)
        api.store.add_player(p)
        return {"id": p.id, "name": name}


# ======================================================================
# 11. THE SCOPED-ROLE READ PROJECTION — Official League visibility
# ======================================================================
class AnOfficialsLeagueViewFollowsTheGamesWholeIdentity(_Authority,
                                                       unittest.TestCase):
    """``context_scope._official_league_ids`` had the SAME falsy-skip, and
    there it was a scoped role's VISIBILITY that widened (2026-08-23 ruling
    3).

    The pre-#427 line was ``if game.season_id and ls.season_id !=
    game.season_id: continue``. A NULL ``game.season_id`` skipped the
    agreement check entirely, so the Official was granted ``ls.league_id`` off
    a Game whose identity does not hold together — a scoped account
    enumerating a League no assignment of theirs actually covers, which is
    precisely what this module exists to prevent. The sibling-drift variant
    was already refused (the value was truthy and unequal); NULL was the hole.

    DRIVEN THROUGH THE PUBLIC READ SCOPE, not the private helper. The surface
    is ``ApiService.get_context_options(user_id, Role.OFFICIAL,
    {"official_id": …})`` — the context switcher's own enumeration, which is
    where an operator would actually SEE the widened League, and which reaches
    ``_official_league_ids`` through ``authorized_league_ids`` →
    ``_own_league_ids``. Pinning the boundary rather than the helper means the
    test survives a refactor of the helper and still fails if the projection
    regresses.

    THE ANCHOR ASSIGNMENT IS LOAD-BEARING. The Official's PROGRAM and SEASON
    authorization is computed by ``_official_program_seasons``, which skips any
    assigned Game with a falsy ``season_id``. With only the drifted assignment
    the Official would authorize no Program at all, ``programs`` would come
    back empty, and the "no League" assertion would pass for the wrong reason —
    it would pass even with the defect restored. A SECOND, COHERENT assignment
    in a DIFFERENT League of the same Season keeps the Program and Season
    authorized, so the only thing under test is the LEAGUE projection: the
    anchor's League must be present in every case, and the drifted Game's
    League must be present only when the Game's identity is coherent.
    """

    def _official(self, fx):
        """An Official assigned to BOTH the fixture Game (League "Elite") and
        a coherent anchor Game in a second League of the same Season."""
        api = fx["api"]
        oid = api.create_official("Ref", actor_id=ADMIN)["id"]
        anchor_league = api.create_league(fx["s1"]["id"], "Anchor",
                                          actor_id=ADMIN)
        self.assertNotIn("error", anchor_league, anchor_league)
        anchor_ls = api.store.league_season_for(anchor_league["id"],
                                                fx["s1"]["id"])
        self.assertIsNotNone(anchor_ls)
        # Built at the store, like `TheDraftBatchGuardsTheLeagueSeasonsSeason`
        # builds its drafts: both columns COHERENT, so this row is never the
        # thing under test.
        with api.store.transaction():
            anchor_gid = api.store.next_id("game")
            api.store.add_game(Game(
                id=anchor_gid, home_team_id=fx["home"],
                away_team_id=fx["away"], start_time=None,
                season_id=fx["s1"]["id"], league_id=anchor_league["id"],
                division_id=None, ice_slot_id=None,
                league_season_id=anchor_ls.id, published=True))
            for gid in (anchor_gid, fx["gid"]):
                api.store.add_official_assignment(OfficialAssignment(
                    id=api.store.next_id("assign"), game_id=gid,
                    official_id=oid, role=OfficialRole.REFEREE))
        return {"official_id": oid, "anchor_league": anchor_league["id"],
                "anchor_game": anchor_gid}

    def _leagues_seen(self, fx, oid):
        """The Leagues the PUBLIC read scope offers this Official."""
        opts = fx["api"].get_context_options(
            "official_user", Role.OFFICIAL, {"official_id": oid})
        self.assertNotIn("error", opts, opts)
        return {lg["id"] for program in opts["programs"]
                for lg in program["leagues"]}

    def test_a_coherent_assignment_grants_its_league_and_a_drifted_one_does_not(
            self):
        """M6 — restoring ``if game.season_id and ls.season_id !=
        game.season_id`` at context_scope ~197 must redden the ``null``
        variant: the Elite League would reappear in the Official's options
        off a Game whose own Season column names nothing."""
        ran = []
        cases = ("coherent",) + VARIANTS
        for label, store in self._stores():
            try:
                self._assert_backend(label, store)
                for case in cases:
                    store.clear_all_data()
                    fx = self._build(store)
                    official = self._official(fx)
                    if case != "coherent":
                        self._drift(fx, case)
                    with self.subTest(backend=label, case=case):
                        seen = self._leagues_seen(fx, official["official_id"])
                        # THE CONTROL, in every case: the anchor keeps the
                        # Program and Season authorized, so an empty result
                        # can never be mistaken for a closed projection.
                        self.assertIn(official["anchor_league"], seen,
                                      (case, seen))
                        if case == "coherent":
                            # A well-formed assigned Game DOES grant its
                            # League — without this the negative below is
                            # satisfiable by a projection that grants nothing.
                            self.assertIn(fx["league"]["id"], seen,
                                          (case, seen))
                        else:
                            self.assertNotIn(fx["league"]["id"], seen,
                                             (case, seen))
                            self.assertEqual(seen,
                                             {official["anchor_league"]},
                                             (case, seen))
                    ran.append((label, case))
            finally:
                self._close(label, store)
        self._assert_matrix_ran(ran, cases)


# ======================================================================
# 12. remind_unresponded — THE GAME IS RE-FETCHED UNDER THE LOCK
# ======================================================================
class RemindUnrespondedHonorsTheBindingItLocked(_Authority,
                                                unittest.TestCase):
    """The residual the guard rollout left behind, and its fix (2026-08-23
    ruling 4).

    THE HOLE. ``ApiService.remind_unresponded`` read the Game with a plain
    ``store.get_game`` OUTSIDE its transaction, and handed THAT object to
    ``season_guard.guard_game_season``. The guard therefore resolved and
    locked whichever Season the object's ``league_season_id`` named at the
    moment of that unlocked read, and the method then wrote notifications
    under it. Unlike the fifteen RosterService mutation sites — every one of
    which now rebinds to the row the guard returns — this site never re-read
    the Game, so a rebinding committed in the window between the locator read
    and the lock was simply never observed. The recipient list came from the
    same stale window: ``get_availability_summary`` ran before the transaction
    even opened.

    THE FIX is the roster family's own helper, reused rather than re-derived:
    ``RosterService._guard_active_season`` locks the canonical Season,
    re-fetches the Game under it, and re-runs the guard when the fresh row's
    identity columns actually moved. Recipients are then determined from that
    locked row, and the notifications are written in the same transaction.

    THE RACE, made deterministic. The one-shot instrument fires at the exact
    moment the guard takes the FIRST Season lock — the one derived from the
    stale locator read — and rebinds the Game to a second competition whose
    Season is ARCHIVED. There are no threads and no sleeps: the interleaving
    is an ordering, so it is proven by where the hook fires, not by timing.
    Under the old shape the guard has already passed on the stale object by
    then and the reminders are written against the archived competition; under
    the fix the re-fetch observes the new binding, the guard re-runs, locks the
    NEW canonical Season, finds it archived, and refuses.

    ZERO WRITES IS ASSERTED BY ATTEMPT, NOT BY DIFF. The method is one
    transaction, so a snapshot diff cannot tell "refused before writing" from
    "wrote and rolled back" — and "no stale reminders may be written" is a
    claim about what was attempted.
    """

    def _home_player(self, fx, name):
        """A player ON HOME. ``remind_unresponded`` reminds
        ``store.players_for_team(team_id)``, so the recipients must be the
        Team's own players — not the fixture's "Mover" shape, whose permanent
        pointer deliberately names THIRD."""
        api = fx["api"]
        p = Player(id=api.store.next_id("player"), team_id=fx["home"],
                   name=name, position=Position.FORWARD)
        api.store.add_player(p)
        return {"id": p.id, "name": name}

    def _second_competition(self, fx):
        """A real, ARCHIVED sibling competition to rebind the Game into: a
        League on S2 with its own LeagueSeason. Archiving S2 is what makes the
        new authority's answer differ from the old one's, so honoring it is
        observable rather than a no-op."""
        api = fx["api"]
        league2 = api.create_league(fx["s2"]["id"], "Sibling", actor_id=ADMIN)
        self.assertNotIn("error", league2, league2)
        ls2 = api.store.league_season_for(league2["id"], fx["s2"]["id"])
        self.assertIsNotNone(ls2)
        self._archive(fx, fx["s2"])
        # The premise: the ORIGINAL competition is still perfectly writable,
        # so nothing but the rebinding can produce a refusal below.
        self.assertEqual(api.store.get_season(fx["s1"]["id"]).status,
                         SeasonStatus.ACTIVE)
        return ls2

    def _rebind_at(self, store, target_season_id, new_ls, seen):
        """One-shot hook on the Season lock, keyed on the STALE authority's
        Season. Rebinding here — inside the guard, after it has resolved the
        stale object and as it takes that object's Season lock — is precisely
        the check/use window the fix closes."""
        real = store.get_season_for_update
        state = {"n": 0, "rebound": False}

        def wrapped(season_id):
            seen.append(season_id)
            if season_id == target_season_id and not state["n"]:
                state["n"] += 1
                # A COPY, deliberately. A real concurrent writer holds its own
                # row object; the in-memory store hands out the live one, and
                # mutating that would let the guard observe half the rebinding
                # through object aliasing -- an artifact of the fixture, not
                # of the code under test. Writing a copy makes all three
                # backends behave the same way a second connection does.
                game = copy.copy(store.get_game(new_ls["game_id"]))
                game.league_season_id = new_ls["ls"].id
                game.season_id = new_ls["ls"].season_id
                store.save_game(game)
                state["rebound"] = True
            return real(season_id)

        store.get_season_for_update = wrapped
        return state

    def _notification_writes(self, calls):
        return [c for c in calls if "notification" in c]

    def test_a_rebinding_after_the_locator_read_is_honored_with_no_reminders(
            self):
        ran = []
        for label, store in self._stores():
            try:
                self._assert_backend(label, store)
                store.clear_all_data()
                fx = self._build(store)
                # Two never-responded players on HOME: a non-empty recipient
                # list, so "no reminders" is a real refusal and not an
                # accidentally empty loop.
                self._home_player(fx, "Alpha")
                self._home_player(fx, "Beta")
                control = fx["api"].remind_unresponded(fx["gid"], fx["home"],
                                                       actor_id=ADMIN)
                self.assertNotIn("error", control, control)
                self.assertGreaterEqual(control["reminded"], 2, control)

                ls2 = self._second_competition(fx)
                seen = []
                fired = self._rebind_at(
                    fx["api"].store, fx["s1"]["id"],
                    {"ls": ls2, "game_id": fx["gid"]}, seen)
                before = self._writes(fx)
                try:
                    with self._write_attempts(fx["api"].store) as calls:
                        res = fx["api"].remind_unresponded(
                            fx["gid"], fx["home"], actor_id=ADMIN)
                finally:
                    del fx["api"].store.get_season_for_update

                with self.subTest(backend=label):
                    # The interleaving really happened, at the lock.
                    self.assertEqual(fired["n"], 1, seen)
                    self.assertTrue(fired["rebound"], seen)
                    # THE NEW AUTHORITY IS HONORED: the refusal names S2, the
                    # Season the Game's CURRENT binding points at — not S1,
                    # which is what the stale locator read named and what the
                    # first lock was taken on.
                    err = self._error(res)
                    details = err["details"] or {}
                    self.assertEqual(details.get("reason"),
                                     season_guard.SEASON_ARCHIVED, res)
                    self.assertEqual(details.get("season_id"),
                                     fx["s2"]["id"], res)
                    # NO STALE REMINDERS — by ATTEMPT. The rebinding write the
                    # hook itself performed is excluded by name; what must be
                    # absent is any notification write at all.
                    self.assertEqual(self._notification_writes(calls), [],
                                     calls)
                    self.assertEqual(self._writes(fx)["notifications"],
                                     before["notifications"])
                ran.append((label, "rebound"))
            finally:
                self._close(label, store)
        self._assert_matrix_ran(ran, ["rebound"])

    def test_the_recipient_list_is_read_inside_the_guarded_transaction(self):
        """The other half of ruling 4: recipients are determined under the
        lock, not from a summary read before the transaction opened.

        The hook fires at the Season lock and then sets every HOME player's
        availability. Under the old shape the recipient list had ALREADY been
        computed by ``get_availability_summary`` before the transaction, so
        those players were reminded anyway; under the fix the summary is
        computed from inside the guarded transaction and sees the responses.

        A control run without the hook proves the same call reminds two
        players when nothing changes, so a zero here is the interleaving's
        doing and not an empty fixture."""
        ran = []
        for label, store in self._stores():
            try:
                self._assert_backend(label, store)
                store.clear_all_data()
                fx = self._build(store)
                api = fx["api"]
                alpha = self._home_player(fx, "Alpha")
                beta = self._home_player(fx, "Beta")
                control = api.remind_unresponded(fx["gid"], fx["home"],
                                                 actor_id=ADMIN)
                self.assertNotIn("error", control, control)
                self.assertEqual(control["reminded"], 2, control)

                real = api.store.get_season_for_update
                state = {"n": 0}

                def wrapped(season_id, _r=real, _s=state):
                    if season_id == fx["s1"]["id"] and not _s["n"]:
                        _s["n"] += 1
                        season = _r(season_id)
                        for p in (alpha, beta):
                            got = api.set_availability(
                                fx["gid"], p["id"], "available",
                                actor_id=ADMIN)
                            assert "error" not in got, got
                        return season
                    return _r(season_id)

                api.store.get_season_for_update = wrapped
                try:
                    res = api.remind_unresponded(fx["gid"], fx["home"],
                                                 actor_id=ADMIN)
                finally:
                    del api.store.get_season_for_update

                with self.subTest(backend=label):
                    self.assertEqual(state["n"], 1)
                    self.assertNotIn("error", res, res)
                    self.assertEqual(
                        res["reminded"], 0,
                        "the recipient list was computed before the "
                        "transaction and reminded players who had already "
                        "responded by the time the lock was held")
                ran.append((label, "recipients"))
            finally:
                self._close(label, store)
        self._assert_matrix_ran(ran, ["recipients"])


if __name__ == "__main__":
    unittest.main()
