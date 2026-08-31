"""#205, ROUND 4 — the private-state leak OUTSIDE the ``/api/games/{id}/…``
family: ``GET /api/demo/overview`` → ``schedule[].roster_status``.

THE DEFECT, and it is the owner's shape verbatim. The Dashboard read's
schedule loop called::

    rstatus = self.roster.compute_roster_status(g.id)      # no team_id

and ``RosterService.compute_roster_status`` then applies
``team_id = team_id or game.home_team_id`` — THE IDENTICAL SILENT HOME
DEFAULT this blocker spent three rounds removing from ``get_board``,
``/roster-status`` and ``list_substitute_candidates``. There was no
participation gate, no side narrowing, no audience test and no ``APP_MODE``
gate on the route: the "demo" in the path is historical, and
``web/static/app.js`` fetches it unconditionally on every render pass for
every role.

REPRODUCED RED at 56aa5dd on Memory, SQLite and real PostgreSQL, over real
authenticated sessions on a real socket, with the two sides deliberately made
to DIFFER (HOME ``needs_substitute`` / AWAY ``open_slot``) so that "it
returned HOME's value" is falsifiable rather than a coincidence::

  homecoach   overview needs_substitute   /roster-status 200 HOME needs_substitute
  awaycoach   overview needs_substitute   /roster-status 200 AWAY open_slot
  awayplayer  overview needs_substitute   /roster-status 200 AWAY open_slot
  official    overview needs_substitute   /roster-status 403
  guardian    overview needs_substitute   /roster-status 403
  thirdcoach  overview needs_substitute   /roster-status 403  (team not in the game)

Every one of the last five received HOME's private per-side operational
state. The third-team coach makes the point sharpest: this route was a WIDER
gate than the family it sits beside — a coach refused on all seven family
leaves still received it.

WHAT IS PINNED HERE, one class per clause of the owner's rules:

* the side is resolved PER ROW, from the server's trusted resolution — this
  is a CROSS-GAME list, so a Coach who is HOME in one game and AWAY in the
  next must get their own side in BOTH (``TheSideIsResolvedPerRow``);
* a caller entitled to NO side in a game receives the field OMITTED with an
  explicit marker — never a guessed value, never ``null`` standing in for an
  enum, and never an empty operational state (``AWithheldRowIsOmitted…``);
* an ASSIGNED OFFICIAL is withheld, and cannot recover here what
  ``_submitted_lineup_status`` neutralises one route away
  (``AnOfficialCannotRecoverSubstituteState``);
* an UNSCOPED OPERATOR is byte-for-byte unchanged, proven against the
  facade's own un-scoped read rather than a hand-written expectation.

MOVER-SHAPED: every player carries a permanent pointer and a seasonal
membership naming DIFFERENT teams, so a row that happened to be right for
pointer reasons cannot pass.

TRI-STORE, PROVEN: ``_stores`` yields Memory, SQLite and — when
TEST_DATABASE_URL is set — real PostgreSQL; ``_assert_backend`` PROVES the
backend rather than trusting the env var, and ``_assert_matrix_ran`` fails a
silently narrow loop. A SKIP IS NOT A PASS.

FALSIFIED: :meth:`_OverviewHarness._falsified_overview` restores the
un-narrowed read into the LIVE code and ``_require_overview_falsifier_breaks``
fails BY NAME if the assertions still pass without the fix.
"""

import ast
import contextlib
import inspect
import textwrap
import unittest

from helpers import BACKEND  # noqa: F401
from test_private_game_sibling_routes import _SiblingHarness
from test_substitute_membership_cutover import ADMIN, _at

from hockey_scheduler.api.service import ApiService as _ApiService
from hockey_scheduler.services import lineup_visibility
from hockey_scheduler.web.auth import DEMO_PASSWORD, DEMO_USERS

#: The six principals the reproduction covers, plus the operator control.
#: ``thirdcoach`` and ``guardian`` exist ONLY here: they are the two callers
#: the family refuses outright, and they are what proves this route was a
#: WIDER gate than the family rather than an equally-narrow one.
PRINCIPALS = ("homecoach", "homeplayer", "awaycoach", "awayplayer",
              "official", "guardian", "thirdcoach", "operator")


