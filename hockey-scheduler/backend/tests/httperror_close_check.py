"""Derive, by PARSING the repository, every place that binds an
``urllib.error.HTTPError`` and check that it CLOSES it (#205, from #426).

Why this exists
---------------
#426's defect was one ``except urllib.error.HTTPError as e:`` handler that
read the response body and returned without closing it. The permanent
regression written for it (``test_resource_warning_leak.py``) proves the fix
by provoking a ``ResourceWarning`` -- and that is where this module comes in:

    unclosed HTTPError, body read, never closed   3.12/3.11: NO WARNING
                                                       3.14: WARNS
    unclosed HTTPError, never read, never closed  3.12/3.11: NO WARNING
                                                       3.14: WARNS
    unclosed raw socket                           3.12/3.11: WARNS
    unclosed raw file                             3.12/3.11: WARNS

measured directly, with ``-W error::ResourceWarning``, a real HTTP server and
``gc.collect()``, on this branch. On 3.14 ``urllib.response.addbase`` became a
``tempfile._TemporaryFileWrapper`` subclass, so an abandoned ``HTTPError`` --
which IS an ``addbase`` -- is reported by ``tempfile._TemporaryFileCloser.
__del__`` as "Implicitly cleaning up <HTTPError 403: 'Forbidden'>". On the
3.11 CI runs on (``.github/workflows/hockey-scheduler-ci.yml``,
``python-version: "3.11"``) an abandoned ``HTTPError`` produces NO
``ResourceWarning`` of any kind.

So the ResourceWarning half of the #426 contract is a NO-OP on the interpreter
that actually gates this repository: reverting the ``with e:`` fix is caught
locally on 3.14 and NOT caught in CI. (The psycopg half of that contract is
unaffected -- psycopg reports its own "connection was deleted while still
open" ``ResourceWarning`` on every interpreter, and the original CI failure
log shows it firing on 3.11.)

This module restores VERSION-INDEPENDENT protection for the HTTPError half by
reading the SOURCE instead of watching the garbage collector: an unclosed
handler is a syntactic property of the code, identical on every interpreter,
and visible without running anything.

The precedent followed here is ``hockey_scheduler/web/route_extract.py``:
stdlib ``ast`` only, one dataclass per finding, a shape this module does not
understand is RAISED rather than skipped (a silently ignored branch is exactly
the failure mode a derived inventory exists to remove), pre-existing exceptions
live in ONE conspicuous ledger, and that ledger is fingerprint-verified so an
entry cannot rot into a hole in the gate (``verify_waiver_usage``'s job there,
:func:`verify_ledger` here).

What counts as "binds an HTTPError"
-----------------------------------
* ``except urllib.error.HTTPError as e:`` and every spelling of that name the
  repository could use -- ``from urllib.error import HTTPError``, ``from
  urllib import error``, ``import urllib.error as ue``, and any ``as`` alias of
  those (resolved from the module's own imports, see :func:`_name_bindings`);
* a tuple handler that MENTIONS one, ``except (urllib.error.HTTPError, X) as
  e:``;
* ``except urllib.error.URLError as e:`` -- ``HTTPError`` IS a ``URLError``,
  so such a handler really can receive one -- UNLESS an EARLIER handler on the
  same ``try`` already catches ``HTTPError``, in which case it provably cannot.
  Included because it is the one realistic way to evade this check by
  rewriting rather than fixing.

``OSError``/``Exception``/``BaseException`` are supertypes too and are
deliberately NOT scanned: they catch overwhelmingly non-HTTP things, so
demanding ``.close()`` on their bound name would be meaningless at almost
every site and would drown the real signal. That is a stated limit of this
check, not an oversight -- see :data:`_SUPERTYPE_NAMES`.

What counts as "closes it"
--------------------------
Any of these, anywhere in the handler body:

* ``with e:`` / ``with e as r:`` (what #426 was fixed with -- ``HTTPError``
  supports the same context-manager protocol as the success path's response);
* ``e.close()`` -- including inside a ``try:``/``finally:``;
* ``with contextlib.closing(e):`` or any ``with`` item whose call takes ``e``;
* handing ``e`` to an ``ExitStack`` (``enter_context``/``push``/``callback``).

Anything else is a violation. The default is "violation", not "fine": this
check fails CLOSED, so an idiom it has not been taught is reported rather than
assumed safe.

The one exemption: RE-RAISING
-----------------------------
A handler that re-raises on every path does not own the response -- ownership
travels with the still-propagating exception, and closing there would shut the
body before whoever catches it next can read it. Such a handler is classified
``reraised`` and reported (not silently dropped). "On every path" is proved
structurally by :func:`_always_raises`, which is deliberately conservative:
constructs it cannot prove terminal (a loop, a ``with``, a ``match``) fall
through to "not exempt", i.e. to a violation. The repository has ZERO
re-raising HTTPError handlers today, so this exemption is exercised only by
:mod:`test_httperror_close_guard`'s synthetic fixtures -- which is precisely
why it has them.

Scope
-----
Every ``.py`` file ``git ls-files`` reports for the whole repository, not a
hand-written directory list. Three reasons: the leak is a property of
``urllib.error.HTTPError`` and not of any one package, so nothing in the tree
is exempt by location; a path allowlist is fail-OPEN (a new ``tools/`` script
would simply never be covered) whereas ``git ls-files`` picks up new
directories automatically; and ``tests/test_repo_hygiene.py`` already
establishes ``git ls-files`` over the repository root as this suite's way of
asking "what source do we actually ship". A tracked file that fails to parse
is an error, never a skip.
"""
from __future__ import annotations

