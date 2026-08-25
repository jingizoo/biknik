"""#205 — THE PRIMARY PROTECTION: a behavioural sweep of the WHOLE
authenticated GET surface, in two worlds, on all three backends.

WHY THIS IS THE PRIMARY PROTECTION AND THE STATIC SCANNER IS SUPPLEMENTAL.
Five rounds of this blocker found five leaks, each by a human enumerating
routes. Round 5 built an AST gate (``services/side_provenance.py``) and the
gate BLESSED the fifth leak under an exemption: ``GET /api/me/player-home``
took no side parameter, so the scanner's condition ("the function accepts no
caller-supplied side") was satisfied while the side it derived was the
opponent's. A scanner reasons about how a value is SPELLED at a call site.
This file reasons about what actually comes out of the server, which is the
claim the blocker is really making, so this is the asset that carries it and
``test_side_provenance_guard.py`` is the supplement.

THE TWO ORACLES
===============
**1. Identity leak.** For every response, no durably-attributed player id of a
side the caller is NOT entitled to may appear anywhere in the serialized body.
WORD-BOUNDARY matched, never a substring: ``player_1`` is a prefix of
``player_12``, and a plain ``in`` test reported the away Coach's own
``player_12`` as a leak of the home side's ``player_1`` — a false positive
that made an earlier version of this matrix untrustworthy in BOTH directions.

**2. Non-interference (two worlds).** Perturb ONLY one side's private per-side
state, re-sweep the entire surface, and diff. Any byte that moves in a
response to a caller not entitled to that side is, by definition, a FUNCTION
of that side's private state. This catches names, counts AND DERIVED ENUMS
alike, and it needs no prior guess about which field might be leaking — which
is exactly why it found the fifth leak when route-by-route reading did not:
``next_game.team_status`` is a relabelled enum, not an identity.

BOTH SIDES ARE PERTURBED. The round that found the fifth leak perturbed HOME
only; the symmetric AWAY perturbation gave zero diffs, and that was a FIXTURE
ARTIFACT — every Mover in the fixture had a pointer naming HOME, so no reader
existed whose stale pointer named AWAY. This fixture adds one
(``home_via_away_pointer``), so the mirror can genuinely fail.

FAIL-CLOSED ON A NEW ROUTE
==========================
The route inventory is taken from ``web/route_registry.REGISTRY``, not from a
hand-written list here. Every GET spec whose recorded ``auth`` is anything
other than ``"none"`` must be either EXERCISED (it has a subject binding in
:data:`ROUTE_SUBJECTS`) or carry a typed, documented entry in
:data:`NOT_SWEPT`. A route that is in neither is an ERROR naming it — so a new
authenticated route cannot be a silent gap in this sweep, which is the
property the fifth leak's route (outside the ``/api/games/{id}/…`` family
entirely) needed and did not have.

TYPED DESIGN CLASSIFICATIONS, NOT ACCUMULATING EXEMPTIONS
=========================================================
Three callers are entitled to BOTH sides, and each is a named class with its
own dedicated assertion rather than a line in a suppression list — see
:data:`ENTITLEMENT` and :class:`TheDesignClassificationsAreStillTrue`. If one
of them stops matching its class, that test fails; it does not silently widen
this sweep.

RUNTIME, MEASURED AND STATED. Four worlds (a fresh base and a perturbed world
for each of the two sides) x every authenticated GET route x 8 principals x 4
side-hint variants: 1,984 real HTTP requests per world, 7,936 per backend,
~24k for the module. Measured on this machine: **2.6 s Memory, 3.0 s SQLite,
10.3 s real PostgreSQL** for the main property, ~102 s for the module
including the guard and Player Home files it is usually run beside. It is a
whole-surface behavioural sweep and it is cheaper than it sounds, because
every request is a loopback call into an in-process server.

``_assert_matrix_ran`` fails a loop that silently covered fewer backends or
cases than were configured: A SKIP IS NOT A PASS.
"""

import contextlib
import json
import re
import time
import unittest

from helpers import BACKEND  # noqa: F401
from test_lineup_side_projection import _ProjectionHarness
from test_overview_schedule_side import _OverviewHarness
from test_substitute_membership_cutover import ADMIN

from hockey_scheduler.api.service import ApiService as _ApiService
from hockey_scheduler.services.roster_service import RosterService
from hockey_scheduler.web import route_registry
from hockey_scheduler.web.auth import DEMO_PASSWORD, DEMO_USERS

#: The eight principals. Real sessions, real cookies, one per row.
PRINCIPALS = ("homecoach", "awaycoach", "homeplayer", "awayplayer",
              "official", "guardian", "thirdcoach", "operator")

#: WHAT THE CLIENT CAN SAY. The assertion is not "these particular parameter
#: names are ignored" but "nothing the client can say selects a side", so the
#: expected answer for every variant is byte equality with the UN-hinted
#: response — asserted in every world, not only the unperturbed one.
HINTS = ("none", "team_id_home", "team_id_away", "side_away")

# ---------------------------------------------------------------------------
# TYPED DESIGN CLASSIFICATIONS — who is entitled to which sides, and WHY.
#
# Not a suppression list. Each class is a claim about the product, each claim
# has a dedicated test in `TheDesignClassificationsAreStillTrue`, and a
# principal whose entitlement stops matching its class fails that test rather
# than quietly widening this sweep.
# ---------------------------------------------------------------------------
SCOPED_TO_ONE_SIDE = "scoped_to_one_side"
#: A Coach bound to a team, or a Player whose game-scoped membership resolves
#: one. Entitled to exactly that side and to nothing of the other.

