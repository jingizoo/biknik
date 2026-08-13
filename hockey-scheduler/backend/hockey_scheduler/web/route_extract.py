"""Derive the LIVE route set from ``web/server.py`` by PARSING it (#202 step 1).

Why this exists
---------------
``server.py`` dispatches with hand-written ``if`` chains, and three separate
hand-maintained tables (``_GET_ROUTES``, ``_POST_ROUTES``,
``CONTEXT_SCOPED_READ_ROUTES``) transcribe parts of that dispatch. A registry
checked against another hand-written list proves nothing: both are prose, both
drift, and they drift silently the moment someone adds a branch. So the
inventory's counterpart is the DISPATCH ITSELF, read out of the source with
``ast``.

What "live route" means here
----------------------------
One entry per ``if`` branch in the dispatch that SELECTS ON THE REQUEST PATH,
expressed as a canonical TEMPLATE:

    ``/api/games/{}/board``      ``{}``  = one path segment (``[^/]+``/``\\w+``)
    ``/api/setup/{*}``           ``{*}`` = a free tail (``.+`` / a prefix route)

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
===============================  ==================================================

Fail LOUD, never quiet
----------------------
The point of a derived inventory is that it cannot silently miss a branch, so
every unknown shape raises :class:`ExtractionError` instead of being skipped:

* an unsupported regex construct in a dispatch pattern;
* an ``if`` test that touches a path-bearing name (the request path, a delegated
  tail, a captured group, or a ``re.match`` result) in any way this module does
  not recognise — see :meth:`_DispatchWalker._audit_function`;
* a call into a ``_handle_*``/``_dispatch_*`` method that is neither walked nor
  listed as a terminal.

A missed branch would show up as a route with no ``RouteSpec``; a shape this
module cannot read shows up as a hard error. Both fail CI; neither passes
quietly.

Stdlib only (CLAUDE.md): ``ast``, ``re``, ``dataclasses``.
"""
from __future__ import annotations

import ast
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

SERVER_PATH = Path(__file__).with_name("server.py")

