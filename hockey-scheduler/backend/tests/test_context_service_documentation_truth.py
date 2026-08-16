"""round-N+3 finding 2: a standing, GREP-BASED tripwire against
``services/context_service.py`` (and the wider backend package, for
defense in depth) ever again POSITIVELY CLAIMING that ``ContextService.
run_scoped_read`` exists. It never has: an early revision of the #159
review finding 3 fix built it, measured it to deadlock a real test harness
(wrapping the epoch comparison and a dependent, unbounded service call in
one shared ``store.transaction()`` holds the store's own process-wide lock
across whatever that call does), and reverted it in favor of the GATE
mechanism that actually shipped (``web/server.py``'s
``Handler._read_under_context_gate`` plus PR #423's PostgreSQL
``epoch_fence`` advisory locks) — see ``context_service.py``'s own NOTE on
#159 review finding 3, and ``context_epoch.py``'s module docstring, for the
full account.

``context_service.py:581`` (in :meth:`_epoch_material_locked`'s docstring)
named ``run_scoped_read`` as a real, currently-callable cross-reference
anyway — a stale claim that survived a full review round before round-N+3
review finding 2 corrected it. This file exists so that specific mistake
cannot silently recur.

THE DISTINGUISHING SIGNAL, precisely — why this is not a bare substring
check. This codebase's own docstring convention uses the Sphinx
``:meth:`name``` role SPECIFICALLY to cross-reference a live, callable
member: every other such reference throughout ``context_service.py`` (its
own docstrings name real, currently-defined siblings this way) points at a
method that genuinely exists. A name that does NOT exist is, by this same
codebase's own convention, mentioned in plain double backticks instead —
exactly how both of today's CORRECT mentions read: ``context_epoch.py``'s
"There is no ``ContextService.run_scoped_read``" and ``context_service.
py``'s own NOTE ("an earlier revision of this fix added a `run_scoped_
read` here"). Wrapping the name in the ``:meth:`` role is therefore the
precise, mechanical signature of a POSITIVE claim that it is a real,
callable member — exactly the shape the stale sentence had, and exactly
what this test refuses to let back in.

A bare ``"run_scoped_read" not in text`` assertion would be WRONG: the
correct, intentional documentation legitimately NAMES the method, in order
to say it does not exist. Failing on the bare substring would break that
legitimate correction the moment it was written — punishing the very fix
this test exists to protect — so the gate is the ``:meth:`` role, not the
name alone.
"""

import re
import unittest
from pathlib import Path

_PKG = Path(__file__).resolve().parents[1] / "hockey_scheduler"
_CONTEXT_SERVICE = _PKG / "services" / "context_service.py"

# The exact Sphinx cross-reference shape that makes a name a POSITIVE claim
# of a real, callable member in this codebase's own docstring convention.
# `+` on the backtick classes tolerates a stray doubled backtick without
# weakening the check — RST's own `:meth:` role syntax is single-backtick.
_METHOD_CLAIM = re.compile(r":meth:`+run_scoped_read`+")


class RunScopedReadNeverPositivelyClaimedTest(unittest.TestCase):
    """#159 review finding 3 / round-N+3 review finding 2: a STANDING
    regression guard, not a one-time verification — it fails loudly the
    moment either this module's or the wider backend's documentation
    claims ``ContextService.run_scoped_read`` exists again."""

    def test_context_service_never_meth_references_run_scoped_read(self):
        text = _CONTEXT_SERVICE.read_text()
        hits = _METHOD_CLAIM.findall(text)
        self.assertEqual(
            hits, [],
            f"{_CONTEXT_SERVICE} positively claims (via a `:meth:` "
            f"cross-reference) that ContextService.run_scoped_read exists "
            f"— it does not and never has; see that class's own NOTE on "
            f"#159 review finding 3. Found: {hits!r}")

    def test_run_scoped_read_is_never_meth_referenced_anywhere_in_the_backend(self):
        """Defense in depth beyond "this module" (the review's own
        phrasing): the identical mistake could just as easily land in a
        sibling file (``context_epoch.py``, the HTTP layer, a future
        design note under ``hockey_scheduler``). Sweeps the whole backend
        package for the same positive-claim shape, not only the one file
        the stale sentence happened to be in."""
        offenders = {}
        for path in _PKG.rglob("*.py"):
            hits = _METHOD_CLAIM.findall(path.read_text())
            if hits:
                offenders[str(path.relative_to(_PKG))] = hits
        self.assertEqual(
            offenders, {},
            f"one or more files positively claim (via `:meth:`) that "
            f"ContextService.run_scoped_read exists: {offenders}")

    def test_the_currently_correct_mentions_still_use_the_non_claim_shape(self):
        """Falsifiability backstop for the two tests above. If this module
        stopped mentioning ``run_scoped_read`` altogether (e.g. someone
        deleted the historical NOTE instead of merely not re-claiming the
        method), the checks above would keep passing but stop proving
        anything — a silent, vacuous pass is exactly the failure mode a
        tripwire must not have. Assert the name is still discussed, with
        the exact plain-backtick, explicitly-non-existent shape today's
        corrected text carries."""
        text = _CONTEXT_SERVICE.read_text()
        self.assertIn(
            "run_scoped_read", text,
            f"{_CONTEXT_SERVICE} no longer mentions run_scoped_read at "
            f"all — the two tests above would now pass VACUOUSLY rather "
            f"than actively guarding anything; the historical NOTE "
            f"explaining why it was proposed and rejected should stay")
        self.assertIn(
            "NO SUCH METHOD EXISTS", text,
            f"{_CONTEXT_SERVICE}'s corrected docstring no longer states "
            f"plainly that run_scoped_read does not exist")


if __name__ == "__main__":
    unittest.main()
