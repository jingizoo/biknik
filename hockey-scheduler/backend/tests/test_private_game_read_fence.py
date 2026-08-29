"""#427 round 2, blocker 1 — A SCOPED /board READ WITH NO RESOLVED SIDE MUST
NOT BECOME A FULL HOME READ.

THE DEFECT, in the owner's words::

    "get_board executes team_id = team_id or game.home_team_id before
    projection. On this exact head, a direct Role.COACH call with
    team_id=None returns team_id=team_1, projection=full, restricted=false,
    and HOME player identities. This is reachable at HTTP: the handler
    authorizes a Player, then independently re-fetches the game and resolves
    membership … If membership is transferred, ended, or invalidated after
    can_read_private_game_data passes but before the second resolution,
    own_team='' -> board_team=None -> full HOME fallback. That turns loss of
    authority into disclosure instead of refusal."

REPRODUCED AT HEAD 42d4f4d, in both shapes:

* DIRECT FACADE, Memory / SQLite / real PostgreSQL — ``get_board(gid,
  team_id=None, viewer_role=Role.COACH)`` and the same for ``Role.PLAYER``
  answered ``team_id=team_1, projection=full, restricted=false`` with HOME's
  six identities and a ``status`` block naming HOME.
* REAL AUTHENTICATED HTTP, PARKED — an AWAY-membership Player's
  ``GET /api/games/{id}/board``, parked between the admission gate and the
  dispatch's own second resolution while ``set_player_active(.., False)``
  committed, came back **200** with ``team_id=team_1``, HOME's six
  identities, HOME's ``status`` block, **3 HOME notifications and 4 HOME
  audit rows** under ``audit_scope: own_side``.

THE FIX IS STRUCTURAL, AND IT IS TWO HALVES THAT ONLY WORK TOGETHER.

1. ONE BOUNDARY. ``services.game_side_scope.resolve_private_game_read``
   decides admission AND resolves the trusted side against a SINGLE fetch of
   the game, and ``web/server.py`` carries that one record into every leaf.
   ``can_read_private_game_data`` remains as a fast-denial PREFLIGHT and is
   no longer the authoritative gate — the read-path twin of the rule the
   coach-authorization work established on the write path — and it is now
   literally ``resolve_private_game_read(...).admitted``, so the two cannot
   answer differently.
2. THE HOME DEFAULT IS AUDIENCE-BOUND.
   ``lineup_visibility.default_side_permitted`` allows it only for an
   audience with no side of its own BY DESIGN (unscoped operator, assigned
   official, in-process caller). A COACH or PLAYER with a missing or
   nonparticipant side is RESTRICTED, and the response does not even name
   the side it declined to answer for.

WHAT IS PROVEN WHERE, honestly labelled:

* the facade fence and the parked HTTP read: Memory, SQLite and real
  PostgreSQL;
* BOTH COMMIT ORDERS of the parked read on TWO-CONNECTION PostgreSQL, where
  the writer really is a separate backend;
* Memory and SQLite run the same two orders for PARITY, with the mutation on
  the same store the reader uses. A TRUE IN-TRANSACTION INTERLEAVING IS
  POSTGRESQL-ONLY and is not claimed here: ``InMemoryStore`` holds a
  process-wide ``RLock`` for a whole transaction and ``SqlStore(":memory:")``
  is a private database per handle, so on neither backend can a second
  connection exist to race. What the parity runs do prove is that the same
  ORDERING produces the same refusal on those backends.

DETERMINISM COMES FROM A LATCH, NOT A SLEEP. The reader signals when it has
reached the park point and blocks; only then does the writer commit; only
when the commit has RETURNED is the reader released. No assertion here
depends on a timeout elapsing.

MOVER-SHAPED. The parked reader is a Player whose seasonal membership names
AWAY while their permanent pointer names HOME, so the HOME payload the defect
produced is exactly what a pointer-based answer would also produce — and the
refusal cannot be satisfied by the pointer accidentally agreeing.
"""

import json
import os
import threading
import unittest

from helpers import (BACKEND, end_membership_directly,  # noqa: F401
                     fresh_sql_store)
from test_lineup_side_projection import _ProjectionHarness
from test_substitute_membership_cutover import ADMIN

from hockey_scheduler.domain import Role
from hockey_scheduler.services import lineup_visibility
from hockey_scheduler.services.game_side_scope import (
    GameAuthorization, PrivateGameRead, game_scoped_own_team_id,
    resolve_private_game_read)
