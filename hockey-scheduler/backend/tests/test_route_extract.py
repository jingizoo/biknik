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
                         ["/x/{}/assign-{w}"])
        self.assertEqual(templates_of_pattern(r"^/calendar/(team)/([^/]+)\.ics$"),
                         ["/calendar/team/{}.ics"])

    def test_word_segment_is_not_identified_with_a_general_segment(self):
        """#202 repair root cause 4: \\w+ excludes '.', '-' and other bytes
        [^/]+ allows -- a route whose real matcher is \\w+ and one whose real
        matcher is [^/]+ are DIFFERENT reachable sets and must not collapse
        to the same template, or a uniqueness/equivalence check over the two
        would wrongly call them the same route.
        """
        word = templates_of_pattern(r"^/a/(\w+)$")
        seg = templates_of_pattern(r"^/a/([^/]+)$")
        self.assertNotEqual(word, seg)
        self.assertEqual(word, ["/a/{w}"])
        self.assertEqual(seg, ["/a/{}"])

    def test_possibly_empty_tail_is_not_identified_with_a_non_empty_one(self):
        """#202 repair root cause 4: .* accepts an empty remainder, .+ does
        not -- also genuinely different reachable sets.
        """
        tail0 = templates_of_pattern(r"^/a/(.*)$")
        tail1 = templates_of_pattern(r"^/a/(.+)$")
        self.assertNotEqual(tail0, tail1)
        self.assertEqual(tail0, ["/a/{*0}"])
        self.assertEqual(tail1, ["/a/{*}"])

    def test_top_level_alternation_is_rejected_as_ambiguous(self):
        """#202 repair root cause 5: regex alternation is the LOWEST-
        precedence operator, so ``^/api/foo|/api/bar$`` parses as
        ``(^/api/foo)|(/api/bar$)`` -- the first branch anchored at the
        START ONLY (so it matches "/api/foo-anything" under re.match, which
        does not require consuming the whole string), not "each branch
        anchored the way the whole pattern reads". That divergence must be
        raised, not silently accepted.
        """
        with self.assertRaises(ExtractionError) as caught:
            templates_of_pattern(r"^/api/foo|/api/bar$")
        self.assertIn("alternation", str(caught.exception))
        # The demonstrated divergence: re.match ACTUALLY matches text a naive
        # "each branch fully anchored" reading would not expect.
        import re as _re
        self.assertIsNotNone(
            _re.match(r"^/api/foo|/api/bar$", "/api/foo-anything-else"))
        # A grouped alternation is unambiguous and must still work.
        self.assertEqual(templates_of_pattern(r"^/api/(?:foo|bar)$"),
                         ["/api/foo", "/api/bar"])

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
            # a prefix branch that delegates nothing is itself a route --
            # {*0} not {*}: startswith() permits an EMPTY remainder
            ("GET", "/api/{*0}"),
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
# PARSED_DELEGATES: call-site args bind to callee params, and a tuple tested   #
# against a literal tuple-keyed dict enumerates concrete leaves (#202 repair   #
# root cause 1 -- the 13-for-13 substitution).                                #
# --------------------------------------------------------------------------- #
# Mirrors the real _handle_setup -> _handle_reassign shape: a regex captures
# (entity, id, target); the id is delegated on as an opaque wildcard, but
# entity+target together are looked up in a schema dict, and ONLY the dict's
# own keys are real leaves -- not the bare regex's full (alpha|beta) x \w+
# cross-product.
REASSIGN_FIXTURE = _module('''
    def do_POST(self):
        path = self.path.split("?", 1)[0]
        if path.startswith("/api/setup/"):
            return self._handle_setup(path[len("/api/setup/"):], {})

    def _handle_setup(self, entity, body):
        m = re.match(r"^(alpha|beta)/([^/]+)/assign-(\\w+)$", entity)
        if m:
            return self._handle_reassign(m.group(1), m.group(2), m.group(3), body)

    def _handle_reassign(self, entity, record_id, target, body):
        combo = (entity, target)
        SCHEMA = {("alpha", "one"): {}, ("beta", "two"): {}}
        if combo in SCHEMA:
            return self._send(1)
        return self._send(2)
''')