class _OverviewHarness(_SiblingHarness):
    """``_SiblingHarness``'s socket, sessions and Mover fixture, with

    * the two sides made to answer DIFFERENT roster statuses, so "HOME's
      value" is distinguishable from "my own side's value";
    * a SECOND game in which the two coaches' sides are SWAPPED, so a
      per-response side would be caught where a per-row one passes;
    * two further principals — a coach of the THIRD team (in no game at all)
      and a guardian linked to a HOME junior — both of which the family
      refuses on every leaf;
    * an explicit active Program/Season/League for every session, because
      this read is context-scoped and an unresolved context returns an EMPTY
      schedule, which would let every assertion below pass vacuously.
    """

    def _fixture(self, store):
        fx = super()._fixture(store)
        api, p = fx["api"], fx["people"]
        # MAKE THE SIDES DIFFER. The shared fixture leaves both sides with
        # enrolled substitutes and open slots, so both compute
        # `needs_substitute` and "HOME's value" would be indistinguishable
        # from "my own". Withdrawing AWAY's enrollments leaves AWAY with open
        # slots and NO substitutes -> `open_slot`, while HOME keeps its
        # enrolled/offered pool -> `needs_substitute`.
        for key in ("away_sub", "away_legacy_sub"):
            out = api.withdraw_substitute(fx["gid"], p[key]["id"],
                                          actor_id=ADMIN)
            assert "error" not in out, out
        # THE PREMISE every assertion rests on. If these were equal, a
        # response carrying the opponent's status would be indistinguishable
        # from one carrying the caller's own.
        assert self._side_status(fx, fx["home"]) == "needs_substitute", \
            self._side_status(fx, fx["home"])
        assert self._side_status(fx, fx["away"]) == "open_slot", \
            self._side_status(fx, fx["away"])

        # THE SWAPPED SECOND GAME: the same two teams with the sides
        # REVERSED, and each team answering a DIFFERENT status than it does
        # in the first game. Both properties are needed and they catch
        # different bugs:
        #
        #   * reversed sides catch "silently HOME" — a Coach who is HOME in
        #     row 1 is AWAY in row 2, so a home default is right once and
        #     wrong once;
        #   * differing statuses catch "resolved ONCE for the whole
        #     response" — a side resolved against row 1 and reused would
        #     still name the caller's own team in row 2 (a team id does not
        #     change between rows), and only the VALUE can expose it.
        slot = api.create_ice_slot(fx["rink"]["id"], _at(21).isoformat(),
                                   _at(22).isoformat(), "game",
                                   actor_id=ADMIN)
        g2 = api.create_game(fx["s1"]["id"], None, fx["away"], fx["home"],
                             slot["id"], target_goalies=0, target_skaters=1,
                             actor_id=ADMIN, league_id=fx["league"]["id"])
        assert "error" not in g2, g2
        api.publish_game(g2["id"], actor_id=ADMIN)
        # AWAY the team fills its single slot here (-> awaiting/confirmed),
        # HOME the team seats nobody (-> draft). Neither matches that team's
        # status in the first game.
        assert "error" not in api.select_roster(
            g2["id"], [p["awayside"]["id"]], actor_id=ADMIN)
        fx["gid2"] = g2["id"]
        # THE PREMISES, asserted rather than assumed — every one of them is
        # what makes some assertion below able to fail.
        assert (api.store.get_game(fx["gid"]).home_team_id
                != api.store.get_game(fx["gid2"]).home_team_id), \
            "fixture: the two games have the same home side"
        for team in (fx["home"], fx["away"]):
            assert (self._side_status(fx, team, fx["gid"])
                    != self._side_status(fx, team, fx["gid2"])), \
                (f"fixture: {team} answers the same status in both games, so "
                 "a side resolved once for the whole response would pass")
        return fx

    def _side_status(self, fx, team_id, game_id=None):
        return fx["api"].roster.compute_roster_status(
            game_id or fx["gid"], team_id).status.value

    def _serve(self, fx):
        who = super()._serve(fx)
        api = fx["api"]
        extra = {
            # A Coach of the THIRD team, which plays in NEITHER game. The
            # family answers this account 403 on every one of its seven
            # leaves; this route answered it HOME's status.
            "thirdcoach": (DEMO_USERS["coach"], {"team_id": fx["third"]}),
            # A guardian with a VERIFIED link to a HOME junior — the shape
            # that resolves a real active context, so their schedule is not
            # empty. A guardian holds no team/player scope of their own.
            "guardian": (DEMO_USERS["guardian"], {}),
        }
        for user, (role, scope) in extra.items():
            acct = api.accounts.create_account(
                user, DEMO_PASSWORD, role, scope=scope, actor_id="test_seed")
            if user == "guardian":
                link = api.create_guardian_link(
                    acct.id, fx["people"]["seated"]["id"], actor_id=ADMIN)
                assert "error" not in link, link
                out = api.verify_guardian_link(link["id"], "signed_form",
                                               actor_id=ADMIN)
                assert "error" not in out, out
            who[user] = self._sign_in(user)
        # EVERY session selects the Program/Season/League explicitly. This
        # read fails CLOSED to an empty payload when no Program resolves, and
        # an empty `schedule` would satisfy every assertion below vacuously —
        # `_rows` asserts non-emptiness for exactly that reason.
        for user, opener in who.items():
            status, body = self._req(opener, "POST", "/api/context", {
                "program_id": fx["program"]["id"],
                "season_id": fx["s1"]["id"],
                "league_id": fx["league"]["id"]})
            self.assertEqual(status, 200, (user, body))
        return who

    # -- readers -----------------------------------------------------------
    def _rows(self, opener, fx, label):
        """``{game_id: row}`` from a real ``GET /api/demo/overview``.

        Asserts BOTH games are present: a narrowed or empty schedule would
        make every per-row assertion below pass by having nothing to check."""
        status, body = self._req(opener, "GET", "/api/demo/overview")
        self.assertEqual(status, 200, (label, body))
        rows = {g["game_id"]: g for g in body.get("schedule", [])}
        self.assertEqual(
            set(rows), {fx["gid"], fx["gid2"]},
            f"[{label}] the schedule did not carry both games, so nothing "
            "below is actually being asserted")
        return rows

    def _assert_withheld(self, row, label):
        """The withholding contract, in one place. Every clause separate on
        purpose: a row that merely LOOKS empty satisfies none of them."""
        self.assertNotIn(
            "roster_status", row,
            f"[{label}] the field was present for a caller entitled to no "
            "side of this game")
        self.assertTrue(row["roster_status_restricted"],
                        f"[{label}] not marked restricted")
        self.assertIsNone(row["roster_status_team_id"], row)

    def _assert_entitled(self, row, value, team_id, label):
        self.assertFalse(row["roster_status_restricted"],
                         f"[{label}] marked restricted")
        self.assertEqual(row["roster_status"], value,
                         f"[{label}] wrong side's roster status")
        self.assertEqual(row["roster_status_team_id"], team_id, row)

    # -- the executable falsifiers -----------------------------------------
    @contextlib.contextmanager
    def _falsified_overview(self, kind):
        """Reintroduce ONE narrowing's ABSENCE into the LIVE code."""
        api = _ApiService
        # Patches BEYOND the single `_schedule_roster_status` one, for the
        # falsifiers that need two places restored at once — see the owner's
        # two variants below.
        extra = []
        if kind == "home_default_for_everyone":
            # THE DEFECT AS MEASURED: the unscoped read, for every caller.
            def status(self, game, role, scoped_team_id, scoped_player_id):
                return {"roster_status": self.roster.compute_roster_status(
                            game.id).status.value,
                        "roster_status_restricted": False,
                        "roster_status_team_id": game.home_team_id}
            target, attr, patch = api, "_schedule_roster_status", status
        elif kind == "withheld_as_empty_state":
            # THE RULING'S NAMED FAILURE MODE: refuse by emitting the key with
            # a null/absent VALUE, which every consumer reads as "not
            # confirmed" — restricted data as an empty operational state.
            real = api._schedule_roster_status

            def status(self, game, role, scoped_team_id, scoped_player_id):
                out = real(self, game, role, scoped_team_id, scoped_player_id)
                if out.get("roster_status_restricted"):
                    out["roster_status"] = None
                return out
            target, attr, patch = api, "_schedule_roster_status", status
        elif kind == "official_served":
            # The OFFICIAL half alone, with the rest of the narrowing intact,
            # so a test that only drives the Coaches cannot cover for it.
            real = api._schedule_roster_status

            def status(self, game, role, scoped_team_id, scoped_player_id):
                from hockey_scheduler.domain import Role
                if role == Role.OFFICIAL:
                    return {
                        "roster_status": self.roster.compute_roster_status(
                            game.id).status.value,
                        "roster_status_restricted": False,
                        "roster_status_team_id": game.home_team_id}
                return real(self, game, role, scoped_team_id, scoped_player_id)
            target, attr, patch = api, "_schedule_roster_status", status
        elif kind == "one_side_for_the_whole_response":
            # THE PER-ROW PROPERTY, alone: the caller's side resolved ONCE
            # against the FIRST row and reused for every row after it. Every
            # single-game assertion still passes under this; only the swapped
            # second game catches it.
            real = api._schedule_roster_status

            def status(self, game, role, scoped_team_id, scoped_player_id):
                first = getattr(self, "_falsify_first_game", None)
                if first is None:
                    self._falsify_first_game = game
                    first = game
                return real(self, first, role, scoped_team_id, scoped_player_id)
            target, attr, patch = api, "_schedule_roster_status", status
        elif kind == "operator_narrowed":
            # THE OTHER DIRECTION, and its own falsifier because narrowing the
            # UNSCOPED OPERATOR is its own regression rather than a milder
            # version of the same fix.
            real = api._schedule_roster_status

            def status(self, game, role, scoped_team_id, scoped_player_id):
                out = real(self, game, role, scoped_team_id, scoped_player_id)
                from hockey_scheduler.domain import Role
                if role in (Role.LEAGUE_ADMIN, Role.ARENA_MANAGER):
                    return dict(api._ROSTER_STATUS_WITHHELD)
                return out
            target, attr, patch = api, "_schedule_roster_status", status
        elif kind in ("owner_variant_mutated_mapping",
                      "owner_variant_second_mapping"):
            # THE OWNER'S TWO ROUND-23 VARIANTS, restored into the LIVE code
            # (#427). Both are statements INSIDE `_schedule_roster_status`,
            # and both need the one thing the round-23 signature took away:
            # the session mapping reaching this method at all. So the
            # falsifier is two patches, and the first restores EXACTLY that
            # and nothing else — a wrapper that stashes the `scope` the route
            # handed `get_demo_overview`, which is what the old
            # `_schedule_roster_status(self, game, role, scope)` received once
            # per schedule row. The second is the variant itself.
            #
            # WHY THIS IS THE HONEST RESTORATION rather than scaffolding: the
            # ONLY difference between the old signature and the new one is
            # whether the mapping crosses into this method. Give it the
            # mapping back and both variants are ordinary statements; take it
            # away and neither can be written at all, which is what the fix
            # is.
            real_overview = api.get_demo_overview
            real = api._schedule_roster_status

            def overview(self, user_id=None, role=None, scope=None):
                self._falsify_scope = scope
                return real_overview(self, user_id, role, scope)

            def status(self, game, role, scoped_team_id, scoped_player_id):
                from hockey_scheduler.services.game_side_scope import (
                    game_scoped_own_team_id)
                from hockey_scheduler.services import lineup_visibility as lv
                scope = getattr(self, "_falsify_scope", None) or {}
                if kind == "owner_variant_mutated_mapping":
                    # VARIANT ONE: mutate it, then project it. The `.get`
                    # below is character-for-character the projection the
                    # withdrawn allowance called "taken AT the call site".
                    scope.update({"team_id": game.home_team_id})
                else:
                    # VARIANT TWO: project the right keys off the WRONG
                    # mapping.
                    scope = {"team_id": game.home_team_id,
                             "player_id": scope.get("player_id")}
                own_side = game_scoped_own_team_id(
                    role, scope.get("team_id"), scope.get("player_id"), game,
                    self.store)
                audience = lv.route_audience(role, own_side, game.home_team_id,
                                             game.away_team_id)
                if audience == lv.FULL:
                    side = game.home_team_id
                elif audience == lv.OWN_SIDE:
                    side = own_side
                else:
                    return dict(api._ROSTER_STATUS_WITHHELD)
                return {"roster_status": self.roster.compute_roster_status(
                            game.id, side).status.value,
                        "roster_status_restricted": False,
                        "roster_status_team_id": side}
            target, attr, patch = api, "_schedule_roster_status", status
            extra = [(api, "get_demo_overview", overview)]
        else:  # pragma: no cover - a typo in a falsifier name must be loud
            raise AssertionError(f"unknown falsifier {kind!r}")
        applied = [(target, attr, target.__dict__[attr])] + [
            (t, a, t.__dict__[a]) for t, a, _ in extra]
        setattr(target, attr, patch)
        for t, a, replacement in extra:
            setattr(t, a, replacement)
        try:
            yield
        finally:
            for t, a, original in applied:
                setattr(t, a, original)

    def _require_overview_falsifier_breaks(self, kind, body, label):
        with self._falsified_overview(kind):
            try:
                body()
            except AssertionError:
                return
            finally:
                _ApiService._falsify_first_game = None
        self.fail(
            f"FALSIFIER '{kind}' did not break {label}: the assertions still "
            "passed with the un-narrowed read restored, so they do not "
            "actually pin this narrowing.")


