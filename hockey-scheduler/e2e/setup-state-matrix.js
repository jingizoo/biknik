// The PER-CARD STATE MATRIX for the six Setup workflow LANDINGS (#365).
//
// #365, verbatim, is a matrix requirement rather than a defect report:
//
//   "States per card: loading / empty / stale / per-card error plus retry /
//   confirmation / success or complete / optional for Workflow 6."
//   "Home/Tasks and every Setup landing render the full applicable state set
//   with stable semantic headings and status text."
//   "Empty states explain what is missing and expose only the authorized
//   primary action."
//   "Per-card failures never blank unrelated cards; retry is keyboard
//   reachable and scoped to that card."
//   "Confirmation and success announcements use the existing live-region
//   mechanism without duplicate speech."
//   "Stale responses cannot replace newer context data or restore an obsolete
//   primary action."
//   "Workflow 6 remains always visible and reachable, is neither done nor
//   todo, is never the next recommendation, and never blocks overall
//   completion."
//   "Use real production entry points and forced transport outcomes rather
//   than helper-only assertions."
//   "For Home/Tasks and each of six workflows cover every applicable state at
//   desktop and 390x844, including keyboard activation and exact focus after
//   retry, confirmation, and completion."
//
// This file is the SETUP half of that: the six workflow landings
// (league_season, teams, participation, roster, facilities, import) and the
// hub grid they share their card models with. The Home/Tasks half lives in
// its own journey.
//
// ============================ WHAT IS COVERED =============================
// Every state below is reached through a REAL production entry point — the
// hub's own "Open …" button, the landing's own Retry/Refresh, the landing's
// own primary control, the real #ctx-select — with the TRANSPORT forced
// (route interception) rather than by calling an internal helper. Nothing in
// this file calls commitCardState, buildSetupWorkflowCardModel,
// retrySetupWorkflowCard or render() directly; the page's own functions are
// READ (readCardState, cardGenerations, setupHubRollup) only to assert what
// the operator's actions produced.
//
//   LOADING        the landing's own Retry pressed with /api/v2/setup/overview
//                  HELD (leg 5). Asserted as the state's contract: skeleton
//                  with its visually-hidden label, aria-busy="true", and ZERO
//                  controls on both the card body and the landing's action
//                  groups — with the action container asserted PRESENT so
//                  "no buttons" is never satisfied by a missing container.
//   EMPTY          a pristine zero-Program installation (leg 1). The inherited
//                  ruling keeps `league_season` EMPTY at both viewports, and
//                  the same pass covers the other four required workflows.
//                  Asserted: the sentence NAMES what is missing (this
//                  workflow's own count labels) AND the unmet prerequisite,
//                  and EXACTLY ONE action is offered.
//   STALE          a REAL context switch through #ctx-select with
//                  /api/v2/setup/progress held open, so the window in which
//                  render() has repainted the retained cards but not yet
//                  committed the new tuple's models is observable rather than
//                  raced (leg 7a). Asserted: retained counts still visible,
//                  labelled as earlier data, a Refresh in the card body,
//                  every landing action group withdrawn, and
//                  contextSwitchIntentPending ALREADY FALSE — so the
//                  withdrawal is attributable to STALE and not to the switch.
//   ERROR + RETRY  /api/v2/setup/overview forced to 500 (legs 4/5), plus the
//                  two other read failures a card can have: /api/players for
//                  `roster` and /api/v2/setup/progress for every required
//                  workflow. Retry is reached BY TABBING from the landing's
//                  own back control and activated with Enter; the announcement
//                  and the EXACT focused element are asserted on both the
//                  success and the failure outcome.
//   CONFIRMATION   the two declared confirmations, both keyboard-driven:
//                  Workflow 6's "Initial Setup wizard" (leg 8a) and the
//                  derived reopen on `facilities`/`participation` under an
//                  archived Season (legs 8b/8c). Exact focus asserted on open,
//                  on cancel, on the blank-reason refusal and on completion;
//                  the live region is read with a MutationObserver so
//                  "exactly once" is what a screen reader would be handed.
//   SUCCESS        a fully provisioned Program whose five required workflows
//                  all report `done` (leg 2), and the reopen's own completion
//                  (leg 8c).
//   OPTIONAL       Workflow 6, in every phase this file visits (leg 3 and the
//                  `import` arm of every other leg).
//
// Plus the four cross-cutting properties the issue names:
//
//   ONE FAILED CARD BESIDE SUCCESSFUL CARDS (leg 4). On the hub grid — the
//   only surface where a card has neighbours — a per-card retry that FAILS
//   leaves every other card's generation, committed model and painted body
//   BYTE-IDENTICAL, beside a neighbour that has already recovered.
//   FAILED RETRY THEN SUCCESSFUL RETRY (leg 4), scoped to that card.
//   DELAYED STALE SUCCESS AFTER A NEWER FAILURE (leg 6) and AFTER A CONTEXT
//   SWITCH (leg 7b): the older response must not replace the newer data and
//   must not restore the obsolete primary action.
//   ZERO CONSOLE ERRORS, through the delivery reconciler below.
//
// ============================ WHAT IS *NOT* ===============================
// Stated precisely, because an unstated gap is indistinguishable from an
// untested one:
//
//   * Workflow 6 has no inventory of its own (SETUP_WORKFLOWS declares no
//     `summary` for it), so EMPTY and ERROR are not merely untested — they are
//     UNREACHABLE, and leg 3 asserts that: with the setup overview, the player
//     list AND the progress read ALL forced to 500 at once, `import` stays
//     READY, reachable and optional. Its STALE and LOADING, by contrast, ARE
//     reachable — readCardState() downgrades any held model whose tuple moved,
//     and the Refresh that STALE offers re-reads the progress route — so both
//     are asserted, together, in leg 7a (leg 5's per-landing LOADING pass
//     cannot reach it, because that pass gets there through an ERROR this
//     workflow can never be in). This is the inherited ruling applied, not
//     relitigated: "cover its OPTIONAL semantics, not a full data state set."
//   * The delayed-stale-response race is inapplicable to Workflow 6 and leg 7b
//     says why by ASSERTION rather than by omission: its committed model is
//     byte-identical under both tuples apart from the identity record, so no
//     response of its own could carry data to corrupt.
//   * CONFIRMATION is inapplicable to `league_season`, `teams` and `roster`:
//     no action on those workflows — declared or derived — carries a `confirm`.
//     Leg 8d asserts that structurally (zero confirmation controls, under BOTH
//     an ordinary and an archived Season) with the three confirmable landings
//     as the control case, rather than manufacturing a confirmation for them.
//   * The unauthorized cross-workflow `open` branch is unreachable by design
//     (inherited ruling) and is not manufactured here.
//   * ONE OPEN QUESTION, recorded rather than decided. Workflow 6's
//     confirmation completes by NAVIGATING, and resolveSetupCardConfirm()
//     announces its `done` sentence and then calls runSetupWorkflowGo() ->
//     switchTab() synchronously — switchTab sets `toast = ""` and render()
//     begins with updateToast(), so the live region is populated and emptied
//     inside the SAME task, before the browser paints. Leg 8a therefore
//     asserts that the sentence was WRITTEN to the region exactly once (which
//     is a real and checkable claim, and the reason the recorder below reads
//     mutation records rather than the region's settled state) and asserts
//     nothing about whether it survived. Whether a polite live region written
//     and withdrawn within one task is spoken is not something an automated
//     browser journey can determine, and encoding either answer here would be
//     asserting a conclusion this file has not earned. Every OTHER
//     announcement in this journey — the confirmation prompts, the cancel
//     sentences, the retry outcomes and the reopen completion — stays standing
//     and is asserted normally.
//   * This journey drives a real browser with real keyboard events. It is NOT
//     a screen-reader session and NOT a moderated human session, and nothing
//     here should be read as one: what it asserts about assistive technology is
//     confined to what the DOM and the live region actually contain.
//
// ==================== ENGINEERING RULES THIS FILE FOLLOWS =================
// Inherited from e2e/setup-card-write-identity.js (copied, never imported, so
// the two files stay independent):
//
//   * HELD RESPONSES ARE CAPTURED FIRST. `const r = await route.fetch()`, hold,
//     then `route.fulfill({ response: r })`. Delaying the REQUEST would merely
//     make the server answer later and would prove nothing about a response
//     arriving into a context that has moved on.
//   * THE DELIVERY RECONCILER. Every non-2xx is recorded with method +
//     absolute URL + status; every deliberate failure is an allowance keyed to
//     (method, URL, status) and consumed AT MOST ONCE; reconciliation happens
//     at end-of-viewport, never live. Unmatched responses, requestfailed,
//     failed-resource console lines and UNDELIVERED injections all fail the
//     run. A fungible "ignore the next console error" counter is forbidden.
//   * QUIESCENCE BEFORE ACTING. Every sample and every keyboard action is
//     taken on a page with no request in flight and none started for 300ms.
//     This is not politeness: an in-flight render commits a fresh model for
//     EVERY card, so a snapshot taken under one turns the neighbour-isolation
//     assertion into a coin flip. It was observed doing exactly that while this
//     journey was being built.
//   * CONTEXT SWITCHES WAIT FOR RECONCILIATION, not for the POST echo: the
//     confirmed tuple (program AND season), contextSwitchIntentPending ===
//     false, AND a repaint observed on #content with `subtree: false`. A
//     subtree observer is satisfied by the retain-the-cards pass and returns
//     while the action groups are still withdrawn for a reason that has
//     nothing to do with the card.
//   * ANTI-VACUITY IS MANDATORY. Every negative assertion in this file is
//     paired with the positive control that proves it could have failed:
//     "exactly one action in EMPTY" is asserted beside the same landings
//     offering two to four when nothing is blocked; "zero controls" is always
//     asserted together with the container being structurally PRESENT; the
//     discarded stale response is always shown, in a control run, to be one
//     that DOES change the card when nothing supersedes it; and the
//     generation counter is asserted to have really moved underneath it.
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
// Ports not used by any other journey in this directory (the highest in use
// elsewhere at the time of writing is 8396, plus the 84xx/85xx/86xx/87xx
// blocks at 8441/8442, 8541/8542, 8641/8642 and 8701-8704).
const VIEWPORTS = [
  { label: "desktop", width: 1440, height: 900, port: 8481 },
  { label: "phone", width: 390, height: 844, port: 8482 },
];

// The three READS a Setup workflow card's model can depend on. Each is a
// separate transport channel below, because which one fails decides which
// sentence the ERROR body must show — and because `roster` is the only
// workflow with two, and `import` is the only one with none that can fail it.
const OVERVIEW_RE = /\/api\/v2\/setup\/overview(\?|$)/;
const PROGRESS_RE = /\/api\/v2\/setup\/progress(\?|$)/;
const PLAYERS_RE = /\/api\/players(\?|$)/;
const CONTEXT_OPTIONS_RE = /\/api\/context\/options(\?|$)/;

// app.js's own copy, asserted as EXACT strings. A regex would let a different
// sentence pass under the same shape, and several of these assertions are
// precisely about which of two sentences was said.
const ERR_OVERVIEW = "Couldn't load the setup overview.";
const ERR_PLAYERS = "Couldn't load the player list.";
const ERR_STATUS = "Couldn't load this workflow's setup status.";
const ERR_LANDING_TAIL = "The action below still works — only these counts are missing.";
const STALE_NOTE = "These counts are from the program, season or league you had"
  + " selected earlier.";
const SUCCESS_NOTE = "✓ This workflow is set up. You can still add more whenever"
  + " you need to.";
const OPTIONAL_NOTE = "This step is optional — you can set everything up by hand"
  + " instead, and skipping it never blocks the rest of setup.";
// Workflow 6's declared confirmation (SETUP_WORKFLOWS, key "import").
const WIZARD_PROMPT = "Restart the guided Initial Setup wizard? Your existing"
  + " data isn't changed — you'll be taken through setup from the top.";
const WIZARD_DONE = "Opening the Initial Setup wizard.";
const WIZARD_CANCELLED = "Stayed on Imports and onboarding.";
// The derived reopen confirmation (SETUP_SEASON_REOPEN_ACTION).
const REOPEN_LABEL = "Reopen this season";
const REOPEN_PROMPT = "Reopen this archived season so it can be changed again?"
  + " It stays selected, and its existing records are untouched.";
const REOPEN_BUSY = "Reopening this season…";
const REOPEN_DONE = "Season reopened — it can be changed again.";
const REOPEN_CANCELLED = "The season stays archived.";
const REOPEN_NO_REASON = "Add a reason before reopening.";

// ============================ THE SIX WORKFLOWS ===========================
// Kept as data so a renamed or missing workflow fails loudly instead of
// silently reducing coverage, and so every leg below iterates the SAME list.
//
//   statLabels   the count labels this workflow's `summary` produces, lower-
//                cased — which is exactly what the EMPTY sentence has to name
//                ("no venues, rinks for this program, season and league").
//   empty        what a PRISTINE zero-Program installation must show: the one
//                authorized action that can resolve the emptiness, and the
//                sentence naming the prerequisite it was derived from.
//   p1 / p2      the settled shape under the two provisioned fixtures. `p1` is
//                the fully provisioned Program (every required workflow
//                `done`); `p2` is a bare Program+Season with nothing else.
//                The two exist so the stale-response legs have a genuinely
//                different "newer" answer to protect, and so the EMPTY leg's
//                "exactly one action" has a control case that offers more.
//   confirmable  whether ANY action this landing can offer — declared or
//                derived — carries a confirmation. Asserted in both
//                directions (leg 8d).
//   readFailure  which forced read failure produces this card's ERROR state
//                through the shared render, and the sentence it must show.
const WORKFLOWS = [
  { key: "league_season", title: "League profile and seasons",
    statLabels: ["programs", "seasons", "leagues"],
    empty: { primary: "Add program",
      why: "A season belongs to a program, and there is no program yet." },
    p1: { state: "success", primary: "Add Season",
      groups: ["Add Season", "Programs", "Leagues"], stats: ["Programs=1", "Seasons=1", "Leagues=1"] },
    p2: { state: "ready", primary: "Add Season",
      groups: ["Add Season", "Programs", "Leagues"], stats: ["Programs=1", "Seasons=1", "Leagues=0"] },
    // Same label under both fixtures: this workflow's chain is satisfied by
    // the Program alone. The stale-response legs therefore lean on the counts
    // (Leagues=1 vs Leagues=0) instead, which is asserted explicitly.
    primaryDiffersAcrossTuples: false,
    confirmable: false },
  { key: "teams", title: "Permanent teams",
    statLabels: ["teams", "clubs"],
    empty: { primary: "Add a league first",
      why: "A permanent team belongs to a league, and this program's active"
        + " season has none yet." },
    p1: { state: "success", primary: "Add Team", groups: ["Add Team", "Clubs"],
      stats: ["Teams=1", "Clubs=0"] },
    p2: { state: "empty", primary: "Add a league first", groups: ["Add a league first"],
      stats: ["Teams=0", "Clubs=0"] },
    primaryDiffersAcrossTuples: true,
    confirmable: false },
  { key: "participation", title: "Season participation and divisions",
    statLabels: ["divisions"],
    empty: { primary: "Add a league first",
      why: "Divisions live inside a league, and this program's active season"
        + " has none yet." },
    p1: { state: "success", primary: "Register Team", groups: ["Register Team", "Divisions"],
      stats: ["Divisions=1"] },
    p2: { state: "empty", primary: "Add a league first", groups: ["Add a league first"],
      stats: ["Divisions=0"] },
    primaryDiffersAcrossTuples: true,
    // Reachable through the derived reopen under an archived Season
    // (SETUP_ASSERTED_PREREQ_ACTIONS "participation/season_active").
    confirmable: true },
  { key: "roster", title: "Clubs, players and staff",
    statLabels: ["players", "officials"],
    empty: { primary: "Add a team first",
      why: "Players are added to a team, and this program, season and league"
        + " has none yet." },
    p1: { state: "success", primary: "Add Player", groups: ["Add Player", "Officials"],
      stats: ["Players=1", "Officials=0"] },
    p2: { state: "empty", primary: "Add a team first", groups: ["Add a team first"],
      stats: ["Players=0", "Officials=0"] },
    primaryDiffersAcrossTuples: true,
    confirmable: false },
  { key: "facilities", title: "Venues, rinks and ice",
    statLabels: ["venues", "rinks"],
    empty: { primary: "Add venue",
      why: "Ice is booked on a rink inside a venue, and there is no venue yet." },
    p1: { state: "success", primary: "Add Ice",
      groups: ["Add Ice", "Venues", "Rinks", "Add one ice slot"], stats: ["Venues=1", "Rinks=1"] },
    p2: { state: "empty", primary: "Add venue", groups: ["Add venue"],
      stats: ["Venues=0", "Rinks=0"] },
    primaryDiffersAcrossTuples: true,
    // "facilities/season_active" -> the same derived reopen.
    confirmable: true },
  // WORKFLOW 6. No `summary`, therefore no inventory, therefore no EMPTY and
  // no ERROR to reach — see the coverage note at the top of this file. Its
  // model is identical under every tuple, which leg 7b asserts.
  { key: "import", title: "Imports and onboarding", optional: true,
    statLabels: [],
    p1: { state: "ready", primary: "Import data",
      groups: ["Import data", "Initial Setup wizard"], stats: [] },
    p2: { state: "ready", primary: "Import data",
      groups: ["Import data", "Initial Setup wizard"], stats: [] },
    primaryDiffersAcrossTuples: false,
    // The DECLARED confirmation on its "Initial Setup wizard" secondary.
    confirmable: true },
];
const REQUIRED = WORKFLOWS.filter((w) => !w.optional);
const byKey = (k) => WORKFLOWS.find((w) => w.key === k);

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