from hockey_scheduler.store import SqlStore
from hockey_scheduler.web import server as srv

#: Fields a refused/restricted board must NOT carry. Named individually
#: because the reproduced defect carried every one of them.
WITHHELD = ("status", "players", "notifications", "audit")


class _FenceHarness(_ProjectionHarness):
    """``_ProjectionHarness``'s socket, real sessions and Mover fixture, plus
    the park point and the two falsifiers."""

    # -- the park point ---------------------------------------------------
    def _park(self, at, victim_player_id):
        """Wrap the dispatch's fast-denial preflight so a test can stop the
        request EXACTLY at one of the two points a concurrent write can land
        relative to it.

        ``at="before_admission"``  the write commits before the caller is
            admitted at all — so the preflight itself must refuse.
        ``at="after_admission"``   the write commits after the preflight said
            yes and before the AUTHORITATIVE resolution runs. This is the gap
            the defect lived in: pre-fix, the dispatch's own second
            resolution found no side and the read fell through to HOME.

        Returns ``(reached, release)`` events. Only the victim's own session
        is parked, so signing in and every other principal's traffic is
        unaffected."""
        reached, release = threading.Event(), threading.Event()
        real = srv.can_read_private_game_data

        def parked(role, scope, game_id, store):
            mine = bool(scope) and scope.get("player_id") == victim_player_id
            if mine and at == "before_admission":
                reached.set()
                assert release.wait(30), "the writer never released the read"
            out = real(role, scope, game_id, store)
            if mine and at == "after_admission":
                reached.set()
                assert release.wait(30), "the writer never released the read"
            return out

        srv.can_read_private_game_data = parked
        self.addCleanup(setattr, srv, "can_read_private_game_data", real)
        return reached, release

    # -- the falsifier: the two-independent-resolutions shape --------------
    def _falsified(self):
        """Put BOTH halves of the pre-fix shape back into the LIVE code.

        They are one fix and only fail together, so a falsifier that reverted
        one half would prove nothing about the other:

        * the dispatch resolves the side INDEPENDENTLY of admission (which is
          taken to have already succeeded — that is what the preflight-as-gate
          meant), and
        * the HOME default applies to every audience.

        Returns a callable that restores both."""
        real_resolve = srv.resolve_private_game_read
        real_default = lineup_visibility.default_side_permitted

        def two_independent_resolutions(role, scope, game_id, store):
            game = store.get_game(game_id)
            scope = scope or {}
            own = (game_scoped_own_team_id(
                       role, scope.get("team_id"), scope.get("player_id"),
                       game, store)
                   if game is not None else None)
            # THE RECORD CARRIES A FROZEN SNAPSHOT SINCE #427 round 23, so
            # the pre-fix shape is restored through the snapshot factory —
            # what this falsifier is about is the two INDEPENDENT
            # resolutions, not what the record holds.
            return PrivateGameRead(
                role=role, authorization=GameAuthorization.of(game),
                own_team=own, admitted=True)

        srv.resolve_private_game_read = two_independent_resolutions
        lineup_visibility.default_side_permitted = lambda role: True

        def restore():
            srv.resolve_private_game_read = real_resolve
            lineup_visibility.default_side_permitted = real_default
        self.addCleanup(restore)
        return restore

    # -- readers ----------------------------------------------------------
    def _home_identities(self, fx):
        return sorted(row["id"] for row in self._rows(fx, fx["home"]))

    def _assert_no_home_data(self, status, body, fx, label):
        """A refusal carries NOTHING of HOME — not an identity, not a status,
        not a notification, not an audit row. Asserted field by field AND
        over the raw serialized text, because the reproduced leak lived in
        two collections underneath a correct-looking envelope."""
        self.assertIn(status, (403,), f"[{label}] expected a refusal: {body}")
        blob = json.dumps(body, sort_keys=True, default=str)
        for pid in self._home_identities(fx):
            self.assertNotIn(
                pid, blob, f"[{label}] a refused read carried HOME's {pid}")
        for field in WITHHELD:
            self.assertIsNone(
                body.get(field),
                f"[{label}] a refused read carried `{field}`: {body}")
        self.assertIsNone(body.get("team_id"),
                          f"[{label}] a refused read NAMED a side: {body}")