IN_NEITHER_SIDE = "in_neither_side"
#: A Coach of a team that plays in NEITHER game. Entitled to NO side of
#: either — the sharpest row in the matrix, and the one that showed round 4's
#: leak was a WIDER gate than the family it sat beside.

GUARDIAN_OF_A_JUNIOR = "guardian_of_a_junior"
#: A guardian verified for one junior inherits exactly that junior's own
#: resolved side, because `get_guardian_home` returns the junior's Player Home
#: payload. Entitled to the junior's side and to nothing else.

OFFICIAL_TWO_SIDED_BY_DESIGN = "official_two_sided_by_design"
#: An assigned official's submitted-lineup projection IS two-sided — see
#: `services/lineup_visibility.route_audience`. Entitled to both, so oracle 1
#: and oracle 2 cannot narrow them; what CAN be asserted about them is pinned
#: separately (they are refused the workflow leaves, and withheld on the
#: Dashboard row), and is, in `test_private_game_sibling_routes.py` and
#: `test_overview_schedule_side.py`.

OPERATOR_UNSCOPED_BY_DESIGN = "operator_unscoped_by_design"
#: An unscoped operator may read either side; narrowing them would be its own
#: regression. Entitled to both.

#: THE THREE ROUTES WHERE A CLIENT HINT SELECTS A SIDE, and only for a caller
#: ALREADY ENTITLED TO BOTH.
#:
#: These are the family's three leaves that accept a ``?team_id=``. Each
#: adjudicates first: a Coach or Player reaches ``OWN_SIDE`` and the hint is
#: ignored outright (the sweep proves that for every scoped principal on every
#: route, including these), and an assigned official is REFUSED all three
#: (asserted in :class:`TheDesignClassificationsAreStillTrue`). What is left is
#: the ``FULL`` branch — an unscoped operator — where the hint chooses which
#: side to answer for. ``side_provenance.EXEMPTIONS`` records both shapes as
#: OPERATOR_DEFAULT: "an unscoped operator may read either side, and narrowing
#: them would be its own regression".
#:
#: WHY THIS IS NOT A HOLE IN THE SWEEP: a hint cannot WIDEN what a caller may
#: read, and for someone entitled to both sides there is nothing to widen to.
#: For everyone else the hint must be inert, and
#: :meth:`_SweepHarness._assert_hints_are_inert` still requires that on every
#: route including these three.
#:
#: MEASURED AND STATED RATHER THAN ASSUMED: ``/availability-summary`` bounds
#: the hint to the game's two sides (a third team's id is refused), while the
#: two workflow leaves do NOT — an unscoped operator naming any team id gets
#: that team's pool. That is pre-existing ``_workflow_side`` FULL-branch
#: behaviour, it is reachable only by a caller who may read every team
#: anyway, and it is outside this round's ruling; it is recorded here so the
#: difference between the three routes is visible rather than folded away.
HINT_MAY_SELECT_FOR_A_BOTH_SIDED_CALLER = {
    "get_games_id_availability_summary",
    "get_games_id_substitute_candidates",
    "get_games_id_substitute_addable",
}

#: ROUTES WHERE ``?team_id=`` IS A COLLECTION FILTER, NOT A SIDE SELECTOR.
#:
#: ``GET /api/players?team_id=`` narrows a roster LIST to one team. It says
#: nothing about a game and selects no side of one; the parameter happens to
#: share a name with the family's hint, which is exactly why it has to be
#: classified rather than silently tolerated.
#:
#: CONDITION, machine-checked in :class:`TheDesignClassificationsAreStillTrue`:
#: the route must be recorded ``auth="operator_only"`` in
#: ``web/route_registry.py``. An operator-only route cannot widen anyone's
#: entitlement with a filter, and if that route's auth is ever loosened this
#: classification breaks instead of quietly covering a new caller.
TEAM_ID_IS_A_COLLECTION_FILTER = {
    "get_players",
}


class _Sweep:
    """One world's measurement: ``{(principal, route, path, hint): result}``."""

    def __init__(self, rows, elapsed, requests):
        self.rows = rows
        self.elapsed = elapsed
        self.requests = requests

    def diff(self, other):
        """Keys whose ``(status, canonical body)`` differ between worlds.

        The two worlds must have swept the SAME keys — a diff computed over
        different route sets would report a coverage change as a leak (or,
        worse, hide one)."""
        assert set(self.rows) == set(other.rows), (
            "the two worlds swept different (principal, route, path, hint) "
            "sets, so their diff means nothing: "
            f"{sorted(set(self.rows) ^ set(other.rows))[:8]}")
        return sorted(k for k, v in self.rows.items() if other.rows[k] != v)


