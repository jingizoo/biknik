// Complete hierarchy import browser journey (#174 PR E2).
//
// At desktop and phone widths, a League Admin opens the existing Import screen,
// downloads every explicit template, validates every hierarchy CSV (including
// the permanent-teams and season-registrations sheets), commits the batch,
// verifies the resulting team + registration appear in the commit summary, and
// checks that no pasted hierarchy data was written to browser storage. Fails on
// browser console/page errors.
const { chromium } = require("playwright");
const { spawn } = require("child_process");
const http = require("http");
const path = require("path");

const HOST = "127.0.0.1";
const BACKEND_DIR = path.resolve(__dirname, "..", "backend");
const READY_TIMEOUT_MS = 15000;
const VIEWPORTS = [
  { label: "desktop", width: 1440, height: 900, port: 8135 },
  { label: "phone", width: 390, height: 844, port: 8136 },
];

const SHEETS = {
  organizations_csv:
    "organization_code,organization_name,short_name\n" +
    "BROWSERORG,Browser Ice Facilities,Browser Ice\n",
  leagues_csv:
    "league_code,organization_code,league_name,country,timezone\n" +
    "BROWSERLEAGUE,BROWSERORG,Browser League,US,America/Chicago\n",
  venues_rinks_csv:
    "venue_code,organization_code,league_code,venue_name,address,timezone,rink_code,rink_name\n" +
    "BROWSERVENUE,BROWSERORG,BROWSERLEAGUE,Browser Arena,1 Test Way,America/Chicago,BROWSERRINK,Browser Rink\n",
  competition_csv:
    "league_code,season_code,season_name,level_code,level_name,level_sort_order,division_code,division_name,age_group\n" +
    "BROWSERLEAGUE,BROWSERSEASON,Browser Season,BROWSERLEVEL,Browser Level,1,BROWSERDIV,Browser Division,Adult\n",
  permanent_teams_csv:
    "league_code,team_code,team_name,club_name\n" +
    "BROWSERLEAGUE,BROWSERTEAM,Browser Team,Browser Club\n",
  registrations_csv:
    "season_code,team_code,division_code\n" +
    "BROWSERSEASON,BROWSERTEAM,BROWSERDIV\n",
};

const FILENAMES = {
  organizations_csv: "organizations.csv",
  leagues_csv: "leagues.csv",
  venues_rinks_csv: "venues_rinks.csv",
  competition_csv: "competition.csv",
  permanent_teams_csv: "permanent_teams.csv",
  registrations_csv: "registrations.csv",
};

