"""#205, ROUND 6 — the FIFTH leak: ``GET /api/me/player-home`` (and the
guardian surface that inherits it) served a MOVER the OPPONENT's private
per-side state, because the side came from ``Player.team_id``.

THE DEFECT, MEASURED AT b1cc02d over real authenticated sessions on a real
socket, on Memory, SQLite and real PostgreSQL, with the two sides made to
genuinely DIFFER (HOME ``needs_substitute`` / AWAY ``open_slot``)::

    Mover player_6 — permanent pointer team_1 (HOME),
                     seasonal membership AND game_scoped_own_team_id team_2

    GET /api/games/game_1/roster-status  ->  team_id=team_2  status=open_slot
    GET /api/me/player-home              ->  next_game.team_id   = team_1
                                             next_game.team_name = "Home"
                                             next_game.opponent_name = "Away"
                                             next_game.team_status = "sub_search"

``team_status`` is the player-facing rendering of the SAME per-side enum the
private-game family spent five rounds binding to the server's trusted
resolution: ``ApiService._PLAYER_TEAM_STATUS`` maps ``needs_substitute`` ->
``"sub_search"`` and ``open_slot`` -> ``"short"``. So the last line above is
HOME's private operational state, served to a player who is on AWAY —
identical on all three backends, and identical inside
``GET /api/me/guardian/home`` for a guardian verified for that junior, which
calls ``get_player_home`` once per junior.

IT WAS ALSO A CORRECTNESS DEFECT, and the same pointer caused it: the Mover's
own Dashboard named their OPPONENT as their team (``app.js`` renders
``${team_name} vs ${opponent_name}``) and addressed their "I'm In" /
"Can't Play" POST to ``next_game.game_id`` — a game chosen by the same
pointer. The other direction was live too: a Mover whose pointer named a team
in NEITHER game (``player_1``, pointer ``team_3``, membership ``team_1``) got
``next_game: null`` — a real player shown no game at all.

THE RULE THIS FILE PINS. Entitlement is decided by AUTHORITATIVE GAME-SCOPED
MEMBERSHIP, never by the pointer, and a pointer that has OUTLIVED the
membership grants NOTHING:

===========================================  ==============================
pointer / membership for this game            what the page answers
===========================================  ==============================
pointer HOME, membership AWAY                 the AWAY side
pointer THIRD (in neither game), memb. HOME   the HOME side
pointer HOME, membership moved to THIRD       no game, neither side
pointer HOME, membership ENDED                no game, neither side
pointer HOME, no seasonal record at all       no game, neither side
pointer HOME, membership HOME                 the HOME side (unchanged)
unauthorized caller (no player binding)       the empty stub
unscoped operator                             the empty stub (unchanged)
game with NO LeagueSeason binding             the pointer, unchanged
===========================================  ==============================

GAME SELECTION IS PART OF THE RULE, not a separate nicety:
``find_next_game_for_player`` and ``count_games_today_for_player`` picked the
game by the same pointer, and a Mover handed the wrong GAME cannot be rescued
by resolving the side correctly within it. Both now go through
``RosterService._plays_in`` (``team_for_game``), which falls back to the
permanent pointer only for a game with NO LeagueSeason binding — so the
exhibition path is byte-for-byte unchanged and is asserted here to be.

NON-INTERFERENCE is asserted as its own property: perturbing ONLY the
opponent side's private state must not change one byte of a scoped caller's
response. That is the property the whole-surface sweep in
``test_authenticated_side_noninterference.py`` generalises; here it is pinned
directly on the route the sweep found.

TRI-STORE, PROVEN. ``_stores`` yields Memory, SQLite and — when
TEST_DATABASE_URL is set — real PostgreSQL; ``_assert_backend`` PROVES the
backend rather than trusting the env var, and ``_assert_matrix_ran`` fails a
silently narrow loop. A SKIP IS NOT A PASS.

MOVER-SHAPED BY CONSTRUCTION: every player in the shared fixture carries a
permanent pointer and a seasonal membership naming DIFFERENT teams (the two
non-mover shapes here are built deliberately and asserted to agree), so no
assertion below can pass because the pointer happened to be right.

FALSIFIED: :meth:`_PlayerHomeHarness._falsified_player_home` restores the
pointer-based side and the pointer-based game selection into the LIVE code,
and ``_require_player_home_falsifier_breaks`` fails BY NAME if the assertions
still pass without the fix.
"""

import contextlib
import copy
import json
import unittest

