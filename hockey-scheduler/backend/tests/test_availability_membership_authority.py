"""PR #427 blocker 5387094674 — availability/reminder recipients come from the
exact game-scoped membership, never from ``Player.team_id``.

THE DEFECT THIS FILE PINS (owner comment 5387094674, reviewed at head
11835a2 — the head that had just moved ``remind_unresponded`` into one guarded
transaction):

    "ApiService._availability_summary_of still enumerates
    store.players_for_team(team_id), whose authority is the permanent
    Player.team_id. […] a player with pointer THIRD plus an active exact-Game-
    LeagueSeason HOME membership is absent from the HOME summary and receives
    no reminder, while a player with pointer HOME plus an active exact THIRD
    membership is included and receives the HOME reminder."

REPRODUCED RED at head 11835a2 on Memory, SQLite and real PostgreSQL, in the
owner's own four flags, identically on all three backends::

    current_member_listed=False      departed_member_listed=True
    current_member_notified=False    departed_member_notified=True
    reminded=1

— the ONE reminder going to the player whose seasonal participation is on
THIRD, addressed as ``player:<departed>``, while the player actually rostered
on HOME for this competition got nothing and was not even named in the private
summary the coach reads.

WHY THE TESTS ADDED ONE COMMIT EARLIER COULD NOT SEE IT, in the owner's own
words: "The new reminder tests seed pointer-HOME players and therefore do not
falsify this path." ``RemindUnrespondedHonorsTheBindingItLocked._home_player``
deliberately built players whose PERMANENT POINTER AGREED with the side they
were being reminded for. A fixture like that is satisfied by pointer discovery
and by membership discovery alike, so it is structurally incapable of failing
when discovery reads the pointer.

    ==> EVERY FIXTURE IN THIS FILE THAT IS ABOUT DISCOVERY USES THE **MOVER**
        SHAPE: the permanent ``Player.team_id`` pointer and the seasonal
        membership deliberately name DIFFERENT teams, in BOTH mirrored
        directions. Each class states which shape it uses and why. The two
        older tests were strengthened to the same shape in this commit rather
        than left in the tree as two more assertions that cannot see this class
        of defect.

WHICH POPULATION EACH SURFACE NEEDS — decided deliberately, not assumed
(the owner's instruction: "a player can be eligible-and-unresponded without
holding a roster row, and a roster row can be held by someone whose membership
has since ended. Decide deliberately and document which population each surface
needs; do not assume they coincide"):

* **availability summary + reminder recipients = LIVE ELIGIBILITY.** The
  question these two surfaces answer is "who is expected to tell this team
  whether they can play THIS game", which is asked BEFORE anyone is seated and
  of people who may never be seated. Holding a roster row is neither necessary
  (the whole point of the summary is the not-yet-selected pool) nor sufficient
  (a row survives its occupant's departure — see ``_side_data``). It is also
  the surface that DISCLOSES private per-player participation data and ROUTES
  notifications, so a departed player must drop out of it the moment their
  membership ends. Population: the exact game-scoped membership contexts,
  through the shared eligibility spine.
* **slot accounting (``RosterService._side_data``) = DURABLE ATTRIBUTION.**
  Unchanged, and deliberately NOT the same population. A seated body occupies
  a slot whatever happened to their membership afterwards; re-resolving there
  erased attribution and admitted overfill (#205 blocker 5 round 2, owner
  comment 5370391045). So ``GameRosterEntry.team_side`` still decides that
  surface, and this file does not touch it.

  The two populations therefore differ in BOTH directions, by design, and
  ``EligibilityNotSeatingDecidesTheAvailabilityPopulation`` below pins exactly
  that: an eligible player with NO roster row is asked and reminded, and a
  player holding an ACCEPTED roster row whose membership has ended is neither.

BOUND vs UNBOUND. A bound game (``league_season_id is not None``) must NEVER
fall back to the permanent pointer. An UNBOUND game — an exhibition, or an
unbound legacy row — has no membership authority at all, so the permanent
roster IS its pool, exactly as before #205.
``UnboundExhibitionKeepsThePermanentPointer`` is the MIRROR IMAGE of section 1
on the same two players, which is the strongest available statement that the
bound path is not merely "always membership".

TRI-STORE, PROVEN, PLUS THE REAL COACH HTTP JOURNEY. ``_stores`` yields
Memory, SQLite and — when TEST_DATABASE_URL is set — real PostgreSQL;
``_assert_backend`` PROVES each one rather than trusting the env var, and
``_assert_matrix_ran`` fails a loop that silently covered fewer backends than
were configured. A SKIP IS NOT A PASS.
"""

import json
import threading
import unittest
import urllib.error
import urllib.request
from http.cookiejar import CookieJar
from http.server import ThreadingHTTPServer

from helpers import BACKEND, end_membership_directly  # noqa: F401
from test_game_league_season_authority import _Authority
from test_substitute_membership_cutover import ADMIN

