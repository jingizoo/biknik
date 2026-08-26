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

ENTITLEMENT IS PER ROUTE AND PER DATA CLASS, NOT PER PRINCIPAL
=============================================================
Round 6 found that this sweep was blind to an official-only widening:
:meth:`_entitlement` handed an assigned official BOTH side ids globally, so
both oracles skipped every official response on every route under either
perturbation. A brand-new REGISTERED route answering an official both sides'
full private lineup rows changed under a HOME perturbation, the raw diff
CONTAINED its rows, and both oracles reported green. Entitlement is now
answered by :meth:`_entitled_teams` from ``(principal, route, data class)``:
an official may observe both sides only for :data:`SUBMITTED_LINEUP_DATA` and
only on :data:`OFFICIAL_ASSIGNED_GAME_ROUTES`, and never
:data:`TEAM_WORKFLOW_DATA` anywhere. That the injected route now fails THIS
oracle, and that a FULL official projection on each of /board, /lineups and
/roster does too, are asserted in
:class:`TheOfficialGrantIsRouteAndDataClassSpecific`.

Round 7 found the SAME SHAPE ONE SEAT OVER: narrowing the official had left
the GUARDIAN globally entitled to a whole side, on every route and for every
data class. A registered route handing ``Role.GUARDIAN`` alone the junior's
entire team's private workflow state — carrying no identities, so only the
non-interference oracle could see it — passed the primary sweep blind. The
guardian is now confined to :data:`GUARDIAN_JUNIOR_ROUTES` by the same
mechanism, narrowed by ROUTE ONLY because the measurement says the data-class
rule does not apply to them; see :class:`TheGuardianGrantIsRouteSpecific`.

RUNTIME, MEASURED AND STATED. Eight worlds — a fresh base and a perturbed
world for each of the two sides x each of the two perturbation kinds in
:data:`PERTURBATIONS` — x every authenticated GET route x 8 principals x 4
side-hint variants: 1,984 real HTTP requests per world, 15,872 per backend.
Measured on this machine: **6.1 s Memory, 6.3 s SQLite, 21.5-26.6 s real
PostgreSQL** for the main property. It is a whole-surface behavioural sweep
and it is cheaper than it sounds, because every request is a loopback call
into an in-process server.

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
from hockey_scheduler.domain import Role
from hockey_scheduler.store import InMemoryStore
from hockey_scheduler.services.roster_service import RosterService
from hockey_scheduler.web import route_registry
from hockey_scheduler.web import server as srv
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
#: A guardian verified for one junior. Entitled to that junior's resolved side
#: — but ONLY on the two routes that serve the junior's own row, never as a
#: standing grant over the junior's whole team.
#:
#: THE BLIND SPOT THIS REPLACES (#427 round 3). The blocker-2 fix narrowed the
#: OFFICIAL and left the GUARDIAN one seat over in exactly the shape the owner
#: had just ruled on: :meth:`_entitled_teams` returned the junior's whole side
#: for every route and every data class, so all three oracles read that one
#: number for the guardian on all 48 route names. MEASURED on the head this
#: corrects: a REGISTERED authenticated route returning to
#: ``Role.GUARDIAN`` alone the entire AWAY side's private workflow state —
#: per-player availability answers, substitute status, roster statuses and
#: candidate-pool cardinality, deliberately carrying NO identities so only the
#: non-interference oracle could see it — moved under an AWAY perturbation and
#: the PRIMARY SWEEP PASSED BLIND.
#:
#: WHAT THE PRODUCT GRANT ACTUALLY IS, measured on this tree rather than
#: assumed (the measurements are re-asserted in
#: :class:`TheGuardianGrantIsRouteSpecific`):
#:
#: * the guardian is REFUSED (403) every leaf of the private-game family —
#:   all ten of them, ``/board`` and ``/lineups`` included, which is where it
#:   differs from the official, who is ADMITTED to three of them;
#: * the only AWAY-durable identity it receives anywhere is on
#:   ``get_me_guardian_home``;
#: * under an AWAY ``substitute_enrolment`` it moves on exactly
#:   ``get_me_guardian_home`` and
#:   ``get_me_guardian_id_substitute_opportunities_id``;
#: * under an AWAY ``backed_out_roster_history`` it moves NOWHERE.
#:
#: So the grant is one junior's own Player Home row and their substitute
#: opportunities — :data:`GUARDIAN_JUNIOR_ROUTES` — and that is what is handed
#: to the oracles.

#: THE ONLY ROUTES on which a guardian may observe anything of their junior's
#: side. The guardian analogue of :data:`OFFICIAL_ASSIGNED_GAME_ROUTES`, and
#: deliberately the SAME mechanism rather than a second one.
#:
#: ROUTE-SPECIFIC ONLY, AND THE MEASUREMENT SAYS WHY. The official's grant is
#: additionally narrowed by DATA CLASS; the guardian's is not, and copying
#: that rule across reflexively would be wrong. A substitute enrolment on the
#: junior's side is :data:`TEAM_WORKFLOW_DATA`, and it LEGITIMATELY moves the
#: junior's substitute-opportunity route — that is what the route is for.
#: Measured: restricting the guardian to :data:`SUBMITTED_LINEUP_DATA` FAILS
#: the sweep, restricting them to nothing at all FAILS it, and restricting
#: them to these two routes PASSES. All three are re-measured in
#: :meth:`TheGuardianGrantIsRouteSpecific.test_the_route_set_is_exactly_what_the_sweep_needs`.
GUARDIAN_JUNIOR_ROUTES = frozenset({
    "get_me_guardian_home",
    "get_me_guardian_id_substitute_opportunities_id"})

OFFICIAL_SUBMITTED_LINEUP_ONLY = "official_submitted_lineup_only"
#: An assigned official. Entitled to BOTH sides — but only to the SUBMITTED /
#: OCCUPYING LINEUP, and only on the assigned-game routes that serve it.
#:
#: THE BLIND SPOT THIS REPLACES (#427 round 2, blocker 2). This class used to
#: be spelled ``OFFICIAL_TWO_SIDED_BY_DESIGN`` and :meth:`_entitlement`
#: handed the official BOTH side ids GLOBALLY — for every route and for every
#: kind of private state. Both oracles read that one number, so EVERY change
#: in EVERY official response, under EITHER side's perturbation, was skipped.
#: Measured on the head this corrects: a brand-new authenticated route whose
#: official response returned both sides' full ``_lineup_rows`` — candidate
#: pool, per-player availability, substitute status — changed under a HOME
#: perturbation, the raw diff CONTAINED all four of its rows, and both
#: oracles still reported GREEN. The sweep is the PRIMARY protection
#: precisely because the static scanner had blessed a live leak; a blind spot
#: in it is worse than a leak on one route, because it is what is supposed to
#: catch the next five.
#:
#: So entitlement is no longer a property of the PRINCIPAL alone. It is a
#: function of (principal, ROUTE, DATA CLASS) — see :meth:`_entitled_teams`,
#: :data:`OFFICIAL_ASSIGNED_GAME_ROUTES` and :data:`TEAM_WORKFLOW_DATA`.
#: What an official may observe is stated positively and narrowly; everything
#: else about them is subject to both oracles like any other principal.

