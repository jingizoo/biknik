"""THE GATE (#202 step 1): the registry and the live dispatch must agree.

Two asymmetries fail CI, and each names the offending entries:

  UNCLASSIFIED  a live dispatch branch with no ``RouteSpec``
  DEAD          a ``RouteSpec`` matching no live dispatch branch

"Live" is not a second hand-written list: ``route_extract`` parses
``web/server.py`` and reports the branches the dispatch actually contains, so
adding a route without registering it fails, and registering a route that was
deleted fails. See ``test_route_extract.py`` for the extractor's own proof that
it finds every branch shape and refuses the ones it cannot read.

``CONTEXT_SCOPED_READ_ROUTES`` in ``server.py`` is still a separate,
hand-maintained table, CROSS-CHECKED here and otherwise left exactly as it is
-- rewiring it is separate, later #202 work (enforcement, not admission).

``_GET_ROUTES``/``_POST_ROUTES`` were the same kind of hand-maintained table
through the #202 routespec-inventory step; the #202 WIRING step replaced
their SOURCE with a live derivation from this registry (every ``kind="route"``
entry scoped to ``/api/`` -- see server.py's own comment), so what this file
checks for them now is that the derivation still reproduces their exact
PRE-EXISTING scope (``MethodTableNarrowingTests`` below), plus two structural
invariants ``kind`` itself needs now that it is load-bearing
(``KindClassificationTests``) -- not that a hand-written list happens to agree
with the parser, which was the old question and no longer applies.
"""

import re
import unittest

from helpers import BACKEND  # noqa: F401  (ensures sys.path is set up)

from hockey_scheduler.web import server as srv
from hockey_scheduler.web.route_extract import (
    SERVER_PATH, extract_walker, sample_path, templates_of_pattern,
)
from hockey_scheduler.web.route_registry import (
    BY_KEY, BY_NAME, REGISTRY, UNCLASSIFIED,
)

WALKER = extract_walker()
LIVE = {route.key: route for route in WALKER.routes.values()}


def _describe(keys, source):
    return "\n".join(f"  {method:5s} {template}   ({source})"
                     for method, template in sorted(keys))


class RegistryCoversTheDispatchTests(unittest.TestCase):
    maxDiff = None

    def test_no_unclassified_dispatch_branch(self):
        """Every live (method, path) the dispatch selects has a RouteSpec."""
        missing = set(LIVE) - set(BY_KEY)
        self.assertEqual(missing, set(), "\n\nUNCLASSIFIED — these dispatch "
                         "branches have no RouteSpec in route_registry.py:\n"
                         + "\n".join(
                             f"  {LIVE[key].method:5s} {LIVE[key].template}"
                             f"   ({LIVE[key].handler}:{LIVE[key].lineno}, "
                             f"{LIVE[key].shape}: {LIVE[key].test})"
                             for key in sorted(missing)))

    def test_no_dead_route_spec(self):
        """Every RouteSpec matches a branch that is still in the dispatch."""
        dead = set(BY_KEY) - set(LIVE)
        self.assertEqual(dead, set(), "\n\nDEAD — these RouteSpecs match no "
                         "live dispatch branch in web/server.py:\n"
                         + "\n".join(
                             f"  {BY_KEY[key].method:5s} {BY_KEY[key].template}"
                             f"   ({BY_KEY[key].name}, declared handler "
                             f"{BY_KEY[key].handler})"
                             for key in sorted(dead)))

    def test_declared_handler_is_where_the_branch_lives(self):
        """``handler`` is verified, so it cannot rot into decoration."""
        wrong = [(spec.name, spec.handler, LIVE[spec.key].handler)
                 for spec in REGISTRY
                 if spec.key in LIVE and spec.handler != LIVE[spec.key].handler]
        self.assertEqual(wrong, [], "\n\nRouteSpec.handler disagrees with the "
                                    "dispatch (name, declared, actual)")

    def test_counts(self):
        """A visible total, so a silent halving of the inventory is not silent.

        #202 repair: 74 GET -> 75 (root cause 6, the static tail: +1) and
        163 POST -> 164 (root cause 1: -12 assign-\\w+ wildcard families +
        13 concrete combo leaves = +1).
        """
        self.assertEqual(len(REGISTRY), len(LIVE))
        self.assertEqual(sum(1 for s in REGISTRY if s.method == "GET"), 75)
        self.assertEqual(sum(1 for s in REGISTRY if s.method == "POST"), 164)


