"""Regression coverage for #205 blocker 5 (slot overfill).

THE DEFECT THIS FILE PINS. On top of the step-1 cutover
(``resolve_membership``/``team_for_game``/``position_for_game`` —
test_substitute_membership_cutover.py), ``_slot_summaries`` and
``compute_roster_status`` still attributed every roster entry/substitute to
"this side" via the PERMANENT ``player.team_id`` pointer, never the
game-scoped membership resolution the rest of the substitute workflow
already used. Concretely: two players whose season membership names HOME
for this exact ``league_season_id``, but whose permanent pointer still
names a THIRD team (the "Mover" shape — a mid-season transfer, or any row
where the seasonal record and the permanent pointer disagree), could BOTH
accept a HOME skater slot with ``target_skaters=1`` — the slot engine never
counted either occupied entry against HOME at all (permanent pointer said
"Third"), so ``open_skater_slots``/``confirmed_skaters`` stayed wrong
FOREVER and the second ``offer_substitute``/``accept_substitute`` that
should have been refused (``SlotAlreadyFilledError``) went through.

DEMONSTRATED FIRST (2026-08-21, pre-fix head 577de77): a standalone
tri-store script (Memory/SQLite/PostgreSQL, not part of this repo) ran the
owner's exact recipe — HOME ``target_skaters=1``, two players with
permanent pointer "Third" and an ACTIVE membership on "Home" — and got, on
all three stores: ``open_skater_slots=1`` after the first accept (should be
0), ``confirmed_skaters=0`` (should be 1), and the SECOND accept
SUCCEEDING (should raise ``SlotAlreadyFilledError``), leaving HOME seated
with 2 skaters against a target of 1.

THE FIX. ``_slot_summaries``/``compute_roster_status`` now resolve every
roster entry and substitute through the batched
``resolve_memberships_for_game`` (roster_service.py) — the same
ACTIVE-over-AFFILIATE, home-before-away precedence ``resolve_membership``
already applies per player, computed ONCE across both sides so a player
pathologically eligible on both cannot be double-counted. The GOALIE/
SKATER bucket for a roster entry now also reads the resolved membership's
SEASON-SCOPED position (falling back to the permanent ``Player.position``
only when unbound or the membership itself carries no position) instead of
the permanent ``player.slot_type`` — see ``SlotOverfillPositionBucketing``
below.

TRI-STORE. Memory + SQLite in every contract test; the PostgreSQL class
runs the same arc against a real engine. A SKIP IS NOT A PASS: the
PostgreSQL class announces loudly when TEST_DATABASE_URL is unset.
"""

import json
import os
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from helpers import BACKEND  # noqa: F401  (sets up sys.path)
from helpers import end_membership_directly, fresh_sql_store

from test_substitute_membership_cutover import ADMIN, _at, _Fixture

from hockey_scheduler.api import ApiService
from hockey_scheduler.store import InMemoryStore, SqlStore
from hockey_scheduler.web import server as srv


class _OverfillFixture(_Fixture):
    """The same Program/Season/League/three-team/venue/rink fixture
    ``_Fixture`` builds, but with the game's ``target_skaters``/
    ``target_goalies`` parameterized so the exact-count boundary (the
    owner's exact recipe: ``target_skaters=1``) can be pinned precisely."""

    def _build(self, store, target_skaters=1, target_goalies=0):
        api = ApiService(store)
        self._bind_clock(api, store)
        org = api.create_organization("Org", "O", actor_id=ADMIN)
        program = api.create_program(
            "Prog", operator_organization_id=org["id"], actor_id=ADMIN)
        season = api.create_season(program["id"], "Fall 2026", actor_id=ADMIN)
        league = api.create_league(season["id"], "Elite", actor_id=ADMIN)
        club = api.create_club("Club", actor_id=ADMIN)
        teams = {}
        for name in ("Home", "Away", "Third"):
            t = api.create_team(club["id"], None, name, actor_id=ADMIN,
                                league_id=league["id"])
            api.register_team_for_season(season["id"], t["id"],
                                         actor_id=ADMIN,
                                         league_id=league["id"])
            teams[name.lower()] = t
        venue = api.create_venue("V", organization_id=org["id"],
                                 league_id=program["id"], actor_id=ADMIN)
        api.grant_season_venue_access(season["id"], venue["id"],
                                      actor_id=ADMIN)
        rink = api.create_rink(venue["id"], "R", actor_id=ADMIN)
        slot = api.create_ice_slot(rink["id"], _at(18).isoformat(),
                                   _at(19).isoformat(), "game",
                                   actor_id=ADMIN)
        game = api.create_game(season["id"], None, teams["home"]["id"],
                               teams["away"]["id"], slot["id"],
                               target_goalies=target_goalies,
                               target_skaters=target_skaters,
                               actor_id=ADMIN, league_id=league["id"])
        assert "error" not in game, game
        assert game["league_season_id"], game
        ls_id = game["league_season_id"]
        api.publish_game(game["id"], actor_id=ADMIN)
        return api, season, league, teams, game, ls_id


