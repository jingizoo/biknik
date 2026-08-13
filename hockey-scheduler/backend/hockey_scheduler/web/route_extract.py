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

    def child(self) -> "_Ctx":
        return _Ctx(self.method, self.handler, dict(self.subjects),
                    dict(self.matches), dict(self.dicts), dict(self.tuples),
                    dict(self.tuple_dicts), dict(self.tuple_lookups),
                    dict(self.origins), self.seen)

    def bind_subject(self, name: str, alts) -> None:
        self.subjects[name] = tuple(alts)
        self.seen.add(name)

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
        for fn_name, expr in orphaned:
            lines.append(f"  DORMANT (0 hits): ({fn_name!r}, {expr!r})")
        for fn_name, expr in too_broad:
            hits = len(self.waiver_hits[(fn_name, expr)])
            lines.append(f"  TOO BROAD ({hits} distinct locations): "
                        f"({fn_name!r}, {expr!r})")
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
        for stmt in orelse:
            if isinstance(stmt, ast.Assign) \
                    and _propagates_taint(stmt.value, {subject}, fn_name,
                                          self.waiver_hits):
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
        # TAINT PROPAGATION. Any local bound from the path — directly, sliced,
        # or from another tainted local — joins the tracked set, so renaming it
        # cannot hide a branch. Iterated to a fixed point because one rename can
        # feed another.
        changed = True
        while changed:
            changed = False
            for node in ast.walk(fn):
                if not isinstance(node, (ast.Assign, ast.AnnAssign)):
                    continue
                value = node.value
                if value is None:
                    continue
                targets = node.targets if isinstance(node, ast.Assign) \
                    else [node.target]
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
                                            self.waiver_hits)
                if not derived:
                    continue
                for leaf in leaves:
                    if leaf.id not in tracked:
                        tracked.add(leaf.id)
                        changed = True
        for node in ast.walk(fn):
            if isinstance(node, ast.If) and id(node) not in self._classified:
                names = _direct_operand_names(node.test)
                hit = names & tracked
                key = (fn.name, ast.unparse(node.test))
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
                names = _direct_operand_names(node.test)
                hit = names & tracked
                key = (fn.name, ast.unparse(node.test))
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
                names = _direct_operand_names(node.test)
                hit = names & tracked
                key = (fn.name, ast.unparse(node.test))
                if hit and key in _AUDIT_WAIVERS:
                    self._record_waiver_hit(key, node)
                    continue
                if hit:
                    raise ExtractionError(
                        f"{fn.name}:{node.lineno} a while loop tests "
                        f"dispatch subject(s) {sorted(hit)} in an "
                        f"unrecognised shape: {ast.unparse(node.test)}")

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
# Keyed by (function name, the exact unparsed test OR call expression). A
# drifted test/expression no longer matches its waiver and raises again,
# which is the intended behaviour.
_AUDIT_WAIVERS = {
    ("_serve_static",
     "STATIC_DIR not in target.parents or not target.is_file()"):
        "filesystem containment on the already-resolved static target -- it "
        "decides whether to SERVE, not which route was chosen",
    ("_serve_static", "target.suffix == '.html'"):
        "content-type selection for the already-resolved static target",
    ("_handle_setup_v2", "mar.group(2) == 'archive'"):
        "a TERNARY (#202 repair, invented-evasion track) choosing which "
        "backend function to call -- api.archive_season vs "
        "api.reopen_season. NOT a routing decision: mar's own pattern "
        "(archive|reopen) already produces BOTH templates as separate "
        "regex-alternation leaves (see route_registry.py's "
        "post_v2_setup_seasons_id_archive/_reopen), so this ternary picks "
        "an implementation for a route already fully decided upstream",
    ("_handle_setup_v2", "kind == 'venue'"):
        "a TERNARY (#202 repair, invented-evasion track) choosing a "
        "response mapper (_v2p.venue_to_v2 vs identity). NOT a routing "
        "decision: kind comes from md's own entity alternation, which "
        "already produces one delete leaf PER entity (see "
        "route_registry.py's post_v2_setup_<entity>_id_delete specs); this "
        "ternary only reshapes the response for one of those already-"
        "enumerated leaves",
    ("_handle_reassign", "_REASSIGN_PARENTS.get(combo)"):
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
    ("_handle_reassign_v2", "_REASSIGN_PARENTS.get(combo)"):
        "same authorisation-parent lookup as _handle_reassign's own "
        "waiver above, reached from the v2 handler -- the route is already "
        "decided upstream by the v2 combo/schema dispatch; see that entry",
    ("_handle_reassign", "self._V1_SETUP_KIND.get(entity, entity)"):
        "#202 repair round 2 finding A -- a legacy-name-alias lookup "
        "(v1 'league' -> canonical 'program', identity for everything "
        "else) that only relabels the authorisation TARGET kind added to "
        "`targets`. NOT a routing decision: the route was already decided "
        "by the regex + combo/schema dispatch upstream; this reshapes an "
        "authorisation-check argument, the same 'produces a RESULT' shape "
        "as a captured group handed to a service",
    ("_handle_setup", "_to_v1.get(kind, lambda r: r)"):
        "#202 repair round 2 finding A -- selects the RESPONSE mapper "
        "(canonical entity -> its legacy v1 wire shape) for the delete "
        "route's already-decided entity kind (`kind = md.group(1)`, "
        "itself matched against a fixed literal alternation). NOT a "
        "routing decision: every kind in `md`'s pattern already yields "
        "the SAME `/api/setup/<entity>/{}/delete` leaf (see "
        "route_registry.py); this only reshapes the response body, the "
        "same 'produces a RESULT' shape as the two _handle_setup_v2 "
        "ternary waivers above",
    ("do_POST", "required_permission(path)"):
        "#202 repair round 2 finding A -- builds the human-readable "
        "permission name for a 403 error body, AFTER `authorize(role, "
        "path)` (the actual gate, tested directly and already exempt: "
        "`path` is an ARGUMENT there, not that call's operand per "
        "_direct_operand_names) has already refused the request. A "
        "blanket per-verb authorisation gate, not a route selector -- see "
        "test_a_guard_that_merely_passes_the_path_along_is_not_a_route "
        "for the same shape via `_operator_only`/`_supported_methods`",
    ("do_POST",
     "scope_violation(role, scope, path, body, api.store, "
     "allow_unscoped_dev_fallback=allow_dev_fallback)"):
        "#202 repair round 2 finding A -- resource-scoping authorisation "
        "(#51: a coach only their team, a player only self), run "
        "UNCONDITIONALLY for every POST before any path-based dispatch "
        "branch is reached. Same blanket-gate shape as `required_"
        "permission(path)` immediately above -- refuses access to an "
        "already-identified resource, does not select a route",
}


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
def _is_opaque_extraction(node) -> bool:
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
        if _is_known_capture_extraction(node):
            return True
        if node.func.attr in _PATH_CONSUMING_METHODS:
            return True
        return False
    return isinstance(node, ast.Attribute) and node.attr in _PATH_PROPERTIES


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
    """
    if isinstance(node, ast.Name):
        return node.id in tracked
    if _is_opaque_extraction(node):
        return False
    return any(_mentions_tracked(child, tracked)
               for child in ast.iter_child_nodes(node))


def _propagates_taint(value, tracked: set, fn_name: str = "",
                      waiver_hits: Optional[dict] = None) -> bool:
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
    to a service still does NOT trip this), or matches a reviewed
    ``_AUDIT_WAIVERS`` entry the SAME way an unrecognised ``if`` test does
    (e.g. ``_handle_reassign``'s own `_REASSIGN_PARENTS.get(combo)`: a
    module-level authorisation-parent lookup keyed on an already-tracked
    combo, consulted AFTER the route itself was decided by `combo in
    _V1_REASSIGN_SCHEMA` -- the same "produces a RESULT, not a routing
    decision" shape as the calendar feed, just not a MECHANICAL one a
    blanket rule can recognise, so it goes through the same declared,
    reviewed, one-hit-fingerprinted escape hatch as everything else that
    needs human judgement rather than a shape rule) are the only ways to
    end this function quietly now; anything else raises rather than
    guessing.
    """
    for node in ast.walk(value):
        if isinstance(node, ast.Call):
            if _is_opaque_extraction(node):
                continue
            func = node.func
            manipulates = (isinstance(func, ast.Attribute)
                           and func.attr in (_PATH_OPS + _PATH_METHODS)
                           and _propagates_taint(func.value, tracked, fn_name,
                                                 waiver_hits))
            if manipulates:
                continue
            if _mentions_tracked(node, tracked):
                waiver_key = (fn_name, ast.unparse(node))
                if waiver_key in _AUDIT_WAIVERS:
                    # Reviewed and declared not-a-routing-decision (see the
                    # waiver's own entry) -- like a provably-unrelated call,
                    # this must not propagate either, or the LHS would join
                    # `tracked` anyway via the fallback below and simply
                    # relocate the raise to whatever `if` tests it next
                    # (DEMONSTRATED: `_REASSIGN_PARENTS.get(combo)` waived
                    # here but left `parent` tracked moved this exact error
                    # onto `if parent is not None:`, unwaived, one line
                    # down).
                    if waiver_hits is not None:
                        # #202 repair round 2, finding D: record the exact
                        # AST node this waiver matched, so a completed run
                        # can verify every declared waiver was consulted
                        # EXACTLY ONCE (see _DispatchWalker.verify_waiver_usage).
                        waiver_hits.setdefault(waiver_key, set()).add(id(node))
                    return False
                raise ExtractionError(
                    f"line {node.lineno}: `{ast.unparse(node)}` is an "
                    "unlisted call whose receiver or argument(s) include a "
                    "tracked dispatch name; route_extract cannot tell "
                    "whether the result still decides the route. Classify "
                    "it here (extend the whitelist, or model the shape "
                    "explicitly) -- do not let it be silently treated as "
                    "detached from the path.")
            return False              # provably unrelated to any tracked name
    if isinstance(value, ast.Call) and _is_opaque_extraction(value):
        return False                  # an extraction/consumption, alone, is not the path
    if _is_path_derived(value):
        return True
    if isinstance(value, ast.BinOp) and isinstance(value.op, ast.Div):
        # pathlib's own join operator: `STATIC_DIR / rel` is a Path built
        # from whichever operand carries the path -- the construction
        # analogue of the method calls handled above.
        return (_propagates_taint(value.left, tracked, fn_name, waiver_hits)
                or _propagates_taint(value.right, tracked, fn_name, waiver_hits))
    if isinstance(value, ast.Attribute) and value.attr in _PATH_PROPERTIES:
        return _propagates_taint(value.value, tracked, fn_name, waiver_hits)
    return any(isinstance(x, ast.Name) and x.id in tracked
               for x in ast.walk(value))


