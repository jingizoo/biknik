"""#205 — THE PRIMARY PROTECTION: a behavioural sweep of the WHOLE
authenticated GET surface, in two worlds, on all three backends.

WHAT "ON ALL THREE BACKENDS" MEANS HERE, MEASURED RATHER THAN IMPLIED. The
WHOLE-SURFACE PROPERTY — :meth:`NoAuthenticatedRouteLeaksTheOtherSide
.test_no_side_private_state_reaches_a_caller_without_entitlement`, which is
the asset — runs on Memory, SQLite and real PostgreSQL, and
``_assert_matrix_ran`` fails if a configured backend silently went missing.
Every OTHER test in this module returns after the first backend, or builds an
``InMemoryStore`` directly, or touches no store at all — each with a written
reason on the line that does it: they pin the behaviour of an ORACLE, of the
fixture, or of a gate, none of which is a per-backend property. INSTRUMENTED,
so the sentence is a count and not an impression: exactly ONE test method in
this file loops every configured backend, and it is the whole-surface property
above. That is deliberate design rather than an accident, but the headline
sentence is easy to read as a claim about every test in the file, so it is
qualified here rather than left to be re-derived by the next round.

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
FOUR callers are entitled to more than their own side — an assigned OFFICIAL,
a LEAGUE ADMIN, an ARENA MANAGER and a GUARDIAN — and each is a named class
with its own dedicated assertion rather than a line in a suppression list; see
:meth:`_SweepHarness._entitlement` and
:class:`TheDesignClassificationsAreStillTrue`. If one of them stops matching
its class, that test fails; it does not silently widen this sweep.

(It said THREE until round 4, and the fourth was the one nobody had looked
at: ``arena_manager`` reads both sides in full, by design, and appeared
nowhere in this file. See :data:`PRINCIPAL_ROLES`.)

AND THE PRINCIPAL AXIS FAILS CLOSED, LIKE THE ROUTE AXIS
========================================================
:data:`PRINCIPAL_ROLES` is checked against ``domain.Role`` itself by
:class:`TheSweepCoversEveryDomainRole`, so a role the product gains and this
sweep does not drive is an error naming it. Before round 4 nothing compared
the swept principals to the domain at all, and two of the seven roles were
unswept.

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

EVERY ASSERTION IN HERE CAN FAIL, AND A NAMED TEST PROVES IT
============================================================
Round 7 also found that neutering :meth:`_assert_non_interference` reddened
three tests while neutering :meth:`_assert_no_foreign_ids` or
:meth:`_assert_hints_are_inert` reddened NOTHING — the two of them could have
silently stopped biting and this suite would not have noticed. That is the
same shape as the defect above, sitting in the protection itself, and on this
PR an earlier round deleted two real authorization gates outright with the
whole suite staying green. Each of the three assertions now has at least one
test that injects a defect ONLY THAT ASSERTION can see, so neutering it
reddens a named test: :class:`TheSweepCatchesTheLeakItFound` for oracle 2,
and :class:`EveryOracleGoesRedOnADefectOnlyItCanSee` for the other two.

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

THE TWO DISCLOSED LIMITS OF THIS SWEEP
======================================
Both are honest scope, both are demonstrated rather than suspected, and both
are written here so a later round does not spend itself re-deriving them.

**1. A name collision in** :data:`_SweepHarness.VOLATILE_KEYS`. The six
time-varying keys are stripped BY NAME and AT ANY DEPTH, from both oracles,
before comparison. A genuinely private per-side field that happened to be
called ``now``, ``issued_at`` or ``last_seen_at``, at any nesting level,
would therefore be invisible to this whole file — demonstrated, not
theorised. The list is short and generic on purpose, and its own comment
already says the operative thing: adding a name to it is widening a blind
spot, not tidying.

**2. THE INVENTORY IS GET-ONLY.** :meth:`_SweepHarness
._authenticated_get_specs` filters ``method == "GET"``, so the fail-closed
property covers exactly the authenticated GET surface — 50 routes on this
tree. Counted off ``route_registry.REGISTRY`` rather than estimated: 161
authenticated POST specs return response bodies and enter NO oracle, and a
newly added one is NOT reported by :meth:`_SweepHarness
._assert_inventory_is_closed`; a further 17 ``auth="none"`` GET routes are
unswept as well. No live POST leak was found when this was
written — the obvious candidates each return a single ``_serialized`` row —
but "none found" is not "none exists", and a POST-shaped sweep is a separate
piece of work rather than a line that can be added here.

**3. ORACLE 2 REACHES 18 OF THE 50 SWEPT ROUTES.** Measured across all
sixteen worlds: only eighteen route names ever move, so
:meth:`_SweepHarness._assert_non_interference` CANNOT fail on the other
thirty-two for any principal. This is not "thirty-two unprotected routes" —
oracle 1 and hint-inertness are asserted on every route in every world, and
they are the two that caught D3 and are pinned by
:class:`EveryOracleGoesRedOnADefectOnlyItCanSee` — but it does bound what the
non-interference argument covers. Two of the thirty-two,
``get_games_id_officials`` and ``get_games_id_reschedule``, are leaves of the
private-game family this round is about, and that specific pair is asserted
rather than left in prose by
:meth:`WhatOracleTwoCanAndCannotReach
.test_two_family_leaves_are_beyond_the_non_interference_oracle`.
"""

import contextlib
import dataclasses
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
from hockey_scheduler.services import game_side_scope, lineup_visibility
from hockey_scheduler.services.roster_service import RosterService
from hockey_scheduler.web import route_registry
from hockey_scheduler.web import scope as web_scope
from hockey_scheduler.web import server as srv
from hockey_scheduler.web.auth import DEMO_PASSWORD, DEMO_USERS

#: ``{principal username: the domain Role that principal's session carries}``
#: — real sessions, real cookies, one per row.
#:
#: THE AXIS FAILS CLOSED, LIKE THE ROUTE AXIS (#427 round 4, D1). The route
#: inventory has been taken from the registry since round 1, so a new route
#: cannot be a silent gap. The PRINCIPAL axis had no such property: the only
#: statement anywhere about which principals are swept was
#: ``assertEqual(sorted(entitlement), sorted(PRINCIPALS))``, and BOTH SIDES OF
#: THAT EQUALITY CAME FROM THIS MODULE. Nothing compared them to
#: ``domain.Role``, which has SEVEN members while the sweep drove FIVE:
#: ``arena_manager`` and ``viewer`` appeared nowhere in the file.
#:
#: THE FALSIFIER, and the reason this is a hole rather than an omission.
#: Adding ``Role.VIEWER`` to ``lineup_visibility._UNSCOPED_OPERATORS`` and to
#: ``game_side_scope.resolve_private_game_read``'s operator tuple — a two-line
#: change of exactly the shape this PR series exists to prevent — hands a
#: signed-in viewer BOTH sides' full private lineups with ``restricted:
#: false`` (measured: ``GET /api/games/{id}/lineups`` -> 200, home n=8, away
#: n=5). Run at the head this corrects, the PRIMARY SWEEP STAYED GREEN — ``Ran
#: 13 tests … OK`` — along with the provenance scanner, the read fence, the
#: sibling-route suite and the overview suite. One test in the whole backend
#: noticed, and it was not this one.
#:
#: :class:`TheSweepCoversEveryDomainRole` now asserts this map against
#: ``domain.Role`` itself, so a role the product gains and this sweep does not
#: drive is an ERROR NAMING IT — the same property the route inventory has.
PRINCIPAL_ROLES = {
    "homecoach": Role.COACH,
    "awaycoach": Role.COACH,
    "homeplayer": Role.PLAYER,
    "awayplayer": Role.PLAYER,
    "official": Role.OFFICIAL,
    "guardian": Role.GUARDIAN,
    "thirdcoach": Role.COACH,
    "operator": Role.LEAGUE_ADMIN,
    "arena": Role.ARENA_MANAGER,
    "viewer": Role.VIEWER,
}

#: The ten principals, in a stable order.
PRINCIPALS = tuple(PRINCIPAL_ROLES)

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
#:
#: ORACLE 2 WAS VACUOUS FOR ONE OF THE TWO CLASSES (#427 round 4, D4). Both
#: kinds used to map to :data:`TEAM_WORKFLOW_DATA`, so
#: :meth:`_assert_non_interference` was only ever CALLED with that class and
#: no world in the matrix could exercise the official's submitted-lineup
#: grant through it. (Oracle 1 did exercise the class — it passes
#: ``SUBMITTED_LINEUP_DATA`` explicitly — so the rule was not unreachable;
#: only oracle 2 was.) ``seated_lineup_row`` closes that: it is the
#: perturbation the official-specific test already used privately, promoted
#: into the matrix so every principal is measured against it on every route,
#: not just the official on three.
PERTURBATIONS = {
    "substitute_enrolment": TEAM_WORKFLOW_DATA,
    "backed_out_roster_history": TEAM_WORKFLOW_DATA,
    "seated_lineup_row": SUBMITTED_LINEUP_DATA,
}

#: ``{fixture game key: the perturbation kinds that can move THAT GAME's own
#: private state}``.
#:
#: THE SECOND GAME WAS SWEPT BUT NEVER PERTURBED (#427 round 4, D4). Both of
#: ``gid2``'s paths are in :meth:`_SweepHarness._route_subjects` and every
#: principal reads them in every world — but the main loop moved private state
#: in ``fx["gid"]`` only, so no response ABOUT the second game was ever the
#: subject of a two-world diff. That is the game whose sides are SWAPPED, which
#: is exactly the fixture property built to catch "silently HOME".
#:
#: WHY ``gid2`` CARRIES ONLY ONE KIND, MEASURED RATHER THAN CHOSEN. The second
#: game has ONE skater slot and no goalie slot, and each side is deliberately
#: left in a DIFFERENT roster status from the one it holds in the first game —
#: that is what makes a side resolved once for the whole response detectable.
#: The consequence is that the two workflow perturbations cannot move it:
#:
#: * ``substitute_enrolment`` — HOME seats nobody there (``draft``) and AWAY
#:   fills its single slot (``awaiting_responses``); enrolling a substitute
#:   moves NEITHER enum, so :meth:`_SweepHarness._perturbed`'s own premise
#:   assertion aborts rather than yielding a vacuous world;
#: * ``backed_out_roster_history`` — the fixture's backed-out seats exist only
#:   in the first game, so there is no row in the second to move.
#:
#: Both exclusions are re-measured by
#: :meth:`TheSecondGameIsPerturbedToo.test_the_excluded_kinds_really_cannot
#: _move_the_second_game`, so this is a recorded limitation of the FIXTURE and
#: not a decision this map is free to make. Widening the second game's slot
#: counts to make the workflow kinds reach it would destroy the differing-
#: status premise the overview fixture is built on, which is why it is stated
#: here instead.
PERTURBED_GAMES = {
    "gid": tuple(sorted(PERTURBATIONS)),
    "gid2": ("seated_lineup_row",),
}

OPERATOR_UNSCOPED_BY_DESIGN = "operator_unscoped_by_design"
#: An unscoped operator may read either side; narrowing them would be its own
#: regression. Entitled to both.

