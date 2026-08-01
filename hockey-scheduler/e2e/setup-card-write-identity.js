// Stale-response identity for a card-INITIATED WRITE (#365 review round 4).
//
// THE DEFECT THIS EXISTS FOR, verbatim from the review:
//
//   "The new Reopen-this-season action drops its card identity before the
//   write settles, announces success before the response, and lets an older
//   tuple's response mutate the current tuple. In resolveSetupCardConfirm(),
//   confirming immediately restores READY, removes the reason field, and
//   announces `Season reopened.` BEFORE reopenSelectedSeasonFromCard() awaits
//   the POST. The async function checks the tuple only BEFORE that await;
//   after it, both error and success update the global toast, and success
//   calls retrySetupWorkflowCard(key) without proving the initiating identity
//   is still current."
//
//   "With the reopen POST held then forced to 503, the page already said
//   'Season reopened', the confirmation/reason had vanished, and focus was on
//   BODY while the request was still pending. Failure then left the card READY
//   with the reopen action, no retained reason/error confirmation, and focus
//   still on BODY."
//
//   "Stale-success: let Program/Season A's reopen commit server-side but hold
//   its successful response, switch fully to Program/Season B, wait for B to
//   settle. Before release, B's Facilities card was generation 7, EMPTY, with
//   'Add venue'. Releasing A's old success changed B to generation 8, replaced
//   the announcement/toast with 'Venues, rinks and ice updated', and moved
//   focus to B's landing heading. An older tuple response therefore mutates
//   the new tuple's generation, DOM/model, focus, announcement,
//   completion/next inputs — the exact race #365 requires every card action to
//   discard."
//
// Every other Setup card path already verifies card + tuple + generation at
// all five mutation points (DOM, focus, announcement, completion, next task),
// with cardIdentityCurrent() as the gate. The reopen was the one path written
// as fire-and-forget optimism. This journey is the regression for applying
// that SAME discipline to it — it asserts nothing about a parallel mechanism,
// only that the established one is now honoured across the write's await.
//
// WHY A SEPARATE FILE FROM setup-prerequisite-floors.js
// -----------------------------------------------------
// That journey's subject is the COMPLETE ORDERED FLOOR SET a card consumes,
// and its (B) leg performs an ordinary, undisturbed reopen as the recovery
// proof. This journey's subject is what happens to that write when the
// response is DELAYED and the operator moves underneath it — a different
// failure class needing route interception, two full Program/Season tuples and
// before/after snapshot comparison. Merging them would bury both.
//
// WHAT IT ASSERTS, at desktop and canonical 390x844:
//
//  (1) HELD POST, THEN 503. While the write is unresolved: the card is
//      PENDING, aria-busy, offers NO control at all (a second press would be
//      a second write), makes NO success or completion claim, leaves the
//      completion count and next-task recommendation exactly as they were, and
//      holds keyboard focus on its own pending line rather than dropping it to
//      <body>. On failure: an actionable confirmation is back, carrying the
//      EXACT reason the operator typed plus the server's own message, with
//      focus on the reason field itself. The Season is still archived
//      server-side throughout — the 503 is fulfilled without route.fetch(), so
//      the server genuinely never takes the write.
//
//  (2) STALE SUCCESS. A's reopen is allowed to COMMIT server-side (route.fetch
//      really runs) and only its DELIVERY is held. The operator then switches
//      fully to Program/Season B through the real #ctx-select and B settles.
//      Releasing A's success must change NOTHING about B: generation,
//      committed model, card DOM, landing action groups, aria-busy, focus,
//      live-region/toast text, completion line and next task are compared by
//      SNAPSHOT, before and after. Afterwards the operator returns to A and
//      the card there IS unblocked — proving the held response really was a
//      committed success that the client correctly refused to apply to B,
//      rather than a no-op that would have changed nothing anyway.
//
//  (3) STALE ERROR. The same shape with a delayed FAILURE response. A
//      superseded failure must be as inert as a superseded success: no toast,
//      no re-opened confirmation on B's card, no focus theft.
//
//  (4) NORMAL SUCCESS. Exactly ONE success announcement, said AFTER the
//      response — not the reported premature "Season reopened." followed by
//      the refresh's own "Venues, rinks and ice updated." — and the refresh
//      touches only this card: every other card's generation and committed
//      model is byte-identical across the whole operation.
//
// TECHNIQUE
// ---------
//   * Delayed responses use this repo's established idiom: capture the REAL
//     response first (`const r = await route.fetch()`), hold, then
//     `route.fulfill({ response: r })`. Delaying the REQUEST would merely make
//     the server answer later and would prove nothing about a response
//     arriving into a tuple that has moved on.
//   * "Unchanged" is proven by SNAPSHOT COMPARISON, field by field, not by a
//     loose re-assertion of expected values — a snapshot catches a mutation
//     this file never thought to name.
//   * Non-vacuity is asserted at every turn: A and B are different on BOTH the
//     Program and the Season axis; B's snapshot must DIFFER from A's (or the
//     comparison would be measuring nothing); the held success must really
//     have committed; and the reopen response must really have been delivered
//     before the "after" snapshot is taken.
//   * Announcements are read off the ONE sitewide live region (#toast-root)
//     with a MutationObserver, so the count is what a screen reader would
//     actually hear rather than what the code intended to say.
//
// Fails on any browser console/page error.
const { chromium } = require("playwright");
const { spawn } = require("child_process");
const http = require("http");
const path = require("path");

const HOST = "127.0.0.1";
const BACKEND_DIR = path.resolve(__dirname, "..", "backend");
const READY_TIMEOUT_MS = 15000;
const VIEWPORTS = [
  { label: "desktop", width: 1440, height: 900, port: 8395 },
  { label: "phone", width: 390, height: 844, port: 8396 },
];

