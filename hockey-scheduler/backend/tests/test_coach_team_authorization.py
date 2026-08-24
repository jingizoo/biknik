"""PART B — Coach-team authorization, revalidated UNDER THE LOCK (#205).

THE BLOCKER (owner comment 5373064375)

    "bind coach-team authorization and the mutation to one transaction and one
     locked, re-fetched membership/registration context (or pass the expected
     authorized team into a service command that revalidates it under those
     locks before any write). The scope preflight may remain for fast denial,
     but it cannot be the authoritative write gate. Apply the same atomic
     contract to every coach-reachable player-targeting game mutation; do not
     special-case only enroll/withdraw."

AND THE COMPARAND RULING (owner comment 5391127041)

    "Thread `authorized_team_id` from the authenticated Coach scope into every
     Coach-reachable service command and revalidate it after `_guard_mutable`/
     the canonical Season lock, before any write. `authorized_team_id=None`
     means no Coach constraint only for already-authorized self/guardian/
     operator call paths; a Coach HTTP route must always pass its scoped team.
     Row-removal/response commands compare against durable row attribution;
     commands creating new state compare against the locked live context."

WHAT WAS MEASURED RED at head 22bd6de, tri-store. ``scope_violation``
passed while the player was still on
HOME; the membership then moved HOME->AWAY; and EVERY ONE of ten surfaces
COMMITTED anyway, because not one of them re-checked any authority inside its
transaction::

    surface                    preflight  outcome      durable
    enroll_substitute          pass       COMMITTED    sub=(enrolled, -)
    add_substitute_candidate   pass       COMMITTED    sub=(enrolled, -)
    offer_substitute           pass       COMMITTED    sub=(offered, AWAY)
    accept_substitute          pass       COMMITTED    sub=(accepted, AWAY)
    decline_substitute         pass       COMMITTED    sub=(declined, HOME)
    add_substitute_to_roster   pass       COMMITTED    entry=(accepted, AWAY)
    withdraw_substitute        pass       COMMITTED    sub=(withdrawn, -)
    select_roster              pass       COMMITTED    entry=(selected, AWAY)
    remove_player              pass       COMMITTED    entry=(removed, AWAY)
    set_availability           pass       COMMITTED    entry=(unavailable, AWAY)

The damage was durable and CROSS-TEAM, not transient: a HOME coach minted an
AWAY-owned offer and seated players into AWAY's roster.

THE COMPARAND EACH SURFACE USES, and why — the table this file pins:

  CREATE (compare against the LOCKED LIVE context)
    enroll_substitute          mints an enrollment on ctx.team_id
    add_substitute_candidate   delegates to enroll (see TRAP 1 below)
    offer_substitute           mints the offer AND its owner snapshot
    accept_substitute          seats a roster row on the live side
    add_substitute_to_roster   coach-override seating, same as accept
    select_roster              seats rows on each player's ctx.team_id
    auto_build_roster          } Part C: the ONE effective batch team,
    copy_previous_roster       } authorized before candidate discovery
    remind_unresponded         notifies one side; the requested team IS
                               the comparand (no row exists to consult)

  ROW-REMOVAL / RESPONSE (compare against DURABLE row attribution)
    withdraw_substitute        sub.team_id  (enroll-time, then offer-time)
    decline_substitute         sub.team_id  (the offer-owner snapshot)
    remove_player              entry.team_side (migration 061)
    set_availability           entry.team_side when a row exists; the live
                               context only when there is NO row to respond
                               to (nothing is seated, so nothing is owned).
                               Both branches word an unattributed refusal
                               IDENTICALLY — see F-5 below

  WHY OFFER IS A CREATE AND NOT A RESPONSE, since the enrollment row already
  exists and already names HOME: because using the durable side here would
  RE-CREATE the measured defect rather than prevent it. `offer_substitute`
  validates the slot against, and snapshots, the LIVE side; authorizing on
  the stored HOME while writing an AWAY-owned offer is exactly the "a HOME
  coach creates an AWAY-OWNED offer" row of the table above. Live for both
  keeps the coach who may offer and the team recorded as owning the offer
  identical by construction.

THE THREE TRAPS, all pinned below:
  1. ``add_substitute_candidate`` is NOT ``@_transactional`` and its
     ``substitute_block_reason`` is an UNLOCKED read outside any transaction,
     so its check lives INSIDE ``enroll_substitute`` — a check beside that
     read would be a second preflight;
  2. ``accept_substitute`` opens its own explicit transaction and writes
     EXPIRED before raising, so the refusal is taken before that write;
  3. ``decline_substitute``'s comparand is ``sub.team_id`` and never a live
     re-resolution, which would re-open the leak the standing blocker-3
     ruling closed.

TRI-STORE, PROVEN. ``_stores`` yields Memory, SQLite and — when
TEST_DATABASE_URL is set — real PostgreSQL; ``_assert_backend`` PROVES each
one, and ``_assert_ran`` fails a loop that silently covered fewer backends
than were configured. A SKIP IS NOT A PASS.

WRITE ATTEMPTS, NOT SNAPSHOT DIFFS, on every refusal. Every surface here is
``@_transactional``, so a guard placed AFTER the first write still leaves an
empty diff — the raise rolls it back on all three backends alike. "Zero
writes" is an ORDERING property and only a spy on the ATTEMPTS can see it.
The spy is ``helpers.write_attempt_spy``, shared with Part C so both files
mean the same thing by the phrase.

WHAT THE REVIEW ROUND ON THIS PR ADDED, and why each addition exists:

  F-1  ``remind_unresponded`` was LISTED in the comparand table above and
       ASSERTED NOWHERE. A reviewer deleted its gate outright and the entire
       Memory/SQLite suite stayed green (reproduced here: 234 modules, 3
       shards, 271s, OK); the same deletion now reddens
       ``RemindUnrespondedRevalidatesTheNotifiedSideUnderTheLock`` on all
       three backends. ``RemindOverRealAuthenticatedHttp`` pins the reachable
       end-to-end behaviour and says plainly what it cannot prove.

  F-3  the real-PostgreSQL interleaving covered 2 of the 14 service-reachable
       surfaces. ``ATrueInterleavingOnRealPostgreSQL`` now races ALL of them,
       in BOTH forced orders, asserting that the losing authorization leaves
       substitute, roster, availability, audit AND notification state — the
       feed and delivery tables included — identity-unchanged.

  F-5  two refusals described a case that was not theirs. See
       ``EveryRefusalDescribesItsActualCase`` for the wording, and
       ``roster_service._ATTRIBUTION_MISSING_LIVE`` for the
       existence-disclosure argument that keeps the cases merged.
"""

import json
import os
import threading
import unittest
import urllib.error
import urllib.request
from http.cookiejar import CookieJar
from http.server import ThreadingHTTPServer
from types import SimpleNamespace

from helpers import (BACKEND, FakeClock, end_membership_directly,  # noqa: F401
                     fresh_sql_store, race_with_forced_order,
                     write_attempt_spy)
from test_substitute_membership_cutover import ADMIN, _at

from hockey_scheduler.api import ApiService
from hockey_scheduler.domain import MembershipStatus, Role
from hockey_scheduler.services.roster_service import (
    _ATTRIBUTION_MISSING_DURABLE, _ATTRIBUTION_MISSING_LIVE,
    ATTRIBUTION_MISSING, TEAM_SCOPE_VIOLATION)
from hockey_scheduler.store import InMemoryStore, SqlStore
from hockey_scheduler.web import server as srv
from hockey_scheduler.web.auth import DEMO_PASSWORD, DEMO_USERS

_PG_SKIP = ("PostgreSQL not configured (TEST_DATABASE_URL); this assertion is "
            "NOT covered on the backend whose row locks it is about.")


