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

import ast
import json
import textwrap
import threading
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from helpers import BACKEND  # noqa: F401  (ensures sys.path is set up)

from hockey_scheduler.web import route_extract as route_extract_module
from hockey_scheduler.web.route_extract import (
    BINDING_NODE_TYPES, ExtractionError, expand_pattern, extract_routes,
    extract_walker, _binding_value_and_targets, sample_path,
    templates_of_pattern,
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
# Real-HTTP demonstration harness (#202 repair round 4). Several round-4      #
# findings require showing an escape reachable over an ACTUAL socket, not      #
# merely that extract_routes() stays silent on the same source -- a purely     #
# static "the walker missed it" proof does not, by itself, show the miss       #
# corresponds to a real HTTP hole (the reviewer's own words: "I verified a     #
# real localhost Handler..."). Builds a REAL, importable BaseHTTPRequestHandler#
# subclass from the SAME dedented source text handed to _module()/            #
# extract_routes() -- so the static check and the live check are provably      #
# examining IDENTICAL code, never a hand-maintained near-duplicate that could   #
# quietly drift from what the extractor actually sees.                         #
# --------------------------------------------------------------------------- #
def _real_http_probe(handler_body: str, method: str, path: str, body=None):
    """Run ``handler_body`` (the same dedented source a fixture passes to
    ``_module()``) as a REAL server on loopback; return ``(status, text)``
    for one real ``method path`` request.

    Unlike ``_module()`` (which pads whichever of do_GET/do_POST is missing
    so the EXTRACTOR always has both entry points to walk), a live server
    only needs whichever verb this probe actually sends -- no padding here,
    so a fixture missing the other verb still runs.
    """
    src = ("from contextlib import nullcontext\n"
          "class _ProbeHandler(BaseHTTPRequestHandler):\n"
          "    def _send(self, n):\n"
          "        self._send_json({'n': n})\n"
          "    def _send_json(self, payload, code=200):\n"
          "        data = json.dumps(payload).encode()\n"
          "        self.send_response(code)\n"
          "        self.send_header('Content-Type', 'application/json')\n"
          "        self.send_header('Content-Length', str(len(data)))\n"
          "        self.end_headers()\n"
          "        self.wfile.write(data)\n"
          "    def log_message(self, *a):\n"
          "        pass\n"
          + textwrap.indent(textwrap.dedent(handler_body), "    "))
    ns = {"BaseHTTPRequestHandler": BaseHTTPRequestHandler, "json": json}
    exec(compile(src, "<probe>", "exec"), ns)  # noqa: S102 -- test-only, fixed source
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), ns["_ProbeHandler"])
    port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{port}{path}"
        headers = {"Content-Type": "application/json"} if method == "POST" else {}
        data = json.dumps(body or {}).encode() if method == "POST" else None
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, r.read().decode()
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode()
    finally:
        httpd.shutdown()
        thread.join(timeout=5)
        httpd.server_close()


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
        # A guard shape among many others in this fixture -- deliberately a
        # LITERAL guard, not `path` itself: #202 repair round 4, finding 1
        # made a bare call's ARGUMENTS count when the call IS the whole
        # test, so `self._operator_only(path)` here would need its own
        # reviewed waiver (see UnknownShapesRaiseTests' dedicated pair of
        # tests for that exemption, now waiver-based); this fixture is
        # about the OTHER shapes below it, not that one.
        if self._operator_only("/api/admin"):
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
        """The real server.py, unmodified: each of the 29 declared waivers
        (18 from rounds 2-3 -- 2 pre-existing + 2 pre-existing ternaries + 6
        round-2 finding A additions + 8 round-3 finding E additions -- plus
        11 round-4 finding 1 additions, once a Call reached DIRECTLY as the
        whole test had its arguments scanned too) is consulted for precisely
        the one line it names -- proves the instrumentation is wired all the
        way through _propagates_taint AND the ast.If/ast.IfExp/ast.While/
        ast.match_case scan, not just one of them. Each key is now a 4-tuple
        (#202 repair round 4, finding 3: function, text, parent shape,
        enclosing if) rather than the original 2-tuple -- WaiverRelocation
        FingerprintTests below is the dedicated proof for what the extra two
        parts catch that this exact-one-hit check alone would not."""
        walker = extract_walker()
        self.assertEqual(len(route_extract_module._AUDIT_WAIVERS), 29)
        for key in route_extract_module._AUDIT_WAIVERS:
            with self.subTest(waiver=key):
                self.assertEqual(len(walker.waiver_hits.get(key, ())), 1)

    def test_a_dormant_waiver_matching_nothing_raises(self):
        """The exact reproduced shape: an orphaned entry, matching no line
        anywhere, used to sit silently with extraction succeeding normally."""
        self._with_waivers({
            ("do_GET", "this_never_matches_anything_in_the_fixture",
             "if_test", ""): "orphaned",
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
        author reviewed. Both occurrences share the SAME parent shape and
        the SAME (absent) enclosing if here -- true top-level siblings, not
        the branch-distinguishable case WaiverRelocationFingerprintTests
        covers -- so the fingerprint's extra context does not save this one,
        by design: it is still genuinely too broad."""
        waiver_text = "path == '/api/x' or path == '/api/y'"
        self._with_waivers({
            ("do_GET", waiver_text, "if_test", ""): "matches two lines"})
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
        self._with_waivers({
            ("do_GET", waiver_text, "if_test", ""): "matches one line"})
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
# #202 repair round 4, finding 3: a waiver keyed on just (function, exact     #
# expression text) is blind to RELOCATION -- the SAME once-used expression,   #
# moved into a NEW, routing-relevant structural position, still unparses      #
# identically, so the old waiver keeps matching a line that is now a          #
# genuine, unreviewed routing decision. _waiver_key widens the key with the   #
# expression's own PARENT SHAPE and its nearest ENCLOSING if-test -- this     #
# class proves both halves, using the reviewer's own named attack.            #
# --------------------------------------------------------------------------- #
class WaiverRelocationFingerprintTests(unittest.TestCase):
    def _with_waivers(self, waivers: dict):
        saved = dict(route_extract_module._AUDIT_WAIVERS)
        route_extract_module._AUDIT_WAIVERS.clear()
        route_extract_module._AUDIT_WAIVERS.update(waivers)
        self.addCleanup(lambda: (
            route_extract_module._AUDIT_WAIVERS.clear(),
            route_extract_module._AUDIT_WAIVERS.update(saved)))

    def test_relocating_the_identical_expression_into_a_routing_selector_is_caught(self):
        """The reviewer's own named attack, reproduced exactly: a waiver
        reviewed for ``required_permission(path)`` used as an Assign's RHS
        (the real do_POST's own shape -- see that entry in _AUDIT_WAIVERS)
        must NOT keep matching once the IDENTICAL expression, unchanged
        character for character, is moved into a dict-subscript SELECTOR
        that picks a handler by the tracked path's derived permission --
        the textbook "hide a route behind an already-waived expression"
        attack this finding exists to close.

        BEFORE (this test, at the safe position): no raise.
        AFTER (relocated into a selector): raises fresh, because the
        waiver's parent_shape ("assign_rhs") no longer matches the
        relocated expression's ("subscript_index")."""
        self._with_waivers({
            ("do_POST", "not authorize(role, path)", "if_test", ""):
                "reviewed blanket per-request authorisation gate",
            ("do_POST", "required_permission(path)", "assign_rhs",
             "not authorize(role, path)"):
                "reviewed: builds a 403 message, not a routing decision "
                "(the real do_POST's own shape)",
        })
        # SAFE: the exact reviewed position -- must not raise.
        safe_routes = extract_routes(_module('''
            def do_POST(self):
                path = self.path.split("?", 1)[0]
                role = "whatever"
                if not authorize(role, path):
                    perm = required_permission(path)
                    return self._send_status(403)
                if path == "/api/x":
                    return self._send(1)
        '''))
        self.assertEqual([(r.method, r.template) for r in safe_routes],
                         [("POST", "/api/x")])
        # ATTACK: the IDENTICAL text `required_permission(path)`, unchanged,
        # relocated into a Subscript index that selects a handler -- a
        # real routing decision hidden behind the already-waived text.
        with self.assertRaises(ExtractionError) as caught:
            extract_routes(_module('''
                _HANDLERS = {}
                def do_POST(self):
                    path = self.path.split("?", 1)[0]
                    handler = _HANDLERS[required_permission(path)]
                    return handler()
            '''))
        msg = str(caught.exception)
        self.assertIn("required_permission(path)", msg)
        self.assertIn("unlisted call", msg)

    def test_a_two_tuple_key_would_have_conflated_the_safe_and_attack_positions(self):
        """Documents WHY the wider key is necessary, not just that it works:
        the (function, text) PREFIX is IDENTICAL between the safe and the
        attack fixture above -- a waiver keyed on only those two parts
        cannot tell them apart at all, which is exactly the gap this
        finding closes."""
        safe_key = ("do_POST", "required_permission(path)", "assign_rhs",
                   "not authorize(role, path)")
        attack_key = ("do_POST", "required_permission(path)",
                      "subscript_index", "")
        self.assertEqual(safe_key[:2], attack_key[:2])
        self.assertNotEqual(safe_key, attack_key)

    def _guardian_fixture(self):
        return _module('''
            def do_POST(self):
                path = self.path.split("?", 1)[0]
                mga = re.match(r"^/api/one/([^/]+)/availability$", path)
                if mga:
                    guid, jid = "g", mga.group(1)
                    if self._guardian_link_or_403(guid, jid):
                        return
                    return self._send(1)
                mgs = re.match(r"^/api/two/([^/]+)/offer$", path)
                if mgs:
                    guid, jid = "g", mgs.group(1)
                    if self._guardian_link_or_403(guid, jid):
                        return
                    return self._send(2)
        ''')

    def test_identical_text_in_two_branches_needs_both_waivers_not_one(self):
        """A small reproduction of the real do_POST collision:
        ``self._guardian_link_or_403(guid, jid)`` appears VERBATIM under
        BOTH ``if mga:`` and ``if mgs:``. A SINGLE waiver, keyed with only
        ONE branch's enclosing_if_text, covers ONLY that branch -- the
        OTHER, textually-identical call site still raises fresh, proving
        the two are tracked as genuinely separate call sites rather than
        one waiver silently also covering a second the reviewer never
        looked at."""
        self._with_waivers({
            ("do_POST", "self._guardian_link_or_403(guid, jid)", "if_test",
             "mga"): "reviewed for the mga branch only",
        })
        with self.assertRaises(ExtractionError) as caught:
            extract_routes(self._guardian_fixture())
        msg = str(caught.exception)
        self.assertIn("self._guardian_link_or_403(guid, jid)", msg)
        self.assertIn("unrecognised shape", msg)

    def test_both_branches_waived_separately_extracts_cleanly_and_each_is_exact_one_hit(self):
        """The legitimate, fully-reviewed case -- the control for the test
        above: with a SEPARATE, correctly-fingerprinted waiver for EACH
        branch, extraction succeeds and verify_waiver_usage sees each
        waiver hit exactly once (never both hits folded onto one key)."""
        self._with_waivers({
            ("do_POST", "self._guardian_link_or_403(guid, jid)", "if_test",
             "mga"): "reviewed for the mga branch",
            ("do_POST", "self._guardian_link_or_403(guid, jid)", "if_test",
             "mgs"): "reviewed for the mgs branch",
        })
        walker = extract_walker(self._guardian_fixture())
        self.assertEqual(
            {(r.method, r.template) for r in walker.routes.values()},
            {("POST", "/api/one/{}/availability"), ("POST", "/api/two/{}/offer")})
        for key in route_extract_module._AUDIT_WAIVERS:
            with self.subTest(waiver=key):
                self.assertEqual(len(walker.waiver_hits.get(key, ())), 1)

    def test_the_real_guardian_link_waivers_are_distinguished_by_branch(self):
        """The real server.py's own two ``self._guardian_link_or_403(guid,
        jid)`` waivers (do_POST, one per branch) are BOTH declared, BOTH
        hit exactly once, and carry DIFFERENT enclosing_if_text -- the
        concrete, non-invented proof that this mechanism is load-bearing
        today, not merely passing a synthetic fixture."""
        matching = [key for key in route_extract_module._AUDIT_WAIVERS
                   if key[0] == "do_POST"
                   and key[1] == "self._guardian_link_or_403(guid, jid)"]
        self.assertEqual(len(matching), 2)
        enclosing = {key[3] for key in matching}
        self.assertEqual(enclosing, {"mga", "mgs"})
        walker = extract_walker()
        for key in matching:
            with self.subTest(waiver=key):
                self.assertEqual(len(walker.waiver_hits.get(key, ())), 1)


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
        """A BARE call reached directly as the whole if-test stays exempt
        from raising -- but, since #202 repair round 4, finding 1, ONLY via
        an EXPLICIT, REVIEWED waiver, not the structural gap that used to
        exempt this shape without even inspecting its arguments (that gap
        is exactly what finding 1 closed: a genuinely NEW, unreviewed
        helper predicate reached the same way now raises -- see
        HelperPredicateCallEscapeTests). This fixture registers the SAME
        kind of waiver the real do_POST needs for its own `_operator_only`/
        `authorize` blanket gates (see _AUDIT_WAIVERS' round 4 entries),
        proving the mechanism, not a structural exemption, is what keeps a
        REVIEWED blanket guard quiet. Contrast
        ``test_a_call_reached_as_a_comparison_operand_raises_even_when_
        it_is_a_known_guard_shape`` immediately below: the SAME
        ``_supported_methods`` guard, reached through ``not in`` instead
        of bare truthiness, is a comparison operand and raises without a
        waiver either way -- this is round 3 finding E's boundary, kept
        exactly as it was."""
        saved = dict(route_extract_module._AUDIT_WAIVERS)
        route_extract_module._AUDIT_WAIVERS[
            ("do_GET", "self._operator_only(path)", "if_test", "")
        ] = "test-only: the reviewed blanket-guard shape, see the module's real entries"
        self.addCleanup(lambda: (
            route_extract_module._AUDIT_WAIVERS.clear(),
            route_extract_module._AUDIT_WAIVERS.update(saved)))
        routes = self._extract('''
            def do_GET(self):
                path = self.path.split("?", 1)[0]
                if self._operator_only(path):
                    return
                if path == "/api/x":
                    return self._send(1)
        ''')
        self.assertEqual([(r.method, r.template) for r in routes],
                         [("GET", "/api/x")])

    def test_a_call_reached_as_a_comparison_operand_raises_even_when_it_is_a_known_guard_shape(self):
        """#202 repair round 3, finding E: unlike the bare-call form above,
        ``self._supported_methods(path)`` reached as a comparator of
        ``not in`` DOES carry ``path`` into a comparison operand position,
        so it raises -- mirroring the real do_POST's own first line
        (``if "POST" not in self._supported_methods(path):``), which
        needed a fresh, reviewed ``_AUDIT_WAIVERS`` entry once this
        stopped being silent (see that entry's own comment). A being a
        long-established, reviewed guard shape does not exempt it from
        the SAME rule finding E applies generally -- it just means the
        waiver, once declared, is a one-line, well-understood fix."""
        with self.assertRaises(ExtractionError) as caught:
            self._extract('''
                def do_GET(self):
                    path = self.path.split("?", 1)[0]
                    if "GET" not in self._supported_methods(path):
                        return self._unmatched_route("GET")
                    if path == "/api/x":
                        return self._send(1)
            ''')
        self.assertIn("unrecognised shape", str(caught.exception))


# --------------------------------------------------------------------------- #
# #202 repair round 4, finding 1: a Call reached DIRECTLY as (or and/or/      #
# not-ed into) the WHOLE test -- never a comparison operand -- had its         #
# arguments left completely unscanned, a STRUCTURAL exemption rather than a    #
# reviewed one: `if self._is_hidden(path): ...` was neither classified nor     #
# raised on, while a real localhost Handler answers HTTP 200 for the path it   #
# hides. Distinct from round 2 finding A (calls flowing into taint as VALUES)  #
# and round 3 finding E (calls as Compare operands, gated by whether they sat  #
# inside an ast.Compare) -- this is the third and last position a Call can     #
# occupy relative to an if/while/ifexp/case-guard test, and the only one       #
# still unscanned before this round.                                          #
# --------------------------------------------------------------------------- #
class HelperPredicateCallEscapeTests(unittest.TestCase):
    def _raises(self, body, *substrings):
        with self.assertRaises(ExtractionError) as caught:
            extract_routes(_module(body))
        msg = str(caught.exception)
        for s in substrings:
            self.assertIn(s, msg)

    def test_bare_call_as_if_test_raises(self):
        """The reviewer's own reproduction, via the static extractor: a
        NEW, unreviewed helper predicate consuming the path, reached
        directly as the whole if-test -- no ast.Compare anywhere."""
        self._raises('''
            def do_GET(self):
                path = self.path.split("?", 1)[0]
                if self._is_hidden(path):
                    return self._send(1)
        ''', "unrecognised shape", "self._is_hidden(path)")

    def test_bare_call_as_if_test_answers_over_real_http_while_the_pre_fix_extractor_stayed_silent(self):
        """The reviewer's own words: "I verified a real localhost Handler
        where `if self._is_hidden(path): ...` returns HTTP 200 for
        /api/hidden, while `extract_routes(source)` returns [] with no
        error." Reproduced here against a REAL loopback server built from
        the IDENTICAL source the extractor call above raises on -- proving
        the static miss corresponded to a genuine, answering HTTP route,
        not a hypothetical shape. route_extract.py performs no runtime
        behaviour of its own, so the HTTP half of this proof holds
        regardless of the extractor fix's own presence; the static half
        (asserted above, in the previous test) is what the fix changes."""
        status, text = _real_http_probe('''
            def do_GET(self):
                path = self.path.split("?", 1)[0]
                if self._is_hidden(path):
                    return self._send(1)
                return self._send_json({"error": "not_found"}, 404)

            def _is_hidden(self, path):
                return path == "/api/hidden"
        ''', "GET", "/api/hidden")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(text), {"n": 1})

    def test_negated_bare_call_as_if_test_raises(self):
        """``if not GUARD(path):`` -- a UnaryOp wrapping the call, still
        directly forming the whole test."""
        self._raises('''
            def do_GET(self):
                path = self.path.split("?", 1)[0]
                if not self._is_allowed(path):
                    return self._send_status(403)
                return self._send(1)
        ''', "unrecognised shape")

    def test_boolop_combined_bare_calls_as_if_test_raises(self):
        """``if GUARD_A(path) and GUARD_B():`` -- and/or-combined, still no
        ast.Compare anywhere; both operands are examined."""
        self._raises('''
            def do_GET(self):
                path = self.path.split("?", 1)[0]
                if self._is_hidden(path) and self._extra_check():
                    return self._send(1)
        ''', "unrecognised shape")

    def test_bare_call_as_while_test_raises(self):
        """The same shape, reached from a ``while`` guard instead of
        ``if`` -- round 2 finding B's own node type, round 4 finding 1's
        own gap."""
        self._raises('''
            def do_GET(self):
                path = self.path.split("?", 1)[0]
                while self._is_hidden(path):
                    return self._send(1)
        ''', "unrecognised shape")

    def test_bare_call_as_ifexp_test_raises(self):
        """A ternary whose OWN test is a bare call touching the path. Goes
        through the pre-existing, ternary-specific message (route_extract
        does not model ternaries at all, regardless of what their test
        looks like) rather than the generic "unrecognised shape" wording
        the If/While/case-guard scans use -- a different, older check
        (#202 repair, invented-evasion track) that this finding's Call-arg
        scanning feeds into via the SAME `_direct_operand_names` call, not
        a new message of its own."""
        self._raises('''
            def do_GET(self):
                path = self.path.split("?", 1)[0]
                return self._send(1 if self._is_hidden(path) else 2)
        ''', "does not model ternaries", "'path'")

    def test_match_case_guard_with_bare_call_raises(self):
        """A match-case GUARD is neither ast.If nor ast.IfExp -- the SAME
        default-deny must still reach it (see the dedicated match-case-guard
        tests below for the non-call, comparison-shaped form of this same
        gap)."""
        self._raises('''
            def do_GET(self):
                path = self.path.split("?", 1)[0]
                mode = "x"
                match mode:
                    case _ if self._is_hidden(path):
                        return self._send(1)
        ''', "match-case guard", "unrecognised shape")

    def test_a_captured_group_as_a_bare_call_argument_still_does_not_raise(self):
        """The design principle this finding does NOT touch: an opaque
        captured group handed to a guard, reached the exact same way (a
        bare call as the whole if-test), still does not raise -- ``oav``
        itself never leaks out of ``oav.group(1))`` regardless of which of
        the three Call positions (value, compare operand, bare test) this
        module examines it from."""
        found = {(r.method, r.template) for r in extract_routes(_module('''
            def do_GET(self):
                path = self.path.split("?", 1)[0]
                oav = re.match(r"^/officials/([^/]+)/availability$", path)
                if oav:
                    if self._official_guard(oav.group(1)):
                        return self._send_status(403)
                    return self._send(1)

            def _official_guard(self, official_id):
                return False
        '''))}
        self.assertEqual(found, {("GET", "/officials/{}/availability")})

    def test_a_call_as_an_argument_to_another_call_still_does_not_raise(self):
        """The OTHER design principle this finding does not touch: a call
        reached as an ARGUMENT to some other call (never itself the tested
        condition, nor a comparison operand of it) is not scanned either --
        this module never looks at what a call's return value is
        subsequently passed to, only at how the value ITSELF was reached."""
        found = {(r.method, r.template) for r in extract_routes(_module('''
            def do_GET(self):
                path = self.path.split("?", 1)[0]
                if self._wrap_bool(self._is_hidden_unrelated()):
                    return self._send(1)
                if path == "/api/x":
                    return self._send(2)

            def _wrap_bool(self, value):
                return bool(value)

            def _is_hidden_unrelated(self):
                return False
        '''))}
        self.assertEqual(found, {("GET", "/api/x")})

    def test_the_real_server_extracts_with_no_new_raises(self):
        """The real server.py -- including do_POST's own bare
        ``authorize(role, path)``/``_operator_only`` blanket gates (never
        previously scanned as the whole if-test) and the guardian-link
        verification reached from FOUR different routes (two GET, two
        POST) -- must still extract cleanly: each is a reviewed, declared
        _AUDIT_WAIVERS entry, not a scoping hole."""
        walker = extract_walker()
        self.assertEqual(len(walker.routes), 239)
        self.assertEqual(walker.unreachable, [])


# --------------------------------------------------------------------------- #
# #202 repair round 4, finding 1 (match-case guards specifically): a          #
# ``case ... if TEST:`` guard is an expression on its own AST node type       #
# (``ast.match_case``), neither ``ast.If`` nor ``ast.IfExp`` -- the           #
# completeness scan never even visited it, and `_walk_stmt`'s own Match       #
# handling only ever inspects the match SUBJECT, a different expression       #
# entirely, so a guard on an UNTAINTED subject reached neither check.         #
# --------------------------------------------------------------------------- #
class MatchCaseGuardTests(unittest.TestCase):
    def _raises(self, body, *substrings):
        with self.assertRaises(ExtractionError) as caught:
            extract_routes(_module(body))
        msg = str(caught.exception)
        for s in substrings:
            self.assertIn(s, msg)

    def test_case_guard_on_a_tracked_subject_raises(self):
        """``match mode: case _ if path == "...":`` -- ``mode`` itself is
        NOT tracked, so `_walk_stmt`'s own match-SUBJECT check stays quiet;
        only the guard-specific scan this finding adds can catch it."""
        self._raises('''
            def do_GET(self):
                path = self.path.split("?", 1)[0]
                mode = "x"
                match mode:
                    case _ if path == "/api/evade-case-guard":
                        return self._send(1)
        ''', "match-case guard", "unrecognised shape")

    def test_case_guard_on_an_untainted_subject_and_guard_is_walked_normally(self):
        """The SAME statement shape must not be flagged when NEITHER the
        subject nor the guard touches a tracked name -- and a route nested
        inside the case body is still found, proving the guard scan does
        not merely tolerate this shape but walks it."""
        found = {(r.method, r.template) for r in extract_routes(_module('''
            def do_GET(self):
                path = self.path.split("?", 1)[0]
                mode = "x"
                flag = True
                match mode:
                    case _ if flag:
                        if path == "/api/inside-guarded-case":
                            return self._send(1)
        '''))}
        self.assertEqual(found, {("GET", "/api/inside-guarded-case")})

    def test_case_guard_calling_a_guard_with_a_captured_group_still_does_not_raise(self):
        """The opaque-extraction boundary applies here exactly as it does
        for an ordinary bare-call if-test (HelperPredicateCallEscapeTests'
        own captured-group test): a guard call reached DIRECTLY as the
        whole case-guard, with an opaque captured-group ARGUMENT, stays
        exempt -- ``oav`` itself never leaks out of ``oav.group(1)``.
        (Contrast: ``oav.group(1) == "special"`` reached DIRECTLY as the
        guard -- not wrapped in a call -- is a routing-relevant COMPARISON
        on the captured value itself and correctly still raises; that is
        not this test.)"""
        found = {(r.method, r.template) for r in extract_routes(_module('''
            def do_GET(self):
                path = self.path.split("?", 1)[0]
                oav = re.match(r"^/officials/([^/]+)/availability$", path)
                if oav:
                    mode = "x"
                    match mode:
                        case _ if self._official_guard(oav.group(1)):
                            return self._send_status(403)
                    return self._send(1)

            def _official_guard(self, official_id):
                return False
        '''))}
        self.assertEqual(found, {("GET", "/officials/{}/availability")})

    def test_the_real_server_has_no_match_statements_to_flag(self):
        """server.py uses no match/case at all today; every guard above is
        not vacuous only because InventedEvasionTests' own
        test_match_statement_on_a_tracked_subject_raises already proves the
        SUBJECT-level check fires on the real extractor's synthetic
        fixtures, and this module's zero-new-raises tests (in
        HelperPredicateCallEscapeTests and elsewhere) independently confirm
        the real file still extracts clean."""
        extract_routes()  # raises if any match/case guard were ever added


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


# --------------------------------------------------------------------------- #
# #202 repair round 3, finding E: _direct_operand_names/root_name never       #
# inspected a Call's ARGUMENTS, only its callee chain, so a Call wrapping the  #
# tracked name and used AS A COMPARISON OPERAND (``len(path) == N``,          #
# ``str(path) == lit``, any project-local wrapper reached the same way) was   #
# invisible: zero ExtractionError, zero recorded route -- and, when the       #
# branch is an earlier arm of a chain whose terminal else re-derives the      #
# subject (root cause 6's unconditional static tail), it silently dropped     #
# that chain's OWN static-tail route too, because an unclassified branch's    #
# orelse never reaches _walk_terminal_else. Deliberately narrow, like round   #
# 2's own additions: a BARE call (never reached as a comparison operand) that #
# merely takes the tracked name as an argument stays exempt (see              #
# UnknownShapesRaiseTests' own pair of tests just above) -- that boundary is  #
# what finding E's own wording ("used as a comparison operand") draws, and    #
# what keeps this from re-litigating round 2's already-reviewed blanket-gate  #
# exemption for ``_operator_only``/``authorize``.                             #
# --------------------------------------------------------------------------- #
class CallWrappedOperandTests(unittest.TestCase):
    def _raises(self, body, *substrings):
        with self.assertRaises(ExtractionError) as caught:
            extract_routes(_module(body))
        msg = str(caught.exception)
        for s in substrings:
            self.assertIn(s, msg)

    def test_len_of_path_compared_to_a_literal_raises(self):
        self._raises('''
            def do_GET(self):
                path = self.path.split("?", 1)[0]
                if len(path) == 21:
                    return self._send(1)
        ''', "unrecognised shape", "len(path) == 21")

    def test_str_of_path_compared_to_a_literal_raises(self):
        self._raises('''
            def do_GET(self):
                path = self.path.split("?", 1)[0]
                if str(path) == "/api/evade":
                    return self._send(1)
        ''', "unrecognised shape")

    def test_hash_of_path_compared_to_hash_of_a_literal_raises(self):
        self._raises('''
            def do_GET(self):
                path = self.path.split("?", 1)[0]
                if hash(path) == hash("/api/evade"):
                    return self._send(1)
        ''', "unrecognised shape")

    def test_call_wrapped_operand_nested_inside_a_boolop_still_raises(self):
        """``in_compare`` must survive passing through a BoolOp/UnaryOp on
        its way to the actual Call, not reset to False -- ``(flag or
        len(path)) == 5`` still reaches ``len(path)`` FROM a genuine
        comparator position, just one layer further in."""
        self._raises('''
            def do_GET(self):
                path = self.path.split("?", 1)[0]
                flag = False
                if (flag or len(path)) == 5:
                    return self._send(1)
        ''', "unrecognised shape")

    def test_project_local_wrapper_function_raises(self):
        self._raises('''
            def _wrap(p):
                return p

            def do_GET(self):
                path = self.path.split("?", 1)[0]
                if _wrap(path) == "/api/evade":
                    return self._send(1)
        ''', "unrecognised shape")

    def test_a_blanket_call_taking_the_raw_name_still_raises_when_compared(self):
        """The general form of the gap, not just three hardcoded builtins:
        mirrors the real do_POST's own ``authorize(role, path)`` gate --
        but COMPARED this time, not bare -- to prove the rule is about the
        SHAPE (comparison operand), not an allowlist of which call it is."""
        self._raises('''
            def do_POST(self):
                path = self.path.split("?", 1)[0]
                role = "whatever"
                if authorize(role, path) == "denied":
                    return self._send_status(403)
                return self._send(1)
        ''', "unrecognised shape")

    def test_call_wrapped_test_no_longer_silently_drops_a_later_sibling(self):
        """Round 3's own hunt reported a call-wrapped elif silently
        dropping an entire GET route list when it preceded an otherwise
        correctly-classified sibling. Whatever the precise mechanism, the
        fix's fail-CLOSED contract makes the outcome unambiguous either
        way: the whole extraction raises loudly the moment the call-
        wrapped branch is reached, rather than possibly returning a
        partial, silently-wrong route list."""
        self._raises('''
            def do_GET(self):
                path = self.path.split("?", 1)[0]
                if str(path) == "/api/evade":
                    return self._send(1)
                elif path == "/api/normal-sibling":
                    return self._send(2)
        ''', "unrecognised shape")

    def test_call_wrapped_branch_no_longer_hides_the_chains_static_tail(self):
        """Mirrors the real _serve_static shape (root cause 6): an earlier
        call-wrapped arm used to make the walker skip _walk_terminal_else
        for the WHOLE chain, silently dropping the unconditional-tail
        route right along with the call-wrapped arm's own -- DEMONSTRATED
        against the pre-round-3 code (not merely theorised): the fixture
        below returned only ``{"/shell", "/shell/"}``, the static tail
        silently missing, with no exception at all. Now raises instead of
        silently narrowing the route set."""
        self._raises('''
            def do_GET(self):
                path = self.path.split("?", 1)[0]
                return self._serve_static(path)

            def _serve_static(self, path):
                if path in ("/shell", "/shell/"):
                    rel = "shell.html"
                elif str(path) == "/api/evade":
                    rel = "evade.html"
                else:
                    rel = path.lstrip("/")
                target = (STATIC_DIR / rel).resolve()
                return target
        ''', "unrecognised shape")

    def test_a_captured_group_handed_to_a_guard_call_still_does_not_raise(self):
        """The design principle _direct_operand_names' own docstring states
        must survive this fix: a captured group handed to an unrelated
        guard is not a routing decision, even reached as a bare argument
        (not just when compared) -- the opaque-extraction boundary
        _tracked_mentions shares with _mentions_tracked is what keeps
        ``oav`` itself from leaking out of ``oav.group(1)``."""
        found = {(r.method, r.template) for r in extract_routes(_module('''
            def do_GET(self):
                path = self.path.split("?", 1)[0]
                oav = re.match(r"^/officials/([^/]+)/availability$", path)
                if oav:
                    if self._official_guard(oav.group(1)) == "denied":
                        return self._send_status(403)
                    return self._send(1)

            def _official_guard(self, official_id):
                return "ok"
        '''))}
        self.assertEqual(found, {("GET", "/officials/{}/availability")})

    def test_the_real_server_extracts_with_no_new_raises(self):
        """The real server.py -- including _handle_reassign(_v2)'s own
        authorisation-target-list construction (``targets.append((dest[0],
        ...))``), its ``check_body(b, **SCHEMA[combo])`` validation, its
        ``len(target) > 2`` name collision between the outer tracked
        ``target`` parameter and the inner for-loop's own local of the
        same name, and do_POST's own ``'POST' not in
        self._supported_methods(path)`` 405/Allow admission check -- must
        still extract cleanly: each is a reviewed, declared
        _AUDIT_WAIVERS entry (see that dict's own round-3 comments), not
        a scoping hole."""
        walker = extract_walker()
        self.assertEqual(len(walker.routes), 239)
        self.assertEqual(walker.unreachable, [])


# --------------------------------------------------------------------------- #
# #202 repair round 3, finding F: neither root_name nor the fixed-point taint #
# loop recognised ast.NamedExpr (the walrus operator) -- a decision bound     #
# via ``(n := EXPR)`` and then compared, in the SAME if-test or a LATER one,  #
# was invisible either way: zero exception, zero route.                       #
# --------------------------------------------------------------------------- #
class WalrusOperatorTests(unittest.TestCase):
    def _raises(self, body, *substrings):
        with self.assertRaises(ExtractionError) as caught:
            extract_routes(_module(body))
        msg = str(caught.exception)
        for s in substrings:
            self.assertIn(s, msg)

    def test_walrus_bound_and_compared_in_the_same_if_test_raises(self):
        self._raises('''
            def do_GET(self):
                path = self.path.split("?", 1)[0]
                if (tail := path.split("/")[-1]) == "evade":
                    return self._send(1)
        ''', "unrecognised shape", "tail")

    def test_walrus_bound_in_one_if_and_tested_in_a_later_if_raises(self):
        self._raises('''
            def do_GET(self):
                path = self.path.split("?", 1)[0]
                if (tail := path.split("/")[-1]):
                    pass
                if tail == "evade":
                    return self._send(1)
        ''', "unrecognised shape")

    def test_a_walrus_not_derived_from_the_path_is_not_flagged(self):
        found = {(r.method, r.template) for r in extract_routes(_module('''
            def do_GET(self):
                path = self.path.split("?", 1)[0]
                if path == "/api/count":
                    if (n := len(KNOWN)) > 5:
                        return self._send(1)
                    return self._send(2)
        '''))}
        self.assertEqual(found, {("GET", "/api/count")})

    def test_the_real_server_extracts_with_no_new_raises(self):
        """server.py uses no walrus operator today; the two tests above are
        not vacuous only because they are genuinely reproduced against a
        fixture, exactly as WhileLoopGuardTests' own real-server control
        documents for the analogous while-loop case."""
        walker = extract_walker()
        self.assertEqual(len(walker.routes), 239)
        self.assertEqual(walker.unreachable, [])


# --------------------------------------------------------------------------- #
# #202 repair round 3, finding G: the fixed-point taint loop and              #
# _audit_dispatch_helper_calls both, by design, only ever examined            #
# ASSIGNMENT RHS values and self._handle_*/_dispatch_* ATTRIBUTE-call         #
# syntax -- a class-level dispatch-table lookup invoked as a BARE,            #
# UNASSIGNED statement was covered by neither, contrasted directly against    #
# the ASSIGNED form of the identical expression, which already raised.       #
# --------------------------------------------------------------------------- #
class BareStatementDispatchCallTests(unittest.TestCase):
    def _raises(self, body, *substrings):
        with self.assertRaises(ExtractionError) as caught:
            extract_routes(_module(body))
        msg = str(caught.exception)
        for s in substrings:
            self.assertIn(s, msg)

    def test_bare_dict_get_dispatch_statement_raises(self):
        self._raises('''
            _ROUTE_TABLE = {"/api/evade": "handler_one"}

            def do_GET(self):
                path = self.path.split("?", 1)[0]
                self._maybe_dispatch(_ROUTE_TABLE.get(path))
                return self._send(0)

            def _maybe_dispatch(self, name):
                return None
        ''', "unlisted call")

    def test_the_assigned_form_of_the_identical_expression_already_raised(self):
        """The control: binding the SAME expression to a local first was
        already caught by the fixed-point loop before this fix -- proves
        the gap this closes is specifically the ASSIGNMENT-only
        restriction, not the taint analysis itself."""
        self._raises('''
            _ROUTE_TABLE = {"/api/evade": "handler_one"}

            def do_GET(self):
                path = self.path.split("?", 1)[0]
                outcome = _ROUTE_TABLE.get(path)
                self._maybe_dispatch(outcome)
                return self._send(0)

            def _maybe_dispatch(self, name):
                return None
        ''', "unlisted call")

    def test_bare_getattr_dispatch_statement_raises(self):
        self._raises('''
            def do_GET(self):
                path = self.path.split("?", 1)[0]
                suffix = path.rsplit("/", 1)[-1]
                getattr(self, "_handle_evade_" + suffix, self._noop)()

            def _noop(self):
                return None

            def _handle_evade_target(self):
                return self._send(1)
        ''', "unlisted call")

    def test_a_bare_call_unrelated_to_any_tracked_name_does_not_raise(self):
        found = {(r.method, r.template) for r in extract_routes(_module('''
            def do_GET(self):
                path = self.path.split("?", 1)[0]
                if path == "/api/ping":
                    api.record_ping()
                    return self._send(1)
        '''))}
        self.assertEqual(found, {("GET", "/api/ping")})

    def test_the_real_server_extracts_with_no_new_raises(self):
        walker = extract_walker()
        self.assertEqual(len(walker.routes), 239)
        self.assertEqual(walker.unreachable, [])


# --------------------------------------------------------------------------- #
# #202 repair round 3, finding H (documented, NOT fixed -- see route_extract  #
# .py's module docstring, KNOWN LIMITATIONS): the walker only harvests        #
# FunctionDef nodes lexically inside class Handler, and walks do_GET/do_POST  #
# as its two entry points. A decorator wrapping either with its OWN routing   #
# logic (a separate module-level function) would never be walked -- nothing   #
# here would notice. NOT exploitable against the real server.py today; this   #
# test IS that proof, re-run every time. If it ever fails, the latent gap the #
# docstring names may have just become live -- see that section before        #
# dismissing this failure as routine.                                        #
# --------------------------------------------------------------------------- #
class DecoratorLimitationGuardTests(unittest.TestCase):
    def test_do_get_and_do_post_carry_no_decorators_today(self):
        walker = extract_walker()
        for name in ("do_GET", "do_POST"):
            with self.subTest(entry_point=name):
                fn = walker.functions[name]
                self.assertEqual(
                    fn.decorator_list, [],
                    f"{name} now has a decorator -- #202 repair round 3 "
                    "finding H's documented, previously-undemonstrated gap "
                    "(a decorator wrapping an entry point with its own "
                    "routing logic is never walked) may now be LIVE. See "
                    "route_extract.py's module docstring, KNOWN "
                    "LIMITATIONS, before dismissing this failure.")


# --------------------------------------------------------------------------- #
# #202 repair round 4, finding 2: the fixed-point taint loop in                #
# _audit_function only ever recognised ast.Assign/ast.AnnAssign (and, round 3  #
# finding F, ast.NamedExpr) as a binding event -- a `for candidate in          #
# (path,):`, `with holder(path) as candidate:`, or `candidate += path` bound a #
# new local carrying the path with NOTHING added to `tracked`, so a later      #
# branch keyed on that local was neither classified nor raised on: zero        #
# exception, zero route. See _binding_value_and_targets' own docstring for the #
# unifying fix.                                                                #
# --------------------------------------------------------------------------- #
class UnmodelledBindingFormTests(unittest.TestCase):
    def _raises(self, body, *substrings):
        with self.assertRaises(ExtractionError) as caught:
            extract_routes(_module(body))
        msg = str(caught.exception)
        for s in substrings:
            self.assertIn(s, msg)

    # -- pre-fix escape, reproduced via git stash (not re-run here: git stash
    # cannot be invoked from inside a test process) -- the transcript below is
    # what running each of these fixtures produced against the code as it
    # stood immediately before this finding's fix, captured verbatim:
    #
    #   FOR:           no raise. routes = []
    #   WITH:          no raise. routes = []
    #   AUGASSIGN:     no raise. routes = []
    #
    # each is now either extracted or refused -- never silent -- asserted
    # below against the FIXED code, which is what a test process can still
    # verify for itself on every run.

    def test_for_loop_target_raises(self):
        """``for candidate in (path,):`` -- the reviewer's own first
        reproduction. ``_propagates_taint``'s fallback treats the tuple
        literal as carrying ``path`` (conservative over-approximation, see
        _binding_value_and_targets' own docstring), so ``candidate`` joins
        ``tracked`` and the later test on it is caught by the completeness
        scan, unrecognised."""
        self._raises('''
            def do_GET(self):
                path = self.path.split("?", 1)[0]
                for candidate in (path,):
                    if candidate == "/api/evade-for":
                        return self._send(1)
        ''', "candidate", "unrecognised shape")

    def test_for_loop_tuple_target_raises(self):
        """Tuple-unpacking FOR target (`for candidate, extra in (...)`) --
        the SAME generalised ``ast.walk(target)`` leaf-collection an
        ordinary ``a, b = ...`` assignment already gets, reused for a For
        target with no separate unpacking rule."""
        self._raises('''
            def do_GET(self):
                path = self.path.split("?", 1)[0]
                for candidate, extra in ((path, 1),):
                    if candidate == "/api/evade-for-tuple":
                        return self._send(1)
        ''', "candidate", "unrecognised shape")

    def test_comprehension_target_raises(self):
        """A list-comprehension's own `for` clause is an ``ast.comprehension``
        node, structurally distinct from ``ast.For`` -- the same fixed-point
        loop must recognise both. ``hits`` itself joins ``tracked`` once its
        comprehension element narrows a tracked subject, then the later
        truthiness test on ``hits`` is unrecognised."""
        self._raises('''
            def do_GET(self):
                path = self.path.split("?", 1)[0]
                hits = [c for c in (path,) if c == "/api/evade-comp"]
                if hits:
                    return self._send(1)
        ''', "hits", "unrecognised shape")

    def test_with_as_target_raises(self):
        """``with holder(path) as candidate:`` -- the reviewer's own second
        reproduction. ``holder(path)`` is itself an unlisted call whose
        argument mentions the tracked ``path`` -- the SAME fail-closed rule
        _propagates_taint already applies to an assignment's RHS, now
        reached from a with-item's ``context_expr`` instead."""
        self._raises('''
            def do_GET(self):
                path = self.path.split("?", 1)[0]
                with holder(path) as candidate:
                    if candidate == "/api/evade-with":
                        return self._send(1)
        ''', "holder(path)", "unlisted call")

    def test_with_as_tuple_target_raises(self):
        """Tuple-unpacking WITH target (`with ctx() as (candidate, extra):`)
        -- same generalised leaf-collection as the For-tuple case above,
        reused for withitem.optional_vars."""
        self._raises('''
            def do_GET(self):
                path = self.path.split("?", 1)[0]
                with holder(path) as (candidate, extra):
                    if candidate == "/api/evade-with-tuple":
                        return self._send(1)
        ''', "holder(path)", "unlisted call")

    def test_with_no_as_binds_nothing_and_is_not_flagged(self):
        """A ``with EXPR:`` with no ``as`` clause binds no target -- there is
        nothing for taint to reach, so this must NOT raise merely because
        the with-item exists; the INNER, ordinary ``path`` test is still
        correctly recognised as a route. (The context expression's own call
        touching ``path`` with no binding at all is a documented, narrower,
        out-of-scope shape -- see _binding_value_and_targets' docstring --
        not a finding-2 regression.)"""
        found = {(r.method, r.template) for r in extract_routes(_module('''
            def do_GET(self):
                path = self.path.split("?", 1)[0]
                with nullcontext(path):
                    if path == "/api/normal-with-noas":
                        return self._send(1)
        '''))}
        self.assertEqual(found, {("GET", "/api/normal-with-noas")})

    def test_augassign_target_raises(self):
        """``candidate += path`` -- the reviewer's own third reproduction.
        ``candidate`` did not exist as a tracked name before this statement;
        the fixed-point loop must still recognise the AugAssign RHS as
        carrying ``path`` and add the target."""
        self._raises('''
            def do_GET(self):
                path = self.path.split("?", 1)[0]
                candidate = ""
                candidate += path
                if candidate == "/api/evade-augassign":
                    return self._send(1)
        ''', "candidate", "unrecognised shape")

    def test_asyncfor_target_is_modelled_identically_to_for(self):
        """``ast.AsyncFor`` cannot be DEMONSTRATED the way the other six
        shapes above are: ``async for``/``async with`` are only legal
        syntax inside an ``async def`` function, and
        ``_DispatchWalker.__init__`` only ever harvests ``ast.FunctionDef``
        nodes into ``self.functions`` (``ast.AsyncFunctionDef`` is a
        SIBLING node type, not a subclass) -- an async entry point or
        delegate helper is already invisible to this module for a separate,
        pre-existing reason (it would fail the unrelated "entry point not
        found" / "delegates to self.x() in a form the walker does not
        follow" checks first), so no fixture can put a live ``async for``
        inside a function this module actually walks. What IS checkable
        directly: ``_binding_value_and_targets`` -- the one place this
        binding form is recognised -- resolves an ``ast.AsyncFor`` node
        identically to the ``ast.For`` sibling it shares both fields with,
        so the SAME fixed-point loop covers it structurally the day
        anything upstream of this function ever starts walking async code."""
        for_node = ast.parse("for c in (path,): pass").body[0]
        async_src = "async def _f():\n    async for c in (path,): pass\n"
        async_for_node = ast.parse(async_src).body[0].body[0]
        self.assertIsInstance(for_node, ast.For)
        self.assertIsInstance(async_for_node, ast.AsyncFor)
        for_result = _binding_value_and_targets(for_node)
        async_result = _binding_value_and_targets(async_for_node)
        self.assertIsNotNone(async_result)
        self.assertEqual(ast.dump(async_result[0]), ast.dump(for_result[0]))
        self.assertEqual(ast.dump(async_result[1][0]), ast.dump(for_result[1][0]))

    def test_every_binding_node_type_is_reachable_by_ast_walk(self):
        """Sanity check on the module's own declared list: every type named
        in BINDING_NODE_TYPES really is a node ``ast.walk`` will visit
        inside an ordinary function body (not, e.g., a node python's grammar
        only permits somewhere ``ast.walk`` does not descend into) -- a
        cross-check on the constant itself, independent of any one fixture
        above happening to exercise it. ``ast.AsyncFor`` is parsed from its
        own ``async def`` snippet (see test_asyncfor_target_is_modelled_
        identically_to_for's own docstring for why it cannot share the
        others' ordinary function body)."""
        src = '''
def do_GET(self):
    path = self.path
    a = path
    b: str = path
    (c := path)
    d = ""
    d += path
    for e in (path,):
        pass
    [f for f in (path,)]
    with nullcontext(path) as g:
        pass
'''
        found_types = {type(n) for n in ast.walk(ast.parse(src))
                       if isinstance(n, BINDING_NODE_TYPES)}
        async_src = "async def _f():\n    async for h in (path,): pass\n"
        found_types |= {type(n) for n in ast.walk(ast.parse(async_src))
                        if isinstance(n, BINDING_NODE_TYPES)}
        self.assertEqual(found_types, set(BINDING_NODE_TYPES))

    # -- real HTTP: the reviewer's own three named reproductions, shown       #
    # reachable over an ACTUAL socket from the identical source handed to     #
    # extract_routes() above -- not merely a static claim about what would    #
    # happen. route_extract.py performs no runtime behaviour of its own (a    #
    # STATIC analyzer), so this half of the proof is independent of whether   #
    # the fix above has landed yet -- the server-shaped fixture answers the   #
    # same way regardless; what changes is only whether extract_routes()      #
    # stays silent about it.                                                 #
    def test_for_loop_escape_answers_over_real_http(self):
        status, text = _real_http_probe('''
            def do_GET(self):
                path = self.path.split("?", 1)[0]
                for candidate in (path,):
                    if candidate == "/api/evade-for":
                        return self._send(1)
                return self._send_json({"error": "not_found"}, 404)
        ''', "GET", "/api/evade-for")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(text), {"n": 1})

    def test_with_as_escape_answers_over_real_http(self):
        status, text = _real_http_probe('''
            def do_GET(self):
                path = self.path.split("?", 1)[0]
                with nullcontext(path) as candidate:
                    if candidate == "/api/evade-with":
                        return self._send(1)
                return self._send_json({"error": "not_found"}, 404)
        ''', "GET", "/api/evade-with")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(text), {"n": 1})

    def test_augassign_escape_answers_over_real_http(self):
        status, text = _real_http_probe('''
            def do_GET(self):
                path = self.path.split("?", 1)[0]
                candidate = ""
                candidate += path
                if candidate == "/api/evade-augassign":
                    return self._send(1)
                return self._send_json({"error": "not_found"}, 404)
        ''', "GET", "/api/evade-augassign")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(text), {"n": 1})

    def test_the_real_server_extracts_with_no_new_raises(self):
        """The real server.py must still extract cleanly after finding 2's
        fixed-point extension -- none of its real For/With/AugAssign/
        comprehension usage (there is plenty, e.g. every ``for pid in
        _player_ids(...)``-shaped loop elsewhere in the codebase) binds a
        name FROM a tracked subject the way the synthetic fixtures above
        do, so nothing new should join ``tracked`` and nothing new should
        raise."""
        walker = extract_walker()
        self.assertEqual(len(walker.routes), 239)
        self.assertEqual(walker.unreachable, [])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
