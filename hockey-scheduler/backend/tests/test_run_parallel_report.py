"""The parallel runner's failure report is itself under test (#382).

This module exists because of how #382 was originally "proved". CI on main went
red with no assertion anywhere in the log — the failing shard's report was a
blind byte slice, ``err[-4000:]``, and the HTTP fixtures leak listening sockets
whose ``ResourceWarning: unclosed <socket …>`` lines are emitted at interpreter
shutdown, *after* unittest has printed ``FAILED (failures=1)``. Hundreds of them
filled the whole 4000-byte window and pushed the ``FAIL:`` banner, the traceback
and the ``AssertionError`` out of it.

The fix was demonstrated by hand-breaking a test and pasting the transcript into
the pull request. A transcript does not run again. The very infrastructure whose
failure ERASED the real assertion was guarded by nothing executable, which is
why the report helpers are pinned here instead — every clause below is a
property some future edit can break, and each one has been shown to fail under
a deliberate mutation of the code it covers.

What is pinned:

* ``--fail-output-lines N`` bounds the excerpt to N lines TOTAL, its own
  omission/elision markers included. The first version allocated all N lines
  between head and tail and *then* added markers, so N=10 returned 12 lines.
* ``MIN_FAIL_OUTPUT_LINES`` is validated, not silently overshot.
* The excerpt is anchored to the FIRST failure, not to the tail: a blind tail
  drops the first failures of a multi-failure shard.
* Both the head (first failures, with their assertions) and the tail
  (``Ran N tests`` / ``FAILED (…)``) survive the elision.
* ``unclosed <socket …>`` warnings are stripped, and *other* ResourceWarnings
  (unclosed files, unclosed database connections — real leak signal) are not,
  nor is the traceback that follows a stripped warning.
* The ``FAILING TESTS`` roll-up names every failure.
* End to end: a failure followed by more than 4,000 bytes of socket warnings
  still reports the failing test's name and its traceback.
"""

import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest

# ``run_parallel`` lives beside this module but is not a package member, and the
# suite is launched both as ``unittest discover -s tests`` (which puts tests/ on
# sys.path) and as ``unittest tests.test_…`` (which does not). Add it either way.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import run_parallel as rp  # noqa: E402  (needs the sys.path line above)


# A faithful reproduction of what CPython writes for a leaked listening socket:
# an attributed header line, the echoed source line (indented), and the
# tracemalloc hint. Reproduced verbatim rather than provoked by really leaking a
# socket, because the emission happens during garbage collection at interpreter
# shutdown and is therefore not reliably reproducible inside a test.
SOCKET_WARNING = (
    "/usr/lib/python3.14/socket.py:{n}: ResourceWarning: unclosed "
    "<socket.socket fd=9, family=2, type=1, proto=6, "
    "laddr=('127.0.0.1', 5{n:04d})>\n"
    "  self._sock = None\n"
    "ResourceWarning: Enable tracemalloc to get the object allocation traceback\n"
)

FAILURE_BLOCK = (
    "=" * 70 + "\n"
    "FAIL: test_roster_locks (test_roster.RosterTest.test_roster_locks)\n"
    + "-" * 70 + "\n"
    "Traceback (most recent call last):\n"
    '  File "/b/tests/test_roster.py", line 42, in test_roster_locks\n'
    "    self.assertEqual(status, \"LOCKED\")\n"
    "AssertionError: 'OPEN' != 'LOCKED'\n"
    "\n"
)

SUMMARY_BLOCK = "Ran 1874 tests in 96.3s\n\nFAILED (failures=1, skipped=256)\n"


def socket_warning_flood(count):
    """``count`` leaked-socket warnings, as the garbage collector renders them."""
    return "".join(SOCKET_WARNING.format(n=i) for i in range(count))


