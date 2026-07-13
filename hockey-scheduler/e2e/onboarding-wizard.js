// Guided Initial Setup browser journey (#174 PR D).
//
// A fresh production installation with its first League Admin signs in, lands on
// the server-derived Initial Setup screen, can leave and resume, and deep-links
// into the existing Setup drawer. Runs at desktop and 390px phone widths.
const { chromium } = require("playwright");
const { spawn } = require("child_process");
const fs = require("fs");
const http = require("http");
const os = require("os");
const path = require("path");

const BIND_HOST = "127.0.0.1";
const BROWSER_HOST = "localhost";
const BACKEND_DIR = path.resolve(__dirname, "..", "backend");
const READY_TIMEOUT_MS = 15000;
const ADMIN_USER = "onboarding-admin";
const ADMIN_PASSWORD = "fixture-onboarding-password";
const VIEWPORTS = [
  { label: "desktop", width: 1440, height: 900, port: 8133 },
  { label: "phone", width: 390, height: 844, port: 8134 },
];

function waitForServer(url, timeoutMs) {
  const deadline = Date.now() + timeoutMs;
  return new Promise((resolve, reject) => {
    const attempt = () => {
      const req = http.get(url, (res) => {
        res.resume();
        resolve();
      });
      req.setTimeout(2000, () => req.destroy(new Error("request timed out")));
      req.on("error", () => {
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
    server.once("exit", () => {
      clearTimeout(escalate);
      resolve();
    });
    server.kill("SIGTERM");
  });
}

async function checkViewport(browser, viewport) {
  const tempDir = fs.mkdtempSync(path.join(os.tmpdir(), "hockey-onboarding-"));
  const databasePath = path.join(tempDir, "client.sqlite");
  const browserBase = `http://${BROWSER_HOST}:${viewport.port}`;
  const probeBase = `http://${BIND_HOST}:${viewport.port}`;
  const server = spawn(
    process.env.PYTHON || "python3",
    ["-u", "-m", "hockey_scheduler.web.server", "--host", BIND_HOST,
      "--port", String(viewport.port)],
    {
      cwd: BACKEND_DIR,
      env: {
        ...process.env,
        APP_MODE: "production",
        DATABASE_URL: databasePath,
        BOOTSTRAP_ADMIN_USER: ADMIN_USER,
        BOOTSTRAP_ADMIN_PASSWORD: ADMIN_PASSWORD,
        INITIAL_SETUP_CODE: "",
      },
      stdio: ["ignore", "pipe", "pipe"],
    },
  );
  let serverOutput = "";
  server.stdout.on("data", (data) => { serverOutput += data.toString(); });
  server.stderr.on("data", (data) => { serverOutput += data.toString(); });

  const context = await browser.newContext({
    viewport: { width: viewport.width, height: viewport.height },
  });
  const page = await context.newPage();
  const errors = [];
  page.on("pageerror", (error) => errors.push(`[pageerror] ${error.message}`));
  page.on("console", (message) => {
    if (message.type() === "error") errors.push(`[console] ${message.text()}`);
  });

  try {
    await waitForServer(`${probeBase}/api/health`, READY_TIMEOUT_MS);
    await page.goto(browserBase, { waitUntil: "domcontentloaded" });
    await page.waitForSelector("#login-screen:not([hidden])", { timeout: 10000 });
    await page.fill("#login-user", ADMIN_USER);
    await page.fill("#login-pass", ADMIN_PASSWORD);

    const loginResponsePromise = page.waitForResponse((response) =>
      response.url() === `${browserBase}/api/auth/login`
      && response.request().method() === "POST");
    await page.click("#login-form button[type=submit]");
    const loginResponse = await loginResponsePromise;
    const loginBody = await loginResponse.json();
    if (loginResponse.status() !== 200 || !loginBody.user
        || loginBody.user.role !== "league_admin") {
      throw new Error(`[${viewport.label}] login failed: HTTP ${loginResponse.status()} ${JSON.stringify(loginBody)}`);
    }

    const me = await page.evaluate(async () => {
      const response = await fetch("/api/auth/me", {
        credentials: "same-origin",
        cache: "no-store",
      });
      return { status: response.status, body: await response.json() };
    });
    if (me.status !== 200 || !me.body.user || me.body.user.role !== "league_admin") {
      throw new Error(`[${viewport.label}] session did not persist: ${JSON.stringify(me)}`);
    }

    await page.waitForSelector('body[data-view="onboarding"]', { timeout: 10000 });
    await page.getByRole("heading", { name: "Complete the program foundation" }).waitFor();
    await page.waitForSelector('[data-onboarding-step="organization"].current');
    await page.waitForSelector('.tab[data-tab="onboarding"].active');
    await page.getByText("Progress is recalculated from saved records", { exact: false }).waitFor();

    // #233 B1: the wizard teaches the target model in the canonical vocabulary.
    // Step/drawer keys stay frozen (exercised below by deep-linking the
    // organization drawer). All steps render regardless of completion state, so
    // the whole step list is inspectable on an empty install. Per-step text and
    // action labels are captured by the frozen data-onboarding-step key.
    const wiz = await page.evaluate(() => {
      const steps = {};
      document.querySelectorAll(".onboarding-step").forEach((el) => {
        steps[el.getAttribute("data-onboarding-step")] = {
          title: ((el.querySelector("h3") || {}).textContent || "").trim(),
          text: el.textContent,
          actions: [...el.querySelectorAll(".onboarding-step-actions button")]
            .map((b) => b.textContent.trim()),
        };
      });
      const list = document.querySelector(".onboarding-step-list");
      return {
        steps,
        allText: list ? list.textContent : "",
        titles: [...document.querySelectorAll(".onboarding-step h3")].map((h) => h.textContent.trim()),
      };
    });
    const need = (cond, msg) => { if (!cond) throw new Error(`[${viewport.label}] ${msg}`); };
    const s = wiz.steps;

    // Umbrella step (frozen key "league") reads "Program" and never a bare
    // "League"; its action is "Add program", its checks name the program and the
    // operating organization (never "owner"/"facility owner").
    need(s.league && s.league.title === "Program", `umbrella step title is not "Program" (got ${JSON.stringify(s.league && s.league.title)})`);
    need(!wiz.titles.includes("League"), `wizard still has a bare "League" step title`);
    need(s.league.actions.includes("Add program") && !s.league.actions.includes("Add league"),
      `umbrella action is not "Add program" (got ${JSON.stringify(s.league.actions)})`);
    need(/Program created/.test(s.league.text) && /Operating organization assigned/.test(s.league.text),
      `umbrella checks not renamed to "Program created"/"Operating organization assigned"`);

    // Step 1 keeps facility ownership distinct from program operation.
    need(/facility owner/i.test(s.organization.text) && /operating organization/i.test(s.organization.text),
      `step 1 does not separate facility owner from operating organization`);

    // Step 3 marks the Venue→Program link as temporary v1 compatibility.
    need(/temporary v1/i.test(s.venues.text) && /Program-linked venue/i.test(s.venues.text),
      `step 3 does not mark the venue→program link as temporary v1`);

    // Step 4 teaches the target model — leagues required, division optional — and
    // no longer says "Leagues remain optional"; the grouping action is "Add
    // league" (not "Add optional league"/"Add optional level").
    need(/leagues \(required\)/i.test(s.season.text) && /optional split of a league/i.test(s.season.text),
      `step 4 does not teach league-required / division-optional target model`);
    need(!/Leagues remain optional/i.test(s.season.text) && !/Levels remain/i.test(s.season.text),
      `step 4 still says "Leagues remain optional"`);
    need(s.season.actions.includes("Add league") && !s.season.actions.some((a) => /optional (league|level)/i.test(a)),
      `step 4 grouping action is not a plain "Add league" (got ${JSON.stringify(s.season.actions)})`);

    // Step 5 frames teams as permanent program members placed seasonally.
    need(/permanently to its program/i.test(s.teams.text) && /optional division/i.test(s.teams.text),
      `step 5 does not describe permanent program ownership + seasonal placement`);

    // The internal grouping key "level"/"Level" is never shown to the operator.
    need(!/\bLevel\b/.test(wiz.allText), `wizard still exposes the internal "Level" noun`);

    const status = await page.evaluate(async () => {
      const response = await fetch("/api/onboarding/status", {
        credentials: "same-origin",
        cache: "no-store",
      });
      return { status: response.status, body: await response.json() };
    });
    if (status.status !== 200 || status.body.ready_to_schedule !== false
        || !status.body.blocking.some((item) => item.code === "no_organization")) {
      throw new Error(`[${viewport.label}] unexpected empty-install status: ${JSON.stringify(status)}`);
    }

    const storage = await page.evaluate(() => ({
      local: Object.keys(localStorage),
      session: Object.keys(sessionStorage),
    }));
    const forbiddenStorage = [...storage.local, ...storage.session]
      .filter((key) => /onboarding|initial.?setup|setup.?step/i.test(key));
    if (forbiddenStorage.length) {
      throw new Error(`[${viewport.label}] onboarding progress leaked to browser storage: ${forbiddenStorage.join(", ")}`);
    }

    // Continue later is a real escape hatch, not a trap.
    await page.click('[data-onboarding-goto="dashboard"]');
    await page.waitForSelector('body[data-view="dashboard"]', { timeout: 10000 });

    // The visible nav entry resumes from persisted records.
    await page.click('.tab[data-tab="onboarding"]');
    await page.waitForSelector('body[data-view="onboarding"]', { timeout: 10000 });
    await page.waitForSelector('[data-onboarding-step="organization"].current');

    // The Fix action reuses the existing Setup drawer instead of a parallel form.
    await page.click('[data-onboarding-drawer="organization"]');
    await page.waitForSelector('body[data-view="setup"]', { timeout: 10000 });
    await page.waitForSelector("#f-org", { timeout: 10000 });

    if (errors.length) {
      throw new Error(`[${viewport.label}] console/page errors:\n${errors.join("\n")}`);
    }
    console.log(`[${viewport.label}] OK — auto-routed, resumable, deep-linked to Setup.`);
  } catch (error) {
    throw new Error(`${error.message}\n--- production server output ---\n${serverOutput}`);
  } finally {
    await context.close();
    await stopServer(server);
    fs.rmSync(tempDir, { recursive: true, force: true });
  }
}

async function main() {
  let browser;
  try {
    browser = await chromium.launch(
      process.env.SMOKE_CHROMIUM_PATH
        ? { executablePath: process.env.SMOKE_CHROMIUM_PATH }
        : {},
    );
    for (const viewport of VIEWPORTS) await checkViewport(browser, viewport);
    console.log("Guided Initial Setup browser journey passed.");
  } catch (error) {
    console.error("Guided Initial Setup browser journey FAILED.");
    console.error(error && error.message ? error.message : error);
    process.exitCode = 1;
  } finally {
    if (browser) await browser.close();
  }
}

main();