const REOPEN_RE = /\/api\/v2\/setup\/seasons\/[^/]+\/reopen$/;
// The card's own declared copy (app.js SETUP_SEASON_REOPEN_ACTION). Asserted
// as exact strings: "exactly one success announcement" is a claim about a
// specific sentence, and a regex would let the refresh's generic sentence slip
// through under a different wording.
const BUSY_TEXT = "Reopening this season…";
const DONE_TEXT = "Season reopened — it can be changed again.";
// The sentence the reported defect emitted SECOND, describing the refresh
// rather than what the operator did. It must never be heard for a reopen.
const REFRESH_TEXT = "Venues, rinks and ice updated.";

function waitForServer(url, timeoutMs) {
  const deadline = Date.now() + timeoutMs;
  return new Promise((resolve, reject) => {
    const tick = () => {
      const req = http.get(url, (res) => { res.resume(); resolve(); });
      req.setTimeout(2000, () => req.destroy(new Error("request timed out")));
      req.on("error", () => {
        if (Date.now() > deadline) reject(new Error(`server never came up at ${url}`));
        else setTimeout(tick, 200);
      });
    };
    tick();
  });
}

function stopServer(server) {
  return new Promise((resolve) => {
    if (!server || server.exitCode !== null) return resolve();
    server.once("exit", () => resolve());
    server.kill("SIGTERM");
    setTimeout(() => { try { server.kill("SIGKILL"); } catch (e) {} resolve(); }, 3000);
  });
}

async function apiPost(page, path_, body) {
  return page.evaluate(async ([p, b]) => (await fetch(p, {
    method: "POST", credentials: "same-origin",
    headers: { "Content-Type": "application/json" }, body: JSON.stringify(b || {}),
  })).json(), [path_, body || {}]);
}

async function loginAs(page, username, password) {
  const res = await apiPost(page, "/api/auth/login", { username, password });
  if (!res || res.error) {
    throw new Error(`login as ${username} failed: ${JSON.stringify(res)}`);
  }
}

function fail(msg) { throw new Error(msg); }

// A fresh page load in the given context. contextOptions is seeded once per
// page load, so a raw /api/context POST needs this to take effect — and the
// leftover "#ctx=" deep link has to go first, or bootstrap() faithfully POSTs
// the PREVIOUS selection straight back over it.
async function reenter(page, base) {
  await page.evaluate(() => history.replaceState(
    null, "", location.pathname + location.search));
  await page.goto(base, { waitUntil: "domcontentloaded" });
  await page.waitForSelector("#content > *", { timeout: 15000 });
}

// Open the Facilities landing through the hub's own permission-gated "Open …"
// button — the transition a real operator uses — and wait for the card to
// SETTLE. LOADING and PENDING both withdraw every action by design, so
// sampling mid-flight would read zero controls and blame the wrong thing.
async function openFacilities(page, step) {
  await page.click('.tab[data-tab="setup"]:not([data-setup-workflow-nav])');
  await page.waitForSelector('[data-setup-workflow="facilities"]', { timeout: 15000 })
    .catch(() => fail(`[${step}] the Setup hub never offered "facilities"`));
  await page.click('[data-setup-workflow="facilities"]');
  await page.waitForSelector('[data-setup-workflow-landing="facilities"]',
    { timeout: 15000 })
    .catch(() => fail(`[${step}] the facilities landing never rendered`));
  await settled(page, step);
}

// Settled == not mid-read (LOADING) and not mid-write (PENDING). PENDING is a
// state this journey creates on purpose, so excluding it is what makes every
// "settled" wait below honest rather than a wait that returns instantly.
async function settled(page, step, seasonId) {
  await page.waitForFunction((sid) => {
    const e = readCardState("setup/facilities");
    if (e.state === "loading" || e.state === "pending" || e.state === "stale") return false;
    return !sid || (e.identity && e.identity.season_id === sid);
  }, seasonId || null, { timeout: 15000 })
    .catch(() => fail(`[${step}] the facilities card never settled`
      + `${seasonId ? ` on season ${seasonId}` : ""}`));
}

// Record every write to the ONE sitewide live region, in order. This is what a
// screen reader is actually handed, so "exactly one success announcement" is
// measured against the region rather than against the code's intent.
async function armAnnouncements(page) {
  await page.evaluate(() => {
    const root = document.getElementById("toast-root");
    if (!root) throw new Error("no #toast-root to observe");
    window.__ann = [];
    const read = () => {
      const msg = root.querySelector(".toast-msg");
      return { text: root.hidden ? "" : (msg ? msg.textContent.trim() : ""),
               error: root.classList.contains("error") };
    };
    const rec = () => {
      const now = read();
      const last = window.__ann[window.__ann.length - 1];
      if (!last || last.text !== now.text || last.error !== now.error) {
        window.__ann.push(now);
      }
    };
    if (window.__annObs) window.__annObs.disconnect();
    window.__annObs = new MutationObserver(rec);
    window.__annObs.observe(root, { childList: true, subtree: true,
      characterData: true, attributes: true });
    rec();
  });
}

// Only the SPOKEN entries: updateToast()'s 4-second auto-clear writes an empty
// region, which is a dismissal, not an announcement.
async function spoken(page) {
  return (await page.evaluate(() => window.__ann || []))
    .filter((a) => a.text).map((a) => ({ text: a.text, error: !!a.error }));
}

async function resetAnnouncements(page) {
  await page.evaluate(() => { window.__ann = []; });
}

