"""PR #427 FINAL blocker (owner ruling, 2026-08-24) — the private-game
boundary is the caller's OBTAINABLE private state, not two named routes.

THE OWNER'S RULING, verbatim on the point this file exists for::

    "The acceptance boundary is the caller's obtainable private game state,
    not only `/board` and `/lineups`; leaving F2/F3 would make the fix
    bypassable through sibling routes. ... These being pre-existing does not
    make them separate scope: they directly defeat the privacy guarantee this
    final blocker is meant to establish."

``ccdb7b4`` projected ``/board`` and ``/lineups`` onto the side the SERVER
resolves. An independent reviewer then measured three pivots still open
around that projection, all reproduced here before the fix, tri-store over
real authenticated sessions on a real socket:

F1 ``/board``'s ``notifications`` and ``audit`` are built from GAME-WIDE
   store reads and were never projected. In the very response whose
   ``team_id`` now correctly named the AWAY Coach's own side, that Coach
   still received HOME's ``substitute_enrolled`` notification, HOME's
   ``roster_selected`` audit entry (which names its players in
   ``detail.player_ids``, with no ``subject_player_id`` at all) and HOME's
   ``availability_set`` entry. ``audit_count`` counted the whole game.

F2 ``/roster-status`` called ``compute_roster_status(game_id)`` with NO team
   and so hard-coded HOME for every caller — the identical defect
   ``get_board`` had, still live one path segment away. An AWAY Coach
   received ``team_id`` naming HOME and HOME's ``substitutes_enrolled``;
   ``?team_id=<away>`` was ignored entirely, so they could not even ask for
   their own. An assigned official received ``substitutes_enrolled`` — the
   precise field ``_submitted_lineup_status`` nulls out one route over.

F3 ``/roster`` and ``/substitutes`` returned BOTH sides, unscoped, to either
   Coach AND to an assigned official.

A SECOND independent review then measured two more, both closed here (round 2):

F4 ``/availability-summary`` — the FIFTH leaf of the same ``re.match`` block,
   behind the same gate, and THE ONLY ONE THAT READS A SIDE FROM THE QUERY
   STRING. Its narrowing was spelled inline in ``web/server.py`` and named
   only COACH and PLAYER, so an assigned OFFICIAL fell straight through it.
   Measured tri-store over real sessions at ae21c40: the un-hinted call
   answered that official ``400`` while ``?team_id=<either side>`` answered
   ``200`` with that side's whole candidate pool — ``player_2``, ``player_3``,
   ``player_4``, ``player_8``, ``player_9`` — carrying NAMES and per-player
   availability, while the SAME official's ``/lineups`` one path segment away
   carried only ``player_1`` with ``availability``/``sub_status``/``eligible``
   stripped. The client hint was the SOLE side selector. The shipped UI fired
   it too, confirmed in a real browser: an official is admitted to the Roster
   tab, their ``/lineups`` sides are ``submitted_lineup`` rather than
   ``restricted``, so the tab fetched ``?team_id=<the shown side>`` on every
   render and the side toggle switched teams.

F5 ``/substitute-candidates`` served a pre-060 NULL-owner enrollment —
   attributed by LIVE MEMBERSHIP, the exact authority ``durable_game_sides``
   refuses to use — to whichever Coach its occupant belongs to TODAY, with
   ``can_offer: True``, while ``/substitutes`` correctly withheld that same
   row from BOTH. ``substitutes_enrolled`` counted it the same way. And the
   act was open, not only the read: an authenticated Coach's ``offer`` on such
   a row returned 200 and MINTED ``team_id`` onto it from today's membership,
   making the guess durable.

WHAT IS PINNED HERE, one class per clause of the ruling:

* a HOME scoped caller cannot pivot to AWAY private state through ANY of the
  four routes, and vice versa — driven as both Coaches AND as both Players;
* an assigned official cannot recover candidate, availability or substitute
  state through ANY sibling;
* CLIENT HINTS cannot select the opponent — every route is driven with
  ``?team_id=`` and ``?side=`` naming the other side and required to answer
  identically to the un-hinted call;
* restricted data is OMITTED (``null``) or explicitly FORBIDDEN (403), never
  an empty operational state — ``[]`` and ``0`` are asserted against BY NAME,
  because both make a false claim about the game rather than a true one about
  the reader's access;
* an UNSCOPED OPERATOR is byte-for-byte unchanged on all four routes, proven
  against the facade's own un-projected read rather than against a
  hand-written expectation.

EVERY NARROWING IS FALSIFIED. :meth:`_SiblingHarness._falsified_route`
reintroduces ONE narrowing's absence into the LIVE code — the un-projected
activity log, the un-narrowed roster, the un-narrowed substitutes, the
home-defaulted status, the full-log ``audit_count``, an official served
instead of refused, and a refusal degraded to ``[]`` — and
``_require_falsifier_breaks`` fails BY NAME if the assertions still pass
without it.

MOVER-SHAPED. Every player is a Mover (permanent pointer and seasonal
membership name different teams), so a response that happened to be right for
pointer reasons cannot pass, and the legacy NULL-attribution shapes are
present on purpose: they must appear on NEITHER side, never be guessed onto
one.

TRI-STORE, PROVEN: ``_stores`` yields Memory, SQLite and — when
TEST_DATABASE_URL is set — real PostgreSQL; ``_assert_backend`` PROVES each
backend rather than trusting the env var, and ``_assert_matrix_ran`` fails a
silently narrow loop. A SKIP IS NOT A PASS.
"""

import contextlib
import json
import re
import unittest

from helpers import BACKEND  # noqa: F401
from test_lineup_side_projection import (OFFICIAL_FORBIDDEN_PLAYER_FIELDS,
                                         _ProjectionHarness)
from test_substitute_membership_cutover import ADMIN

from hockey_scheduler.api.service import ApiService as _ApiService
from hockey_scheduler.services import lineup_visibility
from hockey_scheduler.services.roster_service import RosterService

#: The routes the ruling names as ONE acceptance boundary — "the caller's
#: obtainable private game state". ``availability-summary`` joined the list in
#: round 2: it is the fifth leaf of the same ``re.match`` block, behind the
#: same gate, and it was the last one still answering around the projection.
PRIVATE_SIBLINGS = ("board", "roster-status", "roster", "substitutes",
                    "availability-summary")

#: Query strings that NAME A SIDE. Every one of them is a client hint and
#: every one must be ignored: `team_id` because that is the parameter the
#: sibling `availability-summary` leaf really does accept (so a caller has
#: every reason to try it here), `side` because it is the obvious second
#: guess. The assertion is not "these particular names are ignored" but
#: "nothing the client can say selects a side", which is why the expected
#: answer is byte equality with the UN-hinted response.
SIDE_HINTS = ("team_id", "side", "for_team", "team")