class RegistryInternalConsistencyTests(unittest.TestCase):
    maxDiff = None

    def test_keys_and_names_are_unique(self):
        self.assertEqual(len(BY_KEY), len(REGISTRY))
        self.assertEqual(len(BY_NAME), len(REGISTRY))

    def test_pattern_expands_to_exactly_its_template(self):
        """The regex and the canonical template are two views of ONE route.

        Without this a spec could carry a pattern that matches something else
        entirely and still pass the template comparison above.
        """
        wrong = [(spec.name, spec.pattern, spec.template,
                  templates_of_pattern(spec.pattern))
                 for spec in REGISTRY
                 if templates_of_pattern(spec.pattern) != [spec.template]]
        self.assertEqual(wrong, [])

    def test_pattern_matches_a_sample_of_its_own_template(self):
        for spec in REGISTRY:
            with self.subTest(spec=spec.name):
                self.assertRegex(sample_path(spec.template), spec.pattern)

    # #202 classification is landing in independent, reviewed batches (each
    # batch's own ``note`` on its entries carries the file:line citation), so
    # this can no longer pin the WHOLE registry empty the way it once did — by
    # this batch's own turn other batches (e.g. the games/notifications/
    # standings leaves) already carry values too. What stays checkable
    # without knowing every other batch in advance:
    #   * a spec never carries HALF a classification (auth filled, scope_axis
    #     still UNCLASSIFIED, or vice versa) — that is exactly the
    #     "half-populated policy field reads as authority" failure mode the
    #     original guard existed to catch, and it is still checkable per-spec;
    #   * every filled value is one of the axis's own declared classes, not a
    #     typo or an invented one;
    #   * THIS batch's own leaves (static files + the /calendar/*.ics leaf)
    #     carry exactly what their notes claim: auth="none", scope_axis="none";
    #   * ``get_empty_path`` — the impossible fallback (unreachable over HTTP,
    #     see its note) — is deliberately EXCLUDED and stays UNCLASSIFIED.
    # Nothing yet READS any of these fields (enforcement is the separate,
    # later #202 PR the classification PR's body describes).
    _THIS_BATCH_NONE_NONE = frozenset({
        "get_index", "get_mobile_shell", "get_mobile_shell_slash",
        "get_setup_shell", "get_setup_shell_slash", "get_static_tail",
        "get_calendar_division_id_ics", "get_calendar_official_id_ics",
        "get_calendar_player_id_ics", "get_calendar_team_id_ics",
    })
    _VALID_SCOPE_AXES = frozenset({
        "zero_axis", "program", "season", "league", "cross", "none",
        UNCLASSIFIED,
    })

    def test_classification_slots_are_still_empty(self):
        """``auth``/``scope_axis`` are declared slots, filled in batches.

        See the block comment above for what this can and cannot pin now that
        classification lands batch-by-batch rather than all at once.
        """
        half_filled = [(s.name, s.auth, s.scope_axis) for s in REGISTRY
                        if (s.auth != UNCLASSIFIED)
                        != (s.scope_axis != UNCLASSIFIED)]
        self.assertEqual(half_filled, [], "\n\na spec must be classified on "
                         "BOTH auth and scope_axis, or neither")

        bad_axis = [(s.name, s.scope_axis) for s in REGISTRY
                    if s.scope_axis not in self._VALID_SCOPE_AXES]
        self.assertEqual(bad_axis, [])

        by_name = {s.name: (s.auth, s.scope_axis) for s in REGISTRY}
        for name in self._THIS_BATCH_NONE_NONE:
            with self.subTest(name=name):
                self.assertEqual(by_name[name], ("none", "none"))

        self.assertEqual(by_name["get_empty_path"],
                         (UNCLASSIFIED, UNCLASSIFIED),
                         "get_empty_path is an impossible fallback shape "
                         "(unreachable over HTTP) and must stay excluded "
                         "from classification, not guessed at")

    def test_the_registry_is_now_wired_not_inert(self):
        """server.py DOES import the registry now -- the #202 wiring step.

        Through the routespec-inventory step this test asserted the OPPOSITE
        (``assertNotIn``): that nothing read the registry, which is what made
        "no behaviour change" checkable rather than claimed for THAT step.
        The wiring step's whole point is to stop that being true -- RouteSpec
        is now the 405/Allow admission source (server.py's ``_GET_ROUTES``/
        ``_POST_ROUTES``) -- so what is worth pinning now is that the import
        is real and did not quietly get reverted, not that it is absent.
        """
        self.assertIn("from .route_registry import REGISTRY",
                      SERVER_PATH.read_text())


