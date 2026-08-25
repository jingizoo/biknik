"""#205, ROUND 6 — the SUPPLEMENTAL structural gate, and its own
falsification.

WHERE THIS SITS. ``services/side_provenance.py`` is the SUPPLEMENT. The
PRIMARY protection for the side rule is the behavioural sweep in
``test_authenticated_side_noninterference.py``, which measures what actually
comes out of the server. This file is what makes the supplement's narrower
claim checkable — and, this round, what makes the previous round's version of
that claim HONEST.

TWO THINGS THE PREVIOUS ROUND GOT WRONG, and both are pinned here now:

* **The commit message overclaimed.** It said all four shipped defects were
  reconstructed and required to be reported. The class that claimed it never
  tested F1's ``get_roster`` or F3's ``get_substitutes``, both of which
  shipped as a RAW STORE READ WITH NO SIDE ARGUMENT — a shape the gate could
  not see at all, because there was no side argument to classify.
  :class:`TheGuardCatchesEveryLeakThisBlockerFixed` now covers all FIVE, and
  :class:`TheGuardIsMeasuredAgainstTheRealVulnerableTrees` re-measures them
  against the REAL bytes of each vulnerable commit with ``git archive``
  rather than by text-mutating today's tree.
* **``SUBJECT_OWN_SIDE`` blessed the fifth leak.** Its only machine condition
  was "the function accepts no caller-supplied side", which is a fact about
  the CLIENT and not about the DERIVED value. It is gone; the class that
  replaced it is checked against where the side actually came from, and
  :class:`EveryClassConditionIsEnforced` breaks that condition on purpose to
  prove the check is real.

THE ORDER THIS WAS BUILT IN, because it is the only order that proves
anything: the adversarial cases were written FIRST and confirmed to pass
against the un-guarded tree. The gate was then built until each is caught.
What is pinned here is not "the guard runs" but "the guard catches these
exact regressions and does not catch these exact legitimate shapes".

WHAT EACH CLASS PROVES
======================
:class:`TheGuardIsCleanOnHead`
    The whole package passes, with an EMPTY unclassified ledger.

:class:`ANewLeakFailsTheBuild`
    TWENTY-TWO adversarial mutations of the REAL source, each asserting the
    kind AND the site. Ten are new this round: three for HOLE A (a raw
    two-sided store read, the same shape in a module OUTSIDE the games
    family, and a declared audience reader that stops projecting), six for
    HOLE B (a nested def, a method, a lambda and a redirected import all
    wearing the trusted name, the last reported both at its definition and at
    every call site it poisons) and one for the removed exemption. Eight of
    the ten ESCAPED the round-5 gate, measured by running both gates over the
    same mutated sources.

:class:`TheGuardDoesNotFightLegitimateCases`
    The alias case (the trusted side through a local name) is NOT flagged,
    which is what proves the check is provenance and not spelling.

:class:`TheRegistriesCannotRot`
    Monotonic shrink, per registry.

:class:`TheGuardsOwnTestsCanFail`
    Each detector is NEUTERED in turn and the adversarial cases it owns are
    required to go green.

:class:`TheGuardCatchesEveryLeakThisBlockerFixed`
    All five shipped defects, reconstructed in the live source.

:class:`EveryClassConditionIsEnforced`
    One dedicated test per typed classification — the ruling's "not
    accumulating exemptions".

:class:`TheGuardIsMeasuredAgainstTheRealVulnerableTrees`
    The detectors, over the actual bytes of six commits of this branch.

NO SOCKET, NO STORE, NO BACKEND: this is a source-level contract, so it is
the same on Memory, SQLite and PostgreSQL by construction. The BEHAVIOUR the
guard supplements is pinned tri-store over authenticated HTTP in
``test_authenticated_side_noninterference.py``,
``test_player_home_side_authority.py``, ``test_overview_schedule_side.py``
and ``test_private_game_sibling_routes.py``.

"""

import contextlib
import unittest
from pathlib import Path

from helpers import BACKEND  # noqa: F401

from hockey_scheduler.services import side_provenance as sp

#: A whole-source mutation: ``sources -> sources``.
SERVER = "web/server.py"
FACADE = "api/service.py"


def _sources():
    return dict(sp.package_sources())


def _replace(sources, path, old, new, count=1):
    """Textual surgery on one module, asserting the anchor is unique.

    A mutation whose anchor has drifted must fail LOUDLY here — an
    adversarial case that silently patched nothing would report the guard as
    "catching" a regression it never saw."""
    text = sources[path]
    found = text.count(old)
    if found != count:
        raise AssertionError(
            f"adversarial anchor appears {found} time(s) in {path}, expected "
            f"{count}; the case would patch nothing and prove nothing:\n"
            f"{old[:200]}")
    out = dict(sources)
    out[path] = text.replace(old, new)
    return out


def _append_method(sources, path, source_text):
    """Add a method to the LAST class in ``path``.

    Appended at end of file at class-body indentation, which is inside the
    final class — enough for the AST scan, which is all this gate reads."""
    out = dict(sources)
    out[path] = sources[path].rstrip("\n") + "\n" + source_text + "\n"
    return out


# The anchor every "own side" adversarial case rewrites: the OWN_SIDE branch
# of `/roster-status`, which is the shape round 1 of this blocker created.
_OWN_SIDE_READ = """        if audience == lineup_visibility.OWN_SIDE:
            return self.roster.compute_roster_status(
                game_id, viewer_team_id).to_dict()"""


# ---------------------------------------------------------------------------
# THE ADVERSARIAL CASES. Each returns mutated sources; the test asserts the
# guard reports (kind, function).
# ---------------------------------------------------------------------------
def case_untrusted_side(sources):
    """A call that passes A SIDE THAT IS NOT THE TRUSTED ONE. The trusted
    side is right there in scope and is simply not the one used — so nothing
    about "does this function have the resolution available" can catch it."""
    return _replace(sources, FACADE, _OWN_SIDE_READ, """        if audience == lineup_visibility.OWN_SIDE:
            return self.roster.compute_roster_status(
                game_id, game.away_team_id).to_dict()""")


def case_mixed_disjunct(sources):
    """The trusted side OR an untrusted one. Proves the verdict is "EVERY
    origin is trusted", not "some origin is" — the false-green a naive
    "does the trusted name appear" check would have."""
    return _replace(sources, FACADE, _OWN_SIDE_READ, """        if audience == lineup_visibility.OWN_SIDE:
            return self.roster.compute_roster_status(
                game_id, viewer_team_id or game.away_team_id).to_dict()""")


def case_ternary_hides_the_hint(sources):
    """A client hint smuggled past the ADJUDICATOR through a ternary: the
    adjudicator is still called, on the other branch. A check that accepted
    "an adjudicator call appears among the origins" would pass this, which is
    precisely the #433 review's "a close appears SOMEWHERE" trap."""
    return _replace(sources, FACADE, """        team_id = self._workflow_side(game, team_id, viewer_role,
                                      viewer_team_id, _CANDIDATE_REFUSAL)""",
                    """        team_id = team_id if team_id else self._workflow_side(
            game, team_id, viewer_role, viewer_team_id, _CANDIDATE_REFUSAL)""")


def case_home_default_in_a_new_function(sources):
    """A DEFAULT ARGUMENT THAT REINTRODUCES ``or game.home_team_id`` — the
    literal defect of all four rounds, in a brand-new method."""
    return _append_method(sources, FACADE, '''
    def get_side_summary(self, game_id, team_id=None):
        """A plausible-looking new read."""
        game = self.roster._require_game(game_id)
        team_id = team_id or game.home_team_id
        return self.roster.compute_roster_status(game_id, team_id).to_dict()
''')