class _SiblingHarness(_ProjectionHarness):
    """``_ProjectionHarness``'s socket, sessions and Mover fixture, with BOTH
    sides carrying durable rows.

    The shared fixture puts almost every durable row on HOME, which would let
    "own side only" pass on the AWAY Coach's routes for the wrong reason — an
    empty list is not evidence of narrowing. Two AWAY-side rows are added so
    each direction of the pivot has something real to fail to reach."""

    def _fixture(self, store):
        fx = super()._fixture(store)
        api, p = fx["api"], fx["people"]
        # Pointer HOME, membership AWAY: a Mover in the direction that makes
        # a pointer-based read answer HOME.
        p["away_seated"] = self._mover(fx, "Away Seated", fx["home"],
                                       fx["away"])
        assert "error" not in api.select_roster(
            fx["gid"], [p["away_seated"]["id"]], actor_id=ADMIN)
        p["away_sub"] = self._mover(fx, "Away Sub", fx["home"], fx["away"])
        assert "error" not in api.enroll_substitute(
            fx["gid"], p["away_sub"]["id"], actor_id=ADMIN)
        # A pre-060 NULL-OWNER enrollment whose occupant is a LIVE AWAY
        # member, mirroring the shared fixture's `legacy_sub` (live HOME).
        # Both directions are needed: with only the HOME-side one, "attributed
        # by live membership" and "attributed to HOME" are the same answer,
        # and a test cannot tell which rule produced it.
        p["away_legacy_sub"] = self._mover(fx, "Away Legacy Sub",
                                           fx["home"], fx["away"])
        assert "error" not in api.enroll_substitute(
            fx["gid"], p["away_legacy_sub"]["id"], actor_id=ADMIN)
        self._strip_sub_owner(api, fx["gid"], p["away_legacy_sub"]["id"])
        # The premise every assertion below rests on: the two sides' durable
        # populations are DISJOINT and neither is empty.
        home_side = self._durable(fx, fx["home"])
        away_side = self._durable(fx, fx["away"])
        assert home_side and away_side, (home_side, away_side)
        assert not (home_side & away_side), home_side & away_side
        return fx

    # -- what the game can durably say ------------------------------------
    def _durable(self, fx, team_id) -> set:
        sides = RosterService(fx["api"].store).durable_game_sides(fx["gid"])
        return {pid for pid, side in sides.items() if side == team_id}

    def _unattributable(self, fx) -> set:
        """The people no durable record can place on a side — the legacy
        pre-060/061 shapes. They must appear on NEITHER Coach's response."""
        sides = RosterService(fx["api"].store).durable_game_sides(fx["gid"])
        return {v["id"] for k, v in fx["people"].items()
                if k in ("legacy_seat", "legacy_sub", "orphan_seat",
                         "orphan_sub", "away_legacy_sub")} - set(sides)

    # -- readers -----------------------------------------------------------
    def _get(self, opener, fx, route, query=""):
        return self._req(opener, "GET",
                         f"/api/games/{fx['gid']}/{route}{query}")

    def _ok(self, opener, fx, route, query=""):
        status, body = self._get(opener, fx, route, query)
        self.assertEqual(status, 200, (route, body))
        return body

    def _forbidden(self, opener, fx, route, label, query=""):
        """A refusal, asserted as a REFUSAL — and explicitly not as an empty
        collection, which is the failure mode the ruling names."""
        status, body = self._get(opener, fx, route, query)
        self.assertNotEqual(
            status, 200,
            f"[{label}] {route} answered 200 with {body!r}; restricted data "
            "must be omitted or FORBIDDEN, never represented as an empty "
            "operational state")
        self.assertEqual(status, 403, (label, route, body))
        self.assertEqual(body["error"]["code"], "forbidden", body)
        return body

    def _assert_absent(self, payload, player_ids, label, what):
        """No opponent identity ANYWHERE in the serialized response.

        Checked against the raw JSON TEXT rather than against named fields on
        purpose: the leak this closes lived in ``detail.player_ids``, a
        free-form payload nested two levels down that no field-by-field
        assertion would have looked at — and a text scan also catches an id
        embedded in a free-text ``message``.

        WORD-BOUNDARY MATCHED, not substring. ``player_1`` is a PREFIX of
        ``player_12``, so a plain ``assertNotIn`` reports the away Coach's own
        ``player_12`` as a leak of the home side's ``player_1`` — a false
        positive this file hit on all three backends before the boundaries
        were added, and one that would have made the whole matrix
        untrustworthy in the other direction too."""
        blob = json.dumps(payload, sort_keys=True, default=str)
        for pid in sorted(player_ids):
            self.assertIsNone(
                re.search(rf"\b{re.escape(pid)}\b", blob),
                f"[{label}] {what}: the opponent's {pid} appears in this "
                "response")

    # -- the executable falsifiers -----------------------------------------
    @contextlib.contextmanager
    def _falsified_route(self, kind):
        """Reintroduce ONE narrowing's ABSENCE into the LIVE code.

        Each entry restores exactly the un-narrowed behavior measured at
        ccdb7b4, so the assertions run under it are the assertions that
        reproduced the leak."""
        api = _ApiService
        if kind == "board_activity_unprojected":
            # F1: the game-wide collections, unprojected, for everybody.
            def projection(self, game_id, audience, team_id, notifications,
                           audit):
                return lineup_visibility.FULL, notifications, audit
            target, attr, patch = api, "_activity_projection", projection
        elif kind == "audit_count_over_full_log":
            # The covert cardinality oracle: rows omitted, but still counted.
            real = api.get_board

            def board(self, game_id, team_id=None, viewer_role=None):
                out = real(self, game_id, team_id=team_id,
                           viewer_role=viewer_role)
                out["audit_count"] = len(self.store.audit_for_game(game_id))
                return out
            target, attr, patch = api, "get_board", board
        elif kind == "roster_unnarrowed":
            # F3, first half.
            def get_roster(self, game_id, viewer_role=None,
                           viewer_team_id=None):
                self.roster._require_game(game_id)
                return [r for r in self.store.roster_for_game(game_id)]
            target, attr, patch = api, "get_roster", _serializing(get_roster)
        elif kind == "substitutes_unnarrowed":
            # F3, second half -- and the official's leak with it.
            def get_substitutes(self, game_id, viewer_role=None,
                                viewer_team_id=None):
                self.roster._require_game(game_id)
                return [s for s in self.store.substitutes_for_game(game_id)]
            target, attr, patch = (api, "get_substitutes",
                                   _serializing(get_substitutes))
        elif kind == "roster_status_home_default":
            # F2 exactly as measured: the trusted side arrives and is dropped.
            def get_roster_status(self, game_id, viewer_role=None,
                                  viewer_team_id=None):
                return self.roster.compute_roster_status(game_id).to_dict()
            target, attr, patch = api, "get_roster_status", get_roster_status
        elif kind == "official_substitutes_served":
            # The official refusal alone, with the Coach narrowing intact --
            # so a test that only checked the Coaches cannot cover for it.
            real = api.get_substitutes

            def get_substitutes(self, game_id, viewer_role=None,
                                viewer_team_id=None):
                from hockey_scheduler.domain import Role
                if viewer_role == Role.OFFICIAL:
                    return [_dump(s)
                            for s in self.store.substitutes_for_game(game_id)]
                return real(self, game_id, viewer_role=viewer_role,
                            viewer_team_id=viewer_team_id)
            target, attr, patch = api, "get_substitutes", get_substitutes
        elif kind == "empty_instead_of_forbidden":
            # THE RULING'S NAMED FAILURE MODE: refuse by returning nothing,
            # which reads as "there is none" rather than "not for you".
            real = api.get_substitutes

            def get_substitutes(self, game_id, viewer_role=None,
                                viewer_team_id=None):
                out = real(self, game_id, viewer_role=viewer_role,
                           viewer_team_id=viewer_team_id)
                return [] if isinstance(out, dict) and "error" in out else out
            target, attr, patch = api, "get_substitutes", get_substitutes
        elif kind == "availability_official_served":
            # F4: the leaf's inline narrowing, restored exactly as it stood --
            # COACH and PLAYER only, so an OFFICIAL falls through it and the
            # client hint selects the side.
            real = api.get_availability_summary

            def summary(self, game_id, team_id=None, viewer_role=None,
                        viewer_team_id=None):
                from hockey_scheduler.domain import Role
                if viewer_role in (Role.COACH, Role.PLAYER):
                    return real(self, game_id, team_id,
                                viewer_role=viewer_role,
                                viewer_team_id=viewer_team_id)
                return self._availability_summary_of(
                    self.roster._require_game(game_id),
                    team_id or viewer_team_id or "")
            target, attr, patch = (api, "get_availability_summary",
                                   _catching(summary))
        elif kind == "availability_hint_honoured":
            # F4's other half, ALONE: the official refusal intact, but a
            # Coach/Player's `?team_id=` selects the side again -- so a test
            # that only drove the official cannot cover for it.
            real = api.get_availability_summary

            def summary(self, game_id, team_id=None, viewer_role=None,
                        viewer_team_id=None):
                return real(self, game_id, team_id,
                            viewer_role=viewer_role,
                            viewer_team_id=team_id or viewer_team_id)
            target, attr, patch = api, "get_availability_summary", summary
        elif kind == "availability_empty_instead_of_forbidden":
            # THE RULING'S NAMED FAILURE MODE on this route: refuse by
            # answering an empty summary with zero counts, which asserts
            # "nobody on this team owes an answer".
            real = api.get_availability_summary

            def summary(self, game_id, team_id=None, viewer_role=None,
                        viewer_team_id=None):
                out = real(self, game_id, team_id, viewer_role=viewer_role,
                           viewer_team_id=viewer_team_id)
                if isinstance(out, dict) and "error" in out:
                    return {"game_id": game_id, "team_id": team_id,
                            "counts": {"available": 0, "unavailable": 0,
                                       "maybe": 0, "no_response": 0},
                            "players": []}
                return out
            target, attr, patch = api, "get_availability_summary", summary
        elif kind == "availability_operator_narrowed":
            # THE OTHER DIRECTION, and its own falsifier because narrowing the
            # UNSCOPED OPERATOR is its own regression rather than a milder
            # version of the same fix. The shape restored is the historical
            # one this whole blocker began with -- `get_board`'s hard-coded
            # HOME -- applied here: the operator's hint is dropped and every
            # answer is the home side's.
            real = api.get_availability_summary

            def summary(self, game_id, team_id=None, viewer_role=None,
                        viewer_team_id=None):
                game = self.roster._require_game(game_id)
                if lineup_visibility.route_audience(
                        viewer_role, viewer_team_id, game.home_team_id,
                        game.away_team_id) == lineup_visibility.FULL:
                    return self._availability_summary_of(
                        game, game.home_team_id)
                return real(self, game_id, team_id, viewer_role=viewer_role,
                            viewer_team_id=viewer_team_id)
            target, attr, patch = (api, "get_availability_summary",
                                   _catching(summary))
        elif kind == "candidates_by_live_membership":
            # F5, the read half: the outreach queue attributed by LIVE
            # membership, which is what served a NULL-owner row to whichever
            # Coach its occupant belongs to today.
            real = RosterService.list_substitute_candidates

            def candidates(self, game_id, team_id=None, rstatus=None):
                rows = real(self, game_id, team_id=team_id, rstatus=rstatus)
                game = self._require_game(game_id)
                team_id = team_id or game.home_team_id
                seen = {r["player_id"] for r in rows}
                for sub in self.store.substitutes_for_game(game_id):
                    if sub.team_id is not None or sub.player_id in seen:
                        continue
                    player = self.store.get_player(sub.player_id)
                    if (player is None or not player.is_active
                            or self.team_for_game(game, player) != team_id):
                        continue
                    rows.append({
                        "player_id": sub.player_id, "name": player.name,
                        "position": sub.position.value,
                        "slot_type": sub.slot_type.value,
                        "status": sub.status.value,
                        "priority_rank": sub.priority_rank,
                        "can_offer": True})
                return rows
            target, attr, patch = (RosterService, "list_substitute_candidates",
                                   candidates)
        elif kind == "subs_counted_by_live_membership":
            # F5, the COUNT half: `_side_data` charging an enrollment to
            # whichever side its occupant's CURRENT membership names, so a
            # NULL-owner row is counted into one side's substitutes_enrolled.
            real = RosterService._side_data

            def side_data(self, game_id, team_id):
                summaries, entries, subs = real(self, game_id, team_id)
                game = self._require_game(game_id)
                owned = {s.player_id for s in subs}
                for sub in self.store.substitutes_for_game(game_id):
                    if sub.team_id is not None or sub.player_id in owned:
                        continue
                    player = self.store.get_player(sub.player_id)
                    if (player is not None
                            and self.team_for_game(game, player) == team_id):
                        subs.append(sub)
                return summaries, entries, subs
            target, attr, patch = RosterService, "_side_data", side_data
        elif kind == "null_owner_actionable":
            # F5, the ACT half: the guard that stops a team-scoped Coach
            # transitioning a row whose admitting side is unknown, removed --
            # so `offer` mints an admitting side out of today's membership.
            def allow(self, sub, authorized_team_id):
                return None
            target, attr, patch = (RosterService,
                                   "_require_attributed_enrollment", allow)
        elif kind == "attribution_by_live_membership":
            # NEVER GUESS A SIDE: attribution re-derived from the player's
            # CURRENT membership, which hands a legacy NULL row a side it
            # never durably had.
            def durable_game_sides(self, game_id):
                game = self.store.get_game(game_id)
                contexts = self.resolve_membership_contexts_for_game(game)
                return {pid: ctx.team_id for pid, ctx in contexts.items()}
            target, attr, patch = (RosterService, "durable_game_sides",
                                   durable_game_sides)
        else:  # pragma: no cover - a typo in a falsifier name must be loud
            raise AssertionError(f"unknown falsifier {kind!r}")
        original = target.__dict__[attr]
        setattr(target, attr, patch)
        try:
            yield
        finally:
            setattr(target, attr, original)

    def _require_falsifier_breaks(self, kind, body, label):
        """Run ``body`` under falsifier ``kind`` and REQUIRE it to fail.

        A narrowing whose falsifier leaves the suite green is a narrowing with
        no test, and it is reported BY NAME rather than silently tolerated."""
        with self._falsified_route(kind):
            try:
                body()
            except AssertionError:
                return
        self.fail(
            f"FALSIFIER '{kind}' did not break {label}: the assertions still "
            "passed with the un-narrowed behavior restored, so they do not "
            "actually pin this narrowing.")