import ast
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

#: ``tests/`` -> ``backend/`` -> ``hockey-scheduler/`` -> the repository root.
REPO_ROOT = Path(__file__).resolve().parents[3]

#: Dispositions a scanned handler can carry. ``leaked`` is the default: see
#: the module docstring's "fails CLOSED".
CLOSED = "closed"
RERAISED = "reraised"
LEAKED = "leaked"

#: The attribute name of the exception this module is about, and of the one
#: supertype narrow enough to be worth scanning (see the module docstring).
_HTTP_ERROR = "HTTPError"
_URL_ERROR = "URLError"

#: Supertypes of ``HTTPError`` deliberately OUT of scope, named here so the
#: decision is a visible line rather than an absence. ``except OSError as e:``
#: can bind an ``HTTPError`` -- but it far more often binds a socket error, a
#: file error or a ``ConnectionResetError``, none of which this contract has
#: anything to say about.
_SUPERTYPE_NAMES = ("OSError", "IOError", "EnvironmentError", "Exception",
                    "BaseException")

#: Methods that HAND OWNERSHIP of a bound name to something that will close
#: it. ``enter_context``/``push`` are ``contextlib.ExitStack``'s; ``callback``
#: is how ``e.close`` itself is registered.
_HANDOFF_METHODS = ("enter_context", "push", "callback")


class HttpErrorCloseError(RuntimeError):
    """A file this module cannot parse, or a ledger entry that no longer names
    what it was written for.

    Raised rather than skipped, for the same reason
    :class:`route_extract.ExtractionError` is: a silently ignored file, or a
    silently rotting exemption, is a hole in the gate that reports green.
    """


# --------------------------------------------------------------------------
# 1. Findings
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Handler:
    """One ``except`` handler that can bind an ``HTTPError``.

    ``path`` is repository-relative POSIX text; ``qualname`` is the dotted
    class/function scope the handler sits in (``""`` at module level);
    ``occurrence`` is the 0-based index of this handler among the HTTPError-
    binding handlers of that same ``qualname``, in source order. Those three
    are the ledger key -- chosen over ``lineno`` on purpose, so that editing
    an unrelated line above a catalogued handler does not invalidate its
    entry and turn every refactor into a ledger churn.
    """

    path: str
    qualname: str
    occurrence: int
    lineno: int
    name: Optional[str]
    types: tuple
    disposition: str
    evidence: str

    @property
    def key(self) -> tuple:
        return (self.path, self.qualname, self.occurrence)

    def describe(self) -> str:
        where = self.qualname or "<module>"
        return (f"{self.path}:{self.lineno} in {where} "
                f"[{'/'.join(self.types) or '<bare except>'} as "
                f"{self.name or '<unbound>'}] -> {self.disposition}"
                f" ({self.evidence})")


# --------------------------------------------------------------------------
# 2. Which handlers can bind an HTTPError
# --------------------------------------------------------------------------


