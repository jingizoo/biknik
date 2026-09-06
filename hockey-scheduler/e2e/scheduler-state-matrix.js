// Operational-card state and identity journey for #393 PR B.
//
// Four independently-owned cards are exercised through their real UI entry
// points at desktop and 390px:
//
//   scheduler/draft          Generate a proposal
//   scheduler/review         Read and act on committed drafts
//   facilities/ice-builder   Preview recurring ice
//   calendar/board           Read the Arena Calendar board
//
// Every card must reach LOADING, READY, EMPTY, STALE and ERROR.  Transport
// holds capture the REAL response with route.fetch() before delaying browser
// delivery; this is important because delaying the request would not prove
// that an already-computed response is refused after identity changes.
//
// Two races are the load-bearing regressions:
//
//   * a successful Program A Generate response is held, the operator moves to
//     Program B, and delivery cannot paint A's proposal into B;
//   * another successful Generate response is held while the same Admin signs
//     out and signs back in on the same tuple.  /api/context/options is held
//     through the post-auth privacy window; after it settles, the Games view
//     keeps the card generation unchanged. Username, tuple and generation are
//     therefore equal when delivery resumes, leaving uiIdentityEpoch as the
//     only rejection axis.
//
// The journey never writes cardStates or calls commitCardState/render.  Card
// stores are read only as supplementary evidence; states are reached through
// shipped controls and forced transport outcomes.
const { chromium } = require("playwright");
const { spawn } = require("child_process");
const http = require("http");
const path = require("path");
const {
  installContextFixture,
  selectProgramSeason,
} = require("./context-fixture.js");

const HOST = "127.0.0.1";
const BACKEND_DIR = path.resolve(__dirname, "..", "backend");
const READY_TIMEOUT_MS = 15000;
const QUIET_WINDOW_MS = 300;
const QUIESCE_TIMEOUT_MS = 30000;
const VIEWPORTS = [
  { label: "desktop", width: 1440, height: 900, port: 8981 },
  { label: "phone", width: 390, height: 844, port: 8982 },
];

const CARD_IDS = Object.freeze([
  "scheduler/draft",
  "scheduler/review",
  "facilities/ice-builder",
  "calendar/board",
]);
const CARD_STATES = Object.freeze(["loading", "ready", "empty", "stale", "error"]);
const DRAFT_CARD = "scheduler/draft";
const REVIEW_CARD = "scheduler/review";
const BUILDER_CARD = "facilities/ice-builder";
const CALENDAR_CARD = "calendar/board";

const DRAFT_RE = /\/api\/scheduler\/draft$/;
const DRAFT_COMMIT_RE = /\/api\/scheduler\/commit$/;
const DRAFTS_RE = /\/api\/scheduler\/drafts(?:\?|$)/;
const PUBLISH_RE = /\/api\/scheduler\/drafts\/publish$/;
const ICE_PREVIEW_RE = /\/api\/setup\/ice-availability\/preview$/;
const ICE_COMMIT_RE = /\/api\/setup\/ice-availability\/commit$/;
const OVERVIEW_RE = /\/api\/demo\/overview(?:\?|$)/;
const CONTEXT_OPTIONS_RE = /\/api\/context\/options(?:\?|$)/;
const CONTEXT_RE = /\/api\/context$/;

const MUTATION_SELECTORS = Object.freeze({
  // Preview/Generate and row selection are supersedable reads or local
  // choices.  The loading/stale contract withdraws persisted writes; it does
  // not make a same-tuple read-only computation artificially single-flight.
  [DRAFT_CARD]: "[data-sched-commit]",
  [REVIEW_CARD]: "[data-sched-publish],[data-sched-discard],[data-del]",
  [BUILDER_CARD]: "[data-ib-commit]",
  [CALENDAR_CARD]: "[data-addslot],[data-game],[data-move-game],"
    + "[data-move-confirm],[data-schedule-confirm]",
});

function fail(message) { throw new Error(message); }
function trace(message) { console.error(`  · ${message}`); }

function waitForServer(url, timeoutMs) {
  const deadline = Date.now() + timeoutMs;
  return new Promise((resolve, reject) => {
    const tick = () => {
      const request = http.get(url, (response) => {
        response.resume();
        resolve();
      });
      request.setTimeout(2000, () => request.destroy(new Error("request timed out")));
      request.on("error", () => {
        if (Date.now() > deadline) reject(new Error(`server never came up at ${url}`));
        else setTimeout(tick, 200);
      });
    };
    tick();
  });
}

function stopServer(server) {
  return new Promise((resolve) => {
    if (!server || server.exitCode !== null || server.signalCode !== null) return resolve();
    const hard = setTimeout(() => { try { server.kill("SIGKILL"); } catch (_) {} }, 3000);
    server.once("exit", () => { clearTimeout(hard); resolve(); });
    server.kill("SIGTERM");
  });
}

function makeChannel(name, pattern) {
  return {
    name,
    pattern,
    mode: "pass",
    heldNow: 0,
    released: 0,
    capture: null,
    resolveCapture: () => {},
    fetchResult: null,
    resolveFetchResult: () => {},
    gate: null,
    resolveGate: () => {},
    skipBeforeHold: 0,
    injected: [],
  };
}

function armRequestHold(channel) {
  if (channel.mode !== "pass" || channel.heldNow) {
    fail(`${channel.name}: cannot arm a request hold while the channel is busy`);
  }
  channel.capture = new Promise((resolve) => { channel.resolveCapture = resolve; });
  channel.fetchResult = new Promise((resolve) => {
    channel.resolveFetchResult = resolve;
  });
  channel.gate = new Promise((resolve) => { channel.resolveGate = resolve; });
  channel.mode = "hold-request";
  let released = false;
  return {
    captured: channel.capture,
    fetched: channel.fetchResult,
    release() {
      if (released) fail(`${channel.name}: held request released twice`);
      released = true;
      channel.resolveGate();
    },
  };
}

function armHold(channel) {
  if (channel.mode !== "pass" || channel.heldNow) {
    fail(`${channel.name}: cannot arm a second hold while the channel is busy`);
  }
  channel.capture = new Promise((resolve) => { channel.resolveCapture = resolve; });
  channel.gate = new Promise((resolve) => { channel.resolveGate = resolve; });
  channel.mode = "hold";
  let released = false;
  return {
    captured: channel.capture,
    release() {
      if (released) fail(`${channel.name}: held response released twice`);
      released = true;
      channel.resolveGate();
    },
  };
}

// Hold a real refused context response after the server has computed it.  The
// browser still sends the offered B selection, but the transport substitutes
// a definitely-missing Program only for route.fetch(); the server therefore
// exercises its normal non-oracle refusal path without moving canonical A.
// This is intentionally different from failOnce(): the failure must already
// exist on the far side of the await while the test controls browser delivery.
function armRejectedContextHold(channel) {
  if (channel.mode !== "pass" || channel.heldNow) {
    fail(`${channel.name}: cannot arm a rejected hold while the channel is busy`);
  }
  channel.capture = new Promise((resolve) => { channel.resolveCapture = resolve; });
  channel.gate = new Promise((resolve) => { channel.resolveGate = resolve; });
  channel.mode = "hold-rejected-context";
  let released = false;
  return {
    captured: channel.capture,
    release() {
      if (released) fail(`${channel.name}: held rejection released twice`);
      released = true;
      channel.resolveGate();
    },
  };
}

function armHoldAfter(channel, skipCount) {
  if (!Number.isInteger(skipCount) || skipCount < 1) {
    fail(`${channel.name}: skipped request count must be a positive integer`);
  }
  const hold = armHold(channel);
  channel.skipBeforeHold = skipCount;
  channel.mode = "skip";
  return hold;
}

function failOnce(channel) {
  if (channel.mode !== "pass" || channel.heldNow) {
    fail(`${channel.name}: cannot inject a failure while the channel is busy`);
  }
  channel.mode = "fail";
}

async function installChannel(page, channel) {
  await page.route(channel.pattern, async (route) => {
    const mode = channel.mode;
    if (mode === "pass") return route.continue();
    if (mode === "skip") {
      channel.skipBeforeHold -= 1;
      if (!channel.skipBeforeHold) channel.mode = "hold";
      return route.continue();
    }
    if (mode === "hold-request") {
      channel.mode = "pass";
      channel.heldNow += 1;
      channel.resolveCapture({
        method: route.request().method(),
        url: route.request().url(),
        releasedBefore: channel.released,
      });
      await channel.gate;
      try {
        const response = await route.fetch();
        let body = null;
        try { body = await response.json(); } catch (_) {}
        channel.resolveFetchResult({
          method: route.request().method(),
          url: route.request().url(),
          status: response.status(),
          body,
        });
        await route.fulfill({ response });
      } catch (error) {
        channel.resolveFetchResult({
          method: route.request().method(),
          url: route.request().url(),
          aborted: true,
          error: String(error && error.message || error),
        });
        try { await route.abort(); } catch (_) {}
      } finally {
        channel.heldNow -= 1;
        channel.released += 1;
      }
      return;
    }
    channel.mode = "pass"; // every forced outcome is one-shot
    if (mode === "fail") {
      const rec = {
        method: route.request().method(),
        url: route.request().url(),
        status: 500,
        seen: false,
      };
      channel.injected.push(rec);
      return route.fulfill({
        status: 500,
        contentType: "application/json",
        body: JSON.stringify({ error: {
          code: "matrix_forced_failure",
          message: `Forced ${channel.name} failure for the state-matrix journey.`,
        } }),
      });
    }
    if (mode !== "hold" && mode !== "hold-rejected-context") {
      fail(`${channel.name}: unknown transport mode ${mode}`);
    }
    const response = mode === "hold-rejected-context"
      ? await route.fetch({ postData: JSON.stringify({
        program_id: "matrix-rejected-program-never-created",
        season_id: null,
        league_id: null,
      }) })
      : await route.fetch();
    let body = null;
    try { body = await response.json(); } catch (_) {}
    if (mode === "hold-rejected-context") {
      channel.injected.push({
        method: route.request().method(),
        url: route.request().url(),
        status: response.status(),
        seen: false,
      });
    }
    const captured = {
      method: route.request().method(),
      url: route.request().url(),
      status: response.status(),
      body,
      releasedBefore: channel.released,
      requestedBody: (() => {
        try { return route.request().postDataJSON(); } catch (_) { return null; }
      })(),
    };
    channel.heldNow += 1;
    channel.resolveCapture(captured);
    await channel.gate;
    try {
      await route.fulfill({ response });
      channel.released += 1;
    } finally {
      channel.heldNow -= 1;
    }
  });
}

async function quiesce(page, tracker, step, tolerated) {
  const allow = tolerated || 0;
  const deadline = Date.now() + QUIESCE_TIMEOUT_MS;
  for (;;) {
    if (tracker.inFlight.size <= allow) {
      const seq = tracker.sequence;
      await page.waitForTimeout(QUIET_WINDOW_MS);
      if (tracker.inFlight.size <= allow && tracker.sequence === seq) return;
    } else {
      await page.waitForTimeout(100);
    }
    if (Date.now() > deadline) {
      fail(`[${step}] page never became quiet (${tracker.inFlight.size} request(s), `
        + `${allow} deliberately held)`);
    }
  }
}

async function waitForReleased(page, channel, before, step) {
  const deadline = Date.now() + 15000;
  while (channel.released === before) {
    if (Date.now() > deadline) {
      fail(`[${step}] ${channel.name} response was released by the test but `
        + `never delivered to the page`);
    }
    await page.waitForTimeout(25);
  }
}

function operationalSelector(cardId) {
  return `[data-operational-card="${cardId}"]`;
}

