"""WHERE THE SIDE CAME FROM — a fail-closed AST gate over every read of a
game's per-side private state (#205, rebuilt round 6).

WHAT THIS MODULE IS, AND WHAT IT IS NOT
=======================================
IT IS THE SUPPLEMENT, NOT THE PROOF. The PRIMARY protection for the side rule
is the behavioural sweep in
``tests/test_authenticated_side_noninterference.py``: every authenticated GET
route, eight principals, both side-perturbation directions, three backends,
diffed between two worlds. That sweep reasons about what actually comes out of
the server. This module reasons about how a value is SPELLED at a call site,
which is a strictly weaker thing to know — and the owner's 2026-08-25 ruling
inverted the two after round 5, for a reason worth stating in the module
itself:

    THE FIFTH LEAK WAS BLESSED BY AN EARLIER VERSION OF THIS FILE.
    ``GET /api/me/player-home`` derived its side from ``Player.team_id`` and
    served a transferred player the OPPONENT's private per-side state. The
    exemption class it carried, ``SUBJECT_OWN_SIDE``, had exactly one machine
    condition: "the function accepts no caller-supplied side parameter". That
    was TRUE, and it proved only that the CLIENT had not NAMED the side. It
    said nothing about whether the side the function DERIVED was the one the
    subject is entitled to. The sweep found that leak; this file had passed
    it.

WHAT IT CAN PROVE, precisely:

* that every producer call's side ARGUMENT traces, by intraprocedural
  dataflow, to the canonical trusted resolution, to a verified adjudicated
  decision, or to a declared and machine-checked exemption;
* that every function reaching TWO-SIDED game storage is enumerated and
  carries a class whose condition holds of its own body;
* that the name ``game_scoped_own_team_id`` is bound, everywhere in the
  package, to the one canonical definition and nothing else;
* that the private-game dispatch hoists one trusted side and hands THAT to
  every leaf, and that every leaf is declared.

WHAT IT CANNOT PROVE, and does not claim: **source-to-output authorization.**
It cannot tell you that the value reaching a response body is the value the
CALLER is entitled to. A function can satisfy every rule here and still
derive the wrong side from a correctly-provenanced input — which is what
``get_player_home`` did. Where a class needs a fact about entitlement rather
than about spelling, the class says so and names the behavioural test that
carries it.

THE RULE
========
    A side may ENTER the private-state read chain only from the server's
    trusted resolution or from an adjudicated decision. Every other way a
    side enters must be DECLARED here, with a machine-checked condition.
    Separately: every reach into TWO-SIDED game storage must be declared too,
    because a read with no side argument has no provenance to check.

Stated as a closure property, which is what makes it catch a route nobody
thought of: to read one side's private state you must call a **producer**
(:data:`PRODUCERS`). To call a producer you must either

* **introduce** a side — the argument's origins include something other than
  the enclosing function's own parameters — in which case the origins must
  all be TRUSTED, or the site must carry an :data:`EXEMPTIONS` entry; or
* **forward** a side — every origin is one of the enclosing function's own
  parameters — in which case the enclosing function must be a declared
  :data:`SIDE_FORWARDERS` entry, which moves the obligation to *its* callers
  and keeps the chain enumerated.

WHAT "TRUSTED" MEANS, AND WHY IT IS NOT A NAME
==============================================
Three trusted origins, each VERIFIED rather than asserted:

``call:…game_scoped_own_team_id``
    :func:`services.game_side_scope.game_scoped_own_team_id`, the server's
    resolution — and specifically THE canonical binding of it, RESOLVED
    (:class:`_ModuleContext`) rather than matched by name. Round 5 compared
    the callee's last name component to that string, so a nested ``def``, a
    method on ``ApiService``, a local lambda or an ``import x as
    game_scoped_own_team_id`` were all trusted unconditionally. Now a call is
    trusted only when it reaches the ONE module-level definition, and
    :func:`audit_trusted_binding` separately reports every other binding of
    that name in the package.

``call:…<adjudicator>``
    A declared :data:`ADJUDICATORS` entry, called as ``self.<name>()`` from
    the module that declares it. VERIFIED: the function's own body must call
    ``lineup_visibility.route_audience``.

``param:<f>.<p>``
    A parameter of a declared :data:`ADJUDICATED_READERS` entry. VERIFIED:
    ``f``'s body must pass ``p`` to ``route_audience``/``side_projections``
    as the viewer's side. :func:`audit_dispatch` closes the other half of
    that loop.

NOT FALSE-GREEN: origins are computed by real intraprocedural dataflow over
the argument expression — through ``or``/ternary alternatives, through local
aliases, through a parameter REBOUND above the call — not by matching a name.
A site is trusted only when **every** origin is trusted, so ``trusted or
game.home_team_id`` fails on the second disjunct and ``hint if flag else
trusted()`` fails on the first; an ALIAS of the trusted side PASSES, which is
the same machinery proving it is provenance and not spelling.

THE FIVE DETECTORS
==================
1. :func:`audit_side_provenance` — the side ARGUMENT of every producer call.
2. :func:`audit_home_fallback` — the ``x or <expr>.home_team_id`` shape
   ANYWHERE in the package, whether or not it reaches a producer.
3. :func:`audit_trusted_binding` — the trusted symbol is the only thing
   wearing its name.
4. :func:`audit_two_sided_store_reads` — every function that reaches
   two-sided game storage is enumerated with a class. THIS is the detector
   round 5 lacked: F1 and F3 shipped as ``def get_roster(self, game_id):
   return [... for e in self.store.roster_for_game(game_id)]``, which has NO
   side argument for detector 1 to classify. Measured against the real
   sources at 337374a and 23935a1, detector 1 alone reported nothing naming
   ``get_roster``/``get_substitutes``; detector 4 reports all three.
5. :func:`audit_dispatch` — the private-game dispatch hoists ONE trusted
   side, every leaf is declared, and every ``viewer_team_id=`` is that
   hoisted name.

THE LEGITIMATE CASES ARE TYPED CLASSIFICATIONS, NOT ACCUMULATING EXEMPTIONS
==========================================================================
Every class carries a condition checked against the code, and every class has
a dedicated test that fails if a site stops matching it
(``tests/test_side_provenance_guard.py``, ``EveryClassConditionIsEnforced``):

* :data:`EXEMPTIONS` — a site that introduces a side for a documented reason.
  ``SUBJECT_MEMBERSHIP_CONTEXT`` replaced ``SUBJECT_OWN_SIDE`` and requires
  the side to be a resolved membership context's own ``team_id``;
  ``AUTHORIZED_WRITE`` requires authorization anywhere on the path;
  ``OPERATOR_ONLY_ROUTE`` requires ``web/route_registry.py`` to record that
  route ``auth="operator_only"``.
* :data:`SIDE_FORWARDERS` — functions that hand a side on unchanged.
* :data:`TWO_SIDED_READERS` — every reach into two-sided storage.
* :data:`LIVE_MEMBERSHIP_READERS` — a DESIGN RECORD: reads that answer "who
  owes an answer" / "who could be enrolled" from LIVE membership on purpose.

:data:`LEDGER` — accepted-but-unclassified — is EMPTY, which is the strongest
state it can be in.

MONOTONIC SHRINK
================
Every declaration is checked for LIVENESS as well as for coverage
(:func:`verify_registry_liveness`): a producer that no longer exists with the
declared side parameter, an adjudicator that stopped adjudicating, a
forwarder, exemption or two-sided entry that matches nothing any more, an
AMBIGUOUS declaration resolving to two same-named functions, or a
:data:`LEDGER` entry that is no longer needed is an ERROR, not a silent pass.

EXEMPTIONS and LEDGER are keyed on ``(function, producer, origin
fingerprint)`` rather than on the pair — see :func:`origin_key`. One function
routinely calls one producer twice on two different branches, and a
pair-keyed entry would silently cover a third, new call in the same function.
A forged binding gets a DIFFERENT fingerprint from the honest one
(:data:`UNRESOLVED_BINDING`), so an exemption written for the honest site can
never cover a forgery of it.

EVIDENCE
========
``tests/test_side_provenance_guard.py`` reconstructs all FIVE defects this
blocker fixed and requires each to be reported, drives twenty-two adversarial
shapes, gives each of the nine typed classes a test that breaks its condition
on purpose, and NEUTERS each detector in turn to prove its own tests can
fail.
The measurement against the REAL historical sources (``git archive``, not
text-mutating today's tree) is in that file's
``TheGuardIsMeasuredAgainstTheRealVulnerableTrees`` and in this round's
commit message.

"""

import ast
import os
import re
from pathlib import Path

#: The package root this gate scans. Everything under it except tests and
#: caches — deliberately the WHOLE package, because the fourth leak was
#: outside the route family that the first three lived in.
PACKAGE_ROOT = Path(__file__).resolve().parent.parent

#: Directory names never scanned.
_SKIP_DIRS = ("__pycache__",)


# ---------------------------------------------------------------------------
# THE PRODUCERS — every function that answers with ONE SIDE's private
# per-game state and takes that side as an argument.
#
# `index` counts `self` as 0, so a bound call `self.f(a, b)` supplies index 1
# as its first positional. Verified against the real signatures by
# `verify_registry_liveness`: a rename, a removed parameter or a reordered
# one is an ERROR here, so this table cannot describe a world that no longer
# exists.
#
# WRITE paths are deliberately NOT producers. Minting a side when a row is
# CREATED is live-by-design under the standing ruling, and those paths carry
# their own authorization argument (`authorized_team_id`) which
# `web/scope.py` already gates. See SIDE_FORWARDERS' AUTHORIZED_WRITE class.
# ---------------------------------------------------------------------------
PRODUCERS = {
    # roster_service
    "compute_roster_status": ("team_id", 2),
    "_side_data": ("team_id", 2),
    "_slot_summaries": ("team_id", 2),
    "lineup_population": ("team_id", 2),
    "_unbound_lineup_population": ("team_id", 2),
    "list_substitute_candidates": ("team_id", 2),
    "list_addable_players": ("team_id", 2),
    "_prior_side_candidates": ("team_id", 2),
    # api/service
    "_lineup_rows": ("team_id", 2),
    "_availability_candidates": ("team_id", 2),
    "_availability_summary_of": ("team_id", 2),
    "_activity_projection": ("team_id", 3),
}

#: Where each producer is defined — checked by `verify_registry_liveness`, so
#: a producer that moves house is noticed rather than silently unresolved.
PRODUCER_MODULES = {
    "compute_roster_status": "services/roster_service.py",
    "_side_data": "services/roster_service.py",
    "_slot_summaries": "services/roster_service.py",
    "lineup_population": "services/roster_service.py",
    "_unbound_lineup_population": "services/roster_service.py",
    "list_substitute_candidates": "services/roster_service.py",
    "list_addable_players": "services/roster_service.py",
    "_prior_side_candidates": "services/roster_service.py",
    "_lineup_rows": "api/service.py",
    "_availability_candidates": "api/service.py",
    "_availability_summary_of": "api/service.py",
    "_activity_projection": "api/service.py",
}

#: The ONE trusted resolution. Matched on the callee's final name so every
#: spelling (`game_scoped_own_team_id(...)`, `scope.game_scoped_own_team_id(...)`)
#: is the same origin. Verified to exist by `verify_registry_liveness`.
TRUSTED_RESOLVER = "game_scoped_own_team_id"
TRUSTED_RESOLVER_MODULE = "services/game_side_scope.py"

#: THE CARRIER: the one function that takes the private-game family's
#: admission AND its trusted side together, against a single fetch of the
#: game (#427 round 2, blocker 1). The dispatch used to call
#: :data:`TRUSTED_RESOLVER` itself, INDEPENDENTLY of the admission gate that
#: had already resolved the same thing — and the window between those two
#: resolutions was a disclosure window: an authority lost inside it left the
#: dispatch's own answer empty, which fell through to a full HOME read. So
#: the dispatch now reads the side off ONE carrier record instead.
#:
#: The carrier is trusted ONLY because it traces back to the same one
#: function this whole gate is built on: it must live in
#: :data:`TRUSTED_RESOLVER_MODULE` and its own body must call
#: :data:`TRUSTED_RESOLVER`, which :func:`audit_trusted_binding` verifies. A
#: carrier that stopped doing that would be a second, unchecked answer to
#: "which side is this caller's" — exactly what this module exists to refuse.
TRUSTED_CARRIER = "resolve_private_game_read"
#: The attribute of a carrier record that holds the trusted side.
TRUSTED_CARRIER_SIDE_FIELD = "own_team"

#: Functions that may LAUNDER a client-supplied side: they take the hint AND
#: the trusted side, consult the audience, and return the trusted side or
#: raise. VERIFIED: each body must call `lineup_visibility.route_audience`.
ADJUDICATORS = {
    "_workflow_side": "api/service.py",
}

