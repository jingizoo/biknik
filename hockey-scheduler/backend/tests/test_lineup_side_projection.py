"""PR #427 blocker, second half (owner ruling comment 5394947899) — WHICH
SIDE of a game's private lineup a caller may read, over real authenticated
HTTP, tri-store.

THE DEFECT THIS FILE PINS, in the owner's own words::

    "`get_board` hard-codes HOME, while the HTTP gate admits an AWAY
    Coach/Player because their team participates in the game. The same gate
    lets `/lineups` return both sides' private candidate, availability, and
    substitute state. 'UI convenience' cannot override the private-data
    boundary."

REPRODUCED RED at head 337374a on Memory, SQLite and real PostgreSQL, over
real authenticated sessions on a real socket:

* an AWAY Coach's ``GET /api/games/{id}/board`` returned 200 with HOME's
  private player pool and a ``status`` block whose ``team_id`` named HOME —
  the AWAY Coach never saw their own side at all;
* an AWAY Coach's ``GET /api/games/{id}/lineups`` returned HOME's full
  private ``players`` array, and a HOME Coach's returned AWAY's.

Both routes were gated by exactly ONE check, ``can_read_private_game_data``,
which proves the caller belongs to *a* team in the game and stops there. The
route registry recorded that honestly (``scope_axis="none"``); nothing
narrowed WHICH side.

THE FIX AND WHAT IS ASSERTED HERE. The SERVER resolves the caller's
game-scoped team (``game_scoped_own_team_id``, hoisted to serve the whole
private-game family so the availability rollup and the lineup reads cannot
drift about who the caller is) and passes that TRUSTED side into the facade,
which projects each side (``services/lineup_visibility.py``):

  UNSCOPED OPERATOR   both sides in full, unchanged.
  COACH / PLAYER      own side in full; opponent RESTRICTED.
  ASSIGNED OFFICIAL   both sides' SUBMITTED LINEUP, and neither side's
                      unselected candidates, availability or substitute
                      state.

RESTRICTED IS NOT AN EMPTY ROSTER, and that is asserted as its own property
rather than assumed. ``players: []`` already renders as "No lineup
submitted." on the Game Sheet and "No players on the roster yet" on the
roster view — a different and misleading operational claim. The redacted
side therefore carries ``restricted: true`` with ``players: null`` and
``status: null``, keeping its public ``team_id``/``team_name``.
``TheRedactedOpponentIsNotAnEmptyRoster`` fails if ``players`` is ever ``[]``.

NEVER A CLIENT-SUPPLIED SIDE. ``AClientSuppliedSideIsIgnored`` sends the
opponent's team id in the query string on both routes and requires the
response to be unchanged.

MOVER-SHAPED. Every player is a MOVER — permanent pointer and seasonal
membership name different teams — so a response that happened to be right
for pointer reasons cannot pass. The Coach's own scope is a TEAM id and the
Player's is a PLAYER id, so nothing in the transport can supply the answer
the service is supposed to discover.

TRI-STORE, PROVEN, over a real socket with real sessions: ``_stores`` yields
Memory, SQLite and — when TEST_DATABASE_URL is set — real PostgreSQL;
``_assert_backend`` PROVES each one and ``_assert_matrix_ran`` fails a
silently narrow loop. A SKIP IS NOT A PASS.
"""

import json
import threading
import unittest
import urllib.error
import urllib.request
from http.cookiejar import CookieJar
from http.server import ThreadingHTTPServer

from helpers import BACKEND  # noqa: F401
from test_lineup_population_authority import (PERM_POSITION, SEASON_POSITION,
                                              _LineupAuthority)
from test_substitute_membership_cutover import ADMIN

from hockey_scheduler.services import lineup_visibility
from hockey_scheduler.web import server as srv
from hockey_scheduler.web.auth import DEMO_PASSWORD, DEMO_USERS

#: Private per-player fields an assigned official must never receive.
OFFICIAL_FORBIDDEN_PLAYER_FIELDS = ("availability", "sub_status", "eligible")