from hockey_scheduler.domain import (NotificationAudience, NotificationKind,
                                     Player, Position)
from hockey_scheduler.store import SqlStore
from hockey_scheduler.web import server as srv
from hockey_scheduler.web.auth import DEMO_PASSWORD, DEMO_USERS


class _AvailabilityAuthority(_Authority):
    """``_Authority``'s tri-store fixture (one Program, two sibling Seasons, a
    LeagueSeason on S1, HOME/AWAY/THIRD all registered into it, a published
    regular Game bound to that LeagueSeason) plus the availability-specific
    reads, the two player constructors (``_mover``, which ENFORCES the
    disagreeing shape, and ``_pointer_only`` for the no-membership case) and
    the one-shot Season-lock latch."""

    # -- the MOVER constructors -----------------------------------------
    def _mover(self, fx, name, pointer, membership, status="active"):
        """A player whose PERMANENT pointer names ``pointer`` and whose
        seasonal membership on THIS game's exact LeagueSeason names
        ``membership`` — and the two are asserted to DISAGREE.

        The assert is the guard, not decoration: it is what makes every
        fixture below structurally incapable of the blindness the owner
        identified in the pointer-HOME reminder tests. If a future edit makes
        the two agree, this fails loudly here rather than quietly passing a
        test that has stopped testing anything."""
        assert pointer != membership, (
            f"{name} is not a MOVER: pointer and membership both name "
            f"{pointer}, so this fixture cannot falsify pointer-based "
            "discovery — which is precisely the hole owner comment "
            "5387094674 found in the previous round's reminder tests.")
        api = fx["api"]
        p = Player(id=api.store.next_id("player"), team_id=pointer,
                   name=name, position=Position.FORWARD)
        api.store.add_player(p)
        m = api.create_season_roster_membership(
            p.id, fx["ls_id"], membership, status=status, actor_id=ADMIN)
        assert "error" not in m, m
        return {"id": p.id, "name": name, "membership_id": m["id"]}

    def _pointer_only(self, fx, name, pointer):
        """Pointer set, seasonal record SILENT — the store-level bulk-import
        shape (and any pre-059 row a migration has not backfilled). On a BOUND
        game this player has no membership authority at all, so they are not
        a candidate; on an UNBOUND game the pointer is all there is."""
        api = fx["api"]
        p = Player(id=api.store.next_id("player"), team_id=pointer,
                   name=name, position=Position.FORWARD)
        api.store.add_player(p)
        return {"id": p.id, "name": name}

    # -- the reads under test -------------------------------------------
    def _summary(self, fx, team_id, game_id=None):
        res = fx["api"].get_availability_summary(game_id or fx["gid"], team_id)
        self.assertNotIn("error", res, res)
        return res

    def _summary_ids(self, fx, team_id, game_id=None):
        return sorted(p["player_id"]
                      for p in self._summary(fx, team_id, game_id)["players"])

    def _reminders(self, fx, game_id=None):
        gid = game_id or fx["gid"]
        return [n for n in fx["api"].store.all_notifications_feed()
                if n.kind == NotificationKind.AVAILABILITY_REMINDER
                and n.game_id == gid]

    def _notified_ids(self, fx, game_id=None):
        return sorted(n.audience_ref for n in self._reminders(fx, game_id))

    def _delivery_refs(self, fx, game_id=None):
        """The DELIVERY rows the reminders fanned out to (#58) — the audience
        is not merely a field on the feed row, it is where the message is
        actually routed, and the owner's ruling asks for both."""
        store = fx["api"].store
        return sorted({d.recipient_ref
                       for n in self._reminders(fx, game_id)
                       for d in store.deliveries_for_notification(n.id)})

    def _remind(self, fx, team_id, game_id=None):
        res = fx["api"].remind_unresponded(game_id or fx["gid"], team_id,
                                           actor_id=ADMIN)
        self.assertNotIn("error", res, res)
        return res

    def _pointer_pool(self, fx, team_id):
        return sorted(p.id for p in fx["api"].store.players_for_team(team_id))

    # -- the deterministic interleaving ----------------------------------
    def _at_the_season_lock(self, store, season_id, change):
        """One-shot latch on the canonical Season row lock.

        ``remind_unresponded`` takes that lock FIRST (via
        ``_guard_active_season`` -> ``season_guard.guard_game_season``), and
        only then discovers recipients and partitions them. Firing ``change``
        here therefore lands it strictly BETWEEN the lock and the partition —
        an ORDERING, so it is proven by where the hook fires, not by timing.
        No threads and no sleeps.

        One-shot: ``change`` itself re-enters the store (the governed
        membership mutations take this very lock), and the guard flag sends
        those nested calls straight to the real method. Instance-patched;
        the caller tears it down in ``finally`` and asserts it fired."""
        real = store.get_season_for_update
        state = {"n": 0}

        def wrapped(sid, _r=real, _s=state):
            if sid == season_id and not _s["n"]:
                _s["n"] += 1
                row = _r(sid)          # take the lock FIRST, as the guard does
                change()               # …then move the world under it
                return row
            return _r(sid)

        store.get_season_for_update = wrapped
        return state