def _dump(obj):
    from hockey_scheduler.api.service import _serialize
    return _serialize(obj)


def _catching(fn):
    """Wrap a falsifier that may raise a domain error so it matches the real
    method's ``@catch``-wrapped contract (a structured error dict, never an
    exception across the facade boundary)."""
    from hockey_scheduler.api.service import catch
    return catch(fn)


def _serializing(fn):
    """Wrap a falsifier that returns domain rows so it matches the real
    method's serialized, ``@catch``-wrapped contract."""
    def wrapper(self, *a, **kw):
        return [_dump(r) for r in fn(self, *a, **kw)]
    return wrapper


# ---------------------------------------------------------------------------
# 1. F2 — /roster-status answers the caller's OWN side, and refuses officials.
# ---------------------------------------------------------------------------
class RosterStatusAnswersOnlyTheCallersOwnSide(_SiblingHarness,
                                               unittest.TestCase):
    """"`/roster-status`: Coach/Player receives only the trusted own-side
    status. Ignore client side hints."

    The reproduced defect returned ``team_id`` naming HOME to an AWAY Coach
    along with HOME's ``substitutes_enrolled``, and ignored ``?team_id``
    entirely — so BOTH halves are asserted: the status must name the caller's
    own side, and a hint naming the opponent must change nothing."""

    def test_each_scoped_caller_gets_their_own_status(self):
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
                        body = self._ok(who[user], fx, "roster-status")
                        self.assertEqual(
                            body["team_id"], team,
                            f"[{label}] {user} received the OTHER side's "
                            "roster status")
                        other = (fx["away"] if team == fx["home"]
                                 else fx["home"])
                        for hint in SIDE_HINTS:
                            hinted = self._ok(who[user], fx, "roster-status",
                                              f"?{hint}={other}")
                            self.assertEqual(
                                hinted, body,
                                f"[{label}] {user}: ?{hint}= changed the "
                                "roster-status response -- a client hint "
                                "selected a side")

                with self.subTest(backend=label):
                    check()
                    # An assigned official is REFUSED, not served an empty or
                    # a narrowed status: the Game Sheet does not fetch this
                    # route at all.
                    self._forbidden(who["official"], fx, "roster-status",
                                    f"{label}/official")
                    # The UNSCOPED OPERATOR is unchanged -- proven against the
                    # facade's own un-projected read, not a literal.
                    self.assertEqual(
                        self._ok(who["operator"], fx, "roster-status"),
                        fx["api"].get_roster_status(fx["gid"]),
                        f"[{label}] the operator's roster status changed")
                self._require_falsifier_breaks(
                    "roster_status_home_default", check,
                    f"[{label}] own-side roster status")
                ran.append((label, "roster_status_own_side"))
            finally:
                self._close(label, store)
        self._assert_matrix_ran(ran, ["roster_status_own_side"])