class ParsedDelegateEnumerationTests(unittest.TestCase):
    maxDiff = None

    def test_dict_keys_enumerate_concrete_leaves_not_the_wildcard(self):
        found = {(r.method, r.template) for r in extract_routes(REASSIGN_FIXTURE)}
        self.assertEqual(found, {
            ("POST", "/api/setup/alpha/{}/assign-one"),
            ("POST", "/api/setup/beta/{}/assign-two"),
        })
        # The bare regex's own (alpha|beta) x \w+ wildcard family must NOT
        # additionally survive -- that IS the 13-for-13 substitution: the
        # wildcard standing in as the only representation of a route whose
        # real reachable set is the schema's own, narrower key set.
        self.assertNotIn(("POST", "/api/setup/alpha/{}/assign-{w}"), found)
        self.assertNotIn(("POST", "/api/setup/beta/{}/assign-{w}"), found)

    def test_record_id_stays_the_one_genuine_wildcard(self):
        """The middle capture (an opaque id, never looked up in the schema)
        is not enumerated -- only entity/target, the two the dict actually
        keys on, are resolved to literals."""
        routes = {r.template: r for r in extract_routes(REASSIGN_FIXTURE)}
        self.assertIn("/api/setup/alpha/{}/assign-one", routes)

    def test_a_combo_the_schema_does_not_cover_is_not_a_route(self):
        """A third outer alternative with NO matching schema entry: it must
        contribute NO leaf (not a wildcard, not a guess) -- the schema is the
        truth about what's reachable, and a bare regex alternative with
        nothing in the schema reaches only the function's own not-found
        fallthrough, which is not a policy leaf."""
        fixture = _module('''
            def do_POST(self):
                path = self.path.split("?", 1)[0]
                if path.startswith("/api/setup/"):
                    return self._handle_setup(path[len("/api/setup/"):], {})

            def _handle_setup(self, entity, body):
                m = re.match(r"^(alpha|beta|gamma)/([^/]+)/assign-(\\w+)$", entity)
                if m:
                    return self._handle_reassign(
                        m.group(1), m.group(2), m.group(3), body)

            def _handle_reassign(self, entity, record_id, target, body):
                combo = (entity, target)
                SCHEMA = {("alpha", "one"): {}, ("beta", "two"): {}}
                if combo in SCHEMA:
                    return self._send(1)
                return self._send(2)
        ''')
        self.assertIn("gamma", fixture)
        found = {(r.method, r.template) for r in extract_routes(fixture)}
        self.assertEqual(found, {
            ("POST", "/api/setup/alpha/{}/assign-one"),
            ("POST", "/api/setup/beta/{}/assign-two"),
        })
        self.assertFalse(any("gamma" in t for _, t in found))

    def test_get_lookup_form_enumerates_the_same_way_as_in(self):
        """``SCHEMA.get(combo)`` bound then tested truthy is the SAME
        enumeration as ``combo in SCHEMA`` (#202 repair root cause 1 names
        both ``X in DICT`` and ``DICT.get(X)`` as forms that must enumerate).
        """
        fixture = _module('''
            def do_POST(self):
                path = self.path.split("?", 1)[0]
                if path.startswith("/api/setup/"):
                    return self._handle_setup(path[len("/api/setup/"):], {})

            def _handle_setup(self, entity, body):
                m = re.match(r"^(alpha|beta)/([^/]+)/assign-(\\w+)$", entity)
                if m:
                    return self._handle_reassign(
                        m.group(1), m.group(2), m.group(3), body)

            def _handle_reassign(self, entity, record_id, target, body):
                combo = (entity, target)
                SCHEMA = {("alpha", "one"): {}, ("beta", "two"): {}}
                call = SCHEMA.get(combo)
                if call is not None:
                    return self._send(1)
                return self._send(2)
        ''')
        self.assertIn("SCHEMA.get(combo)", fixture)
        found = {(r.method, r.template) for r in extract_routes(fixture)}
        self.assertEqual(found, {
            ("POST", "/api/setup/alpha/{}/assign-one"),
            ("POST", "/api/setup/beta/{}/assign-two"),
        })

    def test_subscript_lookup_form_also_enumerates(self):
        """``SCHEMA[combo]`` (direct subscript, not ``.get``) is the third
        form #202 repair root cause 1 names."""
        fixture = _module('''
            def do_POST(self):
                path = self.path.split("?", 1)[0]
                if path.startswith("/api/setup/"):
                    return self._handle_setup(path[len("/api/setup/"):], {})

            def _handle_setup(self, entity, body):
                m = re.match(r"^(alpha|beta)/([^/]+)/assign-(\\w+)$", entity)
                if m:
                    return self._handle_reassign(
                        m.group(1), m.group(2), m.group(3), body)

            def _handle_reassign(self, entity, record_id, target, body):
                combo = (entity, target)
                SCHEMA = {("alpha", "one"): {}, ("beta", "two"): {}}
                call = SCHEMA[combo]
                if call is not None:
                    return self._send(1)
                return self._send(2)
        ''')
        self.assertIn("SCHEMA[combo]", fixture)
        found = {(r.method, r.template) for r in extract_routes(fixture)}
        self.assertEqual(found, {
            ("POST", "/api/setup/alpha/{}/assign-one"),
            ("POST", "/api/setup/beta/{}/assign-two"),
        })

    def test_single_key_equality_on_the_tuple_also_enumerates(self):
        """``if combo == ("alpha", "one"):`` -- the real server.py has
        exactly this shape too (a combo-specific validation branch, in
        ``_handle_reassign_v2`` at ``combo == ("team", "league")``), and it
        must resolve to the SAME single leaf its schema entry already
        claims, not raise as an unrecognised comparison."""
        fixture = _module('''
            def do_POST(self):
                path = self.path.split("?", 1)[0]
                if path.startswith("/api/setup/"):
                    return self._handle_setup(path[len("/api/setup/"):], {})

            def _handle_setup(self, entity, body):
                m = re.match(r"^(alpha|beta)/([^/]+)/assign-(\\w+)$", entity)
                if m:
                    return self._handle_reassign(
                        m.group(1), m.group(2), m.group(3), body)

            def _handle_reassign(self, entity, record_id, target, body):
                combo = (entity, target)
                if combo == ("alpha", "one"):
                    pass
                SCHEMA = {("alpha", "one"): {}, ("beta", "two"): {}}
                if combo in SCHEMA:
                    return self._send(1)
                return self._send(2)
        ''')
        found = {(r.method, r.template) for r in extract_routes(fixture)}
        self.assertIn(("POST", "/api/setup/alpha/{}/assign-one"), found)

    def test_a_delegate_call_the_walker_does_not_follow_raises(self):
        """PARSED_DELEGATES joined the "must be followed" check in #202's
        repair -- before it, a call in an unfollowed form (assigned to a
        local first, here) would leave _handle_reassign's whole leaf set
        silently absent instead of raising."""
        fixture = _module('''
            def do_POST(self):
                path = self.path.split("?", 1)[0]
                if path.startswith("/api/setup/"):
                    return self._handle_setup(path[len("/api/setup/"):], {})

            def _handle_setup(self, entity, body):
                m = re.match(r"^(alpha|beta)/([^/]+)/assign-(\\w+)$", entity)
                if m:
                    answer = self._handle_reassign(
                        m.group(1), m.group(2), m.group(3), body)
                    return answer

            def _handle_reassign(self, entity, record_id, target, body):
                combo = (entity, target)
                SCHEMA = {("alpha", "one"): {}, ("beta", "two"): {}}
                if combo in SCHEMA:
                    return self._send(1)
                return self._send(2)
        ''')
        with self.assertRaises(ExtractionError) as caught:
            extract_routes(fixture)
        self.assertIn("does not follow", str(caught.exception))

    def test_uncorrelated_components_fail_closed_rather_than_guess(self):
        """``combo``'s two components must come from the SAME regex match to
        be safely enumerated (see _tuple_dict_outcome). Building it from an
        unrelated tracked name must not silently enumerate a WRONG cross
        product -- it must raise, because nothing here can prove the
        components are positionally correlated."""
        fixture = _module('''
            def do_POST(self):
                path = self.path.split("?", 1)[0]
                other = re.match(r"^/api/other/([^/]+)$", path)
                m = re.match(r"^/api/setup/(alpha|beta)/assign-(\\w+)$", path)
                if m and other:
                    entity, target = m.group(1), m.group(2)
                    uncorrelated = other.group(1)
                    combo = (entity, uncorrelated)
                    SCHEMA = {("alpha", "x"): {}}
                    if combo in SCHEMA:
                        return self._send(1)
        ''')
        with self.assertRaises(ExtractionError) as caught:
            extract_routes(fixture)
        self.assertIn("unrecognised shape", str(caught.exception))