def case_new_route_never_resolves(sources):
    """A NEW ROUTE THAT NEVER CALLS THE RESOLUTION AT ALL — it just takes a
    side and forwards it. No home default, no client hint in sight, nothing
    a pattern match would notice."""
    return _append_method(sources, FACADE, '''
    def get_side_rows(self, game_id, team_id):
        """A plausible-looking new read that trusts its caller."""
        game = self.roster._require_game(game_id)
        return self._lineup_rows(game, team_id)
''')


def case_client_hint_into_a_producer(sources):
    """A side read straight out of a request mapping — the fifth-leaf
    shape."""
    return _append_method(sources, FACADE, '''
    def get_hinted_status(self, game_id, query):
        """A plausible-looking new read that believes the client."""
        return self.roster.compute_roster_status(
            game_id, query.get("team_id")).to_dict()
''')


def case_side_omitted_entirely(sources):
    """THE ROUND-4 LEAK, EXACTLY: a producer called with no side at all, so
    its own home default applies to a caller who has no right to it."""
    return _append_method(sources, FACADE, '''
    def get_row_status(self, game_id):
        """A plausible-looking new read with no side at all."""
        return self.roster.compute_roster_status(game_id).status.value
''')


def case_new_dispatch_leaf(sources):
    """A NEW LEAF of the private-game family, behind the same single
    participation gate as the seven that are already there."""
    return _replace(sources, SERVER, '''            if sub == "reschedule":''',
                    '''            if sub == "roster-preview":
                return self._send_api(api.get_board(gid))
            if sub == "reschedule":''')


def case_dispatch_untrusted_viewer_team(sources):
    """The dispatch fills a facade's TRUSTED parameter from something other
    than the hoisted resolution — which is what would make every
    ``param:<reader>.viewer_team_id`` this gate calls trusted a lie."""
    return _replace(sources, SERVER, """                return self._send_api(api.get_roster_status(
                    gid, viewer_role=role, viewer_team_id=own_team))""",
                    """                return self._send_api(api.get_roster_status(
                    gid, viewer_role=role,
                    viewer_team_id=(scope or {}).get("team_id")))""")


def case_dispatch_hoist_replaced(sources):
    """The hoisted resolution itself replaced by a SECOND answer to "which
    side is the caller's" — the drift the hoist exists to end."""
    return _replace(sources, SERVER, """                own_team = game_scoped_own_team_id(
                    role, scope, sub_game, api.store) or \"\"""",
                    """                own_team = (scope or {}).get("team_id") or \"\"""")


def case_producer_reached_through_an_alias_of_the_hint(sources):
    """A local alias of the CLIENT HINT, one assignment away from the
    producer. The alias machinery that lets the legitimate case through must
    not also let this one through."""
    return _replace(sources, FACADE, """        if audience == lineup_visibility.FULL:
            return self._availability_summary_of(
                game, team_id or viewer_team_id or "")""",
                    """        if audience == lineup_visibility.FULL:
            asked = team_id
            return self._availability_summary_of(game, asked)""")


# ---------------------------------------------------------------------------
# HOLE A — the raw two-sided store read, which has NO side argument for the
# provenance detector to classify. This is the shape F1 and F3 shipped as,
# and the shape the round-5 gate was blind to.
# ---------------------------------------------------------------------------
def case_raw_store_read_no_side(sources):
    """A new facade read that goes STRAIGHT to the store and returns the whole
    game's rows. No side argument, no home default, no client hint — nothing a
    provenance check over producer arguments can see."""
    return _append_method(sources, FACADE, '''
    def get_game_roster_rows(self, game_id):
        """A plausible-looking new read."""
        self.roster._require_game(game_id)
        return [_serialize(e) for e in self.store.roster_for_game(game_id)]
''')


def case_raw_store_read_outside_the_games_family(sources):
    """THE SAME SHAPE IN A NEW MODULE. Rounds 4 and 5 both came from outside
    the ``/api/games/{id}/…`` family, so a detector scoped to that family --
    or to the facade -- would not have seen either."""
    out = dict(sources)
    out["services/side_digest.py"] = '''"""A plausible-looking new service."""


class SideDigest:
    def __init__(self, store):
        self.store = store

    def digest(self, game_id):
        return {"subs": [s.player_id
                         for s in self.store.substitutes_for_game(game_id)]}
'''
    return out


def case_adjudicated_reader_stops_projecting(sources):
    """A declared TWO_SIDED_BY_AUDIENCE_READER that stops consulting the
    audience while still reading both sides — F1/F3 restored in place. The
    class condition is a property of the BODY, not of a name in a table, so
    it fires without any registry edit."""
    return _replace(sources, FACADE, """        game = self.roster._require_game(game_id)
        audience = lineup_visibility.route_audience(
            viewer_role, viewer_team_id, game.home_team_id, game.away_team_id)
        if audience == lineup_visibility.FULL:
            return [_serialize(e) for e in self.store.roster_for_game(game_id)]
        if audience == lineup_visibility.SUBMITTED_LINEUP:
            return self._submitted_lineup_sides(game)
        if audience == lineup_visibility.OWN_SIDE:
            return [_serialize(e) for e in self.store.roster_for_game(game_id)
                    if e.attribution is not None
                    and e.attribution[0] == viewer_team_id]
        raise NotAuthorizedError(_PRIVATE_SIDE_REFUSAL)""",
                    """        self.roster._require_game(game_id)
        return [_serialize(e) for e in self.store.roster_for_game(game_id)]""")


# ---------------------------------------------------------------------------
# HOLE B — a callable WEARING the trusted name. Round 5 compared the callee's
# last name component to the string, so every one of these was trusted
# unconditionally.
# ---------------------------------------------------------------------------
def case_nested_def_forges_the_resolver(sources):
    """A nested ``def`` with the trusted name, shadowing it for one
    function."""
    return _append_method(sources, FACADE, '''
    def get_forged_by_nested_def(self, game_id, query):
        """A plausible-looking new read."""
        def game_scoped_own_team_id(role, scope, game, store):
            return query.get("team_id")
        game = self.roster._require_game(game_id)
        return self.roster.compute_roster_status(
            game_id,
            game_scoped_own_team_id(None, None, game, self.store)).to_dict()
''')


def case_method_forges_the_resolver(sources):
    """A METHOD on the facade with the trusted name — ``self.`` in front of
    it, and a last name component that matches exactly."""
    return _append_method(sources, FACADE, '''
    def game_scoped_own_team_id(self, role, scope, game, store):
        """A plausible-looking helper."""
        return (scope or {}).get("team_id")

    def get_forged_by_method(self, game_id, scope):
        """A plausible-looking new read."""
        game = self.roster._require_game(game_id)
        return self.roster.compute_roster_status(
            game_id,
            self.game_scoped_own_team_id(None, scope, game, self.store)
        ).to_dict()
''')


def case_lambda_forges_the_resolver(sources):
    """A local lambda bound to the trusted name."""
    return _append_method(sources, FACADE, '''
    def get_forged_by_lambda(self, game_id, query):
        """A plausible-looking new read."""
        game_scoped_own_team_id = lambda r, s, g, st: query.get("team_id")
        game = self.roster._require_game(game_id)
        return self.roster.compute_roster_status(
            game_id,
            game_scoped_own_team_id(None, None, game, self.store)).to_dict()
''')


def case_import_alias_forges_the_resolver(sources):
    """THE SUBTLEST ONE: the facade keeps calling ``game_scoped_own_team_id``
    exactly as it always did, and the IMPORT is redirected to a different
    module. Every call site is byte-identical; only the binding moved."""
    out = dict(sources)
    out["services/forged_scope.py"] = '''"""A plausible-looking helper module."""


def game_scoped_own_team_id(role, scope, game, store):
    return (scope or {}).get("team_id")
'''
    return _replace(
        out, FACADE,
        "from ..services.game_side_scope import game_scoped_own_team_id",
        "from ..services.forged_scope import game_scoped_own_team_id")