# ---------------------------------------------------------------------------
# 2. F3 — /roster: own-side DURABLY ATTRIBUTED rows; officials get the sheet.
# ---------------------------------------------------------------------------
class RosterGivesStrictlyOwnSideDurablyAttributedRows(_SiblingHarness,
                                                      unittest.TestCase):
    """"`/roster`: Coach/Player receives strictly own-side durably attributed
    rows. Officials receive only the two-side submitted/occupying lineup
    projection."

    STRICTLY, and DURABLY, are separate properties and are asserted
    separately: the opponent's rows must be absent, AND the legacy
    NULL-attribution rows must be absent from BOTH sides rather than guessed
    onto one."""

    def test_a_coach_reaches_only_their_own_durable_rows(self):
        ran = []
        for label, store in self._stores():
            try:
                self._assert_backend(label, store)
                store.clear_all_data()
                fx = self._fixture(store)
                who = self._serve(fx)
                nameless = self._unattributable(fx)
                self.assertTrue(nameless, "fixture: no unattributable rows")

                def check():
                    for user, team in (("homecoach", fx["home"]),
                                       ("homeplayer", fx["home"]),
                                       ("awaycoach", fx["away"]),
                                       ("awayplayer", fx["away"])):
                        rows = self._ok(who[user], fx, "roster")
                        got = {r["player_id"] for r in rows}
                        mine = self._durable(fx, team)
                        theirs = self._durable(
                            fx, fx["away"] if team == fx["home"]
                            else fx["home"])
                        self.assertTrue(
                            got <= mine,
                            f"[{label}] {user} received rows outside their "
                            f"own durable side: {sorted(got - mine)}")
                        self._assert_absent(rows, theirs, f"{label}/{user}",
                                            "/roster")
                        # Legacy NULL attribution: omitted, never guessed.
                        self.assertFalse(
                            got & nameless,
                            f"[{label}] {user} received a row with NO durable "
                            f"attribution: {sorted(got & nameless)}")
                        for row in rows:
                            self.assertEqual(row["team_side"], team, row)

                with self.subTest(backend=label):
                    check()
                    # Each Coach's list is NON-EMPTY, so "own side only" is
                    # not passing by returning nothing.
                    self.assertTrue(
                        self._ok(who["homecoach"], fx, "roster"), "home empty")
                    self.assertTrue(
                        self._ok(who["awaycoach"], fx, "roster"), "away empty")
                    self._assert_official_sheet(fx, who, label)
                    self._assert_operator_unchanged(fx, who, label, "roster")
                self._require_falsifier_breaks(
                    "roster_unnarrowed", check,
                    f"[{label}] own-side /roster")
                ran.append((label, "roster_own_side"))
            finally:
                self._close(label, store)
        self._assert_matrix_ran(ran, ["roster_own_side"])

    def _assert_official_sheet(self, fx, who, label):
        """An official receives the SUBMITTED LINEUP -- both sides, selected
        rows only, and none of the private per-player workflow fields."""
        rows = self._ok(who["official"], fx, "roster")
        self.assertTrue(rows, f"[{label}] official received no lineup at all")
        sides = {r["team_id"] for r in rows}
        self.assertEqual(sides, {fx["home"], fx["away"]}, rows)
        for row in rows:
            self.assertEqual(row["group"], "selected", row)
            for field in OFFICIAL_FORBIDDEN_PLAYER_FIELDS:
                self.assertNotIn(
                    field, row,
                    f"[{label}] the official's /roster row carries the "
                    f"private field {field!r} -- the /lineups projection "
                    "drops it, so the two routes have drifted")
        # SAME rows as the other route's official projection: one projection,
        # two endpoints, no drift.
        from_lineups = []
        lineups = self._lineups(who["official"], fx)
        for key in ("home", "away"):
            for row in lineups[key]["players"]:
                from_lineups.append({**row, "team_id": lineups[key]["team_id"]})
        self.assertEqual(
            sorted(rows, key=lambda r: (r["team_id"], r["id"])),
            sorted(from_lineups, key=lambda r: (r["team_id"], r["id"])),
            f"[{label}] /roster and /lineups disagree about what an assigned "
            "official may read")

    def _assert_operator_unchanged(self, fx, who, label, route):
        served = self._ok(who["operator"], fx, route)
        direct = getattr(fx["api"], f"get_{route.replace('-', '_')}")(
            fx["gid"])
        self.assertEqual(served, direct,
                         f"[{label}] the operator's {route} was narrowed")


