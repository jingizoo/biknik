"""SELECT_ROSTER IDEMPOTENCY IS SIDE-OWNED, NOT CROSS-TEAM (#205).

THE BLOCKER (owner comment 5439156505), reproduced on exact head 63db78f on
Memory and SQLite and, here, on real PostgreSQL and over authenticated HTTP

    "`RosterService.select_roster` authorizes every target against the locked
     LIVE membership context, but its existing-row branch then treats any
     occupying row as idempotent and returns it without checking that row's
     durable `team_side`. Repro: HOME Coach selects a player, creating
     occupying `entry_1` with `team_side=HOME`; the player's live membership is
     transferred to AWAY; AWAY Coach calls `select_roster(...,
     authorized_team_id=AWAY)`. The call succeeds and returns the HOME-owned
     row, including `team_side=HOME` and its original `selected_by`; storage
     remains HOME occupied and AWAY open; and a second identical
     `roster_selected` audit is appended although no roster state changed."

WHAT WAS MEASURED RED at head 63db78f, all three backends AND over a real
authenticated session on `POST /api/games/{gid}/roster/select`::

    backend    AWAY coach select   returned row              audit      writes
    memory     200                 entry_1 side=HOME         +1 dup     next_id,
                                   selected_by=coach_home               add_audit
    sqlite     200                 entry_1 side=HOME         +1 dup     (same)
    postgres   200                 entry_1 side=HOME         +1 dup     (same)
    HTTP x3    200                 full HOME row in body     +1 dup     (same)

THE RULE THIS FILE PINS, which is the standing ruling on this PR applied to
the one branch that was violating it: DURABLE ATTRIBUTION ANSWERS WHICH SIDE A
ROW HOLDS; LIVE CONTEXT AUTHORIZES WHO MAY ACT ON IT. Idempotency is a claim
about an EXISTING row, so the comparand is that row's `team_side`
(migration 061) — the same source `remove_player` and `set_availability`
already use. Judging it live is what made idempotency cross-team.

  own occupying row       -> idempotent no-op, NO write, NO audit
  foreign occupying row   -> `team_scope_violation`, no row disclosure,
                             zero write ATTEMPTS, no audit
  NULL attribution        -> fails closed under the EXISTING typed rule
                             (`attribution_missing`, durable comparand)
  missing / non-occupying -> live context may create/revive and REATTRIBUTE
  authorized_team_id=None -> unscoped operator, byte-for-byte unchanged

WRITE ATTEMPTS, NOT SNAPSHOT DIFFS. `select_roster` is `@_transactional`, so a
guard placed after the first write still leaves an empty diff — the raise rolls
it back on all three backends alike. "Zero writes" is an ORDERING property and
only a spy on the ATTEMPTS can see it, which is what the whole-batch preflight
is for. The spy is `helpers.write_attempt_spy`, shared with the other coach
authorization suites so all three files mean the same thing by the phrase.

MOVER-SHAPED FIXTURES ONLY. `_move_side` ends the HOME stint as TRANSFERRED
history and opens a new ACTIVE stint on AWAY, so the permanent `Player.team_id`
pointer and the seasonal membership DELIBERATELY DISAGREE. A demo-style fixture
where they agree by construction is structurally blind to this blocker: the
live gate would refuse the AWAY coach before the durable branch was ever
reached, and the test would pass against the broken code.

TRI-STORE, PROVEN. `_stores` yields Memory, SQLite and — when TEST_DATABASE_URL
is set — real PostgreSQL; `_assert_backend` PROVES each one and `_assert_ran`
fails a loop that silently covered fewer backends than were configured. A SKIP
IS NOT A PASS.

THE FALSIFIER. `RestoringTheUnconditionalOccupiesSlotReturnMustRedden` puts the
pre-fix branch back — the unconditional `existing.status.occupies_slot` return
— and requires the refusal assertions to FAIL, so this file cannot go green
against the defect it was written for.
"""

import json
import threading
import unittest
import urllib.error
import urllib.request
from http.cookiejar import CookieJar
from http.server import ThreadingHTTPServer

