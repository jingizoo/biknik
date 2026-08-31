// CI change classifier — decides which heavy jobs a pull request must run,
// FAIL-CLOSED. The design bias is safety, not speed: anything we cannot
// positively classify as skip-safe runs the FULL matrix. Concretely:
//
//   * an UNKNOWN path (anything not matched below), a DEPENDENCY/BUILD manifest,
//     a WORKFLOW change, or a change to the CLASSIFIER ITSELF (ci-classify*.js —
//     the code that decides routing, treated as CI infrastructure so a routing
//     regression can't route around the full gate)  -> the full matrix
//     (test + postgres + frontend + browser);
//   * a backend API-contract change (the HTTP transport or the facade the
//     browser/journeys consume) -> the DB matrix AND the browser/contract gate,
//     because a response-shape change can break a journey with no front-end edit;
//   * other backend Python / SQL migrations -> the DB matrix (memory+SQLite and
//     PostgreSQL) — migrations keep the full DB matrix, never a single backend;
//   * front-end (served static assets) or e2e journeys -> frontend-check + browser;
//   * hockey docs / markdown only -> no heavy job, but still the cheap live
//     PR-body consistency gate;
//   * a path that is unambiguously the SIBLING root project (this is a monorepo)
//     -> no hockey job (skip-safe); anything NOT recognized as hockey or sibling
//     still falls through to `unknown` -> full matrix, so an omitted sibling path
//     over-runs rather than silently skipping;
//   * any non-pull_request event (push to main, dispatch, schedule) -> full matrix,
//     since main is the gate of record and there is no cheap base to diff.
//
// The trigger deliberately does NOT path-filter (that was fail-open — an
// unrecognized path never reached this classifier); every change reaches here
// and THIS decides. This is a pure module (no deps) so it can be unit-tested
// deterministically (see ci-classify.test.js); a cross-category rename and the
// merge-base diff are covered by ci-classify.integration.test.js. The workflow
// pipes `git diff --name-only --no-renames BASE...HEAD` into the CLI at the
// bottom, which appends the booleans to $GITHUB_OUTPUT.

"use strict";

const PROJECT = "hockey-scheduler/";
const BACKEND = PROJECT + "backend/hockey_scheduler/";

// This repo is a MONOREPO: `hockey-scheduler/` sits alongside an unrelated root
// project (a Java/Gradle + Python document-transfer service). This workflow
// gates the hockey-scheduler subtree only, but the trigger no longer path-
// filters (fail-open), so EVERY change reaches the classifier. Paths that are
// unambiguously the sibling project are skip-safe for the hockey suite; ANY
// path we do not positively recognize as hockey OR known-sibling falls through
// to `unknown` -> full matrix. Omitting a sibling path therefore fails CLOSED
// (it over-runs the hockey suite, it never silently skips a gate).
const SIBLING_DIRS = [
  "src/", "sql/", "tests/", "config/", "k8s/", "terraform/", "tools/", "data/",
  "docs/", ".vscode/", ".claude/",
];
const SIBLING_FILES = new Set([
  "build.gradle", "render.yaml", "run_local.py", "run_single_transfer.py",
  "document_xfer.md", "sample_bip_with_pre_dff_updates.json",
  "sample_requests.json", ".env.example", "requirements.txt", "README.md",
  ".gitignore",
]);

// Exactly one category per file; first match wins, so ORDER MATTERS.
function categorize(file) {
  const f = String(file).trim();
  if (!f) return "empty";

  // 1. CI workflow definitions — any change reruns the full matrix.
  if (f.startsWith(".github/workflows/")) return "workflow";

  // 2. The sibling root project — skip-safe for the hockey suite. Checked
  //    BEFORE the manifest/docs rules so a root `requirements.txt`, `build.gradle`
  //    or `docs/` (the sibling's, not hockey's under hockey-scheduler/) is a
  //    skip, not a full-matrix dependency/doc trigger. These patterns are root-
  //    anchored, so they never match a `hockey-scheduler/...` path.
  if (SIBLING_FILES.has(f) || SIBLING_DIRS.some((d) => f.startsWith(d))) return "sibling";

  // 3. Dependency / build / packaging manifests (by basename, anywhere). A
  //    resolved-version or build change can alter any layer -> fail closed.
  const base = f.split("/").pop();
  if (/^(requirements[^/]*\.txt|constraints[^/]*\.txt|pyproject\.toml|setup\.py|setup\.cfg|Pipfile|Pipfile\.lock|poetry\.lock|package\.json|package-lock\.json|npm-shrinkwrap\.json|yarn\.lock|pnpm-lock\.yaml|Dockerfile|docker-compose\.ya?ml|\.tool-versions)$/.test(base)) {
    return "deps";
  }

  // 5. Docs — hockey project docs or any markdown.
  if (f.startsWith(PROJECT + "docs/") || /\.md$/i.test(f)) return "docs";

  // 6. Front-end — the served static assets the browser actually runs.
  if (f.startsWith(BACKEND + "web/static/")) return "frontend";

  // 7a. The classifier itself (and its tests) is CI INFRASTRUCTURE: it is the
  //     code that DECIDES routing, so a change to it gets the same full-matrix
  //     treatment as a workflow change. It lives under e2e/, so this MUST be
  //     checked before the generic e2e rule below — otherwise a classifier-only
  //     PR would route to frontend+browser and skip the Python/DB suites, letting
  //     an accidental routing regression self-suppress the very gate it governs.
  if (f.startsWith(PROJECT + "e2e/ci-classify")) return "ci_infra";

  // 7b. e2e — the ordinary Playwright journeys / node scripts.
  if (f.startsWith(PROJECT + "e2e/")) return "e2e";

  // 8. SQL migrations — DB-sensitive backend (kept on the full DB matrix).
  if (/\.sql$/i.test(f) || f.startsWith(BACKEND + "store/migrations/")) return "migration";

  // 9. Backend API-contract surface — the HTTP transport (web/, minus the
  //    static assets handled in (6)) and the facade the journeys consume. A
  //    change here can break the browser gate, so it also runs the browser.
  if (f.startsWith(BACKEND + "web/") || f.startsWith(BACKEND + "api/")) return "backend_api";

  // 10. Other backend Python (domain / services / store / tests) — DB matrix.
  if (/\.py$/i.test(f) && f.startsWith(PROJECT + "backend/")) return "backend_model";

  // 11. Anything else — unknown -> fail closed (full matrix).
  return "unknown";
}