from helpers import BACKEND, end_membership_directly  # noqa: F401
from test_lineup_population_authority import PERM_POSITION, SEASON_POSITION
from test_overview_schedule_side import _OverviewHarness
from test_substitute_membership_cutover import ADMIN, _at

from hockey_scheduler.api.service import ApiService as _ApiService
from hockey_scheduler.domain import Player
from hockey_scheduler.services.roster_service import RosterService
from hockey_scheduler.web.auth import DEMO_PASSWORD, DEMO_USERS

#: The empty stub ``web/server.py`` answers a caller with no player binding.
#: Asserted as a WHOLE dict rather than field-by-field, so a future field that
#: carried private state into the unauthorized answer would fail here.
EMPTY_STUB = {"player_id": None, "next_game": None, "today_count": 0,
              "substitute_offers": [], "substitute_opportunities": [],
              "unread_notifications": 0}


class _PlayerHomeHarness(_OverviewHarness):
    """``_OverviewHarness``'s tri-store fixture — two games with the sides
    SWAPPED, the two sides answering DIFFERENT roster statuses, eight real
    authenticated sessions — plus the four (pointer, membership) shapes the
    ruling enumerates and a guardian verified for the MOVER junior.

    WHY THE GUARDIAN IS RE-LINKED. ``_OverviewHarness`` verifies its guardian
    for ``seated``, whose pointer names THIRD — a team in NEITHER game. Under
    the defect that junior's ``next_game`` was ``null``, so a guardian linked
    to them could not express the guardian half of this leak at all. The
    guardian here is verified for the Mover instead, which is the shape that
    reproduced.
    """

    # -- fixture ----------------------------------------------------------
    def _loyal(self, fx, name, team):
        """A NON-mover: permanent pointer and seasonal membership both name
        ``team``. Asserted to AGREE — the mirror of ``_mover``'s assert, and
        the guard that keeps "the ordinary player is unchanged" a real claim
        rather than an accident."""
        type(self)._seq += 1
        n = type(self)._seq
        api = fx["api"]
        p = Player(id=api.store.next_id("player"), team_id=team, name=name,
                   position=PERM_POSITION, jersey_number=10 + n)
        api.store.add_player(p)
        m = api.create_season_roster_membership(
            p.id, fx["ls_id"], team, status="active",
            position=SEASON_POSITION, jersey_number=50 + n, actor_id=ADMIN)
        assert "error" not in m, m
        assert api.store.get_player(p.id).team_id == team, "not a loyal player"
        assert api.roster.team_for_game(
            api.store.get_game(fx["gid"]), api.store.get_player(p.id)) == team
        return {"id": p.id, "name": name, "membership_id": m["id"]}

    def _fixture(self, store):
        fx = super()._fixture(store)
        api, p = fx["api"], fx["people"]
        HOME, AWAY = fx["home"], fx["away"]

        # (d) POINTER IN THE GAME, MEMBERSHIP ENDED. The pointer still names
        # HOME; the seasonal record that ever justified it is released. This
        # is the "wrongly still granted" half of `_player_team_for_game`'s own
        # docstring, in its most literal form.
        p["expired"] = self._mover(fx, "Expired Member", HOME, AWAY)
        end_membership_directly(api.store, p["expired"]["membership_id"])

        # (e) THE ORDINARY PLAYER: pointer and membership agree on HOME. The
        # control that proves this change is a narrowing of WHO gets WHAT,
        # not a narrowing of everybody.
        p["loyal"] = self._loyal(fx, "Loyal Member", HOME)

        # THE PREMISES the whole file rests on, re-asserted after the
        # additions above: the two sides must still answer DIFFERENT statuses,
        # or "the opponent's value" is indistinguishable from "my own".
        assert self._side_status(fx, HOME) == "needs_substitute", \
            self._side_status(fx, HOME)
        assert self._side_status(fx, AWAY) == "open_slot", \
            self._side_status(fx, AWAY)
        return fx

    def _add_exhibition(self, fx):
        """An UNBOUND game — no LeagueSeason — where `team_for_game` falls
        back to the permanent pointer BY DESIGN.

        Added only by the class that asserts the unbound path, because its
        presence would give the three "entitled to nothing" shapes a game
        after all: the pointer IS the authority here, which is the whole
        point. Deliberately LATER than both bound games, so it is the next
        game only for someone the bound games do not select."""
        api = fx["api"]
        slot = api.create_ice_slot(fx["rink"]["id"], _at(22).isoformat(),
                                   _at(23).isoformat(), "game",
                                   actor_id=ADMIN)
        ex = api.create_game(fx["s1"]["id"], None, fx["home"], fx["away"],
                             slot["id"], target_goalies=0, target_skaters=1,
                             actor_id=ADMIN, league_id=fx["league"]["id"],
                             game_type="exhibition")
        assert "error" not in ex, ex
        assert ex["league_season_id"] is None, ex
        assert "error" not in api.publish_game(ex["id"], actor_id=ADMIN)
        fx["exhibition_id"] = ex["id"]
        return fx

    def _serve(self, fx):
        who = super()._serve(fx)
        api = fx["api"]
        # A guardian verified for the MOVER, and — deliberately — an
        # UNVERIFIED link to a second junior, so "only verified links surface"
        # is asserted against a link that really exists rather than against
        # the absence of one.
        acct = api.accounts.create_account(
            "moverguardian", DEMO_PASSWORD, DEMO_USERS["guardian"], scope={},
            actor_id="test_seed")
        link = api.create_guardian_link(acct.id, fx["people"]["awayside"]["id"],
                                        actor_id=ADMIN)
        assert "error" not in link, link
        verified = api.verify_guardian_link(link["id"], "signed_form",
                                            actor_id=ADMIN)
        assert "error" not in verified, verified
        unverified = api.create_guardian_link(
            acct.id, fx["people"]["loyal"]["id"], actor_id=ADMIN)
        assert "error" not in unverified, unverified
        who["moverguardian"] = self._sign_in("moverguardian")
        status, body = self._req(who["moverguardian"], "POST", "/api/context", {
            "program_id": fx["program"]["id"], "season_id": fx["s1"]["id"],
            "league_id": fx["league"]["id"]})
        self.assertEqual(status, 200, body)
        return who

    # -- readers -----------------------------------------------------------
    def _home(self, opener, label):
        """A real ``GET /api/me/player-home`` over the socket."""
        status, body = self._req(opener, "GET", "/api/me/player-home")
        self.assertEqual(status, 200, (label, body))
        return body

    def _home_for(self, fx, player_id):
        """The same payload for a player with no session of their own —
        driven at the FACADE, which is the layer the leak lived in. Used only
        for the (pointer, membership) shapes that do not need to prove the
        HTTP identity rules, which the socket-driven cases above cover."""
        out = fx["api"].get_player_home(player_id)
        self.assertNotIn("error", out, out)
        return out

    def _guardian_home(self, opener, label):
        status, body = self._req(opener, "GET", "/api/me/guardian/home")
        self.assertEqual(status, 200, (label, body))
        return body

    def _assert_next_game(self, payload, team_id, team_name, opponent_name,
                          team_status, label):
        """The WHOLE next-game side contract in one place. Every clause is
        separate on purpose: asserting only ``team_status`` would miss the
        correctness half (the opponent named as the caller's team), and
        asserting only the names would miss the privacy half."""
        ng = payload["next_game"]
        self.assertIsNotNone(ng, f"[{label}] no next game at all")
        self.assertEqual(ng["team_id"], team_id, f"[{label}] team_id")
        self.assertEqual(ng["team_name"], team_name, f"[{label}] team_name")
        self.assertEqual(ng["opponent_name"], opponent_name,
                         f"[{label}] opponent_name")
        self.assertEqual(ng["team_status"], team_status,
                         f"[{label}] team_status — the per-side private enum")

    def _assert_no_private_side_state(self, payload, fx, label):
        """A caller entitled to NEITHER side receives no next game and no
        per-side enum — not a guessed one, not a null standing in for one."""
        self.assertIsNone(
            payload["next_game"],
            f"[{label}] a caller whose membership does not put them in any "
            f"game received a next-game card: {payload['next_game']!r}")
        self.assertEqual(payload["today_count"], 0, label)
        blob = json.dumps(payload, sort_keys=True, default=str)
        for key in ("sub_search", "short", "full"):
            self.assertNotIn(
                f'"{key}"', blob,
                f"[{label}] a per-side operational label reached a caller "
                f"entitled to no side of any game")

    # -- the executable falsifiers -----------------------------------------
    @contextlib.contextmanager
    def _falsified_player_home(self, kind):
        """Reintroduce ONE half of the pointer into the LIVE code.

        Each entry restores exactly the behaviour measured at b1cc02d, so the
        assertions run under it are the assertions that reproduced the leak.
        """
        if kind == "pointer_side":
            # THE DEFECT AS MEASURED: the SIDE from `player.team_id`, with the
            # corrected game selection left in place — so this falsifier is
            # specifically about the side, not about which game was chosen.
            # `catch` is a plain wrapper, so the decorated
            # attribute IS the callable to delegate to.
            real = _ApiService.get_player_home

            def home(self, player_id, user_id=None):
                out = real(self, player_id, user_id=user_id)
                ng = out.get("next_game")
                if ng is None:
                    return out
                player = self.store.get_player(player_id)
                game = self.store.get_game(ng["game_id"])
                side = player.team_id
                team = self.store.get_team(side) if side else None
                opp_id = self._opponent_team_id(game, side)
                opp = self.store.get_team(opp_id) if opp_id else None
                rstatus = self.roster.compute_roster_status(game.id, side)
                ng.update({
                    "team_id": side,
                    "team_name": team.name if team else side,
                    "opponent_name": opp.name if opp else None,
                    "team_status": self._PLAYER_TEAM_STATUS.get(
                        rstatus.status.value, "not_responded"),
                })
                return out
            target, attr, patch = _ApiService, "get_player_home", home
        elif kind == "pointer_selection":
            # The SELECTION half alone: which GAMES the page is about, back on
            # the permanent pointer. A test that only checked the side within
            # a game would still pass under this.
            def plays_in(self, g, player):
                return (self._is_visible_game(g)
                        and player.team_id in (g.home_team_id, g.away_team_id))
            target, attr, patch = RosterService, "_plays_in", plays_in
        else:  # pragma: no cover - a typo in a falsifier name must be loud
            raise AssertionError(f"unknown falsifier {kind!r}")
        original = target.__dict__[attr]
        setattr(target, attr, patch)
        try:
            yield
        finally:
            setattr(target, attr, original)

    def _require_player_home_falsifier_breaks(self, kind, body, label):
        with self._falsified_player_home(kind):
            try:
                body()
            except AssertionError:
                return
        self.fail(
            f"FALSIFIER '{kind}' did not break {label}: the assertions still "
            f"passed with the pointer-based behaviour restored, so they do "
            f"not actually pin this fix.")


