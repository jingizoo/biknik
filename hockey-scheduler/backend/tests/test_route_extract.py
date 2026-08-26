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
import inspect
import json
import re
import textwrap
import threading
import types
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


def _real_http_probe_with_globals(handler_body: str, extra_globals: dict,
                                  method: str, path: str, body=None):
    """As :func:`_real_http_probe`, but with ``extra_globals`` merged into the
    exec namespace BEFORE the class body runs -- for a fixture whose
    reviewed waiver text calls a bare, module-level name (``authorize(role,
    path)``, ``required_permission(path)``, the SAME shape server.py's own
    ``from .authz import authorize, required_permission`` produces) rather
    than a ``self.`` method. Keeping these as real free functions, not
    ``self.`` stand-ins defined inline, is what lets the do_POST BODY TEXT
    stay byte-identical to what the static ``_module()``-based test hands
    ``extract_routes`` -- the same "static and live examine identical code"
    guarantee ``_real_http_probe``'s own docstring describes, extended to a
    fixture that needs bare globals ``_real_http_probe`` does not supply.
    """
    src = ("class _ProbeHandler(BaseHTTPRequestHandler):\n"
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
    ns = {"BaseHTTPRequestHandler": BaseHTTPRequestHandler, "json": json,
         **extra_globals}
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


def _waive_matching_node(cleanup_registrar, body, matching_text, fn_name="do_GET"):
    """Parse ``body`` (the SAME dedented source handed to ``_module``), find
    the ONE ``ast.Call``/``ast.Subscript`` node in ``fn_name`` whose own
    ``ast.unparse()`` equals ``matching_text``, compute ITS real
    ``_waiver_key``, install a TEMPORARY ``_AUDIT_WAIVERS`` entry for
    exactly that key (registered with ``cleanup_registrar`` -- normally a
    TestCase's own ``self.addCleanup`` -- so it is removed even if the
    caller's test body itself raises or fails an assertion), and return the
    parsed, ready-to-extract source text (the SAME ``_module(body)`` result
    ``extract_routes``/``extract_walker`` needs).

    #202 repair round 13, finding 1 (external review): retiring the
    structural captured-arg exemption (see route_extract.py's
    ``_TRUSTED_BINDING_SOURCES``, own module comment) means several
    PRE-EXISTING tests elsewhere in this file -- for OTHER mechanisms
    entirely (compositional taint, the generic ``_is_callee`` climb,
    execution-control scans) -- that used a captured-id-to-``api``-facade
    call/subscript as a convenient "definitely not a routing decision"
    fixture now need an EXPLICIT waiver to keep exercising what they
    actually test, the same way any other unmodelled call in a synthetic
    fixture already needs one. This helper computes the key via the REAL
    ``_waiver_key`` rather than a hand-typed guess, so a key typo cannot
    make a test pass for the wrong reason."""
    src = _module(body)
    tree = ast.parse(src)
    handler = next(n for n in tree.body
                   if isinstance(n, ast.ClassDef) and n.name == "Handler")
    fn = next(n for n in handler.body
             if isinstance(n, ast.FunctionDef) and n.name == fn_name)
    parents = route_extract_module._build_parent_map(fn)
    node = next(n for n in ast.walk(fn)
               if isinstance(n, (ast.Call, ast.Subscript))
               and ast.unparse(n) == matching_text)
    key = route_extract_module._waiver_key(fn_name, node, parents)
    route_extract_module._AUDIT_WAIVERS[key] = "test-only waiver"
    cleanup_registrar(route_extract_module._AUDIT_WAIVERS.pop, key, None)
    return src


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
                # #202 repair round 5, finding 2b: a literal marker, NOT
                # `table` itself -- `self._send(table)` would pass the
                # DICT-LOOKUP RESULT (a `self.`-call argument, so
                # DELIBERATELY out of `_propagates_taint`'s ``captured``
                # exemption's reach -- see that function's own docstring)
                # into a self-call now reached by the Return scan; that is
                # a real, reviewable-in-server.py shape (see
                # WaiverTaintPropagationTests' `fn`/`coach` waivers for the
                # genuine article), not what THIS fixture means to
                # exercise -- this branch is about `{...}.get(action)`
                # being recognised as a dispatch shape at all, unrelated
                # to what value the already-selected branch reports back.
                return self._send(12)

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
        self.assertEqual(len(walker.routes), 241)
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
        self.assertEqual(len(walker.routes), 241)
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
        """The real server.py, unmodified: each of the 117 declared waivers
        (119 before PR #427's final blocker, then +1 and -3 across its four
        rounds -- verified commit by commit, because the prose breakdown
        below had gone stale against the number it explains and a count
        nobody can re-derive is not a gate:

          ccdb7b4  119 -> 120  net +1: the family's own-side resolution was
                               hoisted out of the availability-summary leaf
                               to the whole `m` block, which RE-KEYED two
                               entries rather than adding them
                               (`api.store.get_game(gid)`, `sub_game is not
                               None`), and `/board` gained one genuinely new
                               site --
                               `lineup_visibility.own_side(role, own_team,
                               *side_ids)`;
          e8953ac  120 -> 119  the availability-summary leaf's inline
                               `role in (Role.COACH, Role.PLAYER) and
                               own_team and (team_id != own_team)` if_test
                               DELETED when that narrowing moved to the
                               facade;
          round 3  119 -> 117  the same-shaped `role == Role.COACH ...`
                               if_test deleted from BOTH the
                               substitute-candidates and the
                               substitute-addable leaf, for the same reason
                               and in favour of the same facade projection.

        The three service-call waivers those deleted `if`s used to feed were
        not removed but REWORDED IN PLACE: the calls are still there and
        still carry a client-supplied hint. What changed is that the hint is
        now adjudicated against the trusted side inside the facade rather
        than compared to it here, which is a different justification for the
        same site, not a different site. The pre-#427 119 breaks down as:
        116 through #426 round 2 -- see below for that count's own
        breakdown -- plus 3 #205 blocker 1 additions, once the
        availability-summary sub-scope re-fetched the already-selected
        game (`gid`, captured by `m`) to resolve the caller's own team
        against it (`game_scoped_own_team_id`) instead of the permanent
        `player.team_id` pointer: one for the re-fetch itself
        (`api.store.get_game(gid)`), one for the `if sub_game is not
        None:` not-found guard around it, and one for the resolver call
        whose result feeds `own_team` -- see those three entries, tagged
        "#205 blocker 1", for the full rationale. The pre-#205 116 breaks
        down as: 114 through round 13 -- see below for that count's own
        breakdown -- plus 2 round-2 (#426 external review finding 2)
        additions, once
        do_POST gained its own two bare-statement audit calls
        (`self._audit_sensitive_post_denial(path, None, user_id)` at the
        resolve_role() 401 refusal, `self._audit_sensitive_post_denial(
        path, role, user_id)` at the authorize() 403 refusal) each taking
        the tracked `path` -- the SAME "a call consuming a tracked name
        needs its own review" shape `required_permission(path)`'s own
        PRE-EXISTING waiver already covers, see those two entries'
        own comments for the full rationale. The pre-round-2 114 breaks
        down as: 77 through round 9 -- see below for that count's own
        breakdown --
        plus 37 round-13, finding-1 additions, once the general PROVENANCE-
        based exemption in `_propagates_taint` was retired entirely in
        favour of exact-site review: every one of the real file's 37
        captured-only call/subscript sites the retired exemption used to
        cover now needs its own individual waiver, the SAME discipline
        round 9's finding 1 already applied to the two sites that never
        fit that exemption's shape at all -- see the `_AUDIT_WAIVERS`
        entries themselves, tagged "round 13 finding 1", for each one's
        own review). The pre-round-13 77 breaks down as: 18 from rounds
        2-3 -- 2 pre-existing + 2 pre-existing ternaries + 6
        round-2 finding A additions + 8 round-3 finding E additions -- plus
        11 round-4 finding 1 additions, once a Call reached DIRECTLY as the
        whole test had its arguments scanned too, plus 10 round-5 finding 1
        additions, once a WAIVED call's result stopped losing its taint --
        see _propagates_taint's own docstring, "A WAIVER SILENCES THE CALL,
        NOT THE RESULT" -- plus 13 round-5 finding 2b additions MINUS 1
        finding-2b removal, once Return statements were audited at all and
        exposed a class of self-owned mutation helpers
        (`_guarded_mutation`) and locally-selected callables (`call`/
        `mapper`/`deleter`/`fn`/`coach`) that needed their own review, and
        made one PRE-EXISTING waiver -- `_to_v1.get(kind, lambda r: r)` --
        redundant with finding 2b's own new `captured` exemption in
        `_propagates_taint` (see that function's own docstring) rather
        than needing its own entry any more -- plus 23 round-6 finding 1
        additions MINUS 3 finding-1 removals, once `self.path` was
        recognised at any depth (not only as the bare operand) and the
        bottom-of-function fallback in `_propagates_taint` stopped
        reaching past the opaque-extraction boundary: the additions are
        GET query-string filter/scope parameters `_dispatch_get`'s own
        `parse_qs(urlparse(self.path).query)` idiom newly makes visible,
        plus two `SCHEMA[combo]` keyword-unpack Subscripts now
        independently audited the same way an unlisted Call already was;
        the removals are three waivers -- `self._operator_only(guard)` and
        two `_handle_setup_v2` `call(...)` entries -- that turned out to
        have been needed only because the OLD blind-scan fallback wrongly
        read straight through an opaque capture, not because the
        expressions they covered were ever genuinely tracked -- plus 2
        round-6 finding 2 additions, once an except handler whose own
        enclosing try body contains a tracked operation is audited the
        same way finding 6c's function-wide raise check already was: the
        two `_handle_reassign`/`_handle_reassign_v2` `except BodyError as
        exc:` handlers for `check_body`'s own already-reviewed
        `_V{1,2}_REASSIGN_SCHEMA[combo]`-keyed validation) -- plus 2
        round-7 finding 1 additions, once a For/AsyncFor loop's own
        `.iter` is audited as an execution-control sink independent of
        target binding: `_handle_reassign`/`_handle_reassign_v2`'s own
        `for target in targets:` loop is the SAME already-reviewed
        `targets` authorisation-target list (see this dict's round-3/
        round-5 `targets`-related waiver groups) examined at one more
        position, its own loop statement -- is consulted for precisely
        the one line it names -- proves the instrumentation is wired all
        the way through _propagates_taint AND the ast.If/ast.IfExp/
        ast.While/ast.For/ast.AsyncFor/ast.match_case scan, not just one
        of them. Plus 2 round-9 finding 1 additions (external review):
        once the general `captured` exemption in `_propagates_taint` was
        narrowed to a small, explicit, reviewed allowlist of call TARGETS
        (`_captured_arg_safe_callee`, currently just the `api` facade --
        see that function's own docstring), two of the real file's 39
        captured-only call/subscript sites fell outside it and needed
        their own review again: `_to_v1.get(kind, lambda r: r)`
        (RESTORED -- the SAME call the paragraph above this one made
        redundant at round 5 finding 2b, live again now that the general
        exemption no longer reaches a LOCAL dict of response-shape
        mappers) and `kind.capitalize()` (NEW -- a builtin string method
        called ON the captured value itself, see that entry's own
        comment for why it still needs a waiver rather than a shape-based
        carve-out).
        Each key is now a 4-tuple (#202 repair round 4, finding 3:
        function, text, parent shape, enclosing if) rather than the
        original 2-tuple -- WaiverRelocationFingerprintTests below is the
        dedicated proof for what the extra two parts catch that this
        exact-one-hit check alone would not."""
        walker = extract_walker()
        self.assertEqual(len(route_extract_module._AUDIT_WAIVERS), 116)
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
        self.assertEqual(len(walker.routes), 241)
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
        self.assertEqual(len(walker.routes), 241)
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
        self.assertEqual(len(walker.routes), 241)
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
        self.assertEqual(len(walker.routes), 241)
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

    def _with_waivers(self, waivers: dict):
        """As ``ExecutionControlAndDataFlowTests._with_waivers``: temporarily
        ADDS `waivers` on top of the real ``_AUDIT_WAIVERS`` (not a full
        replacement -- this class's other tests still need the real
        server.py's own waivers to stay absent so THEIR fixtures raise
        correctly), restored even if the test body raises."""
        saved = dict(route_extract_module._AUDIT_WAIVERS)
        route_extract_module._AUDIT_WAIVERS.update(waivers)
        self.addCleanup(lambda: (
            route_extract_module._AUDIT_WAIVERS.clear(),
            route_extract_module._AUDIT_WAIVERS.update(saved)))

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
        scan, unrecognised.

        #202 repair round 7, finding 1: the loop's OWN ``.iter`` is now
        ALSO audited directly, as an execution-control sink in its own
        right (see ``_audit_function``'s new ``ast.For``/``ast.AsyncFor``
        branch) -- and a bare tuple literal falls to
        ``_direct_operand_names``'s own default-deny fallback (round 5
        finding 5) the SAME way it would for an ``ast.If``'s ``.test``, so
        THIS check now fires FIRST, before the fixed-point loop's
        SEPARATE conclusion about ``candidate`` is ever reached by the
        completeness scan below -- naming ``path`` (found directly in
        ``.iter``), not ``candidate``. A waiver on the iterable position
        stands in for a real, reviewed shape with no execution-control
        meaning of its own (see ``ExecutionControlAndDataFlowTests``' own
        "for/async for iterables" section for the genuinely
        execution-control-relevant shapes this round's finding is
        actually about), isolating THIS test's original claim -- a For's
        TARGET gets taint propagated from its iterable (round 4 finding
        2) -- from round 7 finding 1's newer, independent check on the
        SAME position: waiving the newer check does not silently disable
        the older one, proven directly below rather than merely assumed."""
        src = _module('''
            def do_GET(self):
                path = self.path.split("?", 1)[0]
                for candidate in (path,):
                    if candidate == "/api/evade-for":
                        return self._send(1)
        ''')
        self._with_waivers({
            ("do_GET", "(path,)", "for", ""):
                "test-only: isolates round 4 finding 2's own target-taint "
                "claim (this fixture's whole point) from round 7 finding "
                "1's newer, unrelated execution-control audit of the SAME "
                "position -- see this test's own docstring",
        })
        with self.assertRaises(ExtractionError) as caught:
            extract_routes(src)
        msg = str(caught.exception)
        self.assertIn("candidate", msg)
        self.assertIn("unrecognised shape", msg)

    def test_for_loop_iterable_itself_raises_before_any_waiver(self):
        """The UNWAIVED counterpart of the test above: without a waiver on
        the iterable position, round 7 finding 1's new check is what
        actually raises for this exact fixture -- naming the ITERABLE
        (``path``, found directly in ``(path,)``), never reaching
        ``candidate`` at all. Pins the CURRENT, earlier-firing message so
        a regression that silently stopped auditing ``.iter`` here would
        show up as a message change, not just a vanished raise."""
        self._raises('''
            def do_GET(self):
                path = self.path.split("?", 1)[0]
                for candidate in (path,):
                    if candidate == "/api/evade-for":
                        return self._send(1)
        ''', "path", "unrecognised shape", "(path,)")

    def test_for_loop_tuple_target_raises(self):
        """Tuple-unpacking FOR target (`for candidate, extra in (...)`) --
        the SAME generalised ``ast.walk(target)`` leaf-collection an
        ordinary ``a, b = ...`` assignment already gets, reused for a For
        target with no separate unpacking rule.

        #202 repair round 7, finding 1: as ``test_for_loop_target_raises``
        above, a waiver on the iterable position isolates this test's
        original target-taint claim from the newer, independent
        execution-control check that would otherwise fire first (see that
        test's own docstring for the full reasoning)."""
        src = _module('''
            def do_GET(self):
                path = self.path.split("?", 1)[0]
                for candidate, extra in ((path, 1),):
                    if candidate == "/api/evade-for-tuple":
                        return self._send(1)
        ''')
        self._with_waivers({
            ("do_GET", "((path, 1),)", "for", ""):
                "test-only: isolates round 4 finding 2's own target-taint "
                "claim (this fixture's whole point) from round 7 finding "
                "1's newer, unrelated execution-control audit of the SAME "
                "position -- see test_for_loop_target_raises' own "
                "docstring",
        })
        with self.assertRaises(ExtractionError) as caught:
            extract_routes(src)
        msg = str(caught.exception)
        self.assertIn("candidate", msg)
        self.assertIn("unrecognised shape", msg)

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
        correctly recognised as a route. Isolated from the context
        expression's OWN taint (a SEPARATE concern -- #202 repair round 6,
        finding 2 audits that directly now, see
        ``ExecutionControlAndDataFlowTests.
        test_with_context_expression_touching_path_raises`` below, closing
        what THIS test's own docstring used to document as an
        intentionally out-of-scope shape for round 4's finding 2) via a
        context expression that mentions nothing tracked at all."""
        found = {(r.method, r.template) for r in extract_routes(_module('''
            def do_GET(self):
                path = self.path.split("?", 1)[0]
                with nullcontext():
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
        self.assertEqual(len(walker.routes), 241)
        self.assertEqual(walker.unreachable, [])


# --------------------------------------------------------------------------- #
# #202 repair round 5, finding 1 (external review, 19:36): a waiver used to   #
# answer TWO questions at once -- "may this call expression appear without    #
# raising" AND "does the value it returns still carry taint" -- by returning  #
# False (the SAME verdict a provably-unrelated call gets) the instant a       #
# waived call was reached inside _propagates_taint's scan. The reviewer's     #
# own repro: `perm = required_permission(path)` left EXACTLY where round 2    #
# finding A's own waiver reviews it, PLUS a brand new                         #
# `if perm == Permission.MANAGE_SCHEDULE: return ...` immediately after --    #
# extraction stayed silent (same 239 routes, all 29 waivers still exactly-    #
# one-hit) while a synthetic live branch answered real HTTP 200. See          #
# _propagates_taint's own docstring, "A WAIVER SILENCES THE CALL, NOT THE     #
# RESULT", for the fix and why it is the narrower one (continued propagation  #
# through the SAME existing machinery, not a new "safe result" system).       #
# --------------------------------------------------------------------------- #
class WaiverTaintPropagationTests(unittest.TestCase):
    # -- pre-fix escape, reproduced via git stash (not re-run here: git
    # stash cannot be invoked from inside a test process) -- the transcript
    # below is what running the fixture in test_waived_call_result_is_no_
    # longer_silently_untracked produced against the code as it stood
    # immediately before this finding's fix, captured verbatim:
    #
    #   NO RAISE. routes = []
    #
    # against the FIXED code (asserted below, which a test process can
    # still verify for itself on every run) the SAME source raises.

    def test_waived_call_result_is_no_longer_silently_untracked(self):
        """The reviewer's own reproduction, via the static extractor: the
        REAL waiver's own fingerprint (function ``do_POST``, exact text
        ``required_permission(path)``, ``assign_rhs``, enclosing
        ``not authorize(role, path)``) reached verbatim, with a NEW
        routing-relevant use of the assigned ``perm`` added immediately
        after -- exactly the reviewer's own repro shape, just with a
        string literal standing in for the real ``Permission`` enum
        (irrelevant to the extractor, which never executes this source)."""
        with self.assertRaises(ExtractionError) as caught:
            extract_routes(_module('''
                def do_POST(self):
                    path = self.path.split("?", 1)[0]
                    role = "x"
                    if not authorize(role, path):
                        perm = required_permission(path)
                        if perm == "manage_schedule":
                            return self._send(1)
                        return self._send_status(403)
                    return self._send(2)
            '''))
        msg = str(caught.exception)
        self.assertIn("perm", msg)
        self.assertIn("unrecognised shape", msg)

    def test_waived_call_result_escape_answers_over_real_http(self):
        """The SAME ``do_POST`` body text as the static test above --
        byte-identical, via :func:`_real_http_probe_with_globals` -- run as
        a real loopback server with working ``authorize``/
        ``required_permission`` supplied as real module-level globals (the
        same free-function shape server.py's own
        ``from .authz import authorize, required_permission`` produces, not
        ``self.`` stand-ins, which would change the waiver-fingerprint text
        the static test depends on): a request that ``authorize`` refuses,
        for a path whose ``required_permission`` is the probed value,
        reaches the hidden branch and answers 200 -- proving the static
        miss (demonstrated in the previous test, pre-fix) corresponded to a
        genuine, answering HTTP route. route_extract.py performs no runtime
        behaviour of its own, so this half of the proof holds regardless of
        the extractor fix's own presence."""
        status, text = _real_http_probe_with_globals('''
            def do_POST(self):
                path = self.path.split("?", 1)[0]
                role = "x"
                if not authorize(role, path):
                    perm = required_permission(path)
                    if perm == "manage_schedule":
                        return self._send(1)
                    return self._send_status(403)
                return self._send(2)

            def _send_status(self, code):
                self._send_json({"error": "forbidden"}, code)
        ''', {
            "authorize": lambda role, path: False,
            "required_permission": lambda path: "manage_schedule",
        }, "POST", "/api/anything", {})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(text), {"n": 1})

    def test_a_call_provably_unrelated_to_any_tracked_name_is_still_untainted(self):
        """The design principle this finding does NOT touch: a call whose
        arguments never mention a tracked name at all (not merely a
        waived one) still leaves its target OUT of ``tracked`` -- this
        finding only changes what happens when the call DOES mention a
        tracked name AND is waived, never the "provably unrelated" path
        one branch below it in ``_propagates_taint``."""
        found = {(r.method, r.template) for r in extract_routes(_module('''
            def do_GET(self):
                path = self.path.split("?", 1)[0]
                ics = api.calendar_feed_ics("static-token")
                if ics == "special":
                    return self._send(1)
                if path == "/api/x":
                    return self._send(2)
        '''))}
        self.assertEqual(found, {("GET", "/api/x")})

    def test_the_real_servers_own_named_exemption_stays_clean(self):
        """The reviewer's own required confirmation: a waived call whose
        result truly never escapes into routing must stay clean, not
        start raising. server.py:2653's real, UNMODIFIED
        ``perm = required_permission(path)`` -- the exact call the round-2
        finding A waiver above reviews -- is used only two ways anywhere
        in ``do_POST``: interpolated into a 403 message string
        (``f"...{perm.value}..."``) and a ternary picking the error body's
        ``details.required`` field (``perm.value if perm else None``,
        server.py:2659). Neither compares ``perm`` for ROUTING, so neither
        is a routing decision -- both are reviewed, dedicated round-5
        waivers (see ``_AUDIT_WAIVERS``'s own round-5, finding-1 entries
        for ``do_POST``), and the real file extracts cleanly with them."""
        walker = extract_walker()
        self.assertEqual(len(walker.routes), 241)
        self.assertEqual(walker.unreachable, [])
        # The two round-5 do_POST waivers this docstring names are each
        # consulted for precisely the one line they name -- not zero
        # (dormant), not more than one (too broad) -- proving THIS
        # specific exemption, not merely "the whole file extracted".
        for key in (("do_POST", "perm", "ifexp_test",
                    "not authorize(role, path)"),
                   ("do_POST", "violation is not None", "if_test", "")):
            with self.subTest(waiver=key):
                self.assertEqual(len(walker.waiver_hits.get(key, ())), 1)

    def test_the_real_server_extracts_with_no_new_raises(self):
        """The real server.py -- including the four ``_handle_reassign``/
        ``_handle_reassign_v2`` authorisation-target bookkeeping call
        chains this finding's fix newly reaches (``parent``/``dest``
        lookups, the request-body field reads that feed them, and the
        #369 context/scope checks over the now-tracked ``targets`` list)
        -- must still extract cleanly: each newly-reached call site is a
        reviewed, declared ``_AUDIT_WAIVERS`` entry (10 new ones, taking
        the total from 29 to 39 -- see WaiverFingerprintTests' own pinned
        count), not a scoping hole."""
        walker = extract_walker()
        self.assertEqual(len(walker.routes), 241)
        self.assertEqual(walker.unreachable, [])
        self.assertEqual(len(route_extract_module._AUDIT_WAIVERS), 116)


