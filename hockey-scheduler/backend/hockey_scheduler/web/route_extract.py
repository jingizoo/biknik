"""Derive the LIVE route set from ``web/server.py`` by PARSING it (#202 step 1).

Why this exists
---------------
``server.py`` dispatches with hand-written ``if`` chains, and three separate
hand-maintained tables (``_GET_ROUTES``, ``_POST_ROUTES``,
``CONTEXT_SCOPED_READ_ROUTES``) transcribed parts of that dispatch. A registry
checked against another hand-written list proves nothing: both are prose, both
drift, and they drift silently the moment someone adds a branch. So the
inventory's counterpart is the DISPATCH ITSELF, read out of the source with
``ast``.

(#202 wiring step: ``_GET_ROUTES``/``_POST_ROUTES`` stopped being a second
hand-maintained table -- server.py now derives them from ``route_registry.py``
directly, closing the loop this module's own output feeds. That is precisely
what this paragraph's original problem statement predicts should happen once
the inventory this module produces is trustworthy enough to build on:
``CONTEXT_SCOPED_READ_ROUTES`` is the one still hand-transcribed, separately,
today.)

What "live route" means here
----------------------------
One entry per REACHABLE (method, path) the dispatch actually admits (an
effective leaf, not merely a syntactic branch -- see the #202 repair note
below), expressed as a canonical TEMPLATE:

    ``/api/games/{}/board``      ``{}``  = one path segment matching ``[^/]+``
    ``/api/setup/{w}``           ``{w}`` = one path segment matching ``\\w+``
                                 (narrower than ``{}`` -- excludes ``.``, ``-``,
                                 etc -- and NEVER identified with it)
    ``/api/games/{}/{*}``        ``{*}`` = a free, NON-EMPTY tail (``.+``)
    ``/api/{*0}``                ``{*0}`` = a free, POSSIBLY-EMPTY tail
                                 (``.*``, or a ``startswith()`` prefix route --
                                 NEVER identified with ``{*}``)

The template is the identity used to compare the dispatch with the registry,
because it is derivable from BOTH sides: from the source by this module, and
from a ``RouteSpec.pattern`` by :func:`templates_of_pattern`.

Branch shapes understood (every shape present in server.py today):

===============================  ==================================================
``path == "/api/players"``       exact literal
``path in ("/a", "/b")``         several exact literals
``m = re.match(rx, path)``       regex, expanded over its alternations/optionals
``+ if m:``
``path.startswith("/api/x/")``   prefix — either a DELEGATION (the body calls
                                 ``self._handle_x(path[len(prefix):], ...)``, and
                                 the callee's branches are walked with that
                                 prefix) or a real prefix route
``entity == "league"``           a sub-dispatch on the delegated tail
``action == "roster/select"``    a sub-dispatch on a captured group
``sub is None``                  the "no subpath" arm of an optional group
``{...}.get(action)`` + ``if x:``  a dict-keyed sub-dispatch
``path.endswith("/preview")``    a refinement of an already-selected literal set
``self._handle_x(m.group(1), …)``  a PARSED_DELEGATES call: each argument binds
                                 to the callee's parameter (see
                                 :meth:`_DispatchWalker._delegate_parsed`)
``combo = (entity, target)``     a tuple of tracked names bound together
``combo in SCHEMA`` / ``SCHEMA.get(combo)`` / ``SCHEMA[combo]``  -- SCHEMA a
                                 literal dict keyed on same-arity tuples: each
                                 key enumerates ONE concrete leaf (see
                                 :meth:`_DispatchWalker._tuple_dict_outcome`) --
                                 this is what makes an effective leaf set,
                                 not just a syntactic one: a wildcard capture
                                 the CALLEE narrows via such a table is not
                                 itself a route once the callee is walked
if/elif/.../else over literal(-set) tests whose TERMINAL else re-derives a
                                 new value from the SAME subject -- an
                                 implicit wildcard (the unconditional static
                                 tail; see :meth:`_DispatchWalker._walk_terminal_else`)
===============================  ==================================================

Fail LOUD, never quiet
----------------------
The point of a derived inventory is that it cannot silently miss a branch, so
every unknown shape raises :class:`ExtractionError` instead of being skipped:

* an unsupported regex construct in a dispatch pattern, including a TOP-LEVEL
  (ungrouped) alternation, which is ambiguous under ``^``/``$`` precedence
  rather than silently accepted with a loose reading;
* an ``if`` test — INCLUDING a ternary (``ast.IfExp``) and an ``any()``/
  ``all()`` over a generator/comprehension, not only a bare ``ast.If`` — or a
  ``match``/``case`` statement, that touches a path-bearing name (the request
  path — even read directly as ``self.path``, bypassing any local — a
  delegated tail, a captured group, a tuple of tracked names, or a
  ``re.match``-family result) in any way this module does not recognise —
  see :meth:`_DispatchWalker._audit_function`;
* a call into a ``_handle_*``/``_dispatch_*`` method that is neither walked nor
  listed as a terminal, or a PARSED_DELEGATES call reached in a form the
  walker does not follow;
* every ``do_<VERB>`` method NOT walked as a dispatch entry point must be
  PROVABLY INERT -- its whole body compared, statement for statement, against
  one of a small number of declared-safe shapes (see
  :meth:`_DispatchWalker._require_safe_verb_shape`) -- unconditionally, not
  dependent on any variable's name.

A missed branch would show up as a route with no ``RouteSpec``; a shape this
module cannot read shows up as a hard error. Both fail CI; neither passes
quietly.

Waivers (``_AUDIT_WAIVERS``) are the one declared escape hatch: a test that
DOES touch a tracked name but is proven, on review, not to be a routing
decision. Each is keyed on the exact function name and unparsed test text (a
drifted test no longer matches and raises again), and each must be an
EXACT-ONE-HIT: consulted by the audit for precisely the one line it names,
never zero times (dormant -- proof nothing depends on it) and never more than
once (too broad to trust).

KNOWN LIMITATIONS
------------------
#202 repair round 3, finding H (documented here deliberately, NOT fixed this
round -- see ``tests/test_route_extract.py``'s ``DecoratorLimitationGuardTests``
for the standing proof it stays true):

* :attr:`_DispatchWalker.functions` only ever harvests ``ast.FunctionDef``
  nodes that are LEXICALLY inside ``class Handler`` in ``server.py``'s own
  source (see :meth:`_DispatchWalker.__init__`), and :func:`extract_routes`
  walks exactly the two named in :data:`ENTRY_POINTS` (``do_GET``/
  ``do_POST``) as its dispatch roots. A ``@decorator`` wrapping either of
  those two methods with routing logic of its OWN -- implemented as a
  separate, module-level function the decorator applies at class-body
  execution time -- would never be walked: this module has no concept of
  "resolve what a decorator does to the function it wraps", only "read the
  function's own body". Any route such a decorator selected would be
  reachable over real HTTP while being invisible to this entire inventory,
  with no error of any kind -- the one outcome every OTHER check in this
  module exists to prevent, reopened through a shape none of them inspects.
* NOT exploitable against the real ``server.py`` today: a straightforward
  ``grep`` for a decorator on either entry point finds none (both are
  reached as plain, undecorated methods), and
  ``tests/test_route_extract.py``'s ``DecoratorLimitationGuardTests`` asserts
  this directly -- parses the real file, and fails LOUDLY the moment either
  method's ``decorator_list`` stops being empty, rather than staying silent
  while the gap this section describes quietly goes live. This is
  architectural and latent, not a live hole today.
* What would close it: walk ``ast.FunctionDef.decorator_list`` for
  ``do_GET``/``do_POST`` and either (a) refuse ANY decorator on either
  entry point outright -- the simplest fail-closed choice, since this
  module cannot generally reason about what an arbitrary decorator does --
  or (b) resolve a known, reviewed allowlist of decorator shapes (e.g. a
  bare ``@functools.wraps``-style pass-through) and continue to refuse
  everything else. Neither is implemented; the guard test above is the
  tripwire that would force the choice to be made the day it first matters.

#202 repair round 5, finding 6c (documented here deliberately, PARTIALLY
fixed this round -- see ``tests/test_route_extract.py``'s
``ExceptionDrivenRoutingTests`` for both the fix and the standing proof of
exactly how far it goes):

* TRUE cross-statement data-flow -- proving, for a GIVEN ``except ... as
  name:`` handler, WHICH specific ``raise`` site (if any) its ``name`` binds
  the payload of -- is out of reach for this module's kind of walker without
  a much larger rework (real type/control-flow analysis, not an AST shape
  scan). This module does not claim to do it, and finding 6c's own fix is
  DELIBERATELY not an attempt at it.
* What IS implemented: (a) an ``ast.Assert``'s own ``.test`` is audited the
  same way an ``ast.If``'s is -- a path-tainted assert is a routing
  decision, full stop, the same shape as every other branching statement
  this module already inspects; (b) an ``ast.Raise``'s own exception
  argument is audited through the SAME unlisted-call/tracked-expression
  rules ``_propagates_taint`` already applies to a Return/bare-Expr value;
  (c) for ``except ... as name:`` SPECIFICALLY, a COARSE, DELIBERATELY
  OVER-INCLUSIVE stand-in: the handler is flagged the moment its ENCLOSING
  FUNCTION contains ANY raise whose own argument mentions a tracked name
  ANYWHERE in that function, regardless of the handler's own declared
  exception type(s) or where in the function the raise sits relative to the
  handler. This is intentionally NOT "does this handler's type match that
  raise's type" -- inheritance, aliasing, and multi-type ``except (A, B) as
  name:`` tuples all make simple textual type-matching UNRELIABLE in
  exactly the direction this module must never guess in (a false "no
  match" would silently miss a live handler); a function-wide
  over-approximation cannot make that mistake, at the cost of also
  flagging a named handler that turns out, on review, to be genuinely
  unconnected to the tracked raise elsewhere in the same function --
  reviewable via the SAME ``_AUDIT_WAIVERS`` escape hatch as everything
  else in this module, not a silent pass.
* Precisely what is STILL NOT covered, honestly: an except handler that
  binds a tracked exception payload to a name is caught (that is finding
  6c's whole point), but what that name is SUBSEQUENTLY tested against, or
  passed to, is examined by NONE of this module's existing mechanisms --
  ``except ... as name:`` is still absent from
  :func:`_binding_value_and_targets`'s recognised binding forms (a
  DELIBERATE choice: teaching the fixed-point loop to treat an exception
  alias as an ordinary tracked local would need to know it is BOUND FROM
  something tracked in the first place, which is exactly the cross-
  statement trace this bullet's first point says is out of reach) -- the
  handler being flagged AT ALL is what stands in for that, not a precise
  model of what happens after. A raise/except pair with NO name binding
  (``except ValueError:``) is unaffected by 6c (nothing to leak through),
  though the RAISE's own argument is still independently audited by 6b.

#202 repair round 11, finding B (documented here deliberately, NOT fixed
this round -- see ``tests/test_route_extract.py``'s
``CapturedArgumentProvenanceTests.test_the_real_api_facade_exposes_no_callable_shaped_signature``
for the standing tripwire):

* PROVENANCE ALONE does not stop a provably-real ``api = STATE.api``
  from being handed to a hypothetical FUTURE higher-order method on the
  real facade. Round 10's fix (extended by round 11's own finding A to
  also cover ``STATE`` itself) proves a name really IS bound from the
  reviewed source expression -- but once that proof holds,
  :func:`_captured_arg_safe_callee` trusts the name's WHOLE surface
  (CLAUDE.md's own layering guarantee, see
  ``_CAPTURED_ARG_SAFE_CALLEE_ROOTS``'s own docstring), not a per-method
  allowlist. CONFIRMED still exempted, fresh against this round's own
  fixed code: ``api = STATE.api`` followed directly by ``api.invoke(
  api.get_item, action)`` raises nothing, the identical shape a genuine
  future callback-taking method on the real facade would need to hide
  routing/policy behaviour behind (see :func:`_captured_arg_safe_callee`'s
  own docstring for why this module cannot generally tell such a method
  apart from any other ``api.<method>()`` call without running the
  program).
* NOT exploitable against the real ``server.py`` today: independently
  RE-confirmed fresh for this round (not copied forward from round 10)
  by walking every PUBLIC method on the real ``ApiService`` and the 9
  sub-facade classes it actually constructs (``AccountService``,
  ``ContextService``, ``DeliveryLoop``, ``DeliveryWorker``,
  ``FactoryResetService``, ``GuardianService``, ``InMemoryStore``,
  ``RosterService``, ``SetupService`` -- discovered from a real instance,
  never hand-listed) -- none declares a ``Callable``-typed parameter or
  return value, the shape a genuine ``api.invoke(fn, target)`` would
  need. ``test_the_real_api_facade_exposes_no_callable_shaped_signature``
  asserts this directly and fails LOUDLY the moment a real callback-typed
  signature is added anywhere on the facade, rather than staying silent
  while the gap this section describes quietly goes live. This is
  architectural and latent, not a live hole today.
* What would close it, and why it is NOT implemented: an exhaustive
  per-method allowlist was tried first and rejected -- the real facade
  has 174 distinct ``api.<attr>`` names referenced in server.py today
  (confirmed by direct AST count, not asserted from memory), and a
  NAME-based heuristic (flagging methods merely SPELLED ``invoke``/
  ``dispatch``/``execute``/...) produces real false positives
  (``FactoryResetService.execute``, a plain domain operation -- wipe the
  database given credentials -- that only happens to share a name with
  the pattern) -- exactly the "spelling is not the same as provenance"
  lesson finding A closes, one level deeper, so the SAME discipline that
  rejects a spelling-based fix for finding A rejects one here too. What
  stands in its place is a CONTRACT test pinning today's architecture,
  not a static-analysis restriction: it fails the moment a real
  ``Callable``-typed parameter or return value is added anywhere on the
  facade, forcing exactly the individual review a NEW allowlist ROOT
  name already requires (see ``_CAPTURED_ARG_SAFE_CALLEE_ROOTS``'s own
  docstring) -- a continuously-verified monitoring/regression backstop,
  not something that closes this gap by construction in
  :mod:`route_extract` itself.

#202 repair round 12, finding 1 -- CLOSED by round 13, finding 2 (was
documented here as NOT fixed; kept only as a pointer for anyone who reads
an old copy of this section, not as an open item):

* Round 11's own finding A bounded itself explicitly to "is ``STATE``
  shadowed here", not to chasing ``STATE``'s own module-level definition
  further -- and, within that narrower scope, round 12 found the fix was
  not yet complete: :func:`_name_rebinding_sites` enumerated the SPELLINGS
  Python's grammar uses for a binding -- an ``ast.Name`` in Store/Del
  context, an ``ast.arg``, an ``ExceptHandler.name``, a ``global``/
  ``nonlocal`` declaration, an ``import ... as`` alias -- but not the two
  binding forms Python's structural pattern matching adds: a bare capture
  pattern (``case STATE:``, ``ast.MatchAs``) and a mapping-rest capture
  (``case {**STATE}:``, ``ast.MatchMapping``). Round 12 documented this
  rather than fixing it, on the strength of "server.py has no ``match``
  statement today" -- round 13's own review rejected that reasoning: the
  ORIGINAL round-12 tripwire test exercised the gap with the capture
  textually BEFORE the trusted ``api = STATE.api`` read, an ordering that
  is not merely unexploited today but literally cannot execute (Python's
  own per-function scoping makes ``STATE`` local to the WHOLE function
  the moment ANY statement in it binds ``STATE``, capture included, so
  that ordering raises ``UnboundLocalError`` before the trusted read is
  ever reached) -- the "not exploitable today" claim rested on a repro
  that could not run, not on the underlying Python semantics being safe.
  See :func:`_name_rebinding_sites`'s own docstring for the fix and
  ``CapturedArgumentProvenanceTests``'s match-capture cases
  (test_route_extract.py) for the corrected, EXECUTABLE (capture-before-
  read) repro, proven both statically and over real HTTP.

On the soundness of this gate, honestly stated
------------------------------------------------
This module has been adversarially reviewed across THIRTEEN rounds: the
original repository-owner review (6 findings, all closed by the #202
repair), a first self-directed adversarial hunt (findings A-D, round 2), a
second (findings E-H, round 3 -- E/F/G closed by this section's own
revision, H documented directly above), a THIRD external repository-owner
review (round 4: 5 findings -- a helper-call/match-case call escape, more
unmodelled binding forms, waiver-relocation, gated-but-unenforced
classification, and three mislabelled auth routes -- all closed), a
FOURTH (round 5: 6 findings across two further external review passes at
the SAME exact head -- a taint-erasing waiver architecture flaw, a
Subscript-callee/Return-statement gap, test-coverage-only, a stale
docstring, an unrecognised-expression-operand gap, and the exception-
driven routing this very section documents -- all closed or, for finding
6c specifically, honestly bounded rather than claimed complete, directly
above), a FIFTH (round 6: 4 findings -- non-compositional expression
taint, a still-fail-open execution-control inventory, a worker-poisoning
test contract, and incomplete documentation -- all closed), a SIXTH
(round 7: 3 findings -- loop execution control and receiver-chain
dispatch selection both fail-open, continued production-database fixture
leakage, and a silently-swallowed restore-side failure -- all closed), a
SEVENTH (round 8: 1 finding -- ``_is_callee``'s curated upward climb
missed a Tuple/List/IfExp wrapper between a captured selector and its
eventual invocation -- closed by replacing the curated climb with a
generic downward scan), and an EIGHTH (round 9: 1 finding -- the SAME
``captured``-only exemption escaped a THIRD way, an arbitrary higher-
order call invoking a captured selector handed to it as a plain
argument, e.g. ``invoke(handlers.get(action, default_handler), self)``
-- closed not by teaching ``_is_callee`` a fourth shape but by inverting
the exemption's own default: a captured value handed to a call is inert
only when the call target is on a small, explicit, reviewed allowlist,
see :func:`_captured_arg_safe_callee`), and a NINTH (round 10: 1 finding
-- a genuinely DIFFERENT category from any of rounds 6-9's own transfer-
shape findings: not WHAT shape defeats the allowlist, but WHO the
allowlist actually trusts. Round 9's allowlist authenticated the
SPELLING ``"api"``, accepting every attribute chain rooted at
``ast.Name("api")`` regardless of what that name was actually bound to
-- ``api = evil_api`` (a local reassignment, or a parameter of the same
name) inherited the identical trust as the one reviewed module-level
facade, DEMONSTRATED live over real HTTP with the SAME "static stays
silent, live diverges 200/404" proof every prior round's finding
required -- closed not by widening or re-shaping the allowlist but by
tying it to PROVEN PROVENANCE: a name earns the exemption, in a GIVEN
function, only when that function's own body proves it is bound EXACTLY
once, at a dominating, top-level, never-rebound ``name = STATE.api``
assignment, the SAME "is this name really what it claims to be"
discipline :func:`_is_self_call`/:func:`_is_self_path` already
established for ``self`` -- see :func:`_captured_arg_trusted_roots`),
and a TENTH (round 11: 2 findings from that round's own independent
verify track. Finding A: round 10's provenance check proved WHO binds
``api``, but never asked the same question of ``STATE`` -- the free
variable embedded in the trusted RHS text ``STATE.api`` itself -- so a
same-spelled parameter or a preceding local reassignment of ``STATE``
inherited the exemption exactly as a shadowed ``api`` did before round
10, the identical spelling-not-provenance bug recurring one level up
the same expression; DEMONSTRATED both statically and live over real
HTTP, the SAME "static stays silent, live diverges 200/404" proof every
prior round's finding required, and closed by extending
:func:`_has_dominating_trusted_binding` to also require every free
variable inside the trusted text to be provably unshadowed, via the
SAME :func:`_name_rebinding_sites` machinery round 10 already built
(see :func:`_trusted_source_free_roots`). Finding B: even a genuinely
provenance-proven ``api = STATE.api`` still trusts the reviewed
facade's WHOLE surface, not a per-method allowlist -- a hypothetical
FUTURE ``api.invoke``-shaped method could hide routing behaviour behind
a name this module cannot vet without running the program, precisely
the residual concern round 10's own fix disclosed; a per-method
allowlist was tried and rejected (174 distinct ``api.<attr>`` names in
server.py today, and a name-based heuristic produced a real false
positive), so this stays NOT fixed -- documented just below as a KNOWN
LIMITATION, with the Callable-annotation contract test round 10 already
added as its standing tripwire), an ELEVENTH (round 12: 1 finding --
round 11's own finding A closed "is ``STATE`` shadowed" for a parameter-
default and a local reassignment, but :func:`_name_rebinding_sites`'s
binding-form enumeration missed a THIRD spelling structural pattern
matching adds, a ``match``/``case`` capture -- documented rather than
fixed, on the strength of "server.py has no ``match`` statement today"),
and a TWELFTH (round 13: round 12's own "not exploitable today" call did
not survive its own tripwire test actually being RUN -- the synthetic
repro placed the trusted ``api = STATE.api`` read BEFORE the capture that
was supposed to shadow it, an ordering Python itself refuses to execute
(``UnboundLocalError``) once ANY statement in the function binds that
name, capture included -- so the gap was live, not merely latent, and
finding 2 fixes it outright: :func:`_name_rebinding_sites` now recognises
``ast.MatchAs``/``ast.MatchMapping`` captures the exact same way it
already recognises ``ast.arg``/``ExceptHandler``/``Global``/``Nonlocal``/
``Import``).
Each round's own pattern repeats: fix what was found, and a FRESH hunt
finds more. That is not a sign any individual round was careless -- it
is the expected, unavoidable shape of a bespoke static analyzer over a
general-purpose language: the class of Python constructs that could
conceivably encode a routing decision is not finite, and this module
recognises a specific, growing-but-always-partial list of them.

So, stated plainly, NOT as an oversight but as a considered engineering
trade-off:

* this gate is NOT claimed to be exhaustively complete against arbitrary
  future Python constructs. Finding H above, finding 6c's own
  precisely-bounded residual gap above, and round 11's own finding B
  above (the captured-arg provenance gate trusts a proven name's WHOLE
  surface, not a per-method allowlist) are known, DOCUMENTED,
  currently-undemonstrated-beyond-what-is-stated gaps; there is no proof
  that no OTHER gap exists beyond the ones thirteen rounds of review
  happened to find -- round 9's own finding is itself a clear illustration:
  round 8 closed its specific category (transparent Tuple/List/IfExp
  composition) completely, and a NEW category (higher-order argument
  transfer) was found regardless. Round 10 confirms the pattern does not
  stop once a round closes its OWN category completely, either: round
  9's own fix (the allowlist) was itself found insufficient by round 10,
  not through a further transfer shape, but through the allowlist's
  trust boundary being spelling-only rather than provenance-based -- a
  category of gap (WHO a mechanism trusts, not WHAT shape defeats it)
  none of rounds 6-9 needed to consider, because none of them had yet
  introduced a mechanism that trusted a NAME at all -- and round 11 bore
  this out again, though from WITHIN round 10's own fix rather than a
  category no round had yet needed to consider: a free variable EMBEDDED
  INSIDE an already-trusted expression turns out to need the identical
  unshadowed-name proof the expression's own root did, one level up (see
  round 11's finding A above). A twelfth round should be expected to
  find a twelfth thing, on the same pattern as the first eleven -- and it
  did: round 12 found a THIRD unmodelled binding spelling
  (:func:`_name_rebinding_sites` had not been taught structural pattern
  matching's own capture forms), the same category as round 11's finding
  A one binding-form further out, not a new category. Round 13 then found
  that round 12's own "not exploitable today" call rested on a repro that
  could not execute -- a reminder that "documented as latent" is only as
  good as the falsifiability of the tripwire proving it stays latent, not
  a substitute for actually running the thing;
* the actual soundness BACKSTOP for CORRECTNESS -- as distinct from
  completeness of this module's own DETECTION -- is not this static walker
  at all, but a RUNTIME proof already in place: the 405/Allow admission
  wiring server.py now runs (the #202 wiring step referenced above) is
  diffed BYTE-IDENTICAL against real HTTP behaviour across a full request
  corpus, and the 13 ``post_v2_setup_*``/``assign-*`` endpoints this
  repair's own classification pass touched were independently CONFIRMED
  REACHABLE over real HTTP, not merely extracted by this module and taken
  on faith. A gap in this file's detection is a gap in the INVENTORY; it is
  not, by itself, a gap in what the server actually does or refuses;
* for whoever picks up further #202 work: further investment may be
  BETTER SPENT on a CORPUS-BASED RUNTIME reachability/uniqueness proof --
  fuzzed or generated real requests fired at the live server, with the
  actual (status, body-shape) response compared against what the registry
  claims for that path -- than on continuing to harden this static walker
  indefinitely. A runtime proof of that shape is immune to "one more
  Python construct the walker didn't enumerate", because it does not care
  HOW server.py arrived at its answer, only WHAT that answer was. This
  static walker will keep being worth strengthening when a NEW escape is
  actually found (as this round did for E/F/G) -- it is a good, cheap
  first line of defence and a genuinely useful development-time inventory
  -- but treating it as the LAST line, capable of eventually reaching
  proven completeness by finding one more gap at a time, is not a
  realistic goal for a hand-written analyzer over a general-purpose
  language, and this module does not claim otherwise.

Stdlib only (CLAUDE.md): ``ast``, ``dataclasses``. No ``re`` — the dispatch
patterns are parsed by this module's own small recursive-descent parser
(:class:`_RegexParser`), not interpreted as live regexes.
"""
from __future__ import annotations

import ast
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

SERVER_PATH = Path(__file__).with_name("server.py")

#: One path segment matching ``[^/]+`` in a canonical template.
SEG = "{}"
#: One path segment matching ``\w+``. NOT the same template as SEG (#202
#: repair, root cause 4): ``\w+`` excludes ``.``, ``-`` and other bytes
#: ``[^/]+`` allows, so the two match different reachable sets. Collapsing
#: them to one token was exactly what let a route whose real matcher is
#: ``\w+`` be silently identified with a broader ``[^/]+`` sibling (or vice
#: versa) with no way for a uniqueness check to tell them apart.
WORD = "{w}"
#: A free, NON-EMPTY tail (``.+``, or everything after a prefix route).
TAIL = "{*}"
#: A free, POSSIBLY-EMPTY tail (``.*``). NOT equivalent to TAIL: an empty
#: remainder matches ``.*`` and does not match ``.+`` (#202 repair root cause
#: 4) -- kept as its own token for the same reason WORD is.
TAIL0 = "{*0}"

#: kind -> its rendered token, for every non-literal Part kind.
_KIND_TOKENS = {"seg": SEG, "word": WORD, "tail": TAIL, "tail0": TAIL0}


class ExtractionError(RuntimeError):
    """A dispatch shape this module does not understand.

    Raised rather than skipped: a silently ignored branch is exactly the
    failure mode the derived inventory exists to remove.
    """


# --------------------------------------------------------------------------
# 1. Regex -> canonical templates
# --------------------------------------------------------------------------
#
# The dispatch patterns use a deliberately small subset of the regex language.
# This parser accepts exactly that subset and rejects everything else, so a new
# construct cannot be half-understood.


@dataclass(frozen=True)
class Part:
    """One piece of an expanded pattern.

    ``kind`` is ``lit`` (literal text), ``seg`` (``[^/]+``), ``word`` (``\\w+``),
    ``tail`` (``.+``) or ``tail0`` (``.*``) -- four DISTINCT non-literal kinds,
    each with its own template token (#202 repair root cause 4: collapsing any
    two of these hides a real difference in what each one matches). ``group``
    is the 1-based capture-group number this piece came from, or ``None`` when
    it is outside every capture group.
    """

    kind: str
    text: str = ""
    group: Optional[int] = None

    def render(self) -> str:
        if self.kind == "lit":
            return self.text
        return _KIND_TOKENS[self.kind]


@dataclass(frozen=True)
class Expansion:
    """One concrete shape a pattern can take once alternations are expanded."""

    parts: tuple
    absent: frozenset = frozenset()  # capture groups skipped by an optional

    @property
    def template(self) -> str:
        return "".join(p.render() for p in self.parts)


class _RegexParser:
    """Recursive-descent parser for the dispatch-pattern subset."""

    def __init__(self, source: str):
        self.src = source
        self.i = 0
        self.groups = 0

    # -- entry ------------------------------------------------------------
    def parse(self) -> list:
        if not self.src.startswith("^"):
            raise ExtractionError(
                f"dispatch pattern is not anchored at the start: {self.src!r}")
        if not self.src.endswith("$"):
            raise ExtractionError(
                f"dispatch pattern is not anchored at the end: {self.src!r}")
        self.i = 1
        node = self._alt()
        if self.i != len(self.src) - 1:
            raise ExtractionError(
                f"unparsed tail {self.src[self.i:]!r} in pattern {self.src!r}")
        if node[0] == "alt" and len(node[1]) > 1:
            # #202 repair root cause 5: a TOP-LEVEL alternation is not what a
            # naive reading of the surrounding ^/$ suggests. Regex alternation
            # is the LOWEST-precedence operator, so ``^/a|/b$`` parses as
            # ``(^/a)|(/b$)`` -- the first branch anchored at the start ONLY
            # (matching "/a-anything" under re.match, which does not require
            # consuming the whole string) and the second anchored at the end
            # ONLY. That divergence from "each branch anchored the way the
            # whole pattern reads" is exactly the ambiguity a human must
            # resolve -- wrap the branches in a group (``(?:a|b)``), which
            # this parser scopes correctly, instead of leaving them at the
            # top level.
            raise ExtractionError(
                f"top-level alternation in {self.src!r} is ambiguous: "
                "'^' and '$' do not distribute over each '|' branch the way "
                "a naive reading suggests (alternation is the lowest-"
                "precedence operator). Wrap the branches in a group, e.g. "
                "(?:a|b), so each is anchored the way the whole pattern "
                "reads.")
        return node

    # -- grammar ----------------------------------------------------------
    def _alt(self):
        branches = [self._seq()]
        while self._peek() == "|":
            self.i += 1
            branches.append(self._seq())
        return ("alt", branches)

    def _seq(self):
        items = []
        while True:
            ch = self._peek()
            if ch is None or ch in "|)" or (ch == "$" and self.i == len(self.src) - 1):
                break
            items.append(self._item())
        return ("seq", items)

    def _item(self):
        ch = self.src[self.i]
        if ch == "(":
            return self._group()
        if ch == "[":
            if self.src.startswith("[^/]+", self.i):
                self.i += len("[^/]+")
                return ("seg",)
            raise ExtractionError(
                f"unsupported character class at {self.i} in {self.src!r} "
                "(only [^/]+ is understood)")
        if ch == "\\":
            nxt = self.src[self.i + 1:self.i + 2]
            if nxt == "w" and self.src[self.i + 2:self.i + 3] == "+":
                self.i += 3
                # NOT "seg" (#202 repair root cause 4): \w+ excludes '.', '-'
                # and other bytes [^/]+ allows, so it is a narrower, DISTINCT
                # reachable set that needs its own template token.
                return ("word",)
            if nxt in ".-/":
                self.i += 2
                return ("lit", nxt)
            raise ExtractionError(
                f"unsupported escape \\{nxt} in {self.src!r}")
        if ch == ".":
            # ``.+`` (a non-empty tail) and ``.*`` (a possibly-empty one, which
            # is how a startswith() prefix route is written as a regex) are
            # DISTINCT (#202 repair root cause 4): an empty remainder matches
            # ``.*`` and not ``.+``, so each gets its own template token
            # rather than being silently identified as "a free tail".
            if self.src[self.i + 1:self.i + 2] == "+":
                self.i += 2
                return ("tail",)
            if self.src[self.i + 1:self.i + 2] == "*":
                self.i += 2
                return ("tail0",)
            raise ExtractionError(f"bare '.' in {self.src!r}")
        if ch in "*+?{}]^$":
            raise ExtractionError(
                f"unsupported metacharacter {ch!r} at {self.i} in {self.src!r}")
        self.i += 1
        return ("lit", ch)

    def _group(self):
        self.i += 1  # consume "("
        capture = True
        if self.src.startswith("?:", self.i):
            capture = False
            self.i += 2
        elif self.src[self.i:self.i + 1] == "?":
            raise ExtractionError(f"unsupported group extension in {self.src!r}")
        index = None
        if capture:
            self.groups += 1
            index = self.groups
        inner = self._alt()
        if self._peek() != ")":
            raise ExtractionError(f"unbalanced '(' in {self.src!r}")
        self.i += 1
        optional = False
        if self._peek() == "?":
            optional = True
            self.i += 1
        return ("group", inner, index, optional)

    def _peek(self):
        return self.src[self.i] if self.i < len(self.src) else None


def _capture_indices(node) -> set:
    kind = node[0]
    if kind == "group":
        found = _capture_indices(node[1])
        if node[2] is not None:
            found.add(node[2])
        return found
    if kind == "alt":
        out = set()
        for branch in node[1]:
            out |= _capture_indices(branch)
        return out
    if kind == "seq":
        out = set()
        for item in node[1]:
            out |= _capture_indices(item)
        return out
    return set()


def _merge_parts(parts) -> tuple:
    """Fuse adjacent literal characters that belong to the same capture group.

    The parser emits one Part per character; a template is only splittable at a
    capture group when that group is a single piece, so ``(board|lineups)`` has
    to come back out as one ``board`` literal rather than five.
    """
    merged = []
    for part in parts:
        if (merged and part.kind == "lit" and merged[-1].kind == "lit"
                and merged[-1].group == part.group):
            merged[-1] = Part("lit", merged[-1].text + part.text, part.group)
        else:
            merged.append(part)
    return tuple(merged)