UNSCOPED_OPERATOR_WITHOUT_ROSTER_AUTHORITY = (
    "unscoped_operator_without_roster_authority")
#: An ARENA MANAGER. Entitled to both sides, like a league admin — and NOT the
#: same principal as one, which is why it gets its own class rather than being
#: folded into :data:`OPERATOR_UNSCOPED_BY_DESIGN` (#427 round 4, D1).
#:
#: CLASSIFIED BY MEASUREMENT, NOT BY ASSUMING IT EQUALS A LEAGUE ADMIN. It sits
#: in the same two admitting tuples — ``lineup_visibility._UNSCOPED_OPERATORS``
#: and ``game_side_scope.resolve_private_game_read`` — so the private-game READ
#: really is two-sided for it, and narrowing its entitlement here would report
#: the product's own design as a leak. But it holds ``MANAGE_ARENA`` and
#: ``MANAGE_SCHEDULE`` and NOT ``MANAGE_ROSTER``, and the leaf-by-leaf matrix
#: measured over a real session differs from the league admin's on exactly the
#: roster-workflow leaves:
#:
#: ===========================  =============  ==============
#: leaf                         arena_manager  league_admin
#: ===========================  =============  ==============
#: ``/lineups`` ``/board``      200 (FULL)     200 (FULL)
#: ``/roster``                  200            200
#: ``/roster-status``           200            200
#: ``/substitutes``             200            200
#: ``/officials``               200            200
#: ``/reschedule``              200            200
#: ``/availability-summary``    400            400
#: ``/substitute-candidates``   **403**        **200**
#: ``/substitute-addable``      **403**        **200**
#: ===========================  =============  ==============
#:
#: Both bold rows are asserted in
#: :class:`TheArenaManagerIsAnOperatorWithoutRosterAuthority`, so the class is
#: a checked claim about the product: an arena manager that gained
#: ``MANAGE_ROSTER`` — or a league admin that lost it — breaks that test rather
#: than quietly changing what this sweep means.

