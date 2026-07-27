// Behavior evidence for the #345 breakpoint-contract batch: proves the two
// corrected boundaries (Game Sheet 680px -> 720px, Ice Builder 460px ->
// 480px) actually activate at the RIGHT width, using the real production
// stylesheet's computed layout -- not just a media-query string match.
//
// No backend server is needed: this loads styles.css itself (via
// page.addStyleTag({ path })) into a blank page and creates representative
// nodes with the exact production class names the two changed rules target,
// then reads computed style/geometry at each width. A regex check (see
// breakpoint-contract.js) can prove the SOURCE only says "720px"; this
// proves the BROWSER actually collapses/restores the grid at that width.
//
// Boundary widths, one px each side of both changed thresholds:
//   479 / 480 / 481 -- Ice Builder (.ib-grid2, .ib-day-row, .mo-cell)
//   719 / 720 / 721 -- Game Sheet (.gs-grid, .gs-off-grid)
// Plus canonical phone (390x844) and desktop (1440x900): no horizontal page
// overflow, zero console/page errors.
const { chromium } = require("playwright");
const path = require("path");

const STYLES_CSS = path.resolve(__dirname, "..", "backend", "hockey_scheduler",
  "web", "static", "styles.css");

const FIXTURE_HTML = `<!DOCTYPE html><html><head><meta charset="utf-8"></head><body>
  <div class="gs-grid"><div>home</div><div>away</div></div>
  <div class="gs-off-grid"><div>referee</div><div>linesman</div><div>timekeeper</div></div>
  <div class="ib-grid2"><div>col-a</div><div>col-b</div></div>
  <div class="ib-day-row"><div>date</div><div>slots</div></div>
  <div class="mo-cell"></div>
</body></html>`;

function fail(msg) { throw new Error(msg); }

// grid-template-columns' computed value is a space-separated px list, one
// token per column -- counting tokens is robust to the actual px numbers,
// which depend on container width.
function colCount(computedGridTemplateColumns) {
  return computedGridTemplateColumns.trim().split(/\s+/).length;
}

async function readLayout(page) {
  return page.evaluate(() => {
    const cs = (sel) => getComputedStyle(document.querySelector(sel));
    return {
      gsGridCols: cs(".gs-grid").gridTemplateColumns,
      gsOffGridCols: cs(".gs-off-grid").gridTemplateColumns,
      ibGrid2Cols: cs(".ib-grid2").gridTemplateColumns,
      ibDayRowDirection: cs(".ib-day-row").flexDirection,
      moCellMinHeight: cs(".mo-cell").minHeight,
    };
  });
}

async function withFixturePage(browser, width, height, run) {
  const context = await browser.newContext({ viewport: { width, height } });
  const page = await context.newPage();
  const errors = [];
  page.on("pageerror", (e) => errors.push(`[pageerror] ${e.message}`));
  page.on("console", (m) => { if (m.type() === "error") errors.push(`[console] ${m.text()}`); });
  try {
    await page.setContent(FIXTURE_HTML);
    await page.addStyleTag({ path: STYLES_CSS });
    await run(page, errors);
  } finally {
    await context.close();
  }
  return errors;
}

async function checkIceBuilderBoundary(browser, width, expectStacked) {
  const errors = await withFixturePage(browser, width, 800, async (page) => {
    const l = await readLayout(page);
    const ibCols = colCount(l.ibGrid2Cols);
    if (expectStacked) {
      if (ibCols !== 1) {
        fail(`Ice Builder @${width}px: expected .ib-grid2 stacked to 1 column, ` +
          `got ${ibCols} (grid-template-columns: "${l.ibGrid2Cols}")`);
      }
      if (l.ibDayRowDirection !== "column") {
        fail(`Ice Builder @${width}px: expected .ib-day-row flex-direction: column, ` +
          `got "${l.ibDayRowDirection}"`);
      }
      if (l.moCellMinHeight !== "52px") {
        fail(`Ice Builder @${width}px: expected .mo-cell min-height: 52px, ` +
          `got "${l.moCellMinHeight}"`);
      }
    } else {
      if (ibCols !== 2) {
        fail(`Ice Builder @${width}px: expected .ib-grid2 at its wider 2-column ` +
          `layout, got ${ibCols} (grid-template-columns: "${l.ibGrid2Cols}")`);
      }
      if (l.ibDayRowDirection !== "row") {
        fail(`Ice Builder @${width}px: expected .ib-day-row flex-direction: row, ` +
          `got "${l.ibDayRowDirection}"`);
      }
      if (l.moCellMinHeight !== "64px") {
        fail(`Ice Builder @${width}px: expected .mo-cell min-height: 64px, ` +
          `got "${l.moCellMinHeight}"`);
      }
    }
  });
  if (errors.length) fail(`Ice Builder @${width}px: console/page errors: ${errors.join("; ")}`);
}