# ---------------------------------------------------------------------------
# 1. THE SIDE IS RESOLVED PER ROW, FROM THE SERVER'S TRUSTED RESOLUTION.
# ---------------------------------------------------------------------------
class TheSideIsResolvedPerRow(_OverviewHarness, unittest.TestCase):
    """A Coach who is HOME in one game and AWAY in the next gets THEIR OWN
    side in both — which is why this cannot be one side for the response.

    Driven as both Coaches AND both Players, because the two resolve their
    side by DIFFERENT authorities (a Coach's permanently-bound
    ``scope["team_id"]``, a Player's live game-scoped membership) and a fix
    that only worked for one would be invisible if only the other were
    driven."""

    def test_each_scoped_caller_gets_their_own_sides_status_in_every_row(self):
        ran = []
        for label, store in self._stores():
            try:
                self._assert_backend(label, store)
                store.clear_all_data()
                fx = self._fixture(store)
                who = self._serve(fx)

                def check():
                    for user, team in (("homecoach", fx["home"]),
                                       ("homeplayer", fx["home"]),
                                       ("awaycoach", fx["away"]),
                                       ("awayplayer", fx["away"])):
                        rows = self._rows(who[user], fx, f"{label}/{user}")
                        for gid in (fx["gid"], fx["gid2"]):
                            self._assert_entitled(
                                rows[gid],
                                self._side_status(fx, team, gid), team,
                                f"{label}/{user}/{gid}")

                with self.subTest(backend=label):
                    check()
                    # THE PREMISE, restated as an assertion: in the FIRST
                    # game the two sides genuinely disagree, so "own side"
                    # and "HOME" are distinguishable answers.
                    self.assertNotEqual(
                        self._side_status(fx, fx["home"]),
                        self._side_status(fx, fx["away"]),
                        "fixture: the two sides answer the same status, so "
                        "no assertion here could tell them apart")
                    # And the two ROWS disagree for one caller, so "one side
                    # for the whole response" is distinguishable too. It is
                    # the VALUE that exposes it, never the team id — a
                    # Coach's team is the same in every row by definition.
                    home_rows = self._rows(who["homecoach"], fx,
                                           f"{label}/premise")
                    self.assertNotEqual(
                        home_rows[fx["gid"]]["roster_status"],
                        home_rows[fx["gid2"]]["roster_status"],
                        "fixture: this Coach's own side answers the same "
                        "status in both games, so a side resolved once for "
                        "the whole response would pass")
                    # And this Coach really is on OPPOSITE sides of the two
                    # games, so a silent home default is right in one row and
                    # wrong in the other.
                    self.assertNotEqual(
                        fx["api"].store.get_game(fx["gid"]).home_team_id,
                        fx["api"].store.get_game(fx["gid2"]).home_team_id)
                self._require_overview_falsifier_breaks(
                    "home_default_for_everyone", check,
                    f"[{label}] own-side schedule status")
                self._require_overview_falsifier_breaks(
                    "one_side_for_the_whole_response", check,
                    f"[{label}] PER-ROW resolution")
                ran.append((label, "overview_own_side"))
            finally:
                self._close(label, store)
        self._assert_matrix_ran(ran, ["overview_own_side"])