class _OverfillContract(_OverfillFixture):
    """Shared TRI-STORE contract body: Memory, SQLite and — whenever
    TEST_DATABASE_URL is configured — real PostgreSQL.

    PostgreSQL used to be absent from this loop, covered only by the two
    hand-written cases in ``SlotOverfillPostgresTest`` below, so most of
    this file's contract ran on two backends while the header claimed
    tri-store. Every contract case now runs on all three
    (``_assert_backend`` PROVES which one is in hand rather than trusting
    the env var, and ``_assert_ran`` fails a loop that silently covered
    fewer backends than were configured — a vacuous tri-store claim is
    itself an open blocker against this PR).

    ``SlotOverfillPostgresTest`` is deliberately KEPT: it exercises the
    same arc against a PostgreSQL store built by ``setUp`` rather than by
    this generator, so a bug in this harness cannot silently take
    PostgreSQL coverage down with it."""

    def _stores(self):
        yield "memory", InMemoryStore()
        yield "sqlite", SqlStore(":memory:")
        url = os.environ.get("TEST_DATABASE_URL")
        if url:
            yield "postgres", fresh_sql_store(url)

    def _assert_backend(self, label, store):
        """PROVE the backend. ``skipUnless`` on the env var proves only that
        a URL was SET, never that any statement reached PostgreSQL."""
        if label == "postgres":
            self.assertIsInstance(store, SqlStore, label)
            self.assertEqual(store.backend, "postgres", store.backend)
        elif label == "sqlite":
            self.assertIsInstance(store, SqlStore, label)
            self.assertEqual(store.backend, "sqlite", store.backend)
        else:
            self.assertIsInstance(store, InMemoryStore, label)

    def _assert_ran(self, labels):
        """The loop is never silently empty, and PostgreSQL is never
        silently absent when it WAS configured."""
        expected = {"memory", "sqlite"}
        if os.environ.get("TEST_DATABASE_URL"):
            expected.add("postgres")
        else:
            print("\n[SLOT OVERFILL CONTRACT] " + _PG_SKIP)
        self.assertEqual(set(labels), expected, sorted(labels))

    def _each(self, target_skaters=1, target_goalies=0):
        ran = []
        for label, store in self._stores():
            try:
                self._assert_backend(label, store)
                # A PostgreSQL store is a REAL database reused across every
                # case in this module's worker, so each case starts from a
                # wiped one rather than inheriting the previous fixture's
                # rows.
                store.clear_all_data()
                api, season, league, teams, game, ls_id = self._build(
                    store, target_skaters=target_skaters,
                    target_goalies=target_goalies)
                ran.append(label)
                yield label, api, season, league, teams, game, ls_id
            finally:
                if isinstance(store, SqlStore):
                    if label == "postgres":
                        store.reset_schema()
                    store.close()
        self._assert_ran(ran)


class SlotOverfillIsRefused(_OverfillContract, unittest.TestCase):
    """THE demonstration, fixed: two players whose PERMANENT pointer names
    a THIRD team, but who each hold an ACTIVE membership on HOME for this
    exact ``league_season_id``, must not both seat past
    ``target_skaters=1`` — the owner's exact recipe."""

    def test_second_accept_is_refused_once_target_is_met(self):
        for label, api, season, league, teams, game, ls_id in self._each(
                target_skaters=1, target_goalies=0):
            with self.subTest(backend=label):
                p1 = self._pointer_only_player(
                    api, teams["third"]["id"], "Mover One")
                p2 = self._pointer_only_player(
                    api, teams["third"]["id"], "Mover Two")
                for p in (p1, p2):
                    m = self._membership(api, p["id"], ls_id,
                                         teams["home"]["id"])
                    assert "error" not in m, (label, m)

                status0 = api.get_roster_status(game["id"])
                self.assertEqual(status0["open_skater_slots"], 1, label)
                self.assertEqual(status0["confirmed_skaters"], 0, label)

                r1 = api.enroll_substitute(game["id"], p1["id"])
                self.assertNotIn("error", r1, (label, r1))
                o1 = api.offer_substitute(game["id"], p1["id"])
                self.assertNotIn("error", o1, (label, o1))
                a1 = api.accept_substitute(game["id"], p1["id"])
                self.assertNotIn("error", a1, (label, a1))

                # THE governed count, resolved through membership, not the
                # permanent pointer (which still says "Third" for p1).
                status1 = api.get_roster_status(game["id"])
                self.assertEqual(status1["open_skater_slots"], 0, label)
                self.assertEqual(status1["confirmed_skaters"], 1, label)

                r2 = api.enroll_substitute(game["id"], p2["id"])
                self.assertNotIn("error", r2, (label, r2))
                o2 = api.offer_substitute(game["id"], p2["id"])
                self.assertEqual(
                    o2.get("error", {}).get("code"),
                    "slot_already_filled", (label, o2))

                # The coach-override one-step path goes through the SAME
                # _require_open_slot gate and must refuse too.
                add2 = api.add_substitute_to_roster(game["id"], p2["id"])
                self.assertEqual(
                    add2.get("error", {}).get("code"),
                    "slot_already_filled", (label, add2))

                status_final = api.get_roster_status(game["id"])
                self.assertEqual(status_final["open_skater_slots"], 0, label)
                self.assertEqual(status_final["confirmed_skaters"], 1, label)
                seated = [
                    e.player_id for e in api.store.roster_for_game(game["id"])
                    if e.status.occupies_slot]
                self.assertEqual(seated, [p1["id"]], label)