// One line per leg, on stderr. A journey this long that times out inside a
// wait otherwise leaves nothing at all to locate the failure with, and "which
// leg was running" is the first question every such failure asks.
function trace(msg) { console.error(`  · ${msg}`); }

// ======== THE DELIVERY RECONCILER, AS A PURE FUNCTION (inherited) =========
// Everything the page's four network/console recorders collected, turned into
// the list of lines that must fail the run. A pure function of its four
// inputs on purpose: it is what makes the non-fungibility of an allowance
// testable without a browser, which selfTestDeliveryReconciler() below does on
// every invocation of this journey.
//
// THE RULES, in order:
//   1. A 3xx is not a failure. Redirects and 304s are ordinary HTTP; they are
//      still RECORDED, they are simply not errors.
//   2. A >=400 response is matched against an UNMATCHED injected allowance
//      with the IDENTICAL method, the IDENTICAL absolute URL and the IDENTICAL
//      status. Nothing else can satisfy it, so an unrelated 404 can never
//      consume this file's deliberate 500 on the setup overview.
//   3. Any unmatched >=400 response fails the run, NAMED: method, URL, status.
//   4. Any allowance that was declared and never delivered fails the run too.
//      A leg that injects a failure the page never receives has stopped
//      testing what it says it tests.
//   5. Chromium's "Failed to load resource" console lines are matched by the
//      URL in m.location() against the URLs step 2 accepted, one line per
//      accepted failure. A console line for any other URL fails the run and is
//      reported WITH that URL, which m.text() alone never carried.
//   6. A request that never got a response at all is always a failure.
function reconcileDeliveries(nonOk, injected, consoleErrors, failedReqs) {
  const out = [];
  const accepted = new Map();  // url -> how many console lines it may explain
  for (const rec of nonOk) {
    if (rec.status < 400) continue;                                    // (1)
    const slot = injected.find((i) => !i.matched                       // (2)
      && i.method === rec.method && i.url === rec.url
      && i.status === rec.status);
    if (slot) {
      slot.matched = true;
      accepted.set(rec.url, (accepted.get(rec.url) || 0) + 1);
      continue;
    }
    out.push(`[response] ${rec.method} ${rec.url} -> ${rec.status} — no `      // (3)
      + `deliberate failure was injected for this request, so this is a real `
      + `failed request, not one of this journey's own forced outcomes`);
  }
  for (const i of injected) {                                          // (4)
    if (i.matched) continue;
    out.push(`[injected] a ${i.status} was injected for ${i.method} ${i.url} `
      + `but no such response was ever delivered to the page, so the leg that `
      + `injected it proved nothing`);
  }
  for (const c of consoleErrors) {                                     // (5)
    const left = accepted.get(c.url) || 0;
    if (left > 0) { accepted.set(c.url, left - 1); continue; }
    out.push(`[console] ${c.text} @ ${c.url || "<no location reported>"}`);
  }
  for (const f of failedReqs) {                                        // (6)
    out.push(`[requestfailed] ${f.method} ${f.url} -> ${f.failure}`);
  }
  return out;
}

// THE ALLOWANCE IS NOT FUNGIBLE, proven before the browser is even launched.
// The scenario: a leg injects a 500 on the setup overview and, during that
// same leg, an unrelated 404 occurs. The 404 must be reported with its exact
// URL and must NOT be absorbed; the 500 must still be accepted, and only for
// its own request.
function selfTestDeliveryReconciler() {
  const OVERVIEW = "http://127.0.0.1:8481/api/v2/setup/overview";
  const PLAYERS = "http://127.0.0.1:8481/api/players";
  const STRAY = "http://127.0.0.1:8481/api/v2/setup/seasons/season_9/venue-access";
  const line = (code) => "Failed to load resource: the server responded with a "
    + `status of ${code}`;
  const check = (what, got, want) => {
    if (JSON.stringify(got) !== JSON.stringify(want)) {
      fail(`delivery-reconciler self-test (${what}) — expected `
        + `${JSON.stringify(want)}, got ${JSON.stringify(got)}`);
    }
  };

  // (i) the injected 500 alone: accepted, and its console line with it.
  check("an injected 500 is accepted for its own request",
    reconcileDeliveries(
      [{ method: "GET", url: OVERVIEW, status: 500 }],
      [{ method: "GET", url: OVERVIEW, status: 500, matched: false }],
      [{ text: line(500), url: OVERVIEW }], []),
    []);

  // (ii) the same 500 PLUS an unrelated 404 in the same leg. The 404 is
  //      reported with its exact URL; the 500 is still accepted silently.
  let out = reconcileDeliveries(
    [{ method: "GET", url: OVERVIEW, status: 500 },
     { method: "GET", url: STRAY, status: 404 }],
    [{ method: "GET", url: OVERVIEW, status: 500, matched: false }],
    [{ text: line(500), url: OVERVIEW }, { text: line(404), url: STRAY }], []);
  if (out.length !== 2 || !out.every((l) => l.indexOf(STRAY) !== -1
        && l.indexOf("404") !== -1 && l.indexOf(OVERVIEW) === -1)) {
    fail(`delivery-reconciler self-test — an unrelated 404 alongside an `
      + `injected 500 must fail the run naming ${STRAY} and nothing else, got `
      + `${JSON.stringify(out)}`);
  }

  // (iii) the 404 ALONE against an outstanding 500 allowance: the allowance is
  //       not spent on it, so BOTH the stray 404 and the undelivered injection
  //       are reported.
  out = reconcileDeliveries(
    [{ method: "GET", url: STRAY, status: 404 }],
    [{ method: "GET", url: OVERVIEW, status: 500, matched: false }],
    [{ text: line(404), url: STRAY }], []);
  if (out.length !== 3
      || !out.some((l) => l.indexOf("[response]") === 0 && l.indexOf(STRAY) !== -1)
      || !out.some((l) => l.indexOf("[injected]") === 0 && l.indexOf(OVERVIEW) !== -1)
      || !out.some((l) => l.indexOf("[console]") === 0 && l.indexOf(STRAY) !== -1)) {
    fail(`delivery-reconciler self-test — a 500 allowance must never be spent `
      + `on an unrelated 404, got ${JSON.stringify(out)}`);
  }

  // (iv) SAME status, DIFFERENT request: still not fungible. This file forces
  //      failures on three different read endpoints, so an allowance for the
  //      overview must never cover the player list.
  out = reconcileDeliveries(
    [{ method: "GET", url: OVERVIEW, status: 500 },
     { method: "GET", url: PLAYERS, status: 500 }],
    [{ method: "GET", url: OVERVIEW, status: 500, matched: false }], [], []);
  if (out.length !== 1 || out[0].indexOf(PLAYERS) === -1) {
    fail(`delivery-reconciler self-test — an allowance bound to the overview `
      + `must not cover the player list, got ${JSON.stringify(out)}`);
  }

  // (v) SAME request, TWICE: one allowance covers one delivery, not both.
  out = reconcileDeliveries(
    [{ method: "GET", url: OVERVIEW, status: 500 },
     { method: "GET", url: OVERVIEW, status: 500 }],
    [{ method: "GET", url: OVERVIEW, status: 500, matched: false }], [], []);
  if (out.length !== 1 || out[0].indexOf(OVERVIEW) === -1) {
    fail(`delivery-reconciler self-test — one allowance must be consumed at `
      + `most once, got ${JSON.stringify(out)}`);
  }

  // (vi) a redirect is not a failure.
  check("a 3xx is not a failure",
    reconcileDeliveries([{ method: "GET", url: STRAY, status: 304 }], [], [], []),
    []);
}

// ============ THE PAGE IS QUIET BEFORE ANYTHING IS SAMPLED ================
// Not politeness — correctness. render() commits a fresh model for EVERY
// Setup card, so a snapshot taken while one is in flight makes the
// neighbour-isolation and stale-response assertions depend on whether the
// render landed before or after the sample. It was observed doing exactly
// that: a "retry teams" leg reported all six cards mutated, and the retry's
// own focus move landing on <body>, purely because a render begun by the
// preceding navigation resolved in the middle of it.
//
// Idle is measured on BOTH axes, because either alone is a false negative:
//   * requests in flight down to the tolerated count — a render sitting on a
//     fetch is not idle; and
//   * no NEW request started for a whole quiet window — a render chains its
//     reads back to back, so a zero sampled between two of them says nothing.
//
// `tolerate` is the number of requests this journey is DELIBERATELY holding
// open at the sample point. Without it, every leg that samples the page while
// a held response is outstanding would hang here forever.
const QUIET_WINDOW_MS = 300;
const QUIESCE_TIMEOUT_MS = 25000;
async function quiesce(page, step, tolerate) {
  const t = tolerate || 0;
  const deadline = Date.now() + QUIESCE_TIMEOUT_MS;
  for (;;) {
    if (page.__smInFlight() > t) await page.waitForTimeout(150);
    if (page.__smInFlight() <= t) {
      const seq = page.__smRequestSeq();
      await page.waitForTimeout(QUIET_WINDOW_MS);
      if (page.__smInFlight() <= t && page.__smRequestSeq() === seq) return;
    }
    if (Date.now() > deadline) {
      fail(`[${step}] the page never went quiet before it was sampled `
        + `(${page.__smInFlight()} request(s) in flight, ${t} tolerated) — a `
        + `render still committing card models would make the assertion that `
        + `follows depend on timing rather than on behaviour`);
    }
  }
}

// ====================== THE FORCED-TRANSPORT CHANNELS =====================
// One channel per read a card's model depends on. Each is driven by a MODE
// rather than by installing and removing routes, so a route handler is never
// swapped underneath an in-flight request:
//
//   "pass"  route.continue() — the ordinary path.
//   "fail"  fulfilled with 500 WITHOUT route.fetch(), so the server genuinely
//           never answers and the failure is the transport's, not a fixture's.
//           Registers an allowance keyed to this exact method + URL + status.
//   "hold"  the REAL response is fetched FIRST, then held on a gate, then
//           fulfilled with `{ response: r }`. Holding the REQUEST instead
//           would merely make the server answer later and would prove nothing
//           about a response arriving into a context that has moved on.
//
// `released` counts HELD responses this channel has actually handed to the
// page — incremented after route.fulfill() returns, not from a network event.
// The distinction matters: a leg that releases a held response and then
// asserts "nothing changed" has to know the response really arrived, and a
// page-level response counter would also be moved by the ordinary (and
// deliberately failing) requests those legs make in between.
function makeChannel(name, re) {
  return { name: name, re: re, mode: "pass", gate: null, release: () => {},
           held: null, markHeld: () => {}, heldNow: 0, released: 0 };
}
// Arm the channel to hold the NEXT matching request. Returns a promise that
// resolves once that request's real response has been captured and parked, so
// a caller can be certain the hold is in force before it does anything else.
function armHold(ch) {
  ch.held = new Promise((resolve) => { ch.markHeld = resolve; });
  ch.gate = new Promise((resolve) => { ch.release = resolve; });
  ch.mode = "hold";
  return ch.held;
}

// ======================= ANNOUNCEMENTS, AS HEARD =========================
// Read off the ONE sitewide live region (#toast-root, role="status"
// aria-live="polite") with a MutationObserver, so "exactly one success
// announcement" is measured against what the region was actually handed
// rather than against what the code intended to say.
//
// IT READS THE MUTATION RECORDS, not only the region's settled state, and
// that difference is load-bearing. A MutationObserver callback is delivered
// ONCE per task with every record from that task, so an announcement that is
// written and then withdrawn WITHIN THE SAME TASK is completely invisible to
// a recorder that only re-reads the DOM when the callback fires. That is not
// hypothetical here: resolveSetupCardConfirm() announces a `go` action's
// completion sentence and then synchronously navigates, and switchTab() sets
// `toast = ""` and render() calls updateToast() before the task ends — so the
// settled-state recorder saw an EMPTY region and reported that the completion
// sentence was never announced at all.
//
// So each added `.toast-msg` node is recorded as a write in its own right.
//
// NOTHING IS DEDUPLICATED (#365 review). This recorder used to drop an entry
// whose {text, error} matched the one before it, which meant a duplicated
// confirmation or success write was counted ONCE BY CONSTRUCTION and the
// "no duplicate speech" clause could not fail here however production behaved.
// Every raw write is now retained, in order, and duplicates are FAILED below.
//
// Each entry carries its `kind`, because two different things are recorded and
// only one of them is speech:
//   "write"    a `.toast-msg` node was added — the region was genuinely handed
//              a sentence. This is an announcement; two consecutive identical
//              ones are duplicate speech.
//   "settled"  the region's state read after the batch, which is how a write
//              that is WITHDRAWN inside the same task is still seen at all
//              (updateToast() hides the region without clearing its markup, so
//              a settled read of a hidden region is the empty string). Kept in
//              the ledger for ordering, never counted as a sentence — counting
//              it would report every single announcement twice.
async function armAnnouncements(page) {
  await page.evaluate(() => {
    const root = document.getElementById("toast-root");
    if (!root) throw new Error("no #toast-root to observe");
    window.__sm = [];
    const push = (entry) => { window.__sm.push(entry); };
    const read = () => {
      const msg = root.querySelector(".toast-msg");
      return { kind: "settled",
               text: root.hidden ? "" : (msg ? msg.textContent.trim() : ""),
               error: root.classList.contains("error") };
    };
    const rec = (records) => {
      (records || []).forEach((r) => {
        if (r.type !== "childList") return;
        Array.prototype.forEach.call(r.addedNodes, (n) => {
          if (!n || n.nodeType !== 1) return;
          const el = n.classList && n.classList.contains("toast-msg")
            ? n : (n.querySelector ? n.querySelector(".toast-msg") : null);
          if (!el) return;
          push({ kind: "write", text: (el.textContent || "").trim(),
                 error: root.classList.contains("error") });
        });
      });
      push(read());
    };
    if (window.__smObs) window.__smObs.disconnect();
    window.__smObs = new MutationObserver(rec);
    window.__smObs.observe(root, { childList: true, subtree: true,
      characterData: true, attributes: true });
    rec(null);
  });
}
// Only the SPOKEN entries, in the raw order they were written, with nothing
// merged away: updateToast()'s 4-second auto-clear hides the region, which is
// a dismissal rather than an announcement, and the settled reads that record
// it are not sentences.
async function spoken(page) {
  return (await page.evaluate(() => window.__sm || []))
    .filter((a) => a.kind === "write" && a.text)
    .map((a) => ({ text: a.text, error: !!a.error }));
}
async function resetAnnouncements(page) {
  await page.evaluate(() => { window.__sm = []; });
}
// #365, "without duplicate speech", measured rather than assumed away: two
// consecutive byte-identical writes to the one sitewide live region are the
// same sentence handed to it twice.
function assertNoRepeatedSpeech(said, L, step) {
  for (let i = 1; i < said.length; i++) {
    if (said[i].text === said[i - 1].text) {
      fail(`[${L}/${step}] the live region was handed two back-to-back writes `
        + `carrying byte-identical text — the same sentence announced twice: `
        + `${JSON.stringify(said[i].text)}. Full ordered ledger: `
        + `${JSON.stringify(said)}`);
    }
  }
}
// Exactly one announcement, and it is the one named. Both halves matter: a
// missing sentence and a duplicated one are different defects and this reports
// which it found. The raw ledger is used, so a duplicate really is visible
// here rather than collapsed on the way in.
async function assertSaidExactly(page, want, isError, L, step) {
  const said = await spoken(page);
  assertNoRepeatedSpeech(said, L, step);
  if (said.length !== 1 || said[0].text !== want || said[0].error !== !!isError) {
    fail(`[${L}/${step}] the live region must have carried EXACTLY the single `
      + `announcement ${JSON.stringify(want)} (error=${!!isError}); it carried `
      + `${JSON.stringify(said)}`);
  }
}