#: ``{function name: the parameter that carries the TRUSTED side}``. VERIFIED:
#: the function's body must pass that parameter to ``route_audience`` or
#: ``side_projections`` as the viewer's side (second positional), so
#: "this parameter is trusted" is a property of the code, not of the name.
#:
#: The OTHER half of the loop — that the dispatch really fills these with the
#: hoisted ``game_scoped_own_team_id`` result and nothing else — is proven by
#: :func:`audit_dispatch`, not assumed here.
ADJUDICATED_READERS = {
    "get_board": "team_id",
    "get_lineups": "viewer_team_id",
    "get_roster": "viewer_team_id",
    "get_roster_status": "viewer_team_id",
    "get_substitutes": "viewer_team_id",
    "get_availability_summary": "viewer_team_id",
    "get_substitute_candidates": "viewer_team_id",
    "get_addable_substitutes": "viewer_team_id",
    "_schedule_roster_status": None,   # resolves its own side per row
    "_workflow_side": "viewer_team_id",
}

#: The visibility helpers an adjudicated reader must consult.
_AUDIENCE_CALLS = ("route_audience", "side_projections", "own_side")


# ---------------------------------------------------------------------------
# FORWARDERS — functions that take a side and hand it on unchanged.
#
# Forwarding decides nothing, so it is not a defect; but the chain of
# forwarders must be CLOSED, or "a new route that never calls the resolution
# at all" hides inside it. Each entry names the CLASS that says who carries
# the obligation instead, and each class has a machine-checked condition.
# ---------------------------------------------------------------------------
#: The forwarder classes and what each one asserts.
PRODUCER_INTERNAL = "producer_internal"
#: The forwarder IS a declared producer. Its own callers are gated by this
#: same rule, so nothing new enters here.

ADJUDICATED = "adjudicated"
#: The forwarder is a declared adjudicated reader: it consults the audience
#: before forwarding, which is the decision itself.

AUTHORIZED_WRITE = "authorized_write"
#: A command whose side is checked against the caller's authorization
#: argument. CONDITION: the function must take an ``authorized_team_id``
#: parameter — a write that takes a side and NO authorization argument does
#: not get this class.

SUBJECT_MEMBERSHIP_CONTEXT = "subject_membership_context"
#: THE CLASS THAT REPLACED ``SUBJECT_OWN_SIDE`` (owner ruling, 2026-08-25).
#:
#: ``SUBJECT_OWN_SIDE``'s only machine condition was "the function accepts no
#: ``team_id``/``side``/``team_side``/``viewer_team_id`` parameter". That
#: proves the CLIENT did not NAME the side, and NOTHING about whether the
#: side the function derived is the one the subject is entitled to — which is
#: exactly how it blessed the fifth leak: ``get_player_home`` accepted no side
#: parameter and derived the OPPONENT's, from ``Player.team_id``.
#:
#: This class says something a machine can actually check about the DERIVED
#: value: the side is the ``team_id`` of a
#: :class:`~services.roster_service.GameMembershipContext` that this same
#: function resolved for the SUBJECT against THIS game — the authoritative
#: game-scoped membership, the same authority ``game_scoped_own_team_id``
#: uses for a Player. CONDITIONS, all three:
#:
#: 1. every origin of the side must be a context field
#:    (:data:`_SUBJECT_CONTEXT_ORIGINS`) — not a pointer, not a request field;
#: 2. the function's own body must resolve that context through a declared
#:    membership resolver (:data:`_MEMBERSHIP_RESOLVERS`), so the context is
#:    the server's answer rather than something handed in whole;
#: 3. the function must identify its SUBJECT by a ``player_id``/``player``
#:    parameter and accept no caller-supplied side.
#:
#: A site that derives its side from ``player.team_id`` fails condition 1,
#: which is the check the fifth leak needed and did not have.

#: Origins that ARE a resolved membership context's own side.
_SUBJECT_CONTEXT_ORIGINS = ("attr:ctx.team_id", "attr:offer_ctx.team_id")

DURABLE_ROW_SIDE = "durable_row_side"
#: The side comes from the ROW's own durable attribution — the authority the
#: standing ruling names. CONDITION: the origin must be one of
#: :data:`_DURABLE_ATTRIBUTION_ORIGINS`.

BOTH_SIDES_BY_AUDIENCE = "both_sides_by_audience"
#: The function answers for BOTH sides on purpose, because an audience that
#: is entitled to both (an unscoped operator, an assigned official's
#: submitted-lineup projection) asked. CONDITION: every call site of the
#: function must lie inside a declared adjudicated reader.

OPERATOR_ONLY_ROUTE = "operator_only_route"
#: An operator-only surface that legitimately takes no side. CONDITION: the
#: named ``web/route_registry.py`` entry must exist AND be recorded
#: ``auth="operator_only"`` — so loosening that route's auth breaks this
#: exemption instead of silently widening a private read.

OPERATOR_DEFAULT = "operator_default"
#: The unscoped-operator / in-process home default: the side falls back to
#: ``game.home_team_id`` (or is omitted so the producer's own default
#: applies) for a caller who may read either side anyway. CONDITION: the
#: enclosing function must be a declared adjudicated reader or a declared
#: producer — a function that never consults the audience cannot claim it.

LIVE_MEMBERSHIP_BY_DESIGN = "live_membership_by_design"
#: The read answers "who owes an answer" / "who could be enrolled" from LIVE
#: membership rather than "whose row is this" from durable attribution. It is
#: recorded so a reader knows why the durable-attribution rule does not apply
#: and so it cannot silently become the model for a new reader. CONDITION:
#: the function must still resolve membership (its body must call one of
#: :data:`_MEMBERSHIP_RESOLVERS`).

#: Attribute origins that ARE a durable side of record.
_DURABLE_ATTRIBUTION_ORIGINS = ("attr:entry.team_side", "attr:sub.team_id",
                                "attr:enrollment.team_id")

#: Membership resolvers a LIVE_MEMBERSHIP_BY_DESIGN reader must still use.
_MEMBERSHIP_RESOLVERS = ("resolve_membership_contexts_for_game",
                         "resolve_membership_context",
                         "_require_membership_context", "team_for_game",
                         # the shared live pool both availability and
                         # addable-substitute discovery go through
                         "_players_for_game_team")

#: ``{function: (class, note)}`` — every function that forwards a side it was
#: handed. A producer call whose side origins are ALL the enclosing
#: function's own parameters requires its enclosing function to appear here.
SIDE_FORWARDERS = {
    "_availability_summary_of": (
        PRODUCER_INTERNAL,
        "a producer itself; its own callers are gated by this same rule."),
    "_lineup_rows": (
        PRODUCER_INTERNAL,
        "a producer itself: it answers one side's lineup rows for the side it was handed, and decides nothing about "
        "which side that is. Its own callers are gated by this same rule."),
    "_slot_summaries": (
        PRODUCER_INTERNAL,
        "a producer itself: it answers one side's slot counts for the side it was handed, and decides nothing about "
        "which side that is. Its own callers are gated by this same rule."),
    "lineup_population": (
        PRODUCER_INTERNAL,
        "a producer itself: it answers one side's eligible population for the side it was handed, and decides nothing about "
        "which side that is. Its own callers are gated by this same rule."),
    "remind_unresponded": (
        AUTHORIZED_WRITE,
        "a command that notifies ONE side; the requested team is revalidated "
        "against `authorized_team_id` inside the transaction "
        "(`_require_authorized_team`) before the first push."),
    "_require_open_slot": (
        AUTHORIZED_WRITE,
        "a seat-time capacity check on the side the write already "
        "authorized."),
    "_seat_batch": (
        AUTHORIZED_WRITE,
        "the batch seating engine; its public entry points carry "
        "`authorized_team_id` and revalidate before it runs."),
    "_newest_prior_source": (
        AUTHORIZED_WRITE,
        "finds the prior game a copy-roster write reads from, for the side "
        "that write already authorized."),
    "side": (
        BOTH_SIDES_BY_AUDIENCE,
        "the per-side closure inside `get_lineups`: called once per side "
        "with the projection `side_projections` chose for it, so the "
        "audience decision is made before either call."),
}

#: The forwarder classes whose condition is checked per function.
_FORWARDER_CONDITIONS = {
    PRODUCER_INTERNAL, ADJUDICATED, AUTHORIZED_WRITE,
    SUBJECT_MEMBERSHIP_CONTEXT, BOTH_SIDES_BY_AUDIENCE, OPERATOR_ONLY_ROUTE,
    DURABLE_ROW_SIDE, OPERATOR_DEFAULT, LIVE_MEMBERSHIP_BY_DESIGN,
}


# ---------------------------------------------------------------------------
# EXEMPTIONS — sites that INTRODUCE a side from something other than the
# trusted resolution, and the first-class documented reason each is correct.
#
# These are NOT debt. Each names a class above whose condition is checked
# mechanically, and a site whose class condition fails is a violation even
# though it is listed. A dormant entry is an error
# (`verify_registry_liveness`), so the table cannot rot.
# ---------------------------------------------------------------------------
def origin_key(origins):
    """The stable fingerprint of ONE call site's side provenance.

    EXEMPTIONS and LEDGER are keyed on ``(function, producer, origin_key)``
    rather than on ``(function, producer)`` — the same discipline
    ``route_extract._AUDIT_WAIVERS`` uses when it keys on exact normalized
    call text, and for the same reason. One function routinely calls one
    producer TWICE: ``get_roster_status`` reads it once with no side (the
    unscoped-operator branch) and once with the trusted side, and
    ``get_board`` reads three producers off one binding. A pair-keyed
    exemption would silently cover a THIRD, new call in the same function —
    which is exactly the adversarial case
    ``tests/test_side_provenance_guard.py`` drives, and exactly how the
    fifth leak would have slipped through the gate meant to catch it."""
    return "|".join(sorted(origins))


#: ``{(function, producer, origin_key): (class, route_or_None, reason)}``
EXEMPTIONS = {
    ("_schedule_roster_status", "compute_roster_status",
     "attr:game.home_team_id|call:game_scoped_own_team_id"): (
        OPERATOR_DEFAULT, None,
        "the Dashboard schedule row (#205 round 4). The OWN_SIDE branch "
        "passes the trusted per-row resolution and the withheld branch "
        "never reaches the producer at all; what this entry covers is the "
        "FULL branch's `side = game.home_team_id`, the unchanged "
        "unscoped-operator answer."),
    ("get_board", "compute_roster_status",
     "attr:game.home_team_id|const:None|param:get_board.team_id"): (
        OPERATOR_DEFAULT, None,
        "the parameter IS the trusted side (`lineup_visibility.own_side`, "
        "off the ONE `resolve_private_game_read`). The `game.home_team_id` "
        "fallback is now AUDIENCE-BOUND (#427 round 2, blocker 1): it is "
        "applied only when `lineup_visibility.default_side_permitted(role)` "
        "— an unscoped operator, an assigned official or an in-process "
        "caller, all of whom may read either side. `const:None` is the new "
        "REFUSAL binding a Coach/Player with no resolved side takes, and it "
        "reaches no producer at all: that branch sets `status`/`rows` to "
        "None without calling one. This scanner is flow-INSENSITIVE, so it "
        "unions every binding of the name; the third origin appearing here "
        "is that union, not a third read."),
    ("get_board", "_lineup_rows",
     "attr:game.home_team_id|const:None|param:get_board.team_id"): (
        OPERATOR_DEFAULT, None, "same binding, same fallback as above."),
    ("get_board", "_activity_projection",
     "attr:game.home_team_id|const:None|param:get_board.team_id"): (
        OPERATOR_DEFAULT, None, "same binding, same fallback as above."),
    ("get_roster_status", "compute_roster_status",
     "absent"): (
        OPERATOR_DEFAULT, None,
        "the FULL branch omits the side so the producer's own home default "
        "applies — reached only after `route_audience` returned FULL."),
    ("get_availability_summary", "_availability_summary_of",
     "const:''|param:get_availability_summary.team_id|param:get_availability_summary.viewer_team_id"): (
        OPERATOR_DEFAULT, None,
        "the FULL branch KEEPS the client hint (`team_id or viewer_team_id "
        "or ''`). An unscoped operator may read either side, and narrowing "
        "them would be its own regression; the OWN_SIDE branch, two lines "
        "up, ignores the hint entirely and is TRUSTED without an entry."),
    ("compute_roster_status", "_side_data",
     "attr:game.home_team_id|param:compute_roster_status.team_id"): (
        OPERATOR_DEFAULT, None,
        "the producer's OWN documented default. A producer is not the "
        "boundary — who may call it without a side is, which is what every "
        "other entry in this table is about."),
    ("list_substitute_candidates", "compute_roster_status",
     "attr:game.home_team_id|param:list_substitute_candidates.team_id"): (
        OPERATOR_DEFAULT, None, "the producer's own default, as above."),
    ("list_addable_players", "compute_roster_status",
     "attr:game.home_team_id|param:list_addable_players.team_id"): (
        OPERATOR_DEFAULT, None, "the producer's own default, as above."),
    ("_draft_review_row", "compute_roster_status",
     "absent"): (
        OPERATOR_ONLY_ROUTE, "get_scheduler_drafts",
        "the operator draft-review row. The side is omitted so the "
        "producer's home default applies; the route is operator-only, and "
        "an operator may read either side."),
    ("auto_build_roster", "compute_roster_status",
     "subscript:result"): (
        AUTHORIZED_WRITE, None,
        "`result['team_id']` — the side the write itself resolved and "
        "`authorized_team_id` gated, read back to report the outcome."),
    ("get_substitute_opportunity", "compute_roster_status",
     "attr:ctx.team_id"): (
        SUBJECT_MEMBERSHIP_CONTEXT, None,
        "`ctx.team_id` — the side of the GameMembershipContext this method "
        "resolves ITSELF, for the subject named by its own `player_id`, "
        "against THIS game. Re-examined when SUBJECT_OWN_SIDE was removed: "
        "the side is the authoritative game-scoped membership, not the "
        "permanent pointer, which is what the old class could not tell."),
    ("get_player_home", "compute_roster_status",
     "attr:offer_ctx.team_id"): (
        SUBJECT_MEMBERSHIP_CONTEXT, None,
        "the signed-in player's same-team OFFER is summarized against the "
        "GameMembershipContext this method resolves for that exact player "
        "and game; cross-team offers never take this branch or read target "
        "roster status."),
    ("substitute_block_reason", "compute_roster_status",
     "attr:ctx.team_id"): (
        SUBJECT_MEMBERSHIP_CONTEXT, None,
        "the same subject's own resolved context one layer down: `ctx` may "
        "be handed in by a caller that already resolved it for this exact "
        "(game, player), and this method resolves it itself when it is "
        "not."),
    ("substitute_offer_block_reason", "compute_roster_status",
     "attr:enrollment.team_id"): (
        DURABLE_ROW_SIDE, None,
        "a cross-team offer is evaluated against the durable target side "
        "recorded on the enrollment; the original source membership is "
        "revalidated separately and never supplies the target slot."),
    ("_back_out_entry", "compute_roster_status",
     "attr:entry.team_side"): (
        DURABLE_ROW_SIDE, None,
        "`entry.team_side` — the side of record ON THE ROW being backed "
        "out. A pre-061 row carries None, the producer's home default "
        "applies to the MESSAGE only, and the targeted push is skipped "
        "rather than sent to a guessed audience."),
    ("_submitted_lineup_sides", "_lineup_rows",
     "attr:game.away_team_id|attr:game.home_team_id"): (
        BOTH_SIDES_BY_AUDIENCE, None,
        "iterates `(game.home_team_id, game.away_team_id)` on purpose: the "
        "assigned official's projection IS two-sided."),
}