# ---------------------------------------------------------------------------
# 2. A CALLER ENTITLED TO NO SIDE GETS THE FIELD OMITTED, NOT GUESSED.
# ---------------------------------------------------------------------------
class AWithheldRowIsOmittedNotGuessed(_OverviewHarness, unittest.TestCase):
    """"restricted data is OMITTED or explicitly FORBIDDEN, never represented
    as an empty operational state — and by extension never as a GUESSED
    value."

    The THIRD-TEAM COACH is the sharpest case and is driven by name: their
    team plays in neither game, the private-game family answers them 403 on
    all seven leaves, and this route handed them HOME's status."""

    def test_a_caller_with_no_side_receives_no_status(self):
        ran = []
        for label, store in self._stores():
            try:
                self._assert_backend(label, store)
                store.clear_all_data()
                fx = self._fixture(store)
                who = self._serve(fx)
                home_value = self._side_status(fx, fx["home"])
                away_value = self._side_status(fx, fx["away"])

                def check():
                    for user in ("thirdcoach", "guardian", "official"):
                        rows = self._rows(who[user], fx, f"{label}/{user}")
                        for gid in (fx["gid"], fx["gid2"]):
                            row = rows[gid]
                            self._assert_withheld(row, f"{label}/{user}/{gid}")
                            # Neither side's value is present ANYWHERE in the
                            # row, under any key — the omission is of the
                            # FACT, not of one spelling of it.
                            for value in (home_value, away_value):
                                self.assertNotIn(
                                    value, row.values(),
                                    f"[{label}/{user}] a private per-side "
                                    f"status ({value}) survived in the row "
                                    "under another key")

                with self.subTest(backend=label):
                    check()
                    # The third-team coach IS refused by the family, so this
                    # route being wider was a real widening, not a difference
                    # of opinion between two equally-narrow gates.
                    self._forbidden(who["thirdcoach"], fx, "roster-status",
                                    f"{label}/thirdcoach")
                self._require_overview_falsifier_breaks(
                    "home_default_for_everyone", check,
                    f"[{label}] withheld rows")
                self._require_overview_falsifier_breaks(
                    "withheld_as_empty_state", check,
                    f"[{label}] omission rather than an empty state")
                ran.append((label, "overview_withheld"))
            finally:
                self._close(label, store)
        self._assert_matrix_ran(ran, ["overview_withheld"])