class OrdinaryPointerAndMembershipAgreeing(_OverfillContract,
                                           unittest.TestCase):
    """Mirror case: a player whose PERMANENT pointer IS the correct team,
    and whose membership also matches it, counts normally — unaffected by
    the fix. Guards against a fix that over-corrects into rejecting the
    ordinary, non-Mover case."""

    def test_matching_pointer_and_membership_counts_normally(self):
        for label, api, season, league, teams, game, ls_id in self._each(
                target_skaters=1, target_goalies=0):
            with self.subTest(backend=label):
                # create_player's parity dual-write (#205) opens the ACTIVE
                # stint on the SAME team as the permanent pointer — the
                # ordinary, non-Mover shape.
                p = self._player(api, teams["home"]["id"], "Ordinary")
                r = api.enroll_substitute(game["id"], p["id"])
                self.assertNotIn("error", r, (label, r))
                o = api.offer_substitute(game["id"], p["id"])
                self.assertNotIn("error", o, (label, o))
                a = api.accept_substitute(game["id"], p["id"])
                self.assertNotIn("error", a, (label, a))
                status = api.get_roster_status(game["id"])
                self.assertEqual(status["open_skater_slots"], 0, label)
                self.assertEqual(status["confirmed_skaters"], 1, label)

                # A THIRD player, still enrolled, is correctly refused —
                # the target is met, not just "some open_skater_slots
                # arithmetic that looked right by coincidence".
                p3 = self._player(api, teams["home"]["id"], "Ordinary Two")
                r3 = api.enroll_substitute(game["id"], p3["id"])
                self.assertNotIn("error", r3, (label, r3))
                o3 = api.offer_substitute(game["id"], p3["id"])
                self.assertEqual(
                    o3.get("error", {}).get("code"),
                    "slot_already_filled", (label, o3))


class SlotOverfillPositionBucketing(_OverfillContract, unittest.TestCase):
    """#205 review blocker 2 continuation: a roster entry's GOALIE/SKATER
    bucket must read the resolved membership's SEASON-SCOPED position, not
    the permanent ``Player.position`` — proven by a mis-pointed player
    (permanent position forward/skater, season-scoped membership position
    goalie) counting against the GOALIE pool, not the SKATER pool, once
    seated."""

    def test_mis_pointed_player_counts_against_the_correct_pool(self):
        for label, api, season, league, teams, game, ls_id in self._each(
                target_skaters=0, target_goalies=1):
            with self.subTest(backend=label):
                # Permanent Player.position is forward (skater, the
                # _pointer_only_player default); the season-scoped
                # membership names goalie for THIS stint.
                p = self._pointer_only_player(
                    api, teams["home"]["id"], "Flexible")
                m = self._membership(api, p["id"], ls_id,
                                     teams["home"]["id"])
                assert "error" not in m, (label, m)
                upd = api.update_season_roster_membership(
                    m["id"], position="goalie", actor_id=ADMIN)
                assert "error" not in upd, (label, upd)

                status0 = api.get_roster_status(game["id"])
                self.assertEqual(status0["open_goalie_slots"], 1, label)
                self.assertEqual(status0["open_skater_slots"], 0, label)

                r = api.enroll_substitute(game["id"], p["id"])
                self.assertNotIn("error", r, (label, r))
                self.assertEqual(r["position"], "goalie", label)
                o = api.offer_substitute(game["id"], p["id"])
                self.assertNotIn("error", o, (label, o))
                a = api.accept_substitute(game["id"], p["id"])
                self.assertNotIn("error", a, (label, a))

                # Seated: counts against the GOALIE pool (the season-scoped
                # position), never the SKATER pool (the permanent pointer's
                # "forward") — a defect here would leave
                # open_goalie_slots=1 (never decremented) and instead
                # decrement a skater pool with target_skaters=0, going
                # negative-clamped to 0 either way, so the assertion checks
                # BOTH counters, not just one.
                status1 = api.get_roster_status(game["id"])
                self.assertEqual(status1["open_goalie_slots"], 0, label)
                self.assertEqual(status1["confirmed_goalies"], 1, label)
                self.assertEqual(status1["open_skater_slots"], 0, label)
                self.assertEqual(status1["confirmed_skaters"], 0, label)


