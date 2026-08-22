"""#205: the VERSION-INDEPENDENT half of #426's leak contract.

``test_resource_warning_leak.py`` proves the #426 fix by provoking a
``ResourceWarning``. That works on this development machine (CPython 3.14) and
does NOT work on the interpreter CI runs (3.11): an abandoned
``urllib.error.HTTPError`` emits no ``ResourceWarning`` there at all --
measured, not assumed; see :mod:`httperror_close_check`'s docstring for the
four-cell measurement and
``test_resource_warning_leak.LeakProbeRegressionTest`` for the runtime
capability probe that makes that half of the contract announce itself as
unenforceable rather than silently pass.

This module is the replacement protection, and it does not depend on the
interpreter at all: an unclosed ``HTTPError`` is a property of the SOURCE, so
it is checked by parsing the source. Two halves:

* the repository contract -- every tracked ``.py`` file, checked;
* the checker's own contract -- each classification arm exercised on a
  synthetic fixture through :func:`httperror_close_check.scan_source`, so no
  arm's correctness rests on a real file happening to be shaped that way.

The single most important case here is
:meth:`ReverseTheFixTest.test_reverting_the_426_fix_is_caught`: it takes the
REAL ``test_sensitive_read_audit_http.py`` source, undoes the ``with e:`` fix
in memory, and requires the checker to report it. That makes the guard's
falsifiability a permanent, automated property rather than something someone
demonstrated once by hand.
"""

import unittest

from helpers import BACKEND  # noqa: F401  (ensures sys.path is set up)

import httperror_close_check as guard

# The #426 site itself. Named here so the anti-vacuity test below asserts on a
# handler this suite KNOWS exists and knows the disposition of, rather than on
# a count alone.
_FIXED_426_PATH = "hockey-scheduler/backend/tests/test_sensitive_read_audit_http.py"
_FIXED_426_KEY = (_FIXED_426_PATH, "SensitiveReadHttpContract._req", 0)

# A floor, not the exact count: the exact count changes whenever anyone adds
# an HTTP helper, which is not a reason to fail. What this defends against is
# the scan silently collapsing to nothing -- an import error, a renamed
# ``git ls-files`` argument, a resolver that stops recognising
# ``urllib.error.HTTPError`` -- which would make every assertion below
# vacuously true. 100 was 144 when this was written.
_MINIMUM_HANDLERS = 100


class RepositoryHttpErrorCloseTest(unittest.TestCase):
    """The contract itself, over every tracked ``.py`` file."""

    maxDiff = None

    @classmethod
    def setUpClass(cls):
        cls.handlers = guard.scan_repository()

    def test_no_uncatalogued_httperror_handler_leaves_a_response_unclosed(self):
        violating = guard.violations(self.handlers)
        self.assertEqual(violating, [], "\n" + guard.report(violating)
                         if violating else "")

    def test_the_ledger_still_names_exactly_what_it_was_written_for(self):
        """A ledger entry that names a vanished handler, or one that has since
        been fixed, must be REMOVED -- the list may only ever shrink. See
        ``_KNOWN_UNCLOSED``'s own comment."""
        guard.verify_ledger(self.handlers)

    def test_the_scan_really_reaches_this_repository_s_http_helpers(self):
        """Anti-vacuity. Every assertion in this module is about a set of
        findings; a scan that found nothing would satisfy all of them."""
        self.assertGreaterEqual(
            len(self.handlers), _MINIMUM_HANDLERS,
            f"the scan found only {len(self.handlers)} HTTPError-binding "
            f"handlers; it used to find 144, so something has stopped the "
            f"walk reaching this repository's HTTP helpers and every other "
            f"assertion here is now vacuous")
        by_key = {h.key: h for h in self.handlers}
        self.assertIn(
            _FIXED_426_KEY, by_key,
            "the #426 handler itself is not in the scan -- the guard is not "
            "looking at the file the whole contract came from")
        self.assertEqual(
            by_key[_FIXED_426_KEY].disposition, guard.CLOSED,
            "the #426 handler must be seen as CLOSED by the guard")

    def test_the_426_handler_is_not_in_the_pre_existing_debt_ledger(self):
        """It was FIXED, so it is held to the contract. If it were catalogued
        instead, reverting the fix would pass -- which is exactly the failure
        this whole module exists to prevent."""
        self.assertNotIn(_FIXED_426_KEY, guard._KNOWN_UNCLOSED)

    def test_every_finding_carries_a_disposition_the_module_defines(self):
        allowed = {guard.CLOSED, guard.RERAISED, guard.LEAKED}
        seen = {h.disposition for h in self.handlers}
        self.assertLessEqual(seen, allowed, sorted(seen))


