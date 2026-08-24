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

WHAT WAS MEASURED RED at head 22bd6de, tri-store (``tests/_repro205.py``
reproduction (ii)). ``scope_violation`` passed while the player was still on
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
                               to (nothing is seated, so nothing is owned)

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
"""

import contextlib
import os
import unittest

from helpers import (BACKEND, FakeClock, end_membership_directly,  # noqa: F401
                     fresh_sql_store, race_with_forced_order)
from test_substitute_membership_cutover import ADMIN, _at

from hockey_scheduler.api import ApiService
from hockey_scheduler.domain import MembershipStatus
from hockey_scheduler.services.roster_service import (ATTRIBUTION_MISSING,
                                                      TEAM_SCOPE_VIOLATION)
from hockey_scheduler.store import InMemoryStore, SqlStore

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
        return {"api": api, "season": season, "league": league,
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
    _WRITE_PREFIXES = ("save_", "add_", "upsert_", "insert_", "update_",
                       "delete_", "remove_", "clear_", "next_id")

    @contextlib.contextmanager
    def _write_attempts(self, store):
        """Record every STORE WRITE METHOD CALLED, whether or not it survived.

        A SNAPSHOT DIFF CANNOT PROVE WHAT IS ASSERTED HERE — see the module
        docstring. Patched on the INSTANCE and restored in ``finally``; the
        number of methods actually wrapped is asserted so a rename that
        empties the prefix list fails loudly instead of turning this into a
        spy that can never fire."""
        calls = []
        originals = {}
        for name in dir(store):
            if not name.startswith(self._WRITE_PREFIXES):
                continue
            attr = getattr(store, name, None)
            if not callable(attr):
                continue
            originals[name] = attr

            def make(n, orig):
                def spy(*a, **kw):
                    calls.append(n)
                    return orig(*a, **kw)
                return spy

            setattr(store, name, make(name, attr))
        self.assertGreater(len(originals), 10, sorted(originals))
        try:
            yield calls
        finally:
            for name in originals:
                delattr(store, name)

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
    blocks behind the guard's ``FOR UPDATE``."""

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


if __name__ == "__main__":
    unittest.main()