class DispatchHasNoDeadBranchesTests(unittest.TestCase):
    def test_no_unreachable_nested_branch(self):
        """A nested branch no live shape can reach is dead code in the dispatch.

        (e.g. ``if sub == "roster"`` under a regex whose alternation has no
        ``roster``.) The walker collects these; there are none today.
        """
        self.assertEqual(
            [f"{handler}:{lineno}  {test}"
             for handler, lineno, test in WALKER.unreachable], [])


# --------------------------------------------------------------------------- #
# Cross-checks against server.py's three method/scope tables. Assertions      #
# only -- none of this rewires or deletes them; ``_GET_ROUTES``/              #
# ``_POST_ROUTES`` are already wired (they ARE a REGISTRY derivation, see     #
# server.py), ``CONTEXT_SCOPED_READ_ROUTES`` still is not (separate, later    #
# #202 work).                                                                 #
# --------------------------------------------------------------------------- #
TABLES = (("_GET_ROUTES", srv._GET_ROUTES, "GET"),
          ("_POST_ROUTES", srv._POST_ROUTES, "POST"),
          ("CONTEXT_SCOPED_READ_ROUTES", srv.CONTEXT_SCOPED_READ_ROUTES, "GET"))

COMPILED = {method: [(spec, re.compile(spec.pattern)) for spec in REGISTRY
                     if spec.method == method]
            for method in ("GET", "POST")}


class TableCrossCheckTests(unittest.TestCase):
    """Every path either table claims must be a route this registry knows.

    A pattern in the 405 table or the scoped-read table with no corresponding
    RouteSpec means that table has drifted away from the dispatch — the exact
    failure #202 exists to make impossible.
    """

    maxDiff = None

    def test_every_table_pattern_has_a_route_spec(self):
        orphans = []
        for label, table, method in TABLES:
            for rx in table:
                for template in templates_of_pattern(rx.pattern):
                    probe = sample_path(template)
                    if not any(compiled.match(probe)
                               for _, compiled in COMPILED[method]):
                        orphans.append(f"{label}: {method} {template} "
                                       f"(from {rx.pattern})")
        self.assertEqual(orphans, [])

    def test_context_scoped_reads_are_all_get_routes(self):
        for rx in srv.CONTEXT_SCOPED_READ_ROUTES:
            for template in templates_of_pattern(rx.pattern):
                with self.subTest(template=template):
                    self.assertIn(("GET", template), BY_KEY)

    def test_is_context_scoped_read_agrees_with_the_registry(self):
        """The predicate the dispatch actually calls, exercised on real paths."""
        scoped = set()
        for rx in srv.CONTEXT_SCOPED_READ_ROUTES:
            scoped.update(templates_of_pattern(rx.pattern))
        for spec in REGISTRY:
            if spec.method != "GET" or spec.kind != "route":
                continue
            with self.subTest(spec=spec.name):
                self.assertEqual(srv.is_context_scoped_read(
                    sample_path(spec.template)), spec.template in scoped)