def _expand_node(node) -> list:
    """Expand one grammar node into a list of :class:`Expansion`."""
    kind = node[0]
    if kind == "lit":
        return [Expansion((Part("lit", node[1]),))]
    if kind in ("seg", "word", "tail", "tail0"):
        return [Expansion((Part(kind),))]
    if kind == "alt":
        out = []
        for branch in node[1]:
            out.extend(_expand_node(branch))
        return out
    if kind == "seq":
        out = [Expansion((), frozenset())]
        for item in node[1]:
            grown = []
            for head in out:
                for piece in _expand_node(item):
                    grown.append(Expansion(
                        _merge_parts(head.parts + piece.parts),
                        head.absent | piece.absent))
            out = grown
        return out
    if kind == "group":
        inner, index, optional = node[1], node[2], node[3]
        out = []
        for exp in _expand_node(inner):
            if index is None:
                out.append(exp)
            else:
                out.append(Expansion(
                    _merge_parts(
                        tuple(p if p.group is not None
                              else Part(p.kind, p.text, index)
                              for p in exp.parts)),
                    exp.absent))
        if optional:
            out.append(Expansion((), frozenset(_capture_indices(node))))
        return out
    raise ExtractionError(f"unknown node {kind!r}")


def expand_pattern(pattern: str) -> list:
    """Expand a dispatch regex into every concrete template it can match."""
    return _expand_node(_RegexParser(pattern).parse())


def templates_of_pattern(pattern: str) -> list:
    """The canonical templates a full-path regex covers (dedup, ordered)."""
    seen, out = set(), []
    for exp in expand_pattern(pattern):
        if exp.template not in seen:
            seen.add(exp.template)
            out.append(exp.template)
    return out


#: Every placeholder token, longest/most-specific first so a shorter token
#: that happens to be a prefix of a longer one never matches early. (None
#: currently collides -- SEG/WORD differ at index 1, TAIL/TAIL0 at index 2 --
#: but checking in this order keeps that true even if a future token does.)
_ALL_TOKENS = (TAIL0, TAIL, WORD, SEG)


def sample_path(template: str, token: str = "sample") -> str:
    """A concrete path matching ``template`` (placeholders -> a token).

    Used by the cross-checks: it turns a template or a table pattern into
    something both sides can be matched against. The same alphanumeric
    filler is valid for every placeholder kind -- it satisfies ``[^/]+``,
    ``\\w+`` and a non-empty ``.+``/``.*`` alike -- so the sample does not
    need to vary by which of the four tokens it is standing in for.
    """
    out, n = [], 0
    rest = template
    while rest:
        matched = next((t for t in _ALL_TOKENS if rest.startswith(t)), None)
        if matched:
            n += 1
            out.append(f"{token}{n}")
            rest = rest[len(matched):]
        else:
            out.append(rest[0])
            rest = rest[1:]
    return "".join(out)


# --------------------------------------------------------------------------
# 2. The walker
# --------------------------------------------------------------------------

FREE = "<free>"  # sentinel: this subject may hold any string


@dataclass(frozen=True)
class Alt:
    """One possible shape of the subject currently being dispatched on.

    ``prefix``/``suffix`` are the template text around the subject's value;
    ``value`` is ``FREE`` (any string), a literal, or ``None`` (an optional
    group that is absent in this shape).
    """

    prefix: str = ""
    suffix: str = ""
    value: object = FREE

    @property
    def is_free(self) -> bool:
        return self.value is FREE

    def template_for(self, literal: str) -> str:
        return f"{self.prefix}{literal}{self.suffix}"

    @property
    def fixed_template(self) -> Optional[str]:
        if self.is_free:
            return None
        return self.prefix + ("" if self.value is None else self.value) + self.suffix


@dataclass(frozen=True)
class LiveRoute:
    """One live dispatch branch, as found in the source."""

    method: str
    template: str
    handler: str
    shape: str
    lineno: int
    test: str

    @property
    def key(self) -> tuple:
        return (self.method, self.template)


@dataclass
class _Ctx:
    method: str
    handler: str
    subjects: dict = field(default_factory=dict)   # name -> tuple[Alt, ...]
    matches: dict = field(default_factory=dict)    # name -> (subject, pattern, expansions)
    dicts: dict = field(default_factory=dict)      # name -> (subject, keys)
    #: name -> tuple of component subject names, e.g. ``combo = (entity,
    #: target)`` -> ``("entity", "target")`` (#202 repair root cause 1).
    tuples: dict = field(default_factory=dict)
    #: name -> tuple of literal key-tuples, e.g. ``{("a","b"): ...}`` ->
    #: ``(("a", "b"), ...)``. Only the KEYS matter; values route nothing.
    tuple_dicts: dict = field(default_factory=dict)
    #: name -> (dict name, tuple name), for a local bound from
    #: ``DICT.get(combo)``/``DICT[combo]`` and later tested for truthiness --
    #: the same enumeration as ``combo in DICT``, reached a different way.
    tuple_lookups: dict = field(default_factory=dict)
    #: name -> (base Alt, expansions, group index) for a subject bound from
    #: ``m.group(K)`` -- either directly (this function's own match) or
    #: carried across a PARSED_DELEGATES call (#202 repair root cause 1).
    #: Lets components of a LATER tuple be proven to come from the SAME
    #: match, which is what makes enumerating ``combo in DICT`` safe: two
    #: names sharing an origin are positionally correlated (same expansion),
    #: not an arbitrary cross-product.
    origins: dict = field(default_factory=dict)
    #: Every path-bearing name bound ANYWHERE in this function walk, including
    #: inside nested branches. Shared (not copied) with children so the
    #: completeness audit below sees names a child ctx introduced.
    seen: set = field(default_factory=set)
    #: Every name EVER bound via :meth:`bind_subject` ANYWHERE in this
    #: function walk (#202 repair round 5, finding 2b) -- a captured regex
    #: group, directly or a TAIL_DELEGATES/PARSED_DELEGATES parameter
    #: carrying one across a delegation boundary. Shared (not copied) with
    #: children the SAME way ``seen`` is, and DELIBERATELY NOT ``subjects``
    #: itself (which IS copied per child, so it only reflects the CURRENT,
    #: still-open narrowing scope): DEMONSTRATED that ``ctx.subjects`` goes
    #: OUT OF SCOPE the moment the ``if <the regex match>:`` block that
    #: introduced a captured name (the near-universal shape for a capture
    #: -- ``mgo = re.match(...); if mgo: jid = mgo.group(1)``) closes, so a
    #: name captured inside ANY nested branch -- the common case -- was
    #: invisible to a check keyed on ``ctx.subjects`` by the time
    #: ``_audit_function`` runs at the END of the whole walk, even though
    #: the SAME name is very much still in ``tracked`` via ``ctx.seen``.
    #: Read by :func:`_propagates_taint` (as ``captured``) to exempt a
    #: non-``self.`` call whose only tracked mentions are already-captured
    #: data -- see that function's own docstring for the full reasoning.
    captured: set = field(default_factory=set)

    def child(self) -> "_Ctx":
        return _Ctx(self.method, self.handler, dict(self.subjects),
                    dict(self.matches), dict(self.dicts), dict(self.tuples),
                    dict(self.tuple_dicts), dict(self.tuple_lookups),
                    dict(self.origins), self.seen, self.captured)

    def bind_subject(self, name: str, alts) -> None:
        self.subjects[name] = tuple(alts)
        self.seen.add(name)
        self.captured.add(name)

    def bind_match(self, name: str, info) -> None:
        self.matches[name] = info
        self.seen.add(name)

    def bind_dict(self, name: str, info) -> None:
        self.dicts[name] = info
        self.seen.add(name)

    def bind_tuple(self, name: str, components: tuple) -> None:
        self.tuples[name] = components
        self.seen.add(name)

    def bind_tuple_dict(self, name: str, keys: tuple) -> None:
        self.tuple_dicts[name] = keys
        self.seen.add(name)

    def bind_tuple_lookup(self, name: str, info: tuple) -> None:
        self.tuple_lookups[name] = info
        self.seen.add(name)


@dataclass
class _Outcome:
    """What a classified ``if`` test selects."""

    shape: str
    subject: Optional[str]
    templates: tuple = ()      # full-path templates this branch claims
    alts: Optional[tuple] = None   # refinement of the subject inside the body
    literal: str = ""          # the startswith/endswith operand, when relevant


#: ``_handle_*`` methods whose first argument is a PATH TAIL: walked, with the
#: sliced prefix carried into their branches.
TAIL_DELEGATES = {"_handle_setup", "_handle_setup_v2"}
#: Methods reached with the WHOLE path (or no argument at all) that continue the
#: same dispatch: walked with the caller's subject unchanged.
SAME_PATH_DELEGATES = {"_dispatch_get", "_serve_static"}
#: ``_handle_*`` methods that receive ALREADY-PARSED regex groups, never a path.
#: Walked with each parameter bound from the caller's own ``m.group(K)``
#: argument (#202 repair root cause 1) -- the route shape is NOT fully decided
#: by the caller's pattern alone when the callee itself narrows the captured
#: groups further (see :meth:`_DispatchWalker._delegate_parsed`).
PARSED_DELEGATES = {"_handle_reassign", "_handle_reassign_v2"}


def _without_leading_docstring(body: list) -> list:
    """``fn.body`` with a leading bare string-literal statement dropped."""
    if body and isinstance(body[0], ast.Expr) \
            and isinstance(body[0].value, ast.Constant) \
            and isinstance(body[0].value.value, str):
        return body[1:]
    return body


#: The EXACT (post-docstring) body of ``do_HEAD`` today -- #202 repair root
#: cause 3. Compared via ``ast.unparse`` rather than inspected shallowly, so
#: ANY change (new logic, a renamed local, a reordered statement) fails
#: closed rather than passing because some individual name looked familiar.
_DO_HEAD_SAFE_SHAPE = (
    "self._head_only = True",
    "try:\n    self.do_GET()\nfinally:\n    self._head_only = False",
)
#: The EXACT (post-docstring) body of ``do_OPTIONS`` today. Same rationale.
_DO_OPTIONS_SAFE_SHAPE = (
    "path = self.path.split('?', 1)[0]",
    "methods = self._supported_methods(path)",
    "if not methods:\n    return self._send_status(404)",
    "return self._send_status(204, [('Allow', ', '.join(sorted(methods)))])",
)