# --------------------------------------------------------------------------- #
# #202 repair round 5, finding 2 (external review, 19:36 + 19:48): "direct    #
# subscript and return dispatch remain silent" -- two sub-shapes. (a) a Call  #
# whose ``.func`` is itself an ``ast.Subscript`` indexed by a tracked name    #
# (``PREDICATES[path]()``) -- the existing scans look at a Call's arguments   #
# or receiver name CHAIN, never at a Subscript used AS the callee. (b) a bare #
# ``return <expr>`` -- neither the fixed-point loop (assignment-only) nor the #
# bare-Expr scan (round 3, finding G; non-Return statements) ever visits      #
# ``Return.value`` at all, so ``return ROUTES[path]()`` and the "broader      #
# returned-helper form", ``return self._route(path)`` (an ARBITRARY,          #
# uncatalogued ``self.`` method -- not a ``_handle_*``/``_dispatch_*`` name   #
# ``_audit_dispatch_helper_calls`` would ever flag), both went unexamined.    #
# --------------------------------------------------------------------------- #
class SubscriptCalleeAndReturnDispatchTests(unittest.TestCase):
    # -- pre-fix escapes, reproduced via git stash (not re-run here: git
    # stash cannot be invoked from inside a test process) -- the transcript
    # below is what running each of the three fixtures below produced
    # against the code as it stood immediately before this finding's fix
    # (i.e. round 5, finding 1's own commit), captured verbatim:
    #
    #   2a-subscript-callee:      NO RAISE. routes = []
    #   2b-return-subscript-call: NO RAISE. routes = []
    #   2b-return-self-route:     NO RAISE. routes = []
    #
    # each now raises against the FIXED code, asserted below, which a test
    # process can still verify for itself on every run.

    def _raises(self, body, *substrings):
        with self.assertRaises(ExtractionError) as caught:
            extract_routes(_module(body))
        msg = str(caught.exception)
        for s in substrings:
            self.assertIn(s, msg)

    def test_subscript_callee_indexed_by_tracked_name_raises(self):
        """Finding 2a, the reviewer's own reproduction: a Call whose own
        CALLEE is a Subscript keyed on the tracked path
        (``PREDICATES[path]()``), reached directly as the whole if-test.
        Before this fix, ``root_name``'s generic Subscript-unwrapping
        (``node = node.value``, meant for a receiver CHAIN like
        ``SOME_DICT[key].attr``) discarded the ``.slice`` -- exactly the
        piece that decides which callable gets invoked here -- so the
        walk resolved straight past ``path`` to ``PREDICATES`` and found
        nothing tracked."""
        self._raises('''
            def do_GET(self):
                path = self.path.split("?", 1)[0]
                if PREDICATES[path]():
                    return self._send(1)
        ''', "unrecognised shape", "PREDICATES[path]()")

    def test_return_of_subscript_call_raises(self):
        """Finding 2b, sub-shape (a) combined with the reviewer's own
        ``ROUTES[path]()`` repro, reached as a bare ``return`` -- neither
        the fixed-point loop (assignment-only) nor the round-3 bare-Expr
        scan (non-Return statements) ever visited ``Return.value``."""
        self._raises('''
            def do_GET(self):
                path = self.path.split("?", 1)[0]
                return ROUTES[path]()
        ''', "unlisted call", "ROUTES[path]()")

    def test_return_of_arbitrary_self_method_raises(self):
        """Finding 2b, sub-shape (b) -- the reviewer's "broader returned-
        helper form": ``self._route`` is an ARBITRARY, uncatalogued
        ``self.`` method (not ``_handle_*``/``_dispatch_*``-prefixed, so
        ``_audit_dispatch_helper_calls``'s delegation detector never even
        looks at it) that can itself serve a hidden route."""
        self._raises('''
            def do_GET(self):
                path = self.path.split("?", 1)[0]
                return self._route(path)

            def _route(self, path):
                if path == "/api/hidden":
                    return self._send(1)
                return self._send(2)
        ''', "unlisted call", "self._route(path)")

    def test_subscript_callee_escape_answers_over_real_http(self):
        status, text = _real_http_probe_with_globals('''
            def do_GET(self):
                path = self.path
                if PREDICATES[path]():
                    return self._send(1)
                return self._send_json({"error": "not_found"}, 404)
        ''', {"PREDICATES": {"/api/hidden": lambda: True}},
                                                     "GET", "/api/hidden")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(text), {"n": 1})

    def test_return_of_subscript_call_escape_answers_over_real_http(self):
        status, text = _real_http_probe_with_globals('''
            def do_GET(self):
                path = self.path
                return ROUTES[path](self)
        ''', {"ROUTES": {"/api/hidden": lambda handler: handler._send(1)}},
                                                     "GET", "/api/hidden")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(text), {"n": 1})

    def test_return_of_arbitrary_self_method_escape_answers_over_real_http(self):
        status, text = _real_http_probe_with_globals('''
            def do_GET(self):
                path = self.path
                return self._route(path)

            def _route(self, path):
                if path == "/api/hidden":
                    return self._send(1)
                return self._send_json({"error": "not_found"}, 404)
        ''', {}, "GET", "/api/hidden")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(text), {"n": 1})

    def test_an_already_followed_delegation_reached_via_return_still_does_not_raise(self):
        """The design principle this finding does NOT touch: a Return
        whose value is a KNOWN, ALREADY-FOLLOWED delegation call
        (``return self._serve_static(path)``, a real, ordinary
        SAME_PATH_DELEGATES call server.py itself makes) must not be
        treated as an unlisted call over the SAME node the delegation
        detector already resolved -- ``_propagates_taint``'s own
        ``followed`` parameter is what prevents that double-flagging."""
        found = {(r.method, r.template) for r in extract_routes(_module('''
            def do_GET(self):
                path = self.path.split("?", 1)[0]
                if path.startswith("/api/"):
                    return self._unmatched_route("GET")
                return self._serve_static(path)

            def _serve_static(self, path):
                if path in ("/shell", "/shell/"):
                    return self._send(1)
        '''))}
        self.assertIn(("GET", "/shell"), found)

    def test_a_non_self_call_consuming_only_captured_data_in_a_return_does_not_raise(self):
        """The design principle finding 2b's Return audit exists to
        reach: a captured id, bound to a local FIRST rather than inlined,
        handed to an ORDINARY (non-``self.``) service call as part of the
        function's own terminal answer -- semantically identical to the
        SAME capture inlined (``api.get_x(m.group(1))``, already exempt),
        just spelled with the bind split out. Not exempt STRUCTURALLY any
        more (#202 repair round 13, finding 1 retired that mechanism; see
        route_extract.py's ``_TRUSTED_BINDING_SOURCES``, own module
        comment) -- an explicit waiver, installed here, is what keeps this
        test isolating what it actually checks: that the Return audit
        REACHES this call at all, not whether a captured-only call is
        structurally trusted. Mirrors the real server.py shape ``return
        self._send_api(api.get_game(gid))`` -- see this class's own
        test_the_real_server_extracts_with_no_new_raises below for the
        genuine, at-scale article (each instance individually waived,
        see route_extract.py's own "round 13 finding 1" entries), reached
        dozens of times over."""
        src = _waive_matching_node(self.addCleanup, '''
            def do_GET(self):
                path = self.path.split("?", 1)[0]
                api = STATE.api
                m = re.match(r"^/api/items/([^/]+)$", path)
                if m:
                    gid = m.group(1)
                    return self._send_api(api.get_item(gid))
        ''', "api.get_item(gid)")
        found = {(r.method, r.template) for r in extract_routes(src)}
        self.assertEqual(found, {("GET", "/api/items/{}")})

    def test_a_self_call_consuming_only_captured_data_in_a_return_still_raises(self):
        """The OTHER half of the same design principle: the ``captured``
        exemption is deliberately narrower than "any call whose arguments
        are all captured ids" -- it excludes EVERY ``self.`` call,
        regardless of what its arguments are, because an arbitrary
        ``self.`` method (however deep the attribute chain reaching it,
        see ``_is_self_call``) is exactly where a hidden dispatcher could
        live in this class. A captured id handed to an uncatalogued
        ``self.`` method must still raise."""
        self._raises('''
            def do_GET(self):
                path = self.path.split("?", 1)[0]
                m = re.match(r"^/api/items/([^/]+)$", path)
                if m:
                    gid = m.group(1)
                    return self._maybe_hidden_route(gid)

            def _maybe_hidden_route(self, gid):
                return self._send(1)
        ''', "unlisted call", "self._maybe_hidden_route(gid)")

    def test_the_real_servers_own_named_exemption_stays_clean(self):
        """The real server.py's own instance of the pattern the previous
        two tests isolate: ``_serve_static``'s ``ctype = CONTENT_TYPES.get(
        target.suffix, ...)`` (an opaque Path-PROPERTY extraction,
        unaffected by this finding) followed by
        ``self.send_header('Content-Type', ctype)`` (a bare-Expr
        statement, NOT this finding's own new Return scan, but reached by
        the SAME `_propagates_taint` call and so a real regression risk
        for the SAME reason) -- DEMONSTRATED to regress during this
        finding's own development: an earlier draft's ``captured``/
        ``_TERMINAL_RESPONSE_SENDERS`` exemptions were checked BEFORE
        ``_mentions_tracked`` fired, which let a call `_mentions_tracked`
        would have called "provably unrelated" anyway (this one) instead
        fall through to `_propagates_taint`'s cruder bottom-of-function
        fallback (which does not honour any opaque-extraction boundary),
        newly finding `target` tracked INSIDE `target.suffix` and wrongly
        deriving `ctype` as tainted -- fixed by gating both new exemptions
        on `_mentions_tracked` having already fired (see
        `_propagates_taint`'s own inline comment at that exact point)."""
        walker = extract_walker()
        self.assertEqual(len(walker.routes), 241)
        self.assertEqual(walker.unreachable, [])

    def test_the_real_server_extracts_with_no_new_raises(self):
        """The real server.py -- including the ~13 new self-owned mutation
        helper (`_guarded_mutation`) and locally-selected-callable
        (`call`/`mapper`/`deleter`/`fn`/`coach`) Return sites this
        finding's fix newly reaches, and the removal of one PRE-EXISTING
        waiver the new `captured` exemption made redundant -- must still
        extract cleanly: each newly-reached call site is a reviewed,
        declared ``_AUDIT_WAIVERS`` entry (51 total as of THIS finding --
        later rounds' own waivers grow the count further; see
        WaiverFingerprintTests' own pinned count and docstring for the
        CURRENT exact accounting), not a scoping hole."""
        walker = extract_walker()
        self.assertEqual(len(walker.routes), 241)
        self.assertEqual(walker.unreachable, [])
        self.assertEqual(len(route_extract_module._AUDIT_WAIVERS), 116)


# --------------------------------------------------------------------------- #
# #202 repair round 5, finding 5 (external review, 19:48): ordinary           #
# expression wrappers still hide tracked path decisions. ``_direct_operand_   #
# names``'s ``root_name`` recognises a small, explicit list of pass-through   #
# shapes (self.path, a walrus target, a bare Name, an Attribute/Subscript     #
# RECEIVER chain, a Call's callee/arguments) and silently returns ``None``    #
# -- no inspection at all -- for anything else. The reviewer's own three      #
# same-source forms (a string concatenation, an f-string, and a ternary       #
# reached as a comparison OPERAND rather than the whole test) each answered   #
# live HTTP 200 while extraction stayed silent: the gate is permissive for    #
# ANY wrapper shape it has not been explicitly taught, not just these three.  #
# --------------------------------------------------------------------------- #
class DefaultDenyExpressionOperandTests(unittest.TestCase):
    # -- pre-fix escapes, reproduced via git stash (not re-run here: git
    # stash cannot be invoked from inside a test process) -- the transcript
    # below is what running each of the three fixtures below produced
    # against the code as it stood immediately before this finding's fix
    # (round 5, finding 2's own commit), captured verbatim:
    #
    #   binop concatenation:  NO RAISE. routes = []
    #   f-string:             NO RAISE. routes = []
    #   ternary as operand:   NO RAISE. routes = []
    #
    # each now raises against the FIXED code, asserted below, which a test
    # process can still verify for itself on every run. The control fixture
    # (an opaque captured group nested inside an f-string operand) was
    # UNAFFECTED before this fix and remains unaffected after it -- verified
    # directly in test_an_opaque_capture_nested_in_an_unrecognised_wrapper_
    # still_does_not_raise, not merely asserted in this comment.

    def _raises(self, body, *substrings):
        with self.assertRaises(ExtractionError) as caught:
            extract_routes(_module(body))
        msg = str(caught.exception)
        for s in substrings:
            self.assertIn(s, msg)

    def test_string_concatenation_operand_raises(self):
        """The reviewer's own first reproduction: ``path + ""`` is an
        ``ast.BinOp``, a node type ``root_name`` never recognised at all --
        not judged safe, simply never looked at."""
        self._raises('''
            def do_GET(self):
                path = self.path.split("?", 1)[0]
                if path + "" == "/api/hidden":
                    return self._send(1)
        ''', "unrecognised shape", "path")

    def test_fstring_operand_raises(self):
        """The reviewer's own second reproduction: ``f"{path}"`` is an
        ``ast.JoinedStr``, likewise never recognised."""
        self._raises('''
            def do_GET(self):
                path = self.path.split("?", 1)[0]
                if f"{path}" == "/api/hidden":
                    return self._send(1)
        ''', "unrecognised shape", "path")

    def test_ternary_as_comparison_operand_raises(self):
        """The reviewer's own third reproduction: a ternary reached as a
        COMPARISON OPERAND (``(path if True else "") == "..."``), distinct
        from the ALREADY-modelled case of a ternary forming the WHOLE
        test (that shape has its own, older, dedicated check and its own
        message -- 'route_extract does not model ternaries' -- this is a
        ternary buried one level deeper, inside another expression)."""
        self._raises('''
            def do_GET(self):
                path = self.path.split("?", 1)[0]
                if (path if True else "") == "/api/hidden":
                    return self._send(1)
        ''', "unrecognised shape", "path")

    def test_string_concatenation_escape_answers_over_real_http(self):
        status, text = _real_http_probe('''
            def do_GET(self):
                path = self.path.split("?", 1)[0]
                if path + "" == "/api/hidden":
                    return self._send(1)
                return self._send_json({"error": "not_found"}, 404)
        ''', "GET", "/api/hidden")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(text), {"n": 1})

    def test_fstring_escape_answers_over_real_http(self):
        status, text = _real_http_probe('''
            def do_GET(self):
                path = self.path.split("?", 1)[0]
                if f"{path}" == "/api/hidden":
                    return self._send(1)
                return self._send_json({"error": "not_found"}, 404)
        ''', "GET", "/api/hidden")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(text), {"n": 1})

    def test_ternary_as_operand_escape_answers_over_real_http(self):
        status, text = _real_http_probe('''
            def do_GET(self):
                path = self.path.split("?", 1)[0]
                if (path if True else "") == "/api/hidden":
                    return self._send(1)
                return self._send_json({"error": "not_found"}, 404)
        ''', "GET", "/api/hidden")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(text), {"n": 1})

    def test_an_opaque_capture_nested_in_an_unrecognised_wrapper_still_does_not_raise(self):
        """The design principle this finding does NOT touch: a captured
        group remains genuinely detached even when nested inside one of
        the PREVIOUSLY-silent wrapper shapes this finding now inspects --
        ``_tracked_mentions`` (the SAME name-collecting function, SAME
        opaque-extraction boundary the Call-argument scan already relies
        on) is reused here rather than a new, parallel rule, so
        ``f"prefix-{oav.group(1)}"`` used as a comparison operand still
        does not surface ``oav``."""
        found = {(r.method, r.template) for r in extract_routes(_module('''
            def do_GET(self):
                path = self.path.split("?", 1)[0]
                oav = re.match(r"^/officials/([^/]+)/availability$", path)
                if oav:
                    if f"prefix-{oav.group(1)}" == self._some_const():
                        return self._send_status(403)
                    return self._send(1)

            def _some_const(self):
                return "x"
        '''))}
        self.assertEqual(found, {("GET", "/officials/{}/availability")})

    def test_an_unrelated_binop_not_touching_path_is_still_unaffected(self):
        """A control: string concatenation of two names NEITHER of which
        is tracked must not raise -- the default-deny only fires once
        ``_tracked_mentions`` actually finds something, exactly as the
        Call-argument scan's own existing behaviour already works."""
        found = {(r.method, r.template) for r in extract_routes(_module('''
            def do_GET(self):
                path = self.path.split("?", 1)[0]
                prefix = "x"
                suffix = "y"
                if prefix + suffix == "xy":
                    return self._send(1)
                if path == "/api/real":
                    return self._send(2)
        '''))}
        self.assertEqual(found, {("GET", "/api/real")})

    def test_the_real_server_extracts_with_no_new_raises(self):
        """The real server.py contains no BinOp/JoinedStr/IfExp-as-operand
        wrapper around any tracked name -- must still extract cleanly with
        no new waivers needed: 239 routes, the SAME 51 waivers as finding
        2 left it (see WaiverFingerprintTests' own pinned count)."""
        walker = extract_walker()
        self.assertEqual(len(walker.routes), 241)
        self.assertEqual(walker.unreachable, [])
        self.assertEqual(len(route_extract_module._AUDIT_WAIVERS), 116)


# --------------------------------------------------------------------------- #
# #202 repair round 5, finding 6 (external review, 19:48): exception-driven   #
# routing is uninspected. Three parts: (a) an ``ast.Assert``'s own ``.test``  #
# was never audited the way an ``ast.If``'s already is; (b) an ``ast.Raise``  #
# whose exception ARGUMENT mentions tracked data was never audited; (c) the   #
# binding model omits ``ExceptHandler.name`` entirely, so a name bound from   #
# ``except ... as name:`` can carry exception-payload taint into routing-     #
# relevant code with no error of any kind. (c) is the HARD part -- true       #
# cross-statement data-flow (which raise site feeds which handler) is out of  #
# reach for this kind of walker without a much larger rework -- so it is      #
# fixed with a reasonably-scoped, HONEST, deliberately fail-closed COARSE     #
# over-approximation instead (see route_extract.py's own KNOWN LIMITATIONS    #
# section, finding 6c's entry, for exactly how precise this is and is not),   #
# the same honesty standard finding H (round 3) already set for a residual    #
# gap this module chose not to fully close.                                   #
# --------------------------------------------------------------------------- #
class ExceptionDrivenRoutingTests(unittest.TestCase):
    # -- pre-fix escapes, reproduced via git stash (not re-run here: git
    # stash cannot be invoked from inside a test process) -- the transcript
    # below is what running each of the reviewer's own two fixtures below
    # produced against the code as it stood immediately before this
    # finding's fix (round 5, finding 5's own commit), captured verbatim:
    #
    #   assert / except AssertionError:            NO RAISE. routes = []
    #   raise ValueError(path) / except ... as candidate: NO RAISE. routes = []
    #
    # both now raise against the FIXED code, asserted below, which a test
    # process can still verify for itself on every run.

    def _with_waivers(self, waivers: dict):
        """As WaiverFingerprintTests' own helper of the same name (kept
        local rather than shared -- this is the only class in this module
        that needs it outside that one) -- temporarily replace the
        module's real _AUDIT_WAIVERS with exactly `waivers`, restored even
        if the test body raises."""
        saved = dict(route_extract_module._AUDIT_WAIVERS)
        route_extract_module._AUDIT_WAIVERS.clear()
        route_extract_module._AUDIT_WAIVERS.update(waivers)
        self.addCleanup(lambda: (
            route_extract_module._AUDIT_WAIVERS.clear(),
            route_extract_module._AUDIT_WAIVERS.update(saved)))

    def _raises(self, body, *substrings):
        with self.assertRaises(ExtractionError) as caught:
            extract_routes(_module(body))
        msg = str(caught.exception)
        for s in substrings:
            self.assertIn(s, msg)

    def test_assert_tests_a_tracked_subject_raises(self):
        """Finding 6a, the reviewer's own first reproduction: an assert
        inside a try, its failure caught by ``except AssertionError:`` --
        a real, live routing decision (assert succeeds -> one answer,
        fails -> the except's answer) reached via a statement type this
        module's completeness scan never inspected."""
        self._raises('''
            def do_GET(self):
                path = self.path.split("?", 1)[0]
                try:
                    assert path == "/api/hidden"
                except AssertionError:
                    return self._send_status(404)
                return self._send(1)
        ''', "unrecognised shape", "path")

    def test_raise_of_tracked_value_raises(self):
        """Finding 6b: ``raise ValueError(path)`` hands the tracked path
        DIRECTLY to an unlisted exception constructor -- the SAME
        unlisted-call rule ``_propagates_taint`` already applies to a
        Return/bare-Expr value, now also reached from a Raise."""
        self._raises('''
            def do_GET(self):
                path = self.path.split("?", 1)[0]
                raise ValueError(path)
        ''', "unlisted call", "ValueError(path)")

    def test_except_as_binding_after_a_tracked_raise_raises(self):
        """Finding 6c, the reviewer's own second reproduction, combined:
        ``raise ValueError(path)`` (already caught by 6b alone here, since
        the raise is itself unwaived) followed by ``except ValueError as
        candidate: if str(candidate) == "/api/hidden": ...`` -- either
        mechanism raising closes the escape; see
        test_a_waived_raise_still_flags_its_named_handler below for 6c's
        OWN, independent value when 6b's mechanism alone would not fire."""
        self._raises('''
            def do_GET(self):
                path = self.path.split("?", 1)[0]
                try:
                    raise ValueError(path)
                except ValueError as candidate:
                    if str(candidate) == "/api/hidden":
                        return self._send(1)
                    return self._send(2)
        ''')

    def test_assert_escape_answers_over_real_http_both_ways(self):
        """Both sides of the reviewer's own assert repro answer for real:
        the assert succeeding and failing are two DIFFERENT live routes,
        not a hypothetical branch extraction merely failed to prove
        unreachable."""
        hit_status, hit_text = _real_http_probe('''
            def do_GET(self):
                path = self.path.split("?", 1)[0]
                try:
                    assert path == "/api/hidden"
                except AssertionError:
                    return self._send_json({"error": "not_found"}, 404)
                return self._send(1)
        ''', "GET", "/api/hidden")
        self.assertEqual(hit_status, 200)
        self.assertEqual(json.loads(hit_text), {"n": 1})
        miss_status, _ = _real_http_probe('''
            def do_GET(self):
                path = self.path.split("?", 1)[0]
                try:
                    assert path == "/api/hidden"
                except AssertionError:
                    return self._send_json({"error": "not_found"}, 404)
                return self._send(1)
        ''', "GET", "/api/other")
        self.assertEqual(miss_status, 404)

    def test_raise_except_as_escape_answers_over_real_http_both_ways(self):
        """Both sides of the reviewer's own raise/except-as repro answer
        for real: the exception payload genuinely selects the response."""
        hit_status, hit_text = _real_http_probe('''
            def do_GET(self):
                path = self.path.split("?", 1)[0]
                try:
                    raise ValueError(path)
                except ValueError as candidate:
                    if str(candidate) == "/api/hidden":
                        return self._send(1)
                    return self._send(2)
        ''', "GET", "/api/hidden")
        self.assertEqual(hit_status, 200)
        self.assertEqual(json.loads(hit_text), {"n": 1})
        miss_status, miss_text = _real_http_probe('''
            def do_GET(self):
                path = self.path.split("?", 1)[0]
                try:
                    raise ValueError(path)
                except ValueError as candidate:
                    if str(candidate) == "/api/hidden":
                        return self._send(1)
                    return self._send(2)
        ''', "GET", "/api/other")
        self.assertEqual(miss_status, 200)
        self.assertEqual(json.loads(miss_text), {"n": 2})

    def test_a_waived_raise_still_flags_its_named_handler(self):
        """Finding 6c's OWN independent value, isolated: even when the
        raise site itself is waived (judged, on review, not itself a
        routing decision -- 6b's own mechanism stays silent), the named
        except handler downstream is STILL flagged, because 6c's
        function-wide over-approximation does not depend on 6b having
        fired. This is the coarse, deliberately-conservative design the
        module's own KNOWN LIMITATIONS section (finding 6c's entry)
        describes: it cannot prove `candidate` is safe just because the
        raise that produced it was reviewed, so it does not try to."""
        src = _module('''
            def do_GET(self):
                path = self.path.split("?", 1)[0]
                try:
                    raise ValueError(path)
                except ValueError as candidate:
                    if str(candidate) == "/api/hidden":
                        return self._send(1)
                    return self._send(2)
        ''')
        self._with_waivers({
            ("do_GET", "ValueError(path)", "raise", ""):
                "test-only: pretend this raise site was reviewed and "
                "judged not a routing decision, to isolate finding 6c's "
                "OWN check from finding 6b's",
        })
        with self.assertRaises(ExtractionError) as caught:
            extract_routes(src)
        msg = str(caught.exception)
        self.assertIn("except", msg)
        self.assertIn("candidate", msg)

    def test_a_non_path_exception_with_a_named_handler_does_not_raise(self):
        """The design principle finding 6c does NOT touch: a function
        whose only raise mentions nothing tracked leaves its named except
        handler(s) alone, exactly as before this finding."""
        found = {(r.method, r.template) for r in extract_routes(_module('''
            def do_GET(self):
                path = self.path.split("?", 1)[0]
                if path == "/api/real":
                    try:
                        raise ValueError("unrelated, no path involved")
                    except ValueError as err:
                        return self._send_status(500)
                    return self._send(1)
                return self._send(2)
        '''))}
        self.assertEqual(found, {("GET", "/api/real")})

    def test_an_except_with_no_as_binding_is_unaffected_by_finding_6c(self):
        """``except ValueError:`` (no name bound) has nothing for finding
        6c to flag -- there is no alias a payload could leak through.
        Isolated from finding 6b (which would otherwise ALSO raise on
        this fixture's own `ValueError(path)` argument) via the SAME
        `_with_waivers` technique the dormant-check test above uses, so
        this test proves 6c SPECIFICALLY stays quiet for a no-name
        handler, not merely that the fixture as a whole fails to raise
        for some other reason."""
        src = _module('''
            def do_GET(self):
                path = self.path.split("?", 1)[0]
                if path == "/api/real":
                    try:
                        raise ValueError(path)
                    except ValueError:
                        return self._send_status(404)
                    return self._send(1)
                return self._send(2)
        ''')
        self._with_waivers({
            ("do_GET", "ValueError(path)", "raise", "path == '/api/real'"):
                "test-only: silence finding 6b so this test isolates "
                "finding 6c's own behaviour for a handler with NO name "
                "binding",
        })
        found = {(r.method, r.template) for r in extract_routes(src)}
        self.assertEqual(found, {("GET", "/api/real")})

    def test_the_real_server_extracts_with_no_new_raises(self):
        """The real server.py has no assert on a tracked subject, no raise
        whose argument mentions one, and so no named except handler this
        finding's coarse, function-wide over-approximation needs to flag
        -- must still extract cleanly: 239 routes, the SAME 51 waivers as
        of THIS finding's own landing (see WaiverFingerprintTests' own
        pinned count for the CURRENT total -- later rounds grow it
        further) -- no new waiver needed for finding 6 itself."""
        walker = extract_walker()
        self.assertEqual(len(walker.routes), 241)
        self.assertEqual(walker.unreachable, [])
        self.assertEqual(len(route_extract_module._AUDIT_WAIVERS), 116)


