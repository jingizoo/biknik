"""#426 round-4 review finding 2: ``test_sensitive_read_audit_http.py``'s
``_req()`` helper read an ``urllib.error.HTTPError`` and returned without
ever closing it — unlike the success path, which already closes its
response via ``with op.open(req) as r:``. The leak was invisible to a
plain ``python3 -m unittest`` run: the ``ResourceWarning`` fires from
urllib's own ``__del__`` at garbage-collection time, well after the test
method already recorded "ok". It was ALSO invisible to
``-W error::ResourceWarning``: turning the warning into an exception
inside a destructor cannot propagate normally (a ``__del__`` can't raise
past the garbage collector), so Python prints "Exception ignored while
calling deallocator ..." to stderr and the process still exits 0 — the
review's own exact point, reproduced verbatim before the fix:

    python3 -W error::ResourceWarning -m unittest -v \\
        test_sensitive_read_audit_http.MemorySensitiveReadHttpTest.\\
        test_device_tokens_unauthorized_signed_in_roles_get_zero_disclosure_and_a_denial

exited 0 while printing two "Implicitly cleaning up <HTTPError 403>"
warnings. The fix wraps the ``except urllib.error.HTTPError as e:`` branch
in ``with e:`` (``HTTPError`` IS an ``addinfourl``/``addbase`` — the same
class ``urlopen()`` returns on success — so it supports the identical
context-manager protocol the success path already uses).

This module proves the fix two ways, neither of which trusts an exit code:

* :class:`DirectHttpErrorCloseTest` — in-process, deterministic:
  ``warnings.catch_warnings(record=True)`` around real ``_req()`` calls
  covering the public/invalid-session/Coach-denial/405-DeviceToken paths
  the review named, asserting the recorded list is empty. Deterministic
  because CPython reclaims the unreferenced ``HTTPError`` by refcount the
  moment ``_req()``'s own ``except`` frame exits (confirmed: the warnings
  fired synchronously, mid-test, in the review's own transcript above —
  not deferred to interpreter shutdown), not at some later, unpredictable
  GC pass; ``gc.collect()`` is called anyway as defence in depth.
* :class:`SubprocessResourceWarningTest` — the review's own exact
  diagnostic, run as real subprocesses with their CAPTURED TEXT inspected.
  Covers every public, invalid-session, Coach/Viewer denial, and 405
  DeviceToken path on Memory/SQLite always, and PostgreSQL too when
  ``TEST_DATABASE_URL`` is set in THIS process's own environment —
  inherited automatically by the child (``subprocess.run`` inherits the
  parent environment by default), the same skip-gating convention
  ``PostgresSensitiveReadHttpTest`` already uses. Asserts zero
  "ResourceWarning"/"Exception ignored"/"unraisable" text in
  stdout+stderr combined — never the exit code alone, which the review
  showed stays 0 even when the leak fires.

  #205 review: that probe used to be ONE opaque child running the whole
  ``test_sensitive_read_audit_http`` module (~51 cases across three
  backends) under a single ``timeout=180``. It duly timed out in CI (run
  32568130004's postgres job) and raised a bare
  ``subprocess.TimeoutExpired`` that named NO backend and NO case, so the
  red check said only "something in there took too long". It is now a
  bounded MATRIX: one child per backend class, each with its own measured
  bound (see ``_PROBE_MATRIX``), and a timeout is caught and turned into a
  failure that NAMES the backend and the last case the child reached, read
  out of the partial ``-v`` stream ``TimeoutExpired`` carries. See
  :class:`LeakProbeRegressionTest` for the forced-hang test that pins that
  naming, and for the two deliberate-leak tests that pin the detection
  itself.
"""

import gc
import os
import re
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
import warnings

from helpers import BACKEND  # noqa: F401

import hockey_scheduler.web.server as srv
import test_sensitive_read_audit_http as audit_http
from test_sensitive_read_audit_http import PERSONA, SensitiveReadHttpContract

