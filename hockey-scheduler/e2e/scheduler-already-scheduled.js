// Scheduler preview surfaces already-scheduled pairings, not just games and
// conflicts (#206 slice 1 / #326, #328 review).
//
// renderScheduler() previously read only draft_games/created and unscheduled;
// a pairing the backend now reports in already_scheduled[] (#206 slice 1 —
// a real Game already exists for it, so it is neither proposed nor a
// conflict) was silently invisible, and an all-already-scheduled Division
// rendered the misleading generic "No games generated." At desktop and
// 390px, this journey proves two acceptance states on the real UI:
//   * MIXED   — a 4-team Division with 2 of its 6 round-robin pairings
//     already real Games: the preview shows 4 proposed games, 0 conflicts,
//     and the 2 already-scheduled pairings named with their existing Game
//     reference; commit stays ENABLED (there are 4 real games to commit).
//   * ALL-DONE — a 2-team Division whose one possible pairing already has a
//     real Game: the preview shows 0 games, 0 conflicts, 1 already
//     scheduled, an explanatory "already scheduled" message instead of the
//     generic empty state, and commit stays DISABLED.
// It also proves the operator-facing reaction to two stubbed commit-time
// refusals: a concurrent-commit race (pairing_already_scheduled) and a
// stale preview invalidated by a Game created/cancelled after Generate
// (preview_stale, #328 review round 5) -- both show an actionable message,
// clear the stale preview, and require a fresh Generate before retrying.
// Both stubs also capture and assert the real request body's
// draft_fingerprint against the preceding Generate response (#328 review
// round 8 finding 3), and the stale-preview one is triggered via keyboard
// Enter and asserts focus lands on the newly rendered Generate control
// rather than silently dropping to the document body (#328 review round 8
// finding 4).
// Finally, an UNSTUBBED scenario proves the real backend, not just a canned
// response: a brand-new team registers in a division after Generate but
// before Commit, and the real commit_draft_schedule refusal (preview_stale)
// is surfaced the same way, with zero Games created (#328 review round 10
// finding 1).
//
// Fails on any browser console/page error.
const { chromium } = require("playwright");
const { spawn } = require("child_process");
const http = require("http");
const path = require("path");
const { installContextFixture } = require("./context-fixture.js");

const HOST = "127.0.0.1";
const BACKEND_DIR = path.resolve(__dirname, "..", "backend");
const READY_TIMEOUT_MS = 15000;
const VIEWPORTS = [
  { label: "desktop", width: 1440, height: 900, port: 8301 },
  { label: "phone", width: 390, height: 844, port: 8302 },
];

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

function deadline(promise, label, timeoutMs = 15000) {
  let timer;
  const timeout = new Promise((_, reject) => {
    timer = setTimeout(() => reject(new Error(label)), timeoutMs);
  });
  return Promise.race([promise, timeout]).finally(() => clearTimeout(timer));
}

const DRAFT_CARD = "scheduler/draft";
const REVIEW_CARD = "scheduler/review";

async function waitForCardState(page, cardId, state, label) {
  await page.waitForFunction(([id, expected]) => {
    const root = document.querySelector(`[data-operational-card="${id}"]`);
    return root && root.dataset.cardState === expected
      && typeof readCardState === "function"
      && readCardState(id).state === expected;
  }, [cardId, state], { timeout: 15000 }).catch((error) => {
    throw new Error(`${label}: card ${cardId} never reached ${state}: ${error.message}`);
  });
}

function schedulerCardSnapshot(page, cardId) {
  return page.evaluate((id) => {
    const root = document.querySelector(`[data-operational-card="${id}"]`);
    const entry = typeof readCardState === "function" ? readCardState(id) : null;
    const displayed = entry && typeof cardDisplayModel === "function"
      ? (cardDisplayModel(entry) || entry) : entry;
    const payload = displayed && displayed.payload || {};
    const preview = payload.preview || null;
    const selected = payload.selected instanceof Set
      ? payload.selected : new Set(payload.selected || []);
    const pending = root && root.querySelector("[data-sched-pending]");
    const livePreview = root && root.querySelector("#sched-preview");
    const loadingHeading = root
      && root.querySelector("[data-operational-status-heading]");
    const active = document.activeElement;
    return {
      card: id,
      domState: root && root.dataset.cardState,
      modelState: entry && entry.state,
      generation: entry && entry.identity && entry.identity.generation,
      busy: root && root.getAttribute("aria-busy"),
      readOutcome: entry && entry.readOutcome,
      reason: entry && entry.reason,
      retryOperation: entry && entry.retryOperation,
      previewCounts: livePreview ? {
        games: livePreview.dataset.games,
        conflicts: livePreview.dataset.conflicts,
        alreadyScheduled: livePreview.dataset.alreadyScheduled,
      } : null,
      modelCount: id === "scheduler/draft"
        ? ((preview && (preview.draft_games || preview.created)) || []).length
        : (payload.drafts || []).length,
      selectedCount: selected.size,
      pendingAction: pending && pending.dataset.schedPending,
      pendingText: pending && pending.textContent.trim(),
      pendingFocused: !!pending && active === pending,
      pendingTabIndex: pending && pending.getAttribute("tabindex"),
      previewRole: livePreview && livePreview.getAttribute("role"),
      previewLive: livePreview && livePreview.getAttribute("aria-live"),
      loadingHeading: loadingHeading && loadingHeading.textContent.trim(),
      loadingHeadingFocused: !!loadingHeading && active === loadingHeading,
      loadingHeadingTabIndex: loadingHeading
        && loadingHeading.getAttribute("tabindex"),
      rowTitles: root ? Array.from(root.querySelectorAll(".li-title"))
        .map((title) => title.textContent.trim()).sort() : [],
      mutationControls: root ? root.querySelectorAll([
        "[data-sched-generate]", "[data-sched-commit]",
        "[data-sched-publish]", "[data-sched-discard]",
        "[data-sched-pick]", "[data-sched-select-all]",
        "[data-sched-select-clean]", "[data-sched-select-none]",
        "#sched-filter-div", "#sched-filter-rink", "#sched-filter-issue",
        "[data-del]",
      ].join(",")).length : null,
      writeControls: root ? root.querySelectorAll([
        "[data-sched-generate]", "[data-sched-commit]",
        "[data-sched-publish]", "[data-sched-discard]",
        "[data-sched-pick]", "[data-sched-select-all]",
        "[data-sched-select-clean]", "[data-sched-select-none]",
        "[data-del]",
      ].join(",")).length : null,
      filterControls: root ? root.querySelectorAll([
        "#sched-filter-div", "#sched-filter-rink", "#sched-filter-issue",
      ].join(",")).length : null,
      text: root ? root.textContent.replace(/\s+/g, " ").trim() : "",
      active: active && {
        tag: active.tagName,
        text: active.textContent && active.textContent.trim(),
        commit: active.hasAttribute && active.hasAttribute("data-sched-commit"),
        discard: active.hasAttribute && active.hasAttribute("data-sched-discard"),
      },
    };
  }, cardId);
}

async function assertPendingCard(page, fail, cardId, action, expectedCount,
    expectedPreviewCounts) {
  await waitForCardState(page, cardId, "pending", `${action} pending`);
  const got = await schedulerCardSnapshot(page, cardId);
  if (got.busy !== "true" || got.pendingAction !== action
      || !got.pendingFocused || got.pendingTabIndex !== "-1"
      || got.mutationControls !== 0 || got.modelCount !== expectedCount) {
    fail(`${action}: pending card must be busy, count-preserving, read-only, and own focus: ${JSON.stringify(got)}`);
  }
  if (expectedPreviewCounts
      && JSON.stringify(got.previewCounts) !== JSON.stringify(expectedPreviewCounts)) {
    fail(`${action}: pending preview counts changed: expected ${JSON.stringify(expectedPreviewCounts)}, got ${JSON.stringify(got)}`);
  }
  const live = await page.evaluate(() => {
    const toast = document.getElementById("toast-root");
    return toast && {
      hidden: toast.hidden,
      role: toast.getAttribute("role"),
      live: toast.getAttribute("aria-live"),
      text: toast.textContent.replace(/\s+/g, " ").trim(),
    };
  });
  if (!live || live.hidden || live.role !== "status" || live.live !== "polite"
      || !live.text.includes(got.pendingText.replace(/…$/, ""))) {
    fail(`${action}: pending status must be announced once through the site live region: ${JSON.stringify({ got, live })}`);
  }
  return got;
}

async function confirmationSnapshot(page, action) {
  return page.evaluate((name) => {
    const yes = document.querySelector(`[data-sched-confirm-yes="${name}"]`);
    const no = document.querySelector(`[data-sched-confirm-no="${name}"]`);
    const root = yes && yes.closest("[data-operational-card]");
    const prompt = root && root.querySelector(".swf-confirm-prompt");
    const selectedForDiscard = root ? Array.from(
      root.querySelectorAll("[data-sched-discard-selected]")) : [];
    return {
      state: root && root.dataset.cardState,
      busy: root && root.getAttribute("aria-busy"),
      prompt: prompt && prompt.textContent.trim(),
      yesText: yes && yes.textContent.trim(),
      noText: no && no.textContent.trim(),
      yesFocused: document.activeElement === yes,
      yesPresent: !!yes,
      noPresent: !!no,
      selectedForDiscard: selectedForDiscard.map((marker) => {
        const row = marker.closest(".li");
        const title = row && row.querySelector(".li-title");
        return title ? title.textContent.trim() : "";
      }).sort(),
      selectedForDiscardCount: selectedForDiscard.length,
      decisionTitle: root && Array.from(root.querySelectorAll(".section-title"))
        .map((title) => title.textContent.replace(/\s+/g, " ").trim())
        .find((text) => /^Drafts selected for discard/.test(text)),
      backgroundControls: root ? root.querySelectorAll([
        "[data-sched-generate]", "[data-sched-commit]",
        "[data-sched-publish]", "[data-sched-discard]",
        "[data-sched-pick]", "[data-sched-select-all]",
        "[data-sched-select-clean]", "[data-sched-select-none]",
        "#sched-filter-div", "#sched-filter-rink", "#sched-filter-issue",
        "[data-del]",
      ].join(",")).length : null,
    };
  }, action);
}

function schedulerToastSnapshot(page) {
  return page.evaluate(() => {
    const root = document.getElementById("toast-root");
    return root && {
      hidden: root.hidden,
      role: root.getAttribute("role"),
      live: root.getAttribute("aria-live"),
      text: root.textContent.replace(/\s+/g, " ").trim(),
    };
  });
}

async function assertGeneratedPreviewOwnsLiveStatus(page, fail) {
  const snapshot = await page.evaluate(() => {
    const candidates = Array.from(document.querySelectorAll(
      '[aria-live]:not([aria-live="off"]),[role="status"],[role="alert"]'));
    const visible = candidates.filter((el, index) => {
      if (candidates.indexOf(el) !== index || el.hidden
          || el.getAttribute("aria-hidden") === "true") return false;
      const style = window.getComputedStyle(el);
      return style.display !== "none" && style.visibility !== "hidden"
        && el.getClientRects().length > 0;
    }).map((el) => ({
      id: el.id || null,
      role: el.getAttribute("role"),
      live: el.getAttribute("aria-live"),
      text: el.textContent.replace(/\s+/g, " ").trim(),
    }));
    const toast = document.getElementById("toast-root");
    return {
      visible,
      toast: toast && {
        hidden: toast.hidden,
        text: toast.textContent.replace(/\s+/g, " ").trim(),
      },
    };
  });
  if (snapshot.visible.length !== 1
      || snapshot.visible[0].id !== "sched-preview"
      || snapshot.visible[0].role !== "status"
      || snapshot.visible[0].live !== "polite"
      || !snapshot.toast || !snapshot.toast.hidden
      || /generated|success/i.test(snapshot.toast.text)) {
    fail(`Generate READY must have exactly one visible live region (#sched-preview) and no success toast: ${JSON.stringify(snapshot)}`);
  }
}