# ======================================================================
# 1. THE BLOCKER ITSELF — both mirrored directions, exact ids and audience
# ======================================================================
class MembershipNotThePointerDecidesTheAvailabilityAudience(
        _AvailabilityAuthority, unittest.TestCase):
    """The owner's exact reproduction, asserted in both directions at once.

    FIXTURE SHAPE: MOVER, mirrored. ``Current`` has pointer THIRD + an active
    HOME membership on this game's exact LeagueSeason; ``Departed`` has
    pointer HOME + an active THIRD membership. The two players are exact
    mirror images, so pointer-based discovery and membership-based discovery
    give EXACTLY OPPOSITE answers and no assertion here can be satisfied by
    both. That is the property the previous round's pointer-HOME fixtures
    lacked.

    The pointer pool is asserted to be non-empty and DIFFERENT from the
    answer, so "the summary happens to be right" cannot be an accident of an
    empty or coincident fixture.
    """

    def _pair(self, fx):
        current = self._mover(fx, "Current", pointer=fx["third"],
                              membership=fx["home"])
        departed = self._mover(fx, "Departed", pointer=fx["home"],
                               membership=fx["third"])
        return current, departed

    def test_the_home_summary_and_reminder_name_exactly_the_home_member(self):
        ran = []
        for label, store in self._stores():
            try:
                self._assert_backend(label, store)
                store.clear_all_data()
                fx = self._build(store)
                current, departed = self._pair(fx)

                summary = self._summary(fx, fx["home"])
                res = self._remind(fx, fx["home"])

                with self.subTest(backend=label):
                    # THE OWNER'S FOUR FLAGS, all four the other way round.
                    listed = {p["player_id"] for p in summary["players"]}
                    notified = set(self._notified_ids(fx))
                    self.assertIn(current["id"], listed)      # was False
                    self.assertNotIn(departed["id"], listed)  # was True
                    self.assertIn(current["id"], notified)    # was False
                    self.assertNotIn(departed["id"], notified)  # was True

                    # EXACT ids and EXACT counts, not "the right number".
                    self.assertEqual(
                        summary["players"],
                        [{"player_id": current["id"], "name": "Current",
                          "status": "no_response"}], summary)
                    self.assertEqual(
                        summary["counts"],
                        {"available": 0, "unavailable": 0, "maybe": 0,
                         "no_response": 1}, summary)

                    # EXACT notification audience and delivery routing, and
                    # ZERO notifications to the ineligible player.
                    self.assertEqual(res["reminded"], 1, res)
                    self.assertEqual(self._notified_ids(fx), [current["id"]])
                    self.assertEqual(
                        [n.audience for n in self._reminders(fx)],
                        [NotificationAudience.PLAYER])
                    self.assertEqual(self._delivery_refs(fx),
                                     [f"player:{current['id']}"])
                    self.assertNotIn(f"player:{departed['id']}",
                                     self._delivery_refs(fx))

                    # THE ANSWER IS NOT THE POINTER POOL. Both sides of this
                    # are load-bearing: the pool is non-empty (so a passing
                    # assertion is not an empty-fixture artifact) and it is a
                    # DIFFERENT set (so pointer discovery cannot satisfy the
                    # assertions above).
                    pool = self._pointer_pool(fx, fx["home"])
                    self.assertEqual(pool, [departed["id"]], pool)
                    self.assertNotEqual(sorted(listed), pool)
                ran.append((label, "mirrored"))
            finally:
                self._close(label, store)
        self._assert_matrix_ran(ran, ["mirrored"])

    def test_the_departed_player_is_answered_on_the_side_they_moved_to(self):
        """The mirror control: ``Departed`` is not universally suppressed —
        they are simply answered on THIRD, the side their membership names.
        Without this, "not in the HOME summary" could be satisfied by a bug
        that dropped them everywhere.

        THIRD is not playing in this game, so the summary for it is refused
        outright by the participation check — which is itself the assertion:
        the player's own participation lives in a competition slot this game
        has no side for. What is proven here is that HOME's answer contains
        the HOME member and AWAY's answer contains neither of them, so the
        two are partitioned rather than blanked."""
        ran = []
        for label, store in self._stores():
            try:
                self._assert_backend(label, store)
                store.clear_all_data()
                fx = self._build(store)
                current, departed = self._pair(fx)
                # A third MOVER, on the AWAY side, to prove the partition has
                # a populated other half rather than an empty one.
                away_side = self._mover(fx, "Awayman", pointer=fx["third"],
                                        membership=fx["away"])

                with self.subTest(backend=label):
                    self.assertEqual(self._summary_ids(fx, fx["home"]),
                                     [current["id"]])
                    self.assertEqual(self._summary_ids(fx, fx["away"]),
                                     [away_side["id"]])
                    # THIRD is not in this game at all.
                    bad = fx["api"].get_availability_summary(fx["gid"],
                                                             fx["third"])
                    self.assertEqual(self._error(bad)["code"],
                                     "validation_error", bad)
                    # Reminding HOME reaches the HOME member only — the AWAY
                    # member and the departed player get nothing.
                    res = self._remind(fx, fx["home"])
                    self.assertEqual(res["reminded"], 1, res)
                    self.assertEqual(self._notified_ids(fx), [current["id"]])
                    for absent in (departed["id"], away_side["id"]):
                        self.assertNotIn(absent, self._notified_ids(fx))
                ran.append((label, "partition"))
            finally:
                self._close(label, store)
        self._assert_matrix_ran(ran, ["partition"])


