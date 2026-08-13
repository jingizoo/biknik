"""The route extractor's own tests (#202 step 1).

The registry gate is only as trustworthy as the thing it compares against, so
the extractor is tested on a SYNTHETIC dispatch that contains every branch
shape ``server.py`` uses — and on shapes it must REFUSE. Testing it only
against the real file would prove nothing about a shape someone adds tomorrow:
the extractor would either see it or quietly not, and "quietly not" is the
failure this whole step exists to prevent.

Three properties, each with a falsifying case:

1. every shape present is FOUND (a fixture with one of each, asserted route by
   route);
2. every shape absent is REFUSED, loudly (``ExtractionError``), never skipped;
3. a nested branch no live shape can reach is REPORTED as unreachable rather
   than being emitted as a route.
"""

import textwrap
import unittest

from helpers import BACKEND  # noqa: F401  (ensures sys.path is set up)

from hockey_scheduler.web.route_extract import (
    ExtractionError, expand_pattern, extract_routes, extract_walker,
    sample_path, templates_of_pattern,
)


def _module(body: str) -> str:
    """Wrap a dispatch body in the minimum Handler class the walker needs.

    Both entry points must exist, so a fixture that exercises one verb gets an
    empty stub for the other.
    """
    body = textwrap.dedent(body)
    for entry in ("do_GET", "do_POST"):
        if f"def {entry}(" not in body:
            body += f"\ndef {entry}(self):\n    return None\n"
    return "class Handler:\n" + textwrap.indent(body, "    ")


# --------------------------------------------------------------------------- #
# The regex subset -> canonical templates                                      #
# --------------------------------------------------------------------------- #
class PatternExpansionTests(unittest.TestCase):
    def test_literal_and_segment(self):
        self.assertEqual(templates_of_pattern(r"^/api/players$"),
                         ["/api/players"])
        self.assertEqual(templates_of_pattern(r"^/api/games/([^/]+)$"),
                         ["/api/games/{}"])

    def test_alternation_expands_to_one_template_each(self):
        self.assertEqual(
            templates_of_pattern(r"^/api/demo/(?:reset|load|clear)$"),
            ["/api/demo/reset", "/api/demo/load", "/api/demo/clear"])

    def test_optional_group_expands_both_ways(self):
        self.assertEqual(
            templates_of_pattern(r"^/api/games/([^/]+)(?:/(board|lineups))?$"),
            ["/api/games/{}/board", "/api/games/{}/lineups", "/api/games/{}"])

    def test_free_tail_and_word_segment_and_escaped_dot(self):
        self.assertEqual(templates_of_pattern(r"^/api/games/([^/]+)/(.+)$"),
                         ["/api/games/{}/{*}"])
        self.assertEqual(templates_of_pattern(r"^/x/([^/]+)/assign-(\w+)$"),
                         ["/x/{}/assign-{}"])
        self.assertEqual(templates_of_pattern(r"^/calendar/(team)/([^/]+)\.ics$"),
                         ["/calendar/team/{}.ics"])

    def test_capture_group_numbering_follows_python(self):
        # group 2 is the alternation, group 1 the segment - the walker relies on
        # this to know which piece m.group(2) names.
        expansions = expand_pattern(r"^/a/([^/]+)(?:/(b|c))?$")
        groups = [sorted({p.group for p in e.parts if p.group}) for e in expansions]
        self.assertEqual(groups, [[1, 2], [1, 2], [1]])

    def test_unsupported_constructs_raise(self):
        for pattern in (r"^/a/(?P<x>[^/]+)$",     # named group
                        r"^/a/[a-z]+$",            # character class
                        r"^/a/x{2}$",              # repetition
                        r"^/a/\d+$",               # unsupported escape
                        r"/a/b$",                  # unanchored start
                        r"^/a/b"):                 # unanchored end
            with self.subTest(pattern=pattern):
                with self.assertRaises(ExtractionError):
                    templates_of_pattern(pattern)

    def test_sample_path_is_concrete(self):
        self.assertEqual(sample_path("/api/games/{}/substitutes/{}/offer"),
                         "/api/games/sample1/substitutes/sample2/offer")