class _ProjectionHarness(_LineupAuthority):
    """One real listening socket, real sessions for five principals, with
    ``srv.STATE.api`` pointed at THIS fixture's ApiService for the duration
    of each backend's case — so every request runs against Memory, SQLite and
    real PostgreSQL in turn rather than against the demo singleton's store.

    Modelled on ``test_availability_membership_authority._CoachHttpHarness``,
    for the same reason it gives: a second setUpClass is a second chance to
    point the server at a store the assertions do not read."""

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

    def _sign_in(self, username):
        opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(CookieJar()))
        status, body = self._req(opener, "POST", "/api/auth/login",
                                 {"username": username,
                                  "password": DEMO_PASSWORD})
        self.assertEqual(status, 200, (username, body))
        return opener

    def _serve(self, fx):
        """Point the running server at this backend's fixture and sign in one
        real session per principal.

        The two Coaches are scoped by TEAM, the two Players by PLAYER id
        (their side is resolved live from the membership, never the pointer),
        the operator is unscoped, and the official is scoped to an Official
        who is actually ASSIGNED to this game — the only shape
        ``can_read_private_game_data`` admits for that role."""
        api = fx["api"]
        p = fx["people"]
        official = api.create_official("Ref Riley", actor_id=ADMIN)
        self.assertNotIn("error", official, official)
        assigned = api.assign_official(fx["gid"], official["id"], "referee",
                                       actor_id=ADMIN)
        self.assertNotIn("error", assigned, assigned)
        accounts = {
            "homecoach": (DEMO_USERS["coach"], {"team_id": fx["home"]}),
            "awaycoach": (DEMO_USERS["coach"], {"team_id": fx["away"]}),
            # A HOME player by MEMBERSHIP whose permanent pointer names THIRD,
            # and an AWAY player by membership whose pointer names HOME.
            "homeplayer": (DEMO_USERS["player"],
                           {"player_id": p["candidate"]["id"]}),
            "awayplayer": (DEMO_USERS["player"],
                           {"player_id": p["awayside"]["id"]}),
            "operator": (DEMO_USERS["admin"], {}),
            "official": (DEMO_USERS["official"],
                         {"official_id": official["id"]}),
        }
        for user, (role, scope) in accounts.items():
            api.accounts.create_account(user, DEMO_PASSWORD, role,
                                        scope=scope, actor_id="test_seed")
        srv.STATE.api = api
        return {user: self._sign_in(user) for user in accounts}

    # -- readers ----------------------------------------------------------
    def _lineups(self, opener, fx, query=""):
        status, body = self._req(
            opener, "GET", f"/api/games/{fx['gid']}/lineups{query}")
        self.assertEqual(status, 200, body)
        return body

    def _board(self, opener, fx, query=""):
        status, body = self._req(
            opener, "GET", f"/api/games/{fx['gid']}/board{query}")
        self.assertEqual(status, 200, body)
        return body

    @staticmethod
    def _ids(side):
        return sorted(pl["id"] for pl in (side["players"] or []))

    def _assert_restricted(self, side, team_id, team_name, label):
        """The redaction contract, in one place.

        Every clause is separate on purpose: a side that merely LOOKS empty
        satisfies none of them, which is the whole point."""
        self.assertTrue(side["restricted"], f"[{label}] not marked restricted")
        self.assertEqual(side["projection"], lineup_visibility.RESTRICTED,
                         f"[{label}] projection label")
        self.assertEqual(side["restricted_reason"], "opponent_private", side)
        self.assertIsNone(
            side["players"],
            f"[{label}] a redacted opponent was represented as a LIST -- an "
            "empty list renders as 'no lineup submitted', a different and "
            "misleading operational claim")
        self.assertIsNone(side["status"],
                          f"[{label}] private status survived redaction")
        # PUBLIC metadata is preserved, so the screen can still name the
        # opponent rather than showing a blank column.
        self.assertEqual(side["team_id"], team_id, side)
        self.assertEqual(side["team_name"], team_name, side)

    def _assert_full(self, side, label):
        self.assertFalse(side["restricted"], f"[{label}] marked restricted")
        self.assertEqual(side["projection"], lineup_visibility.FULL,
                         f"[{label}] projection label")
        self.assertIsInstance(side["players"], list, side)
        self.assertIsNotNone(side["status"], side)


class _Names:
    """Team names as ``_Authority._build`` creates them."""
    HOME = "Home"
    AWAY = "Away"