# ---------------------------------------------------------------------------
# 1. THE SIDE COMES FROM MEMBERSHIP — both stale-pointer directions, the
#    authorized current membership, the unauthorized caller, the operator.
# ---------------------------------------------------------------------------
class TheNextGameSideIsTheMembershipSide(_PlayerHomeHarness,
                                         unittest.TestCase):
    """Every (pointer, membership) shape the ruling enumerates, over real
    authenticated sessions where the caller has one and at the facade where
    the shape is about the resolution rather than about the HTTP identity
    rules.

    Both the PRIVATE enum and the PUBLIC names are asserted, because this was
    one defect with two faces: the Mover was served the opponent's private
    per-side state AND told the opponent was their own team."""

    CASES = ["mover_pointer_home_membership_away",
             "mover_pointer_outside_the_game",
             "membership_moved_off_the_team",
             "membership_ended",
             "no_seasonal_record",
             "loyal_member_unchanged",
             "unauthorized_caller",
             "unscoped_operator"]

    def test_every_pointer_membership_shape(self):
        ran = []
        for label, store in self._stores():
            try:
                self._assert_backend(label, store)
                store.clear_all_data()
                fx = self._fixture(store)
                p = fx["people"]
                who = self._serve(fx)

                def check():
                    # (1) POINTER HOME, MEMBERSHIP AWAY — the shape that
                    # reproduced. The AWAY side, named correctly, with AWAY's
                    # own `open_slot` rendered as "short". HOME's value would
                    # be "sub_search".
                    body = self._home(who["awayplayer"], f"{label}/mover")
                    self.assertEqual(body["player_id"], p["awayside"]["id"])
                    self._assert_next_game(
                        body, fx["away"], "Away", "Home", "short",
                        f"{label}/pointer-HOME-membership-AWAY")

                    # (2) POINTER OUTSIDE THE GAME, MEMBERSHIP HOME — the
                    # OTHER stale direction: a real Mover who was shown no
                    # next game at all.
                    body = self._home(who["homeplayer"], f"{label}/denied")
                    self.assertEqual(body["player_id"], p["candidate"]["id"])
                    self._assert_next_game(
                        body, fx["home"], "Home", "Away", "sub_search",
                        f"{label}/pointer-THIRD-membership-HOME")

                    # (3) POINTER HOME, MEMBERSHIP MOVED TO THIRD.
                    self._assert_no_private_side_state(
                        self._home_for(fx, p["departed"]["id"]), fx,
                        f"{label}/membership-moved-off")
                    # (4) POINTER HOME, MEMBERSHIP ENDED.
                    self._assert_no_private_side_state(
                        self._home_for(fx, p["expired"]["id"]), fx,
                        f"{label}/membership-ended")
                    # (5) POINTER HOME, NO SEASONAL RECORD AT ALL.
                    self._assert_no_private_side_state(
                        self._home_for(fx, p["ghost"]["id"]), fx,
                        f"{label}/no-membership")

                    # (6) THE ORDINARY PLAYER, UNCHANGED.
                    self._assert_next_game(
                        self._home_for(fx, p["loyal"]["id"]),
                        fx["home"], "Home", "Away", "sub_search",
                        f"{label}/loyal")

                def check_unauthorized():
                    # (7) A SESSION WITH NO PLAYER BINDING gets the stub —
                    # asserted as the WHOLE dict, so a future field carrying
                    # private state into this answer fails here.
                    for user in ("homecoach", "awaycoach", "thirdcoach",
                                 "official", "guardian", "moverguardian"):
                        body = self._home(who[user], f"{label}/{user}")
                        self.assertEqual(
                            body, EMPTY_STUB,
                            f"[{label}] {user} — a caller with no player "
                            f"binding received more than the empty stub")
                    # (8) THE UNSCOPED OPERATOR, unchanged: no player
                    # binding, so the same stub.
                    self.assertEqual(
                        self._home(who["operator"], f"{label}/operator"),
                        EMPTY_STUB, f"[{label}] operator")

                with self.subTest(backend=label):
                    check()
                    check_unauthorized()
                    self._require_player_home_falsifier_breaks(
                        "pointer_side", check,
                        f"[{label}] the next-game side")
                    self._require_player_home_falsifier_breaks(
                        "pointer_selection", check,
                        f"[{label}] the next-game selection")
                ran.extend((label, case) for case in self.CASES)
            finally:
                self._close(label, store)
        self._assert_matrix_ran(ran, self.CASES)


