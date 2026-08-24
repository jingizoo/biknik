"""PART C — ONE effective batch team, resolved before authorization (#205).

THE OWNER'S SECOND BLOCKER (comment 5391127041), a STEADY-STATE hole and not
a race at all:

    "There is also a separate steady-state blocker, not only a race.
     `scope_violation` checks `team_id` only when the body value is truthy,
     while `_batch_team` turns an omitted value into `game.home_team_id`. I
     reproduced this through authenticated HTTP on this head: an AWAY Coach
     posted `{}` to `/api/games/{id}/build-roster`, received 200, the response
     named HOME, and a current HOME player was durably written as `confirmed`
     with `team_side=HOME`. `roster/copy-previous` has the same
     target-selection shape. This allows an opposing coach to create and
     confirm another team's roster without any interleaving."

REPRODUCED AGAIN at head 22bd6de, through authenticated HTTP, on Memory,
SQLite AND real PostgreSQL, on BOTH routes::

    route      = /api/games/{gid}/build-roster   body={}
    coach      = AWAY (team_2)
    HTTP       = 200
    resp.team_id = 'team_1'  (HOME='team_1' AWAY='team_2')
    resp.seated  = ['player_1']
    roster after = [('player_1', 'confirmed', 'team_1', 'forward')]
    audit rows   = ['roster_selected', 'availability_set',
                    'roster_batch_seated']

    route      = /api/games/{gid}/roster/copy-previous   body={}
    coach      = AWAY (team_5)
    HTTP       = 200
    resp.team_id = 'team_4'  (HOME='team_4' AWAY='team_5')
    roster after = [('player_2', 'selected', 'team_4', 'forward')]

Each half looked reasonable alone — the preflight abstained because a falsy
``team_id`` is "no target to constrain", and the service defaulted because
HOME is the documented #25 default. Together they let an opposing coach
create and confirm another team's roster with an EMPTY BODY.

THE CORRECTION: ``_batch_team`` now resolves ONE effective team and
authorizes it in the same place, under the Season lock, before candidate
discovery or any write.

THE PINNED OMISSION BEHAVIOUR — the ruling requires the choice to be pinned
either way, and this is the choice: FOR A COACH, AN OMITTED ``team_id`` MEANS
THEIR OWN SIDE, never HOME by fallback, and never a refusal. It is the action
they are unambiguously authorized for; it is what every existing one-click
coach flow already intends; and refusing would make the AWAY coach's ``{}``
behave differently from the HOME coach's ``{}`` for no reason a user could
see. An EXPLICIT different team stays FORBIDDEN — it is not silently
rewritten to their own side, because that would turn a mistaken or malicious
request into a successful one.

FOR AN UNSCOPED ROLE the HOME default is preserved byte-for-byte, exactly as
the ruling asks ("Preserve the existing HOME default only for unscoped roles
where that behavior is already accepted").

AND ONE BATCH SEATS ONE SIDE. Candidate discovery is deliberately NOT
spine-derived, so both pools can surface a player whose CURRENT context
resolves onto the OTHER side of this same game. ``_partition_candidates``
used to call such a candidate seatable because a context merely EXISTED, and
``select_roster`` then wrote the row on ``ctx.team_id`` — measured on Memory
and SQLite at head a90f314, auto-filling HOME with a pointer-HOME /
membership-AWAY player: ``team_id=HOME seated=[player_1] skipped=[]`` while
storage held ``team_side=AWAY``. It is now a REPORTED skip
(``membership_other_side``), never a silent cross-side seat.

TRI-STORE OVER REAL AUTHENTICATED HTTP. Every case below runs against Memory,
SQLite and — when TEST_DATABASE_URL is set — real PostgreSQL, through a live
``ThreadingHTTPServer`` with a real cookie session, so the Coach scope under
test is the one ``_resolve_role`` actually produces rather than one the test
asserts into existence. ``_assert_backend`` PROVES the backend and
``_assert_ran`` fails a silently-narrowed loop. A SKIP IS NOT A PASS.

...AND, SINCE THE REVIEW ROUND ON THIS PR, ALSO BELOW HTTP (finding F-2).
Every case above enters through ``/api/games/{gid}/...``, where a truthy,
foreign ``body["team_id"]`` is refused by the ``scope_violation`` PREFLIGHT
before the service is ever reached — so a 403 there says nothing about the
gate underneath, which the ruling insists is the authoritative one. A
reviewer replaced ``_batch_team``'s refusal with a SILENT REWRITE to the
coach's own side and the whole suite stayed green. See
``AnExplicitForeignTeamIsRefusedBelowThePreflight`` at the bottom of this
file: same tri-store discipline, but every call is a DIRECT facade call
carrying ``authorized_team_id``, so the preflight is out of the picture and
the same rewrite reddens it.
"""