# ======================================================================
# 2. EVERY INELIGIBLE MEMBERSHIP SHAPE — denied, and never notified
# ======================================================================
class IneligibleMembershipShapesAreNeitherListedNorNotified(
        _AvailabilityAuthority, unittest.TestCase):
    """The shapes the ruling enumerates — inactive, terminal, missing
    registration, opponent, third-team, and no membership at all.

    FIXTURE SHAPE: MOVER throughout — every player in every case has a
    pointer and a membership that name DIFFERENT teams, and ``_mover``
    asserts it.

    * ``Control`` — pointer THIRD, active HOME membership. Must survive every
      break except the one that ends HOME's own participation. Pointer
      discovery would MISS them, so its presence in the expected answer is
      itself falsifying.
    * ``Subject`` — the shape under test. Where the break acts on a HOME
      membership the subject must HOLD one, so its pointer is AWAY; where the
      shape IS the membership (opponent/third/absent) the pointer is HOME, so
      pointer discovery would wrongly list them.
    * ``Ghost`` — pointer HOME, membership THIRD, planted in EVERY case. It
      keeps the permanent HOME pool non-empty and different from the expected
      answer even in the cases where ``Subject``'s pointer is elsewhere, so no
      case can be satisfied by pointer discovery.

    Each case runs on its OWN fixture: several of these shapes are properties
    of shared rows (the Team's registration) and must not leak between cases.
    """

    # -- the individual breaks ------------------------------------------
    def _inactive(self, fx, subject):
        r = fx["api"].set_season_roster_membership_status(
            subject["membership_id"], "inactive", reason="parked",
            actor_id=ADMIN)
        self.assertNotIn("error", r, r)

    def _terminal(self, fx, subject):
        # Every terminal transition is hard-refused by the service (#205
        # round 2 owner ruling), so an ALREADY-terminal row is constructed
        # at the store, exactly as the Slice A tests do.
        end_membership_directly(fx["api"].store, subject["membership_id"],
                                "transferred")

    def _registration_missing(self, fx, subject):
        """The Team's ``SeasonTeamRegistration`` row is GONE — what
        ``delete_season_team_registration`` and any restore that lost the
        table leave behind. It is a property of the SIDE, so this case's
        expectation is that NOBODY on HOME is asked or reminded: the whole
        side's participation has ended. Fail-closed, and still not the
        pointer pool, which is exactly what makes it a MOVER assertion."""
        store = fx["api"].store
        reg = store.registration_for_team_in_league_season(fx["ls_id"],
                                                           fx["home"])
        self.assertIsNotNone(reg)
        if isinstance(store, SqlStore):
            with store.transaction():
                store.delete_season_team_registration(reg.id)
        else:
            store.delete_season_team_registration(reg.id)

    # (case, subject pointer, subject membership, break, control survives?)
    # The subject's pointer and membership always disagree. Where the break
    # acts on a HOME membership the subject's pointer must therefore be
    # elsewhere — the falsifying pointer-HOME body in those cases is
    # ``Ghost``, planted in every case below.
    CASES = (
        ("membership_inactive", "away", "home", "_inactive", True),
        ("membership_terminal", "away", "home", "_terminal", True),
        ("registration_missing", "away", "home", "_registration_missing",
         False),
        ("membership_opponent", "home", "away", None, True),
        ("membership_third", "home", "third", None, True),
        ("membership_absent", "home", None, None, True),
    )

    def test_each_ineligible_shape_is_denied_on_every_backend(self):
        ran = []
        for label, store in self._stores():
            try:
                self._assert_backend(label, store)
                for case, ptr, side, brk, control_survives in self.CASES:
                    store.clear_all_data()
                    fx = self._build(store)
                    control = self._mover(fx, "Control", pointer=fx["third"],
                                          membership=fx["home"])
                    ghost = self._mover(fx, "Ghost", pointer=fx["home"],
                                        membership=fx["third"])
                    if side is None:
                        # No seasonal record at all, pointer on HOME.
                        subject = self._pointer_only(fx, "Subject", fx[ptr])
                    else:
                        subject = self._mover(fx, "Subject", pointer=fx[ptr],
                                              membership=fx[side])
                    # The CONTROL, before the break: membership discovery
                    # already answers correctly, so the break below is the
                    # only thing that can change the answer.
                    if brk is not None:
                        self.assertEqual(
                            self._summary_ids(fx, fx["home"]),
                            sorted([control["id"], subject["id"]]),
                            (label, case))
                        getattr(self, brk)(fx, subject)

                    expected = ([control["id"]] if control_survives else [])
                    with self.subTest(backend=label, case=case):
                        self.assertEqual(self._summary_ids(fx, fx["home"]),
                                         expected, (label, case))
                        res = self._remind(fx, fx["home"])
                        self.assertEqual(res["reminded"], len(expected),
                                         (label, case, res))
                        self.assertEqual(self._notified_ids(fx), expected,
                                         (label, case))
                        # ZERO notifications to either ineligible player, on
                        # the feed AND on the delivery queue.
                        for absent in (subject["id"], ghost["id"]):
                            self.assertNotIn(absent, self._notified_ids(fx))
                            self.assertNotIn(f"player:{absent}",
                                             self._delivery_refs(fx))
                        # The permanent pool is non-empty and is NOT the
                        # answer, so every denial here is a refusal rather
                        # than an absence — and pointer discovery could not
                        # produce ``expected`` on any of these cases.
                        pool = self._pointer_pool(fx, fx["home"])
                        self.assertIn(ghost["id"], pool)
                        self.assertNotEqual(expected, pool, (label, case))
                    ran.append((label, case))
            finally:
                self._close(label, store)
        self._assert_matrix_ran(ran, [c for c, _p, _s, _b, _k in self.CASES])


