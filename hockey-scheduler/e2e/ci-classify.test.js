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
// The classifier itself is CI infrastructure, matched BEFORE the generic e2e
// rule, so a routing regression can't route around the full gate.
assert.strictEqual(categorize(E + "ci-classify.js"), "ci_infra", "the classifier impl is ci_infra");
assert.strictEqual(categorize(E + "ci-classify.test.js"), "ci_infra", "classifier unit test is ci_infra");
assert.strictEqual(categorize(E + "ci-classify.integration.test.js"), "ci_infra", "classifier integration test is ci_infra");
assert.strictEqual(categorize(E + "context-switcher.js"), "e2e", "an ordinary journey is still e2e");
assert.strictEqual(categorize(D + "x.md"), "docs", "hockey docs markdown");
assert.strictEqual(categorize("Makefile"), "unknown", "unrecognized path is unknown");
// Sibling monorepo project — skip-safe, checked before deps/docs so a ROOT
// requirements.txt / build.gradle / docs is a skip, not a full-matrix trigger.
assert.strictEqual(categorize("src/Main.java"), "sibling", "sibling src/ dir");
assert.strictEqual(categorize("terraform/main.tf"), "sibling", "sibling terraform/ dir");
assert.strictEqual(categorize("build.gradle"), "sibling", "sibling build file (not deps)");
assert.strictEqual(categorize("requirements.txt"), "sibling", "root requirements.txt is the sibling's (not deps)");
assert.strictEqual(categorize("docs/design.md"), "sibling", "ROOT docs/ is the sibling's");
assert.strictEqual(categorize("run_single_transfer.py"), "sibling", "sibling root script (not backend_model)");
assert.strictEqual(categorize("README.md"), "sibling", "root README is the sibling's");
// ...but hockey-scheduler manifests/docs/backend are still hockey.
assert.strictEqual(categorize("hockey-scheduler/backend/requirements.txt"), "deps", "a hockey manifest IS deps");
assert.strictEqual(categorize(D + "architecture/x.md"), "docs", "hockey docs are hockey, not sibling");
// Unrecognized paths OUTSIDE hockey and OUTSIDE the known sibling set are
// unknown (fail-closed), not silently skipped.
assert.strictEqual(categorize("scripts/deploy.sh"), "unknown", "scripts/ is unrecognized -> unknown");
assert.strictEqual(categorize(".github/dependabot.yml"), "unknown", "non-workflow .github file -> unknown");
console.log("  ok  categorize() ordering");
passed += 1;

// --- classify(): the reviewer-named cases ----------------------------------
// 1. docs-only  -> no heavy job
expect("docs-only", [D + "architecture/season-lifecycle.md", "README.md"], "pull_request", NONE);
// 2. frontend-only -> frontend + browser
expect("frontend-only", [B + "web/static/app.js", B + "web/static/styles.css"], "pull_request", WEB);
// 3. e2e-only (an ORDINARY journey) -> frontend + browser (lighter route kept)
expect("e2e-journey-only", [E + "context-switcher.js"], "pull_request", WEB);
expect("e2e-journey+add", [E + "smoke.js", E + "coach-scope.js"], "pull_request", WEB);
// 3b. the classifier itself is CI-critical -> FULL matrix, even though it lives
//     under e2e/. A routing regression must never self-suppress the full gate.
expect("classifier-impl-only", [E + "ci-classify.js"], "pull_request", FULL);
expect("classifier-unit-test-only", [E + "ci-classify.test.js"], "pull_request", FULL);
expect("classifier-integration-test-only", [E + "ci-classify.integration.test.js"], "pull_request", FULL);
expect("classifier+ordinary-journey", [E + "ci-classify.js", E + "context-switcher.js"], "pull_request", FULL);
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
// 8. unknown path -> full matrix (see the dedicated block below for more)
// 9. push to main (and other non-PR events) -> full matrix regardless of files
expect("push-to-main", [D + "x.md"], "push", FULL);
expect("workflow-dispatch", [B + "web/static/app.js"], "workflow_dispatch", FULL);

// --- sibling monorepo project — skip-safe (no hockey job) -------------------
expect("sibling-src-only", ["src/Main.java", "src/util/Helper.java"], "pull_request", NONE);
expect("sibling-terraform-only", ["terraform/main.tf"], "pull_request", NONE);
expect("sibling-build-gradle", ["build.gradle"], "pull_request", NONE);
expect("sibling-root-requirements", ["requirements.txt"], "pull_request", NONE);
expect("sibling-root-docs", ["docs/design.md", "document_xfer.md"], "pull_request", NONE);
expect("sibling-mixed-dirs", ["src/A.java", "terraform/x.tf", "k8s/deploy.yaml", "README.md"], "pull_request", NONE);
// A sibling change ALONGSIDE a hockey change still runs the hockey job (union).
expect("sibling+hockey-frontend", ["src/A.java", B + "web/static/app.js"], "pull_request", WEB);
expect("sibling+hockey-api", ["terraform/x.tf", B + "api/service.py"], "pull_request", FULL);

// --- unrecognized paths outside hockey AND outside the sibling set -> full ---
expect("unknown-scripts", ["scripts/deploy.sh"], "pull_request", FULL);
expect("unknown-makefile", ["Makefile"], "pull_request", FULL);
expect("unknown-dotgithub-nonworkflow", [".github/dependabot.yml"], "pull_request", FULL);
expect("sibling+unknown", ["src/A.java", "Makefile"], "pull_request", FULL);

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
