// Responsive-breakpoint contract check (#345 batch — approved tokens).
//
// #345 approved exactly four responsive width tokens: 480px, 720px, 880px,
// and 1040px. This reads the REAL production stylesheet (styles.css — the
// file named in the batch that introduced this check, and where both of its
// known violations lived: Game Sheet's `680px` and Ice Builder's `460px`)
// and fails if any `@media` rule references a min-width/max-width outside
// that approved set.
//
// Scanning is prelude-scoped: only text inside an `@media (...)` condition
// list is inspected for width tokens, so a `@media print` block (no width
// feature at all) is correctly ignored rather than matched as a violation,
// and a plain code comment mentioning an unrelated pixel value never causes
// a false positive.
//
// Scope: EVERY production stylesheet the app ships, not just styles.css.
//
// The earlier version deliberately scanned styles.css alone and recorded, in
// this very comment, that onboarding.css and setup.css carried non-conforming
// widths which were "flagged separately rather than silently fixed". That is
// exactly how those two survived: a contract that names its own blind spot
// still has the blind spot, and CI stayed green with 760px and 520px live in
// shipped CSS. Both are retokenized now (720 and 480), and the checker covers
// all four stylesheets so the gap cannot silently reopen.
//
// Self-tests its own extraction against synthetic fixtures before checking
// the real files (same convention as check-v1-route-contract.js), so a
// broken checker fails loudly rather than silently passing everything.
const fs = require("fs");
const path = require("path");

const STATIC_DIR = path.resolve(__dirname, "..", "backend", "hockey_scheduler",
  "web", "static");
// Every stylesheet served to a browser. A new one added here without being
// listed would escape the contract, so the list is asserted non-empty and
// each entry is required to exist.
const STYLESHEETS = ["styles.css", "web.css", "onboarding.css", "setup.css"];

const APPROVED_WIDTHS = new Set([480, 720, 880, 1040]);

// Every `@media (...)` prelude (the text between `@media` and the rule's
// opening `{`), e.g. "print", "(max-width: 680px)",
// "(min-width: 480px) and (max-width: 880px)".
function extractMediaPreludes(source) {
  const preludes = [];
  const re = /@media\s+([^{]+?)\s*\{/g;
  let m;
  while ((m = re.exec(source))) preludes.push(m[1].trim());
  return preludes;
}

// Every `min-width`/`max-width` token's pixel value within one prelude.
// Deliberately does not care which of min/max it is — either can carry an
// out-of-contract value.
function extractWidths(prelude) {
  const widths = [];
  const re = /(?:min|max)-width\s*:\s*(\d+)px/g;
  let m;
  while ((m = re.exec(prelude))) widths.push(Number(m[1]));
  return widths;
}

// The full check, parameterized over source text so it can run against both
// the real stylesheet and synthetic self-test fixtures.
function checkSource(source) {
  const violations = [];
  const preludes = extractMediaPreludes(source);
  if (!preludes.length) {
    violations.push("extraction found zero @media rules — the checker's " +
      "regex may be out of sync with the stylesheet's structure.");
    return violations;
  }
  for (const prelude of preludes) {
    const widths = extractWidths(prelude);
    // A prelude with no width feature at all (e.g. `print`,
    // `prefers-color-scheme: dark`) is non-width media — not this
    // contract's concern, and correctly not a violation.
    if (!widths.length) continue;
    for (const w of widths) {
      if (!APPROVED_WIDTHS.has(w)) {
        violations.push(`@media ${prelude} uses ${w}px, which is not one ` +
          `of the four approved tokens (480/720/880/1040).`);
      }
    }
  }
  return violations;
}

function selfTest() {
  const clean = `
@media (max-width: 480px) { .a { display: none; } }
@media (min-width: 720px) and (max-width: 880px) { .b { display: block; } }
@media print { .c { display: none; } }
`;
  const strayMax = clean.replace("(max-width: 480px)", "(max-width: 460px)");
  const strayMin = clean.replace("(min-width: 720px)", "(min-width: 700px)");

  const cleanViolations = checkSource(clean);
  if (cleanViolations.length) {
    throw new Error(`self-test: the clean fixture should have zero violations, got: ${
      JSON.stringify(cleanViolations)}`);
  }
  const strayMaxViolations = checkSource(strayMax);
  if (!strayMaxViolations.length) {
    throw new Error("self-test: a stray max-width (460px) was NOT flagged — " +
      "the checker can't catch a real regression.");
  }
  const strayMinViolations = checkSource(strayMin);
  if (!strayMinViolations.length) {
    throw new Error("self-test: a stray min-width (700px) was NOT flagged — " +
      "the checker only detects max-width, not min-width.");
  }
  console.log("Self-test OK — the checker flags a stray max-width, a stray " +
    "min-width, ignores non-width media (print), and reports zero " +
    "violations on a clean fixture.");
}

function main() {
  selfTest();
  if (!STYLESHEETS.length) {
    console.error("Breakpoint contract: the stylesheet list is empty, so this "
      + "check would pass vacuously.");
    process.exitCode = 1;
    return;
  }
  const failures = [];
  const inventory = [];
  for (const name of STYLESHEETS) {
    const file = path.join(STATIC_DIR, name);
    if (!fs.existsSync(file)) {
      // A listed-but-missing stylesheet means the list drifted from reality;
      // failing loudly beats quietly checking three of four files.
      failures.push(`${name}: listed in STYLESHEETS but not found on disk`);
      continue;
    }
    const source = fs.readFileSync(file, "utf8");
    checkSource(source).forEach((v) => failures.push(`${name}: ${v}`));
    const widths = new Set();
    extractMediaPreludes(source).forEach((prelude) => {
      const re = /(?:min|max)-width\s*:\s*(\d+)px/g;
      let m;
      while ((m = re.exec(prelude))) widths.add(Number(m[1]));
    });
    inventory.push(`${name}: ${widths.size
      ? [...widths].sort((a, b) => a - b).join(", ") : "(no width queries)"}`);
  }
  if (failures.length) {
    console.error("Breakpoint contract violations:");
    failures.forEach((v) => console.error(`  - ${v}`));
    process.exitCode = 1;
    return;
  }
  console.log("Breakpoint contract OK — every @media width in every shipped "
    + "stylesheet is one of the four approved tokens (480/720/880/1040).");
  console.log("Final query inventory:");
  inventory.forEach((line) => console.log(`  ${line}`));
}

main();