# ======================================================================
# 3. ELIGIBILITY, NOT SEATING — the population decision, made falsifiable
# ======================================================================
class EligibilityNotSeatingDecidesTheAvailabilityPopulation(
        _AvailabilityAuthority, unittest.TestCase):
    """The documented population rule, in BOTH directions the owner named.

    FIXTURE SHAPE: MOVER on all three players. ``Unseated`` is pointer THIRD
    + HOME membership and holds NO roster row; ``Stale`` is pointer THIRD +
    HOME membership, was SEATED while that membership was live, and the
    membership has since gone terminal; ``Ghost`` is pointer HOME +
    THIRD membership and is the pointer-pool body that must never appear.

    * a player can be eligible-and-unresponded WITHOUT holding a roster row
      -> ``Unseated`` is asked and reminded;
    * a roster row can be held by someone whose membership has since ended
      -> ``Stale`` is NOT asked and NOT reminded, while the row itself stays
      exactly where it was.

    The surviving row is asserted afterwards, because the OTHER surface
    (``_side_data``) still counts it: the two populations differ on purpose,
    and this test would also fail if someone "fixed" the divergence by
    deleting or re-resolving the durable row."""

    def test_a_roster_row_neither_grants_nor_survives_as_authority(self):
        ran = []
        for label, store in self._stores():
            try:
                self._assert_backend(label, store)
                store.clear_all_data()
                fx = self._build(store)
                api = fx["api"]
                unseated = self._mover(fx, "Unseated", pointer=fx["third"],
                                       membership=fx["home"])
                stale = self._mover(fx, "Stale", pointer=fx["third"],
                                    membership=fx["home"])
                ghost = self._mover(fx, "Ghost", pointer=fx["home"],
                                    membership=fx["third"])

                # Seat ``Stale`` while their membership is live — a real row
                # carrying real durable attribution.
                seated = api.select_roster(fx["gid"], [stale["id"]],
                                           actor_id=ADMIN)
                self.assertNotIn("error", seated, seated)
                entry = api.store.roster_entry_for_player(fx["gid"],
                                                          stale["id"])
                self.assertIsNotNone(entry)
                self.assertEqual(entry.attribution[0], fx["home"])
                self.assertTrue(entry.status.occupies_slot, entry.status)
                before = (entry.id, entry.player_id, entry.status.value,
                          entry.attribution)

                # …then their stint ends.
                end_membership_directly(api.store, stale["membership_id"],
                                        "transferred")

                with self.subTest(backend=label):
                    # Eligible without a row -> asked and reminded.
                    # Row without eligibility -> neither.
                    self.assertEqual(self._summary_ids(fx, fx["home"]),
                                     [unseated["id"]])
                    res = self._remind(fx, fx["home"])
                    self.assertEqual(res["reminded"], 1, res)
                    self.assertEqual(self._notified_ids(fx), [unseated["id"]])
                    self.assertEqual(self._delivery_refs(fx),
                                     [f"player:{unseated['id']}"])
                    for absent in (stale["id"], ghost["id"]):
                        self.assertNotIn(absent, self._notified_ids(fx))
                    # The pointer pool is a THIRD set again — neither the
                    # answer nor the roster occupants.
                    self.assertEqual(self._pointer_pool(fx, fx["home"]),
                                     [ghost["id"]])
                    # The DURABLE row is untouched — the OTHER surface still
                    # owns it, and this change must not have touched it.
                    still = api.store.roster_entry_for_player(fx["gid"],
                                                              stale["id"])
                    self.assertIsNotNone(still)
                    self.assertEqual(
                        (still.id, still.player_id, still.status.value,
                         still.attribution), before)
                ran.append((label, "population"))
            finally:
                self._close(label, store)
        self._assert_matrix_ran(ran, ["population"])