# ---------------------------------------------------------------------------
# 3. F3 — /substitutes: own-side durably OWNED; officials REFUSED.
# ---------------------------------------------------------------------------
class SubstitutesAreOwnSideOnlyAndRefusedToOfficials(_SiblingHarness,
                                                     unittest.TestCase):
    """"`/substitutes`: Coach/Player receives strictly own-side durably owned
    rows; officials are refused. Legacy NULL attribution is omitted, never
    guessed."

    The official clause has its own falsifier
    (``official_substitutes_served``) because the Coach narrowing and the
    official refusal are independent defects — the reviewer measured BOTH,
    and a test that only drove the Coaches would have covered for one."""

    def test_only_the_owning_side_reads_its_enrollments(self):
        ran = []
        for label, store in self._stores():
            try:
                self._assert_backend(label, store)
                store.clear_all_data()
                fx = self._fixture(store)
                who = self._serve(fx)
                nameless = {fx["people"][k]["id"]
                            for k in ("legacy_sub", "orphan_sub",
                                      "away_legacy_sub")}

                def check():
                    for user, team in (("homecoach", fx["home"]),
                                       ("homeplayer", fx["home"]),
                                       ("awaycoach", fx["away"]),
                                       ("awayplayer", fx["away"])):
                        rows = self._ok(who[user], fx, "substitutes")
                        other = (fx["away"] if team == fx["home"]
                                 else fx["home"])
                        for row in rows:
                            self.assertEqual(
                                row["team_id"], team,
                                f"[{label}] {user} received an enrollment "
                                "owned by the other side")
                        got = {r["player_id"] for r in rows}
                        self.assertFalse(
                            got & nameless,
                            f"[{label}] {user} received a legacy NULL-owner "
                            f"enrollment on a GUESSED side: "
                            f"{sorted(got & nameless)}")
                        self._assert_absent(
                            rows, self._durable(fx, other),
                            f"{label}/{user}", "/substitutes")
                        for hint in SIDE_HINTS:
                            self.assertEqual(
                                self._ok(who[user], fx, "substitutes",
                                         f"?{hint}={other}"),
                                rows,
                                f"[{label}] {user}: ?{hint}= changed the "
                                "substitute rows")

                def check_official():
                    self._forbidden(who["official"], fx, "substitutes",
                                    f"{label}/official")

                with self.subTest(backend=label):
                    check()
                    check_official()
                    self.assertTrue(
                        self._ok(who["homecoach"], fx, "substitutes"),
                        f"[{label}] HOME has no enrollments -- the narrowing "
                        "would pass vacuously")
                    self.assertTrue(
                        self._ok(who["awaycoach"], fx, "substitutes"),
                        f"[{label}] AWAY has no enrollments")
                    served = self._ok(who["operator"], fx, "substitutes")
                    self.assertEqual(
                        served, fx["api"].get_substitutes(fx["gid"]),
                        f"[{label}] the operator's /substitutes was narrowed")
                    # The operator DOES still see the legacy NULL-owner rows,
                    # which is what makes the Coaches' omission of them a
                    # narrowing rather than the rows being absent entirely.
                    self.assertTrue(
                        {r["player_id"] for r in served} & nameless,
                        f"[{label}] fixture: the operator cannot see the "
                        "legacy rows either, so their omission proves nothing")
                self._require_falsifier_breaks(
                    "substitutes_unnarrowed", check,
                    f"[{label}] own-side /substitutes")
                self._require_falsifier_breaks(
                    "official_substitutes_served", check_official,
                    f"[{label}] the official's /substitutes refusal")
                self._require_falsifier_breaks(
                    "empty_instead_of_forbidden", check_official,
                    f"[{label}] refusal-not-empty on /substitutes")
                ran.append((label, "substitutes_own_side"))
            finally:
                self._close(label, store)
        self._assert_matrix_ran(ran, ["substitutes_own_side"])


# ---------------------------------------------------------------------------
# 4. F1 — /board's activity log is attributed, and audit_count counts what
#    was SENT.
# ---------------------------------------------------------------------------
class BoardActivityIsDurablyAttributedOrOmitted(_SiblingHarness,
                                                unittest.TestCase):
    """"`/board`: scoped callers and officials must not receive game-wide
    `notifications`, `audit`, or `audit_count`. Omit them unless an event can
    be durably attributed to the permitted side."

    THE ATTRIBUTION RULE UNDER TEST: an event is retained for side S only when
    EVERY player identity it discloses is durably attributed to S, and it
    discloses at least one. The ``roster_selected`` entry is the reason for
    "every, not just the subject" — it carries no ``subject_player_id`` and
    names its players in ``detail.player_ids``, so a subject-only rule retains
    it verbatim, which is precisely what the reviewer measured leaking."""

    def test_a_scoped_caller_reads_only_their_own_sides_activity(self):
        ran = []
        for label, store in self._stores():
            try:
                self._assert_backend(label, store)
                store.clear_all_data()
                fx = self._fixture(store)
                who = self._serve(fx)
                # CAPTURED BEFORE ANY FALSIFIER RUNS, and deliberately so.
                # These expectations are derived from
                # `RosterService.durable_game_sides`, which is itself one of
                # the things falsified below -- recomputing them inside
                # `check()` let the `attribution_by_live_membership`
                # falsifier rewrite the ANSWER KEY along with the code, so
                # the test stayed green while a legacy NULL row was being
                # guessed onto a live-membership side. Measured: that
                # falsifier was the one falsifier of eight that did not
                # redden, until these three lines moved out here.
                forbidden = {
                    fx["home"]: self._durable(fx, fx["away"]),
                    fx["away"]: self._durable(fx, fx["home"]),
                }
                nameless = self._unattributable(fx)

                def check():
                    for user, team in (("homecoach", fx["home"]),
                                       ("homeplayer", fx["home"]),
                                       ("awaycoach", fx["away"]),
                                       ("awayplayer", fx["away"])):
                        board = self._ok(who[user], fx, "board")
                        self.assertEqual(board["audit_scope"],
                                         lineup_visibility.OWN_SIDE, board)
                        # Nothing about the opponent, in EITHER collection,
                        # at ANY depth -- including detail.player_ids -- and
                        # nothing about a player NO durable record can place
                        # on a side, which must reach neither Coach.
                        self._assert_absent(
                            {"notifications": board["notifications"],
                             "audit": board["audit"]},
                            forbidden[team] | nameless,
                            f"{label}/{user}", "/board activity")
                        # audit_count counts what was SENT: no covert
                        # cardinality oracle over the omitted rows.
                        self.assertEqual(
                            board["audit_count"], len(board["audit"]),
                            f"[{label}] {user}: audit_count disagrees with "
                            "the audit rows actually sent")

                def check_official():
                    board = self._ok(who["official"], fx, "board")
                    self.assertEqual(board["audit_scope"], "withheld", board)
                    for field in ("notifications", "audit", "audit_count"):
                        self.assertIsNone(
                            board[field],
                            f"[{label}] the official's board carries {field} "
                            f"as {board[field]!r}; withheld data must be "
                            "null, never an empty collection or a zero count")

                with self.subTest(backend=label):
                    check()
                    check_official()
                    # THE OPERATOR keeps the whole game-wide log, and it is
                    # STRICTLY BIGGER than what a Coach receives -- which is
                    # what proves the Coach's list was narrowed rather than
                    # the game being quiet.
                    op = self._ok(who["operator"], fx, "board")
                    self.assertEqual(op["audit_scope"],
                                     lineup_visibility.FULL, op)
                    self.assertEqual(
                        op["audit_count"],
                        len(fx["api"].store.audit_for_game(fx["gid"])),
                        f"[{label}] the operator's audit was narrowed")
                    coach = self._ok(who["homecoach"], fx, "board")
                    self.assertLess(
                        coach["audit_count"], op["audit_count"],
                        f"[{label}] the HOME Coach's audit is the same size "
                        "as the whole game's -- nothing was withheld, so "
                        "this fixture proves no narrowing")
                    self.assertTrue(
                        coach["audit"],
                        f"[{label}] the HOME Coach received NO audit rows; "
                        "own-side retention must keep genuinely own-side "
                        "events, not omit everything")
                self._require_falsifier_breaks(
                    "board_activity_unprojected", check,
                    f"[{label}] own-side /board activity")
                self._require_falsifier_breaks(
                    "board_activity_unprojected", check_official,
                    f"[{label}] the official's withheld /board activity")
                self._require_falsifier_breaks(
                    "audit_count_over_full_log", check,
                    f"[{label}] audit_count over sent rows")
                self._require_falsifier_breaks(
                    "attribution_by_live_membership", check,
                    f"[{label}] durable-only attribution")
                ran.append((label, "board_activity"))
            finally:
                self._close(label, store)
        self._assert_matrix_ran(ran, ["board_activity"])