# ---------------------------------------------------------------------------
# LIVE-MEMBERSHIP READERS — a first-class DESIGN RECORD, not debt.
#
# These reads obey the side rule above like every other producer: the SIDE
# they answer for is still the trusted one. What is different is the
# POPULATION they answer with — they read LIVE membership rather than the
# game's durable attribution, and they are right to. "Who owes this side an
# availability answer" and "who could still be enrolled" are questions about
# who is on the team NOW; a player who has left owes nothing and cannot be
# enrolled. Durable attribution answers a different question ("whose row is
# this"), and #427 spent a round making the ROW readers use it.
#
# Recorded here so that (a) a reader can see the distinction is deliberate
# rather than an oversight the durable-attribution work missed, and (b) it
# cannot silently become the model for a NEW reader that should have used
# durable attribution. CONDITION: each must still resolve live membership,
# checked by `verify_registry_liveness`.
#
# The CREATE side of the standing ruling belongs to the same class and is
# recorded for the same reason: when a row is first written, its side is
# minted from live membership because there is no durable record yet.
# ---------------------------------------------------------------------------
LIVE_MEMBERSHIP_READERS = {
    "_availability_candidates": (
        "api/service.py",
        "'who owes this side an availability answer' — the ONE discovery "
        "both availability surfaces consume. Live by design: a departed "
        "player owes nothing."),
    "list_addable_players": (
        "services/roster_service.py",
        "'who could still be enrolled as a substitute for this side' — "
        "eligibility is a fact about NOW, not about a row that does not "
        "exist yet."),
    "add_substitute_to_roster": (
        "services/roster_service.py",
        "the CREATE side of the standing ruling. A row's `team_side` is "
        "minted from the live membership context this transition's own gate "
        "resolved (`_require_membership_context`), because until this write "
        "there IS no durable record. Every READ of that side afterwards uses "
        "the durable column."),
    "_accept_offered_substitute": (
        "services/roster_service.py",
        "the same CREATE mint on the player-self-service path."),
}

#: Module qualifiers for the registries above, for the names the package
#: spells more than once (a facade method and the service method it wraps).
#: An entry is required only where `_resolve_declared` reports ambiguity.
ADJUDICATED_READER_MODULES = {
    "get_board": "api/service.py",
    "get_lineups": "api/service.py",
    "get_roster": "api/service.py",
    "get_roster_status": "api/service.py",
    "get_substitutes": "api/service.py",
    "get_availability_summary": "api/service.py",
    "get_substitute_candidates": "api/service.py",
    "get_addable_substitutes": "api/service.py",
    "_schedule_roster_status": "api/service.py",
    "_workflow_side": "api/service.py",
}
SIDE_FORWARDER_MODULES = {
    "_availability_summary_of": "api/service.py",
    "_lineup_rows": "api/service.py",
    "_slot_summaries": "services/roster_service.py",
    "lineup_population": "services/roster_service.py",
    "remind_unresponded": "api/service.py",
    "_require_open_slot": "services/roster_service.py",
    "_seat_batch": "services/roster_service.py",
    "_newest_prior_source": "services/roster_service.py",
    "side": "api/service.py",
}
EXEMPTION_MODULES = {
    "_schedule_roster_status": "api/service.py",
    "get_board": "api/service.py",
    "get_roster_status": "api/service.py",
    "get_availability_summary": "api/service.py",
    "compute_roster_status": "services/roster_service.py",
    "list_substitute_candidates": "services/roster_service.py",
    "list_addable_players": "services/roster_service.py",
    "_draft_review_row": "api/service.py",
    "auto_build_roster": "api/service.py",
    "get_substitute_opportunity": "api/service.py",
    "substitute_block_reason": "services/roster_service.py",
    "_back_out_entry": "services/roster_service.py",
    "_submitted_lineup_sides": "api/service.py",
}

#: ACCEPTED-BUT-UNCLASSIFIED sites. Empty, and that is the strongest state
#: this table can be in: every producer call in the package is either TRUSTED
#: by provenance, a declared forwarder, or an exemption whose condition is
#: machine-checked. An entry here would mean "we know about this one and have
#: not decided". A DORMANT entry is an error, so it can only shrink.
LEDGER = {}


# ---------------------------------------------------------------------------
# THE HOME-TEAM FALLBACK LEDGER — detector 2.
#
# Independent of the producer set on purpose: `x or <expr>.home_team_id` is
# the literal shape all four leaks shared, and this finds it anywhere in the
# package, including in a function that calls no producer at all. Keyed on
# (file, enclosing function) so a new one in a NEW function fails even if it
# is spelled exactly like an accepted one.
# ---------------------------------------------------------------------------
HOME_FALLBACKS = {
    ("api/service.py", "get_board"):
        "the unscoped-operator default; see EXEMPTIONS.",
    ("api/service.py", "_workflow_side"):
        "the FULL branch's default, inside the adjudicator itself.",
    ("services/roster_service.py", "_batch_team"):
        "the batch WRITE's side resolver: when `authorized_team_id` is None "
        "the caller is an unscoped operator, and the #25 home default is "
        "preserved byte-for-byte. A scoped caller's requested side is "
        "revalidated against their authorization instead, never defaulted.",
    ("services/roster_service.py", "compute_roster_status"):
        "the producer's own documented default.",
    ("services/roster_service.py", "list_substitute_candidates"):
        "the producer's own documented default.",
    ("services/roster_service.py", "list_addable_players"):
        "the producer's own documented default.",
}


# ---------------------------------------------------------------------------
# THE PRIVATE-GAME DISPATCH — detector 3.
#
# `web/server.py` resolves the caller's own side ONCE for the whole
# `/api/games/{id}/…` family and hands that one trusted value to every leaf.
# This registry is what makes a NEW leaf fail the build rather than inherit
# a gate it was never checked against.
# ---------------------------------------------------------------------------
#: The one name the dispatch hoists the trusted resolution into.
DISPATCH_TRUSTED_NAME = "own_team"

#: ``{leaf: what decides its side}`` — every `if sub == "<leaf>":` branch of
#: the private-game block. A leaf present in the dispatch and absent here is
#: a violation; an entry here matching no branch is a DORMANT error.
PRIVATE_GAME_LEAVES = {
    "board": "lineup_visibility.own_side on the hoisted own_team",
    "lineups": "side_projections on the hoisted own_team",
    "roster-status": "route_audience on the hoisted own_team",
    "roster": "route_audience on the hoisted own_team",
    "substitutes": "route_audience on the hoisted own_team",
    "availability-summary": "route_audience; the ?team_id hint is adjudicated",
    "substitute-candidates": "_workflow_side; the ?team_id hint is adjudicated",
    "substitute-addable": "_workflow_side; the ?team_id hint is adjudicated",
    "officials": "no side: the game's staff assignments are not per-team",
    "reschedule": "no side: a game's reschedule history is not per-team",
}


class Violation:
    """One site the rule refuses, named so the next reader does not have to
    rediscover what it is about."""

    def __init__(self, kind, path, line, function, what, should_be):
        self.kind = kind
        self.path = path
        self.line = line
        self.function = function
        self.what = what
        self.should_be = should_be

    def __repr__(self):  # pragma: no cover - shown only in a failure
        return str(self)

    def __str__(self):
        return (f"{self.path}:{self.line}  [{self.kind}]  in {self.function}()\n"
                f"      READ:        {self.what}\n"
                f"      SHOULD BE:   {self.should_be}")

    @property
    def site(self):
        return (self.path, self.function, self.line)


