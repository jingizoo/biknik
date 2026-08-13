"""THE GATE (#202 step 1): the registry and the live dispatch must agree.

Two asymmetries fail CI, and each names the offending entries:

  UNCLASSIFIED  a live dispatch branch with no ``RouteSpec``
  DEAD          a ``RouteSpec`` matching no live dispatch branch

"Live" is not a second hand-written list: ``route_extract`` parses
``web/server.py`` and reports the branches the dispatch actually contains, so
adding a route without registering it fails, and registering a route that was
deleted fails. See ``test_route_extract.py`` for the extractor's own proof that
it finds every branch shape and refuses the ones it cannot read.

The three hand-maintained tables in ``server.py`` (``_GET_ROUTES``,
``_POST_ROUTES``, ``CONTEXT_SCOPED_READ_ROUTES``) are CROSS-CHECKED here and
otherwise left exactly as they are: replacing them is a later step of #202, and
this one changes no behaviour.
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
        """A visible total, so a silent halving of the inventory is not silent."""
        self.assertEqual(len(REGISTRY), len(LIVE))
        self.assertEqual(sum(1 for s in REGISTRY if s.method == "GET"), 74)
        self.assertEqual(sum(1 for s in REGISTRY if s.method == "POST"), 163)


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

    def test_classification_slots_are_still_empty(self):
        """``auth``/``scope_axis`` are declared slots for a LATER step.

        Pinned empty on purpose: a half-populated policy field reads as
        authority and gets believed. Whoever fills these in must arrive with the
        code that reads them, and will have to change this test to do it.
        """
        filled = [(s.name, s.auth, s.scope_axis) for s in REGISTRY
                  if s.auth != UNCLASSIFIED or s.scope_axis != UNCLASSIFIED]
        self.assertEqual(filled, [])

    def test_the_registry_is_inert(self):
        """server.py must not import the registry in this step.

        This is what makes "no behaviour change" checkable rather than claimed:
        an inventory nothing reads cannot alter a single response.
        """
        self.assertNotIn("route_registry", SERVER_PATH.read_text())


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
# Cross-checks against the three hand-maintained tables (#202: assertions       #
# only — this step does NOT rewire or delete them).                            #
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

    ``_GET_ROUTES``/``_POST_ROUTES`` are deliberately narrower than the dispatch
    in places, so "every route is in the table" is NOT true today and forcing it
    would be a behaviour change. What must not happen is a NEW divergence
    appearing unnoticed — for POST that is severe, because ``do_POST`` refuses
    anything ``_supported_methods`` does not admit BEFORE the dispatch chain
    runs, so a new POST branch missing from ``_POST_ROUTES`` is unreachable
    code that answers 404.

    So the current divergence is pinned exactly. Each entry below is a fact
    about today's tables, not an endorsement.
    """

    maxDiff = None

    #: Live GET routes ``_GET_ROUTES`` does not list. Consequence: their
    #: ``_supported_methods`` is empty, so ``OPTIONS`` on them answers 404
    #: instead of 204+Allow and a PUT/DELETE answers 404 instead of 405+Allow.
    #: The static shells and /favicon.ico are outside that table's declared
    #: scope; the four /calendar feeds are a REAL hole and are reported as a
    #: finding of this step, not fixed here (no behaviour changes).
    GET_NOT_IN_TABLE = {
        "", "/", "/favicon.ico", "/mobile", "/mobile/", "/setup", "/setup/",
        "/api/{*}",
        "/calendar/division/{}.ics", "/calendar/official/{}.ics",
        "/calendar/player/{}.ics", "/calendar/team/{}.ics",
    }

    #: Live POST branches ``_POST_ROUTES`` does not admit as written. All are
    #: branches BROADER than the real routes under them, and the table is
    #: right to be narrower: the game family regex matches any subpath (the
    #: real actions are listed individually), and ``assign-(\w+)`` matches any
    #: word where only the combos in ``_handle_reassign``'s table exist.
    POST_NOT_IN_TABLE = {
        "/api/games/{}/{*}",
        "/api/setup/division/{}/assign-{}", "/api/setup/league/{}/assign-{}",
        "/api/setup/player/{}/assign-{}", "/api/setup/rink/{}/assign-{}",
        "/api/setup/team/{}/assign-{}", "/api/setup/venue/{}/assign-{}",
        "/api/v2/setup/division/{}/assign-{}",
        "/api/v2/setup/player/{}/assign-{}",
        "/api/v2/setup/program/{}/assign-{}",
        "/api/v2/setup/rink/{}/assign-{}", "/api/v2/setup/team/{}/assign-{}",
        "/api/v2/setup/venue/{}/assign-{}",
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
        so the day someone lists /calendar in _GET_ROUTES, this test tells them
        the hole is closed instead of silently passing.
        """
        feed = sample_path("/calendar/team/{}.ics")
        self.assertFalse(any(rx.match(feed) for rx in srv._GET_ROUTES))
        self.assertTrue(any(re.compile(spec.pattern).match(feed)
                            for spec in REGISTRY if spec.method == "GET"))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