# ---------------------------------------------------------------------------
# 2. THE GUARDIAN INHERITS THE JUNIOR'S RESOLVED SIDE.
# ---------------------------------------------------------------------------
class AGuardianInheritsTheJuniorsResolvedSide(_PlayerHomeHarness,
                                              unittest.TestCase):
    """``GET /api/me/guardian/home`` returns each verified junior's Player
    Home payload, so it carried this leak unchanged — and to a second person.

    Driven over a real guardian session, with an UNVERIFIED link to a second
    junior present in the store, so "only verified links surface" is asserted
    against a link that exists rather than against its absence."""

    CASES = ["guardian_reads_the_juniors_membership_side",
             "an_unverified_link_surfaces_nothing"]

    def test_a_verified_guardian_reads_the_membership_side(self):
        ran = []
        for label, store in self._stores():
            try:
                self._assert_backend(label, store)
                store.clear_all_data()
                fx = self._fixture(store)
                p = fx["people"]
                who = self._serve(fx)

                def check():
                    body = self._guardian_home(who["moverguardian"], label)
                    juniors = {j["player_id"]: j for j in body["juniors"]}
                    self.assertEqual(
                        set(juniors), {p["awayside"]["id"]},
                        f"[{label}] an UNVERIFIED link surfaced a junior, or "
                        f"the verified one did not")
                    self._assert_next_game(
                        juniors[p["awayside"]["id"]],
                        fx["away"], "Away", "Home", "short",
                        f"{label}/guardian-of-mover")
                    # The opponent's private label must not appear ANYWHERE in
                    # the guardian's payload — not only in the field checked
                    # above.
                    self.assertNotIn(
                        '"sub_search"',
                        json.dumps(body, sort_keys=True, default=str),
                        f"[{label}] HOME's private per-side label reached the "
                        f"guardian of an AWAY junior")

                with self.subTest(backend=label):
                    check()
                    self._require_player_home_falsifier_breaks(
                        "pointer_side", check,
                        f"[{label}] the guardian's inherited side")
                ran.extend((label, case) for case in self.CASES)
            finally:
                self._close(label, store)
        self._assert_matrix_ran(ran, self.CASES)