# ---------------------------------------------------------------------------
# 3. AN ASSIGNED OFFICIAL CANNOT RECOVER HERE WHAT THE PROJECTION REMOVES.
# ---------------------------------------------------------------------------
class AnOfficialCannotRecoverSubstituteState(_OverviewHarness,
                                             unittest.TestCase):
    """``_submitted_lineup_status`` exists to neutralise exactly three things
    for an assigned official, and its own docstring names the second: "the
    ``needs_substitute`` state, which exists precisely to say a substitute
    pool is standing by".

    At 56aa5dd the same official on the same game got ``/board`` → status
    ``open_slot`` with ``substitutes_enrolled: null``, ``/roster-status`` →
    403, and overview → ``needs_substitute``. Two surfaces contradicted each
    other and the untouched one leaked. This pins the three answers into
    agreement."""

    def test_the_official_sees_no_substitute_bearing_state_on_any_surface(self):
        ran = []
        for label, store in self._stores():
            try:
                self._assert_backend(label, store)
                store.clear_all_data()
                fx = self._fixture(store)
                who = self._serve(fx)
                # THE PREMISE: the real HOME status IS the one the projection
                # neutralises, so this test is about that exact state.
                self.assertEqual(self._side_status(fx, fx["home"]),
                                 "needs_substitute")

                def check():
                    rows = self._rows(who["official"], fx, f"{label}/official")
                    self._assert_withheld(rows[fx["gid"]],
                                          f"{label}/official")
                    # THE SIBLING SURFACES, asserted together: the board's
                    # official projection still neutralises, /roster-status
                    # still refuses, and overview now withholds. No pair of
                    # them contradicts.
                    board = self._board(who["official"], fx)
                    self.assertEqual(board["status"]["status"], "open_slot",
                                     board["status"])
                    self.assertIsNone(board["status"]["substitutes_enrolled"],
                                      board["status"])
                    self.assertNotIn("needs_substitute",
                                     str(rows[fx["gid"]]),
                                     "the neutralised state reappeared in "
                                     "the schedule row")

                with self.subTest(backend=label):
                    check()
                    self._forbidden(who["official"], fx, "roster-status",
                                    f"{label}/official")
                self._require_overview_falsifier_breaks(
                    "official_served", check,
                    f"[{label}] the official's withheld schedule status")
                ran.append((label, "overview_official"))
            finally:
                self._close(label, store)
        self._assert_matrix_ran(ran, ["overview_official"])