# --------------------------------------------------------------------------- #
# #202 repair round 6, finding 1 (external review, 03:59:55): expression      #
# taint is non-compositional. Four root causes behind five reviewer-supplied  #
# same-source, live-HTTP-before/static-silent-after repros, each independently#
# demonstrated below (transcript in each test's own docstring) before being   #
# shown closed the same way:                                                  #
#                                                                              #
#   (A) ``root_name`` discarded a plain Subscript's own SLICE while           #
#       unwrapping a receiver chain -- fine for ``SOME_DICT[key].attr``       #
#       (the slice does not decide the CHAIN's root name), wrong for          #
#       ``FLAGS[path]`` used directly as (or boolop/not-ed into) the whole    #
#       test, where the slice IS what the test decides on. Round 5, finding   #
#       2a already special-cased this for a Subscript reached as a Call's     #
#       own CALLEE; this generalises that fix to every Subscript this loop    #
#       ever unwraps through, and removes the now-redundant special case.    #
#   (B) ``_propagates_taint`` audited only ``ast.Call`` nodes -- a bare       #
#       ``ast.Subscript`` with no Call anywhere around it (``RESPONSES       #
#       [path]``, ``ERRORS[path]``) was invisible to it, Return/Raise/       #
#       bare-Expr scans included, no matter how directly it carried the      #
#       path.                                                                 #
#   (C) the SAME function's per-node walk ``return False``'d the INSTANT     #
#       the FIRST Call it found turned out unrelated to any tracked name --   #
#       a SHORT-CIRCUIT, not a missing-shape gap: ``candidate = path or       #
#       fallback()`` has an entirely independent tracked SIBLING             #
#       (``path``) right next to the unrelated call, discarded anyway.       #
#   (D) ``_mentions_tracked``/``_tracked_mentions`` only ever matched a bare  #
#       ``ast.Name`` against ``tracked`` -- ``self.path``, an                #
#       ``ast.Attribute``, was invisible to both NO MATTER HOW DEEP it sat    #
#       (``str(self.path).split(...)``), even though ``root_name`` already   #
#       recognised it when it was the bare operand directly.                 #
#                                                                              #
# All four are fixed via a SMALL number of targeted, reviewed changes rather  #
# than a full rewrite of this module's taint model (see route_extract.py's    #
# own docstring on the soundness trade-off this represents) -- a shared       #
# ``_is_self_path`` helper closes (D) everywhere at once; (A) and (B) each    #
# get one new, narrowly-scoped branch; (C) is the removal of one wrong line.  #
# Fixing (D) and (C) together surfaced 23 real new call/subscript sites in    #
# server.py needing their own reviewed waiver, and (independently) proved 3   #
# PRE-EXISTING waivers dormant -- each was needed only because the OLD        #
# bottom-of-function fallback did not honour the opaque-extraction boundary   #
# and so wrongly read a captured group's OWN name as "tracked" (the exact     #
# regression class round 5 finding 2b fought for two OTHER exemptions, not    #
# yet closed at the fallback's own final line) -- see WaiverFingerprintTests' #
# own pinned count and docstring for the full accounting of both.             #
# --------------------------------------------------------------------------- #
class CompositionalTaintTests(unittest.TestCase):
    # -- pre-fix escapes, reproduced via git stash (not re-run here: git
    # stash cannot be invoked from inside a test process) -- the transcript
    # below is what running each of the five fixtures below produced against
    # the code as it stood immediately before this finding's fix (head
    # 15ff6b1618a47135fab5e867bab3e89eaaf25034, round 5's own final head),
    # captured verbatim, real HTTP alongside the static verdict for every
    # one exactly as the reviewer's own report did:
    #
    #   repro1-FLAGS-subscript-if:            NO RAISE. routes = [].  live: 200
    #   repro2-return-send_json-subscript:    NO RAISE. routes = [].  live: 200
    #   repro3-raise-subscript-caught:        NO RAISE. routes = [].  live: 200
    #   repro4-boolop-or-fallback:            NO RAISE. routes = [].  live: 200
    #   repro5-str-self-path-split:           NO RAISE. routes = [].  live: 200
    #
    # each now raises against the FIXED code, asserted below, which a test
    # process can still verify for itself on every run; each live-HTTP probe
    # (still 200 on the SAME unmodified source -- server.py is untouched by
    # this finding) is reproduced directly in this class too, not merely
    # asserted in this comment.

    def _raises(self, body, *substrings):
        with self.assertRaises(ExtractionError) as caught:
            extract_routes(_module(body))
        msg = str(caught.exception)
        for s in substrings:
            self.assertIn(s, msg)

    # -- root cause A: a bare Subscript's slice, discarded while unwrapping ----

    def test_subscript_used_directly_as_the_test_raises(self):
        """Root cause A, the reviewer's own first repro: ``FLAGS[path]``
        used directly as the whole if-test. ``root_name``'s receiver-chain
        unwrap (``node = node.value``) used to discard ``.slice``
        unconditionally -- correct for resolving ``SOME_DICT[key].attr``
        toward its own root name, wrong here, where the slice IS what the
        test decides on."""
        self._raises('''
            def do_GET(self):
                path = self.path
                if FLAGS[path]:
                    return self._send(1)
                return self._send_json({"error": "not_found"}, 404)
        ''', "unrecognised shape", "path")

    def test_subscript_test_escape_answers_over_real_http(self):
        status, text = _real_http_probe_with_globals('''
            def do_GET(self):
                path = self.path
                if FLAGS[path]:
                    return self._send(1)
                return self._send_json({"error": "not_found"}, 404)
        ''', {"FLAGS": {"/api/hidden": True}}, "GET", "/api/hidden")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(text), {"n": 1})

    # -- root cause B: a bare Subscript, no Call anywhere around it ------------

    def test_return_of_bare_subscript_raises(self):
        """Root cause B, the reviewer's own second repro: ``return
        self._send_json(RESPONSES[path], 200)``. ``_send_json`` is a
        reviewed ``_TERMINAL_RESPONSE_SENDERS`` exemption -- correctly, on
        its own terms -- but its own comment's claim ("whatever is nested
        in its arguments is still reached... by this SAME walk") was only
        ever true for a nested CALL; ``RESPONSES[path]`` is a bare
        Subscript, which nothing in the per-node loop inspected at all
        before this finding."""
        self._raises('''
            def do_GET(self):
                path = self.path
                return self._send_json(RESPONSES[path], 200)
        ''', "indexes a container", "RESPONSES[path]")

    def test_return_of_bare_subscript_escape_answers_over_real_http(self):
        status, text = _real_http_probe_with_globals('''
            def do_GET(self):
                path = self.path
                return self._send_json(RESPONSES[path], 200)
        ''', {"RESPONSES": {"/api/hidden": {"n": 1}}}, "GET", "/api/hidden")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(text), {"n": 1})

    def test_raise_of_bare_subscript_raises(self):
        """Root cause B, the reviewer's own third repro: ``raise
        ERRORS[path]``, caught by an enclosing handler -- the SAME bare-
        Subscript gap as the Return case immediately above, reached via
        the Raise scan instead. No name binding on the ``except`` clause
        (unlike round 5 finding 6c's own repro), so finding 6c's
        function-wide except-as-name mechanism is not what would catch
        this -- isolates root cause B specifically."""
        self._raises('''
            def do_GET(self):
                path = self.path
                try:
                    raise ERRORS[path]
                except LookupError:
                    return self._send(1)
                except Exception:
                    return self._send_json({"error": "not_found"}, 404)
        ''', "indexes a container", "ERRORS[path]")

    def test_raise_of_bare_subscript_escape_answers_over_real_http(self):
        status, text = _real_http_probe_with_globals('''
            def do_GET(self):
                path = self.path
                try:
                    raise ERRORS[path]
                except LookupError:
                    return self._send(1)
                except Exception:
                    return self._send_json({"error": "not_found"}, 404)
        ''', {"ERRORS": {"/api/hidden": LookupError("boom")}},
                                                     "GET", "/api/hidden")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(text), {"n": 1})

    # -- root cause C: an unrelated Call short-circuiting the WHOLE walk -------

    def test_tracked_sibling_of_an_unrelated_call_still_raises(self):
        """Root cause C, the reviewer's own fourth repro: ``candidate =
        path or fallback()``. ``fallback()`` mentions nothing tracked, so
        the OLD code's ``return False`` fired the instant it was examined
        -- discarding the WHOLE expression's verdict, including the
        entirely independent, plainly tracked ``path`` sitting right next
        to it in the SAME ``BoolOp``. Not a missing-shape gap: a
        SHORT-CIRCUIT, in the reviewer's own words ("lets any unrelated
        nested Call clear taint for the whole expression")."""
        self._raises('''
            def do_GET(self):
                path = self.path
                candidate = path or fallback()
                if candidate == "/api/hidden":
                    return self._send(1)
                return self._send_json({"error": "not_found"}, 404)
        ''', "unrecognised shape", "candidate")

    def test_boolop_short_circuit_escape_answers_over_real_http(self):
        status, text = _real_http_probe_with_globals('''
            def do_GET(self):
                path = self.path
                candidate = path or fallback()
                if candidate == "/api/hidden":
                    return self._send(1)
                return self._send_json({"error": "not_found"}, 404)
        ''', {"fallback": lambda: "/never"}, "GET", "/api/hidden")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(text), {"n": 1})

    # -- root cause D: self.path unrecognised below the top level --------------

    def test_nested_self_path_inside_an_unlisted_call_raises(self):
        """Root cause D, the reviewer's own fifth repro: ``candidate =
        str(self.path).split("?", 1)[0]``. ``str(...)`` is not a
        recognised ``_PATH_OPS``/``_PATH_METHODS`` manipulation, so its
        one argument -- ``self.path`` -- needed to be SEEN as tracked for
        this to raise as an unlisted call; before this finding, neither
        ``_mentions_tracked`` nor ``_tracked_mentions`` ever matched
        anything but a bare ``ast.Name``, so ``self.path`` (an
        ``ast.Attribute``) was invisible to both, no matter how deep it
        sat."""
        self._raises('''
            def do_GET(self):
                candidate = str(self.path).split("?", 1)[0]
                if candidate == "/api/hidden":
                    return self._send(1)
                return self._send_json({"error": "not_found"}, 404)
        ''', "unlisted call", "str(self.path)")

    def test_nested_self_path_escape_answers_over_real_http(self):
        status, text = _real_http_probe('''
            def do_GET(self):
                candidate = str(self.path).split("?", 1)[0]
                if candidate == "/api/hidden":
                    return self._send(1)
                return self._send_json({"error": "not_found"}, 404)
        ''', "GET", "/api/hidden")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(text), {"n": 1})

    # -- negative controls: ordinary code stays accepted ------------------------

    def test_an_unrelated_call_sibling_of_nothing_tracked_is_unaffected(self):
        """A control for root cause C: when NEITHER side of the BoolOp
        mentions a tracked name, the fixed compositional walk must still
        say so -- the fix is not "assume tracked the moment any Call
        looks unrelated", it is "keep checking every child independently"."""
        found = {(r.method, r.template) for r in extract_routes(_module('''
            def do_GET(self):
                path = self.path.split("?", 1)[0]
                candidate = unrelated_one() or unrelated_two()
                if candidate == "/whatever":
                    return self._send(1)
                if path == "/api/real":
                    return self._send(2)
        '''))}
        self.assertEqual(found, {("GET", "/api/real")})

    def test_an_unrelated_subscript_is_unaffected(self):
        """A control for root cause B: a Subscript keyed on something NOT
        tracked must not raise -- the new check fires on the SLICE
        mentioning a tracked name, not on every Subscript."""
        found = {(r.method, r.template) for r in extract_routes(_module('''
            def do_GET(self):
                path = self.path.split("?", 1)[0]
                x = CONSTANTS["some_fixed_key"]
                if path == "/api/real":
                    return self._send_json({"x": x}, 200)
        '''))}
        self.assertEqual(found, {("GET", "/api/real")})

    def test_a_subscript_keyed_on_an_already_captured_id_does_not_raise(self):
        """A control for compositional taint tracking, not for the
        captured-arg exemption itself (#202 repair round 13, finding 1
        retired that mechanism entirely -- see route_extract.py's
        ``_TRUSTED_BINDING_SOURCES``, own module comment): a dict/sequence
        lookup keyed on an already-CAPTURED id (``api.CACHE[gid]``) needs
        its OWN individually reviewed waiver now, the same as any other
        unmodelled call/subscript in a synthetic fixture -- installed here
        (rather than relying on any structural rule) so this test keeps
        isolating what it actually checks: that a captured-only Subscript,
        once accepted, does not ALSO get compositionally mis-flagged
        elsewhere in the SAME expression."""
        src = _waive_matching_node(self.addCleanup, '''
            def do_GET(self):
                path = self.path.split("?", 1)[0]
                api = STATE.api
                m = re.match(r"^/api/items/([^/]+)$", path)
                if m:
                    gid = m.group(1)
                    return self._send_json(api.CACHE[gid], 200)
        ''', "api.CACHE[gid]")
        found = {(r.method, r.template) for r in extract_routes(src)}
        self.assertEqual(found, {("GET", "/api/items/{}")})

    def test_an_opaque_capture_still_does_not_leak_through_the_fallback(self):
        """A control for the bottom-of-function fallback's own fix (now
        ``_mentions_tracked``, not a blind ``ast.walk`` Name-scan): the
        module's own canonical "does NOT carry taint" example --
        ``api.calendar_feed_ics(cal.group(1))`` -- must stay clean even
        though it CONTAINS a tracked match-object name (``cal``) two
        levels down, behind the opaque-extraction boundary. A blind scan
        would wrongly find `cal` here; this is the regression the
        `_mentions_tracked`-based fallback exists to prevent."""
        found = {(r.method, r.template) for r in extract_routes(_module('''
            def do_GET(self):
                path = self.path.split("?", 1)[0]
                cal = re.match(r"^/officials/([^/]+)/calendar$", path)
                if cal:
                    ics = api.calendar_feed_ics(cal.group(1))
                    return self._send_ics(ics)
        '''))}
        self.assertEqual(found, {("GET", "/officials/{}/calendar")})

    def test_the_real_server_extracts_with_no_new_raises(self):
        """The real server.py -- including the GET query-string filter/
        scope idiom (`parse_qs(urlparse(self.path).query)`) this finding's
        self.path fix newly reaches throughout `_dispatch_get`, and the
        two `SCHEMA[combo]` keyword-unpack Subscripts this finding's own
        new Subscript audit newly reaches -- must still extract cleanly:
        239 routes, 71 waivers as of THIS finding's own landing (23 new
        minus 3 proven dormant -- see WaiverFingerprintTests' own pinned
        count for the CURRENT total, which round 6 finding 2 grows
        further)."""
        walker = extract_walker()
        self.assertEqual(len(walker.routes), 241)
        self.assertEqual(walker.unreachable, [])
        self.assertEqual(len(route_extract_module._AUDIT_WAIVERS), 116)