class FailureExcerptBudgetTest(unittest.TestCase):
    """``--fail-output-lines N`` means N lines TOTAL, markers included."""

    def _report(self, pre_lines=6, filler_lines=40):
        """Output whose failure section overflows any budget under test."""
        return (
            "".join(f"noise line {i}\n" for i in range(pre_lines))
            + FAILURE_BLOCK
            + "".join(f"filler line {i}\n" for i in range(filler_lines))
            + SUMMARY_BLOCK
        )

    def test_normal_n_emits_exactly_n_lines(self):
        """The regression the repo owner measured: N=10 used to return 12."""
        text = self._report()
        for n in (10, 12, 25):
            got = rp._failure_excerpt(text, n).splitlines()
            self.assertEqual(
                len(got), n,
                f"--fail-output-lines {n} emitted {len(got)} lines:\n"
                + "\n".join(got))

    def test_minimum_n_emits_exactly_min_lines(self):
        """N=3 is the floor and it is exact, not 3-plus-markers."""
        got = rp._failure_excerpt(self._report(), rp.MIN_FAIL_OUTPUT_LINES)
        self.assertEqual(len(got.splitlines()), rp.MIN_FAIL_OUTPUT_LINES,
                         f"minimum-N excerpt was not exact:\n{got}")

    def test_minimum_n_still_carries_name_marker_and_summary(self):
        """What the floor buys: WHICH test failed, that a gap exists, HOW MANY."""
        got = rp._failure_excerpt(
            self._report(), rp.MIN_FAIL_OUTPUT_LINES).splitlines()
        self.assertIn(
            "FAIL: test_roster_locks (test_roster.RosterTest.test_roster_locks)",
            got)
        self.assertTrue(any("elided from the middle" in ln for ln in got),
                        f"no elision marker, so the gap is silent:\n{got}")
        self.assertIn("FAILED (failures=1, skipped=256)", got)

    def test_n_below_minimum_is_rejected_not_overshot(self):
        """N=1 and N=2 cannot carry a report, so they are an error, not 3 lines."""
        text = self._report()
        for n in (1, 2):
            with self.assertRaises(ValueError) as caught:
                rp._failure_excerpt(text, n)
            self.assertIn("--fail-output-lines", str(caught.exception))
            self.assertIn(str(rp.MIN_FAIL_OUTPUT_LINES), str(caught.exception))

    def test_cli_rejects_below_minimum_before_the_suite_runs(self):
        """Argument validation, so a 40-minute run does not die at the report."""
        for bad in ("1", "2", "-1"):
            with self.assertRaises(Exception):
                rp._fail_output_lines_arg(bad)
        self.assertEqual(rp._fail_output_lines_arg("0"), 0)
        self.assertEqual(rp._fail_output_lines_arg(str(rp.MIN_FAIL_OUTPUT_LINES)),
                         rp.MIN_FAIL_OUTPUT_LINES)

    def test_zero_is_unbounded(self):
        text = self._report()
        self.assertEqual(rp._failure_excerpt(text, 0), text.rstrip("\n"))

    def test_short_output_is_not_padded_or_marked(self):
        """Under budget means untouched — no markers bought that nothing needs."""
        text = FAILURE_BLOCK + SUMMARY_BLOCK
        got = rp._failure_excerpt(text, 200)
        self.assertEqual(got, text.rstrip("\n"))
        self.assertNotIn("elided", got)
        self.assertNotIn("omitted", got)

    def test_budget_holds_across_shapes_and_sizes(self):
        """Sweep, because the accounting has one branch per marker."""
        for pre in (0, 1, 5):
            for filler in (0, 3, 9, 40):
                text = self._report(pre_lines=pre, filler_lines=filler)
                for n in range(rp.MIN_FAIL_OUTPUT_LINES, 30):
                    got = rp._failure_excerpt(text, n).splitlines()
                    self.assertLessEqual(
                        len(got), n,
                        f"pre={pre} filler={filler} N={n} emitted {len(got)}:\n"
                        + "\n".join(got))

    def test_elision_marker_never_claims_zero_lines(self):
        """A marker that says '0 line(s) elided' means the budget maths slipped."""
        for pre in (0, 1, 5):
            for filler in (0, 3, 9, 40):
                text = self._report(pre_lines=pre, filler_lines=filler)
                for n in range(rp.MIN_FAIL_OUTPUT_LINES, 30):
                    for line in rp._failure_excerpt(text, n).splitlines():
                        if "elided from the middle" in line:
                            self.assertNotIn(" 0 line(s) ", line)


