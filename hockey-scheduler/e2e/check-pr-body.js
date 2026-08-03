// PR body vs. diff consistency gate (#389 review).
//
// WHY THIS EXISTS. #389's body described the implementation it had on its FIRST
// round: day zero snapping to Saturday 7-13 days out, no existing tests changed,
// the `app.js` pointer listed under "Outstanding / not fixed here", and browser
// gates "in progress". By the time it was reviewed the head did none of those
// things. Because the body says `Closes #387`, that stale text is the surface a
// merge decision is made on — the owner can accept or reject a contract the
// branch no longer implements. A PR body is not documentation; it is the
// argument for merging, and an argument about code that is not there is worse
// than no argument.
//
// A careful rewrite does not fix this, because the next commit makes it stale
// again. So the body carries a machine-readable fact block, and this gate FAILS
// when any of those facts disagrees with the head — plus a scan for the exact
// prose that went stale last time, since a correct fact block underneath
// misleading paragraphs would still mislead.
//
// GROUND TRUTH comes from the working tree and git, never from the body:
//
//   lead_days           _DEMO_LEAD_DAYS in full_demo.py
//   weekday_snap        whether _DEMO_WEEKDAY still exists there
//   backend_tests_changed / e2e_files_changed
//                       git diff --name-only <base>...<head>
//   ui_pointer          whether app.js still names a YYYY-MM-DD literal
//   head / base         git rev-parse / merge-base (or the CI event payload)
//
// WHY THERE IS NO `ci` FACT (#389 review round 2).
//
// There used to be one, and it was the worst thing in this file. `ciStatus()`
// returned null whenever GITHUB_ACTIONS was set — a job inside a run genuinely
// cannot observe its own run's terminal status — and the `ci` row was then
// printed SKIP and counted as `ok`. So in the one environment where this gate
// is authoritative, its most volatile field was not checked at all, while the
// body told readers it was. The gate went green on a head whose body said
// `ci: pending` after the run had finished green. A check that skips a field
// and reports ok is indistinguishable from one that verified it.
//
// The fix is not a better skip. CI status is simply NOT A PROPERTY OF THE HEAD:
// every other fact above is a pure function of the tree and git and cannot
// change without a commit, whereas a run's conclusion changes on re-run, on
// infrastructure flake, on nothing at all. Recording it as a "fact about the
// head that this gate verifies" is a category error, and no amount of
// machinery fixes a claim that is not about the thing being claimed.
//
// The alternative considered was a separate `workflow_run`-triggered workflow
// firing on the main run's completion. That is genuinely non-self-referential,
// and it was rejected for a concrete reason: `workflow_run` workflows only ever
// execute from the DEFAULT BRANCH, so one added on this branch would not run on
// this PR at all. Adding it here would ship a mechanism that provably does
// nothing while the body advertised the protection — the same defect this whole
// gate exists to prevent, one level up.
//
// So `ci` is gone from the block, and the body no longer claims it is verified.
// The protection is not merely dropped: UNKNOWN KEYS ARE NOW REJECTED, so a
// future `ci:` line (or any other unverified claim smuggled into the
// machine-checked block) fails loudly instead of sitting there looking checked.
// That removes the whole class rather than tracking one instance of it.
//
// USAGE
//   node check-pr-body.js                  # fetches the body with `gh`
//   node check-pr-body.js --body-file X    # offline, or to test a candidate
//
// The body is always fetched LIVE, never taken from the CI event payload. A
// pull_request payload snapshots the body as it was when the run started, so a
// body corrected after the push would still be judged on its old text — the
// gate would report a failure that no longer exists, and re-running would not
// clear it. Fetching live means a re-run always judges what is actually there.
//
// Exits non-zero on any disagreement.
const { execFileSync } = require("child_process");
const fs = require("fs");
const path = require("path");

const REPO = "jingizoo/biknik";
// Only used by the LOCAL `gh` fallback. On CI the body, head and base all
// arrive through the event payload, so the number is never consulted there.
const PR = process.env.PR_NUMBER || "389";
const ROOT = path.resolve(__dirname, "..", "..");
const FULL_DEMO = path.join(
  ROOT, "hockey-scheduler/backend/hockey_scheduler/full_demo.py");
const APP_JS = path.join(
  ROOT, "hockey-scheduler/backend/hockey_scheduler/web/static/app.js");

// Prose from the superseded body that is false at this head.
//
// Deliberately narrow: only strings that CANNOT be a historical aside. A body
// should be free to explain what it replaced ("the first round put day zero on
// the first Saturday…"), and no regex can reliably tell that from a live claim.
// So anything a fact above already covers rigorously is left to the fact — the
// `weekday_snap` check reads _DEMO_WEEKDAY straight out of the source, which
// beats grepping prose for "Saturday" and never mistakes history for a claim.
//
// What remains are the two structural headings a reader navigates by, and the
// one claim no derived fact covers: whether EXISTING tests changed
// (`backend_tests_changed` counts changed files, and cannot distinguish an
// added file from a modified one).
const STALE_PROSE = [
  { re: /Outstanding\s*\/\s*not fixed here/i,
    why: "lists work as outstanding that this head actually does" },
  { re: /browser gates\s*\|?\s*in progress/i,
    why: "claims browser gates are still running" },
  { re: /no existing assertion needed to change/i,
    why: "claims no existing test changed; this head changes several" },
  { re: /\b0 existing tests modified\b/i,
    why: "claims no existing test changed; this head changes several" },
];

function sh(cmd, args) {
  return execFileSync(cmd, args, { cwd: ROOT, encoding: "utf8" }).trim();
}