# --------------------------------------------------------------------------- #
# #202 repair round 6, finding 2 (external review, 03:59:55): the audited     #
# execution-control inventory remains fail-open. Five reviewer-supplied       #
# same-source repros (four live-HTTP-before/static-silent-after, one a        #
# static-wildcard/live-behaviour-split), each independently demonstrated      #
# below before being shown closed:                                            #
#                                                                              #
#   * a LOCAL closure reading a tracked name as a free variable rather than   #
#     a syntactic argument -- invisible to every call-auditing mechanism,     #
#     which only ever looks at a call's own visible receiver/arguments;       #
#   * a ``with EXPR:`` (no ``as``) context expression -- never inspected by   #
#     anything, the binding model only ever looked at a WITH-item's context   #
#     expression to decide whether ITS OWN bound name should join tracked;    #
#   * a ``for``/``async for`` iterable keyed on a tracked name -- ALREADY     #
#     closed as a side effect of round 6 finding 1's own Subscript audit      #
#     (the fixed-point loop already runs every iterable through               #
#     ``_propagates_taint`` to decide the loop TARGET's own trackedness, so   #
#     the new Subscript branch reaches it for free) -- verified, not just     #
#     assumed, below;                                                         #
#   * an IMPLICIT exception (a Subscript/operation that fails naturally,      #
#     never an explicit ``raise``) inspected through its own handler's        #
#     payload -- round 5 finding 6c's coarse over-approximation only ever     #
#     looks for an explicit ``ast.Raise`` node, function-wide;                #
#   * the round 5 finding 2b ``captured`` exemption applied to a captured id  #
#     used to SELECT a callable that is then immediately invoked             #
#     (``handlers.get(action, default)()``) -- the exemption's own "only a   #
#     captured id" test cannot tell dispatch SELECTION apart from inert      #
#     DATA handed to a fixed, known service.                                  #
#                                                                              #
# Closed via: a closure's own name treated as an IMPLICIT binding (joins     #
# ``tracked`` when its body mentions anything tracked, through the SAME      #
# fixed-point loop); a ``with``/``async with`` context-expression scan       #
# (reusing ``_propagates_taint`` exactly as the Return/Raise/bare-Expr       #
# scans already do); a new, narrowly-SCOPED (the handler's own enclosing     #
# try body, not function-wide) implicit-exception check alongside finding    #
# 6c's own explicit one; and a new ``_is_callee`` guard narrowing the        #
# ``captured`` exemption so it can never fire for a Call/Subscript that is   #
# itself about to be invoked. Two real new sites in server.py needed their   #
# own reviewed waiver (see WaiverFingerprintTests' own pinned count).        #
# --------------------------------------------------------------------------- #
class ExecutionControlAndDataFlowTests(unittest.TestCase):
    # -- pre-fix escapes, reproduced via git stash (not re-run here: git
    # stash cannot be invoked from inside a test process) -- the transcript
    # below is what running each of the five fixtures below produced against
    # the code as it stood immediately before this finding's fix (head
    # 15ff6b1618a47135fab5e867bab3e89eaaf25034, round 5's own final head, via
    # an isolated copy of route_extract.py at that exact commit -- round 6
    # finding 1's OWN fix landed first, on top of the same base, as an
    # independent commit; the four repros below were separately re-verified
    # against that finding-1-fixed head too, to isolate exactly what finding
    # 2 itself closes), captured verbatim:
    #
    #   repro1-closure-captures-path:            NO RAISE both heads. live: 200
    #   repro2-with-contexts-subscript:           NO RAISE both heads. live: 200
    #   repro3-for-routes-subscript:               NO RAISE pre-fix-1; RAISES
    #                                               once finding 1 lands (see
    #                                               its own Subscript audit)
    #   repro4-implicit-exception-handler-inspect: NO RAISE both heads. live: 200
    #   repro5-captured-dispatch-selection:  wildcard both heads. live: 200/404
    #
    # each now raises (or, for repro3, already raised via finding 1) against
    # the FIXED code, asserted below, which a test process can still verify
    # for itself on every run.

    def _raises(self, body, *substrings):
        with self.assertRaises(ExtractionError) as caught:
            extract_routes(_module(body))
        msg = str(caught.exception)
        for s in substrings:
            self.assertIn(s, msg)

    # -- closures ----------------------------------------------------------

    def test_closure_reading_path_as_a_free_variable_raises(self):
        """The reviewer's own first repro: a LOCAL closure whose body reads
        ``path`` through Python's own closure mechanism (a free variable,
        never a syntactic argument), called with `compute()` -- a call
        site that mentions NOTHING tracked in its own visible syntax, so
        no existing call-auditing check ever looked twice at it."""
        self._raises('''
            def do_GET(self):
                path = self.path
                def compute():
                    return path
                candidate = compute()
                if candidate == "/api/hidden":
                    return self._send(1)
                return self._send_json({"error": "not_found"}, 404)
        ''', "unlisted call", "compute()")

    def test_closure_escape_answers_over_real_http(self):
        status, text = _real_http_probe('''
            def do_GET(self):
                path = self.path
                def compute():
                    return path
                candidate = compute()
                if candidate == "/api/hidden":
                    return self._send(1)
                return self._send_json({"error": "not_found"}, 404)
        ''', "GET", "/api/hidden")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(text), {"n": 1})

    def test_a_closure_touching_nothing_tracked_is_unaffected(self):
        """A control: a closure that reads no tracked name at all -- only
        a constant -- must not raise merely for existing, and its call
        site must not be treated as tracked either."""
        found = {(r.method, r.template) for r in extract_routes(_module('''
            def do_GET(self):
                path = self.path.split("?", 1)[0]
                def compute():
                    return "fixed-value"
                candidate = compute()
                if candidate == "whatever":
                    return self._send(1)
                if path == "/api/real":
                    return self._send(2)
        '''))}
        self.assertEqual(found, {("GET", "/api/real")})

    def test_a_closure_touching_only_a_captured_id_still_raises_deliberately(self):
        """A DELIBERATE choice, stated plainly: a closure whose only
        tracked mention is an already-CAPTURED name (``gid``, not the
        primary ``path``) still needs review, unlike an INLINE capture
        used the SAME way (``f"item {gid}"`` written directly, with no
        closure, is already exempt via ``_TERMINAL_RESPONSE_SENDERS``
        here). The closure fix intentionally does not extend the
        ``captured``-only exemption through a closure call -- the call
        site's own tracked mention is the CLOSURE'S NAME, never ``gid``
        itself, and ``ctx.captured`` is deliberately never grown by this
        fix (see its own comment) -- so this fails CLOSED rather than
        silently assuming every closure that merely touches captured data
        is safe. Reviewable via the SAME waiver escape hatch as
        everything else, demonstrated below."""
        src = _module('''
            def do_GET(self):
                path = self.path.split("?", 1)[0]
                m = re.match(r"^/api/items/([^/]+)$", path)
                if m:
                    gid = m.group(1)
                    def describe():
                        return f"item {gid}"
                    return self._send_json({"label": describe()}, 200)
        ''')
        with self.assertRaises(ExtractionError):
            extract_routes(src)
        self._with_waivers({
            ("do_GET", "describe()", "dict_value", "m"):
                "test-only: describe() only formats an already-captured "
                "id into a label string, never used for routing",
        })
        found = {(r.method, r.template) for r in extract_routes(src)}
        self.assertEqual(found, {("GET", "/api/items/{}")})

    # -- with context expressions -------------------------------------------

    def test_with_context_expression_touching_path_raises(self):
        """The reviewer's own second repro: ``with CONTEXTS[path]:`` --
        the context expression of a bare ``with`` (no ``as`` clause) was
        never inspected by anything: the binding model only ever looks at
        a with-item's context expression to decide whether ITS OWN bound
        name should join ``tracked``, which does not apply here at all."""
        self._raises('''
            def do_GET(self):
                path = self.path
                with CONTEXTS[path]:
                    return self._send(1)
        ''', "indexes a container", "CONTEXTS[path]")

    def test_with_context_expression_escape_answers_over_real_http(self):
        status, text = _real_http_probe_with_globals('''
            def do_GET(self):
                path = self.path
                with CONTEXTS[path]:
                    return self._send(1)
        ''', {"CONTEXTS": {"/api/hidden": __import__("contextlib").nullcontext()}},
                                                     "GET", "/api/hidden")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(text), {"n": 1})

    def test_with_no_as_context_expression_touching_only_captured_data_is_unaffected(self):
        """A control for the WITH-context-expression audit position, not
        for the captured-arg exemption itself (#202 repair round 13,
        finding 1 retired that mechanism -- see route_extract.py's
        ``_TRUSTED_BINDING_SOURCES``, own module comment): a context
        manager selected by an already-captured id (``api.LOCKS[gid]``)
        needs its own individually reviewed waiver now, installed here so
        this test keeps isolating what it actually checks -- that the
        with-audit examines the context expression at all, not whether a
        captured-only subscript is structurally trusted."""
        src = _waive_matching_node(self.addCleanup, '''
            def do_GET(self):
                path = self.path.split("?", 1)[0]
                api = STATE.api
                m = re.match(r"^/api/items/([^/]+)$", path)
                if m:
                    gid = m.group(1)
                    with api.LOCKS[gid]:
                        return self._send(1)
        ''', "api.LOCKS[gid]")
        found = {(r.method, r.template) for r in extract_routes(src)}
        self.assertEqual(found, {("GET", "/api/items/{}")})

    def test_async_with_context_expression_touching_path_raises(self):
        """``async with`` gets the SAME audit as ``with`` (see the
        ``isinstance(node, (ast.With, ast.AsyncWith))`` branch in
        ``_audit_function`` -- one check, both node types) -- exercised
        via a nested ``async def`` (the only syntactically valid place one
        can appear at all: ``do_GET`` itself must stay a plain,
        synchronous ``BaseHTTPRequestHandler`` method, per ENTRY_POINTS'
        own model, so this is necessarily static-only -- there is no
        meaningful way to drive an ``async with`` over a synchronous real
        HTTP probe here). In THIS particular arrangement the closure fix
        (this same finding's own OTHER new mechanism, several tests above)
        actually catches it FIRST -- ``helper``'s body mentions `path`
        (through the async-with's own context expression, however it is
        nested), so ``helper`` itself joins ``tracked`` during the SAME
        fixed-point pass, before the dedicated with/async-with scan even
        runs -- which is a fine outcome (the escape closes either way) but
        means the message below names ``helper()``, not ``CONTEXTS
        [path]`` directly; the with-audit's OWN message is independently
        proven, in isolation from any closure, by the plain (synchronous)
        ``test_with_context_expression_touching_path_raises`` above."""
        self._raises('''
            def do_GET(self):
                path = self.path
                async def helper():
                    async with CONTEXTS[path]:
                        return True
                if helper():
                    return self._send(1)
        ''', "unrecognised shape", "helper()")

    # -- for/async for iterables (verifying finding 1's own coverage) ------

    def test_for_loop_over_a_tracked_subscript_raises(self):
        """The reviewer's own third repro: ``for _ in ROUTES[path]: return
        ...`` -- CLOSED as a side effect of round 6 finding 1's own
        Subscript audit (the fixed-point loop already runs a For's own
        ``.iter`` through ``_propagates_taint`` to decide whether the loop
        TARGET should join ``tracked``, so the new Subscript branch
        reaches ``ROUTES[path]`` for free) -- verified here directly
        rather than merely assumed, since finding 2's own required
        correction names ``For``/``AsyncFor`` iterables explicitly."""
        self._raises('''
            def do_GET(self):
                path = self.path
                for _ in ROUTES[path]:
                    return self._send(1)
                return self._send_json({"error": "not_found"}, 404)
        ''', "indexes a container", "ROUTES[path]")

    def test_for_loop_escape_answers_over_real_http(self):
        status, text = _real_http_probe_with_globals('''
            def do_GET(self):
                path = self.path
                for _ in ROUTES[path]:
                    return self._send(1)
                return self._send_json({"error": "not_found"}, 404)
        ''', {"ROUTES": {"/api/hidden": [1]}}, "GET", "/api/hidden")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(text), {"n": 1})

    def test_for_loop_tuple_target_over_a_tracked_subscript_raises(self):
        """The tuple-target variant of the same shape (``for a, b in
        ROUTES[path]:``) -- the reviewer's own required "tuple ...
        variants where applicable"."""
        self._raises('''
            def do_GET(self):
                path = self.path
                for a, b in ROUTES[path]:
                    return self._send(1)
                return self._send_json({"error": "not_found"}, 404)
        ''', "indexes a container", "ROUTES[path]")

    def test_async_for_over_a_tracked_subscript_raises(self):
        """``async for`` gets the SAME coverage as ``for`` (both are
        already routed through the SAME ``_binding_value_and_targets``
        extraction, per ``BINDING_NODE_TYPES``) -- exercised via a nested
        ``async def``, static-only, for the same reason
        ``test_async_with_context_expression_touching_path_raises``
        above is."""
        self._raises('''
            def do_GET(self):
                path = self.path
                async def helper():
                    async for _ in ROUTES[path]:
                        return True
                    return False
                if helper():
                    return self._send(1)
        ''', "indexes a container", "ROUTES[path]")

    def test_a_for_loop_over_something_unrelated_is_unaffected(self):
        """A control: a for-loop iterable that mentions nothing tracked
        must not raise, and its target must not become tracked either."""
        found = {(r.method, r.template) for r in extract_routes(_module('''
            def do_GET(self):
                path = self.path.split("?", 1)[0]
                for item in FIXED_LIST:
                    if item == "whatever":
                        return self._send(1)
                if path == "/api/real":
                    return self._send(2)
        '''))}
        self.assertEqual(found, {("GET", "/api/real")})

    # -- implicit exceptions inspected through a handler's own payload -----

    def test_implicit_exception_inspected_via_handler_value_raises(self):
        """The reviewer's own fourth repro: ``{}[path]`` raises `KeyError`
        IMPLICITLY (no explicit ``raise`` anywhere), and the handler
        inspects the caught exception's own payload
        (``e.args[0]``) to make the actual routing decision. The bare
        ``{}[path]`` access is independently caught by finding 1's own
        Subscript audit (see the message asserted below) -- the DEEPER,
        finding-2-specific claim (a WAIVED implicit-exception-producing
        expression still leaves its handler examined) is proven in
        isolation by
        ``test_implicit_exception_behind_a_waived_call_still_flags_its_handler``
        below, which cannot be masked by finding 1's own Subscript catch."""
        self._raises('''
            def do_GET(self):
                path = self.path
                try:
                    {}[path]
                except KeyError as e:
                    candidate = str(e.args[0])
                    if candidate == "/api/hidden":
                        return self._send(1)
                return self._send_json({"error": "not_found"}, 404)
        ''', "indexes a container", "{}[path]")

    def test_implicit_exception_escape_answers_over_real_http(self):
        status, text = _real_http_probe('''
            def do_GET(self):
                path = self.path
                try:
                    {}[path]
                except KeyError as e:
                    candidate = str(e.args[0])
                    if candidate == "/api/hidden":
                        return self._send(1)
                return self._send_json({"error": "not_found"}, 404)
        ''', "GET", "/api/hidden")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(text), {"n": 1})

    def test_implicit_exception_behind_a_waived_call_still_flags_its_handler(self):
        """Isolates finding 2's own new mechanism
        (:meth:`_DispatchWalker._try_body_has_tracked_operation`) from
        finding 1's Subscript audit: the try body's own tracked operation
        here is a CALL that is explicitly WAIVED (a stand-in for a real,
        reviewed call that may raise internally on bad input) -- so
        finding 1's machinery raises nothing for it, and finding 6c's own
        EXPLICIT-raise check (round 5) also sees nothing (there is no
        ``ast.Raise`` anywhere in this function). Without this finding's
        own try-body-scoped check, `except LookupError as e: candidate =
        str(e)` would go completely unexamined."""
        src = _module('''
            def do_GET(self):
                path = self.path
                try:
                    api.some_reviewed_lookup(path)
                except LookupError as e:
                    candidate = str(e)
                    if candidate == "/api/hidden":
                        return self._send(1)
                return self._send_json({"error": "not_found"}, 404)
        ''')
        self._with_waivers({
            ("do_GET", "api.some_reviewed_lookup(path)", "bare_stmt", ""):
                "test-only: a reviewed, waived call that may raise "
                "internally, isolating finding 2's try-body-scoped "
                "implicit-exception check from finding 1's own Subscript "
                "audit",
        })
        with self.assertRaises(ExtractionError) as caught:
            extract_routes(src)
        self.assertIn("except ... as", str(caught.exception))

    def _with_waivers(self, waivers: dict):
        """As ``WaiverFingerprintTests._with_waivers``: temporarily ADDS
        `waivers` on top of the real ``_AUDIT_WAIVERS`` (not a full
        replacement -- this class's other tests still need the real
        server.py's own waivers to stay absent so THEIR fixtures raise
        correctly), restored even if the test body raises."""
        saved = dict(route_extract_module._AUDIT_WAIVERS)
        route_extract_module._AUDIT_WAIVERS.update(waivers)
        self.addCleanup(lambda: (
            route_extract_module._AUDIT_WAIVERS.clear(),
            route_extract_module._AUDIT_WAIVERS.update(saved)))

    def test_an_except_handler_whose_try_body_touches_nothing_tracked_is_unaffected(self):
        """A control: a named except handler in a function that has NO
        explicit tracked raise AND whose own enclosing try body mentions
        nothing tracked must not be flagged."""
        found = {(r.method, r.template) for r in extract_routes(_module('''
            def do_GET(self):
                path = self.path.split("?", 1)[0]
                if path == "/api/real":
                    try:
                        unrelated_call()
                    except LookupError as e:
                        return self._send_status(500)
                    return self._send(1)
                return self._send(2)
        '''))}
        self.assertEqual(found, {("GET", "/api/real")})

    def test_a_named_handler_in_an_unrelated_sibling_try_is_unaffected(self):
        """A control: this check is scoped to the handler's OWN enclosing
        try body, not function-wide -- a named handler on a DIFFERENT,
        unrelated try statement in the SAME function must stay clean even
        though the function has SOME OTHER try body that touches `path`
        (a reviewed, WAIVED call here -- isolating the scoping question
        from whether that OTHER try body's own tracked call needed a
        waiver in the first place, already covered by
        ``test_implicit_exception_behind_a_waived_call_still_flags_its_handler``
        above)."""
        src = _module('''
            def do_GET(self):
                path = self.path.split("?", 1)[0]
                try:
                    logger.info(path)
                except LookupError:
                    pass
                if path == "/api/real":
                    try:
                        unrelated_call()
                    except LookupError as e:
                        return self._send_status(500)
                    return self._send(1)
                return self._send(2)
        ''')
        self._with_waivers({
            ("do_GET", "logger.info(path)", "bare_stmt", ""):
                "test-only: an unrelated, reviewed logging call in a "
                "SIBLING try body, isolating the try-body-scoping "
                "question this test is actually about",
        })
        found = {(r.method, r.template) for r in extract_routes(src)}
        self.assertEqual(found, {("GET", "/api/real")})

    # -- captured-exemption narrowing for dispatch selection ---------------

    def test_captured_id_selecting_a_directly_invoked_callable_raises(self):
        """The reviewer's own fifth repro: a regex-captured ``action``
        selects ``handlers.get(action, default_handler)()`` -- the
        ``captured`` exemption (round 5, finding 2b) accepted this
        because its ONLY tracked mention (``action``) is captured, never
        asking whether the call's own RESULT is about to be invoked."""
        self._raises('''
            def do_GET(self):
                path = self.path
                m = re.match(r"^/api/([^/]+)$", path)
                if m:
                    action = m.group(1)
                    return handlers.get(action, default_handler)(self)
        ''', "unlisted call", "handlers.get(action, default_handler)")

    def test_captured_dispatch_selection_escape_answers_over_real_http(self):
        status, text = _real_http_probe_with_globals('''
            def do_GET(self):
                path = self.path
                m = re.match(r"^/api/([^/]+)$", path)
                if m:
                    action = m.group(1)
                    return handlers.get(action, default_handler)(self)
        ''', {"re": re, "handlers": {"hidden": lambda h: h._send(1)},
              "default_handler": lambda h: h._send_json(
                  {"error": "not_found"}, 404)},
                                                     "GET", "/api/hidden")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(text), {"n": 1})
        status2, text2 = _real_http_probe_with_globals('''
            def do_GET(self):
                path = self.path
                m = re.match(r"^/api/([^/]+)$", path)
                if m:
                    action = m.group(1)
                    return handlers.get(action, default_handler)(self)
        ''', {"re": re, "handlers": {"hidden": lambda h: h._send(1)},
              "default_handler": lambda h: h._send_json(
                  {"error": "not_found"}, 404)},
                                                     "GET", "/api/other")
        self.assertEqual(status2, 404)
        self.assertEqual(json.loads(text2), {"error": "not_found"})

    def test_captured_id_selecting_a_subscript_callee_also_raises(self):
        """The Subscript-callee sibling of the same shape --
        ``HANDLERS[action]()`` -- narrowed via the identical
        ``_is_callee`` guard on finding 1's own Subscript-branch
        exemption, not only the Call-branch one.

        #202 repair round 9 (external review): the message this now
        raises CHANGED -- ``HANDLERS[action]()`` (the outer Call) rather
        than ``indexes a container``/``HANDLERS[action]`` (the inner
        Subscript) -- without weakening anything: round 9's new
        ``_captured_arg_safe_callee`` gate (see its own docstring) denies
        the OUTER call's own exemption ALREADY, because a bare Subscript
        is never a recognised safe callee shape regardless of what it
        contains, so the walk (BFS, outer node first) now raises there
        before ever reaching the inner Subscript's own branch -- this
        round's gate happens to subsume round 6 finding 2's own
        Subscript-``_is_callee`` (removed round 13; see
        route_extract.py's ``_TRUSTED_BINDING_SOURCES``, own module
        comment) guard for this EXACT "used directly as callee" shape,
        though not for others (see ``CapturedArgumentTransferTests`` for
        round 9's own coverage, which uses a fresh set of repros rather
        than re-purposing this one)."""
        self._raises('''
            def do_GET(self):
                path = self.path
                m = re.match(r"^/api/([^/]+)$", path)
                if m:
                    action = m.group(1)
                    return HANDLERS[action]()
        ''', "unlisted call", "HANDLERS[action]()")

    def test_captured_id_as_a_plain_call_argument_is_still_unaffected(self):
        """A control proving the ``_is_callee`` narrowing is precise: a
        captured id handed to a FIXED, KNOWN service as a plain ARGUMENT
        (never a callee) stays exempt -- not because of any STRUCTURAL
        captured-arg rule (#202 repair round 13, finding 1 retired that
        entirely; see route_extract.py's ``_TRUSTED_BINDING_SOURCES``, own
        module comment), but because THIS specific call carries its own
        individually reviewed waiver, installed here the same way any
        other unmodelled call in a synthetic fixture needs one -- the
        real server.py shape (`deleter`/`mapper`/`fn`/`coach`, bound from
        a table lookup FIRST via a separate assignment, then invoked
        through a bare Name that is never itself a Call/Subscript node
        this class's own audit even reaches) is the SAME thing, verified
        at scale by this class's own real-server test below."""
        src = _waive_matching_node(self.addCleanup, '''
            def do_GET(self):
                path = self.path.split("?", 1)[0]
                api = STATE.api
                m = re.match(r"^/api/items/([^/]+)$", path)
                if m:
                    gid = m.group(1)
                    return self._send_api(api.get_item(gid))
        ''', "api.get_item(gid)")
        found = {(r.method, r.template) for r in extract_routes(src)}
        self.assertEqual(found, {("GET", "/api/items/{}")})

    def test_captured_id_bound_first_then_invoked_via_a_bare_name_still_needs_its_own_review(self):
        """Precisely characterises the EXACT real server.py idiom
        (`deleter`/`mapper`/`fn`/`coach`, each already carrying its own
        round-5 finding 2b waiver): a captured id selects a CALLABLE via a
        table lookup bound to a local FIRST -- THIS assignment's own call
        is `_is_callee`-exempt (its parent is the Assign, not an enclosing
        Call, so the narrowing this finding adds does not touch it at
        all) -- but the round-5 "waiver/exemption silences the call, not
        the RESULT" rule still makes `fn` itself join `tracked`, so the
        LATER `fn(self)` -- a bare Name callee, never a Call/Subscript
        node `_is_callee` would even be consulted for -- still needs its
        OWN review, UNCHANGED by this finding either way: exactly the
        pre-existing behaviour the real server.py's own four waived call
        sites already rely on, not a new requirement finding 2 adds.

        #202 repair round 13, finding 1 (external review): the table
        lookup itself -- `{"hidden": api.get_item, "other": api.get_game}
        [action]`, an inline dict LITERAL of `api.X` values keyed by the
        captured id -- ALSO now needs its own explicit review: the
        structural captured-arg exemption that used to cover it
        (rounds 9-11) is retired entirely (see route_extract.py's
        `_TRUSTED_BINDING_SOURCES`, own module comment), so this fixture
        now demonstrates TWO separate, individually reviewed waivers, not
        one -- proving each is INDEPENDENTLY required: with NEITHER
        waived, the ASSIGNMENT raises first; with ONLY the assignment
        waived, `fn(self)` raises next, exactly isolating this test's own
        point (that binding a captured selector to a local first never
        exempts a LATER bare-name invocation of it, regardless of
        whatever separately exempts the assignment); with BOTH waived,
        extraction succeeds."""
        src = _module('''
            def do_GET(self):
                path = self.path
                api = STATE.api
                m = re.match(r"^/api/([^/]+)$", path)
                if m:
                    action = m.group(1)
                    fn = {"hidden": api.get_item, "other": api.get_game}[action]
                    return fn(self)
        ''')
        with self.assertRaises(ExtractionError) as caught:
            extract_routes(src)
        self.assertIn(
            "{'hidden': api.get_item, 'other': api.get_game}[action]",
            str(caught.exception))

        self._with_waivers({
            ("do_GET", "{'hidden': api.get_item, 'other': api.get_game}"
             "[action]", "assign_rhs", "m"):
                "test-only: the SAME dict-literal-of-api-values table "
                "lookup the real server.py's own delete-dispatch tables "
                "use (see _handle_setup's own waivers), keyed on the "
                "captured action",
        })
        with self.assertRaises(ExtractionError) as caught2:
            extract_routes(src)
        self.assertIn("fn(self)", str(caught2.exception))

        self._with_waivers({
            ("do_GET", "fn(self)", "return_value", "m"):
                "test-only: fn is {\"hidden\": api.get_item, \"other\": "
                "api.get_game}[action], the SAME table-lookup-bound-first "
                "shape as the real server.py's own deleter/mapper/fn/coach",
        })
        found = {(r.method, r.template) for r in extract_routes(src)}
        self.assertEqual(found, {("GET", "/api/{}")})

    def test_the_real_server_extracts_with_no_new_raises(self):
        """The real server.py has no local closure that reads a tracked
        free variable, no ``with``/``async with`` statement at all, and
        no captured id used to select-then-immediately-invoke a callable
        -- the ONLY real new sites round 6 finding 2 reaches are the two
        `_handle_reassign`/`_handle_reassign_v2` `except BodyError as
        exc:` handlers (their own enclosing try body's `check_body(b,
        **_V{1,2}_REASSIGN_SCHEMA[combo])` is a tracked operation, round 3
        finding E's own pre-existing waiver); round 7 finding 1's own new
        For/AsyncFor execution-control audit (see
        ``LoopIterableAndReceiverChainDispatchTests`` below for that
        mechanism's own isolated proof) reaches exactly the SAME two
        functions' `for target in targets:` loop, both already reviewed
        (this dict's own round-7 finding 1 waiver group) -- must still
        extract cleanly: 239 routes, 117 waivers (see
        WaiverFingerprintTests' own pinned count and docstring for the
        exact accounting)."""
        walker = extract_walker()
        self.assertEqual(len(walker.routes), 241)
        self.assertEqual(walker.unreachable, [])
        self.assertEqual(len(route_extract_module._AUDIT_WAIVERS), 116)


# --------------------------------------------------------------------------- #
# #202 repair round 7, finding 1 (external review): loop execution control    #
# and captured dispatch selection both remained fail-open, TWO independent    #
# gaps closed together since the review reported them together:               #
#                                                                              #
# RULE 1 -- a For/AsyncFor's OWN ``.iter`` was consulted ONLY to decide        #
# whether the loop's TARGET becomes tainted (round 4 finding 2's own fixed-    #
# point mechanism); nothing consumed a true taint result there as a           #
# path-dependent condition on whether the BODY executes at all. Closed by a   #
# new, independent ``ast.For``/``ast.AsyncFor`` branch in                     #
# ``_DispatchWalker._audit_function`` (route_extract.py) that runs ``.iter``  #
# through ``_direct_operand_names`` -- the SAME name-resolving walk           #
# If/While/Assert/match-case already use, INCLUDING its own default-deny      #
# fallback (round 5 finding 5) for a shape (here, ``ast.BinOp``) none of its   #
# explicit branches special-case -- exactly the way ``ast.While``'s own       #
# ``.test`` is already audited, just for a different statement type.          #
#                                                                              #
# RULE 2 -- ``_is_callee`` (route_extract.py) checked only a node's IMMEDIATE  #
# parent, so a dispatch-selecting Call reached through a RECEIVER CHAIN       #
# (``handlers.get(action, default_handler).serve(self)`` -- the inner Call's  #
# own parent is the ``.serve`` Attribute, never a Call directly) answered     #
# False, letting the round 5 finding 2b ``captured``-only exemption accept it #
# as harmless. Closed by walking UP through every enclosing                   #
# Attribute/Subscript layer where the node reached so far is that layer's     #
# OWN receiver (``.value``), before asking whether the result is a Call's     #
# callee -- mirrors the SAME receiver-chain unwrapping ``_is_self_call``/     #
# ``_direct_operand_names``'s own ``root_name`` already do, just walking the  #
# opposite direction (up toward an eventual callee, not down toward a root).  #
#                                                                              #
# Reproduced via git stash (not re-run here: git stash cannot be invoked      #
# from inside a test process; ``git stash push -- ...route_extract.py`` then  #
# ``git stash pop``, isolating JUST the production fix from this file's own   #
# new tests) against the code as it stood immediately before this finding's   #
# fix, captured verbatim:                                                     #
#                                                                              #
#   repro1-tuple-repeat-count-loop-gate: NO RAISE. routes = []. live: 200/404 #
#   repro2-attribute-wrapped-selector:   NO RAISE. routes = [('GET',          #
#     '/api/{}')] (a single wildcard, never flagged). live: 200/404           #
#   repro2b-subscript-hop-variant:       NO RAISE. routes = [('GET',          #
#     '/api/{}')]. live: 200/404                                              #
#                                                                              #
# every LoopIterableAndReceiverChainDispatchTests test asserting a raise      #
# FAILED against that pre-fix code (``test_tuple_repeat_count_loop_gate_      #
# raises``, ``test_async_variant_of_the_loop_gate_raises_on_its_own_once_     #
# the_closure_escape_is_waived``, ``test_attribute_wrapped_selector_          #
# raises``, ``test_a_deeper_subscript_mediated_receiver_chain_also_           #
# raises`` -- each a genuine "ExtractionError not raised" AssertionError, not #
# a vacuous pass) -- the live-HTTP and negative-control tests in this same    #
# class do NOT (by design: the live tests assert nothing about               #
# ``extract_routes`` at all, and the negative controls must pass either way,  #
# proving THEMSELVES only, not this finding's fix) -- each now raises (or,    #
# for the negative controls, still correctly does not) against the FIXED     #
# code, asserted below, which a test process can still verify for itself on   #
# every run.                                                                  #
# --------------------------------------------------------------------------- #
class _ServeHandler:
    """A tiny stand-in dispatch target for the receiver-chain tests below --
    exposes exactly the ``.serve(request_handler)`` shape the reviewer's own
    repro invokes through an Attribute hop, and (``__call__``) the bare
    ``(request_handler)`` shape a Subscript-mediated variant invokes
    directly, so ONE fixture class serves both."""

    def __init__(self, action):
        self._action = action

    def serve(self, request_handler):
        self._action(request_handler)

    __call__ = serve


class LoopIterableAndReceiverChainDispatchTests(unittest.TestCase):
    def _raises(self, body, *substrings):
        with self.assertRaises(ExtractionError) as caught:
            extract_routes(_module(body))
        msg = str(caught.exception)
        for s in substrings:
            self.assertIn(s, msg)

    def _with_waivers(self, waivers: dict):
        """As ``ExecutionControlAndDataFlowTests._with_waivers``: temporarily
        ADDS `waivers` on top of the real ``_AUDIT_WAIVERS``, restored even
        if the test body raises."""
        saved = dict(route_extract_module._AUDIT_WAIVERS)
        route_extract_module._AUDIT_WAIVERS.update(waivers)
        self.addCleanup(lambda: (
            route_extract_module._AUDIT_WAIVERS.clear(),
            route_extract_module._AUDIT_WAIVERS.update(saved)))

    # -- rule 1: a For/AsyncFor's OWN .iter as an execution-control sink ----

    def test_tuple_repeat_count_loop_gate_raises(self):
        """The reviewer's own first repro, verbatim: a path-derived boolean
        used as a tuple REPEAT COUNT -- ``(1,) * (path == "/api/hidden")``
        -- so the loop body runs zero or one times depending on ``path``,
        with NO Subscript anywhere (unlike round 6's own
        ``ROUTES[path]``-shaped coverage, see
        ``ExecutionControlAndDataFlowTests``' own "for/async for iterables"
        section above) to incidentally catch it. ``_direct_operand_names``
        has no explicit branch for ``ast.BinOp``, so this reaches its own
        default-deny fallback (round 5 finding 5) -- the SAME "an
        unrecognised shape mentioning a tracked name is reviewed, never
        silently trusted" rule as everywhere else in this module, just
        reached from ``.iter`` instead of ``.test``."""
        self._raises('''
            def do_GET(self):
                path = self.path
                for _ in (1,) * (path == "/api/hidden"):
                    return self._send(1)
                return self._send_json({"error": "not_found"}, 404)
        ''', "path", "unrecognised shape", "loop's iterable")

    def test_tuple_repeat_count_loop_gate_escape_answers_over_real_http(self):
        """The SAME source text as the static test above -- a real
        localhost Handler answers 200 for the hidden path and 404 for
        every other one, exactly the divergence ``extract_routes`` must
        not stay silent about."""
        status, text = _real_http_probe('''
            def do_GET(self):
                path = self.path
                for _ in (1,) * (path == "/api/hidden"):
                    return self._send(1)
                return self._send_json({"error": "not_found"}, 404)
        ''', "GET", "/api/hidden")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(text), {"n": 1})
        status2, text2 = _real_http_probe('''
            def do_GET(self):
                path = self.path
                for _ in (1,) * (path == "/api/hidden"):
                    return self._send(1)
                return self._send_json({"error": "not_found"}, 404)
        ''', "GET", "/api/other")
        self.assertEqual(status2, 404)
        self.assertEqual(json.loads(text2), {"error": "not_found"})

    def test_async_variant_of_the_loop_gate_still_raises(self):
        """``async for`` gets the SAME audit as ``for`` (one shared
        ``isinstance(node, (ast.For, ast.AsyncFor))`` branch) -- exercised
        via a nested ``async def``, static-only, for the same reason
        ``test_async_for_over_a_tracked_subscript_raises`` above is
        (``do_GET`` itself must stay a plain, synchronous handler method).
        In THIS arrangement round 6 finding 2's own closure audit
        (``helper`` reads ``path`` as a free variable) catches it FIRST --
        a fine outcome (the escape closes either way, see
        ``test_async_with_context_expression_touching_path_raises``' own
        docstring for the identical situation) -- so the message below
        names ``helper()``, not the tuple-repeat expression directly; the
        async-for check's OWN, independent message is proven in isolation,
        with the closure escape waived out of the way, by the next test."""
        self._raises('''
            def do_GET(self):
                path = self.path
                async def helper():
                    async for _ in (1,) * (path == "/api/hidden"):
                        return True
                    return False
                if helper():
                    return self._send(1)
        ''', "unrecognised shape", "helper()")

    def test_async_variant_of_the_loop_gate_raises_on_its_own_once_the_closure_escape_is_waived(self):
        """As the test above, but with round 6 finding 2's OWN closure
        escape (``helper()`` itself) explicitly waived -- isolating THIS
        round's ``ast.AsyncFor`` branch, proving it independently
        recognises the SAME tuple-repeat-count shape
        ``test_tuple_repeat_count_loop_gate_raises`` proves for plain
        ``ast.For``, not merely inferred from the shared ``isinstance``
        check."""
        src = _module('''
            def do_GET(self):
                path = self.path
                async def helper():
                    async for _ in (1,) * (path == "/api/hidden"):
                        return True
                    return False
                if helper():
                    return self._send(1)
        ''')
        self._with_waivers({
            ("do_GET", "helper()", "if_test", ""):
                "test-only: isolates round 7 finding 1's OWN ast.AsyncFor "
                "branch from round 6 finding 2's closure audit, which "
                "would otherwise catch this fixture first -- see the "
                "test above",
        })
        with self.assertRaises(ExtractionError) as caught:
            extract_routes(src)
        msg = str(caught.exception)
        self.assertIn("async for", msg)
        self.assertIn("unrecognised shape", msg)
        self.assertIn("path", msg)

    def test_a_boolean_derived_repeat_count_unrelated_to_path_is_unaffected(self):
        """A control precisely targeting THIS round's own repro shape: a
        tuple-repeat-count loop whose boolean is NOT derived from ``path``
        at all must not raise, and must not manufacture a route either."""
        found = {(r.method, r.template) for r in extract_routes(_module('''
            def do_GET(self):
                path = self.path.split("?", 1)[0]
                for _ in (1,) * bool(some_unrelated_flag):
                    pass
                if path == "/api/real":
                    return self._send(2)
        '''))}
        self.assertEqual(found, {("GET", "/api/real")})

    def test_an_unrelated_for_loop_iterable_remains_unaffected(self):
        """A control for the general rule (not just this round's own repro
        shape): an ordinary for-loop over a fixed, untracked iterable is
        untouched by this finding. ``ExecutionControlAndDataFlowTests``'
        own "for/async for iterables" section above
        (``test_a_for_loop_over_something_unrelated_is_unaffected``)
        already covers the identical fixture; repeated here anyway so this
        round's own section proves its negative control self-contained,
        without depending on a DIFFERENT class staying unchanged."""
        found = {(r.method, r.template) for r in extract_routes(_module('''
            def do_GET(self):
                path = self.path.split("?", 1)[0]
                for item in FIXED_LIST:
                    if item == "whatever":
                        return self._send(1)
                if path == "/api/real":
                    return self._send(2)
        '''))}
        self.assertEqual(found, {("GET", "/api/real")})

    # -- rule 2: a receiver chain between a dispatch-selecting Call and -----
    # -- its enclosing invocation --------------------------------------

    def test_attribute_wrapped_selector_raises(self):
        """The reviewer's own second repro, verbatim: a regex-captured
        ``action`` selects ``handlers.get(action, default_handler).serve(
        self)`` -- ONE Attribute hop (``.serve``) between the dispatch-
        selecting ``handlers.get(...)`` Call and the Call that actually
        invokes it. The OLD, single-hop ``_is_callee`` looked only at
        ``handlers.get(...)``'s OWN immediate parent (the ``.serve``
        Attribute, never a Call directly) and answered False, so the
        ``captured``-only exemption (round 5 finding 2b) accepted this as
        harmless -- it is not: ``.serve(self)`` invokes exactly what the
        lookup returns."""
        self._raises('''
            def do_GET(self):
                path = self.path
                m = re.match(r"^/api/([^/]+)$", path)
                if m:
                    action = m.group(1)
                    return handlers.get(action, default_handler).serve(self)
        ''', "unlisted call", "handlers.get(action, default_handler)")

    def test_attribute_wrapped_selector_escape_answers_over_real_http(self):
        """The SAME source text as the static test above, driven over a
        real socket -- 200 for the hidden action, 404 for every other
        one, via the ``.serve(self)`` receiver-chain shape
        ``_is_callee``'s single-hop version could not see."""
        body = '''
            def do_GET(self):
                path = self.path
                m = re.match(r"^/api/([^/]+)$", path)
                if m:
                    action = m.group(1)
                    return handlers.get(action, default_handler).serve(self)
        '''
        extra_globals = {
            "re": re,
            "handlers": {"hidden": _ServeHandler(lambda h: h._send(1))},
            "default_handler": _ServeHandler(
                lambda h: h._send_json({"error": "not_found"}, 404)),
        }
        status, text = _real_http_probe_with_globals(
            body, extra_globals, "GET", "/api/hidden")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(text), {"n": 1})
        status2, text2 = _real_http_probe_with_globals(
            body, extra_globals, "GET", "/api/other")
        self.assertEqual(status2, 404)
        self.assertEqual(json.loads(text2), {"error": "not_found"})

    def test_a_deeper_subscript_mediated_receiver_chain_also_raises(self):
        """A receiver-chain variant reached through a Subscript hop
        instead of an Attribute one -- ``REGISTRY.get(action, DEFAULT)[0]
        (self)`` -- proving ``_is_callee``'s climb recognises
        ``isinstance(parent, (ast.Attribute, ast.Subscript))`` generally,
        not merely the ONE Attribute shape the reviewer's own repro
        happened to use."""
        self._raises('''
            def do_GET(self):
                path = self.path
                m = re.match(r"^/api/([^/]+)$", path)
                if m:
                    action = m.group(1)
                    return REGISTRY.get(action, DEFAULT)[0](self)
        ''', "unlisted call", "REGISTRY.get(action, DEFAULT)")

    def test_a_deeper_subscript_mediated_receiver_chain_escape_answers_over_real_http(self):
        """As ``test_attribute_wrapped_selector_escape_answers_over_real_
        http`` above, for the Subscript-hop variant."""
        body = '''
            def do_GET(self):
                path = self.path
                m = re.match(r"^/api/([^/]+)$", path)
                if m:
                    action = m.group(1)
                    return REGISTRY.get(action, DEFAULT)[0](self)
        '''
        extra_globals = {
            "re": re,
            "REGISTRY": {"hidden": (_ServeHandler(lambda h: h._send(1)), "meta")},
            "DEFAULT": (_ServeHandler(
                lambda h: h._send_json({"error": "not_found"}, 404)), "meta"),
        }
        status, text = _real_http_probe_with_globals(
            body, extra_globals, "GET", "/api/hidden")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(text), {"n": 1})
        status2, text2 = _real_http_probe_with_globals(
            body, extra_globals, "GET", "/api/other")
        self.assertEqual(status2, 404)
        self.assertEqual(json.loads(text2), {"error": "not_found"})

    def test_a_fixed_service_receiver_chain_not_derived_from_path_is_unaffected(self):
        """A control proving the receiver-chain climb is precise, not a
        blanket "any ``.method()`` call now raises": a chain selected by a
        FIXED, untracked key stays exempt exactly as an ordinary reviewed
        service call already is -- the real server.py's own zero new
        raises (below) is the SAME claim proven at scale."""
        found = {(r.method, r.template) for r in extract_routes(_module('''
            def do_GET(self):
                path = self.path.split("?", 1)[0]
                if path == "/api/real":
                    return SERVICES.get("fixed_action", default_handler).serve(self)
                return self._send(2)
        '''))}
        self.assertEqual(found, {("GET", "/api/real")})

    def test_the_real_server_extracts_with_no_new_raises(self):
        """The real server.py has no For/AsyncFor whose ``.iter`` mentions
        a tracked name in an unrecognised shape (its own six real For
        loops each name a fixed module constant/parameter, or -- the two
        ``_handle_reassign``/``_handle_reassign_v2`` ``for target in
        targets:`` loops -- an already-reviewed, waived authorisation-
        target list; see this module's own ``_AUDIT_WAIVERS`` comment for
        that pair), and no dispatch selector reached through a receiver
        chain this round's ``_is_callee`` climb newly exposes -- must
        still extract cleanly: 239 routes, 117 waivers (see
        WaiverFingerprintTests' own pinned count and docstring for the
        exact accounting)."""
        walker = extract_walker()
        self.assertEqual(len(walker.routes), 241)
        self.assertEqual(walker.unreachable, [])
        self.assertEqual(len(route_extract_module._AUDIT_WAIVERS), 116)