class FailureExcerptAnchorTest(unittest.TestCase):
    """The excerpt starts at the FIRST failure, not at the end of the log."""

    def _two_failures(self, gap_lines=60):
        return (
            "".join(f"leading noise {i}\n" for i in range(30))
            + "=" * 70 + "\n"
            "FAIL: test_first (test_a.A.test_first)\n"
            + "-" * 70 + "\n"
            "Traceback (most recent call last):\n"
            "AssertionError: the FIRST failure\n\n"
            + "".join(f"middle noise {i}\n" for i in range(gap_lines))
            + "=" * 70 + "\n"
            "FAIL: test_last (test_z.Z.test_last)\n"
            + "-" * 70 + "\n"
            "AssertionError: the LAST failure\n\n"
            + SUMMARY_BLOCK
        )

    def test_first_failure_survives_a_tight_budget(self):
        """A blind tail keeps test_last and drops test_first. The head must win."""
        got = rp._failure_excerpt(self._two_failures(), 12)
        self.assertIn("FAIL: test_first (test_a.A.test_first)", got)
        self.assertIn("AssertionError: the FIRST failure", got)

    def test_leading_noise_before_the_failure_is_dropped(self):
        """30 lines of pre-failure noise must not consume the window."""
        got = rp._failure_excerpt(self._two_failures(), 12)
        self.assertNotIn("leading noise 0", got)
        self.assertNotIn("leading noise 29", got)

    def test_dropped_leading_noise_is_reported_when_it_fits(self):
        got = rp._failure_excerpt(self._two_failures(), 20)
        self.assertIn("before the first failure omitted", got)

    def test_tail_is_retained_alongside_the_head(self):
        """``FAILED (…)`` is what makes a red run countable; it must survive."""
        got = rp._failure_excerpt(self._two_failures(), 12)
        self.assertIn("FAILED (failures=1, skipped=256)", got)
        self.assertIn("Ran 1874 tests in 96.3s", got)

    def test_head_and_tail_are_both_non_empty_at_every_budget(self):
        text = self._two_failures()
        for n in range(rp.MIN_FAIL_OUTPUT_LINES, 40):
            got = rp._failure_excerpt(text, n)
            self.assertIn("FAIL: test_first (test_a.A.test_first)", got,
                          f"N={n} lost the first failure:\n{got}")
            self.assertIn("FAILED (failures=1, skipped=256)", got,
                          f"N={n} lost the summary:\n{got}")

    def test_anchors_on_a_bare_traceback_when_unittest_never_reported(self):
        """A shard that dies on import has no FAIL: banner — anchor the traceback."""
        text = ("".join(f"noise {i}\n" for i in range(40))
                + "Traceback (most recent call last):\n"
                '  File "/b/tests/test_x.py", line 1, in <module>\n'
                "ImportError: cannot import name 'gone'\n")
        got = rp._failure_excerpt(text, 8)
        self.assertIn("Traceback (most recent call last):", got)
        self.assertIn("ImportError: cannot import name 'gone'", got)
        self.assertNotIn("noise 0", got)


