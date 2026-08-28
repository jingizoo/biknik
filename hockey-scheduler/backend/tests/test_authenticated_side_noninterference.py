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
fixture, or of a gate, none of which is a per-backend property. MEASURED off
the module's own AST rather than eyeballed, so the sentence is a count and
not an impression: exactly SEVEN test methods in this file loop every
configured backend and call ``_assert_matrix_ran``. The first is the
whole-surface property above. The second is
:meth:`TheHintAxisIsClosedAgainstWhatTheServerReads
.test_no_parameter_the_server_reads_selects_a_side`, added in round 9, which
carries the FULL query-parameter matrix on one fresh world per backend
because carrying it in all twenty-four worlds is not affordable — see
:data:`HINTS`. Three more are the whole of
:class:`AGameKeyedGrantDoesNotSpanASecondGame`, added in round 10, which the
owner required on all three backends by name. The sixth is
:meth:`ThePlayerGrantIsTheEligibleMembershipRow
.test_the_ex_member_is_refused_and_a_blind_gate_reddens_the_sweep`, added in
round 12, which LB1's close condition 4 required on all three backends by
name. The seventh is :meth:`NoPrincipalEntitledToNoSideIsAdmittedToTheFamily
.test_no_principal_entitled_to_no_side_is_admitted_to_any_leaf`, added in
round 17 — the ADMISSION MATRIX, on every backend because what a leaf
answers a caller entitled to no side is a statement about the product over
real HTTP and not about any gate's text. (It said ONE until round 9, TWO
until round 10, FIVE until round 12 and SIX until round 17. The count is
stated because the headline sentence is easy to read as a claim about every
test in the file, and it is MEASURED off this module's own AST: seven
methods contain both ``_stores()`` and ``_assert_matrix_ran``. NOTHING
ENFORCES IT — no test in this file re-derives that number — so it is prose
and has to be re-measured by hand by whoever moves it.)

WHERE THE ENUMERATION KEEPS MOVING TO, AND WHY THIS ROUND IS ONE LEVEL UP
AGAIN (#427 round 12, LB1). Round 11 derived every grant DIMENSION from the
product row and left the SET OF GRANT ROWS hand-written in
:data:`GRANT_ROWS`, audited by nothing — so the enumeration moved up one
level and the hole moved with it. ``SeasonRosterMembership``, the row the
whole of #205 exists to make authoritative, was simply absent from it: Coach
and Player were ONE entitlement class whose comment said "there is no
per-game row to key on", which is true of a Coach and false of a Player. The
answer is not a third entry in that map — it is that the ADMISSION BRANCHES
are now derived from the GATE (:func:`admission_branches`), so a
non-operator branch with no authority behind it fails by name. See the
``admission branch`` row of the axis table below.

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
**1. Identity leak.** For every response, NO WAY OF NAMING a person private to
a side the caller is NOT entitled to may appear anywhere in the serialized
body. WORD-BOUNDARY matched, never a substring: ``player_1`` is a prefix of
``player_12``, and a plain ``in`` test reported the away Coach's own
``player_12`` as a leak of the home side's ``player_1`` — a false positive
that made an earlier version of this matrix untrustworthy in BOTH directions.

"NO WAY OF NAMING" AND NOT "NO ID" (#427 round 9, D8). The alphabet was the
``id`` FIELD alone until this round, and a name is an identity too: a
registered route handing a coach of NEITHER team the AWAY side's five private
people BY NAME passed all three oracles green. It is now every string field
of the ``Player`` RECORD — derived by ``dataclasses.fields``, so a new
identity field enters with no edit here — minus values two people share, and
with permitted names EXCISED FROM THE BODY BEFORE THE SEARCH, because word
boundaries do not separate ``Legacy Sub`` from ``Away Legacy Sub``. See
:meth:`_SweepHarness._identity_tokens`, :meth:`_SweepHarness._tokens_of` and
:meth:`_SweepHarness._redact`.

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

A THIRD QUESTION, ASKED OF THE STATUS: WAS THIS CALLER ADMITTED AT ALL
=====================================================================
(#427 round 17. This is NOT a third oracle in the sense the rest of this
docstring uses "the two oracles" and "all three assertions" — those still
mean the two data oracles and hint-inertness, and every sentence about them
is unchanged. It is a separate, narrower assertion with its own section 6b.)

BOTH DATA ORACLES ASK WHETHER PRIVATE DATA REACHED AN UNENTITLED CALLER, so
both are STRUCTURALLY BLIND to an ADMISSION THAT CARRIES LITTLE OR NO DATA —
and that blindness is measured, not feared. One branch added to
``resolve_private_game_read`` ahead of its COACH/PLAYER branch, admitting a
Coach whose scope names NEITHER side while filling neither grant-bearing
field (``game=None``, ``own_team=None``), is exempt from needing an
authority for the not-found passthrough's own reason. MEASURED at 6641fe7:
``_audit()`` returns ``[]``, THE WHOLE MODULE STAYS GREEN — ``Ran 91 tests
in 195.9s … OK`` — and ``thirdcoach``, a Coach of a team in NEITHER game,
receives 200 on ``/board``, ``/lineups``, ``/officials`` and ``/reschedule``
ON BOTH GAMES, carrying the officiating assignment's id and name. Both sides
come back ``restricted: true, players: null``, which is exactly why nothing
here sees it: no foreign identity to find, nothing that moves.

So the admission matrix asks a question the payload cannot answer for:
EVERY ``(principal, GAME)`` CELL whose entitled-team set NARROWED AGAINST
THAT GAME is empty must be answered ``403`` on EVERY leaf of the
private-game family. Both axes are DERIVED — the cells from
:meth:`_SweepHarness._subject_narrowed`, i.e. from the RELATIONSHIP ROWS and
their declared active state; the leaves from ``route_registry.REGISTRY`` via
:func:`private_game_family` — and each is proved to fail closed by ADDING a
member and requiring a named failure.

IT WAS A PRINCIPAL AXIS UNTIL ROUND 18 AND THAT WAS THE HOLE. Selecting
principals by ``_entitlement``'s GLOBAL team union can only ever select a
caller entitled to nothing ANYWHERE, so the swept OFFICIAL — whose map says
``{home, away}`` although the grant is assignment-keyed PER GAME — was
outside the matrix on the game they do not referee. Measured at ``73faf4d``
with ``200 {}`` answered to that official on the UNASSIGNED game's
``/officials`` and ``/reschedule``, the whole module passed:
``Ran 95 tests in 297.042s … OK``, tri-store. MEASURED over a battery of ten hostile
spellings of that exemption class, every one of them a live admission: the
matrix reports 10 of 10, and 8 of the 10 leave ``_audit()`` empty.

ITS HONEST BOUNDARY, MEASURED TOO: an escape that serves an ENTITLED
principal the OPPONENT's side never moves a status, so the matrix stays
GREEN on it and the DATA oracles are what report it — driven, with the home
Coach handed the away side: admission matrix green, oracle 1 red naming
``Away Legacy Sub`` on ``/availability-summary``. The two questions are
complementary and neither subsumes the other.

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

…AND PER GAME, BECAUSE ONE OF THE THREE GRANTS IS ISSUED AGAINST A GAME
======================================================================
Round 10 found that the narrowing above was still a statement about a
principal and a route and never about WHICH GAME. An
``OfficialAssignment`` names one game; the two swept games share both teams
with the sides swapped; and both oracles were reading a number computed
across the pair. :meth:`_submitted_side_ids` UNIONED each side's occupants
over both games, so an official assigned to the first was permitted an
identity that occupies a slot only in the second; and
:meth:`_assert_non_interference` was handed the perturbed TEAM and never the
perturbed GAME, so a response about the assigned game could vary with the
other game's sheet and never be an offender. Both are now keyed by game —
see :meth:`_grant_spans`, :data:`GRANT_RECORD_FIELDS` and
:class:`AGameKeyedGrantDoesNotSpanASecondGame` — and WHICH principals that
rule applies to is derived from the domain rows their grants live in rather
than listed, which :class:`NoGrantIsAggregatedAcrossADimensionItIsKeyedOn`
asserts in both directions.

…AND PER EVERY OTHER DIMENSION THOSE ROWS CARRY, WHICH IS THE ACTUAL RULE
========================================================================
Round 11 read the rest of the same two rows. ``GRANT_RECORD_FIELDS`` had been
reading their field NAMES since round 10 and round 10 used exactly one of
them, ``game_id``; three more instances of the same species were in the
others.

* the GUARDIAN's grant was aggregated across JUNIORS exactly as the
  official's had been across games. :meth:`_SweepHarness._permitted_ids`
  returned the constant ``fx["guardian_junior_id"]`` and never received the
  junior the PATH named — and both swept bindings for that path were the
  guardian's own junior, so the unentitled direction was never swept at all.
* ``OfficialAssignment.status``. The row declares it, the product declares
  what it means (``OfficialAssignmentStatus.is_active``: "Proposed or
  accepted assignments hold the official's time"), and four other consumers
  honour it. The read gate did not, and neither did this file:
  ``_official_is_assigned`` reproduced the gate's predicate VERBATIM, so the
  expectation widened in lockstep with the behaviour — the exact failure the
  comment above :meth:`_SweepHarness._subject_narrowed` says it avoids.
  MEASURED: the swept official DECLINED through ``respond_assignment``, kept
  200 on ``/board``, ``/lineups`` and ``/roster``, and the primary sweep
  passed on all three backends — ``Ran 1 test in 110.641s … OK``.
* ``GuardianLink.guardian_user_id``. The oracle asked "does this JUNIOR have
  a verified link", not "does THIS GUARDIAN have one", so another guardian's
  link granted the swept one.

The rule is therefore no longer "key on the game" but A GRANT IS KEYED BY
EVERY DIMENSION THE PRODUCT ROW THAT STORES IT CARRIES — the rows it names
(:func:`_subject_fields`) and the activation state it declares about itself
(:func:`_activation_fields`), both derived from the record. Every field of
every grant row is now either a derived dimension or carries a typed reason
in :data:`GRANT_FIELDS_THAT_KEY_NOTHING`, and
:class:`TheGrantIsKeyedByEveryDimensionOfItsRow` MEASURES the partition in
both directions per field: moving a field in the store must change what the
oracles grant if and only if it is a declared dimension. A grant row that
gains a column is an ERROR NAMING IT.

The gate was wrong too, and is fixed rather than described:
``game_side_scope.resolve_private_game_read`` now requires
``a.status.is_active``, so a DECLINED official is refused the private-game
family — see ``docs/architecture/api-contract.md``.

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

EVERY AXIS OF THIS SWEEP, AND THE AUTHORITY IT IS CLOSED AGAINST
================================================================
Four consecutive rounds each found a hole in this file, and each hole was in
whichever axis was NOT closed against an authority outside it. The route axis
has never been the hole, because it has been derived from
``route_registry.REGISTRY`` since round 1. So the axes are enumerated here —
all of them, including the ones nothing has tripped over yet — with what each
is closed against, because "close the axis somebody just found" is what the
last four rounds did.

WHAT THE REQUEST VARIES

=====================  ==========  ==================================
axis                   status      authority / disclosed limit
=====================  ==========  ==================================
route                  CLOSED      ``route_registry.REGISTRY``
principal (role)       CLOSED      ``domain.Role``
session scope          CLOSED      ``AccountService._ALLOWED_SCOPE_KEYS``
query parameter        CLOSED      ``route_extract.query_parameter_names``
subject (what the      **LIMIT**   entitlement recomputed from the store's
path names)                        own rows, matched on EVERY DIMENSION the
                                   grant row declares — derived from the
                                   record by ``GRANT_DIMENSIONS``, with the
                                   partition against
                                   ``GRANT_FIELDS_THAT_KEY_NOTHING`` measured
                                   in both directions per field, and every
                                   path-varied dimension swept in the
                                   UNENTITLED direction too. Still a LIMIT
                                   because the REVOCATION KINDS remain a test
                                   construction (see ``relationship kind``)
backend                CLOSED      ``_assert_matrix_ran``: a skip is not a pass
HTTP method            **LIMIT**   GET only; the VOCABULARY is closed
admission branch       CLOSED      ``admission_branches()`` — the GATE'S OWN
                                   SOURCE. #427 round 12, LB1: the set of
                                   branches that admit a caller to a private
                                   game is DERIVED from
                                   ``services/game_side_scope.py`` the way
                                   ``query_parameter_names`` derives the
                                   query axis from ``server.py``, and every
                                   non-operator branch must carry an entry
                                   in ``ADMISSION_AUTHORITIES``. Proved by
                                   INJECTION, in forty-eight spellings; the
                                   statement walk that derives them is an
                                   ALLOW-LIST, so a statement kind it cannot
                                   attribute is refused by name rather than
                                   skipped (#427 round 13 — a ``match`` arm
                                   was); and the SIX MODELS the walk rests
                                   on each fail closed on input they cannot
                                   read (#427 round 14) — a binding a nested
                                   block may have changed is REFUSED; what a
                                   branch GRANTS is derived from what
                                   ``web/server.py`` and ``web/scope.py``
                                   actually read off the record; a function
                                   that rebinds its own role parameter is
                                   REFUSED; a branch that DELEGATES is
                                   pinned on what it itself tests and
                                   returns, not only on what the resolver
                                   answers; it is excused by the resolver
                                   only where it RESTS ON that answer; and
                                   the carrier must have exactly ONE
                                   module-level definition, because Python
                                   binds the last and this read the first.
                                   #427 round 15: the pin FOLLOWS the call
                                   a decision rests on (``_inlined``) into
                                   the product predicate that takes it, so
                                   what is pinned is what is DECIDED and
                                   never a function name — following is an
                                   allow-list too, refusing by name a callee
                                   outside the product package, bound twice,
                                   containing a statement the fold cannot
                                   read, able to fall through, deciding on
                                   its own globals, or called other than
                                   plain-positionally. #427 round 16: and
                                   what a DELEGATING BRANCH HANDS the
                                   audited resolver — every argument must be
                                   the resolver's own parameter for that
                                   slot, PASSED STRAIGHT THROUGH, resolving
                                   through this body's bindings to an
                                   expression built only out of the gate's
                                   own parameters. A forged scope, a forged
                                   role, a different variable, and the
                                   session scope REBUILT one line above
                                   every branch each left all three pins
                                   reading exactly what they pinned while a
                                   Coach of a team in NEITHER game received
                                   the HOME side's private roster over real
                                   HTTP; the argument shape allow-list is
                                   ONE expression kind, so a kind Python has
                                   not shipped yet is refused too. The same
                                   round closes the resolver's own per-role
                                   answers, which were pinned as a FUNCTION
                                   NAME whose body nothing derived. #427
                                   round 18: a slot the carrier HAS AN INPUT
                                   FOR must still HOLD that input — the
                                   argument's resolved value must be the
                                   parameter itself or that parameter
                                   normalised against an EMPTY default, so
                                   ``scope or {}`` parses and
                                   ``scope.fromkeys(scope, …)`` does not.
                                   #427 round 19: AND THE NAME IS NOT THE
                                   VALUE — an input may be MENTIONED only
                                   where this derivation READS the mention
                                   (a ``return``, a LIVE binding, an
                                   argument the five conditions vouch, and
                                   the role's own role test), so an object
                                   MUTATED before the call is refused; and
                                   the RESOLVER'S OWN parameters must hold
                                   what it was handed, so the same rebuild
                                   moved one function up is refused too.
                                   And a binding the FOLD would drop is
                                   refused one call deeper, in the helper
                                   the PLAYER authority is folded through.
                                   ELEVEN spellings, all eleven measured at
                                   ``8a6ed27`` with ``_audit() == []``, ten
                                   of them LIVE — sixty of sixty family
                                   cells admitted, tri-store. WHAT REMAINS
                                   OPEN: a call that mutates AND whose
                                   result the decision consumes is vouched
                                   by derivation, not identity
official assignment    CLOSED      ``OfficialAssignmentStatus`` DRIVEN
status                             through the product predicate an
                                   Official's admission is decided by,
                                   member by member, pinned in
                                   ``OFFICIAL_STATUS_GRANTS`` and required
                                   to equal the enum's own ``is_active`` —
                                   so the check dropped, a member
                                   reclassified and a member added each fail
                                   by name (#205, #427 round 15)
membership status      CLOSED      ``MembershipStatus`` x
                                   ``RosterService
                                   ._ELIGIBLE_MEMBERSHIP_STATUSES``, pinned
                                   per member in
                                   ``MEMBERSHIP_STATUS_GRANTS`` and MEASURED
                                   over real HTTP on every backend
=====================  ==========  ==================================

WHAT THE ORACLES INTERPRET

=====================  ==========  ==================================
axis                   status      authority / disclosed limit
=====================  ==========  ==================================
identity alphabet      **LIMIT**   ``Player``'s own fields, STRING-VALUED ONLY
entitlement class      CLOSED      each principal BOUND by an assertion, and
                                   each ADMISSION BRANCH the gate takes bound
                                   to the class that models it
admission (status      **LIMIT**   ``route_registry.REGISTRY`` x the
only, section 6b)                  RELATIONSHIP ROWS. The LEAVES are the
                                   private-game family derived from the
                                   registry by ``private_game_family()`` —
                                   an eleventh leaf acquires the assertion
                                   with no edit, proved by REGISTERING one.
                                   The other axis is no longer a set of
                                   PRINCIPALS (#427 round 18): it is every
                                   ``(principal, GAME SUBJECT)`` CELL whose
                                   entitled-team set NARROWED AGAINST THAT
                                   GAME is empty, via ``_subject_narrowed``
                                   and therefore via the grant rows and
                                   their declared active state. Selecting
                                   principals by their GLOBAL team union
                                   excluded the swept official on the game
                                   they do not referee, which an empty-body
                                   ``200`` walked straight through. Asked of
                                   the RESPONSE STATUS, which is what makes
                                   it reach the admissions both data oracles
                                   are blind to (#427 round 17). A LIMIT
                                   because the GAMES it is driven against —
                                   ``_SweepHarness.GAME_KEYS`` — are still
                                   hand-enumerated: round 18 makes the cell
                                   axis a function of that list rather than
                                   independent of it, and does NOT close it
data class             **LIMIT**   no product enum exists; non-vacuity asserted
perturbation kind      **LIMIT**   test constructions; PREMISES asserted
relationship kind      **LIMIT**   test constructions; PREMISES asserted
compared fields        **LIMIT**   :data:`_SweepHarness.VOLATILE_KEYS`
=====================  ==========  ==================================

Each CLOSED row means the same thing the route row has always meant: the
product gaining one more of that thing, without this sweep gaining a probe
for it, is an ERROR NAMING IT rather than a silent gap. Each **LIMIT** row is
a disclosed limit with a measurement, in the numbered section at the bottom of
this docstring — not a claim of closure. Round 9 replaced three hand-written
lists that a reproduced falsifier had just walked through; only ONE of the
three — query parameter — came out CLOSED. Subject and identity alphabet are
better than the lists they replaced and are still LIMIT rows, because a
closure needs an authority in the PRODUCT and neither has one yet: there is
no enum of relationship kinds, and ``Player``'s fields are authoritative
about that record, not about every way a person can be named.

RUNTIME, MEASURED AND STATED, AND IT IS NOT CHEAP ANY MORE. TWENTY-FOUR
worlds — a fresh base and a changed world for each of the two sides x each
kind in :data:`PERTURBATIONS` in each game of :data:`PERTURBED_GAMES`
(sixteen), plus one for each kind in :data:`RELATIONSHIP_REVOCATIONS` (EIGHT,
since round 11 drives DECLINED as well as unassigned and round 12 adds the
ex-member's ``season_roster_membership``) — x every
authenticated GET route x 10 principals x the FOUR per-world variants in
:data:`HINTS`. The other six live in :data:`FULL_HINTS` and are swept once
per backend, for the reason measured at :data:`HINTS` itself.

The sentence this replaces said "Eight worlds … two perturbation kinds … 8
principals … 1,984 requests per world … 6.1 s Memory". Every one of those
numbers was wrong: round 8 had already taken :data:`PERTURBATIONS` to THREE
kinds and :data:`PRINCIPALS` to TEN, and limit 3 in this same docstring said
"sixteen worlds" three paragraphs further down. That is the failure this file
exists to prevent, in the file whose whole thesis is MEASURED RATHER THAN
IMPLIED, so the numbers below are re-measured on the current tree rather than
adjusted:

* **2,560 real HTTP requests per world, 61,440 per backend** for the main
  property — 64 concrete paths x 10 principals x the 4 per-world variants in
  :data:`HINTS`. Round 11 added two concrete paths and one world-pair: the
  guardian route is bound to a junior the guardian is NOT linked to (see
  :class:`TheSweptBindingsExerciseTheUnentitledDirection`),
  ``get_officials_id_availability`` to a second official, and
  ``official_assignment_declined`` is a third revocation kind. Round 12 adds
  NO path and ONE world-pair — ``season_roster_membership``, the EX-MEMBER,
  which is the state no world in this matrix contained;
* measured on this machine, for the whole-surface property alone:
  **42.1 s Memory, 24.4 s SQLite, 81.1 s real PostgreSQL** — one recorded
  run (round 17; round 16 recorded 46.0 / 24.6 / 83.2, round 15
  44.9 / 24.4 / 82.7 and round 14 43.4 / 23.1 / 75.8), and it moves a few
  percent between runs with the machine's load;
* the WHOLE MODULE, tri-store: **Ran 106 tests in 314.6 s ... OK** at round
  19, measured solo with ``Errno 49`` zero — against **99 tests / 299.2 s**
  at round 18 (8a6ed27), **95 tests / 301.5 s** at round 17, **91 tests /
  310.3 s** at round 16 (6641fe7), **87 tests /
  305.5 s** at round 15 (d3f5f1e), **82 tests /
  284.9 s** at round 14 (5c3caf8) and **72 tests / 294.7 s** at the head
  round 14 started from (c4a725b). Round 14 added TEN tests, round 15 FIVE,
  round 16 FOUR, round 17 FOUR, round 18 TWO and round 19 SEVEN —
  thirty-two in all, of which THIRTY-ONE drive no backend. The one exception is round 17's
  ADMISSION MATRIX, which drives all three and is why the count of
  backend-looping methods moves from SIX to SEVEN. Round 19's seven are the
  three new refusals' falsifiers, the OTHER direction of the mutation rule
  (the positions the real gate genuinely uses, required GREEN), the eighth
  and tenth spellings' own seams, and the check that ``DECIDING_FIELDS`` is
  still what ``admission_branches`` pins. Round 14's
  are the six MODELS the admission derivation rests on,
  one per model plus the consumer derivation, the interpreter-grammar check
  and the two halves of the binding model. Round 15's are the OFFICIAL
  grant after `62a52b7` centralised it: that the pin FOLLOWS the predicate
  the branch calls, that following is an allow-list which refuses six named
  shapes rather than falling back to the function name, that the callee's
  signature grammar is the running interpreter's, and — driven, not matched
  — that the predicate's answer is
  ``OfficialAssignmentStatus.is_active`` member by member, with its own
  three-way falsifier. Round 16's are the SEVENTH model — what a DELEGATING
  BRANCH HANDS the audited resolver: that a forged argument is refused over
  twelve expression shapes, that a session scope the gate REBUILT is not
  the one it received, that a subject the resolver could have computed for
  itself carries none, and that the PLAYER authority is what its helper
  DECIDES BY rather than the helper's name. Every one of those nineteen is
  pure source analysis or a direct call of
  the product predicate, for the reason
  :class:`EveryAdmissionBranchIsDerivedAndCarriesAnAuthority`'s own
  docstring gives. Round 17's FOUR are the ADMISSION MATRIX and its three
  supporting proofs — the family's derivation and addressability, and the
  two new-member falsifiers, one per axis — and the matrix itself DOES loop
  every backend, so that count moves to SEVEN, re-measured off this
  module's AST rather than assumed;
* round 10 adds THREE more requests of a full sweep per backend —
  :class:`AGameKeyedGrantDoesNotSpanASecondGame` sweeps once for the
  identity falsifier and twice for the non-interference one, and its third
  test sweeps not at all — and round 11 adds EIGHT more, all on the FIRST
  backend only: :class:`TheGrantIsKeyedByEveryDimensionOfItsRow` sweeps ONCE
  for the ``status`` falsifier, TWICE for ``official_id`` (the widened world
  and the control without the second official's row), ONCE for
  ``guardian_user_id``, and FOUR times for ``player_id`` — two sweeps on
  each of two fixtures, because ``_perturbed`` cannot be entered twice on
  one. Its three AUDIT tests, and all three of
  :class:`TheSweptBindingsExerciseTheUnentitledDirection`'s, sweep NOTHING:
  they read the oracles directly. Round 12 adds ONE more full sweep per
  backend — :class:`ThePlayerGrantIsTheEligibleMembershipRow` sweeps once
  inside the widened window, to require oracle 1 to REPORT the ex-member —
  and :class:`EveryAdmissionBranchIsDerivedAndCarriesAnAuthority` sweeps
  nothing at all: it reads the GATE'S OWN SOURCE;
* and the full derived parameter matrix — :data:`FULL_HINTS`, ten variants,
  6,400 requests — once per backend on a fresh world, which is the shape
  that made closing the query-string axis affordable at all;
* round 17 sweeps NOTHING and adds FORTY plain requests per backend — the
  two principals ``_entitlement`` gives an empty entitled-team set x the ten
  derived leaves x both games. It is the cheapest assertion in the file and
  it is the one that reaches an admission carrying no data at all, which is
  the point of section 6b.

Closing the hint axis is most of that increase: probing every parameter the
server reads took the variants from four to ten, and the request count with
them. It is stated rather than absorbed, because a whole-surface sweep that
quietly became the slowest module in the suite is something the next round
should know it inherited.

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

**HOW BIG THE BLIND SPOT ACTUALLY IS, MEASURED rather than left at "six
names, any depth, every response"** (#427 round 9). Swept with the stripping
DISABLED, only TWO of the six names appear anywhere on the authenticated GET
surface — ``expires_at`` and ``issued_at`` — and both on exactly ONE route,
``get_accounts_id_sessions``. ``generated_at``, ``server_time``, ``now`` and
``last_seen_at`` NEVER APPEAR AT ALL. So the blind spot today is two names on
one route, not six names everywhere, and four of the six entries are
currently inert.

**AND ``test_the_sweep_is_stable`` DOES NOT PROVE THIS LIST IS NEEDED**, which
is the sentence the previous rounds' comment came close to implying. Measured:
removing ANY ONE of the six and re-running two consecutive sweeps of the same
world leaves them byte-identical — including the two names that do appear. The
list is what makes the sweep stable in principle; on this fixture, in this
window, nothing in it is observed to vary. Both facts are pinned by
:class:`TheDisclosedLimitsAreMeasuredNotRemembered` so this paragraph cannot
become another set of stale numbers.

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

**3. ORACLE 2 REACHES 18 OF THE 50 SWEPT ROUTES.** Re-measured on the
round-11 tree across all sixteen perturbation worlds — unchanged from rounds
8, 9 and 10, and unchanged by the wider hint matrix or by the two paths round
11 adds: only eighteen route NAMES ever move, so
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

import ast
import contextlib
import copy
import dataclasses
import datetime as _datetime
import enum
import functools
import inspect
import json
import re
import sys
import textwrap
import time
import typing
import unittest
from pathlib import Path
from unittest import mock

from helpers import BACKEND, end_membership_directly  # noqa: F401
from test_lineup_side_projection import _ProjectionHarness
from test_overview_schedule_side import _OverviewHarness
from test_substitute_membership_cutover import ADMIN

from hockey_scheduler.api.service import ApiService as _ApiService
from hockey_scheduler.domain import (Game, GuardianLink, MembershipStatus,
                                     OfficialAssignment,
                                     OfficialAssignmentStatus, OfficialRole,
                                     Role,
                                     ROLE_PERMISSIONS, Permission,
                                     SeasonRosterMembership)
from hockey_scheduler.store import InMemoryStore
from hockey_scheduler.services import (game_side_scope, guardian_service,
                                        lineup_visibility, side_provenance,
                                        subject_scope)
from hockey_scheduler.services.account_service import AccountService
from hockey_scheduler.services.roster_service import RosterService
from hockey_scheduler.web import route_extract
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

#: ``{query parameter name: (probe label, …)}`` — WHAT THE CLIENT SAYS, and
#: the axis is now CLOSED AGAINST WHAT THE SERVER ACTUALLY READS.
#:
#: THE HOLE THIS REPLACES (#427 round 9, D7). :data:`HINTS` was a hand-written
#: 4-tuple, and the variant that carried the whole "client input cannot select
#: a side" claim spelled itself ``?side=<a team id>``. ``side`` IS NOT A
#: PARAMETER THIS SERVER READS — ``web/server.py`` reads exactly seven names,
#: enumerated from its own ``parse_qs`` call sites by
#: :func:`route_extract.query_parameter_names` — so that variant probed a name
#: the server ignores, carrying a value outside the parameter's real domain.
#:
#: MEASURED, over real authenticated HTTP on the head this corrects: four
#: injected lines that make the private-game family honour ``?side=away`` hand
#: ``homecoach`` AND ``homeplayer`` the AWAY side's five private identities
#: with ``restricted: false`` on ``GET /api/games/{id}/lineups`` — while
#: ``?side=<away team id>``, THE ONLY SPELLING THIS SWEEP SENT, stays inert.
#: With that leak live the PRIMARY SWEEP PASSED: ``Ran 27 tests in 172.133s …
#: OK`` on Memory, SQLite and real PostgreSQL. What caught it was
#: ``test_side_provenance_guard`` — the scanner the owner DEMOTED to
#: supplemental — and ``test_lineup_side_projection
#: ::AClientSuppliedSideIsIgnored`` on two routes for one principal.
#:
#: SO THE AXIS IS DERIVED, NOT LISTED. Every parameter the server reads must
#: appear here with at least one probe value, and
#: :class:`TheHintAxisIsClosedAgainstWhatTheServerReads` fails NAMING a
#: parameter that does not — the same fail-closed property the ROUTE axis has
#: had against ``route_registry.REGISTRY`` since round 1. A parameter the
#: product begins reading cannot be a silent gap in this sweep.
#:
#: WHY THE VALUES ARE SIDE-BEARING WHERE THE PARAMETER CAN CARRY ONE. The
#: question this axis asks is "can the client SELECT A SIDE by saying
#: something", so every probe whose parameter can name a team, a season or a
#: person names the AWAY one — the side a HOME-scoped caller must never
#: reach. A probe with a value no side could be read out of would be inert by
#: construction and would prove nothing.
QUERY_PARAMETER_PROBES = {
    "team_id": ("home_team", "away_team"),
    "season_id": ("season",),
    "scope_type": ("scope_type_team",),
    "scope_id": ("away_team",),
    "recipient_ref": ("away_player",),
    "actor_type": ("actor_type_player",),
    "actor_ref": ("away_player",),
}

#: A parameter the server does NOT read, probed in ITS OWN REAL DOMAIN.
#:
#: This is the RESPELLING of the old ``side_away`` variant, kept as an
#: explicit CONTROL and demoted to what it actually is. It is not coverage of
#: the query-string axis — :data:`QUERY_PARAMETER_PROBES` is — it is one
#: probe of the proposition "a name nothing reads stays unread", spelled
#: ``?side=away`` rather than ``?side=<a team id>`` so that the value lies
#: inside the domain the parameter would have if anything did read it. That
#: respelling is what makes it bite: it is the exact request that reddens the
#: D7 falsifier above, and it is how the sibling test
#: ``AClientSuppliedSideIsIgnored`` already spells its own probe.
#:
#: :class:`TheHintAxisIsClosedAgainstWhatTheServerReads` also asserts that
#: nothing here is read by the server, so the day ``side`` becomes a real
#: parameter this entry stops being a control and has to move into
#: :data:`QUERY_PARAMETER_PROBES` deliberately.
UNREAD_PARAMETER_CONTROL = {
    "side": ("away_word",),
}

#: ``{probe label: the fixture expression it resolves to}`` — resolved per
#: fixture by :meth:`_SweepHarness._probe_value`, listed here so a label with
#: no value is a KeyError naming it rather than a silently skipped variant.
PROBE_LABELS = ("home_team", "away_team", "season", "scope_type_team",
                "actor_type_player", "away_player", "away_word")

#: EVERY hint variant, in a stable order: the un-hinted control plus one per
#: (parameter, probe value). DERIVED from the two maps above, so adding a
#: probe adds a swept variant and nothing has to be kept in step by hand.
FULL_HINTS = ("none",) + tuple(
    f"{param}={label}"
    for param, labels in sorted({**QUERY_PARAMETER_PROBES,
                                 **UNREAD_PARAMETER_CONTROL}.items())
    for label in labels)

#: The variants swept in EVERY ONE OF THE TWENTY-FOUR WORLDS, as against
#: :data:`FULL_HINTS`, which is swept once per backend by
#: :class:`TheHintAxisIsClosedAgainstWhatTheServerReads`.
#:
#: WHY THE AXIS IS SPLIT ACROSS TWO PLACES, WITH THE MEASUREMENT THAT FORCED
#: IT (#427 round 9). Sweeping all ten variants in all twenty-four worlds is a
#: correct design and an unaffordable one. This sweep RE-MEASURES A FRESH
#: BASE FOR EVERY PHASE — it has to, because a perturbation is not
#: byte-reversible and ``/board`` serves an append-only audit stream — so the
#: store grows monotonically across a run, and ``GET
#: /api/context/options`` deep-copies the whole in-memory store on every
#: request. MEASURED, memory backend, ten variants in every world: the per-
#: sweep cost climbs from **2.3 s at phase 0 to 12.1 s at phase 7**, 108 s
#: for the first eight phases alone, and the PostgreSQL column of the same
#: matrix already stood at 756 s in the last recorded full-suite run at FOUR
#: variants. The growth is PRE-EXISTING and not introduced here; multiplying
#: the request count by 2.5 is what made it decisive.
#:
#: SO THE TWO QUESTIONS ARE SEPARATED, and each is answered where it is
#: affordable:
#:
#: * "does ANY parameter this server reads select a side?" — the whole
#:   derived matrix, swept ONCE PER BACKEND against a fresh fixture by
#:   :class:`TheHintAxisIsClosedAgainstWhatTheServerReads`. A parameter the
#:   product begins reading still enters the sweep automatically; it is the
#:   number of WORLDS it enters that is bounded, not whether it enters.
#: * "does a side's private STATE change what a hint does?" — the
#:   side-bearing variants below, in all twenty-four worlds, exactly as
#:   before.
#:
#: WHAT THAT COSTS, STATED RATHER THAN ABSORBED: a parameter that selects a
#: side ONLY while some side's private state holds a particular value would
#: be caught only if it is one of the three below. That is a narrower claim
#: than "every parameter, every world", it is the claim this file can afford
#: to keep true on three backends, and it is strictly wider than what stood
#: here before round 9 — which probed ``?side=`` under a value outside its
#: own domain and therefore probed nothing at all.
HINTS = ("none", "team_id=home_team", "team_id=away_team", "side=away_word")

# ---------------------------------------------------------------------------
# TYPED DESIGN CLASSIFICATIONS — who is entitled to which sides, and WHY.
#
# Not a suppression list. Each class is a claim about the product, each claim
# has a dedicated test in `TheDesignClassificationsAreStillTrue`, and a
# principal whose entitlement stops matching its class fails that test rather
# than quietly widening this sweep.
# ---------------------------------------------------------------------------
COACH_SCOPED_TO_ONE_SIDE = "coach_scoped_to_one_side"
#: A COACH bound to a team by their ACCOUNT SCOPE. Entitled to exactly that
#: side and to nothing of the other.
#:
#: THE GRANT IS THE SCOPE, AND THERE IS NO ROW (#427 round 12, LB1). This is
#: the half of the old merged ``scoped_to_one_side`` class that the sentence
#: describing it was actually TRUE of: ``game_side_scope
#: .game_scoped_own_team_id`` answers a Coach with ``scope.get("team_id")``
#: and reads no store at all, and that function's own docstring records why —
#: "There is no ``CoachSeasonMembership`` (or any season-scoped Coach model)
#: anywhere in this codebase — a Coach's team assignment genuinely IS
#: permanent". A Coach really does manage a team's roster across every game
#: it plays, so there is no per-game row to key on.

PLAYER_SCOPED_BY_MEMBERSHIP = "player_scoped_by_membership"
#: A PLAYER whose side in THIS game is resolved from an ELIGIBLE
#: :class:`SeasonRosterMembership`. Entitled to exactly the side that row
#: names, in exactly the games that row's LeagueSeason covers, and to nothing
#: anywhere once the row stops granting participation.
#:
#: THE BLIND SPOT THIS REPLACES (#427 round 12, LB1). Coach and Player were
#: ONE class, and the comment above :data:`GRANT_RECORD_FIELDS` stated
#: affirmatively that its grant is "the ACCOUNT SCOPE… there is no per-game
#: row to key on". That is true of a Coach and FALSE of a Player: a Player's
#: scope is canonicalized to ``player_id`` ALONE (#160), and the side is
#: resolved live by ``game_side_scope._player_team_for_game`` ->
#: ``RosterService.team_for_game`` -> ``resolve_membership_context``, which
#: gates on ``SeasonRosterMembership.status`` against
#: ``RosterService._ELIGIBLE_MEMBERSHIP_STATUSES`` and on the
#: ``SeasonTeamRegistration`` spine. There IS a per-game row, and it is the
#: row #205 exists to make authoritative.
#:
#: Because the class was merged, ``SeasonRosterMembership`` was in neither
#: :data:`GRANT_ROWS` nor :data:`GRANT_DIMENSIONS`, every one of its eleven
#: fields was keyed on by NOTHING, and no perturbation or revocation in the
#: matrix ever moved a membership — so no world in this sweep contained the
#: state at all.
#:
#: MEASURED at the head this corrects, driven through the product's own
#: ``SetupService.set_season_roster_membership_status`` write path: the
#: PRODUCT narrowed ``homeplayer``'s ``GET /api/games/{gid}/board`` from 200
#: to 403 while the ORACLE'S ``_entitled_teams`` stayed ``[team_1]``. And the
#: falsifier that turns that blindness into a leak is d62473a's byte for byte
#: on the row nobody modelled — widen the gate by ONE enum member
#: (``_ELIGIBLE_MEMBERSHIP_STATUSES += (MembershipStatus.INACTIVE,)``, the
#: same omission that let a DECLINED official keep 200) and a real
#: authenticated EX-MEMBER session receives 200 with EIGHT private HOME
#: identities, while this file stayed green on all three backends: ``Ran 59
#: tests in 275.541s … OK``, 22 worlds x 2,560 requests per backend.
#:
#: :class:`ThePlayerGrantIsTheEligibleMembershipRow` is the falsifier that
#: closes it, and :class:`ThePlayerGrantIsTheEligibleMembershipRow` pins
#: each status by name.

IN_NEITHER_SIDE = "in_neither_side"
#: A Coach of a team that plays in NEITHER game. Entitled to NO side of
#: either — the sharpest row in the matrix, and the one that showed round 4's
#: leak was a WIDER gate than the family it sat beside.
#:
#: AND UNTIL #427 ROUND 17 NOTHING ASSERTED THAT IT IS REFUSED. Every other
#: principal typed here had a per-leaf status matrix somewhere in this file —
#: the official's, the guardian's, the viewer's, the two operators' — and
#: this one, the sharpest, had none: it was subject to the two DATA oracles
#: only, which see nothing when an admission carries no data. That is
#: exactly the gap ``q07`` walks through. It is now covered by
#: :class:`NoPrincipalEntitledToNoSideIsAdmittedToTheFamily`, which reaches
#: it BECAUSE THE ENTITLED-TEAM SET IS EMPTY rather than because anybody
#: named it.

GUARDIAN_OF_A_JUNIOR = "guardian_of_a_junior"
#: A guardian verified for one junior. Entitled to that junior's resolved side
#: — but ONLY on the two routes that serve the junior's own row, never as a
#: standing grant over the junior's whole team.
#:
#: THE BLIND SPOT THIS REPLACES (#427 round 3). The blocker-2 fix narrowed the
#: OFFICIAL and left the GUARDIAN one seat over in exactly the shape the owner
#: had just ruled on: :meth:`_entitled_teams` returned the junior's whole side
#: for every route and every data class, so all three oracles read that one
#: number for the guardian on all 50 swept route names. (It said 48 until
#: round 10: the count was written when the surface had 48, never re-counted,
#: and `TheMethodAxisIsADisclosedLimitWithLiveNumbers` has been asserting
#: `assertEqual(50, count("GET", True))` beside it.) MEASURED on the head
#: this corrects: a REGISTERED authenticated route returning to
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
#:   all ten of them ON BOTH GAMES (#427 round 17 measured the second game;
#:   until then only the first was asserted), ``/board`` and ``/lineups``
#:   included, which is where it differs from the official, who is ADMITTED
#:   to three of them with a projection and to two more that carry no side;
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
#: REFUSED every other OWN-SIDE leaf of the family outright, which
#: :class:`TheDesignClassificationsAreStillTrue` asserts — so this set being
#: exactly these three is a claim about the product, checked, not assumed.
#:
#: "EVERY OTHER LEAF" WAS WRONG AND IS NOW MEASURED (#427 round 17). This
#: said the official is refused every other leaf of the family, and the
#: assertion beside it named FIVE refusals out of a family of TEN. The
#: remaining two, ``/officials`` and ``/reschedule``, answer 200 — they are
#: not part of this grant and never were, they carry no side's private
#: roster state, and nothing said so. They are typed in
#: :data:`OFFICIAL_ADMITTED_BEYOND_THE_SHEET`, and the assertion now decides
#: EVERY leaf of the derived family rather than five of them.
OFFICIAL_ASSIGNED_GAME_ROUTES = frozenset({
    "get_games_id_board", "get_games_id_lineups", "get_games_id_roster"})

#: The leaves of the family that ADMIT an assigned official although they
#: are not part of the sheet grant above — so "admitted" and "granted a
#: side" are two different statements and each leaf carries one of them.
#:
#: WHY THIS CONSTANT EXISTS (#427 round 17). The official's per-leaf matrix
#: in :class:`TheDesignClassificationsAreStillTrue` used to name FIVE
#: refusals by hand, and the family has TEN leaves: ``/officials`` and
#: ``/reschedule`` appeared in neither half and no line anywhere said why.
#: Deriving the refusals as "the family minus the sheet routes" would
#: therefore have been wrong — MEASURED, both answer 200 — and leaving the
#: five hand-written would have meant an eleventh leaf was asserted for the
#: official by nobody. So the omission is now a typed claim instead: these
#: two are game-level reads that carry NEITHER side's private roster state
#: (which is also why they are the two family leaves oracle 2 cannot reach —
#: see limit 3 in the module docstring), and a leaf that begins admitting an
#: official has to join one of these two sets deliberately.
OFFICIAL_ADMITTED_BEYOND_THE_SHEET = frozenset({
    "get_games_id_officials", "get_games_id_reschedule"})

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

#: ``{revocation kind: the principal whose relationship to the SUBJECT it
#: withdraws}`` — the second half of the SUBJECT axis (#427 round 9).
#:
#: :data:`PERTURBATIONS` moves a SIDE's private state and asks who observes
#: it. These move the PRINCIPAL'S RELATIONSHIP TO THE SUBJECT and ask the
#: same question — which is the half that was missing, and the half a
#: standing grant hides in. A grant that outlives the relationship it was
#: granted for is the shape of every finding in rounds 5-9: the official
#: entitled to a sheet, the guardian entitled to a side, each keyed on WHO
#: they are and never on WHETHER THEY STILL ARE.
#:
#: Both are withdrawn through the product's own write path or its own record
#: — ``ApiService.unassign_official`` and the ``GuardianLink.verified``
#: flag — so what the sweep observes afterwards is a real state of the
#: system and not a fixture that never existed.
#:
#: WHY A REVOKED WORLD RUNS ORACLE 1 AND HINT-INERTNESS BUT NOT ORACLE 2.
#: Oracle 2 asks "did anything move that should not have"; under a
#: revocation the revoked principal's responses MUST move — that is the
#: revocation working — so the two-world diff is expected to be non-empty
#: for exactly them and reading it as a leak would be wrong. What must hold
#: is that they now receive NOTHING they are no longer entitled to, which is
#: oracle 1 with the entitlement recomputed from the store; and that their
#: loss really reached the surface, which
#: :meth:`_SweepHarness._assert_relationship_loss_is_observed` asserts so
#: the world cannot pass vacuously.
#: ``official_assignment_declined`` is #427 round 11's addition, and it is
#: the state the two halves of this map had never been driven into agreement
#: on: the official half REVOKED BY DELETING THE ROW while the guardian half
#: used the product's softer "grants nothing" state. ``DECLINED`` is the
#: official's own softer state — the row survives, still names this official
#: and this game, and ``OfficialAssignmentStatus.is_active`` says it holds
#: nothing — so it is the sharper subject the guardian half already had.
#: ``season_roster_membership`` is #427 round 12's addition (LB1), and it is
#: the world whose ABSENCE was the finding. No perturbation and no revocation
#: kind here ever moved a ``SeasonRosterMembership``, so no world in this
#: matrix contained the state at all — and the state is condition 4's
#: "ex-member": a membership the PRODUCT ITSELF made ineligible, through
#: ``SetupService.set_season_roster_membership_status``. Like the guardian's
#: unverified link and the official's DECLINED assignment it is the SOFT
#: revocation, not a deletion: the row survives, still names this player,
#: this team and this LeagueSeason, and only ``status`` moves — which is the
#: product's own statement that the stint no longer participates.
RELATIONSHIP_REVOCATIONS = {
    "official_assignment": "official",
    "official_assignment_declined": "official",
    "guardian_link": "guardian",
    "season_roster_membership": "homeplayer",
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
#: leaves of the private-game family, on BOTH games (#427 round 17; it was
#: measured on one game until then). That is the product being correct today;
#: the value of the row is that it is now WATCHED. The two-line falsifier in
#: :data:`PRINCIPAL_ROLES` — the one the whole suite missed — is required to
#: redden this sweep by
#: :meth:`TheViewerIsEntitledToNothingAndTheSweepProvesIt
#: .test_admitting_the_viewer_to_the_operator_tuples_reddens_this_sweep`.
#: (That reference named ``TheViewerFalsifierRedensThePrimarySweep``, a class
#: this file does not contain, until round 17.)

#: ``{(route name, query parameter): the registry auth it was classified
#: against}`` — EVERY PLACE A CLIENT HINT LEGITIMATELY SELECTS SOMETHING, and
#: only for a caller typed as an UNSCOPED OPERATOR.
#:
#: WHAT THIS REPLACES, AND WHY IT IS NARROWER (#427 round 9). There used to be
#: two constants here: ``HINT_MAY_SELECT_FOR_A_BOTH_SIDED_CALLER``, a set of
#: three ROUTE names exempt from hint-inertness for any caller entitled to
#: both sides, and ``TEAM_ID_IS_A_COLLECTION_FILTER``, a set of one route
#: exempt for everybody. Both were keyed on the ROUTE ALONE, so the exemption
#: covered EVERY query parameter on that route — including parameters nobody
#: had measured, and including any parameter a future round adds to
#: :data:`QUERY_PARAMETER_PROBES`. Keying on the (route, PARAMETER) pair is a
#: strict tightening: on ``/availability-summary`` exactly ``?team_id`` is
#: exempt now, and a second parameter arriving on that route is checked.
#:
#: AND THE PRINCIPAL SIDE OF IT IS TIGHTER TOO. The old exemption tested
#: "entitled to both sides", which is true of the assigned OFFICIAL as well as
#: the two operators. It never mattered — the official is refused all three of
#: those leaves — but it was a latent second seat in an exemption that only
#: ever needed one. The condition is now the CLASS: one of the two unscoped
#: operator classes, and nothing else.
#:
#: MEASURED, not assumed, on this fixture: of 5,580 hinted requests across
#: every authenticated GET route x ten principals x eight parameters, exactly
#: SIX (route, parameter) pairs move a response at all, and every one of them
#: moves ONLY for ``operator`` and/or ``arena``. NO SCOPED PRINCIPAL MOVES ON
#: ANY PARAMETER ON ANY ROUTE. That measurement is what
#: :class:`TheHintExemptionsAreNecessaryAndSufficient` re-runs.
#:
#: THE CONDITION IS MACHINE-CHECKED, PER ENTRY. The value is the ``auth`` the
#: registry recorded for that route when the pair was classified. If a route's
#: auth is ever loosened, the recorded string stops matching and the
#: classification has to be RE-DECIDED rather than silently inherited by a
#: wider set of callers — the rule ``get_players`` already carried, applied to
#: all six.
#:
#: WHY EACH ONE IS HERE:
#:
#: * the three private-game leaves — the family's ``?team_id=`` hint. Each
#:   adjudicates first: a Coach or Player reaches ``OWN_SIDE`` and the hint is
#:   ignored outright, and an assigned official is REFUSED all three. What is
#:   left is the ``FULL`` branch, where the hint chooses which side to answer
#:   for a caller who may already read both, so it widens nothing.
#:   ``side_provenance.EXEMPTIONS`` records both shapes as OPERATOR_DEFAULT.
#: * ``get_players`` — ``?team_id=`` narrows a roster LIST to one team. It
#:   says nothing about a game and selects no side of one; the parameter
#:   happens to share a name with the family's hint, which is exactly why it
#:   has to be classified rather than silently tolerated.
#: * ``get_notifications_preferences`` — ``?recipient_ref=`` names WHOSE
#:   preferences to read. It names a person, not a side of a game, and the
#:   route's own auth already requires ``MANAGE_SCHEDULE`` to read anyone
#:   else's.
#: * ``get_setup_scheduling_policy`` — ``?scope_type=`` names WHICH SCOPE's
#:   policy to read, on an operator-only route. Same shape as
#:   ``get_players``.
#:
#: MEASURED AND STATED RATHER THAN ASSUMED: ``/availability-summary`` bounds
#: the ``?team_id`` hint to the game's two sides (a third team's id is
#: refused), while the two workflow leaves do NOT — an unscoped operator
#: naming any team id gets that team's pool. That is pre-existing
#: ``_workflow_side`` FULL-branch behaviour, it is reachable only by a caller
#: who may read every team anyway, and it is outside this round's ruling; it is
#: recorded here so the difference between the three routes is visible rather
#: than folded away.
HINT_MAY_SELECT_FOR_AN_UNSCOPED_OPERATOR = {
    ("get_games_id_availability_summary", "team_id"):
        "session+own-side-projection",
    ("get_games_id_substitute_candidates", "team_id"):
        "session+MANAGE_ROSTER+own-side-projection",
    ("get_games_id_substitute_addable", "team_id"):
        "session+MANAGE_ROSTER+own-side-projection",
    ("get_players", "team_id"): "operator_only",
    ("get_notifications_preferences", "recipient_ref"):
        "session+MANAGE_SCHEDULE-or-self",
    ("get_setup_scheduling_policy", "scope_type"): "operator_only",
}

#: The two classes an exemption above may be claimed by, and no others.
UNSCOPED_OPERATOR_CLASSES = frozenset({
    OPERATOR_UNSCOPED_BY_DESIGN,
    UNSCOPED_OPERATOR_WITHOUT_ROSTER_AUTHORITY})

#: THE STATUS A PRIVATE-GAME LEAF ANSWERS A CALLER WHO MAY READ NO SIDE OF
#: IT, and every cell of the admission matrix is pinned at exactly this.
#:
#: MEASURED FIRST, PINNED SECOND (#427 round 17). The question "is 403 right
#: for EVERY unentitled principal on EVERY leaf, or does some leaf
#: legitimately answer 200 with an empty body" is a question about the
#: product, so it was driven before it was decided: the two principals whose
#: entitled-team set is empty — ``thirdcoach`` and ``viewer`` — were measured
#: against all ten leaves of the derived family on BOTH games, forty cells,
#: and every one of the forty answered 403. Nothing is being asserted here
#: that the product does not already do; what is new is that it is WATCHED.
#:
#: AND RE-MEASURED WHEN THE AXIS BECAME PER-GAME (#427 round 18). The
#: derivation now yields FIVE ``(principal, game)`` cells rather than four —
#: those two on both games plus the swept OFFICIAL on the game they do not
#: referee — so FIFTY cells, and every one of the fifty answered 403 before
#: this pinned them. The official's ten are the ones nothing asserted.
#:
#: THERE IS DELIBERATELY NO EXEMPTION MECHANISM. A leaf that begins answering
#: something else — a 200 with a restricted body, a 404, a 400 — fails this
#: matrix by name, and that is the intent: an unentitled caller reaching a
#: private-game leaf at all is the judgement this round exists to force
#: somebody to make, and a per-cell excuse would be the hand-written axis
#: coming back one level down. Contrast the two operators, whose measured
#: matrix in :class:`TheArenaManagerIsAnOperatorWithoutRosterAuthority`
#: records 400s and 403s alike, because THEY ARE ENTITLED and the question
#: there is a different one.
REFUSED = 403

#: THE PRIVATE-GAME FAMILY'S OWN SHAPE, in the registry's template
#: vocabulary: a GET leaf UNDER one game id. Both placeholder spellings the
#: registry uses are matched (``{}`` and the narrower ``{w}``) and so is any
#: depth below the game — a leaf one segment deeper is IN the family and has
#: to be DECIDED about, never silently outside the pattern.
_PRIVATE_GAME_LEAF = re.compile(r"^/api/games/\{[^{}]*\}/.+$")


def private_game_family(specs=None):
    """``{route name: RouteSpec}`` — the private-game family, DERIVED FROM
    ``route_registry.REGISTRY`` rather than listed.

    ONE DEFINITION, AND IT IS DERIVED FROM THE PATH (#427 round 17). Five
    places in this file asserted something about "the ten leaves of the
    private-game family" and four of them spelled the ten by hand, in a
    string literal, per test. The fifth derived them from the registry BY
    NAME PREFIX (``get_games_id_``), which is a naming CONVENTION and not an
    authority: a leaf whose route name did not happen to start that way was
    outside every one of those assertions. The hand-written axis is where
    the next leak hides — that is the whole lesson of this file's sixteen
    rounds — and "the eleventh leaf" is the shape it would take here. So the
    family is a function of the TEMPLATE, which is what makes a path a
    private-game read in the first place, and every one of the five call
    sites now asks this.

    :meth:`_SweepHarness._private_game_family` is what the tests call: it
    adds the assertions that make a leaf this file cannot ADDRESS an error
    NAMING IT rather than a leaf quietly dropped."""
    specs = route_registry.REGISTRY if specs is None else specs
    return {s.name: s for s in specs
            if s.method == "GET" and s.kind == "route"
            and _PRIVATE_GAME_LEAF.match(s.template)}


def _scope_keys(*roles):
    """The scope keys the PRODUCT accepts for these roles — the account-scope
    half of :data:`GRANT_RECORD_FIELDS`, read from
    ``AccountService._ALLOWED_SCOPE_KEYS`` rather than listed here."""
    out = frozenset()
    for role in roles:
        out |= AccountService._ALLOWED_SCOPE_KEYS.get(role, frozenset())
    return out


def _record_fields(record):
    """The field names of a domain record, from the record itself."""
    return frozenset(f.name for f in dataclasses.fields(record))


def _record_types(record):
    """``{field name: its DECLARED type}``, resolved through
    ``typing.get_type_hints`` so a stringified annotation still yields the
    real class. The TYPE is what makes the two derivations below possible
    without a list: a field's meaning to a grant is carried by how the
    product declared it, not by how this module reads it."""
    return typing.get_type_hints(record)


def _base_type(annotation):
    """``Optional[X]``/``Union[X, None]`` reduced to ``X``; anything else
    unchanged. Every optional column in these rows is ``Optional[X]``."""
    args = [a for a in typing.get_args(annotation) if a is not type(None)]
    return args[0] if len(args) == 1 else annotation


def _subject_fields(record):
    """The field names of ``record`` that NAME ANOTHER ROW — every field
    ending in ``_id`` except the record's OWN ``id``.

    This is the first half of "which dimensions is a grant keyed on", and it
    is DERIVED rather than listed on purpose (#427 round 11): the last two
    rounds each found an entitlement computed over a coarser key than the
    response it governs, and both times the missing dimension was sitting in
    plain sight as a field of the row. ``OfficialAssignment`` names a
    ``game_id`` AND an ``official_id``; ``GuardianLink`` names a
    ``guardian_user_id`` AND a ``player_id``. Reading the ``_id`` suffix off
    the record catches a row that GAINS one."""
    return frozenset(f.name for f in dataclasses.fields(record)
                     if f.name.endswith("_id") and f.name != "id")


#: ``{activation enum: (the object that owns the product's partition of it,
#: the ELIGIBLE attribute, the INELIGIBLE attribute)}`` — for an enum whose
#: "does this value grant anything" answer lives OUTSIDE the enum.
#:
#: WHY THIS IS NOT A SECOND WAY OF SPELLING THE SAME THING (#427 round 12,
#: LB1). :func:`_activation_fields` recognised exactly two shapes: a ``bool``
#: column, and an ``Enum`` exposing an ``is_active`` PROPERTY. Both read the
#: answer off the type itself, which is why they need nothing declared here.
#: ``MembershipStatus`` declares ``is_terminal`` and NOT ``is_active``: the
#: product keeps its participation answer in ``RosterService``, as the pair
#: of tuples ``_ELIGIBLE_MEMBERSHIP_STATUSES`` /
#: ``_INELIGIBLE_MEMBERSHIP_STATUSES`` whose own comment says they "must
#: partition ``MembershipStatus`` exactly". So the answer is still the
#: PRODUCT'S — it is only kept somewhere the TYPE cannot point at, and this
#: is the pointer.
#:
#: BOTH HALVES, NOT JUST THE ELIGIBLE ONE, and that is what makes it
#: fail-closed: :meth:`ThePlayerGrantIsTheEligibleMembershipRow
#: .test_the_products_two_tuples_still_partition_the_enum` requires the two
#: to partition the enum, so a status the product has not classified cannot
#: reach :func:`_row_is_active` at all.
ELIGIBILITY_AUTHORITIES = {
    MembershipStatus: (RosterService, "_ELIGIBLE_MEMBERSHIP_STATUSES",
                       "_INELIGIBLE_MEMBERSHIP_STATUSES"),
}


def _eligibility_partition(declared):
    """``(eligible, ineligible)`` frozensets for an activation enum whose
    partition the PRODUCT keeps outside the type — read live off the owner
    named in :data:`ELIGIBILITY_AUTHORITIES`, never copied here."""
    owner, yes, no = ELIGIBILITY_AUTHORITIES[declared]
    return (frozenset(getattr(owner, yes)), frozenset(getattr(owner, no)))


def _is_activation_enum(declared):
    """Does ``declared`` carry the product's own "does this value grant
    anything" answer — by its OWN property, or by a partition the product
    keeps beside it?"""
    if not (isinstance(declared, type) and issubclass(declared, enum.Enum)):
        return False
    return (isinstance(getattr(declared, "is_active", None), property)
            or declared in ELIGIBILITY_AUTHORITIES)


def _activation_fields(record):
    """The field names whose DECLARED TYPE carries the product's own answer
    to "does this row grant anything at all".

    The second half, and the one F2 was: ``OfficialAssignment.status`` is
    typed ``OfficialAssignmentStatus``, which declares ``is_active`` —
    "Proposed or accepted assignments hold the official's time" — and
    ``GuardianLink.verified`` is a ``bool`` whose own docstring says an
    unverified link grants nothing. Both are ACTIVATION state, and both are
    recognised HERE by the shape of the type rather than by name:

    * ``bool`` — a flag the row carries about itself;
    * an ``Enum`` exposing an ``is_active`` PROPERTY — the product's own
      predicate, which :func:`_row_is_active` then calls rather than
      re-deriving;
    * an ``Enum`` the product PARTITIONS beside the type rather than on it —
      :data:`ELIGIBILITY_AUTHORITIES` (#427 round 12, LB1).
      ``SeasonRosterMembership.status`` is the case that matters, and it is
      the reason the third shape exists: ``MembershipStatus`` declares
      ``is_terminal`` and no ``is_active`` at all, so a rule that recognised
      only the first two shapes would have called the ONE column #205 makes
      authoritative "not activation state" and let it fall into
      :data:`GRANT_FIELDS_THAT_KEY_NOTHING` with a reason that was false.

    ``OfficialRole`` is an Enum and declares no ``is_active``, so it is not
    activation state and does not land here — which the measured audit in
    :class:`TheGrantIsKeyedByEveryDimensionOfItsRow` then has to confirm
    rather than assume."""
    hints = _record_types(record)
    out = set()
    for field in dataclasses.fields(record):
        declared = _base_type(hints.get(field.name))
        if declared is bool or _is_activation_enum(declared):
            out.add(field.name)
    return frozenset(out)


def _keying_fields(record):
    """Every DIMENSION a grant row carries: the rows it names, plus the
    activation state it declares about itself."""
    return _subject_fields(record) | _activation_fields(record)


def _row_is_active(record, row):
    """Does this stored grant row grant anything — asked of the PRODUCT'S OWN
    declared state, never of the gate.

    That distinction is the whole of F2 and of the claim
    :meth:`_SweepHarness._subject_narrowed` makes above it. ``bool`` fields
    must be true; an activation Enum is coerced back through its own class
    (the SQL backends hydrate it as a plain string) and asked ``is_active``,
    or — where the product keeps the partition beside the type rather than on
    it — tested against the ELIGIBLE half of
    :data:`ELIGIBILITY_AUTHORITIES` (#427 round 12, LB1). So deleting a
    status check from ``game_side_scope``, or WIDENING
    ``_ELIGIBLE_MEMBERSHIP_STATUSES`` by one enum member, does NOT move this
    answer in the direction the gate moved: the eligible half is read live,
    so a widening makes the gate and this predicate agree — which is exactly
    why the eligible SET is additionally pinned by name in
    :data:`MEMBERSHIP_STATUS_GRANTS` and measured against real HTTP by
    :class:`ThePlayerGrantIsTheEligibleMembershipRow`. Deleting the check
    outright is what this predicate catches on its own, and it is what makes
    that deletion a RED sweep instead of a silent widening of the
    expectation."""
    hints = _record_types(record)
    for name in sorted(_activation_fields(record)):
        value = getattr(row, name)
        declared = _base_type(hints[name])
        if declared is bool:
            if not value:
                return False
        elif declared in ELIGIBILITY_AUTHORITIES:
            eligible, _ineligible = _eligibility_partition(declared)
            if declared(value) not in eligible:
                return False
        elif not declared(value).is_active:
            return False
    return True


#: ``{entitlement class: the field names of the row the PRODUCT stores that
#: class's grant in}`` — the WHOLE row, every column of it. Every value is
#: DERIVED from an authority outside this module: a domain dataclass's own
#: fields, or ``AccountService._ALLOWED_SCOPE_KEYS``.
#:
#: WHICH of those fields KEY the grant is :data:`GRANT_DIMENSIONS`, and the
#: rest carry a typed reason in :data:`GRANT_FIELDS_THAT_KEY_NOTHING`. Those
#: two together must partition THIS map exactly — which is what makes a grant
#: row gaining a column an error naming it rather than a dimension nobody
#: keyed on (#427 round 11).
#:
#: WHY THIS MAP EXISTS (#427 round 10, D10). The owner's blocker was that an
#: assigned official's permitted identities were aggregated ACROSS GAMES while
#: the grant that produces them — an ``OfficialAssignment`` row — names ONE
#: game. The general shape is "an entitlement aggregated across a dimension
#: the grant is keyed on", and answering "which other principal has it?" by
#: reading this file's own entitlement code would put both sides of the
#: question inside this module, which is the failure
#: :data:`PRINCIPAL_ROLES` already records once.
#:
#: So the question is asked of the PRODUCT: what row carries the grant, and
#: does that row name a game?
#:
#: * ``OFFICIAL_SUBMITTED_LINEUP_ONLY`` — ``OfficialAssignment``, which
#:   carries ``game_id``. GAME-KEYED. The account's own ``official_id`` scope
#:   says only WHICH official the caller is; it grants no read.
#: * ``GUARDIAN_OF_A_JUNIOR`` — ``GuardianLink``, which carries
#:   ``player_id`` and no game. A verified link is a standing authority over
#:   one junior in every game, so narrowing it by game would report the real
#:   product grant as a leak.
#: * ``COACH_SCOPED_TO_ONE_SIDE`` / ``IN_NEITHER_SIDE`` — the ACCOUNT SCOPE,
#:   whose accepted key is ``team_id``. A coach manages a team's roster across
#:   every game it plays, and ``game_scoped_own_team_id`` answers a Coach out
#:   of the scope without reading the store at all; there is no per-game row
#:   to key on.
#: * ``PLAYER_SCOPED_BY_MEMBERSHIP`` — ``SeasonRosterMembership``, which
#:   carries ``league_season_id``, ``team_id``, ``player_id`` and ``status``.
#:   THERE IS A PER-GAME ROW (#427 round 12, LB1). Until this round Coach and
#:   Player were one class and the three lines above claimed the sentence
#:   they now carry alone: "there is no per-game row to key on". That is a
#:   true statement about a COACH and a false one about a PLAYER, and merging
#:   the two put ``SeasonRosterMembership`` — the row #205 exists to make
#:   authoritative — outside :data:`GRANT_ROWS`, outside
#:   :data:`GRANT_DIMENSIONS`, and outside every world in the matrix.
#: * the three unscoped classes — no grant row at all: their authority is the
#:   role's permissions, which name nothing.
GRANT_RECORD_FIELDS = {
    COACH_SCOPED_TO_ONE_SIDE: _scope_keys(Role.COACH),
    PLAYER_SCOPED_BY_MEMBERSHIP: _record_fields(SeasonRosterMembership),
    IN_NEITHER_SIDE: _scope_keys(Role.COACH),
    GUARDIAN_OF_A_JUNIOR: _record_fields(GuardianLink),
    OFFICIAL_SUBMITTED_LINEUP_ONLY: _record_fields(OfficialAssignment),
    OPERATOR_UNSCOPED_BY_DESIGN: frozenset(),
    UNSCOPED_OPERATOR_WITHOUT_ROSTER_AUTHORITY: frozenset(),
    VIEWER_ENTITLED_TO_NOTHING: frozenset(),
}

#: ``{entitlement class: the DOMAIN ROW the product stores its grant in}`` —
#: the two classes whose grant is a ROW rather than an account scope.
#:
#: WHY THIS EXISTS (#427 round 11). :data:`GRANT_RECORD_FIELDS` reads the
#: FIELD NAMES off these records and the previous round used exactly one of
#: them, ``game_id``. Three more instances of the same species were sitting
#: in the rest: the guardian's grant was aggregated across juniors exactly as
#: the official's had been across games, the guardian's link was read for ANY
#: guardian rather than the swept one, and ``OfficialAssignment.status`` — a
#: dimension the row DECLARES, with the product's own ``is_active`` predicate
#: on it — was keyed on by neither the gate nor this file. So the record
#: itself is carried, not just its field names, and every dimension it
#: carries keys the grant.
#:
#: STILL HAND-WRITTEN, AND THAT IS NOW AUDITED FROM ONE LEVEL UP (#427 round
#: 12, LB1). Round 11 derived the grant DIMENSIONS from the product row and
#: left the SET OF ROWS listed here, checked by nothing — so the enumeration
#: moved up one level and the hole moved with it: a class whose grant IS a
#: row could simply be absent, which is exactly what
#: ``PLAYER_SCOPED_BY_MEMBERSHIP`` was. Adding a third entry does not fix
#: that shape; what fixes it is that the ADMISSION BRANCHES are now DERIVED
#: FROM THE GATE'S OWN SOURCE (:func:`admission_branches`) and every
#: non-operator branch must name an authority here —
#: :class:`EveryAdmissionBranchIsDerivedAndCarriesAnAuthority` fails BY NAME
#: on a branch that does not, and the derivation is proved by injecting one.
GRANT_ROWS = {
    GUARDIAN_OF_A_JUNIOR: GuardianLink,
    OFFICIAL_SUBMITTED_LINEUP_ONLY: OfficialAssignment,
    PLAYER_SCOPED_BY_MEMBERSHIP: SeasonRosterMembership,
}

#: ``{domain row: the store method that reads EVERY row of that kind}``.
#:
#: DELIBERATELY THE UNFILTERED READER. ``assignments_for_game`` and
#: ``guardian_links_for_player`` each pre-key the fetch on ONE dimension,
#: which is how a dimension hides: ``_guardian_link_is_verified`` asked
#: ``guardian_links_for_player(junior)`` and then tested only ``verified``,
#: so the swept guardian was granted the junior on the strength of ANOTHER
#: guardian's link (F3). Fetching every row and filtering on every DECLARED
#: dimension means the fetch itself can conceal none of them.
GRANT_ROW_READERS = {
    GuardianLink: "all_guardian_links",
    OfficialAssignment: "all_official_assignments",
    # `memberships_for_player` pre-keys on the player, which is the same
    # shape `guardian_links_for_player` had when it hid F3.
    SeasonRosterMembership: "all_season_roster_memberships",
}

#: ``{domain row: the store method that writes one row of that kind}`` — used
#: ONLY by the measured dimension audit, which moves a field, re-asks the
#: ORACLES, and puts the row back.
#:
#: A STORE WRITE HERE IS NOT A PRODUCT STATE CHANGE, and the distinction is
#: the reason this map may exist at all. That audit asks a question about the
#: ORACLES — "does this file's own answer depend on this column?" — and never
#: asks the product anything, so what it needs is the ability to move a
#: column, not a workflow. Every place this file makes a claim about the
#: PRODUCT drives the product's own write path instead: `_perturbed`,
#: `_revoked` and :class:`ThePlayerGrantIsTheEligibleMembershipRow` all go
#: through `ApiService`/`SetupService`.
#:
#: It is DERIVED-SHAPED rather than derived: a store's writer cannot be read
#: off a dataclass. :meth:`TheGrantIsKeyedByEveryDimensionOfItsRow
#: .test_every_field_of_every_grant_row_is_declared_one_way_or_the_other`
#: requires one entry per grant row, so a row added to :data:`GRANT_ROWS`
#: without one is an error naming it rather than a silently unmeasured row.
GRANT_ROW_WRITERS = {
    GuardianLink: "save_guardian_link",
    OfficialAssignment: "save_official_assignment",
    SeasonRosterMembership: "save_season_roster_membership",
}

#: ``{entitlement class: the field names of its grant row that KEY the
#: grant}`` — derived, never listed. For the two row-backed classes this is
#: :func:`_keying_fields` of the record; for the scope-backed classes every
#: accepted scope key IS a subject binding, so the two coincide.
GRANT_DIMENSIONS = {
    klass: (_keying_fields(GRANT_ROWS[klass]) if klass in GRANT_ROWS
            else fields)
    for klass, fields in GRANT_RECORD_FIELDS.items()
}

#: ``{domain row: {field name: why it keys NOTHING}}`` — the residue of every
#: grant row, typed with a reason each.
#:
#: THE FAIL-CLOSED HALF, and the point of the whole round. Every field of
#: every grant row lands in exactly one of two places: :data:`GRANT_DIMENSIONS`
#: (derived, and then MEASURED to move the oracles) or here (typed, and then
#: MEASURED not to). A row that GAINS a field is therefore an ERROR NAMING
#: IT — the property the route, role and query-parameter axes already have —
#: rather than a dimension nobody keyed on, which is what the last three
#: rounds each found one of.
GRANT_FIELDS_THAT_KEY_NOTHING = {
    OfficialAssignment: {
        "id": "the row's OWN primary key: it IS the grant, not a dimension "
              "of it. See GRANT_FIELDS_NOT_PERTURBABLE.",
        "role": "referee / linesperson / scorekeeper. OfficialRole declares "
                "no is_active, and the sheet an assigned official may read "
                "is the same whichever role they hold — measured, not "
                "assumed: perturbing it moves no oracle answer.",
        "assigned_at": "when the offer was made — bookkeeping",
        "responded_at": "when it was answered — bookkeeping; the ANSWER "
                        "itself lives in `status`, which IS a dimension",
        "assigned_by": "which operator made the offer — an audit trail, not "
                       "an authority the official holds",
        "note": "operator free text",
    },
    SeasonRosterMembership: {
        "id": "the row's OWN primary key — see above",
        "position": "which position this stint plays (#269, season-scoped). "
                    "A sheet field, not an authority: it changes what the "
                    "roster SAYS about the player, never which game or side "
                    "the membership admits them to — measured, not assumed",
        "jersey_number": "the season-Team jersey (#269). Unique within a "
                         "season Team and therefore a real dimension of the "
                         "ROSTER; it keys no read, and the measured audit "
                         "below confirms perturbing it moves no oracle",
        "shoots": "left/right — a scouting attribute of the stint",
        "effective_from": "when the stint opened; NULL on a backfilled row, "
                          "first-class 'predates membership tracking'. "
                          "`resolve_membership_context` reads neither bound: "
                          "participation is answered by `status` against "
                          "`_ELIGIBLE_MEMBERSHIP_STATUSES` and by the "
                          "LeagueSeason/registration spine, never by a date "
                          "window — measured here, not read off the docstring",
        "effective_to": "when it closed — stamped by a terminal transition, "
                        "and the terminal STATUS is the dimension; see above",
    },
    GuardianLink: {
        "id": "the row's OWN primary key — see above",
        "created_at": "when the link was made — bookkeeping",
        "consent_method": "the GDPR Art. 8 consent RECORD (#35): HOW an "
                          "operator obtained authorization. `verified` is "
                          "what gates authority, and it is a dimension; this "
                          "is the paperwork behind it",
        "consented_at": "when that consent was recorded — bookkeeping",
    },
}

#: The one field of a grant row no perturbation can express, and why.
#:
#: A row's primary key is not a value the row CARRIES about something else —
#: it is what makes the row that row. Changing it in the store does not move
#: the grant; it makes a DIFFERENT grant and orphans the old one. So the
#: measured audit below skips exactly this field, and asserts that the set it
#: skips is exactly this one, so "not measurable" cannot quietly grow.
GRANT_FIELDS_NOT_PERTURBABLE = frozenset({"id"})

#: The dimension a PATH names when it names a game, and nothing else does.
GAME_SUBJECT = "game_id"

#: Every subject dimension any grant row declares, across all of them.
GRANT_SUBJECT_FIELDS = frozenset().union(
    *(_subject_fields(record) for record in GRANT_ROWS.values()))

#: The dimensions a read INHERITS FROM THE GAME ROW ITSELF — derived as the
#: fields ``Game`` declares that a grant row is keyed on BY THE SAME NAME.
#:
#: WHY A SUBJECT ROW CONTRIBUTES MORE THAN ITS OWN ID (#427 round 12, LB1).
#: ``SeasonRosterMembership`` is keyed on ``league_season_id``, and NO path in
#: this product names a LeagueSeason: what a private-game path names is a
#: GAME, and the game row is what fixes which competition the read belongs
#: to. Without this the Player's grant would match a membership in ANY
#: LeagueSeason, which is the "exact game-season" half of the #205 rule
#: dropped on the floor.
#:
#: THE GAME AND ONLY THE GAME, DELIBERATELY. The same trick applied to a
#: PLAYER subject row would inherit ``Player.team_id`` — the PERMANENT
#: POINTER — as a ``team_id`` dimension, and re-admitting the stale pointer
#: into this sweep's own reasoning is the exact defect #205 exists to remove.
#: A subject row may contribute a dimension only where the product's own
#: resolution does, and ``resolve_membership_context`` matches on
#: ``game.league_season_id`` directly.
SUBJECT_ROW_INHERITED = frozenset(_subject_fields(Game)) & GRANT_SUBJECT_FIELDS

#: The classes whose grant row NAMES A GAME, DERIVED from the dimensions.
#:
#: For these, and only these, state of a game OTHER than the response's own
#: subject may never excuse a diff: the grant was issued against one game, so
#: it cannot span another. See :meth:`_SweepHarness._grant_spans` and
#: :class:`NoGrantIsAggregatedAcrossADimensionItIsKeyedOn`, which asserts this
#: derived set against the sweep's own MEASURED behaviour, so the declared
#: authority and the running code have to agree.
GAME_KEYED_CLASSES = frozenset(
    klass for klass, fields in GRANT_DIMENSIONS.items()
    if "game_id" in fields)


# ---------------------------------------------------------------------------
# THE ADMISSION AXIS — DERIVED FROM THE GATE, NOT LISTED BESIDE IT.
#
# WHAT THIS REPLACES, AND WHY ONE MORE LIST WOULD HAVE BEEN THE SAME DEFECT
# (#427 round 12, LB1). Round 11 derived the grant DIMENSIONS from the product
# row — :func:`_subject_fields`, :func:`_activation_fields`,
# :func:`_keying_fields` — and left the SET OF ROWS hand-written in
# :data:`GRANT_ROWS`, audited by nothing. The enumeration moved up one level
# and the hole moved with it: an entitlement class whose grant IS a row could
# simply be ABSENT from that map, which is exactly what
# :data:`PLAYER_SCOPED_BY_MEMBERSHIP` was. Adding a third entry to
# ``GRANT_ROWS`` and calling LB1 closed would repeat the defect one round
# later, one level up.
#
# SO THE RULE IS ONE LEVEL UP AGAIN, and this repo has a working precedent
# for it. ``route_extract.query_parameter_names`` holds no list of the query
# parameters the server reads: it DERIVES that axis from the server's own
# ``parse_qs`` call sites and raises ``ExtractionError`` on any shape it
# cannot resolve, because a skip is not an option there. That axis is the one
# that came out genuinely CLOSED. The analogue for admission is to
#
#     ASK THE GATE WHICH ROLES IT ADMITS.
#
# :func:`admission_branches` parses ``services/game_side_scope.py`` and walks
# the two functions that TOGETHER decide a private-game read: the carrier
# ``resolve_private_game_read`` and the resolver ``game_scoped_own_team_id``
# it delegates the side to. Both names are taken from
# ``services/side_provenance.py``'s own constants rather than spelled again
# here, so renaming the gate breaks one place loudly instead of leaving this
# axis pointed at a function that no longer exists.
#
# WHAT MAKES IT FAIL-CLOSED, in four rules:
#
# * every ``return`` in the carrier must be a ``PrivateGameRead(...)`` naming
#   every field of the record — anything else is an
#   :class:`AdmissionExtractionError` at that line, because an admission
#   decided somewhere this cannot see is an admission nothing audits;
# * a role test that NAMES a ``Role`` and is not ``role == Role.X`` /
#   ``role in (Role.X, …)`` is refused outright, rather than guessed at;
# * a test that names no ``Role`` at all leaves the branch UNCONSTRAINED, and
#   an unconstrained branch is attributed to EVERY role. That is the safe
#   direction: a nested helper, ``role.value == "coach"`` or
#   ``getattr(Role, name)`` makes a branch look like one that admits
#   everybody, and every non-operator role in it then demands an authority;
# * and THE STATEMENT WALK ITSELF IS AN ALLOW-LIST (:func:`_decisions`): the
#   statement kinds that can carry a decision are handled, the two that are
#   provably inert are skipped with the proof written on the line, and
#   EVERYTHING ELSE RAISES NAMING ITS TYPE. See that function for why the
#   deny-list it replaces was the same defect one level along.
#
# THE COACH/PLAYER SPLIT IS PART OF THE DERIVATION, not an editorial choice.
# The carrier tests ``role in (Role.COACH, Role.PLAYER)`` as ONE branch; the
# resolver it delegates to splits them, answering a Coach from
# ``scope.get("team_id")`` and a Player from ``_player_team_for_game(scope,
# game, store)``. Following that one delegation is what turns one merged
# branch into the two separate authorization branches LB1's close condition 1
# asks for — and it is the GATE'S OWN STRUCTURE that says where the seam is.
# ---------------------------------------------------------------------------
class AdmissionExtractionError(AssertionError):
    """A shape in the gate this inventory will not guess at.

    An ``AssertionError`` deliberately: an unresolvable admission branch is a
    TEST FAILURE, not an infrastructure error to be caught and skipped — the
    same posture ``route_extract.ExtractionError`` takes towards a query
    string it cannot enumerate."""


#: The two functions that TOGETHER decide a private-game read, named from the
#: SUPPLEMENTAL scanner's own constants rather than spelled again here.
GATE_CARRIER = side_provenance.TRUSTED_CARRIER
GATE_RESOLVER = side_provenance.TRUSTED_RESOLVER

#: The decision record's own field names — so a field renamed in the product
#: makes the extraction fail rather than silently read the wrong keyword.
ADMISSION_FIELDS = _record_fields(game_side_scope.PrivateGameRead)

#: The record's own name, as the gate's source spells it at every return.
ADMISSION_RECORD = game_side_scope.PrivateGameRead.__name__

#: The record fields whose EXPRESSION the classification reads, as opposed to
#: the ones it reads only as a test against the literal ``None`` (#427 round
#: 19). :func:`admission_branches` folds these two through :func:`_inlined`
#: and pins the result as ``admits_source``/``side_source``, so a mutation
#: hidden inside one MOVES A PIN; ``game=`` becomes the boolean
#: ``carries_game`` and ``role=`` is not read at all, so a mutation hidden
#: inside either of those moves nothing.
#:
#: THIS IS WHAT :func:`_live_bindings` SEEDS FROM, and the ninth spelling of
#: round 19's own hunt is why. Seeding from a whole returned record made::
#:
#:     _t = scope.update({'team_id': store.get_game(game_id).home_team_id})
#:     scope = scope or {}
#:     if role is None:
#:         return PrivateGameRead(role=role, game=_t, own_team=None,
#:                                admitted=False)
#:
#: pass, because ``_t`` was "read" — by a field that collapses to a boolean.
#: The mutation runs for EVERY caller and the branch that names ``_t`` runs
#: for none of them.
#:
#: NOT A LIST SOMEBODY REMEMBERED:
#: :meth:`EveryAdmissionBranchIsDerivedAndCarriesAnAuthority
#: .test_the_deciding_fields_are_the_ones_the_classification_pins` reads
#: :func:`admission_branches`' OWN SOURCE and requires this to be exactly the
#: set of keywords whose resolved value it hands to :func:`_inlined`.
DECIDING_FIELDS = ("admitted", "own_team")


# ---------------------------------------------------------------------------
# WHAT A BRANCH GRANTS IS DECIDED BY WHAT THE CONSUMER READS (#427 round 14).
#
# THE MODEL THIS REPLACES WAS THE RECORD'S OWN NARRATIVE. ``needs_authority``
# was ``self.admits and self.carries_game``, and its stated ground was
# ``PrivateGameRead``'s docstring: an admission carrying ``game=None`` is the
# not-found passthrough and "grants nothing". ``game=None`` GRANTS NOTHING BY
# ITSELF — and that is not the same sentence. ``web/server.py``'s
# private-game dispatch reads ``private_read.admitted``,
# ``private_read.own_team`` and ``private_read.side_ids`` and NEVER re-checks
# ``private_read.game``; it then re-fetches the game BY ID for every leaf. So
# a branch with ``game=None`` AND A REAL ``own_team`` is a full disclosure
# that the audit exempted. MEASURED at ``c4a725b``: injecting
#
#     if role == Role.COACH:
#         return PrivateGameRead(role=role, game=None,
#                                own_team=game.home_team_id, admitted=True)
#
# gave ``thirdcoach`` — a coach of a team in NEITHER game — 200 on
# ``/lineups`` with ``home.restricted false`` and eight private rows, on
# ``/roster``, ``/roster-status`` and ``/substitutes``, while ``_audit()``
# returned ``[]``. A literal ``game=None`` and a ``game=_g`` that unfolds to
# ``None`` both did it.
#
# So the grant condition is DERIVED FROM THE CONSUMERS. :func:`_carrier_reads`
# finds every attribute the product reads off a carrier record, anywhere in
# the package; :data:`CARRIER_READ_KINDS` classifies each one and is asserted
# EQUAL to that derived set, so a consumer that starts reading a new attribute
# fails by name instead of inheriting a classification made for the old ones;
# and :data:`GRANT_BEARING_FIELDS` maps the side-bearing reads back to the
# record FIELDS behind them — ``own_team`` is a field, ``side_ids`` is a
# property over ``game`` — which is exactly the pair of keywords
# ``grants_side`` and ``carries_game`` read off each branch.
# ---------------------------------------------------------------------------

#: A read that decides whether the caller is answered AT ALL.
READ_GATES_ADMISSION = "gates admission"
#: A read that NAMES A SIDE, so a branch that fills it has granted one.
READ_NAMES_A_SIDE = "names a side"

#: ``{attribute of the carrier record: what reading it can disclose}``.
#: ASSERTED against :func:`_carrier_reads` — the product's own call sites —
#: by :meth:`EveryAdmissionBranchIsDerivedAndCarriesAnAuthority
#: .test_the_grant_condition_is_derived_from_what_the_consumers_read`.
CARRIER_READ_KINDS = {
    # `web/scope.can_read_private_game_data` and the dispatch's own 403.
    "admitted": READ_GATES_ADMISSION,
    # The TRUSTED SIDE, hoisted once and handed to every leaf of the family.
    "own_team": READ_NAMES_A_SIDE,
    # `(home, away)` of the game the decision was taken against.
    "side_ids": READ_NAMES_A_SIDE,
}


def _record_read_backing(record, attribute):
    """The record FIELDS ``attribute`` is answered out of.

    A declared field answers itself. A PROPERTY answers out of whatever
    ``self.<field>`` its body reads — ``side_ids`` is ``self.game``'s two
    team ids — so the mapping from "what the consumer reads" to "what
    keyword the branch sets" is read off the record instead of asserted
    about it."""
    fields = _record_fields(record)
    if attribute in fields:
        return frozenset({attribute})
    member = getattr(record, attribute, None)
    if not isinstance(member, property):
        raise AdmissionExtractionError(
            f"{record.__name__} has no field and no property named "
            f"{attribute!r}, so what a consumer reading it would receive "
            f"cannot be traced to any keyword of a branch")
    tree = ast.parse(textwrap.dedent(inspect.getsource(member.fget)))
    backing = {node.attr for node in ast.walk(tree)
               if isinstance(node, ast.Attribute)
               and isinstance(node.value, ast.Name)
               and node.value.id == "self"} & fields
    if not backing:
        raise AdmissionExtractionError(
            f"{record.__name__}.{attribute} reads no field of the record, "
            f"so nothing this inventory measures about a branch says what it "
            f"would answer")
    return frozenset(backing)


def _reads_in(rel, source):
    """``{attribute read off a carrier record in this module}``, or ``None``
    when the module holds no call site at all.

    FAIL-CLOSED IN TWO PLACES, because "what does the consumer read" is only
    an authority if it cannot quietly miss a read. Both are exercised by
    :meth:`EveryAdmissionBranchIsDerivedAndCarriesAnAuthority
    .test_a_consumer_this_cannot_follow_is_refused`:

    * a call site that is neither bound to a bare name nor immediately
      attribute-read is REFUSED — the record went somewhere this cannot
      follow, so what is taken off it is unknown;
    * so is a holder name used any way other than as ``holder.<attr>``,
      because passing the record on means the reads happen elsewhere."""
    tree = ast.parse(source)
    sites = [node for node in ast.walk(tree)
             if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
             and node.func.id == GATE_CARRIER]
    if not sites:
        return None
    holders, here = set(), set()
    parents = {id(child): node for node in ast.walk(tree)
               for child in ast.iter_child_nodes(node)}
    for site in sites:
        parent = parents.get(id(site))
        if isinstance(parent, ast.Attribute):
            here.add(parent.attr)
        elif isinstance(parent, ast.Assign) and len(parent.targets) == 1 \
                and isinstance(parent.targets[0], ast.Name):
            holders.add(parent.targets[0].id)
        else:
            raise AdmissionExtractionError(
                f"{rel}: the {GATE_CARRIER} record at line {site.lineno} is "
                f"neither bound to a name nor read immediately — it goes "
                f"somewhere this inventory cannot follow, so what the "
                f"product reads off it, and therefore what a branch grants "
                f"by filling it, cannot be derived")
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Name) and node.id in holders
                and isinstance(node.ctx, ast.Load)):
            continue
        parent = parents.get(id(node))
        if not isinstance(parent, ast.Attribute):
            raise AdmissionExtractionError(
                f"{rel} line {node.lineno}: the carrier record held by "
                f"{node.id!r} is used as a whole value, not as "
                f"`{node.id}.<attribute>`, so the reads taken off it happen "
                f"somewhere this inventory does not see")
        here.add(parent.attr)
    return frozenset(here)


def _carrier_reads():
    """``(modules, {attribute the product reads off a carrier record})`` —
    every call site of :data:`GATE_CARRIER` in the whole package, FOUND
    rather than listed, so a new consumer cannot be a silent gap.

    Finding NO call site at all is refused: a derivation with no sites would
    classify an empty set and agree with anything."""
    root = Path(inspect.getsourcefile(game_side_scope)).parent.parent
    modules, reads = {}, set()
    for path in sorted(root.rglob("*.py")):
        source = path.read_text()
        if GATE_CARRIER not in source:
            continue
        here = _reads_in(path.relative_to(root.parent).as_posix(), source)
        if here is None:
            continue
        modules[path.relative_to(root.parent).as_posix()] = here
        reads |= here
    if not modules:
        raise AdmissionExtractionError(
            f"no module under {root} calls {GATE_CARRIER}, so either the "
            f"gate has no consumer at all or this inventory is deriving "
            f"what a branch grants from a function nothing uses")
    return modules, frozenset(reads)


#: The record fields a branch can GRANT a side through — derived from the
#: reads classified :data:`READ_NAMES_A_SIDE`. Measured on this tree:
#: ``{'game', 'own_team'}``, which is precisely what ``carries_game`` and
#: ``grants_side`` read off each branch.
GRANT_BEARING_FIELDS = frozenset().union(*(
    _record_read_backing(game_side_scope.PrivateGameRead, attribute)
    for attribute, kind in CARRIER_READ_KINDS.items()
    if kind == READ_NAMES_A_SIDE))


@dataclasses.dataclass(frozen=True)
class AdmissionBranch:
    """ONE decision the gate takes, for ONE role, read off the gate's own
    source."""

    role: str            #: a ``domain.Role`` MEMBER NAME
    lineno: int          #: where the decision is returned
    admits: bool         #: ``admitted=`` is not literally ``False``
    carries_game: bool   #: ``game=`` is not literally ``None``
    authority: str       #: normalized source of what decides it; ``"True"``
    #: means an UNCONDITIONAL admission
    grants_side: bool = True    #: ``own_team=`` is not literally ``None``
    admits_source: str = ""     #: what THIS BRANCH itself tests, normalized
    side_source: str = ""       #: the side THIS BRANCH itself returns

    @property
    def needs_authority(self):
        """Does this branch let somebody read a REAL game's private state?

        Derived from the branch's own keywords AND FROM WHAT THE CONSUMERS
        READ (see :data:`CARRIER_READ_KINDS`), never listed. A refusal grants
        nothing. An admission grants something as soon as ANY of the
        record's :data:`GRANT_BEARING_FIELDS` is filled — the game the
        ``side_ids`` property answers out of, OR the trusted ``own_team``
        the dispatch hands every leaf of the family. Either one alone is a
        side, because either one alone is what the server acts on.

        The not-found passthrough is still exempt, and for the reason it
        always was rather than for the one that was written down: it fills
        NEITHER — ``game=None`` and ``own_team=None`` — so it admits every
        role precisely so the facade can answer its normal ``not_found``
        rather than a 403 that would confirm the id's absence differently
        from every other route."""
        return self.admits and (self.carries_game or self.grants_side)


def _role_aliases(tree):
    """Every name ``domain.Role`` is bound to in this module.

    ALIAS-AWARE ON PURPOSE: a text matcher for ``Role.`` is defeated by one
    ``as`` clause, and the query-parameter closure this is modelled on had to
    resolve ``parse_qs`` aliases for exactly the same reason."""
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name == "Role":
                    out.add(alias.asname or alias.name)
                elif alias.name.endswith("domain"):
                    out.add(f"{alias.asname or alias.name}.Role")
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.endswith("domain"):
                    out.add(f"{alias.asname or alias.name}.Role")
    return frozenset(out)


def _role_member(node, aliases):
    """The ``Role`` MEMBER NAME ``node`` names, or ``None``."""
    if not (isinstance(node, ast.Attribute)
            and node.attr in Role.__members__):
        return None
    return node.attr if ast.unparse(node.value) in aliases else None


def _names_a_role(node, aliases):
    return any(_role_member(sub, aliases) for sub in ast.walk(node))


def _module_constants(tree):
    """``{name: the module-level literal container bound to it}`` — so the
    ``_UNSCOPED_OPERATORS = (Role.LEAGUE_ADMIN, Role.ARENA_MANAGER)`` spelling
    the product already uses in ``services/lineup_visibility.py`` resolves,
    instead of degrading into "this branch admits everybody"."""
    return {node.targets[0].id: node.value for node in tree.body
            if isinstance(node, ast.Assign) and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and isinstance(node.value, (ast.Tuple, ast.List, ast.Set))}


def _role_parameter(fn):
    """The name the gate's own signature gives the role it decides by — its
    FIRST positional parameter, read off the function rather than spelled
    here, so renaming it follows automatically and REORDERING it fails loudly
    (every later role test then names a Role on some other name, which
    :func:`_resolve_roles` refuses).

    Both walked functions take ``(role, scope, …)``. That the role is the
    subject of the gate is the gate's own statement about itself."""
    parameters = list(fn.args.posonlyargs) + list(fn.args.args)
    if not parameters:
        raise AdmissionExtractionError(
            f"line {fn.lineno}: {fn.name} takes no positional parameter, so "
            f"there is no name this inventory can read its role tests as "
            f"being about")
    return parameters[0].arg


def _resolve_roles(test, aliases, constants, role_param):
    """The ``Role`` members ``test`` is true for, or ``None`` for a test that
    constrains no role — see the section comment for why ``None`` is the
    fail-closed answer and not a permissive one.

    THE TEST MUST BE ABOUT ``role_param`` ITSELF (#427 round 13). It used to
    be enough that the left of the comparison was ANY bare name, and the
    enclosing branches' role sets are INTERSECTED — so two tests on two
    DIFFERENT names, ``default_kind == Role.COACH`` outside and ``role ==
    Role.GUARDIAN`` inside, intersected ``{COACH} & {GUARDIAN}`` to the EMPTY
    SET and the return under them was attributed to NO role at all: a live
    branch admitting a Guardian to a real game, recorded nowhere and audited
    by nothing. A comparison of some other name against a ``Role`` member is
    now refused by the clause below instead, which is the same answer this
    already gave every other unreadable role test."""
    if isinstance(test, ast.Compare) and len(test.ops) == 1:
        left, op, right = test.left, test.ops[0], test.comparators[0]
        if isinstance(left, ast.Name) and left.id == role_param:
            if isinstance(op, (ast.Eq, ast.Is)):
                member = _role_member(right, aliases)
                if member:
                    return frozenset({member})
            if isinstance(op, ast.In):
                container = right
                if isinstance(container, ast.Name):
                    container = constants.get(container.id, container)
                if isinstance(container, (ast.Tuple, ast.List, ast.Set)):
                    members = set()
                    for elt in container.elts:
                        member = _role_member(elt, aliases)
                        if member is None:
                            raise AdmissionExtractionError(
                                f"line {test.lineno}: the role membership "
                                f"test includes {ast.unparse(elt)!r}, which "
                                f"is not a literal Role member, so the roles "
                                f"this branch admits cannot be enumerated")
                        members.add(member)
                    return frozenset(members)
    if _names_a_role(test, aliases):
        raise AdmissionExtractionError(
            f"line {test.lineno}: the role test {ast.unparse(test)!r} names "
            f"a Role in a shape this inventory does not resolve. Spell it "
            f"`{role_param} == Role.X` or `{role_param} in (Role.X, ...)` — "
            f"about {role_param} ITSELF, the gate's own role parameter — or "
            f"the roles this branch admits cannot be enumerated")
    return None


def _calls_to(node, name):
    """Every ``name(...)`` call inside ``node``, as CALL NODES.

    The nodes and not merely the fact of them (#427 round 16): "this branch
    delegates" was all that was ever read off a resolver call, and WHAT THE
    BRANCH HANDED THE RESOLVER was therefore in none of the three pins. The
    arguments are on these nodes."""
    return [sub for sub in ast.walk(node)
            if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Name)
            and sub.func.id == name]


def _calls(node, name):
    return bool(_calls_to(node, name))


def _rests_on(node, names):
    """Does ``node`` MENTION any of ``names``?

    "This branch delegates" is a fact about one assignment; "the resolver
    decides this branch" is a fact about what the branch RETURNS, and this
    is the difference between them."""
    return any(isinstance(sub, ast.Name) and sub.id in names
               for sub in ast.walk(node))


class _Unresolvable:
    """The value a binding takes when the walk CANNOT say what it is.

    :data:`UNRESOLVABLE` is the single instance. It is a MARKER and never an
    ``ast`` node on purpose: anything that reads a binding has to notice it,
    and :func:`_resolved` turns noticing it into a refusal."""

    __slots__ = ()

    def __repr__(self):
        return "<unresolvable>"


#: A name whose value at the point of a later read this walk cannot
#: determine — see :func:`_decisions`' poisoning rule.
UNRESOLVABLE = _Unresolvable()


def _assigned_names(node):
    """Every name BOUND anywhere inside ``node``, by any binding form.

    DELIBERATELY WIDER THAN THE STATEMENT ALLOW-LIST. The walk refuses most
    of these kinds outright, so on today's grammar only ``Assign`` and
    ``AnnAssign`` can reach here — but this function answers "what might a
    block have changed", and answering that with the same list the walk
    happens to handle would make the poisoning rule only as good as the
    allow-list it is meant to back up. A form this does not know still
    contributes nothing, which is why the walk's refusal is the primary
    guard and this is the second one."""
    out = set()
    for sub in ast.walk(node):
        if isinstance(sub, ast.Assign):
            targets = sub.targets
        elif isinstance(sub, (ast.AnnAssign, ast.AugAssign, ast.NamedExpr)):
            targets = [sub.target]
        elif isinstance(sub, (ast.For, ast.AsyncFor)):
            targets = [sub.target]
        elif isinstance(sub, (ast.With, ast.AsyncWith)):
            targets = [item.optional_vars for item in sub.items
                       if item.optional_vars is not None]
        elif isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef,
                              ast.ClassDef)):
            out.add(sub.name)
            continue
        elif isinstance(sub, (ast.Import, ast.ImportFrom)):
            out.update((alias.asname or alias.name).split(".")[0]
                       for alias in sub.names)
            continue
        elif isinstance(sub, ast.ExceptHandler) and sub.name:
            out.add(sub.name)
            continue
        elif isinstance(sub, (ast.MatchAs, ast.MatchStar)) and sub.name:
            out.add(sub.name)
            continue
        elif isinstance(sub, ast.MatchMapping) and sub.rest:
            out.add(sub.rest)
            continue
        else:
            continue
        for target in targets:
            out.update(name.id for name in ast.walk(target)
                       if isinstance(name, ast.Name))
    return out


def _unfold(node, bindings, depth=4):
    """A bare local name replaced by the expression last assigned to it, so
    ``admitted = <predicate>`` … ``admitted=admitted`` reports the PREDICATE
    and not the word ``admitted``.

    Stops at an :data:`UNRESOLVABLE` binding rather than substituting it:
    :func:`_resolved` is what turns that into the refusal."""
    while isinstance(node, ast.Name) and depth:
        bound = bindings.get(node.id, None)
        if bound is None or bound is UNRESOLVABLE:
            break
        node, depth = bound, depth - 1
    return node


def _resolved(node, bindings, where):
    """:func:`_unfold`, PLUS THE REFUSAL THAT MAKES THE BINDING MODEL FAIL
    CLOSED (#427 round 14).

    The walk's bindings used to be FLOW-INSENSITIVE IN THE UNSAFE DIRECTION.
    ``walk`` copies its bindings per body, so an assignment inside a nested
    block never reached the statements AFTER that block in the enclosing
    body, and ``_unfold`` went on reporting THE STALE OUTER LITERAL. Measured
    at ``c4a725b``::

        _ok = False
        if role == Role.GUARDIAN:
            if game is not None:
                _ok = True
            return PrivateGameRead(role=role, game=game,
                                   own_team=game.home_team_id, admitted=_ok)

    ``admitted=_ok`` unfolded to the outer ``False``, so ``admits`` was
    False, so ``needs_authority`` was False, and the branch was not reported
    at all — while at RUNTIME every Guardian reached it with ``_ok`` True.
    Driven live over authenticated HTTP with ``Role.COACH`` in place of
    ``Role.GUARDIAN`` (the role the projection layer answers a side for),
    ``thirdcoach`` — a coach of a team in NEITHER game — received 200 on
    ``/lineups`` with ``home.restricted false`` and EIGHT private rows, and
    ``_audit()`` returned ``[]``.

    THE ANSWER IS REFUSAL, NOT A JOIN. A join-aware walk would have to model
    what each branch does to each name, which is one more hand-reasoned
    model to be wrong about; refusing is the rule the statement allow-list
    already establishes — A SHAPE THE INVENTORY CANNOT READ MUST RAISE. So
    :func:`_decisions` marks every name a nested block assigns
    :data:`UNRESOLVABLE` for the remainder of the enclosing body, and this
    function refuses any expression that MENTIONS such a name ANYWHERE
    inside it, not merely at the top. Checking only the top level would miss
    ``own_team if admitted else None`` after a nested block re-assigned
    ``own_team``, which is the same defect one nesting level along."""
    node = _unfold(node, bindings)
    unresolvable = sorted({
        sub.id for sub in ast.walk(node) if isinstance(sub, ast.Name)
        and bindings.get(sub.id, None) is UNRESOLVABLE})
    if unresolvable:
        raise AdmissionExtractionError(
            f"line {getattr(node, 'lineno', '?')}: {where} reads "
            f"{ast.unparse(node)!r}, which mentions "
            f"{', '.join(repr(n) for n in unresolvable)} — assigned inside a "
            f"nested block, so what it holds HERE is not something this walk "
            f"can determine. It is REFUSED rather than resolved to the value "
            f"it had before that block: reading the stale outer literal is "
            f"how a branch that admits a real caller to a real game reported "
            f"`admitted=False` and was audited by nothing")
    return node


def _pattern_roles(pattern, aliases):
    """The ``Role`` members a ``case`` PATTERN matches, or ``None`` for the
    bare ``case _:`` wildcard, which constrains no role.

    Only the shapes a role gate is actually written in are resolved — the
    value pattern ``case Role.X:``, the or-pattern ``case Role.X | Role.Y:``,
    and the wildcard. EVERY OTHER PATTERN IS REFUSED rather than guessed at.
    A capture (``case r:``), an as-pattern (``case Role.X as r:``), a class,
    sequence, mapping or star pattern each bind a name or destructure a
    subject in ways this inventory does not model, and a pattern attributed
    to the WRONG roles is worse than one it declines to read: it would move a
    live branch under somebody else's authority and report green."""
    if isinstance(pattern, ast.MatchValue):
        member = _role_member(pattern.value, aliases)
        if member is None:
            raise AdmissionExtractionError(
                f"line {pattern.lineno}: the case pattern "
                f"{ast.unparse(pattern.value)!r} is not a literal Role "
                f"member, so the roles this case admits cannot be enumerated")
        return frozenset({member})
    if isinstance(pattern, ast.MatchOr):
        members = set()
        for alternative in pattern.patterns:
            alternative_roles = _pattern_roles(alternative, aliases)
            if alternative_roles is None:
                # `case Role.X | _:` matches everything the wildcard does.
                return None
            members |= alternative_roles
        return frozenset(members)
    if isinstance(pattern, ast.MatchAs) and pattern.pattern is None \
            and pattern.name is None:
        # `case _:` — matches every remaining subject and binds nothing, so
        # it constrains no role. That is the same UNCONSTRAINED answer
        # `_resolve_roles` gives a test naming no Role, and it lands in the
        # same safe direction: the arm is attributed to EVERY role.
        return None
    raise AdmissionExtractionError(
        f"line {pattern.lineno}: the case pattern {ast.unparse(pattern)!r} "
        f"({type(pattern).__name__}) is a shape this inventory does not "
        f"resolve to a set of roles. Spell the role arms `case Role.X:` or "
        f"`case Role.X | Role.Y:`, or the roles this branch admits cannot be "
        f"enumerated")


def _refuse_a_rebound_role(fn, stmt, role_param, bound):
    """THE ROLE-IDENTITY REFUSAL (#427 round 14).

    :func:`_role_parameter` reads the role's NAME off the signature and
    every role test in the function is then read as a statement about that
    name — but nothing checked the name still HOLDS the parameter. MEASURED
    at ``c4a725b``, injected into the carrier::

        role = Role.LEAGUE_ADMIN
        match role:                      # also reproduces with a plain `if`
            case Role.LEAGUE_ADMIN:
                return PrivateGameRead(role=role, game=game,
                                       own_team=game.home_team_id,
                                       admitted=True)

    At runtime EVERY role reaches that arm; the walk booked it to
    LEAGUE_ADMIN, which is an OPERATOR and therefore exempt, and ``_audit()``
    returned ``[]`` while a viewer, a guardian and a coach of a team in
    neither game each received 200 with eight private HOME rows.

    A separate function so it has its own seam: removing it must redden
    :meth:`EveryAdmissionBranchIsDerivedAndCarriesAnAuthority
    .test_a_function_that_rebinds_its_role_parameter_is_refused`."""
    if role_param not in bound:
        return
    raise AdmissionExtractionError(
        f"line {stmt.lineno}: {fn.name} ASSIGNS TO ITS OWN ROLE PARAMETER "
        f"{role_param!r} — {ast.unparse(stmt)!r}. Every role test in this "
        f"function is read as a statement about {role_param}, and after this "
        f"it is a statement about something else, so the roles each branch "
        f"admits would be attributed to whatever this names rather than to "
        f"whoever actually reaches it")


def _refuse_a_second_binding(fn, stmt, bindings, bound):
    """ONE BINDING PER NAME PER BODY.

    A second assignment to a name still holding a LIVE expression leaves
    :func:`_unfold` a choice of two answers and no rule for picking, which is
    the same species of stale read :func:`_poison` closes one nesting level
    out. Assigning OVER an :data:`UNRESOLVABLE` marker IS allowed and is how
    a name becomes readable again — the real gate does exactly that with
    ``admitted``, which the COACH/PLAYER branch binds and the OFFICIAL branch
    binds again after the first has been poisoned."""
    relive = sorted(name for name in bound if name in bindings
                    and bindings[name] is not UNRESOLVABLE)
    if not relive:
        return
    raise AdmissionExtractionError(
        f"line {stmt.lineno}: {fn.name} RE-ASSIGNS "
        f"{', '.join(repr(n) for n in relive)}, which this body has already "
        f"bound. Which of the two expressions a later read means is a flow "
        f"question this walk does not answer, so it is refused rather than "
        f"guessed at")


def _refuse_an_unvouched_argument(call, carrier, resolver, bindings, where,
                                  projected=frozenset()):
    """WHAT A DELEGATING BRANCH HANDS THE AUDITED RESOLVER (#427 round 16,
    the seventh model — and the one that made the other six describable
    while the answer was forged).

    ``admission_branches`` recorded WHETHER a branch delegates and WHAT THE
    RESOLVER RETURNS FOR THAT ROLE. It never recorded what THIS BRANCH HANDED
    THE RESOLVER, and none of the three pins can: ``authority`` is the
    resolver's own return expression, and ``admits_source``/``side_source``
    unfold to ``own_team is not None and own_team in (...)`` and ``own_team
    if admitted else None`` — text a forged argument leaves CHARACTER FOR
    CHARACTER unchanged. MEASURED at ``d3f5f1e``, changing only the argument
    list::

        own_team = game_scoped_own_team_id(
            role, {'team_id': game.home_team_id}, game, store)

    ``_audit()`` returned ``[]``, all three pins still read exactly what they
    pinned, and ``thirdcoach`` — a Coach of a team in NEITHER game — received
    200 over real authenticated HTTP on ``/lineups`` (HOME ``restricted
    false``, eight private rows), ``/board``, ``/roster``, ``/roster-status``
    and ``/substitutes``.

    THE RULE IS THAT EVERY ARGUMENT IS TRACEABLE TO THE GATE'S OWN
    PARAMETERS, UNFORGED, and it is FIVE conditions because there are five
    ways to break it — four here and the fifth in
    :func:`_refuse_a_rebuilt_carrier_argument`, which this hands every slot
    last. Each is derived from a signature this file READS
    rather than from a list it keeps: the resolver's own parameter names and
    the carrier's own parameter names.

    1. SHAPE — an argument must be a bare ``Name``. EVERY other expression
       kind is refused NAMING IT, which is the fail-closed default the
       statement allow-list already establishes: a literal, an attribute, a
       call, a reconstructed dict, a subscript, a conditional and every kind
       Python has not shipped yet are all "not a Name", so this cannot be
       one shape behind the grammar the way a deny-list was.
    2. IDENTITY — that name must be THE RESOLVER'S OWN PARAMETER NAME for
       the slot it fills, read off the resolver's ``def``. "Passed straight
       through" is the only relation between an argument and a parameter
       this inventory can vouch for; A DIFFERENT VARIABLE in that slot —
       ``forged`` for ``scope`` — is a substitution nothing here can check,
       and it reaches the same disclosure by a longer road.
    3. PROVENANCE — the name must resolve, through THIS BODY'S OWN BINDINGS,
       to an expression built only out of the CARRIER'S parameters. Every
       ``Name`` leaf must be one of them; a ``Constant`` leaf is refused
       outright, because a literal is by definition not something the gate
       was handed; and when the argument is named for a carrier parameter it
       must still MENTION that parameter, which is what catches the
       one-line rebinding that leaves the call site untouched::

           scope = {'team_id': store.get_game(game_id).home_team_id}

       ROUND 14'S BINDING MODEL IS WHAT ANSWERS HERE, reused rather than
       re-implemented: the unfold and its refusal are :func:`_resolved`, so a
       name a nested block may have changed is already :data:`UNRESOLVABLE`
       (:func:`_poison`) and a name bound twice is already refused
       (:func:`_refuse_a_second_binding`) — the literal
       ``scope = scope or {}`` / ``scope = {...}`` pair is caught by that
       rule and never reaches this one.
    4. AND A SLOT FILLED BY A LOCAL MUST CARRY SOMETHING THE RESOLVER IS NOT
       ALREADY BEING HANDED. Found by this round's OWN hunt, against the
       three rules above, and MEASURED live::

           game = store.all_games().pop()

       — the gate's one line that selects the subject, replaced by a value
       that is a function of ``store`` ALONE. It passes 1, 2 and 3: the call
       site still reads ``game``, ``game`` is not a parameter so nothing
       requires it to mention itself, and its only leaf IS a parameter. At
       ``d3f5f1e`` with this rule absent it left ``_audit() == []`` while
       ``thirdcoach`` — a Coach of a team in NEITHER side of ``game_1`` —
       received 200 on ``/lineups`` and ``/board`` for that game, breaching
       the family's 403 boundary (the projection layer then refused the rows
       themselves, which is defence in depth and not this axis's answer).
       The rule that closes it is an information statement rather than a
       shape one: the resolver's ``game`` slot exists to carry the SUBJECT of
       the request, and a value computed from nothing but arguments the
       resolver already has in its other slots carries no subject at all.
       ``store.get_game(game_id)`` carries ``game_id``, which no other slot
       hands over; ``store.all_games().pop()`` carries nothing.

    5. AND A SLOT THE CARRIER HAS AN INPUT FOR MUST STILL HOLD THAT INPUT
       (#427 round 18, the owner's blocker 2). Conditions 1-4 VOUCH A NAME,
       NOT A VALUE: ``scope = scope.fromkeys(scope, store.get_game(game_id)
       .home_team_id)`` satisfies every one of them and rebuilds the value
       anyway. :func:`_refuse_a_rebuilt_carrier_argument` is the seam that
       answers it, and its docstring carries the reasoning — including why
       ``scope = scope or {}``, the real gate's own normalisation, is NOT
       refused, and what the one slot it cannot speak to costs.

    MEASURED against the real gate, all four arguments pass: ``role`` and
    ``store`` are parameters read unchanged, ``scope`` resolves to
    ``scope or {}`` (its own parameter normalised against an EMPTY default —
    no literal leaf, and condition 5's identity form), and ``game`` resolves
    to ``store.get_game(game_id)``, whose only leaves are two parameters, one
    of which — ``game_id`` — is handed to the resolver nowhere else."""
    resolver_params = _positional_parameters(resolver, where)
    carrier_params = _positional_parameters(carrier, where)
    if call.keywords or any(isinstance(a, ast.Starred) for a in call.args) \
            or len(call.args) != len(resolver_params):
        raise AdmissionExtractionError(
            f"line {call.lineno}: the branch hands {resolver.name} "
            f"{ast.unparse(call)!r}, which is not exactly its "
            f"{len(resolver_params)} positional parameter(s) as plain "
            f"positional arguments. Which argument answers which parameter "
            f"is then a matching rule this inventory does not model, so what "
            f"this branch resolves the caller's side FROM cannot be vouched "
            f"for")
    handed = {a.id for a in call.args if isinstance(a, ast.Name)}
    for argument, parameter in zip(call.args, resolver_params):
        spelled = ast.unparse(argument)
        if not isinstance(argument, ast.Name):
            raise AdmissionExtractionError(
                f"line {argument.lineno}: the branch hands {resolver.name} "
                f"{spelled!r} ({type(argument).__name__}) for its "
                f"{parameter!r} parameter. The only argument this inventory "
                f"can vouch for is the gate's own {parameter!r} PASSED "
                f"STRAIGHT THROUGH: what the resolver answers is pinned, "
                f"what this branch tests is pinned and what it returns is "
                f"pinned, and a forged argument leaves all three reading "
                f"exactly what they pinned while the resolver answers a side "
                f"nobody asked it about")
        if argument.id != parameter:
            raise AdmissionExtractionError(
                f"line {argument.lineno}: the branch hands {resolver.name} "
                f"{spelled!r} for its {parameter!r} parameter. A DIFFERENT "
                f"VARIABLE in that slot is a substitution nothing this "
                f"inventory measures can see — every pin reads the same text "
                f"either way — so only {parameter!r} itself, the name the "
                f"resolver's own signature gives this slot, is vouchable")
        if parameter in projected:
            # A PROJECTED ID IS VOUCHED BY ITS PROJECTION, NOT BY PROVENANCE
            # (#427 round 20). Conditions 3, 4 and 5 exist to ask "could this
            # value be something the gate was not handed" of an expression
            # this walk has to reason about. A projection is not such an
            # expression: `_projections` has already required it to be
            # EXACTLY `<the mapping>.get(<a key the product accepts>)`,
            # named for its own key, taken once, above every role branch;
            # `_refuse_a_second_binding` has required it to be written once;
            # condition 8 has
            # required this slot to be the one that key is named for. That is
            # a STRICTLY STRONGER statement than "every leaf is a parameter
            # and no leaf is a literal" — which this value would fail anyway,
            # because THE SCOPE KEY IS A LITERAL and has to be. Handing the
            # slot to conditions 3-5 here would refuse the projected gate for
            # the one shape that is not the defect, exactly as condition 5
            # would refuse the `game` slot if it were asked of it.
            continue
        value = _resolved(
            argument, bindings,
            f"the {parameter!r} argument the branch at line {call.lineno} "
            f"hands {resolver.name}")
        names = sorted({sub.id for sub in ast.walk(value)
                        if isinstance(sub, ast.Name)})
        outside = [n for n in names if n not in carrier_params]
        if outside or not names:
            raise AdmissionExtractionError(
                f"line {call.lineno}: the {parameter!r} the branch hands "
                f"{resolver.name} holds {ast.unparse(value)!r}, which is "
                f"built out of "
                f"{', '.join(repr(n) for n in outside) or 'no name at all'} "
                f"— not {carrier.name}'s own parameter(s) "
                f"{', '.join(repr(n) for n in carrier_params)}. An argument "
                f"the gate did not RECEIVE is one the pins cannot vouch for")
        if parameter in carrier_params and parameter not in names:
            raise AdmissionExtractionError(
                f"line {call.lineno}: {carrier.name} hands {resolver.name} "
                f"the name {parameter!r}, but by then {parameter!r} holds "
                f"{ast.unparse(value)!r}, which no longer derives from the "
                f"gate's OWN {parameter!r} parameter. The call site's text "
                f"never moved and neither did any of the three pins, so a "
                f"branch that resolves the caller's side from a value the "
                f"gate REBUILT rather than RECEIVED would read green")
        if parameter not in carrier_params and not (set(names) - handed):
            raise AdmissionExtractionError(
                f"line {call.lineno}: the {parameter!r} the branch hands "
                f"{resolver.name} is the local {spelled!r}, which holds "
                f"{ast.unparse(value)!r} — a value computed from "
                f"{', '.join(repr(n) for n in names)} alone, and "
                f"{resolver.name} is handed "
                f"{', '.join(repr(n) for n in sorted(handed & set(names)))} "
                f"already. This slot carries the SUBJECT of the request, and "
                f"a value the resolver could have computed for itself out of "
                f"its own other arguments carries no subject at all — so "
                f"what side it answers about is not traceable to anything "
                f"the gate was asked")
        literals = [ast.unparse(sub) for sub in ast.walk(value)
                    if isinstance(sub, ast.Constant)]
        if literals:
            raise AdmissionExtractionError(
                f"line {call.lineno}: the {parameter!r} the branch hands "
                f"{resolver.name} holds {ast.unparse(value)!r}, which is "
                f"built partly out of the literal(s) "
                f"{', '.join(literals)}. A literal is by definition not "
                f"something this gate was handed, so what the resolver "
                f"answers about is not traceable to the gate's own "
                f"parameters")
        _refuse_a_rebuilt_carrier_argument(
            call, carrier, resolver, parameter, value, carrier_params)


def _carries_nothing(node):
    """An EMPTY container display — a value that can carry no authority.

    ``{}``, ``[]``, ``()``. Emptiness is asked as "has no ``Name`` and no
    ``Constant`` anywhere inside it" rather than by looking at the node's own
    element list, so a nested non-empty display cannot slip through a
    top-level emptiness test."""
    return isinstance(node, (ast.Dict, ast.List, ast.Set, ast.Tuple)) and not [
        leaf for leaf in ast.walk(node)
        if isinstance(leaf, (ast.Name, ast.Constant))]


def _is_the_carrier_input_itself(value, parameter):
    """Is ``value`` STILL the gate's own ``parameter``, or has it been
    REBUILT?

    THE DISTINCTION THIS FUNCTION IS (#427 round 18, blocker 2). Two shapes
    both "mention the parameter" and only one of them preserves its VALUE:

        scope = scope or {}                    # NORMALISATION -- identity
        scope = scope.fromkeys(scope, …)       # REBUILDING    -- not

    Exactly two forms answer YES, and every other expression Python has or
    will ship answers NO, which is the fail-closed default the statement
    allow-list already establishes:

    1. THE BARE NAME. ``value`` IS the parameter.
    2. AN ``or``-CHAIN whose FIRST operand is (recursively) the parameter and
       whose every remaining operand :func:`_carries_nothing`. ``x or {}``
       evaluates to ``x`` whenever ``x`` is truthy and to an EMPTY container
       otherwise, so the value handed on is either exactly what the gate
       received or a container with nothing in it. ``x or {'team_id': …}``
       is NOT this: a default that carries a side is a side the gate was
       never handed, and it reaches the resolver on precisely the input the
       caller supplied nothing for.

    A ``Call``, an ``Attribute``, a ``Subscript``, a ``Dict``, an ``IfExp``,
    a ``BinOp``, an ``and``-chain and anything else are all REBUILDING: they
    may compute something equal to the parameter, and equality is not the
    question. What the pins can vouch for is the value the gate RECEIVED."""
    if isinstance(value, ast.Name):
        return value.id == parameter
    if isinstance(value, ast.BoolOp) and isinstance(value.op, ast.Or):
        first, *defaults = value.values
        return _is_the_carrier_input_itself(first, parameter) and all(
            _carries_nothing(alternative) for alternative in defaults)
    return False


def _refuse_a_rebuilt_carrier_argument(call, carrier, resolver, parameter,
                                       value, carrier_params):
    """CONDITION 5: A SLOT THE CARRIER HAS AN INPUT FOR MUST HOLD THAT INPUT
    UNCHANGED (#427 round 18, blocker 2).

    WHAT THIS CLOSES. Round 16's PROVENANCE condition vouched a NAME, not a
    VALUE: the argument had to resolve to an expression whose every ``Name``
    leaf is a carrier parameter, with no ``Constant`` leaf, and still
    MENTIONING the parameter it fills. MEASURED at ``73faf4d``, replacing
    only the carrier's own normalisation::

        scope = scope.fromkeys(scope, store.get_game(game_id).home_team_id)

    The call argument is still the bare ``scope``; it mentions the original
    parameter; its leaves are ``scope``, ``store`` and ``game_id``, all
    carrier parameters; and it contains no literal. It satisfies SHAPE,
    IDENTITY, PROVENANCE and SUBJECT, and ``_audit(mutated_source)``
    returned ``[]`` — while a live equivalent gave ``thirdcoach``, a Coach
    of a team in NEITHER game, 200 on ``/lineups`` with HOME
    ``restricted=false`` and EIGHT private players.

    THE TENSION, AND HOW IT IS RESOLVED RATHER THAN AVOIDED. The real gate's
    own first line is ``scope = scope or {}``, which is ALSO a rebinding, so
    a rule that refuses every rebinding refuses the real gate. "Mentions the
    parameter" was the original attempt to separate the two and it separates
    nothing: both mention it. The separation taken here is IDENTITY OF
    VALUE, not of name — :func:`_is_the_carrier_input_itself` — and it is a
    positive allow-list of the two forms that provably evaluate to the
    received value, with everything else refused. ``scope or {}`` is
    admitted because it evaluates to ``scope`` or to a container carrying
    nothing; ``scope.fromkeys(…)`` is refused because it evaluates to
    something the gate built. The rule is not weakened to keep the gate
    parsing, and the gate needs no production change to satisfy it.

    WHAT IT DOES NOT COVER, AND THE COST OF FAILING CLOSED THERE. This
    condition can only speak to a slot the CARRIER HAS AN INPUT FOR — a
    resolver parameter whose name is also a carrier parameter. Three of the
    real gate's four are (``role``, ``scope``, ``store``). The fourth,
    ``game``, is not: the carrier receives a ``game_id`` and the resolver
    wants a ``Game`` ROW, so there is no received value for it to be
    IDENTICAL TO and "unchanged carrier input" has no referent. That slot is
    still vouched by conditions 3 and 4 — every leaf a carrier parameter, no
    literal, and a SUBJECT no other slot already hands over — which are
    strictly weaker than identity. FAILING CLOSED THERE WOULD REFUSE THE
    REAL GATE for a shape that is not the defect: the cost of not failing
    closed is that a ``game`` rebuilt out of carrier parameters in some
    other subject-carrying way is vouched by derivation rather than by
    identity, and this file does not claim otherwise.

    SO THIS DOES NOT CLOSE LB1 CONDITION 5, and nothing here says it does.
    It closes the ``(scope, role, store)`` half of the seventh model, which
    is the half the owner's falsifier walked through.

    A separate function so it has its own seam: removing it must redden
    :meth:`EveryAdmissionBranchIsDerivedAndCarriesAnAuthority
    .test_a_carrier_parameter_the_gate_rebuilt_is_refused`."""
    if parameter not in carrier_params:
        # No carrier input of this name exists, so identity is not a
        # question that can be asked. See the docstring's disclosed cost.
        return
    if _is_the_carrier_input_itself(value, parameter):
        return
    raise AdmissionExtractionError(
        f"line {call.lineno}: the {parameter!r} the branch hands "
        f"{resolver.name} is REBUILT rather than RECEIVED — by the call it "
        f"holds {ast.unparse(value)!r}, which is not {carrier.name}'s own "
        f"{parameter!r} and is not that parameter normalised against an "
        f"empty default. Every leaf of it may be a parameter of this gate "
        f"and it may still MENTION {parameter!r}: an expression that "
        f"REBUILDS a value out of the gate's inputs is not the value the "
        f"gate was HANDED, and only the value it was handed is what the "
        f"three pins vouch for. The call site's text never moves, so a "
        f"branch that resolves the caller's side from a rebuilt "
        f"{parameter!r} would otherwise read green")


def _read_expressions(fn):
    """The expressions of ``fn`` THIS DERIVATION READS AS A WHOLE, and the
    ROLE-TEST positions it reads ONE NAME out of.

    ``(return values, role-test expressions)``. A ``return`` value is read
    entire — :func:`admission_branches` classifies every keyword of the
    record and :func:`_resolver_authorities` classifies the resolver's answer
    — so every name in it is a name something here reads.

    AN ``if`` TEST AND A ``match`` SUBJECT ARE NOT, AND THE DIFFERENCE IS
    WHERE THE EIGHTH SPELLING LIVED (#427 round 19). :func:`_resolve_roles`
    reads a test only as A STATEMENT ABOUT THE ROLE PARAMETER — the left of
    a comparison, the members of a container — and :func:`_decisions` reads a
    ``match`` subject only to ask whether it IS that parameter. Everything
    else inside a test is evaluated and thrown away as far as this walk is
    concerned, which is why::

        if scope.update({'team_id': store.get_game(game_id).home_team_id}):
            pass

    slipped condition 6 as first written: the test WAS a "root", so the
    mutation inside it counted as read. It is not read; only the role's own
    appearance there is. So these two lists are returned separately and
    :func:`_refuse_a_mutated_input` admits only the role parameter out of the
    second."""
    returns, tests = [], []
    for node in ast.walk(fn):
        if isinstance(node, ast.If):
            tests.append(node.test)
        elif isinstance(node, ast.Match):
            tests.append(node.subject)
        elif isinstance(node, ast.Return) and node.value is not None:
            returns.append(node.value)
    return returns, tests


def _vouched_arguments(fn):
    """Every BARE ``Name`` handed to the audited resolver by ``fn``.

    THESE ARE READ, AND BY THE STRICTEST RULE IN THIS FILE.
    :func:`_refuse_an_unvouched_argument` requires each of them to be a bare
    ``Name`` that is the resolver's own parameter name for its slot and that
    resolves to the gate's own input, so an argument position is the one
    place a mutation cannot hide: anything that is not a bare ``Name`` is
    refused by condition 1 before this rule is ever consulted. Keyword
    arguments are included for the same reason — the call SHAPE is refused
    by condition 1 too, and refusing the name twice would take that
    refusal's falsifier away from it."""
    out = set()
    for call in _calls_to(fn, GATE_RESOLVER):
        for argument in list(call.args) + [kw.value for kw in call.keywords]:
            if isinstance(argument, ast.Name):
                out.add(id(argument))
    return out


def _named_bindings(fn):
    """``[(name, value expression, statement), …]`` for every assignment in
    ``fn`` whose target is EXACTLY ONE BARE NAME.

    The same shape :func:`_decisions` binds and :func:`_decided_by` folds,
    and deliberately no other: a subscript, an attribute, a tuple unpack or
    a walrus target is a binding whose name-to-value map this file does not
    model, and :func:`_refuse_a_mutated_input` refuses a vouched input that
    appears in one rather than reading it."""
    out = []
    for node in ast.walk(fn):
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
        else:
            continue
        if len(targets) == 1 and isinstance(targets[0], ast.Name) \
                and node.value is not None:
            out.append((targets[0].id, node.value, node))
    return out


def _live_bindings(fn):
    """The names ``fn`` binds whose value THIS DERIVATION EVENTUALLY READS.

    A least fixed point over :func:`_named_bindings`, seeded from exactly the
    three READ positions :func:`_refuse_a_mutated_input` documents — a
    ``return`` value, the ROLE PARAMETER's own appearance in a role test, and
    a bare-``Name`` argument handed to the audited resolver. A name is LIVE
    when it is read from one of those, or from the value of a binding that
    is itself LIVE. On the real carrier that is ``game``, ``own_team``,
    ``admitted``, ``official_id`` and ``scope``; on the real resolver it is
    ``scope``.

    THE SEEDING IS THE NARROW ONE AND THAT IS DELIBERATE, IN TWO PLACES,
    both found by hunting this rule rather than reasoning about it. Seeding
    from whole ``if`` tests would make a name LIVE by being tested::

        _x = scope.update({'team_id': …})
        if _x:
            pass

    and seeding from a whole returned record would make one live by being
    RETURNED IN A FIELD THAT COLLAPSES TO A BOOLEAN::

        _t = scope.update({'team_id': …})
        …
        if role is None:
            return PrivateGameRead(role=role, game=_t, own_team=None,
                                   admitted=False)

    Both launder a mutation through a binding the walk had called live, and
    the second is LIVE ON THE WIRE: the mutation runs for every caller while
    the branch naming ``_t`` runs for none. So a returned record seeds only
    :data:`DECIDING_FIELDS`, whose expressions the classification really does
    read, and any OTHER return — the resolver's own answer — seeds entire.
    What makes a binding live is that the DECISION reads it, not that
    something looked at it.

    WHAT THE ANSWER IS FOR. A statement that MENTIONS one of the gate's own
    inputs and whose value nothing here reads is a statement kept for its
    EFFECT, and the only effect available to it is changing the object that
    input names — which is exactly what identity of value forbids. See
    :func:`_refuse_a_mutated_input`."""
    reads = {}
    for name, value, _stmt in _named_bindings(fn):
        reads.setdefault(name, []).append(value)
    live = set()

    def take(nodes):
        for node in nodes:
            if isinstance(node, ast.Name) and node.id in reads \
                    and node.id not in live:
                live.add(node.id)
                for value in reads[node.id]:
                    take(ast.walk(value))

    returns, tests = _read_expressions(fn)
    role_param = _role_parameter(fn)
    vouched = _vouched_arguments(fn)
    for value in returns:
        if isinstance(value, ast.Call) and isinstance(value.func, ast.Name) \
                and value.func.id == ADMISSION_RECORD:
            for keyword in value.keywords:
                if keyword.arg in DECIDING_FIELDS:
                    take(ast.walk(keyword.value))
        else:
            take(ast.walk(value))
    take(sub for expr in tests for sub in ast.walk(expr)
         if isinstance(sub, ast.Name) and sub.id == role_param)
    take(sub for sub in ast.walk(fn) if id(sub) in vouched)
    return live


def _refuse_a_mutated_input(fn, where, what):
    """CONDITION 6: AN INPUT MAY BE MENTIONED ONLY WHERE THE MENTION IS READ
    (#427 round 19, blocker 2 class A).

    WHAT THIS CLOSES. Condition 5 asks what the ARGUMENT EXPRESSION at the
    call site evaluates to. It never asks whether the OBJECT that expression
    names was changed before the call, so the identity of the NAME was
    vouched while the identity of the VALUE was destroyed. MEASURED at
    ``8a6ed27``, five spellings, each inserted above the gate's own
    normalisation and each leaving the call site's text and all three pins
    character-for-character unchanged::

        scope['team_id'] = store.get_game(game_id).home_team_id
        (scope or {})['team_id'] = store.get_game(game_id).home_team_id
        _t = scope.update({'team_id': store.get_game(game_id).home_team_id})
        _t = [scope.__setitem__('team_id', …)]
        _t: object = scope.__setitem__('team_id', …)

    ``_audit()`` returned ``[]`` for all five, and each one, compiled into
    the running server, gave ``thirdcoach`` — a Coach of a team in NEITHER
    game — 200 on all ten leaves of the private-game family on BOTH games on
    Memory, SQLite and real PostgreSQL: 60 admissions out of 60, with eight
    of HOME's private players in the ``gid`` ``/lineups`` body at
    ``restricted=false``.

    THE DIRECTION TAKEN, AND WHY IT IS NOT A DENY-LIST. "Which methods
    mutate" cannot be answered from an AST — ``scope.update`` does,
    ``scope.get`` does not, and a name that is not a ``dict`` at all may do
    either — so a list of mutating methods would be one spelling behind
    forever, which is the deny-list failure the statement walk already
    rejects. The question asked instead is POSITIVE and is about the
    POSITION rather than the METHOD: every occurrence of an input must be
    somewhere THE DERIVATION READS. Exactly four positions qualify, and each
    is a position something in this file genuinely reads:

    * inside a ``return`` value, which :func:`admission_branches` and
      :func:`_resolver_authorities` classify keyword by keyword;
    * inside the VALUE of a binding to a single bare name that
      :func:`_live_bindings` finds LIVE, or as the TARGET of one;
    * as a bare ``Name`` argument handed to the audited resolver, which
      :func:`_refuse_an_unvouched_argument` vouches by five conditions;
    * and, for the ROLE PARAMETER ALONE, inside an ``if`` test or a
      ``match`` subject — the only thing :func:`_resolve_roles` and
      :func:`_decisions` read out of one.

    Everything else is refused NAMING THE STATEMENT. A subscript target
    (``scope['team_id'] = …``) is not a single bare name, so no occurrence
    inside it can qualify; a mutating call laundered through a binding
    (``_t = scope.update(…)``) is refused because ``_t`` is read by nothing,
    which is the whole of what makes that statement a mutation rather than a
    computation; and a mutation hidden in a test that constrains no role —

        if scope.update({'team_id': store.get_game(game_id).home_team_id}):
            pass

    — is refused because a test is read only as a statement about the role,
    which is THE EIGHTH SPELLING THIS ROUND'S OWN HUNT FOUND against the
    rule as first written, when whole ``if`` tests still counted as read. It
    was green at ``8a6ed27`` as well, in the carrier and in the resolver
    alike, so it is an escape of the state under review and not only of this
    round's own first attempt — and each is live: 60 live admissions out of
    60, tri-store.

    WHAT IT COSTS, STATED RATHER THAN DISCOVERED. The gate may no longer
    MENTION one of its own inputs for a purpose invisible to this
    derivation. A log line, a metric, an assertion or a debug read of
    ``scope`` above the delegation is refused even though it changes
    nothing, and the answer to wanting one is to spell it so its value
    reaches the decision, not to widen this rule. THE REAL GATE NEEDS NO
    PRODUCTION CHANGE: every mention of ``role``, ``game_id`` and ``store``
    in the carrier, and of ``role``, ``game`` and ``store`` in the resolver,
    is already in one of the three positions — ``role`` in the branch tests
    and in the returned records, ``store`` and ``game_id`` in
    ``game = store.get_game(game_id)`` and in ``admitted =
    … store.assignments_for_game(game_id)`` (``game`` and ``admitted`` are
    both LIVE). ``scope`` is not in this list any more: since round 20 it is
    policed by :func:`_refuse_a_raw_scope_use`, which is strictly stronger.

    THE RESIDUE, CORRECTED (#427 round 20). THIS DOCSTRING PREVIOUSLY
    CARRIED A FALSE CLAIM AND IT IS REMOVED RATHER THAN SOFTENED. It said
    the sharpest residue found — the OFFICIAL branch's own read, with a
    mutation ridden in on an ``or`` —

        official_id = (scope.update({'team_id': game.home_team_id})
                       or scope.get("official_id"))

    was "A COVERAGE residue with no disclosure behind it on this gate",
    justified by "the branch it sits in runs only AFTER the COACH/PLAYER
    branch has returned". THAT WAS FALSE, and the thing that falsified it
    was a two-line RELOCATION within the same function: hoist the statement
    above the COACH/PLAYER branch and replace the original assignment with
    ``pass``. ``official_id`` stays live, so this rule still admits it — but
    it now runs FIRST. Static audit ``[]``; and over real authenticated
    HTTP, TWENTY of the FIFTY derived unentitled cells answered 200, with
    THIRTEEN private side identities across those bodies and EIGHT of HOME's
    private players in the ``gid`` ``/lineups`` body at ``restricted=false``
    — on Memory, SQLite and real PostgreSQL alike, byte-identical to the
    leak set of the owner's own spelling. An ALIAS of the same statement
    measured identically.

    THE RESIDUE'S HARMLESSNESS WAS A PROPERTY OF THE POSITION TESTED, NOT OF
    THE CLASS. That is the whole reason this round changed the production
    boundary instead of writing an eighth rule over the mapping: what this
    rule can honestly claim is that no statement's ONLY purpose CAN be
    mutation, and a mutation the decision genuinely consumes is outside
    that claim in every position, not only in the one that was measured.

    WHAT IS LEFT OF THE RESIDUE, AND IT IS NARROWER. This still does not
    prove NON-MUTATION: a call that mutates AND whose result the decision
    consumes — ``store.get_game(game_id)`` is one, for all this walk knows —
    is vouched by derivation rather than by identity. For the SESSION
    MAPPING that residue is now closed, not by this rule but by the
    boundary: there is no mapping here to mutate. For ``store`` it stands,
    and is disclosed rather than argued away.

    A separate function so it has its own seam: removing it must redden
    :meth:`EveryAdmissionBranchIsDerivedAndCarriesAnAuthority
    .test_an_input_mutated_before_the_call_is_refused`."""
    inputs = set(_positional_parameters(fn, where))
    live = _live_bindings(fn)
    role_param = _role_parameter(fn)
    returns, tests = _read_expressions(fn)
    allowed = set(_vouched_arguments(fn))
    for expr in returns:
        allowed.update(id(sub) for sub in ast.walk(expr))
    for expr in tests:
        allowed.update(id(sub) for sub in ast.walk(expr)
                       if isinstance(sub, ast.Name) and sub.id == role_param)
    for name, value, stmt in _named_bindings(fn):
        if name not in live:
            continue
        allowed.update(id(sub) for sub in ast.walk(value))
        target = (stmt.targets[0] if isinstance(stmt, ast.Assign)
                  else stmt.target)
        allowed.add(id(target))
    for sub in ast.walk(fn):
        if not isinstance(sub, ast.Name) or sub.id not in inputs \
                or id(sub) in allowed:
            continue
        raise AdmissionExtractionError(
            f"line {sub.lineno}: {what} mentions its own {sub.id!r} input in "
            f"a position this derivation does not read — "
            f"{ast.unparse(_statement_of(fn, sub)).splitlines()[0]!r}. A "
            f"statement that names an input and whose value nothing here "
            f"reads is kept for its EFFECT, and the only effect available "
            f"to it is CHANGING THE OBJECT {sub.id!r} names. The name at "
            f"the call site would still be {sub.id!r} and every pin would "
            f"still read exactly what it pinned, while the value the "
            f"resolver decides from is one the gate built rather than one "
            f"it was handed")


def _statement_of(fn, node):
    """The INNERMOST statement of ``fn`` that contains ``node`` — so a
    refusal quotes the line somebody wrote rather than the sub-expression it
    tripped on. Innermost is "the containing statement that starts latest",
    which on a tree of statements is the deepest one."""
    holders = [stmt for stmt in ast.walk(fn) if isinstance(stmt, ast.stmt)
               and any(sub is node for sub in ast.walk(stmt))]
    return max(holders, key=lambda s: (s.lineno, s.col_offset),
               default=node)


def _refuse_a_rebound_resolver_parameter(resolver, where):
    """CONDITION 7: THE RESOLVER'S OWN PARAMETERS MUST STILL HOLD WHAT IT WAS
    HANDED (#427 round 19, blocker 2 class B).

    WHAT THIS CLOSES. Condition 5 vouches what the CARRIER hands the
    RESOLVER. It says nothing about what the resolver then does to its own
    parameters before deciding, and the resolver's pinned answer for COACH is
    the TEXT ``scope.get('team_id')`` — text that a rebinding of ``scope``
    one line above leaves character for character unchanged, because
    :func:`_unfold` only unfolds a bare ``Name`` and that return is a call.
    MEASURED at ``8a6ed27``, two spellings, both at the top of
    ``game_scoped_own_team_id`` and neither touching the carrier at all::

        scope = {'team_id': game.home_team_id}
        scope = scope.fromkeys(scope, game.home_team_id)

    The second is THE REVIEW'S OWN EXPRESSION, moved one function over. Both
    left ``_audit()`` returning ``[]`` and both, compiled into the running
    server, gave ``thirdcoach`` 200 on all ten leaves of the private-game
    family on BOTH games on Memory, SQLite and real PostgreSQL — 60 of 60,
    with eight of HOME's private players in the ``gid`` ``/lineups`` body at
    ``restricted=false``.

    THE RULE IS CONDITION 5'S, ASKED WHERE THE RESOLVER LIVES. There is no
    call site here to vouch a value through, so the question is asked of the
    binding directly: a statement that binds one of the resolver's own
    parameter names must hold a value that IS that parameter —
    :func:`_is_the_carrier_input_itself`, the same allow-list of two forms,
    so ``scope = scope or {}`` (which the real resolver opens with, exactly
    as the carrier does) is admitted and everything that REBUILDS is
    refused. THE REAL RESOLVER NEEDS NO PRODUCTION CHANGE.

    WHY THIS IS NOT ALSO APPLIED TO THE CARRIER. It would be the same
    sentence twice: on the carrier a rebound parameter is already refused by
    condition 5, which follows the binding chain to the value the call
    actually hands over, and duplicating it there would leave condition 5
    with no falsifier of its own. The two rules are about two functions and
    they do not overlap.

    A separate function so it has its own seam: removing it must redden
    :meth:`EveryAdmissionBranchIsDerivedAndCarriesAnAuthority
    .test_a_resolver_that_rebuilds_its_own_parameter_is_refused`."""
    parameters = set(_positional_parameters(resolver, where))
    for name, value, stmt in _named_bindings(resolver):
        if name not in parameters or _is_the_carrier_input_itself(value, name):
            continue
        raise AdmissionExtractionError(
            f"line {stmt.lineno}: {resolver.name} REBUILDS its own {name!r} "
            f"parameter — {ast.unparse(stmt).splitlines()[0]!r} — which "
            f"is not that parameter and is not that parameter normalised "
            f"against an empty default. What the carrier hands this slot is "
            f"vouched at the call site, and by the time the answer is "
            f"decided the slot holds something this resolver BUILT instead. "
            f"Every pin reads the same text either way, because the pinned "
            f"answer names {name!r} rather than unfolding it")


# ---------------------------------------------------------------------------
# ROUND 20: THE MAPPING NO LONGER CROSSES THE BOUNDARY, so the question
# stops being undecidable (#427, the owner's blocker-2 ruling).
#
# WHAT EVERY EARLIER ROUND WAS DOING. Rounds 16, 18 and 19 each tried to
# INFER, from the source alone, whether the mutable session mapping had been
# tampered with between the carrier receiving it and the resolver deciding
# from it — a forged argument, then a rebuilt value, then an in-place
# mutation, then a resolver-side rebuild. Each was defeated one spelling
# later, and this round's own reproduction defeated the last of them twice:
#
#     official_id = (scope.update({'team_id': game.home_team_id})
#                    or scope.get("official_id"))       # THE RELOCATION
#
#     _alias = scope                                    # THE ALIAS
#     official_id = (_alias.update({'team_id': game.home_team_id})
#                    or _alias.get("official_id"))
#
# Both hoisted above the COACH/PLAYER branch with the OFFICIAL branch's own
# read replaced by `pass`. MEASURED at ``f9b094e``: ``_audit() == []`` for
# both, and over real authenticated HTTP TWENTY of the FIFTY derived
# unentitled cells answered 200, with EIGHT of HOME's private players in the
# ``gid`` ``/lineups`` body at ``restricted=false``, byte-identically on
# Memory, SQLite and real PostgreSQL. The behavioural oracles saw both; the
# static audit saw neither.
#
# WHY NO RULE OVER THE MAPPING COULD HAVE CLOSED THEM. "Does this call mutate
# shared state" is not a question an AST answers — ``scope.update`` does,
# ``scope.get`` does not, an ALIAS of ``scope`` is not spelled ``scope`` at
# all, and a name that is not a ``dict`` may do either. `_refuse_a_mutated_
# input` asks the only decidable version of it — "is this mention in a
# position the derivation reads" — and the relocation's mention IS read,
# because ``official_id`` genuinely decides the OFFICIAL branch. The residue
# was not small; it was the whole class.
#
# SO THE PRODUCTION BOUNDARY CHANGED AND THE QUESTION WENT AWAY. The carrier
# now PROJECTS the raw mapping ONCE, before any role branch, into explicit
# immutable scalar ids, and hands only those to the resolver. Between the
# projection and the decision there is no mutable object left, and what this
# file has to prove shrinks from an undecidable question to three syntactic
# ones — the three the owner's ruling names:
#
#   * SINGLE ASSIGNMENT      — already enforced; NO NEW RULE. See below.
#   * UNCHANGED PROPAGATION  `_refuse_an_unprojected_slot`      (condition 8)
#   * NO LATER RAW `scope`   `_refuse_a_raw_scope_use`          (condition 9)
#
# …and CONDITION 10, which is not in the ruling because nobody had found the
# escape it closes: `_refuse_a_carrier_the_product_does_not_call`. See its
# own docstring — the derivation picks the mapping by "whichever parameter
# the projections read", and a fifth REQUIRED parameter projected out of
# instead leaves the session's own scope policed by nothing.
#
#
# Each is decidable, each is derived from a signature or a product constant
# rather than from a list somebody kept, and each fails BY NAME.
#
# AND SINGLE ASSIGNMENT GOT NO RULE OF ITS OWN, WHICH IS A FINDING RATHER
# THAN AN OMISSION. One was written, and then removed for having no
# falsifier: every way a projected id can be written twice is ALREADY
# answered, and answered FIRST, by a rule that was here before —
#
#   * both writes in the same body — `_refuse_a_second_binding` (round 14),
#     which refuses re-binding a name still holding a live expression;
#   * the two writes separated by a role test — `_projections` itself, whose
#     "before any role branch" precondition refuses the lower one.
#
# The one shape that slips between those two (a nested block binding the
# name FIRST, so `_poison` marks it UNRESOLVABLE and the projection then
# writes over the marker legitimately) is not a disclosure at all: the
# projection is the write that wins. A rule whose only unique catch is a
# shape that cannot leak is dead machinery, and this file's standing rule is
# that a rule must have a seam of its own or not exist. The REQUIREMENT is
# still asserted — see
# :meth:`EveryAdmissionBranchIsDerivedAndCarriesAnAuthority
# .test_a_projected_id_cannot_be_assigned_twice`, which drives both shapes
# and names the rule that answers each.
# ---------------------------------------------------------------------------

#: The prefix a PROJECTED SCALAR's name carries — the one convention this
#: round introduces, and it is CHECKED IN BOTH DIRECTIONS rather than
#: trusted: :func:`_projections` requires every projection to be named
#: ``scoped_<the key it reads>``, and :func:`_refuse_an_unprojected_slot`
#: requires every resolver parameter spelled this way to be filled by the
#: projection of exactly that key. A projection named for one key and
#: reading another — ``scoped_team_id = scope.get('player_id')`` — is the
#: defect the convention exists to make visible, and it fails by name.
PROJECTION_PREFIX = "scoped_"

#: The scope keys THE PRODUCT ITSELF accepts, unioned over every role —
#: ``AccountService._ALLOWED_SCOPE_KEYS``, read rather than restated. A
#: projection of a key no account can ever carry is refused: it would be a
#: value the session cannot have supplied, dressed as one it did.
PRODUCT_SCOPE_KEYS = _scope_keys(*Role)


def _projection_of(stmt, carrier_params):
    """``(name, base, key)`` when ``stmt`` IS a projection, else ``None``.

    EXACTLY ONE SHAPE QUALIFIES, and every other expression Python has or
    will ship answers ``None`` — the fail-closed default the statement
    allow-list already establishes::

        <bare name> = <a carrier parameter>.get(<a string literal>)

    No keyword arguments, no default second argument, no chained call, no
    subscript. ``dict.get`` with one argument is a PURE READ that cannot
    raise and cannot mutate, which is what makes hoisting it above the role
    branches behaviour-preserving; ``.get(k, d)``, ``.pop(k)``,
    ``.setdefault(k, d)`` and ``[k]`` are each either impure or capable of
    raising where the old code did not, so none of them is this."""
    if not isinstance(stmt, ast.Assign) or len(stmt.targets) != 1:
        return None
    target = stmt.targets[0]
    value = stmt.value
    if not isinstance(target, ast.Name):
        return None
    if not (isinstance(value, ast.Call)
            and isinstance(value.func, ast.Attribute)
            and value.func.attr == "get"
            and isinstance(value.func.value, ast.Name)
            and value.func.value.id in carrier_params
            and not value.keywords
            and len(value.args) == 1
            and isinstance(value.args[0], ast.Constant)
            and isinstance(value.args[0].value, str)):
        return None
    return (target.id, value.func.value.id, value.args[0].value)


@functools.lru_cache(maxsize=1)
def _carrier_call_sites():
    """``(arities, "module:line" for each)`` for every call of the carrier
    in the package.

    CACHED, because :func:`_refuse_a_carrier_the_product_does_not_call` runs
    on every ``_audit()`` — sixty-four times in one test — and the product
    source does not change inside a run. The MUTATED copies the battery
    injects are never on disk, which is the whole point: the call sites this
    reads are the product's, and a mutation that widens the gate's signature
    cannot widen them too."""
    root = Path(inspect.getsourcefile(game_side_scope)).parent.parent
    arities, sites = set(), []
    for path in sorted(root.rglob("*.py")):
        source = path.read_text()
        if GATE_CARRIER not in source:
            continue
        for node in ast.walk(ast.parse(source)):
            if not (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name)
                    and node.func.id == GATE_CARRIER):
                continue
            rel = path.relative_to(root.parent).as_posix()
            if node.keywords or any(isinstance(a, ast.Starred)
                                    for a in node.args):
                raise AdmissionExtractionError(
                    f"{rel} line {node.lineno}: {GATE_CARRIER} is called as "
                    f"{ast.unparse(node)!r} — not its parameters as plain "
                    f"positional arguments. Which argument answers which "
                    f"parameter is then a matching rule this inventory does "
                    f"not model, so it cannot say the gate's signature is "
                    f"the one the product uses")
            arities.add(len(node.args))
            sites.append(f"{rel}:{node.lineno}")
    return frozenset(arities), tuple(sites)


def _refuse_a_carrier_the_product_does_not_call(carrier, where):
    """CONDITION 10: THE GATE'S SIGNATURE MUST BE THE ONE ITS CALLERS
    ACTUALLY USE (#427
    round 20) — and this rule exists because HUNTING THE ROUND'S OWN NEW
    RULES FOUND THE ESCAPE IT CLOSES.

    :func:`_projections` names no parameter: the raw mapping is "whichever
    carrier parameter the projections read". That is what keeps it derived
    rather than remembered, and on its own it is exploitable. MEASURED
    against this round's own first draft::

        def resolve_private_game_read(role, scope, game_id, store,
                                      override) -> PrivateGameRead:
            override = override or {}
            scoped_team_id = override.get("team_id")
            scoped_player_id = override.get("player_id")
            scoped_official_id = override.get("official_id")

    ``_audit()`` returned ``[]``. Every projection is well-formed, every key
    is one the product accepts, every name matches its key, nothing is
    written twice, and ``override`` is used nowhere but the projection — so
    conditions 8 and 9 are satisfied about ``override`` while the SESSION'S
    OWN ``scope`` is not mentioned at all and is therefore policed by
    nothing. The derivation had been persuaded that a different parameter
    was the mapping.

    ITS LIVENESS IS NOT CLAIMED, and that is the honest standing: the
    product's two call sites hand this function FOUR positional arguments,
    so the fifth parameter makes the call raise ``TypeError`` and the server
    answers 500 rather than a side. It is a COVERAGE escape — the audit
    would have blessed a gate deciding from a mapping nobody supplied — and
    it is refused for that reason, exactly as
    :func:`_refuse_a_binding_the_fold_would_drop` is.

    THE ANSWER IS THE PRODUCT'S OWN CALL SITES, not a parameter list kept
    here. Every call to the carrier anywhere in the package must hand it
    exactly the positional parameters its ``def`` declares; finding NO call
    site at all is refused for the same reason :func:`_carrier_reads`
    refuses it, because a signature nothing calls can agree with anything.

    A separate function so it has its own seam: removing it must redden
    :meth:`EveryAdmissionBranchIsDerivedAndCarriesAnAuthority
    .test_a_gate_signature_no_caller_uses_is_refused`."""
    parameters = _positional_parameters(carrier, where)
    root = Path(inspect.getsourcefile(game_side_scope)).parent.parent
    arities, sites = _carrier_call_sites()
    if not sites:
        raise AdmissionExtractionError(
            f"no module under {root} CALLS {GATE_CARRIER}, so its signature "
            f"agrees with anything and the parameter this derivation calls "
            f"the raw session mapping is whichever one the gate's own body "
            f"chose to read")
    if arities != frozenset({len(parameters)}):
        raise AdmissionExtractionError(
            f"{where}: {GATE_CARRIER} declares {len(parameters)} "
            f"parameter(s) {', '.join(repr(p) for p in parameters)}, and the "
            f"product calls it with {sorted(arities)} argument(s) at "
            f"{', '.join(sites)}. A parameter no caller supplies is one the "
            f"SESSION never fills — and the projection is derived from "
            f"whichever parameter the gate reads, so a gate that projects "
            f"out of an argument nobody passes would be audited about a "
            f"mapping that is not the caller's")


def _projections(carrier, where):
    """``(the raw mapping parameter, {projected name: (key, statement)})`` —
    THE PROJECTION, derived from the carrier's own body.

    NOT A LIST SOMEBODY KEPT. The mapping parameter is not named here: it is
    whichever carrier parameter the projections actually read, and finding
    two different ones is refused rather than picked between. The KEYS are
    checked against the product's own :data:`PRODUCT_SCOPE_KEYS`. The NAMES
    are checked against the keys through :data:`PROJECTION_PREFIX`.

    AND THE PROJECTION MUST BE FIRST, which is the half of the owner's
    ruling that the three refusal rules cannot express on their own: "project
    raw ``scope`` ONCE, BEFORE ANY ROLE BRANCH". A projection taken after a
    role test is a read of the mapping that some callers reach and others do
    not, and "the mapping is read once" would then be a statement about one
    path rather than about the function. So every projection must be a
    TOP-LEVEL statement of the carrier, and every one of them must come
    before the first top-level statement that mentions the role parameter.

    Finding NO projection at all is refused too: a derivation with an empty
    projection set would let conditions 8-10 agree with anything, which is
    exactly the vacuity :func:`_carrier_reads` refuses for the same reason."""
    carrier_params = _positional_parameters(carrier, where)
    role_param = _role_parameter(carrier)
    found, bases = {}, set()
    first_role = len(carrier.body)
    for index, stmt in enumerate(carrier.body):
        if any(isinstance(sub, ast.Name) and sub.id == role_param
               for sub in ast.walk(stmt)):
            first_role = min(first_role, index)
    for index, stmt in enumerate(carrier.body):
        projection = _projection_of(stmt, carrier_params)
        if projection is None:
            continue
        name, base, key = projection
        if index > first_role:
            raise AdmissionExtractionError(
                f"{where} line {stmt.lineno}: {carrier.name} projects "
                f"{name!r} out of {base!r} AFTER its first role test. The "
                f"whole of what this boundary now rests on is that the raw "
                f"mapping is read ONCE, BEFORE any role branch — a read "
                f"below one is a read only some callers reach, and 'the "
                f"mapping is read once' would be a statement about one path "
                f"rather than about {carrier.name}")
        if key not in PRODUCT_SCOPE_KEYS:
            raise AdmissionExtractionError(
                f"{where} line {stmt.lineno}: {carrier.name} projects the "
                f"scope key {key!r}, which is not one of "
                f"{sorted(PRODUCT_SCOPE_KEYS)} — the keys "
                f"AccountService._ALLOWED_SCOPE_KEYS says an account may "
                f"actually carry. A projection of a key no session can hold "
                f"is a value the caller never supplied wearing the shape of "
                f"one they did")
        if name != PROJECTION_PREFIX + key:
            raise AdmissionExtractionError(
                f"{where} line {stmt.lineno}: {carrier.name} binds "
                f"{name!r} to the scope key {key!r}. A projected id is named "
                f"{PROJECTION_PREFIX + key!r} — its own key — because the "
                f"NAME is what every downstream slot is matched on: a "
                f"projection called {name!r} and reading {key!r} would fill "
                f"the {name!r} slot of {GATE_RESOLVER} with somebody else's "
                f"identifier and every pin would read exactly what it pinned")
        found[name] = (key, stmt)
        bases.add(base)
    if not found:
        raise AdmissionExtractionError(
            f"{where} line {carrier.lineno}: {carrier.name} projects NOTHING "
            f"— no `<name> = <parameter>.get(<key>)` at its top level. "
            f"Conditions 8-10 are all statements ABOUT the projection, so "
            f"with no projection to speak of they would agree with anything, "
            f"and the mutable mapping this boundary was refactored to stop "
            f"carrying would be back across it unwatched")
    if len(bases) != 1:
        raise AdmissionExtractionError(
            f"{where} line {carrier.lineno}: {carrier.name} projects out of "
            f"{', '.join(repr(b) for b in sorted(bases))} — more than one "
            f"input. Which of them is THE raw session mapping, and therefore "
            f"which one condition 9 must find no other use of, is a "
            f"judgement this derivation will not make for itself")
    return bases.pop(), found


def _refuse_an_unprojected_slot(call, carrier, resolver, projected, where):
    """CONDITION 8: EVERY PROJECTED SLOT OF THE RESOLVER IS FILLED BY ITS OWN
    PROJECTION (#427 round 20, the UNCHANGED PROPAGATION half of the ruling).

    Conditions 1 and 2 already require every argument to be a bare ``Name``
    that IS the resolver's parameter name for its slot. That is what makes
    the slot unforgeable; it is not what makes it CORRECT. A resolver
    parameter spelled ``scoped_team_id`` must be filled by a projection that
    reads ``team_id`` — and the two halves of the convention are checked
    against each other rather than against a list::

        scoped_team_id = scope.get("player_id")   # refused in _projections
        game_scoped_own_team_id(role, scoped_player_id, …)   # refused here

    THE DIRECTION THAT MATTERS IS THIS ONE. A resolver that grows a
    ``scoped_*`` parameter the carrier does not project fails by name rather
    than being handed whatever local happens to carry that name — which is
    how a new scope-backed grant would otherwise arrive with nothing
    watching it.

    A separate function so it has its own seam: removing it must redden
    :meth:`EveryAdmissionBranchIsDerivedAndCarriesAnAuthority
    .test_a_resolver_slot_no_projection_fills_is_refused`."""
    resolver_params = _positional_parameters(resolver, where)
    for parameter, argument in zip(resolver_params, call.args):
        if not parameter.startswith(PROJECTION_PREFIX):
            continue
        key = parameter[len(PROJECTION_PREFIX):]
        if not isinstance(argument, ast.Name) \
                or argument.id not in projected \
                or projected[argument.id][0] != key:
            raise AdmissionExtractionError(
                f"line {call.lineno}: {carrier.name} fills "
                f"{resolver.name}'s {parameter!r} slot with "
                f"{ast.unparse(argument)!r}, which is not the projection of "
                f"the scope key {key!r} that slot is named for. This gate's "
                f"whole answer to blocker 2 is that the value crossing here "
                f"is the SESSION'S OWN id, projected once and propagated "
                f"unchanged; a slot filled by anything else is a side the "
                f"caller never sent, and every pin reads the same text either "
                f"way")


def _refuse_a_raw_scope_use(carrier, base, projected, where):
    """CONDITION 9: THE RAW MAPPING IS READ ONLY BY THE PROJECTION (#427
    round 20, the NO LATER RAW ``scope`` half of the ruling — and the one
    that closes the relocation, the alias and the live-``game`` binding
    together).

    After the projection there is nothing left for the carrier to want the
    mapping for, and every use of it is therefore a use this derivation has
    no account of. TWO POSITIONS ARE ALLOWED and every other occurrence of
    the name is REFUSED:

    * THE NORMALISATION — a binding of the mapping parameter to a value that
      IS that parameter, :func:`_is_the_carrier_input_itself`, the same
      two-form allow-list condition 5 already carries. ``scope = scope or
      {}`` is admitted; ``scope = scope.fromkeys(scope, …)`` is not.
    * THE BASE OF A PROJECTION — the ``scope`` in ``scope.get("team_id")``,
      and nothing else in that statement.

    WHAT THAT CLOSES, MEASURED. All three of this round's named regressions
    are this one refusal, and each was live at ``f9b094e``:

    * THE RELOCATION — ``official_id = (scope.update({…}) or
      scope.get("official_id"))`` hoisted above the COACH/PLAYER branch. The
      mention is in the value of a LIVE binding, so condition 6 admits it;
      here the ``scope`` in ``scope.update`` is in neither allowed position.
    * THE ALIAS — ``_alias = scope``, mutated through ``_alias``. Condition 6
      never asked, because ``_alias`` is not an input; here the ``scope`` on
      the right of the alias is in neither allowed position.
    * THE LIVE ``game`` BINDING — ``game = (scope.update({…}) or
      store.get_game(game_id))``, laundered through the one binding the
      carrier makes that the derivation certainly reads. Same answer, same
      rule, by name.

    THIS IS WHY THE PRODUCTION SHAPE HAD TO CHANGE. Every one of those is a
    question about what a CALL does to a shared object, and none of them is
    answerable from a source tree. Asking instead where a NAME may appear is
    answerable, and it is only sound because the mapping no longer has to
    survive the trip: what crosses into the resolver is three immutable
    scalars, and an alias of a string is the string.

    A separate function so it has its own seam: removing it must redden
    :meth:`EveryAdmissionBranchIsDerivedAndCarriesAnAuthority
    .test_a_raw_scope_use_after_the_projection_is_refused`."""
    allowed = set()
    for _key, stmt in projected.values():
        allowed.add(id(stmt.value.func.value))
    for stmt in carrier.body:
        if not isinstance(stmt, ast.Assign) or len(stmt.targets) != 1:
            continue
        target = stmt.targets[0]
        if not (isinstance(target, ast.Name) and target.id == base):
            continue
        if not _is_the_carrier_input_itself(stmt.value, base):
            continue
        allowed.update(id(sub) for sub in ast.walk(stmt)
                       if isinstance(sub, ast.Name) and sub.id == base)
    for sub in ast.walk(carrier):
        if not isinstance(sub, ast.Name) or sub.id != base \
                or id(sub) in allowed:
            continue
        raise AdmissionExtractionError(
            f"line {sub.lineno}: {carrier.name} uses the RAW session "
            f"mapping {base!r} outside its projection — "
            f"{ast.unparse(_statement_of(carrier, sub)).splitlines()[0]!r}. "
            f"The mapping is read ONCE, into "
            f"{', '.join(repr(n) for n in sorted(projected))}, and after "
            f"that there is nothing this gate can legitimately want it for. "
            f"Every use that is not that read is a use this derivation has "
            f"no account of — and a mutable object it has no account of is "
            f"exactly what five rounds of rules over this mapping could not "
            f"be made sound about")


def _poison(bindings, stmt):
    """Mark every name ``stmt`` assigns ANYWHERE inside it
    :data:`UNRESOLVABLE` for the rest of the enclosing body.

    THE JOIN THIS REPLACES IS THE ONE NOBODY WROTE. ``walk`` copies its
    bindings per body, so a nested block's assignments never reached the
    statements after it and the enclosing body went on reading the value
    from BEFORE the block. This does not compute what the block left behind
    — it records that the walk does not know, and :func:`_resolved` refuses
    anything that reads it."""
    for name in _assigned_names(stmt):
        bindings[name] = UNRESOLVABLE


def _decisions(fn, aliases, constants, resolver_name=None):
    """``[(Return, roles|None, resolver calls, bindings), …]`` for every
    return in ``fn``, carrying the role constraint of the enclosing branches.

    THE THIRD ELEMENT IS THE CALL NODES AND NOT A BOOLEAN (#427 round 16).
    "This branch delegates" was the whole of what a resolver call was read
    for, and the ARGUMENTS were therefore measured by nothing — see
    :func:`_refuse_an_unvouched_argument`, which is handed these. Truthiness
    is unchanged, so every reader that only asked whether the branch
    delegates still asks it.

    THE BINDING MODEL FAILS CLOSED IN THREE RULES (#427 round 14), because
    the round-13 walk was flow-insensitive in the UNSAFE direction and
    :func:`_resolved` documents the shape that got through:

    * a name any NESTED block assigns is :data:`UNRESOLVABLE` for the rest
      of the enclosing body (:func:`_poison`), so no read can resolve to the
      value it had before a block that might have changed it;
    * a name may be bound ONCE per body while it holds a live expression —
      re-assigning it is refused rather than resolved to either answer;
    * and A FUNCTION THAT ASSIGNS TO ITS OWN ROLE PARAMETER IS REFUSED. That
      is the ROLE-IDENTITY model, the third of the six this round closes: the
      walk reads the role's name off the signature (:func:`_role_parameter`)
      and then reads every role test as a statement about it, and
      ``role = Role.LEAGUE_ADMIN`` above a ``match role:`` made that reading
      false while every role on earth reached the LEAGUE_ADMIN arm. The
      information needed to refuse it was already here and unused.

    ROLES ARE OVER-APPROXIMATED, WHICH IS THE SAFE DIRECTION: an earlier
    branch's early return is not subtracted from a later one, so a role may
    be attributed to a branch it can never actually reach. That can only ADD
    branches demanding an authority; it can never hide one.

    THE STATEMENT WALK IS AN ALLOW-LIST, AND THAT IS THE WHOLE POINT (#427
    round 13). It used to be a DENY-LIST: ``For``/``While``/``With``/``Try``
    and friends raised, and any statement kind NOT named in that tuple fell
    off the end of the ``elif`` chain and was SILENTLY SKIPPED. ``ast.Match``
    was not in the tuple, so a ``match role: case Role.GUARDIAN:`` arm —
    ordinary Python since 3.10, and the most natural way anyone would
    refactor a seven-arm role gate — admitted a real caller to a real game
    with a real side while this function reported NOTHING and the audit
    returned no failures. Adding ``ast.Match`` to that tuple would have been
    the same defect one instance further along: Python keeps gaining
    statement kinds, and several already-existing ones were unlisted too.

    So the default is now REFUSAL. A statement kind this cannot attribute to
    a set of roles raises :class:`AdmissionExtractionError` NAMING ITS TYPE,
    which is the posture every other closed axis in this file already takes
    towards a member it has not seen — the route, principal, query-parameter
    and session-scope axes all fail closed on a new one. The allow-list was
    determined by WALKING THE REAL GATE, not by guessing: the two functions
    this is ever called on contain exactly ``Assign``, ``Expr``, ``If`` and
    ``Return``, and each kind below says why it is handled or why it is
    inert.

    ``ast.Match``'s fields (``subject``/``cases``, and ``match_case``'s
    ``pattern``/``guard``/``body``) are the PEP 634 grammar and are identical
    in 3.11 — the version CI runs — and 3.14, the version this was written
    on; both were checked rather than assumed."""
    out = []
    role_param = _role_parameter(fn)

    def walk(body, roles, delegates, bindings):
        bindings = dict(bindings)
        for stmt in body:
            # -- HANDLED: statement kinds that can carry a decision --------
            if isinstance(stmt, (ast.Assign, ast.AnnAssign)):
                # A binding `_unfold` may later have to see through, and the
                # one place the carrier's delegation to the resolver shows
                # up. `AnnAssign` is the SAME shape with a type on it
                # (`own_team: str | None = game_scoped_own_team_id(...)`), so
                # it is handled identically rather than left to be skipped.
                target = (stmt.targets[0]
                          if isinstance(stmt, ast.Assign)
                          and len(stmt.targets) == 1 else
                          stmt.target if isinstance(stmt, ast.AnnAssign)
                          else None)
                bound = _assigned_names(stmt)
                _refuse_a_rebound_role(fn, stmt, role_param, bound)
                _refuse_a_second_binding(fn, stmt, bindings, bound)
                if stmt.value is not None and resolver_name:
                    delegates = delegates + tuple(
                        _calls_to(stmt.value, resolver_name))
                if isinstance(target, ast.Name) and stmt.value is not None:
                    bindings[target.id] = stmt.value
                    bound.discard(target.id)
                # Anything else this statement binds — a tuple target, a
                # walrus in the value, a bare annotation — is a name whose
                # value this walk does not model, so it is marked rather
                # than left absent: absent reads as "not a local", which is
                # the permissive answer.
                for name in bound:
                    bindings[name] = UNRESOLVABLE
            elif isinstance(stmt, ast.If):
                narrowed = _resolve_roles(stmt.test, aliases, constants,
                                          role_param)
                inner = roles if narrowed is None else (
                    narrowed if roles is None else roles & narrowed)
                walk(stmt.body, inner, delegates, bindings)
                walk(stmt.orelse, roles, delegates, bindings)
                _poison(bindings, stmt)
            elif isinstance(stmt, ast.Match):
                # A `match role:` role gate, ATTRIBUTED — the same way `if`
                # is, and by the same rule: the SUBJECT must be the gate's
                # own role parameter for `case Role.X:` to be a statement
                # about the role at all, which is exactly the condition
                # `_resolve_roles` puts on the left of its comparison and for
                # exactly the same reason (a match on some OTHER name would
                # narrow the enclosing role set to the empty intersection and
                # record the arm under no role at all). When the subject is
                # something else, a pattern that still NAMES a Role is
                # refused rather than read, while one that names none leaves
                # the arm unconstrained and therefore attributed to everybody.
                subject_is_the_role = (isinstance(stmt.subject, ast.Name)
                                       and stmt.subject.id == role_param)
                for case in stmt.cases:
                    if case.guard is not None:
                        raise AdmissionExtractionError(
                            f"line {case.pattern.lineno}: the case guard "
                            f"{ast.unparse(case.guard)!r} in {fn.name} is "
                            f"part of what decides this arm, and this "
                            f"inventory would attribute the arm to "
                            f"{ast.unparse(case.pattern)!r} as though the "
                            f"guard were not there — which would report a "
                            f"real authority as an unconditional admission. "
                            f"Spell the guard as a nested `if`")
                    if subject_is_the_role:
                        narrowed = _pattern_roles(case.pattern, aliases)
                    elif _names_a_role(case.pattern, aliases):
                        raise AdmissionExtractionError(
                            f"line {case.pattern.lineno}: the case pattern "
                            f"{ast.unparse(case.pattern)!r} names a Role but "
                            f"the match subject is "
                            f"{ast.unparse(stmt.subject)!r}, not a bare "
                            f"name, so the roles this arm admits cannot be "
                            f"enumerated")
                    else:
                        narrowed = None
                    inner = roles if narrowed is None else (
                        narrowed if roles is None else roles & narrowed)
                    walk(case.body, inner, delegates, bindings)
                _poison(bindings, stmt)
            elif isinstance(stmt, ast.Return):
                # A SNAPSHOT, not the live dict (#427 round 14). `bindings`
                # is this body's own mutable map and the statements AFTER
                # this return go on writing to it — later assignments and
                # later poisoning alike — so handing the caller the object
                # describes the return by state that did not exist when it
                # was taken. It is wrong in both directions: a name bound
                # only AFTER the return unfolded into it (`admitted=ok` …
                # `ok = False` reported `admits=False`), and a later block's
                # poisoning refused a read that was perfectly resolvable
                # here.
                out.append((
                    stmt, roles,
                    delegates + tuple(_calls_to(stmt, resolver_name)
                                      if resolver_name else ()),
                    dict(bindings)))
            # -- INERT: skipped, with the proof on the line ----------------
            elif isinstance(stmt, ast.Pass):
                # `pass` is defined to do nothing. It binds no name, takes no
                # branch and produces no value, so no decision can hide in
                # it. (The `else_branch` injection below uses one.)
                continue
            elif isinstance(stmt, ast.Expr) \
                    and isinstance(stmt.value, ast.Constant):
                # A docstring, or any other bare literal: a constant
                # expression evaluated and discarded. Both walked functions
                # open with one. Note this is deliberately NARROWER than
                # `ast.Expr` — a NON-constant expression statement can
                # contain a walrus, which binds a name this walk does not
                # track, so it falls through to the refusal below.
                continue
            # -- EVERYTHING ELSE: refused, by name -------------------------
            else:
                raise AdmissionExtractionError(
                    f"line {stmt.lineno}: {type(stmt).__name__} in "
                    f"{fn.name} — {ast.unparse(stmt).splitlines()[0]!r} — is "
                    f"a statement kind this inventory cannot attribute to a "
                    f"set of roles. It is REFUSED rather than skipped: a "
                    f"statement nothing here reads is an admission nothing "
                    f"audits, which is exactly how a `match` arm went "
                    f"unseen. Either teach this walk to attribute it or "
                    f"spell the gate in a shape it already reads")
    walk(fn.body, None, (), {})
    return out


def _module_bindings_of(tree, name):
    """Every MODULE-LEVEL statement that binds ``name``, in source order."""
    out = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                             ast.ClassDef)) and node.name == name:
            out.append(node)
        elif isinstance(node, (ast.Import, ast.ImportFrom)) and any(
                (alias.asname or alias.name).split(".")[0] == name
                for alias in node.names):
            out.append(node)
        elif isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            targets = (node.targets if isinstance(node, ast.Assign)
                       else [node.target])
            if any(isinstance(sub, ast.Name) and sub.id == name
                   for target in targets for sub in ast.walk(target)):
                out.append(node)
    return out


def _gate_function(tree, name, where):
    """THE ONE module-level definition of ``name``.

    EXACTLY ONE, AND IT MUST BE A ``def`` (#427 round 14, the sixth model).
    This used to return the FIRST ``FunctionDef`` it found, and Python binds
    the LAST — so a module carrying two definitions of the carrier had its
    FIRST one audited while its SECOND one ran. MEASURED at ``c4a725b``:
    appending a second ``def resolve_private_game_read(...)`` that admits
    everybody with ``own_team=game.home_team_id`` left ``_audit()`` returning
    ``[]`` — the real definition above it parsed clean — while ``thirdcoach``,
    a coach of a team in NEITHER game, received 200 with the HOME side's
    private rows on ``/lineups``, ``/roster`` and ``/roster-status``.

    A duplicate ``def`` is what a bad merge looks like, and "a linter would
    have caught it" is not this axis's answer to anything: every other axis
    in this file fails closed on a shape it cannot read rather than
    delegating to a tool that may not run."""
    bindings = _module_bindings_of(tree, name)
    if not bindings:
        raise AdmissionExtractionError(
            f"{where}: no module-level function named {name!r}, so the gate "
            f"this axis is derived from is not where side_provenance says it "
            f"is")
    if len(bindings) > 1:
        raise AdmissionExtractionError(
            f"{where}: {name!r} is bound {len(bindings)} times at module "
            f"level (lines {', '.join(str(n.lineno) for n in bindings)}). "
            f"Python keeps the LAST and this inventory would read one of "
            f"them, so which definition the server actually calls is not "
            f"something the source says unambiguously")
    node = bindings[0]
    if not isinstance(node, ast.FunctionDef):
        raise AdmissionExtractionError(
            f"{where} line {node.lineno}: {name!r} is bound by a "
            f"{type(node).__name__}, not by a `def`, so the decisions it "
            f"takes are not in this module's source at all")
    return node


# ---------------------------------------------------------------------------
# FOLLOWING A CALL THE DECISION RESTS ON (#427 round 15, #205)
# ---------------------------------------------------------------------------
# A PIN THAT CANNOT SEE WHAT IT PROTECTS IS A PIN IN NAME ONLY. Until this
# round the OFFICIAL entry of :data:`ADMISSION_AUTHORITIES` was pinned on
#
#     official_id is not None and any(a.official_id == official_id
#                                     and a.status.is_active
#                                     for a in store.assignments_for_game(...))
#
# and `d62473a` on this branch exists BECAUSE `a.status.is_active` was
# missing: a DECLINED official kept 200 on the whole private-game family. The
# ledger could SEE that check, which is what made re-deciding the pin a real
# decision. `62a52b7` then centralised the predicate — the same question was
# being answered in three places and the two it did not touch went on granting
# a declined Official's Program, Season and League in the context switcher —
# so the branch now spells `assignment_grants_official_scope(a, official_id)`
# and the pin would have read a FUNCTION NAME. The one check the pin exists to
# protect would have become invisible to it, and a pin on a name cannot tell a
# predicate that still tests `status` from one that has stopped.
#
# So the derivation FOLLOWS THE CALL, the way it already follows the carrier's
# delegation to the resolver, and what is pinned is what the branch DECIDES BY
# rather than what it CALLS. The rules below are the same posture every other
# axis in this file takes: an allow-list, refusing by name anything it has not
# been taught to read, because a call this cannot follow is a decision nothing
# audits.
#
# ROUND 15'S FIRST DISCLOSED LIMIT IS CLOSED HERE (#427 round 16, d14). It
# read: "only the CARRIER's own decision expressions are inlined... the
# resolver's per-role answers are not inlined further. MEASURED reason:
# `_player_team_for_game`, the resolver's answer for PLAYER, decides through
# a LOCAL binding (`player = store.get_player(...)`) guarded by an early
# return, and `_substituted` refuses a decision that reads anything but its
# own parameters. Inlining it would refuse the REAL gate."
#
# THAT WAS A GAP, AND A LIVE ONE. The PLAYER authority was the TEXT
# `_player_team_for_game(scope, game, store)` and that function's body was
# derived by nothing: changing its `return` to `game.home_team_id` gave
# `awayplayer` HOME's whole private sheet over real authenticated HTTP with
# `_audit() == []`. So the fold was EXTENDED TO READ THAT SHAPE rather than
# the limit being restated. Two named changes, each a refusal when it does
# not hold: `_decided_by` folds a single bare-name assignment as the `let` it
# is, and `_substituted` keeps a free name that is bound at module level BY A
# DEFINITION in the SAME FILE as the call site, where the identical text
# means the identical object. `_resolver_authorities` then follows its own
# answers, and the PLAYER pin is the decision instead of the name.
#
# THE LIMIT THAT REMAINS, disclosed rather than discovered: the SEMANTIC half
# of a followed pin — that `status.is_active` is still what the followed
# predicate answers — is not carried by text at all. An expression can be
# rewritten while still testing status, and can keep its text while testing
# nothing. See :meth:`EveryAdmissionBranchIsDerivedAndCarriesAnAuthority
# .test_the_official_grant_answers_the_status_enum_member_by_member`. The
# PLAYER pin has no such driven half yet; what it has is that the two things
# the function NAME could not carry — the #270 `is_active` guard and the
# `RosterService.team_for_game` membership spine — are both IN it, and
# deleting either moves it (`test_the_player_authority_is_what_the_helper
# _decides_by`).

#: The product package a decision may be followed INTO, and the directory its
#: modules live in — READ OFF THE GATE MODULE ITSELF, so moving the package
#: follows automatically instead of being re-spelled here.
GATE_PACKAGE = game_side_scope.__name__.split(".")[0]
GATE_PACKAGE_DIR = Path(inspect.getsourcefile(game_side_scope)).resolve(
    ).parents[game_side_scope.__name__.count(".") - 1]

#: How many levels of followed call :func:`_inlined` will inline before
#: refusing. A BOUND, not a recursion guard: a predicate chain deeper than
#: this is a shape nobody has read, and the answer to that here is a refusal.
FOLLOW_DEPTH = 4


def _followed_module_source(node, name, where):
    """The FILE a module-level ``from … import name`` binds ``name`` from.

    RESOLVED ON THE FILESYSTEM, NEVER IMPORTED. This walk runs over MUTATED
    copies of the gate as well as over the real one, and importing whatever a
    mutated import line names would EXECUTE it — an analysis that runs the
    code it is analysing is not an analysis.

    Only the product package is followed. A stdlib or third-party callee is a
    decision taken outside the source this axis is derived from, so it is
    refused by name rather than pinned by its own."""
    package = ".".join(
        Path(where).resolve().relative_to(GATE_PACKAGE_DIR.parent)
        .with_suffix("").parts[:-1])
    anchor = package.split(".")
    if node.level:
        if node.level > len(anchor):
            raise AdmissionExtractionError(
                f"{where} line {node.lineno}: a decision calls {name!r}, "
                f"imported {node.level} levels above {package!r}, which is "
                f"past the top of the package — so there is no file this "
                f"walk can read the definition out of")
        anchor = anchor[:len(anchor) - node.level + 1]
        parts = ".".join(anchor + ([node.module] if node.module else [])
                         ).split(".")
    else:
        parts = (node.module or "").split(".")
    if parts[0] != GATE_PACKAGE:
        raise AdmissionExtractionError(
            f"{where} line {node.lineno}: a decision calls {name!r}, which "
            f"is imported from {'.'.join(parts)!r} — outside the product "
            f"package {GATE_PACKAGE!r}. What that call decides is not in "
            f"this codebase's source, so the branch is REFUSED rather than "
            f"booked under the name of a function nothing here can read")
    base = GATE_PACKAGE_DIR.joinpath(*parts[1:])
    for candidate in (base.with_suffix(".py"), base / "__init__.py"):
        if candidate.is_file():
            return candidate
    raise AdmissionExtractionError(
        f"{where} line {node.lineno}: {name!r} is imported from "
        f"{'.'.join(parts)!r}, which resolves to no file under "
        f"{GATE_PACKAGE_DIR} — so the definition this call actually runs is "
        f"not something this walk can read")


def _followed_definition(name, tree, where):
    """``(the ONE def a call to ``name`` runs, its module's tree, its file)``
    — or ``None`` when ``name`` is not bound at module level of ``where``.

    ``None`` is the answer for a BUILTIN (``any``), for a parameter and for a
    local: none of those is a decision this module's source states, and each
    stays in the pin as the text it already is. THE DISCRIMINATOR IS
    :func:`_module_bindings_of` AND NOT A LIST OF NAMES TO FOLLOW, which is
    what makes it closed: a module-level ``any = _something_else`` IS
    followed rather than skipped, because the question asked is "does this
    module's own source bind this name", not "is this name one of the ones
    somebody remembered".

    The ONE-DEFINITION model applies to the callee for exactly the reason it
    applies to the carrier — Python binds the LAST definition and this would
    read one of them — so :func:`_gate_function` is what answers, in the
    callee's own module.

    A CLASS IS NOT FOLLOWED, AND THAT IS A STATEMENT ABOUT WHAT A CALL
    DECIDES (#427 round 16). ``RosterService(store)`` constructs; what the
    surrounding expression decides by is the METHOD then called on it, which
    is an ``Attribute`` call and which this fold already declines to follow.
    Reading a ``ClassDef`` as "the decision a call to this name takes" would
    mean reading ``__init__``, which decides nothing about admission — so a
    name bound EXACTLY ONCE, to a class, keeps its call text, the same
    answer a builtin or a parameter gets. Bound any OTHER way it is still
    refused by :func:`_gate_function`: two bindings, or a binding that is
    neither a ``def`` nor a class, remain shapes this cannot read.

    THE LIMIT THAT COMES WITH IT IS THE ONE THE FOLD ALREADY HAD, not a new
    one: a decision taken inside a METHOD is pinned as the call TEXT, exactly
    as ``store.get_game(...)`` and ``a.status.is_active`` already are,
    because :class:`_Follow` only follows a call whose ``func`` is a bare
    ``Name``. Moving an admission decision into a class method would put it
    behind that text — and the SEMANTIC half of any pin is what
    ``test_the_official_grant_answers_the_status_enum_member_by_member`` is
    for."""
    bindings = _module_bindings_of(tree, name)
    if not bindings:
        return None
    if len(bindings) == 1 and isinstance(bindings[0], ast.ImportFrom):
        source = _followed_module_source(bindings[0], name, where)
        sub_tree, sub_where = ast.parse(source.read_text()), source
    else:
        sub_tree, sub_where = tree, where
    resolved = _module_bindings_of(sub_tree, name)
    if len(resolved) == 1 and isinstance(resolved[0], ast.ClassDef):
        return None
    return _gate_function(sub_tree, name, sub_where), sub_tree, sub_where


def _bound_forward(node, name, value):
    """``node`` with every read of ``name`` replaced by ``value`` — the `let`
    :func:`_decided_by` folds an assignment into.

    The pasted value is NOT re-visited (``NodeTransformer`` does not descend
    into what a visitor returns), so ``scope = scope or {}`` substitutes the
    PARAMETER's expression into later reads instead of expanding forever."""
    class _Let(ast.NodeTransformer):
        def visit_Name(self, sub):
            if sub.id != name or not isinstance(sub.ctx, ast.Load):
                return sub
            return copy.deepcopy(value)

    return ast.fix_missing_locations(_Let().visit(node))


def _refuse_a_binding_the_fold_would_drop(stmt, name, rest, fn, where):
    """A `let` WHOSE NAME IS NEVER READ IS NOT A `let` (#427 round 19, the
    eleventh spelling — one call deeper than the two the review named).

    :func:`_bound_forward` pastes a binding's value into every later read of
    its name. When there is no later read the value VANISHES: the fold's
    output, and therefore the pin, is exactly what it would have been had the
    statement not been there. MEASURED at ``8a6ed27`` and again against
    round 19's own conditions 6 and 7, injected into
    ``_player_team_for_game`` — the helper the PLAYER authority is folded
    through::

        _t = scope.update({'player_id': …})

    ``_audit()`` returned ``[]`` in both, and the PLAYER pin did not move by
    one character.

    ITS LIVENESS IS NOT CLAIMED, BECAUSE IT WAS NOT DEMONSTRATED. Driven
    tri-store with that exact expression compiled in, both player principals
    were answered ``home restricted=true, players=0`` (and 403 on Memory)
    rather than a side they do not hold: this fixture's players are MOVERS,
    so the first row of ``players_for_team(home)`` is somebody whose
    MEMBERSHIP is elsewhere and the forged ``player_id`` resolves to no side
    of this game. It is a COVERAGE escape — the derivation could not see the
    statement at all — and the statement is refused for that reason, not for
    a disclosure this round measured.

    THE ANSWER IS CONDITION 6'S SENTENCE, ASKED WHERE THE FOLD WORKS. A
    statement whose value nothing reads is kept for its EFFECT, and an effect
    is the one thing this fold cannot see. It is refused NAMING THE BINDING
    rather than dropped. THE REAL GATE NEEDS NO PRODUCTION CHANGE: every
    binding in every function this fold follows is read afterwards —
    ``_player_team_for_game``'s ``player_id`` twice and its ``player``
    twice.

    Why here and not in :func:`_refuse_a_mutated_input`: that rule is asked
    of the two functions this file NAMES, and a followed callee is neither.
    The fold is what reaches them, so the fold is where the question is put,
    and it is put at every depth :func:`_inlined` follows to.

    A separate function so it has its own seam: removing it must redden
    :meth:`EveryAdmissionBranchIsDerivedAndCarriesAnAuthority
    .test_a_binding_the_fold_would_drop_is_refused`."""
    if any(isinstance(sub, ast.Name) and sub.id == name
           for sub in ast.walk(rest)):
        return
    raise AdmissionExtractionError(
        f"{where} line {stmt.lineno}: {fn.name} binds "
        f"{ast.unparse(stmt).splitlines()[0]!r} and never reads {name!r} "
        f"again, so this fold would substitute the value into NOTHING and "
        f"what a call to {fn.name} decides by would read exactly as it does "
        f"without the statement. A binding whose name is never read is kept "
        f"for its EFFECT, and an effect is the one thing this fold cannot "
        f"see — including a mutation of the very argument the call site was "
        f"vouched for handing over")


def _decided_by(body, fn, where):
    """The ONE expression a straight-line ``body`` evaluates to.

    AN ALLOW-LIST, for the reason :func:`_decisions` is one: a statement kind
    this cannot fold into an expression is REFUSED NAMING ITS TYPE, never
    skipped, because a statement nothing reads is a decision nothing audits.

    * a docstring — a constant evaluated and discarded — is inert;
    * an ``if`` becomes the conditional expression it already is, with its
      ``orelse`` AND EVERYTHING AFTER IT as the else. That is what keeps a
      GUARD in the pin: ``if assignment is None: return False`` is part of
      what the predicate decides, and a fold that took only the last return
      would let the guard be deleted without moving the pin;
    * a ``return`` is the value, and it must be the LAST statement of its
      body — anything after it is unreachable and would be silently unread;
    * A BODY THAT CAN FALL THROUGH IS REFUSED. Its value is not the value of
      anything this fold can name: an ``if`` whose body falls through
      continues into the statements AFTER the ``if``, so reading it as the
      then-arm of a conditional expression would be simply false.
    * AN ASSIGNMENT TO ONE BARE NAME is folded by SUBSTITUTING ITS VALUE
      FORWARD into everything after it (#427 round 16). This is the shape
      round 15 disclosed it could not read and therefore did not close:
      "``_player_team_for_game`` decides through a local binding behind an
      early return". Reading it is not a modelling job — a single assignment
      in straight-line code is a `let`, and pasting the value where the name
      is read is what a `let` MEANS. Note the value is pasted WITHOUT being
      re-visited, so ``scope = scope or {}`` substitutes the parameter's own
      expression rather than recurring on itself; and ONE binding per name
      is what round 14's :func:`_refuse_a_second_binding` already requires of
      the walk that reads the gate. A target that is not exactly one bare
      ``Name`` — a tuple unpack, an attribute, a subscript, a walrus — is
      REFUSED, because which name holds what is then a rule this does not
      model.

    Everything else — a loop, a ``with``, a ``match`` — is refused. Each of
    them is a modelling job, and the whole of round 14 is what happens when
    one of those is done by hand and quietly."""
    for index, stmt in enumerate(body):
        if isinstance(stmt, ast.Pass) or (
                isinstance(stmt, ast.Expr)
                and isinstance(stmt.value, ast.Constant)):
            # `pass` and a docstring alike bind nothing, decide nothing and
            # produce no value — inert for the same reason `_decisions` calls
            # them inert. A body made only of these still FALLS THROUGH, and
            # that is refused below rather than read as a value.
            continue
        if isinstance(stmt, ast.Return):
            if index != len(body) - 1:
                raise AdmissionExtractionError(
                    f"{where} line {stmt.lineno}: {fn.name} has "
                    f"{len(body) - index - 1} statement(s) after this "
                    f"`return`, which this fold would never read")
            return (stmt.value if stmt.value is not None
                    else ast.Constant(value=None))
        if isinstance(stmt, ast.If):
            return ast.IfExp(
                test=stmt.test,
                body=_decided_by(stmt.body, fn, where),
                orelse=_decided_by(list(stmt.orelse) + list(body[index + 1:]),
                                   fn, where))
        if isinstance(stmt, (ast.Assign, ast.AnnAssign)):
            targets = (stmt.targets if isinstance(stmt, ast.Assign)
                       else [stmt.target])
            if len(targets) != 1 or not isinstance(targets[0], ast.Name) \
                    or stmt.value is None:
                raise AdmissionExtractionError(
                    f"{where} line {stmt.lineno}: {fn.name} binds "
                    f"{ast.unparse(stmt).splitlines()[0]!r}, which is not a "
                    f"single bare name holding a value — which name holds "
                    f"what afterwards is a rule this fold does not model, so "
                    f"what a call to {fn.name} decides by cannot be written "
                    f"out")
            rest = _decided_by(body[index + 1:], fn, where)
            _refuse_a_binding_the_fold_would_drop(stmt, targets[0].id, rest,
                                                  fn, where)
            return _bound_forward(rest, targets[0].id, stmt.value)
        raise AdmissionExtractionError(
            f"{where} line {stmt.lineno}: {type(stmt).__name__} in "
            f"{fn.name} — {ast.unparse(stmt).splitlines()[0]!r} — is a "
            f"statement kind this walk cannot fold into the expression a "
            f"call to {fn.name} decides by. A branch whose admission rests "
            f"on a call nothing here can read is REFUSED rather than booked "
            f"under that call's own name")
    raise AdmissionExtractionError(
        f"{where} line {fn.lineno}: a body of {fn.name} reaches its end "
        f"without deciding anything, so what a call to it evaluates to "
        f"depends on the statements AFTER the block it is in — which this "
        f"fold does not model and will not guess")


def _positional_parameters(fn, where):
    """``fn``'s parameter NAMES, in order — and a REFUSAL when its signature
    is not one this file may read argument by argument.

    THE ONE PLACE THAT DECIDES THAT, for the reason round 15 gave when
    :func:`_substituted` was the only place: which argument answers which
    parameter is a matching rule, ``*args``, ``**kwargs``, a keyword-only
    parameter and a default each change it, and a rule modelled in two
    places is a rule that can be right in one of them. Both the FOLD (which
    pastes a callee's decision into its caller) and the ARGUMENT PROVENANCE
    rule (#427 round 16, which reads what a delegating branch hands the
    resolver) ask this same question, so they ask it here.

    EVERY FIELD of the running interpreter's ``ast.arguments`` is read, and
    :meth:`EveryAdmissionBranchIsDerivedAndCarriesAnAuthority
    .test_the_signature_fields_the_substitution_reads_are_this_grammar` is
    what requires that of THIS function's own source: CI runs 3.11 and this
    was written on 3.14, and a field that is not looked at is an
    argument-matching rule silently not modelled."""
    args = fn.args
    if (args.vararg or args.kwarg or args.kwonlyargs or args.defaults
            or args.kw_defaults):
        raise AdmissionExtractionError(
            f"{where} line {fn.lineno}: {fn.name} takes *args, **kwargs, a "
            f"keyword-only parameter or a default, so which argument answers "
            f"which parameter is a matching rule this walk does not model")
    return [arg.arg for arg in list(args.posonlyargs) + list(args.args)]


def _module_level_definitions(tree):
    """Names bound at MODULE level of ``tree`` BY A DEFINITION — a ``def``, a
    ``class`` or an ``import``.

    Two conditions, and the second is what keeps this from being a hole.
    A name here means the SAME OBJECT at every point of its module, which is
    why :func:`_substituted` may leave one standing when the callee and the
    call site are the same file. And it names a DEFINITION rather than a
    VALUE: a module-level ASSIGNMENT is deliberately excluded, because what
    it holds is part of what the callee decides, and letting its NAME stand
    in the pin would put back exactly the defect round 15 removed — a pin
    that reads a name instead of the decision behind it. ``_ALWAYS = True``
    in a followed predicate is still refused by name, and
    :meth:`EveryAdmissionBranchIsDerivedAndCarriesAnAuthority
    .test_a_call_the_pin_cannot_follow_is_refused_by_name` is what requires
    that."""
    out = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                             ast.ClassDef)):
            out.add(node.name)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            out.update((alias.asname or alias.name).split(".")[0]
                       for alias in node.names)
    return frozenset(out)


def _substituted(node, fn, call, where, in_scope=frozenset()):
    """``node`` — what ``fn`` decides by — with each PARAMETER replaced by
    the ARGUMENT this call passes for it.

    NARROW ON PURPOSE. Only a plain positional call onto a plain positional
    signature is read: ``*args``, ``**kwargs``, keyword-only parameters,
    defaults, a starred argument and a keyword argument are each refused by
    name, because each is an argument-matching rule this would otherwise
    have to model and could be wrong about.

    AND EVERY FREE NAME IN THE DECISION MUST BE A PARAMETER — OR A NAME THAT
    MEANS THE SAME THING AT THE CALL SITE. A decision that reads its own
    module's globals, or its own locals, means something in the CALLEE's
    namespace that the identical text would not mean at the call site;
    pasting it would produce a pin that reads plausibly and describes
    nothing, so it is refused.

    ``in_scope`` IS THAT EXCEPTION, AND IT IS AN IDENTITY ARGUMENT RATHER
    THAN A LOOSENING (#427 round 16). When the callee is defined in the SAME
    FILE as the call site, a name bound at that module's top level BY A
    DEFINITION names the same object in both places — the text does not
    change meaning, so there is nothing to be wrong about. :func:`_inlined`
    passes :func:`_module_level_definitions` of the caller's own module ONLY
    when the two files are the same one, and an empty set otherwise, so a
    callee in another module is refused exactly as before, and a module-level
    ASSIGNMENT is refused in both cases. This is what lets the PLAYER
    authority be folded at all: ``_player_team_for_game`` decides through
    ``RosterService(store).team_for_game(...)``, and ``RosterService`` is a
    module-level import of the gate's own file."""
    params = _positional_parameters(fn, where)
    if call.keywords or any(isinstance(a, ast.Starred) for a in call.args) \
            or len(call.args) != len(params):
        raise AdmissionExtractionError(
            f"{where}: the call {ast.unparse(call)!r} does not hand "
            f"{fn.name} exactly its {len(params)} positional parameter(s) as "
            f"plain positional arguments, so what the callee decides about "
            f"cannot be re-stated in the caller's own names")
    outside = sorted({sub.id for sub in ast.walk(node)
                      if isinstance(sub, ast.Name)}
                     - set(params) - set(in_scope))
    if outside:
        raise AdmissionExtractionError(
            f"{where} line {fn.lineno}: what {fn.name} decides by reads "
            f"{', '.join(repr(n) for n in outside)}, which is not one of its "
            f"parameters — a global or a local of its own module. The same "
            f"text at the call site would name something else or nothing, so "
            f"it is refused rather than pasted")
    mapping = dict(zip(params, call.args))

    class _Bind(ast.NodeTransformer):
        def visit_Name(self, sub):
            bound = mapping.get(sub.id)
            return sub if bound is None else copy.deepcopy(bound)

    return ast.fix_missing_locations(_Bind().visit(node))


def _inlined(node, tree, where, depth=FOLLOW_DEPTH):
    """``node`` with every call this walk CAN follow replaced by what the
    called function decides by, so the pin reads the DECISION and not the
    name of whatever takes it."""
    if depth <= 0:
        raise AdmissionExtractionError(
            f"{where}: following what this decision rests on ran past "
            f"{FOLLOW_DEPTH} levels of call. A chain that deep is a shape "
            f"nobody has read, and this axis refuses those rather than "
            f"stopping partway and reporting the part it managed")

    class _Follow(ast.NodeTransformer):
        def visit_Call(self, call):
            self.generic_visit(call)
            if not isinstance(call.func, ast.Name):
                return call
            found = _followed_definition(call.func.id, tree, where)
            if found is None:
                return call
            fn, fn_tree, fn_where = found
            # The callee's own module-level names, and ONLY when that module
            # is this one: then the text means the same object here as
            # there, which is the identity argument `_substituted` documents.
            here = (_module_level_definitions(tree)
                    if Path(fn_where) == Path(where) else frozenset())
            return _substituted(
                _inlined(_decided_by(fn.body, fn, fn_where),
                         fn_tree, fn_where, depth - 1),
                fn, call, fn_where, here)

    return ast.fix_missing_locations(_Follow().visit(copy.deepcopy(node)))


def _resolver_authorities(tree, aliases, constants, where):
    """``{role: what the RESOLVER answers it with, FOLLOWED}`` — how the
    carrier's single ``role in (Role.COACH, Role.PLAYER)`` branch becomes TWO
    authorization branches.

    A role the resolver answers with a literal ``None`` resolves no side and
    therefore reaches no admission, so it does not appear.

    THE ANSWER IS FOLLOWED, NOT NAMED (#427 round 16, and this is what
    closes d14). The PLAYER authority used to be pinned as the TEXT
    ``_player_team_for_game(scope, game, store)`` and that function's BODY
    was derived by nothing: changing its ``return`` to ``game.home_team_id``
    gave ``awayplayer`` HOME's whole private sheet with ``_audit() == []``.
    Round 15 disclosed exactly this gap and its reason — the helper "decides
    through a local binding behind an early return, and ``_substituted``
    refuses that" — so the fold was extended to READ THAT SHAPE rather than
    the pin being moved somewhere else: :func:`_decided_by` now folds a
    single bare-name assignment as the `let` it is, and :func:`_substituted`
    keeps a free name that means the same object at the call site because
    the callee is in the same file. Both are named refusals when they do not
    hold, so the PLAYER authority is a decision this file can see rather
    than a function name it cannot."""
    fn = _gate_function(tree, GATE_RESOLVER, where)
    out = {}
    for node, roles, _delegates, bindings in _decisions(fn, aliases,
                                                        constants):
        value = (_resolved(node.value, bindings,
                           f"{GATE_RESOLVER}'s return")
                 if node.value else None)
        if value is None or (isinstance(value, ast.Constant)
                             and value.value is None):
            continue
        if roles is None:
            raise AdmissionExtractionError(
                f"line {node.lineno}: {GATE_RESOLVER} resolves a side for "
                f"EVERY role — no role test governs this return — so no "
                f"caller's side can be attributed to an authority")
        # FOLLOWED after the `None` test and not before: inlining can only
        # turn an expression into a longer one, and "this role gets no side"
        # is a fact about the literal the resolver returns.
        answers = _inlined(value, tree, where)
        for role in roles:
            out[role] = ast.unparse(answers)
    return out


def admission_branches(source=None):
    """``{role name: (every AdmissionBranch the gate takes for it, …)}``,
    DERIVED from the gate's own source.

    ``source`` follows ``route_extract``'s convention: ``None`` means "the
    real module", and a string is a mutated copy — which is what
    :class:`EveryAdmissionBranchIsDerivedAndCarriesAnAuthority` injects new
    branches into to prove the derivation is real rather than decorative."""
    where = Path(inspect.getsourcefile(game_side_scope))
    tree = ast.parse(source if source is not None else where.read_text())
    aliases = _role_aliases(tree)
    if not aliases:
        raise AdmissionExtractionError(
            f"{where}: `Role` is not imported under any name this inventory "
            f"recognises, so either this gate no longer decides admission by "
            f"role or it spells the roles in a way this cannot read")
    constants = _module_constants(tree)
    resolver = _resolver_authorities(tree, aliases, constants, where)
    resolver_fn = _gate_function(tree, GATE_RESOLVER, where)
    carrier = _gate_function(tree, GATE_CARRIER, where)
    # THE NAME IS NOT THE VALUE, ASKED OF BOTH FUNCTIONS (#427 round 19,
    # blocker 2). Conditions 1-5 vouch what the carrier HANDS the resolver;
    # neither asks whether the OBJECT that argument names was changed first
    # (`_refuse_a_mutated_input`), and none follows the value past the call
    # into what the resolver does to its OWN parameters
    # (`_refuse_a_rebound_resolver_parameter`). Seven spellings measured at
    # `8a6ed27` produced a live 60-of-60 admission with `_audit() == []`
    # through exactly those two gaps.
    #
    # AFTER THE WALK, NOT BEFORE IT, AND THAT ORDER IS LOAD-BEARING. The
    # statement allow-list is the PRIMARY guard and its refusal has to NAME
    # THE STATEMENT KIND the gate grew (`ast.Assert`, `ast.AugAssign`, …);
    # `_poison` and `_refuse_a_second_binding` likewise own the spellings
    # their falsifiers pin. Running the two new rules first took those
    # answers away from them and reported a mutation instead, which is a
    # worse message for the same defect and left three earlier falsifiers
    # measuring nothing.
    decisions = _decisions(carrier, aliases, constants, GATE_RESOLVER)
    _refuse_a_mutated_input(carrier, where, GATE_CARRIER)
    _refuse_a_mutated_input(resolver_fn, where, GATE_RESOLVER)
    _refuse_a_rebound_resolver_parameter(resolver_fn, where)
    # ROUND 20: THE PROJECTION. Derived before the branch walk for the same
    # reason the two rules above run after it — each rule owns the spellings
    # its own falsifier pins, and conditions 8-10 are statements about the
    # carrier's SHAPE rather than about any one branch. The derivation
    # itself refuses a carrier that projects nothing, projects out of two
    # inputs, projects a key the product does not accept, names a projection
    # for a key it does not read, or takes one below a role branch.
    _refuse_a_carrier_the_product_does_not_call(carrier, where)
    scope_param, projected = _projections(carrier, where)
    _refuse_a_raw_scope_use(carrier, scope_param, projected, where)
    record = game_side_scope.PrivateGameRead.__name__
    out = {}
    for node, roles, delegated_calls, bindings in decisions:
        # WHAT THIS BRANCH HANDED THE RESOLVER, before anything else about
        # the branch is read — a forged argument is not a mis-described
        # branch, it is a branch whose description is about somebody else.
        for delegation in delegated_calls:
            _refuse_an_unvouched_argument(delegation, carrier, resolver_fn,
                                          bindings, where, projected)
            _refuse_an_unprojected_slot(delegation, carrier, resolver_fn,
                                        projected, where)
        delegates = bool(delegated_calls)
        call = node.value
        if not (isinstance(call, ast.Call) and isinstance(call.func, ast.Name)
                and call.func.id == record):
            raise AdmissionExtractionError(
                f"line {node.lineno}: {GATE_CARRIER} returns "
                f"{(ast.unparse(call) if call else 'None')!r}, which is not "
                f"a `{record}(...)` this inventory can classify. An "
                f"admission decided somewhere this cannot see is an "
                f"admission nothing audits")
        keywords = {kw.arg: kw.value for kw in call.keywords}
        if frozenset(keywords) != ADMISSION_FIELDS:
            raise AdmissionExtractionError(
                f"line {node.lineno}: the decision names {sorted(keywords)} "
                f"and `{record}` declares {sorted(ADMISSION_FIELDS)}; a "
                f"positional or defaulted field is a decision this inventory "
                f"would read wrong")
        where_read = f"the decision at line {node.lineno} of {GATE_CARRIER}"
        admitted = _resolved(keywords["admitted"], bindings, where_read)
        game = _resolved(keywords["game"], bindings, where_read)
        own_team = _resolved(keywords["own_team"], bindings, where_read)
        admits = not (isinstance(admitted, ast.Constant)
                      and admitted.value is False)
        carries_game = not (isinstance(game, ast.Constant)
                            and game.value is None)
        # THE OTHER HALF OF THE GRANT, and the one the record's own docstring
        # talked this inventory out of measuring — see `needs_authority`.
        grants_side = not (isinstance(own_team, ast.Constant)
                           and own_team.value is None)
        # WHAT THIS BRANCH DECIDES BY, not what it CALLS (#427 round 15).
        # The three booleans above are read off the RAW resolved keywords and
        # deliberately not off these: inlining can only turn an expression
        # into a literal, and a branch whose `admitted=` folded down to
        # `False` would stop needing an authority — the permissive direction.
        # The PINS are read off these, because a pin that stops at a function
        # name cannot see the check it exists to protect.
        decides_admission = _inlined(admitted, tree, where)
        decides_side = _inlined(own_team, tree, where)
        # The names this branch bound to the RESOLVER'S answer. A branch may
        # only be excused on the ground that the resolver decides it if what
        # it returns actually RESTS ON that answer — see below.
        delegated = {name for name, value in bindings.items()
                     if value is not UNRESOLVABLE
                     and _calls(value, GATE_RESOLVER)}
        for role in sorted(roles if roles is not None else Role.__members__):
            authority = None
            if delegates and role in resolver:
                authority = resolver[role]
            elif delegates and _rests_on(admitted, delegated) \
                    and _rests_on(own_team, delegated):
                # The carrier hands this role to the resolver, the resolver
                # answers it with nothing, and BOTH the admission and the
                # side this branch returns are functions of that answer: no
                # side, so nothing this branch can admit them to.
                #
                # THE SECOND HALF OF THAT SENTENCE USED TO BE MISSING (#427
                # round 14, the fifth model). The skip was taken on the
                # strength of the ASSIGNMENT alone, so a branch could call
                # the resolver, IGNORE what it said, and admit the role
                # outright — and because the role has no resolver entry the
                # branch was not merely mis-attributed, it was DROPPED and
                # never audited at all. MEASURED at `c4a725b`: a new
                # `if role == Role.COACH:` branch that calls the resolver and
                # returns `own_team=game.home_team_id, admitted=True` gave
                # `thirdcoach` 200 with HOME's private rows on `/lineups`,
                # `/roster` and `/roster-status`; spelled for a role with no
                # ADMISSION_AUTHORITIES entry it produced `_audit() == []`.
                continue
            if authority is None:
                authority = ast.unparse(decides_admission)
            out.setdefault(role, []).append(AdmissionBranch(
                role=role, lineno=node.lineno, admits=admits,
                carries_game=carries_game, authority=authority,
                grants_side=grants_side,
                # WHAT THIS BRANCH ITSELF DOES, recorded separately from
                # what the RESOLVER answers (#427 round 14). `authority` is
                # the resolver's expression whenever the branch delegates,
                # and a branch that delegates and then ignores the answer was
                # booked under the resolver's authority and reported green —
                # see `ADMISSION_AUTHORITIES`. Both are what the branch
                # DECIDES BY — every call this walk can follow already
                # replaced by what the called function decides (#427 round
                # 15), so a pin cannot be satisfied by a function name.
                admits_source=ast.unparse(decides_admission),
                side_source=ast.unparse(decides_side)))
    return {role: tuple(branches) for role, branches in sorted(out.items())}

#: The roles whose authority over a game is LEAGUE-WIDE rather than earned by
#: participating in it — the only branches of the gate that may be admitted
#: with no authority row behind them, and DERIVED from the product's own
#: permission table.
#:
#: ``MANAGE_SCHEDULE`` IS THE DISCRIMINATOR, and it is the product's own
#: sentence: "create / move / publish games; assign officials". A role that
#: may CREATE and MOVE the game is an operator OF the competition; every role
#: that merely takes part in one — Coach, Player, Guardian, Official, Viewer —
#: holds none of it. Measured on this tree that is exactly
#: ``LEAGUE_ADMIN`` and ``ARENA_MANAGER``, which is also exactly the tuple
#: the product's own ``lineup_visibility._UNSCOPED_OPERATORS`` names and
#: exactly the tuple the gate short-circuits — three independent product
#: statements, and
#: :meth:`EveryAdmissionBranchIsDerivedAndCarriesAnAuthority
#: .test_the_operator_exemption_is_the_products_own_answer_three_times`
#: requires all three to agree, so this exemption cannot be widened in one
#: place.
OPERATOR_PERMISSION = Permission.MANAGE_SCHEDULE
OPERATOR_ROLES = frozenset(
    role.name for role in Role
    if OPERATOR_PERMISSION in ROLE_PERMISSIONS.get(role, frozenset()))


@dataclasses.dataclass(frozen=True)
class _DeclaredAuthority:
    """What answers for ONE role's admission branch, and the three pieces of
    normalized source that judgement was made about."""

    klass: str        #: the entitlement class this sweep models the grant as
    authority: str    #: what RESOLVES the side (the resolver, when delegated)
    admits: str       #: what THE BRANCH ITSELF tests before admitting
    side: str         #: what side THE BRANCH ITSELF returns


#: ``{Role member name: the :class:`_DeclaredAuthority` that answers for it}``
#: — the AUTHORITY MAPPING every non-operator admission branch must have.
#:
#: THIS IS NOT THE ENUMERATION; :func:`admission_branches` IS. The set of
#: branches is derived from the gate, and this map only has to ANSWER for the
#: ones the derivation finds. A branch the gate gains and this map does not
#: name is an error naming the role and the line — which is the property
#: :data:`GRANT_ROWS` did not have, and the whole of LB1.
#:
#: EVERY PIN IS EXACT NORMALIZED AST TEXT, the rule
#: ``route_extract._AUDIT_WAIVERS`` already carries: the classification is a
#: judgement about a SPECIFIC expression, so a branch that starts resolving
#: its side some other way has to be RE-DECIDED rather than silently
#: inheriting a mapping made for the old one.
#:
#: WHY THERE ARE THREE PINS AND NOT ONE (#427 round 14). ``authority`` alone
#: was the whole judgement, and for a DELEGATING branch it is the RESOLVER'S
#: expression — a statement about what ``game_scoped_own_team_id`` answers,
#: not about what the branch then DOES with the answer. So the branch could
#: resolve the caller's side through the audited resolver and return a
#: DIFFERENT one, and still be booked under the resolver's authority.
#: MEASURED at ``c4a725b``, changing only the COACH/PLAYER branch::
#:
#:     own_team = game_scoped_own_team_id(role, scope, game, store)
#:     admitted = own_team is not None
#:     return PrivateGameRead(role=role, game=game,
#:                            own_team=game.home_team_id, admitted=admitted)
#:
#: ``thirdcoach`` — a coach of a team in NEITHER game — received 200 with
#: HOME's private rows on ``/lineups``, ``/board``, ``/roster``,
#: ``/roster-status`` AND ``/substitutes``, and ``_audit()`` returned ``[]``.
#: ``admits`` and ``side`` pin what the branch itself does, so both halves of
#: that mutation now fail by name.
ADMISSION_AUTHORITIES = {
    # A Coach's side is the account scope, read without touching the store —
    # so the grant is the SCOPE, and the class is scope-backed.
    #
    # RE-PINNED IN ROUND 20, AND THE MOVE IS THE REFACTOR (#427, the owner's
    # blocker-2 ruling). The pin was `scope.get('team_id')` — the resolver
    # reading the mapping for itself. The mapping no longer crosses the
    # boundary, so what the resolver answers a Coach with is now the
    # PROJECTED ID the carrier handed it, and the pin is the name of that id.
    #
    # THE PIN ALONE IS WEAKER THAN IT WAS, AND THAT IS SAID RATHER THAN
    # GLOSSED: `scoped_team_id` does not, by itself, say WHICH scope key the
    # Coach's side comes from, and the old text did. The information did not
    # vanish, it MOVED INTO A RULE — `_projections` requires the projection
    # named `scoped_team_id` to read exactly the key `team_id` (and requires
    # that key to be one `AccountService._ALLOWED_SCOPE_KEYS` accepts), and
    # `_refuse_an_unprojected_slot` requires the resolver's `scoped_team_id`
    # slot to be filled by exactly that projection. Both fail BY NAME. So the
    # pair says what the one string used to, and says it somewhere a mutation
    # cannot leave character-for-character unchanged: `scoped_team_id =
    # scope.get('player_id')` moves nothing in this pin and is refused by the
    # first rule, while `return scoped_player_id` for COACH moves this pin.
    "COACH": _DeclaredAuthority(
        klass=COACH_SCOPED_TO_ONE_SIDE,
        authority="scoped_team_id",
        admits="own_team is not None and own_team in "
               "(game.home_team_id, game.away_team_id)",
        side="own_team if admitted else None"),
    # A Player's side is resolved live, through the membership spine, which
    # is why this class is ROW-backed and the Coach's is not. This one line
    # is the whole of LB1: the gate has always split here, and this file
    # modelled the two halves as one class.
    #
    # RE-PINNED IN ROUND 16, AND THIS IS WHAT CLOSES d14. The pin was the
    # TEXT `_player_team_for_game(scope, game, store)` and that function's
    # BODY was derived by nothing at all: changing its `return` to
    # `game.home_team_id` gave `awayplayer` HOME's whole private sheet —
    # `/lineups` home `restricted false` with eight rows, `roster-status`
    # `team_id team_1`, against a baseline of AWAY's five — with `_audit()`
    # returning `[]`. Round 15 DISCLOSED that gap and its reason: the helper
    # "decides through a local binding behind an early return, and
    # `_substituted` refuses that". The answer taken here is the first of the
    # three the owner offered — EXTEND THE FOLD TO READ THAT SHAPE — because
    # the shape is a `let` and pasting a single assignment's value where the
    # name is read is what a `let` means, not a model of one. See
    # `_decided_by`'s assignment clause and `_substituted`'s `in_scope`.
    #
    # SO THE PIN IS NOW WHAT THE HELPER DECIDES BY, and it carries the two
    # things the function NAME could not: the #270 fail-closed guard
    # (`player is None or not player.is_active`, so a deactivated player's
    # login cannot outlive their roster exit) and the membership spine
    # itself (`RosterService(store).team_for_game`, never the permanent
    # `Player.team_id` pointer). Deleting either one now moves this pin.
    # `RosterService` survives the fold as a NAME because it is a class —
    # a constructor is not a decision — and because the helper and its
    # caller are the same file, where that name means the same object.
    #
    # RE-PINNED IN ROUND 20, AND THE PIN GOT SHORTER WITHOUT GIVING ANYTHING
    # UP (#427). `_player_team_for_game` used to take the mapping and read
    # `(scope or {}).get('player_id')` out of it three times over, once per
    # fold of the same subexpression; it now takes the projected id, so every
    # one of those collapses to `scoped_player_id`. BOTH THINGS THE PIN
    # EXISTS FOR ARE STILL IN IT, and deleting either still moves it: the
    # #270 fail-closed guard (`store.get_player(...) is None or not
    # ....is_active`, so a deactivated player's login cannot outlive their
    # roster exit) and the membership spine itself
    # (`RosterService(store).team_for_game`, never the permanent
    # `Player.team_id` pointer). What went is the mapping, which was never
    # what this pin was protecting — and its absence is what
    # `a_binding_the_fold_would_drop_in_the_player_helper` now has no target
    # for: there is no `scope` in that helper to mutate any more.
    "PLAYER": _DeclaredAuthority(
        klass=PLAYER_SCOPED_BY_MEMBERSHIP,
        authority="None if not scoped_player_id else None if "
                  "store.get_player(scoped_player_id) is None "
                  "or not store.get_player(scoped_player_id)"
                  ".is_active else RosterService(store).team_for_game(game, "
                  "store.get_player(scoped_player_id))",
        admits="own_team is not None and own_team in "
               "(game.home_team_id, game.away_team_id)",
        side="own_team if admitted else None"),
    # An assignment the product still records as ACTIVE (d62473a). This
    # branch does not delegate, so `authority` and `admits` are the same
    # expression — MEASURED, not arranged: an official has no side of their
    # own, which is what `side` being a literal `None` says.
    #
    # RE-DECIDED IN ROUND 15, and this is what re-deciding it means. The
    # branch stopped spelling its own conjunction: `62a52b7` centralised the
    # question as `subject_scope.assignment_grants_official_scope`, called
    # from the private-game admission AND from
    # `context_scope._official_program_seasons` / `_official_league_ids`,
    # because the same declined grant was being answered two ways — refused
    # on `/board`, `/lineups` and `/roster` while the target Program, Season
    # and League stayed on offer in the context switcher.
    #
    # THE STALE PIN DID NOT SIMPLY GO RED, IT WENT VACUOUS. Reported as
    # `Ran 82 tests … FAILED (failures=12)` on the integrated tree: because
    # the OFFICIAL branch was mis-described on EVERY mutation, the negative
    # controls that require `_audit() == []` all saw it, and the falsifiers
    # for `_poison`, `_rests_on`, `_refuse_a_rebound_role`,
    # `_refuse_a_second_binding`, `_module_bindings_of` and the delegation
    # pins each detected their own vacuity and said so — "the mutation is
    # ALREADY reported with `_poison` removed, so this test is not measuring
    # `_poison` at all". That is the suite working: a stale pin had stopped
    # six rules from measuring anything.
    #
    # AND THE PIN COULD NOT SIMPLY BE MOVED ONTO THE CALL. `d62473a` exists
    # BECAUSE `status.is_active` was missing here and a DECLINED official
    # kept 200 on the private-game family; a pin reading
    # `assignment_grants_official_scope(a, official_id)` cannot see that
    # check at all, and neither can one asserting that three modules resolve
    # the same function object — that proves they share A predicate, never
    # that the predicate still tests status. So the derivation now FOLLOWS
    # THE CALL (`_inlined`) and this pin is what the branch DECIDES BY. It
    # gained the predicate's GUARD, which the inline spelling never had:
    # deleting `if assignment is None or not official_id: return False` now
    # moves the pin too. The BEHAVIOURAL half — that the answer still equals
    # `OfficialAssignmentStatus.is_active`, member by member, read off the
    # product enum — is
    # :meth:`EveryAdmissionBranchIsDerivedAndCarriesAnAuthority
    # .test_the_official_grant_answers_the_status_enum_member_by_member`,
    # because text can be rewritten while still testing status and can be
    # kept while testing nothing.
    #
    # RE-PINNED IN ROUND 20 FOR ONE REASON ONLY: the branch's own
    # `official_id = scope.get("official_id")` is gone, because THAT
    # STATEMENT IS WHERE THE ESCAPE THIS ROUND CLOSES LIVED. Relocated above
    # the COACH/PLAYER branch as
    #
    #     official_id = (scope.update({'team_id': game.home_team_id})
    #                    or scope.get("official_id"))
    #
    # it was static-audit green at `f9b094e` and admitted `thirdcoach` on
    # twenty of the fifty derived unentitled cells, tri-store. The branch now
    # reads the id the carrier projected once, above every branch, so the
    # local read is not there to hoist. Nothing else about this judgement
    # moved: the pin still carries the shared predicate's own GUARD (`False
    # if a is None or not official_id`) and still tests `a.status.is_active`
    # member by member — see the behavioural half below.
    "OFFICIAL": _DeclaredAuthority(
        klass=OFFICIAL_SUBMITTED_LINEUP_ONLY,
        authority="scoped_official_id is not None and any((False if a is "
                  "None or not scoped_official_id else a.official_id == "
                  "scoped_official_id and a.status.is_active for a in "
                  "store.assignments_for_game(game_id)))",
        admits="scoped_official_id is not None and any((False if a is "
               "None or not scoped_official_id else a.official_id == "
               "scoped_official_id and a.status.is_active for a in "
               "store.assignments_for_game(game_id)))",
        side="None"),
}


class _Sweep:
    """One world's measurement: ``{(principal, route, path, hint): result}``."""

    def __init__(self, rows, elapsed, requests, subject_of=None):
        self.rows = rows
        self.elapsed = elapsed
        self.requests = requests
        #: ``{(route, path): the GAME this concrete read is ABOUT, or
        #: None}`` — the SUBJECT axis (#427 round 9). Carried on the
        #: measurement rather than recomputed by each oracle, so both read
        #: the same answer for the same row.
        self.subject_of = subject_of or {}

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

    AND TWO ROWS THAT EXIST ONLY TO BE THE OTHER ONE (#427 round 11). A
    finding that is real but unreachable is still a finding, and "the fixture
    cannot express it" was the standing the owner's own blocker had, so the
    fixture expresses these:

    * a SECOND OFFICIAL holding the only assignment on the SECOND GAME. Until
      round 11 ``gid2`` carried no assignment row at all, so deleting
      ``a.official_id == official_id`` from the gate admitted the swept
      official to precisely the game they were already assigned to and moved
      nothing — the ``official_id`` dimension was unfalsifiable.
    * a SECOND GUARDIAN verified for the SAME JUNIOR. The oracle asked
      whether the JUNIOR had a verified link, and with one guardian in the
      world that reading was indistinguishable from the correct one. The
      ``guardian_link`` revocation now un-verifies the SWEPT guardian's link
      and leaves this one standing, which is the world where the two
      readings disagree.
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
        # A SECOND OFFICIAL, ASSIGNED TO THE SECOND GAME AND NOWHERE ELSE
        # (#427 round 11). Without it the swept official's `official_id`
        # dimension is unfalsifiable: `gid2` carried no assignment row at
        # all, so dropping `official_id == …` from the gate admitted the
        # swept official to exactly the game they were already assigned to
        # and changed nothing anyone could observe. With a row on `gid2` that
        # belongs to SOMEBODY ELSE, that deletion admits the swept official
        # to a game they do not referee, which the primary sweep must catch —
        # see `TheGrantIsKeyedByEveryDimensionOfItsRow`.
        second = api.create_official("Ref Morgan", actor_id=ADMIN)
        assert "error" not in second, second
        fx["official2_id"] = second["id"]
        assigned2 = api.assign_official(fx["gid2"], second["id"], "referee",
                                        actor_id=ADMIN)
        assert "error" not in assigned2, assigned2
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
        # A SECOND GUARDIAN, VERIFIED FOR THE SAME JUNIOR (#427 round 11, F3).
        # The oracle asked `any(link.verified for link in
        # guardian_links_for_player(junior))` — ANY guardian's link — and the
        # fixture held exactly one guardian, so the reading that grants the
        # swept guardian the junior on the strength of SOMEBODY ELSE'S link
        # was indistinguishable from the correct one. "Not demonstrable
        # because the fixture cannot reach the state" is the same standing
        # the owner's own blocker had, so the fixture reaches it.
        #
        # NOT A SWEPT PRINCIPAL. This account holds a link identical in every
        # dimension but `guardian_user_id`, so sweeping it would only
        # duplicate the swept guardian's own rows; what it is FOR is to be the
        # OTHER row, and the `guardian_link` revocation now un-verifies the
        # swept guardian's link and leaves this one verified.
        second_guardian = api.accounts.create_account(
            "guardian2", DEMO_PASSWORD, DEMO_USERS["guardian"], scope={},
            actor_id="test_seed")
        fx["guardian2_account_id"] = second_guardian.id
        other_link = api.create_guardian_link(
            second_guardian.id, fx["guardian_junior_id"], actor_id=ADMIN)
        assert "error" not in other_link, other_link
        verified = api.verify_guardian_link(other_link["id"], "signed_form",
                                            actor_id=ADMIN)
        assert "error" not in verified, verified
        # THE JUNIOR NOBODY IN THIS SWEEP IS A GUARDIAN OF, and the
        # UNENTITLED direction of the guardian route's own path argument
        # (#427 round 11, F1). A real HOME-side player, so a widening that
        # served them would also be a cross-side identity oracle 1 can see.
        fx["unlinked_junior_id"] = fx["people"]["seated"]["id"]
        assert fx["unlinked_junior_id"] != fx["guardian_junior_id"]
        assert not api.store.guardian_links_for_player(
            fx["unlinked_junior_id"]), (
                "the junior bound as the UNENTITLED direction of the "
                "guardian route already has a guardian link, so binding it "
                "sweeps the entitled direction twice")
        # Every principal's account id, taken from the store rather than from
        # the two created above — `get_accounts/{}/sessions` needs a real
        # subject and the six base principals are created by
        # `_ProjectionHarness._serve`.
        fx["account_ids"] = {a.username: a.id
                             for a in api.accounts.list_accounts()}
        assert set(PRINCIPALS) <= set(fx["account_ids"]), fx["account_ids"]
        # THE SESSION'S OWN BINDING, READ BACK FROM THE PRODUCT rather than
        # from the dicts above: `AccountService` canonicalizes a scope at
        # creation (#160), so what a live session actually carries is what
        # the stored account says it carries. This is the second of the three
        # sources :meth:`_dimension_value` resolves a grant dimension from.
        fx["scopes"] = {a.username: dict(a.scope or {})
                        for a in api.accounts.list_accounts()}
        # ``{grant-row dimension: a DIFFERENT, still-real value for it}`` —
        # what the measured dimension audit substitutes to ask whether a
        # dimension is keyed on at all. Every entry names a row this fixture
        # really holds, because an id naming nothing would collapse the
        # oracles for the wrong reason.
        # THE SECOND LEAGUESEASON, and the reason it has to exist (#427
        # round 12, LB1). `SeasonRosterMembership` is keyed on
        # `league_season_id` — that is the "exact game-season" half of the
        # #205 rule — and with ONE LeagueSeason in the world there is no
        # second value to move the row to, so the dimension would be
        # UNFALSIFIABLE in exactly the way `gid2`'s missing assignment row
        # made `official_id` unfalsifiable until round 11. It is bound to the
        # SIBLING Season the fixture already carries, holds no team
        # registration and no game, and exists only to be the OTHER
        # LeagueSeason.
        other_ls = api.setup.create_league_season(fx["league"]["id"],
                                                  fx["s2"]["id"])
        fx["other_league_season_id"] = other_ls.id
        assert other_ls.id != fx["ls_id"], other_ls
        fx["other_subjects"] = {
            "game_id": fx["gid2"],
            "official_id": fx["official2_id"],
            "player_id": fx["unlinked_junior_id"],
            "guardian_user_id": fx["guardian2_account_id"],
            # A team that plays in NEITHER game, so a membership moved onto
            # it grants nothing anywhere.
            "team_id": fx["third"],
            "league_season_id": other_ls.id,
            "season_id": fx["s2"]["id"],
        }
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
            "homecoach": (COACH_SCOPED_TO_ONE_SIDE, frozenset({fx["home"]})),
            "awaycoach": (COACH_SCOPED_TO_ONE_SIDE, frozenset({fx["away"]})),
            # Both Players are MOVERS: their entitled side is the one their
            # game-scoped MEMBERSHIP names, never their permanent pointer —
            # and since #427 round 12 that is a ROW-BACKED class, so this
            # constant is the WIDEST the class may reach and
            # `_subject_narrowed` reads the actual side off
            # `SeasonRosterMembership.team_id`.
            "homeplayer": (PLAYER_SCOPED_BY_MEMBERSHIP,
                           frozenset({fx["home"]})),
            "awayplayer": (PLAYER_SCOPED_BY_MEMBERSHIP,
                           frozenset({fx["away"]})),
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

    # -- the SUBJECT axis: the principal's RELATIONSHIP to what is read ----
    #
    # READ FROM THE STORE'S OWN ROWS, NEVER FROM THE GATE UNDER TEST. This is
    # the whole point of the closure and it is easy to get backwards: if
    # "is this official entitled to this game" were answered by asking
    # ``game_side_scope.resolve_private_game_read`` — the very function whose
    # admission rule the sweep is supposed to protect — then deleting that
    # rule would widen the EXPECTATION in lockstep with the behaviour and the
    # sweep would stay green. The authority is therefore the RELATIONSHIP
    # DATA (assignment rows, guardian links, the game's own two side ids),
    # which the gate reads but does not own.
    #
    # THAT CLAIM WAS FALSE AS WRITTEN UNTIL ROUND 11, AND THIS IS WHAT MADE IT
    # TRUE. Reading the store's rows is necessary and was not sufficient:
    # `_official_is_assigned` read the assignment rows and then reproduced the
    # gate's predicate over them VERBATIM —
    #
    #     any(a.official_id == fx["official_id"]
    #         for a in store.assignments_for_game(game_id))
    #
    # which is character-for-character `services/game_side_scope.py`'s own
    # OFFICIAL branch. A predicate copied from the gate widens with the gate
    # whatever it is copied out of, so the expectation moved in lockstep on
    # every dimension the gate happened to omit. It omitted one:
    # `OfficialAssignment.status`. MEASURED at the head this corrects — the
    # swept official DECLINED through the product's own `respond_assignment`
    # write path, the fixture otherwise untouched, `is_active` False, HTTP
    # `/board`, `/lineups` and `/roster` still 200 carrying `player_1` and
    # `player_12` — the PRIMARY SWEEP PASSED on all three backends:
    # `Ran 1 test in 110.641s … OK`.
    #
    # So the expectation is no longer a predicate at all. It is a MATCH
    # AGAINST THE GRANT ROW ON EVERY DIMENSION THE ROW DECLARES
    # (`_grant_rows` below), with the dimensions derived from the record by
    # `_keying_fields` and the row's own "does this grant anything" answer
    # taken from the PRODUCT'S OWN declared state by `_row_is_active`
    # (`OfficialAssignmentStatus.is_active`, `GuardianLink.verified`).
    # Deleting the gate's `and a.status.is_active` therefore does NOT move
    # this answer — it reddens the sweep, which
    # `TheGrantIsKeyedByEveryDimensionOfItsRow` requires by name.
    #: Set ONLY inside :meth:`_frozen_relationships`. See there for why a
    #: cache is sound in that window and nowhere else.
    _relationships = None

    @contextlib.contextmanager
    def _frozen_relationships(self):
        """Read each relationship fact ONCE for the duration of one oracle
        pass.

        SOUND BECAUSE THE WORLD IS ALREADY FROZEN, and unsound outside this
        window — which is why it is a context manager and not a cache. An
        oracle pass runs AFTER ``_sweep`` has returned: every request is
        answered, no perturbation or revocation is in flight, and the store
        cannot move until the pass ends. Within it the same two questions —
        "which grant rows cover this read" and "what are this game's two
        sides" — are asked ten times per swept path, once per principal, and
        on real PostgreSQL that was tens of thousands of round-trips per
        pass. Round 11 made the first of them read EVERY row of the kind
        rather than a pre-filtered slice (see :data:`GRANT_ROW_READERS`), so
        the memo matters more than it did, not less.

        The memo is torn down unconditionally, so a relationship read
        OUTSIDE a pass — the one a revocation changes — always reaches the
        store. `TheSubjectAxisIsClosedAgainstTheRelationshipRows
        .test_a_revoked_relationship_costs_the_grant_it_carried` is what
        proves that: it asserts the entitlement collapses across a
        revocation, which a memo that outlived its window would prevent."""
        self._relationships = {}
        try:
            yield
        finally:
            self._relationships = None

    def _remember(self, key, compute):
        memo = self._relationships
        if memo is None:
            return compute()
        if key not in memo:
            memo[key] = compute()
        return memo[key]

    def _dimension_value(self, fx, principal, field, subjects):
        """The value THIS REQUEST supplies for one dimension of a grant row —
        or ``None``, meaning it supplies none.

        THREE SOURCES, RESOLVED IN THIS ORDER, AND NONE OF THEM IS THIS
        MODULE. A dimension is either something about WHO IS ASKING or
        something about WHAT IS BEING READ, and both halves are read off the
        product:

        1. a ``*_user_id`` dimension is the SESSION'S OWN ACCOUNT ID — the
           id ``web/server._require_guardian_scope`` hands
           ``guardians.is_verified_guardian`` as its first argument, and the
           id ``GuardianLink.guardian_user_id`` stores;
        2. a dimension the ACCOUNT SCOPE binds is that scope's value, read
           back from the stored account (``fx["scopes"]``) rather than from
           the dict this fixture passed in — an Official's ``official_id``
           is the case that matters;
        3. anything else is what the PATH NAMES — the subject axis, in the
           same vocabulary, from :meth:`_path_subjects` (which includes the
           dimensions the SUBJECT GAME ROW itself carries; see
           :data:`SUBJECT_ROW_INHERITED`).

        SCOPE BEFORE PATH, AND FROM THIS ROUND IT IS LOAD-BEARING (#427 round
        12, LB1). The precedence was harmless while the only row-backed
        classes were the official's and the guardian's, because neither
        principal's scope binds a dimension its own routes also name. It
        stops being harmless the moment ``SeasonRosterMembership`` is
        modelled: ``player_id`` is then BOTH a scope key (a Player's stored
        scope is canonicalized to ``player_id`` alone, #160) AND a path
        argument (``/api/me/guardian/{player_id}/substitute-opportunities/
        {game_id}``).

        THE SESSION WINS, and the reason is the product's, not a
        convenience. ``game_side_scope._player_team_for_game`` reads
        ``scope.get("player_id")`` and nothing else, and the module's own
        docstring states the property the whole private-game family rests on:
        "NOTHING HERE READS A REQUEST… a query string, a body field or a
        header can never reach this resolution". A Player's grant is
        therefore a fact about WHO IS ASKING, and modelling it off a
        path-supplied ``player_id`` would make this oracle's answer a
        function of a value the CLIENT chose — the precise property every
        other axis of this file exists to deny. Symmetrically, for a
        principal whose scope does NOT bind the dimension — the guardian,
        whose ``player_id`` names the JUNIOR being read — the path is the
        only source and supplies it, unchanged.

        SO THE RULE IS: a dimension the READING PRINCIPAL'S OWN SCOPE binds
        is a fact about the caller and can never be overridden by the
        request; every other dimension is a fact about what is being read.
        :meth:`_session_fixed` classifies by exactly those two sources, and
        :meth:`TheSweptBindingsExerciseTheUnentitledDirection
        .test_a_path_cannot_override_a_dimension_the_session_binds` measures
        the precedence rather than trusting this paragraph.

        ``None`` FOR AN UNSUPPLIED DIMENSION IS DELIBERATE AND IS NOT THE
        HOLE F1 WAS. A route that names no junior (``get_me_guardian_home``)
        genuinely is a read of EVERY junior the caller is a guardian of, so
        the grant is not narrowed by a junior nobody named; the route that
        DOES name one is narrowed by it. What F1 actually was is that the
        oracle never received the argument the route DID supply — and
        :class:`TheSweptBindingsExerciseTheUnentitledDirection` is what keeps
        "the request supplies it" from silently becoming "the request never
        supplies anything but the entitled value"."""
        if field.endswith("_user_id"):
            return fx["account_ids"][principal]
        scope = fx["scopes"][principal]
        if field in scope:
            return scope[field]
        return subjects.get(field)

    @staticmethod
    def _session_fixed(fx, principal, field):
        """Is this dimension supplied by the SESSION rather than by the path?

        Answered from the two SOURCES — the account's own id under any
        ``*_user_id`` name, and the keys of the stored account scope — and
        deliberately NOT by asking :meth:`_dimension_value` what it returns.
        An audit that classified a dimension by the behaviour of the function
        under audit would call a dimension the function had stopped reading
        "session-fixed" and skip it, which is the shape of the finding it is
        supposed to catch."""
        return (field.endswith("_user_id")
                or field in fx["scopes"][principal])

    def _grant_rows(self, fx, klass, principal, subjects):
        """Every STORED grant row of ``klass`` that grants THIS principal
        THIS read — matched on EVERY DIMENSION the row declares.

        THE GOVERNING RULE OF ROUND 11, in one method. Read every row of the
        kind with nothing pre-filtered (:data:`GRANT_ROW_READERS`), drop the
        ones the PRODUCT'S OWN declared state says grant nothing
        (:func:`_row_is_active`), and keep the ones that agree with the
        request on every subject dimension the request supplies. Nothing
        here names ``game_id``, ``official_id``, ``player_id`` or
        ``guardian_user_id``: they arrive from :func:`_subject_fields`, so a
        grant row that GAINS a dimension is keyed on it the same day, and
        :class:`TheGrantIsKeyedByEveryDimensionOfItsRow` fails if the new one
        is not also MEASURED to matter."""
        record = GRANT_ROWS[klass]
        wanted = tuple(
            (field, self._dimension_value(fx, principal, field, subjects))
            for field in sorted(_subject_fields(record)))

        def read():
            rows = getattr(fx["api"].store, GRANT_ROW_READERS[record])()
            return tuple(
                row for row in rows
                if _row_is_active(record, row)
                and all(want is None or str(getattr(row, field)) == str(want)
                        for field, want in wanted))
        return self._remember(("grant", klass, principal, wanted), read)

    def _official_is_assigned(self, fx, subjects):
        """Does the swept official hold an ACTIVE assignment that covers this
        read? — the store's own rows, matched on every dimension
        ``OfficialAssignment`` declares, never ``resolve_private_game_read``
        and never that function's predicate copied out."""
        return bool(self._grant_rows(
            fx, OFFICIAL_SUBMITTED_LINEUP_ONLY, "official", subjects))

    def _subject_sides(self, fx, subject):
        """The two side ids OF THE GAME BEING READ — ``game.home_team_id`` /
        ``away_team_id``, the game's own record."""
        def read():
            game = fx["api"].store.get_game(subject)
            if game is None:
                return frozenset()
            return frozenset({game.home_team_id, game.away_team_id}) - {None}
        return self._remember(("sides", subject), read)

    def _subject_narrowed(self, fx, klass, teams, subjects, principal):
        """``teams`` narrowed by the principal's RELATIONSHIP to what this
        path names.

        THE BLIND SPOT THIS CLOSES (#427 round 9). Entitlement was keyed on
        ``(principal, ROUTE, data class)`` and never on WHICH GAME the route
        was being read for, so every statement the oracles made about a
        principal held identically for a game they had no relationship with
        at all. MEASURED on the head this corrects: deleting the official's
        assignment check — three lines in
        ``services/game_side_scope.resolve_private_game_read`` — leaves an
        official admitted 200 to ``/board``, ``/lineups`` and ``/roster`` of
        a game carrying ZERO assignment rows for them, serving that game's
        private identities, and the PRIMARY SWEEP PASSED: ``Ran 27 tests in
        175.345s … OK`` on all three backends. Exactly ONE test in the whole
        backend noticed (``test_lineup_side_projection
        ::TheExistingRefusalsStillRefuse``, memory and sqlite).

        A ROUTE WITH NO GAME SUBJECT KEEPS THE PRINCIPAL-LEVEL GRANT, and
        that is deliberate rather than an omission: a Coach's own dashboard
        is ABOUT their side, so narrowing it by a game it names none of
        would make the sweep report their own legitimate row as a leak.
        Subject narrowing applies exactly where there is a subject to narrow
        against.

        AND THE NARROWING IS NOW ONE RULE FOR BOTH ROW-BACKED CLASSES (#427
        round 11). It used to be two hand-written questions — "is the
        official assigned to this game" and "is the guardian's link
        verified" — and each omitted a dimension its own row declares: the
        first ignored ``status``, the second ignored BOTH
        ``guardian_user_id`` (so another guardian's link granted the swept
        one, F3) and ``player_id`` (so the junior in the path never reached
        the oracle at all, F1). :meth:`_grant_rows` asks the single question
        both were approximations of."""
        subject = subjects.get(GAME_SUBJECT)
        if klass in GRANT_ROWS:
            rows = self._grant_rows(fx, klass, principal, subjects)
            if not rows:
                # No stored row grants this principal this read: an official
                # refereeing a DIFFERENT game, an official whose assignment
                # the product records as DECLINED, a guardian of a DIFFERENT
                # junior, a guardian whose own link is unverified, a PLAYER
                # whose membership for this game-season the product no
                # longer treats as participating. Each is a stranger to this
                # read — the same standing this sweep gives a coach of
                # neither team.
                return frozenset()
            record = GRANT_ROWS[klass]
            if "team_id" in _subject_fields(record):
                # …AND WHERE THE GRANT ROW ITSELF NAMES THE SIDE, THE SIDE IS
                # READ OFF THE ROW (#427 round 12, LB1) — the exact analogue
                # of `_permitted_ids` reading the guardian's junior off
                # `row.player_id` rather than off a constant. A Player's side
                # IS `SeasonRosterMembership.team_id`, so a membership that
                # moves, ends or stops participating collapses the
                # entitlement instead of leaving `_entitlement`'s constant
                # standing. Intersected rather than substituted: this may
                # only ever NARROW the class's widest entitlement.
                teams = frozenset(teams) & frozenset(
                    row.team_id for row in rows)
        if subject is None:
            return teams
        # …and nobody is entitled to a side that is not one of THIS game's
        # two, whatever their class: a stale grant cannot survive the game
        # it was granted against.
        return frozenset(teams) & self._subject_sides(fx, subject)

    def _grant_spans(self, klass, subjects, perturbed_game):
        """May private state of ``perturbed_game`` legitimately move a
        response whose subject is ``subjects``, for a principal of ``klass``?

        THE BLIND SPOT THIS CLOSES (#427 round 10, D10, owner comment
        5432572444). Oracle 2 was handed the perturbed TEAM and never the
        perturbed GAME, so "an official may observe the submitted lineup of a
        game they referee" was applied to a response about their assigned
        game that had moved because a DIFFERENT game's sheet changed. The two
        games in this fixture share both teams, so the team id alone cannot
        tell the two situations apart — which is exactly why keying on it
        alone was undetectable.

        THE RULE IS DERIVED FROM THE GRANT ROW, NOT LISTED. A class whose
        grant row names a game (:data:`GAME_KEYED_CLASSES`) holds it against
        THAT game and no other, so a diff is excusable only where the
        response's own subject IS the perturbed game. A class whose grant row
        names no game — a coach's ``team_id`` scope, a guardian's
        ``GuardianLink`` — is a standing authority over every game its
        subject appears in, and narrowing it here would report the real
        product grant as a leak. See :data:`GRANT_DIMENSIONS`.

        ``perturbed_game is None`` means "no perturbation is in question" —
        the identity oracle's path — and spans everything by construction."""
        if perturbed_game is None or klass not in GAME_KEYED_CLASSES:
            return True
        subject = subjects.get("game_id")
        return subject is not None and subject == perturbed_game

    def _entitled_teams(self, fx, principal, route, subjects, data_class,
                        perturbed_game=None):
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
        # THE SUBJECT AXIS FIRST: whatever the route and the data class say,
        # a principal with no relationship to the thing being read is
        # entitled to nothing of it.
        teams = self._subject_narrowed(fx, klass, teams, subjects, principal)
        # …AND A GAME-KEYED GRANT CANNOT SPAN A SECOND GAME (#427 round 10,
        # D10). The team id alone cannot separate "this game's sheet moved"
        # from "the other game's sheet moved" when both games share both
        # teams, which is what made this invisible.
        if not self._grant_spans(klass, subjects, perturbed_game):
            return frozenset()
        if klass == GUARDIAN_OF_A_JUNIOR:
            # The junior's own SIDE, on the junior's own two routes — not a
            # standing grant over the junior's whole team on all 50 swept
            # route names, and not the junior's own ROW either: the
            # row-level narrowing is oracle 1's, in `_permitted_ids`. What
            # is returned here is the
            # SIDE whose private state may legitimately move these two
            # routes, which is what oracle 2 needs.
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
        what makes :meth:`_assert_inventory_is_closed` fail closed.

        EVERY PATH ARGUMENT THAT NAMES A GRANT-ROW DIMENSION IS BOUND IN BOTH
        DIRECTIONS (#427 round 11, F1). It is not enough that a route is
        swept: if every value the sweep ever binds for an argument is one the
        principal IS entitled to, the unentitled direction is never
        exercised and a widening that served somebody else's row passes. The
        guardian's junior was bound to the guardian's OWN junior both times,
        which is exactly how F1 stayed invisible.
        :class:`TheSweptBindingsExerciseTheUnentitledDirection` audits this
        against :data:`GRANT_DIMENSIONS` rather than against this comment."""
        games = [(fx["gid"],), (fx["gid2"],)]
        junior = fx["guardian_junior_id"]
        stranger = fx["unlinked_junior_id"]
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
            # BOTH DIRECTIONS of the `official_id` dimension: the swept
            # official's own row, and the SECOND official's, whom they are
            # not.
            "get_officials_id_availability": [(fx["official_id"],),
                                              (fx["official2_id"],)],
            "get_me_substitute_opportunities_id": list(games),
            # BOTH DIRECTIONS of the `player_id` dimension: the junior this
            # guardian holds a verified link to, and one they do not (#427
            # round 11, F1). Both games for the linked junior keeps the
            # `game_id` axis of this route unchanged.
            "get_me_guardian_id_substitute_opportunities_id": [
                (junior, fx["gid"]), (junior, fx["gid2"]),
                (stranger, fx["gid"])],
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

    # -- the private-game family, derived and ADDRESSABLE ------------------
    def _private_game_family(self):
        """:func:`private_game_family`, plus every property this file needs
        in order to DRIVE each leaf — so a leaf it cannot address fails
        NAMING THE LEAF instead of dropping out of the derivation.

        FAIL-CLOSED IN FOUR DIRECTIONS, each one a way a new leaf could
        otherwise become invisible: it is not in this sweep's own
        authenticated-GET inventory; it carries a ``NOT_SWEPT`` excuse; it
        takes a path argument besides the game id; or it spells that
        argument with a placeholder :meth:`_path_of` does not fill."""
        family = private_game_family()
        self.assertGreater(
            len(family), 5,
            "the registry yielded almost no GET leaves under "
            "/api/games/{id}/, so every assertion this file derives from "
            "the private-game family would be vacuous")
        authed = {spec.name for spec in self._authenticated_get_specs()}
        for name, spec in sorted(family.items()):
            self.assertIn(
                name, authed,
                f"{name} is a leaf of the private-game family that this "
                f"sweep's own inventory does not carry — the registry "
                f"records auth={spec.auth!r}. A family leaf outside "
                f"`_authenticated_get_specs` is swept by no oracle and "
                f"refused for no principal; decide it deliberately rather "
                f"than letting the auth filter decide it silently.")
            self.assertNotIn(
                name, NOT_SWEPT,
                f"{name} is a leaf of the private-game family carrying a "
                f"NOT_SWEPT excuse. This family is the surface the whole of "
                f"#205 is about; no leaf of it may be excused from the "
                f"sweep.")
            self.assertEqual(
                (1, 1),
                (spec.template.count("{"), spec.template.count("{}")),
                f"{name}: the private-game family gained a leaf this file "
                f"cannot ADDRESS — {spec.template!r} does not take exactly "
                f"one `{{}}` game id. Bind its other arguments deliberately; "
                f"a leaf nobody can drive is a leaf nobody asserts.")
        return family

    @staticmethod
    def _leaf_word(spec):
        """A leaf's last path segment — how the per-leaf matrices in this
        file spell it. Derived from the template so the word and the route
        are never two independent statements."""
        return spec.template.rsplit("/", 1)[1]

    #: The GAME SUBJECTS every family matrix in this file is driven against.
    #:
    #: STILL HAND-ENUMERATED, AND STILL OPEN — recorded here rather than
    #: repeated at five call sites so the gap has ONE place to be closed
    #: from. Deleting ``"gid2"`` leaves this module green; the owner has
    #: ruled that cannot be deferred without an explicit scope change and it
    #: is NOT closed by #427 round 18. What round 18 does change is that the
    #: PRINCIPAL axis is no longer independent of this one — see
    #: :meth:`_family_status_matrix_over`.
    GAME_KEYS = ("gid", "gid2")

    def _family_status_matrix_over(self, who, fx, family, cells):
        """``{(principal, route name, game key): HTTP status}`` — every leaf
        of ``family``, driven for exactly these ``(principal, game key)``
        CELLS, over real authenticated HTTP.

        THE CELL IS THE UNIT, NOT THE PRINCIPAL (#427 round 18, blocker 1).
        "Which principals may read nothing" is a question with no answer in
        this product: an official's grant is ASSIGNMENT-KEYED PER GAME, so
        the same principal is entitled on one game and a stranger on the
        other. A matrix whose rows were principals could only ask the
        question of a principal entitled to NO game at all, which is why the
        swept official — whose global entitled-team set is ``{home, away}``
        — was outside it on the game they do not referee.

        Addressed through :meth:`_path_of`, the same one definition the
        sweep itself builds paths with, so this cannot probe a path the
        sweep never produces."""
        measured = {}
        for principal, game_key in cells:
            for name, spec in sorted(family.items()):
                status, _body = self._req(
                    who[principal], "GET",
                    self._path_of(spec, (fx[game_key],)))
                measured[(principal, name, game_key)] = status
        return measured

    def _family_status_matrix(self, who, fx, family, principals):
        """:meth:`_family_status_matrix_over` for every cell these
        principals have — every leaf x BOTH GAMES.

        BOTH GAMES BECAUSE THE FIXTURE SWAPS THE SIDES. A principal
        unentitled to one game may be entitled to the other — that is
        exactly the subject-axis defect round 11 found — so a matrix driven
        on ``gid`` alone would be a statement about one arrangement of the
        sides rather than about the rule."""
        return self._family_status_matrix_over(
            who, fx, family,
            [(principal, game_key) for principal in principals
             for game_key in self.GAME_KEYS])

    def _admissions(self, measured):
        """The cells of a status matrix that are NOT a refusal, named."""
        return sorted(
            f"{principal} was answered {status} on {name} ({game_key})"
            for (principal, name, game_key), status in measured.items()
            if status != REFUSED)

    # -- the sweep ---------------------------------------------------------
    def _canonical(self, body):
        """The body with the documented time-varying keys removed."""
        if isinstance(body, dict):
            return {k: self._canonical(v) for k, v in body.items()
                    if k not in self.VOLATILE_KEYS}
        if isinstance(body, list):
            return [self._canonical(v) for v in body]
        return body

    def _probe_value(self, fx, label):
        """The concrete value a probe label sends.

        Every label in :data:`PROBE_LABELS` resolves here; a label that does
        not is a ``KeyError`` naming it, which is why the labels are spelled
        rather than the values being inlined into
        :data:`QUERY_PARAMETER_PROBES`."""
        return {
            "home_team": fx["home"],
            "away_team": fx["away"],
            "season": fx["s1"]["id"],
            # A value in the PARAMETER's own domain rather than a team id:
            # `scope_type` names a KIND of scope, and a probe outside its
            # domain would be rejected before it could select anything.
            "scope_type_team": "team",
            "actor_type_player": "player",
            # The AWAY side's own Mover — an identity a HOME-scoped caller
            # must not be able to name their way into.
            "away_player": fx["people"]["awayside"]["id"],
            # `side`'s real domain is a SIDE WORD, not a team id — see
            # :data:`UNREAD_PARAMETER_CONTROL`.
            "away_word": "away",
        }[label]

    def _hint_query(self, fx, hint):
        if hint == "none":
            return ""
        param, label = hint.split("=", 1)
        return f"?{param}={self._probe_value(fx, label)}"

    @staticmethod
    def _path_of(spec, args):
        """The concrete path a route spec + its bound arguments name — ONE
        definition, so a test that drives a leaf by hand cannot address a
        path the sweep itself never produces."""
        path = spec.template
        for arg in args:
            path = path.replace("{}", arg, 1)
        return path

    def _sweep(self, who, fx, specs, subjects, hints=None):
        """One world: every principal x every route x every hint.

        ``hints`` defaults to :data:`HINTS`, the per-world matrix; the
        closure test passes :data:`FULL_HINTS`. See :data:`HINTS` for why
        the axis is measured in two places and what that costs."""
        hints = HINTS if hints is None else hints
        started = time.time()
        rows, requests, subject_of = {}, 0, {}
        for spec in specs:
            if spec.name not in subjects:
                continue
            for args in subjects[spec.name]:
                path = self._path_of(spec, args)
                subject_of[(spec.name, path)] = self._path_subjects(fx, args)
                for principal in PRINCIPALS:
                    for hint in hints:
                        status, body = self._req(
                            who[principal], "GET",
                            path + self._hint_query(fx, hint))
                        requests += 1
                        rows[(principal, spec.name, path, hint)] = (
                            status, self._canonical(body))
        return _Sweep(rows, time.time() - started, requests, subject_of)

    #: ``{grant-row dimension: the store reader that answers "does this id
    #: name one of THOSE?"}`` — how a path argument is resolved to a
    #: DIMENSION rather than to "the game argument, if any".
    #:
    #: THE VOCABULARY IS THE GRANT ROWS' OWN (#427 round 11). Round 9 asked
    #: only ``get_game``, so a path could name a game and nothing else; the
    #: junior in ``/api/me/guardian/{}/substitute-opportunities/{}`` was
    #: discarded before any oracle saw it, which is F1. Every subject field
    #: any grant row declares must be answerable here, and
    #: :meth:`TheGrantIsKeyedByEveryDimensionOfItsRow
    #: .test_every_subject_dimension_is_resolvable_from_a_path` asserts that
    #: — so a row that gains a subject field the sweep cannot recognise in a
    #: path is an ERROR NAMING IT.
    SUBJECT_READERS = {
        "game_id": "get_game",
        "player_id": "get_player",
        "official_id": "get_official",
        "team_id": "get_team",
        # `SeasonRosterMembership`'s competition keys (#427 round 12, LB1).
        # No path in this product names either directly; both arrive by
        # INHERITANCE from the subject game (:data:`SUBJECT_ROW_INHERITED`),
        # and they are listed here because
        # `test_every_subject_dimension_is_resolvable_from_a_path` requires
        # every subject dimension of every grant row to be recognisable —
        # a dimension the sweep cannot RESOLVE is F1 one layer earlier.
        "league_season_id": "get_league_season",
        "season_id": "get_season",
    }

    def _path_subjects(self, fx, args):
        """``{dimension: value}`` — WHAT this concrete path is a read ABOUT.

        Answered by ASKING THE STORE what each argument names, not by a
        hand-written map of which routes take which id, so a new
        subject-bearing route acquires its subjects the moment it acquires a
        binding in :meth:`_route_subjects`, with nothing to keep in step.
        An argument that names none of them (a Season, a League, a Program,
        an account, :data:`ABSENT`) contributes nothing, which is correct:
        no grant row in this product is keyed on one.

        AND A GAME ARGUMENT CONTRIBUTES THE GAME ROW'S OWN DIMENSIONS TOO
        (#427 round 12, LB1) — :data:`SUBJECT_ROW_INHERITED`, derived as the
        fields ``Game`` declares that a grant row is keyed on by the same
        name. ``SeasonRosterMembership.league_season_id`` is why: no path
        anywhere in this product names a LeagueSeason, so without this the
        Player's grant would match a membership in ANY competition and the
        "exact game-season" half of the #205 rule would never reach an
        oracle. Only the GAME row contributes — see the constant for the
        ``Player.team_id`` pointer this deliberately does not re-admit."""
        store = fx["api"].store
        out = {}
        for arg in args:
            for field, reader in self.SUBJECT_READERS.items():
                if field in out:
                    continue
                row = getattr(store, reader)(arg)
                if row is None:
                    continue
                out[field] = arg
                if field == GAME_SUBJECT:
                    for name in sorted(SUBJECT_ROW_INHERITED):
                        value = getattr(row, name, None)
                        if value is not None:
                            out.setdefault(name, value)
                break
        return out

    def _subject_of(self, fx, *args):
        """The subject a test means when it says "a read about these rows" —
        built by the SAME resolution the sweep uses, so a hand-written call
        site cannot mean something the sweep never produces."""
        return self._path_subjects(fx, args)

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

    #: FIELDS OF A ``Player`` RECORD THAT ARE NOT IDENTITY.
    #:
    #: ``team_id`` names a SIDE, not a person: it is the same string for
    #: every player of a side, and putting it in the alphabet would report a
    #: side's own public ``team_id`` as a leaked identity on every route that
    #: names the opponent — which the redaction contract requires it to do.
    #: Everything else the record carries IS a way to name that one person,
    #: which is why this is a two-name exclusion list rather than an
    #: inclusion list.
    NON_IDENTITY_PLAYER_FIELDS = ("team_id",)

    def _identity_tokens(self, player):
        """Every way the product can name this one person WITH A STRING,
        derived from the ``Player`` record's own fields rather than
        hand-listed.

        THE LIMIT, STATED BECAUSE IT IS NOT CLOSED (#427 round 9 review).
        The FIELD SET is derived; the VALUE SPACE is not. ``type(v) is str``
        admits nine string-valued fields and excludes every numeric one, so
        a NUMERIC identity is outside this alphabet. ``jersey_number`` is
        not hypothetical: it is served on ``_lineup_rows`` and allow-listed
        in ``_SHEET_PLAYER_FIELDS``. A route handing ``thirdcoach`` the away
        side's five jersey numbers passes oracle 1 green.

        HOW MUCH OF THE SWEPT SURFACE THAT IS, RE-MEASURED (#427 round 10).
        The sentence that stood here said "it names a person uniquely on 779
        of 2,480 rows, on ``/board``, ``/lineups`` and ``/roster``". Three
        things in it were wrong, and this file exists to catch exactly that:
        779 is not a number this fixture produces at all, the denominator
        framed NODES as if they were REQUESTS, and the route list was short
        by one. Counted by walking every swept body for a dict carrying a
        non-null ``jersey_number``:

        * **789 jersey-bearing NODES**, on **112 of the 2,560 requests** of
          the base world — a node is one player on one response, so the two
          are different units and 779-of-2,480 read as neither;
        * on **FOUR** routes, not three: ``get_games_id_lineups`` (424),
          ``get_games_id_board`` (316), ``get_players`` (41) and
          ``get_games_id_roster`` (8). ``get_players`` is a flat roster list
          outside the private-game family and was missing from the list;
        * across the matrix's TWENTY-FOUR worlds the node count runs **789
          to 1,080** and the request count **96 to 112** — it is never 779
          in any of them. (The node floor is the first base world; the
          ceiling is a late world, because each ``seated_lineup_row``
          perturbation adds a player and the store grows monotonically
          across a run.) RE-MEASURED ON THE ROUND-12 TREE, all 24 worlds:
          the node range is unchanged and the REQUEST FLOOR drops from 100
          to **96**, in exactly one world — ``revoked/season_roster_
          membership``, where the ex-member is refused the whole
          private-game family and therefore carries four fewer
          jersey-bearing responses than any other world;

        WHAT BOUNDS IT, ALSO MEASURED: **all 789 of the 789 nodes carry the
        player ``id`` as well**, so a realistic ``_lineup_rows`` disclosure
        is still caught through the id; the jersey is the sole carrier only
        in a payload that deliberately omits the id. Of the 30 distinct
        jersey values served, none names more than one person, so the
        "uniquely" in the old sentence was the one part of it that held. And
        no live leak exists today — only callers entitled to both sides, plus
        each side's own coach and player, receive their own side's jerseys;
        zero cross-side. CLOSING THIS NEEDS A PRODUCT-SIDE AUTHORITY OVER
        IDENTITIES rather than over ``Player``'s fields, which does not exist
        yet. Until it does, this is a disclosed limit with a measurement, not
        a closure — and the base-world numbers above are re-measured every
        run by :meth:`TheDisclosedLimitsAreMeasuredNotRemembered
        .test_the_numeric_identity_blind_spot_is_still_the_measured_one`, so
        this paragraph cannot become another set of stale numbers.

        THE BLIND SPOT THIS CLOSES (#427 round 9, D8). Oracle 1's alphabet
        was the ``id`` FIELD ALONE. A name is an identity too, and so is a
        registration number, and the record carries nine string-valued
        identity fields today. MEASURED on the head this corrects: a
        registered authenticated route handing ``thirdcoach`` — typed
        :data:`IN_NEITHER_SIDE`, entitled to nothing of either side — the
        AWAY side's five private people BY NAME (``["Away Legacy Sub",
        "Away Member", "Away Seated", "Away Sub", "Backed Out Seat away"]``)
        passed ALL THREE ORACLES GREEN, because none of those five strings
        was in any forbidden set.

        DERIVED, SO A NEW FIELD ENTERS AUTOMATICALLY. ``dataclasses.fields``
        over the live record is the authority: the day ``Player`` gains
        another string identity field, that field's values are in the
        alphabet without anyone editing this file — the property the ROUTE
        axis has had against the registry since round 1.

        ``type(v) is str``, NOT ``isinstance``. ``Position`` is a
        str-subclass enum, so ``isinstance`` admits ``"forward"`` as an
        identity — a shared enum value, not a way to name a person. It
        happened to be dropped by the uniqueness rule below on THIS fixture
        (two players share it); a fixture with one goalie per side would
        have put ``"goalie"`` in the alphabet and reported every honest
        response as a leak. Measured, not theorised.
        """
        out = set()
        for field in dataclasses.fields(player):
            if field.name in self.NON_IDENTITY_PLAYER_FIELDS:
                continue
            value = getattr(player, field.name)
            if type(value) is str and value:
                out.add(value)
        return frozenset(out)

    @staticmethod
    def _players_in(owners):
        return {pid for who in owners.values() for pid in who}

    def _token_owners(self, fx):
        """``{token: the set of players it can name}`` across every player
        this fixture has — the input to the uniqueness rule."""
        api = fx["api"]
        owners = {}
        seen = set()
        for gid in (fx["gid"], fx["gid2"]):
            game = api.store.get_game(gid)
            for side in (game.home_team_id, game.away_team_id):
                if not side:
                    continue
                for player in api.store.players_for_team(side):
                    seen.add(player.id)
        for team in (fx["home"], fx["away"], fx["third"]):
            for player in api.store.players_for_team(team):
                seen.add(player.id)
        for person in fx["people"].values():
            seen.add(person["id"])
        for pid in seen:
            player = api.store.get_player(pid)
            if player is None:
                continue
            for token in self._identity_tokens(player):
                owners.setdefault(token, set()).add(pid)
        return owners

    def _alphabet_index(self, fx):
        """``{player id: that person's UNAMBIGUOUS identity tokens}``,
        computed ONCE per oracle run.

        A token two different people answer to names neither of them, so it
        is omitted — the SAME "ambiguity is omitted, never guessed" ruling
        :meth:`_private_side_ids` applies to side membership, applied to the
        alphabet for the same reason. Without it a shared value would put a
        side's own coach in breach for reading their own player.

        BUILT PER CALL, NEVER CACHED ACROSS WORLDS. A perturbation SEATS A
        FRESH PLAYER, and a cached index would leave that person's name out
        of the alphabet in exactly the perturbed world the oracle is reading
        — silently narrowing the forbidden set in the half of the matrix
        that matters. It is passed down instead of memoised so the saving is
        structural rather than a cache that can go stale."""
        owners = self._token_owners(fx)
        unique = {t for t, who in owners.items() if len(who) == 1}
        index = {}
        for pid in self._players_in(owners):
            player = fx["api"].store.get_player(pid)
            if player is None:
                continue
            index[pid] = frozenset(
                t for t in self._identity_tokens(player) if t in unique)
        return index

    def _tokens_of(self, fx, ids, index=None):
        """The UNAMBIGUOUS identity tokens of ``ids`` — see
        :meth:`_alphabet_index`, which this is a projection of."""
        if index is None:
            index = self._alphabet_index(fx)
        out = set()
        for pid in ids:
            # An id with no record is still an id: it names exactly one
            # thing, and a route serving it is serving an identity.
            out |= index.get(pid) or frozenset({pid})
        return frozenset(out)

    @staticmethod
    def _shadowing(alphabet):
        """The tokens in ``alphabet`` that CONTAIN another token in it.

        Only these can produce a false positive, so only these have to be
        excised before the forbidden search — which turns
        :meth:`_redact` from "one substitution per permitted token per
        response" into "one per genuinely ambiguous token", and on this
        fixture that is two rather than twenty-six."""
        return frozenset(
            token for token in alphabet
            if any(other != token and other in token for other in alphabet))

    @staticmethod
    def _redact(blob, tokens):
        """``blob`` with every PERMITTED token excised, LONGEST FIRST.

        WHY THIS IS NEEDED THE MOMENT THE ALPHABET STOPS BEING IDS (#427
        round 9, D8). Word-boundary matching is what keeps ``player_1`` from
        reporting ``player_12``; it does NOT help between ``"Legacy Sub"``
        and ``"Away Legacy Sub"``, where the shorter name sits inside the
        longer one WITH word boundaries on both sides. MEASURED: matching
        naively made HOME's ``Legacy Sub`` a false positive in the AWAY
        Coach's own honest response, on five routes.

        Excising what the caller MAY see, before looking for what they may
        not, is exact rather than approximate: a genuinely leaked
        ``Legacy Sub`` standing on its own is still there afterwards.
        Longest-first so a permitted long name is removed before a permitted
        short one can eat half of it."""
        for token in sorted(tokens, key=len, reverse=True):
            blob = re.sub(rf"\b{re.escape(token)}\b", "\x00", blob)
        return blob

    def _submitted_side_ids(self, fx):
        """``{(game_id, team_id): frozenset}`` — the identities that OCCUPY A
        SLOT on ONE side's sheet IN ONE GAME, which is the strictly narrower
        population an assigned official is entitled to.

        ``ApiService._submitted_lineup_rows`` over ``_lineup_rows`` — the
        SAME pair the three official routes run, called rather than
        re-implemented, so "what the official may see" cannot drift from
        what the official is served. Without this, widening the forbidden
        set to the whole private population (above) would have handed the
        official the candidate pool as PERMITTED on their three routes, and
        oracle 1 would have stopped being able to see an official receiving
        a candidate identity — closing D3 by opening a new hole one seat
        over, which is this round's recurring shape.

        KEYED BY ``(GAME, SIDE)``, NEVER BY SIDE ALONE (#427 round 10, D10,
        owner comment 5432572444). This used to UNION a side's occupants
        across both games into ``{team_id: ids}``, and
        :meth:`_permitted_ids` then handed that aggregate to an official on
        the strength of an assignment to the RESPONSE's game. The two games
        share both teams, so the aggregate silently carried the OTHER game's
        sheet. MEASURED at the head this corrects: the official is assigned
        to ``game_1`` and not to ``game_2``; ``game_1`` seats ``player_1``
        and ``player_12`` while ``player_6`` occupies ``game_2`` alone; yet
        ``_permitted_ids(official, get_games_id_board, subject=game_1)``
        answered ``{player_1, player_12, player_6}``, and injecting
        ``player_6`` into the official's authenticated ``GET
        /api/games/game_1/board`` left ``_assert_no_foreign_ids`` GREEN with
        that id in the raw swept body. Assignment is game-specific; an
        aggregate over the games two teams happen to share is not."""
        out = {}
        api = fx["api"]
        for gid in (fx["gid"], fx["gid2"]):
            game = api.store.get_game(gid)
            for side in (game.home_team_id, game.away_team_id):
                if not side:
                    continue
                rows = api._submitted_lineup_rows(
                    api._lineup_rows(game, side))
                out[(gid, side)] = frozenset(row["id"] for row in rows)
        return out

    def _permitted_ids(self, fx, principal, route, subjects, private,
                       submitted):
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

          AND THE JUNIOR IS NOW READ OFF THE MATCHING GRANT ROWS (#427 round
          11, F1). It used to be the constant ``fx["guardian_junior_id"]``,
          returned for either of the two guardian routes whatever junior the
          PATH named — this method never received the junior at all. The
          swept binding was the guardian's own junior in both of its rows,
          so the unentitled direction was never exercised and a widening
          that served another junior's identity would have passed. What is
          returned now is ``row.player_id`` of every ``GuardianLink`` that
          matches this request on every dimension the row declares, which is
          the guardian's own junior for the linked path and NOTHING for the
          stranger's.
        * :data:`OFFICIAL_SUBMITTED_LINEUP_ONLY` — the OCCUPYING rows of both
          sides OF THE RESPONSE'S OWN SUBJECT GAME, on
          :data:`OFFICIAL_ASSIGNED_GAME_ROUTES`, and no identity anywhere
          else. Keyed by ``(subject game, side)`` rather than by side, which
          is D10: see :meth:`_submitted_side_ids` for the measurement.
        * everyone else — the whole private population of each side
          :meth:`_entitled_teams` grants them.
        """
        klass, _teams = self._entitlement(fx)[principal]
        subject = subjects.get("game_id")
        if klass == GUARDIAN_OF_A_JUNIOR:
            if route not in GUARDIAN_JUNIOR_ROUTES:
                return frozenset()
            # THE SUBJECT AXIS, on every dimension `GuardianLink` declares:
            # an unverified link is not a grant, ANOTHER guardian's link is
            # not this guardian's grant, and a junior this guardian holds no
            # link to is not theirs to receive.
            return frozenset(
                row.player_id for row in
                self._grant_rows(fx, klass, principal, subjects))
        if klass == OFFICIAL_SUBMITTED_LINEUP_ONLY:
            if route not in OFFICIAL_ASSIGNED_GAME_ROUTES:
                return frozenset()
            # THE SUBJECT AXIS: the sheet of a game they REFEREE under an
            # assignment the product still records as ACTIVE, read from the
            # store's own rows — not "any sheet, anywhere".
            if not self._official_is_assigned(fx, subjects):
                return frozenset()
            # …and THAT GAME'S sheet, both of ITS sides. Indexed rather than
            # `.get`-ed on purpose: a subject game missing from the map is a
            # KeyError naming it, never a silent under-permission that would
            # redden this oracle for the wrong reason (#427 round 10, D10).
            return frozenset().union(*(
                submitted[(subject, side)]
                for side in sorted(self._subject_sides(fx, subject))))
        teams = self._entitled_teams(
            fx, principal, route, subjects, SUBMITTED_LINEUP_DATA)
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
        with self._frozen_relationships():
            self._no_foreign_ids(sweep, fx, label)

    def _no_foreign_ids(self, sweep, fx, label):
        private, ambiguous = self._private_side_ids(fx)
        submitted = self._submitted_side_ids(fx)
        everything = private[fx["home"]] | private[fx["away"]]
        # THE ALPHABET (#427 round 9, D8): every way the product can NAME one
        # of these people, not just their id field. Built ONCE and threaded
        # through every lookup below — it is a whole-fixture computation and
        # this oracle asks it for ten principals x every swept path.
        index = self._alphabet_index(fx)
        alphabet = self._tokens_of(fx, everything, index)
        self.assertEqual(
            frozenset(), ambiguous,
            f"[{label}] {sorted(ambiguous)} are claimed by BOTH sides, so "
            f"this fixture now exercises the 'ambiguity is omitted, never "
            f"guessed' rule and those identities are in NO forbidden set. "
            f"That is a deliberate omission when it costs nothing and a "
            f"blind spot when it does not — decide which, do not delete "
            f"this assertion.")
        forbidden, permits = {}, {}
        # PER (route, PATH), not per route (#427 round 9): the same route
        # read for a DIFFERENT game is a different question, and answering
        # it with one number is exactly what made an official's standing in
        # a game they do not referee invisible.
        places = {(key[1], key[2]) for key in sweep.rows}
        for principal in PRINCIPALS:
            for route, path in places:
                subjects = sweep.subject_of.get((route, path), {})
                permitted = self._permitted_ids(
                    fx, principal, route, subjects, private, submitted)
                permits[(principal, route, path)] = self._tokens_of(
                    fx, permitted, index)
                forbidden[(principal, route, path)] = (
                    alphabet - permits[(principal, route, path)])
        # The premise assertions below are about a principal's standing on a
        # route, so they are read at the path whose SUBJECT is the game the
        # fixture built those claims around.
        assigned = {route: path for route, path in places
                    if sweep.subject_of.get((route, path), {}).get("game_id")
                    == fx["gid"]}

        def at(principal, route):
            return forbidden[(principal, route, assigned[route])]
        # THE PREMISES. Each one is a way this oracle could be vacuous, and
        # each is asserted rather than assumed.
        #
        # (1) somebody must have something real to fail to reach.
        self.assertTrue(
            at("thirdcoach", "get_games_id_board"),
            f"[{label}] no private identities exist, so the identity oracle "
            f"is vacuous")
        # (2) the official's ROUTE narrowing is real.
        self.assertTrue(
            at("official", "get_games_id_officials"),
            f"[{label}] the official is forbidden no identity on any "
            f"non-assigned-game route, so the route-specific entitlement is "
            f"vacuous for exactly the principal it was introduced for")
        # (3) D3: the forbidden set is STRICTLY WIDER than durable
        #     attribution, and the extra identities really are forbidden to
        #     a principal entitled to neither side. Without this, reverting
        #     `_private_side_ids` to `_durable_ids` would pass silently.
        durable = self._durable_ids(fx, fx["home"]) | self._durable_ids(
            fx, fx["away"])
        candidates = self._tokens_of(fx, everything - durable, index)
        self.assertTrue(
            candidates,
            f"[{label}] every private identity is durably attributed, so "
            f"the candidate pool is not being exercised and D3's widening "
            f"is vacuous on this fixture")
        blind_to = at("thirdcoach", "get_games_id_board")
        self.assertLessEqual(
            candidates, blind_to,
            f"[{label}] {sorted(candidates - blind_to)} are served "
            f"candidate identities that a coach of NEITHER team is still "
            f"permitted to receive")
        # (4) the official's DATA-CLASS narrowing is real for identities
        #     too: a candidate is not on the sheet, so it stays forbidden to
        #     them on the three routes their grant covers.
        # Read at the FIRST GAME's sheet, which is the game the fixture's
        # official is assigned to and the one `at()` above reads.
        unsubmitted = self._tokens_of(
            fx, private[fx["home"]] - submitted[(fx["gid"], fx["home"])],
            index)
        self.assertTrue(
            unsubmitted,
            f"[{label}] every HOME private identity occupies a slot, so the "
            f"official's submitted-lineup identity narrowing is vacuous")
        self.assertLessEqual(
            unsubmitted, at("official", "get_games_id_board"),
            f"[{label}] the official is permitted HOME identities that do "
            f"not occupy a slot on the sheet they are entitled to")
        # (5) D2: the guardian's grant is the JUNIOR'S ROW, so the rest of
        #     the junior's side stays forbidden on the guardian's own
        #     routes.
        junior = fx["guardian_junior_id"]
        rest_of_the_side = self._tokens_of(
            fx, private[fx["away"]] - {junior}, index)
        junior_tokens = self._tokens_of(fx, {junior}, index)
        self.assertTrue(
            rest_of_the_side,
            f"[{label}] the junior is the only private identity on their "
            f"side, so 'the junior's row, not the whole side' is vacuous")
        # THE GRANT'S PREMISE HOLDS ONLY WHILE THE LINK DOES (#427 round 9).
        # In a REVOKED world the guardian has no grant at all, so "the junior
        # is never forbidden to the guardian" is false — correctly, and that
        # is the closure working rather than a premise failing. Both
        # directions are asserted, so neither world passes vacuously.
        for route in sorted(GUARDIAN_JUNIOR_ROUTES):
            for path in sorted(p for r, p in places if r == route):
                # THE GRANT IS PER PATH, NOT PER PRINCIPAL (#427 round 11,
                # F1): the junior route is now bound to a junior this
                # guardian is NOT linked to as well as to their own, so
                # "linked" is a fact about THIS path's subjects.
                linked = bool(self._grant_rows(
                    fx, GUARDIAN_OF_A_JUNIOR, "guardian",
                    sweep.subject_of.get((route, path), {})))
                self.assertLessEqual(
                    rest_of_the_side, forbidden[("guardian", route, path)],
                    f"[{label}] on {route} the guardian is permitted "
                    f"identities of the junior's side other than the junior "
                    f"— that is the standing whole-side grant the class "
                    f"comment says it is not")
                if linked:
                    self.assertEqual(
                        frozenset(),
                        junior_tokens & forbidden[("guardian", route, path)],
                        f"[{label}] a VERIFIED guardian is forbidden their "
                        f"own junior on {route}, so the grant is dead")
                else:
                    self.assertLessEqual(
                        junior_tokens, forbidden[("guardian", route, path)],
                        f"[{label}] no GuardianLink grants this guardian "
                        f"this read — their own link is unverified, or this "
                        f"path names a junior they hold no link to — and "
                        f"they are STILL permitted the junior on {route} "
                        f"({path}): a grant that outlived, or reached past, "
                        f"the relationship it was granted for")
        # ONE COMPILED ALTERNATION PER PLACE, and redaction only by the
        # tokens that can actually shadow another. Both are performance, not
        # semantics: `search` over an alternation answers the same question
        # as a search per token, and a permitted token that contains no other
        # token cannot create the false positive redaction exists to prevent.
        shadowing = self._shadowing(alphabet)
        matchers, shadows = {}, {}
        for place, tokens in forbidden.items():
            matchers[place] = re.compile("|".join(
                rf"\b{re.escape(t)}\b" for t in sorted(tokens))) \
                if tokens else None
            shadows[place] = permits[place] & shadowing
        for (principal, route, path, hint), (_status, body) in sweep.rows.items():
            place = (principal, route, path)
            matcher = matchers[place]
            if matcher is None:
                continue
            blob = json.dumps(body, sort_keys=True, default=str)
            if shadows[place]:
                blob = self._redact(blob, shadows[place])
            hit = matcher.search(blob)
            self.assertIsNone(
                hit,
                f"[{label}] {principal} received "
                f"{hit.group(0) if hit else ''!r} — a way to name a person "
                f"private to a side they are not permitted on this read — "
                f"from GET {path} (hint={hint}, route={route}, "
                f"subject={sweep.subject_of.get((route, path))})")

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

    @contextlib.contextmanager
    def _revoked(self, fx, kind):
        """Withdraw ONE principal's relationship to the FIRST game, and
        nothing else — and assert the withdrawal really landed in the store,
        so "they see nothing" is not a statement about a write that failed.

        ``official_assignment``
            ``ApiService.unassign_official`` on the assignment that admits
            the swept official to ``fx["gid"]``. They remain a real,
            signed-in Official with a real ``official_id``; what changes is
            that this game is no longer theirs. The row is DELETED.

        ``official_assignment_declined``
            THE SHARPER SUBJECT, and the one F2 was about (#427 round 11).
            The official DECLINES through the product's own write path,
            ``ApiService.respond_assignment(accept=False)``. The row SURVIVES
            and still names this official and this game — every dimension of
            it is unchanged except ``status``, which the product's own
            ``OfficialAssignmentStatus.is_active`` declares grants nothing.
            That is the official's exact analogue of the guardian's
            ``verified=False`` below, and until this round nothing drove it:
            the official half deleted the row while the guardian half used
            the product's softer state, so the one state where the two
            halves could disagree was never entered.

        ``season_roster_membership``
            THE ROW #205 EXISTS TO MAKE AUTHORITATIVE (#427 round 12, LB1).
            The swept HOME Player's ``SeasonRosterMembership`` is driven to
            ``inactive`` through ``SetupService
            .set_season_roster_membership_status`` — the product's own write
            path, so what the sweep observes afterwards is a real state of
            the system. The row SURVIVES and still names this player, this
            team and this LeagueSeason; only ``status`` moves, and the
            product's own ``_ELIGIBLE_MEMBERSHIP_STATUSES`` says it now
            grants nothing. That is the guardian's ``verified=False`` and
            the official's ``DECLINED``, on the third row — and until this
            round NOTHING in this matrix ever moved a membership at all, so
            the state the whole of #205 turns on appeared in no world.

        ``guardian_link``
            The SWEPT GUARDIAN'S OWN ``GuardianLink`` loses ``verified``. The
            link row survives — an UNVERIFIED link is the product's own
            "grants nothing" state, which is a sharper subject than deleting
            the row would be, because the guardian still resolves to the
            junior.

            AND THE SECOND GUARDIAN'S LINK TO THE SAME JUNIOR IS LEFT
            VERIFIED (#427 round 11, F3), which is what makes this world
            sharp: the store still holds a verified link FOR THAT JUNIOR, so
            an oracle keyed on the junior alone answers "verified" and hands
            the swept guardian a grant built out of somebody else's row. It
            used to un-verify every link the junior had, which hid exactly
            that.
        """
        api = fx["api"]
        assert kind in RELATIONSHIP_REVOCATIONS, kind
        gid = self._subject_of(fx, fx["gid"])
        if kind == "season_roster_membership":
            principal = RELATIONSHIP_REVOCATIONS[kind]
            player = api.store.get_player(fx["scopes"][principal]["player_id"])
            context = RosterService(api.store).resolve_membership_context(
                api.store.get_game(fx["gid"]), player)
            assert context is not None, (
                f"{principal} holds no membership context for the first "
                f"game, so there is no participation to withdraw")
            row = context.membership
            assert self._grant_rows(fx, PLAYER_SCOPED_BY_MEMBERSHIP,
                                    principal, gid)
            # THE PRODUCT'S OWN WRITE PATH, never a field write: condition 4
            # says "ex-member", which means a membership the product itself
            # made ineligible.
            after = api.setup.set_season_roster_membership_status(
                row.id, MembershipStatus.INACTIVE.value, actor_id=ADMIN)
            # THE PREMISE, in the guardian's shape: the row is still there,
            # still names this player, this team and this LeagueSeason, and
            # the PRODUCT says it participates in nothing.
            assert after.id == row.id and after.player_id == row.player_id
            assert after.team_id == row.team_id
            assert after.league_season_id == row.league_season_id
            assert after.status not in \
                RosterService._ELIGIBLE_MEMBERSHIP_STATUSES, after.status
            assert not self._grant_rows(fx, PLAYER_SCOPED_BY_MEMBERSHIP,
                                        principal, gid), (
                "revoking the membership left the ex-member a grant row")
            try:
                yield
            finally:
                back = api.setup.set_season_roster_membership_status(
                    row.id, MembershipStatus.ACTIVE.value, actor_id=ADMIN)
                assert back.status == MembershipStatus.ACTIVE, back
                assert self._grant_rows(fx, PLAYER_SCOPED_BY_MEMBERSHIP,
                                        principal, gid)
            return
        if kind in ("official_assignment", "official_assignment_declined"):
            rows = [a for a in api.store.assignments_for_game(fx["gid"])
                    if a.official_id == fx["official_id"]
                    and a.status.is_active]
            assert rows, ("the swept official holds no active assignment to "
                          "the first game, so revoking it revokes nothing")
            assert self._official_is_assigned(fx, gid)
            for row in rows:
                if kind == "official_assignment":
                    out = api.unassign_official(row.id, actor_id=ADMIN)
                else:
                    out = api.respond_assignment(row.id, accept=False,
                                                 actor_id=ADMIN)
                assert "error" not in out, out
            if kind == "official_assignment_declined":
                # THE PREMISE: the row is still there, still names this
                # official and this game, and the PRODUCT says it is dead.
                for row in rows:
                    still = api.store.get_official_assignment(row.id)
                    assert still is not None, "respond_assignment deleted it"
                    assert still.game_id == row.game_id
                    assert still.official_id == row.official_id
                    assert not still.status.is_active, still.status
            assert not self._official_is_assigned(fx, gid), (
                f"revoking {kind} left the swept official an ACTIVE "
                f"assignment to the first game")
            try:
                yield
            finally:
                for row in rows:
                    if kind == "official_assignment_declined":
                        # Clear the dead row before re-offering: leaving it
                        # would make the next base world hold two rows for
                        # one (official, game) pair, which is a fixture this
                        # sweep never otherwise measures.
                        out = api.unassign_official(row.id, actor_id=ADMIN)
                        assert "error" not in out, out
                back = api.assign_official(fx["gid"], fx["official_id"],
                                           "referee", actor_id=ADMIN)
                assert "error" not in back, back
                assert self._official_is_assigned(fx, gid)
            return
        swept = fx["account_ids"]["guardian"]
        links = [x for x in api.store.guardian_links_for_player(
            fx["guardian_junior_id"])
            if x.verified and x.guardian_user_id == swept]
        assert links, ("the swept guardian holds no verified link, so "
                       "revoking it revokes nothing")
        for link in links:
            api.store.save_guardian_link(
                dataclasses.replace(link, verified=False))
        # THE PREMISE THAT MAKES THIS WORLD SHARP: the junior still HAS a
        # verified guardian — just not this one.
        assert api.guardians.is_verified_guardian(
            fx["guardian2_account_id"], fx["guardian_junior_id"]), (
                "the second guardian's link was revoked too, so this world "
                "cannot show a grant built out of another guardian's row")
        assert not self._grant_rows(fx, GUARDIAN_OF_A_JUNIOR, "guardian", {})
        try:
            yield
        finally:
            for link in links:
                api.store.save_guardian_link(link)
            assert self._grant_rows(fx, GUARDIAN_OF_A_JUNIOR, "guardian", {})

    def _assert_relationship_loss_is_observed(self, base, world, fx, kind,
                                              label):
        """THE PREMISE of a revoked world: the principal whose relationship
        was withdrawn MUST answer differently somewhere.

        Without this a revocation that silently failed to reach the server —
        a cached session, a write that rolled back, a route that never asked
        — would leave the two worlds identical and every "they received
        nothing they may not have" below would be a statement about a world
        that never changed."""
        principal = RELATIONSHIP_REVOCATIONS[kind]
        moved = sorted({(key[1], key[2]) for key in base.diff(world)
                        if key[0] == principal})
        self.assertTrue(
            moved,
            f"[{label}] revoking {kind} changed NOTHING {principal} reads, "
            f"so the withdrawal never reached the read surface and this "
            f"world cannot show that a lost relationship costs a grant")
        return moved

    def _assert_non_interference(self, base, perturbed, fx, team_id, label,
                                 data_class, game_id):
        """ORACLE 2. Every diff must belong to a caller ENTITLED to the side
        that was perturbed, IN THE GAME that was perturbed, ON THAT ROUTE,
        for THAT KIND of private state. Anything else is a response that is a
        function of that side's private state.

        ``data_class`` is what :meth:`_perturbed` actually moved, asserted
        there rather than assumed here. It is the dimension blocker 2 was
        missing: the official's grant is real on three routes for the
        submitted lineup, and is nothing at all for availability, substitute,
        candidate, audit or backed-out roster history — so under a
        ``TEAM_WORKFLOW_DATA`` perturbation an official diff is an offender
        exactly like anyone else's.

        ``game_id`` is WHICH GAME :meth:`_perturbed` moved it in, and it is
        D10 (#427 round 10, owner comment 5432572444): without it a response
        ABOUT the official's assigned game could vary with ANOTHER game's
        submitted state and never be an offender, because the perturbed TEAM
        alone is identical in both games of this fixture. It is a REQUIRED
        argument rather than an optional one so a caller cannot inherit the
        old blindness by omission. See :meth:`_grant_spans`."""
        with self._frozen_relationships():
            self._non_interference(base, perturbed, fx, team_id, label,
                                   data_class, game_id)

    def _non_interference(self, base, perturbed, fx, team_id, label,
                          data_class, game_id):
        offenders, entitled_moved = [], set()
        for key in base.diff(perturbed):
            principal, route, path = key[0], key[1], key[2]
            subjects = base.subject_of.get((route, path), {})
            if team_id in self._entitled_teams(
                    fx, principal, route, subjects, data_class,
                    perturbed_game=game_id):
                entitled_moved.add(principal)
                continue
            offenders.append((key, base.rows[key], perturbed.rows[key]))
        self.assertEqual(
            [], [o[0] for o in offenders],
            f"[{label}] PRIVATE STATE OF {team_id} IN {game_id} REACHED A "
            f"CALLER NOT ENTITLED TO IT. Each row below is (principal, "
            f"route, path, hint) whose response changed when ONLY "
            f"{team_id}'s private per-side state in {game_id} changed, so "
            f"that response is a function of it:\n"
            + "\n".join(
                f"  {key}\n     before: {json.dumps(b, sort_keys=True, default=str)[:400]}"
                f"\n     after:  {json.dumps(a, sort_keys=True, default=str)[:400]}"
                for key, b, a in offenders[:8]))
        # THE PREMISE, again as an assertion: somebody entitled to that side
        # MUST have moved, or the perturbation never reached the surface and
        # "nobody else moved" proves nothing.
        self.assertTrue(
            entitled_moved,
            f"[{label}] perturbing {team_id} in {game_id} changed NO "
            f"response at all, so this world is indistinguishable from the "
            f"base one and the non-interference assertion is vacuous")

    # -- hint invariance ---------------------------------------------------
    def _assert_hints_are_inert(self, sweep, fx, label):
        """Nothing the client can SAY widens what a caller reads — asserted
        in EVERY world, which is what lets the two-world diff be read on the
        whole hint matrix rather than only on the un-hinted variant.

        THE VARIANTS ARE DERIVED, NOT LISTED (#427 round 9, D7). Every
        parameter this server reads is probed, because
        :data:`QUERY_PARAMETER_PROBES` is checked against
        :func:`route_extract.query_parameter_names` — so this assertion
        covers the query-string surface the way the route inventory covers
        the route surface. The claim it can support is therefore "no
        parameter THIS SERVER READS, and no unread name we control for,
        selects a side for a caller not typed as an unscoped operator" —
        which is narrower than the sentence that used to stand here, and is
        true.

        The exceptions are the (route, parameter) pairs in
        :data:`HINT_MAY_SELECT_FOR_AN_UNSCOPED_OPERATOR`, and ONLY for a
        principal typed into :data:`UNSCOPED_OPERATOR_CLASSES` — a caller
        who may already read everything the parameter could select, so
        selecting widens nothing. Every other principal, every other route
        and every other parameter must answer identically with and without
        the hint."""
        entitlement = self._entitlement(fx)
        for (principal, route, path, hint), value in sweep.rows.items():
            if hint == "none":
                continue
            param = hint.split("=", 1)[0]
            if ((route, param) in HINT_MAY_SELECT_FOR_AN_UNSCOPED_OPERATOR
                    and entitlement[principal][0]
                    in UNSCOPED_OPERATOR_CLASSES):
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
    """THE ASSET. Twenty-four worlds, all three oracles, both perturbation
    directions, every authenticated GET route, ten principals, four hint
    variants, three backends.

    THESE NUMBERS ARE MEASURED, AND THAT IS NOT A FORMALITY. The sentence
    this replaces read "Three worlds ... eight principals" and survived the
    round whose own docstring narrates correcting the SAME defect one screen
    above (:data:`_SweepHarness` RUNTIME). A count here that drifts from the
    matrix is the file's own thesis failing on the file itself, so re-measure
    rather than reason when either constant moves."""

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
        for kind in PERTURBED_GAMES[game]] + [
        f"revoking_{kind}_costs_the_grant_it_carried"
        for kind in sorted(RELATIONSHIP_REVOCATIONS)]

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
                                data_class, fx[game_key])
                            self._assert_no_foreign_ids(
                                world, fx, f"{tag}/perturbed")
                        total += base.elapsed + world.elapsed
                        phases += 2
                    # THE SUBJECT AXIS, as a WORLD rather than as a
                    # lookup: withdraw a principal's relationship to the
                    # game and re-measure. Entitlement is derived from the
                    # store's own relationship rows, so nothing here has to
                    # tell the oracles what changed — they recompute it.
                    for kind in sorted(RELATIONSHIP_REVOCATIONS):
                        tag = f"{label}/revoked/{kind}"
                        base = self._sweep(who, fx, specs, subjects)
                        self._assert_no_foreign_ids(base, fx, f"{tag}/base")
                        with self._revoked(fx, kind):
                            world = self._sweep(who, fx, specs, subjects)
                            self._assert_relationship_loss_is_observed(
                                base, world, fx, kind, tag)
                            self._assert_no_foreign_ids(
                                world, fx, f"{tag}/revoked")
                            self._assert_hints_are_inert(
                                world, fx, f"{tag}/revoked")
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

                # EACH PRINCIPAL IS BOUND TO ITS CLASS BY AN ASSERTION
                # (#427 round 9). Everything below constrained the class's
                # SHAPE — "the class is one of the seven", "an
                # IN_NEITHER_SIDE row carries no team" — and nothing said
                # WHICH class each principal carries. So the four typed
                # classes that exist to keep this sweep honest could each
                # be folded into a neighbour with the whole file staying
                # green: `arena` retyped OPERATOR_UNSCOPED_BY_DESIGN,
                # `viewer` retyped IN_NEITHER_SIDE, `official` or `guardian`
                # widened to an operator. UNSCOPED_OPERATOR_WITHOUT_ROSTER_
                # AUTHORITY's own docstring says it may be folded away only
                # if the two roles measure identically —
                # `TheArenaManagerIsAnOperatorWithoutRosterAuthority`
                # measures that they do NOT — and until now nothing
                # connected that measurement to the class the sweep
                # actually uses.
                for principal, expected in (
                        ("official", OFFICIAL_SUBMITTED_LINEUP_ONLY),
                        ("guardian", GUARDIAN_OF_A_JUNIOR),
                        ("arena", UNSCOPED_OPERATOR_WITHOUT_ROSTER_AUTHORITY),
                        ("operator", OPERATOR_UNSCOPED_BY_DESIGN),
                        ("viewer", VIEWER_ENTITLED_TO_NOTHING),
                        ("thirdcoach", IN_NEITHER_SIDE),
                        ("homecoach", COACH_SCOPED_TO_ONE_SIDE),
                        ("awaycoach", COACH_SCOPED_TO_ONE_SIDE),
                        # SEPARATE AUTHORIZATION BRANCHES (#427 round 12,
                        # LB1). A Coach's side is their account scope and a
                        # Player's is an eligible SeasonRosterMembership;
                        # one class for both is what left that row outside
                        # every grant map in this file.
                        ("homeplayer", PLAYER_SCOPED_BY_MEMBERSHIP),
                        ("awayplayer", PLAYER_SCOPED_BY_MEMBERSHIP)):
                    self.assertEqual(
                        entitlement[principal][0], expected,
                        f"[{label}] {principal} is typed "
                        f"{entitlement[principal][0]!r}, not {expected!r}. "
                        f"Each of these bindings is a claim about the "
                        f"product with its own dedicated test elsewhere in "
                        f"this file; retyping a principal silently moves it "
                        f"under a different rule and detaches it from that "
                        f"test.")
                # …and every class the module defines is actually CARRIED by
                # somebody, so a class cannot rot into a dead constant whose
                # dedicated test proves nothing about the sweep.
                self.assertEqual(
                    {COACH_SCOPED_TO_ONE_SIDE, PLAYER_SCOPED_BY_MEMBERSHIP,
                     IN_NEITHER_SIDE,
                     GUARDIAN_OF_A_JUNIOR, OFFICIAL_SUBMITTED_LINEUP_ONLY,
                     OPERATOR_UNSCOPED_BY_DESIGN,
                     UNSCOPED_OPERATOR_WITHOUT_ROSTER_AUTHORITY,
                     VIEWER_ENTITLED_TO_NOTHING},
                    {klass for klass, _teams in entitlement.values()},
                    f"[{label}] a typed design classification is carried by "
                    f"no swept principal, so its dedicated test is measuring "
                    f"a rule this sweep never applies")

                for principal, (klass, teams) in entitlement.items():
                    with self.subTest(principal=principal):
                        self.assertIn(klass, (
                            COACH_SCOPED_TO_ONE_SIDE,
                            PLAYER_SCOPED_BY_MEMBERSHIP, IN_NEITHER_SIDE,
                            GUARDIAN_OF_A_JUNIOR,
                            OFFICIAL_SUBMITTED_LINEUP_ONLY,
                            OPERATOR_UNSCOPED_BY_DESIGN,
                            UNSCOPED_OPERATOR_WITHOUT_ROSTER_AUTHORITY,
                            VIEWER_ENTITLED_TO_NOTHING))
                        if klass in (COACH_SCOPED_TO_ONE_SIDE,
                                     PLAYER_SCOPED_BY_MEMBERSHIP):
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
                #
                # THE LEAF SET IS DERIVED (#427 round 17). This named FIVE
                # refusals and THREE admissions by hand, out of a family of
                # TEN — so an eleventh leaf would have been asserted for the
                # official by nobody, and two existing ones already were.
                # Every leaf of the derived family is now decided, and each
                # 200 is classified into one of the two typed sets rather
                # than left out of both.
                family = self._private_game_family()
                for name, declared in (
                        ("OFFICIAL_ASSIGNED_GAME_ROUTES",
                         OFFICIAL_ASSIGNED_GAME_ROUTES),
                        ("OFFICIAL_ADMITTED_BEYOND_THE_SHEET",
                         OFFICIAL_ADMITTED_BEYOND_THE_SHEET)):
                    self.assertLessEqual(
                        declared, set(family),
                        f"[{label}] {name} names a route that is not a leaf "
                        f"of the private-game family, so what it describes "
                        f"about the official is a claim about a surface "
                        f"that no longer exists")
                self.assertEqual(
                    frozenset(),
                    OFFICIAL_ASSIGNED_GAME_ROUTES
                    & OFFICIAL_ADMITTED_BEYOND_THE_SHEET,
                    "a leaf claims both that it grants the official a side "
                    "and that it grants them none")
                admitted = (OFFICIAL_ASSIGNED_GAME_ROUTES
                            | OFFICIAL_ADMITTED_BEYOND_THE_SHEET)
                official = {
                    name: self._req(
                        who["official"], "GET",
                        self._path_of(spec, (fx["gid"],)))[0]
                    for name, spec in sorted(family.items())}
                self.assertEqual(
                    {name: (200 if name in admitted else REFUSED)
                     for name in family}, official,
                    f"[{label}] the assigned official's leaf-by-leaf matrix "
                    f"moved. They are REFUSED every own-side leaf and "
                    f"admitted to exactly "
                    f"{sorted(OFFICIAL_ASSIGNED_GAME_ROUTES)} with the "
                    f"submitted-lineup projection plus "
                    f"{sorted(OFFICIAL_ADMITTED_BEYOND_THE_SHEET)}, which "
                    f"carry no side's private roster state at all. A leaf "
                    f"that has moved either way has to be re-classified "
                    f"into one of those two sets or the "
                    f"{OFFICIAL_SUBMITTED_LINEUP_ONLY} classification is no "
                    f"longer sound.")

                # HINT_MAY_SELECT_FOR_AN_UNSCOPED_OPERATOR: the exception is
                # reachable ONLY by a principal in
                # UNSCOPED_OPERATOR_CLASSES. The official is the other
                # both-sided principal and is refused all three of those
                # routes outright (asserted just above), and every scoped
                # principal's hint is proven inert by the sweep itself, on
                # these routes like every other.
                for route, leaf in (
                        ("get_games_id_availability_summary",
                         "availability-summary"),
                        ("get_games_id_substitute_candidates",
                         "substitute-candidates"),
                        ("get_games_id_substitute_addable",
                         "substitute-addable")):
                    self.assertIn(
                        (route, "team_id"),
                        HINT_MAY_SELECT_FOR_AN_UNSCOPED_OPERATOR)
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

                # EVERY HINT EXEMPTION IS SOUND ONLY WHILE ITS ROUTE'S AUTH
                # IS WHAT IT WAS CLASSIFIED AGAINST, so the condition is
                # checked against the registry's recorded auth rather than
                # taken on trust — the rule `get_players` already carried,
                # now applied to all six pairs.
                by_name = {spec.name: spec for spec in route_registry.REGISTRY}
                for (name, param), recorded in sorted(
                        HINT_MAY_SELECT_FOR_AN_UNSCOPED_OPERATOR.items()):
                    self.assertIn(name, by_name, name)
                    self.assertIn(
                        param, QUERY_PARAMETER_PROBES,
                        f"({name}, {param}) is exempt from hint-inertness "
                        f"for a parameter this sweep does not probe, so the "
                        f"exemption covers nothing that is measured")
                    self.assertEqual(
                        by_name[name].auth, recorded,
                        f"({name}, {param}) is exempt from the "
                        f"hint-inertness assertion for an unscoped operator, "
                        f"classified against auth={recorded!r}, but the "
                        f"registry now records auth="
                        f"{by_name[name].auth!r}. Loosening that route's auth "
                        f"widens who may hint with it, so the exemption must "
                        f"be re-decided rather than inherited.")
                # …and the operator-only ones really are refused everyone
                # else, so the exemption cannot cover a caller who should
                # never have reached the route at all.
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
                        f"this sweep exempts")

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
                # the class hand out a whole side on all 50 swept routes. The
                # official got a per-leaf 403/200 matrix; so does this one.
                #
                # The guardian differs from the official in the direction
                # that matters: the official is ADMITTED to three leaves of
                # the private-game family with a projection, the guardian is
                # REFUSED ALL TEN outright.
                #
                # DERIVED, AND ON BOTH GAMES (#427 round 17). The ten were
                # spelled by hand and driven on `gid` alone. The guardian is
                # NOT a member of the empty-entitlement matrix in section 6b
                # — their entitled-team set is their junior's resolved side
                # and is NOT empty — so this is where their confinement is
                # asserted, and it has to fail closed on a new leaf by
                # itself. MEASURED at #427 round 17: 403 on all ten leaves
                # of BOTH games, so the second game was not a gap either,
                # merely an unasserted one.
                guardian = self._family_status_matrix(
                    who, fx, self._private_game_family(), ["guardian"])
                self.assertEqual(
                    [], self._admissions(guardian),
                    f"[{label}] a guardian was ADMITTED to a leaf of the "
                    f"private-game family. A guardian speaks for ONE junior; "
                    f"the {GUARDIAN_OF_A_JUNIOR} classification confines "
                    f"them to {sorted(GUARDIAN_JUNIOR_ROUTES)} and no leaf "
                    f"of this family is one of them: "
                    + str(self._admissions(guardian)))
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

    def test_each_scoped_principals_side_is_the_same_in_both_games(self):
        """THE STATED LIMIT OF :meth:`_entitlement`, MEASURED (#427 round 11).

        ``_entitlement`` returns ONE side per principal, for every game. For
        a Coach that is exact — a Coach's team is permanently bound, and
        ``game_scoped_own_team_id``'s own docstring records that there is no
        season-scoped Coach model in this codebase at all. For a PLAYER it is
        not exact in general: a Player's side is resolved from their
        GAME-SCOPED MEMBERSHIP, so a mid-season transfer can put the same
        Player on different sides of two games, and a single number would
        then be right once and wrong once.

        IT IS EXACT ON THIS FIXTURE, and this is the measurement that says so
        rather than a sentence claiming it: both games hang off the SAME
        LeagueSeason, so one membership resolves both. Asked of the
        PRODUCT'S OWN resolver per game — which is a PIN, not the oracle
        reading the gate: ``_entitlement`` keeps its independent constant and
        this test fails if the product ever disagrees with it.

        The day a fixture puts the two games on different LeagueSeasons, or a
        Player's membership moves between them, this fails with the two
        answers in the message — and the fix is to make ``_entitlement``
        subject-aware, not to relax this."""
        store = InMemoryStore()
        try:
            fx = self._fixture(store)
            self._serve(fx)
            api = fx["api"]
            g1, g2 = (api.store.get_game(fx["gid"]),
                      api.store.get_game(fx["gid2"]))
            self.assertEqual(
                g1.season_id, g2.season_id,
                "the two games no longer share a Season, so one membership "
                "no longer resolves both and a per-principal side constant "
                "is not safe")
            for principal, (klass, teams) in sorted(
                    self._entitlement(fx).items()):
                if klass not in (COACH_SCOPED_TO_ONE_SIDE,
                                 PLAYER_SCOPED_BY_MEMBERSHIP):
                    continue
                scope = fx["scopes"][principal]
                with self.subTest(principal=principal):
                    # PROJECTED HERE THE SAME WAY THE GATE PROJECTS (#427
                    # round 20): the resolver takes immutable ids, never the
                    # session mapping. `fx["scopes"]` is read back off the
                    # product's own stored account, so this is the session's
                    # value and not one this test made up.
                    resolved = {
                        game.id: game_side_scope.game_scoped_own_team_id(
                            PRINCIPAL_ROLES[principal],
                            scope.get("team_id"), scope.get("player_id"),
                            game, api.store)
                        for game in (g1, g2)}
                    self.assertEqual(
                        {g1.id: sorted(teams)[0], g2.id: sorted(teams)[0]},
                        resolved,
                        f"{principal}'s side, resolved by the product per "
                        f"game, is {resolved} — but `_entitlement` states "
                        f"the single value {sorted(teams)} for both. A "
                        f"principal whose side DIFFERS between the two games "
                        f"cannot be described by one number, and every "
                        f"oracle reading that number is wrong for one of "
                        f"them.")
        finally:
            store.clear_all_data()

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

    def _offenders(self, base, world, fx, team, label, data_class, game_id):
        """``_assert_non_interference``'s failure text, or ``None`` when it
        passed. The sweep's OWN oracle — not a re-implementation of it."""
        try:
            self._assert_non_interference(base, world, fx, team, label,
                                          data_class, game_id)
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
                            TEAM_WORKFLOW_DATA, fx["gid"])
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

                        def blind(_fx, principal, _route, _subject,
                                  _data_class, perturbed_game=None,
                                  _widest=widest):
                            return _widest[principal][1]

                        real = self._entitled_teams
                        self._entitled_teams = blind
                        try:
                            still_green = self._offenders(
                                base, world, fx, fx["home"],
                                f"{label}/blind", TEAM_WORKFLOW_DATA,
                                fx["gid"])
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
                                f"{method}/{kind}", PERTURBATIONS[kind],
                                fx["gid"])
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

    def _offenders(self, base, world, fx, team, label, data_class, game_id):
        """``_assert_non_interference``'s failure text, or ``None`` when it
        passed. The sweep's OWN oracle — not a re-implementation of it."""
        try:
            self._assert_non_interference(base, world, fx, team, label,
                                          data_class, game_id)
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
                            f"{label}/guardian-probe", TEAM_WORKFLOW_DATA,
                            fx["gid"])
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

                        def blind(_fx, principal, _route, _subject,
                                  _data_class, perturbed_game=None,
                                  _widest=widest):
                            return _widest[principal][1]

                        real = self._entitled_teams
                        self._entitled_teams = blind
                        try:
                            still_green = self._offenders(
                                base, world, fx, fx["away"],
                                f"{label}/blind", TEAM_WORKFLOW_DATA,
                                fx["gid"])
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

                def candidate(_fx, principal, route, subject, data_class,
                              perturbed_game=None, _v=variant, _real=real):
                    klass, teams = self._entitlement(_fx)[principal]
                    if klass != GUARDIAN_OF_A_JUNIOR:
                        return _real(_fx, principal, route, subject,
                                     data_class,
                                     perturbed_game=perturbed_game)
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
                            TEAM_WORKFLOW_DATA, fx["gid"]) is None
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
                                f"{label}/falsified", TEAM_WORKFLOW_DATA,
                                fx["gid"])
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
                # THE KEY SET IS DERIVED (#427 round 17). The values below
                # are measurements and stay written down; WHICH leaves must
                # be measured is not this table's decision to make, so an
                # eleventh leaf of the family is an error naming it rather
                # than a row nobody thought to add for the two operators.
                self.assertEqual(
                    sorted(self._leaf_word(spec) for spec
                           in self._private_game_family().values()),
                    sorted(self.MATRIX),
                    f"[{label}] this matrix and the private-game family "
                    f"derived from route_registry.REGISTRY disagree about "
                    f"which leaves exist; measure the new one for both "
                    f"operators rather than leaving it unclassified")
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
        assuming it: the value of the row is that it is now watched.

        THE LEAVES ARE DERIVED AND BOTH GAMES ARE DRIVEN (#427 round 17).
        This spelled the ten leaves by hand and drove only ``gid``. The
        generalisation of it — every principal ``_entitlement`` gives an
        empty entitled-team set, not the viewer alone — is
        :class:`NoPrincipalEntitledToNoSideIsAdmittedToTheFamily`; this stays
        as :data:`VIEWER_ENTITLED_TO_NOTHING`'s own dedicated assertion, in
        the shape every other typed class in this file has, and now shares
        that section's derivation instead of restating it."""
        for label, store in self._stores():
            try:
                self._assert_backend(label, store)
                store.clear_all_data()
                fx = self._fixture(store)
                who = self._serve(fx)
                family = self._private_game_family()
                measured = self._family_status_matrix(
                    who, fx, family, ["viewer"])
                self.assertEqual(
                    [], self._admissions(measured),
                    f"[{label}] a signed-in VIEWER was ADMITTED to a leaf of "
                    f"the private-game family. A viewer holds only "
                    f"Permission.VIEW and the "
                    f"{VIEWER_ENTITLED_TO_NOTHING} classification gives "
                    f"them no side of either game: "
                    + str(self._admissions(measured)))
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
# 6b. THE ADMISSION MATRIX: "WAS THIS CALLER ADMITTED AT ALL", DERIVED ON
#     BOTH AXES FROM AUTHORITIES OUTSIDE THIS SECTION.
#
# WHY A THIRD QUESTION EXISTS AT ALL (#427 round 17). Both data oracles ask
# "did private DATA reach a caller without entitlement". They are
# STRUCTURALLY BLIND to an ADMISSION THAT CARRIES LITTLE OR NO DATA, and that
# is not a hypothetical:
#
#   MEASURED at 6641fe7, one branch added to `resolve_private_game_read`
#   ahead of its COACH/PLAYER branch --
#
#       if role == Role.COACH:
#           if scope.get('team_id') not in (game.home_team_id,
#                                           game.away_team_id):
#               return PrivateGameRead(role=role, game=None,
#                                      own_team=None, admitted=True)
#
#   -- admits a Coach whose scope names NEITHER side. It fills NEITHER
#   grant-bearing field, so `needs_authority` is False and it takes the
#   not-found passthrough's own exemption: `_audit()` returns `[]`, the
#   delegation premise is unmoved, and THE WHOLE MODULE STAYS GREEN at
#   `Ran 91 tests in 199.2s ... OK` -- while `thirdcoach`, a Coach of a team
#   in NEITHER game, receives 200 on `/board`, `/lineups`, `/officials` and
#   `/reschedule` ON BOTH GAMES, carrying the officiating assignment's id and
#   name (`official_1`, "Ref Riley"). Both sides come back
#   `restricted: true, players: null`, which is exactly why every data oracle
#   in this file is silent: there is no foreign identity to find and nothing
#   that moves under a perturbation. The dispatch re-fetches the game by id,
#   so `game=None` costs the caller nothing.
#
# The question this section asks instead is answered by the RESPONSE STATUS,
# which no amount of payload trimming evades.
#
# AND BOTH ITS AXES ARE DERIVED, because a hand-enumerated axis is where the
# next leak hides and this section could repeat that mistake in two places:
#
#   CELLS       every `(principal, GAME SUBJECT)` whose entitled-team set,
#               NARROWED AGAINST THAT GAME, is `frozenset()` -- see
#               `_unentitled_cells`. NOT a tuple of names, and since #427
#               round 18 NOT a set of principals either.
#   LEAVES      `private_game_family()`, off `route_registry.REGISTRY`. A
#               leaf registered into the family acquires the assertion with
#               NO EDIT HERE, which `test_a_new_leaf_of_the_family_is
#               _asserted_with_no_edit_here` proves by registering one that
#               returns an EMPTY BODY -- the shape both data oracles are
#               blind to, caught here by its status alone.
#
# WHY THE PRINCIPAL AXIS BECAME A CELL AXIS (#427 round 18, blocker 1). It
# was `every principal whose entitled-team set is frozenset()`, and that set
# is `_entitlement`'s GLOBAL team union. So it drove `thirdcoach` and
# `viewer` and EXCLUDED THE SWEPT OFFICIAL, whose map says `{home, away}`
# although the real grant is ASSIGNMENT-KEYED PER GAME: an unassigned or
# revoked official could be admitted to a status-only leaf of a game they do
# not referee while both data oracles and this very matrix stayed green.
# MEASURED at `73faf4d` with the owner's own falsifier -- `200 {}` for that
# official on the UNASSIGNED `gid2`'s `/officials` and `/reschedule` -- the
# WHOLE MODULE passed, `Ran 95 tests in 297.042s ... OK`, tri-store.
#
# The replacement is not one predicate wider. It is `_subject_narrowed`, the
# same one definition both data oracles are read against, which asks the
# RELATIONSHIP ROWS -- `OfficialAssignment` with its `status.is_active`,
# `GuardianLink.verified`, the eligible `SeasonRosterMembership` statuses,
# the account scope -- and then intersects with THIS game's own two sides.
# A DECLINED or REVOKED row therefore grants nothing, which
# `test_a_dead_assignment_row_does_not_create_a_grant` drives through the
# product's own write paths.
#
# THREE AXES, AND THE THIRD IS STILL OPEN. The games this matrix is driven
# against are `_SweepHarness.GAME_KEYS`, still hand-enumerated and protected
# by nothing. Round 18 makes the CELL axis a function of it rather than
# independent of it; it does NOT close it, and this file does not claim it.
#
# WHY THIS IS NOT AN OVER-REFUSAL. The matrix covers exactly the cells the
# sweep's own relationship rows say may read NO side OF THAT GAME.
# Everybody else keeps their measured grant, asserted where it already was:
# the assigned official's three leaves and five refusals and the guardian's
# ten refusals in `TheDesignClassificationsAreStillTrue`, the two operators'
# leaf-by-leaf matrix in
# `TheArenaManagerIsAnOperatorWithoutRosterAuthority`. All three of those
# read their leaves from THIS derivation now, so a new family leaf is an
# error naming it for the entitled principals too. And the official's own
# refereed game is re-measured leaf by leaf, WITH ITS PRIVATE ROWS, inside
# both of round 18's new tests -- `_assert_the_official_keeps_their_own_game`
# -- because a round that widens a refusal rule is exactly the round that
# breaks a real grant.
#
# WHICH PRINCIPALS FALL OUT OF THE GENERALISATION, MEASURED. `thirdcoach`
# and `viewer` on both games, as before, plus the official on the game they
# do not referee. NOT the guardian and NOT the players: `GuardianLink`
# carries no `game_id` and this fixture's two games carry THE SAME TWO
# TEAMS, so their side is a side of both. Their confinement is a ROUTE fact
# and stays where it was asserted.
#
# WHAT THIS DOES NOT CLAIM. This is a BEHAVIOURAL assertion about the
# family's admissions; it is not a closure of the admission-branch axis and
# it does not make one. It catches a class of gate defect the static
# derivation cannot see -- one whose branch is exempt from needing an
# authority -- and the static models that remain open remain open.
# ---------------------------------------------------------------------------

#: A REGISTERED leaf of the private-game family, used ONLY to prove the leaf
#: axis fails closed. It ADMITS every signed-in caller and returns an EMPTY
#: BODY: no identity for oracle 1 to find, nothing for oracle 2 to move, so
#: the only thing that can report it is the status.
_FAMILY_PROBE_TEMPLATE = "/api/games/{}/round-seventeen-probe"
_FAMILY_PROBE_NAME = "get_games_id_round_seventeen_probe"
_FAMILY_PROBE_PATTERN = r"^/api/games/[^/]+/round-seventeen-probe$"
_FAMILY_PROBE_SPEC = route_registry.RouteSpec(
    "GET", _FAMILY_PROBE_PATTERN, _FAMILY_PROBE_TEMPLATE, _FAMILY_PROBE_NAME,
    "_dispatch_get", kind="route", auth="session", scope_axis="none",
    note="injected by test_authenticated_side_noninterference; never shipped")


@contextlib.contextmanager
def _a_registered_leaf_of_the_private_game_family():
    """An ELEVENTH leaf, registered for real.

    The spec goes into ``route_registry.REGISTRY`` and the branch into the
    real ``Handler._dispatch_get``, so the derivation discovers it exactly as
    it would discover a leaf somebody shipped — the same mechanism
    :func:`_a_registered_route_that_widens_only_for_the_official` uses, for
    the same reason: a faked route would prove something about the fake."""
    real_registry = route_registry.REGISTRY
    real_dispatch = srv.Handler._dispatch_get

    def dispatch(self):
        path = self.path.split("?", 1)[0]
        if re.match(_FAMILY_PROBE_PATTERN, path) is None:
            return real_dispatch(self)
        _role, _scope, _user_id, err = self._resolve_role()
        if err is not None:
            code, payload = err
            return self._send_json(payload, code)
        # ADMITTED, CARRYING NOTHING AT ALL.
        return self._send_json({})

    route_registry.REGISTRY = real_registry + (_FAMILY_PROBE_SPEC,)
    srv.Handler._dispatch_get = dispatch
    try:
        yield
    finally:
        route_registry.REGISTRY = real_registry
        srv.Handler._dispatch_get = real_dispatch


#: THE OWNER'S OWN FALSIFIER for blocker 1 (#427 round 18), spelled here so
#: the regression drives the exact shape the review describes: the two family
#: leaves that carry NEITHER side's private roster state
#: (:data:`OFFICIAL_ADMITTED_BEYOND_THE_SHEET`) admitting an OFFICIAL on ANY
#: game, with an EMPTY body.
#:
#: WHY THIS SHAPE IS THE SHARP ONE. On the game the official REFEREES both
#: leaves already answer 200 — that is the product's real, typed grant — so
#: the only cell this widening moves is the UNASSIGNED game's, and it carries
#: nothing for either data oracle to find. An axis keyed on the PRINCIPAL
#: cannot see it, because the swept official's global entitled-team set is
#: `{home, away}` and never enters the matrix at all.
_UNASSIGNED_OFFICIAL_LEAVES = ("officials", "reschedule")
_UNASSIGNED_OFFICIAL_LEAK = re.compile(
    r"^/api/games/[^/]+/(" + "|".join(_UNASSIGNED_OFFICIAL_LEAVES) + r")$")


@contextlib.contextmanager
def _an_official_admitted_to_a_game_they_do_not_referee():
    """Answer ``200 {}`` to any OFFICIAL on the two status-only leaves,
    whatever game they name.

    Installed into the REAL ``Handler._dispatch_get``, after the real
    ``_resolve_role``, so what it widens is a real authenticated session's
    real admission decision — the same mechanism
    :func:`_a_registered_leaf_of_the_private_game_family` uses, for the same
    reason: a faked route would prove something about the fake."""
    real_dispatch = srv.Handler._dispatch_get

    def dispatch(self):
        path = self.path.split("?", 1)[0]
        if _UNASSIGNED_OFFICIAL_LEAK.match(path) is None:
            return real_dispatch(self)
        role, _scope, _user_id, err = self._resolve_role()
        if err is not None or role != Role.OFFICIAL:
            return real_dispatch(self)
        # ADMITTED, CARRYING NOTHING AT ALL — invisible to both data oracles.
        return self._send_json({})

    srv.Handler._dispatch_get = dispatch
    try:
        yield
    finally:
        srv.Handler._dispatch_get = real_dispatch


class NoPrincipalEntitledToNoSideIsAdmittedToTheFamily(_SweepHarness,
                                                       unittest.TestCase):
    """THE ADMISSION MATRIX, derived on both axes — see section 6b."""

    CASES = ["the_admission_matrix"]
    #: One case name per tri-store test, so `_assert_matrix_ran` fails a
    #: loop that silently covered fewer backends than were configured.
    UNREFEREED_CASE = "an_official_admitted_to_an_unrefereed_game"
    DEAD_ROW_CASE = "a_dead_assignment_row_grants_nothing"

    #: Set by :meth:`_extra_principal`; a principal name aliased onto another
    #: principal's real session, so the map can gain a member without the
    #: fixture gaining an account.
    extra_principal = None

    def _entitlement(self, fx):
        """The real map, plus — while the falsifier is installed — ONE more
        principal entitled to no side."""
        entitlement = super()._entitlement(fx)
        if self.extra_principal is not None:
            entitlement[self.extra_principal] = (IN_NEITHER_SIDE, frozenset())
        return entitlement

    @contextlib.contextmanager
    def _extra_principal(self, name, aliases, who):
        who[name] = who[aliases]
        self.extra_principal = name
        try:
            yield
        finally:
            self.extra_principal = None
            who.pop(name, None)

    def _unentitled(self, fx):
        """THE OLD PRINCIPAL AXIS: every principal ``_entitlement`` gives an
        EMPTY entitled-team set GLOBALLY.

        KEPT ONLY AS THE THING THE REAL AXIS MUST BE STRICTLY WIDER THAN
        (#427 round 18, blocker 1). This is not what the matrix drives any
        more — see :meth:`_unentitled_cells` — and
        :meth:`test_no_principal_entitled_to_no_side_is_admitted_to_any_leaf`
        asserts that the derived cells are NOT merely this set crossed with
        both games, so a future edit that collapsed the subject axis back
        into a global one would be reported rather than merely smaller."""
        return sorted(name for name, (_klass, teams)
                      in self._entitlement(fx).items() if not teams)

    def _unentitled_cells(self, fx):
        """THE AXIS, DERIVED PER ``(principal, GAME SUBJECT)``: every cell
        whose principal may read NO SIDE OF THAT GAME.

        THE GAP THIS CLOSES (#427 round 18, blocker 1). The axis used to be
        a set of PRINCIPALS, selected by ``not teams`` off
        :meth:`_entitlement` — a GLOBAL team union. That predicate can only
        ever select a principal entitled to nothing ANYWHERE, so it drove
        ``thirdcoach`` and ``viewer`` and EXCLUDED THE SWEPT OFFICIAL, whose
        map says ``{home, away}`` although the actual grant is
        ASSIGNMENT-KEYED PER GAME. MEASURED at ``73faf4d`` with an in-memory
        HTTP widening that answered ``200 {}`` for that official on the
        UNASSIGNED ``gid2``'s ``/officials`` and ``/reschedule``: both wire
        calls were admitted and all three relevant tests stayed green — this
        matrix (which never selected the official), the official's
        design-classification matrix (which drives ``gid`` only) and
        ``AGameKeyedGrantDoesNotSpanASecondGame
        .test_the_game_one_grant_survives_and_game_two_stays_refused``
        (which drives ``gid2`` only on ``/board``, ``/lineups`` and
        ``/roster``).

        SO THE ANSWER IS NOT ONE PREDICATE WIDER, IT IS THE RELATIONSHIP
        ROWS. The cell is derived by :meth:`_subject_narrowed` — the same
        one definition both oracles are read against — which asks
        :meth:`_grant_rows` for every STORED row of the principal's grant
        kind, drops the ones the PRODUCT'S OWN declared state says grant
        nothing (:func:`_row_is_active`: ``OfficialAssignmentStatus
        .is_active``, ``GuardianLink.verified``, the eligible membership
        statuses), matches the survivors on EVERY dimension the row
        declares, and finally intersects with THIS GAME'S OWN TWO SIDES.
        Nothing here is a hand list and nothing here asks the gate under
        test. A DECLINED or REVOKED assignment therefore produces an
        unentitled cell exactly as a missing one does, which
        :meth:`test_a_dead_assignment_row_does_not_create_a_grant` measures
        through the product's own write paths.

        WHICH PRINCIPALS FALL OUT OF IT, MEASURED rather than predicted (see
        ``test_no_principal_entitled_to_no_side_is_admitted_to_any_leaf``'s
        own non-vacuity assertions): ``thirdcoach`` and ``viewer`` on both
        games, as before, AND the official on the game they do not referee.
        The GUARDIAN does NOT fall out, and that is a fact about this
        product rather than an omission: ``GuardianLink`` carries no
        ``game_id`` (:data:`GAME_KEYED_CLASSES`), so a guardian's grant is a
        standing authority over every game their junior's side appears in,
        and this fixture's two games carry THE SAME TWO TEAMS. Their
        confinement is a ROUTE fact, not a SUBJECT fact, and it stays
        asserted where it already was —
        :meth:`TheDesignClassificationsAreStillTrue
        .test_every_principal_carries_a_documented_class` pins all ten
        leaves of BOTH games at 403 for them. The Players are the same
        shape: their membership row names a side of both games.

        THE THIRD AXIS — WHICH GAMES — IS STILL :data:`GAME_KEYS`, still
        hand-enumerated, and NOT closed by this. Round 18 makes the
        principal axis a function of it; it does not derive it."""
        entitlement = self._entitlement(fx)
        cells = []
        for principal in sorted(entitlement):
            klass, teams = entitlement[principal]
            for game_key in self.GAME_KEYS:
                subjects = self._path_subjects(fx, (fx[game_key],))
                if not self._subject_narrowed(
                        fx, klass, teams, subjects, principal):
                    cells.append((principal, game_key))
        return cells

    # -- the derivation itself --------------------------------------------
    def test_the_family_is_derived_from_the_registry_and_addressable(self):
        """Every leaf comes from the registry, and every leaf can be
        DRIVEN. Source-level, so one backend proves it."""
        for label, store in self._stores():
            try:
                self._assert_backend(label, store)
                store.clear_all_data()
                fx = self._fixture(store)
                self._serve(fx)   # `_route_subjects` reads what it binds
                family = self._private_game_family()
                # The derivation is a function of the TEMPLATE, so it must
                # agree with what the paths actually look like…
                for name, spec in sorted(family.items()):
                    self.assertTrue(
                        self._path_of(spec, (fx["gid"],)).startswith(
                            f"/api/games/{fx['gid']}/"),
                        f"{name} was derived into the private-game family "
                        f"but does not address one game: {spec.template!r}")
                # …and every leaf is swept by the sweep itself, which is the
                # property that makes "derived from the registry" mean the
                # same thing here as it does for the inventory closure.
                _specs, subjects = self._assert_inventory_is_closed(fx)
                self.assertEqual(
                    [], sorted(set(family) - set(subjects)),
                    "a leaf of the private-game family is not bound in "
                    "_route_subjects, so the sweep never drives it: "
                    + str(sorted(set(family) - set(subjects))))
            finally:
                self._close(label, store)
            return   # the derivation is source-level, not per-backend

    # -- THE MATRIX --------------------------------------------------------
    def test_no_principal_entitled_to_no_side_is_admitted_to_any_leaf(self):
        """THE ASSERTION WHOSE ABSENCE FOR ``thirdcoach`` IS THE GAP.

        On every backend, because this is a statement about what the PRODUCT
        answers over real authenticated HTTP and not about a gate's text."""
        ran = []
        for label, store in self._stores():
            try:
                self._assert_backend(label, store)
                store.clear_all_data()
                fx = self._fixture(store)
                who = self._serve(fx)
                family = self._private_game_family()
                cells = self._unentitled_cells(fx)
                # NON-VACUITY, not an identity: pinning WHICH cells are
                # unentitled would put the hand-written axis straight back.
                self.assertTrue(
                    cells,
                    f"[{label}] no (principal, game) cell resolves to an "
                    f"empty entitled-team set, so this matrix asserts "
                    f"nothing. The two sharpest rows in _entitlement — "
                    f"{IN_NEITHER_SIDE} and {VIEWER_ENTITLED_TO_NOTHING} — "
                    f"and the swept official's UNASSIGNED game are what it "
                    f"is for.")
                # …AND STRICTLY WIDER THAN THE GLOBAL PREDICATE IT REPLACES
                # (#427 round 18, blocker 1). This is the whole finding in
                # one assertion: if every derived cell belonged to a
                # principal whose GLOBAL entitled-team set is empty, the
                # subject axis would have collapsed back into
                # `not teams` and the swept official on the game they do not
                # referee would be outside this matrix again.
                globally = set(self._unentitled(fx))
                per_subject = sorted(
                    cell for cell in cells if cell[0] not in globally)
                self.assertTrue(
                    per_subject,
                    f"[{label}] EVERY derived cell belongs to a principal "
                    f"entitled to no side ANYWHERE, so this axis is the "
                    f"global `not teams` predicate again under a new name. "
                    f"A grant this product keys PER GAME — an official's "
                    f"assignment — must produce a refusal cell on the game "
                    f"it does not cover: {sorted(cells)}")
                for principal, _game_key in cells:
                    self.assertIn(
                        principal, who,
                        f"[{label}] {principal} is entitled to no side of a "
                        f"game and holds no session, so it is asserted by "
                        f"nothing")
                measured = self._family_status_matrix_over(
                    who, fx, family, cells)
                self.assertEqual(
                    [], self._admissions(measured),
                    f"[{label}] A CALLER ENTITLED TO NO SIDE OF THE GAME "
                    f"THEY ADDRESSED WAS ADMITTED TO A LEAF OF THE "
                    f"PRIVATE-GAME FAMILY. Every cell of this matrix is "
                    f"pinned at {REFUSED}, measured; an admission here is a "
                    f"disclosure whether or not the body carries anything, "
                    f"because both data oracles in this file are blind to an "
                    f"admission that carries nothing. The derived cells were "
                    f"{sorted(cells)}, of which {per_subject} are keyed on "
                    f"the GAME rather than on the principal: "
                    + str(self._admissions(measured)))
                ran.append((label, "the_admission_matrix"))
            finally:
                self._close(label, store)
        self._assert_matrix_ran(ran, self.CASES)

    # -- BOTH AXES FAIL CLOSED ON A MEMBER THEY HAVE NOT SEEN --------------
    def test_a_new_leaf_of_the_family_is_asserted_with_no_edit_here(self):
        """REGISTER AN ELEVENTH LEAF that admits everybody and returns an
        EMPTY body; the matrix must report it, naming the leaf.

        The premise is asserted first — the leaf really is registered, really
        is in the derived family, and really does answer 200 — so this cannot
        pass because the injection did nothing."""
        for label, store in self._stores():
            try:
                self._assert_backend(label, store)
                store.clear_all_data()
                fx = self._fixture(store)
                who = self._serve(fx)
                before = self._private_game_family()
                self.assertNotIn(_FAMILY_PROBE_NAME, before)
                with _a_registered_leaf_of_the_private_game_family():
                    family = private_game_family()
                    # THE PREMISE, in three parts.
                    self.assertIn(
                        _FAMILY_PROBE_NAME, family,
                        "the registered leaf did not enter the derived "
                        "family, so nothing below is a statement about it")
                    cells = self._unentitled_cells(fx)
                    principal, game_key = cells[0]
                    status, body = self._req(
                        who[principal], "GET",
                        _FAMILY_PROBE_TEMPLATE.replace(
                            "{}", fx[game_key], 1))
                    self.assertEqual(status, 200, body)
                    self.assertEqual({}, body, body)
                    # …and the ORACLE. Nothing but the status can see this.
                    measured = self._family_status_matrix_over(
                        who, fx, family, cells)
                    reported = self._admissions(measured)
                self.assertTrue(
                    reported,
                    "A NEW LEAF OF THE PRIVATE-GAME FAMILY WAS NOT ASSERTED. "
                    "The leaf axis is supposed to be derived from "
                    "route_registry.REGISTRY, so an eleventh leaf must "
                    "acquire this matrix with no edit in this file; if it "
                    "does not, the ten are a hand-written list again.")
                for principal, game_key in self._unentitled_cells(fx):
                    self.assertTrue(
                        any(r.startswith(f"{principal} ")
                            and _FAMILY_PROBE_NAME in r
                            and r.endswith(f"({game_key})")
                            for r in reported),
                        f"the report does not name {principal} on "
                        f"{_FAMILY_PROBE_NAME} ({game_key}): {reported}")
                # …and the injection really is gone afterwards.
                self.assertEqual(before, self._private_game_family())
            finally:
                self._close(label, store)
            return   # the axis's own behaviour, not a per-backend property

    def test_a_new_principal_entitled_to_no_side_is_asserted_with_no_edit(
            self):
        """ADD A PRINCIPAL to ``_entitlement`` with an empty entitled-team
        set, aliased onto the OPERATOR's real session — a caller the product
        admits everywhere. The matrix must report it, naming the new
        principal, with no edit to any list of principals."""
        for label, store in self._stores():
            try:
                self._assert_backend(label, store)
                store.clear_all_data()
                fx = self._fixture(store)
                who = self._serve(fx)
                family = self._private_game_family()
                self.assertNotIn(
                    "a_ninth_principal",
                    [p for p, _g in self._unentitled_cells(fx)])
                with self._extra_principal("a_ninth_principal", "operator",
                                           who):
                    cells = self._unentitled_cells(fx)
                    # THE PREMISE: the map really gained the member, on BOTH
                    # games — an empty entitled-team set cannot be narrowed
                    # to anything by any subject — and the session behind it
                    # really is admitted.
                    self.assertEqual(
                        [("a_ninth_principal", key)
                         for key in self.GAME_KEYS],
                        [c for c in cells if c[0] == "a_ninth_principal"])
                    status, body = self._req(
                        who["a_ninth_principal"], "GET",
                        f"/api/games/{fx['gid']}/lineups")
                    self.assertEqual(status, 200, body)
                    measured = self._family_status_matrix_over(
                        who, fx, family, cells)
                    reported = self._admissions(measured)
                self.assertTrue(
                    any(r.startswith("a_ninth_principal ") for r in reported),
                    "A NEW PRINCIPAL ENTITLED TO NO SIDE WAS NOT ASSERTED. "
                    "The principal axis is supposed to be derived from "
                    "_entitlement itself, so a row that enters it with an "
                    "empty entitled-team set must acquire this matrix with "
                    "no edit in this file: " + str(reported))
                # …and the falsifier really is gone afterwards.
                self.assertNotIn(
                    "a_ninth_principal",
                    [p for p, _g in self._unentitled_cells(fx)])
            finally:
                self._close(label, store)
            return   # the axis's own behaviour, not a per-backend property

    # -- THE SUBJECT HALF OF THE AXIS FAILS CLOSED TOO ---------------------
    def test_an_official_admitted_to_an_unrefereed_game_is_reported(self):
        """THE OWNER'S REQUIRED REGRESSION FOR BLOCKER 1 (#427 round 18), on
        every backend over real authenticated HTTP.

        Widen the two status-only leaves — ``/officials`` and
        ``/reschedule``, the two the product legitimately answers an
        ASSIGNED official 200 on — to answer ANY official ``200 {}``,
        whatever game they name. On the game they referee that changes
        nothing; on the game they do NOT it is a live admission carrying no
        body, which is precisely the shape both data oracles are blind to.

        MEASURED AT ``73faf4d``, with this exact falsifier installed: both
        wire calls were admitted and the WHOLE MODULE stayed green —
        ``Ran 95 tests in 297.042s … OK`` on Memory, SQLite and real
        PostgreSQL — the three tests the owner named among them. The axis
        selected principals by their GLOBAL entitled-team set, so the swept
        official, whose map says ``{home, away}``, was never in the matrix
        at all.

        BOTH CELLS MUST BE NAMED. A report that named one leaf would leave
        the other unasserted, which is the hand-written-axis failure one
        level down."""
        ran = []
        for label, store in self._stores():
            try:
                self._assert_backend(label, store)
                store.clear_all_data()
                fx = self._fixture(store)
                who = self._serve(fx)
                family = self._private_game_family()
                cells = self._unentitled_cells(fx)
                # THE PREMISE, from the RELATIONSHIP ROWS and not from the
                # gate: the official holds an active assignment on `gid` and
                # none on `gid2`, so exactly one of their two cells is
                # unentitled.
                self.assertTrue(self._official_is_assigned(
                    fx, self._subject_of(fx, fx["gid"])), label)
                self.assertFalse(self._official_is_assigned(
                    fx, self._subject_of(fx, fx["gid2"])), label)
                self.assertEqual(
                    [("official", "gid2")],
                    [c for c in cells if c[0] == "official"],
                    f"[{label}] the derivation does not give the swept "
                    f"official exactly one unentitled cell — the game they "
                    f"do not referee — so this falsifier is not aimed at "
                    f"what it claims: {sorted(cells)}")
                for leaf in _UNASSIGNED_OFFICIAL_LEAVES:
                    self.assertIn(f"get_games_id_{leaf}", family, label)
                with _an_official_admitted_to_a_game_they_do_not_referee():
                    # THE PREMISE, on the wire: the widening really is live
                    # and really is an EMPTY body.
                    for leaf in _UNASSIGNED_OFFICIAL_LEAVES:
                        status, body = self._req(
                            who["official"], "GET",
                            f"/api/games/{fx['gid2']}/{leaf}")
                        self.assertEqual((200, {}), (status, body), label)
                    reported = self._admissions(
                        self._family_status_matrix_over(
                            who, fx, family, cells))
                for leaf in _UNASSIGNED_OFFICIAL_LEAVES:
                    self.assertIn(
                        f"official was answered 200 on get_games_id_{leaf} "
                        f"(gid2)", reported,
                        f"[{label}] an official was ADMITTED to /{leaf} of a "
                        f"game carrying no assignment row for them and this "
                        f"matrix did not report the cell. A game-keyed grant "
                        f"reduced to one GLOBAL team union is what let this "
                        f"through at 73faf4d: " + str(reported))
                # …AND NOTHING ELSE MOVED. The report is exactly the two
                # widened cells, so this cannot pass by reporting the world.
                self.assertEqual(
                    sorted(f"official was answered 200 on "
                           f"get_games_id_{leaf} (gid2)"
                           for leaf in _UNASSIGNED_OFFICIAL_LEAVES),
                    reported, label)
                # …and the falsifier really is gone afterwards, with the
                # REAL grant on the refereed game intact.
                self.assertEqual(
                    [], self._admissions(self._family_status_matrix_over(
                        who, fx, family, self._unentitled_cells(fx))), label)
                self._assert_the_official_keeps_their_own_game(
                    who, fx, family, label)
                ran.append((label, self.UNREFEREED_CASE))
            finally:
                self._close(label, store)
        self._assert_matrix_ran(ran, [self.UNREFEREED_CASE])

    def _assert_the_official_keeps_their_own_game(self, who, fx, family,
                                                  label):
        """NO OVER-REFUSAL: the assigned game's grant is untouched.

        Widening a refusal rule is exactly how something real breaks, so the
        official's own leaf-by-leaf answers on ``gid`` are re-measured here
        against the two TYPED sets rather than assumed — and the three
        sheet leaves are read for the private rows they are supposed to
        carry, so "still 200" cannot mean "200 and empty"."""
        admitted = (OFFICIAL_ASSIGNED_GAME_ROUTES
                    | OFFICIAL_ADMITTED_BEYOND_THE_SHEET)
        for name, spec in sorted(family.items()):
            status, body = self._req(
                who["official"], "GET", self._path_of(spec, (fx["gid"],)))
            self.assertEqual(
                200 if name in admitted else REFUSED, status,
                f"[{label}] the assigned official's answer on {name} of the "
                f"game they REFEREE moved to {status}. This round widens a "
                f"refusal rule; the grant it narrows must survive it: {body}")
            if name in OFFICIAL_ASSIGNED_GAME_ROUTES:
                blob = json.dumps(body, sort_keys=True, default=str)
                self.assertTrue(
                    any(re.search(rf"\b{re.escape(pid)}\b", blob)
                        for pid in self._submitted_side_ids(fx).get(
                            (fx["gid"], fx["home"]), frozenset())),
                    f"[{label}] the official is still admitted to {name} of "
                    f"their own game but it no longer carries the submitted "
                    f"lineup their grant IS, so 'the grant survives' would "
                    f"be a statement about a status code alone: {body}")

    def test_a_dead_assignment_row_does_not_create_a_grant(self):
        """THE NEGATIVE SUBJECT DERIVATION INCLUDES DECLINED AND REVOKED
        ROWS — the owner's second requirement for blocker 1.

        Give the swept official a REAL assignment on the game they do not
        referee, through the product's own ``assign_official``, and then
        take it away two ways the product itself spells:

        * DECLINED — ``respond_assignment(accept=False)``. The row SURVIVES
          and still names this official and this game; only ``status``
          moves, and ``OfficialAssignmentStatus.is_active`` is the product's
          own statement that it now grants nothing.
        * REVOKED — ``unassign_official``. The row is gone.

        In both states the cell must STILL be derived as unentitled. A
        derivation that merely asked "is there a row for this official and
        this game" would lose the cell in the DECLINED state, and an
        official the product records as having declined would be back
        outside this matrix — which is the F2 shape one axis over.

        AND THE POSITIVE HALF IS MEASURED IN THE SAME WORLD: while the
        assignment is ACTIVE the cell is GONE, so this is a statement about
        the assignment's state and not about a cell that never moves."""
        ran = []
        for label, store in self._stores():
            try:
                self._assert_backend(label, store)
                store.clear_all_data()
                fx = self._fixture(store)
                who = self._serve(fx)
                family = self._private_game_family()
                api = fx["api"]
                self.assertIn(("official", "gid2"),
                              self._unentitled_cells(fx), label)
                row = api.assign_official(fx["gid2"], fx["official_id"],
                                          "linesperson", actor_id=ADMIN)
                self.assertNotIn("error", row, (label, row))
                # THE POSITIVE HALF: an ACTIVE row really does remove the
                # cell, so the two negatives below are about `status`.
                self.assertNotIn(
                    ("official", "gid2"), self._unentitled_cells(fx),
                    f"[{label}] an ACTIVE assignment on the second game "
                    f"leaves the cell unentitled, so this test cannot tell "
                    f"a dead row from a live one")
                for kind in ("declined", "revoked"):
                    with self.subTest(backend=label, kind=kind):
                        if kind == "declined":
                            api.respond_assignment(
                                row["id"], accept=False, actor_id=ADMIN)
                            # READ BACK FROM THE STORE, so what is asserted
                            # is the state the derivation itself reads.
                            answered = api.store.get_official_assignment(
                                row["id"])
                            self.assertIsNotNone(
                                answered,
                                "respond_assignment deleted the row, so this "
                                "is the REVOKED case again")
                            self.assertEqual(
                                (fx["official_id"], fx["gid2"]),
                                (answered.official_id, answered.game_id),
                                f"[{label}] the declined row no longer names "
                                f"this official and this game, so 'the row "
                                f"survives and only status moves' is false")
                            self.assertFalse(
                                answered.status.is_active,
                                (label, answered.status))
                        else:
                            api.unassign_official(row["id"], actor_id=ADMIN)
                            self.assertIsNone(
                                api.store.get_official_assignment(row["id"]),
                                label)
                        self.assertIn(
                            ("official", "gid2"),
                            self._unentitled_cells(fx),
                            f"[{label}] a {kind} assignment row still grants "
                            f"the official the game it names, so the "
                            f"negative subject derivation reads the row's "
                            f"EXISTENCE rather than the product's own "
                            f"statement of what it holds")
                        # …and the PRODUCT agrees, over real authenticated
                        # HTTP, on every leaf of that game.
                        self.assertEqual(
                            [], self._admissions(
                                self._family_status_matrix_over(
                                    who, fx, family, [("official", "gid2")])),
                            f"[{label}] a {kind} official is ADMITTED to a "
                            f"leaf of the private-game family")
                # …and the game they DO referee is untouched throughout.
                self._assert_the_official_keeps_their_own_game(
                    who, fx, family, label)
                ran.append((label, self.DEAD_ROW_CASE))
            finally:
                self._close(label, store)
        self._assert_matrix_ran(ran, [self.DEAD_ROW_CASE])


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
                sheet = submitted[(fx["gid"], fx["home"])]
                unsubmitted = private[fx["home"]] - sheet
                self.assertTrue(
                    unsubmitted,
                    f"[{label}] every HOME private identity occupies a slot, "
                    f"so this test cannot distinguish the sheet from the "
                    f"side")
                self.assertTrue(
                    sheet,
                    f"[{label}] HOME's submitted sheet is empty, so "
                    f"'entitled to the sheet' is vacuous")
                for route in sorted(OFFICIAL_ASSIGNED_GAME_ROUTES):
                    permitted = self._permitted_ids(
                        fx, "official", route, self._subject_of(fx, fx["gid"]),
                        private, submitted)
                    self.assertEqual(
                        frozenset(), unsubmitted & permitted,
                        f"[{label}] on {route} the official is PERMITTED "
                        f"{sorted(unsubmitted & permitted)} — HOME "
                        f"identities that do not occupy a slot. Their grant "
                        f"is the submitted sheet, not the side's whole "
                        f"private population, and widening the forbidden set "
                        f"must not have quietly widened their permit.")
                    self.assertLessEqual(
                        sheet, permitted,
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
                        fx, "guardian", route,
                        self._subject_of(fx, fx["guardian_junior_id"],
                                         fx["gid"]),
                        private, submitted)
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


# ---------------------------------------------------------------------------
# 9. THE HINT AXIS IS CLOSED AGAINST WHAT THE SERVER READS.
#
# THE DEFECT THIS SECTION EXISTS FOR (#427 round 9, D7). `HINTS` was a
# hand-written 4-tuple and one of its four variants named `side` -- a
# parameter `web/server.py` does not read -- carrying a TEAM ID, a value
# outside that parameter's real domain. So the variant that carried the whole
# "client input cannot select a side" claim was probing a name the server
# ignores. Four injected lines that honour `?side=away` handed a HOME Coach
# and a HOME Player the AWAY side's five private identities with
# `restricted: false`, and the primary sweep passed: `Ran 27 tests in
# 172.133s ... OK` on all three backends. The supplemental scanner caught it;
# the primary protection did not.
#
# The route axis has never been the hole, because it is derived from
# `route_registry.REGISTRY`. This section gives the query-string axis the
# same property against `route_extract.query_parameter_names`.
# ---------------------------------------------------------------------------
class TheHintAxisIsClosedAgainstWhatTheServerReads(_SweepHarness,
                                                   unittest.TestCase):
    """A query parameter the product begins reading enters this sweep, or a
    named test fails."""

    def test_a_new_query_parameter_fails_this_test(self):
        read = frozenset(route_extract.query_parameter_names())
        self.assertTrue(
            read, "the query-parameter inventory is empty, so this closure "
                  "would pass vacuously")
        unprobed = sorted(read - set(QUERY_PARAMETER_PROBES))
        self.assertEqual(
            [], unprobed,
            f"NEW QUERY PARAMETER(S) THE SERVER READS AND THIS SWEEP DOES "
            f"NOT PROBE: {unprobed}. Every parameter `web/server.py` reads "
            f"from a query string must carry at least one probe value in "
            f"QUERY_PARAMETER_PROBES, or the hint-inertness assertion is "
            f"silent about it -- which is exactly how `?side=` went unswept "
            f"while the sweep sent `?side=<a team id>`.")
        stale = sorted(set(QUERY_PARAMETER_PROBES) - read)
        self.assertEqual(
            [], stale,
            f"{stale} are probed but the server no longer reads them; delete "
            f"the probe or move it to UNREAD_PARAMETER_CONTROL deliberately")

    def test_the_unread_control_really_is_unread(self):
        """`?side=away` is a CONTROL, and only while nothing reads `side`."""
        read = frozenset(route_extract.query_parameter_names())
        overlap = sorted(read & set(UNREAD_PARAMETER_CONTROL))
        self.assertEqual(
            [], overlap,
            f"{overlap} is classified as a name the server does NOT read, "
            f"and the server now reads it. It has stopped being a control "
            f"and must move into QUERY_PARAMETER_PROBES with a value in its "
            f"real domain.")

    def test_the_inventory_reports_a_parameter_the_moment_one_appears(self):
        """THE FALSIFIER FOR THE CLOSURE ITSELF, measured on source text
        rather than argued.

        The closure above is only worth having if the inventory it reads
        actually grows when the server starts reading something new. The D7
        falsifier is applied HERE, to a copy of `server.py`'s source, in
        exactly the spelling that defeated a textual matcher: aliased
        imports, and no intermediate `qs` name at all."""
        source = route_extract.SERVER_PATH.read_text()
        anchor = '            side_ids = private_read.side_ids\n'
        self.assertIn(anchor, source)
        falsified = source.replace(anchor, anchor + (
            '            from urllib.parse import parse_qs as _pq, '
            'urlparse as _up\n'
            '            _s = (_pq(_up(self.path).query).get("side") '
            'or [""])[0]\n'), 1)
        grown = frozenset(route_extract.query_parameter_names(falsified))
        self.assertIn(
            "side", grown,
            "a server that reads ?side= does not report `side` in the "
            "query-parameter inventory, so the closure above cannot fail "
            "when the axis reopens")
        self.assertEqual(
            frozenset(route_extract.query_parameter_names()) | {"side"},
            grown,
            "the falsified inventory differs from the real one by more than "
            "the injected parameter, so this measurement is not isolating "
            "the change it claims to")

    def test_no_parameter_the_server_reads_selects_a_side(self):
        """THE BEHAVIOURAL HALF OF THE CLOSURE, on every backend.

        The whole derived matrix — every parameter ``web/server.py`` reads,
        each carrying a value that names the AWAY side wherever the
        parameter can carry one, plus the ``?side=away`` control — against
        every authenticated GET route and every principal. This is where a
        NEW parameter is actually exercised; :data:`HINTS` carries only the
        side-bearing subset into the twenty-four worlds, for the measured
        reason
        recorded on that constant.

        ONE FRESH WORLD PER BACKEND, which is what makes it affordable: the
        cost this bounds is the sweep's monotonic growth ACROSS worlds, not
        the sweep itself."""
        ran = []
        for label, store in self._stores():
            try:
                self._assert_backend(label, store)
                store.clear_all_data()
                fx = self._fixture(store)
                who = self._serve(fx)
                specs, subjects = self._assert_inventory_is_closed(fx)
                with self.subTest(backend=label):
                    sweep = self._sweep(who, fx, specs, subjects, FULL_HINTS)
                    # PREMISE: the matrix really is wider than the per-world
                    # one, or this test is the sibling assertion again.
                    self.assertGreater(len(FULL_HINTS), len(HINTS))
                    self.assertEqual(
                        len(FULL_HINTS) * len(PRINCIPALS)
                        * len({(k[1], k[2]) for k in sweep.rows}),
                        len(sweep.rows),
                        "the full-matrix sweep did not issue one request per "
                        "(principal, path, variant)")
                    self._assert_hints_are_inert(
                        sweep, fx, f"{label}/full-hint-matrix")
                    self._assert_no_foreign_ids(
                        sweep, fx, f"{label}/full-hint-matrix")
                ran.append((label, "full_hint_matrix"))
            finally:
                self._close(label, store)
        self._assert_matrix_ran(ran, ["full_hint_matrix"])

    def test_every_probe_label_resolves_to_a_value(self):
        """A label with no value would be a silently skipped variant."""
        store = InMemoryStore()
        try:
            fx = self._fixture(store)
            for label in PROBE_LABELS:
                self.assertTrue(self._probe_value(fx, label), label)
            declared = {lab for labs in QUERY_PARAMETER_PROBES.values()
                        for lab in labs}
            declared |= {lab for labs in UNREAD_PARAMETER_CONTROL.values()
                         for lab in labs}
            self.assertEqual(
                declared, set(PROBE_LABELS),
                "PROBE_LABELS and the two probe maps disagree about which "
                "labels exist")
            for hint in HINTS:
                self._hint_query(fx, hint)   # raises on an unknown label
        finally:
            store.clear_all_data()


class TheHintExemptionsAreNecessaryAndSufficient(_SweepHarness,
                                                 unittest.TestCase):
    """`HINT_MAY_SELECT_FOR_AN_UNSCOPED_OPERATOR` is a MEASUREMENT, not a
    suppression list: every entry must be needed, and nothing outside it may
    move.

    This is the assertion that stops the exemption map being the
    accumulating-exemption shape the owner banned. An entry that has stopped
    being necessary is dead weight that silently covers whatever arrives on
    that route next."""

    def test_exactly_the_declared_pairs_move_and_only_for_an_operator(self):
        store = InMemoryStore()
        try:
            fx = self._fixture(store)
            who = self._serve(fx)
            specs, subjects = self._assert_inventory_is_closed(fx)
            sweep = self._sweep(who, fx, specs, subjects, FULL_HINTS)
            entitlement = self._entitlement(fx)
            movers = {}
            for (principal, route, path, hint), value in sweep.rows.items():
                if hint == "none":
                    continue
                if value == sweep.rows[(principal, route, path, "none")]:
                    continue
                movers.setdefault((route, hint.split("=", 1)[0]),
                                  set()).add(principal)
            self.assertEqual(
                set(HINT_MAY_SELECT_FOR_AN_UNSCOPED_OPERATOR), set(movers),
                "the set of (route, parameter) pairs where a hint changes "
                "the answer is not the set this sweep exempts. A pair that "
                "MOVES and is not exempt is a client-selected read; a pair "
                "that is exempt and does NOT move is a dead exemption that "
                "will silently cover the next parameter to arrive on that "
                f"route. moved={sorted(movers)} "
                f"exempt={sorted(HINT_MAY_SELECT_FOR_AN_UNSCOPED_OPERATOR)}")
            for pair, principals in sorted(movers.items()):
                classes = {entitlement[p][0] for p in principals}
                self.assertLessEqual(
                    classes, UNSCOPED_OPERATOR_CLASSES,
                    f"{pair} moves for {sorted(principals)}, whose classes "
                    f"are {sorted(classes)} — a hint is selecting a read for "
                    f"a principal that is not an unscoped operator")
        finally:
            store.clear_all_data()


# ---------------------------------------------------------------------------
# 10. THE SUBJECT AXIS IS CLOSED AGAINST THE RELATIONSHIP ROWS.
#
# THE DEFECT THIS SECTION EXISTS FOR (#427 round 9). Entitlement was keyed on
# (principal, route, data class) and never on WHICH GAME was being read, so an
# official's standing was the same for a game they referee and a game they
# have never been assigned to. Deleting the three-line assignment check in
# `services/game_side_scope.resolve_private_game_read` left an official
# admitted 200 to /board, /lineups and /roster of a game with ZERO assignment
# rows for them -- and the primary sweep passed: `Ran 27 tests in 175.345s ...
# OK`. One test in the backend noticed.
#
# The closure reads the STORE'S OWN RELATIONSHIP ROWS -- assignment rows,
# guardian links, the game's two side ids -- and never the gate under test,
# which is what stops the expectation widening in lockstep with the defect.
# ---------------------------------------------------------------------------
@contextlib.contextmanager
def _an_official_admitted_to_every_game():
    """The reported one-line deletion, applied in process.

    `resolve_private_game_read` admits an OFFICIAL whether or not an
    assignment row for them exists -- byte-for-byte the effect of deleting
    the `any(a.official_id == official_id ...)` test at
    `services/game_side_scope.py`. Patched on BOTH names that hold it: the
    dispatch's own import in `web/server.py` and the one
    `web/scope.can_read_private_game_data` reads."""
    real = game_side_scope.resolve_private_game_read

    def widened(role, scope, game_id, store):
        out = real(role, scope, game_id, store)
        if role == Role.OFFICIAL and not out.admitted:
            return dataclasses.replace(out, admitted=True)
        return out

    srv.resolve_private_game_read = widened
    web_scope.resolve_private_game_read = widened
    try:
        yield
    finally:
        srv.resolve_private_game_read = real
        web_scope.resolve_private_game_read = real


class TheSubjectAxisIsClosedAgainstTheRelationshipRows(_SweepHarness,
                                                       unittest.TestCase):
    """An official who is not assigned to THIS game is a stranger to it, and
    the primary sweep must say so."""

    def _reported(self, fn, *args):
        try:
            fn(*args)
        except AssertionError as exc:
            return str(exc)
        return None

    def test_admitting_an_unassigned_official_reddens_the_primary_sweep(self):
        """THE REPORTED FALSIFIER, required to go RED.

        The fixture already contains the sharp subject: the swept official is
        assigned to `gid` and NOT to `gid2`. With the assignment check gone
        they are admitted to `gid2` and served its private sheet, while the
        store holds no assignment row for them there -- so their permitted
        set is empty and ORACLE 1 must report it.

        AND THE FALSIFIER FOR THE CLOSURE: with entitlement keyed the way it
        was before this round -- on the route alone, with no subject -- the
        same world is GREEN. Without that half, this test would pass for a
        sweep that had not changed at all."""
        for label, store in self._stores():
            try:
                self._assert_backend(label, store)
                store.clear_all_data()
                fx = self._fixture(store)
                who = self._serve(fx)
                specs, subjects = self._assert_inventory_is_closed(fx)
                # The premise: the fixture really does contain a game this
                # official does not referee.
                self.assertTrue(self._official_is_assigned(
                    fx, self._subject_of(fx, fx["gid"])))
                self.assertFalse(self._official_is_assigned(
                    fx, self._subject_of(fx, fx["gid2"])))
                with _an_official_admitted_to_every_game():
                    status, body = self._req(
                        who["official"], "GET",
                        f"/api/games/{fx['gid2']}/lineups")
                    self.assertEqual(
                        status, 200,
                        "the injected widening did not actually admit the "
                        "unassigned official, so nothing below is a "
                        "statement about this sweep")
                    sweep = self._sweep(who, fx, specs, subjects)
                    reported = self._reported(
                        self._assert_no_foreign_ids, sweep, fx,
                        f"{label}/unassigned-official")
                    self.assertIsNotNone(
                        reported,
                        "THE PRIMARY SWEEP DID NOT CATCH AN OFFICIAL SERVED "
                        "THE PRIVATE SHEET OF A GAME THEY ARE NOT ASSIGNED "
                        "TO. That is the D1 signature the owner ruled a "
                        "blocker on, one axis over.")
                    self.assertIn("official", reported)

                    # -- THE FALSIFIER: round 8's SUBJECT-BLIND entitlement,
                    # restored in full. Both places the subject is consulted
                    # have to go, or this measures a half-change: the
                    # narrowing in `_subject_narrowed` AND the assignment
                    # test `_permitted_ids` makes for the official's own
                    # grant.
                    real_narrow = self._subject_narrowed
                    real_assigned = self._official_is_assigned
                    self._subject_narrowed = (
                        lambda _fx, _klass, teams, _subj, _who: teams)
                    self._official_is_assigned = lambda _fx, _subj: True
                    try:
                        blind = self._reported(
                            self._assert_no_foreign_ids, sweep, fx,
                            f"{label}/subject-blind")
                    finally:
                        self._subject_narrowed = real_narrow
                        self._official_is_assigned = real_assigned
                    self.assertIsNone(
                        blind,
                        "the subject-BLIND entitlement this round replaces "
                        "already catches the unassigned official, so this "
                        "test is not measuring the change it claims to: "
                        + str(blind))
            finally:
                self._close(label, store)
            return   # the oracle's own behaviour, not a per-backend property

    def test_a_revoked_relationship_costs_the_grant_it_carried(self):
        """Both revocations really withdraw something, and what the
        principal reads afterwards carries nothing they may no longer have.

        This is the half a keyed entitlement cannot give on its own: it
        observes the relationship CHANGING, on the first game, which is the
        one every earlier round's grant was written against."""
        for label, store in self._stores():
            try:
                self._assert_backend(label, store)
                store.clear_all_data()
                fx = self._fixture(store)
                who = self._serve(fx)
                specs, subjects = self._assert_inventory_is_closed(fx)
                base = self._sweep(who, fx, specs, subjects)
                for kind, principal in sorted(
                        RELATIONSHIP_REVOCATIONS.items()):
                    with self.subTest(revocation=kind):
                        with self._revoked(fx, kind):
                            world = self._sweep(who, fx, specs, subjects)
                            moved = (
                                self._assert_relationship_loss_is_observed(
                                    base, world, fx, kind, f"{label}/{kind}"))
                            self.assertTrue(moved)
                            self._assert_no_foreign_ids(
                                world, fx, f"{label}/{kind}")
                            # …and the ENTITLEMENT really collapsed, which is
                            # what makes the oracle above non-vacuous.
                            for route, path in sorted(world.subject_of):
                                subject = world.subject_of[(route, path)]
                                if subject.get("game_id") != fx["gid"]:
                                    continue
                                self.assertEqual(
                                    frozenset(),
                                    self._entitled_teams(
                                        fx, principal, route, subject,
                                        SUBMITTED_LINEUP_DATA),
                                    f"[{label}] {principal} is still "
                                    f"entitled to a side of {route} after "
                                    f"{kind} was revoked")
            finally:
                self._close(label, store)
            return   # the revocation's own behaviour, not a per-backend one


# ---------------------------------------------------------------------------
# 11. ORACLE 1'S ALPHABET IS EVERY WAY THE PRODUCT CAN NAME A PERSON.
#
# THE DEFECT THIS SECTION EXISTS FOR (#427 round 9, D8). The forbidden set
# was the `id` FIELD alone. A registered route handing `thirdcoach` -- typed
# IN_NEITHER_SIDE, entitled to nothing of either side -- the AWAY side's five
# private people BY NAME passed ALL THREE ORACLES GREEN.
# ---------------------------------------------------------------------------
_NAME_PROBE_TEMPLATE = "/api/sweep-probe-names/{}"
_NAME_PROBE_NAME = "get_sweep_probe_names_id"
_NAME_PROBE_SPEC = route_registry.RouteSpec(
    "GET", r"^/api/sweep-probe-names/[^/]+$", _NAME_PROBE_TEMPLATE,
    _NAME_PROBE_NAME, "_dispatch_get", kind="route", auth="session",
    scope_axis="none",
    note="injected by test_authenticated_side_noninterference; never shipped")


@contextlib.contextmanager
def _a_registered_route_serving_a_side_by_name(third_team_id):
    """A REAL, REGISTERED authenticated GET route that hands the coach of a
    team playing in NEITHER game one side's served population BY NAME, and
    answers everyone else with nothing.

    NO IDENTIFIER IS CARRIED, deliberately: the payload is exactly the
    `name` values `ApiService._lineup_rows` already serves, so the ONLY
    thing that can see it is an alphabet wider than the id field.
    SNAPSHOTTED so oracle 2 sees no diff, and the query string is never read
    so hint-inertness sees nothing either -- the same construction the
    candidate-pool probe uses, for the same reason."""
    real_registry = route_registry.REGISTRY
    real_dispatch = srv.Handler._dispatch_get
    snapshot = {}

    def dispatch(self):
        path = self.path.split("?", 1)[0]
        match = re.match(r"^/api/sweep-probe-names/([^/]+)$", path)
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
            snapshot[gid] = sorted(
                row["name"]
                for row in api._lineup_rows(game, game.home_team_id))
        return self._send_json({"secret": {"names": snapshot[gid]}})

    route_registry.REGISTRY = real_registry + (_NAME_PROBE_SPEC,)
    srv.Handler._dispatch_get = dispatch
    try:
        yield
    finally:
        route_registry.REGISTRY = real_registry
        srv.Handler._dispatch_get = real_dispatch


class TheIdentityAlphabetIsEveryWayToNameAPerson(_SweepHarness,
                                                 unittest.TestCase):
    """Names are identities, and so is anything else the record carries."""

    probe_registered = False

    def _route_subjects(self, fx):
        subjects = super()._route_subjects(fx)
        if self.probe_registered:
            subjects[_NAME_PROBE_NAME] = [(fx["gid2"],)]
        return subjects

    def _reported(self, fn, *args):
        try:
            fn(*args)
        except AssertionError as exc:
            return str(exc)
        return None

    def test_a_route_serving_a_side_by_name_fails_the_primary_sweep(self):
        """THE ADJUDICATOR'S PROBE, required to go RED — and the id-only
        alphabet re-measured GREEN on the same world in the same test, so
        the result cannot be accidental."""
        for label, store in self._stores():
            try:
                self._assert_backend(label, store)
                store.clear_all_data()
                fx = self._fixture(store)
                who = self._serve(fx)
                with _a_registered_route_serving_a_side_by_name(fx["third"]):
                    self.probe_registered = True
                    try:
                        specs, subjects = self._assert_inventory_is_closed(fx)
                        self.assertIn(_NAME_PROBE_NAME,
                                      {s.name for s in specs})
                        sweep = self._sweep(who, fx, specs, subjects)
                        key = [k for k in sweep.rows
                               if k[1] == _NAME_PROBE_NAME
                               and k[0] == "thirdcoach"][0]
                        served = (sweep.rows[key][1].get("secret")
                                  or {}).get("names")
                        self.assertTrue(
                            served,
                            "the probe served no names, so nothing below is "
                            "a statement about the alphabet")
                        # PREMISE: it carries no identifier at all.
                        blob = json.dumps(sweep.rows[key][1], default=str)
                        self.assertIsNone(
                            re.search(r"\bplayer_\d+\b", blob),
                            f"the probe carried an id, so the OLD alphabet "
                            f"could catch it: {blob[:200]}")
                        reported = self._reported(
                            self._assert_no_foreign_ids, sweep, fx,
                            f"{label}/names")
                        self.assertIsNotNone(
                            reported,
                            "THE PRIMARY SWEEP DID NOT CATCH A SIDE'S "
                            "PRIVATE POPULATION SERVED BY NAME to a coach "
                            "of neither team. A name is an identity.")
                        self.assertIn("thirdcoach", reported)

                        # -- THE FALSIFIER: the id-only alphabet.
                        real_tokens = self._tokens_of
                        self._tokens_of = (
                            lambda _fx, ids, _index=None: frozenset(ids))
                        try:
                            blind = self._reported(
                                self._assert_no_foreign_ids, sweep, fx,
                                f"{label}/ids-only")
                        finally:
                            self._tokens_of = real_tokens
                        self.assertIsNone(
                            blind,
                            "the ID-ONLY alphabet this round replaces "
                            "already catches a population served by name, "
                            "so this test is not measuring the change it "
                            "claims to: " + str(blind))
                    finally:
                        self.probe_registered = False
            finally:
                self._close(label, store)
            return   # the oracle's own behaviour, not a per-backend property

    def test_the_alphabet_is_derived_from_the_player_record(self):
        """A new identity FIELD on `Player` enters the alphabet with no edit
        to this file — the property that makes this a closure rather than a
        second hand-written list."""
        store = InMemoryStore()
        try:
            fx = self._fixture(store)
            private, _amb = self._private_side_ids(fx)
            everything = private[fx["home"]] | private[fx["away"]]
            self.assertTrue(everything)
            tokens = self._tokens_of(fx, everything)
            self.assertLess(
                len(everything), len(tokens),
                "the derived alphabet is no wider than the id set, so this "
                "round's widening is vacuous on this fixture")
            # every id survives…
            self.assertLessEqual(frozenset(everything), tokens)
            # …and so does at least one NAME, which is what D8 was about.
            names = {fx["api"].store.get_player(pid).name
                     for pid in everything}
            self.assertTrue(
                names & tokens,
                "no private player's NAME is in the alphabet, so the D8 "
                "widening did not happen")
            # …and the fields it reads are the RECORD's, not a list here.
            sample = fx["api"].store.get_player(sorted(everything)[0])
            self.assertLessEqual(
                self._identity_tokens(sample),
                {getattr(sample, f.name) for f in dataclasses.fields(sample)},
                "an identity token is not a value of the Player record it "
                "was derived from")
            self.assertNotIn(
                sample.team_id, self._identity_tokens(sample),
                "team_id names a SIDE, not a person, and must not be an "
                "identity token — every player of a side shares it")
        finally:
            store.clear_all_data()


# ---------------------------------------------------------------------------
# 11c. A GAME-KEYED GRANT DOES NOT SPAN A SECOND GAME.
#
# THE DEFECT THIS SECTION EXISTS FOR (#427 round 10, D10, owner comment
# 5432572444). Round 9 added the SUBJECT axis and this file's own axis table
# recorded it as narrowed. It was narrowed in ONE direction only: an official
# with no assignment row for the response's game was correctly cut to
# nothing, while an official WITH one was handed an aggregate computed over
# BOTH games.
#
#   * ORACLE 1: `_submitted_side_ids` unioned each side's occupants across
#     `gid` and `gid2` into `{team_id: ids}`, and `_permitted_ids` granted
#     that aggregate on the strength of an assignment to the RESPONSE's game.
#     Measured at f7ae9d7: the official is assigned to `game_1` and not to
#     `game_2`; `game_1` seats `player_1`/`player_12` and `player_6` occupies
#     `game_2` alone; `_permitted_ids(official, get_games_id_board,
#     subject=game_1)` answered `{player_1, player_12, player_6}`. Injecting
#     `player_6` into the official's authenticated `GET
#     /api/games/game_1/board` put it in the raw swept body and
#     `_assert_no_foreign_ids` STILL PASSED.
#   * ORACLE 2: `_assert_non_interference` received the perturbed TEAM and
#     never the perturbed GAME. Both games carry the same two teams, so the
#     team id alone cannot separate "this game's sheet moved" from "the other
#     game's did". Measured at the same head: a `/board` answer about
#     `game_1` made to vary with `game_2`'s seated lineup moved for the
#     official under a `game_2` `seated_lineup_row` perturbation, and
#     `_assert_non_interference` PASSED.
#
# Assignment is game-specific, not team-wide. Production is clean today; this
# was a hole in the PRIMARY PROTECTION, in the axis the previous round had
# just added — which is this file's own recurring shape and the reason a
# blind spot here is worse than a leak on one route.
# ---------------------------------------------------------------------------
@contextlib.contextmanager
def _the_official_sheet_also_carries(extra_ids):
    """The assigned official's ``/board`` answer carries ``extra_ids`` as
    occupying rows, and changes nothing else.

    Injected into the LIVE facade read rather than into a new probe route,
    because the claim under test is about the THREE ROUTES THE GRANT ALREADY
    COVERS. A probe route the official is entitled to nothing on would be
    caught by the ROUTE narrowing that has stood since round 2 and would say
    nothing about the game keying."""
    real = _ApiService.get_board

    def widened(self, game_id, team_id=None, viewer_role=None):
        out = real(self, game_id, team_id=team_id, viewer_role=viewer_role)
        if viewer_role == Role.OFFICIAL and out.get("players") is not None:
            out["players"] = list(out["players"]) + [
                {"id": pid, "group": "selected"} for pid in sorted(extra_ids)]
        return out

    _ApiService.get_board = widened
    try:
        yield
    finally:
        _ApiService.get_board = real


@contextlib.contextmanager
def _the_official_sheet_counts_another_game(subject_game, other_game):
    """The assigned official's ``/board`` FOR ``subject_game`` carries a
    field that is a function of ``other_game``'s SEATED LINEUP.

    A COUNT, carrying no identity at all — deliberately, so oracle 1 cannot
    see it and the test is unambiguous about which oracle bites. Same design
    as the guardian probe in
    :func:`_a_registered_route_that_widens_only_for_the_guardian`, and for
    the same reason."""
    real = _ApiService.get_board

    def widened(self, game_id, team_id=None, viewer_role=None):
        out = real(self, game_id, team_id=team_id, viewer_role=viewer_role)
        if viewer_role == Role.OFFICIAL and game_id == subject_game:
            other = self.store.get_game(other_game)
            rs = RosterService(self.store)
            seated = 0
            for side in (other.home_team_id, other.away_team_id):
                if not side:
                    continue
                seated += sum(
                    1 for row in rs.lineup_population(other, side)
                    if row.entry is not None
                    and row.entry.status.occupies_slot)
            out["other_game_seated_count"] = seated
        return out

    _ApiService.get_board = widened
    try:
        yield
    finally:
        _ApiService.get_board = real


class AGameKeyedGrantDoesNotSpanASecondGame(_SweepHarness, unittest.TestCase):
    """The owner's required regression coverage for D10, tri-store.

    Two games share the same two teams with the sides SWAPPED, the official
    is assigned to the FIRST only, and the second seats a player who occupies
    no slot in the first. Both falsifiers are required to redden a PRIMARY
    oracle, each with the rule this round replaces asserted STILL GREEN on
    the identical measurement — so neither test can pass for a reason other
    than the change it claims to measure. Nothing supplemental is consulted
    here: ``test_side_provenance_guard`` cannot see either defect, because
    neither is a spelling at a call site."""

    #: One case name per test, so ``_assert_matrix_ran`` fails a loop that
    #: silently covered fewer backends than were configured.
    IDENTITY_CASE = "a_game_two_occupant_reddens_the_identity_oracle"
    INTERFERENCE_CASE = "game_two_state_moving_game_one_reddens_oracle_two"
    GRANT_CASE = "the_game_one_grant_survives_and_game_two_stays_refused"

    def _reported(self, fn, *args):
        """The oracle's own failure text, or ``None`` when it passed."""
        try:
            fn(*args)
        except AssertionError as exc:
            return str(exc)
        return None

    @staticmethod
    def _aggregated_by_team(keyed):
        """The keying this round REPLACES: a side's occupants unioned across
        every game, then re-keyed back onto ``(game, side)``.

        Re-keyed rather than returned in the old shape on purpose — the ONLY
        thing that differs from the shipped rule is the AGGREGATION, so a
        falsifier built on it measures the keying and not the plumbing."""
        by_team = {}
        for (_game, side), ids in keyed.items():
            by_team[side] = by_team.get(side, frozenset()) | ids
        return {key: by_team[key[1]] for key in keyed}

    def _premises(self, fx, label):
        """The fixture facts both falsifiers rest on, ASSERTED rather than
        assumed — every one of them is a way this section could be vacuous.

        Returns ``(game_1's occupants, the ids that occupy game_2 alone)``."""
        api = fx["api"]
        g1 = api.store.get_game(fx["gid"])
        g2 = api.store.get_game(fx["gid2"])
        self.assertEqual(
            {g1.home_team_id, g1.away_team_id},
            {g2.home_team_id, g2.away_team_id},
            f"[{label}] the two games do not carry the SAME TWO TEAMS, so "
            f"the perturbed team id alone would already separate them and "
            f"this fixture cannot express D10 at all")
        self.assertNotEqual(
            g1.home_team_id, g2.home_team_id,
            f"[{label}] the sides are not swapped between the two games")
        self.assertTrue(
            self._official_is_assigned(fx, self._subject_of(fx, fx["gid"])),
            f"[{label}] the official is not assigned to the first game, so "
            f"'their grant covers this game' is vacuous")
        self.assertFalse(
            self._official_is_assigned(fx, self._subject_of(fx, fx["gid2"])),
            f"[{label}] the official IS assigned to the second game, so "
            f"'a game they do not referee' is vacuous")
        # READ FROM THE PRODUCT, NOT FROM `_submitted_side_ids` — the method
        # under test. Taking the premise from the thing being falsified would
        # make the falsifiers below degenerate the moment it is reverted:
        # they would fail on a vacuous premise instead of on a blind oracle,
        # which proves nothing about the oracle. `_submitted_lineup_rows`
        # over `_lineup_rows` is the pair the three official routes run.
        def occupants(game):
            out = frozenset()
            for side in (game.home_team_id, game.away_team_id):
                if not side:
                    continue
                out |= {row["id"] for row in api._submitted_lineup_rows(
                    api._lineup_rows(game, side))}
            return out
        first, second = occupants(g1), occupants(g2)
        only_second = second - first
        self.assertTrue(
            first,
            f"[{label}] nobody occupies a slot in the first game, so the "
            f"official's real grant is empty and proof (3) is vacuous")
        self.assertTrue(
            only_second,
            f"[{label}] no identity occupies the SECOND game and not the "
            f"first, so there is nothing the aggregate could have carried "
            f"across and both falsifiers below are vacuous")
        return first, only_second

    # -- FALSIFIER (1): the PRIMARY IDENTITY ORACLE ------------------------
    def test_a_game_two_occupant_on_the_game_one_sheet_reddens_oracle_one(
            self):
        """THE OWNER'S FIRST REQUIRED FALSIFIER, required to go RED.

        A row that occupies a slot in ``game_2`` and in no other game is
        injected into the official's authenticated ``game_1`` ``/board``.
        Oracle 1 must fail NAMING that id: the official referees ``game_1``,
        and ``game_2``'s sheet is not theirs however many teams the two
        games share."""
        ran = []
        for label, store in self._stores():
            try:
                self._assert_backend(label, store)
                store.clear_all_data()
                fx = self._fixture(store)
                who = self._serve(fx)
                specs, subjects = self._assert_inventory_is_closed(fx)
                _first, only_second = self._premises(fx, label)
                with self.subTest(backend=label):
                    with _the_official_sheet_also_carries(only_second):
                        # THE PREMISE: the injection really reached the wire,
                        # over a real authenticated session.
                        status, body = self._req(
                            who["official"], "GET",
                            f"/api/games/{fx['gid']}/board")
                        self.assertEqual(status, 200, (label, body))
                        served = {row["id"] for row in body["players"]}
                        self.assertLessEqual(
                            only_second, served,
                            f"[{label}] the injected game-2 occupant did not "
                            f"reach the official's game-1 board, so nothing "
                            f"below is measuring a leak")
                        sweep = self._sweep(who, fx, specs, subjects)
                        reported = self._reported(
                            self._assert_no_foreign_ids, sweep, fx,
                            f"{label}/d10")
                        self.assertIsNotNone(
                            reported,
                            f"[{label}] THE PRIMARY IDENTITY ORACLE DID NOT "
                            f"CATCH AN OFFICIAL RECEIVING "
                            f"{sorted(only_second)} — an identity that "
                            f"occupies a slot only in a game they do not "
                            f"referee — on the sheet of the game they do. "
                            f"The sweep is the primary protection: it must "
                            f"fail here before anything supplemental is "
                            f"consulted.")
                        for pid in sorted(only_second):
                            self.assertIn(
                                pid, reported,
                                f"[{label}] the oracle went red but did not "
                                f"name {pid}, so it is not this leak it "
                                f"caught")
                        self.assertIn("official", reported)
                        self.assertIn("get_games_id_board", reported)
                        # THE FALSIFIER: the aggregate-by-team keying this
                        # round replaces, on the IDENTICAL sweep, stays GREEN.
                        real = self._submitted_side_ids
                        self._submitted_side_ids = (
                            lambda _fx, _r=real:
                            self._aggregated_by_team(_r(_fx)))
                        try:
                            still_green = self._reported(
                                self._assert_no_foreign_ids, sweep, fx,
                                f"{label}/aggregate")
                        finally:
                            self._submitted_side_ids = real
                        self.assertIsNone(
                            still_green,
                            f"[{label}] the AGGREGATE-BY-TEAM keying this "
                            f"round replaces also catches the injected row, "
                            f"so this test is not measuring the change it "
                            f"claims to: {still_green}")
                ran.append((label, self.IDENTITY_CASE))
            finally:
                self._close(label, store)
        self._assert_matrix_ran(ran, [self.IDENTITY_CASE])

    # -- FALSIFIER (2): the PRIMARY NON-INTERFERENCE ORACLE ----------------
    def test_a_game_one_response_moving_with_game_two_reddens_oracle_two(
            self):
        """THE OWNER'S SECOND REQUIRED FALSIFIER, required to go RED.

        The official's ``game_1`` ``/board`` is made a function of
        ``game_2``'s seated lineup — as a COUNT, so it carries no identity
        and only oracle 2 can see it — and then ONLY ``game_2``'s seated
        lineup is perturbed. Oracle 2 must report the official."""
        ran = []
        for label, store in self._stores():
            try:
                self._assert_backend(label, store)
                store.clear_all_data()
                fx = self._fixture(store)
                who = self._serve(fx)
                specs, subjects = self._assert_inventory_is_closed(fx)
                self._premises(fx, label)
                path = f"/api/games/{fx['gid']}/board"
                team = fx["home"]
                with self.subTest(backend=label):
                    with _the_official_sheet_counts_another_game(
                            fx["gid"], fx["gid2"]):
                        base = self._sweep(who, fx, specs, subjects)
                        with self._perturbed(fx, team, fx["gid2"],
                                             "seated_lineup_row"):
                            world = self._sweep(who, fx, specs, subjects)
                            # THE PREMISE: the game-1 response really moved.
                            moved = [k for k in base.diff(world)
                                     if k[0] == "official"
                                     and k[2] == path]
                            self.assertTrue(
                                moved,
                                f"[{label}] the official's game-1 board did "
                                f"not move when game 2's seated lineup "
                                f"changed, so there is no cross-game "
                                f"dependence to catch")
                            # ORACLE 1 CANNOT SEE IT — the probe is a count.
                            self.assertIsNone(
                                self._reported(self._assert_no_foreign_ids,
                                               world, fx, f"{label}/count"),
                                f"[{label}] the cross-game probe carries an "
                                f"identity after all, so oracle 2 is not "
                                f"the only oracle that could catch it")
                            reported = self._reported(
                                self._assert_non_interference, base, world,
                                fx, team, f"{label}/d10",
                                PERTURBATIONS["seated_lineup_row"],
                                fx["gid2"])
                            self.assertIsNotNone(
                                reported,
                                f"[{label}] THE PRIMARY NON-INTERFERENCE "
                                f"ORACLE DID NOT CATCH A RESPONSE ABOUT THE "
                                f"OFFICIAL'S ASSIGNED GAME THAT IS A "
                                f"FUNCTION OF ANOTHER GAME'S SUBMITTED "
                                f"STATE. It must fail here before anything "
                                f"supplemental is consulted.")
                            self.assertIn("official", reported)
                            self.assertIn(path, reported)
                            # THE FALSIFIER: the oracle WITHOUT the perturbed
                            # game — exactly what stood at f7ae9d7 — stays
                            # GREEN on the identical pair of worlds.
                            real = self._entitled_teams

                            def blind(_fx, principal, route, subject,
                                      data_class, perturbed_game=None,
                                      _real=real):
                                return _real(_fx, principal, route, subject,
                                             data_class)

                            self._entitled_teams = blind
                            try:
                                still_green = self._reported(
                                    self._assert_non_interference, base,
                                    world, fx, team, f"{label}/blind",
                                    PERTURBATIONS["seated_lineup_row"],
                                    fx["gid2"])
                            finally:
                                self._entitled_teams = real
                            self.assertIsNone(
                                still_green,
                                f"[{label}] the oracle that never received "
                                f"the perturbed game also catches this, so "
                                f"this test is not measuring the change it "
                                f"claims to: {still_green}")
                ran.append((label, self.INTERFERENCE_CASE))
            finally:
                self._close(label, store)
        self._assert_matrix_ran(ran, [self.INTERFERENCE_CASE])

    # -- PROOF (3): THE REAL GRANT IS UNHARMED -----------------------------
    def test_the_game_one_grant_survives_and_game_two_stays_refused(self):
        """THE OWNER'S THIRD REQUIREMENT: the narrowing did not kill the
        grant it narrows.

        Game 1's occupying rows are still PERMITTED to the official on all
        three of :data:`OFFICIAL_ASSIGNED_GAME_ROUTES`, game 2's
        game-2-only occupant is not, and the official's real game 2 reads
        are still REFUSED over authenticated HTTP. Without this, keying by
        game would be indistinguishable from deleting the grant."""
        ran = []
        for label, store in self._stores():
            try:
                self._assert_backend(label, store)
                store.clear_all_data()
                fx = self._fixture(store)
                who = self._serve(fx)
                first, only_second = self._premises(fx, label)
                private, _ambiguous = self._private_side_ids(fx)
                submitted = self._submitted_side_ids(fx)
                with self.subTest(backend=label):
                    for route in sorted(OFFICIAL_ASSIGNED_GAME_ROUTES):
                        permitted = self._permitted_ids(
                            fx, "official", route,
                            self._subject_of(fx, fx["gid"]), private,
                            submitted)
                        self.assertEqual(
                            first, permitted,
                            f"[{label}] on {route} the official's permitted "
                            f"identities for game 1 are {sorted(permitted)}, "
                            f"not game 1's own occupying rows "
                            f"{sorted(first)} — keying by game must narrow "
                            f"the grant, not delete or widen it")
                        self.assertEqual(
                            frozenset(), only_second & permitted,
                            f"[{label}] on {route} the official is STILL "
                            f"permitted {sorted(only_second & permitted)}, "
                            f"which occupies a slot only in the game they "
                            f"do not referee")
                        # …and the game they do NOT referee grants nothing,
                        # to either oracle.
                        self.assertEqual(
                            frozenset(),
                            self._permitted_ids(
                                fx, "official", route,
                                self._subject_of(fx, fx["gid2"]), private,
                                submitted),
                            f"[{label}] on {route} the official is permitted "
                            f"an identity of a game carrying no assignment "
                            f"row for them")
                        self.assertEqual(
                            frozenset(),
                            self._entitled_teams(
                                fx, "official", route,
                                self._subject_of(fx, fx["gid2"]),
                                SUBMITTED_LINEUP_DATA),
                            f"[{label}] on {route} the official is entitled "
                            f"to a SIDE of a game they do not referee")
                    # THE PRODUCT, over real authenticated HTTP: game 1
                    # served, game 2 refused. Measured, not assumed — a
                    # narrowing that only moved the test's expectation would
                    # pass everything above.
                    for leaf, name in (("board", "get_games_id_board"),
                                       ("lineups", "get_games_id_lineups"),
                                       ("roster", "get_games_id_roster")):
                        status, body = self._req(
                            who["official"], "GET",
                            f"/api/games/{fx['gid']}/{leaf}")
                        self.assertEqual(
                            status, 200,
                            f"[{label}] the official lost their real grant "
                            f"on /{leaf} of the game they referee: {body}")
                        blob = json.dumps(body, sort_keys=True, default=str)
                        for pid in sorted(only_second):
                            self.assertIsNone(
                                re.search(rf"\b{re.escape(pid)}\b", blob),
                                f"[{label}] the PRODUCT serves {pid} on "
                                f"/{leaf} of game 1 — a live cross-game "
                                f"disclosure, not merely an oracle that "
                                f"would have missed one")
                        status, body = self._req(
                            who["official"], "GET",
                            f"/api/games/{fx['gid2']}/{leaf}")
                        self.assertEqual(
                            status, 403,
                            f"[{label}] the official is no longer REFUSED "
                            f"/{leaf} of the game they do not referee, so "
                            f"the product's own game keying moved: {body}")
                        self.assertEqual(
                            "forbidden", body["error"]["code"], (label, body))
                ran.append((label, self.GRANT_CASE))
            finally:
                self._close(label, store)
        self._assert_matrix_ran(ran, [self.GRANT_CASE])


# ---------------------------------------------------------------------------
# 11d. NO GRANT IS AGGREGATED ACROSS A DIMENSION IT IS KEYED ON.
#
# THE QUESTION THE OWNER ASKED OF THE OTHER SEATS. D10 was found in the
# official's chair; three of the last four rounds found the SAME SHAPE one
# seat over from wherever it was last fixed. So the question is asked of
# every principal at once, and against an authority OUTSIDE this module —
# because "which of our own entitlements is game-keyed?" answered by reading
# our own entitlement code puts both sides of the equality in this file,
# which is the failure `PRINCIPAL_ROLES` already records once.
#
# THE AUTHORITY IS THE GRANT ROW ITSELF: `OfficialAssignment` carries
# `game_id`, `GuardianLink` does not, and an account scope's accepted keys
# (`AccountService._ALLOWED_SCOPE_KEYS`) are `team_id`/`player_id`/
# `official_id` — no game anywhere. See `GRANT_DIMENSIONS`, and section 11.5
# for the same question asked of EVERY dimension rather than of the game.
# ---------------------------------------------------------------------------
class NoGrantIsAggregatedAcrossADimensionItIsKeyedOn(_SweepHarness,
                                                    unittest.TestCase):
    """The declared authority and the running oracles must agree about which
    principals are game-keyed — measured both ways, so a future principal
    cannot acquire a game-keyed grant and keep a game-blind entitlement."""

    def test_every_swept_class_declares_the_row_its_grant_lives_in(self):
        """FAIL-CLOSED, like the route and principal axes: a class with no
        entry in :data:`GRANT_RECORD_FIELDS` is an ERROR NAMING IT rather
        than a silently game-blind grant."""
        store = InMemoryStore()
        try:
            fx = self._fixture(store)
            classes = {klass for klass, _teams in self._entitlement(fx).values()}
            self.assertEqual(
                frozenset(), classes - frozenset(GRANT_RECORD_FIELDS),
                "an entitlement class this sweep drives declares no grant "
                "row, so nothing says whether its grant is keyed on a game")
            self.assertEqual(
                frozenset(), frozenset(GRANT_RECORD_FIELDS) - classes,
                "GRANT_RECORD_FIELDS names a class this sweep no longer "
                "drives, so its declaration is dead")
        finally:
            store.clear_all_data()

    def test_the_declared_game_keyed_set_is_the_measured_one(self):
        """THE AUDIT THE OWNER REQUIRED, executable in both directions.

        The two games carry the SAME TWO TEAMS, so any principal whose
        entitlement differs between them can only be reading a relationship
        that names a game. That measured set must equal
        :data:`GAME_KEYED_CLASSES`, which is derived from the domain records
        — and every game-keyed class must actually be cut to nothing when
        the perturbed game is not the subject, which is the rule D10 added.

        Measured on this tree: the OFFICIAL is the only one. The guardian's
        `GuardianLink` names a junior and no game; a coach's account scope
        names a team; the two unscoped operators and the viewer read no
        relationships at all."""
        store = InMemoryStore()
        try:
            fx = self._fixture(store)
            self._serve(fx)
            specs, subjects = self._assert_inventory_is_closed(fx)
            routes = sorted({s.name for s in specs if s.name in subjects})
            private, _ambiguous = self._private_side_ids(fx)
            submitted = self._submitted_side_ids(fx)
            entitlement = self._entitlement(fx)
            here_at = self._subject_of(fx, fx["gid"])
            there_at = self._subject_of(fx, fx["gid2"])
            measured, narrowed = set(), set()
            for principal in PRINCIPALS:
                klass = entitlement[principal][0]
                for route in routes:
                    for data_class in sorted(
                            {SUBMITTED_LINEUP_DATA, TEAM_WORKFLOW_DATA}):
                        here = self._entitled_teams(
                            fx, principal, route, here_at, data_class)
                        there = self._entitled_teams(
                            fx, principal, route, there_at, data_class)
                        if here != there:
                            measured.add(klass)
                        # …and the D10 rule: state of the OTHER game may not
                        # excuse a diff about this one.
                        if here and not self._entitled_teams(
                                fx, principal, route, here_at, data_class,
                                perturbed_game=fx["gid2"]):
                            narrowed.add(klass)
                    if (self._permitted_ids(fx, principal, route, here_at,
                                            private, submitted)
                            != self._permitted_ids(fx, principal, route,
                                                   there_at, private,
                                                   submitted)):
                        measured.add(klass)
            self.assertEqual(
                GAME_KEYED_CLASSES, frozenset(measured),
                "the entitlement classes whose answer MEASURABLY depends on "
                "which of the two games is being read are not the classes "
                "whose grant row NAMES a game. A class in the measured set "
                "and not the declared one is reading a game relationship "
                "nothing declared; a class in the declared set and not the "
                "measured one has a game-keyed grant this sweep is applying "
                "game-blind — which is D10 in another seat.")
            self.assertEqual(
                GAME_KEYED_CLASSES, frozenset(narrowed),
                "the classes cut to nothing when the perturbed game is not "
                "the subject are not the game-keyed ones, so oracle 2 is "
                "either still blind to a cross-game diff or is reporting a "
                "standing team-wide grant as one")
            self.assertEqual(
                frozenset({OFFICIAL_SUBMITTED_LINEUP_ONLY}),
                GAME_KEYED_CLASSES,
                "the set of game-keyed grants moved. That is a product "
                "change, not a test detail: re-run this audit and decide "
                "what the new one may observe, rather than adjusting this "
                "line.")
        finally:
            store.clear_all_data()



# ---------------------------------------------------------------------------
# 11.5 EVERY DIMENSION THE GRANT ROW CARRIES KEYS THE GRANT.
#
# THE SPECIES, THREE ROUNDS RUNNING: an entitlement computed over a COARSER
# KEY than the response it governs. Round 10 was the official's grant
# aggregated across GAMES while `OfficialAssignment` names one. Round 11 found
# three more of it in the rest of the same two rows:
#
#   F1  the guardian's grant aggregated across JUNIORS. `_permitted_ids` never
#       received the junior the path named, and both swept bindings were the
#       guardian's OWN junior, so the unentitled direction was never swept.
#   F2  `OfficialAssignment.status`. The row DECLARES it, the product declares
#       what it means (`OfficialAssignmentStatus.is_active`), four other
#       consumers honour it — and neither the read gate nor this file keyed on
#       it. Driven through `respond_assignment(accept=False)`, the swept
#       official DECLINED and still read `/board`, `/lineups` and `/roster`,
#       and the primary sweep passed: `Ran 1 test in 110.641s … OK`.
#   F3  `GuardianLink.guardian_user_id`. The oracle asked "does this junior
#       have a verified link", not "does THIS guardian have one".
#
# So the rule stops being "key on the game" and becomes: A GRANT IS KEYED BY
# EVERY DIMENSION THE PRODUCT ROW THAT STORES IT CARRIES. `GRANT_DIMENSIONS`
# derives those dimensions from the record — `_subject_fields` for the rows it
# names, `_activation_fields` for the state it declares about itself — and
# this section pins the derivation in BOTH DIRECTIONS PER FIELD:
#
#   * every field of every grant row is either a DERIVED dimension or carries
#     a TYPED reason in `GRANT_FIELDS_THAT_KEY_NOTHING`, so a row that gains
#     one is an ERROR NAMING IT;
#   * and each half is MEASURED rather than asserted: perturbing a field moves
#     the oracles if and only if it is a declared dimension.
# ---------------------------------------------------------------------------

#: Every dimension name that is ACTIVATION state rather than a named row,
#: across every grant row — derived, and used only to choose which half of the
#: oracle a blindness control has to disable.
_ACTIVATION_DIMENSIONS = frozenset().union(*(
    _activation_fields(record) for record in GRANT_ROWS.values()))


def _alternative_value(fx, record, field, current):
    """A DIFFERENT, still-VALID value for one field of a grant row.

    Derived from the field's DECLARED TYPE, so there is no per-field recipe
    list to fall out of step with the record:

    * a SUBJECT field takes another real row of that kind from
      ``fx["other_subjects"]`` — an id naming nothing would collapse the
      oracles for the wrong reason, and a ``KeyError`` here NAMES a subject
      dimension this fixture holds no second instance of;
    * an ACTIVATION field takes a value whose ACTIVATION differs, not merely
      whose value does: ``PROPOSED`` → ``ACCEPTED`` moves no authority and
      would report ``status`` as a dead dimension, so the member is chosen by
      ``is_active``;
    * any other Enum takes a different member, a datetime moves a day, a
      string takes a sentinel.

    Anything else is an ``AssertionError`` naming the field and its type
    rather than a silently unmeasured field."""
    if field in _subject_fields(record):
        return fx["other_subjects"][field]
    declared = _base_type(_record_types(record)[field])
    if field in _activation_fields(record):
        if declared is bool:
            return not current
        if declared in ELIGIBILITY_AUTHORITIES:
            # The product keeps the partition beside the type, so "a value
            # whose ACTIVATION differs" is read off that partition — and off
            # the INELIGIBLE half specifically, never off `set(enum) -
            # eligible`, so a member the product has classified in neither
            # tuple can never be picked up here as though it had been.
            eligible, ineligible = _eligibility_partition(declared)
            pool = ineligible if declared(current) in eligible else eligible
            return next(m for m in declared if m in pool)
        return next(m for m in declared
                    if m.is_active != declared(current).is_active)
    if isinstance(declared, type) and issubclass(declared, enum.Enum):
        return next(m for m in declared if m != declared(current))
    if declared is int:
        # A season-Team jersey is unique within the season Team, so the
        # substitute must be one nothing else holds.
        return (current or 0) + 900
    if declared is _datetime.datetime:
        base = current or _datetime.datetime(
            2031, 1, 1, tzinfo=_datetime.timezone.utc)
        return base + _datetime.timedelta(days=1)
    if declared is str:
        return "sweep_alternative_value"
    raise AssertionError(
        f"{record.__name__}.{field} is declared {declared!r}, which this "
        f"audit does not know how to move. A grant-row field nothing can "
        f"perturb is a field nothing can MEASURE, which is exactly the "
        f"standing every finding this section exists for had.")


@contextlib.contextmanager
def _an_official_admitted_on_a_dead_assignment():
    """`resolve_private_game_read`'s OFFICIAL branch WITHOUT the status test
    — byte-for-byte the predicate this round replaces, restored in process.

    This is the falsifier the module's own "the oracle does not widen in
    lockstep with the gate" claim needs: the gate loses a dimension, the
    expectation must NOT follow it."""
    real = game_side_scope.resolve_private_game_read

    def widened(role, scope, game_id, store):
        if role != Role.OFFICIAL:
            return real(role, scope, game_id, store)
        official_id = (scope or {}).get("official_id")
        return game_side_scope.PrivateGameRead(
            role=role, game=store.get_game(game_id), own_team=None,
            admitted=official_id is not None and any(
                a.official_id == official_id
                for a in store.assignments_for_game(game_id)))

    srv.resolve_private_game_read = widened
    web_scope.resolve_private_game_read = widened
    try:
        yield
    finally:
        srv.resolve_private_game_read = real
        web_scope.resolve_private_game_read = real


@contextlib.contextmanager
def _an_official_admitted_on_anyone_s_assignment():
    """The same branch with the ``official_id`` half deleted instead:
    admission on the EXISTENCE of an active assignment for the game, whoever
    it names."""
    real = game_side_scope.resolve_private_game_read

    def widened(role, scope, game_id, store):
        if role != Role.OFFICIAL:
            return real(role, scope, game_id, store)
        return game_side_scope.PrivateGameRead(
            role=role, game=store.get_game(game_id), own_team=None,
            admitted=(scope or {}).get("official_id") is not None and any(
                a.status.is_active
                for a in store.assignments_for_game(game_id)))

    srv.resolve_private_game_read = widened
    web_scope.resolve_private_game_read = widened
    try:
        yield
    finally:
        srv.resolve_private_game_read = real
        web_scope.resolve_private_game_read = real


@contextlib.contextmanager
def _a_guardian_admitted_on_another_guardians_link():
    """``GuardianService``'s two read helpers with ``guardian_user_id``
    IGNORED — the two-line widening shape, aimed at F3's dimension. A
    guardian is now whoever asks about a junior somebody is verified for."""
    cls = guardian_service.GuardianService
    real_is, real_ids = cls.is_verified_guardian, cls.verified_junior_ids

    def is_verified(self, guardian_user_id, player_id):
        return any(link.verified
                   for link in self.store.guardian_links_for_player(player_id))

    def junior_ids(self, guardian_user_id):
        return sorted({link.player_id
                       for link in self.store.all_guardian_links()
                       if link.verified})

    cls.is_verified_guardian = is_verified
    cls.verified_junior_ids = junior_ids
    try:
        yield
    finally:
        cls.is_verified_guardian = real_is
        cls.verified_junior_ids = real_ids


@contextlib.contextmanager
def _a_guardian_admitted_to_any_junior():
    """``Handler._guardian_link_or_403`` neutered — the junior NAMED IN THE
    PATH is no longer checked against the caller's links at all. F1's
    dimension, and the one whose whole point is that the sweep must be
    BINDING that path argument in both directions to see it."""
    real = srv.Handler._guardian_link_or_403
    srv.Handler._guardian_link_or_403 = (
        lambda self, guardian_user_id, player_id: False)
    try:
        yield
    finally:
        srv.Handler._guardian_link_or_403 = real


class TheGrantIsKeyedByEveryDimensionOfItsRow(_SweepHarness,
                                              unittest.TestCase):
    """The declared dimensions, the typed residue, the measurement that
    separates them, and one required-RED falsifier per dimension."""

    def _reported(self, fn, *args):
        try:
            fn(*args)
        except AssertionError as exc:
            return str(exc)
        return None

    @contextlib.contextmanager
    def _oracle_blind_to(self, dimension):
        """THE ORACLE AS IT WAS BEFORE THIS ROUND, one dimension at a time.

        Each falsifier below is paired with this so it cannot be circular: a
        test that reddens on a widening the PREVIOUS oracle also caught is
        not measuring the change it claims to. An activation dimension is
        disabled by reading every row as live; a subject dimension by making
        the request supply no value for it, which is exactly the state F1
        and F3 were in."""
        real_dim = self._dimension_value
        real_active = globals()["_row_is_active"]
        real_memo = self._relationships
        self._relationships = None
        if dimension in _ACTIVATION_DIMENSIONS:
            globals()["_row_is_active"] = lambda record, row: True
        else:
            self._dimension_value = (
                lambda fx, principal, field, subjects:
                None if field == dimension
                else real_dim(fx, principal, field, subjects))
        try:
            yield
        finally:
            self._dimension_value = real_dim
            globals()["_row_is_active"] = real_active
            self._relationships = real_memo

    def _oracle_answer(self, fx, principal, subjects, private, submitted):
        """Everything both oracles say about one principal, over EVERY path
        the sweep binds — the number a dimension either moves or does not."""
        out = {}
        for name, arglists in sorted(subjects.items()):
            for args in arglists:
                at = self._path_subjects(fx, args)
                out[(name, args)] = (
                    tuple(sorted(self._permitted_ids(
                        fx, principal, name, at, private, submitted))),
                    tuple(sorted(self._entitled_teams(
                        fx, principal, name, at, SUBMITTED_LINEUP_DATA))),
                    tuple(sorted(self._entitled_teams(
                        fx, principal, name, at, TEAM_WORKFLOW_DATA))))
        return out

    # -- the DECLARED half -------------------------------------------------
    def test_every_field_of_every_grant_row_is_declared_one_way_or_the_other(
            self):
        """FAIL-CLOSED ON A GRANT ROW GAINING A FIELD.

        Not "the dimensions are these": the PARTITION is the property. Every
        field of every grant row is either derived into
        :data:`GRANT_DIMENSIONS` or carries a typed reason in
        :data:`GRANT_FIELDS_THAT_KEY_NOTHING`, and the two never overlap. A
        product change that adds a column therefore stops this test with the
        column's name in the message, which is what the route, role and
        query-parameter axes already do and what the grant rows did not."""
        self.assertEqual(frozenset(GRANT_RECORD_FIELDS),
                         frozenset(GRANT_DIMENSIONS),
                         "a class declares a grant row but no dimensions")
        for klass, record in sorted(GRANT_ROWS.items()):
            with self.subTest(row=record.__name__):
                fields = _record_fields(record)
                dims = GRANT_DIMENSIONS[klass]
                residue = frozenset(GRANT_FIELDS_THAT_KEY_NOTHING[record])
                self.assertEqual(
                    frozenset(), dims & residue,
                    f"{record.__name__}: a field is declared BOTH a keying "
                    f"dimension and a field that keys nothing")
                self.assertEqual(
                    fields, dims | residue,
                    f"{record.__name__} has a field that is neither a "
                    f"derived keying dimension nor typed as keying nothing: "
                    f"{sorted(fields - (dims | residue))}. A grant row that "
                    f"gains a dimension nobody keys on is the whole species "
                    f"this section exists for — name it, then MEASURE it.")
                self.assertTrue(
                    dims, f"{record.__name__} declares no dimensions at all")
                self.assertIn(
                    record, GRANT_ROW_WRITERS,
                    f"{record.__name__} has no entry in GRANT_ROW_WRITERS, "
                    f"so the measured audit below cannot move a single one "
                    f"of its fields and every dimension it declares is "
                    f"unfalsifiable")
                self.assertIn(
                    record, GRANT_ROW_READERS,
                    f"{record.__name__} has no unfiltered reader")
                for field, why in GRANT_FIELDS_THAT_KEY_NOTHING[record].items():
                    self.assertTrue(
                        why.strip(),
                        f"{record.__name__}.{field} carries an empty reason")
        # …and the ONE field no perturbation can express is exactly the
        # primary key, on every row. "Not measurable" cannot grow.
        self.assertEqual(frozenset({"id"}), GRANT_FIELDS_NOT_PERTURBABLE)
        for record in GRANT_ROWS.values():
            self.assertLessEqual(GRANT_FIELDS_NOT_PERTURBABLE,
                                 _record_fields(record))
            self.assertEqual(
                frozenset(),
                GRANT_FIELDS_NOT_PERTURBABLE & _keying_fields(record),
                f"{record.__name__}: a DIMENSION was excused from being "
                f"measured, which is how a dimension stops being keyed on")

    def test_every_subject_dimension_is_resolvable_from_a_path(self):
        """A subject dimension the sweep cannot RECOGNISE in a path is a
        dimension no binding can ever supply — F1 with the argument thrown
        away one layer earlier. Every ``_id`` field of every grant row, and
        every account-scope key, must have a store reader in
        :data:`_SweepHarness.SUBJECT_READERS`."""
        wanted = frozenset().union(
            *(_subject_fields(record) for record in GRANT_ROWS.values()),
            *(fields for klass, fields in GRANT_RECORD_FIELDS.items()
              if klass not in GRANT_ROWS))
        # A `*_user_id` names an ACCOUNT, which is not a subject any path in
        # this product addresses; it is resolved from the session instead.
        wanted = frozenset(f for f in wanted if not f.endswith("_user_id"))
        self.assertEqual(
            frozenset(), wanted - frozenset(self.SUBJECT_READERS),
            f"a grant row or account scope names a subject this sweep "
            f"cannot recognise in a path: "
            f"{sorted(wanted - frozenset(self.SUBJECT_READERS))}")
        store = InMemoryStore()
        try:
            fx = self._fixture(store)
            self._serve(fx)
            # …and every reader really resolves, on this fixture's own rows.
            for field, value in sorted(fx["other_subjects"].items()):
                if field.endswith("_user_id"):
                    continue
                resolved = self._path_subjects(fx, (value,))
                self.assertEqual(
                    value, resolved.get(field),
                    f"{field}={value} does not resolve back to {field}: "
                    f"{resolved}")
                # A subject may contribute MORE than its own id — a game
                # carries the competition keys a membership is keyed on
                # (:data:`SUBJECT_ROW_INHERITED`) — but only those, and only
                # a game. Anything else appearing here is a dimension this
                # sweep is silently reading off a row nobody declared it on.
                extra = frozenset(resolved) - {field}
                allowed = (SUBJECT_ROW_INHERITED if field == GAME_SUBJECT
                           else frozenset())
                self.assertEqual(
                    frozenset(), extra - allowed,
                    f"{field}={value} contributed the undeclared dimensions "
                    f"{sorted(extra - allowed)}")
        finally:
            store.clear_all_data()

    # -- the MEASURED half -------------------------------------------------
    def test_each_field_moves_the_oracles_iff_it_is_a_declared_dimension(
            self):
        """THE AUDIT IN BOTH DIRECTIONS, PER FIELD.

        For every field of every grant row that a perturbation can express,
        move it in the store — through :func:`_alternative_value`, which
        derives the substitute from the field's own declared type — and ask
        whether the oracles' answer over the whole swept surface changed.
        It must change for exactly the DECLARED dimensions:

        * a declared dimension that does NOT move the oracles is a dimension
          nothing is keyed on, which is F1, F2 and F3;
        * a residue field that DOES move them is an authority nobody
          declared.

        The oracles' own behaviour, not a per-backend property, so this runs
        on one ``InMemoryStore`` — same standing as the sibling audit in
        :class:`NoGrantIsAggregatedAcrossADimensionItIsKeyedOn`."""
        store = InMemoryStore()
        try:
            fx = self._fixture(store)
            self._serve(fx)
            specs, subjects = self._assert_inventory_is_closed(fx)
            subjects = {name: args for name, args in subjects.items()
                        if name in {s.name for s in specs}}
            private, _ambiguous = self._private_side_ids(fx)
            submitted = self._submitted_side_ids(fx)
            api = fx["api"]
            for klass, record in sorted(GRANT_ROWS.items()):
                principal = next(p for p in PRINCIPALS
                                 if self._entitlement(fx)[p][0] == klass)
                rows = self._grant_rows(fx, klass, principal, {})
                self.assertEqual(
                    1, len(rows),
                    f"[{record.__name__}] {principal} holds {len(rows)} "
                    f"grant rows, so 'move THE row' is ambiguous")
                row = rows[0]
                save = getattr(api.store, GRANT_ROW_WRITERS[record])
                base = self._oracle_answer(fx, principal, subjects, private,
                                           submitted)
                self.assertTrue(
                    any(any(v) for v in base.values()),
                    f"[{record.__name__}] {principal} is entitled to nothing "
                    f"anywhere before the perturbation, so nothing below can "
                    f"be observed to move")
                for field in sorted(_record_fields(record)
                                    - GRANT_FIELDS_NOT_PERTURBABLE):
                    with self.subTest(row=record.__name__, field=field):
                        current = getattr(row, field)
                        moved_to = _alternative_value(fx, record, field,
                                                      current)
                        self.assertNotEqual(
                            current, moved_to,
                            f"{record.__name__}.{field}: the substitute "
                            f"equals the current value, so this measures "
                            f"nothing")
                        save(dataclasses.replace(row, **{field: moved_to}))
                        try:
                            after = self._oracle_answer(
                                fx, principal, subjects, private, submitted)
                        finally:
                            save(row)
                        keyed = field in GRANT_DIMENSIONS[klass]
                        declared = ("a DECLARED DIMENSION" if keyed
                                    else "typed as keying NOTHING")
                        happened = ("changed" if after != base
                                    else "changed NOTHING")
                        self.assertEqual(
                            keyed, after != base,
                            f"{record.__name__}.{field} is {declared} and "
                            f"moving it {happened} "
                            f"in what the oracles grant {principal}. "
                            + ("A dimension the oracles do not key on is an "
                               "entitlement computed over a coarser key than "
                               "the response it governs — F1, F2 and F3 in "
                               "one sentence."
                               if keyed else
                               "A field declared to key nothing is keying "
                               "something, so the typed reason beside it in "
                               "GRANT_FIELDS_THAT_KEY_NOTHING is false."))
                        self.assertEqual(
                            base,
                            self._oracle_answer(fx, principal, subjects,
                                                private, submitted),
                            f"{record.__name__}.{field}: the restore did not "
                            f"put the world back, so every later field in "
                            f"this loop measures a different fixture")
                # …AND THE OTHER DIRECTION OF THE SAME QUESTION: hold the ROW
                # still and move what THE REQUEST says. Moving the row is not
                # enough on its own — `_permitted_ids` reads the junior off
                # the matching row, so re-pointing the row moves the answer
                # even for an oracle that ignores the path entirely, which is
                # exactly the state F1 was in. A dimension the REQUEST
                # supplies must select between grants.
                for field in sorted(_subject_fields(record)):
                    elsewhere = fx["other_subjects"][field]
                    if self._session_fixed(fx, principal, field):
                        continue   # no request can move it — audited in
                        # TheSweptBindingsExerciseTheUnentitledDirection
                    with self.subTest(row=record.__name__, request=field):
                        self.assertEqual(
                            elsewhere,
                            self._dimension_value(fx, principal, field,
                                                  {field: elsewhere}),
                            f"{record.__name__}.{field} is neither a "
                            f"`*_user_id` nor a key of {principal}'s account "
                            f"scope, so the REQUEST is the only thing that "
                            f"can supply it — and `_dimension_value` does "
                            f"not take it from the path. Nothing can name "
                            f"this dimension, which is F1 one layer earlier.")
                        self.assertTrue(
                            self._grant_rows(fx, klass, principal,
                                             {field: getattr(row, field)}),
                            f"{record.__name__}.{field}: the request naming "
                            f"the subject this row actually carries is "
                            f"granted NOTHING, so the real product grant is "
                            f"unreachable")
                        self.assertFalse(
                            self._grant_rows(fx, klass, principal,
                                             {field: elsewhere}),
                            f"{record.__name__}.{field}: a request naming "
                            f"{elsewhere} — a subject this row does NOT name "
                            f"— is granted this row anyway, so the oracle is "
                            f"not keyed on what the request says. That is F1 "
                            f"and F3 exactly.")
        finally:
            store.clear_all_data()

    # -- one required-RED falsifier per dimension --------------------------
    def test_a_declined_official_still_admitted_reddens_the_primary_sweep(
            self):
        """``status``. THE FALSIFIER THAT MAKES THE MODULE'S OWN LOCKSTEP
        CLAIM TRUE, required to go RED.

        The swept official DECLINES through the product's own write path, and
        the gate is restored to the predicate that ignores ``status``. The
        product serves them the sheet; the store's own row says the grant is
        dead; oracle 1 must report it.

        AND THE CONTROL: with the oracle blind to activation — the state this
        file was in at ``40a5e29``, where ``_official_is_assigned``
        reproduced the gate's predicate verbatim — the identical world is
        GREEN. Without that half this test would pass for an oracle that had
        not changed at all."""
        for label, store in self._stores():
            try:
                self._assert_backend(label, store)
                store.clear_all_data()
                fx = self._fixture(store)
                who = self._serve(fx)
                specs, subjects = self._assert_inventory_is_closed(fx)
                with self._revoked(fx, "official_assignment_declined"):
                    with _an_official_admitted_on_a_dead_assignment():
                        status, body = self._req(
                            who["official"], "GET",
                            f"/api/games/{fx['gid']}/lineups")
                        self.assertEqual(
                            status, 200,
                            "the restored predicate did not actually admit "
                            "the DECLINED official, so nothing below is a "
                            "statement about this sweep")
                        sweep = self._sweep(who, fx, specs, subjects)
                        reported = self._reported(
                            self._assert_no_foreign_ids, sweep, fx,
                            f"{label}/declined-official")
                        self.assertIsNotNone(
                            reported,
                            "THE PRIMARY SWEEP DID NOT CATCH AN OFFICIAL "
                            "SERVED THE PRIVATE SHEET OF A GAME THEY "
                            "DECLINED. `OfficialAssignmentStatus.is_active` "
                            "says that row grants nothing.")
                        self.assertIn("official", reported)
                        with self._oracle_blind_to("status"):
                            blind = self._reported(
                                self._assert_no_foreign_ids, sweep, fx,
                                f"{label}/status-blind")
                        self.assertIsNone(
                            blind,
                            "the ACTIVATION-blind oracle this round replaces "
                            "already catches the declined official, so this "
                            "test is not measuring the change it claims to: "
                            + str(blind))
            finally:
                self._close(label, store)
            return   # the oracle's own behaviour, not a per-backend property

    def test_admitting_an_official_on_another_officials_row_reddens_the_sweep(
            self):
        """``official_id``, required to go RED.

        The gate keeps the game and the status and drops WHO the row names.
        The second game carries an ACTIVE assignment belonging to the SECOND
        official, so the swept official is admitted to a game that is not
        theirs and served its sheet.

        AND THE CONTROL, which is the fixture rather than the oracle: with
        the second official's row removed — the fixture at ``40a5e29``, where
        ``gid2`` carried no assignment at all — the identical widening
        admits nobody new and the sweep is GREEN. That is why the second
        official exists."""
        for label, store in self._stores():
            try:
                self._assert_backend(label, store)
                store.clear_all_data()
                fx = self._fixture(store)
                who = self._serve(fx)
                specs, subjects = self._assert_inventory_is_closed(fx)
                api = fx["api"]
                other = [a for a in api.store.assignments_for_game(fx["gid2"])
                         if a.official_id == fx["official2_id"]]
                self.assertEqual(
                    1, len(other),
                    f"[{label}] the second game does not carry exactly one "
                    f"assignment for the second official")
                with _an_official_admitted_on_anyone_s_assignment():
                    status, _body = self._req(
                        who["official"], "GET",
                        f"/api/games/{fx['gid2']}/lineups")
                    self.assertEqual(
                        status, 200,
                        "the widening did not admit the swept official to "
                        "the second game, so nothing below is a statement "
                        "about this sweep")
                    sweep = self._sweep(who, fx, specs, subjects)
                    reported = self._reported(
                        self._assert_no_foreign_ids, sweep, fx,
                        f"{label}/anyones-assignment")
                    self.assertIsNotNone(
                        reported,
                        "THE PRIMARY SWEEP DID NOT CATCH AN OFFICIAL SERVED "
                        "THE SHEET OF A GAME WHOSE ONLY ASSIGNMENT NAMES "
                        "SOMEBODY ELSE.")
                    self.assertIn("official", reported)
                    # THE CONTROL: the round-10 fixture, in which no other
                    # official held a row anywhere.
                    self.assertNotIn(
                        "error", api.unassign_official(other[0].id,
                                                       actor_id=ADMIN))
                    try:
                        thin = self._sweep(who, fx, specs, subjects)
                        blind = self._reported(
                            self._assert_no_foreign_ids, thin, fx,
                            f"{label}/no-second-official")
                    finally:
                        back = api.assign_official(
                            fx["gid2"], fx["official2_id"], "referee",
                            actor_id=ADMIN)
                        self.assertNotIn("error", back, back)
                    self.assertIsNone(
                        blind,
                        "the fixture without a second official's assignment "
                        "already reddens under this widening, so the second "
                        "official is not what makes `official_id` "
                        "falsifiable: " + str(blind))
            finally:
                self._close(label, store)
            return   # the oracle's own behaviour, not a per-backend property

    def test_a_guardian_admitted_on_another_guardians_link_reddens_the_sweep(
            self):
        """``guardian_user_id``, required to go RED — F3.

        The swept guardian's own link is un-verified through the existing
        revocation; the SECOND guardian's link to the same junior stays
        verified; and ``GuardianService``'s two read helpers are widened to
        ignore WHO is asking. The product then serves the swept guardian a
        junior they hold no verified link to.

        AND THE CONTROL: an oracle that does not key on ``guardian_user_id``
        — the one at ``40a5e29``, which asked ``any(link.verified for link in
        guardian_links_for_player(junior))`` — is GREEN on the identical
        world."""
        for label, store in self._stores():
            try:
                self._assert_backend(label, store)
                store.clear_all_data()
                fx = self._fixture(store)
                who = self._serve(fx)
                specs, subjects = self._assert_inventory_is_closed(fx)
                with self._revoked(fx, "guardian_link"):
                    with _a_guardian_admitted_on_another_guardians_link():
                        status, body = self._req(
                            who["guardian"], "GET", "/api/me/guardian/home")
                        self.assertEqual(status, 200, body)
                        self.assertTrue(
                            body.get("juniors"),
                            "the widening did not actually serve the swept "
                            "guardian a junior, so nothing below is a "
                            "statement about this sweep")
                        sweep = self._sweep(who, fx, specs, subjects)
                        reported = self._reported(
                            self._assert_no_foreign_ids, sweep, fx,
                            f"{label}/another-guardians-link")
                        self.assertIsNotNone(
                            reported,
                            "THE PRIMARY SWEEP DID NOT CATCH A GUARDIAN "
                            "SERVED A JUNIOR ON THE STRENGTH OF SOMEBODY "
                            "ELSE'S VERIFIED LINK.")
                        self.assertIn("guardian", reported)
                        with self._oracle_blind_to("guardian_user_id"):
                            blind = self._reported(
                                self._assert_no_foreign_ids, sweep, fx,
                                f"{label}/guardian-identity-blind")
                        self.assertIsNone(
                            blind,
                            "the junior-keyed oracle this round replaces "
                            "already catches it, so this test is not "
                            "measuring the change it claims to: "
                            + str(blind))
            finally:
                self._close(label, store)
            return   # the oracle's own behaviour, not a per-backend property

    def test_a_guardian_admitted_to_another_junior_reddens_the_sweep(self):
        """``player_id``, required to go RED — F1, and the one that is about
        the SWEPT BINDING rather than about the oracle.

        ``Handler._guardian_link_or_403`` is neutered, so the junior named in
        the path is no longer checked. The route carries no identity, so this
        is ORACLE 2's to catch: the response is the HOME side's own roster
        status and open-slot counts, and it moves under a HOME perturbation
        for a caller entitled to nothing of that game.

        AND THE CONTROL IS THE BINDING: swept with round 10's bindings — the
        guardian's OWN junior in both rows — the identical widening moves
        nothing at all and the sweep is GREEN. That is F1 exactly: not a
        wrong rule, an argument the sweep never supplied."""
        results = {}
        # A FRESH WORLD PER BINDING SET, not two perturbations of one.
        # `_perturbed` is entered ONCE per (side, game, kind) in the primary
        # sweep, and its undo leaves the withdrawn enrolment row behind, so
        # re-entering it on one fixture aborts on its own premise rather
        # than measuring anything. Measured, not reasoned: the second entry
        # answers `not_enrolled`.
        for tag in ("bound", "round-10-bindings"):
            for label, store in self._stores():
                try:
                    self._assert_backend(label, store)
                    store.clear_all_data()
                    fx = self._fixture(store)
                    who = self._serve(fx)
                    specs, subjects = self._assert_inventory_is_closed(fx)
                    stranger = fx["unlinked_junior_id"]
                    if tag == "round-10-bindings":
                        subjects = dict(subjects)
                        subjects[
                            "get_me_guardian_id_substitute_opportunities_id"] = [
                                (fx["guardian_junior_id"], fx["gid"]),
                                (fx["guardian_junior_id"], fx["gid2"])]
                    with _a_guardian_admitted_to_any_junior():
                        status, _body = self._req(
                            who["guardian"], "GET",
                            f"/api/me/guardian/{stranger}/"
                            f"substitute-opportunities/{fx['gid']}")
                        self.assertEqual(
                            status, 200,
                            "the widening did not admit the guardian to a junior "
                            "they hold no link to, so nothing below is a "
                            "statement about this sweep")
                        base = self._sweep(who, fx, specs, subjects)
                        with self._perturbed(fx, fx["home"], fx["gid"],
                                             "substitute_enrolment"):
                            world = self._sweep(who, fx, specs, subjects)
                        results[tag] = self._reported(
                            self._assert_non_interference, base, world, fx,
                            fx["home"], f"{label}/{tag}", TEAM_WORKFLOW_DATA,
                            fx["gid"])
                finally:
                    self._close(label, store)
                break   # the oracle's own behaviour, not a per-backend one
        self.assertIsNotNone(
            results["bound"],
            "THE PRIMARY SWEEP DID NOT CATCH A GUARDIAN SERVED ANOTHER "
            "JUNIOR'S SIDE. The junior is a path argument the sweep binds, "
            "and binding only the direction the guardian IS entitled to is "
            "what made this invisible.")
        self.assertIn("guardian", results["bound"])
        self.assertIsNone(
            results["round-10-bindings"],
            "round 10's bindings — the guardian's OWN junior in both rows — "
            "already catch this, so the unentitled binding is not what makes "
            "`player_id` falsifiable: " + str(results["round-10-bindings"]))


# ---------------------------------------------------------------------------
# 11.6 EVERY PATH ARGUMENT IS SWEPT IN THE UNENTITLED DIRECTION TOO.
#
# F1 was only half a keying bug. The other half was that both swept bindings
# for the guardian route named the guardian's OWN junior, so however the
# oracle was keyed, the sweep never asked the question. A dimension the
# request can vary and the sweep never varies is a dimension no falsifier can
# reach — which is why this is audited against `GRANT_DIMENSIONS` rather than
# left to whoever adds the next binding.
# ---------------------------------------------------------------------------

#: ``{dimension: why the sweep binds no unentitled value for it}`` — typed,
#: and MEASURED by the test below rather than trusted.
DIMENSIONS_WITH_NO_PATH_DIRECTION = {
    "team_id":
        "no authenticated GET route in route_registry takes a TEAM id as a "
        "path argument — measured off the registry by the test below, not "
        "assumed — so there is no path for either direction of a coach's "
        "`team_id` to be bound in. What stands in for it is the GAME "
        "subject: `thirdcoach` is scoped to a team that plays in NEITHER "
        "game and is swept on every path in every world.",
}


#: ``{dimension: why the sweep binds exactly ONE direction of it, and where
#: the other direction is measured instead}`` — a limitation of the FIXTURE,
#: stated rather than absorbed, and machine-checked in BOTH halves.
#:
#: DISTINCT FROM :data:`DIMENSIONS_WITH_NO_PATH_DIRECTION`, which is for a
#: dimension no path supplies AT ALL. These two ARE supplied by a path — by
#: INHERITANCE from the subject game (:data:`SUBJECT_ROW_INHERITED`) — and
#: every path that supplies them supplies the SAME value, because both games
#: in this fixture hang off one LeagueSeason. That is a property
#: :meth:`TheDesignClassificationsAreStillTrue
#: .test_each_scoped_principals_side_is_the_same_in_both_games` depends on:
#: moving the second game to another LeagueSeason would leave both Players
#: with NO side in it and change what every world in the matrix means, which
#: is far past what LB1 asks for.
#:
#: SO THE UNENTITLED DIRECTION IS MEASURED ON THE REQUEST SIDE INSTEAD, and
#: the audit below re-runs that measurement here rather than pointing at it:
#: a request naming ``fx["other_subjects"][field]`` — a REAL second
#: LeagueSeason / Season this fixture holds — must be granted NOTHING, while
#: the value a swept path really binds must be granted the row. Both halves
#: run, so this entry cannot become a way to skip the dimension.
DIMENSIONS_THE_FIXTURE_BINDS_IN_ONE_DIRECTION = {
    "league_season_id":
        "no authenticated GET route names a LeagueSeason; the only paths "
        "that supply one supply it from the subject GAME's own row, and "
        "both swept games hang off `fx[\"ls_id\"]`. The unentitled "
        "direction is the second LeagueSeason the fixture holds for exactly "
        "this purpose (`fx[\"other_league_season_id\"]`), measured below "
        "and again by TheGrantIsKeyedByEveryDimensionOfItsRow.",
    "season_id":
        "the denormalized half of the same key — service-enforced equal to "
        "the LeagueSeason's own `season_id` — so it is bound in one "
        "direction for the same reason, and its unentitled direction is the "
        "sibling Season `fx[\"s2\"]`.",
}


class TheSweptBindingsExerciseTheUnentitledDirection(_SweepHarness,
                                                     unittest.TestCase):
    """Every grant dimension is either fixed by the SESSION — a path cannot
    vary it at all — or bound by the sweep in BOTH directions."""

    def test_every_grant_dimension_is_session_fixed_or_swept_both_ways(self):
        """THE PATH-ARGUMENT AUDIT (#427 round 11, item 5).

        For every dimension of every row-backed grant, ask which of the three
        sources :meth:`_dimension_value` resolves it from:

        * THE SESSION — a ``*_user_id``, or a key of this principal's own
          account scope. A path cannot make a caller somebody else, and that
          is asserted here by MEASURING that ``_dimension_value`` ignores a
          path that names a different one.
        * THE PATH — then the sweep must bind at least one value that DOES
          grant this principal the read and at least one that does NOT, or
          carry a typed entry in :data:`DIMENSIONS_WITH_NO_PATH_DIRECTION`.
          "Entitled" is not judged here: it is
          :meth:`_SweepHarness._grant_rows` answering, so the audit and the
          oracle cannot disagree about what a grant is."""
        store = InMemoryStore()
        try:
            fx = self._fixture(store)
            self._serve(fx)
            specs, subjects = self._assert_inventory_is_closed(fx)
            live = {s.name for s in specs}
            bound = [args for name, arglists in subjects.items()
                     if name in live for args in arglists]
            audited = set()
            for klass, record in sorted(GRANT_ROWS.items()):
                principal = next(p for p in PRINCIPALS
                                 if self._entitlement(fx)[p][0] == klass)
                for field in sorted(_subject_fields(record)):
                    audited.add(field)
                    elsewhere = fx["other_subjects"][field]
                    from_path = self._dimension_value(
                        fx, principal, field, {field: elsewhere})
                    with self.subTest(row=record.__name__, dimension=field):
                        if self._session_fixed(fx, principal, field):
                            # SESSION-FIXED, classified from the SOURCES:
                            # naming another subject in the path must not
                            # move the value at all.
                            self.assertNotEqual(
                                elsewhere, from_path,
                                f"{field} is supplied by the session, and a "
                                f"path naming {elsewhere} moved it — a "
                                f"request can make this caller somebody else")
                            self.assertIsNotNone(
                                from_path,
                                f"{field} is neither taken from the path nor "
                                f"fixed by the session, so no request can "
                                f"ever supply it and the dimension is dead")
                            continue
                        self.assertEqual(
                            elsewhere, from_path,
                            f"{field} is not session-fixed, so the PATH is "
                            f"the only thing that can supply it — and it "
                            f"does not reach the oracle. That is F1: the "
                            f"junior in "
                            f"/api/me/guardian/{{}}/substitute-opportunities/"
                            f"{{}} was discarded before any oracle saw it.")
                        values = {v for args in bound
                                  for k, v in self._path_subjects(
                                      fx, args).items() if k == field}
                        granting = {v for v in values if self._grant_rows(
                            fx, klass, principal, {field: v})}
                        if field in DIMENSIONS_WITH_NO_PATH_DIRECTION:
                            self.assertFalse(
                                values,
                                f"{field} carries a typed reason for having "
                                f"no path direction, and the sweep binds "
                                f"{sorted(values)} for it — delete the "
                                f"reason rather than keeping a false one")
                            continue
                        one_way = \
                            DIMENSIONS_THE_FIXTURE_BINDS_IN_ONE_DIRECTION
                        if field in one_way:
                            # THE ENTRY'S OWN PREMISE, re-measured: the sweep
                            # really does bind this dimension, and really
                            # does bind only the granting direction.
                            self.assertTrue(
                                values,
                                f"{field} is typed as bound in ONE "
                                f"direction and the sweep binds NO value "
                                f"for it at all — it belongs in "
                                f"DIMENSIONS_WITH_NO_PATH_DIRECTION")
                            self.assertEqual(
                                values, granting,
                                f"{field} is typed as bound in ONE "
                                f"direction and the sweep now binds "
                                f"{sorted(values - granting)} that grants "
                                f"nothing — the fixture gained the other "
                                f"direction, so delete the entry")
                            # …AND THE OTHER DIRECTION, on the request side,
                            # against a REAL second row. This is the whole
                            # substance of the excuse, so it runs here.
                            self.assertFalse(
                                self._grant_rows(fx, klass, principal,
                                                 {field: elsewhere}),
                                f"{field}: a request naming {elsewhere} — a "
                                f"real row this principal's grant is NOT "
                                f"keyed to — is granted the read anyway, so "
                                f"the dimension keys nothing and the typed "
                                f"reason is false")
                            continue
                        self.assertTrue(
                            granting,
                            f"{field}: the sweep binds no value that GRANTS "
                            f"{principal} the read, so the entitled "
                            f"direction is never swept and the real product "
                            f"grant is never exercised")
                        self.assertTrue(
                            values - granting,
                            f"{field}: EVERY value the sweep binds for this "
                            f"dimension grants {principal} the read, so the "
                            f"UNENTITLED direction is never swept. That is "
                            f"F1: the guardian's junior was bound to their "
                            f"own junior in both rows, and a widening that "
                            f"served another junior would have passed. Bind "
                            f"one, or add a typed entry to "
                            f"DIMENSIONS_WITH_NO_PATH_DIRECTION.")
            # …and every typed excuse names a real dimension, measured
            # against the registry rather than remembered.
            self.assertEqual(
                frozenset(),
                frozenset(DIMENSIONS_WITH_NO_PATH_DIRECTION)
                & frozenset(DIMENSIONS_THE_FIXTURE_BINDS_IN_ONE_DIRECTION),
                "a dimension is typed BOTH as having no path direction and "
                "as being bound in one — the two excuses say opposite "
                "things about the same fixture")
            for field, why in sorted(
                    DIMENSIONS_THE_FIXTURE_BINDS_IN_ONE_DIRECTION.items()):
                self.assertTrue(why.strip(), field)
                self.assertIn(
                    field, frozenset().union(*GRANT_DIMENSIONS.values()),
                    f"{field} is typed as bound in one direction but is not "
                    f"a grant dimension at all")
                self.assertIn(
                    field, audited,
                    f"{field} is typed as bound in one direction but this "
                    f"audit never reached it, so the excuse is dead")
            for field in DIMENSIONS_WITH_NO_PATH_DIRECTION:
                self.assertIn(
                    field,
                    frozenset().union(*GRANT_DIMENSIONS.values()),
                    f"{field} is excused from the path audit but is not a "
                    f"grant dimension at all")
            self.assertEqual(
                frozenset(DIMENSIONS_WITH_NO_PATH_DIRECTION) - audited,
                frozenset(DIMENSIONS_WITH_NO_PATH_DIRECTION)
                - frozenset().union(*(_subject_fields(r)
                                      for r in GRANT_ROWS.values())),
                "a typed excuse names a dimension this audit already covers")
        finally:
            store.clear_all_data()

    def test_a_path_cannot_override_a_dimension_the_session_binds(self):
        """THE PRECEDENCE DECISION, MEASURED (#427 round 12, LB1).

        ``_dimension_value`` resolves the session BEFORE the path, and that
        became load-bearing the moment ``SeasonRosterMembership`` was
        modelled: ``player_id`` is now BOTH a key of a Player's account scope
        and a path argument of the guardian route. The rule is that a
        dimension the READING PRINCIPAL'S OWN SCOPE binds is a fact about the
        caller and can never be overridden by the request; every other
        dimension is a fact about what is being read.

        BOTH DIRECTIONS ARE MEASURED HERE, on the same dimension, because a
        rule that only ever ran one way would be indistinguishable from
        "the scope always wins" or from "the path always wins"."""
        store = InMemoryStore()
        try:
            fx = self._fixture(store)
            self._serve(fx)
            stranger = fx["other_subjects"]["player_id"]
            mine = fx["scopes"]["homeplayer"]["player_id"]
            self.assertNotEqual(stranger, mine)
            # THE PLAYER: the scope binds `player_id`, so the path loses.
            self.assertEqual(
                mine,
                self._dimension_value(fx, "homeplayer", "player_id",
                                      {"player_id": stranger}),
                "a path naming another player moved the PLAYER's own grant "
                "dimension, so this oracle's answer is a function of a value "
                "the CLIENT chose — the property every other axis of this "
                "file exists to deny")
            self.assertTrue(self._session_fixed(fx, "homeplayer", "player_id"))
            # THE GUARDIAN: the scope binds no `player_id`, so the path wins
            # — the junior being READ is what the request names.
            self.assertEqual(
                stranger,
                self._dimension_value(fx, "guardian", "player_id",
                                      {"player_id": stranger}),
                "the guardian's `player_id` names the JUNIOR being read and "
                "the path is its only source; discarding it is F1")
            self.assertFalse(self._session_fixed(fx, "guardian", "player_id"))
            # THE PRODUCT'S HALF, which is why the session wins for a Player:
            # the resolver takes no request of any kind. Its parameters are
            # a session-resolved role, TWO IDS PROJECTED FROM THE SESSION'S
            # OWN SCOPE, the game the server already selected, and the store.
            #
            # RE-MEASURED IN ROUND 20 (#427). The mapping used to arrive
            # whole and is now projected into `scoped_team_id` and
            # `scoped_player_id` before any role branch — see
            # `resolve_private_game_read`'s "THE PROJECTION" note. What this
            # assertion is ABOUT is unchanged and is the reason it names the
            # parameters rather than counting them: not one of them can be
            # written by a request, so a path that named a `player_id` could
            # not reach the side this resolver answers.
            self.assertEqual(
                ["role", "scoped_team_id", "scoped_player_id", "game",
                 "store"],
                list(inspect.signature(
                    game_side_scope.game_scoped_own_team_id).parameters),
                "the trusted resolver's inputs changed; if a request can now "
                "reach it, `_dimension_value`'s scope-before-path rule is "
                "modelling an authority the product no longer has")
        finally:
            store.clear_all_data()

    def test_no_authenticated_get_route_takes_a_team_id_path_argument(self):
        """THE MEASUREMENT BEHIND THE ONE TYPED EXCUSE.

        ``team_id``'s entry in :data:`DIMENSIONS_WITH_NO_PATH_DIRECTION` is a
        claim about the REGISTRY, so it is re-measured every run: no
        authenticated GET route the sweep binds resolves any of its path
        arguments to a Team. The day one does, the excuse stops being true
        and this fails rather than the audit silently skipping a dimension."""
        store = InMemoryStore()
        try:
            fx = self._fixture(store)
            self._serve(fx)
            specs, subjects = self._assert_inventory_is_closed(fx)
            live = {s.name for s in specs}
            offenders = sorted(
                (name, args) for name, arglists in subjects.items()
                if name in live for args in arglists
                if "team_id" in self._path_subjects(fx, args))
            self.assertEqual(
                [], offenders,
                f"these swept paths DO name a team, so `team_id` has a path "
                f"direction after all and its typed excuse is false: "
                f"{offenders}")
            # The premise: a Team id really would be recognised if one
            # appeared. Otherwise this test passes because the resolver is
            # broken rather than because no route takes one.
            self.assertEqual(
                {"team_id": fx["third"]},
                self._path_subjects(fx, (fx["third"],)),
                "a team id no longer resolves to the team_id dimension, so "
                "the audit above cannot see one even if a route grew it")
        finally:
            store.clear_all_data()


# ---------------------------------------------------------------------------
# 12. THE SESSION-SCOPE AXIS IS CLOSED AGAINST WHAT ACCOUNTS ACCEPT.
#
# The sixth axis, found by enumerating them rather than by waiting for a
# round to trip over it (#427 round 9). Every principal in this sweep is a
# real signed-in session, and a session's authority is its ROLE plus its
# SCOPE BINDING. The role half has been closed against `domain.Role` since
# round 8. The scope half was hand-written in `_ProjectionHarness._serve` and
# compared to nothing: a scope key the product accepts, and this sweep never
# binds, is a shape of authority no oracle here has ever seen.
# ---------------------------------------------------------------------------

#: ``{(role, scope key): why this sweep does not drive it}`` — typed and
#: MEASURED, not a suppression list.
#:
#: The one entry is the one measurement:
#: ``AccountService._ALLOWED_SCOPE_KEYS`` accepts ``team_id`` for a PLAYER,
#: and #160 canonicalizes a Player scope to ``player_id`` ALONE. Measured
#: here rather than trusted to that comment: creating a Player account with
#: ``{"player_id": …, "team_id": …}`` stores ``{'player_id': 'player_6'}``.
#: So the pair is UNREACHABLE, not merely undriven — which is a different
#: and much stronger statement, and the reason it is recorded rather than
#: driven. :meth:`TheSessionScopeAxisIsClosedAgainstWhatAccountsAccept
#: .test_the_undriven_pair_is_unreachable_not_merely_unswept` re-measures it
#: every run, so the day canonicalization stops happening this stops being a
#: reason.
SCOPE_KEYS_NOT_DRIVEN = {
    (Role.PLAYER, "team_id"):
        "canonicalized away at account creation (#160): a Player scope is "
        "stored as player_id alone, so no live session can carry it",
}


class TheSessionScopeAxisIsClosedAgainstWhatAccountsAccept(
        _SweepHarness, unittest.TestCase):
    """A new scope binding the product accepts fails this test."""

    def test_every_accepted_scope_key_is_driven_or_typed(self):
        store = InMemoryStore()
        try:
            fx = self._fixture(store)
            self._serve(fx)
            driven = set()
            for account in fx["api"].accounts.list_accounts():
                if account.username not in PRINCIPALS:
                    continue
                for key, value in (account.scope or {}).items():
                    if value:
                        driven.add((account.role, key))
            accepted = {(role, key)
                        for role, keys
                        in AccountService._ALLOWED_SCOPE_KEYS.items()
                        for key in keys}
            self.assertTrue(
                accepted, "the scope authority is empty, so this closure "
                          "would pass vacuously")
            unaccounted = sorted(
                (str(role), key) for role, key in accepted - driven
                if (role, key) not in SCOPE_KEYS_NOT_DRIVEN)
            self.assertEqual(
                [], unaccounted,
                f"SCOPE BINDING(S) THE PRODUCT ACCEPTS AND THIS SWEEP NEVER "
                f"DRIVES: {unaccounted}. A session's authority is its role "
                f"AND its scope; a binding no principal here carries is a "
                f"shape of authority no oracle in this file has seen. Bind a "
                f"principal to it in `_serve`, or record a typed, MEASURED "
                f"reason in SCOPE_KEYS_NOT_DRIVEN.")
            stale = sorted((str(r), k) for r, k in
                           set(SCOPE_KEYS_NOT_DRIVEN) - accepted)
            self.assertEqual(
                [], stale,
                f"{stale} carries a reason for not being driven and the "
                f"product no longer accepts it at all")
            # …and the roles themselves are the ones this sweep drives.
            self.assertLessEqual(
                {role for role, _key in accepted}, set(PRINCIPAL_ROLES.values()),
                "the product scopes a role this sweep does not drive at all")
        finally:
            store.clear_all_data()

    def test_the_undriven_pair_is_unreachable_not_merely_unswept(self):
        """The recorded reason is a MEASUREMENT, re-run every time.

        If a Player scope ever stops being canonicalized to ``player_id``
        alone, ``(Role.PLAYER, "team_id")`` becomes a live authority shape
        this sweep does not drive, and the entry above stops being a reason.
        """
        store = InMemoryStore()
        try:
            fx = self._fixture(store)
            self._serve(fx)
            account = fx["api"].accounts.create_account(
                "scope_axis_probe", DEMO_PASSWORD, DEMO_USERS["player"],
                scope={"player_id": fx["people"]["awayside"]["id"],
                       "team_id": fx["home"]},
                actor_id="test_seed")
            self.assertEqual(
                {"player_id": fx["people"]["awayside"]["id"]},
                {k: v for k, v in (account.scope or {}).items() if v},
                "a Player account created with a team_id STORED it, so "
                "SCOPE_KEYS_NOT_DRIVEN's reason no longer holds and this "
                "sweep must drive a team-scoped Player")
        finally:
            store.clear_all_data()


# ---------------------------------------------------------------------------
# 13. THE METHOD AXIS IS A DISCLOSED LIMIT WHOSE BOUNDARY IS CLOSED.
#
# The seventh axis. This one CANNOT be closed the way the other six are: a
# POST-shaped sweep needs a request BODY per route, which is a different
# piece of work and not a line that can be added here. What CAN be closed --
# and is, below -- is the boundary of the limit, so that the limit cannot
# quietly grow:
#
#   * a NEW HTTP VERB in the registry fails this test, rather than being
#     silently outside a filter that only ever said `method == "GET"`;
#   * the counts the module docstring's limit 2 states are re-measured every
#     run, so the disclosed limit cannot become a stale number the way the
#     RUNTIME paragraph did.
#
# That is a weaker property than the other axes have and it is labelled as
# one. It is written down because "the sweep is GET-only" was already prose
# in the docstring with hand-counted numbers beside it, and prose with
# numbers in it is exactly what this round found to be wrong three times.
# ---------------------------------------------------------------------------

#: ``{MembershipStatus: (does a membership in this state admit its Player to
#: the private-game family, why)}`` — EVERY member of the product's own enum,
#: pinned by name.
#:
#: WHY A PIN EXISTS AT ALL WHEN THE PRODUCT ALREADY DECLARES THE PARTITION
#: (#427 round 12, LB1, close condition 3). The ORACLES read the product:
#: :func:`_row_is_active` asks :data:`ELIGIBILITY_AUTHORITIES`, which resolves
#: ``RosterService._ELIGIBLE_MEMBERSHIP_STATUSES`` LIVE. That is the right
#: authority for the oracle — a test-side copy driving the oracle is the
#: "predicate copied from the gate" failure round 11 records — and it has one
#: consequence that has to be answered rather than absorbed: a widening of
#: that tuple moves the gate AND the oracle together, so the oracle alone can
#: never report a status that becomes newly eligible.
#:
#: So this map is the SECOND, INDEPENDENT statement, and it is what makes a
#: newly-eligible status fail BY NAME:
#:
#: * a status the product GAINS is absent here — :meth:`
#:   ThePlayerGrantIsTheEligibleMembershipRow
#:   .test_every_membership_status_is_pinned_by_name` fails naming it,
#:   because the keys are checked against ``MembershipStatus`` itself;
#: * a status that becomes newly ELIGIBLE disagrees with this pin —
#:   :meth:`ThePlayerGrantIsTheEligibleMembershipRow
#:   .test_the_pin_and_the_products_own_declaration_agree` fails naming it;
#: * and neither is trusted: :meth:`ThePlayerGrantIsTheEligibleMembershipRow
#:   .test_the_ex_member_is_refused_and_a_blind_gate_reddens_the_sweep`
#:   DRIVES the swept Player's membership into every state the product will
#:   accept and MEASURES the admission over real authenticated HTTP, on all
#:   three backends.
#:
#: ELIGIBLE IS THE NARROW HALF, AND THAT IS THE PRODUCT'S OWN SENTENCE.
#: ``RosterService``'s comment: "An ACTIVE membership is the authoritative
#: stint; AFFILIATE is the governed call-up exception the #205 model defines
#: … applicant/inactive/injured hold no current participation, and terminal
#: rows are immutable history — none of them grants eligibility."
MEMBERSHIP_STATUS_GRANTS = {
    MembershipStatus.ACTIVE: (
        True, "the AUTHORITATIVE stint: at most one per (player, Season), "
              "enforced by migration 059's partial unique index"),
    MembershipStatus.AFFILIATE: (
        True, "the governed call-up exception the #205 epic names — "
              "secondary participation with exactly that Team, deliberately "
              "outside the authoritative-uniqueness rule"),
    MembershipStatus.APPLICANT: (
        False, "a pending request that occupies no roster place yet"),
    MembershipStatus.INACTIVE: (
        False, "PARKED — the membership-scoped successor of #270's "
               "`Player.is_active`. THE STATE LB1 IS ABOUT: this is the "
               "'ex-member' condition 4 names, and adding it to "
               "`_ELIGIBLE_MEMBERSHIP_STATUSES` is the one-enum widening "
               "that serves a departed player eight private HOME identities"),
    MembershipStatus.INJURED: (
        False, "temporarily unavailable; the stint stays open without being "
               "authoritative-active"),
    MembershipStatus.RELEASED: (
        False, "TERMINAL: the stint ended and the row is immutable history. "
               "Unreachable through `set_season_roster_membership_status`, "
               "which refuses every terminal transition unconditionally "
               "(#205 review round 2 owner ruling) — so the measurement "
               "below plants it at the store layer, exactly as the rest of "
               "this suite does"),
    MembershipStatus.TRANSFERRED: (
        False, "TERMINAL, as above — a later stint is a NEW row"),
}


@contextlib.contextmanager
def _a_player_admitted_on_an_ineligible_membership():
    """The #205 membership resolver WITHOUT its status test — d62473a's
    defect, on the row nobody had modelled.

    THE SHAPE IS THE OFFICIAL'S, ONE ROW OVER.
    ``_an_official_admitted_on_a_dead_assignment`` restores the gate's old
    predicate, which omitted ``OfficialAssignmentStatus.is_active``; this
    restores a membership resolution that omits
    ``_ELIGIBLE_MEMBERSHIP_STATUSES``. In both cases the PRODUCT'S DECLARED
    partition is untouched, so :func:`_row_is_active` — and therefore every
    oracle in this file — does NOT move with the widening, and the sweep is
    free to report it. That is why the check is deleted here rather than the
    declared tuple widened: widening the tuple moves the gate and the oracle
    in lockstep, which is the circularity this whole file is built to avoid,
    and it is instead caught by :data:`MEMBERSHIP_STATUS_GRANTS` by name.

    The widened window lives strictly INSIDE one resolver call and is
    restored before it returns, so nothing that reads the tuple from outside
    — the oracles included — can observe it."""
    real = RosterService.resolve_membership_context

    def widened(self, game, player):
        context = real(self, game, player)
        if context is not None:
            return context
        keep = RosterService._ELIGIBLE_MEMBERSHIP_STATUSES
        RosterService._ELIGIBLE_MEMBERSHIP_STATUSES = tuple(MembershipStatus)
        try:
            return real(self, game, player)
        finally:
            RosterService._ELIGIBLE_MEMBERSHIP_STATUSES = keep

    RosterService.resolve_membership_context = widened
    try:
        yield
    finally:
        RosterService.resolve_membership_context = real


class ThePlayerGrantIsTheEligibleMembershipRow(_SweepHarness,
                                               unittest.TestCase):
    """LB1's close conditions 2, 3 and 4, in one tri-store fixture.

    A Player's side is an ELIGIBLE :class:`SeasonRosterMembership` at the
    exact game-season — and this is where that stops being a sentence: the
    ex-member is driven into being one through the product's own write path,
    the refusal is measured over real authenticated HTTP on Memory, SQLite
    and real PostgreSQL, and the PRIMARY ORACLE is required to go RED when
    the gate stops reading the status."""

    #: The whole private-game family, from the registry rather than listed —
    #: an ex-member must be refused EVERY leaf, not the three that carry a
    #: sheet.
    #:
    #: DERIVED FROM THE PATH, NOT FROM THE ROUTE NAME (#427 round 17). This
    #: read `spec.name.startswith("get_games_id_")`, which is a naming
    #: CONVENTION rather than an authority: a family leaf whose route name
    #: did not happen to begin that way was outside this refusal entirely.
    #: :func:`private_game_family` is the one definition now.
    def _family(self):
        return sorted(self._private_game_family())

    CASES = ["an_ex_member_is_refused_the_whole_family",
             "every_status_admits_where_the_product_pins_it",
             "a_gate_blind_to_membership_status_reddens_oracle_one"]

    # -- the pin, checked against the product ------------------------------
    def test_every_membership_status_is_pinned_by_name(self):
        """FAIL-CLOSED ON THE PRODUCT GAINING A STATUS. The keys are checked
        against ``MembershipStatus`` itself, so a new member is an error
        NAMING IT rather than a state nobody decided about."""
        self.assertEqual(
            frozenset(MembershipStatus), frozenset(MEMBERSHIP_STATUS_GRANTS),
            "MembershipStatus and this file's pin disagree about which "
            "states exist; a state nobody has classified is a state that "
            "can be silently seated or silently refused")
        for status, (_grants, why) in MEMBERSHIP_STATUS_GRANTS.items():
            self.assertTrue(why.strip(), f"{status.value} carries no reason")

    def test_the_products_two_tuples_still_partition_the_enum(self):
        """The PRODUCT's own half. ``RosterService``'s two tuples must
        partition ``MembershipStatus``, which is the property
        :data:`ELIGIBILITY_AUTHORITIES` relies on to read the ELIGIBLE half
        as an answer rather than as a guess: a status in neither tuple would
        be silently ineligible to :func:`_row_is_active` while the product
        had decided nothing about it."""
        eligible = frozenset(RosterService._ELIGIBLE_MEMBERSHIP_STATUSES)
        ineligible = frozenset(RosterService._INELIGIBLE_MEMBERSHIP_STATUSES)
        self.assertEqual(frozenset(), eligible & ineligible)
        self.assertEqual(frozenset(MembershipStatus), eligible | ineligible)
        self.assertEqual((eligible, ineligible),
                         _eligibility_partition(MembershipStatus))

    def test_the_pin_and_the_products_own_declaration_agree(self):
        """THE BY-NAME FAILURE FOR A NEWLY ELIGIBLE STATUS.

        Two independent statements about the same question: this file's pin,
        and ``_ELIGIBLE_MEMBERSHIP_STATUSES``. Widening the product tuple by
        one enum member — the one-enum widening that reproduced LB1 — makes
        them disagree, and the message names the status."""
        pinned = frozenset(s for s, (grants, _why)
                           in MEMBERSHIP_STATUS_GRANTS.items() if grants)
        declared = frozenset(RosterService._ELIGIBLE_MEMBERSHIP_STATUSES)
        self.assertEqual(
            pinned, declared,
            f"newly ELIGIBLE: {sorted(s.value for s in declared - pinned)}; "
            f"newly INELIGIBLE: {sorted(s.value for s in pinned - declared)}."
            f" A status that changes side changes WHO MAY READ A GAME'S "
            f"PRIVATE STATE — decide it here, with a reason, rather than "
            f"letting the oracles follow the gate wherever it goes.")

    # -- the measured half, tri-store --------------------------------------
    def _membership_of(self, fx, principal):
        """The swept Player's membership for the FIRST game, resolved by the
        product's own context resolver — never picked out of the store by
        hand, so this is the exact row the gate reads."""
        api = fx["api"]
        player = api.store.get_player(fx["scopes"][principal]["player_id"])
        context = RosterService(api.store).resolve_membership_context(
            api.store.get_game(fx["gid"]), player)
        self.assertIsNotNone(
            context, f"{principal} holds no membership context for the "
                     f"first game, so there is no ex-member to make")
        return context.membership

    def _set_status(self, fx, membership_id, status):
        """Drive the membership through the PRODUCT'S OWN write path.

        A terminal status has no write path at all — ``set_season_roster_
        membership_status`` refuses every terminal transition
        unconditionally — so the product's REFUSAL is asserted and the row is
        then planted at the store layer by the same helper the rest of this
        suite uses. That refusal is itself part of the measurement: a state
        the product will not let a membership reach is a stronger statement
        than a 403."""
        api = fx["api"]
        if status.is_terminal:
            with self.assertRaises(Exception) as caught:
                api.setup.set_season_roster_membership_status(
                    membership_id, status.value, actor_id=ADMIN)
            self.assertIn("NotAuthorized", type(caught.exception).__name__,
                          f"the product now ALLOWS a terminal transition to "
                          f"{status.value} through its own write path")
            return end_membership_directly(api.store, membership_id,
                                           status.value)
        return api.setup.set_season_roster_membership_status(
            membership_id, status.value, actor_id=ADMIN)

    def _restore_active(self, fx, membership_id):
        api = fx["api"]
        row = api.store.get_season_roster_membership(membership_id)
        if row.status.is_terminal:
            # An immutable row cannot be revived through the service either,
            # so the store puts it back exactly as it planted it.
            end_membership_directly(api.store, membership_id,
                                    MembershipStatus.ACTIVE.value)
        elif row.status is not MembershipStatus.ACTIVE:
            api.setup.set_season_roster_membership_status(
                membership_id, MembershipStatus.ACTIVE.value, actor_id=ADMIN)
        self.assertEqual(
            MembershipStatus.ACTIVE,
            api.store.get_season_roster_membership(membership_id).status)

    def _foreign_ids_in(self, fx, body, permitted):
        """Every identity PRIVATE TO EITHER SIDE that this body names and
        this caller is not permitted — oracle 1's own question, asked of one
        response."""
        private, _ambiguous = self._private_side_ids(fx)
        blob = json.dumps(body, sort_keys=True, default=str)
        forbidden = (private[fx["home"]] | private[fx["away"]]) - permitted
        return sorted(pid for pid in forbidden
                      if re.search(rf"\b{re.escape(pid)}\b", blob))

    def test_the_ex_member_is_refused_and_a_blind_gate_reddens_the_sweep(
            self):
        ran = []
        for label, store in self._stores():
            try:
                self._assert_backend(label, store)
                store.clear_all_data()
                fx = self._fixture(store)
                who = self._serve(fx)
                specs, subjects = self._assert_inventory_is_closed(fx)
                membership = self._membership_of(fx, "homeplayer")
                family = self._family()
                self.assertGreaterEqual(len(family), 10, family)
                gid = self._subject_of(fx, fx["gid"])
                with self.subTest(backend=label):
                    # THE PREMISE: while the membership is ACTIVE the Player
                    # really does read their own side, so a 403 below is the
                    # membership being withdrawn and not a broken fixture.
                    status, body = self._req(
                        who["homeplayer"], "GET",
                        f"/api/games/{fx['gid']}/board")
                    self.assertEqual(status, 200, (label, body))
                    self.assertTrue(
                        self._entitled_teams(
                            fx, "homeplayer", "get_games_id_board", gid,
                            SUBMITTED_LINEUP_DATA),
                        f"[{label}] the ORACLE grants an ACTIVE member no "
                        f"side, so its collapse below would prove nothing")

                    # ---- CONDITION 4: the ex-member, through the product's
                    # own write path, refused the WHOLE family with no
                    # private identity anywhere.
                    self._set_status(fx, membership.id,
                                     MembershipStatus.INACTIVE)
                    self.assertEqual(
                        frozenset(),
                        self._entitled_teams(
                            fx, "homeplayer", "get_games_id_board", gid,
                            SUBMITTED_LINEUP_DATA),
                        f"[{label}] the PRIMARY ORACLE still grants an "
                        f"ex-member their old side. That is LB1 exactly: the "
                        f"product narrows 200 -> 403 and the oracle does not "
                        f"move, so a gate that stopped narrowing would be "
                        f"invisible.")
                    for leaf_name in family:
                        spec = next(s for s in specs if s.name == leaf_name)
                        for args in subjects[leaf_name]:
                            path = self._path_of(spec, args)
                            status, body = self._req(
                                who["homeplayer"], "GET", path)
                            self.assertEqual(
                                status, 403,
                                f"[{label}] an EX-MEMBER — a membership the "
                                f"product itself made ineligible — was "
                                f"admitted to {path}: {body}")
                            self.assertEqual(
                                [], self._foreign_ids_in(fx, body,
                                                         frozenset()),
                                f"[{label}] {path} handed an ex-member a "
                                f"side-private identity")
                    ran.append((label, self.CASES[0]))

                    # ---- CONDITION 3: every status, measured where the
                    # product pins it.
                    for status_value in sorted(MembershipStatus,
                                               key=lambda m: m.value):
                        with self.subTest(backend=label,
                                          status=status_value.value):
                            self._restore_active(fx, membership.id)
                            if status_value is not MembershipStatus.ACTIVE:
                                self._set_status(fx, membership.id,
                                                 status_value)
                            code, body = self._req(
                                who["homeplayer"], "GET",
                                f"/api/games/{fx['gid']}/board")
                            grants, why = MEMBERSHIP_STATUS_GRANTS[
                                status_value]
                            self.assertEqual(
                                200 if grants else 403, code,
                                f"[{label}] a membership the product records "
                                f"as {status_value.value!r} answered "
                                f"{code} on /board, and this file pins that "
                                f"state as "
                                f"{'ELIGIBLE' if grants else 'INELIGIBLE'} "
                                f"({why}). One of the two is wrong, and "
                                f"which one is a product decision.")
                            self.assertEqual(
                                grants,
                                bool(self._entitled_teams(
                                    fx, "homeplayer", "get_games_id_board",
                                    gid, SUBMITTED_LINEUP_DATA)),
                                f"[{label}] the ORACLE and the PRODUCT "
                                f"disagree about {status_value.value!r}")
                    self._restore_active(fx, membership.id)
                    ran.append((label, self.CASES[1]))

                    # ---- THE RED FALSIFIER: a gate that stops reading the
                    # membership status, on an ex-member, must be reported by
                    # the PRIMARY oracle.
                    self._set_status(fx, membership.id,
                                     MembershipStatus.INACTIVE)
                    try:
                        with _a_player_admitted_on_an_ineligible_membership():
                            code, body = self._req(
                                who["homeplayer"], "GET",
                                f"/api/games/{fx['gid']}/board")
                            self.assertEqual(
                                200, code,
                                f"[{label}] the injected widening did not "
                                f"re-admit the ex-member, so nothing below "
                                f"measures a leak: {body}")
                            served = self._foreign_ids_in(fx, body,
                                                          frozenset())
                            self.assertTrue(
                                served,
                                f"[{label}] the re-admitted ex-member "
                                f"received NO side-private identity, so the "
                                f"falsifier carries no disclosure")
                            sweep = self._sweep(who, fx, specs, subjects)
                            reported = None
                            try:
                                self._assert_no_foreign_ids(
                                    sweep, fx, f"{label}/ex-member")
                            except AssertionError as exc:
                                reported = str(exc)
                            self.assertIsNotNone(
                                reported,
                                f"[{label}] THE PRIMARY SWEEP DID NOT CATCH "
                                f"AN EX-MEMBER SERVED {len(served)} PRIVATE "
                                f"IDENTITIES OF A SIDE THEY LEFT: "
                                f"{served}. This is d62473a's failure on the "
                                f"row nobody modelled, and closing LB1 means "
                                f"exactly this going RED.")
                            self.assertIn("homeplayer", reported)
                    finally:
                        self._restore_active(fx, membership.id)
                    ran.append((label, self.CASES[2]))
            finally:
                self._close(label, store)
        self._assert_matrix_ran(ran, self.CASES)


# ---------------------------------------------------------------------------
# 12. EVERY ADMISSION BRANCH IS DERIVED FROM THE GATE AND CARRIES AN
#     AUTHORITY — AND THE DERIVATION IS PROVED BY INJECTING ONE.
#
# LB1's close condition 5: "Make a new non-admin admission branch fail unless
# it has an authority mapping — without relying on another unaudited
# GRANT_ROWS list."
#
# The audit below never reads `GRANT_ROWS`' keys to decide WHICH branches
# exist. It asks `admission_branches()`, which reads the GATE. `GRANT_ROWS`
# is then only an ANSWER to the branches the gate was found to take, and a
# branch with no answer fails naming the role and the line.
# ---------------------------------------------------------------------------
class EveryAdmissionBranchIsDerivedAndCarriesAnAuthority(_SweepHarness,
                                                        unittest.TestCase):
    """The set of admission branches is DERIVED; the authority behind each is
    DECLARED; and a branch the gate gains fails by name until somebody
    decides what admits it.

    Pure source analysis plus one in-memory fixture for every claim about
    the gate's TEXT, which is not a per-backend property — and ONE tri-store
    loop, :meth:`test_the_rebuilt_scope_disclosure_is_not_live`, which is
    the claim about the PRODUCT that stands beside round 18's new static
    refusal (#427 round 18, blocker 2)."""

    #: The case name for this class's one tri-store loop, so
    #: `_assert_matrix_ran` fails a loop that silently covered fewer
    #: backends than were configured.
    REBUILT_SCOPE_CASE = "thirdcoach_is_refused_the_whole_family"

    #: Where an injected branch is spliced in — the first role test in the
    #: carrier that a non-operator role actually reaches, so an injected
    #: branch is genuinely live rather than shadowed by an earlier return.
    ANCHOR = "    if role in (Role.COACH, Role.PLAYER):"

    #: A branch that admits its caller to a REAL game with a REAL side. The
    #: injected branches all end here, so what differs between them is only
    #: HOW THE ROLE IS SPELLED, which is the thing under test.
    ADMITTING_BODY = (
        "        return PrivateGameRead(role=role, game=game,\n"
        "                               own_team=game.home_team_id,\n"
        "                               admitted=True)\n")

    #: The two statements of the real COACH/PLAYER branch a mutation aimed at
    #: the DELEGATION model has to replace — the resolver call and the
    #: participation test it takes on the answer.
    DELEGATION = (
        "        own_team = game_scoped_own_team_id(role, scoped_team_id,\n"
        "                                           scoped_player_id, game, "
        "store)\n"
        "        admitted = own_team is not None and own_team in (\n"
        "            game.home_team_id, game.away_team_id)\n")

    #: The delegation's FIRST line alone — the resolver call whose ARGUMENT
    #: LIST is what round 16 vouches for. Split out so a mutation can forge
    #: an argument without touching the participation test that follows it,
    #: which is exactly the shape all three pins are blind to.
    RESOLVER_CALL = (
        "        own_team = game_scoped_own_team_id(role, scoped_team_id,\n"
        "                                           scoped_player_id, game, "
        "store)\n")

    #: THE PROJECTION (#427 round 20): the gate's normalisation of the
    #: session scope AND the three reads that are now the only reads of it.
    #: Anchored INCLUDING the first projection line, because after the
    #: refactor ``scope = scope or {}`` appears exactly once in the module
    #: (the resolver and the player helper no longer normalise anything) and
    #: an injection has to be able to land above, between or below the three.
    THE_PROJECTION = ('    scope = scope or {}\n'
                      '    scoped_team_id = scope.get("team_id")\n'
                      '    scoped_player_id = scope.get("player_id")\n'
                      '    scoped_official_id = scope.get("official_id")\n')

    #: The gate's own normalisation ALONE, anchored on the line after it —
    #: where round 19's class-A mutations were spelled and where this round's
    #: RELOCATION, ALIAS and LIVE-``game`` regressions are spelled too.
    NORMALISED_SCOPE = ("    scope = scope or {}\n"
                        "    scoped_team_id = scope.get(\"team_id\")")

    #: THE RESOLVER'S FIRST DECISION. Round 19's class B was anchored on the
    #: resolver's own ``scope = scope or {}``; the resolver has no mapping to
    #: normalise any more, so the anchor is its first role test — which is
    #: where a resolver-side REBUILD of a PROJECTED id goes instead, and that
    #: is the seam condition 7 keeps (see
    #: :meth:`test_a_resolver_that_rebuilds_its_own_parameter_is_refused`).
    RESOLVER_FIRST_TEST = ("    if role == Role.COACH:\n"
                           "        return scoped_team_id")

    #: What the PLAYER authority ultimately answers — the membership spine.
    PLAYER_HELPER_ANSWER = ("    return RosterService(store).team_for_game("
                            "game, player)\n")

    #: …and the helper's OWN first statement. Round 19's eleventh spelling
    #: mutated the mapping HERE, one call deeper than either class the review
    #: named. There is no mapping here any more — the helper takes the
    #: projected id — so the fold-drop rule is exercised on the id instead.
    PLAYER_HELPER_GUARD = ("    if not scoped_player_id:\n"
                           "        return None\n")

    #: The OFFICIAL branch's whole admission — the statement whose earlier
    #: spelling (``official_id = scope.get("official_id")``) was the one the
    #: relocation hoisted.
    OFFICIAL_ADMISSION = (
        "        admitted = scoped_official_id is not None and any(\n"
        "            assignment_grants_official_scope(a, "
        "scoped_official_id)\n"
        "            for a in store.assignments_for_game(game_id))\n")

    #: The gate's one line that selects the SUBJECT of the request — the
    #: game the caller asked about, fetched by the id the gate was handed.
    SUBJECT_FETCH = "    game = store.get_game(game_id)\n"

    #: …and the decision it then returns. Split from :data:`DELEGATION` so a
    #: mutation can move one without the other, which is the whole point of
    #: the fourth model: what the branch RESOLVES and what it RETURNS are two
    #: things, and only the first of them used to be audited.
    DELEGATED_DECISION = (
        "        return PrivateGameRead(role=role, game=game,\n"
        "                               own_team=own_team if admitted "
        "else None,\n"
        "                               admitted=admitted)\n")

    def _gate_source(self):
        return Path(inspect.getsourcefile(game_side_scope)).read_text()

    def _audit(self, source=None):
        """The rule, in one place, applied to the real gate or to a mutated
        copy — so the injections below are checked by the SAME code that
        guards the real one, never by a re-statement of it.

        Returns a list of NAMED failures; empty means the axis is closed."""
        try:
            branches = admission_branches(source=source)
        except AdmissionExtractionError as exc:
            return [f"unresolvable admission shape — {exc}"]
        failures = []
        for role, decisions in branches.items():
            for branch in decisions:
                if not branch.needs_authority:
                    continue
                if branch.authority == "True":
                    # ADMITTED BEFORE ANY AUTHORITY IS CONSULTED. Legitimate
                    # for an operator of the competition; for anybody else it
                    # is a branch with nothing behind it at all, which no
                    # mapping could answer for.
                    if role in OPERATOR_ROLES:
                        continue
                    failures.append(
                        f"{role} is ADMITTED UNCONDITIONALLY at line "
                        f"{branch.lineno} of {GATE_CARRIER} and holds no "
                        f"{OPERATOR_PERMISSION.value} permission, so nothing "
                        f"decides whether this caller may read the game")
                    continue
                declared = ADMISSION_AUTHORITIES.get(role)
                if declared is None:
                    failures.append(
                        f"{role} is admitted at line {branch.lineno} of "
                        f"{GATE_CARRIER} by {branch.authority!r} and has no "
                        f"entry in ADMISSION_AUTHORITIES, so this sweep "
                        f"models no grant behind it — every oracle in this "
                        f"file would answer for it out of a class that "
                        f"describes somebody else")
                    continue
                # THREE PINS, NOT ONE — see `ADMISSION_AUTHORITIES`. What
                # RESOLVES the side, what THIS BRANCH tests, and what side
                # THIS BRANCH returns are three separate judgements, and a
                # branch that delegates to the audited resolver and then
                # ignores its answer moves only the last two.
                for what, found, pinned in (
                        ("resolved by", branch.authority, declared.authority),
                        ("admitted by", branch.admits_source,
                         declared.admits),
                        ("answering the side",
                         branch.side_source, declared.side)):
                    if found == pinned:
                        continue
                    failures.append(
                        f"{role} at line {branch.lineno} of {GATE_CARRIER} "
                        f"is now {what} {found!r}, and its authority was "
                        f"classified against {pinned!r}. The classification "
                        f"is a judgement about a specific expression; "
                        f"re-decide it rather than inheriting it")
        return failures

    # -- the real gate -----------------------------------------------------
    def test_every_admission_branch_the_gate_takes_carries_an_authority(self):
        """THE AXIS ITSELF, against the real
        ``services/game_side_scope.py``."""
        self.assertEqual([], self._audit(),
                         "\n".join(self._audit()))

    # -- THE OFFICIAL GRANT: the pin follows the predicate, and the grant
    #    is proved BY BEHAVIOUR against the product enum (#427 round 15) ---
    #
    # `62a52b7` moved the Official's grant out of this branch and into
    # `subject_scope.assignment_grants_official_scope`, shared with the two
    # context projections. Two separate things then have to be true, and
    # NEITHER implies the other:
    #
    #  1. the PIN must see what is decided, not the name of the thing that
    #     decides it — `_inlined`, proved by
    #     `test_the_official_pin_follows_the_predicate_the_branch_calls`;
    #  2. the DECISION must still be the status. Text cannot carry that: an
    #     expression can be rewritten while still testing status, and can
    #     keep every character while testing nothing. So it is driven.

    #: ``{OfficialAssignmentStatus member NAME: does it grant scope}`` —
    #: DECIDED HERE, member by member, and required to equal what the
    #: product enum's own ``is_active`` says. Two pins in one: a status
    #: RECLASSIFIED in the product, or a NEW member added to it, fails by
    #: name here instead of being inherited by an Official's admission.
    OFFICIAL_STATUS_GRANTS = {"PROPOSED": True,
                              "ACCEPTED": True,
                              "DECLINED": False}

    #: The Official the driven rows below belong to.
    GRANTEE = "official_under_test"

    def _assignment(self, status):
        """A REAL ``OfficialAssignment`` row carrying ``status``."""
        return OfficialAssignment(id="oa_under_test", game_id="g_under_test",
                                  official_id=self.GRANTEE,
                                  role=OfficialRole.REFEREE, status=status)

    def _assert_the_grant_is_the_status(self, predicate, members):
        """``predicate`` answers every member of ``members`` with exactly
        what :data:`OFFICIAL_STATUS_GRANTS` says that member grants, and that
        table is what the enum itself says.

        NO ``subTest`` HERE ON PURPOSE: a failure inside one is recorded on
        the result rather than raised, and the three falsifiers below have to
        CATCH this failing. Each half instead names the offending members in
        its own message."""
        unclassified = sorted(m.name for m in members
                              if m.name not in self.OFFICIAL_STATUS_GRANTS)
        self.assertEqual(
            [], unclassified,
            f"{unclassified} are members of the assignment-status enum that "
            f"nothing has decided a grant for, so an Official holding one "
            f"would be admitted or refused by whatever the predicate happens "
            f"to answer — decide it")
        reclassified = {m.name: m.is_active for m in members
                        if m.is_active != self.OFFICIAL_STATUS_GRANTS[m.name]}
        self.assertEqual(
            {}, reclassified,
            f"the product's own OfficialAssignmentStatus.is_active now "
            f"classifies {reclassified}, and this file's grant table says "
            f"otherwise. `is_active` is what the whole private-game family "
            f"and both context projections read, so re-decide the grant")
        answered = {m.name: predicate(self._assignment(m), self.GRANTEE)
                    for m in members}
        disagrees = sorted(name for name, granted in answered.items()
                           if granted is not self.OFFICIAL_STATUS_GRANTS[name])
        self.assertEqual(
            [], disagrees,
            f"the predicate an Official's admission is decided by answers "
            f"{answered}, and {disagrees} is not what that status grants. "
            f"d62473a exists because this check was missing and a DECLINED "
            f"official kept 200 on /board, /lineups and /roster")

    def test_the_official_grant_answers_the_status_enum_member_by_member(
            self):
        """THE SEMANTIC HALF OF THE OFFICIAL PIN — driven, never matched.

        The owner's ruling on the centralisation is explicit that following
        the shared predicate is not enough on its own and that "function-
        object identity or a function-name ledger alone is insufficient":
        `test_official_scope_grant_predicate
        .test_one_predicate_is_shared_by_all_three_call_sites` proves the
        three surfaces resolve the SAME function object, which proves they
        share A predicate and says nothing whatever about what it tests.

        So the product predicate is DRIVEN, against a real
        ``OfficialAssignment`` row per member of the real
        ``OfficialAssignmentStatus``, and its answer must equal that
        member's own ``is_active``. `_assert_the_grant_is_the_status` is
        what the three falsifiers in
        `test_the_status_grant_fails_by_name_three_ways` remove a piece of,
        one at a time."""
        self.assertEqual(
            frozenset(self.OFFICIAL_STATUS_GRANTS),
            frozenset(OfficialAssignmentStatus.__members__),
            "the grant table and the product's assignment-status enum name "
            "different sets of statuses")
        self._assert_the_grant_is_the_status(
            subject_scope.assignment_grants_official_scope,
            list(OfficialAssignmentStatus))
        # THE OTHER CONJUNCT, so "equals is_active" cannot be satisfied by a
        # predicate that has stopped caring WHOSE assignment it is reading —
        # `resolve_private_game_read` iterates `assignments_for_game`, so
        # every Official on the sheet reaches every row.
        for status in OfficialAssignmentStatus:
            self.assertFalse(
                subject_scope.assignment_grants_official_scope(
                    self._assignment(status), "a_different_official"),
                f"a {status.name} assignment belonging to somebody else "
                f"granted scope, so the exact-Official half of the predicate "
                f"is not being tested by the status half passing")
        self.assertFalse(
            subject_scope.assignment_grants_official_scope(None,
                                                           self.GRANTEE),
            "a missing assignment must grant nothing")
        self.assertFalse(
            subject_scope.assignment_grants_official_scope(
                self._assignment(OfficialAssignmentStatus.ACCEPTED), None),
            "a caller with no official_id must grant nothing")

    def test_the_status_grant_fails_by_name_three_ways(self):
        """THE FALSIFIER FOR THE ASSERTION ABOVE — all three ways the grant
        can stop being the status, each required to fail BY NAME.

        A refusal nothing can falsify may already have stopped biting, which
        is the standard round 14 set and the reason this exists: the
        assertion reads `predicate(row) == member.is_active`, and a predicate
        that DELEGATES to `is_active` can never disagree with it, so that
        half alone would be vacuous against a reclassification. The grant
        TABLE is the second half, and these three prove both halves bite."""
        # 1. THE CHECK DROPPED FROM THE PREDICATE — the shape d62473a fixed,
        #    re-spelled as the centralised predicate would now have to spell
        #    it. DECLINED must be named.
        def _no_status_test(assignment, official_id):
            if assignment is None or not official_id:
                return False
            return assignment.official_id == official_id

        with self.assertRaises(self.failureException) as dropped:
            self._assert_the_grant_is_the_status(
                _no_status_test, list(OfficialAssignmentStatus))
        self.assertIn("DECLINED", str(dropped.exception))

        # 2. A MEMBER RECLASSIFIED IN THE PRODUCT ENUM. The real predicate,
        #    unchanged, driven against a status enum that has changed its
        #    mind about ACCEPTED — which the predicate follows silently,
        #    because it reads `is_active` rather than a literal.
        class _Reclassified(str, enum.Enum):
            PROPOSED = "proposed"
            ACCEPTED = "accepted"
            DECLINED = "declined"

            @property
            def is_active(self):
                return self is not _Reclassified.ACCEPTED

        with self.assertRaises(self.failureException) as moved:
            self._assert_the_grant_is_the_status(
                subject_scope.assignment_grants_official_scope,
                list(_Reclassified))
        self.assertIn("ACCEPTED", str(moved.exception))

        # 3. A NEW MEMBER. The predicate answers it confidently — that is
        #    the whole hazard — so the enumeration is what has to notice.
        class _WithANewMember(str, enum.Enum):
            PROPOSED = "proposed"
            ACCEPTED = "accepted"
            DECLINED = "declined"
            WITHDRAWN = "withdrawn"

            @property
            def is_active(self):
                return self in (_WithANewMember.PROPOSED,
                                _WithANewMember.ACCEPTED)

        with self.assertRaises(self.failureException) as added:
            self._assert_the_grant_is_the_status(
                subject_scope.assignment_grants_official_scope,
                list(_WithANewMember))
        self.assertIn("WITHDRAWN", str(added.exception))

        # …and the real thing still passes, so none of the three is failing
        # for a reason the real predicate shares.
        self._assert_the_grant_is_the_status(
            subject_scope.assignment_grants_official_scope,
            list(OfficialAssignmentStatus))

    def test_the_official_pin_follows_the_predicate_the_branch_calls(self):
        """THE PIN IS ON WHAT IS DECIDED, NOT ON WHAT IS CALLED.

        MUTATION: the gate's OFFICIAL branch keeps the call it has today,
        CHARACTER FOR CHARACTER — ``assignment_grants_official_scope(a,
        official_id)`` — and the name resolves to a definition that has
        dropped ``status.is_active``. Nothing about the branch's own text
        moves, so a pin that stopped at the function name could not see it,
        and neither could a ledger asserting that three modules share one
        function object: they still would.

        THE FALSIFIER IS THE FOLLOW ITSELF. Neuter ``_inlined`` — which is
        exactly what round 14 had — re-pin OFFICIAL on the call text that
        then derives, and the audit goes SILENT on the same mutation. That
        is what shows the following is what bites."""
        source = self._gate_source()
        importline = ("from .subject_scope import "
                      "assignment_grants_official_scope\n")
        self.assertIn(importline, source)
        source = source.replace(importline, "", 1)
        source = source.replace(
            "def _player_team_for_game(",
            "def assignment_grants_official_scope(assignment, official_id):\n"
            "    if assignment is None or not official_id:\n"
            "        return False\n"
            "    return assignment.official_id == official_id\n\n\n"
            "def _player_team_for_game(", 1)
        failures = self._audit(source)
        self.assertTrue(
            any("OFFICIAL" in f and "a.status.is_active" in f
                for f in failures),
            f"the branch's own text is byte-identical and the predicate it "
            f"resolves to no longer tests the status, and the audit did not "
            f"report it: {failures}")
        # RE-PINNED ON WHAT THE MUTATION ITSELF DECIDES: silent, so what
        # caught it is the pin and not something else in the chain.
        moved = next(b for b in admission_branches(source=source)["OFFICIAL"]
                     if b.needs_authority)
        repinned = dict(ADMISSION_AUTHORITIES)
        repinned["OFFICIAL"] = dataclasses.replace(
            ADMISSION_AUTHORITIES["OFFICIAL"], authority=moved.authority,
            admits=moved.admits_source, side=moved.side_source)
        with mock.patch.dict(ADMISSION_AUTHORITIES, repinned, clear=True):
            self.assertEqual(
                [], self._audit(source),
                "the mutation is still reported after the pin is moved onto "
                "it, so this test is not measuring the pin")
        # AND WITHOUT THE FOLLOW — round 14's derivation exactly — the pin
        # is the CALL, the call is unchanged, and the mutation is invisible.
        #
        # PLAYER IS RE-PINNED HERE TOO, because round 16 folds the RESOLVER'S
        # per-role answers with the same function: neutering `_inlined` puts
        # that pin back on `_player_team_for_game(scope, game, store)` as
        # well, and a failure about the PLAYER pin in this stanza would be a
        # failure about something other than the OFFICIAL follow.
        with self._without("_inlined", lambda node, tree, where, depth=None:
                           node):
            unfollowed = {role: next(b for b in admission_branches()[role]
                                     if b.needs_authority)
                          for role in ("OFFICIAL", "PLAYER")}
            self.assertIn(
                "assignment_grants_official_scope(a, scoped_official_id)",
                unfollowed["OFFICIAL"].authority,
                          "neutering the follow did not put the pin back on "
                          "the function name, so this half measures nothing")
            self.assertEqual(
                "_player_team_for_game(scoped_player_id, game, store)",
                unfollowed["PLAYER"].authority,
                             "neutering the follow did not put the PLAYER "
                             "pin back on the helper's NAME, so the fold "
                             "this round added is not what carries it")
            stale = dict(ADMISSION_AUTHORITIES)
            for role, branch in unfollowed.items():
                stale[role] = dataclasses.replace(
                    ADMISSION_AUTHORITIES[role], authority=branch.authority,
                    admits=branch.admits_source, side=branch.side_source)
            with mock.patch.dict(ADMISSION_AUTHORITIES, stale, clear=True):
                blind = self._audit(source)
        self.assertEqual(
            [], blind,
            f"a pin that stops at the function name was expected to be blind "
            f"to a predicate that stopped testing the status, and it was "
            f"not — so this test is not measuring the follow: {blind}")

    def test_a_call_the_pin_cannot_follow_is_refused_by_name(self):
        """FOLLOWING IS AN ALLOW-LIST TOO, which is the only reason it is
        worth having.

        A follow that gave up quietly on a shape it had not seen would be
        the round-14 defect exactly: the pin would fall back to the function
        name and report green, and "this branch calls something" would once
        again be the whole of the judgement. So every shape this cannot read
        RAISES, naming what it could not read — and each is a shape somebody
        would plausibly write.

        The predicate's own module is not touched by any of these: each
        mutation is spliced into a COPY of the GATE, which is how the rest of
        this class works and what keeps the real product source out of it."""
        replaced = ("from .subject_scope import "
                    "assignment_grants_official_scope\n")
        local = "def _player_team_for_game("
        for label, prelude, expected in (
                ("decided outside the product package",
                 (replaced,
                  "from json import loads as "
                  "assignment_grants_official_scope\n"),
                 "outside the product package"),
                ("bound twice at module level",
                 (local,
                  "def assignment_grants_official_scope(assignment, oid):\n"
                  "    return assignment.official_id == oid\n\n\n"
                  + local),
                 "is bound 2 times at module level"),
                ("a statement kind the fold cannot read",
                 (replaced,
                  "def assignment_grants_official_scope(assignment, oid):\n"
                  "    for _ in (1,):\n"
                  "        return assignment.official_id == oid\n"
                  "    return False\n\n\n"),
                 "is a statement kind this walk cannot fold"),
                ("a body that falls through",
                 (replaced,
                  "def assignment_grants_official_scope(assignment, oid):\n"
                  "    if assignment is None:\n"
                  "        pass\n"
                  "    return assignment.official_id == oid\n\n\n"),
                 "reaches its end without deciding anything"),
                ("a decision that reads its own module's globals",
                 (replaced,
                  "_ALWAYS = True\n\n\n"
                  "def assignment_grants_official_scope(assignment, oid):\n"
                  "    return _ALWAYS and assignment.official_id == oid\n\n\n"
                  ),
                 "which is not one of its parameters"),
                ("called by keyword",
                 (replaced,
                  "def assignment_grants_official_scope(assignment, oid):\n"
                  "    return assignment.official_id == oid\n\n\n"),
                 "as plain positional arguments")):
            with self.subTest(shape=label):
                source = self._injected(prelude=prelude)
                if label == "called by keyword":
                    source = source.replace(
                        "assignment_grants_official_scope("
                        "a, scoped_official_id)",
                        "assignment_grants_official_scope(\n"
                        "                a, "
                        "official_id=scoped_official_id)", 1)
                failures = self._audit(source)
                self.assertTrue(
                    any("unresolvable admission shape" in f
                        and expected in f for f in failures),
                    f"a call the follow cannot read was not refused naming "
                    f"why: {failures}")

    def test_the_signature_fields_the_substitution_reads_are_this_grammar(
            self):
        """THE 3.11 CHECK FOR THE FOLLOW, made by the interpreter running it.

        :func:`_positional_parameters` decides whether a callee's signature
        is one this file may read argument by argument, by naming the fields
        of ``ast.arguments``, and CI runs 3.11 while this was written on
        3.14. A field this did not look at would be an argument-matching rule
        silently not modelled — the callee's decision pasted into the caller
        as though every argument were plain positional — so EVERY field of
        the running interpreter's ``ast.arguments`` must be read, and the
        fields are read off that function's OWN SOURCE rather than off a list
        somebody copied out of the grammar. Round 13 checked ``ast.Match``'s
        PEP 634 fields the same way, and round 14 ``_assigned_names``' 17
        pairs.

        IT READS ONE FUNCTION AND NOT TWO (#427 round 16). The fields used to
        be named inside :func:`_substituted`; the ARGUMENT PROVENANCE rule
        asks the same question of the same grammar, and a signature model
        written out twice is a model that can be right in one of the two
        copies. :func:`_positional_parameters` is now the only place either
        of them asks, which is also why this test still names one."""
        fn = ast.parse(
            textwrap.dedent(inspect.getsource(_positional_parameters))).body[0]
        read = {node.attr for node in ast.walk(fn)
                if isinstance(node, ast.Attribute)
                and isinstance(node.value, ast.Name)
                and node.value.id == "args"}
        self.assertEqual(
            frozenset(ast.arguments._fields), read,
            f"{sorted(frozenset(ast.arguments._fields) ^ read)} is the "
            f"difference between the signature grammar this interpreter has "
            f"and the fields the substitution refusal looks at. A field it "
            f"does not read is a call shape it does not model, substituted "
            f"as though it were plain positional")

    def test_the_derivation_finds_the_branches_the_gate_actually_has(self):
        """The premise: the extraction is not vacuously green.

        Every ROLE the product declares is accounted for — a role the gate
        never mentions still appears, because the catch-all refusal is
        attributed to every role — and the branches that need an authority
        are exactly the five the gate takes."""
        branches = admission_branches()
        self.assertEqual(frozenset(Role.__members__), frozenset(branches),
                         "a domain Role reaches no decision in the gate at "
                         "all, so nothing says what happens to it")
        needing = {role for role, decisions in branches.items()
                   for b in decisions if b.needs_authority}
        answered = frozenset(OPERATOR_ROLES) | frozenset(
            ADMISSION_AUTHORITIES)
        self.assertEqual(
            answered, needing,
            f"the gate admits {sorted(needing)} to a real game, and the "
            f"operator exemption plus ADMISSION_AUTHORITIES answer for "
            f"{sorted(answered)}")
        # …and the two roles the gate REFUSES outright really are refused,
        # so "no authority needed" is not hiding an admission.
        for role in ("GUARDIAN", "VIEWER"):
            self.assertTrue(
                all(not b.needs_authority for b in branches[role]),
                f"{role} reaches an admission carrying a real game, which is "
                f"a grant this file types as entitled to nothing")

    def test_every_declared_authority_is_a_class_this_sweep_models(self):
        """A mapping may not answer with a class the sweep does not drive, or
        with one whose grant names nothing — either would be an authority in
        name only."""
        store = InMemoryStore()
        try:
            fx = self._fixture(store)
            entitlement = self._entitlement(fx)
            classes = {klass for klass, _teams in entitlement.values()}
            for role, declared in sorted(ADMISSION_AUTHORITIES.items()):
                klass = declared.klass
                with self.subTest(role=role):
                    self.assertIn(
                        klass, classes,
                        f"{role}'s admission is answered by a class no swept "
                        f"principal carries")
                    self.assertTrue(
                        GRANT_DIMENSIONS[klass],
                        f"{role}'s admission is answered by {klass}, whose "
                        f"grant is keyed on NOTHING")
                    self.assertEqual(
                        PRINCIPAL_ROLES[next(
                            p for p in PRINCIPALS
                            if entitlement[p][0] == klass)].name,
                        role,
                        f"{role}'s admission is answered by {klass}, which "
                        f"the sweep drives under a DIFFERENT role")
        finally:
            store.clear_all_data()

    def test_the_operator_exemption_is_the_products_own_answer_three_times(
            self):
        """The one exemption this axis has, cross-checked against three
        independent product statements so it cannot be widened in one place:
        the PERMISSION table, ``lineup_visibility._UNSCOPED_OPERATORS``, and
        the gate's own unconditional branch."""
        self.assertEqual(
            OPERATOR_ROLES,
            frozenset(r.name for r in lineup_visibility._UNSCOPED_OPERATORS),
            "the roles holding MANAGE_SCHEDULE and the roles the projection "
            "module calls unscoped operators have diverged, so 'operator' "
            "means two different things in the product")
        unconditional = {role for role, decisions in
                         admission_branches().items() for b in decisions
                         if b.needs_authority and b.authority == "True"}
        self.assertEqual(
            OPERATOR_ROLES, unconditional,
            "the gate admits a different set of roles before consulting any "
            "authority than the product calls operators")
        for role in Role:
            if role.name in OPERATOR_ROLES:
                continue
            self.assertNotIn(
                OPERATOR_PERMISSION, ROLE_PERMISSIONS.get(role, frozenset()),
                f"{role.name} gained {OPERATOR_PERMISSION.value} and is "
                f"therefore now exempt from carrying an authority — decide "
                f"that deliberately")

    # -- THE INJECTION: the derivation is real, in SIXTY-FOUR spellings
    def _injected(self, *, anchor=None, replacement=None, prelude=None,
                  prelude2=None, suffix=None):
        source = self._gate_source()
        # TWO PRELUDES SINCE ROUND 20 (#427). The relocation and the alias
        # each edit the gate in TWO places — they put the OFFICIAL branch
        # back on a local read AND hoist that read above the role branches —
        # and the resolver-slot falsifier changes a signature and a call
        # site. Applied in order, each asserting its own anchor is present,
        # so a spelling whose anchor has drifted fails LOUDLY instead of
        # patching nothing and proving nothing.
        for pair in (prelude, prelude2):
            if pair is None:
                continue
            before, after = pair
            self.assertIn(before, source)
            source = source.replace(before, after, 1)
        if replacement is not None:
            anchor = anchor or self.ANCHOR
            self.assertIn(anchor, source)
            source = source.replace(anchor, replacement, 1)
        if suffix is not None:
            # A module-level append — the only way to spell a SECOND
            # definition of the carrier, which is what the sixth model is
            # about and which no edit inside the first one can express.
            source += suffix
        return source

    def _spellings(self):
        """A new NON-ADMIN admission branch, spelled SIXTY-FOUR ways —
        chosen to defeat a text matcher, which is what the query-parameter
        closure was tested against and what this axis has to survive too;
        since round 13, to defeat a walk that reads only the statement kinds
        somebody thought to list; since round 14, to defeat each of the
        SIX MODELS the walk rests on; and since round 16, to defeat the
        SEVENTH — what a delegating branch HANDS the audited resolver;
        since round 18, to defeat the seventh model's own blind spot, which
        vouched a NAME rather than a VALUE; and since round 19, to defeat
        the two ways the NAME can be identical while the VALUE is not — the
        object MUTATED before the call, and the RESOLVER'S OWN parameter
        rebuilt after it; and since round 20, to defeat the mutation of a
        mapping that is no longer THERE to mutate — the boundary now
        projects the session scope into immutable ids before any role
        branch, so SIXTEEN more are spelled against the PROJECTION and
        against the inputs the gate still receives whole.

        EVERY NUMBER HERE IS MEASURED off this dict, not remembered:
        :meth:`test_a_new_non_admin_admission_branch_fails_by_name` asserts
        the total against ``len(self._spellings())`` and the split against
        the dict's own entries, so a spelling added or removed moves the
        prose or fails.

        FORTY-ONE OF THE SIXTY-FOUR DO NOT INJECT A NEW BRANCH AT ALL, which
        is the half of this battery the derivation's pins and rules have to
        answer for rather than its role walk. They are, by what they touch:

        * ELEVEN move only the gate's EXISTING COACH/PLAYER branch — the
          delegation, what it hands the resolver, and what it returns;
        * ONE adds no branch whatever but a SECOND DEFINITION of the
          carrier, and ONE binds a name only AFTER the return that reads it;
        * FIVE mutate or rebuild the RAW SESSION MAPPING above the
          projection, and FOUR more do it in shapes round 18 and round 19
          found — a rebuild out of the gate's own inputs, a mutation in a
          test that constrains no role, one laundered through a field the
          classification collapses to a boolean, and one through the
          carrier's live ``game`` binding;
        * TWO are the owner's OWN NAMED VARIANTS from this round — the
          OFFICIAL branch's read RELOCATED above the role branches, and the
          same statement through a local ALIAS of the mapping;
        * FIVE do to ``store`` what those did to the mapping, because
          ``store`` is the mutable input the gate still receives whole and
          is where conditions 5 and 6 keep their own falsifiers;
        * SIX attack the PROJECTION itself — a projected id assigned
          twice, a resolver slot no projection fills, a projection named for
          one key and reading another, one taken below a role branch, one of
          a key no account can carry, and one taken from a fifth parameter
          no caller passes, which is what this round's own hunt found;
        * TWO change the RESOLVER alone and never touch the carrier;
        * and FOUR change no line of the carrier: two edit the HELPER the
          PLAYER authority rests on, one replaces the SUBJECT the gate
          selects, and one calls the resolver by keyword."""
        body, anchor = self.ADMITTING_BODY, self.ANCHOR
        resolver_player = ("    if role == Role.PLAYER:\n"
                           "        return _player_team_for_game("
                           "scoped_player_id, game, store)")
        return {
            "plain_equality":
                dict(replacement=f"    if role == Role.GUARDIAN:\n"
                                 f"{body}{anchor}"),
            "aliased_import":
                dict(replacement=f"    if role in (_R.GUARDIAN,):\n"
                                 f"{body}{anchor}",
                     prelude=("from ..domain import Role\n",
                              "from ..domain import Role\n"
                              "from ..domain import Role as _R\n")),
            "nested_helper":
                dict(replacement=f"    if _is_guardian(role):\n{body}{anchor}",
                     prelude=("def _player_team_for_game(",
                              "def _is_guardian(r):\n"
                              "    return r == Role.GUARDIAN\n\n\n"
                              "def _player_team_for_game(")),
            "identity_test":
                dict(replacement=f"    if role is Role.GUARDIAN:\n"
                                 f"{body}{anchor}"),
            "role_value_string":
                dict(replacement=f"    if role.value == 'guardian':\n"
                                 f"{body}{anchor}"),
            "module_level_tuple":
                dict(replacement=f"    if role in _EXTRA_ADMITTED:\n"
                                 f"{body}{anchor}",
                     prelude=("def _player_team_for_game(",
                              "_EXTRA_ADMITTED = (Role.GUARDIAN,)\n\n\n"
                              "def _player_team_for_game(")),
            "getattr_member":
                dict(replacement=f"    if role == getattr(Role, 'GUARDIAN'):"
                                 f"\n{body}{anchor}"),
            "else_branch":
                dict(replacement=f"    if role == Role.OFFICIAL:\n"
                                 f"        pass\n    else:\n{body}{anchor}"),
            # THE SPELLING THE DENY-LIST WALK MISSED ENTIRELY (#427 round
            # 13). `ast.Match` was not in the tuple the old walk raised on
            # and there was no `else`, so this arm was SILENTLY SKIPPED: the
            # gate admitted a Guardian to a real game with a real side while
            # `admission_branches()` reported nothing at all. It is now
            # ATTRIBUTED — not merely refused — so it fails naming GUARDIAN,
            # at the same line and with the same message the `if` spelling
            # of the identical body produces.
            "match_statement":
                dict(replacement="    match role:\n"
                                 "        case Role.GUARDIAN:\n"
                                 + textwrap.indent(body, "    ") + anchor),
            "match_or_pattern":
                dict(replacement="    match role:\n"
                                 "        case Role.VIEWER | Role.GUARDIAN:\n"
                                 + textwrap.indent(body, "    ") + anchor),
            # THE EMPTY-INTERSECTION SPELLING — the one found by hunting for
            # a shape the ALLOW-LIST would still not catch, and the sharpest
            # of the set because every statement in it is one the walk
            # handles. Two role tests on TWO DIFFERENT NAMES, both true at
            # runtime: the outer narrowed to {COACH}, the inner to
            # {GUARDIAN}, and the walk intersects enclosing role sets, so the
            # return under them was attributed to NO ROLE AT ALL and
            # recorded nowhere. A live branch admitting a Guardian to a real
            # game, and `_audit()` returned []. `_resolve_roles` now requires
            # the test to be about the gate's own role parameter.
            "empty_intersection_two_names":
                dict(replacement="    default_kind = Role.COACH\n"
                                 "    if default_kind == Role.COACH:\n"
                                 "        if role == Role.GUARDIAN:\n"
                                 + textwrap.indent(body, "    ") + anchor),
            "empty_intersection_match_subject":
                dict(replacement="    default_kind = Role.COACH\n"
                                 "    if default_kind == Role.COACH:\n"
                                 "        match role:\n"
                                 "            case Role.GUARDIAN:\n"
                                 + textwrap.indent(body, "        ")
                                 + anchor),
            # `case _:` constrains no role, so the arm is attributed to
            # EVERY role — the same over-approximation an `if` whose test
            # names no Role gets, and the same safe direction.
            "match_wildcard":
                dict(replacement="    match role:\n"
                                 "        case Role.OFFICIAL:\n"
                                 "            pass\n"
                                 "        case _:\n"
                                 + textwrap.indent(body, "    ") + anchor),
            "returned_from_a_helper":
                dict(replacement="    if role == Role.GUARDIAN:\n"
                                 "        return _guardian_read(role, game)\n"
                                 + anchor,
                     prelude=("def _player_team_for_game(",
                              "def _guardian_read(role, game):\n"
                              "    return PrivateGameRead(role=role, "
                              "game=game, own_team=game.home_team_id, "
                              "admitted=True)\n\n\n"
                              "def _player_team_for_game(")),
            # BOTH HALVES OF THE REAL SEAM, which is the sharpest one: widen
            # the carrier's role tuple AND teach the resolver to answer the
            # new role a side. This is the shape that would actually ship.
            "carrier_and_resolver":
                dict(anchor=anchor,
                     replacement="    if role in (Role.COACH, Role.PLAYER, "
                                 "Role.GUARDIAN):",
                     prelude=(resolver_player,
                              resolver_player
                              + "\n    if role == Role.GUARDIAN:\n"
                                "        return scoped_team_id")),
            # ---- ROUND 14: the three hand-reasoned MODELS the brief
            # named, plus THREE MORE this round's own hunt found. Every one
            # of these was audited GREEN at c4a725b AND at 155f1fd — the two
            # commits measured byte-for-byte, so none is a round-13
            # regression — and all but the SNAPSHOT one produced a live
            # admission there.
            #
            # THE BINDING MODEL. `walk` copied its bindings per body, so an
            # assignment inside a nested block never reached the statements
            # after it and `_unfold` reported THE STALE OUTER LITERAL. Aimed
            # at `admitted=` (a literal False became `admits=False`) and at
            # `game=` (a literal None became `carries_game=False`) — the two
            # keywords whose literal-emptiness is what excuses a branch from
            # needing an authority at all.
            "stale_outer_literal_admitted":
                dict(replacement="    _ok = False\n"
                                 "    if role == Role.GUARDIAN:\n"
                                 "        if game is not None:\n"
                                 "            _ok = True\n"
                                 "        return PrivateGameRead(role=role, "
                                 "game=game, own_team=game.home_team_id, "
                                 "admitted=_ok)\n" + anchor),
            "stale_outer_literal_game":
                dict(replacement="    _g = None\n"
                                 "    if role == Role.GUARDIAN:\n"
                                 "        if game is not None:\n"
                                 "            _g = game\n"
                                 "        return PrivateGameRead(role=role, "
                                 "game=_g, own_team=game.home_team_id, "
                                 "admitted=True)\n" + anchor),
            # THE GRANT MODEL. `needs_authority` was `admits and
            # carries_game`, on the ground that `game=None` "grants nothing".
            # `web/server.py` never re-checks `private_read.game` and
            # re-fetches the game by id, so a real `own_team` with no game is
            # a FULL disclosure. Both the literal and the unfolded spelling.
            "side_without_a_game_literal":
                dict(replacement="    if role == Role.GUARDIAN:\n"
                                 "        return PrivateGameRead(role=role, "
                                 "game=None, own_team=game.home_team_id, "
                                 "admitted=True)\n" + anchor),
            "side_without_a_game_unfolded":
                dict(replacement="    _g = None\n"
                                 "    if role == Role.GUARDIAN:\n"
                                 "        return PrivateGameRead(role=role, "
                                 "game=_g, own_team=game.home_team_id, "
                                 "admitted=True)\n" + anchor),
            # THE ROLE-IDENTITY MODEL. `_role_parameter` read the role's name
            # off the signature and never checked the name still HOLDS the
            # parameter; rebinding it books the arm under whatever the
            # mutation names while every role reaches it at runtime.
            "role_parameter_rebound_if":
                dict(replacement="    role = Role.LEAGUE_ADMIN\n"
                                 "    if role == Role.LEAGUE_ADMIN:\n"
                                 + body + anchor),
            "role_parameter_rebound_match":
                dict(replacement="    role = Role.LEAGUE_ADMIN\n"
                                 "    match role:\n"
                                 "        case Role.LEAGUE_ADMIN:\n"
                                 + textwrap.indent(body, "    ") + anchor),
            # THE DELEGATION MODEL — the FOURTH, found by this round's own
            # hunt rather than handed to it, and the first of the three that
            # needs no new branch at all. `authority` is the RESOLVER'S
            # expression whenever the branch delegates, so a branch could
            # resolve the caller's side through the audited resolver and then
            # return a DIFFERENT one under the same authority. Two
            # spellings here — return another side outright, and drop the
            # participation test — and two more below that reach the same
            # end through the BINDING model instead, binding the trusted
            # side a second time and re-binding it inside a nested block.
            # The nested one is the sharpest: all three pins still read
            # exactly what they pinned, so only `_poison` refuses it.
            "delegated_branch_returns_another_side":
                dict(anchor=anchor, replacement=anchor, names="COACH",
                     prelude=(self.DELEGATED_DECISION,
                              "        return PrivateGameRead(role=role, "
                              "game=game,\n"
                              "                               own_team="
                              "game.home_team_id,\n"
                              "                               admitted="
                              "admitted)\n")),
            "delegated_branch_drops_its_own_test":
                dict(anchor=anchor, replacement=anchor, names="COACH",
                     prelude=(self.DELEGATION,
                              self.RESOLVER_CALL
                              + "        admitted = True\n")),
            # THE SNAPSHOT. `bindings` is the body's own mutable map and
            # the walk handed it out by reference, so a name bound only
            # AFTER a return still unfolded into it. NOT itself a live
            # disclosure — the gate would raise NameError — but a walk that
            # describes a return by state that did not exist at it is wrong
            # in both directions, and the other one is a false refusal of
            # legitimate code.
            "a_name_bound_after_the_return":
                dict(replacement="    if role == Role.GUARDIAN:\n"
                                 "        return PrivateGameRead(role=role, "
                                 "game=game, own_team=game.home_team_id, "
                                 "admitted=_ok)\n"
                                 "    _ok = False\n" + anchor),
            "the_trusted_side_bound_twice":
                dict(anchor=anchor, replacement=anchor, names="COACH",
                     prelude=(self.DELEGATION,
                              self.RESOLVER_CALL
                              + "        own_team = game.home_team_id\n"
                                "        admitted = own_team is not None and "
                                "own_team in (\n"
                                "            game.home_team_id, "
                                "game.away_team_id)\n")),
            # THE DELEGATION-DROP MODEL, the fifth. A branch that CALLS the
            # resolver and then ignores what it said was not merely
            # mis-attributed for a role the resolver answers with nothing —
            # it was DROPPED and never audited at all.
            "delegating_branch_that_ignores_the_resolver":
                dict(replacement="    if role == Role.GUARDIAN:\n"
                                 "        own_team = "
                                 "game_scoped_own_team_id(role, "
                                 "scoped_team_id, scoped_player_id, game, "
                                 "store)\n"
                                 "        return PrivateGameRead(role=role, "
                                 "game=game, own_team=game.home_team_id, "
                                 "admitted=True)\n" + anchor),
            # THE ONE-DEFINITION MODEL, the sixth. `_gate_function` returned
            # the FIRST module-level `def` and Python binds the LAST, so the
            # audited definition and the running one were different
            # functions. The gate above is untouched and parses clean.
            "a_second_definition_of_the_carrier":
                dict(names="COACH",
                     suffix="\n\ndef " + GATE_CARRIER
                            + "(role, scope, game_id, store):\n"
                              "    game = store.get_game(game_id)\n"
                              "    return PrivateGameRead(role=role, "
                              "game=game,\n"
                              "                           own_team="
                              "game.home_team_id, admitted=True)\n"),
            "a_nested_block_rebinds_the_trusted_side":
                dict(anchor=anchor, replacement=anchor, names="COACH",
                     prelude=(self.DELEGATION,
                              "        _ = game_scoped_own_team_id(\n"
                              "            role, scoped_team_id, "
                              "scoped_player_id, game, store)\n"
                              "        if game is not None:\n"
                              "            own_team = game.home_team_id\n"
                              "        admitted = own_team is not None and "
                              "own_team in (\n"
                              "            game.home_team_id, "
                              "game.away_team_id)\n")),
            # ---- ROUND 16: WHAT THE BRANCH HANDS THE RESOLVER. None of
            # these adds a branch, and none of them moves a single character
            # of any of the three pins: `authority` is the resolver's own
            # return expression, `admits_source` unfolds to the participation
            # predicate and `side_source` to `own_team if admitted else
            # None`, all of which a forged ARGUMENT leaves exactly as they
            # were. Every one was measured at `d3f5f1e` with `_audit() == []`
            # and four of them driven to a live HTTP disclosure.
            # RE-AIMED IN ROUND 20 (#427). These forged the ``scope`` SLOT,
            # and there is no scope slot any more — the resolver takes
            # projected ids. The defect is the same one and the slot it is
            # spelled in is the ``scoped_team_id`` one, which is where a
            # forged side would now have to arrive.
            "forged_team_id_into_the_resolver":
                dict(anchor=anchor, replacement=anchor, names="COACH",
                     prelude=(self.RESOLVER_CALL,
                              "        own_team = game_scoped_own_team_id(\n"
                              "            role, game.home_team_id, "
                              "scoped_player_id, game, store)\n")),
            "forged_role_into_the_resolver":
                dict(anchor=anchor, replacement=anchor, names="COACH",
                     prelude=(self.RESOLVER_CALL,
                              "        own_team = game_scoped_own_team_id(\n"
                              "            Role.COACH, scoped_team_id, "
                              "scoped_player_id, game, store)\n")),
            "forged_role_and_team_id_into_the_resolver":
                dict(anchor=anchor, replacement=anchor, names="COACH",
                     prelude=(self.RESOLVER_CALL,
                              "        own_team = game_scoped_own_team_id(\n"
                              "            Role.COACH, game.home_team_id, "
                              "scoped_player_id, game, store)\n")),
            # THE CALL SITE'S TEXT NEVER MOVES — one line, above every
            # branch, replacing the gate's own normalisation of the session
            # scope with a scope the gate BUILT.
            "the_scope_rebuilt_before_every_branch":
                dict(anchor=anchor, replacement=anchor, names="COACH",
                     prelude=(self.NORMALISED_SCOPE,
                              "    scope = {'team_id': "
                              "store.get_game(game_id).home_team_id}\n"
                              "    scoped_team_id = scope.get(\"team_id\")")),
            # A DIFFERENT VARIABLE in the slot — the same disclosure by a
            # longer road, and a bare `Name` at the call site, so only the
            # IDENTITY half of the rule can answer it.
            "a_different_variable_in_the_projected_slot":
                dict(anchor=anchor, replacement=anchor, names="COACH",
                     prelude=(self.RESOLVER_CALL,
                              "        _side_id = game.home_team_id\n"
                              "        own_team = game_scoped_own_team_id(\n"
                              "            role, _side_id, "
                              "scoped_player_id, game, store)\n")),
            # A CALL SHAPE THE VOUCHING DOES NOT MODEL is refused, not read
            # position by position as though it were plain positional.
            "the_resolver_called_by_keyword":
                dict(anchor=anchor, replacement=anchor, names="COACH",
                     prelude=(self.RESOLVER_CALL,
                              "        own_team = game_scoped_own_team_id(\n"
                              "            role, scoped_team_id, "
                              "scoped_player_id, game, store=store)\n")),
            # THE SAME DEFECT ONE CALL DEEPER. The PLAYER authority was
            # pinned as the TEXT `_player_team_for_game(scope, game, store)`
            # and that function's BODY was derived by nothing.
            "the_player_helper_answers_home":
                dict(anchor=anchor, replacement=anchor, names="PLAYER",
                     prelude=(self.PLAYER_HELPER_ANSWER,
                              "    return game.home_team_id\n")),
            # FOUND BY THIS ROUND'S OWN HUNT, against the three rules it had
            # already written: the gate's one line that selects the SUBJECT,
            # replaced by a value that is a function of `store` alone.
            "the_subject_the_resolver_already_has":
                dict(anchor=anchor, replacement=anchor, names="COACH",
                     prelude=(self.SUBJECT_FETCH,
                              "    game = store.all_games().pop()\n")),
            # ---- ROUND 18: THE OWNER'S BLOCKER 2. The gate's OWN
            # normalisation of the session scope, replaced by a REBUILD that
            # satisfies all four of round 16's conditions — a bare `Name` at
            # the call site, the resolver's own parameter name, every leaf a
            # carrier parameter, no literal anywhere, and it still MENTIONS
            # `scope`. Measured at `73faf4d`: `_audit() == []`.
            "the_scope_rebuilt_out_of_the_gates_own_inputs":
                dict(anchor=anchor, replacement=anchor, names="COACH",
                     prelude=(self.NORMALISED_SCOPE,
                              "    scope = scope.fromkeys(scope, "
                              "store.get_game(game_id).home_team_id)\n"
                              "    scoped_team_id = scope.get(\"team_id\")")),
            # ---- ROUND 19, CLASS A: THE NAME IS STILL THE PARAMETER AND
            # THE VALUE IS NOT. Every one of these leaves the call site
            # reading the bare `scope`, leaves it resolving to the gate's own
            # normalisation, and leaves all three pins character for
            # character where they were — condition 5 inspects the ARGUMENT
            # EXPRESSION and never asks whether the OBJECT it names was
            # changed first. All five measured at `8a6ed27`: `_audit() == []`
            # and, compiled into the running server, 60 live admissions out
            # of 60 for `thirdcoach` — ten family leaves x two games x three
            # backends — with eight of HOME's private players in the `gid`
            # `/lineups` body at `restricted=false` on real PostgreSQL.
            #
            # They go BEFORE the normalisation, not after: after it, round
            # 14's `_refuse_a_second_binding` already answers, because
            # `_assigned_names` reads `scope['team_id'] = …` as binding
            # `scope`. Before it, the binding is poisoned and then RE-BOUND
            # by the gate's own next line, which clears the poison — which
            # is exactly why placement, and not spelling, is what made these
            # green.
            "the_scope_subscripted_before_its_normalisation":
                dict(anchor=anchor, replacement=anchor, names="COACH",
                     prelude=(self.NORMALISED_SCOPE,
                              "    scope['team_id'] = "
                              "store.get_game(game_id).home_team_id\n"
                              "    scope = scope or {}\n"
                              "    scoped_team_id = scope.get(\"team_id\")")),
            "the_scope_subscripted_through_its_own_default":
                dict(anchor=anchor, replacement=anchor, names="COACH",
                     prelude=(self.NORMALISED_SCOPE,
                              "    (scope or {})['team_id'] = "
                              "store.get_game(game_id).home_team_id\n"
                              "    scope = scope or {}\n"
                              "    scoped_team_id = scope.get(\"team_id\")")),
            "the_scope_updated_into_a_binding_nothing_reads":
                dict(anchor=anchor, replacement=anchor, names="COACH",
                     prelude=(self.NORMALISED_SCOPE,
                              "    _t = scope.update({'team_id': "
                              "store.get_game(game_id).home_team_id})\n"
                              "    scope = scope or {}\n"
                              "    scoped_team_id = scope.get(\"team_id\")")),
            "the_scope_mutated_inside_a_list_display":
                dict(anchor=anchor, replacement=anchor, names="COACH",
                     prelude=(self.NORMALISED_SCOPE,
                              "    _t = [scope.__setitem__('team_id', "
                              "store.get_game(game_id).home_team_id)]\n"
                              "    scope = scope or {}\n"
                              "    scoped_team_id = scope.get(\"team_id\")")),
            "the_scope_mutated_by_an_annotated_assignment":
                dict(anchor=anchor, replacement=anchor, names="COACH",
                     prelude=(self.NORMALISED_SCOPE,
                              "    _t: object = scope.__setitem__("
                              "'team_id', "
                              "store.get_game(game_id).home_team_id)\n"
                              "    scope = scope or {}\n"
                              "    scoped_team_id = scope.get(\"team_id\")")),
            # ---- ROUND 19, CLASS B: THE RESOLVER'S OWN PARAMETERS. The
            # carrier is untouched in both, so every one of round 16's and
            # round 18's conditions is satisfied by construction — they vouch
            # what the carrier HANDS OVER and say nothing about what the
            # resolver does with it. The second is THE REVIEW'S OWN
            # EXPRESSION relocated one function up, and the resolver's pinned
            # answer for COACH is the TEXT `scope.get('team_id')`, which a
            # rebinding one line above leaves untouched because `_unfold`
            # only unfolds a bare `Name`. Both measured at `8a6ed27`:
            # `_audit() == []` and 60 live admissions out of 60.
            # RE-AIMED IN ROUND 20 (#427). Both of these rebuilt the
            # RESOLVER'S OWN ``scope`` parameter, and the resolver has no
            # such parameter now. The seam condition 7 owns did NOT go away
            # with it — it moved onto the PROJECTED ID, which is the sharper
            # place for it: the carrier's projection is vouched at the call
            # site by conditions 1, 2 and 9, and none of them follows the
            # value PAST the call into what the resolver then does to its own
            # slot. The resolver's pinned answer for COACH is the bare name
            # ``scoped_team_id``, which a rebinding one line above leaves
            # character for character unchanged.
            # AIMED AT ``scoped_player_id`` RATHER THAN
            # ``scoped_team_id``, and the difference is measured. The COACH
            # pin is now the BARE NAME ``scoped_team_id``, so `_unfold`
            # follows it and a rebinding of THAT slot moves the pin as well
            # — condition 7 would have no falsifier there. The PLAYER slot
            # is where it answers ALONE: the PLAYER pin folds
            # ``scoped_player_id`` through the helper, and a rebinding above
            # the role tests reaches the decision without moving it.
            "the_resolver_rebinds_a_projected_id_to_a_literal":
                dict(anchor=anchor, replacement=anchor, names="COACH",
                     prelude=(self.RESOLVER_FIRST_TEST,
                              "    scoped_player_id = 'player_1'\n"
                              + self.RESOLVER_FIRST_TEST)),
            "the_resolver_rebuilds_a_projected_id_from_its_own_inputs":
                dict(anchor=anchor, replacement=anchor, names="COACH",
                     prelude=(self.RESOLVER_FIRST_TEST,
                              "    scoped_player_id = game.home_team_id\n"
                              + self.RESOLVER_FIRST_TEST)),
            # ---- ROUND 19, THE EIGHTH SPELLING, found by hunting THIS
            # ROUND'S OWN RULE. The first draft of `_refuse_a_mutated_input`
            # counted a whole `if` test as a position the derivation reads,
            # because `_resolve_roles` reads one. It reads one AS A STATEMENT
            # ABOUT THE ROLE and out of nothing else, so a mutation parked in
            # a test that constrains no role was evaluated for its effect and
            # read by nobody. Measured against that draft, in both functions:
            # `_audit() == []`.
            "a_mutation_in_a_test_that_constrains_no_role":
                dict(anchor=anchor, replacement=anchor, names="COACH",
                     prelude=(self.NORMALISED_SCOPE,
                              "    if scope.update({'team_id': "
                              "store.get_game(game_id).home_team_id}):\n"
                              "        pass\n"
                              "    scope = scope or {}\n"
                              "    scoped_team_id = scope.get(\"team_id\")")),
            # RE-AIMED IN ROUND 20 at ``store``, because the resolver has no
            # mapping left to mutate. The rule this exercises — condition 6 —
            # is about POSITIONS rather than about the mapping, and ``store``
            # is the input it still has to answer for: a mutable object the
            # gate is handed, mentioned in a test that constrains no role, is
            # a statement kept for its effect exactly as the mapping's was.
            "a_mutation_in_a_resolver_test_that_constrains_no_role":
                dict(anchor=anchor, replacement=anchor, names="COACH",
                     prelude=(self.RESOLVER_FIRST_TEST,
                              "    if store.__setattr__('_forced', True):\n"
                              "        pass\n"
                              + self.RESOLVER_FIRST_TEST)),
            # ---- ROUND 19, THE TENTH, and the SECOND time hunting this
            # round's own rule moved it. The mutation is bound, and the
            # binding is READ — by ``game=``, which the classification
            # collapses to the boolean ``carries_game``. The branch that
            # names ``_t`` runs for nobody (``role is None`` never holds for
            # a real caller) and the mutation runs for everybody. Green
            # against the second draft of `_refuse_a_mutated_input`, and
            # LIVE: sixty of sixty, tri-store.
            "a_mutation_live_through_a_field_that_collapses":
                dict(anchor=anchor, replacement=anchor, names="COACH",
                     prelude=(self.NORMALISED_SCOPE,
                              "    _t = scope.update({'team_id': "
                              "store.get_game(game_id).home_team_id})\n"
                              "    scope = scope or {}\n"
                              "    if role is None:\n"
                              "        return PrivateGameRead(role=role, "
                              "game=_t, own_team=None,\n"
                              "                               "
                              "admitted=False)\n"
                              "    scoped_team_id = scope.get(\"team_id\")")),
            # ---- ROUND 19, THE ELEVENTH, one call DEEPER than either class
            # the review named. `_bound_forward` pastes a binding's value
            # into every later read of its name; with no later read the
            # value VANISHES and the PLAYER pin reads exactly as it would
            # have without the statement. Green at `8a6ed27` AND against
            # round 19's own conditions 6 and 7, which are asked of the two
            # functions this file NAMES and not of a followed callee.
            # RE-AIMED IN ROUND 20 (#427). The mutation this used to carry —
            # `scope.update({'player_id': …})` inside the PLAYER helper — has
            # nothing left to mutate: the helper takes the projected id and
            # never sees a mapping. The RULE it exercises is not about the
            # mapping, though: `_bound_forward` pastes a binding's value into
            # every later read of its name, and a binding NOTHING reads
            # vanishes from the fold's output, so the pin reads exactly as it
            # would without the statement. That is true of any statement kept
            # for its effect, and `store` is the mutable input the helper
            # still holds.
            "a_binding_the_fold_would_drop_in_the_player_helper":
                dict(anchor=anchor, replacement=anchor, names="PLAYER",
                     prelude=(self.PLAYER_HELPER_GUARD,
                              "    _t = store.__setattr__('_forced', True)\n"
                              + self.PLAYER_HELPER_GUARD)),
            # ---- ROUND 20: THE THREE NAMED REGRESSIONS the owner's ruling
            # requires, and the falsifiers for the three rules the refactor
            # made possible. See `_refuse_a_raw_scope_use` for the measured
            # reproduction of the first two.
            #
            # THE RELOCATION. The OFFICIAL branch's own read of the mapping,
            # hoisted above the COACH/PLAYER branch, with the mutation ridden
            # in on the `or` — `official_id` really is read, so the LIVENESS
            # rule admits the statement, and the mutation runs for every
            # caller while the branch that names it runs for almost none.
            # MEASURED at `f9b094e`, spelled against the gate as it was then:
            # `_audit() == []`, and 20 of the 50 derived unentitled cells
            # answered 200 with EIGHT private HOME identities in the `gid`
            # `/lineups` body at `restricted=false`, on Memory, SQLite and
            # real PostgreSQL alike. Spelled here against the gate as it IS,
            # where the OFFICIAL branch no longer has a local read to hoist,
            # so the relocated statement is written out in full.
            "the_official_read_relocated_above_the_role_branches":
                dict(anchor=anchor, replacement=anchor, names="COACH",
                     prelude=('    scoped_official_id = '
                              'scope.get("official_id")\n',
                              '    scoped_official_id = (scope.update('
                              "{'team_id': game_id})\n"
                              '                          or '
                              'scope.get("official_id"))\n')),
            # THE ALIAS. The same statement, one indirection along. Condition
            # 6 polices occurrences of the gate's OWN INPUT NAMES, and
            # `_alias` is not one — so at `f9b094e` the mutation was invisible
            # to it and `scope` appeared only in `_alias = scope`, whose
            # target IS live. Byte-identical live result to the relocation:
            # 20 of 50, eight identities, tri-store.
            "the_scope_mutated_through_a_local_alias":
                dict(anchor=anchor, replacement=anchor, names="COACH",
                     prelude=('    scoped_official_id = '
                              'scope.get("official_id")\n',
                              '    _alias = scope\n'
                              '    scoped_official_id = (_alias.update('
                              "{'team_id': game_id})\n"
                              '                          or '
                              '_alias.get("official_id"))\n')),
            # THE LIVE `game` BINDING. The mutation laundered through the one
            # binding the carrier makes that the derivation certainly reads —
            # `game` is live because `admitted` tests `game.home_team_id`.
            # STATED PLAINLY: unlike the two above, this one was NOT green at
            # `f9b094e`. Condition 3 refused it there, for the string literal
            # `'team_id'` in the `game` slot's resolved value — a rule about
            # ARGUMENT PROVENANCE that happens to answer a mutation, and one
            # this round changes, since a projected slot no longer goes
            # through it. It is pinned here so that the change cannot quietly
            # cost the coverage: after the refactor it is refused BY NAME, by
            # the rule that is actually about the mapping.
            "the_scope_mutated_through_the_live_game_binding":
                dict(anchor=anchor, replacement=anchor, names="COACH",
                     prelude=(self.SUBJECT_FETCH,
                              "    game = (scope.update({'team_id': "
                              "store.get_game(game_id).home_team_id})\n"
                              "            or store.get_game(game_id))\n")),
            # SINGLE ASSIGNMENT'S OWN SPELLING: the projection taken
            # correctly and
            # then written over. Every pin and the whole call site are
            # untouched.
            "a_projected_id_assigned_twice":
                dict(anchor=anchor, replacement=anchor, names="COACH",
                     prelude=(self.RESOLVER_CALL,
                              "        scoped_team_id = game.home_team_id\n"
                              + self.RESOLVER_CALL)),
            # CONDITION 8'S OWN FALSIFIER: the resolver grows a projected
            # slot the carrier projects nothing for, and the carrier fills it
            # with a local that happens to carry the name.
            "a_resolver_slot_no_projection_fills":
                dict(anchor=anchor, replacement=anchor, names="COACH",
                     prelude=("def game_scoped_own_team_id(role, "
                              "scoped_team_id, scoped_player_id, game,\n"
                              "                            store):",
                              "def game_scoped_own_team_id(role, "
                              "scoped_team_id, scoped_player_id,\n"
                              "                            scoped_venue_id, "
                              "game, store):"),
                     prelude2=(self.RESOLVER_CALL,
                               "        scoped_venue_id = game_id\n"
                               "        own_team = game_scoped_own_team_id(\n"
                               "            role, scoped_team_id, "
                               "scoped_player_id, scoped_venue_id,\n"
                               "            game, store)\n")),
            # THE NAMING RULE'S OWN FALSIFIER, and the sharpest of the three:
            # a projection named for one key and reading ANOTHER. The name at
            # every downstream slot is correct, the call site is correct, and
            # a Player's id would arrive in the Coach's side.
            "a_projection_named_for_another_key":
                dict(anchor=anchor, replacement=anchor, names="COACH",
                     prelude=('    scoped_team_id = scope.get("team_id")\n',
                              '    scoped_team_id = '
                              'scope.get("player_id")\n')),
            # …and the two structural preconditions `_projections` states:
            # a projection taken BELOW a role branch, and a projection of a
            # key `AccountService._ALLOWED_SCOPE_KEYS` does not accept.
            "a_projection_taken_below_a_role_branch":
                dict(anchor=anchor, replacement=anchor, names="COACH",
                     prelude=('    scoped_team_id = scope.get("team_id")\n',
                              ''),
                     prelude2=(anchor,
                               '    scoped_team_id = scope.get("team_id")\n'
                               + anchor)),
            # RE-AIMED IN ROUND 20 (#427). Condition 5 asks "is the value in
            # this slot still the input the carrier RECEIVED", and it can
            # only ask it of a slot that is BOTH a resolver parameter and a
            # carrier parameter. Before the refactor those were `role`,
            # `scope` and `store`; `scope` is not a slot any more, so the
            # spelling that used to falsify this rule (`scope =
            # scope.fromkeys(scope, …)`) is now answered by condition 9
            # instead. `store` is the seam condition 5 keeps, and it is a
            # real one: a rebuilt store is a store whose `get_player` and
            # `assignments_for_game` answer whatever the gate built.
            "the_store_rebuilt_out_of_the_gates_own_inputs":
                dict(anchor=anchor, replacement=anchor, names="COACH",
                     prelude=(self.SUBJECT_FETCH,
                              "    store = store.copy()\n"
                              + self.SUBJECT_FETCH)),
            # …and the same input MUTATED rather than rebuilt, which is what
            # condition 6 still answers for once the mapping is gone.
            "the_store_mutated_into_a_binding_nothing_reads":
                dict(anchor=anchor, replacement=anchor, names="COACH",
                     prelude=(self.SUBJECT_FETCH,
                              "    _t = store.__setattr__('_forced', True)\n"
                              + self.SUBJECT_FETCH)),
            # THE ESCAPE THIS ROUND'S OWN HUNT FOUND, against the rules
            # this round itself wrote. A FIFTH REQUIRED PARAMETER, projected
            # from instead of the session's own mapping: every projection is
            # well-formed, `scope` is not mentioned at all, and the audit was
            # GREEN. See `_refuse_a_carrier_the_product_does_not_call`.
            "the_projection_taken_from_a_parameter_nobody_passes":
                dict(anchor=anchor, replacement=anchor, names="COACH",
                     prelude=("def resolve_private_game_read(role, scope, "
                              "game_id, store) -> PrivateGameRead:",
                              "def resolve_private_game_read(role, scope, "
                              "game_id, store,\n"
                              "                              override) -> "
                              "PrivateGameRead:"),
                     prelude2=(self.THE_PROJECTION,
                               '    override = override or {}\n'
                               '    scoped_team_id = '
                               'override.get("team_id")\n'
                               '    scoped_player_id = '
                               'override.get("player_id")\n'
                               '    scoped_official_id = '
                               'override.get("official_id")\n')),
            "a_projection_of_a_key_no_account_carries":
                dict(anchor=anchor, replacement=anchor, names="COACH",
                     prelude=('    scoped_team_id = '
                              'scope.get("team_id")\n',
                              '    scoped_team_id = '
                              'scope.get("venue_id")\n')),
            # ---- ROUND 20: CONDITION 6'S OWN SPELLINGS, RE-AIMED AT THE
            # INPUT IT STILL HAS TO ANSWER FOR. All five of round 19's class
            # A mutated the MAPPING, and condition 9 now answers every one
            # of them first — which is correct and is the point of the
            # refactor, but it leaves condition 6 with no falsifier of its
            # own on those. It is NOT redundant: `store` is a mutable object
            # the gate is handed and never projects, so "a statement that
            # names an input and whose value nothing here reads is kept for
            # its EFFECT" is exactly as true of it. Four shapes, mirroring
            # round 19's four expressible ones.
            "the_store_subscripted_into_its_own_dict":
                dict(anchor=anchor, replacement=anchor, names="COACH",
                     prelude=(self.SUBJECT_FETCH,
                              "    store.__dict__['_forced'] = True\n"
                              + self.SUBJECT_FETCH)),
            "the_store_mutated_inside_a_list_display":
                dict(anchor=anchor, replacement=anchor, names="COACH",
                     prelude=(self.SUBJECT_FETCH,
                              "    _t = [store.__setattr__('_forced', True)]\n"
                              + self.SUBJECT_FETCH)),
            "the_store_mutated_by_an_annotated_assignment":
                dict(anchor=anchor, replacement=anchor, names="COACH",
                     prelude=(self.SUBJECT_FETCH,
                              "    _t: object = store.__setattr__(\n"
                              "        '_forced', True)\n"
                              + self.SUBJECT_FETCH)),
            "a_store_mutation_in_a_test_that_constrains_no_role":
                dict(anchor=anchor, replacement=anchor, names="COACH",
                     prelude=(self.SUBJECT_FETCH,
                              "    if store.__setattr__('_forced', True):\n"
                              "        pass\n" + self.SUBJECT_FETCH)),
            "a_store_mutation_live_through_a_field_that_collapses":
                dict(anchor=anchor, replacement=anchor, names="COACH",
                     prelude=(self.SUBJECT_FETCH,
                              "    _t = store.__setattr__('_forced', True)\n"
                              "    if role is None:\n"
                              "        return PrivateGameRead(role=role, "
                              "game=_t, own_team=None,\n"
                              "                               "
                              "admitted=False)\n" + self.SUBJECT_FETCH)),
        }

    def test_a_new_non_admin_admission_branch_fails_by_name(self):
        """THE PROOF THAT CONDITION 5 IS DERIVED AND NOT DECORATIVE.

        SIXTY-FOUR spellings, each injected into a COPY of the gate's
        source, each required to produce a NAMED failure from the same audit
        that guards the real gate.

        THAT NUMBER WAS STALE AND IS NOW MACHINE-CHECKED (#427 round 20, the
        owner's ruling). It read "THIRTY-SEVEN" while the battery had been
        forty-eight for two rounds — a number somebody remembered rather
        than measured, in a file whose whole method is that a hand-kept list
        is where the next leak hides. The assertion below now pins the count
        against ``len(self._spellings())``, so the prose cannot drift from
        the dict again. Aliased imports, a nested helper, a
        module-level tuple, ``role.value``, ``getattr``, an ``else`` branch,
        a decision returned from a second function, the two-place widening
        that touches both the carrier and the resolver; three ``match``
        statements (a value pattern, an or-pattern and a wildcard arm) and
        the two EMPTY-INTERSECTION shapes, both added in round 13 when the
        walk stopped being a deny-list; THIRTEEN added in round 14,
        which are the six hand-reasoned MODELS the statement allow-list could
        not speak to — two stale-binding shapes, two side-without-a-game
        shapes, two role-parameter rebindings, four that move only the
        existing COACH/PLAYER branch, one that calls the resolver and ignores
        it, one that adds a SECOND DEFINITION of the carrier and leaves the
        first untouched, and one that binds a name only AFTER the return that
        reads it; and EIGHT added in round 16, which are the seventh model —
        what the branch HANDS the resolver. TWELVE of round 14's thirteen
        were measured at ``c4a725b``
        producing a LIVE admission while ``_audit()`` returned ``[]``, six of
        those driven to a real HTTP disclosure of the HOME side's private
        roster to ``thirdcoach``, a coach of a team in NEITHER game; the
        thirteenth would raise ``NameError`` and is here because a walk that
        describes a return by state that did not exist at it is wrong in both
        directions.

        ROUND 16'S EIGHT are a forged scope, a forged role, both together, a
        DIFFERENT VARIABLE in the scope slot, the resolver called by keyword,
        the session scope REBUILT one line above every branch, the PLAYER
        helper answering HOME, and the SUBJECT of the request replaced by a
        value the resolver could have computed for itself. SIX of the eight
        were measured at ``d3f5f1e``
        producing a LIVE HTTP disclosure with ``_audit() == []`` —
        ``thirdcoach`` receiving HOME's eight private rows on all five leaves
        of the private-game family for the first four, and ``awayplayer``
        AND ``homeplayer`` receiving HOME's sheet for the helper one, and
        ``thirdcoach`` receiving 200 on ``/lineups`` and ``/board`` for a
        game they play in NEITHER side of for the subject one. The
        forged-role-alone spelling is included even though it FAILS CLOSED on
        this fixture: the derivation was equally blind to it, and a forgery
        that happens to be denied by the resolver it forges is not a
        different defect, only a luckier one.

        THE LAST OF THE EIGHT WAS FOUND BY HUNTING THIS ROUND'S OWN RULE
        rather than handed to it: it passed all three of the argument
        conditions as first written, and is what the fourth exists for.

        ROUND 19 ADDS ELEVEN, and they are three groups. FIVE MUTATE THE
        OBJECT rather than the argument — a subscript assignment, the same
        one through the parameter's own ``or``-default, and three that
        launder ``update``/``__setitem__`` through a binding nothing reads,
        including an ``AnnAssign``. TWO CHANGE THE RESOLVER AND NOT THE
        CARRIER — its ``scope`` rebound to a literal, and the review's own
        ``fromkeys`` expression relocated one function up. Those seven are
        the review's own list and all seven were measured at ``8a6ed27``:
        ``_audit()`` returned ``[]`` for every one,
        and every one, compiled into the running server and driven over real
        authenticated HTTP, admitted ``thirdcoach`` — a Coach of a team in
        NEITHER game — on ALL SIXTY cells of the family matrix (ten leaves x
        two games x Memory, SQLite and real PostgreSQL), with eight of
        HOME's private players in the ``gid`` ``/lineups`` body at
        ``restricted=false``.

        THE LAST THREE WERE FOUND BY HUNTING ROUND 19'S OWN RULE, exactly
        as round 16's last was, and each moved it. TWO park a mutation in an
        ``if`` test that constrains no role, one in the carrier and one in
        the resolver: the first draft counted a whole test as read because
        ``_resolve_roles`` reads one, and it reads one only as a statement
        about the ROLE. THE THIRD binds the mutation and has the binding read
        by ``game=``, a field the classification collapses to a boolean, so
        the second draft called it live. ALL THREE were green at ``8a6ed27``
        as well — they are escapes of the state under review and not only of
        this round's drafts — and all three are LIVE: sixty of sixty again,
        tri-store, with private identities of a side in thirty-three of the
        sixty bodies.

        AND THE ELEVENTH IS ONE CALL DEEPER THAN EITHER CLASS THE REVIEW
        NAMED: a binding inside ``_player_team_for_game`` that the FOLD
        would drop, because ``_bound_forward`` pastes a value into every
        later read of its name and there is none — so the PLAYER pin reads
        as though the statement were not there. Green at ``8a6ed27`` and
        green again against round 19's own conditions 6 and 7, which are
        asked of the two functions this file NAMES and not of a followed
        callee. It is the only one of the eleven whose LIVENESS IS NOT
        CLAIMED: driven tri-store, the expression used answered
        ``restricted=true, players=0`` to both player principals rather than
        a side they do not hold. A coverage escape, refused because the
        derivation could not see the statement.

        SO ROUND 19 CLOSED ELEVEN, of which SEVEN came from the review and
        FOUR from hunting the answer to them.

        ROUND 20 ADDS SIXTEEN and RE-AIMS FIVE, and both halves are the
        refactor. The SIXTEEN are the owner's THREE NAMED VARIANTS — the
        RELOCATION, the ALIAS and the live ``game`` binding — SIX against
        the PROJECTION the boundary now takes, FIVE that do to ``store``
        what round 19's did to the mapping, and two more shapes those
        opened. The FIVE re-aimed are the ones whose target no longer
        exists: three forged the ``scope`` SLOT of a resolver that no longer
        has one, and two rebuilt the resolver's own ``scope`` parameter,
        which is likewise gone. Nothing was DELETED from this battery; a
        spelling that became inexpressible was moved onto the thing the
        refactor left in its place, and this docstring says which.

        TWO OF THE FIFTEEN WERE MEASURED LIVE AT ``f9b094e`` and are the
        reason this round exists: the RELOCATION and the ALIAS each returned
        ``_audit() == []`` there and each, compiled into the running server,
        answered 200 on TWENTY of the FIFTY derived unentitled cells, leaked
        THIRTEEN private side identities, and put EIGHT of HOME's private
        players in the ``gid`` ``/lineups`` body at ``restricted=false`` —
        on Memory, SQLite and real PostgreSQL, byte-identically. THE THIRD,
        the live ``game`` binding, was NOT green there: condition 3 refused
        it for a string literal in the ``game`` slot's resolved value. It is
        in the battery because this round changes exactly that machinery.

        The count is MEASURED: it is ``len(self._spellings())``, asserted
        below against the number this docstring states, and the loop runs
        every key of it.

        A spelling this cannot READ fails as an unresolvable shape naming
        the line, which is the same fail-closed answer
        ``route_extract.query_parameter_names`` gives a query string it
        cannot enumerate; a spelling it CAN read fails naming the ROLE whose
        branch moved — ``GUARDIAN`` for the injections that add a branch,
        and ``COACH`` for the three that mutate the gate's existing
        team-scoped one."""
        # THE COUNT ITSELF, so the prose above cannot go stale the way
        # "THIRTY-SEVEN" did (#427 round 20). A spelling added or removed
        # fails HERE, naming both numbers, instead of leaving a remembered
        # figure standing in a docstring nobody re-reads.
        self.assertEqual(
            64, len(self._spellings()),
            "the battery's size has moved and this test's docstring still "
            "states the old one; re-measure the prose rather than the "
            "number")
        for name, spelling in sorted(self._spellings().items()):
            with self.subTest(spelling=name):
                injection = dict(spelling)
                names = injection.pop("names", "GUARDIAN")
                failures = self._audit(self._injected(**injection))
                self.assertTrue(
                    failures,
                    f"a new non-admin admission branch spelled {name!r} was "
                    f"NOT reported. Condition 5 is closed only if the "
                    f"BRANCHES are derived from the gate; a spelling that "
                    f"slips past this is a role admitted with no authority "
                    f"and no oracle in this file modelling it.")
                self.assertTrue(
                    any(names in f or "unresolvable" in f
                        for f in failures),
                    f"{name!r} was reported, but not in a way that NAMES the "
                    f"branch it moved ({names}): {failures}")

    #: Statement kinds the walk does NOT handle, each wrapped around a body
    #: that admits a Guardian. The point is not that any of these is a
    #: likely refactor — it is that the walk's DEFAULT is refusal, so a
    #: statement kind nobody anticipated cannot be silently stepped over.
    #: ``try/except*`` is 3.11's addition and ``type X = …`` is 3.12's:
    #: neither was in the deny-list this replaced, because neither existed
    #: when it was written, which is the whole argument.
    UNHANDLED_STATEMENTS = {
        "while": "    while role == Role.GUARDIAN:\n{body}",
        "for": "    for _ in (role,):\n{body}",
        "with": "    with contextlib.nullcontext():\n{body}",
        "try": "    try:\n{body}    except Exception:\n        pass\n",
        "try_star": ("    try:\n{body}    except* Exception:\n"
                     "        pass\n"),
        "nested_def": ("    def _inner():\n{body}"
                       "    return _inner()\n"),
        "class_body": ("    class _C:\n"
                       "        x = 1\n"),
        "conditional_import": "    from ..domain import Role as _R2\n",
        "delete": "    del store\n",
        "aug_assign": "    scope += scope\n",
        "assert": "    assert role != Role.GUARDIAN\n",
        "raise": "    raise RuntimeError('no')\n",
        "global_": "    global _cache\n",
        "walrus_expression_statement": "    (admitted := True)\n",
        "type_alias": "    type _Alias = int\n",
    }

    def test_an_unhandled_statement_kind_is_refused_naming_its_type(self):
        """THE INVERSION ITSELF — the property this round exists for.

        The walk used to be a DENY-LIST of control-flow statement kinds with
        no ``else``, so anything unlisted fell through and was SKIPPED.
        ``ast.Match`` was unlisted, and a ``match role: case Role.GUARDIAN:``
        arm therefore admitted a real caller to a real game with a real side
        while ``admission_branches()`` reported nothing — see the
        ``match_statement`` spelling above, which is now attributed and
        fails by name.

        Adding ``ast.Match`` to that tuple would have been the same defect
        one instance along. So the walk now REFUSES by default, and this
        makes two claims about that default.

        FIRST, IT FIRES AND IT NAMES THE TYPE: every kind in
        :data:`UNHANDLED_STATEMENTS` — ``len()`` of it, FIFTEEN on the tree
        this sentence was written on, and each one MEASURED below to build a
        DISTINCT ``ast`` type rather than assumed to — is required to raise
        an :class:`AdmissionExtractionError` whose message contains the
        statement type, not to be stepped over and not to be reported as
        some other shape. (``except*`` is its own node, ``ast.TryStar``, not
        a ``Try``; that was measured too, after the first draft of this test
        asserted otherwise and went red.) Exactly one is newer grammar than
        CI's 3.11 — ``type X = …``, which is 3.12+ — and a ``SyntaxError``
        from the interpreter under test is not a hole, so a template is
        skipped ONLY when the grammar rejects it, never when the walk merely
        fails to raise; the bookkeeping below asserts that partition rather
        than trusting it.

        SECOND, AND THIS IS THE PART THE SAMPLES CANNOT CARRY: that there IS
        a default at all — that NO statement kind can fall off the end of
        the chain the way ``ast.Match`` did, including kinds this Python
        does not have yet — is a property of the chain's SHAPE, not of any
        list of samples. So it is read off :func:`_decisions`' own source:
        the ``if``/``elif`` chain must terminate in an ``else`` that raises.
        A future round that adds a case and forgets the ``else`` reddens
        here rather than in whichever release ships the next statement
        kind."""
        self._assert_the_walk_ends_in_a_raising_else()
        seen, attempted, skipped = set(), [], []
        for name, template in sorted(self.UNHANDLED_STATEMENTS.items()):
            with self.subTest(statement=name):
                source = self._injected(replacement=template.format(
                    body=self.ADMITTING_BODY) + self.ANCHOR)
                try:
                    ast.parse(source)
                except SyntaxError:
                    skipped.append(name)
                    self.skipTest(
                        f"{name!r} is not this interpreter's grammar "
                        f"(running {sys.version_info.major}."
                        f"{sys.version_info.minor})")
                with self.assertRaises(AdmissionExtractionError) as caught:
                    admission_branches(source=source)
                message = str(caught.exception)
                node = next(
                    type(s).__name__ for s in ast.walk(ast.parse(source))
                    if isinstance(s, ast.stmt)
                    and s.lineno == self._first_injected_line(source))
                self.assertIn(
                    node, message,
                    f"a {node} statement was refused, but the message does "
                    f"not name the type, so nobody reading the failure "
                    f"learns which shape the gate grew: {message}")
                seen.add(node)
                attempted.append(name)
        # Each template must build a DISTINCT statement kind, or the count
        # in the docstring is measuring fewer shapes than it names. MEASURED
        # both ways rather than pinned to a literal, so the interpreter's
        # grammar decides how many run and nothing here has to remember
        # which release added which kind.
        self.assertEqual(
            len(attempted), len(seen),
            f"{len(attempted)} templates produced only {len(seen)} distinct "
            f"statement kinds ({sorted(seen)}), so two of them build the "
            f"same shape and one of the kinds this claims to cover is not "
            f"actually exercised")
        self.assertEqual(
            frozenset(self.UNHANDLED_STATEMENTS) - frozenset(attempted),
            frozenset(skipped),
            "a template neither ran nor was refused by the grammar")

    # -- ROUND 14: one named test per MODEL, each with its own falsifier ---
    #
    # THE STANDARD THIS ROUND IS HELD TO. Rounds 5-13 each closed the axis
    # somebody had just enumerated by hand; round 13 correctly inverted the
    # STATEMENT-KIND axis and a hostile round of 55 injections could not find
    # a statement-kind hole. What got through were the three remaining
    # hand-reasoned MODELS — the binding model, the grant model and the
    # role-parameter identity model — none of which failed closed on a shape
    # it had not seen. Hunting for a FOURTH found three more, all of the same
    # species and none a statement-kind hole: the DELEGATION model (what
    # resolves the side is not what the branch does with it), the
    # DELEGATION-DROP model (a branch was skipped entirely on the strength of
    # an assignment), and the ONE-DEFINITION model (the audited `def` and the
    # running `def` could be different functions).
    #
    # So each test below states its model, and each REMOVES ITS OWN RULE and
    # requires the identical mutation to go green, because a refusal nothing
    # can falsify is a refusal that may already have stopped biting. Where
    # two rules overlap on one shape, the overlap is MEASURED and stated
    # rather than hidden behind a weaker falsifier.

    @contextlib.contextmanager
    def _without(self, name, replacement):
        """One rule of the derivation, removed in process."""
        module = sys.modules[__name__]
        real = getattr(module, name)
        setattr(module, name, replacement)
        try:
            yield
        finally:
            setattr(module, name, real)

    def _assert_only_this_rule_catches(self, source, rule, replacement,
                                       expected):
        """``source`` is refused by name WITH ``rule`` and green WITHOUT it.

        Both halves are required. The first alone would pass for a mutation
        something ELSE already caught, which is how a refusal quietly stops
        being the thing that matters."""
        failures = self._audit(source)
        self.assertTrue(failures, f"{rule} did not report this mutation")
        self.assertTrue(
            any(expected in f for f in failures),
            f"{rule} reported the mutation without naming it: {failures}")
        with self._without(rule, replacement):
            blind = self._audit(source)
        self.assertEqual(
            [], blind,
            f"the mutation is ALREADY reported with {rule} removed, so this "
            f"test is not measuring {rule} at all: {blind}")

    def test_every_binding_form_the_poison_rule_reads_is_this_grammar(self):
        """THE 3.11 CHECK, MADE BY 3.11 ITSELF.

        :func:`_assigned_names` answers "what might this block have changed"
        and reads a different field off each binding form — ``Assign.targets``
        but ``AnnAssign.target``, ``ClassDef.name`` but ``Import.names``,
        ``MatchMapping.rest``. CI runs 3.11 and this was written on 3.14, and
        a field that is not there does not raise: the ``isinstance`` simply
        never matches and the form silently contributes NOTHING, which is
        the permissive answer and exactly the failure mode this whole round
        exists to remove.

        So the pairs are READ OFF :func:`_assigned_names`' OWN SOURCE and
        checked against the RUNNING interpreter's ``ast``, rather than
        against a list somebody copied out of the 3.11 grammar. Round 13
        checked ``ast.Match``'s PEP 634 fields the same way after a first
        draft asserted ``except*`` was an ``ast.Try`` and went red."""
        source = textwrap.dedent(inspect.getsource(_assigned_names))
        fn = ast.parse(source).body[0]
        loop = next(n for n in ast.walk(fn) if isinstance(n, ast.For))
        chain = next(n for n in loop.body if isinstance(n, ast.If))
        pairs, checked = [], 0
        while True:
            calls = [n for n in ast.walk(chain.test) if isinstance(n, ast.Call)
                     and getattr(n.func, "id", None) == "isinstance"]
            for call in calls:
                spec = call.args[1]
                names = [ast.unparse(e) for e in spec.elts] \
                    if isinstance(spec, ast.Tuple) else [ast.unparse(spec)]
                # The fields read off `sub` in the TEST and in the BODY this
                # test guards — `sub.name` appears in one, `sub.targets` in
                # the other, and both are fields of the classes named here.
                pairs.append((names, [chain.test] + list(chain.body)))
            if len(chain.orelse) == 1 and isinstance(chain.orelse[0], ast.If):
                chain = chain.orelse[0]
                continue
            break
        self.assertTrue(pairs, "no isinstance test found in _assigned_names")
        for names, region in pairs:
            attributes = {n.attr for node in region
                          for n in ast.walk(node)
                          if isinstance(n, ast.Attribute)
                          and getattr(n.value, "id", None) == "sub"}
            for spelled in names:
                self.assertTrue(
                    spelled.startswith("ast."),
                    f"{spelled!r} is not an `ast` node class")
                klass = getattr(ast, spelled[len("ast."):], None)
                self.assertIsNotNone(
                    klass,
                    f"{spelled} does not exist on Python "
                    f"{sys.version_info.major}.{sys.version_info.minor}, so "
                    f"the binding form it names contributes NOTHING here and "
                    f"a block that uses it poisons nothing")
                for attribute in sorted(attributes):
                    self.assertIn(
                        attribute, klass._fields,
                        f"{spelled} has no field {attribute!r} on this "
                        f"interpreter, so `_assigned_names` reads a name off "
                        f"it that is not there")
                    checked += 1
        # …and the two helper nodes whose fields are read off something other
        # than `sub`, which the loop above cannot see.
        self.assertIn("optional_vars", ast.withitem._fields)
        self.assertIn("asname", ast.alias._fields)
        self.assertIn("name", ast.alias._fields)
        self.assertGreaterEqual(
            checked, 12,
            f"only {checked} (node, field) pairs were checked, so this test "
            f"is no longer reading the binding forms off the function")

    def test_a_binding_a_nested_block_may_have_changed_is_refused(self):
        """THE BINDING MODEL, and the half of it no other rule covers.

        ``walk`` copies its bindings per body, so an assignment inside a
        nested block never reached the statements AFTER it and ``_unfold``
        went on reporting the value from BEFORE the block. The mutation here
        re-binds the TRUSTED SIDE inside a nested ``if`` and changes nothing
        else: the resolver is still called, so the delegation stands; the
        admission predicate is still the pinned one; and the ``own_team=``
        keyword is still the pinned ``own_team if admitted else None``. All
        THREE pins therefore read exactly what they pinned, and only
        :func:`_poison` — which marks ``own_team`` unresolvable for the rest
        of that body — refuses it.

        MEASURED LIVE with this mutation compiled into the running server:
        ``thirdcoach``, a coach of a team in NEITHER game, received 200 on
        ``/lineups``, ``/roster`` and ``/roster-status`` with the HOME side's
        private rows."""
        source = self._injected(
            **{k: v for k, v in
               self._spellings()["a_nested_block_rebinds_the_trusted_side"]
               .items() if k != "names"})
        self._assert_only_this_rule_catches(
            source, "_poison", lambda bindings, stmt: None,
            "assigned inside a nested block")

    def test_a_name_this_body_already_bound_is_not_re_bound(self):
        """THE BINDING MODEL, the outer half — the shape the reproduction
        started from.

        ``_ok = False`` … ``if game is not None: _ok = True`` … and the
        return reads the OUTER literal, so ``admits`` was False and the
        branch was exempted while every caller reached it with ``_ok`` True.
        The rule that answers it is one binding per name per body: which of
        the two expressions a later read means is a flow question this walk
        does not answer.

        THREE CLAIMS, and the OVERLAPS ARE STATED rather than arranged
        away. First, a shape only this rule answers — the trusted side bound
        twice in the SAME body, no nesting anywhere, every pin still reading
        what it pinned. Second, both stale-outer shapes are refused AS
        re-bindings. Third, ``stale_outer_literal_admitted`` — where the
        stale name is ``admitted=`` and nothing else in the derivation has
        anything to say — goes green when BOTH rules of this model are
        removed, which is what shows the model and not something else is
        carrying it.

        ``stale_outer_literal_game`` is deliberately NOT held to that third
        claim: with both binding rules removed it is still reported, by the
        GRANT model, because ``own_team=game.home_team_id`` is a real side
        whatever the stale ``game=`` keyword says. Two of this round's four
        models catching one shape between them is a fact about the shape, so
        it is measured here rather than hidden by picking a weaker
        falsifier."""
        nothing = lambda fn, stmt, bindings, bound: None      # noqa: E731
        no_poison = lambda bindings, stmt: None               # noqa: E731
        self._assert_only_this_rule_catches(
            self._injected(**{
                k: v for k, v
                in self._spellings()["the_trusted_side_bound_twice"].items()
                if k != "names"}),
            "_refuse_a_second_binding", nothing, "RE-ASSIGNS")
        for spelling in ("stale_outer_literal_admitted",
                         "stale_outer_literal_game"):
            with self.subTest(spelling=spelling):
                source = self._injected(**self._spellings()[spelling])
                self.assertTrue(
                    any("RE-ASSIGNS" in f for f in self._audit(source)),
                    f"{spelling!r} is not refused as a re-binding")
        stale = self._injected(
            **self._spellings()["stale_outer_literal_admitted"])
        with self._without("_refuse_a_second_binding", nothing):
            self.assertTrue(
                self._audit(stale),
                "the stale-outer shape is answered by the re-binding rule "
                "ALONE, so the poisoning rule is not the second line of "
                "defence this claims")
            with self._without("_poison", no_poison):
                self.assertEqual(
                    [], self._audit(stale),
                    "the stale-outer shape is still reported with BOTH "
                    "rules of the binding model removed, so neither of them "
                    "is what catches it")

    def test_a_function_that_rebinds_its_role_parameter_is_refused(self):
        """THE ROLE-IDENTITY MODEL.

        ``_role_parameter`` reads the role's name off the signature and the
        walk then reads every role test as being about that name — without
        ever checking the name still HOLDS the parameter. The walk ALREADY
        RECORDED the binding; the information needed to refuse this was
        present and unused."""
        nothing = lambda fn, stmt, role_param, bound: None     # noqa: E731

        def no_vouch(call, carrier, resolver, bindings, where,
                     projected=frozenset()):
            return None

        for spelling in ("role_parameter_rebound_if",
                         "role_parameter_rebound_match"):
            with self.subTest(spelling=spelling):
                source = self._injected(**self._spellings()[spelling])
                failures = self._audit(source)
                self.assertTrue(
                    any("ASSIGNS TO ITS OWN ROLE PARAMETER" in f
                        for f in failures),
                    f"the role parameter was rebound and the refusal did not "
                    f"name it: {failures}")
                # THE OVERLAP, MEASURED AND STATED rather than arranged away
                # (#427 round 16), the way round 14 states the one between
                # its two binding rules. With this rule removed the mutation
                # is STILL reported — by the ARGUMENT PROVENANCE rule, one
                # branch further down: the gate's COACH/PLAYER branch hands
                # the resolver `role`, and `role` now holds
                # `Role.LEAGUE_ADMIN`, which is not a parameter of the gate.
                # So the falsifier removes BOTH, and only then is the
                # mutation green — which is what shows this MODEL, and not
                # something else, is what carries the shape.
                with self._without("_refuse_a_rebound_role", nothing):
                    self.assertTrue(
                        self._audit(source),
                        "the rebinding is answered by the role-identity "
                        "rule ALONE, so the argument rule is not the second "
                        "line of defence this claims")
                    with self._without("_refuse_an_unvouched_argument",
                                       no_vouch):
                        self.assertEqual(
                            [], self._audit(source),
                            "the rebinding is still reported with BOTH the "
                            "role-identity rule and the argument rule "
                            "removed, so neither of them is what catches it")

    def test_a_real_side_with_no_game_still_needs_an_authority(self):
        """THE GRANT MODEL.

        ``needs_authority`` was ``admits and carries_game``, and its ground
        was the record's own docstring: ``game=None`` is the not-found
        passthrough and "grants nothing". It grants nothing BY ITSELF, which
        is a different sentence — ``web/server.py`` reads ``admitted``,
        ``own_team`` and ``side_ids``, NEVER re-checks ``game``, and
        re-fetches the game by id for every leaf. So a real ``own_team``
        with ``game=None`` is a full disclosure the audit exempted.

        The falsifier restores the old condition rather than deleting a
        refusal, because this model's failure was an over-narrow definition
        and not a missing raise."""
        for spelling in ("side_without_a_game_literal",
                         "side_without_a_game_unfolded"):
            with self.subTest(spelling=spelling):
                source = self._injected(**self._spellings()[spelling])
                failures = self._audit(source)
                self.assertTrue(
                    any("GUARDIAN" in f for f in failures),
                    f"a branch answering a REAL side with `game=None` was "
                    f"not reported: {failures}")
                with mock.patch.object(
                        AdmissionBranch, "needs_authority",
                        property(lambda b: b.admits and b.carries_game)):
                    blind = self._audit(source)
                self.assertEqual(
                    [], blind,
                    "the round-13 grant condition already reports this "
                    "branch, so this test is not measuring the change it "
                    "claims to: " + str(blind))

    def test_the_grant_condition_is_derived_from_what_the_consumers_read(
            self):
        """…AND THE MODEL ABOVE IS CLOSED AGAINST THE PRODUCT, not decided
        here.

        :func:`_carrier_reads` finds every attribute the package reads off a
        carrier record. :data:`CARRIER_READ_KINDS` must answer for exactly
        that set — a consumer that starts reading a new one is an error
        naming it rather than a classification inherited from the old ones —
        and the reads that NAME A SIDE must map back, through the record's
        own fields and properties, to exactly the two keywords
        ``needs_authority`` reads.

        MEASURED on this tree: ``web/scope.py`` reads ``admitted``;
        ``web/server.py`` reads ``admitted``, ``own_team`` and ``side_ids``;
        and ``side_ids`` is a property over ``game``."""
        modules, reads = _carrier_reads()
        self.assertEqual(
            {"hockey_scheduler/web/scope.py": frozenset({"admitted"}),
             "hockey_scheduler/web/server.py": frozenset(
                 {"admitted", "own_team", "side_ids"})},
            modules,
            "the set of modules that consume a private-game decision, or "
            "what they read off it, has changed — so what a branch GRANTS "
            "by filling a keyword has to be re-derived")
        self.assertEqual(
            frozenset(CARRIER_READ_KINDS), reads,
            "the product reads an attribute off the carrier record that "
            "CARRIER_READ_KINDS does not classify (or classifies one it no "
            "longer reads), so `needs_authority` is deciding what a branch "
            "grants from a list that has drifted from the consumers")
        self.assertEqual(
            frozenset({"game", "own_team"}), GRANT_BEARING_FIELDS,
            "the record fields a side-naming read is answered out of are no "
            "longer the two `needs_authority` measures")
        # …and each of those two really is one of the record's own keywords,
        # so `carries_game`/`grants_side` are measuring fields the gate sets
        # rather than names this file invented.
        self.assertLessEqual(GRANT_BEARING_FIELDS, ADMISSION_FIELDS)
        self.assertEqual(
            frozenset({"admitted"}),
            frozenset(a for a, kind in CARRIER_READ_KINDS.items()
                      if kind == READ_GATES_ADMISSION),
            "more than one read gates admission, so `admits` alone no "
            "longer says whether the caller is answered at all")

    def test_a_consumer_this_cannot_follow_is_refused(self):
        """…AND THE DERIVATION ABOVE FAILS CLOSED, in both directions it can.

        The set of reads is only an authority if it cannot quietly miss one.
        A record handed to something else, or held in a name that is passed
        on rather than read, takes its reads somewhere this does not see —
        so both are refusals rather than empty answers. The control is a
        module shaped like the two real consumers, which must resolve
        cleanly."""
        control = (f"x = {GATE_CARRIER}(role, scope, gid, store)\n"
                   f"y = x.own_team\n"
                   f"z = {GATE_CARRIER}(role, scope, gid, store).admitted\n")
        self.assertEqual(frozenset({"own_team", "admitted"}),
                         _reads_in("control.py", control))
        self.assertIsNone(_reads_in("none.py", "x = 1\n"),
                          "a module with no call site must answer None, not "
                          "an empty set that would agree with anything")
        for label, source in (
                ("handed straight to something else",
                 f"log({GATE_CARRIER}(role, scope, gid, store))\n"),
                ("returned from the call site",
                 f"def f():\n"
                 f"    return {GATE_CARRIER}(role, scope, gid, store)\n"),
                ("held, then passed on whole",
                 f"x = {GATE_CARRIER}(role, scope, gid, store)\n"
                 f"project(x)\n")):
            with self.subTest(shape=label):
                with self.assertRaises(AdmissionExtractionError):
                    _reads_in("mutant.py", source)

    def test_a_delegating_branch_is_pinned_on_what_it_itself_does(self):
        """THE FOURTH MODEL — found by this round's own hunt, not handed to
        it, and the only one of the four that needs no new branch.

        ``authority`` is the RESOLVER'S expression whenever the branch
        delegates, so it is a statement about what
        ``game_scoped_own_team_id`` answers and NOT about what the branch
        does with the answer. A branch could therefore resolve the caller's
        side through the fully audited resolver and then admit on a weaker
        test, or return a DIFFERENT side, and stay booked under the
        resolver's authority.

        MEASURED LIVE at ``c4a725b`` with the second of the two compiled
        into the running server — the COACH/PLAYER branch resolving
        ``own_team`` exactly as it does today and returning
        ``own_team=game.home_team_id`` — ``thirdcoach`` received 200 with
        HOME's private state on ALL FIVE leaves of the family
        (``/lineups``, ``/board``, ``/roster``, ``/roster-status``,
        ``/substitutes``) and ``_audit()`` returned ``[]``.

        THE FALSIFIER IS THE PIN ITSELF: re-pin ``ADMISSION_AUTHORITIES`` on
        what the mutation produces and the audit goes silent, which is what
        shows the pin is what bites and not something else in the chain."""
        for spelling, moved in (
                ("delegated_branch_drops_its_own_test", "admitted by"),
                ("delegated_branch_returns_another_side",
                 "answering the side")):
            with self.subTest(spelling=spelling):
                injection = {k: v for k, v
                             in self._spellings()[spelling].items()
                             if k != "names"}
                source = self._injected(**injection)
                failures = self._audit(source)
                self.assertTrue(
                    any(moved in f and "COACH" in f for f in failures),
                    f"a branch that delegates to the audited resolver and "
                    f"then {moved} something else was not reported: "
                    f"{failures}")
                self.assertTrue(
                    any(moved in f and "PLAYER" in f for f in failures),
                    f"only one of the two roles the branch serves was "
                    f"reported: {failures}")
                # RE-PINNED ON WHAT THE MUTATION DOES: the audit must then
                # be silent, so what caught it is this pin and nothing else.
                moved_branches = {
                    role: next(b for b in branches
                               if b.needs_authority
                               and role in ADMISSION_AUTHORITIES)
                    for role, branches
                    in admission_branches(source=source).items()
                    if role in ("COACH", "PLAYER")}
                repinned = dict(ADMISSION_AUTHORITIES)
                for role, branch in moved_branches.items():
                    repinned[role] = dataclasses.replace(
                        ADMISSION_AUTHORITIES[role],
                        admits=branch.admits_source,
                        side=branch.side_source)
                with mock.patch.dict(ADMISSION_AUTHORITIES, repinned,
                                     clear=True):
                    blind = self._audit(source)
                self.assertEqual(
                    [], blind,
                    "the mutation is still reported after the pins are "
                    "moved onto it, so this test is not measuring the pins: "
                    + str(blind))

    def test_a_branch_is_only_excused_by_the_resolver_it_rests_on(self):
        """THE FIFTH MODEL, and the only one whose failure DROPPED a branch
        rather than mis-describing it.

        ``admission_branches`` skips a role the resolver answers with
        nothing, on the ground that a caller with no side has nothing to be
        admitted to. That ground held only because the real branch's
        admission IS the resolver's answer — and the skip was taken on the
        strength of the ASSIGNMENT alone. A branch that calls the resolver,
        ignores what it said and admits the role outright was therefore not
        recorded AT ALL: not attributed to the wrong authority, simply
        absent, so no failure could name it.

        MEASURED at ``c4a725b``, spelled for ``Role.COACH`` so the
        projection layer answers a side: ``thirdcoach`` — a coach of a team
        in NEITHER game — received 200 with HOME's private rows on
        ``/lineups``, ``/roster`` and ``/roster-status``. Spelled for a role
        with no ``ADMISSION_AUTHORITIES`` entry, ``_audit()`` returned
        ``[]``.

        The rule is now that BOTH the admission and the side the branch
        returns must MENTION the name the resolver's answer was bound to.
        The falsifier restores the old ground — ``_rests_on`` answering yes
        to everything — and requires the identical injection to go green."""
        self._assert_only_this_rule_catches(
            self._injected(**self._spellings()[
                "delegating_branch_that_ignores_the_resolver"]),
            "_rests_on", lambda node, names: True,
            "GUARDIAN is ADMITTED UNCONDITIONALLY")
        # THE CONTROL IS THE ONE THAT WAS ALREADY HERE: widening the
        # carrier's role tuple and touching nothing else still admits
        # nobody, because THAT branch's admission really does rest on the
        # resolver. The new rule must not have turned the control red.
        self.assertEqual([], self._audit(self._injected(
            replacement="    if role in (Role.COACH, Role.PLAYER, "
                        "Role.GUARDIAN):")))

    # -- ROUND 16: WHAT A DELEGATING BRANCH FEEDS THE AUDITED RESOLVER ----

    #: Argument expressions to hand the resolver in the ``scope`` slot, one
    #: per ``ast`` expression kind somebody might plausibly write. NOT a list
    #: of kinds the rule knows: the rule allows exactly ``ast.Name`` and
    #: refuses everything else, so this table only has to show that "refuses
    #: everything else" is a real answer over a real spread of shapes —
    #: including kinds this Python has and kinds it may gain.
    #: Twelve argument SHAPES, aimed at the resolver's ``game`` slot.
    #:
    #: THEY USED TO FILL THE ``scope`` SLOT, and round 20 moved them for a
    #: reason worth stating (#427). The mapping is not a slot any more, and
    #: its replacements — the PROJECTED ids — are answered by TWO rules at
    #: once: conditions 1 and 2 refuse anything that is not the projected
    #: name, and condition 8 refuses anything that is not that key's own
    #: projection. That overlap is deliberate and named in
    #: :func:`_refuse_an_unprojected_slot`, but it means a projected slot can
    #: no longer FALSIFY conditions 1-2 on its own: removing them leaves
    #: condition 8 answering, so the test would measure nothing.
    #:
    #: ``game`` is the slot where conditions 1 and 2 still speak ALONE —
    #: it is not projected and it is not a carrier parameter, so neither
    #: condition 5 nor condition 8 has anything to say about it — which
    #: makes it the one place this rule has a falsifier of its own.
    FORGED_GAME_ARGUMENTS = {
        "a reconstructed dict": "{'home_team_id': game.home_team_id}",
        "a literal": "None",
        "a string literal": "'game_1'",
        "an attribute": "game.home_team_id",
        "a call": "store.get_game(game_id)",
        "a subscript": "[game][0]",
        "a conditional": "game if game else store.get_game(game_id)",
        "a walrus": "(_g := store.get_game(game_id))",
        "a tuple": "(game,)",
        "a comparison": "game or store.get_game(game_id)",
        "a lambda's result": "(lambda s: s.get_game(game_id))(store)",
        "a starred argument": "*[game]",
    }

    def test_a_forged_argument_into_the_resolver_is_refused(self):
        """THE SEVENTH MODEL: what a delegating branch HANDS the resolver.

        ``admission_branches`` recorded WHETHER a branch delegates and WHAT
        THE RESOLVER RETURNS FOR THAT ROLE, and the resolver's call ARGUMENTS
        appeared in none of the three pins — ``authority`` is the resolver's
        own return expression, and ``admits``/``side`` unfold to the
        participation predicate and ``own_team if admitted else None``, which
        a forged argument leaves CHARACTER FOR CHARACTER unchanged.

        MEASURED LIVE at ``d3f5f1e`` with the gate compiled into the running
        server and driven over real authenticated HTTP, changing ONLY the
        argument list to ``role, {'team_id': game.home_team_id}, game,
        store``: ``thirdcoach`` — a Coach of a team in NEITHER game —
        received 200 on ``/lineups`` (HOME ``restricted false``, eight
        private rows: ``player_16``, ``player_2``, ``player_15``, ``player_8``,
        ``player_9``, ``player_4``, ``player_3``, ``player_1``), on
        ``/board`` (the full HOME pool and status block), on ``/roster``,
        on ``/roster-status`` (``team_id: team_1``) and on ``/substitutes``
        — while ``_audit()`` returned ``[]``.

        THE RULE FAILS CLOSED ON A SHAPE IT HAS NOT SEEN, which is the
        standard this round is held to. The allow-list is exactly ONE
        expression kind — a bare ``Name`` — so a kind Python has not shipped
        yet is refused for the same reason a ``Dict`` is, and there is no
        list to be one release behind. The falsifier removes the rule and
        requires every one of these to go green."""
        def no_vouch(call, carrier, resolver, bindings, where,
                     projected=frozenset()):
            return None

        # THE PREMISE: the vouching actually runs on the real gate. A rule
        # applied to no call would agree with everything.
        tree = ast.parse(self._gate_source())
        carrier = _gate_function(tree, GATE_CARRIER, "gate")
        delegations = [calls for _n, _r, calls, _b in _decisions(
            carrier, _role_aliases(tree), _module_constants(tree),
            GATE_RESOLVER) if calls]
        self.assertEqual(
            [1], [len(c) for c in delegations],
            "the gate's decisions rest on a different number of resolver "
            "calls than the one this rule is measured against")
        for label, argument in sorted(self.FORGED_GAME_ARGUMENTS.items()):
            with self.subTest(shape=label):
                source = self._injected(prelude=(
                    self.RESOLVER_CALL,
                    f"        own_team = game_scoped_own_team_id(\n"
                    f"            role, scoped_team_id, scoped_player_id, "
                    f"{argument}, store)\n"))
                failures = self._audit(source)
                self.assertTrue(
                    any("unresolvable admission shape" in f for f in
                        failures),
                    f"{label} in the game slot was not refused: {failures}")
                with self._without("_refuse_an_unvouched_argument", no_vouch):
                    blind = self._audit(source)
                self.assertEqual(
                    [], blind,
                    f"{label} is ALREADY reported with the argument rule "
                    f"removed, so this test is not measuring it: {blind}")

    def test_a_carrier_input_the_gate_rebuilt_is_not_the_one_it_received(
            self):
        """THE HALF NO CALL-SITE READING CAN ANSWER.

        RE-AIMED IN ROUND 20 FROM ``scope`` TO ``store``, AND RENAMED FOR IT
        (#427). The PROVENANCE clause this exercises — "a name the
        resolver's signature gives a slot must still DERIVE from the gate's
        own parameter of that name" — can only speak about a slot that is
        both a resolver parameter and a carrier parameter. ``scope`` is not
        a slot any more, and its old spelling is now condition 9's (see
        :data:`RAW_SCOPE_ONLY_SPELLINGS`). ``store`` is: it is handed
        straight through, and it is the object the PLAYER authority's
        ``get_player`` and the OFFICIAL authority's
        ``assignments_for_game`` are both read off, so a ``store`` the gate
        BUILT is a gate deciding from rows it made up.

        ONE LINE, above every branch, replacing the gate's own normalisation
        of the session scope::

            scope = {'team_id': store.get_game(game_id).home_team_id}

        The delegation's text does not move — it still reads ``scope`` —
        and neither does any of the three pins. What moves is what ``scope``
        HOLDS, so the rule that answers it is the PROVENANCE half: a name
        the resolver's signature gives a slot must still derive from the
        gate's own parameter of that name.

        MEASURED LIVE at ``d3f5f1e``: ``thirdcoach`` received 200 with HOME's
        eight private rows on all five leaves of the family, ``_audit() ==
        []``.

        AND THE OVERLAP IS STATED. Spelled as an EXTRA statement rather than
        a replacement — ``scope = scope or {}`` followed by ``scope = {…}`` —
        round 14's :func:`_refuse_a_second_binding` answers it first, because
        that is a name bound twice in one body. It is the REPLACEMENT
        spelling that reaches this rule, and it is the one that survived
        everything before this round."""
        source = self._injected(prelude=(
            self.SUBJECT_FETCH, "    store = game_id\n" + self.SUBJECT_FETCH))
        self._assert_only_this_rule_catches(
            source, "_refuse_an_unvouched_argument",
            lambda call, carrier, resolver, bindings, where,
            projected=frozenset(): None,
            "no longer derives from the gate's OWN 'store' parameter")
        # The EXTRA-statement spelling, and which rule answers it.
        second = self._injected(prelude=(
            self.SUBJECT_FETCH,
            "    store = game_id\n"
            "    store = game_id\n" + self.SUBJECT_FETCH))
        self.assertTrue(
            any("RE-ASSIGNS" in f for f in self._audit(second)),
            "the second-binding spelling is not answered by round 14's "
            "one-binding-per-body rule any more")

    def test_a_carrier_parameter_the_gate_rebuilt_is_refused(self):
        """CONDITION 5 — THE OWNER'S BLOCKER 2 (#427 round 18).

        Round 16's provenance rule VOUCHED A NAME, NOT A VALUE. The exact
        spelling the owner's review names, replacing only the carrier's own
        normalisation::

            scope = scope.fromkeys(
                scope, store.get_game(game_id).home_team_id)

        passes all four of round 16's conditions — the call argument is the
        bare ``scope``, it is the resolver's own parameter name for that
        slot, its only ``Name`` leaves are ``scope``, ``store`` and
        ``game_id`` (all carrier parameters), it contains no ``Constant``,
        and it still MENTIONS ``scope``. MEASURED at ``73faf4d``:
        ``_audit(mutated_source)`` returned ``[]``, and the live equivalent
        gave ``thirdcoach`` — a Coach of a team in NEITHER game — 200 on
        ``/lineups`` with HOME ``restricted=false`` and EIGHT private
        players.

        THREE CLAIMS HERE, and the third is what makes the first two mean
        something.

        1. The spelling is REFUSED, and the failure NAMES THE REBUILT
           ARGUMENT rather than merely reporting that something is wrong.
        2. It is refused ONLY by :func:`_refuse_a_rebuilt_carrier_argument`
           — with that one rule removed the audit is green again, so this is
           not measuring some other rule that happened to answer first.
        3. THE REAL GATE STILL PARSES. ``scope = scope or {}`` is also a
           rebinding, so a blanket refusal would refuse the gate itself;
           this asserts that the distinction drawn is between IDENTITY-
           PRESERVING NORMALISATION and VALUE REBUILDING and not between
           "the gate's spelling" and "everything else" — the normalisation
           written with a DIFFERENT empty default is admitted, and a default
           that CARRIES A SIDE is refused."""
        source = self._injected(**{
            k: v for k, v in
            self._spellings()["the_store_rebuilt_out_of_the_gates_own_inputs"]
            .items() if k != "names"})
        self._assert_only_this_rule_catches(
            source, "_refuse_a_rebuilt_carrier_argument",
            lambda call, carrier, resolver, parameter, value, params: None,
            "REBUILT rather than RECEIVED")
        self.assertTrue(
            any("'store'" in f for f in self._audit(source)),
            "the refusal does not NAME the rebuilt argument: "
            + str(self._audit(source)))
        # (3) THE TENSION, MEASURED IN BOTH DIRECTIONS rather than asserted,
        # AND WITH THE ANSWERING RULE NAMED PER SPELLING. Three of these
        # rebuilding shapes are refused by conditions the earlier rounds
        # already wrote, and pinning WHICH rule answers each is the point:
        # "refused" alone would let condition 5 quietly stop mattering while
        # this test went on passing. `None` means the audit is GREEN — the
        # gate's own normalisation, which a blanket refusal would break.
        for spelling, answered_by in (
                ("store = store or {}\n", None),
                ("store = store or []\n", None),
                ("store = store or {} or {}\n", None),
                # A default that CARRIES A SIDE reaches the resolver on
                # exactly the input the caller supplied nothing for — and
                # round 16's LITERAL condition already refuses it.
                ("store = store or {'team_id': game_id}\n",
                 "built partly out of the literal"),
                # A name that is not a parameter of this gate at all —
                # round 16's PROVENANCE condition.
                ("store = dict(store)\n", "which is built out of 'dict'"),
                # …and the shapes that reach CONDITION 5: every leaf is the
                # gate's own `store`, no literal anywhere, and the value is
                # still something the gate BUILT.
                ("store = {**store}\n", "REBUILT rather than RECEIVED"),
                ("store = store if store else store\n",
                 "REBUILT rather than RECEIVED"),
                ("store = store.copy()\n", "REBUILT rather than RECEIVED")):
            with self.subTest(spelling=spelling.strip()):
                mutated = self._injected(prelude=(
                    self.SUBJECT_FETCH,
                    f"    {spelling}{self.SUBJECT_FETCH}"))
                failures = self._audit(mutated)
                if answered_by is None:
                    self.assertEqual(
                        [], failures,
                        f"{spelling.strip()!r} preserves the value the gate "
                        f"RECEIVED — it is the gate's own normalisation with "
                        f"a different empty default — and the audit refuses "
                        f"it, so this rule separates SPELLINGS rather than "
                        f"separating normalisation from rebuilding")
                else:
                    self.assertTrue(
                        any(answered_by in f for f in failures),
                        f"{spelling.strip()!r} hands the resolver a value "
                        f"the gate BUILT and no rule answered it with "
                        f"{answered_by!r}: {failures}")

    def test_the_rebuilt_scope_disclosure_is_not_live(self):
        """THE OTHER HALF OF BLOCKER 2, over real authenticated HTTP on
        Memory, SQLite and real PostgreSQL.

        The static rule above says the gate as written hands the resolver
        the scope it RECEIVED. This says what that is worth on the wire:
        ``thirdcoach`` — a Coach of a team in NEITHER game — is REFUSED
        every leaf of the private-game family, on BOTH games, and no private
        identity of either side appears in any of those bodies.

        WHY BOTH HALVES ARE HERE. The committed delta of this round is
        TEST-ONLY: the rebuilt scope is a hostile mutation, not something
        this head ships, so the audit's new refusal is a COVERAGE claim and
        this is the claim about the PRODUCT that stands beside it. If the
        product ever did what the mutation does, this is the test that
        would say so in three backends rather than in one static walk."""
        ran = []
        for label, store in self._stores():
            try:
                self._assert_backend(label, store)
                store.clear_all_data()
                fx = self._fixture(store)
                who = self._serve(fx)
                private, _ambiguous = self._private_side_ids(fx)
                forbidden = frozenset().union(*private.values())
                self.assertTrue(
                    forbidden,
                    f"[{label}] no side carries a private identity, so "
                    f"'no private rows' asserts nothing")
                for name, spec in sorted(
                        self._private_game_family().items()):
                    for game_key in self.GAME_KEYS:
                        path = self._path_of(spec, (fx[game_key],))
                        status, body = self._req(
                            who["thirdcoach"], "GET", path)
                        self.assertEqual(
                            REFUSED, status,
                            f"[{label}] thirdcoach — a Coach of a team in "
                            f"NEITHER game — was answered {status} on "
                            f"{path}: {body}")
                        blob = json.dumps(body, sort_keys=True, default=str)
                        leaked = sorted(
                            pid for pid in forbidden
                            if re.search(rf"\b{re.escape(pid)}\b", blob))
                        self.assertEqual(
                            [], leaked,
                            f"[{label}] the refusal body on {path} carries "
                            f"private identities {leaked}")
                ran.append((label, self.REBUILT_SCOPE_CASE))
            finally:
                self._close(label, store)
        self._assert_matrix_ran(ran, [self.REBUILT_SCOPE_CASE])

    # -- ROUND 19: THE NAME IS NOT THE VALUE, TWICE MORE ------------------

    #: The five class-A spellings, and the ONE the failure must name. Kept
    #: here rather than re-spelled in the test so `_spellings` stays the one
    #: place a spelling is written down.
    #: CONDITION 6'S OWN SPELLINGS — the ones for which removing
    #: :func:`_refuse_a_mutated_input` really does make the audit green.
    #:
    #: RE-AIMED IN ROUND 20 FROM THE MAPPING TO ``store`` (#427). The five
    #: this used to name mutate ``scope``, and condition 9 answers all five
    #: now — first, and by a rule that is about the mapping rather than about
    #: positions. Keeping them here would have measured nothing: with
    #: condition 6 removed the audit is still red, which
    #: :meth:`_assert_only_this_rule_catches` detects and reports as its own
    #: vacuity. They stay in the battery, under condition 9's test.
    #:
    #: ``store`` IS THE SEAM CONDITION 6 KEEPS, and it is not a contrivance:
    #: it is a mutable object the gate is handed and does NOT project, its
    #: ``get_player`` and ``assignments_for_game`` are two of the three
    #: things the gate decides by, and "a statement that names an input and
    #: whose value nothing here reads is kept for its EFFECT" is exactly as
    #: true of it as it was of the mapping.
    #: ``the_store_subscripted_into_its_own_dict`` is deliberately NOT
    #: here, and the reason is worth recording: a subscript target is read by
    #: :func:`_assigned_names` as binding its BASE, so that spelling poisons
    #: ``store`` and :func:`_resolved` refuses the ``game`` argument before
    #: condition 6 is consulted. It is in the battery and refused by name;
    #: it is not condition 6's falsifier, because it does not falsify it.
    MUTATION_SPELLINGS = ("the_store_mutated_into_a_binding_nothing_reads",
                          "the_store_mutated_inside_a_list_display",
                          "the_store_mutated_by_an_annotated_assignment")

    #: THE SPELLINGS CONDITION 9 ANSWERS ALONE — measured, not assumed.
    #: Every one of these mentions the raw mapping in a position that is
    #: neither its normalisation nor a projection, AND no other rule in this
    #: file answers it: with :func:`_refuse_a_raw_scope_use` removed the
    #: audit goes green for each. Two of them are the owner's own named
    #: variants; the other two are round 16's and round 18's, which USED to
    #: be answered by the provenance and rebuild conditions and are answered
    #: here now because the mapping is no longer a resolver slot for those
    #: conditions to speak about.
    RAW_SCOPE_ONLY_SPELLINGS = (
        "the_official_read_relocated_above_the_role_branches",
        "the_scope_mutated_through_a_local_alias",
        "the_scope_rebuilt_before_every_branch",
        "the_scope_rebuilt_out_of_the_gates_own_inputs",
    )

    #: …and the ones condition 9 answers TOO but not FIRST, with the rule
    #: that does answer first named per spelling. This is the same
    #: "measured in both directions, and which rule answers each" discipline
    #: condition 5's own tension is held to: "refused" alone would let a
    #: rule quietly stop mattering while the test went on passing.
    #:
    #: The pattern is not an accident. A mutation whose value NOTHING reads
    #: is condition 6's by construction — that is what condition 6 is — and
    #: condition 9 catches it as well only because the object being mutated
    #: happens to be the mapping. The two rules overlap exactly where a
    #: statement is both "unread" and "about `scope`", and each keeps its own
    #: falsifier outside that overlap.
    RAW_SCOPE_ALSO_SPELLINGS = (
        ("the_scope_subscripted_before_its_normalisation",
         "_refuse_a_mutated_input"),
        ("the_scope_subscripted_through_its_own_default",
         "_refuse_a_mutated_input"),
        ("the_scope_updated_into_a_binding_nothing_reads",
         "_refuse_a_mutated_input"),
        ("the_scope_mutated_inside_a_list_display",
         "_refuse_a_mutated_input"),
        ("the_scope_mutated_by_an_annotated_assignment",
         "_refuse_a_mutated_input"),
        ("a_mutation_in_a_test_that_constrains_no_role",
         "_refuse_a_mutated_input"),
        ("a_mutation_live_through_a_field_that_collapses",
         "_refuse_a_mutated_input"),
        # THE THIRD NAMED VARIANT, and the one that was never an escape —
        # see the test's own docstring.
        ("the_scope_mutated_through_the_live_game_binding",
         "_refuse_an_unvouched_argument"),
    )

    #: THE THREE THE OWNER'S RULING NAMES, kept apart from the rest so the
    #: report and the test agree on which three they are. Two of them are in
    #: :data:`RAW_SCOPE_ONLY_SPELLINGS` and the third — deliberately — is in
    #: :data:`RAW_SCOPE_ALSO_SPELLINGS`.
    NAMED_REGRESSIONS = ("the_official_read_relocated_above_the_role_branches",
                         "the_scope_mutated_through_a_local_alias",
                         "the_scope_mutated_through_the_live_game_binding")

    def _mutation_source(self, spelling):
        return self._injected(**{k: v for k, v
                                 in self._spellings()[spelling].items()
                                 if k != "names"})

    def test_an_input_mutated_before_the_call_is_refused(self):
        """BLOCKER 2, CLASS A — the OBJECT changed while the NAME was not.

        Condition 5 vouches that the ARGUMENT EXPRESSION at the call site
        evaluates to the value the gate received. It never asks whether that
        value was MUTATED first, so ``scope`` stayed a bare ``Name``, stayed
        the resolver's own parameter name, stayed resolving to ``scope or
        {}`` — and carried a side the caller never sent. MEASURED at
        ``8a6ed27``, five spellings, each above the gate's own
        normalisation::

            scope['team_id'] = store.get_game(game_id).home_team_id
            (scope or {})['team_id'] = store.get_game(game_id).home_team_id
            _t = scope.update({'team_id': …})
            _t = [scope.__setitem__('team_id', …)]
            _t: object = scope.__setitem__('team_id', …)

        ``_audit()`` returned ``[]`` for all five, and each one compiled into
        the running server admitted ``thirdcoach`` on all SIXTY cells of the
        family matrix, tri-store, with eight of HOME's private players in
        the ``gid`` ``/lineups`` body at ``restricted=false``.

        EACH IS REFUSED, EACH REFUSAL NAMES ``scope``, AND EACH IS REFUSED
        ONLY BY :func:`_refuse_a_mutated_input` — with that one rule removed
        the audit is green again for every one of them, so this is not
        measuring a rule that happened to answer first. The placement is
        what makes that true and is the whole reason these five are spelled
        ABOVE the normalisation: below it, round 14's
        :func:`_refuse_a_second_binding` answers instead, because
        :func:`_assigned_names` reads a subscript target as binding its
        base.

        RE-AIMED IN ROUND 20, AT ``store`` (#427). Those five mutate the
        MAPPING, and the mapping no longer crosses this boundary: condition
        10 answers all five now, so leaving them here would have left this
        test measuring nothing — which
        :meth:`_assert_only_this_rule_catches` detects, and did. They are
        still in the battery and still refused by name; the test that owns
        them is :meth:`test_a_raw_scope_use_after_the_projection_is_refused`.
        See :data:`MUTATION_SPELLINGS` for why ``store`` is a real seam and
        not a contrivance."""
        for spelling in self.MUTATION_SPELLINGS:
            with self.subTest(spelling=spelling):
                source = self._mutation_source(spelling)
                self._assert_only_this_rule_catches(
                    source, "_refuse_a_mutated_input",
                    lambda fn, where, what: None,
                    "position this derivation does not read")
                self.assertTrue(
                    any("'store'" in f for f in self._audit(source)),
                    "the refusal does not NAME the mutated input: "
                    + str(self._audit(source)))

    # -- ROUND 20: THE THREE RULES THE REFACTOR MADE DECIDABLE -------------

    def test_a_projected_id_cannot_be_assigned_twice(self):
        """SINGLE ASSIGNMENT — the first of the three the owner's ruling
        names, and THE ONE THAT NEEDED NO NEW RULE (#427 round 20).

        The requirement is real and is asserted here. What is NOT here is a
        rule of its own, and that is a finding rather than an omission: one
        was written and then removed, because it had no falsifier. Every way
        a projected id can be written twice is already answered, and
        answered FIRST, by a rule that predates this round — which
        :meth:`_assert_only_this_rule_catches` would have reported as
        vacuity if the rule had been kept.

        BOTH SHAPES ARE DRIVEN AND THE ANSWERING RULE IS NAMED PER SHAPE,
        the same way condition 5's tension is measured rather than asserted.
        "Refused" alone would let this quietly stop being enforced while the
        test went on passing.

        THE ONE SHAPE THAT SLIPS BETWEEN THEM IS DISCLOSED. A nested block
        binding the name BEFORE the projection leaves :func:`_poison`'s
        ``UNRESOLVABLE`` marker, which :func:`_refuse_a_second_binding`
        deliberately allows writing over — so that pair of writes is
        accepted. It is not a disclosure: the projection is the write that
        wins, and the nested one is dead. Recorded here rather than left for
        the next review to find."""
        for spelling, answered_by in (
                # Both writes in the carrier's own body.
                ("a_projected_id_assigned_twice", "RE-ASSIGNS"),
                # …and separated by a role branch, which is the shape
                # `_refuse_a_second_binding` would NOT see, because the walk
                # copies its bindings per body.
                ("a_projection_taken_below_a_role_branch",
                 "AFTER its first role test")):
            with self.subTest(spelling=spelling):
                failures = self._audit(self._mutation_source(spelling))
                self.assertTrue(
                    any(answered_by in f for f in failures),
                    f"{spelling} writes a projected id twice and no rule "
                    f"answered it with {answered_by!r}: {failures}")
                self.assertTrue(
                    any("scoped_team_id" in f for f in failures),
                    f"{spelling}'s refusal does not NAME the projected id: "
                    f"{failures}")
        # AND THE DISCLOSED GAP, driven rather than described: the nested
        # write comes FIRST, the projection writes over the poison, and the
        # audit is GREEN. If a future change makes this shape reachable as a
        # disclosure, this is the assertion that has to move.
        self.assertEqual(
            [], self._audit(self._injected(prelude=(
                '    scoped_team_id = scope.get("team_id")\n',
                "    if True:\n"
                "        scoped_team_id = 'team_1'\n"
                '    scoped_team_id = scope.get("team_id")\n'))),
            "the disclosed single-assignment gap has closed or moved; "
            "re-measure it rather than leaving this note standing")

    def test_a_resolver_slot_no_projection_fills_is_refused(self):
        """CONDITION 8 — UNCHANGED PROPAGATION, the second of the three.

        THE DIRECTION THAT MATTERS. The resolver grows a ``scoped_*``
        parameter the carrier projects nothing for, and the carrier fills it
        with a local that happens to carry the name. Conditions 1 and 2 are
        both satisfied — it is a bare ``Name`` and it IS the resolver's own
        parameter name for that slot — and without this rule a new
        scope-backed grant would arrive with nothing watching it.

        AND THE OTHER HALF OF THE CONVENTION, which :func:`_projections`
        owns: a projection NAMED for one key and READING another fills the
        right-looking slot with somebody else's identifier. That one is
        asserted here too, because the two halves are only worth anything
        together."""
        source = self._mutation_source("a_resolver_slot_no_projection_fills")
        self._assert_only_this_rule_catches(
            source, "_refuse_an_unprojected_slot",
            lambda call, carrier, resolver, projected, where: None,
            "is not the projection of the scope key")
        self.assertTrue(
            any("'scoped_venue_id'" in f for f in self._audit(source)),
            "the refusal does not NAME the unprojected slot: "
            + str(self._audit(source)))
        crossed = self._mutation_source("a_projection_named_for_another_key")
        self.assertTrue(
            any("binds 'scoped_team_id' to the scope key 'player_id'" in f
                for f in self._audit(crossed)),
            "a projection named for one key and reading another is not "
            "refused by name: " + str(self._audit(crossed)))

    def test_a_raw_scope_use_after_the_projection_is_refused(self):
        """CONDITION 9 — NO LATER RAW ``scope``, the third of the three,
        and the one that closes THE OWNER'S THREE NAMED VARIANTS together
        (#427 round 20).

        THE REPRODUCTION THIS ROUND EXISTS FOR. Two of the three were
        measured LIVE at ``f9b094e``, each hoisted above the COACH/PLAYER
        branch with the OFFICIAL branch's own read replaced by ``pass``::

            official_id = (scope.update({'team_id': game.home_team_id})
                           or scope.get("official_id"))          # RELOCATION

            _alias = scope                                       # ALIAS
            official_id = (_alias.update({'team_id': game.home_team_id})
                           or _alias.get("official_id"))

        ``_audit()`` returned ``[]`` for both. Driven over real authenticated
        HTTP, both gave BYTE-IDENTICAL results: TWENTY of the FIFTY derived
        unentitled cells answered 200 instead of 403, THIRTEEN private side
        identities appeared across those bodies, and the ``gid`` ``/lineups``
        body carried EIGHT of HOME's private players at
        ``restricted=false`` — on Memory, on SQLite and on real PostgreSQL
        alike. The behavioural oracles in this file went red on both; the
        static audit saw neither.

        THE THIRD IS DIFFERENT AND IS SAID SO. The LIVE ``game`` BINDING —
        ``game = (scope.update({…}) or store.get_game(game_id))`` — was NOT
        green at ``f9b094e``: condition 3 refused it there, for the string
        literal in the ``game`` slot's resolved value. That is a rule about
        ARGUMENT PROVENANCE which happened to answer a mutation, and this
        round changes exactly that machinery, so the variant is pinned here
        to prove the change did not quietly cost the coverage.

        WHAT MAKES ONE RULE ENOUGH FOR ALL THREE. Each is a question about
        what a CALL does to a shared object, and none of them is answerable
        from a source tree — which is why five rounds of rules over the
        mapping were each defeated one spelling later. Asking instead WHERE
        THE NAME MAY APPEAR is answerable, and it is sound only because the
        mapping no longer has to survive the trip: what crosses into the
        resolver is three immutable scalars, and an alias of a string IS the
        string."""
        for spelling in self.RAW_SCOPE_ONLY_SPELLINGS:
            with self.subTest(only=spelling):
                source = self._mutation_source(spelling)
                self._assert_only_this_rule_catches(
                    source, "_refuse_a_raw_scope_use",
                    lambda carrier, base, projected, where: None,
                    "outside its projection")
                self.assertTrue(
                    any("'scope'" in f for f in self._audit(source)),
                    "the refusal does not NAME the raw mapping: "
                    + str(self._audit(source)))
        # …and THE OVERLAP, measured rather than glossed. Each of these is
        # refused by this rule TOO, and by an EARLIER one FIRST — named per
        # spelling, so a rule that stopped answering would be reported here
        # rather than absorbed.
        for spelling, answers_first in self.RAW_SCOPE_ALSO_SPELLINGS:
            with self.subTest(also=spelling):
                source = self._mutation_source(spelling)
                self.assertTrue(
                    any("'scope'" in f for f in self._audit(source)),
                    f"{spelling} is not refused naming the raw mapping: "
                    + str(self._audit(source)))
                with self._without(
                        "_refuse_a_raw_scope_use",
                        lambda carrier, base, projected, where: None):
                    without_ten = self._audit(source)
                self.assertTrue(
                    without_ten,
                    f"{spelling} is answered ONLY by this rule, so it "
                    f"belongs in RAW_SCOPE_ONLY_SPELLINGS")
                for rule, stub in (
                        ("_refuse_a_mutated_input",
                         lambda fn, where, what: None),
                        ("_refuse_an_unvouched_argument",
                         lambda call, carrier, resolver, bindings, where,
                         projected=frozenset(): None)):
                    if rule != answers_first:
                        continue
                    with self._without(rule, stub):
                        remaining = self._audit(source)
                    self.assertTrue(
                        remaining,
                        f"{spelling} is no longer answered by {rule}, which "
                        f"this pin says answers it first")

    def test_a_gate_signature_no_caller_uses_is_refused(self):
        """THE ESCAPE THIS ROUND'S OWN HUNT FOUND, against the rules this
        round itself wrote (#427 round 20).

        :func:`_projections` names no parameter — the raw mapping is
        whichever carrier parameter the projections read — and that is what
        keeps it derived rather than remembered. On its own it is
        exploitable: give the carrier a FIFTH REQUIRED PARAMETER and project
        out of THAT, and every projection is well-formed, every key is one
        the product accepts, every name matches its key, and the SESSION'S
        OWN ``scope`` is never mentioned, so nothing polices it. Measured
        against this round's first draft: ``_audit() == []``.

        ITS LIVENESS IS NOT CLAIMED. The product's two call sites hand this
        function four positional arguments, so the fifth parameter makes
        every private-game read raise ``TypeError``. It is a COVERAGE escape
        — the audit would have blessed a gate deciding from a mapping nobody
        supplied — and it is refused for exactly that reason, the same
        standing :func:`_refuse_a_binding_the_fold_would_drop` carries.

        REFUSED, NAMING BOTH SIDES OF THE DISAGREEMENT, and refused ONLY by
        this rule."""
        source = self._mutation_source(
            "the_projection_taken_from_a_parameter_nobody_passes")
        self._assert_only_this_rule_catches(
            source, "_refuse_a_carrier_the_product_does_not_call",
            lambda carrier, where: None,
            "the product calls it with")
        self.assertTrue(
            any("'override'" in f for f in self._audit(source)),
            "the refusal does not NAME the parameter no caller supplies: "
            + str(self._audit(source)))
        # AND THE PREMISE: the rule really is reading the product's own call
        # sites, so it cannot be satisfied by a gate nothing calls.
        tree = ast.parse(self._gate_source())
        carrier = _gate_function(tree, GATE_CARRIER, "gate")
        self.assertEqual(
            ["role", "scope", "game_id", "store"],
            _positional_parameters(carrier, "gate"),
            "the gate's signature moved; the call sites this rule reads it "
            "against are web/scope.py and web/server.py")

    def test_the_projection_is_derived_and_not_listed(self):
        """THE DERIVATION BEHIND ALL THREE, and its own refusals.

        :func:`_projections` names nothing. The mapping parameter is
        whichever carrier parameter the projections read; the KEYS are
        checked against ``AccountService._ALLOWED_SCOPE_KEYS``, the
        product's own statement of what an account may carry; and the NAMES
        are checked against the keys. What it finds on this gate is
        MEASURED here rather than recited, so a projection added, removed or
        renamed fails by name.

        AND THE TWO STRUCTURAL PRECONDITIONS, each with its own spelling: a
        projection taken BELOW a role branch is a read only some callers
        reach, and a projection of a key no account can hold is a value the
        session never supplied."""
        tree = ast.parse(self._gate_source())
        carrier = _gate_function(tree, GATE_CARRIER, "gate")
        base, projected = _projections(carrier, "gate")
        self.assertEqual("scope", base)
        self.assertEqual(
            {"scoped_team_id": "team_id",
             "scoped_player_id": "player_id",
             "scoped_official_id": "official_id"},
            {name: key for name, (key, _stmt) in projected.items()})
        self.assertLessEqual(
            set(key for key, _s in projected.values()), PRODUCT_SCOPE_KEYS,
            "the gate projects a key AccountService._ALLOWED_SCOPE_KEYS "
            "does not accept")
        for spelling, expected in (
                ("a_projection_taken_below_a_role_branch",
                 "AFTER its first role test"),
                ("a_projection_of_a_key_no_account_carries",
                 "which is not one of")):
            with self.subTest(spelling=spelling):
                failures = self._audit(self._mutation_source(spelling))
                self.assertTrue(
                    any(expected in f for f in failures),
                    f"{spelling} was not refused naming {expected!r}: "
                    f"{failures}")

    def test_the_real_gate_may_still_mention_its_own_inputs(self):
        """THE OTHER DIRECTION OF CLASS A, so the rule separates POSITIONS
        and not SPELLINGS.

        The refusal above is worth nothing if it is really "anything but the
        gate's own text". The real carrier names its own four inputs
        TWENTY-ONE times and only TWO of those are the delegating call's
        arguments — ``role`` nine times, ``scope`` five, ``store`` four and
        ``game_id`` three, across four branch tests, four returned records,
        THE THREE PROJECTIONS and their normalisation,
        ``game = store.get_game(game_id)`` and ``store.assignments_for_game(
        game_id)`` — and every one of the other NINETEEN parses. Both counts
        are MEASURED off the gate here rather than recited, so a gate that
        stops exercising these positions cannot leave the claim standing.

        RE-MEASURED IN ROUND 20 (#427), and both numbers moved for reasons
        the refactor requires: ``scope`` gained a mention (three projections
        and one normalisation, where before it was one normalisation and one
        ``.get``), and the delegating call now hands the resolver TWO of the
        carrier's own inputs rather than three, because the ``scope`` slot
        it used to fill is gone. Then the READ positions are asserted one at
        a time, each injected into a copy of the gate and each required to
        be GREEN, beside two shapes in the same place that are required to
        be REFUSED."""
        carrier = _gate_function(
            ast.parse(self._gate_source()), GATE_CARRIER, "gate")
        inputs = set(_positional_parameters(carrier, "gate"))
        mentions = [n for n in ast.walk(carrier) if isinstance(n, ast.Name)
                    and n.id in inputs]
        handed = _vouched_arguments(carrier)
        self.assertEqual(
            (21, 2), (len(mentions),
                      sum(1 for n in mentions if id(n) in handed)),
            "the carrier's mentions of its own inputs moved, so the "
            "seventeen-outside-the-call claim above needs remeasuring")
        tail = '    scoped_team_id = scope.get("team_id")'
        operator = "    if role in (Role.LEAGUE_ADMIN, Role.ARENA_MANAGER):"
        for anchor, spelling, green in (
                # NOT READ: a store read nothing consumes.
                (self.NORMALISED_SCOPE,
                 "    _keep = store.get_game(game_id)\n"
                 "    scope = scope or {}\n" + tail, False),
                # READ: the same normalisation the gate already has,
                # respelled with a different empty default.
                (self.NORMALISED_SCOPE,
                 "    scope = scope or []\n" + tail, True),
                # READ: an input in a `return` value, which is classified
                # keyword by keyword. ANCHORED BELOW THE PROJECTION, because
                # the projection must precede every role test and this
                # spelling adds one (#427 round 20).
                (operator,
                 "    if role is None:\n"
                 "        return PrivateGameRead(role=role, game=None,\n"
                 "                               own_team=None, "
                 "admitted=False)\n" + operator, True),
                # NOT READ: a store read discarded into a dead binding.
                (self.NORMALISED_SCOPE,
                 "    _t = store.get_game(game_id)\n"
                 "    scope = scope or {}\n" + tail, False)):
            with self.subTest(spelling=spelling.splitlines()[0].strip()):
                failures = self._audit(self._injected(prelude=(
                    anchor, spelling)))
                if green:
                    self.assertEqual(
                        [], failures,
                        f"{spelling.splitlines()[0].strip()!r} mentions an "
                        f"input in a position this derivation READS and the "
                        f"audit refuses it, so the rule separates spellings "
                        f"rather than positions")
                else:
                    self.assertTrue(
                        any("position this derivation does not read" in f
                            for f in failures),
                        f"{spelling.splitlines()[0].strip()!r} mentions an "
                        f"input where nothing reads it and no rule answered: "
                        f"{failures}")

    def test_a_mutation_in_a_test_that_constrains_no_role_is_refused(self):
        """THE EIGHTH SPELLING, found by hunting THIS round's own rule.

        The first draft of :func:`_refuse_a_mutated_input` treated a whole
        ``if`` test as a position the derivation reads, on the ground that
        :func:`_resolve_roles` reads one. It reads one only AS A STATEMENT
        ABOUT THE ROLE PARAMETER — the left of a comparison, the members of a
        container — so everything else inside a test is evaluated and read by
        nobody::

            if scope.update({'team_id': store.get_game(game_id).…}):
                pass

        Measured against that draft, in the carrier and in the resolver
        alike: ``_audit() == []``.

        THE SEAM IS :func:`_read_expressions`, which is what draws the line,
        and the falsifier is that FIRST DRAFT: restoring it — every test
        counted as read, entire — must make both spellings green again."""
        whole_tests = _read_expressions

        def first_draft(fn):
            returns, tests = whole_tests(fn)
            return returns + tests, []

        for spelling in ("a_store_mutation_in_a_test_that_constrains_no"
                         "_role",
                         "a_mutation_in_a_resolver_test_that_constrains_no"
                         "_role"):
            with self.subTest(spelling=spelling):
                self._assert_only_this_rule_catches(
                    self._mutation_source(spelling), "_read_expressions",
                    first_draft, "position this derivation does not read")

    def test_the_deciding_fields_are_the_ones_the_classification_pins(self):
        """:data:`DECIDING_FIELDS` IS DERIVED FROM
        :func:`admission_branches`' OWN SOURCE, not remembered.

        Liveness is seeded from the record fields whose EXPRESSION the
        classification reads, and a field that quietly stopped being pinned
        — or a new one that started — would leave that set describing the
        code as it was. So the set is read back off the function: every
        ``NAME = _resolved(keywords['FIELD'], …)`` binding is mapped to its
        field, and the fields whose ``NAME`` is then handed to
        :func:`_inlined` must be exactly :data:`DECIDING_FIELDS`.

        This is the idiom
        ``test_every_binding_form_the_poison_rule_reads_is_this_grammar``
        already uses on :func:`_assigned_names`, and for the same reason: a
        list nobody re-derives is a list that describes an older function."""
        fn = ast.parse(
            textwrap.dedent(inspect.getsource(admission_branches))).body[0]
        field_of = {}
        for node in ast.walk(fn):
            if not isinstance(node, ast.Assign) or len(node.targets) != 1 \
                    or not isinstance(node.targets[0], ast.Name) \
                    or not isinstance(node.value, ast.Call) \
                    or getattr(node.value.func, "id", None) != "_resolved":
                continue
            read = node.value.args[0]
            if isinstance(read, ast.Subscript) \
                    and isinstance(read.slice, ast.Constant):
                field_of[node.targets[0].id] = read.slice.value
        self.assertEqual(
            {"admitted", "game", "own_team"}, set(field_of.values()),
            f"the fields the classification RESOLVES moved to "
            f"{sorted(set(field_of.values()))}, so what DECIDING_FIELDS is a "
            f"subset OF is no longer what this test measured")
        pinned = {field_of[arg.id]
                  for node in ast.walk(fn)
                  if isinstance(node, ast.Call)
                  and getattr(node.func, "id", None) == "_inlined"
                  for arg in node.args[:1]
                  if isinstance(arg, ast.Name) and arg.id in field_of}
        self.assertEqual(
            set(DECIDING_FIELDS), pinned,
            f"DECIDING_FIELDS says {sorted(DECIDING_FIELDS)} and "
            f"admission_branches folds {sorted(pinned)} through `_inlined`. "
            f"Liveness is seeded from the first and the pins are read off "
            f"the second, so a field in one and not the other is a mutation "
            f"this walk would call READ while nothing reads it")

    def test_a_mutation_live_through_a_collapsing_field_is_refused(self):
        """THE TENTH SPELLING, and the SECOND found by hunting this round's
        own rule rather than by reasoning about it.

        The second draft of :func:`_refuse_a_mutated_input` seeded liveness
        from a whole returned record, so::

            _t = scope.update({'team_id': store.get_game(game_id).…})
            scope = scope or {}
            if role is None:
                return PrivateGameRead(role=role, game=_t, own_team=None,
                                       admitted=False)

        passed: ``_t`` was READ — by ``game=``, which
        :func:`admission_branches` collapses to the boolean
        ``carries_game``. The branch naming ``_t`` runs for no real caller
        and the mutation above it runs for every one. MEASURED live against
        that draft: ``thirdcoach`` admitted on all SIXTY cells of the family
        matrix, tri-store, with private identities of a side in thirty-three
        of the sixty bodies.

        THE SEAM IS :data:`DECIDING_FIELDS`, and the falsifier is the draft:
        widen it back to every field the classification resolves and the
        spelling must go green again."""
        self._assert_only_this_rule_catches(
            self._mutation_source(
                "a_store_mutation_live_through_a_field_that_collapses"),
            "DECIDING_FIELDS", ("admitted", "own_team", "game", "role"),
            "position this derivation does not read")

    def test_a_binding_the_fold_would_drop_is_refused(self):
        """THE ELEVENTH SPELLING, one call DEEPER than either class the
        review named.

        Conditions 6 and 7 are asked of the two functions this file NAMES.
        A FOLLOWED CALLEE is neither, and the fold is what reaches it:
        :func:`_bound_forward` pastes a binding's value into every later read
        of its name, so a binding nothing reads afterwards VANISHES and the
        pin reads exactly as it would have without the statement. Injected
        into ``_player_team_for_game``, the helper the PLAYER authority is
        folded through::

            _t = scope.update(
                {'player_id':
                 store.players_for_team(game.home_team_id)[0].id})

        MEASURED green at ``8a6ed27`` and green again against round 19's own
        conditions 6 and 7, with the PLAYER authority pin unmoved by a
        single character. ITS LIVENESS IS NOT CLAIMED: driven tri-store with
        that expression compiled in, both player principals were answered
        ``restricted=true, players=0`` (403 on Memory), because this
        fixture's players are MOVERS and the forged ``player_id`` names
        somebody whose membership is elsewhere. A COVERAGE escape, refused
        because the derivation could not see the statement — not because a
        disclosure was measured behind it.

        THE SEAM IS :func:`_refuse_a_binding_the_fold_would_drop`, and with
        it removed the audit must be green again — the same both-directions
        bar every other refusal in this class is held to. THE REAL GATE
        STILL PARSES: every binding in every function this fold follows is
        read afterwards."""
        source = self._mutation_source(
            "a_binding_the_fold_would_drop_in_the_player_helper")
        self._assert_only_this_rule_catches(
            source, "_refuse_a_binding_the_fold_would_drop",
            lambda stmt, name, rest, fn, where: None,
            "never reads")
        self.assertTrue(
            any("'_t'" in f for f in self._audit(source)),
            "the refusal does not NAME the dropped binding: "
            + str(self._audit(source)))

    def test_a_resolver_that_rebuilds_its_own_parameter_is_refused(self):
        """BLOCKER 2, CLASS B — the review's own expression, one function up.

        Conditions 1-5 vouch what the CARRIER hands the RESOLVER. They say
        nothing about what the resolver then does to its own parameters, and
        the resolver's pinned answer for COACH is the TEXT
        ``scope.get('team_id')`` — which :func:`_unfold` leaves alone,
        because it unfolds a bare ``Name`` and that return is a call. So a
        rebinding one line above it moves NOTHING any pin reads. MEASURED at
        ``8a6ed27``, with the carrier untouched::

            scope = {'team_id': game.home_team_id}
            scope = scope.fromkeys(scope, game.home_team_id)

        the second being the review's own expression relocated into
        ``game_scoped_own_team_id``. Both gave ``_audit() == []`` and both,
        compiled into the running server, admitted ``thirdcoach`` on all
        sixty cells of the family matrix, tri-store.

        THREE CLAIMS, and the third is the tension held open rather than
        resolved by weakening. Each spelling is REFUSED and the refusal NAMES
        the rebuilt parameter; each is refused ONLY by
        :func:`_refuse_a_rebound_resolver_parameter`; and THE REAL RESOLVER
        STILL PARSES — it opens with ``scope = scope or {}``, which is a
        rebinding too, so the line drawn is between IDENTITY-PRESERVING
        NORMALISATION and VALUE REBUILDING, and it is drawn by exactly the
        allow-list condition 5 already uses
        (:func:`_is_the_carrier_input_itself`), asked of the binding instead
        of the argument.

        RE-AIMED IN ROUND 20 AND SHARPER FOR IT (#427). The resolver has no
        ``scope`` parameter any more, so neither spelling above is even
        expressible — but the SEAM is not the mapping's, it is the
        resolver's OWN SLOTS, and the projected ids are now what fills them.
        The COACH pin is the bare name ``scoped_team_id``, which
        :func:`_unfold` leaves alone for the same reason it left the call
        alone, so a rebinding one line above it still moves nothing any pin
        reads — and it is a strictly sharper target than the mapping was,
        because the projection is the whole of what this round's answer to
        blocker 2 rests on."""
        for spelling in ("the_resolver_rebinds_a_projected_id_to_a_literal",
                         "the_resolver_rebuilds_a_projected_id_from_its_own"
                         "_inputs"):
            with self.subTest(spelling=spelling):
                source = self._mutation_source(spelling)
                self._assert_only_this_rule_catches(
                    source, "_refuse_a_rebound_resolver_parameter",
                    lambda resolver, where: None,
                    "REBUILDS its own")
                self.assertTrue(
                    any("'scoped_player_id'" in f
                        for f in self._audit(source)),
                    "the refusal does not NAME the rebuilt parameter: "
                    + str(self._audit(source)))
        # THE TENSION, MEASURED IN BOTH DIRECTIONS. `None` means THIS RULE
        # does not answer — an IDENTITY-PRESERVING normalisation of a slot,
        # which a blanket refusal of every rebinding would refuse, spelled
        # three ways.
        #
        # AND WHAT "DOES NOT ANSWER" MEANS HERE IS NARROWER THAN IT WAS, for
        # a reason the refactor causes and this round will not paper over.
        # Before, the resolver's normalisation was `scope = scope or {}` and
        # the COACH pin was the CALL `scope.get('team_id')`, which `_unfold`
        # leaves alone — so an identity-preserving rebinding was invisible to
        # every pin and the audit was GREEN. Now the pin IS the bare name,
        # so `_unfold` follows it and even an identity-preserving rebinding
        # MOVES THE PIN. That is a strictly stronger position, not a weaker
        # one, and the assertion is written for what is actually true: this
        # rule stays silent, and the pin is what speaks.
        for spelling, answered_by in (
                ("scoped_team_id = scoped_team_id or {}\n", None),
                ("scoped_team_id = scoped_team_id or ()\n", None),
                ("scoped_team_id = scoped_team_id or {} or []\n", None),
                ("scoped_team_id = scoped_team_id.strip()\n",
                 "REBUILDS its own"),
                ("scoped_team_id = str(scoped_team_id)\n",
                 "REBUILDS its own"),
                ("scoped_team_id = game.home_team_id\n", "REBUILDS its own"),
                ("scoped_team_id = scoped_team_id if scoped_team_id "
                 "else scoped_team_id\n", "REBUILDS its own")):
            with self.subTest(resolver_spelling=spelling.strip()):
                failures = self._audit(self._injected(prelude=(
                    self.RESOLVER_FIRST_TEST,
                    f"    {spelling}{self.RESOLVER_FIRST_TEST}")))
                if answered_by is None:
                    self.assertFalse(
                        [f for f in failures if "REBUILDS its own" in f],
                        f"{spelling.strip()!r} hands on the value the "
                        f"resolver RECEIVED and THIS RULE refuses it, so it "
                        f"separates SPELLINGS rather than separating "
                        f"normalisation from rebuilding")
                    # …and the pin is what does speak, which is the stronger
                    # position the refactor bought. Asserted rather than
                    # left implied, so a pin that stopped following the
                    # projected name would be reported here.
                    self.assertTrue(
                        any("is now resolved by" in f for f in failures),
                        f"{spelling.strip()!r} rebinds the slot the COACH "
                        f"pin now names and the pin did not move: "
                        f"{failures}")
                else:
                    self.assertTrue(
                        any(answered_by in f for f in failures),
                        f"{spelling.strip()!r} rebuilds the resolver's own "
                        f"parameter and no rule answered it with "
                        f"{answered_by!r}: {failures}")

    def test_a_subject_the_resolver_could_have_computed_is_refused(self):
        """THE FOURTH CONDITION, and the one this round's own HUNT found.

        The three shape/identity/provenance conditions were written first,
        and then hunted against. This got through all of them::

            game = store.all_games().pop()

        The call site still reads ``game``; ``game`` is a LOCAL, so nothing
        requires it to mention a parameter of its own name; and its only leaf
        IS a parameter of the gate. MEASURED at ``d3f5f1e`` with the rule
        absent: ``_audit() == []``, and with the gate compiled into the
        running server and a third game seeded whose sides are ``home`` and
        ``third``, ``thirdcoach`` — a Coach of a team in NEITHER side of
        ``game_1`` — received 200 on ``GET /api/games/game_1/lineups`` and
        ``/board``, which the family answers 403 for that caller. The rows
        themselves were still redacted, because the projection layer resolves
        the side again per leaf; that is defence in depth and not this axis's
        answer, and the admission boundary was breached either way.

        THE CONDITION IS AN INFORMATION STATEMENT, not another shape: the
        resolver's ``game`` slot exists to carry the SUBJECT of the request,
        and a local computed out of nothing but arguments the resolver is
        already being handed carries no subject at all.
        ``store.get_game(game_id)`` carries ``game_id``, which fills no other
        slot."""
        self._assert_only_this_rule_catches(
            self._injected(**{
                k: v for k, v
                in self._spellings()["the_subject_the_resolver_already_has"]
                .items() if k != "names"}),
            "_refuse_an_unvouched_argument",
            lambda call, carrier, resolver, bindings, where,
            projected=frozenset(): None,
            "carries no subject at all")

    def test_the_player_authority_is_what_the_helper_decides_by(self):
        """d14 — THE SAME DEFECT ONE CALL DEEPER, and round 15's own
        disclosed gap.

        The PLAYER authority was pinned as the TEXT
        ``_player_team_for_game(scope, game, store)``, and that function's
        BODY was derived by nothing at all. MEASURED LIVE at ``d3f5f1e``
        with its ``return`` changed to ``game.home_team_id``: ``awayplayer``
        received ``home[team_id=team_1 restricted=False n=8]`` on
        ``/lineups`` and ``roster-status team_id=team_1``, against a baseline
        of ``away[n=5]`` — and so did ``homeplayer``, whose membership is the
        other side — with ``_audit()`` returning ``[]``.

        ROUND 15 DISCLOSED THIS GAP AND ITS REASON: the helper "decides
        through a local binding behind an early return, and ``_substituted``
        refuses that — inlining it would refuse the real gate". The answer
        taken here is the FIRST of the three the owner offered, EXTEND THE
        FOLD TO READ THAT SHAPE, on the ground that a single assignment in
        straight-line code is a `let` and pasting its value where the name is
        read is what a `let` means rather than a model of one. Two things
        had to be true and both are named refusals when they are not:
        :func:`_decided_by` folds the assignment, and :func:`_substituted`
        keeps ``RosterService`` — a name bound at module level BY A
        DEFINITION, in the same file as the call site, where it means the
        same object.

        SO THE PIN NOW CARRIES THE TWO THINGS THE FUNCTION NAME COULD NOT:
        the #270 fail-closed guard and the membership spine. Both are
        required to move it, and both are checked below.

        THE FALSIFIER IS THE FOLD ITSELF: neuter ``_inlined``, re-pin PLAYER
        (and OFFICIAL, whose pin the same function carries) on what then
        derives, and the audit goes silent on the identical mutation."""
        source = self._injected(**{
            k: v for k, v
            in self._spellings()["the_player_helper_answers_home"].items()
            if k != "names"})
        failures = self._audit(source)
        self.assertTrue(
            any("PLAYER" in f and "team_for_game" in f for f in failures),
            f"the helper the PLAYER authority rests on stopped answering "
            f"from the membership spine and the audit did not report it: "
            f"{failures}")
        # THE OTHER HALF OF WHAT THE FOLD BOUGHT: the #270 guard is IN the
        # pin now, so deleting it moves the pin too — which a pin on the
        # helper's NAME could never have seen.
        guard = self._injected(prelude=(
            "    player = store.get_player(scoped_player_id)\n"
            "    if player is None or not player.is_active:\n"
            "        return None\n",
            "    player = store.get_player(scoped_player_id)\n"))
        self.assertTrue(
            any("PLAYER" in f and "is_active" in f
                for f in self._audit(guard)),
            "deleting the deactivated-player guard from the helper did not "
            "move the PLAYER pin")
        # RE-PINNED ON WHAT THE MUTATION DECIDES: silent, so the pin bites.
        moved = next(b for b in admission_branches(source=source)["PLAYER"]
                     if b.needs_authority)
        repinned = dict(ADMISSION_AUTHORITIES)
        repinned["PLAYER"] = dataclasses.replace(
            ADMISSION_AUTHORITIES["PLAYER"], authority=moved.authority,
            admits=moved.admits_source, side=moved.side_source)
        with mock.patch.dict(ADMISSION_AUTHORITIES, repinned, clear=True):
            self.assertEqual(
                [], self._audit(source),
                "the mutation is still reported after the PLAYER pin is "
                "moved onto it, so this test is not measuring the pin")
        # AND WITHOUT THE FOLD — round 15's derivation exactly — the pin is
        # the helper's NAME, the name is unchanged, and d14 is invisible.
        with self._without("_inlined", lambda node, tree, where, depth=None:
                           node):
            unfollowed = {role: next(b for b in admission_branches()[role]
                                     if b.needs_authority)
                          for role in ("PLAYER", "OFFICIAL")}
            self.assertEqual(
                "_player_team_for_game(scoped_player_id, game, store)",
                unfollowed["PLAYER"].authority,
                "neutering the fold did not put the PLAYER pin back on the "
                "helper's name, so this half measures nothing")
            stale = dict(ADMISSION_AUTHORITIES)
            for role, branch in unfollowed.items():
                stale[role] = dataclasses.replace(
                    ADMISSION_AUTHORITIES[role], authority=branch.authority,
                    admits=branch.admits_source, side=branch.side_source)
            with mock.patch.dict(ADMISSION_AUTHORITIES, stale, clear=True):
                blind = self._audit(source)
        self.assertEqual(
            [], blind,
            f"a pin that stops at the helper's name was expected to be blind "
            f"to a helper that stopped answering from the membership spine, "
            f"and it was not — so this test is not measuring the fold: "
            f"{blind}")

    def test_the_carrier_has_exactly_one_module_level_definition(self):
        """THE SIXTH MODEL, and the one that needs no edit to the gate at
        all.

        ``_gate_function`` returned the FIRST module-level ``def`` of the
        carrier. Python binds the LAST. So a module carrying two definitions
        had its first one audited — parsing clean, every pin intact, every
        role accounted for — while its second one ran.

        MEASURED at ``c4a725b``: appending a second
        ``def resolve_private_game_read(...)`` that admits everybody with
        ``own_team=game.home_team_id`` left ``_audit()`` returning ``[]``
        while ``thirdcoach`` received 200 with the HOME side's private rows
        on ``/lineups``, ``/roster`` and ``/roster-status``, and a signed-in
        VIEWER was admitted to ``/lineups`` as well.

        The falsifier restores the old reading — take the first binding and
        ignore the rest — and requires the identical duplicate to go
        green."""
        # Bound now, not looked up inside the replacement: `_without` swaps
        # the module global, so a lambda that named it would call itself.
        every = _module_bindings_of
        self._assert_only_this_rule_catches(
            self._injected(**{
                k: v for k, v
                in self._spellings()["a_second_definition_of_the_carrier"]
                .items() if k != "names"}),
            "_module_bindings_of",
            lambda tree, name: every(tree, name)[:1],
            "is bound 2 times at module level")

    def _first_injected_line(self, source):
        """The 1-based line of the first statement of the injection — the
        line the anchor used to be on."""
        return self._gate_source().splitlines().index(
            self.ANCHOR) + 1

    def _assert_the_walk_ends_in_a_raising_else(self):
        """The ``if``/``elif`` chain inside :func:`_decisions`' statement
        walk terminates in an ``else`` that raises
        :class:`AdmissionExtractionError` — READ OFF ITS OWN SOURCE, because
        "no statement kind falls through" is a claim about every kind,
        including the ones no sample can name yet."""
        module = ast.parse(Path(__file__).read_text())
        decisions = next(n for n in module.body
                         if isinstance(n, ast.FunctionDef)
                         and n.name == "_decisions")
        walk = next(n for n in ast.walk(decisions)
                    if isinstance(n, ast.FunctionDef) and n.name == "walk")
        loop = next(n for n in walk.body if isinstance(n, ast.For))
        chain = next(n for n in loop.body if isinstance(n, ast.If))
        while len(chain.orelse) == 1 and isinstance(chain.orelse[0], ast.If):
            chain = chain.orelse[0]
        self.assertTrue(
            chain.orelse,
            f"the statement chain in `_decisions` ends at line "
            f"{chain.lineno} with NO `else`, so a statement kind it does not "
            f"name is silently skipped — which is exactly the defect that "
            f"let a `match` arm admit a Guardian with the audit green")
        self.assertEqual(
            1, len(chain.orelse), "the terminating `else` does more than "
            "refuse, so what it does for an unrecognised statement is no "
            "longer readable from its shape")
        refusal = chain.orelse[0]
        self.assertIsInstance(
            refusal, ast.Raise,
            f"the terminating `else` is a "
            f"{type(refusal).__name__}, not a refusal")
        self.assertEqual(
            AdmissionExtractionError.__name__,
            getattr(refusal.exc.func, "id", None),
            f"the terminating `else` raises "
            f"{ast.unparse(refusal.exc)!r}, which the audit does not turn "
            f"into a NAMED failure the way it does an "
            f"{AdmissionExtractionError.__name__}")

    def test_a_mutation_that_admits_nobody_is_not_reported(self):
        """THE CONTROL, without which the test above proves only that this
        audit is noisy.

        Widening the carrier's role tuple ALONE admits nobody new: the
        resolver still answers a Guardian ``None``, so ``own_team`` is
        ``None`` and the branch's own ``admitted`` is False. The audit must
        stay silent — and it is the same mutation whose SECOND half
        (teaching the resolver to answer that role) the injection above
        requires it to catch, so the two together show the audit is reading
        the admission and not merely the diff."""
        self.assertEqual([], self._audit(self._injected(
            replacement="    if role in (Role.COACH, Role.PLAYER, "
                        "Role.GUARDIAN):")))

    def test_the_gate_this_axis_reads_is_the_gate_the_server_calls(self):
        """THE PREMISE OF THE WHOLE SECTION. Deriving the branches from a
        module nothing calls would be a closed axis over dead code, so the
        names come from ``side_provenance``'s own constants and the objects
        are resolved live."""
        self.assertEqual(GATE_CARRIER, "resolve_private_game_read")
        self.assertEqual(GATE_RESOLVER, "game_scoped_own_team_id")
        for name in (GATE_CARRIER, GATE_RESOLVER):
            self.assertTrue(callable(getattr(game_side_scope, name)), name)
        self.assertIs(web_scope.game_scoped_own_team_id,
                      game_side_scope.game_scoped_own_team_id,
                      "web/scope.py no longer re-exports the one canonical "
                      "resolver, so the gate this axis reads and the one the "
                      "server calls may be different functions")
        self.assertEqual(
            side_provenance.TRUSTED_RESOLVER_MODULE,
            "services/game_side_scope.py",
            "the scanner and this axis disagree about where the gate lives")

class TheMethodAxisIsADisclosedLimitWithLiveNumbers(unittest.TestCase):
    """The GET-only limit, re-measured rather than remembered."""

    #: The registry's whole method vocabulary today. A THIRD verb means the
    #: `method == "GET"` filter is excluding something nobody has classified.
    METHODS = ("GET", "POST")

    def _routes(self):
        return [spec for spec in route_registry.REGISTRY
                if spec.kind == "route"]

    def test_a_new_http_verb_fails_this_test(self):
        found = sorted({spec.method for spec in self._routes()})
        self.assertEqual(
            list(self.METHODS), found,
            f"the registry's method vocabulary is {found}, not "
            f"{list(self.METHODS)}. `_authenticated_get_specs` filters "
            f"`method == 'GET'`, so a new verb is outside this sweep AND "
            f"outside its stated limit — decide which, do not let the "
            f"filter decide silently.")

    def test_the_disclosed_counts_are_still_the_measured_ones(self):
        """Limit 2 in the module docstring states three numbers. They are
        measured here so the docstring cannot drift from the tree — which is
        the failure this round had to correct in the RUNTIME paragraph."""
        routes = self._routes()

        def count(method, authed):
            return len([
                s for s in routes
                if s.method == method
                and ((s.auth not in ("none", route_registry.UNCLASSIFIED))
                     is authed)])

        self.assertEqual(50, count("GET", True), "authenticated GET (SWEPT)")
        self.assertEqual(17, count("GET", False), "auth=none GET (unswept)")
        self.assertEqual(161, count("POST", True),
                         "authenticated POST (unswept, and NOT reported by "
                         "_assert_inventory_is_closed)")
        self.assertEqual(
            0, len([s for s in routes
                    if s.auth == route_registry.UNCLASSIFIED]),
            "an UNCLASSIFIED route is excluded from the sweep's inventory by "
            "`_authenticated_get_specs` and is in neither half of the "
            "disclosed limit")


# ---------------------------------------------------------------------------
# 14. THE DISCLOSED LIMITS ARE MEASURED, NOT REMEMBERED.
#
# Three of this module's four honesty corrections in round 9 were STALE
# NUMBERS in prose: a RUNTIME paragraph describing eight worlds and eight
# principals for a sweep that had sixteen and ten, and a limit that described
# six blind names without ever having asked how many of them the surface
# actually serves. The numbers this file states about ITSELF are now measured
# where they can be.
# ---------------------------------------------------------------------------
class TheDisclosedLimitsAreMeasuredNotRemembered(_SweepHarness,
                                                 unittest.TestCase):
    """Limit 1's two claims, re-run rather than recorded."""

    #: The volatile names that ACTUALLY appear on the swept surface, and
    #: where. Measured with the stripping disabled.
    VOLATILE_KEYS_THAT_APPEAR = {
        "expires_at": frozenset({"get_accounts_id_sessions"}),
        "issued_at": frozenset({"get_accounts_id_sessions"}),
    }

    @staticmethod
    def _keys_at_any_depth(node, out):
        if isinstance(node, dict):
            out |= set(node)
            for value in node.values():
                TheDisclosedLimitsAreMeasuredNotRemembered._keys_at_any_depth(
                    value, out)
        elif isinstance(node, list):
            for value in node:
                TheDisclosedLimitsAreMeasuredNotRemembered._keys_at_any_depth(
                    value, out)
        return out

    def test_the_blind_spot_is_exactly_two_names_on_one_route(self):
        """How wide the VOLATILE_KEYS blind spot really is.

        A name that starts appearing on a second route widens the spot and
        must be re-decided; a name that stops appearing anywhere is dead
        weight in a list whose own comment says adding to it is widening a
        blind spot."""
        store = InMemoryStore()
        real = _SweepHarness.VOLATILE_KEYS
        try:
            fx = self._fixture(store)
            who = self._serve(fx)
            specs, subjects = self._assert_inventory_is_closed(fx)
            _SweepHarness.VOLATILE_KEYS = ()
            raw = self._sweep(who, fx, specs, subjects)
        finally:
            _SweepHarness.VOLATILE_KEYS = real
            store.clear_all_data()
        appears = {}
        for (_p, route, _path, _hint), (_st, body) in raw.rows.items():
            for key in self._keys_at_any_depth(body, set()) & set(real):
                appears.setdefault(key, set()).add(route)
        self.assertEqual(
            {k: frozenset(v) for k, v in appears.items()},
            self.VOLATILE_KEYS_THAT_APPEAR,
            "the set of stripped names that actually reach a swept response "
            "has moved. Every one of them is a name both oracles are blind "
            "to on every route it appears on, so this is the size of a blind "
            "spot and not a detail — re-decide it, do not update the "
            "constant to match.")

    def test_nothing_in_the_list_is_observed_to_vary_at_all(self):
        """`test_the_sweep_is_stable` does not prove this list is NEEDED.

        Measured directly and in one pass rather than argued: sweep the same
        world TWICE with the stripping switched OFF entirely. If any of the
        six names carried a time-varying value, the two raw sweeps would
        differ — and they do not. So on this fixture, in this window, the
        list is defensive rather than load-bearing, and the stability the
        sibling test observes is a property of the responses and not of the
        stripping.

        If this ever fails, a member of the list has become load-bearing.
        That is worth knowing rather than absorbing: it means the sweep IS
        relying on the strip, and the blind spot is being paid for."""
        store = InMemoryStore()
        real = _SweepHarness.VOLATILE_KEYS
        try:
            fx = self._fixture(store)
            who = self._serve(fx)
            specs, subjects = self._assert_inventory_is_closed(fx)
            _SweepHarness.VOLATILE_KEYS = ()
            first = self._sweep(who, fx, specs, subjects)
            second = self._sweep(who, fx, specs, subjects)
        finally:
            _SweepHarness.VOLATILE_KEYS = real
            store.clear_all_data()
        self.assertEqual(
            [], first.diff(second),
            "with VOLATILE_KEYS emptied, two sweeps of the SAME world "
            "disagree — so one of the stripped names really does vary, the "
            "limit-1 paragraph's measurement is stale, and the blind spot is "
            "now being paid for rather than merely declared: "
            + str(first.diff(second)[:6]))

    #: The NUMERIC-identity blind spot, MEASURED on the base world (#427
    #: round 10). ``{route name: jersey-bearing nodes it serves}`` — see
    #: :meth:`_SweepHarness._identity_tokens`, whose paragraph these numbers
    #: are. The paragraph they replace stated a count this fixture never
    #: produces, on a denominator that was a different unit, over a route
    #: list that was short by one; it stood for a round because nothing
    #: re-ran it.
    JERSEY_NODES_BY_ROUTE = {
        "get_games_id_lineups": 424,
        "get_games_id_board": 316,
        "get_players": 41,
        "get_games_id_roster": 8,
    }

    #: Requests of the base world's 2,560 that carry at least one such node.
    JERSEY_BEARING_REQUESTS = 112

    @staticmethod
    def _jersey_nodes(node, out):
        """Every dict node carrying a non-null ``jersey_number``, at any
        depth — the same depth-independent walk the volatile-key measurement
        above uses, asking a different question of the same bodies."""
        if isinstance(node, dict):
            if node.get("jersey_number") not in (None, ""):
                out.append(node)
            for value in node.values():
                TheDisclosedLimitsAreMeasuredNotRemembered._jersey_nodes(
                    value, out)
        elif isinstance(node, list):
            for value in node:
                TheDisclosedLimitsAreMeasuredNotRemembered._jersey_nodes(
                    value, out)
        return out

    def test_the_numeric_identity_blind_spot_is_still_the_measured_one(self):
        """``jersey_number`` is outside oracle 1's alphabet, and HOW MUCH of
        the surface that leaves uncovered is a measurement, not a memory.

        Three separate facts, because each fails differently: WHERE the
        jerseys are (a fifth route would widen the spot), HOW MANY there are
        (a shrinking count can make the limit read worse than it is), and
        whether every one of them still carries the player ``id`` — which is
        the whole reason the spot is bounded rather than open."""
        store = InMemoryStore()
        try:
            fx = self._fixture(store)
            who = self._serve(fx)
            specs, subjects = self._assert_inventory_is_closed(fx)
            sweep = self._sweep(who, fx, specs, subjects)
            per_route, requests, nodes, with_id = {}, 0, 0, 0
            owners = {}
            for (_p, route, _path, _h), (_st, body) in sweep.rows.items():
                found = self._jersey_nodes(body, [])
                if not found:
                    continue
                requests += 1
                nodes += len(found)
                per_route[route] = per_route.get(route, 0) + len(found)
                for row in found:
                    if row.get("id"):
                        with_id += 1
                        owners.setdefault(
                            str(row["jersey_number"]), set()).add(row["id"])
            self.assertEqual(
                self.JERSEY_NODES_BY_ROUTE, per_route,
                "the routes that serve a jersey_number, or how many each "
                "serves, moved. A route appearing here that is not in the "
                "declared map is a WIDER numeric blind spot than the "
                "`_identity_tokens` paragraph discloses; re-measure it and "
                "rewrite the paragraph rather than this line.")
            self.assertEqual(
                sum(self.JERSEY_NODES_BY_ROUTE.values()), nodes,
                "the jersey-bearing NODE count moved")
            self.assertEqual(
                self.JERSEY_BEARING_REQUESTS, requests,
                f"jersey-bearing REQUESTS moved; the paragraph states "
                f"{self.JERSEY_BEARING_REQUESTS} of {sweep.requests}")
            self.assertEqual(
                nodes, with_id,
                "a jersey_number is served on a node that carries NO player "
                "id, so the jersey is the SOLE carrier of that identity — "
                "which is exactly the payload the disclosed limit says does "
                "not exist today, and the bound on the blind spot is gone.")
            self.assertEqual(
                {}, {j: sorted(ids) for j, ids in owners.items()
                     if len(ids) > 1},
                "a served jersey_number names more than one person, so it is "
                "not the unique identity the paragraph calls it")
        finally:
            store.clear_all_data()