# --------------------------------------------------------------------------- #
# #202 repair round 8, finding 1 (external review): round 7's ``_is_callee``   #
# climb recognised exactly TWO transparent wrapper shapes -- ``ast.Attribute``  #
# and ``ast.Subscript`` -- so it stopped, returning False, the INSTANT a       #
# captured-derived Call/Subscript's own immediate parent was anything else.    #
# A ``Tuple``, a ``List`` or an ``IfExp`` holding the node is none of those     #
# two shapes. The reviewer's own repro, verbatim: a regex-captured ``action``  #
# selecting ``(handlers.get(action, default_handler),)[0].serve(self)`` -- a   #
# one-element TUPLE immediately indexed by a literal ``0`` -- extracted as a   #
# single ``/api/{}`` wildcard while live HTTP answered 200/404. The SAME true  #
# spelled as a ``List`` or wrapped in an ``ast.IfExp`` ternary. This exact     #
# category recurred THREE times (round 5 finding 5, round 6 finding 2, round   #
# 7 finding 1) across three different specific wrapper shapes each time, so    #
# THIS round replaces the curated upward climb entirely -- a generic          #
# DOWNWARD scan of the whole enclosing statement (does ``node`` occur          #
# anywhere inside any ``ast.Call``'s own ``.func`` subtree, an ordinary        #
# unrestricted ``ast.walk``) rather than a fourth curated parent-shape hop --  #
# per the review's own stated preference over enumerating another shape.       #
# See ``_is_callee``'s own docstring (route_extract.py) for the full account.  #
#                                                                              #
# Reproduced via git stash (git stash cannot run from inside a test process;   #
# ``git stash push -- backend/hockey_scheduler/web/route_extract.py`` then     #
# ``git stash pop``, isolating JUST the production fix from this file's own    #
# new tests) against the code as it stood immediately before this finding's    #
# fix, ``python3 -m unittest test_route_extract.                              #
# TransparentCompositionCalleeTests -v``, captured verbatim:                   #
#                                                                              #
#   FAIL: test_tuple_indexed_selector_raises   (AssertionError: ExtractionError#
#     not raised) -- the reviewer's own repro verbatim; live probe on the      #
#     SAME source (run separately, not gated on the static side) still shows   #
#     200 for /api/hidden, 404 for /api/other                                 #
#   FAIL: test_list_indexed_selector_raises    (AssertionError: ExtractionError#
#     not raised)                                                             #
#   FAIL: test_ifexp_wrapped_selector_raises   (AssertionError: ExtractionError#
#     not raised)                                                             #
#   Ran 17 tests in 3.175s -- FAILED (failures=3)                             #
#                                                                              #
# exactly the THREE new raise-tests failed -- a genuine "ExtractionError not   #
# raised" AssertionError each, not a vacuous pass. Every OTHER test in this    #
# class passed AGAINST THE PRE-FIX CODE TOO, which is the expected, CORRECT    #
# outcome for each, not a gap in this proof: the three                        #
# ``test_existing_*_form_still_raises`` regression tests assert round 6/7's    #
# OWN shapes, already fixed in THIS branch before this finding existed, so     #
# they pass whether or not round 8's own fix is present -- they exist to      #
# prove round 8 does not REGRESS round 6/7, a question the pre-fix run cannot  #
# even pose; the live-HTTP and negative-control tests pass unconditionally by  #
# design (the live tests assert nothing about ``extract_routes`` at all, and   #
# the negative controls must pass either way, proving THEMSELVES only); the    #
# two MUTATION tests construct their OWN self-contained mutated function       #
# in-process rather than exercising whatever ``route_extract_module.          #
# _is_callee`` currently is, so they are insensitive to the stash either way   #
# by construction (see their own docstrings). All 17 pass against the FIXED    #
# code, asserted below on every run -- ``git stash pop`` restored the fix      #
# immediately after capturing the transcript above; it is not left stashed.    #
#                                                                              #
# #202 repair round 13, finding 1 (external review): ``_is_callee`` -- and    #
# the two LOAD-BEARING-MUTATION tests this class used to carry, proving its    #
# two structural rules independently necessary -- are REMOVED this round, not  #
# because the reasoning above stopped being true (it is still exactly why a    #
# Tuple/List/IfExp-wrapped captured selector needs recognising as "about to    #
# be invoked"), but because that QUESTION no longer changes this module's      #
# ANSWER: the captured-arg exemption ``_is_callee`` existed to gate is itself   #
# retired (see route_extract.py's ``_TRUSTED_BINDING_SOURCES``, own module     #
# comment) -- every captured-only call/subscript now needs its own individual  #
# waiver regardless of WHERE it sits (argument, receiver, or callee), so       #
# ``_is_callee`` had, confirmed by grepping route_extract.py's own source      #
# before removing it, exactly ONE remaining caller (the exemption's own        #
# guard) and is now dead code, deleted rather than left orphaned. The          #
# raise-tests below (this round's own new shapes AND round 6/7's regression    #
# controls) all stay green regardless -- a captured selector wrapped in any    #
# of these compositions was ALWAYS going to raise "unlisted call, no waiver"   #
# once nothing is structurally exempt, whether or not it was "about to be      #
# invoked" -- so they are KEPT, unchanged, as a plain regression proof that    #
# none of these shapes silently stops raising; only their own docstrings note  #
# the mechanism that now closes them. The four negative controls just below    #
# them (a captured id used purely as inert DATA, even when wrapped in the      #
# SAME compositions) now need an explicit waiver apiece, the same as any       #
# other unmodelled call in a synthetic fixture -- see the module-level         #
# ``_waive_matching_node`` helper each one calls.                             #
# --------------------------------------------------------------------------- #
class TransparentCompositionCalleeTests(unittest.TestCase):
    def _raises(self, body, *substrings):
        with self.assertRaises(ExtractionError) as caught:
            extract_routes(_module(body))
        msg = str(caught.exception)
        for s in substrings:
            self.assertIn(s, msg)

    # -- rule: a captured dispatch selector wrapped in a Tuple/List/IfExp  --
    # -- before its receiver chain or invocation must not escape ------------

    def test_tuple_indexed_selector_raises(self):
        """The reviewer's own repro, verbatim: a regex-captured ``action``
        selects ``(handlers.get(action, default_handler),)[0].serve(self)``
        -- a one-element TUPLE immediately indexed by a literal ``0``,
        between the dispatch-selecting Call and the ``.serve`` receiver
        chain round 7 already climbs through. Round 7's climb stops the
        instant it reaches ``node``'s own immediate parent (the Tuple),
        which is neither an ``ast.Attribute`` nor an ``ast.Subscript``, so
        it never even reaches the Subscript (``[0]``) or Attribute
        (``.serve``) layers sitting ABOVE the tuple."""
        self._raises('''
            def do_GET(self):
                path = self.path
                m = re.match(r"^/api/([^/]+)$", path)
                if m:
                    action = m.group(1)
                    return (handlers.get(action, default_handler),)[0].serve(self)
        ''', "unlisted call", "handlers.get(action, default_handler)")

    def test_tuple_indexed_selector_escape_answers_over_real_http(self):
        """The SAME source text as the static test above, driven over a
        real socket -- 200 for the hidden action, 404 for every other one,
        via the tuple-index-then-receiver-chain shape the old
        ``_is_callee`` could not see through."""
        body = '''
            def do_GET(self):
                path = self.path
                m = re.match(r"^/api/([^/]+)$", path)
                if m:
                    action = m.group(1)
                    return (handlers.get(action, default_handler),)[0].serve(self)
        '''
        extra_globals = {
            "re": re,
            "handlers": {"hidden": _ServeHandler(lambda h: h._send(1))},
            "default_handler": _ServeHandler(
                lambda h: h._send_json({"error": "not_found"}, 404)),
        }
        status, text = _real_http_probe_with_globals(
            body, extra_globals, "GET", "/api/hidden")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(text), {"n": 1})
        status2, text2 = _real_http_probe_with_globals(
            body, extra_globals, "GET", "/api/other")
        self.assertEqual(status2, 404)
        self.assertEqual(json.loads(text2), {"error": "not_found"})

    def test_list_indexed_selector_raises(self):
        """The List sibling of the reviewer's own Tuple repro --
        ``[handlers.get(action, default_handler)][0].serve(self)`` -- the
        review's own words, "List ... wrapper[s] fail the same way"."""
        self._raises('''
            def do_GET(self):
                path = self.path
                m = re.match(r"^/api/([^/]+)$", path)
                if m:
                    action = m.group(1)
                    return [handlers.get(action, default_handler)][0].serve(self)
        ''', "unlisted call", "handlers.get(action, default_handler)")

    def test_list_indexed_selector_escape_answers_over_real_http(self):
        """As ``test_tuple_indexed_selector_escape_answers_over_real_http``
        above, for the List-index variant."""
        body = '''
            def do_GET(self):
                path = self.path
                m = re.match(r"^/api/([^/]+)$", path)
                if m:
                    action = m.group(1)
                    return [handlers.get(action, default_handler)][0].serve(self)
        '''
        extra_globals = {
            "re": re,
            "handlers": {"hidden": _ServeHandler(lambda h: h._send(1))},
            "default_handler": _ServeHandler(
                lambda h: h._send_json({"error": "not_found"}, 404)),
        }
        status, text = _real_http_probe_with_globals(
            body, extra_globals, "GET", "/api/hidden")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(text), {"n": 1})
        status2, text2 = _real_http_probe_with_globals(
            body, extra_globals, "GET", "/api/other")
        self.assertEqual(status2, 404)
        self.assertEqual(json.loads(text2), {"error": "not_found"})

    def test_ifexp_wrapped_selector_raises(self):
        """The conditional-expression (``ast.IfExp``) sibling of the same
        shape -- ``(handlers.get(action, default_handler) if
        unrelated_flag else default_handler)(self)``. ``unrelated_flag`` is
        deliberately NOT derived from ``path``/``action`` at all, so the
        ONLY escape this fixture can be exercising is the IfExp
        composition sitting between the captured-derived Call and its
        direct invocation -- not the already-independently-covered "an if
        test touches a tracked name" rule (``_direct_operand_names``),
        which a tracked IfExp ``.test`` would trip through a completely
        different mechanism."""
        self._raises('''
            def do_GET(self):
                path = self.path
                m = re.match(r"^/api/([^/]+)$", path)
                if m:
                    action = m.group(1)
                    return (handlers.get(action, default_handler) if unrelated_flag else default_handler)(self)
        ''', "unlisted call", "handlers.get(action, default_handler)")

    def test_ifexp_wrapped_selector_escape_answers_over_real_http(self):
        """As the Tuple/List live tests above, for the IfExp variant --
        ``unrelated_flag`` is a plain global, True for every request, so
        both live probes below exercise the SAME branch of the ternary and
        the divergence is entirely ``handlers``' own captured lookup, not
        which side of ``if unrelated_flag else`` ran."""
        body = '''
            def do_GET(self):
                path = self.path
                m = re.match(r"^/api/([^/]+)$", path)
                if m:
                    action = m.group(1)
                    return (handlers.get(action, default_handler) if unrelated_flag else default_handler)(self)
        '''
        extra_globals = {
            "re": re, "unrelated_flag": True,
            "handlers": {"hidden": lambda h: h._send(1)},
            "default_handler": lambda h: h._send_json(
                {"error": "not_found"}, 404),
        }
        status, text = _real_http_probe_with_globals(
            body, extra_globals, "GET", "/api/hidden")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(text), {"n": 1})
        status2, text2 = _real_http_probe_with_globals(
            body, extra_globals, "GET", "/api/other")
        self.assertEqual(status2, 404)
        self.assertEqual(json.loads(text2), {"error": "not_found"})

    # -- regression: the round 6/round 7 shapes this round must not break --
    # -- (self-contained per this round's own section, not merely relying  --
    # -- on ExecutionControlAndDataFlowTests/                              --
    # -- LoopIterableAndReceiverChainDispatchTests staying unchanged) ------

    def test_existing_direct_call_form_still_raises(self):
        """Round 6 finding 2's own repro must still raise under the new,
        generic scan -- a direct call needs no composition at all:
        ``candidate.func is node`` on the very first (and only) candidate
        Call is already true."""
        self._raises('''
            def do_GET(self):
                path = self.path
                m = re.match(r"^/api/([^/]+)$", path)
                if m:
                    action = m.group(1)
                    return handlers.get(action, default_handler)(self)
        ''', "unlisted call", "handlers.get(action, default_handler)")

    def test_existing_attribute_receiver_chain_form_still_raises(self):
        """Round 7 finding 1's own repro must still raise: an Attribute
        hop alone (no Tuple/List/IfExp at all) is still recognised by the
        new scan -- ``ast.walk(candidate.func)`` descends into an
        Attribute's own ``.value`` exactly as readily as into anything
        else, so this shape needs no special case any more than the new
        ones do."""
        self._raises('''
            def do_GET(self):
                path = self.path
                m = re.match(r"^/api/([^/]+)$", path)
                if m:
                    action = m.group(1)
                    return handlers.get(action, default_handler).serve(self)
        ''', "unlisted call", "handlers.get(action, default_handler)")

    def test_existing_subscript_receiver_chain_form_still_raises(self):
        """Round 7 finding 1's own Subscript-hop repro must still raise,
        for the same reason as the Attribute one above."""
        self._raises('''
            def do_GET(self):
                path = self.path
                m = re.match(r"^/api/([^/]+)$", path)
                if m:
                    action = m.group(1)
                    return REGISTRY.get(action, DEFAULT)[0](self)
        ''', "unlisted call", "REGISTRY.get(action, DEFAULT)")

    # -- negative controls: a captured id used purely as inert DATA, never --
    # -- a callee, must stay exempt -- including when it is ALSO wrapped   --
    # -- in the exact tuple/list/ifexp shapes this round newly recognises, --
    # -- proving the fix is precise (position-sensitive), not a blanket    --
    # -- "captured value touched by any transparent wrapper now raises" ----

    def test_captured_id_as_a_plain_argument_is_still_unaffected(self):
        """Round 5 finding 2b's own control: a captured id handed to a
        FIXED, KNOWN service as a plain ARGUMENT (never a callee) stays
        exempt -- via its own individually reviewed waiver (#202 repair
        round 13, finding 1 retired the STRUCTURAL version of this
        exemption; see route_extract.py's ``_TRUSTED_BINDING_SOURCES``,
        own module comment), installed here the same way any other
        unmodelled call in a synthetic fixture needs one."""
        src = _waive_matching_node(self.addCleanup, '''
            def do_GET(self):
                path = self.path.split("?", 1)[0]
                api = STATE.api
                m = re.match(r"^/api/items/([^/]+)$", path)
                if m:
                    gid = m.group(1)
                    return self._send_api(api.get_item(gid))
        ''', "api.get_item(gid)")
        found = {(r.method, r.template) for r in extract_routes(src)}
        self.assertEqual(found, {("GET", "/api/items/{}")})

    def test_captured_id_tuple_indexed_as_a_plain_argument_is_unaffected(self):
        """The tuple-index SHAPE round 8 recognises for the CALLEE
        position, but used here as a plain ARGUMENT instead: ``api.
        get_item((gid, "meta")[0])`` extracts ``gid`` through the
        identical tuple-index composition the vulnerable repro used, but
        the result is handed to a fixed service as DATA, never invoked.
        An explicit waiver (see the class-level comment above for why)
        keeps this isolating what it actually checks -- that this
        position, not merely this composition, is what a real waiver
        needs to name."""
        src = _waive_matching_node(self.addCleanup, '''
            def do_GET(self):
                path = self.path.split("?", 1)[0]
                api = STATE.api
                m = re.match(r"^/api/items/([^/]+)$", path)
                if m:
                    gid = m.group(1)
                    return self._send_api(api.get_item((gid, "meta")[0]))
        ''', "api.get_item((gid, 'meta')[0])")
        found = {(r.method, r.template) for r in extract_routes(src)}
        self.assertEqual(found, {("GET", "/api/items/{}")})

    def test_captured_id_list_indexed_as_a_plain_argument_is_unaffected(self):
        """As the tuple-index control above, for a List."""
        src = _waive_matching_node(self.addCleanup, '''
            def do_GET(self):
                path = self.path.split("?", 1)[0]
                api = STATE.api
                m = re.match(r"^/api/items/([^/]+)$", path)
                if m:
                    gid = m.group(1)
                    return self._send_api(api.get_item([gid, "meta"][0]))
        ''', "api.get_item([gid, 'meta'][0])")
        found = {(r.method, r.template) for r in extract_routes(src)}
        self.assertEqual(found, {("GET", "/api/items/{}")})

    def test_captured_id_in_ifexp_as_a_plain_argument_is_unaffected(self):
        """As the tuple/list controls above, for an IfExp whose BOTH
        branches are the same captured id -- still never a callee."""
        src = _waive_matching_node(self.addCleanup, '''
            def do_GET(self):
                path = self.path.split("?", 1)[0]
                api = STATE.api
                m = re.match(r"^/api/items/([^/]+)$", path)
                if m:
                    gid = m.group(1)
                    return self._send_api(api.get_item(gid if unrelated_flag else gid))
        ''', "api.get_item(gid if unrelated_flag else gid)")
        found = {(r.method, r.template) for r in extract_routes(src)}
        self.assertEqual(found, {("GET", "/api/items/{}")})

    def test_a_fixed_service_receiver_chain_not_derived_from_path_is_unaffected(self):
        """Round 7's own control, unchanged and needing no waiver: a
        chain selected by a FIXED, untracked key mentions no tracked name
        at all, so it never even reaches an unlisted-call question,
        structural exemption or waiver alike."""
        found = {(r.method, r.template) for r in extract_routes(_module('''
            def do_GET(self):
                path = self.path.split("?", 1)[0]
                if path == "/api/real":
                    return SERVICES.get("fixed_action", default_handler).serve(self)
                return self._send(2)
        '''))}
        self.assertEqual(found, {("GET", "/api/real")})

    def test_the_real_server_extracts_with_no_new_raises(self):
        """The real server.py's own 39 captured-only call/subscript sites
        (confirmed by direct instrumentation, see route_extract.py's own
        round-9 comment) are each independently confirmed to never place
        the captured value in a callee/receiver position -- proven,
        before round 13, by ``_is_callee`` returning False for every one
        of them; after round 13 (which deleted ``_is_callee`` along with
        the exemption it gated -- see ``_TRUSTED_BINDING_SOURCES``'s own
        module comment) that fact no longer changes any OUTCOME here,
        since 37 of those 39 sites now carry their own individual
        ``_AUDIT_WAIVERS`` entry regardless of position (the remaining 2
        never fit the shape at all and already had their own waivers, see
        ``CapturedArgumentTransferTests`` below for both counts' own
        breakdown). Must still extract cleanly: 239 routes, 117 waivers
        (see WaiverFingerprintTests' own pinned count and docstring for
        the exact accounting)."""
        walker = extract_walker()
        self.assertEqual(len(walker.routes), 241)
        self.assertEqual(walker.unreachable, [])
        self.assertEqual(len(route_extract_module._AUDIT_WAIVERS), 116)