# ---------------------------------------------------------------------------
# 5. THE PIVOT MATRIX: no scoped caller reaches the opponent through ANY of
#    the four routes, and no official recovers workflow state through ANY
#    sibling.
# ---------------------------------------------------------------------------
class TheOpponentIsUnreachableThroughEverySibling(_SiblingHarness,
                                                  unittest.TestCase):
    """The ruling's acceptance boundary, driven as ONE matrix rather than as
    four independent route tests: "a HOME scoped caller cannot pivot to AWAY
    private state through ANY of the four routes, and vice versa".

    Every principal x every route x every side hint, with the opponent's whole
    durable population required absent from the serialized response."""

    def test_no_route_hint_or_role_reaches_the_other_side(self):
        ran = []
        for label, store in self._stores():
            try:
                self._assert_backend(label, store)
                store.clear_all_data()
                fx = self._fixture(store)
                who = self._serve(fx)
                probes = 0

                with self.subTest(backend=label):
                    for user, team in (("homecoach", fx["home"]),
                                       ("homeplayer", fx["home"]),
                                       ("awaycoach", fx["away"]),
                                       ("awayplayer", fx["away"])):
                        other = (fx["away"] if team == fx["home"]
                                 else fx["home"])
                        theirs = self._durable(fx, other)
                        self.assertTrue(theirs, "fixture: opponent has no "
                                                "durable rows to leak")
                        queries = [""] + [f"?{h}={other}" for h in SIDE_HINTS]
                        for route in PRIVATE_SIBLINGS:
                            for query in queries:
                                status, body = self._get(
                                    who[user], fx, route, query)
                                probes += 1
                                if status != 200:
                                    self.assertEqual(status, 403,
                                                     (route, query, body))
                                    continue
                                self._assert_absent(
                                    body, theirs,
                                    f"{label}/{user}/{route}{query}",
                                    "pivot probe")
                    # THE OFFICIAL: no candidate or substitute state through
                    # any sibling. `/roster` is allowed to answer, but only
                    # with SELECTED rows -- an unselected candidate reaching
                    # them is the same recovery by another name.
                    unselected = {fx["people"][k]["id"]
                                  for k in ("candidate", "enrolled",
                                            "offered", "away_sub",
                                            "legacy_sub", "away_legacy_sub")}
                    for route in PRIVATE_SIBLINGS:
                        for query in [""] + [f"?{h}={fx['away']}"
                                             for h in SIDE_HINTS]:
                            status, body = self._get(
                                who["official"], fx, route, query)
                            probes += 1
                            if status != 200:
                                self.assertEqual(status, 403,
                                                 (route, query, body))
                                continue
                            self._assert_absent(
                                body, unselected,
                                f"{label}/official/{route}{query}",
                                "an official recovered candidate or "
                                "substitute state")
                    self.assertGreaterEqual(probes, 100, probes)
                ran.append((label, "pivot_matrix"))
            finally:
                self._close(label, store)
        self._assert_matrix_ran(ran, ["pivot_matrix"])


# ---------------------------------------------------------------------------
# 6. THE OPERATOR IS NOT A CASUALTY. "A fix that narrows the operator is a
#    regression."
# ---------------------------------------------------------------------------
class AnUnscopedOperatorKeepsFullGameAccess(_SiblingHarness,
                                            unittest.TestCase):
    """All four routes, compared against the facade's OWN un-projected read
    (``viewer_role=None``, every in-process caller's default) rather than
    against a hand-written expectation — so this cannot drift into agreeing
    with a narrowed operator."""

    def test_every_route_answers_an_operator_exactly_as_before(self):
        ran = []
        for label, store in self._stores():
            try:
                self._assert_backend(label, store)
                store.clear_all_data()
                fx = self._fixture(store)
                who = self._serve(fx)
                api = fx["api"]

                with self.subTest(backend=label):
                    self.assertEqual(
                        self._ok(who["operator"], fx, "roster"),
                        api.get_roster(fx["gid"]), f"[{label}] /roster")
                    self.assertEqual(
                        self._ok(who["operator"], fx, "substitutes"),
                        api.get_substitutes(fx["gid"]),
                        f"[{label}] /substitutes")
                    self.assertEqual(
                        self._ok(who["operator"], fx, "roster-status"),
                        api.get_roster_status(fx["gid"]),
                        f"[{label}] /roster-status")
                    board = self._ok(who["operator"], fx, "board")
                    direct = api.get_board(fx["gid"])
                    for field in ("notifications", "audit", "audit_count"):
                        self.assertEqual(board[field], direct[field],
                                         f"[{label}] /board {field}")
                    # The whole game's rows really are there. Asserted on the
                    # SIDES present rather than on a player-id subset,
                    # because `_durable` spans seats AND enrollments while
                    # `/roster` carries only seats -- so a subset test would
                    # fail for a reason that has nothing to do with the
                    # operator being narrowed. `None` is the legacy
                    # NULL-attribution seat, which is the sharpest evidence
                    # of all: it is a row NEITHER Coach may receive, and the
                    # operator still does.
                    sides = {r["team_side"]
                             for r in self._ok(who["operator"], fx, "roster")}
                    self.assertIn(fx["home"], sides, sides)
                    self.assertIn(fx["away"], sides, sides)
                    self.assertIn(
                        None, sides,
                        f"[{label}] the operator lost the legacy "
                        "NULL-attribution seats, so this fixture cannot show "
                        "that their read stayed wider than a Coach's")
                ran.append((label, "operator_unchanged"))
            finally:
                self._close(label, store)
        self._assert_matrix_ran(ran, ["operator_unchanged"])