# --------------------------------------------------------------------------- #
# Every branch shape present is found                                          #
# --------------------------------------------------------------------------- #
EVERY_SHAPE = _module('''
    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/api/literal":
            return self._send(1)
        if path in ("/api/one", "/api/two"):
            if path == "/api/one":
                return self._send(2)
            return self._send(3)
        mid = re.match(r"^/api/thing/([^/]+)$", path)
        if mid:
            return self._send(4)
        game = re.match(r"^/api/games/([^/]+)(?:/(board|lineups))?$", path)
        if game:
            gid, sub = game.group(1), game.group(2)
            if sub is None:
                return self._send(5)
            if sub == "board":
                return self._send(6)
            if sub == "lineups":
                return self._send(7)
        if self._operator_only(path):
            return
        if path.startswith("/api/"):
            return self._unmatched_route("GET")
        return self._serve_static(path)

    def _serve_static(self, path):
        if path in ("/shell", "/shell/"):
            rel = "shell.html"

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        if path.startswith("/api/setup/"):
            return self._handle_setup(path[len("/api/setup/"):], {})
        if path in ("/api/twin/preview", "/api/twin/commit"):
            if path.endswith("/preview"):
                return self._send(8)
            return self._send(9)
        act = re.match(r"^/api/games/([^/]+)/(.+)$", path)
        if act:
            gid, action = act.group(1), act.group(2)
            if action == "publish":
                return self._send(10)
            nested = re.match(r"^subs/([^/]+)/(offer|accept)$", action)
            if nested:
                return self._send(11)
            table = {"lock": 12, "unlock": 13}.get(action)
            if table:
                return self._send(table)

    def _handle_setup(self, entity, body):
        rid = re.match(r"^(team|player)/([^/]+)/delete$", entity)
        if rid:
            return self._send(14)
        if entity == "club":
            return self._send(15)
''')


class EveryShapeFoundTests(unittest.TestCase):
    maxDiff = None

    def test_every_shape_becomes_a_route(self):
        found = {(r.method, r.template) for r in extract_routes(EVERY_SHAPE)}
        self.assertEqual(found, {
            # literal / literal-set / nested literal inside a literal set
            ("GET", "/api/literal"), ("GET", "/api/one"), ("GET", "/api/two"),
            # regex, optional group (absent + each alternative), sub-dispatch
            ("GET", "/api/thing/{}"),
            ("GET", "/api/games/{}"), ("GET", "/api/games/{}/board"),
            ("GET", "/api/games/{}/lineups"),
            # a prefix branch that delegates nothing is itself a route
            ("GET", "/api/{*}"),
            # branches inside the static handler the tail hands off to
            ("GET", "/shell"), ("GET", "/shell/"),
            # endswith refinement of an already-selected pair
            ("POST", "/api/twin/preview"), ("POST", "/api/twin/commit"),
            # the family regex and each action under it
            ("POST", "/api/games/{}/{*}"), ("POST", "/api/games/{}/publish"),
            ("POST", "/api/games/{}/subs/{}/offer"),
            ("POST", "/api/games/{}/subs/{}/accept"),
            ("POST", "/api/games/{}/lock"), ("POST", "/api/games/{}/unlock"),
            # the delegated tail, walked with its prefix restored
            ("POST", "/api/setup/team/{}/delete"),
            ("POST", "/api/setup/player/{}/delete"),
            ("POST", "/api/setup/club"),
        })

    def test_shape_counts_cover_all_branch_kinds(self):
        walker = extract_walker(EVERY_SHAPE)
        self.assertEqual(
            {k: v for k, v in walker.shape_counts.items()},
            {"literal": 6, "literal-set": 3, "regex": 5, "prefix": 2,
             "dict-key": 1, "absent-group": 1, "endswith": 1})

    def test_handler_and_provenance_are_recorded(self):
        routes = {(r.method, r.template): r for r in extract_routes(EVERY_SHAPE)}
        self.assertEqual(routes[("POST", "/api/setup/club")].handler,
                         "_handle_setup")
        self.assertEqual(routes[("GET", "/shell")].handler, "_serve_static")
        self.assertEqual(routes[("GET", "/api/literal")].handler, "do_GET")
        self.assertGreater(routes[("GET", "/api/literal")].lineno, 0)

    def test_one_handler_reached_from_two_prefixes_yields_both(self):
        """A shared handler is walked once PER PREFIX, not once in total.

        Nothing does this today; the guard exists because the cheap version of
        it ("already walked, skip") loses a whole prefix's routes silently,
        which is the one failure mode this module may not have.
        """
        routes = extract_routes(_module('''
            def do_POST(self):
                path = self.path.split("?", 1)[0]
                if path.startswith("/api/setup/"):
                    return self._handle_setup(path[len("/api/setup/"):], {})
                if path.startswith("/api/legacy/"):
                    return self._handle_setup(path[len("/api/legacy/"):], {})

            def _handle_setup(self, entity, body):
                if entity == "club":
                    return self._send(1)
        '''))
        self.assertEqual({(r.method, r.template) for r in routes},
                         {("POST", "/api/setup/club"),
                          ("POST", "/api/legacy/club")})

    def test_editing_a_branch_moves_its_route(self):
        """Falsification: the extractor READS the source, it does not remember.

        Rename the path in the dispatch and the old route must disappear and the
        new one appear — a list that merely happened to agree would not move.
        """
        edited = EVERY_SHAPE.replace('"/api/literal"', '"/api/renamed"')
        self.assertNotEqual(edited, EVERY_SHAPE)
        found = {(r.method, r.template) for r in extract_routes(edited)}
        self.assertNotIn(("GET", "/api/literal"), found)
        self.assertIn(("GET", "/api/renamed"), found)


