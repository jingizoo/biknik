// League context bar browser journey (#345 senior slice, issue #364).
//
// Promotes League to a third, persistent context axis alongside the existing
// #159 Program/Season switcher (context-switcher.js, untouched and NOT
// duplicated here). This file exercises ONLY the League-axis-specific
// surface: the new #ctx-league-select, its interaction with the existing
// Program/Season select, the extended v2 "#ctx=" hash, and the request-
// generation race guard extended to carry a third value. It relies on the
// ALREADY-MERGED HTTP contract (context_service.py's *_with_league methods,
// #363) and the already-merged seven-area shell (#361) without touching
// either.
//
// Per options_with_league's own docstring, the League <select> is
// PROGRAM-scoped, not Season-scoped: a League not yet bound to the active
// Season is still offered, because selecting it is a legitimate way to MOVE
// to that (Season, League) pair -- the binding is validated at selection
// time by set_with_league itself (one generic non-oracle reason for every
// rejection cause, by design). Symmetrically, a same-Program Season change
// carries the active League forward for the same reason.
//
// What happens when the carried axis DOESN'T match a binding is the #364 owner
// ruling, and the browser has no say in it: the SERVER owns the canonical
// tuple (ContextService._canonical_league_season), so the Season that comes
// back may legitimately differ from the one that was sent, and the client must
// render what it was given rather than what it asked for. Keep the current
// Season when it genuinely binds (rule 1); otherwise, when the League has
// EXACTLY ONE valid authorized ACTIVE binding in the Program, move atomically
// to that Season (rule 2 -- checkCore's leg (M)); otherwise refuse generically
// with zero mutation and never tie-break a Season on the operator's behalf
// (rule 3 -- leg (D)). Legs (D) and (M) are what pin those two apart, and (M)
// additionally proves the move is agreed by every surface that could
// contradict it (persisted row, both controls, hash, reload, rendered screen)
// rather than merely returning 200. Every refusal is ONE generic non-oracle
// reason, atomic (set_with_league writes nothing until every axis validates),
// with both selects snapping back to the true persisted state.
//
// Scope decisions (recorded here, not just the PR body, so a future reader
// of this file sees the boundary without cross-referencing GitHub):
//   * Division/Team drawer-seeding fixes for the League axis are covered by
//     unit-level assertions inline in checkCore (they are the concrete
//     "existing screens now respecting the tuple" evidence) rather than a
//     separate file -- there are exactly two such call sites
//     (contextSeededDrawerValues 'division'/'team' in app.js) and both are
//     exercised here through the real Setup drawers.
//   * Setup-progress/workflow-hub summary COUNTS are deliberately NOT made
//     League-aware (a real product/semantic redesign, not "wire the context
//     bar") -- unchanged here, matching app.js's own scope note.
//   * Role-axis AUTHORIZATION (authorized_league_ids) is pre-existing,
//     already-merged backend logic (#363); checkRoleScoping proves the
//     CLIENT surfaces it correctly (GET /api/context/options never offers a
//     League a role can't select), not the backend rule's own correctness.
//     Official is asserted with NO assignments (fails closed to zero
//     Leagues) rather than a fully wired Game/officiating fixture -- that
//     heavier fixture is exercised elsewhere (role-authorization-matrix.js)
//     and adds nothing League-axis-specific beyond proving the same
//     fail-closed branch a zero-assignment Official already proves.
//   * "Missing/deleted/unbound/revoked League states with restoration"
//     (checkUnboundRevokedRestore) covers: a bogus/never-existed id in a
//     crafted hash (missing), a real binding removed then re-created
//     (unbound + restoration, the self-healing `_resolve_league_locked`
//     never-rewrites-the-saved-row behavior), and a Coach's own League
//     access revoked by a team transfer then restored (revoked +
//     restoration). A hard-deleted League is not modeled as a distinct case:
//     it exercises the identical "absent from authorized_league_ids" branch
//     the missing-id case already covers.
//
// Fails on any unexpected browser console/page error.
const { chromium } = require("playwright");
const { spawn, spawnSync } = require("child_process");
const fs = require("fs");
const http = require("http");
const os = require("os");
const path = require("path");

const HOST = "127.0.0.1";
const BACKEND_DIR = path.resolve(__dirname, "..", "backend");
const READY_TIMEOUT_MS = 15000;
const VIEWPORTS = [
  { label: "desktop", width: 1440, height: 900, port: 8361 },
  { label: "phone", width: 390, height: 844, port: 8362 },
];
const UNBOUND_PORT = 8363;
const RACE_PORT = 8364;
const ROLE_PORT = 8365;

function b64url(json) {
  return Buffer.from(json, "utf8").toString("base64")
    .replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}
// The v2 hash this journey's own app.js writes (program/season/league).
function encodeCtx(programId, seasonId, leagueId) {
  return "#ctx=" + b64url(JSON.stringify(
    { v: 2, p: programId || null, s: seasonId || null, l: leagueId || null }));
}
// The OLD v1 shape (no league key at all) -- a link minted before #364 ships,
// still sitting in a bookmark or a shared URL. decodeContextHash must still
// adopt it, with league_id defaulting to null.
function encodeCtxV1(programId, seasonId) {
  return "#ctx=" + b64url(JSON.stringify(
    { v: 1, p: programId || null, s: seasonId || null }));
}