# ---------------------------------------------------------------------------
# 3. NON-INTERFERENCE: the opponent's private state is not an input.
# ---------------------------------------------------------------------------
class ChangingOnlyTheOpponentsStateChangesNothing(_PlayerHomeHarness,
                                                  unittest.TestCase):
    """THE ASSERTION THE RULING ADDS, and the one that FOUND this leak.

    Two worlds: perturb ONLY one side's private per-side state and re-read.
    Any byte of a scoped caller's response that moves is a FUNCTION of that
    side's private state — which catches names, counts and DERIVED ENUMS
    alike, and needed no prior guess about which field was leaking.

    BOTH DIRECTIONS. The previous round perturbed HOME only, and a symmetric
    AWAY perturbation gave zero diffs — a FIXTURE ARTIFACT (every Mover's
    pointer named HOME there), not a property of the defect. Here the AWAY
    perturbation is read by a caller whose pointer names AWAY
    (``home_via_away_pointer``), so the mirror really can fail."""

    CASES = ["perturbing_home_does_not_reach_an_away_caller",
             "perturbing_away_does_not_reach_a_home_caller",
             "the_entitled_caller_does_see_their_own_side_move"]

    def _fixture(self, store):
        fx = super()._fixture(store)
        # THE MIRROR the previous round lacked: pointer AWAY, membership
        # HOME. Without it, "perturbing AWAY changes nothing" is true of the
        # FIXTURE (no reader's pointer names AWAY) rather than of the code.
        fx["people"]["home_via_away_pointer"] = self._mover(
            fx, "Home Via Away Pointer", fx["away"], fx["home"])
        return fx

    @contextlib.contextmanager
    def _perturbed(self, fx, team_id):
        """Change ONE side's private per-side state, and NOTHING else — and
        assert that it really moved.

        The state perturbed is that side's substitute pool, because
        ``compute_roster_status`` renders it as the per-side operational enum
        the Player Home Page shows (``needs_substitute`` -> ``"sub_search"``,
        ``open_slot`` -> ``"short"``). Which DIRECTION moves the enum depends
        on where the side starts, so the perturbation is chosen from the
        side's own current state rather than assumed: a side that HAS active
        enrollments has them withdrawn, a side that has none gets one. Either
        way the premise below asserts the enum genuinely changed — a
        perturbation that moved nothing would make "nothing changed" a
        vacuous observation."""
        api = fx["api"]
        before = api.roster.compute_roster_status(
            fx["gid"], team_id).status.value
        active = [s for s in api.store.substitutes_for_game(fx["gid"])
                  if s.team_id == team_id and s.status.is_active_enrollment]
        if active:
            def do():
                for sub in active:
                    out = api.withdraw_substitute(fx["gid"], sub.player_id,
                                                  actor_id=ADMIN)
                    assert "error" not in out, out

            def undo():
                for sub in active:
                    out = api.enroll_substitute(fx["gid"], sub.player_id,
                                                actor_id=ADMIN)
                    assert "error" not in out, out
        else:
            fresh = self._mover(
                fx, f"Perturber {team_id}",
                fx["home"] if team_id == fx["away"] else fx["away"], team_id)

            def do():
                out = api.enroll_substitute(fx["gid"], fresh["id"],
                                            actor_id=ADMIN)
                assert "error" not in out, out

            def undo():
                out = api.withdraw_substitute(fx["gid"], fresh["id"],
                                              actor_id=ADMIN)
                assert "error" not in out, out
        do()
        after = api.roster.compute_roster_status(
            fx["gid"], team_id).status.value
        assert before != after, (
            f"the perturbation of {team_id} did not move that side's private "
            f"per-side enum ({before!r}), so 'nothing changed' would prove "
            f"nothing")
        try:
            yield
        finally:
            undo()

    def test_only_the_entitled_side_can_move_a_response(self):
        ran = []
        for label, store in self._stores():
            try:
                self._assert_backend(label, store)
                store.clear_all_data()
                fx = self._fixture(store)
                p = fx["people"]
                self._serve(fx)
                away_reader = p["awayside"]["id"]               # pointer HOME
                home_reader = p["home_via_away_pointer"]["id"]  # pointer AWAY

                def phase(perturb_team, blind, entitled, what):
                    """Perturb ONE side; the caller not entitled to it must
                    not move, and the caller entitled to it MUST — which is
                    what makes the first assertion a narrowing rather than a
                    response that never changes at all.

                    The baselines are taken inside the phase so the two
                    phases cannot contaminate each other."""
                    base_blind = copy.deepcopy(self._home_for(fx, blind))
                    base_entitled = copy.deepcopy(self._home_for(fx, entitled))
                    with self._perturbed(fx, perturb_team):
                        self.assertEqual(
                            self._home_for(fx, blind), base_blind,
                            f"[{label}] {what}: a caller NOT entitled to "
                            f"{perturb_team} saw their Player Home change "
                            f"when only {perturb_team}'s private state "
                            f"changed, so their response is a FUNCTION of "
                            f"that side's private state")
                        self.assertNotEqual(
                            self._home_for(fx, entitled), base_entitled,
                            f"[{label}] {what}: the caller entitled to "
                            f"{perturb_team} did not see their OWN side "
                            f"move, so this perturbation proves nothing")

                def check():
                    # BOTH DIRECTIONS. The previous round perturbed HOME
                    # only, and the symmetric AWAY perturbation gave zero
                    # diffs — a FIXTURE artifact (every Mover's pointer named
                    # HOME), not a property of the defect.
                    phase(fx["home"], away_reader, home_reader,
                          "perturbing HOME")
                    phase(fx["away"], home_reader, away_reader,
                          "perturbing AWAY")

                with self.subTest(backend=label):
                    check()
                    self._require_player_home_falsifier_breaks(
                        "pointer_side", check,
                        f"[{label}] the non-interference property")
                ran.extend((label, case) for case in self.CASES)
            finally:
                self._close(label, store)
        self._assert_matrix_ran(ran, self.CASES)