# =========================================================================== #
# harness                                                                     #
# =========================================================================== #
class _CoachAuthHarness:

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

    def _assert_ran(self, ran, banner):
        expected = {"memory", "sqlite"}
        if os.environ.get("TEST_DATABASE_URL"):
            expected.add("postgres")
        else:
            print(f"\n[{banner}] " + _PG_SKIP)
        self.assertEqual(set(ran), expected, sorted(ran))

    # -- fixture ----------------------------------------------------------
    def _build(self, store, target_skaters=4):
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
                               target_goalies=0, target_skaters=target_skaters,
                               actor_id=ADMIN, league_id=league["id"])
        assert "error" not in game, game
        assert game["league_season_id"], game
        api.publish_game(game["id"], actor_id=ADMIN)
        return {"api": api, "season": season, "league": league, "rink": rink,
                "game": game, "gid": game["id"],
                "ls_id": game["league_season_id"],
                "home": teams["home"]["id"], "away": teams["away"]["id"],
                "third": teams["third"]["id"]}

    def _player(self, fx, team_id, name="Mo Mover"):
        p = fx["api"].create_player(team_id, name, "forward", actor_id=ADMIN)
        assert "error" not in p, p
        return p["id"]

    def _stint(self, fx, pid):
        rows = fx["api"].list_season_roster_memberships(
            player_id=pid)["memberships"]
        (m,) = [r for r in rows if r["league_season_id"] == fx["ls_id"]]
        return m["id"]

    def _move_side(self, fx, pid, to_team):
        """HOME -> AWAY: the old stint becomes TRANSFERRED history, a new
        ACTIVE stint opens on the other side of the SAME game."""
        end_membership_directly(fx["api"].store, self._stint(fx, pid),
                                "transferred")
        m = fx["api"].create_season_roster_membership(
            pid, fx["ls_id"], to_team, status="active", actor_id=ADMIN)
        assert "error" not in m, m

    def _deactivate(self, fx, pid):
        """ACTIVE -> INACTIVE on the SAME side: the player becomes eligible
        for NEITHER side, which is a different fact from moving sides."""
        end_membership_directly(fx["api"].store, self._stint(fx, pid),
                                "inactive")

    # -- observation -------------------------------------------------------
    def _write_attempts(self, store):
        """Every STORE WRITE METHOD CALLED, whether or not it survived — see
        the module docstring for why a snapshot diff cannot stand in for it.

        The implementation is ``helpers.write_attempt_spy``, shared with
        ``test_batch_effective_team`` so both files' "zero write attempts"
        claims are the same claim, checked once."""
        return write_attempt_spy(store)

    def _state(self, fx, pid):
        """Every durable class a refusal must leave untouched, as comparable
        IDENTITY values — never bare counts."""
        store, gid = fx["api"].store, fx["gid"]
        sub = store.substitute_for_player(gid, pid)
        entry = store.roster_entry_for_player(gid, pid)
        av = store.availability_for_player(gid, pid)
        return {
            "substitute": None if sub is None else (
                sub.id, sub.status.value, sub.team_id),
            "roster": None if entry is None else (
                entry.id, entry.status.value, entry.team_side),
            "availability": None if av is None else (
                av.id, av.availability_status.value),
            "audit": sorted((a.id, a.action.value)
                            for a in store.audit_for_game(gid)),
            "notifications": sorted(
                (n.id, n.type.value) for n in store.notifications_for_game(gid)),
        }

    def _feed(self, fx):
        """THE NOTIFICATION STATE THAT WOULD OTHERWISE BE WATCHED VACUOUSLY.

        ``_state``'s ``notifications`` key reads ``notifications_for_game``,
        the NotificationEvent table — and ``remind_unresponded`` NEVER WRITES
        TO IT. Its whole durable footprint is the FEED notification
        ``notifier.push`` inserts plus the delivery rows that push enqueues,
        which live in different tables entirely. A refusal test for that
        surface that watched only the event table would compare two empty
        sets and pass no matter what the gate did, so the feed and the
        delivery queue are snapshotted here as identity values and asserted
        alongside the rest."""
        store = fx["api"].store
        return {
            "feed": sorted((n.id, n.kind.value, n.audience.value,
                            n.audience_ref, n.game_id)
                           for n in store.all_notifications_feed()),
            "deliveries": sorted(
                (d.id, d.notification_id, d.channel.value, d.recipient_ref)
                for d in store.all_notification_deliveries()),
        }

    def _game_state(self, fx):
        """Every durable class this file cares about, GAME-WIDE rather than
        for one player — what a side-targeting command
        (``remind_unresponded``, the batch entry points) can touch. Identity
        values throughout, never bare counts, and the feed/delivery tables
        included for the reason ``_feed`` gives."""
        store, gid = fx["api"].store, fx["gid"]
        return {
            "substitutes": sorted(
                (s.id, s.player_id, s.status.value, s.team_id)
                for s in store.substitutes_for_game(gid)),
            "roster": sorted((e.id, e.player_id, e.status.value, e.team_side)
                             for e in store.roster_for_game(gid)),
            "availability": sorted(
                (a.id, a.player_id, a.availability_status.value)
                for a in store.availability_for_game(gid)),
            "audit": sorted((a.id, a.action.value)
                            for a in store.audit_for_game(gid)),
            "notifications": sorted(
                (n.id, n.type.value)
                for n in store.notifications_for_game(gid)),
        } | self._feed(fx)

    def _error(self, res):
        self.assertIsInstance(res, dict, res)
        self.assertIn("error", res, res)
        return res["error"]

    def _assert_refused(self, fx, pid, res, calls, before, reason):
        """ONE refusal contract, asserted the same way everywhere: the exact
        code and machine-readable reason, ZERO write ATTEMPTS, and every
        durable class unchanged."""
        err = self._error(res)
        self.assertEqual(err["code"], "forbidden", res)
        self.assertEqual((err.get("details") or {}).get("reason"), reason, res)
        self.assertEqual(calls, [], calls)
        self.assertEqual(self._state(fx, pid), before)


# =========================================================================== #
# the surface table                                                           #
# =========================================================================== #
def _prepare(h, fx, pid, kind):
    """Put the row in the state each surface needs WHILE the player is still
    on HOME, so the HOME coach is genuinely the owner at that moment."""
    api = fx["api"]
    if kind in ("offer", "withdraw_enrolled", "add_to_roster"):
        api.enroll_substitute(fx["gid"], pid, ADMIN)
    elif kind in ("accept", "decline", "withdraw_offered"):
        api.enroll_substitute(fx["gid"], pid, ADMIN)
        api.offer_substitute(fx["gid"], pid, ADMIN)
    elif kind in ("remove", "availability"):
        api.select_roster(fx["gid"], [pid], ADMIN)


def _call(api, fx, pid, kind, team):
    g = fx["gid"]
    return {
        "enroll": lambda: api.enroll_substitute(
            g, pid, "coach", authorized_team_id=team),
        "add_candidate": lambda: api.add_substitute_candidate(
            g, pid, "coach", authorized_team_id=team),
        "offer": lambda: api.offer_substitute(
            g, pid, "coach", authorized_team_id=team),
        "accept": lambda: api.accept_substitute(
            g, pid, "coach", authorized_team_id=team),
        "decline": lambda: api.decline_substitute(
            g, pid, "coach", authorized_team_id=team),
        "add_to_roster": lambda: api.add_substitute_to_roster(
            g, pid, "coach", authorized_team_id=team),
        "withdraw_enrolled": lambda: api.withdraw_substitute(
            g, pid, "coach", authorized_team_id=team),
        "withdraw_offered": lambda: api.withdraw_substitute(
            g, pid, "coach", authorized_team_id=team),
        "select": lambda: api.select_roster(
            g, [pid], "coach", authorized_team_id=team),
        "remove": lambda: api.remove_player(
            g, pid, "coach", authorized_team_id=team),
        "availability": lambda: api.set_availability(
            g, pid, "unavailable", "coach", "coach", authorized_team_id=team),
    }[kind]()


# Every Coach-reachable player-targeting surface, with the comparand kind the
# ruling assigns it. "create" = compare against the locked live context;
# "response" = compare against durable row attribution.
CREATE_SURFACES = ("enroll", "add_candidate", "offer", "accept",
                   "add_to_roster", "select")
RESPONSE_SURFACES = ("withdraw_enrolled", "withdraw_offered", "decline",
                     "remove", "availability")


class CreateCommandsCompareAgainstTheLockedLiveContext(_CoachAuthHarness,
                                                       unittest.TestCase):
    """After HOME->AWAY, the side that may CREATE state for this player is
    the side they are genuinely on now — so the HOME coach is refused with
    zero write attempts, and the AWAY coach may act.

    This is the half of the measured table where the old code let a HOME
    coach mint AWAY-owned offers and seat players into AWAY's roster."""

    def test_home_coach_is_refused_after_the_player_moves_to_away(self):
        ran = []
        for label, store in self._stores():
            try:
                self._assert_backend(label, store)
                for kind in CREATE_SURFACES:
                    store.clear_all_data()
                    fx = self._build(store)
                    pid = self._player(fx, fx["home"])
                    _prepare(self, fx, pid, kind)
                    self._move_side(fx, pid, fx["away"])
                    before = self._state(fx, pid)
                    with self.subTest(backend=label, surface=kind):
                        with self._write_attempts(fx["api"].store) as calls:
                            res = _call(fx["api"], fx, pid, kind, fx["home"])
                        self._assert_refused(fx, pid, res, calls, before,
                                             TEAM_SCOPE_VIOLATION)
                ran.append(label)
            finally:
                self._close(label, store)
        self._assert_ran(ran, "CREATE / LIVE COMPARAND")

    def test_the_side_the_player_is_now_on_may_act(self):
        """The refusal above is about AUTHORITY, not about the transition
        being impossible: the AWAY coach — who the player now genuinely
        plays for — is allowed through the same call."""
        ran = []
        for label, store in self._stores():
            try:
                self._assert_backend(label, store)
                for kind in CREATE_SURFACES:
                    store.clear_all_data()
                    fx = self._build(store)
                    pid = self._player(fx, fx["home"])
                    _prepare(self, fx, pid, kind)
                    self._move_side(fx, pid, fx["away"])
                    with self.subTest(backend=label, surface=kind):
                        res = _call(fx["api"], fx, pid, kind, fx["away"])
                        if isinstance(res, dict):
                            self.assertNotIn("error", res, (kind, res))
                ran.append(label)
            finally:
                self._close(label, store)
        self._assert_ran(ran, "CREATE / LIVE COMPARAND (POSITIVE)")