class ReverseTheFixTest(unittest.TestCase):
    """Falsifiability, permanently automated."""

    maxDiff = None

    # The #426 fix, and what the code looked like before it. Both are matched
    # EXACTLY against the live file: if either drifts, this test fails loudly
    # instead of quietly reverting nothing and asserting on an unchanged file.
    _FIXED = (
        "        except urllib.error.HTTPError as e:\n"
        "            with e:\n"
        "                raw = e.read()\n"
        "                return e.code, (json.loads(raw) if raw else {}), "
        "dict(e.headers)\n")
    _REVERTED = (
        "        except urllib.error.HTTPError as e:\n"
        "            raw = e.read()\n"
        "            return e.code, (json.loads(raw) if raw else {}), "
        "dict(e.headers)\n")

    def test_reverting_the_426_fix_is_caught(self):
        path = guard.REPO_ROOT / _FIXED_426_PATH
        source = path.read_text()
        self.assertIn(
            self._FIXED, source,
            "the #426 fix is no longer spelled the way this test knows how to "
            "revert -- update _FIXED/_REVERTED so the guard keeps being "
            "proved falsifiable against the real file")

        clean = guard.scan_source(source, _FIXED_426_PATH)
        self.assertEqual(
            [h.key for h in guard.violations(clean)], [],
            "the real file must be clean before the revert means anything")

        broken = guard.scan_source(source.replace(self._FIXED, self._REVERTED),
                                   _FIXED_426_PATH)
        reported = guard.violations(broken)
        self.assertEqual(
            [h.key for h in reported], [_FIXED_426_KEY],
            "reverting `with e:` in _req() must make the guard fire, and fire "
            f"on THAT handler: {[h.describe() for h in reported]}")
        self.assertEqual(reported[0].disposition, guard.LEAKED)
        self.assertIn("never closed", reported[0].evidence)


