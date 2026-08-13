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

from hockey_scheduler.web import route_extract as route_extract_module
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
# #202 repair round 2, finding A: _propagates_taint's per-Call loop used to    #
# return False -- silently "not tainted" -- for the WHOLE expression the       #
# instant ANY unlisted call appeared anywhere in it, with no regard for        #
# whether that call's own receiver/arguments still carried a tracked name.     #
# A local bound this way never joined `tracked`, so the `if` that actually     #
# decided the route was invisible to the completeness scan: no route, no       #
# raise. Three concrete, reproduced escapes, plus proof the fix is a GENERAL   #
# rule (an unbounded family of shapes, not 3 hardcoded patterns) that still    #
# does not flag a value legitimately handed to an unrelated call.              #
# --------------------------------------------------------------------------- #
class UnlistedCallTaintDetachmentTests(unittest.TestCase):
    def _raises(self, body, *substrings):
        with self.assertRaises(ExtractionError) as caught:
            extract_routes(_module(body))
        msg = str(caught.exception)
        for s in substrings:
            self.assertIn(s, msg)

    def test_next_over_a_genexpr_bound_then_tested_truthy_raises(self):
        """``found = next((r for r in KNOWN if r == path), None)`` then
        ``if found:`` -- a routing decision (compare path against a known
        set) spelled as a generator lookup instead of ``path in (...)``.
        ``next`` is a bare-name call (``func`` isn't even an ``ast.Attribute``),
        so the OLD code's ``isinstance(func, ast.Attribute)`` check failed
        immediately and silently returned "not tainted" without ever looking
        inside the generator's own condition."""
        self._raises('''
            def do_GET(self):
                path = self.path.split("?", 1)[0]
                found = next((r for r in ("/api/evade-next-1",
                                          "/api/evade-next-2")
                             if r == path), None)
                if found:
                    return self._send(1)
        ''', "unlisted call")

    def test_index_in_try_except_used_as_a_sentinel_raises(self):
        """``.index()`` raising ``ValueError`` as control flow: a decision
        variable produced inside ``try``/``except``, tested afterwards."""
        self._raises('''
            KNOWN = ("/api/evade-index-1", "/api/evade-index-2")

            def do_GET(self):
                path = self.path.split("?", 1)[0]
                try:
                    which = KNOWN.index(path)
                except ValueError:
                    which = -1
                if which >= 0:
                    return self._send(1)
        ''', "unlisted call")

    def test_getattr_indirect_dispatch_raises(self):
        """``getattr(self, computed_name, None)`` used to obtain and invoke a
        handler indirectly -- doubly invisible under the old code: the
        ``getattr`` call itself is a bare-name call (trips the same
        immediate "not tainted" return as ``next``), and even a tainted
        result would not have matched the separate "unknown dispatch
        helper" check, which only pattern-matches literal
        ``self.<name>(...)`` attribute calls, never a call through a
        variable."""
        self._raises('''
            def do_GET(self):
                path = self.path.split("?", 1)[0]
                suffix = path.rsplit("/", 1)[-1]
                handler_name = "_handle_evade_" + suffix
                handler = getattr(self, handler_name, None)
                if handler:
                    return handler()

            def _handle_evade_target(self):
                return self._send(1)
        ''', "unlisted call")

    def test_general_unlisted_call_shapes_all_raise(self):
        """The gap is unbounded, not 3 specific shapes: every construct the
        finding names as "equally unmodelled and equally silent" -- .count(),
        a generic dict.get() (not the recognised inline-dict/tuple-dict
        forms), sorted() wrapping a comprehension, str.format_map(), and an
        arbitrary project-local helper function -- must ALSO raise, proving
        the fix is a rule about unlisted calls in general, not a lookup
        table of the 3 reproduced cases."""
        shapes = {
            # NOTE: `.count()` bound to a local first, then tested -- NOT
            # `if path.count(...):` directly, which a DIFFERENT, pre-existing
            # mechanism already raises on (_classify's own "unsupported
            # method count() on dispatch subject" check for a method call
            # used AS an if-test). Binding it to a local is what actually
            # exercises _propagates_taint's new check, same as the 3
            # reproduced escapes (each also binds to a local before testing).
            "dot_count": '''
                def do_GET(self):
                    path = self.path.split("?", 1)[0]
                    n = path.count("/api/evade-count")
                    if n:
                        return self._send(1)
            ''',
            "generic_dict_get": '''
                _ROUTE_KIND = {"/api/evade-dictget": "special"}

                def do_GET(self):
                    path = self.path.split("?", 1)[0]
                    kind = _ROUTE_KIND.get(path)
                    if kind == "special":
                        return self._send(1)
            ''',
            "sorted_over_comprehension": '''
                def do_GET(self):
                    path = self.path.split("?", 1)[0]
                    matches = sorted(p for p in ("/api/evade-sorted",)
                                     if p == path)
                    if matches:
                        return self._send(1)
            ''',
            "str_format_map": '''
                def do_GET(self):
                    path = self.path.split("?", 1)[0]
                    s = "{x}".format_map({"x": path})
                    if s == "/api/evade-formatmap":
                        return self._send(1)
            ''',
            "arbitrary_helper_function": '''
                def _looks_like_evade(p):
                    return p == "/api/evade-helper"

                def do_GET(self):
                    path = self.path.split("?", 1)[0]
                    hit = _looks_like_evade(path)
                    if hit:
                        return self._send(1)
            ''',
        }
        for name, body in shapes.items():
            with self.subTest(shape=name):
                self._raises(body, "unlisted call")

    def test_captured_group_handed_inline_to_a_service_does_not_raise(self):
        """The legitimate case, spelled the way the real ``_dispatch_get``
        spells it: a captured group hand-delivered straight into a service
        call, never bound to a local first. Mirrors
        ``ics = api.calendar_feed_ics(cal.group(1))`` from
        ``_propagates_taint``'s own docstring -- must NOT raise merely
        because the extraction is inline instead of pre-bound."""
        found = {(r.method, r.template) for r in extract_routes(_module('''
            def do_GET(self):
                path = self.path.split("?", 1)[0]
                cal = re.match(r"^/calendar/(team|division)/([^/]+)\\.ics$",
                              path)
                if cal:
                    ics = api.calendar_feed_ics(cal.group(1), cal.group(2))
                    if ics is None:
                        return self._send_json({"error": "not_found"}, 404)
                    return self._send_ics(ics)
        '''))}
        self.assertEqual(found, {("GET", "/calendar/team/{}.ics"),
                                 ("GET", "/calendar/division/{}.ics")})

    def test_path_property_handed_to_an_unrelated_lookup_does_not_raise(self):
        """Mirrors the real ``_serve_static``'s
        ``CONTENT_TYPES.get(target.suffix, "application/octet-stream")``:
        ``target`` is legitimately tracked (#202 repair root cause 2 --
        pathlib reshaping IS still the path), but ``.suffix`` is a lossy,
        narrow EXTRACTION (the pathlib analogue of a captured regex group),
        and handing it to an unrelated content-type table is not a routing
        decision. This is the over-broad failure mode the round 2 fix must
        NOT reintroduce."""
        found = {(r.method, r.template) for r in extract_routes(_module('''
            def do_GET(self):
                path = self.path.split("?", 1)[0]
                return self._serve_static(path)

            def _serve_static(self, path):
                if path in ("/shell", "/shell/"):
                    rel = "shell.html"
                target = (STATIC_DIR / rel).resolve()
                ctype = CONTENT_TYPES.get(target.suffix, "application/octet-stream")
                return ctype
        '''))}
        self.assertEqual(found, {("GET", "/shell"), ("GET", "/shell/")})

    def test_path_consuming_method_does_not_raise(self):
        """Mirrors the real ``_serve_static``'s ``data = target.read_bytes()``:
        a Path method that CONSUMES an already-resolved location to produce
        something wholly unrelated (file content), not another Path. Not the
        same list as the Path-reshaping ``_PATH_METHODS`` (those still
        return a Path); this is the pathlib analogue of a captured group,
        the SAME "over-broad failure mode" guard as the property test above."""
        found = {(r.method, r.template) for r in extract_routes(_module('''
            def do_GET(self):
                path = self.path.split("?", 1)[0]
                return self._serve_static(path)

            def _serve_static(self, path):
                if path in ("/shell", "/shell/"):
                    rel = "shell.html"
                target = (STATIC_DIR / rel).resolve()
                data = target.read_bytes()
                return data
        '''))}
        self.assertEqual(found, {("GET", "/shell"), ("GET", "/shell/")})

    def test_the_real_server_extracts_with_no_new_raises(self):
        """The real server.py -- including ``_handle_reassign``'s
        ``_REASSIGN_PARENTS.get(combo)``/``self._V1_SETUP_KIND.get(entity,
        entity)``, ``_handle_setup``'s ``_to_v1.get(kind, lambda r: r)``, and
        do_POST's blanket ``required_permission(path)``/``scope_violation(
        ...)`` authorisation calls -- must still extract cleanly: each is a
        reviewed, declared ``_AUDIT_WAIVERS`` entry, not a scoping hole."""
        walker = extract_walker()
        self.assertEqual(len(walker.routes), 239)
        self.assertEqual(walker.unreachable, [])