# ---------------------------------------------------------------------------
# THE REMOVED EXEMPTION — the fifth leak's own shape, which SUBJECT_OWN_SIDE
# blessed.
# ---------------------------------------------------------------------------
def case_subject_side_from_the_permanent_pointer(sources):
    """A ``/api/me/*``-shaped read that takes NO side parameter — so the old
    SUBJECT_OWN_SIDE condition is satisfied — and derives the side from
    ``Player.team_id``, the permanent pointer. This is ``get_player_home``
    before round 6, and the round-5 gate passed it."""
    return _append_method(sources, FACADE, '''
    def get_my_next_game_status(self, player_id, game_id):
        """A plausible-looking new subject-scoped read."""
        player = self.store.get_player(player_id)
        return self.roster.compute_roster_status(
            game_id, player.team_id).to_dict()
''')


#: ``name -> (mutation, expected kind, expected function)``. Every entry is a
#: regression this blocker actually saw, or a way of hiding one.
ADVERSARIAL = {
    "untrusted_side": (
        case_untrusted_side, "untrusted_side", "get_roster_status"),
    "mixed_disjunct": (
        case_mixed_disjunct, "untrusted_side", "get_roster_status"),
    "ternary_hides_the_hint": (
        case_ternary_hides_the_hint, "untrusted_side",
        "get_substitute_candidates"),
    "home_default_in_a_new_function": (
        case_home_default_in_a_new_function, "home_team_fallback",
        "get_side_summary"),
    "home_default_reaches_a_producer": (
        case_home_default_in_a_new_function, "untrusted_side",
        "get_side_summary"),
    "new_route_never_resolves": (
        case_new_route_never_resolves, "undeclared_forwarder",
        "get_side_rows"),
    "client_hint_into_a_producer": (
        case_client_hint_into_a_producer, "untrusted_side",
        "get_hinted_status"),
    "side_omitted_entirely": (
        case_side_omitted_entirely, "untrusted_side", "get_row_status"),
    "new_dispatch_leaf": (
        case_new_dispatch_leaf, "undeclared_private_game_leaf",
        "_dispatch_get"),
    "dispatch_untrusted_viewer_team": (
        case_dispatch_untrusted_viewer_team, "dispatch_untrusted_viewer_team",
        "_dispatch_get"),
    "dispatch_hoist_replaced": (
        case_dispatch_hoist_replaced, "dispatch_hoist_untrusted",
        "_dispatch_get"),
    "alias_of_the_hint": (
        case_producer_reached_through_an_alias_of_the_hint, "untrusted_side",
        "get_availability_summary"),
    # -- HOLE A -----------------------------------------------------------
    "raw_store_read_no_side": (
        case_raw_store_read_no_side, "undeclared_two_sided_read",
        "get_game_roster_rows"),
    "raw_store_read_outside_the_games_family": (
        case_raw_store_read_outside_the_games_family,
        "undeclared_two_sided_read", "digest"),
    "adjudicated_reader_stops_projecting": (
        case_adjudicated_reader_stops_projecting, "two_sided_class_broken",
        "get_roster"),
    # -- HOLE B -----------------------------------------------------------
    "nested_def_forges_the_resolver": (
        case_nested_def_forges_the_resolver, "untrusted_side",
        "get_forged_by_nested_def"),
    "nested_def_is_reported_at_its_definition": (
        case_nested_def_forges_the_resolver, "forged_trusted_resolver",
        "game_scoped_own_team_id"),
    "method_forges_the_resolver": (
        case_method_forges_the_resolver, "untrusted_side",
        "get_forged_by_method"),
    "lambda_forges_the_resolver": (
        case_lambda_forges_the_resolver, "untrusted_side",
        "get_forged_by_lambda"),
    "import_alias_forges_the_resolver": (
        case_import_alias_forges_the_resolver, "forged_trusted_resolver",
        "<module>"),
    "import_alias_poisons_every_call_site": (
        case_import_alias_forges_the_resolver, "untrusted_side",
        "get_player_home"),
    # -- THE REMOVED EXEMPTION --------------------------------------------
    "subject_side_from_the_permanent_pointer": (
        case_subject_side_from_the_permanent_pointer, "untrusted_side",
        "get_my_next_game_status"),
}


class _GuardHarness:
    """Runs the gate over mutated sources and answers "was THIS caught"."""

    def _audit(self, sources, verify_liveness=False):
        return sp.audit(sources=sources, verify_liveness=verify_liveness)

    def _caught(self, violations, kind, function):
        return [v for v in violations
                if v.kind == kind and v.function == function]

    def _assert_case_caught(self, name):
        mutate, kind, function = ADVERSARIAL[name]
        violations, _errors = self._audit(mutate(_sources()))
        hits = self._caught(violations, kind, function)
        self.assertTrue(
            hits,
            f"ADVERSARIAL CASE {name!r} WAS NOT CAUGHT. Expected a "
            f"{kind!r} violation in {function}(); the gate reported "
            f"{[(v.kind, v.function) for v in violations]!r}")
        # And it NAMES the fix, not just a line number -- the whole point of
        # the message contract.
        for hit in hits:
            self.assertTrue(hit.should_be.strip(),
                            f"{name}: the violation names no fix")
            self.assertIn(str(hit.line), str(hit), name)

    @contextlib.contextmanager
    def _neutered(self, what):
        """Disable ONE detector, the way a future edit might."""
        if what == "producers":
            saved, sp.PRODUCERS = sp.PRODUCERS, {}
            try:
                yield
            finally:
                sp.PRODUCERS = saved
        elif what == "home_fallbacks":
            saved = sp.audit_home_fallback
            sp.audit_home_fallback = lambda sources=None: ([], set())
            try:
                yield
            finally:
                sp.audit_home_fallback = saved
        elif what == "dispatch":
            saved = sp.audit_dispatch
            sp.audit_dispatch = lambda source=None: ([], set())
            try:
                yield
            finally:
                sp.audit_dispatch = saved
        elif what == "two_sided":
            saved = sp.audit_two_sided_store_reads
            sp.audit_two_sided_store_reads = lambda sources=None: ([], set())
            try:
                yield
            finally:
                sp.audit_two_sided_store_reads = saved
        elif what == "trusted_binding":
            # The Hole-B neutering a careless edit really produces: go back
            # to comparing the callee's LAST NAME COMPONENT to the string.
            saved = sp._ModuleContext.resolves_to_canonical
            sp._ModuleContext.resolves_to_canonical = \
                lambda self, func, scope: True
            saved_binding = sp.audit_trusted_binding
            sp.audit_trusted_binding = lambda sources=None: []
            try:
                yield
            finally:
                sp._ModuleContext.resolves_to_canonical = saved
                sp.audit_trusted_binding = saved_binding
        elif what == "trusted_everything":
            # The subtlest neutering, and the one a careless edit really
            # produces: every origin declared trusted.
            saved = sp._trusted_origin
            sp._trusted_origin = lambda origin, readers, adjudicators: True
            try:
                yield
            finally:
                sp._trusted_origin = saved
        else:  # pragma: no cover - a typo must be loud
            raise AssertionError(f"unknown neutering {what!r}")