# ---------------------------------------------------------------------------
# 1. THE FACADE FENCE: a team-scoped caller with no resolved side, and one
#    with a side that is not in this game.
# ---------------------------------------------------------------------------
class AScopedCallerWithNoSideIsRestrictedNeverDefaulted(_FenceHarness,
                                                        unittest.TestCase):
    """"apply the HOME default only for an explicitly unscoped
    operator/internal audience; a Coach/Player with a missing or
    nonparticipant trusted side must be restricted/refused, and the admission
    plus projection decision must share the canonical read/fence boundary."

    The direct-facade half, on both team-scoped roles, for BOTH shapes the
    ruling names — ``team_id=None`` and a FOREIGN side — and the audiences
    that legitimately keep the default are pinned in the same test, so
    tightening the fence cannot quietly narrow an operator."""

    def test_a_missing_or_foreign_side_never_becomes_a_home_read(self):
        ran = []
        for label, store in self._stores():
            try:
                self._assert_backend(label, store)
                store.clear_all_data()
                fx = self._fixture(store)
                api = fx["api"]
                home_ids = self._home_identities(fx)
                self.assertTrue(home_ids, "fixture: HOME has no identities")

                with self.subTest(backend=label):
                    for role in (Role.COACH, Role.PLAYER):
                        for case, side in (("missing", None),
                                           ("foreign", fx["third"])):
                            board = api.get_board(fx["gid"], team_id=side,
                                                  viewer_role=role)
                            tag = f"{label}/{role.value}/{case}"
                            self.assertTrue(
                                board["restricted"],
                                f"[{tag}] not marked restricted: {board}")
                            self.assertEqual(
                                board["projection"],
                                lineup_visibility.RESTRICTED, board)
                            self.assertIsNone(
                                board["team_id"],
                                f"[{tag}] a restricted board NAMED a side, "
                                f"which is what the HOME default did: "
                                f"{board}")
                            for field in WITHHELD:
                                self.assertIsNone(board[field],
                                                  f"[{tag}] {field}: {board}")
                            blob = json.dumps(board, sort_keys=True,
                                              default=str)
                            for pid in home_ids:
                                self.assertNotIn(
                                    pid, blob,
                                    f"[{tag}] carried HOME's {pid}")

                    # THE AUDIENCES THE DEFAULT IS FOR are unchanged — an
                    # unscoped operator, an assigned official, and an
                    # in-process caller with no role at all.
                    for role in (Role.LEAGUE_ADMIN, Role.ARENA_MANAGER,
                                 Role.OFFICIAL, None):
                        board = api.get_board(fx["gid"], team_id=None,
                                              viewer_role=role)
                        tag = f"{label}/{role}"
                        self.assertEqual(
                            board["team_id"], fx["home"],
                            f"[{tag}] lost the home default: {board}")
                        self.assertFalse(board["restricted"], board)
                        self.assertIsNotNone(board["players"], board)
                    # …and the official's default side is still the SUBMITTED
                    # projection, so keeping the default widens nothing.
                    official = api.get_board(fx["gid"], team_id=None,
                                             viewer_role=Role.OFFICIAL)
                    self.assertEqual(official["projection"],
                                     lineup_visibility.SUBMITTED_LINEUP,
                                     official)
                ran.append((label, "facade_fence"))
            finally:
                self._close(label, store)
        self._assert_matrix_ran(ran, ["facade_fence"])