#: THE ONLY ROUTES on which an assigned official may observe anything of
#: either side, and the routes whose whole subject IS the submitted lineup:
#: the Game Sheet (`/board`, `/lineups`) and the sheet-shaped `/roster`
#: projection built from the same `_submitted_lineup_rows`. The official is
#: REFUSED every other leaf of the family outright, which
#: :class:`TheDesignClassificationsAreStillTrue` asserts — so this set being
#: exactly these three is a claim about the product, checked, not assumed.
OFFICIAL_ASSIGNED_GAME_ROUTES = frozenset({
    "get_games_id_board", "get_games_id_lineups", "get_games_id_roster"})

#: WHAT KIND OF PRIVATE STATE a perturbation moves.
#:
#: ``SUBMITTED_LINEUP_DATA``
#:     Who OCCUPIES a slot on a side's sheet, and the sheet fields of those
#:     rows. An assigned official is entitled to this, on
#:     :data:`OFFICIAL_ASSIGNED_GAME_ROUTES` and nowhere else.
#: ``TEAM_WORKFLOW_DATA``
#:     Availability answers, substitute workflow, the candidate pool, the
#:     audit stream, and the roster history of rows that no longer occupy a
#:     slot. "An official referees the game, they do not manage anyone's
#:     roster": they are entitled to NONE of this, on ANY route, including
#:     the three above.
SUBMITTED_LINEUP_DATA = "submitted_lineup_data"
TEAM_WORKFLOW_DATA = "team_workflow_data"