function full(reason) {
  return {
    test: true,
    postgres: true,
    frontend_check: true,
    browser_smoke: true,
    pr_body_check: true,
    reason: "full:" + reason,
  };
}

// files: array of repo-relative paths. eventName: the GitHub event name.
function classify(files, eventName) {
  // Non-PR events always run the full matrix (main is the gate of record; no
  // cheap PR base to diff against).
  if (eventName && eventName !== "pull_request") return full("event:" + eventName);

  const list = (files || []).map((f) => String(f).trim()).filter(Boolean);
  // No detectable changes -> fail closed rather than skip silently.
  if (!list.length) return full("no-files");

  const cats = list.map(categorize);
  const set = new Set(cats);

  // Fail-closed triggers: any of these forces the whole matrix.
  if (set.has("unknown")) return full("unknown-path");
  if (set.has("ci_infra")) return full("classifier-change");
  if (set.has("deps")) return full("dependency-manifest");
  if (set.has("workflow")) return full("workflow-change");

  let test = false, postgres = false, frontend_check = false, browser_smoke = false;
  for (const c of set) {
    if (c === "migration" || c === "backend_model" || c === "backend_api") {
      test = true; postgres = true;
    }
    if (c === "backend_api" || c === "frontend" || c === "e2e") {
      frontend_check = true; browser_smoke = true;
    }
    // 'docs' and 'sibling' contribute no heavy job. Mixed PRs take the union,
    // so a sibling change alongside a hockey change still runs the hockey job
    // the hockey change needs.
  }
  // The live PR-body contract belongs to every Hockey Scheduler PR, including
  // docs-only changes, rather than to the classifier-gated front-end job. The
  // only safe skip is a diff made exclusively from positively recognized
  // sibling-project paths. Unknown and empty diffs take the `full()` exits
  // above and therefore fail closed to true.
  const pr_body_check = [...set].some((c) => c !== "sibling");
  return {
    test,
    postgres,
    frontend_check,
    browser_smoke,
    pr_body_check,
    reason: cats.join(","),
  };
}

module.exports = { classify, categorize };

// ---- CLI: `git diff --name-only BASE HEAD | node ci-classify.js` ----------
// Reads changed files from argv (if any) else stdin (newline-separated), the
// event name from $CI_EVENT_NAME, and appends the booleans to $GITHUB_OUTPUT
// (also echoing to stdout for local use). Any non-PR event ignores the files.
if (require.main === module) {
  const fs = require("fs");
  const argvFiles = process.argv.slice(2).filter(Boolean);
  let stdin = "";
  if (!argvFiles.length) {
    try { stdin = fs.readFileSync(0, "utf8"); } catch (_) { stdin = ""; }
  }
  const files = (argvFiles.length ? argvFiles : stdin.split("\n"))
    .map((s) => s.trim()).filter(Boolean);
  const eventName = process.env.CI_EVENT_NAME || "pull_request";
  const r = classify(files, eventName);
  const lines = [
    `test=${r.test}`,
    `postgres=${r.postgres}`,
    `frontend_check=${r.frontend_check}`,
    `browser_smoke=${r.browser_smoke}`,
    `pr_body_check=${r.pr_body_check}`,
  ];
  if (process.env.GITHUB_OUTPUT) {
    fs.appendFileSync(process.env.GITHUB_OUTPUT, lines.join("\n") + "\n");
  }
  process.stdout.write(lines.join("\n") + "\n");
  process.stderr.write(`ci-classify: ${files.length} file(s) [${eventName}] -> ${r.reason}\n`);
}