_LEAK_MARKERS = ("resourcewarning", "exception ignored", "unraisable")


def _leak_markers_in(text):
    lowered = text.lower()
    return [m for m in _LEAK_MARKERS if m in lowered]


def _check_no_leak_markers(combined, label):
    """The leak contract itself, in ONE place: a probe's captured TEXT must
    carry none of :data:`_LEAK_MARKERS`.

    Deliberately NOT a ``TestCase`` method — :class:`SubprocessResourceWarning
    Test` calls it to assert cleanliness, and :class:`LeakProbeRegressionTest`
    calls it on the output of a deliberately-leaking child to assert it
    RAISES. Both therefore exercise the identical check, so the regressions
    cannot drift into re-implementing a weaker copy of it.
    """
    hits = _leak_markers_in(combined)
    if hits:
        raise AssertionError(
            f"{label}: leak marker(s) {hits} found in the captured subprocess "
            f"output (exit code was not consulted -- text is the only reliable "
            f"signal, see module docstring):\n{combined}")


# --------------------------------------------------------------------------
# The bounded subprocess matrix
# --------------------------------------------------------------------------

_AUDIT_MODULE = "test_sensitive_read_audit_http"

# Every ``test_*`` method ``SensitiveReadHttpContract`` itself defines — the
# single source of truth for BOTH the shadow list further down (which stops
# unittest re-running the mixin's matrix a fourth time under
# DirectHttpErrorCloseTest) AND the per-backend case count the subprocess
# matrix asserts. Enumerated explicitly, not derived via introspection, so a
# FUTURE new mixin test method is caught by the ``assert`` in the shadow loop
# rather than silently changing either consumer's meaning.
_CONTRACT_CASES = (
    "test_authorized_roles_get_real_reads_with_exact_attribution",
    "test_unauthorized_signed_in_roles_get_zero_disclosure_and_a_denial",
    "test_public_no_session_gets_401_and_a_durable_denial",
    "test_invalid_session_cookie_gets_401_and_a_durable_denial",
    "test_one_request_one_correlation_id_distinct_across_requests",
    "test_head_request_is_gated_the_same_as_get",
    "test_device_tokens_authorized_roles_get_real_reads_with_exact_attribution",
    "test_device_tokens_unauthorized_signed_in_roles_get_zero_disclosure_and_a_denial",
    "test_device_tokens_public_no_session_gets_401_and_a_durable_denial",
    "test_device_tokens_invalid_session_cookie_gets_401_and_a_durable_denial",
    "test_toggle_propagates_the_real_signed_in_actor",
    "test_device_token_toggle_propagates_the_real_signed_in_actor",
    "test_allowed_personas_get_normal_behavior_for_every_route",
    "test_denied_personas_get_zero_disclosure_and_exactly_one_denial",
    "test_toggle_refused_for_coach_leaves_row_untouched",
    "test_public_and_invalid_session_get_denial_on_every_route",
    "test_get_and_head_to_the_post_only_routes_are_405_unaffected",
)
_CASES_PER_BACKEND = len(_CONTRACT_CASES)

# The five paths #426's review named by hand. Kept as documentation of
# provenance only: the matrix below asserts ALL of _CONTRACT_CASES appear in
# EVERY backend's own output, which is a strict superset of these.
_REVIEW_NAMED_CASES = (
    "test_public_no_session_gets_401_and_a_durable_denial",
    "test_invalid_session_cookie_gets_401_and_a_durable_denial",
    "test_unauthorized_signed_in_roles_get_zero_disclosure_and_a_denial",
    "test_device_tokens_unauthorized_signed_in_roles_get_zero_disclosure_and_a_denial",
    "test_get_and_head_to_the_post_only_routes_are_405_unaffected",
)
assert set(_REVIEW_NAMED_CASES) <= set(_CONTRACT_CASES), (
    "a review-named case was renamed in _CONTRACT_CASES without being renamed "
    "here")