# --------------------------------------------------------------------------- #
# #202 repair round 2, finding B: a `while` loop's own `.test` was invisible   #
# to the completeness audit -- `_walk_stmt`'s (Try, With, For, While)          #
# handling walks the BODY (so a route nested further inside is still found),   #
# but neither that walk nor the audit's ast.walk scan (which matched only      #
# ast.If/ast.IfExp) ever inspected the loop's own guard. A `while path ==      #
# "/new-route":` produced ZERO routes and ZERO exceptions -- not even a raise. #
# --------------------------------------------------------------------------- #
class WhileLoopGuardTests(unittest.TestCase):
    def test_a_while_guard_on_a_tracked_subject_raises(self):
        """The exact reproduced shape: silent before the fix (no route, no
        exception), must now raise loudly."""
        with self.assertRaises(ExtractionError) as caught:
            extract_routes(_module('''
                def do_GET(self):
                    path = self.path.split("?", 1)[0]
                    while path == "/new-route":
                        return self._send(1)
            '''))
        msg = str(caught.exception)
        self.assertIn("while loop", msg)
        self.assertIn("path", msg)

    def test_an_untainted_while_guard_is_not_flagged(self):
        """The SAME statement shape, but the loop condition does not touch a
        tracked name -- must NOT raise (over-triggering would be a new
        false-positive bug, not a fix), and a route nested inside the loop
        body must still be found (the walker's own body-walk is unaffected
        by this audit-only addition)."""
        found = {(r.method, r.template) for r in extract_routes(_module('''
            def do_GET(self):
                path = self.path.split("?", 1)[0]
                i = 0
                while i < 3:
                    if path == "/api/inside-while":
                        return self._send(1)
                    i += 1
        '''))}
        self.assertEqual(found, {("GET", "/api/inside-while")})

    def test_the_real_server_has_no_while_loops_to_flag(self):
        """server.py uses none today; the guard above is not vacuous only
        because a synthetic fixture proves it fires when one exists."""
        extract_routes()  # raises if the real file somehow grew one unaudited