class ResponseCommandsCompareAgainstDurableRowAttribution(_CoachAuthHarness,
                                                          unittest.TestCase):
    """THE ORDINARY CLEANUP PATH, preserved exactly as the ruling requires:
    "This preserves the ordinary cleanup path after transfer/inactivation and
    prevents a row from silently changing owners because eligibility later
    changed."

    After HOME->AWAY the row is still HOME's — so HOME's coach may clean it
    up and AWAY's coach may NOT touch it, which is the precise opposite of
    the create table above and the reason one rule cannot serve both."""

    def test_the_owning_coach_still_cleans_up_after_a_transfer(self):
        ran = []
        for label, store in self._stores():
            try:
                self._assert_backend(label, store)
                for kind in RESPONSE_SURFACES:
                    store.clear_all_data()
                    fx = self._build(store)
                    pid = self._player(fx, fx["home"])
                    _prepare(self, fx, pid, kind)
                    self._move_side(fx, pid, fx["away"])
                    with self.subTest(backend=label, surface=kind):
                        res = _call(fx["api"], fx, pid, kind, fx["home"])
                        self.assertNotIn("error", res, (kind, res))
                ran.append(label)
            finally:
                self._close(label, store)
        self._assert_ran(ran, "RESPONSE / DURABLE COMPARAND (CLEANUP)")

    def test_the_opposing_coach_cannot_take_the_row_over(self):
        ran = []
        for label, store in self._stores():
            try:
                self._assert_backend(label, store)
                for kind in RESPONSE_SURFACES:
                    store.clear_all_data()
                    fx = self._build(store)
                    pid = self._player(fx, fx["home"])
                    _prepare(self, fx, pid, kind)
                    self._move_side(fx, pid, fx["away"])
                    before = self._state(fx, pid)
                    with self.subTest(backend=label, surface=kind):
                        with self._write_attempts(fx["api"].store) as calls:
                            res = _call(fx["api"], fx, pid, kind, fx["away"])
                        self._assert_refused(fx, pid, res, calls, before,
                                             TEAM_SCOPE_VIOLATION)
                ran.append(label)
            finally:
                self._close(label, store)
        self._assert_ran(ran, "RESPONSE / DURABLE COMPARAND (OPPONENT)")

    def test_cleanup_survives_deactivation_too(self):
        """ACTIVE->INACTIVE is a DIFFERENT fact from moving sides: the
        player becomes eligible for NEITHER side, so a live comparand would
        have nothing to answer with and would refuse the owner. The durable
        side still names HOME, so HOME's cleanup still works — and the
        opponent still cannot reach the row."""
        ran = []
        for label, store in self._stores():
            try:
                self._assert_backend(label, store)
                for kind in RESPONSE_SURFACES:
                    store.clear_all_data()
                    fx = self._build(store)
                    pid = self._player(fx, fx["home"])
                    _prepare(self, fx, pid, kind)
                    self._deactivate(fx, pid)
                    with self.subTest(backend=label, surface=kind):
                        res = _call(fx["api"], fx, pid, kind, fx["home"])
                        self.assertNotIn("error", res, (kind, res))
                    # ...and the opponent is still refused, zero writes.
                    store.clear_all_data()
                    fx = self._build(store)
                    pid = self._player(fx, fx["home"])
                    _prepare(self, fx, pid, kind)
                    self._deactivate(fx, pid)
                    before = self._state(fx, pid)
                    with self.subTest(backend=label, surface=kind,
                                      side="opponent"):
                        with self._write_attempts(fx["api"].store) as calls:
                            res = _call(fx["api"], fx, pid, kind, fx["away"])
                        self._assert_refused(fx, pid, res, calls, before,
                                             TEAM_SCOPE_VIOLATION)
                ran.append(label)
            finally:
                self._close(label, store)
        self._assert_ran(ran, "RESPONSE / DEACTIVATION")


class DeclineReadsTheOfferOwnerSnapshotAndNeverALiveLookup(_CoachAuthHarness,
                                                           unittest.TestCase):
    """TRAP 3. ``decline_substitute``'s comparand is ``sub.team_id`` — the
    side ``offer_substitute`` validated the offer against — for the whole
    lifetime of the offer, exactly as the standing #205 blocker-3 ruling
    already requires of its notification audience.

    A LIVE comparand would let the coach the player has just MOVED TO decline
    an offer that was never theirs, and would lock out the coach who issued
    it and must advance their own queue."""

    def test_the_issuing_coach_declines_and_the_new_side_cannot(self):
        ran = []
        for label, store in self._stores():
            try:
                self._assert_backend(label, store)
                store.clear_all_data()
                fx = self._build(store)
                pid = self._player(fx, fx["home"])
                fx["api"].enroll_substitute(fx["gid"], pid, ADMIN)
                fx["api"].offer_substitute(fx["gid"], pid, ADMIN)
                self.assertEqual(
                    fx["api"].store.substitute_for_player(fx["gid"], pid).team_id,
                    fx["home"], label)
                self._move_side(fx, pid, fx["away"])
                with self.subTest(backend=label, side="away"):
                    before = self._state(fx, pid)
                    with self._write_attempts(fx["api"].store) as calls:
                        res = fx["api"].decline_substitute(
                            fx["gid"], pid, "coach",
                            authorized_team_id=fx["away"])
                    self._assert_refused(fx, pid, res, calls, before,
                                         TEAM_SCOPE_VIOLATION)
                with self.subTest(backend=label, side="home"):
                    res = fx["api"].decline_substitute(
                        fx["gid"], pid, "coach", authorized_team_id=fx["home"])
                    self.assertNotIn("error", res, res)
                ran.append(label)
            finally:
                self._close(label, store)
        self._assert_ran(ran, "DECLINE / OFFER-OWNER COMPARAND")


class LegacyNullAttributionFailsClosedForACoachOnly(_CoachAuthHarness,
                                                    unittest.TestCase):
    """"A Coach acting on a NULL-attribution enrollment fails closed with
    structured ``attribution_missing`` and zero writes. Player self-service
    may still withdraw its own enrollment, and an unscoped League Admin may
    act under its existing authority."

    The legacy shapes are modelled the way migrations 060/061 actually leave
    them — the column present and NULL, no backfill — and the NULL is proven
    to survive the database round trip so this is not testing an in-process
    object."""

    def _null_substitute(self, fx, pid):
        store = fx["api"].store
        with store.transaction():
            row = store.substitute_for_player(fx["gid"], pid)
            row.team_id = None
            store.save_substitute(row)
        self.assertIsNone(store.substitute_for_player(fx["gid"], pid).team_id)

    def _null_entry(self, fx, pid):
        store = fx["api"].store
        with store.transaction():
            row = store.roster_entry_for_player(fx["gid"], pid)
            row.team_side = None
            row.seated_position = None
            store.save_roster_entry(row)
        self.assertIsNone(
            store.roster_entry_for_player(fx["gid"], pid).team_side)

    def test_coach_is_refused_attribution_missing_with_zero_writes(self):
        ran = []
        for label, store in self._stores():
            try:
                self._assert_backend(label, store)
                for kind in RESPONSE_SURFACES:
                    store.clear_all_data()
                    fx = self._build(store)
                    pid = self._player(fx, fx["home"])
                    _prepare(self, fx, pid, kind)
                    if kind in ("remove", "availability"):
                        self._null_entry(fx, pid)
                    else:
                        self._null_substitute(fx, pid)
                    before = self._state(fx, pid)
                    with self.subTest(backend=label, surface=kind):
                        with self._write_attempts(fx["api"].store) as calls:
                            res = _call(fx["api"], fx, pid, kind, fx["home"])
                        self._assert_refused(fx, pid, res, calls, before,
                                             ATTRIBUTION_MISSING)
                ran.append(label)
            finally:
                self._close(label, store)
        self._assert_ran(ran, "LEGACY NULL / COACH FAILS CLOSED")

    def test_player_self_service_still_withdraws_its_own_enrollment(self):
        """``authorized_team_id=None`` — the Player self-service route's own
        value — imposes NO team constraint, because that caller's authority
        was established by ``_require_player_scope`` and never came from
        this column."""
        ran = []
        for label, store in self._stores():
            try:
                self._assert_backend(label, store)
                store.clear_all_data()
                fx = self._build(store)
                pid = self._player(fx, fx["home"])
                fx["api"].enroll_substitute(fx["gid"], pid, ADMIN)
                self._null_substitute(fx, pid)
                with self.subTest(backend=label):
                    res = fx["api"].withdraw_substitute(fx["gid"], pid, pid)
                    self.assertNotIn("error", res, res)
                    self.assertEqual(
                        store.substitute_for_player(fx["gid"], pid).status.value,
                        "withdrawn", label)
                ran.append(label)
            finally:
                self._close(label, store)
        self._assert_ran(ran, "LEGACY NULL / PLAYER SELF-SERVICE")

    def test_unscoped_league_admin_still_acts_on_a_legacy_row(self):
        ran = []
        for label, store in self._stores():
            try:
                self._assert_backend(label, store)
                store.clear_all_data()
                fx = self._build(store)
                pid = self._player(fx, fx["home"])
                fx["api"].select_roster(fx["gid"], [pid], ADMIN)
                self._null_entry(fx, pid)
                with self.subTest(backend=label):
                    res = fx["api"].remove_player(fx["gid"], pid, ADMIN)
                    self.assertNotIn("error", res, res)
                ran.append(label)
            finally:
                self._close(label, store)
        self._assert_ran(ran, "LEGACY NULL / LEAGUE ADMIN")


class NoCoachConstraintLeavesEverySurfaceByteForByteUnCHANGED(_CoachAuthHarness,
                                                              unittest.TestCase):
    """``authorized_team_id=None`` MEANS NO COACH CONSTRAINT — the default,
    so no call site is silently gated by omission, and the value the Player
    self-service, Guardian and unscoped League Admin paths all carry.

    Asserted as a WHOLE-STATE comparison against the pre-#205 expectation for
    each surface: every one of them still succeeds on a player who has moved
    to the other side, exactly as before, because none of these callers'
    authority ever came from a team."""

    def test_every_surface_still_succeeds_unconstrained(self):
        ran = []
        for label, store in self._stores():
            try:
                self._assert_backend(label, store)
                for kind in CREATE_SURFACES + RESPONSE_SURFACES:
                    store.clear_all_data()
                    fx = self._build(store)
                    pid = self._player(fx, fx["home"])
                    _prepare(self, fx, pid, kind)
                    self._move_side(fx, pid, fx["away"])
                    with self.subTest(backend=label, surface=kind):
                        res = _call(fx["api"], fx, pid, kind, None)
                        if isinstance(res, dict):
                            self.assertNotIn("error", res, (kind, res))
                ran.append(label)
            finally:
                self._close(label, store)
        self._assert_ran(ran, "UNCONSTRAINED / NONE")