class MethodTableNarrowingTests(unittest.TestCase):
    """The OTHER direction, pinned rather than asserted-correct.

    ``_GET_ROUTES``/``_POST_ROUTES`` are deliberately narrower than the full
    registry -- their own derivation filter (server.py: ``kind == "route"``
    and the pattern scoped to ``/api/``) excludes some live routes on
    purpose, so "every route is in the table" is NOT true today and forcing
    it would be a behaviour change (#202 wiring step: proven byte-for-byte
    via HTTP diffing that this exact narrowing survived the hand-written
    table's replacement, not merely reasoned about). What must not happen is
    a NEW divergence appearing unnoticed — for POST that is severe, because
    ``do_POST`` refuses anything ``_supported_methods`` does not admit BEFORE
    the dispatch chain runs, so a live POST branch whose spec stops being
    admitted (e.g. by a ``kind`` retag -- see ``KindClassificationTests``) is
    unreachable code that answers 404.

    So the current divergence is pinned exactly. Each entry below is a fact
    about today's derivation filter, not an endorsement.
    """

    maxDiff = None

    #: Live GET routes ``_GET_ROUTES`` does not admit. Consequence: their
    #: ``_supported_methods`` is empty, so ``OPTIONS`` on them answers 404
    #: instead of 204+Allow and a PUT/DELETE answers 404 instead of 405+Allow.
    #: The static shells, /favicon.ico, and the four /calendar feeds are all
    #: excluded by the ``/api/`` prefix filter (kind="route", but not scoped
    #: to /api/); the calendar hole is REAL and reported as a finding of the
    #: #202 routespec-inventory step, not fixed here (no behaviour changes --
    #: closing it is enforcement, separate later work). ``/{*}``/``/api/{*0}``
    #: would join this set too on prefix alone, but are ALSO excluded by
    #: ``kind`` (static/fallthrough, never "route") -- belt and suspenders.
    GET_NOT_IN_TABLE = {
        "", "/", "/favicon.ico", "/mobile", "/mobile/", "/setup", "/setup/",
        "/api/{*0}", "/{*}",
        "/calendar/division/{}.ics", "/calendar/official/{}.ics",
        "/calendar/player/{}.ics", "/calendar/team/{}.ics",
    }

    #: Live POST branches ``_POST_ROUTES`` does not admit.
    #:
    #: #202 repair root cause 1: this set SHRANK from 12 entries to 1. The 12
    #: assign-\w+ WILDCARD templates that used to be here are gone -- they
    #: were never real leaves, and their replacement (the 13 CONCRETE combo
    #: templates _handle_reassign's own schema admits) are all kind="route"
    #: and /api/-scoped, so the #202 wiring step's derivation admits them
    #: automatically -- proof, independent of this registry, that the
    #: concrete leaves (and not the wildcard) were always the intended
    #: reachable set: whoever wrote the ORIGINAL 405 table by hand had
    #: already worked out the real combos, and the OLD registry disagreed
    #: with its own neighbour table without either side ever being checked
    #: against the other. Only the games family remains excluded, now by
    #: ``kind == "family"`` rather than simply not being hand-transcribed: it
    #: matches ANY subpath, including nonexistent ones (the real actions are
    #: each their own kind="route" spec, and each IS admitted).
    POST_NOT_IN_TABLE = {
        "/api/games/{}/{*}",
    }

    def _unadmitted(self, table, method):
        return {spec.template for spec in REGISTRY if spec.method == method
                and not any(rx.match(sample_path(spec.template))
                            for rx in table)}

    def test_get_table_omissions_are_the_known_set(self):
        self.assertEqual(self._unadmitted(srv._GET_ROUTES, "GET"),
                         self.GET_NOT_IN_TABLE)

    def test_post_table_omissions_are_the_known_set(self):
        self.assertEqual(self._unadmitted(srv._POST_ROUTES, "POST"),
                         self.POST_NOT_IN_TABLE)

    def test_the_calendar_feed_hole_is_real_and_pinned(self):
        """The finding above, exercised through the code that has the bug.

        ``_supported_methods`` sees no methods for a live ICS feed path, which
        is why ``OPTIONS``/``PUT`` on it answer 404 rather than 204/405. Pinned
        so the day someone widens the ``/api/`` prefix filter (or otherwise
        starts admitting ``/calendar/...``) in server.py's ``_GET_ROUTES``,
        this test tells them the hole is closed instead of silently passing.
        """
        feed = sample_path("/calendar/team/{}.ics")
        self.assertFalse(any(rx.match(feed) for rx in srv._GET_ROUTES))
        self.assertTrue(any(re.compile(spec.pattern).match(feed)
                            for spec in REGISTRY if spec.method == "GET"))