class ReconfirmGateUsesGameScopedResolution(_OverfillContract,
                                            unittest.TestCase):
    """Round-2 gap closure: ``set_availability``'s re-confirm branch
    (roster_service.py ~L292-301) feeds ``_require_open_slot`` from
    ``position_for_game``/``_require_team_for_game`` — the SAME
    game-scoped resolution ``_side_data`` uses, never the permanent
    ``player.slot_type``/``player.team_id``. Proven with a Mover-shaped
    player (permanent pointer THIRD, membership HOME) so a defect reverting
    to the permanent pointer would consult THIRD's slot state (which this
    game never configured) instead of HOME's, and get the wrong answer in
    the wrong direction for each half of this test."""

    def test_reconfirm_after_back_out_is_gated_by_the_real_side(self):
        for label, api, season, league, teams, game, ls_id in self._each(
                target_skaters=1, target_goalies=0):
            with self.subTest(backend=label):
                mover = self._pointer_only_player(
                    api, teams["third"]["id"], "Mover")
                m = self._membership(api, mover["id"], ls_id,
                                     teams["home"]["id"])
                assert "error" not in m, (label, m)
                self.assertNotIn(
                    "error", api.enroll_substitute(game["id"], mover["id"]))
                self.assertNotIn(
                    "error", api.offer_substitute(game["id"], mover["id"]))
                self.assertNotIn(
                    "error", api.accept_substitute(game["id"], mover["id"]))
                status_seated = api.get_roster_status(game["id"])
                self.assertEqual(status_seated["confirmed_skaters"], 1, label)

                # Back out: the single skater slot re-opens.
                backed_out = api.set_availability(
                    game["id"], mover["id"], "unavailable",
                    actor_id=ADMIN)
                self.assertNotIn("error", backed_out, (label, backed_out))
                status_open = api.get_roster_status(game["id"])
                self.assertEqual(status_open["open_skater_slots"], 1, label)

                # Reconfirm while the slot is genuinely still open (HOME's
                # perspective) must SUCCEED. Reverted to the permanent
                # pointer, this would consult THIRD's slot state (no
                # roster entries, target 0) and could refuse or misbehave
                # for the wrong reason.
                reconfirm_ok = api.set_availability(
                    game["id"], mover["id"], "available",
                    actor_id=ADMIN)
                self.assertNotIn(
                    "error", reconfirm_ok, (label, reconfirm_ok))
                status_reseated = api.get_roster_status(game["id"])
                self.assertEqual(
                    status_reseated["confirmed_skaters"], 1, label)
                self.assertEqual(
                    status_reseated["open_skater_slots"], 0, label)

                # Back out again, and this time let a SECOND mover fill the
                # now-open slot before the first tries to reconfirm.
                self.assertNotIn("error", api.set_availability(
                    game["id"], mover["id"], "unavailable", actor_id=ADMIN))
                filler = self._pointer_only_player(
                    api, teams["third"]["id"], "Filler")
                fm = self._membership(api, filler["id"], ls_id,
                                      teams["home"]["id"])
                assert "error" not in fm, (label, fm)
                self.assertNotIn(
                    "error", api.enroll_substitute(game["id"], filler["id"]))
                self.assertNotIn(
                    "error", api.offer_substitute(game["id"], filler["id"]))
                self.assertNotIn(
                    "error", api.accept_substitute(game["id"], filler["id"]))

                # The slot is now genuinely filled by HOME's real count.
                # The original mover reconfirming must be REFUSED — this is
                # the direction a permanent-pointer defect gets wrong the
                # OTHER way: THIRD (no roster entries, target 0) would
                # report an "open" slot and wrongly let this succeed.
                reconfirm_refused = api.set_availability(
                    game["id"], mover["id"], "available", actor_id=ADMIN)
                self.assertEqual(
                    reconfirm_refused.get("error", {}).get("code"),
                    "slot_already_filled", (label, reconfirm_refused))