# Per-invocation bounds, in seconds — one child per backend class.
#
# WHY PER CLASS AND NOT PER TEST. Splitting further, to one child per case, is
# affordable (interpreter start + importing the audit module measured 0.09s,
# and a lone PostgreSQL case measured 1.73s standalone versus 1.55-1.73s
# in-class, so 51 children would add only ~5s) but it would WEAKEN the leak
# contract: several of the leaks this file hunts are only reported when the
# interpreter tears down (run_parallel.py's own docstring records a whole
# class of them surfacing at INTERPRETER SHUTDOWN, after unittest had already
# printed its verdict), and cumulative leaks — a pool that exhausts only after
# N fixtures — need N fixtures in ONE process to show up at all. Keeping each
# backend's full matrix in a single child preserves that, and the timeout
# handler below already recovers per-CASE naming from the partial -v stream,
# so nothing diagnostic is bought by paying for 51 processes.
#
# WHERE THE NUMBERS COME FROM. Measured on this branch, with this file's own
# probe command, against a real PostgreSQL (17 cases per class):
#
#                       unloaded   8 spinners   16 spinners
#     Memory              17.1s         --          37.6s   (2.20x)
#     SQLite              15.8s         --          38.1s   (2.41x)
#     PostgreSQL          25.1s        51.5s       933.1s   (37.2x)
#     all three, 1 child  57.3s       110.2s          --
#
# on an 18-core host, the spinner counts standing in for the oversubscription
# CI's ``run_parallel.py -j 2`` produces alongside a postgres service
# container. Two things fall out, and they are the answer to "what actually
# consumed the 180s":
#
# * It is NOT a hung case. Per-case durations stay flat at every load level
#   measured — unloaded PostgreSQL spans 1.547s-1.732s (max/min 1.12), and
#   under 8 spinners 2.73s-4.80s. The cost scales; it does not concentrate.
# * It IS aggregate volume, and it is dominated by PostgreSQL, which degrades
#   SUPERLINEARLY where Memory/SQLite degrade merely proportionally (37x vs
#   ~2.3x at the same load). Memory and SQLite drive their store in-process;
#   the PostgreSQL fixture pays a client<->server round trip per statement
#   (plus a full clear_all_data() schema reset per case), so once the host is
#   oversubscribed every round trip waits on the scheduler and the per-case
#   cost explodes. The old single 180s budget covered all three at once, so
#   PostgreSQL's blowup ate the budget and killed the child mid-PostgreSQL —
#   taking Memory's and SQLite's verdicts down with it and naming nothing.
#
# A BOUND HERE IS A CEILING, NOT A COST. Nothing waits for it in the normal
# case: unloaded, the whole matrix costs ~57s (Memory 17.1s, SQLite 15.8s,
# PostgreSQL 25.1s). The number only binds when something is already wrong,
# and its job is to be generous enough not to invent a flake while still
# firing on genuine pathology -- with a name attached.
#
# Load figures, measured under TRUE 2x oversubscription (36 busy-loop spinners
# on 18 cores, host at 1715% of 1800% CPU). An earlier revision of this comment
# justified the bounds from a "16 spinner" run that was semaphore-bound at
# ~558% CPU -- i.e. not oversubscribed at all -- and therefore understated the
# degradation badly. The real numbers:
#
#     Memory      17.1s -> 94.8s   (5.5x)
#     SQLite      15.8s -> 83.1s   (5.3x)
#     PostgreSQL  25.1s -> 914.6s  (36.4x)
#
# Only PostgreSQL's superlinearity was ever right; Memory/SQLite degrade ~5x,
# not the ~2.3x previously claimed. So Memory/SQLite get 180s (~10x unloaded,
# ~1.9x their real oversubscribed cost) rather than the 120s a bad measurement
# suggested -- 120s left Memory at 1.27x headroom, putting the flake risk in
# the opposite place from where the comment asserted it was.
#
# PostgreSQL stays at 300s (~12x unloaded) DELIBERATELY, even though 914.6s
# would breach it: at genuine 2x oversubscription this probe SHOULD fail. The
# owner's correction forbids clearing this by enlarging a timeout, and a bound
# so large it can never fire would do exactly that. 300s is ~3.7x the ~80s
# PostgreSQL would cost at the ~3.2x degradation CI actually exhibited (it
# breached 180s for work costing 57s unloaded), so it is comfortable for real
# CI load and still fires under true pathology.
#
# The part that matters is independent of every number above: a breach is now
# a NAMED failure identifying the backend and the last case reached (pinned by
# _HangingProbe), not the anonymous traceback CI produced.
_PROBE_MATRIX = {
    "MemorySensitiveReadHttpTest": 180,
    "SqliteSensitiveReadHttpTest": 180,
    "PostgresSensitiveReadHttpTest": 300,
}