# ---------------------------------------------------------------------------
# 1. THE REAL TREE PASSES, WITH AN EMPTY UNCLASSIFIED LEDGER.
# ---------------------------------------------------------------------------
class TheGuardIsCleanOnHead(_GuardHarness, unittest.TestCase):

    def test_no_private_state_read_reaches_an_unclassified_side(self):
        violations, errors = sp.audit()
        self.assertEqual(
            [], violations,
            "private-state read(s) reached a side without the server's "
            "trusted resolution:\n" + sp.report(violations, errors))
        self.assertEqual(
            [], errors,
            "side_provenance registry error(s):\n" + sp.report(violations,
                                                               errors))

    def test_the_unclassified_ledger_is_empty(self):
        """The strongest state this table can be in. Every producer call in
        the package is TRUSTED by provenance, a declared forwarder, or an
        exemption whose condition is machine-checked -- so nothing is merely
        tolerated. This assertion is what stops the ledger being the place a
        future round quietly parks a leak."""
        self.assertEqual(
            {}, sp.LEDGER,
            "side_provenance.LEDGER is the accepted-but-unclassified list "
            "and it was empty when this guard shipped. An entry here means "
            "'we know about this one and have not decided'.")

    def test_the_gate_actually_examined_the_package(self):
        """A gate that scanned nothing would pass every assertion above."""
        sources = sp.package_sources()
        self.assertIn(FACADE, sources)
        self.assertIn(SERVER, sources)
        self.assertIn("services/roster_service.py", sources)
        self.assertGreater(len(sources), 40, sorted(sources))
        # And it really found producer calls to classify.
        seen = 0
        for producer in sp.PRODUCERS:
            seen += sum(text.count(producer + "(")
                        for text in sources.values())
        self.assertGreater(seen, 20, "the producer set matched almost "
                                     "nothing, so nothing was checked")


# ---------------------------------------------------------------------------
# 2. EVERY ADVERSARIAL CASE IS CAUGHT.
# ---------------------------------------------------------------------------
class ANewLeakFailsTheBuild(_GuardHarness, unittest.TestCase):
    """Written before the guard existed and confirmed to pass against the
    un-guarded tree; the guard was then built until each is caught."""

    def test_every_adversarial_case_is_caught(self):
        for name in sorted(ADVERSARIAL):
            with self.subTest(case=name):
                self._assert_case_caught(name)

    def test_the_failure_message_names_the_site_and_the_fix(self):
        """"violation at line N" costs the next person an afternoon."""
        violations, _errors = self._audit(
            case_side_omitted_entirely(_sources()))
        hit = next(v for v in violations if v.function == "get_row_status")
        text = str(hit)
        self.assertIn(FACADE, text)                       # WHERE
        self.assertIn("get_row_status", text)             # WHICH function
        self.assertIn("compute_roster_status", text)      # WHAT it read
        self.assertIn("absent", text)                     # HOW it got the side
        self.assertIn(sp.TRUSTED_RESOLVER, text)          # WHAT to use instead
        self.assertIn("EXEMPTIONS", text)                 # the declared way out

    def test_a_mutation_that_patches_nothing_fails_loudly(self):
        """The adversarial harness's own self-check: a drifted anchor must
        raise here rather than silently reporting a case as 'caught'."""
        with self.assertRaises(AssertionError):
            _replace(_sources(), FACADE, "this text is not in the facade", "x")


# ---------------------------------------------------------------------------
# 3. IT DOES NOT FIGHT THE LEGITIMATE CASES.
# ---------------------------------------------------------------------------
class TheGuardDoesNotFightLegitimateCases(_GuardHarness, unittest.TestCase):

    def test_the_trusted_side_through_an_alias_is_accepted(self):
        """PROVENANCE, NOT SPELLING. The trusted side reaches the producer
        through a local name the gate has never heard of. If this were
        flagged, the check would be matching an identifier rather than
        following the value -- and every real refactor would fight it."""
        aliased = _replace(_sources(), FACADE, _OWN_SIDE_READ, """        if audience == lineup_visibility.OWN_SIDE:
            mine = viewer_team_id
            return self.roster.compute_roster_status(
                game_id, mine).to_dict()""")
        violations, _errors = self._audit(aliased)
        self.assertEqual(
            [], [v for v in violations if v.function == "get_roster_status"],
            "an ALIAS of the trusted side was reported as untrusted, so the "
            "gate is matching names rather than following provenance:\n"
            + sp.report(violations, []))

    def test_a_two_hop_alias_of_the_trusted_side_is_accepted(self):
        aliased = _replace(_sources(), FACADE, _OWN_SIDE_READ, """        if audience == lineup_visibility.OWN_SIDE:
            mine = viewer_team_id
            still_mine = mine
            return self.roster.compute_roster_status(
                game_id, still_mine).to_dict()""")
        violations, _errors = self._audit(aliased)
        self.assertEqual(
            [], [v for v in violations if v.function == "get_roster_status"],
            sp.report(violations, []))

    def test_the_exemptions_are_documented_first_class_cases(self):
        """Not debt: every exemption names a CLASS whose condition is
        checked, and gives a reason a reader can evaluate."""
        for key, (klass, _route, reason) in sorted(sp.EXEMPTIONS.items()):
            _fn, _producer, fingerprint = key
            with self.subTest(site=key):
                self.assertIn(klass, sp._FORWARDER_CONDITIONS, key)
                self.assertTrue(
                    fingerprint,
                    f"{key} carries no ORIGIN FINGERPRINT, so it would "
                    f"cover any future call to that producer from that "
                    f"function, not the one site it was reviewed for")
                self.assertGreater(
                    len(reason), 30,
                    f"{key} carries no real justification: {reason!r}")
        for name, (klass, reason) in sorted(sp.SIDE_FORWARDERS.items()):
            with self.subTest(forwarder=name):
                self.assertIn(klass, sp._FORWARDER_CONDITIONS, name)
                self.assertGreater(len(reason), 20, name)
        for name, (_module, reason) in sorted(
                sp.LIVE_MEMBERSHIP_READERS.items()):
            with self.subTest(live_membership=name):
                self.assertGreater(len(reason), 40, name)

    def test_the_named_legitimate_cases_are_all_present(self):
        """The four the ruling names by hand, so a future edit that quietly
        drops one is noticed."""
        self.assertIn("_availability_candidates", sp.LIVE_MEMBERSHIP_READERS)
        self.assertIn("list_addable_players", sp.LIVE_MEMBERSHIP_READERS)
        # the create-state side of the standing ruling
        self.assertIn("add_substitute_to_roster", sp.LIVE_MEMBERSHIP_READERS)
        # unscoped-operator paths that genuinely take no side
        self.assertEqual(
            sp.EXEMPTIONS[("_draft_review_row", "compute_roster_status",
                           "absent")][0],
            sp.OPERATOR_ONLY_ROUTE)

    def test_an_operator_only_exemption_is_tied_to_the_route_registry(self):
        """OPERATOR_ONLY_ROUTE is only as good as the route it names, so the
        gate checks that route's recorded auth rather than taking the
        claim."""
        errors = sp._route_is_operator_only("get_scheduler_drafts", "x")
        self.assertEqual([], errors, errors)
        errors = sp._route_is_operator_only("get_api_health", "x")
        self.assertTrue(errors, "a NON-operator route was accepted as the "
                                "basis for an operator-only exemption")
        errors = sp._route_is_operator_only("no_such_route_at_all", "x")
        self.assertTrue(errors)