// EVERYTHING a stale response could reach, in one comparable record: the
// card's generation and committed model, the DOM both surfaces paint from, the
// landing's action groups, aria-busy, keyboard focus, the live region, and the
// completion line plus next-task recommendation. Compared field by field
// before and after a release, so "unchanged" is proven rather than asserted
// loosely — and so a mutation this file never thought to name is still caught.
async function snapshot(page) {
  return page.evaluate(() => {
    const describe = (el) => {
      if (!el) return "null";
      const attrs = ["data-setup-card-pending", "data-setup-card-confirm-reason",
                     "data-setup-card-confirm-yes", "data-setup-card-retry",
                     "data-setup-workflow-go", "id"]
        .map((a) => `${a}=${el.getAttribute && el.getAttribute(a) || ""}`).join(",");
      return `${el.tagName}[${el.className || ""}][${attrs}]`
        + `{${(el.textContent || "").replace(/\s+/g, " ").trim().slice(0, 80)}}`;
    };
    const slot = document.querySelector('[data-setup-card-slot="facilities"]');
    const actions = document.querySelector('[data-setup-landing-actions="facilities"]');
    const root = document.getElementById("toast-root");
    const msg = root && root.querySelector(".toast-msg");
    return {
      generations: JSON.stringify(cardGenerations),
      models: JSON.stringify(cardStates),
      // (4) completion and (5) next task, as the model AND as the sentence the
      // hub paints from it — a stale response could move either.
      rollup: JSON.stringify(setupHubRollup()),
      rollupHtml: setupHubProgressHtml(),
      slotHtml: slot ? slot.innerHTML : "no-slot",
      slotBusy: slot ? slot.getAttribute("aria-busy") : "no-slot",
      actionsHtml: actions ? actions.innerHTML : "no-actions",
      buttons: JSON.stringify(actions
        ? Array.from(actions.querySelectorAll("button")).map((b) => b.textContent.trim())
        : null),
      focus: describe(document.activeElement),
      toast: root && !root.hidden ? (msg ? msg.textContent.trim() : "") : "",
      toastError: !!(root && root.classList.contains("error")),
      selected: JSON.stringify((contextOptions && contextOptions.selected) || null),
    };
  });
}

const SNAPSHOT_FIELDS = ["generations", "models", "rollup", "rollupHtml", "slotHtml",
  "slotBusy", "actionsHtml", "buttons", "focus", "toast", "toastError", "selected"];

function assertSame(before, after, L, step) {
  for (const field of SNAPSHOT_FIELDS) {
    if (before[field] !== after[field]) {
      fail(`[${L}/${step}] releasing the superseded response changed B's `
        + `"${field}" — an older tuple's response mutated the current tuple, `
        + `which is exactly the race #365 requires every card action to `
        + `discard.\n  before: ${String(before[field]).slice(0, 900)}\n`
        + `  after:  ${String(after[field]).slice(0, 900)}`);
    }
  }
}

function assertDiffers(a, b, L, step) {
  const same = SNAPSHOT_FIELDS.filter((f) => a[f] === b[f]);
  if (same.length === SNAPSHOT_FIELDS.length) {
    fail(`[${L}/${step}] the two tuples produce IDENTICAL snapshots, so the `
      + `before/after comparison below would pass no matter what happened`);
  }
}

// The card's live view of the blocked/unblocked question, read from its own
// committed model plus the DOM it produced.
async function readCard(page) {
  return page.evaluate(() => {
    const entry = readCardState("setup/facilities");
    const root = document.querySelector('[data-setup-workflow-landing="facilities"]');
    const box = root && root.querySelector("[data-setup-landing-actions]");
    const slot = document.querySelector('[data-setup-card-slot="facilities"]');
    const reason = document.querySelector('[data-setup-card-confirm-reason="facilities"]');
    const err = slot && slot.querySelector(".swf-card-error");
    return {
      state: entry.state,
      generation: entry.identity ? entry.identity.generation : null,
      seasonId: entry.identity ? entry.identity.season_id : null,
      programId: entry.identity ? entry.identity.program_id : null,
      blockedBecause: entry.blockedBecause || null,
      effective: entry.effective === undefined ? "undefined"
        : entry.effective === null ? null : entry.effective.label,
      busy: slot ? slot.getAttribute("aria-busy") : null,
      slotButtons: slot ? Array.from(slot.querySelectorAll("button"))
        .map((b) => b.textContent.trim()) : null,
      actionButtons: box ? Array.from(box.querySelectorAll("button"))
        .map((b) => b.textContent.trim()) : null,
      goRoutes: root ? Array.from(root.querySelectorAll("[data-setup-workflow-go]"))
        .map((b) => b.dataset.setupWorkflowGo) : null,
      reasonValue: reason ? reason.value : null,
      confirmError: err ? err.textContent.replace(/\s+/g, " ").trim() : null,
      pendingText: slot && slot.querySelector("[data-setup-card-pending]")
        ? slot.querySelector("[data-setup-card-pending]").textContent.trim() : null,
      focusPending: !!(document.activeElement
        && document.activeElement.hasAttribute
        && document.activeElement.hasAttribute("data-setup-card-pending")),
      focusReason: !!(document.activeElement
        && document.activeElement.hasAttribute
        && document.activeElement.hasAttribute("data-setup-card-confirm-reason")),
      focusTag: document.activeElement ? document.activeElement.tagName : null,
    };
  });
}

// Is the selected Season still archived, according to the SERVER's own
// prerequisite assertion? The 503 legs must never actually reopen anything.
async function seasonActiveFloorMet(page) {
  return page.evaluate(async () => {
    const pr = await (await fetch("/api/v2/setup/progress",
                                  { credentials: "same-origin" })).json();
    const row = ((pr && pr.workflows) || []).find((w) => w.key === "facilities");
    const p = ((row && row.prerequisites) || []).find((x) => x.key === "season_active");
    return p ? p.met === true : null;
  });
}