class TheNonTransactionalWrappersCheckInsideTheirCallee(_CoachAuthHarness,
                                                        unittest.TestCase):
    """TRAPS 1 AND 2, pinned as ORDERING facts rather than as prose.

    ``add_substitute_candidate`` runs an UNLOCKED ``substitute_block_reason``
    outside any transaction, and ``accept_substitute`` writes EXPIRED before
    it raises. In both, a check placed in the wrapper would be a second
    preflight or would land after a write; the refusal must come from inside
    the transactional callee, with nothing written."""

    def test_coach_add_refuses_from_inside_enroll_with_zero_writes(self):
        ran = []
        for label, store in self._stores():
            try:
                self._assert_backend(label, store)
                store.clear_all_data()
                fx = self._build(store)
                pid = self._player(fx, fx["home"])
                self._move_side(fx, pid, fx["away"])
                before = self._state(fx, pid)
                with self.subTest(backend=label):
                    with self._write_attempts(fx["api"].store) as calls:
                        res = fx["api"].add_substitute_candidate(
                            fx["gid"], pid, "coach",
                            authorized_team_id=fx["home"])
                    self._assert_refused(fx, pid, res, calls, before,
                                         TEAM_SCOPE_VIOLATION)
                ran.append(label)
            finally:
                self._close(label, store)
        self._assert_ran(ran, "TRAP 1 / COACH-ADD")

    def test_accept_refuses_before_the_expired_write(self):
        """THE EXPIRY WRITE IS THE POINT. A lapsed offer durably records
        EXPIRED before raising, so an authorization check taken after it
        would leave a write behind on a refusal. The offer here IS expired,
        and the unauthorized coach must still cause zero writes."""
        ran = []
        for label, store in self._stores():
            try:
                self._assert_backend(label, store)
                store.clear_all_data()
                fx = self._build(store)
                api = fx["api"]
                pid = self._player(fx, fx["home"])
                api.enroll_substitute(fx["gid"], pid, ADMIN)
                api.offer_substitute(fx["gid"], pid, ADMIN,
                                     expires_at=_at(1).isoformat())
                self._move_side(fx, pid, fx["away"])
                before = self._state(fx, pid)
                with self.subTest(backend=label):
                    with self._write_attempts(api.store) as calls:
                        res = api.accept_substitute(
                            fx["gid"], pid, "coach",
                            authorized_team_id=fx["home"])
                    self._assert_refused(fx, pid, res, calls, before,
                                         TEAM_SCOPE_VIOLATION)
                    # The offer is STILL OFFERED — not expired — because the
                    # refusal preceded that write.
                    self.assertEqual(
                        api.store.substitute_for_player(
                            fx["gid"], pid).status.value, "offered", label)
                ran.append(label)
            finally:
                self._close(label, store)
        self._assert_ran(ran, "TRAP 2 / ACCEPT BEFORE EXPIRY WRITE")


# =========================================================================== #
# the race — two real PostgreSQL connections, both commit orders             #
# =========================================================================== #
@unittest.skipUnless(os.environ.get("TEST_DATABASE_URL"),
                     "PostgreSQL required for a true in-transaction "
                     "interleaving (see the class docstring)")
