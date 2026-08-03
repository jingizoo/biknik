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
//   ci                  gh pr checks
//
// USAGE
//   node check-pr-body.js                  # fetches the body with `gh`
//   node check-pr-body.js --body-file X    # offline, or to test a candidate
//   PR_BODY=... node check-pr-body.js      # CI, from the event payload
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

// Prose that was true of the superseded body and is false at this head. Each
// entry is a claim a reader would act on, not a stylistic preference.
const STALE_PROSE = [
  { re: /first Saturday/i,
    why: "claims day zero snaps to a Saturday; the snap was removed" },
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

  const head = process.env.PR_HEAD_SHA || sh("git", ["rev-parse", "HEAD"]);
  const base = process.env.PR_BASE_SHA
    || sh("git", ["merge-base", "origin/main", "HEAD"]);

  const changed = sh("git", ["diff", "--name-only", `${base}...${head}`])
    .split("\n").filter(Boolean);

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

// `null` means "could not be determined here" — reported as SKIPPED, never
// silently treated as agreement.
function ciStatus() {
  if (process.env.GITHUB_ACTIONS) return null;   // its own checks are running
  let raw;
  try {
    raw = sh("gh", ["pr", "checks", PR, "--repo", REPO,
                    "--json", "name,bucket"]);
  } catch {
    return null;                                  // no gh / no network / no auth
  }
  const checks = JSON.parse(raw);
  if (!checks.length) return null;
  if (checks.some((c) => c.bucket === "pending")) return "pending";
  if (checks.every((c) => c.bucket === "pass")) return "all-green";
  return "failing";
}

function parseFactBlock(body) {
  const m = body.match(/<!--\s*pr-facts\s*([\s\S]*?)-->/);
  if (!m) {
    throw new Error(
      "the PR body has no <!-- pr-facts ... --> block. Merge-decision facts "
      + "must be machine-checkable; prose alone goes stale silently.");
  }
  const facts = {};
  for (const line of m[1].split("\n")) {
    const kv = line.match(/^\s*([a-z_]+)\s*:\s*(\S+)\s*$/);
    if (kv) facts[kv[1]] = kv[2];
  }
  return facts;
}

function main() {
  const i = process.argv.indexOf("--body-file");
  const body = i !== -1
    ? fs.readFileSync(process.argv[i + 1], "utf8")
    : (process.env.PR_BODY
       || sh("gh", ["pr", "view", PR, "--repo", REPO, "--json", "body",
                    "--jq", ".body"]));

  const truth = groundTruth();
  const facts = parseFactBlock(body);
  const ci = ciStatus();

  const rows = [];
  const check = (name, expected, actual) => {
    rows.push({ name, expected: String(expected), actual: String(actual),
                ok: String(expected) === String(actual) });
  };

  for (const key of ["lead_days", "weekday_snap", "backend_tests_changed",
                     "e2e_files_changed", "ui_pointer", "head", "base"]) {
    check(key, truth[key], facts[key] === undefined ? "(missing)" : facts[key]);
  }
  if (ci === null) {
    rows.push({ name: "ci", expected: "(undeterminable here)",
                actual: facts.ci || "(missing)", ok: true, skipped: true });
  } else {
    check("ci", ci, facts.ci === undefined ? "(missing)" : facts.ci);
  }

  const stale = STALE_PROSE
    .filter((s) => s.re.test(body.replace(/<!--[\s\S]*?-->/g, "")))
    .map((s) => s.why);

  let width = 0;
  for (const r of rows) width = Math.max(width, r.name.length);
  for (const r of rows) {
    const mark = r.skipped ? "SKIP" : (r.ok ? "ok  " : "FAIL");
    console.log(`  ${mark}  ${r.name.padEnd(width)}  head=${r.expected}`
      + `  body=${r.actual}`);
  }
  for (const why of stale) console.log(`  FAIL  stale prose: ${why}`);

  // Anti-vacuity: a body with a well-formed but empty fact block, or a run
  // where nothing could be derived, must not read as agreement.
  const ran = rows.filter((r) => !r.skipped).length;
  if (ran < 7) {
    console.error(`only ${ran} facts were actually compared; expected 7+`);
    process.exit(1);
  }

  const bad = rows.filter((r) => !r.ok).length + stale.length;
  if (bad) {
    console.error(`\nPR body disagrees with this head in ${bad} place(s). `
      + `Rewrite the body from the head, not the other way round.`);
    process.exit(1);
  }
  console.log(`PR body consistent with ${truth.head} (${ran} facts checked`
    + `${ci === null ? ", ci skipped" : ""}).`);
}

main();