# ======================================================================
# 4. THE UNBOUND BRANCH — explicitly separate, and the exact mirror image
# ======================================================================
class UnboundExhibitionKeepsThePermanentPointer(_AvailabilityAuthority,
                                                unittest.TestCase):
    """An exhibition has NO LeagueSeason, therefore no membership authority,
    therefore the permanent roster IS its pool — pre-#205 behaviour, preserved
    as an EXPLICITLY separate path rather than as a fallback.

    FIXTURE SHAPE: MOVER, mirrored — the SAME two players as section 1, on
    the same store, so this class is section 1's exact photographic negative.
    On the BOUND game ``Current`` is the answer; on the UNBOUND game
    ``Departed`` is. A single-population implementation of either kind fails
    one of the two, which is what makes the split falsifiable rather than
    merely asserted."""

    def test_the_two_games_answer_oppositely_on_the_same_two_players(self):
        ran = []
        for label, store in self._stores():
            try:
                self._assert_backend(label, store)
                store.clear_all_data()
                fx = self._build(store)
                current = self._mover(fx, "Current", pointer=fx["third"],
                                      membership=fx["home"])
                departed = self._mover(fx, "Departed", pointer=fx["home"],
                                       membership=fx["third"])
                ex = self._exhibition(fx)
                self.assertIsNone(
                    fx["api"].store.get_game(ex["id"]).league_season_id)

                with self.subTest(backend=label):
                    # BOUND: membership decides.
                    self.assertEqual(self._summary_ids(fx, fx["home"]),
                                     [current["id"]])
                    # UNBOUND: the permanent pointer decides — the OPPOSITE
                    # player, on the same store, at the same moment.
                    self.assertEqual(
                        self._summary_ids(fx, fx["home"], game_id=ex["id"]),
                        [departed["id"]])

                    bound = self._remind(fx, fx["home"])
                    self.assertEqual(bound["reminded"], 1, bound)
                    self.assertEqual(self._notified_ids(fx), [current["id"]])

                    unbound = self._remind(fx, fx["home"], game_id=ex["id"])
                    self.assertEqual(unbound["reminded"], 1, unbound)
                    self.assertEqual(
                        self._notified_ids(fx, game_id=ex["id"]),
                        [departed["id"]])
                    self.assertEqual(
                        self._delivery_refs(fx, game_id=ex["id"]),
                        [f"player:{departed['id']}"])
                ran.append((label, "unbound"))
            finally:
                self._close(label, store)
        self._assert_matrix_ran(ran, ["unbound"])