class ATrueInterleavingOnRealPostgreSQL(_CoachAuthHarness, unittest.TestCase):
    """THE ATOMIC CONTRACT, on the only backend that can actually host it.

    A TRUE IN-TRANSACTION INTERLEAVING IS POSTGRESQL-ONLY, and this file says
    so rather than claiming three interleavings it did not perform: SQLite's
    RESERVED/EXCLUSIVE lock is whole-DATABASE, so a write transaction cannot
    host a second connection committing an unrelated row, and InMemoryStore
    holds its per-instance RLock for a transaction's entire body (a
    pause-based barrier there only deadlocks). The Memory/SQLite parity for
    the same property is the SEQUENTIAL both-orders form in
    ``BothOrdersInSequenceOnMemoryAndSqlite`` below, labelled as such.

    ``race_with_forced_order`` gates both connections on
    ``get_season_for_update`` — the canonical Season row lock
    ``guard_game_season`` takes and holds to commit, and the SAME row every
    governed membership mutation takes through ``_require_active_season``. So
    the ordering is FORCED, once per direction, not sampled: by the time the
    second side's gated call unblocks, the first side's whole transaction has
    already committed.

    WHY NO NEW STORE PRIMITIVE WAS NEEDED: the schema agrees with the
    service. ``season_roster_memberships.season_id -> seasons(id)`` is a real
    FK, so even a raw INSERT takes ``FOR KEY SHARE`` on the Season row and
    blocks behind the guard's ``FOR UPDATE``.

    COVERAGE: ALL 14 SERVICE-REACHABLE SURFACES, BOTH ORDERS (PR #427 review,
    F-3). The first four tests below are the original two surfaces, kept for
    their specific end-state assertions; everything after them is the full
    matrix — see the comment block introducing it for which cell carries the
    losing authorization on each comparand, and for why nothing is omitted.

    THIS RACE DEMONSTRABLY BITES, and does not merely run.
    ``test_the_batch_seats_the_mover_only_when_it_wins_the_race`` asserts
    DIFFERENT durable outcomes for the two forced orders of the SAME call, so
    it cannot pass unless the ordering is genuinely being forced. Measured
    independently as well: switching ``remove_player``'s comparand from
    durable attribution to a live re-resolution reddens exactly the
    ``surface='remove', order='move_first'`` cells and leaves every
    ``coach_first`` cell green."""

    @classmethod
    def setUpClass(cls):
        cls.url = os.environ["TEST_DATABASE_URL"]

    def _fixture(self, seat=True):
        """One player on HOME, carrying the single live HOME stint the parity
        dual-write opens.

        ``seat`` decides whether they already hold a HOME roster row. The
        ROW-REMOVAL tests need one (it is the thing being removed); the
        CREATE tests must NOT have one, because an existing roster row makes
        ``enroll_substitute`` refuse ``already_selected`` for a reason that
        has nothing to do with authorization."""
        store = fresh_sql_store(self.url)
        try:
            self.assertEqual(store.backend, "postgres", store.backend)
            fx = self._build(store)
            pid = self._player(fx, fx["home"])
            if seat:
                fx["api"].select_roster(fx["gid"], [pid], ADMIN)
            return {k: v for k, v in fx.items() if k != "api"} | {
                "pid": pid, "home_stint": self._stint(fx, pid),
                "season_id": fx["season"]["id"]}
        finally:
            store.close()

    def _move_op(self, fx):
        """HOME->AWAY as ONE ATOMIC TRANSACTION whose FIRST statement is the
        canonical Season row lock — the same row ``guard_game_season`` takes
        and holds to commit, and the row ``race_with_forced_order`` gates on.

        THAT SHAPE IS WHAT MAKES THE RACE DETERMINISTIC rather than merely
        likely. Driving the move through the facade instead would leave its
        reads, and a SECOND transaction for the insert, OUTSIDE the gate, so
        the loser could observe a HALF-moved player (old stint already
        terminal, new stint not yet committed) and refuse for the wrong
        reason — an artefact of the harness, not a property of the code.

        Both halves are STORE writes for the reason ``end_membership_
        directly`` documents: the facade refuses terminal transitions
        outright, and a partial unique index refuses a second OPEN
        membership on one LeagueSeason — so ending and re-opening must
        happen in one unit, which is exactly what production does under
        this same lock."""
        import dataclasses

        def op(store):
            with store.transaction():
                store.get_season_for_update(fx["season_id"])
                live = store.get_season_roster_membership(fx["home_stint"])
                end_membership_directly(store, fx["home_stint"],
                                        "transferred")
                store.add_season_roster_membership(dataclasses.replace(
                    live, id=store.next_id("srm"), team_id=fx["away"],
                    status=MembershipStatus.ACTIVE))
            return {"moved": True}
        return op

    def _remove_op(self, fx):
        """The HOME coach removing the HOME row they own."""
        def op(store):
            return ApiService(store).remove_player(
                fx["gid"], fx["pid"], "coach", authorized_team_id=fx["home"])
        return op

    def _enroll_op(self, fx):
        """The HOME coach CREATING new state for the same player."""
        def op(store):
            return ApiService(store).enroll_substitute(
                fx["gid"], fx["pid"], "coach", authorized_team_id=fx["home"])
        return op

    def _check(self, fx):
        store = SqlStore(self.url)
        try:
            entry = store.roster_entry_for_player(fx["gid"], fx["pid"])
            sub = store.substitute_for_player(fx["gid"], fx["pid"])
            return (None if entry is None else (entry.status.value,
                                                entry.team_side),
                    None if sub is None else (sub.status.value, sub.team_id))
        finally:
            store.close()

    # -- the CREATE surface: the losing coach writes nothing ---------------
    def test_membership_move_first_then_the_home_coach_create_loses(self):
        fx = self._fixture(seat=False)
        move_res, enroll_res = race_with_forced_order(
            self.url, "get_season_for_update",
            self._move_op(fx), self._enroll_op(fx))
        self.assertEqual(move_res, {"moved": True}, move_res)
        # The loser re-reads the FRESH, committed membership under the lock
        # and refuses — the preflight it passed a moment earlier is not what
        # decided this.
        err = self._error(enroll_res)
        self.assertEqual(err["code"], "forbidden", enroll_res)
        self.assertEqual(err["details"]["reason"], TEAM_SCOPE_VIOLATION,
                         enroll_res)
        entry, sub = self._check(fx)
        self.assertIsNone(sub, sub)                       # nothing enrolled
        self.assertIsNone(entry, entry)

    def test_the_home_coach_create_first_then_the_membership_move(self):
        """THE OTHER ORDER, forced. The coach wins because the membership
        had not moved yet when the lock was taken — and the enrollment it
        writes carries HOME, the side that authorized it."""
        fx = self._fixture(seat=False)
        enroll_res, move_res = race_with_forced_order(
            self.url, "get_season_for_update",
            self._enroll_op(fx), self._move_op(fx))
        self.assertNotIn("error", enroll_res, enroll_res)
        self.assertEqual(move_res, {"moved": True}, move_res)
        entry, sub = self._check(fx)
        self.assertEqual(sub, ("enrolled", fx["home"]), sub)

    # -- the RESPONSE surface: durable attribution outlives the move -------
    def test_membership_move_first_does_not_disown_the_home_row(self):
        """The removal is a ROW-REMOVAL command, so a committed membership
        move CANNOT take the row away from the coach who owns it — the
        cleanup path the ruling preserves, proven against a genuinely
        interleaved, already-committed move."""
        fx = self._fixture()
        move_res, remove_res = race_with_forced_order(
            self.url, "get_season_for_update",
            self._move_op(fx), self._remove_op(fx))
        self.assertEqual(move_res, {"moved": True}, move_res)
        self.assertNotIn("error", remove_res, remove_res)
        entry, _sub = self._check(fx)
        self.assertEqual(entry, ("removed", fx["home"]), entry)

    def test_the_opposing_coach_still_loses_against_a_committed_move(self):
        """...and the coach the player MOVED TO still cannot reach the row,
        in the order most favourable to them: their move commits first."""
        fx = self._fixture()

        def away_remove(store):
            return ApiService(store).remove_player(
                fx["gid"], fx["pid"], "coach", authorized_team_id=fx["away"])

        move_res, remove_res = race_with_forced_order(
            self.url, "get_season_for_update", self._move_op(fx), away_remove)
        self.assertEqual(move_res, {"moved": True}, move_res)
        err = self._error(remove_res)
        self.assertEqual(err["details"]["reason"], TEAM_SCOPE_VIOLATION,
                         remove_res)
        entry, _sub = self._check(fx)
        self.assertEqual(entry, ("selected", fx["home"]), entry)

    # ==================================================================== #
    # THE FULL SURFACE MATRIX (PR #427 review, F-3)                        #
    # ==================================================================== #
    # The four tests above interleave TWO surfaces — enroll (create) and
    # remove (response). The ruling names EVERY Coach-reachable
    # player/row/batch mutation, in BOTH commit orders, with the losing
    # authorization leaving substitute, roster, availability, audit AND
    # notification state unchanged. What follows is that matrix: 14
    # service-reachable surfaces, every one of them raced on two real
    # PostgreSQL connections whose ordering is FORCED rather than sampled.
    #
    # WHICH CELL IS THE "LOSING AUTHORIZATION" DIFFERS BY COMPARAND, and
    # that is the whole point of the two-comparand ruling:
    #
    #   CREATE surfaces authorize against the LOCKED LIVE context, so the
    #   loser is the HOME coach once the move has COMMITTED (order
    #   `move_first`), and the AWAY coach while it has NOT yet (order
    #   `coach_first`). Both orders therefore carry a losing authorization,
    #   and both are asserted.
    #
    #   RESPONSE surfaces authorize against DURABLE row attribution, which
    #   the move cannot touch — so the loser is the OPPOSING coach in BOTH
    #   orders, and the owning coach wins in both. An order-independent
    #   outcome is not a weaker assertion here; it is the property that
    #   makes durable attribution the right comparand, and it is asserted
    #   in both directions rather than assumed.
    #
    #   BATCH surfaces authorize a SIDE before any candidate discovery, so
    #   the losing authorization is an explicitly-named foreign team, in
    #   both orders. Their positive test asserts the race genuinely bites:
    #   the moved candidate is seated when the coach's transaction wins and
    #   REPORTED AS A SKIP when the move's does.
    #
    #   `remind_unresponded` is included and its outcome is ORDER-
    #   INDEPENDENT BY CONSTRUCTION — it targets a side, so its comparand is
    #   the requested team and no membership move can change it. That is
    #   stated rather than hidden: both orders are still forced and both are
    #   asserted, and its POSITIVE test does what its authorization cannot,
    #   pinning that the RECIPIENT list is linearized with the move (the
    #   moved player is reminded when the coach wins and is not when the
    #   move does).
    #
    # NOTHING IS OMITTED. All 14 surfaces the Coach can reach through the
    # service are raced here; none proved unable to host an interleaving.
    ORDERS = ("move_first", "coach_first")

    def _surface_fixture(self, kind):
        """A fresh PostgreSQL fixture carrying whatever row ``kind`` needs,
        put there WHILE the player is still on HOME — so the HOME coach is
        genuinely the owner at the moment the race starts."""
        store = fresh_sql_store(self.url)
        try:
            self.assertEqual(store.backend, "postgres", store.backend)
            fx = self._build(store)
            pid = self._player(fx, fx["home"])
            _prepare(self, fx, pid, kind)
            return {k: v for k, v in fx.items() if k != "api"} | {
                "pid": pid, "home_stint": self._stint(fx, pid),
                "season_id": fx["season"]["id"]}
        finally:
            store.close()

    def _coach_op(self, fx, kind, team):
        """One Coach-reachable surface, on its OWN connection, entered at the
        facade with an explicit ``authorized_team_id`` — the parameter the
        ruling requires each command to revalidate under its own lock."""
        def op(store):
            api = ApiService(store)
            api.roster.clock = FakeClock()
            return _call(api, fx, fx["pid"], kind, team)
        return op

    def _pg_state(self, fx):
        """All five durable classes, read back over a FRESH connection so the
        assertion sees committed rows and not either racer's session."""
        store = SqlStore(self.url)
        try:
            return self._game_state(
                {"api": SimpleNamespace(store=store), "gid": fx["gid"]})
        finally:
            store.close()

    def _interleave(self, fx, coach_op, order):
        """Force one ordering, once, and return the coach side's result. The
        move is always asserted to have succeeded, so a race that silently
        failed to perform the interleaving cannot masquerade as a pass."""
        move = self._move_op(fx)
        if order == "move_first":
            move_res, coach_res = race_with_forced_order(
                self.url, "get_season_for_update", move, coach_op)
        else:
            coach_res, move_res = race_with_forced_order(
                self.url, "get_season_for_update", coach_op, move)
        self.assertEqual(move_res, {"moved": True}, move_res)
        return coach_res

    def _assert_lost(self, fx, res, before, where):
        """THE LOSING AUTHORIZATION, asserted the same way in every cell: the
        structured refusal, and every durable class IDENTITY-unchanged —
        substitutes, roster, availability, audit, the notification event
        table AND the feed/delivery rows."""
        err = self._error(res)
        self.assertEqual(err["code"], "forbidden", (where, res))
        self.assertEqual(err["details"]["reason"], TEAM_SCOPE_VIOLATION,
                         (where, res))
        self.assertEqual(self._pg_state(fx), before, where)

    def _assert_won(self, res, where):
        if isinstance(res, dict):
            self.assertNotIn("error", res, (where, res))

    # -- create surfaces --------------------------------------------------
    def test_every_create_surface_refuses_its_loser_in_both_orders(self):
        """Six CREATE surfaces x both forced orders. The loser differs by
        order — HOME once the move has committed, AWAY while it has not —
        and in every cell the refusal leaves all five state classes
        untouched."""
        for kind in CREATE_SURFACES:
            for order, side in (("move_first", "home"),
                                ("coach_first", "away")):
                with self.subTest(surface=kind, order=order, coach=side):
                    fx = self._surface_fixture(kind)
                    before = self._pg_state(fx)
                    res = self._interleave(
                        fx, self._coach_op(fx, kind, fx[side]), order)
                    self._assert_lost(fx, res, before, (kind, order, side))

    def test_every_create_surface_still_admits_the_coach_who_wins(self):
        """...and the same six surfaces still COMMIT for the HOME coach whose
        transaction takes the Season lock before the move does. Without this
        cell the refusals above would also be satisfied by a gate that
        refused everyone."""
        for kind in CREATE_SURFACES:
            with self.subTest(surface=kind):
                fx = self._surface_fixture(kind)
                res = self._interleave(
                    fx, self._coach_op(fx, kind, fx["home"]), "coach_first")
                self._assert_won(res, kind)

    # -- response surfaces ------------------------------------------------
    def test_every_response_surface_refuses_the_opponent_in_both_orders(self):
        """Five RESPONSE surfaces x both forced orders, with the OPPOSING
        coach acting. A committed membership move does not hand them the row:
        durable attribution still names HOME, so they lose in the order most
        favourable to them as well as the other."""
        for kind in RESPONSE_SURFACES:
            for order in self.ORDERS:
                with self.subTest(surface=kind, order=order):
                    fx = self._surface_fixture(kind)
                    before = self._pg_state(fx)
                    res = self._interleave(
                        fx, self._coach_op(fx, kind, fx["away"]), order)
                    self._assert_lost(fx, res, before, (kind, order, "away"))

    def test_every_response_surface_keeps_the_owners_cleanup(self):
        """THE CLEANUP PATH THE RULING PRESERVES, in both orders: "This
        preserves the ordinary cleanup path after transfer/inactivation and
        prevents a row from silently changing owners because eligibility
        later changed." Order-independence is the property, so both are
        forced rather than one being assumed from the other."""
        for kind in RESPONSE_SURFACES:
            for order in self.ORDERS:
                with self.subTest(surface=kind, order=order):
                    fx = self._surface_fixture(kind)
                    res = self._interleave(
                        fx, self._coach_op(fx, kind, fx["home"]), order)
                    self._assert_won(res, (kind, order))

    # -- batch surfaces ---------------------------------------------------
    def _batch_fixture(self, route):
        """TWO home candidates — one who MOVES and one who does not — so a
        batch always has something real to seat whichever transaction wins,
        and "was the mover seated?" is answerable from the response alone.
        A zero-seat result could otherwise be mistaken for a correct one."""
        store = fresh_sql_store(self.url)
        try:
            self.assertEqual(store.backend, "postgres", store.backend)
            fx = self._build(store)
            api = fx["api"]
            mover = self._player(fx, fx["home"], "Mo Mover")
            stayer = self._player(fx, fx["home"], "Sam Stayer")
            self._player(fx, fx["away"], "Ava Away")
            out = {k: v for k, v in fx.items() if k != "api"} | {
                "pid": mover, "stayer": stayer,
                "home_stint": self._stint(fx, mover),
                "season_id": fx["season"]["id"]}
            if route == "copy":
                slot = api.create_ice_slot(
                    fx["rink"]["id"], _at(2).isoformat(), _at(3).isoformat(),
                    "game", actor_id=ADMIN)
                prior = api.create_game(
                    fx["season"]["id"], None, fx["home"], fx["away"],
                    slot["id"], target_goalies=0, target_skaters=8,
                    actor_id=ADMIN, league_id=fx["league"]["id"])
                self.assertNotIn("error", prior, prior)
                api.publish_game(prior["id"], actor_id=ADMIN)
                seated = api.select_roster(prior["id"], [mover, stayer],
                                           actor_id=ADMIN)
                self.assertNotIsInstance(seated, dict, seated)
                out["prior_gid"] = prior["id"]
            return out
        finally:
            store.close()

    def _batch_op(self, fx, route, team_id, authorized_team_id):
        def op(store):
            api = ApiService(store)
            api.roster.clock = FakeClock()
            if route == "build":
                return api.auto_build_roster(
                    fx["gid"], team_id, "coach",
                    authorized_team_id=authorized_team_id)
            return api.copy_previous_roster(
                fx["gid"], team_id, "coach",
                authorized_team_id=authorized_team_id)
        return op

    def test_a_foreign_batch_team_loses_in_both_orders(self):
        """The AWAY coach names HOME explicitly on both batch entry points,
        interleaved with a committing membership move in both directions.
        The authorization is decided before candidate discovery, so neither
        order can turn it into a seat — and nothing is written."""
        for route in ("build", "copy"):
            for order in self.ORDERS:
                with self.subTest(route=route, order=order):
                    fx = self._batch_fixture(route)
                    before = self._pg_state(fx)
                    res = self._interleave(
                        fx, self._batch_op(fx, route, fx["home"], fx["away"]),
                        order)
                    self._assert_lost(fx, res, before, (route, order))

    def test_the_batch_seats_the_mover_only_when_it_wins_the_race(self):
        """THE PROOF THAT THIS RACE BITES rather than merely running. The
        SAME authorized batch produces DIFFERENT durable state depending on
        which transaction took the Season lock first: the moved candidate is
        seated when the coach wins, and is a REPORTED SKIP — never a silent
        omission and never a cross-side seat — when the move wins."""
        for route in ("build", "copy"):
            with self.subTest(route=route, order="coach_first"):
                fx = self._batch_fixture(route)
                res = self._interleave(
                    fx, self._batch_op(fx, route, None, fx["home"]),
                    "coach_first")
                self.assertNotIn("error", res, (route, res))
                self.assertEqual(sorted(res["seated"]),
                                 sorted([fx["pid"], fx["stayer"]]),
                                 (route, res))
            with self.subTest(route=route, order="move_first"):
                fx = self._batch_fixture(route)
                res = self._interleave(
                    fx, self._batch_op(fx, route, None, fx["home"]),
                    "move_first")
                self.assertNotIn("error", res, (route, res))
                self.assertEqual(res["seated"], [fx["stayer"]], (route, res))
                self.assertEqual([r["player_id"] for r in res["skipped"]],
                                 [fx["pid"]], (route, res))

    # -- the side-targeting surface ---------------------------------------
    def _remind_fixture(self):
        store = fresh_sql_store(self.url)
        try:
            self.assertEqual(store.backend, "postgres", store.backend)
            fx = self._build(store)
            mover = self._player(fx, fx["home"], "Mo Mover")
            away = self._player(fx, fx["away"], "Ava Away")
            return {k: v for k, v in fx.items() if k != "api"} | {
                "pid": mover, "away_pid": away,
                "home_stint": self._stint(fx, mover),
                "season_id": fx["season"]["id"]}
        finally:
            store.close()

    def _remind_op(self, fx, team_id, authorized_team_id):
        def op(store):
            api = ApiService(store)
            api.roster.clock = FakeClock()
            return api.remind_unresponded(
                fx["gid"], team_id, "coach",
                authorized_team_id=authorized_team_id)
        return op

    def test_remind_refuses_the_other_side_in_both_orders(self):
        """ORDER-INDEPENDENT BY CONSTRUCTION, and said so rather than dressed
        up: this surface's comparand is the REQUESTED side, which no
        membership move can alter. Both orders are still forced, and in both
        the refusal leaves the feed and the delivery queue — the tables this
        surface actually writes — byte-identical."""
        for order in self.ORDERS:
            with self.subTest(order=order):
                fx = self._remind_fixture()
                before = self._pg_state(fx)
                res = self._interleave(
                    fx, self._remind_op(fx, fx["away"], fx["home"]), order)
                self._assert_lost(fx, res, before, ("remind", order))

    def test_the_reminders_recipients_are_linearized_with_the_move(self):
        """What the AUTHORIZATION cannot show, the RECIPIENT LIST can: the
        HOME coach's own reminder finds the mover when their transaction wins
        the Season lock, and does not once the move has committed. That is
        the same lock making the audience of a notification agree with the
        membership it was computed from."""
        with self.subTest(order="coach_first"):
            fx = self._remind_fixture()
            res = self._interleave(
                fx, self._remind_op(fx, fx["home"], fx["home"]),
                "coach_first")
            self.assertEqual(res, {"reminded": 1}, res)
        with self.subTest(order="move_first"):
            fx = self._remind_fixture()
            res = self._interleave(
                fx, self._remind_op(fx, fx["home"], fx["home"]), "move_first")
            self.assertEqual(res, {"reminded": 0}, res)


