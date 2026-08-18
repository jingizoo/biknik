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
  diagnostic, run as a real subprocess with its CAPTURED TEXT inspected.
  Runs the WHOLE ``test_sensitive_read_audit_http`` module (every public,
  invalid-session, Coach/Viewer denial, and 405 DeviceToken path) on
  Memory/SQLite always, and PostgreSQL too when ``TEST_DATABASE_URL`` is
  set in THIS process's own environment — inherited automatically by the
  child (``subprocess.run`` inherits the parent environment by default),
  the same skip-gating convention ``PostgresSensitiveReadHttpTest`` already
  uses. Asserts zero "ResourceWarning"/"Exception ignored"/"unraisable"
  text in stdout+stderr combined — never the exit code alone, which the
  review showed stays 0 even when the leak fires.
"""

import gc
import os
import subprocess
import sys
import unittest
import warnings

from helpers import BACKEND  # noqa: F401

import hockey_scheduler.web.server as srv
from test_sensitive_read_audit_http import PERSONA, SensitiveReadHttpContract

_LEAK_MARKERS = ("resourcewarning", "exception ignored", "unraisable")


def _leak_markers_in(text):
    lowered = text.lower()
    return [m for m in _LEAK_MARKERS if m in lowered]


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
# Enumerated explicitly, not derived via introspection, so a FUTURE new
# mixin test method is caught here by a NameError on the next run of THIS
# file (forcing a conscious decision to shadow it too) rather than silently
# starting to re-run under this class name.
for _name in (
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
        "test_get_and_head_to_the_post_only_routes_are_405_unaffected"):
    assert getattr(SensitiveReadHttpContract, _name, None) is not None, (
        f"{_name} no longer exists on SensitiveReadHttpContract — trim it "
        "from this shadow list")
    setattr(DirectHttpErrorCloseTest, _name, None)
del _name


class SubprocessResourceWarningTest(unittest.TestCase):
    """The review's own exact diagnostic, run as a real subprocess and its
    CAPTURED TEXT inspected — not the exit code, which stays 0 even when
    the leak fires (see module docstring)."""

    def test_full_http_audit_matrix_emits_zero_resource_warnings(self):
        tests_dir = os.path.dirname(os.path.abspath(__file__))
        proc = subprocess.run(
            [sys.executable, "-W", "error::ResourceWarning",
             "-m", "unittest", "-v", "test_sensitive_read_audit_http"],
            capture_output=True, text=True, cwd=tests_dir, timeout=180)
        combined = proc.stdout + proc.stderr
        self.assertEqual(
            proc.returncode, 0,
            f"the audit matrix itself must still pass:\n{combined}")
        hits = _leak_markers_in(combined)
        self.assertEqual(
            hits, [],
            f"leak marker(s) {hits} found in the captured subprocess "
            f"output (exit code was 0 -- text is the only reliable "
            f"signal, see module docstring):\n{combined}")
        # The exact matrix the review named must actually have RUN, not
        # been skipped/collected into zero tests by an import error that
        # would otherwise make the assertions above vacuously true.
        for marker in (
                "test_public_no_session_gets_401_and_a_durable_denial",
                "test_invalid_session_cookie_gets_401_and_a_durable_denial",
                "test_unauthorized_signed_in_roles_get_zero_disclosure_and_a_denial",
                "test_device_tokens_unauthorized_signed_in_roles_get_zero_disclosure_and_a_denial",
                "test_get_and_head_to_the_post_only_routes_are_405_unaffected"):
            self.assertIn(marker, combined)
        self.assertIn("MemorySensitiveReadHttpTest", combined)
        self.assertIn("SqliteSensitiveReadHttpTest", combined)
        if os.environ.get("TEST_DATABASE_URL"):
            self.assertIn("PostgresSensitiveReadHttpTest", combined)
        # unittest's own closing line is "OK" alone, or "OK (skipped=N)"
        # when TEST_DATABASE_URL is unset — never bare "OK" in that case.
        last_line = combined.rstrip().splitlines()[-1]
        self.assertTrue(
            last_line == "OK" or last_line.startswith("OK ("),
            f"unexpected final line {last_line!r}:\n{combined[-400:]}")


if __name__ == "__main__":
    unittest.main()