# ---------------------------------------------------------------------------
# 7. F4 — /availability-summary is projected like its four siblings.
# ---------------------------------------------------------------------------
class TheAvailabilityRollupIsProjectedLikeItsSiblings(_SiblingHarness,
                                                      unittest.TestCase):
    """The FIFTH leaf, and the only one that reads a side from the query
    string — so it is the only place the ruling's "Ignore client side hints"
    could actually be defeated, and it was.

    THREE SEPARATE PROPERTIES, three falsifiers, because they are three
    independent defects and a test that proved one could cover for another:

    * an assigned OFFICIAL is REFUSED — the old narrowing named only COACH and
      PLAYER, and this rollup IS per-player availability, the thing
      ``_submitted_lineup_rows`` exists to strip;
    * a Coach/Player's hint is IGNORED, not honoured and not merely refused —
      the four siblings answer a hinted call identically to an un-hinted one,
      and this must too or the family answers "what does ``?team_id=`` do"
      two different ways;
    * the UNSCOPED OPERATOR is byte-for-byte unchanged, proven against the
      facade's own un-projected read rather than a literal.
    """

    def _summary(self, opener, fx, query=""):
        return self._get(opener, fx, "availability-summary", query)

    def test_only_a_full_or_own_side_caller_reads_the_rollup(self):
        ran = []
        for label, store in self._stores():
            try:
                self._assert_backend(label, store)
                store.clear_all_data()
                fx = self._fixture(store)
                who = self._serve(fx)
                # The premise: each side really does owe answers, and the two
                # sides name DIFFERENT people -- otherwise "own side only"
                # could pass on an empty or an identical rollup.
                api = fx["api"]
                home_pool = {r["player_id"] for r in api
                             .get_availability_summary(fx["gid"],
                                                       fx["home"])["players"]}
                away_pool = {r["player_id"] for r in api
                             .get_availability_summary(fx["gid"],
                                                       fx["away"])["players"]}
                self.assertTrue(home_pool and away_pool, (home_pool, away_pool))
                self.assertFalse(home_pool & away_pool,
                                 "fixture: the two pools overlap")

                def check_hint_ignored():
                    for user, team in (("homecoach", fx["home"]),
                                       ("homeplayer", fx["home"]),
                                       ("awaycoach", fx["away"]),
                                       ("awayplayer", fx["away"])):
                        other = (fx["away"] if team == fx["home"]
                                 else fx["home"])
                        status, plain = self._summary(who[user], fx)
                        self.assertEqual(status, 200, (user, plain))
                        self.assertEqual(
                            plain["team_id"], team,
                            f"[{label}] {user} received the OTHER side's "
                            "availability rollup")
                        for hint in SIDE_HINTS:
                            status, hinted = self._summary(
                                who[user], fx, f"?{hint}={other}")
                            self.assertEqual(
                                status, 200,
                                f"[{label}] {user}: ?{hint}= naming the "
                                "opponent must be IGNORED, not refused -- "
                                "every sibling answers a hinted call exactly "
                                f"as an un-hinted one: {hinted!r}")
                            self.assertEqual(
                                hinted, plain,
                                f"[{label}] {user}: ?{hint}= changed the "
                                "availability rollup -- a client hint "
                                "selected a side")
                        # And nothing of the opponent's pool is in it.
                        self._assert_absent(
                            plain, away_pool if team == fx["home"]
                            else home_pool, f"{label}/{user}",
                            "/availability-summary")

                def check_official():
                    for query in [""] + [f"?{h}={side}"
                                         for h in SIDE_HINTS
                                         for side in (fx["home"], fx["away"])]:
                        status, body = self._summary(who["official"], fx,
                                                     query)
                        self.assertNotEqual(
                            status, 200,
                            f"[{label}] an assigned official read the "
                            f"availability rollup with {query!r}: {body!r}")
                        self.assertEqual(status, 403, (query, body))
                        self.assertEqual(body["error"]["code"], "forbidden",
                                         body)
                        # NEVER AN EMPTY OPERATIONAL STATE. The refusal must
                        # not carry a rollup shape at all -- `players: []`
                        # with zero counts asserts "nobody on this team owes
                        # an answer", a claim about the GAME rather than
                        # about this reader.
                        self.assertNotIn("players", body, body)
                        self.assertNotIn("counts", body, body)

                # CAPTURED BEFORE ANY FALSIFIER RUNS. The expectation is the
                # facade's OWN un-projected read rather than a literal, so it
                # cannot drift into agreeing with a narrowed operator -- but
                # that same call goes through the very method the operator
                # falsifier replaces, so recomputing it inside `check_operator`
                # would rewrite the ANSWER KEY along with the code and the
                # falsifier would pass. This is the identical trap
                # `attribution_by_live_membership` fell into.
                unprojected = {team: fx["api"].get_availability_summary(
                    fx["gid"], team) for team in (fx["home"], fx["away"])}

                def check_operator():
                    for team in (fx["home"], fx["away"]):
                        status, served = self._summary(
                            who["operator"], fx, f"?team_id={team}")
                        self.assertEqual(status, 200, served)
                        self.assertEqual(
                            served, unprojected[team],
                            f"[{label}] the operator's rollup was narrowed")

                with self.subTest(backend=label):
                    check_hint_ignored()
                    check_official()
                    check_operator()
                self._require_falsifier_breaks(
                    "availability_official_served", check_official,
                    f"[{label}] the official's availability refusal")
                self._require_falsifier_breaks(
                    "availability_empty_instead_of_forbidden", check_official,
                    f"[{label}] refusal-not-empty on /availability-summary")
                self._require_falsifier_breaks(
                    "availability_hint_honoured", check_hint_ignored,
                    f"[{label}] the ignored side hint")
                self._require_falsifier_breaks(
                    "availability_operator_narrowed", check_operator,
                    f"[{label}] the unchanged operator rollup")
                ran.append((label, "availability_projected"))
            finally:
                self._close(label, store)
        self._assert_matrix_ran(ran, ["availability_projected"])


