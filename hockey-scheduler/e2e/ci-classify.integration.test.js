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
//      re-introduce the fail-open bypass this PR removes); and
//      D. a falsified workflow contract proving the live PR-body gate is in
//      its own edited-aware workflow rather than the gated front-end job; and
//      E. the real CLI writes the routing decision GitHub Actions consumes.

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
const isFull = (r) => r.test && r.postgres && r.frontend_check
  && r.browser_smoke && r.pr_body_check;
const isDB = (r) => r.test && r.postgres && !r.frontend_check
  && !r.browser_smoke && r.pr_body_check;
const hasNoHeavyJob = (r) => !r.test && !r.postgres
  && !r.frontend_check && !r.browser_smoke;

function assertBodyWorkflowContract(mainWorkflow, bodyWorkflow) {
  const runLines = (stepName) => {
    const marker = `      - name: ${stepName}\n`;
    const start = bodyWorkflow.indexOf(marker);
    assert.ok(start >= 0, `missing workflow step: ${stepName}`);
    const next = bodyWorkflow.indexOf("\n      - name:", start + marker.length);
    const block = bodyWorkflow.slice(start, next < 0 ? undefined : next);
    const run = block.indexOf("\n        run: |\n");
    assert.ok(run >= 0, `${stepName} must have a multiline run block`);
    return block.slice(run + "\n        run: |\n".length).split("\n")
      .map((line) => line.trim()).filter(Boolean);
  };
  const onStart = bodyWorkflow.indexOf("\non:");
  const permissionsStart = bodyWorkflow.indexOf("\npermissions:");
  const concurrencyStart = bodyWorkflow.indexOf("\nconcurrency:");
  assert.ok(onStart >= 0 && permissionsStart > onStart,
    "the PR-body workflow must have an on block before permissions");
  assert.ok(concurrencyStart > permissionsStart,
    "the PR-body workflow must declare concurrency after permissions");
  const triggerBlock = bodyWorkflow.slice(onStart, permissionsStart);
  const permissionsBlock = bodyWorkflow.slice(permissionsStart, concurrencyStart);
  assert.ok(
    /types:\s*\[[^\]]*opened[^\]]*synchronize[^\]]*reopened[^\]]*edited[^\]]*\]/
      .test(bodyWorkflow),
    "the PR-body workflow must rerun for edited bodies as well as new heads");
  assert.ok(!/^\s*paths\s*:/m.test(triggerBlock),
    "the PR-body workflow trigger must not use paths (a start-time bypass)");
  assert.ok(!/^\s*paths-ignore\s*:/m.test(triggerBlock),
    "the PR-body workflow trigger must not use paths-ignore (a start-time bypass)");
  assert.ok(!/^\s*pull_request_target\s*:/m.test(bodyWorkflow),
    "PR-controlled JavaScript must never run under pull_request_target");
  const permissions = permissionsBlock.split("\n").map((line) => line.trim())
    .filter((line) => /^[a-z-]+:\s*\S+/.test(line));
  assert.deepStrictEqual(permissions, ["contents: read", "pull-requests: read"],
    "the PR-body workflow permissions must be exactly the two read grants");
  assert.ok(/group:\s*hockey-pr-body-/.test(bodyWorkflow),
    "the PR-body workflow must have concurrency independent from the long CI run");
  assert.ok(/fetch-depth:\s*0/.test(bodyWorkflow),
    "the body workflow needs full history for its merge-base diff");
  assert.ok(/--no-renames/.test(bodyWorkflow),
    "the body workflow classifier must surface both sides of a rename");
  assert.ok(/\$BASE_SHA\.\.\.\$HEAD_SHA/.test(bodyWorkflow),
    "the body workflow classifier must use the PR merge-base diff");
  assert.ok(/CI_EVENT_NAME:\s*pull_request/.test(bodyWorkflow),
    "the body workflow must classify the diff as a pull request");
  const stepIds = bodyWorkflow.split("\n").map((line) => line.trim())
    .filter((line) => line.startsWith("id:"));
  assert.deepStrictEqual(stepIds, ["id: classify"],
    "the producer step ID must remain classify so the condition consumes its output");
  assert.ok(!/^\s*GITHUB_OUTPUT\s*:/m.test(bodyWorkflow),
    "the workflow must not override GitHub's protected step-output path");
  assert.deepStrictEqual(runLines("Classify PR scope for the body contract"), [
    "set -euo pipefail",
    "git fetch --no-tags origin \"$BASE_SHA\" || true",
    "FILES=\"$(git diff --name-only --no-renames \"$BASE_SHA...$HEAD_SHA\")\" || FILES=\"__DIFF_ERROR__\"",
    "printf '%s\\n' \"$FILES\" | node hockey-scheduler/e2e/ci-classify.js",
  ], "the classify step must derive and route the exact PR diff, with no bypass commands");
  const conditions = bodyWorkflow.split("\n")
    .map((line) => line.trim()).filter((line) => line.startsWith("if:"));
  assert.deepStrictEqual(
    conditions,
    ["if: steps.classify.outputs.pr_body_check == 'true'"],
    "the only workflow condition must be exactly the pr_body_check output");
  assert.ok(!/^\s*continue-on-error\s*:/m.test(bodyWorkflow),
    "the live body workflow must never turn a checker failure into success");
  assert.ok(/PR_NUMBER:\s*\$\{\{\s*github\.event\.pull_request\.number\s*\}\}/
    .test(bodyWorkflow), "the checker must receive the live PR number");
  assert.ok(/PR_HEAD_SHA:\s*\$\{\{\s*github\.event\.pull_request\.head\.sha\s*\}\}/
    .test(bodyWorkflow), "the checker must receive the exact PR head");
  assert.deepStrictEqual(runLines("PR body agrees with this head (#436)"), [
    "set -euo pipefail",
    "node --check hockey-scheduler/e2e/check-pr-body.js",
    "git fetch --no-tags origin main:refs/remotes/origin/main",
    "node hockey-scheduler/e2e/check-pr-body.js",
  ], "the body step must syntax-check and invoke the checker with no bypass commands");
  assert.ok(/main:refs\/remotes\/origin\/main/.test(bodyWorkflow),
    "the checker must fetch the base ref it derives head facts against");
  assert.ok(!/check-pr-body\.js/.test(mainWorkflow),
    "frontend-check must not retain a second, classifier-gated body invocation");
}

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
    const misclassified = classify(withRenames, "pull_request");
    assert.ok(hasNoHeavyJob(misclassified),
      "sanity: the rename-detected diff misclassifies as docs-only (the gap)");
    assert.ok(misclassified.pr_body_check,
      "even a docs-only Hockey diff still runs the cheap PR-body gate");

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

  // --- D. the live PR-body gate must not hide under a heavy job ------------
  {
    const mainPath = path.resolve(
      __dirname, "..", "..", ".github", "workflows", "hockey-scheduler-ci.yml");
    const bodyPath = path.resolve(
      __dirname, "..", "..", ".github", "workflows",
      "hockey-scheduler-pr-body.yml");
    const mainWorkflow = fs.readFileSync(mainPath, "utf8");
    const bodyWorkflow = fs.readFileSync(bodyPath, "utf8");

    assertBodyWorkflowContract(mainWorkflow, bodyWorkflow);

    // Falsify the two regressions this contract exists to catch. Each mutation
    // must make this check itself fail, rather than relying on some unrelated
    // syntax or classifier assertion to turn the overall suite red.
    assert.throws(
      () => assertBodyWorkflowContract(
        mainWorkflow,
        bodyWorkflow.replace(", edited]", "]")),
      /edited bodies/,
      "dropping the body-edited trigger must fail this contract");
    assert.throws(
      () => assertBodyWorkflowContract(
        mainWorkflow,
        bodyWorkflow.replace(
          "steps.classify.outputs.pr_body_check",
          "needs.changes.outputs.frontend_check")),
      /pr_body_check output/,
      "putting the live-body step back behind frontend_check must fail");
    assert.throws(
      () => assertBodyWorkflowContract(
        mainWorkflow + "\nnode hockey-scheduler/e2e/check-pr-body.js\n",
        bodyWorkflow),
      /must not retain/,
      "duplicating the checker in the heavy workflow must fail");
    assert.throws(
      () => assertBodyWorkflowContract(
        mainWorkflow,
        bodyWorkflow.replace(
          "    types: [opened, synchronize, reopened, edited]",
          "    types: [opened, synchronize, reopened, edited]\n"
          + "    paths-ignore:\n      - hockey-scheduler/**")),
      /must not use paths-ignore/,
      "a start-time paths-ignore bypass must fail this contract");
    assert.throws(
      () => assertBodyWorkflowContract(
        mainWorkflow,
        bodyWorkflow.replace(
          "    types: [opened, synchronize, reopened, edited]",
          "    types: [opened, synchronize, reopened, edited]\n"
          + "    paths:\n      - docs/**")),
      /must not use paths/,
      "a start-time paths bypass must fail this contract");
    assert.throws(
      () => assertBodyWorkflowContract(
        mainWorkflow,
        bodyWorkflow.replace(
          "steps.classify.outputs.pr_body_check == 'true'",
          "steps.classify.outputs.pr_body_check == 'true' && false")),
      /only workflow condition must be exactly/,
      "a condition that makes the checker step vacuous must fail");
    assert.throws(
      () => assertBodyWorkflowContract(
        mainWorkflow,
        bodyWorkflow.replace(
          "        if: steps.classify.outputs.pr_body_check == 'true'",
          "        if: steps.classify.outputs.pr_body_check == 'true'\n"
          + "        continue-on-error: true")),
      /never turn a checker failure into success/,
      "continue-on-error must fail this contract");
    assert.throws(
      () => assertBodyWorkflowContract(
        mainWorkflow,
        bodyWorkflow.replace(
          "  pr-body-check:\n",
          "  pr-body-check:\n    if: false\n")),
      /only workflow condition must be exactly/,
      "a job-level false condition must fail this contract");
    assert.throws(
      () => assertBodyWorkflowContract(
        mainWorkflow,
        bodyWorkflow.replace("        id: classify", "        id: scope")),
      /producer step ID must remain classify/,
      "renaming the output-producing step must fail this contract");
    assert.throws(
      () => assertBodyWorkflowContract(
        mainWorkflow,
        bodyWorkflow.replace(
          "printf '%s\\n' \"$FILES\" | node hockey-scheduler/e2e/ci-classify.js",
          "node hockey-scheduler/e2e/ci-classify.js src/Main.java")),
      /classify step must derive and route the exact PR diff/,
      "hard-coding a sibling path instead of classifying the diff must fail");

    passed += 1;
    console.log("  ok  D. PR-body workflow is edited-aware, read-only, independently gated, and mutation-proved");
  }

  // --- E. the CLI must publish the output the workflow condition consumes --
  {
    const classifier = path.resolve(__dirname, "ci-classify.js");
    const cases = [
      {
        name: "hockey-docs",
        files: ["hockey-scheduler/docs/product/x.md"],
        want: { test: "false", postgres: "false", frontend_check: "false",
          browser_smoke: "false", pr_body_check: "true" },
      },
      {
        name: "sibling-only",
        files: ["src/Main.java", "README.md"],
        want: { test: "false", postgres: "false", frontend_check: "false",
          browser_smoke: "false", pr_body_check: "false" },
      },
      {
        name: "mixed",
        files: ["src/Main.java", "hockey-scheduler/backend/tests/test_x.py"],
        want: { test: "true", postgres: "true", frontend_check: "false",
          browser_smoke: "false", pr_body_check: "true" },
      },
    ];

    const runCli = (script, testCase) => {
      const dir = fs.mkdtempSync(path.join(os.tmpdir(), "ci-output-"));
      cleanup.push(dir);
      const output = path.join(dir, "github-output");
      execFileSync(process.execPath, [script, ...testCase.files], {
        cwd: path.resolve(__dirname, "..", ".."),
        encoding: "utf8",
        env: {
          ...process.env,
          CI_EVENT_NAME: "pull_request",
          GITHUB_OUTPUT: output,
        },
      });
      return Object.fromEntries(fs.readFileSync(output, "utf8").trim()
        .split("\n").map((line) => line.split("=")));
    };

    for (const testCase of cases) {
      assert.deepStrictEqual(runCli(classifier, testCase), testCase.want,
        `${testCase.name}: CLI outputs must exactly match the workflow contract`);
    }

    // Prove the check bites the precise wiring defect: deleting only the
    // pr_body_check output must fail this section even though classify() still
    // returns the right in-memory value and every unit case remains green.
    const mutatedDir = fs.mkdtempSync(path.join(os.tmpdir(), "ci-output-mutant-"));
    cleanup.push(mutatedDir);
    const mutated = path.join(mutatedDir, "ci-classify.js");
    fs.writeFileSync(mutated, fs.readFileSync(classifier, "utf8").replace(
      "    `pr_body_check=${r.pr_body_check}`,\n", ""));
    assert.throws(
      () => assert.deepStrictEqual(runCli(mutated, cases[0]), cases[0].want),
      /pr_body_check/,
      "removing only the GitHub output must fail the CLI contract");

    passed += 1;
    console.log("  ok  E. CLI publishes exact Hockey/sibling/mixed outputs and the missing-output mutant fails");
  }

  console.log(`\nci-classify integration: ${passed} checks passed.`);
} finally {
  for (const d of cleanup) {
    try { fs.rmSync(d, { recursive: true, force: true }); } catch (_) { /* best effort */ }
  }
}