async function cardSnapshot(page, cardId) {
  return page.evaluate(([id, mutationSelector]) => {
    const root = Array.from(document.querySelectorAll("[data-operational-card]"))
      .find((node) => node.getAttribute("data-operational-card") === id);
    const plain = (value) => JSON.parse(JSON.stringify(value));
    const active = document.activeElement;
    const toastRoot = document.getElementById("toast-root");
    let model = null;
    try { model = typeof readCardState === "function" ? plain(readCardState(id)) : null; }
    catch (_) { model = null; }
    return {
      exists: !!root,
      state: root ? root.getAttribute("data-card-state") : null,
      busy: root ? root.getAttribute("aria-busy") : null,
      role: root ? root.getAttribute("role") : null,
      text: root ? root.textContent.replace(/\s+/g, " ").trim() : "",
      html: root ? root.innerHTML : "",
      mutations: root && mutationSelector
        ? root.querySelectorAll(mutationSelector).length : 0,
      buttons: root ? Array.from(root.querySelectorAll("button")).map((b) => ({
        text: b.textContent.replace(/\s+/g, " ").trim(),
        retry: b.getAttribute("data-card-retry"),
        disabled: b.disabled,
      })) : [],
      retryCount: root
        ? root.querySelectorAll(`[data-card-retry="${id}"]`).length : 0,
      alertCount: root ? root.querySelectorAll('[role="alert"]').length
        + (root.getAttribute("role") === "alert" ? 1 : 0) : 0,
      model,
      generation: typeof cardGenerations === "undefined"
        ? null : (cardGenerations[id] || 0),
      epoch: typeof uiIdentityEpoch === "undefined" ? null : uiIdentityEpoch,
      principal: typeof currentUser === "undefined" || !currentUser
        ? null : currentUser.username,
      tuple: typeof currentCardTuple === "function" ? plain(currentCardTuple()) : null,
      focus: active ? {
        tag: active.tagName,
        id: active.id || "",
        operationalCard: active.closest && active.closest("[data-operational-card]")
          ? active.closest("[data-operational-card]").getAttribute("data-operational-card") : null,
        retry: active.getAttribute ? active.getAttribute("data-card-retry") : null,
        text: (active.textContent || "").replace(/\s+/g, " ").trim(),
      } : null,
      toast: toastRoot && !toastRoot.hidden
        ? toastRoot.textContent.replace(/\s+/g, " ").trim() : "",
    };
  }, [cardId, MUTATION_SELECTORS[cardId] || ""]);
}

async function waitForCardState(page, cardId, state, step) {
  await page.waitForFunction(([id, expected]) => {
    const root = Array.from(document.querySelectorAll("[data-operational-card]"))
      .find((node) => node.getAttribute("data-operational-card") === id);
    return !!root && root.getAttribute("data-card-state") === expected;
  }, [cardId, state], { timeout: 20000 }).catch(async () => {
    fail(`[${step}] ${cardId} never reached ${state}: `
      + JSON.stringify(await cardSnapshot(page, cardId)));
  });
  return cardSnapshot(page, cardId);
}

function coverageLedger() {
  const ledger = new Map(CARD_IDS.map((id) => [id, new Set()]));
  return {
    mark(id, state) { ledger.get(id).add(state); },
    assertComplete(label) {
      const missing = [];
      for (const id of CARD_IDS) {
        for (const state of CARD_STATES) {
          if (!ledger.get(id).has(state)) missing.push(`${id}:${state}`);
        }
      }
      const checked = Array.from(ledger.values())
        .reduce((sum, states) => sum + states.size, 0);
      const expected = CARD_IDS.length * CARD_STATES.length;
      if (checked !== expected || missing.length) {
        fail(`[${label}] state axis shrank: checked ${checked}/${expected}; missing `
          + `${missing.join(", ")}`);
      }
      return checked;
    },
  };
}

async function assertProductionAxes(page, label) {
  const production = await page.evaluate(() => ({
    cards: typeof SCHEDULE_FACILITY_CARD_IDS === "undefined"
      ? null : Array.from(SCHEDULE_FACILITY_CARD_IDS),
    states: typeof CARD_STATE === "undefined" ? null : Object.values(CARD_STATE),
    // The owner called out this exact post-await boundary for a mutation
    // probe. commitCardState() independently repeats the identity check, so
    // the behavioral race below proves the protection as a whole while this
    // structural assertion keeps the Generate entry point's own guard from
    // silently becoming dead documentation.
    generateGuarded: (() => {
      if (typeof generateSchedulerDraft !== "function") return false;
      const source = String(generateSchedulerDraft);
      const request = source.indexOf(
        'await postScoped("/api/scheduler/draft", request)');
      const guard = source.indexOf(
        "if (!cardIdentityCurrent(identity)) return;", request);
      const outcome = source.indexOf("if (result && !result.error)", request);
      return request >= 0 && guard > request && outcome > guard;
    })(),
  }));
  if (JSON.stringify(production.cards) !== JSON.stringify(CARD_IDS)) {
    fail(`[${label}] journey card axis diverged from production: journey `
      + `${JSON.stringify(CARD_IDS)}, production ${JSON.stringify(production.cards)}`);
  }
  const missingStates = CARD_STATES.filter((state) =>
    !production.states || !production.states.includes(state));
  if (missingStates.length) {
    fail(`[${label}] production no longer declares the journey's state axis: `
      + `${missingStates.join(", ")}`);
  }
  if (!production.generateGuarded) {
    fail(`[${label}] Generate must reject a superseded card identity directly `
      + "after its awaited response and before processing either outcome");
  }
}

async function assertState(page, coverage, cardId, state, step, marker) {
  const got = await waitForCardState(page, cardId, state, step);
  if (!got.exists) fail(`[${step}] ${cardId} root is absent`);
  if (got.model && got.model.state !== state) {
    fail(`[${step}] ${cardId} DOM says ${state}, model says ${got.model.state}: `
      + JSON.stringify(got));
  }
  const shouldBusy = state === "loading" || state === "stale";
  if (got.busy !== String(shouldBusy)) {
    fail(`[${step}] ${cardId}/${state} aria-busy=${JSON.stringify(got.busy)}, `
      + `expected ${shouldBusy}`);
  }
  if (state === "loading") {
    if (!/(load|refresh|generat|review|working|waiting)/i.test(got.text)) {
      fail(`[${step}] ${cardId} loading state has no labelled progress text: ${got.text}`);
    }
    if (got.mutations) {
      fail(`[${step}] ${cardId} exposes ${got.mutations} mutation control(s) while loading`);
    }
  } else if (state === "ready") {
    if (marker && !got.text.includes(marker)) {
      fail(`[${step}] ${cardId} READY did not paint non-empty marker `
        + `${JSON.stringify(marker)}: ${got.text}`);
    }
  } else if (state === "empty") {
    if (!/(\bno\b|nothing|not yet|start by|generate|preview)/i.test(got.text)) {
      fail(`[${step}] ${cardId} EMPTY does not explain the absence: ${got.text}`);
    }
  } else if (state === "stale") {
    if (!/(earlier|previous|stale)/i.test(got.text)) {
      fail(`[${step}] ${cardId} STALE is not visibly labelled as earlier data: ${got.text}`);
    }
    if (marker && !got.text.includes(marker)) {
      fail(`[${step}] ${cardId} STALE did not retain its earlier-context marker `
        + `${JSON.stringify(marker)}: ${got.text}`);
    }
    if (got.retryCount !== 1) {
      fail(`[${step}] ${cardId} STALE must offer exactly its own Refresh, got `
        + `${got.retryCount}`);
    }
    if (got.mutations) {
      fail(`[${step}] ${cardId} STALE exposes ${got.mutations} obsolete mutation control(s)`);
    }
  } else if (state === "error") {
    if (!got.alertCount) fail(`[${step}] ${cardId} ERROR has no role=alert`);
    if (got.retryCount !== 1) {
      fail(`[${step}] ${cardId} ERROR must offer exactly its own Retry, got `
        + `${got.retryCount}`);
    }
  }
  coverage.mark(cardId, state);
  return got;
}

async function assertSingleErrorLiveRegion(page, cardId, step) {
  const exposure = await page.evaluate((id) => {
    const root = document.querySelector(`[data-operational-card="${id}"]`);
    const visible = (node) => !!node && !node.hidden
      && getComputedStyle(node).display !== "none"
      && getComputedStyle(node).visibility !== "hidden";
    const text = (node) => (node && node.textContent || "")
      .replace(/\s+/g, " ").trim();
    const alerts = root ? Array.from(root.querySelectorAll('[role="alert"]'))
      .filter(visible).map(text).filter(Boolean) : [];
    const toast = document.getElementById("toast-root");
    return {
      alerts,
      toastExposed: visible(toast),
      toastText: visible(toast) ? text(toast) : "",
    };
  }, cardId);
  if (exposure.alerts.length !== 1) {
    fail(`[${step}] ${cardId} ERROR must expose exactly one card alert: `
      + JSON.stringify(exposure));
  }
  const errorText = exposure.alerts[0];
  const repeatedInToast = exposure.toastExposed && exposure.toastText
    && (exposure.toastText.includes(errorText) || errorText.includes(exposure.toastText));
  const repeatedInSpeech = (await spoken(page)).some((message) => {
    const normalized = String(message || "").replace(/\s+/g, " ").trim();
    return normalized && (normalized.includes(errorText) || errorText.includes(normalized));
  });
  if (repeatedInToast || repeatedInSpeech) {
    fail(`[${step}] ${cardId} ERROR is exposed in both its card alert and the `
      + `sitewide toast region: ${JSON.stringify(Object.assign({}, exposure, {
        speech: await spoken(page),
      }))}`);
  }
}

async function assertErrorRepaintIsSilent(page, cardId, step) {
  const before = await cardSnapshot(page, cardId);
  const speechBefore = await spoken(page);
  await page.evaluate((id) => {
    if (id === "scheduler/draft" || id === "scheduler/review") {
      repaintSchedulerSurface(id);
    } else {
      repaintCalendarSurface(id);
    }
  }, cardId);
  const after = await cardSnapshot(page, cardId);
  const speechAfter = await spoken(page);
  if (after.state !== "error" || after.alertCount !== 0
      || after.text !== before.text || after.toast !== before.toast
      || JSON.stringify(speechAfter) !== JSON.stringify(speechBefore)) {
    fail(`[${step}] stored ERROR was re-announced when its card re-entered `
      + `the DOM: before ${JSON.stringify(before)}, after ${JSON.stringify(after)}, `
      + `speech before ${JSON.stringify(speechBefore)}, speech after `
      + JSON.stringify(speechAfter));
  }
}