from helpers import BACKEND  # noqa: F401  (ensures sys.path is set up)
from test_coach_team_authorization import _CoachAuthHarness

from hockey_scheduler.domain import Role
from hockey_scheduler.services.roster_service import (
    ATTRIBUTION_MISSING, TEAM_SCOPE_VIOLATION)
from hockey_scheduler.web import server as srv
from hockey_scheduler.web.auth import DEMO_PASSWORD, DEMO_USERS


class _SideOwnedHarness(_CoachAuthHarness):
    """Adds the OCCUPANCY and AUDIT observations this blocker is about to the
    shared coach-authorization harness."""

    def _occupants(self, fx, team_id):
        """The PLAYER IDENTITIES occupying ``team_id``'s slots, read off the
        rows' own durable attribution — never a count, which a same-cardinality
        swap would satisfy."""
        return sorted(e.player_id
                      for e in fx["api"].store.roster_for_game(fx["gid"])
                      if e.status.occupies_slot and e.team_side == team_id)

    def _audits(self, fx):
        return sorted((a.id, a.action.value)
                      for a in fx["api"].store.audit_for_game(fx["gid"]))

    def _roster_selected(self, fx):
        return [a for a in self._audits(fx) if a[1] == "roster_selected"]

    def _row(self, fx, pid):
        e = fx["api"].store.roster_entry_for_player(fx["gid"], pid)
        if e is None:
            return None
        return (e.id, e.status.value, e.team_side, e.selected_by,
                None if e.seated_position is None else e.seated_position.value)

    def _mover_seated_home(self, store, name="Mo Mover"):
        """THE FIXTURE THIS BLOCKER REQUIRES: a player seated by the HOME coach
        whose live membership has since moved to AWAY. Pointer and membership
        disagree on purpose."""
        fx = self._build(store)
        pid = self._player(fx, fx["home"], name)
        fx["api"].select_roster(fx["gid"], [pid], actor_id="coach_home",
                                authorized_team_id=fx["home"])
        self._move_side(fx, pid, fx["away"])
        return fx, pid