# ---------------------------------------------------------------------------
# 1. /board answers the CALLER'S OWN side, never a hard-coded HOME.
# ---------------------------------------------------------------------------
class TheBoardAnswersTheCallersOwnSide(_ProjectionHarness, unittest.TestCase):
    """"`get_board` hard-codes HOME, while the HTTP gate admits an AWAY
    Coach/Player because their team participates in the game."

    Both roles, both sides, and the identities AND the status block are
    required to name the SAME own side — the reproduced defect had
    ``status.team_id`` naming HOME to an AWAY Coach, so asserting only the
    player list would have missed half of it."""

    def test_each_scoped_caller_sees_only_their_own_side(self):
        ran = []
        for label, store in self._stores():
            try:
                self._assert_backend(label, store)
                store.clear_all_data()
                fx = self._fixture(store)
                p = fx["people"]
                who = self._serve(fx)
                home_ids = sorted(
                    r["id"] for r in self._rows(fx, fx["home"]))
                away_ids = sorted(
                    r["id"] for r in self._rows(fx, fx["away"]))
                self.assertNotEqual(home_ids, away_ids, "fixture: sides agree")

                with self.subTest(backend=label):
                    for user, team, ids in (
                            ("homecoach", fx["home"], home_ids),
                            ("homeplayer", fx["home"], home_ids),
                            ("awaycoach", fx["away"], away_ids),
                            ("awayplayer", fx["away"], away_ids)):
                        board = self._board(who[user], fx)
                        self.assertEqual(
                            board["team_id"], team,
                            f"[{label}] {user} got another side's board")
                        self.assertEqual(
                            board["status"]["team_id"], team,
                            f"[{label}] {user}'s board STATUS names another "
                            "side")
                        self.assertEqual(
                            sorted(pl["id"] for pl in board["players"]), ids,
                            f"[{label}] {user}'s board player identities")
                    # The AWAY caller must not be able to see the HOME-only
                    # people at all -- named explicitly so a failure says who.
                    away_board = self._board(who["awaycoach"], fx)
                    for key in ("seated", "enrolled", "offered", "candidate"):
                        self.assertNotIn(
                            p[key]["id"],
                            [pl["id"] for pl in away_board["players"]],
                            f"[{label}] AWAY Coach sees HOME's {key}")
                    # An UNSCOPED operator keeps the existing home default.
                    op_board = self._board(who["operator"], fx)
                    self.assertEqual(op_board["team_id"], fx["home"], op_board)
                    self.assertEqual(
                        sorted(pl["id"] for pl in op_board["players"]),
                        home_ids, op_board)
                ran.append((label, "board_own_side"))
            finally:
                self._close(label, store)
        self._assert_matrix_ran(ran, ["board_own_side"])


# ---------------------------------------------------------------------------
# 2. /lineups: own side full, opponent RESTRICTED.
# ---------------------------------------------------------------------------
class TheOpponentSideIsRestrictedForAScopedCaller(_ProjectionHarness,
                                                  unittest.TestCase):
    """"Coach/Player responses expose only their own side... Preserve public
    game/team metadata as needed, but mark a scoped caller's opponent side
    restricted and omit its private `status`/`players`."

    The private state named by the ruling — candidates, availability,
    substitute rows — is asserted absent by CATEGORY, not just by identity, so
    a future field that carries it is caught by the ``players is None``
    clause rather than needing its own assertion."""

    def test_a_scoped_caller_gets_one_side_on_every_backend(self):
        ran = []
        for label, store in self._stores():
            try:
                self._assert_backend(label, store)
                store.clear_all_data()
                fx = self._fixture(store)
                p = fx["people"]
                who = self._serve(fx)
                home_ids = sorted(r["id"] for r in self._rows(fx, fx["home"]))
                away_ids = sorted(r["id"] for r in self._rows(fx, fx["away"]))

                with self.subTest(backend=label):
                    for user in ("homecoach", "homeplayer"):
                        body = self._lineups(who[user], fx)
                        self._assert_full(body["home"], f"{label}/{user}")
                        self.assertEqual(self._ids(body["home"]), home_ids,
                                         f"[{label}] {user} own-side ids")
                        self._assert_restricted(
                            body["away"], fx["away"], _Names.AWAY,
                            f"{label}/{user}")
                    for user in ("awaycoach", "awayplayer"):
                        body = self._lineups(who[user], fx)
                        self._assert_full(body["away"], f"{label}/{user}")
                        self.assertEqual(self._ids(body["away"]), away_ids,
                                         f"[{label}] {user} own-side ids")
                        self._assert_restricted(
                            body["home"], fx["home"], _Names.HOME,
                            f"{label}/{user}")
                        # The specific private facts the ruling names, gone.
                        self.assertNotIn(
                            p["seated"]["id"],
                            json.dumps(body["home"]),
                            f"[{label}] a HOME identity survived redaction")

                    # 3. THE UNSCOPED OPERATOR IS UNCHANGED: both in full.
                    body = self._lineups(who["operator"], fx)
                    self._assert_full(body["home"], f"{label}/operator")
                    self._assert_full(body["away"], f"{label}/operator")
                    self.assertEqual(self._ids(body["home"]), home_ids, body)
                    self.assertEqual(self._ids(body["away"]), away_ids, body)
                ran.append((label, "lineups_scoped"))
            finally:
                self._close(label, store)
        self._assert_matrix_ran(ran, ["lineups_scoped"])