function groundTruth() {
  const demo = fs.readFileSync(FULL_DEMO, "utf8");
  const lead = demo.match(/^_DEMO_LEAD_DAYS\s*=\s*(\d+)/m);
  if (!lead) throw new Error("could not read _DEMO_LEAD_DAYS from full_demo.py");

  // On a pull_request event the checkout is the MERGE commit, so `git rev-parse
  // HEAD` is not the PR head — the workflow passes the real one in.
  const head = process.env.PR_HEAD_SHA || sh("git", ["rev-parse", "HEAD"]);
  // Always the merge-base, never the event's `base.sha`: the body's claims are
  // about `git diff main...HEAD`, and that is the commit it diffs from.
  const base = sh("git", ["merge-base", "origin/main", head]);

  const changed = sh("git", ["diff", "--name-only", `${base}...${head}`])
    .split("\n").filter(Boolean);
  // Premise for the two counts below: a PR always changes something. An empty
  // diff (wrong base, shallow clone, detached ref) would make both counts a
  // vacuous 0, which a body stating 0 would then "agree" with — the same
  // can't-fail shape as the retired `ci` skip, in a different field.
  if (!changed.length) {
    throw new Error(
      `git diff ${base}...${head} is empty — the changed-file counts would be `
      + `a vacuous 0. Is origin/main fetched and the base correct?`);
  }

  // A literal in EXECUTABLE code only — comments explaining the retired dates
  // by name are expected, same exemption the backend's structural guard makes.
  const appHasLiteral = fs.readFileSync(APP_JS, "utf8").split("\n")
    .some((l) => /["'`]\d{4}-\d{2}-\d{2}["'`]/.test(l.replace(/\/\/.*$/, "")));

  return {
    lead_days: Number(lead[1]),
    weekday_snap: /^_DEMO_WEEKDAY\s*=/m.test(demo) ? "saturday" : "none",
    backend_tests_changed: changed.filter(
      (f) => f.startsWith("hockey-scheduler/backend/tests/")).length,
    e2e_files_changed: changed.filter(
      (f) => f.startsWith("hockey-scheduler/e2e/")).length,
    ui_pointer: appHasLiteral ? "outstanding" : "fixed",
    head: head.slice(0, 7),
    base: base.slice(0, 7),
  };
}

// Every fact this gate compares. Exhaustive on purpose: the block must carry
// exactly these keys, no more and no fewer. Anything else is either a fact
// nobody verifies (which is how the retired `ci` field misled readers) or a
// misspelling of one that is silently going unchecked.
const FACTS = ["lead_days", "weekday_snap", "backend_tests_changed",
               "e2e_files_changed", "ui_pointer", "head", "base"];

function parseFactBlock(body) {
  const m = body.match(/<!--\s*pr-facts\s*([\s\S]*?)-->/);
  if (!m) {
    throw new Error(
      "the PR body has no <!-- pr-facts ... --> block. Merge-decision facts "
      + "must be machine-checkable; prose alone goes stale silently.");
  }
  const facts = {};
  for (const line of m[1].split("\n")) {
    // Digits belong in key names too — `e2e_files_changed` was silently
    // unparsed and reported "(missing)" while sitting right there in the block.
    const kv = line.match(/^\s*([a-z0-9_]+)\s*:\s*(\S+)\s*$/);
    if (kv) facts[kv[1]] = kv[2];
  }
  return facts;
}

function main() {
  const i = process.argv.indexOf("--body-file");
  const body = i !== -1
    ? fs.readFileSync(process.argv[i + 1], "utf8")
    : sh("gh", ["pr", "view", PR, "--repo", REPO, "--json", "body",
                "--jq", ".body"]);

  const truth = groundTruth();
  const facts = parseFactBlock(body);

  const rows = [];
  const check = (name, expected, actual) => {
    rows.push({ name, expected: String(expected), actual: String(actual),
                ok: String(expected) === String(actual) });
  };

  for (const key of FACTS) {
    check(key, truth[key], facts[key] === undefined ? "(missing)" : facts[key]);
  }

  // Unknown keys are a failure, not noise. A key nobody compares looks exactly
  // like a verified one to a reader — that is precisely how `ci: pending` sat
  // in a block advertised as gate-checked while the run was green.
  const unknown = Object.keys(facts).filter((k) => !FACTS.includes(k));

  const stale = STALE_PROSE
    .filter((s) => s.re.test(body.replace(/<!--[\s\S]*?-->/g, "")))
    .map((s) => s.why);

  let width = 0;
  for (const r of rows) width = Math.max(width, r.name.length);
  for (const r of rows) {
    console.log(`  ${r.ok ? "ok  " : "FAIL"}  ${r.name.padEnd(width)}`
      + `  head=${r.expected}  body=${r.actual}`);
  }
  for (const k of unknown) {
    console.log(`  FAIL  ${k}: not a fact this gate verifies — remove it, or `
      + `add it to FACTS with ground truth derived from the head`);
  }
  for (const why of stale) console.log(`  FAIL  stale prose: ${why}`);

  // Anti-vacuity. Every row must have been a real comparison — there is no
  // longer any code path that records a row without comparing it, and this
  // asserts that rather than trusting it. An exact count, not a floor, so
  // dropping a fact from FACTS fails here instead of quietly narrowing the
  // gate.
  if (rows.length !== FACTS.length) {
    console.error(`compared ${rows.length} facts, expected exactly `
      + `${FACTS.length}`);
    process.exit(1);
  }

  const bad = rows.filter((r) => !r.ok).length + unknown.length + stale.length;
  if (bad) {
    console.error(`\nPR body disagrees with this head in ${bad} place(s). `
      + `Rewrite the body from the head, not the other way round.`);
    process.exit(1);
  }
  console.log(`PR body consistent with ${truth.head} `
    + `(${rows.length} facts checked, none skipped).`);
}

main();