def _name_bindings(tree: ast.Module) -> tuple:
    """Resolve, from a module's OWN imports, every name that denotes
    ``urllib.error.HTTPError`` / ``URLError`` and every name that denotes the
    ``urllib.error`` MODULE.

    Import aliasing is the obvious way a name-matching check goes blind
    (``from urllib.error import HTTPError as HE`` then ``except HE as e:``),
    so the aliases are read rather than guessed. The dotted-tail fallback in
    :func:`_classified_type` still catches spellings this misses -- these two
    layers overlap on purpose.
    """
    http, url, modules = set(), set(), {"urllib.error"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "urllib.error" and alias.asname:
                    modules.add(alias.asname)
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                bound = alias.asname or alias.name
                if node.module == "urllib.error":
                    if alias.name == _HTTP_ERROR:
                        http.add(bound)
                    elif alias.name == _URL_ERROR:
                        url.add(bound)
                elif node.module == "urllib" and alias.name == "error":
                    modules.add(bound)
    return frozenset(http), frozenset(url), frozenset(modules)


def _classified_type(expr: ast.expr, http: frozenset, url: frozenset,
                     modules: frozenset) -> Optional[str]:
    """``"http"``, ``"url"`` or ``None`` for ONE exception-type expression."""
    if isinstance(expr, ast.Name):
        if expr.id in http:
            return "http"
        if expr.id in url:
            return "url"
        return None
    if isinstance(expr, ast.Attribute):
        base = ast.unparse(expr.value)
        if expr.attr == _HTTP_ERROR and (base in modules
                                         or base.endswith("error")):
            return "http"
        if expr.attr == _URL_ERROR and (base in modules
                                        or base.endswith("error")):
            return "url"
    # Fallback for a spelling the import scan did not model (a re-export, a
    # module-level ``HTTPError = urllib.error.HTTPError``): match the dotted
    # tail. Generous on purpose -- this check fails closed.
    text = ast.unparse(expr)
    if text == _HTTP_ERROR or text.endswith(f".{_HTTP_ERROR}"):
        return "http"
    if text == _URL_ERROR or text.endswith(f".{_URL_ERROR}"):
        return "url"
    return None


def _handler_types(handler: ast.ExceptHandler) -> list:
    if handler.type is None:
        return []
    if isinstance(handler.type, ast.Tuple):
        return list(handler.type.elts)
    return [handler.type]


def _binding_handlers(node: ast.Try, http: frozenset, url: frozenset,
                      modules: frozenset) -> list:
    """The handlers of ONE ``try`` that can really receive an ``HTTPError``.

    A ``URLError`` handler is only reachable by an ``HTTPError`` while no
    EARLIER handler on the same ``try`` catches ``HTTPError`` -- Python tries
    handlers in order, so once one matches the subtype the supertype handler
    below it can never see it. Modelling that is what keeps the repository's
    ``except (urllib.error.URLError, http.client.HTTPException) as e:``
    transport-error arms -- all of which sit BELOW an HTTPError arm -- out of
    the findings instead of producing a dozen false positives.
    """
    found, http_caught = [], False
    for handler in node.handlers:
        kinds = {_classified_type(t, http, url, modules)
                 for t in _handler_types(handler)}
        if "http" in kinds:
            found.append((handler, sorted(k for k in kinds if k)))
            http_caught = True
        elif "url" in kinds and not http_caught:
            found.append((handler, sorted(k for k in kinds if k)))
    return found


# --------------------------------------------------------------------------
# 3. Did the handler close what it bound?
# --------------------------------------------------------------------------


def _mentions_name(expr: ast.expr, name: str) -> bool:
    return any(isinstance(n, ast.Name) and n.id == name
               for n in ast.walk(expr))


def _closing_evidence(handler: ast.ExceptHandler, name: str) -> Optional[str]:
    """The FIRST closing idiom found in ``handler``'s body, or ``None``.

    Searches the whole body, so a ``try:``/``finally: e.close()`` counts and a
    close inside a conditional counts. Deliberately does not try to prove the
    close happens on every path -- a handler that closes on SOME path is a
    different (and much rarer) defect than #426's, and treating it as a
    violation would report code that is visibly trying to do the right thing.
    """
    body = ast.Module(body=list(handler.body), type_ignores=[])
    for node in ast.walk(body):
        if isinstance(node, (ast.With, ast.AsyncWith)):
            for item in node.items:
                expr = item.context_expr
                if isinstance(expr, ast.Name) and expr.id == name:
                    return f"with {name}:"
                if isinstance(expr, ast.Call) and _mentions_name(expr, name):
                    return f"with {ast.unparse(expr)}:"
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            target = node.func.value
            if (node.func.attr == "close" and isinstance(target, ast.Name)
                    and target.id == name):
                return f"{name}.close()"
            if node.func.attr in _HANDOFF_METHODS and any(
                    _mentions_name(arg, name) for arg in node.args):
                return f"{ast.unparse(node.func)}({name}...)"
    return None


#: Statements that leave a handler WITHOUT raising. ``break``/``continue``
#: count: a handler nested in a loop can exit through either.
_NON_RAISING_EXITS = (ast.Return, ast.Break, ast.Continue, ast.Yield,
                      ast.YieldFrom)

#: Nodes whose bodies belong to a DIFFERENT scope, so a ``return`` inside one
#: says nothing about how the handler itself exits.
_NESTED_SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda,
                  ast.ClassDef)


def _walk_scope(body: list):
    """``ast.walk`` over ``body``, but NOT descending into nested function,
    lambda or class bodies."""
    stack = list(body)
    while stack:
        node = stack.pop()
        yield node
        if isinstance(node, _NESTED_SCOPES):
            continue  # its `return` belongs to IT, not to the handler
        stack.extend(ast.iter_child_nodes(node))


