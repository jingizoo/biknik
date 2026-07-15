// Static v1-route contract check (#233 B2c review, bridge removed in Slice
// E): rather than scanning for the literal substring "/api/setup/" (which
// also matches the two *parameterized* v1-fallback templates in
// commitReassign()/wireModal() — `/api/setup/${pr.kind}/.../assign-${pr.parent}`
// and `/api/setup/${m.kind}/${m.id}/delete` — neither of which spells out a
// concrete path in source), this inspects the DATA STRUCTURES that gate
// those fallbacks:
//
//   - REASSIGN_V2 must cover EVERY REASSIGN entry — there is no longer any
//     documented v1 exception (the temporary Venue→Program compatibility
//     bridge, venue:league, was removed in #233 Slice E). Any REASSIGN entry
//     with no REASSIGN_V2 counterpart falls through commitReassign()'s v1
//     POST — a stray v1 write.
//   - SETUP_POST's create dispatch must target /api/v2/setup/... for every
//     entity — a literal v1 URL there is a stray v1 write.
//
// This is an executable route-CONTRACT test, not a runtime crawl (that's
// v1-dependency-proof.js, which proves no stray call fires in practice).
// Both are required: a reverted REASSIGN_V2/SETUP_POST entry that happens
// not to be exercised by the runtime journey would still be caught here.
//
// Self-tests its own extraction logic against synthetic fixtures before
// checking the real file, so a broken checker fails loudly rather than
// silently passing everything (the "regression proving CI fails on a stray
// call and passes once removed" requirement).
const fs = require("fs");
const path = require("path");

const APP_JS = path.resolve(__dirname, "..", "backend", "hockey_scheduler",
  "web", "static", "app.js");

// Extract the `{...}` body of `const NAME = { ... };`, balancing braces (a
// naive `indexOf("};")` would stop at the first entry's own closing brace).
function extractObjectBody(source, constName) {
  const start = source.indexOf(`const ${constName} = {`);
  if (start === -1) throw new Error(`const ${constName} not found in source`);
  const openBrace = source.indexOf("{", start);
  let depth = 0;
  for (let i = openBrace; i < source.length; i++) {
    if (source[i] === "{") depth++;
    else if (source[i] === "}") {
      depth--;
      if (depth === 0) return source.slice(openBrace + 1, i);
    }
  }
  throw new Error(`const ${constName}'s closing brace was never found`);
}

// REASSIGN / REASSIGN_V2 keys are quoted "kind:parent" object keys at the
// start of a line (ignoring indentation).
function extractQuotedKeys(objectBody) {
  const keys = new Set();
  const re = /^\s*"([a-zA-Z0-9_-]+:[a-zA-Z0-9_-]+)":\s*\{/gm;
  let m;
  while ((m = re.exec(objectBody))) keys.add(m[1]);
  return keys;
}

// SETUP_POST entries: `key: () => post("<path>", ...)` or
// `"key": () => post("<path>", ...)`.
function extractSetupPostPaths(objectBody) {
  const paths = {};
  const re = /^\s*(?:"([\w-]+)"|([\w][\w-]*)):\s*\(\)\s*=>\s*post\("([^"]+)"/gm;
  let m;
  while ((m = re.exec(objectBody))) {
    const key = m[1] || m[2];
    paths[key] = m[3];
  }
  return paths;
}

// The full check, parameterized over source text so it can run against both
// the real app.js and synthetic self-test fixtures.
function checkSource(source) {
  const violations = [];
  const reassignKeys = extractQuotedKeys(extractObjectBody(source, "REASSIGN"));
  const reassignV2Keys = extractQuotedKeys(extractObjectBody(source, "REASSIGN_V2"));
  for (const key of reassignKeys) {
    if (!reassignV2Keys.has(key)) {
      violations.push(`REASSIGN["${key}"] has no REASSIGN_V2 entry — ` +
        `it falls through to a v1 POST.`);
    }
  }
  const setupPostPaths = extractSetupPostPaths(extractObjectBody(source, "SETUP_POST"));
  if (!Object.keys(setupPostPaths).length) {
    violations.push("SETUP_POST extraction found zero entries — the checker's " +
      "regex may be out of sync with app.js's structure.");
  }
  for (const [key, p] of Object.entries(setupPostPaths)) {
    if (!p.startsWith("/api/v2/setup/")) {
      violations.push(`SETUP_POST["${key}"] posts to "${p}", not /api/v2/setup/... — a stray v1 write.`);
    }
  }
  return violations;
}

function selfTest() {
  const clean = `
const SETUP_POST = {
  team: () => post("/api/v2/setup/team", { name: val("f-team") }),
};
const REASSIGN = {
  "team:club": { perm: "manage_setup" },
};
const REASSIGN_V2 = {
  "team:club": { kind: "team", parent: "club", bodyKey: "club_id" },
};
`;
  const strayReassign = clean.replace(
    '"team:club": { perm: "manage_setup" },\n};',
    '"team:club": { perm: "manage_setup" },\n  "player:team": { perm: "manage_setup" },\n};');
  const strayCreate = clean.replace(
    'post("/api/v2/setup/team"', 'post("/api/setup/team"');

  const cleanViolations = checkSource(clean);
  if (cleanViolations.length) {
    throw new Error(`self-test: the clean fixture should have zero violations, got: ${
      JSON.stringify(cleanViolations)}`);
  }
  const strayReassignViolations = checkSource(strayReassign);
  if (!strayReassignViolations.length) {
    throw new Error("self-test: an unmapped REASSIGN entry (player:team, no " +
      "REASSIGN_V2 counterpart) was NOT flagged — the checker can't catch a real regression.");
  }
  const strayCreateViolations = checkSource(strayCreate);
  if (!strayCreateViolations.length) {
    throw new Error("self-test: a SETUP_POST entry reverted to a v1 URL was NOT " +
      "flagged — the checker can't catch a real regression.");
  }
  console.log("Self-test OK — the checker flags a stray REASSIGN entry and a stray " +
    "SETUP_POST v1 URL, and reports zero violations on a clean fixture.");
}

function main() {
  selfTest();
  const source = fs.readFileSync(APP_JS, "utf8");
  const violations = checkSource(source);
  if (violations.length) {
    console.error("v1 route contract violations:");
    violations.forEach((v) => console.error(`  - ${v}`));
    process.exitCode = 1;
    return;
  }
  console.log("v1 route contract OK — every REASSIGN entry has a REASSIGN_V2 " +
    "counterpart, and every SETUP_POST entry targets /api/v2/setup/...");
}

main();