class BackOutNotifiesTheRealSideCoach(_OverfillContract, unittest.TestCase):
    """Round-2 gap closure: ``_back_out_entry``'s post-hoc
    ``compute_roster_status``/notification resolution (roster_service.py
    ~L417-431) must use the game-resolved team (HOME), never the permanent
    ``player.team_id`` pointer (THIRD, not even a side of this game) — for
    both which side's status gets recomputed and which coach the
    SLOT_OPEN notification's ``audience_ref`` names."""

    def test_open_slot_notification_names_the_real_side(self):
        # target_skaters=2: one ORDINARY player stays seated throughout, so
        # the mover's back-out leaves one occupying entry + one open slot
        # -- GameStatus.OPEN_SLOT (roster_service.py's _derive_status only
        # fires it with at least one remaining occupying entry; an
        # all-vacant roster reports DRAFT instead and never notifies).
        for label, api, season, league, teams, game, ls_id in self._each(
                target_skaters=2, target_goalies=0):
            with self.subTest(backend=label):
                anchor = self._player(api, teams["home"]["id"], "Anchor")
                self.assertNotIn(
                    "error", api.enroll_substitute(game["id"], anchor["id"]))
                self.assertNotIn(
                    "error", api.offer_substitute(game["id"], anchor["id"]))
                self.assertNotIn(
                    "error", api.accept_substitute(game["id"], anchor["id"]))

                mover = self._pointer_only_player(
                    api, teams["third"]["id"], "Mover")
                m = self._membership(api, mover["id"], ls_id,
                                     teams["home"]["id"])
                assert "error" not in m, (label, m)
                self.assertNotIn(
                    "error", api.enroll_substitute(game["id"], mover["id"]))
                self.assertNotIn(
                    "error", api.offer_substitute(game["id"], mover["id"]))
                self.assertNotIn(
                    "error", api.accept_substitute(game["id"], mover["id"]))
                self.assertEqual(
                    api.get_roster_status(game["id"])["confirmed_skaters"],
                    2, label)

                before = [(n.kind.value, n.audience_ref) for n in
                         api.store.all_notifications_feed()]

                backed_out = api.set_availability(
                    game["id"], mover["id"], "unavailable", actor_id=ADMIN)
                self.assertNotIn("error", backed_out, (label, backed_out))

                after = [(n.kind.value, n.audience_ref) for n in
                        api.store.all_notifications_feed()]
                new_events = [e for e in after if e not in before]

                # A defect reverting to the permanent pointer would resolve
                # "Third" here -- not a participant in this game at all --
                # so the open-slot alert would misfire against the wrong
                # team's coach rather than HOME's.
                self.assertIn(
                    ("roster_open_slot", teams["home"]["id"]), new_events,
                    (label, new_events))
                self.assertNotIn(
                    ("roster_open_slot", teams["third"]["id"]), new_events,
                    (label, new_events))


class BackOutSurvivesLapsedMembership(_OverfillContract, unittest.TestCase):
    """#205 blocker 3 sibling (found alongside decline_substitute's own
    identical defect — see test_substitute_membership_cutover.py's
    ``EligibilityIsLiveNotFrozen``): ``_back_out_entry``'s comment above
    (roster_service.py ~L410-417) already documents that it resolves
    ``team_for_game`` "tolerantly" so a lapsed membership degrades to
    ``team_id=None`` rather than blocking the back-out. That tolerance was
    incomplete — ``team_id=None`` still reached a COACH-audience
    ``_push_notification``, and delivery's #60 fail-closed invariant
    ("a coach notification needs an audience_ref") raised on exactly that,
    rolling the whole ``@_transactional`` back-out back. Reachable via the
    ordinary confirm/back-out surface (``PATCH roster/{playerId}/status``
    or ``set_availability``) for any confirmed player whose membership
    ends before they back out — no substitute enrollment involved at all.
    Demonstrated fresh, tri-store, on the pre-fix code (see PR
    description): identical ``validation_error`` crash and roll-back
    (entry status still ``CONFIRMED``, not ``UNAVAILABLE``) on Memory,
    SQLite AND PostgreSQL.

    ADAPTED IN BLOCKER 5 ROUND 2 — the assertion got STRONGER, not weaker.

    BEFORE: this test asserted the targeted push was SKIPPED. That was the
    best available answer at the time, because ``_back_out_entry`` resolved
    the audience with a LIVE ``team_for_game(game, player)`` lookup: once
    the mover's membership ended that returned ``None``, and the only
    choices left were "crash the back-out" (the #60 invariant raising, the
    original defect) or "send nothing". Skipping was correct relative to
    the alternatives — but it is a real product hole, and the test was
    pinning the hole: the HOME coach, who now genuinely has a skater slot
    open, was told NOTHING.

    AFTER: ``_back_out_entry`` reads ``entry.team_side`` — the side the row
    was SEATED on, recorded at selection time and unaffected by anything
    that happened to the membership since (#205 blocker 5 round 2, migration
    061). There is now an honest audience for every seated row, so the push
    FIRES and names exactly HOME. The test asserts that, plus everything it
    asserted before: the back-out still commits (entry ``unavailable``, no
    rollback), so the #60 crash this class exists to pin stays closed.

    "There is no earlier offer moment to snapshot a team onto" — the reason
    the original fix had to differ in shape from ``decline_substitute``'s —
    is no longer true: the SELECTION is that moment, and it now records the
    attribution. The skip path still exists and is still correct, but only
    for a row that carries NO durable attribution at all (a pre-061 row);
    see ``LegacyRowsWithNoAttributionFailClosed`` in
    test_roster_attribution_durability.py, which pins it there."""

    def test_back_out_after_release_notifies_the_seated_side(self):
        for label, api, season, league, teams, game, ls_id in self._each(
                target_skaters=2, target_goalies=0):
            with self.subTest(backend=label):
                anchor = self._player(api, teams["home"]["id"], "Anchor2")
                mover = self._player(api, teams["home"]["id"], "Mover2")
                sel = api.select_roster(
                    game["id"], [anchor["id"], mover["id"]], actor_id=ADMIN)
                self.assertNotIn("error", sel, (label, sel))
                for p in (anchor, mover):
                    av = api.set_availability(
                        game["id"], p["id"], "available", actor_id=p["id"])
                    self.assertNotIn("error", av, (label, av))
                self.assertEqual(
                    api.get_roster_status(game["id"])["confirmed_skaters"],
                    2, label)

                end_membership_directly(
                    api.store, self._stint_id(api, mover["id"], ls_id),
                    "released")

                before = [(n.kind.value, n.audience_ref) for n in
                         api.store.all_notifications_feed()]
                res = api.set_availability(
                    game["id"], mover["id"], "unavailable",
                    actor_id=mover["id"])
                self.assertNotIn("error", res, (label, res))

                entry = api.store.roster_entry_for_player(
                    game["id"], mover["id"])
                self.assertEqual(entry.status.value, "unavailable", label)
                # The back-out did NOT rewrite the row's attribution — a
                # status change is not a re-seat. The side it was seated on
                # is still recorded, which is the whole reason the push
                # below has an honest audience.
                self.assertEqual(entry.team_side, teams["home"]["id"], label)

                after = [(n.kind.value, n.audience_ref) for n in
                        api.store.all_notifications_feed()]
                new_events = [e for e in after if e not in before]
                # The targeted push FIRES and names the side that actually
                # lost the player — read off entry.team_side, not
                # re-resolved from a membership that has since ended (which
                # is what used to answer None and silence this alert).
                self.assertIn(
                    ("roster_open_slot", teams["home"]["id"]), new_events,
                    (label, new_events))
                # ...and to exactly ONE audience: never broadened, never the
                # opposing coach (#60 stays intact).
                open_slot = [e for e in new_events
                             if e[0] == "roster_open_slot"]
                self.assertEqual(
                    open_slot, [("roster_open_slot", teams["home"]["id"])],
                    (label, new_events))