class _DispatchWalker:
    def __init__(self, tree: ast.Module):
        self.functions = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and node.name == "Handler":
                for item in node.body:
                    if isinstance(item, ast.FunctionDef):
                        self.functions[item.name] = item
        self.routes = {}
        self.unreachable = []      # nested branches no live shape can reach
        self.shape_counts = {}
        self._classified = set()   # id() of If nodes this walker understood
        self._followed = set()     # id() of delegation Calls this walker took
        self._walked = set()       # (function, subject shapes) already walked
        #: #202 repair round 2, finding D. key -> {id(node), ...} for every
        #: _AUDIT_WAIVERS entry actually consulted this run, recording the
        #: DISTINCT AST NODES matched (not a raw hit count) so re-examining
        #: the SAME source line more than once -- which _propagates_taint's
        #: own taint fixed-point loop legitimately does, revisiting every
        #: assignment on each pass until nothing new grows, an
        #: implementation detail unrelated to how many SOURCE LOCATIONS a
        #: waiver actually matches -- never masquerades as "matches two
        #: different lines". See :meth:`verify_waiver_usage`.
        self.waiver_hits = {}

    def _record_waiver_hit(self, key: tuple, node) -> None:
        self.waiver_hits.setdefault(key, set()).add(id(node))

    # -- public ------------------------------------------------------------
    def run(self, entry_points: dict) -> list:
        for method, fname in entry_points.items():
            fn = self.functions.get(fname)
            if fn is None:
                raise ExtractionError(f"entry point {fname} not found on Handler")
            ctx = _Ctx(method, fname)
            ctx.bind_subject("path", (Alt(),))
            self._walk_function(fn, ctx)
        self._audit_unwalked_verbs(set(entry_points.values()))
        return sorted(self.routes.values(),
                      key=lambda r: (r.method, r.template))

    def verify_waiver_usage(self) -> None:
        """#202 repair round 2, finding D: every declared ``_AUDIT_WAIVERS``
        entry must be consulted EXACTLY ONCE by a completed run -- never
        zero (a DORMANT/orphaned entry matching no line anywhere in the
        parsed source, proof nothing depends on it and it is silently
        rotting) and never more than one DISTINCT source location (too
        broad to trust that it is really pinned to the one line it names).
        This is the fingerprinting the original review required and is the
        one check standing between a future waiver and it quietly
        defeating the whole gate by matching something its author never
        reviewed.

        Call after a completed :meth:`run` -- callers gate this to the
        REAL ``server.py`` (see :func:`extract_routes`/:func:`extract_walker`
        with ``source=None``): a synthetic test fixture legitimately
        consults none of server.py's own waivers, so enforcing this
        unconditionally would fail every such fixture, not just a
        genuinely orphaned or over-broad waiver.
        """
        orphaned = [key for key in _AUDIT_WAIVERS
                   if len(self.waiver_hits.get(key, ())) == 0]
        too_broad = [key for key in _AUDIT_WAIVERS
                    if len(self.waiver_hits.get(key, ())) > 1]
        if not orphaned and not too_broad:
            return
        lines = []
        for key in orphaned:
            lines.append(f"  DORMANT (0 hits): {key!r}")
        for key in too_broad:
            hits = len(self.waiver_hits[key])
            lines.append(f"  TOO BROAD ({hits} distinct locations): {key!r}")
        raise ExtractionError(
            "_AUDIT_WAIVERS entries failed exact-one-hit fingerprinting:\n"
            + "\n".join(lines) +
            "\nA dormant waiver matches nothing and must be removed (it is "
            "proof nothing depends on it); a too-broad waiver matches more "
            "than the one reviewed line it names and must be narrowed or "
            "split into one entry per location -- neither may be trusted "
            "as-is.")

    def _audit_unwalked_verbs(self, walked: set):
        """No OTHER ``do_*`` verb may grow a dispatch of its own unnoticed.

        Today only GET and POST have one: HEAD re-runs ``do_GET``, and
        PUT/PATCH/DELETE/OPTIONS answer from ``_supported_methods`` without
        selecting a route. A verb handler that started matching paths while the
        extractor still read two entry points would leave a whole method's
        routes out of the inventory in silence.

        #202 repair root cause 3: enumerating ``do_*`` methods is not enough
        on its own -- the OLD version of this check only asked "does an `if`
        here test something literally named `path`, or call `re.match`", which
        a renamed local (``p2 = self.path...; if p2 == ...``) or any OTHER
        change entirely defeats, silently. Every unwalked verb must now be
        PROVABLY INERT: its whole body compared, statement for statement, via
        ``ast.unparse``, against one of a small number of declared-safe shapes
        (see :func:`_require_safe_verb_shape`) -- unconditionally, not
        dependent on any variable's name.
        """
        for name, fn in sorted(self.functions.items()):
            if not name.startswith("do_") or name in walked:
                continue
            self._require_safe_verb_shape(name, fn)

    def _require_safe_verb_shape(self, name: str, fn: ast.FunctionDef) -> None:
        """Raise unless ``fn`` (a ``do_<VERB>`` NOT walked as a dispatch entry
        point) is one of the shapes server.py is known to use for a verb that
        selects no route: a bare ``self._method_fallback(<VERB>)`` (PUT,
        PATCH, DELETE, and any future verb following the same convention), or
        ``do_HEAD``'s / ``do_OPTIONS``'s own exact bodies. Anything else --
        real routing logic, a renamed local, an added statement, a wholly new
        verb that doesn't follow the fallback convention -- raises, every
        time, because the comparison is the COMPLETE body, not a name.
        """
        verb = name[3:]
        body = _without_leading_docstring(fn.body)
        unparsed = tuple(ast.unparse(stmt) for stmt in body)
        if name == "do_HEAD":
            safe = unparsed == _DO_HEAD_SAFE_SHAPE
        elif name == "do_OPTIONS":
            safe = unparsed == _DO_OPTIONS_SAFE_SHAPE
        else:
            safe = unparsed == (f"self._method_fallback({verb!r})",)
        if safe:
            return
        # The two originally-named evasions get their own specific diagnosis
        # first, when they apply, so the message points straight at the
        # test/call that looks like a dispatch.
        for node in ast.walk(fn):
            if isinstance(node, ast.If) and \
                    "path" in _direct_operand_names(node.test):
                raise ExtractionError(
                    f"{name}:{node.lineno} dispatches on the path "
                    f"({ast.unparse(node.test)}) but is not an extractor "
                    "entry point — add it to ENTRY_POINTS")
            if isinstance(node, ast.Call) \
                    and isinstance(node.func, ast.Attribute) \
                    and isinstance(node.func.value, ast.Name) \
                    and node.func.value.id == "re" \
                    and node.func.attr in ("match", "fullmatch"):
                raise ExtractionError(
                    f"{name}:{node.lineno} matches paths with a regex but "
                    "is not an extractor entry point — add it to "
                    "ENTRY_POINTS")
        # Fail closed UNCONDITIONALLY for anything else: a renamed local, an
        # injected statement, a body that merely LOOKS like the safe shape --
        # none of those touch a name this loop recognises, which is exactly
        # why the check above is not the only one.
        raise ExtractionError(
            f"{name} is not a recognised extractor entry point (see "
            "ENTRY_POINTS) and its body does not match any do_* shape "
            f"route_extract.py has declared safe (a bare "
            f"self._method_fallback({verb!r}) fallback, or do_HEAD's / "
            "do_OPTIONS's own exact modelled shape). Classify its real "
            "shape here — do not let a new or changed do_* verb pass "
            "silently.")

    # -- walking -----------------------------------------------------------
    def _walk_function(self, fn: ast.FunctionDef, ctx: _Ctx):
        # The subject shapes are part of the key: the same handler reached from
        # two different prefixes serves two different route sets, and skipping
        # the second as "already walked" would silently lose one of them.
        key = (fn.name, ctx.method, repr(sorted(ctx.subjects.items())))
        if key in self._walked:
            return
        self._walked.add(key)
        ctx.handler = fn.name
        self._walk_body(fn.body, ctx)
        self._audit_function(fn, ctx)

    def _walk_body(self, body: list, ctx: _Ctx):
        # A fresh sibling-overlap SCOPE per statement list (#202 repair round
        # 2, finding C): every ast.If directly in THIS list -- and every
        # elif arm continuing one of them, via _walk_terminal_else -- shares
        # this one dict, so a LATER sibling claiming a template an EARLIER
        # one already claimed is caught. A NESTED if's own body gets its own
        # fresh scope from ITS OWN _walk_body call below, which is exactly
        # what keeps this scoped to "siblings", not "anywhere in the
        # function" -- the nested-unreachable detector already owns that.
        scope = {}
        for stmt in body:
            self._walk_stmt(stmt, ctx, scope)

    def _walk_stmt(self, stmt, ctx: _Ctx, scope: dict):
        if isinstance(stmt, ast.Assign):
            self._record_binding(stmt, ctx)
        elif isinstance(stmt, ast.If):
            self._walk_if(stmt, ctx, scope)
        elif isinstance(stmt, (ast.Try, ast.With, ast.For, ast.While)):
            # Each of body/orelse/finalbody is its OWN statement list -- walk
            # it exactly as _walk_body would (own fresh sibling scope), not
            # folded into this statement's enclosing scope: a `try:`'s body
            # is a different dispatch scope than the code around the `try`.
            for attr in ("body", "orelse", "finalbody"):
                self._walk_body(getattr(stmt, attr, []) or [], ctx)
            for handler in getattr(stmt, "handlers", []) or []:
                self._walk_body(handler.body, ctx)
        elif isinstance(stmt, ast.Return) and stmt.value is not None:
            self._maybe_delegate(stmt.value, ctx)
        elif isinstance(stmt, ast.Expr):
            self._maybe_delegate(stmt.value, ctx)
        elif hasattr(ast, "Match") and isinstance(stmt, ast.Match):
            # ``match``/``case`` (#202 repair, invented-evasion track): NOT
            # an ast.If, so the completeness audit's own scan (which looks
            # for ast.If nodes) would never see one at all -- SILENT, not
            # even a raise, which this module's whole design treats as the
            # one unacceptable outcome. Fail closed on the subject itself
            # (no case-pattern classifier exists here to trust); walk into
            # each case body for any dispatch NESTED further inside it.
            if self._touches_tracked(stmt.subject, ctx):
                raise ExtractionError(
                    f"line {stmt.lineno}: a `match` statement dispatches on "
                    "a tracked subject, which route_extract does not model "
                    "-- rewrite as if/elif, or classify match/case here")
            for case in stmt.cases:
                self._walk_body(case.body, ctx)

    # -- bindings ----------------------------------------------------------
    def _record_binding(self, stmt: ast.Assign, ctx: _Ctx):
        targets = stmt.targets
        if len(targets) != 1:
            return
        target, value = targets[0], stmt.value
        # m = re.match(r"...", <subject>)
        if isinstance(target, ast.Name):
            info = self._as_regex_call(value, ctx)
            if info is not None:
                ctx.bind_match(target.id, info)
                return
            keys = self._as_dict_get(value, ctx)
            if keys is not None:
                ctx.bind_dict(target.id, keys)
                return
            # combo = (entity, target) -- #202 repair root cause 1.
            combo = self._as_tuple_of_subjects(value, ctx)
            if combo is not None:
                ctx.bind_tuple(target.id, combo)
                return
            # SCHEMA = {("a", "b"): ..., ...} -- a literal dict whose keys are
            # literal tuples. Same root cause: only the key SHAPE routes.
            tuple_keys = self._as_tuple_keyed_dict(value)
            if tuple_keys is not None:
                ctx.bind_tuple_dict(target.id, tuple_keys)
                return
            # dest = SCHEMA.get(combo)  /  dest = SCHEMA[combo]
            lookup = self._as_tuple_dict_lookup(value, ctx)
            if lookup is not None:
                ctx.bind_tuple_lookup(target.id, lookup)
                return
            grp = self._as_group_call(value, ctx)
            if grp is not None:
                ctx.bind_subject(target.id, grp)
                origin = self._group_origin(value, ctx)
                if origin is not None:
                    ctx.origins[target.id] = origin
            return
        # a, b = m.group(1), m.group(2)
        if isinstance(target, ast.Tuple) and isinstance(value, ast.Tuple):
            for name_node, val in zip(target.elts, value.elts):
                if not isinstance(name_node, ast.Name):
                    continue
                grp = self._as_group_call(val, ctx)
                if grp is not None:
                    ctx.bind_subject(name_node.id, grp)
                    origin = self._group_origin(val, ctx)
                    if origin is not None:
                        ctx.origins[name_node.id] = origin

    def _as_tuple_of_subjects(self, node, ctx: _Ctx):
        """``combo = (entity, target)`` -> ``("entity", "target")`` when every
        element is a name this walker already tracks as a dispatch subject.

        #202 repair root cause 1: this is what lets a LATER ``combo in
        DICT`` be recognised as a routing decision over entity/target
        TOGETHER, rather than the tuple literal silently falling through
        untracked (which is what let a schema dict's own key set go
        invisible to the audit before this fix).
        """
        if not isinstance(node, ast.Tuple) or not node.elts:
            return None
        names = []
        for elt in node.elts:
            if not (isinstance(elt, ast.Name) and elt.id in ctx.subjects):
                return None
            names.append(elt.id)
        return tuple(names)

    def _as_tuple_keyed_dict(self, node):
        """A dict literal every key of which is a literal tuple of the SAME
        arity, e.g. ``{("league", "organization"): dict(...), ...}``.

        Only the KEYS are inspected -- the key shape is what a membership/
        lookup test routes on; the values (here, ``dict(...)`` calls) are
        opaque payload and irrelevant to routing.
        """
        if not isinstance(node, ast.Dict) or not node.keys:
            return None
        keys = []
        arity = None
        for key in node.keys:
            if not isinstance(key, ast.Tuple):
                return None
            values = []
            for elt in key.elts:
                if not isinstance(elt, ast.Constant):
                    return None
                values.append(elt.value)
            if arity is None:
                arity = len(values)
            elif len(values) != arity:
                return None
            keys.append(tuple(values))
        return tuple(keys)

    def _as_tuple_dict_lookup(self, node, ctx: _Ctx):
        """``DICT.get(combo)`` or ``DICT[combo]``, DICT a tracked tuple-keyed
        dict and combo a tracked tuple, -> ``(dict name, tuple name)`` -- so a
        LATER truthy/``is not None`` test on the bound local enumerates the
        same leaves ``combo in DICT`` would (#202 repair root cause 1: the
        real dispatch reads this shape via ``.get()``, not only ``in``).
        """
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "get"
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id in ctx.tuple_dicts
                and len(node.args) >= 1 and isinstance(node.args[0], ast.Name)
                and node.args[0].id in ctx.tuples):
            return (node.func.value.id, node.args[0].id)
        if (isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name)
                and node.value.id in ctx.tuple_dicts
                and isinstance(node.slice, ast.Name)
                and node.slice.id in ctx.tuples):
            return (node.value.id, node.slice.id)
        return None

    def _group_origin(self, node, ctx: _Ctx):
        """``m.group(K)`` -> ``(base, expansions, K)`` when the match's
        subject is a single (non-branching) base.

        #202 repair root cause 1: this is what lets several names bound from
        the SAME match (e.g. entity from group 1, target from group 3) be
        proven to correspond POSITIONALLY -- the same expansion, i.e. the
        same alternative of the source pattern -- rather than an arbitrary
        cross-product of their independently-computed value sets. More than
        one base refuses (returns None) rather than guess.
        """
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "group"
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id in ctx.matches
                and len(node.args) == 1
                and isinstance(node.args[0], ast.Constant)):
            return None
        subject, _pattern, expansions = ctx.matches[node.func.value.id]
        bases = ctx.subjects.get(subject, ())
        if len(bases) != 1:
            return None
        return (bases[0], expansions, node.args[0].value)

    # Every regex entry point, not just the two this walker understands. A call
    # to any of these ON A TRACKED SUBJECT that the walker cannot classify is an
    # ERROR, never a skip: `re.search(...)` and a module-level precompiled
    # `_RX.match(path)` were both DEMONSTRATED to add a live, publicly reachable
    # route while the extractor's count stayed at 237 and every gate test passed.
    # A gate that silently misses a shape is worse than no gate, because the next
    # person trusts it.
    _REGEX_METHODS = ("match", "fullmatch", "search", "findall", "finditer",
                      "split", "sub", "subn")

    def _touches_tracked(self, node, ctx: _Ctx) -> bool:
        return any(isinstance(a, ast.Name) and a.id in ctx.subjects
                   for a in ast.walk(node))

    def _as_regex_call(self, node, ctx: _Ctx):
        if not isinstance(node, ast.Call):
            return None
        recognised = (isinstance(node.func, ast.Attribute)
                      and isinstance(node.func.value, ast.Name)
                      and node.func.value.id == "re"
                      and node.func.attr in ("match", "fullmatch")
                      and len(node.args) == 2)
        if not recognised:
            # FAIL CLOSED. Anything regex-shaped that consumes a dispatch
            # subject, in a form this walker does not model, stops the build.
            if isinstance(node.func, ast.Attribute) \
                    and node.func.attr in self._REGEX_METHODS \
                    and self._touches_tracked(node, ctx):
                raise ExtractionError(
                    f"line {node.lineno}: regex call "
                    f"`{ast.unparse(node)}` consumes a dispatch subject in a "
                    f"shape route_extract does not model. Classify it here — "
                    f"do not let it be skipped, or the route it guards becomes "
                    f"invisible to the #202 gate.")
            return None
        pattern_node, subject_node = node.args
        if not (isinstance(pattern_node, ast.Constant)
                and isinstance(pattern_node.value, str)):
            raise ExtractionError("re.match with a non-literal pattern at line "
                                  f"{node.lineno}")
        if not (isinstance(subject_node, ast.Name)
                and subject_node.id in ctx.subjects):
            # A modelled re.match() whose subject we do not track: either the
            # subject is genuinely unrelated to routing, or it is the path under
            # a name we failed to taint. Only the first is safe, and we cannot
            # tell them apart here — so raise unless the subject is provably
            # untainted (not derived from self.path and not a tracked name).
            if self._touches_tracked(subject_node, ctx) or _is_path_derived(subject_node):
                raise ExtractionError(
                    f"line {node.lineno}: re.match on `"
                    f"{ast.unparse(subject_node)}`, which is path-derived but "
                    f"not a tracked dispatch subject")
            return None
        return (subject_node.id, pattern_node.value,
                expand_pattern(pattern_node.value))

    def _as_dict_get(self, node, ctx: _Ctx):
        if not (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "get"
                and isinstance(node.func.value, ast.Dict)
                and len(node.args) == 1
                and isinstance(node.args[0], ast.Name)
                and node.args[0].id in ctx.subjects):
            return None
        keys = []
        for key in node.func.value.keys:
            if not (isinstance(key, ast.Constant) and isinstance(key.value, str)):
                raise ExtractionError(
                    f"non-literal dict key in a dispatch at line {node.lineno}")
            keys.append(key.value)
        return (node.args[0].id, tuple(keys))

    def _as_group_call(self, node, ctx: _Ctx):
        """``m.group(N)`` -> the Alts the captured value can take."""
        if not (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "group"
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id in ctx.matches
                and len(node.args) == 1
                and isinstance(node.args[0], ast.Constant)):
            return None
        subject, pattern, expansions = ctx.matches[node.func.value.id]
        index = node.args[0].value
        alts = []
        for base in ctx.subjects[subject]:
            if not base.is_free:
                raise ExtractionError(
                    f"regex dispatch on an already-constrained subject "
                    f"{subject!r} at line {node.lineno}")
            for exp in expansions:
                alts.append(self._alt_for_group(base, exp, index, pattern))
        return tuple(alts)

    def _alt_for_group(self, base: Alt, exp: Expansion, index: int,
                       pattern: str) -> Alt:
        if index in exp.absent:
            return Alt(base.prefix + exp.template + base.suffix, "", None)
        slots = [i for i, p in enumerate(exp.parts) if p.group == index]
        if len(slots) != 1:
            raise ExtractionError(
                f"capture group {index} of {pattern!r} is not a single piece; "
                "the extractor cannot split the template there")
        pos = slots[0]
        prefix = base.prefix + "".join(p.render() for p in exp.parts[:pos])
        suffix = "".join(p.render() for p in exp.parts[pos + 1:]) + base.suffix
        part = exp.parts[pos]
        if part.kind == "lit":
            return Alt(prefix, suffix, part.text)
        return Alt(prefix, suffix, FREE)

    # -- branches ----------------------------------------------------------
    def _walk_if(self, node: ast.If, ctx: _Ctx, scope: dict):
        outcome = self._classify(node.test, ctx)
        if outcome is None:
            # Not a dispatch test (an auth guard, an error check, ...): its body
            # can still hold dispatch branches, so keep walking.
            self._walk_body(node.body, ctx)
            self._walk_body(node.orelse, ctx)
            return
        self._classified.add(id(node))
        self.shape_counts[outcome.shape] = \
            self.shape_counts.get(outcome.shape, 0) + 1
        templates = outcome.templates
        deferred = False
        if outcome.shape == "prefix":
            # A prefix branch either DELEGATES the tail to a handler this module
            # walks — in which case the callee's branches carry the routes — or
            # it is itself a real prefix route (``/api/{*0}``).
            if self._body_delegates_tail(node.body, outcome.subject):
                templates = ()
                deferred = True
            else:
                # TAIL0, not TAIL (#202 repair root cause 4, applied for
                # consistency to this OTHER source of a "free tail" template):
                # ``path.startswith("/api/")`` is true of "/api/" itself, an
                # EMPTY remainder -- the same possibly-vs-non-empty distinction
                # that separates ``.*`` from ``.+`` in a regex.
                templates = tuple(a.prefix + outcome.literal + TAIL0
                                  if a.is_free else a.fixed_template
                                  for a in (outcome.alts or ()))
        elif outcome.shape == "regex" and self._body_delegates_parsed(node.body):
            # #202 repair root cause 1: the body hands the captured groups
            # straight to a PARSED_DELEGATES callee that itself narrows them
            # further (e.g. a schema dict keyed on (entity, target)). The
            # WILDCARD family this regex would otherwise claim is not the
            # true reachable set -- the callee's walk (below, via
            # _maybe_delegate -> _delegate_parsed) emits the real, narrower
            # leaves instead. Emitting both would leave the false wildcard
            # standing ALONGSIDE the true leaves, which is the original
            # defect, not a fix for it.
            templates = ()
            deferred = True
        if outcome.shape not in ("present-group", "prefix") and not deferred \
                and not templates:
            # Nothing this branch tests for can reach it: the subject is already
            # constrained to shapes the test excludes. Dead dispatch code.
            self.unreachable.append(
                (ctx.handler, node.lineno, ast.unparse(node.test)))
        if templates:
            self._check_sibling_overlap(scope, set(templates), node, ctx)
        for template in sorted(set(templates)):
            self._emit(ctx, template, outcome.shape, node.lineno, node.test)
        child = ctx.child()
        if outcome.subject is not None and outcome.alts is not None \
                and outcome.shape != "prefix":
            child.bind_subject(outcome.subject, outcome.alts)
        self._walk_body(node.body, child)
        self._walk_terminal_else(node, ctx, outcome, scope)

    def _check_sibling_overlap(self, scope: dict, templates: set,
                               node: ast.If, ctx: _Ctx) -> None:
        """#202 repair round 2, finding C: raise when a SIBLING branch --
        not one NESTED inside another (the unreachable detector above
        already owns that case) -- claims a template an EARLIER sibling in
        the SAME dispatch scope already claimed, and that EARLIER sibling's
        body ALWAYS EXITS (see :meth:`_body_always_exits`) -- i.e. THIS
        branch is now provably dead code, exactly the reproduced shape:

        Two top-level ``if path == "/x": return self._send(1)`` /
        ``if path == "/x": return self._send(2)`` branches (the second
        provably dead code, unreachable after the first's unconditional
        return) used to produce ONE recorded route with no signal that a
        second, duplicate/dead branch exists -- silent, because the
        EXISTING unreachable detector only fires when a NESTED branch's
        subject is already narrowed by an ENCLOSING alternation; two
        top-level siblings narrow nothing, so their (identical) templates
        computed cleanly and the second simply lost the ``self.routes``
        ``setdefault`` race with no trace.

        The "earlier body always exits" gate is deliberate, not incidental:
        two siblings can legitimately share every key of a tuple-dict
        without either being dead code, when the first's body does NOT
        unconditionally exit -- the real ``_handle_reassign_v2`` has
        exactly this shape (``if combo in _V2_REASSIGN_SCHEMA:`` validates
        the body and only returns on FAILURE, falling through on success to
        an unrelated, independently-reachable ``dest =
        _V2_REASSIGN_DEST.get(combo); if dest is not None:`` authorisation-
        target lookup that happens to share every key). Flagging that
        pairing was a genuine over-broad failure, found and closed by this
        gate -- overlap alone is not ambiguity; overlap where the second
        claim can PROVABLY never be reached is.

        ``scope`` is fresh per :meth:`_walk_body` call (and threaded through
        an elif chain by :meth:`_walk_terminal_else`), so this only ever
        compares branches AT THE SAME DECISION POINT, never unrelated
        branches elsewhere in the function -- that breadth is deliberately
        not this check's job.
        """
        for template in sorted(templates):
            prior = scope.get(template)
            if prior is None:
                continue
            prior_lineno, prior_test, prior_always_exits = prior
            if not prior_always_exits:
                continue
            raise ExtractionError(
                f"{ctx.handler}:{node.lineno} tests `{ast.unparse(node.test)}`, "
                f"which claims {template!r} -- already claimed by "
                f"{ctx.handler}:{prior_lineno}'s `{prior_test}` in the same "
                "dispatch scope, whose body always exits, making this "
                "branch unreachable. Two sibling branches claiming the "
                "same route, the first provably dead-ending before the "
                "second could ever run, is an ambiguous overlap (often "
                "dead code after the first's unconditional return, or a "
                "copy/paste mistake that meant to test something else) -- "
                "resolve the duplication in server.py; a second, "
                "unreachable claim on an already-claimed route must not "
                "pass silently")
        for template in templates:
            scope.setdefault(
                template, (node.lineno, ast.unparse(node.test),
                          self._body_always_exits(node.body)))

    @staticmethod
    def _body_always_exits(body: list) -> bool:
        """Does this branch's body unconditionally ``return``/``raise`` on
        its last statement, so nothing textually after it in the SAME
        statement list can ever run once this branch is entered?

        Deliberately conservative -- a simple, common shape (the LAST
        statement is a bare ``return``/``raise``), not full control-flow
        reachability (e.g. an ``if``/``else`` where BOTH arms return is not
        recognised). A false NEGATIVE here just leaves
        :meth:`_check_sibling_overlap` silent for that shape -- no worse
        than before finding C. A false POSITIVE would wrongly call live,
        independently-reachable code "dead", which is the over-broad
        failure this module's fail-closed checks must never produce.
        """
        return bool(body) and isinstance(body[-1], (ast.Return, ast.Raise))

    def _walk_terminal_else(self, node: ast.If, ctx: _Ctx, outcome: _Outcome,
                            scope: dict):
        """Walk ``node.orelse`` -- continuing an elif CHAIN when it is one,
        or, at the chain's true bottom, checking whether the terminal
        ``else`` is itself an IMPLICIT route (#202 repair root cause 6: the
        unconditional static tail).

        ``_serve_static`` is ``if path in (a, b): rel = ... elif path in (c,
        d): rel = ... else: rel = path.lstrip("/")`` -- an if/elif chain over
        LITERAL(-set) tests of a tracked subject, whose terminal else
        re-derives a NEW value FROM that same subject via string surgery
        (``_PATH_OPS``), rather than being unreachable, a ``pass``, or an
        error. That is not "nothing left to say" the way an ordinary
        ``if`` with no ``else`` is -- every path NOT in the listed literals
        still reaches this else and is served (or 404s) from it, which is
        exactly what the ``{*}`` wildcard family already means for the
        `/api/games/{}/{*}` action family and the `/api/{*}` fallthrough. Left
        unmodelled, real files under ``web/static/`` are reachable but
        omitted from the inventory entirely.
        """
        orelse = node.orelse
        if len(orelse) == 1 and isinstance(orelse[0], ast.If):
            # An elif ARM continuing this chain shares the SAME sibling-
            # overlap scope (#202 repair round 2, finding C) as the chain's
            # earlier arms -- they are exactly the "siblings ... in the same
            # dispatch scope" finding C means, just spelled as `elif`
            # instead of a second top-level `if`. `_walk_body`'s call for
            # `orelse` below (the chain's NON-elif tail) still gets its own
            # fresh scope, same as any other nested body.
            self._walk_if(orelse[0], ctx, scope)
            return
        self._walk_body(orelse, ctx)
        if outcome.shape not in ("literal", "literal-set") or not outcome.subject:
            return
        if not self._else_rederives_subject(orelse, outcome.subject, ctx.handler):
            return
        for base in ctx.subjects.get(outcome.subject, ()):
            if not base.is_free:
                continue
            # A REQUEST PATH always starts with "/" (HTTP protocol, not this
            # module's own invention); the subject here is the WHOLE
            # remaining path with no literal text of its own before it
            # (base.prefix == ""), so the leading "/" has to be supplied
            # explicitly rather than coming from an enclosing literal the way
            # it does for every other TAIL usage (``/api/{*}``, where the
            # "/api/" IS the literal prefix).
            template = base.prefix + TAIL + base.suffix
            if not template.startswith("/"):
                template = "/" + template
            self._emit(ctx, template, "static-tail", node.lineno, node.test)

    def _else_rederives_subject(self, orelse: list, subject: str,
                                fn_name: str) -> bool:
        # No `parents` map passed (#202 repair round 4, finding 3): this
        # runs during the main WALK, before _audit_function builds one for
        # the function being audited, and this narrow, terminal-else-only
        # check consults no waiver in the shipped server.py today --
        # _waiver_key degrades to an empty enclosing_if_text rather than
        # requiring one, so a future waiver reached this way still gets the
        # relocation-detecting parent_shape half of the fingerprint.
        for stmt in orelse:
            if isinstance(stmt, ast.Assign) \
                    and _propagates_taint(stmt.value, {subject}, fn_name,
                                          self.waiver_hits, None,
                                          self._followed):
                return True
        return False

    def _body_delegates_tail(self, body: list, subject: Optional[str]) -> bool:
        for stmt in body:
            for node in ast.walk(stmt):
                if (isinstance(node, ast.Call)
                        and isinstance(node.func, ast.Attribute)
                        and isinstance(node.func.value, ast.Name)
                        and node.func.value.id == "self"
                        and node.func.attr in TAIL_DELEGATES
                        and node.args):
                    sliced = self._slice_prefix(node.args[0])
                    if sliced is not None and sliced[0] == subject:
                        return True
        return False

    def _body_delegates_parsed(self, body: list) -> bool:
        """Does this ``if <regex match>:`` body hand off to a PARSED_DELEGATES
        callee? (#202 repair root cause 1.) Any such call, in any statement
        form ``_maybe_delegate`` walks (a ``return``/bare-expression call, the
        only forms server.py uses) is enough -- the callee's OWN walk is what
        proves whether its groups are further narrowed; this only decides
        whether to defer to it instead of emitting the caller's wildcard.
        """
        for stmt in body:
            for node in ast.walk(stmt):
                if (isinstance(node, ast.Call)
                        and isinstance(node.func, ast.Attribute)
                        and isinstance(node.func.value, ast.Name)
                        and node.func.value.id == "self"
                        and node.func.attr in PARSED_DELEGATES):
                    return True
        return False

    def _classify(self, test, ctx: _Ctx) -> Optional[_Outcome]:
        # if <matchvar>:  (the regex was assigned on a previous line)
        if isinstance(test, ast.Name) and test.id in ctx.matches:
            return self._regex_outcome(ctx.matches[test.id], ctx, test)
        # if <dictvar>:   ({...}.get(subject))
        if isinstance(test, ast.Name) and test.id in ctx.dicts:
            subject, keys = ctx.dicts[test.id]
            alts = self._select(ctx, subject, keys)
            return _Outcome("dict-key", subject, self._templates(alts), alts)
        # if <lookupvar>:  (bound earlier from SCHEMA.get(combo)/SCHEMA[combo])
        # #202 repair root cause 1.
        if isinstance(test, ast.Name) and test.id in ctx.tuple_lookups:
            dict_name, tuple_name = ctx.tuple_lookups[test.id]
            return self._tuple_dict_outcome(ctx, tuple_name, dict_name, test)
        # if re.match(...):  (inline)
        info = self._as_regex_call(test, ctx)
        if info is not None:
            return self._regex_outcome(info, ctx, test)
        if isinstance(test, ast.Compare) and len(test.ops) == 1:
            left, op, right = test.left, test.ops[0], test.comparators[0]
            if isinstance(left, ast.Name) and left.id in ctx.subjects:
                subject = left.id
                if isinstance(op, ast.Eq) and isinstance(right, ast.Constant):
                    alts = self._select(ctx, subject, (right.value,))
                    return _Outcome("literal", subject,
                                    self._templates(alts), alts)
                if isinstance(op, ast.In) and isinstance(
                        right, (ast.Tuple, ast.List, ast.Set)):
                    values = []
                    for elt in right.elts:
                        if not (isinstance(elt, ast.Constant)
                                and isinstance(elt.value, str)):
                            raise ExtractionError(
                                "non-literal member in a path `in` test at "
                                f"line {test.lineno}")
                        values.append(elt.value)
                    alts = self._select(ctx, subject, tuple(values))
                    return _Outcome("literal-set", subject,
                                    self._templates(alts), alts)
                if isinstance(op, ast.Is) and isinstance(right, ast.Constant) \
                        and right.value is None:
                    alts = tuple(a for a in ctx.subjects[subject]
                                 if a.value is None)
                    return _Outcome("absent-group", subject,
                                    self._templates(alts), alts)
                if isinstance(op, ast.IsNot) and isinstance(right, ast.Constant) \
                        and right.value is None:
                    alts = tuple(a for a in ctx.subjects[subject]
                                 if a.value is not None)
                    return _Outcome("present-group", subject, (), alts)
                raise ExtractionError(
                    f"unsupported comparison on dispatch subject {subject!r} at "
                    f"line {test.lineno}: {ast.unparse(test)}")
            # if combo in SCHEMA:  -- #202 repair root cause 1. ``combo`` is a
            # tuple of tracked names (see _as_tuple_of_subjects); ``SCHEMA``
            # a literal dict keyed on same-arity tuples (see
            # _as_tuple_keyed_dict). Each key enumerates one concrete leaf.
            if isinstance(left, ast.Name) and left.id in ctx.tuples:
                tuple_name = left.id
                if isinstance(op, ast.In) and isinstance(right, ast.Name) \
                        and right.id in ctx.tuple_dicts:
                    return self._tuple_dict_outcome(
                        ctx, tuple_name, right.id, test)
                # if combo == ("team", "league"):  -- a single-key equality
                # test on the tuple (server.py uses this for a combo-specific
                # validation rule; it selects the SAME leaf its schema entry
                # already enumerates, not a new one, but it must still be
                # PROVEN reachable and correlated, not assumed).
                if isinstance(op, ast.Eq) and isinstance(right, ast.Tuple):
                    values = []
                    for elt in right.elts:
                        if not isinstance(elt, ast.Constant):
                            raise ExtractionError(
                                "non-literal element in a tuple dispatch "
                                f"equality at line {test.lineno}")
                        values.append(elt.value)
                    return self._tuple_keys_outcome(
                        ctx, tuple_name, (tuple(values),), test)
                raise ExtractionError(
                    "unsupported comparison on tuple dispatch subject "
                    f"{tuple_name!r} at line {test.lineno}: "
                    f"{ast.unparse(test)}")
            # if dest is not None:  -- the same enumeration as ``combo in
            # SCHEMA``, reached via a pre-bound SCHEMA.get(combo)/SCHEMA[combo]
            # local instead of an inline ``in`` test.
            if isinstance(left, ast.Name) and left.id in ctx.tuple_lookups:
                dict_name, tuple_name = ctx.tuple_lookups[left.id]
                if isinstance(op, ast.IsNot) and isinstance(right, ast.Constant) \
                        and right.value is None:
                    return self._tuple_dict_outcome(
                        ctx, tuple_name, dict_name, test)
                raise ExtractionError(
                    "unsupported comparison on tuple-dict lookup "
                    f"{left.id!r} at line {test.lineno}: {ast.unparse(test)}")
        if isinstance(test, ast.Call) and isinstance(test.func, ast.Attribute) \
                and isinstance(test.func.value, ast.Name) \
                and test.func.value.id in ctx.subjects:
            subject = test.func.value.id
            attr = test.func.attr
            if not (len(test.args) == 1 and isinstance(test.args[0], ast.Constant)):
                raise ExtractionError(
                    f"unsupported {attr}() dispatch at line {test.lineno}")
            literal = test.args[0].value
            if attr == "startswith":
                alts = tuple(a for a in ctx.subjects[subject]
                             if a.is_free
                             or str(a.value).startswith(literal))
                return _Outcome("prefix", subject, (), alts, literal)
            if attr == "endswith":
                alts = tuple(a for a in ctx.subjects[subject]
                             if not a.is_free and str(a.value).endswith(literal))
                if any(a.is_free for a in ctx.subjects[subject]):
                    raise ExtractionError(
                        f"endswith() on a free path subject at line "
                        f"{test.lineno}: the route shape is not expressible")
                return _Outcome("endswith", subject,
                                self._templates(alts), alts)
            raise ExtractionError(
                f"unsupported method {attr}() on dispatch subject {subject!r} "
                f"at line {test.lineno}")
        return None

    def _regex_outcome(self, info, ctx: _Ctx, test) -> _Outcome:
        subject, pattern, expansions = info
        templates = []
        for base in ctx.subjects[subject]:
            if not base.is_free:
                raise ExtractionError(
                    f"regex dispatch on the already-constrained subject "
                    f"{subject!r} at line {test.lineno}")
            for exp in expansions:
                templates.append(base.prefix + exp.template + base.suffix)
        # No refinement: the body binds m.group(N), which needs the subject to
        # stay free so the group's own Alts can be derived from the pattern.
        return _Outcome("regex", None, tuple(templates), None)

    def _tuple_dict_outcome(self, ctx: _Ctx, tuple_name: str, dict_name: str,
                            test) -> Optional[_Outcome]:
        """``if combo in SCHEMA:`` (or the ``.get()``/``[]``-then-truthy
        equivalent) -> one concrete leaf PER KEY of ``SCHEMA``. See
        :meth:`_tuple_keys_outcome` for the shared enumeration this and the
        single-key-equality form (``combo == (...)``) both reduce to.

        This is the fix for the 13-for-13 substitution: ``_handle_reassign``'s
        own ``_V1_REASSIGN_SCHEMA``/``_V2_REASSIGN_SCHEMA`` are exactly this
        shape, and their key sets are the true reachable (entity, target)
        pairs -- the wildcard the caller's bare regex would otherwise claim.
        """
        return self._tuple_keys_outcome(
            ctx, tuple_name, ctx.tuple_dicts[dict_name], test, dict_name)

    def _tuple_keys_outcome(self, ctx: _Ctx, tuple_name: str, keys: tuple,
                            test, keys_source: str = "") -> Optional[_Outcome]:
        """Core enumeration (#202 repair root cause 1): ``tuple_name``'s
        components, all traced back to the SAME regex match, checked against
        each key in ``keys`` (same arity) -> one concrete leaf per matching
        key.

        A component a key holds a LITERAL for narrows that segment (e.g.
        "organization" replaces the `target` capture's own `{}`/`{w}`
        rendering); a component whose OWN captured value is free (record_id,
        an opaque id) is left exactly as the match already renders it -- the
        one genuine wildcard segment, never enumerated.

        Returns ``None`` (fails closed via the completeness audit, since
        every name involved is already tracked) when the components do not
        all share one match: correlating them positionally would otherwise
        be a guess, not a proof.
        """
        components = ctx.tuples[tuple_name]
        origins = [ctx.origins.get(name) for name in components]
        if any(o is None for o in origins):
            return None
        bases = {id(o[0]) for o in origins}
        expansions_ids = {id(o[1]) for o in origins}
        if len(bases) != 1 or len(expansions_ids) != 1:
            return None
        base = origins[0][0]
        expansions = origins[0][1]
        group_indices = [o[2] for o in origins]
        if any(len(key) != len(components) for key in keys):
            label = keys_source or "the tuple literal"
            raise ExtractionError(
                f"{label}'s keys do not match {tuple_name}={components!r} "
                f"in arity at line {test.lineno}")
        templates = []
        for exp in expansions:
            by_group = {p.group: p for p in exp.parts}
            current = []
            for gi in group_indices:
                part = by_group.get(gi)
                if part is None:
                    current.append(None)          # an absent optional group
                elif part.kind == "lit":
                    current.append(part.text)
                else:
                    current.append(FREE)
            for key in keys:
                if not all(cur == FREE or cur == val
                          for cur, val in zip(current, key)):
                    continue
                overrides = {gi: val for gi, cur, val in
                            zip(group_indices, current, key) if cur is FREE}
                rendered = "".join(
                    overrides[p.group] if p.group in overrides else p.render()
                    for p in exp.parts)
                templates.append(base.prefix + rendered + base.suffix)
        return _Outcome("tuple-dict-key", None, tuple(templates), None)

    @staticmethod
    def _templates(alts) -> tuple:
        return tuple(a.fixed_template for a in alts
                     if a.fixed_template is not None)

    def _select(self, ctx: _Ctx, subject: str, values) -> tuple:
        """The Alts selected by comparing ``subject`` against ``values``.

        An empty result means the branch can never run: the subject is already
        constrained to shapes none of these values can take.
        """
        alts = []
        for value in values:
            for base in ctx.subjects[subject]:
                if base.is_free:
                    alts.append(Alt(base.prefix, base.suffix, value))
                elif base.value == value:
                    alts.append(base)
        return tuple(alts)

    # -- emission ----------------------------------------------------------
    def _emit(self, ctx: _Ctx, template: str, shape: str, lineno: int, test):
        if not template.startswith("/") and template != "":
            raise ExtractionError(
                f"derived template {template!r} is not an absolute path "
                f"(line {lineno}) — a prefix was lost")
        route = LiveRoute(ctx.method, template, ctx.handler, shape, lineno,
                          ast.unparse(test))
        self.routes.setdefault(route.key, route)

    # -- delegation --------------------------------------------------------
    def _maybe_delegate(self, value, ctx: _Ctx):
        for node in ast.walk(value):
            if not (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and isinstance(node.func.value, ast.Name)
                    and node.func.value.id == "self"):
                continue
            name = node.func.attr
            if name in TAIL_DELEGATES:
                self._followed.add(id(node))
                self._delegate_tail(name, node, ctx)
            elif name in SAME_PATH_DELEGATES:
                self._followed.add(id(node))
                self._delegate_same_path(name, node, ctx)
            elif name in PARSED_DELEGATES:
                self._followed.add(id(node))
                self._delegate_parsed(name, node, ctx)

    def _delegate_tail(self, name: str, call: ast.Call, ctx: _Ctx):
        if not call.args:
            raise ExtractionError(f"{name}() called with no path tail")
        first = call.args[0]
        prefix = self._slice_prefix(first)
        if prefix is None:
            raise ExtractionError(
                f"{name}() first argument is not path[len('...'):] "
                f"at line {call.lineno}")
        subject_name, literal = prefix
        if subject_name not in ctx.subjects:
            raise ExtractionError(
                f"{name}() slices {subject_name!r}, which is not a dispatch "
                f"subject at line {call.lineno}")
        fn = self.functions.get(name)
        if fn is None:
            raise ExtractionError(f"{name} not found on Handler")
        param = fn.args.args[1].arg  # args[0] is self
        alts = []
        for base in ctx.subjects[subject_name]:
            if not base.is_free:
                raise ExtractionError(
                    f"{name}() reached with a constrained subject")
            alts.append(Alt(base.prefix + literal, base.suffix, FREE))
        child = _Ctx(ctx.method, name)
        child.bind_subject(param, alts)
        self._walk_function(fn, child)

    def _delegate_same_path(self, name: str, call: ast.Call, ctx: _Ctx):
        fn = self.functions.get(name)
        if fn is None:
            raise ExtractionError(f"{name} not found on Handler")
        params = [a.arg for a in fn.args.args[1:]]
        if call.args:
            if not (isinstance(call.args[0], ast.Name)
                    and call.args[0].id in ctx.subjects):
                raise ExtractionError(
                    f"{name}() called with something other than the dispatch "
                    f"subject at line {call.lineno}")
            alts = ctx.subjects[call.args[0].id]
            subject = params[0]
        else:
            # The callee re-derives the path from self.path.
            alts = ctx.subjects.get("path", (Alt(),))
            subject = "path"
        child = _Ctx(ctx.method, name)
        child.bind_subject(subject, alts)
        self._walk_function(fn, child)

    def _delegate_parsed(self, name: str, call: ast.Call, ctx: _Ctx):
        """Walk a PARSED_DELEGATES callee with each of its parameters bound
        from the corresponding ``m.group(K)`` call-site argument (#202
        repair root cause 1).

        This is the fix for the 13-for-13 substitution: before it, following
        one of these calls satisfied only the bookkeeping check in
        :meth:`_audit_function` ("is this a known helper") -- the callee was
        never actually WALKED, so ``entity``/``record_id``/``target`` were
        untracked inside it, a ``combo = (entity, target)`` tuple built from
        them was invisible, and a dict-membership test on that tuple was
        neither classified nor audited. The outer regex's own wildcard
        survived as the only representation. Binding each argument here (via
        :meth:`_group_origin`, which also records which match/group it came
        from) is what lets :meth:`_tuple_dict_outcome` later prove several
        bound names are POSITIONALLY correlated rather than an arbitrary
        cross-product.

        An argument that is not itself an ``m.group(K)`` call (``body``,
        ``actor_id``, ``role``, ``scope``: the non-path parameters) is simply
        left unbound -- it is not path-derived, so there is nothing to track.
        """
        fn = self.functions.get(name)
        if fn is None:
            raise ExtractionError(f"{name} not found on Handler")
        params = [a.arg for a in fn.args.args[1:]]  # args[0] is self
        if len(call.args) < len(params):
            raise ExtractionError(
                f"{name}() called with fewer positional arguments than it "
                f"declares at line {call.lineno}")
        child = _Ctx(ctx.method, name)
        for param, arg in zip(params, call.args):
            grp = self._as_group_call(arg, ctx)
            if grp is None:
                continue
            child.bind_subject(param, grp)
            origin = self._group_origin(arg, ctx)
            if origin is not None:
                child.origins[param] = origin
        self._walk_function(fn, child)

    @staticmethod
    def _slice_prefix(node):
        """``path[len("/api/setup/"):]`` -> ("path", "/api/setup/")."""
        if not (isinstance(node, ast.Subscript)
                and isinstance(node.value, ast.Name)
                and isinstance(node.slice, ast.Slice)
                and node.slice.upper is None and node.slice.step is None):
            return None
        lower = node.slice.lower
        if not (isinstance(lower, ast.Call)
                and isinstance(lower.func, ast.Name) and lower.func.id == "len"
                and len(lower.args) == 1
                and isinstance(lower.args[0], ast.Constant)):
            return None
        return (node.value.id, lower.args[0].value)

    # -- the completeness audit -------------------------------------------
    def _audit_function(self, fn: ast.FunctionDef, ctx: _Ctx):
        """Fail if any ``if`` in this function touches a path-bearing name in a
        way the walker did not classify, or if it calls an unknown dispatch
        helper. This is what makes "the extractor sees every branch" checkable
        rather than hopeful."""
        # Delegation/unknown-dispatch-helper calls are checked FIRST, before
        # taint propagation runs (#202 repair round 2, finding A interaction):
        # this check needs nothing but `self._followed` (fully populated by
        # the walk, long before this audit runs) and is far more SPECIFIC
        # than the generic "unlisted call touches a tracked name" check
        # _propagates_taint now performs. An unfollowed delegate assigned to
        # a local first (`answer = self._handle_setup(...)`) is BOTH "a
        # delegation in a form the walker does not follow" AND "an unlisted
        # call whose argument includes a tracked name" -- the former names
        # the actual mistake and must win the race, not be pre-empted by the
        # latter, coarser diagnosis merely because taint propagation happens
        # to run its scan first.
        self._audit_dispatch_helper_calls(fn)
        tracked = set(ctx.seen) | {"path"}
        # #202 repair round 5, finding 2b: the SUBSET of `tracked` that is a
        # CAPTURED SUBJECT ANYWHERE in this function's own walk --
        # ``ctx.captured``, populated by ``bind_subject`` for a regex-group
        # capture (directly, or a TAIL_DELEGATES/PARSED_DELEGATES parameter
        # carrying one across a delegation boundary -- see ``_Ctx.captured``'s
        # own docstring for why this is a DEDICATED, ``seen``-like SHARED
        # field rather than reusing ``ctx.subjects`` itself, which is
        # COPIED per nested scope and so goes empty the moment the
        # enclosing ``if <the regex match>:`` block closes -- the near-
        # universal shape a capture is actually written in) -- threaded
        # into `_propagates_taint` so it can exempt a non-`self.` call
        # whose only tracked mentions are already-captured data.
        # Deliberately NOT ``ctx.origins`` (a narrower, positional-
        # correlation record for PARSED_DELEGATES specifically, populated
        # only when `_group_origin` additionally succeeds -- empty for an
        # ordinary top-level capture like ``jid = mgo.group(1)``,
        # DEMONSTRATED to leave the exemption below inert for exactly the
        # common case it exists for) and deliberately NOT the broader
        # ``ctx.seen`` (which also contains ``combo``-style tuples and
        # ``dest``/``parent``-style tuple-dict lookups -- names whose OWN
        # comparison genuinely IS a routing input, unlike a plain captured
        # id -- see `_propagates_taint`'s own docstring for the full
        # reasoning behind this exact set). ``- {"path"}`` UNCONDITIONALLY,
        # even though ``bind_subject`` itself never binds the literal name
        # "path" on purpose: DEMONSTRATED that a SAME_PATH_DELEGATES
        # callee -- ``_serve_static``, whose own parameter happens to ALSO
        # be named ``path`` -- receives it via exactly this mechanism
        # (``child.bind_subject(param, alts)`` in
        # :meth:`_delegate_same_path`, keyed on the CALLEE's parameter
        # name, not on any "is this literally the request path" test), so
        # ``ctx.captured`` DOES contain "path" for that function's own
        # audit. ``tracked``'s primary subject must never be treated as
        # merely-captured data under any circumstance, in ANY function --
        # this subtraction is the explicit, unconditional guarantee of
        # that, independent of whichever binding path put "path" in
        # ``ctx.captured`` this time.
        captured_names = set(ctx.captured) - {"path"}
        # #202 repair round 4, finding 3: built ONCE per audited function and
        # threaded into every waiver-key computation below (directly, and via
        # _propagates_taint) -- see _waiver_key's own docstring for what it
        # is for and why it costs one linear pass per function, not per node.
        parents = _build_parent_map(fn)
        # #202 repair round 10 (external review): ALSO built ONCE per
        # audited function, the SAME discipline as `parents` immediately
        # above -- which _CAPTURED_ARG_SAFE_CALLEE_ROOTS names THIS
        # function's own body proves are bound, dominating and unrebound,
        # to the exact source expression each is allowlisted for (see
        # _captured_arg_trusted_roots's own docstring). Threaded into every
        # _propagates_taint call below the same way captured_names already
        # is -- a name spelled "api" earns the captured-arg exemption in
        # THIS function only when it is a member of THIS set, never merely
        # by matching _CAPTURED_ARG_SAFE_CALLEE_ROOTS directly.
        trusted_roots = _captured_arg_trusted_roots(fn, parents)
        # TAINT PROPAGATION. Any local bound from the path — directly, sliced,
        # or from another tainted local — joins the tracked set, so renaming it
        # cannot hide a branch. Iterated to a fixed point because one rename can
        # feed another.
        changed = True
        while changed:
            changed = False
            for node in ast.walk(fn):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                        and node is not fn:
                    # #202 repair round 6, finding 2: a LOCALLY-DEFINED
                    # closure -- nested directly inside the audited
                    # function, reading an outer name through Python's own
                    # closure mechanism rather than receiving it as a
                    # syntactic argument -- is invisible to every call-
                    # auditing mechanism in this module: _mentions_tracked
                    # only ever looks at a Call's OWN visible receiver and
                    # arguments, and a call to a closure that reads a
                    # tracked FREE VARIABLE mentions nothing tracked in its
                    # own syntax at all, regardless of how many arguments
                    # it takes. DEMONSTRATED: ``def compute(): return
                    # path`` then ``candidate = compute()`` answered live
                    # HTTP 200 while extraction stayed silent --
                    # ``compute()``'s call site has no argument for any
                    # existing check to examine.
                    # Treated as an IMPLICIT binding, the closure's own
                    # NAME "bound from" its whole body: if the closure
                    # mentions ANYTHING tracked anywhere in it
                    # (``_mentions_tracked`` -- the SAME opaque-extraction-
                    # aware check every other consumer in this module
                    # uses, so a closure that only touches a captured/
                    # opaque value stays clean, same as everywhere else),
                    # its name joins ``tracked`` through this SAME fixed-
                    # point loop -- so a LATER call to it, however it is
                    # spelled or how many arguments it takes, is audited
                    # exactly like any other tracked reference, with zero
                    # new call-shape logic needed. Re-checked every pass
                    # (not just once) the same way every other binding
                    # here is, so a closure that itself calls ANOTHER
                    # closure only provably tracked on a later pass still
                    # converges correctly.
                    if node.name not in tracked and _mentions_tracked(node, tracked):
                        tracked.add(node.name)
                        changed = True
                    continue
                # #202 repair round 4, finding 2: every binding form this
                # module tracks -- ``ast.Assign``/``ast.AnnAssign``, (round
                # 3 finding F) ``ast.NamedExpr``, and (this round)
                # ``ast.AugAssign``/``ast.For``/``ast.AsyncFor``/
                # ``ast.comprehension``/``ast.withitem`` -- funnels through
                # ONE extraction function so the propagation logic below
                # (the actual taint question) is written once. See
                # :func:`_binding_value_and_targets`'s own docstring for why
                # each shape belongs here and what stays deliberately out of
                # scope.
                binding = _binding_value_and_targets(node)
                if binding is None:
                    continue
                value, targets = binding
                leaves = [leaf for target in targets
                         for leaf in ast.walk(target)
                         if isinstance(leaf, ast.Name)]
                if leaves and all(leaf.id in tracked for leaf in leaves):
                    # Every name this assignment binds is ALREADY tracked --
                    # almost always because _record_binding's own richer,
                    # ctx-aware recognisers (a regex match, a `{...}.get()`,
                    # a tuple-dict lookup, a direct `m.group(K)` call, ...)
                    # classified it as a proper dispatch subject during the
                    # walk, well before this audit runs (ctx.seen is fully
                    # populated by then). Nothing to learn by re-deriving a
                    # coarse yes/no from the bare syntax here -- and (#202
                    # repair round 2, finding A) _propagates_taint's own
                    # unlisted-call check now RAISES on exactly these shapes
                    # (`re.match(...)`, `{...}.get(subject)`,
                    # `SCHEMA.get(combo)`) examined in isolation, precisely
                    # BECAUSE it cannot see the richer, ctx-aware reasoning
                    # that already cleared them. Skip re-deriving what
                    # _record_binding already resolved rather than force
                    # this coarse, flat-namespace check to duplicate it.
                    continue
                derived = _propagates_taint(value, tracked, fn.name,
                                            self.waiver_hits, parents,
                                            self._followed, captured_names,
                                            trusted_roots)
                if not derived:
                    continue
                for leaf in leaves:
                    if leaf.id not in tracked:
                        tracked.add(leaf.id)
                        changed = True
        for node in ast.walk(fn):
            if isinstance(node, ast.If) and id(node) not in self._classified:
                names = _direct_operand_names(node.test, tracked)
                hit = names & tracked
                key = _waiver_key(fn.name, node.test, parents)
                if hit and key in _AUDIT_WAIVERS:
                    self._record_waiver_hit(key, node)
                    continue
                if hit:
                    raise ExtractionError(
                        f"{fn.name}:{node.lineno} tests dispatch subject(s) "
                        f"{sorted(hit)} in an unrecognised shape: "
                        f"{ast.unparse(node.test)}")
            if isinstance(node, ast.IfExp):
                # A TERNARY (#202 repair, invented-evasion track): ``ast.If``
                # is a STATEMENT; ``ast.IfExp`` (``a if TEST else b``) is a
                # different node type the scan above never matches, so a
                # ternary testing a tracked subject was DEMONSTRATED to be
                # invisible even when inlined straight into a ``return`` --
                # never bound to a local at all, so there is no assignment
                # for taint-propagation to have flagged either. Ternaries are
                # not modelled as routing decisions anywhere in this module;
                # server.py's own two real examples pick a BACKEND FUNCTION
                # for a route the enclosing regex's own alternation already
                # fully enumerates, not a new route -- exactly the
                # "decides SERVE, not the route" shape the waiver list
                # already exists for, so it goes through the SAME waivers.
                names = _direct_operand_names(node.test, tracked)
                hit = names & tracked
                key = _waiver_key(fn.name, node.test, parents)
                if hit and key in _AUDIT_WAIVERS:
                    self._record_waiver_hit(key, node)
                    continue
                if hit:
                    raise ExtractionError(
                        f"{fn.name}:{node.lineno} a ternary tests dispatch "
                        f"subject(s) {sorted(hit)} -- route_extract does not "
                        "model ternaries; rewrite as if/elif")
            if isinstance(node, ast.While):
                # #202 repair round 2, finding B: `_walk_stmt`'s own
                # (Try, With, For, While) handling walks a `while` loop's
                # BODY (so a route nested further inside one is still
                # found), but neither that walk nor this scan (which, until
                # now, only matched ast.If/ast.IfExp) ever looked at the
                # loop's OWN `.test` -- a `while path == "/new-route":`
                # guard produced ZERO recorded routes and ZERO exceptions,
                # DEMONSTRATED. Same shape as the ast.If case above, just a
                # different statement type; goes through the SAME waivers
                # (none needed today -- server.py has no `while` at all).
                names = _direct_operand_names(node.test, tracked)
                hit = names & tracked
                key = _waiver_key(fn.name, node.test, parents)
                if hit and key in _AUDIT_WAIVERS:
                    self._record_waiver_hit(key, node)
                    continue
                if hit:
                    raise ExtractionError(
                        f"{fn.name}:{node.lineno} a while loop tests "
                        f"dispatch subject(s) {sorted(hit)} in an "
                        f"unrecognised shape: {ast.unparse(node.test)}")
            if isinstance(node, (ast.For, ast.AsyncFor)):
                # #202 repair round 7, finding 1: a for/async-for loop's OWN
                # `.iter` can decide whether its body executes AT ALL -- zero
                # iterations vs one or more -- exactly the same "does this
                # gate a branch" question `ast.If`/`ast.While`'s own `.test`
                # already answers for, just spelled as a loop instead of a
                # conditional. `_binding_value_and_targets` (see its own
                # docstring) already threads `.iter` through the fixed-point
                # taint loop ABOVE this scan -- but ONLY to decide whether
                # the loop's TARGET becomes tainted, a genuinely SEPARATE
                # question from "is EXECUTION of the body gated on a tracked
                # name", which nothing anywhere in this module asked before
                # now: a target that is never read again (a throwaway `_`)
                # leaves that taint-propagation use with nothing to trip any
                # LATER check, even though the loop's own presence/absence
                # of iterations already decided the response.
                # DEMONSTRATED (the reviewer's own repro): `for _ in (1,) *
                # (path == "/api/hidden"): return 200` -- a path-derived
                # boolean used as a tuple REPEAT COUNT, so the loop body
                # runs zero or one times depending on `path` -- answered
                # live HTTP 200 for the hidden path and 404 for every other
                # one, while `extract_routes` recorded ZERO routes and
                # raised nothing. Reuses `_direct_operand_names` -- the SAME
                # name-resolving walk the If/While/Assert/match-case cases
                # above already use, including its own default-deny fallback
                # (round 5 finding 5) for a node shape (here, an
                # `ast.BinOp`) none of its explicit branches special-case --
                # over `.iter` in place of `.test`, so a comparison, a
                # boolop, a helper-predicate call, or any other shape those
                # checks already recognise is recognised here identically.
                # Same shape as the ast.While case above, just a different
                # statement type (covering AsyncFor for the identical
                # reason no other check in this module special-cases sync
                # vs async); goes through the SAME waivers (none needed
                # today -- server.py's own six real For loops either name a
                # fixed module constant/parameter never derived from a
                # tracked name, or -- see `_audit_dispatch_helper_calls` and
                # this function's own fixed-point loop above -- iterate a
                # local this module already tracks through the ordinary
                # taint mechanism with no ADDITIONAL execution-control
                # meaning of its own).
                names = _direct_operand_names(node.iter, tracked)
                hit = names & tracked
                key = _waiver_key(fn.name, node.iter, parents)
                if hit and key in _AUDIT_WAIVERS:
                    self._record_waiver_hit(key, node)
                    continue
                if hit:
                    kind = ("an async for" if isinstance(node, ast.AsyncFor)
                            else "a for")
                    raise ExtractionError(
                        f"{fn.name}:{node.lineno} {kind} loop's iterable "
                        f"tests dispatch subject(s) {sorted(hit)} in an "
                        f"unrecognised shape: {ast.unparse(node.iter)}")
            if isinstance(node, ast.Assert):
                # #202 repair round 5, finding 6a: an ``assert`` is a
                # CONTROL-TRANSFER expression exactly like an ``ast.If``'s
                # own test -- a failing assert raises ``AssertionError``,
                # transferring control away from the rest of the block,
                # the same branch-shaped decision an ``if`` makes by
                # returning early instead. `_walk_stmt`'s own ``Try``
                # handling walks a try BODY (so a route nested further
                # inside one is still found), but neither that walk nor
                # this scan (which, until now, only matched ast.If/
                # ast.IfExp/ast.While/match-case guards) ever looked at an
                # Assert's OWN `.test`. DEMONSTRATED (the reviewer's own
                # repro): ``try: assert path == "/api/hidden" \n except
                # AssertionError: return 404 \n return 200`` answered live
                # HTTP 200/404 on the two sides of that assert while
                # extraction recorded zero routes and raised nothing. Same
                # shape as the ast.If/ast.While cases above, just a
                # different statement type; goes through the SAME waivers
                # (none needed today -- server.py has no ``assert`` on any
                # tracked subject).
                names = _direct_operand_names(node.test, tracked)
                hit = names & tracked
                key = _waiver_key(fn.name, node.test, parents)
                if hit and key in _AUDIT_WAIVERS:
                    self._record_waiver_hit(key, node)
                    continue
                if hit:
                    raise ExtractionError(
                        f"{fn.name}:{node.lineno} an assert tests "
                        f"dispatch subject(s) {sorted(hit)} in an "
                        f"unrecognised shape: {ast.unparse(node.test)}")
            if hasattr(ast, "match_case") and isinstance(node, ast.match_case) \
                    and node.guard is not None:
                # #202 repair round 4, finding 1: a ``match``/``case`` GUARD
                # (``case _ if path == "...":``) is an EXPRESSION on its own
                # node type -- neither ``ast.If`` nor ``ast.IfExp`` -- so the
                # scans above never matched it. `_walk_stmt`'s own Match
                # handling (#202 repair, invented-evasion track) already
                # raises when the match SUBJECT itself is tracked, but that
                # is a DIFFERENT check on a DIFFERENT expression; a guard on
                # an UNTAINTED subject (``match mode: case _ if path ==
                # "...":``) reached neither check and was DEMONSTRATED
                # silent: zero exception, zero route. Same shape as the
                # ast.If case above, just a different statement type; goes
                # through the SAME waivers (none needed today -- server.py
                # has no ``match`` statement at all, case guard or otherwise).
                names = _direct_operand_names(node.guard, tracked)
                hit = names & tracked
                key = _waiver_key(fn.name, node.guard, parents)
                if hit and key in _AUDIT_WAIVERS:
                    self._record_waiver_hit(key, node)
                    continue
                if hit:
                    # ast.match_case itself carries no .lineno (unlike every
                    # other statement/expression node in this module) -- its
                    # own guard expression does.
                    raise ExtractionError(
                        f"{fn.name}:{node.guard.lineno} a match-case guard "
                        f"tests dispatch subject(s) {sorted(hit)} in an "
                        f"unrecognised shape: {ast.unparse(node.guard)}")
        # #202 repair round 5, finding 2b: the bare-Expr/Return scan below
        # runs as its OWN, SEPARATE walk, AFTER the If/IfExp/While/
        # match-case scan above has finished over the WHOLE function --
        # deliberately not folded into the SAME loop any more (it used to
        # be). `_propagates_taint`'s "unlisted call" check is a coarse,
        # GENERIC diagnosis; If/IfExp/While/match-case each have their OWN
        # more SPECIFIC message (e.g. the ternary-specific "route_extract
        # does not model ternaries" a bare ``ast.If`` scan can't produce).
        # A ternary directly inside a ``return``/bare-Expr statement --
        # ``return self._send(1 if self._is_hidden(path) else 2)``, an
        # entirely ordinary Python idiom -- has BOTH an outer Return node
        # AND a nested IfExp node in the SAME subtree; a single combined
        # ``ast.walk`` visits the OUTER Return first (BFS visits parents
        # before children), so the coarse Return scan used to raise its
        # own generic message before the walk ever reached the ternary's
        # OWN, dedicated check -- DEMONSTRATED to break
        # InventedEvasionTests' own pinned ternary-message assertions the
        # moment Return scanning was added in the SAME loop. The general
        # principle already has precedent in this module:
        # :meth:`_audit_dispatch_helper_calls` runs BEFORE taint
        # propagation for the identical reason ("the former names the
        # actual mistake and must win the race, not be pre-empted by the
        # latter, coarser diagnosis" -- see that method's own docstring);
        # this is the same rule applied to a second race the Return scan's
        # addition newly created. Running the specific scan to completion
        # FIRST, over the WHOLE function, before the generic one starts,
        # makes the outcome independent of ``ast.walk``'s node order.
        for node in ast.walk(fn):
            if isinstance(node, ast.Expr):
                # #202 repair round 3, finding G: a class-level dispatch-
                # table lookup invoked as a BARE, UNASSIGNED statement
                # (``_ROUTE_TABLE.get(path, default)()``, ``getattr(self,
                # "_handle_" + suffix, self._default)()``) joins nothing via
                # the fixed-point loop above -- there is no assignment
                # target to add to ``tracked`` -- and is not a
                # ``self._handle_*``/``self._dispatch_*`` ATTRIBUTE call
                # :meth:`_audit_dispatch_helper_calls` would recognise.
                # DEMONSTRATED: contrasted directly against the ASSIGNED
                # form of the IDENTICAL expression (``outcome =
                # _ROUTE_TABLE.get(path); self._maybe_dispatch(outcome)``),
                # which the fixed-point loop above already raises on via
                # _propagates_taint's own unlisted-call check -- the bare
                # form raised nothing at all. Reuse that SAME check here,
                # merely without the assignment: whatever the statement's
                # own expression calls, if unlisted and still touching a
                # tracked name, is exactly as invisible a routing decision
                # as if it had been bound to a name first (round 2 finding
                # A's rule, applied without the assignment-only restriction
                # that created this gap). The return value is unused --
                # only the RAISE (or waiver) side effect matters here; a
                # bare statement has no target to add to ``tracked``.
                # ``self._followed`` passed through (#202 repair round 5,
                # finding 2b) so an already-recognised delegation reached
                # this way is not ALSO flagged as an unlisted call -- see
                # _propagates_taint's own docstring for the ``followed``
                # parameter; no real bare-Expr delegate call exists in
                # server.py today, but the Return scan just below needs
                # exactly this for its own, very real ``self._serve_
                # static(path)`` case, and both scans share one code path.
                _propagates_taint(node.value, tracked, fn.name,
                                  self.waiver_hits, parents, self._followed,
                                  captured_names, trusted_roots)
            if isinstance(node, ast.Return) and node.value is not None:
                # #202 repair round 5, finding 2b: a bare ``return <expr>``
                # is neither an assignment (the fixed-point loop above only
                # ever adds a leaf to ``tracked``, it never inspects a
                # Return) nor an ``ast.Expr`` (the round-3 finding G scan
                # immediately above this one) -- so NEITHER existing scan
                # ever visits ``Return.value`` at all. DEMONSTRATED:
                # ``return ROUTES[path]()`` (a live dispatch-table lookup,
                # keyed on the tracked path, invoked and returned in one
                # statement) and ``return self._route(path)`` (an
                # ARBITRARY, uncatalogued ``self.`` method call -- not a
                # ``_handle_*``/``_dispatch_*`` name
                # :meth:`_audit_dispatch_helper_calls` would ever flag, so
                # its own delegation-detector stays silent too) both
                # answered live HTTP while extraction recorded zero routes
                # and raised nothing. Reuses the exact SAME
                # _propagates_taint check the bare-Expr scan above already
                # runs, purely for its raise-if-unlisted-and-tracked side
                # effect (round 2 finding A's rule, extended to a THIRD
                # statement shape rather than duplicated for it) -- the
                # return value is unused, and a Return has no assignment
                # target to add to ``tracked`` any more than a bare Expr
                # does. ``self._followed`` passed through so an ALREADY-
                # recognised delegation (``return self._serve_static(
                # path)``, ordinary and common -- ``_serve_static`` is a
                # real ``SAME_PATH_DELEGATES`` entry, independently walked
                # with its own tracked set by :meth:`_maybe_delegate`
                # during the walk phase, well before this audit runs) is
                # not ALSO flagged here as an unlisted call over the very
                # same node -- see _propagates_taint's own docstring for
                # what ``followed`` skips and why.
                _propagates_taint(node.value, tracked, fn.name,
                                  self.waiver_hits, parents, self._followed,
                                  captured_names, trusted_roots)
            if isinstance(node, ast.Raise) and node.exc is not None:
                # #202 repair round 5, finding 6b: ``raise <expr>`` is a
                # CONTROL-TRANSFER expression this module never inspected
                # at all -- neither the fixed-point loop (assignment-
                # only), the bare-Expr scan, nor the Return scan (finding
                # 2b) visits a Raise's own exception argument.
                # DEMONSTRATED (half of the reviewer's own combined
                # repro): ``raise ValueError(path)`` -- the tracked path,
                # handed DIRECTLY to an unlisted exception constructor --
                # answered live HTTP while extraction stayed silent.
                # Reuses the exact SAME _propagates_taint check the
                # Return/bare-Expr scans already run, purely for its
                # raise-if-unlisted-and-tracked side effect (the SAME
                # "unlisted call/tracked-expression" rules already applied
                # everywhere else in this module, per the reviewer's own
                # required-correction wording) -- the raised exception's
                # eventual DESTINATION (which except clause, if any, ends
                # up catching it) is a SEPARATE concern, handled
                # conservatively below rather than by tracing the actual
                # control-flow edge (see finding 6c's own comment for why
                # that precise a trace is out of reach for this module).
                _propagates_taint(node.exc, tracked, fn.name,
                                  self.waiver_hits, parents, self._followed,
                                  captured_names, trusted_roots)
            if isinstance(node, (ast.With, ast.AsyncWith)):
                # #202 repair round 6, finding 2: a ``with`` statement's
                # CONTEXT EXPRESSION (``with CONTEXTS[path]:``) is a
                # CONTROL-TRANSFER-relevant expression this module never
                # inspected at all -- not the fixed-point loop (which only
                # ever looks at a ``with ... as name:`` item's context
                # expression to decide whether ITS OWN bound NAME should
                # join ``tracked``, per :func:`_binding_value_and_targets`,
                # and does nothing at all for a bare ``with EXPR:`` with no
                # ``as`` clause -- exactly this shape), nor the bare-Expr/
                # Return/Raise scans above (none of which visit
                # ``With.items``). A context manager's own ``__enter__``/
                # ``__exit__`` can have side effects, and ``__exit__``
                # returning true SUPPRESSES an exception raised in the
                # block -- both observably change what response the
                # request gets, driven entirely by WHICH context manager
                # ``CONTEXTS[path]`` resolves to. DEMONSTRATED: ``with
                # CONTEXTS[path]: return self._send(1)`` answered live
                # HTTP 200 while extraction stayed silent. Reuses the SAME
                # _propagates_taint check every other statement position
                # in this module already runs, purely for its raise-if-
                # unlisted-and-tracked side effect -- every item, not only
                # the first, and regardless of whether the item binds a
                # name at all.
                for item in node.items:
                    _propagates_taint(item.context_expr, tracked, fn.name,
                                      self.waiver_hits, parents,
                                      self._followed, captured_names,
                                      trusted_roots)
            if isinstance(node, ast.ExceptHandler) and node.name is not None:
                # #202 repair round 5, finding 6c -- the HARD part, stated
                # honestly rather than left silently unaddressed (matching
                # the standard #202 repair round 3, finding H already set:
                # fix what is tractable, document the rest). TRUE cross-
                # statement data-flow -- proving WHICH raise site's
                # payload a GIVEN except clause actually catches -- is out
                # of reach for a walker with no type/control-flow
                # analysis, and this module does not claim to do it. What
                # IS tractable, and implemented here: a COARSE, DELIBERATELY
                # CONSERVATIVE over-approximation -- ``except ... as
                # name:`` binds a name this module's binding model has
                # never listed (see :func:`_binding_value_and_targets`'s
                # own KNOWN LIMITATIONS note), so if it were TESTED for
                # routing later, that test would be invisible; rather than
                # attempt (and likely get wrong) a precise "does this
                # handler's exception TYPE match that raise's exception
                # TYPE" check -- inheritance, aliasing and multi-type
                # ``except (A, B) as name:`` tuples all make simple textual
                # matching UNRELIABLE in the permissive direction, which
                # is exactly the direction this module must never guess in
                # -- this fails closed on ANY named handler the moment the
                # ENCLOSING FUNCTION contains ANY raise whose own argument
                # mentions a tracked name ANYWHERE, regardless of the
                # handler's own declared type(s). DELIBERATELY
                # OVER-INCLUSIVE: a function with an unrelated tracked
                # raise and a genuinely-unconnected named handler will be
                # flagged too, reviewable via the SAME declared
                # ``_AUDIT_WAIVERS`` escape hatch as everything else in
                # this module -- "occasionally over-flag a genuinely-safe
                # except clause" is the accepted cost of not "silently
                # miss[ing] a live one". DEMONSTRATED (the other half of
                # the reviewer's own combined repro): ``raise
                # ValueError(path)`` followed by ``except ValueError as
                # candidate: if str(candidate) == "/api/hidden": ...``
                # answered live HTTP while extraction stayed silent --
                # `candidate` carries the raised path as its exception
                # payload, invisible to every check in this module until
                # now.
                #
                # #202 repair round 6, finding 2: finding 6c's own check
                # above only ever looks for an EXPLICIT ``ast.Raise`` node
                # -- an IMPLICIT exception (a Subscript/operation that
                # fails naturally: ``{}[path]``'s own ``KeyError``,
                # ``int(path)``'s own ``ValueError``, ...) can carry
                # tracked data into THIS SAME handler's payload exactly
                # the way an explicit ``raise`` already does, but was
                # invisible to a check that only pattern-matches
                # ``ast.Raise``. :meth:`_try_body_has_tracked_operation`
                # closes this SCOPED to the handler's OWN enclosing
                # ``ast.Try`` body specifically (not function-wide, the
                # way the explicit-raise check above deliberately is) --
                # not a further approximation, but an ACCURATE reflection
                # of Python's own exception semantics: a handler can only
                # ever catch what its OWN try body raises, implicitly or
                # explicitly. DEMONSTRATED: ``try: {}[path] \n except
                # KeyError as e: candidate = str(e.args[0]) \n if
                # candidate == "/api/hidden": ...`` answered live HTTP
                # while extraction stayed silent -- even once the bare
                # ``{}[path]`` access is independently caught by this
                # round's own Subscript audit (see _propagates_taint), a
                # REVIEWED, WAIVED tracked operation in the SAME try body
                # (this module's own established escape hatch for a
                # legitimately-safe call/subscript) would otherwise still
                # leave this handler completely unexamined.
                if self._function_has_tracked_raise(fn, tracked) \
                        or self._try_body_has_tracked_operation(
                            node, tracked, parents):
                    key = _waiver_key(fn.name, node, parents)
                    if key in _AUDIT_WAIVERS:
                        self._record_waiver_hit(key, node)
                        continue
                    raise ExtractionError(
                        f"{fn.name}:{node.lineno} `except ... as "
                        f"{node.name}:` binds a name that MAY carry "
                        "exception-payload taint -- this function "
                        "contains a raise (explicit, or an operation that "
                        "may raise implicitly) whose argument mentions a "
                        "tracked dispatch name, and route_extract cannot "
                        "prove this handler does not receive it (a "
                        "conservative, function-wide-or-try-body-scoped "
                        "over-approximation: see _audit_function's own "
                        "finding-6c comment). Rewrite to avoid depending "
                        "on the exception payload, or classify here.")

    def _function_has_tracked_raise(self, fn: ast.FunctionDef,
                                    tracked: set) -> bool:
        """Does ``fn`` contain ANY ``raise <expr>`` whose argument mentions
        a tracked name? (#202 repair round 5, finding 6c's own coarse,
        function-wide "could plausibly reach" stand-in -- see that
        branch's comment in :meth:`_audit_function` for why a precise,
        per-handler match is not attempted.) Uses the SAME
        ``_mentions_tracked`` boundary every other check in this module
        does, so a raise whose only tracked mention is a genuinely opaque
        extraction (a captured group, a Path property) does not trigger
        this either -- consistent with, not a special case of, the rest
        of the module's taint model.
        """
        for node in ast.walk(fn):
            if isinstance(node, ast.Raise) and node.exc is not None \
                    and _mentions_tracked(node.exc, tracked):
                return True
        return False

    @staticmethod
    def _try_body_has_tracked_operation(handler: ast.ExceptHandler,
                                        tracked: set,
                                        parents: dict) -> bool:
        """Does the ``ast.Try`` block ``handler`` belongs to contain, in
        its OWN try body specifically, an expression that mentions a
        tracked name? (#202 repair round 6, finding 2.)

        :meth:`_function_has_tracked_raise` (round 5, finding 6c) only
        ever looks for an EXPLICIT ``ast.Raise`` node, function-wide. An
        IMPLICIT exception -- a Subscript/operation that fails naturally
        (``{}[path]``'s own ``KeyError``, ...) rather than an explicit
        ``raise`` -- was invisible to it no matter how directly it carried
        tracked data into THIS SAME handler's payload. This check is
        scoped to the handler's OWN enclosing try body, narrower than
        finding 6c's function-wide reach -- not a further approximation,
        but an ACCURATE reflection of Python's own exception semantics: a
        handler can only ever catch what its OWN try body raises, so
        looking function-wide here would over-flag every named handler in
        a function that merely happens to ALSO have an unrelated tracked
        expression somewhere else in it, in a way finding 6c's own
        already-accepted function-wide over-approximation for EXPLICIT
        raises does not (an explicit ``raise`` can be re-raised or
        propagate through nested try/except in ways this module has no
        control-flow model for, which is exactly finding 6c's own
        documented reason for going function-wide there; an implicit
        failure has no such ambiguity -- it can only ever be caught by a
        handler of the SAME try statement it occurs in).

        Uses the SAME ``_mentions_tracked`` boundary every other check in
        this module does, walking each top-level statement of the try
        body (not the try statement's OWN handlers/orelse/finalbody,
        which are not what this try body's OWN exceptions come from).
        """
        parent = parents.get(id(handler))
        if not isinstance(parent, ast.Try):
            return False
        return any(_mentions_tracked(stmt, tracked) for stmt in parent.body)

    def _audit_dispatch_helper_calls(self, fn: ast.FunctionDef) -> None:
        """Raise if ``fn`` calls an uncatalogued ``_handle_*``/``_dispatch_*``
        helper, or a catalogued one in a statement form :meth:`_maybe_delegate`
        does not follow. See :meth:`_audit_function` for why this runs before
        taint propagation rather than alongside the ``If``/``IfExp`` scan.
        """
        for node in ast.walk(fn):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                    and isinstance(node.func.value, ast.Name) \
                    and node.func.value.id == "self":
                name = node.func.attr
                if (name.startswith("_handle_") or name.startswith("_dispatch_")) \
                        and name not in (TAIL_DELEGATES | SAME_PATH_DELEGATES
                                         | PARSED_DELEGATES):
                    raise ExtractionError(
                        f"{fn.name}:{node.lineno} calls unknown dispatch helper "
                        f"self.{name}() — classify it in route_extract.py")
                if name in (TAIL_DELEGATES | SAME_PATH_DELEGATES
                           | PARSED_DELEGATES) \
                        and id(node) not in self._followed:
                    # A delegation in a statement form the walker does not
                    # follow (an assignment, a comprehension, ...). Its callee's
                    # routes would simply be absent — the one failure this
                    # module must never have. PARSED_DELEGATES joined this
                    # check in #202's repair: before it, a call to
                    # _handle_reassign/_handle_reassign_v2 was EXEMPT from
                    # ever needing to be followed, which is exactly how its
                    # combo-dict enumeration went unwalked in the first place.
                    raise ExtractionError(
                        f"{fn.name}:{node.lineno} delegates to self.{name}() "
                        "in a form the walker does not follow")