import json
import os
import threading
import unittest
import urllib.error
import urllib.request
from http.cookiejar import CookieJar
from http.server import ThreadingHTTPServer

from helpers import (BACKEND, FakeClock, fresh_sql_store,  # noqa: F401
                     write_attempt_spy)
from test_substitute_membership_cutover import ADMIN, _at

from hockey_scheduler.api import ApiService
from hockey_scheduler.domain import Player, Position, Role
from hockey_scheduler.services import membership_spine as spine
from hockey_scheduler.services.roster_service import TEAM_SCOPE_VIOLATION
from hockey_scheduler.store import InMemoryStore, SqlStore
from hockey_scheduler.web import server as srv
from hockey_scheduler.web.auth import DEMO_PASSWORD, DEMO_USERS

_PG_SKIP = ("PostgreSQL not configured (TEST_DATABASE_URL); the batch "
            "effective-team contract is NOT covered on the backend whose row "
            "locks and real SQL it is about.")


class _BatchTeamHarness:

    # -- backends --------------------------------------------------------
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

    # -- fixture ---------------------------------------------------------
    def _build(self, store, prior=False):
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
                               target_goalies=0, target_skaters=4,
                               actor_id=ADMIN, league_id=league["id"])
        self.assertNotIn("error", game, game)
        api.publish_game(game["id"], actor_id=ADMIN)
        fx = {"api": api, "season": season, "league": league, "rink": rink,
              "game": game, "gid": game["id"],
              "ls_id": game["league_season_id"],
              "home": teams["home"]["id"], "away": teams["away"]["id"],
              "third": teams["third"]["id"]}
        if prior:
            pslot = api.create_ice_slot(rink["id"], _at(2).isoformat(),
                                        _at(3).isoformat(), "game",
                                        actor_id=ADMIN)
            pg = api.create_game(season["id"], None, teams["home"]["id"],
                                 teams["away"]["id"], pslot["id"],
                                 target_goalies=0, target_skaters=8,
                                 actor_id=ADMIN, league_id=league["id"])
            self.assertNotIn("error", pg, pg)
            api.publish_game(pg["id"], actor_id=ADMIN)
            fx["pid"] = pg["id"]
        return fx

    def _player(self, fx, team_id, name):
        p = fx["api"].create_player(team_id, name, "forward", actor_id=ADMIN)
        self.assertNotIn("error", p, p)
        return p["id"]

    def _both_sides(self, fx):
        """One eligible player on EACH side, so "which side did the batch
        act on" is answerable from the seated ids alone and neither answer
        can be reached by accident."""
        return (self._player(fx, fx["home"], "Hana Home"),
                self._player(fx, fx["away"], "Ava Away"))

    def _seat_prior(self, fx, ids):
        res = fx["api"].select_roster(fx["pid"], ids, actor_id=ADMIN)
        self.assertNotIn("error", res if isinstance(res, dict) else {}, res)

    # -- accounts + HTTP --------------------------------------------------
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
            # Closed explicitly: an HTTPError holds an open response body,
            # and letting the GC reclaim it emits a ResourceWarning at
            # interpreter shutdown that run_parallel.py does NOT filter.
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

    # -- observation ------------------------------------------------------
    def _rows(self, fx, gid=None):
        return sorted((e.player_id, e.status.value, e.team_side)
                      for e in fx["api"].store.roster_for_game(gid or fx["gid"]))

    def _writes(self, fx):
        """Every durable class a batch can touch, as identity values."""
        store, gid = fx["api"].store, fx["gid"]
        return {
            "roster": self._rows(fx),
            "audit": sorted((a.id, a.action.value)
                            for a in store.audit_for_game(gid)),
            "notifications": sorted(
                (n.id, n.type.value)
                for n in store.notifications_for_game(gid)),
            "availability": sorted(
                (a.id, a.player_id, a.availability_status.value)
                for a in store.availability_for_game(gid)),
        }