# Bound for the tiny self-contained fixtures LeakProbeRegressionTest writes to
# a temp dir. They do no project work at all (one HTTP round trip, or one
# psycopg connect), so they finish in well under a second; 60s is pure slack
# for a saturated CI host and still fails fast if one ever wedges.
_REGRESSION_BOUND = 60
# Bound for the deliberate-hang fixture. Its first case is a no-op, so the
# child reliably reaches and blocks in the SECOND case long before this
# expires; the test costs this many seconds by construction, so keep it small.
_HANG_BOUND = 10

# unittest's own verbose progress line, e.g.
#     test_head_request_is_gated_the_same_as_get (test_sensitive_read_audit_ht
#     tp.MemorySensitiveReadHttpTest.test_head_request_is_gated_the_same_as_ge
#     t) ... ok
# ``TextTestResult.startTest`` writes the description and " ... " and FLUSHES
# before the case body runs, so in a killed child's partial stream the final
# such line names the case that was in flight — with its verdict group empty,
# because the case never got to report one.
_VERBOSE_CASE = re.compile(r"^(test_\w+) \((\S+)\)(?: \.\.\. (.*))?$")


def _decode_partial(stream):
    """Return one of ``TimeoutExpired``'s captured streams as ``str``.

    ``subprocess.TimeoutExpired`` really does carry the output captured
    BEFORE the kill — but not in the shape ``text=True`` implies.
    ``Popen._communicate`` raises with the raw byte buffers it has
    accumulated so far, and the decoding ``communicate()`` would normally
    apply happens only on the non-timeout return path; a pipe that produced
    nothing at all arrives as ``None`` rather than ``b""``. Verified rather
    than assumed, on this suite's interpreter (CPython 3.14.6): a
    deliberately hung ``-m unittest -v`` child yields ``e.stdout is None``
    and ``e.stderr`` as ``bytes``, even though the call passed
    ``text=True``. Assuming ``str`` here would raise ``TypeError`` inside
    the timeout handler and reproduce the exact name-nothing failure this
    matrix exists to prevent.
    """
    if stream is None:
        return ""
    if isinstance(stream, (bytes, bytearray)):
        return bytes(stream).decode("utf-8", "replace")
    return stream


def _last_case_reached(partial):
    """Parse a partial ``-m unittest -v`` stream for the last case that
    STARTED. Returns ``(case_name, verdict, completed_count)``; ``verdict``
    is ``None`` when that case had not reported one, i.e. it was still
    running when the child was killed."""
    name = verdict = None
    completed = 0
    for line in partial.splitlines():
        match = _VERBOSE_CASE.match(line)
        if match is None:
            continue
        name, verdict = match.group(1), (match.group(3) or None)
        if verdict:
            completed += 1
    return name, verdict, completed