# ---------------------------------------------------------------------------
# Dataflow
# ---------------------------------------------------------------------------
def _dotted(node):
    """``a.b.c`` for a Name/Attribute chain, else ``None``."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _dotted(node.value)
        return f"{base}.{node.attr}" if base else node.attr
    if isinstance(node, ast.Call):
        return _dotted(node.func)
    return None


def _final_name(node):
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Name):
        return node.id
    return None


class _Scope:
    """One function's parameters and its assignments, with enough position
    information to see a parameter REBOUND above a use.

    Flow sensitivity is deliberately minimal and deliberately explicit: an
    assignment that is a DIRECT statement of the function body (depth 0) and
    that appears above the use SUPERSEDES the parameter, because that is what
    a reader means by "the parameter was reassigned above". An assignment
    nested inside a branch or a loop does NOT supersede — it is unioned in,
    which is the fail-closed direction."""

    def __init__(self, fn):
        self.fn = fn
        self.name = fn.name
        args = fn.args
        self.params = {a.arg for a in
                       args.posonlyargs + args.args + args.kwonlyargs}
        if args.vararg:
            self.params.add(args.vararg.arg)
        if args.kwarg:
            self.params.add(args.kwarg.arg)
        # Names REBOUND inside this function by a nested `def`/`lambda`/
        # `class` — Hole B: a nested definition named exactly like the trusted
        # resolver used to be trusted on the strength of its NAME alone.
        self.shadowed = set()
        for stmt in ast.walk(fn):
            if stmt is fn:
                continue
            if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef,
                                 ast.ClassDef)):
                self.shadowed.add(stmt.name)
            elif isinstance(stmt, (ast.Import, ast.ImportFrom)):
                for alias in stmt.names:
                    self.shadowed.add((alias.asname or alias.name)
                                      .split(".")[0])
        # name -> [(lineno, depth, value_expr)]
        self.assigns = {}
        top = {id(stmt) for stmt in fn.body}
        for stmt in ast.walk(fn):
            depth = 0 if id(stmt) in top else 1
            if isinstance(stmt, ast.Assign):
                for target in stmt.targets:
                    for n in _bound_names(target):
                        self.assigns.setdefault(n, []).append(
                            (stmt.lineno, depth, stmt.value))
            elif isinstance(stmt, (ast.AnnAssign, ast.AugAssign)):
                if stmt.value is not None:
                    for n in _bound_names(stmt.target):
                        self.assigns.setdefault(n, []).append(
                            (stmt.lineno, depth, stmt.value))
            elif isinstance(stmt, ast.NamedExpr):
                for n in _bound_names(stmt.target):
                    self.assigns.setdefault(n, []).append(
                        (stmt.lineno, depth, stmt.value))
            elif isinstance(stmt, ast.For):
                for n in _bound_names(stmt.target):
                    # The loop ITERABLE is the origin: `for t in (home, away)`
                    # introduces both sides, and that must be visible.
                    self.assigns.setdefault(n, []).append(
                        (stmt.lineno, 1, stmt.iter))
            elif isinstance(stmt, ast.With):
                for item in stmt.items:
                    if item.optional_vars is not None:
                        for n in _bound_names(item.optional_vars):
                            self.assigns.setdefault(n, []).append(
                                (stmt.lineno, 1, item.context_expr))


def _bound_names(target):
    if isinstance(target, ast.Name):
        return [target.id]
    if isinstance(target, (ast.Tuple, ast.List)):
        out = []
        for elt in target.elts:
            out.extend(_bound_names(elt))
        return out
    return []


#: Modules allowed to RE-EXPORT the canonical resolver, and therefore to be
#: an honest import source for it. Verified, not asserted: each must itself
#: import the name from :data:`TRUSTED_RESOLVER_MODULE` (see
#: :func:`audit_trusted_binding`), so the chain always ends at the one
#: definition. ``web/scope.py`` is the historical home the function moved out
#: of; every `from .scope import game_scoped_own_team_id` caller still works.
TRUSTED_REEXPORTS = ("web/scope.py",)


class _ModuleContext:
    """WHICH NAMES IN THIS MODULE REALLY ARE THE CANONICAL TRUSTED SYMBOL —
    Hole B, closed.

    Before this existed, ``_trusted_origin`` compared a call's LAST NAME
    COMPONENT to the string ``"game_scoped_own_team_id"``. Anything whose
    final component matched was trusted unconditionally: a nested ``def`` in
    the same function, a method on ``ApiService``, a local lambda, a module
    alias of something else entirely, an ``import x as game_scoped_own_team_id``.
    ``verify_registry_liveness`` checked that the REAL resolver still existed;
    nothing checked that it was the ONLY thing wearing its name.

    A binding is trusted here only when it can be RESOLVED to the one
    module-level definition in :data:`TRUSTED_RESOLVER_MODULE`:

    * a bare ``game_scoped_own_team_id(...)`` in a module that imports the
      name from the canonical module (or from a declared, verified re-export
      of it), and that is NOT shadowed inside the enclosing function;
    * ``<alias>.game_scoped_own_team_id(...)`` where ``<alias>`` is that
      module imported as a module;
    * the canonical module's own body.

    Every OTHER binding of that name anywhere in the package is reported by
    :func:`audit_trusted_binding` as a forgery, so the two halves together say
    "this call reaches the canonical symbol" rather than "this call is spelled
    like it".
    """

    def __init__(self, path, tree):
        self.path = path
        self.trusted_names = set()
        self.trusted_modules = set()
        self.forgeries = []
        if path == TRUSTED_RESOLVER_MODULE:
            self.trusted_names.add(TRUSTED_RESOLVER)
        canonical_mod = Path(TRUSTED_RESOLVER_MODULE).stem
        honest_sources = {canonical_mod}
        honest_sources |= {Path(m).stem for m in TRUSTED_REEXPORTS}
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                source = (node.module or "").split(".")[-1]
                for alias in node.names:
                    bound = alias.asname or alias.name
                    if alias.name == TRUSTED_RESOLVER:
                        if source in honest_sources \
                                and bound == TRUSTED_RESOLVER:
                            self.trusted_names.add(bound)
                        else:
                            self.forgeries.append(
                                (node.lineno,
                                 f"imports {TRUSTED_RESOLVER!r} from "
                                 f"{node.module!r} as {bound!r}"))
                    elif bound == TRUSTED_RESOLVER:
                        self.forgeries.append(
                            (node.lineno,
                             f"binds the name {TRUSTED_RESOLVER!r} to "
                             f"{alias.name!r}"))
                    elif alias.name in honest_sources:
                        self.trusted_modules.add(bound)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    bound = alias.asname or alias.name.split(".")[0]
                    if bound == TRUSTED_RESOLVER:
                        self.forgeries.append(
                            (node.lineno,
                             f"binds the name {TRUSTED_RESOLVER!r} to the "
                             f"module {alias.name!r}"))
                    elif alias.name.split(".")[-1] in honest_sources:
                        self.trusted_modules.add(bound)

    def resolves_to_canonical(self, func, scope):
        """Does THIS call expression reach the one canonical definition?"""
        if isinstance(func, ast.Name):
            if func.id != TRUSTED_RESOLVER:
                return False
            if scope is not None and (func.id in scope.params
                                      or func.id in scope.shadowed
                                      or func.id in scope.assigns):
                return False
            return TRUSTED_RESOLVER in self.trusted_names
        if isinstance(func, ast.Attribute):
            if func.attr != TRUSTED_RESOLVER:
                return False
            base = _dotted(func.value)
            return base in self.trusted_modules
        return False

    def resolves_to_adjudicator(self, func, name):
        """An ADJUDICATOR is a method of the module that declares it, and is
        trusted only when called AS one: ``self.<name>(...)`` inside that same
        module. A same-named function anywhere else — or a local rebinding —
        is not the adjudicator whose ``route_audience`` call was verified."""
        if not isinstance(func, ast.Attribute) or func.attr != name:
            return False
        if not isinstance(func.value, ast.Name) or func.value.id != "self":
            return False
        return self.path == ADJUDICATORS.get(name)


#: The suffix a call origin carries when its callee WEARS a trusted name but
#: does not RESOLVE to the trusted symbol. Deliberately part of the origin
#: string, so a forged binding gets its own EXEMPTIONS fingerprint and can
#: never be covered by the honest site's entry.
UNRESOLVED_BINDING = "!not-the-canonical-binding"


def _origins(expr, scope, line, seen=None, ctx=None):
    """Every ROOT this side expression can have come from.

    A set of tagged strings rather than AST nodes, so a verdict is a set
    membership test a reader can check by eye and a failure message can
    print.

    ``ctx`` is the enclosing module's :class:`_ModuleContext`. A call whose
    callee merely WEARS a trusted name but does not RESOLVE to the canonical
    symbol is tagged :data:`UNRESOLVED_BINDING`, so it is a different origin
    from the honest one — untrusted, and with its own exemption fingerprint.
    ``None`` means "no binding information", which is the FAIL-CLOSED
    direction: nothing resolves, so nothing is trusted."""
    seen = seen if seen is not None else set()
    if expr is None:
        return {"absent"}
    if isinstance(expr, ast.BoolOp):
        out = set()
        for value in expr.values:
            out |= _origins(value, scope, line, seen, ctx)
        return out
    if isinstance(expr, ast.IfExp):
        return (_origins(expr.body, scope, line, seen, ctx)
                | _origins(expr.orelse, scope, line, seen, ctx))
    if isinstance(expr, ast.Tuple):
        out = set()
        for elt in expr.elts:
            out |= _origins(elt, scope, line, seen, ctx)
        return out
    if isinstance(expr, ast.Name):
        key = (expr.id, line)
        if key in seen:
            return set()
        seen = seen | {key}
        candidates = [c for c in scope.assigns.get(expr.id, [])
                      if c[0] < line]
        dominating = [c for c in candidates if c[1] == 0]
        out = set()
        if dominating:
            # A rebind at the top of the body: the parameter is gone.
            for lineno, _depth, value in dominating:
                out |= _origins(value, scope, lineno, seen, ctx)
            return out or {f"unresolved:{expr.id}"}
        for lineno, _depth, value in candidates:
            out |= _origins(value, scope, lineno, seen, ctx)
        if expr.id in scope.params:
            out.add(f"param:{scope.name}.{expr.id}")
        return out or {f"unresolved:{expr.id}"}
    if isinstance(expr, ast.Call):
        name = _final_name(expr.func) or "?"
        if name == TRUSTED_RESOLVER:
            resolved = ctx is not None and ctx.resolves_to_canonical(
                expr.func, scope)
            return {f"call:{name}" if resolved
                    else f"call:{name}{UNRESOLVED_BINDING}"}
        if name in ADJUDICATORS:
            resolved = ctx is not None and ctx.resolves_to_adjudicator(
                expr.func, name)
            return {f"call:{name}" if resolved
                    else f"call:{name}{UNRESOLVED_BINDING}"}
        return {f"call:{name}"}
    if isinstance(expr, ast.Attribute):
        return {f"attr:{_dotted(expr)}"}
    if isinstance(expr, ast.Subscript):
        return {f"subscript:{_dotted(expr.value) or '?'}"}
    if isinstance(expr, ast.Constant):
        return {f"const:{expr.value!r}"}
    return {f"expr:{type(expr).__name__}"}


# ---------------------------------------------------------------------------
# Source loading
# ---------------------------------------------------------------------------
def package_sources(root=None):
    """``{relative path: source text}`` for every module this gate covers."""
    root = Path(root or PACKAGE_ROOT)
    out = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
        for filename in sorted(filenames):
            if not filename.endswith(".py"):
                continue
            full = Path(dirpath) / filename
            out[str(full.relative_to(root))] = full.read_text()
    return out


#: ``{source text: parsed tree}``. The liveness pass asks "where is this
#: name defined" and "who calls this name" once per registry entry, which
#: re-parses every module O(entries) times; the trees are immutable for the
#: duration of one audit, so parsing each source once is a pure speed-up with
#: no change in what is checked. Keyed on the SOURCE TEXT, so a mutated
#: fixture never reuses the real module's tree.
_PARSE_CACHE = {}
#: ``{source text: [(fn, parent), …]}`` — same reasoning.
_FUNCTIONS_CACHE = {}


def parse(text):
    """``ast.parse`` with the audit-lifetime cache."""
    tree = _PARSE_CACHE.get(text)
    if tree is None:
        tree = _PARSE_CACHE[text] = ast.parse(text)
        if len(_PARSE_CACHE) > 400:  # a bounded cache, never a leak
            _PARSE_CACHE.clear()
            _FUNCTIONS_CACHE.clear()
            _PARSE_CACHE[text] = tree
    return tree


def functions_in(text):
    out = _FUNCTIONS_CACHE.get(text)
    if out is None:
        out = _FUNCTIONS_CACHE[text] = _functions(parse(text))
    return out


def _functions(tree):
    """``(function node, enclosing function node or None)`` for every def."""
    out = []

    def walk(node, parent):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                out.append((child, parent))
                walk(child, child)
            else:
                walk(child, parent)

    walk(tree, None)
    return out


def _calls_in(fn):
    """Calls whose NEAREST enclosing function is ``fn`` — a call inside a
    nested def belongs to that def, never to this one."""
    out = []

    def walk(node):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef,
                                  ast.Lambda)):
                continue
            if isinstance(child, ast.Call):
                out.append(child)
            walk(child)

    walk(fn)
    return out


def _body_calls(fn):
    """Every callee simple-name reachable in ``fn``, nested defs included."""
    return {_final_name(n.func) for n in ast.walk(fn)
            if isinstance(n, ast.Call)}


# ---------------------------------------------------------------------------
# Detector 1 — side provenance at producer call sites
# ---------------------------------------------------------------------------
def _side_argument(call, producer):
    """The expression supplying ``producer``'s side at this call, or ``None``
    when the argument was omitted."""
    param, index = PRODUCERS[producer]
    for keyword in call.keywords:
        if keyword.arg == param:
            return keyword.value
    # A bound call `self.f(a)` passes `self` implicitly, so the declared
    # index (which counts self) shifts by one.
    offset = 1 if isinstance(call.func, ast.Attribute) else 0
    position = index - offset
    if 0 <= position < len(call.args):
        return call.args[position]
    return None


def _trusted_origin(origin, readers, adjudicators):
    if origin == f"call:{TRUSTED_RESOLVER}":
        return True
    if origin.startswith("call:") and origin[5:] in adjudicators:
        return True
    if origin.startswith("param:"):
        function, _, param = origin[6:].partition(".")
        return readers.get(function) == param
    return False


def audit_side_provenance(sources=None):
    """Every producer call whose side did not come from the trusted
    resolution, an adjudicated decision, a declared forwarder or a declared
    exemption."""
    sources = package_sources() if sources is None else sources
    violations = []
    used_forwarders, used_exemptions, used_ledger = set(), set(), set()
    for path, text in sorted(sources.items()):
        ctx = _ModuleContext(path, parse(text))
        for fn, parent in functions_in(text):
            scope = _Scope(fn)
            for call in _calls_in(fn):
                producer = _final_name(call.func)
                if producer not in PRODUCERS:
                    continue
                if fn.name == producer:
                    continue  # a producer's own recursive/self reference
                arg = _side_argument(call, producer)
                origins = _origins(arg, scope, call.lineno, ctx=ctx)
                trusted = all(
                    _trusted_origin(o, ADJUDICATED_READERS, ADJUDICATORS)
                    for o in origins)
                if trusted:
                    continue
                # A declared adjudicated READER is the BOUNDARY: it decides.
                # It may not fall back on "I only forwarded what I was
                # given", because the parameters it was given include the
                # CLIENT HINT (`get_availability_summary`'s `team_id`,
                # `get_substitute_candidates`'s `team_id`). Only its ONE
                # declared trusted parameter is trusted, and that is already
                # covered by the TRUSTED verdict above.
                forwarded = (fn.name not in ADJUDICATED_READERS
                             and all(o.startswith(f"param:{fn.name}.")
                                     for o in origins))
                shown = ", ".join(sorted(origins))
                if forwarded:
                    if fn.name in SIDE_FORWARDERS:
                        used_forwarders.add(fn.name)
                        continue
                    violations.append(Violation(
                        "undeclared_forwarder", path, call.lineno, fn.name,
                        f"{producer}(…) with the side forwarded from this "
                        f"function's own parameter ({shown})",
                        "declare this function in side_provenance."
                        "SIDE_FORWARDERS with the class that says who "
                        "carries the obligation instead — forwarding decides "
                        "nothing, so the chain of forwarders has to stay "
                        "enumerated or a new route hides inside it"))
                    continue
                key = (fn.name, producer, origin_key(origins))
                if key in EXEMPTIONS:
                    used_exemptions.add(key)
                    continue
                if key in LEDGER:
                    used_ledger.add(key)
                    continue
                violations.append(Violation(
                    "untrusted_side", path, call.lineno, fn.name,
                    f"{producer}(…) with a side from {shown}",
                    f"pass the side the SERVER resolved — "
                    f"{TRUSTED_RESOLVER}(…), a declared adjudicator "
                    f"({', '.join(sorted(ADJUDICATORS))}), or a parameter of "
                    f"a declared adjudicated reader. If this site is "
                    f"genuinely correct, add ({fn.name!r}, {producer!r}) to "
                    f"side_provenance.EXEMPTIONS with one of the documented "
                    f"classes; its condition is machine-checked. The key is "
                    f"({fn.name!r}, {producer!r}, {origin_key(origins)!r})"))
    return violations, {"forwarders": used_forwarders,
                        "exemptions": used_exemptions,
                        "ledger": used_ledger}


# ---------------------------------------------------------------------------
# Detector 2 — the home-team fallback shape, anywhere
# ---------------------------------------------------------------------------
_SIDE_ATTRS = ("home_team_id", "away_team_id")


def audit_home_fallback(sources=None):
    """``x or <expr>.home_team_id`` — the literal shape all four leaks shared
    — anywhere in the package, whether or not it reaches a declared
    producer."""
    sources = package_sources() if sources is None else sources
    violations, used = [], set()
    for path, text in sorted(sources.items()):
        for fn, _parent in functions_in(text):
            for node in ast.walk(fn):
                if not isinstance(node, ast.BoolOp) \
                        or not isinstance(node.op, ast.Or):
                    continue
                fallbacks = [v for v in node.values[1:]
                             if isinstance(v, ast.Attribute)
                             and v.attr in _SIDE_ATTRS]
                if not fallbacks:
                    continue
                key = (path, fn.name)
                if key in HOME_FALLBACKS:
                    used.add(key)
                    continue
                violations.append(Violation(
                    "home_team_fallback", path, node.lineno, fn.name,
                    f"a side defaulting to {_dotted(fallbacks[0])} when the "
                    "supplied one is falsy",
                    "resolve the side from "
                    f"{TRUSTED_RESOLVER}(…) instead. A silent home default "
                    "is the exact defect #427 removed from get_board, "
                    "/roster-status, list_substitute_candidates and "
                    "/api/demo/overview: it answers an AWAY caller with "
                    "HOME's private state. If this really is an "
                    "unscoped-operator default, add "
                    f"({path!r}, {fn.name!r}) to "
                    "side_provenance.HOME_FALLBACKS with its reason"))
    return violations, used


# ---------------------------------------------------------------------------
# Detector 3 — EVERY REACH INTO TWO-SIDED GAME STORAGE IS ENUMERATED
#
# HOLE A, the one the ship-date counterfactual exposed. The provenance
# detector above classifies the SIDE ARGUMENT of a producer call — so it is
# blind, by construction, to a read that takes NO SIDE AT ALL because it goes
# straight to the store:
#
#     def get_roster(self, game_id):
#         return [_serialize(e) for e in self.store.roster_for_game(game_id)]
#
# That is F1 and F3 verbatim, and against the real 2f8eb73 source the
# detectors reported ZERO violations naming `get_roster`/`get_substitutes`/
# `get_board`. They only appear "caught" at HEAD because the FIX already
# landed and left registry entries whose LIVENESS check fires when the fix is
# undone — which proves nothing about a NEW route of that shape, and nothing
# at all about a route outside the games family, where rounds 4 and 5 both
# came from.
#
# The closure property here is about REACHING the storage rather than about
# naming a side: these store methods return rows spanning BOTH sides of a
# game, so any function that calls one holds two-sided private state and must
# say, in a machine-checked class, what it does with it.
# ---------------------------------------------------------------------------
#: Store reads that return rows for BOTH sides of one game.
TWO_SIDED_STORE_READS = ("roster_for_game", "substitutes_for_game",
                         "availability_for_game", "audit_for_game",
                         "notifications_for_game")

NARROWS_BY_TRUSTED_SIDE = "narrows_by_trusted_side"
#: The function immediately narrows the rows to ONE side. CONDITION: it must
#: be a declared PRODUCER or SIDE_FORWARDER, so detector 1 gates the side it
#: narrows by and its own callers are enumerated.

TWO_SIDED_BY_AUDIENCE_READER = "two_sided_by_audience_reader"
#: The function reads both sides and then PROJECTS by audience. CONDITION: it
#: must be a declared ADJUDICATED_READER, whose ``route_audience`` call is
#: separately verified.

GAME_WIDE_WRITE = "game_wide_write"
#: A lifecycle write over the whole game (cancel, delete) that touches both
#: sides because the GAME does. CONDITION: the function must take an
#: ``actor_id`` — a read cannot claim this class.

ATTRIBUTION_PRIMITIVE = "attribution_primitive"
#: The function IS the durable-attribution authority: it answers "which side
#: is this row on", which is the question the side rule is decided by, so it
#: necessarily reads both. CONDITION: every caller must itself be classified
#: — a declared producer, forwarder, adjudicated reader, or an entry in this
#: registry — so the two-sided value never escapes an enumerated chain.

IDENTITY_RECOVERY_ONLY = "identity_recovery_only"
#: The function reads both sides only to RECOGNISE player identities so an
#: event mentioning an unattributable or opponent player can be WITHHELD. The
#: value never leaves as content. CONDITION: the same closure as
#: ATTRIBUTION_PRIMITIVE — every caller classified.

OUT_OF_BAND_TOOLING = "out_of_band_tooling"
#: A seeding/demo script that is not part of any request path. CONDITION: the
#: module must not be imported by ``web/server.py``, so no route can reach it.

#: ``{(module, function): (class, reason)}`` — every function in the package
#: that reaches two-sided game storage. Keyed by MODULE and function so a
#: same-named facade wrapper and service method are never confused, and so a
#: DORMANT entry (a site that no longer exists) is an error.
TWO_SIDED_READERS = {
    ("api/service.py", "get_board"): (
        TWO_SIDED_BY_AUDIENCE_READER,
        "reads the game-wide audit and notification streams and hands them "
        "to `_activity_projection`, which withholds every event it cannot "
        "durably attribute to the audience's own side."),
    ("api/service.py", "get_roster"): (
        TWO_SIDED_BY_AUDIENCE_READER,
        "F1's own route. It now takes `viewer_team_id`, consults "
        "`route_audience` and answers one side; the raw store read is "
        "narrowed before anything is serialized."),
    ("api/service.py", "get_substitutes"): (
        TWO_SIDED_BY_AUDIENCE_READER,
        "F3's own route, narrowed the same way and by the same audience "
        "decision."),
    ("api/service.py", "_availability_summary_of"): (
        NARROWS_BY_TRUSTED_SIDE,
        "a declared producer: it answers ONE side's availability rollup for "
        "the side it was handed, and decides nothing about which."),
    ("api/service.py", "_lineup_rows"): (
        NARROWS_BY_TRUSTED_SIDE,
        "a declared producer: the availability join is per (game, player), "
        "and the population it joins onto is already one side's."),
    ("api/service.py", "_game_player_universe"): (
        IDENTITY_RECOVERY_ONLY,
        "the VALUE half of `_player_ids_in`'s two-way identity recovery. "
        "Deliberately wider than the durable sides: a player with NO side "
        "must still be RECOGNISED, precisely so the event naming them is "
        "withheld rather than passed through."),
    ("services/roster_service.py", "_side_data"): (
        NARROWS_BY_TRUSTED_SIDE,
        "a declared producer: it partitions the game's rows against the side "
        "it was handed and returns only that side's."),
    ("services/roster_service.py", "lineup_population"): (
        NARROWS_BY_TRUSTED_SIDE,
        "a declared producer: the four populations are computed for the one "
        "side it was handed."),
    ("services/roster_service.py", "_lineup_substitutes"): (
        ATTRIBUTION_PRIMITIVE,
        "selects one deterministic active-or-newest durable enrollment per "
        "player across the game; its only caller is the declared one-side "
        "lineup_population producer, which performs the side narrowing."),
    ("services/roster_service.py", "_prior_side_candidates"): (
        NARROWS_BY_TRUSTED_SIDE,
        "a declared producer: the copy-roster source rows for the side the "
        "write already authorized."),
    ("services/roster_service.py", "list_substitute_candidates"): (
        NARROWS_BY_TRUSTED_SIDE,
        "a declared producer: the outreach queue for one side."),
    ("services/roster_service.py", "list_addable_players"): (
        NARROWS_BY_TRUSTED_SIDE,
        "a declared producer: the addable pool for one side."),
    ("services/roster_service.py", "_seat_batch"): (
        NARROWS_BY_TRUSTED_SIDE,
        "a declared SIDE_FORWARDER of class AUTHORIZED_WRITE: the batch "
        "seating engine reads the game's existing rows to avoid double "
        "seating, for the side its public entry points already authorized."),
    ("services/roster_service.py", "durable_game_sides"): (
        ATTRIBUTION_PRIMITIVE,
        "THE durable-attribution authority — `GameRosterEntry.team_side` and "
        "`SubstituteEnrollment.team_id`, never live membership and never the "
        "permanent pointer. Answering 'which side is this row on' for a game "
        "requires reading the game."),
    ("services/roster_service.py", "_cancel_game_locked"): (
        GAME_WIDE_WRITE,
        "cancelling a GAME cancels both sides' enrollments, because the "
        "cancellation is a fact about the game rather than about a side; the "
        "private helper is the transaction-held body reached only from "
        "cancel_game's game-wide command."),
    ("services/setup_service.py", "delete_game"): (
        GAME_WIDE_WRITE,
        "deleting a draft game removes both sides' rows with it, for the "
        "same reason."),
    ("demo.py", "main"): (
        OUT_OF_BAND_TOOLING,
        "the deterministic demo seeder. It prints a summary of the world it "
        "just built; it is not reachable from any request path."),
}

#: The classes detector 3 knows, and the condition behind each is checked in
#: :func:`_two_sided_condition`.
_TWO_SIDED_CLASSES = {
    NARROWS_BY_TRUSTED_SIDE, TWO_SIDED_BY_AUDIENCE_READER, GAME_WIDE_WRITE,
    ATTRIBUTION_PRIMITIVE, IDENTITY_RECOVERY_ONLY, OUT_OF_BAND_TOOLING,
}


def audit_two_sided_store_reads(sources=None):
    """Every function that reaches two-sided game storage, and whether it is
    declared with a class whose condition holds."""
    sources = package_sources() if sources is None else sources
    violations, used = [], set()
    for path, text in sorted(sources.items()):
        for fn, _parent in functions_in(text):
            reads = sorted({_final_name(c.func) for c in _calls_in(fn)}
                           & set(TWO_SIDED_STORE_READS))
            if not reads:
                continue
            key = (path, fn.name)
            entry = TWO_SIDED_READERS.get(key)
            if entry is None:
                violations.append(Violation(
                    "undeclared_two_sided_read", path, fn.lineno, fn.name,
                    f"store.{'/'.join(reads)}(game_id) — rows for BOTH sides "
                    f"of the game, read with no side argument at all",
                    "this is the shape F1 and F3 shipped as ("
                    "`get_roster`/`get_substitutes` returning the whole "
                    "game's rows), and the side-provenance detector cannot "
                    "see it because there is no side argument to classify. "
                    f"Declare ({path!r}, {fn.name!r}) in "
                    "side_provenance.TWO_SIDED_READERS with the class that "
                    "says what this function does with two-sided state; each "
                    "class's condition is machine-checked"))
                continue
            used.add(key)
            klass, _reason = entry
            if klass not in _TWO_SIDED_CLASSES:
                violations.append(Violation(
                    "undeclared_two_sided_read", path, fn.lineno, fn.name,
                    f"an unknown two-sided class {klass!r}",
                    f"use one of {sorted(_TWO_SIDED_CLASSES)}"))
                continue
            for problem in _two_sided_condition(klass, path, fn, sources):
                violations.append(Violation(
                    "two_sided_class_broken", path, fn.lineno, fn.name,
                    problem,
                    "the class this site claims is machine-checked; a site "
                    "that stops matching its class is a violation even "
                    "though it is listed"))
    return violations, used


#: Registries whose members are already enumerated by another detector — the
#: closure ATTRIBUTION_PRIMITIVE and IDENTITY_RECOVERY_ONLY require of every
#: caller.
def _is_enumerated(path, name):
    return (name in PRODUCERS or name in SIDE_FORWARDERS
            or name in ADJUDICATED_READERS
            or (path, name) in TWO_SIDED_READERS)


def _two_sided_condition(klass, path, fn, sources):
    args = [a.arg for a in fn.args.posonlyargs + fn.args.args
            + fn.args.kwonlyargs]
    problems = []
    if klass == NARROWS_BY_TRUSTED_SIDE:
        if fn.name not in PRODUCERS and fn.name not in SIDE_FORWARDERS:
            problems.append(
                f"{fn.name}() claims NARROWS_BY_TRUSTED_SIDE but is neither "
                f"a declared PRODUCER nor a declared SIDE_FORWARDER, so "
                f"nothing gates the side it narrows by or enumerates its "
                f"callers")
        # …and it must REALLY take a side. Membership of a registry is not
        # the check — a function that lost its side parameter narrows by
        # nothing, whatever the registry still says about it.
        elif fn.name in PRODUCERS and PRODUCERS[fn.name][0] not in args:
            problems.append(
                f"{fn.name}() claims NARROWS_BY_TRUSTED_SIDE but its "
                f"signature ({', '.join(args)}) carries no "
                f"{PRODUCERS[fn.name][0]!r} to narrow BY, so it reads both "
                f"sides and answers with both")
    elif klass == TWO_SIDED_BY_AUDIENCE_READER:
        if fn.name not in ADJUDICATED_READERS:
            problems.append(
                f"{fn.name}() claims TWO_SIDED_BY_AUDIENCE_READER but is not "
                f"a declared ADJUDICATED_READER, so no audience decision is "
                f"verified before it answers with two-sided state")
        # THE CHECK THAT MAKES THIS DETECTOR WORK ON A TREE THE REGISTRY WAS
        # NOT WRITTEN FOR, and the reason it catches F1/F3 rather than
        # excusing them: the condition is a property of THIS function's body,
        # not of a name appearing in a table maintained alongside the fix.
        # At the trees where `get_roster`/`get_substitutes`/`get_board`
        # returned the whole game's rows, they consulted no audience at all.
        elif not (_body_calls(fn) & (set(_AUDIENCE_CALLS)
                                     | set(ADJUDICATORS))):
            problems.append(
                f"{fn.name}() claims TWO_SIDED_BY_AUDIENCE_READER but its "
                f"body calls none of {_AUDIENCE_CALLS} and delegates to no "
                f"declared adjudicator, so it reads BOTH sides of the game "
                f"and answers with them unprojected — the shape F1 and F3 "
                f"shipped as")
    elif klass == GAME_WIDE_WRITE:
        if "actor_id" not in args:
            problems.append(
                f"{fn.name}() claims GAME_WIDE_WRITE but takes no "
                f"`actor_id`; a READ of both sides does not get this class")
    elif klass in (ATTRIBUTION_PRIMITIVE, IDENTITY_RECOVERY_ONLY):
        escapes = _unenumerated_callers(sources, fn.name)
        if escapes:
            problems.append(
                f"{fn.name}() claims {klass} — whose whole condition is that "
                f"its two-sided answer never escapes an enumerated chain — "
                f"but it is called from {sorted(escapes)}, which no other "
                f"registry covers")
    elif klass == OUT_OF_BAND_TOOLING:
        # Checked against the IMPORT GRAPH, not against a word search: the
        # transport layer legitimately mentions the string "demo" all over
        # (`/api/demo/overview`, `DEMO_USERS`) without importing `demo.py`.
        # One hop, and stated as such: no serving module may import it.
        importers = sorted(
            other for other in sources
            if (other.startswith(("api/", "web/"))
                and Path(path).stem in _imported_module_names(sources[other])))
        if importers:
            problems.append(
                f"{path} claims OUT_OF_BAND_TOOLING but {', '.join(importers)}"
                f" import(s) it, so a request path may reach it")
    return problems


def _imported_module_names(text):
    """Every module simple-name this source imports."""
    names = set()
    for node in ast.walk(parse(text)):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.update(alias.name.split("."))
        elif isinstance(node, ast.ImportFrom):
            names.update((node.module or "").split("."))
            for alias in node.names:
                names.add(alias.name)
    return names - {""}


def _unenumerated_callers(sources, name):
    """Callers of ``name`` that no registry enumerates. A function nothing "
    calls returns the empty set, which is the honest answer: there is no
    escape."""
    escapes = set()
    for path, text in sorted(sources.items()):
        for fn, parent in functions_in(text):
            for call in _calls_in(fn):
                if _final_name(call.func) != name:
                    continue
                if _is_enumerated(path, fn.name):
                    continue
                if parent is not None and _is_enumerated(path, parent.name):
                    continue
                escapes.add(f"{path}:{fn.name}")
    return escapes


# ---------------------------------------------------------------------------
# Detector 3 — THE TRUSTED SYMBOL IS THE ONLY THING WEARING ITS NAME
#
# Hole B, reported at its source rather than only at the call sites it
# poisons. `_ModuleContext` already refuses to TRUST a binding it cannot
# resolve to the canonical definition; this detector additionally says WHERE
# the forgery is, so the failure names the def rather than the read.
# ---------------------------------------------------------------------------
def audit_trusted_binding(sources=None):
    """Every binding of :data:`TRUSTED_RESOLVER` in the package, and whether
    it is the one canonical definition.

    Exactly ONE module-level ``def`` may carry that name, in
    :data:`TRUSTED_RESOLVER_MODULE`. A nested def, a method, a class, a
    lambda assigned to it, an ``import … as`` and an import from an
    undeclared module are each reported — and a declared re-export must
    really re-export the canonical symbol, so the chain always terminates at
    the one definition."""
    sources = package_sources() if sources is None else sources
    violations = []
    canonical = []
    carriers = []
    for path, text in sorted(sources.items()):
        tree = parse(text)
        ctx = _ModuleContext(path, tree)
        # THE CARRIER IS TRUSTED ONLY BECAUSE IT CALLS THE RESOLVER. Checked
        # here, beside the canonical-definition rule, because it is the same
        # claim: everything downstream calls trusted traces back to ONE
        # function. A carrier defined elsewhere, or one that stopped calling
        # the resolver, is a second answer to "which side is this caller's".
        for fn, parent in functions_in(text):
            if fn.name != TRUSTED_CARRIER or parent is not None:
                continue
            if path != TRUSTED_RESOLVER_MODULE:
                violations.append(Violation(
                    "forged_trusted_carrier", path, fn.lineno, fn.name,
                    f"{TRUSTED_CARRIER} is defined outside "
                    f"{TRUSTED_RESOLVER_MODULE}",
                    f"the carrier the dispatch reads its trusted side off "
                    f"must live beside {TRUSTED_RESOLVER}, so the two "
                    f"cannot be maintained apart"))
                continue
            calls = {_final_name(node.func) for node in ast.walk(fn)
                     if isinstance(node, ast.Call)}
            if TRUSTED_RESOLVER not in calls:
                violations.append(Violation(
                    "forged_trusted_carrier", path, fn.lineno, fn.name,
                    f"{TRUSTED_CARRIER} does not call {TRUSTED_RESOLVER}",
                    f"the dispatch trusts `{TRUSTED_CARRIER_SIDE_FIELD}` off "
                    f"this record BECAUSE the record is built by calling "
                    f"{TRUSTED_RESOLVER}. A carrier that resolves the side "
                    f"some other way is an unchecked second answer"))
                continue
            carriers.append((path, fn.lineno))
        for lineno, what in ctx.forgeries:
            violations.append(Violation(
                "forged_trusted_resolver", path, lineno, "<module>",
                f"this module {what}",
                f"{TRUSTED_RESOLVER} is the ONE resolution everything this "
                f"gate calls TRUSTED traces back to. Import it from "
                f"{TRUSTED_RESOLVER_MODULE} (or a declared re-export: "
                f"{', '.join(TRUSTED_REEXPORTS)}) under its own name, or the "
                f"name stops meaning the canonical symbol"))
        for fn, parent in functions_in(text):
            if fn.name != TRUSTED_RESOLVER:
                continue
            if path == TRUSTED_RESOLVER_MODULE and parent is None \
                    and _is_module_level(tree, fn):
                canonical.append((path, fn.lineno))
                continue
            where = ("nested inside " + parent.name + "()" if parent
                     else "at class scope" if not _is_module_level(tree, fn)
                     else "at module scope")
            violations.append(Violation(
                "forged_trusted_resolver", path, fn.lineno, fn.name,
                f"a SECOND definition of {TRUSTED_RESOLVER}, {where}",
                f"there may be exactly one, module-level, in "
                f"{TRUSTED_RESOLVER_MODULE}. A same-named nested def, method "
                f"or lambda used to be trusted on the strength of its name "
                f"alone — that is the hole this detector closes"))
        for node in ast.walk(tree):
            targets = []
            if isinstance(node, ast.Assign):
                targets = [t for t in node.targets]
            elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
                targets = [node.target]
            elif isinstance(node, ast.NamedExpr):
                targets = [node.target]
            for target in targets:
                if TRUSTED_RESOLVER in _bound_names(target):
                    violations.append(Violation(
                        "forged_trusted_resolver", path, node.lineno,
                        "<assignment>",
                        f"the name {TRUSTED_RESOLVER} is ASSIGNED here",
                        f"the trusted resolution is a function, not a "
                        f"rebindable name. Assigning it — to a lambda, to a "
                        f"method, to anything — makes every call spelled "
                        f"like it mean something else"))
        if path in TRUSTED_REEXPORTS \
                and TRUSTED_RESOLVER not in ctx.trusted_names:
            violations.append(Violation(
                "forged_trusted_resolver", path, 0, "<module>",
                f"declared a TRUSTED_REEXPORT but does not import "
                f"{TRUSTED_RESOLVER} from {TRUSTED_RESOLVER_MODULE}",
                "a re-export that no longer re-exports the canonical symbol "
                "turns every importer of it into an unchecked binding"))
    if len(canonical) != 1:
        violations.append(Violation(
            "forged_trusted_resolver", TRUSTED_RESOLVER_MODULE, 0,
            "<module>",
            f"{len(canonical)} canonical definitions of {TRUSTED_RESOLVER} "
            f"(expected exactly 1): {canonical}",
            "everything this gate calls TRUSTED is trusted because it traces "
            "back to ONE function"))
    if len(carriers) != 1:
        violations.append(Violation(
            "forged_trusted_carrier", TRUSTED_RESOLVER_MODULE, 0,
            "<module>",
            f"{len(carriers)} definitions of {TRUSTED_CARRIER} "
            f"(expected exactly 1): {carriers}",
            f"the private-game dispatch reads admission AND its side off "
            f"ONE carrier record; two carriers are two boundaries again"))
    return violations


def _is_module_level(tree, fn):
    return any(node is fn for node in tree.body)


# ---------------------------------------------------------------------------
# Detector 4 — the private-game dispatch
# ---------------------------------------------------------------------------
def _private_game_block(tree):
    """The ``if sub == …`` chain of ``_dispatch_get``'s private-game family,
    identified by the statement that hoists the trusted resolution."""
    for fn, _parent in _functions(tree):
        if fn.name != "_dispatch_get":
            continue
        for node in ast.walk(fn):
            if not isinstance(node, ast.Assign):
                continue
            if not any(t.id == DISPATCH_TRUSTED_NAME
                       for t in node.targets if isinstance(t, ast.Name)):
                continue
            return fn, node
    return None, None


def audit_dispatch(source=None):
    """The other half of the trust loop: the dispatch really does hoist
    ``game_scoped_own_team_id`` once and hand THAT to every leaf."""
    if source is None:
        source = (PACKAGE_ROOT / "web" / "server.py").read_text()
    path = "web/server.py"
    tree = parse(source)
    ctx = _ModuleContext(path, tree)
    fn, hoist = _private_game_block(tree)
    violations, seen_leaves = [], set()
    if fn is None:
        return [Violation(
            "dispatch_hoist_missing", path, 0, "_dispatch_get",
            f"no assignment to {DISPATCH_TRUSTED_NAME!r} in the dispatch",
            f"the private-game family's side must be resolved ONCE into "
            f"{DISPATCH_TRUSTED_NAME!r} from {TRUSTED_RESOLVER}(…)")], set()

    # (a0) THE CARRIER IS TAKEN, ONCE. `own_team` may be read off a record
    # produced by `TRUSTED_CARRIER` — the ONE resolution that decides
    # admission and the side together (#427 round 2, blocker 1) — so the
    # names bound from that call are collected first, and only THOSE
    # attributes are accepted as trusted below.
    scope = _Scope(fn)
    carrier_names = set()
    for node in ast.walk(fn):
        if not isinstance(node, ast.Assign) \
                or not isinstance(node.value, ast.Call):
            continue
        if _final_name(node.value.func) != TRUSTED_CARRIER:
            continue
        for target in node.targets:
            if isinstance(target, ast.Name):
                carrier_names.add(target.id)
    if not carrier_names:
        violations.append(Violation(
            "dispatch_carrier_missing", path, fn.lineno, "_dispatch_get",
            f"no assignment from {TRUSTED_CARRIER}(…) in the dispatch",
            f"the private-game family's admission and its trusted side must "
            f"be resolved ONCE, together, into a {TRUSTED_CARRIER} record — "
            f"resolving them separately is the disclosure window #427 round "
            f"2 closed"))
    trusted_carrier_origins = {
        f"attr:{name}.{TRUSTED_CARRIER_SIDE_FIELD}" for name in carrier_names}

    # (a) `own_team` is assigned from NOTHING but the trusted resolution —
    # either the resolver itself, or the side field of a carrier record taken
    # from it.
    for node in ast.walk(fn):
        if not isinstance(node, ast.Assign):
            continue
        if not any(isinstance(t, ast.Name) and t.id == DISPATCH_TRUSTED_NAME
                   for t in node.targets):
            continue
        scope = _Scope(fn)
        origins = _origins(node.value, scope, node.lineno + 1, ctx=ctx)
        bad = sorted(o for o in origins
                     if o != f"call:{TRUSTED_RESOLVER}"
                     and o not in trusted_carrier_origins
                     and not o.startswith("const:"))
        if bad:
            violations.append(Violation(
                "dispatch_hoist_untrusted", path, node.lineno, "_dispatch_get",
                f"{DISPATCH_TRUSTED_NAME} assigned from {', '.join(bad)}",
                f"{DISPATCH_TRUSTED_NAME} is the ONE trusted side the whole "
                f"private-game family reads; it must come from "
                f"{TRUSTED_RESOLVER}(…), or from the "
                f"`{TRUSTED_CARRIER_SIDE_FIELD}` field of a "
                f"{TRUSTED_CARRIER}(…) record, and nothing else — every "
                f"`viewer_team_id` the facade trusts is filled from it"))

    # (b) every leaf of the block is declared.
    for node in ast.walk(fn):
        if not isinstance(node, ast.Compare) \
                or not isinstance(node.left, ast.Name) or node.left.id != "sub":
            continue
        for comparator in node.comparators:
            names = ([comparator] if isinstance(comparator, ast.Constant)
                     else getattr(comparator, "elts", []))
            for element in names:
                if not isinstance(element, ast.Constant) \
                        or not isinstance(element.value, str):
                    continue
                leaf = element.value
                seen_leaves.add(leaf)
                if leaf in PRIVATE_GAME_LEAVES:
                    continue
                violations.append(Violation(
                    "undeclared_private_game_leaf", path, element.lineno,
                    "_dispatch_get",
                    f"a new /api/games/{{id}}/{leaf} leaf",
                    "every leaf of this dispatch sits behind ONE "
                    "participation gate that proves the caller belongs to *a* "
                    "team in the game and does NOT bound which side they may "
                    "read. Add it to side_provenance.PRIVATE_GAME_LEAVES "
                    "naming what decides its side, and route it through "
                    "lineup_visibility.route_audience on the hoisted "
                    f"{DISPATCH_TRUSTED_NAME}"))

    # (c) every `viewer_team_id=` in the block is the hoisted name.
    for call in ast.walk(fn):
        if not isinstance(call, ast.Call):
            continue
        for keyword in call.keywords:
            if keyword.arg != "viewer_team_id":
                continue
            if isinstance(keyword.value, ast.Name) \
                    and keyword.value.id == DISPATCH_TRUSTED_NAME:
                continue
            violations.append(Violation(
                "dispatch_untrusted_viewer_team", path, call.lineno,
                "_dispatch_get",
                f"viewer_team_id={ast.unparse(keyword.value)}",
                f"the facade treats `viewer_team_id` as the TRUSTED side; it "
                f"must be the hoisted {DISPATCH_TRUSTED_NAME}, never a query "
                f"string, a body field or a second resolution"))
    return violations, seen_leaves


# ---------------------------------------------------------------------------
# Liveness — the registries cannot rot, and can only shrink
# ---------------------------------------------------------------------------
def _find_function(sources, path, name):
    text = sources.get(path)
    if text is None:
        return None
    for fn, _parent in functions_in(text):
        if fn.name == name:
            return fn
    return None


def _find_anywhere(sources, name):
    """EVERY function with this simple name, across the package.

    Returns a list on purpose. A registry entry that resolves to two
    different functions is an ERROR, not a coin flip: `ApiService.
    add_substitute_to_roster` is a thin facade wrapper around
    `RosterService.add_substitute_to_roster`, and a check that silently
    picked whichever sorted first would verify a condition against the wrong
    body. Callers qualify with a module, or `_resolve_declared` refuses."""
    out = []
    for path, text in sorted(sources.items()):
        for fn, parent in functions_in(text):
            if fn.name == name:
                out.append((path, fn, parent))
    return out


def _resolve_declared(sources, name, module, registry):
    """``(path, fn, parent, errors)`` for one declared name."""
    matches = _find_anywhere(sources, name)
    if module is not None:
        matches = [m for m in matches if m[0] == module]
        if not matches:
            return None, None, None, [
                f"{registry} declares {name!r} in {module}, which has no "
                f"such function."]
    if not matches:
        return None, None, None, [
            f"{registry} declares {name!r}, which no longer exists."]
    if len(matches) > 1:
        where = ", ".join(m[0] for m in matches)
        return None, None, None, [
            f"{registry}[{name!r}] is AMBIGUOUS — {len(matches)} functions "
            f"carry that name ({where}), so the condition behind it would be "
            f"checked against whichever sorted first. Qualify the entry with "
            f"the module it means."]
    path, fn, parent = matches[0]
    return path, fn, parent, []


def verify_registry_liveness(sources=None, usage=None, dispatch_leaves=None):
    """Every declaration still describes the code, and every one is still
    needed. Errors are strings so a failure names all of them at once."""
    sources = package_sources() if sources is None else sources
    errors = []

    # -- producers exist, in the declared module, with the declared side ----
    for producer, (param, index) in sorted(PRODUCERS.items()):
        module = PRODUCER_MODULES[producer]
        fn = _find_function(sources, module, producer)
        if fn is None:
            errors.append(
                f"PRODUCERS declares {producer!r} in {module}, which has no "
                f"such function. A producer that moved or was renamed must be "
                f"re-declared, or every call to it silently stops being "
                f"checked.")
            continue
        args = [a.arg for a in fn.args.posonlyargs + fn.args.args]
        if index >= len(args) or args[index] != param:
            errors.append(
                f"PRODUCERS declares {producer}'s side as parameter "
                f"{param!r} at index {index}; the real signature is "
                f"({', '.join(args)}). The declared index counts `self`.")

    if _find_function(sources, TRUSTED_RESOLVER_MODULE,
                      TRUSTED_RESOLVER) is None:
        errors.append(
            f"the trusted resolution {TRUSTED_RESOLVER}() is not in "
            f"{TRUSTED_RESOLVER_MODULE}. Everything this gate calls TRUSTED "
            f"is trusted because it traces back to that one function.")

    # -- adjudicators really adjudicate -------------------------------------
    for name, module in sorted(ADJUDICATORS.items()):
        fn = _find_function(sources, module, name)
        if fn is None:
            errors.append(f"ADJUDICATORS declares {name!r} in {module}, "
                          f"which has no such function.")
            continue
        if "route_audience" not in _body_calls(fn):
            errors.append(
                f"{name}() is declared an ADJUDICATOR — the one place a "
                f"client-supplied side may be laundered — but its body no "
                f"longer calls lineup_visibility.route_audience, so it "
                f"launders nothing and every call site trusting it is "
                f"unchecked.")

    # -- adjudicated readers really consult the audience on that parameter --
    for name, param in sorted(
            ADJUDICATED_READERS.items(), key=lambda kv: kv[0]):
        path, fn, _parent, problems = _resolve_declared(
            sources, name, ADJUDICATED_READER_MODULES.get(name),
            "ADJUDICATED_READERS")
        if problems:
            errors.extend(problems)
            continue
        calls = _body_calls(fn)
        # The decision may be DELEGATED to a declared adjudicator rather than
        # taken inline: `get_substitute_candidates` and `get_addable_
        # substitutes` both hand the hint and the trusted side to
        # `_workflow_side`, which is separately verified to consult
        # `route_audience`. Accepting the delegation is not a loosening --
        # the adjudicator's own condition is what makes it sound.
        delegated = bool(calls & set(ADJUDICATORS))
        if not calls & set(_AUDIENCE_CALLS) and not delegated:
            errors.append(
                f"{name}() is declared an ADJUDICATED READER but its body "
                f"calls none of {_AUDIENCE_CALLS} and delegates to no "
                f"declared adjudicator — so nothing decides which side it "
                f"may answer for, and its {param!r} parameter is not "
                f"trustworthy.")
            continue
        if param is None:
            continue
        if delegated and _passes_to_adjudicator(fn, param):
            continue
        if not _passes_as_viewer_side(fn, param):
            errors.append(
                f"{name}() is declared to carry the trusted side in "
                f"{param!r}, but that parameter is never passed to "
                f"{'/'.join(_AUDIENCE_CALLS)} as the viewer's side. A "
                f"parameter this gate treats as TRUSTED has to be the one "
                f"the audience decision is actually taken on.")

    # -- forwarder classes: each condition, per function --------------------
    for name, (klass, _note) in sorted(SIDE_FORWARDERS.items()):
        if klass not in _FORWARDER_CONDITIONS:
            errors.append(f"SIDE_FORWARDERS[{name!r}] names an unknown class "
                          f"{klass!r}.")
            continue
        path, fn, parent, problems = _resolve_declared(
            sources, name, SIDE_FORWARDER_MODULES.get(name),
            "SIDE_FORWARDERS")
        if problems:
            errors.extend(problems)
            continue
        errors.extend(_class_condition(klass, name, fn, parent, sources,
                                       origins=set(), route=None))

    # -- exemption classes: each condition, per site ------------------------
    for (name, producer, _fingerprint), (klass, route, _reason) in sorted(
            EXEMPTIONS.items()):
        if klass not in _FORWARDER_CONDITIONS:
            errors.append(f"EXEMPTIONS[{(name, producer)!r}] names an unknown "
                          f"class {klass!r}.")
            continue
        origins = set(_fingerprint.split("|")) if _fingerprint else set()
        path, fn, parent, problems = _resolve_declared(
            sources, name, EXEMPTION_MODULES.get(name), "EXEMPTIONS")
        if problems:
            errors.extend(problems)
            continue
        errors.extend(_class_condition(klass, name, fn, parent, sources,
                                       origins=origins, route=route))

    # -- live-membership readers still read live membership -----------------
    for name, (module, _reason) in sorted(LIVE_MEMBERSHIP_READERS.items()):
        _path, fn, _parent, problems = _resolve_declared(
            sources, name, module, "LIVE_MEMBERSHIP_READERS")
        if problems:
            errors.extend(problems)
            continue
        if not _body_calls(fn) & set(_MEMBERSHIP_RESOLVERS):
            errors.append(
                f"{name}() is recorded LIVE_MEMBERSHIP_BY_DESIGN but no "
                f"longer resolves live membership "
                f"({'/'.join(sorted(_MEMBERSHIP_RESOLVERS))}), so the record "
                f"no longer describes it. If it moved to durable "
                f"attribution, delete the record.")

    # -- DORMANT entries: the monotonic-shrink half -------------------------
    if usage is not None:
        for name in sorted(set(SIDE_FORWARDERS) - usage["forwarders"]):
            errors.append(
                f"SIDE_FORWARDERS[{name!r}] is DORMANT: no producer call in "
                f"the package forwards a side through it any more. Delete "
                f"the entry — a declaration nothing depends on is a "
                f"declaration nobody will notice going stale.")
        for key in sorted(set(EXEMPTIONS) - usage["exemptions"]):
            errors.append(
                f"EXEMPTIONS[{key!r}] is DORMANT: that call site no longer "
                f"exists or no longer needs an exemption. Delete the entry.")
        for key in sorted(set(LEDGER) - usage["ledger"]):
            errors.append(
                f"LEDGER[{key!r}] is DORMANT: the site it accepted is gone. "
                f"Delete the entry — this ledger may only shrink.")
    if usage is not None and "two_sided" in usage:
        for key in sorted(set(TWO_SIDED_READERS) - usage["two_sided"]):
            errors.append(
                f"TWO_SIDED_READERS[{key!r}] is DORMANT: that function no "
                f"longer reaches two-sided game storage, or no longer "
                f"exists. Delete the entry — this registry may only shrink.")
    if usage is not None and "home_fallbacks" in usage:
        for key in sorted(set(HOME_FALLBACKS) - usage["home_fallbacks"]):
            errors.append(
                f"HOME_FALLBACKS[{key!r}] is DORMANT: that home default is "
                f"gone. Delete the entry — this ledger may only shrink.")
    if dispatch_leaves is not None:
        for leaf in sorted(set(PRIVATE_GAME_LEAVES) - dispatch_leaves):
            errors.append(
                f"PRIVATE_GAME_LEAVES[{leaf!r}] is DORMANT: the dispatch has "
                f"no such leaf any more. Delete the entry.")
    return errors


def _passes_as_viewer_side(fn, param):
    """Is ``param`` handed to an audience helper as the VIEWER's side?

    Positional index 1 for ``route_audience``/``side_projections``/
    ``own_side`` — all three take ``(role, viewer_team_id, home, away)`` — or
    the ``viewer_team_id`` keyword."""
    for node in ast.walk(fn):
        if not isinstance(node, ast.Call):
            continue
        if _final_name(node.func) not in _AUDIENCE_CALLS:
            continue
        for keyword in node.keywords:
            if keyword.arg == "viewer_team_id" \
                    and isinstance(keyword.value, ast.Name) \
                    and keyword.value.id == param:
                return True
        if len(node.args) > 1 and isinstance(node.args[1], ast.Name) \
                and node.args[1].id == param:
            return True
    return False


def _class_condition(klass, name, fn, parent, sources, origins, route):
    """The machine-checked condition behind one class. Returns error strings.

    THIS is what stops a class being a rubber stamp: a site cannot claim
    SUBJECT_MEMBERSHIP_CONTEXT without resolving the subject's side for THIS
    game, or AUTHORIZED_WRITE without an authorization argument, or
    OPERATOR_ONLY_ROUTE on a route the registry does not record as
    operator-only.

    The first example used to read ``SUBJECT_OWN_SIDE``, which is exactly the
    class this module removed: its only machine condition was "the function
    accepts no caller-supplied side", which proves the client did not NAME a
    side and says nothing about whether the side is AUTHORIZED -- and it
    blessed the player-home leak on that basis. Citing it here as a live
    example of a condition that bites would have been the same overclaim in
    miniature."""
    args = [a.arg for a in fn.args.posonlyargs + fn.args.args
            + fn.args.kwonlyargs]
    errors = []
    if klass == PRODUCER_INTERNAL:
        if name not in PRODUCERS:
            errors.append(
                f"{name}() claims PRODUCER_INTERNAL but is not in PRODUCERS, "
                f"so nothing gates its own callers.")
    elif klass == ADJUDICATED:
        if name not in ADJUDICATED_READERS:
            errors.append(
                f"{name}() claims ADJUDICATED but is not in "
                f"ADJUDICATED_READERS, so no audience decision is verified.")
    elif klass == AUTHORIZED_WRITE:
        if "authorized_team_id" not in args and not _reaches_authorized(fn) \
                and not _callers_are_authorized(sources, name):
            errors.append(
                f"{name}() claims AUTHORIZED_WRITE but takes no "
                f"`authorized_team_id`, never authorizes its side "
                f"({'/'.join(_SIDE_AUTHORIZERS)}), and has a caller that "
                f"does neither. A write that takes a side and no "
                f"authorization anywhere on the path into it does not get "
                f"this class.")
    elif klass == SUBJECT_MEMBERSHIP_CONTEXT:
        offered = [a for a in args
                   if a in ("team_id", "side", "team_side", "viewer_team_id")]
        if offered:
            errors.append(
                f"{name}() claims SUBJECT_MEMBERSHIP_CONTEXT but accepts a "
                f"caller-supplied side ({', '.join(offered)}).")
        if origins and not all(o in _SUBJECT_CONTEXT_ORIGINS
                               for o in origins):
            errors.append(
                f"{name}() claims SUBJECT_MEMBERSHIP_CONTEXT but its side "
                f"comes from {sorted(origins)}, and only "
                f"{', '.join(_SUBJECT_CONTEXT_ORIGINS)} is a resolved "
                f"membership context's own side. This is the check the "
                f"fifth leak needed: `attr:player.team_id` is the permanent "
                f"pointer, not the game-scoped membership, and the old "
                f"SUBJECT_OWN_SIDE class could not tell them apart.")
        if not _body_calls(fn) & set(_MEMBERSHIP_RESOLVERS):
            errors.append(
                f"{name}() claims SUBJECT_MEMBERSHIP_CONTEXT but its body "
                f"never resolves a membership context "
                f"({'/'.join(sorted(_MEMBERSHIP_RESOLVERS))}), so the "
                f"context it reads was handed in whole rather than resolved "
                f"for the subject here.")
        if not ({"player_id", "player"} & set(args)):
            errors.append(
                f"{name}() claims SUBJECT_MEMBERSHIP_CONTEXT but names no "
                f"SUBJECT (`player_id`/`player`), so there is nothing for "
                f"the side to be the subject's own side OF.")
    elif klass == DURABLE_ROW_SIDE:
        if not any(o in _DURABLE_ATTRIBUTION_ORIGINS for o in origins) \
                and origins:
            errors.append(
                f"{name}() claims DURABLE_ROW_SIDE but its side comes from "
                f"{sorted(origins)}, none of which is a durable attribution "
                f"field ({', '.join(_DURABLE_ATTRIBUTION_ORIGINS)}).")
    elif klass == BOTH_SIDES_BY_AUDIENCE:
        if not _only_called_from_adjudicated(sources, name):
            errors.append(
                f"{name}() claims BOTH_SIDES_BY_AUDIENCE, but it is called "
                f"from somewhere that is not a declared adjudicated reader — "
                f"so 'the audience was already chosen' is not true of every "
                f"path into it.")
    elif klass == OPERATOR_ONLY_ROUTE:
        errors.extend(_route_is_operator_only(route, name))
    elif klass == OPERATOR_DEFAULT:
        if name not in ADJUDICATED_READERS and name not in PRODUCERS:
            errors.append(
                f"{name}() claims OPERATOR_DEFAULT but is neither a declared "
                f"adjudicated reader nor a producer. A function that never "
                f"consults the audience cannot claim the caller may read "
                f"either side.")
    elif klass == LIVE_MEMBERSHIP_BY_DESIGN:
        if not _body_calls(fn) & set(_MEMBERSHIP_RESOLVERS) \
                and not _reaches_membership(sources, fn):
            errors.append(
                f"{name}() is recorded LIVE_MEMBERSHIP_BY_DESIGN but no "
                f"longer resolves live membership, so the record no longer "
                f"describes it.")
    return errors


def _passes_to_adjudicator(fn, param):
    """Is ``param`` handed to a declared adjudicator as the trusted side?

    Keyword `viewer_team_id`, or the adjudicator's own declared position.
    `_workflow_side(game, team_id, viewer_role, viewer_team_id, refusal)`
    puts the TRUSTED side at index 4 counting `self`, so index 3 of a bound
    call's positional arguments."""
    for node in ast.walk(fn):
        if not isinstance(node, ast.Call):
            continue
        if _final_name(node.func) not in ADJUDICATORS:
            continue
        for keyword in node.keywords:
            if keyword.arg == "viewer_team_id" \
                    and isinstance(keyword.value, ast.Name) \
                    and keyword.value.id == param:
                return True
        for arg in node.args:
            if isinstance(arg, ast.Name) and arg.id == param:
                return True
    return False