# ---------------------------------------------------------------------------
# 4. THE REGISTRIES CANNOT ROT, AND CAN ONLY SHRINK.
# ---------------------------------------------------------------------------
class TheRegistriesCannotRot(_GuardHarness, unittest.TestCase):

    @contextlib.contextmanager
    def _registry(self, name, value):
        saved = getattr(sp, name)
        setattr(sp, name, value)
        try:
            yield
        finally:
            setattr(sp, name, saved)

    def _liveness_errors(self, sources=None):
        sources = sources or _sources()
        _v, usage = sp.audit_side_provenance(sources)
        _f, used = sp.audit_home_fallback(sources)
        usage["home_fallbacks"] = used
        _d, leaves = sp.audit_dispatch(sources[SERVER])
        return sp.verify_registry_liveness(sources, usage, leaves)

    def test_a_dormant_exemption_is_an_error(self):
        with self._registry("EXEMPTIONS", {
                **sp.EXEMPTIONS,
                ("get_board", "list_addable_players", "absent"):
                    (sp.OPERATOR_DEFAULT, None, "a" * 60)}):
            errors = self._liveness_errors()
        self.assertTrue(
            any("DORMANT" in e and "list_addable_players" in e
                for e in errors),
            f"a dormant exemption was accepted, so the table can rot: "
            f"{errors}")

    def test_a_dormant_ledger_entry_is_an_error(self):
        with self._registry("LEDGER", {
                ("get_board", "lineup_population", "absent"): "stale"}):
            errors = self._liveness_errors()
        self.assertTrue(any("LEDGER" in e and "DORMANT" in e for e in errors),
                        f"the ledger can rot: {errors}")

    def test_a_dormant_home_fallback_entry_is_an_error(self):
        with self._registry("HOME_FALLBACKS", {
                **sp.HOME_FALLBACKS,
                ("api/service.py", "get_lineups"): "stale"}):
            errors = self._liveness_errors()
        self.assertTrue(any("HOME_FALLBACKS" in e and "DORMANT" in e
                            for e in errors), errors)

    def test_a_dormant_dispatch_leaf_is_an_error(self):
        with self._registry("PRIVATE_GAME_LEAVES", {
                **sp.PRIVATE_GAME_LEAVES,
                "roster-preview": "never existed"}):
            errors = self._liveness_errors()
        self.assertTrue(any("PRIVATE_GAME_LEAVES" in e and "DORMANT" in e
                            for e in errors), errors)

    def test_a_dormant_forwarder_is_an_error(self):
        with self._registry("SIDE_FORWARDERS", {
                **sp.SIDE_FORWARDERS,
                "get_lineups": (sp.ADJUDICATED, "b" * 40)}):
            errors = self._liveness_errors()
        self.assertTrue(any("SIDE_FORWARDERS" in e and "DORMANT" in e
                            for e in errors), errors)

    def test_a_producer_that_no_longer_exists_is_an_error(self):
        with self._registry("PRODUCERS",
                            dict(sp.PRODUCERS, compute_side_status=("team_id", 2))), \
             self._registry("PRODUCER_MODULES",
                            dict(sp.PRODUCER_MODULES,
                                 compute_side_status="services/roster_service.py")):
            errors = self._liveness_errors()
        self.assertTrue(
            any("compute_side_status" in e for e in errors),
            f"a producer that does not exist was accepted, so a RENAMED "
            f"producer would silently stop being checked: {errors}")

    def test_a_producer_whose_side_parameter_moved_is_an_error(self):
        with self._registry("PRODUCERS",
                            dict(sp.PRODUCERS,
                                 compute_roster_status=("team_id", 1))):
            errors = self._liveness_errors()
        self.assertTrue(
            any("compute_roster_status" in e and "index" in e
                for e in errors),
            f"a wrong side index was accepted, so every call site would be "
            f"classified on the wrong argument: {errors}")

    def test_an_adjudicator_that_stops_adjudicating_is_an_error(self):
        """The one place a client hint may be laundered. If it stops
        consulting the audience it launders nothing, and every call site
        trusting it becomes a hole."""
        sources = _replace(
            _sources(), FACADE,
            """        audience = lineup_visibility.route_audience(
            viewer_role, viewer_team_id, game.home_team_id, game.away_team_id)
        if audience == lineup_visibility.OWN_SIDE:
            return viewer_team_id""",
            """        audience = None
        if audience == lineup_visibility.OWN_SIDE:
            return viewer_team_id""")
        errors = self._liveness_errors(sources)
        self.assertTrue(
            any("_workflow_side" in e and "ADJUDICATOR" in e
                for e in errors),
            f"an adjudicator that stopped adjudicating was accepted: "
            f"{errors}")

    def test_an_ambiguous_declaration_is_an_error(self):
        """`ApiService.add_substitute_to_roster` is a thin facade wrapper
        around `RosterService.add_substitute_to_roster`. An unqualified
        declaration would have its condition checked against whichever body
        sorted first -- a coin flip, and a silent one."""
        with self._registry("LIVE_MEMBERSHIP_READERS", {
                name: (None, reason) for name, (_m, reason)
                in sp.LIVE_MEMBERSHIP_READERS.items()}):
            errors = self._liveness_errors()
        self.assertTrue(
            any("AMBIGUOUS" in e for e in errors),
            f"a declaration resolving to more than one function was accepted "
            f"without qualification: {errors}")


# ---------------------------------------------------------------------------
# 5. THE GUARD'S OWN TESTS CAN FAIL.
# ---------------------------------------------------------------------------
class TheGuardsOwnTestsCanFail(_GuardHarness, unittest.TestCase):
    """A guard whose own tests cannot fail is the thing this task exists to
    prevent, so each detector is NEUTERED in turn and the adversarial cases
    it owns are required to stop being caught.

    Reported BY NAME: a neutering that leaves the assertions red would mean
    those assertions are passing for some other reason entirely."""

    #: ``neutering -> the adversarial cases that detector alone catches``
    OWNED = {
        "producers": ("untrusted_side", "mixed_disjunct",
                      "ternary_hides_the_hint", "new_route_never_resolves",
                      "client_hint_into_a_producer", "side_omitted_entirely",
                      "alias_of_the_hint"),
        "home_fallbacks": ("home_default_in_a_new_function",),
        "dispatch": ("new_dispatch_leaf", "dispatch_untrusted_viewer_team",
                     "dispatch_hoist_replaced"),
        "trusted_everything": ("untrusted_side", "mixed_disjunct",
                               "ternary_hides_the_hint",
                               "client_hint_into_a_producer",
                               "side_omitted_entirely",
                               "subject_side_from_the_permanent_pointer"),
        "two_sided": ("raw_store_read_no_side",
                      "raw_store_read_outside_the_games_family",
                      "adjudicated_reader_stops_projecting"),
        "trusted_binding": ("nested_def_forges_the_resolver",
                            "nested_def_is_reported_at_its_definition",
                            "method_forges_the_resolver",
                            "lambda_forges_the_resolver",
                            "import_alias_forges_the_resolver",
                            "import_alias_poisons_every_call_site"),
    }

    def test_neutering_each_detector_makes_its_cases_go_green(self):
        for neutering, cases in sorted(self.OWNED.items()):
            for case in cases:
                with self.subTest(neutering=neutering, case=case):
                    with self._neutered(neutering):
                        try:
                            self._assert_case_caught(case)
                        except AssertionError:
                            continue
                    self.fail(
                        f"NEUTERING {neutering!r} did not break the "
                        f"adversarial case {case!r}: the case still reported "
                        f"as caught with that detector disabled, so it is "
                        f"not actually pinned by it and the guard's own test "
                        f"cannot fail.")

    def test_neutering_the_producer_set_makes_head_pass_vacuously(self):
        """The most important single neutering: with no producers there is
        nothing to classify, and an empty result would look like success."""
        with self._neutered("producers"):
            violations, _errors = self._audit(
                case_side_omitted_entirely(_sources()))
        self.assertEqual(
            [], [v for v in violations if v.kind == "untrusted_side"],
            "the producer set was emptied and side-provenance violations "
            "were still reported, so the detector under test is not the one "
            "producing them")