# =========================================================================== #
# the refusal                                                                 #
# =========================================================================== #
class AForeignOccupyingRowIsRefusedNotReturned(
        _SideOwnedHarness, unittest.TestCase):

    def test_away_coach_is_refused_with_no_row_no_write_no_audit(self):
        ran = []
        for label, store in self._stores():
            try:
                self._assert_backend(label, store)
                store.clear_all_data()
                fx, pid = self._mover_seated_home(store)
                api = fx["api"]

                before_row = self._row(fx, pid)
                before_audits = self._audits(fx)
                before_home = self._occupants(fx, fx["home"])
                before_away = self._occupants(fx, fx["away"])

                with self.subTest(backend=label):
                    # the live context really HAS moved: without this the
                    # test would be proving the live gate, not the durable one
                    ctx = api.roster.resolve_membership_contexts_for_game(
                        store.get_game(fx["gid"]))[pid]
                    self.assertEqual(ctx.team_id, fx["away"], label)

                    with self._write_attempts(store) as calls:
                        out = api.select_roster(
                            fx["gid"], [pid], actor_id="coach_away",
                            authorized_team_id=fx["away"])

                    # -- the structured refusal -------------------------------
                    self.assertIn("error", out, out)
                    self.assertEqual(out["error"]["code"], "forbidden", out)
                    self.assertEqual(out["error"]["details"]["reason"],
                                     TEAM_SCOPE_VIOLATION, out)

                    # -- NO ROW DISCLOSURE -----------------------------------
                    # the refusal must not hand the opposing coach the HOME
                    # row's identity, its seating actor, or its slot.
                    blob = json.dumps(out)
                    self.assertNotIn(before_row[0], blob, out)   # entry id
                    self.assertNotIn("coach_home", blob, out)    # selected_by
                    self.assertNotIn("seated_position", blob, out)
                    self.assertNotIn("selection_source", blob, out)

                    # -- ZERO WRITE ATTEMPTS ---------------------------------
                    # not "rolled back": never attempted. `next_id` counts —
                    # minting an audit id is a write attempt too.
                    self.assertEqual(list(calls), [], (label, list(calls)))

                    # -- nothing durable moved, by IDENTITY ------------------
                    self.assertEqual(self._row(fx, pid), before_row, label)
                    self.assertEqual(self._audits(fx), before_audits, label)
                    self.assertEqual(self._occupants(fx, fx["home"]),
                                     before_home, label)
                    self.assertEqual(self._occupants(fx, fx["home"]), [pid],
                                     label)
                    # AWAY's own side is still OPEN — the coach's real
                    # complaint in the blocker was a false success while
                    # their side stayed unfilled.
                    self.assertEqual(self._occupants(fx, fx["away"]),
                                     before_away, label)
                    self.assertEqual(self._occupants(fx, fx["away"]), [], label)
                ran.append(label)
            finally:
                self._close(label, store)
        self._assert_ran(ran, "SELECT / FOREIGN OCCUPYING ROW")

    def test_null_attribution_fails_closed_under_the_existing_typed_rule(self):
        """A pre-061 row carries `team_side IS NULL` and cannot prove whose it
        is. It must be refused with the EXISTING `attribution_missing` reason —
        not a second vocabulary minted for this branch."""
        ran = []
        for label, store in self._stores():
            try:
                self._assert_backend(label, store)
                store.clear_all_data()
                fx, pid = self._mover_seated_home(store)
                api = fx["api"]
                # the one store-write this file makes: manufacture the
                # pre-migration shape, which no supported write path produces.
                with store.transaction():
                    e = store.roster_entry_for_player(fx["gid"], pid)
                    e.team_side = None
                    e.seated_position = None
                    store.save_roster_entry(e)
                before_audits = self._audits(fx)

                with self.subTest(backend=label):
                    with self._write_attempts(store) as calls:
                        out = api.select_roster(
                            fx["gid"], [pid], actor_id="coach_away",
                            authorized_team_id=fx["away"])
                    self.assertEqual(out["error"]["details"]["reason"],
                                     ATTRIBUTION_MISSING, out)
                    self.assertEqual(list(calls), [], (label, list(calls)))
                    self.assertEqual(self._audits(fx), before_audits, label)
                    self.assertIsNone(
                        store.roster_entry_for_player(
                            fx["gid"], pid).team_side, label)
                ran.append(label)
            finally:
                self._close(label, store)
        self._assert_ran(ran, "SELECT / NULL ATTRIBUTION")


# =========================================================================== #
# the three behaviours the refusal must NOT break                             #
# =========================================================================== #
class TheOwningCoachsRetryIsATrueNoOp(_SideOwnedHarness, unittest.TestCase):

    def test_home_retry_writes_nothing_and_appends_no_duplicate_audit(self):
        """The blocker's second half: `select_roster` must not claim a
        selection that did not occur. A HOME retry of an already-seated HOME
        row changes nothing, so it audits nothing."""
        ran = []
        for label, store in self._stores():
            try:
                self._assert_backend(label, store)
                store.clear_all_data()
                fx = self._build(store)
                pid = self._player(fx, fx["home"], "Hana Home")
                api = fx["api"]
                first = api.select_roster(fx["gid"], [pid],
                                          actor_id="coach_home",
                                          authorized_team_id=fx["home"])
                after_first_row = self._row(fx, pid)
                after_first_audits = self._audits(fx)

                with self.subTest(backend=label):
                    self.assertEqual(len(self._roster_selected(fx)), 1, label)

                    with self._write_attempts(store) as calls:
                        again = api.select_roster(fx["gid"], [pid],
                                                  actor_id="coach_home",
                                                  authorized_team_id=fx["home"])

                    # the retry still SUCCEEDS and still returns the row —
                    # idempotency is preserved, it is merely side-owned now
                    self.assertEqual(again, first, label)
                    self.assertEqual(again[0]["team_side"], fx["home"], label)
                    # ...and it is a TRUE no-op: no write attempted at all
                    self.assertEqual(list(calls), [], (label, list(calls)))
                    self.assertEqual(self._row(fx, pid), after_first_row, label)
                    # NO SECOND `roster_selected`
                    self.assertEqual(self._audits(fx), after_first_audits,
                                     label)
                    self.assertEqual(len(self._roster_selected(fx)), 1, label)
                ran.append(label)
            finally:
                self._close(label, store)
        self._assert_ran(ran, "SELECT / OWN RETRY IS A NO-OP")