VIEWER_ENTITLED_TO_NOTHING = "viewer_entitled_to_nothing"
#: A signed-in VIEWER. Entitled to NO side of either game, anywhere, on any
#: route, for any data class — and swept precisely BECAUSE it is entitled to
#: nothing, which makes it the sharpest row in the matrix alongside
#: :data:`IN_NEITHER_SIDE`.
#:
#: A viewer holds only ``Permission.VIEW`` and is measured 403 on all ten
#: leaves of the private-game family. That is the product being correct today;
#: the value of the row is that it is now WATCHED. The two-line falsifier in
#: :data:`PRINCIPAL_ROLES` — the one the whole suite missed — is required to
#: redden this sweep by :class:`TheViewerFalsifierRedensThePrimarySweep`.

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
    #:
    #: THE STRIPPING IS BY NAME AND DEPTH-INDEPENDENT: :meth:`_canonical`
    #: recurses, so a key called ``now`` or ``issued_at`` is removed wherever
    #: it appears, at ANY nesting level, in EVERY response — and it is removed
    #: before BOTH oracles read the body, not only before the diff. A private
    #: per-side field that happened to be given one of these names would
    #: therefore be invisible to this sweep. The list is short and generic on
    #: purpose; adding a name to it is widening a blind spot, not tidying.
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
            # THE TWO ROLES THE SWEEP DID NOT DRIVE (#427 round 4, D1). Both
            # are unscoped: an arena manager's authority is league-wide, and a
            # viewer has none to scope. See :data:`PRINCIPAL_ROLES`.
            "arena": (DEMO_USERS["arena"], {}),
            "viewer": (DEMO_USERS["viewer"], {}),
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
            # Two-sided by the product's own `_UNSCOPED_OPERATORS` tuple, and
            # NOT a league admin — see the class comment for the measured
            # difference.
            "arena": (UNSCOPED_OPERATOR_WITHOUT_ROSTER_AUTHORITY,
                      frozenset({fx["home"], fx["away"]})),
            # Nothing, anywhere. The row the falsifier has to redden.
            "viewer": (VIEWER_ENTITLED_TO_NOTHING, frozenset()),
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
        stored ``GameRosterEntry.attribution[0]`` / ``SubstituteEnrollment
        .team_id`` across both games, never live membership and never the
        pointer.

        NO LONGER THE FORBIDDEN SET (#427 round 4, D3) — see
        :meth:`_private_side_ids`. It is kept because it is what makes the
        widening MEASURABLE: the premise assertions below require the
        forbidden set to be strictly wider than this, so a future edit that
        quietly reverted to durable-only would be reported rather than
        merely smaller."""
        out = set()
        for gid in (fx["gid"], fx["gid2"]):
            sides = RosterService(fx["api"].store).durable_game_sides(gid)
            out |= {pid for pid, side in sides.items() if side == team_id}
        return out

    def _private_side_ids(self, fx):
        """``({team_id: frozenset of ids}, ambiguous)`` — EVERY IDENTITY THE
        SYSTEM TREATS AS PRIVATE TO A SIDE, which is the question oracle 1
        is actually asking.

        THE BLIND SPOT THIS REPLACES (#427 round 4, D3). The forbidden set
        was :meth:`_durable_ids` alone, and ``durable_game_sides`` has only
        two authorities — ``GameRosterEntry.attribution[0]`` and
        ``SubstituteEnrollment.team_id``. An ELIGIBLE-BUT-UNSELECTED
        CANDIDATE has neither, so no candidate identity was in ANY
        principal's forbidden set. That population is not a footnote: it is
        this blocker's own subject matter — the owner described the original
        defect as a leak of "both sides' private candidate lists".
        MEASURED on the head this corrects: ``/lineups`` serves HOME eight
        identities, FOUR of which (``player_4``, ``player_8``, ``player_9``,
        ``player_15``) were outside every forbidden set, and AWAY one more
        (``player_14``). A REGISTERED authenticated route handing
        ``thirdcoach`` — typed :data:`IN_NEITHER_SIDE`, entitled to nothing
        of either side — HOME's candidate pool, SNAPSHOTTED so it is
        perturbation-invariant and never reading the query string, passed
        ALL THREE oracles green.

        WHY THE SOURCE IS TWO PRODUCTION AUTHORITIES AND NEITHER IS WIDENED.
        The tempting fix is to teach ``durable_game_sides`` about candidates.
        That would be wrong: it is production code that decides who may READ
        AN EVENT THAT HAS ALREADY HAPPENED, its narrowness is the ruling
        ("Omit them unless an event can be durably attributed to the
        permitted side"), and widening a real authorization authority to
        make a test convenient is the exact shape this PR series exists to
        stop. The two questions are simply different:

        * ``RosterService.durable_game_sides`` — "which side was this row
          ADMITTED on", the authority for reading a past event. Includes a
          WITHDRAWN or DECLINED enrollment, which no longer appears in any
          served population;
        * ``RosterService.lineup_population`` — "who does this side's
          private screen SERVE", the authority ``ApiService._lineup_rows``
          is already a thin serializer over, and therefore the exact
          population ``/lineups``, ``/board``, ``/availability-summary`` and
          ``/substitute-addable`` hand out. Its population (c) is the live
          eligible-but-unselected candidate pool.

        Neither is modified. This method READS both and takes their union,
        which is what "private to this side" means on a read surface. If a
        route ever serves an identity from a third population, the audit
        that has to be re-run is the one in
        :meth:`TheForbiddenSetIsEverySidePrivateIdentity
        .test_no_swept_route_serves_an_identity_outside_the_forbidden_set`,
        which is why that test exists rather than a comment saying it was
        checked once.

        AMBIGUITY IS OMITTED, NEVER GUESSED — the same ruling
        ``durable_game_sides`` applies to itself, applied here for the same
        reason. A player claimed by BOTH sides has no single private side,
        and putting them in both forbidden sets would report a side's own
        coach reading their own player as a leak. The omitted set is
        RETURNED rather than swallowed, and asserted empty by the caller, so
        the rule costs nothing today and says so loudly if it ever starts
        costing something.
        """
        api = fx["api"]
        rs = RosterService(api.store)
        claims = {}
        for gid in (fx["gid"], fx["gid2"]):
            game = api.store.get_game(gid)
            for pid, side in rs.durable_game_sides(gid).items():
                claims.setdefault(pid, set()).add(side)
            for side in (game.home_team_id, game.away_team_id):
                if not side:
                    continue
                for row in rs.lineup_population(game, side):
                    claims.setdefault(row.player.id, set()).add(side)
        sides = {fx["home"]: set(), fx["away"]: set()}
        ambiguous = set()
        for pid, owners in claims.items():
            if len(owners) != 1:
                ambiguous.add(pid)
                continue
            owner = next(iter(owners))
            if owner in sides:
                sides[owner].add(pid)
        return ({t: frozenset(v) for t, v in sides.items()},
                frozenset(ambiguous))

    def _submitted_side_ids(self, fx):
        """``{team_id: frozenset}`` — the identities that OCCUPY A SLOT on a
        side's sheet, which is the strictly narrower population an assigned
        official is entitled to.

        ``ApiService._submitted_lineup_rows`` over ``_lineup_rows`` — the
        SAME pair the three official routes run, called rather than
        re-implemented, so "what the official may see" cannot drift from
        what the official is served. Without this, widening the forbidden
        set to the whole private population (above) would have handed the
        official the candidate pool as PERMITTED on their three routes, and
        oracle 1 would have stopped being able to see an official receiving
        a candidate identity — closing D3 by opening a new hole one seat
        over, which is this round's recurring shape."""
        out = {fx["home"]: set(), fx["away"]: set()}
        api = fx["api"]
        for gid in (fx["gid"], fx["gid2"]):
            game = api.store.get_game(gid)
            for side in (game.home_team_id, game.away_team_id):
                if side not in out:
                    continue
                rows = api._submitted_lineup_rows(
                    api._lineup_rows(game, side))
                out[side] |= {row["id"] for row in rows}
        return {t: frozenset(v) for t, v in out.items()}

    def _permitted_ids(self, fx, principal, route, private, submitted):
        """WHICH IDENTITIES this principal may receive ON THIS ROUTE.

        Stated POSITIVELY and narrowly, with the forbidden set derived as
        its complement — the shape blocker 2 taught, because a deny-list
        silently omits every population nobody thought of.

        THREE classes answer with something other than a whole side, and
        each narrows along its own axis:

        * :data:`GUARDIAN_OF_A_JUNIOR` — the JUNIOR'S OWN IDENTITY, on the
          junior's own routes. ROW-specific, not side-specific (#427 round
          4, D2). The class comment has claimed since round 3 that the grant
          is "the junior's own row… not a standing grant over the junior's
          whole team", while this oracle read the whole side. MEASURED with
          payload on the head this corrects: widening production
          ``ApiService.get_guardian_home`` to also carry every durably
          attributed AWAY identity returned, over a real session, 200 with
          three identities that are NOT the junior — and all thirteen tests
          stayed green. MEASURED here in the other direction: across the
          whole swept surface the guardian receives exactly ONE player
          identity anywhere, ``player_6``, the junior, on
          ``get_me_guardian_home`` — so the narrowing is sufficient for the
          real grant, which is why the residual is closed rather than
          documented.
        * :data:`OFFICIAL_SUBMITTED_LINEUP_ONLY` — the OCCUPYING rows of both
          sides, on :data:`OFFICIAL_ASSIGNED_GAME_ROUTES`, and no identity
          anywhere else. See :meth:`_submitted_side_ids`.
        * everyone else — the whole private population of each side
          :meth:`_entitled_teams` grants them.
        """
        klass, _teams = self._entitlement(fx)[principal]
        if klass == GUARDIAN_OF_A_JUNIOR:
            if route not in GUARDIAN_JUNIOR_ROUTES:
                return frozenset()
            return frozenset({fx["guardian_junior_id"]})
        if klass == OFFICIAL_SUBMITTED_LINEUP_ONLY:
            if route not in OFFICIAL_ASSIGNED_GAME_ROUTES:
                return frozenset()
            return submitted[fx["home"]] | submitted[fx["away"]]
        teams = self._entitled_teams(
            fx, principal, route, SUBMITTED_LINEUP_DATA)
        out = set()
        for team in (fx["home"], fx["away"]):
            if team in teams:
                out |= private[team]
        return frozenset(out)

    def _assert_no_foreign_ids(self, sweep, fx, label):
        """ORACLE 1. No identity PRIVATE TO A SIDE may appear anywhere in a
        response to a caller not permitted it ON THAT ROUTE, word-boundary
        matched.

        PER ROUTE, not per principal (#427 round 2, blocker 2), and built
        from a PERMIT list rather than a deny list (#427 round 4, D3): the
        forbidden set is every private identity of either side MINUS what
        :meth:`_permitted_ids` says this caller may receive here. A
        population nobody enumerated is therefore forbidden by default
        instead of invisible by default, which is the property the durable-
        only forbidden set did not have."""
        private, ambiguous = self._private_side_ids(fx)
        submitted = self._submitted_side_ids(fx)
        everything = private[fx["home"]] | private[fx["away"]]
        self.assertEqual(
            frozenset(), ambiguous,
            f"[{label}] {sorted(ambiguous)} are claimed by BOTH sides, so "
            f"this fixture now exercises the 'ambiguity is omitted, never "
            f"guessed' rule and those identities are in NO forbidden set. "
            f"That is a deliberate omission when it costs nothing and a "
            f"blind spot when it does not — decide which, do not delete "
            f"this assertion.")
        forbidden = {}
        routes = {key[1] for key in sweep.rows}
        for principal in PRINCIPALS:
            for route in routes:
                permitted = self._permitted_ids(
                    fx, principal, route, private, submitted)
                forbidden[(principal, route)] = everything - permitted
        # THE PREMISES. Each one is a way this oracle could be vacuous, and
        # each is asserted rather than assumed.
        #
        # (1) somebody must have something real to fail to reach.
        self.assertTrue(
            forbidden[("thirdcoach", "get_games_id_board")],
            f"[{label}] no private identities exist, so the identity oracle "
            f"is vacuous")
        # (2) the official's ROUTE narrowing is real.
        self.assertTrue(
            forbidden[("official", "get_games_id_officials")],
            f"[{label}] the official is forbidden no identity on any "
            f"non-assigned-game route, so the route-specific entitlement is "
            f"vacuous for exactly the principal it was introduced for")
        # (3) D3: the forbidden set is STRICTLY WIDER than durable
        #     attribution, and the extra identities really are forbidden to
        #     a principal entitled to neither side. Without this, reverting
        #     `_private_side_ids` to `_durable_ids` would pass silently.
        durable = self._durable_ids(fx, fx["home"]) | self._durable_ids(
            fx, fx["away"])
        candidates = everything - durable
        self.assertTrue(
            candidates,
            f"[{label}] every private identity is durably attributed, so "
            f"the candidate pool is not being exercised and D3's widening "
            f"is vacuous on this fixture")
        blind_to = forbidden[("thirdcoach", "get_games_id_board")]
        self.assertLessEqual(
            candidates, blind_to,
            f"[{label}] {sorted(candidates - blind_to)} are served "
            f"candidate identities that a coach of NEITHER team is still "
            f"permitted to receive")
        # (4) the official's DATA-CLASS narrowing is real for identities
        #     too: a candidate is not on the sheet, so it stays forbidden to
        #     them on the three routes their grant covers.
        unsubmitted = private[fx["home"]] - submitted[fx["home"]]
        self.assertTrue(
            unsubmitted,
            f"[{label}] every HOME private identity occupies a slot, so the "
            f"official's submitted-lineup identity narrowing is vacuous")
        self.assertLessEqual(
            unsubmitted, forbidden[("official", "get_games_id_board")],
            f"[{label}] the official is permitted HOME identities that do "
            f"not occupy a slot on the sheet they are entitled to")
        # (5) D2: the guardian's grant is the JUNIOR'S ROW, so the rest of
        #     the junior's side stays forbidden on the guardian's own
        #     routes.
        junior = fx["guardian_junior_id"]
        rest_of_the_side = private[fx["away"]] - {junior}
        self.assertTrue(
            rest_of_the_side,
            f"[{label}] the junior is the only private identity on their "
            f"side, so 'the junior's row, not the whole side' is vacuous")
        for route in sorted(GUARDIAN_JUNIOR_ROUTES):
            self.assertLessEqual(
                rest_of_the_side, forbidden[("guardian", route)],
                f"[{label}] on {route} the guardian is permitted identities "
                f"of the junior's side other than the junior — that is the "
                f"standing whole-side grant the class comment says it is "
                f"not")
            self.assertNotIn(junior, forbidden[("guardian", route)])
        for (principal, route, path, hint), (_status, body) in sweep.rows.items():
            blob = json.dumps(body, sort_keys=True, default=str)
            for pid in sorted(forbidden[(principal, route)]):
                self.assertIsNone(
                    re.search(rf"\b{re.escape(pid)}\b", blob),
                    f"[{label}] {principal} received {pid} — an identity "
                    f"private to a side they are not permitted on this "
                    f"route — from GET {path} (hint={hint}, "
                    f"route={route})")

    # -- oracle 2: non-interference ---------------------------------------
    @contextlib.contextmanager
    def _perturbed(self, fx, team_id, game_id, kind="substitute_enrolment"):
        """Change ONE side's private per-side state in ONE game, and nothing
        else — and assert it really moved that side's own private state, so
        "nothing changed elsewhere" is not a vacuous observation.

        THREE KINDS. The first two are :data:`TEAM_WORKFLOW_DATA` (#427
        round 2, blocker 2), the third is :data:`SUBMITTED_LINEUP_DATA`
        (#427 round 4, D4):

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

        ``seated_lineup_row``
            Seat a fresh player of ``team_id``, so a row that did not occupy
            a slot now does — a change to WHO IS ON THE SHEET. Premise: the
            new entry's status ``occupies_slot``, asserted, so this cannot
            quietly become a workflow change and make the official's
            data-class grant look exercised when it is not. This is the ONLY
            kind that drives :meth:`_assert_non_interference` with
            :data:`SUBMITTED_LINEUP_DATA`; without it that half of the
            official's rule was never reached by oracle 2 at all.
        """
        assert kind in PERTURBATIONS, kind
        api = fx["api"]
        if kind == "seated_lineup_row":
            fresh = self._mover(
                fx, f"Sheet Perturber {team_id} {game_id}", fx["third"],
                team_id)
            out = api.select_roster(game_id, [fresh["id"]], actor_id=ADMIN)
            assert "error" not in out, out
            entry = api.store.roster_entry_for_player(game_id, fresh["id"])
            assert entry is not None and entry.status.occupies_slot, (
                f"the {kind} perturbation did not put {fresh['id']} ON the "
                f"sheet, so it is not the SUBMITTED_LINEUP change it is "
                f"classified as")
            try:
                yield
            finally:
                out = api.remove_player(game_id, fresh["id"], actor_id=ADMIN)
                assert "error" not in out, out
            return
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

    @staticmethod
    def _phases():
        """``[(game key, side key, perturbation kind), …]`` — the worlds this
        property measures, derived from :data:`PERTURBED_GAMES` so the loop
        below and :data:`CASES` cannot disagree about what ran."""
        return [(game, side, kind)
                for game in sorted(PERTURBED_GAMES)
                for side in ("home", "away")
                for kind in PERTURBED_GAMES[game]]

    CASES = ["identity_oracle_on_the_whole_surface",
             "hints_are_inert_in_every_world"] + [
        f"perturbing_{side}_{kind}_in_{game}_reaches_only_entitled_callers"
        for game in sorted(PERTURBED_GAMES)
        for side in ("home", "away")
        for kind in PERTURBED_GAMES[game]]

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
                    for game_key, case, kind in self._phases():
                        team = fx[case]
                        data_class = PERTURBATIONS[kind]
                        # THE BASE IS MEASURED FRESH FOR EACH PHASE, not
                        # once for all of them. A perturbation is not
                        # byte-reversible: `/board` serves the game's
                        # AUDIT STREAM, which is append-only, so undoing
                        # an enrolment leaves more audit rows than it
                        # found. Comparing a later phase against a base
                        # taken before an earlier one would report that
                        # bookkeeping as a leak — and, worse, could mask
                        # a real one inside the noise.
                        tag = f"{label}/{game_key}/{case}/{kind}"
                        base = self._sweep(who, fx, specs, subjects)
                        self._assert_no_foreign_ids(base, fx, f"{tag}/base")
                        self._assert_hints_are_inert(base, fx, f"{tag}/base")
                        with self._perturbed(fx, team, fx[game_key], kind):
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



class WhatOracleTwoCanAndCannotReach(_SweepHarness, unittest.TestCase):
    """The non-interference oracle's REACH, asserted rather than assumed
    (#427 round 4, D4).

    A route no world moves is a route on which oracle 2 cannot fail. That is
    a real bound on what this sweep's central argument covers, and the two
    routes where it matters most — leaves of the private-game family itself —
    are named here so the limit is a checked claim instead of a sentence
    somebody has to re-derive."""

    def test_two_family_leaves_are_beyond_the_non_interference_oracle(self):
        for label, store in self._stores():
            try:
                self._assert_backend(label, store)
                store.clear_all_data()
                fx = self._fixture(store)
                who = self._serve(fx)
                specs, subjects = self._assert_inventory_is_closed(fx)
                swept = {key[1] for key in
                         self._sweep(who, fx, specs, subjects).rows}
                moved = set()
                for game_key, side, kind in \
                        NoAuthenticatedRouteLeaksTheOtherSide._phases():
                    base = self._sweep(who, fx, specs, subjects)
                    with self._perturbed(fx, fx[side], fx[game_key], kind):
                        world = self._sweep(who, fx, specs, subjects)
                        moved |= {key[1] for key in base.diff(world)}
                family = {name for name in swept
                          if name.startswith("get_games_id_")}
                self.assertEqual(
                    {"get_games_id_officials", "get_games_id_reschedule"},
                    family - moved,
                    f"[{label}] the leaves of the private-game family that "
                    f"NO world moves are {sorted(family - moved)}, not the "
                    f"two this sweep records. Oracle 2 cannot fail on a "
                    f"route nothing moves, so this set is a bound on the "
                    f"non-interference argument: a leaf that JOINED it lost "
                    f"that coverage silently, and a leaf that LEFT it means "
                    f"the recorded limit is stale.")
                # …and the premise: most of the family IS reachable, or the
                # bound above would be the whole story.
                self.assertGreaterEqual(
                    len(family & moved), 8,
                    f"[{label}] only {len(family & moved)} leaves of the "
                    f"private-game family are moved by any world, so oracle "
                    f"2 barely reaches the family this blocker is about")
            finally:
                self._close(label, store)
            return


class TheSecondGameIsPerturbedToo(_SweepHarness, unittest.TestCase):
    """:data:`PERTURBED_GAMES` is a MEASUREMENT about the fixture, not a
    choice this module is free to make (#427 round 4, D4).

    The second game — the one whose sides are SWAPPED — was swept in every
    world and never had its own private state moved, so no response about it
    was ever the subject of a two-world diff. One perturbation kind reaches
    it and two cannot, and BOTH halves are asserted here: an excluded kind
    that silently became able to move it would be coverage this sweep was
    entitled to and did not take."""

    def test_the_included_kind_really_does_move_the_second_game(self):
        for label, store in self._stores():
            try:
                self._assert_backend(label, store)
                store.clear_all_data()
                fx = self._fixture(store)
                who = self._serve(fx)
                specs, subjects = self._assert_inventory_is_closed(fx)
                for side in ("home", "away"):
                    for kind in PERTURBED_GAMES["gid2"]:
                        base = self._sweep(who, fx, specs, subjects)
                        with self._perturbed(fx, fx[side], fx["gid2"], kind):
                            world = self._sweep(who, fx, specs, subjects)
                            moved = {key[1] for key in base.diff(world)}
                        self.assertTrue(
                            moved,
                            f"[{label}] perturbing {side} in the SECOND game "
                            f"with {kind!r} moved nothing, so that phase of "
                            f"the main sweep is vacuous and PERTURBED_GAMES "
                            f"is claiming coverage it does not have")
            finally:
                self._close(label, store)
            return

    def test_the_excluded_kinds_really_cannot_move_the_second_game(self):
        """The two exclusions are a limitation of THE FIXTURE, re-measured
        rather than inherited. If one of them starts working, this test says
        so and :data:`PERTURBED_GAMES` has to be widened to take the coverage
        — the same fail-closed discipline the route inventory has."""
        excluded = sorted(set(PERTURBATIONS) - set(PERTURBED_GAMES["gid2"]))
        self.assertEqual(
            ["backed_out_roster_history", "substitute_enrolment"], excluded,
            "the set of kinds excluded from the second game moved; re-run "
            "the measurement rather than editing this list")
        for label, store in self._stores():
            try:
                self._assert_backend(label, store)
                store.clear_all_data()
                fx = self._fixture(store)
                self._serve(fx)
                for side in ("home", "away"):
                    for kind in excluded:
                        with self.subTest(side=side, kind=kind):
                            with self.assertRaises(
                                    (AssertionError, AttributeError),
                                    msg=(f"[{label}] {kind!r} now moves "
                                         f"{side} in the SECOND game. That "
                                         f"is coverage this sweep is "
                                         f"entitled to: add it to "
                                         f"PERTURBED_GAMES['gid2'] rather "
                                         f"than deleting this assertion.")):
                                with self._perturbed(fx, fx[side],
                                                     fx["gid2"], kind):
                                    pass
            finally:
                self._close(label, store)
            return

# ---------------------------------------------------------------------------
# 3. THE DESIGN CLASSIFICATIONS ARE STILL TRUE.
# ---------------------------------------------------------------------------
class TheDesignClassificationsAreStillTrue(_SweepHarness, unittest.TestCase):
    """The principals entitled to more than their own side are TYPED claims
    about the product, not suppression-list entries — so each one gets an
    assertion that fails if it stops being true.

    THERE ARE FOUR OF THEM, NOT THREE (#427 round 4, D1). This docstring said
    three and named the official, the operator and the guardian; the ARENA
    MANAGER is the fourth, reads both sides in full by design, and was not a
    swept principal at all. Its class is
    :data:`UNSCOPED_OPERATOR_WITHOUT_ROSTER_AUTHORITY` and its measured
    matrix is asserted in
    :class:`TheArenaManagerIsAnOperatorWithoutRosterAuthority`; the VIEWER,
    entitled to nothing, is covered by
    :class:`TheViewerIsEntitledToNothingAndTheSweepProvesIt`.

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
                            OPERATOR_UNSCOPED_BY_DESIGN,
                            UNSCOPED_OPERATOR_WITHOUT_ROSTER_AUTHORITY,
                            VIEWER_ENTITLED_TO_NOTHING))
                        if klass == SCOPED_TO_ONE_SIDE:
                            self.assertEqual(len(teams), 1, principal)
                        if klass == IN_NEITHER_SIDE:
                            self.assertEqual(teams, frozenset(), principal)
                        if klass == VIEWER_ENTITLED_TO_NOTHING:
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
                #
                # THIS ONE IS DEFENSIVE, NOT FALSIFIABLE, AND THAT IS NOT A
                # DEFECT — recorded here so a later reader does not mistake
                # it for a proven property. The precondition is refused
                # UPSTREAM: `AccountService._ALLOWED_SCOPE_KEYS` does not
                # accept `team_id` for a league_admin, and a row planted
                # directly into the store is normalised away at login, so
                # three attempts to make this assertion fail could not. It
                # stands as a tripwire for a future change that loosens
                # either of those, not as evidence about today's code.
                #
                # AND IT READS THE RIGHT KEY, WHICH IT DID NOT (#427 round 4).
                # `/api/auth/me` answers ``{"user": {…, "scope": {…}}}``, so
                # `body.get("scope")` was ALWAYS `None` and this assertion
                # could not have fired even for a session that did carry a
                # team scope — defensive is one thing, unable to fail is
                # another. The reader is now proven live against a principal
                # whose scope really does carry `team_id` before it is
                # trusted to say that the operator's does not.
                status, body = self._req(who["homecoach"], "GET",
                                         "/api/auth/me")
                self.assertEqual(status, 200, body)
                self.assertIn(
                    "team_id",
                    json.dumps((body.get("user") or {}).get("scope") or {},
                               sort_keys=True),
                    "a team-scoped Coach's session shows no team scope at "
                    "the key this assertion reads, so the operator check "
                    "below would pass for a session that DID carry one")
                status, body = self._req(who["operator"], "GET",
                                         "/api/auth/me")
                self.assertEqual(status, 200, body)
                self.assertNotIn(
                    "team_id",
                    json.dumps((body.get("user") or {}).get("scope") or {},
                               sort_keys=True),
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
            # AND THE THIRD KIND CATCHES NONE OF THEM, WHICH IS THE POINT
            # OF HAVING IT CLASSIFIED (#427 round 4, D4). `seated_lineup_row`
            # is SUBMITTED_LINEUP_DATA, and an assigned official IS entitled
            # to both sides' submitted lineup on exactly these three routes —
            # so widening their projection to FULL is invisible to oracle 2
            # under this perturbation, and only a WORKFLOW perturbation can
            # report it. If one of these ever flipped to True the official's
            # data-class rule would have stopped meaning what it says.
            ("get_games_id_board", "seated_lineup_row"): False,
            ("get_games_id_lineups", "seated_lineup_row"): False,
            ("get_games_id_roster", "seated_lineup_row"): False,
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
                with self._perturbed(fx, fx["home"], fx["gid"],
                                     "seated_lineup_row"):
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




# ---------------------------------------------------------------------------
# 5. EVERY ORACLE GOES RED ON A DEFECT ONLY IT CAN SEE.
#
# THE DEFECT THIS SECTION EXISTS FOR (#427 round 3). The sweep makes three
# assertions. Neutering `_assert_non_interference` reddened three tests;
# neutering `_assert_no_foreign_ids` or `_assert_hints_are_inert` reddened
# NOTHING. Two thirds of the primary protection could have silently stopped
# biting with the whole suite green — and on this PR an earlier round deleted
# two real authorization gates outright without the suite noticing, so this is
# not a hypothetical failure mode.
#
# `TheSweepCatchesTheLeakItFound` covers oracle 2 by reintroducing the fifth
# leak into live code. The two tests here do the same job for the other two,
# each injecting a defect the OTHER assertions are structurally incapable of
# seeing — so each test reddens if and only if its own assertion stops
# biting. Both injections are registered routes, the same mechanism the
# guardian and official probes use.
# ---------------------------------------------------------------------------

_STATIC_LEAK_NAME = "get_sweep_probe_roster_roll_id"
_STATIC_LEAK_SPEC = route_registry.RouteSpec(
    "GET", r"^/api/sweep-probe-roll/[^/]+$", "/api/sweep-probe-roll/{}",
    _STATIC_LEAK_NAME, "_dispatch_get", kind="route", auth="session",
    scope_axis="none",
    note="injected by test_authenticated_side_noninterference; never shipped")

_HINT_LEAK_NAME = "get_sweep_probe_hinted_status_id"
_HINT_LEAK_SPEC = route_registry.RouteSpec(
    "GET", r"^/api/sweep-probe-hinted/[^/]+$", "/api/sweep-probe-hinted/{}",
    _HINT_LEAK_NAME, "_dispatch_get", kind="route", auth="session",
    scope_axis="none",
    note="injected by test_authenticated_side_noninterference; never shipped")


@contextlib.contextmanager
def _a_route_returning_a_snapshotted_two_side_roll():
    """A registered route that answers EVERY caller a frozen roll of BOTH
    sides' durably-attributed player ids.

    WHY IT IS INVISIBLE TO THE OTHER TWO ASSERTIONS, by construction:

    * SNAPSHOTTED. The roll is computed once, on first request, and cached
      forever. It is therefore PERTURBATION-INVARIANT — the two worlds return
      byte-identical bodies — so ``_assert_non_interference`` sees no diff and
      cannot report it. This is the property that makes the test a statement
      about oracle 1 alone.
    * HINT-INDEPENDENT. The query string is never read, so
      ``_assert_hints_are_inert`` sees nothing either.

    A real leak of exactly this shape is not exotic: any endpoint that
    caches, precomputes or denormalises a roster roll has it.
    """
    real_registry = route_registry.REGISTRY
    real_dispatch = srv.Handler._dispatch_get
    snapshot = {}

    def dispatch(self):
        path = self.path.split("?", 1)[0]
        match = re.match(r"^/api/sweep-probe-roll/([^/]+)$", path)
        if match is None:
            return real_dispatch(self)
        _role, _scope, _user_id, err = self._resolve_role()
        if err is not None:
            code, payload = err
            return self._send_json(payload, code)
        gid = match.group(1)
        if gid not in snapshot:
            api = srv.STATE.api
            snapshot[gid] = sorted(
                RosterService(api.store).durable_game_sides(gid))
        return self._send_json({"roll": snapshot[gid]})

    route_registry.REGISTRY = real_registry + (_STATIC_LEAK_SPEC,)
    srv.Handler._dispatch_get = dispatch
    try:
        yield
    finally:
        route_registry.REGISTRY = real_registry
        srv.Handler._dispatch_get = real_dispatch


@contextlib.contextmanager
def _a_route_whose_hint_selects_a_side_for_a_scoped_caller():
    """A registered route where ``?team_id=`` chooses WHICH side's private
    roster-status enum the caller reads — for every caller, scoped ones
    included.

    WHY IT IS INVISIBLE TO THE OTHER TWO ASSERTIONS, by construction:

    * NO IDENTITIES. It returns one enum string and never a player id, so
      ``_assert_no_foreign_ids`` has nothing to match.
    * SNAPSHOTTED, per ``(game, side)``, so it is perturbation-invariant and
      ``_assert_non_interference`` sees no diff between the two worlds.

    What is left is precisely the property hint-inertness exists to defend:
    a query parameter selecting what a caller reads. This is the shape of the
    real thing — ``_workflow_side``'s FULL branch already answers whatever
    ``?team_id`` names, and the whole claim of this sweep is that no scoped
    caller can reach that branch."""
    real_registry = route_registry.REGISTRY
    real_dispatch = srv.Handler._dispatch_get
    snapshot = {}

    def dispatch(self):
        raw = self.path
        path = raw.split("?", 1)[0]
        match = re.match(r"^/api/sweep-probe-hinted/([^/]+)$", path)
        if match is None:
            return real_dispatch(self)
        _role, _scope, _user_id, err = self._resolve_role()
        if err is not None:
            code, payload = err
            return self._send_json(payload, code)
        gid = match.group(1)
        query = raw.split("?", 1)[1] if "?" in raw else ""
        hinted = None
        for part in query.split("&"):
            if part.startswith("team_id="):
                hinted = part.split("=", 1)[1]
        api = srv.STATE.api
        game = api.store.get_game(gid)
        if game is None:
            return self._send_json({"status": None})
        side = hinted or game.home_team_id
        if (gid, side) not in snapshot:
            try:
                snapshot[(gid, side)] = api.roster.compute_roster_status(
                    gid, side).status.value
            except Exception:
                snapshot[(gid, side)] = None
        return self._send_json({"status": snapshot[(gid, side)]})

    route_registry.REGISTRY = real_registry + (_HINT_LEAK_SPEC,)
    srv.Handler._dispatch_get = dispatch
    try:
        yield
    finally:
        route_registry.REGISTRY = real_registry
        srv.Handler._dispatch_get = real_dispatch


class EveryOracleGoesRedOnADefectOnlyItCanSee(_SweepHarness,
                                              unittest.TestCase):
    """One injected defect per otherwise-silent assertion.

    Each test asserts BOTH halves, and the second half is the one that makes
    it a falsifiability test rather than just another leak test: the
    assertion under test must REPORT the defect, and the other two must be
    measured GREEN on the same world. Without that, a test could go red for
    the wrong reason and the assertion it is meant to pin could still be
    dead."""

    probe = None

    def _route_subjects(self, fx):
        subjects = super()._route_subjects(fx)
        if self.probe:
            subjects[self.probe] = [(fx["gid"],)]
        return subjects

    def _reported(self, fn, *args):
        """The assertion's failure text, or ``None`` when it passed."""
        try:
            fn(*args)
        except AssertionError as exc:
            return str(exc)
        return None

    def test_a_static_two_side_identity_roll_reddens_only_the_id_oracle(self):
        """NEUTERING ``_assert_no_foreign_ids`` MUST REDDEN THIS TEST.

        A registered route hands every caller a frozen roll of both sides'
        durably-attributed player ids. It is perturbation-invariant and
        hint-independent by construction, so oracle 2 and hint-inertness are
        structurally blind to it — measured here, not assumed — and oracle 1
        is the only thing standing between this and a shipped leak."""
        for label, store in self._stores():
            try:
                self._assert_backend(label, store)
                store.clear_all_data()
                fx = self._fixture(store)
                who = self._serve(fx)
                with _a_route_returning_a_snapshotted_two_side_roll():
                    self.probe = _STATIC_LEAK_NAME
                    try:
                        specs, subjects = self._assert_inventory_is_closed(fx)
                        self.assertIn(
                            _STATIC_LEAK_NAME, {s.name for s in specs},
                            "the injected route is not in the sweep's own "
                            "inventory, so nothing below is a statement "
                            "about the sweep")
                        base = self._sweep(who, fx, specs, subjects)
                        # THE ASSERTION UNDER TEST must report it, and must
                        # name a caller who should not have received an id.
                        ids = self._reported(
                            self._assert_no_foreign_ids, base, fx,
                            f"{label}/static-roll")
                        self.assertIsNotNone(
                            ids,
                            "ORACLE 1 DID NOT REPORT A ROUTE HANDING EVERY "
                            "CALLER BOTH SIDES' DURABLE PLAYER IDS. This is "
                            "the assertion's only proof of life: the other "
                            "two oracles cannot see a snapshotted leak, so "
                            "if this one stops biting the sweep is silently "
                            "two thirds of a protection.")
                        self.assertIn(_STATIC_LEAK_NAME, ids)
                        # …and it names a caller who genuinely may not read
                        # that id. WHICH one it names is iteration order, not
                        # a property worth pinning; that it is a caller
                        # entitled to at most one side IS.
                        self.assertTrue(
                            any(p in ids for p in (
                                "homecoach", "awaycoach", "homeplayer",
                                "awayplayer", "thirdcoach", "guardian")),
                            f"oracle 1 reported the injected roll but named "
                            f"no caller who is short of both sides, so it is "
                            f"not reporting the leak this test injected: "
                            f"{ids}")
                        # THE OTHER TWO MUST BE BLIND, so this test can only
                        # go red for the reason it claims.
                        self.assertIsNone(
                            self._reported(self._assert_hints_are_inert, base,
                                           fx, f"{label}/static-roll"),
                            "the static roll moved under a client hint, so "
                            "hint-inertness could carry this test and it "
                            "would no longer pin oracle 1")
                        with self._perturbed(fx, fx["home"], fx["gid"],
                                             "substitute_enrolment"):
                            world = self._sweep(who, fx, specs, subjects)
                            moved = [k for k in base.diff(world)
                                     if k[1] == _STATIC_LEAK_NAME]
                            self.assertEqual(
                                [], moved,
                                "the injected roll CHANGED between the two "
                                "worlds, so it is not the snapshotted leak "
                                "this test is about and oracle 2 could "
                                "carry it")
                    finally:
                        self.probe = None
            finally:
                self._close(label, store)
            return   # the oracle's own behaviour, not a per-backend property

    def test_a_hint_selected_side_reddens_only_the_hint_assertion(self):
        """NEUTERING ``_assert_hints_are_inert`` MUST REDDEN THIS TEST.

        A registered route lets ``?team_id=`` choose which side's private
        roster-status enum the caller reads. It carries no identities and is
        perturbation-invariant, so both oracles are structurally blind to it
        — measured here — and hint-inertness is the only assertion that can
        see a query parameter selecting a side.

        A HINT-ONLY LEAK IS GENUINELY CONSTRUCTIBLE, which is worth stating
        because it was an open question: the assertion is not merely
        defensive, it defends a reachable shape. The FULL branch of
        ``_workflow_side`` really does answer whatever ``?team_id`` names,
        and this sweep's claim is that no scoped caller reaches it."""
        for label, store in self._stores():
            try:
                self._assert_backend(label, store)
                store.clear_all_data()
                fx = self._fixture(store)
                who = self._serve(fx)
                with _a_route_whose_hint_selects_a_side_for_a_scoped_caller():
                    self.probe = _HINT_LEAK_NAME
                    try:
                        specs, subjects = self._assert_inventory_is_closed(fx)
                        self.assertIn(
                            _HINT_LEAK_NAME, {s.name for s in specs},
                            "the injected route is not in the sweep's own "
                            "inventory, so nothing below is a statement "
                            "about the sweep")
                        base = self._sweep(who, fx, specs, subjects)
                        # THE ASSERTION UNDER TEST must report it.
                        hints = self._reported(
                            self._assert_hints_are_inert, base, fx,
                            f"{label}/hinted-side")
                        self.assertIsNotNone(
                            hints,
                            "HINT-INERTNESS DID NOT REPORT A ROUTE WHERE "
                            "?team_id SELECTS WHICH SIDE A SCOPED CALLER "
                            "READS. This is the assertion's only proof of "
                            "life: neither oracle can see a hint-selected, "
                            "identity-free, perturbation-invariant answer.")
                        self.assertIn(_HINT_LEAK_NAME, hints)
                        # THE OTHER TWO MUST BE BLIND.
                        self.assertIsNone(
                            self._reported(self._assert_no_foreign_ids, base,
                                           fx, f"{label}/hinted-side"),
                            "the hinted route leaked an identity, so oracle "
                            "1 could carry this test and it would no longer "
                            "pin hint-inertness")
                        with self._perturbed(fx, fx["home"], fx["gid"],
                                             "substitute_enrolment"):
                            world = self._sweep(who, fx, specs, subjects)
                            moved = [k for k in base.diff(world)
                                     if k[1] == _HINT_LEAK_NAME]
                            self.assertEqual(
                                [], moved,
                                "the injected route CHANGED between the two "
                                "worlds, so oracle 2 could carry this test "
                                "and it would no longer pin hint-inertness")
                    finally:
                        self.probe = None
            finally:
                self._close(label, store)
            return   # the assertion's own behaviour, not a per-backend one



# ---------------------------------------------------------------------------
# 6. THE PRINCIPAL AXIS FAILS CLOSED — AND THE TWO ROLES IT WAS MISSING.
#
# THE DEFECT THIS SECTION EXISTS FOR (#427 round 4, D1). The route axis has
# failed closed since round 1: `_assert_inventory_is_closed` takes its
# inventory from `route_registry.REGISTRY`, so a new authenticated route is an
# ERROR naming it. The PRINCIPAL axis had no such property. The only statement
# anywhere about which principals are swept was
#
#     self.assertEqual(sorted(entitlement), sorted(PRINCIPALS))
#
# and BOTH SIDES OF THAT EQUALITY CAME FROM THIS MODULE. `domain.Role` has
# SEVEN members; the sweep drove FIVE. `arena_manager` — a FOURTH role reading
# both sides in full, by design — and `viewer` appeared nowhere in the file,
# and neither carried a typed classification.
#
# The falsifier is in `PRINCIPAL_ROLES`' comment and is executed below.
# ---------------------------------------------------------------------------
class TheSweepCoversEveryDomainRole(_SweepHarness, unittest.TestCase):
    """A ROLE THE PRODUCT GAINS AND THIS SWEEP DOES NOT DRIVE IS AN ERROR
    NAMING IT — the property the route inventory already had."""

    def test_a_new_role_fails_this_test(self):
        swept = set(PRINCIPAL_ROLES.values())
        self.assertEqual(
            [], sorted(r.value for r in Role if r not in swept),
            "ROLE(S) THE PRODUCT HAS AND THIS SWEEP DOES NOT DRIVE. Every "
            "member of domain.Role must appear in PRINCIPAL_ROLES with a "
            "real session and a typed entitlement class, or a whole seat is "
            "unswept — which is how arena_manager and viewer went five "
            "rounds without either oracle ever reading a response of "
            "theirs.")
        # …and the map cannot rot in the other direction either.
        self.assertEqual(
            [], sorted(str(r) for r in swept if r not in set(Role)),
            "PRINCIPAL_ROLES names a role domain.Role no longer has")
        self.assertEqual(sorted(PRINCIPALS), sorted(PRINCIPAL_ROLES))

    def test_every_swept_principal_really_carries_that_role(self):
        """The map is a CLAIM about the sessions, so it is checked against
        what the server actually resolves rather than trusted. A principal
        whose account was seeded with a different role than the map records
        would make every classification below describe the wrong caller."""
        for label, store in self._stores():
            try:
                self._assert_backend(label, store)
                store.clear_all_data()
                fx = self._fixture(store)
                who = self._serve(fx)
                for principal, role in sorted(PRINCIPAL_ROLES.items()):
                    status, body = self._req(who[principal], "GET",
                                             "/api/auth/me")
                    self.assertEqual(status, 200, (principal, body))
                    resolved = (body.get("user") or {}).get("role")
                    self.assertEqual(
                        resolved, role.value,
                        f"[{label}] the {principal!r} session resolves "
                        f"{resolved!r}, but PRINCIPAL_ROLES records "
                        f"{role.value!r}")
            finally:
                self._close(label, store)
            return   # a property of the seeded sessions, not of a backend


class TheArenaManagerIsAnOperatorWithoutRosterAuthority(_SweepHarness,
                                                        unittest.TestCase):
    """:data:`UNSCOPED_OPERATOR_WITHOUT_ROSTER_AUTHORITY` is a MEASURED
    classification, not the assumption that an arena manager equals a league
    admin.

    Both halves matter. If the two roles were identical the class would be
    noise and could be folded away; if the arena manager were NOT two-sided,
    giving it both sides here would be silencing this sweep rather than
    describing the product."""

    #: ``{leaf: (arena_manager status, league_admin status)}`` — measured
    #: over real sessions on this tree. The two roles differ on exactly the
    #: two roster-workflow leaves, which is what ``MANAGE_ROSTER`` buys.
    MATRIX = {
        "board": (200, 200),
        "lineups": (200, 200),
        "roster": (200, 200),
        "roster-status": (200, 200),
        "substitutes": (200, 200),
        "officials": (200, 200),
        "reschedule": (200, 200),
        "availability-summary": (400, 400),
        "substitute-candidates": (403, 200),
        "substitute-addable": (403, 200),
    }

    def test_the_measured_matrix_is_still_what_the_class_claims(self):
        for label, store in self._stores():
            try:
                self._assert_backend(label, store)
                store.clear_all_data()
                fx = self._fixture(store)
                who = self._serve(fx)
                measured = {}
                for leaf in sorted(self.MATRIX):
                    row = []
                    for principal in ("arena", "operator"):
                        status, _body = self._req(
                            who[principal], "GET",
                            f"/api/games/{fx['gid']}/{leaf}")
                        row.append(status)
                    measured[leaf] = tuple(row)
                self.assertEqual(
                    self.MATRIX, measured,
                    f"[{label}] the arena_manager/league_admin matrix moved. "
                    f"This sweep types them as DIFFERENT principals with the "
                    f"SAME side entitlement; if they have become identical "
                    f"the class is noise, and if the arena manager's "
                    f"admission has changed its entitlement has to be "
                    f"re-decided rather than inherited.")
                # THE DIFFERENCE IS REAL, spelled as its own assertion so a
                # matrix edited to make them equal cannot pass quietly.
                differing = sorted(
                    leaf for leaf, (a, o) in measured.items() if a != o)
                self.assertEqual(
                    ["substitute-addable", "substitute-candidates"],
                    differing,
                    f"[{label}] an arena manager is no longer distinguishable "
                    f"from a league admin on this family, so "
                    f"{UNSCOPED_OPERATOR_WITHOUT_ROSTER_AUTHORITY} describes "
                    f"nothing")
            finally:
                self._close(label, store)
            return

    def test_the_two_sided_read_it_does_get_is_really_two_sided(self):
        """The entitlement this sweep hands it — BOTH sides — is the
        product's own answer, so it is read off a real response rather than
        assumed. Without this, ``frozenset({home, away})`` would be a way of
        switching both oracles off for a whole role."""
        for label, store in self._stores():
            try:
                self._assert_backend(label, store)
                store.clear_all_data()
                fx = self._fixture(store)
                who = self._serve(fx)
                status, body = self._req(
                    who["arena"], "GET", f"/api/games/{fx['gid']}/lineups")
                self.assertEqual(status, 200, body)
                for side in ("home", "away"):
                    self.assertFalse(
                        body[side]["restricted"],
                        f"[{label}] an arena manager's {side} side is "
                        f"RESTRICTED, so it is not the unscoped-operator "
                        f"read this sweep types it as: {body[side]}")
                    self.assertIsInstance(body[side]["players"], list, body)
                    self.assertTrue(body[side]["players"], body[side])
                self.assertIn(Role.ARENA_MANAGER,
                              lineup_visibility._UNSCOPED_OPERATORS,
                              "the product no longer classifies an arena "
                              "manager as an unscoped operator, so this "
                              "sweep's entitlement for it is stale")
            finally:
                self._close(label, store)
            return


@contextlib.contextmanager
def _the_viewer_admitted_to_the_operator_tuples():
    """THE TWO-LINE CHANGE, applied where the two lines are actually read.

    A real edit adds ``Role.VIEWER`` to ``lineup_visibility
    ._UNSCOPED_OPERATORS`` and to the operator tuple inside
    ``game_side_scope.resolve_private_game_read``. The first is a module
    global and is patched directly; the second is a tuple literal inside a
    function body, so the FUNCTION is wrapped instead — in every namespace
    that holds a reference to it, because ``web/scope.py`` and
    ``web/server.py`` both imported the name directly and patching one would
    leave the other admitting nobody new.

    The wrapper reproduces the edited branch exactly: a viewer is admitted
    with no side of their own, and the resolution still carries
    ``role=Role.VIEWER`` so every downstream projection sees the role the
    session really has."""
    targets = (srv, web_scope, game_side_scope)
    real_fns = [mod.resolve_private_game_read for mod in targets]
    real_ops = lineup_visibility._UNSCOPED_OPERATORS
    canonical = game_side_scope.resolve_private_game_read

    def widened(role, scope, game_id, store):
        if role == Role.VIEWER:
            return dataclasses.replace(
                canonical(Role.LEAGUE_ADMIN, scope, game_id, store),
                role=Role.VIEWER)
        return canonical(role, scope, game_id, store)

    for mod in targets:
        mod.resolve_private_game_read = widened
    lineup_visibility._UNSCOPED_OPERATORS = tuple(real_ops) + (Role.VIEWER,)
    try:
        yield
    finally:
        for mod, fn in zip(targets, real_fns):
            mod.resolve_private_game_read = fn
        lineup_visibility._UNSCOPED_OPERATORS = real_ops


class TheViewerIsEntitledToNothingAndTheSweepProvesIt(_SweepHarness,
                                                      unittest.TestCase):
    """:data:`VIEWER_ENTITLED_TO_NOTHING`, and the falsifier the whole
    backend missed."""

    def test_a_viewer_is_refused_every_leaf_of_the_private_game_family(self):
        """The product is CORRECT today, and this records that rather than
        assuming it: the value of the row is that it is now watched."""
        for label, store in self._stores():
            try:
                self._assert_backend(label, store)
                store.clear_all_data()
                fx = self._fixture(store)
                who = self._serve(fx)
                for leaf in ("board", "lineups", "roster", "roster-status",
                             "substitutes", "availability-summary",
                             "substitute-candidates", "substitute-addable",
                             "officials", "reschedule"):
                    status, body = self._req(
                        who["viewer"], "GET",
                        f"/api/games/{fx['gid']}/{leaf}")
                    self.assertEqual(
                        status, 403,
                        f"[{label}] a signed-in VIEWER was ADMITTED to the "
                        f"private-game leaf {leaf!r}. A viewer holds only "
                        f"Permission.VIEW and the "
                        f"{VIEWER_ENTITLED_TO_NOTHING} classification gives "
                        f"them no side of either game: {body}")
            finally:
                self._close(label, store)
            return

    def test_admitting_the_viewer_to_the_operator_tuples_reddens_this_sweep(
            self):
        """THE ACCEPTANCE BAR FOR D1, required to go RED.

        Adding ``Role.VIEWER`` to the two operator tuples hands a signed-in
        viewer both sides' full private lineups with ``restricted: false``.
        MEASURED at the head this corrects: the primary sweep stayed GREEN
        (``Ran 13 tests … OK``) and so did the provenance scanner, the read
        fence, the sibling-route suite and the overview suite; one test in
        the whole backend noticed, and it was in another module.

        Both halves are asserted. The PREMISE — the widening really did
        reach the wire — so this cannot pass because the patch did nothing;
        and the ORACLE — the sweep's own `_assert_no_foreign_ids`, not a
        re-implementation, must report it and must NAME the viewer."""
        for label, store in self._stores():
            try:
                self._assert_backend(label, store)
                store.clear_all_data()
                fx = self._fixture(store)
                who = self._serve(fx)
                specs, subjects = self._assert_inventory_is_closed(fx)
                with _the_viewer_admitted_to_the_operator_tuples():
                    # THE PREMISE: the two-line change really is live.
                    status, body = self._req(
                        who["viewer"], "GET",
                        f"/api/games/{fx['gid']}/lineups")
                    self.assertEqual(
                        status, 200,
                        f"[{label}] the falsifier did not admit the viewer, "
                        f"so nothing below is a statement about the sweep: "
                        f"{body}")
                    self.assertFalse(body["home"]["restricted"], body)
                    self.assertFalse(body["away"]["restricted"], body)
                    self.assertTrue(body["home"]["players"], body)
                    self.assertTrue(body["away"]["players"], body)
                    # THE ORACLE must report it.
                    sweep = self._sweep(who, fx, specs, subjects)
                    reported = None
                    try:
                        self._assert_no_foreign_ids(sweep, fx,
                                                    f"{label}/viewer")
                    except AssertionError as exc:
                        reported = str(exc)
                self.assertIsNotNone(
                    reported,
                    "THE PRIMARY SWEEP DID NOT CATCH A SIGNED-IN VIEWER "
                    "READING BOTH SIDES' FULL PRIVATE LINEUPS. This is D1 "
                    "exactly: a two-line change of the shape this PR series "
                    "exists to prevent, invisible to the sweep because the "
                    "role was not a principal.")
                self.assertIn("viewer", reported)
            finally:
                self._close(label, store)
            return   # the oracle's own behaviour, not a per-backend property



# ---------------------------------------------------------------------------
# 7. ORACLE 1'S FORBIDDEN SET IS EVERY IDENTITY PRIVATE TO A SIDE — AND THE
#    ORACLE ITSELF GOES RED ON A CANDIDATE-POOL LEAK.
#
# THE DEFECT THIS SECTION EXISTS FOR (#427 round 4, D3). The forbidden set was
# `durable_game_sides` alone, whose only two authorities are
# `GameRosterEntry.attribution[0]` and `SubstituteEnrollment.team_id`. An
# ELIGIBLE-BUT-UNSELECTED CANDIDATE has neither, so no candidate identity was
# in ANY principal's forbidden set — and "both sides' private candidate lists"
# is how the owner described the original blocker's own subject matter. The
# primary protection could not see a leak of exactly the population it was
# built for.
#
# The structure is the official's and the guardian's, reused rather than
# reinvented: a registered-route injection driving the PRIMARY oracle, and the
# replaced rule re-measured in the same test so the result cannot be
# accidental.
# ---------------------------------------------------------------------------

_POOL_PROBE_TEMPLATE = "/api/sweep-probe-pool/{}"
_POOL_PROBE_NAME = "get_sweep_probe_pool_id"
_POOL_PROBE_SPEC = route_registry.RouteSpec(
    "GET", r"^/api/sweep-probe-pool/[^/]+$", _POOL_PROBE_TEMPLATE,
    _POOL_PROBE_NAME, "_dispatch_get", kind="route", auth="session",
    scope_axis="none",
    note="injected by test_authenticated_side_noninterference; never shipped")


@contextlib.contextmanager
def _a_registered_route_serving_a_side_s_candidate_pool(third_team_id):
    """A REAL, REGISTERED authenticated GET route that hands the coach of a
    team playing in NEITHER game — typed :data:`IN_NEITHER_SIDE`, entitled to
    nothing of either side — the HOME side's SERVED CANDIDATE POOL, and
    answers everyone else with nothing.

    WHY THE OTHER TWO ASSERTIONS ARE STRUCTURALLY BLIND TO IT, by
    construction, so this is a statement about ORACLE 1 alone:

    * SNAPSHOTTED. The pool is computed once, on first request, and cached
      forever, so the two worlds return byte-identical bodies and
      ``_assert_non_interference`` sees no diff.
    * HINT-INDEPENDENT. The query string is never read, so
      ``_assert_hints_are_inert`` sees nothing either.

    THE POOL IS TAKEN FROM THE SAME PRODUCTION READ THE FAMILY SERVES —
    ``ApiService._lineup_rows`` — and then filtered to the identities
    ``durable_game_sides`` cannot attribute, so the probe carries ONLY the
    population the replaced forbidden set was blind to. A probe that also
    carried a durably-seated id would have been caught by the OLD oracle and
    would prove nothing about the new one."""
    real_registry = route_registry.REGISTRY
    real_dispatch = srv.Handler._dispatch_get
    snapshot = {}

    def dispatch(self):
        path = self.path.split("?", 1)[0]
        match = re.match(r"^/api/sweep-probe-pool/([^/]+)$", path)
        if match is None:
            return real_dispatch(self)
        role, scope, _user_id, err = self._resolve_role()
        if err is not None:
            code, payload = err
            return self._send_json(payload, code)
        if role != Role.COACH or (scope or {}).get("team_id") != third_team_id:
            return self._send_json({"secret": None})
        gid = match.group(1)
        api = srv.STATE.api
        game = api.store.get_game(gid)
        if game is None or not game.home_team_id:
            return self._send_json({"secret": None})
        if gid not in snapshot:
            durable = RosterService(api.store).durable_game_sides(gid)
            snapshot[gid] = sorted(
                row["id"] for row in api._lineup_rows(game, game.home_team_id)
                if row["id"] not in durable)
        return self._send_json({"secret": {"pool": snapshot[gid]}})

    route_registry.REGISTRY = real_registry + (_POOL_PROBE_SPEC,)
    srv.Handler._dispatch_get = dispatch
    try:
        yield
    finally:
        route_registry.REGISTRY = real_registry
        srv.Handler._dispatch_get = real_dispatch


class TheForbiddenSetIsEverySidePrivateIdentity(_SweepHarness,
                                                unittest.TestCase):
    """The three proofs oracle 1 was missing."""

    probe_registered = False

    def _route_subjects(self, fx):
        subjects = super()._route_subjects(fx)
        if self.probe_registered:
            subjects[_POOL_PROBE_NAME] = [(fx["gid"],)]
        return subjects

    @contextlib.contextmanager
    def _probe(self, fx):
        with _a_registered_route_serving_a_side_s_candidate_pool(fx["third"]):
            self.probe_registered = True
            try:
                yield
            finally:
                self.probe_registered = False

    # -- PROOF (a) --------------------------------------------------------
    def test_a_registered_candidate_pool_route_fails_the_primary_sweep(self):
        """THE OWNER'S EXPERIMENT, AIMED AT THE CANDIDATE POOL, required to
        go RED.

        AND THE FALSIFIER, in the same test and NOT routed through the
        oracle: every identity the probe serves is measured to be absent
        from ``durable_game_sides``, so the forbidden set this round replaces
        was structurally incapable of holding any of them. That is the
        reproduction, executable — the test cannot go green because the
        probe happened to leak something the old rule already covered.

        The falsifier is asserted on the SETS rather than by re-running the
        oracle with a durable-only source on purpose: `_assert_no_foreign_ids`
        now carries premise assertions that the widening is live, so a
        durable-only run would raise for the premise rather than for the
        leak and the "still green" reading would be a lie."""
        for label, store in self._stores():
            try:
                self._assert_backend(label, store)
                store.clear_all_data()
                fx = self._fixture(store)
                who = self._serve(fx)
                with self._probe(fx):
                    specs, subjects = self._assert_inventory_is_closed(fx)
                    self.assertIn(
                        _POOL_PROBE_NAME, {spec.name for spec in specs},
                        "the injected route is not in the inventory the "
                        "sweep builds itself from, so nothing below is a "
                        "statement about the sweep")
                    status, body = self._req(
                        who["thirdcoach"], "GET",
                        f"/api/sweep-probe-pool/{fx['gid']}")
                    self.assertEqual(status, 200, body)
                    pool = frozenset(body["secret"]["pool"])
                    # THE PREMISE: the probe really did hand over a pool.
                    self.assertTrue(
                        pool,
                        "the injected route served no candidate identities, "
                        "so it is not the leak this test is about")
                    # THE FALSIFIER: the replaced rule could not hold any of
                    # them.
                    durable = (self._durable_ids(fx, fx["home"])
                               | self._durable_ids(fx, fx["away"]))
                    self.assertEqual(
                        frozenset(), pool & frozenset(durable),
                        "the injected pool contains a DURABLY ATTRIBUTED "
                        "id, so the forbidden set this round replaces would "
                        "already have caught it and this test measures "
                        "nothing about the widening")
                    # …and the shipped rule does hold them.
                    private, ambiguous = self._private_side_ids(fx)
                    self.assertEqual(frozenset(), ambiguous)
                    self.assertLessEqual(
                        pool, private[fx["home"]],
                        "the served candidate pool is not in HOME's private "
                        "population, so `_private_side_ids` is not reading "
                        "the population the routes actually serve")
                    # THE ORACLE must report it, naming the route and the
                    # caller.
                    sweep = self._sweep(who, fx, specs, subjects)
                    reported = None
                    try:
                        self._assert_no_foreign_ids(sweep, fx, f"{label}/pool")
                    except AssertionError as exc:
                        reported = str(exc)
                    self.assertIsNotNone(
                        reported,
                        "THE PRIMARY SWEEP DID NOT CATCH A REGISTERED ROUTE "
                        "HANDING A COACH OF NEITHER TEAM ONE SIDE'S PRIVATE "
                        "CANDIDATE POOL. That population is this blocker's "
                        "own subject matter, and the sweep is the primary "
                        "protection: it must fail here before anything "
                        "supplemental is consulted.")
                    self.assertIn(_POOL_PROBE_NAME, reported)
                    self.assertIn("thirdcoach", reported)
                    # …and the OTHER two assertions are blind, so this test
                    # can only go red for the reason it claims.
                    try:
                        self._assert_hints_are_inert(sweep, fx,
                                                     f"{label}/pool")
                    except AssertionError as exc:      # pragma: no cover
                        self.fail(f"the pool probe moved under a client "
                                  f"hint, so hint-inertness could carry this "
                                  f"test: {exc}")
                    base = sweep
                    with self._perturbed(fx, fx["home"], fx["gid"],
                                         "substitute_enrolment"):
                        world = self._sweep(who, fx, specs, subjects)
                        self.assertEqual(
                            [], [k for k in base.diff(world)
                                 if k[1] == _POOL_PROBE_NAME],
                            "the injected pool CHANGED between the two "
                            "worlds, so oracle 2 could carry this test and "
                            "it would no longer pin oracle 1")
            finally:
                self._close(label, store)
            return   # the oracle's own behaviour, not a per-backend property

    # -- PROOF (b) --------------------------------------------------------
    def test_no_swept_route_serves_an_identity_outside_the_forbidden_set(
            self):
        """THE AUDIT, RE-RUN EVERY TIME RATHER THAN RECORDED AS DONE.

        The candidate pool was found by asking "what does ``_lineup_rows``
        return that ``durable_game_sides`` does not know about?". The same
        question has to hold for every OTHER population the routes serve, and
        the honest way to keep it holding is to measure it rather than to
        write a comment saying it was checked once.

        Every player identity that appears anywhere on the swept surface must
        be in one of the two sides' private populations — or be one of the
        identities the product itself REFUSES to attribute to a side, which
        is a closed, named list rather than a residue.

        WHAT THE MEASURED RESIDUE IS, and why it is a ruling rather than a
        hole. Four fixture players sit outside both populations, and all four
        are the shapes the standing "legacy NULL attribution is omitted,
        never guessed" ruling puts there: ``Orphan Seat`` and ``Orphan Sub``
        (durable rows that cannot name their owner) and ``Departed Player``
        and ``Pointer Ghost`` (a permanent pointer and no seasonal
        membership). Measured: they reach ONLY the unscoped operator — who is
        entitled to both sides anyway — on ``/board``, ``/roster``,
        ``/substitutes`` and the operator-only ``/api/players``. An identity
        the system declines to attribute to a side cannot be placed in a
        side's forbidden set without guessing, which is the one thing
        ``durable_game_sides`` exists not to do; so the rule is that such an
        identity may reach an operator and nobody else, and THAT is what is
        asserted."""
        for label, store in self._stores():
            try:
                self._assert_backend(label, store)
                store.clear_all_data()
                fx = self._fixture(store)
                who = self._serve(fx)
                specs, subjects = self._assert_inventory_is_closed(fx)
                sweep = self._sweep(who, fx, specs, subjects)
                private, ambiguous = self._private_side_ids(fx)
                self.assertEqual(frozenset(), ambiguous)
                known = private[fx["home"]] | private[fx["away"]]
                unattributed = {}
                for (principal, route, path, hint), (_st, body) in \
                        sweep.rows.items():
                    blob = json.dumps(body, sort_keys=True, default=str)
                    for pid in set(re.findall(r"\bplayer_\d+\b", blob)):
                        if pid not in known:
                            unattributed.setdefault(
                                principal, set()).add((route, pid))
                # THE PREMISE: the residue is real on this fixture, or this
                # test asserts nothing about it.
                self.assertTrue(
                    unattributed,
                    f"[{label}] no unattributable identity reaches any "
                    f"route, so this audit is vacuous — the fixture no "
                    f"longer models the legacy-NULL shapes the omission "
                    f"ruling is about")
                both = frozenset({fx["home"], fx["away"]})
                entitlement = self._entitlement(fx)
                short = sorted(p for p in unattributed
                               if entitlement[p][1] != both)
                self.assertEqual(
                    [], short,
                    f"[{label}] an identity that belongs to NEITHER side's "
                    f"private population reached a caller who is NOT "
                    f"entitled to both sides: "
                    f"{ {p: sorted(unattributed[p]) for p in short} }. Such "
                    f"an identity is in no forbidden set — the product "
                    f"declines to attribute it to a side and this sweep will "
                    f"not guess — so the ONLY callers who may receive one "
                    f"are those entitled to both sides anyway. A "
                    f"side-scoped principal here is a real disclosure that "
                    f"oracle 1 is structurally unable to report.")
                # …and the rule is not satisfied by nobody receiving them.
                self.assertEqual(
                    ["arena", "operator"], sorted(unattributed),
                    f"[{label}] the set of callers receiving an "
                    f"unattributable identity moved to "
                    f"{sorted(unattributed)}. Both of today's are unscoped "
                    f"operators; a change here means the residue has moved "
                    f"and the ruling above has to be re-decided rather than "
                    f"inherited.")
            finally:
                self._close(label, store)
            return

    # -- PROOF (c): THE WIDENING DID NOT SWALLOW THE OFFICIAL'S NARROWING --
    def test_the_official_is_still_forbidden_a_candidate_on_their_own_routes(
            self):
        """Widening the forbidden set to the whole private population would
        have handed the official the candidate pool as PERMITTED on the three
        routes their grant covers — closing D3 by opening the same hole one
        seat over, which is this round's recurring shape.

        :meth:`_submitted_side_ids` is what prevents that, so it is measured:
        a candidate identity is in HOME's private population, is NOT on
        HOME's submitted sheet, and is therefore forbidden to the official on
        ``/board``, ``/lineups`` and ``/roster`` — the routes where they are
        entitled to the sheet and to nothing else."""
        for label, store in self._stores():
            try:
                self._assert_backend(label, store)
                store.clear_all_data()
                fx = self._fixture(store)
                self._serve(fx)
                private, _ambiguous = self._private_side_ids(fx)
                submitted = self._submitted_side_ids(fx)
                unsubmitted = private[fx["home"]] - submitted[fx["home"]]
                self.assertTrue(
                    unsubmitted,
                    f"[{label}] every HOME private identity occupies a slot, "
                    f"so this test cannot distinguish the sheet from the "
                    f"side")
                self.assertTrue(
                    submitted[fx["home"]],
                    f"[{label}] HOME's submitted sheet is empty, so "
                    f"'entitled to the sheet' is vacuous")
                for route in sorted(OFFICIAL_ASSIGNED_GAME_ROUTES):
                    permitted = self._permitted_ids(
                        fx, "official", route, private, submitted)
                    self.assertEqual(
                        frozenset(), unsubmitted & permitted,
                        f"[{label}] on {route} the official is PERMITTED "
                        f"{sorted(unsubmitted & permitted)} — HOME "
                        f"identities that do not occupy a slot. Their grant "
                        f"is the submitted sheet, not the side's whole "
                        f"private population, and widening the forbidden set "
                        f"must not have quietly widened their permit.")
                    self.assertLessEqual(
                        submitted[fx["home"]], permitted,
                        f"[{label}] on {route} the official is no longer "
                        f"permitted HOME's own submitted sheet, so the grant "
                        f"has gone dead")
            finally:
                self._close(label, store)
            return


# ---------------------------------------------------------------------------
# 7b. THE GUARDIAN'S GRANT IS ROW-SPECIFIC, NOT ONLY ROUTE-SPECIFIC.
#
# THE DEFECT THIS SECTION EXISTS FOR (#427 round 4, D2). Round 3 confined the
# guardian to `GUARDIAN_JUNIOR_ROUTES` and wrote, in the class comment, that
# the grant is "the junior's own row… Not a standing grant over the junior's
# whole team". Oracle 1 did not implement that: on those two routes it
# permitted the junior's WHOLE SIDE. MEASURED with payload on the head this
# corrects — widening production `ApiService.get_guardian_home` to also carry
# every durably attributed AWAY identity returned, over a real session, 200
# with three identities that are NOT the junior, and ALL THIRTEEN TESTS STAYED
# GREEN.
#
# A comment asserting a rule the code does not implement is the accumulating-
# exemption shape the owner prohibited, so the code is narrowed to match the
# comment rather than the comment softened to match the code. Measured across
# the whole swept surface, the guardian receives exactly ONE player identity
# anywhere — the junior, on `get_me_guardian_home` — so the narrowing costs
# the real grant nothing, which is why this is closed rather than documented.
# ---------------------------------------------------------------------------
@contextlib.contextmanager
def _guardian_home_widened_to_the_whole_side(extra_ids):
    """Production ``ApiService.get_guardian_home`` also carries every
    durably attributed identity of the junior's side — the payload form of
    "a standing grant over the junior's whole team"."""
    real = _ApiService.get_guardian_home

    def widened(self, *args, **kwargs):
        out = real(self, *args, **kwargs)
        if isinstance(out, dict):
            out = dict(out, whole_side_ids=sorted(extra_ids))
        return out

    _ApiService.get_guardian_home = widened
    try:
        yield
    finally:
        _ApiService.get_guardian_home = real


class TheGuardianGrantIsRowSpecific(_SweepHarness, unittest.TestCase):
    """The proof the guardian's identity grant was missing, and the proof the
    narrowing does not break the real product grant."""

    def test_a_guardian_home_carrying_the_whole_side_fails_the_primary_sweep(
            self):
        """Required to go RED, with the falsifier asserted on the sets so it
        cannot be circular."""
        for label, store in self._stores():
            try:
                self._assert_backend(label, store)
                store.clear_all_data()
                fx = self._fixture(store)
                who = self._serve(fx)
                specs, subjects = self._assert_inventory_is_closed(fx)
                junior = fx["guardian_junior_id"]
                private, _ambiguous = self._private_side_ids(fx)
                submitted = self._submitted_side_ids(fx)
                others = private[fx["away"]] - {junior}
                self.assertTrue(
                    others,
                    f"[{label}] the junior is the only private identity on "
                    f"their side, so 'the junior's row, not the whole side' "
                    f"is vacuous")
                # THE FALSIFIER: the rule this round replaces — the junior's
                # WHOLE SIDE permitted on the junior's routes — would not
                # have forbidden any of them.
                whole_side = frozenset(others | {junior})
                for route in sorted(GUARDIAN_JUNIOR_ROUTES):
                    permitted = self._permitted_ids(
                        fx, "guardian", route, private, submitted)
                    self.assertEqual(
                        frozenset({junior}), permitted,
                        f"[{label}] on {route} the guardian is permitted "
                        f"{sorted(permitted)}, not exactly the junior's own "
                        f"identity")
                    self.assertLessEqual(
                        others, whole_side,
                        "the falsifier's whole-side permit does not contain "
                        "the identities the shipped rule forbids, so this "
                        "test is not measuring the change it claims to")
                with _guardian_home_widened_to_the_whole_side(others):
                    # THE PREMISE: the widening really reached the wire.
                    status, body = self._req(who["guardian"], "GET",
                                             "/api/me/guardian/home")
                    self.assertEqual(status, 200, body)
                    self.assertEqual(sorted(others),
                                     body.get("whole_side_ids"), body)
                    sweep = self._sweep(who, fx, specs, subjects)
                    reported = None
                    try:
                        self._assert_no_foreign_ids(
                            sweep, fx, f"{label}/guardian-row")
                    except AssertionError as exc:
                        reported = str(exc)
                self.assertIsNotNone(
                    reported,
                    "THE PRIMARY SWEEP DID NOT CATCH A GUARDIAN ROUTE "
                    "CARRYING THE JUNIOR'S WHOLE SIDE. The class comment has "
                    "claimed since round 3 that the grant is the junior's "
                    "own row; this is the assertion that makes that true of "
                    "the code and not only of the prose.")
                self.assertIn("guardian", reported)
                self.assertIn("get_me_guardian_home", reported)
            finally:
                self._close(label, store)
            return

    def test_the_real_guardian_grant_still_works_over_authenticated_http(
            self):
        """NARROWING MUST NOT COST THE PRODUCT ANYTHING, so what the guardian
        is actually FOR is exercised over a real session rather than
        reasoned about: the junior's own Player Home row, and the junior's
        substitute opportunities.

        This is the half that would have forced the residual to be documented
        instead of closed, if it had failed."""
        for label, store in self._stores():
            try:
                self._assert_backend(label, store)
                store.clear_all_data()
                fx = self._fixture(store)
                who = self._serve(fx)
                junior = fx["guardian_junior_id"]
                status, body = self._req(who["guardian"], "GET",
                                         "/api/me/guardian/home")
                self.assertEqual(status, 200, body)
                juniors = body.get("juniors") or []
                self.assertEqual(
                    [junior], [j.get("player_id") for j in juniors],
                    f"[{label}] the guardian's Player Home no longer carries "
                    f"exactly their junior's row: {body}")
                self.assertIsNotNone(
                    juniors[0].get("next_game"),
                    f"[{label}] the junior's Player Home row carries no "
                    f"next_game, so the grant this sweep permits is empty "
                    f"and 'the junior's own row' would be satisfied "
                    f"vacuously: {juniors[0]}")
                status, body = self._req(
                    who["guardian"], "GET",
                    f"/api/me/guardian/{junior}/substitute-opportunities/"
                    f"{fx['gid']}")
                self.assertEqual(
                    status, 200,
                    f"[{label}] the junior's substitute-opportunities route "
                    f"no longer answers their guardian: {body}")
                # …and the narrowing really is what is being tested: the
                # response carries the junior and no other identity of that
                # side.
                private, _ambiguous = self._private_side_ids(fx)
                for path in ("/api/me/guardian/home",
                             f"/api/me/guardian/{junior}/"
                             f"substitute-opportunities/{fx['gid']}"):
                    _st, payload = self._req(who["guardian"], "GET", path)
                    blob = json.dumps(payload, sort_keys=True, default=str)
                    got = {pid for pid in private[fx["away"]]
                           if re.search(rf"\b{re.escape(pid)}\b", blob)}
                    self.assertLessEqual(
                        got, {junior},
                        f"[{label}] GET {path} carries identities of the "
                        f"junior's side other than the junior: "
                        f"{sorted(got - {junior})}")
            finally:
                self._close(label, store)
            return


if __name__ == "__main__":
    unittest.main()