function waitForServer(url, timeoutMs) {
  const deadline = Date.now() + timeoutMs;
  return new Promise((resolve, reject) => {
    const attempt = () => {
      const request = http.get(url, (response) => {
        response.resume();
        resolve();
      });
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
    server.once("exit", () => {
      clearTimeout(escalate);
      resolve();
    });
    server.kill("SIGTERM");
  });
}

async function checkViewport(browser, viewport) {
  const base = `http://${HOST}:${viewport.port}`;
  const server = spawn(
    process.env.PYTHON || "python3",
    ["-u", "-m", "hockey_scheduler.web.server", "--host", HOST,
      "--port", String(viewport.port)],
    { cwd: BACKEND_DIR, stdio: ["ignore", "pipe", "pipe"] },
  );
  let serverOutput = "";
  server.stdout.on("data", (data) => { serverOutput += data.toString(); });
  server.stderr.on("data", (data) => { serverOutput += data.toString(); });

  const context = await browser.newContext({
    viewport: { width: viewport.width, height: viewport.height },
    acceptDownloads: true,
  });
  const page = await context.newPage();
  const errors = [];
  page.on("pageerror", (error) => errors.push(`[pageerror] ${error.message}`));
  page.on("console", (message) => {
    if (message.type() === "error") errors.push(`[console] ${message.text()}`);
  });

  try {
    await waitForServer(`${base}/api/health`, READY_TIMEOUT_MS);
    await page.goto(base, { waitUntil: "domcontentloaded" });
    await page.waitForSelector("#content > *", { timeout: 10000 });
    await page.click('.tab[data-tab="import"]');
    await page.waitForSelector("#hierarchy-import-panel", { timeout: 10000 });

    for (const [key, filename] of Object.entries(FILENAMES)) {
      const downloadPromise = page.waitForEvent("download");
      await page.click(`[data-hierarchy-template="${key}"]`);
      const download = await downloadPromise;
      if (download.suggestedFilename() !== filename) {
        throw new Error(`[${viewport.label}] expected ${filename}, got ${download.suggestedFilename()}`);
      }
    }

    for (const [key, csv] of Object.entries(SHEETS)) {
      await page.fill(`[data-hierarchy-sheet="${key}"]`, csv);
    }

    const validateResponse = page.waitForResponse((response) =>
      response.url() === `${base}/api/import/commit/teams-players`
      && response.request().method() === "POST"
      && response.request().postData().includes('"dry_run":true'));
    await page.click("[data-hierarchy-validate]");
    const preview = await validateResponse;
    if (preview.status() !== 200) {
      throw new Error(`[${viewport.label}] hierarchy validation returned HTTP ${preview.status()}`);
    }
    await page.getByRole("heading", { name: "Hierarchy validation passed" }).waitFor();
    await page.waitForSelector("[data-hierarchy-commit]:not([disabled])");

    const commitResponse = page.waitForResponse((response) =>
      response.url() === `${base}/api/import/commit/teams-players`
      && response.request().method() === "POST"
      && response.request().postData().includes('"dry_run":false'));
    await page.click("[data-hierarchy-commit]");
    const committed = await commitResponse;
    if (committed.status() !== 200) {
      throw new Error(`[${viewport.label}] hierarchy commit returned HTTP ${committed.status()}`);
    }
    await page.getByRole("heading", { name: "Hierarchy committed" }).waitFor();
    await page.getByText("organizations", { exact: true }).waitFor();
    await page.getByText("rinks", { exact: true }).waitFor();
    // The permanent team and its season registration were created (#180 import
    // convergence): the commit summary lists both entities.
    await page.getByText("permanent_teams", { exact: true }).waitFor();
    await page.getByText("registrations", { exact: true }).waitFor();

    // Verify the ACTUAL resulting records via the setup APIs (#214 review),
    // not just the summary labels: the imported permanent Team exists exactly
    // once in its league (no division), with one active Season/Division
    // registration.
    const verify = await page.evaluate(async () => {
      const getJson = async (url) =>
        (await fetch(url, { credentials: "same-origin" })).json();
      const hierarchy = await getJson("/api/setup/hierarchy");
      const league = (hierarchy.leagues || []).find((l) => l.name === "Browser League");
      const season = league && (league.seasons || []).find((s) => s.name === "Browser Season");
      const divisions = season
        ? [...(season.levels || []).flatMap((lv) => lv.divisions || []),
          ...(season.divisions_without_level || [])]
        : [];
      const division = divisions.find((d) => d.name === "Browser Division");
      const teams = league ? await getJson(`/api/setup/leagues/${league.id}/teams`) : { teams: [] };
      const imported = (teams.teams || []).filter((t) => t.name === "Browser Team");
      const team = imported[0];
      const regsResp = season
        ? await getJson(`/api/setup/seasons/${season.id}/team-registrations`)
        : { registrations: [] };
      const regs = team ? (regsResp.registrations || []).filter((r) => r.team_id === team.id) : [];
      return {
        leagueId: league && league.id, divisionId: division && division.id,
        teamCount: imported.length, teamLeagueId: team && team.league_id,
        teamDivisionId: team ? team.division_id : "no-team",
        regCount: regs.length, regDivisionId: regs[0] && regs[0].division_id,
        regActive: regs[0] && regs[0].active,
      };
    });
    if (verify.teamCount !== 1) {
      throw new Error(`[${viewport.label}] expected exactly one imported Browser Team, got ${verify.teamCount}`);
    }
    if (verify.teamLeagueId !== verify.leagueId) {
      throw new Error(`[${viewport.label}] imported team is not owned by Browser League`);
    }
    if (verify.teamDivisionId !== null) {
      throw new Error(`[${viewport.label}] a permanent team must carry no division, got ${verify.teamDivisionId}`);
    }
    if (verify.regCount !== 1 || verify.regDivisionId !== verify.divisionId || !verify.regActive) {
      throw new Error(`[${viewport.label}] expected one active registration in Browser Division: ${JSON.stringify(verify)}`);
    }

    const browserStorage = await page.evaluate(() => JSON.stringify({
      local: { ...localStorage },
      session: { ...sessionStorage },
    }));
    for (const marker of ["BROWSERORG", "BROWSERLEAGUE", "BROWSERVENUE",
      "BROWSERDIV", "BROWSERTEAM"]) {
      if (browserStorage.includes(marker)) {
        throw new Error(`[${viewport.label}] hierarchy data appeared in browser storage`);
      }
    }
    if (errors.length) {
      throw new Error(`[${viewport.label}] console/page errors:\n${errors.join("\n")}`);
    }
    console.log(`[${viewport.label}] OK — downloaded, validated, and committed hierarchy.`);
  } catch (error) {
    throw new Error(`${error.message}\n--- demo server output ---\n${serverOutput}`);
  } finally {
    await context.close();
    await stopServer(server);
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
    console.log("Hierarchy import browser journey passed.");
  } catch (error) {
    console.error("Hierarchy import browser journey FAILED.");
    console.error(error && error.message ? error.message : error);
    process.exitCode = 1;
  } finally {
    if (browser) await browser.close();
  }
}

main();