async function checkGameSheetBoundary(browser, width, expectStacked) {
  const errors = await withFixturePage(browser, width, 800, async (page) => {
    const l = await readLayout(page);
    const gsCols = colCount(l.gsGridCols);
    const gsOffCols = colCount(l.gsOffGridCols);
    if (expectStacked) {
      if (gsCols !== 1) {
        fail(`Game Sheet @${width}px: expected .gs-grid stacked to 1 column, ` +
          `got ${gsCols} (grid-template-columns: "${l.gsGridCols}")`);
      }
      if (gsOffCols !== 1) {
        fail(`Game Sheet @${width}px: expected .gs-off-grid stacked to 1 column, ` +
          `got ${gsOffCols} (grid-template-columns: "${l.gsOffGridCols}")`);
      }
    } else {
      if (gsCols !== 2) {
        fail(`Game Sheet @${width}px: expected .gs-grid at its wider 2-column ` +
          `layout, got ${gsCols} (grid-template-columns: "${l.gsGridCols}")`);
      }
      if (gsOffCols !== 3) {
        fail(`Game Sheet @${width}px: expected .gs-off-grid at its wider 3-column ` +
          `layout, got ${gsOffCols} (grid-template-columns: "${l.gsOffGridCols}")`);
      }
    }
  });
  if (errors.length) fail(`Game Sheet @${width}px: console/page errors: ${errors.join("; ")}`);
}

async function checkNoOverflowNoErrors(browser, label, width, height) {
  const errors = await withFixturePage(browser, width, height, async (page) => {
    const overflow = await page.evaluate(() => ({
      scrollWidth: document.documentElement.scrollWidth,
      clientWidth: document.documentElement.clientWidth,
    }));
    if (overflow.scrollWidth > overflow.clientWidth) {
      fail(`${label}: horizontal page overflow -- scrollWidth ${overflow.scrollWidth} ` +
        `> clientWidth ${overflow.clientWidth}`);
    }
  });
  if (errors.length) fail(`${label}: console/page errors: ${errors.join("; ")}`);
}

(async () => {
  const browser = await chromium.launch();
  try {
    // Ice Builder boundary: stacked at/below 480, restored above.
    await checkIceBuilderBoundary(browser, 479, true);
    await checkIceBuilderBoundary(browser, 480, true);
    await checkIceBuilderBoundary(browser, 481, false);

    // Game Sheet boundary: stacked at/below 720, restored above.
    await checkGameSheetBoundary(browser, 719, true);
    await checkGameSheetBoundary(browser, 720, true);
    await checkGameSheetBoundary(browser, 721, false);

    // Canonical viewports: no horizontal overflow, no console/page errors.
    await checkNoOverflowNoErrors(browser, "phone 390x844", 390, 844);
    await checkNoOverflowNoErrors(browser, "desktop 1440x900", 1440, 900);

    console.log("Breakpoint boundaries OK -- Ice Builder stacks at/below 480px "
      + "and restores above it; Game Sheet stacks at/below 720px and restores "
      + "above it; no horizontal overflow or console/page errors at 390x844 "
      + "or 1440x900.");
  } finally {
    await browser.close();
  }
})().catch((e) => {
  console.error("Breakpoint boundaries journey FAILED.");
  console.error(e.message);
  process.exit(1);
});