def _terminates_in_raise(body: list) -> bool:
    """Does control FALL OFF THE END of ``body``, or is the end unreachable
    because a ``raise`` always happens first?

    Conservative by construction: only ``raise`` itself, an ``if``/``else``
    whose BOTH arms terminate, and a ``try`` whose every arm does, are proved
    terminal. A loop (which may run zero times), a ``with``, a ``match``, or
    anything else returns ``False`` -- i.e. the handler is NOT granted the
    re-raise exemption and is reported. Failing closed here is the point: the
    exemption is the one way a real leak could be waved through.
    """
    if not body:
        return False
    last = body[-1]
    if isinstance(last, ast.Raise):
        return True
    if isinstance(last, ast.If):
        return bool(last.orelse) and (_terminates_in_raise(last.body)
                                      and _terminates_in_raise(last.orelse))
    if isinstance(last, ast.Try):
        if last.finalbody and _terminates_in_raise(last.finalbody):
            return True
        arms = [last.body] + [h.body for h in last.handlers]
        if last.orelse:
            arms.append(last.orelse)
        return all(_terminates_in_raise(arm) for arm in arms)
    return False


def _always_raises(body: list) -> bool:
    """Does EVERY exit from ``body`` go through a ``raise``?

    Two halves, and the second is the one a last-statement-only check gets
    wrong: the end must be unreachable (:func:`_terminates_in_raise`) AND the
    body must contain no earlier ``return``/``break``/``continue``/``yield``.
    A handler shaped ``if e.code == 404: return None`` ... ``raise`` ends in a
    raise and still has a path that leaves without one -- exempting it would
    wave through a genuine leak on exactly that path.
    """
    if any(isinstance(n, _NON_RAISING_EXITS) for n in _walk_scope(body)):
        return False
    return _terminates_in_raise(body)


# --------------------------------------------------------------------------
# 4. The scan
# --------------------------------------------------------------------------


class _Scanner(ast.NodeVisitor):
    """Walks one module, tracking the class/function scope stack so each
    finding carries a line-independent ``qualname``."""

    def __init__(self, path: str, tree: ast.Module):
        self.path = path
        self.http, self.url, self.modules = _name_bindings(tree)
        self._scope = []
        self._seen = {}  # qualname -> how many HTTPError handlers so far
        self.handlers = []

    # -- scope tracking ----------------------------------------------------
    def _in_scope(self, node):
        self._scope.append(node.name)
        self.generic_visit(node)
        self._scope.pop()

    visit_ClassDef = _in_scope
    visit_FunctionDef = _in_scope
    visit_AsyncFunctionDef = _in_scope

    # -- the finding itself ------------------------------------------------
    def _visit_try(self, node):
        qualname = ".".join(self._scope)
        for handler, kinds in _binding_handlers(node, self.http, self.url,
                                                self.modules):
            occurrence = self._seen.get(qualname, 0)
            self._seen[qualname] = occurrence + 1
            self.handlers.append(
                self._classify(handler, kinds, qualname, occurrence))
        self.generic_visit(node)

    visit_Try = _visit_try
    if hasattr(ast, "TryStar"):  # pragma: no branch -- Python 3.11+
        visit_TryStar = _visit_try

    def _classify(self, handler, kinds, qualname, occurrence) -> Handler:
        types = tuple(ast.unparse(t) for t in _handler_types(handler))
        if handler.name is None:
            # Nothing to close: the response object exists (urlopen built it)
            # but the handler never names it, so it can only be reclaimed by
            # the garbage collector -- the #426 leak with the handle thrown
            # away. Bind it with ``as`` and close it.
            return Handler(self.path, qualname, occurrence, handler.lineno,
                           None, types, LEAKED, "the exception is never bound")
        evidence = _closing_evidence(handler, handler.name)
        if evidence is not None:
            return Handler(self.path, qualname, occurrence, handler.lineno,
                           handler.name, types, CLOSED, evidence)
        if _always_raises(handler.body):
            return Handler(self.path, qualname, occurrence, handler.lineno,
                           handler.name, types, RERAISED,
                           "re-raises on every path; ownership passes on")
        return Handler(self.path, qualname, occurrence, handler.lineno,
                       handler.name, types, LEAKED,
                       f"{handler.name} is never closed")


def scan_source(text: str, path: str = "<source>") -> list:
    """Every HTTPError-binding handler in ONE module's source.

    The ``source``-override convention is ``route_extract.extract_routes``':
    the real files are the default, and a caller may hand in a synthetic
    fixture instead -- which is how :mod:`test_httperror_close_guard` proves
    each classification arm without needing a real file to demonstrate it.
    """
    try:
        tree = ast.parse(text)
    except SyntaxError as exc:
        raise HttpErrorCloseError(
            f"{path}: tracked Python that does not parse cannot be checked, "
            f"and is never skipped: {exc}") from None
    scanner = _Scanner(path, tree)
    scanner.visit(tree)
    return scanner.handlers


def tracked_python_files(root: Optional[Path] = None) -> list:
    """Every ``.py`` file git tracks, repository-wide. See the module
    docstring's "Scope" for why this is not a directory allowlist."""
    root = root or REPO_ROOT
    out = subprocess.run(["git", "ls-files", "-z", "*.py"], cwd=root,
                         capture_output=True, text=True, check=True).stdout
    return [root / rel for rel in out.split("\0") if rel]