class _SweepHarness(_OverviewHarness):
    """``_OverviewHarness``'s tri-store fixture — two games with the sides
    SWAPPED, the sides answering DIFFERENT roster statuses — plus the AWAY
    pointer the previous round's fixture lacked, and a guardian verified for
    the MOVER junior.

    WHY ``_serve`` IS NOT INHERITED. ``_OverviewHarness`` verifies its
    guardian for ``seated``, whose permanent pointer names a team in NEITHER
    game. That junior's Player Home was empty under the fifth leak, so a
    guardian linked to them cannot express the guardian half of it at all.
    Here the guardian is verified for the Mover instead, which is the shape
    that reproduced.
    """

    #: Fields whose value is a function of WHEN the request ran rather than of
    #: any side's state. Removed before diffing — each one named, so the
    #: normalisation is reviewable and cannot quietly grow to cover a real
    #: difference. `test_the_sweep_is_stable` is what proves the list is
    #: COMPLETE: two consecutive sweeps of the same world must agree exactly.
    VOLATILE_KEYS = ("expires_at", "generated_at", "server_time", "now",
                     "last_seen_at", "issued_at")

    # -- fixture ----------------------------------------------------------
    def _fixture(self, store):
        fx = super()._fixture(store)
        # THE MIRROR the previous round's fixture lacked: a reader whose
        # PERMANENT POINTER names AWAY while their membership names HOME.
        # Without it, "perturbing AWAY changed nothing" is a fact about the
        # fixture, not about the code.
        fx["people"]["home_via_away_pointer"] = self._mover(
            fx, "Home Via Away Pointer", fx["away"], fx["home"])
        return fx

    def _serve(self, fx):
        # Deliberately skips `_OverviewHarness._serve` — see the class
        # docstring. The six base principals come from `_ProjectionHarness`;
        # the two the family refuses outright are added here.
        who = _ProjectionHarness._serve(self, fx)
        api = fx["api"]
        officials = api.store.all_officials()
        assert len(officials) == 1, officials
        fx["official_id"] = officials[0].id
        extra = {
            "thirdcoach": (DEMO_USERS["coach"], {"team_id": fx["third"]}),
            "guardian": (DEMO_USERS["guardian"], {}),
        }
        for user, (role, scope) in extra.items():
            acct = api.accounts.create_account(
                user, DEMO_PASSWORD, role, scope=scope, actor_id="test_seed")
            if user == "guardian":
                # Verified for the MOVER (pointer HOME, membership AWAY), so
                # the guardian's entitled side is AWAY and a HOME
                # perturbation reaching them is a leak.
                link = api.create_guardian_link(
                    acct.id, fx["people"]["awayside"]["id"], actor_id=ADMIN)
                assert "error" not in link, link
                verified = api.verify_guardian_link(
                    link["id"], "signed_form", actor_id=ADMIN)
                assert "error" not in verified, verified
                fx["guardian_junior_id"] = fx["people"]["awayside"]["id"]
            who[user] = self._sign_in(user)
        # Every principal's account id, taken from the store rather than from
        # the two created above — `get_accounts/{}/sessions` needs a real
        # subject and the six base principals are created by
        # `_ProjectionHarness._serve`.
        fx["account_ids"] = {a.username: a.id
                             for a in api.accounts.list_accounts()}
        assert set(PRINCIPALS) <= set(fx["account_ids"]), fx["account_ids"]
        # Every session selects the Program/Season/League explicitly: the
        # context-scoped reads fail CLOSED to an empty payload otherwise, and
        # an empty payload would make this sweep pass vacuously.
        for user, opener in who.items():
            status, body = self._req(opener, "POST", "/api/context", {
                "program_id": fx["program"]["id"],
                "season_id": fx["s1"]["id"],
                "league_id": fx["league"]["id"]})
            self.assertEqual(status, 200, (user, body))
        return who

    # -- the entitlement matrix -------------------------------------------
    def _entitlement(self, fx):
        """``{principal: (class, frozenset of team ids they may read)}``."""
        return {
            "homecoach": (SCOPED_TO_ONE_SIDE, frozenset({fx["home"]})),
            "awaycoach": (SCOPED_TO_ONE_SIDE, frozenset({fx["away"]})),
            # Both Players are MOVERS: their entitled side is the one their
            # game-scoped MEMBERSHIP names, never their permanent pointer.
            "homeplayer": (SCOPED_TO_ONE_SIDE, frozenset({fx["home"]})),
            "awayplayer": (SCOPED_TO_ONE_SIDE, frozenset({fx["away"]})),
            "thirdcoach": (IN_NEITHER_SIDE, frozenset()),
            "guardian": (GUARDIAN_OF_A_JUNIOR, frozenset({fx["away"]})),
            "official": (OFFICIAL_TWO_SIDED_BY_DESIGN,
                         frozenset({fx["home"], fx["away"]})),
            "operator": (OPERATOR_UNSCOPED_BY_DESIGN,
                         frozenset({fx["home"], fx["away"]})),
        }

    # -- the route inventory ----------------------------------------------
    #: A stable id that names no row. Used where the fixture has no instance
    #: of that subject; the route is still swept (its refusal or its empty
    #: answer must be a function of nothing private either).
    ABSENT = "sweep_absent_subject"

    def _route_subjects(self, fx):
        """``{route name: [tuple of path arguments, …]}`` — every
        authenticated GET route in the registry that this sweep exercises.

        Keyed by route NAME rather than by path, so a route whose pattern is
        edited keeps its binding and a route that is ADDED has none — which is
        what makes :meth:`_assert_inventory_is_closed` fail closed."""
        games = [(fx["gid"],), (fx["gid2"],)]
        junior = fx["guardian_junior_id"]
        return {
            # -- no path arguments --------------------------------------
            **{name: [()] for name in (
                "get_auth_me", "get_me_assignments", "get_me_player_home",
                "get_context", "get_context_options", "get_demo_overview",
                "get_notifications", "get_calendar_feeds",
                "get_notifications_preferences", "get_me_guardian_home",
                "get_v2_setup_overview", "get_v2_setup_progress",
                "get_accounts", "get_guardians_links",
                "get_import_hierarchy_codes", "get_notifications_contacts",
                "get_notifications_deliveries",
                "get_notifications_device_tokens", "get_onboarding_status",
                "get_players", "get_reschedule_pending",
                "get_scheduler_drafts", "get_scheduler_scenarios",
                "get_setup_hierarchy", "get_setup_scheduling_policy",
                "get_v2_onboarding_status", "get_v2_setup_hierarchy")},
            # -- the private-game family, on BOTH games ------------------
            **{name: list(games) for name in (
                "get_games_id_availability_summary", "get_games_id_board",
                "get_games_id_lineups", "get_games_id_officials",
                "get_games_id_reschedule", "get_games_id_roster",
                "get_games_id_roster_status",
                "get_games_id_substitute_addable",
                "get_games_id_substitute_candidates",
                "get_games_id_substitutes")},
            # -- one real subject each ----------------------------------
            "get_accounts_id_sessions": [(fx["account_ids"]["operator"],)],
            "get_officials_id_availability": [(fx["official_id"],)],
            "get_me_substitute_opportunities_id": list(games),
            "get_me_guardian_id_substitute_opportunities_id": [
                (junior, fx["gid"]), (junior, fx["gid2"])],
            "get_setup_leagues_id_teams": [(fx["league"]["id"],)],
            "get_setup_seasons_id_team_registrations": [(fx["s1"]["id"],)],
            "get_v2_setup_programs_id_teams": [(fx["program"]["id"],)],
            "get_v2_setup_seasons_id_team_registrations": [(fx["s1"]["id"],)],
            "get_v2_setup_seasons_id_venue_access": [(fx["s1"]["id"],)],
            "get_v2_setup_seasons_id_venue_candidates": [(fx["s1"]["id"],)],
            "get_standings_league_season_id_id": [
                (fx["league"]["id"], fx["s1"]["id"])],
            # -- swept against a subject the fixture has no instance of --
            # Both are still real dispatch branches, and their answer must be
            # a function of nothing private either.
            "get_standings_id": [(self.ABSENT,)],
            "get_scheduler_scenarios_id": [(self.ABSENT,)],
        }

    def _authenticated_get_specs(self):
        """Every GET route spec the registry records as needing identity."""
        return [s for s in route_registry.REGISTRY
                if s.method == "GET" and s.kind == "route"
                and s.auth not in ("none", route_registry.UNCLASSIFIED)]

    def _assert_inventory_is_closed(self, fx):
        """FAIL CLOSED ON A NEW ROUTE. Every authenticated GET spec is either
        swept or carries a typed, documented reason not to be."""
        specs = self._authenticated_get_specs()
        self.assertGreater(len(specs), 40,
                           "the registry yielded almost no authenticated GET "
                           "routes, so this sweep would pass vacuously")
        subjects = self._route_subjects(fx)
        unaccounted = sorted(
            s.name for s in specs
            if s.name not in subjects and s.name not in NOT_SWEPT)
        self.assertEqual(
            [], unaccounted,
            "NEW AUTHENTICATED ROUTE(S) NOT COVERED BY THE SWEEP: "
            f"{unaccounted}. Every authenticated GET route in "
            "route_registry.REGISTRY must be exercised here — add a subject "
            "binding to _route_subjects — or carry a typed entry in "
            "NOT_SWEPT saying why it cannot be. A route in neither is a "
            "silent gap, which is exactly what let the fifth leak ship on a "
            "route outside the /api/games/{id}/… family.")
        # …and the bindings cannot rot in the other direction either.
        live = {s.name for s in specs}
        self.assertEqual(
            [], sorted(set(subjects) - live),
            "a _route_subjects binding names a route the registry no longer "
            "records as an authenticated GET; delete it or re-classify it")
        self.assertEqual(
            [], sorted(set(NOT_SWEPT) - live),
            "a NOT_SWEPT entry names a route that no longer exists")
        # Only `{}` placeholders are understood here; a `{w}`/`{*}` template
        # would be filled wrong rather than reported.
        for spec in specs:
            if spec.name in subjects:
                self.assertEqual(
                    spec.template.count("{}"),
                    spec.template.count("{"),
                    f"{spec.name}: this sweep only fills `{{}}` segments")
        return specs, subjects

    # -- the sweep ---------------------------------------------------------
    def _canonical(self, body):
        """The body with the documented time-varying keys removed."""
        if isinstance(body, dict):
            return {k: self._canonical(v) for k, v in body.items()
                    if k not in self.VOLATILE_KEYS}
        if isinstance(body, list):
            return [self._canonical(v) for v in body]
        return body

    def _hint_query(self, fx, hint):
        return {"none": "",
                "team_id_home": f"?team_id={fx['home']}",
                "team_id_away": f"?team_id={fx['away']}",
                "side_away": f"?side={fx['away']}"}[hint]

    def _sweep(self, who, fx, specs, subjects):
        """One world: every principal x every route x every hint."""
        started = time.time()
        rows, requests = {}, 0
        for spec in specs:
            if spec.name not in subjects:
                continue
            for args in subjects[spec.name]:
                path = spec.template
                for arg in args:
                    path = path.replace("{}", arg, 1)
                for principal in PRINCIPALS:
                    for hint in HINTS:
                        status, body = self._req(
                            who[principal], "GET",
                            path + self._hint_query(fx, hint))
                        requests += 1
                        rows[(principal, spec.name, path, hint)] = (
                            status, self._canonical(body))
        return _Sweep(rows, time.time() - started, requests)

    # -- oracle 1: identity ------------------------------------------------
    def _durable_ids(self, fx, team_id):
        """The player ids this game can DURABLY attribute to ``team_id`` —
        stored ``GameRosterEntry.team_side`` / ``SubstituteEnrollment.team_id``
        across both games, never live membership and never the pointer."""
        out = set()
        for gid in (fx["gid"], fx["gid2"]):
            sides = RosterService(fx["api"].store).durable_game_sides(gid)
            out |= {pid for pid, side in sides.items() if side == team_id}
        return out

    def _assert_no_foreign_ids(self, sweep, fx, label):
        """ORACLE 1. No durably-attributed id of a side the caller is not
        entitled to, anywhere in any body, word-boundary matched."""
        entitlement = self._entitlement(fx)
        forbidden = {}
        for principal, (_klass, teams) in entitlement.items():
            ids = set()
            for team in (fx["home"], fx["away"]):
                if team not in teams:
                    ids |= self._durable_ids(fx, team)
            forbidden[principal] = ids
        # The premise: at least one principal must have something real to
        # fail to reach, or this oracle asserts nothing.
        self.assertTrue(forbidden["thirdcoach"],
                        f"[{label}] no durably attributed ids exist, so the "
                        f"identity oracle is vacuous")
        for (principal, route, path, hint), (_status, body) in sweep.rows.items():
            blob = json.dumps(body, sort_keys=True, default=str)
            for pid in sorted(forbidden[principal]):
                self.assertIsNone(
                    re.search(rf"\b{re.escape(pid)}\b", blob),
                    f"[{label}] {principal} received {pid} — a player "
                    f"durably attributed to a side they are not entitled to "
                    f"— from GET {path} (hint={hint}, route={route})")

    # -- oracle 2: non-interference ---------------------------------------
    @contextlib.contextmanager
    def _perturbed(self, fx, team_id, game_id):
        """Change ONE side's private per-side state in ONE game, and nothing
        else — and assert it really moved that side's operational enum, so
        "nothing changed elsewhere" is not a vacuous observation.

        The direction is chosen from the side's own current state: a side that
        HAS active enrollments has them withdrawn, a side that has none gets
        one."""
        api = fx["api"]
        before = api.roster.compute_roster_status(
            game_id, team_id).status.value
        active = [s for s in api.store.substitutes_for_game(game_id)
                  if s.team_id == team_id and s.status.is_active_enrollment]
        if active:
            def do():
                for sub in active:
                    out = api.withdraw_substitute(game_id, sub.player_id,
                                                  actor_id=ADMIN)
                    assert "error" not in out, out

            def undo():
                for sub in active:
                    out = api.enroll_substitute(game_id, sub.player_id,
                                                actor_id=ADMIN)
                    assert "error" not in out, out
        else:
            fresh = self._mover(
                fx, f"Sweep Perturber {team_id} {game_id}",
                fx["home"] if team_id == fx["away"] else fx["away"], team_id)

            def do():
                out = api.enroll_substitute(game_id, fresh["id"],
                                            actor_id=ADMIN)
                assert "error" not in out, out

            def undo():
                out = api.withdraw_substitute(game_id, fresh["id"],
                                              actor_id=ADMIN)
                assert "error" not in out, out
        do()
        after = api.roster.compute_roster_status(game_id, team_id).status.value
        assert before != after, (
            f"perturbing {team_id} in {game_id} did not move that side's own "
            f"private per-side enum ({before!r}); the two worlds would be "
            f"indistinguishable and every 'nothing changed' below vacuous")
        try:
            yield
        finally:
            undo()

    def _assert_non_interference(self, base, perturbed, fx, team_id, label):
        """ORACLE 2. Every diff must belong to a caller ENTITLED to the side
        that was perturbed. Anything else is a response that is a function of
        that side's private state."""
        entitlement = self._entitlement(fx)
        offenders, entitled_moved = [], set()
        for key in base.diff(perturbed):
            principal = key[0]
            if team_id in entitlement[principal][1]:
                entitled_moved.add(principal)
                continue
            offenders.append((key, base.rows[key], perturbed.rows[key]))
        self.assertEqual(
            [], [o[0] for o in offenders],
            f"[{label}] PRIVATE STATE OF {team_id} REACHED A CALLER NOT "
            f"ENTITLED TO IT. Each row below is (principal, route, path, "
            f"hint) whose response changed when ONLY {team_id}'s private "
            f"per-side state changed, so that response is a function of it:\n"
            + "\n".join(
                f"  {key}\n     before: {json.dumps(b, sort_keys=True, default=str)[:400]}"
                f"\n     after:  {json.dumps(a, sort_keys=True, default=str)[:400]}"
                for key, b, a in offenders[:8]))
        # THE PREMISE, again as an assertion: somebody entitled to that side
        # MUST have moved, or the perturbation never reached the surface and
        # "nobody else moved" proves nothing.
        self.assertTrue(
            entitled_moved,
            f"[{label}] perturbing {team_id} changed NO response at all, so "
            f"this world is indistinguishable from the base one and the "
            f"non-interference assertion is vacuous")

    # -- hint invariance ---------------------------------------------------
    def _assert_hints_are_inert(self, sweep, fx, label):
        """Nothing the client can SAY widens what a caller reads — asserted
        in EVERY world, which is what lets the two-world diff be read on the
        whole hint matrix rather than only on the un-hinted variant.

        The ONE documented exception is a caller entitled to BOTH sides on
        :data:`HINT_MAY_SELECT_FOR_A_BOTH_SIDED_CALLER`, where selecting
        between two sides the caller may already read widens nothing. Every
        other principal, and every other route, must answer identically with
        and without the hint."""
        entitlement = self._entitlement(fx)
        both = frozenset({fx["home"], fx["away"]})
        for (principal, route, path, hint), value in sweep.rows.items():
            if hint == "none":
                continue
            if (entitlement[principal][1] == both
                    and route in HINT_MAY_SELECT_FOR_A_BOTH_SIDED_CALLER):
                continue
            if route in TEAM_ID_IS_A_COLLECTION_FILTER:
                continue
            plain = sweep.rows[(principal, route, path, "none")]
            self.assertEqual(
                value, plain,
                f"[{label}] {principal}: GET {path} answered DIFFERENTLY "
                f"with the client hint {hint!r} than without it, so a query "
                f"parameter is selecting what this caller reads (route "
                f"{route})")


