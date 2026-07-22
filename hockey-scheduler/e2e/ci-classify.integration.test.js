// Integration regressions for the CI classifier's PRE-classifier extraction —
// the parts the pure unit tests (ci-classify.test.js) cannot cover, because
// they depend on the exact `git diff` the workflow runs. Real temp git repos,
// no deps. Run: node ci-classify.integration.test.js
//
// Covers the two extraction gaps a green full-matrix run cannot prove:
//   A. a cross-category RENAME must not vanish into its destination — moving a
//      backend/API file into a docs path must still classify as a backend
//      change (`--no-renames` surfaces the deletion), not docs-only;
//   B. a merge-base (three-dot) diff must exclude changes the base branch
//      advanced with after the fork (two-dot pulls them in and can force a
//      wrong full matrix);
// plus C. a guard that the workflow trigger does NOT path-filter (which would
//      re-introduce the fail-open bypass this PR removes).

"use strict";
const assert = require("assert");
const fs = require("fs");
const os = require("os");
const path = require("path");
const { execFileSync } = require("child_process");
const { classify } = require("./ci-classify.js");

const API = "hockey-scheduler/backend/hockey_scheduler/api/service.py";
const DOCS = "hockey-scheduler/docs/service.py";
const MODEL = "hockey-scheduler/backend/hockey_scheduler/services/roster.py";

function git(repo, args) {
  return execFileSync("git", args, { cwd: repo, encoding: "utf8" });
}
function mkrepo() {
  const repo = fs.mkdtempSync(path.join(os.tmpdir(), "ci-classify-"));
  git(repo, ["init", "-q"]);
  git(repo, ["config", "user.email", "t@example.com"]);
  git(repo, ["config", "user.name", "t"]);
  git(repo, ["config", "commit.gpgsign", "false"]);
  return repo;
}
function write(repo, rel, body) {
  const p = path.join(repo, rel);
  fs.mkdirSync(path.dirname(p), { recursive: true });
  fs.writeFileSync(p, body);
}
function rev(repo) { return git(repo, ["rev-parse", "HEAD"]).trim(); }
// The exact extraction the workflow uses.
function diffFiles(repo, base, head, noRenames) {
  const args = ["diff", "--name-only"];
  if (noRenames) args.push("--no-renames");
  args.push(`${base}...${head}`);
  return git(repo, args).split("\n").map((s) => s.trim()).filter(Boolean);
}
const isFull = (r) => r.test && r.postgres && r.frontend_check && r.browser_smoke;
const isDB = (r) => r.test && r.postgres && !r.frontend_check && !r.browser_smoke;
const isNone = (r) => !r.test && !r.postgres && !r.frontend_check && !r.browser_smoke;