async function exerciseReviewLocalControlAxis(page, fail, expectedDraftCount) {
  const deriveAxis = () => page.evaluate(() => {
    const root = document.querySelector(
      '[data-operational-card="scheduler/review"]');
    if (!root) return [];
    const accessibleName = (el) => {
      const aria = el.getAttribute("aria-label");
      if (aria && aria.trim()) return aria.trim();
      const labels = el.labels ? Array.from(el.labels) : [];
      const labelled = labels.map((label) => label.textContent
        .replace(/\s+/g, " ").trim()).filter(Boolean).join(" ");
      return labelled || el.textContent.replace(/\s+/g, " ").trim();
    };
    return Array.from(root.querySelectorAll("input,select,button"))
      .map((el) => {
        if (el.matches('input[type="checkbox"][data-sched-pick]')) {
          return { kind: "checkbox", key: el.dataset.schedPick,
            name: accessibleName(el), checked: el.checked };
        }
        if (el.matches('select[id^="sched-filter-"]')) {
          return { kind: "filter", key: el.id, name: accessibleName(el),
            value: el.value,
            alternatives: Array.from(el.options).map((option) => option.value)
              .filter((value) => value !== el.value) };
        }
        const selectionAttr = Array.from(el.attributes)
          .find((attr) => attr.name.startsWith("data-sched-select-"));
        if (el.tagName === "BUTTON" && selectionAttr) {
          return { kind: "selection", key: selectionAttr.name,
            name: accessibleName(el) };
        }
        return null;
      }).filter(Boolean);
  });
  const axis = await deriveAxis();
  const checkboxes = axis.filter((row) => row.kind === "checkbox");
  const filters = axis.filter((row) => row.kind === "filter");
  const selections = axis.filter((row) => row.kind === "selection");
  const keys = axis.map((row) => `${row.kind}:${row.key}`);
  if (axis.length !== new Set(keys).size
      || checkboxes.length !== expectedDraftCount || filters.length !== 3
      || !selections.length || axis.some((row) => !row.name)
      || checkboxes.some((row) => !/^Select draft .+ vs .+ — .+ — .+/.test(row.name))
      || filters.some((row) => !/^Filter draft games by /.test(row.name))
      || filters.some((row) => !row.alternatives.length)) {
    fail(`Review local-control axis must be complete, uniquely named, and operable: ${JSON.stringify(axis)}`);
  }

  const focusMatches = async (control, label) => {
    const got = await page.evaluate((expected) => {
      const active = document.activeElement;
      const selectionAttr = active && Array.from(active.attributes || [])
        .find((attr) => attr.name.startsWith("data-sched-select-"));
      return {
        tag: active && active.tagName,
        kind: active && active.matches('input[type="checkbox"][data-sched-pick]')
          ? "checkbox" : (active && active.matches('select[id^="sched-filter-"]')
            ? "filter" : (selectionAttr ? "selection" : null)),
        key: active && active.dataset && active.dataset.schedPick
          ? active.dataset.schedPick
          : (active && active.id ? active.id : (selectionAttr && selectionAttr.name)),
      };
    }, control);
    if (got.tag === "BODY" || got.kind !== control.kind || got.key !== control.key) {
      fail(`${label}: repaint must restore exact semantic focus, never BODY: ${JSON.stringify({ control, got })}`);
    }
  };
  const locatorFor = (control) => {
    if (control.kind === "checkbox") {
      return page.locator(`[data-sched-pick="${control.key}"]`);
    }
    if (control.kind === "filter") return page.locator(`#${control.key}`);
    return page.locator(`[${control.key}]`);
  };

  for (const control of checkboxes) {
    await locatorFor(control).focus();
    await page.keyboard.press("Space");
    await focusMatches(control, `Review checkbox ${control.key} Space`);
    await page.keyboard.press("Space");
    await focusMatches(control, `Review checkbox ${control.key} restore`);
  }
  for (const control of selections) {
    await locatorFor(control).focus();
    await page.keyboard.press("Enter");
    await focusMatches(control, `Review selection ${control.key} Enter`);
  }
  for (const control of filters) {
    const alternative = control.alternatives[0];
    await locatorFor(control).focus();
    await page.evaluate(({ id, value }) => {
      const select = document.getElementById(id);
      if (!select) throw new Error(`missing #${id}`);
      select.value = value;
      select.dispatchEvent(new Event("change", { bubbles: true }));
    }, { id: control.key, value: alternative });
    await focusMatches(control, `Review filter ${control.key} change`);
    await page.evaluate(({ id, value }) => {
      const select = document.getElementById(id);
      if (!select) throw new Error(`missing #${id}`);
      select.value = value;
      select.dispatchEvent(new Event("change", { bubbles: true }));
    }, { id: control.key, value: control.value });
    await focusMatches(control, `Review filter ${control.key} restore`);
  }

  const after = await deriveAxis();
  const afterKeys = after.map((row) => `${row.kind}:${row.key}`);
  if (JSON.stringify(afterKeys) !== JSON.stringify(keys)) {
    fail(`Review local-control repaint must preserve its derived axis: ${JSON.stringify({ keys, afterKeys })}`);
  }
}

// Hold browser delivery only AFTER a real write has completed on the server.
// `stubBody` is used solely for Publish below: it gives that client state
// machine the same deterministic pending window without changing server truth,
// so the four committed drafts can still be discarded and their slots reused
// by the pre-existing real stale-preview scenario.
async function holdNextSchedulerWrite(page, pattern, label, stubBody = null) {
  let requestCount = 0;
  let released = false;
  let releaseGate;
  let markCaptured;
  let markDelivered;
  const gate = new Promise((resolve) => { releaseGate = resolve; });
  const captured = new Promise((resolve) => { markCaptured = resolve; });
  const delivered = new Promise((resolve) => { markDelivered = resolve; });
  const handler = async (route) => {
    requestCount += 1;
    if (requestCount > 1) {
      return route.fulfill({
        status: 409,
        contentType: "application/json",
        body: JSON.stringify({ error: {
          code: "duplicate_write_probe",
          message: `${label} was submitted more than once`,
        } }),
      });
    }
    let response = null;
    if (stubBody === null) response = await route.fetch();
    const request = route.request();
    markCaptured({
      method: request.method(),
      body: (() => { try { return request.postDataJSON(); } catch (_) { return null; } })(),
      status: response ? response.status() : 200,
    });
    await gate;
    try {
      if (response) await route.fulfill({ response });
      else await route.fulfill({
        status: 200, contentType: "application/json",
        body: JSON.stringify(stubBody),
      });
    } finally {
      markDelivered();
    }
  };
  await page.route(pattern, handler);
  return {
    requestCount: () => requestCount,
    waitCaptured: () => deadline(captured,
      `${label}: write was never captured after the server answered`, 30000),
    release() {
      if (!released) {
        released = true;
        releaseGate();
      }
    },
    async finish() {
      this.release();
      await deadline(delivered, `${label}: held response was not delivered`, 15000);
      await page.unroute(pattern, handler);
    },
  };
}

// Forward the write to the real server and wait until that response proves the
// transaction finished, but replace the browser-visible response with invalid
// non-JSON. This is an unknown delivery outcome, not a canned successful
// write: fresh reads must establish what actually committed, and the write may
// never be replayed merely because its response was unreadable.
async function holdCompletedSchedulerWriteWithUnreadableResponse(
    page, pattern, label) {
  let requestCount = 0;
  let released = false;
  let releaseGate;
  let markCaptured;
  let markDelivered;
  const gate = new Promise((resolve) => { releaseGate = resolve; });
  const captured = new Promise((resolve) => { markCaptured = resolve; });
  const delivered = new Promise((resolve) => { markDelivered = resolve; });
  const handler = async (route) => {
    requestCount += 1;
    if (requestCount > 1) {
      return route.fulfill({ status: 409, contentType: "application/json",
        body: JSON.stringify({ error: { code: "duplicate_write_probe",
          message: `${label} was replayed` } }) });
    }
    const response = await route.fetch();
    const request = route.request();
    markCaptured({
      method: request.method(), status: response.status(),
      body: (() => { try { return request.postDataJSON(); } catch (_) { return null; } })(),
    });
    await gate;
    try {
      await route.fulfill({ status: 200, contentType: "text/plain",
        body: "upstream response body was lost after commit" });
    } finally {
      markDelivered();
    }
  };
  await page.route(pattern, handler);
  return {
    requestCount: () => requestCount,
    waitCaptured: () => deadline(captured,
      `${label}: real server write did not complete`, 30000),
    release() {
      if (!released) { released = true; releaseGate(); }
    },
    async finish() {
      this.release();
      await deadline(delivered,
        `${label}: unreadable response was not delivered`, 15000);
      await page.unroute(pattern, handler);
    },
  };
}

async function clickDetachedTwice(handle) {
  await handle.evaluate((button) => { button.click(); button.click(); });
}

async function generateFor(page, divId, waitSelector) {
  await page.selectOption("#sched-div", divId);
  await page.click("[data-sched-generate]");
  await page.waitForSelector(waitSelector, { timeout: 15000 });
}

function previewState(page) {
  return page.evaluate(() => {
    const pv = document.querySelector("#sched-preview");
    if (!pv) return null;
    const commit = document.querySelector("[data-sched-commit]");
    // #328 review: structured per-row text (not just aggregate counts/text),
    // so callers can assert the EXACT pairing name + Game id a specific row
    // shows, not merely that the right number of rows exist.
    const rows = Array.from(pv.querySelectorAll(".card .li")).map((li) => ({
      title: ((li.querySelector(".li-title") || {}).textContent || "").trim(),
      sub: ((li.querySelector(".li-sub") || {}).textContent || "").trim(),
    }));
    return {
      games: pv.getAttribute("data-games"),
      conflicts: pv.getAttribute("data-conflicts"),
      alreadyScheduled: pv.getAttribute("data-already-scheduled"),
      commitPresent: !!commit,
      commitDisabled: commit ? commit.disabled : null,
      rows,
      text: pv.textContent.replace(/\s+/g, " ").trim(),
    };
  });
}

