// Deterministic unit tests for the fail-closed CI classifier (ci-classify.js).
// Plain Node + assert (matches the dependency-free e2e tooling). Run:
//   node ci-classify.test.js
// Asserts the EXACT set of heavy jobs for each reviewer-named case, and that
// unknown / dependency / workflow paths and non-PR events keep the full matrix.

"use strict";
const assert = require("assert");
const { classify, categorize } = require("./ci-classify.js");

const B = "hockey-scheduler/backend/hockey_scheduler/";
const T = "hockey-scheduler/backend/tests/";
const E = "hockey-scheduler/e2e/";
const D = "hockey-scheduler/docs/";

// Expected job sets.
const NONE = { test: false, postgres: false, frontend_check: false, browser_smoke: false };
const DB   = { test: true,  postgres: true,  frontend_check: false, browser_smoke: false };
const WEB  = { test: false, postgres: false, frontend_check: true,  browser_smoke: true  };
const FULL = { test: true,  postgres: true,  frontend_check: true,  browser_smoke: true  };

const KEYS = ["test", "postgres", "frontend_check", "browser_smoke"];
let passed = 0;

function expect(name, files, eventName, want) {
  const got = classify(files, eventName);
  for (const k of KEYS) {
    assert.strictEqual(
      got[k], want[k],
      `${name}: ${k} expected ${want[k]}, got ${got[k]} (reason=${got.reason})`);
  }
  passed += 1;
  console.log(`  ok  ${name}  ->  ${KEYS.filter((k) => want[k]).join("+") || "none"}  (${got.reason})`);
}

// --- categorize() spot checks (first-match ordering) ------------------------
assert.strictEqual(categorize(B + "web/static/app.js"), "frontend", "static asset is frontend, not backend_api");
assert.strictEqual(categorize(B + "web/server.py"), "backend_api", "web/server.py is the API surface");
assert.strictEqual(categorize(B + "api/service.py"), "backend_api", "api/ facade is the API surface");
assert.strictEqual(categorize(B + "services/context_service.py"), "backend_model", "service is backend_model");
assert.strictEqual(categorize(B + "store/migrations/044_x.sql"), "migration", "migration by path");
assert.strictEqual(categorize("anything/foo.sql"), "migration", "any .sql is a migration");
assert.strictEqual(categorize(E + "package.json"), "deps", "e2e manifest is a dependency file");
assert.strictEqual(categorize(E + "context-switcher.js"), "e2e", "e2e journey");
assert.strictEqual(categorize(D + "x.md"), "docs", "docs markdown");
assert.strictEqual(categorize("Makefile"), "unknown", "unrecognized path is unknown");
console.log("  ok  categorize() ordering");
passed += 1;

// --- classify(): the reviewer-named cases ----------------------------------
// 1. docs-only  -> no heavy job
expect("docs-only", [D + "architecture/season-lifecycle.md", "README.md"], "pull_request", NONE);
// 2. frontend-only -> frontend + browser
expect("frontend-only", [B + "web/static/app.js", B + "web/static/styles.css"], "pull_request", WEB);
// 3. e2e-only -> frontend + browser
expect("e2e-only", [E + "context-switcher.js"], "pull_request", WEB);
// 4. backend model-only -> DB matrix, no browser
expect("backend-model-only", [B + "services/context_service.py", B + "domain/season.py"], "pull_request", DB);
expect("backend-store-only", [B + "store/sql_store.py"], "pull_request", DB);
expect("backend-tests-only", [T + "test_active_context.py"], "pull_request", DB);
// 5. backend API-only -> DB matrix AND the browser/contract gate
expect("backend-api-facade", [B + "api/service.py"], "pull_request", FULL);
expect("backend-api-transport", [B + "web/server.py"], "pull_request", FULL);
// 6. requirements / package manifests -> full matrix (fail-closed)
expect("requirements-manifest", ["hockey-scheduler/backend/requirements.txt"], "pull_request", FULL);
expect("e2e-package-lock", [E + "package-lock.json"], "pull_request", FULL);
expect("pyproject", ["pyproject.toml"], "pull_request", FULL);
// 7. workflow change -> full matrix
expect("workflow-change", [".github/workflows/hockey-scheduler-ci.yml"], "pull_request", FULL);
// 8. unknown path -> full matrix
expect("unknown-path", ["scripts/deploy.sh"], "pull_request", FULL);
expect("unknown-root-file", ["Makefile"], "pull_request", FULL);
// 9. push to main (and other non-PR events) -> full matrix regardless of files
expect("push-to-main", [D + "x.md"], "push", FULL);
expect("workflow-dispatch", [B + "web/static/app.js"], "workflow_dispatch", FULL);

// --- migrations keep the full DB matrix ------------------------------------
expect("migration-only", [B + "store/migrations/045_x.sql"], "pull_request", DB);

// --- mixed changes take the UNION -------------------------------------------
expect("docs+frontend", [D + "x.md", B + "web/static/app.js"], "pull_request", WEB);
expect("backend-model+frontend", [B + "services/roster.py", B + "web/static/app.js"], "pull_request", FULL);
expect("model+api", [B + "domain/season.py", B + "api/service.py"], "pull_request", FULL);
// A single unknown file in an otherwise-safe set still forces the full matrix.
expect("frontend+unknown", [B + "web/static/app.js", "random.cfg"], "pull_request", FULL);

// --- empty / defensive -------------------------------------------------------
expect("empty-PR", [], "pull_request", FULL);

console.log(`\nci-classify: ${passed} checks passed.`);