#: ``{perturbation kind: the data class it moves}``. Every kind the sweep
#: drives is named here, and :meth:`_perturbed` asserts the class it claims —
#: a "workflow" perturbation that silently changed slot occupancy would make
#: every official assertion below mean something different from what it says.
PERTURBATIONS = {
    "substitute_enrolment": TEAM_WORKFLOW_DATA,
    "backed_out_roster_history": TEAM_WORKFLOW_DATA,
}

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
        # A DURABLY SEATED ROW PER SIDE THAT NO LONGER OCCUPIES ITS SLOT —
        # what the `backed_out_roster_history` perturbation moves (#427
        # round 2, blockers 2 and 3). `_lineup_rows` keeps such a row in the
        # `selected` GROUP and flags it `backed_out: true`, so a side's own
        # coach can still see it for cleanup; it is that side's roster
        # HISTORY, not its current sheet, and an official must not observe
        # it changing. Seated then marked unavailable, so it holds no slot
        # and the side's own status enums are untouched.
        api = fx["api"]
        for label, side in (("home", fx["home"]), ("away", fx["away"])):
            person = self._mover(
                fx, f"Backed Out Seat {label}", fx["third"], side)
            out = api.select_roster(fx["gid"], [person["id"]], actor_id=ADMIN)
            assert "error" not in out, out
            out = api.set_availability(fx["gid"], person["id"], "unavailable",
                                       actor_id=ADMIN)
            assert "error" not in out, out
            entry = api.store.roster_entry_for_player(fx["gid"], person["id"])
            assert not entry.status.occupies_slot, entry.status
            fx["people"][f"backed_out_{label}"] = person
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
            "official": (OFFICIAL_SUBMITTED_LINEUP_ONLY,
                         frozenset({fx["home"], fx["away"]})),
            "operator": (OPERATOR_UNSCOPED_BY_DESIGN,
                         frozenset({fx["home"], fx["away"]})),
        }

    def _entitled_teams(self, fx, principal, route, data_class):
        """WHICH SIDES this principal may observe ON THIS ROUTE, for THIS
        KIND of private state — the number both oracles are read against
        (#427 round 2, blocker 2).

        :meth:`_entitlement` above states each principal's WIDEST
        entitlement, which is what the hint-inertness rule and the
        classification tests are about. It is NOT what the oracles may use:
        an official's widest entitlement is both sides, and reading that one
        number everywhere is exactly what made every official response
        invisible to both oracles.

        TWO classes narrow here, and they narrow along DIFFERENT axes —
        which is the point, and why each carries its own measurement:

        * :data:`OFFICIAL_SUBMITTED_LINEUP_ONLY` narrows by ROUTE **and** by
          DATA CLASS. An official referees the game; the current sheet is
          theirs on three routes, the workflow behind it is theirs nowhere.
        * :data:`GUARDIAN_OF_A_JUNIOR` narrows by ROUTE ONLY. A guardian's
          grant is one junior's own row, so it is confined to
          :data:`GUARDIAN_JUNIOR_ROUTES`; but ON those routes it covers
          workflow state too, because a substitute enrolment on the junior's
          side legitimately moves the junior's substitute-opportunity route.
          Adding the official's data-class rule here would make the sweep
          report that legitimate move as a leak — measured, not assumed.

        The rest do not depend on the route or on which kind of state moved.
        A Coach/Player is entitled to their own side's everything (they
        manage that roster, and every route that answers them their own side
        is answering them their own business); an unscoped operator to both
        sides' everything; a coach of neither team to nothing at all.
        """
        klass, teams = self._entitlement(fx)[principal]
        if klass == GUARDIAN_OF_A_JUNIOR:
            # The junior's own row, on the junior's own two routes. Not a
            # standing grant over the junior's whole team on all 48.
            if route not in GUARDIAN_JUNIOR_ROUTES:
                return frozenset()
            return teams
        if klass != OFFICIAL_SUBMITTED_LINEUP_ONLY:
            return teams
        if data_class != SUBMITTED_LINEUP_DATA:
            # Availability, substitute, candidate, audit and backed-out
            # roster history: not theirs ANYWHERE, the three assigned-game
            # routes included.
            return frozenset()
        if route not in OFFICIAL_ASSIGNED_GAME_ROUTES:
            return frozenset()
        return teams

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
        entitled to ON THAT ROUTE, anywhere in any body, word-boundary
        matched.

        PER ROUTE, not per principal (#427 round 2, blocker 2). A player
        identity on a response is a SUBMITTED-LINEUP-class observation — it
        is a name on a game sheet — so the official keeps both sides on
        :data:`OFFICIAL_ASSIGNED_GAME_ROUTES` and is entitled to NO identity
        of either side anywhere else. Reading one global number per
        principal is what let a new route hand an official both sides'
        private rows with this oracle green."""
        forbidden = {}
        for principal in PRINCIPALS:
            for route in {key[1] for key in sweep.rows}:
                teams = self._entitled_teams(
                    fx, principal, route, SUBMITTED_LINEUP_DATA)
                ids = set()
                for team in (fx["home"], fx["away"]):
                    if team not in teams:
                        ids |= self._durable_ids(fx, team)
                forbidden[(principal, route)] = ids
        # The premise: at least one principal must have something real to
        # fail to reach, or this oracle asserts nothing.
        self.assertTrue(
            forbidden[("thirdcoach", "get_games_id_board")],
            f"[{label}] no durably attributed ids exist, so the identity "
            f"oracle is vacuous")
        # …and the official's narrowing is REAL: they are forbidden both
        # sides' identities somewhere, or this oracle has not tightened at
        # all and blocker 2 is only claimed to be closed.
        self.assertTrue(
            forbidden[("official", "get_games_id_officials")],
            f"[{label}] the official is forbidden no identity on any "
            f"non-assigned-game route, so the route-specific entitlement is "
            f"vacuous for exactly the principal it was introduced for")
        for (principal, route, path, hint), (_status, body) in sweep.rows.items():
            blob = json.dumps(body, sort_keys=True, default=str)
            for pid in sorted(forbidden[(principal, route)]):
                self.assertIsNone(
                    re.search(rf"\b{re.escape(pid)}\b", blob),
                    f"[{label}] {principal} received {pid} — a player "
                    f"durably attributed to a side they are not entitled to "
                    f"on this route — from GET {path} (hint={hint}, "
                    f"route={route})")

    # -- oracle 2: non-interference ---------------------------------------
    @contextlib.contextmanager
    def _perturbed(self, fx, team_id, game_id, kind="substitute_enrolment"):
        """Change ONE side's private per-side state in ONE game, and nothing
        else — and assert it really moved that side's own private state, so
        "nothing changed elsewhere" is not a vacuous observation.

        TWO KINDS, both :data:`TEAM_WORKFLOW_DATA` (#427 round 2, blocker 2):

        ``substitute_enrolment``
            The original. Enrol or withdraw a substitute; the direction is
            chosen from the side's own current state, so a side that HAS
            active enrollments has them withdrawn and a side that has none
            gets one. Premise: the side's own roster-status enum moves.

        ``backed_out_roster_history``
            Move a durably-seated row that ALREADY holds no slot between
            ``unavailable`` and ``removed``. Premise: the entry's status
            moves AND ``occupies_slot`` is false on BOTH sides of the move —
            which is what makes it workflow history rather than a change to
            the submitted sheet, and therefore something an assigned official
            must not observe on any route. It is also the perturbation that
            reaches ``/roster``: an official's `/roster` projection is
            insensitive to substitute enrolment (measured), so without this
            kind the FULL-projection proof could not run on that leaf at all.
        """
        assert kind in PERTURBATIONS, kind
        api = fx["api"]
        if kind == "backed_out_roster_history":
            person = fx["people"][
                "backed_out_home" if team_id == fx["home"] else
                "backed_out_away"]
            entry = api.store.roster_entry_for_player(game_id, person["id"])
            was = entry.status
            assert not was.occupies_slot, (
                f"the {kind} fixture row occupies a slot ({was.value}), so "
                f"perturbing it would change the SUBMITTED sheet and every "
                f"official assertion below would mean something else")

            def do():
                out = api.remove_player(game_id, person["id"], actor_id=ADMIN)
                assert "error" not in out, out

            def undo():
                out = api.select_roster(game_id, [person["id"]],
                                        actor_id=ADMIN)
                assert "error" not in out, out
                out = api.set_availability(game_id, person["id"],
                                           "unavailable", actor_id=ADMIN)
                assert "error" not in out, out
            do()
            now = api.store.roster_entry_for_player(game_id, person["id"]).status
            assert now != was, (
                f"perturbing {team_id}'s backed-out seat did not move its "
                f"entry status ({was.value}); the two worlds would be "
                f"indistinguishable")
            assert not now.occupies_slot, (
                f"the {kind} perturbation put {person['id']} back ON the "
                f"sheet ({now.value}), so it is a SUBMITTED_LINEUP change "
                f"and not the workflow-history change it is classified as")
            try:
                yield
            finally:
                undo()
            return
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

    def _assert_non_interference(self, base, perturbed, fx, team_id, label,
                                 data_class):
        """ORACLE 2. Every diff must belong to a caller ENTITLED to the side
        that was perturbed, ON THAT ROUTE, for THAT KIND of private state.
        Anything else is a response that is a function of that side's private
        state.

        ``data_class`` is what :meth:`_perturbed` actually moved, asserted
        there rather than assumed here. It is the dimension blocker 2 was
        missing: the official's grant is real on three routes for the
        submitted lineup, and is nothing at all for availability, substitute,
        candidate, audit or backed-out roster history — so under a
        ``TEAM_WORKFLOW_DATA`` perturbation an official diff is an offender
        exactly like anyone else's."""
        offenders, entitled_moved = [], set()
        for key in base.diff(perturbed):
            principal, route = key[0], key[1]
            if team_id in self._entitled_teams(
                    fx, principal, route, data_class):
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
             "hints_are_inert_in_every_world"] + [
        f"perturbing_{side}_{kind}_reaches_only_entitled_callers"
        for side in ("home", "away") for kind in sorted(PERTURBATIONS)]

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
                    total, phases = 0.0, 0
                    for team, case in ((fx["home"], "home"),
                                       (fx["away"], "away")):
                        for kind, data_class in sorted(PERTURBATIONS.items()):
                            # THE BASE IS MEASURED FRESH FOR EACH PHASE, not
                            # once for all of them. A perturbation is not
                            # byte-reversible: `/board` serves the game's
                            # AUDIT STREAM, which is append-only, so undoing
                            # an enrolment leaves more audit rows than it
                            # found. Comparing a later phase against a base
                            # taken before an earlier one would report that
                            # bookkeeping as a leak — and, worse, could mask
                            # a real one inside the noise.
                            tag = f"{label}/{case}/{kind}"
                            base = self._sweep(who, fx, specs, subjects)
                            self._assert_no_foreign_ids(base, fx, f"{tag}/base")
                            self._assert_hints_are_inert(base, fx, f"{tag}/base")
                            with self._perturbed(fx, team, fx["gid"], kind):
                                world = self._sweep(who, fx, specs, subjects)
                                self._assert_hints_are_inert(
                                    world, fx, f"{tag}/perturbed")
                                self._assert_non_interference(
                                    base, world, fx, team, f"{tag}/perturbed",
                                    data_class)
                                self._assert_no_foreign_ids(
                                    world, fx, f"{tag}/perturbed")
                            total += base.elapsed + world.elapsed
                            phases += 2
                    print(f"\n[SIDE SWEEP] {label}: {phases} worlds x "
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
                            OFFICIAL_SUBMITTED_LINEUP_ONLY,
                            OPERATOR_UNSCOPED_BY_DESIGN))
                        if klass == SCOPED_TO_ONE_SIDE:
                            self.assertEqual(len(teams), 1, principal)
                        if klass == IN_NEITHER_SIDE:
                            self.assertEqual(teams, frozenset(), principal)

                # OFFICIAL_SUBMITTED_LINEUP_ONLY: the class is sound only
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
                        f"{OFFICIAL_SUBMITTED_LINEUP_ONLY} classification in "
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

                # …AND WHAT THE GUARDIAN MAY READ, ON WHICH ROUTE (#427
                # round 3). Asserting only "the guardian's side equals the
                # junior's resolved side" says nothing about the SURFACE that
                # side is readable on, and it was precisely that gap that let
                # the class hand out a whole side on all 48 routes. The
                # official got a per-leaf 403/200 matrix; so does this one.
                #
                # The guardian differs from the official in the direction
                # that matters: the official is ADMITTED to three leaves of
                # the private-game family with a projection, the guardian is
                # REFUSED ALL TEN outright.
                for leaf in ("board", "lineups", "roster", "roster-status",
                             "substitutes", "availability-summary",
                             "substitute-candidates", "substitute-addable",
                             "officials", "reschedule"):
                    status, body = self._req(
                        who["guardian"], "GET",
                        f"/api/games/{fx['gid']}/{leaf}")
                    self.assertEqual(
                        status, 403,
                        f"[{label}] a guardian was ADMITTED to the "
                        f"private-game leaf {leaf!r}. A guardian speaks for "
                        f"ONE junior; the {GUARDIAN_OF_A_JUNIOR} "
                        f"classification confines them to "
                        f"{sorted(GUARDIAN_JUNIOR_ROUTES)} and this leaf is "
                        f"not one of them: {body}")
                # …and the two routes the grant IS on really do answer them.
                for path in ("/api/me/guardian/home",
                             f"/api/me/guardian/{fx['guardian_junior_id']}"
                             f"/substitute-opportunities/{fx['gid']}"):
                    status, body = self._req(who["guardian"], "GET", path)
                    self.assertEqual(
                        status, 200,
                        f"[{label}] the guardian's own route {path} no "
                        f"longer answers them, so GUARDIAN_JUNIOR_ROUTES "
                        f"names a surface that is not theirs: {body}")
                # …and the route set is spelled the way the registry spells
                # it, so a renamed route breaks this rather than silently
                # emptying the grant.
                by_name_all = {s.name for s in route_registry.REGISTRY}
                self.assertLessEqual(
                    GUARDIAN_JUNIOR_ROUTES, by_name_all,
                    "GUARDIAN_JUNIOR_ROUTES names a route the registry does "
                    "not have; the guardian's grant would be silently empty")

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
# 3b. THE OFFICIAL'S GRANT IS ROUTE- AND DATA-CLASS-SPECIFIC — AND THE SWEEP
#     ITSELF GOES RED WHEN IT IS NOT.
#
# THE DEFECT THIS SECTION EXISTS FOR (#427 round 2, blocker 2). The
# entitlement map handed the official BOTH side ids globally, so both oracles
# skipped every official response on every route under either perturbation.
# The sweep is the PRIMARY protection — the static scanner had already
# blessed a live leak — so a blind spot here is worse than a leak on one
# route: it is the thing that is supposed to catch the next five.
#
# Everything below drives THE PRIMARY ORACLE. Nothing here consults
# `services/side_provenance.py`, and nothing here is a classification-only
# assertion: each test injects a real defect and requires
# `_assert_non_interference` — the same function the whole-surface sweep
# calls — to report it.
# ---------------------------------------------------------------------------

