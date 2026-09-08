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
const SEMANTIC_CONTROL_SELECTOR =
  "button,select,input:not([type=hidden]),textarea,[role=button],[draggable=true]";

const DRAFT_RE = /\/api\/scheduler\/draft$/;
const DRAFT_COMMIT_RE = /\/api\/scheduler\/commit$/;
const DRAFTS_RE = /\/api\/scheduler\/drafts(?:\?|$)/;
const PUBLISH_RE = /\/api\/scheduler\/drafts\/publish$/;
const ICE_PREVIEW_RE = /\/api\/setup\/ice-availability\/preview$/;
const ICE_COMMIT_RE = /\/api\/setup\/ice-availability\/commit$/;
const ADD_ICE_RE = /\/api\/demo\/add-ice-slot$/;
const OVERVIEW_RE = /\/api\/demo\/overview(?:\?|$)/;
const CONTEXT_OPTIONS_RE = /\/api\/context\/options(?:\?|$)/;
const CONTEXT_RE = /\/api\/context$/;

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
  return page.evaluate(([id, semanticControlSelector]) => {
    const root = Array.from(document.querySelectorAll("[data-operational-card]"))
      .find((node) => node.getAttribute("data-operational-card") === id);
    const plain = (value) => JSON.parse(JSON.stringify(value));
    const descriptor = (node) => {
      const attrs = Array.from(node.attributes || [])
        .filter((attr) => attr.name === "id" || attr.name === "type"
          || attr.name === "role" || attr.name === "aria-label"
          || attr.name.startsWith("data-"))
        .map((attr) => `${attr.name}=${JSON.stringify(attr.value)}`)
        .sort().join(" ");
      const text = (node.textContent || "").replace(/\s+/g, " ").trim().slice(0, 80);
      return `<${node.tagName.toLowerCase()}${attrs ? ` ${attrs}` : ""}> ${text}`;
    };
    const semanticControls = root
      ? Array.from(root.querySelectorAll(semanticControlSelector)) : [];
    // The read-only contract is fail-closed over every semantic control the
    // card actually rendered. Only local/supersedable choices and the one
    // Refresh/Retry escape hatch are explicitly exempt; adding a new button,
    // input, role-button or draggable automatically joins this inventory.
    const mutations = semanticControls.filter((node) =>
      !node.matches("[data-card-retry],[data-card-local-control]")
        && !node.closest("[data-card-local-controls]"));
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
      mutations: mutations.map(descriptor),
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
  }, [cardId, SEMANTIC_CONTROL_SELECTOR]);
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
    // Every persisted operational-card write must cross one shared settlement
    // barrier. The owner list is the complete production write surface: its
    // wrapper count pins all nine current writes, while the raw-call scan keeps
    // a new branch inside any owner from bypassing the helper.
    operationalWrites: (() => {
      const owners = [wireModal, previewIceBuilder, wireCalendarCards,
        generateSchedulerDraft, wireSchedulerCards];
      const rows = owners.map((owner) => ({
        name: owner.name,
        source: String(owner),
      }));
      const rawOwners = rows.filter((row) => /\bpostScoped\s*\(/.test(row.source))
        .map((row) => row.name);
      const wrapperCalls = rows.reduce((total, row) => total
        + (row.source.match(/\bpostOperationalCardScoped\s*\(/g) || []).length, 0);
      const withdrawnGuards = rows.reduce((total, row) => total
        + (row.source.match(/=== OPERATIONAL_CARD_WRITE_WITHDRAWN/g) || []).length, 0);
      const helper = String(postOperationalCardScoped);
      const intent = helper.indexOf("contextSwitchIntentPending");
      const preIdentity = helper.indexOf("!cardIdentityCurrent(identity)", intent);
      const preWithdrawn = helper.indexOf(
        "return OPERATIONAL_CARD_WRITE_WITHDRAWN", preIdentity);
      const post = helper.indexOf("await postScoped(path, body)", preWithdrawn);
      const settle = helper.indexOf(
        "await awaitOperationalCardContextSettlement(identity)", post);
      const postIdentity = helper.indexOf("cardIdentityCurrent(identity)", settle);
      const postWithdrawn = helper.indexOf("OPERATIONAL_CARD_WRITE_WITHDRAWN", postIdentity);
      const withdrawal = String(withdrawContextScopedActionControls);
      const withdrawalSelectors = ["[data-publish]", "[data-move-undo]",
        "[data-del-confirm]"].every((selector) => withdrawal.includes(selector));
      const cleanup = String(invalidateAcceptedScheduleFacilityContext);
      const cleanupState = ["wizard", "conflict", "pendingMove", "movingGameId",
        "modal"].every((name) => cleanup.includes(`${name} = null`));
      const acceptedCalls = (String(sendContextSwitch).match(
        /if \(acceptedContextMoved\) invalidateAcceptedScheduleFacilityContext\(\);/g)
        || []).length;
      return { rawOwners, wrapperCalls, withdrawnGuards,
        withdrawalSelectors, cleanupState, acceptedCalls,
        helperOrdered: intent >= 0 && preIdentity > intent
          && preWithdrawn > preIdentity && post > preWithdrawn
          && settle > post && postIdentity > settle
          && postWithdrawn > postIdentity };
    })(),
    // commitCardState() independently repeats the identity check, but keep
    // Generate's direct outcome guard pinned too: deleting it must not turn
    // that documented defense-in-depth boundary into dead prose.
    generateGuarded: (() => {
      if (typeof generateSchedulerDraft !== "function") return false;
      const source = String(generateSchedulerDraft);
      const request = source.indexOf(
        'await postOperationalCardScoped(\n    identity, "/api/scheduler/draft", request)');
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
  if (!production.operationalWrites.helperOrdered
      || production.operationalWrites.wrapperCalls !== 9
      || production.operationalWrites.withdrawnGuards !== 9
      || !production.operationalWrites.withdrawalSelectors
      || !production.operationalWrites.cleanupState
      || production.operationalWrites.acceptedCalls !== 2
      || production.operationalWrites.rawOwners.length) {
    fail(`[${label}] operational write settlement axis diverged: expected 9 `
      + `writes with caller withdrawal guards behind one two-sided helper, got ${JSON.stringify(
        production.operationalWrites)}`);
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
    if (got.mutations.length) {
      fail(`[${step}] ${cardId} exposes mutation control(s) while loading: `
        + JSON.stringify(got.mutations));
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
    if (got.mutations.length) {
      fail(`[${step}] ${cardId} STALE exposes obsolete mutation control(s): `
        + JSON.stringify(got.mutations));
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

async function operationalSiblingSnapshot(page, targetCardId) {
  return page.evaluate(([ids, target]) => {
    const plain = (value) => JSON.parse(JSON.stringify(value));
    return ids.filter((id) => id !== target).map((id) => {
      const root = document.querySelector(`[data-operational-card="${id}"]`);
      return {
        id,
        generation: cardGenerations[id] || 0,
        model: plain(readCardState(id)),
        html: root ? root.innerHTML : null,
      };
    });
  }, [CARD_IDS, targetCardId]);
}

// Refresh each real EMPTY card through its shipped loader. EMPTY is retained
// internally because it can carry an authoritative summary or editable local
// template, but the loading UI must not call that absence "data" or serialize
// a READY-looking zero-row Review shell. The sibling snapshot also pins the
// independent-repaint boundary while each target is in flight.
async function assertEmptyRefresh(page, tracker, ledger, cardId, channel,
    trigger, step) {
  await quiesce(page, tracker, `${step}/before`);
  const before = await cardSnapshot(page, cardId);
  if (before.state !== "empty") {
    fail(`[${step}] ${cardId} did not start from EMPTY: ${JSON.stringify(before)}`);
  }
  const siblingsBefore = await operationalSiblingSnapshot(page, cardId);
  const held = armHold(channel);
  await trigger();
  const response = await held.captured;
  if (response.status !== 200) {
    fail(`[${step}] ${cardId} refresh was not a real successful response: `
      + JSON.stringify(response));
  }
  const loading = await waitForCardState(page, cardId, "loading", `${step}/loading`);
  if (!loading.model || !loading.model.retained
      || loading.model.retained.state !== "empty"
      || !/previous empty result/i.test(loading.text)
      || /showing (?:the last loaded |read-only )?data/i.test(loading.text)) {
    fail(`[${step}] ${cardId} misrepresented its retained EMPTY result: `
      + JSON.stringify(loading));
  }
  if (cardId === REVIEW_CARD
      && (!loading.html.includes('data-card-empty="review"')
        || loading.text.includes("No draft games match these filters"))) {
    fail(`[${step}] Review rendered a READY-looking zero-row shell while `
      + `refreshing EMPTY: ${JSON.stringify(loading)}`);
  }
  const siblingsDuring = await operationalSiblingSnapshot(page, cardId);
  if (JSON.stringify(siblingsDuring) !== JSON.stringify(siblingsBefore)) {
    fail(`[${step}] ${cardId} refresh mutated a sibling while loading: before `
      + `${JSON.stringify(siblingsBefore)}, during ${JSON.stringify(siblingsDuring)}`);
  }
  const released = channel.released;
  held.release();
  await waitForReleased(page, channel, released, `${step}/release`);
  await waitForCardState(page, cardId, "empty", `${step}/settled`);
  await quiesce(page, tracker, `${step}/settled`);
  const siblingsAfter = await operationalSiblingSnapshot(page, cardId);
  if (JSON.stringify(siblingsAfter) !== JSON.stringify(siblingsBefore)) {
    fail(`[${step}] ${cardId} refresh mutated a sibling after settlement: before `
      + `${JSON.stringify(siblingsBefore)}, after ${JSON.stringify(siblingsAfter)}`);
  }
  ledger.add(cardId);
}

function armCardReadAfterSiblings(channel, skippedSiblingReads) {
  return skippedSiblingReads
    ? armHoldAfter(channel, skippedSiblingReads) : armHold(channel);
}

// A tuple round trip can return to the tuple that owns a retained EMPTY while
// its replacement read is still in flight. The retained display model is then
// STALE/staleFrom=EMPTY rather than directly EMPTY; that provenance must keep
// the same truthful "previous empty result" copy across every operational
// card instead of calling the absence "data" just because the outer model is
// LOADING. Both holds are real, already-computed responses: the away response
// is retired while the return intent is pending, then a distinct return read
// is held so the exact nested model is observable.
async function assertEmptyRoundTripLoading(page, tracker, cardId, channel,
    contextChannel, skippedSiblingReads, origin, away, step) {
  await quiesce(page, tracker, `${step}/origin`);
  const initial = await cardSnapshot(page, cardId);
  if (initial.state !== "empty" || !initial.model || !initial.model.payload) {
    fail(`[${step}] ${cardId} did not begin with a real payload-bearing EMPTY: `
      + JSON.stringify(initial));
  }

  const awayRead = armCardReadAfterSiblings(channel, skippedSiblingReads);
  await startContextSwitch(page, away.programId, away.seasonId,
    `${step}/away-switch`);
  const awayResponse = await awayRead.captured;
  if (awayResponse.status !== 200 || !awayResponse.body) {
    fail(`[${step}] ${cardId} away replacement was not a real successful read: `
      + JSON.stringify(awayResponse));
  }
  await waitForSelectedTuple(page, away.programId, away.seasonId, true,
    `${step}/away-selected`);
  const stale = await waitForCardState(page, cardId, "stale", `${step}/away-stale`);
  if (!stale.model || !stale.model.retained
      || stale.model.retained.state !== "stale"
      || stale.model.retained.staleFrom !== "empty") {
    fail(`[${step}] ${cardId} did not retain EMPTY provenance while away: `
      + JSON.stringify(stale));
  }

  // POST /api/context mutates the server before its response reaches the app.
  // Hold that return echo, then retire the away read while the return intent
  // has already invalidated its identity. Only after the channel is free can
  // the distinct origin replacement be armed.
  const returnContext = armHold(contextChannel);
  await startContextSwitch(page, origin.programId, origin.seasonId,
    `${step}/return-switch`);
  // The switch pipeline cancels and drains the old scoped read before it is
  // allowed to POST the new context. Release only after the return intent is
  // queued, then observe the computed context echo on the far side of that
  // settlement barrier.
  const awayReleased = channel.released;
  awayRead.release();
  await waitForReleased(page, channel, awayReleased, `${step}/away-release`);
  const returnEcho = await returnContext.captured;
  if (returnEcho.status !== 200 || !returnEcho.body
      || returnEcho.body.program_id !== origin.programId
      || returnEcho.body.season_id !== origin.seasonId) {
    fail(`[${step}] ${cardId} return switch was not committed by the server: `
      + JSON.stringify(returnEcho));
  }

  const originRead = armCardReadAfterSiblings(channel, skippedSiblingReads);
  const returnContextReleased = contextChannel.released;
  returnContext.release();
  await waitForReleased(page, contextChannel, returnContextReleased,
    `${step}/return-context-release`);
  const originResponse = await originRead.captured;
  if (originResponse.status !== 200 || !originResponse.body) {
    fail(`[${step}] ${cardId} return replacement was not a real successful read: `
      + JSON.stringify(originResponse));
  }
  await waitForSelectedTuple(page, origin.programId, origin.seasonId, true,
    `${step}/origin-selected`);
  await quiesce(page, tracker, `${step}/origin-loading`, 1);

  const siblingsBefore = await operationalSiblingSnapshot(page, cardId);
  const loading = await waitForCardState(page, cardId, "loading",
    `${step}/return-loading`);
  if (!loading.model || !loading.model.retained
      || loading.model.retained.state !== "stale"
      || loading.model.retained.staleFrom !== "empty") {
    fail(`[${step}] ${cardId} did not reach the retained EMPTY return model: `
      + JSON.stringify(loading));
  }
  const representationFailures = [];
  if (!/previous empty result/i.test(loading.text)
      || /showing (?:the last loaded |read-only )?data/i.test(loading.text)) {
    representationFailures.push(`${cardId} called its retained EMPTY data`);
  }
  if (cardId === REVIEW_CARD
      && (!loading.html.includes('data-card-empty="review"')
        || loading.text.includes("No draft games match these filters"))) {
    representationFailures.push(
      "scheduler/review serialized its READY-only zero-row shell");
  }
  await page.waitForTimeout(QUIET_WINDOW_MS);
  const siblingsDuring = await operationalSiblingSnapshot(page, cardId);
  if (JSON.stringify(siblingsDuring) !== JSON.stringify(siblingsBefore)) {
    fail(`[${step}] ${cardId} return hold mutated a sibling: before `
      + `${JSON.stringify(siblingsBefore)}, during ${JSON.stringify(siblingsDuring)}`);
  }

  const originReleased = channel.released;
  originRead.release();
  await waitForReleased(page, channel, originReleased, `${step}/origin-release`);
  await waitForCardState(page, cardId, "empty", `${step}/origin-empty`);
  await quiesce(page, tracker, `${step}/origin-settled`);
  const siblingsAfter = await operationalSiblingSnapshot(page, cardId);
  if (JSON.stringify(siblingsAfter) !== JSON.stringify(siblingsBefore)) {
    fail(`[${step}] ${cardId} settlement mutated a sibling: before `
      + `${JSON.stringify(siblingsBefore)}, after ${JSON.stringify(siblingsAfter)}`);
  }
  return representationFailures;
}

// Review rows may legitimately have no Division (League-wide game) or no
// resolved Rink (historical/detached slot). Exercise the renderer directly so
// those nullable DTO fields cannot turn into a blank option that filters its
// own row away. The foreign-key leg also pins stale local filter state: a key
// absent from the current Review inventory must behave and render as All.
async function assertReviewFilterEdges(page, step) {
  const siblingsBefore = await operationalSiblingSnapshot(page, REVIEW_CARD);
  const fixture = await page.evaluate((cardId) => {
    const entry = readCardState(cardId);
    const drafts = entry && entry.payload && entry.payload.drafts || [];
    if (entry.state !== CARD_STATE.READY || !drafts.length) {
      return { installed: false, state: entry && entry.state, drafts: drafts.length };
    }
    const gameId = "matrix-review-unassigned-axis";
    const source = drafts[0];
    const synthetic = Object.assign({}, source, {
      game_id: gameId,
      division_id: null,
      division_name: null,
      rink_id: null,
      rink_name: null,
      home_team_name: "Matrix Unassigned Home",
      away_team_name: "Matrix Unassigned Away",
    });
    window.__schedulerMatrixReviewFilterOriginal = {
      entry,
      filters: Object.assign({}, schedulerState.filters),
    };
    cardStates[cardId] = Object.assign({}, entry, {
      payload: Object.assign({}, entry.payload, {
        drafts: drafts.concat([synthetic]),
        selected: new Set(entry.payload.selected || []),
      }),
    });
    schedulerState.filters = { division: "all", rink: "all", issue: "all" };
    repaintSchedulerSurface(cardId);
    const options = (selector) => Array.from(
      document.querySelector(selector).options).filter(
      (option) => option.value === SCHEDULER_REVIEW_UNASSIGNED_FILTER)
      .map((option) => ({ value: option.value, label: option.textContent.trim() }));
    return {
      installed: true,
      gameId,
      sentinel: SCHEDULER_REVIEW_UNASSIGNED_FILTER,
      sentinelInjective: schedulerReviewFilterValue(
        SCHEDULER_REVIEW_UNASSIGNED_FILTER) !== SCHEDULER_REVIEW_UNASSIGNED_FILTER,
      divisionOptions: options("#sched-filter-div"),
      rinkOptions: options("#sched-filter-rink"),
    };
  }, REVIEW_CARD);
  if (!fixture.installed || !fixture.sentinelInjective
      || JSON.stringify(fixture.divisionOptions)
        !== JSON.stringify([{ value: fixture.sentinel, label: "Unassigned" }])
      || JSON.stringify(fixture.rinkOptions)
        !== JSON.stringify([{ value: fixture.sentinel, label: "Unassigned" }])) {
    fail(`[${step}] nullable Review axes were not represented by one explicit, `
      + `injective Unassigned option: ${JSON.stringify(fixture)}`);
  }

  const filteredRow = async (selector) => {
    await page.selectOption(selector, fixture.sentinel);
    return page.evaluate(([cardId, gameId]) => {
      const root = document.querySelector(`[data-operational-card="${cardId}"]`);
      return {
        rowVisible: !!(root && root.querySelector(
          `[data-sched-pick="${CSS.escape(gameId)}"]`)),
        visibleRows: root ? root.querySelectorAll("[data-sched-pick]").length : 0,
      };
    }, [REVIEW_CARD, fixture.gameId]);
  };
  const byDivision = await filteredRow("#sched-filter-div");
  await page.selectOption("#sched-filter-div", "all");
  const byRink = await filteredRow("#sched-filter-rink");
  if (!byDivision.rowVisible || byDivision.visibleRows !== 1
      || !byRink.rowVisible || byRink.visibleRows !== 1) {
    fail(`[${step}] selecting Unassigned hid its own Review row: `
      + JSON.stringify({ byDivision, byRink }));
  }

  const absentFilter = await page.evaluate((cardId) => {
    schedulerState.filters.division = schedulerReviewFilterValue(
      "matrix-foreign-division");
    schedulerState.filters.rink = "all";
    repaintSchedulerSurface(cardId);
    const entry = readCardState(cardId);
    const drafts = entry && entry.payload && entry.payload.drafts || [];
    return {
      selected: document.querySelector("#sched-filter-div").value,
      visibleRows: document.querySelectorAll(
        `[data-operational-card="${cardId}"] [data-sched-pick]`).length,
      expectedRows: drafts.length,
    };
  }, REVIEW_CARD);
  if (absentFilter.selected !== "all"
      || absentFilter.visibleRows !== absentFilter.expectedRows
      || !absentFilter.expectedRows) {
    fail(`[${step}] an absent prior-tuple filter produced a false empty Review: `
      + JSON.stringify(absentFilter));
  }

  await page.evaluate((cardId) => {
    const original = window.__schedulerMatrixReviewFilterOriginal;
    cardStates[cardId] = original.entry;
    schedulerState.filters = original.filters;
    delete window.__schedulerMatrixReviewFilterOriginal;
    repaintSchedulerSurface(cardId);
  }, REVIEW_CARD);
  const siblingsAfter = await operationalSiblingSnapshot(page, REVIEW_CARD);
  if (JSON.stringify(siblingsAfter) !== JSON.stringify(siblingsBefore)) {
    fail(`[${step}] Review filter exercise mutated a sibling: before `
      + `${JSON.stringify(siblingsBefore)}, after ${JSON.stringify(siblingsAfter)}`);
  }
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
  if (got.busy !== "true" || got.mutations.length || got.alertCount
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

// Card-only repainting replaces the control nodes without returning through
// render()'s cross-view wiring pass.  Derive this oracle from the interactive
// elements that the card actually rendered rather than from a hand-maintained
// selector list: every semantic control must retain a matching direct or
// delegated event handler on its new node.  The init-script listener ledger
// below makes addEventListener bindings observable without activating (and
// potentially committing) the controls.
async function assertCardActionsWired(page, cardId, step) {
  const observed = await page.evaluate(([id, semanticControlSelector]) => {
    const root = document.querySelector(`[data-operational-card="${id}"]`);
    if (!root) return { exists: false, checked: 0, unwired: [], nonSemanticActions: [] };
    const controls = Array.from(root.querySelectorAll(semanticControlSelector));
    const descriptor = (node) => {
      const attrs = Array.from(node.attributes || [])
        .filter((attr) => attr.name === "id" || attr.name === "type"
          || attr.name === "role" || attr.name === "aria-label"
          || attr.name.startsWith("data-"))
        .map((attr) => `${attr.name}=${JSON.stringify(attr.value)}`)
        .sort().join(" ");
      const text = (node.textContent || "").replace(/\s+/g, " ").trim().slice(0, 80);
      return `<${node.tagName.toLowerCase()}${attrs ? ` ${attrs}` : ""}> ${text}`;
    };
    const requiredEvents = (node) => {
      const groups = [];
      if (node.matches("button,[role=button]")) {
        groups.push({ alternatives: ["click"], delegated: false });
      }
      if (node.matches("select,input[type=checkbox],input[type=radio]")) {
        groups.push({ alternatives: ["change"], delegated: true });
      } else if (node.matches("input,textarea")) {
        groups.push({ alternatives: ["input", "change"], delegated: true });
      }
      if (node.matches("[draggable=true]")) {
        groups.push({ alternatives: ["dragstart"], delegated: false });
      }
      return groups;
    };
    const unwired = [];
    let checked = 0;
    controls.forEach((node) => {
      requiredEvents(node).forEach((requirement) => {
        checked += 1;
        const handlerRoot = requirement.delegated ? root : node;
        const wired = requirement.alternatives.some((eventName) =>
          window.__schedulerMatrixHasEventHandler(node, eventName, handlerRoot));
        if (!wired) unwired.push({
          control: descriptor(node),
          alternatives: requirement.alternatives,
          delegated: requirement.delegated,
        });
      });
    });
    // Reverse axis: an action node must not evade the inventory merely by
    // losing its semantic HTML. This is deliberately direct-only; delegated
    // change/input listeners belong to their descendant controls, not the
    // container they are registered on.
    const actionEvents = ["click", "dragstart", "drop"];
    const nonSemanticActions = Array.from(root.querySelectorAll("*")).filter(
      (node) => !node.matches(semanticControlSelector)
        && actionEvents.some((eventName) =>
          window.__schedulerMatrixHasDirectEventHandler(node, eventName)))
      .map(descriptor);
    return { exists: true, checked, unwired, nonSemanticActions };
  }, [cardId, SEMANTIC_CONTROL_SELECTOR]);
  if (!observed.exists || !observed.checked || observed.unwired.length
      || observed.nonSemanticActions.length) {
    fail(`[${step}] ${cardId} independent repaint left action controls unwired: `
      + JSON.stringify(observed));
  }
  return observed.checked;
}

async function showCalendarDeleteVariant(page, step) {
  const observed = await page.evaluate((cardId) => {
    const model = cardDisplayModel(readCardState(cardId));
    const overview = model && model.payload && model.payload.overview;
    const candidate = overview && (overview.ice_slots || []).find((slot) =>
      slot.status === "available" && !slot.game_id && slot.start_time
        && slot.start_time > new Date().toISOString());
    if (!candidate) return { candidate: null, deleteControls: 0 };
    calendarDate = candidate.start_time.slice(0, 10);
    calendarMode = "day";
    repaintCalendarSurface(cardId);
    const root = document.querySelector(`[data-operational-card="${cardId}"]`);
    return {
      candidate: candidate.id,
      deleteControls: root ? root.querySelectorAll(
        `[data-del="ice-slot"][data-del-id="${candidate.id}"]`).length : 0,
    };
  }, CALENDAR_CARD);
  if (!observed.candidate || observed.deleteControls !== 1) {
    fail(`[${step}] could not establish one real future-slot Delete control: `
      + JSON.stringify(observed));
  }
}

function requestCount(tracker, method, pathname) {
  return tracker.requests.filter((row) => row.method === method
    && row.pathname === pathname).length;
}

// Establish two real Calendar-owned writes through the shipped controls: move
// a published fixture so its live Undo closure exists, then open generic
// ice-slot Delete so its live confirmation closure owns the Calendar identity
// while retaining the setup-delete transport.  The two fresh slots are fixture
// data only; every action under test is a production handler and real request.
async function prepareCalendarIntentWrites(page, tracker, fixture, step) {
  const fixtureSlots = await page.evaluate(async (seed) => {
    const F = window.hsFixture;
    const create = (what, start, end) => F.create(what, "/api/setup/ice-slot", {
      rink_id: seed.rink, start_time: start, end_time: end, slot_type: "game",
    });
    const source = await create("intent source ice",
      "2028-03-08T18:00:00+00:00", "2028-03-08T19:00:00+00:00");
    const move = await create("intent move ice",
      "2028-03-10T18:00:00+00:00", "2028-03-10T19:00:00+00:00");
    const remove = await create("intent delete ice",
      "2028-03-10T20:00:00+00:00", "2028-03-10T21:00:00+00:00");
    const game = await F.create("intent Calendar game", "/api/v2/setup/game", {
      season_id: seed.season, league_id: seed.league,
      division_id: seed.division, home_team_id: seed.home,
      away_team_id: seed.away, ice_slot_id: source.id,
    });
    await F.call("publish intent Calendar game", `/api/games/${game.id}/publish`, {});
    return { move: move.id, remove: remove.id, game: game.id };
  }, fixture);
  await page.evaluate(() => { void loadCalendarCard({ userInitiated: true }); });
  await waitForCardState(page, CALENDAR_CARD, "ready", `${step}/fixture-refresh`);
  await quiesce(page, tracker, `${step}/fixture-refresh`);

  const sourceAxis = await page.evaluate(([cardId, gameId]) => {
    const model = cardDisplayModel(readCardState(cardId));
    const overview = model && model.payload && model.payload.overview;
    const game = overview && (overview.schedule || []).find((row) =>
      row.game_id === gameId && row.ice_slot_id && row.start_time);
    return {
      source: game
        ? { gameId: game.game_id, date: game.start_time.slice(0, 10) } : null,
      schedule: (overview && overview.schedule || []).map((row) => ({
        game_id: row.game_id, ice_slot_id: row.ice_slot_id,
        start_time: row.start_time, published: row.published,
        keys: Object.keys(row).sort(),
      })),
    };
  }, [CALENDAR_CARD, fixtureSlots.game]);
  const source = sourceAxis.source;
  if (!source) fail(`[${step}] no real committed draft game exists for move/undo: `
    + JSON.stringify(sourceAxis.schedule));

  await page.evaluate(([cardId, date]) => {
    calendarDate = date; calendarMode = "day";
    repaintCalendarSurface(cardId);
  }, [CALENDAR_CARD, source.date]);
  const moveButton = page.locator(`[data-move-game="${source.gameId}"]`).first();
  if (await moveButton.count() !== 1) {
    fail(`[${step}] committed draft has no live Move control`);
  }
  await moveButton.click();
  await page.evaluate((cardId) => {
    calendarDate = "2028-03-10"; calendarMode = "day";
    repaintCalendarSurface(cardId);
  }, CALENDAR_CARD);

  const movePath = `/api/games/${source.gameId}/move`;
  const moved = page.waitForResponse((response) => response.request().method() === "POST"
    && new URL(response.url()).pathname === movePath);
  const target = page.locator(`[data-slot="${fixtureSlots.move}"]`).first();
  if (await target.count() !== 1) {
    fail(`[${step}] fresh move target has no live Calendar slot control`);
  }
  await target.click();
  const moveConfirm = page.locator("[data-move-confirm]").first();
  if (await moveConfirm.count() === 1) await moveConfirm.click();
  const moveResponse = await moved;
  let moveBody = null;
  try { moveBody = await moveResponse.json(); } catch (_) {}
  if (moveResponse.status() !== 200 || !moveBody || moveBody.error
      || !moveBody.moved || !moveBody.moved.old_slot_id) {
    fail(`[${step}] real Calendar move did not produce undo evidence: `
      + `${moveResponse.status()} ${JSON.stringify(moveBody)}`);
  }
  await quiesce(page, tracker, `${step}/move-settled`);
  await page.evaluate((cardId) => {
    calendarDate = "2028-03-10"; calendarMode = "day";
    repaintCalendarSurface(cardId);
  }, CALENDAR_CARD);

  const deleteButton = page.locator(
    `[data-del="ice-slot"][data-del-id="${fixtureSlots.remove}"]`).first();
  if (await deleteButton.count() !== 1) {
    fail(`[${step}] fresh spare slot has no live generic Delete control`);
  }
  await deleteButton.click();
  await page.waitForSelector("[data-del-confirm]");

  const captured = await page.evaluate(([cardId, gameId, removeId]) => {
    const root = document.querySelector(`[data-operational-card="${cardId}"]`);
    const undo = root && root.querySelector("[data-move-undo]");
    const confirm = document.querySelector("[data-del-confirm]");
    window.__schedulerMatrixIntentHandlers = { undo, confirm };
    return {
      undo: !!undo && typeof undo.onclick === "function",
      confirm: !!confirm && typeof confirm.onclick === "function",
      modal: modal && {
        type: modal.type, kind: modal.kind, id: modal.id,
        cardId: modal.cardIdentity && modal.cardIdentity.card,
        deleteTransport: modal.deleteTransport,
      },
      conflict: conflict && conflict.undo && {
        gid: conflict.undo.gid, oldSlotId: conflict.undo.oldSlotId,
      },
      paths: {
        undo: `/api/games/${gameId}/move`,
        remove: deleteRoute("ice-slot", removeId),
      },
    };
  }, [CALENDAR_CARD, source.gameId, fixtureSlots.remove]);
  if (!captured.undo || !captured.confirm
      || !captured.modal || captured.modal.type !== "confirm-delete"
      || captured.modal.kind !== "ice-slot" || captured.modal.id !== fixtureSlots.remove
      || captured.modal.cardId !== CALENDAR_CARD
      || captured.modal.deleteTransport !== "setup-delete"
      || !captured.conflict || captured.conflict.gid !== source.gameId
      || !captured.conflict.oldSlotId) {
    fail(`[${step}] Calendar ownership/transport axis was vacuous: `
      + JSON.stringify(captured));
  }
  captured.moveSlot = fixtureSlots.move;
  captured.removeSlot = fixtureSlots.remove;
  captured.gameId = source.gameId;
  return captured;
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
    const readyTeam1 = await makeTeam("Matrix A Ready 1", readyDiv.id);
    const readyTeam2 = await makeTeam("Matrix A Ready 2", readyDiv.id);
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
    // B needs its own non-empty option axes. The cross-card Review regression
    // below deliberately settles Draft under B while Review still retains A;
    // an empty B overview would let the old sibling-borrowing implementation
    // fall back to A's Review rows and pass without exercising the defect.
    const leagueB = await F.create("matrix competition League B", "/api/setup/level", {
      season_id: sb.id, name: "Matrix Silver",
    });
    const divB = await F.create("matrix Division B", "/api/setup/division", {
      season_id: sb.id, level_id: leagueB.id, name: "Matrix B Division",
    });
    const venueB = await F.create("matrix Arena B", "/api/setup/venue", {
      name: "Matrix B Arena", league_id: pb.id,
    });
    await F.call("grant matrix Arena B", `/api/v2/setup/seasons/${sb.id}/venue-access`, {
      venue_id: venueB.id,
    });
    const rinkB = await F.create("matrix rink B", "/api/setup/rink", {
      venue_id: venueB.id, name: "Matrix B Ice",
    });
    await F.selectProgramSeason("restore matrix Program A / Season A", pa.id, sa.id);
    return {
      pa: pa.id, sa: sa.id, pb: pb.id, sb: sb.id,
      readyDiv: readyDiv.id, raceDiv: raceDiv.id, rink: rink.id,
      league: league.id, readyTeam1, readyTeam2,
      divB: divB.id, rinkB: rinkB.id,
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
  await page.addInitScript(() => {
    const listeners = new WeakMap();
    const originalAdd = EventTarget.prototype.addEventListener;
    const originalRemove = EventTarget.prototype.removeEventListener;
    EventTarget.prototype.addEventListener = function addTrackedListener(
      type, listener, options) {
      if (listener) {
        let byType = listeners.get(this);
        if (!byType) {
          byType = new Map();
          listeners.set(this, byType);
        }
        let registered = byType.get(type);
        if (!registered) {
          registered = new Set();
          byType.set(type, registered);
        }
        registered.add(listener);
      }
      return originalAdd.call(this, type, listener, options);
    };
    EventTarget.prototype.removeEventListener = function removeTrackedListener(
      type, listener, options) {
      const byType = listeners.get(this);
      const registered = byType && byType.get(type);
      if (registered) registered.delete(listener);
      return originalRemove.call(this, type, listener, options);
    };
    window.__schedulerMatrixHasEventHandler = (node, type, root) => {
      for (let current = node; current; current = current.parentNode) {
        if (typeof current[`on${type}`] === "function") return true;
        const byType = listeners.get(current);
        if (byType && byType.get(type) && byType.get(type).size) return true;
        if (current === root) break;
      }
      return false;
    };
    window.__schedulerMatrixHasDirectEventHandler = (node, type) => {
      if (typeof node[`on${type}`] === "function") return true;
      const byType = listeners.get(node);
      return !!(byType && byType.get(type) && byType.get(type).size);
    };
  });
  const tracker = { inFlight: new Set(), sequence: 0, requests: [] };
  const nonOk = [];
  const requestFailures = [];
  const consoleErrors = [];
  page.on("request", (request) => {
    tracker.inFlight.add(request);
    tracker.sequence += 1;
    tracker.requests.push({
      method: request.method(),
      pathname: new URL(request.url()).pathname,
    });
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
    addIce: makeChannel("calendar add ice", ADD_ICE_RE),
    overview: makeChannel("calendar overview", OVERVIEW_RE),
    options: makeChannel("context options", CONTEXT_OPTIONS_RE),
    context: makeChannel("context switch", CONTEXT_RE),
  };
  const coverage = coverageLedger();
  const emptyRefreshCoverage = new Set();

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
    await assertEmptyRefresh(page, tracker, emptyRefreshCoverage,
      DRAFT_CARD, channels.overview,
      () => page.evaluate(() => { void loadSchedulerDraftCard({ userInitiated: true }); }),
      `${label}/draft-empty-refresh`);
    await assertEmptyRefresh(page, tracker, emptyRefreshCoverage,
      REVIEW_CARD, channels.drafts,
      () => page.evaluate(() => { void loadSchedulerReviewCard({ userInitiated: true }); }),
      `${label}/review-empty-refresh`);

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
    await assertCardActionsWired(page, DRAFT_CARD,
      `${label}/draft-ready-actions`);

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
    await assertCardActionsWired(page, REVIEW_CARD,
      `${label}/review-ready-actions`);
    await assertReviewFilterEdges(page, `${label}/review-filter-edges`);

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
    const draftBDivisions = draftBPayload.body && draftBPayload.body.divisions || [];
    const draftBRinks = draftBPayload.body && draftBPayload.body.rinks || [];
    if (draftBPayload.status !== 200 || reviewBPayload.status !== 200
        || !draftBDivisions.some((row) => row.id === ids.divB
          && row.name === "Matrix B Division")
        || !draftBRinks.some((row) => row.id === ids.rinkB
          && row.name === "Matrix B Ice")) {
      fail(`[${label}/scheduler-stale] B replacement reads were not successful: `
        + JSON.stringify({ draftBPayload, reviewBPayload,
          expectedBAxes: { division: ids.divB, rink: ids.rinkB } }));
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
    await waitForCardState(page, DRAFT_CARD, "empty", `${label}/draft-b-empty`);
    await waitForReleased(page, channels.overview, draftBReleased,
      `${label}/draft-b-empty`);
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

    // Review is still showing A while Draft has independently settled B.
    // Exercise Review's own local filter repaint and require both option axes
    // to equal the ids/names derived from Review's rows exactly. Borrowing the
    // sibling Draft overview here leaks B names and ids into the retained A
    // card even though each card's response guard is individually correct.
    await page.selectOption("#sched-filter-issue", "issues");
    const reviewFilterAxis = await page.evaluate(([reviewCardId, forbiddenDiv, forbiddenRink]) => {
      const model = cardDisplayModel(readCardState(reviewCardId));
      const drafts = model && model.payload && model.payload.drafts || [];
      const expected = (idKey, nameKey) => Array.from(new Map(drafts.map((row) => {
        const id = schedulerReviewFilterValue(row[idKey]);
        return [id, { id, name: id === SCHEDULER_REVIEW_UNASSIGNED_FILTER
          ? "Unassigned" : (row[nameKey] || row[idKey]) }];
      })).values()).sort((a, b) => String(a.id).localeCompare(String(b.id)));
      const actual = (selector) => Array.from(document.querySelectorAll(
        `${selector} option:not([value="all"])`)).map((option) => ({
        id: option.value,
        name: option.textContent.trim(),
      })).sort((a, b) => String(a.id).localeCompare(String(b.id)));
      return {
        reviewState: readCardState(reviewCardId).state,
        expectedDivisions: expected("division_id", "division_name"),
        actualDivisions: actual("#sched-filter-div"),
        expectedRinks: expected("rink_id", "rink_name"),
        actualRinks: actual("#sched-filter-rink"),
        borrowedB: document.querySelector(
          `#sched-filter-div option[value="${CSS.escape(
            schedulerReviewFilterValue(forbiddenDiv))}"],`
          + `#sched-filter-rink option[value="${CSS.escape(
            schedulerReviewFilterValue(forbiddenRink))}"]`) !== null,
        draftSibling: JSON.parse(JSON.stringify(readCardState("scheduler/draft"))),
      };
    }, [REVIEW_CARD, ids.divB, ids.rinkB]);
    if (reviewFilterAxis.reviewState !== "stale"
        || JSON.stringify(reviewFilterAxis.actualDivisions)
          !== JSON.stringify(reviewFilterAxis.expectedDivisions)
        || JSON.stringify(reviewFilterAxis.actualRinks)
          !== JSON.stringify(reviewFilterAxis.expectedRinks)
        || !reviewFilterAxis.expectedDivisions.length
        || !reviewFilterAxis.expectedRinks.length
        || reviewFilterAxis.borrowedB
        || reviewFilterAxis.draftSibling.state !== "empty") {
      fail(`[${label}/review-filter-axis] Review borrowed a sibling tuple or `
        + `the oracle was vacuous: ${JSON.stringify(reviewFilterAxis)}`);
    }
    heldBReview.release();
    await waitForCardState(page, REVIEW_CARD, "empty", `${label}/review-b-empty`);
    await waitForReleased(page, channels.drafts, reviewBReleased,
      `${label}/review-b-empty`);
    await assertContextChrome(page, {
      programId: ids.pb, seasonId: ids.sb,
      programName: "Matrix Program B", seasonName: "Matrix Season B",
    }, `${label}/scheduler-settled-chrome`);
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
    await showCalendarDeleteVariant(page, `${label}/calendar-delete-variant`);
    await assertCardActionsWired(page, CALENDAR_CARD,
      `${label}/calendar-ready-actions`);

    // Persist a real Calendar write under A, then withhold its delivery while
    // the server commits B but the context echo is still pending in the app.
    // The response must remain a byte-for-byte no-op until that intent settles;
    // otherwise its follow-up overview GET is answered under B and committed
    // beneath the still-A card identity.
    const calendarAddGap = armHold(channels.addIce);
    await page.click("[data-addslot]");
    const calendarAddPayload = await calendarAddGap.captured;
    if (calendarAddPayload.status !== 200 || !calendarAddPayload.body
        || calendarAddPayload.body.error) {
      fail(`[${label}/calendar-write-gap] Add ice did not perform a real A `
        + `write: ${JSON.stringify(calendarAddPayload)}`);
    }
    const calendarContextGap = armHold(channels.context);
    await startContextSwitch(page, ids.pb, ids.sb,
      `${label}/calendar-write-gap-switch`);
    const calendarContextPayload = await calendarContextGap.captured;
    if (calendarContextPayload.status !== 200 || !calendarContextPayload.body
        || calendarContextPayload.body.program_id !== ids.pb
        || calendarContextPayload.body.season_id !== ids.sb) {
      fail(`[${label}/calendar-write-gap] B context did not commit while its `
        + `echo was held: ${JSON.stringify(calendarContextPayload)}`);
    }
    await page.focus("#ctx-select");
    await resetAnnouncements(page);
    const calendarBeforeWriteRelease = await immutableSnapshot(page, CALENDAR_CARD);
    const overviewBeforeWriteRelease = channels.overview.released;
    const calendarAddReleased = channels.addIce.released;
    calendarAddGap.release();
    await waitForReleased(page, channels.addIce, calendarAddReleased,
      `${label}/calendar-write-gap-release`);
    await quiesce(page, tracker, `${label}/calendar-write-gap-pending`, 1);
    const calendarDuringWriteGap = await immutableSnapshot(page, CALENDAR_CARD);
    assertByteEqual(`${label}/calendar-write-gap`, calendarBeforeWriteRelease,
      calendarDuringWriteGap);
    if (channels.overview.released !== overviewBeforeWriteRelease) {
      fail(`[${label}/calendar-write-gap] Calendar launched a follow-up read `
        + `before the context echo settled`);
    }

    // Accepting B invalidates the A write. Hold B's own replacement read so
    // the existing STALE oracle observes that transition explicitly.
    const calendarB = armHold(channels.overview);
    const calendarContextReleased = channels.context.released;
    calendarContextGap.release();
    await waitForReleased(page, channels.context, calendarContextReleased,
      `${label}/calendar-write-gap-context-release`);
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
    await assertEmptyRefresh(page, tracker, emptyRefreshCoverage,
      CALENDAR_CARD, channels.overview,
      () => page.evaluate(() => { void loadCalendarCard({ userInitiated: true }); }),
      `${label}/calendar-empty-refresh`);
    await assertContextChrome(page, {
      programId: ids.pb, seasonId: ids.sb,
      programName: "Matrix Program B", seasonName: "Matrix Season B",
    }, `${label}/calendar-settled-chrome`);
    await selectProgramSeason(page, `${label}: calendar return to A`, ids.pa, ids.sa);

    // A context INTENT must close the wire, not merely remove today's nodes.
    // Create a real move/Undo and a real Calendar-owned generic Delete modal,
    // retain their shipped handler closures, then invoke those detached
    // closures while B's successful context echo is held. Neither POST may be
    // observable. Once B is accepted, the old modal/conflict must be erased
    // rather than resurfacing when A is visited again.
    trace(`${label}: Calendar writes are refused before dispatch during context intent`);
    await waitForCardState(page, CALENDAR_CARD, "ready",
      `${label}/calendar-intent-a-ready`);
    await quiesce(page, tracker, `${label}/calendar-intent-a-ready`);
    const intentWrites = await prepareCalendarIntentWrites(
      page, tracker, {
        rink: ids.rink, season: ids.sa, league: ids.league,
        division: ids.readyDiv, home: ids.readyTeam1, away: ids.readyTeam2,
      }, `${label}/calendar-intent-prepare`);
    const intentCounts = {
      undo: requestCount(tracker, "POST", intentWrites.paths.undo),
      remove: requestCount(tracker, "POST", intentWrites.paths.remove),
    };

    const calendarIntent = armHold(channels.context);
    await startContextSwitch(page, ids.pb, ids.sb,
      `${label}/calendar-intent-switch`);
    const calendarIntentEcho = await calendarIntent.captured;
    if (calendarIntentEcho.status !== 200 || !calendarIntentEcho.body
        || calendarIntentEcho.body.program_id !== ids.pb
        || calendarIntentEcho.body.season_id !== ids.sb) {
      fail(`[${label}/calendar-intent] held echo was not a real accepted B switch: `
        + JSON.stringify(calendarIntentEcho));
    }
    const pendingIntent = await page.evaluate(([cardId, removeId]) => ({
      intent: contextSwitchIntentPending,
      liveUndo: document.querySelectorAll("[data-move-undo]").length,
      liveConfirm: document.querySelectorAll("[data-del-confirm]").length,
      retainedModal: !!modal && modal.type === "confirm-delete"
        && modal.id === removeId && modal.deleteTransport === "setup-delete"
        && modal.cardIdentity && modal.cardIdentity.card === cardId,
      retainedUndo: !!(conflict && conflict.undo),
      handlers: !!window.__schedulerMatrixIntentHandlers
        && !!window.__schedulerMatrixIntentHandlers.undo
        && !!window.__schedulerMatrixIntentHandlers.confirm,
    }), [CALENDAR_CARD, intentWrites.removeSlot]);
    if (!pendingIntent.intent || pendingIntent.liveUndo !== 0
        || pendingIntent.liveConfirm !== 0 || !pendingIntent.retainedModal
        || !pendingIntent.retainedUndo || !pendingIntent.handlers) {
      fail(`[${label}/calendar-intent] intent did not withdraw live controls while `
        + `preserving state until acceptance: ${JSON.stringify(pendingIntent)}`);
    }

    await page.evaluate(async () => {
      const retained = window.__schedulerMatrixIntentHandlers;
      // Undo's DOM handler deliberately launches its async commit without
      // returning it. Invoke both closures exactly as detached controls would;
      // do not await Delete either, because a broken pre-fetch guard would then
      // correctly wait on context settlement and deadlock the oracle itself.
      retained.undo.onclick();
      retained.confirm.onclick();
      await new Promise((resolve) => setTimeout(resolve, 50));
    });
    await page.waitForTimeout(100);
    const afterDetachedClicks = {
      undo: requestCount(tracker, "POST", intentWrites.paths.undo),
      remove: requestCount(tracker, "POST", intentWrites.paths.remove),
    };
    if (JSON.stringify(afterDetachedClicks) !== JSON.stringify(intentCounts)) {
      fail(`[${label}/calendar-intent] a detached Calendar handler dispatched `
        + `after intent began: before ${JSON.stringify(intentCounts)}, after `
        + JSON.stringify(afterDetachedClicks));
    }

    const calendarIntentB = armHold(channels.overview);
    const calendarIntentReleased = channels.context.released;
    calendarIntent.release();
    await waitForReleased(page, channels.context, calendarIntentReleased,
      `${label}/calendar-intent-context-release`);
    const calendarIntentBPayload = await calendarIntentB.captured;
    if (calendarIntentBPayload.status !== 200 || !calendarIntentBPayload.body) {
      fail(`[${label}/calendar-intent] replacement B read was not real: `
        + JSON.stringify(calendarIntentBPayload));
    }
    await waitForSelectedTuple(page, ids.pb, ids.sb, true,
      `${label}/calendar-intent-b-selected`);
    await waitForCardState(page, CALENDAR_CARD, "stale",
      `${label}/calendar-intent-b-stale`);
    const acceptedCleanup = await page.evaluate(() => ({
      modal: modal,
      conflict: conflict,
      wizard: wizard,
      pendingMove: pendingMove,
      movingGameId: movingGameId,
      liveConfirm: document.querySelectorAll("[data-del-confirm]").length,
      liveUndo: document.querySelectorAll("[data-move-undo]").length,
      retainedConnected: Object.values(window.__schedulerMatrixIntentHandlers || {})
        .some((node) => node && node.isConnected),
    }));
    if (acceptedCleanup.modal !== null || acceptedCleanup.conflict !== null
        || acceptedCleanup.wizard !== null || acceptedCleanup.pendingMove !== null
        || acceptedCleanup.movingGameId !== null
        || acceptedCleanup.liveConfirm !== 0 || acceptedCleanup.liveUndo !== 0
        || acceptedCleanup.retainedConnected) {
      fail(`[${label}/calendar-intent] accepted move retained old Calendar state: `
        + JSON.stringify(acceptedCleanup));
    }
    const calendarIntentBReleased = channels.overview.released;
    calendarIntentB.release();
    await waitForReleased(page, channels.overview, calendarIntentBReleased,
      `${label}/calendar-intent-b-release`);
    await waitForCardState(page, CALENDAR_CARD, "empty",
      `${label}/calendar-intent-b-empty`);
    await selectProgramSeason(page, `${label}: calendar intent return to A`,
      ids.pa, ids.sa);
    await waitForCardState(page, CALENDAR_CARD, "ready",
      `${label}/calendar-intent-a-return`);
    await quiesce(page, tracker, `${label}/calendar-intent-a-return`);
    const noResurface = await page.evaluate(([cardId, gameId, moveSlot, removeSlot]) => {
      const model = cardDisplayModel(readCardState(cardId));
      const overview = model && model.payload && model.payload.overview;
      const game = overview && (overview.schedule || []).find(
        (row) => row.game_id === gameId);
      const spare = overview && (overview.ice_slots || []).find(
        (row) => row.id === removeSlot);
      return {
        modal: modal,
        conflict: conflict,
        liveConfirm: document.querySelectorAll("[data-del-confirm]").length,
        liveUndo: document.querySelectorAll("[data-move-undo]").length,
        movedSlot: game && game.ice_slot_id,
        spareStillExists: !!spare,
        expectedMoveSlot: moveSlot,
      };
    }, [CALENDAR_CARD, intentWrites.gameId, intentWrites.moveSlot,
      intentWrites.removeSlot]);
    if (noResurface.modal !== null || noResurface.conflict !== null
        || noResurface.liveConfirm !== 0 || noResurface.liveUndo !== 0
        || noResurface.movedSlot !== noResurface.expectedMoveSlot
        || !noResurface.spareStillExists) {
      fail(`[${label}/calendar-intent] accepted A -> B -> A resurrected state or `
        + `a refused write reached storage: ${JSON.stringify(noResurface)}`);
    }

    // Ice Builder: opening a fresh builder is the explicit "no preview yet"
    // EMPTY state. A successful zero-slot preview is still reviewed data (it
    // may explain duplicates/conflicts), so it correctly belongs to READY and
    // is not fabricated into a second meaning for EMPTY merely for coverage.
    trace(`${label}: Ice Builder EMPTY, ERROR, LOADING, READY and STALE`);
    await openBuilder(page, `${label}/builder`);
    await assertState(page, coverage, BUILDER_CARD, "empty", `${label}/builder-empty`);
    await assertEmptyRefresh(page, tracker, emptyRefreshCoverage,
      BUILDER_CARD, channels.overview,
      () => page.evaluate(() => { void loadIceBuilderCard({ userInitiated: true }); }),
      `${label}/builder-empty-refresh`);
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
    // A user can move to the persistent context selector while a card write is
    // in flight. Settlement must respect that connected focus rather than
    // pulling it back into the card merely because the request was initiated
    // by the user.
    await page.focus("#ctx-select");
    const builderReleased = channels.ice.released;
    builderRetry.release();
    await waitForCardState(page, BUILDER_CARD, "ready", `${label}/builder-ready`);
    await waitForReleased(page, channels.ice, builderReleased, `${label}/builder-ready`);
    const focusAfterIntentionalMove = await page.evaluate(() => ({
      id: document.activeElement && document.activeElement.id,
      tag: document.activeElement && document.activeElement.tagName,
    }));
    if (focusAfterIntentionalMove.id !== "ctx-select") {
      fail(`[${label}/builder-focus-preserve] settlement stole intentional `
        + `context-selector focus: ${JSON.stringify(focusAfterIntentionalMove)}`);
    }
    await assertState(page, coverage, BUILDER_CARD, "ready", `${label}/builder-ready`,
      "Matrix A Ice");

    // Positive control: when the initiating Preview button is removed and
    // focus genuinely falls to BODY, settlement restores it inside this card.
    const builderFocusRestore = armHold(channels.ice);
    await page.click("[data-ib-preview]");
    const builderFocusPayload = await builderFocusRestore.captured;
    if (builderFocusPayload.status !== 200 || !builderFocusPayload.body
        || !builderFocusPayload.body.totals) {
      fail(`[${label}/builder-focus-restore] held Preview was vacuous: `
        + JSON.stringify(builderFocusPayload));
    }
    await waitForCardState(page, BUILDER_CARD, "loading",
      `${label}/builder-focus-restore-loading`);
    const focusWhileReplaced = await page.evaluate(() =>
      document.activeElement && document.activeElement.tagName);
    if (focusWhileReplaced !== "BODY") {
      fail(`[${label}/builder-focus-restore] initiating control replacement `
        + `did not strand focus on BODY: ${focusWhileReplaced}`);
    }
    const builderFocusReleased = channels.ice.released;
    builderFocusRestore.release();
    await waitForReleased(page, channels.ice, builderFocusReleased,
      `${label}/builder-focus-restore-release`);
    await waitForCardState(page, BUILDER_CARD, "ready",
      `${label}/builder-focus-restore-ready`);
    const focusRestored = await page.evaluate((cardId) => {
      const active = document.activeElement;
      const owner = active && active.closest
        ? active.closest("[data-operational-card]") : null;
      return { tag: active && active.tagName,
        card: owner && owner.getAttribute("data-operational-card") };
    }, BUILDER_CARD);
    if (focusRestored.card !== BUILDER_CARD || focusRestored.tag === "BODY") {
      fail(`[${label}/builder-focus-restore] lost focus after the initiating `
        + `control was replaced: ${JSON.stringify(focusRestored)}`);
    }
    await assertCardActionsWired(page, BUILDER_CARD,
      `${label}/builder-ready-actions`);

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
    await assertContextChrome(page, {
      programId: ids.pb, seasonId: ids.sb,
      programName: "Matrix Program B", seasonName: "Matrix Season B",
    }, `${label}/builder-stale-chrome`);
    await page.focus("#ctx-select");
    const builderBReleased = channels.overview.released;
    builderB.release();
    await waitForReleased(page, channels.overview, builderBReleased,
      `${label}/builder-stale-release`);
    await quiesce(page, tracker, `${label}/builder-stale-settled`);
    await assertContextChrome(page, {
      programId: ids.pb, seasonId: ids.sb,
      programName: "Matrix Program B", seasonName: "Matrix Season B",
    }, `${label}/builder-settled-chrome`);
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
    if (insideEpochWindow.text.includes("Matrix A Race")
        || insideEpochWindow.mutations.length) {
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

    // EMPTY can retain authoritative history. Both later retained states must
    // keep presenting it as EMPTY rather than serializing the READY-only
    // zero-row table and claiming that no rows match local filters.
    failOnce(channels.drafts);
    await page.evaluate(() => {
      void loadSchedulerReviewCard({ userInitiated: true });
    });
    await waitForCardState(page, REVIEW_CARD, "error",
      `${label}/review-empty-error`);
    const errorRetainedEmpty = await cardSnapshot(page, REVIEW_CARD);
    if (!errorRetainedEmpty.html.includes('data-card-empty="review"')
        || errorRetainedEmpty.text.includes("No draft games match these filters")
        || !errorRetainedEmpty.text.includes(
          `${publishedSummary.published_count} published`)) {
      fail(`[${label}/review-empty-error] ERROR misrepresented retained EMPTY: `
        + JSON.stringify(errorRetainedEmpty));
    }
    await activateRetryWithKeyboard(page, REVIEW_CARD,
      `${label}/review-empty-error-retry`);
    await waitForCardState(page, REVIEW_CARD, "empty",
      `${label}/review-empty-error-recovered`);

    const retainedEmptyB = armHold(channels.drafts);
    await startContextSwitch(page, ids.pb, ids.sb,
      `${label}/review-empty-stale-switch`);
    const retainedEmptyBPayload = await retainedEmptyB.captured;
    if (retainedEmptyBPayload.status !== 200 || !retainedEmptyBPayload.body) {
      fail(`[${label}/review-empty-stale] B replacement read was not real: `
        + JSON.stringify(retainedEmptyBPayload));
    }
    await waitForSelectedTuple(page, ids.pb, ids.sb, true,
      `${label}/review-empty-stale-selected`);
    await waitForCardState(page, REVIEW_CARD, "stale",
      `${label}/review-empty-stale`);
    const staleRetainedEmpty = await cardSnapshot(page, REVIEW_CARD);
    if (!staleRetainedEmpty.html.includes('data-card-empty="review"')
        || staleRetainedEmpty.text.includes("No draft games match these filters")
        || !staleRetainedEmpty.text.includes(
          `${publishedSummary.published_count} published`)) {
      fail(`[${label}/review-empty-stale] STALE misrepresented retained EMPTY: `
        + JSON.stringify(staleRetainedEmpty));
    }
    const retainedEmptyBReleased = channels.drafts.released;
    retainedEmptyB.release();
    await waitForReleased(page, channels.drafts, retainedEmptyBReleased,
      `${label}/review-empty-stale-release`);
    await waitForCardState(page, REVIEW_CARD, "empty",
      `${label}/review-empty-stale-settled`);

    // Exercise the nested EMPTY provenance that only exists on a real tuple
    // round trip: origin EMPTY -> away STALE while its read is held -> origin
    // LOADING retaining that STALE/staleFrom=EMPTY model. Derive the complete
    // target axis from production so a fifth card cannot silently miss this
    // boundary. Program B is the origin for this block because its Scheduler
    // and Calendar reads are authoritatively empty; "origin/away" describe
    // the transition roles, independently of fixture letter names.
    trace(`${label}: every EMPTY card remains truthful across A → B → A`);
    const emptyRoundTripCards = await page.evaluate(() =>
      Array.from(SCHEDULE_FACILITY_CARD_IDS));
    const emptyRoundTripPlans = new Map([
      [DRAFT_CARD, {
        channel: channels.overview, skippedSiblingReads: 0,
        open: () => openView(page, "scheduler", [DRAFT_CARD, REVIEW_CARD],
          `${label}/empty-aba-draft-open`),
      }],
      [REVIEW_CARD, {
        channel: channels.drafts, skippedSiblingReads: 0,
        open: () => openView(page, "scheduler", [DRAFT_CARD, REVIEW_CARD],
          `${label}/empty-aba-review-open`),
      }],
      [BUILDER_CARD, {
        channel: channels.overview, skippedSiblingReads: 1,
        open: () => openBuilder(page, `${label}/empty-aba-builder-open`),
      }],
      [CALENDAR_CARD, {
        channel: channels.overview, skippedSiblingReads: 0,
        open: () => openView(page, "calendar", [CALENDAR_CARD],
          `${label}/empty-aba-calendar-open`),
      }],
    ]);
    const plannedEmptyRoundTripCards = Array.from(emptyRoundTripPlans.keys()).sort();
    if (JSON.stringify(emptyRoundTripCards.slice().sort())
        !== JSON.stringify(plannedEmptyRoundTripCards)) {
      fail(`[${label}/empty-aba] production card axis has no exact test plan: `
        + `${JSON.stringify(emptyRoundTripCards)}`);
    }
    const emptyOrigin = { programId: ids.pb, seasonId: ids.sb };
    const emptyAway = { programId: ids.pa, seasonId: ids.sa };
    const emptyRoundTripRepresentationFailures = [];
    for (const cardId of emptyRoundTripCards) {
      const plan = emptyRoundTripPlans.get(cardId);
      await selectProgramSeason(page, `${label}: ${cardId} EMPTY origin`,
        emptyOrigin.programId, emptyOrigin.seasonId);
      await plan.open();
      await quiesce(page, tracker, `${label}/empty-aba-${cardId}/prepared`);
      await waitForCardState(page, cardId, "empty",
        `${label}/empty-aba-${cardId}/prepared-empty`);
      const representationFailures = await assertEmptyRoundTripLoading(
        page, tracker, cardId, plan.channel,
        channels.context, plan.skippedSiblingReads, emptyOrigin, emptyAway,
        `${label}/empty-aba-${cardId}`);
      emptyRoundTripRepresentationFailures.push(...representationFailures);
      if (cardId === BUILDER_CARD) {
        const cancel = page.locator("[data-ib-cancel]");
        if (await cancel.count() !== 1) {
          fail(`[${label}/empty-aba-builder] settled Builder has no Back control`);
        }
        await cancel.click();
        await page.waitForSelector(operationalSelector(CALENDAR_CARD), {
          timeout: 15000,
        });
        await quiesce(page, tracker, `${label}/empty-aba-builder/closed`);
      }
    }
    if (emptyRoundTripRepresentationFailures.length) {
      fail(`[${label}/empty-aba] retained EMPTY presentation failed on `
        + `${emptyRoundTripRepresentationFailures.length}/${emptyRoundTripCards.length} `
        + `card legs: ${emptyRoundTripRepresentationFailures.join("; ")}`);
    }

    const checked = coverage.assertComplete(label);
    const emptyRefreshAxis = Array.from(emptyRefreshCoverage).sort();
    const expectedEmptyRefreshAxis = CARD_IDS.slice().sort();
    if (JSON.stringify(emptyRefreshAxis) !== JSON.stringify(expectedEmptyRefreshAxis)) {
      fail(`[${label}] EMPTY-refresh axis shrank: expected `
        + `${JSON.stringify(expectedEmptyRefreshAxis)}, got `
        + JSON.stringify(emptyRefreshAxis));
    }

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