# --------------------------------------------------------------------------- #
# #202 repair round 9, finding 1 (external review, HISTORICAL -- see round    #
# 13, finding 1 below for the CURRENT rule): the THIRD recurrence of "the     #
# captured-value exemption is broader than it should be" (round 6: direct-    #
# compare shapes; round 8: transparent tuple/list/IfExp composition). Round   #
# 8's generic ``_is_callee`` scan answers a purely SYNTACTIC question -- does #
# ``node`` sit inside some Call's own ``.func`` subtree, however much         #
# composition is in between -- which cannot see a captured selector handed   #
# to an ARBITRARY function as a plain ARGUMENT and invoked from INSIDE that   #
# function's own body (a different function entirely): the reviewer's own    #
# ``invoke(handlers.get(action, default_handler), self)``, with              #
# ``invoke = lambda fn, h: fn.serve(h)``. Rather than teaching ``_is_callee`` #
# a fourth curated shape -- the review's own diagnosis is that shape-by-     #
# shape closure is not converging -- round 9 flipped the ``captured``        #
# exemption's default: a captured value handed to a call was inert ONLY when #
# the call TARGET was on a small, explicit, reviewed allowlist, never merely #
# because the call happened not to be ``self.``. #202 repair round 13,       #
# finding 1 (external review) RETIRED that allowlist (and the provenance     #
# gate rounds 10-11 layered on top of it) entirely -- see                    #
# ``_TRUSTED_BINDING_SOURCES``'s own module comment in route_extract.py --   #
# so the five named repros below still raise for the SAME reason they always #
# did (``invoke``/``operator.call``/``setattr``/``list.append`` were never   #
# trusted, allowlist or not), but the two POSITIVE controls that follow them #
# now assert the OPPOSITE of what they used to: a captured id handed to the  #
# API facade, in a SYNTHETIC function that is not one of the 37 real,        #
# individually reviewed server.py sites, now needs its own waiver too, the   #
# same as everything else this module cannot structurally trust.            #
# --------------------------------------------------------------------------- #
class CapturedArgumentTransferTests(unittest.TestCase):
    def _raises(self, body, *substrings):
        with self.assertRaises(ExtractionError) as caught:
            extract_routes(_module(body))
        msg = str(caught.exception)
        for s in substrings:
            self.assertIn(s, msg)

    # -- the five named repros: invoke (positional/keyword), operator.call, --
    # -- and a callback stashed via setattr/a container -- each proven BOTH --
    # -- as a live-HTTP 200/404 divergence (the shape really is exploitable --
    # -- Python, independent of this module) and as a static raise ----------

    def test_invoke_positional_argument_transfer_raises(self):
        """The reviewer's own repro, verbatim: ``handlers.get(action,
        default_handler)`` is a plain ARGUMENT of ``invoke(...)`` -- never
        inside any call's own ``.func`` subtree in this statement -- so
        round 8's ``_is_callee`` scan correctly (and, after this round,
        harmlessly) answers False for it; before this round, nothing else
        asked whether ``invoke`` -- a bare, unmodelled Name callee -- was
        itself a call this module has any basis to trust."""
        self._raises('''
            def do_GET(self):
                path = self.path
                m = re.match(r"^/api/([^/]+)$", path)
                if m:
                    action = m.group(1)
                    return invoke(handlers.get(action, default_handler), self)
        ''', "unlisted call",
             "invoke(handlers.get(action, default_handler), self)")

    def test_invoke_positional_argument_transfer_escape_answers_over_real_http(self):
        """The SAME source text as the static test above, driven over a
        real socket with ``invoke = lambda fn, h: fn(h)`` -- 200 for the
        hidden action, 404 for every other one. Proves the shape is
        genuinely exploitable Python, not merely an extraction gap."""
        body = '''
            def do_GET(self):
                path = self.path
                m = re.match(r"^/api/([^/]+)$", path)
                if m:
                    action = m.group(1)
                    return invoke(handlers.get(action, default_handler), self)
        '''
        extra_globals = {
            "re": re, "invoke": lambda fn, h: fn(h),
            "handlers": {"hidden": lambda h: h._send(1)},
            "default_handler": lambda h: h._send_json(
                {"error": "not_found"}, 404),
        }
        status, text = _real_http_probe_with_globals(
            body, extra_globals, "GET", "/api/hidden")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(text), {"n": 1})
        status2, text2 = _real_http_probe_with_globals(
            body, extra_globals, "GET", "/api/other")
        self.assertEqual(status2, 404)
        self.assertEqual(json.loads(text2), {"error": "not_found"})

    def test_invoke_keyword_argument_transfer_raises(self):
        """The reviewer's own words, "The keyword form escapes too" --
        the identical shape with ``fn``/``h`` passed as KEYWORD arguments
        rather than positional. ``ast.Call.keywords`` is walked by the
        SAME ``ast.walk``/``ast.iter_child_nodes`` machinery as
        ``ast.Call.args``, so this needs no separate mechanism to close,
        only this test to confirm it -- proving the fix is not
        accidentally positional-argument-specific."""
        self._raises('''
            def do_GET(self):
                path = self.path
                m = re.match(r"^/api/([^/]+)$", path)
                if m:
                    action = m.group(1)
                    return invoke(fn=handlers.get(action, default_handler), h=self)
        ''', "unlisted call",
             "invoke(fn=handlers.get(action, default_handler), h=self)")

    def test_invoke_keyword_argument_transfer_escape_answers_over_real_http(self):
        body = '''
            def do_GET(self):
                path = self.path
                m = re.match(r"^/api/([^/]+)$", path)
                if m:
                    action = m.group(1)
                    return invoke(fn=handlers.get(action, default_handler), h=self)
        '''
        extra_globals = {
            "re": re, "invoke": lambda fn, h: fn(h),
            "handlers": {"hidden": lambda h: h._send(1)},
            "default_handler": lambda h: h._send_json(
                {"error": "not_found"}, 404),
        }
        status, text = _real_http_probe_with_globals(
            body, extra_globals, "GET", "/api/hidden")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(text), {"n": 1})
        status2, text2 = _real_http_probe_with_globals(
            body, extra_globals, "GET", "/api/other")
        self.assertEqual(status2, 404)
        self.assertEqual(json.loads(text2), {"error": "not_found"})

    def test_operator_call_argument_transfer_raises(self):
        """The reviewer's own independent reproduction: a STDLIB
        higher-order callable (``operator.call``, 3.11+) rather than a
        hand-written one, combined with the round-7 receiver-chain hop
        (``.serve``) -- proving the fix is not specific to a
        hand-rolled ``invoke`` helper this module could special-case by
        name."""
        self._raises('''
            def do_GET(self):
                path = self.path
                m = re.match(r"^/api/([^/]+)$", path)
                if m:
                    action = m.group(1)
                    return operator.call(handlers.get(action, default_handler).serve, self)
        ''', "unlisted call",
             "operator.call(handlers.get(action, default_handler).serve, self)")

    def test_operator_call_argument_transfer_escape_answers_over_real_http(self):
        body = '''
            def do_GET(self):
                path = self.path
                m = re.match(r"^/api/([^/]+)$", path)
                if m:
                    action = m.group(1)
                    return operator.call(handlers.get(action, default_handler).serve, self)
        '''
        extra_globals = {
            "re": re, "operator": __import__("operator"),
            "handlers": {"hidden": _ServeHandler(lambda h: h._send(1))},
            "default_handler": _ServeHandler(
                lambda h: h._send_json({"error": "not_found"}, 404)),
        }
        status, text = _real_http_probe_with_globals(
            body, extra_globals, "GET", "/api/hidden")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(text), {"n": 1})
        status2, text2 = _real_http_probe_with_globals(
            body, extra_globals, "GET", "/api/other")
        self.assertEqual(status2, 404)
        self.assertEqual(json.loads(text2), {"error": "not_found"})

    def test_setattr_stored_callback_raises(self):
        """A captured selector STASHED via ``setattr`` rather than
        directly invoked -- ``setattr`` is a plain builtin Name callee,
        never inside any call's own ``.func`` subtree here either, so
        this is the SAME argument-transfer gap as ``invoke``, just with
        the invocation deferred to a LATER statement (``self._cb(self)``)
        instead of happening inline. The storing statement itself
        (`setattr(...)`) is what raises -- a bare ``ast.Expr`` statement,
        audited by :meth:`_DispatchWalker._audit_function`'s own
        bare-Expr scan (#202 repair round 3, finding G) the same way any
        other statement position is."""
        self._raises('''
            def do_GET(self):
                path = self.path
                m = re.match(r"^/api/([^/]+)$", path)
                if m:
                    action = m.group(1)
                    setattr(self, "_cb", handlers.get(action, default_handler))
                    return self._cb(self)
        ''', "unlisted call",
             "setattr(self, '_cb', handlers.get(action, default_handler))")

    def test_setattr_stored_callback_escape_answers_over_real_http(self):
        body = '''
            def do_GET(self):
                path = self.path
                m = re.match(r"^/api/([^/]+)$", path)
                if m:
                    action = m.group(1)
                    setattr(self, "_cb", handlers.get(action, default_handler))
                    return self._cb(self)
        '''
        extra_globals = {
            "re": re,
            "handlers": {"hidden": lambda h: h._send(1)},
            "default_handler": lambda h: h._send_json(
                {"error": "not_found"}, 404),
        }
        status, text = _real_http_probe_with_globals(
            body, extra_globals, "GET", "/api/hidden")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(text), {"n": 1})
        status2, text2 = _real_http_probe_with_globals(
            body, extra_globals, "GET", "/api/other")
        self.assertEqual(status2, 404)
        self.assertEqual(json.loads(text2), {"error": "not_found"})

    def test_container_append_stored_callback_raises(self):
        """The container sibling of the ``setattr`` case above -- a
        captured selector stashed via ``list.append`` and invoked, in a
        LATER statement, through a Subscript-callee (``bucket[0](self)``).
        ``bucket.append(...)`` is what raises: an arbitrary, unmodelled
        non-``self.`` call (root name ``bucket``, never ``api``) whose
        only argument's only tracked mention is the captured ``action``
        -- exactly the shape the OLD blanket "any non-self call" default
        wrongly trusted."""
        self._raises('''
            def do_GET(self):
                path = self.path
                m = re.match(r"^/api/([^/]+)$", path)
                if m:
                    action = m.group(1)
                    bucket = []
                    bucket.append(handlers.get(action, default_handler))
                    return bucket[0](self)
        ''', "unlisted call",
             "bucket.append(handlers.get(action, default_handler))")

    def test_container_append_stored_callback_escape_answers_over_real_http(self):
        body = '''
            def do_GET(self):
                path = self.path
                m = re.match(r"^/api/([^/]+)$", path)
                if m:
                    action = m.group(1)
                    bucket = []
                    bucket.append(handlers.get(action, default_handler))
                    return bucket[0](self)
        '''
        extra_globals = {
            "re": re,
            "handlers": {"hidden": lambda h: h._send(1)},
            "default_handler": lambda h: h._send_json(
                {"error": "not_found"}, 404),
        }
        status, text = _real_http_probe_with_globals(
            body, extra_globals, "GET", "/api/hidden")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(text), {"n": 1})
        status2, text2 = _real_http_probe_with_globals(
            body, extra_globals, "GET", "/api/other")
        self.assertEqual(status2, 404)
        self.assertEqual(json.loads(text2), {"error": "not_found"})

    # -- #202 repair round 13, finding 1: a captured id handed to the API   -
    # -- facade, or to a dict LITERAL of API-facade values, is no longer    -
    # -- exempt merely by shape -- these are the SAME two fixtures round 9  -
    # -- introduced as "the real, benign shapes stay exempt" negative       -
    # -- controls; retirement flips their own expectation, since a          -
    # -- SYNTHETIC ``do_GET`` (never one of the 37 real, individually       -
    # -- reviewed server.py sites -- different fn_name, different exact     -
    # -- text/position) cannot match any real _AUDIT_WAIVERS entry ---------

    def test_captured_scalar_to_api_facade_call_now_needs_its_own_waiver(self):
        """The real server.py's own overwhelming-majority shape (37 of 39
        real captured-only sites, by direct instrumentation): a captured
        id handed to the API FACADE as a plain argument. Before round 13
        this stayed exempt by STRUCTURE alone (a proven ``api = STATE.
        api`` binding); now it raises exactly like any other unlisted
        call, because this SYNTHETIC ``do_GET`` is not one of the real,
        individually fingerprinted ``_AUDIT_WAIVERS`` sites -- see
        ``test_a_matching_individual_waiver_exempts_the_same_api_call_
        shape`` below for the SAME shape WITH a matching waiver."""
        self._raises('''
            def do_GET(self):
                path = self.path.split("?", 1)[0]
                api = STATE.api
                m = re.match(r"^/api/items/([^/]+)$", path)
                if m:
                    gid = m.group(1)
                    return self._send_api(api.get_item(gid))
        ''', "unlisted call")

    def test_captured_scalar_to_dict_literal_of_api_values_subscript_now_needs_its_own_waiver(self):
        """The real ``_handle_setup``/``_handle_setup_v2``/``do_POST``
        delete- and action-dispatch shape: a dict LITERAL of ``api.X``
        values, keyed by a captured id. Before round 13 this stayed
        exempt by STRUCTURE alone (the retired ``ast.Dict`` recursion
        rule); now it raises exactly like any other unlisted subscript --
        see ``test_a_matching_individual_waiver_exempts_the_same_dict_
        literal_shape`` below for the SAME shape WITH a matching waiver."""
        self._raises('''
            def do_GET(self):
                path = self.path.split("?", 1)[0]
                api = STATE.api
                m = re.match(r"^/api/setup/([^/]+)/delete$", path)
                if m:
                    kind = m.group(1)
                    return self._send_api({"team": api.delete_team, "venue": api.delete_venue}[kind])
        ''', "indexes a container")

    # -- positive-path proof of the REPLACEMENT mechanism: the SAME two    -
    # -- shapes above, each with its OWN individually reviewed waiver      -
    # -- (added and removed by the test itself, never the real dict),      -
    # -- computed via the REAL _waiver_key -- never hand-guessed -- so a    -
    # -- key typo cannot make either test pass for the wrong reason --------

    def _waive_the_one_call_or_subscript_node(self, body, matching_text):
        """Thin wrapper around the module-level ``_waive_matching_node``
        (see its own docstring), binding ``cleanup_registrar`` to this
        TestCase's own ``self.addCleanup``."""
        return _waive_matching_node(self.addCleanup, body, matching_text)

    def test_a_matching_individual_waiver_exempts_the_same_api_call_shape(self):
        """The IDENTICAL source text as
        ``test_captured_scalar_to_api_facade_call_now_needs_its_own_waiver``
        above, but with its own ``_AUDIT_WAIVERS`` entry -- exactly the
        discipline round 13 now requires of EVERY real captured-only call
        site (see the 37 entries in route_extract.py tagged "round 13
        finding 1"). Passing this proves the waiver mechanism itself
        correctly exempts a genuinely-reviewed site, not merely that
        everything unreviewed raises."""
        body = '''
            def do_GET(self):
                path = self.path.split("?", 1)[0]
                api = STATE.api
                m = re.match(r"^/api/items/([^/]+)$", path)
                if m:
                    gid = m.group(1)
                    return self._send_api(api.get_item(gid))
        '''
        src = self._waive_the_one_call_or_subscript_node(
            body, "api.get_item(gid)")
        found = {(r.method, r.template) for r in extract_routes(src)}
        self.assertEqual(found, {("GET", "/api/items/{}")})

    def test_a_matching_individual_waiver_exempts_the_same_dict_literal_shape(self):
        """The IDENTICAL source text as
        ``test_captured_scalar_to_dict_literal_of_api_values_subscript_
        now_needs_its_own_waiver`` above, but with its own
        ``_AUDIT_WAIVERS`` entry -- the SAME positive-path proof as the
        Call-branch test above, for the Subscript branch."""
        body = '''
            def do_GET(self):
                path = self.path.split("?", 1)[0]
                api = STATE.api
                m = re.match(r"^/api/setup/([^/]+)/delete$", path)
                if m:
                    kind = m.group(1)
                    return self._send_api({"team": api.delete_team, "venue": api.delete_venue}[kind])
        '''
        src = self._waive_the_one_call_or_subscript_node(
            body, "{'team': api.delete_team, 'venue': api.delete_venue}[kind]")
        found = {(r.method, r.template) for r in extract_routes(src)}
        self.assertEqual(found, {("GET", "/api/setup/{}/delete")})

    def test_the_real_server_extracts_with_no_new_raises(self):
        """The real server.py, with round 13's per-site waivers live
        (round 9's allowlist gate they replace is retired -- see
        ``_TRUSTED_BINDING_SOURCES``'s own module comment in
        route_extract.py): still extracts cleanly, 239 routes, 117
        waivers (77 through round 9 + 37 round-13, finding-1 additions +
        3 #205 blocker 1 additions, one per real captured-only call/
        subscript site the retired allowlist used to cover, plus the
        three new sites the #205 blocker 1 availability-summary fix
        introduces -- see WaiverFingerprintTests' own pinned count and
        docstring for the exact accounting, and this module's
        ``_AUDIT_WAIVERS`` dict, entries tagged "round 13 finding 1" or
        "#205 blocker 1", for each one's own review comment)."""
        walker = extract_walker()
        self.assertEqual(len(walker.routes), 241)
        self.assertEqual(walker.unreachable, [])
        self.assertEqual(len(route_extract_module._AUDIT_WAIVERS), 116)


class _EvilApiFacade:
    """A stand-in for whatever a shadowed/parameter-shadowed ``api`` name
    actually resolves to -- exposes exactly the higher-order-invoker shape
    the round-10 reviewer's own repro needs (``invoke(fn, target)`` calls
    ``fn(target)``), plus two selectable actions keyed the same way the
    real ``_handle_setup``-style dispatch tables this module's own
    ``_AUDIT_WAIVERS`` individually reviews are (see
    ``CapturedArgumentTransferTests``'s own Dict-literal coverage) -- so
    the SAME dict-literal-selection shape that is genuinely safe when it
    is one of the real, individually reviewed sites is reused here to
    show it is NOT safe for an arbitrary, unreviewed root."""

    def invoke(self, fn, target):
        return fn(target)

    def hidden(self, h):
        h._send(1)

    def other(self, h):
        h._send_json({"error": "not_found"}, 404)