#: The injected route's spec. Registered for real, so it flows through the
#: SAME `route_registry.REGISTRY` inventory the sweep builds itself from.
_PROBE_TEMPLATE = "/api/sweep-probe/{}"
_PROBE_NAME = "get_sweep_probe_id"
_PROBE_SPEC = route_registry.RouteSpec(
    "GET", r"^/api/sweep-probe/[^/]+$", _PROBE_TEMPLATE, _PROBE_NAME,
    "_dispatch_get", kind="route", auth="session", scope_axis="none",
    note="injected by test_authenticated_side_noninterference; never shipped")


@contextlib.contextmanager
def _a_registered_route_that_widens_only_for_the_official():
    """A REAL, REGISTERED authenticated GET route that answers an assigned
    official with BOTH sides' full private lineup rows — candidate pool,
    per-player availability, substitute status — and answers everyone else
    with nothing.

    This is the owner's exact experiment, and on the head this round
    corrects it passed: the raw two-world diff CONTAINED all four of this
    route's official rows and both oracles still reported green, because the
    official was globally entitled to both sides.

    Registered rather than faked: the spec goes into
    ``route_registry.REGISTRY`` and the branch into the real
    ``Handler._dispatch_get``, so the sweep discovers it through its own
    inventory exactly as it would discover a route somebody shipped."""
    real_registry = route_registry.REGISTRY
    real_dispatch = srv.Handler._dispatch_get

    def dispatch(self):
        path = self.path.split("?", 1)[0]
        match = re.match(r"^/api/sweep-probe/([^/]+)$", path)
        if match is None:
            return real_dispatch(self)
        role, scope, _user_id, err = self._resolve_role()
        if err is not None:
            code, payload = err
            return self._send_json(payload, code)
        if role != Role.OFFICIAL:
            return self._send_json({"secret": None})
        api = srv.STATE.api
        game = api.store.get_game(match.group(1))
        if game is None:
            return self._send_json({"secret": None})
        return self._send_json({"secret": {
            side: api._lineup_rows(game, side)
            for side in (game.home_team_id, game.away_team_id) if side}})

    route_registry.REGISTRY = real_registry + (_PROBE_SPEC,)
    srv.Handler._dispatch_get = dispatch
    try:
        yield
    finally:
        route_registry.REGISTRY = real_registry
        srv.Handler._dispatch_get = real_dispatch