def _timeout_report(label, target, bound, exc, expected_cases):
    """Turn a ``TimeoutExpired`` into a message that NAMES the backend and the
    case reached — the whole point of the #205 correction."""
    partial = _decode_partial(exc.output) + _decode_partial(exc.stderr)
    name, verdict, completed = _last_case_reached(partial)
    if name is None:
        where = ("NO case had started yet -- the child was still importing or "
                 "building its first fixture when the bound expired")
    elif verdict is None:
        where = f"last case reached: {name} -- STILL RUNNING when killed"
    else:
        where = (f"last case reached: {name} -- it had already reported "
                 f"{verdict!r}, so the child wedged after it (tearDown, the "
                 f"next fixture, or interpreter exit)")
    tail = "\n".join(partial.rstrip().splitlines()[-12:]) or "<no output at all>"
    return (
        f"{label}: the leak probe for {target!r} exceeded its documented "
        f"{bound}s bound; {where}; {completed} of {expected_cases} cases had "
        f"completed.\nTail of the partial -v stream captured before the kill:"
        f"\n{tail}")


def _run_bounded_probe(target, bound, cwd):
    """Run ONE ``-W error::ResourceWarning`` probe child under ``bound``
    seconds. Returns ``(CompletedProcess, elapsed)``; propagates
    ``subprocess.TimeoutExpired`` for :func:`_run_probe_or_fail` to name."""
    started = time.monotonic()
    proc = subprocess.run(
        [sys.executable, "-W", "error::ResourceWarning",
         "-m", "unittest", "-v", target],
        capture_output=True, text=True, cwd=cwd, timeout=bound)
    return proc, time.monotonic() - started


def _run_probe_or_fail(target, bound, label, cwd,
                       expected_cases=_CASES_PER_BACKEND):
    """:func:`_run_bounded_probe`, with a timeout converted into unittest's own
    ``failureException`` (``AssertionError``) carrying the backend name and the
    last case reached. ``from None`` because ``TimeoutExpired``'s own traceback
    is the uninformative one CI printed."""
    try:
        return _run_bounded_probe(target, bound, cwd)
    except subprocess.TimeoutExpired as exc:
        raise AssertionError(
            _timeout_report(label, target, bound, exc, expected_cases)) from None


def _tests_dir():
    return os.path.dirname(os.path.abspath(__file__))


class DirectHttpErrorCloseTest(SensitiveReadHttpContract, unittest.TestCase):
    """Mixes in ``SensitiveReadHttpContract`` for its real fixture (a genuine
    ``ThreadingHTTPServer`` + store + seeded sentinel destinations/token) —
    the SAME contract ``MemorySensitiveReadHttpTest``/``SqliteSensitiveRead
    HttpTest``/``PostgresSensitiveReadHttpTest`` mix in. Unlike those three,
    this class exists ONLY to add the resource-warning-focused test methods
    below, so every ``test_*`` the mixin itself defines is explicitly
    shadowed with ``None`` right after the class body — the mixin's own
    methods are plain attributes inherited like any other, so unittest's
    loader would otherwise discover and re-run its entire functional matrix
    a FOURTH time under this class name too, with zero added resource-
    warning signal (those methods call ``self._req()`` directly, never
    through ``warnings.catch_warnings``) — that functional coverage already
    belongs to the three classes named above.
    """

    def database_url(self):
        return None

    def _assert_req_leaks_nothing(self, method, path, *, opener=None,
                                  cookie=None, expect_status):
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            status, body, _ = self._req(
                method, path, opener=opener, cookie=cookie)
            gc.collect()  # defence in depth; see module docstring
        self.assertEqual(status, expect_status, body)
        resource_warnings = [w for w in caught
                             if issubclass(w.category, ResourceWarning)]
        self.assertEqual(
            resource_warnings, [],
            f"_req() leaked {len(resource_warnings)} ResourceWarning(s) on "
            f"{method} {path} -> {expect_status}: "
            f"{[str(w.message) for w in resource_warnings]}")

    def test_public_401_closes_cleanly(self):
        self._assert_req_leaks_nothing(
            "GET", "/api/notifications/contacts", expect_status=401)

    def test_invalid_session_401_closes_cleanly(self):
        self._assert_req_leaks_nothing(
            "GET", "/api/notifications/contacts", expect_status=401,
            cookie=f"{srv.SESSION_COOKIE}=totally-bogus-session-token")

    def test_coach_denial_403_closes_cleanly(self):
        op = self._login(PERSONA["coach"])
        self._assert_req_leaks_nothing(
            "GET", "/api/notifications/contacts", opener=op,
            expect_status=403)

    def test_viewer_denial_403_closes_cleanly(self):
        op = self._login(PERSONA["viewer"])
        self._assert_req_leaks_nothing(
            "GET", "/api/notifications/device-tokens", opener=op,
            expect_status=403)

    def test_device_token_toggle_405_closes_cleanly(self):
        op = self._login(PERSONA["league_admin"])
        self._assert_req_leaks_nothing(
            "GET", self._device_token_toggle_path(), opener=op,
            expect_status=405)