class BothOrdersInSequenceOnMemoryAndSqlite(_CoachAuthHarness,
                                            unittest.TestCase):
    """MEMORY/SQLITE PARITY FOR THE SAME PROPERTY, HONESTLY LABELLED.

    This is NOT an interleaving and does not claim to be one — see
    ``ATrueInterleavingOnRealPostgreSQL`` for why neither backend can host
    one. What IS backend-independent and IS asserted here is the pre-
    transaction PREFLIGHT WINDOW: the authority is re-decided inside the
    mutation, so the same two operations produce the same outcome in BOTH
    sequential orders on every backend."""

    def _both_orders(self, label, store, kind, expect_first, expect_second):
        # order A: the membership moves, THEN the coach acts.
        store.clear_all_data()
        fx = self._build(store)
        pid = self._player(fx, fx["home"])
        _prepare(self, fx, pid, kind)
        self._move_side(fx, pid, fx["away"])
        a = _call(fx["api"], fx, pid, kind, fx["home"])
        # order B: the coach acts, THEN the membership moves.
        store.clear_all_data()
        fx = self._build(store)
        pid = self._player(fx, fx["home"])
        _prepare(self, fx, pid, kind)
        b = _call(fx["api"], fx, pid, kind, fx["home"])
        self._move_side(fx, pid, fx["away"])
        self.assertEqual(isinstance(a, dict) and "error" in a, expect_first,
                         (label, kind, a))
        self.assertEqual(isinstance(b, dict) and "error" in b, expect_second,
                         (label, kind, b))

    def test_create_refuses_only_after_the_move_commits(self):
        ran = []
        for label, store in self._stores():
            try:
                self._assert_backend(label, store)
                for kind in CREATE_SURFACES:
                    with self.subTest(backend=label, surface=kind):
                        self._both_orders(label, store, kind, True, False)
                ran.append(label)
            finally:
                self._close(label, store)
        self._assert_ran(ran, "BOTH ORDERS / CREATE")

    def test_response_succeeds_in_either_order(self):
        """The durable side does not move, so the owning coach's cleanup is
        order-INDEPENDENT — which is the property that makes it the right
        comparand."""
        ran = []
        for label, store in self._stores():
            try:
                self._assert_backend(label, store)
                for kind in RESPONSE_SURFACES:
                    with self.subTest(backend=label, surface=kind):
                        self._both_orders(label, store, kind, False, False)
                ran.append(label)
            finally:
                self._close(label, store)
        self._assert_ran(ran, "BOTH ORDERS / RESPONSE")


