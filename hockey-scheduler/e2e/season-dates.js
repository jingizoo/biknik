// Operator season date-boundary browser journey (#272).
//
// The backend accepts a date-only YYYY-MM-DD season boundary and anchors it to
// local midnight in the Program's timezone. This journey proves the operator UI
// half end-to-end at desktop AND 390px:
//   * seed a Program in a POSITIVE-offset timezone (Asia/Tokyo) over the wire;
//   * open the Season create drawer, pick that Program, enter a bare start/end
//     DATE (native date inputs → YYYY-MM-DD), and submit;
//   * assert the season row shows the ENTERED calendar day (e.g. "Sep 15,
//     2026"), NOT the adjacent UTC day ("Sep 14") a naive conversion would show;
//   * assert an end-before-start date pair surfaces the drawer's field error.
// Fails on any browser console/page error.
const { chromium } = require("playwright");
const { spawn } = require("child_process");
const http = require("http");
const path = require("path");

const HOST = "127.0.0.1";
const BACKEND_DIR = path.resolve(__dirname, "..", "backend");
const READY_TIMEOUT_MS = 15000;
const VIEWPORTS = [
  { label: "desktop", width: 1440, height: 900, port: 8271 },
  { label: "phone", width: 390, height: 844, port: 8272 },
];

function waitForServer(url, timeoutMs) {
  const deadline = Date.now() + timeoutMs;
  return new Promise((resolve, reject) => {
    const attempt = () => {
      const request = http.get(url, (r) => { r.resume(); resolve(); });
      request.setTimeout(2000, () => request.destroy(new Error("request timed out")));
      request.on("error", () => {
        if (Date.now() > deadline) reject(new Error(`Server never came up at ${url}`));
        else setTimeout(attempt, 250);
      });
    };
    attempt();
  });
}

function stopServer(server) {
  return new Promise((resolve) => {
    if (server.exitCode !== null || server.signalCode !== null) return resolve();
    const escalate = setTimeout(() => server.kill("SIGKILL"), 3000);
    server.once("exit", () => { clearTimeout(escalate); resolve(); });
    server.kill("SIGTERM");
  });
}