# ---------------------------------------------------------------------------
# 6. EVERY LEAK THIS BLOCKER FIXED — INCLUDING THE TWO THE PREVIOUS COMMIT
#    MESSAGE CLAIMED AND THIS CLASS DID NOT TEST.
# ---------------------------------------------------------------------------
class TheGuardCatchesEveryLeakThisBlockerFixed(_GuardHarness,
                                               unittest.TestCase):
    """The claim the whole exercise rests on, made checkable — and CORRECTED.

    THE OVERCLAIM THIS CLASS REPLACES. Its previous name said "the four leaks
    this blocker fixed" and its previous body tested four shapes, none of
    which was F1's ``get_roster`` or F3's ``get_substitutes``: both of those
    shipped as a RAW STORE READ WITH NO SIDE ARGUMENT, which the round-5 gate
    could not see at all. The commit message claimed all four were
    reconstructed and required to be reported. They were not.

    All FIVE are here now, each RECONSTRUCTED in the live source and required
    to be reported. This is a stronger statement than the synthetic
    adversarial cases above: these are not shapes chosen to be catchable,
    they are the shapes that actually shipped and were actually found by
    hand, one round at a time.

    Reconstruction here is text surgery on TODAY's tree, with unique anchors
    that fail loudly if they drift. The independent measurement against the
    REAL historical sources — ``git archive`` at each vulnerable commit — is
    :class:`TheGuardIsMeasuredAgainstTheRealVulnerableTrees`."""

    def test_round_1_get_board_hard_coding_home_for_everybody(self):
        """`get_board` read `game.home_team_id` unconditionally, so an AWAY
        Coach was handed the HOME side's private pool."""
        leaked = _replace(_sources(), FACADE,
                          "        team_id = team_id or game.home_team_id",
                          "        team_id = game.home_team_id")
        violations, _errors = self._audit(leaked)
        self.assertTrue(
            self._caught(violations, "untrusted_side", "get_board"),
            "the round-1 defect was not reported:\n"
            + sp.report(violations, []))

    def test_round_1_roster_status_losing_its_audience_test(self):
        """`/roster-status` called `compute_roster_status(game_id)` with no
        team for EVERY caller. Reconstructed by deleting the audience test:
        the remaining call site's provenance is unchanged (`absent` is the
        unscoped-operator branch's own legitimate fingerprint), so what
        catches it is the DECLARATION -- `get_roster_status` is declared an
        adjudicated reader, and a reader that stops consulting the audience
        stops being one."""
        leaked = _replace(_sources(), FACADE, """        game = self.roster._require_game(game_id)
        audience = lineup_visibility.route_audience(
            viewer_role, viewer_team_id, game.home_team_id, game.away_team_id)
        if audience == lineup_visibility.FULL:
            return self.roster.compute_roster_status(game_id).to_dict()
        if audience == lineup_visibility.OWN_SIDE:
            return self.roster.compute_roster_status(
                game_id, viewer_team_id).to_dict()
        raise NotAuthorizedError(_ROSTER_STATUS_REFUSAL)""",
                          """        self.roster._require_game(game_id)
        return self.roster.compute_roster_status(game_id).to_dict()""")
        _violations, errors = self._audit(leaked, verify_liveness=True)
        self.assertTrue(
            any("get_roster_status" in e for e in errors),
            f"a declared adjudicated reader that stopped consulting the "
            f"audience was accepted: {errors}")

    def test_round_2_availability_summary_taking_its_side_from_the_client(self):
        """The fifth leaf: the query-string `?team_id=` WAS the side
        selector, and an assigned official fell straight through the inline
        COACH/PLAYER narrowing."""
        leaked = _replace(_sources(), FACADE, """        game = self.roster._require_game(game_id)
        audience = lineup_visibility.route_audience(
            viewer_role, viewer_team_id, game.home_team_id, game.away_team_id)
        if audience == lineup_visibility.OWN_SIDE:
            return self._availability_summary_of(game, viewer_team_id)
        if audience == lineup_visibility.FULL:
            return self._availability_summary_of(
                game, team_id or viewer_team_id or "")
        raise NotAuthorizedError(_AVAILABILITY_REFUSAL)""",
                          """        game = self.roster._require_game(game_id)
        return self._availability_summary_of(game, team_id or "")""")
        violations, errors = self._audit(leaked, verify_liveness=True)
        self.assertTrue(
            self._caught(violations, "untrusted_side",
                         "get_availability_summary")
            or any("get_availability_summary" in e for e in errors),
            "the round-2 defect was not reported:\n"
            + sp.report(violations, errors))

    def test_round_3_the_workflow_leaves_binding_the_side_themselves(self):
        """`substitute-candidates` re-resolved the side locally and let the
        hint select it: `team_id or viewer_team_id or game.home_team_id`."""
        leaked = _replace(_sources(), FACADE, """        team_id = self._workflow_side(game, team_id, viewer_role,
                                      viewer_team_id, _CANDIDATE_REFUSAL)""",
                          """        team_id = team_id or viewer_team_id or game.home_team_id""")
        violations, _errors = self._audit(leaked)
        self.assertTrue(
            self._caught(violations, "home_team_fallback",
                         "get_substitute_candidates"),
            "the round-3 home default was not reported:\n"
            + sp.report(violations, []))
        self.assertTrue(
            self._caught(violations, "untrusted_side",
                         "get_substitute_candidates"),
            "the round-3 hint was not reported:\n" + sp.report(violations, []))

    def test_round_4_the_dashboard_schedule_row(self):
        """THE ONE THIS ROUND FIXED, restored verbatim: the schedule loop
        called `compute_roster_status(g.id)` with no side at all."""
        leaked = _replace(_sources(), FACADE, """        own_side = game_scoped_own_team_id(role, scope, game, self.store)""",
                          """        own_side = None""")
        leaked = _replace(leaked, FACADE, """            "roster_status": self.roster.compute_roster_status(
                game.id, side).status.value,""",
                          """            "roster_status": self.roster.compute_roster_status(
                game.id).status.value,""")
        violations, _errors = self._audit(leaked)
        self.assertTrue(
            self._caught(violations, "untrusted_side",
                         "_schedule_roster_status"),
            "THE DEFECT THIS ROUND FIXED was not reported by the guard "
            "built to stop the next one:\n" + sp.report(violations, []))

    def test_round_1_get_roster_returning_the_whole_games_rows(self):
        """F1's OTHER half, and the one the previous version of this class
        never tested: ``get_roster`` returned ``roster_for_game`` — EVERY
        seated row in the game, both sides — to any caller the participation
        gate admitted. There is no side argument here for the provenance
        detector to classify, which is exactly why detector 4 exists."""
        leaked = case_adjudicated_reader_stops_projecting(_sources())
        violations, _errors = self._audit(leaked)
        self.assertTrue(
            self._caught(violations, "two_sided_class_broken", "get_roster"),
            "F1's raw two-sided store read was not reported:\n"
            + sp.report(violations, []))

    def test_round_1_get_substitutes_returning_the_whole_games_rows(self):
        """F3, the same shape on the substitute pool."""
        leaked = _replace(_sources(), FACADE, """        game = self.roster._require_game(game_id)
        audience = lineup_visibility.route_audience(
            viewer_role, viewer_team_id, game.home_team_id, game.away_team_id)
        if audience == lineup_visibility.FULL:
            return [_serialize(s)
                    for s in self.store.substitutes_for_game(game_id)]
        if audience == lineup_visibility.OWN_SIDE:
            return [_serialize(s)
                    for s in self.store.substitutes_for_game(game_id)
                    if s.team_id is not None and s.team_id == viewer_team_id]
        raise NotAuthorizedError(_SUBSTITUTE_REFUSAL)""",
                          """        self.roster._require_game(game_id)
        return [_serialize(s)
                for s in self.store.substitutes_for_game(game_id)]""")
        violations, _errors = self._audit(leaked)
        self.assertTrue(
            self._caught(violations, "two_sided_class_broken",
                         "get_substitutes"),
            "F3's raw two-sided store read was not reported:\n"
            + sp.report(violations, []))

    def test_round_6_player_home_taking_its_side_from_the_pointer(self):
        """THE FIFTH LEAK, restored verbatim — and the one an earlier version
        of this gate BLESSED, under the ``SUBJECT_OWN_SIDE`` exemption whose
        only condition was "takes no caller-supplied side". It takes none;
        it derived the OPPONENT's side from ``Player.team_id``."""
        leaked = _replace(_sources(), FACADE, """            my_team_id = game_scoped_own_team_id(
                Role.PLAYER, {"player_id": player_id}, next_game, self.store)""",
                          """            my_team_id = player.team_id""")
        violations, _errors = self._audit(leaked)
        self.assertTrue(
            self._caught(violations, "untrusted_side", "get_player_home"),
            "THE DEFECT THE COMPANION COMMIT FIXES was not reported by the "
            "guard rebuilt after it was missed:\n" + sp.report(violations, []))

    def test_the_ship_date_counterfactual_is_recorded(self):
        """WHAT THE ROUND-5 GATE WOULD HAVE DONE, stated as an assertion
        rather than as a claim in a commit message: with detector 4 removed,
        F1 and F3 stop being reported at all — which is the measurement that
        justifies detector 4 existing."""
        leaked = case_adjudicated_reader_stops_projecting(_sources())
        with self._neutered("two_sided"):
            violations, _errors = self._audit(leaked)
        self.assertEqual(
            [], [v for v in violations if v.function == "get_roster"],
            "with the two-sided detector removed, F1's raw store read was "
            "still reported — so the ship-date counterfactual this round "
            "rests on is not what it says it is: " + sp.report(violations, []))