# =========================================================================== #
# remind_unresponded — the side-targeting surface (#427 review, F-1)         #
# =========================================================================== #
class RemindUnrespondedRevalidatesTheNotifiedSideUnderTheLock(
        _CoachAuthHarness, unittest.TestCase):
    """THE GATE THAT HAD NO TEST. The comparand table at the top of this file
    has always LISTED ``remind_unresponded`` — "notifies one side; the
    requested team IS the comparand" — but nothing in the tree ASSERTED it. A
    reviewer deleted the gate outright and ran the entire Memory/SQLite suite
    green (measured again here, 234 modules / 3 shards / 271s, with this
    class's ancestor absent), so the correct code was carrying no falsifiable
    coverage at all and a regression would have been invisible.

    WHY THIS SURFACE NEEDS ITS OWN CLASS rather than a row in the surface
    table above. Every other surface targets a PLAYER and is refused on that
    player's side; this one targets a SIDE and creates state (a feed
    notification plus its delivery rows) for everyone on it. There is no row
    to consult and no player to resolve, so ``_call``/``_prepare`` cannot
    express it and the comparand is the REQUESTED ``team_id`` itself.

    WHAT KILLS EACH TEST BELOW — stated because this round exists precisely
    because two correct gates had none:

      * ``test_a_coach_cannot_remind_the_other_sides_players`` — deleting
        ``_require_authorized_team`` from ``ApiService.remind_unresponded``
        (api/service.py, immediately before ``_availability_summary_of``)
        turns the refusal into ``{"reminded": 1}`` and writes an
        AVAILABILITY_REMINDER for the other side's player.
      * ``test_the_owning_coach_reminds_their_own_side_only`` — widening the
        comparand from the requested team to, say, the coach's own team makes
        the gate vacuous and this test's cross-side assertion still holds; it
        is the NEGATIVE test above that kills that mutation. What this one
        kills is a gate placed too tightly (refusing the legitimate owner).
      * ``test_an_unscoped_league_admin_may_remind_either_side`` — gating on
        anything other than ``authorized_team_id is None`` reddens it.

    THE ZERO-WRITE ASSERTION IS NOT VACUOUS HERE, and is proven so: after the
    refusal, the SAME call is repeated as an unscoped League Admin and must
    report ``{"reminded": 1}``. So the refused call genuinely had a reminder
    to write and the gate is what stopped it, not an empty recipient
    list."""

    def _sides(self, fx):
        """One player on EACH side, neither having answered — so "which side
        was notified" is answerable from the feed rows alone."""
        return (self._player(fx, fx["home"], "Hana Home"),
                self._player(fx, fx["away"], "Ava Away"))

    def _reminders(self, fx):
        return sorted(n.audience_ref
                      for n in fx["api"].store.all_notifications_feed()
                      if n.kind.value == "availability_reminder")

    def test_a_coach_cannot_remind_the_other_sides_players(self):
        ran = []
        for label, store in self._stores():
            try:
                self._assert_backend(label, store)
                store.clear_all_data()
                fx = self._build(store)
                home_pid, away_pid = self._sides(fx)
                before = self._game_state(fx)
                with self.subTest(backend=label):
                    with self._write_attempts(fx["api"].store) as calls:
                        res = fx["api"].remind_unresponded(
                            fx["gid"], fx["away"], "coach",
                            authorized_team_id=fx["home"])
                    err = self._error(res)
                    self.assertEqual(err["code"], "forbidden", res)
                    self.assertEqual((err.get("details") or {}).get("reason"),
                                     TEAM_SCOPE_VIOLATION, res)
                    # ZERO WRITE ATTEMPTS — an ordering property, not a diff.
                    self.assertEqual(calls, [], calls)
                    # ...and every durable class, INCLUDING the feed and the
                    # delivery queue this surface actually writes to.
                    self.assertEqual(self._game_state(fx), before, label)
                    self.assertEqual(self._reminders(fx), [], label)
                    # THE REFUSAL WAS NOT VACUOUS: the same reminder, sent by
                    # someone entitled to send it, does real work.
                    allowed = fx["api"].remind_unresponded(
                        fx["gid"], fx["away"], ADMIN)
                    self.assertEqual(allowed, {"reminded": 1}, allowed)
                    self.assertEqual(self._reminders(fx), [away_pid], label)
                ran.append(label)
            finally:
                self._close(label, store)
        self._assert_ran(ran, "REMIND / FOREIGN SIDE")

    def test_the_owning_coach_reminds_their_own_side_only(self):
        """The refusal above is about AUTHORITY, not about reminders being
        impossible: the side's OWN coach still nudges their own players, and
        the other side's player is not touched."""
        ran = []
        for label, store in self._stores():
            try:
                self._assert_backend(label, store)
                store.clear_all_data()
                fx = self._build(store)
                home_pid, away_pid = self._sides(fx)
                with self.subTest(backend=label):
                    res = fx["api"].remind_unresponded(
                        fx["gid"], fx["home"], "coach",
                        authorized_team_id=fx["home"])
                    self.assertEqual(res, {"reminded": 1}, res)
                    self.assertEqual(self._reminders(fx), [home_pid], label)
                ran.append(label)
            finally:
                self._close(label, store)
        self._assert_ran(ran, "REMIND / OWN SIDE")

    def test_an_unscoped_league_admin_may_remind_either_side(self):
        """``authorized_team_id=None`` means NO team constraint, here as
        everywhere else: the League Admin reminds both sides in turn."""
        ran = []
        for label, store in self._stores():
            try:
                self._assert_backend(label, store)
                store.clear_all_data()
                fx = self._build(store)
                home_pid, away_pid = self._sides(fx)
                with self.subTest(backend=label):
                    for team, pid in ((fx["home"], home_pid),
                                      (fx["away"], away_pid)):
                        res = fx["api"].remind_unresponded(
                            fx["gid"], team, ADMIN)
                        self.assertEqual(res, {"reminded": 1}, (team, res))
                    self.assertEqual(self._reminders(fx),
                                     sorted([home_pid, away_pid]), label)
                ran.append(label)
            finally:
                self._close(label, store)
        self._assert_ran(ran, "REMIND / LEAGUE ADMIN")


class RemindOverRealAuthenticatedHttp(_CoachAuthHarness, unittest.TestCase):
    """THE SAME SURFACE END-TO-END, through a live ``ThreadingHTTPServer``
    with a real cookie session, so the Coach scope under test is the one
    ``_resolve_role`` actually produces rather than one a test asserted into
    existence.

    WHAT THIS CLASS CAN AND CANNOT PROVE, stated rather than implied. Over
    HTTP the ``scope_violation`` PREFLIGHT refuses an explicit foreign
    ``team_id`` before the request ever reaches the service, and an OMITTED
    ``team_id`` is filled from the coach's own scope by the route itself
    (``body.get("team_id") or scope.get("team_id")``). So on this route the
    two possible bodies are the coach's own side or a preflight denial, and
    the under-lock gate is NOT independently reachable through HTTP: it is
    defence in depth, which is exactly the posture the ruling asks for ("the
    scope preflight may remain for fast denial, but it cannot be the
    authoritative write gate").

    THEREFORE: deleting the service gate does NOT redden this class — it
    reddens ``RemindUnrespondedRevalidatesTheNotifiedSideUnderTheLock``
    above, which calls the facade directly and bypasses the preflight. What
    THIS class pins is the reachable end-to-end behaviour: the 403 an
    opposing coach actually receives, with nothing written on any durable
    surface, and the 200 the owning coach receives touching only their own
    side.

    WHAT KILLS THIS CLASS, stated precisely because an earlier draft of this
    sentence was wrong and a reviewer measured it: removing the preflight's
    ``body["team_id"]`` check ALONE does NOT redden it. The request falls
    through to the service gate, which refuses with the same ``forbidden``,
    and the assertions here are satisfied. That is defence in depth working
    as intended, not a hole. Only removing BOTH layers -- the preflight check
    AND ``_require_authorized_team`` in ``remind_unresponded`` -- reddens it."""

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
        # server_close() as well as shutdown(): the latter only stops the
        # serve loop, and a listening socket left for the GC surfaces as a
        # ResourceWarning at interpreter shutdown (see run_parallel.py).
        cls.httpd.server_close()
        srv.STATE.api = cls._saved_api

    def _accounts(self, fx):
        api = fx["api"]
        api.accounts.create_account("admin", DEMO_PASSWORD,
                                    DEMO_USERS["admin"], scope={},
                                    actor_id="test_seed",
                                    account_id="user_admin")
        api.accounts.create_account("coachhome", DEMO_PASSWORD, Role.COACH,
                                    scope={"team_id": fx["home"]},
                                    actor_id="test_seed",
                                    account_id="user_coach_home")
        api.accounts.create_account("coachaway", DEMO_PASSWORD, Role.COACH,
                                    scope={"team_id": fx["away"]},
                                    actor_id="test_seed",
                                    account_id="user_coach_away")

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
            with e:
                return e.code, json.loads(e.read() or b"{}")

    def _login(self, fx, username):
        srv.STATE.api = fx["api"]
        opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(CookieJar()))
        status, body = self._req(opener, "POST", "/api/auth/login",
                                 {"username": username, "password": "demo"})
        self.assertEqual(status, 200, (username, body))
        return opener

    def _fixture(self, store):
        fx = self._build(store)
        home_pid = self._player(fx, fx["home"], "Hana Home")
        away_pid = self._player(fx, fx["away"], "Ava Away")
        self._accounts(fx)
        return fx, home_pid, away_pid

    def _reminders(self, fx):
        return sorted(n.audience_ref
                      for n in fx["api"].store.all_notifications_feed()
                      if n.kind.value == "availability_reminder")

    def test_an_opposing_coach_naming_the_other_side_is_refused(self):
        ran = []
        for label, store in self._stores():
            try:
                self._assert_backend(label, store)
                store.clear_all_data()
                fx, home_pid, away_pid = self._fixture(store)
                before = self._game_state(fx)
                away = self._login(fx, "coachaway")
                with self.subTest(backend=label):
                    status, body = self._req(
                        away, "POST",
                        f"/api/games/{fx['gid']}/availability/remind",
                        {"team_id": fx["home"]})
                    self.assertEqual(status, 403, body)
                    self.assertEqual(body["error"]["code"], "forbidden", body)
                    self.assertEqual(self._game_state(fx), before, label)
                    self.assertEqual(self._reminders(fx), [], label)
                ran.append(label)
            finally:
                self._close(label, store)
        self._assert_ran(ran, "REMIND HTTP / FOREIGN SIDE")

    def test_each_coachs_own_reminder_reaches_only_their_own_side(self):
        """Both bodies the route accepts, for both coaches: the omitted
        ``team_id`` is filled from the caller's OWN scope, so an AWAY coach's
        empty body reminds AWAY and never HOME."""
        ran = []
        for label, store in self._stores():
            try:
                self._assert_backend(label, store)
                for user, own_pid in (("coachhome", "home"),
                                      ("coachaway", "away")):
                    store.clear_all_data()
                    fx, home_pid, away_pid = self._fixture(store)
                    expected = home_pid if own_pid == "home" else away_pid
                    opener = self._login(fx, user)
                    with self.subTest(backend=label, user=user):
                        status, body = self._req(
                            opener, "POST",
                            f"/api/games/{fx['gid']}/availability/remind", {})
                        self.assertEqual(status, 200, body)
                        self.assertEqual(body, {"reminded": 1}, body)
                        self.assertEqual(self._reminders(fx), [expected],
                                         (user, label))
                ran.append(label)
            finally:
                self._close(label, store)
        self._assert_ran(ran, "REMIND HTTP / OWN SIDE")