def scan_repository(root: Optional[Path] = None) -> list:
    """Every HTTPError-binding handler in the whole tracked tree."""
    root = root or REPO_ROOT
    found = []
    for path in tracked_python_files(root):
        rel = path.relative_to(root).as_posix()
        found.extend(scan_source(path.read_text(), rel))
    return found


# --------------------------------------------------------------------------
# 5. The pre-existing-debt ledger
# --------------------------------------------------------------------------
#
# #426 fixed ONE handler. The scan above finds that the same shape is present
# in most of this suite's HTTP helpers -- they were written before the leak
# was understood, and every one of them is a real unclosed response.
#
# They are CATALOGUED here rather than fixed in the same commit, deliberately:
# rewriting ~100 test helpers across ~90 files would collide with every branch
# in flight, and "closing e" is only obviously safe at a site once someone has
# read what that site does with ``e`` afterwards. What must not happen is that
# the debt stays invisible, so:
#
#   * a handler NOT in this ledger must close what it binds -- so new code,
#     and any REVERT of an already-fixed handler, fails immediately;
#   * every entry is fingerprint-verified by :func:`verify_ledger`. An entry
#     naming a handler that no longer exists is an ERROR (it is rotting, and
#     proof nothing depends on it); an entry naming a handler that now CLOSES
#     is an ERROR too (delete it -- the ledger may only shrink). Both mirror
#     ``route_extract.verify_waiver_usage``'s exact-one-hit rule, and both
#     exist so that this list can never quietly become a hole in the gate.
#
# Keyed by (repository-relative path, enclosing class/function scope, index
# among that scope's HTTPError handlers) -- never by line number, so ordinary
# edits above a catalogued handler do not churn the list.
_LEDGER_REASON = (
    "pre-existing #426-shaped unclosed HTTPError, catalogued by #205 when the "
    "guard was introduced; close it (`with e:`) and delete this entry")