# --------------------------------------------------------------------------- #
# Every shape absent is refused, loudly                                        #
# --------------------------------------------------------------------------- #
class UnknownShapesRaiseTests(unittest.TestCase):
    def _extract(self, body):
        return extract_routes(_module(body))

    def test_unknown_string_method_on_the_path_raises(self):
        with self.assertRaises(ExtractionError) as caught:
            self._extract('''
                def do_GET(self):
                    path = self.path.split("?", 1)[0]
                    if path.rstrip("/") == "/api/x":
                        return self._send(1)
            ''')
        self.assertIn("rstrip", str(caught.exception))

    def test_unsupported_comparison_on_the_path_raises(self):
        with self.assertRaises(ExtractionError) as caught:
            self._extract('''
                def do_GET(self):
                    path = self.path.split("?", 1)[0]
                    if path != "/api/x":
                        return self._send(1)
            ''')
        self.assertIn("unsupported comparison", str(caught.exception))

    def test_compound_test_touching_the_path_raises(self):
        with self.assertRaises(ExtractionError) as caught:
            self._extract('''
                def do_GET(self):
                    path = self.path.split("?", 1)[0]
                    if path == "/api/x" or path == "/api/y":
                        return self._send(1)
            ''')
        self.assertIn("unrecognised shape", str(caught.exception))

    def test_new_dispatch_helper_must_be_classified(self):
        with self.assertRaises(ExtractionError) as caught:
            self._extract('''
                def do_POST(self):
                    path = self.path.split("?", 1)[0]
                    if path.startswith("/api/new/"):
                        return self._handle_new(path[len("/api/new/"):], {})

                def _handle_new(self, entity, body):
                    if entity == "thing":
                        return self._send(1)
            ''')
        self.assertIn("_handle_new", str(caught.exception))

    def test_a_new_verb_with_its_own_dispatch_raises(self):
        """GET and POST are the only dispatch chains; a third must announce
        itself rather than have its whole method quietly uninventoried."""
        with self.assertRaises(ExtractionError) as caught:
            self._extract('''
                def do_GET(self):
                    path = self.path.split("?", 1)[0]
                    if path == "/api/x":
                        return self._send(1)

                def do_PUT(self):
                    path = self.path.split("?", 1)[0]
                    if path == "/api/x":
                        return self._send(2)
            ''')
        self.assertIn("do_PUT", str(caught.exception))
        self.assertIn("ENTRY_POINTS", str(caught.exception))

    def test_todays_other_verbs_are_not_dispatchers(self):
        """The real do_HEAD/do_OPTIONS/do_PUT pass that audit — the guard above
        is not vacuous only because they are genuinely path-blind."""
        extract_routes()  # the real server.py; raises if any verb dispatches

    def test_a_delegation_the_walker_cannot_follow_raises(self):
        """A known handler reached in an unfollowed statement form.

        Its whole prefix would otherwise be missing from the inventory with no
        error at all — silence is the one outcome this module may not produce.
        """
        with self.assertRaises(ExtractionError) as caught:
            self._extract('''
                def do_POST(self):
                    path = self.path.split("?", 1)[0]
                    if path.startswith("/api/setup/"):
                        answer = self._handle_setup(path[len("/api/setup/"):], {})
                        return answer

                def _handle_setup(self, entity, body):
                    if entity == "club":
                        return self._send(1)
            ''')
        self.assertIn("does not follow", str(caught.exception))

    def test_a_guard_that_merely_passes_the_path_along_is_not_a_route(self):
        routes = self._extract('''
            def do_GET(self):
                path = self.path.split("?", 1)[0]
                if self._operator_only(path):
                    return
                if "GET" not in self._supported_methods(path):
                    return self._unmatched_route("GET")
                if path == "/api/x":
                    return self._send(1)
        ''')
        self.assertEqual([(r.method, r.template) for r in routes],
                         [("GET", "/api/x")])


# --------------------------------------------------------------------------- #
# Unreachable nested branches are reported, not emitted                        #
# --------------------------------------------------------------------------- #
class UnreachableBranchTests(unittest.TestCase):
    def test_nested_branch_outside_the_enclosing_set_is_reported(self):
        walker = extract_walker(_module('''
            def do_GET(self):
                path = self.path.split("?", 1)[0]
                if path in ("/api/a", "/api/b"):
                    if path == "/api/c":
                        return self._send(1)
                    return self._send(2)
        '''))
        self.assertEqual({(r.method, r.template) for r in walker.routes.values()},
                         {("GET", "/api/a"), ("GET", "/api/b")})
        self.assertEqual([entry[2] for entry in walker.unreachable],
                         ["path == '/api/c'"])

    def test_subpath_outside_its_own_alternation_is_reported(self):
        walker = extract_walker(_module('''
            def do_GET(self):
                path = self.path.split("?", 1)[0]
                game = re.match(r"^/api/games/([^/]+)/(board|lineups)$", path)
                if game:
                    gid, sub = game.group(1), game.group(2)
                    if sub == "roster":
                        return self._send(1)
        '''))
        self.assertEqual([entry[2] for entry in walker.unreachable],
                         ["sub == 'roster'"])

    def test_the_real_server_has_no_unreachable_dispatch_branches(self):
        self.assertEqual(extract_walker().unreachable, [])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
