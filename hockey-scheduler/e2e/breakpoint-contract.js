// Responsive-breakpoint contract check (#345 batch — approved tokens).
//
// #345 approved exactly four responsive width tokens: 480px, 720px, 880px,
// and 1040px. This fails if any `@media` rule in any shipped stylesheet
// references a min-width/max-width outside that approved set.
//
// WHICH stylesheets is DISCOVERED, not declared. Two earlier versions of this
// check each shipped its own blind spot, and each one wrote the blind spot
// down in a comment instead of closing it:
//
//   1. the first scanned styles.css alone and recorded that onboarding.css and
//      setup.css carried non-conforming widths "flagged separately rather than
//      silently fixed" — which is how 760px and 520px stayed live in shipped
//      CSS with CI green;
//   2. the second replaced that with a hard-coded four-entry array and
//      conceded, in its own comment, that "a new one added here without being
//      listed would escape the contract" — the identical failure mode, moved
//      one level up.
//
// So the stylesheet set is now derived from the production HTML entry points
// themselves: every `<link rel="stylesheet">` in index.html and setup.html is
// parsed out, normalized, de-duplicated, and checked. Linking a new stylesheet
// puts it under contract immediately; there is no list to forget to update.
//
// Parsing is attribute-order-independent (`rel` before or after `href` are the
// same link), tolerates single/double/unquoted values and extra attributes,
// and strips `?query` and `#hash` suffixes so a cache-busted
// `href="/future.css?v=1"` resolves to the real file. A linked local
// stylesheet that cannot be resolved on disk fails loudly rather than being
// skipped. (`rel="preload" as="style"` is deliberately NOT treated as a
// stylesheet link — it is a fetch hint, not a render-blocking stylesheet, and
// none is used today.)
//
// Scanning is prelude-scoped: only text inside an `@media (...)` condition
// list is inspected for width tokens, so a `@media print` block (no width
// feature at all) is correctly ignored rather than matched as a violation, a
// plain code comment mentioning an unrelated pixel value never causes a false
// positive, and a CSS *property* that happens to carry a token-like value
// (`width: min(100%, 520px)`) is correctly not a breakpoint.
//
// Self-tests both halves — width extraction AND stylesheet discovery — against
// synthetic fixtures written to a temp directory before checking the real
// files, so a broken checker fails loudly rather than silently passing
// everything.
const fs = require("fs");
const os = require("os");
const path = require("path");

const STATIC_DIR = path.resolve(__dirname, "..", "backend", "hockey_scheduler",
  "web", "static");
// The production HTML entry points the server serves. These are the roots of
// discovery — not a stylesheet list.
const HTML_ENTRY_POINTS = ["index.html", "setup.html"];

const APPROVED_WIDTHS = new Set([480, 720, 880, 1040]);

// ---------------------------------------------------------------------------
// Width extraction
// ---------------------------------------------------------------------------

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

// The full width check, parameterized over source text so it can run against
// both real stylesheets and synthetic self-test fixtures.
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

function widthsIn(source) {
  const widths = new Set();
  extractMediaPreludes(source).forEach((prelude) => {
    extractWidths(prelude).forEach((w) => widths.add(w));
  });
  return widths.size
    ? [...widths].sort((a, b) => a - b).join(", ") : "(no width queries)";
}

// ---------------------------------------------------------------------------
// Stylesheet discovery
// ---------------------------------------------------------------------------

// Attributes of one tag, as a lowercase-keyed map — so `rel` before `href`
// and `href` before `rel` parse identically. Handles double-quoted,
// single-quoted, and unquoted values.
function parseAttributes(text) {
  const attrs = {};
  const re = /([a-zA-Z_:][-a-zA-Z0-9_:.]*)\s*=\s*("([^"]*)"|'([^']*)'|([^\s"'=<>`]+))/g;
  let m;
  while ((m = re.exec(text))) {
    const value = m[3] !== undefined ? m[3]
      : (m[4] !== undefined ? m[4] : m[5]);
    attrs[m[1].toLowerCase()] = value;
  }
  return attrs;
}

// Every stylesheet href in one HTML document. `rel` is a space-separated
// token list, so it is matched token-wise rather than by string equality.
function extractStylesheetHrefs(html) {
  const hrefs = [];
  const re = /<link\b([^>]*)>/gi;
  let m;
  while ((m = re.exec(html))) {
    const attrs = parseAttributes(m[1]);
    const rel = (attrs.rel || "").trim().toLowerCase().split(/\s+/);
    if (!rel.includes("stylesheet")) continue;
    if (attrs.href && attrs.href.trim()) hrefs.push(attrs.href.trim());
  }
  return hrefs;
}