class TheGameSelectionUsesTheMembershipAuthority(_PlayerHomeHarness,
                                                 unittest.TestCase):
    """``find_next_game_for_player`` and ``count_games_today_for_player``
    picked the game by ``Player.team_id`` too. That is the same guessed side
    one step earlier: it decides WHICH GAME the page is about, and the
    availability POST the screen offers is addressed to
    ``next_game.game_id``.

    The UNBOUND branch is asserted in the same class, because "resolve
    everything through membership" would be a regression there: an exhibition
    carries no LeagueSeason, so the permanent pointer is the only authority
    there has ever been and ``team_for_game`` deliberately falls back to
    it."""

    CASES = ["a_mover_is_selected_into_the_game_their_membership_names",
             "a_departed_pointer_selects_no_bound_game",
             "the_unbound_exhibition_still_follows_the_pointer",
             "todays_count_follows_the_same_authority"]

    def _fixture(self, store):
        # The UNBOUND game lives only here: its presence would give the three
        # "entitled to nothing" shapes a game after all, because there the
        # pointer IS the authority — which is this class's point and the
        # other classes' confusion.
        return self._add_exhibition(super()._fixture(store))

    def test_selection_and_the_unbound_fallback(self):
        ran = []
        for label, store in self._stores():
            try:
                self._assert_backend(label, store)
                store.clear_all_data()
                fx = self._fixture(store)
                p, api = fx["people"], fx["api"]
                self._serve(fx)

                def check():
                    # The Mover whose pointer names THIRD is selected into the
                    # game their MEMBERSHIP puts them in.
                    ng = self._home_for(fx, p["candidate"]["id"])["next_game"]
                    self.assertIsNotNone(
                        ng, f"[{label}] a Mover whose pointer names a team in "
                            f"neither game was shown no next game at all")
                    self.assertEqual(
                        ng["game_id"], fx["gid"],
                        f"[{label}] a Mover was not selected into their own "
                        f"game")
                    # The departed player's pointer still names HOME, which
                    # plays BOTH bound games. Neither is theirs — but the
                    # UNBOUND exhibition still is, because there the pointer
                    # IS the authority.
                    ng = self._home_for(fx, p["departed"]["id"])["next_game"]
                    self.assertIsNotNone(
                        ng, f"[{label}] the unbound exhibition was closed to "
                            f"a pointer-only player — the pre-#205 behaviour "
                            f"this fix must preserve")
                    self.assertEqual(ng["game_id"], fx["exhibition_id"],
                                     f"[{label}] wrong game selected")
                    self.assertEqual(ng["team_id"], fx["home"],
                                     f"[{label}] the unbound side must be the "
                                     f"permanent pointer")
                    # `count_games_today_for_player` shares the predicate:
                    # the Mover counts the TWO bound games their membership
                    # puts them in and not the exhibition (their pointer
                    # names a team not in it); the departed player counts the
                    # exhibition and NEITHER bound game.
                    saved = api.roster.clock
                    api.roster.clock = lambda: api.store.get_game(
                        fx["gid"]).start_time
                    try:
                        self.assertEqual(
                            api.roster.count_games_today_for_player(
                                p["candidate"]["id"]), 2,
                            f"[{label}] today's count did not follow the "
                            f"membership authority")
                        self.assertEqual(
                            api.roster.count_games_today_for_player(
                                p["departed"]["id"]), 1,
                            f"[{label}] a departed player's today-count still "
                            f"counted the pointer team's bound games")
                    finally:
                        api.roster.clock = saved

                with self.subTest(backend=label):
                    check()
                    self._require_player_home_falsifier_breaks(
                        "pointer_selection", check,
                        f"[{label}] the game selection")
                ran.extend((label, case) for case in self.CASES)
            finally:
                self._close(label, store)
        self._assert_matrix_ran(ran, self.CASES)