# --------------------------------------------------------------------------- #
# #202 repair round 2, finding C: the existing unreachable-branch detector     #
# only fires when a NESTED branch's subject is already narrowed by an          #
# ENCLOSING alternation -- two top-level SIBLING branches narrow nothing, so   #
# a second, identical-literal branch (dead code after the first's              #
# unconditional return) claimed the same route with zero signal: ONE route     #
# recorded, no trace a duplicate/dead branch ever existed.                     #
# --------------------------------------------------------------------------- #
class SiblingOverlapTests(unittest.TestCase):
    def test_two_sibling_ifs_claiming_the_same_literal_raises(self):
        """The exact reproduced shape: silent before the fix."""
        with self.assertRaises(ExtractionError) as caught:
            extract_routes(_module('''
                def do_GET(self):
                    path = self.path.split("?", 1)[0]
                    if path == "/api/dup":
                        return self._send(1)
                    if path == "/api/dup":
                        return self._send(2)
            '''))
        msg = str(caught.exception)
        self.assertIn("ambiguous overlap", msg)
        # Both locations named, per the fix's own requirement -- the
        # duplicated test text appears twice: once for "this branch tests
        # X", once for "already claimed by ... X".
        self.assertIn("already claimed by", msg)
        self.assertEqual(msg.count("path == '/api/dup'"), 2)

    def test_duplicate_elif_arm_also_raises(self):
        """An elif ARM sharing a literal with an earlier arm of the SAME
        chain is the identical ambiguity, just spelled as `elif` instead of
        a second top-level `if` -- elif arms must share the sibling scope,
        not each get a fresh one."""
        with self.assertRaises(ExtractionError) as caught:
            extract_routes(_module('''
                def do_GET(self):
                    path = self.path.split("?", 1)[0]
                    if path == "/api/a":
                        return self._send(1)
                    elif path == "/api/a":
                        return self._send(2)
            '''))
        self.assertIn("ambiguous overlap", str(caught.exception))

    def test_elif_chain_with_distinct_literals_is_not_flagged(self):
        """The ordinary, non-ambiguous elif shape -- every arm tests a
        DIFFERENT literal -- must keep working exactly as before."""
        found = {(r.method, r.template) for r in extract_routes(_module('''
            def do_GET(self):
                path = self.path.split("?", 1)[0]
                if path == "/api/a":
                    return self._send(1)
                elif path == "/api/b":
                    return self._send(2)
        '''))}
        self.assertEqual(found, {("GET", "/api/a"), ("GET", "/api/b")})

    def test_overlap_is_not_ambiguous_when_the_first_body_falls_through(self):
        """SCOPE guard, not incidental: two siblings may legitimately share
        every claimed literal when the FIRST's body does not unconditionally
        exit -- mirrors the real ``_handle_reassign_v2``'s ``if combo in
        _V2_REASSIGN_SCHEMA:`` (returns only on a body-validation FAILURE,
        falls through to ordinary control flow on success) followed by an
        unrelated, independently-reachable ``dest = _V2_REASSIGN_DEST.get(
        combo); if dest is not None:`` authorisation-target lookup that
        happens to share every key. Flagging this pairing was a genuine
        over-broad failure, found and closed by requiring the EARLIER
        sibling's body to always exit before a later, overlapping claim
        counts as provably dead."""
        found = {(r.method, r.template) for r in extract_routes(_module('''
            def do_GET(self):
                path = self.path.split("?", 1)[0]
                if path == "/api/shared":
                    log_something()
                if path == "/api/shared":
                    return self._send(1)
        '''))}
        self.assertEqual(found, {("GET", "/api/shared")})

    def test_siblings_with_no_overlap_are_unaffected(self):
        """Two ordinary, non-overlapping top-level ifs -- the common case
        throughout server.py -- must be completely unaffected."""
        found = {(r.method, r.template) for r in extract_routes(_module('''
            def do_GET(self):
                path = self.path.split("?", 1)[0]
                if path == "/api/a":
                    return self._send(1)
                if path == "/api/b":
                    return self._send(2)
        '''))}
        self.assertEqual(found, {("GET", "/api/a"), ("GET", "/api/b")})

    def test_the_same_literal_in_a_different_nested_scope_is_unaffected(self):
        """The IDENTICAL literal, tested again inside a DIFFERENT enclosing
        body (its own fresh sibling scope, one level down in a prefix
        branch whose body the walker does not narrow the subject for) --
        must NOT be flagged. This check is scoped to siblings sharing ONE
        dispatch scope, not "the same literal anywhere in the function"."""
        found = {(r.method, r.template) for r in extract_routes(_module('''
            def do_GET(self):
                path = self.path.split("?", 1)[0]
                if path == "/api/shared-literal":
                    return self._send(1)
                if path.startswith("/api/wrap/"):
                    if path == "/api/shared-literal":
                        return self._send(2)
        '''))}
        # The startswith() branch is itself a real (non-delegating) prefix
        # route -- unrelated to this test's point, just the ordinary "prefix"
        # shape -- alongside the literal claimed identically in both scopes.
        self.assertEqual(found, {("GET", "/api/shared-literal"),
                                 ("GET", "/api/wrap/{*0}")})

    def test_the_real_server_has_no_ambiguous_sibling_overlaps(self):
        """server.py has none today -- including the _handle_reassign_v2
        schema/dest-lookup pairing above, which shares every key but is not
        ambiguous. Not vacuous: the earlier tests prove the check fires."""
        walker = extract_walker()
        self.assertEqual(len(walker.routes), 239)
        self.assertEqual(walker.unreachable, [])