# ---------------------------------------------------------------------------
# 8. F5 — a NULL-attribution enrollment: not served, not counted, not
#    actionable, by EITHER Coach.
# ---------------------------------------------------------------------------
class ANullOwnerEnrollmentIsServedCountedAndActedOnByNobody(
        _SiblingHarness, unittest.TestCase):
    """"Legacy NULL attribution is omitted, never guessed" — applied to the
    THREE surfaces that were still guessing, in the two directions that make
    the guess visible.

    THE CONTRADICTION THIS CLOSES, stated as the assertion it becomes:
    ``/substitutes`` and ``/substitute-candidates`` are two views of ONE
    resource, so they must name the SAME rows. They did not — the flat list
    keyed on ``enrollment.team_id`` (durable) and the queue on
    ``team_for_game`` (live), so a NULL-owner row was withheld by one and
    served by the other WITH ``can_offer: True``.

    BOTH DIRECTIONS ARE FIXTURED. ``legacy_sub`` is a NULL-owner row whose
    occupant is a live HOME member and ``away_legacy_sub`` one whose occupant
    is a live AWAY member. With only the first, "attributed by live
    membership" and "attributed to HOME" are indistinguishable answers.

    AND THE ACT, NOT ONLY THE READ. ``offer_substitute`` re-resolves the side
    LIVE and then WRITES it into ``sub.team_id`` — so on a row that named no
    side, the transition invented one and made it durable. The refusal is
    asserted together with the row being UNCHANGED in the store, because a
    403 returned after a write would be worse than the 200."""

    def _null_owned(self, fx):
        rows = {s.player_id for s in
                fx["api"].store.substitutes_for_game(fx["gid"])
                if s.team_id is None}
        self.assertTrue(rows, "fixture: no NULL-owner enrollments")
        return rows

    def _queue(self, opener, fx):
        status, body = self._get(opener, fx, "substitute-candidates")
        self.assertEqual(status, 200, body)
        return body["candidates"]

    def _durable_enrolled(self, fx, team_id) -> int:
        """The ENROLLED rows this game DURABLY attributes to ``team_id`` whose
        occupant is still a live member of it — the number
        ``substitutes_enrolled`` must report, derived from the two authorities
        rather than from a literal."""
        api = fx["api"]
        game = api.store.get_game(fx["gid"])
        roster = RosterService(api.store)
        n = 0
        for sub in api.store.substitutes_for_game(fx["gid"]):
            if sub.team_id is None or sub.team_id != team_id:
                continue
            if sub.status.value != "enrolled":
                continue
            player = api.store.get_player(sub.player_id)
            if player is None:
                continue
            if roster.team_for_game(game, player) == team_id:
                n += 1
        return n

    def test_neither_coach_reads_counts_or_acts_on_an_unattributed_row(self):
        ran = []
        for label, store in self._stores():
            try:
                self._assert_backend(label, store)
                store.clear_all_data()
                fx = self._fixture(store)
                who = self._serve(fx)
                nameless = self._null_owned(fx)
                # The premise the whole class rests on: at least one
                # NULL-owner row's occupant IS a live member of each side, so
                # a live-membership rule really would serve it to that Coach.
                api, roster = fx["api"], RosterService(store)
                game = store.get_game(fx["gid"])
                live_sides = {
                    roster.team_for_game(game, store.get_player(pid))
                    for pid in nameless}
                self.assertIn(fx["home"], live_sides, live_sides)
                self.assertIn(fx["away"], live_sides, live_sides)

                def check_reads():
                    for user, team in (("homecoach", fx["home"]),
                                       ("awaycoach", fx["away"])):
                        queue = {c["player_id"] for c in
                                 self._queue(who[user], fx)}
                        self.assertFalse(
                            queue & nameless,
                            f"[{label}] {user}'s outreach queue carries a "
                            "NULL-owner enrollment on a GUESSED side: "
                            f"{sorted(queue & nameless)}")
                        # ONE RESOURCE, ONE ANSWER: the queue and the flat
                        # list must name the same rows.
                        flat = {r["player_id"] for r in
                                self._ok(who[user], fx, "substitutes")}
                        self.assertEqual(
                            queue, flat,
                            f"[{label}] {user}: /substitute-candidates and "
                            "/substitutes disagree about which enrollments "
                            "are this side's -- two authorities, one "
                            "resource")

                def check_counts():
                    for user, team in (("homecoach", fx["home"]),
                                       ("awaycoach", fx["away"])):
                        body = self._ok(who[user], fx, "roster-status")
                        self.assertEqual(body["team_id"], team, body)
                        self.assertEqual(
                            body["substitutes_enrolled"],
                            self._durable_enrolled(fx, team),
                            f"[{label}] {user}'s substitutes_enrolled counts "
                            "a row this game cannot durably attribute to "
                            "their side")

                def check_acts():
                    for user, team in (("homecoach", fx["home"]),
                                       ("awaycoach", fx["away"])):
                        for pid in sorted(nameless):
                            before = _enrollment(fx, pid)
                            for path, body in (
                                    (f"substitutes/{pid}/offer", {}),
                                    (f"substitutes/{pid}/add-to-roster", {}),
                                    ("substitutes/withdraw",
                                     {"player_id": pid})):
                                status, out = self._req(
                                    who[user], "POST",
                                    f"/api/games/{fx['gid']}/{path}", body)
                                self.assertEqual(
                                    status, 403,
                                    f"[{label}] {user} acted on an "
                                    f"enrollment whose admitting side is "
                                    f"unknown via {path}: {out!r}")
                            after = _enrollment(fx, pid)
                            self.assertEqual(
                                (before.status, before.team_id),
                                (after.status, after.team_id),
                                f"[{label}] {user}'s refused action still "
                                f"wrote to {pid}'s enrollment -- a 403 after "
                                "a write is worse than the 200")
                            self.assertIsNone(
                                after.team_id,
                                f"[{label}] {pid}'s admitting side was MINTED "
                                "from live membership by a refused action")

                with self.subTest(backend=label):
                    check_reads()
                    check_counts()
                    check_acts()
                    # THE NARROWINGS ARE NARROWINGS, not blanket breakage: a
                    # DURABLY OWNED row is still served, still counted, and
                    # still actionable by its own Coach.
                    owned = {c["player_id"]
                             for c in self._queue(who["homecoach"], fx)}
                    self.assertTrue(
                        owned, f"[{label}] HOME's outreach queue is empty, so "
                        "the omission of the NULL row proves nothing")
                    self.assertGreater(
                        self._durable_enrolled(fx, fx["home"]), 0,
                        f"[{label}] fixture: HOME owns no ENROLLED row")
                    target = sorted(
                        c["player_id"] for c in self._queue(who["homecoach"],
                                                            fx)
                        if c["status"] == "enrolled")[0]
                    status, out = self._req(
                        who["homecoach"], "POST",
                        f"/api/games/{fx['gid']}/substitutes/{target}/offer",
                        {})
                    self.assertEqual(
                        status, 200,
                        f"[{label}] the HOME Coach can no longer offer a row "
                        f"their own side durably owns: {out!r}")
                    # AND THE OPERATOR KEEPS THE REPAIR PATH. An unscoped
                    # caller claims no side, so they are not guessing -- and
                    # they are the only route by which a legacy row can be
                    # resolved at all.
                    victim = sorted(nameless)[0]
                    status, out = self._req(
                        who["operator"], "POST",
                        f"/api/games/{fx['gid']}/substitutes/withdraw",
                        {"player_id": victim})
                    self.assertEqual(
                        status, 200,
                        f"[{label}] the unscoped operator lost the only path "
                        f"by which a legacy row can be cleared: {out!r}")

                self._require_falsifier_breaks(
                    "candidates_by_live_membership", check_reads,
                    f"[{label}] durable attribution on the outreach queue")
                self._require_falsifier_breaks(
                    "subs_counted_by_live_membership", check_counts,
                    f"[{label}] durable attribution in substitutes_enrolled")
                self._require_falsifier_breaks(
                    "null_owner_actionable", check_acts,
                    f"[{label}] the unattributed-enrollment act refusal")
                ran.append((label, "null_owner_nowhere"))
            finally:
                self._close(label, store)
        self._assert_matrix_ran(ran, ["null_owner_nowhere"])


def _enrollment(fx, player_id):
    return fx["api"].store.substitute_for_player(fx["gid"], player_id)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