# ---------------------------------------------------------------------------
# 5. THE CROSS-GAME OVERVIEW STILL DECIDES PER ROW — confirmed, not rebuilt.
# ---------------------------------------------------------------------------
class TheOverviewStillDecidesEntitlementPerRow(_PlayerHomeHarness,
                                               unittest.TestCase):
    """The ruling's overview clause says to omit ``side`` entirely for scoped
    callers IF ``app.js`` does not consume it — and it DOES:
    ``web/static/app.js`` reads ``roster_status_restricted`` at the
    ``rosterStatusKnown`` helper, tests ``roster_status`` against
    ``["roster_confirmed","locked"]``, and renders it in the Games checklist
    tile. So the OTHERWISE branch applies: decide entitlement independently
    per row, never defaulting to HOME.

    That is what the landed commit already does, and this class CONFIRMS it
    on the two-game swapped fixture rather than rebuilding it: the same Coach
    is HOME in one row and AWAY in the next, and gets THEIR OWN side's value
    in both — a per-response decision would be right once and wrong once."""

    CASES = ["entitlement_is_decided_independently_per_row",
             "a_caller_entitled_to_no_side_gets_the_field_omitted"]

    def test_the_per_row_decision_still_holds(self):
        ran = []
        for label, store in self._stores():
            try:
                self._assert_backend(label, store)
                store.clear_all_data()
                fx = self._fixture(store)
                who = self._serve(fx)
                with self.subTest(backend=label):
                    for user, team in (("homecoach", fx["home"]),
                                       ("awaycoach", fx["away"]),
                                       ("homeplayer", fx["home"]),
                                       ("awayplayer", fx["away"])):
                        rows = self._rows(who[user], fx, f"{label}/{user}")
                        seen = {}
                        for gid in (fx["gid"], fx["gid2"]):
                            row = rows[gid]
                            self._assert_entitled(
                                row, self._side_status(fx, team, gid), team,
                                f"{label}/{user}/{gid}")
                            seen[gid] = row["roster_status"]
                        # THE PER-ROW PROPERTY, stated as a fact about the
                        # values: the fixture makes each team answer a
                        # DIFFERENT status in the two games, so one value for
                        # the whole response cannot satisfy both rows.
                        self.assertNotEqual(
                            seen[fx["gid"]], seen[fx["gid2"]],
                            f"[{label}] {user} got one value for both rows")
                        # And never HOME's by default. The two games have
                        # OPPOSITE home sides, so any caller scoped to one
                        # team is the AWAY side in exactly one row — a home
                        # default would name the home team there.
                        away_rows = [
                            gid for gid in (fx["gid"], fx["gid2"])
                            if fx["api"].store.get_game(gid).home_team_id
                            != team]
                        self.assertEqual(
                            len(away_rows), 1,
                            f"[{label}] fixture: {user} is not the away side "
                            f"in exactly one row, so a home default could "
                            f"not be distinguished")
                        self.assertEqual(
                            rows[away_rows[0]]["roster_status_team_id"], team,
                            f"[{label}] {user} was answered the HOME side in "
                            f"the row where they are the AWAY side")
                    # A caller entitled to NO side of either game.
                    rows = self._rows(who["thirdcoach"], fx,
                                      f"{label}/thirdcoach")
                    for gid in (fx["gid"], fx["gid2"]):
                        self._assert_withheld(rows[gid],
                                              f"{label}/thirdcoach/{gid}")
                ran.extend((label, case) for case in self.CASES)
            finally:
                self._close(label, store)
        self._assert_matrix_ran(ran, self.CASES)


if __name__ == "__main__":
    unittest.main()