# ---------------------------------------------------------------------------
# 4. AN UNSCOPED OPERATOR IS UNCHANGED.
# ---------------------------------------------------------------------------
class TheUnscopedOperatorIsUnchanged(_OverviewHarness, unittest.TestCase):
    """"Unscoped operators retain full access."

    Proven against the facade's OWN un-scoped read rather than a
    hand-written expectation, so this cannot drift with the fixture."""

    def test_the_operator_still_receives_the_home_default(self):
        ran = []
        for label, store in self._stores():
            try:
                self._assert_backend(label, store)
                store.clear_all_data()
                fx = self._fixture(store)
                who = self._serve(fx)

                def check():
                    rows = self._rows(who["operator"], fx, f"{label}/operator")
                    for gid in (fx["gid"], fx["gid2"]):
                        game = fx["api"].store.get_game(gid)
                        self._assert_entitled(
                            rows[gid],
                            # The facade's own un-scoped answer — the home
                            # default, byte-for-byte what this route has
                            # always returned to an operator.
                            fx["api"].roster.compute_roster_status(
                                gid).status.value,
                            game.home_team_id, f"{label}/operator/{gid}")

                with self.subTest(backend=label):
                    check()
                    # And the audience really is FULL for them, so this is
                    # the operator branch and not a coincidence of ids.
                    game = fx["api"].store.get_game(fx["gid"])
                    from hockey_scheduler.domain import Role
                    self.assertEqual(
                        lineup_visibility.route_audience(
                            Role.LEAGUE_ADMIN, None, game.home_team_id,
                            game.away_team_id),
                        lineup_visibility.FULL)
                self._require_overview_falsifier_breaks(
                    "operator_narrowed", check,
                    f"[{label}] the unscoped operator's unchanged read")
                ran.append((label, "overview_operator"))
            finally:
                self._close(label, store)
        self._assert_matrix_ran(ran, ["overview_operator"])