// Open the confirmation through the card's own primary control and type the
// reason #159 requires. Returns nothing; throws if the control is not there.
async function openConfirmWithReason(page, step, reason) {
  await page.click('[data-setup-landing-actions="facilities"] .act.primary');
  await page.waitForSelector('[data-setup-card-confirm-reason="facilities"]',
    { timeout: 10000 })
    .catch(() => fail(`[${step}] the reopen control did not open a confirmation `
      + `with the reason field #159 requires`));
  await page.fill('[data-setup-card-confirm-reason="facilities"]', reason);
}

// Switch context through the REAL #ctx-select, exactly as an operator does —
// never through a raw /api/context POST, which bypasses setActiveContext()'s
// whole invalidate/withdraw/re-render sequence and would prove nothing about
// what a switch does to a card's identity.
async function switchContext(page, programId, seasonId, step) {
  const ok = await page.evaluate(([p, s]) => {
    const sel = document.getElementById("ctx-select");
    if (!sel) return "no #ctx-select";
    const want = `${p}|${s}`;
    if (!Array.from(sel.options).some((o) => o.value === want)) {
      return `#ctx-select offers no option "${want}": `
        + JSON.stringify(Array.from(sel.options).map((o) => o.value));
    }
    sel.value = want;
    sel.dispatchEvent(new Event("change", { bubbles: true }));
    return true;
  }, [programId, seasonId]);
  if (ok !== true) fail(`[${step}] ${ok}`);
  await page.waitForFunction((s) => {
    const cur = (contextOptions && contextOptions.selected) || {};
    return cur.season_id === s;
  }, seasonId, { timeout: 15000 })
    .catch(() => fail(`[${step}] the context never settled on season ${seasonId}`));
}

// One archived Season, with a Venue, a Rink and an ACTIVE venue-access grant,
// under the given Program. Non-vacuous by construction: the counts are
// non-zero and the grant is live, so `season_active` is the ONLY unmet floor
// and the card's one offered control is the real reopen path.
async function archivedSeasonFixture(page, programId, orgId, venueId, name) {
  const made = await page.evaluate(async ([pid, oid, vid, nm]) => {
    const post = async (p, b) => (await fetch(p, {
      method: "POST", credentials: "same-origin",
      headers: { "Content-Type": "application/json" }, body: JSON.stringify(b),
    })).json();
    const season = await post("/api/v2/setup/season", { program_id: pid, name: nm });
    await post("/api/context", { program_id: pid, season_id: season.id });
    const access = await post(`/api/v2/setup/seasons/${season.id}/venue-access`,
      { venue_id: vid });
    const archived = await post(`/api/v2/setup/seasons/${season.id}/archive`,
      { reason: "write-identity fixture" });
    return { season: season.id, name: season.name, access: access && access.id,
             archived: !!archived && !archived.error, org: oid };
  }, [programId, orgId, venueId, name]);
  if (!made.season || !made.access || !made.archived) {
    fail(`fixture "${name}" incomplete: ${JSON.stringify(made)}`);
  }
  return made;
}