# ---------------------------------------------------------------------------
# ROUTES DELIBERATELY NOT SWEPT — typed, documented, and CI-checked against
# the registry (an entry naming a dead route is an error).
#
# Empty, and that is the strongest state it can be in: every authenticated
# GET route in the registry is exercised. An entry here would have to say why
# a route cannot be driven, and would be a hole a future leak could sit in.
# ---------------------------------------------------------------------------
NOT_SWEPT = {}


# ---------------------------------------------------------------------------
# 1. THE INVENTORY IS CLOSED, AND THE SWEEP IS STABLE.
# ---------------------------------------------------------------------------
class TheSweepCoversEveryAuthenticatedRoute(_SweepHarness, unittest.TestCase):
    """Before any oracle means anything, two things must be true: the sweep
    covers every authenticated route the registry knows about, and a response
    is a function of the world rather than of the clock."""

    def test_a_new_authenticated_route_fails_this_test(self):
        store = None
        for label, store in self._stores():
            try:
                self._assert_backend(label, store)
                store.clear_all_data()
                fx = self._fixture(store)
                self._serve(fx)
                specs, subjects = self._assert_inventory_is_closed(fx)
                covered = sum(len(subjects[s.name]) for s in specs
                              if s.name in subjects)
                self.assertGreater(
                    covered, 50,
                    "the sweep resolved almost no concrete paths")
                self.assertEqual({}, NOT_SWEPT,
                                 "NOT_SWEPT was empty when this sweep "
                                 "shipped; an entry is a hole in the primary "
                                 "protection and needs a reason")
            finally:
                self._close(label, store)
            return   # inventory is source-level: one backend proves it

    def test_the_sweep_is_stable(self):
        """Two consecutive sweeps of the SAME world must agree exactly, or
        every diff below is noise. This is also what proves VOLATILE_KEYS is
        complete rather than merely plausible."""
        for label, store in self._stores():
            try:
                self._assert_backend(label, store)
                store.clear_all_data()
                fx = self._fixture(store)
                who = self._serve(fx)
                specs, subjects = self._assert_inventory_is_closed(fx)
                first = self._sweep(who, fx, specs, subjects)
                second = self._sweep(who, fx, specs, subjects)
                self.assertEqual(
                    [], first.diff(second),
                    "the same world measured twice disagreed, so a two-world "
                    "diff cannot distinguish a leak from noise: "
                    + str(first.diff(second)[:10]))
                print(f"\n[SIDE SWEEP] {label}: {first.requests} requests "
                      f"in {first.elapsed:.1f}s")
            finally:
                self._close(label, store)
            return   # stability is per-request behaviour, not per-backend