let passed = 0;
const cleanup = [];
try {
  // --- A. cross-category rename (backend/API -> docs) ----------------------
  {
    const repo = mkrepo(); cleanup.push(repo);
    write(repo, API, "def handler():\n    return 1\n");
    git(repo, ["add", "-A"]); git(repo, ["commit", "-qm", "base"]);
    const base = rev(repo);
    fs.mkdirSync(path.dirname(path.join(repo, DOCS)), { recursive: true });
    git(repo, ["mv", API, DOCS]);
    git(repo, ["commit", "-qm", "move api into docs"]);
    const head = rev(repo);

    // With git's DEFAULT rename detection, --name-only reports only the
    // destination — so the classifier would see docs-only and skip every gate.
    const withRenames = diffFiles(repo, base, head, false);
    assert.ok(!withRenames.includes(API),
      "sanity: default rename detection hides the deleted API path");
    assert.ok(isNone(classify(withRenames, "pull_request")),
      "sanity: the rename-detected diff misclassifies as docs-only (the gap)");

    // The fix: --no-renames surfaces BOTH sides, so the deleted API surface is
    // seen and the change runs the full matrix (backend_api).
    const noRenames = diffFiles(repo, base, head, true);
    assert.ok(noRenames.includes(API),
      "fix: --no-renames surfaces the deleted API path");
    assert.ok(noRenames.includes(DOCS), "fix: --no-renames also has the destination");
    assert.ok(isFull(classify(noRenames, "pull_request")),
      "fix: a backend->docs rename runs the full matrix, not docs-only");
    passed += 1;
    console.log("  ok  A. cross-category rename surfaces the deleted backend surface -> full matrix");
  }

  // --- B. merge-base (three-dot) excludes base-only advances ---------------
  {
    const repo = mkrepo(); cleanup.push(repo);
    write(repo, MODEL, "v1\n");
    git(repo, ["add", "-A"]); git(repo, ["commit", "-qm", "A"]);
    git(repo, ["branch", "-M", "main"]);
    // feature forks at A and makes a hockey model-only change.
    git(repo, ["checkout", "-q", "-b", "feature"]);
    write(repo, MODEL, "v2\n");
    git(repo, ["add", "-A"]); git(repo, ["commit", "-qm", "C hockey change"]);
    const head = rev(repo);
    // base (main) advances AFTER the fork with an unrelated, classification-
    // changing file (an unknown path).
    git(repo, ["checkout", "-q", "main"]);
    write(repo, "Makefile", "all:\n\techo hi\n");
    git(repo, ["add", "-A"]); git(repo, ["commit", "-qm", "B base advance"]);
    const base = rev(repo);

    // Two-dot pulls the base-only Makefile into the set -> wrongly forces full.
    const twoDot = git(repo, ["diff", "--name-only", "--no-renames", `${base}`, `${head}`])
      .split("\n").map((s) => s.trim()).filter(Boolean);
    assert.ok(twoDot.includes("Makefile"),
      "two-dot diff pulls in the base-only change");
    assert.ok(isFull(classify(twoDot, "pull_request")),
      "two-dot would wrongly force the full matrix from a base-only unknown file");

    // Three-dot (merge-base) sees only the PR's own hockey change -> DB matrix.
    const threeDot = diffFiles(repo, base, head, true);
    assert.ok(!threeDot.includes("Makefile"),
      "three-dot (merge-base) excludes the base-only change");
    assert.deepStrictEqual(threeDot, [MODEL], "three-dot is exactly the PR's change");
    assert.ok(isDB(classify(threeDot, "pull_request")),
      "three-dot classifies the PR as a backend model change -> DB matrix");
    passed += 1;
    console.log("  ok  B. merge-base (three-dot) diff excludes base-only advances");
  }

  // --- C. the workflow trigger must NOT path-filter (fail-open guard) -------
  {
    const wfPath = path.resolve(__dirname, "..", "..", ".github", "workflows", "hockey-scheduler-ci.yml");
    const wf = fs.readFileSync(wfPath, "utf8");
    const onStart = wf.indexOf("\non:");
    const jobsStart = wf.indexOf("\njobs:");
    assert.ok(onStart >= 0 && jobsStart > onStart, "workflow has an on: block before jobs:");
    const onBlock = wf.slice(onStart, jobsStart);
    assert.ok(!/\n\s+paths:/.test(onBlock),
      "the trigger must not use paths: (that fail-open bypass is what this PR removes)");
    assert.ok(!/\n\s+paths-ignore:/.test(onBlock),
      "the trigger must not use paths-ignore: either");
    // And the changes job must use the merge-base, --no-renames extraction.
    assert.ok(/--no-renames/.test(wf), "the changes job pipes a --no-renames diff");
    assert.ok(/\.\.\.\$\{?HEAD_SHA\}?|\$\{?BASE_SHA\}?\.\.\.|BASE_SHA\.\.\.HEAD_SHA/.test(wf)
      || /\$BASE_SHA\.\.\.\$HEAD_SHA/.test(wf),
      "the changes job uses a three-dot merge-base diff");
    passed += 1;
    console.log("  ok  C. workflow trigger does not path-filter; changes job uses merge-base + --no-renames");
  }

  console.log(`\nci-classify integration: ${passed} checks passed.`);
} finally {
  for (const d of cleanup) {
    try { fs.rmSync(d, { recursive: true, force: true }); } catch (_) { /* best effort */ }
  }
}
