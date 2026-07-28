// Behavior evidence for the #345 breakpoint-contract batch: proves that each
// of the four approved width tokens (480 / 720 / 880 / 1040) actually switches
// layout at the RIGHT width, using the real production stylesheets' computed
// layout -- not just a media-query string match.
//
// breakpoint-contract.js can only prove the SOURCE says "720px". This proves
// the BROWSER collapses and restores at that width, one pixel each side.
//
// No backend server is needed: this loads the real stylesheets themselves (via
// page.addStyleTag({ path })) into a blank page, with the same per-page
// stylesheet SET the corresponding production page links -- index.html loads
// styles.css + web.css + onboarding.css, setup.html loads setup.css alone --
// so no fixture sees a combination a browser never sees.
//
// The seven-area navigation fixture is NOT hand-written: the real
// `<nav class="side-nav">` element is extracted from index.html at runtime, so
// a nav change in production cannot silently drift away from what is asserted
// here. Its inline `style="display:none"` gates are stripped, because those are
// per-role gates applied by app.js at runtime and the width-stress worth
// proving is the WIDEST case -- a League Admin, who sees every destination.
//
// Boundary widths, one px each side of all four tokens:
//   479 / 480 / 481    -- Ice Builder (styles.css), phone shell + 16px inputs
//                         (web.css/styles.css), sign-in card (setup.css)
//   719 / 720 / 721    -- Game Sheet (styles.css), onboarding hero
//                         (onboarding.css)
//   879 / 880 / 881    -- app shell + seven-area nav (web.css)
//   1039 / 1040 / 1041 -- dashboard grids (web.css)
//
// Plus, at the collapsed-nav widths (879 and canonical phone 390x844): no
// horizontal page overflow, no destination clipped out of the scroll strip or
// rendered non-visible, and a real Tab walk reaches every destination. And at
// canonical phone (390x844) and desktop (1440x900): no horizontal page
// overflow, zero console/page errors.
const { chromium } = require("playwright");
const fs = require("fs");
const path = require("path");

const STATIC_DIR = path.resolve(__dirname, "..", "backend", "hockey_scheduler",
  "web", "static");
const sheet = (name) => path.join(STATIC_DIR, name);

// The exact stylesheet sets the production pages link (asserted in
// breakpoint-contract.js' inventory; see the <link> tags in each HTML file).
const INDEX_SHEETS = [sheet("styles.css"), sheet("web.css"),
  sheet("onboarding.css")];
const SETUP_SHEETS = [sheet("setup.css")];

function fail(msg) { throw new Error(msg); }