# ---------------------------------------------------------------------------
# 7. EVERY CLASS CONDITION IS ENFORCED, ONE DEDICATED TEST EACH.
# ---------------------------------------------------------------------------
class EveryClassConditionIsEnforced(_GuardHarness, unittest.TestCase):
    """THE RULING'S "typed, documented design classifications with dedicated
    tests -- not accumulating exemptions".

    A class whose condition is never exercised is a rubber stamp, and a
    rubber stamp is how ``SUBJECT_OWN_SIDE`` blessed the fifth leak. Each
    test below breaks ONE class's condition and requires the gate to say so
    BY NAME."""

    def _condition_errors(self, klass, name, fn_source, path=FACADE,
                          origins=(), route=None):
        sources = _append_method(_sources(), path, fn_source)
        fn = sp._find_function(sources, path, name)
        self.assertIsNotNone(fn, f"the fixture method {name} did not parse")
        return sp._class_condition(klass, name, fn, None, sources,
                                   origins=set(origins), route=route)

    def test_subject_membership_context_rejects_the_permanent_pointer(self):
        """THE CHECK THE FIFTH LEAK NEEDED. The old class asked only whether
        a caller could NAME a side. This one asks where the derived side came
        from, and ``attr:player.team_id`` is not a resolved membership
        context."""
        errors = self._condition_errors(
            sp.SUBJECT_MEMBERSHIP_CONTEXT, "pretend_subject_read", '''
    def pretend_subject_read(self, player_id, game_id):
        """Resolves a context, then ignores it."""
        player = self.store.get_player(player_id)
        game = self.roster._require_game(game_id)
        self.roster.resolve_membership_context(game, player)
        return self.roster.compute_roster_status(
            game_id, player.team_id).to_dict()
''', origins=("attr:player.team_id",))
        self.assertTrue(
            any("permanent pointer" in e for e in errors),
            f"a side taken from the permanent pointer was accepted as a "
            f"'subject's own membership context': {errors}")

    def test_subject_membership_context_requires_a_real_resolution(self):
        errors = self._condition_errors(
            sp.SUBJECT_MEMBERSHIP_CONTEXT, "pretend_handed_context", '''
    def pretend_handed_context(self, player_id, game_id, ctx):
        """Never resolves anything: the context is handed in whole."""
        return self.roster.compute_roster_status(
            game_id, ctx.team_id).to_dict()
''', origins=("attr:ctx.team_id",))
        self.assertTrue(
            any("never resolves a membership context" in e for e in errors),
            errors)

    def test_subject_membership_context_requires_a_subject(self):
        errors = self._condition_errors(
            sp.SUBJECT_MEMBERSHIP_CONTEXT, "pretend_subjectless", '''
    def pretend_subjectless(self, game_id, ctx):
        """No subject at all."""
        self.roster.resolve_membership_context(None, None)
        return self.roster.compute_roster_status(
            game_id, ctx.team_id).to_dict()
''', origins=("attr:ctx.team_id",))
        self.assertTrue(any("names no SUBJECT" in e for e in errors), errors)

    def test_authorized_write_needs_authorization_on_the_path(self):
        errors = self._condition_errors(
            sp.AUTHORIZED_WRITE, "pretend_write", '''
    def pretend_write(self, game_id, team_id):
        """A write that takes a side and authorizes nothing."""
        return self.roster.compute_roster_status(game_id, team_id).to_dict()
''')
        self.assertTrue(any("AUTHORIZED_WRITE" in e for e in errors), errors)

    def test_operator_default_needs_an_audience_consulting_function(self):
        errors = self._condition_errors(
            sp.OPERATOR_DEFAULT, "pretend_operator_default", '''
    def pretend_operator_default(self, game_id, game):
        """Never consults an audience."""
        return self.roster.compute_roster_status(
            game_id, game.home_team_id).to_dict()
''')
        self.assertTrue(any("OPERATOR_DEFAULT" in e for e in errors), errors)

    def test_operator_only_route_is_tied_to_the_registrys_recorded_auth(self):
        self.assertEqual([], sp._route_is_operator_only(
            "get_scheduler_drafts", "x"))
        self.assertTrue(sp._route_is_operator_only("get_api_health", "x"))
        self.assertTrue(sp._route_is_operator_only("no_such_route", "x"))

    def test_durable_row_side_rejects_a_non_durable_origin(self):
        errors = self._condition_errors(
            sp.DURABLE_ROW_SIDE, "pretend_durable", '''
    def pretend_durable(self, game_id, player):
        """Claims the row's own side, reads the permanent pointer."""
        return self.roster.compute_roster_status(
            game_id, player.team_id).to_dict()
''', origins=("attr:player.team_id",))
        self.assertTrue(any("DURABLE_ROW_SIDE" in e for e in errors), errors)

    def test_live_membership_by_design_must_still_resolve_membership(self):
        errors = self._condition_errors(
            sp.LIVE_MEMBERSHIP_BY_DESIGN, "pretend_live", '''
    def pretend_live(self, game_id, team_id):
        """Claims to read live membership; reads nothing of the kind."""
        return list(self.store.players_for_team(team_id))
''')
        self.assertTrue(
            any("LIVE_MEMBERSHIP_BY_DESIGN" in e for e in errors), errors)

    def test_narrows_by_trusted_side_needs_a_side_to_narrow_by(self):
        """A declared producer that LOSES its side parameter narrows by
        nothing — and the registry, which still names it, would say it is
        fine."""
        sources = _replace(
            _sources(), "services/roster_service.py",
            '    def lineup_population(self, game, team_id: str) -> '
            'List["LineupRow"]:',
            '    def lineup_population(self, game) -> List["LineupRow"]:')
        fn = sp._find_function(sources, "services/roster_service.py",
                               "lineup_population")
        problems = sp._two_sided_condition(
            sp.NARROWS_BY_TRUSTED_SIDE, "services/roster_service.py", fn,
            sources)
        self.assertTrue(
            any("narrow BY" in p for p in problems),
            f"a producer with no side parameter still claimed to narrow by "
            f"one: {problems}")

    def test_out_of_band_tooling_is_checked_against_the_import_graph(self):
        """The demo seeder's class holds only while no serving module
        imports it."""
        sources = _sources()
        problems = sp._two_sided_condition(
            sp.OUT_OF_BAND_TOOLING, "demo.py",
            sp._find_function(sources, "demo.py", "main"), sources)
        self.assertEqual([], problems, problems)
        poisoned = dict(sources)
        poisoned[SERVER] = "from .. import demo\n" + sources[SERVER]
        problems = sp._two_sided_condition(
            sp.OUT_OF_BAND_TOOLING, "demo.py",
            sp._find_function(poisoned, "demo.py", "main"), poisoned)
        self.assertTrue(
            any("import(s) it" in p for p in problems),
            f"a seeding script a serving module imports still claimed to be "
            f"out of band: {problems}")

    def test_a_dormant_two_sided_entry_is_an_error(self):
        saved = sp.TWO_SIDED_READERS
        sp.TWO_SIDED_READERS = {
            **saved, ("api/service.py", "get_lineups"): (
                sp.NARROWS_BY_TRUSTED_SIDE, "stale")}
        try:
            sources = _sources()
            _v, usage = sp.audit_side_provenance(sources)
            _f, used_f = sp.audit_home_fallback(sources)
            usage["home_fallbacks"] = used_f
            _t, used_t = sp.audit_two_sided_store_reads(sources)
            usage["two_sided"] = used_t
            _d, leaves = sp.audit_dispatch(sources[SERVER])
            errors = sp.verify_registry_liveness(sources, usage, leaves)
        finally:
            sp.TWO_SIDED_READERS = saved
        self.assertTrue(
            any("TWO_SIDED_READERS" in e and "DORMANT" in e for e in errors),
            f"the two-sided registry can rot: {errors}")