#: One path segment (``[^/]+`` or ``\w+``) in a canonical template.
SEG = "{}"
#: A free tail (``.+``, or everything after a prefix route) in a template.
TAIL = "{*}"


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

    ``kind`` is ``lit`` (literal text), ``seg`` (one path segment) or ``tail``
    (a free tail). ``group`` is the 1-based capture-group number this piece came
    from, or ``None`` when it is outside every capture group.
    """

    kind: str
    text: str = ""
    group: Optional[int] = None

    def render(self) -> str:
        if self.kind == "lit":
            return self.text
        return SEG if self.kind == "seg" else TAIL


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
                return ("seg",)
            if nxt in ".-/":
                self.i += 2
                return ("lit", nxt)
            raise ExtractionError(
                f"unsupported escape \\{nxt} in {self.src!r}")
        if ch == ".":
            # ``.+`` (a non-empty tail) and ``.*`` (a possibly-empty one, which
            # is how a startswith() prefix route is written as a regex) are both
            # a free tail as far as the canonical template is concerned.
            if self.src[self.i + 1:self.i + 2] in ("+", "*"):
                self.i += 2
                return ("tail",)
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
    if kind == "seg":
        return [Expansion((Part("seg"),))]
    if kind == "tail":
        return [Expansion((Part("tail"),))]
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


def sample_path(template: str, token: str = "sample") -> str:
    """A concrete path matching ``template`` (placeholders -> a token).

    Used by the cross-checks: it turns a template or a table pattern into
    something both sides can be matched against.
    """
    out, n = [], 0
    rest = template
    while rest:
        if rest.startswith(TAIL):
            n += 1
            out.append(f"{token}{n}")
            rest = rest[len(TAIL):]
        elif rest.startswith(SEG):
            n += 1
            out.append(f"{token}{n}")
            rest = rest[len(SEG):]
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
    #: Every path-bearing name bound ANYWHERE in this function walk, including
    #: inside nested branches. Shared (not copied) with children so the
    #: completeness audit below sees names a child ctx introduced.
    seen: set = field(default_factory=set)

    def child(self) -> "_Ctx":
        return _Ctx(self.method, self.handler, dict(self.subjects),
                    dict(self.matches), dict(self.dicts), self.seen)

    def bind_subject(self, name: str, alts) -> None:
        self.subjects[name] = tuple(alts)
        self.seen.add(name)

    def bind_match(self, name: str, info) -> None:
        self.matches[name] = info
        self.seen.add(name)

    def bind_dict(self, name: str, info) -> None:
        self.dicts[name] = info
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
#: The route shape is fully decided by the caller's own pattern, so there is
#: nothing further to read here; listed so the audit below can tell a known
#: terminal from a delegation someone added later.
PARSED_DELEGATES = {"_handle_reassign", "_handle_reassign_v2"}


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

    def _audit_unwalked_verbs(self, walked: set):
        """No OTHER ``do_*`` verb may grow a dispatch of its own unnoticed.

        Today only GET and POST have one: HEAD re-runs ``do_GET``, and
        PUT/PATCH/DELETE/OPTIONS answer from ``_supported_methods`` without
        selecting a route. A verb handler that started matching paths while the
        extractor still read two entry points would leave a whole method's
        routes out of the inventory in silence.
        """
        for name, fn in sorted(self.functions.items()):
            if not name.startswith("do_") or name in walked:
                continue
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
        for stmt in body:
            self._walk_stmt(stmt, ctx)

    def _walk_stmt(self, stmt, ctx: _Ctx):
        if isinstance(stmt, ast.Assign):
            self._record_binding(stmt, ctx)
        elif isinstance(stmt, ast.If):
            self._walk_if(stmt, ctx)
        elif isinstance(stmt, (ast.Try, ast.With, ast.For, ast.While)):
            for attr in ("body", "orelse", "finalbody", "handlers"):
                for sub in getattr(stmt, attr, []) or []:
                    if isinstance(sub, ast.ExceptHandler):
                        self._walk_body(sub.body, ctx)
                    else:
                        self._walk_stmt(sub, ctx)
        elif isinstance(stmt, ast.Return) and stmt.value is not None:
            self._maybe_delegate(stmt.value, ctx)
        elif isinstance(stmt, ast.Expr):
            self._maybe_delegate(stmt.value, ctx)

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
            grp = self._as_group_call(value, ctx)
            if grp is not None:
                ctx.bind_subject(target.id, grp)
            return
        # a, b = m.group(1), m.group(2)
        if isinstance(target, ast.Tuple) and isinstance(value, ast.Tuple):
            for name_node, val in zip(target.elts, value.elts):
                if not isinstance(name_node, ast.Name):
                    continue
                grp = self._as_group_call(val, ctx)
                if grp is not None:
                    ctx.bind_subject(name_node.id, grp)

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
    def _walk_if(self, node: ast.If, ctx: _Ctx):
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
        if outcome.shape == "prefix":
            # A prefix branch either DELEGATES the tail to a handler this module
            # walks — in which case the callee's branches carry the routes — or
            # it is itself a real prefix route (``/api/{*}``).
            if self._body_delegates_tail(node.body, outcome.subject):
                templates = ()
            else:
                templates = tuple(a.prefix + outcome.literal + TAIL
                                  if a.is_free else a.fixed_template
                                  for a in (outcome.alts or ()))
        if outcome.shape not in ("present-group", "prefix") and not templates:
            # Nothing this branch tests for can reach it: the subject is already
            # constrained to shapes the test excludes. Dead dispatch code.
            self.unreachable.append(
                (ctx.handler, node.lineno, ast.unparse(node.test)))
        for template in sorted(set(templates)):
            self._emit(ctx, template, outcome.shape, node.lineno, node.test)
        child = ctx.child()
        if outcome.subject is not None and outcome.alts is not None \
                and outcome.shape != "prefix":
            child.bind_subject(outcome.subject, outcome.alts)
        self._walk_body(node.body, child)
        self._walk_body(node.orelse, ctx)

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

    def _classify(self, test, ctx: _Ctx) -> Optional[_Outcome]:
        # if <matchvar>:  (the regex was assigned on a previous line)
        if isinstance(test, ast.Name) and test.id in ctx.matches:
            return self._regex_outcome(ctx.matches[test.id], ctx, test)
        # if <dictvar>:   ({...}.get(subject))
        if isinstance(test, ast.Name) and test.id in ctx.dicts:
            subject, keys = ctx.dicts[test.id]
            alts = self._select(ctx, subject, keys)
            return _Outcome("dict-key", subject, self._templates(alts), alts)
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
                derived = _propagates_taint(value, tracked)
                if not derived:
                    continue
                targets = node.targets if isinstance(node, ast.Assign) \
                    else [node.target]
                for target in targets:
                    for leaf in ast.walk(target):
                        if isinstance(leaf, ast.Name) and leaf.id not in tracked:
                            tracked.add(leaf.id)
                            changed = True
        for node in ast.walk(fn):
            if isinstance(node, ast.If) and id(node) not in self._classified:
                names = _direct_operand_names(node.test)
                hit = names & tracked
                if hit and (fn.name, ast.unparse(node.test)) in _AUDIT_WAIVERS:
                    continue
                if hit:
                    raise ExtractionError(
                        f"{fn.name}:{node.lineno} tests dispatch subject(s) "
                        f"{sorted(hit)} in an unrecognised shape: "
                        f"{ast.unparse(node.test)}")
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
                if name in (TAIL_DELEGATES | SAME_PATH_DELEGATES) \
                        and id(node) not in self._followed:
                    # A delegation in a statement form the walker does not
                    # follow (an assignment, a comprehension, ...). Its callee's
                    # routes would simply be absent — the one failure this
                    # module must never have.
                    raise ExtractionError(
                        f"{fn.name}:{node.lineno} delegates to self.{name}() "
                        "in a form the walker does not follow")


# Branches that DECIDE ON a path-derived name but are not routing decisions.
#
# Fail-closed is the rule: an unrecognised test on a dispatch subject stops the
# build. These are the declared exceptions — each one reviewed, each one a
# visible line in the diff. Adding a waiver is deliberately as conspicuous as
# adding a route, because a waiver is how the gate would be quietly defeated.
#
# Keyed by (function name, the exact unparsed test). A drifted test no longer
# matches its waiver and raises again, which is the intended behaviour.
_AUDIT_WAIVERS = {
    ("_serve_static",
     "STATIC_DIR not in target.parents or not target.is_file()"):
        "filesystem containment on the already-resolved static target -- it "
        "decides whether to SERVE, not which route was chosen",
    ("_serve_static", "target.suffix == '.html'"):
        "content-type selection for the already-resolved static target",
}


_PATH_OPS = ("split", "rsplit", "strip", "lstrip", "rstrip", "lower", "upper",
             "partition", "rpartition", "removeprefix", "removesuffix",
             "replace", "format", "join", "casefold")


def _propagates_taint(value, tracked: set) -> bool:
    """Does this expression still CARRY the request path?

    Deliberately narrow. `p2 = self.path.split("?", 1)[0]` carries it -- string
    surgery on the path is still the path. `ics = api.calendar_feed_ics(
    cal.group(1))` does NOT: a route capture handed to a service produces a
    RESULT, and testing that result (`if ics is None`) is a post-dispatch
    decision, not a route choice.

    Getting this wrong in the permissive direction reopens the hole the taint
    tracking exists to close. Getting it wrong the other way produced eleven
    spurious failures, which I initially waived -- waiving an analysis defect
    would have left eleven standing invitations to hide a route behind a service
    call.
    """
    for node in ast.walk(value):
        if isinstance(node, ast.Call):
            func = node.func
            manipulates = (isinstance(func, ast.Attribute)
                           and func.attr in _PATH_OPS
                           and _propagates_taint(func.value, tracked))
            if not manipulates:
                return False          # a call result is not the path
    if _is_path_derived(value):
        return True
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
    return walker.run(entry_points or ENTRY_POINTS)


def extract_walker(source: Optional[str] = None,
                   entry_points: Optional[dict] = None) -> _DispatchWalker:
    """As :func:`extract_routes`, but returns the walker (counts, findings)."""
    text = source if source is not None else SERVER_PATH.read_text()
    walker = _DispatchWalker(ast.parse(text))
    walker.run(entry_points or ENTRY_POINTS)
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