class SlotOverfillOverHttp(unittest.TestCase):
    """The #205 blocker 5 fix exercised through a REAL HTTP request against
    ``web/server.py`` — not just the facade in isolation, matching the
    ``ResolverFixOverHttp`` pattern the step-1 cutover established.

    The owner's exact recipe (permanent pointer "Third", ACTIVE membership
    "Home", ``target_skaters=1``) is seeded via the setup facade directly
    at the live demo server state (``srv.STATE``) — setup/registration has
    no HTTP surface of its own worth exercising here — but every
    substitute-workflow step under test (enroll, offer, the first accept,
    the roster-status read, and the second offer that must now be refused)
    goes through a real socket request, using the ``X-Demo-Role`` dev-role
    header (League Admin — "full control", so neither the coach-scoped
    MANAGE_ROSTER gate nor the player-scoped RESPOND_AVAILABILITY gate is
    itself under test here) rather than a full login/session round trip."""

    def setUp(self):
        srv.STATE.reset(seed=False)
        self.api = srv.STATE.api
        fx = _OverfillFixture()
        (self.api, self.season, self.league, self.teams, self.game,
         self.ls_id) = fx._build(self.api.store, target_skaters=1,
                                 target_goalies=0)
        self.p1 = fx._pointer_only_player(
            self.api, self.teams["third"]["id"], "HTTP Mover One")
        self.p2 = fx._pointer_only_player(
            self.api, self.teams["third"]["id"], "HTTP Mover Two")
        for p in (self.p1, self.p2):
            m = fx._membership(self.api, p["id"], self.ls_id,
                               self.teams["home"]["id"])
            assert "error" not in m, m
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), srv.Handler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(
            target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self._teardown_server)

    def _teardown_server(self):
        self.httpd.shutdown()
        self.thread.join(timeout=5)
        self.httpd.server_close()

    def _req(self, method, path, body=None, role="league_admin"):
        url = f"http://127.0.0.1:{self.port}{path}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        if role is not None:
            req.add_header("X-Demo-Role", role)
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            with e:
                return e.code, json.loads(e.read() or b"{}")

    def test_second_accept_refused_over_http(self):
        gid = self.game["id"]

        status, d0 = self._req("GET", f"/api/games/{gid}/roster-status")
        self.assertEqual(status, 200, d0)
        self.assertEqual(d0["open_skater_slots"], 1, d0)
        self.assertEqual(d0["confirmed_skaters"], 0, d0)

        status, r1 = self._req(
            "POST", f"/api/games/{gid}/substitutes/enroll",
            {"player_id": self.p1["id"]})
        self.assertEqual(status, 200, r1)
        status, o1 = self._req(
            "POST", f"/api/games/{gid}/substitutes/{self.p1['id']}/offer", {})
        self.assertEqual(status, 200, o1)
        status, a1 = self._req(
            "POST", f"/api/games/{gid}/substitutes/{self.p1['id']}/accept", {})
        self.assertEqual(status, 200, a1)

        # The governed count, over the wire, resolved through membership —
        # the permanent pointer still says "Third" for p1.
        status, d1 = self._req("GET", f"/api/games/{gid}/roster-status")
        self.assertEqual(status, 200, d1)
        self.assertEqual(d1["open_skater_slots"], 0, d1)
        self.assertEqual(d1["confirmed_skaters"], 1, d1)

        status, r2 = self._req(
            "POST", f"/api/games/{gid}/substitutes/enroll",
            {"player_id": self.p2["id"]})
        self.assertEqual(status, 200, r2)
        status, o2 = self._req(
            "POST", f"/api/games/{gid}/substitutes/{self.p2['id']}/offer", {})
        self.assertEqual(o2["error"]["code"], "slot_already_filled",
                         (status, o2))
        # The coach-override one-step path is refused the same way.
        status, add2 = self._req(
            "POST",
            f"/api/games/{gid}/substitutes/{self.p2['id']}/add-to-roster", {})
        self.assertEqual(add2["error"]["code"], "slot_already_filled",
                         (status, add2))

        status, d_final = self._req("GET", f"/api/games/{gid}/roster-status")
        self.assertEqual(d_final["open_skater_slots"], 0, d_final)
        self.assertEqual(d_final["confirmed_skaters"], 1, d_final)