// ============ THE ONE RECONCILIATION ROUND TRIP, UNDER CONTROL ===========
// /api/context/options is the second round trip of a context switch: the
// switch awaits it between moving `contextOptions.selected` and releasing
// `contextSwitchIntentPending`. Intercepting it once per page makes that
// window controllable without changing a single response byte.
let optionsDelayMs = 0;
async function installContextOptionsControl(page) {
  await page.route(CONTEXT_OPTIONS_RE, async (route) => {
    const delay = optionsDelayMs;
    if (delay > 0) await new Promise((r) => setTimeout(r, delay));
    try { await route.continue(); } catch (e) { /* page closed mid-hold */ }
  });
}

// Switch context through the REAL #ctx-select, exactly as an operator does —
// never through a raw /api/context POST, which bypasses setActiveContext()'s
// whole invalidate/withdraw/re-render sequence and would prove nothing about
// what a switch does to a card.
//
// THREE CONJUNCTS, all necessary (inherited):
//   (1) THE CONFIRMED TUPLE — program AND season. Reading the season alone
//       would not wait at all for a switch that moved only the Program axis.
//   (2) contextSwitchIntentPending === false. Until it clears,
//       setupLandingActions() withdraws EVERY action group on purpose, so a
//       landing sampled inside that window has no controls for a reason that
//       has nothing to do with the card's own state — which is exactly the
//       confusion the STALE leg has to avoid.
//   (3) A REPAINT THAT HAPPENED AFTER (2), observed on #content's DIRECT
//       CHILDREN. sendContextSwitch()'s own repaintContextScopedCardsAsStale()
//       and render()'s retain-the-cards pass both rewrite card slots —
//       DESCENDANTS of #content — while the withdrawal is still in force. A
//       subtree observer is satisfied by those and returns too early.
async function switchContext(page, programId, seasonId, step) {
  await page.evaluate(([p, s]) => {
    if (window.__smSwitchObs) window.__smSwitchObs.disconnect();
    window.__smSwitchPainted = false;
    const c = document.getElementById("content");
    window.__smSwitchObs = new MutationObserver(() => {
      if (contextSwitchIntentPending) return;
      const cur = (contextOptions && contextOptions.selected) || {};
      if (cur.program_id !== p || cur.season_id !== s) return;
      window.__smSwitchPainted = true;
    });
    if (c) window.__smSwitchObs.observe(c, { childList: true, subtree: false });
  }, [programId, seasonId]);

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
}

async function waitForContextReconciled(page, programId, seasonId, step) {
  await page.waitForFunction(([p, s]) => {
    if (contextSwitchIntentPending) return false;
    const cur = (contextOptions && contextOptions.selected) || {};
    if (cur.program_id !== p || cur.season_id !== s) return false;
    return window.__smSwitchPainted === true;
  }, [programId, seasonId], { timeout: 40000 })
    .catch(async () => {
      const why = await page.evaluate(() => ({
        intentPending: !!contextSwitchIntentPending,
        selected: (contextOptions && contextOptions.selected) || null,
        painted: !!window.__smSwitchPainted,
      })).catch(() => null);
      fail(`[${step}] the context switch to ${programId}/${seasonId} never `
        + `RECONCILED — the confirmed tuple, the release of the action-control `
        + `withdrawal and a repaint after it are all required before anything `
        + `on this page can be sampled: ${JSON.stringify(why)}`);
    });
}

// What is ACTUALLY on screen, for every navigation failure message. A bare
// Playwright "waiting for locator(…)" timeout says which selector was missing
// and nothing about why, which is not enough to tell a product regression from
// a journey that clicked too early.
async function describeSurface(page) {
  return JSON.stringify(await page.evaluate(() => ({
    view: document.body.dataset.view || null,
    setupView: typeof setupView === "undefined" ? null : setupView,
    setupWorkflow: typeof setupWorkflow === "undefined" ? null : setupWorkflow,
    signedIn: typeof currentUser === "undefined" ? null
      : (currentUser && currentUser.username) || null,
    setupTabVisible: !!document.querySelector(
      '.tab[data-tab="setup"]:not([data-setup-workflow-nav])')
      && document.querySelector('.tab[data-tab="setup"]:not([data-setup-workflow-nav])')
        .style.display !== "none",
    contentChildren: Array.from(document.getElementById("content").children)
      .map((el) => `${el.tagName}.${el.className}`).slice(0, 10),
    skeletons: document.querySelectorAll("#content .skeleton").length,
    // render()'s own failure banner, verbatim. Without it a surface that
    // failed to load is indistinguishable from one that merely rendered late.
    banner: (() => {
      const b = document.querySelector("#content .banner");
      return b ? b.textContent.replace(/\s+/g, " ").trim().slice(0, 200) : null;
    })(),
  })).catch(() => null));
}

// ============================== NAVIGATION ================================
// Every transition below uses a control a real operator uses. Nothing calls
// render(), openSetupWorkflowLanding() or switchTab() directly.
async function openHub(page, step, tolerate) {
  await page.click('.tab[data-tab="setup"]:not([data-setup-workflow-nav])')
    .catch(async () => fail(`[${step}] the Setup nav entry could not be `
      + `activated: ${await describeSurface(page)}`));
  await page.waitForFunction(() => document.body.dataset.view === "setup",
    null, { timeout: 20000 })
    .catch(async () => fail(`[${step}] the Setup tab never reached the setup `
      + `view: ${await describeSurface(page)}`));
  // The sub-view toggle is painted by renderSetup(), which runs only after the
  // Setup reads land — so this waits for the destination to exist rather than
  // relying on page.click()'s own actionability timeout, whose failure message
  // says nothing about what IS on screen.
  await page.waitForSelector('[data-setup-view="hub"]', { timeout: 25000 })
    .catch(async () => fail(`[${step}] the Setup sub-view toggle never `
      + `rendered: ${await describeSurface(page)}`));
  await page.click('[data-setup-view="hub"]');
  await page.waitForSelector(".swf-grid", { timeout: 20000 })
    .catch(async () => fail(`[${step}] the workflow index never rendered: `
      + `${await describeSurface(page)}`));
  await page.waitForFunction((n) =>
    document.querySelectorAll("[data-setup-card-slot]").length === n,
    WORKFLOWS.length, { timeout: 15000 })
    .catch(async () => fail(`[${step}] the hub painted `
      + `${await page.evaluate(() => document.querySelectorAll("[data-setup-card-slot]").length)} `
      + `card slots, expected ${WORKFLOWS.length}`));
  await quiesce(page, `${step}/hub`, tolerate);
}

// The hub's own permission-gated "Open …" button — the transition an operator
// uses — followed by a wait for the landing to be PAINTED (its stable action
// container present) and its card to be out of LOADING/PENDING. Both halves
// matter: the model settles inside render() BEFORE the paint, so waiting on
// the model alone routinely returns while the previous surface is still on
// screen.
async function openLanding(page, key, step, tolerate) {
  await openHub(page, step, tolerate);
  await openLandingFromHub(page, key, step);
  await settledLanding(page, key, step, tolerate);
}

async function settledLanding(page, key, step, tolerate) {
  await page.waitForFunction((k) => {
    const root = document.querySelector(`[data-setup-workflow-landing="${k}"]`);
    if (!root || !root.querySelector("[data-setup-landing-actions]")) return false;
    const e = readCardState("setup/" + k);
    return e.state !== "loading" && e.state !== "pending";
  }, key, { timeout: 20000 })
    .catch(async () => fail(`[${step}] the "${key}" landing never settled: `
      + `${JSON.stringify(await page.evaluate((k) => ({
          painted: !!document.querySelector(`[data-setup-workflow-landing="${k}"]`),
          state: readCardState("setup/" + k).state }), key))}`));
  await quiesce(page, `${step}/landing:${key}`, tolerate);
}

// Return to the hub through the landing's OWN back control, and come back —
// the cheapest SAME-TUPLE render pair a real operator can produce, and the one
// leg 6 runs underneath an unresolved read.
async function sameTupleRenderThroughUi(page, key, step) {
  await page.click('[data-setup-workflow=""]');
  await page.waitForSelector(".swf-grid", { timeout: 20000 })
    .catch(() => fail(`[${step}] the landing's own back control never reached `
      + `the workflow index`));
  await page.waitForFunction((n) =>
    document.querySelectorAll("[data-setup-card-slot]").length === n,
    WORKFLOWS.length, { timeout: 20000 });
  await openLandingFromHub(page, key, step);
}

// The hub's own "Open …" button, and the wait for the destination to be
// PAINTED. Split out because both callers need the same diagnostic when it
// does not arrive: "the landing never came back" is useless without knowing
// what IS on screen instead.
async function openLandingFromHub(page, key, step) {
  await page.click(`[data-setup-workflow="${key}"]`);
  await page.waitForSelector(`[data-setup-workflow-landing="${key}"]`, { timeout: 25000 })
    .catch(async () => fail(`[${step}] the "${key}" landing never came back: `
      + `${JSON.stringify(await page.evaluate(() => ({
          view: document.body.dataset.view,
          setupView: typeof setupView === "undefined" ? null : setupView,
          setupWorkflow: typeof setupWorkflow === "undefined" ? null : setupWorkflow,
          contentChildren: Array.from(document.getElementById("content").children)
            .map((el) => `${el.tagName}.${el.className}`).slice(0, 8),
          landings: Array.from(document.querySelectorAll("[data-setup-workflow-landing]"))
            .map((el) => el.dataset.setupWorkflowLanding),
        })).catch(() => null))}`));
}

// ============================ READING A CARD =============================
// The card's committed model plus the DOM both of its surfaces paint from.
// Every "no controls" field is accompanied by the STRUCTURAL presence of the
// container it was read from, so an assertion about emptiness can never be
// satisfied by a container that simply is not there.
async function readCard(page, key) {
  return page.evaluate((k) => {
    const e = readCardState("setup/" + k);
    const landing = document.querySelector(`[data-setup-workflow-landing="${k}"]`);
    const box = landing && landing.querySelector("[data-setup-landing-actions]");
    const slot = document.querySelector(`[data-setup-card-slot="${k}"]`);
    const tertiary = landing && landing.querySelector(".swf-tertiary");
    const alert = slot && slot.querySelector(".swf-card-error");
    const txt = (el) => el ? el.textContent.replace(/\s+/g, " ").trim() : null;
    return {
      state: e.state,
      staleFrom: e.staleFrom || null,
      status: e.status,
      optional: e.optional === undefined ? null : !!e.optional,
      effective: e.effective === undefined ? "undefined"
        : e.effective === null ? null : e.effective.label,
      blockedBecause: e.blockedBecause || null,
      stats: (e.stats || []).map((s) => `${s.label}=${s.n}`),
      generation: e.identity ? e.identity.generation : null,
      seasonId: e.identity ? e.identity.season_id : null,
      programId: e.identity ? e.identity.program_id : null,
      counter: cardGenerations["setup/" + k],
      busy: slot ? slot.getAttribute("aria-busy") : null,
      // STRUCTURAL presence, beside every emptiness claim below.
      hasLanding: !!landing,
      hasActionsBox: !!box,
      hasSlot: !!slot,
      slotButtons: slot ? Array.from(slot.querySelectorAll("button"))
        .map((b) => b.textContent.trim()) : null,
      actionButtons: box ? Array.from(box.querySelectorAll("button"))
        .map((b) => b.textContent.trim()) : null,
      primaryButtons: box ? Array.from(box.querySelectorAll(".act.primary"))
        .map((b) => b.textContent.trim()) : null,
      tertiaryButtons: tertiary ? Array.from(tertiary.querySelectorAll("button"))
        .map((b) => b.textContent.trim()) : null,
      askControls: landing ? Array.from(landing.querySelectorAll("[data-setup-card-ask]"))
        .map((b) => b.textContent.trim()) : null,
      retryControls: slot ? Array.from(slot.querySelectorAll("[data-setup-card-retry]"))
        .map((b) => b.textContent.trim()) : null,
      bodyText: txt(slot),
      alertText: txt(alert),
      alertRole: alert ? alert.getAttribute("role") : null,
      emptyText: txt(slot && slot.querySelector(".swf-card-empty")),
      staleText: txt(slot && slot.querySelector(".swf-card-stale")),
      doneText: txt(slot && slot.querySelector(".swf-card-done")),
      pendingText: txt(slot && slot.querySelector("[data-setup-card-pending]")),
      confirmPrompt: txt(slot && slot.querySelector(".swf-confirm-prompt")),
      skeleton: !!(slot && slot.querySelector(".skeleton")),
      skeletonLabel: txt(slot && slot.querySelector(".skeleton .sr-only")),
      // The landing's own stable semantic heading, and the workflow it names.
      headingTag: landing && landing.querySelector(".swf-landing-title")
        ? landing.querySelector(".swf-landing-title").tagName : null,
      headingText: txt(landing && landing.querySelector(".swf-landing-title")),
      optionalNote: txt(landing && landing.querySelector(".swf-optional-note")),
      chip: landing ? null : (() => {
        const head = document.querySelector(`[data-setup-workflow-card="${k}"] .swf-head`);
        const c = head && head.querySelector(".swf-optional, .swf-status");
        return c ? { cls: c.className, text: c.textContent.trim() } : null;
      })(),
      intentPending: !!contextSwitchIntentPending,
    };
  }, key);
}

// The hub's own status chip for a card, read from the hub grid.
async function readChip(page, key) {
  return page.evaluate((k) => {
    const head = document.querySelector(`[data-setup-workflow-card="${k}"] .swf-head`);
    if (!head) return { present: false };
    const c = head.querySelector(".swf-optional, .swf-status");
    return { present: true, chip: c ? { cls: c.className, text: c.textContent.trim() } : null,
             open: !!document.querySelector(`[data-setup-workflow="${k}"]`) };
  }, key);
}

// The hub roll-up, as the model AND as the sentence painted from it. Both,
// because Workflow 6 could be smuggled into either one alone.
async function readRollup(page) {
  return page.evaluate(() => {
    const roll = setupHubRollup();
    const el = document.querySelector("[data-setup-hub-progress]");
    return {
      total: roll.total, known: roll.known, done: roll.done, allDone: roll.allDone,
      next: roll.next ? roll.next.key : null,
      blockedBy: roll.blockedBy ? roll.blockedBy.key : null,
      requiredKeys: roll.required.map((r) => r.key),
      optionalKeys: roll.optional.map((r) => r.key),
      statuses: JSON.stringify(roll.required.concat(roll.optional)
        .map((r) => [r.key, r.status, r.optional])),
      slotPresent: !!document.querySelector("[data-setup-hub-progress-slot]"),
      text: el ? el.textContent.replace(/\s+/g, " ").trim() : "",
    };
  });
}