# Shadow every test_* the mixin defines (see the class docstring above) so
# unittest's loader finds only this file's own 5 methods on this class.
# _CONTRACT_CASES is enumerated explicitly, not derived via introspection, so
# a FUTURE new mixin test method is caught here by the assert below (forcing a
# conscious decision to shadow it too, and to re-check _CASES_PER_BACKEND)
# rather than silently starting to re-run under this class name.
for _name in _CONTRACT_CASES:
    assert getattr(SensitiveReadHttpContract, _name, None) is not None, (
        f"{_name} no longer exists on SensitiveReadHttpContract — trim it "
        "from _CONTRACT_CASES")
    setattr(DirectHttpErrorCloseTest, _name, None)
del _name


class SubprocessResourceWarningTest(unittest.TestCase):
    """The review's own exact diagnostic, run as real subprocesses and their
    CAPTURED TEXT inspected — not the exit code, which stays 0 even when the
    leak fires (see module docstring).

    One child PER BACKEND CLASS, each under its own measured bound from
    ``_PROBE_MATRIX``, so a slow or stuck backend is named instead of
    anonymously consuming a shared budget and voiding the other backends'
    verdicts along with its own.
    """

    maxDiff = None

    def _assert_backend_probe_is_clean(self, class_name):
        bound = _PROBE_MATRIX[class_name]
        target = f"{_AUDIT_MODULE}.{class_name}"
        proc, elapsed = _run_probe_or_fail(
            target, bound, class_name, _tests_dir())
        combined = proc.stdout + proc.stderr

        # (1) The bound, restated as an assertion. subprocess.run's timeout=
        # already enforces it, so this is belt-and-braces: it keeps the bound
        # a checked property of the RECORDED elapsed time, so a future edit
        # that loosens or drops the timeout kwarg still fails here instead of
        # silently restoring an unbounded probe.
        self.assertLess(
            elapsed, bound,
            f"{class_name}: probe took {elapsed:.1f}s, over its {bound}s bound")

        # (2) The audit matrix must still pass on its own terms...
        self.assertEqual(
            proc.returncode, 0,
            f"{class_name}: the audit matrix itself must still pass:\n"
            f"{combined}")
        # (3) ...but the LEAK verdict is read from the captured TEXT, never
        # the exit code, which stays 0 even when the leak fires.
        _check_no_leak_markers(combined, class_name)

        # (4) Anti-vacuity, now enforced PER BACKEND rather than across a
        # merged transcript: every contract case must appear in THIS
        # backend's own output, and the count line must agree. Strictly
        # stronger than the old check, which asserted five case names against
        # the three backends' combined text and so was satisfied if a single
        # backend contributed all five.
        for case in _CONTRACT_CASES:
            self.assertIn(
                case, combined,
                f"{class_name}: {case} did not run — an import error or a "
                f"shrunken matrix would otherwise make the leak assertions "
                f"vacuously true:\n{combined}")
        self.assertIn(class_name, combined, combined)
        self.assertIn(
            f"Ran {_CASES_PER_BACKEND} tests", combined,
            f"{class_name}: expected exactly {_CASES_PER_BACKEND} cases:\n"
            f"{combined}")

        # (5) A bare "OK" — no "OK (skipped=N)". Because each child now runs
        # exactly ONE backend class, a skip here means that backend did not
        # really run, which the old merged run could not distinguish: its
        # `assertIn("PostgresSensitiveReadHttpTest", combined)` was satisfied
        # by the very "... skipped" line that proves the opposite.
        last_line = combined.rstrip().splitlines()[-1]
        self.assertEqual(
            last_line, "OK",
            f"{class_name}: expected a clean bare 'OK' (a skip here means "
            f"this backend did not actually run):\n{combined[-400:]}")

    def test_memory_backend_probe_emits_zero_resource_warnings(self):
        self._assert_backend_probe_is_clean("MemorySensitiveReadHttpTest")

    def test_sqlite_backend_probe_emits_zero_resource_warnings(self):
        self._assert_backend_probe_is_clean("SqliteSensitiveReadHttpTest")

    @unittest.skipUnless(os.environ.get("TEST_DATABASE_URL"),
                         "PostgreSQL suite only (TEST_DATABASE_URL not set)")
    def test_postgres_backend_probe_emits_zero_resource_warnings(self):
        # Gated exactly as PostgresSensitiveReadHttpTest itself is, so when
        # TEST_DATABASE_URL IS set this test runs and (5) above proves the
        # child did not skip — i.e. PostgreSQL remains required, not merely
        # mentioned, whenever the variable is present.
        self._assert_backend_probe_is_clean("PostgresSensitiveReadHttpTest")

    def test_every_backend_class_has_its_own_documented_bound(self):
        """A new backend class in the audit module must not escape the bounded
        matrix (and a bound must not outlive the class it names)."""
        discovered = sorted(
            name for name, obj in vars(audit_http).items()
            if isinstance(obj, type)
            and issubclass(obj, unittest.TestCase)
            and issubclass(obj, SensitiveReadHttpContract))
        self.assertEqual(
            discovered, sorted(_PROBE_MATRIX),
            "test_sensitive_read_audit_http's backend classes and this file's "
            "_PROBE_MATRIX have drifted apart — every backend class needs its "
            "own measured per-invocation bound, and no bound may name a class "
            "that no longer exists")