_PG_SKIP = ("PostgreSQL not configured (TEST_DATABASE_URL) or psycopg "
            "missing — the #205 blocker 5 (slot overfill) fix was NOT "
            "exercised on PostgreSQL. A SKIP HERE IS NOT A PASS: the "
            "membership-batch reads behind _slot_summaries are real SQL "
            "here. Set TEST_DATABASE_URL (run_parallel.py --postgres does) "
            "to run it.")


@unittest.skipUnless(os.environ.get("TEST_DATABASE_URL"), _PG_SKIP)
class SlotOverfillPostgresTest(_OverfillFixture, unittest.TestCase):
    """The owner's exact recipe against real PostgreSQL."""

    def setUp(self):
        self.store = fresh_sql_store(os.environ["TEST_DATABASE_URL"])
        self.addCleanup(self.store.close)
        (self.api, self.season, self.league, self.teams, self.game,
         self.ls_id) = self._build(self.store, target_skaters=1,
                                   target_goalies=0)

    def test_second_accept_is_refused_on_postgres(self):
        api, game, teams, ls_id = self.api, self.game, self.teams, self.ls_id
        p1 = self._pointer_only_player(api, teams["third"]["id"], "PG Mover 1")
        p2 = self._pointer_only_player(api, teams["third"]["id"], "PG Mover 2")
        for p in (p1, p2):
            m = self._membership(api, p["id"], ls_id, teams["home"]["id"])
            assert "error" not in m, m

        self.assertEqual(
            api.get_roster_status(game["id"])["open_skater_slots"], 1)

        api.enroll_substitute(game["id"], p1["id"])
        api.offer_substitute(game["id"], p1["id"])
        a1 = api.accept_substitute(game["id"], p1["id"])
        self.assertNotIn("error", a1, a1)

        status1 = api.get_roster_status(game["id"])
        self.assertEqual(status1["open_skater_slots"], 0)
        self.assertEqual(status1["confirmed_skaters"], 1)

        api.enroll_substitute(game["id"], p2["id"])
        o2 = api.offer_substitute(game["id"], p2["id"])
        self.assertEqual(o2["error"]["code"], "slot_already_filled")

        seated = [e.player_id for e in api.store.roster_for_game(game["id"])
                 if e.status.occupies_slot]
        self.assertEqual(seated, [p1["id"]])

    def test_back_out_after_release_succeeds_on_postgres(self):
        """#205 blocker 3 sibling against real PostgreSQL — see
        ``BackOutSurvivesLapsedMembership`` for the Memory/SQLite half and
        the full defect narrative. Demonstrated pre-fix on this exact
        class' fixture: ``validation_error`` crash, entry rolled back to
        ``CONFIRMED``.

        Builds its OWN ``target_skaters=2`` game rather than reusing
        ``self.game`` (``target_skaters=1`` from ``setUp``, for the
        second-accept-refused recipe above) — with only 1 target, backing
        the mover out of a 2-CONFIRMED roster leaves 1 confirmed against a
        target of 1, which is a MET target, not ``OPEN_SLOT``, so
        ``_back_out_entry``'s notification branch would never even run and
        this test would pass whether or not the bug it exists to catch was
        present. Caught by falsifiability (see PR description): with the
        production fix reverted, ``test_second_accept_is_refused_on_
        postgres`` and every OTHER new regression case still failed with
        the expected crash, but THIS one silently passed on the shared
        ``target_skaters=1`` fixture — exactly the false-negative building
        its own game now closes."""
        api, teams = self.api, self.teams
        # A second ice slot on the fixture's own rink (self.game already
        # occupies the first one) — same pattern as
        # ``UnboundGamesKeepThePermanentGate._exhibition``.
        rink_id = api.store.get_ice_slot(self.game["ice_slot_id"]).rink_id
        slot = api.create_ice_slot(
            rink_id, _at(20).isoformat(), _at(21).isoformat(), "game",
            actor_id=ADMIN)
        game = api.create_game(
            self.season["id"], None, teams["home"]["id"],
            teams["away"]["id"], slot["id"],
            target_goalies=0, target_skaters=2, actor_id=ADMIN,
            league_id=self.league["id"])
        assert "error" not in game, game
        api.publish_game(game["id"], actor_id=ADMIN)
        ls_id = self.ls_id
        anchor = self._player(api, teams["home"]["id"], "PG Anchor")
        mover = self._player(api, teams["home"]["id"], "PG Mover")
        sel = api.select_roster(
            game["id"], [anchor["id"], mover["id"]], actor_id=ADMIN)
        self.assertNotIn("error", sel, sel)
        for p in (anchor, mover):
            av = api.set_availability(
                game["id"], p["id"], "available", actor_id=p["id"])
            self.assertNotIn("error", av, av)

        end_membership_directly(
            api.store, self._stint_id(api, mover["id"], ls_id), "released")

        res = api.set_availability(
            game["id"], mover["id"], "unavailable", actor_id=mover["id"])
        self.assertNotIn("error", res, res)

        entry = api.store.roster_entry_for_player(game["id"], mover["id"])
        self.assertEqual(entry.status.value, "unavailable")