// `/styles.css`, `./styles.css`, `styles.css?v=1`, `styles.css#x` all name the
// same local file. An absolute or protocol-relative URL is a remote sheet this
// checker cannot read from disk.
function normalizeHref(href) {
  if (!href) return null;
  if (/^[a-z][a-z0-9+.-]*:/i.test(href) || href.startsWith("//")) {
    return { external: true, href };
  }
  const name = href.split("#")[0].split("?")[0].replace(/^\.?\//, "");
  return name ? { name } : null;
}

// Ordered, de-duplicated stylesheet set across all entry points.
function discoverStylesheets(dir, entryPoints, failures) {
  const found = [];
  const seen = new Set();
  for (const entry of entryPoints) {
    const file = path.join(dir, entry);
    if (!fs.existsSync(file)) {
      failures.push(`${entry}: HTML entry point not found on disk — the ` +
        `discovery roots have drifted from the served files.`);
      continue;
    }
    for (const href of extractStylesheetHrefs(fs.readFileSync(file, "utf8"))) {
      const norm = normalizeHref(href);
      if (!norm) continue;
      const key = norm.external ? `url:${norm.href}` : norm.name;
      if (seen.has(key)) continue;
      seen.add(key);
      found.push(norm.external
        ? { external: true, href: norm.href, from: entry }
        : { name: norm.name, file: path.join(dir, norm.name), from: entry });
    }
  }
  return found;
}

// The whole contract, parameterized over a directory so the self-tests can run
// the REAL pipeline against synthetic fixtures rather than a reimplementation.
function runContract(dir, entryPoints) {
  const failures = [];
  const inventory = [];
  const sheets = discoverStylesheets(dir, entryPoints, failures);
  if (!sheets.length) {
    failures.push("no stylesheet was discovered from any HTML entry point, " +
      "so this check would pass vacuously.");
    return { failures, inventory, sheets };
  }
  for (const s of sheets) {
    if (s.external) {
      inventory.push(`${s.href}: (remote — not readable from disk, ` +
        `not checked; linked from ${s.from})`);
      continue;
    }
    if (!fs.existsSync(s.file)) {
      // Linked but unresolved: failing loudly beats quietly checking the rest.
      failures.push(`${s.name}: linked from ${s.from} but not found on disk`);
      continue;
    }
    const source = fs.readFileSync(s.file, "utf8");
    checkSource(source).forEach((v) => failures.push(`${s.name}: ${v}`));
    inventory.push(`${s.name}: ${widthsIn(source)}`);
  }
  return { failures, inventory, sheets };
}

// ---------------------------------------------------------------------------
// Self-tests
// ---------------------------------------------------------------------------

function selfTestWidths() {
  const clean = `
@media (max-width: 480px) { .a { display: none; } }
@media (min-width: 720px) and (max-width: 880px) { .b { display: block; } }
@media print { .c { display: none; } }
`;
  const strayMax = clean.replace("(max-width: 480px)", "(max-width: 460px)");
  const strayMin = clean.replace("(min-width: 720px)", "(min-width: 700px)");

  if (checkSource(clean).length) {
    throw new Error(`self-test: the clean fixture should have zero ` +
      `violations, got: ${JSON.stringify(checkSource(clean))}`);
  }
  if (!checkSource(strayMax).length) {
    throw new Error("self-test: a stray max-width (460px) was NOT flagged — " +
      "the checker can't catch a real regression.");
  }
  if (!checkSource(strayMin).length) {
    throw new Error("self-test: a stray min-width (700px) was NOT flagged — " +
      "the checker only detects max-width, not min-width.");
  }
  // A token-like value in a PROPERTY is not a breakpoint (setup.css really
  // ships `width: min(100%, 520px)`), so it must not be flagged.
  const propertyOnly = `.card { width: min(100%, 520px); }\n${clean}`;
  if (checkSource(propertyOnly).length) {
    throw new Error("self-test: a CSS property carrying a token-like value " +
      "was flagged as a breakpoint — the scan is not prelude-scoped.");
  }
  console.log("Self-test (widths) OK — flags a stray max-width and a stray " +
    "min-width, ignores non-width media and property values, and reports " +
    "zero violations on a clean fixture.");
}

function selfTestDiscovery() {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "breakpoint-contract-"));
  const write = (name, body) => fs.writeFileSync(path.join(dir, name), body);
  const CLEAN = "@media (max-width: 480px) { .a { display: none; } }\n";
  const DIRTY = "@media (max-width: 760px) { .b { display: none; } }\n";
  const expect = (label, cond, detail) => {
    if (!cond) throw new Error(`self-test (discovery): ${label} — ${detail}`);
  };

  try {
    // (1) Attribute order, quoting, query/hash suffixes, and extra attributes
    //     must all resolve to the same four local files, de-duplicated across
    //     both entry points.
    write("a.css", CLEAN);
    write("b.css", CLEAN);
    write("c.css", CLEAN);
    write("d.css", CLEAN);
    write("index.html", `<!DOCTYPE html><html><head>
      <link rel="stylesheet" href="/a.css" />
      <link href="/b.css" rel="stylesheet" />
      <link rel="stylesheet" href="/c.css?v=8f3a" />
      <link href='./d.css#top' rel='preconnect stylesheet' media="all" />
      <link rel="icon" href="/favicon.ico" />
      <link rel="preload" as="style" href="/not-a-sheet.css" />
      </head><body></body></html>`);
    // The duplicate link in the second entry point must collapse, not double.
    write("setup.html", `<!DOCTYPE html><html><head>
      <link href="/a.css" rel="stylesheet"></head><body></body></html>`);

    let r = runContract(dir, HTML_ENTRY_POINTS);
    expect("order/quote/suffix parsing", r.failures.length === 0,
      `expected zero failures, got ${JSON.stringify(r.failures)}`);
    const names = r.sheets.map((s) => s.name);
    expect("discovery set", JSON.stringify(names)
      === JSON.stringify(["a.css", "b.css", "c.css", "d.css"]),
      `rel-before-href, href-before-rel, a ?query href, a #hash href and ` +
      `single quotes must all resolve, non-stylesheet rels must not, and a ` +
      `duplicate across entry points must collapse. Got ${
        JSON.stringify(names)}`);

    // (2) A NEWLY LINKED stylesheet carrying a disallowed width must fail,
    //     naming that file. This is the exact blind spot a hard-coded list
    //     had: nothing here was edited except the HTML and the new file.
    write("future.css", DIRTY);
    fs.writeFileSync(path.join(dir, "index.html"),
      fs.readFileSync(path.join(dir, "index.html"), "utf8").replace(
        "</head>", `<link href="/future.css?v=1" rel="stylesheet"></head>`));
    r = runContract(dir, HTML_ENTRY_POINTS);
    expect("newly linked stylesheet",
      r.failures.some((f) => f.startsWith("future.css:") && f.includes("760")),
      `a newly linked stylesheet with a 760px query must fail naming it; ` +
      `got ${JSON.stringify(r.failures)}`);

    // (3) A linked-but-missing local stylesheet must fail loudly.
    fs.unlinkSync(path.join(dir, "future.css"));
    r = runContract(dir, HTML_ENTRY_POINTS);
    expect("unresolved stylesheet",
      r.failures.some((f) => f.startsWith("future.css:")
        && f.includes("not found on disk")),
      `a linked stylesheet missing from disk must fail; got ${
        JSON.stringify(r.failures)}`);

    // (4) Entry points that link nothing must not pass vacuously.
    write("index.html", "<!DOCTYPE html><html><head></head><body></body></html>");
    write("setup.html", "<!DOCTYPE html><html><head></head><body></body></html>");
    r = runContract(dir, HTML_ENTRY_POINTS);
    expect("vacuity guard", r.failures.some((f) => f.includes("vacuously")),
      `zero discovered stylesheets must fail, not pass; got ${
        JSON.stringify(r.failures)}`);

    // (5) A missing entry point must fail rather than silently narrow scope.
    fs.unlinkSync(path.join(dir, "setup.html"));
    r = runContract(dir, HTML_ENTRY_POINTS);
    expect("missing entry point",
      r.failures.some((f) => f.startsWith("setup.html:")),
      `a missing HTML entry point must fail; got ${
        JSON.stringify(r.failures)}`);
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
  console.log("Self-test (discovery) OK — parses rel before/after href, " +
    "single/double quotes, ?query and #hash hrefs; ignores non-stylesheet " +
    "rels; de-duplicates across entry points; and fails on a newly linked " +
    "out-of-contract stylesheet, an unresolved stylesheet, a missing entry " +
    "point, and a vacuous empty set.");
}

// ---------------------------------------------------------------------------

function main() {
  selfTestWidths();
  selfTestDiscovery();

  const { failures, inventory, sheets } = runContract(STATIC_DIR,
    HTML_ENTRY_POINTS);
  if (failures.length) {
    console.error("Breakpoint contract violations:");
    failures.forEach((v) => console.error(`  - ${v}`));
    process.exitCode = 1;
    return;
  }
  console.log(`Breakpoint contract OK — ${sheets.length} stylesheet(s) `
    + `discovered from ${HTML_ENTRY_POINTS.join(" + ")}, and every @media `
    + `width in all of them is one of the four approved tokens `
    + `(480/720/880/1040).`);
  console.log("Final query inventory:");
  inventory.forEach((line) => console.log(`  ${line}`));
}

main();