# --------------------------------------------------------------------------- #
# Waiver fingerprinting (#202 repair round 4, finding 3). A waiver keyed on   #
# just (function, exact expression text) is exact-one-hit against DRIFT of   #
# the text itself (round 2 finding D), but blind to RELOCATION: the SAME     #
# once-used expression, moved into a NEW, routing-relevant structural        #
# position -- e.g. ``required_permission(path)`` moved from a 403 error      #
# body's local (its current, reviewed position) into a dict key selecting a  #
# handler -- still unparses identically, so the old waiver keeps matching a  #
# line that is now a genuine, unreviewed routing decision. DEMONSTRATED: the #
# waiver text alone does not change when the STATEMENT changes.              #
#                                                                             #
# The fix widens the key with two structural facts, both cheap to compute   #
# from a PARENT MAP built once per audited function:                        #
#                                                                             #
#   parent_shape       the role the expression plays for its nearest        #
#                       MEANINGFUL parent (walking up through pure           #
#                       pass-through BoolOp/UnaryOp wrappers) -- "if_test",   #
#                       "compare_operand", "assign_rhs", "call_argument",    #
#                       "subscript_index", ... Relocating an expression from #
#                       one of these into another (the attack named above)   #
#                       changes this value, so the old waiver stops          #
#                       matching and the new position raises fresh.          #
#   enclosing_if_text  the nearest ENCLOSING ``ast.If``'s own test (not the  #
#                       expression's own, if it IS reached as an if-test),   #
#                       distinguishing two occurrences of textually          #
#                       IDENTICAL expressions reached from two DIFFERENT     #
#                       branches of the SAME function -- DEMONSTRATED in the #
#                       real server.py: ``self._guardian_link_or_403(guid,   #
#                       jid)`` appears verbatim under BOTH ``if mga:`` and    #
#                       ``if mgs:`` inside do_POST (two different            #
#                       guardian-scoped routes, the SAME reviewed link       #
#                       check applied twice) -- parent_shape alone           #
#                       ("if_test" both times) cannot tell them apart; the   #
#                       enclosing branch can.                               #
#                                                                             #
# Neither depends on a line number (the ORIGINAL design's whole point --     #
# unrelated code moving elsewhere in the file must not invalidate a waiver), #
# only on structural POSITION relative to the expression itself.            #
# --------------------------------------------------------------------------- #
def _build_parent_map(root: ast.AST) -> dict:
    """``id(node) -> its immediate AST parent`` for every node in ``root``'s
    subtree. ``ast.iter_child_nodes`` already flattens list-valued fields
    (a statement inside ``If.body``, an item inside ``With.items``, ...) to
    direct parent/child edges, so this needs no special-casing per field
    shape -- one pass, same as :func:`ast.walk` itself uses internally."""
    parents = {}
    for node in ast.walk(root):
        for child in ast.iter_child_nodes(node):
            parents[id(child)] = node
    return parents


#: parent node type -> the attribute name on it that, when it holds the
#: child being fingerprinted, names a MEANINGFUL structural role rather than
#: the generic type-name fallback in :func:`_parent_shape`.
_ROLE_ATTRS = {
    ast.If: (("test", "if_test"),),
    ast.While: (("test", "while_test"),),
    ast.IfExp: (("test", "ifexp_test"),),
    ast.Assign: (("value", "assign_rhs"),),
    ast.AnnAssign: (("value", "assign_rhs"),),
    ast.AugAssign: (("value", "assign_rhs"),),
    ast.NamedExpr: (("value", "assign_rhs"),),
    ast.Subscript: (("slice", "subscript_index"), ("value", "subscript_value")),
    ast.Return: (("value", "return_value"),),
}
if hasattr(ast, "match_case"):  # pragma: no branch -- Python 3.10+
    _ROLE_ATTRS[ast.match_case] = (("guard", "case_guard"),)


def _parent_shape(child: ast.AST, parent: Optional[ast.AST]) -> str:
    """A short, stable descriptor of the STRUCTURAL role ``child`` plays for
    its immediate ``parent`` -- part of the waiver fingerprint. See this
    section's own module comment for why relocating an expression between
    two of these values must invalidate an existing waiver."""
    if parent is None:
        return "root"
    for attr, label in _ROLE_ATTRS.get(type(parent), ()):
        if getattr(parent, attr, None) is child:
            return label
    if isinstance(parent, ast.Compare):
        return "compare_operand"
    if isinstance(parent, ast.Expr):
        return "bare_stmt"
    if isinstance(parent, ast.Call):
        if parent.func is child:
            return "call_callee"
        if any(kw.value is child for kw in parent.keywords):
            return "call_keyword_argument"
        return "call_argument"
    if isinstance(parent, (ast.Tuple, ast.List, ast.Set)):
        return f"{type(parent).__name__.lower()}_element"
    if isinstance(parent, ast.Dict):
        return "dict_value" if child in parent.values else "dict_key"
    return type(parent).__name__.lower()


def _enclosing_if_text(expr: ast.AST, parents: dict) -> str:
    """The nearest ``ast.If`` STRICTLY ENCLOSING ``expr`` -- i.e. reached
    through its body/orelse, not merely because ``expr`` IS that same If's
    own ``.test`` (the self-reference :meth:`_DispatchWalker._audit_function`
    creates when it fingerprints an If/While/IfExp's test against ITSELF,
    which names nothing about which BRANCH the test lives in). Returns
    ``""`` when there is no enclosing If at all (a top-level statement)."""
    current = parents.get(id(expr))
    if isinstance(current, ast.If) and current.test is expr:
        current = parents.get(id(current))
    while current is not None:
        if isinstance(current, ast.If):
            return ast.unparse(current.test)
        current = parents.get(id(current))
    return ""


def _waiver_key(fn_name: str, expr: ast.AST, parents: Optional[dict]) -> tuple:
    """The full ``_AUDIT_WAIVERS`` key for ``expr``, reached while auditing
    ``fn_name``: ``(function, exact text, parent shape, enclosing if)``.
    Every waiver-consulting call site in this module builds its key through
    this ONE function, so the four-part shape (and any future change to it)
    never drifts between them. ``parents`` may be ``None`` for a caller that
    has no whole-function parent map handy (:meth:`_DispatchWalker.
    _else_rederives_subject`'s narrow, terminal-else-only check, which does
    not consult a waiver in the shipped server.py today) -- the fingerprint
    degrades to an empty ``enclosing_if_text`` rather than failing, since
    ``parent_shape`` alone (computable from ``expr`` and its own immediate
    AST parent, independent of any map) still captures finding 3's primary,
    REQUIRED case: a relocated expression's shape differs regardless.
    """
    parent = parents.get(id(expr)) if parents is not None else None
    shape = _parent_shape(expr, parent)
    enclosing = _enclosing_if_text(expr, parents) if parents is not None else ""
    return (fn_name, ast.unparse(expr), shape, enclosing)