# --------------------------------------------------------------------------- #
# The unconditional static tail (#202 repair root cause 6): an if/elif chain   #
# over literal(-set) tests of a tracked subject whose terminal else RE-        #
# DERIVES a new value from that same subject is an implicit wildcard branch,   #
# not a silent dead end -- mirrors _serve_static's own if/elif/else.          #
# --------------------------------------------------------------------------- #
class StaticTailTests(unittest.TestCase):
    maxDiff = None

    def test_terminal_else_that_rederives_the_subject_is_a_wildcard(self):
        fixture = _module('''
            def do_GET(self):
                path = self.path.split("?", 1)[0]
                return self._serve_static(path)

            def _serve_static(self, path):
                if path in ("/setup", "/setup/"):
                    rel = "setup.html"
                elif path in ("/", ""):
                    rel = "index.html"
                else:
                    rel = path.lstrip("/")
        ''')
        found = {(r.method, r.template) for r in extract_routes(fixture)}
        self.assertEqual(found, {
            ("GET", "/setup"), ("GET", "/setup/"),
            ("GET", "/"), ("GET", ""),
            ("GET", "/{*}"),
        })
        routes = {r.template: r for r in extract_routes(fixture)}
        self.assertEqual(routes["/{*}"].shape, "static-tail")
        self.assertEqual(routes["/{*}"].handler, "_serve_static")

    def test_a_terminal_else_not_derived_from_the_subject_is_not_a_route(self):
        """The SAME if/elif/else SHAPE, but the terminal else does not touch
        the subject at all -- must NOT manufacture a phantom wildcard.
        Over-triggering here would be a new false-positive bug, not a fix.
        """
        fixture = _module('''
            def do_GET(self):
                path = self.path.split("?", 1)[0]
                return self._serve_static(path)

            def _serve_static(self, path):
                if path in ("/a", "/b"):
                    rel = "a.html"
                else:
                    rel = "fallback.html"
        ''')
        found = {(r.method, r.template) for r in extract_routes(fixture)}
        self.assertEqual(found, {("GET", "/a"), ("GET", "/b")})

    def test_an_if_with_no_else_at_all_stays_exactly_as_before(self):
        """No terminal else -> nothing to model; the ordinary case (used
        throughout EVERY_SHAPE) must be completely unaffected."""
        fixture = _module('''
            def do_GET(self):
                path = self.path.split("?", 1)[0]
                return self._serve_static(path)

            def _serve_static(self, path):
                if path in ("/a", "/b"):
                    rel = "a.html"
        ''')
        found = {(r.method, r.template) for r in extract_routes(fixture)}
        self.assertEqual(found, {("GET", "/a"), ("GET", "/b")})

    def test_the_real_server_static_tail_is_exactly_one_route(self):
        walker = extract_walker()
        statics = [r for r in walker.routes.values() if r.shape == "static-tail"]
        self.assertEqual([(r.method, r.template, r.handler) for r in statics],
                         [("GET", "/{*}", "_serve_static")])