class SocketWarningFilterTest(unittest.TestCase):
    """Strip the shutdown-time socket noise — and nothing else."""

    def test_socket_warnings_are_removed_with_their_echo_and_hint(self):
        text = FAILURE_BLOCK + SUMMARY_BLOCK + socket_warning_flood(20)
        kept, dropped = rp._strip_socket_warnings(text)
        self.assertNotIn("unclosed <socket.socket", kept)
        self.assertNotIn("Enable tracemalloc", kept)
        self.assertNotIn("self._sock = None", kept)
        self.assertEqual(dropped, 60, "header + echo + hint per warning")

    def test_ssl_socket_warnings_are_removed_too(self):
        text = ("/b/tests/test_x.py:484: ResourceWarning: unclosed "
                "<ssl.SSLSocket fd=11, family=2, type=1, proto=6>\n"
                + SUMMARY_BLOCK)
        kept, dropped = rp._strip_socket_warnings(text)
        self.assertNotIn("ssl.SSLSocket", kept)
        self.assertEqual(dropped, 1)

    def test_non_socket_resource_warnings_survive(self):
        """Unclosed files and database connections are REAL leak signal."""
        text = (
            "/b/tests/test_x.py:12: ResourceWarning: unclosed file "
            "<_io.TextIOWrapper name='/tmp/seed.json' mode='r'>\n"
            "/b/tests/test_y.py:33: ResourceWarning: unclosed database in "
            "<sqlite3.Connection object at 0x104>\n"
            + socket_warning_flood(5)
            + SUMMARY_BLOCK
        )
        kept, _ = rp._strip_socket_warnings(text)
        self.assertIn("unclosed file", kept)
        self.assertIn("unclosed database", kept)
        self.assertNotIn("unclosed <socket.socket", kept)

    def test_other_warning_categories_survive(self):
        text = ("/b/tests/test_x.py:9: DeprecationWarning: datetime.utcnow() "
                "is deprecated\n" + socket_warning_flood(3) + SUMMARY_BLOCK)
        kept, _ = rp._strip_socket_warnings(text)
        self.assertIn("DeprecationWarning", kept)

    def test_a_traceback_after_a_stripped_warning_is_not_eaten(self):
        """The echo-line rule drops INDENTED lines; a traceback header is not."""
        text = (SOCKET_WARNING.format(n=1)
                + "Traceback (most recent call last):\n"
                '  File "/b/tests/test_x.py", line 7, in test_x\n'
                "AssertionError: kept\n")
        kept, _ = rp._strip_socket_warnings(text)
        self.assertIn("Traceback (most recent call last):", kept)
        self.assertIn("AssertionError: kept", kept)
        self.assertIn('File "/b/tests/test_x.py", line 7, in test_x', kept)

    def test_a_failure_block_immediately_after_a_warning_survives(self):
        text = socket_warning_flood(4) + FAILURE_BLOCK + SUMMARY_BLOCK
        kept, _ = rp._strip_socket_warnings(text)
        self.assertIn("FAIL: test_roster_locks", kept)
        self.assertIn("AssertionError: 'OPEN' != 'LOCKED'", kept)

    def test_nothing_is_dropped_when_there_is_no_socket_noise(self):
        text = FAILURE_BLOCK + SUMMARY_BLOCK
        kept, dropped = rp._strip_socket_warnings(text)
        self.assertEqual(kept, text)
        self.assertEqual(dropped, 0)


class FailingTestNameTest(unittest.TestCase):
    """The ``FAILING TESTS`` roll-up must name every failure."""

    def test_every_failure_and_error_is_named(self):
        text = (
            "=" * 70 + "\n"
            "FAIL: test_first (test_a.A.test_first)\n"
            "ERROR: test_boom (test_b.B.test_boom)\n"
            "FAIL: test_last (test_z.Z.test_last)\n"
            + SUMMARY_BLOCK
        )
        self.assertEqual(rp._failing_test_names(text), [
            "FAIL: test_a.A.test_first",
            "ERROR: test_b.B.test_boom",
            "FAIL: test_z.Z.test_last",
        ])

    def test_dotted_name_is_preferred_over_the_bare_method(self):
        """``test_a.A.test_first`` locates the test; ``test_first`` does not."""
        names = rp._failing_test_names(
            "FAIL: test_first (test_a.A.test_first)\n")
        self.assertEqual(names, ["FAIL: test_a.A.test_first"])

    def test_setupclass_style_names_without_parentheses_are_kept(self):
        """``ERROR: setUpClass (test_x.X)`` and bare forms both matter."""
        names = rp._failing_test_names(
            "ERROR: setUpClass (test_x.XTest)\n"
            "ERROR: test_bare\n")
        self.assertEqual(names, ["ERROR: test_x.XTest", "ERROR: test_bare"])

    def test_no_names_when_the_shard_died_outside_the_test_run(self):
        text = ("Traceback (most recent call last):\n"
                "ImportError: cannot import name 'gone'\n")
        self.assertEqual(rp._failing_test_names(text), [])

    def test_names_are_not_invented_from_ordinary_output(self):
        self.assertEqual(rp._failing_test_names(
            "this line mentions FAIL: but not at the start\n"
            "Ran 3 tests in 1s\n"), [])