# =========================================================================== #
# the owner's exact reproduction, now closed                                  #
# =========================================================================== #
class AnOpposingCoachCannotReachTheOtherSidesRoster(_BatchTeamHarness,
                                                    unittest.TestCase):

    ROUTES = ("build-roster", "roster/copy-previous")

    def _fixture(self, store, route):
        prior = route == "roster/copy-previous"
        fx = self._build(store, prior=prior)
        home_pid, away_pid = self._both_sides(fx)
        if prior:
            # BOTH sides have a prior roster, so a copy has something real to
            # find whichever side it resolves to — a zero-seat result must
            # never be mistaken for "the hole is closed".
            self._seat_prior(fx, [home_pid, away_pid])
        self._accounts(fx)
        return fx, home_pid, away_pid

    def test_away_coach_with_an_empty_body_never_touches_home(self):
        """THE OWNER'S EXACT REPRODUCTION. The empty body no longer falls
        back to HOME; it resolves to the AWAY coach's OWN side — the pinned
        behaviour — and HOME's roster, audit, notification and availability
        state are all left exactly as they were."""
        ran = []
        for label, store in self._stores():
            try:
                self._assert_backend(label, store)
                for route in self.ROUTES:
                    store.clear_all_data()
                    fx, home_pid, away_pid = self._fixture(store, route)
                    before = self._writes(fx)
                    away = self._login(fx, "coachaway")
                    with self.subTest(backend=label, route=route):
                        status, body = self._req(
                            away, "POST", f"/api/games/{fx['gid']}/{route}", {})
                        self.assertEqual(status, 200, (route, body))
                        # THE PINNED OMISSION BEHAVIOUR: their own side.
                        self.assertEqual(body["team_id"], fx["away"],
                                         (route, body))
                        self.assertEqual(body["seated"], [away_pid],
                                         (route, body))
                        # NOT ONE HOME ROW, on any durable surface.
                        rows = self._rows(fx)
                        self.assertEqual(
                            [r for r in rows if r[2] == fx["home"]], [],
                            (route, rows))
                        self.assertEqual([r[0] for r in rows], [away_pid],
                                         (route, rows))
                        # THE DECISIVE ASSERTION: the ONLY player id that
                        # appears anywhere in the target game's durable state
                        # is the AWAY one. Checked across roster AND
                        # availability rather than on the roster alone,
                        # because build-roster also CONFIRMS what it seats and
                        # the reproduction's damage included an
                        # `availability_set` row.
                        after = self._writes(fx)
                        self.assertEqual(before["roster"], [], route)
                        touched = ({r[0] for r in after["roster"]}
                                   | {a[1] for a in after["availability"]})
                        self.assertEqual(touched, {away_pid}, (route, after))
                ran.append(label)
            finally:
                self._close(label, store)
        self._assert_ran(ran, "BATCH EFFECTIVE TEAM / AWAY {}")

    def test_away_coach_naming_home_explicitly_is_forbidden_zero_writes(self):
        """An explicit foreign team is REFUSED, not silently rewritten to
        the coach's own side — and nothing at all is written, on either
        side, on any durable surface."""
        ran = []
        for label, store in self._stores():
            try:
                self._assert_backend(label, store)
                for route in self.ROUTES:
                    store.clear_all_data()
                    fx, home_pid, away_pid = self._fixture(store, route)
                    before = self._writes(fx)
                    away = self._login(fx, "coachaway")
                    with self.subTest(backend=label, route=route):
                        status, body = self._req(
                            away, "POST", f"/api/games/{fx['gid']}/{route}",
                            {"team_id": fx["home"]})
                        self.assertEqual(status, 403, (route, body))
                        self.assertEqual(body["error"]["code"], "forbidden",
                                         (route, body))
                        self.assertEqual(self._writes(fx), before, route)
                ran.append(label)
            finally:
                self._close(label, store)
        self._assert_ran(ran, "BATCH EFFECTIVE TEAM / AWAY EXPLICIT HOME")

    def test_the_home_coach_still_builds_their_own_roster(self):
        """The fix must not cost the ordinary case its one-click flow: the
        HOME coach's empty body still seats HOME, and only HOME."""
        ran = []
        for label, store in self._stores():
            try:
                self._assert_backend(label, store)
                for route in self.ROUTES:
                    store.clear_all_data()
                    fx, home_pid, away_pid = self._fixture(store, route)
                    home = self._login(fx, "coachhome")
                    with self.subTest(backend=label, route=route):
                        status, body = self._req(
                            home, "POST", f"/api/games/{fx['gid']}/{route}", {})
                        self.assertEqual(status, 200, (route, body))
                        self.assertEqual(body["team_id"], fx["home"],
                                         (route, body))
                        self.assertEqual(body["seated"], [home_pid],
                                         (route, body))
                        self.assertEqual([r[0] for r in self._rows(fx)],
                                         [home_pid], route)
                ran.append(label)
            finally:
                self._close(label, store)
        self._assert_ran(ran, "BATCH EFFECTIVE TEAM / HOME {}")

    def test_an_unscoped_league_admin_keeps_the_home_default(self):
        """"Preserve the existing HOME default only for unscoped roles where
        that behavior is already accepted." A League Admin is not
        resource-scoped, carries no team, and still gets #25's HOME default
        from an empty body — byte-for-byte the pre-existing behaviour."""
        ran = []
        for label, store in self._stores():
            try:
                self._assert_backend(label, store)
                for route in self.ROUTES:
                    store.clear_all_data()
                    fx, home_pid, away_pid = self._fixture(store, route)
                    admin = self._login(fx, "admin")
                    with self.subTest(backend=label, route=route):
                        status, body = self._req(
                            admin, "POST",
                            f"/api/games/{fx['gid']}/{route}", {})
                        self.assertEqual(status, 200, (route, body))
                        self.assertEqual(body["team_id"], fx["home"],
                                         (route, body))
                        self.assertEqual(body["seated"], [home_pid],
                                         (route, body))
                ran.append(label)
            finally:
                self._close(label, store)
        self._assert_ran(ran, "BATCH EFFECTIVE TEAM / ADMIN HOME DEFAULT")

    def test_an_unscoped_league_admin_may_still_name_either_side(self):
        ran = []
        for label, store in self._stores():
            try:
                self._assert_backend(label, store)
                store.clear_all_data()
                fx, home_pid, away_pid = self._fixture(store, "build-roster")
                admin = self._login(fx, "admin")
                with self.subTest(backend=label):
                    status, body = self._req(
                        admin, "POST", f"/api/games/{fx['gid']}/build-roster",
                        {"team_id": fx["away"]})
                    self.assertEqual(status, 200, body)
                    self.assertEqual(body["team_id"], fx["away"], body)
                    self.assertEqual(body["seated"], [away_pid], body)
                ran.append(label)
            finally:
                self._close(label, store)
        self._assert_ran(ran, "BATCH EFFECTIVE TEAM / ADMIN EXPLICIT")