# ---------------------------------------------------------------------------
# 2. THE TWO ORACLES, OVER THE WHOLE SURFACE, ON EVERY BACKEND.
# ---------------------------------------------------------------------------
class NoAuthenticatedRouteLeaksTheOtherSide(_SweepHarness, unittest.TestCase):
    """THE ASSET. Three worlds, both oracles, both perturbation directions,
    every authenticated GET route, eight principals, four hint variants,
    three backends."""

    CASES = ["identity_oracle_on_the_whole_surface",
             "hints_are_inert_in_every_world",
             "perturbing_home_reaches_only_home_entitled_callers",
             "perturbing_away_reaches_only_away_entitled_callers"]

    def test_no_side_private_state_reaches_a_caller_without_entitlement(self):
        ran = []
        for label, store in self._stores():
            try:
                self._assert_backend(label, store)
                store.clear_all_data()
                fx = self._fixture(store)
                who = self._serve(fx)
                specs, subjects = self._assert_inventory_is_closed(fx)
                with self.subTest(backend=label):
                    total = 0.0
                    for team, case in ((fx["home"], "home"),
                                       (fx["away"], "away")):
                        # THE BASE IS MEASURED FRESH FOR EACH PHASE, not once
                        # for both. A perturbation is not byte-reversible:
                        # `/board` serves the game's AUDIT STREAM, which is
                        # append-only, so undoing an enrolment leaves two more
                        # audit rows than it found. Comparing phase 2 against
                        # a base taken before phase 1 would report that
                        # bookkeeping as a leak — and, worse, could mask a
                        # real one inside the noise.
                        base = self._sweep(who, fx, specs, subjects)
                        self._assert_no_foreign_ids(
                            base, fx, f"{label}/base-{case}")
                        self._assert_hints_are_inert(
                            base, fx, f"{label}/base-{case}")
                        with self._perturbed(fx, team, fx["gid"]):
                            world = self._sweep(who, fx, specs, subjects)
                            self._assert_hints_are_inert(
                                world, fx, f"{label}/perturbed-{case}")
                            self._assert_non_interference(
                                base, world, fx, team,
                                f"{label}/perturbed-{case}")
                            self._assert_no_foreign_ids(
                                world, fx, f"{label}/perturbed-{case}")
                        total += base.elapsed + world.elapsed
                    print(f"\n[SIDE SWEEP] {label}: 4 worlds x "
                          f"{base.requests} requests in {total:.1f}s")
                ran.extend((label, case) for case in self.CASES)
            finally:
                self._close(label, store)
        self._assert_matrix_ran(ran, self.CASES)