# ======================================================================
# 5. A MEMBERSHIP CHANGE LANDING BEFORE THE PARTITION IS OBSERVED
# ======================================================================
class AMembershipChangeBeforeThePartitionIsObserved(_AvailabilityAuthority,
                                                    unittest.TestCase):
    """Discovery happens under the canonical Season lock, inside the ONE
    transaction 11835a2 established — so a membership change committed after
    the lock is taken and before recipients are partitioned is OBSERVED, not
    read from a stale pre-transaction pool.

    That is not incidental: ``SetupService.set_season_roster_membership_
    status`` locks the membership row AND, via ``_require_active_season``,
    the SAME canonical Season row this method holds to commit. So the lock
    already in place is precisely the one that serializes these two, and
    keeping discovery inside it is what makes the observation guaranteed
    rather than lucky.

    FIXTURE SHAPE: MOVER in both directions. The player whose membership is
    ENDED mid-transaction has pointer THIRD (so a pointer-based recipient
    list would never have contained them, and the assertion 1 -> 0 could not
    move); the player whose membership is OPENED mid-transaction also has
    pointer THIRD (so 0 -> 1 cannot be the pointer's doing either). A
    pointer-HOME bystander is present throughout and must be reminded in
    NEITHER run.

    DETERMINISTIC BY ORDERING, NOT BY TIMING: a one-shot latch on
    ``get_season_for_update`` fires at the exact moment the guard takes the
    canonical Season lock. No threads, no sleeps; the latch is asserted to
    have fired exactly once, and is torn down in ``finally``."""

    def _bystander(self, fx):
        """Pointer HOME, membership THIRD — the departed-side player who must
        never be reminded for HOME in either run. Their presence is what
        makes "reminded == 0" below a real partition rather than an empty
        store."""
        return self._mover(fx, "Bystander", pointer=fx["home"],
                           membership=fx["third"])

    def test_a_membership_ended_at_the_lock_removes_the_recipient(self):
        ran = []
        for label, store in self._stores():
            try:
                self._assert_backend(label, store)
                store.clear_all_data()
                fx = self._build(store)
                api = fx["api"]
                mover = self._mover(fx, "Mover", pointer=fx["third"],
                                    membership=fx["home"])
                bystander = self._bystander(fx)

                # CONTROL: without the latch this call reminds exactly Mover.
                control = self._remind(fx, fx["home"])
                self.assertEqual(control["reminded"], 1, control)
                self.assertEqual(self._notified_ids(fx), [mover["id"]])
                # Nobody's availability moved (a reminder does not record a
                # response), so the second call would remind Mover again —
                # which is precisely what makes a 0 below attributable to the
                # interleaving and to nothing else.
                baseline = len(self._reminders(fx))
                self.assertEqual(baseline, 1)

                def change():
                    r = api.set_season_roster_membership_status(
                        mover["membership_id"], "inactive", reason="parked",
                        actor_id=ADMIN)
                    assert "error" not in r, r

                fired = self._at_the_season_lock(api.store, fx["s1"]["id"],
                                                 change)
                try:
                    res = self._remind(fx, fx["home"])
                finally:
                    del api.store.get_season_for_update

                with self.subTest(backend=label):
                    self.assertEqual(fired["n"], 1)
                    self.assertEqual(res["reminded"], 0, res)
                    self.assertEqual(len(self._reminders(fx)), baseline)
                    # And the pointer-HOME bystander was reminded by neither
                    # call, at any point.
                    self.assertNotIn(bystander["id"], self._notified_ids(fx))
                ran.append((label, "ended"))
            finally:
                self._close(label, store)
        self._assert_matrix_ran(ran, ["ended"])

    def test_a_membership_opened_at_the_lock_adds_the_recipient(self):
        """The mirror: a stint that OPENS inside the same window is picked
        up. Without it, "0 reminders" above could be satisfied by a recipient
        list that had simply stopped working."""
        ran = []
        for label, store in self._stores():
            try:
                self._assert_backend(label, store)
                store.clear_all_data()
                fx = self._build(store)
                api = fx["api"]
                # Pointer THIRD, no seasonal record yet: invisible to BOTH
                # discovery rules at the moment the call starts.
                joiner = self._pointer_only(fx, "Joiner", fx["third"])
                bystander = self._bystander(fx)

                # CONTROL: nobody is eligible on HOME yet.
                control = self._remind(fx, fx["home"])
                self.assertEqual(control["reminded"], 0, control)

                def change():
                    m = api.create_season_roster_membership(
                        joiner["id"], fx["ls_id"], fx["home"],
                        status="active", actor_id=ADMIN)
                    assert "error" not in m, m

                fired = self._at_the_season_lock(api.store, fx["s1"]["id"],
                                                 change)
                try:
                    res = self._remind(fx, fx["home"])
                finally:
                    del api.store.get_season_for_update

                with self.subTest(backend=label):
                    self.assertEqual(fired["n"], 1)
                    self.assertEqual(res["reminded"], 1, res)
                    self.assertEqual(self._notified_ids(fx), [joiner["id"]])
                    self.assertEqual(self._delivery_refs(fx),
                                     [f"player:{joiner['id']}"])
                    self.assertNotIn(bystander["id"], self._notified_ids(fx))
                ran.append((label, "opened"))
            finally:
                self._close(label, store)
        self._assert_matrix_ran(ran, ["opened"])


# ======================================================================
# 6. THE REAL COACH HTTP JOURNEY — tri-store
# ======================================================================
class _CoachHttpHarness(_AvailabilityAuthority):
    """One real listening socket + real authenticated sessions, with
    ``srv.STATE.api`` pointed at THIS fixture's ApiService for the duration
    of each backend's case — so the request runs against Memory, SQLite and
    real PostgreSQL in turn rather than against the demo singleton's store.
    Modelled on ``test_roster_attribution_durability._HttpAvailabilityHarness``
    (same reason: a second setUpClass is a second chance to point the server
    at a store the assertions do not read)."""

    @classmethod
    def setUpClass(cls):
        cls._saved_api = srv.STATE.api
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), srv.Handler)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever,
                                      daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.thread.join(timeout=5)
        cls.httpd.server_close()
        # Process-global and shared with every other module this worker runs.
        srv.STATE.api = cls._saved_api

    def _req(self, opener, method, path, body=None):
        url = f"http://127.0.0.1:{self.port}{path}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            url, data=data, method=method,
            headers={"Content-Type": "application/json"})
        try:
            with opener.open(req) as r:
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            # Closed explicitly: an unclosed HTTPError body emits a
            # ResourceWarning at interpreter shutdown that run_parallel.py
            # deliberately does NOT filter.
            with e:
                return e.code, json.loads(e.read() or b"{}")

    def _serve(self, fx, label):
        """Point the running server at this backend's fixture and sign in a
        real COACH scoped to HOME plus a real operator."""
        api = fx["api"]
        api.accounts.create_account(
            "hcoach", DEMO_PASSWORD, DEMO_USERS["coach"],
            scope={"team_id": fx["home"]}, actor_id="test_seed")
        api.accounts.create_account(
            "hadmin", DEMO_PASSWORD, DEMO_USERS["admin"], scope={},
            actor_id="test_seed")
        srv.STATE.api = api
        openers = {}
        for user in ("hcoach", "hadmin"):
            opener = urllib.request.build_opener(
                urllib.request.HTTPCookieProcessor(CookieJar()))
            status, body = self._req(opener, "POST", "/api/auth/login",
                                     {"username": user,
                                      "password": DEMO_PASSWORD})
            self.assertEqual(status, 200, (label, user, body))
            openers[user] = opener
        return openers