class ARemovedRowIsLegitimatelyReSeatableByTheNewSide(
        _SideOwnedHarness, unittest.TestCase):

    def test_away_reseats_a_removed_home_row_with_away_attribution(self):
        """The refusal must not become a trap. Once the HOME row is REMOVED it
        no longer occupies a slot, so it carries no side-ownership claim and
        the live context may revive AND REATTRIBUTE it."""
        ran = []
        for label, store in self._stores():
            try:
                self._assert_backend(label, store)
                store.clear_all_data()
                fx = self._build(store)
                pid = self._player(fx, fx["home"], "Mo Mover")
                api = fx["api"]
                api.select_roster(fx["gid"], [pid], actor_id="coach_home",
                                  authorized_team_id=fx["home"])
                # HOME gives the seat up through the product's own write path
                api.remove_player(fx["gid"], pid, actor_id="coach_home",
                                  authorized_team_id=fx["home"])
                self._move_side(fx, pid, fx["away"])
                entry_id = store.roster_entry_for_player(fx["gid"], pid).id
                before_selected = len(self._roster_selected(fx))

                with self.subTest(backend=label):
                    self.assertEqual(self._occupants(fx, fx["home"]), [], label)
                    out = api.select_roster(fx["gid"], [pid],
                                            actor_id="coach_away",
                                            authorized_team_id=fx["away"])
                    self.assertNotIn("error", out, out)

                    e = store.roster_entry_for_player(fx["gid"], pid)
                    # REVIVED, not duplicated: one row per (game, player)
                    self.assertEqual(e.id, entry_id, label)
                    # DURABLE ATTRIBUTION IS NOW AWAY
                    self.assertEqual(e.team_side, fx["away"], label)
                    self.assertEqual(e.selected_by, "coach_away", label)
                    self.assertTrue(e.status.occupies_slot, label)
                    # and the seasonal position comes from the AWAY context
                    ctx = api.roster.resolve_membership_contexts_for_game(
                        store.get_game(fx["gid"]))[pid]
                    self.assertEqual(e.seated_position, ctx.position, label)
                    # occupancy moved sides, by identity
                    self.assertEqual(self._occupants(fx, fx["away"]), [pid],
                                     label)
                    self.assertEqual(self._occupants(fx, fx["home"]), [], label)
                    # EXACTLY ONE new audit — something really did change
                    self.assertEqual(len(self._roster_selected(fx)),
                                     before_selected + 1, label)
                ran.append(label)
            finally:
                self._close(label, store)
        self._assert_ran(ran, "SELECT / LEGITIMATE RE-SEAT")


class AnUnscopedOperatorIsUnchanged(_SideOwnedHarness, unittest.TestCase):

    def test_authorized_team_id_none_still_returns_the_occupying_row(self):
        """`authorized_team_id=None` means NO coach constraint — a League
        Admin / operator / in-process caller. The preflight must abstain
        entirely, leaving the pre-existing unconditional behaviour.

        TWO SEPARATE CLAIMS, and only the first is about the operator.
        AUTHORIZATION is preserved: the operator still receives the
        HOME-attributed row rather than the `team_scope_violation` a Coach
        would now get, which is the "preserve unscoped-operator behaviour
        explicitly" half of the ruling. The AUDIT suppression below is NOT
        role-conditional — it is the separate rule that a call which changed
        no roster state appends no `ROSTER_SELECTED`, and it applies to every
        caller alike. So this test reddens against the pre-fix code on the
        audit assertion while the authorization assertion holds either way,
        which is exactly the intended split.
        """
        ran = []
        for label, store in self._stores():
            try:
                self._assert_backend(label, store)
                store.clear_all_data()
                fx, pid = self._mover_seated_home(store)
                api = fx["api"]
                before_row = self._row(fx, pid)

                with self.subTest(backend=label):
                    with self._write_attempts(store) as calls:
                        out = api.select_roster(fx["gid"], [pid],
                                                actor_id="admin")
                    # the operator still receives the HOME-owned row, exactly
                    # as before this change
                    self.assertNotIn("error", out, out)
                    self.assertEqual(out[0]["team_side"], fx["home"], label)
                    self.assertEqual(self._row(fx, pid), before_row, label)
                    # ...and it is still a no-op, so still no duplicate audit
                    self.assertEqual(list(calls), [], (label, list(calls)))
                ran.append(label)
            finally:
                self._close(label, store)
        self._assert_ran(ran, "SELECT / UNSCOPED OPERATOR")