# ---------------------------------------------------------------------------
# 1b. THE TWO THINGS THE FENCE MUST NOT REGRESS (owner ruling, this round).
# ---------------------------------------------------------------------------
class TheAdmissionBoundaryKeepsTheRuledPointerBehaviour(_FenceHarness,
                                                        unittest.TestCase):
    """Collapsing admission and resolution into ONE boundary re-decides WHO
    is admitted, so the two pointer rulings this round explicitly does NOT
    change are asserted here rather than assumed.

    * "Bound games must continue to grant nothing from a stale pointer." A
      Player whose PERMANENT pointer names one of a BOUND game's two sides,
      but whose seasonal membership does not, is refused — the pointer buys
      nothing.
    * The permanent-pointer behaviour for an EXPLICITLY UNBOUND exhibition
      matches the accepted legacy path and is unchanged: there the pointer is
      the only authority there is, so the same Player IS admitted, and to
      their pointer's side.

    Both run through the real admission boundary
    (``resolve_private_game_read``) rather than through the resolver alone,
    because it is the boundary this round moved."""

    def test_a_stale_pointer_grants_nothing_on_a_bound_game(self):
        ran = []
        for label, store in self._stores():
            try:
                self._assert_backend(label, store)
                store.clear_all_data()
                fx = self._fixture(store)
                api = fx["api"]
                # `departed`: pointer HOME, membership moved to THIRD. On
                # neither of this BOUND game's sides.
                stale = fx["people"]["departed"]["id"]
                self.assertEqual(api.store.get_player(stale).team_id,
                                 fx["home"],
                                 "fixture: this player's pointer does not "
                                 "name a side of the bound game, so nothing "
                                 "below could falsify pointer admission")
                scope = {"player_id": stale}
                with self.subTest(backend=label):
                    read = resolve_private_game_read(
                        Role.PLAYER, scope, fx["gid"], api.store)
                    self.assertFalse(
                        read.admitted,
                        f"[{label}] a BOUND game admitted a caller on their "
                        f"stale permanent pointer")
                    self.assertIsNone(read.own_team, read)
                    # …and the read the pointer would have bought is not
                    # served either.
                    board = api.get_board(fx["gid"], team_id=read.own_team,
                                          viewer_role=Role.PLAYER)
                    self.assertTrue(board["restricted"], board)
                    self.assertIsNone(board["players"], board)
                ran.append((label, "stale_pointer_bound"))
            finally:
                self._close(label, store)
        self._assert_matrix_ran(ran, ["stale_pointer_bound"])

    def test_an_unbound_exhibition_still_resolves_through_the_pointer(self):
        ran = []
        for label, store in self._stores():
            try:
                self._assert_backend(label, store)
                store.clear_all_data()
                fx = self._fixture(store)
                api = fx["api"]
                exhibition = self._exhibition(fx)
                self.assertIsNone(exhibition["league_season_id"], exhibition)
                # The SAME player the bound game refuses: pointer HOME.
                stale = fx["people"]["departed"]["id"]
                with self.subTest(backend=label):
                    read = resolve_private_game_read(
                        Role.PLAYER, {"player_id": stale}, exhibition["id"],
                        api.store)
                    self.assertTrue(
                        read.admitted,
                        f"[{label}] the accepted unbound-exhibition pointer "
                        f"path regressed: the caller was refused")
                    self.assertEqual(
                        read.own_team, fx["home"],
                        f"[{label}] the exhibition resolved a side other "
                        f"than the permanent pointer's")
                ran.append((label, "unbound_exhibition_pointer"))
            finally:
                self._close(label, store)
        self._assert_matrix_ran(ran, ["unbound_exhibition_pointer"])