# ---------------------------------------------------------------------------
# 3. THE DESIGN CLASSIFICATIONS ARE STILL TRUE.
# ---------------------------------------------------------------------------
class TheDesignClassificationsAreStillTrue(_SweepHarness, unittest.TestCase):
    """The three principals entitled to more than their own side are TYPED
    claims about the product, not suppression-list entries — so each one gets
    an assertion that fails if it stops being true.

    Without these, "entitled to both sides" would be a way to silence the
    oracles, and the next round's leak could hide behind it."""

    def test_every_principal_carries_a_documented_class(self):
        for label, store in self._stores():
            try:
                self._assert_backend(label, store)
                store.clear_all_data()
                fx = self._fixture(store)
                who = self._serve(fx)
                entitlement = self._entitlement(fx)
                self.assertEqual(sorted(entitlement), sorted(PRINCIPALS))
                for principal, (klass, teams) in entitlement.items():
                    with self.subTest(principal=principal):
                        self.assertIn(klass, (
                            SCOPED_TO_ONE_SIDE, IN_NEITHER_SIDE,
                            GUARDIAN_OF_A_JUNIOR,
                            OFFICIAL_TWO_SIDED_BY_DESIGN,
                            OPERATOR_UNSCOPED_BY_DESIGN))
                        if klass == SCOPED_TO_ONE_SIDE:
                            self.assertEqual(len(teams), 1, principal)
                        if klass == IN_NEITHER_SIDE:
                            self.assertEqual(teams, frozenset(), principal)

                # OFFICIAL_TWO_SIDED_BY_DESIGN: the class is sound only
                # because the official's two-sided entitlement is the
                # SUBMITTED-LINEUP PROJECTION and nothing wider — they are
                # REFUSED every own-side leaf outright. If that stopped being
                # true, "entitled to both" would be silencing this sweep
                # rather than describing the product.
                for leaf in ("roster-status", "substitutes",
                             "availability-summary", "substitute-candidates",
                             "substitute-addable"):
                    status, body = self._req(
                        who["official"], "GET",
                        f"/api/games/{fx['gid']}/{leaf}")
                    self.assertEqual(
                        status, 403,
                        f"[{label}] an assigned official was ADMITTED to the "
                        f"own-side leaf {leaf!r}; the "
                        f"{OFFICIAL_TWO_SIDED_BY_DESIGN} classification in "
                        f"this sweep is no longer sound")
                for leaf in ("board", "lineups", "roster"):
                    status, body = self._req(
                        who["official"], "GET",
                        f"/api/games/{fx['gid']}/{leaf}")
                    self.assertEqual(
                        status, 200,
                        f"[{label}] the official's two-sided projection on "
                        f"{leaf!r} is gone, so this classification no longer "
                        f"describes them")

                # HINT_MAY_SELECT_FOR_A_BOTH_SIDED_CALLER: the exception is
                # reachable ONLY by the unscoped operator. The official is
                # the other both-sided principal and is refused all three of
                # those routes outright (asserted just above), and every
                # scoped principal's hint is proven inert by the sweep
                # itself, on these routes like every other.
                for route, leaf in (
                        ("get_games_id_availability_summary",
                         "availability-summary"),
                        ("get_games_id_substitute_candidates",
                         "substitute-candidates"),
                        ("get_games_id_substitute_addable",
                         "substitute-addable")):
                    self.assertIn(route,
                                  HINT_MAY_SELECT_FOR_A_BOTH_SIDED_CALLER)
                    for team in (fx["home"], fx["away"]):
                        status, body = self._req(
                            who["operator"], "GET",
                            f"/api/games/{fx['gid']}/{leaf}?team_id={team}")
                        self.assertEqual(status, 200, (leaf, body))
                        self.assertEqual(body["team_id"], team, (leaf, body))
                # …and `/availability-summary` additionally BOUNDS the hint to
                # the game's two sides. Asserted only where it is true: the
                # two workflow leaves do not, which the constant's own note
                # records rather than hides.
                status, body = self._req(
                    who["operator"], "GET",
                    f"/api/games/{fx['gid']}/availability-summary"
                    f"?team_id={fx['third']}")
                self.assertNotEqual(
                    status, 200,
                    f"[{label}] the ?team_id hint reached a team that is not "
                    f"in the game on availability-summary: {body}")

                # TEAM_ID_IS_A_COLLECTION_FILTER: only sound while the route
                # is operator-only, so the condition is checked against the
                # registry's recorded auth rather than taken on trust.
                by_name = {spec.name: spec for spec in route_registry.REGISTRY}
                for name in sorted(TEAM_ID_IS_A_COLLECTION_FILTER):
                    self.assertIn(name, by_name, name)
                    self.assertEqual(
                        by_name[name].auth, "operator_only",
                        f"{name} is classified TEAM_ID_IS_A_COLLECTION_FILTER "
                        f"— exempt from the hint-inertness assertion — but "
                        f"the registry records it auth="
                        f"{by_name[name].auth!r}. Loosening that route's auth "
                        f"widens who may filter with it, so the exemption "
                        f"must be re-decided rather than inherited.")
                    for principal in PRINCIPALS:
                        if principal == "operator":
                            continue
                        status, _body = self._req(
                            who[principal], "GET",
                            f"/api/players?team_id={fx['home']}")
                        self.assertNotEqual(
                            status, 200,
                            f"[{label}] {principal} was admitted to an "
                            f"operator-only collection whose ?team_id filter "
                            f"this sweep does not check")

                # GUARDIAN_OF_A_JUNIOR: the entitled side is the JUNIOR's
                # resolved side, and the junior is a Mover — so this class is
                # a statement about membership, not about the pointer.
                junior = fx["api"].store.get_player(fx["guardian_junior_id"])
                self.assertEqual(junior.team_id, fx["home"],
                                 "the guardian's junior is not a Mover, so "
                                 "GUARDIAN_OF_A_JUNIOR would be satisfied by "
                                 "the pointer")
                self.assertEqual(
                    fx["api"].roster.team_for_game(
                        fx["api"].store.get_game(fx["gid"]), junior),
                    fx["away"])
                self.assertEqual(entitlement["guardian"][1],
                                 frozenset({fx["away"]}))

                # OPERATOR_UNSCOPED_BY_DESIGN: the operator session really
                # carries no team/player binding.
                status, body = self._req(who["operator"], "GET",
                                         "/api/auth/me")
                self.assertEqual(status, 200, body)
                self.assertNotIn(
                    "team_id",
                    json.dumps(body.get("scope") or {}, sort_keys=True),
                    "the 'unscoped operator' session carries a team scope, "
                    "so OPERATOR_UNSCOPED_BY_DESIGN is not what it claims")

                # IN_NEITHER_SIDE: the third team really plays in neither
                # game, which is what makes that row the sharpest in the
                # matrix.
                for gid in (fx["gid"], fx["gid2"]):
                    game = fx["api"].store.get_game(gid)
                    self.assertNotIn(fx["third"],
                                     (game.home_team_id, game.away_team_id))
            finally:
                self._close(label, store)
            return   # these are claims about the fixture and the gates