# =========================================================================== #
# the batch                                                                   #
# =========================================================================== #
class TheWholeBatchIsPreflightedBeforeAnyWrite(
        _SideOwnedHarness, unittest.TestCase):

    def test_one_foreign_row_refuses_the_batch_before_earlier_players_write(
            self):
        """ATOMIC REFUSAL, and specifically refusal BEFORE the first write.

        The batch is ordered so the legitimate creates come FIRST and the
        foreign occupying row LAST — the order in which a per-player check
        inside the write loop would already have seated players 1 and 2 before
        noticing player 3. `@_transactional` would roll those back, so a
        snapshot diff cannot tell that case apart from this one; the write
        ATTEMPT spy can, and must see nothing."""
        ran = []
        for label, store in self._stores():
            try:
                self._assert_backend(label, store)
                store.clear_all_data()
                fx, mover = self._mover_seated_home(store)
                api = fx["api"]
                # two players who are legitimately AWAY's to seat
                a1 = self._player(fx, fx["away"], "Ann Away")
                a2 = self._player(fx, fx["away"], "Abe Away")
                before_audits = self._audits(fx)
                before_home = self._occupants(fx, fx["home"])

                with self.subTest(backend=label):
                    with self._write_attempts(store) as calls:
                        out = api.select_roster(
                            fx["gid"], [a1, a2, mover],
                            actor_id="coach_away",
                            authorized_team_id=fx["away"])

                    self.assertEqual(out["error"]["details"]["reason"],
                                     TEAM_SCOPE_VIOLATION, out)
                    # NOT ONE WRITE WAS ATTEMPTED, though two players ahead of
                    # the foreign row were perfectly seatable
                    self.assertEqual(list(calls), [], (label, list(calls)))
                    # neither earlier player was seated
                    self.assertIsNone(
                        store.roster_entry_for_player(fx["gid"], a1), label)
                    self.assertIsNone(
                        store.roster_entry_for_player(fx["gid"], a2), label)
                    self.assertEqual(self._occupants(fx, fx["away"]), [], label)
                    self.assertEqual(self._occupants(fx, fx["home"]),
                                     before_home, label)
                    self.assertEqual(self._audits(fx), before_audits, label)

                    # and the same batch WITHOUT the foreign row succeeds, so
                    # the refusal above is about that row and nothing else
                    ok = api.select_roster(fx["gid"], [a1, a2],
                                           actor_id="coach_away",
                                           authorized_team_id=fx["away"])
                    self.assertNotIn("error", ok, ok)
                    self.assertEqual(self._occupants(fx, fx["away"]),
                                     sorted([a1, a2]), label)
                ran.append(label)
            finally:
                self._close(label, store)
        self._assert_ran(ran, "SELECT / MIXED BATCH ATOMIC REFUSAL")

    def test_a_repeated_player_id_in_one_batch_still_seats_exactly_one_row(
            self):
        """THE PREFLIGHT MUST NOT BECOME THE WRITE LOOP'S SOURCE OF TRUTH.

        `player_ids` may legitimately contain the same player twice, and the
        output is required to echo the caller's order duplicates included
        (`test_lifecycle_concurrency.RosterSelectionOrderParityTest`). The write
        loop takes its idempotent branch for the second occurrence only because
        it RE-READS and sees the row the first occurrence just inserted.

        An earlier draft of this change cached the classification pass's row and
        reused it below, which served the second occurrence a snapshot taken
        before any write: it inserted a second row, giving two rows on Memory
        and an IntegrityError against the one-row-per-(game, player) unique
        index on SQLite. That defect was caught by an unrelated suite, so it is
        pinned HERE too — beside the preflight whose optimization caused it."""
        ran = []
        for label, store in self._stores():
            try:
                self._assert_backend(label, store)
                store.clear_all_data()
                fx = self._build(store)
                p1 = self._player(fx, fx["home"], "Hana Home")
                p2 = self._player(fx, fx["home"], "Hugo Home")
                api = fx["api"]

                with self.subTest(backend=label):
                    out = api.select_roster(
                        fx["gid"], [p2, p1, p2], actor_id="coach_home",
                        authorized_team_id=fx["home"])
                    self.assertNotIn("error", out, out)
                    # caller order, duplicates included
                    self.assertEqual([e["player_id"] for e in out],
                                     [p2, p1, p2], label)
                    # ...but ONE row per player, by identity
                    for pid in (p1, p2):
                        rows = [e for e in store.roster_for_game(fx["gid"])
                                if e.player_id == pid]
                        self.assertEqual(len(rows), 1, (label, pid))
                    self.assertEqual(self._occupants(fx, fx["home"]),
                                     sorted([p1, p2]), label)
                    # a real change happened, so exactly one audit
                    self.assertEqual(len(self._roster_selected(fx)), 1, label)
                ran.append(label)
            finally:
                self._close(label, store)
        self._assert_ran(ran, "SELECT / REPEATED ID IN ONE BATCH")