@contextlib.contextmanager
def _official_projection_replaced_with_full(method_name):
    """One facade read stops projecting for an assigned official and answers
    them the FULL two-side private read instead — the shape
    ``services/lineup_visibility`` exists to prevent, reintroduced on ONE
    route at a time so a failure names which one."""
    real = getattr(_ApiService, method_name)

    def widened(self, *args, **kwargs):
        if kwargs.get("viewer_role") == Role.OFFICIAL:
            kwargs = dict(kwargs, viewer_role=None)
        return real(self, *args, **kwargs)

    setattr(_ApiService, method_name, widened)
    try:
        yield
    finally:
        setattr(_ApiService, method_name, real)


#: ``{facade read: the route name the sweep knows it by}`` — the three routes
#: :data:`OFFICIAL_ASSIGNED_GAME_ROUTES` names, and nothing else.
_OFFICIAL_READS = {
    "get_board": "get_games_id_board",
    "get_lineups": "get_games_id_lineups",
    "get_roster": "get_games_id_roster",
}


class TheOfficialGrantIsRouteAndDataClassSpecific(_SweepHarness,
                                                  unittest.TestCase):
    """The two proofs the owner required for blocker 2, plus the one that
    keeps the grant from being vacuous."""

    #: Set by the injection context manager; keeps the probe out of the
    #: inventory when it is not installed, so the sweep still fails closed.
    probe_registered = False

    def _route_subjects(self, fx):
        subjects = super()._route_subjects(fx)
        if self.probe_registered:
            subjects[_PROBE_NAME] = [(fx["gid"],)]
        return subjects

    @contextlib.contextmanager
    def _probe(self):
        with _a_registered_route_that_widens_only_for_the_official():
            self.probe_registered = True
            try:
                yield
            finally:
                self.probe_registered = False

    @contextlib.contextmanager
    def _sheet_perturbed(self, fx, team_id, game_id):
        """A SUBMITTED_LINEUP_DATA perturbation: seat a new player of
        ``team_id``, so a row that did not occupy a slot now does. Asserted
        to be exactly that, so this cannot quietly become a workflow change
        and make the grant below look live when it is not."""
        api = fx["api"]
        fresh = self._mover(
            fx, f"Sheet Perturber {team_id} {game_id}", fx["third"], team_id)
        out = api.select_roster(game_id, [fresh["id"]], actor_id=ADMIN)
        assert "error" not in out, out
        entry = api.store.roster_entry_for_player(game_id, fresh["id"])
        assert entry is not None and entry.status.occupies_slot, entry
        try:
            yield
        finally:
            out = api.remove_player(game_id, fresh["id"], actor_id=ADMIN)
            assert "error" not in out, out

    def _offenders(self, base, world, fx, team, label, data_class):
        """``_assert_non_interference``'s failure text, or ``None`` when it
        passed. The sweep's OWN oracle — not a re-implementation of it."""
        try:
            self._assert_non_interference(base, world, fx, team, label,
                                          data_class)
        except AssertionError as exc:
            return str(exc)
        return None

    # -- PROOF (a) --------------------------------------------------------
    def test_a_registered_official_widening_route_fails_the_primary_sweep(
            self):
        """THE OWNER'S EXPERIMENT, required to go RED.

        A brand-new registered authenticated route hands an assigned official
        both sides' full private lineup rows. Perturbing HOME's private
        workflow state changes that response, and the PRIMARY non-interference
        oracle must report it — naming the route and naming the official.

        AND THE FALSIFIER, in the same test: with the global both-sides
        entitlement restored — the exact code this round replaced — the SAME
        experiment passes. That is the reproduction, executable, so this test
        cannot go green for some unrelated reason."""
        for label, store in self._stores():
            try:
                self._assert_backend(label, store)
                store.clear_all_data()
                fx = self._fixture(store)
                who = self._serve(fx)
                with self._probe():
                    specs, subjects = self._assert_inventory_is_closed(fx)
                    self.assertIn(
                        _PROBE_NAME, {spec.name for spec in specs},
                        "the injected route is not in the inventory the "
                        "sweep builds itself from, so nothing below is a "
                        "statement about the sweep")
                    base = self._sweep(who, fx, specs, subjects)
                    with self._perturbed(fx, fx["home"], fx["gid"],
                                         "substitute_enrolment"):
                        world = self._sweep(who, fx, specs, subjects)
                        # The premise: the widened route really did move.
                        moved = [key for key in base.diff(world)
                                 if key[1] == _PROBE_NAME]
                        self.assertTrue(
                            moved,
                            "the injected route's response did not change "
                            "under a HOME perturbation, so it is not the "
                            "widening this test is about")
                        reported = self._offenders(
                            base, world, fx, fx["home"], f"{label}/probe",
                            TEAM_WORKFLOW_DATA)
                        self.assertIsNotNone(
                            reported,
                            "THE PRIMARY SWEEP DID NOT CATCH A REGISTERED "
                            "ROUTE HANDING AN ASSIGNED OFFICIAL BOTH SIDES' "
                            "FULL PRIVATE LINEUP STATE. This is blocker 2 "
                            "exactly, and the sweep is the primary "
                            "protection: it must fail here before anything "
                            "supplemental is consulted.")
                        self.assertIn(_PROBE_NAME, reported)
                        self.assertIn("official", reported)

                        # -- THE FALSIFIER: the old, global entitlement.
                        widest = self._entitlement(fx)

                        def blind(_fx, principal, _route, _data_class,
                                  _widest=widest):
                            return _widest[principal][1]

                        real = self._entitled_teams
                        self._entitled_teams = blind
                        try:
                            still_green = self._offenders(
                                base, world, fx, fx["home"],
                                f"{label}/blind", TEAM_WORKFLOW_DATA)
                        finally:
                            self._entitled_teams = real
                        self.assertIsNone(
                            still_green,
                            "the GLOBAL both-sides entitlement this round "
                            "replaced still catches the injected route, so "
                            "this test is not measuring the change it "
                            "claims to: " + str(still_green))
            finally:
                self._close(label, store)
            return   # the oracle's own behaviour, not a per-backend property

    # -- PROOF (b) --------------------------------------------------------
    def test_a_full_official_projection_is_caught_on_each_of_the_three_routes(
            self):
        """EACH of ``/board``, ``/lineups`` and ``/roster`` stops projecting
        for an official and answers the FULL two-side private read; the
        primary oracle must report that route by name.

        WHICH PERTURBATION, AND WHY IT IS THIS ONE. The measured matrix on
        this tree: ``backed_out_roster_history`` — a durably-seated row that
        holds no slot moving between ``unavailable`` and ``removed``, which is
        availability/roster workflow and NOT a change to the submitted sheet
        — catches all three. ``substitute_enrolment`` catches ``/board`` and
        ``/lineups`` but NOT ``/roster``, because ``get_roster``'s FULL branch
        returns roster ENTRIES and an enrolment creates none. The matrix is
        asserted, not assumed: a route that stopped being caught, or one that
        started being caught by the substitute perturbation, fails here."""
        expected = {
            ("get_games_id_board", "backed_out_roster_history"): True,
            ("get_games_id_lineups", "backed_out_roster_history"): True,
            ("get_games_id_roster", "backed_out_roster_history"): True,
            ("get_games_id_board", "substitute_enrolment"): True,
            ("get_games_id_lineups", "substitute_enrolment"): True,
            # `get_roster`'s FULL branch reads roster ENTRIES; a substitute
            # enrolment creates none, so this pair genuinely cannot catch it
            # — which is why the workflow-history perturbation exists.
            ("get_games_id_roster", "substitute_enrolment"): False,
        }
        measured = {}
        for method, route in sorted(_OFFICIAL_READS.items()):
            for kind in sorted(PERTURBATIONS):
                # A FRESH FIXTURE PER CASE. `enroll_substitute` after a
                # withdrawal writes a NEW enrollment row rather than
                # reviving the old one, so the substitute perturbation is
                # only reversible ONCE per side per fixture (the whole-
                # surface sweep uses it exactly once per side). Rebuilding
                # keeps that fact from turning into a false result here.
                store = InMemoryStore()
                try:
                    fx = self._fixture(store)
                    who = self._serve(fx)
                    specs, subjects = self._assert_inventory_is_closed(fx)
                    with _official_projection_replaced_with_full(method):
                        base = self._sweep(who, fx, specs, subjects)
                        with self._perturbed(fx, fx["home"], fx["gid"], kind):
                            world = self._sweep(who, fx, specs, subjects)
                            reported = self._offenders(
                                base, world, fx, fx["home"],
                                f"{method}/{kind}", PERTURBATIONS[kind])
                    caught = reported is not None and route in reported
                    measured[(route, kind)] = caught
                finally:
                    store.clear_all_data()
        self.assertEqual(
            expected, measured,
            "the FULL-official-projection catch matrix moved. Every True "
            "here is a route on which the primary sweep proves the "
            "submitted-lineup projection is load-bearing; a True that went "
            "False means the sweep would no longer notice that route "
            "answering an official both sides in full.")

    # -- THE GRANT IS LIVE, NOT DEAD ---------------------------------------
    def test_the_three_assigned_game_routes_really_do_grant_the_sheet(self):
        """``OFFICIAL_ASSIGNED_GAME_ROUTES`` is a GRANT, not a dead constant.

        If an official were simply never entitled to anything, every
        assertion above would still pass and the route set would mean
        nothing. So: a SUBMITTED_LINEUP_DATA perturbation — seating a player,
        which changes who occupies a slot — must move the official's response
        on exactly those three routes and on no other route they can reach.

        SCOPE, STATED: this asserts about the OFFICIAL only. Which other
        principals move under a sheet perturbation is not this test's claim,
        and is not asserted here."""
        for label, store in self._stores():
            try:
                self._assert_backend(label, store)
                store.clear_all_data()
                fx = self._fixture(store)
                who = self._serve(fx)
                specs, subjects = self._assert_inventory_is_closed(fx)
                base = self._sweep(who, fx, specs, subjects)
                with self._sheet_perturbed(fx, fx["home"], fx["gid"]):
                    world = self._sweep(who, fx, specs, subjects)
                    moved = {key[1] for key in base.diff(world)
                             if key[0] == "official"}
                self.assertEqual(
                    set(OFFICIAL_ASSIGNED_GAME_ROUTES), moved,
                    f"[{label}] a change to who OCCUPIES a slot moved the "
                    f"official's response on {sorted(moved)}, not on exactly "
                    f"{sorted(OFFICIAL_ASSIGNED_GAME_ROUTES)}. Either the "
                    f"grant those three routes carry has gone dead (every "
                    f"assertion about the official above would then be "
                    f"vacuous), or a fourth route is answering them "
                    f"submitted-lineup state.")
                # …and under a WORKFLOW perturbation the same official moves
                # NOWHERE, which is the other half of "route- AND
                # data-class-specific".
                base = self._sweep(who, fx, specs, subjects)
                with self._perturbed(fx, fx["home"], fx["gid"],
                                     "backed_out_roster_history"):
                    world = self._sweep(who, fx, specs, subjects)
                    workflow_moved = {key[1] for key in base.diff(world)
                                      if key[0] == "official"}
                self.assertEqual(
                    set(), workflow_moved,
                    f"[{label}] a side's ROSTER WORKFLOW HISTORY reached the "
                    f"official on {sorted(workflow_moved)}; an official is "
                    f"entitled to the current sheet, not to how it got "
                    f"there.")
            finally:
                self._close(label, store)
            return   # the grant is a property of the projection, not of a
                     # backend; the whole-surface sweep runs on all three