_KNOWN_UNCLOSED = frozenset({
    ('hockey-scheduler/backend/tests/test_active_context.py', 'ActiveContextHttpTest._req', 0),
    ('hockey-scheduler/backend/tests/test_actor_attribution.py', 'ActorAttributionHttpTest._req', 0),
    ('hockey-scheduler/backend/tests/test_app_mode.py', 'DemoModeIsTheDefaultTest._get', 0),
    ('hockey-scheduler/backend/tests/test_app_mode.py', 'ProductionModeTest._login', 0),
    ('hockey-scheduler/backend/tests/test_app_mode.py', 'ProductionModeTest._req', 0),
    ('hockey-scheduler/backend/tests/test_archived_history_reproducible.py', 'ArchivedHistoryOverHttpContract._req', 0),
    ('hockey-scheduler/backend/tests/test_auth_fallback.py', 'AuthFallbackTest._req', 0),
    ('hockey-scheduler/backend/tests/test_auth_session.py', 'AuthSessionTest._req', 0),
    ('hockey-scheduler/backend/tests/test_availability_ux.py', 'AvailabilityUxHttpTest._req', 0),
    ('hockey-scheduler/backend/tests/test_bootstrap.py', 'ProductionBootstrapHttpTest._login', 0),
    ('hockey-scheduler/backend/tests/test_bootstrap.py', 'ProductionBootstrapHttpTest._req', 0),
    ('hockey-scheduler/backend/tests/test_calendar_feeds.py', 'CalendarHttpTest._req', 0),
    ('hockey-scheduler/backend/tests/test_calendar_feeds.py', 'CalendarHttpTest._reqx', 0),
    ('hockey-scheduler/backend/tests/test_coach_scope_fail_closed.py', 'CoachScopeHttpTest._req', 0),
    ('hockey-scheduler/backend/tests/test_context_league_http.py', 'LeagueContextHttpContract._req', 0),
    ('hockey-scheduler/backend/tests/test_context_switch_server_exit.py', 'ContextGateFixtureBase._req', 0),
    ('hockey-scheduler/backend/tests/test_cookie_hardening.py', '_CookieBase._post', 0),
    ('hockey-scheduler/backend/tests/test_db_connection_recovery.py', 'HealthHttpStatusTest._get', 0),
    ('hockey-scheduler/backend/tests/test_dead_letter.py', 'DeadLetterHttpAuthzTest._req', 0),
    ('hockey-scheduler/backend/tests/test_demo_add_ice_slot.py', '_request', 0),
    ('hockey-scheduler/backend/tests/test_draft_review.py', 'DraftReviewHttpTest._req', 0),
    ('hockey-scheduler/backend/tests/test_epoch_fence_cross_replica.py', '_Replica.req', 0),
    ('hockey-scheduler/backend/tests/test_epoch_fence_response_pairing.py', '_ParkableReplica.req', 0),
    ('hockey-scheduler/backend/tests/test_facility_tree_exception.py', 'ArchivedSelectedSeasonCandidateHttpTest._req', 0),
    ('hockey-scheduler/backend/tests/test_facility_tree_exception.py', 'GrantCandidateHttpPermissionTest._req', 0),
    ('hockey-scheduler/backend/tests/test_facility_tree_exception.py', 'ScopedVenueAccessHttpTest._req', 0),
    ('hockey-scheduler/backend/tests/test_facility_tree_exception.py', 'SelectedSeasonCeilingHttpTest._req', 0),
    ('hockey-scheduler/backend/tests/test_factory_reset.py', 'FactoryResetHttpTest._req', 0),
    ('hockey-scheduler/backend/tests/test_game_league_season_identity_http.py', 'GameLeagueSeasonIdentityHttpTest._req', 0),
    ('hockey-scheduler/backend/tests/test_guardian_consent.py', 'GuardianConsentHttpTest._get', 0),
    ('hockey-scheduler/backend/tests/test_guardian_consent.py', 'GuardianConsentHttpTest._post', 0),
    ('hockey-scheduler/backend/tests/test_guardian_notification_delivery.py', 'GuardianPreferenceHttpTest._get', 0),
    ('hockey-scheduler/backend/tests/test_guardian_notification_delivery.py', 'GuardianPreferenceHttpTest._post', 0),
    ('hockey-scheduler/backend/tests/test_guardian_workflow.py', 'GuardianHttpTest._get', 0),
    ('hockey-scheduler/backend/tests/test_guardian_workflow.py', 'GuardianHttpTest._post', 0),
    ('hockey-scheduler/backend/tests/test_hardening.py', 'HardeningHttpTest._req', 0),
    ('hockey-scheduler/backend/tests/test_health_readiness.py', 'HealthReadinessHttpTest._get', 0),
    ('hockey-scheduler/backend/tests/test_hierarchy_import.py', 'HierarchyImportHttpTest.request', 0),
    ('hockey-scheduler/backend/tests/test_hierarchy_program_scope.py', 'HierarchyWriteScopeHttpTest._raw', 0),
    ('hockey-scheduler/backend/tests/test_hierarchy_program_scope.py', 'HierarchyWriteScopeHttpTest._req', 0),
    ('hockey-scheduler/backend/tests/test_http_error_boundary.py', '_ErrorBoundaryContract._req', 0),
    ('hockey-scheduler/backend/tests/test_ice_availability.py', 'IceAvailabilityHttpAuthzTest._req', 0),
    ('hockey-scheduler/backend/tests/test_ice_availability_season_scope.py', 'IceAvailabilitySeasonScopeTest._req', 0),
    ('hockey-scheduler/backend/tests/test_import_commit.py', 'ImportCommitHttpTest._req', 0),
    ('hockey-scheduler/backend/tests/test_import_dry_run.py', 'ImportDryRunHttpTest._req', 0),
    ('hockey-scheduler/backend/tests/test_import_officials_availability_commit.py', 'ImportOfficialsAvailabilityCommitHttpTest._req', 0),
    ('hockey-scheduler/backend/tests/test_import_rinks_ice_slots_commit.py', 'ImportRinksIceSlotsCommitHttpTest._req', 0),
    ('hockey-scheduler/backend/tests/test_installation_claim.py', 'InstallationClaimHttpTest._req', 0),
    ('hockey-scheduler/backend/tests/test_jersey_number_rules.py', 'HttpJerseyTest._req', 0),
    ('hockey-scheduler/backend/tests/test_league_context_http.py', 'CanonicalLeagueContextHttpContract._req', 0),
    ('hockey-scheduler/backend/tests/test_league_filtered_dashboard.py', 'DemoOverviewHttpTest._req', 0),
    ('hockey-scheduler/backend/tests/test_league_filtered_overview_v2.py', 'SetupOverviewV2HttpTest._req', 0),
    ('hockey-scheduler/backend/tests/test_league_filtered_standings.py', 'StandingsAuthorizationHttpTest._req', 0),
    ('hockey-scheduler/backend/tests/test_login_security.py', 'LoginSecurityHttpTest._req', 0),
    ('hockey-scheduler/backend/tests/test_manual_player_and_candidates.py', 'ManualPlayerAndCandidateHttpTest._get', 0),
    ('hockey-scheduler/backend/tests/test_manual_player_and_candidates.py', 'ManualPlayerAndCandidateHttpTest._post', 0),
    ('hockey-scheduler/backend/tests/test_notification_preferences.py', 'PreferenceHttpAccessTest._req', 0),
    ('hockey-scheduler/backend/tests/test_official_availability.py', 'AvailabilityHttpAccessTest._req', 0),
    ('hockey-scheduler/backend/tests/test_official_identity.py', 'OfficialHttpTest._post', 0),
    ('hockey-scheduler/backend/tests/test_official_identity.py', 'OfficialHttpTest.test_unbound_official_header_cannot_respond', 0),
    ('hockey-scheduler/backend/tests/test_onboarding_status.py', 'OnboardingStatusHttpTest._get', 0),
    ('hockey-scheduler/backend/tests/test_pending_link_ownership.py', 'PendingLinkOwnershipHttpTest._req', 0),
    ('hockey-scheduler/backend/tests/test_per_user_read_receipts.py', 'PerUserReadReceiptHttpTest._req', 0),
    ('hockey-scheduler/backend/tests/test_player_edit.py', 'UpdatePlayerHttpTest._req', 0),
    ('hockey-scheduler/backend/tests/test_player_home.py', 'PlayerHomeHttpTest._post', 0),
    ('hockey-scheduler/backend/tests/test_player_lifecycle.py', 'SetPlayerActiveHttpTest._req', 0),
    ('hockey-scheduler/backend/tests/test_player_private_field_leak.py', 'PlayerMoveDeleteHttpLeakTest._req', 0),
    ('hockey-scheduler/backend/tests/test_players_http_scope.py', 'PlayersHttpScopeTest._players_raw', 0),
    ('hockey-scheduler/backend/tests/test_players_http_scope.py', 'PlayersHttpScopeTest._req', 0),
    ('hockey-scheduler/backend/tests/test_public_pages.py', '_Base._get', 0),
    ('hockey-scheduler/backend/tests/test_public_privacy.py', '_HttpBase._get', 0),
    ('hockey-scheduler/backend/tests/test_rate_limit.py', 'RateLimitHttpTest._req', 0),
    ('hockey-scheduler/backend/tests/test_rate_limit.py', 'RateLimitHttpTest.test_authenticated_calendar_feed_route_is_not_rate_limited_by_the_public_bucket.authed_req', 0),
    ('hockey-scheduler/backend/tests/test_reschedule_workflow.py', 'RescheduleHttpTest._get', 0),
    ('hockey-scheduler/backend/tests/test_reschedule_workflow.py', 'RescheduleHttpTest._post', 0),
    ('hockey-scheduler/backend/tests/test_route_extract.py', '_real_http_probe', 0),
    ('hockey-scheduler/backend/tests/test_route_extract.py', '_real_http_probe_with_globals', 0),
    ('hockey-scheduler/backend/tests/test_scheduler.py', 'SchedulerHttpTest._req', 0),
    ('hockey-scheduler/backend/tests/test_scheduler_explanations.py', 'SchedulerExplanationHttpTest._request', 0),
    ('hockey-scheduler/backend/tests/test_scheduler_team_overlap.py', 'SchedulerTeamOverlapHttpTest._req', 0),
    ('hockey-scheduler/backend/tests/test_scheduler_turnaround.py', 'TurnaroundHttpTest._req', 0),
    ('hockey-scheduler/backend/tests/test_scheduling_constraints.py', 'ConstraintHttpValidationTest._post', 0),
    ('hockey-scheduler/backend/tests/test_scheduling_policy.py', 'SchedulingPolicyHttpTest._req', 0),
    ('hockey-scheduler/backend/tests/test_scope.py', 'ScopeHttpTest._post', 0),
    ('hockey-scheduler/backend/tests/test_season_dates.py', 'SeasonDateHttpTest._req', 0),
    ('hockey-scheduler/backend/tests/test_season_lifecycle.py', 'SeasonLifecycleHttpTest._req', 0),
    ('hockey-scheduler/backend/tests/test_season_registration_http.py', 'SeasonRegistrationHttpTest._req', 0),
    ('hockey-scheduler/backend/tests/test_season_registration_safety.py', 'RegistrationActorIntegrityHttpTest._post', 0),
    ('hockey-scheduler/backend/tests/test_season_rollover.py', 'SeasonRolloverHttpTest._post', 0),
    ('hockey-scheduler/backend/tests/test_server_authz.py', 'OptionalSessionProductionMatrixContract._get_h', 0),
    ('hockey-scheduler/backend/tests/test_server_authz.py', 'OptionalSessionRouteTests._get_h', 0),
    ('hockey-scheduler/backend/tests/test_server_authz.py', 'ServerAuthzTest._get_h', 0),
    ('hockey-scheduler/backend/tests/test_server_authz.py', 'ServerAuthzTest._post', 0),
    ('hockey-scheduler/backend/tests/test_session_admin.py', 'SessionAdminTest._req', 0),
    ('hockey-scheduler/backend/tests/test_session_admin.py', 'SessionAdminTest.test_invalid_session_cookie_is_rejected', 0),
    ('hockey-scheduler/backend/tests/test_setup_parent_write_scope.py', 'SetupParentWriteScopeHttpTest._req', 0),
    ('hockey-scheduler/backend/tests/test_setup_progress.py', 'SetupProgressHttpTest._req', 0),
    ('hockey-scheduler/backend/tests/test_setup_target_authorization.py', 'SetupTargetAtomicityTest._raw', 0),
    ('hockey-scheduler/backend/tests/test_setup_target_authorization.py', 'SetupTargetAxisConsistencyTest._raw', 0),
    ('hockey-scheduler/backend/tests/test_setup_target_authorization.py', 'SetupTargetLockAtomicityTest._raw', 0),
    ('hockey-scheduler/backend/tests/test_setup_target_authorization.py', 'SetupTargetRouteMatrixTest._raw', 0),
    ('hockey-scheduler/backend/tests/test_standings_route_contract.py', 'StandingsRouteContract._req', 0),
    ('hockey-scheduler/backend/tests/test_substitute_opportunity.py', 'SubstituteOpportunityHttpTest._get', 0),
    ('hockey-scheduler/backend/tests/test_substitute_opportunity.py', 'SubstituteOpportunityHttpTest._post', 0),
    ('hockey-scheduler/backend/tests/test_substitute_outreach.py', 'OutreachHttpTest._get', 0),
    ('hockey-scheduler/backend/tests/test_substitute_outreach.py', 'OutreachHttpTest._post', 0),
    ('hockey-scheduler/backend/tests/test_user_accounts.py', 'AccountCreationHttpTest._req', 0),
    ('hockey-scheduler/backend/tests/test_v1_setup_contract.py', 'V1SetupContractTest._req', 0),
    ('hockey-scheduler/backend/tests/test_v2_setup_contract.py', 'V2SetupContractTest._req', 0),
    ('hockey-scheduler/backend/tests/test_web_server.py', '_request', 0),
    ('hockey-scheduler/backend/tests/test_web_server.py', '_request_selected._call', 0),
    ('hockey-scheduler/backend/tests/test_web_server.py', '_request_signed_in', 0),
    ('hockey-scheduler/backend/tests/test_write_schemas.py', 'WriteSchemaHttpTest._req', 0),
    ('hockey-scheduler/backend/tests/test_write_schemas_uncovered.py', '_HttpBase._req', 0),
    ('hockey-scheduler/backend/tests/test_zero_program_bootstrap_scoping.py', 'ZeroProgramBootstrapScopingHttpTest._req', 0),
})