# Branches (or, since #202 repair round 2 finding A, unlisted CALLS reached
# while auditing an assignment for taint propagation) that DECIDE ON a
# path-derived name but are not routing decisions.
#
# Fail-closed is the rule: an unrecognised test -- or, now, an unrecognised
# call consuming a tracked name -- stops the build. These are the declared
# exceptions — each one reviewed, each one a visible line in the diff. Adding
# a waiver is deliberately as conspicuous as adding a route, because a waiver
# is how the gate would be quietly defeated.
#
# Keyed by _waiver_key(function, expression, parents) -- #202 repair round 4,
# finding 3: (function name, the exact unparsed test OR call expression, the
# expression's own structural PARENT SHAPE, the nearest ENCLOSING if-test's
# text). A drifted expression, a RELOCATED one (moved to a new parent shape),
# or one moved to a different branch of the same function no longer matches
# its waiver and raises again -- see the fingerprinting section above this
# dict for what each of the four parts defends against and why.
_AUDIT_WAIVERS = {
    ("_serve_static",
     "STATIC_DIR not in target.parents or not target.is_file()",
     "if_test", ""):
        "filesystem containment on the already-resolved static target -- it "
        "decides whether to SERVE, not which route was chosen",
    ("_serve_static", "target.suffix == '.html'", "if_test", ""):
        "content-type selection for the already-resolved static target",
    ("_handle_setup_v2", "mar.group(2) == 'archive'", "ifexp_test", "mar"):
        "a TERNARY (#202 repair, invented-evasion track) choosing which "
        "backend function to call -- api.archive_season vs "
        "api.reopen_season. NOT a routing decision: mar's own pattern "
        "(archive|reopen) already produces BOTH templates as separate "
        "regex-alternation leaves (see route_registry.py's "
        "post_v2_setup_seasons_id_archive/_reopen), so this ternary picks "
        "an implementation for a route already fully decided upstream",
    ("_handle_setup_v2", "kind == 'venue'", "ifexp_test", "md"):
        "a TERNARY (#202 repair, invented-evasion track) choosing a "
        "response mapper (_v2p.venue_to_v2 vs identity). NOT a routing "
        "decision: kind comes from md's own entity alternation, which "
        "already produces one delete leaf PER entity (see "
        "route_registry.py's post_v2_setup_<entity>_id_delete specs); this "
        "ternary only reshapes the response for one of those already-"
        "enumerated leaves",
    ("_handle_reassign", "_REASSIGN_PARENTS.get(combo)", "assign_rhs", ""):
        "#202 repair round 2 finding A -- a MODULE-LEVEL authorisation-"
        "parent lookup keyed on the already-tracked combo. NOT a routing "
        "decision: the route was already fully decided upstream by `combo "
        "in _V1_REASSIGN_SCHEMA`; this result only feeds `targets` for the "
        "#369 write-side parent-ownership check, the same 'produces a "
        "RESULT' shape as a captured group handed to a service, just not a "
        "mechanical one (unlike a captured group or a Path property, "
        "_REASSIGN_PARENTS being a plain module dict isn't something a "
        "shape rule can tell apart from a genuine hidden route table, so "
        "it is reviewed here instead)",
    ("_handle_reassign_v2", "_REASSIGN_PARENTS.get(combo)", "assign_rhs", ""):
        "same authorisation-parent lookup as _handle_reassign's own "
        "waiver above, reached from the v2 handler -- the route is already "
        "decided upstream by the v2 combo/schema dispatch; see that entry",
    # -- #202 repair round 5, finding 1 -- newly REACHED now that a waived
    # call's RESULT keeps propagating through the ordinary fixed-point
    # mechanism instead of being erased the instant its call site is
    # waived (see _propagates_taint's own docstring, "A WAIVER SILENCES
    # THE CALL, NOT THE RESULT"). `parent` (bound two waiver entries above,
    # from the SAME module-level `_REASSIGN_PARENTS.get(combo)` this
    # dict already reviews) now joins `tracked`, so the `is not None`
    # check on it -- previously invisible, because `parent` never reached
    # `tracked` at all -- is examined for the first time. NOT a routing
    # decision: `parent` is `None` exactly when `combo` has no #369
    # write-side parent-ownership rule to enforce (most reassign combos
    # don't need one), so this only decides whether to APPEND that one
    # extra authorisation-target entry -- the SAME `targets` list this
    # dict's own `targets.append((parent[0], ...))` waiver already
    # reviews below, reached here as the bare `is not None` guard around
    # it instead of the append itself. The route was fully decided
    # upstream by `combo in _V1_REASSIGN_SCHEMA`, unchanged by this branch
    # either way.
    ("_handle_reassign", "parent is not None", "if_test", ""):
        "#202 repair round 5, finding 1 -- guards whether the module-level "
        "`_REASSIGN_PARENTS.get(combo)` lookup (waived two entries above) "
        "found a #369 write-side parent-ownership rule for the already-"
        "decided combo; append-or-not, not a routing decision -- see the "
        "comment block immediately above this entry for the full "
        "reasoning, and the sibling `targets.append(...)` waiver below "
        "for the SAME `targets` list reached as a bare statement instead",
    ("_handle_reassign_v2", "parent is not None", "if_test", ""):
        "the v2 sibling of _handle_reassign's own identical-shape waiver "
        "immediately above (same module-level `_REASSIGN_PARENTS.get(combo)` "
        "lookup, same #369 append-or-not guard, same reasoning) -- the "
        "route here is already decided upstream by `combo in "
        "_V2_REASSIGN_SCHEMA`",
    ("_handle_reassign", "self._V1_SETUP_KIND.get(entity, entity)",
     "tuple_element", ""):
        "#202 repair round 2 finding A -- a legacy-name-alias lookup "
        "(v1 'league' -> canonical 'program', identity for everything "
        "else) that only relabels the authorisation TARGET kind added to "
        "`targets`. NOT a routing decision: the route was already decided "
        "by the regex + combo/schema dispatch upstream; this reshapes an "
        "authorisation-check argument, the same 'produces a RESULT' shape "
        "as a captured group handed to a service",
    ("_handle_setup", "self._V1_SETUP_KIND.get(kind, kind)",
     "tuple_element", "md"):
        "#202 repair round 5, finding 2b -- the SAME legacy-name-alias "
        "lookup as _handle_reassign's own identical-shape waiver "
        "immediately above (see that entry), reached from the v1 delete "
        "route's `_guarded_mutation` authorisation-target list instead of "
        "`_handle_reassign`'s `targets`; newly reached now that a Return "
        "statement's own value is audited at all (see this dict's own "
        "round-5, finding-2b comment block below `_handle_reassign_v2`'s "
        "own guardian-link waivers, above `deleter`/`mapper`) -- NOT a "
        "routing decision, the route was already decided by `md`'s own "
        "entity alternation",
    # RESTORED (#202 repair round 9, finding 1 -- external review): removed
    # at round 5 finding 2b (see the git history for that round's own
    # "REMOVED" comment, superseded by this one) once the general
    # `captured` exemption in `_propagates_taint` started covering this
    # shape without a per-call-site waiver -- round 9 narrows that general
    # exemption to a small, explicit allowlist of call TARGETS
    # (`_captured_arg_safe_callee`: currently just the `api` facade) that
    # does not include `_to_v1` (a LOCAL dict of v1 response-shape mappers,
    # built two lines above this call, not the API facade), so this call
    # site needs its own reviewed waiver again, live once more (see
    # WaiverFingerprintTests' own pinned count).
    ("_handle_setup", "_to_v1.get(kind, lambda r: r)", "assign_rhs", "md"):
        "#202 repair round 2, finding A's own original reasoning, restored "
        "round 9: selects the v1 wire-shape RESPONSE mapper for the "
        "delete route's already-decided entity `kind` -- every kind in "
        "`md`'s pattern already yields the SAME `/api/setup/<entity>/{}/"
        "delete` leaf, so this only reshapes the response body, it does "
        "not choose a route. `_to_v1` is a plain module-shape dict built "
        "from `_v1.*_to_v1` mapper functions two lines above this call, "
        "never invoked with anything other than the already-serialized "
        "deleted record (see the `mapper(deleter(...))` waiver just below "
        "`_handle_reassign_v2`'s own entries) -- not a hidden dispatcher.",
    # NEW (#202 repair round 9, finding 1 -- external review): round 9's
    # `_captured_arg_safe_callee` allowlist covers only a call/subscript
    # rooted at `api` (or a dict literal of such) -- `kind.capitalize()`
    # is neither: the captured value `kind` is the call's own RECEIVER,
    # not an argument selecting some OTHER callable, so no dispatch table
    # is involved at all, but it also does not fit the allowlist shape,
    # so it needs its own explicit waiver rather than silently widening
    # that allowlist for a single-site pattern.
    ("_handle_setup", "kind.capitalize()", "formattedvalue", "mmv"):
        "reshapes the captured v2-moved entity name for a 409 error "
        "MESSAGE string only (`f\"{kind.capitalize()} delete has moved "
        "to v2....\"`)  -- a builtin `str.capitalize()` call ON the "
        "captured value itself (never an argument choosing which OTHER "
        "callable runs), the same narrow 'produces a derived scalar, not "
        "a routing decision' shape `_PATH_METHODS`/`_PATH_PROPERTIES` "
        "already carve out for `path` -- the route here was already "
        "fully decided by `mmv`'s own `(player|official)` alternation two "
        "lines above.",
    ("do_POST", "required_permission(path)", "assign_rhs",
     "not authorize(role, path)"):
        "#202 repair round 2 finding A -- builds the human-readable "
        "permission name for a 403 error body, AFTER `authorize(role, "
        "path)` (the actual gate; see that call's OWN waiver below -- "
        "#202 repair round 4, finding 1 closed the structural gap that "
        "used to exempt a bare call reached directly as a whole if-test "
        "without even scanning its arguments) has already refused the "
        "request. A blanket per-verb authorisation gate, not a route "
        "selector -- see test_a_guard_that_merely_passes_the_path_along_"
        "is_not_a_route for the same shape via `_operator_only`",
    ("do_POST", "perm", "ifexp_test",
     "not authorize(role, path)"):
        "#202 repair round 5, finding 1 -- `perm.value if perm else None` "
        "(the 403 body's `details.required` field): `perm` is `required_"
        "permission(path)`, waived two entries above; this ternary only "
        "decides whether the human-readable permission NAME is present or "
        "null in the error body -- it does not choose a route, `authorize"
        "(role, path)` (waived below) already refused the request before "
        "this line runs. Newly reached because `perm` now stays tracked "
        "past its own waived call -- see _propagates_taint's own "
        "docstring, 'A WAIVER SILENCES THE CALL, NOT THE RESULT'",
    ("do_POST", "violation is not None", "if_test", ""):
        "#202 repair round 5, finding 1 -- guards whether `scope_violation"
        "(...)` (waived below) found a #51 resource-scoping problem for "
        "the ALREADY-AUTHORISED request; `violation` is either `None` or a "
        "human-readable message string, never a route selector -- refuses "
        "with that message or falls through to dispatch, the SAME blanket-"
        "gate shape as `scope_violation(...)`'s own waiver immediately "
        "below. Newly reached because `violation` now stays tracked past "
        "its own waived call, the same way `perm` does two entries above",
    ("do_POST",
     "scope_violation(role, scope, path, body, api.store, "
     "allow_unscoped_dev_fallback=allow_dev_fallback)", "assign_rhs", ""):
        "#202 repair round 2 finding A -- resource-scoping authorisation "
        "(#51: a coach only their team, a player only self), run "
        "UNCONDITIONALLY for every POST before any path-based dispatch "
        "branch is reached. Same blanket-gate shape as `required_"
        "permission(path)` immediately above -- refuses access to an "
        "already-identified resource, does not select a route",
    # #202 repair round 3, finding E -- newly examined now that a Call
    # reached AS A COMPARISON OPERAND has its arguments scanned too (see
    # _direct_operand_names' own docstring). `_supported_methods(path)` is
    # the SAME 405/Allow admission source route_registry.py's own gate is
    # diffed byte-identical against (#202 wiring step) -- a DERIVED,
    # POST-HOC check of which HTTP verbs an ALREADY-MATCHED path admits,
    # not a selector between different templates. Membership against a
    # small FIXED set of verb strings ("GET"/"POST"/...) can never pick a
    # different route the way `len(path) == N`/`str(path) == lit` could.
    ("do_POST", "'POST' not in self._supported_methods(path)", "if_test", ""):
        "#202 repair round 3, finding E -- the very first line of do_POST: "
        "refuses (405/Allow) BEFORE any path-based dispatch branch, using "
        "the SAME derived method-admission source the registry's own gate "
        "is diffed against; see this waiver's own comment block above "
        "for the general shape",
    ("_handle_reassign_v2",
     "targets.append((dest[0], b.get(dest[1]) or None))", "bare_stmt",
     "dest is not None"):
        "#202 repair round 3, finding E -- `targets` is the AUTHORISATION-"
        "TARGET list fed to `_refuse_unchosen_context`/"
        "`_reject_target_outside_scope` below, seeded from `(entity, "
        "record_id)` (both tracked #369 write-side identifiers) -- which "
        "is what makes `targets` ITSELF read as tracked once a Call (`."
        "append`) reaches it undisguised. `dest` is `_V2_REASSIGN_DEST."
        "get(combo)`, a richly-tracked tuple-dict lookup (#202 repair root "
        "cause 1) keyed on the ALREADY-DECIDED `combo` -- appending its "
        "components onto the target list only widens the AUTHORISATION "
        "check (#369: the destination row must also be writable), it does "
        "not choose a different template; the route was fully decided "
        "upstream by `combo in _V2_REASSIGN_SCHEMA`",
    ("_handle_reassign_v2",
     "targets.append((parent[0], b.get(parent[1]) or None, "
     "'writable_parent'))", "bare_stmt", "parent is not None"):
        "#202 repair round 3, finding E -- same `targets` list as the "
        "immediately preceding waiver (this entry's own comment explains "
        "why `targets` itself reads as tracked); `parent` is "
        "`_REASSIGN_PARENTS.get(combo)`, already covered by its own "
        "existing waiver above -- appending it extends the SAME "
        "authorisation-target list with the write-side parent-ownership "
        "check (#369), not a routing decision",
    ("_handle_reassign_v2", "check_body(b, **_V2_REASSIGN_SCHEMA[combo])",
     "bare_stmt", "combo in _V2_REASSIGN_SCHEMA"):
        "#202 repair round 3, finding E -- request-BODY field validation "
        "against the schema for the ALREADY-DECIDED `combo` (`combo in "
        "_V2_REASSIGN_SCHEMA` chose the route upstream); `check_body` "
        "raises `BodyError` on a malformed body, it does not choose "
        "between templates -- the same 'produces a RESULT for a "
        "post-dispatch concern' shape as every other waiver in this "
        "dict, just reached as a bare statement instead of an assignment",
    ("_handle_reassign_v2", "len(target) > 2", "ifexp_test",
     "self._reject_target_outside_scope(target[0], target[1], actor_id, "
     "role, scope, target[2] if len(target) > 2 else 'scope')"):
        "#202 repair round 3, finding E -- a NAME COLLISION this flat, "
        "unscoped `tracked` set cannot see past: `target` is BOTH "
        "_handle_reassign_v2's own tracked parameter (the reassignment "
        "destination's ENTITY KIND, e.g. 'organization') AND, shadowing "
        "it, the `for target in targets:` loop variable a few lines "
        "below -- an authorisation-target TUPLE, `(kind, id[, "
        "'writable_parent'])`. `len(target) > 2` asks whether THIS TUPLE "
        "carries the optional third field, selecting the scope label "
        "passed to `_reject_target_outside_scope` -- it has nothing to do "
        "with the outer `target` parameter it happens to share a bare "
        "name with. route_extract does not model lexical scoping (a "
        "documented, accepted limitation -- see the module docstring's "
        "KNOWN LIMITATIONS section), so a same-named inner shadow of an "
        "outer tracked subject is indistinguishable from the real thing "
        "without this kind of human review",
    ("_handle_reassign", "targets.append((dest[0], b.get(dest[1]) or None))",
     "bare_stmt", "dest is not None"):
        "#202 repair round 3, finding E -- the v1 sibling of "
        "_handle_reassign_v2's own identical-shape waiver above (see that "
        "entry): `dest` is `_V1_REASSIGN_DEST.get(combo)`, a richly-"
        "tracked tuple-dict lookup keyed on the already-decided `combo`; "
        "appending it onto the authorisation-target list widens the #369 "
        "write-side ownership check, it does not choose a route",
    ("_handle_reassign", "check_body(b, **_V1_REASSIGN_SCHEMA[combo])",
     "bare_stmt", "combo in _V1_REASSIGN_SCHEMA"):
        "#202 repair round 3, finding E -- the v1 sibling of "
        "_handle_reassign_v2's own identical-shape waiver above (see that "
        "entry): request-body field validation against the schema for "
        "the already-decided `combo`, not a routing decision",
    ("_handle_reassign", "len(target) > 2", "ifexp_test",
     "self._reject_target_outside_scope(target[0], target[1], actor_id, "
     "role, scope, target[2] if len(target) > 2 else 'scope')"):
        "#202 repair round 3, finding E -- the v1 sibling of "
        "_handle_reassign_v2's own identical-shape waiver above (see that "
        "entry for the full name-collision explanation): `target` here is "
        "the `for target in targets:` loop variable (an authorisation-"
        "target tuple), shadowing _handle_reassign's own tracked `target` "
        "parameter (the reassignment destination's entity kind) in name "
        "only",
    # -- #202 repair round 5, finding 1 -- newly REACHED, all four, for the
    # SAME reason as this dict's two new "parent is not None" entries above:
    # a waived call's RESULT no longer stops propagating the instant its
    # call site is waived (see _propagates_taint's own docstring). Two
    # consequences, both here:
    #   (a) `_handle_reassign`'s SECOND `targets.append(...)` (the
    #       "writable_parent" one) was previously never even examined --
    #       `parent` wasn't tracked pre-round-5, so `_mentions_tracked`
    #       found nothing in it -- unlike its v2 sibling and unlike this
    #       same function's FIRST append (`dest`-based), both of which
    #       were ALREADY reviewed pre-round-5 because `dest` reaches
    #       `tracked` a DIFFERENT way (a LOCAL tuple-keyed dict literal,
    #       recognised by the walker's own tuple-dict-lookup shape during
    #       the walk, independent of _propagates_taint entirely) that
    #       `parent`'s MODULE-LEVEL dict never gets;
    #   (b) `_propagates_taint`'s scan of ANY bare statement/assignment no
    #       longer stops at the FIRST waived call it meets -- it used to
    #       `return False` for the WHOLE expression there, which incidentally
    #       also hid every OTHER, unrelated call nested alongside it. Once a
    #       `targets.append((X[0], b.get(X[1]) or None))` statement's outer
    #       `.append(...)` call is (correctly) waived, the walk now
    #       continues into its ARGUMENTS and reaches the previously-
    #       unexamined `b.get(X[1])` inline call too -- `b` is the request
    #       BODY dict (`b = body`, both handlers' own top line), and
    #       `X[1]` (`dest[1]`/`parent[1]`) is a BODY KEY NAME the
    #       already-decided combo/dest/parent lookup chose, e.g.
    #       "organization_id" -- reading that one field's value to append
    #       onto the SAME authorisation-target list the enclosing
    #       `.append(...)` waiver already reviews. Not a routing decision
    #       either: the route was fully decided upstream by `combo in
    #       _V{1,2}_REASSIGN_SCHEMA`, unchanged by which body field gets
    #       read here.
    ("_handle_reassign",
     "targets.append((parent[0], b.get(parent[1]) or None, "
     "'writable_parent'))", "bare_stmt", "parent is not None"):
        "#202 repair round 5, finding 1, consequence (a) -- the v1 sibling "
        "of _handle_reassign_v2's own identical-shape 'writable_parent' "
        "append waiver (this dict, round 3 finding E section); newly "
        "reached because `parent` now stays tracked past its own waived "
        "lookup -- see the comment block immediately above this entry",
    ("_handle_reassign", "b.get(dest[1])", "boolop", "dest is not None"):
        "#202 repair round 5, finding 1, consequence (b) -- reads the "
        "request-body field the ALREADY-DECIDED `dest` (`_V1_REASSIGN_DEST."
        "get(combo)`) names, to append onto the `targets` authorisation "
        "list this dict's enclosing `targets.append(...)` waiver already "
        "reviews (round 3, finding E); not a routing decision -- see the "
        "comment block above this entry's group for the general shape",
    ("_handle_reassign", "b.get(parent[1])", "boolop", "parent is not None"):
        "same shape as this function's `b.get(dest[1])` waiver immediately "
        "above, for the `parent`-keyed (\"writable_parent\") append instead "
        "of the `dest`-keyed one",
    ("_handle_reassign_v2", "b.get(dest[1])", "boolop", "dest is not None"):
        "the v2 sibling of _handle_reassign's own identical-shape "
        "`b.get(dest[1])` waiver above; same request-body field read for "
        "the same already-decided `dest`, feeding the SAME `targets` list "
        "_handle_reassign_v2's own `targets.append(...)` waiver (round 3, "
        "finding E) already reviews",
    ("_handle_reassign_v2", "b.get(parent[1])", "boolop", "parent is not None"):
        "the v2 sibling of _handle_reassign's own identical-shape "
        "`b.get(parent[1])` waiver above; same request-body field read for "
        "the same already-decided `parent`, feeding the SAME `targets` "
        "list _handle_reassign_v2's own 'writable_parent' append waiver "
        "(round 3, finding E) already reviews",
    # -- #202 repair round 4, finding 1 -- newly examined now that a Call
    # reached DIRECTLY AS (or and/or/not-ed into) the WHOLE test has its
    # arguments scanned too, the same way finding E already did for a
    # comparison operand (see _direct_operand_names' own docstring). Every
    # entry below is a genuine, reviewed BLANKET GUARD -- an authorisation
    # or bookkeeping check that runs on an ALREADY-SELECTED route/resource,
    # never a selector between templates -- previously exempt only because
    # this shape was never even inspected, not because it was judged
    # harmless. Each is fingerprinted the SAME way as every entry above
    # (parent shape + enclosing if-text), verified exact-one-hit by
    # WaiverFingerprintTests' own real-server test.
    # REMOVED (#202 repair round 6, finding 1): this dict used to carry its
    # own waiver here for ("_dispatch_get", "self._operator_only(guard)",
    # "if_test", "mvc") -- `guard` is an f-string built from the ALREADY-
    # DECIDED `mvc` regex match (f'/api/v2/setup/seasons/{mvc.group(1)}/
    # venue-candidates'). It was ONLY ever a "hit" needing this waiver
    # because of the exact bug this round's finding 1 fixes: the OLD
    # bottom-of-function fallback in `_propagates_taint` was a blind
    # `ast.walk` Name-scan that did not honour the opaque-extraction
    # boundary, so it wrongly found `mvc` "tracked" reaching straight
    # THROUGH `mvc.group(1)` (a captured group, provably opaque) merely
    # because the f-string CONTAINED that call somewhere in its subtree --
    # the SAME class of bug round 5 finding 2b fought for `_TERMINAL_
    # RESPONSE_SENDERS`/`captured`, just not yet closed at the fallback's
    # own final line. `guard` is genuinely NOT tracked once that boundary
    # is respected (see `_mentions_tracked`, now this fallback's own
    # replacement) -- DEMONSTRATED dormant (0 hits, WaiverFingerprintTests'
    # own real-server exact-one-hit check) once this round's fix landed --
    # removed per this module's own discipline ("a dormant waiver matches
    # nothing and must be removed: proof nothing depends on it") rather
    # than left as a stale entry a future reader would have no way to tell
    # apart from a live one.
    ("_dispatch_get", "self._operator_only(path)", "if_test", "ms"):
        "get_accounts_id_sessions: the SAME blanket `_operator_only` gate, "
        "this call site passes the raw `path` directly rather than a "
        "literal guard string; the route was already selected by `ms` "
        "(^/api/accounts/([^/]+)/sessions$) before this line runs",
    ("_dispatch_get", "self._guardian_link_or_403(guid, jid)", "if_test",
     "mgo"):
        "get_me_guardian_id_substitute_opportunities_id: verifies the "
        "signed-in guardian holds a VERIFIED link to the named junior "
        "(`jid`, captured by `mgo`) before returning opportunity detail. "
        "NOT a routing decision: the route is already selected by `mgo`'s "
        "own regex match; this refuses an unlinked guardian access to an "
        "already-identified resource, the same 'produces a RESULT, not a "
        "routing decision' shape as `_official_guard`'s own calls "
        "elsewhere in this function (which stay exempt because their "
        "arguments are opaque captures/attributes, never a bare tracked "
        "Name)",
    ("_dispatch_get",
     "not can_read_private_game_data(role, scope, gid, api.store)",
     "if_test", "m"):
        "the /api/games/{}/<sub> family's private-data gate (#73): `gid` "
        "is captured by `m`'s own regex match; this decides whether the "
        "SIGNED-IN caller may view an already-selected game's private "
        "sub-resource (board/roster/etc, all already enumerated as "
        "separate leaves under the SAME `m` match), not which route was "
        "chosen",
    ("do_GET", "not is_context_scoped_read(path)", "if_test", ""):
        "do_GET's own PHASE A context-gate arrival ticket (#159): BOTH "
        "arms of this if unconditionally call self._dispatch_get() "
        "(server.py:1336-1341) -- the branch only decides whether to wrap "
        "that SAME delegation with reader-registration bookkeeping around "
        "the context-switch gate, never which template _dispatch_get goes "
        "on to select",
    ("_handle_reassign_v2",
     "self._refuse_unchosen_context(targets, actor_id, role, scope)",
     "if_test", ""):
        "the #369 write-side context check on the AUTHORISATION-TARGET "
        "list `targets`, built entirely from names this dict's own "
        "`targets.append(...)` waivers above already established are "
        "'produces a RESULT, not a routing decision' (the route was fully "
        "decided upstream by `combo in _V2_REASSIGN_SCHEMA`); this is the "
        "SAME `targets` list, now examined as a bare if-test instead of "
        "an assignment/append",
    ("_handle_reassign",
     "self._refuse_unchosen_context(targets, actor_id, role, scope)",
     "if_test", ""):
        "#202 repair round 5, finding 1 -- the v1 sibling of "
        "_handle_reassign_v2's own identical-shape waiver immediately "
        "above (same #369 write-side context check on the SAME kind of "
        "authorisation-target list); newly reached because `targets` now "
        "stays tracked in v1 too, for the reasons this dict's round-5, "
        "finding-1 waiver group above explains -- the route here is fully "
        "decided upstream by `combo in _V1_REASSIGN_SCHEMA`",
    # -- #202 repair round 5, finding 2b -- newly reached now that Return
    # statements are audited at all (see _propagates_taint's own
    # docstring's "captured" paragraph): `_guarded_mutation` is the OTHER
    # #369 gate alongside `_refuse_unchosen_context`/
    # `_reject_target_outside_scope` (both waived above) -- it takes the
    # SAME authorisation-target list `targets` PLUS a zero-argument
    # MUTATION CALLABLE, runs the target check, the row locks, the write
    # and the audit inside ONE transaction (server.py:1049-1075), and is
    # reached here as `return self._guarded_mutation(...)`, the function's
    # own terminal statement. Not a routing decision either: the route was
    # fully decided upstream by `combo in _V{1,2}_REASSIGN_SCHEMA`;
    # `_guarded_mutation` decides whether THIS caller may WRITE to the
    # already-identified target(s), the same authorisation question as
    # its two siblings, just running the actual mutation once that
    # question is answered yes.
    ("_handle_reassign",
     "self._guarded_mutation(targets, call, actor_id, role, scope)",
     "return_value", "call is not None"):
        "#202 repair round 5, finding 2b -- see the comment block "
        "immediately above this entry: `_guarded_mutation` is the #369 "
        "write gate for the ALREADY-DECIDED `combo`; `call` is "
        "`_V1_REASSIGN_CALL.get(combo)`, a local tuple-keyed-dict lookup "
        "(#202 repair root cause 1) already structurally recognised as an "
        "optional dispatch subject -- `if call is not None:` needed no "
        "waiver of its own for the same reason `dest is not None` never "
        "did (see round 5, finding 1's own comment block: a LOCAL "
        "tuple-dict lookup is classified during the walk, unlike a "
        "module-level one)",
    ("_handle_reassign_v2",
     "self._guarded_mutation(targets, call, actor_id, role, scope)",
     "return_value", "call is not None"):
        "the v2 sibling of _handle_reassign's own identical-shape waiver "
        "immediately above; `call` is `_V2_REASSIGN_CALL.get(combo)`, the "
        "same local tuple-dict-lookup shape, and the route here is fully "
        "decided upstream by `combo in _V2_REASSIGN_SCHEMA`",
    ("_handle_reassign_v2",
     "self._reject_target_outside_scope(target[0], target[1], actor_id, "
     "role, scope, target[2] if len(target) > 2 else 'scope')", "if_test",
     ""):
        "per-target #369 scope check inside `for target in targets:`; "
        "`len(target) > 2` (this call's own ternary argument) already has "
        "its own waiver above under the SAME name-collision reasoning -- "
        "this waives the ENCLOSING call now that it too is examined as a "
        "bare if-test, not a new routing decision: `target` is one entry "
        "of the ALREADY-decided authorisation-target list",
    ("_handle_reassign",
     "self._reject_target_outside_scope(target[0], target[1], actor_id, "
     "role, scope, target[2] if len(target) > 2 else 'scope')", "if_test",
     ""):
        "the v1 sibling of _handle_reassign_v2's own identical-shape "
        "waiver immediately above; see that entry",
    # -- #202 repair round 7, finding 1 -- newly examined now that a
    # For/AsyncFor loop's OWN `.iter` is audited as an execution-control
    # sink in its own right (see _audit_function's own new comment block),
    # independent of whether the loop's TARGET ends up tracked. Both
    # `_handle_reassign`/`_handle_reassign_v2`'s `for target in targets:`
    # loop already has EVERY other position touching `targets` reviewed
    # and waived above (the `targets.append(...)` builds, the
    # `_refuse_unchosen_context(targets, ...)`/`_guarded_mutation(targets,
    # ...)` calls, and the loop BODY's own `_reject_target_outside_scope`
    # call) -- this is the SAME `targets` list, examined at its one
    # remaining position, the loop statement's own `.iter`. Genuinely
    # NOT the shape this round's finding demonstrates: `targets` is not a
    # boolean-derived REPEAT COUNT standing in for a hidden if/else (the
    # reviewer's own `(1,) * (path == "/api/hidden")` repro) -- it is an
    # ordinary, ALWAYS-at-least-one-element list of (kind, id[, scope
    # label]) authorisation targets (`targets = [(entity, record_id)]`,
    # optionally lengthened by the already-reviewed `dest`/`parent`
    # appends above), and the loop merely runs an identical, already-
    # audited per-target scope check over each one in turn. The route was
    # fully decided upstream by `combo in _V{1,2}_REASSIGN_SCHEMA`
    # (round 3, finding E's own waivers); nothing about WHICH response
    # this handler sends depends on `targets` having one, two or three
    # elements -- only on which authorisation checks run, each already
    # independently reviewed.
    ("_handle_reassign_v2", "targets", "for", ""):
        "the for-loop's own `.iter` position for the SAME `targets` "
        "authorisation-target list every other position already waives "
        "in this dict (see the comment block immediately above) -- an "
        "ordinary bounded per-target authorisation loop, not a "
        "path-dependent iteration-count gate",
    ("_handle_reassign", "targets", "for", ""):
        "the v1 sibling of _handle_reassign_v2's own identical-shape "
        "waiver immediately above; see that entry and the comment block "
        "above it",
    ("do_POST", "not authorize(role, path)", "if_test", ""):
        "do_POST's own generic per-request authorisation gate (#24/#50), "
        "the SAME blanket check `required_permission(path)`/"
        "`scope_violation(...)` (waived above) sit inside/beside: "
        "`authorize(role, path)` decides whether THIS role may perform "
        "the action the path ALREADY selected, not which route it is. "
        "Previously exempt via the structural gap THIS finding closes -- "
        "a bare call directly forming the whole if-test was never even "
        "scanned for tracked arguments; now it is, and gets this "
        "explicit, reviewed waiver instead",
    ("do_POST", "self._guardian_link_or_403(guid, jid)", "if_test", "mga"):
        "POST sibling of get_me_guardian_id_substitute_opportunities_id's "
        "own identical-text waiver above (see that entry for the general "
        "shape): verifies the signed-in guardian's VERIFIED link to the "
        "junior captured by `mga` (jid = mga.group(1)) before setting "
        "that junior's availability. NOT a routing decision -- the route "
        "is already selected by `mga`",
    ("do_POST", "self._guardian_link_or_403(guid, jid)", "if_test", "mgs"):
        "same guardian-link check as the `mga`-branch waiver immediately "
        "above, guarding a DIFFERENT POST action: before the junior "
        "captured by `mgs` (jid = mgs.group(1)) may accept/decline a "
        "coach's substitute offer on the guardian's behalf. Textually "
        "IDENTICAL to that entry -- the SAME reviewed check, applied "
        "twice; #202 repair round 4, finding 3's enclosing-if-text "
        "fingerprint (`mga` vs `mgs`) is what lets both be recorded as "
        "separate, individually exact-one-hit entries rather than one "
        "waiver silently also covering a second, independently-reviewed "
        "call site it was never written against",
    # -- #202 repair round 5, finding 2b -- newly reached now that Return
    # statements are audited at all. Every entry below shares ONE shape:
    # a LOCAL CALLABLE, selected from a small set of already-imported API
    # functions by a captured id whose alternation ALREADY produces one
    # leaf per outcome upstream (a dict keyed by a captured `kind`/`op`/
    # `action`, or -- `call`/`mapper` (v2) -- a ternary on a captured
    # group already covered by this dict's own PRE-EXISTING ternary
    # waivers: `mar.group(2) == 'archive'` and `kind == 'venue'`, both
    # several entries above), then INVOKED. `_propagates_taint`'s
    # `captured` exemption (see its own docstring) does not reach these
    # automatically because none of `fn`/`coach`/`mapper`/`deleter`/`call`
    # is itself a captured id -- each is a NAME BOUND FROM a dict
    # lookup/ternary keyed on one, the SAME "derived, not captured"
    # category `perm`/`violation`/`targets` (finding 1) and `call`
    # (this finding, `_handle_setup_v2`'s own archive/reopen selector,
    # immediately below) are in. NOT a routing decision in any of these:
    # the route is fully decided upstream by the SAME alternation that
    # picks the callable; invoking it merely runs the already-selected
    # implementation.
    # REMOVED (#202 repair round 6, finding 1): this dict used to carry two
    # waivers here, for ("_handle_setup_v2", "self._guarded_mutation([
    # ('season', mar.group(1))], lambda: call(mar.group(1), reason=b.get(
    # 'reason'), actor_id=actor_id), actor_id, role, scope)", "return_value",
    # "mar") and its "lambda"-shaped sibling immediately below it. `call` is
    # bound by `call = (api.archive_season if mar.group(2) == "archive" else
    # api.reopen_season)` (server.py) -- BOTH ternary branches are bare
    # module-attribute references with NO tracked name in them at all; only
    # the ternary's own TEST touches a tracked name, through the provably
    # opaque `mar.group(2)` capture. `call` was ONLY ever a "hit" needing
    # these two waivers because of the exact bug this round's finding 1
    # fixes -- see the REMOVED `self._operator_only(guard)` waiver's own
    # comment, several entries above, for the identical root cause (the
    # OLD bottom-of-function fallback in `_propagates_taint` did not
    # honour the opaque-extraction boundary, so it wrongly walked straight
    # THROUGH `mar.group(2)` and found `mar` "tracked"). DEMONSTRATED
    # dormant (0 hits each) once this round's fix landed -- removed per
    # this module's own discipline, the same as that entry.
    ("_handle_setup_v2",
     "self._guarded_mutation([(kind, md.group(2))], lambda: "
     "mapper(deleter(md.group(2), actor_id)), actor_id, role, scope)",
     "return_value", "md"):
        "#202 repair round 5, finding 2b -- the #369 write gate for the "
        "v2 setup-entity delete route; `deleter` is one of eleven "
        "`api.delete_<entity>` functions selected by a dict keyed on the "
        "captured `kind` (`md.group(1)`, already-decided entity "
        "alternation); `mapper` is `_v2p.venue_to_v2` or identity, "
        "selected by the SAME `kind == 'venue'` ternary this dict already "
        "waives (several entries above) -- see this section's own "
        "comment block for the general shape",
    ("_handle_setup_v2", "mapper(deleter(md.group(2), actor_id))",
     "lambda", "md"):
        "the response-mapping callable, reached as the lambda's own body "
        "instead of as `_guarded_mutation`'s bare argument (this dict's "
        "immediately preceding entry) -- same selectors, same reasoning",
    ("_handle_setup_v2", "deleter(md.group(2), actor_id)",
     "call_argument", "md"):
        "the delete callable itself, reached as `mapper`'s own argument -- "
        "same eleven-way `kind`-keyed dict selection, same reasoning as "
        "the two entries immediately above",
    ("_handle_setup",
     "self._guarded_mutation([(self._V1_SETUP_KIND.get(kind, kind), "
     "md.group(2))], lambda: mapper(deleter(md.group(2), actor_id)), "
     "actor_id, role, scope)", "return_value", "md"):
        "the v1 sibling of _handle_setup_v2's own identical-shape delete "
        "waiver above: `deleter` is one of ten `api.delete_<entity>` "
        "functions keyed on the captured `kind`; `mapper` is "
        "`_to_v1.get(kind, lambda r: r)`, a v1-wire-shape response mapper "
        "for the same already-decided `kind` (a LOCAL, non-tuple-keyed "
        "dict `.get()` -- structurally unlike the tuple-keyed dicts this "
        "module's walker recognises, so `mapper` reaches `tracked` only "
        "via the generic fallback, same as `deleter`) -- not a routing "
        "decision, the route was already decided by `md`'s own entity "
        "alternation",
    ("_handle_setup", "mapper(deleter(md.group(2), actor_id))",
     "lambda", "md"):
        "the response-mapping callable, reached as the lambda's own body "
        "instead of as `_guarded_mutation`'s bare argument (this dict's "
        "immediately preceding entry) -- same selectors, same reasoning",
    ("_handle_setup", "deleter(md.group(2), actor_id)",
     "call_argument", "md"):
        "the delete callable itself, reached as `mapper`'s own argument -- "
        "same ten-way `kind`-keyed dict selection, same reasoning as the "
        "two entries immediately above",
    ("do_POST", "fn(gid, player_id, user_id)", "call_argument", "sub"):
        "#202 repair round 5, finding 2b -- `fn` is one of three "
        "`api.{accept,decline,add_substitute_to_roster}` functions, "
        "selected by `{...}[op]` keyed on `op` (`sub.group(2)`, captured "
        "by the ALREADY-DECIDED `substitutes/(offer|accept|decline|"
        "add-to-roster)` alternation `sub` matches) -- a dict SUBSCRIPT, "
        "not `.get()`, so `fn` reaches `tracked` only via the generic "
        "fallback rather than this module's `.get()`-shaped recogniser. "
        "Not a routing decision: `gid`/`player_id` are already-captured "
        "ids handed to the already-selected implementation, the route "
        "was decided upstream by `sub`'s own regex alternation",
    ("do_POST", "coach(gid, user_id)", "call_argument", "coach"):
        "#202 repair round 5, finding 2b -- `coach` is one of "
        "`api.{lock_roster,unlock_roster,cancel_game}`, selected by "
        "`{...}.get(action)` keyed on the ALREADY-DECIDED `action` -- not "
        "a routing decision, the route was decided upstream by the "
        "literal `action` alternation this dict-`.get()` merely narrows "
        "an implementation for",
    # -- #202 repair round 6, finding 1 -- newly reached now that
    # `_mentions_tracked`/`_tracked_mentions` recognise `self.path` at any
    # depth (not only when it is the bare operand) and the bottom-of-
    # function fallback in `_propagates_taint` no longer blindly walks
    # PAST the opaque-extraction boundary. `self.path` appears throughout
    # `_dispatch_get`'s own GET query-string idiom -- `from urllib.parse
    # import parse_qs, urlparse; qs = parse_qs(urlparse(self.path).query)`
    # -- which this module had NEVER been able to see through before this
    # round (`self.path` nested two calls deep was invisible to every
    # existing check). Every entry in this block shares ONE shape: a GET
    # query-string FILTER/IDENTITY parameter for a route the enclosing
    # literal `path`/`sub` test has ALREADY fully decided -- the query
    # string is never compared against by any dispatch test anywhere in
    # this file (every test strips it first: `path = self.path.split("?",
    # 1)[0]`), so a value read FROM it can change the response body or
    # refuse the request (a resource-scoping/ownership check, same
    # category as `scope_violation(...)`'s own waiver above) but never
    # which route this is.
    ("_dispatch_get", "parse_qs(urlparse(self.path).query)", "assign_rhs",
     "path == '/api/setup/scheduling-policy'"):
        "get_setup_scheduling_policy: the query-string parse itself. "
        "`self.path` is examined here ONLY for its query component "
        "(everything after '?', which dispatch never compares against); "
        "the route was already selected by the literal `path` test",
    ("_dispatch_get", "urlparse(self.path)", "attribute",
     "path == '/api/setup/scheduling-policy'"):
        "the same query-string parse, reached a second time as `urlparse`'s "
        "own call node (its result's `.query` attribute is what the "
        "immediately-preceding waiver's `parse_qs(...)` consumes) -- "
        "identical reasoning",
    ("_dispatch_get", "qs.get('scope_type')", "boolop",
     "path == '/api/setup/scheduling-policy'"):
        "one of three optional filter/scope query parameters "
        "(scope_type/scope_id/season_id) for the already-decided "
        "scheduling-policy read; see this block's own comment above",
    ("_dispatch_get", "qs.get('scope_id')", "boolop",
     "path == '/api/setup/scheduling-policy'"):
        "same three-parameter group as `qs.get('scope_type')` immediately "
        "above -- see that entry",
    ("_dispatch_get", "qs.get('season_id')", "boolop",
     "path == '/api/setup/scheduling-policy'"):
        "same three-parameter group as `qs.get('scope_type')`, two entries "
        "above -- see that entry",
    ("_dispatch_get",
     "api.get_scheduling_policy(scope_type=(qs.get('scope_type') or "
     "[None])[0], scope_id=(qs.get('scope_id') or [None])[0], "
     "season_id=(qs.get('season_id') or [None])[0])", "call_argument",
     "path == '/api/setup/scheduling-policy'"):
        "the service call itself, consuming all three already-waived "
        "query parameters above -- not a routing decision, `path` alone "
        "already selected this route",
    ("_dispatch_get", "qs.get('team_id')", "boolop", "path == '/api/players'"):
        "get_players: an optional team-scope filter for the already-"
        "decided player-list read (#369) -- see this block's own comment "
        "above",
    ("_dispatch_get",
     "api.list_players(team_id, include_email=True, user_id=user_id, "
     "role=role, scope=scope)", "call_argument", "path == '/api/players'"):
        "the service call consuming the already-waived `team_id` filter "
        "immediately above -- not a routing decision",
    ("_dispatch_get", "qs.get('recipient_ref')", "boolop",
     "path == '/api/notifications/preferences'"):
        "get_notifications_preferences: the recipient identity for a "
        "channel-preferences read (#81) -- see this block's own comment "
        "above",
    ("_dispatch_get", "self._prefs_guard(recipient_ref)", "if_test",
     "path == '/api/notifications/preferences'"):
        "a per-request authorisation gate (operator -> any recipient; a "
        "signed-in user -> only their own), the SAME 'blanket guard, not a "
        "route selector' shape as `authorize(role, path)`/`_operator_only` "
        "above -- refuses (403/401) or falls through to the SAME "
        "already-decided read",
    ("_dispatch_get", "api.get_notification_preferences(recipient_ref)",
     "call_argument", "path == '/api/notifications/preferences'"):
        "the service call consuming the already-waived `recipient_ref` -- "
        "not a routing decision",
    ("_dispatch_get", "qs.get('actor_type')", "boolop",
     "path == '/api/calendar-feeds'"):
        "get_calendar_feeds: one of two actor-identity query parameters "
        "for a feed-token list read (#82) -- see this block's own comment "
        "above",
    ("_dispatch_get", "qs.get('actor_ref')", "boolop",
     "path == '/api/calendar-feeds'"):
        "the second of the two actor-identity parameters, alongside "
        "`qs.get('actor_type')` immediately above -- same reasoning",
    ("_dispatch_get", "self._feed_guard(actor_type, actor_ref)", "if_test",
     "path == '/api/calendar-feeds'"):
        "a per-request authorisation gate (operator -> any actor; a "
        "signed-in user -> only their own), the SAME 'blanket guard, not a "
        "route selector' shape as `self._prefs_guard(recipient_ref)` "
        "above",
    ("_dispatch_get", "api.list_calendar_feed_tokens(actor_type, actor_ref)",
     "call_argument", "path == '/api/calendar-feeds'"):
        "the service call consuming the two already-waived actor-identity "
        "parameters above -- not a routing decision",
    # This sub-group is reached inside `if m:` (a regex match on
    # `/api/games/{id}(/{sub})?`), so its own enclosing if-text is the
    # narrower `sub == '...'` literal test that already selected the leaf
    # -- an own-team RESOURCE-SCOPING check (a coach/player may read only
    # their own team's data; operators any team), the SAME category as
    # `scope_violation(...)`'s own do_POST waiver above, not a route
    # selector. `team_id` defaults to the caller's own team when the query
    # string omits it, and is read from `self.path`'s query component the
    # SAME way as every other entry in this block.
    ("_dispatch_get",
     "role in (Role.COACH, Role.PLAYER) and own_team and (team_id != "
     "own_team)", "if_test", "sub == 'availability-summary'"):
        "get_games_id_availability_summary (#89): own-team scoping for a "
        "coach/player, operators exempt -- see this sub-group's own "
        "comment above",
    ("_dispatch_get", "api.get_availability_summary(gid, team_id)",
     "call_argument", "sub == 'availability-summary'"):
        "the service call consuming the already-waived `team_id` scope "
        "check immediately above -- not a routing decision",
    ("_dispatch_get",
     "role == Role.COACH and own_team and (team_id != own_team)", "if_test",
     "sub == 'substitute-candidates'"):
        "get_games_id_substitute_candidates (#112): own-team scoping for a "
        "coach, operators exempt -- see this sub-group's own comment "
        "above (a `MANAGE_ROSTER` permission gate runs first, independent "
        "of this scoping check)",
    ("_dispatch_get", "api.get_substitute_candidates(gid, team_id)",
     "call_argument", "sub == 'substitute-candidates'"):
        "the service call consuming the already-waived `team_id` scope "
        "check immediately above -- not a routing decision",
    ("_dispatch_get",
     "role == Role.COACH and own_team and (team_id != own_team)", "if_test",
     "sub == 'substitute-addable'"):
        "get_games_id_substitute_addable (#114): the same own-team scoping "
        "as substitute-candidates immediately above, textually identical "
        "but reached from a DIFFERENT `sub` branch -- the enclosing-if-"
        "text fingerprint (`substitute-candidates` vs `substitute-"
        "addable`) is what keeps the two from masquerading as one",
    ("_dispatch_get", "api.get_addable_substitutes(gid, team_id)",
     "call_argument", "sub == 'substitute-addable'"):
        "the service call consuming the already-waived `team_id` scope "
        "check immediately above -- not a routing decision",
    # -- #202 repair round 6, finding 1's OTHER new shape: a bare Subscript
    # (no Call anywhere around it) keyed on a tracked name, independently
    # audited the same way an unlisted Call already is (see
    # `_propagates_taint`'s own new `ast.Subscript` branch). Both entries
    # below are the SAME already-reviewed `check_body(b, **_SCHEMA[combo])`
    # bare statement (round 3, finding E's own waiver, above) -- that
    # waiver covers the CALL as a whole; the Subscript nested inside its
    # `**kwargs` unpack is now ALSO independently visited and needs its
    # own entry, the same "each real one still needs its own reviewed
    # waiver" discipline this dict uses throughout. NOT a routing
    # decision: `combo` is already PROVEN a valid key of this exact
    # SCHEMA dict by the enclosing `combo in _V{1,2}_REASSIGN_SCHEMA`
    # test, which is what selected this route in the first place (#202
    # repair root cause 1) -- re-indexing the SAME dict by the SAME combo
    # to unpack its value as `check_body` validation kwargs does not pick
    # a different one.
    ("_handle_reassign", "_V1_REASSIGN_SCHEMA[combo]", "keyword",
     "combo in _V1_REASSIGN_SCHEMA"):
        "the `**_V1_REASSIGN_SCHEMA[combo]` unpack inside `check_body(b, "
        "**_V1_REASSIGN_SCHEMA[combo])` (round 3, finding E's own waiver "
        "above covers that call as a whole) -- see this sub-group's own "
        "comment for why the nested Subscript is not a second routing "
        "decision",
    ("_handle_reassign_v2", "_V2_REASSIGN_SCHEMA[combo]", "keyword",
     "combo in _V2_REASSIGN_SCHEMA"):
        "the v2 sibling of `_handle_reassign`'s own identical-shape waiver "
        "immediately above -- same reasoning, `check_body(b, "
        "**_V2_REASSIGN_SCHEMA[combo])` is round 3, finding E's own "
        "pre-existing waiver for the call as a whole",
    # -- #202 repair round 6, finding 2 -- newly reached now that a
    # `with`/an implicit-exception-into-handler data flow is audited at
    # all (this dict's own two entries below are the SECOND of those, the
    # try-body-scoped except-handler check; finding 2's other three gaps
    # -- closures, `with` context expressions, and the captured-exemption
    # narrowing for dispatch selection -- need no waiver against the real
    # server.py: it has no local closure that reads a tracked free
    # variable, no `with` statement at all, and no captured id used to
    # select-then-immediately-invoke a callable). Both entries below are
    # the SAME shape: `except BodyError as exc: return self._send_json(
    # exc.payload, exc.status)`, whose own enclosing try body is
    # `check_body(b, **_V{1,2}_REASSIGN_SCHEMA[combo])` (round 3, finding
    # E's own pre-existing waiver, several entries above, covers that
    # call). NOT a routing decision: `check_body` raises `BodyError` on a
    # malformed REQUEST BODY for the ALREADY-DECIDED `combo`, and this
    # handler's only action is to translate that into the response --
    # `exc.payload`/`exc.status` are returned directly, never inspected or
    # compared to select a different template -- the SAME "produces the
    # final answer for an already-decided route" shape as every other
    # `check_body`-adjacent waiver in this dict.
    ("_handle_reassign",
     "except BodyError as exc:\n    return self._send_json(exc.payload, "
     "exc.status)", "try", "combo in _V1_REASSIGN_SCHEMA"):
        "the exception handler for `check_body`'s own BodyError, "
        "translating a malformed-body failure straight into the "
        "response -- see this sub-group's own comment above",
    ("_handle_reassign_v2",
     "except BodyError as exc:\n    return self._send_json(exc.payload, "
     "exc.status)", "try", "combo in _V2_REASSIGN_SCHEMA"):
        "the v2 sibling of `_handle_reassign`'s own identical-shape "
        "waiver immediately above -- same reasoning",
}