def _callers_are_authorized(sources, name):
    """Is EVERY caller of ``name`` itself an authorized write?

    One hop, and every caller must qualify — an internal helper of a write
    path inherits its callers' authorization only when they ALL carry it.
    A helper nothing calls does not qualify: an unreachable claim is not a
    proof."""
    found_any = False
    for _path, text in sorted(sources.items()):
        for fn, _parent in functions_in(text):
            for call in _calls_in(fn):
                if _final_name(call.func) != name:
                    continue
                found_any = True
                args = [a.arg for a in fn.args.posonlyargs + fn.args.args
                        + fn.args.kwonlyargs]
                if "authorized_team_id" in args or _reaches_authorized(fn):
                    continue
                if SIDE_FORWARDERS.get(fn.name, (None,))[0] == AUTHORIZED_WRITE:
                    continue
                return False
    return found_any


#: The two ways a WRITE's side is authorized in this codebase, and there are
#: exactly two: the caller's explicit `authorized_team_id` is revalidated
#: against the requested side, or the side is re-resolved from the SUBJECT's
#: own membership context by the transition's own gate (a player self-service
#: path has no `authorized_team_id` — the row and the subject decide).
_SIDE_AUTHORIZERS = ("_require_authorized_team", "_require_membership_context",
                     "_authorize_seated_side")