def verify_ledger(handlers: list) -> None:
    """Fingerprint every :data:`_KNOWN_UNCLOSED` entry against a completed
    scan. See that dict's own comment for what each failure means."""
    by_key = {}
    for handler in handlers:
        by_key.setdefault(handler.key, []).append(handler)
    dormant = [k for k in _KNOWN_UNCLOSED if k not in by_key]
    fixed = [k for k in _KNOWN_UNCLOSED
             if k in by_key and all(h.disposition != LEAKED
                                    for h in by_key[k])]
    if not dormant and not fixed:
        return
    lines = [f"  DORMANT (names no handler any more): {k!r}" for k in dormant]
    lines += [f"  ALREADY FIXED (no longer leaks): {k!r}" for k in fixed]
    raise HttpErrorCloseError(
        "the pre-existing-unclosed-HTTPError ledger has drifted from the "
        "source:\n" + "\n".join(sorted(lines)) +
        "\nA dormant entry must be removed (the handler it names is gone, so "
        "nothing depends on it); an already-fixed entry must be removed too "
        "(the ledger may only ever shrink). Neither may be left as-is: a "
        "stale entry is how this list would quietly become a hole in the "
        "gate.")


def violations(handlers: list) -> list:
    """Every leaking handler this repository has NOT already catalogued."""
    return [h for h in handlers
            if h.disposition == LEAKED and h.key not in _KNOWN_UNCLOSED]