class CapturedArgumentProvenanceTests(unittest.TestCase):
    """#202 repair round 10 (external review): ``_captured_arg_safe_callee``
    authenticates the SPELLING ``"api"``, not the actual facade binding or
    method -- ``_CAPTURED_ARG_SAFE_CALLEE_ROOTS``/``_captured_arg_safe_
    callee`` alone accepted every attribute chain rooted at
    ``ast.Name("api")`` regardless of what that name was actually bound to
    at the use site. DEMONSTRATED by the reviewer, live over real HTTP:

        api = evil_api
        return api.invoke({"hidden": api.hidden, "other": api.other}[action], self)

    -- ``/api/hidden`` answers 200, ``/api/other`` answers 404, while
    static extraction (round 9's own fix, otherwise untouched) stayed
    silent, reporting only the wildcard ``GET /api/{}``. Both the
    higher-order invoker (``api.invoke``) and the dict-literal selector's
    own values (``api.hidden``/``api.other``) passed because their roots
    are lexically named ``api``, never asking what ``api`` actually,
    provably IS at that point in the function.

    Round 10 ties the exemption to PROVENANCE instead
    (``_captured_arg_trusted_roots``/``_has_dominating_trusted_binding``/
    ``_name_rebinding_sites``, see their own docstrings): a name earns the
    exemption, for a GIVEN function, only when that function's own body
    proves it is bound EXACTLY once, at a dominating, top-level,
    never-rebound ``name = STATE.api`` assignment -- the SAME "is this
    name really what it claims to be" discipline ``_is_self_call``/
    ``_is_self_path`` already established for ``self`` (see those
    functions' own docstrings), extended to an ORDINARY local Python's own
    calling convention does not bind for free the way the first parameter
    always is.
    """

    def _raises(self, body, *substrings):
        with self.assertRaises(ExtractionError) as caught:
            extract_routes(_module(body))
        msg = str(caught.exception)
        for s in substrings:
            self.assertIn(s, msg)

    # -- the reviewer's own named repro: IDENTICAL source, static AND    -
    # -- live -- shadowed api, nested dict-literal selection, and a      -
    # -- higher-order invoker all in one statement --------------------------

    def test_shadowed_api_dict_literal_higher_order_invoke_raises(self):
        """The reviewer's own round-10 repro, verbatim. ``api = evil_api``
        rebinds the name one line before it is used; ``{"hidden": api.
        hidden, "other": api.other}[action]`` is the SAME "nested
        dict-literal selection" shape the real ``_handle_setup``-style
        dispatch tables use (round 9's own reviewed-safe shape); and
        ``api.invoke(...)`` is the SAME higher-order-transfer shape round
        9's own ``invoke``/``operator.call`` repros used, just spelled as
        an attribute of a name that HAPPENS to be on the allowlist.
        Before this round both passed because their roots are lexically
        named ``api`` -- this must now raise."""
        self._raises('''
            def do_GET(self):
                path = self.path
                m = re.match(r"^/api/([^/]+)$", path)
                if m:
                    action = m.group(1)
                    api = evil_api
                    return api.invoke({"hidden": api.hidden, "other": api.other}[action], self)
        ''', "unlisted call")

    def test_shadowed_api_dict_literal_higher_order_invoke_escape_answers_over_real_http(self):
        """The SAME source text as the static test above, driven over a
        real socket -- 200 for the hidden action, 404 for every other one
        -- proving the shape is genuinely exploitable Python, independent
        of this module, exactly the divergence the reviewer's own report
        cites."""
        body = '''
            def do_GET(self):
                path = self.path
                m = re.match(r"^/api/([^/]+)$", path)
                if m:
                    action = m.group(1)
                    api = evil_api
                    return api.invoke({"hidden": api.hidden, "other": api.other}[action], self)
        '''
        extra_globals = {"re": re, "evil_api": _EvilApiFacade()}
        status, text = _real_http_probe_with_globals(
            body, extra_globals, "GET", "/api/hidden")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(text), {"n": 1})
        status2, text2 = _real_http_probe_with_globals(
            body, extra_globals, "GET", "/api/other")
        self.assertEqual(status2, 404)
        self.assertEqual(json.loads(text2), {"error": "not_found"})

    # -- parameter shadowing: the ONLY binding is not an assignment at   -
    # -- all, so there is no RHS for _has_dominating_trusted_binding to  -
    # -- even compare against ------------------------------------------------

    def test_parameter_shadowing_raises(self):
        """``api`` is never ASSIGNED anywhere in this function -- its
        only binding is the function's own PARAMETER, one of the binding
        forms :func:`_name_rebinding_sites` watches for precisely because
        it can never be an ``ast.Assign`` :func:`_has_dominating_trusted_
        binding` could accept: whoever calls ``do_GET`` fully controls
        what ``api`` is inside it. (A NESTED closure's own parameter was
        tried first and rejected as this test's shape: a closure that
        also mentions a tracked name in its own body gets its NAME
        implicitly tainted by the round-6 finding-2 rule, which raises
        at the OUTER call to the closure for an unrelated, pre-existing
        reason -- correctly, but not in a way that isolates round 10's
        OWN mechanism, confirmed by re-running this class's own
        falsifiability check with that shape. Shadowing the entry
        point's own parameter directly avoids that confound: nothing
        else in this fixture would raise if this mechanism did not.)"""
        self._raises('''
            def do_GET(self, api=evil_api):
                path = self.path
                m = re.match(r"^/api/([^/]+)$", path)
                if m:
                    action = m.group(1)
                    return api.invoke({"hidden": api.hidden, "other": api.other}[action], self)
        ''', "unlisted call")

    def test_parameter_shadowing_escape_answers_over_real_http(self):
        """The SAME source text as the static test above, driven live:
        ``BaseHTTPRequestHandler`` itself calls ``self.do_GET()`` with NO
        arguments, so the DEFAULT value is what a real request actually
        reaches -- ``api=evil_api`` is simultaneously realistic (a
        parameter shadowing a name via its own default is ordinary
        Python) and live-testable via completely normal HTTP dispatch,
        with no extra plumbing needed to drive the parameter's value."""
        body = '''
            def do_GET(self, api=evil_api):
                path = self.path
                m = re.match(r"^/api/([^/]+)$", path)
                if m:
                    action = m.group(1)
                    return api.invoke({"hidden": api.hidden, "other": api.other}[action], self)
        '''
        extra_globals = {"re": re, "evil_api": _EvilApiFacade()}
        status, text = _real_http_probe_with_globals(
            body, extra_globals, "GET", "/api/hidden")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(text), {"n": 1})
        status2, text2 = _real_http_probe_with_globals(
            body, extra_globals, "GET", "/api/other")
        self.assertEqual(status2, 404)
        self.assertEqual(json.loads(text2), {"error": "not_found"})

    # -- rebinding AFTER a valid facade assignment: the LATER, evil       -
    # -- assignment is the one that actually reaches the use at runtime --

    def test_rebinding_after_a_valid_facade_assignment_raises(self):
        """A genuine ``api = STATE.api`` is NOT the end of the story if a
        SECOND assignment follows it -- :func:`_name_rebinding_sites`
        finds TWO Store-context sites for ``"api"`` here, so
        :func:`_has_dominating_trusted_binding`'s "exactly one" test
        fails regardless of which one is real: at the point of use,
        ``api`` really is ``evil_api``, not ``STATE.api``."""
        self._raises('''
            def do_GET(self):
                path = self.path
                api = STATE.api
                m = re.match(r"^/api/([^/]+)$", path)
                if m:
                    action = m.group(1)
                    api = evil_api
                    return api.invoke({"hidden": api.hidden, "other": api.other}[action], self)
        ''', "unlisted call")

    def test_rebinding_after_a_valid_facade_assignment_escape_answers_over_real_http(self):
        """The SAME source text as the static test above, driven live --
        the LATER assignment is the one a real request actually reaches,
        so ``STATE`` itself only needs an ``.api`` attribute to satisfy
        the (dead, never-reached-at-runtime) first assignment; what it
        holds is irrelevant to the outcome, exactly as the static
        analysis question ("is there exactly one binding") does not
        depend on which one is real either."""
        body = '''
            def do_GET(self):
                path = self.path
                api = STATE.api
                m = re.match(r"^/api/([^/]+)$", path)
                if m:
                    action = m.group(1)
                    api = evil_api
                    return api.invoke({"hidden": api.hidden, "other": api.other}[action], self)
        '''
        extra_globals = {
            "re": re, "evil_api": _EvilApiFacade(),
            "STATE": types.SimpleNamespace(api=object()),
        }
        status, text = _real_http_probe_with_globals(
            body, extra_globals, "GET", "/api/hidden")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(text), {"n": 1})
        status2, text2 = _real_http_probe_with_globals(
            body, extra_globals, "GET", "/api/other")
        self.assertEqual(status2, 404)
        self.assertEqual(json.loads(text2), {"error": "not_found"})

    # -- rebinding BEFORE a valid facade assignment: textually ambiguous, -
    # -- so this raises too, even though at RUNTIME only the LAST         -
    # -- assignment (the genuine one) ever reaches a use --------------------

    def test_rebinding_before_a_valid_facade_assignment_raises(self):
        """The mirror image of the "after" case: a throwaway ``api =
        evil_api`` is IMMEDIATELY overwritten by a genuine ``api =
        STATE.api`` before any use. At RUNTIME this specific written
        form is harmless (the LAST assignment always wins), so unlike the
        "after" case there is no live 200/404 divergence to demonstrate
        here -- only the static shape is ambiguous. Static extraction
        still raises: :func:`_name_rebinding_sites` counts TWO sites
        regardless of order, and this module has no control-flow/
        dominance analysis to distinguish "rebound, then fixed" from
        "fixed, then rebound" -- exactly the reviewer's own "reasonable
        design" ("assigned exactly once ... and it is never reassigned
        afterward"), applied SYMMETRICALLY rather than only to the
        direction that happens to be exploitable in this exact fixture.
        Pairing this test with the "after" test above (both fail the
        SAME "exactly one site" gate, in either order) is what rules out
        a narrower, buggier implementation that only checks "is the LAST
        assignment trusted" and would silently pass this direction."""
        self._raises('''
            def do_GET(self):
                path = self.path
                api = evil_api
                api = STATE.api
                m = re.match(r"^/api/([^/]+)$", path)
                if m:
                    action = m.group(1)
                    return self._send_api(api.get_item(action))
        ''', "unlisted call")

    # -- negative control: a genuine, provably-dominating STATE.api        -
    # -- binding remains exempt, on BOTH the Call branch and the           -
    # -- Subscript/Dict-literal branch, in the SAME function ----------------

    def test_genuine_dominating_state_api_binding_is_provably_bound_but_no_longer_structurally_exempt(self):
        """The real server.py shape round 10's provenance gate exists for
        -- ``api = STATE.api`` as a single, dominating, top-level,
        never-rebound assignment -- is STILL provably bound at the
        provenance-helper level (:func:`_has_dominating_trusted_binding`
        is KEPT, fixed for round 13's own finding 2; see
        ``_TRUSTED_BINDING_SOURCES``'s own module comment,
        route_extract.py, for why it is no longer WIRED into any
        exemption). #202 repair round 13, finding 1 retired the
        STRUCTURAL exemption this proof used to feed: proving a name's
        BINDING is exactly this trusted expression says nothing about
        whether the ATTRIBUTE it reads has since been mutated
        (``CapturedArgumentAttributeMutationTests`` demonstrates this
        live), so a SYNTHETIC function -- never one of the 37 real,
        individually reviewed server.py sites -- now raises regardless of
        how provably genuine its own ``api = STATE.api`` binding is.
        Exercises BOTH the Call-branch (``api.get_item``) and the
        Subscript/Dict-literal branch (``{"team": ..., "venue": ...}
        [kind]``) in the SAME function -- both need their OWN waiver now,
        proving the retirement is total, not selective by branch."""
        src = _module('''
            def do_GET(self):
                path = self.path.split("?", 1)[0]
                api = STATE.api
                m = re.match(r"^/api/items/([^/]+)$", path)
                if m:
                    gid = m.group(1)
                    return self._send_api(api.get_item(gid))
                mv = re.match(r"^/api/setup/([^/]+)/delete$", path)
                if mv:
                    kind = mv.group(1)
                    return self._send_api({"team": api.delete_team, "venue": api.delete_venue}[kind])
        ''')
        tree = ast.parse(src)
        handler = next(n for n in tree.body
                       if isinstance(n, ast.ClassDef) and n.name == "Handler")
        fn = next(n for n in handler.body
                 if isinstance(n, ast.FunctionDef) and n.name == "do_GET")
        parents = route_extract_module._build_parent_map(fn)
        self.assertTrue(
            route_extract_module._has_dominating_trusted_binding(
                "api", fn, parents),
            "the provenance-helper itself must still recognise a "
            "genuine, dominating api = STATE.api binding")

        with self.assertRaises(ExtractionError):
            extract_routes(src)

        # Waiving only ONE of the two branches still leaves the OTHER
        # raising -- proving neither branch's own waiver does the other's
        # job. Each iteration installs and REMOVES its own waiver before
        # the next one runs (a bare ``self.addCleanup`` inside the loop
        # would not fire until the whole test method ends, leaving BOTH
        # waivers simultaneously active by the second iteration).
        for text in ("api.get_item(gid)",
                     "{'team': api.delete_team, 'venue': api.delete_venue}"
                     "[kind]"):
            with self.subTest(waived=text):
                cleanups = []

                def _register(func, *args, **kwargs):
                    cleanups.append((func, args, kwargs))

                try:
                    waived_src = _waive_matching_node(
                        _register, '''
                        def do_GET(self):
                            path = self.path.split("?", 1)[0]
                            api = STATE.api
                            m = re.match(r"^/api/items/([^/]+)$", path)
                            if m:
                                gid = m.group(1)
                                return self._send_api(api.get_item(gid))
                            mv = re.match(r"^/api/setup/([^/]+)/delete$", path)
                            if mv:
                                kind = mv.group(1)
                                return self._send_api({"team": api.delete_team, "venue": api.delete_venue}[kind])
                    ''', text)
                    with self.assertRaises(ExtractionError):
                        extract_routes(waived_src)
                finally:
                    for func, args, kwargs in cleanups:
                        func(*args, **kwargs)

    # -- round 11, finding A: the trusted RHS TEXT ``STATE.api`` matching   -
    # -- is not the end of the proof -- ``STATE``, the free variable        -
    # -- EMBEDDED inside that text, must also be provably unshadowed, the   -
    # -- SAME "no rebinding site, no parameter of that name" bar round 10   -
    # -- already holds ``api`` itself to, applied one level up the          -
    # -- expression. Mirrors round 10's own parameter-shadow and local-     -
    # -- reassignment repros exactly, just shadowing ``STATE`` instead of   -
    # -- ``api`` -------------------------------------------------------------

    def test_state_parameter_shadow_raises(self):
        """``STATE`` is never ASSIGNED anywhere in this function -- its
        only binding is the function's own PARAMETER, exactly the
        "parameter shadowing" shape round 10 already closed for ``api``
        itself, one level up: the RHS text ``STATE.api`` still matches
        ``_TRUSTED_BINDING_SOURCES["api"]`` exactly, but ``STATE`` is
        fully attacker-controlled here, so ``api`` is not really the
        reviewed facade's own attribute at all."""
        self._raises('''
            def do_GET(self, STATE=evil_state):
                path = self.path
                api = STATE.api
                m = re.match(r"^/api/([^/]+)$", path)
                if m:
                    action = m.group(1)
                    return api.invoke({"hidden": api.hidden, "other": api.other}[action], self)
        ''', "unlisted call")

    def test_state_parameter_shadow_escape_answers_over_real_http(self):
        """The SAME source text as the static test above, driven live:
        ``BaseHTTPRequestHandler`` calls ``self.do_GET()`` with NO
        arguments, so the DEFAULT value is what a real request actually
        reaches -- the same live-testability round 10's own parameter-
        shadow repro relied on, one level up the expression."""
        body = '''
            def do_GET(self, STATE=evil_state):
                path = self.path
                api = STATE.api
                m = re.match(r"^/api/([^/]+)$", path)
                if m:
                    action = m.group(1)
                    return api.invoke({"hidden": api.hidden, "other": api.other}[action], self)
        '''
        extra_globals = {
            "re": re, "evil_state": types.SimpleNamespace(api=_EvilApiFacade()),
        }
        status, text = _real_http_probe_with_globals(
            body, extra_globals, "GET", "/api/hidden")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(text), {"n": 1})
        status2, text2 = _real_http_probe_with_globals(
            body, extra_globals, "GET", "/api/other")
        self.assertEqual(status2, 404)
        self.assertEqual(json.loads(text2), {"error": "not_found"})

    def test_state_local_reassignment_raises(self):
        """A plain preceding local ``STATE = evil_state`` -- never a
        parameter, never anywhere near the ``api = STATE.api`` line's own
        single-assignment shape -- shadows the module-level singleton
        before ``api = STATE.api`` ever reads it. ``api`` itself is still
        bound EXACTLY once, at a dominating, top-level, never-rebound
        assignment whose RHS text matches the trusted source exactly --
        round 10's OWN check alone would accept this; only round 11's
        free-root check catches that ``STATE`` does not resolve to the
        reviewed singleton here."""
        self._raises('''
            def do_GET(self):
                STATE = evil_state
                path = self.path
                api = STATE.api
                m = re.match(r"^/api/([^/]+)$", path)
                if m:
                    action = m.group(1)
                    return api.invoke({"hidden": api.hidden, "other": api.other}[action], self)
        ''', "unlisted call")

    def test_state_local_reassignment_escape_answers_over_real_http(self):
        """The SAME source text as the static test above, driven live --
        the ONLY ``STATE`` assignment in this function is the evil one, so
        a real request reaches it unconditionally."""
        body = '''
            def do_GET(self):
                STATE = evil_state
                path = self.path
                api = STATE.api
                m = re.match(r"^/api/([^/]+)$", path)
                if m:
                    action = m.group(1)
                    return api.invoke({"hidden": api.hidden, "other": api.other}[action], self)
        '''
        extra_globals = {
            "re": re, "evil_state": types.SimpleNamespace(api=_EvilApiFacade()),
        }
        status, text = _real_http_probe_with_globals(
            body, extra_globals, "GET", "/api/hidden")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(text), {"n": 1})
        status2, text2 = _real_http_probe_with_globals(
            body, extra_globals, "GET", "/api/other")
        self.assertEqual(status2, 404)
        self.assertEqual(json.loads(text2), {"error": "not_found"})

    def test_disabling_the_free_root_check_restores_the_state_shadowing_escape(self):
        """MUTATION, at the provenance-helper level (#202 repair round 13,
        finding 1 retired the STRUCTURAL exemption this proof used to be
        observable through end-to-end via ``extract_routes`` -- see
        ``_TRUSTED_BINDING_SOURCES``'s own module comment,
        route_extract.py -- so this mutation's effect is now checked
        directly against :func:`_has_dominating_trusted_binding`, the
        SAME function ``extract_routes``' own retired exemption used to
        consult): ``_trusted_source_free_roots`` replaced with a stub
        that always returns an EMPTY set -- i.e. "this trusted expression
        has no free variables to check" -- while leaving round 10's own
        "exactly one dominating, text-matching assignment" check for
        ``api`` itself completely intact. This exactly reproduces round
        10's OWN (pre-round-11) behaviour for ``STATE``: proving the
        free-root check (not merely round 10's pre-existing ``api``-only
        check, held fixed here) is what independently makes
        ``_has_dominating_trusted_binding`` correctly answer False for
        the parameter-shadow repro above -- under this mutation it
        WRONGLY answers True instead."""
        original = route_extract_module._trusted_source_free_roots
        route_extract_module._trusted_source_free_roots = lambda expr: frozenset()
        self.addCleanup(setattr, route_extract_module,
                        "_trusted_source_free_roots", original)

        src = _module('''
            def do_GET(self, STATE=evil_state):
                path = self.path
                api = STATE.api
                m = re.match(r"^/api/([^/]+)$", path)
                if m:
                    action = m.group(1)
                    return api.invoke({"hidden": api.hidden, "other": api.other}[action], self)
        ''')
        tree = ast.parse(src)
        handler = next(n for n in tree.body
                       if isinstance(n, ast.ClassDef) and n.name == "Handler")
        fn = next(n for n in handler.body
                 if isinstance(n, ast.FunctionDef) and n.name == "do_GET")
        parents = route_extract_module._build_parent_map(fn)
        self.assertTrue(
            route_extract_module._has_dominating_trusted_binding(
                "api", fn, parents),
            "mutation expected to WRONGLY prove this shadowed binding "
            "trusted")

    # -- round 13, finding 2: the SAME "is STATE shadowed here" question    -
    # -- round 11's finding A already closed for a parameter-default and a  -
    # -- local reassignment, reopened through a THIRD binding spelling      -
    # -- neither round 10 nor round 11 enumerated -- a match/case CAPTURE   -
    # -- pattern. Round 12 documented this without fixing it, reasoning     -
    # -- from a synthetic repro that placed the capture AFTER the trusted   -
    # -- ``api = STATE.api`` read -- an ordering that cannot actually       -
    # -- execute (Python's own per-function scoping makes a name bound by   -
    # -- a capture anywhere in the function LOCAL for the WHOLE function,   -
    # -- so reading it before the capture runs raises ``UnboundLocalError``,-
    # -- never reaching the real global ``STATE``). These fixtures use the  -
    # -- CORRECTED ordering -- capture first, trusted read second -- so the -
    # -- live probes below exercise a repro that genuinely runs -----------------

    def test_state_bare_capture_shadow_raises(self):
        """A bare ``case STATE:`` capture pattern (``ast.MatchAs``) ahead of
        ``api = STATE.api`` shadows ``STATE`` for the rest of the function
        exactly as a parameter default or a local reassignment already do
        (round 11's finding A) -- :func:`_name_rebinding_sites` now finds
        this ``ast.MatchAs`` site (round 13 finding 2's fix), so the
        free-root check refuses the ``api`` exemption here too."""
        self._raises('''
            def do_GET(self, x=evil_state):
                path = self.path
                match x:
                    case STATE:
                        pass
                api = STATE.api
                m = re.match(r"^/api/([^/]+)$", path)
                if m:
                    action = m.group(1)
                    return api.invoke({"hidden": api.hidden, "other": api.other}[action], self)
        ''', "unlisted call")

    def test_state_bare_capture_shadow_escape_answers_over_real_http(self):
        """The SAME source text as the static test above, driven live: the
        parameter default (``x=evil_state``) is what a real request with no
        arguments reaches, the match statement UNCONDITIONALLY captures it
        into ``STATE`` (a bare capture pattern has no test to fail), and
        that capture runs BEFORE ``api = STATE.api`` reads it -- so this
        genuinely executes, unlike round 12's own repro. ``/api/hidden``
        answers 200, ``/api/other`` answers 404, the SAME "static stays
        silent [before this round's fix], live diverges 200/404" proof
        every prior finding in this class required."""
        body = '''
            def do_GET(self, x=evil_state):
                path = self.path
                match x:
                    case STATE:
                        pass
                api = STATE.api
                m = re.match(r"^/api/([^/]+)$", path)
                if m:
                    action = m.group(1)
                    return api.invoke({"hidden": api.hidden, "other": api.other}[action], self)
        '''
        extra_globals = {
            "re": re, "evil_state": types.SimpleNamespace(api=_EvilApiFacade()),
        }
        status, text = _real_http_probe_with_globals(
            body, extra_globals, "GET", "/api/hidden")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(text), {"n": 1})
        status2, text2 = _real_http_probe_with_globals(
            body, extra_globals, "GET", "/api/other")
        self.assertEqual(status2, 404)
        self.assertEqual(json.loads(text2), {"error": "not_found"})

    def test_state_mapping_rest_capture_shadow_raises(self):
        """A mapping-rest ``case {**STATE}:`` capture pattern (``ast.
        MatchMapping``) is the SAME kind of shadow as the bare-capture case
        above, just a different binding spelling -- :func:`_name_rebinding_
        sites` now finds this ``ast.MatchMapping`` site too."""
        self._raises('''
            def do_GET(self, x=evil_state):
                path = self.path
                match x:
                    case {**STATE}:
                        pass
                api = STATE.api
                m = re.match(r"^/api/([^/]+)$", path)
                if m:
                    action = m.group(1)
                    return api.invoke({"hidden": api.hidden, "other": api.other}[action], self)
        ''', "unlisted call")

    def test_state_mapping_rest_capture_shadow_escape_answers_over_real_http(self):
        """The SAME source text as the static test above, driven live --
        with one HONEST difference from every other live repro in this
        class, stated plainly rather than glossed over: a mapping-rest
        capture ALWAYS binds a freshly-constructed, plain ``dict`` (CPython
        never preserves the subject's own type, or attaches any of its
        attributes, to the ``**rest`` capture -- confirmed directly against
        the interpreter, not assumed), so ``STATE.api`` cannot resolve to
        ``evil_state``'s facade the way the bare-capture case's ``STATE``
        does -- it raises ``AttributeError: 'dict' object has no attribute
        'api'`` INSIDE the handler instead. That is still a live divergence
        from what the (pre-fix) static analysis claims: extraction used to
        treat this function as safe to exempt, yet no real request against
        it can even REACH the reviewed facade at all -- it never gets past
        the shadowed read. ``http.server``'s threading dispatch has no
        handler for an exception escaping ``do_GET`` (see
        ``BaseServer.handle_error``): it logs a traceback server-side and
        the connection simply closes with no HTTP response, which
        ``urllib`` surfaces client-side as ``ConnectionError`` (concretely
        ``http.client.RemoteDisconnected``, itself a ``ConnectionError``
        subclass -- confirmed directly, not assumed) rather than any status
        code. The shape differs from the 200/404 split elsewhere in this
        class only in WHICH observable signals the shadow -- the shadow
        itself, and the fact that (pre-fix) static analysis stayed silent
        about it, are identical."""
        body = '''
            def do_GET(self, x=evil_state):
                path = self.path
                match x:
                    case {**STATE}:
                        pass
                api = STATE.api
                m = re.match(r"^/api/([^/]+)$", path)
                if m:
                    action = m.group(1)
                    return api.invoke({"hidden": api.hidden, "other": api.other}[action], self)
        '''
        extra_globals = {
            "re": re, "evil_state": types.SimpleNamespace(api=_EvilApiFacade()),
        }
        with self.assertRaises(ConnectionError):
            _real_http_probe_with_globals(body, extra_globals, "GET", "/api/hidden")

    # -- round 11, finding B -- CLOSED by round 13, finding 1 (was a KNOWN -
    # -- LIMITATION, NOT fixed -- see route_extract.py's module docstring):-
    # -- a GENUINELY, provenance-proven ``api = STATE.api`` (finding A's   -
    # -- own fix, directly above) USED TO trust the reviewed facade's      -
    # -- WHOLE surface, not a per-method allowlist -- a hypothetical       -
    # -- FUTURE ``api.invoke``-shaped method could have hidden routing     -
    # -- behaviour behind a name this module cannot vet without running    -
    # -- the program. Retiring the STRUCTURAL exemption entirely (finding  -
    # -- 1) closes this as a direct consequence, not a separate fix: NO    -
    # -- method call on ANY name is trusted by provenance any more, proven -
    # -- or not -- only an individually reviewed, exact-text-fingerprinted -
    # -- _AUDIT_WAIVERS entry exempts a specific call site, and            -
    # -- ``api.invoke(...)`` is not one of the 37 real ones this round     -
    # -- names --------------------------------------------------------------

    def test_genuine_state_provenance_no_longer_exempts_arbitrary_api_methods(self):
        """A dominating, unshadowed ``api = STATE.api`` -- passing BOTH
        round 10's check and round 11's finding-A free-root check -- USED
        TO exempt ANY method called on ``api``, including a higher-order
        ``api.invoke(callback, arg)`` shape indistinguishable, by this
        module's own static reading, from the genuinely inert
        ``api.get_item(gid)`` calls the real server.py uses today (round
        11's own finding B, previously left deliberately UNFIXED -- see
        the module docstring's history). #202 repair round 13, finding 1
        closes it: this SYNTHETIC ``do_GET`` is not one of the 37 real,
        individually reviewed server.py sites, so it now raises exactly
        like any other unlisted call, REGARDLESS of how provably genuine
        its own ``api = STATE.api`` binding is. The Callable-annotation
        contract test
        (``test_the_real_api_facade_exposes_no_callable_shaped_signature``,
        just below) is KEPT regardless -- it is an independent monitoring
        backstop on the real ``ApiService``'s own architecture, not
        merely a tripwire for this now-closed gap."""
        self._raises('''
            def do_GET(self):
                path = self.path
                api = STATE.api
                m = re.match(r"^/api/([^/]+)$", path)
                if m:
                    action = m.group(1)
                    return api.invoke(api.get_item, action)
        ''', "unlisted call")

    # -- "safe-name object/higher-order-method" cases -- HISTORICAL        -
    # -- context (round 11 finding B, closed by round 13 finding 1's       -
    # -- retirement of the whole structural exemption; see                 -
    # -- route_extract.py's _TRUSTED_BINDING_SOURCES, own module comment): -
    # -- once a name's provenance was proven, the OLD exemption trusted    -
    # -- the REVIEWED facade's WHOLE surface (CLAUDE.md's own layering     -
    # -- guarantee), not a per-method allowlist: the real facade has 174+  -
    # -- distinct methods (direct AST count against server.py today, see  -
    # -- the test below), and hand-enumerating them would have been        -
    # -- exactly the "curated shape-by-shape closure [that] is not         -
    # -- converging" this module's own round-9 docstring already rejected  -
    # -- as a strategy, at 174-times the scale. The contract test below   -
    # -- is KEPT regardless of that closure -- it is an INDEPENDENT        -
    # -- monitoring backstop on the real ApiService's own architecture,    -
    # -- continuously verified, not merely a tripwire for a now-closed gap-

    def test_the_real_api_facade_exposes_no_callable_shaped_signature(self):
        """#202 repair round 10 (external review): "a future callback-
        taking method added under that name can again hide executable
        route/policy/Allow behavior" -- the reviewer's own stated residual
        concern once provenance ALONE is fixed (round 11 finding B,
        CLOSED by round 13 finding 1's retirement of the whole structural
        exemption -- see route_extract.py's ``_TRUSTED_BINDING_SOURCES``,
        own module comment). This module could not prove a HYPOTHETICAL
        future ``api.invoke``-shaped method was safe any more than it
        could prove what an arbitrary unlisted function does -- there was
        no way to verify a method's BODY never forwards or invokes
        whatever it is handed by reading server.py's own source, and this
        module never attempted to for ANY call, ``api.`` included. Kept
        as a standing contract test below regardless of finding B's own
        closure: it is a genuinely independent architectural monitoring
        backstop on the real facade, not merely a tripwire for that one
        gap.

        What IS checked here, empirically, against the REAL facade: no
        PUBLIC method on ``ApiService`` or any sub-facade it constructs
        (``store``/``roster``/``setup``/``delivery``/``delivery_loop``/
        ``accounts``/``factory_reset``/``guardians``/``context`` today --
        discovered from a real instance rather than hand-listed a second
        time here, so this stays accurate as new ones are added) declares
        a ``Callable``-typed parameter or return value -- the shape a
        genuine ``api.invoke(fn, target)`` would need. A name-based
        heuristic (flagging methods merely NAMED ``invoke``/``dispatch``/
        ``execute``/...) was tried first and rejected: it flagged
        ``FactoryResetService.execute`` (a plain, ordinary domain
        operation -- wipe the database given credentials -- that merely
        happens to be a common English verb), a real false positive
        proving name-matching is the wrong signal here, exactly the
        "spelling is not the same as provenance" lesson this whole round
        is about, applied one level deeper.

        This is a CONTRACT test pinning today's architecture, not a
        static-analysis code path -- it fails loudly the moment a real
        callback-typed parameter is added anywhere on the facade,
        applied at the method-signature level, without the 174-entry,
        ever-drifting list an exhaustive per-method allowlist would
        otherwise force this module to hand-maintain -- and, since round
        13 finding 1, without any allowlist at all to extend: a new
        captured-only call site now earns trust by its OWN individually
        reviewed ``_AUDIT_WAIVERS`` entry, the same weight of review a
        new allowlist ROOT name used to require. KNOWN LIMIT:
        an UNANNOTATED parameter that accepts and invokes a callable
        without ever being typed ``Callable`` would not be caught by
        this heuristic either -- the same honestly-documented boundary
        this module already draws elsewhere (see e.g.
        ``_binding_value_and_targets``'s own KNOWN LIMITATIONS note)
        rather than a claim of exhaustive proof."""
        from hockey_scheduler.api.service import ApiService
        api = ApiService()
        facades = {ApiService: api}
        for _, value in vars(api).items():
            cls = type(value)
            if cls.__module__.startswith("hockey_scheduler"):
                facades.setdefault(cls, value)
        self.assertGreaterEqual(
            len(facades), 5,
            "sanity: this should discover several real sub-facade classes, "
            "not merely ApiService itself -- a collapse to 1 would mean "
            "the discovery loop above stopped finding sub-facades and this "
            "test would be silently checking far less than it claims to")
        offenders = []
        for cls in facades:
            for name, member in vars(cls).items():
                if name.startswith("_") or not inspect.isfunction(member):
                    continue
                try:
                    sig = inspect.signature(member)
                except (TypeError, ValueError):
                    continue
                for pname, param in sig.parameters.items():
                    if "callable" in str(param.annotation).lower():
                        offenders.append(f"{cls.__name__}.{name}({pname})")
                if "callable" in str(sig.return_annotation).lower():
                    offenders.append(f"{cls.__name__}.{name} return")
        self.assertEqual(offenders, [])

        # Count the REAL, distinct api.<attr> attribute names actually
        # referenced in server.py's Handler class -- the "174+" figure
        # this test's own docstring cites, kept HONEST rather than
        # asserted from memory.
        server_src = (BACKEND / "hockey_scheduler" / "web" / "server.py").read_text()
        names = {
            node.attr for node in ast.walk(ast.parse(server_src))
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
               and node.value.id == "api"
        }
        self.assertGreaterEqual(len(names), 150)

    # -- load-bearing mutation: the provenance gate is independently       -
    # -- necessary, not merely redundant with the pre-existing allowlist ---

    # #202 repair round 13, finding 1 (external review): the mutation this
    # test used to run -- stubbing ``_captured_arg_trusted_roots`` back to
    # SPELLING-only trust, reproducing round 9's own pre-round-10 behaviour
    # -- has no equivalent any more: that function, and the "spelling vs
    # proven" distinction it existed to enforce, are both retired along
    # with the whole structural exemption (see route_extract.py's
    # ``_TRUSTED_BINDING_SOURCES``, own module comment).
    # ``_has_dominating_trusted_binding`` (the one piece of this machinery
    # still standing) never had a "spelling-only" mode to fall back to in
    # the first place -- it is REMOVED rather than reworked, since there is
    # no longer a mutation of it that would reproduce round 9's own
    # behaviour; the shadowed-name repro this test used to exercise is
    # covered end-to-end by
    # ``test_shadowed_api_dict_literal_higher_order_invoke_raises`` above,
    # which raises today for the SAME "not one of the 37 real, individually
    # reviewed sites" reason every other captured-only call in a synthetic
    # fixture now does.