class DeclineAndBackOutAfterMembershipEndOverHttp(unittest.TestCase):
    """#205 blocker 3 (decline_substitute) and its sibling
    (``_back_out_entry``), both exercised through a REAL HTTP request
    against ``web/server.py`` — matching the pattern
    ``SlotOverfillOverHttp`` established for blocker 5. The membership-
    ending step has no HTTP surface worth exercising (a store-level-only
    precondition — see ``end_membership_directly``'s docstring), so it is
    seeded directly at the live demo server state (``srv.STATE``), like
    ``SlotOverfillOverHttp``'s own setup steps."""

    def setUp(self):
        srv.STATE.reset(seed=False)
        self.api = srv.STATE.api
        fx = _OverfillFixture()
        (self.api, self.season, self.league, self.teams, self.game,
         self.ls_id) = fx._build(self.api.store, target_skaters=2,
                                 target_goalies=0)
        self.fx = fx
        self.sub_player = fx._player(
            self.api, self.teams["home"]["id"], "HTTP Sub Offered")
        self.anchor = fx._player(
            self.api, self.teams["home"]["id"], "HTTP Anchor")
        self.mover = fx._player(
            self.api, self.teams["home"]["id"], "HTTP Mover")
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), srv.Handler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(
            target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self._teardown_server)

    def _teardown_server(self):
        self.httpd.shutdown()
        self.thread.join(timeout=5)
        self.httpd.server_close()

    def _req(self, method, path, body=None, role="league_admin"):
        url = f"http://127.0.0.1:{self.port}{path}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        if role is not None:
            req.add_header("X-Demo-Role", role)
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            with e:
                return e.code, json.loads(e.read() or b"{}")

    def test_decline_after_release_succeeds_over_http(self):
        gid, pid = self.game["id"], self.sub_player["id"]
        status, r = self._req(
            "POST", f"/api/games/{gid}/substitutes/enroll",
            {"player_id": pid})
        self.assertEqual(status, 200, r)
        status, r = self._req(
            "POST", f"/api/games/{gid}/substitutes/{pid}/offer", {})
        self.assertEqual(status, 200, r)

        stint_id = self.fx._stint_id(self.api, pid, self.ls_id)
        end_membership_directly(self.api.store, stint_id, "released")

        status, r = self._req(
            "POST", f"/api/games/{gid}/substitutes/{pid}/decline", {})
        self.assertEqual(status, 200, r)
        self.assertEqual(r["status"], "declined", r)

    def test_back_out_after_release_succeeds_over_http(self):
        gid = self.game["id"]
        aid, mid = self.anchor["id"], self.mover["id"]
        status, r = self._req(
            "POST", f"/api/games/{gid}/roster/select",
            {"player_ids": [aid, mid]})
        self.assertEqual(status, 200, r)
        for who in (aid, mid):
            status, r = self._req(
                "POST", f"/api/games/{gid}/availability",
                {"player_id": who, "availability_status": "available"})
            self.assertEqual(status, 200, r)

        stint_id = self.fx._stint_id(self.api, mid, self.ls_id)
        end_membership_directly(self.api.store, stint_id, "released")

        status, r = self._req(
            "POST", f"/api/games/{gid}/availability",
            {"player_id": mid, "availability_status": "unavailable"})
        self.assertEqual(status, 200, r)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