# --------------------------------------------------------------------------- #
# INVENTED evasions -- not named in the #202 repair review, found by trying to #
# defeat the finished gate the same way the review's own examples do.         #
# --------------------------------------------------------------------------- #
class InventedEvasionTests(unittest.TestCase):
    def _raises(self, body, *substrings):
        with self.assertRaises(ExtractionError) as caught:
            extract_routes(_module(body))
        msg = str(caught.exception)
        for s in substrings:
            self.assertIn(s, msg)

    def test_direct_self_path_bypassing_the_local_raises(self):
        """``if self.path == ...`` with no ``path = self.path...`` local ever
        bound: root_name() used to resolve this to "self", which is never
        tracked -- neither classified nor flagged, a true silent miss."""
        self._raises('''
            def do_GET(self):
                if self.path == "/api/evade-direct-path":
                    return self._send(1)
        ''', "unrecognised shape")

    def test_match_statement_on_a_tracked_subject_raises(self):
        """``match``/``case`` is not an ast.If -- the completeness scan never
        even visited the node, so this was silent, not just unclassified."""
        self._raises('''
            def do_GET(self):
                path = self.path.split("?", 1)[0]
                match path:
                    case "/api/evade-match":
                        return self._send(1)
        ''', "match", "does not model")

    def test_match_statement_on_an_untainted_subject_is_walked_normally(self):
        """The SAME statement shape must NOT be flagged when its subject is
        not path-derived -- and a route nested inside a case body is still
        found, proving match/case is walked, not merely tolerated."""
        found = {(r.method, r.template) for r in extract_routes(_module('''
            def do_GET(self):
                path = self.path.split("?", 1)[0]
                mode = "x"
                match mode:
                    case "x":
                        if path == "/api/inside-case":
                            return self._send(1)
        '''))}
        self.assertEqual(found, {("GET", "/api/inside-case")})

    def test_ternary_inlined_directly_never_bound_to_a_local_raises(self):
        """The hardest form: the ternary's result is never assigned anywhere
        -- straight into a call argument -- so there is no local for
        taint-propagation to have flagged either."""
        self._raises('''
            def do_GET(self):
                path = self.path.split("?", 1)[0]
                return self._send(1 if path == "/api/evade-ternary" else 2)
        ''', "ternary", "does not model")

    def test_any_over_a_generator_expression_raises(self):
        """``any(path == p for p in candidates)``: root_name() on the outer
        Call resolves only to "any" itself; the generator's own element
        expression was invisible without special-casing any()/all()."""
        self._raises('''
            def do_GET(self):
                path = self.path.split("?", 1)[0]
                if any(path == p for p in ("/api/evade-any-1", "/api/evade-any-2")):
                    return self._send(1)
        ''', "unrecognised shape")

    def test_precompiled_regex_alias_still_raises(self):
        """Not a new fix -- a standing proof that an evasion the review's own
        text names ("aliases used by regex") is ALREADY closed by the
        existing _REGEX_METHODS fail-closed check, so this round's other
        changes have not quietly reopened it."""
        self._raises('''
            _RX = None

            def do_GET(self):
                path = self.path.split("?", 1)[0]
                if _RX.match(path):
                    return self._send(1)
        ''', "regex call", "does not model")

    def test_the_real_servers_two_real_ternaries_are_reviewed_waivers(self):
        """server.py DOES use a ternary touching a tracked capture twice
        (_handle_setup_v2, archive/reopen and the venue delete mapper) --
        both pick a BACKEND FUNCTION for a route the enclosing regex's own
        alternation already fully enumerates as separate leaves, not a new
        route. They are waived, not unmodelled; extract_walker() over the
        real file must not raise for them."""
        extract_routes()  # raises if either is unwaived


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
