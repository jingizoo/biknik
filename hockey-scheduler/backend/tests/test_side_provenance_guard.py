"""#205, ROUND 4 — the STRUCTURAL guard, and its own falsification.

FOUR ROUNDS, FOUR LEAKS, EACH FOUND BY HAND: three sibling routes, then the
fifth leaf, then the candidates path, then a route outside the family
entirely. Every one was the same shape — a private-state read reaching a side
by default or by a client hint instead of by the server's trusted resolution.
Finding the fifth that way is not a strategy, so
``services/side_provenance.py`` fails the build instead, and this file is
what makes that claim checkable.

THE ORDER THIS WAS BUILT IN, because it is the only order that proves
anything: the adversarial cases below were written FIRST and confirmed to
pass against the un-guarded tree (there was no guard, so every one of them
was accepted silently). The gate was then built until each is caught. What
is pinned here is therefore not "the guard runs" but "the guard catches these
exact regressions and does not catch these exact legitimate shapes".

WHAT EACH CLASS PROVES
======================
:class:`TheGuardIsCleanOnHead`
    The whole package passes, with an EMPTY unclassified ledger. Runs first
    so a later red is a real regression, not accumulated noise.

:class:`ANewLeakFailsTheBuild`
    A dozen adversarial mutations of the REAL source. Each asserts the kind
    AND the site, so a guard that fires for some other reason does not count
    as catching it.

:class:`TheGuardDoesNotFightLegitimateCases`
    The alias case (the trusted side through a local name) is NOT flagged,
    which is what proves the check is provenance and not spelling; and every
    documented exemption's machine condition really is enforced.

:class:`TheRegistriesCannotRot`
    Monotonic shrink, per registry: a dormant entry, a renamed producer, an
    adjudicator that stopped adjudicating, and an ambiguous declaration are
    each an ERROR.

:class:`TheGuardsOwnTestsCanFail`
    THE POINT OF THE WHOLE EXERCISE. Each detector is NEUTERED in turn and
    the adversarial cases are required to go green — a guard whose own tests
    cannot fail is exactly the thing this task exists to prevent.

:class:`TheGuardCatchesTheFourLeaksThisBlockerFixed`
    The strongest evidence available: all four of the defects that actually
    shipped and were actually found by hand, reconstructed in the live
    source and required to be reported. Two are caught by the provenance
    detector and two by the registry-liveness check.

NO SOCKET, NO STORE, NO BACKEND: this is a source-level contract, so it is
the same on Memory, SQLite and PostgreSQL by construction. The BEHAVIOUR the
guard protects is pinned tri-store over authenticated HTTP in
``test_overview_schedule_side.py`` and ``test_private_game_sibling_routes.py``.
"""

import contextlib
import unittest

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
                               "side_omitted_entirely"),
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
# 6. THE FOUR LEAKS THIS BLOCKER ACTUALLY FIXED.
# ---------------------------------------------------------------------------
class TheGuardCatchesTheFourLeaksThisBlockerFixed(_GuardHarness,
                                                  unittest.TestCase):
    """The claim the whole exercise rests on, made checkable.

    Each round's real defect is RECONSTRUCTED in the live source and the gate
    is required to report it. This is a stronger statement than the synthetic
    adversarial cases above: these are not shapes chosen to be catchable,
    they are the four shapes that actually shipped and were actually found by
    hand, one round at a time.

    TWO of them are caught by the PROVENANCE detector and two by the
    REGISTRY-LIVENESS check, which is the design working as intended rather
    than a gap: deleting a reader's audience test does not change any call
    site's provenance, so a gate that only looked at call sites would have
    missed it. The declaration that reader carries is what notices."""

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


if __name__ == "__main__":
    unittest.main()