class ClassificationTest(unittest.TestCase):
    """One synthetic fixture per arm of the checker, so no arm's correctness
    depends on a real file happening to be shaped that way."""

    maxDiff = None

    _PROLOGUE = "import contextlib\nimport urllib.error\nimport urllib.request\n"

    def _scan(self, body, prologue=None):
        return guard.scan_source((prologue or self._PROLOGUE) + body,
                                 "<fixture>")

    def _one(self, body, prologue=None):
        found = self._scan(body, prologue)
        self.assertEqual(len(found), 1, [h.describe() for h in found])
        return found[0]

    # -- closing idioms ----------------------------------------------------
    def test_with_e_closes(self):
        handler = self._one(
            "def f(u):\n"
            "    try:\n"
            "        return urllib.request.urlopen(u)\n"
            "    except urllib.error.HTTPError as e:\n"
            "        with e:\n"
            "            return e.read()\n")
        self.assertEqual(handler.disposition, guard.CLOSED)
        self.assertEqual(handler.evidence, "with e:")
        self.assertEqual(handler.qualname, "f")

    def test_explicit_close_closes(self):
        handler = self._one(
            "def f(u):\n"
            "    try:\n"
            "        return urllib.request.urlopen(u)\n"
            "    except urllib.error.HTTPError as e:\n"
            "        raw = e.read()\n"
            "        e.close()\n"
            "        return raw\n")
        self.assertEqual(handler.disposition, guard.CLOSED)
        self.assertEqual(handler.evidence, "e.close()")

    def test_try_finally_close_closes(self):
        handler = self._one(
            "def f(u):\n"
            "    try:\n"
            "        return urllib.request.urlopen(u)\n"
            "    except urllib.error.HTTPError as e:\n"
            "        try:\n"
            "            return e.read()\n"
            "        finally:\n"
            "            e.close()\n")
        self.assertEqual(handler.disposition, guard.CLOSED)

    def test_contextlib_closing_closes(self):
        handler = self._one(
            "def f(u):\n"
            "    try:\n"
            "        return urllib.request.urlopen(u)\n"
            "    except urllib.error.HTTPError as e:\n"
            "        with contextlib.closing(e) as r:\n"
            "            return r.read()\n")
        self.assertEqual(handler.disposition, guard.CLOSED)

    def test_exit_stack_handoff_closes(self):
        handler = self._one(
            "def f(u, stack):\n"
            "    try:\n"
            "        return urllib.request.urlopen(u)\n"
            "    except urllib.error.HTTPError as e:\n"
            "        stack.enter_context(e)\n"
            "        return e.read()\n")
        self.assertEqual(handler.disposition, guard.CLOSED)

    # -- the leak itself ---------------------------------------------------
    def test_the_426_shape_leaks(self):
        handler = self._one(
            "def f(u):\n"
            "    try:\n"
            "        return urllib.request.urlopen(u)\n"
            "    except urllib.error.HTTPError as e:\n"
            "        return e.code, e.read()\n")
        self.assertEqual(handler.disposition, guard.LEAKED)
        self.assertEqual(handler.evidence, "e is never closed")

    def test_never_reading_the_body_still_leaks(self):
        """Measured: on 3.14 an HTTPError that is never read warns exactly as
        one that is read does. Not reading it is not a defence."""
        handler = self._one(
            "def f(u):\n"
            "    try:\n"
            "        return urllib.request.urlopen(u)\n"
            "    except urllib.error.HTTPError as e:\n"
            "        return e.code\n")
        self.assertEqual(handler.disposition, guard.LEAKED)

    def test_an_unbound_handler_leaks(self):
        """``except HTTPError:`` with no ``as`` throws the handle away: the
        response object still exists and can now only be reclaimed by the
        garbage collector."""
        handler = self._one(
            "def f(u):\n"
            "    try:\n"
            "        return urllib.request.urlopen(u)\n"
            "    except urllib.error.HTTPError:\n"
            "        return None\n")
        self.assertEqual(handler.disposition, guard.LEAKED)
        self.assertIsNone(handler.name)
        self.assertIn("never bound", handler.evidence)

    def test_closing_the_wrong_name_does_not_count(self):
        handler = self._one(
            "def f(u, other):\n"
            "    try:\n"
            "        return urllib.request.urlopen(u)\n"
            "    except urllib.error.HTTPError as e:\n"
            "        other.close()\n"
            "        return e.read()\n")
        self.assertEqual(handler.disposition, guard.LEAKED)

    # -- the re-raise exemption, and its limits ----------------------------
    def test_a_handler_that_always_reraises_is_exempt(self):
        handler = self._one(
            "def f(u):\n"
            "    try:\n"
            "        return urllib.request.urlopen(u)\n"
            "    except urllib.error.HTTPError as e:\n"
            "        log(e.code)\n"
            "        raise\n")
        self.assertEqual(handler.disposition, guard.RERAISED)
        self.assertEqual(guard.violations([handler]), [])

    def test_reraising_on_both_arms_of_an_if_is_exempt(self):
        handler = self._one(
            "def f(u):\n"
            "    try:\n"
            "        return urllib.request.urlopen(u)\n"
            "    except urllib.error.HTTPError as e:\n"
            "        if e.code == 404:\n"
            "            raise LookupError(e.code) from e\n"
            "        else:\n"
            "            raise\n")
        self.assertEqual(handler.disposition, guard.RERAISED)

    def test_reraising_on_only_one_arm_is_NOT_exempt(self):
        """The false-positive guard cuts both ways: an exemption that fired
        for a handler with a non-raising path would wave through a real
        leak."""
        handler = self._one(
            "def f(u):\n"
            "    try:\n"
            "        return urllib.request.urlopen(u)\n"
            "    except urllib.error.HTTPError as e:\n"
            "        if e.code == 404:\n"
            "            return None\n"
            "        raise\n")
        self.assertEqual(handler.disposition, guard.LEAKED)

    def test_a_return_inside_a_nested_function_does_not_disqualify(self):
        """The exit scan must not descend into a nested ``def``: that
        ``return`` leaves the callback, not the handler."""
        handler = self._one(
            "def f(u):\n"
            "    try:\n"
            "        return urllib.request.urlopen(u)\n"
            "    except urllib.error.HTTPError as e:\n"
            "        def describe():\n"
            "            return e.code\n"
            "        log(describe)\n"
            "        raise\n")
        self.assertEqual(handler.disposition, guard.RERAISED)

    def test_a_raise_inside_a_loop_is_NOT_exempt(self):
        """A loop may run zero times, so its body proves nothing about the
        handler's exit paths."""
        handler = self._one(
            "def f(u, attempts):\n"
            "    try:\n"
            "        return urllib.request.urlopen(u)\n"
            "    except urllib.error.HTTPError as e:\n"
            "        for _ in attempts:\n"
            "            raise\n")
        self.assertEqual(handler.disposition, guard.LEAKED)

    # -- name resolution ---------------------------------------------------
    def test_a_from_import_alias_is_resolved(self):
        handler = self._one(
            "def f(u):\n"
            "    try:\n"
            "        return urlopen(u)\n"
            "    except HE as e:\n"
            "        return e.read()\n",
            prologue="from urllib.error import HTTPError as HE\n"
                     "from urllib.request import urlopen\n")
        self.assertEqual(handler.disposition, guard.LEAKED)
        self.assertEqual(handler.types, ("HE",))

    def test_a_module_alias_is_resolved(self):
        handler = self._one(
            "def f(u):\n"
            "    try:\n"
            "        return urllib.request.urlopen(u)\n"
            "    except ue.HTTPError as e:\n"
            "        return e.read()\n",
            prologue="import urllib.error as ue\nimport urllib.request\n")
        self.assertEqual(handler.disposition, guard.LEAKED)

    def test_a_tuple_handler_mentioning_httperror_counts(self):
        handler = self._one(
            "def f(u):\n"
            "    try:\n"
            "        return urllib.request.urlopen(u)\n"
            "    except (ValueError, urllib.error.HTTPError) as e:\n"
            "        return e\n")
        self.assertEqual(handler.disposition, guard.LEAKED)

    def test_an_unrelated_handler_is_not_reported(self):
        self.assertEqual(
            self._scan(
                "def f(u):\n"
                "    try:\n"
                "        return urllib.request.urlopen(u)\n"
                "    except ValueError as e:\n"
                "        return e\n"), [])

    # -- URLError reachability --------------------------------------------
    def test_a_urlerror_handler_below_an_httperror_handler_is_shielded(self):
        """Python matches handlers in order, so this ``URLError`` arm can
        never receive an ``HTTPError``. This is the shape the repository's
        transport-error arms actually use; treating it as a violation would
        be a dozen false positives."""
        found = self._scan(
            "def f(u):\n"
            "    try:\n"
            "        return urllib.request.urlopen(u)\n"
            "    except urllib.error.HTTPError as e:\n"
            "        with e:\n"
            "            return e.read()\n"
            "    except urllib.error.URLError as e:\n"
            "        return repr(e)\n")
        self.assertEqual([h.types for h in found],
                         [("urllib.error.HTTPError",)])

    def test_a_lone_urlerror_handler_is_reachable_and_checked(self):
        """With no ``HTTPError`` arm above it, this one really does receive
        them -- so rewriting a leaking handler as ``except URLError`` does not
        escape the guard."""
        handler = self._one(
            "def f(u):\n"
            "    try:\n"
            "        return urllib.request.urlopen(u)\n"
            "    except urllib.error.URLError as e:\n"
            "        return getattr(e, 'code', 0)\n")
        self.assertEqual(handler.disposition, guard.LEAKED)
        self.assertEqual(handler.types, ("urllib.error.URLError",))

    def test_the_broad_supertypes_are_deliberately_out_of_scope(self):
        """``except Exception as e:`` can bind an ``HTTPError`` and is still
        not reported -- see :data:`httperror_close_check._SUPERTYPE_NAMES`.
        Asserted rather than left implicit, because it is the checker's
        stated LIMIT: were it ever widened, the suite's hundreds of broad
        handlers would all become violations at once, and whoever did it
        should see that decision fail here first."""
        for name in guard._SUPERTYPE_NAMES:
            with self.subTest(supertype=name):
                self.assertEqual(
                    self._scan(
                        "def f(u):\n"
                        "    try:\n"
                        "        return urllib.request.urlopen(u)\n"
                        f"    except {name} as e:\n"
                        "        return e\n"), [])

    # -- keys --------------------------------------------------------------
    def test_the_key_is_scope_based_and_survives_added_lines(self):
        """The ledger key must not move when an unrelated line is inserted
        above the handler -- otherwise every edit churns the ledger."""
        body = ("def f(u):\n"
                "    try:\n"
                "        return urllib.request.urlopen(u)\n"
                "    except urllib.error.HTTPError as e:\n"
                "        return e.read()\n")
        first = self._one(body)
        shifted = self._one("x = 1\ny = 2\n" + body)
        self.assertEqual(first.key, shifted.key)
        self.assertNotEqual(first.lineno, shifted.lineno)

    def test_two_handlers_in_one_scope_get_distinct_keys(self):
        found = self._scan(
            "class C:\n"
            "    def m(self, u):\n"
            "        try:\n"
            "            return urllib.request.urlopen(u)\n"
            "        except urllib.error.HTTPError as e:\n"
            "            return e.read()\n"
            "        finally:\n"
            "            pass\n"
            "    def n(self, u):\n"
            "        try:\n"
            "            return urllib.request.urlopen(u)\n"
            "        except urllib.error.HTTPError as e:\n"
            "            return e.read()\n")
        self.assertEqual([h.key for h in found],
                         [("<fixture>", "C.m", 0), ("<fixture>", "C.n", 0)])

    # -- fail-closed -------------------------------------------------------
    def test_unparseable_source_raises_rather_than_being_skipped(self):
        with self.assertRaises(guard.HttpErrorCloseError) as caught:
            guard.scan_source("def (:\n", "<broken>")
        self.assertIn("never skipped", str(caught.exception))