def report(violating: list) -> str:
    return (
        f"{len(violating)} HTTPError handler(s) bind a response and never "
        f"close it. urllib.error.HTTPError IS the response object urlopen() "
        f"returns on success, so an unclosed one leaks a socket exactly like "
        f"an unclosed `with urlopen(...) as r:` would (#426). Close it with "
        f"`with e:`, an explicit `e.close()`, or try/finally:\n"
        + "\n".join(f"  {h.describe()}" for h in violating))


def _main() -> int:  # pragma: no cover - developer CLI
    import argparse

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--all", action="store_true",
                        help="list every handler found, not just violations")
    parser.add_argument("--emit-ledger", action="store_true",
                        help="print today's leaking handlers as ledger entries")
    args = parser.parse_args()
    handlers = scan_repository()
    if args.emit_ledger:
        leaking = [h.key for h in handlers if h.disposition == LEAKED]
        for key in sorted(leaking):
            print(f"    {key!r},")
        return 0
    if args.all:
        for handler in sorted(handlers, key=lambda h: (h.path, h.lineno)):
            print(f"  {handler.describe()}")
    counts = {}
    for handler in handlers:
        counts[handler.disposition] = counts.get(handler.disposition, 0) + 1
    print(f"\n{len(handlers)} HTTPError-binding handlers: "
          + ", ".join(f"{v} {k}" for k, v in sorted(counts.items()))
          + f"; {len(_KNOWN_UNCLOSED)} catalogued in the ledger")
    verify_ledger(handlers)
    violating = violations(handlers)
    if violating:
        print("\n" + report(violating))
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_main())