#: ``self.`` methods that only WRITE the HTTP response -- ``send_response``/
#: ``send_header``/``end_headers``/``self.wfile.write``, nothing else --
#: never a dispatch decision (#202 repair round 5, finding 2b). Verified
#: against server.py's own four definitions: none contains an ``if``/
#: ``elif`` on anything path-derived; ``_send_api``'s only branch is a
#: PAYLOAD-SHAPE check (``"error" in payload``) mapping a domain error to
#: an HTTP status, not a routing choice. A ``self._send_json(...)``/
#: ``self._send_api(...)``/``self._send_ics(...)``/``self._send_status(...)``
#: call reached from a Return is the SAME 'produces the final answer for an
#: ALREADY-decided route' shape as a waived call (see _propagates_taint's
#: own docstring) -- checked at that SAME point, only once
#: ``_mentions_tracked`` has already fired (never any earlier -- an
#: unconditional skip here would swap this function's precise, opaque-
#: extraction-aware "provably unrelated" verdict for the cruder fallback
#: at the bottom of ``_propagates_taint``, which does not honour that
#: boundary; see the exact regression this caused, documented at that
#: call site): skip judging the wrapper call itself and let ``ast.walk``
#: keep examining whatever is nested inside its arguments on its own
#: merits (a genuinely hidden dispatch table nested INSIDE one of these
#: calls, e.g.
#: ``self._send_json(ROUTES[path](), 200)``, is still reached as its own,
#: separate Call node and still raises).
_TERMINAL_RESPONSE_SENDERS = {"_send_json", "_send_api", "_send_ics",
                              "_send_status"}

_PATH_OPS = ("split", "rsplit", "strip", "lstrip", "rstrip", "lower", "upper",
             "partition", "rpartition", "removeprefix", "removesuffix",
             "replace", "format", "join", "casefold")

#: pathlib ``Path`` methods that reshape a filesystem location without
#: introducing new, unrelated information -- the Path analogue of _PATH_OPS
#: for strings (#202 repair root cause 2). ``(STATIC_DIR / rel).resolve()``
#: is still a location DERIVED from ``rel`` in exactly the sense
#: ``path.strip()`` is still derived from ``path``; deliberately NOT widened
#: to "any attribute call on a tracked receiver" for the same reason _PATH_OPS
#: isn't -- that reopens the hole taint tracking exists to close (see the
#: docstring below).
_PATH_METHODS = ("resolve", "absolute", "expanduser", "with_name",
                 "with_suffix", "with_stem", "joinpath")
#: The no-argument pathlib PROPERTIES that do the same, read as plain
#: attributes rather than called.
_PATH_PROPERTIES = ("parent", "name", "stem", "suffix", "parts")


#: ``X.group(<int constant>)`` -- the ONLY shape a regex capture surfaces as
#: anywhere in this module (:meth:`_DispatchWalker._as_group_call` and
#: :meth:`_DispatchWalker._group_origin` both hardcode exactly this pattern).
#: A captured value consumed this way -- however it is spelled, bound to a
#: local first (``gid = m.group(1)``) or written inline as a call argument
#: (``api.calendar_feed_ics(cal.group(1))``) -- is the SAME "produces a
#: RESULT, not a routing decision" case this module already draws elsewhere:
#: the walker's own group machinery accounts for every way a captured value
#: can propagate further (becoming a new dispatch subject, joining a tracked
#: tuple, ...); ``_propagates_taint``/``_mentions_tracked`` must not ALSO
#: treat the same call as an unrecognised-call escape merely because it was
#: written inline instead of bound to a name first.
def _is_known_capture_extraction(node) -> bool:
    return (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            and node.func.attr == "group" and len(node.args) == 1
            and isinstance(node.args[0], ast.Constant))


#: pathlib ``Path`` methods that CONSUME an already-resolved location to
#: produce something UNRELATED to any path/route (file content, a
#: readable stream, ...) rather than another Path -- the terminal end of
#: the SAME chain ``_PATH_METHODS`` reshapes. The real ``_serve_static``
#: demonstrates this: ``target`` is legitimately tracked (#202 repair root
#: cause 2), but ``data = target.read_bytes()`` reads the FILE'S BYTES --
#: exactly as unrelated to routing as ``ics = api.calendar_feed_ics(
#: cal.group(1))``, just spelled as a Path method instead of a service
#: call. NOT the same list as ``_PATH_METHODS`` (those still return a
#: Path, further reshapable/comparable); these terminate the chain, the
#: pathlib analogue of a captured regex group.
_PATH_CONSUMING_METHODS = ("read_bytes", "read_text", "open")


#: A node that EXTRACTS or CONSUMES a narrower, unrelated value from a
#: tracked one, as opposed to one that RESHAPES a tracked value while
#: keeping it fully comparable (``_PATH_OPS``/``_PATH_METHODS`` --
#: ``path.strip()`` is still the whole path, just trimmed). A captured
#: regex group and a ``_PATH_CONSUMING_METHODS`` call are the CALL-shaped
#: case; the pathlib PROPERTIES (``.suffix``, ``.parent``, ``.name``,
#: ``.stem``, ``.parts``) are the ATTRIBUTE-shaped case -- not a third
#: thing, ``target.suffix`` is just a file extension, exactly as narrow
#: and exactly as opaque-once-extracted as a captured id. All are already
#: the SAME "produces a RESULT, not the routing-relevant value itself"
#: pattern this module draws for ``api.calendar_feed_ics(cal.group(1))``;
#: the real ``_serve_static``'s ``CONTENT_TYPES.get(target.suffix, ...)``
#: and ``target.read_bytes()`` are the concrete cases that demonstrated
#: both need the SAME treatment -- `target` is legitimately tracked (#202
#: repair root cause 2: pathlib reshaping IS still the path), but reading
#: or classifying it this way is not a routing decision, the same way a
#: service keyed on a captured id isn't. Deliberately NOT extended to
#: ``_PATH_OPS``/``_PATH_METHODS`` themselves: those calls are still
#: independently visited as their own node by the loop below (and
#: correctly pass as "manipulates" there), so a tracked name reached ONLY
#: through full reshaping (``path.strip()``) must still surface as a
#: mention when it is nested inside some OTHER unlisted call's arguments
#: (e.g. a routing comparison hidden inside a genexpr) -- only an
#: EXTRACTION/CONSUMPTION's receiver must not leak outward like that.
def _is_callee(node: ast.AST, parents: Optional[dict]) -> bool:
    """Is ``node`` -- however much transparent composition sits between it
    and its eventual use -- about to be INVOKED somewhere in its enclosing
    statement, rather than merely read as data?

    #202 repair round 6, finding 2: the ``captured`` exemption (see
    :func:`_propagates_taint`'s own docstring) is deliberately narrow --
    "a NON-``self.`` call whose only tracked mentions are already-captured
    names" -- but that test alone cannot tell apart ``api.get_x(gid)`` (a
    captured id handed to a FIXED, KNOWN service function as inert DATA --
    exactly the shape the exemption exists for) from ``handlers.get(action,
    default)()`` (a captured id used to SELECT WHICH CALLABLE RUNS, then
    immediately invoke it -- the SAME dispatch-table shape
    ``PREDICATES[path]()``/``ROUTES[path]()`` already require full
    scrutiny for, round 5 finding 2a and this round's own Subscript audit,
    just with the tracked key replaced by a captured group). DEMONSTRATED:
    a regex-captured ``action`` selecting ``handlers.get(action,
    default_handler)(self)`` extracted as a single ``/api/{}`` wildcard
    while the live handler answered 200 for one concrete value and 404 for
    another -- the captured exemption accepted it because its ONLY
    tracked mention (``action``) was captured, never asking whether the
    call's own RESULT was about to be called. Introduced this function to
    gate the ``captured`` exemption OFF for exactly this shape: a Call or
    Subscript about to be invoked never qualifies, no matter how narrowly
    its own tracked mentions are captured-only. A captured id used as a
    plain ARGUMENT or receiver elsewhere (``deleter(md.group(2),
    actor_id)``, ``mapper(deleter(...))`` -- real server.py shapes, see
    ``_AUDIT_WAIVERS``) is UNAFFECTED by any round of this function's own
    history, including this one: neither is ever part of what invokes it
    (each is bound to a plain local NAME first via a separate assignment,
    and a bare ``ast.Name`` callee is never independently examined by the
    Call/Subscript-shaped checks this gates at all).

    #202 repair round 7, finding 1: the original version only checked
    ``node``'s own IMMEDIATE parent, so a call reached through a RECEIVER
    CHAIN -- ``handlers.get(action, default_handler).serve(self)``, whose
    dispatch-selecting Call's immediate parent is the ``.serve``
    ``ast.Attribute``, never a Call directly -- answered False even though
    ``.serve(self)`` invokes exactly what that Call returns. Widened to
    walk UP through every ``ast.Attribute``/``ast.Subscript`` layer where
    the node reached so far is that layer's own RECEIVER (``.value``)
    before asking "is this a Call's callee". DEMONSTRATED the same way:
    ``/api/{}`` wildcard, 200/404 live divergence.

    #202 repair round 8, finding 1 (external review): round 7's climb
    still only continues past TWO curated shapes -- Attribute and
    Subscript, one hop at a time -- so it stops, returning False, the
    INSTANT ``node``'s own immediate parent is anything else, before ever
    asking what sits above THAT. A ``Tuple``, a ``List`` or an ``IfExp``
    holding ``node`` is none of those two shapes. DEMONSTRATED, the
    reviewer's own repro, verbatim: a regex-captured ``action`` selecting
    ``(handlers.get(action, default_handler),)[0].serve(self)`` -- a
    ONE-ELEMENT TUPLE immediately indexed by a literal ``0`` -- extracted
    as a single ``/api/{}`` wildcard while a real live handler answered
    200 for one concrete value and 404 for another; the SAME true of the
    identical shape spelled as a ``List``
    (``[handlers.get(...)][0].serve(self)``) or as a conditional
    expression (``(handlers.get(...) if flag else default_handler)
    (self)``) wrapping the captured-derived call before its receiver
    chain or invocation. This exact category has now recurred three times
    (round 5 finding 5's own default-deny expression-operand gap, round 6
    finding 2's original narrowing, round 7 finding 1's receiver-chain
    climb) across three different specific wrapper shapes each time --
    enumerating one more transparent-wrapper shape as it is separately
    discovered is not converging, so this round REPLACES the curated
    upward climb entirely rather than extending its shape list a fourth
    time.

    The replacement needs no notion of "transparent wrapper" at all:
    climb ``parents`` from ``node`` up to the nearest enclosing
    ``ast.stmt`` (the smallest scope always cheaply available without
    re-deriving it from the expression's own shape -- climbing any
    FURTHER, e.g. to the whole enclosing function, would answer identically
    since an AST node object is never shared or interned, so ``node`` can
    structurally occur at only the one position it actually sits at,
    however large a scope containing that position is searched), then,
    for EVERY ``ast.Call`` anywhere in that statement, ask whether
    ``node`` is present ANYWHERE inside that call's own ``.func``
    subtree -- an ordinary, unrestricted ``ast.walk``, which recurses
    through a Tuple, a List, an IfExp, a BoolOp, or any node shape
    Python's grammar has or will ever add, none of which this function
    needs to name to recognise. ``node`` can only be found there if it
    really is, structurally, part of what that Call is about to invoke;
    a plain ARGUMENT position (``deleter(md.group(2), actor_id)``,
    ``mapper(deleter(...))``) is never inside any Call's own ``.func``
    subtree, so both real server.py shapes the ``captured`` exemption
    exists for stay exempt exactly as every prior round left them. This
    is deliberately the MORE conservative of the two fixes the review
    offered over a fourth curated shape: it now also refuses a captured
    value found in a callee/receiver position through composition no
    round of this function ever enumerated by name, which is the point.

    #202 repair round 9 (external review): this function alone cannot
    carry the whole "is a captured selector about to be invoked" question
    any further -- see :func:`_captured_arg_safe_callee`'s own docstring
    for why. The reviewer's own round-9 repro --
    ``invoke(handlers.get(action, default_handler), self)`` with
    ``invoke = lambda fn, h: fn.serve(h)`` -- hands the dispatch-selecting
    call to ``invoke`` as a plain ARGUMENT; it never sits inside ANY
    call's own ``.func`` subtree in this statement (``invoke`` itself is
    invoked BY ITS CALLER, elsewhere, not by anything visible here), so
    this function correctly -- and, after this round, PERMANENTLY --
    answers False for it, exactly as it always correctly has for the
    genuinely inert ``api.get_item(gid)``. That is not a bug in this
    function: a purely SYNTACTIC, single-statement climb structurally
    cannot see what an arbitrary function does with an argument inside
    its own body, a different function entirely -- no further shape this
    function could be taught to recognise closes that gap, only running
    the callee's body would. The round-9 fix is therefore a SEPARATE,
    additional gate in :func:`_propagates_taint` (`_captured_arg_safe_
    callee`), not a further widening of this function's own climb; this
    function's contract stays exactly "is this node about to be invoked,
    syntactically, in this statement" -- unchanged, neither more nor less
    conservative.
    """
    if parents is None:
        return False
    statement = node
    while not isinstance(statement, ast.stmt):
        parent = parents.get(id(statement))
        if parent is None:
            break
        statement = parent
    for candidate in ast.walk(statement):
        if not isinstance(candidate, ast.Call):
            continue
        for sub in ast.walk(candidate.func):
            if sub is node:
                return True
    return False


#: #202 repair round 9 (external review): the ONLY call targets the
#: ``captured``-only exemption in :func:`_propagates_taint` may ever treat
#: a captured value handed to as inert DATA, now that "any non-``self.``
#: call" is no longer good enough (see :func:`_captured_arg_safe_callee`'s
#: own docstring for why). Deliberately ONE entry: ``api``, the module-
#: level API FACADE this codebase's own layering (CLAUDE.md) guarantees
#: performs no HTTP routing -- the SAME guarantee this module's docstrings
#: have invoked since round 5 finding 2b introduced the ``captured``
#: exemption in the first place. Confirmed (by the SAME instrumentation
#: methodology round 8's own docstring used -- a temporary counter at each
#: exemption site, run against the real file, removed before commit) to
#: cover 37 of the real server.py's 39 captured-only call/subscript sites;
#: the remaining two (a local dict of response-shape mappers, and a
#: builtin string method called ON the captured value itself, both in
#: ``_handle_setup``) do NOT fit this shape and are reviewed individually
#: as their own ``_AUDIT_WAIVERS`` entries instead -- see those entries'
#: own comments, just below ``_handle_setup``'s existing waivers.
#: Extending this set is a REVIEW, exactly the weight of adding an
#: ``_AUDIT_WAIVERS`` entry: do not add a second name merely to make some
#: future call site pass without individually confirming it is a genuine
#: service boundary that never invokes, stores, or forwards whatever it is
#: handed -- that confirmation is the entire point of keeping this set
#: small and explicit rather than defaulting to "anything not self.".
#:
#: #202 repair round 10 (external review): membership here is NECESSARY
#: but, since this round, no longer SUFFICIENT for a name to earn the
#: exemption at a given call site -- this set alone answers "which
#: SPELLING may ever qualify", never "does THIS function's own body
#: actually, provably bind that spelling to the reviewed object", and the
#: round-10 finding proved those are not the same question. See
#: ``_CAPTURED_ARG_SAFE_CALLEE_TRUSTED_SOURCES`` and
#: :func:`_captured_arg_trusted_roots`, just below, for what closes the
#: gap -- derived from that dict's keys so the two are never extended out
#: of step with each other.
#:
#: #202 repair round 10 (external review): the EXACT source expression
#: each name above must be proven -- by :func:`_captured_arg_trusted_roots`,
#: never by spelling -- to be bound from, dominating and unrebound, before
#: the exemption may fire for it in a GIVEN function. Matched by EXACT
#: ``ast.unparse()`` text, the SAME "is this the literal reviewed shape,
#: not merely something that LOOKS like it" discipline
#: ``_DO_HEAD_SAFE_SHAPE``/``_DO_OPTIONS_SAFE_SHAPE`` already use elsewhere
#: in this module -- ``getattr(STATE, "api")``, an aliased ``S = STATE;
#: api = S.api``, or any other expression that only RESOLVES to the same
#: object at runtime does NOT match, and stays outside the exemption
#: exactly like everything else this module cannot prove without running
#: the program. ``"api": "STATE.api"`` is the one entry today: the module
#: singleton's own facade attribute, the SAME expression every real
#: server.py call site (``_dispatch_get``, ``do_POST``,
#: ``_handle_reassign``/``_handle_reassign_v2``,
#: ``_handle_setup``/``_handle_setup_v2``) assigns ``api`` from (confirmed
#: by direct AST inspection, all six sites, before this fix landed).
#: Extending this dict is the SAME weight of review as extending
#: ``_CAPTURED_ARG_SAFE_CALLEE_ROOTS`` used to be on its own -- the two are
#: now meant to be extended TOGETHER, which is why ``ROOTS`` is derived
#: from this dict rather than declared independently.
#:
#: #202 repair round 11 (this round's own independent verify track):
#: matching this dict's TEXT is necessary but was not, until this round,
#: SUFFICIENT either -- a trusted source expression can itself name a free
#: variable (``STATE``, for the one entry here), and round 10's own check
#: proved nothing about whether THAT name is what it claims to be in a
#: given function. :func:`_has_dominating_trusted_binding` now also
#: requires every such free variable to be provably unshadowed (see
#: :func:`_trusted_source_free_roots`) before the exemption fires --
#: extending a value in this dict to something with a MORE COMPLEX free-
#: variable shape than a single bare name (``STATE.api``) stays within
#: that same check's coverage automatically, since it walks the RHS's own
#: parsed AST rather than special-casing this one shape.
_CAPTURED_ARG_SAFE_CALLEE_TRUSTED_SOURCES = {"api": "STATE.api"}
_CAPTURED_ARG_SAFE_CALLEE_ROOTS = frozenset(_CAPTURED_ARG_SAFE_CALLEE_TRUSTED_SOURCES)