# ---------------------------------------------------------------------------
# 3c. THE GUARDIAN'S GRANT IS ROUTE-SPECIFIC — AND THE SWEEP ITSELF GOES RED
#     WHEN IT IS NOT.
#
# THE DEFECT THIS SECTION EXISTS FOR (#427 round 3). Narrowing the official
# for blocker 2 left the guardian in the identical shape one seat over:
# entitled to a whole side globally, on every route, for every data class.
# The same experiment the owner ran for the official — a registered route
# widening for ONE principal — passed the primary sweep blind when aimed at
# the guardian, and it did so while deliberately carrying NO identities, so
# oracle 1 could not have covered for oracle 2 either.
#
# The structure here is the official's, reused rather than reinvented: a
# registered-route injection driving the PRIMARY oracle, a live-grant
# measurement, and the route set pinned against the alternatives.
# ---------------------------------------------------------------------------

_GUARDIAN_PROBE_TEMPLATE = "/api/sweep-probe-guardian/{}"
_GUARDIAN_PROBE_NAME = "get_sweep_probe_guardian_id"
_GUARDIAN_PROBE_SPEC = route_registry.RouteSpec(
    "GET", r"^/api/sweep-probe-guardian/[^/]+$", _GUARDIAN_PROBE_TEMPLATE,
    _GUARDIAN_PROBE_NAME, "_dispatch_get", kind="route", auth="session",
    scope_axis="none",
    note="injected by test_authenticated_side_noninterference; never shipped")