# ---------------------------------------------------------------------------
# 3. RESTRICTED IS NOT AN EMPTY ROSTER.
# ---------------------------------------------------------------------------
class TheRedactedOpponentIsNotAnEmptyRoster(_ProjectionHarness,
                                            unittest.TestCase):
    """"do not represent redaction as an empty roster."

    The distinction is only meaningful if an actually-empty side is
    DISTINGUISHABLE from a redacted one, so this builds both in one response
    and requires them to differ. A game whose AWAY side has no members at all
    reads ``players: []``/``restricted: false`` to an operator; the same side
    reads ``players: null``/``restricted: true`` to the HOME Coach. If a
    future edit collapses redaction onto ``[]``, these two become identical
    and this test fails."""

    def test_an_empty_side_and_a_redacted_side_are_distinguishable(self):
        ran = []
        for label, store in self._stores():
            try:
                self._assert_backend(label, store)
                store.clear_all_data()
                fx = self._fixture(store)
                api, p = fx["api"], fx["people"]
                # Make AWAY genuinely empty: its one member's stint ends.
                from helpers import end_membership_directly
                end_membership_directly(api.store,
                                        p["awayside"]["membership_id"])
                who = self._serve(fx)

                with self.subTest(backend=label):
                    # The GENUINELY EMPTY reading, from a caller allowed to
                    # see it.
                    operator_away = self._lineups(who["operator"], fx)["away"]
                    self.assertEqual(
                        operator_away["players"], [],
                        "fixture: AWAY is not actually empty, so this cannot "
                        "contrast emptiness with redaction")
                    self.assertFalse(operator_away["restricted"],
                                     operator_away)
                    self.assertIsNotNone(operator_away["status"],
                                         operator_away)

                    # The REDACTED reading of the SAME side.
                    coach_away = self._lineups(who["homecoach"], fx)["away"]
                    self._assert_restricted(coach_away, fx["away"],
                                            _Names.AWAY, f"{label}/homecoach")

                    # THE POINT: the two are not the same document.
                    self.assertNotEqual(
                        coach_away["players"], operator_away["players"],
                        f"[{label}] a redacted opponent is indistinguishable "
                        "from a side that genuinely submitted no lineup")
                    self.assertIsNot(
                        coach_away["players"], [],
                        f"[{label}] redaction used an empty list")
                ran.append((label, "not_empty_roster"))
            finally:
                self._close(label, store)
        self._assert_matrix_ran(ran, ["not_empty_roster"])


# ---------------------------------------------------------------------------
# 4. THE ASSIGNED OFFICIAL: the submitted lineup, and nothing else.
# ---------------------------------------------------------------------------
class AnAssignedOfficialGetsTheSubmittedLineupOnly(_ProjectionHarness,
                                                   unittest.TestCase):
    """"An assigned official may retain the two-side submitted-lineup
    projection needed for the Game Sheet, but not either side's unselected
    candidates, availability, or substitute state."

    Four separate claims: both sides present (the Game Sheet needs them),
    ONLY selected rows, NO private per-player workflow fields, and no
    substitute state in the derived ``status`` either — the status block's
    ``substitutes_enrolled`` count, its ``needs_substitute`` state and the
    two open-slot messages that end "Substitutes are available — coach
    decision needed." / "No substitutes enrolled." are all substitute state
    in a field a first pass would have left alone."""

    def test_the_official_sees_selected_rows_and_no_workflow_state(self):
        ran = []
        for label, store in self._stores():
            try:
                self._assert_backend(label, store)
                store.clear_all_data()
                fx = self._fixture(store)
                p = fx["people"]
                who = self._serve(fx)

                with self.subTest(backend=label):
                    body = self._lineups(who["official"], fx)
                    # BOTH sides, because the Game Sheet is a two-side sheet.
                    for key in ("home", "away"):
                        side = body[key]
                        self.assertFalse(side["restricted"], side)
                        self.assertEqual(side["projection"],
                                         lineup_visibility.SUBMITTED_LINEUP,
                                         side)
                        self.assertIsInstance(side["players"], list, side)
                        for row in side["players"]:
                            self.assertEqual(
                                row["group"], "selected",
                                f"[{label}] an official received an "
                                "UNSELECTED row")
                            for field in OFFICIAL_FORBIDDEN_PLAYER_FIELDS:
                                self.assertNotIn(
                                    field, row,
                                    f"[{label}] an official received private "
                                    f"`{field}`")
                        # NO SUBSTITUTE STATE in the status block either.
                        self.assertIsNone(side["status"]["substitutes_enrolled"],
                                          side["status"])
                        self.assertNotEqual(side["status"]["status"],
                                            "needs_substitute", side["status"])
                        self.assertNotIn("ubstitute", side["status"]["message"],
                                         side["status"])

                    home_rows = body["home"]["players"]
                    self.assertEqual([r["id"] for r in home_rows],
                                     [p["seated"]["id"]], home_rows)
                    # The Game Sheet's own fields survive, and they are the
                    # SEASONAL ones.
                    self.assertEqual(home_rows[0]["position"],
                                     SEASON_POSITION.value, home_rows)
                    self.assertNotEqual(home_rows[0]["position"],
                                        PERM_POSITION.value, home_rows)
                    self.assertEqual(home_rows[0]["jersey_number"],
                                     p["seated"]["season_jersey"], home_rows)
                    # The unselected HOME pool is absent by identity too.
                    blob = json.dumps(body)
                    for key in ("candidate", "enrolled", "offered"):
                        self.assertNotIn(
                            p[key]["id"], blob,
                            f"[{label}] an official received HOME's {key}")
                ran.append((label, "official_projection"))
            finally:
                self._close(label, store)
        self._assert_matrix_ran(ran, ["official_projection"])