def _name_rebinding_sites(name: str, fn: ast.FunctionDef) -> list:
    """Every AST node ANYWHERE in ``fn`` that binds (or unbinds) ``name`` --
    an ``ast.Name`` in Store or Del context (an ordinary assignment target,
    however deeply nested inside a tuple/list/starred unpack, a
    ``for``/comprehension target, a ``with ... as`` target, a walrus, an
    aug-assign, a ``del``), PLUS the handful of binding forms Python's own
    grammar does not spell as an ``ast.Name`` at all: a function/lambda
    PARAMETER (``ast.arg``), an ``except ... as name:`` handler (a plain
    ``str`` on ``ExceptHandler.name``, never a Name node), ``global``/
    ``nonlocal name``, ``import ... as name``, and (#202 repair round 13,
    finding 2 -- see below) a ``match``/``case`` CAPTURE pattern: a bare
    capture (``case name:``, ``ast.MatchAs`` with ``.name`` set) or a
    mapping-rest capture (``case {**name}:``, ``ast.MatchMapping`` with
    ``.rest`` set).

    #202 repair round 10 (external review): used by
    :func:`_captured_arg_trusted_roots` to answer "is ``name`` bound
    EXACTLY once in this function, at a dominating, never-rebound
    assignment" -- a STRICTLY BROADER question than
    :func:`_binding_value_and_targets` answers (that function only ever
    needs "does a VALUE flow into this name", so it correctly leaves a
    parameter or an ``except``-handler out of ITS OWN model, per its own
    docstring's documented boundary). Here the opposite bias is correct: a
    parameter or an except-handler binds a value this module can prove
    NOTHING about, and must count against "exactly one, provably-trusted,
    unrebound binding" exactly as hard as an explicit ``api = evil_api``
    reassignment does -- the reviewer's own required "parameter shadowing"
    coverage is exactly this case.

    #202 repair round 13, finding 2 (external review): round 12 finding 1
    documented, but deliberately did NOT fix, that this enumeration missed
    two more binding spellings Python's structural pattern matching adds --
    ``ast.MatchAs`` (a bare ``case NAME:`` capture) and ``ast.MatchMapping``
    (a mapping-rest ``case {**NAME}:`` capture) -- each binds its own name
    for the REST of the enclosing function exactly as a plain assignment
    would (a ``match`` statement introduces no scope of its own), the SAME
    "spelling Python's grammar does not write as an ``ast.Name``" shape
    ``ast.arg``/``ExceptHandler``/``Global``/``Nonlocal``/``Import`` are
    already special-cased for, just a THIRD binding form neither round 10
    nor round 11 had reason to enumerate at the time. FIXED here rather
    than left as a documented gap: round 12's own "not exploitable today"
    argument (server.py has no ``match`` statement) rested on treating this
    as merely latent, but a corrected, EXECUTABLE repro (the capture
    textually BEFORE the trusted ``api = STATE.api`` read, so Python's
    static per-function scoping -- which makes ``STATE`` local to the
    WHOLE function the instant ANY statement in it binds ``STATE``, capture
    included -- does not raise ``UnboundLocalError`` before the read is
    even reached) shows the underlying Python semantics are exploitable
    NOW, independent of whether server.py happens to use this construct
    today; see ``CapturedArgumentProvenanceTests``'s match-capture cases
    (test_route_extract.py) for the live-HTTP proof and
    :func:`_has_dominating_trusted_binding`'s own docstring for how this
    closes the exemption. A ``getattr(ast, ..., ())`` fallback keeps this
    a no-op (never an ``AttributeError``) on a Python old enough to lack
    structural pattern matching, the same defensive style
    ``_ROLE_ATTRS``'s own ``hasattr(ast, "match_case")`` guard already
    uses elsewhere in this module.

    Deliberately counts a binding inside a NESTED closure (a ``def``/
    ``lambda`` inside ``fn``) too, even though such a closure's own local
    is, by Python's actual lexical scoping, a SEPARATE variable that
    leaves an outer binding of the same name untouched -- over-
    conservative on paper (a hypothetical function with both a legitimate
    outer ``api = STATE.api`` and an unrelated inner closure parameter
    also spelled ``api`` would lose the exemption for its own, unaffected
    outer uses too), but no such closure exists anywhere in server.py
    today (``CapturedArgumentProvenanceTests`` pins the real-file check
    unaffected), and "occasionally over-flag a safe function, review it
    explicitly" is the same accepted cost this module's fail-closed
    philosophy already pays everywhere else rather than risk under-
    flagging a genuinely shadowed one.
    """
    sites = []
    for node in ast.walk(fn):
        if isinstance(node, ast.Name) and node.id == name \
                and not isinstance(node.ctx, ast.Load):
            sites.append(node)
        elif isinstance(node, ast.arg) and node.arg == name:
            sites.append(node)
        elif isinstance(node, ast.ExceptHandler) and node.name == name:
            sites.append(node)
        elif isinstance(node, (ast.Global, ast.Nonlocal)) and name in node.names:
            sites.append(node)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                if (alias.asname or alias.name.split(".")[0]) == name:
                    sites.append(node)
        elif isinstance(node, getattr(ast, "MatchAs", ())) and node.name == name:
            sites.append(node)
        elif isinstance(node, getattr(ast, "MatchMapping", ())) \
                and node.rest == name:
            sites.append(node)
    return sites


def _trusted_source_free_roots(expr: ast.expr) -> frozenset:
    """Every distinct NAME referenced, in Load context, anywhere in a
    trusted source expression's own AST -- for ``STATE.api`` this is
    ``{"STATE"}``: the free variable(s) the expression is rooted at.

    #202 repair round 11 (this round's own independent verify track):
    :func:`_has_dominating_trusted_binding` proves the LHS name (``api``)
    is bound exactly once, dominating, unrebound -- but its final check is
    a bare ``ast.unparse(stmt.value) == trusted_source`` TEXTUAL match,
    which by itself says nothing about whether the free variables INSIDE
    that trusted text (``STATE``) are themselves what they claim to be in
    the audited function. This is what lets that caller ask the SAME
    "is this free variable shadowed here" question of ``STATE`` that it
    already asks of ``api`` -- one level up the same expression, and no
    further (a trusted source that itself named something needing its
    OWN provenance proof, rather than merely needing to be UNSHADOWED,
    would be a new architectural question, not one this function begs).

    Deliberately walks ``expr`` -- the ALREADY-PARSED ``stmt.value`` the
    caller just compared by ``ast.unparse()``, never a fresh parse of the
    trusted-source STRING -- so this can never disagree with what that
    textual match already confirmed the expression's shape to be.
    """
    return frozenset(
        node.id for node in ast.walk(expr)
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load))


def _has_dominating_trusted_binding(name: str, fn: ast.FunctionDef,
                                    parents: dict) -> bool:
    """Is ``name`` bound, in ``fn``, EXACTLY once -- at a plain, single-
    target, TOP-LEVEL (directly in ``fn.body``, so it unconditionally runs
    before anything else in the function, on every path through it)
    ``name = <the reviewed source expression>`` assignment, with no OTHER
    binding of ``name`` anywhere else in the function to rebind, shadow,
    or race it?

    #202 repair round 10 (external review): the provenance question
    :func:`_captured_arg_safe_callee` alone could not answer -- see that
    function's own updated docstring for the exploit this closes
    (``api = evil_api`` before a call the OLD, spelling-only check still
    trusted). Deliberately the narrow, purely SYNTACTIC discipline
    :func:`_is_self_call`/:func:`_is_self_path` already established for
    "is this name really what it claims to be" (no CFG, no alias analysis
    -- just "does the SOURCE TEXT prove it"), extended to a name ``self``
    never needs it for (Python's own calling convention already guarantees
    ``self`` is bound exactly once, as the first parameter, before the
    function body runs at all -- an ordinary local like ``api`` gets no
    such guarantee for free, so THIS function is what supplies one):

    * exactly ONE :func:`_name_rebinding_sites` hit -- zero means ``name``
      is never bound in this function at all (an opaque module global this
      module has no basis to trust); more than one means it is rebound,
      shadowed by a parameter, or bound in more than one place, in EITHER
      order (a later ``api = evil_api`` after a genuine assignment, or a
      genuine assignment after an earlier throwaway one) -- the reviewer's
      own "rebinding before/after a valid facade assignment" coverage;
    * that one site must be a plain ``ast.Name`` in Store context, not one
      of the non-Name forms :func:`_name_rebinding_sites` also watches for
      (a parameter, an ``except``-handler, ...) -- the "parameter
      shadowing" case, where the ONLY binding is not an assignment at all;
    * its immediate parent (:func:`_build_parent_map`, the SAME map
      :meth:`_DispatchWalker._audit_function` already builds once per
      function for every other waiver/parent lookup) must be an
      ``ast.Assign`` with THIS site as its one and only target -- rules
      out chained assignment (``api = other = STATE.api``, two targets)
      and tuple/list unpacking (``api, x = STATE.api, 5`` -- the target's
      immediate parent is the ``ast.Tuple``, not the ``ast.Assign``, so
      this test already excludes it with no separate case needed) as
      AMBIGUOUS rather than trying to reason about them;
    * that ``ast.Assign`` must be a DIRECT statement of ``fn.body`` --
      never nested inside an ``if``/``try``/``with``/loop/nested closure
      -- which is what makes it DOMINATING: a statement directly in the
      function's own top-level body runs unconditionally, before any
      nested block, on every path through the function, with no CFG
      needed to prove it;
    * and its value, compared by EXACT ``ast.unparse()`` text (the SAME
      discipline ``_DO_HEAD_SAFE_SHAPE``/``_DO_OPTIONS_SAFE_SHAPE`` already
      use for "the literal reviewed shape, not merely something shaped
      like it"), must match
      ``_CAPTURED_ARG_SAFE_CALLEE_TRUSTED_SOURCES[name]`` exactly;
    * #202 repair round 11 (this round's own independent verify track):
      matching the trusted TEXT is not the end of the proof either --
      every free variable REFERENCED inside that text (``STATE``, for the
      one entry this dict has today) must ALSO be provably unshadowed in
      ``fn``: zero :func:`_name_rebinding_sites` hits, the SAME "no
      rebinding site, no parameter of that name" bar this function already
      holds ``name`` itself to, applied one level up the expression via
      :func:`_trusted_source_free_roots`. Round 10 closed "who binds
      ``api``"; a same-spelled parameter (with a default) or a preceding
      local reassignment of ``STATE`` would otherwise inherit the
      exemption exactly as a shadowed ``api`` did before round 10 -- the
      identical spelling-not-provenance bug, recurring one level up the
      same expression it trusts.

    Every real server.py site this exemption exists for -- ``_dispatch_get``,
    ``do_POST``, ``_handle_reassign``, ``_handle_reassign_v2``,
    ``_handle_setup``, ``_handle_setup_v2`` -- opens with exactly
    ``api = STATE.api`` as its own early statement, directly in the method
    body, never reassigned again in that method, and ``STATE`` itself is
    NEVER bound as a parameter or local anywhere in server.py (confirmed by
    direct AST inspection, the same methodology ``CapturedArgumentProvenanceTests``
    already applies to the real file) -- so this predicate is not a NEW
    restriction on real code, only on code that does not actually have the
    property the OLD check merely assumed from the name's spelling.
    """
    trusted_source = _CAPTURED_ARG_SAFE_CALLEE_TRUSTED_SOURCES.get(name)
    if trusted_source is None:
        return False
    sites = _name_rebinding_sites(name, fn)
    if len(sites) != 1:
        return False
    site = sites[0]
    if not (isinstance(site, ast.Name) and isinstance(site.ctx, ast.Store)):
        return False
    stmt = parents.get(id(site))
    if not (isinstance(stmt, ast.Assign) and len(stmt.targets) == 1
            and stmt.targets[0] is site and stmt in fn.body):
        return False
    if ast.unparse(stmt.value) != trusted_source:
        return False
    return all(not _name_rebinding_sites(root, fn)
               for root in _trusted_source_free_roots(stmt.value)
               if root != name)


def _captured_arg_trusted_roots(fn: ast.FunctionDef, parents: dict) -> frozenset:
    """Which names in :data:`_CAPTURED_ARG_SAFE_CALLEE_ROOTS` does THIS
    function's own body actually, provably earn the exemption for -- round
    10's replacement for trusting the bare allowlist directly (see
    :func:`_captured_arg_safe_callee`'s own updated docstring). Computed
    ONCE per audited function by :meth:`_DispatchWalker._audit_function`,
    the SAME "one linear pass, not per node" discipline
    :func:`_build_parent_map`'s own call site there already established,
    and threaded into :func:`_propagates_taint` (as ``trusted_roots``)
    exactly the way ``captured``/``waiver_hits``/``followed`` already are.
    """
    return frozenset(name for name in _CAPTURED_ARG_SAFE_CALLEE_ROOTS
                     if _has_dominating_trusted_binding(name, fn, parents))


def _captured_arg_safe_callee(node, trusted_roots: frozenset = frozenset()) -> bool:
    """Does ``node`` -- a Call's own ``.func``, or the container a
    Subscript/``.get()`` indexes -- resolve to a call target the
    ``captured``-only exemption (see :func:`_propagates_taint`'s own
    docstring) may trust with a captured value as plain data, rather than
    an unmodelled receiver that might invoke, store, or forward it?

    #202 repair round 9 (external review) -- the THIRD recurrence of "the
    captured-value exemption is broader than it should be" (round 6:
    direct-compare shapes; round 8: transparent tuple/list/IfExp
    composition; this round: higher-order ARGUMENT transfer). Every prior
    round narrowed the exemption by teaching :func:`_is_callee` one more
    shape a captured selector's own call could sit inside on its way to
    being invoked -- all of them SYNTACTIC, single-statement, "where does
    this node sit relative to a Call it's inside" questions. The
    reviewer's own round-9 repro defeats that whole strategy at once,
    demonstrated over real HTTP (200 for one concrete value, 404 for
    another) while extraction stayed silent:

        return invoke(handlers.get(action, default_handler), self)

    with ``invoke = lambda fn, h: fn.serve(h)`` (the reviewer's own
    independent reproduction used ``operator.call(handlers.get(action,
    default_handler).serve, self)`` instead -- the SAME shape, a stdlib
    higher-order callable rather than a hand-written one). ``handlers.get(
    action, default_handler)`` sits in ``invoke``'s ARGUMENT list -- never
    inside ANY call's own ``.func`` subtree in this statement -- so
    :func:`_is_callee` correctly, and PERMANENTLY, answers False for it,
    the same as it does for the genuinely inert ``api.get_item(gid)``.
    :func:`_is_callee` cannot tell these two apart because the difference
    between them is not syntactic (where the captured value sits in THIS
    expression) but SEMANTIC (what the receiving function DOES with it),
    and ``invoke``'s own body -- the only place that answer lives -- is a
    different function entirely, possibly not even defined in this file.
    Chasing one more transfer shape (a keyword argument, ``operator.
    call``, a callback stashed via ``setattr`` or a container and invoked
    later) would only rename the same unbounded problem: ANY unmodelled
    callee could invoke, store, or forward whatever it is handed, and
    this module cannot run the callee's own body to find out -- exactly
    the review's own diagnosis: "shape-by-shape closure is not
    converging".

    So this function does not add another shape. It inverts the DEFAULT
    instead: rather than trusting any non-``self.`` call unless proven
    otherwise (the OLD rule this round removes), a captured value handed
    to a call is inert ONLY when the call target is on the small,
    explicit, reviewed allowlist above
    (``_CAPTURED_ARG_SAFE_CALLEE_ROOTS``) -- currently just the API
    facade, ``api``. ``invoke``, ``operator.call``, ``setattr``, and
    ``list.append`` are all, correctly, NOT on it, so a captured selector
    handed to any of them now falls through to the SAME unlisted-call
    raise (or a dedicated, individually reviewed ``_AUDIT_WAIVERS``
    entry) any other unmodelled call already gets -- see
    ``CapturedArgumentCalleeAllowlistTests`` (test_route_extract.py) for
    all five named repros (invoke positional, invoke keyword, operator.
    call, a callback stashed via ``setattr``, one stashed via
    ``list.append``), each proven BOTH as a live-HTTP 200/404 divergence
    (the underlying shape really is exploitable Python, independent of
    this module) and as a static ``ExtractionError`` raise against the
    fixed code.

    #202 repair round 10 (external review): membership in
    ``_CAPTURED_ARG_SAFE_CALLEE_ROOTS`` -- a bare SPELLING match -- is no
    longer sufficient either. DEMONSTRATED, with the round-9 fix above
    otherwise untouched:

        api = evil_api
        return api.invoke({"hidden": api.hidden, "other": api.other}[action], self)

    resolves ``api.invoke``'s root to the ``ast.Name`` ``"api"`` exactly
    the same way the genuinely inert ``api.get_item(gid)`` does -- this
    function had no way to tell a LOCAL variable that merely HAPPENS to be
    spelled ``api`` apart from the one reviewed module-level facade the
    allowlist exists to trust, because it never asked what ``api``
    actually, provably IS at this point in the function. Live over real
    HTTP: ``/api/hidden`` answers 200, ``/api/other`` answers 404, while
    static extraction stayed silent -- the SAME "static and live examine
    identical code" proof every prior round's finding in this function
    required. ``root.id in trusted_roots`` -- the caller's OWN, per-
    function-computed :func:`_captured_arg_trusted_roots` result, NEVER
    the bare module-level allowlist directly -- closes it: ``trusted_
    roots`` contains ``"api"`` for a GIVEN function only when
    :func:`_has_dominating_trusted_binding` has independently proven that
    function's own ``api`` is bound, once, dominating, unrebound, to
    exactly ``STATE.api`` -- see that function's own docstring for the
    full discipline, and ``CapturedArgumentProvenanceTests``
    (test_route_extract.py) for the regression coverage: the reviewer's
    own repro (static AND live), parameter shadowing, rebinding before/
    after a valid assignment (both orders), the nested-dict-literal
    selector reproduced above, genuine ``STATE.api``-bound facade calls as
    negative controls, and a load-bearing mutation that degrades this
    check back to spelling-only and reproduces the escape.

    Resolves a plain ``ast.Attribute`` chain to its ROOT name the same
    way :func:`_is_self_call` resolves a ``self.`` chain (any depth --
    ``api.x``, ``self.api.x`` would resolve to ``self`` first and never
    reach this function at all, since the caller only ever invokes this
    for a call :func:`_is_self_call` has already ruled out). A plain
    ``ast.Dict`` LITERAL gets its own rule: ``{...}[kind]`` or ``{...}.
    get(kind, default)`` can only ever select one of the VALUES written
    right there in the source -- the real ``_handle_setup``/
    ``_handle_setup_v2`` delete-dispatch tables and ``do_POST``'s
    substitute-action table are exactly this shape, each a dict literal
    of ``api.X`` references -- so a dict literal is safe iff EVERY one of
    its values independently is, checked recursively AGAINST THE SAME
    ``trusted_roots`` (a dict of dicts is not a real server.py shape
    today, but costs nothing extra to handle correctly rather than assume
    away). Anything else -- a bare ``Name`` like ``handlers``/``invoke``/
    ``operator`` (an opaque external variable or function this module
    cannot see the contents or body of), a further Call or Subscript, ...
    -- is NOT safe: there is no way to confirm what it resolves to, or
    does with what it is handed, without running the program.
    """
    if isinstance(node, ast.Attribute):
        root = node
        while isinstance(root, (ast.Attribute, ast.Subscript)):
            root = root.value
        return isinstance(root, ast.Name) and root.id in trusted_roots
    if isinstance(node, ast.Dict):
        return all(_captured_arg_safe_callee(v, trusted_roots)
                   for v in node.values)
    return False


def _is_self_call(node: ast.Call) -> bool:
    """Is ``node``'s callee rooted at ``self`` -- ``self.method(...)``,
    ``self.attr.method(...)``, or any deeper ``self.a.b.c(...)`` chain?

    #202 repair round 5, finding 2b: :func:`_propagates_taint`'s ``captured``
    exemption (see its own docstring) must NEVER fire for a call this
    codebase's OWN class defines or owns, since an arbitrary ``self.``
    method -- however deep the attribute chain reaching it -- is exactly
    where a hidden dispatcher could live. DEMONSTRATED: checking only the
    single-level ``self.method(...)`` shape (``func.value`` a bare
    ``Name("self")``) missed ``self._V1_SETUP_KIND.get(entity, entity)``
    (``func.value`` is ITSELF an ``Attribute`` -- ``self._V1_SETUP_KIND``,
    not a bare ``self``) -- silently exempting it and driving an existing,
    reviewed waiver dormant (0 hits) instead of raising into it. Walks
    through every ``ast.Attribute``/``ast.Subscript`` layer (the same
    receiver-chain unwrapping :func:`_direct_operand_names`'s own
    ``root_name`` already does) to find the chain's ultimate root.
    """
    root = node.func
    while isinstance(root, (ast.Attribute, ast.Subscript)):
        root = root.value
    return isinstance(root, ast.Name) and root.id == "self"


def _is_opaque_extraction(node) -> bool:
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
        if _is_known_capture_extraction(node):
            return True
        if node.func.attr in _PATH_CONSUMING_METHODS:
            return True
        return False
    return isinstance(node, ast.Attribute) and node.attr in _PATH_PROPERTIES


def _is_self_path(node) -> bool:
    """Is ``node`` exactly the ``self.path`` attribute access?

    #202 repair round 6, finding 1: the ONE place this three-part shape
    (``ast.Attribute`` named ``path``, off an ``ast.Name`` literally
    ``self``) is matched -- :func:`_direct_operand_names`'s ``root_name``,
    :func:`_mentions_tracked` and :func:`_tracked_mentions` all call this
    rather than re-testing the same shape independently, which is exactly
    what let it drift out of sync before this finding: ``root_name``
    already recognised ``self.path`` when it is the WHOLE operand directly
    (``if self.path == "/x":``), but neither mention helper recognised it
    NESTED inside some other expression (``str(self.path)``,
    ``f(self.path, other)``) -- both of them only ever matched a bare
    ``ast.Name``, which ``self.path`` (an ``ast.Attribute``) never is,
    however deep it sits. DEMONSTRATED: ``candidate =
    str(self.path).split("?", 1)[0]`` then ``if candidate ==
    "/api/hidden":`` answered live HTTP 200 while extraction stayed
    silent -- ``str(...)`` is an unlisted call whose only tracked mention
    was ``self.path``, invisible to ``_mentions_tracked`` before this fix.
    """
    return (isinstance(node, ast.Attribute) and node.attr == "path"
            and isinstance(node.value, ast.Name) and node.value.id == "self")


def _mentions_tracked(node, tracked: set) -> bool:
    """Does ``node`` reference a tracked name anywhere in its subtree,
    treating a nested :func:`_is_opaque_extraction` node as opaque?

    A plain ``ast.walk`` would find ``cal`` inside ``cal.group(1)`` (or
    ``target`` inside ``target.suffix``) even when that expression is
    itself just an ARGUMENT to some unrelated, unlisted call
    (``api.calendar_feed_ics(cal.group(1))``,
    ``CONTENT_TYPES.get(target.suffix, ...)``) -- leaking the tracked
    receiver's trackedness into an enclosing call this check is really
    about. Stopping at an extraction's own boundary is what keeps those
    legitimate shapes (see ``_propagates_taint``'s docstring) from tripping
    the fail-closed check below; the extracted VALUE's own further use is
    already covered by this module's dedicated machinery elsewhere (the
    walker's group-tracking for a capture, the completeness audit's own
    waivers for a Path property compared directly), not this coarse check.

    #202 repair round 6, finding 1: checks :func:`_is_self_path` FIRST, so
    ``self.path`` reads as tracked (whenever "path" itself is -- always
    true for the completeness audit's own ``tracked``, which unconditionally
    seeds "path" in) no matter how deeply it is nested, not only when it
    happens to be the bare operand :func:`_direct_operand_names` already
    special-cased.
    """
    if _is_self_path(node):
        return "path" in tracked
    if isinstance(node, ast.Name):
        return node.id in tracked
    if _is_opaque_extraction(node):
        return False
    return any(_mentions_tracked(child, tracked)
               for child in ast.iter_child_nodes(node))


def _tracked_mentions(node, tracked: set) -> set:
    """Every tracked name reachable in ``node``'s subtree, stopping at an
    opaque-extraction boundary the SAME way :func:`_mentions_tracked` does
    (a captured group or Path property/consuming call stays opaque, so
    ``self._official_guard(oav.group(1))``'s argument still does not
    surface ``oav`` here even though this is the name-COLLECTING
    counterpart of that bool-returning check, not a second, independent
    rule).

    #202 repair round 3, finding E: :func:`_direct_operand_names`'s
    ``root_name`` needs to know not just "does this Call's argument/keyword
    mention a tracked name" (:func:`_mentions_tracked` already answers
    that) but WHICH name, so the caller's ``found`` set stays a set of
    real dispatch-subject names -- reuses :func:`_is_opaque_extraction`,
    the SAME boundary, rather than a second, independently-drifting rule.

    #202 repair round 6, finding 1: also checks :func:`_is_self_path`
    first, the SAME nested-``self.path`` recognition
    :func:`_mentions_tracked` gained this round, for the SAME reason --
    see that function's own docstring.
    """
    if _is_self_path(node):
        return {"path"} if "path" in tracked else set()
    if isinstance(node, ast.Name):
        return {node.id} if node.id in tracked else set()
    if _is_opaque_extraction(node):
        return set()
    found = set()
    for child in ast.iter_child_nodes(node):
        found |= _tracked_mentions(child, tracked)
    return found


#: Every AST node shape :func:`_binding_value_and_targets` recognises as a
#: NAME-BINDING EVENT, for the docstring below and for
#: ``test_route_extract.py`` to assert against directly rather than
#: hardcoding the list a second time.
BINDING_NODE_TYPES = (ast.Assign, ast.AnnAssign, ast.NamedExpr, ast.AugAssign,
                      ast.For, ast.AsyncFor, ast.comprehension, ast.withitem)


def _binding_value_and_targets(node):
    """``(value_expr, [target_nodes])`` for any node that binds a name FROM
    an expression, or ``None`` for anything else.

    #202 repair round 4, finding 2: :meth:`_DispatchWalker._audit_function`'s
    fixed-point taint loop used to hardcode exactly two shapes --
    ``ast.Assign``/``ast.AnnAssign`` and (#202 repair round 3, finding F)
    ``ast.NamedExpr`` -- so ANY other binding form silently carried the path
    into a new local with NOTHING added to ``tracked``: DEMONSTRATED for
    ``for candidate in (path,):``, ``with holder(path) as candidate:`` and
    ``candidate += path`` alike -- each produced zero exception and zero
    recorded route for a literal branch keyed on the bound name. All four
    (plus the pre-existing three) share the SAME shape once named
    generically -- "a target receives a value FROM an expression" -- so one
    extraction function, consulted by the SAME fixed-point loop, is what
    lets a For/AsyncFor/comprehension/with/aug-assign target join ``tracked``
    exactly the way an ordinary ``n = EXPR`` already does, rather than
    re-deriving the propagation logic per binding form.

    ``targets`` is always a LIST, even for the single-target forms, so the
    caller's existing ``ast.walk(target)`` tuple/starred/list unpacking logic
    (already needed for a plain ``a, b = ...`` assignment) covers a
    ``for a, *rest in pairs:`` or ``with ctx() as (a, b):`` target for free,
    with no separate unpacking rule needed per binding form.

    Deliberately narrow, matching every other extraction function in this
    module: a ``with`` item with no ``as`` clause (``optional_vars is
    None``) binds nothing and is skipped, not raised on -- there is no
    target for taint to reach. Binding forms this function does not list
    (an ``except ... as name:`` handler, a function parameter default) stay
    outside this module's tracked-set model entirely, the same documented,
    accepted boundary the module's own KNOWN LIMITATIONS section already
    draws for lexical scoping in general -- not a shape this round's finding
    named, and not one #202's own dispatch-branch shapes use.
    """
    if isinstance(node, ast.Assign):
        return (node.value, list(node.targets)) if node.value is not None else None
    if isinstance(node, ast.AnnAssign):
        return (node.value, [node.target]) if node.value is not None else None
    if isinstance(node, (ast.NamedExpr, ast.AugAssign)):
        return (node.value, [node.target])
    if isinstance(node, (ast.For, ast.AsyncFor, ast.comprehension)):
        # A `for`/comprehension TARGET is bound from each ELEMENT of
        # ``iter``, not from ``iter`` itself -- but this module's taint
        # question is only ever "does the bound name still carry the
        # path", and ``_propagates_taint``'s own fallback (any tracked
        # Name anywhere in the expression's subtree) already answers that
        # conservatively for a container literal like ``(path,)`` without
        # needing to model *which* element ends up in the target on a
        # given iteration -- the same over-approximation this module
        # prefers everywhere (fail closed / re-raise-and-review beats a
        # silent miss).
        return (node.iter, [node.target])
    if isinstance(node, ast.withitem) and node.optional_vars is not None:
        return (node.context_expr, [node.optional_vars])
    return None