class SummaryLineTest(unittest.TestCase):
    """The progress column reads the verdict, not the last ResourceWarning."""

    def test_summary_is_taken_after_the_socket_noise_is_stripped(self):
        raw = FAILURE_BLOCK + SUMMARY_BLOCK + socket_warning_flood(30)
        self.assertIn("ResourceWarning", rp._summary_line(raw))
        kept, _ = rp._strip_socket_warnings(raw)
        self.assertEqual(rp._summary_line(kept),
                         "FAILED (failures=1, skipped=256)")


class WarningFloodErasureTest(unittest.TestCase):
    """#382 itself: a failure followed by >4,000 bytes of warnings.

    This is the exact shape that erased the evidence on main. It is asserted at
    the pipeline level — the same two calls ``main()`` makes, in the same order.
    """

    def _flooded(self):
        text = (FAILURE_BLOCK + SUMMARY_BLOCK
                + socket_warning_flood(40))     # comfortably over 4,000 bytes
        self.assertGreater(len(socket_warning_flood(40)), 4000)
        return text

    def test_the_old_blind_tail_slice_really_did_erase_the_evidence(self):
        """Control: without this, the test below could pass on an easy input."""
        raw = self._flooded()
        window = raw[-4000:]
        self.assertNotIn("FAIL: test_roster_locks", window)
        self.assertNotIn("Traceback (most recent call last):", window)
        self.assertNotIn("AssertionError", window)

    def test_the_pipeline_keeps_the_name_and_traceback_through_the_flood(self):
        kept, dropped = rp._strip_socket_warnings(self._flooded())
        report = rp._failure_excerpt(kept, 40)
        self.assertEqual(dropped, 120)
        self.assertIn(
            "FAIL: test_roster_locks (test_roster.RosterTest.test_roster_locks)",
            report)
        self.assertIn("Traceback (most recent call last):", report)
        self.assertIn("AssertionError: 'OPEN' != 'LOCKED'", report)
        self.assertIn("FAILED (failures=1, skipped=256)", report)

    def test_the_flood_survives_even_the_minimum_budget(self):
        kept, _ = rp._strip_socket_warnings(self._flooded())
        report = rp._failure_excerpt(kept, rp.MIN_FAIL_OUTPUT_LINES)
        self.assertIn("FAIL: test_roster_locks", report)
        self.assertIn("FAILED (failures=1, skipped=256)", report)


# The synthetic shard for the end-to-end test: it fails an assertion, then
# floods stderr with leaked-socket warnings at interpreter shutdown — the same
# ordering the garbage collector produced on main, where the warnings land
# AFTER unittest has already printed its summary.
SYNTHETIC_SHARD = textwrap.dedent('''
    import atexit
    import sys
    import unittest

    WARNING = (
        "/usr/lib/python3.14/socket.py:{n}: ResourceWarning: unclosed "
        "<socket.socket fd=9, family=2, type=1, proto=6, "
        "laddr=(\\'127.0.0.1\\', 5{n:04d})>\\n"
        "  self._sock = None\\n"
        "ResourceWarning: Enable tracemalloc to get the object allocation "
        "traceback\\n"
    )

    @atexit.register
    def _flood():
        sys.stderr.write("".join(WARNING.format(n=i) for i in range(60)))
        sys.stderr.flush()


    class SyntheticShardTest(unittest.TestCase):
        def test_roster_locks(self):
            self.assertEqual("OPEN", "LOCKED")
''')