@contextlib.contextmanager
def _a_registered_route_that_widens_only_for_the_guardian():
    """A REAL, REGISTERED authenticated GET route that answers a verified
    guardian with the junior's ENTIRE SIDE's private workflow state —
    per-player availability answers, substitute status, roster statuses and
    the candidate-pool cardinality — and answers everyone else with nothing.

    IT CARRIES NO IDENTITIES, DELIBERATELY. ``id``, ``name`` and
    ``jersey_number`` are stripped from every row, so oracle 1 cannot see
    this and the experiment is a statement about the NON-INTERFERENCE oracle
    alone — the one the guardian's global entitlement was blinding. A probe
    that leaked names would pass this test for the wrong reason.

    Registered rather than faked, exactly like the official's probe: the spec
    goes into ``route_registry.REGISTRY`` and the branch into the real
    ``Handler._dispatch_get``, so the sweep discovers it through its own
    inventory as it would discover a route somebody shipped."""
    real_registry = route_registry.REGISTRY
    real_dispatch = srv.Handler._dispatch_get

    def dispatch(self):
        path = self.path.split("?", 1)[0]
        match = re.match(r"^/api/sweep-probe-guardian/([^/]+)$", path)
        if match is None:
            return real_dispatch(self)
        role, scope, _user_id, err = self._resolve_role()
        if err is not None:
            code, payload = err
            return self._send_json(payload, code)
        if role != Role.GUARDIAN:
            return self._send_json({"secret": None})
        api = srv.STATE.api
        game = api.store.get_game(match.group(1))
        if game is None or not game.away_team_id:
            return self._send_json({"secret": None})
        rows = api._lineup_rows(game, game.away_team_id)
        scrubbed = [{k: v for k, v in row.items()
                     if k not in ("id", "name", "jersey_number")}
                    for row in rows]
        return self._send_json({"secret": {
            "rows": scrubbed, "candidate_pool_size": len(scrubbed)}})

    route_registry.REGISTRY = real_registry + (_GUARDIAN_PROBE_SPEC,)
    srv.Handler._dispatch_get = dispatch
    try:
        yield
    finally:
        route_registry.REGISTRY = real_registry
        srv.Handler._dispatch_get = real_dispatch