def _is_path_derived(node) -> bool:
    """Does this expression read the request path, however indirectly?

    ``self.path``, ``self.path.split("?", 1)[0]``, ``p2`` bound from either —
    all of it. Renaming the local was a DEMONSTRATED evasion: `p2 = self.path...`
    then `if p2 == "/api/evade-rename"` produced a live 200 while the gate
    stayed green, because the audit only ever tracked the literal name "path".
    """
    for sub in ast.walk(node):
        if isinstance(sub, ast.Attribute) and sub.attr == "path" \
                and isinstance(sub.value, ast.Name) and sub.value.id == "self":
            return True
    return False


def _direct_operand_names(test) -> set:
    """Names this test DECIDES ON, as opposed to names it merely passes along.

    ``if path.startswith("/api/")`` and ``if m.group(2) == "board"`` decide on
    ``path``/``m``; ``if self._official_guard(oav.group(1))`` does not decide on
    ``oav`` — it hands a captured id to a guard, and the guard's answer is about
    permissions, not about which route this is. So arguments of a call are NOT
    operand positions, while a call's own receiver is.
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
            if isinstance(node, ast.Attribute) and node.attr == "path" \
                    and isinstance(node.value, ast.Name) \
                    and node.value.id == "self":
                return "path"
            if isinstance(node, ast.Name):
                return node.id
            if isinstance(node, (ast.Attribute, ast.Subscript)):
                node = node.value
            elif isinstance(node, ast.Call):
                node = node.func
            else:
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