def _reaches_authorized(fn):
    return bool(_body_calls(fn) & set(_SIDE_AUTHORIZERS))


def _reaches_membership(sources, fn):
    """A LIVE_MEMBERSHIP reader may resolve membership one call away — via a
    declared producer it calls. Bounded to one hop on purpose: an unbounded
    search would accept anything."""
    for callee in _body_calls(fn):
        if callee in PRODUCERS:
            inner = _find_function(sources, PRODUCER_MODULES[callee], callee)
            if inner is not None \
                    and _body_calls(inner) & set(_MEMBERSHIP_RESOLVERS):
                return True
        if callee in _MEMBERSHIP_RESOLVERS:
            return True
    return False


def _only_called_from_adjudicated(sources, name):
    found_any = False
    for _path, text in sorted(sources.items()):
        for fn, parent in functions_in(text):
            for call in _calls_in(fn):
                if _final_name(call.func) != name:
                    continue
                found_any = True
                holder = fn.name
                if holder in ADJUDICATED_READERS:
                    continue
                if parent is not None and parent.name in ADJUDICATED_READERS:
                    continue
                return False
    return found_any


def _route_is_operator_only(route, name):
    if not route:
        return [f"{name}() claims OPERATOR_ONLY_ROUTE but names no route to "
                f"check it against."]
    from ..web import route_registry
    for spec in route_registry.REGISTRY:
        if spec.name == route:
            if spec.auth != "operator_only":
                return [f"{name}() claims OPERATOR_ONLY_ROUTE via route "
                        f"{route!r}, which the registry records as "
                        f"auth={spec.auth!r}. Loosening that route's auth "
                        f"widens this private read; re-classify the site."]
            return []
    return [f"{name}() claims OPERATOR_ONLY_ROUTE via route {route!r}, which "
            f"is not in web/route_registry.REGISTRY."]