async function assertNeutralLoading(page, cardId, step) {
  const got = await waitForCardState(page, cardId, "loading", step);
  if (got.busy !== "true" || got.mutations || got.alertCount
      || /(earlier|previous|stale|couldn't|failed|forced)/i.test(got.text)) {
    fail(`[${step}] payload-less replacement must be neutral LOADING, not `
      + `fabricated STALE/ERROR: ${JSON.stringify(got)}`);
  }
  return got;
}

async function assertSeededDemoChrome(page, step) {
  const observed = await page.evaluate(() => {
    const menu = document.getElementById("demo-menu");
    const button = document.getElementById("demo-btn");
    return {
      envKnown: typeof envStatus !== "undefined" && !!envStatus,
      demoEmpty: typeof envStatus === "undefined" || !envStatus
        ? null : !!envStatus.demo_empty,
      menuHidden: !menu || menu.hidden,
      label: button && button.getAttribute("aria-label"),
    };
  });
  if (!observed.envKnown || observed.demoEmpty || observed.menuHidden
      || observed.label !== "Reset demo data") {
    fail(`[${step}] an operational view rewrote seeded demo chrome as empty: `
      + JSON.stringify(observed));
  }
}

async function activateRetryWithKeyboard(page, cardId, step) {
  const selector = `[data-card-retry="${cardId}"]`;
  const retry = page.locator(selector);
  if (await retry.count() !== 1) {
    fail(`[${step}] expected exactly one ${selector}, got ${await retry.count()}`);
  }
  await retry.focus();
  const focused = await page.evaluate((id) => document.activeElement
    && document.activeElement.getAttribute("data-card-retry") === id, cardId);
  if (!focused) fail(`[${step}] ${cardId} retry is not keyboard focusable`);
  await page.keyboard.press("Enter");
}

async function openView(page, view, cards, step) {
  const tab = page.locator(`.tab[data-tab="${view}"]`).first();
  await tab.waitFor({ state: "visible", timeout: 15000 })
    .catch(() => fail(`[${step}] ${view} tab is not visible`));
  await tab.click();
  await page.waitForFunction((v) => document.body.dataset.view === v,
    view, { timeout: 20000 }).catch(async () => {
    const observed = await page.evaluate(() => ({
      view: document.body.dataset.view || null,
      signedIn: typeof currentUser !== "undefined" && !!currentUser,
      modal: !!document.querySelector(".modal,[role=dialog]"),
    }));
    fail(`[${step}] ${view} tab did not enter its view: ${JSON.stringify(observed)}`);
  });
  for (const cardId of cards) {
    await page.waitForSelector(operationalSelector(cardId), { timeout: 20000 })
      .catch(async () => {
        const observed = await page.evaluate(() => ({
          view: document.body.dataset.view || null,
          cards: Array.from(document.querySelectorAll("[data-operational-card]"))
            .map((node) => node.getAttribute("data-operational-card")),
          content: (document.getElementById("content")?.textContent || "")
            .replace(/\s+/g, " ").trim().slice(0, 600),
        }));
        fail(`[${step}] ${cardId} root did not render: ${JSON.stringify(observed)}`);
      });
  }
}

async function openBuilder(page, step) {
  await openView(page, "calendar", [CALENDAR_CARD], `${step}/calendar`);
  await page.click("[data-ice-builder-open]");
  await page.waitForSelector(operationalSelector(BUILDER_CARD), { timeout: 15000 });
  await page.waitForSelector(".ib-form", { timeout: 15000 });
}

async function configureBuilder(page, rinkId, weekdays, fromDate, toDate) {
  await page.evaluate((wanted) => {
    const set = new Set(wanted);
    const boxes = Array.from(document.querySelectorAll(".ib-weekday"));
    boxes.forEach((box) => { box.checked = set.has(Number(box.value)); });
    if (boxes[0]) boxes[0].dispatchEvent(new Event("change", { bubbles: true }));
  }, weekdays);
  await page.waitForSelector(`.ib-wd-row[data-weekday="${weekdays[0]}"]`, {
    timeout: 10000,
  });
  await page.check(`.ib-rink[value="${rinkId}"]`);
  await page.fill("#ib-from", fromDate);
  await page.fill("#ib-to", toDate);
}

async function startContextSwitch(page, programId, seasonId, step) {
  const started = await page.evaluate(([p, s]) => {
    const select = document.getElementById("ctx-select");
    if (!select) return { ok: false, why: "no #ctx-select" };
    const wanted = `${p}|${s}`;
    if (!Array.from(select.options).some((option) => option.value === wanted)) {
      return { ok: false, why: `no option ${wanted}`,
        offered: Array.from(select.options).map((option) => option.value) };
    }
    select.value = wanted;
    select.dispatchEvent(new Event("change", { bubbles: true }));
    return { ok: true };
  }, [programId, seasonId]);
  if (!started.ok) fail(`[${step}] could not start real context switch: ${JSON.stringify(started)}`);
}

async function waitForSelectedTuple(page, programId, seasonId, requireSettled, step) {
  await page.waitForFunction(([p, s, settled]) => {
    const selected = (typeof contextOptions !== "undefined" && contextOptions
      && contextOptions.selected) || {};
    return selected.program_id === p && selected.season_id === s
      && (!settled || !contextSwitchIntentPending);
  }, [programId, seasonId, !!requireSettled], { timeout: 30000 }).catch(() => {
    fail(`[${step}] context never selected ${programId}/${seasonId}`);
  });
}

async function assertContextChrome(page, expected, step) {
  const observed = await page.evaluate(() => {
    const select = document.getElementById("ctx-select");
    const option = select && select.selectedOptions && select.selectedOptions[0];
    const group = option && option.closest("optgroup");
    const breadcrumb = document.getElementById("breadcrumb");
    return {
      value: select && select.value,
      option: option && option.textContent.trim(),
      group: group && group.getAttribute("label"),
      breadcrumb: breadcrumb && breadcrumb.textContent.replace(/\s+/g, " ").trim(),
    };
  });
  if (observed.value !== `${expected.programId}|${expected.seasonId}`
      || observed.option !== expected.seasonName
      || observed.group !== expected.programName
      || !observed.breadcrumb.includes(expected.programName)
      || !observed.breadcrumb.includes(expected.seasonName)) {
    fail(`[${step}] selector and breadcrumb do not both identify the accepted `
      + `context: expected ${JSON.stringify(expected)}, observed `
      + JSON.stringify(observed));
  }
  return observed;
}

async function armAnnouncements(page) {
  await page.evaluate(() => {
    const root = document.getElementById("toast-root");
    if (!root) throw new Error("missing #toast-root");
    window.__schedulerMatrixSpeech = [];
    if (window.__schedulerMatrixSpeechObserver) {
      window.__schedulerMatrixSpeechObserver.disconnect();
    }
    window.__schedulerMatrixSpeechObserver = new MutationObserver((records) => {
      for (const record of records) {
        for (const node of record.addedNodes || []) {
          if (!node || node.nodeType !== 1) continue;
          const message = node.classList && node.classList.contains("toast-msg")
            ? node : (node.querySelector ? node.querySelector(".toast-msg") : null);
          if (message) window.__schedulerMatrixSpeech.push(message.textContent.trim());
        }
      }
    });
    window.__schedulerMatrixSpeechObserver.observe(root, {
      childList: true, subtree: true, characterData: true,
    });
  });
}

async function resetAnnouncements(page) {
  await page.evaluate(() => { window.__schedulerMatrixSpeech = []; });
}

async function spoken(page) {
  return page.evaluate(() => (window.__schedulerMatrixSpeech || []).slice());
}

async function immutableSnapshot(page, cardId) {
  const snapshot = await cardSnapshot(page, cardId);
  return {
    state: snapshot.state,
    busy: snapshot.busy,
    text: snapshot.text,
    html: snapshot.html,
    model: snapshot.model,
    generation: snapshot.generation,
    focus: snapshot.focus,
    toast: snapshot.toast,
    speech: await spoken(page),
  };
}

function assertByteEqual(step, before, after) {
  if (JSON.stringify(before) !== JSON.stringify(after)) {
    fail(`[${step}] stale response mutated the current surface\nBEFORE `
      + `${JSON.stringify(before)}\nAFTER  ${JSON.stringify(after)}`);
  }
}

async function seedFixtures(page) {
  const ids = await page.evaluate(async () => {
    const F = window.hsFixture;
    const pa = await F.create("matrix Program A", "/api/setup/league", {
      name: "Matrix Program A", timezone: "UTC",
    });
    await F.selectProgram("select matrix Program A", pa.id);
    const sa = await F.create("matrix Season A", "/api/setup/season", {
      league_id: pa.id,
      name: "Matrix Season A",
      start_date: "2027-09-01",
      end_date: "2028-04-30",
    });
    await F.selectProgramSeason("select matrix Program A / Season A", pa.id, sa.id);
    const league = await F.create("matrix competition League", "/api/setup/level", {
      season_id: sa.id, name: "Matrix Bronze",
    });
    const readyDiv = await F.create("matrix ready Division", "/api/setup/division", {
      season_id: sa.id, level_id: league.id, name: "Matrix Ready Division",
    });
    const raceDiv = await F.create("matrix race Division", "/api/setup/division", {
      season_id: sa.id, level_id: league.id, name: "Matrix Race Division",
    });
    const club = await F.create("matrix Club", "/api/setup/club", {
      name: "Matrix Club",
    });
    const makeTeam = async (name, divisionId) => {
      const team = await F.create(`team ${name}`, "/api/v2/setup/team", {
        club_id: club.id, league_id: league.id, name,
      });
      await F.call(`register ${name}`, `/api/setup/seasons/${sa.id}/team-registrations`, {
        team_id: team.id, division_id: divisionId,
      });
      return team.id;
    };
    await makeTeam("Matrix A Ready 1", readyDiv.id);
    await makeTeam("Matrix A Ready 2", readyDiv.id);
    await makeTeam("Matrix A Race 1", raceDiv.id);
    await makeTeam("Matrix A Race 2", raceDiv.id);
    const venue = await F.create("matrix Arena", "/api/setup/venue", {
      name: "Matrix A Arena", league_id: pa.id,
    });
    await F.call("grant matrix Arena", `/api/v2/setup/seasons/${sa.id}/venue-access`, {
      venue_id: venue.id,
    });
    const rink = await F.create("matrix rink", "/api/setup/rink", {
      venue_id: venue.id, name: "Matrix A Ice",
    });
    for (const day of ["2027-10-05", "2027-10-07", "2027-10-12", "2027-10-14"]) {
      await F.call(`ice ${day}`, "/api/setup/ice-slot", {
        rink_id: rink.id,
        start_time: `${day}T18:00:00+00:00`,
        end_time: `${day}T19:00:00+00:00`,
        slot_type: "game",
      });
    }

    const pb = await F.create("matrix Program B", "/api/setup/league", {
      name: "Matrix Program B", timezone: "UTC",
    });
    await F.selectProgram("select matrix Program B", pb.id);
    const sb = await F.create("matrix Season B", "/api/setup/season", {
      league_id: pb.id,
      name: "Matrix Season B",
      start_date: "2027-09-01",
      end_date: "2028-04-30",
    });
    await F.selectProgramSeason("select matrix Program B / Season B", pb.id, sb.id);
    await F.selectProgramSeason("restore matrix Program A / Season A", pa.id, sa.id);
    return {
      pa: pa.id, sa: sa.id, pb: pb.id, sb: sb.id,
      readyDiv: readyDiv.id, raceDiv: raceDiv.id, rink: rink.id,
    };
  });

  return ids;
}

async function checkViewport(browser, viewport) {
  const label = viewport.label;
  const base = `http://${HOST}:${viewport.port}`;
  const server = spawn(
    process.env.PYTHON || "python3",
    ["-u", "-m", "hockey_scheduler.web.server", "--host", HOST,
      "--port", String(viewport.port)],
    { cwd: BACKEND_DIR, stdio: ["ignore", "pipe", "pipe"] });
  let serverOutput = "";
  server.stdout.on("data", (data) => { serverOutput += data.toString(); });
  server.stderr.on("data", (data) => { serverOutput += data.toString(); });

  const context = await browser.newContext({ viewport: {
    width: viewport.width, height: viewport.height,
  } });
  const page = await context.newPage();
  const tracker = { inFlight: new Set(), sequence: 0 };
  const nonOk = [];
  const requestFailures = [];
  const consoleErrors = [];
  page.on("request", (request) => {
    tracker.inFlight.add(request);
    tracker.sequence += 1;
  });
  page.on("response", (response) => {
    tracker.inFlight.delete(response.request());
    if (response.status() >= 400) nonOk.push({
      method: response.request().method(), url: response.url(), status: response.status(),
    });
  });
  page.on("requestfailed", (request) => {
    tracker.inFlight.delete(request);
    requestFailures.push({ method: request.method(), url: request.url(),
      failure: request.failure() && request.failure().errorText });
  });
  page.on("pageerror", (error) => consoleErrors.push({
    text: `[pageerror] ${error.message}`, url: "",
  }));
  page.on("console", (message) => {
    if (message.type() !== "error") return;
    consoleErrors.push({ text: message.text(),
      url: (message.location() && message.location().url) || "" });
  });

  const channels = {
    draft: makeChannel("scheduler draft", DRAFT_RE),
    draftCommit: makeChannel("scheduler commit", DRAFT_COMMIT_RE),
    drafts: makeChannel("scheduler drafts", DRAFTS_RE),
    publish: makeChannel("scheduler publish", PUBLISH_RE),
    ice: makeChannel("ice preview", ICE_PREVIEW_RE),
    iceCommit: makeChannel("ice commit", ICE_COMMIT_RE),
    overview: makeChannel("calendar overview", OVERVIEW_RE),
    options: makeChannel("context options", CONTEXT_OPTIONS_RE),
    context: makeChannel("context switch", CONTEXT_RE),
  };
  const coverage = coverageLedger();

  try {
    for (const channel of Object.values(channels)) await installChannel(page, channel);
    await waitForServer(`${base}/api/health`, READY_TIMEOUT_MS);
    await page.goto(base, { waitUntil: "domcontentloaded" });
    await page.waitForSelector("#content > *", { timeout: 15000 });
    await installContextFixture(page);
    const ids = await seedFixtures(page);
    await page.reload({ waitUntil: "domcontentloaded" });
    await page.waitForSelector("#content > *", { timeout: 15000 });
    await installContextFixture(page);
    await armAnnouncements(page);
    await quiesce(page, tracker, `${label}/boot`);
    await assertProductionAxes(page, `${label}/axes`);

    // Scheduler starts honestly empty: no proposal has been generated and no
    // draft Game has been committed.
    trace(`${label}: scheduler EMPTY, ERROR, LOADING and READY`);
    await openView(page, "scheduler", [DRAFT_CARD, REVIEW_CARD], `${label}/scheduler`);
    await quiesce(page, tracker, `${label}/scheduler-initial`);
    await assertSeededDemoChrome(page, `${label}/scheduler-demo-chrome`);
    await assertState(page, coverage, DRAFT_CARD, "empty",
      `${label}/draft-empty`);
    await assertState(page, coverage, REVIEW_CARD, "empty",
      `${label}/review-empty`);

    // Create an operation failure under A so its shipped Retry drives the
    // ERROR -> LOADING -> READY state axis below.
    await page.selectOption("#sched-div", ids.readyDiv);
    await resetAnnouncements(page);
    failOnce(channels.draft);
    await page.click("[data-sched-generate]");
    await assertState(page, coverage, DRAFT_CARD, "error",
      `${label}/draft-error-retryable`);
    await assertSingleErrorLiveRegion(page, DRAFT_CARD,
      `${label}/draft-error-retryable-announcement`);
    await assertErrorRepaintIsSilent(page, DRAFT_CARD,
      `${label}/draft-error-reentry`);
    const draftRetry = armHold(channels.draft);
    await activateRetryWithKeyboard(page, DRAFT_CARD, `${label}/draft-retry`);
    const draftReadyResponse = await draftRetry.captured;
    if (draftReadyResponse.status !== 200
        || !draftReadyResponse.body
        || !(draftReadyResponse.body.draft_games || []).length) {
      fail(`[${label}/draft-loading] held Generate was not a real non-empty success: `
        + JSON.stringify(draftReadyResponse));
    }
    await assertState(page, coverage, DRAFT_CARD, "loading", `${label}/draft-loading`);
    const draftReleased = channels.draft.released;
    draftRetry.release();
    await waitForCardState(page, DRAFT_CARD, "ready", `${label}/draft-ready`);
    await waitForReleased(page, channels.draft, draftReleased, `${label}/draft-ready`);
    await assertState(page, coverage, DRAFT_CARD, "ready", `${label}/draft-ready`,
      "Matrix A Ready");

    // A view change is not a context or identity change. The legacy Scheduler
    // retained its uncommitted proposal across navigation; putting the payload
    // on a card must preserve that behavior rather than letting the card's
    // same-tuple overview refresh redefine a real proposal as EMPTY.
    trace(`${label}: same-tuple navigation preserves generated Draft`);
    await openView(page, "calendar", [CALENDAR_CARD],
      `${label}/draft-navigation-leave`);
    await quiesce(page, tracker, `${label}/draft-navigation-leave`);
    await openView(page, "scheduler", [DRAFT_CARD, REVIEW_CARD],
      `${label}/draft-navigation-return`);
    await quiesce(page, tracker, `${label}/draft-navigation-return`);
    const retainedDraft = await cardSnapshot(page, DRAFT_CARD);
    if (retainedDraft.state !== "ready"
        || !retainedDraft.text.includes("Matrix A Ready")) {
      fail(`[${label}/draft-navigation] same-tuple navigation discarded the `
        + `generated proposal: ${JSON.stringify(retainedDraft)}`);
    }

    // A second same-tuple refresh can start before the first settles. At that
    // point readCardState() is LOADING and the proposal lives in `retained`,
    // not directly on the outer model. The newer refresh must preserve it and
    // the older response must lose without clearing it when finally delivered.
    trace(`${label}: overlapping same-tuple refresh preserves generated Draft`);
    await openView(page, "calendar", [CALENDAR_CARD],
      `${label}/draft-overlap-leave`);
    await quiesce(page, tracker, `${label}/draft-overlap-leave`);
    const olderDraftRefresh = armHold(channels.overview);
    await openView(page, "scheduler", [DRAFT_CARD, REVIEW_CARD],
      `${label}/draft-overlap-first`);
    const olderOverview = await olderDraftRefresh.captured;
    if (olderOverview.status !== 200 || !olderOverview.body
        || olderOverview.body.league.id !== ids.pa) {
      fail(`[${label}/draft-overlap] held refresh was not a real A overview: `
        + JSON.stringify(olderOverview));
    }
    await waitForCardState(page, DRAFT_CARD, "loading",
      `${label}/draft-overlap-loading`);
    await openView(page, "scheduler", [DRAFT_CARD, REVIEW_CARD],
      `${label}/draft-overlap-second`);
    await quiesce(page, tracker, `${label}/draft-overlap-second`, 1);
    const overlapWinner = await cardSnapshot(page, DRAFT_CARD);
    if (overlapWinner.state !== "ready"
        || !overlapWinner.text.includes("Matrix A Ready")) {
      fail(`[${label}/draft-overlap] newer same-tuple refresh discarded the `
        + `retained proposal: ${JSON.stringify(overlapWinner)}`);
    }
    const beforeOlderDraftRelease = await immutableSnapshot(page, DRAFT_CARD);
    const olderDraftReleased = channels.overview.released;
    olderDraftRefresh.release();
    await waitForReleased(page, channels.overview, olderDraftReleased,
      `${label}/draft-overlap-release`);
    await quiesce(page, tracker, `${label}/draft-overlap-release`);
    const afterOlderDraftRelease = await immutableSnapshot(page, DRAFT_CARD);
    assertByteEqual(`${label}/draft-overlap`, beforeOlderDraftRelease,
      afterOlderDraftRelease);

    // The same-tuple preservation is conditional, not a blanket revival of
    // any READY payload. Change to another still-offered Division through the
    // shipped selector, refresh the Scheduler inputs, and prove the proposal
    // for readyDiv is discarded under raceDiv. Then create a fresh readyDiv
    // proposal for the commit/review setup below.
    trace(`${label}: changed Division invalidates retained Draft`);
    await page.selectOption("#sched-div", ids.raceDiv);
    await openView(page, "calendar", [CALENDAR_CARD],
      `${label}/draft-division-leave`);
    await quiesce(page, tracker, `${label}/draft-division-leave`);
    await openView(page, "scheduler", [DRAFT_CARD, REVIEW_CARD],
      `${label}/draft-division-return`);
    await quiesce(page, tracker, `${label}/draft-division-return`);
    const invalidDivisionDraft = await cardSnapshot(page, DRAFT_CARD);
    const selectedDivision = await page.inputValue("#sched-div");
    if (invalidDivisionDraft.state !== "empty"
        || invalidDivisionDraft.text.includes("Matrix A Ready")
        || selectedDivision !== ids.raceDiv) {
      fail(`[${label}/draft-division] proposal survived under a different `
        + `selected Division: ${JSON.stringify({
          selectedDivision, invalidDivisionDraft,
        })}`);
    }
    await page.selectOption("#sched-div", ids.readyDiv);
    await page.click("[data-sched-generate]");
    await waitForCardState(page, DRAFT_CARD, "ready",
      `${label}/draft-division-regenerate`);
    await assertState(page, coverage, DRAFT_CARD, "ready",
      `${label}/draft-division-regenerate`, "Matrix A Ready");

    // The commit branch is a distinct operation failure from Generate. Pin
    // the same one-alert/no-toast contract, then use its shipped Retry to
    // regenerate before the successful commit that seeds Review.
    await resetAnnouncements(page);
    failOnce(channels.draftCommit);
    await page.click("[data-sched-commit]");
    await waitForCardState(page, DRAFT_CARD, "error",
      `${label}/draft-commit-error`);
    await assertSingleErrorLiveRegion(page, DRAFT_CARD,
      `${label}/draft-commit-error-announcement`);
    await assertErrorRepaintIsSilent(page, DRAFT_CARD,
      `${label}/draft-commit-error-reentry`);
    await activateRetryWithKeyboard(page, DRAFT_CARD,
      `${label}/draft-commit-retry`);
    await waitForCardState(page, DRAFT_CARD, "ready",
      `${label}/draft-commit-regenerated`);
    await quiesce(page, tracker, `${label}/draft-commit-regenerated`);
    await assertState(page, coverage, DRAFT_CARD, "ready",
      `${label}/draft-commit-regenerated`, "Matrix A Ready");

    // Commit the regenerated proposal through the shipped button so the
    // review card has authoritative, non-empty server data.
    await page.click("[data-sched-commit]");
    await waitForCardState(page, REVIEW_CARD, "ready", `${label}/review-ready`);
    await assertState(page, coverage, REVIEW_CARD, "ready", `${label}/review-ready`,
      "Matrix A Ready");

    // A Review checkbox is local interaction state, not durable context data.
    // Select one row, leave Scheduler, and queue A -> B -> A while B's context
    // echo is withheld. No B review response can overwrite the old A model.
    // When the final A read settles, the invalidated pre-round-trip selection
    // must not revive merely because tuple and principal compare equal again.
    trace(`${label}: Review selection cannot survive A → B → A`);
    const firstReviewPick = page.locator(".sched-pick").first();
    await firstReviewPick.check();
    const selectedBeforeRoundTrip = await page.evaluate(() => ({
      checked: document.querySelectorAll(".sched-pick:checked").length,
      publishDisabled: document.querySelector("[data-sched-publish]")?.disabled,
    }));
    if (selectedBeforeRoundTrip.checked !== 1
        || selectedBeforeRoundTrip.publishDisabled !== false) {
      fail(`[${label}/review-selection] shipped checkbox did not establish a `
        + `live selection: ${JSON.stringify(selectedBeforeRoundTrip)}`);
    }
    await openView(page, "dashboard", [],
      `${label}/review-selection-dashboard`);
    await quiesce(page, tracker, `${label}/review-selection-dashboard`);
    const reviewRoundTripB = armHold(channels.context);
    await startContextSwitch(page, ids.pb, ids.sb,
      `${label}/review-selection-a-to-b`);
    const reviewRoundTripEcho = await reviewRoundTripB.captured;
    if (reviewRoundTripEcho.status !== 200 || !reviewRoundTripEcho.body
        || reviewRoundTripEcho.body.program_id !== ids.pb
        || reviewRoundTripEcho.body.season_id !== ids.sb) {
      fail(`[${label}/review-selection] held B switch was not successful: `
        + JSON.stringify(reviewRoundTripEcho));
    }
    await startContextSwitch(page, ids.pa, ids.sa,
      `${label}/review-selection-b-to-a`);
    reviewRoundTripB.release();
    await waitForSelectedTuple(page, ids.pa, ids.sa, true,
      `${label}/review-selection-a-settled`);
    await openView(page, "scheduler", [DRAFT_CARD, REVIEW_CARD],
      `${label}/review-selection-return`);
    await waitForCardState(page, REVIEW_CARD, "ready",
      `${label}/review-selection-ready`);
    await quiesce(page, tracker, `${label}/review-selection-ready`);
    const selectedAfterRoundTrip = await page.evaluate(() => ({
      checked: document.querySelectorAll(".sched-pick:checked").length,
      publishDisabled: document.querySelector("[data-sched-publish]")?.disabled,
    }));
    if (selectedAfterRoundTrip.checked !== 0
        || selectedAfterRoundTrip.publishDisabled !== true) {
      fail(`[${label}/review-selection] invalidated A selection revived after `
        + `A -> B -> A: ${JSON.stringify(selectedAfterRoundTrip)}`);
    }

    await openView(page, "calendar", [CALENDAR_CARD], `${label}/leave-scheduler`);
    await resetAnnouncements(page);
    failOnce(channels.drafts);
    await openView(page, "scheduler", [DRAFT_CARD, REVIEW_CARD], `${label}/review-error-open`);
    await assertState(page, coverage, REVIEW_CARD, "error", `${label}/review-error`);
    await assertSingleErrorLiveRegion(page, REVIEW_CARD,
      `${label}/review-error-announcement`);
    await assertErrorRepaintIsSilent(page, REVIEW_CARD,
      `${label}/review-error-reentry`);
    const reviewRetry = armHold(channels.drafts);
    await activateRetryWithKeyboard(page, REVIEW_CARD, `${label}/review-retry`);
    const reviewPayload = await reviewRetry.captured;
    if (reviewPayload.status !== 200 || !reviewPayload.body
        || !(reviewPayload.body.draft_games || []).length) {
      fail(`[${label}/review-loading] held drafts read was not non-empty: `
        + JSON.stringify(reviewPayload));
    }
    await assertState(page, coverage, REVIEW_CARD, "loading", `${label}/review-loading`);
    const reviewReleased = channels.drafts.released;
    reviewRetry.release();
    await waitForCardState(page, REVIEW_CARD, "ready", `${label}/review-recovered`);
    await waitForReleased(page, channels.drafts, reviewReleased, `${label}/review-recovered`);

    // Generate an uncommitted proposal for the second Division.  This leaves
    // both Scheduler cards READY for the shared A -> B stale transition.
    await page.selectOption("#sched-div", ids.raceDiv);
    await page.click("[data-sched-generate]");
    await waitForCardState(page, DRAFT_CARD, "ready", `${label}/race-preview`);
    // Both Scheduler cards start their own read for B. Hold both computed
    // responses so the assertion observes the shared STALE interval rather
    // than depending on one localhost response losing a timing race.
    const heldBDraft = armHold(channels.overview);
    const heldBReview = armHold(channels.drafts);
    await startContextSwitch(page, ids.pb, ids.sb, `${label}/scheduler-stale-switch`);
    const [draftBPayload, reviewBPayload] = await Promise.all([
      heldBDraft.captured, heldBReview.captured,
    ]);
    if (draftBPayload.status !== 200 || reviewBPayload.status !== 200) {
      fail(`[${label}/scheduler-stale] B replacement reads were not successful: `
        + JSON.stringify({ draftBPayload, reviewBPayload }));
    }
    await waitForSelectedTuple(page, ids.pb, ids.sb, true,
      `${label}/scheduler-stale-selected`);
    await assertState(page, coverage, DRAFT_CARD, "stale", `${label}/draft-stale`,
      "Matrix A Race");
    await assertState(page, coverage, REVIEW_CARD, "stale", `${label}/review-stale`,
      "Matrix A Ready");
    await assertContextChrome(page, {
      programId: ids.pb, seasonId: ids.sb,
      programName: "Matrix Program B", seasonName: "Matrix Season B",
    }, `${label}/scheduler-stale-chrome`);
    const draftStaleRefresh = page.locator(
      `[data-card-retry="${DRAFT_CARD}"]`);
    if (await draftStaleRefresh.count() !== 1) {
      fail(`[${label}/scheduler-stale-focus] Draft STALE has no unique Refresh`);
    }
    await draftStaleRefresh.focus();
    const draftBReleased = channels.overview.released;
    const reviewBReleased = channels.drafts.released;
    heldBDraft.release();
    heldBReview.release();
    await waitForCardState(page, DRAFT_CARD, "empty", `${label}/draft-b-empty`);
    await waitForCardState(page, REVIEW_CARD, "empty", `${label}/review-b-empty`);
    await waitForReleased(page, channels.overview, draftBReleased,
      `${label}/draft-b-empty`);
    await waitForReleased(page, channels.drafts, reviewBReleased,
      `${label}/review-b-empty`);
    await assertContextChrome(page, {
      programId: ids.pb, seasonId: ids.sb,
      programName: "Matrix Program B", seasonName: "Matrix Season B",
    }, `${label}/scheduler-settled-chrome`);
    const settledDraftFocus = await page.evaluate((cardId) => {
      const active = document.activeElement;
      const owner = active && active.closest
        ? active.closest("[data-operational-card]") : null;
      return {
        tag: active && active.tagName,
        card: owner && owner.getAttribute("data-operational-card"),
        retry: active && active.getAttribute
          ? active.getAttribute("data-card-retry") : null,
        emptyLead: !!(active && active.classList
          && active.classList.contains("sched-empty-lead")),
      };
    }, DRAFT_CARD);
    if (settledDraftFocus.tag === "BODY"
        || settledDraftFocus.card !== DRAFT_CARD
        || (!settledDraftFocus.emptyLead
          && settledDraftFocus.retry !== DRAFT_CARD)) {
      fail(`[${label}/scheduler-stale-focus] replacing B data lost Draft's `
        + `same-card semantic focus: ${JSON.stringify(settledDraftFocus)}`);
    }
    await selectProgramSeason(page, `${label}: return to A`, ids.pa, ids.sa);
    await openView(page, "scheduler", [DRAFT_CARD, REVIEW_CARD], `${label}/scheduler-a-return`);
    await waitForCardState(page, REVIEW_CARD, "ready", `${label}/review-a-return-ready`);

    // Calendar: A has an authoritative rink/ice row, B has none.  Failure and
    // retry are scoped to this card rather than replacing the whole surface.
    trace(`${label}: calendar EMPTY, ERROR, LOADING, READY and STALE`);
    await openView(page, "scheduler", [DRAFT_CARD, REVIEW_CARD], `${label}/calendar-from`);
    // Scheduler Draft consumes the same overview endpoint as Calendar. Let
    // its own load settle before arming the one-shot failure so the injection
    // is deterministically Calendar's, never a late sibling request.
    await quiesce(page, tracker, `${label}/calendar-from`);
    await resetAnnouncements(page);
    failOnce(channels.overview);
    await openView(page, "calendar", [CALENDAR_CARD], `${label}/calendar-error-open`);
    await assertState(page, coverage, CALENDAR_CARD, "error", `${label}/calendar-error`);
    await assertSeededDemoChrome(page, `${label}/calendar-demo-chrome`);
    await assertSingleErrorLiveRegion(page, CALENDAR_CARD,
      `${label}/calendar-error-announcement`);
    await assertErrorRepaintIsSilent(page, CALENDAR_CARD,
      `${label}/calendar-error-reentry`);
    const calendarRetry = armHold(channels.overview);
    await activateRetryWithKeyboard(page, CALENDAR_CARD, `${label}/calendar-retry`);
    const calendarPayload = await calendarRetry.captured;
    if (calendarPayload.status !== 200 || !calendarPayload.body
        || !(calendarPayload.body.ice_slots || []).length) {
      fail(`[${label}/calendar-loading] held board read was not non-empty: `
        + JSON.stringify(calendarPayload));
    }
    await assertState(page, coverage, CALENDAR_CARD, "loading", `${label}/calendar-loading`);
    const calendarReleased = channels.overview.released;
    calendarRetry.release();
    await waitForCardState(page, CALENDAR_CARD, "ready", `${label}/calendar-ready`);
    await waitForReleased(page, channels.overview, calendarReleased, `${label}/calendar-ready`);
    await assertState(page, coverage, CALENDAR_CARD, "ready", `${label}/calendar-ready`,
      "Matrix A Ice");

    const calendarB = armHold(channels.overview);
    await startContextSwitch(page, ids.pb, ids.sb, `${label}/calendar-stale-switch`);
    await calendarB.captured;
    await waitForSelectedTuple(page, ids.pb, ids.sb, true,
      `${label}/calendar-stale-selected`);
    await assertState(page, coverage, CALENDAR_CARD, "stale", `${label}/calendar-stale`,
      "Matrix A Ice");
    await assertContextChrome(page, {
      programId: ids.pb, seasonId: ids.sb,
      programName: "Matrix Program B", seasonName: "Matrix Season B",
    }, `${label}/calendar-stale-chrome`);
    calendarB.release();
    await waitForCardState(page, CALENDAR_CARD, "empty", `${label}/calendar-empty`);
    await assertState(page, coverage, CALENDAR_CARD, "empty", `${label}/calendar-empty`);
    await assertContextChrome(page, {
      programId: ids.pb, seasonId: ids.sb,
      programName: "Matrix Program B", seasonName: "Matrix Season B",
    }, `${label}/calendar-settled-chrome`);
    await selectProgramSeason(page, `${label}: calendar return to A`, ids.pa, ids.sa);

    // Ice Builder: opening a fresh builder is the explicit "no preview yet"
    // EMPTY state. A successful zero-slot preview is still reviewed data (it
    // may explain duplicates/conflicts), so it correctly belongs to READY and
    // is not fabricated into a second meaning for EMPTY merely for coverage.
    trace(`${label}: Ice Builder EMPTY, ERROR, LOADING, READY and STALE`);
    await openBuilder(page, `${label}/builder`);
    await assertState(page, coverage, BUILDER_CARD, "empty", `${label}/builder-empty`);
    await configureBuilder(page, ids.rink, [1], "2027-10-05", "2027-10-05");
    await resetAnnouncements(page);
    failOnce(channels.ice);
    await page.click("[data-ib-preview]");
    await assertState(page, coverage, BUILDER_CARD, "error", `${label}/builder-error`);
    await assertSingleErrorLiveRegion(page, BUILDER_CARD,
      `${label}/builder-error-announcement`);
    await assertErrorRepaintIsSilent(page, BUILDER_CARD,
      `${label}/builder-error-reentry`);
    const builderRetry = armHold(channels.ice);
    await activateRetryWithKeyboard(page, BUILDER_CARD, `${label}/builder-retry`);
    const builderPayload = await builderRetry.captured;
    if (builderPayload.status !== 200 || !builderPayload.body
        || !builderPayload.body.totals || builderPayload.body.totals.new < 1) {
      fail(`[${label}/builder-loading] held preview was not non-empty: `
        + JSON.stringify(builderPayload));
    }
    await assertState(page, coverage, BUILDER_CARD, "loading", `${label}/builder-loading`);
    const builderReleased = channels.ice.released;
    builderRetry.release();
    await waitForCardState(page, BUILDER_CARD, "ready", `${label}/builder-ready`);
    await waitForReleased(page, channels.ice, builderReleased, `${label}/builder-ready`);
    await assertState(page, coverage, BUILDER_CARD, "ready", `${label}/builder-ready`,
      "Matrix A Ice");

    // Ice Commit owns its own failure branch. A generic 500 must be exposed
    // only through the card alert; reload its options through the scoped Retry
    // and rebuild the preview so the STALE axis below still begins with real
    // reviewed data.
    await resetAnnouncements(page);
    failOnce(channels.iceCommit);
    await page.click("[data-ib-commit]");
    await waitForCardState(page, BUILDER_CARD, "error",
      `${label}/builder-commit-error`);
    await assertSingleErrorLiveRegion(page, BUILDER_CARD,
      `${label}/builder-commit-error-announcement`);
    await assertErrorRepaintIsSilent(page, BUILDER_CARD,
      `${label}/builder-commit-error-reentry`);
    await activateRetryWithKeyboard(page, BUILDER_CARD,
      `${label}/builder-commit-retry`);
    await waitForCardState(page, BUILDER_CARD, "empty",
      `${label}/builder-commit-options`);
    await quiesce(page, tracker, `${label}/builder-commit-options`);
    await page.click("[data-ib-preview]");
    await waitForCardState(page, BUILDER_CARD, "ready",
      `${label}/builder-commit-preview`);
    await assertState(page, coverage, BUILDER_CARD, "ready",
      `${label}/builder-commit-preview`, "Matrix A Ice");

    // Calendar and the open Builder independently own reads of the same
    // overview route, launched in that order. Let Calendar's sibling request
    // pass and hold Builder's own computed response; holding the first can
    // serialize the browser's identical GETs and never let the second start.
    const builderB = armHoldAfter(channels.overview, 1);
    await startContextSwitch(page, ids.pb, ids.sb, `${label}/builder-stale-switch`);
    const builderBPayload = await builderB.captured;
    if (builderBPayload.status !== 200) {
      fail(`[${label}/builder-stale] B replacement overview was not successful: `
        + JSON.stringify(builderBPayload));
    }
    await waitForSelectedTuple(page, ids.pb, ids.sb, true,
      `${label}/builder-stale-selected`);
    await assertState(page, coverage, BUILDER_CARD, "stale", `${label}/builder-stale`,
      "Matrix A Ice");
    await page.focus("#ctx-select");
    const builderBReleased = channels.overview.released;
    builderB.release();
    await waitForReleased(page, channels.overview, builderBReleased,
      `${label}/builder-stale-release`);
    await quiesce(page, tracker, `${label}/builder-stale-settled`);
    const builderSettlementFocus = await page.evaluate(() => ({
      id: document.activeElement && document.activeElement.id,
      tag: document.activeElement && document.activeElement.tagName,
    }));
    if (builderSettlementFocus.id !== "ctx-select") {
      fail(`[${label}/builder-stale-focus] automatic Builder settlement stole `
        + `focus from the context selector: ${JSON.stringify(builderSettlementFocus)}`);
    }
    await selectProgramSeason(page, `${label}: builder return to A`, ids.pa, ids.sa);

    // The context POST has already mutated server selection when route.fetch()
    // returns, but the browser has not accepted its response yet. Navigating to
    // Scheduler inside that gap must not launch reads: the server would answer
    // them from B while beginCardRequest still labels them A. Preserve the last
    // A model and generation byte-for-byte until the held B echo is delivered.
    trace(`${label}: pending context echo cannot seed an A card with B data`);
    await openView(page, "scheduler", [DRAFT_CARD, REVIEW_CARD],
      `${label}/pending-echo-seed`);
    await quiesce(page, tracker, `${label}/pending-echo-seed`);
    await page.selectOption("#sched-div", ids.raceDiv);
    await page.click("[data-sched-generate]");
    await waitForCardState(page, DRAFT_CARD, "ready",
      `${label}/pending-echo-ready`);
    const beforePendingEcho = await cardSnapshot(page, DRAFT_CARD);
    await openView(page, "calendar", [CALENDAR_CARD],
      `${label}/pending-echo-leave`);
    await quiesce(page, tracker, `${label}/pending-echo-leave`);
    const pendingContextEcho = armHold(channels.context);
    await startContextSwitch(page, ids.pb, ids.sb,
      `${label}/pending-echo-switch`);
    const computedPendingEcho = await pendingContextEcho.captured;
    if (computedPendingEcho.status !== 200 || !computedPendingEcho.body
        || computedPendingEcho.body.program_id !== ids.pb
        || computedPendingEcho.body.season_id !== ids.sb) {
      fail(`[${label}/pending-echo] held switch was not a real successful B `
        + `selection: ${JSON.stringify(computedPendingEcho)}`);
    }
    await openView(page, "scheduler", [DRAFT_CARD, REVIEW_CARD],
      `${label}/pending-echo-open`);
    await quiesce(page, tracker, `${label}/pending-echo-open`, 1);
    const insidePendingEcho = await cardSnapshot(page, DRAFT_CARD);
    if (insidePendingEcho.generation !== beforePendingEcho.generation
        || JSON.stringify(insidePendingEcho.model)
          !== JSON.stringify(beforePendingEcho.model)
        || JSON.stringify(insidePendingEcho.model).includes(ids.pb)
        || JSON.stringify(insidePendingEcho.model).includes("Matrix Program B")) {
      fail(`[${label}/pending-echo] server-B data committed under the still-A `
        + `card identity: before ${JSON.stringify(beforePendingEcho)}, inside `
        + JSON.stringify(insidePendingEcho));
    }
    const pendingEchoReleased = channels.context.released;
    pendingContextEcho.release();
    await waitForReleased(page, channels.context, pendingEchoReleased,
      `${label}/pending-echo-release`);
    await waitForSelectedTuple(page, ids.pb, ids.sb, true,
      `${label}/pending-echo-b-settled`);
    await quiesce(page, tracker, `${label}/pending-echo-b-settled`);
    await selectProgramSeason(page, `${label}: pending echo return to A`,
      ids.pa, ids.sa);

    // A context POST mutates the server before its response reaches this app.
    // Hold both sides of that interval: a real A Generate response that has
    // already been computed, and B's successful context echo after B commits.
    // Releasing Generate while the browser still displays canonical A must be
    // a byte-for-byte no-op; accepting B afterwards must never reveal the old
    // A proposal. The refused-switch control below proves this provisional
    // invalidation does not discard the response when the tuple never moves.
    trace(`${label}: held Generate cannot settle inside accepted-context echo gap`);
    await openView(page, "scheduler", [DRAFT_CARD, REVIEW_CARD],
      `${label}/accepted-gap-open`);
    await quiesce(page, tracker, `${label}/accepted-gap-open`);
    await page.selectOption("#sched-div", ids.raceDiv);
    const acceptedGapGenerate = armHold(channels.draft);
    await page.click("[data-sched-generate]");
    const acceptedGapPayload = await acceptedGapGenerate.captured;
    const acceptedGapGames = acceptedGapPayload.body
      && acceptedGapPayload.body.draft_games;
    if (acceptedGapPayload.status !== 200 || !Array.isArray(acceptedGapGames)
        || !acceptedGapGames.length
        || !JSON.stringify(acceptedGapGames).includes("Matrix A Race")) {
      fail(`[${label}/accepted-gap] held response lacks a real A proposal: `
        + JSON.stringify(acceptedGapPayload));
    }
    const acceptedGapContext = armHold(channels.context);
    await startContextSwitch(page, ids.pb, ids.sb,
      `${label}/accepted-gap-switch`);
    const acceptedGapEcho = await acceptedGapContext.captured;
    if (acceptedGapEcho.status !== 200 || !acceptedGapEcho.body
        || acceptedGapEcho.body.program_id !== ids.pb
        || acceptedGapEcho.body.season_id !== ids.sb) {
      fail(`[${label}/accepted-gap] B context did not commit while its echo `
        + `was held: ${JSON.stringify(acceptedGapEcho)}`);
    }
    await page.focus("#ctx-select");
    await resetAnnouncements(page);
    const beforeAcceptedGapGenerate = await immutableSnapshot(page, DRAFT_CARD);
    const acceptedGapGenerateReleased = channels.draft.released;
    acceptedGapGenerate.release();
    await waitForReleased(page, channels.draft, acceptedGapGenerateReleased,
      `${label}/accepted-gap-generate-release`);
    await quiesce(page, tracker, `${label}/accepted-gap-generate-release`, 1);
    const afterAcceptedGapGenerate = await immutableSnapshot(page, DRAFT_CARD);
    assertByteEqual(`${label}/accepted-gap`, beforeAcceptedGapGenerate,
      afterAcceptedGapGenerate);
    const acceptedGapContextReleased = channels.context.released;
    acceptedGapContext.release();
    await waitForReleased(page, channels.context, acceptedGapContextReleased,
      `${label}/accepted-gap-context-release`);
    await waitForSelectedTuple(page, ids.pb, ids.sb, true,
      `${label}/accepted-gap-b-selected`);
    await quiesce(page, tracker, `${label}/accepted-gap-b-selected`);
    const acceptedGapB = await cardSnapshot(page, DRAFT_CARD);
    if (acceptedGapB.text.includes("Matrix A Race")
        || JSON.stringify(acceptedGapB.model).includes("Matrix A Race")) {
      fail(`[${label}/accepted-gap] old A Generate appeared after B was `
        + `accepted: ${JSON.stringify(acceptedGapB)}`);
    }
    await selectProgramSeason(page, `${label}: accepted gap return to A`,
      ids.pa, ids.sa);

    // The inverse delivery window starts before the scoped GET reaches the
    // server. Hold an A-labelled Scheduler overview BEFORE route.fetch(), let
    // the context POST commit B on the server while its echo remains withheld
    // from the app, then release the old GET. It may be cancelled in Chromium
    // or reach the epoch fence and receive 204; in neither case may a B answer
    // mutate the still-A card. This fails if the operational GET is reverted
    // from getJSONContextScoped to plain getJSON.
    trace(`${label}: pre-fetch Scheduler read cannot cross server context`);
    await openView(page, "calendar", [CALENDAR_CARD],
      `${label}/prefetch-barrier-leave`);
    await quiesce(page, tracker, `${label}/prefetch-barrier-leave`);
    const abortLedgerBeforePrefetch = await page.evaluate(() =>
      contextScopedReadAborts.length);
    const prefetchedAOverview = armRequestHold(channels.overview);
    await openView(page, "scheduler", [DRAFT_CARD, REVIEW_CARD],
      `${label}/prefetch-barrier-open`);
    const prefetchedRequest = await prefetchedAOverview.captured;
    if (prefetchedRequest.method !== "GET") {
      fail(`[${label}/prefetch-barrier] held request was not Scheduler's GET: `
        + JSON.stringify(prefetchedRequest));
    }
    const prefetchContextEcho = armHold(channels.context);
    await startContextSwitch(page, ids.pb, ids.sb,
      `${label}/prefetch-barrier-switch`);
    const computedPrefetchContext = await prefetchContextEcho.captured;
    if (computedPrefetchContext.status !== 200
        || !computedPrefetchContext.body
        || computedPrefetchContext.body.program_id !== ids.pb
        || computedPrefetchContext.body.season_id !== ids.sb) {
      fail(`[${label}/prefetch-barrier] B context did not commit while its `
        + `echo was held: ${JSON.stringify(computedPrefetchContext)}`);
    }
    await page.focus("#ctx-select");
    await resetAnnouncements(page);
    const beforePrefetchRelease = await immutableSnapshot(page, DRAFT_CARD);
    const prefetchedReleased = channels.overview.released;
    prefetchedAOverview.release();
    await prefetchedAOverview.fetched;
    await waitForReleased(page, channels.overview, prefetchedReleased,
      `${label}/prefetch-barrier-read-release`);
    await page.waitForTimeout(QUIET_WINDOW_MS);
    const prefetchAbortEvidence = await page.evaluate((before) =>
      contextScopedReadAborts.slice(before).filter((entry) =>
        entry.method === "GET" && entry.url === "/api/demo/overview"),
    abortLedgerBeforePrefetch);
    if (prefetchAbortEvidence.length !== 1) {
      fail(`[${label}/prefetch-barrier] app did not record exactly one `
        + `intentional overview abort: ${JSON.stringify(prefetchAbortEvidence)}`);
    }
    const afterPrefetchRelease = await immutableSnapshot(page, DRAFT_CARD);
    assertByteEqual(`${label}/prefetch-barrier`, beforePrefetchRelease,
      afterPrefetchRelease);
    if (afterPrefetchRelease.text.includes("Matrix Program B")
        || JSON.stringify(afterPrefetchRelease.model).includes(ids.pb)) {
      fail(`[${label}/prefetch-barrier] B overview painted under A before `
        + `the context echo: ${JSON.stringify(afterPrefetchRelease)}`);
    }
    const prefetchContextReleased = channels.context.released;
    prefetchContextEcho.release();
    await waitForReleased(page, channels.context, prefetchContextReleased,
      `${label}/prefetch-barrier-context-release`);
    await waitForSelectedTuple(page, ids.pb, ids.sb, true,
      `${label}/prefetch-barrier-b-settled`);
    await selectProgramSeason(page, `${label}: pre-fetch barrier return to A`,
      ids.pa, ids.sa);

    // Context race: a computed, non-empty A response must be delivered and
    // ignored after B fully owns the same Scheduler surface.
    trace(`${label}: held Generate response cannot cross context`);
    await openView(page, "scheduler", [DRAFT_CARD, REVIEW_CARD], `${label}/context-race-open`);
    await page.selectOption("#sched-div", ids.raceDiv);
    const contextRace = armHold(channels.draft);
    await page.click("[data-sched-generate]");
    const contextResponse = await contextRace.captured;
    const raceGames = contextResponse.body && contextResponse.body.draft_games;
    if (contextResponse.status !== 200 || !Array.isArray(raceGames) || !raceGames.length
        || !JSON.stringify(raceGames).includes("Matrix A Race")) {
      fail(`[${label}/context-race] held response lacks a real A-only proposal: `
        + JSON.stringify(contextResponse));
    }
    await selectProgramSeason(page, `${label}: context race to B`, ids.pb, ids.sb);
    await openView(page, "scheduler", [DRAFT_CARD, REVIEW_CARD], `${label}/context-race-b`);
    await page.focus("#ctx-select");
    await resetAnnouncements(page);
    const beforeContextRelease = await immutableSnapshot(page, DRAFT_CARD);
    const contextReleased = channels.draft.released;
    contextRace.release();
    await waitForReleased(page, channels.draft, contextReleased, `${label}/context-race`);
    await quiesce(page, tracker, `${label}/context-race-release`);
    const afterContextRelease = await immutableSnapshot(page, DRAFT_CARD);
    assertByteEqual(`${label}/context-race`, beforeContextRelease, afterContextRelease);
    if (afterContextRelease.text.includes("Matrix A Race")) {
      fail(`[${label}/context-race] A-only proposal painted under Program B`);
    }

    // Tuple equality is not enough: an operation computed during the first
    // visit to A is obsolete after a successful A -> B -> A round trip even
    // though username, epoch and final tuple all compare equal again. Stay on
    // Dashboard throughout both switches so no Scheduler render can issue a
    // newer request and accidentally make the generation axis do the work.
    trace(`${label}: held Generate response cannot survive A → B → A`);
    await selectProgramSeason(page, `${label}: round-trip start at A`, ids.pa, ids.sa);
    await openView(page, "scheduler", [DRAFT_CARD, REVIEW_CARD],
      `${label}/round-trip-open`);
    await quiesce(page, tracker, `${label}/round-trip-open`);
    await page.selectOption("#sched-div", ids.raceDiv);
    const roundTripRace = armHold(channels.draft);
    await page.click("[data-sched-generate]");
    const roundTripResponse = await roundTripRace.captured;
    const roundTripGames = roundTripResponse.body
      && roundTripResponse.body.draft_games;
    if (roundTripResponse.status !== 200 || !Array.isArray(roundTripGames)
        || !roundTripGames.length
        || !JSON.stringify(roundTripGames).includes("Matrix A Race")) {
      fail(`[${label}/round-trip] held response lacks a real A-only proposal: `
        + JSON.stringify(roundTripResponse));
    }
    await openView(page, "dashboard", [], `${label}/round-trip-dashboard`);
    await quiesce(page, tracker, `${label}/round-trip-dashboard`, 1);
    const assertDashboardOnly = async (step) => {
      const observed = await page.evaluate(() => ({
        view: document.body.dataset.view,
        operationalCards: document.querySelectorAll("[data-operational-card]").length,
      }));
      if (observed.view !== "dashboard" || observed.operationalCards) {
        fail(`[${step}] Scheduler rendered during the context round trip: `
          + JSON.stringify(observed));
      }
    };
    await assertDashboardOnly(`${label}/round-trip-dashboard`);
    const heldBContext = armHold(channels.context);
    await startContextSwitch(page, ids.pb, ids.sb, `${label}/round-trip-a-to-b`);
    const computedBContext = await heldBContext.captured;
    if (computedBContext.status !== 200 || !computedBContext.body
        || computedBContext.body.program_id !== ids.pb
        || computedBContext.body.season_id !== ids.sb) {
      fail(`[${label}/round-trip] held B switch was not a real successful `
        + `selection: ${JSON.stringify(computedBContext)}`);
    }
    // B has committed on the server, but its response is still withheld from
    // the app. Queue A now through the real switcher; sendContextSwitch must
    // drain it after B without ever rendering Scheduler or treating the final
    // A tuple equality as proof that the old Generate is current again.
    await startContextSwitch(page, ids.pa, ids.sa, `${label}/round-trip-b-to-a`);
    await assertDashboardOnly(`${label}/round-trip-queued`);
    const bContextReleased = channels.context.released;
    heldBContext.release();
    await waitForReleased(page, channels.context, bContextReleased,
      `${label}/round-trip-b-release`);
    await waitForSelectedTuple(page, ids.pa, ids.sa, true,
      `${label}/round-trip-a-settled`);
    await quiesce(page, tracker, `${label}/round-trip-a` , 1);
    await assertDashboardOnly(`${label}/round-trip-a`);
    await page.focus("#ctx-select");
    await resetAnnouncements(page);
    const beforeRoundTripRelease = await immutableSnapshot(page, DRAFT_CARD);
    const roundTripReleased = channels.draft.released;
    roundTripRace.release();
    await waitForReleased(page, channels.draft, roundTripReleased,
      `${label}/round-trip-release`);
    await quiesce(page, tracker, `${label}/round-trip-release`);
    const afterRoundTripRelease = await immutableSnapshot(page, DRAFT_CARD);
    assertByteEqual(`${label}/round-trip`, beforeRoundTripRelease,
      afterRoundTripRelease);

    // The exact inverse boundary: an ATTEMPT is not a confirmed tuple change.
    // Compute and hold both another genuine A response and B's real backend
    // refusal. Deliver A while the refused echo is still withheld: the shared
    // intent barrier must make that an exact no-op. Once refusal reconciles
    // canonical A, however, the very same response must be admitted. A guard
    // keyed to contextRevision (which bumps on every attempt) would wrongly
    // discard it; a guard absent altogether would mutate during the hold.
    trace(`${label}: failed context switch preserves held Generate response`);
    await openView(page, "scheduler", [DRAFT_CARD, REVIEW_CARD],
      `${label}/failed-switch-open`);
    await quiesce(page, tracker, `${label}/failed-switch-open`);
    await page.selectOption("#sched-div", ids.raceDiv);
    const failedSwitchRace = armHold(channels.draft);
    await page.click("[data-sched-generate]");
    const failedSwitchResponse = await failedSwitchRace.captured;
    const failedSwitchGames = failedSwitchResponse.body
      && failedSwitchResponse.body.draft_games;
    if (failedSwitchResponse.status !== 200
        || !Array.isArray(failedSwitchGames) || !failedSwitchGames.length
        || !JSON.stringify(failedSwitchGames).includes("Matrix A Race")) {
      fail(`[${label}/failed-switch] held response lacks a real A proposal: `
        + JSON.stringify(failedSwitchResponse));
    }
    const failedSwitchOutgoing = await cardSnapshot(page, DRAFT_CARD);
    if (failedSwitchOutgoing.model.state !== "loading") {
      fail(`[${label}/failed-switch] Generate was not in flight before refusal: `
        + JSON.stringify(failedSwitchOutgoing));
    }
    const failedContextEcho = armRejectedContextHold(channels.context);
    await startContextSwitch(page, ids.pb, ids.sb, `${label}/failed-switch-attempt`);
    const rejectedEcho = await failedContextEcho.captured;
    if (rejectedEcho.status < 400 || rejectedEcho.status >= 500
        || !rejectedEcho.body || !rejectedEcho.body.error
        || !rejectedEcho.requestedBody
        || rejectedEcho.requestedBody.program_id !== ids.pb
        || rejectedEcho.requestedBody.season_id !== ids.sb) {
      fail(`[${label}/failed-switch] held echo was not a real backend refusal `
        + `of the UI's B intent: ${JSON.stringify(rejectedEcho)}`);
    }
    await page.focus("#ctx-select");
    await resetAnnouncements(page);
    const beforeFailedSwitchRelease = await immutableSnapshot(page, DRAFT_CARD);
    const failedSwitchReleased = channels.draft.released;
    failedSwitchRace.release();
    await waitForReleased(page, channels.draft, failedSwitchReleased,
      `${label}/failed-switch-release`);
    await quiesce(page, tracker, `${label}/failed-switch-release`, 1);
    const duringFailedSwitch = await immutableSnapshot(page, DRAFT_CARD);
    assertByteEqual(`${label}/failed-switch-pending`,
      beforeFailedSwitchRelease, duringFailedSwitch);
    const rejectedContextReleased = channels.context.released;
    failedContextEcho.release();
    await waitForReleased(page, channels.context, rejectedContextReleased,
      `${label}/failed-switch-context-release`);
    await waitForSelectedTuple(page, ids.pa, ids.sa, true,
      `${label}/failed-switch-reconciled`);
    await quiesce(page, tracker, `${label}/failed-switch-reconciled`);
    const admitted = await cardSnapshot(page, DRAFT_CARD);
    const admittedSpeech = await spoken(page);
    if (!admitted.model || admitted.model.state !== "ready"
        || !admitted.tuple || admitted.tuple.program_id !== ids.pa
        || admitted.tuple.season_id !== ids.sa
        || !JSON.stringify(admitted.model).includes("Matrix A Race")
        || !admitted.toast.includes("Draft schedule preview updated")
        || admittedSpeech.filter((message) =>
          message === "Draft schedule preview updated.").length !== 1) {
      fail(`[${label}/failed-switch] held A response was discarded after a `
        + `refused switch: card ${JSON.stringify(admitted)}, speech `
        + JSON.stringify(admittedSpeech));
    }

    // Same-surface identity privacy window. Unlike the epoch race below,
    // remain on Scheduler while the same username signs out and back in, and
    // hold the arriving session's context/options response. The two existing
    // roots must be synchronously neutralized before any arriving-principal
    // read can repaint them: explicit LOADING, busy, and byte-empty with no
    // controls or departing text.
    trace(`${label}: identity boundary blanks both Scheduler cards in place`);
    await openView(page, "scheduler", [DRAFT_CARD, REVIEW_CARD],
      `${label}/identity-blank-open`);
    await quiesce(page, tracker, `${label}/identity-blank-open`);
    await page.evaluate(() => {
      const button = document.getElementById("signout-btn");
      if (!button) throw new Error("missing #signout-btn");
      button.click();
    });
    await page.waitForFunction(() => !currentUser, null, { timeout: 15000 });
    await page.waitForSelector("#login-screen:not([hidden])", { timeout: 15000 });
    await page.fill("#login-user", "admin");
    await page.fill("#login-pass", "demo");
    const blankOptionsHold = armHold(channels.options);
    await page.click("#login-form button[type=submit]");
    await blankOptionsHold.captured;
    await page.waitForFunction(() => currentUser
      && currentUser.username === "admin", null, { timeout: 15000 });
    const blankCards = await page.evaluate((ids_) => ids_.map((id) => {
      const root = document.querySelector(
        `[data-operational-card="${id}"]`);
      return { id, exists: !!root,
        state: root && root.getAttribute("data-card-state"),
        busy: root && root.getAttribute("aria-busy"),
        html: root && root.innerHTML,
        text: root && root.textContent.trim(),
        controls: root ? root.querySelectorAll(
          "button,input,select,textarea,a[href],[tabindex]").length : null };
    }), [DRAFT_CARD, REVIEW_CARD]);
    const badBlank = blankCards.filter((card) => !card.exists
      || card.state !== "loading" || card.busy !== "true"
      || card.html !== "" || card.text !== "" || card.controls !== 0);
    if (badBlank.length) {
      fail(`[${label}/identity-blank] arriving session saw non-neutral `
        + `Scheduler card(s): ${JSON.stringify(blankCards)}`);
    }
    // Turn the arriving identity's first two reads into payload-less ERRORs.
    // The identity reset above deliberately removed every departing payload,
    // so these failures cannot truthfully become "earlier data" when the
    // confirmed context changes below.
    await resetAnnouncements(page);
    failOnce(channels.overview);
    failOnce(channels.drafts);
    const blankOptionsReleased = channels.options.released;
    blankOptionsHold.release();
    await waitForReleased(page, channels.options, blankOptionsReleased,
      `${label}/identity-blank-options`);
    await waitForSelectedTuple(page, ids.pa, ids.sa, true,
      `${label}/identity-blank-options`);
    await page.waitForSelector(operationalSelector(DRAFT_CARD), { timeout: 15000 });
    await page.waitForSelector(operationalSelector(REVIEW_CARD), { timeout: 15000 });
    await waitForCardState(page, DRAFT_CARD, "error",
      `${label}/identity-first-draft-error`);
    await waitForCardState(page, REVIEW_CARD, "error",
      `${label}/identity-first-review-error`);
    await assertSingleErrorLiveRegion(page, DRAFT_CARD,
      `${label}/identity-first-draft-announcement`);
    await assertErrorRepaintIsSilent(page, DRAFT_CARD,
      `${label}/identity-first-draft-reentry`);
    await assertSingleErrorLiveRegion(page, REVIEW_CARD,
      `${label}/identity-first-review-announcement`);
    await assertErrorRepaintIsSilent(page, REVIEW_CARD,
      `${label}/identity-first-review-reentry`);

    trace(`${label}: payload-less ERROR switch to neutral LOADING`);
    const payloadlessBDraft = armHold(channels.overview);
    const payloadlessBReview = armHold(channels.drafts);
    await startContextSwitch(page, ids.pb, ids.sb,
      `${label}/payloadless-switch`);
    const [payloadlessDraftResponse, payloadlessReviewResponse] = await Promise.all([
      payloadlessBDraft.captured, payloadlessBReview.captured,
    ]);
    if (payloadlessDraftResponse.status !== 200
        || payloadlessReviewResponse.status !== 200) {
      fail(`[${label}/payloadless-switch] replacement reads were not real `
        + `successful responses: ${JSON.stringify({
          payloadlessDraftResponse, payloadlessReviewResponse,
        })}`);
    }
    await waitForSelectedTuple(page, ids.pb, ids.sb, true,
      `${label}/payloadless-selected`);
    await assertNeutralLoading(page, DRAFT_CARD,
      `${label}/payloadless-draft-loading`);
    await assertNeutralLoading(page, REVIEW_CARD,
      `${label}/payloadless-review-loading`);
    const payloadlessDraftReleased = channels.overview.released;
    const payloadlessReviewReleased = channels.drafts.released;
    payloadlessBDraft.release();
    payloadlessBReview.release();
    await waitForReleased(page, channels.overview, payloadlessDraftReleased,
      `${label}/payloadless-draft-release`);
    await waitForReleased(page, channels.drafts, payloadlessReviewReleased,
      `${label}/payloadless-review-release`);
    await waitForCardState(page, DRAFT_CARD, "empty",
      `${label}/payloadless-draft-empty`);
    await waitForCardState(page, REVIEW_CARD, "empty",
      `${label}/payloadless-review-empty`);
    await selectProgramSeason(page, `${label}: payload-less return to A`,
      ids.pa, ids.sa);
    await openView(page, "scheduler", [DRAFT_CARD, REVIEW_CARD],
      `${label}/payloadless-a-return`);
    await quiesce(page, tracker, `${label}/payloadless-a-return`);

    // Principal race: hold context/options after setUser() has bumped the
    // epoch but before the arriving principal can render. First prove the
    // post-auth/pre-render privacy window is blank. Then let options restore
    // the same tuple while staying on Games, which issues no Scheduler
    // card request and therefore leaves generation equal. At old-response
    // delivery time the epoch is the only identity axis that differs.
    trace(`${label}: held Generate response cannot cross uiIdentityEpoch`);
    await selectProgramSeason(page, `${label}: epoch race return to A`, ids.pa, ids.sa);
    await openView(page, "scheduler", [DRAFT_CARD, REVIEW_CARD], `${label}/epoch-race-open`);
    await page.selectOption("#sched-div", ids.raceDiv);
    const epochRace = armHold(channels.draft);
    await page.click("[data-sched-generate]");
    const epochResponse = await epochRace.captured;
    const epochGames = epochResponse.body && epochResponse.body.draft_games;
    if (epochResponse.status !== 200 || !Array.isArray(epochGames) || !epochGames.length) {
      fail(`[${label}/epoch-race] held response is not a real successful proposal: `
        + JSON.stringify(epochResponse));
    }
    const outgoing = await cardSnapshot(page, DRAFT_CARD);
    if (outgoing.state !== "loading" || outgoing.principal !== "admin") {
      fail(`[${label}/epoch-race] outgoing card was not admin's live LOADING request: `
        + JSON.stringify(outgoing));
    }

    // Leave the Scheduler via its shipped navigation before signing out. The
    // request remains real and in flight, but the arriving Admin will settle
    // on Games and cannot advance this card's generation before the old
    // delivery is tested. Games is deliberate: a same-user re-login re-arms
    // the first-session Initial Setup redirect for Dashboard, while Games is a
    // stable operator destination with no Scheduler card request.
    await openView(page, "games", [], `${label}/epoch-race-games`);
    await quiesce(page, tracker, `${label}/epoch-race-games`, 1);

    // Use the real header control and login form.  Re-entering as the SAME
    // username is stronger than a second-account switch: principal equality
    // cannot explain the rejection, so the epoch must do the work.
    await page.evaluate(() => {
      const button = document.getElementById("signout-btn");
      if (!button) throw new Error("missing #signout-btn");
      button.click();
    });
    await page.waitForFunction(() => !currentUser, null, { timeout: 15000 });
    await page.waitForSelector("#login-screen:not([hidden])", { timeout: 15000 });
    await page.fill("#login-user", "admin");
    await page.fill("#login-pass", "demo");
    const optionsHold = armHold(channels.options);
    await page.click("#login-form button[type=submit]");
    await optionsHold.captured;
    await page.waitForFunction(() => currentUser
      && currentUser.username === "admin", null, { timeout: 15000 });
    const insideEpochWindow = await cardSnapshot(page, DRAFT_CARD);
    if (insideEpochWindow.epoch <= outgoing.epoch
        || insideEpochWindow.generation !== outgoing.generation) {
      fail(`[${label}/epoch-race] identity boundary did not preserve the monotone `
        + `generation while advancing uiIdentityEpoch: outgoing `
        + `${JSON.stringify(outgoing)}, arriving ${JSON.stringify(insideEpochWindow)}`);
    }
    const invalidated = await page.evaluate((ids_) => ids_.map((id) => ({
      id,
      state: readCardState(id).state,
    })), CARD_IDS);
    if (invalidated.some((entry) => entry.state !== "loading")) {
      fail(`[${label}/epoch-race] re-login did not invalidate all four card models: `
        + JSON.stringify(invalidated));
    }
    if (insideEpochWindow.text.includes("Matrix A Race") || insideEpochWindow.mutations) {
      fail(`[${label}/epoch-race] departing payload/control survived the identity boundary: `
        + JSON.stringify(insideEpochWindow));
    }

    const optionsReleased = channels.options.released;
    optionsHold.release();
    await waitForReleased(page, channels.options, optionsReleased,
      `${label}/epoch-options`);
    await waitForSelectedTuple(page, ids.pa, ids.sa, true,
      `${label}/epoch-options`);
    await quiesce(page, tracker, `${label}/epoch-options-settled`, 1);

    const isolated = await cardSnapshot(page, DRAFT_CARD);
    if (isolated.epoch <= outgoing.epoch
        || isolated.generation !== outgoing.generation
        || isolated.principal !== outgoing.principal
        || !isolated.tuple || !outgoing.tuple
        || JSON.stringify(isolated.tuple) !== JSON.stringify(outgoing.tuple)) {
      fail(`[${label}/epoch-race] test failed to isolate uiIdentityEpoch after `
        + `the same user's tuple settled: outgoing ${JSON.stringify(outgoing)}, `
        + `arriving ${JSON.stringify(isolated)}`);
    }

    await page.focus("#ctx-select");
    await resetAnnouncements(page);
    const beforeEpochRelease = await immutableSnapshot(page, DRAFT_CARD);
    const epochReleased = channels.draft.released;
    epochRace.release();
    await waitForReleased(page, channels.draft, epochReleased, `${label}/epoch-race`);
    const afterEpochRelease = await immutableSnapshot(page, DRAFT_CARD);
    assertByteEqual(`${label}/epoch-race`, beforeEpochRelease, afterEpochRelease);

    await openView(page, "scheduler", [DRAFT_CARD, REVIEW_CARD],
      `${label}/epoch-recovery-open`);
    await quiesce(page, tracker, `${label}/epoch-recovery`);
    const recovered = await cardSnapshot(page, DRAFT_CARD);
    if (recovered.principal !== "admin"
        || !["empty", "ready"].includes(recovered.state)) {
      fail(`[${label}/epoch-recovery] arriving principal did not recover from its own render: `
        + JSON.stringify(recovered));
    }

    // Independent sibling ownership includes the DOM and keyboard, not only
    // model generations. Run this last so its deliberately held shared
    // overview cannot perturb the state-transition setup above.
    trace(`${label}: Draft settlement preserves focused Review DOM`);
    await openView(page, "calendar", [CALENDAR_CARD],
      `${label}/sibling-focus-leave`);
    await quiesce(page, tracker, `${label}/sibling-focus-leave`);
    const siblingDraft = armHold(channels.overview);
    await openView(page, "scheduler", [DRAFT_CARD, REVIEW_CARD],
      `${label}/sibling-focus-open`);
    const siblingDraftPayload = await siblingDraft.captured;
    if (siblingDraftPayload.status !== 200 || !siblingDraftPayload.body) {
      fail(`[${label}/sibling-focus] held Draft overview was not successful: `
        + JSON.stringify(siblingDraftPayload));
    }
    await waitForCardState(page, REVIEW_CARD, "ready",
      `${label}/sibling-focus-review`);
    await quiesce(page, tracker, `${label}/sibling-focus-review`, 1);
    const siblingBefore = await page.evaluate((reviewId) => {
      const root = document.querySelector(
        `[data-operational-card="${reviewId}"]`);
      const target = root && root.querySelector(".sched-pick");
      if (!target) return null;
      target.focus();
      window.__schedulerMatrixSiblingFocus = target;
      return { focused: document.activeElement === target,
        connected: target.isConnected };
    }, REVIEW_CARD);
    if (!siblingBefore || !siblingBefore.focused || !siblingBefore.connected) {
      fail(`[${label}/sibling-focus] Review did not expose a focusable live control`);
    }
    const siblingReleased = channels.overview.released;
    siblingDraft.release();
    await waitForReleased(page, channels.overview, siblingReleased,
      `${label}/sibling-focus-release`);
    await quiesce(page, tracker, `${label}/sibling-focus-release`);
    const siblingAfter = await page.evaluate(() => {
      const target = window.__schedulerMatrixSiblingFocus;
      return { focused: !!target && document.activeElement === target,
        connected: !!target && target.isConnected,
        card: target && target.closest("[data-operational-card]")
          ? target.closest("[data-operational-card]")
            .getAttribute("data-operational-card") : null };
    });
    if (!siblingAfter.focused || !siblingAfter.connected
        || siblingAfter.card !== REVIEW_CARD) {
      fail(`[${label}/sibling-focus] Draft settlement replaced or unfocused `
        + `Review's live node: ${JSON.stringify(siblingAfter)}`);
    }

    // Publish the final drafts through the real Review workflow. First force
    // the bulk operation's ERROR to pin its single live-region exposure, then
    // retry the read and publish successfully. The last draft disappearing
    // makes the card EMPTY, but the non-zero published summary is still
    // authoritative history and must remain visible and present in the model.
    trace(`${label}: final publish keeps non-zero Review summary in EMPTY`);
    const draftsBeforePublish = await page.evaluate(() => {
      const model = cardDisplayModel(readCardState("scheduler/review"));
      return model && model.payload && Array.isArray(model.payload.drafts)
        ? model.payload.drafts.length : 0;
    });
    if (draftsBeforePublish < 1) {
      fail(`[${label}/review-final-publish] no draft remained for the oracle`);
    }
    await page.click("[data-sched-select-all]");
    await resetAnnouncements(page);
    failOnce(channels.publish);
    await page.click("[data-sched-publish]");
    await waitForCardState(page, REVIEW_CARD, "error",
      `${label}/review-publish-error`);
    await assertSingleErrorLiveRegion(page, REVIEW_CARD,
      `${label}/review-publish-error-announcement`);
    await assertErrorRepaintIsSilent(page, REVIEW_CARD,
      `${label}/review-publish-error-reentry`);
    await activateRetryWithKeyboard(page, REVIEW_CARD,
      `${label}/review-publish-retry`);
    await waitForCardState(page, REVIEW_CARD, "ready",
      `${label}/review-publish-recovered`);
    await page.click("[data-sched-select-all]");
    await page.click("[data-sched-publish]");
    await waitForCardState(page, REVIEW_CARD, "empty",
      `${label}/review-publish-empty`);
    await quiesce(page, tracker, `${label}/review-publish-empty`);
    const publishedEmpty = await cardSnapshot(page, REVIEW_CARD);
    const publishedSummary = publishedEmpty.model && publishedEmpty.model.payload
      && publishedEmpty.model.payload.summary;
    if (!publishedSummary || publishedSummary.draft_count !== 0
        || publishedSummary.published_count < draftsBeforePublish
        || !publishedEmpty.text.includes("0 draft")
        || !publishedEmpty.text.includes(
          `${publishedSummary.published_count} published`)) {
      fail(`[${label}/review-final-publish] EMPTY lost its published-history `
        + `summary: expected at least ${draftsBeforePublish}, observed `
        + JSON.stringify(publishedEmpty));
    }

    const checked = coverage.assertComplete(label);

    // Reconcile forced failures exactly.  A deliberate 500 excuses only the
    // matching method+URL+status response and its browser resource line.
    const injections = Object.values(channels).flatMap((channel) => channel.injected);
    for (const response of nonOk) {
      const match = injections.find((item) => !item.seen
        && item.method === response.method && item.url === response.url
        && item.status === response.status);
      if (match) match.seen = true;
      else fail(`[${label}] unexpected HTTP failure: ${JSON.stringify(response)}`);
    }
    const undelivered = injections.filter((item) => !item.seen);
    if (undelivered.length) {
      fail(`[${label}] forced failure never reached the page: ${JSON.stringify(undelivered)}`);
    }
    // A context-scoped GET cancelled by the app is an explained withdrawal,
    // not a transport defect. Reconcile Chromium's net::ERR_ABORTED rows to
    // the app's own per-request abort ledger exactly; every other failed
    // request remains fatal.
    const abortLedger = await page.evaluate(() => contextScopedReadAborts
      .map((entry) => Object.assign({}, entry)));
    const availableAborts = abortLedger.map((entry) => ({ entry, seen: false }));
    const unexplainedFailures = requestFailures.filter((failure) => {
      let pathname = failure.url;
      try { pathname = new URL(failure.url).pathname; } catch (_) {}
      const match = availableAborts.find((candidate) => !candidate.seen
        && candidate.entry.dispatched && !candidate.entry.discarded
        && candidate.entry.method === failure.method
        && candidate.entry.url === pathname
        && /ERR_ABORTED/.test(failure.failure || ""));
      if (!match) return true;
      match.seen = true;
      return false;
    });
    if (unexplainedFailures.length) {
      fail(`[${label}] unexplained failed request(s): `
        + JSON.stringify(unexplainedFailures));
    }
    const badConsole = consoleErrors.filter((entry) => {
      if (!/Failed to load resource/i.test(entry.text)) return true;
      return !injections.some((item) => item.url === entry.url);
    });
    if (badConsole.length) {
      fail(`[${label}] console/page error(s): ${JSON.stringify(badConsole)}`);
    }
    console.log(`[${label}] OK — ${checked} card/state cells, context-response `
      + `fence, and uiIdentityEpoch fence.`);
  } catch (error) {
    throw new Error(`${error.message}\n--- server output ---\n${serverOutput}`);
  } finally {
    await context.close();
    await stopServer(server);
  }
}

async function main() {
  let browser;
  try {
    browser = await chromium.launch(process.env.SMOKE_CHROMIUM_PATH
      ? { executablePath: process.env.SMOKE_CHROMIUM_PATH } : {});
    for (const viewport of VIEWPORTS) await checkViewport(browser, viewport);
    console.log("Scheduler operational-card state matrix passed.");
  } catch (error) {
    console.error("Scheduler operational-card state matrix FAILED.");
    console.error(error && error.stack ? error.stack : error);
    process.exitCode = 1;
  } finally {
    if (browser) await browser.close();
  }
}

main();