class TheGuardianGrantIsRouteSpecific(_SweepHarness, unittest.TestCase):
    """The three proofs the guardian was missing — the same three the
    official got."""

    probe_registered = False

    def _route_subjects(self, fx):
        subjects = super()._route_subjects(fx)
        if self.probe_registered:
            subjects[_GUARDIAN_PROBE_NAME] = [(fx["gid"],)]
        return subjects

    @contextlib.contextmanager
    def _probe(self):
        with _a_registered_route_that_widens_only_for_the_guardian():
            self.probe_registered = True
            try:
                yield
            finally:
                self.probe_registered = False

    def _offenders(self, base, world, fx, team, label, data_class):
        """``_assert_non_interference``'s failure text, or ``None`` when it
        passed. The sweep's OWN oracle — not a re-implementation of it."""
        try:
            self._assert_non_interference(base, world, fx, team, label,
                                          data_class)
        except AssertionError as exc:
            return str(exc)
        return None

    # -- PROOF (a) --------------------------------------------------------
    def test_a_registered_guardian_widening_route_fails_the_primary_sweep(
            self):
        """THE OWNER'S EXPERIMENT, AIMED AT THE GUARDIAN, required to go RED.

        A brand-new registered authenticated route hands a verified guardian
        the junior's whole side's private workflow state. Perturbing that
        side changes the response, and the PRIMARY non-interference oracle
        must report it — naming the route and naming the guardian.

        AND THE FALSIFIER, in the same test: with the global whole-side
        entitlement restored — the exact code this round replaces — the SAME
        experiment passes. That is the reproduction, executable, so this test
        cannot go green for some unrelated reason.

        ORACLE 1 IS ALSO REQUIRED TO BE BLIND HERE, asserted rather than
        assumed: the probe strips identities, so if oracle 1 caught it this
        would not be a statement about the non-interference oracle at all."""
        for label, store in self._stores():
            try:
                self._assert_backend(label, store)
                store.clear_all_data()
                fx = self._fixture(store)
                who = self._serve(fx)
                with self._probe():
                    specs, subjects = self._assert_inventory_is_closed(fx)
                    self.assertIn(
                        _GUARDIAN_PROBE_NAME, {spec.name for spec in specs},
                        "the injected route is not in the inventory the "
                        "sweep builds itself from, so nothing below is a "
                        "statement about the sweep")
                    base = self._sweep(who, fx, specs, subjects)
                    with self._perturbed(fx, fx["away"], fx["gid"],
                                         "substitute_enrolment"):
                        world = self._sweep(who, fx, specs, subjects)
                        moved = [key for key in base.diff(world)
                                 if key[1] == _GUARDIAN_PROBE_NAME]
                        self.assertTrue(
                            moved,
                            "the injected route's response did not change "
                            "under an AWAY perturbation, so it is not the "
                            "widening this test is about")
                        # The probe is invisible to oracle 1 BY CONSTRUCTION,
                        # and that is what makes this a test of oracle 2.
                        self._assert_no_foreign_ids(
                            world, fx, f"{label}/guardian-probe")
                        reported = self._offenders(
                            base, world, fx, fx["away"],
                            f"{label}/guardian-probe", TEAM_WORKFLOW_DATA)
                        self.assertIsNotNone(
                            reported,
                            "THE PRIMARY SWEEP DID NOT CATCH A REGISTERED "
                            "ROUTE HANDING A GUARDIAN THE JUNIOR'S WHOLE "
                            "SIDE'S PRIVATE WORKFLOW STATE. This is blocker "
                            "2's shape one seat over, and the sweep is the "
                            "primary protection: it must fail here before "
                            "anything supplemental is consulted.")
                        self.assertIn(_GUARDIAN_PROBE_NAME, reported)
                        self.assertIn("guardian", reported)

                        # -- THE FALSIFIER: the old, global entitlement.
                        widest = self._entitlement(fx)

                        def blind(_fx, principal, _route, _data_class,
                                  _widest=widest):
                            return _widest[principal][1]

                        real = self._entitled_teams
                        self._entitled_teams = blind
                        try:
                            still_green = self._offenders(
                                base, world, fx, fx["away"],
                                f"{label}/blind", TEAM_WORKFLOW_DATA)
                        finally:
                            self._entitled_teams = real
                        self.assertIsNone(
                            still_green,
                            "the GLOBAL whole-side entitlement this round "
                            "replaced still catches the injected route, so "
                            "this test is not measuring the change it "
                            "claims to: " + str(still_green))
            finally:
                self._close(label, store)
            return   # the oracle's own behaviour, not a per-backend property

    # -- PROOF (b): THE GRANT IS LIVE, NOT DEAD ---------------------------
    def test_the_guardian_moves_on_exactly_its_own_routes_and_nowhere_else(
            self):
        """``GUARDIAN_JUNIOR_ROUTES`` is a GRANT, not a dead constant.

        If a guardian were simply never entitled to anything, every assertion
        above would still pass and the route set would mean nothing. So:
        perturbing the JUNIOR's side must move the guardian's response on
        exactly those routes and on no other route they can reach — and
        perturbing the OTHER side must move them NOWHERE, which is the half
        that says the grant is the junior's side rather than "whatever the
        guardian happens to see".

        SCOPE, STATED: this asserts about the GUARDIAN only. Which other
        principals move is not this test's claim."""
        for label, store in self._stores():
            try:
                self._assert_backend(label, store)
                store.clear_all_data()
                fx = self._fixture(store)
                who = self._serve(fx)
                specs, subjects = self._assert_inventory_is_closed(fx)
                base = self._sweep(who, fx, specs, subjects)
                with self._perturbed(fx, fx["away"], fx["gid"],
                                     "substitute_enrolment"):
                    world = self._sweep(who, fx, specs, subjects)
                    moved = {key[1] for key in base.diff(world)
                             if key[0] == "guardian"}
                self.assertEqual(
                    set(GUARDIAN_JUNIOR_ROUTES), moved,
                    f"[{label}] perturbing the junior's own side moved the "
                    f"guardian's response on {sorted(moved)}, not on exactly "
                    f"{sorted(GUARDIAN_JUNIOR_ROUTES)}. Either the grant "
                    f"those routes carry has gone dead (every assertion "
                    f"about the guardian above would then be vacuous), or a "
                    f"further route is answering them their junior's side.")
                # …and the OTHER side reaches them nowhere at all.
                base = self._sweep(who, fx, specs, subjects)
                with self._perturbed(fx, fx["home"], fx["gid"],
                                     "substitute_enrolment"):
                    world = self._sweep(who, fx, specs, subjects)
                    other = {key[1] for key in base.diff(world)
                             if key[0] == "guardian"}
                self.assertEqual(
                    set(), other,
                    f"[{label}] the side the guardian's junior does NOT play "
                    f"for reached them on {sorted(other)}. A guardian speaks "
                    f"for one junior; the opposing side's private state is "
                    f"not theirs on any route.")
            finally:
                self._close(label, store)
            return   # the grant is a property of the routes, not of a
                     # backend; the whole-surface sweep runs on all three

    # -- PROOF (c): THE ROUTE SET IS EXACTLY RIGHT ------------------------
    def test_the_route_set_is_exactly_what_the_sweep_needs(self):
        """WHY THESE TWO ROUTES, AND WHY NOT A DATA-CLASS RULE — measured.

        The route set is not a guess to be taken on trust, so the three
        candidate entitlements are each run against the sweep's own oracle
        and the outcome asserted:

        * ``nothing anywhere`` must FAIL — otherwise the grant is dead and
          could simply be deleted;
        * ``only on GUARDIAN_JUNIOR_ROUTES`` must PASS — the shipped rule;
        * ``only for SUBMITTED_LINEUP_DATA`` must FAIL — this is the measured
          reason the official's data-class narrowing is NOT copied across. A
          substitute enrolment is workflow data and it LEGITIMATELY moves the
          junior's substitute-opportunity route.

        A future round that tries to tighten the guardian the way the
        official was tightened gets a red test here explaining why."""
        outcomes = {}
        for variant in ("nothing", "route_specific", "submitted_only"):
            store = InMemoryStore()
            try:
                fx = self._fixture(store)
                who = self._serve(fx)
                specs, subjects = self._assert_inventory_is_closed(fx)
                real = self._entitled_teams

                def candidate(_fx, principal, route, data_class,
                              _v=variant, _real=real):
                    klass, teams = self._entitlement(_fx)[principal]
                    if klass != GUARDIAN_OF_A_JUNIOR:
                        return _real(_fx, principal, route, data_class)
                    if _v == "nothing":
                        return frozenset()
                    if _v == "submitted_only":
                        return (teams if data_class == SUBMITTED_LINEUP_DATA
                                else frozenset())
                    return (teams if route in GUARDIAN_JUNIOR_ROUTES
                            else frozenset())

                base = self._sweep(who, fx, specs, subjects)
                with self._perturbed(fx, fx["away"], fx["gid"],
                                     "substitute_enrolment"):
                    world = self._sweep(who, fx, specs, subjects)
                    self._entitled_teams = candidate
                    try:
                        outcomes[variant] = self._offenders(
                            base, world, fx, fx["away"], variant,
                            TEAM_WORKFLOW_DATA) is None
                    finally:
                        self._entitled_teams = real
            finally:
                store.clear_all_data()
        self.assertEqual(
            {"nothing": False, "route_specific": True,
             "submitted_only": False}, outcomes,
            "the guardian entitlement measurement moved. `route_specific` is "
            "the shipped rule and must pass; the other two must fail, which "
            "is what makes the route set load-bearing and the absence of a "
            "data-class rule deliberate rather than an oversight.")


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
                                f"{label}/falsified", TEAM_WORKFLOW_DATA)
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