# =========================================================================== #
# authenticated HTTP — the production route the blocker names                 #
# =========================================================================== #
class OverRealAuthenticatedHttp(_SideOwnedHarness, unittest.TestCase):
    """`web/server.py` passes the authenticated Coach scope straight into
    `authorized_team_id`, so this defect was reachable through the production
    HTTP route — that is what made it a live production blocker rather than a
    test blind spot.

    AND THE UNDER-LOCK GATE IS INDEPENDENTLY REACHABLE HERE, unlike the
    `availability/remind` route: `POST /api/games/{gid}/roster/select` takes
    only `player_ids`, never a `team_id`, so there is no explicit foreign team
    for a scope preflight to reject. The request reaches the service with the
    coach's own scope and the service gate is the ONLY thing standing between
    an opposing coach and another side's roster row. Deleting it reddens this
    class directly."""

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

    def test_opposing_coach_gets_403_and_no_home_row_id_or_actor(self):
        ran = []
        for label, store in self._stores():
            try:
                self._assert_backend(label, store)
                store.clear_all_data()
                fx = self._build(store)
                self._accounts(fx)
                pid = self._player(fx, fx["home"], "Mo Mover")

                home = self._login(fx, "coachhome")
                status, seated = self._req(
                    home, "POST", f"/api/games/{fx['gid']}/roster/select",
                    {"player_ids": [pid]})
                self.assertEqual(status, 200, seated)
                entry_id = seated[0]["id"]
                self.assertEqual(seated[0]["team_side"], fx["home"], seated)

                self._move_side(fx, pid, fx["away"])
                before_audits = self._audits(fx)
                before_row = self._row(fx, pid)

                away = self._login(fx, "coachaway")
                with self.subTest(backend=label):
                    status, body = self._req(
                        away, "POST",
                        f"/api/games/{fx['gid']}/roster/select",
                        {"player_ids": [pid]})

                    self.assertEqual(status, 403, body)
                    self.assertEqual(body["error"]["code"], "forbidden", body)
                    self.assertEqual(body["error"]["details"]["reason"],
                                     TEAM_SCOPE_VIOLATION, body)

                    # NO HOME ROW, ID OR ACTOR anywhere in the response
                    blob = json.dumps(body)
                    self.assertNotIn(entry_id, blob, body)
                    self.assertNotIn("user_coach_home", blob, body)
                    self.assertNotIn("selected_by", blob, body)
                    self.assertNotIn("seated_position", blob, body)

                    # nothing written, nothing audited, occupancy unmoved
                    self.assertEqual(self._audits(fx), before_audits, label)
                    self.assertEqual(self._row(fx, pid), before_row, label)
                    self.assertEqual(self._occupants(fx, fx["home"]), [pid],
                                     label)
                    self.assertEqual(self._occupants(fx, fx["away"]), [], label)
                ran.append(label)
            finally:
                self._close(label, store)
        self._assert_ran(ran, "SELECT HTTP / FOREIGN OCCUPYING ROW")

    def test_the_owning_coach_over_http_still_succeeds_and_then_no_ops(self):
        """The 403 above is about the SIDE, not about the route: the coach who
        owns the row still gets 200, and their retry still appends no
        duplicate audit."""
        ran = []
        for label, store in self._stores():
            try:
                self._assert_backend(label, store)
                store.clear_all_data()
                fx = self._build(store)
                self._accounts(fx)
                pid = self._player(fx, fx["home"], "Hana Home")
                home = self._login(fx, "coachhome")

                with self.subTest(backend=label):
                    s1, b1 = self._req(
                        home, "POST",
                        f"/api/games/{fx['gid']}/roster/select",
                        {"player_ids": [pid]})
                    self.assertEqual(s1, 200, b1)
                    self.assertEqual(b1[0]["team_side"], fx["home"], b1)
                    audits_after_first = self._audits(fx)
                    self.assertEqual(len(self._roster_selected(fx)), 1, label)

                    s2, b2 = self._req(
                        home, "POST",
                        f"/api/games/{fx['gid']}/roster/select",
                        {"player_ids": [pid]})
                    self.assertEqual(s2, 200, b2)
                    self.assertEqual(b2, b1, (b1, b2))
                    self.assertEqual(self._audits(fx), audits_after_first,
                                     label)
                    self.assertEqual(self._occupants(fx, fx["home"]), [pid],
                                     label)
                ran.append(label)
            finally:
                self._close(label, store)
        self._assert_ran(ran, "SELECT HTTP / OWNING COACH")