class LedgerMechanicsTest(unittest.TestCase):
    """The ledger's own fail-closed behaviour, on synthetic findings -- the
    real ledger is (and must stay) consistent, so its failure modes cannot be
    demonstrated against it."""

    maxDiff = None

    def _handler(self, key, disposition):
        return guard.Handler(key[0], key[1], key[2], 1, "e",
                             ("urllib.error.HTTPError",), disposition, "x")

    def test_a_catalogued_leak_is_not_reported_as_a_violation(self):
        key = next(iter(guard._KNOWN_UNCLOSED))
        self.assertEqual(
            guard.violations([self._handler(key, guard.LEAKED)]), [])

    def test_an_uncatalogued_leak_in_the_same_file_IS_reported(self):
        path = next(iter(guard._KNOWN_UNCLOSED))[0]
        key = (path, "SomeBrandNewHelper._req", 0)
        self.assertNotIn(key, guard._KNOWN_UNCLOSED)
        self.assertEqual(
            [h.key for h in guard.violations([self._handler(key,
                                                            guard.LEAKED)])],
            [key])

    def test_a_dormant_entry_is_an_error(self):
        """Nothing in the scan matches the entry any more."""
        with self.assertRaises(guard.HttpErrorCloseError) as caught:
            guard.verify_ledger([])
        self.assertIn("DORMANT", str(caught.exception))

    def test_an_already_fixed_entry_is_an_error(self):
        """The ledger may only shrink: once a catalogued handler closes, its
        entry must be deleted rather than left behind."""
        scan = [self._handler(key, guard.CLOSED)
                for key in guard._KNOWN_UNCLOSED]
        with self.assertRaises(guard.HttpErrorCloseError) as caught:
            guard.verify_ledger(scan)
        self.assertIn("ALREADY FIXED", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