# ---------------------------------------------------------------------------
# 2. THE PARKED READ — the shape that makes this a disclosure rather than an
#    API misuse. Real session, real socket, both commit orders, both ways an
#    authority can be lost.
# ---------------------------------------------------------------------------
class _ParkedRead(_FenceHarness):
    """The body the tri-store parity case and the two-connection PostgreSQL
    case both run."""

    #: ``order -> what it means``. Both are required by the ruling, and they
    #: are NOT the same claim — see
    #: :meth:`test_the_falsified_shape_discloses_home_in_the_admission_gap`
    #: for why only the second one is a disclosure.
    ORDERS = {
        "before_admission":
            "the authority is lost BEFORE the caller is admitted at all",
        "after_admission":
            "the authority is lost after the preflight says yes and before "
            "the authoritative resolution — the gap the defect lived in",
    }

    #: ``mutation -> how the caller's authority ends``. The ruling names
    #: "transferred, ended, or invalidated"; these are the two shapes the
    #: product can actually produce for a Player.
    MUTATIONS = ("membership_transferred", "player_deactivated")

    def _mutation(self, fx, api, kind):
        """The write that ends the parked reader's authority, as a callable.

        ``membership_transferred`` ends the seasonal membership this game
        resolves through, so ``RosterService.team_for_game`` stops naming a
        side. ``player_deactivated`` deactivates the Player, which
        ``_player_team_for_game`` refuses on (#270). Both are real losses of
        authority; neither touches the game or the other side."""
        victim = fx["people"]["awayside"]

        def transferred():
            end_membership_directly(api.store, victim["membership_id"],
                                    status="transferred")

        def deactivated():
            out = api.set_player_active(victim["id"], False, actor_id=ADMIN)
            assert "error" not in out, out

        return {"membership_transferred": transferred,
                "player_deactivated": deactivated}[kind]

    def _run(self, fx, who, order, mutate, label):
        """Park the victim's ``/board`` read at ``order``, run ``mutate()`` to
        completion, release, and return the response.

        A LATCH, NOT A SLEEP: the reader signals when it has reached the park
        point and blocks; the writer only then commits; the reader is only
        released once that commit has RETURNED. Nothing here depends on a
        timeout elapsing."""
        victim = fx["people"]["awayside"]["id"]
        reached, release = self._park(order, victim)
        got = {}

        def reader():
            try:
                got["response"] = self._req(
                    who["awayplayer"], "GET",
                    f"/api/games/{fx['gid']}/board")
            except BaseException as exc:       # surfaced by the asserts
                got["error"] = repr(exc)

        thread = threading.Thread(target=reader, daemon=True)
        thread.start()
        self.assertTrue(reached.wait(30),
                        f"[{label}] the read never reached the park point")
        mutate()
        release.set()
        thread.join(timeout=30)
        self.assertNotIn("error", got, got)
        self.assertIn("response", got,
                      f"[{label}] the parked read never returned")
        return got["response"]

    def _world(self, store):
        """A FRESH fixture and fresh sessions.

        Rebuilt per case rather than reused: ``set_player_active(.., False)``
        REVOKES that account's sessions (#270 — a deactivated player's login
        must not outlive their roster exit), so a world that has hosted one
        deactivation case cannot host another. Measured, not assumed: the
        second read in a reused world came back 401 with the gate never
        reached at all."""
        store.clear_all_data()
        fx = self._fixture(store)
        return fx, self._serve(fx)

    def _assert_mover_premise(self, fx):
        """The parked reader is a MOVER whose PERMANENT pointer names HOME,
        so the HOME payload the defect produced is exactly what a
        pointer-based read would produce — and a passing refusal cannot be
        the pointer accidentally agreeing."""
        api = fx["api"]
        victim = fx["people"]["awayside"]["id"]
        self.assertEqual(api.store.get_player(victim).team_id, fx["home"])
        self.assertEqual(
            api.roster.team_for_game(api.store.get_game(fx["gid"]),
                                     api.store.get_player(victim)),
            fx["away"])


class TheParkedReadRefusesOnEveryBackend(_ParkedRead, unittest.TestCase):
    """BOTH ORDERS x BOTH WAYS AN AUTHORITY ENDS, tri-store.

    Memory and SQLite are the PARITY runs — the writer is the same store the
    reader uses, so this is a deterministic ORDERING check and not a
    two-connection race. PostgreSQL runs the same matrix here AND again, for
    real, in the class below."""

    def test_losing_authority_refuses_and_never_falls_back_to_home(self):
        ran = []
        for label, store in self._stores():
            try:
                self._assert_backend(label, store)
                with self.subTest(backend=label):
                    for order in sorted(self.ORDERS):
                        for kind in self.MUTATIONS:
                            tag = f"{label}/{order}/{kind}"
                            fx, who = self._world(store)
                            self._assert_mover_premise(fx)
                            status, body = self._run(
                                fx, who, order,
                                self._mutation(fx, fx["api"], kind), tag)
                            self._assert_no_home_data(status, body, fx, tag)
                            ran.append((label, f"{order}/{kind}"))
            finally:
                self._close(label, store)
        self._assert_matrix_ran(
            ran, [f"{order}/{kind}" for order in sorted(self.ORDERS)
                  for kind in self.MUTATIONS])

    def test_the_falsified_shape_discloses_home_in_the_admission_gap(self):
        """THE FALSIFIER, and the honest difference between the two orders.

        With the pre-fix shape restored — the dispatch resolving the side
        INDEPENDENTLY of admission, and the HOME default applying to every
        audience — the ``after_admission`` order returns **200 with HOME's
        board**, which is the reproduction. The ``before_admission`` order
        refuses even then, because the PREFLIGHT itself sees the committed
        change: which is exactly why the preflight alone was never the
        boundary, and why only the second order is a disclosure."""
        for label, store in self._stores():
            try:
                self._assert_backend(label, store)
                fx, who = self._world(store)
                mutate = self._mutation(fx, fx["api"], "membership_transferred")
                restore = self._falsified()
                try:
                    status, body = self._run(fx, who, "after_admission",
                                             mutate, f"{label}/falsified")
                    self.assertEqual(
                        status, 200,
                        f"[{label}] the falsified shape did not reproduce the "
                        f"defect, so the assertions above do not pin it: "
                        f"{body}")
                    self.assertEqual(
                        body["team_id"], fx["home"],
                        f"[{label}] the falsified shape did not fall back to "
                        f"HOME: {body}")
                    self.assertFalse(body["restricted"], body)
                    self.assertEqual(
                        sorted(row["id"] for row in body["players"]),
                        self._home_identities(fx), body)
                    # …and it carried the rest of HOME's private board too,
                    # which is what made this a disclosure and not a label
                    # error.
                    self.assertEqual(body["status"]["team_id"], fx["home"],
                                     body["status"])
                    self.assertTrue(body["notifications"], body)
                    self.assertTrue(body["audit"], body)
                finally:
                    restore()
                # …and the SAME park point, unfalsified, refuses.
                fx, who = self._world(store)
                status, body = self._run(
                    fx, who, "after_admission",
                    self._mutation(fx, fx["api"], "membership_transferred"),
                    f"{label}/fixed")
                self._assert_no_home_data(status, body, fx, f"{label}/fixed")
            finally:
                self._close(label, store)
            return   # the falsifier is a property of the code, not a backend