# ---------------------------------------------------------------------------
# The whole gate
# ---------------------------------------------------------------------------
def audit(sources=None, server_source=None, verify_liveness=None):
    """``(violations, errors)`` for the whole rule.

    ``sources``/``server_source`` of ``None`` mean the REAL package, which is
    also what turns liveness verification on — a synthetic fixture
    legitimately consults none of the real registries, exactly as
    ``route_extract.extract_routes`` gates ``verify_waiver_usage``."""
    real = sources is None and server_source is None
    if verify_liveness is None:
        verify_liveness = real
    sources = package_sources() if sources is None else sources
    provenance, usage = audit_side_provenance(sources)
    fallbacks, used_fallbacks = audit_home_fallback(sources)
    usage["home_fallbacks"] = used_fallbacks
    two_sided, used_two_sided = audit_two_sided_store_reads(sources)
    usage["two_sided"] = used_two_sided
    binding = audit_trusted_binding(sources)
    dispatch, leaves = audit_dispatch(
        server_source if server_source is not None
        else sources.get("web/server.py"))
    violations = provenance + fallbacks + binding + two_sided + dispatch
    errors = (verify_registry_liveness(sources, usage, leaves)
              if verify_liveness else [])
    return violations, errors


def report(violations, errors):  # pragma: no cover - failure text only
    lines = []
    if violations:
        lines.append(f"{len(violations)} private-state read(s) reached a side "
                     f"without the server's trusted resolution:\n")
        lines.extend(str(v) for v in violations)
    if errors:
        lines.append(f"\n{len(errors)} side_provenance registry error(s):\n")
        lines.extend(f"  - {e}" for e in errors)
    return "\n".join(lines)