# --------------------------------------------------------------------------- #
# #202 repair round 2, finding D: nothing counted or checked that a declared   #
# _AUDIT_WAIVERS entry was actually CONSULTED during a run. An orphaned entry  #
# (matching no line anywhere) sat silently and extraction succeeded normally   #
# -- directly contradicting "any future waiver must be exact-one-hit and       #
# fingerprinted". _DispatchWalker.waiver_hits/verify_waiver_usage close it.    #
# --------------------------------------------------------------------------- #
class WaiverFingerprintTests(unittest.TestCase):
    def _with_waivers(self, waivers: dict):
        """Temporarily replace the module's real _AUDIT_WAIVERS with exactly
        `waivers`, restored even if the test body raises -- these tests must
        never leak a mutated waiver dict into any other test in the process."""
        saved = dict(route_extract_module._AUDIT_WAIVERS)
        route_extract_module._AUDIT_WAIVERS.clear()
        route_extract_module._AUDIT_WAIVERS.update(waivers)
        self.addCleanup(lambda: (
            route_extract_module._AUDIT_WAIVERS.clear(),
            route_extract_module._AUDIT_WAIVERS.update(saved)))

    def test_every_real_waiver_is_hit_exactly_once(self):
        """The real server.py, unmodified: each of the 10 declared waivers
        (2 pre-existing + 2 pre-existing ternaries + 6 this round's findings
        added) is consulted for precisely the one line it names -- proves
        the instrumentation is wired all the way through _propagates_taint
        AND the ast.If/ast.IfExp/ast.While scan, not just one of them."""
        walker = extract_walker()
        self.assertEqual(len(route_extract_module._AUDIT_WAIVERS), 10)
        for key in route_extract_module._AUDIT_WAIVERS:
            with self.subTest(waiver=key):
                self.assertEqual(len(walker.waiver_hits.get(key, ())), 1)

    def test_a_dormant_waiver_matching_nothing_raises(self):
        """The exact reproduced shape: an orphaned entry, matching no line
        anywhere, used to sit silently with extraction succeeding normally."""
        self._with_waivers({
            ("do_GET", "this_never_matches_anything_in_the_fixture"): "orphaned",
        })
        walker = extract_walker(_module('''
            def do_GET(self):
                path = self.path.split("?", 1)[0]
                if path == "/api/x":
                    return self._send(1)
        '''))
        with self.assertRaises(ExtractionError) as caught:
            walker.verify_waiver_usage()
        msg = str(caught.exception)
        self.assertIn("DORMANT", msg)
        self.assertIn("this_never_matches_anything_in_the_fixture", msg)

    def test_a_waiver_matching_two_distinct_locations_raises(self):
        """"More than one hit" is a failure too -- a waiver text that
        happens to match TWO sibling occurrences of the identical
        unrecognised test cannot be trusted as pinned to the one line its
        author reviewed."""
        waiver_text = "path == '/api/x' or path == '/api/y'"
        self._with_waivers({("do_GET", waiver_text): "matches two lines"})
        walker = extract_walker(_module('''
            def do_GET(self):
                path = self.path.split("?", 1)[0]
                if path == "/api/x" or path == "/api/y":
                    return self._send(1)
                if path == "/api/x" or path == "/api/y":
                    return self._send(2)
        '''))
        with self.assertRaises(ExtractionError) as caught:
            walker.verify_waiver_usage()
        msg = str(caught.exception)
        self.assertIn("TOO BROAD", msg)
        self.assertIn("2 distinct locations", msg)

    def test_a_waiver_matching_exactly_one_location_does_not_raise(self):
        """The legitimate, single-hit case -- the control for the two
        failure-mode tests above, proving they fail for the stated reason
        and not merely because verify_waiver_usage always raises."""
        waiver_text = "path == '/api/x' or path == '/api/y'"
        self._with_waivers({("do_GET", waiver_text): "matches one line"})
        walker = extract_walker(_module('''
            def do_GET(self):
                path = self.path.split("?", 1)[0]
                if path == "/api/x" or path == "/api/y":
                    return self._send(1)
        '''))
        walker.verify_waiver_usage()  # must not raise

    def test_synthetic_fixtures_are_not_checked_against_real_waivers(self):
        """extract_routes()/extract_walker() gate verify_waiver_usage() to
        `source is None` (the real file) specifically -- a synthetic test
        fixture legitimately consults none of server.py's own waivers, and
        must NOT be forced to satisfy their fingerprint. Every OTHER test in
        this file passes a synthetic `source`; this is the one asserting
        that gate exists at all, not just relying on the rest happening not
        to trip it."""
        walker = extract_walker(_module('''
            def do_GET(self):
                path = self.path.split("?", 1)[0]
                if path == "/api/x":
                    return self._send(1)
        '''))
        # None of the real _AUDIT_WAIVERS were consulted -- would fail
        # fingerprinting if checked, but extract_walker() must not have.
        self.assertEqual(walker.waiver_hits, {})


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