def _propagates_taint(value, tracked: set, fn_name: str = "",
                      waiver_hits: Optional[dict] = None,
                      parents: Optional[dict] = None,
                      followed: Optional[set] = None,
                      captured: Optional[set] = None,
                      trusted_roots: frozenset = frozenset()) -> bool:
    """Does this expression still CARRY the request path?

    Deliberately narrow. `p2 = self.path.split("?", 1)[0]` carries it -- string
    surgery on the path is still the path. `ics = api.calendar_feed_ics(
    cal.group(1))` does NOT: a route capture handed to a service produces a
    RESULT, and testing that result (`if ics is None`) is a post-dispatch
    decision, not a route choice. `target = (STATIC_DIR / rel).resolve()`
    DOES carry it (#202 repair root cause 2): pathlib's `/` join and its own
    reshaping methods (`.resolve()`, `.parent`, ...) are the Path equivalent
    of string surgery, not a call into unrelated code -- `STATIC_DIR` is a
    fixed module constant, so the result is still a location derived from
    whichever operand is tainted, same as a stripped/split string is.

    Getting this wrong in the permissive direction reopens the hole the taint
    tracking exists to close. Getting it wrong the other way produced eleven
    spurious failures, which I initially waived -- waiving an analysis defect
    would have left eleven standing invitations to hide a route behind a service
    call.

    FAIL CLOSED on an UNLISTED call (#202 repair round 2, finding A): the
    loop below used to return False -- silently "not tainted" -- the instant
    ANY call not on the ``_PATH_OPS``/``_PATH_METHODS`` whitelist appeared
    ANYWHERE in the expression, with no regard for whether that call's own
    receiver or arguments still carried a tracked name. That is all-or-
    nothing over the WHOLE subtree, so it could not tell a value that is
    merely handed to an unrelated service (fine) apart from a routing
    decision hidden behind an unmodelled call -- DEMONSTRATED live escapes:
    `found = next((r for r in KNOWN if r == path), None)` then `if found:`;
    a `KNOWN.index(path)` bound inside a `try`/`except ValueError:` and
    tested as a sentinel; `getattr(self, "_handle_" + suffix, None)` used to
    dispatch indirectly. All three bind a LOCAL that still decides the
    route, through a call this module does not model -- and none of them
    joined `tracked`, so the `if` that actually chose the route was never
    even looked at by the completeness scan below. Whether the call
    "manipulates" (the whitelist), is provably UNRELATED to any tracked
    name (see `_mentions_tracked`, which treats a nested capture-group or
    Path-property/consuming extraction as opaque so `cal.group(1)` handed
    to a service still does NOT trip this), or is a reviewed
    ``_AUDIT_WAIVERS`` entry -- see the next paragraph for what a waiver
    means now -- are the only ways this function does not simply raise on
    an unlisted call; anything else raises rather than guessing.

    A WAIVER SILENCES THE CALL, NOT THE RESULT (#202 repair round 5,
    finding 1). A waiver answers exactly one question -- "is THIS call
    EXPRESSION, at THIS reviewed position, allowed to appear without
    raising" -- never the SEPARATE question "does the value this call
    RETURNS still carry taint if it is assigned to a name". Those two used
    to be conflated: hitting a waived call inside the loop below used to
    ``return False`` for the WHOLE expression immediately, the same
    "definitely untainted" verdict as a call provably unrelated to any
    tracked name -- so `perm = required_permission(path)` (waived, because
    the CALL is a reviewed blanket-permission lookup, not a route
    selector) left `perm` OUT of `tracked` entirely, and a LATER
    `if perm == Permission.MANAGE_SCHEDULE:` reached neither this function
    nor the completeness scan that consults it: a live route, zero
    routes recorded, zero exception -- DEMONSTRATED, both over real HTTP
    and via `extract_routes`, in ``tests/test_route_extract.py``'s
    ``WaiverTaintPropagationTests``. The fix: a waived call's own node
    now falls through (``continue``, not ``return False``) to the SAME
    walk/fallback that decides every other expression's taint, exactly as
    if the call were not there to examine at all -- so `perm` joins
    `tracked` through the ordinary fixed-point mechanism precisely because
    `required_permission(path)`'s own argument (`path`) is still plainly
    present in the expression, the same as it would be for ANY OTHER
    call's argument. A subsequent routing-relevant use of that name then
    raises through the SAME existing machinery as any other tracked name,
    unless IT separately earns its own waiver at ITS OWN position (round
    4 finding 3's parent-shape/enclosing-if fingerprint already makes a
    relocated or newly-reached use of the same name a FRESH key, so this
    costs no new machinery). A waived call whose result truly never
    escapes into a routing decision -- e.g. `perm.value` only ever
    interpolated into a 403 message string -- stays clean under this
    change for the mundane reason every other untested tracked name
    does: nothing ever tests it in a shape the completeness scan
    recognises, so nothing raises. This is NOT a relaxation of the
    waiver mechanism itself: the call site still raises, unwaived, the
    exact same way it always did; only what happens to its RESULT
    changed, from "silently forgotten" to "tracked like everything else".

    ``followed`` (#202 repair round 5, finding 2b) is the walker's own
    ``self._followed`` -- ``id()`` of every Call this run already resolved
    as a KNOWN delegation (``TAIL_DELEGATES``/``SAME_PATH_DELEGATES``/
    ``PARSED_DELEGATES``, recognised by :meth:`_DispatchWalker.
    _maybe_delegate` during the WALK, independently walked with its own
    tracked set) and CONSUMED as such, rather than an unlisted call this
    function should reason about. Needed once a bare ``return
    self._serve_static(path)`` -- ordinary, common, and ALREADY correctly
    followed -- is reached by :meth:`_DispatchWalker._audit_function`'s new
    Return scan: without this, ``_serve_static`` (in ``SAME_PATH_DELEGATES``
    but NOT ``_handle_*``/``_dispatch_*``-prefixed, so
    :meth:`_audit_dispatch_helper_calls` never looks at it either) would be
    treated as an unlisted call mentioning the tracked `path` and raise on
    its own, already-reviewed delegation. Skipped the SAME way a
    ``manipulates`` call is -- ``continue``, not a verdict on the whole
    expression -- so a followed delegate nested inside something larger
    still lets the REST of that expression be examined normally.

    ``captured`` (#202 repair round 5, finding 2b) is the audited function's
    own ``set(ctx.captured)`` -- names EVER bound as a REGEX-CAPTURE
    dispatch subject ANYWHERE in this function's walk (:meth:`_Ctx.
    bind_subject`: a direct ``gid = m.group(1)``, or a TAIL_DELEGATES/
    PARSED_DELEGATES parameter carrying one across a delegation boundary --
    see ``_Ctx.captured``'s own docstring for why this must be a DEDICATED,
    function-wide-SHARED field and not ``ctx.subjects`` itself, which goes
    out of scope the moment the ``if <the regex match>:`` block that
    introduced the name closes), deliberately narrower than ``tracked``
    itself -- excludes ``path``, and excludes ``combo``/``dest``/
    ``parent``-style names bound via :meth:`_Ctx.bind_tuple`/
    ``bind_tuple_dict``/``bind_tuple_lookup``, whose own comparison
    genuinely IS a routing input, unlike a plain captured id. Reaching
    Return.value with the SAME
    "unlisted call raises" rule the fixed-point loop and bare-Expr scan
    already use surfaced a real, LOUD pattern in server.py: dozens of
    ``return self._send_api(api.get_x(gid, ...))``-shaped statements,
    where a captured id was bound to a local FIRST (``gid = m.group(1)``)
    rather than inlined (``api.get_x(m.group(1))``) -- semantically the
    SAME "a route capture handed to a service produces a RESULT" case
    ``_is_known_capture_extraction`` already exempts for the INLINE form,
    just not recognised for the bound-first one, since a bare ``ast.Name``
    was never treated as an extraction boundary the way ``X.group(N)`` is.
    Reviewing each of these individually would be 40+ near-identical
    "waivers" recording the SAME judgement, not 40+ distinct reviews --
    exactly the "if a fix needs that, the design is wrong" shape a
    case-by-case waiver is meant to avoid. The narrower, PRINCIPLED fix: a
    NON-``self.`` call (``api.X``, or a local bound from one via a dict
    lookup, e.g. ``fn = {"accept": api.accept_substitute, ...}[op];
    fn(gid, ...)`` -- never a routing table, always the API FACADE this
    codebase's own layering (CLAUDE.md) guarantees performs no HTTP
    routing) whose ONLY tracked mentions are already-CAPTURED names is
    exempt, the SAME way an inline capture already is. Deliberately NOT
    extended to ``self.`` calls: an arbitrary, uncatalogued ``self.``
    method is exactly where a HIDDEN dispatcher (the reviewer's own
    ``self._route(path)``) would live in this class, so those stay under
    FULL scrutiny regardless of whether their arguments are capture-only --
    each real one still needs (and, below, has) its own reviewed waiver. A
    comparison/dispatch TEST on a captured name is UNAFFECTED by this --
    ``_direct_operand_names``/``root_name`` (the If/IfExp/While/match-case
    completeness scan) uses ``_tracked_mentions``, a separate function this
    change does not touch, so ``if self._is_hidden(gid):`` still raises
    exactly as finding 1 (round 4) already made it.

    #202 repair round 9 (external review): "a NON-``self.`` call" is no
    longer sufficient on its own -- narrowed further to "a NON-``self.``
    call whose own callee ALSO resolves to the small, explicit, reviewed
    allowlist :func:`_captured_arg_safe_callee` checks" (both the Call
    branch below and the parallel Subscript branch). Round 6's own
    narrowing (``_is_callee``: never when the call is itself about to be
    invoked) answers a SYNTACTIC question -- where does the captured value
    sit in THIS statement -- that a captured selector handed to an
    arbitrary function AS AN ARGUMENT, and invoked from INSIDE that
    function's own body, simply does not trip: ``invoke(handlers.get(
    action, default_handler), self)`` (the reviewer's own repro,
    ``invoke = lambda fn, h: fn.serve(h)``) never places the selector in
    any call's own ``.func`` subtree, so ``_is_callee`` correctly answers
    False, and -- before this round -- nothing else asked whether
    ``invoke`` itself was a call this module has any basis to trust.
    DEMONSTRATED live (200/404 divergence, extraction silent) for that
    repro, its keyword-argument form, the reviewer's own
    ``operator.call(...)`` variant, and two independently invented
    transfer shapes -- a callback stashed via ``setattr`` and one via
    ``list.append`` -- each invoked in a later statement. Fixing this by
    teaching :func:`_is_callee` about ``invoke`` specifically would only
    be a fourth curated shape in a series that has not converged (round
    6, round 7, round 8 each closed exactly what they targeted and each
    time review found a materially different composition); INSTEAD of
    that, or of attempting general data-flow analysis to trace what an
    arbitrary function does with its own arguments, this round flips the
    default: a captured value handed to a call is inert ONLY when the
    call target is on the allowlist, not whenever it merely fails to be
    ``self.`` -- see :func:`_captured_arg_safe_callee`'s own docstring for
    the full reasoning and :class:`CapturedArgumentCalleeAllowlistTests`
    (test_route_extract.py) for the regression coverage.

    ``trusted_roots`` (#202 repair round 10, external review) is the
    audited function's own :func:`_captured_arg_trusted_roots` result --
    which :data:`_CAPTURED_ARG_SAFE_CALLEE_ROOTS` names THIS function's
    own body proves are bound, dominating and unrebound, to the exact
    source expression each is allowlisted for. Round 9's allowlist
    trusted a NAME's spelling alone, so a same-spelled LOCAL --
    ``api = evil_api`` -- inherited the SAME trust as the one reviewed
    facade; see :func:`_captured_arg_safe_callee`'s own updated docstring
    for the exploit this closes. Threaded through to both
    :func:`_captured_arg_safe_callee` call sites below exactly the way
    ``captured`` already is; defaults to the empty set, so a caller that
    does not compute it (:meth:`_DispatchWalker._else_rederives_subject`,
    which also never passes ``captured`` and so never reaches either call
    site at all) gets the same fail-closed behaviour as passing no
    ``captured`` set already does.
    """
    for node in ast.walk(value):
        if isinstance(node, ast.Call):
            if followed is not None and id(node) in followed:
                continue
            if _is_opaque_extraction(node):
                continue
            func = node.func
            is_self_call = _is_self_call(node)
            manipulates = (isinstance(func, ast.Attribute)
                           and func.attr in (_PATH_OPS + _PATH_METHODS)
                           and _propagates_taint(func.value, tracked, fn_name,
                                                 waiver_hits, parents,
                                                 followed, captured,
                                                 trusted_roots))
            if manipulates:
                continue
            if _mentions_tracked(node, tracked):
                # #202 repair round 5, finding 2b: BOTH exemptions below are
                # gated on _mentions_tracked already having fired (i.e. on
                # this call being one that WOULD otherwise need a waiver or
                # raise) -- deliberately NOT evaluated any earlier. Checking
                # either one BEFORE this point -- reachable even when
                # ``_mentions_tracked`` is False, e.g. `CONTENT_TYPES.get(
                # target.suffix, ...)` (`target.suffix` is itself an opaque
                # Path-property extraction _mentions_tracked already looks
                # straight through) -- would swap this function's ORIGINAL
                # `return False` (a HARD, immediate "not tainted" verdict)
                # for a `continue` that lets the loop fall through to the
                # cruder bottom-of-function fallback below, which does NOT
                # respect any opaque-extraction boundary (`any(isinstance(x,
                # ast.Name) and x.id in tracked for x in ast.walk(value))`
                # walks blindly) -- DEMONSTRATED: exactly this reordering
                # made `ctype = CONTENT_TYPES.get(target.suffix, ...)`
                # wrongly derive True (`target` itself is tracked; the
                # fallback finds it INSIDE `target.suffix` where
                # `_mentions_tracked` correctly does not), producing a
                # brand new, spurious raise on `_serve_static`'s
                # `self.send_header('Content-Type', ctype)` two lines later
                # -- a real regression this exact placement fixes.
                if is_self_call and func.attr in _TERMINAL_RESPONSE_SENDERS:
                    # See _TERMINAL_RESPONSE_SENDERS' own comment: a call
                    # that only WRITES the HTTP response is never a
                    # routing decision. Whatever is nested in its
                    # arguments is still reached and examined on its own,
                    # separately, by this SAME walk.
                    continue
                if (captured and not is_self_call
                        and not _is_callee(node, parents)
                        and _tracked_mentions(node, tracked) <= captured
                        and _captured_arg_safe_callee(func, trusted_roots)):
                    # See this function's own docstring: a NON-self call
                    # whose only tracked mentions are already-captured
                    # names is the bound-first counterpart of
                    # ``_is_known_capture_extraction``'s inline exemption.
                    # #202 repair round 6, finding 2: ALSO never when this
                    # call is itself about to be INVOKED as an enclosing
                    # call's own callee (see ``_is_callee``'s own
                    # docstring) -- selecting WHICH callable runs is a
                    # dispatch decision even when the selector is a
                    # captured id, not the inert-data case this exemption
                    # exists for.
                    # #202 repair round 9, finding 1 (external review):
                    # ALSO never when this call's own callee (``func``) is
                    # not on the small, explicit, reviewed allowlist (see
                    # ``_captured_arg_safe_callee``'s own docstring) --
                    # "not a captured/receiver position" (round 6) is
                    # necessary but no longer sufficient, because a
                    # captured selector handed to an ARBITRARY unmodelled
                    # function as a plain argument can be invoked from
                    # INSIDE that function's own body, never appearing in
                    # any call's own ``.func`` subtree in THIS statement
                    # at all (``invoke(handlers.get(action,
                    # default_handler), self)``, DEMONSTRATED live 200/404
                    # while every prior round's checks stayed silent).
                    continue
                waiver_key = _waiver_key(fn_name, node, parents)
                if waiver_key in _AUDIT_WAIVERS:
                    # Reviewed and declared not-a-routing-decision for THIS
                    # CALL SITE (see the waiver's own entry) -- #202 repair
                    # round 5, finding 1: this no longer also erases the
                    # RESULT's taint. Record the hit (exact-one-hit
                    # verification, unchanged) and fall through to the
                    # SAME walk/fallback logic below that decides every
                    # other node's taint, exactly as if this Call were
                    # simply absent -- see this function's own docstring,
                    # "A WAIVER SILENCES THE CALL, NOT THE RESULT", for why.
                    if waiver_hits is not None:
                        # #202 repair round 2, finding D: record the exact
                        # AST node this waiver matched, so a completed run
                        # can verify every declared waiver was consulted
                        # EXACTLY ONCE (see _DispatchWalker.verify_waiver_usage).
                        waiver_hits.setdefault(waiver_key, set()).add(id(node))
                    continue
                raise ExtractionError(
                    f"line {node.lineno}: `{ast.unparse(node)}` is an "
                    "unlisted call whose receiver or argument(s) include a "
                    "tracked dispatch name; route_extract cannot tell "
                    "whether the result still decides the route. Classify "
                    "it here (extend the whitelist, or model the shape "
                    "explicitly) -- do not let it be silently treated as "
                    "detached from the path.")
            # #202 repair round 6, finding 1: NOT ``return False`` any more.
            # A Call found here that does not itself mention a tracked name
            # is provably unrelated ON ITS OWN -- but ``value`` may be a
            # LARGER expression with an entirely independent SIBLING that
            # DOES (``candidate = path or fallback()``: ``fallback()``
            # mentions nothing tracked, ``path`` sits right next to it).
            # The bug this fixes was a SHORT-CIRCUIT, not a missing-shape
            # gap: this loop walks EVERY node in ``value``'s subtree
            # looking for a Call to classify, but the old ``return False``
            # fired the INSTANT the FIRST such Call turned out unrelated,
            # discarding whatever the REST of the walk might still find --
            # "lets any unrelated nested Call clear taint for the whole
            # expression", in the reviewer's own words. DEMONSTRATED:
            # exactly the ``path or fallback()`` shape above answered live
            # HTTP 200 while extraction stayed silent. Simply falling
            # through here (nothing left to do for THIS node) lets the walk
            # keep auditing every other node, and the bottom-of-function
            # fallback below -- itself now compositional, see its own
            # comment -- is what actually decides the boolean once the
            # whole subtree has been audited, exactly the same way the
            # Subscript case immediately below already has to (a bare
            # Subscript never reaches this ``ast.Call`` branch at all).
        if isinstance(node, ast.Subscript):
            # #202 repair round 6, finding 1: a Subscript is audited the
            # SAME way a Call is -- ``RESPONSES[path]``/``ERRORS[path]``
            # are dict lookups KEYED on a tracked name, exactly as
            # dispatch-relevant as ``TABLE.get(path)`` or a Subscript-
            # callee (``TABLE[path]()``, already caught -- as a CALL -- by
            # the branch above, and independently by ``root_name``'s own
            # matching fix for the if-test side of this finding) -- but
            # nothing here inspected a BARE Subscript with no Call
            # anywhere around it at all. DEMONSTRATED, both live HTTP 200
            # and silent ``extract_routes() == []``: ``return
            # self._send_json(RESPONSES[path], 200)`` and ``raise
            # ERRORS[path]``. The ``_TERMINAL_RESPONSE_SENDERS`` exemption
            # above is correct on its own terms (WRITING the response is
            # never itself a routing decision) and its own comment already
            # claims "whatever is nested in its arguments is still
            # reached... by this SAME walk" -- true for a nested CALL
            # (independently visited and audited), never true for a bare
            # Subscript before this branch existed. Only the SLICE (the
            # KEY) is examined: a receiver being tracked (``combo[0]``) is
            # a DIFFERENT, already-handled question -- see the bottom-of-
            # function fallback, which still sees the whole Subscript.
            mentioned = _tracked_mentions(node.slice, tracked)
            if mentioned:
                if captured and mentioned <= captured \
                        and not _is_callee(node, parents) \
                        and _captured_arg_safe_callee(node.value, trusted_roots):
                    # The SAME captured-only exemption a Call's arguments
                    # get, several lines above (see this function's own
                    # docstring on ``captured``): a lookup keyed on an
                    # already-CAPTURED id (``CACHE[gid]``) is the subscript
                    # form of the identical "produces a RESULT, not a
                    # routing decision" shape ``api.get_x(gid)`` already
                    # is -- never the primary ``path`` itself, and never a
                    # ``combo``/``dest``/``parent``-style tuple, both
                    # unconditionally excluded from ``captured`` upstream
                    # (see ``_Ctx.captured``'s own docstring), so this
                    # cannot be used to smuggle a real dispatch table past
                    # review. #202 repair round 6, finding 2: ALSO never
                    # when this Subscript is itself about to be INVOKED as
                    # an enclosing call's own callee (``HANDLERS[action]
                    # ()``, see ``_is_callee``'s own docstring) -- the
                    # Subscript-callee analogue of the SAME dispatch-
                    # selection concern the Call-branch's own exemption,
                    # above, is narrowed for. #202 repair round 9, finding
                    # 1 (external review): ALSO never when the CONTAINER
                    # (``node.value``) is not on the same small, explicit,
                    # reviewed allowlist the Call-branch now requires (see
                    # ``_captured_arg_safe_callee``'s own docstring) -- an
                    # opaque external dict (``HANDLERS[action]`` used as a
                    # plain argument, never itself a callee) is no more
                    # provably inert than an opaque external function is;
                    # only a dict LITERAL of already-safe values (the real
                    # ``_handle_setup``/``_handle_setup_v2``/``do_POST``
                    # delete- and action-dispatch tables, each spelled
                    # inline right here) is.
                    continue
                waiver_key = _waiver_key(fn_name, node, parents)
                if waiver_key in _AUDIT_WAIVERS:
                    if waiver_hits is not None:
                        waiver_hits.setdefault(waiver_key, set()).add(id(node))
                    continue
                raise ExtractionError(
                    f"line {node.lineno}: `{ast.unparse(node)}` indexes a "
                    "container by a tracked dispatch name; route_extract "
                    "cannot tell whether the looked-up value still decides "
                    "the route. Classify it here (the same way an unlisted "
                    "call is classified) -- do not let it be silently "
                    "treated as detached from the path.")
    if isinstance(value, ast.Call) and _is_opaque_extraction(value):
        return False                  # an extraction/consumption, alone, is not the path
    if _is_path_derived(value):
        return True
    if isinstance(value, ast.BinOp) and isinstance(value.op, ast.Div):
        # pathlib's own join operator: `STATIC_DIR / rel` is a Path built
        # from whichever operand carries the path -- the construction
        # analogue of the method calls handled above.
        return (_propagates_taint(value.left, tracked, fn_name, waiver_hits,
                                  parents, followed, captured, trusted_roots)
                or _propagates_taint(value.right, tracked, fn_name,
                                     waiver_hits, parents, followed, captured,
                                     trusted_roots))
    if isinstance(value, ast.Attribute) and value.attr in _PATH_PROPERTIES:
        return _propagates_taint(value.value, tracked, fn_name, waiver_hits,
                                 parents, followed, captured, trusted_roots)
    # #202 repair round 6, finding 1: ``_mentions_tracked`` -- which
    # recognises ``self.path`` at any depth (see its own docstring) and
    # stops at an opaque-extraction boundary -- not a blind ``ast.walk``
    # Name-scan. The blind scan (`any(isinstance(x, ast.Name) and x.id in
    # tracked for x in ast.walk(value))`) does not honour that boundary at
    # all, so the MOMENT this fallback was reached for an expression
    # merely CONTAINING an opaque extraction -- not equal to one, see the
    # check immediately above -- it wrongly found the extraction's own
    # RECEIVER "tracked" underneath it (round 5 finding 2b's own
    # regression, documented at the ``_TERMINAL_RESPONSE_SENDERS``/
    # ``captured`` exemptions above, was exactly this bug reached a
    # different way: gate those exemptions too early and THIS SAME crude
    # fallback is what they fall through to). ``_mentions_tracked`` is the
    # SAME, already-correct boundary this function's own opaque/manipulates
    # checks above rely on, reused here rather than a second, cruder rule
    # for the cases that fall all the way through to it.
    return _mentions_tracked(value, tracked)


def _is_path_derived(node) -> bool:
    """Does this expression read the request path, however indirectly?

    ``self.path``, ``self.path.split("?", 1)[0]``, ``p2`` bound from either —
    all of it. Renaming the local was a DEMONSTRATED evasion: `p2 = self.path...`
    then `if p2 == "/api/evade-rename"` produced a live 200 while the gate
    stayed green, because the audit only ever tracked the literal name "path".

    #202 repair round 6, finding 1: reuses :func:`_is_self_path`, the SAME
    ``self.path`` shape-test :func:`_mentions_tracked`/:func:`_tracked_mentions`
    and ``root_name`` (:func:`_direct_operand_names`) now also call, instead
    of independently re-testing the same three-part shape a fourth time.
    """
    return any(_is_self_path(sub) for sub in ast.walk(node))


def _direct_operand_names(test, tracked: frozenset = frozenset({"path"})) -> set:
    """Names this test DECIDES ON, as opposed to names it merely passes along.

    ``if path.startswith("/api/")`` and ``if m.group(2) == "board"`` decide on
    ``path``/``m``; ``if self._official_guard(oav.group(1))`` does not decide on
    ``oav`` — it hands a captured id to a guard, and the guard's answer is about
    permissions, not about which route this is. So arguments of a call are NOT
    operand positions, while a call's own receiver is.

    UNLESS the call is itself the tested condition -- the LEFT or a comparator
    of an ``ast.Compare`` (``len(path) == N``, #202 repair round 3, finding E),
    OR the call IS the test (directly, or reached through any nesting of
    ``not``/``and``/``or`` around it: ``if self._is_hidden(path):``, ``if not
    authorize(role, path):``, ``if guard_a(path) and guard_b():`` -- #202
    repair round 4, finding 1) -- in which case an argument that still
    carries a tracked name undisguised by an opaque extraction DOES count
    (see the ``root_name`` Call branch below). A call reached NEITHER way --
    as an argument to some OTHER call, or as a captured-group/Path-property
    extraction's own receiver -- still does not count: ``_official_guard(
    oav.group(1))``'s ``oav`` stays invisible here regardless (opaque
    extraction, see :func:`_tracked_mentions`), and nothing in this module
    ever looks at what a call's return value is subsequently passed to.

    Finding 1 closes what finding E deliberately left open: before it, a
    BARE call used directly as (or boolop/not-ed into) the whole test was a
    STRUCTURAL exemption -- its arguments were never even inspected, not
    just judged harmless -- so a NEW, unreviewed helper predicate consuming
    the path (``if self._is_hidden(path):``) was silently invisible, not
    merely un-waived. DEMONSTRATED: a real localhost Handler answers HTTP
    200 for a path such a predicate hides, while ``extract_routes`` records
    zero routes and raises nothing. The fix removes the structural
    exemption entirely -- a Call's arguments are now scanned wherever
    ``root_name`` reaches the call, regardless of ``ast.Compare`` -- so
    every one of THIS round's genuinely reviewed blanket guards
    (``_operator_only``, ``authorize``, the two other guard shapes named in
    the ``_AUDIT_WAIVERS`` comments below) now needs, and has, an explicit
    waiver instead of relying on this structural gap; an unreviewed
    predicate has no such entry and raises. This does NOT re-litigate round
    2's design choice that a blanket per-request authorisation gate is not
    itself a routing decision (see those waivers' own comments) -- it only
    changes HOW that conclusion is recorded: a declared, fingerprinted,
    one-hit-verified waiver (:meth:`_DispatchWalker.verify_waiver_usage`)
    rather than an implicit shape this function silently never looked at.

    ``tracked`` defaults to just ``{"path"}`` for callers outside the
    completeness audit's own ``ctx``-aware fixed point (e.g.
    :meth:`_DispatchWalker._require_safe_verb_shape`, which only ever cared
    about the literal name "path" even before this parameter existed);
    :meth:`_DispatchWalker._audit_function` passes its own, fuller
    ``tracked`` set explicitly.
    """
    found = set()

    def root_name(node):
        while True:
            # DIRECT ``self.path`` (#202 repair, invented-evasion track):
            # bypassing the local entirely (``if self.path == "/x":``, never
            # binding a ``path`` local at all) must still resolve to "path" --
            # not "self" -- or it is invisible to the completeness audit
            # (which seeds "path" into ``tracked`` unconditionally, but never
            # "self"). DEMONSTRATED: this exact form produced a live route
            # neither classified nor raised before this check existed.
            # #202 repair round 6, finding 1: reuses :func:`_is_self_path`,
            # the SAME shape-test :func:`_mentions_tracked`/
            # :func:`_tracked_mentions` now also call, rather than
            # independently re-testing it a third time.
            if _is_self_path(node):
                return "path"
            # A WALRUS operand (#202 repair round 3, finding F):
            # ``(n := EXPR) == "foo"`` decides on ``n``'s newly bound value.
            # Resolving to the bind's own TARGET -- always a bare Name;
            # Python's grammar allows no other shape for a walrus target --
            # is what lets this be recognised once the fixed-point loop in
            # _audit_function has (or has not) proven EXPR itself
            # path-derived and joined ``n`` to ``tracked`` accordingly, the
            # SAME two-part contract an ordinary ``n = EXPR`` assignment
            # already gets. DEMONSTRATED: neither this nor the fixed-point
            # loop recognised ``ast.NamedExpr`` before this fix, so a
            # walrus-bound routing decision raised nothing and recorded
            # nothing, whether tested in the SAME if-test or a later one.
            # Unconditional: no shape reached by root_name (walrus, the Call
            # case below, ...) gets a structural pass any more (#202 repair
            # round 4, finding 1 removed the last one -- see below).
            if isinstance(node, ast.NamedExpr):
                return node.target.id
            if isinstance(node, ast.Name):
                return node.id
            if isinstance(node, ast.Subscript):
                # #202 repair round 6, finding 1: the SLICE (the KEY) is a
                # routing-relevant operand in its own right whenever it
                # mentions a tracked name -- ``FLAGS[path]`` used directly
                # as (or boolop/not-ed into) the whole test decides on
                # ``path`` exactly as plainly as ``FLAGS.get(path)`` would
                # -- but the unwrap below (``node = node.value``, needed to
                # resolve a RECEIVER chain like ``SOME_DICT[key].attr``
                # toward its own root name) used to discard ``.slice``
                # UNCONDITIONALLY. Round 5, finding 2a already special-
                # cased this for a Subscript reached as a CALL's OWN
                # callee (``PREDICATES[path]()``) with a one-off scan
                # inside the ``ast.Call`` branch below; this generalises
                # that fix to EVERY Subscript this loop ever unwraps
                # through (this branch runs again for that SAME
                # ``PREDICATES[path]`` node once ``node = node.func`` below
                # loops back to the top, so the one-off scan there is now
                # redundant and has been removed rather than left to drift
                # out of sync with this more general rule). DEMONSTRATED:
                # ``if FLAGS[path]: ...`` answered live HTTP 200 while
                # extraction recorded zero routes and raised nothing.
                found.update(_tracked_mentions(node.slice, tracked))
                node = node.value
            elif isinstance(node, ast.Attribute):
                node = node.value
            elif isinstance(node, ast.Call):
                # A Call's ARGUMENTS -- UNCONDITIONALLY (#202 repair round 4,
                # finding 1; round 3, finding E introduced this scan but
                # gated it to ``in_compare`` -- a comparison operand only).
                # Every node ``root_name`` ever examines is already reached
                # from ``visit_operand`` as part of THE TESTED CONDITION
                # ITSELF (the whole test, or and/or/not-ed into it, or a
                # comparison operand of it -- see this function's own
                # docstring) -- there is no OTHER way into this loop -- so
                # there is no remaining shape where scanning a Call's
                # arguments here would reach past what the test actually
                # decides on. Before this round, a BARE call used directly
                # as the whole test (no ``ast.Compare`` anywhere) left this
                # branch entirely unscanned -- not merely judged harmless --
                # so a NEW, unreviewed helper predicate consuming the path
                # (``if self._is_hidden(path):``) was structurally
                # invisible: zero exception, zero route, a real HTTP 200.
                # Fail closed the SAME WAY _propagates_taint does for an
                # unlisted call's receiver/arguments: reuse
                # _tracked_mentions (the name-collecting counterpart of
                # _mentions_tracked, same opaque-extraction boundary) rather
                # than duplicate that logic here. This only ADDS names to
                # ``found`` -- it never suppresses the callee-chain
                # resolution below, so ``path.startswith(...)`` still
                # resolves to "path" exactly as before either way.
                for arg in list(node.args) + [kw.value for kw in node.keywords]:
                    found.update(_tracked_mentions(arg, tracked))
                # A Subscript used AS THE CALLEE (``PREDICATES[path]()``,
                # ``ROUTES[path]()``, round 5 finding 2a's own repro) needs
                # no special case here any more (#202 repair round 6,
                # finding 1): ``node = node.func`` below sends this SAME
                # Subscript node back through the TOP of this loop on the
                # next iteration, where the general ``ast.Subscript``
                # branch above now scans its ``.slice`` unconditionally --
                # a one-off scan here would just re-derive the identical
                # answer a second time.
                node = node.func
            else:
                # #202 repair round 5, finding 5: DEFAULT-DENY for every
                # remaining expression shape, the SAME "fail closed on an
                # unlisted call" pattern round 2 finding A already applies
                # in `_propagates_taint` (see that function's own
                # docstring), extended here to the general operand-
                # resolution walk. Every branch above this one is an
                # explicit, REVIEWED pass-through -- self.path, a walrus
                # target, a bare Name, an Attribute/Subscript RECEIVER
                # chain, a Call's callee/arguments -- each recognised
                # because it is either the tracked name itself or a chain
                # that plainly still carries it. Anything else -- a BinOp
                # (string concatenation: `path + ""`), a JoinedStr
                # (an f-string: `f"{path}"`), an IfExp reached as an
                # OPERAND rather than the whole test (`(path if True else
                # "")`), or any future node type this module has not been
                # taught -- used to fall through here silently, returning
                # None with NO further inspection. DEMONSTRATED (the
                # reviewer's own three same-source forms): each of
                # `if path + "" == "/api/hidden":`, `if f"{path}" ==
                # "/api/hidden":`, and `if (path if True else "") ==
                # "/api/hidden":` answered live HTTP 200 while extraction
                # stayed silent -- the comparison operand was reached
                # here, resolved to nothing, and the tracked name inside
                # it was simply never looked at. Reuses `_tracked_mentions`
                # (the SAME name-collecting function, SAME opaque-
                # extraction boundary, the Call-argument branch above
                # already relies on) rather than a new, parallel rule --
                # so a genuinely detached value (a captured group nested
                # inside an otherwise-unrecognised wrapper) still does not
                # trip this, exactly as it would not for a Call argument.
                # This only ADDS to `found`; an unrecognised node that
                # truly mentions nothing tracked is silently, correctly
                # ignored, same as before.
                found.update(_tracked_mentions(node, tracked))
                return None

    def visit_operand(node):
        if isinstance(node, ast.BoolOp):
            for value in node.values:
                visit_operand(value)
            return
        if isinstance(node, ast.UnaryOp):
            visit_operand(node.operand)
            return
        if isinstance(node, ast.Compare):
            visit_operand(node.left)
            for comparator in node.comparators:
                visit_operand(comparator)
            return
        # any(...)/all(...) over a generator or list/set comprehension (#202
        # repair, invented-evasion track): the comprehension's own element
        # expression (and any ``if`` clauses on its generators) can itself
        # test a tracked subject -- ``any(path == p for p in candidates)`` --
        # and DEMONSTRATED to be invisible otherwise: root_name(Call) only
        # ever resolves to "any"/"all" itself, never looking at the argument.
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) \
                and node.func.id in ("any", "all") and len(node.args) == 1 \
                and isinstance(node.args[0],
                               (ast.GeneratorExp, ast.ListComp, ast.SetComp)):
            comp = node.args[0]
            visit_operand(comp.elt)
            for generator in comp.generators:
                for cond in generator.ifs:
                    visit_operand(cond)
            return
        name = root_name(node)
        if name is not None:
            found.add(name)

    visit_operand(test)
    return found


ENTRY_POINTS = {"GET": "do_GET", "POST": "do_POST"}


def extract_routes(source: Optional[str] = None,
                   entry_points: Optional[dict] = None) -> list:
    """Every live dispatch branch in ``server.py`` (or in ``source``)."""
    text = source if source is not None else SERVER_PATH.read_text()
    walker = _DispatchWalker(ast.parse(text))
    routes = walker.run(entry_points or ENTRY_POINTS)
    if source is None:
        # #202 repair round 2, finding D: fingerprint-verify _AUDIT_WAIVERS
        # against the REAL server.py specifically -- gated on `source is
        # None` (the "give me the real file" convention every call site
        # already uses) rather than unconditional, because a SYNTHETIC test
        # fixture legitimately consults none of server.py's own waivers;
        # enforcing this for every such fixture would fail all of them, not
        # just a genuinely orphaned or over-broad waiver.
        walker.verify_waiver_usage()
    return routes


def extract_walker(source: Optional[str] = None,
                   entry_points: Optional[dict] = None) -> _DispatchWalker:
    """As :func:`extract_routes`, but returns the walker (counts, findings)."""
    text = source if source is not None else SERVER_PATH.read_text()
    walker = _DispatchWalker(ast.parse(text))
    walker.run(entry_points or ENTRY_POINTS)
    if source is None:
        walker.verify_waiver_usage()  # see extract_routes' own comment
    return walker


def _main() -> int:  # pragma: no cover - developer CLI
    import argparse

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--shapes", action="store_true",
                        help="print the per-shape branch counts")
    args = parser.parse_args()
    walker = extract_walker()
    routes = sorted(walker.routes.values(), key=lambda r: (r.method, r.template))
    for route in routes:
        print(f"{route.method:5s} {route.template:65s} "
              f"{route.handler}:{route.lineno} [{route.shape}]")
    print(f"\n{len(routes)} live routes "
          f"({sum(1 for r in routes if r.method == 'GET')} GET, "
          f"{sum(1 for r in routes if r.method == 'POST')} POST)")
    if args.shapes:
        for shape, count in sorted(walker.shape_counts.items()):
            print(f"  {shape:18s} {count} branches")
    if walker.unreachable:
        print("\nUNREACHABLE nested branches:")
        for handler, lineno, test in walker.unreachable:
            print(f"  {handler}:{lineno}  {test}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_main())