async function checkViewport(browser, viewport) {
  const base = `http://${HOST}:${viewport.port}`;
  const L = viewport.label;
  const server = spawn(
    process.env.PYTHON || "python3",
    ["-u", "-m", "hockey_scheduler.web.server", "--host", HOST, "--port", String(viewport.port)],
    { cwd: BACKEND_DIR, stdio: ["ignore", "pipe", "pipe"] });
  let out = "";
  server.stdout.on("data", (d) => { out += d.toString(); });
  server.stderr.on("data", (d) => { out += d.toString(); });

  const context = await browser.newContext({
    viewport: { width: viewport.width, height: viewport.height },
  });
  const page = await context.newPage();
  const errors = [];
  page.on("pageerror", (e) => errors.push(`[pageerror] ${e.message}`));
  page.on("console", (m) => { if (m.type() === "error") errors.push(`[console] ${m.text()}`); });

  // The Seasons entity-card list-item text for the season with this name.
  const seasonRowText = (name) => page.evaluate((nm) => {
    const item = Array.from(document.querySelectorAll(".setup-card .li"))
      .find((n) => n.textContent.includes(nm));
    return item ? item.textContent : null;
  }, name);

  try {
    await waitForServer(`${base}/api/health`, READY_TIMEOUT_MS);
    await page.goto(base, { waitUntil: "domcontentloaded" });
    await page.waitForSelector("#content > *", { timeout: 10000 });
    await page.click('.tab[data-tab="setup"]');
    await page.click('[data-setup-view="records"]');
    await page.waitForSelector(".setup-card", { timeout: 10000 });

    // Create a POSITIVE-offset Program through the operator drawer (the anchor
    // we need to exercise), including the timezone field the drawer now
    // exposes. First prove a bad IANA name is rejected in the drawer.
    await page.click('.setup-card .sc-new[data-drawer="league"]');
    await page.waitForSelector(".drawer[role=dialog]", { timeout: 10000 });
    await page.fill("#f-league", "Tokyo Program");
    await page.fill("#f-league-tz", "Not/AZone");
    const badTzErrs = errors.length;
    await page.click('[data-drawer-submit="league"]');
    await page.waitForSelector(".drawer-err", { timeout: 10000 });
    const tzErr = await page.textContent(".drawer-err");
    if (!/timezone/i.test(tzErr)) {
      throw new Error(`[${L}] expected an invalid-timezone drawer error, got "${tzErr}"`);
    }
    // Correct the timezone in place and submit for real.
    await page.fill("#f-league-tz", "Asia/Tokyo");
    const progResp = page.waitForResponse((r) =>
      r.url() === `${base}/api/v2/setup/program` && r.request().method() === "POST"
      && r.status() === 200);
    await page.click('[data-drawer-submit="league"]');
    const progBody = await (await progResp).json();
    if (progBody.error) throw new Error(`[${L}] program create failed: ${JSON.stringify(progBody.error)}`);
    if (progBody.timezone !== "Asia/Tokyo") {
      throw new Error(`[${L}] program timezone not stored: ${JSON.stringify(progBody)}`);
    }
    const programId = progBody.id;
    await page.waitForFunction(() => !document.querySelector(".drawer[role=dialog]"),
      null, { timeout: 10000 });
    // The bad-timezone submit logs one expected HTTP 400; anything else is a
    // real error and still fails the run below.
    const progUnexpected = errors.slice(badTzErrs).filter(
      (e) => !/\b400\b|Failed to load resource/i.test(e));
    if (progUnexpected.length) {
      throw new Error(`[${L}] unexpected errors creating program:\n${progUnexpected.join("\n")}`);
    }
    errors.length = 0;  // reset for the console-clean season happy-path below

    const openSeasonDrawer = async () => {
      await page.click('.setup-card .sc-new[data-drawer="season"]');
      await page.waitForSelector(".drawer[role=dialog]", { timeout: 10000 });
      await page.selectOption("#f-season-league", programId);
    };

    // 1) A bare start/end DATE, entered in the operator drawer.
    await openSeasonDrawer();
    await page.fill("#f-season", "2026-27");
    await page.fill("#f-season-start", "2026-09-15");
    await page.fill("#f-season-end", "2027-03-15");
    const createResp = page.waitForResponse((r) =>
      r.url() === `${base}/api/v2/setup/season` && r.request().method() === "POST");
    await page.click('[data-drawer-submit="season"]');
    const created = await (await createResp).json();
    if (created.error) throw new Error(`[${L}] season create failed: ${JSON.stringify(created.error)}`);
    // Anchored to Tokyo midnight, stored as the previous UTC day.
    if (created.start_date !== "2026-09-14T15:00:00+00:00") {
      throw new Error(`[${L}] unexpected stored start_date: ${created.start_date}`);
    }
    await page.waitForFunction(() => !document.querySelector(".drawer[role=dialog]"),
      null, { timeout: 10000 });

    // The Seasons card shows the ENTERED calendar day (Tokyo), not the adjacent
    // UTC day. "Sep 15" must appear; "Sep 14" must NOT.
    await page.waitForFunction(() => {
      const s = Array.from(document.querySelectorAll(".setup-card .li"))
        .find((n) => n.textContent.includes("2026-27"));
      return s && /Sep\s+15,\s+2026/.test(s.textContent);
    }, null, { timeout: 10000 });
    const row = await seasonRowText("2026-27");
    if (/Sep\s+14/.test(row)) {
      throw new Error(`[${L}] season row shows the adjacent UTC day: "${row}"`);
    }
    // The happy path must be completely console-clean.
    if (errors.length) throw new Error(`[${L}] console/page errors:\n${errors.join("\n")}`);

    // 2) End-before-start is a field error surfaced in the drawer, no create.
    // This DELIBERATELY posts an invalid body, so the browser logs one expected
    // HTTP 400 resource error — tolerated below; any OTHER error still fails.
    const beforeErrorCase = errors.length;
    await openSeasonDrawer();
    await page.fill("#f-season", "Reversed");
    await page.fill("#f-season-start", "2026-09-15");
    await page.fill("#f-season-end", "2026-09-01");
    await page.click('[data-drawer-submit="season"]');
    await page.waitForSelector(".drawer-err", { timeout: 10000 });
    const errText = await page.textContent(".drawer-err");
    if (!/before/i.test(errText)) {
      throw new Error(`[${L}] expected an end-before-start drawer error, got "${errText}"`);
    }
    const unexpected = errors.slice(beforeErrorCase).filter(
      (e) => !/\b400\b|Failed to load resource/i.test(e));
    if (unexpected.length) {
      throw new Error(`[${L}] unexpected console/page errors:\n${unexpected.join("\n")}`);
    }
    console.log(`[${L}] OK — date-only season accepted, displayed on the entered calendar day.`);
  } catch (error) {
    throw new Error(`${error.message}\n--- server output ---\n${out}`);
  } finally {
    await context.close();
    await stopServer(server);
  }
}

async function main() {
  let browser;
  try {
    browser = await chromium.launch(
      process.env.SMOKE_CHROMIUM_PATH ? { executablePath: process.env.SMOKE_CHROMIUM_PATH } : {});
    for (const viewport of VIEWPORTS) await checkViewport(browser, viewport);
    console.log("Season date-boundary operator browser journey passed.");
  } catch (error) {
    console.error("Season date-boundary operator browser journey FAILED.");
    console.error(error && error.message ? error.message : error);
    process.exitCode = 1;
  } finally {
    if (browser) await browser.close();
  }
}

main();