class KindClassificationTests(unittest.TestCase):
    """#202 wiring step, go-beyond finding: ``kind`` is now LOAD-BEARING.

    server.py's ``_GET_ROUTES``/``_POST_ROUTES`` admit exactly the
    ``kind == "route"`` specs (scoped to ``/api/``) -- but
    ``RegistryCoversTheDispatchTests`` above only ever compares ``(method,
    template)`` SET MEMBERSHIP between the registry and the live dispatch; it
    never looks at ``kind``. So retagging a genuine concrete leaf from
    ``"route"`` to ``"static"``/``"fallthrough"``/``"family"`` -- or the
    reverse, retagging the games family or the static tail TO ``"route"`` --
    would sail through every existing test in this file: the (method,
    template) key is unchanged, only its ``kind`` moved, and nothing checked
    that. The live consequence is real either way: mistag a concrete POST
    leaf as non-"route" and it silently stops being admitted (a 405/Allow
    regression, exactly the shape ``MethodTableNarrowingTests`` pins for the
    entries that ARE deliberately excluded); mistag ``post_games_id_action``
    (or the static tail) TO "route" and ``_GET_ROUTES``/``_POST_ROUTES``
    would over-claim every path under it, including nonexistent game actions
    and every static path on the wire -- the OPPOSITE failure from the one
    #202's post-merge review found (a wildcard silently standing in for a
    finite set), and just as invisible to the dispatch-vs-registry gate.

    These two invariants close that gap independently of the hand-typed
    label itself.
    """

    #: Every non-``"route"`` RouteSpec, pinned by (method, name, kind) --
    #: exactly like ``_AUDIT_WAIVERS`` in route_extract.py, retagging one is
    #: now a conspicuous, reviewed diff line instead of a silent set-member
    #: move nothing here would otherwise notice.
    NON_ROUTE_KINDS = {
        ("GET", "get_empty_path", "static"),
        ("GET", "get_index", "static"),
        ("GET", "get_api_unmatched", "fallthrough"),
        ("GET", "get_mobile_shell", "static"),
        ("GET", "get_mobile_shell_slash", "static"),
        ("GET", "get_setup_shell", "static"),
        ("GET", "get_setup_shell_slash", "static"),
        ("GET", "get_static_tail", "static"),
        ("POST", "post_games_id_action", "family"),
    }

    def test_every_non_route_kind_is_exactly_the_pinned_set(self):
        actual = {(s.method, s.name, s.kind) for s in REGISTRY
                 if s.kind != "route"}
        self.assertEqual(actual, self.NON_ROUTE_KINDS)

    def test_no_route_kind_spec_carries_a_free_tail_token(self):
        """A free, unbounded tail (``{*}``/``{*0}``) is exactly what makes a
        family/fallthrough/the static tail NOT a single concrete leaf --
        #202 repair root cause 1's own lesson, that a wildcard is not a real
        route on its own. Every entry that carries one today IS one of the
        three non-"route" kinds (this is what makes them non-"route" in the
        first place); this pins the converse too, mechanically, independent
        of ``kind`` -- so a future ``kind="route"`` spec that reintroduces a
        free tail (which WOULD be silently admitted and over-claim every
        path under it) fails here even if someone forgets to update
        ``NON_ROUTE_KINDS`` at all.
        """
        offenders = [spec.name for spec in REGISTRY if spec.kind == "route"
                    and ("{*}" in spec.template or "{*0}" in spec.template)]
        self.assertEqual(offenders, [])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
