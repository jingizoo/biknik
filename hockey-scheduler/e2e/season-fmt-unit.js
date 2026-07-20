// Focused unit regression for the Season date formatter (#272 review).
//
// fmtDateInTz() renders a stored UTC instant as a calendar date in the
// Program's timezone. A legacy/invalid stored zone must fall back to UTC — the
// same fallback create_season/parse_season_boundary use — so a validly-stored
// boundary is shown as its UTC calendar day, never hidden (returned null and
// dropped from the Seasons card + competition tree).
//
// The real functions are extracted from app.js so this can never drift from the
// shipped source. Pure JS (Node ICU), no browser required.
const fs = require("fs");
const path = require("path");
const assert = require("assert");

const APP_JS = path.resolve(
  __dirname, "..", "backend", "hockey_scheduler", "web", "static", "app.js");

function extract(src, name) {
  const match = src.match(
    new RegExp(`const ${name} = \\([^)]*\\) => \\{[\\s\\S]*?\\n\\};`));
  if (!match) throw new Error(`could not extract ${name} from app.js`);
  return match[0];
}

function main() {
  const src = fs.readFileSync(APP_JS, "utf8");
  const fmtSrc = extract(src, "fmtDateInTz");
  const rangeSrc = extract(src, "seasonDateRange");
  // eslint-disable-next-line no-new-func
  const fmtDateInTz = new Function(`${fmtSrc}\nreturn fmtDateInTz;`)();
  // eslint-disable-next-line no-new-func
  const seasonDateRange = new Function(
    `${fmtSrc}\n${rangeSrc}\nreturn seasonDateRange;`)();

  const iso = "2026-09-15T00:00:00+00:00"; // date-only Season anchored under UTC
  const utc = fmtDateInTz(iso, "UTC");

  // The crux: an unresolvable Program timezone must NOT hide the boundary — it
  // falls back to the UTC calendar day, identical to an explicit UTC render.
  const invalid = fmtDateInTz(iso, "Not/AZone");
  assert.ok(invalid !== null, "invalid timezone must not return null");
  assert.strictEqual(invalid, utc, "invalid timezone must render the UTC day");
  assert.ok(/2026/.test(invalid) && /15/.test(invalid),
    `expected the 2026-09-15 calendar day, got "${invalid}"`);

  // Blank / missing zone also defaults to UTC, and a null instant stays null.
  assert.strictEqual(fmtDateInTz(iso, ""), utc, "blank zone → UTC");
  assert.strictEqual(fmtDateInTz(iso, undefined), utc, "missing zone → UTC");
  assert.strictEqual(fmtDateInTz(null, "UTC"), null, "null instant → null");

  // A resolvable zone is still honored (no blanket UTC): Tokyo (+09) keeps the
  // 15th for a UTC-midnight instant, New_York (-04) rolls back to the 14th.
  assert.ok(/15/.test(fmtDateInTz(iso, "Asia/Tokyo")), "Tokyo shows the 15th");
  assert.ok(/14/.test(fmtDateInTz(iso, "America/New_York")),
    "New_York shows the adjacent 14th");

  // seasonDateRange composes two boundaries; with an invalid zone it must still
  // build a range rather than collapse to null.
  const range = seasonDateRange(
    { start_date: iso, end_date: "2026-09-20T00:00:00+00:00" }, "Not/AZone");
  assert.ok(range && /–/.test(range),
    `expected a rendered range for an invalid zone, got "${range}"`);

  console.log("season-fmt-unit: OK — invalid Program timezone falls back to UTC.");
}

try {
  main();
} catch (error) {
  console.error("season-fmt-unit FAILED.");
  console.error(error && error.message ? error.message : error);
  process.exitCode = 1;
}