// EVERYTHING a stale response could reach, in one comparable record: the
// card's generation and committed model, the DOM both surfaces paint from, the
// landing's action groups, aria-busy, keyboard focus, the live region, and the
// completion line plus next-task recommendation. Compared field by field, so a
// mutation this file never thought to name is still caught.
async function snapshot(page, key) {
  return page.evaluate((k) => {
    const describe = (el) => {
      if (!el) return "null";
      const attrs = ["data-setup-card-pending", "data-setup-card-confirm-reason",
                     "data-setup-card-confirm-yes", "data-setup-card-retry",
                     "data-setup-card-ask", "data-setup-workflow-go", "id"]
        .map((a) => `${a}=${(el.getAttribute && el.getAttribute(a)) || ""}`).join(",");
      return `${el.tagName}[${el.className || ""}][${attrs}]`
        + `{${(el.textContent || "").replace(/\s+/g, " ").trim().slice(0, 80)}}`;
    };
    const slot = document.querySelector(`[data-setup-card-slot="${k}"]`);
    const actions = document.querySelector(`[data-setup-landing-actions="${k}"]`);
    const root = document.getElementById("toast-root");
    const msg = root && root.querySelector(".toast-msg");
    return {
      generations: JSON.stringify(cardGenerations),
      models: JSON.stringify(cardStates),
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
  }, key);
}

const SNAPSHOT_FIELDS = ["generations", "models", "rollup", "rollupHtml", "slotHtml",
  "slotBusy", "actionsHtml", "buttons", "focus", "toast", "toastError", "selected"];

function assertSame(before, after, L, step, why) {
  for (const field of SNAPSHOT_FIELDS) {
    if (before[field] !== after[field]) {
      fail(`[${L}/${step}] ${why} changed "${field}" — a superseded response `
        + `reached the current state, which is exactly what #365 requires a `
        + `card action to discard.\n  before: ${String(before[field]).slice(0, 900)}`
        + `\n  after:  ${String(after[field]).slice(0, 900)}`);
    }
  }
}

function assertDiffers(a, b, L, step, why) {
  const same = SNAPSHOT_FIELDS.filter((f) => a[f] === b[f]);
  if (same.length === SNAPSHOT_FIELDS.length) {
    fail(`[${L}/${step}] ${why} — the two snapshots are IDENTICAL on every `
      + `field, so the before/after comparison would pass no matter what `
      + `happened`);
  }
}

// ======================== FOCUS, AS AN EXACT NODE ========================
// "Something is focused" is not the requirement. Every assertion below names
// the ONE element that must hold focus, compares by NODE IDENTITY inside the
// page, and then RE-ASSERTS after a settling delay — because focusContentHeading()
// polls for up to two seconds after a navigation and will happily land on a
// destination heading well after a first sample succeeded. Both failure modes
// (never arrived; arrived and was then stolen) are reported distinctly.
const FOCUS_SETTLE_MS = 700;
async function describeFocus(page) {
  return page.evaluate(() => {
    const el = document.activeElement;
    if (!el) return "<null>";
    const attrs = ["data-setup-card-retry", "data-setup-card-ask",
                   "data-setup-card-confirm-yes", "data-setup-card-confirm-no",
                   "data-setup-card-confirm-reason", "data-setup-card-pending"]
      .filter((a) => el.hasAttribute && el.hasAttribute(a));
    return `<${el.tagName.toLowerCase()} class="${el.className || ""}"`
      + `${attrs.length ? ` [${attrs.join(" ")}]` : ""}>`
      + `${(el.textContent || el.value || "").replace(/\s+/g, " ").trim().slice(0, 60)}`;
  });
}
// `selector` must resolve to exactly one element; focus must be ON it.
async function assertFocusExactly(page, selector, L, step, what) {
  const count = await page.evaluate((s) => document.querySelectorAll(s).length, selector);
  if (count !== 1) {
    fail(`[${L}/${step}] the focus target "${selector}" (${what}) matches `
      + `${count} elements, so "focus is exactly on it" is not a well-formed `
      + `assertion`);
  }
  const ok = await page.waitForFunction((s) => {
    const el = document.querySelector(s);
    return !!el && document.activeElement === el;
  }, selector, { timeout: 10000 }).then(() => true).catch(() => false);
  if (!ok) {
    fail(`[${L}/${step}] keyboard focus never landed on ${what} `
      + `("${selector}"); it is on ${await describeFocus(page)}`);
  }
  await page.waitForTimeout(FOCUS_SETTLE_MS);
  const still = await page.evaluate((s) => {
    const el = document.querySelector(s);
    return !!el && document.activeElement === el;
  }, selector);
  if (!still) {
    fail(`[${L}/${step}] keyboard focus reached ${what} ("${selector}") and was `
      + `then taken away — it is now on ${await describeFocus(page)}. A focus `
      + `destination that a later poll steals leaves the operator somewhere `
      + `they did not ask to be`);
  }
}

// Wait until keyboard focus has STOPPED moving on its own before this journey
// puts it somewhere deliberately.
//
// This is not padding. focusContentHeading() polls for up to two seconds after
// a navigation, landing on the destination heading whenever it finally
// appears — so a test that focused a control immediately after a settle could
// have that focus taken away between placing it and pressing Enter, and the
// resulting failure would say nothing about the product. Observed while this
// journey was being built: a confirmation's blank-reason refusal appeared to
// land focus on the landing heading, when in fact the heading poll from the
// preceding navigation had arrived late.
async function awaitFocusQuiet(page) {
  const deadline = Date.now() + 6000;
  const sample = () => page.evaluate(() => {
    const a = document.activeElement;
    return a ? `${a.tagName}|${a.className || ""}|`
      + `${(a.textContent || "").replace(/\s+/g, " ").trim().slice(0, 40)}` : "null";
  });
  let last = await sample();
  for (;;) {
    await page.waitForTimeout(350);
    const now = await sample();
    if (now === last) return;
    last = now;
    if (Date.now() > deadline) return;   // best effort; the assertions still judge
  }
}

// ===================== KEYBOARD ACTIVATION, FOR REAL =====================
// A control is "keyboard reachable" when TAB gets to it from a real anchor in
// the same surface, and "keyboard activated" when ENTER on it does the work.
// Both are done with real key events; nothing here calls .click().
//
// The anchor is the landing's own back control, which is the first focusable
// element inside the landing — so the hop count also proves the control sits
// in natural document order rather than being reachable only by a focus grab.
async function tabToAttribute(page, attr, value, L, step, what, anchorSelector) {
  await awaitFocusQuiet(page);
  await page.focus(anchorSelector);
  const anchored = await page.evaluate((s) =>
    document.activeElement === document.querySelector(s), anchorSelector);
  if (!anchored) {
    fail(`[${L}/${step}] could not place keyboard focus on the tab anchor `
      + `"${anchorSelector}", so the reachability walk below would start from `
      + `nowhere`);
  }
  for (let hop = 1; hop <= 25; hop++) {
    await page.keyboard.press("Tab");
    const hit = await page.evaluate(([a, v]) => {
      const el = document.activeElement;
      return !!(el && el.getAttribute && el.getAttribute(a) === v);
    }, [attr, value]);
    if (hit) return hop;
  }
  fail(`[${L}/${step}] ${what} was never reached by TAB from `
    + `"${anchorSelector}" within 25 hops — focus ended on `
    + `${await describeFocus(page)}. The requirement is that retry is keyboard `
    + `reachable, which means reachable by the sequential tab order, not merely `
    + `present in the markup`);
}

// ================================= LEGS ==================================

// (1) EMPTY, on a PRISTINE zero-Program installation.
//
// The inherited coverage ruling: "`league_season` EMPTY is reachable on a
// pristine zero-Program installation; keep desktop and 390px coverage of it."
// The same installation puts the other four required workflows in EMPTY too,
// so all five are asserted here and Workflow 6's optional presence with them.
//
// What the issue requires of this state, verbatim: "Empty states explain what
// is missing and expose only the authorized primary action." Both halves are
// asserted literally: the sentence must NAME this workflow's own missing
// records AND the prerequisite that blocks it, and the landing must offer
// EXACTLY ONE control. The "exactly one" half is not vacuous — leg 2 shows the
// same five landings offering two, three and four controls once nothing is
// blocked.
async function legEmptyPristine(page, L) {
  for (const w of WORKFLOWS) {
    const step = `1/empty/${w.key}`;
    await openLanding(page, w.key, `${L}/${step}`);
    const c = await readCard(page, w.key);

    if (!c.hasLanding || !c.hasActionsBox || !c.hasSlot) {
      fail(`[${L}/${step}] the landing did not paint its own structure `
        + `(landing=${c.hasLanding} actions=${c.hasActionsBox} slot=${c.hasSlot}) — `
        + `every emptiness assertion below would be satisfied by absence`);
    }
    // The stable semantic heading the issue asks for, present in every state.
    // Asserted as a real <h2> whose text ENDS WITH the workflow's own title —
    // the decorative icon in front of it is aria-hidden decoration and is
    // deliberately not part of the contract.
    if (c.headingTag !== "H2" || !c.headingText
        || c.headingText.slice(-w.title.length) !== w.title) {
      fail(`[${L}/${step}] the landing heading is `
        + `<${c.headingTag}> ${JSON.stringify(c.headingText)}, expected an <h2> `
        + `naming "${w.title}"`);
    }

    if (w.optional) {
      // Workflow 6 has no inventory, so a pristine installation leaves it
      // exactly where it always is: present, reachable, optional.
      if (c.state !== "ready" || c.optional !== true) {
        fail(`[${L}/${step}] Workflow 6 on a pristine installation reads `
          + `state "${c.state}" optional=${c.optional}; it has no data `
          + `dependency, so it must simply be there`);
      }
      if (c.optionalNote !== OPTIONAL_NOTE) {
        fail(`[${L}/${step}] Workflow 6's landing must carry its optional note; `
          + `it carries ${JSON.stringify(c.optionalNote)}`);
      }
      continue;
    }

    if (c.state !== "empty") {
      fail(`[${L}/${step}] "${w.title}" must be EMPTY on a pristine `
        + `zero-Program installation; it reads "${c.state}" with stats `
        + `${JSON.stringify(c.stats)}`);
    }
    // (a) it explains what is missing — this workflow's own count labels.
    for (const label of w.statLabels) {
      if (!c.emptyText || c.emptyText.indexOf(label) === -1) {
        fail(`[${L}/${step}] the EMPTY sentence does not name the missing `
          + `"${label}": ${JSON.stringify(c.emptyText)}`);
      }
    }
    // (b) ...and the prerequisite that is actually blocking it, from the
    //     committed model rather than from a paint-time re-derivation.
    if (c.blockedBecause !== w.empty.why) {
      fail(`[${L}/${step}] the committed blocker is `
        + `${JSON.stringify(c.blockedBecause)}, expected `
        + `${JSON.stringify(w.empty.why)}`);
    }
    if (!c.emptyText || c.emptyText.indexOf(w.empty.why) === -1) {
      fail(`[${L}/${step}] the EMPTY sentence does not carry the blocker the `
        + `single action was derived from: ${JSON.stringify(c.emptyText)}`);
    }
    // (c) EXACTLY ONE action, and it is the authorized effective one.
    if ((c.actionButtons || []).length !== 1
        || c.actionButtons[0] !== w.empty.primary) {
      fail(`[${L}/${step}] an EMPTY landing must expose ONLY the authorized `
        + `primary action; it offers ${JSON.stringify(c.actionButtons)}, `
        + `expected exactly ["${w.empty.primary}"]`);
    }
    if ((c.tertiaryButtons || []).length !== 0) {
      fail(`[${L}/${step}] an EMPTY landing still offers tertiary controls `
        + `${JSON.stringify(c.tertiaryButtons)}`);
    }
    if (c.effective !== w.empty.primary) {
      fail(`[${L}/${step}] the committed effective action is `
        + `${JSON.stringify(c.effective)} while the landing paints `
        + `${JSON.stringify(c.actionButtons)} — the copy and the control must `
        + `come from the same committed field`);
    }
    if (c.busy !== "false") {
      fail(`[${L}/${step}] a settled EMPTY card reports aria-busy="${c.busy}"`);
    }
  }

  // The hub, in the same pristine state: all six cards, Workflow 6 optional,
  // and no recommendation that names it.
  await openHub(page, `${L}/1/empty/hub`);
  await assertWorkflowSixInvariants(page, L, "1/empty/hub");
}

// (2) SUCCESS / COMPLETE, and the control case for leg 1's "exactly one".
async function legSuccessComplete(page, L, fx) {
  for (const w of WORKFLOWS) {
    const step = `2/success/${w.key}`;
    await openLanding(page, w.key, `${L}/${step}`);
    const c = await readCard(page, w.key);
    if (c.state !== w.p1.state) {
      fail(`[${L}/${step}] "${w.title}" reads "${c.state}" on the fully `
        + `provisioned Program, expected "${w.p1.state}"`);
    }
    if (JSON.stringify(c.stats) !== JSON.stringify(w.p1.stats)) {
      fail(`[${L}/${step}] "${w.title}" counts are ${JSON.stringify(c.stats)}, `
        + `expected ${JSON.stringify(w.p1.stats)}`);
    }
    if (JSON.stringify(c.actionButtons) !== JSON.stringify(w.p1.groups)) {
      fail(`[${L}/${step}] "${w.title}" offers ${JSON.stringify(c.actionButtons)}, `
        + `expected ${JSON.stringify(w.p1.groups)}`);
    }
    if (!w.optional) {
      // The SUCCESS state's own copy, and its backend-sourced status.
      if (c.status !== "done" || c.doneText !== SUCCESS_NOTE) {
        fail(`[${L}/${step}] "${w.title}" is not showing the complete state `
          + `(status "${c.status}", copy ${JSON.stringify(c.doneText)})`);
      }
      // ANTI-VACUITY for leg 1: this same landing offers MORE than one
      // control when nothing is blocked, so "exactly one in EMPTY" is a real
      // restriction rather than a property of every landing.
      if ((c.actionButtons || []).length < 2) {
        fail(`[${L}/${step}] "${w.title}" offers only `
          + `${JSON.stringify(c.actionButtons)} in its unblocked complete `
          + `state, so leg 1's "EMPTY exposes exactly one action" would be `
          + `vacuous for it`);
      }
      if (c.blockedBecause !== null) {
        fail(`[${L}/${step}] "${w.title}" still reports an unmet prerequisite `
          + `(${JSON.stringify(c.blockedBecause)}) on a fully provisioned `
          + `Program`);
      }
    }
  }
  await openHub(page, `${L}/2/success/hub`);
  const roll = await readRollup(page);
  const wantText = "5 of 5 required setup workflows you manage are done."
    + " Every required workflow you manage is done. Imports and onboarding is"
    + " optional and never blocks completion.";
  if (roll.text !== wantText) {
    fail(`[${L}/2/success/hub] the completion line reads `
      + `${JSON.stringify(roll.text)}, expected ${JSON.stringify(wantText)}`);
  }
  if (!roll.allDone || roll.done !== 5 || roll.total !== 5) {
    fail(`[${L}/2/success/hub] the roll-up is ${JSON.stringify(roll)} — every `
      + `required workflow is done, so completion must say so`);
  }
  await assertWorkflowSixInvariants(page, L, "2/success/hub");
  return fx;
}

// (3) WORKFLOW 6, the optional one — the whole of what the inherited ruling
// says the matrix must assert about it, plus the proof that its
// data-dependent states really are unreachable rather than merely untested.
//
// Called from every phase that reaches the hub, so "always visible and
// reachable" is asserted while its neighbours are complete, erroring, stale
// and mid-load — not only in the happy state.
async function assertWorkflowSixInvariants(page, L, step) {
  const chip = await readChip(page, "import");
  if (!chip.present || !chip.open) {
    fail(`[${L}/${step}] Workflow 6 is not on the hub (card=${chip.present}, `
      + `open control=${chip.open}) — it must be ALWAYS visible and reachable`);
  }
  // Neither done nor todo: it carries the optional chip and neither of the
  // done/todo classes, which app.js deliberately keeps disjoint.
  if (!chip.chip || chip.chip.text !== "Optional"
      || chip.chip.cls.indexOf("swf-optional") === -1
      || chip.chip.cls.indexOf("swf-status") !== -1) {
    fail(`[${L}/${step}] Workflow 6's hub chip is ${JSON.stringify(chip.chip)}; `
      + `it must read "Optional" and carry neither the done nor the todo status`);
  }
  const roll = await readRollup(page);
  if (!roll.slotPresent) {
    fail(`[${L}/${step}] the roll-up's stable container is missing, so any `
      + `claim about what the completion line does or does not say would be a `
      + `claim about a container that is not there`);
  }
  // Never the next recommendation — in the model AND in the sentence.
  if (roll.next === "import") {
    fail(`[${L}/${step}] the hub recommends Workflow 6 as the next task`);
  }
  if (roll.text.indexOf("Next: Imports and onboarding") !== -1) {
    fail(`[${L}/${step}] the completion line recommends Workflow 6: `
      + `${JSON.stringify(roll.text)}`);
  }
  // Never in the completion arithmetic: `required` is a partition, and the
  // optional workflow must not be in the list the arithmetic is computed over.
  if (roll.requiredKeys.indexOf("import") !== -1
      || roll.optionalKeys.join(",") !== "import") {
    fail(`[${L}/${step}] the completion partition is required=`
      + `${JSON.stringify(roll.requiredKeys)} optional=`
      + `${JSON.stringify(roll.optionalKeys)} — Workflow 6 must be in the `
      + `optional partition and in no other`);
  }
  if (roll.total !== REQUIRED.length) {
    fail(`[${L}/${step}] the completion total is ${roll.total}, expected `
      + `${REQUIRED.length} — the optional workflow is being counted as work`);
  }
  // It never blocks: a required prefix can block a recommendation, but the
  // optional workflow must never be what blocked it.
  if (roll.blockedBy === "import") {
    fail(`[${L}/${step}] the hub says Workflow 6 is what blocks the next step`);
  }
}

// (3b) Workflow 6's data-dependent states are UNREACHABLE, asserted rather
// than assumed. All three reads a card can depend on are forced to 500 at
// once; every required workflow errors, and Workflow 6 does not — it stays
// READY, keeps both its controls, stays reachable, and stays optional.
async function legOptionalCannotFail(page, L, ch) {
  const step = "3/optional-cannot-fail";
  ch.overview.mode = "fail";
  ch.players.mode = "fail";
  ch.progress.mode = "fail";
  await openHub(page, `${L}/${step}`);

  for (const w of REQUIRED) {
    const c = await readCard(page, w.key);
    if (c.state !== "error") {
      fail(`[${L}/${step}] required workflow "${w.title}" reads "${c.state}" `
        + `with every read forced to 500 — if the required cards do not fail `
        + `here, Workflow 6 not failing proves nothing`);
    }
    // The progress read is what the model prefers to report, because a
    // required workflow whose done/todo status could not be read is an
    // explicit card ERROR before the overview is even consulted.
    if (c.alertText !== `${ERR_STATUS} ${ERR_LANDING_TAIL}`
        && c.alertText !== ERR_STATUS) {
      fail(`[${L}/${step}] "${w.title}" reports ${JSON.stringify(c.alertText)}, `
        + `expected the setup-status failure sentence`);
    }
  }
  const imp = await readCard(page, "import");
  if (imp.state !== "ready" || imp.optional !== true) {
    fail(`[${L}/${step}] Workflow 6 reads state "${imp.state}" `
      + `optional=${imp.optional} with every read failing; it has no data `
      + `dependency, so EMPTY and ERROR must be unreachable for it`);
  }
  await assertWorkflowSixInvariants(page, L, step);
  // ...and it is still REACHABLE while its five neighbours are all errored.
  await openLanding(page, "import", `${L}/${step}/reachable`);
  const impLanding = await readCard(page, "import");
  if (impLanding.state !== "ready"
      || JSON.stringify(impLanding.actionButtons) !== JSON.stringify(byKey("import").p1.groups)) {
    fail(`[${L}/${step}] Workflow 6's landing under a total read failure reads `
      + `"${impLanding.state}" offering ${JSON.stringify(impLanding.actionButtons)}`);
  }
  if (impLanding.optionalNote !== OPTIONAL_NOTE) {
    fail(`[${L}/${step}] Workflow 6's landing lost its optional note under a `
      + `total read failure`);
  }
  ch.overview.mode = "pass";
  ch.players.mode = "pass";
  ch.progress.mode = "pass";
}

// (4) ONE FAILED CARD BESIDE SUCCESSFUL CARDS, and FAILED RETRY THEN
// SUCCESSFUL RETRY — on the hub grid, the only Setup surface where a card has
// neighbours to blank.
//
// Three presses, all by keyboard:
//   (a) every card errored; ONE card's Retry succeeds. Its four required
//       neighbours and Workflow 6 must be BYTE-IDENTICAL afterwards —
//       generation, committed model and painted body.
//   (b) a DIFFERENT card's Retry FAILS while the recovered neighbour is on
//       screen. Same isolation, in the harder direction: a failure must not
//       take the recovered card down with it.
//   (c) the same card's Retry then SUCCEEDS. The recovery is scoped to it.
async function legHubNeighbourIsolation(page, L, ch) {
  const step = "4/neighbours";
  ch.overview.mode = "fail";
  await openHub(page, `${L}/${step}/errored`);
  for (const w of REQUIRED) {
    const c = await readCard(page, w.key);
    if (c.state !== "error") {
      fail(`[${L}/${step}] "${w.title}" is "${c.state}" rather than ERROR, so `
        + `there is nothing to recover from`);
    }
  }
  const snapAll = () => page.evaluate((keys) => {
    const out = {};
    keys.forEach((k) => {
      const slot = document.querySelector(`[data-setup-card-slot="${k}"]`);
      out[k] = { generation: cardGenerations["setup/" + k],
                 model: JSON.stringify(cardStates["setup/" + k]),
                 html: slot ? slot.innerHTML : null,
                 hasSlot: !!slot };
    });
    return out;
  }, WORKFLOWS.map((w) => w.key));

  const assertOnlyChanged = async (before, after, changedKey, why) => {
    for (const w of WORKFLOWS) {
      const b = before[w.key];
      const a = after[w.key];
      if (!b.hasSlot || !a.hasSlot) {
        fail(`[${L}/${step}] "${w.title}" has no painted card slot, so "its `
          + `body was untouched" would be a statement about nothing`);
      }
      const same = b.generation === a.generation && b.model === a.model
        && b.html === a.html;
      if (w.key === changedKey && same) {
        fail(`[${L}/${step}] ${why}: the card that was retried did not change `
          + `at all (generation ${b.generation}), so the isolation assertions `
          + `on its neighbours are vacuous`);
      }
      if (w.key !== changedKey && !same) {
        fail(`[${L}/${step}] ${why}: the unrelated card "${w.title}" changed. `
          + `A per-card failure or recovery must never blank or re-commit a `
          + `neighbour.\n  generation ${b.generation} -> ${a.generation}`
          + `\n  model before: ${b.model}\n  model after:  ${a.model}`);
      }
    }
  };

  // (a) a SUCCESSFUL retry, beside four still-failed neighbours.
  await armAnnouncements(page);
  await resetAnnouncements(page);
  const before1 = await snapAll();
  ch.overview.mode = "pass";
  await awaitFocusQuiet(page);
  await page.focus('[data-setup-card-retry="teams"]');
  await page.keyboard.press("Enter");
  await page.waitForFunction(() => readCardState("setup/teams").state === "success",
    null, { timeout: 20000 })
    .catch(() => fail(`[${L}/${step}/a] the recovered card never settled`));
  await quiesce(page, `${L}/${step}/a`);
  await assertOnlyChanged(before1, await snapAll(), "teams",
    "a successful per-card retry");
  await assertSaidExactly(page, "Permanent teams updated.", false, L, `${step}/a`);
  await assertFocusExactly(page,
    '[data-setup-workflow-card="teams"] .swf-title', L, `${step}/a`,
    "the retried card's own heading on the hub");

  // (b) a FAILING retry on a different card, beside the recovered one.
  ch.overview.mode = "fail";
  await resetAnnouncements(page);
  const before2 = await snapAll();
  await awaitFocusQuiet(page);
  await page.focus('[data-setup-card-retry="roster"]');
  await page.keyboard.press("Enter");
  await page.waitForFunction((g) => cardGenerations["setup/roster"] > g
    && readCardState("setup/roster").state === "error",
    before2.roster.generation, { timeout: 20000 })
    .catch(() => fail(`[${L}/${step}/b] the failing retry never resolved`));
  await quiesce(page, `${L}/${step}/b`);
  await assertOnlyChanged(before2, await snapAll(), "roster",
    "a FAILING per-card retry");
  const recovered = await readCard(page, "teams");
  if (recovered.state !== "success") {
    fail(`[${L}/${step}/b] the neighbour that had already recovered is now `
      + `"${recovered.state}" — one card's failure took a successful card `
      + `down with it`);
  }
  await assertSaidExactly(page, "Still couldn't load Clubs, players and staff.",
    true, L, `${step}/b`);
  await assertFocusExactly(page,
    '[data-setup-workflow-card="roster"] .swf-title', L, `${step}/b`,
    "the failed card's own heading on the hub");

  // (c) the SAME card's retry, now succeeding — scoped to it.
  ch.overview.mode = "pass";
  await resetAnnouncements(page);
  const before3 = await snapAll();
  await awaitFocusQuiet(page);
  await page.focus('[data-setup-card-retry="roster"]');
  await page.keyboard.press("Enter");
  await page.waitForFunction(() => readCardState("setup/roster").state === "success",
    null, { timeout: 20000 })
    .catch(() => fail(`[${L}/${step}/c] the second retry never succeeded`));
  await quiesce(page, `${L}/${step}/c`);
  await assertOnlyChanged(before3, await snapAll(), "roster",
    "a failed retry followed by a successful one");
  await assertSaidExactly(page, "Clubs, players and staff updated.", false,
    L, `${step}/c`);
  await assertWorkflowSixInvariants(page, L, `${step}/c`);
}

// (5) ERROR + LOADING + KEYBOARD RETRY + EXACT FOCUS, on each landing.
//
// The full per-landing round trip, for every workflow that has a read that can
// fail. `roster` runs twice, because it is the only workflow with a SECOND
// read (/api/players) and the two failures must produce two different
// sentences.
async function legErrorAndLoadingPerLanding(page, L, ch) {
  const cases = [];
  for (const w of REQUIRED) {
    cases.push({ w: w, channel: "overview", sentence: ERR_OVERVIEW });
  }
  cases.push({ w: byKey("roster"), channel: "players", sentence: ERR_PLAYERS });

  for (const c of cases) {
    const w = c.w;
    const step = `5/${w.key}/${c.channel}`;
    // --- ERROR, produced by the shared render with this read forced to 500.
    ch[c.channel].mode = "fail";
    await openLanding(page, w.key, `${L}/${step}`);
    let card = await readCard(page, w.key);
    if (card.state !== "error") {
      fail(`[${L}/${step}] the landing reads "${card.state}" with `
        + `${c.channel} forced to 500, expected ERROR`);
    }
    if (card.alertText !== `${c.sentence} ${ERR_LANDING_TAIL}`) {
      fail(`[${L}/${step}] the ERROR body reads ${JSON.stringify(card.alertText)}, `
        + `expected ${JSON.stringify(`${c.sentence} ${ERR_LANDING_TAIL}`)}`);
    }
    if (card.alertRole !== "alert") {
      fail(`[${L}/${step}] the ERROR sentence is not role="alert" `
        + `(role=${JSON.stringify(card.alertRole)})`);
    }
    if ((card.retryControls || []).length !== 1 || card.retryControls[0] !== "Retry") {
      fail(`[${L}/${step}] the ERROR body offers `
        + `${JSON.stringify(card.retryControls)} rather than exactly one Retry`);
    }
    // The ERROR state deliberately KEEPS the landing's actions: the copy says
    // so ("The action below still works"), and a card whose actions vanished
    // would contradict its own sentence.
    if (JSON.stringify(card.actionButtons) !== JSON.stringify(w.p1.groups)) {
      fail(`[${L}/${step}] the ERROR landing offers `
        + `${JSON.stringify(card.actionButtons)}, but its own copy promises the `
        + `actions still work; expected ${JSON.stringify(w.p1.groups)}`);
    }

    // --- keyboard REACHABILITY of the retry, from the landing's own anchor.
    const hops = await tabToAttribute(page, "data-setup-card-retry", w.key,
      L, step, "the per-card Retry",
      `[data-setup-workflow-landing="${w.key}"] .swf-back`);
    if (hops < 1) fail(`[${L}/${step}] impossible hop count ${hops}`);

    // --- LOADING, with the retry's own read HELD. The press is ENTER on the
    //     control TAB just landed on, so activation is genuinely keyboard.
    const held = armHold(ch[c.channel]);
    await armAnnouncements(page);
    await resetAnnouncements(page);
    const errGeneration = card.generation;
    await page.keyboard.press("Enter");
    await held;
    await quiesce(page, `${L}/${step}/loading`, 1);
    card = await readCard(page, w.key);
    if (card.state !== "loading") {
      fail(`[${L}/${step}/loading] the card reads "${card.state}" while its own `
        + `retry read is unresolved`);
    }
    if (card.generation === errGeneration) {
      fail(`[${L}/${step}/loading] the retry did not issue a new generation `
        + `(still ${errGeneration}), so it was refused rather than started`);
    }
    if (card.busy !== "true") {
      fail(`[${L}/${step}/loading] aria-busy is "${card.busy}" while a read is `
        + `in flight; assistive technology is told the region is idle`);
    }
    if (!card.skeleton || card.skeletonLabel !== `Loading ${w.title.toLowerCase()}…`) {
      fail(`[${L}/${step}/loading] the LOADING body is skeleton=${card.skeleton} `
        + `label=${JSON.stringify(card.skeletonLabel)}, expected a labelled `
        + `skeleton naming "${w.title.toLowerCase()}"`);
    }
    if (!card.hasActionsBox) {
      fail(`[${L}/${step}/loading] the landing's action container is GONE, so `
        + `"no controls while loading" would be satisfied by absence`);
    }
    if ((card.actionButtons || []).length !== 0
        || (card.slotButtons || []).length !== 0) {
      fail(`[${L}/${step}/loading] the LOADING landing still offers controls `
        + `(card ${JSON.stringify(card.slotButtons)}, landing `
        + `${JSON.stringify(card.actionButtons)}) — nothing is known about this `
        + `card yet, so the primary must be held back with the rest`);
    }
    if ((await spoken(page)).length !== 0) {
      fail(`[${L}/${step}/loading] the live region spoke while the read was `
        + `still unresolved: ${JSON.stringify(await spoken(page))}`);
    }

    // --- release into SUCCESS: exactly one announcement, exact focus.
    ch[c.channel].mode = "pass";
    ch[c.channel].release();
    await page.waitForFunction((k) => {
      const e = readCardState("setup/" + k);
      return e.state !== "loading" && e.state !== "pending";
    }, w.key, { timeout: 20000 })
      .catch(() => fail(`[${L}/${step}/recover] the released retry never settled`));
    await quiesce(page, `${L}/${step}/recover`);
    card = await readCard(page, w.key);
    if (card.state !== w.p1.state) {
      fail(`[${L}/${step}/recover] the recovered card reads "${card.state}", `
        + `expected "${w.p1.state}"`);
    }
    await assertSaidExactly(page, `${w.title} updated.`, false, L, `${step}/recover`);
    await assertFocusExactly(page,
      `[data-setup-workflow-landing="${w.key}"] .swf-landing-title`,
      L, `${step}/recover`, "the landing's own heading after a retry");
  }
}

// (6) A DELAYED STALE SUCCESS ARRIVING AFTER A NEWER FAILURE, same tuple.
//
// The shape, per landing:
//   CONTROL   the identical held success, released with NOTHING superseding
//             it, MUST turn the card from ERROR into its settled state and
//             announce. Without this the negative below could pass on a build
//             that simply dropped every held response.
//   RACE      the same held success, with an ordinary SAME-TUPLE render run
//             underneath it through the landing's own back control (and the
//             read forced to fail on that render, so the newer answer is a
//             FAILURE). The older success must change nothing: the card stays
//             ERROR at the newer generation, its counts are not restored and
//             its obsolete primary action is not restored.
async function legRaceAfterNewerFailure(page, L, ch) {
  for (const w of REQUIRED) {
    const step = `6/${w.key}`;

    // ---------------------------- CONTROL ---------------------------------
    ch.overview.mode = "fail";
    await openLanding(page, w.key, `${L}/${step}/control`);
    let card = await readCard(page, w.key);
    if (card.state !== "error") {
      fail(`[${L}/${step}/control] the card is "${card.state}" rather than `
        + `ERROR, so there is no Retry to hold`);
    }
    let held = armHold(ch.overview);
    await armAnnouncements(page);
    await resetAnnouncements(page);
    await awaitFocusQuiet(page);
    await page.focus(`[data-setup-card-retry="${w.key}"]`);
    await page.keyboard.press("Enter");
    await held;
    ch.overview.mode = "pass";
    ch.overview.release();
    await page.waitForFunction((k) => readCardState("setup/" + k).state !== "loading",
      w.key, { timeout: 20000 })
      .catch(() => fail(`[${L}/${step}/control] the released response never settled`));
    await quiesce(page, `${L}/${step}/control`);
    card = await readCard(page, w.key);
    if (card.state !== w.p1.state || JSON.stringify(card.stats) !== JSON.stringify(w.p1.stats)) {
      fail(`[${L}/${step}/control] releasing a held success with nothing `
        + `superseding it must settle the card; it reads "${card.state}" with `
        + `${JSON.stringify(card.stats)}. Without this the race assertion below `
        + `would be vacuous`);
    }
    await assertSaidExactly(page, `${w.title} updated.`, false, L, `${step}/control`);

    // ------------------------------ RACE ----------------------------------
    ch.overview.mode = "fail";
    await sameTupleRenderThroughUi(page, w.key, `${L}/${step}/arm`);
    await settledLanding(page, w.key, `${L}/${step}/arm`);
    card = await readCard(page, w.key);
    if (card.state !== "error") {
      fail(`[${L}/${step}/arm] the card is "${card.state}" rather than ERROR`);
    }
    held = armHold(ch.overview);
    await resetAnnouncements(page);
    await awaitFocusQuiet(page);
    await page.focus(`[data-setup-card-retry="${w.key}"]`);
    await page.keyboard.press("Enter");
    await held;
    ch.overview.mode = "fail";
    await quiesce(page, `${L}/${step}/held`, 1);
    const loading = await readCard(page, w.key);
    if (loading.state !== "loading") {
      fail(`[${L}/${step}/held] the card reads "${loading.state}" with its own `
        + `read held`);
    }

    // The NEWER answer: an ordinary same-tuple render, through real controls,
    // with the read failing.
    await sameTupleRenderThroughUi(page, w.key, `${L}/${step}/render`);
    await page.waitForFunction((k) => readCardState("setup/" + k).state === "error",
      w.key, { timeout: 20000 })
      .catch(() => fail(`[${L}/${step}/render] the same-tuple render never `
        + `committed the newer FAILURE this leg needs`));
    await quiesce(page, `${L}/${step}/render`, 1);
    const newer = await readCard(page, w.key);
    // NON-VACUITY: the render must really have superseded the held read's
    // identity, or the discard below would be explained by nothing having
    // happened at all.
    if (!(newer.counter > loading.counter)) {
      fail(`[${L}/${step}/render] the card's generation counter did not move `
        + `(${loading.counter} -> ${newer.counter}), so the held response was `
        + `never superseded and the assertion below proves nothing`);
    }
    // Focus must have stopped moving on its own before the snapshot: a late
    // focusContentHeading() poll landing between the two samples would look
    // exactly like the released response having stolen focus.
    await awaitFocusQuiet(page);
    const before = await snapshot(page, w.key);
    // The release has to be OBSERVED arriving: on a correct build it changes
    // nothing the page can be polled for, so waiting on the page itself would
    // be waiting for a timeout and calling it a pass.
    const releasedBefore = ch.overview.released;
    ch.overview.release();
    const deadline = Date.now() + 20000;
    while (ch.overview.released <= releasedBefore && Date.now() < deadline) {
      await page.waitForTimeout(100);
    }
    if (ch.overview.released <= releasedBefore) {
      fail(`[${L}/${step}/release] the held response was never handed to the `
        + `page, so nothing was actually raced`);
    }
    await quiesce(page, `${L}/${step}/release`);
    const after = await snapshot(page, w.key);
    assertSame(before, after, L, `${step}/release`,
      "releasing the superseded read's delayed SUCCESS into a card whose newer "
      + "answer was a FAILURE");
    const settled = await readCard(page, w.key);
    if (settled.state !== "error") {
      fail(`[${L}/${step}/release] the card is now "${settled.state}" — an older `
        + `response replaced the newer failure`);
    }
    if ((settled.stats || []).length !== 0) {
      fail(`[${L}/${step}/release] the older response restored counts `
        + `${JSON.stringify(settled.stats)} onto a card whose newer answer had `
        + `none`);
    }
    if (settled.effective !== "undefined") {
      fail(`[${L}/${step}/release] the older response restored the obsolete `
        + `effective action ${JSON.stringify(settled.effective)}`);
    }
    if ((await spoken(page)).length !== 0) {
      fail(`[${L}/${step}/release] the superseded response spoke into the live `
        + `region: ${JSON.stringify(await spoken(page))}`);
    }
    ch.overview.mode = "pass";
  }
}

// (7) STALE, and A DELAYED STALE SUCCESS ARRIVING AFTER A CONTEXT SWITCH.
//
// One pass per workflow, moving P1 -> P2 -> P1 through the REAL #ctx-select
// each time, so both directions of the switch are exercised across the six.
//
//   (a) STALE. /api/v2/setup/progress — the LAST read render() awaits before
//       it commits the new tuple's card models — is held open, which makes the
//       window in which the retained cards have been repainted but not yet
//       replaced OBSERVABLE rather than raced. contextSwitchIntentPending is
//       asserted to be ALREADY FALSE at the sample, so the withdrawal of the
//       landing's action groups is attributable to STALE itself and not to a
//       switch that has not reconciled.
//   (b) THE RACE. Back on the new tuple, the card is failed, its Retry is
//       pressed with the overview HELD, the operator switches BACK, and the
//       held success is released into the returned tuple. Nothing may move —
//       and in particular the primary action of the tuple the operator is now
//       on must not be replaced by the one the held response was carrying.
async function legStaleAndContextRace(page, L, ch, fx) {
  for (const w of WORKFLOWS) {
    const step = `7/${w.key}`;
    // --------------------- (a) the STALE window ---------------------------
    await openLanding(page, w.key, `${L}/${step}/before`);
    const onP1 = await readCard(page, w.key);
    if (onP1.state !== w.p1.state) {
      fail(`[${L}/${step}/before] the card must be settled on P1 before the `
        + `switch; it reads "${onP1.state}"`);
    }
    const heldProgress = armHold(ch.progress);
    await switchContext(page, fx.p2, fx.s2, `${L}/${step}/switch`);
    await heldProgress;
    await page.waitForFunction((k) => !contextSwitchIntentPending
      && readCardState("setup/" + k).state === "stale", w.key, { timeout: 30000 })
      .catch(async () => fail(`[${L}/${step}/stale] the STALE window never `
        + `opened: ${JSON.stringify(await page.evaluate((k) => ({
            intentPending: !!contextSwitchIntentPending,
            state: readCardState("setup/" + k).state }), w.key))}`));
    await quiesce(page, `${L}/${step}/stale`, 1);
    const stale = await readCard(page, w.key);
    if (stale.intentPending) {
      fail(`[${L}/${step}/stale] contextSwitchIntentPending is still set, so the `
        + `withdrawn action groups below would be explained by the switch `
        + `rather than by the STALE state`);
    }
    if (stale.state !== "stale" || stale.staleFrom !== w.p1.state) {
      fail(`[${L}/${step}/stale] the card reads "${stale.state}" `
        + `(from "${stale.staleFrom}"), expected STALE carried over from `
        + `"${w.p1.state}"`);
    }
    // The retained read is still on screen and is LABELLED as earlier data.
    if (JSON.stringify(stale.stats) !== JSON.stringify(w.p1.stats)) {
      fail(`[${L}/${step}/stale] STALE dropped the retained counts: `
        + `${JSON.stringify(stale.stats)}, expected ${JSON.stringify(w.p1.stats)}`);
    }
    if (stale.staleText !== STALE_NOTE) {
      fail(`[${L}/${step}/stale] the STALE label reads `
        + `${JSON.stringify(stale.staleText)}`);
    }
    if ((stale.retryControls || []).length !== 1
        || stale.retryControls[0] !== "Refresh") {
      fail(`[${L}/${step}/stale] STALE must offer exactly one Refresh; it offers `
        + `${JSON.stringify(stale.retryControls)}`);
    }
    if (!stale.hasActionsBox) {
      fail(`[${L}/${step}/stale] the landing's action container is gone, so `
        + `"every mutation control withdrawn" would be satisfied by absence`);
    }
    if ((stale.actionButtons || []).length !== 0) {
      fail(`[${L}/${step}/stale] a STALE landing still offers `
        + `${JSON.stringify(stale.actionButtons)} — a mutation fired from here `
        + `would act on the CURRENT tuple using the OLD one's evidence`);
    }
    if (stale.busy !== "false") {
      fail(`[${L}/${step}/stale] a STALE card reports aria-busy="${stale.busy}"; `
        + `it is showing data, not loading it`);
    }
    if (w.optional) {
      // ===== WORKFLOW 6's LOADING, reachable ONLY from here =====
      // It has no inventory, so no render can put it into LOADING and no read
      // failure can error it — but STALE gives it a Refresh, and that Refresh
      // re-reads the progress route. Pressed while that read is ALREADY held,
      // it is the one path that puts the optional card into LOADING at all,
      // which is why this leg carries the assertion rather than leg 5.
      //
      // The channel is NOT re-armed: the render's own read is still parked on
      // the existing gate, and replacing it would strand that request forever.
      // The second arrival is observed through the channel's held count
      // instead, and one release frees both.
      const heldBefore = ch.progress.heldNow;
      await awaitFocusQuiet(page);
      await page.focus(`[data-setup-card-retry="${w.key}"]`);
      await page.keyboard.press("Enter");
      const optDeadline = Date.now() + 20000;
      while (ch.progress.heldNow <= heldBefore && Date.now() < optDeadline) {
        await page.waitForTimeout(100);
      }
      if (ch.progress.heldNow <= heldBefore) {
        fail(`[${L}/${step}/optional-loading] the STALE Refresh never issued a `
          + `read of its own, so Workflow 6 was never put into LOADING`);
      }
      await quiesce(page, `${L}/${step}/optional-loading`, ch.progress.heldNow);
      const optLoading = await readCard(page, w.key);
      if (optLoading.state !== "loading") {
        fail(`[${L}/${step}/optional-loading] Workflow 6 reads `
          + `"${optLoading.state}" with its own refresh read unresolved`);
      }
      if (optLoading.busy !== "true") {
        fail(`[${L}/${step}/optional-loading] aria-busy is "${optLoading.busy}" `
          + `while Workflow 6's read is in flight`);
      }
      if (!optLoading.skeleton
          || optLoading.skeletonLabel !== `Loading ${w.title.toLowerCase()}…`) {
        fail(`[${L}/${step}/optional-loading] the LOADING body is `
          + `skeleton=${optLoading.skeleton} label=`
          + `${JSON.stringify(optLoading.skeletonLabel)}`);
      }
      if (!optLoading.hasActionsBox) {
        fail(`[${L}/${step}/optional-loading] the action container is gone, so `
          + `"no controls while loading" would be satisfied by absence`);
      }
      if ((optLoading.actionButtons || []).length !== 0
          || (optLoading.slotButtons || []).length !== 0) {
        fail(`[${L}/${step}/optional-loading] Workflow 6 still offers controls `
          + `while loading (card ${JSON.stringify(optLoading.slotButtons)}, `
          + `landing ${JSON.stringify(optLoading.actionButtons)})`);
      }
    }

    // Release, and let the new tuple's own answer land.
    ch.progress.mode = "pass";
    ch.progress.release();
    await waitForContextReconciled(page, fx.p2, fx.s2, `${L}/${step}/reconcile`);
    await settledLanding(page, w.key, `${L}/${step}/onP2`);
    const onP2 = await readCard(page, w.key);
    if (onP2.state !== w.p2.state
        || JSON.stringify(onP2.stats) !== JSON.stringify(w.p2.stats)) {
      fail(`[${L}/${step}/onP2] after the switch the card reads "${onP2.state}" `
        + `with ${JSON.stringify(onP2.stats)}, expected "${w.p2.state}" with `
        + `${JSON.stringify(w.p2.stats)}`);
    }
    if (JSON.stringify(onP2.actionButtons) !== JSON.stringify(w.p2.groups)) {
      fail(`[${L}/${step}/onP2] the settled landing offers `
        + `${JSON.stringify(onP2.actionButtons)}, expected `
        + `${JSON.stringify(w.p2.groups)}`);
    }
    // The SETTLED shape of the tuple the held response will belong to. Taken
    // here, while it is genuinely settled, because the non-vacuity check below
    // has to compare two real answers — comparing a mid-load surface with a
    // settled one would differ for reasons that have nothing to do with the
    // tuple.
    await awaitFocusQuiet(page);
    const settledOnP2 = await snapshot(page, w.key);

    if (w.optional) {
      // Workflow 6's committed model carries no context-scoped data at all, so
      // there is nothing for a delayed response of its own to corrupt. That is
      // asserted rather than assumed: the two tuples' models must be identical
      // apart from the identity record they were committed under.
      const strip = (m) => {
        const c = Object.assign({}, m);
        delete c.identity;
        return JSON.stringify(c);
      };
      const a = await page.evaluate(() => JSON.parse(JSON.stringify(cardStates["setup/import"])));
      await switchContext(page, fx.p1, fx.s1, `${L}/${step}/back`);
      await waitForContextReconciled(page, fx.p1, fx.s1, `${L}/${step}/back`);
      await settledLanding(page, w.key, `${L}/${step}/back`);
      const b = await page.evaluate(() => JSON.parse(JSON.stringify(cardStates["setup/import"])));
      if (strip(a) !== strip(b)) {
        fail(`[${L}/${step}/optional] Workflow 6's committed model differs `
          + `across tuples, so the claim that a delayed response of its own `
          + `could not corrupt anything is false.\n  P2: ${strip(a)}\n  P1: ${strip(b)}`);
      }
      if (a.identity.season_id === b.identity.season_id) {
        fail(`[${L}/${step}/optional] Workflow 6's identity did not move with `
          + `the tuple, so the comparison above compared the same thing twice`);
      }
      // ...and it is still there, and still optional, after a full round trip
      // through two Programs. Asserted on the HUB, which is where the card,
      // its chip and the roll-up live.
      await openHub(page, `${L}/${step}/optional`);
      await assertWorkflowSixInvariants(page, L, `${step}/optional`);
      continue;
    }

    // ------------- (b) the delayed success after the switch ---------------
    ch.overview.mode = "fail";
    await sameTupleRenderThroughUi(page, w.key, `${L}/${step}/arm`);
    await settledLanding(page, w.key, `${L}/${step}/arm`);
    let card = await readCard(page, w.key);
    if (card.state !== "error") {
      fail(`[${L}/${step}/arm] the card is "${card.state}" rather than ERROR on `
        + `P2, so there is no Retry to hold`);
    }
    const heldOverview = armHold(ch.overview);
    await armAnnouncements(page);
    await resetAnnouncements(page);
    await awaitFocusQuiet(page);
    await page.focus(`[data-setup-card-retry="${w.key}"]`);
    await page.keyboard.press("Enter");
    await heldOverview;
    ch.overview.mode = "pass";
    await quiesce(page, `${L}/${step}/heldOnP2`, 1);
    const heldState = await readCard(page, w.key);
    if (heldState.state !== "loading" || heldState.programId !== fx.p2) {
      fail(`[${L}/${step}/heldOnP2] the held read is not bound to P2 `
        + `(state "${heldState.state}", program ${heldState.programId})`);
    }

    // Back to P1, through the real switcher, and let it settle completely.
    await switchContext(page, fx.p1, fx.s1, `${L}/${step}/return`);
    await waitForContextReconciled(page, fx.p1, fx.s1, `${L}/${step}/return`);
    await settledLanding(page, w.key, `${L}/${step}/return`, 1);
    const returned = await readCard(page, w.key);
    if (returned.state !== w.p1.state || returned.programId !== fx.p1) {
      fail(`[${L}/${step}/return] the returned tuple never settled `
        + `(state "${returned.state}", program ${returned.programId})`);
    }
    await resetAnnouncements(page);
    await awaitFocusQuiet(page);
    const before = await snapshot(page, w.key);
    // NON-VACUITY: the tuple the held response belongs to and the tuple on
    // screen must genuinely produce different settled surfaces, or "nothing
    // changed" would be true no matter which of them won.
    assertDiffers(settledOnP2, before, L, `${step}/return`,
      "the held read's own tuple and the returned tuple");

    const releasedBefore = ch.overview.released;
    ch.overview.release();
    const deadline = Date.now() + 20000;
    while (ch.overview.released <= releasedBefore && Date.now() < deadline) {
      await page.waitForTimeout(100);
    }
    if (ch.overview.released <= releasedBefore) {
      fail(`[${L}/${step}/release] the held response was never handed to the `
        + `page, so nothing was raced`);
    }
    await quiesce(page, `${L}/${step}/release`);
    const after = await snapshot(page, w.key);
    assertSame(before, after, L, `${step}/release`,
      "releasing a response belonging to the Program/Season the operator left");
    const finalCard = await readCard(page, w.key);
    if (finalCard.state !== w.p1.state
        || JSON.stringify(finalCard.stats) !== JSON.stringify(w.p1.stats)) {
      fail(`[${L}/${step}/release] the older tuple's response replaced the `
        + `current context data: the card is "${finalCard.state}" with `
        + `${JSON.stringify(finalCard.stats)}`);
    }
    if (JSON.stringify(finalCard.actionButtons) !== JSON.stringify(w.p1.groups)) {
      fail(`[${L}/${step}/release] the older tuple's response restored an `
        + `obsolete action set ${JSON.stringify(finalCard.actionButtons)}, `
        + `expected ${JSON.stringify(w.p1.groups)}`);
    }
    if (w.primaryDiffersAcrossTuples && finalCard.effective === w.p2.primary) {
      fail(`[${L}/${step}/release] the landing's primary action is now the one `
        + `the LEFT tuple resolved to (${JSON.stringify(w.p2.primary)})`);
    }
    if ((await spoken(page)).length !== 0) {
      fail(`[${L}/${step}/release] the superseded response spoke into the live `
        + `region: ${JSON.stringify(await spoken(page))}`);
    }
  }
}

// (8a) THE CONFIRMATION state on Workflow 6, driven entirely by keyboard.
//
// This is the one DECLARED confirmation in SETUP_WORKFLOWS, and it is the only
// one whose completion leaves the Setup surface — which makes it the case
// where "exact focus after confirmation and completion" means a different
// destination's heading rather than the card's own.
async function legConfirmImport(page, L) {
  const step = "8a/import-confirm";
  await openLanding(page, "import", `${L}/${step}`);
  await armAnnouncements(page);

  // --- open, by TAB + ENTER on the landing's own secondary control.
  await resetAnnouncements(page);
  await tabToAttribute(page, "data-setup-card-ask", "import", L, step,
    "Workflow 6's confirmation control",
    '[data-setup-workflow-landing="import"] .swf-back');
  await page.keyboard.press("Enter");
  await page.waitForFunction(() => readCardState("setup/import").state === "confirm",
    null, { timeout: 15000 })
    .catch(() => fail(`[${L}/${step}] the confirmation never opened`));
  await quiesce(page, `${L}/${step}/open`);
  let card = await readCard(page, "import");
  if (card.confirmPrompt !== WIZARD_PROMPT) {
    fail(`[${L}/${step}/open] the confirmation prompt reads `
      + `${JSON.stringify(card.confirmPrompt)}`);
  }
  if (!card.hasActionsBox) {
    fail(`[${L}/${step}/open] the landing's action container disappeared, so `
      + `"the confirmation withdraws the other controls" would be satisfied by `
      + `absence`);
  }
  if ((card.actionButtons || []).length !== 0) {
    fail(`[${L}/${step}/open] the landing still offers `
      + `${JSON.stringify(card.actionButtons)} beside an open confirmation — a `
      + `confirmation that leaves the action it is confirming live is not a `
      + `confirmation`);
  }
  if (JSON.stringify(card.slotButtons) !== JSON.stringify(["Start the wizard", "Stay here"])) {
    fail(`[${L}/${step}/open] the confirmation offers `
      + `${JSON.stringify(card.slotButtons)}`);
  }
  await assertSaidExactly(page, WIZARD_PROMPT, false, L, `${step}/open`);
  await assertFocusExactly(page, '[data-setup-card-confirm-yes="import"]',
    L, `${step}/open`, "the confirmation's own affirmative control");

  // --- CANCEL, by keyboard: focus must return to the control that opened it.
  await resetAnnouncements(page);
  await page.keyboard.press("Tab");
  const onNo = await page.evaluate(() => !!(document.activeElement
    && document.activeElement.hasAttribute
    && document.activeElement.hasAttribute("data-setup-card-confirm-no")));
  if (!onNo) {
    fail(`[${L}/${step}/cancel] TAB from the affirmative control did not reach `
      + `the cancel control; focus is on ${await describeFocus(page)}`);
  }
  await page.keyboard.press("Enter");
  await page.waitForFunction(() => readCardState("setup/import").state !== "confirm",
    null, { timeout: 15000 })
    .catch(() => fail(`[${L}/${step}/cancel] the confirmation never closed`));
  await quiesce(page, `${L}/${step}/cancel`);
  await assertSaidExactly(page, WIZARD_CANCELLED, false, L, `${step}/cancel`);
  await assertFocusExactly(page, '[data-setup-card-ask="import"]',
    L, `${step}/cancel`, "the control that opened the confirmation");
  card = await readCard(page, "import");
  if (JSON.stringify(card.actionButtons) !== JSON.stringify(byKey("import").p1.groups)) {
    fail(`[${L}/${step}/cancel] cancelling did not restore the landing's own `
      + `controls: ${JSON.stringify(card.actionButtons)}`);
  }

  // --- CONFIRM, by keyboard: the completion announcement and the exact focus
  //     on the destination it opens.
  await resetAnnouncements(page);
  await page.keyboard.press("Enter");
  await page.waitForFunction(() => readCardState("setup/import").state === "confirm",
    null, { timeout: 15000 })
    .catch(() => fail(`[${L}/${step}/again] the confirmation never re-opened`));
  await assertFocusExactly(page, '[data-setup-card-confirm-yes="import"]',
    L, `${step}/again`, "the confirmation's own affirmative control");
  await resetAnnouncements(page);
  await page.keyboard.press("Enter");
  await page.waitForFunction(() => document.body.dataset.view === "onboarding",
    null, { timeout: 20000 })
    .catch(() => fail(`[${L}/${step}/confirm] confirming did not open the `
      + `Initial Setup wizard`));
  await quiesce(page, `${L}/${step}/confirm`);
  // WHAT THIS ASSERTS (#365 review round 10 — this used to stop halfway).
  //
  // (1) The declared completion sentence was written to the ONE sitewide live
  //     region, exactly once, with nothing else said alongside it and no
  //     duplicate of it — measured on the RAW ledger, which no longer collapses
  //     consecutive identical writes.
  //
  // (2) It SURVIVED THE NAVIGATION it triggered. This confirmation's completion
  //     opens the Initial Setup wizard, and switchTab() sets `toast = ""` and
  //     re-runs updateToast(). Announcing before navigating therefore wrote the
  //     sentence and withdrew it inside ONE synchronous task — populated and
  //     empty again before the browser painted, so nothing could be exposed.
  //     Production now announces AFTER the destination render
  //     (announceCardStatusAfter), and this proves the outcome rather than the
  //     mechanism: the region is sampled across a real task AND paint boundary
  //     (two animation frames, then a macrotask) and must still be VISIBLE and
  //     still carrying the sentence.
  const said = await spoken(page);
  assertNoRepeatedSpeech(said, L, `${step}/confirm`);
  if (said.length !== 1 || said[0].text !== WIZARD_DONE) {
    fail(`[${L}/${step}/confirm] the completion announcement was `
      + `${JSON.stringify(said)}, expected exactly one ${JSON.stringify(WIZARD_DONE)} `
      + `written to the live region`);
  }
  const exposed = await page.evaluate(() => new Promise((resolve) => {
    // Two frames, then a macrotask: a message removed in the same task as the
    // announcement (or in the microtask drain after it) is gone before the
    // first of these resolves.
    requestAnimationFrame(() => requestAnimationFrame(() => setTimeout(() => {
      const root = document.getElementById("toast-root");
      const msg = root && root.querySelector(".toast-msg");
      resolve({
        present: !!root,
        hidden: !!(root && root.hidden),
        text: msg ? (msg.textContent || "").trim() : null,
        view: document.body.dataset.view,
      });
    }, 0)));
  }));
  if (!exposed.present || exposed.hidden || exposed.text !== WIZARD_DONE) {
    fail(`[${L}/${step}/confirm] the completion sentence did not SURVIVE the `
      + `navigation it triggered: a task and two paints after the wizard `
      + `opened, the live region is ${JSON.stringify(exposed)} — a message `
      + `written and withdrawn before the browser paints cannot be exposed to `
      + `assistive technology at all, so it was never announced`);
  }
  if (exposed.view !== "onboarding") {
    fail(`[${L}/${step}/confirm] the survival check was taken somewhere other `
      + `than the destination this confirmation navigates to (view `
      + `${JSON.stringify(exposed.view)}), so it proves nothing about surviving `
      + `the navigation`);
  }
  // Exact focus after completion: the destination's own first heading, which
  // is what focusContentHeading() targets.
  const headed = await page.evaluate(() => {
    const h = document.querySelector("#content h1, #content h2, #content h3, #content .section-title");
    return { isFocused: !!h && document.activeElement === h,
             tag: h && h.tagName, text: h && h.textContent.replace(/\s+/g, " ").trim(),
             active: document.activeElement && document.activeElement.tagName };
  });
  if (!headed.isFocused) {
    fail(`[${L}/${step}/confirm] after the confirmation completed, focus is on `
      + `<${headed.active}> rather than on the destination's own first heading `
      + `(<${headed.tag}> ${JSON.stringify(headed.text)})`);
  }
}

// (8b/8c) THE CONFIRMATION state on a DERIVED action: the reopen an archived
// Season puts in front of `facilities` and `participation`.
//
// 8b runs the non-destructive halves on `facilities` — open, the blank-reason
// refusal #159 requires, and cancel — so the Season is still archived for 8c.
// 8c then runs the one COMPLETION on `participation`: the write really
// commits, so it must be last.
async function legConfirmReopen(page, L, fx) {
  // Leg 8a finishes on the Initial Setup wizard, which is a different view.
  // Come back to Setup through the real nav before moving the context, so the
  // switch below renders the surface these assertions are about.
  await openHub(page, `${L}/8b/return-to-setup`);
  await switchContext(page, fx.p3, fx.s3, `${L}/8b/switch`);
  await waitForContextReconciled(page, fx.p3, fx.s3, `${L}/8b/switch`);

  // ---------------------------- 8b: facilities --------------------------
  let step = "8b/facilities-confirm";
  await openLanding(page, "facilities", `${L}/${step}`);
  await armAnnouncements(page);
  let card = await readCard(page, "facilities");
  if (card.effective !== REOPEN_LABEL
      || JSON.stringify(card.actionButtons) !== JSON.stringify([REOPEN_LABEL])) {
    fail(`[${L}/${step}] the archived-Season fixture does not offer the reopen `
      + `path (effective ${JSON.stringify(card.effective)}, actions `
      + `${JSON.stringify(card.actionButtons)}) — there is no confirmation to `
      + `open`);
  }
  await resetAnnouncements(page);
  await tabToAttribute(page, "data-setup-card-ask", "facilities", L, step,
    "the reopen confirmation control",
    '[data-setup-workflow-landing="facilities"] .swf-back');
  await page.keyboard.press("Enter");
  await page.waitForFunction(() => readCardState("setup/facilities").state === "confirm",
    null, { timeout: 15000 })
    .catch(() => fail(`[${L}/${step}] the reopen confirmation never opened`));
  await quiesce(page, `${L}/${step}/open`);
  card = await readCard(page, "facilities");
  if (card.confirmPrompt !== REOPEN_PROMPT) {
    fail(`[${L}/${step}/open] the prompt reads ${JSON.stringify(card.confirmPrompt)}`);
  }
  if (!card.hasActionsBox || (card.actionButtons || []).length !== 0) {
    fail(`[${L}/${step}/open] the landing's action groups are `
      + `${JSON.stringify(card.actionButtons)} (container present=`
      + `${card.hasActionsBox}) beside an open confirmation`);
  }
  await assertSaidExactly(page, REOPEN_PROMPT, false, L, `${step}/open`);
  // A confirmation that REQUIRES a reason lands focus on the field the
  // operator has to fill, not on the button that would reject it.
  await assertFocusExactly(page, '[data-setup-card-confirm-reason="facilities"]',
    L, `${step}/open`, "the required-reason field");

  // The blank-reason refusal: the confirmation stays OPEN, says so once, and
  // puts focus back on the field.
  await resetAnnouncements(page);
  await page.keyboard.press("Tab");
  await page.keyboard.press("Enter");
  await page.waitForFunction(() => {
    const slot = document.querySelector('[data-setup-card-slot="facilities"]');
    const e = slot && slot.querySelector(".swf-card-error");
    return !!e && e.textContent.trim() === "Add a reason before reopening.";
  }, null, { timeout: 15000 })
    .catch(() => fail(`[${L}/${step}/blank] confirming with a blank reason did `
      + `not keep the confirmation open with its own error`));
  await quiesce(page, `${L}/${step}/blank`);
  card = await readCard(page, "facilities");
  if (card.state !== "confirm") {
    fail(`[${L}/${step}/blank] the card left CONFIRM on a blank reason `
      + `("${card.state}") — #159 requires a non-empty reason, so a blank one `
      + `must not fire the write`);
  }
  await assertSaidExactly(page, REOPEN_NO_REASON, true, L, `${step}/blank`);
  await assertFocusExactly(page, '[data-setup-card-confirm-reason="facilities"]',
    L, `${step}/blank`, "the reason field the operator still has to fill");

  // Cancel: said once, focus back on the control that opened it, controls back.
  await resetAnnouncements(page);
  await page.evaluate(() => {
    const buttons = Array.from(document.querySelectorAll(
      '[data-setup-card-slot="facilities"] button'));
    const no = buttons.find((b) => b.hasAttribute("data-setup-card-confirm-no"));
    if (no) no.focus();
  });
  const onNo = await page.evaluate(() => !!(document.activeElement
    && document.activeElement.hasAttribute
    && document.activeElement.hasAttribute("data-setup-card-confirm-no")));
  if (!onNo) fail(`[${L}/${step}/cancel] could not reach the cancel control`);
  await page.keyboard.press("Enter");
  await page.waitForFunction(() => readCardState("setup/facilities").state !== "confirm",
    null, { timeout: 15000 })
    .catch(() => fail(`[${L}/${step}/cancel] the confirmation never closed`));
  await quiesce(page, `${L}/${step}/cancel`);
  await assertSaidExactly(page, REOPEN_CANCELLED, false, L, `${step}/cancel`);
  await assertFocusExactly(page, '[data-setup-card-ask="facilities"]',
    L, `${step}/cancel`, "the control that opened the reopen confirmation");
  card = await readCard(page, "facilities");
  if (JSON.stringify(card.actionButtons) !== JSON.stringify([REOPEN_LABEL])) {
    fail(`[${L}/${step}/cancel] cancelling did not restore the landing's own `
      + `control: ${JSON.stringify(card.actionButtons)}`);
  }

  // ------------- 8d: the three landings with NO confirmation --------------
  // Asserted under the ARCHIVED Season, where two of their siblings DO carry
  // one — so "no confirmation control" is a fact about these workflows rather
  // than about the fixture.
  for (const w of WORKFLOWS.filter((x) => !x.confirmable)) {
    const s = `8d/${w.key}`;
    await openLanding(page, w.key, `${L}/${s}`);
    const c = await readCard(page, w.key);
    if (!c.hasActionsBox || !c.hasSlot) {
      fail(`[${L}/${s}] the landing did not paint, so "no confirmation control" `
        + `would be satisfied by absence`);
    }
    if ((c.askControls || []).length !== 0) {
      fail(`[${L}/${s}] "${w.title}" offers a confirmation control `
        + `${JSON.stringify(c.askControls)} — no action this workflow can offer `
        + `declares or derives one, so this state is supposed to be `
        + `unreachable`);
    }
    if ((c.actionButtons || []).length === 0) {
      fail(`[${L}/${s}] "${w.title}" offers no controls at all, so the absence `
        + `of a confirmation control says nothing about confirmations`);
    }
  }

  // ------------------ 8c: the one COMPLETION, on participation ------------
  step = "8c/participation-reopen";
  await openLanding(page, "participation", `${L}/${step}`);
  card = await readCard(page, "participation");
  if (card.effective !== REOPEN_LABEL) {
    fail(`[${L}/${step}] participation does not offer the reopen path `
      + `(effective ${JSON.stringify(card.effective)})`);
  }
  await armAnnouncements(page);
  await resetAnnouncements(page);
  await tabToAttribute(page, "data-setup-card-ask", "participation", L, step,
    "the reopen confirmation control",
    '[data-setup-workflow-landing="participation"] .swf-back');
  await page.keyboard.press("Enter");
  await page.waitForFunction(() => readCardState("setup/participation").state === "confirm",
    null, { timeout: 15000 })
    .catch(() => fail(`[${L}/${step}] the reopen confirmation never opened`));
  await assertFocusExactly(page, '[data-setup-card-confirm-reason="participation"]',
    L, `${step}/open`, "the required-reason field");
  // Typed through the keyboard, into the field focus was landed on.
  await page.keyboard.type("state matrix completion");
  await page.keyboard.press("Tab");
  const onYes = await page.evaluate(() => !!(document.activeElement
    && document.activeElement.hasAttribute
    && document.activeElement.hasAttribute("data-setup-card-confirm-yes")));
  if (!onYes) {
    fail(`[${L}/${step}] TAB from the reason field did not reach the `
      + `affirmative control; focus is on ${await describeFocus(page)}`);
  }
  await resetAnnouncements(page);
  await page.keyboard.press("Enter");
  await page.waitForFunction(() => {
    const s = readCardState("setup/participation").state;
    return s !== "confirm" && s !== "pending" && s !== "loading";
  }, null, { timeout: 40000 })
    .catch(() => fail(`[${L}/${step}] the reopen never completed`));
  await quiesce(page, `${L}/${step}/done`);
  // Exactly ONE completion sentence, said AFTER the response — the progress
  // sentence is allowed before it, the generic refresh sentence is not.
  const said = await spoken(page);
  assertNoRepeatedSpeech(said, L, `${step}/done`);
  const done = said.filter((a) => a.text === REOPEN_DONE);
  const generic = said.filter((a) => a.text === "Season participation and divisions updated.");
  if (done.length !== 1 || generic.length !== 0) {
    fail(`[${L}/${step}] the completion announcement must be exactly one `
      + `${JSON.stringify(REOPEN_DONE)} and never the card refresh's own `
      + `generic sentence; the live region carried ${JSON.stringify(said)}`);
  }
  const order = said.map((a) => a.text);
  if (order.indexOf(REOPEN_BUSY) !== -1
      && order.indexOf(REOPEN_BUSY) > order.indexOf(REOPEN_DONE)) {
    fail(`[${L}/${step}] the progress sentence was said AFTER the completion `
      + `sentence: ${JSON.stringify(order)}`);
  }
  await assertFocusExactly(page,
    '[data-setup-workflow-landing="participation"] .swf-landing-title',
    L, `${step}/done`, "the landing's own heading after completion");
  card = await readCard(page, "participation");
  if (card.effective !== "Register Team" || card.blockedBecause !== null) {
    fail(`[${L}/${step}] after the reopen completed the card still reads `
      + `effective ${JSON.stringify(card.effective)} blocked `
      + `${JSON.stringify(card.blockedBecause)} — the completed write must have `
      + `advanced the card to the next valid action`);
  }
}

// ============================== THE FIXTURES ==============================
// P1  a fully provisioned Program whose five required workflows all report
//     `done`: a Season with a League, a Team registered into it, a Player, and
//     a Venue/Rink with an active grant plus one available GAME ice slot.
// P2  a bare Program with two Seasons and nothing else, so every required
//     workflow settles into a DIFFERENT state with a DIFFERENT action.
// P3  a Program whose selected Season is ARCHIVED but whose Venue/Rink/League
//     floors are met, so `season_active` is the FIRST unmet floor and the
//     derived reopen is the action on offer. It is a separate Program on
//     purpose: an archived Season inside P1 would make P1's own
//     `league_season` workflow report `todo` (every Season must carry a
//     League), and the completion leg would stop being about completion.
async function seedFixtures(page, L) {
  const fx = await page.evaluate(async () => {
    const F = window.hsFixture;
    const org = await F.create("org", "/api/v2/setup/organization", { name: "SM Org" });

    const p1 = await F.create("p1", "/api/v2/setup/program", { name: "SM Program One", country: "US" });
    // #409 EXPLICIT SELECTION, boundary 1: the Season create is PROGRAM-AXIS,
    // so the Program-only choice has to be PERSISTED BEFORE it — not after,
    // where the raw context POST used to sit.
    await F.selectProgram("P1 Program-only bootstrap", p1.id);
    const s1 = await F.create("s1", "/api/v2/setup/season", { program_id: p1.id, name: "SM Season One" });
    // BOUNDARY 2. Everything after this is created from INSIDE the target
    // Program's own context: the v2 setup writes are judged against the
    // caller's ACTIVE Program (#367) and, for the SEASON-OWNED ones (League,
    // Division, registration, venue-access grant), against the saved Season
    // too (#409).
    await F.selectProgramSeason("P1 + Season One", p1.id, s1.id);
    const l1 = await F.create("l1", "/api/v2/setup/league", { season_id: s1.id, name: "SM League One" });
    const t1 = await F.create("t1", "/api/v2/setup/team", { league_id: l1.id, name: "SM Team One" });
    await F.call("division", "/api/v2/setup/division",
      { league_id: l1.id, season_id: s1.id, name: "SM Division One" });
    await F.call("team-registrations", `/api/v2/setup/seasons/${s1.id}/team-registrations`,
      { team_id: t1.id, league_id: l1.id, division_id: null });
    await F.call("player", "/api/v2/setup/player",
      { team_id: t1.id, name: "SM Player One", position: "forward" });
    const v1 = await F.create("v1", "/api/v2/setup/venue",
      { name: "SM Venue One", organization_id: org.id });
    const r1 = await F.create("r1", "/api/v2/setup/rink", { venue_id: v1.id, name: "SM Rink One" });
    await F.call("venue-access", `/api/v2/setup/seasons/${s1.id}/venue-access`, { venue_id: v1.id });
    const slot = await F.create("slot", "/api/v2/setup/ice-slot", { rink_id: r1.id,
      start_time: "2026-09-01T18:30:00+00:00",
      end_time: "2026-09-01T20:00:00+00:00", slot_type: "game" });

    const p2 = await F.create("p2", "/api/v2/setup/program", { name: "SM Program Two", country: "US" });
    // P2's Season is PROGRAM-AXIS against P2, so the saved Program moves to
    // P2 first. P2 is deliberately left otherwise empty.
    await F.selectProgram("P2 Program-only bootstrap", p2.id);
    const s2 = await F.create("s2", "/api/v2/setup/season", { program_id: p2.id, name: "SM Season Two" });

    const p3 = await F.create("p3", "/api/v2/setup/program", { name: "SM Program Three", country: "US" });
    await F.selectProgram("P3 Program-only bootstrap", p3.id);
    const s3 = await F.create("s3", "/api/v2/setup/season", { program_id: p3.id, name: "SM Season Three" });
    await F.selectProgramSeason("P3 + Season Three", p3.id, s3.id);
    const l3 = await F.create("l3", "/api/v2/setup/league", { season_id: s3.id, name: "SM League Three" });
    await F.call("team", "/api/v2/setup/team", { league_id: l3.id, name: "SM Team Three" });
    await F.call("division", "/api/v2/setup/division",
      { league_id: l3.id, season_id: s3.id, name: "SM Division Three" });
    const v3 = await F.create("v3", "/api/v2/setup/venue",
      { name: "SM Venue Three", organization_id: org.id });
    await F.call("rink", "/api/v2/setup/rink", { venue_id: v3.id, name: "SM Rink Three" });
    await F.call("venue-access", `/api/v2/setup/seasons/${s3.id}/venue-access`, { venue_id: v3.id });
    const archived = await F.create("archived", `/api/v2/setup/seasons/${s3.id}/archive`,
      { reason: "setup state matrix fixture" });

    // Leave the fixture where the matrix below expects to start.
    await F.selectProgramSeason("back to P1 + Season One", p1.id, s1.id);
    return { org: org.id, p1: p1.id, s1: s1.id, l1: l1.id, v1: v1.id, r1: r1.id,
             slot: slot && slot.id, p2: p2.id, s2: s2.id,
             p3: p3.id, s3: s3.id, archived: !!archived && !archived.error };
  });
  for (const k of ["org", "p1", "s1", "l1", "v1", "r1", "slot", "p2", "s2",
                   "p3", "s3"]) {
    if (!fx[k]) fail(`[${L}] fixture failed to create ${k}: ${JSON.stringify(fx)}`);
  }
  if (!fx.archived) fail(`[${L}] the P3 Season was not archived, so the reopen `
    + `confirmation is unreachable: ${JSON.stringify(fx)}`);
  if (fx.p1 === fx.p2 || fx.p1 === fx.p3) {
    fail(`[${L}] the fixtures share a Program, so a switch would move only the `
      + `Season axis and every tuple comparison would be half-blind`);
  }
  return fx;
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
  const L = viewport.label;
  page.on("pageerror", (e) => errors.push(`[pageerror] ${e.message}`));

  // ============ EVERY FAILED DELIVERY, NAMED (inherited) ================
  // Four independent records, reconciled ONCE at the end of the viewport:
  //   nonOk          every non-2xx response, as { method, url, status }.
  //   failedReqs     requests that never got a response at all.
  //   consoleErrors  Chromium's own lines, each with m.location().url.
  //   injected       one entry per failure THIS FILE forces, stamped with the
  //                  exact method + URL + status of the request being
  //                  fulfilled. Not a counter: an allowance for a 500 on the
  //                  setup overview can only ever be satisfied by that method,
  //                  that URL and that status, and only once.
  const nonOk = [];
  const failedReqs = [];
  const consoleErrors = [];
  const injected = [];
  const FORCED = [OVERVIEW_RE, PROGRESS_RE, PLAYERS_RE];
  const allowInjected = (route, status) => {
    const req = route.request();
    const url = req.url();
    const p = new URL(url).pathname;
    if (!FORCED.some((re) => re.test(p))) {
      // Pushed rather than thrown: a throw inside a route handler is swallowed
      // by Playwright and would silently disarm this guard.
      errors.push(`[injected] a deliberate ${status} was declared for `
        + `${req.method()} ${url}, which is not one of this journey's forced `
        + `read endpoints`);
      return;
    }
    injected.push({ method: req.method(), url: url, status: status, matched: false });
  };
  page.on("console", (m) => {
    if (m.type() !== "error") return;
    const loc = m.location() || {};
    if (/Failed to load resource/.test(m.text())) {
      consoleErrors.push({ text: m.text(), url: loc.url || "" });
      return;
    }
    errors.push(`[console] ${m.text()}${loc.url ? ` @ ${loc.url}` : ""}`);
  });
  page.on("requestfailed", (req) => {
    const f = req.failure();
    failedReqs.push({ method: req.method(), url: req.url(),
                      failure: (f && f.errorText) || "unknown" });
  });

  // ---- in-flight accounting, so nothing is ever sampled under a render.
  let inFlight = 0;
  let requestSeq = 0;
  page.on("request", () => { inFlight += 1; requestSeq += 1; });
  page.on("requestfinished", () => { inFlight = Math.max(0, inFlight - 1); });
  page.on("requestfailed", () => { inFlight = Math.max(0, inFlight - 1); });
  page.__smInFlight = () => inFlight;
  page.__smRequestSeq = () => requestSeq;

  // ---- the three forced-transport channels.
  const ch = {
    overview: makeChannel("overview", OVERVIEW_RE),
    progress: makeChannel("progress", PROGRESS_RE),
    players: makeChannel("players", PLAYERS_RE),
  };
  page.on("response", (r) => {
    const s = r.status();
    if (s < 200 || s > 299) {
      nonOk.push({ method: r.request().method(), url: r.url(), status: s });
    }
  });
  for (const c of Object.values(ch)) {
    await page.route(c.re, async (route) => {
      if (c.mode === "fail") {
        allowInjected(route, 500);
        try {
          await route.fulfill({ status: 500, contentType: "application/json",
            body: JSON.stringify({ error: { code: "server_unavailable",
              message: "The server is temporarily unavailable (500). Please try "
                + "again in a moment." } }) });
        } catch (e) { /* page closed */ }
        return;
      }
      if (c.mode === "hold") {
        let resp;
        try { resp = await route.fetch(); } catch (e) { return; }
        c.heldNow += 1;
        c.markHeld();
        await c.gate;
        c.heldNow -= 1;
        try { await route.fulfill({ response: resp }); } catch (e) { /* closed */ }
        c.released += 1;
        return;
      }
      try { await route.continue(); } catch (e) { /* page closed */ }
    });
  }
  optionsDelayMs = 0;
  await installContextOptionsControl(page);

  try {
    await waitForServer(`${base}/api/health`, READY_TIMEOUT_MS);
    await page.goto(base, { waitUntil: "domcontentloaded" });
    await page.waitForSelector("#content > *", { timeout: 15000 });
    await installContextFixture(page);
    await loginAs(page, "admin", "demo");
    await page.reload({ waitUntil: "domcontentloaded" });
    await page.waitForSelector("#content > *", { timeout: 15000 });
    await installContextFixture(page);

    // ---- (1) EMPTY, on the pristine zero-Program installation. This has to
    //      run BEFORE anything is created; it is the one state that is only
    //      reachable on an installation that has never been provisioned.
    trace(`[${L}] 1: EMPTY on a pristine zero-Program installation`);
    await legEmptyPristine(page, L);

    // ---- fixtures, then everything that needs data.
    trace(`[${L}] fixtures`);
    const fx = await seedFixtures(page, L);
    // #409: the fixture's last act is an explicit context selection, and that
    // selection runs through `setActiveContext` — the app's own switch
    // pipeline — so it legitimately starts a render. Let that render finish
    // before reloading: this journey's whole contract is that nothing is ever
    // sampled under one, and a reload issued mid-render would abort its reads
    // into `requestfailed` entries the next leg would have to explain. The
    // raw `POST /api/context` this replaced never re-rendered at all, which
    // is precisely why the client could end up believing a stale tuple.
    await quiesce(page, `${L}/fixtures`);
    await page.reload({ waitUntil: "domcontentloaded" });
    await page.waitForSelector("#content > *", { timeout: 15000 });
    await installContextFixture(page);

    trace(`[${L}] 2: SUCCESS / complete`);
    await legSuccessComplete(page, L, fx);
    trace(`[${L}] 3: Workflow 6's data-dependent states are unreachable`);
    await legOptionalCannotFail(page, L, ch);
    trace(`[${L}] 4: per-card failure beside successful neighbours`);
    await legHubNeighbourIsolation(page, L, ch);
    trace(`[${L}] 5: ERROR + LOADING + keyboard retry, per landing`);
    await legErrorAndLoadingPerLanding(page, L, ch);
    trace(`[${L}] 6: delayed stale success after a newer failure`);
    await legRaceAfterNewerFailure(page, L, ch);
    trace(`[${L}] 7: STALE + delayed stale success after a context switch`);
    await legStaleAndContextRace(page, L, ch, fx);
    trace(`[${L}] 8a: Workflow 6's confirmation`);
    await legConfirmImport(page, L);
    trace(`[${L}] 8b-d: the derived reopen confirmation`);
    await legConfirmReopen(page, L, fx);

    // Nothing may still be held when the reconciliation runs: an outstanding
    // hold is a request with no response, which rule (6) would report — and
    // which would be a real finding about this journey rather than about the
    // product.
    for (const c of Object.values(ch)) {
      if (c.heldNow !== 0) {
        errors.push(`[harness] the ${c.name} channel still holds ${c.heldNow} `
          + `request(s) at the end of the viewport`);
      }
    }

    errors.push(...reconcileDeliveries(nonOk, injected, consoleErrors, failedReqs));
    if (errors.length) fail(`[${L}] browser errors:\n${errors.join("\n")}`);

    console.log(`[${L}] Setup state matrix: on a pristine zero-Program `
      + `installation all five required workflow landings render EMPTY with a `
      + `sentence that names their own missing records AND the prerequisite `
      + `that blocks them, and expose EXACTLY ONE authorized action — beside `
      + `the same five landings offering two to four once nothing is blocked, `
      + `so "exactly one" is a restriction rather than a property of the `
      + `surface. On a fully provisioned Program all five report the complete `
      + `state and the hub says "5 of 5 required setup workflows you manage `
      + `are done". With the setup overview, the player list and the progress `
      + `read ALL forced to 500 at once, every required card errors and `
      + `Workflow 6 does not — it stays READY, optional, visible and `
      + `reachable, which is what makes its EMPTY and ERROR states `
      + `unreachable rather than untested. On the hub grid a per-card retry `
      + `that SUCCEEDS and one that FAILS each leave every neighbour's `
      + `generation, committed model and painted body byte-identical, and a `
      + `failed retry followed by a successful one is scoped to its own card. `
      + `On every landing the ERROR body names which read failed, keeps the `
      + `actions its own copy promises, and offers a Retry that is reached by `
      + `TAB from the landing's back control and activated with ENTER — `
      + `holding that read open shows a labelled skeleton, aria-busy="true", `
      + `zero controls on a container asserted PRESENT and a silent live `
      + `region, and releasing it announces exactly "<workflow> updated." `
      + `once and lands focus exactly on the landing's own heading. A delayed `
      + `SUCCESS released after an ordinary same-tuple render has committed a `
      + `newer FAILURE changes nothing by snapshot, restores neither the `
      + `counts nor the obsolete primary action, and says nothing — with the `
      + `identical held response shown first, uninterrupted, to settle the `
      + `card. A real #ctx-select switch with the progress read held open `
      + `exposes the STALE window with contextSwitchIntentPending ALREADY `
      + `false: retained counts, the "earlier data" label, exactly one `
      + `Refresh, and every landing action group withdrawn; and a response `
      + `belonging to the Program/Season the operator left changes nothing on `
      + `the tuple they returned to. Workflow 6's committed model is proven `
      + `identical across tuples apart from its identity. Both `
      + `confirmations are driven entirely by keyboard: Workflow 6's wizard `
      + `prompt (focus on the affirmative control, cancel returning focus to `
      + `the control that opened it, completion announcing once — on a raw, `
      + `never-deduplicated ledger, so once means once and not twice — and `
      + `STILL STANDING in the visible live region a task and two paints after `
      + `the navigation it triggered, with focus landing on the destination's `
      + `own heading) and the derived reopen under an `
      + `archived Season (focus on the required-reason field, a blank reason `
      + `refused with focus back on the field, cancel restoring the control, `
      + `and one completion announcing exactly "Season reopened — it can be `
      + `changed again." and landing on the landing's own heading), while the `
      + `three workflows that carry no confirmation are asserted to offer none `
      + `under the same archived Season. Every failed delivery in the run is `
      + `reconciled by exact method, URL and status against the deliberate `
      + `500s this file injected.`);
  } catch (e) {
    if (serverOutput.trim()) {
      console.error("--- demo server output ---\n" + serverOutput.trim());
    }
    throw e;
  } finally {
    // Release anything still parked, or context.close() turns it into a
    // requestfailed the next run would have to explain.
    for (const c of Object.values(ch)) { c.mode = "pass"; c.release(); }
    await context.close();
    await stopServer(server);
  }
}

(async () => {
  // Before anything else: the allowance is not a counter and cannot be spent
  // on somebody else's failure. Deterministic, browser-free, and it runs on
  // every invocation.
  selfTestDeliveryReconciler();
  const browser = await chromium.launch();
  try {
    for (const viewport of VIEWPORTS) await checkViewport(browser, viewport);
    console.log("Setup per-card state matrix browser journey passed.");
  } catch (e) {
    console.error("Setup per-card state matrix browser journey FAILED.");
    console.error(e.message);
    process.exitCode = 1;
  } finally {
    await browser.close();
  }
})();