function waitForServer(url, timeoutMs) {
  const deadline = Date.now() + timeoutMs;
  return new Promise((resolve, reject) => {
    const attempt = () => {
      const request = http.get(url, (response) => { response.resume(); resolve(); });
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
function startServer(port, env) {
  return spawn(
    process.env.PYTHON || "python3",
    ["-u", "-m", "hockey_scheduler.web.server", "--host", HOST, "--port", String(port)],
    { cwd: BACKEND_DIR, stdio: ["ignore", "pipe", "pipe"], env: { ...process.env, ...env } });
}

function apiPost(page, url, body) {
  return page.evaluate(async ([u, b]) => {
    const r = await fetch(u, {
      method: "POST", credentials: "same-origin",
      headers: { "Content-Type": "application/json" }, body: JSON.stringify(b || {}),
    });
    return { status: r.status, json: await r.json().catch(() => ({})) };
  }, [url, body]);
}
function apiGet(page, url) {
  return page.evaluate(async (u) => {
    const r = await fetch(u, { credentials: "same-origin" });
    return { status: r.status, json: await r.json().catch(() => ({})) };
  }, url);
}
const loginAs = (page, username, password) =>
  apiPost(page, "/api/auth/login", { username, password: password || "demo" });

async function newPage(browser, viewport) {
  const context = await browser.newContext({
    viewport: { width: viewport.width, height: viewport.height },
  });
  const page = await context.newPage();
  const errors = [];
  page.on("pageerror", (e) => errors.push(`[pageerror] ${e.message}`));
  page.on("console", (m) => {
    if (m.type() === "error" && !/Failed to load resource/.test(m.text())) {
      errors.push(`[console] ${m.text()}`);
    }
  });
  return { context, page, errors };
}
async function reloadShell(page) {
  await page.reload({ waitUntil: "domcontentloaded" });
  await page.waitForSelector("#content > *", { timeout: 10000 });
}
const ctxProgSel = (page) => page.evaluate(() => document.getElementById("ctx-select").value);
const ctxLeagueSel = (page) => page.evaluate(() => {
  const el = document.getElementById("ctx-league-select");
  return el && !el.hidden ? el.value : undefined;
});
const ctxNow = async (page) => (await apiGet(page, "/api/context")).json;
// A raw fetch POST to /api/context (bypassing the app's own setActiveContext,
// used throughout this file to set up fixtures without going through UI
// clicks) never touches location.hash the way a real switcher interaction
// does. A hash left over from an EARLIER step -- e.g. the normalized
// fallback a bogus-id probe settles on -- otherwise wins over the freshly
// POSTed row on the very next reload (restoreContextDeepLink's own
// documented "hash wins over the persisted row" behavior, context-
// switcher.js's own established contract), silently clobbering exactly what
// the raw POST just set. Clearing the hash after a raw POST -- whenever a
// later reload must trust that POST's own effect -- avoids exercising that
// (correct, intentional) adoption behavior by accident.
const clearHash = (page) => page.evaluate(() => history.replaceState(null, "", location.pathname + location.search));
// The persisted server row is only eventually consistent with a UI action --
// the switcher's POST is fired from an onchange handler nobody awaits -- so
// every "did this actually commit?" assertion polls the real HTTP boundary
// rather than sleeping a guessed interval. Returns the context that matched,
// and reports the LAST one seen on timeout so a failure names the actual
// divergence instead of just "timed out".
async function waitForContext(page, matches, message, timeoutMs) {
  const deadline = Date.now() + (timeoutMs || 10000);
  for (;;) {
    const now = await ctxNow(page);
    if (matches(now)) return now;
    if (Date.now() > deadline) throw new Error(`${message}: ${JSON.stringify(now)}`);
    await new Promise((r) => setTimeout(r, 100));
  }
}
// The inverse of encodeCtx: what the app itself actually wrote into the URL.
function decodeCtx(hash) {
  const m = /^#ctx=(.+)$/.exec(hash || "");
  if (!m) return null;
  const b64 = m[1].replace(/-/g, "+").replace(/_/g, "/");
  try { return JSON.parse(Buffer.from(b64, "base64").toString("utf8")); }
  catch (_) { return null; }
}
// Both context controls as a USER sees them: the committed value AND the
// visible label of the option actually showing. Value alone would still pass
// if the option list were never repainted (a stale label over a fresh value is
// exactly the bar/view contradiction #364 exists to rule out); a hidden
// control reports null so "the League select vanished" can never read as
// agreement.
const ctxRendered = (page) => page.evaluate(() => {
  const pick = (id) => {
    const el = document.getElementById(id);
    if (!el || el.hidden || el.offsetParent === null) return null;
    const opt = el.selectedOptions && el.selectedOptions[0];
    return { value: el.value, label: opt ? (opt.textContent || "").trim() : null };
  };
  return { prog: pick("ctx-select"), league: pick("ctx-league-select") };
});

// Real keyboard-only reachability (#345 review culture: page.focus() bypasses
// the actual Tab order and never triggers :focus-visible, so it proves
// neither "reachable by Tab" nor "has a real focus indicator" -- both legs
// this asserts). Starts from a blurred, defined point.
async function tabToSelect(page, selector, maxPresses) {
  await page.evaluate(() => {
    if (document.activeElement && document.activeElement.blur) document.activeElement.blur();
  });
  for (let i = 1; i <= (maxPresses || 200); i += 1) {
    await page.keyboard.press("Tab");
    const hit = await page.evaluate(
      (sel) => document.activeElement && document.activeElement === document.querySelector(sel),
      selector);
    if (hit) {
      const focusStyle = await page.evaluate((sel) => {
        const el = document.querySelector(sel);
        const cs = getComputedStyle(el);
        return { outlineStyle: cs.outlineStyle, outlineWidth: cs.outlineWidth, boxShadow: cs.boxShadow };
      }, selector);
      const visible = (focusStyle.outlineStyle !== "none" && focusStyle.outlineWidth !== "0px")
        || focusStyle.boxShadow !== "none";
      if (!visible) throw new Error(`${selector}: reached by Tab but has no visible focus indicator`);
      return i;
    }
  }
  throw new Error(`${selector}: not reachable via real keyboard Tab traversal within ${maxPresses || 200} presses`);
}

// A LeagueSeason row's own id is never exposed over HTTP (by design -- it is
// an internal join, not a resource callers address directly; the only writes
// against it are create_league_season/delete_league_season, reached through
// roll-forward and the generic v2 delete route respectively). Looked up
// directly from the SAME durable SQLite file the running server uses, from a
// separate process -- the identical technique season-rollover.js's own
// injectSecondActiveRegistration uses, for the same reason (no HTTP path
// exists for this fixture/verification need).
function lookupLeagueSeasonId(databasePath, leagueId, seasonId) {
  const script = `
import os
from hockey_scheduler.store import create_store
store = create_store(os.environ["FIXTURE_DB_PATH"])
try:
    ls = store.league_season_for(os.environ["FIXTURE_LEAGUE_ID"], os.environ["FIXTURE_SEASON_ID"])
    print(ls.id if ls else "")
finally:
    store.close()
`;
  const result = spawnSync(process.env.PYTHON || "python3", ["-c", script], {
    cwd: BACKEND_DIR,
    env: { ...process.env, FIXTURE_DB_PATH: databasePath, FIXTURE_LEAGUE_ID: leagueId, FIXTURE_SEASON_ID: seasonId },
    encoding: "utf8",
  });
  if (result.status !== 0) throw new Error(`LeagueSeason lookup failed: ${result.stderr}`);
  const id = result.stdout.trim();
  if (!id) throw new Error(`No LeagueSeason binds league=${leagueId} season=${seasonId}`);
  return id;
}

// Builds: Program "Zulu Program" (created FIRST -- so it sorts LAST by
// creation-assigned id, proving nothing here accidentally relies on
// "first in the id-sorted list"), Season S1 + S2, a League bound to BOTH
// (via one active-registration roll-forward round-trip -- create_league_season
// itself has no HTTP route; #345's own context_service.py docstring says
// binding a League to a Season "stays the authorized, audited job of
// setup_service.create_league_season", and roll-forward is its one
// HTTP-reachable caller, via _link_league_season's find-or-create), a
// second League bound to S1 ONLY (for the atomic-rejection case), a THIRD
// unbound Program "Alpha Program" (created SECOND, alphabetically first --
// the "B not alphabetically first" fixture), and a viewer account so the
// switcher renders without the League-Admin onboarding wizard taking over
// #content (context-switcher.js's own established convention).
async function buildCoreFixture(page, suffix) {
  return page.evaluate(async (sfx) => {
    const post = async (p, b) => (await fetch(p, {
      method: "POST", credentials: "same-origin",
      headers: { "Content-Type": "application/json" }, body: JSON.stringify(b),
    })).json();
    // An operator Organization, linked at Program creation (division-delete-
    // cleanup.js's own established fixture pattern): satisfies the client
    // onboarding wizard's own prerequisites so League Admin lands on the real
    // Setup tab for (K) below instead of the "Initial Setup" wizard taking
    // over #content -- unrelated to anything this journey itself asserts.
    const org = await post("/api/v2/setup/organization", { name: `Zulu Org ${sfx}` });
    const zulu = await post("/api/v2/setup/program",
      { name: `Zulu Program ${sfx}`, operator_organization_id: org.id });
    const s1 = await post("/api/v2/setup/season", { program_id: zulu.id, name: "S1 2026" });
    const s2 = await post("/api/v2/setup/season", { program_id: zulu.id, name: "S2 2027" });
    const s3 = await post("/api/v2/setup/season", { program_id: zulu.id, name: "S3 Unbound" });
    const dual = await post("/api/v2/setup/league", { season_id: s1.id, name: "Dual League" });
    const solo = await post("/api/v2/setup/league", { season_id: s1.id, name: "Solo League" });
    const club = await post("/api/v2/setup/club", { name: "Fixture Club" });
    const team = await post("/api/v2/setup/team",
      { league_id: dual.id, club_id: club.id, name: "Fixture Team" });
    await post(`/api/v2/setup/seasons/${s1.id}/team-registrations`,
      { team_id: team.id, league_id: dual.id, division_id: null });
    await post(`/api/v2/setup/seasons/${s2.id}/roll-forward`,
      { from_season_id: s1.id, selections: [{ team_id: team.id, league_id: dual.id }] });
    const alpha = await post("/api/v2/setup/program", { name: `Alpha Program ${sfx}` });
    const acct = await post("/api/accounts",
      { username: `lcb_seer_${sfx}`, password: "demo", role: "viewer", scope: {} });
    return {
      zulu: zulu.id, s1: s1.id, s2: s2.id, s3: s3.id,
      dual: dual.id, solo: solo.id, team: team.id, alpha: alpha.id, viewer: acct.username,
    };
  }, suffix);
}

// ---------------------------------------------------------------------------
// Core: render, persist+round-trip, dual-Season carry-forward, atomic
// rejection, cross-Program clear, v1-hash back-compat, keyboard, drawer
// seeding, zero overflow/console errors. Desktop AND 390x844.
// ---------------------------------------------------------------------------
async function checkCore(browser, viewport) {
  const base = `http://${HOST}:${viewport.port}`;
  const server = startServer(viewport.port, {});
  let out = "";
  server.stdout.on("data", (d) => { out += d.toString(); });
  server.stderr.on("data", (d) => { out += d.toString(); });
  const { context, page, errors } = await newPage(browser, viewport);
  const L = viewport.label;
  try {
    await waitForServer(`${base}/api/health`, READY_TIMEOUT_MS);
    await page.goto(base, { waitUntil: "domcontentloaded" });
    if ((await loginAs(page, "admin")).status !== 200) throw new Error(`[${L}] admin login failed`);
    const f = await buildCoreFixture(page, `${viewport.port}`);
    if ((await loginAs(page, f.viewer)).status !== 200) throw new Error(`[${L}] viewer login failed`);
    await reloadShell(page);

    // (A) Render: League select is HIDDEN until a Program with Leagues is
    // active; "No League" is the first, explicit option once shown.
    await page.selectOption("#ctx-select", `${f.zulu}|${f.s1}`);
    await page.waitForFunction(() => {
      const s = document.getElementById("ctx-league-select");
      return s && !s.hidden;
    }, null, { timeout: 10000 });
    const opts0 = await page.locator("#ctx-league-select option").evaluateAll(
      (os) => os.map((o) => ({ value: o.value, label: o.textContent })));
    if (opts0[0].value !== "" || !/no league/i.test(opts0[0].label)) {
      throw new Error(`[${L}] "No League" must be the first, explicit option: ${JSON.stringify(opts0)}`);
    }
    // Program-scoped, NOT Season-filtered: Solo League (bound to S1 only) is
    // STILL offered while S2 is active below -- proven together with (C).

    // (B) Select a League bound to the active Season -> persists, hash
    // updates, survives reload.
    await page.selectOption("#ctx-league-select", f.dual);
    await page.waitForFunction((h) => location.hash.indexOf(h) === 0, "#ctx=", { timeout: 10000 });
    let now = await ctxNow(page);
    if (now.season_id !== f.s1 || now.league_id !== f.dual) {
      throw new Error(`[${L}] League selection did not persist: ${JSON.stringify(now)}`);
    }
    await reloadShell(page);
    await page.waitForFunction((v) => document.getElementById("ctx-league-select").value === v,
      f.dual, { timeout: 10000 });
    if ((await ctxProgSel(page)) !== `${f.zulu}|${f.s1}`) {
      throw new Error(`[${L}] Program/Season did not survive reload alongside League`);
    }

    // (C) Dual-Season carry-forward -- the core #364 requirement: a League
    // bound to BOTH S1 and S2 must be reachable at S2 by switching the
    // Season select while it is active, not only by re-picking the League
    // after. Same-Program Season change carries League forward; the S2
    // binding must persist and round-trip exactly.
    await page.selectOption("#ctx-select", `${f.zulu}|${f.s2}`);
    await page.waitForFunction((v) => document.getElementById("ctx-league-select").value === v,
      f.dual, { timeout: 10000 });
    now = await ctxNow(page);
    if (now.season_id !== f.s2 || now.league_id !== f.dual) {
      throw new Error(`[${L}] League did not carry forward across a same-Program Season change: ${JSON.stringify(now)}`);
    }
    await reloadShell(page);
    await page.waitForFunction((v) => document.getElementById("ctx-league-select").value === v,
      f.dual, { timeout: 10000 });
    if ((await ctxProgSel(page)) !== `${f.zulu}|${f.s2}`) {
      throw new Error(`[${L}] S2+League round-trip lost the Season half on reload`);
    }

    // (D) Atomic rejection, which under the #364 owner ruling is now the
    // AMBIGUOUS case specifically. Dual League is bound to BOTH S1 and S2.
    // Switching to S3 (which binds nothing) while Dual is active leaves the
    // server holding SEVERAL valid bindings with the current Season not among
    // them -- rule 3, which deliberately never tie-breaks a Season on the
    // operator's behalf, because a deterministic pick would silently move them
    // into a Season they never chose and never saw. It must be refused
    // WHOLESALE: set_with_league validates every axis before writing anything,
    // so the prior (S1, Dual) context survives completely unchanged, both
    // selects snap back, and ONE generic toast (no existence oracle) appears.
    // Contrast (M) below, where the League has EXACTLY ONE valid binding and
    // the server therefore MOVES the Season instead of refusing -- the two
    // legs together are what pin rule 2 and rule 3 apart. (A League bound to
    // exactly one Season, e.g. Solo League here, is NOT a rejection case any
    // more: it is (M)'s canonical move.)
    await page.selectOption("#ctx-select", `${f.zulu}|${f.s1}`);
    await page.waitForFunction((v) => document.getElementById("ctx-select").value === v,
      `${f.zulu}|${f.s1}`, { timeout: 10000 });
    await waitForContext(page, (c) => c.season_id === f.s1 && c.league_id === f.dual,
      `[${L}] setup for (D) did not land on (S1, Dual League)`);
    await page.selectOption("#ctx-select", `${f.zulu}|${f.s3}`);
    await page.waitForFunction(() => {
      const t = document.getElementById("toast-root");
      return t && /isn't available/i.test(t.textContent || "");
    }, null, { timeout: 10000 });
    await page.waitForFunction((v) => document.getElementById("ctx-select").value === v,
      `${f.zulu}|${f.s1}`, { timeout: 10000 });
    if ((await ctxLeagueSel(page)) !== f.dual) {
      throw new Error(`[${L}] League select did not snap back to Dual League after the ambiguous carry`);
    }
    const afterReject = await ctxNow(page);
    if (afterReject.season_id !== f.s1 || afterReject.league_id !== f.dual) {
      throw new Error(`[${L}] rejected Season change was not fully atomic: ${JSON.stringify(afterReject)}`);
    }
    // ...and it never invented a binding to make the ambiguity go away.
    // Resolution is read-only over LeagueSeason (#364): S3 must still bind
    // nothing, so re-selecting it is refused identically the second time.
    await page.selectOption("#ctx-select", `${f.zulu}|${f.s3}`);
    await page.waitForFunction((v) => document.getElementById("ctx-select").value === v,
      `${f.zulu}|${f.s1}`, { timeout: 10000 });
    const afterReject2 = await ctxNow(page);
    if (afterReject2.season_id !== f.s1 || afterReject2.league_id !== f.dual) {
      throw new Error(`[${L}] a refused resolution created a binding: ${JSON.stringify(afterReject2)}`);
    }

    // (E) Explicit "No League" is first-class: selecting it clears league_id
    // and round-trips.
    await page.selectOption("#ctx-league-select", "");
    await page.waitForFunction(() => (document.getElementById("ctx-league-select").value || "") === "",
      null, { timeout: 10000 });
    now = await ctxNow(page);
    if (now.league_id !== null) throw new Error(`[${L}] "No League" did not persist as league_id:null`);
    await reloadShell(page);
    if ((await ctxLeagueSel(page)) !== "") throw new Error(`[${L}] explicit "No League" did not survive reload`);

    // (F) Cross-Program change always drops League, even toward a Program
    // that is ALPHABETICALLY first but was created SECOND (proves the
    // switch uses the real selected program_id, never an accidental
    // first-in-list default).
    await page.selectOption("#ctx-select", `${f.zulu}|${f.s1}`);
    await page.selectOption("#ctx-league-select", f.dual);
    await page.waitForFunction((v) => document.getElementById("ctx-league-select").value === v,
      f.dual, { timeout: 10000 });
    await page.selectOption("#ctx-select", `${f.alpha}|`);
    await page.waitForFunction((v) => document.getElementById("ctx-select").value === v,
      `${f.alpha}|`, { timeout: 10000 });
    now = await ctxNow(page);
    if (now.program_id !== f.alpha || now.league_id !== null) {
      throw new Error(`[${L}] Program change did not drop League: ${JSON.stringify(now)}`);
    }

    // (G) v1 hash backward-compat: a link minted before #364 (no "l" key)
    // must still adopt cleanly, league defaulting to null, no crash.
    await apiPost(page, "/api/context", { program_id: f.zulu, season_id: f.s1, league_id: f.dual });
    await page.evaluate((h) => { location.hash = h; }, encodeCtxV1(f.zulu, f.s2));
    await reloadShell(page);
    await page.waitForFunction((v) => document.getElementById("ctx-select").value === v,
      `${f.zulu}|${f.s2}`, { timeout: 10000 });
    now = await ctxNow(page);
    if (now.season_id !== f.s2 || now.league_id !== null) {
      throw new Error(`[${L}] v1 hash (no league key) did not adopt with league_id:null: ${JSON.stringify(now)}`);
    }

    // (H) A bogus/never-existed League id in a v2 hash must normalize
    // generically (no existence oracle) rather than crash or leak.
    await page.evaluate((h) => { location.hash = h; }, encodeCtx(f.zulu, f.s1, "ghost_league"));
    await reloadShell(page);
    const toastText = (await page.locator("#toast-root").textContent()) || "";
    if (/ghost_league/i.test(toastText)) throw new Error(`[${L}] bogus League id leaked into the toast`);
    if (/ghost_league/.test(await page.evaluate(() => location.hash))) {
      throw new Error(`[${L}] bogus League id left in the URL hash`);
    }

    // (I) The League select is operable by KEYBOARD ALONE: real Tab
    // traversal to a real, browser-computed visible focus indicator
    // (tabToSelect throws otherwise -- never page.focus(), the established
    // #345 review convention, since it bypasses the real Tab order and never
    // triggers :focus-visible), THEN a real native <select> keyboard
    // interaction -- type-ahead (pressing the first letter of the target
    // option's own label, e.g. "D" for "Dual League") -- commits the value
    // change and fires a genuine `change` event, exactly like a sighted
    // keyboard user or a screen-reader user would operate it. This is
    // deliberately NOT page.selectOption/a synthetic value assignment: it is
    // a real KeyboardEvent the browser itself resolves into a selection, so
    // this assertion is dead if the onchange wiring (or the select itself)
    // is ever removed -- the DOM value would still change (browser-native
    // type-ahead needs no JS), but the persisted server-side context below
    // would then never update and the wait would time out and fail.
    // (A bare ArrowDown on a CLOSED select, by contrast, does not reliably
    // commit a value in every headless Chromium build -- type-ahead does,
    // confirmed directly against this exact select/fixture shape.)
    await apiPost(page, "/api/context", { program_id: f.zulu, season_id: f.s1, league_id: null });
    await clearHash(page);
    await reloadShell(page);
    await tabToSelect(page, "#ctx-league-select", 200);
    const kbBeforeVal = await ctxLeagueSel(page);
    await page.keyboard.press("D"); // "Dual League" -- the only option starting with D
    await page.waitForFunction((b) => document.getElementById("ctx-league-select").value !== b,
      kbBeforeVal, { timeout: 10000 });
    const kbAfterVal = await ctxLeagueSel(page);
    if (kbAfterVal !== f.dual) {
      throw new Error(`[${L}] keyboard type-ahead selected the wrong League: got ${kbAfterVal}, expected ${f.dual}`);
    }
    const kbDeadline = Date.now() + 10000;
    for (;;) {
      const c = await ctxNow(page);
      if (c.league_id === f.dual) break;
      if (Date.now() > kbDeadline) throw new Error(`[${L}] keyboard-driven League change did not persist`);
      await new Promise((r) => setTimeout(r, 100));
    }

    // (J) No horizontal overflow at this viewport with the switcher's two
    // selects + note all rendered.
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth + 1);
    if (overflow) throw new Error(`[${L}] context switcher causes horizontal page overflow`);

    // (K) Drawer seeding respects the active League (#364 fixes to
    // contextSeededDrawerValues 'division'/'team') -- the concrete
    // "existing screens now respect the tuple" evidence. The "Divisions"
    // demoted action lives on the "Season participation and divisions"
    // workflow landing ([data-setup-workflow-act="division"]) and routes
    // through contextSeededDrawerValues, unlike the hierarchy tree's own
    // per-row "+ Add division" buttons (independently, directly prefilled
    // from the row they're rendered under -- a different, unrelated seeding
    // path). Set the context to (S2, Dual League) -- valid -- then confirm
    // the opened drawer seeds Dual League, not Solo League (Solo isn't even
    // in S2).
    if ((await loginAs(page, "admin")).status !== 200) throw new Error(`[${L}] admin re-login (drawer) failed`);
    await apiPost(page, "/api/context", { program_id: f.zulu, season_id: f.s2, league_id: f.dual });
    await clearHash(page);
    await reloadShell(page);
    // Two nav entries share data-tab="setup" (the Facilities-area composite
    // destination opens straight into the "facilities" workflow landing via
    // its own data-setup-workflow-nav, bypassing the hub); ":not(...)" is
    // this repo's own established convention (setup-workflow-hub.js,
    // role-authorization-matrix.js, seven-area-navigation.js) for reaching
    // the plain six-workflow hub specifically.
    await page.click('.tab[data-tab="setup"]:not([data-setup-workflow-nav])');
    await page.waitForFunction(
      () => !!document.querySelector('[data-setup-workflow="participation"]'), null, { timeout: 15000 });
    await page.click('[data-setup-workflow="participation"]');
    await page.waitForFunction(
      () => !!document.querySelector('[data-setup-workflow-act="division"]'), null, { timeout: 10000 });
    await page.click('[data-setup-workflow-act="division"]');
    await page.waitForSelector('.drawer[role="dialog"]', { timeout: 10000 });
    const seededLeague = await page.$eval("#f-div-league", (el) => el.value).catch(() => null);
    if (seededLeague !== f.dual) {
      throw new Error(`[${L}] Division drawer did not seed the active League (Dual): got ${seededLeague}`);
    }
    const seededSeason = await page.$eval("#f-div-season", (el) => el.value).catch(() => null);
    if (seededSeason !== f.s2) {
      throw new Error(`[${L}] Division drawer did not seed the active Season (S2): got ${seededSeason}`);
    }
    await page.keyboard.press("Escape");

    // (L) #364 review: zero horizontal overflow, both selects still reachable
    // by keyboard with a real visible focus indicator, and zero console/page
    // errors for MAXIMUM-PERMISSION roles specifically (the widest nav +
    // action set, and the two roles the review's own reproduction and fix
    // requirement name) with an active League selected -- not just Viewer,
    // which (J) already covers with a narrower nav/action set. Checked on
    // League Admin's CURRENT rendered state (still on Setup from (K), the
    // widest surface -- no reload here: this fixture's Program/org satisfies
    // enough onboarding prerequisites to open Setup directly, but not enough
    // for a fresh page load to skip landing back on the onboarding wizard
    // instead, which is a separate, unrelated flow this check has no reason
    // to depend on). Arena Manager gets its own fresh account, reload, and
    // check on the Dashboard, its own primary landing -- Arena Manager never
    // triggers that wizard regardless of onboarding completeness.
    let overflowL = await page.evaluate(() => document.documentElement.scrollWidth > document.documentElement.clientWidth);
    if (overflowL) throw new Error(`[${L}] League Admin: context bar causes horizontal page overflow`);
    await tabToSelect(page, "#ctx-league-select", 200);

    const arenaAcct = await page.evaluate(async () => {
      const r = await fetch("/api/accounts", {
        method: "POST", credentials: "same-origin", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ username: `lcb_arena_${Date.now()}`, password: "demo", role: "arena_manager", scope: {} }),
      });
      return (await r.json()).username;
    });
    if ((await loginAs(page, arenaAcct)).status !== 200) throw new Error(`[${L}] arena manager login failed`);
    if ((await apiPost(page, "/api/context",
      { program_id: f.zulu, season_id: f.s2, league_id: f.dual })).status !== 200) {
      throw new Error(`[${L}] arena manager could not select the active League`);
    }
    await clearHash(page);
    await reloadShell(page);
    overflowL = await page.evaluate(() => document.documentElement.scrollWidth > document.documentElement.clientWidth);
    if (overflowL) throw new Error(`[${L}] Arena Manager: context bar causes horizontal page overflow`);
    await tabToSelect(page, "#ctx-league-select", 200);
    const bothVisible = await page.evaluate(() => {
      const prog = document.getElementById("ctx-select");
      const lg = document.getElementById("ctx-league-select");
      const visible = (el) => !!el && el.offsetParent !== null;
      return visible(prog) && visible(lg);
    });
    if (!bothVisible) throw new Error(`[${L}] Arena Manager: both context selects must remain visible/reachable`);

    // (M) The #364 owner ruling itself, end to end through the REAL context
    // bar: the SERVER owns the canonical (Season, League) tuple, and the
    // returned Season may legitimately DIFFER from the one the browser sent.
    //
    // Starting on active S2, choosing Solo League -- bound ONLY to S1 -- is a
    // legitimate operator intent. Rule 1 does not apply (no S2 binding), rule 2
    // does: exactly one valid authorized ACTIVE binding, so the server moves
    // atomically to (S1, Solo League). Two failure modes this must rule out,
    // both of which a green "the POST returned 200" assertion would miss:
    // refusing the pick outright (the pre-#364 behavior, which left the primary
    // League control unable to commit), and committing the League while leaving
    // the operator parked on S2 with a League that is not in it.
    //
    // So the proof is AGREEMENT ACROSS EVERY SURFACE THAT CAN DISAGREE, not a
    // return value: the persisted row read back over HTTP through the page's
    // own fetch, BOTH context controls' rendered values AND their visible
    // labels, the URL hash, all of that again after a full page RELOAD, and
    // finally the rendered screen itself.
    const agreeOnS1Solo = async (stage) => {
      await waitForContext(page, (c) => c.program_id === f.zulu && c.season_id === f.s1
        && c.league_id === f.solo,
        `[${L}] (M) ${stage}: persisted server context is not (S1, Solo League)`);
      // The bar repaints from a SECOND round-trip (loadContextOptions) that
      // lands after the POST commits, so the controls are polled to their
      // SETTLED state instead of sampled the instant the server row changed.
      // A fixed sleep would either flake or quietly mask a real repaint lag;
      // a bounded poll that reports the last state it saw does neither -- a
      // bar that never converges still fails, and names what it was stuck on.
      const deadline = Date.now() + 10000;
      let bar;
      for (;;) {
        bar = await ctxRendered(page);
        if (bar.prog && bar.league && bar.prog.value === `${f.zulu}|${f.s1}`
          && bar.league.value === f.solo) break;
        if (Date.now() > deadline) {
          throw new Error(`[${L}] (M) ${stage}: the context bar never settled on `
            + `(S1, Solo League) -- last read ${JSON.stringify(bar)}`);
        }
        await new Promise((r) => setTimeout(r, 100));
      }
      // Same snapshot, now the VISIBLE labels: a fresh value under a stale
      // option list would still be a bar/view contradiction to a real reader.
      if (!/^S1 2026\b/.test(bar.prog.label || "")) {
        throw new Error(`[${L}] (M) ${stage}: the Program/Season control shows the wrong Season `
          + `label: ${JSON.stringify(bar.prog)}`);
      }
      if (bar.league.label !== "Solo League") {
        throw new Error(`[${L}] (M) ${stage}: the League control shows the wrong League `
          + `label: ${JSON.stringify(bar.league)}`);
      }
      // syncContextHash() runs off the POST echo, strictly before the repaint
      // just awaited, so by now the hash is settled too -- no second poll.
      const hash = decodeCtx(await page.evaluate(() => location.hash));
      if (!hash || hash.p !== f.zulu || hash.s !== f.s1 || hash.l !== f.solo) {
        throw new Error(`[${L}] (M) ${stage}: the URL hash disagrees with the committed tuple: ${JSON.stringify(hash)}`);
      }
    };

    // A fresh page load lands League Admin on the "Initial Setup" wizard,
    // whose view returns before render() ever paints the context bar -- the
    // same onboarding-flow dependency (L) above deliberately refuses to lean
    // on. Re-entering the real six-workflow Setup hub after each reload is
    // what makes the reload legs below assert the CONTEXT BAR rather than the
    // wizard's absence of one. It is plain navigation: it never POSTs a
    // context, so nothing here can manufacture the tuple being proven.
    const enterSetupHub = async () => {
      await page.click('.tab[data-tab="setup"]:not([data-setup-workflow-nav])');
      await page.waitForFunction(
        () => !!document.querySelector('[data-setup-workflow="participation"]'), null, { timeout: 15000 });
    };

    // Begin on active S2 with Dual League -- a real, fully-populated tuple, so
    // the move below has to change BOTH axes and cannot be confused with a
    // control that merely started out empty. League Admin throughout: the
    // context row is per-user, so the rendered-screen half at the end must be
    // the SAME account that made the keyboard pick, not a second session.
    if ((await loginAs(page, "admin")).status !== 200) throw new Error(`[${L}] admin re-login (M) failed`);
    if ((await apiPost(page, "/api/context",
      { program_id: f.zulu, season_id: f.s2, league_id: f.dual })).status !== 200) {
      throw new Error(`[${L}] (M) could not set up the starting (S2, Dual League) context`);
    }
    await clearHash(page);
    await reloadShell(page);
    await enterSetupHub();
    await page.waitForFunction((v) => document.getElementById("ctx-league-select").value === v,
      f.dual, { timeout: 10000 });
    const startBar = await ctxRendered(page);
    if (!startBar.prog || startBar.prog.value !== `${f.zulu}|${f.s2}`
      || !/^S2 2027\b/.test(startBar.prog.label)) {
      throw new Error(`[${L}] (M) did not begin on active S2: ${JSON.stringify(startBar)}`);
    }

    // The League change is driven by REAL keyboard activation -- Tab to the
    // control (through the genuine tab order, with a browser-computed visible
    // focus indicator) then native <select> type-ahead, "S" for "Solo League",
    // the only option whose label starts with S. This is deliberately NOT
    // page.selectOption: the browser itself resolves the keystroke into a
    // selection and fires a genuine `change` event, so if the onchange wiring
    // is ever removed the DOM value would still move (type-ahead needs no JS)
    // while nothing below would ever commit -- the assertions fail, as they
    // must. Consistent with (I) above.
    await tabToSelect(page, "#ctx-league-select", 200);
    await page.keyboard.press("S");
    await page.waitForFunction((v) => document.getElementById("ctx-league-select").value === v,
      f.solo, { timeout: 10000 });
    await agreeOnS1Solo("after the keyboard-driven League change");
    // The canonical MOVE is a success, not a recovered failure: the generic
    // refusal toast must never have been shown for it.
    const moveToast = (await page.locator("#toast-root").textContent()) || "";
    if (/isn't available/i.test(moveToast)) {
      throw new Error(`[${L}] (M) the canonical Season move surfaced a rejection toast: "${moveToast}"`);
    }

    // ...and all of it survives a full page load from scratch.
    await reloadShell(page);
    await enterSetupHub();
    await page.waitForFunction((v) => document.getElementById("ctx-league-select").value === v,
      f.solo, { timeout: 10000 });
    await agreeOnS1Solo("after a full page reload");

    // Finally the RENDERED SCREEN, which must not contradict the bar. The
    // Division drawer seeds from the active (Season, League) through
    // contextSeededDrawerValues and FAILS CLOSED (`leagueNotInSeason`, no
    // drawer at all) when the active League is not in the active Season -- so
    // if the bar said (S1, Solo) while the view still believed S2, this drawer
    // would never open. That it opens seeded with exactly S1 + Solo League is
    // the two halves agreeing, from the view's own independent lookup.
    await page.click('[data-setup-workflow="participation"]');
    await page.waitForFunction(
      () => !!document.querySelector('[data-setup-workflow-act="division"]'), null, { timeout: 10000 });
    await page.click('[data-setup-workflow-act="division"]');
    await page.waitForSelector('.drawer[role="dialog"]', { timeout: 10000 });
    const movedLeague = await page.$eval("#f-div-league", (el) => el.value).catch(() => null);
    const movedSeason = await page.$eval("#f-div-season", (el) => el.value).catch(() => null);
    if (movedLeague !== f.solo || movedSeason !== f.s1) {
      throw new Error(`[${L}] (M) the view contradicts the context bar -- Division drawer seeded `
        + `league=${movedLeague} season=${movedSeason}, expected league=${f.solo} season=${f.s1}`);
    }
    await page.keyboard.press("Escape");
    // The bar itself is still on the canonical tuple after all that screen
    // work -- no surface drifted back to the requested-but-not-canonical S2.
    await agreeOnS1Solo("after exercising the rendered screen");

    if (errors.length) throw new Error(`[${L}] console/page errors:\n${errors.join("\n")}`);
    console.log(`[${L}] OK — render, persist/round-trip, dual-Season carry-forward, atomic rejection of the `
      + `ambiguous case, server-owned canonical Season move (S2 -> S1) agreed by server/both controls/hash/`
      + `reload/rendered screen, explicit No League, cross-Program clear, v1/bogus hash handling, keyboard, `
      + `drawer seeding, no overflow (incl. max-permission roles with an active League).`);
  } catch (error) {
    throw new Error(`${error.message}\n--- server output ---\n${out}`);
  } finally {
    await context.close();
    await stopServer(server);
  }
}

// ---------------------------------------------------------------------------
// Missing / unbound+restore / revoked+restore.
// ---------------------------------------------------------------------------
async function checkUnboundRevokedRestore(browser) {
  const base = `http://${HOST}:${UNBOUND_PORT}`;
  const tempDir = fs.mkdtempSync(path.join(os.tmpdir(), "hockey-lcb-"));
  const databasePath = path.join(tempDir, "unbound.sqlite");
  const server = startServer(UNBOUND_PORT, { DATABASE_URL: databasePath });
  let out = "";
  server.stdout.on("data", (d) => { out += d.toString(); });
  server.stderr.on("data", (d) => { out += d.toString(); });
  const { context, page, errors } = await newPage(browser, { width: 1440, height: 900 });
  const admin = await newPage(browser, { width: 1440, height: 900 });
  try {
    await waitForServer(`${base}/api/health`, READY_TIMEOUT_MS);
    await page.goto(base, { waitUntil: "domcontentloaded" });
    if ((await loginAs(page, "admin")).status !== 200) throw new Error("admin login failed");
    const f = await page.evaluate(async () => {
      const post = async (p, b) => (await fetch(p, {
        method: "POST", credentials: "same-origin",
        headers: { "Content-Type": "application/json" }, body: JSON.stringify(b),
      })).json();
      const program = await post("/api/v2/setup/program", { name: "Unbound Program" });
      const s1 = await post("/api/v2/setup/season", { program_id: program.id, name: "S1" });
      const league = await post("/api/v2/setup/league", { season_id: s1.id, name: "Revocable League" });
      const club = await post("/api/v2/setup/club", { name: "Revocable Club" });
      const teamA = await post("/api/v2/setup/team", { league_id: league.id, club_id: club.id, name: "Team A" });
      const teamB = await post("/api/v2/setup/team", { league_id: league.id, club_id: club.id, name: "Team B" });
      // Coach's own authorized_season_ids is derived from its team's ACTIVE
      // registration (context_scope.py's _team_season_ids), not merely its
      // permanent League's bindings -- an unregistered Team authorizes no
      // Season at all.
      const teamAReg = await post(`/api/v2/setup/seasons/${s1.id}/team-registrations`,
        { team_id: teamA.id, league_id: league.id, division_id: null });
      const otherLeague = await post("/api/v2/setup/league", { season_id: s1.id, name: "Other League" });
      const coach = await post("/api/accounts",
        { username: "lcb_coach", password: "demo", role: "coach", scope: { team_id: teamA.id } });
      const viewer = await post("/api/accounts",
        { username: "lcb_seer_missing", password: "demo", role: "viewer", scope: {} });
      return { program: program.id, s1: s1.id, league: league.id, otherLeague: otherLeague.id,
        teamA: teamA.id, teamB: teamB.id, teamAReg: teamAReg.id,
        coach: coach.username, viewer: viewer.username };
    });

    // ---- (1) Missing: a never-existed League id in a v2 hash normalizes
    //      generically, never crashes, never leaks the bogus id.
    if ((await loginAs(page, f.viewer)).status !== 200) throw new Error("viewer login failed");
    await reloadShell(page);
    await page.evaluate((h) => { location.hash = h; }, encodeCtx(f.program, f.s1, "totally_bogus_league"));
    await reloadShell(page);
    const toast1 = (await page.locator("#toast-root").textContent()) || "";
    if (!/isn't available|saved context/i.test(toast1)) {
      throw new Error(`missing-League hash did not normalize with a generic message: "${toast1}"`);
    }
    if (/totally_bogus_league/.test(await page.evaluate(() => location.hash))) {
      throw new Error("bogus League id left in the URL hash");
    }

    await admin.page.goto(base, { waitUntil: "domcontentloaded" });
    if ((await loginAs(admin.page, "admin")).status !== 200) throw new Error("admin (actor) login failed");

    // ---- (2) Revoked + restoration: a Coach's own League access is derived
    //      from their team's CURRENT permanent League. Transferring the team
    //      revokes it; transferring back restores it -- the same self-
    //      healing "saved row never rewritten" rule (3) below proves for a
    //      removed binding, exercised here through a SCOPED role instead.
    //      Runs BEFORE (3): Team A's own active registration (needed for
    //      Coach to have Season access at all) is a dependent of the very
    //      LeagueSeason binding (3) deletes, so it is removed at the end of
    //      this section rather than coexisting with that delete.
    if ((await loginAs(page, f.coach)).status !== 200) throw new Error("coach login failed");
    const coachSelectRes = await apiPost(page, "/api/context",
      { program_id: f.program, season_id: f.s1, league_id: f.league });
    if (coachSelectRes.status !== 200) {
      throw new Error(`coach could not select their own League: ${JSON.stringify(coachSelectRes)}`);
    }
    if ((await apiPost(admin.page, `/api/v2/setup/team/${f.teamA}/assign-league`,
      { league_id: f.otherLeague })).status !== 200) {
      throw new Error("concurrent team transfer failed");
    }
    let now = await ctxNow(page);
    if (now.league_id !== null) {
      throw new Error(`Coach's revoked League must resolve to null, not leak the old one: ${JSON.stringify(now)}`);
    }
    if ((await apiPost(admin.page, `/api/v2/setup/team/${f.teamA}/assign-league`,
      { league_id: f.league })).status !== 200) {
      throw new Error("concurrent team transfer-back failed");
    }
    now = await ctxNow(page);
    if (now.league_id !== f.league) {
      throw new Error(`Coach's League selection did not self-heal once access was restored: ${JSON.stringify(now)}`);
    }
    // Clears the way for (3) below to delete this same LeagueSeason binding:
    // Team A's own registration is a dependent regardless of active status
    // (delete_league_season's dependency scan counts every registration row,
    // not only active ones), so a HARD delete is required -- but
    // delete_season_team_registration only ever accepts an already-INACTIVE
    // row (by design: never destroy a live registration), so deactivate via
    // /remove first, then purge via /delete.
    if ((await apiPost(admin.page, `/api/v2/setup/season-team-registration/${f.teamAReg}/remove`, {}))
      .status !== 200) {
      throw new Error("deactivating Team A's registration before (3) failed");
    }
    if ((await apiPost(admin.page, `/api/v2/setup/season-team-registration/${f.teamAReg}/delete`, {}))
      .status !== 200) {
      throw new Error("deleting Team A's registration before (3) failed");
    }

    // ---- (3) Unbound + restoration: select a real, currently-bound League,
    //      then a CONCURRENT admin session removes the binding. The saved
    //      row is never rewritten (context_service's own documented
    //      behavior) -- it must silently resolve to League:null on the very
    //      next GET, with the Season left intact -- and REBINDING it must
    //      silently restore the selection with zero client action.
    if ((await loginAs(page, f.viewer)).status !== 200) throw new Error("viewer re-login (3) failed");
    await apiPost(page, "/api/context", { program_id: f.program, season_id: f.s1, league_id: f.league });
    await clearHash(page);
    now = await ctxNow(page);
    if (now.league_id !== f.league) throw new Error("setup for (3) did not select the real League");

    const lsId = lookupLeagueSeasonId(databasePath, f.league, f.s1);
    const unbindRes = await apiPost(admin.page, `/api/v2/setup/league-season/${lsId}/delete`, {});
    if (unbindRes.status !== 200) throw new Error(`concurrent unbind failed: ${JSON.stringify(unbindRes)}`);

    now = await ctxNow(page);
    if (now.season_id !== f.s1) throw new Error(`unbind must not disturb the Season: ${JSON.stringify(now)}`);
    if (now.league_id !== null) {
      throw new Error(`League must silently resolve to null once its binding is gone: ${JSON.stringify(now)}`);
    }
    await reloadShell(page);
    if ((await ctxLeagueSel(page)) !== "") throw new Error("UI did not reflect the silently-dropped League");
    if ((await ctxProgSel(page)) !== `${f.program}|${f.s1}`) throw new Error("Season was lost alongside the League");

    // Restore: rebind via the same roll-forward mechanism (create_league_season
    // itself has no HTTP route; _link_league_season find-or-creates). Roll-
    // forward requires distinct source/target seasons, so a fresh throwaway
    // season -- registering Team B into the League there, then rolling that
    // single selection INTO S1 -- is the one HTTP-reachable way to re-bind an
    // existing League back onto a Season it already left.
    const throwaway = await admin.page.evaluate(async (programId) => {
      const post = async (p, b) => (await fetch(p, {
        method: "POST", credentials: "same-origin",
        headers: { "Content-Type": "application/json" }, body: JSON.stringify(b),
      })).json();
      return (await post("/api/v2/setup/season", { program_id: programId, name: "S-throwaway" })).id;
    }, f.program);
    if ((await apiPost(admin.page, `/api/v2/setup/seasons/${throwaway}/team-registrations`,
      { team_id: f.teamB, league_id: f.league, division_id: null })).status !== 200) {
      throw new Error("throwaway registration failed");
    }
    const restoreRoll = await apiPost(admin.page, `/api/v2/setup/seasons/${f.s1}/roll-forward`,
      { from_season_id: throwaway, selections: [{ team_id: f.teamB, league_id: f.league }] });
    if (restoreRoll.status !== 200) throw new Error(`restore rebind failed: ${JSON.stringify(restoreRoll)}`);

    now = await ctxNow(page); // no client action beyond a fresh GET
    if (now.league_id !== f.league) {
      throw new Error(`saved League selection did not self-heal once rebound: ${JSON.stringify(now)}`);
    }

    if (errors.length) throw new Error(`console/page errors:\n${errors.join("\n")}`);
    console.log("[unbound/revoked/restore] OK — missing id normalizes; unbind and Coach-scope revocation both "
      + "silently drop to null without disturbing the Season, and self-heal with zero client action once restored.");
  } catch (error) {
    throw new Error(`${error.message}\n--- server output ---\n${out}`);
  } finally {
    await admin.context.close();
    await context.close();
    await stopServer(server);
  }
}

// ---------------------------------------------------------------------------
// Race guard: the newest (Season, League) intent must win even when an
// earlier request's response is artificially delayed past a later one's.
// ---------------------------------------------------------------------------
async function checkRaceGuard(browser) {
  const base = `http://${HOST}:${RACE_PORT}`;
  const server = startServer(RACE_PORT, {});
  let out = "";
  server.stdout.on("data", (d) => { out += d.toString(); });
  server.stderr.on("data", (d) => { out += d.toString(); });
  const { context, page, errors } = await newPage(browser, { width: 1440, height: 900 });
  try {
    await waitForServer(`${base}/api/health`, READY_TIMEOUT_MS);
    await page.goto(base, { waitUntil: "domcontentloaded" });
    if ((await loginAs(page, "admin")).status !== 200) throw new Error("admin login failed");
    const f = await page.evaluate(async () => {
      const post = async (p, b) => (await fetch(p, {
        method: "POST", credentials: "same-origin",
        headers: { "Content-Type": "application/json" }, body: JSON.stringify(b),
      })).json();
      const program = await post("/api/v2/setup/program", { name: "Race Program" });
      const s1 = await post("/api/v2/setup/season", { program_id: program.id, name: "S1" });
      const leagueA = await post("/api/v2/setup/league", { season_id: s1.id, name: "League A" });
      const leagueB = await post("/api/v2/setup/league", { season_id: s1.id, name: "League B" });
      const acct = await post("/api/accounts",
        { username: "lcb_racer", password: "demo", role: "viewer", scope: {} });
      return { program: program.id, s1: s1.id, leagueA: leagueA.id, leagueB: leagueB.id, viewer: acct.username };
    });
    if ((await loginAs(page, f.viewer)).status !== 200) throw new Error("viewer login failed");
    await apiPost(page, "/api/context", { program_id: f.program, season_id: f.s1, league_id: null });
    await reloadShell(page);
    await page.selectOption("#ctx-select", `${f.program}|${f.s1}`);
    await page.waitForFunction((v) => document.getElementById("ctx-league-select").hidden === false,
      null, { timeout: 10000 });

    // Delay the FIRST POST /api/context response well past when the second,
    // un-delayed one would land -- the queued-dequeue guard must still make
    // the SECOND (newest) intent win, and the first's stale response must
    // never even flash on screen (contextSwitchSeq/contextRevision, now
    // carrying leagueId, discard it on arrival).
    let firstSeen = false;
    await page.route("**/api/context", async (route) => {
      if (route.request().method() === "POST" && !firstSeen) {
        firstSeen = true;
        await new Promise((r) => setTimeout(r, 1500));
      }
      await route.continue();
    });

    await page.selectOption("#ctx-league-select", f.leagueA);   // request #1 -- delayed
    await page.waitForFunction(() => true, null, { timeout: 50 }).catch(() => {});
    await page.selectOption("#ctx-league-select", f.leagueB);   // request #2 -- queued, sent immediately on #1's arrival

    await page.waitForFunction((v) => document.getElementById("ctx-league-select").value === v,
      f.leagueB, { timeout: 10000 });
    // Give the delayed first response time to arrive and (if the guard were
    // broken) clobber the render.
    await new Promise((r) => setTimeout(r, 2000));
    await page.unroute("**/api/context");

    const finalDom = await ctxLeagueSel(page);
    const finalServer = (await ctxNow(page)).league_id;
    if (finalDom !== f.leagueB || finalServer !== f.leagueB) {
      throw new Error(`race guard failed -- newest intent (League B) did not win: `
        + `dom=${finalDom} server=${finalServer} (League A=${f.leagueA} League B=${f.leagueB})`);
    }

    if (errors.length) throw new Error(`console/page errors:\n${errors.join("\n")}`);
    console.log("[race guard] OK — a deliberately delayed stale response never overwrote the newer "
      + "(Season, League) intent, in the DOM or on the server.");
  } catch (error) {
    throw new Error(`${error.message}\n--- server output ---\n${out}`);
  } finally {
    await context.close();
    await stopServer(server);
  }
}

// ---------------------------------------------------------------------------
// Role scoping: GET /api/context/options never offers a League a role can't
// select (authorized_league_ids, #363 -- pre-existing; this proves the
// CLIENT-facing contract, not the backend rule's own correctness).
// ---------------------------------------------------------------------------
async function checkRoleScoping(browser) {
  const base = `http://${HOST}:${ROLE_PORT}`;
  const server = startServer(ROLE_PORT, {});
  let out = "";
  server.stdout.on("data", (d) => { out += d.toString(); });
  server.stderr.on("data", (d) => { out += d.toString(); });
  const { context, page, errors } = await newPage(browser, { width: 1440, height: 900 });
  try {
    await waitForServer(`${base}/api/health`, READY_TIMEOUT_MS);
    await page.goto(base, { waitUntil: "domcontentloaded" });
    if ((await loginAs(page, "admin")).status !== 200) throw new Error("admin login failed");
    const f = await page.evaluate(async () => {
      const post = async (p, b) => (await fetch(p, {
        method: "POST", credentials: "same-origin",
        headers: { "Content-Type": "application/json" }, body: JSON.stringify(b),
      })).json();
      const program = await post("/api/v2/setup/program", { name: "Scope Program" });
      const s1 = await post("/api/v2/setup/season", { program_id: program.id, name: "S1" });
      const ownLeague = await post("/api/v2/setup/league", { season_id: s1.id, name: "Own League" });
      const otherLeague = await post("/api/v2/setup/league", { season_id: s1.id, name: "Other League" });
      const club = await post("/api/v2/setup/club", { name: "Scope Club" });
      const ownTeam = await post("/api/v2/setup/team",
        { league_id: ownLeague.id, club_id: club.id, name: "Own Team" });
      const player = await post("/api/v2/setup/player",
        { team_id: ownTeam.id, name: "Scope Player", position: "forward" });
      const official = await post("/api/v2/setup/official", { name: "Scope Official" });
      const PW = "scope-pw";
      const coach = await post("/api/accounts",
        { username: "lcb_scope_coach", password: PW, role: "coach", scope: { team_id: ownTeam.id } });
      const plyr = await post("/api/accounts",
        { username: "lcb_scope_player", password: PW,
          role: "player", scope: { player_id: player.id, team_id: ownTeam.id } });
      const guardian = await post("/api/accounts",
        { username: "lcb_scope_guardian", password: PW, role: "guardian", scope: {} });
      const link = await post("/api/guardians/links",
        { guardian_user_id: guardian.id, player_id: player.id });
      await post(`/api/guardians/links/${link.id}/verify`, { consent_method: "verbal_confirmed" });
      const arena = await post("/api/accounts",
        { username: "lcb_scope_arena", password: PW, role: "arena_manager", scope: {} });
      const official_acct = await post("/api/accounts",
        { username: "lcb_scope_official", password: PW,
          role: "official", scope: { official_id: official.id } });
      const viewer = await post("/api/accounts",
        { username: "lcb_scope_viewer", password: PW, role: "viewer", scope: {} });
      return { program: program.id, ownLeague: ownLeague.id, otherLeague: otherLeague.id, PW,
        coach: coach.username, player: plyr.username, guardian: guardian.username,
        arena: arena.username, official: official_acct.username, viewer: viewer.username };
    });

    const leaguesFor = async (username, password) => {
      if ((await loginAs(page, username, password)).status !== 200) {
        throw new Error(`login failed for ${username}`);
      }
      const opts = (await apiGet(page, "/api/context/options")).json;
      const program = (opts.programs || []).find((p) => p.id === f.program);
      return (program && program.leagues || []).map((lg) => lg.id).sort();
    };

    const global = [f.ownLeague, f.otherLeague].sort();
    for (const [label, username, password, expected] of [
      ["League Admin", "admin", "demo", global],
      ["Arena Manager", f.arena, f.PW, global],
      ["Viewer", f.viewer, f.PW, global],
      ["Coach", f.coach, f.PW, [f.ownLeague]],
      ["Player", f.player, f.PW, [f.ownLeague]],
      ["Guardian", f.guardian, f.PW, [f.ownLeague]],
      ["Official (no assignments)", f.official, f.PW, []],
    ]) {
      const got = await leaguesFor(username, password);
      if (JSON.stringify(got) !== JSON.stringify(expected)) {
        throw new Error(`${label}: expected authorized League set ${JSON.stringify(expected)}, `
          + `got ${JSON.stringify(got)}`);
      }
    }

    if (errors.length) throw new Error(`console/page errors:\n${errors.join("\n")}`);
    console.log("[role scoping] OK — global roles see every Program League; Coach/Player/Guardian see only "
      + "their own team's; an Official with no assignments sees none.");
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
    for (const viewport of VIEWPORTS) await checkCore(browser, viewport);
    await checkUnboundRevokedRestore(browser);
    await checkRaceGuard(browser);
    await checkRoleScoping(browser);
    console.log("League context bar browser journey passed.");
  } catch (error) {
    console.error("League context bar browser journey FAILED.");
    console.error(error && error.message ? error.message : error);
    process.exitCode = 1;
  } finally {
    if (browser) await browser.close();
  }
}

main();