# =========================================================================== #
# what a refusal SAYS — and the existence it must not disclose               #
# =========================================================================== #
class EveryRefusalDescribesItsActualCase(_CoachAuthHarness, unittest.TestCase):
    """PR #427 review, F-5. Both refusals below already failed CLOSED and
    neither leaked existence; what was wrong was the SENTENCE.

    ``_require_authorized_team`` raised ONE message for a NULL comparand —
    "This {what} predates durable team attribution" — which is true only when
    a row was actually FOUND and cannot name its side (a pre-060/061 row, no
    backfill). At the two LIVE-comparand sites the same NULL means something
    else entirely:

      * ``select_roster`` with an id that names NOBODY answered "This player
        predates durable team attribution" instead of anything about the
        player not being theirs. The preflight ``continue``s past a missing
        player, so this is the REACHABLE HTTP behaviour, not a theoretical
        path.
      * ``set_availability`` for a player with no roster row and no context
        said "This roster row predates durable team attribution" ABOUT A ROW
        THAT DOES NOT EXIST.

    Either would send an operator to repair a legacy-attribution problem that
    was never there.

    THE EXISTENCE-DISCLOSURE TENSION, DECIDED. The obvious "accurate" fix —
    ``not_found`` for an unknown player id — is refused deliberately: it
    would answer "does this player id exist?" for exactly the caller this
    gate has just decided is not entitled to an answer, i.e. hand a coach a
    player-id enumeration oracle. The same argument applies WITHIN
    ``set_availability``, where a distinct sentence for the no-row branch
    would tell an unauthorized coach whether the player holds a roster row in
    this game. So the cases stay MERGED behind one reason
    (``attribution_missing``) and one INVARIANT sentence that interpolates no
    subject noun, and what the refusal now describes is the decision rather
    than the cause. The durable sites, where the subject provably exists and
    the legacy explanation is both true and actionable, keep their original
    wording.

    WHAT KILLS EACH TEST: reverting either live call site to the default
    ``comparand="durable"`` reddens the three wording tests; making
    ``select_roster`` raise ``NotFoundError`` before the gate (or letting the
    live message interpolate ``what``) reddens the non-disclosure test;
    switching the durable sites to ``comparand="live"`` reddens the last one.
    """

    _LEGACY_PHRASE = "predates durable team attribution"

    def _refusal(self, res):
        err = self._error(res)
        self.assertEqual(err["code"], "forbidden", res)
        self.assertEqual((err.get("details") or {}).get("reason"),
                         ATTRIBUTION_MISSING, res)
        return err

    def test_an_unknown_player_is_not_blamed_on_legacy_attribution(self):
        ran = []
        for label, store in self._stores():
            try:
                self._assert_backend(label, store)
                store.clear_all_data()
                fx = self._build(store)
                before = self._game_state(fx)
                with self.subTest(backend=label):
                    with self._write_attempts(fx["api"].store) as calls:
                        res = fx["api"].select_roster(
                            fx["gid"], ["player_that_does_not_exist"],
                            "coach", authorized_team_id=fx["home"])
                    err = self._refusal(res)
                    self.assertEqual(err["message"],
                                     _ATTRIBUTION_MISSING_LIVE, res)
                    self.assertNotIn(self._LEGACY_PHRASE, err["message"], res)
                    self.assertEqual(calls, [], calls)
                    self.assertEqual(self._game_state(fx), before, label)
                ran.append(label)
            finally:
                self._close(label, store)
        self._assert_ran(ran, "F-5 / SELECT UNKNOWN PLAYER")

    def test_the_refusal_does_not_disclose_whether_the_player_exists(self):
        """THE CONSERVATIVE CHOICE, PINNED. An id naming nobody and a REAL
        player who simply is not on this coach's side of this game must
        produce BYTE-IDENTICAL refusals — same code, same reason, same
        message, same details. The moment they differ, the route becomes an
        existence oracle for any coach who can post to it.

        SCOPE, STATED HONESTLY: this pins ``select_roster`` AND NOTHING ELSE.
        It is the surface where a coach can name an arbitrary player id and
        this gate is the first thing to answer, which is what makes the
        oracle reachable there. Sibling surfaces at the same facade do NOT
        hold this property today and did not before the commit that added
        this test -- a reviewer measured, for an unauthorized coach,
        ``set_availability(<unknown id>)`` answering ``not_found`` with
        "Player {id} not found." while a real player gets
        ``forbidden``/``attribution_missing``, and ``enroll_substitute``
        naming a third-team player OUT LOUD in its refusal. Both are
        pre-existing and outside this round's ruling; neither was created or
        widened here. Do not read this test as a system-wide guarantee."""
        ran = []
        for label, store in self._stores():
            try:
                self._assert_backend(label, store)
                store.clear_all_data()
                fx = self._build(store)
                # A player who genuinely exists, on a team registered for
                # this Season but NOT playing in this game — so no context
                # resolves for them here, exactly as for an unknown id.
                real_pid = self._player(fx, fx["third"], "Tia Third")
                with self.subTest(backend=label):
                    unknown = fx["api"].select_roster(
                        fx["gid"], ["player_that_does_not_exist"], "coach",
                        authorized_team_id=fx["home"])
                    existing = fx["api"].select_roster(
                        fx["gid"], [real_pid], "coach",
                        authorized_team_id=fx["home"])
                    self._refusal(unknown)
                    self._refusal(existing)
                    self.assertEqual(unknown, existing,
                                     (label, unknown, existing))
                ran.append(label)
            finally:
                self._close(label, store)
        self._assert_ran(ran, "F-5 / NO EXISTENCE ORACLE")

    def test_a_missing_roster_row_is_not_described_as_a_legacy_row(self):
        """``set_availability`` for a player with no roster row and no
        context in this game — and, on the SAME assertion, for a player whose
        row exists but carries a legacy NULL ``team_side``. The two must read
        IDENTICALLY: letting them differ would disclose whether the row
        exists. Neither may claim a row "predates durable team attribution",
        because in the first case there is no row to predate anything."""
        ran = []
        for label, store in self._stores():
            try:
                self._assert_backend(label, store)
                store.clear_all_data()
                fx = self._build(store)
                no_row_pid = self._player(fx, fx["third"], "Tia Third")
                legacy_pid = self._player(fx, fx["home"], "Lee Legacy")
                fx["api"].select_roster(fx["gid"], [legacy_pid], ADMIN)
                entry = store.roster_entry_for_player(fx["gid"], legacy_pid)
                with store.transaction():
                    entry.team_side = None
                    entry.seated_position = None
                    store.save_roster_entry(entry)
                self.assertIsNone(
                    store.roster_entry_for_player(
                        fx["gid"], legacy_pid).team_side, label)
                with self.subTest(backend=label):
                    no_row = fx["api"].set_availability(
                        fx["gid"], no_row_pid, "unavailable", "coach",
                        "coach", authorized_team_id=fx["home"])
                    legacy = fx["api"].set_availability(
                        fx["gid"], legacy_pid, "unavailable", "coach",
                        "coach", authorized_team_id=fx["home"])
                    for res in (no_row, legacy):
                        err = self._refusal(res)
                        self.assertEqual(err["message"],
                                         _ATTRIBUTION_MISSING_LIVE, res)
                        self.assertNotIn(self._LEGACY_PHRASE, err["message"],
                                         res)
                    self.assertEqual(no_row["error"]["message"],
                                     legacy["error"]["message"], label)
                ran.append(label)
            finally:
                self._close(label, store)
        self._assert_ran(ran, "F-5 / AVAILABILITY WORDING")

    def test_a_genuinely_legacy_row_keeps_the_wording_that_fits_it(self):
        """THE OTHER HALF OF THE CHANGE, so it cannot be over-applied. On the
        DURABLE comparand sites the row was FOUND and its NULL side really is
        a pre-060/061 artefact, so "predates durable team attribution" is
        literally true and tells an operator what to fix. Those messages must
        NOT be flattened into the live one."""
        cases = {"withdraw_enrolled": "substitute enrollment",
                 "decline": "substitute offer",
                 "remove": "roster row"}
        ran = []
        for label, store in self._stores():
            try:
                self._assert_backend(label, store)
                for kind, what in cases.items():
                    store.clear_all_data()
                    fx = self._build(store)
                    pid = self._player(fx, fx["home"])
                    _prepare(self, fx, pid, kind)
                    if kind == "remove":
                        row = store.roster_entry_for_player(fx["gid"], pid)
                        with store.transaction():
                            row.team_side = None
                            row.seated_position = None
                            store.save_roster_entry(row)
                    else:
                        row = store.substitute_for_player(fx["gid"], pid)
                        with store.transaction():
                            row.team_id = None
                            store.save_substitute(row)
                    with self.subTest(backend=label, surface=kind):
                        res = _call(fx["api"], fx, pid, kind, fx["home"])
                        err = self._refusal(res)
                        self.assertEqual(
                            err["message"],
                            _ATTRIBUTION_MISSING_DURABLE.format(what=what),
                            res)
                        self.assertIn(self._LEGACY_PHRASE, err["message"], res)
                ran.append(label)
            finally:
                self._close(label, store)
        self._assert_ran(ran, "F-5 / DURABLE WORDING PRESERVED")


if __name__ == "__main__":
    unittest.main()