# ---------------------------------------------------------------------------
# 5. THE TRUSTED RESOLUTION IS ONE FUNCTION, NOT TWO.
# ---------------------------------------------------------------------------
class TheTrustedResolutionIsShared(_OverviewHarness, unittest.TestCase):
    """The Dashboard's per-row side and the private-game family's hoisted
    ``own_team`` come from the SAME function object.

    Four rounds of this blocker were spent deleting second answers to "which
    team does this caller act for". The resolution MOVED to
    ``services/game_side_scope.py`` so the facade could import it without
    depending on ``web/``; this asserts it did not get COPIED there instead
    — the failure mode a reader cannot see by looking at either file alone.
    """

    def test_web_scope_and_the_facade_share_one_definition(self):
        from hockey_scheduler.api import service as facade
        from hockey_scheduler.services import game_side_scope
        from hockey_scheduler.web import scope as web_scope
        self.assertIs(web_scope.game_scoped_own_team_id,
                      game_side_scope.game_scoped_own_team_id)
        self.assertIs(facade.game_scoped_own_team_id,
                      game_side_scope.game_scoped_own_team_id)


if __name__ == "__main__":
    unittest.main()


# ---------------------------------------------------------------------------
# 5. THE FACADE PROJECTS BEFORE ANYTHING ELSE RUNS (#427 round 23, the
#    owner's ruling), AND THE TWO VARIANTS THAT SAY WHY.
#
# THE DEFECT. `services/game_side_scope.py` stopped taking the session
# mapping in round 20 — projected once, above every role branch, into
# immutable ids. `ApiService._schedule_roster_status`, the Dashboard's
# per-row entitlement decision ONE FILE AWAY, went on taking `scope` whole
# and projecting it itself, once per schedule row, with some seventy calls
# between the mapping arriving in `get_demo_overview` and that projection
# reading it. BOTH static audits stayed green: neither reads that function.
# That is the owner's first bullet exactly — static green never closes a
# blocker — and the two variants below are what it cost.
# ---------------------------------------------------------------------------
class TheFacadeProjectsBeforeAnyOtherCall(_OverviewHarness, unittest.TestCase):
    """`team_id` and `player_id` are projected at the FIRST EXECUTABLE LINES
    of ``get_demo_overview``, and ``_schedule_roster_status`` accepts
    SCALARS ONLY.

    THREE THINGS ARE ASSERTED AND THEY ARE NOT THE SAME THING:

    * the SIGNATURE — no mapping crosses into the per-row decision, read off
      the running function rather than off a docstring;
    * the POSITION — the projection is the first two statements of the
      method, before any call, so nothing the method does can sit between
      the session's value and the decision;
    * the BEHAVIOUR — a Coach of a third team, playing in NEITHER game, is
      withheld on every row, tri-store over real authenticated HTTP, with
      the owner's TWO VARIANTS restored into the live code as executable
      falsifiers.

    THE VARIANTS ARE THE POINT OF THE POSITION ASSERTION. Neither can be
    written against the current signature — there is no mapping in that
    method to mutate or to shadow — so the falsifier restores the mapping
    (see :meth:`_OverviewHarness._falsified_overview`) and each then hands
    ``thirdcoach`` HOME's private roster status on every row."""

    #: The two ids the ruling names, in the order the projection takes them.
    PROJECTED = ("scoped_team_id", "scoped_player_id")

    def test_the_per_row_decision_accepts_scalars_and_not_a_mapping(self):
        """THE SIGNATURE, read off the running function."""
        params = list(inspect.signature(
            _ApiService._schedule_roster_status).parameters)
        self.assertEqual(
            ["self", "game", "role", "scoped_team_id", "scoped_player_id"],
            params,
            "the per-row entitlement decision's signature has moved. It "
            "takes SCALARS -- a mapping here is the round-23 bypass, and "
            "both of the owner's variants are statements that need one")

    def test_the_projection_is_the_first_two_statements_of_the_read(self):
        """THE POSITION — "before any other call", derived from the source.

        A projection taken LATER is a projection with calls in front of it,
        and "which of those calls mutates a shared mapping" is not a
        question a source tree answers. So the requirement is positional and
        it is checked positionally: the first two executable statements of
        ``get_demo_overview`` are the two projections, each reading its own
        key off the session mapping, and NO call precedes them."""
        tree = ast.parse(textwrap.dedent(
            inspect.getsource(_ApiService.get_demo_overview)))
        body = tree.body[0].body
        self.assertIsInstance(body[0], ast.Expr, "expected the docstring")
        statements = body[1:3]
        for statement, name in zip(statements, self.PROJECTED):
            self.assertIsInstance(statement, ast.Assign)
            self.assertEqual([name], [t.id for t in statement.targets])
            key = name[len("scoped_"):]
            call = statement.value
            self.assertTrue(
                isinstance(call, ast.Call)
                and isinstance(call.func, ast.Attribute)
                and call.func.attr == "get"
                and [c.value for c in call.args] == [key],
                f"{name} is not a projection of {key!r}: "
                f"{ast.unparse(statement)!r}")
        # …AND NOTHING CALLS ANYTHING BEFORE THEM. The projections' own
        # `.get` reads are the two calls this permits, by identity, so the
        # rule cannot be satisfied by renaming something into a projection.
        permitted = {id(s.value) for s in statements}
        for statement in statements:
            for node in ast.walk(statement):
                if isinstance(node, ast.Call) and id(node) not in permitted:
                    self.fail(f"a call precedes the projection: "
                              f"{ast.unparse(node)!r}")

    def test_a_third_teams_coach_is_withheld_and_both_variants_break_it(self):
        """THE BEHAVIOUR, tri-store, with the owner's two variants as the
        falsifiers.

        ``thirdcoach``'s team plays in NEITHER game and the private-game
        family answers them 403 on every leaf. Under each variant they
        receive HOME's ``needs_substitute`` — HOME's private per-side
        operational state — on every row of their Dashboard, which is
        asserted POSITIVELY here rather than only inferred from the
        falsifier breaking, because "the assertion failed" and "the caller
        got the other side's private state" are two different claims."""
        ran = []
        for label, store in self._stores():
            try:
                self._assert_backend(label, store)
                store.clear_all_data()
                fx = self._fixture(store)
                who = self._serve(fx)
                home_value = self._side_status(fx, fx["home"])

                def check():
                    rows = self._rows(who["thirdcoach"], fx,
                                      f"{label}/thirdcoach")
                    for gid in (fx["gid"], fx["gid2"]):
                        self._assert_withheld(rows[gid],
                                              f"{label}/thirdcoach/{gid}")

                with self.subTest(backend=label):
                    check()
                    # THE VARIANTS, DRIVEN, and what they hand the caller
                    # stated as a positive fact.
                    for kind in ("owner_variant_mutated_mapping",
                                 "owner_variant_second_mapping"):
                        with self.subTest(variant=kind):
                            with self._falsified_overview(kind):
                                rows = self._rows(
                                    who["thirdcoach"], fx,
                                    f"{label}/{kind}")
                                for gid in (fx["gid"], fx["gid2"]):
                                    row = rows[gid]
                                    self.assertFalse(
                                        row["roster_status_restricted"],
                                        f"[{label}/{kind}/{gid}] the variant "
                                        "did not reproduce: still withheld")
                                    self.assertEqual(
                                        fx["api"].store.get_game(
                                            gid).home_team_id,
                                        row["roster_status_team_id"],
                                        f"[{label}/{kind}/{gid}] the variant "
                                        "did not hand this caller HOME")
                                self.assertEqual(
                                    home_value,
                                    rows[fx["gid"]]["roster_status"],
                                    f"[{label}/{kind}] the value served was "
                                    "not HOME's own private status")
                for kind in ("owner_variant_mutated_mapping",
                             "owner_variant_second_mapping"):
                    self._require_overview_falsifier_breaks(
                        kind, check, f"[{label}] {kind}")
                ran.append((label, "overview_facade_projection"))
            finally:
                self._close(label, store)
        self._assert_matrix_ran(ran, ["overview_facade_projection"])