async function checkViewport(browser, viewport) {
  const base = `http://${HOST}:${viewport.port}`;
  const server = spawn(
    process.env.PYTHON || "python3",
    ["-u", "-m", "hockey_scheduler.web.server", "--host", HOST, "--port", String(viewport.port)],
    { cwd: BACKEND_DIR, stdio: ["ignore", "pipe", "pipe"] });
  let serverOutput = "";
  server.stdout.on("data", (d) => { serverOutput += d.toString(); });
  server.stderr.on("data", (d) => { serverOutput += d.toString(); });

  const context = await browser.newContext({
    viewport: { width: viewport.width, height: viewport.height },
  });
  const page = await context.newPage();
  const errors = [];
  page.on("pageerror", (e) => errors.push(`[pageerror] ${e.message}`));
  // Chromium logs a "Failed to load resource" console error for every non-2xx
  // fetch, and this journey injects 503s on purpose. Absorbed on a BUDGET
  // rather than by blanket-ignoring the pattern (the role-authorization-matrix
  // idiom): exactly one line is forgiven per 503 this file actually injected,
  // so an unrelated failed request is still a hard failure.
  let resourceAllowance = 0;
  page.on("console", (m) => {
    if (m.type() !== "error") return;
    if (/Failed to load resource/.test(m.text()) && resourceAllowance > 0) {
      resourceAllowance -= 1;
      return;
    }
    errors.push(`[console] ${m.text()}`);
  });
  // Deliveries the PAGE actually received, so "the response was released" is
  // observed rather than assumed before any "after" snapshot is taken.
  let reopenDeliveries = 0;
  page.on("response", (r) => { if (REOPEN_RE.test(new URL(r.url()).pathname)) reopenDeliveries++; });
  const L = viewport.label;

  try {
    await waitForServer(`${base}/api/health`, READY_TIMEOUT_MS);
    await page.goto(base, { waitUntil: "domcontentloaded" });
    await page.waitForSelector("#content > *", { timeout: 15000 });
    await loginAs(page, "admin", "demo");
    const loadStatus = await page.evaluate(() => fetch("/api/demo/load", {
      method: "POST", credentials: "same-origin",
      headers: { "Content-Type": "application/json" }, body: "{}",
    }).then((r) => r.status));
    if (loadStatus !== 200) fail(`[${L}] demo load failed (status ${loadStatus})`);

    // ---------------------------- fixtures -----------------------------
    // Program A owns four archived Seasons — one per leg, because two of the
    // legs really do reopen theirs and a reused Season would silently make the
    // next leg's blocked precondition false.
    // Program B owns one ACTIVE Season with no Venue at all, so its Facilities
    // card settles into a DIFFERENT state entirely (EMPTY, "Add venue") — the
    // reviewer's own B fixture, and what makes the snapshot comparison
    // sensitive on every axis rather than only on the generation counter.
    const shared = await page.evaluate(async () => {
      const post = async (p, b) => (await fetch(p, {
        method: "POST", credentials: "same-origin",
        headers: { "Content-Type": "application/json" }, body: JSON.stringify(b),
      })).json();
      const org = await post("/api/v2/setup/organization", { name: "WI Org" });
      const pa = await post("/api/v2/setup/program",
        { name: "WI Program A", country: "US", operator_organization_id: org.id });
      const pb = await post("/api/v2/setup/program",
        { name: "WI Program B", country: "US", operator_organization_id: org.id });
      const venue = await post("/api/v2/setup/venue",
        { name: "WI Venue", organization_id: org.id });
      const rink = await post("/api/v2/setup/rink",
        { venue_id: venue.id, name: "WI Rink" });
      const sb = await post("/api/v2/setup/season",
        { program_id: pb.id, name: "WI Season B" });
      return { org: org.id, pa: pa.id, pb: pb.id, venue: venue.id,
               rink: rink.id, sb: sb.id, sbName: sb.name };
    });
    for (const k of ["org", "pa", "pb", "venue", "rink", "sb"]) {
      if (!shared[k]) fail(`[${L}] shared fixture failed to create ${k}: `
        + `${JSON.stringify(shared)}`);
    }
    if (shared.pa === shared.pb) {
      fail(`[${L}] A and B share a Program, so a switch would move only the `
        + `Season axis and the tuple comparison would be half-blind`);
    }
    const s1 = await archivedSeasonFixture(page, shared.pa, shared.org, shared.venue, "WI Season A1");
    const s2 = await archivedSeasonFixture(page, shared.pa, shared.org, shared.venue, "WI Season A2");
    const s3 = await archivedSeasonFixture(page, shared.pa, shared.org, shared.venue, "WI Season A3");
    const s4 = await archivedSeasonFixture(page, shared.pa, shared.org, shared.venue, "WI Season A4");

    // =================== (1) HELD POST, THEN 503 =======================
    await apiPost(page, "/api/context", { program_id: shared.pa, season_id: s1.season });
    await reenter(page, base);
    await openFacilities(page, `${L}/1`);
    await armAnnouncements(page);

    const beforeWrite = await readCard(page);
    if (beforeWrite.effective !== "Reopen this season") {
      fail(`[${L}/1] the fixture does not offer the reopen path (effective `
        + `${JSON.stringify(beforeWrite.effective)}, state "${beforeWrite.state}") — `
        + `there is no card write to race`);
    }
    if (await seasonActiveFloorMet(page) !== false) {
      fail(`[${L}/1] the fixture's season_active floor is not unmet, so the `
        + `Season is not archived and this leg proves nothing`);
    }
    const rollupBefore = await snapshot(page);

    await openConfirmWithReason(page, `${L}/1`, "spring split restart");
    await resetAnnouncements(page);

    // Hold the POST, then answer 503 WITHOUT route.fetch() — the server must
    // genuinely never take this write, or "failure" would not mean failure.
    let releaseOne = () => {};
    let markOneHeld = () => {};
    const oneHeld = new Promise((resolve) => { markOneHeld = resolve; });
    const oneGate = new Promise((resolve) => { releaseOne = resolve; });
    const holdAndFail = async (route) => {
      markOneHeld();
      await oneGate;
      resourceAllowance += 1;
      await route.fulfill({ status: 503, contentType: "application/json",
        body: JSON.stringify({ error: { code: "server_unavailable",
          message: "The server is temporarily unavailable (503). Please try "
            + "again in a moment." } }) });
    };
    await page.route(REOPEN_RE, holdAndFail);
    await page.click("[data-setup-card-confirm-yes]");
    await oneHeld;
    // Give the page a beat to do whatever it is going to do while the write is
    // outstanding. A correct build settles into PENDING; the reported build
    // had already claimed success by now.
    await page.waitForTimeout(400);

    const pending = await readCard(page);
    if (pending.state !== "pending") {
      fail(`[${L}/1/pending] the card reads "${pending.state}" while its own `
        + `reopen POST is unresolved — the write's outcome is not known, so no `
        + `settled state may be shown yet`);
    }
    if (pending.busy !== "true") {
      fail(`[${L}/1/pending] aria-busy is "${pending.busy}" while a write is in `
        + `flight; assistive technology is told the region is idle`);
    }
    if ((pending.slotButtons || []).length !== 0
        || (pending.actionButtons || []).length !== 0) {
      fail(`[${L}/1/pending] the card is still actionable while its write is `
        + `unresolved (card ${JSON.stringify(pending.slotButtons)}, landing `
        + `${JSON.stringify(pending.actionButtons)}) — a second press would `
        + `fire a second reopen`);
    }
    if (!pending.focusPending) {
      fail(`[${L}/1/pending] keyboard focus is on <${pending.focusTag}> while `
        + `the write is pending; the reported defect left it on BODY, stranding `
        + `a keyboard or screen-reader operator mid-operation`);
    }
    if (pending.pendingText !== BUSY_TEXT) {
      fail(`[${L}/1/pending] the pending line reads `
        + `${JSON.stringify(pending.pendingText)}, expected `
        + `${JSON.stringify(BUSY_TEXT)}`);
    }
    const heldSpoken = await spoken(page);
    const premature = heldSpoken.filter((a) =>
      a.text === DONE_TEXT || a.text === REFRESH_TEXT || /reopened\b/i.test(a.text));
    if (premature.length) {
      fail(`[${L}/1/pending] a success/completion sentence was announced BEFORE `
        + `the response: ${JSON.stringify(premature)} (full sequence `
        + `${JSON.stringify(heldSpoken)})`);
    }
    if (heldSpoken.length !== 1 || heldSpoken[0].text !== BUSY_TEXT) {
      fail(`[${L}/1/pending] expected exactly the progress sentence `
        + `${JSON.stringify(BUSY_TEXT)} while pending, got `
        + `${JSON.stringify(heldSpoken)}`);
    }
    const pendingSnap = await snapshot(page);
    if (pendingSnap.rollup !== rollupBefore.rollup
        || pendingSnap.rollupHtml !== rollupBefore.rollupHtml) {
      fail(`[${L}/1/pending] the completion count / next-task recommendation `
        + `moved while the write was still unresolved — nothing has been `
        + `written, so nothing may be counted as progress.\n  before: `
        + `${rollupBefore.rollup}\n  after:  ${pendingSnap.rollup}`);
    }
    if (await seasonActiveFloorMet(page) !== false) {
      fail(`[${L}/1/pending] the server already reports the Season active while `
        + `its reopen is held and unanswered`);
    }

    releaseOne();
    await page.waitForFunction(() =>
      readCardState("setup/facilities").state === "confirm", null, { timeout: 15000 })
      .catch(() => fail(`[${L}/1/failed] the failed reopen never restored an `
        + `actionable confirmation`));
    await page.unroute(REOPEN_RE, holdAndFail);

    const failedCard = await readCard(page);
    if (failedCard.reasonValue !== "spring split restart") {
      fail(`[${L}/1/failed] the restored confirmation lost the operator's own `
        + `reason (${JSON.stringify(failedCard.reasonValue)}) — they must not `
        + `have to type it again`);
    }
    if (!failedCard.confirmError || !/503|unavailable/i.test(failedCard.confirmError)) {
      fail(`[${L}/1/failed] the restored confirmation does not carry the `
        + `server's own message: ${JSON.stringify(failedCard.confirmError)}`);
    }
    if (!failedCard.focusReason) {
      fail(`[${L}/1/failed] keyboard focus landed on <${failedCard.focusTag}>, `
        + `not on the reason field the operator has to correct — the reported `
        + `defect left it on BODY`);
    }
    if (!(failedCard.slotButtons || []).some((b) => /reopen season/i.test(b))) {
      fail(`[${L}/1/failed] the failure left no scoped retry: `
        + `${JSON.stringify(failedCard.slotButtons)}`);
    }
    const failedSpoken = await spoken(page);
    if (failedSpoken.some((a) => a.text === DONE_TEXT || a.text === REFRESH_TEXT)) {
      fail(`[${L}/1/failed] a failed reopen still announced success: `
        + `${JSON.stringify(failedSpoken)}`);
    }
    const last = failedSpoken[failedSpoken.length - 1];
    if (!last || !last.error || !/503|unavailable/i.test(last.text)) {
      fail(`[${L}/1/failed] the failure was not announced as an error: `
        + `${JSON.stringify(failedSpoken)}`);
    }
    const failedSnap = await snapshot(page);
    if (failedSnap.rollup !== rollupBefore.rollup) {
      fail(`[${L}/1/failed] a failed reopen still moved the completion / `
        + `next-task derivation.\n  before: ${rollupBefore.rollup}\n`
        + `  after:  ${failedSnap.rollup}`);
    }
    if (await seasonActiveFloorMet(page) !== false) {
      fail(`[${L}/1/failed] the Season is active after a 503 that never reached `
        + `the server`);
    }

    // ============= (2) STALE SUCCESS, RELEASED ONTO TUPLE B =============
    await apiPost(page, "/api/context", { program_id: shared.pa, season_id: s2.season });
    await reenter(page, base);
    await openFacilities(page, `${L}/2`);
    await armAnnouncements(page);
    await openConfirmWithReason(page, `${L}/2`, "A2 reopened for the race");

    // The REAL request runs (route.fetch), so the reopen genuinely COMMITS
    // server-side; only its DELIVERY to the page is held. Delaying the request
    // instead would prove nothing — the whole point is a response arriving
    // into a tuple that has moved on.
    let releaseTwo = () => {};
    let markTwoHeld = () => {};
    const twoHeld = new Promise((resolve) => { markTwoHeld = resolve; });
    const twoGate = new Promise((resolve) => { releaseTwo = resolve; });
    let capturedStatus = null;
    let capturedBody = null;
    const holdSuccess = async (route) => {
      const response = await route.fetch();          // committed HERE...
      capturedStatus = response.status();
      try { capturedBody = await response.json(); } catch (e) { capturedBody = null; }
      markTwoHeld();
      await twoGate;
      await route.fulfill({ response });             // ...delivered only HERE
    };
    await page.route(REOPEN_RE, holdSuccess);
    await page.click("[data-setup-card-confirm-yes]");
    await twoHeld;
    if (capturedStatus !== 200 || !capturedBody || capturedBody.error) {
      fail(`[${L}/2] the held response is not a committed success (status `
        + `${capturedStatus}, body ${JSON.stringify(capturedBody)}) — a stale `
        + `no-op would change nothing whatever the client did`);
    }
    const deliveriesBeforeRelease = reopenDeliveries;

    // Switch FULLY to Program/Season B and let it settle. The write is still
    // outstanding the whole time.
    await switchContext(page, shared.pb, shared.sb, `${L}/2`);
    await settled(page, `${L}/2/B`, shared.sb);
    await page.waitForTimeout(300);
    const bCard = await readCard(page);
    if (bCard.seasonId !== shared.sb || bCard.programId !== shared.pb) {
      fail(`[${L}/2/B] B's card is bound to `
        + `${JSON.stringify({ p: bCard.programId, s: bCard.seasonId })}, not to `
        + `Program/Season B`);
    }
    const bBefore = await snapshot(page);
    assertDiffers(rollupBefore, bBefore, L, "2/B");
    if (reopenDeliveries !== deliveriesBeforeRelease) {
      fail(`[${L}/2/B] the held response was delivered before the release`);
    }

    releaseTwo();
    // Wait until the page has actually RECEIVED the release, then give it a
    // generous window to do damage. A correct build does nothing at all here,
    // so there is no positive signal to wait for — only elapsed time.
    const deadline = Date.now() + 10000;
    while (reopenDeliveries === deliveriesBeforeRelease && Date.now() < deadline) {
      await page.waitForTimeout(100);
    }
    if (reopenDeliveries === deliveriesBeforeRelease) {
      fail(`[${L}/2] the released response never reached the page, so nothing `
        + `was actually raced`);
    }
    await page.waitForTimeout(1500);
    await page.unroute(REOPEN_RE, holdSuccess);

    const bAfter = await snapshot(page);
    assertSame(bBefore, bAfter, L, "2");
    const bSpoken = await spoken(page);
    if (bSpoken.some((a) => a.text === DONE_TEXT || a.text === REFRESH_TEXT)) {
      fail(`[${L}/2] an older tuple's success spoke into B's live region: `
        + `${JSON.stringify(bSpoken)}`);
    }

    // NON-VACUITY, the other way round: the held response really WAS a
    // committed reopen. Going back to A must find the card unblocked — so the
    // client refused to apply a REAL success to the wrong tuple, rather than
    // discarding something that would have changed nothing anyway.
    await switchContext(page, shared.pa, s2.season, `${L}/2/back`);
    await settled(page, `${L}/2/back`, s2.season);
    const backCard = await readCard(page);
    if (backCard.blockedBecause !== null) {
      fail(`[${L}/2/back] Season A2 still reports "${backCard.blockedBecause}" — `
        + `the held response was not a committed reopen after all, so leg 2 `
        + `raced a no-op`);
    }
    if (backCard.effective !== "Add Ice") {
      fail(`[${L}/2/back] Season A2's card did not advance to its declared `
        + `primary after the committed reopen; effective is `
        + `${JSON.stringify(backCard.effective)}`);
    }

    // ============== (3) STALE ERROR, RELEASED ONTO TUPLE B ==============
    await apiPost(page, "/api/context", { program_id: shared.pa, season_id: s3.season });
    await reenter(page, base);
    await openFacilities(page, `${L}/3`);
    await armAnnouncements(page);
    await openConfirmWithReason(page, `${L}/3`, "A3 reopened for the race");

    let releaseThree = () => {};
    let markThreeHeld = () => {};
    const threeHeld = new Promise((resolve) => { markThreeHeld = resolve; });
    const threeGate = new Promise((resolve) => { releaseThree = resolve; });
    const holdError = async (route) => {
      markThreeHeld();
      await threeGate;
      resourceAllowance += 1;
      await route.fulfill({ status: 503, contentType: "application/json",
        body: JSON.stringify({ error: { code: "server_unavailable",
          message: "The server is temporarily unavailable (503). Please try "
            + "again in a moment." } }) });
    };
    await page.route(REOPEN_RE, holdError);
    await page.click("[data-setup-card-confirm-yes]");
    await threeHeld;
    const deliveriesBeforeThree = reopenDeliveries;

    await switchContext(page, shared.pb, shared.sb, `${L}/3`);
    await settled(page, `${L}/3/B`, shared.sb);
    await page.waitForTimeout(300);
    const b3Before = await snapshot(page);
    assertDiffers(rollupBefore, b3Before, L, "3/B");

    releaseThree();
    const deadline3 = Date.now() + 10000;
    while (reopenDeliveries === deliveriesBeforeThree && Date.now() < deadline3) {
      await page.waitForTimeout(100);
    }
    if (reopenDeliveries === deliveriesBeforeThree) {
      fail(`[${L}/3] the released error never reached the page, so nothing was `
        + `actually raced`);
    }
    await page.waitForTimeout(1500);
    await page.unroute(REOPEN_RE, holdError);

    const b3After = await snapshot(page);
    assertSame(b3Before, b3After, L, "3");
    const b3Spoken = await spoken(page);
    if (b3Spoken.some((a) => /unavailable|503|could not be reopened/i.test(a.text))) {
      fail(`[${L}/3] an older tuple's FAILURE spoke into B's live region: `
        + `${JSON.stringify(b3Spoken)} — a superseded response must do nothing, `
        + `and that includes saying nothing`);
    }
    const b3Card = await readCard(page);
    if (b3Card.state === "confirm" || b3Card.state === "pending") {
      fail(`[${L}/3] the superseded failure re-opened a confirmation on B's `
        + `card (state "${b3Card.state}") — A's reason and A's error, on B`);
    }

    // ==================== (4) NORMAL SUCCESS ============================
    await apiPost(page, "/api/context", { program_id: shared.pa, season_id: s4.season });
    await reenter(page, base);
    await openFacilities(page, `${L}/4`);
    await armAnnouncements(page);
    await openConfirmWithReason(page, `${L}/4`, "A4 reopened normally");

    // A sentinel that survives everything EXCEPT a document reload, plus every
    // OTHER card's generation and committed model, so "refreshes only this
    // card" is asserted rather than assumed.
    await page.evaluate(() => { window.__wiNoReload = "sentinel"; });
    const others = () => page.evaluate(() => ({
      gens: JSON.stringify(cardGenerations),
      models: Object.keys(cardStates).filter((k) => k !== "setup/facilities")
        .reduce((acc, k) => { acc[k] = JSON.stringify(cardStates[k]); return acc; }, {}),
    }));
    const othersBefore = await others();
    await resetAnnouncements(page);

    const okResp = page.waitForResponse((r) =>
      REOPEN_RE.test(new URL(r.url()).pathname) && r.request().method() === "POST");
    await page.click("[data-setup-card-confirm-yes]");
    const okBody = await (await okResp).json();
    if (!okBody || okBody.error) {
      fail(`[${L}/4] the normal reopen failed: ${JSON.stringify(okBody)}`);
    }
    await settled(page, `${L}/4`, s4.season);
    await page.waitForTimeout(300);

    const done = await readCard(page);
    if (done.blockedBecause !== null || done.effective !== "Add Ice") {
      fail(`[${L}/4] the card did not advance after a successful reopen `
        + `(blockedBecause ${JSON.stringify(done.blockedBecause)}, effective `
        + `${JSON.stringify(done.effective)})`);
    }
    if ((done.goRoutes || []).indexOf("facilities") === -1) {
      fail(`[${L}/4] no control is wired to the Ice Availability Builder once `
        + `the season is active again: ${JSON.stringify(done.goRoutes)}`);
    }
    const okSpoken = await spoken(page);
    const successes = okSpoken.filter((a) => a.text === DONE_TEXT);
    if (successes.length !== 1) {
      fail(`[${L}/4] expected EXACTLY ONE success announcement `
        + `(${JSON.stringify(DONE_TEXT)}), got ${successes.length} in `
        + `${JSON.stringify(okSpoken)}`);
    }
    if (okSpoken.some((a) => a.text === REFRESH_TEXT)) {
      fail(`[${L}/4] the card refresh's own generic sentence `
        + `${JSON.stringify(REFRESH_TEXT)} was announced as well — that is the `
        + `reported double announcement, describing the refresh instead of what `
        + `the operator did: ${JSON.stringify(okSpoken)}`);
    }
    if (okSpoken[okSpoken.length - 1].text !== DONE_TEXT) {
      fail(`[${L}/4] the success sentence is not the last thing said: `
        + `${JSON.stringify(okSpoken)}`);
    }
    if (okSpoken[0].text !== BUSY_TEXT) {
      fail(`[${L}/4] the first thing said after confirming was `
        + `${JSON.stringify(okSpoken[0])}, expected the progress sentence — a `
        + `completion sentence here would precede the response`);
    }
    if (await page.evaluate(() => window.__wiNoReload || null) !== "sentinel") {
      fail(`[${L}/4] the page reloaded during the reopen — the card is required `
        + `to advance without one`);
    }
    const othersAfter = await others();
    const gensBefore = JSON.parse(othersBefore.gens);
    const gensAfter = JSON.parse(othersAfter.gens);
    if (!(gensAfter["setup/facilities"] > gensBefore["setup/facilities"])) {
      fail(`[${L}/4] the reopen issued no new generation for this card `
        + `(${gensBefore["setup/facilities"]} -> `
        + `${gensAfter["setup/facilities"]}), so it asserted nothing`);
    }
    for (const key of Object.keys(othersBefore.models)) {
      if (gensBefore[key] !== gensAfter[key]) {
        fail(`[${L}/4] the reopen moved "${key}"'s generation `
          + `(${gensBefore[key]} -> ${gensAfter[key]}) — a card write may `
          + `refresh only its own card`);
      }
      if (othersBefore.models[key] !== othersAfter.models[key]) {
        fail(`[${L}/4] the reopen mutated adjacent card "${key}"'s committed model`);
      }
    }

    if (errors.length) fail(`[${L}] browser errors:\n${errors.join("\n")}`);
    console.log(`[${L}] OK — a held reopen POST leaves the Facilities card `
      + `PENDING and aria-busy with NO control on either surface, keyboard focus `
      + `on its own pending line, the completion count and next-task `
      + `recommendation untouched, and no success or completion sentence spoken; `
      + `its 503 restores an actionable confirmation carrying the exact reason `
      + `typed plus the server's own message, with focus on the reason field. A `
      + `reopen that COMMITS server-side but whose success is held, released `
      + `after a full switch to Program/Season B, changes nothing about B — `
      + `generation, committed model, card DOM, landing actions, aria-busy, `
      + `focus, live region, completion line and next task all compare `
      + `identical by snapshot — while returning to Season A2 proves the `
      + `discarded response really was a committed success. A delayed FAILURE `
      + `after the same switch is equally inert. A normal success refreshes only `
      + `this card, with no reload, no adjacent generation or model touched, and `
      + `exactly ONE success announcement, said last and after the response.`);
  } catch (e) {
    if (serverOutput.trim()) {
      console.error("--- demo server output ---\n" + serverOutput.trim());
    }
    throw e;
  } finally {
    await context.close();
    await stopServer(server);
  }
}

(async () => {
  const browser = await chromium.launch();
  try {
    for (const viewport of VIEWPORTS) await checkViewport(browser, viewport);
    console.log("Setup card write-identity browser journey passed.");
  } catch (e) {
    console.error("Setup card write-identity browser journey FAILED.");
    console.error(e.message);
    process.exitCode = 1;
  } finally {
    await browser.close();
  }
})();