# ---------------------------------------------------------------------------
# 8. MEASURED AGAINST THE REAL VULNERABLE TREES.
# ---------------------------------------------------------------------------
#: ``{commit: [(kind, function), …]}`` — what the rebuilt gate reports when
#: run over the REAL source at that commit, recovered with ``git archive``.
#: Not a text mutation of today's tree: these are the bytes that shipped.
#:
#: Liveness is OFF for these runs, deliberately. The registries describe
#: TODAY's package, so a liveness pass over a 2026-08 tree would report
#: dozens of "declares X, which has no such function" errors that say nothing
#: about whether the DEFECT is caught. What is asserted is only that the
#: DETECTORS report the defect.
VULNERABLE_TREES = {
    # F1 + F3 live: `/board` hard-codes home, and `get_roster` /
    # `get_substitutes` return the whole game's rows.
    "337374a": [("untrusted_side", "get_board"),
                ("two_sided_class_broken", "get_roster"),
                ("two_sided_class_broken", "get_substitutes"),
                ("untrusted_side", "get_player_home")],
    "23935a1": [("untrusted_side", "get_board"),
                ("two_sided_class_broken", "get_roster"),
                ("two_sided_class_broken", "get_substitutes"),
                ("untrusted_side", "get_player_home")],
    # F2: the availability-summary leaf takes its side from the query string.
    "2f8eb73": [("untrusted_side", "get_availability_summary"),
                ("untrusted_side", "get_player_home")],
    # F3's workflow leaves: `team_id or viewer_team_id or home`.
    "ae21c40": [("home_team_fallback", "get_substitute_candidates"),
                ("home_team_fallback", "get_addable_substitutes"),
                ("untrusted_side", "get_player_home")],
    # F4: `GET /api/demo/overview`, outside the family entirely.
    "56aa5dd": [("untrusted_side", "get_demo_overview"),
                ("untrusted_side", "get_player_home")],
    # F5, alone, at the tip this round started from.
    "b1cc02d": [("untrusted_side", "get_player_home")],
}


class TheGuardIsMeasuredAgainstTheRealVulnerableTrees(_GuardHarness,
                                                      unittest.TestCase):
    """RECONSTRUCTED FROM REAL SOURCES, not by text-mutating today's tree.

    Every other class here edits the CURRENT source to look like a past
    defect, which proves the detector fires on that SHAPE. This one runs the
    detectors over the actual bytes of the vulnerable commits, which proves
    it fires on what actually shipped — including the ship-date
    counterfactual the previous commit message asserted without testing.

    NOT SILENTLY SKIPPED. These are commits of this branch; if the history
    has been rewritten or the checkout is shallow they cannot be recovered,
    and this test SAYS SO LOUDLY rather than passing quietly. A skip is not a
    pass."""

    def _sources_at(self, sha):
        import shutil
        import subprocess
        import tempfile
        root = Path(__file__).resolve().parents[3]
        probe = subprocess.run(
            ["git", "-C", str(root), "cat-file", "-e", f"{sha}^{{commit}}"],
            capture_output=True)
        if probe.returncode != 0:
            return None, None
        archive = subprocess.run(
            ["git", "-C", str(root), "archive", sha,
             "hockey-scheduler/backend/hockey_scheduler"],
            capture_output=True)
        if archive.returncode != 0:
            return None, None
        tmp = tempfile.mkdtemp()
        subprocess.run(["tar", "-x", "-C", tmp], input=archive.stdout,
                       check=True)
        pkg = Path(tmp) / "hockey-scheduler/backend/hockey_scheduler"
        return sp.package_sources(pkg), (lambda: shutil.rmtree(tmp))

    def test_every_shipped_defect_is_reported_at_the_tree_that_shipped_it(self):
        checked = []
        for sha, expected in sorted(VULNERABLE_TREES.items()):
            sources, cleanup = self._sources_at(sha)
            if sources is None:
                print(f"\n[SIDE PROVENANCE HISTORY] commit {sha} is not in "
                      f"this checkout, so the guard was NOT measured against "
                      f"the real source that shipped the defect. The recorded "
                      f"measurement is in the [#205] guard commit message. "
                      f"A SKIP IS NOT A PASS.")
                continue
            try:
                violations, _errors = sp.audit(sources=sources,
                                               verify_liveness=False)
                found = {(v.kind, v.function) for v in violations}
                for pair in expected:
                    with self.subTest(commit=sha, defect=pair):
                        self.assertIn(
                            pair, found,
                            f"{sha}: the guard did NOT report {pair} against "
                            f"the real source that shipped it. Reported: "
                            f"{sorted(found)}")
                checked.append(sha)
            finally:
                cleanup()
        if checked:
            self.assertEqual(sorted(checked), sorted(VULNERABLE_TREES),
                             "some vulnerable trees were not measured")

    def test_the_fifth_leak_is_reported_at_every_tree_that_carried_it(self):
        """F5 was live for the WHOLE blocker and no round noticed. The gate
        that missed it now reports it at every one of these trees — which is
        the sharpest available statement that this rebuild is not just a
        patch over the last symptom."""
        for sha in sorted(VULNERABLE_TREES):
            sources, cleanup = self._sources_at(sha)
            if sources is None:
                continue
            try:
                violations, _errors = sp.audit(sources=sources,
                                               verify_liveness=False)
                with self.subTest(commit=sha):
                    self.assertTrue(
                        self._caught(violations, "untrusted_side",
                                     "get_player_home"),
                        f"{sha}: the fifth leak was live here and was not "
                        f"reported")
            finally:
                cleanup()



if __name__ == "__main__":
    unittest.main()