// ---------------------------------------------------------------------------
// Real production nav, lifted out of index.html rather than re-typed here.
// ---------------------------------------------------------------------------
function productionNavHtml() {
  const html = fs.readFileSync(path.join(STATIC_DIR, "index.html"), "utf8");
  const m = html.match(/<nav class="side-nav">[\s\S]*?<\/nav>/);
  if (!m) {
    fail("could not find `<nav class=\"side-nav\">` in index.html -- this " +
      "fixture extracts the REAL nav, so a markup change must fail loudly " +
      "here rather than silently fall back to a stale hand-written copy.");
  }
  const nav = m[0];
  const groups = (nav.match(/data-nav-area="/g) || []).length;
  if (groups !== 7) {
    fail(`expected the approved seven-area IA (#345), found ${groups} ` +
      `\`data-nav-area\` groups in index.html's nav.`);
  }
  // Strip the per-role gates so the strip is stressed at its widest.
  return nav.replace(/\s*style="display:none"/g, "");
}

// The app shell: the real sidebar/nav plus a representative dashboard body,
// exactly as index.html composes them (`.web` > `.sidebar` + `.main`).
function shellHtml() {
  return `<!DOCTYPE html><html><head><meta charset="utf-8"></head>
<body class="web-body" data-view="dashboard"><div class="web">
  <aside class="sidebar">
    <div class="brand"><span class="brand-mark" aria-hidden="true">H</span>
      <div><div class="brand-name">Hockey Scheduler</div>
        <div class="brand-sub">Operator Console</div></div></div>
    ${productionNavHtml()}
    <div class="side-foot"><label class="side-switch">
      <span class="side-switch-label">Signed in as</span>
      <select id="role-switch"><option>League Admin</option></select>
    </label></div>
  </aside>
  <main class="main">
    <header class="topbar"><div class="topbar-title">
      <div class="topbar-heading"><h1 id="nav-title">Dashboard</h1></div>
    </div><div class="topbar-actions"></div></header>
    <div class="content">
      <div class="dash-stats">
        <div class="dash-stat"><div class="ds-label">Teams</div></div>
        <div class="dash-stat"><div class="ds-label">Games</div></div>
        <div class="dash-stat"><div class="ds-label">Rinks</div></div>
        <div class="dash-stat"><div class="ds-label">Players</div></div>
      </div>
      <div class="dash-grid">
        <div class="dash-card">main</div>
        <div class="dash-card">aside</div>
      </div>
    </div>
  </main>
</div></body></html>`;
}

// styles.css-only fixture: the two rules #345 originally retokenized.
const GRID_HTML = `<!DOCTYPE html><html><head><meta charset="utf-8"></head><body>
  <div class="gs-grid"><div>home</div><div>away</div></div>
  <div class="gs-off-grid"><div>referee</div><div>linesman</div><div>timekeeper</div></div>
  <div class="ib-grid2"><div>col-a</div><div>col-b</div></div>
  <div class="ib-day-row"><div>date</div><div>slots</div></div>
  <div class="mo-cell"></div>
</body></html>`;

// onboarding.css hero (index.html's stylesheet set).
const ONBOARDING_HTML = `<!DOCTYPE html><html><head><meta charset="utf-8"></head>
<body data-view="onboarding"><div class="content"><div class="onboarding-shell">
  <div class="onboarding-hero">
    <div><h2>Guided setup</h2><p>Get the season ready.</p></div>
    <div class="onboarding-progress-card"><strong>3</strong><span>of 7</span></div>
  </div>
</div></div></body></html>`;

// setup.css sign-in card (setup.html's stylesheet set -- setup.css alone).
const SETUP_HTML = `<!DOCTYPE html><html><head><meta charset="utf-8"></head><body>
  <div class="setup-shell"><div class="setup-card">
    <p class="eyebrow">Setup</p><h1>Claim this install</h1>
    <form><label>Email<input type="email" /></label></form>
  </div></div>
</body></html>`;

// Every control the ≤480 rules promise to lift to 16px (iOS Safari auto-zooms
// any focused form control below 16px). Sources: web.css `@media (max-width:
// 480px)` and styles.css' two `@media (max-width: 480px)` blocks.
const SIXTEEN_PX_INPUTS = [
  ".login-field input", ".side-switch select", ".avail-form input",
  ".avail-form select", ".cd-input", ".drawer-body input",
  ".drawer-body select", ".cal-filters select", ".import-form select",
  ".ctx-select",
];
const INPUTS_HTML = `<!DOCTYPE html><html><head><meta charset="utf-8"></head>
<body class="web-body"><div class="web"><div class="sidebar">
  <label class="side-switch"><select><option>a</option></select></label>
</div><div class="main">
  <label class="login-field"><span>Email</span><input type="email" /></label>
  <div class="avail-form"><input type="text" /><select><option>a</option></select></div>
  <input class="cd-input" type="text" />
  <div class="drawer-body"><input type="text" /><select><option>a</option></select></div>
  <div class="cal-filters"><select><option>a</option></select></div>
  <div class="import-form"><select><option>a</option></select>
    <textarea rows="2"></textarea></div>
  <select class="ctx-select"><option>a</option></select>
</div></div></body></html>`;

// grid-template-columns' computed value is a space-separated px list, one
// token per column -- counting tokens is robust to the actual px numbers,
// which depend on container width.
function colCount(computedGridTemplateColumns) {
  return computedGridTemplateColumns.trim().split(/\s+/).length;
}

async function withFixturePage(browser, { width, height, html, sheets }, run) {
  const context = await browser.newContext({ viewport: { width, height } });
  const page = await context.newPage();
  const errors = [];
  page.on("pageerror", (e) => errors.push(`[pageerror] ${e.message}`));
  page.on("console", (m) => { if (m.type() === "error") errors.push(`[console] ${m.text()}`); });
  try {
    await page.setContent(html);
    for (const s of sheets) await page.addStyleTag({ path: s });
    await run(page, errors);
  } finally {
    await context.close();
  }
  return errors;
}

// A fixture leg: run `body`, then require zero console/page errors.
async function leg(browser, label, opts, body) {
  const errors = await withFixturePage(browser, opts, body);
  if (errors.length) fail(`${label}: console/page errors: ${errors.join("; ")}`);
}

// ---------------------------------------------------------------------------
// 480 -- Ice Builder (styles.css)
// ---------------------------------------------------------------------------
async function checkIceBuilderBoundary(browser, width, expectStacked) {
  const label = `Ice Builder @${width}px`;
  await leg(browser, label,
    { width, height: 800, html: GRID_HTML, sheets: [sheet("styles.css")] },
    async (page) => {
      const l = await page.evaluate(() => {
        const cs = (s) => getComputedStyle(document.querySelector(s));
        return {
          ibGrid2Cols: cs(".ib-grid2").gridTemplateColumns,
          ibDayRowDirection: cs(".ib-day-row").flexDirection,
          moCellMinHeight: cs(".mo-cell").minHeight,
        };
      });
      const ibCols = colCount(l.ibGrid2Cols);
      const want = expectStacked
        ? { cols: 1, dir: "column", minH: "52px" }
        : { cols: 2, dir: "row", minH: "64px" };
      if (ibCols !== want.cols) {
        fail(`${label}: expected .ib-grid2 at ${want.cols} column(s), got ` +
          `${ibCols} (grid-template-columns: "${l.ibGrid2Cols}")`);
      }
      if (l.ibDayRowDirection !== want.dir) {
        fail(`${label}: expected .ib-day-row flex-direction: ${want.dir}, ` +
          `got "${l.ibDayRowDirection}"`);
      }
      if (l.moCellMinHeight !== want.minH) {
        fail(`${label}: expected .mo-cell min-height: ${want.minH}, got ` +
          `"${l.moCellMinHeight}"`);
      }
    });
}

// ---------------------------------------------------------------------------
// 720 -- Game Sheet (styles.css)
// ---------------------------------------------------------------------------
async function checkGameSheetBoundary(browser, width, expectStacked) {
  const label = `Game Sheet @${width}px`;
  await leg(browser, label,
    { width, height: 800, html: GRID_HTML, sheets: [sheet("styles.css")] },
    async (page) => {
      const l = await page.evaluate(() => {
        const cs = (s) => getComputedStyle(document.querySelector(s));
        return {
          gsGridCols: cs(".gs-grid").gridTemplateColumns,
          gsOffGridCols: cs(".gs-off-grid").gridTemplateColumns,
        };
      });
      const want = expectStacked ? { grid: 1, off: 1 } : { grid: 2, off: 3 };
      const gsCols = colCount(l.gsGridCols);
      const gsOffCols = colCount(l.gsOffGridCols);
      if (gsCols !== want.grid) {
        fail(`${label}: expected .gs-grid at ${want.grid} column(s), got ` +
          `${gsCols} (grid-template-columns: "${l.gsGridCols}")`);
      }
      if (gsOffCols !== want.off) {
        fail(`${label}: expected .gs-off-grid at ${want.off} column(s), got ` +
          `${gsOffCols} (grid-template-columns: "${l.gsOffGridCols}")`);
      }
    });
}

// ---------------------------------------------------------------------------
// 720 -- onboarding hero (onboarding.css, retokenized from 760 in this slice)
// ---------------------------------------------------------------------------
async function checkOnboardingBoundary(browser, width, expectStacked) {
  const label = `Onboarding hero @${width}px`;
  await leg(browser, label,
    { width, height: 800, html: ONBOARDING_HTML, sheets: INDEX_SHEETS },
    async (page) => {
      const l = await page.evaluate(() => ({
        heroCols: getComputedStyle(document.querySelector(".onboarding-hero"))
          .gridTemplateColumns,
        scrollWidth: document.documentElement.scrollWidth,
        clientWidth: document.documentElement.clientWidth,
      }));
      const cols = colCount(l.heroCols);
      const want = expectStacked ? 1 : 2;
      if (cols !== want) {
        fail(`${label}: expected .onboarding-hero at ${want} column(s), got ` +
          `${cols} (grid-template-columns: "${l.heroCols}")`);
      }
      // The 721-760px band now keeps the two-column hero (it stacked at 760
      // before this slice's retokenization). That band is exactly where an
      // overflow would show up if the wider layout did not fit, so assert it.
      if (l.scrollWidth > l.clientWidth) {
        fail(`${label}: horizontal page overflow -- scrollWidth ` +
          `${l.scrollWidth} > clientWidth ${l.clientWidth}`);
      }
    });
}

// ---------------------------------------------------------------------------
// 480 -- sign-in card (setup.css, retokenized from 520 in this slice)
// ---------------------------------------------------------------------------
async function checkSetupCardBoundary(browser, width, expectFullBleed) {
  const label = `Setup card @${width}px`;
  await leg(browser, label,
    { width, height: 800, html: SETUP_HTML, sheets: SETUP_SHEETS },
    async (page) => {
      const l = await page.evaluate(() => {
        const cs = (s) => getComputedStyle(document.querySelector(s));
        const card = cs(".setup-card");
        return {
          borderTopWidth: card.borderTopWidth,
          borderRadius: card.borderTopLeftRadius,
          placeItems: cs(".setup-shell").alignItems,
          cardWidth: document.querySelector(".setup-card").offsetWidth,
          scrollWidth: document.documentElement.scrollWidth,
          clientWidth: document.documentElement.clientWidth,
        };
      });
      if (expectFullBleed) {
        if (l.borderTopWidth !== "0px" || l.borderRadius !== "0px") {
          fail(`${label}: expected the full-bleed card (border 0, radius 0), ` +
            `got border-top-width "${l.borderTopWidth}", radius ` +
            `"${l.borderRadius}"`);
        }
        if (l.placeItems !== "stretch") {
          fail(`${label}: expected .setup-shell align-items: stretch, got ` +
            `"${l.placeItems}"`);
        }
      } else {
        if (l.borderTopWidth === "0px" || l.borderRadius === "0px") {
          fail(`${label}: expected the bordered/rounded card above the phone ` +
            `token, got border-top-width "${l.borderTopWidth}", radius ` +
            `"${l.borderRadius}"`);
        }
        if (l.placeItems !== "center") {
          fail(`${label}: expected .setup-shell align-items: center, got ` +
            `"${l.placeItems}"`);
        }
      }
      // The 481-520px band now renders the padded card rather than the
      // full-bleed one; prove that band still fits inside the viewport.
      if (l.scrollWidth > l.clientWidth) {
        fail(`${label}: horizontal page overflow -- scrollWidth ` +
          `${l.scrollWidth} > clientWidth ${l.clientWidth} (card rendered ` +
          `${l.cardWidth}px)`);
      }
    });
}

// ---------------------------------------------------------------------------
// 880 -- app shell + seven-area nav (web.css)
// ---------------------------------------------------------------------------
async function checkShellBoundary(browser, width, expectCollapsed) {
  const label = `App shell @${width}px`;
  await leg(browser, label,
    { width, height: 900, html: shellHtml(), sheets: INDEX_SHEETS },
    async (page) => {
      const l = await page.evaluate(() => {
        const cs = (s) => getComputedStyle(document.querySelector(s));
        return {
          webDirection: cs(".web").flexDirection,
          navDirection: cs(".side-nav").flexDirection,
          navOverflowX: cs(".side-nav").overflowX,
          groupDirection: cs(".nav-group").flexDirection,
          groupPosition: cs(".nav-group").position,
          labelPosition: cs(".nav-group-label").position,
          labelDisplay: cs(".nav-group-label").display,
          labelWidth: cs(".nav-group-label").width,
        };
      });
      if (expectCollapsed) {
        if (l.webDirection !== "column") {
          fail(`${label}: expected .web flex-direction: column, got ` +
            `"${l.webDirection}"`);
        }
        if (l.navDirection !== "row") {
          fail(`${label}: expected .side-nav flex-direction: row (the ` +
            `horizontal strip), got "${l.navDirection}"`);
        }
        if (l.navOverflowX !== "auto") {
          fail(`${label}: expected .side-nav overflow-x: auto so the strip ` +
            `scrolls rather than clipping, got "${l.navOverflowX}"`);
        }
        if (l.groupDirection !== "row") {
          fail(`${label}: expected .nav-group flex-direction: row, got ` +
            `"${l.groupDirection}"`);
        }
        // `position: relative` is load-bearing here (see web.css): without it
        // the absolutely-positioned label escapes the scroller and drags the
        // document's scrollWidth out.
        if (l.groupPosition !== "relative") {
          fail(`${label}: expected .nav-group position: relative (it is the ` +
            `containing block for the absolutely-positioned area label), ` +
            `got "${l.groupPosition}"`);
        }
        // Visually hidden but STILL IN the accessibility tree: `display:none`
        // here would strip every area's accessible name.
        if (l.labelPosition !== "absolute" || l.labelWidth !== "1px") {
          fail(`${label}: expected the area label clip-hidden (absolute, ` +
            `1px), got position "${l.labelPosition}", width "${l.labelWidth}"`);
        }
        if (l.labelDisplay === "none") {
          fail(`${label}: the area label is display:none -- that removes ` +
            `every area's accessible name from the a11y tree at exactly the ` +
            `width where the visual grouping is already gone.`);
        }
      } else {
        if (l.webDirection !== "row") {
          fail(`${label}: expected .web flex-direction: row, got ` +
            `"${l.webDirection}"`);
        }
        if (l.navDirection !== "column") {
          fail(`${label}: expected .side-nav flex-direction: column, got ` +
            `"${l.navDirection}"`);
        }
        if (l.labelPosition === "absolute") {
          fail(`${label}: the area label is still clip-hidden above the ` +
            `shell token -- the visible sidebar heading should be back.`);
        }
      }
    });
}

// ---------------------------------------------------------------------------
// 1040 -- dashboard grids (web.css)
// ---------------------------------------------------------------------------
async function checkDashboardBoundary(browser, width, expectNarrow) {
  const label = `Dashboard @${width}px`;
  await leg(browser, label,
    { width, height: 900, html: shellHtml(), sheets: INDEX_SHEETS },
    async (page) => {
      const l = await page.evaluate(() => {
        const cs = (s) => getComputedStyle(document.querySelector(s));
        return {
          gridCols: cs(".dash-grid").gridTemplateColumns,
          statCols: cs(".dash-stats").gridTemplateColumns,
        };
      });
      const want = expectNarrow ? { grid: 1, stats: 2 } : { grid: 2, stats: 4 };
      const gridCols = colCount(l.gridCols);
      const statCols = colCount(l.statCols);
      if (gridCols !== want.grid) {
        fail(`${label}: expected .dash-grid at ${want.grid} column(s), got ` +
          `${gridCols} (grid-template-columns: "${l.gridCols}")`);
      }
      if (statCols !== want.stats) {
        fail(`${label}: expected .dash-stats at ${want.stats} column(s), got ` +
          `${statCols} (grid-template-columns: "${l.statCols}")`);
      }
    });
}

// ---------------------------------------------------------------------------
// Collapsed nav integrity: no page overflow, no destination clipped or hidden.
// ---------------------------------------------------------------------------
async function checkCollapsedNavIntegrity(browser, label, width, height) {
  await leg(browser, `${label} nav integrity`,
    { width, height, html: shellHtml(), sheets: INDEX_SHEETS },
    async (page) => {
      const report = await page.evaluate(() => {
        const nav = document.querySelector(".side-nav");
        const navBox = nav.getBoundingClientRect();
        const tabs = [...nav.querySelectorAll(".tab")];
        const problems = [];
        const seen = [];
        for (const t of tabs) {
          const name = (t.querySelector(".nav-label") || t).textContent.trim();
          const area = t.closest(".nav-group").dataset.navArea;
          seen.push(`${area}/${name}`);
          const cs = getComputedStyle(t);
          // 1. Rendered at all.
          if (cs.display === "none" || cs.visibility === "hidden") {
            problems.push(`${area}/${name}: not rendered (display ` +
              `"${cs.display}", visibility "${cs.visibility}")`);
            continue;
          }
          if (!t.offsetWidth || !t.offsetHeight) {
            problems.push(`${area}/${name}: zero-sized ` +
              `(${t.offsetWidth}x${t.offsetHeight})`);
            continue;
          }
          // 2. Inside the scroller's own content box -- not escaped past it.
          const right = t.offsetLeft + t.offsetWidth;
          if (right > nav.scrollWidth + 1) {
            problems.push(`${area}/${name}: escapes the scroll strip ` +
              `(right edge ${right} > scrollWidth ${nav.scrollWidth})`);
          }
          // 3. Not vertically clipped by the strip.
          const box = t.getBoundingClientRect();
          if (box.height > navBox.height + 1) {
            problems.push(`${area}/${name}: taller (${box.height}) than the ` +
              `strip (${navBox.height}) -- vertically clipped`);
          }
          // 4. Reachable by scrolling the strip to it.
          t.scrollIntoView({ block: "nearest", inline: "nearest" });
          const after = t.getBoundingClientRect();
          const navAfter = nav.getBoundingClientRect();
          if (after.left < navAfter.left - 1 || after.right > navAfter.right + 1) {
            problems.push(`${area}/${name}: not scroll-reachable -- after ` +
              `scrollIntoView it sits at [${Math.round(after.left)}, ` +
              `${Math.round(after.right)}] outside the strip's ` +
              `[${Math.round(navAfter.left)}, ${Math.round(navAfter.right)}]`);
          }
        }
        return {
          problems,
          count: tabs.length,
          seen,
          groups: [...document.querySelectorAll(".nav-group")].map((g) => ({
            area: g.dataset.navArea,
            tabs: g.querySelectorAll(".tab").length,
          })),
          scrollWidth: document.documentElement.scrollWidth,
          clientWidth: document.documentElement.clientWidth,
        };
      });
      if (report.scrollWidth > report.clientWidth) {
        fail(`${label}: horizontal PAGE overflow with the seven-area nav ` +
          `collapsed -- scrollWidth ${report.scrollWidth} > clientWidth ` +
          `${report.clientWidth}`);
      }
      const empty = report.groups.filter((g) => !g.tabs);
      if (empty.length) {
        fail(`${label}: nav area(s) with zero destinations: ` +
          `${empty.map((g) => g.area).join(", ")}`);
      }
      if (report.problems.length) {
        fail(`${label}: ${report.problems.length} destination(s) clipped or ` +
          `hidden in the collapsed nav:\n    - ` +
          `${report.problems.join("\n    - ")}`);
      }
      console.log(`  ${label}: ${report.count} destinations across ` +
        `${report.groups.length} areas, none clipped or hidden.`);
    });
}

// ---------------------------------------------------------------------------
// Keyboard reachability in the collapsed nav.
// ---------------------------------------------------------------------------
async function checkCollapsedNavKeyboard(browser, label, width, height) {
  await leg(browser, `${label} keyboard`,
    { width, height, html: shellHtml(), sheets: INDEX_SHEETS },
    async (page) => {
      // Stamp a fixture-local index so two destinations that share a route key
      // (Facilities and Administration both route to `setup`) stay distinct.
      const expected = await page.evaluate(() => {
        const tabs = [...document.querySelectorAll(".side-nav .tab")];
        tabs.forEach((t, i) => { t.dataset.kbIdx = String(i); });
        document.body.setAttribute("tabindex", "-1");
        document.body.focus();
        return tabs.length;
      });
      const reached = new Set();
      // Generous cap: every nav destination plus the shell's other focusables,
      // so a missed destination fails on the assertion below, not on the cap.
      for (let i = 0; i < expected * 3 + 20; i += 1) {
        await page.keyboard.press("Tab");
        const idx = await page.evaluate(() => {
          const a = document.activeElement;
          if (!a || !a.dataset || a.dataset.kbIdx === undefined) return null;
          const cs = getComputedStyle(a);
          // A destination that cannot show a focus ring is not usefully
          // reachable, so record outline state alongside identity.
          return { idx: a.dataset.kbIdx, outline: cs.outlineStyle };
        });
        if (idx) reached.add(idx.idx);
        if (reached.size === expected) break;
      }
      if (reached.size !== expected) {
        const missing = [];
        for (let i = 0; i < expected; i += 1) {
          if (!reached.has(String(i))) missing.push(i);
        }
        const names = await page.evaluate((ids) => ids.map((i) => {
          const t = document.querySelector(`[data-kb-idx="${i}"]`);
          const label2 = (t.querySelector(".nav-label") || t).textContent.trim();
          return `${t.closest(".nav-group").dataset.navArea}/${label2}`;
        }), missing);
        fail(`${label}: a Tab walk reached ${reached.size} of ${expected} ` +
          `destinations in the collapsed nav; unreachable: ${names.join(", ")}`);
      }
      console.log(`  ${label}: Tab reaches all ${expected} destinations.`);
    });
}

// ---------------------------------------------------------------------------
// 16px mobile inputs (iOS Safari auto-zoom guard).
// ---------------------------------------------------------------------------
async function checkMobileInputSizes(browser, width, expectLifted) {
  const label = `Mobile inputs @${width}px`;
  await leg(browser, label,
    { width, height: 800, html: INPUTS_HTML, sheets: INDEX_SHEETS },
    async (page) => {
      const sizes = await page.evaluate((sels) => {
        const out = {};
        for (const s of sels) {
          const el = document.querySelector(s);
          out[s] = el ? getComputedStyle(el).fontSize : null;
        }
        out[".import-form textarea"] = getComputedStyle(
          document.querySelector(".import-form textarea")).fontSize;
        return out;
      }, SIXTEEN_PX_INPUTS);

      for (const s of SIXTEEN_PX_INPUTS) {
        if (sizes[s] === null) {
          fail(`${label}: fixture has no element matching "${s}" -- the ` +
            `selector list has drifted from the markup, so this check would ` +
            `pass vacuously for that rule.`);
        }
        const px = parseFloat(sizes[s]);
        if (expectLifted) {
          if (px !== 16) {
            fail(`${label}: "${s}" is ${sizes[s]}; below 16px iOS Safari ` +
              `auto-zooms the page on focus.`);
          }
        } else if (px >= 16) {
          // Above the phone token the base size must be BELOW 16, otherwise
          // the ≤480 rule proves nothing -- deleting it would still pass.
          fail(`${label}: "${s}" is already ${sizes[s]} above the phone ` +
            `token, so the ≤480px 16px rule is unfalsifiable for it.`);
        }
      }
      // Recorded, not asserted at 16: styles.css deliberately holds the import
      // textarea at 14px. That is a real pre-existing iOS auto-zoom gap, and
      // it is NOT in this slice's scope (font sizing is not one of the four
      // approved width tokens). Printed so it stays visible instead of being
      // quietly excluded from a "16px mobile inputs" claim.
      if (expectLifted) {
        console.log(`  ${label}: ${SIXTEEN_PX_INPUTS.length} controls at 16px;` +
          ` .import-form textarea is ${sizes[".import-form textarea"]} ` +
          `(deliberate pre-existing exception, out of scope here).`);
      }
    });
}

// ---------------------------------------------------------------------------
async function checkNoOverflowNoErrors(browser, label, width, height) {
  await leg(browser, label,
    { width, height, html: shellHtml(), sheets: INDEX_SHEETS },
    async (page) => {
      const overflow = await page.evaluate(() => ({
        scrollWidth: document.documentElement.scrollWidth,
        clientWidth: document.documentElement.clientWidth,
      }));
      if (overflow.scrollWidth > overflow.clientWidth) {
        fail(`${label}: horizontal page overflow -- scrollWidth ` +
          `${overflow.scrollWidth} > clientWidth ${overflow.clientWidth}`);
      }
    });
}

(async () => {
  const browser = await chromium.launch();
  try {
    // 480 -- Ice Builder stacks at/below, restores above.
    await checkIceBuilderBoundary(browser, 479, true);
    await checkIceBuilderBoundary(browser, 480, true);
    await checkIceBuilderBoundary(browser, 481, false);

    // 480 -- sign-in card goes full-bleed at/below, padded above.
    await checkSetupCardBoundary(browser, 479, true);
    await checkSetupCardBoundary(browser, 480, true);
    await checkSetupCardBoundary(browser, 481, false);

    // 480 -- mobile form controls lift to 16px at/below.
    await checkMobileInputSizes(browser, 479, true);
    await checkMobileInputSizes(browser, 480, true);
    await checkMobileInputSizes(browser, 481, false);

    // 720 -- Game Sheet stacks at/below, restores above.
    await checkGameSheetBoundary(browser, 719, true);
    await checkGameSheetBoundary(browser, 720, true);
    await checkGameSheetBoundary(browser, 721, false);

    // 720 -- onboarding hero collapses at/below, two columns above.
    await checkOnboardingBoundary(browser, 719, true);
    await checkOnboardingBoundary(browser, 720, true);
    await checkOnboardingBoundary(browser, 721, false);

    // 880 -- app shell collapses to the horizontal nav strip at/below.
    await checkShellBoundary(browser, 879, true);
    await checkShellBoundary(browser, 880, true);
    await checkShellBoundary(browser, 881, false);

    // 1040 -- dashboard grids narrow at/below, full width above.
    await checkDashboardBoundary(browser, 1039, true);
    await checkDashboardBoundary(browser, 1040, true);
    await checkDashboardBoundary(browser, 1041, false);

    // The collapsed seven-area nav, at the boundary itself and at canonical
    // phone: nothing clipped, hidden, or keyboard-unreachable.
    await checkCollapsedNavIntegrity(browser, "shell token 879x900", 879, 900);
    await checkCollapsedNavIntegrity(browser, "phone 390x844", 390, 844);
    await checkCollapsedNavKeyboard(browser, "shell token 879x900", 879, 900);
    await checkCollapsedNavKeyboard(browser, "phone 390x844", 390, 844);

    // Canonical viewports: no horizontal overflow, no console/page errors.
    await checkNoOverflowNoErrors(browser, "phone 390x844", 390, 844);
    await checkNoOverflowNoErrors(browser, "desktop 1440x900", 1440, 900);

    console.log("Breakpoint boundaries OK -- all four approved tokens switch "
      + "at the right width (480 Ice Builder/sign-in card/16px inputs, 720 "
      + "Game Sheet/onboarding hero, 880 app shell + nav strip, 1040 "
      + "dashboard grids), the collapsed seven-area nav clips/hides nothing "
      + "and is fully Tab-reachable at 879 and 390, and there is no "
      + "horizontal overflow or console/page error at 390x844 or 1440x900.");
  } finally {
    await browser.close();
  }
})().catch((e) => {
  console.error("Breakpoint boundaries journey FAILED.");
  console.error(e.message);
  process.exit(1);
});