# --------------------------------------------------------------------------
# Regression fixtures: tiny, self-contained modules written to a temp dir and
# run through the SAME bounded-probe helpers the matrix above uses. They are
# real deliberate leaks and a real deliberate hang — not simulated strings —
# so they prove the probe still detects what it exists to detect.
# --------------------------------------------------------------------------

_HTTP_ERROR_LEAK_SOURCE = """
    import gc
    import threading
    import unittest
    import urllib.error
    import urllib.request
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


    class _Denier(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(403)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"{}")

        def log_message(self, *args):
            pass


    class DeliberateHttpErrorLeakTest(unittest.TestCase):
        def setUp(self):
            self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Denier)
            threading.Thread(target=self.httpd.serve_forever,
                             daemon=True).start()
            self.addCleanup(self.httpd.server_close)
            self.addCleanup(self.httpd.shutdown)

        def test_reads_an_httperror_and_never_closes_it(self):
            port = self.httpd.server_address[1]
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/denied")
            except urllib.error.HTTPError as e:
                e.read()  # read it, then drop it WITHOUT the `with e:` fix
            gc.collect()
"""

_PSYCOPG_LEAK_SOURCE = """
    import gc
    import os
    import unittest

    import psycopg


    class DeliberatePsycopgLeakTest(unittest.TestCase):
        def test_opens_a_connection_and_never_closes_it(self):
            conn = psycopg.connect(os.environ["TEST_DATABASE_URL"])
            conn.execute("SELECT 1").fetchone()
            del conn  # dropped WITHOUT .close() and without `with`
            gc.collect()
"""