// Exact-match row lookup (#328 review): a pairing's rendered title must be
// EXACTLY "Home vs Away" and its sub-line must contain the given substring
// (e.g. naming its specific existing Game id) -- proves the right row, not
// merely that some row somewhere mentions "already scheduled".
function hasRow(rows, title, subIncludes) {
  return rows.some((r) => r.title === title && r.sub.includes(subIncludes));
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
  let intentionalDraftsFailure = false;
  page.on("pageerror", (e) => errors.push(`[pageerror] ${e.message}`));
  page.on("console", (m) => {
    // The stubbed 409 in scenario (3) below deliberately triggers the
    // browser's own benign resource-load log for that response; it is not
    // an application error, so it must not fail the strict zero-error bar
    // the other two scenarios still enforce (mirrors api-error-resilience.js's
    // pageerror-only approach for its own intentional error responses).
    const expectedDrafts500 = intentionalDraftsFailure
      && /Failed to load resource.*500/.test(m.text());
    if (m.type() === "error" && !/Failed to load resource.*409/.test(m.text())
        && !expectedDrafts500) {
      errors.push(`[console] ${m.text()}`);
    }
  });

  const fail = (msg) => { throw new Error(`[${viewport.label}] ${msg}`); };

  try {
    await waitForServer(`${base}/api/health`, READY_TIMEOUT_MS);
    await page.goto(base, { waitUntil: "domcontentloaded" });
    await page.waitForSelector("#content > *", { timeout: 10000 });
    await installContextFixture(page);
    // This journey never navigates the calendar, so its day only has to be
    // strictly in the FUTURE — step (5) deletes the leftover open slots, and
    // `delete_ice_slot` refuses anything at or before its clock ("past slots
    // are history"). The literal 2026-09-12 met that only because the date had
    // not arrived yet; a week out from the app's own current day always does,
    // at every hour, and comes from the app's clock rather than a second one
    // (#387/#389).
    const ICE_DAY = await page.evaluate(() => addDays(calendarDate, 7));

    // Build one League with two Divisions: a 4-team "Mixed" (six round-robin
    // pairings) and a 2-team "AllDone" (exactly one pairing).
    const ids = await page.evaluate(async (day) => {
      const F = window.hsFixture;
      // #409 EXPLICIT SELECTION on the V1 SURFACE. `POST /api/setup/league`
      // mints the PROGRAM (v1 calls it "league") and `POST /api/setup/season`
      // is a PROGRAM-AXIS create comparing the body's `league_id`
      // (server.py:3686), behind the same `setup_create_context_error`
      // preflight v2 uses (server.py:1160). Minting is not selecting.
      const league = await F.create("v1 league (the Program)", "/api/setup/league", { name: "Already-Scheduled Program" });
      await F.selectProgram("Program-only bootstrap", league.id);
      const season = await F.create("season", "/api/setup/season", { league_id: league.id, name: "Fall 2026" });
      // The v1 "level" IS the v2 League; it, the Divisions, the registrations
      // and the venue-access grant are SEASON-OWNED and land in THIS Season.
      await F.selectProgramSeason("Program+Season", league.id, season.id);
      const level = await F.create("level (the v2 League)", "/api/setup/level", { season_id: season.id, name: "Silver League" });
      const dMixed = await F.create("dMixed", "/api/setup/division", { season_id: season.id, level_id: level.id, name: "SilverMixed" });
      const dAllDone = await F.create("dAllDone", "/api/setup/division", { season_id: season.id, level_id: level.id, name: "SilverAllDone" });
      const arena = await F.create("scheduler Arena Manager", "/api/accounts", {
        username: `scheduler_arena_${Date.now()}`, password: "demo",
        role: "arena_manager", scope: {},
      });
      const club = await F.create("club", "/api/setup/club", { name: "Club" });
      const team = async (n) =>
        (await F.create("team", "/api/v2/setup/team", { club_id: club.id, league_id: level.id, name: n })).id;
      const m0 = await team("Mixed 0"), m1 = await team("Mixed 1");
      const m2 = await team("Mixed 2"), m3 = await team("Mixed 3");
      const a0 = await team("AllDone 0"), a1 = await team("AllDone 1");
      const register = (teamId, divisionId) => post(
        `/api/setup/seasons/${season.id}/team-registrations`,
        { team_id: teamId, division_id: divisionId });
      await register(m0, dMixed.id); await register(m1, dMixed.id);
      await register(m2, dMixed.id); await register(m3, dMixed.id);
      await register(a0, dAllDone.id); await register(a1, dAllDone.id);

      const venue = await F.create("venue", "/api/setup/venue", { name: "Arena", league_id: league.id });
      await F.call("season venue-access grant", `/api/v2/setup/seasons/${season.id}/venue-access`, { venue_id: venue.id });
      const rink = await F.create("rink", "/api/setup/rink", { venue_id: venue.id, name: "Rink 1" });
      const pad = (n) => String(n).padStart(2, "0");
      const slot = async (h) => (await F.create("ice-slot", "/api/setup/ice-slot", {
        rink_id: rink.id, start_time: `${day}T${pad(h)}:00:00+00:00`,
        end_time: `${day}T${pad(h + 1)}:00:00+00:00`, slot_type: "game",
      })).id;

      // Ask the scheduler which pairings the Mixed round robin actually
      // produces (no ice yet, so all six land in unscheduled) instead of
      // re-implementing the circle method here; pre-seed real Games for
      // the first two so the NEXT preview is genuinely mixed. Capture each
      // seeded pairing's names + Game id (#328 review) so the caller can
      // assert the EXACT already-scheduled rows the preview renders, not
      // just their count.
      // A draft PREVIEW returns no id — it is a computed response, not a
      // created record — so it is asserted with `F.call` (success, no id)
      // rather than `F.create` (#409).
      const bare = await F.call("scheduler draft preview", "/api/scheduler/draft",
        { division_id: dMixed.id });
      const toSeed = bare.unscheduled.slice(0, 2);
      const seededMixed = [];
      for (const pairing of toSeed) {
        const seedSlot = await slot(6 + toSeed.indexOf(pairing));
        const g = await F.create("g", "/api/setup/game", {
          season_id: season.id, division_id: dMixed.id,
          home_team_id: pairing.home_team_id, away_team_id: pairing.away_team_id,
          ice_slot_id: seedSlot,
        });
        seededMixed.push({
          home_team_name: pairing.home_team_name,
          away_team_name: pairing.away_team_name,
          game_id: g.id,
        });
      }
      // Ice for the four still-missing Mixed pairings -- these stay
      // AVAILABLE for the rest of the run (scenarios (3)/(4) only ever
      // stub the commit response, so nothing here ever really commits).
      const mixedOpenSlotIds = [];
      for (let h = 8; h < 8 + 4; h++) mixedOpenSlotIds.push(await slot(h));

      // AllDone's one pairing already has a real Game — no ice slot is
      // even needed for it to be picked up as already-scheduled.
      const allDoneSlot = await slot(20);
      const allDoneGame = await F.create("allDoneGame", "/api/setup/game", {
        season_id: season.id, division_id: dAllDone.id,
        home_team_id: a0, away_team_id: a1, ice_slot_id: allDoneSlot,
      });

      return {
        dMixed: dMixed.id, dAllDone: dAllDone.id, seededMixed,
        allDone: { home_team_name: "AllDone 0", away_team_name: "AllDone 1",
                  game_id: allDoneGame.id },
        seasonId: season.id, levelId: level.id, leagueId: league.id,
        mixedOpenSlotIds, arenaUsername: arena.username,
      };
    }, ICE_DAY);

    await page.waitForSelector('.tab[data-tab="scheduler"]', { state: "visible", timeout: 10000 });

    // A transport failure is not an empty draft list. Force the FIRST review
    // read to fail, before this principal has any last-good review payload to
    // retain. The card must say ERROR/FAILED, expose Retry, and must not paint
    // any of the three true-empty copies. Retrying the exact card through the
    // keyboard then walks a fail-closed malformed-success matrix before the
    // one declared empty shape is accepted.
    // This is deliberately the first Scheduler navigation in the journey;
    // testing after a successful empty read would exercise retention instead
    // and could not distinguish "read failed" from "read succeeded empty".
    const draftsReadPattern = /\/api\/scheduler\/drafts(?:\?.*)?$/;
    let draftsReadFailures = 0;
    const draftsReadFailure = async (route) => {
      if (route.request().method() !== "GET" || draftsReadFailures) {
        return route.fallback();
      }
      draftsReadFailures += 1;
      return route.fulfill({
        status: 500,
        contentType: "application/json",
        body: JSON.stringify({ error: {
          code: "forced_drafts_read_failure",
          message: "The draft list failed for this browser regression.",
        } }),
      });
    };
    intentionalDraftsFailure = true;
    await page.route(draftsReadPattern, draftsReadFailure);
    await page.click('.tab[data-tab="scheduler"]');
    await page.waitForSelector("#sched-div", { timeout: 10000 });
    await waitForCardState(page, REVIEW_CARD, "error", "initial drafts 500");
    const failedReview = await schedulerCardSnapshot(page, REVIEW_CARD);
    const retrySelector = `[data-card-retry="${REVIEW_CARD}"]`;
    if (draftsReadFailures !== 1 || failedReview.readOutcome !== "failed"
        || failedReview.busy !== "false"
        || !await page.locator(retrySelector).isVisible()
        || /Draft games \(0\)|No draft games in this selection|No draft games match these filters/i
          .test(failedReview.text)) {
      fail(`drafts 500: failure must remain explicit and must not masquerade as empty: ${JSON.stringify({ draftsReadFailures, failedReview })}`);
    }
    await page.unroute(draftsReadPattern, draftsReadFailure);
    intentionalDraftsFailure = false;

    // HTTP 200 alone is not authority to claim there are no drafts. These are
    // all successful payloads that readApiResponse can legitimately hand to
    // the card — including JSON null — but none proves an array-shaped draft
    // inventory. A 204 is deliberately NOT in this matrix: for a
    // context-scoped read it is the server's stale-epoch discard protocol,
    // intercepted before body parsing and followed by a fresh context read;
    // it is not a malformed success response owned by Review. Each retry must
    // advance the card generation and settle ERROR/FAILED without any
    // empty-state copy.
    // The final `{draft_games: []}` response is the positive control: if that
    // also failed, this would merely prove that every response is rejected.
    const draftsShapes = [
      { label: "missing draft_games", status: 200, body: {} },
      { label: "null draft_games", status: 200, body: { draft_games: null } },
      { label: "object draft_games", status: 200, body: { draft_games: {} } },
      { label: "JSON null", status: 200, body: null },
      { label: "declared empty array", status: 200, body: { draft_games: [] }, valid: true },
    ];
    for (const shape of draftsShapes) {
      let shapeRequests = 0;
      let captureShapeRequest;
      const shapeRequestCaptured = new Promise((resolve) => {
        captureShapeRequest = resolve;
      });
      const draftsShapeHandler = async (route) => {
        if (route.request().method() !== "GET" || shapeRequests) {
          return route.fallback();
        }
        shapeRequests += 1;
        captureShapeRequest();
        return route.fulfill({
          status: shape.status,
          contentType: "application/json",
          body: JSON.stringify(shape.body),
        });
      };
      await page.route(draftsReadPattern, draftsShapeHandler);
      const beforeShape = await schedulerCardSnapshot(page, REVIEW_CARD);
      await page.focus(retrySelector);
      await page.keyboard.press("Enter");
      await deadline(shapeRequestCaptured,
        `drafts ${shape.label}: Retry did not issue its drafts GET`, 15000);
      try {
        await page.waitForFunction(([priorGeneration, expectedState]) => {
          const entry = readCardState("scheduler/review");
          return entry && entry.identity
            && entry.identity.generation > priorGeneration
            && entry.state === expectedState;
        }, [beforeShape.generation, shape.valid ? "empty" : "error"],
        { timeout: 15000 });
      } catch (error) {
        const afterTimeout = await schedulerCardSnapshot(page, REVIEW_CARD);
        throw new Error(`drafts ${shape.label}: expected ${shape.valid ? "EMPTY" : "ERROR"} after generation ${beforeShape.generation}; got ${JSON.stringify(afterTimeout)} (${error.message})`);
      } finally {
        await page.unroute(draftsReadPattern, draftsShapeHandler);
      }
      const shaped = await schedulerCardSnapshot(page, REVIEW_CARD);
      if (shapeRequests !== 1) {
        fail(`drafts ${shape.label}: expected exactly one intercepted Retry read, got ${shapeRequests}`);
      }
      if (shape.valid) {
        if (shaped.readOutcome !== "ok" || shaped.reason !== "no_drafts") {
          fail(`drafts ${shape.label}: exact empty-array control was not accepted: ${JSON.stringify(shaped)}`);
        }
      } else if (shaped.readOutcome !== "failed" || shaped.busy !== "false"
          || /Draft games \(0\)|No draft games in this selection|No draft games match these filters/i
            .test(shaped.text)
          || !await page.locator(retrySelector).isVisible()) {
        fail(`drafts ${shape.label}: malformed success must fail closed, never become empty: ${JSON.stringify(shaped)}`);
      }
    }

    const emptyReview = await schedulerCardSnapshot(page, REVIEW_CARD);
    if (emptyReview.readOutcome !== "ok" || emptyReview.reason !== "no_drafts"
        || !/No draft games in this selection/i.test(emptyReview.text)
        || await page.locator(
          `[data-operational-card="${REVIEW_CARD}"] .banner.alert`).count()) {
      fail(`drafts empty: successful retry must render the asserted true-empty state: ${JSON.stringify(emptyReview)}`);
    }

    // The malformed-write sweep is closed over production's declaration,
    // rather than over a four-item route list in this journey. Each scenario
    // below records the action it actually drove; a fifth declaration, a
    // removed action, a duplicate route or a moved owner therefore makes the
    // final coverage equality fail instead of silently shrinking the sweep.
    const schedulerWriteAxis = await page.evaluate(() => Object.entries(
      SCHEDULER_WRITE_ACTIONS).map(([action, declared]) => ({
      action, path: declared.path, card: declared.card,
    })));
    const schedulerWriteByAction = new Map(
      schedulerWriteAxis.map((declared) => [declared.action, declared]));
    if (schedulerWriteByAction.size !== schedulerWriteAxis.length
        || new Set(schedulerWriteAxis.map((declared) => declared.path)).size
          !== schedulerWriteAxis.length) {
      fail(`Scheduler write declaration must have unique actions and routes: ${JSON.stringify(schedulerWriteAxis)}`);
    }
    const malformedWriteActions = new Set();
    const schedulerWritePath = (action, card) => {
      const declared = schedulerWriteByAction.get(action);
      if (!declared || declared.card !== card || !declared.path) {
        fail(`Missing ${action} declaration for ${card}: ${JSON.stringify(schedulerWriteAxis)}`);
      }
      return declared.path;
    };

    // Both controls were missing an accessible name/live contract before
    // this slice. Assert them through the actual rendered DOM at both desktop
    // and 390px, not merely by searching the source string.
    const divisionPicker = page.getByRole("combobox", { name: "Division to schedule" });
    if (await divisionPicker.count() !== 1 || !await divisionPicker.isVisible()) {
      fail("scheduler Division picker must have one visible accessible label");
    }
    await page.waitForFunction(
      (id) => !!document.querySelector(`#sched-div option[value="${id}"]`),
      ids.dMixed, { timeout: 10000 });

    // A 2xx status is not sufficient evidence that Generate produced a
    // usable preview. Deliver a deliberately shape-less success through the
    // route declared by SCHEDULER_WRITE_ACTIONS. It must pass through PENDING,
    // then fail explicitly without accepting a preview or exposing Commit.
    const malformedGenerateHold = await holdNextSchedulerWrite(
      page, `**${schedulerWritePath("generate", DRAFT_CARD)}`,
      "Generate malformed 2xx", {});
    await page.click("[data-sched-generate]");
    const malformedGenerateRequest = await malformedGenerateHold.waitCaptured();
    if (malformedGenerateRequest.method !== "POST"
        || !malformedGenerateRequest.body
        || malformedGenerateRequest.body.division_id !== ids.dMixed) {
      fail(`Generate malformed 2xx did not exercise the declared action: ${JSON.stringify(malformedGenerateRequest)}`);
    }
    await assertPendingCard(page, fail, DRAFT_CARD, "generate", 0);
    await malformedGenerateHold.finish();
    await waitForCardState(page, DRAFT_CARD, "error", "Generate malformed 2xx");
    const malformedGenerate = await schedulerCardSnapshot(page, DRAFT_CARD);
    if (malformedGenerate.readOutcome !== "failed"
        || malformedGenerate.retryOperation !== "generate"
        || malformedGenerate.previewCounts !== null
        || malformedGenerate.modelCount !== 0
        || !/invalid response.*No preview was accepted/i.test(malformedGenerate.text)
        || await page.locator("[data-sched-commit]").count()) {
      fail(`Generate malformed 2xx must be explicit ERROR with no accepted preview: ${JSON.stringify(malformedGenerate)}`);
    }
    malformedWriteActions.add("generate");

    // (1) Mixed: 4 missing pairings proposed, 2 already scheduled, 0 conflicts.
    await generateFor(page, ids.dMixed,
      '#sched-preview[data-games="4"][data-already-scheduled="2"]');
    let s = await previewState(page);
    if (s.conflicts !== "0") {
      fail(`mixed: expected 0 conflicts, got ${JSON.stringify(s)}`);
    }
    if (!/4 game\(s\), 0 conflict\(s\), 2 already scheduled/.test(s.text)) {
      fail(`mixed: header should name the already-scheduled count: ${s.text}`);
    }
    // #328 review: assert the EXACT two seeded pairings, each naming its OWN
    // existing Game id -- not just that some row somewhere says "already
    // scheduled" and the counts add up (which a duplicated or misattributed
    // row could also satisfy).
    for (const seeded of ids.seededMixed) {
      const title = `${seeded.home_team_name} vs ${seeded.away_team_name}`;
      if (!hasRow(s.rows, title, `Already scheduled — Game ${seeded.game_id}`)) {
        fail(`mixed: missing exact already-scheduled row "${title}" naming Game ${seeded.game_id}: ${JSON.stringify(s.rows)}`);
      }
    }
    const alreadyRows = s.rows.filter((r) => r.sub.includes("Already scheduled"));
    if (alreadyRows.length !== 2) {
      fail(`mixed: expected exactly 2 already-scheduled rows, got ${JSON.stringify(alreadyRows)}`);
    }
    // The two seeded pairings must never ALSO appear as proposed games.
    const gameRows = s.rows.filter((r) => !r.sub.includes("Already scheduled"));
    for (const seeded of ids.seededMixed) {
      const title = `${seeded.home_team_name} vs ${seeded.away_team_name}`;
      if (gameRows.some((r) => r.title === title)) {
        fail(`mixed: seeded pairing "${title}" must not also appear as a proposed game: ${JSON.stringify(gameRows)}`);
      }
    }
    if (s.commitPresent !== true || s.commitDisabled !== false) {
      fail(`mixed: commit must stay enabled with 4 real missing games: ${JSON.stringify(s)}`);
    }

    // (2) All-done: nothing missing, nothing to commit, but NOT the
    // misleading generic "No games generated." — an explanatory message
    // naming that the round robin is already fully scheduled.
    await generateFor(page, ids.dAllDone,
      '#sched-preview[data-games="0"][data-already-scheduled="1"]');
    s = await previewState(page);
    if (s.conflicts !== "0") {
      fail(`all-done: expected 0 conflicts, got ${JSON.stringify(s)}`);
    }
    if (/No games generated\./.test(s.text)) {
      fail(`all-done: must not show the generic "No games generated." message: ${s.text}`);
    }
    if (!/already scheduled/i.test(s.text) || !/nothing missing/i.test(s.text)) {
      fail(`all-done: missing explanatory "already scheduled" message: ${s.text}`);
    }
    // #328 review: assert the exact row, not just the aggregate count/text.
    const allDoneTitle = `${ids.allDone.home_team_name} vs ${ids.allDone.away_team_name}`;
    if (!hasRow(s.rows, allDoneTitle, `Already scheduled — Game ${ids.allDone.game_id}`)) {
      fail(`all-done: missing exact already-scheduled row "${allDoneTitle}" naming Game ${ids.allDone.game_id}: ${JSON.stringify(s.rows)}`);
    }
    if (s.commitDisabled !== true) {
      fail(`all-done: commit must stay disabled with nothing missing: ${JSON.stringify(s)}`);
    }

    // (3) Commit-time race, stubbed (#328 review round 3): genuinely
    // reproducing the concurrent-commit race in a single browser page
    // isn't practical, so /api/scheduler/commit is stubbed with the exact
    // shape a real pairing_already_scheduled refusal returns
    // (DomainError.to_dict(), HTTP 409) -- proving the UI reaction, not
    // the backend race itself (that is the forced two-session PostgreSQL
    // test in test_placement_concurrency.py). The message must be
    // actionable on its own: post()'s generic toast surfaces
    // error.message alone, never error.details.
    //
    // #328 review round 8 finding 3: the stub must not just answer any
    // request that arrives -- it must prove app.js actually SENT the
    // previewed draft_fingerprint. Without capturing and asserting the real
    // request body, removing or renaming that field in app.js would leave
    // this journey green while production returns preview_required.
    await generateFor(page, ids.dMixed,
      '#sched-preview[data-games="4"][data-already-scheduled="2"]');
    s = await previewState(page);
    if (s.commitDisabled !== false) {
      fail(`race stub: needs an enabled Commit to click: ${JSON.stringify(s)}`);
    }
    let expectedFingerprint = await page.evaluate(() => {
      const payload = cardDisplayPayload(readCardState(SCHEDULER_DRAFT_CARD));
      return payload && payload.preview && payload.preview.draft_fingerprint;
    });
    if (!expectedFingerprint) {
      fail(`race stub: no draft_fingerprint on the Generate response to compare against: ${expectedFingerprint}`);
    }
    let sentFingerprint;
    await page.route("**/api/scheduler/commit", (r) => {
      const body = r.request().postDataJSON();
      sentFingerprint = body && body.draft_fingerprint;
      return r.fulfill({
        status: 409,
        contentType: "application/json",
        body: JSON.stringify({
          error: {
            code: "concurrency_conflict",
            message: "Mixed 0 vs Mixed 1 is already scheduled as Game "
              + "game_stub_race — generate a fresh preview before "
              + "committing again.",
            details: {
              reason: "pairing_already_scheduled",
              home_team_id: "stub_home", away_team_id: "stub_away",
              existing_game_id: "game_stub_race",
            },
          },
        }),
      });
    });
    await page.click("[data-sched-commit]");
    await waitForCardState(page, DRAFT_CARD, "confirm", "race stub confirmation");
    let confirm = await confirmationSnapshot(page, "commit");
    if (sentFingerprint !== undefined || confirm.busy !== "false"
        || confirm.backgroundControls !== 0
        || !confirm.yesFocused || confirm.prompt !== "Commit 4 proposed games as draft?") {
      fail(`race stub: opening confirmation must not POST and must focus the exact decision: ${JSON.stringify({ confirm, sentFingerprint })}`);
    }
    await page.click('[data-sched-confirm-yes="commit"]');
    await page.waitForFunction(
      () => /Mixed 0 vs Mixed 1 is already scheduled as Game game_stub_race/
        .test((document.querySelector(
          '[data-operational-card="scheduler/draft"] .banner.alert') || {}).textContent || ""),
      null, { timeout: 10000 });
    // render() wipes #content to a loading skeleton SYNCHRONOUSLY, then does
    // several awaited fetches before the real content replaces it -- so
    // "#sched-preview detached" alone fires the moment the skeleton
    // appears, not once the real re-render (with a fresh, uncommittable
    // Generate-only state) has actually landed. Wait for that settled state
    // directly instead.
    await page.waitForFunction(
      () => !document.querySelector("[data-sched-commit]")
        && !!document.querySelector("[data-sched-generate]"),
      null, { timeout: 10000 });
    if (sentFingerprint !== expectedFingerprint) {
      fail(`race stub: Commit must POST the exact previewed draft_fingerprint `
        + `(${JSON.stringify(expectedFingerprint)}), got ${JSON.stringify(sentFingerprint)}`);
    }
    await page.unroute("**/api/scheduler/commit");

    // (4) Stale-preview TOCTOU gate, stubbed (#328 review round 5): a Game
    // created or cancelled in the window between Generate and Commit
    // invalidates the reviewed preview's fingerprint. Genuinely reproducing
    // that gap in a single browser page isn't practical either (the
    // backend regressions in test_scheduler.py cover the real
    // create/cancel-between-preview-and-commit scenarios directly against
    // the store), so /api/scheduler/commit is stubbed with the exact shape
    // a real preview_stale refusal returns -- proving the UI reaction: the
    // message is actionable on its own (post()'s generic toast surfaces
    // error.message alone, never error.details), the stale preview is
    // cleared, and Commit cannot be retried without a fresh Generate.
    //
    // #328 review round 8 finding 3: same request-capture requirement as
    // scenario (3) above -- prove app.js actually sent the previewed
    // draft_fingerprint, not just that SOME request arrived.
    //
    // #328 review round 8 finding 4: triggered with a keyboard Enter (not a
    // pointer click) on the focused Commit button, then asserts focus lands
    // on Generate once the terminal error clears the stale preview --
    // render() replaces #content wholesale, so the focused Commit button
    // is simply removed from the DOM; nothing otherwise moves focus
    // anywhere, silently dropping a keyboard user back to the document
    // body even though the live-region toast (outside #content, so it
    // survives) announced what to do next.
    await generateFor(page, ids.dMixed,
      '#sched-preview[data-games="4"][data-already-scheduled="2"]');
    s = await previewState(page);
    if (s.commitDisabled !== false) {
      fail(`stale-preview stub: needs an enabled Commit to click: ${JSON.stringify(s)}`);
    }
    expectedFingerprint = await page.evaluate(() => {
      const payload = cardDisplayPayload(readCardState(SCHEDULER_DRAFT_CARD));
      return payload && payload.preview && payload.preview.draft_fingerprint;
    });
    if (!expectedFingerprint) {
      fail(`stale-preview stub: no draft_fingerprint on the Generate response to compare against: ${expectedFingerprint}`);
    }
    sentFingerprint = undefined;
    await page.route("**/api/scheduler/commit", (r) => {
      const body = r.request().postDataJSON();
      sentFingerprint = body && body.draft_fingerprint;
      return r.fulfill({
        status: 409,
        contentType: "application/json",
        body: JSON.stringify({
          error: {
            code: "concurrency_conflict",
            message: "This preview is out of date — a game may have been "
              + "added, cancelled, or otherwise changed since you "
              + "generated it. Generate a fresh preview and review it "
              + "before committing.",
            details: { reason: "preview_stale" },
          },
        }),
      });
    });
    await page.focus("[data-sched-commit]");
    if (!(await page.evaluate(
        () => document.activeElement.hasAttribute("data-sched-commit")))) {
      fail("stale-preview stub: Commit must be focusable to trigger it via keyboard");
    }
    await page.keyboard.press("Enter");
    await waitForCardState(page, DRAFT_CARD, "confirm",
      "stale-preview keyboard confirmation");
    confirm = await confirmationSnapshot(page, "commit");
    if (!confirm.yesFocused || confirm.busy !== "false"
        || confirm.backgroundControls !== 0
        || sentFingerprint !== undefined
        || confirm.prompt !== "Commit 4 proposed games as draft?") {
      fail(`stale-preview stub: Enter must open a focused, non-busy confirmation before POST: ${JSON.stringify({ confirm, sentFingerprint })}`);
    }
    // The affirmative control receives focus on entry, so a second Enter is
    // the complete keyboard-only path through the destructive boundary.
    await page.keyboard.press("Enter");
    await page.waitForFunction(
      () => /This preview is out of date/
        .test((document.querySelector(
          '[data-operational-card="scheduler/draft"] .banner.alert') || {}).textContent || ""),
      null, { timeout: 10000 });
    // render() wipes #content to a loading skeleton SYNCHRONOUSLY, then does
    // several awaited fetches before the real content (and the focus-restore
    // this scenario checks for) replaces it -- so "#sched-preview detached"
    // fires the moment the skeleton appears, long before the real re-render
    // completes. Waiting for the fresh Generate-only state directly (no
    // Commit button, since the preview was cleared) is true in neither the
    // pre-click nor the skeleton DOM, only once the real re-render lands.
    await page.waitForFunction(
      () => !document.querySelector("[data-sched-commit]")
        && !!document.querySelector("[data-sched-generate]"),
      null, { timeout: 10000 });
    if (sentFingerprint !== expectedFingerprint) {
      fail(`stale-preview stub: Commit must POST the exact previewed draft_fingerprint `
        + `(${JSON.stringify(expectedFingerprint)}), got ${JSON.stringify(sentFingerprint)}`);
    }
    const focusAfter = await page.evaluate(() => ({
      hasGenerateAttr: document.activeElement
        ? document.activeElement.hasAttribute("data-sched-generate") : false,
      tag: document.activeElement ? document.activeElement.tagName : null,
    }));
    if (!focusAfter.hasGenerateAttr) {
      fail(`stale-preview stub: focus must land on Generate after terminal `
        + `recovery, not silently drop to <${focusAfter.tag}>: ${JSON.stringify(focusAfter)}`);
    }
    await page.unroute("**/api/scheduler/commit");

    // (5) #393 PR D: the four Scheduler writes use an explicit PENDING state,
    // not the read-only LOADING state. Exercise every action at desktop and
    // 390px with a response whose server work has finished but whose delivery
    // is held. Counts must stay visible, aria-busy must stay true, focus must
    // land on the pending sentence, and every mutation control must disappear.
    // Retained detached controls are then fired twice to make the single-write
    // guarantee falsifiable: a visual disabled assertion alone would miss a
    // stale closure or a keyboard repeat still capable of submitting.
    //
    // Generate is preview-only, Commit creates four real drafts, Publish uses
    // a success-shaped held response without mutating the server (so all four
    // drafts remain available for the destructive leg), and Discard really
    // deletes those drafts and releases their slots. The pre-existing scenario
    // below can therefore keep using those exact slots.
    await generateFor(page, ids.dMixed,
      '#sched-preview[data-games="4"][data-already-scheduled="2"]');
    let draftBefore = await schedulerCardSnapshot(page, DRAFT_CARD);
    const expectedMixedCounts = { games: "4", conflicts: "0", alreadyScheduled: "2" };
    if (draftBefore.previewRole !== "status" || draftBefore.previewLive !== "polite"
        || JSON.stringify(draftBefore.previewCounts) !== JSON.stringify(expectedMixedCounts)) {
      fail(`scheduler preview must be a polite status region carrying exact counts: ${JSON.stringify(draftBefore)}`);
    }

    const generateHold = await holdNextSchedulerWrite(
      page, "**/api/scheduler/draft", "Generate pending");
    const oldGenerate = await page.$("[data-sched-generate]");
    await oldGenerate.click();
    const generated = await generateHold.waitCaptured();
    if (generated.status !== 200 || generated.method !== "POST") {
      fail(`Generate pending: expected a real successful POST, got ${JSON.stringify(generated)}`);
    }
    await assertPendingCard(page, fail, DRAFT_CARD, "generate", 4,
      expectedMixedCounts);
    await clickDetachedTwice(oldGenerate);
    await page.waitForTimeout(100);
    if (generateHold.requestCount() !== 1) {
      fail(`Generate pending: detached repeated activation sent ${generateHold.requestCount()} writes`);
    }
    await generateHold.finish();
    await waitForCardState(page, DRAFT_CARD, "ready", "Generate settlement");
    draftBefore = await schedulerCardSnapshot(page, DRAFT_CARD);
    if (JSON.stringify(draftBefore.previewCounts) !== JSON.stringify(expectedMixedCounts)) {
      fail(`Generate settlement changed the reviewed counts: ${JSON.stringify(draftBefore)}`);
    }
    await assertGeneratedPreviewOwnsLiveStatus(page, fail);
    expectedFingerprint = await page.evaluate(() => {
      const payload = cardDisplayPayload(readCardState(SCHEDULER_DRAFT_CARD));
      return payload && payload.preview && payload.preview.draft_fingerprint;
    });
    if (!expectedFingerprint) {
      fail("Generate settlement did not retain the fingerprint Commit must send");
    }

    // Commit is destructive enough to require an in-card decision. Prove the
    // keyboard path in both directions: Enter opens the decision and focuses
    // Yes without making a request; Tab+Enter cancels and returns focus/data
    // to the original Commit; reopening and pressing Enter on Yes makes one
    // request and enters PENDING.
    let commitRequests = 0;
    const countCommit = (request) => {
      if (request.method() === "POST"
          && /\/api\/scheduler\/commit(?:\?.*)?$/.test(request.url())) {
        commitRequests += 1;
      }
    };
    page.on("request", countCommit);
    await page.focus("[data-sched-commit]");
    await page.keyboard.press("Enter");
    await waitForCardState(page, DRAFT_CARD, "confirm", "Commit cancel confirmation");
    confirm = await confirmationSnapshot(page, "commit");
    if (commitRequests !== 0 || confirm.busy !== "false" || !confirm.yesFocused
        || confirm.backgroundControls !== 0
        || confirm.prompt !== "Commit 4 proposed games as draft?"
        || confirm.yesText !== "Commit as draft" || confirm.noText !== "Keep reviewing") {
      fail(`Commit confirmation must be exact, focused, and pre-request: ${JSON.stringify({ confirm, commitRequests })}`);
    }
    await page.keyboard.press("Tab");
    const commitNoFocused = await page.evaluate(() => document.activeElement
      && document.activeElement.dataset.schedConfirmNo === "commit");
    if (!commitNoFocused) fail("Commit cancellation must be keyboard reachable after Yes");
    await page.keyboard.press("Enter");
    await waitForCardState(page, DRAFT_CARD, "ready", "Commit cancellation");
    const commitCancelled = await schedulerCardSnapshot(page, DRAFT_CARD);
    if (commitRequests !== 0 || commitCancelled.modelCount !== 4
        || JSON.stringify(commitCancelled.previewCounts) !== JSON.stringify(expectedMixedCounts)
        || !commitCancelled.active || !commitCancelled.active.commit) {
      fail(`Commit cancel must preserve proposal/counts and restore focus without POST: ${JSON.stringify({ commitCancelled, commitRequests })}`);
    }
    page.off("request", countCommit);

    // Commit's malformed 2xx is an UNKNOWN outcome: even though this probe
    // does not forward to the server, the client cannot know that. It must
    // throw away the reviewed proposal, issue fresh reads for both cards, and
    // never offer a blind Commit retry. Count those reads on the wire so a
    // local repaint cannot satisfy the assertion.
    const malformedCommitReads = { overview: 0, drafts: 0 };
    const countMalformedCommitReads = (request) => {
      if (request.method() !== "GET") return;
      if (/\/api\/demo\/overview(?:\?.*)?$/.test(request.url())) {
        malformedCommitReads.overview += 1;
      }
      if (/\/api\/scheduler\/drafts(?:\?.*)?$/.test(request.url())) {
        malformedCommitReads.drafts += 1;
      }
    };
    page.on("request", countMalformedCommitReads);
    const malformedCommitHold = await holdNextSchedulerWrite(
      page, `**${schedulerWritePath("commit", DRAFT_CARD)}`,
      "Commit malformed 2xx", {});
    await page.focus("[data-sched-commit]");
    await page.keyboard.press("Enter");
    await waitForCardState(page, DRAFT_CARD, "confirm",
      "Commit malformed confirmation");
    await page.keyboard.press("Enter");
    const malformedCommitRequest = await malformedCommitHold.waitCaptured();
    if (malformedCommitRequest.method !== "POST"
        || !malformedCommitRequest.body
        || malformedCommitRequest.body.draft_fingerprint !== expectedFingerprint) {
      fail(`Commit malformed 2xx did not send the reviewed proposal: ${JSON.stringify(malformedCommitRequest)}`);
    }
    await assertPendingCard(page, fail, DRAFT_CARD, "commit", 4,
      expectedMixedCounts);
    await malformedCommitHold.finish();
    await page.waitForFunction(() => {
      const draft = readCardState("scheduler/draft");
      const reviewState = readCardState("scheduler/review");
      return draft && draft.state === "empty"
        && reviewState && reviewState.state === "empty";
    }, null, { timeout: 15000 });
    const malformedCommitDraft = await schedulerCardSnapshot(page, DRAFT_CARD);
    const malformedCommitReview = await schedulerCardSnapshot(page, REVIEW_CARD);
    const malformedCommitToast = await schedulerToastSnapshot(page);
    if (malformedCommitReads.overview < 1 || malformedCommitReads.drafts < 1
        || malformedCommitDraft.modelCount !== 0
        || malformedCommitDraft.previewCounts !== null
        || malformedCommitDraft.retryOperation === "commit"
        || malformedCommitReview.modelCount !== 0
        || !malformedCommitToast
        || !/commit response could not be verified.*Refreshing schedule state/i
          .test(malformedCommitToast.text)
        || await page.locator(`[data-card-retry="${DRAFT_CARD}"]`).count()
        || await page.locator("[data-sched-commit]").count()) {
      fail(`Commit malformed 2xx must reconcile both cards with no blind retry: ${JSON.stringify({ malformedCommitReads, malformedCommitDraft, malformedCommitReview, malformedCommitToast })}`);
    }
    page.off("request", countMalformedCommitReads);
    malformedWriteActions.add("commit");

    // Fresh server truth says no commit occurred, so Generate again before the
    // positive real Commit. The fingerprint is re-read from this new preview;
    // reusing the discarded one's token would defeat the reconciliation test.
    await generateFor(page, ids.dMixed,
      '#sched-preview[data-games="4"][data-already-scheduled="2"]');
    expectedFingerprint = await page.evaluate(() => {
      const payload = cardDisplayPayload(readCardState(SCHEDULER_DRAFT_CARD));
      return payload && payload.preview && payload.preview.draft_fingerprint;
    });
    if (!expectedFingerprint) {
      fail("Commit malformed reconciliation did not permit a fresh reviewed preview");
    }

    const commitHold = await holdNextSchedulerWrite(
      page, "**/api/scheduler/commit", "Commit pending");
    await page.focus("[data-sched-commit]");
    await page.keyboard.press("Enter");
    await waitForCardState(page, DRAFT_CARD, "confirm", "Commit confirmation");
    const oldCommitYes = await page.$('[data-sched-confirm-yes="commit"]');
    await page.keyboard.press("Enter");
    const committed = await commitHold.waitCaptured();
    if (committed.status !== 200 || committed.method !== "POST"
        || !committed.body || committed.body.draft_fingerprint !== expectedFingerprint) {
      fail(`Commit pending: real request did not carry the reviewed fingerprint: ${JSON.stringify(committed)}`);
    }
    await assertPendingCard(page, fail, DRAFT_CARD, "commit", 4,
      expectedMixedCounts);
    await clickDetachedTwice(oldCommitYes);
    await page.waitForTimeout(100);
    if (commitHold.requestCount() !== 1) {
      fail(`Commit pending: detached repeated affirmation sent ${commitHold.requestCount()} writes`);
    }
    await commitHold.finish();
    await waitForCardState(page, DRAFT_CARD, "empty", "Commit settlement Draft");
    await waitForCardState(page, REVIEW_CARD, "ready", "Commit settlement Review");
    let review = await schedulerCardSnapshot(page, REVIEW_CARD);
    if (review.modelCount !== 4 || !/Draft games \(4\)/.test(review.text)) {
      fail(`Commit settlement must move all four drafts into Review: ${JSON.stringify(review)}`);
    }

    // Derive the Review-local control surface from the rendered card rather
    // than repeating its members here. Every draft checkbox and every filter
    // must have an accessible name. Space (checkbox), change (native select)
    // and Enter (selection button) each repaint the card, and each replacement
    // DOM node must recover the exact semantic control that owned focus.
    await exerciseReviewLocalControlAxis(page, fail, review.modelCount);

    // Publish is intentionally not destructive and therefore does not ask for
    // confirmation. It still owns the same PENDING/single-submit contract.
    // Select all through the shipped control so the retained count and exact
    // request body can be checked while delivery is held.
    await page.click("[data-sched-select-all]");
    review = await schedulerCardSnapshot(page, REVIEW_CARD);
    if (review.selectedCount !== 4) {
      fail(`Publish setup must have exactly four selected drafts: ${JSON.stringify(review)}`);
    }

    // Publish's declared success count is the only evidence that the returned
    // 2xx describes the write. Withhold that field and prove the client does
    // not invent "Published 0" (or any other exact count), but reconciles the
    // list from a fresh GET before making the card actionable again.
    let malformedPublishReads = 0;
    const countMalformedPublishReads = (request) => {
      if (request.method() === "GET"
          && /\/api\/scheduler\/drafts(?:\?.*)?$/.test(request.url())) {
        malformedPublishReads += 1;
      }
    };
    page.on("request", countMalformedPublishReads);
    const malformedPublishHold = await holdNextSchedulerWrite(
      page, `**${schedulerWritePath("publish", REVIEW_CARD)}`,
      "Publish malformed 2xx", {});
    await page.focus("[data-sched-publish]");
    await page.keyboard.press("Enter");
    const malformedPublishRequest = await malformedPublishHold.waitCaptured();
    if (malformedPublishRequest.method !== "POST"
        || !malformedPublishRequest.body
        || !Array.isArray(malformedPublishRequest.body.game_ids)
        || malformedPublishRequest.body.game_ids.length !== 4) {
      fail(`Publish malformed 2xx did not send four selected games: ${JSON.stringify(malformedPublishRequest)}`);
    }
    await assertPendingCard(page, fail, REVIEW_CARD, "publish", 4);
    await malformedPublishHold.finish();
    await waitForCardState(page, REVIEW_CARD, "ready",
      "Publish malformed reconciliation");
    const malformedPublish = await schedulerCardSnapshot(page, REVIEW_CARD);
    const malformedPublishToast = await schedulerToastSnapshot(page);
    if (malformedPublishReads < 1 || malformedPublish.modelCount !== 4
        || malformedPublish.selectedCount !== 4
        || !malformedPublishToast
        || !/publish response could not be verified.*Refreshing the draft list/i
          .test(malformedPublishToast.text)
        || /Published\s+\d+\s+game/i.test(malformedPublishToast.text)) {
      fail(`Publish malformed 2xx must avoid an exact success count and reconcile fresh truth: ${JSON.stringify({ malformedPublishReads, malformedPublish, malformedPublishToast })}`);
    }
    page.off("request", countMalformedPublishReads);
    malformedWriteActions.add("publish");

    const publishHold = await holdNextSchedulerWrite(
      page, "**/api/scheduler/drafts/publish", "Publish pending", { published: 4 });
    const oldPublish = await page.$("[data-sched-publish]");
    await oldPublish.focus();
    await page.keyboard.press("Enter");
    const published = await publishHold.waitCaptured();
    if (published.method !== "POST" || !published.body
        || !Array.isArray(published.body.game_ids)
        || published.body.game_ids.length !== 4) {
      fail(`Publish pending: expected four exact selected ids in one POST: ${JSON.stringify(published)}`);
    }
    await assertPendingCard(page, fail, REVIEW_CARD, "publish", 4);
    const publishPending = await schedulerCardSnapshot(page, REVIEW_CARD);
    if (publishPending.selectedCount !== 4 || !/Draft games \(4\)/.test(publishPending.text)) {
      fail(`Publish pending must retain four visible and selected drafts: ${JSON.stringify(publishPending)}`);
    }
    await clickDetachedTwice(oldPublish);
    await page.waitForTimeout(100);
    if (publishHold.requestCount() !== 1) {
      fail(`Publish pending: detached repeated activation sent ${publishHold.requestCount()} writes`);
    }
    const publishReadHold = await holdNextSchedulerWrite(
      page, draftsReadPattern, "Publish reconciliation read");
    await publishHold.finish();
    const publishRead = await publishReadHold.waitCaptured();
    if (publishRead.status !== 200 || publishRead.method !== "GET") {
      fail(`Publish reconciliation must use one real successful drafts GET: ${JSON.stringify(publishRead)}`);
    }
    await waitForCardState(page, REVIEW_CARD, "loading",
      "Publish reconciliation loading");
    const publishLoading = await schedulerCardSnapshot(page, REVIEW_CARD);
    if (publishLoading.busy !== "true"
        || publishLoading.loadingHeading !== "Loading draft review"
        || !publishLoading.loadingHeadingFocused
        || publishLoading.loadingHeadingTabIndex !== "-1") {
      fail(`Publish settlement must hand focus from PENDING to the follow-up LOADING heading: ${JSON.stringify(publishLoading)}`);
    }
    await publishReadHold.finish();
    await waitForCardState(page, REVIEW_CARD, "ready", "Publish settlement");
    review = await schedulerCardSnapshot(page, REVIEW_CARD);
    if (review.modelCount !== 4 || review.selectedCount !== 4) {
      fail(`Publish reconciliation must restore the real four-draft server truth: ${JSON.stringify(review)}`);
    }

    // Freeze a destructive decision whose selected set is larger than the
    // current filtered DOM. Every row is selected, then the issue filter is
    // chosen from the real payload so at least one selected game disappears.
    // CONFIRM must expand back to the exact selected games (names in the UI,
    // ids on the wire), rather than showing only a count beside the filtered
    // subset. Its malformed 2xx then exercises Discard's unknown-outcome path.
    const discardDecisionRows = await page.evaluate(() => {
      const entry = readCardState(SCHEDULER_REVIEW_CARD);
      const payload = cardDisplayPayload(entry) || {};
      const selected = payload.selected instanceof Set
        ? payload.selected : new Set(payload.selected || []);
      return (payload.drafts || []).filter((game) => selected.has(game.game_id))
        .map((game) => ({
          id: game.game_id,
          title: `${game.home_team_name} vs ${game.away_team_name}`,
          hasIssues: !!(game.issues && game.issues.length),
        }));
    });
    if (discardDecisionRows.length !== 4
        || new Set(discardDecisionRows.map((row) => row.id)).size !== 4
        || new Set(discardDecisionRows.map((row) => row.title)).size !== 4) {
      fail(`Filtered discard fixture needs four uniquely identifiable selected games: ${JSON.stringify(discardDecisionRows)}`);
    }
    const issueFilter = discardDecisionRows.some((row) => row.hasIssues)
      ? "clean" : "issues";
    await page.selectOption("#sched-filter-issue", issueFilter);
    const filteredSelectedRows = await page.locator("[data-sched-pick]")
      .evaluateAll((picks) => picks.filter((pick) => pick.checked).map((pick) => ({
        id: pick.dataset.schedPick,
        title: ((pick.closest(".li").querySelector(".li-title") || {}).textContent || "").trim(),
      })));
    if (filteredSelectedRows.length >= discardDecisionRows.length) {
      fail(`Discard filter must hide at least one selected game before confirmation: ${JSON.stringify({ issueFilter, discardDecisionRows, filteredSelectedRows })}`);
    }

    let malformedDiscardReads = 0;
    const countMalformedDiscardReads = (request) => {
      if (request.method() === "GET"
          && /\/api\/scheduler\/drafts(?:\?.*)?$/.test(request.url())) {
        malformedDiscardReads += 1;
      }
    };
    page.on("request", countMalformedDiscardReads);
    const malformedDiscardHold = await holdNextSchedulerWrite(
      page, `**${schedulerWritePath("discard", REVIEW_CARD)}`,
      "Discard malformed 2xx", {});
    await page.focus("[data-sched-discard]");
    await page.keyboard.press("Enter");
    await waitForCardState(page, REVIEW_CARD, "confirm",
      "filtered Discard confirmation");
    confirm = await confirmationSnapshot(page, "discard");
    const expectedDiscardDecisionTitles = discardDecisionRows
      .map((row) => row.title).sort();
    if (confirm.prompt !== "Discard 4 selected draft games? This frees their ice and cannot be undone."
        || confirm.decisionTitle !== "Drafts selected for discard (4)"
        || confirm.selectedForDiscardCount !== 4
        || JSON.stringify(confirm.selectedForDiscard)
          !== JSON.stringify(expectedDiscardDecisionTitles)
        || confirm.backgroundControls !== 0 || !confirm.yesFocused) {
      fail(`Discard CONFIRM must expand the exact filtered-out selection: ${JSON.stringify({ confirm, issueFilter, discardDecisionRows, filteredSelectedRows })}`);
    }
    await page.keyboard.press("Enter");
    const malformedDiscardRequest = await malformedDiscardHold.waitCaptured();
    const sentDiscardIds = malformedDiscardRequest.body
      && Array.isArray(malformedDiscardRequest.body.game_ids)
      ? malformedDiscardRequest.body.game_ids.slice().sort() : [];
    const expectedDiscardIds = discardDecisionRows.map((row) => row.id).sort();
    if (malformedDiscardRequest.method !== "POST"
        || JSON.stringify(sentDiscardIds) !== JSON.stringify(expectedDiscardIds)) {
      fail(`Discard malformed 2xx must POST the exact expanded decision: ${JSON.stringify({ malformedDiscardRequest, expectedDiscardIds })}`);
    }
    await assertPendingCard(page, fail, REVIEW_CARD, "discard", 4);
    await malformedDiscardHold.finish();
    await waitForCardState(page, REVIEW_CARD, "ready",
      "Discard malformed reconciliation");
    const malformedDiscard = await schedulerCardSnapshot(page, REVIEW_CARD);
    const malformedDiscardToast = await schedulerToastSnapshot(page);
    if (malformedDiscardReads < 1 || malformedDiscard.modelCount !== 4
        || malformedDiscard.selectedCount !== 4
        || !malformedDiscardToast
        || !/discard response could not be verified.*Refreshing the draft list/i
          .test(malformedDiscardToast.text)
        || /Discarded\s+\d+\s+game/i.test(malformedDiscardToast.text)) {
      fail(`Discard malformed 2xx must avoid an exact success count and reconcile fresh truth: ${JSON.stringify({ malformedDiscardReads, malformedDiscard, malformedDiscardToast })}`);
    }
    page.off("request", countMalformedDiscardReads);
    malformedWriteActions.add("discard");
    await page.selectOption("#sched-filter-issue", "all");

    const uncoveredMalformedWrites = schedulerWriteAxis
      .map((declared) => declared.action)
      .filter((action) => !malformedWriteActions.has(action));
    const undeclaredMalformedWrites = Array.from(malformedWriteActions)
      .filter((action) => !schedulerWriteByAction.has(action));
    if (uncoveredMalformedWrites.length || undeclaredMalformedWrites.length
        || malformedWriteActions.size !== schedulerWriteAxis.length) {
      fail(`Malformed 2xx coverage must equal SCHEDULER_WRITE_ACTIONS exactly: ${JSON.stringify({ schedulerWriteAxis, covered: Array.from(malformedWriteActions).sort(), uncoveredMalformedWrites, undeclaredMalformedWrites })}`);
    }

    // The row-level discard used to reuse delBtn(), whose MANAGE_SETUP gate
    // hid it from Arena Managers even though the Scheduler contract is
    // MANAGE_SCHEDULE. Cross the real principal boundary, explicitly select
    // the same Program/Season for the new account, and prove the role that
    // separates those permissions sees all four row actions. The shared modal
    // must not write on open; its confirmation must serialize through Review
    // PENDING and POST exactly the clicked row's one id (never the bulk Set).
    await page.evaluate(() => {
      const signout = document.getElementById("signout-btn");
      if (!signout) throw new Error("missing #signout-btn");
      signout.click();
    });
    await page.waitForFunction(() => !currentUser, null, { timeout: 15000 });
    await page.waitForSelector("#login-screen:not([hidden])", { timeout: 15000 });
    await page.fill("#login-user", ids.arenaUsername);
    await page.fill("#login-pass", "demo");
    await page.click("#login-form button[type=submit]");
    await page.waitForFunction((username) => currentUser
      && currentUser.username === username && currentUser.role === "arena_manager",
    ids.arenaUsername, { timeout: 15000 });
    await page.evaluate(async ({ programId, seasonId }) => {
      await window.hsFixture.selectProgramSeason(
        "Arena Manager scheduler row-discard context", programId, seasonId);
    }, { programId: ids.leagueId, seasonId: ids.seasonId });
    await page.waitForSelector('.tab[data-tab="scheduler"]',
      { state: "visible", timeout: 10000 });
    await page.click('.tab[data-tab="scheduler"]');
    await waitForCardState(page, REVIEW_CARD, "ready", "Arena Manager Review");

    // Two rows deliberately share the same matchup name while keeping their
    // real, independently deletable ids. Only the browser's retained display
    // model is changed: the server records stay untouched, so confirming the
    // second row still performs one real destructive write. Distinct time and
    // rink text make a matchup-only confirmation provably insufficient.
    const duplicateRowFixture = await page.evaluate(() => {
      const entry = readCardState(SCHEDULER_REVIEW_CARD);
      const payload = cardDisplayPayload(entry) || {};
      const drafts = payload.drafts || [];
      if (drafts.length < 2) throw new Error("need two real drafts for duplicate-row fixture");
      const originals = drafts.map((game) => ({
        id: game.game_id,
        title: `${game.home_team_name} vs ${game.away_team_name}`,
      }));
      const firstId = drafts[0].game_id;
      const secondId = drafts[1].game_id;
      updateSchedulerReviewCard((next) => {
        next.drafts = (next.drafts || []).map((game) => {
          if (game.game_id !== firstId && game.game_id !== secondId) return game;
          const second = game.game_id === secondId;
          return Object.assign({}, game, {
            home_team_name: "Duplicate Home",
            away_team_name: "Duplicate Away",
            start_time: second
              ? "2037-01-15T22:00:00+00:00"
              : "2037-01-15T21:00:00+00:00",
            rink_name: second ? "Twin Rink Beta" : "Twin Rink Alpha",
          });
        });
      });
      repaintSchedulerSurface(SCHEDULER_REVIEW_CARD);
      return { firstId, secondId, originals };
    });
    const rowDiscardRows = await page.locator("[data-sched-row-discard]")
      .evaluateAll((buttons) => buttons.map((button) => ({
        id: button.dataset.schedRowDiscard,
        title: ((button.closest(".li").querySelector(".li-title") || {}).textContent || "").trim(),
        date: ((button.closest(".li").querySelector(".li-date") || {}).textContent || "").trim(),
        time: ((button.closest(".li").querySelector(".li-time") || {}).textContent || "").trim(),
        rink: ((button.closest(".li").querySelector(".li-sub") || {}).textContent || "")
          .split("·")[1].trim(),
        descriptor: button.dataset.delName,
        ariaLabel: button.getAttribute("aria-label"),
      })));
    const rowDiscardIds = rowDiscardRows.map((row) => row.id);
    if (rowDiscardIds.length !== 4 || new Set(rowDiscardIds).size !== 4) {
      fail(`Arena Manager must see one distinct row discard per draft: ${JSON.stringify(rowDiscardRows)}`);
    }
    const duplicateRows = rowDiscardRows.filter((row) =>
      row.id === duplicateRowFixture.firstId
      || row.id === duplicateRowFixture.secondId);
    const secondDuplicate = rowDiscardRows.find((row) =>
      row.id === duplicateRowFixture.secondId);
    const expectedSecondDescriptor = secondDuplicate
      && `${secondDuplicate.title} — ${secondDuplicate.date} ${secondDuplicate.time} — ${secondDuplicate.rink}`;
    if (duplicateRows.length !== 2
        || new Set(duplicateRows.map((row) => row.title)).size !== 1
        || new Set(duplicateRows.map((row) => row.time)).size !== 2
        || new Set(duplicateRows.map((row) => row.rink)).size !== 2
        || !secondDuplicate
        || secondDuplicate.descriptor !== expectedSecondDescriptor
        || secondDuplicate.ariaLabel !== `Discard draft ${expectedSecondDescriptor}`) {
      fail(`row-discard fixture must distinguish two same-matchup rows by exact time and rink: ${JSON.stringify({ duplicateRows, expectedSecondDescriptor })}`);
    }
    const rowId = duplicateRowFixture.secondId;

    // Escape is an overlay-only cancellation. It must neither launch a card
    // load nor advance either Scheduler generation, and the exact second-row
    // trigger (not BODY or the first duplicate) must regain focus.
    let rowModalSchedulerGets = 0;
    let rowModalSchedulerWrites = 0;
    const countRowModalTraffic = (request) => {
      if (!/\/api\/scheduler\//.test(request.url())) return;
      if (request.method() === "GET") rowModalSchedulerGets += 1;
      if (request.method() === "POST") rowModalSchedulerWrites += 1;
    };
    page.on("request", countRowModalTraffic);
    const generationsBeforeEscape = await page.evaluate(() => ({
      draft: readCardState(SCHEDULER_DRAFT_CARD).identity.generation,
      review: readCardState(SCHEDULER_REVIEW_CARD).identity.generation,
    }));
    await page.focus(`[data-sched-row-discard="${rowId}"]`);
    await page.keyboard.press("Enter");
    await page.waitForSelector("[data-del-confirm]", { state: "visible", timeout: 10000 });
    let rowDialog = await page.evaluate((id) => ({
      title: ((document.querySelector(".modal h2") || {}).textContent || "").trim(),
      descriptor: ((document.querySelector(".modal strong") || {}).textContent || "").trim(),
      sourceStillPresent: !!document.querySelector(`[data-sched-row-discard="${id}"]`),
    }), rowId);
    if (rowDialog.title !== "Discard this draft game?"
        || rowDialog.descriptor !== expectedSecondDescriptor
        || !rowDialog.sourceStillPresent || rowModalSchedulerGets !== 0
        || rowModalSchedulerWrites !== 0) {
      fail(`second duplicate must open its exact decision without reads or writes: ${JSON.stringify({ rowDialog, expectedSecondDescriptor, rowModalSchedulerGets, rowModalSchedulerWrites })}`);
    }
    await page.keyboard.press("Escape");
    await page.waitForSelector(".modal", { state: "detached", timeout: 10000 });
    await page.waitForFunction((id) => document.activeElement
      && document.activeElement.dataset.schedRowDiscard === id,
    rowId, { timeout: 10000 });
    const escapedRowModal = await page.evaluate((id) => ({
      modal: !!document.querySelector(".modal"),
      activeId: document.activeElement
        && document.activeElement.dataset.schedRowDiscard,
      activeTag: document.activeElement && document.activeElement.tagName,
      draftGeneration: readCardState(SCHEDULER_DRAFT_CARD).identity.generation,
      reviewGeneration: readCardState(SCHEDULER_REVIEW_CARD).identity.generation,
      triggerPresent: !!document.querySelector(`[data-sched-row-discard="${id}"]`),
    }), rowId);
    page.off("request", countRowModalTraffic);
    if (escapedRowModal.modal || !escapedRowModal.triggerPresent
        || escapedRowModal.activeTag === "BODY"
        || escapedRowModal.activeId !== rowId
        || escapedRowModal.draftGeneration !== generationsBeforeEscape.draft
        || escapedRowModal.reviewGeneration !== generationsBeforeEscape.review
        || rowModalSchedulerGets !== 0 || rowModalSchedulerWrites !== 0) {
      fail(`Escape must close only the card-owned modal and restore its exact trigger: ${JSON.stringify({ generationsBeforeEscape, escapedRowModal, rowModalSchedulerGets, rowModalSchedulerWrites })}`);
    }

    // This time the real server completes the deletion, but the browser gets
    // deliberately unreadable non-JSON. The client must treat the outcome as
    // unknown, never replay the write, and reconcile from fresh server truth.
    const rowDiscardHold = await holdCompletedSchedulerWriteWithUnreadableResponse(
      page, "**/api/scheduler/drafts/discard",
      "Arena Manager row discard unreadable response");
    await page.focus(`[data-sched-row-discard="${rowId}"]`);
    await page.keyboard.press("Enter");
    await page.waitForSelector("[data-del-confirm]", { state: "visible", timeout: 10000 });
    rowDialog = await page.evaluate(() => ({
      descriptor: ((document.querySelector(".modal strong") || {}).textContent || "").trim(),
    }));
    if (rowDialog.descriptor !== expectedSecondDescriptor
        || rowDiscardHold.requestCount() !== 0) {
      fail(`reopened second duplicate decision drifted or wrote before confirmation: ${JSON.stringify({ rowDialog, expectedSecondDescriptor, requests: rowDiscardHold.requestCount() })}`);
    }
    // Opening an overlay deliberately queues focus to its container on the
    // next animation frame. Wait for that ownership transfer, then exercise
    // the real backward tab boundary to reach the final destructive control;
    // focusing the button immediately races the queued frame and is a false
    // negative rather than a user-reachable sequence.
    await page.waitForFunction(() => {
      const active = document.activeElement;
      return active && active.matches(".modal[role=dialog]");
    }, null, { timeout: 10000 });
    await page.keyboard.press("Shift+Tab");
    const oldRowConfirm = await page.$("[data-del-confirm]");
    const rowConfirmFocused = await page.evaluate(() => document.activeElement
      && document.activeElement.hasAttribute("data-del-confirm"));
    if (!rowConfirmFocused) {
      fail("Arena Manager row discard must reach Discard draft from the dialog's keyboard boundary");
    }
    await page.keyboard.press("Enter");
    const rowDiscard = await rowDiscardHold.waitCaptured();
    if (rowDiscard.method !== "POST" || !rowDiscard.body
        || JSON.stringify(rowDiscard.body.game_ids) !== JSON.stringify([rowId])) {
      fail(`Arena Manager row confirmation must POST exactly its own one id: expected ${rowId}, got ${JSON.stringify(rowDiscard)}`);
    }
    await assertPendingCard(page, fail, REVIEW_CARD, "discard", 4);
    await clickDetachedTwice(oldRowConfirm);
    await page.waitForTimeout(100);
    if (rowDiscardHold.requestCount() !== 1) {
      fail(`Arena Manager row discard: detached confirmation sent ${rowDiscardHold.requestCount()} writes`);
    }

    // The POST above has already completed on the real server, but its response
    // is unreadable and the next three reconciliation reads (the immediate one
    // plus two explicit Retry attempts) fail. Every failure must retain the
    // complete last-loaded four-row model, the same outcome-unverified note,
    // and a read-only surface. Only a later successful GET may remove the row.
    let rowDiscardReadFailures = 0;
    const rowDiscardReadFailureBudget = 3;
    const rowDiscardReadFailure = async (route) => {
      if (route.request().method() !== "GET"
          || rowDiscardReadFailures >= rowDiscardReadFailureBudget) {
        return route.fallback();
      }
      rowDiscardReadFailures += 1;
      return route.fulfill({
        status: 500,
        contentType: "application/json",
        body: JSON.stringify({ error: {
          code: "forced_post_discard_read_failure",
          message: "The post-discard draft refresh failed for this browser regression.",
        } }),
      });
    };
    intentionalDraftsFailure = true;
    await page.route(draftsReadPattern, rowDiscardReadFailure);
    await rowDiscardHold.finish();
    await waitForCardState(page, REVIEW_CARD, "error",
      "Arena Manager row reconciliation failure");
    let retainedAfterRowDiscard = await schedulerCardSnapshot(page, REVIEW_CARD);
    let retainedOutcomeToast = await schedulerToastSnapshot(page);
    const retainedRowTitles = rowDiscardRows.map((row) => row.title).sort();
    if (rowDiscardReadFailures !== 1
        || retainedAfterRowDiscard.readOutcome !== "failed"
        || retainedAfterRowDiscard.modelCount !== 4
        || retainedAfterRowDiscard.writeControls !== 0
        || retainedAfterRowDiscard.filterControls !== 3
        || JSON.stringify(retainedAfterRowDiscard.rowTitles)
          !== JSON.stringify(retainedRowTitles)
        || !/last draft list loaded before the discard/i.test(retainedAfterRowDiscard.text)
        || !/response could not be verified/i.test(retainedAfterRowDiscard.text)
        || !/may be out of date and remains read-only until Retry succeeds/i
          .test(retainedAfterRowDiscard.text)
        || !retainedOutcomeToast
        || !/discard response could not be verified.*Refreshing the draft list/i
          .test(retainedOutcomeToast.text)
        || !await page.locator(retrySelector).isVisible()) {
      fail(`Unknown row-discard outcome plus failed refresh must retain four explicitly stale read-only rows: ${JSON.stringify({ rowDiscardReadFailures, retainedAfterRowDiscard, retainedOutcomeToast, retainedRowTitles })}`);
    }

    for (let retry = 1; retry <= 2; retry += 1) {
      const beforeRetry = retainedAfterRowDiscard;
      await page.focus(retrySelector);
      await page.keyboard.press("Enter");
      await page.waitForFunction((priorGeneration) => {
        const entry = readCardState(SCHEDULER_REVIEW_CARD);
        return entry && entry.state === "error" && entry.identity
          && entry.identity.generation > priorGeneration;
      }, beforeRetry.generation, { timeout: 15000 });
      retainedAfterRowDiscard = await schedulerCardSnapshot(page, REVIEW_CARD);
      retainedOutcomeToast = await schedulerToastSnapshot(page);
      if (rowDiscardReadFailures !== retry + 1
          || rowDiscardHold.requestCount() !== 1
          || retainedAfterRowDiscard.readOutcome !== "failed"
          || retainedAfterRowDiscard.modelCount !== 4
          || retainedAfterRowDiscard.writeControls !== 0
          || retainedAfterRowDiscard.filterControls !== 3
          || JSON.stringify(retainedAfterRowDiscard.rowTitles)
            !== JSON.stringify(retainedRowTitles)
          || !/last draft list loaded before the discard/i.test(retainedAfterRowDiscard.text)
          || !/response could not be verified/i.test(retainedAfterRowDiscard.text)
          || !/may be out of date and remains read-only until Retry succeeds/i
            .test(retainedAfterRowDiscard.text)
          || !retainedOutcomeToast
          || !/discard response could not be verified/i.test(retainedOutcomeToast.text)
          || !await page.locator(retrySelector).isVisible()) {
        fail(`Retry ${retry} must preserve the same unknown-outcome retention chain without replaying the write: ${JSON.stringify({ rowDiscardReadFailures, requests: rowDiscardHold.requestCount(), retainedAfterRowDiscard, retainedOutcomeToast, retainedRowTitles })}`);
      }
    }

    await page.unroute(draftsReadPattern, rowDiscardReadFailure);
    intentionalDraftsFailure = false;

    await page.focus(retrySelector);
    await page.keyboard.press("Enter");
    await waitForCardState(page, REVIEW_CARD, "ready",
      "Arena Manager row reconciliation Retry");
    review = await schedulerCardSnapshot(page, REVIEW_CARD);
    const remainingRowDiscardIds = await page.locator("[data-sched-row-discard]")
      .evaluateAll((buttons) => buttons.map((button) => button.dataset.schedRowDiscard).sort());
    const expectedRemainingRowIds = rowDiscardIds
      .filter((id) => id !== rowId).sort();
    const expectedRemainingRowTitles = duplicateRowFixture.originals
      .filter((row) => row.id !== rowId).map((row) => row.title).sort();
    if (review.modelCount !== 3
        || review.readOutcome !== "ok"
        || rowDiscardHold.requestCount() !== 1
        || JSON.stringify(remainingRowDiscardIds) !== JSON.stringify(expectedRemainingRowIds)
        || JSON.stringify(review.rowTitles) !== JSON.stringify(expectedRemainingRowTitles)) {
      fail(`Arena Manager Retry must prove the real write completed by removing exactly its second duplicate id without replay: ${JSON.stringify({ review, rowDiscardRows, remainingRowDiscardIds, requests: rowDiscardHold.requestCount() })}`);
    }
    await page.click("[data-sched-select-all]");
    review = await schedulerCardSnapshot(page, REVIEW_CARD);
    if (review.selectedCount !== 3) {
      fail(`Bulk discard setup after principal switch must select the three remaining drafts: ${JSON.stringify(review)}`);
    }

    // Discard is the second explicit decision and proves that selection/counts
    // survive Cancel. Then run its real destructive path behind the same held
    // delivery and stale-control double-submit attack. The final EMPTY state is
    // asserted independently of the success count.
    let discardRequests = 0;
    const countDiscard = (request) => {
      if (request.method() === "POST"
          && /\/api\/scheduler\/drafts\/discard(?:\?.*)?$/.test(request.url())) {
        discardRequests += 1;
      }
    };
    page.on("request", countDiscard);
    await page.focus("[data-sched-discard]");
    await page.keyboard.press("Enter");
    await waitForCardState(page, REVIEW_CARD, "confirm", "Discard cancel confirmation");
    confirm = await confirmationSnapshot(page, "discard");
    if (discardRequests !== 0 || confirm.busy !== "false" || !confirm.yesFocused
        || confirm.backgroundControls !== 0
        || confirm.prompt !== "Discard 3 selected draft games? This frees their ice and cannot be undone."
        || confirm.yesText !== "Discard drafts" || confirm.noText !== "Keep drafts") {
      fail(`Discard confirmation must be exact, focused, and pre-request: ${JSON.stringify({ confirm, discardRequests })}`);
    }
    await page.keyboard.press("Tab");
    const discardNoFocused = await page.evaluate(() => document.activeElement
      && document.activeElement.dataset.schedConfirmNo === "discard");
    if (!discardNoFocused) fail("Discard cancellation must be keyboard reachable after Yes");
    await page.keyboard.press("Enter");
    await waitForCardState(page, REVIEW_CARD, "ready", "Discard cancellation");
    review = await schedulerCardSnapshot(page, REVIEW_CARD);
    if (discardRequests !== 0 || review.modelCount !== 3 || review.selectedCount !== 3
        || !review.active || !review.active.discard) {
      fail(`Discard cancel must preserve rows/selection and restore focus without POST: ${JSON.stringify({ review, discardRequests })}`);
    }
    page.off("request", countDiscard);

    const discardHold = await holdNextSchedulerWrite(
      page, "**/api/scheduler/drafts/discard", "Discard pending");
    await page.focus("[data-sched-discard]");
    await page.keyboard.press("Enter");
    await waitForCardState(page, REVIEW_CARD, "confirm", "Discard confirmation");
    const oldDiscardYes = await page.$('[data-sched-confirm-yes="discard"]');
    await page.keyboard.press("Enter");
    const discarded = await discardHold.waitCaptured();
    if (discarded.status !== 200 || discarded.method !== "POST"
        || !discarded.body || !Array.isArray(discarded.body.game_ids)
        || discarded.body.game_ids.length !== 3) {
      fail(`Discard pending: expected three exact selected ids in one real POST: ${JSON.stringify(discarded)}`);
    }
    await assertPendingCard(page, fail, REVIEW_CARD, "discard", 3);
    const discardPending = await schedulerCardSnapshot(page, REVIEW_CARD);
    if (discardPending.selectedCount !== 3 || !/Draft games \(3\)/.test(discardPending.text)) {
      fail(`Discard pending must retain three visible and selected drafts: ${JSON.stringify(discardPending)}`);
    }
    await clickDetachedTwice(oldDiscardYes);
    await page.waitForTimeout(100);
    if (discardHold.requestCount() !== 1) {
      fail(`Discard pending: detached repeated affirmation sent ${discardHold.requestCount()} writes`);
    }
    await discardHold.finish();
    await waitForCardState(page, REVIEW_CARD, "empty", "Discard settlement");
    review = await schedulerCardSnapshot(page, REVIEW_CARD);
    if (review.modelCount !== 0 || review.readOutcome !== "ok"
        || !/No draft games in this selection/i.test(review.text)) {
      fail(`Discard settlement must end in asserted true-empty Review: ${JSON.stringify(review)}`);
    }

    // The principal-boundary leg above deliberately proves Arena Manager can
    // operate Scheduler without MANAGE_SETUP. The remaining historical
    // eligibility fixture creates/deletes Setup records, so explicitly return
    // to the League Admin rather than accidentally asking the narrower role to
    // perform out-of-scope setup work.
    await page.evaluate(() => {
      const signout = document.getElementById("signout-btn");
      if (!signout) throw new Error("missing #signout-btn");
      signout.click();
    });
    await page.waitForFunction(() => !currentUser, null, { timeout: 15000 });
    await page.waitForSelector("#login-screen:not([hidden])", { timeout: 15000 });
    await page.fill("#login-user", "admin");
    await page.fill("#login-pass", "demo");
    await page.click("#login-form button[type=submit]");
    await page.waitForFunction(() => currentUser
      && currentUser.username === "admin" && currentUser.role === "league_admin",
    null, { timeout: 15000 });
    await page.evaluate(async ({ programId, seasonId }) => {
      await window.hsFixture.selectProgramSeason(
        "League Admin eligibility setup context", programId, seasonId);
    }, { programId: ids.leagueId, seasonId: ids.seasonId });

    // (6) Real (UNSTUBBED) end-to-end proof that a team-eligibility change
    // between Generate and Commit is caught by the REAL backend and
    // correctly surfaced by the UI (#328 review round 10 finding 1).
    // Unlike scenarios (3)/(4) above, Commit here is NOT intercepted --
    // this is a genuine round trip through commit_draft_schedule's own
    // regeneration and fingerprint comparison, not a canned response.
    //
    // The fixture starts with an EVEN (2) team count -- round_robin_pairings'
    // circle method has no bye at all there, so with exactly one ice slot
    // the two teams' single pairing is what gets placed. Registering a
    // THIRD team that sorts alphabetically FIRST makes the round robin ODD
    // and gives THAT new team the round-0 bye, while the two original teams
    // shift up by exactly one array position each -- preserving their
    // mutual pairing byte-for-byte on Commit's own regeneration (the same
    // circle-method mechanic proven directly, with hand-picked ids, in
    // test_scheduler.py's `_stale_preview_refused_on_team_eligibility_change`
    // "register" case). This isolates the eligibility axis from the
    // (separately covered) placement axis: a fingerprint that only hashed
    // draft_games/already_scheduled would see no difference at all and let
    // Commit silently persist a batch the operator never reviewed the
    // three-team version of.
    //
    // Two setup quirks this depends on:
    // - /api/scheduler/draft draws game slots from the WHOLE store, not
    //   just this division's own rink -- the four still-AVAILABLE Mixed
    //   slots from setup above (scenarios (1)/(3)/(4) only ever preview or
    //   stub-fail Commit for that division, so they were never actually
    //   consumed) would otherwise leak into this fixture's round robin.
    // - Team ids are assigned sequentially as "team_N" and compared as
    //   STRINGS, not numbers: crossing from single- to double-digit (e.g.
    //   "team_9" -> "team_10") sorts the new double-digit id FIRST (its
    //   first differing character, '1', is less than '9'). One
    //   unregistered decoy team is created between the two original teams
    //   and the late third team specifically to land that crossing exactly
    //   where the new team needs to sort first.
    const elig = await page.evaluate(async ({ seasonId, levelId, leagueId, day, openSlotIds }) => {
      const F = window.hsFixture;
      for (const id of openSlotIds) {
        await F.call("delete", `/api/setup/ice-slot/${id}/delete`, {});
      }
      // #409: the Division, the two registrations and the venue-access grant
      // are SEASON-OWNED in this Season, and real UI work has intervened
      // since the bootstrap — state the tuple rather than assume it stands.
      await F.selectProgramSeason("Program+Season for the eligibility fixture",
        leagueId, seasonId);
      const division = await F.create("division", "/api/setup/division",
        { season_id: seasonId, level_id: levelId, name: "TeamEligibility" });
      const club = await F.create("club", "/api/setup/club", { name: "Eligibility Club" });
      const team = async (n) => (await post("/api/v2/setup/team",
        { club_id: club.id, league_id: levelId, name: n })).id;
      const e0 = await team("Eligibility 0");
      const e1 = await team("Eligibility 1");
      const register = (teamId) => post(
        `/api/setup/seasons/${seasonId}/team-registrations`,
        { team_id: teamId, division_id: division.id });
      await register(e0);
      await register(e1);
      const venue = await F.create("venue", "/api/setup/venue",
        { name: "Eligibility Arena", league_id: leagueId });
      await F.call("season venue-access grant", `/api/v2/setup/seasons/${seasonId}/venue-access`,
        { venue_id: venue.id });
      const rink = await F.create("rink", "/api/setup/rink",
        { venue_id: venue.id, name: "Eligibility Rink" });
      await F.call("ice-slot", "/api/setup/ice-slot", {
        rink_id: rink.id, start_time: `${day}T23:00:00+00:00`,
        end_time: `${day}T23:59:00+00:00`, slot_type: "game",
      });
      return { divisionId: division.id, clubId: club.id };
    }, {
      seasonId: ids.seasonId, levelId: ids.levelId, leagueId: ids.leagueId,
      day: ICE_DAY, openSlotIds: ids.mixedOpenSlotIds,
    });

    // The Scheduler tab's division list is captured at render time, not
    // re-fetched on every action -- switch away and back to force a fresh
    // render that picks up the just-created division.
    await page.click('.tab[data-tab="dashboard"]');
    await page.waitForSelector('.tab[data-tab="scheduler"]', { state: "visible", timeout: 10000 });
    await page.click('.tab[data-tab="scheduler"]');
    await page.waitForSelector("#sched-div", { timeout: 10000 });
    await page.waitForFunction(
      (id) => !!document.querySelector(`#sched-div option[value="${id}"]`),
      elig.divisionId, { timeout: 10000 });
    await generateFor(page, elig.divisionId,
      '#sched-preview[data-games="1"][data-already-scheduled="0"]');
    s = await previewState(page);
    if (s.commitDisabled !== false) {
      fail(`team-eligibility staleness: needs an enabled Commit to click: ${JSON.stringify(s)}`);
    }
    const placedRowBefore = s.rows.find((r) => !r.sub.includes("Already scheduled"));
    if (!placedRowBefore) {
      fail(`team-eligibility staleness: expected exactly one placed row: ${JSON.stringify(s)}`);
    }

    // One unregistered decoy team (see the digit-boundary note above), then
    // a third team that registers in the SAME division after Generate --
    // no stub, no interception, just a real backend write in the gap
    // between the operator's preview and clicking Commit.
    await page.evaluate(async ({ seasonId, divisionId, clubId, levelId, leagueId }) => {
      const F = window.hsFixture;
      // #409: the late registration below is SEASON-OWNED in this Season.
      await F.selectProgramSeason("Program+Season for the late registration",
        leagueId, seasonId, "api");
      await F.call("decoy team", "/api/v2/setup/team",
        { club_id: clubId, league_id: levelId, name: "(decoy)" });
      const e2 = await F.create("late team Eligibility 2", "/api/v2/setup/team",
        { club_id: clubId, league_id: levelId, name: "Eligibility 2 (late)" });
      await F.call("team registration", `/api/setup/seasons/${seasonId}/team-registrations`,
        { team_id: e2.id, division_id: divisionId });
    }, {
      seasonId: ids.seasonId, divisionId: elig.divisionId,
      clubId: elig.clubId, levelId: ids.levelId, leagueId: ids.leagueId,
    });

    await page.click("[data-sched-commit]");
    await waitForCardState(page, DRAFT_CARD, "confirm",
      "team-eligibility confirmation");
    await page.click('[data-sched-confirm-yes="commit"]');
    await page.waitForFunction(
      () => /out of date/i.test((document.querySelector(
        '[data-operational-card="scheduler/draft"] .banner.alert') || {}).textContent || ""),
      null, { timeout: 10000 });
    await page.waitForFunction(
      () => !document.querySelector("[data-sched-commit]")
        && !!document.querySelector("[data-sched-generate]"),
      null, { timeout: 10000 });

    // Zero side effects: a fresh Generate for the SAME (now 3-team)
    // division must still show nothing already-scheduled -- if the
    // refused commit had silently persisted anyway, the previously
    // previewed pairing would show up as already-scheduled here. It must
    // also still place the EXACT SAME pairing on the exact same slot
    // (confirming the fixture really did isolate the eligibility axis --
    // Commit was refused despite the placed row being unchanged, not
    // because the round robin happened to reshuffle it too).
    await generateFor(page, elig.divisionId,
      '#sched-preview[data-already-scheduled="0"]');
    s = await previewState(page);
    if (s.alreadyScheduled !== "0") {
      fail(`team-eligibility staleness: refused commit must not have created `
        + `any Game (would show as already-scheduled on the next preview): ${JSON.stringify(s)}`);
    }
    const placedRowAfter = s.rows.find((r) => !r.sub.includes("Already scheduled"));
    if (!placedRowAfter || placedRowAfter.title !== placedRowBefore.title
        || placedRowAfter.sub !== placedRowBefore.sub) {
      fail(`team-eligibility staleness: expected the late third team's `
        + `registration to leave the originally placed row unchanged `
        + `(proving this fixture isolates the eligibility axis): `
        + `before=${JSON.stringify(placedRowBefore)} after=${JSON.stringify(placedRowAfter)}`);
    }

    if (errors.length) {
      fail(`console/page errors:\n${errors.join("\n")}`);
    }
    console.log(`[${viewport.label}] OK — Scheduler confirms destructive writes by keyboard, serializes every declared write through count-preserving PENDING, rejects malformed 2xx outcomes with fresh reconciliation, expands filtered discard decisions to exact games, retains and labels rows read-only when a post-write refresh fails, and Retry proves the one real Arena Manager discard.`);
  } catch (error) {
    const diagnostic = await page.evaluate(() => ({
      view: typeof view === "undefined" ? null : view,
      context: typeof contextOptions === "undefined" ? null : contextOptions,
      draftCard: typeof readCardState === "function"
        ? readCardState("scheduler/draft") : null,
      renderPass: typeof renderPass === "undefined" ? null : renderPass,
      contextRevision: typeof contextRevision === "undefined" ? null : contextRevision,
      switchPending: typeof contextSwitchIntentPending === "undefined"
        ? null : contextSwitchIntentPending,
      scopedAborts: typeof contextScopedReadAborts === "undefined"
        ? null : contextScopedReadAborts,
      html: (document.getElementById("content") || {}).innerHTML || "",
      content: (document.getElementById("content") || {}).innerText || "",
    })).catch((e) => ({ diagnosticError: e.message }));
    throw new Error(`${error.message}\n--- page state ---\n${JSON.stringify(diagnostic)}`
      + `\n--- demo server output ---\n${serverOutput}`);
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
    console.log("Scheduler already-scheduled browser journey passed.");
  } catch (error) {
    console.error("Scheduler already-scheduled browser journey FAILED.");
    console.error(error && error.message ? error.message : error);
    process.exitCode = 1;
  } finally {
    if (browser) await browser.close();
  }
}

main();
