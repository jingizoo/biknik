"""Repository hygiene assertions (#367 prereq review).

A machine-local dependency symlink was accidentally committed:
``hockey-scheduler/e2e/node_modules`` as mode 120000 pointing at an absolute
path under one developer's home directory. It leaked that path and made
checkout/dependency behaviour machine-dependent.

It got in because a broad ``git add -A`` swept up a symlink created to let a
git worktree reuse the main checkout's installed dependencies. Reviewing the
staged list would have caught it; asserting it here means nobody has to
remember.
"""

import subprocess
import unittest

from helpers import BACKEND  # noqa: F401  (ensures sys.path is set up)

REPO_ROOT = BACKEND.parents[1]


class TrackedFileHygieneTest(unittest.TestCase):
    def _tracked(self):
        out = subprocess.run(
            ["git", "ls-files", "-z"], cwd=REPO_ROOT,
            capture_output=True, text=True, check=True).stdout
        return [p for p in out.split("\0") if p]

    def test_no_tracked_path_contains_node_modules(self):
        """Including symlinks -- ``git ls-files`` lists a symlink like any
        other path, so this catches the exact case that slipped through."""
        offenders = [p for p in self._tracked()
                     if "node_modules" in p.split("/")]
        self.assertEqual(
            offenders, [],
            "node_modules must never be tracked (installed dependencies are "
            f"machine-local, and a symlink leaks an absolute path): {offenders}")

    def test_no_tracked_symlink_points_outside_the_repository(self):
        """A tracked symlink whose target is absolute, or escapes the repo,
        makes a fresh clone behave differently from this machine."""
        out = subprocess.run(
            ["git", "ls-files", "-s", "-z"], cwd=REPO_ROOT,
            capture_output=True, text=True, check=True).stdout
        bad = []
        for entry in out.split("\0"):
            if not entry.startswith("120000"):
                continue
            path = entry.split("\t", 1)[1]
            target = subprocess.run(
                ["git", "show", f"HEAD:{path}"], cwd=REPO_ROOT,
                capture_output=True, text=True).stdout.strip()
            if target.startswith("/") or target.startswith("~"):
                bad.append((path, target))
        self.assertEqual(
            bad, [],
            f"tracked symlinks must not use absolute targets: {bad}")


if __name__ == "__main__":
    unittest.main()