# ---------------------------------------------------------------------------
# 3. TWO-CONNECTION POSTGRESQL — where the writer really is a second backend.
# ---------------------------------------------------------------------------
_PG_SKIP = ("PostgreSQL not configured (TEST_DATABASE_URL); the "
            "two-connection parked read is NOT covered")


class TheParkedReadRefusesUnderARealTwoConnectionRace(_ParkedRead,
                                                      unittest.TestCase):
    """The reader is the HTTP handler on the server's own connection; the
    writer is a SEPARATE ``SqlStore`` on the same DSN — a different backend
    pid, asserted. Both commit orders, both mutations.

    PostgreSQL only, and honestly so: see this module's docstring for why
    Memory and SQLite cannot host a second connection at all."""

    def setUp(self):
        url = os.environ.get("TEST_DATABASE_URL")
        if not url:
            print(f"\n[PRIVATE-GAME READ FENCE] {_PG_SKIP}")
            self.skipTest(_PG_SKIP)
        self.url = url

    def _backend_pid(self, store):
        with store.conn.cursor() as cur:
            cur.execute("SELECT pg_backend_pid() AS pid")
            return cur.fetchone()["pid"]

    def test_the_dsn_is_real_and_a_broken_one_errors_loudly(self):
        """PROVE POSTGRESQL GENUINELY EXECUTES. ``skipUnless`` on an env var
        proves a URL was SET, never that a statement reached a server — so
        the live DSN is asserted to open a real ``postgres`` backend with a
        real backend pid, and a SABOTAGED DSN is required to RAISE rather
        than degrade into anything that would let this class pass without
        PostgreSQL."""
        store = fresh_sql_store(self.url)
        try:
            self.assertIsInstance(store, SqlStore)
            self.assertEqual(store.backend, "postgres", store.backend)
            self.assertIsInstance(self._backend_pid(store), int)
        finally:
            store.reset_schema()
            store.close()
        head, _, db = self.url.rpartition("/")
        sabotaged = f"{head}/{db}_sabotage_this_database_does_not_exist"
        with self.assertRaises(Exception) as caught:
            SqlStore(sabotaged).close()
        self.assertNotIsInstance(
            caught.exception, unittest.SkipTest,
            "a broken DSN was turned into a SKIP; a skip is not a pass")

    def test_both_commit_orders_refuse_with_no_home_data(self):
        for order in sorted(self.ORDERS):
            for kind in self.MUTATIONS:
                with self.subTest(order=order, mutation=kind,
                                  why=self.ORDERS[order]):
                    self._one_race(order, kind)

    def _one_race(self, order, kind):
        store = fresh_sql_store(self.url)
        writer = None
        try:
            self.assertEqual(store.backend, "postgres", store.backend)
            fx, who = self._world(store)
            self._assert_mover_premise(fx)
            victim = fx["people"]["awayside"]["id"]

            # THE SECOND CONNECTION, proven to be a different backend.
            writer = SqlStore(self.url)
            from hockey_scheduler.api import ApiService
            writer_api = ApiService(writer)
            self.assertNotEqual(
                self._backend_pid(writer), self._backend_pid(store),
                "the writer shares the reader's backend, so this is not a "
                "two-connection race")

            tag = f"postgres/{order}/{kind}"
            status, body = self._run(
                fx, who, order, self._mutation(fx, writer_api, kind), tag)
            self._assert_no_home_data(status, body, fx, tag)

            # The writer's commit really is durable and really was seen —
            # read it back on a THIRD connection, so the refusal above cannot
            # be an artifact of the reader's own snapshot.
            check = SqlStore(self.url)
            try:
                if kind == "player_deactivated":
                    self.assertFalse(check.get_player(victim).is_active,
                                     "the deactivation did not commit")
                else:
                    membership = check.get_season_roster_membership(
                        fx["people"]["awayside"]["membership_id"])
                    self.assertEqual(membership.status.value, "transferred",
                                     "the transfer did not commit")
            finally:
                check.close()
        finally:
            if writer is not None:
                writer.close()
            store.reset_schema()
            store.close()