class OneBatchSeatsOneSide(_BatchTeamHarness, unittest.TestCase):
    """A candidate whose CURRENT context resolves onto the OPPOSITE side of
    this same game is a REPORTED SKIP, never a silent cross-side seat.

    Discovery is deliberately not spine-derived, so this shape is reachable
    from both pools; it is a fact about the cohort, not a bad request, so it
    skips-with-a-reason rather than raising."""

    def _cross_side(self, fx, name="Xan CrossSide"):
        """Permanent pointer on HOME — which is how auto-fill's pointer half
        discovers them as a HOME candidate — but an ACTIVE membership on
        AWAY, so the live context names the other side of this same game."""
        api = fx["api"]
        p = Player(id=api.store.next_id("player"), team_id=fx["home"],
                   name=name, position=Position.FORWARD)
        api.store.add_player(p)
        m = api.create_season_roster_membership(
            p.id, fx["ls_id"], fx["away"], status="active", actor_id=ADMIN)
        self.assertNotIn("error", m, m)
        return p.id

    def test_an_opposite_side_candidate_is_skipped_not_seated(self):
        ran = []
        for label, store in self._stores():
            try:
                self._assert_backend(label, store)
                store.clear_all_data()
                fx = self._build(store)
                xan = self._cross_side(fx)
                with self.subTest(backend=label):
                    res = fx["api"].auto_build_roster(fx["gid"], fx["home"],
                                                      ADMIN)
                    self.assertNotIn("error", res, res)
                    self.assertEqual(res["team_id"], fx["home"], res)
                    self.assertEqual(res["seated"], [], res)
                    self.assertEqual(
                        [(r["player_id"], r["reason"]) for r in res["skipped"]],
                        [(xan, spine.MEMBERSHIP_OTHER_SIDE)], res)
                    # And nothing was written on EITHER side.
                    self.assertEqual(self._rows(fx), [], label)
                ran.append(label)
            finally:
                self._close(label, store)
        self._assert_ran(ran, "ONE BATCH ONE SIDE")


