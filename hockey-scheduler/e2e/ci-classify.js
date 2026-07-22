// CI change classifier — decides which heavy jobs a pull request must run,
// FAIL-CLOSED. The design bias is safety, not speed: anything we cannot
// positively classify as skip-safe runs the FULL matrix. Concretely:
//
//   * an UNKNOWN path (anything not matched below), a DEPENDENCY/BUILD manifest,
//     or a WORKFLOW change  -> the full matrix (test + postgres + frontend + browser);
//   * a backend API-contract change (the HTTP transport or the facade the
//     browser/journeys consume) -> the DB matrix AND the browser/contract gate,
//     because a response-shape change can break a journey with no front-end edit;
//   * other backend Python / SQL migrations -> the DB matrix (memory+SQLite and
//     PostgreSQL) — migrations keep the full DB matrix, never a single backend;
//   * front-end (served static assets) or e2e journeys -> frontend-check + browser;
//   * docs / markdown only -> no heavy job;
//   * any non-pull_request event (push to main, dispatch, schedule) -> full matrix,
//     since main is the gate of record and there is no cheap base to diff.
//
// This is a pure module (no deps) so it can be unit-tested deterministically
// (see ci-classify.test.js). The GitHub workflow pipes `git diff --name-only`
// into the CLI at the bottom, which appends the booleans to $GITHUB_OUTPUT.

"use strict";

const PROJECT = "hockey-scheduler/";
const BACKEND = PROJECT + "backend/hockey_scheduler/";

// Exactly one category per file; first match wins, so ORDER MATTERS.
function categorize(file) {
  const f = String(file).trim();
  if (!f) return "empty";

  // 1. CI workflow definitions — any change reruns the full matrix.
  if (f.startsWith(".github/workflows/")) return "workflow";

  // 2. Dependency / build / packaging manifests (by basename, anywhere). A
  //    resolved-version or build change can alter any layer -> fail closed.
  const base = f.split("/").pop();
  if (/^(requirements[^/]*\.txt|constraints[^/]*\.txt|pyproject\.toml|setup\.py|setup\.cfg|Pipfile|Pipfile\.lock|poetry\.lock|package\.json|package-lock\.json|npm-shrinkwrap\.json|yarn\.lock|pnpm-lock\.yaml|Dockerfile|docker-compose\.ya?ml|\.tool-versions)$/.test(base)) {
    return "deps";
  }

  // 3. Docs — project docs or any markdown.
  if (f.startsWith(PROJECT + "docs/") || /\.md$/i.test(f)) return "docs";

  // 4. Front-end — the served static assets the browser actually runs.
  if (f.startsWith(BACKEND + "web/static/")) return "frontend";

  // 5. e2e — the Playwright journeys / node scripts.
  if (f.startsWith(PROJECT + "e2e/")) return "e2e";

  // 6. SQL migrations — DB-sensitive backend (kept on the full DB matrix).
  if (/\.sql$/i.test(f) || f.startsWith(BACKEND + "store/migrations/")) return "migration";

  // 7. Backend API-contract surface — the HTTP transport (web/, minus the
  //    static assets handled in (4)) and the facade the journeys consume. A
  //    change here can break the browser gate, so it also runs the browser.
  if (f.startsWith(BACKEND + "web/") || f.startsWith(BACKEND + "api/")) return "backend_api";

  // 8. Other backend Python (domain / services / store / tests) — DB matrix.
  if (/\.py$/i.test(f) && f.startsWith(PROJECT + "backend/")) return "backend_model";

  // 9. Anything else — unknown -> fail closed.
  return "unknown";
}

function full(reason) {
  return { test: true, postgres: true, frontend_check: true, browser_smoke: true, reason: "full:" + reason };
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
    // 'docs' contributes no heavy job.
  }
  return { test, postgres, frontend_check, browser_smoke, reason: cats.join(",") };
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
  ];
  if (process.env.GITHUB_OUTPUT) {
    fs.appendFileSync(process.env.GITHUB_OUTPUT, lines.join("\n") + "\n");
  }
  process.stdout.write(lines.join("\n") + "\n");
  process.stderr.write(`ci-classify: ${files.length} file(s) [${eventName}] -> ${r.reason}\n`);
}