class EndToEndRunnerReportTest(unittest.TestCase):
    """Run the real ``run_parallel.py`` over a shard that fails, then floods.

    Everything above tests the helpers in isolation. This one tests the thing
    the repo owner actually reads: the process's stdout.
    """

    def _run(self, *extra):
        workdir = tempfile.mkdtemp(prefix="run_parallel_report_")
        self.addCleanup(shutil.rmtree, workdir, True)
        shutil.copy(rp.__file__, os.path.join(workdir, "run_parallel.py"))
        with open(os.path.join(workdir, "test_synthetic_shard.py"), "w") as fh:
            fh.write(SYNTHETIC_SHARD)
        proc = subprocess.run(
            [sys.executable, "run_parallel.py", "-j", "1", *extra],
            cwd=workdir, capture_output=True, text=True)
        return proc

    def test_report_names_the_failing_test_and_shows_its_traceback(self):
        proc = self._run("--fail-output-lines", "40")
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        report = proc.stdout
        self.assertIn("SyntheticShardTest.test_roster_locks", report)
        self.assertIn("Traceback (most recent call last):", report)
        self.assertIn("AssertionError", report)
        self.assertIn("===== FAILING TESTS =====", report)

    def test_report_is_not_buried_in_socket_warnings(self):
        proc = self._run("--fail-output-lines", "40")
        self.assertNotIn("unclosed <socket.socket", proc.stdout)
        self.assertIn("filtered from this report", proc.stdout)

    def test_progress_line_shows_the_verdict_not_a_warning(self):
        proc = self._run("--fail-output-lines", "40")
        status = [ln for ln in proc.stdout.splitlines() if "[FAIL] shard" in ln]
        self.assertTrue(status, proc.stdout)
        self.assertIn("FAILED (failures=1)", status[0])

    def test_unbounded_default_also_names_the_failing_test(self):
        proc = self._run()
        self.assertIn("SyntheticShardTest.test_roster_locks", proc.stdout)
        self.assertIn("AssertionError", proc.stdout)

    def test_cli_rejects_a_budget_below_the_minimum(self):
        proc = self._run("--fail-output-lines", "1")
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
        self.assertIn("--fail-output-lines", proc.stderr)
        self.assertIn(str(rp.MIN_FAIL_OUTPUT_LINES), proc.stderr)


class ListeningSocketLeakTest(unittest.TestCase):
    """The other half of #382: stop producing the noise in the first place.

    Filtering ``unclosed <socket …>`` out of the report treats the symptom. The
    cause is that ``BaseServer.shutdown()`` only stops the ``serve_forever``
    loop — it does NOT close the listening socket; ``server_close()`` does. 80
    of the suite's 88 HTTP fixtures called the first and not the second, so 80
    listening sockets survived to be reported by the garbage collector at
    interpreter shutdown, which is what buried the assertion on main.

    This is a structural guard rather than a runtime one because the warnings
    are emitted during interpreter finalisation, after any assertion in this
    process could observe them.
    """

    def _fixture_sites(self):
        """``(module, line_no, receiver)`` for every server shutdown in tests/."""
        here = os.path.dirname(os.path.abspath(__file__))
        sites = []
        for name in sorted(os.listdir(here)):
            if not (name.startswith("test_") and name.endswith(".py")):
                continue
            with open(os.path.join(here, name)) as fh:
                lines = fh.read().splitlines()
            for i, line in enumerate(lines):
                stripped = line.strip()
                if stripped.endswith(".shutdown()"):
                    sites.append((name, i + 1,
                                  stripped[:-len(".shutdown()")], lines))
        return sites

    def test_the_suite_has_http_fixtures_to_check(self):
        """Control: the scan below is worthless if it finds nothing."""
        self.assertGreater(len(self._fixture_sites()), 50)

    def test_every_shut_down_server_is_also_closed(self):
        """``shutdown()`` stops the loop; only ``server_close()`` frees the fd."""
        unclosed = []
        for name, line_no, recv, lines in self._fixture_sites():
            window = "\n".join(lines[line_no - 1:line_no + 6])
            if f"{recv}.server_close()" not in window:
                unclosed.append(f"{name}:{line_no} ({recv})")
        self.assertEqual(
            unclosed, [],
            "these fixtures stop their HTTP server but never release its "
            "listening socket, so the garbage collector reports it as "
            "'ResourceWarning: unclosed <socket …>' at interpreter shutdown "
            "— the noise that erased the real assertion in #382:\n  "
            + "\n  ".join(unclosed))


if __name__ == "__main__":
    unittest.main()