class ThePlayerDecisionIsTakenAgainstTheSnapshot(_FenceHarness,
                                                unittest.TestCase):
    """PLAYER admission decides against the FROZEN five-field snapshot, not
    the mutable ``Game`` row (#427 round 24, owner blocker).

    THE DEFECT THIS CLOSES. ``GameAuthorization`` froze the five facts a
    private read is authorized by, and ``PrivateGameRead`` stopped holding
    the row — but the PLAYER branch still handed the LIVE ``Game`` down
    ``_player_team_for_game`` -> ``RosterService.team_for_game`` ->
    ``resolve_membership_context``, which reads ``league_season_id`` to pick
    the membership that decides the side. So the record was frozen while the
    DECISION was not: a LeagueSeason change committed after capture flipped
    which membership resolved, and admission moved with it while the frozen
    record stayed exactly as it was.

    WHAT THIS ASSERTS, and why it is the whole point: the decision is taken
    against a snapshot, the row is then mutated on the SAME store the
    decision came from, and the decision is re-derived from that snapshot
    and must be UNCHANGED. The falsifier is the shape this replaced —
    resolving against ``store.get_game(...)`` instead — which moves."""

    def test_a_league_season_change_after_capture_cannot_move_admission(self):
        ran = []
        for label, store in self._stores():
            try:
                self._assert_backend(label, store)
                store.clear_all_data()
                fx = self._fixture(store)
                api = fx["api"]
                who = fx["people"]["awayside"]["id"]
                scope = {"player_id": who}

                with self.subTest(backend=label):
                    before = resolve_private_game_read(
                        Role.PLAYER, scope, fx["gid"], api.store)
                    self.assertTrue(
                        before.admitted,
                        f"[{label}] fixture: this player is not admitted "
                        f"before the mutation, so nothing below could show "
                        f"a mutation FAILING to move them")
                    frozen = before.authorization

                    # The row moves to a LeagueSeason the membership does not
                    # name. Committed through the store the decision read.
                    game = api.store.get_game(fx["gid"])
                    game.league_season_id = "leagueseason_not_theirs"
                    api.store.save_game(game)
                    self.assertNotEqual(
                        api.store.get_game(fx["gid"]).league_season_id,
                        frozen.league_season_id,
                        f"[{label}] the mutation did not commit, so this "
                        f"test would pass without proving anything")

                    # THE ASSERTION: the decision re-derived from the frozen
                    # snapshot is the decision that was taken.
                    after = game_scoped_own_team_id(
                        Role.PLAYER, None, who, frozen, api.store)
                    self.assertEqual(
                        after, before.own_team,
                        f"[{label}] the PLAYER side moved when the game's "
                        f"LeagueSeason changed AFTER capture: "
                        f"{before.own_team!r} -> {after!r}. The decision is "
                        f"reading the live row, not the snapshot")

                    # THE FALSIFIER: the shape this replaced. Resolving
                    # against the mutated row must MOVE, or the assertion
                    # above is passing for a reason other than the freeze.
                    moved = game_scoped_own_team_id(
                        Role.PLAYER, None, who,
                        api.store.get_game(fx["gid"]), api.store)
                    self.assertNotEqual(
                        moved, before.own_team,
                        f"[{label}] resolving against the LIVE row did not "
                        f"move either, so this fixture cannot tell a frozen "
                        f"decision from a live one")
                ran.append((label, "snapshot_frozen"))
            finally:
                store.close()
        self._assert_matrix_ran(ran, ["snapshot_frozen"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