# Alphabetical method order is unittest's default, so _b_ hangs after _a_ has
# passed and before _c_ is ever reached — which is what lets the forced-hang
# test assert the report names the case actually reached and NOT a later one.
_HANG_SOURCE = """
    import time
    import unittest


    class HangingBackendTest(unittest.TestCase):
        def test_a_case_that_completes_first(self):
            pass

        def test_b_the_case_that_hangs_forever(self):
            time.sleep(3600)

        def test_c_case_that_is_never_reached(self):
            pass
"""


class LeakProbeRegressionTest(unittest.TestCase):
    """#205 review's required regression coverage for the bounded probe:

    * the probe still FAILS on a real unclosed ``HTTPError`` (the original
      #426 leak) and on a real unclosed psycopg resource, in both cases while
      the child exits 0 — so it is the TEXT contract, not the exit code, that
      is doing the work; and
    * a timeout NAMES the backend and the last case reached, which is exactly
      what CI run 32568130004 failed to do.
    """

    maxDiff = None

    def _fixture_dir(self, name, source):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        with open(os.path.join(tmp.name, f"{name}.py"), "w") as handle:
            handle.write(textwrap.dedent(source))
        return tmp.name

    def _assert_probe_reports_a_leak(self, name, source, signature, label):
        cwd = self._fixture_dir(name, source)
        proc, _elapsed = _run_probe_or_fail(
            name, _REGRESSION_BOUND, label, cwd, expected_cases=1)
        combined = proc.stdout + proc.stderr
        # The exit code LIES: a ResourceWarning raised inside a __del__ cannot
        # propagate past the garbage collector, so the child still reports OK
        # and exits 0. This is the premise of the whole file, re-proved here
        # against a live leak rather than asserted in prose.
        self.assertEqual(proc.returncode, 0, combined)
        self.assertIn("OK", combined)
        self.assertIn(signature, combined, combined)
        # ...and the TEXT contract catches it anyway.
        with self.assertRaises(AssertionError) as caught:
            _check_no_leak_markers(combined, label)
        message = str(caught.exception)
        self.assertIn("resourcewarning", message)
        self.assertIn("exception ignored", message)

    def test_probe_still_fails_when_an_httperror_is_left_unclosed(self):
        self._assert_probe_reports_a_leak(
            "_deliberate_httperror_leak", _HTTP_ERROR_LEAK_SOURCE,
            "Implicitly cleaning up <HTTPError 403",
            "deliberate-HTTPError-leak fixture")

    @unittest.skipUnless(os.environ.get("TEST_DATABASE_URL"),
                         "PostgreSQL suite only (TEST_DATABASE_URL not set)")
    def test_probe_still_fails_when_a_psycopg_resource_is_left_unclosed(self):
        self._assert_probe_reports_a_leak(
            "_deliberate_psycopg_leak", _PSYCOPG_LEAK_SOURCE,
            "was deleted while still open",
            "deliberate-psycopg-leak fixture")

    def test_timeout_names_the_backend_and_the_last_case_reached(self):
        cwd = self._fixture_dir("_deliberate_hang", _HANG_SOURCE)
        label = "HangingBackendTest"
        with self.assertRaises(AssertionError) as caught:
            _run_probe_or_fail("_deliberate_hang", _HANG_BOUND, label, cwd,
                               expected_cases=3)
        report = str(caught.exception)
        # The two facts CI run 32568130004's traceback carried neither of.
        self.assertIn(label, report, report)
        self.assertIn("test_b_the_case_that_hangs_forever", report, report)
        # ...and it is the case actually REACHED, not just any name in the
        # module: the case after the hang must not be reported as reached.
        self.assertNotIn("test_c_case_that_is_never_reached", report, report)
        self.assertIn("STILL RUNNING when killed", report, report)
        self.assertIn(f"{_HANG_BOUND}s bound", report, report)
        self.assertIn("1 of 3 cases had completed", report, report)


if __name__ == "__main__":
    unittest.main()