class TheCoachJourneyOverRealHttpUsesMembership(_CoachHttpHarness,
                                                unittest.TestCase):
    """The whole coach journey — read the summary, press remind — over a
    real socket, real session, real role scoping, on all three backends.

    FIXTURE SHAPE: MOVER, mirrored, the same pair as section 1. The coach's
    own scope is a TEAM (``team_id``), never a player pointer, so nothing in
    the transport can accidentally supply the right answer; what the route
    returns is what the service discovered.

    Opponent denial is asserted at the transport too: the coach is refused
    the AWAY summary by the #89 team-scope check, and an operator who IS
    allowed to read it gets the AWAY side's membership answer — not the AWAY
    side's pointer pool."""

    def test_summary_and_remind_over_http_on_every_backend(self):
        ran = []
        for label, store in self._stores():
            try:
                self._assert_backend(label, store)
                store.clear_all_data()
                fx = self._build(store)
                current = self._mover(fx, "Current", pointer=fx["third"],
                                      membership=fx["home"])
                departed = self._mover(fx, "Departed", pointer=fx["home"],
                                       membership=fx["third"])
                away_side = self._mover(fx, "Awayman", pointer=fx["home"],
                                        membership=fx["away"])
                openers = self._serve(fx, label)
                coach, admin = openers["hcoach"], openers["hadmin"]

                with self.subTest(backend=label):
                    # 1. The coach reads their own summary.
                    status, body = self._req(
                        coach, "GET",
                        f"/api/games/{fx['gid']}/availability-summary"
                        f"?team_id={fx['home']}")
                    self.assertEqual(status, 200, body)
                    self.assertEqual(
                        body["players"],
                        [{"player_id": current["id"], "name": "Current",
                          "status": "no_response"}], body)
                    self.assertEqual(
                        body["counts"],
                        {"available": 0, "unavailable": 0, "maybe": 0,
                         "no_response": 1}, body)

                    # 2. OPPONENT DENIAL at the transport.
                    status, denied = self._req(
                        coach, "GET",
                        f"/api/games/{fx['gid']}/availability-summary"
                        f"?team_id={fx['away']}")
                    self.assertEqual(status, 403, denied)
                    self.assertEqual(denied["error"]["code"], "forbidden",
                                     denied)
                    # THIRD is not in this game at all — refused as a
                    # non-participant even for an operator.
                    status, bad = self._req(
                        admin, "GET",
                        f"/api/games/{fx['gid']}/availability-summary"
                        f"?team_id={fx['third']}")
                    self.assertNotEqual(status, 200, bad)
                    self.assertEqual(bad["error"]["code"], "validation_error",
                                     bad)

                    # 3. The operator's AWAY read is the AWAY MEMBERSHIP
                    #    answer, not the AWAY pointer pool (which is empty —
                    #    every mover's pointer names HOME or THIRD).
                    status, away = self._req(
                        admin, "GET",
                        f"/api/games/{fx['gid']}/availability-summary"
                        f"?team_id={fx['away']}")
                    self.assertEqual(status, 200, away)
                    self.assertEqual(
                        [p["player_id"] for p in away["players"]],
                        [away_side["id"]], away)
                    self.assertEqual(self._pointer_pool(fx, fx["away"]), [])

                    # 4. The coach presses REMIND.
                    status, res = self._req(
                        coach, "POST",
                        f"/api/games/{fx['gid']}/availability/remind",
                        {"team_id": fx["home"]})
                    self.assertEqual(status, 200, res)
                    self.assertEqual(res["reminded"], 1, res)
                    self.assertEqual(self._notified_ids(fx), [current["id"]])
                    self.assertEqual(self._delivery_refs(fx),
                                     [f"player:{current['id']}"])
                    for absent in (departed["id"], away_side["id"]):
                        self.assertNotIn(absent, self._notified_ids(fx))
                        self.assertNotIn(f"player:{absent}",
                                         self._delivery_refs(fx))
                ran.append((label, "http"))
            finally:
                self._close(label, store)
        self._assert_matrix_ran(ran, ["http"])


if __name__ == "__main__":
    unittest.main()