# =========================================================================== #
# falsifier                                                                   #
# =========================================================================== #
class RestoringTheUnconditionalOccupiesSlotReturnMustRedden(
        _SideOwnedHarness, unittest.TestCase):
    """THE RULED FALSIFIER: put the pre-fix branch back and require this
    file's central assertions to FAIL.

    The defect was that ANY occupying row was returned as idempotent, so the
    falsifier restores exactly that — a `select_roster` whose existing-row
    branch consults `existing.status.occupies_slot` and nothing else. If the
    refusal, the zero-write claim and the no-duplicate-audit claim all still
    held against that implementation, they would not be evidence of anything.
    """

    def _patched_select(self):
        """`select_roster` with the durable classification removed: the live
        gate stays (it is not what was broken), the whole-batch preflight and
        the audit suppression are gone."""
        from hockey_scheduler.domain import (
            AuditAction, RosterEntryStatus, RosterRole, SelectionSource)
        from hockey_scheduler.domain.models import GameRosterEntry
        from hockey_scheduler.domain.errors import NotEligibleError, NotFoundError
        from hockey_scheduler.services import season_guard

        def broken(self, game_id, player_ids, actor_id=None,
                   authorized_team_id=None):
            game = self._require_game(game_id)
            game = self._guard_mutable(game)
            locked = {pid: self.store.get_player_for_update(pid)
                      for pid in sorted(set(player_ids))}
            bound = season_guard.game_is_league_season_bound(game)
            contexts = (self.resolve_membership_contexts_for_game(game)
                        if bound else {})
            resolved = {}
            for player_id in player_ids:
                player = locked.get(player_id)
                ctx = None
                if player is not None:
                    ctx = (contexts.get(player_id) if bound
                           else self.resolve_membership_context(game, player))
                resolved[player_id] = ctx
                self._require_authorized_team(
                    authorized_team_id, ctx.team_id if ctx else None, "player",
                    comparand="live")
            entries = []
            for player_id in player_ids:
                player = locked[player_id]
                if player is None:
                    raise NotFoundError(f"Player {player_id} not found.")
                ctx = resolved[player_id]
                if ctx is None:
                    raise NotEligibleError(
                        f"{player.name} is not on either team in this game.")
                if not player.is_active:
                    raise NotEligibleError(
                        f"{player.name} is not an active player.")
                now = self.clock()
                existing = self.store.roster_entry_for_player(game_id, player_id)
                if existing is not None:
                    # THE DEFECT, restored verbatim.
                    if existing.status.occupies_slot:
                        entries.append(existing)
                        continue
                    existing.roster_role = RosterRole.SELECTED
                    existing.selection_source = SelectionSource.COACH_SELECTED
                    existing.status = RosterEntryStatus.SELECTED
                    existing.selected_by = actor_id
                    existing.updated_at = now
                    existing.team_side = ctx.team_id
                    existing.seated_position = ctx.position
                    self.store.save_roster_entry(existing)
                    entries.append(existing)
                    continue
                entry = GameRosterEntry(
                    id=self.store.next_id("entry"), game_id=game_id,
                    player_id=player_id, roster_role=RosterRole.SELECTED,
                    selection_source=SelectionSource.COACH_SELECTED,
                    status=RosterEntryStatus.SELECTED, selected_at=now,
                    updated_at=now, selected_by=actor_id,
                    team_side=ctx.team_id, seated_position=ctx.position)
                self.store.add_roster_entry(entry)
                entries.append(entry)
            self._audit(game_id, AuditAction.ROSTER_SELECTED,
                        actor_id=actor_id,
                        detail={"player_ids": player_ids})
            return entries
        return broken

    def _with_defect(self):
        from hockey_scheduler.services.roster_service import (
            RosterService, _transactional)
        original = RosterService.select_roster
        RosterService.select_roster = _transactional(self._patched_select())
        self.addCleanup(setattr, RosterService, "select_roster", original)

    def test_the_refusal_assertions_fail_against_the_restored_defect(self):
        from hockey_scheduler.store import InMemoryStore
        self._with_defect()
        store = InMemoryStore()
        try:
            fx, pid = self._mover_seated_home(store)
            api = fx["api"]
            before_audits = self._audits(fx)

            with self._write_attempts(store) as calls:
                out = api.select_roster(fx["gid"], [pid],
                                        actor_id="coach_away",
                                        authorized_team_id=fx["away"])

            # EVERY ONE of these is what the fixed code must do, and EVERY ONE
            # of them fails here — which is the point of the falsifier.
            with self.assertRaises(AssertionError):
                self.assertIn("error", out)
            with self.assertRaises(AssertionError):
                self.assertEqual(list(calls), [])
            with self.assertRaises(AssertionError):
                self.assertEqual(self._audits(fx), before_audits)
            # and the defect's signature is present: the HOME row, returned
            # to the AWAY coach, with HOME attribution and HOME's actor
            self.assertEqual(out[0]["team_side"], fx["home"], out)
            self.assertEqual(out[0]["selected_by"], "coach_home", out)
        finally:
            store.close() if hasattr(store, "close") else None

    def test_the_no_duplicate_audit_assertion_fails_against_the_defect(self):
        from hockey_scheduler.store import InMemoryStore
        self._with_defect()
        store = InMemoryStore()
        fx = self._build(store)
        pid = self._player(fx, fx["home"], "Hana Home")
        api = fx["api"]
        api.select_roster(fx["gid"], [pid], actor_id="coach_home",
                          authorized_team_id=fx["home"])
        audits_after_first = self._audits(fx)
        api.select_roster(fx["gid"], [pid], actor_id="coach_home",
                          authorized_team_id=fx["home"])
        with self.assertRaises(AssertionError):
            self.assertEqual(self._audits(fx), audits_after_first)
        self.assertEqual(len(self._roster_selected(fx)), 2,
                         "the defect appends a second roster_selected")


if __name__ == "__main__":
    unittest.main(verbosity=2)