# =========================================================================== #
# the refusal BELOW the preflight (PR #427 review, F-2)                       #
# =========================================================================== #
class AnExplicitForeignTeamIsRefusedBelowThePreflight(_BatchTeamHarness,
                                                      unittest.TestCase):
    """THE COMMIT MESSAGE'S CLAIM, FINALLY ASSERTED WHERE IT LIVES: "an
    explicit foreign team is forbidden, not rewritten".

    WHY THE HTTP TEST ABOVE COULD NOT MAKE THIS CLAIM.
    ``test_away_coach_naming_home_explicitly_is_forbidden_zero_writes`` posts
    ``{"team_id": HOME}`` as the AWAY coach and asserts 403 — but a truthy,
    foreign ``body["team_id"]`` is exactly what ``scope_violation`` refuses at
    the PREFLIGHT, so that 403 is produced before the request reaches the
    service at all. A reviewer replaced ``_batch_team``'s refusal with a
    SILENT REWRITE to the coach's own side and the ENTIRE suite stayed green
    (measured here too: 234 modules, 3 shards, 271s, OK). The preflight is,
    in the ruling's words, exactly what "cannot be the authoritative write
    gate" — so a test that can only ever see the preflight's answer proves
    nothing about the gate underneath it. This is the same shape as the
    earlier blocker where a test routed through ``publish_game`` could never
    reach the line it named.

    SO EVERY CALL BELOW IS A DIRECT FACADE CALL with an explicit
    ``authorized_team_id``, entering at ``ApiService`` and never touching
    ``scope_violation``, ``web/server.py`` or an HTTP socket. That is also
    the honest model of the contract: ``authorized_team_id`` is the parameter
    the ruling requires every Coach-reachable command to revalidate, and a
    service that is correct only when a particular front end happens to
    filter its inputs first is not the contract that was asked for.

    WHAT KILLS EVERY TEST HERE, precisely: replacing

        if authorized_team_id is not None:
            if team_id is not None and team_id != authorized_team_id:
                self._require_authorized_team(...)
            team_id = authorized_team_id

    with the silent rewrite ``team_id = authorized_team_id`` — i.e. deleting
    only the refusal and keeping the resolution. Under that mutation the
    refusal tests receive a successful batch naming the coach's OWN side and
    redden on the missing ``error``; the whole rest of the suite does not
    move.

    ZERO WRITE ATTEMPTS, not a snapshot diff: ``auto_build_roster`` and
    ``copy_previous_roster`` are transactional, so a gate placed after the
    first seat still leaves an empty before/after diff on every backend. The
    spy is ``helpers.write_attempt_spy``, shared with
    ``test_coach_team_authorization`` so both files mean the same thing by
    the phrase.
    """

    def _fixture(self, store, route):
        prior = route == "copy"
        fx = self._build(store, prior=prior)
        home_pid, away_pid = self._both_sides(fx)
        if prior:
            self._seat_prior(fx, [home_pid, away_pid])
        return fx, home_pid, away_pid

    def _batch(self, fx, route, team_id, authorized_team_id):
        api = fx["api"]
        if route == "build":
            return api.auto_build_roster(
                fx["gid"], team_id, "coach",
                authorized_team_id=authorized_team_id)
        return api.copy_previous_roster(
            fx["gid"], team_id, "coach",
            authorized_team_id=authorized_team_id)

    ROUTES = ("build", "copy")

    def test_the_service_refuses_an_explicit_foreign_team(self):
        """The AWAY coach names HOME explicitly, straight at the facade. The
        answer must be a structured ``team_scope_violation`` refusal with
        zero write ATTEMPTS — never a 200 that quietly acted on AWAY."""
        ran = []
        for label, store in self._stores():
            try:
                self._assert_backend(label, store)
                for route in self.ROUTES:
                    store.clear_all_data()
                    fx, home_pid, away_pid = self._fixture(store, route)
                    before = self._writes(fx)
                    with self.subTest(backend=label, route=route):
                        with write_attempt_spy(fx["api"].store) as calls:
                            res = self._batch(fx, route, fx["home"],
                                              fx["away"])
                        self.assertIsInstance(res, dict, res)
                        self.assertIn("error", res, (route, res))
                        err = res["error"]
                        self.assertEqual(err["code"], "forbidden",
                                         (route, res))
                        self.assertEqual(err["details"]["reason"],
                                         TEAM_SCOPE_VIOLATION, (route, res))
                        self.assertEqual(err["details"]["owning_team_id"],
                                         fx["home"], (route, res))
                        # THE SILENT-REWRITE SIGNATURE, ruled out explicitly:
                        # a rewritten request answers with the coach's OWN
                        # side and a seated list. There is no response body
                        # here at all.
                        self.assertNotIn("team_id", res, (route, res))
                        self.assertEqual(calls, [], (route, calls))
                        self.assertEqual(self._writes(fx), before, route)
                ran.append(label)
            finally:
                self._close(label, store)
        self._assert_ran(ran, "BATCH SERVICE / EXPLICIT FOREIGN TEAM")

    def test_naming_their_own_team_explicitly_still_works(self):
        """The refusal is about WHOSE side was named, not about naming one:
        the same explicit form, pointed at the coach's own team, succeeds and
        seats only that side. Without this, deleting ``_batch_team``'s whole
        ``authorized_team_id`` branch would still leave the test above
        passing for the wrong reason."""
        ran = []
        for label, store in self._stores():
            try:
                self._assert_backend(label, store)
                for route in self.ROUTES:
                    store.clear_all_data()
                    fx, home_pid, away_pid = self._fixture(store, route)
                    with self.subTest(backend=label, route=route):
                        res = self._batch(fx, route, fx["away"], fx["away"])
                        self.assertNotIn("error", res, (route, res))
                        self.assertEqual(res["team_id"], fx["away"],
                                         (route, res))
                        self.assertEqual(res["seated"], [away_pid],
                                         (route, res))
                        self.assertEqual(
                            [r for r in self._rows(fx) if r[2] == fx["home"]],
                            [], (route, self._rows(fx)))
                ran.append(label)
            finally:
                self._close(label, store)
        self._assert_ran(ran, "BATCH SERVICE / EXPLICIT OWN TEAM")

    def test_an_omitted_team_is_the_coachs_own_side_at_the_service_too(self):
        """The PINNED omission behaviour, asserted below the preflight as
        well as through it: ``team_id=None`` with a Coach constraint resolves
        to the coach's own side — never HOME by fallback, and never a
        refusal. Restoring ``team_id = team_id or game.home_team_id`` for a
        scoped caller reddens this on the AWAY coach."""
        ran = []
        for label, store in self._stores():
            try:
                self._assert_backend(label, store)
                for route in self.ROUTES:
                    store.clear_all_data()
                    fx, home_pid, away_pid = self._fixture(store, route)
                    with self.subTest(backend=label, route=route):
                        res = self._batch(fx, route, None, fx["away"])
                        self.assertNotIn("error", res, (route, res))
                        self.assertEqual(res["team_id"], fx["away"],
                                         (route, res))
                        self.assertEqual(res["seated"], [away_pid],
                                         (route, res))
                ran.append(label)
            finally:
                self._close(label, store)
        self._assert_ran(ran, "BATCH SERVICE / OMITTED TEAM")

    def test_a_team_not_playing_is_still_a_bad_request_not_a_refusal(self):
        """The third team is neither the coach's nor in this game. An
        unscoped caller naming it must still get the pre-existing
        ``ValidationError`` — the gate must not have swallowed that path —
        while a Coach naming it gets the authorization refusal, because
        "not yours" is decided before "not playing"."""
        ran = []
        for label, store in self._stores():
            try:
                self._assert_backend(label, store)
                for route in self.ROUTES:
                    store.clear_all_data()
                    fx, home_pid, away_pid = self._fixture(store, route)
                    with self.subTest(backend=label, route=route):
                        unscoped = self._batch(fx, route, fx["third"], None)
                        self.assertEqual(
                            unscoped["error"]["code"], "validation_error",
                            (route, unscoped))
                        coached = self._batch(fx, route, fx["third"],
                                              fx["away"])
                        self.assertEqual(coached["error"]["details"]["reason"],
                                         TEAM_SCOPE_VIOLATION,
                                         (route, coached))
                ran.append(label)
            finally:
                self._close(label, store)
        self._assert_ran(ran, "BATCH SERVICE / THIRD TEAM")


if __name__ == "__main__":
    unittest.main()