# ---------------------------------------------------------------------------
# 4. THE SWEEP CAN FAIL — AND FAILS ON THE LEAK IT FOUND.
# ---------------------------------------------------------------------------
class TheSweepCatchesTheLeakItFound(_SweepHarness, unittest.TestCase):
    """A whole-surface sweep that cannot go red is a very expensive way of
    asserting nothing.

    The fifth leak is REINTRODUCED into the live code and the two oracles are
    required to report it — which is also the executable form of the claim
    that this sweep, and not the static scanner, is what carries the side
    rule: the same defect passed
    ``services/side_provenance.py`` under an exemption whose condition was
    satisfied."""

    @contextlib.contextmanager
    def _pointer_side_restored(self):
        """``get_player_home`` derives its side from ``Player.team_id``, as
        it did at b1cc02d."""
        real = _ApiService.get_player_home

        def home(self, player_id, user_id=None):
            out = real(self, player_id, user_id=user_id)
            ng = out.get("next_game") if isinstance(out, dict) else None
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
                    rstatus.status.value, "not_responded")})
            return out
        _ApiService.get_player_home = home
        try:
            yield
        finally:
            _ApiService.get_player_home = real

    def test_the_non_interference_oracle_reports_the_fifth_leak(self):
        for label, store in self._stores():
            try:
                self._assert_backend(label, store)
                store.clear_all_data()
                fx = self._fixture(store)
                who = self._serve(fx)
                specs, subjects = self._assert_inventory_is_closed(fx)
                with self._pointer_side_restored():
                    base = self._sweep(who, fx, specs, subjects)
                    # The AWAY-entitled reader's PERMANENT POINTER names HOME,
                    # so perturbing HOME must reach them under the defect.
                    with self._perturbed(fx, fx["home"], fx["gid"]):
                        world = self._sweep(who, fx, specs, subjects)
                        with self.assertRaises(AssertionError) as caught:
                            self._assert_non_interference(
                                base, world, fx, fx["home"],
                                f"{label}/falsified")
                self.assertIn(
                    "get_me_player_home", str(caught.exception),
                    "the sweep went red, but not on /api/me/player-home — so "
                    "it is not the leak this oracle is claimed to have found")
                self.assertIn(
                    "awayplayer", str(caught.exception),
                    "the sweep did not name the AWAY-entitled caller whose "
                    "response moved with HOME's private state")
            finally:
                self._close(label, store)
            return  # one backend proves the oracle can fail; the property
                    # itself is asserted on all three above




if __name__ == "__main__":
    unittest.main()