# --------------------------------------------------------------------------- #
# #202 repair round 13, finding 1 (external review): rounds 9-11's provenance  #
# chain proved a captured NAME (`api`) is bound, once, dominating, unrebound,  #
# to the literal text `STATE.api` -- but never asked whether `STATE.api`'s     #
# own VALUE stays whatever it was reviewed to be, either for the REST of this  #
# same function call, or -- since `STATE` is a MODULE-LEVEL singleton, never   #
# recreated between requests -- for every LATER request too. DEMONSTRATED by   #
# the reviewer, live over real HTTP: `STATE.api = evil_api; api = STATE.api`   #
# followed by the captured-action `api.invoke({...}[action], self)` was        #
# accepted (at head 169a329) as only `GET /api/{}`; live `/api/hidden`         #
# returned 200 while `/api/other` returned 404, with static extraction         #
# staying silent throughout. Proving the entire trusted access path immutable  #
# across a whole function (and everything it calls) is not something static    #
# analysis can soundly do for a mutable Python attribute -- there is no        #
# `const`/`final` -- so route_extract.py does not try any more (see            #
# `_TRUSTED_BINDING_SOURCES`'s own module comment): every case below, which    #
# WOULD have been wrongly exempted by the retired structural gate, now raises  #
# "unlisted call" exactly like any other unmodelled captured-only call in a    #
# SYNTHETIC function that is not one of the 37 real, individually reviewed     #
# server.py sites.                                                            #
#                                                                              #
# Every form below mutates the SAME attribute a different way -- direct       #
# assignment, a `del` STATEMENT, the `setattr`/`delattr` BUILTINS, and an      #
# ALIAS (`s = STATE; s.api = ...` -- never `STATE` itself in Store/Del         #
# context, so `_name_rebinding_sites("STATE", fn)` correctly finds nothing to  #
# rebind: the name `STATE` is never reassigned, only the object it already     #
# points to is mutated) -- each demonstrated BOTH before and after the         #
# trusted `api = STATE.api` read. "Before" is the reviewer's own repro shape:  #
# the SAME request's own read picks up the already-corrupted value. "After"    #
# is the SAME escape reached a different way: THIS request's own `api` local   #
# is unaffected (Python already bound it to the pre-mutation object by the     #
# time the mutation statement runs), but `STATE` persists across requests, so  #
# the very NEXT request's own `api = STATE.api` sees the corruption --         #
# demonstrated with a real two-request probe against the SAME running server,  #
# not merely asserted. `delattr`/`del` need one more piece to answer live      #
# rather than merely raising `AttributeError` after the deletion: a small      #
# `__getattr__` fallback on the STATE stand-in (a realistic "lazily            #
# recomputed attribute" idiom, not a route_extract-specific contrivance) that   #
# returns the evil facade once the instance's own `.api` is gone.              #
# --------------------------------------------------------------------------- #
class CapturedArgumentAttributeMutationTests(unittest.TestCase):
    def _raises(self, body, *substrings):
        with self.assertRaises(ExtractionError) as caught:
            extract_routes(_module(body))
        msg = str(caught.exception)
        for s in substrings:
            self.assertIn(s, msg)

    # -- BEFORE the trusted read: the SAME request's own read picks up the -
    # -- already-mutated value -------------------------------------------------

    def test_direct_attribute_assignment_before_the_read_raises(self):
        """The reviewer's own repro, verbatim: `STATE.api = evil_api`
        immediately before the trusted `api = STATE.api` read. `STATE`
        the NAME is never rebound (this is an ATTRIBUTE assignment target,
        `ast.Attribute` in Store context -- `STATE` itself appears only in
        LOAD context, to look the attribute up on), so
        `_name_rebinding_sites("STATE", fn)` finds nothing here at all --
        this is not a gap in that function (its own contract is "does
        this rebind the NAME", never "does this mutate the OBJECT the
        name already points to"), it is why the retired exemption needed
        a different proof entirely."""
        self._raises('''
            def do_GET(self):
                path = self.path
                STATE.api = evil_api
                api = STATE.api
                m = re.match(r"^/api/([^/]+)$", path)
                if m:
                    action = m.group(1)
                    return api.invoke({"hidden": api.hidden, "other": api.other}[action], self)
        ''', "unlisted call")

    def test_direct_attribute_assignment_before_the_read_escape_answers_over_real_http(self):
        """The SAME source text as the static test above, driven live:
        `/api/hidden` answers 200, `/api/other` answers 404 -- the SAME
        "static stays silent [before round 13's fix], live diverges
        200/404" proof every prior finding in this module required."""
        body = '''
            def do_GET(self):
                path = self.path
                STATE.api = evil_api
                api = STATE.api
                m = re.match(r"^/api/([^/]+)$", path)
                if m:
                    action = m.group(1)
                    return api.invoke({"hidden": api.hidden, "other": api.other}[action], self)
        '''
        extra_globals = {
            "re": re, "evil_api": _EvilApiFacade(),
            "STATE": types.SimpleNamespace(api=object()),
        }
        status, text = _real_http_probe_with_globals(
            body, extra_globals, "GET", "/api/hidden")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(text), {"n": 1})
        status2, text2 = _real_http_probe_with_globals(
            body, extra_globals, "GET", "/api/other")
        self.assertEqual(status2, 404)
        self.assertEqual(json.loads(text2), {"error": "not_found"})

    def test_setattr_before_the_read_raises(self):
        """The `setattr` BUILTIN sibling of direct assignment -- a plain
        ``ast.Call`` (`setattr(STATE, "api", evil_api)`), never an
        ``ast.Attribute`` in Store context at all, so it is not merely
        unseen by ``_name_rebinding_sites`` for the same reason as direct
        assignment -- it is not even the SAME shape that function's
        docstring discusses; it is an ordinary, opaque call this module's
        completeness scan never had reason to treat as a binding of
        anything."""
        self._raises('''
            def do_GET(self):
                path = self.path
                setattr(STATE, "api", evil_api)
                api = STATE.api
                m = re.match(r"^/api/([^/]+)$", path)
                if m:
                    action = m.group(1)
                    return api.invoke({"hidden": api.hidden, "other": api.other}[action], self)
        ''', "unlisted call")

    def test_setattr_before_the_read_escape_answers_over_real_http(self):
        body = '''
            def do_GET(self):
                path = self.path
                setattr(STATE, "api", evil_api)
                api = STATE.api
                m = re.match(r"^/api/([^/]+)$", path)
                if m:
                    action = m.group(1)
                    return api.invoke({"hidden": api.hidden, "other": api.other}[action], self)
        '''
        extra_globals = {
            "re": re, "evil_api": _EvilApiFacade(),
            "STATE": types.SimpleNamespace(api=object()),
        }
        status, text = _real_http_probe_with_globals(
            body, extra_globals, "GET", "/api/hidden")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(text), {"n": 1})
        status2, text2 = _real_http_probe_with_globals(
            body, extra_globals, "GET", "/api/other")
        self.assertEqual(status2, 404)
        self.assertEqual(json.loads(text2), {"error": "not_found"})

    def test_alias_based_mutation_before_the_read_raises(self):
        """The ALIAS sibling: `s = STATE` binds a SECOND name to the SAME
        object, then `s.api = evil_api` mutates through IT -- `STATE`
        itself is never assigned, deleted, or even mentioned again after
        the alias is taken, so no rule keyed on the SPELLING `STATE`
        (rebinding or otherwise) could ever see this coming; the object
        `STATE` still points to is exactly the one `s` just mutated,
        Python object identity, not aliasing analysis."""
        self._raises('''
            def do_GET(self):
                path = self.path
                s = STATE
                s.api = evil_api
                api = STATE.api
                m = re.match(r"^/api/([^/]+)$", path)
                if m:
                    action = m.group(1)
                    return api.invoke({"hidden": api.hidden, "other": api.other}[action], self)
        ''', "unlisted call")

    def test_alias_based_mutation_before_the_read_escape_answers_over_real_http(self):
        body = '''
            def do_GET(self):
                path = self.path
                s = STATE
                s.api = evil_api
                api = STATE.api
                m = re.match(r"^/api/([^/]+)$", path)
                if m:
                    action = m.group(1)
                    return api.invoke({"hidden": api.hidden, "other": api.other}[action], self)
        '''
        extra_globals = {
            "re": re, "evil_api": _EvilApiFacade(),
            "STATE": types.SimpleNamespace(api=object()),
        }
        status, text = _real_http_probe_with_globals(
            body, extra_globals, "GET", "/api/hidden")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(text), {"n": 1})
        status2, text2 = _real_http_probe_with_globals(
            body, extra_globals, "GET", "/api/other")
        self.assertEqual(status2, 404)
        self.assertEqual(json.loads(text2), {"error": "not_found"})

    # -- delattr/del need a REALISTIC reason for STATE.api to still        -
    # -- resolve to something once the instance's own attribute is gone --  -
    # -- a lazily-recomputed-attribute __getattr__ fallback, the ordinary  -
    # -- Python idiom this shape is genuinely reachable through, not a     -
    # -- route_extract-specific contrivance --------------------------------

    def test_delattr_before_the_read_raises(self):
        """The `delattr` BUILTIN: removes the instance's own `.api`
        attribute; `STATE.__class__.__getattr__` (see `_FallbackState`,
        this file) then supplies the evil facade the NEXT time `.api` is
        looked up, the ordinary Python fallback-attribute protocol, not a
        route_extract-specific construction."""
        self._raises('''
            def do_GET(self):
                path = self.path
                delattr(STATE, "api")
                api = STATE.api
                m = re.match(r"^/api/([^/]+)$", path)
                if m:
                    action = m.group(1)
                    return api.invoke({"hidden": api.hidden, "other": api.other}[action], self)
        ''', "unlisted call")

    def test_delattr_before_the_read_escape_answers_over_real_http(self):
        """A FRESH ``_FallbackState`` per probe (``delattr`` is not
        idempotent -- a second ``delattr`` on an already-deleted
        attribute raises ``AttributeError`` on its OWN, unrelated to this
        module -- so hidden/other each need their own instance, exactly
        as the real ``STATE`` singleton would only ever be deleted
        once)."""
        body = '''
            def do_GET(self):
                path = self.path
                delattr(STATE, "api")
                api = STATE.api
                m = re.match(r"^/api/([^/]+)$", path)
                if m:
                    action = m.group(1)
                    return api.invoke({"hidden": api.hidden, "other": api.other}[action], self)
        '''
        status, text = _real_http_probe_with_globals(
            body, {"re": re, "STATE": _FallbackState()}, "GET", "/api/hidden")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(text), {"n": 1})
        status2, text2 = _real_http_probe_with_globals(
            body, {"re": re, "STATE": _FallbackState()}, "GET", "/api/other")
        self.assertEqual(status2, 404)
        self.assertEqual(json.loads(text2), {"error": "not_found"})

    def test_del_statement_before_the_read_raises(self):
        """The `del` STATEMENT sibling of `delattr` -- `del STATE.api` is
        an `ast.Delete` whose one target is an `ast.Attribute` in Del
        context, a THIRD shape distinct from both a Store-context
        attribute assignment and a plain `ast.Call`, and one
        `_name_rebinding_sites` was never asked to recognise for
        anything OTHER than a bare `ast.Name` (its own docstring's "an
        ordinary assignment target... a `del`" bullet is explicitly about
        `del name`, never `del obj.attr`)."""
        self._raises('''
            def do_GET(self):
                path = self.path
                del STATE.api
                api = STATE.api
                m = re.match(r"^/api/([^/]+)$", path)
                if m:
                    action = m.group(1)
                    return api.invoke({"hidden": api.hidden, "other": api.other}[action], self)
        ''', "unlisted call")

    def test_del_statement_before_the_read_escape_answers_over_real_http(self):
        """As the `delattr` live proof above, a FRESH ``_FallbackState``
        per probe for the same reason."""
        body = '''
            def do_GET(self):
                path = self.path
                del STATE.api
                api = STATE.api
                m = re.match(r"^/api/([^/]+)$", path)
                if m:
                    action = m.group(1)
                    return api.invoke({"hidden": api.hidden, "other": api.other}[action], self)
        '''
        status, text = _real_http_probe_with_globals(
            body, {"re": re, "STATE": _FallbackState()}, "GET", "/api/hidden")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(text), {"n": 1})
        status2, text2 = _real_http_probe_with_globals(
            body, {"re": re, "STATE": _FallbackState()}, "GET", "/api/other")
        self.assertEqual(status2, 404)
        self.assertEqual(json.loads(text2), {"error": "not_found"})

    # -- AFTER the trusted read: THIS request's own `api` local is         -
    # -- unaffected (already bound to the pre-mutation object), but        -
    # -- `STATE` is a persistent singleton, so the NEXT request's own      -
    # -- `api = STATE.api` sees the corruption -- demonstrated with a      -
    # -- real two-request probe against the SAME server, not merely       -
    # -- asserted ------------------------------------------------------------

    def test_direct_attribute_assignment_after_the_read_raises(self):
        """Static analysis has no dominance/control-flow model
        distinguishing "mutated before this read" from "mutated after
        it" for an ATTRIBUTE target -- `_name_rebinding_sites` never sees
        either, in EITHER position, since neither is a rebinding of the
        NAME `STATE` at all -- so this raises for the identical, simpler
        reason the "before" case does, not a new one; the interesting
        part of "after" is the LIVE proof below, not the static one."""
        self._raises('''
            def do_GET(self):
                path = self.path
                api = STATE.api
                STATE.api = evil_api
                m = re.match(r"^/api/([^/]+)$", path)
                if m:
                    action = m.group(1)
                    return api.invoke({"hidden": api.hidden, "other": api.other}[action], self)
        ''', "unlisted call")

    def test_direct_attribute_assignment_after_the_read_escape_answers_over_real_http(self):
        """The SAME source text as the static test above, driven live
        across TWO requests against the SAME running server (sharing the
        SAME ``STATE`` singleton object -- ``extra_globals`` holds actual
        object references, not copies): a PRIMING request whose own
        response is deliberately not asserted on (this fixture's
        ``api = STATE.api`` reads the STILL-GENUINE facade on this first
        call, which has no ``invoke``/``hidden``/``other`` surface, so
        this request predictably fails part-way through -- its only job
        is to let the mutation statement run, which it does regardless,
        since that statement executes unconditionally, before the
        dispatch branch, on every path through the function); then TWO
        further requests, against the NOW-corrupted singleton, answering
        the SAME 200/404 split as the "before" case -- proving a mutation
        textually AFTER the trusted read still poisons the shared
        singleton for every request that follows, not merely a
        hypothetical concern."""
        body = '''
            def do_GET(self):
                path = self.path
                api = STATE.api
                STATE.api = evil_api
                m = re.match(r"^/api/([^/]+)$", path)
                if m:
                    action = m.group(1)
                    return api.invoke({"hidden": api.hidden, "other": api.other}[action], self)
        '''
        extra_globals = {
            "re": re, "evil_api": _EvilApiFacade(),
            "STATE": types.SimpleNamespace(api=object()),
        }
        try:
            _real_http_probe_with_globals(
                body, extra_globals, "GET", "/api/hidden")
        except ConnectionError:
            pass  # priming call -- expected to fail, only its side effect matters

        status, text = _real_http_probe_with_globals(
            body, extra_globals, "GET", "/api/hidden")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(text), {"n": 1})
        status2, text2 = _real_http_probe_with_globals(
            body, extra_globals, "GET", "/api/other")
        self.assertEqual(status2, 404)
        self.assertEqual(json.loads(text2), {"error": "not_found"})


class _FallbackState:
    """A REALISTIC "lazily recomputed attribute" stand-in for the module
    singleton, used ONLY by the ``delattr``/``del`` live probes above: the
    instance starts with a genuine (inert) ``.api``, and ``__getattr__`` --
    the ordinary Python protocol for "the normal lookup found nothing,
    here is a fallback", triggered ONLY once the instance's own attribute
    is truly gone -- supplies the evil facade instead. NOT a
    route_extract-specific contrivance: this is the same shape a
    real caching/lazy-initialisation property could take."""

    def __init__(self):
        self.api = object()

    def __getattr__(self, name):
        if name == "api":
            return _EvilApiFacade()
        raise AttributeError(name)


# --------------------------------------------------------------------------- #
# #202 repair round 12, finding 1 -- CLOSED by round 13, finding 2. Round 11's #
# own finding A bounded itself to "is STATE shadowed here", and closed that    #
# for a parameter-default and a local reassignment; round 12 found (but did    #
# not fix) a THIRD binding spelling that also shadows a name -- a match/case   #
# CAPTURE pattern (a bare `case STATE:`, ast.MatchAs, or a mapping-rest        #
# `case {**STATE}:`, ast.MatchMapping) -- on the strength of "server.py has    #
# no match statement today". Round 13's review rejected that "not exploitable  #
# today" call: round 12's OWN tripwire test placed the capture AFTER the       #
# trusted `api = STATE.api` read, an ordering that cannot execute at all       #
# (Python's per-function scoping makes a captured name LOCAL for the WHOLE     #
# function, so reading it before the capture runs raises UnboundLocalError),   #
# so the "gap is merely latent" conclusion rested on an unrunnable repro, not  #
# on the underlying Python semantics being safe. This class now proves the     #
# FIX (see _name_rebinding_sites's own docstring, route_extract.py) with the   #
# CORRECTED (capture-before-read) ordering -- see CapturedArgumentProvenance-  #
# Tests's own match-capture cases, this file, for the end-to-end static+live   #
# proof; this class covers the _name_rebinding_sites/_has_dominating_trusted_  #
# binding mechanism directly, plus the negative controls and load-bearing      #
# mutations that show the fix -- not merely the fixture -- is what closes it.  #
# --------------------------------------------------------------------------- #
class MatchCaptureBindingRecognitionTests(unittest.TestCase):
    def test_the_real_server_has_no_match_statement_anywhere(self):
        """Informational, not a limitation guard (the gap this originally
        pinned is closed -- see this class's own module comment): parses
        the real server.py FRESH from disk and confirms it still contains
        no ``ast.Match`` node anywhere. Kept as a cheap regression sanity
        check and because ``test_the_real_server_extracts_with_no_new_raises``-
        style tests elsewhere in this module rely on the same "walk the
        real file fresh" discipline -- not because a match statement
        appearing would reopen anything: :func:`_name_rebinding_sites` now
        recognises both of structural pattern matching's capture forms
        the same way it recognises every other binding spelling."""
        server_src = (BACKEND / "hockey_scheduler" / "web" / "server.py").read_text()
        match_nodes = [
            node for node in ast.walk(ast.parse(server_src))
            if isinstance(node, getattr(ast, "Match", ()))
        ]
        self.assertEqual(match_nodes, [])

    # -- the fix itself, at the provenance-helper level -- both binding    -
    # -- orders, since _name_rebinding_sites walks the WHOLE function via  -
    # -- ast.walk and does not reason about statement order at all, so a   -
    # -- capture textually BEFORE or AFTER the trusted read is found       -
    # -- identically (only the BEFORE order is executable over real HTTP;  -
    # -- see CapturedArgumentProvenanceTests for that proof and, mirroring -
    # -- test_rebinding_before_a_valid_facade_assignment_raises's own      -
    # -- documented asymmetry, the AFTER order stays static-only here) -----

    def _sites_and_trust(self, body: str):
        src = _module(body)
        tree = ast.parse(src)
        handler = next(n for n in tree.body
                       if isinstance(n, ast.ClassDef) and n.name == "Handler")
        fn = next(n for n in handler.body
                 if isinstance(n, ast.FunctionDef) and n.name == "do_GET")
        sites = route_extract_module._name_rebinding_sites("STATE", fn)
        parents = route_extract_module._build_parent_map(fn)
        trusted = route_extract_module._has_dominating_trusted_binding(
            "api", fn, parents)
        return sites, trusted

    def test_name_rebinding_sites_recognizes_a_bare_capture_before_the_read(self):
        """The CORRECTED ordering (capture, then the trusted read) -- the
        one a real request can actually reach; see
        ``CapturedArgumentProvenanceTests.test_state_bare_capture_shadow_escape_answers_over_real_http``
        for the live proof of this exact source."""
        sites, trusted = self._sites_and_trust('''
            def do_GET(self, x=1):
                path = self.path
                match x:
                    case STATE:
                        pass
                api = STATE.api
                m = re.match(r"^/api/([^/]+)$", path)
                if m:
                    action = m.group(1)
                    return api.invoke({"hidden": api.hidden}[action], self)
        ''')
        self.assertEqual(len(sites), 1)
        self.assertIsInstance(sites[0], getattr(ast, "MatchAs", ()))
        self.assertFalse(trusted)

    def test_name_rebinding_sites_recognizes_a_bare_capture_after_the_read(self):
        """The MIRROR ordering (the trusted read, then the capture) --
        round 12's own (unrunnable) fixture shape. Static analysis has no
        control-flow/dominance model to distinguish "read, then shadowed"
        from "shadowed, then read" any more than
        ``test_rebinding_before_a_valid_facade_assignment_raises`` does for
        a plain reassignment of ``api`` itself -- both orders must raise,
        not merely the executable one, since a text-only walk cannot tell
        them apart."""
        sites, trusted = self._sites_and_trust('''
            def do_GET(self, x=1):
                path = self.path
                api = STATE.api
                match x:
                    case STATE:
                        pass
                m = re.match(r"^/api/([^/]+)$", path)
                if m:
                    action = m.group(1)
                    return api.invoke({"hidden": api.hidden}[action], self)
        ''')
        self.assertEqual(len(sites), 1)
        self.assertIsInstance(sites[0], getattr(ast, "MatchAs", ()))
        self.assertFalse(trusted)

    def test_name_rebinding_sites_recognizes_a_mapping_rest_before_the_read(self):
        """The mapping-rest capture's own CORRECTED ordering; see
        ``CapturedArgumentProvenanceTests.test_state_mapping_rest_capture_shadow_escape_answers_over_real_http``
        for the live proof of this exact source."""
        sites, trusted = self._sites_and_trust('''
            def do_GET(self, x=1):
                path = self.path
                match x:
                    case {**STATE}:
                        pass
                api = STATE.api
                m = re.match(r"^/api/([^/]+)$", path)
                if m:
                    action = m.group(1)
                    return api.invoke({"hidden": api.hidden}[action], self)
        ''')
        self.assertEqual(len(sites), 1)
        self.assertIsInstance(sites[0], getattr(ast, "MatchMapping", ()))
        self.assertFalse(trusted)

    def test_name_rebinding_sites_recognizes_a_mapping_rest_after_the_read(self):
        """The mapping-rest capture's own MIRROR ordering -- static-only,
        the same asymmetry as the bare-capture "after" case above."""
        sites, trusted = self._sites_and_trust('''
            def do_GET(self, x=1):
                path = self.path
                api = STATE.api
                match x:
                    case {**STATE}:
                        pass
                m = re.match(r"^/api/([^/]+)$", path)
                if m:
                    action = m.group(1)
                    return api.invoke({"hidden": api.hidden}[action], self)
        ''')
        self.assertEqual(len(sites), 1)
        self.assertIsInstance(sites[0], getattr(ast, "MatchMapping", ()))
        self.assertFalse(trusted)

    # -- negative controls: a pattern that binds NOTHING, or binds a       -
    # -- DIFFERENT name, must not be mistaken for a "STATE" rebinding site -
    # -- (a too-broad implementation -- e.g. flagging every MatchAs/       -
    # -- MatchMapping regardless of name/rest -- would pass the tests      -
    # -- above but fail these) ----------------------------------------------

    def test_non_binding_value_class_and_or_patterns_are_not_rebinding_sites(self):
        """None of a VALUE pattern (a dotted name, ``ast.MatchValue``), a
        CLASS pattern with no captures (``ast.MatchClass``), or an OR
        pattern of two non-binding alternatives (``ast.MatchOr``) binds any
        name at all -- ``STATE`` stays whatever it already was outside the
        match statement, so none of these may register as a rebinding
        site. Still exercises the genuine ``api = STATE.api`` exemption
        (the params/locals/attribute-mutation baseline this fix must not
        regress) by asserting the function stays PROVABLY trusted, not
        merely that its own ``STATE`` site list is empty."""
        sites, trusted = self._sites_and_trust('''
            def do_GET(self, x=1):
                path = self.path
                api = STATE.api
                match x:
                    case re.IGNORECASE:
                        pass
                    case dict():
                        pass
                    case 1 | 2:
                        pass
                m = re.match(r"^/api/([^/]+)$", path)
                if m:
                    action = m.group(1)
                    return api.invoke({"hidden": api.hidden}[action], self)
        ''')
        self.assertEqual(sites, [])
        self.assertTrue(trusted)

    def test_unrelated_capture_name_is_not_a_state_rebinding_site(self):
        """A bare capture pattern for a DIFFERENT name (``case OTHER:``)
        binds ``OTHER``, never ``STATE`` -- confirms the fix matches on
        the captured NAME, the same discipline every other branch of
        :func:`_name_rebinding_sites` already applies (an ``ast.arg``
        named ``x`` is not a rebinding site for ``"y"`` either), not
        merely "is this an ``ast.MatchAs``/``ast.MatchMapping`` node
        anywhere in the function"."""
        sites, trusted = self._sites_and_trust('''
            def do_GET(self, x=1):
                path = self.path
                api = STATE.api
                match x:
                    case OTHER:
                        pass
                m = re.match(r"^/api/([^/]+)$", path)
                if m:
                    action = m.group(1)
                    return api.invoke({"hidden": api.hidden}[action], self)
        ''')
        self.assertEqual(sites, [])
        self.assertTrue(trusted)

    # -- load-bearing mutations: each of the two new arms is             -
    # -- INDEPENDENTLY necessary, not merely redundant with the other or  -
    # -- with round 10/11's pre-existing checks -- removing just ONE arm  -
    # -- (via a stub that calls the real, fixed function and then filters -
    # -- out exactly the site shape under test) must reopen exactly the   -
    # -- escape that arm closes, and no other -------------------------------

    def test_disabling_matchas_recognition_restores_the_bare_capture_escape(self):
        """MUTATION, at the provenance-helper level (#202 repair round 13,
        finding 1 retired the STRUCTURAL exemption this proof used to be
        observable through end-to-end via ``extract_routes`` -- see
        route_extract.py's ``_TRUSTED_BINDING_SOURCES``, own module
        comment -- so, exactly as
        ``CapturedArgumentProvenanceTests.test_disabling_the_free_root_check_restores_the_state_shadowing_escape``
        already does for ITS OWN mutation, this is checked directly
        against :func:`_has_dominating_trusted_binding`): ``_name_
        rebinding_sites`` replaced with a stub that calls the real
        (fixed) function and then drops every ``ast.MatchAs`` hit -- i.e.
        "as if this round never taught it that shape" -- while leaving
        the ``ast.MatchMapping`` recognition (added in the SAME commit)
        intact. The bare-capture shadow, which correctly makes
        ``_has_dominating_trusted_binding`` answer False against the real
        fix (see
        ``MatchCaptureBindingRecognitionTests.test_name_rebinding_sites_recognizes_a_bare_capture_before_the_read``
        above), goes back to WRONGLY answering True under this
        mutation."""
        original = route_extract_module._name_rebinding_sites

        def stub(name, fn):
            return [site for site in original(name, fn)
                   if not isinstance(site, getattr(ast, "MatchAs", ()))]

        route_extract_module._name_rebinding_sites = stub
        self.addCleanup(setattr, route_extract_module,
                        "_name_rebinding_sites", original)

        sites, trusted = self._sites_and_trust('''
            def do_GET(self, x=1):
                path = self.path
                match x:
                    case STATE:
                        pass
                api = STATE.api
                m = re.match(r"^/api/([^/]+)$", path)
                if m:
                    action = m.group(1)
                    return api.invoke({"hidden": api.hidden}[action], self)
        ''')
        self.assertEqual(sites, [],
                         "mutation expected to hide the MatchAs site")
        self.assertTrue(trusted,
                        "mutation expected to WRONGLY prove this shadowed "
                        "binding trusted")

    def test_disabling_matchmapping_recognition_restores_the_mapping_rest_escape(self):
        """MUTATION: the mirror image of the mutation above -- drops every
        ``ast.MatchMapping`` hit while leaving ``ast.MatchAs`` recognition
        intact. The mapping-rest shadow, which correctly makes
        ``_has_dominating_trusted_binding`` answer False against the real
        fix (see
        ``MatchCaptureBindingRecognitionTests.test_name_rebinding_sites_recognizes_a_mapping_rest_before_the_read``
        above), goes back to WRONGLY answering True under this mutation
        -- proving this arm is independently load-bearing too, not merely
        along for the ride with the ``ast.MatchAs`` arm above."""
        original = route_extract_module._name_rebinding_sites

        def stub(name, fn):
            return [site for site in original(name, fn)
                   if not isinstance(site, getattr(ast, "MatchMapping", ()))]

        route_extract_module._name_rebinding_sites = stub
        self.addCleanup(setattr, route_extract_module,
                        "_name_rebinding_sites", original)

        sites, trusted = self._sites_and_trust('''
            def do_GET(self, x=1):
                path = self.path
                match x:
                    case {**STATE}:
                        pass
                api = STATE.api
                m = re.match(r"^/api/([^/]+)$", path)
                if m:
                    action = m.group(1)
                    return api.invoke({"hidden": api.hidden}[action], self)
        ''')
        self.assertEqual(sites, [],
                         "mutation expected to hide the MatchMapping site")
        self.assertTrue(trusted,
                        "mutation expected to WRONGLY prove this shadowed "
                        "binding trusted")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