# ---------------------------------------------------------------------------
# 5. NEVER TRUST A CLIENT-SUPPLIED SIDE.
# ---------------------------------------------------------------------------
class AClientSuppliedSideIsIgnored(_ProjectionHarness, unittest.TestCase):
    """"never trust a client-supplied side."

    The routes take no side parameter, which is exactly the property worth
    pinning: a future edit that adds one for "UI convenience" — the same
    phrase that justified the hard-coded HOME — has to break this."""

    def test_a_team_id_query_parameter_changes_nothing(self):
        ran = []
        for label, store in self._stores():
            try:
                self._assert_backend(label, store)
                store.clear_all_data()
                fx = self._fixture(store)
                who = self._serve(fx)

                with self.subTest(backend=label):
                    for path_reader in (self._board, self._lineups):
                        honest = path_reader(who["homecoach"], fx)
                        for query in (f"?team_id={fx['away']}",
                                      f"?side=away&team_id={fx['away']}",
                                      f"?viewer_team_id={fx['away']}"):
                            spoofed = path_reader(who["homecoach"], fx, query)
                            self.assertEqual(
                                spoofed, honest,
                                f"[{label}] `{query}` changed a scoped "
                                "caller's response, so the side is being "
                                "read from the client")
                ran.append((label, "no_client_side"))
            finally:
                self._close(label, store)
        self._assert_matrix_ran(ran, ["no_client_side"])


# ---------------------------------------------------------------------------
# 6. THE GATE ITSELF IS UNCHANGED.
# ---------------------------------------------------------------------------
class TheExistingRefusalsStillRefuse(_ProjectionHarness, unittest.TestCase):
    """The projection narrows what a 200 may CONTAIN; it must not widen who
    gets one. An unrelated coach, an unassigned official and a plain viewer
    are still 403 on both routes."""

    def test_outsiders_are_still_refused_on_every_backend(self):
        ran = []
        for label, store in self._stores():
            try:
                self._assert_backend(label, store)
                store.clear_all_data()
                fx = self._fixture(store)
                api = fx["api"]
                who = self._serve(fx)
                # A coach of THIRD -- a real team, not in this game.
                api.accounts.create_account(
                    "thirdcoach", DEMO_PASSWORD, DEMO_USERS["coach"],
                    scope={"team_id": fx["third"]}, actor_id="test_seed")
                # An official who exists but is assigned to nothing.
                other = api.create_official("Unassigned Uma", actor_id=ADMIN)
                api.accounts.create_account(
                    "loneofficial", DEMO_PASSWORD, DEMO_USERS["official"],
                    scope={"official_id": other["id"]}, actor_id="test_seed")
                api.accounts.create_account(
                    "viewer1", DEMO_PASSWORD, DEMO_USERS["viewer"], scope={},
                    actor_id="test_seed")

                with self.subTest(backend=label):
                    for user in ("thirdcoach", "loneofficial", "viewer1"):
                        opener = self._sign_in(user)
                        for sub in ("board", "lineups"):
                            status, body = self._req(
                                opener, "GET",
                                f"/api/games/{fx['gid']}/{sub}")
                            self.assertEqual(
                                status, 403,
                                f"[{label}] {user} read /{sub}: {body}")
                            self.assertEqual(body["error"]["code"],
                                             "forbidden", body)
                    self.assertIsNotNone(who)
                ran.append((label, "refusals"))
            finally:
                self._close(label, store)
        self._assert_matrix_ran(ran, ["refusals"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
