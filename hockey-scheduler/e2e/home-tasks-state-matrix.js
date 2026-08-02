// The HOME/TASKS half of the #365 seven-surface browser state matrix, plus the
// ROLE / AUTHORIZATION dimension across the state model.
//
// SCOPE, and what it deliberately is NOT
// -------------------------------------
// #365 asks for a per-card state matrix across seven surfaces: the Home/Tasks
// card and the six Setup landings. This file owns ONE of those seven — the
// Home/Tasks setup-progress card (app.js HOME_TASKS_CARD = "home/setup-progress",
// painted into #sp-card-slot) — and the role/authorization axis that cuts
// across all of them. The six Setup landings are a separate journey.
//
// It is independent of home-tasks-hub.js (#204/#330/#331), which is a
// FEATURE journey: what the card recommends, which real entry point each CTA
// opens, and the Arena-Manager redaction of `workflows`/`next`. This file is a
// STATE journey: the card's own state machine, driven through real production
// entry points with FORCED TRANSPORT OUTCOMES, plus keyboard activation and
// the exact focus target after each user-initiated settle. Neither file is
// modified by the other's existence and no assertion here duplicates one there.
//
// THE CARD'S APPLICABLE STATE SET, and why it is six and not eight
// ---------------------------------------------------------------
// app.js CARD_STATE declares eight states. renderSetupProgressCard() handles
// six of them, and the missing two are not an omission:
//
//   LOADING   skeleton + <h3>Setup progress</h3> + a visually-hidden label,
//             inside an aria-busy="true" #sp-card-slot.                (leg 1a)
//   READY     <h3>Continue setup</h3>, the next workflow, the full workflow
//             list with per-row Done/To do/Optional status text, the
//             completion line, and AT MOST ONE primary action.    (legs 1b/2a)
//   EMPTY     its OWN heading, status sentence and reason-specific
//             explanation, one body per separately-named reason
//             ("no_program" — with the single genuinely authorized path that
//             resolves it; "nothing_actionable" — guidance and NO control).
//                                                               (legs 1h/2b')
//   STALE     <h3>Setup progress — showing earlier data</h3>, the retained
//             rows and counts, and the obsolete primary action WITHDRAWN in
//             favour of a ghost "Refresh setup progress".              (leg 1f)
//   ERROR     <h3>Setup progress unavailable</h3> + an assertive banner +
//             a scoped "Retry".                                   (legs 1c-1e)
//   SUCCESS   <h3>✓ All setup steps complete</h3> + "Go to Schedule".   (leg 1g)
//
//   CONFIRM and PENDING are STRUCTURALLY UNREACHABLE for this card, and leg 1i
//   proves that rather than asserting it in a comment: this card is a pure
//   READ. It starts no write, so it never registers one in the `cardWrites`
//   ledger, and the two states that describe an in-flight or awaiting-operator
//   MUTATION have nothing to describe. Leg 1i therefore records EVERY request
//   the page makes to /api/v2/setup/progress across the whole run and requires
//   all of them to be GETs, requires `cardWrites` never to gain this card's id,
//   and requires no confirmation/pending markup ever to appear in the slot.
//   Claiming "the confirmation state passes" for a card that cannot have one
//   would be the vacuous assertion this branch has already been burned by
//   three times.
//
// ANNOUNCEMENTS: WHY #toast-root MUST STAY SILENT HERE
// ---------------------------------------------------
// Every Setup card announces through announceCardStatus(), which writes the
// ONE sitewide live region (#toast-root). The Home/Tasks card deliberately does
// NOT: #sp-card-slot is itself `role="status" aria-live="polite"`, so settling
// it IS the announcement, and toasting as well would be the same sentence
// spoken twice (app.js loadSetupProgressCard, verbatim: "Deliberately NOT also
// toasted: that would be the same sentence announced twice.").
//
// So this file does not count toasts and hope. It arms ONE document-wide
// observer that attributes every mutation to its NEAREST live-region ancestor
// — which is exactly the ARIA rule for which region owns a change — and keeps
// an ordered ledger of {region, role, aria-live, text}. Against that ledger:
//
//   * every Home/Tasks transition must produce at least one write to
//     #sp-card-slot (the card really does speak), and
//   * ZERO writes to #toast-root (it must not speak twice), and
//   * no two consecutive CONTENT writes to the region carrying byte-identical
//     text — retained raw, never deduplicated by the recorder, and FAILED
//     rather than reported (#365 review: the recorder used to collapse
//     consecutive identical entries, so a duplicated repaint was counted once
//     by construction), and
//   * the status sentence of the settled state must appear EXACTLY ONCE in the
//     whole document, and
//   * nested live regions inside the slot (the STALE banner's role="status",
//     the ERROR banner's role="alert") must carry text DISJOINT from the
//     heading the outer region owns — a nested region repeating its parent's
//     sentence is duplicate speech even though it is one DOM write.
//
// THE LEDGER IS PROVEN LIVE BEFORE IT IS TRUSTED (leg 2a). "#toast-root never
// spoke" is worthless from an observer that cannot see #toast-root. So the
// SAME observer, on the SAME page, is required to record a real production
// toast: the card's own "Add Season" CTA is activated BY KEYBOARD, the real
// season drawer it opens is filled and submitted, and the ledger must contain
// "Season created." attributed to #toast-root. That one leg is simultaneously
// the anti-vacuity control for the authorized primary action (it EXISTS, it is
// ENABLED, and it really works end to end) and for the announcement ledger.
//
// THE ROLE / AUTHORIZATION DIMENSION (leg 2)
// -----------------------------------------
// Three roles, on the SAME Program fixture, so every "absent" is a real
// difference and never a missing container:
//
//   League Admin (MANAGE_SETUP + MANAGE_ARENA) — the no-data path: every
//     required workflow "To do" with its own explanatory detail, and EXACTLY
//     ONE enabled primary action, "Add Season". Proven by using it.
//   Arena Manager (MANAGE_ARENA only) — authorized on the very same tuple,
//     and NOT authorized for the MANAGE_SETUP work that Program actually
//     needs. Its card is present and painted, its one visible workflow is
//     redacted to "Venues, rinks and ice", and it is BLOCKED on a Season it
//     cannot create: explicit guidance ("Create or select a Season before
//     adding ice.") and ZERO controls — while the slot, the card and the
//     heading are all structurally there.
//   Viewer (VIEW only) — no Home/Tasks card at all, and no Setup surface it
//     can navigate to. Both negatives are asserted against the IDENTICAL
//     selector battery that returns a NON-EMPTY, ENABLED set for League Admin
//     in the same fixture, and the server's own refusal (403, requires
//     manage_arena) is provoked and recorded, so "the UI hides it" is not
//     mistaken for "the data is protected".
//
// STALE RESPONSES (leg 3), the #365 clause this card is most exposed to
// --------------------------------------------------------------------
//   3a a delayed SUCCESS released after a NEWER FAILURE has already settled;
//   3b a delayed SUCCESS released after a full CONTEXT SWITCH has settled;
//   3c a delayed FAILURE released after a newer SUCCESS has already settled.
// In all three the older response must change NOTHING — asserted by SNAPSHOT
// comparison field by field, never by re-asserting expected values — must not
// restore the obsolete primary action, and must not announce. Each release is
// observed AT THE NETWORK before the "after" snapshot is taken, so "nothing
// changed" is never the accident of a response that never arrived, and each
// held response's real upstream status and payload are recorded so the thing
// discarded is provably one that WOULD have changed the card.
//
// TECHNIQUE, following the standards this branch already established
// -----------------------------------------------------------------
//   * Delayed responses capture the REAL response first (`await route.fetch()`),
//     hold, then fulfil it. Delaying the REQUEST proves nothing — the server
//     would never have answered.
//   * Every non-2xx delivery is recorded with method + absolute URL + status.
//     Each deliberate failure (injected by a route handler, or provoked from
//     the server) is DECLARED with that same triple and consumed at most once,
//     reconciled at end-of-viewport rather than live. Unmatched responses,
//     requestfailed events, unexplained "Failed to load resource" console lines
//     and UNDELIVERED declarations all fail the run. There is no fungible
//     "ignore the next console error" counter, and a browser-free self-test
//     proves the non-fungibility on every invocation.
//   * Context moves go through the REAL #ctx-select and wait for the CONFIRMED
//     tuple (program AND season), contextSwitchIntentPending === false, and a
//     repaint of #content's own children (`subtree:false` — a subtree observer
//     is satisfied by the retain-the-cards pass and returns too early).
//   * Anti-vacuity is asserted rather than assumed: containers are proven
//     PRESENT before they are asserted EMPTY, the positive case is proven in
//     the same fixture before the negative is asserted, and every window this
//     file samples inside is re-asserted as still open AFTER the sample.
//
// WHAT THIS FILE DOES NOT CLAIM. No screen reader was run and no human
// session was moderated. "Announcement" here means a write to a live region,
// observed in the DOM. "Keyboard activation" means a real bounded Tab/Enter
// traversal in Chromium, from a defined starting point, with :focus-visible
// and a browser-computed focus indicator asserted before the key press.
//
// Fails on any browser console/page error.
const { chromium } = require("playwright");
const { spawn } = require("child_process");
const http = require("http");
const path = require("path");

const HOST = "127.0.0.1";
const BACKEND_DIR = path.resolve(__dirname, "..", "backend");
const READY_TIMEOUT_MS = 15000;
// Ports well clear of every other journey in this directory (which occupy
// 8225-8396 plus 8441/8442, 8541/8542, 8641/8642), so this journey can run
// beside any of them.
const VIEWPORTS = [
  { label: "desktop", width: 1440, height: 900, port: 8971 },
  { label: "phone", width: 390, height: 844, port: 8972 },
];

const PROGRESS_RE = /\/api\/v2\/setup\/progress(\?|$)/;
const CONTEXT_OPTIONS_RE = /\/api\/context\/options(\?|$)/;

// The card's own declared copy (app.js renderSetupProgressCard). Asserted as
// EXACT strings, never as regexes: "stable semantic headings and status text"
// is a claim about specific sentences, and a loose match would let a reworded
// or wrong-state heading satisfy it.
const H_LOADING = "Setup progress";
const H_READY = "Continue setup";
const H_ERROR = "Setup progress unavailable";
const H_STALE = "Setup progress — showing earlier data";
const H_SUCCESS = "✓ All setup steps complete";
// EMPTY renders a body per NAMED REASON (#365 review: both reasons used to
// return the empty string, which left a no-Program operator and a role with
// nothing actionable with no explanation at all, and left a keyboard operator
// whose Retry resolved here with no perceivable place to land). The two
// reasons are DIFFERENT claims and are asserted as different copy, so a build
// that collapsed them back into one sentence fails.
const H_EMPTY_NO_PROGRAM = "Setup progress — no program yet";
const H_EMPTY_NOTHING = "Setup progress — nothing for your role to do";
const EMPTY_NO_PROGRAM_STATUS =
  "No program has been set up yet, so there is no setup progress to show.";
const EMPTY_NO_PROGRAM_PATH =
  "The guided Initial Setup wizard creates the first one — this card fills in "
  + "as each setup workflow is done.";
const EMPTY_NOTHING_STATUS =
  "There is nothing left for your role to do in this program's setup right now.";
const EMPTY_NOTHING_EXPLAIN =
  "Setup workflows your role doesn't manage aren't shown on this card, so this "
  + "isn't a claim that the whole program is finished.";
const CTA_START_ONBOARDING = "Start Initial Setup";
const ERROR_SENTENCE = "Could not load your setup progress.";
const LOADING_SR = "Loading setup progress…";
const CTA_ADD_SEASON = "Add Season";
const CTA_RETRY = "Retry";
const CTA_REFRESH = "Refresh setup progress";
const CTA_SCHEDULE = "Go to Schedule";
// The backend's own guidance for a MANAGE_ARENA holder blocked on a Season
// only MANAGE_SETUP can create (api/service.py _workflow_prerequisite_gap).
const ARENA_BLOCKED_DETAIL = "Create or select a Season before adding ice.";
const ARENA_WORKFLOW_LABEL = "Venues, rinks and ice";
// The Setup hub's explicit guidance for a role that manages no workflow at
// all — the "explicit guidance instead of a mutation control" half of #365's
// unauthorized-role requirement.
const VIEWER_SETUP_GUIDANCE = "Your role doesn't manage any setup workflows.";
// The real production toast the CTA control leg provokes (app.js drawer save).
const SEASON_CREATED_TOAST = "Season created.";

// The 503 body every injected progress failure answers with.
const FORCED_503_BODY = JSON.stringify({
  error: { code: "unavailable", message: "Forced by home-tasks-state-matrix." },
});

// Every mutation entry point either surface can offer. Used as ONE battery in
// both directions: it must return a non-empty ENABLED set for League Admin and
// an empty set for Viewer, on the same page and the same fixture.
const MUTATION_CONTROLS = [
  "#sp-card-slot [data-setup-progress-action]",
  "#sp-card-slot [data-setup-progress-retry]",
  "[data-setup-workflow-go]",
  "[data-setup-landing-actions] button",
  "[data-setup-card-ask]",
  "[data-setup-card-confirm-yes]",
  "[data-setup-card-confirm-no]",
  "[data-setup-card-retry]",
].join(",");

function fail(msg) { throw new Error(msg); }

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
  return page.evaluate(async ([p, b]) => {
    const r = await fetch(p, {
      method: "POST", credentials: "same-origin",
      headers: { "Content-Type": "application/json" }, body: JSON.stringify(b || {}),
    });
    return { status: r.status, body: await r.json() };
  }, [path_, body || {}]);
}

async function loginAs(page, username, password, step) {
  const res = await apiPost(page, "/api/auth/login", { username, password });
  if (res.status !== 200 || (res.body && res.body.error)) {
    fail(`[${step}] login as ${username} failed: ${JSON.stringify(res)}`);
  }
}

// ======== THE DELIVERY RECONCILER, AS A PURE FUNCTION (#365 round 9) =======
// Everything the page's four network/console recorders collected, turned into
// the list of lines that must fail the run. Pure on purpose: it is what makes
// the non-fungibility of a failure allowance testable without a browser, which
// selfTestDeliveryReconciler() below does on every invocation of this journey.
//
// THE RULES, in order:
//   1. A 3xx is recorded but is not a failure.
//   2. A >=400 response is matched against an UNMATCHED DECLARED failure with
//      the IDENTICAL method, the IDENTICAL absolute URL and the IDENTICAL
//      status — whether this file injected it at a route handler or provoked
//      it from the server on purpose. Nothing else can satisfy it.
//   3. Any unmatched >=400 fails the run, NAMED: method, URL, status.
//   4. Any declaration that was never delivered fails the run too: a journey
//      that declares a 503 the page never receives has stopped testing what it
//      says it tests.
//   5. Chromium's "Failed to load resource" console lines are matched by the
//      URL in m.location() against the URLs step 2 accepted, one line per
//      accepted failure. A console line for any other URL fails the run and is
//      reported WITH that URL, which m.text() alone never carried.
//   6. A request that never got a response at all is always a failure.
function reconcileDeliveries(nonOk, declared, consoleErrors, failedReqs) {
  const out = [];
  const accepted = new Map();  // url -> how many console lines it may explain
  for (const rec of nonOk) {
    if (rec.status < 400) continue;                                    // (1)
    const slot = declared.find((d) => !d.matched                       // (2)
      && d.method === rec.method && d.url === rec.url
      && d.status === rec.status);
    if (slot) {
      slot.matched = true;
      accepted.set(rec.url, (accepted.get(rec.url) || 0) + 1);
      continue;
    }
    out.push(`[response] ${rec.method} ${rec.url} -> ${rec.status} — no `      // (3)
      + `deliberate failure was declared for this request, so this is a real `
      + `failed request, not one of this journey's own forced outcomes`);
  }
  for (const d of declared) {                                          // (4)
    if (d.matched) continue;
    out.push(`[declared] a ${d.status} was declared for ${d.method} ${d.url} `
      + `(${d.origin}) but no such response was ever delivered to the page, so `
      + `the leg that declared it proved nothing`);
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
// A fungible "ignore the next console error" counter already absorbed a real
// 404 on this branch, so the property is asserted rather than described.
function selfTestDeliveryReconciler() {
  const PROG = "http://127.0.0.1:8971/api/v2/setup/progress";
  const STRAY = "http://127.0.0.1:8971/api/v2/setup/overview";
  const check = (what, got, want) => {
    if (JSON.stringify(got) !== JSON.stringify(want)) {
      fail(`delivery-reconciler self-test (${what}) — expected `
        + `${JSON.stringify(want)}, got ${JSON.stringify(got)}`);
    }
  };

  // (i) the declared 503 alone: accepted, and its console line with it.
  check("a declared 503 is accepted for its own request", reconcileDeliveries(
    [{ method: "GET", url: PROG, status: 503 }],
    [{ method: "GET", url: PROG, status: 503, matched: false, origin: "leg" }],
    [{ text: "Failed to load resource: the server responded with a status of "
        + "503 (Service Unavailable)", url: PROG }],
    []), []);

  // (ii) the same 503 PLUS an unrelated 404 in the same leg. The 404 is
  //      reported with its exact URL; the 503 is still accepted silently.
  let out = reconcileDeliveries(
    [{ method: "GET", url: PROG, status: 503 },
     { method: "GET", url: STRAY, status: 404 }],
    [{ method: "GET", url: PROG, status: 503, matched: false, origin: "leg" }],
    [{ text: "Failed to load resource: 503", url: PROG },
     { text: "Failed to load resource: 404", url: STRAY }],
    []);
  if (out.length !== 2 || !out.every((l) => l.indexOf(STRAY) !== -1
        && l.indexOf(PROG) === -1)) {
    fail(`delivery-reconciler self-test — an unrelated 404 alongside a declared `
      + `503 must fail the run naming ${STRAY} and nothing else, got `
      + `${JSON.stringify(out)}`);
  }

  // (iii) the 404 ALONE against an outstanding 503 allowance: the allowance is
  //       not spent on it, so BOTH the stray 404 and the undelivered
  //       declaration are reported.
  out = reconcileDeliveries(
    [{ method: "GET", url: STRAY, status: 404 }],
    [{ method: "GET", url: PROG, status: 503, matched: false, origin: "leg" }],
    [{ text: "Failed to load resource: 404", url: STRAY }],
    []);
  if (out.length !== 3
      || !out.some((l) => l.indexOf("[response]") === 0 && l.indexOf(STRAY) !== -1)
      || !out.some((l) => l.indexOf("[declared]") === 0 && l.indexOf(PROG) !== -1)
      || !out.some((l) => l.indexOf("[console]") === 0 && l.indexOf(STRAY) !== -1)) {
    fail(`delivery-reconciler self-test — a 503 allowance must never be spent `
      + `on an unrelated 404, got ${JSON.stringify(out)}`);
  }

  // (iv) SAME URL, DIFFERENT status: still not fungible. This is the exact
  //      shape the Viewer leg creates — a deliberate 403 and a deliberate 503
  //      on the very same /api/v2/setup/progress URL — so it must be pinned.
  out = reconcileDeliveries(
    [{ method: "GET", url: PROG, status: 403 }],
    [{ method: "GET", url: PROG, status: 503, matched: false, origin: "leg" }],
    [], []);
  if (out.length !== 2
      || !out.some((l) => l.indexOf("[response]") === 0 && l.indexOf("403") !== -1)
      || !out.some((l) => l.indexOf("[declared]") === 0 && l.indexOf("503") !== -1)) {
    fail(`delivery-reconciler self-test — a 503 allowance on a URL must not `
      + `cover a 403 on that same URL, got ${JSON.stringify(out)}`);
  }

  // (v) SAME request, SAME status, TWICE: one allowance covers one delivery.
  out = reconcileDeliveries(
    [{ method: "GET", url: PROG, status: 503 },
     { method: "GET", url: PROG, status: 503 }],
    [{ method: "GET", url: PROG, status: 503, matched: false, origin: "leg" }],
    [], []);
  if (out.length !== 1 || out[0].indexOf("[response]") !== 0) {
    fail(`delivery-reconciler self-test — one allowance must be consumed at `
      + `most once, got ${JSON.stringify(out)}`);
  }

  // (vi) a redirect is not a failure.
  check("a 3xx is not a failure",
    reconcileDeliveries([{ method: "GET", url: STRAY, status: 304 }], [], [], []),
    []);
}

// ================== THE PROGRESS TRANSPORT, UNDER TEST CONTROL ==============
// ONE-SHOT directives, deliberately: every leg arms the outcome for THE VERY
// NEXT /api/v2/setup/progress request and then performs the real production
// action that issues it. A standing mode would silently apply to whatever
// render happened to run next, and a leg would then be asserting on a request
// it did not cause.
let progressDirective = null;

// Answer the next progress request with `status` without ever asking the
// server. Nothing this file forces to fail is allowed to reach the backend:
// a card that shows an error state because the server really failed is not the
// same test as one whose transport failed under it.
function failNextProgress(status) {
  progressDirective = { kind: "fail", status: status };
}

// Hold the next progress request. `fail:false` (the default) lets the request
// REALLY reach the server and captures its REAL response first, then holds
// only the DELIVERY — the idiom this branch established, because delaying the
// REQUEST would merely make the server answer later and would prove nothing
// about a response arriving into a context that has moved on. `fail:true`
// holds the request BEFORE the server and answers 503 on release.
function holdNextProgress(opts) {
  let release = () => {};
  const gate = new Promise((r) => { release = r; });
  const d = {
    kind: "hold", fail: !!(opts && opts.fail), gate,
    started: false, fetched: false, upstreamStatus: null, upstreamProgram: null,
    upstreamComplete: null, released: false, settled: false,
  };
  progressDirective = d;
  return { d: d, release: () => { d.released = true; release(); } };
}

async function installProgressControl(page, declareInjected) {
  await page.route(PROGRESS_RE, async (route) => {
    const d = progressDirective;
    progressDirective = null;
    if (!d) { try { await route.continue(); } catch (e) {} return; }
    d.started = true;
    if (d.kind === "fail") {
      declareInjected(route, d.status);
      try {
        await route.fulfill({ status: d.status, contentType: "application/json",
                              body: FORCED_503_BODY });
      } catch (e) { /* page closed */ }
      d.settled = true;
      return;
    }
    if (d.fail) {
      await d.gate;                       // held BEFORE the server, on purpose
      declareInjected(route, 503);
      try {
        await route.fulfill({ status: 503, contentType: "application/json",
                              body: FORCED_503_BODY });
      } catch (e) {}
      d.settled = true;
      return;
    }
    let body = "";
    try {
      const real = await route.fetch();   // the server REALLY answers, first
      d.upstreamStatus = real.status();
      body = await real.text();
      try {
        const parsed = JSON.parse(body);
        d.upstreamProgram = parsed && parsed.program_id;
        d.upstreamComplete = parsed && parsed.complete;
      } catch (e) { /* not JSON — recorded as null */ }
      d.fetched = true;
    } catch (e) {
      d.fetchError = String(e);
    }
    await d.gate;                          // only the DELIVERY is held
    try {
      await route.fulfill({ status: d.upstreamStatus || 200,
                            contentType: "application/json", body: body });
    } catch (e) {}
    d.settled = true;
  });
}

// ============ THE ONE RECONCILIATION ROUND TRIP, UNDER TEST CONTROL ========
// /api/context/options is the round trip a context switch awaits between
// moving `contextOptions.selected` and releasing `contextSwitchIntentPending`.
// Holding it open is what makes the STALE window OBSERVABLE rather than raced:
// it does not change a single response byte, and a build that renders STALE
// correctly is unaffected by it.
let optionsGate = null;
async function installContextOptionsControl(page) {
  await page.route(CONTEXT_OPTIONS_RE, async (route) => {
    const gate = optionsGate;
    if (gate) await gate;
    try { await route.continue(); } catch (e) { /* page closed mid-hold */ }
  });
}
function holdContextOptions() {
  let release = () => {};
  optionsGate = new Promise((resolve) => { release = resolve; });
  return () => { optionsGate = null; release(); };
}

// ============== EVERY LIVE-REGION WRITE, ATTRIBUTED TO ITS OWNER ===========
// One observer for the whole document, attributing each mutation to its
// NEAREST live-region ancestor — the ARIA rule for which region owns a change.
// That is what makes "the card spoke, and the sitewide region did not" a
// measurement rather than an assumption, and what would catch a nested region
// repeating its parent's sentence.
//
// NOTHING IS DEDUPLICATED HERE, and that is the point (#365 review). This
// recorder used to drop an entry whose {region, text, hidden} matched the one
// before it — so a region written twice in a row with byte-identical content
// was collapsed to one write BY CONSTRUCTION, and "the settled sentence was
// said exactly once" could not fail for a build that said it twice. Every raw
// write is now retained, in order, and the duplicate is judged below.
//
// Each entry carries its `kind`, because the two kinds of mutation are not the
// same event and conflating them would manufacture false duplicates rather
// than find real ones:
//   "content"    a childList/characterData change — the region's content was
//                actually rewritten. This is a write, and two consecutive
//                identical ones are duplicate speech.
//   "attribute"  aria-busy/hidden/aria-live/role changed while the content did
//                not. Production correctly writes the settled card and THEN
//                clears aria-busy on the same element (the ARIA convention),
//                which is one announcement, not two — but it carries the same
//                text, so counting it as a write would fail every settle.
// Both are recorded, in order; only content writes are judged.
async function armLiveRegions(page) {
  await page.evaluate(() => {
    window.__live = [];
    const isRegion = (el) => !!(el && el.getAttribute
      && (el.hasAttribute("aria-live") || el.getAttribute("role") === "status"
          || el.getAttribute("role") === "alert"));
    const regionOf = (node) => {
      let el = node && node.nodeType === 1 ? node : (node && node.parentElement);
      while (el) { if (isRegion(el)) return el; el = el.parentElement; }
      return null;
    };
    const record = (node, kind) => {
      const reg = regionOf(node);
      if (!reg) return;
      const id = reg.id || `${reg.tagName.toLowerCase()}[${reg.getAttribute("role")
        || reg.getAttribute("aria-live")}]`;
      const text = (reg.textContent || "").replace(/\s+/g, " ").trim();
      window.__live.push({ region: id, kind: kind, role: reg.getAttribute("role"),
                           live: reg.getAttribute("aria-live"),
                           hidden: !!reg.hidden, text: text.slice(0, 400) });
    };
    if (window.__liveObs) window.__liveObs.disconnect();
    window.__liveObs = new MutationObserver((records) => {
      for (const r of records) {
        if (r.type === "childList" && r.addedNodes.length) {
          r.addedNodes.forEach((n) => record(n, "content"));
        } else {
          record(r.target, r.type === "attributes" ? "attribute" : "content");
        }
      }
    });
    window.__liveObs.observe(document.body, {
      childList: true, subtree: true, characterData: true,
      attributes: true, attributeFilter: ["hidden", "aria-busy", "aria-live", "role"],
    });
  });
}
async function liveWrites(page) {
  return page.evaluate(() => (window.__live || []).slice());
}
// The writes a person would actually hear: content rewrites carrying text,
// in the order they happened, with nothing merged away.
function spokenWrites(writes, region) {
  return writes.filter((w) => w.kind === "content" && w.text
    && (!region || w.region === region));
}

// The one Home/Tasks announcement assertion, applied identically everywhere.
//
// THREE HARD RULES, and one thing this file deliberately only RECORDS.
//
//   (1) NOTHING may be spoken through #toast-root. This is the requirement the
//       card's own code states: #sp-card-slot is already a polite live region,
//       so settling it is what speaks there, and toasting as well would be the
//       same sentence twice. Proven non-vacuous by leg 2a, which requires this
//       same ledger to record a real production toast.
//   (2) The card MUST have written its own region at least once — a silent
//       settle is not "no duplicate speech", it is no speech.
//   (3) The SETTLED state's own sentence must have been written EXACTLY ONCE,
//       and must be the last thing the region said. This is the #365 clause
//       ("without duplicate speech") measured on the sentence that is actually
//       being announced, rather than on every transient paint in between.
//
//   (4) NO TWO CONSECUTIVE CONTENT WRITES to the card's region may carry
//       byte-identical text. This was previously OBSERVED AND NOT JUDGED, and
//       the #365 review was explicit that this is not an open question: a
//       context switch really did paint the card's STALE content twice — once
//       by repaintContextScopedCardsAsStale(), which is deliberately
//       synchronous and must keep running before the awaited reads, and once
//       by render() rebuilding the slot from the same still-stale model — and
//       `#content.innerHTML += renderModal()` re-serialized the whole region a
//       second time on EVERY render. Both are now eliminated in production
//       (the live region is carried through the render instead of rebuilt),
//       and reinstating either one fails here.
function assertCardSpokeOnce(writes, expectText, L, step) {
  const toast = spokenWrites(writes, "toast-root");
  if (toast.length) {
    fail(`[${L}/${step}] the Home/Tasks card spoke through the SITEWIDE live `
      + `region as well as its own — that is the same sentence announced `
      + `twice, which app.js's loadSetupProgressCard refuses on purpose: `
      + `${JSON.stringify(toast)}`);
  }
  const mine = writes.filter((w) => w.region === "sp-card-slot");
  const said = spokenWrites(writes, "sp-card-slot");
  if (!mine.length) {
    fail(`[${L}/${step}] nothing at all was written to #sp-card-slot, so the `
      + `card never announced its new state through the one live region it `
      + `owns; recorded writes: ${JSON.stringify(writes)}`);
  }
  if (!said.length) {
    fail(`[${L}/${step}] #sp-card-slot was touched but never had CONTENT `
      + `written into it, so nothing was announced: ${JSON.stringify(mine)}`);
  }
  const last = said[said.length - 1];
  if (expectText && last.text.indexOf(expectText) === -1) {
    fail(`[${L}/${step}] the last thing #sp-card-slot said does not contain `
      + `"${expectText}": ${JSON.stringify(last)}`);
  }
  if (expectText) {
    const carried = said.filter((w) => w.text.indexOf(expectText) !== -1);
    if (carried.length !== 1) {
      fail(`[${L}/${step}] the settled state's own sentence "${expectText}" was `
        + `written to #sp-card-slot ${carried.length} times; a polite live region `
        + `is handed it once. Writes: ${JSON.stringify(said.map((w) => w.text.slice(0, 90)))}`);
    }
  }
  for (let i = 1; i < said.length; i++) {
    if (said[i].text === said[i - 1].text) {
      fail(`[${L}/${step}] #sp-card-slot received two back-to-back CONTENT `
        + `writes carrying byte-identical text, which is the same sentence `
        + `handed to a polite live region twice: `
        + `"${said[i].text.slice(0, 160)}…"\n  full ordered ledger: `
        + `${JSON.stringify(said.map((w) => w.text.slice(0, 70)))}`);
    }
  }
  return said;
}

// The nested-region half of "no duplicate speech": a role="status"/"alert"
// element INSIDE the slot owns its own text, so if that text is also the
// heading the outer region owns, one DOM write is two announcements.
async function assertNoNestedEcho(page, L, step) {
  const bad = await page.evaluate(() => {
    const slot = document.getElementById("sp-card-slot");
    if (!slot) return null;
    const outer = (slot.textContent || "").replace(/\s+/g, " ").trim();
    const inner = Array.from(slot.querySelectorAll(
      "[aria-live],[role=status],[role=alert]"));
    return inner.map((el) => {
      const t = (el.textContent || "").replace(/\s+/g, " ").trim();
      const rest = outer.split(t).join(" ").replace(/\s+/g, " ").trim();
      return { role: el.getAttribute("role"), live: el.getAttribute("aria-live"),
               text: t, echoedElsewhere: t.length > 0 && rest.indexOf(t) !== -1 };
    }).filter((x) => x.echoedElsewhere);
  });
  if (bad && bad.length) {
    fail(`[${L}/${step}] a nested live region inside #sp-card-slot carries text `
      + `that also appears elsewhere in the card, so the same sentence is `
      + `owned by two regions: ${JSON.stringify(bad)}`);
  }
}

// Exactly once in the WHOLE document — a sentence rendered in two places is
// read twice however the live regions are arranged.
async function assertSentenceAppearsOnce(page, sentence, L, step) {
  const n = await page.evaluate((s) => {
    const t = (document.body.innerText || "").replace(/\s+/g, " ");
    let i = 0, from = 0;
    for (;;) {
      const at = t.indexOf(s, from);
      if (at === -1) break;
      i += 1; from = at + s.length;
    }
    return i;
  }, sentence.replace(/\s+/g, " "));
  if (n !== 1) {
    fail(`[${L}/${step}] the sentence "${sentence}" appears ${n} time(s) in the `
      + `document; exactly one is required (0 = the state did not render its `
      + `status text, 2+ = it is announced twice)`);
  }
}

// Reach the Dashboard the way an operator does. The Initial Setup wizard (#174)
// takes the first paint whenever the ACTIVE Program is not set up yet — which
// is true of this journey's own no-data fixtures — so it is dismissed through
// its OWN control rather than by forcing a view.
async function reachDashboard(page, L, step) {
  await page.waitForFunction(() => !!document.body.dataset.view, null,
    { timeout: 20000 })
    .catch(() => fail(`[${L}/${step}] the shell never resolved a view`));
  if (await page.evaluate(() => document.body.dataset.view) === "onboarding") {
    await page.click('[data-onboarding-goto="dashboard"]');
  }
  await page.waitForFunction(
    () => document.body.dataset.view === "dashboard"
      && !!document.getElementById("sp-card-slot"),
    null, { timeout: 20000 })
    .catch(async () => {
      const why = await page.evaluate(() => ({
        view: document.body.dataset.view,
        slot: !!document.getElementById("sp-card-slot"),
      })).catch(() => null);
      fail(`[${L}/${step}] never reached the Dashboard with its Home/Tasks `
        + `slot: ${JSON.stringify(why)}`);
    });
}

// "#content has children" rather than waitForSelector("#content > *"), which
// asks for the first child to be VISIBLE. #sp-card-slot is legitimately the
// first child in every state, including the ones this journey deliberately
// creates before the card has painted anything into it, so the visibility wait
// times out on states this journey exists to assert.
async function contentPainted(page) {
  await page.waitForFunction(() => {
    const c = document.getElementById("content");
    return !!c && c.children.length > 0;
  }, null, { timeout: 15000 });
}

// A fresh page load. The leftover "#ctx=" deep link has to go first, or
// bootstrap() faithfully POSTs the PREVIOUS selection straight back over it.
async function reenter(page, base) {
  await page.evaluate(() => history.replaceState(
    null, "", location.pathname + location.search)).catch(() => {});
  await page.goto(base, { waitUntil: "domcontentloaded" });
  await contentPainted(page);
  // A page load destroys the observer with the document that carried it. Every
  // "#toast-root never spoke" assertion is about a ledger, so re-arming here
  // rather than at each call site is what stops a leg from measuring nothing.
  await armLiveRegions(page);
}

// Settled == the slot exists, its own aria-busy has been cleared by the card's
// load, and the committed model is no longer LOADING. Excluding LOADING is what
// makes every "settled" wait below honest rather than one that returns instantly
// on a skeleton this journey deliberately created.
async function settled(page, step) {
  await page.waitForFunction(() => {
    const s = document.getElementById("sp-card-slot");
    if (!s || s.getAttribute("aria-busy") !== "false") return false;
    return readCardState("home/setup-progress").state !== "loading";
  }, null, { timeout: 20000 })
    .catch(async () => {
      const why = await page.evaluate(() => {
        const s = document.getElementById("sp-card-slot");
        return { slot: !!s, busy: s && s.getAttribute("aria-busy"),
                 state: (readCardState("home/setup-progress") || {}).state };
      }).catch(() => null);
      fail(`[${step}] the Home/Tasks card never settled: ${JSON.stringify(why)}`);
    });
  await page.waitForTimeout(250);
}

// EVERYTHING about the card, in one comparable record.
async function homeCard(page) {
  return page.evaluate(() => {
    const slot = document.getElementById("sp-card-slot");
    const entry = readCardState("home/setup-progress");
    const a = document.activeElement;
    const describe = (el) => {
      if (!el) return "null";
      return `${el.tagName}[${el.className || ""}]`
        + `[id=${el.id || ""}][tabindex=${el.getAttribute
            && el.getAttribute("tabindex") || ""}]`
        + `{${(el.textContent || "").replace(/\s+/g, " ").trim().slice(0, 60)}}`;
    };
    const toastRoot = document.getElementById("toast-root");
    const toastMsg = toastRoot && toastRoot.querySelector(".toast-msg");
    return {
      view: document.body.dataset.view,
      hasSlot: !!slot,
      slotRole: slot ? slot.getAttribute("role") : null,
      slotLive: slot ? slot.getAttribute("aria-live") : null,
      busy: slot ? slot.getAttribute("aria-busy") : null,
      slotHtml: slot ? slot.innerHTML : null,
      slotText: slot ? (slot.textContent || "").replace(/\s+/g, " ").trim() : null,
      headings: slot ? Array.from(slot.querySelectorAll("h3"))
        .map((h) => h.textContent.trim()) : null,
      buttons: slot ? Array.from(slot.querySelectorAll("button")).map((b) => ({
        text: b.textContent.trim(), cls: b.className, disabled: !!b.disabled,
        action: b.dataset.setupProgressAction || null,
        retry: b.hasAttribute("data-setup-progress-retry"),
        goto: b.dataset.goto || null,
      })) : null,
      primaries: slot ? Array.from(slot.querySelectorAll("button.act.primary"))
        .map((b) => b.textContent.trim()) : null,
      rows: slot ? Array.from(slot.querySelectorAll(".li")).map((li) => ({
        title: ((li.querySelector(".li-title") || {}).textContent || "").trim(),
        status: ((li.querySelector(".badge") || {}).textContent || "").trim(),
        detail: ((li.querySelector(".li-sub") || {}).textContent || "").trim(),
      })) : null,
      progressLine: slot && slot.querySelector(".sp-progress-line")
        ? slot.querySelector(".sp-progress-line").textContent.replace(/\s+/g, " ").trim()
        : null,
      // The EMPTY state's own status sentence, read from its own hook so its
      // presence and its exact wording are measured rather than inferred from
      // the slot's whole text.
      emptyStatus: slot && slot.querySelector(".sp-empty-status")
        ? slot.querySelector(".sp-empty-status").textContent.replace(/\s+/g, " ").trim()
        : null,
      skeleton: !!(slot && slot.querySelector(".skeleton")),
      // The card's own model, beside the DOM painted from it.
      state: entry.state,
      reason: entry.reason || null,
      staleFrom: entry.staleFrom || null,
      generation: entry.identity ? entry.identity.generation : null,
      counter: cardGenerations["home/setup-progress"],
      identityProgram: entry.identity ? entry.identity.program_id : null,
      identitySeason: entry.identity ? entry.identity.season_id : null,
      // The withdrawal is a DIFFERENT reason for a card to have no CTA
      // (renderSetupProgressCard checks it first), so it is read explicitly
      // and never confused with "this role is not authorized".
      intentPending: !!contextSwitchIntentPending,
      selected: JSON.stringify((contextOptions && contextOptions.selected) || null),
      // The ledger. This card must never appear in it — see leg 1i.
      ledgerCards: Object.keys(cardWrites).sort(),
      // Confirmation / pending markup must never exist on this surface.
      confirmMarkup: slot ? slot.querySelectorAll(
        "[data-setup-card-confirm-yes],[data-setup-card-confirm-no],"
        + "[data-setup-card-confirm-reason],[data-setup-card-pending],"
        + "[data-setup-card-ask]").length : null,
      focus: describe(a),
      // "focus landed somewhere" is not the requirement. The target has to be
      // something a person can PERCEIVE (a real box) and something that NAMES
      // where they are (its own text) — the zero-height, text-less
      // #sp-card-slot satisfied neither, which is why focusing it was only
      // half a fix.
      focusName: a ? (a.textContent || "").replace(/\s+/g, " ").trim() : "",
      focusBox: a && a.getBoundingClientRect ? (() => {
        const r = a.getBoundingClientRect();
        return { w: Math.round(r.width), h: Math.round(r.height) };
      })() : null,
      focusIsBody: a === document.body,
      focusInSlot: !!(slot && a && slot.contains(a)),
      focusIsSlot: !!(slot && a === slot),
      focusIsCardHeading: !!(a && a.tagName === "H3" && slot && slot.contains(a)),
      toast: toastRoot && !toastRoot.hidden && toastMsg
        ? toastMsg.textContent.trim() : "",
    };
  });
}

// Everything OUTSIDE the card, so "a per-card failure never blanks unrelated
// cards" and "retry is scoped to that card" are measured rather than asserted.
async function dashboardOutsideCard(page) {
  return page.evaluate(() => {
    const slot = document.getElementById("sp-card-slot");
    const cards = Array.from(document.querySelectorAll("#content .dash-card"))
      .filter((c) => !slot || !slot.contains(c));
    return {
      count: cards.length,
      headings: cards.map((c) => ((c.querySelector("h3, h2") || {}).textContent
        || "").replace(/\s+/g, " ").trim()),
      rowCounts: cards.map((c) => c.querySelectorAll(".li").length),
      html: cards.map((c) => c.outerHTML).join("\n<!--card-->\n"),
    };
  });
}

const SNAPSHOT_FIELDS = ["view", "hasSlot", "slotRole", "slotLive", "busy",
  "slotHtml", "headings", "buttons", "primaries", "rows", "progressLine",
  "state", "reason", "staleFrom", "generation", "counter", "identityProgram",
  "identitySeason", "intentPending", "selected", "ledgerCards", "focus",
  "toast"];

function snapKey(card) {
  const o = {};
  SNAPSHOT_FIELDS.forEach((f) => { o[f] = JSON.stringify(card[f]); });
  return o;
}

function assertSame(before, after, L, step) {
  const a = snapKey(before), b = snapKey(after);
  for (const f of SNAPSHOT_FIELDS) {
    if (a[f] !== b[f]) {
      fail(`[${L}/${step}] releasing the superseded response changed the card's `
        + `"${f}" — an older response mutated newer state, which is exactly the `
        + `race #365 requires this card to discard.\n  before: `
        + `${String(a[f]).slice(0, 800)}\n  after:  ${String(b[f]).slice(0, 800)}`);
    }
  }
}

function assertDiffers(x, y, L, step, why) {
  const a = snapKey(x), b = snapKey(y);
  if (SNAPSHOT_FIELDS.every((f) => a[f] === b[f])) {
    fail(`[${L}/${step}] ${why} — the two states produce IDENTICAL snapshots, so `
      + `the before/after comparison would pass no matter what happened`);
  }
}

// ============================ KEYBOARD DRIVING ============================
// A real, BOUNDED Tab traversal from a defined starting point — never
// page.focus(), which can jump past the real tab order and never triggers
// :focus-visible. The whole trail is kept so a failure names where the
// traversal actually went.
async function tabTo(page, selector, L, step, maxSteps) {
  await page.evaluate(() => {
    if (document.activeElement && document.activeElement !== document.body) {
      document.activeElement.blur();
    }
  });
  const max = maxSteps || 80;
  const trail = [];
  for (let i = 0; i < max; i++) {
    await page.keyboard.press("Tab");
    const d = await page.evaluate((sel) => {
      const a = document.activeElement;
      if (!a || a === document.body) return { tag: "BODY", matched: false };
      const cs = getComputedStyle(a);
      return {
        tag: a.tagName, id: a.id, cls: a.className,
        text: (a.textContent || "").replace(/\s+/g, " ").trim().slice(0, 40),
        matched: a.matches(sel),
        focusVisible: a.matches(":focus-visible"),
        outlineStyle: cs.outlineStyle, outlineWidth: cs.outlineWidth,
        boxShadow: cs.boxShadow,
      };
    }, selector);
    trail.push(`${i}:${d.tag}#${d.id || ""}.${d.cls || ""}{${d.text || ""}}`);
    if (d.matched) {
      if (!d.focusVisible) {
        fail(`[${L}/${step}] Tab landed on "${selector}" but the browser does `
          + `not consider it :focus-visible, so a keyboard operator would get `
          + `no focus cue`);
      }
      const hasIndicator = (d.outlineStyle && d.outlineStyle !== "none"
          && d.outlineWidth !== "0px") || (d.boxShadow && d.boxShadow !== "none");
      if (!hasIndicator) {
        fail(`[${L}/${step}] Tab landed on "${selector}" with NO computed focus `
          + `indicator (outline ${d.outlineStyle}/${d.outlineWidth}, box-shadow `
          + `${d.boxShadow})`);
      }
      return { steps: i + 1, trail: trail, node: d };
    }
  }
  fail(`[${L}/${step}] a bounded Tab traversal of ${max} steps never reached `
    + `"${selector}". Trail:\n  ${trail.join("\n  ")}`);
  return null;
}

// Re-enter the Dashboard through the app's own navigation controls, which is
// what issues a fresh render-driven load of this card. Two clicks on purpose:
// clicking the tab you are already on is not a transition an operator makes.
async function renderDashboardAgain(page, L, step) {
  await page.click('.tab[data-tab="calendar"]');
  await page.waitForFunction(() => document.body.dataset.view === "calendar",
    null, { timeout: 20000 })
    .catch(() => fail(`[${L}/${step}] the Calendar tab never took`));
  await page.click('.tab[data-tab="dashboard"]');
  await page.waitForFunction(
    () => document.body.dataset.view === "dashboard"
      && !!document.getElementById("sp-card-slot"),
    null, { timeout: 20000 })
    .catch(() => fail(`[${L}/${step}] the Dashboard never came back with its `
      + `Home/Tasks slot`));
}

// Switch context through the REAL #ctx-select, exactly as an operator does.
// The wait has THREE conjuncts, all necessary (established on this branch):
//   (1) the CONFIRMED tuple — program AND season, so a Program-only move is
//       waited for as honestly as a Season move;
//   (2) contextSwitchIntentPending === false — while it is set, every
//       context-scoped action control is withdrawn ON PURPOSE, so nothing
//       about "is this card actionable" can be sampled before it clears;
//   (3) a repaint of #content's OWN CHILDREN observed AFTER (2). `subtree:false`
//       is the point: repaintContextScopedCardsAsStale() and render()'s
//       retain-the-cards pass both rewrite card slots (DESCENDANTS of #content)
//       from HELD models while the withdrawal is still in force, and a subtree
//       observer is satisfied by those and returns too early.
async function switchContext(page, programId, seasonId, L, step) {
  await page.evaluate(([p, s]) => {
    if (window.__swObs) window.__swObs.disconnect();
    window.__swPainted = false;
    const c = document.getElementById("content");
    window.__swObs = new MutationObserver(() => {
      if (contextSwitchIntentPending) return;
      const cur = (contextOptions && contextOptions.selected) || {};
      if (cur.program_id !== p || (cur.season_id || null) !== s) return;
      window.__swPainted = true;
    });
    if (c) window.__swObs.observe(c, { childList: true, subtree: false });
  }, [programId, seasonId]);

  const ok = await page.evaluate(([p, s]) => {
    const sel = document.getElementById("ctx-select");
    if (!sel) return "no #ctx-select";
    if (sel.hidden) return "#ctx-select is hidden (single authorized context)";
    const want = `${p}|${s || ""}`;
    if (!Array.from(sel.options).some((o) => o.value === want)) {
      return `#ctx-select offers no option "${want}": `
        + JSON.stringify(Array.from(sel.options).map((o) => o.value));
    }
    sel.value = want;
    sel.dispatchEvent(new Event("change", { bubbles: true }));
    return true;
  }, [programId, seasonId]);
  if (ok !== true) fail(`[${L}/${step}] ${ok}`);
  await waitForContextReconciled(page, programId, seasonId, L, step);
  await page.evaluate(() => {
    if (window.__swObs) window.__swObs.disconnect();
    window.__swObs = null;
  }).catch(() => {});
}

async function waitForContextReconciled(page, programId, seasonId, L, step) {
  await page.waitForFunction(([p, s]) => {
    if (contextSwitchIntentPending) return false;
    const cur = (contextOptions && contextOptions.selected) || {};
    if (cur.program_id !== p || (cur.season_id || null) !== s) return false;
    return window.__swPainted === true;
  }, [programId, seasonId], { timeout: 30000 })
    .catch(async () => {
      const why = await page.evaluate(() => ({
        intentPending: !!contextSwitchIntentPending,
        selected: (contextOptions && contextOptions.selected) || null,
        painted: !!window.__swPainted,
      })).catch(() => null);
      fail(`[${L}/${step}] the context switch to ${programId}/${seasonId || "(no season)"} `
        + `never RECONCILED — the confirmed tuple, the release of the action-control `
        + `withdrawal and a repaint after it are all required before this card can `
        + `be sampled: ${JSON.stringify(why)}`);
    });
}

// The battery, run identically for every role. Returns the ENABLED mutation
// controls the current surface offers.
async function mutationControls(page, selector) {
  return page.evaluate((sel) => {
    const all = Array.from(document.querySelectorAll(sel));
    return {
      total: all.length,
      enabled: all.filter((el) => !el.disabled
          && el.getAttribute("aria-disabled") !== "true")
        .map((el) => `${el.tagName}{${(el.textContent || "").replace(/\s+/g, " ").trim().slice(0, 40)}}`),
      // The containers themselves, so "no controls" is never satisfied by a
      // surface that simply did not render.
      contentPainted: !!document.getElementById("content")
        && document.getElementById("content").children.length > 0,
      contentText: (document.getElementById("content").innerText || "")
        .replace(/\s+/g, " ").trim().slice(0, 400),
      view: document.body.dataset.view,
      hasHomeSlot: !!document.getElementById("sp-card-slot"),
      setupCardSlots: document.querySelectorAll("[data-setup-card-slot]").length,
      workflowGo: document.querySelectorAll("[data-setup-workflow-go]").length,
      // The hub's own permission-gated "Open <workflow>" entries. Navigation
      // rather than mutation, so they are not in the battery — but a Viewer
      // must not be given them either, and a League Admin must have them, or
      // the Viewer's zero is a statement about an unrendered hub.
      workflowOpen: document.querySelectorAll("[data-setup-workflow]").length,
      visibleTabs: Array.from(document.querySelectorAll(".tab"))
        .filter((t) => t.offsetParent !== null).map((t) => t.dataset.tab)
        .filter((t) => !!t),
    };
  }, selector);
}

async function checkViewport(browser, viewport) {
  const base = `http://${HOST}:${viewport.port}`;
  const L = viewport.label;
  const server = spawn(
    process.env.PYTHON || "python3",
    ["-u", "-m", "hockey_scheduler.web.server", "--host", HOST,
     "--port", String(viewport.port)],
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

  // ---- the four independent records the reconciler is handed at the end.
  const nonOk = [];          // { method, url, status }
  const failedReqs = [];     // { method, url, failure }
  const consoleErrors = [];  // { text, url }
  const declared = [];       // { method, url, status, matched, origin }

  // Declared from INSIDE a route handler, with the route itself, so the
  // allowance carries the concrete URL rather than a category.
  const declareInjected = (route, status) => {
    const req = route.request();
    const url = req.url();
    if (!PROGRESS_RE.test(new URL(url).pathname)) {
      // Pushed rather than thrown: a throw inside a route handler is swallowed
      // by Playwright and would silently disarm this guard.
      errors.push(`[declared] a deliberate ${status} was declared for `
        + `${req.method()} ${url}, which is not this journey's progress endpoint`);
      return;
    }
    declared.push({ method: req.method(), url: url, status: status,
                    matched: false, origin: "route-injected" });
  };
  // Declared for a refusal this journey provokes from the SERVER (the Viewer's
  // own 403). Same triple, same one-shot consumption, different origin.
  const declareExpected = (method, url, status, origin) => {
    declared.push({ method, url, status, matched: false, origin });
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

  // Deliveries the PAGE actually received, so "the held response was released"
  // is observed rather than assumed before any "after" snapshot is taken; and
  // every request METHOD, so leg 1i can prove this card is a pure read.
  let progressDeliveries = 0;
  const progressMethods = new Set();
  let requestCount = 0;
  const requestLog = [];
  page.on("request", (rq) => {
    requestCount += 1;
    requestLog.push(`${rq.method()} ${new URL(rq.url()).pathname}`);
    if (PROGRESS_RE.test(new URL(rq.url()).pathname)) {
      progressMethods.add(rq.method());
    }
  });
  page.on("response", (r) => {
    if (PROGRESS_RE.test(new URL(r.url()).pathname)) progressDeliveries += 1;
    const s = r.status();
    if (s < 200 || s > 299) {
      nonOk.push({ method: r.request().method(), url: r.url(), status: s });
    }
  });
  page.on("requestfailed", (req) => {
    const f = req.failure();
    failedReqs.push({ method: req.method(), url: req.url(),
                      failure: (f && f.errorText) || "unknown" });
  });

  progressDirective = null;
  optionsGate = null;
  await installProgressControl(page, declareInjected);
  await installContextOptionsControl(page);

  try {
    await waitForServer(`${base}/api/health`, READY_TIMEOUT_MS);
    await page.goto(base, { waitUntil: "domcontentloaded" });
    await contentPainted(page);
    await loginAs(page, "admin", "demo", `${L}/bootstrap`);
    await armLiveRegions(page);

    // ================== LEG 1h — THE NO-DATA EMPTY PATH ===================
    // Run FIRST, on purpose: it is the only leg whose precondition is a store
    // with NO Program at all, and /api/demo/load below destroys that forever.
    // The onboarding wizard owns the first-Program flow, so this is reached
    // the way an operator reaches it — through the wizard's own "go to the
    // Dashboard" control — rather than by forcing a view.
    await reenter(page, base);
    await reachDashboard(page, L, "1h");
    await settled(page, `${L}/1h`);
    const empty = await homeCard(page);
    if (empty.state !== "empty" || empty.reason !== "no_program") {
      fail(`[${L}/1h] with no Program at all the card must resolve to the named `
        + `EMPTY/"no_program" state, got ${empty.state}/${empty.reason}`);
    }
    // ANTI-VACUITY: the container is proven PRESENT before it is asserted
    // EMPTY. "No buttons" where the container itself is absent is the exact
    // failure this branch has already shipped once.
    if (!empty.hasSlot) {
      fail(`[${L}/1h] #sp-card-slot is not in the document at all, so every `
        + `"the empty state offers nothing" assertion below would be vacuous`);
    }
    if (empty.slotRole !== "status" || empty.slotLive !== "polite") {
      fail(`[${L}/1h] the empty card's slot lost its live-region semantics `
        + `(role=${empty.slotRole}, aria-live=${empty.slotLive})`);
    }
    if (empty.busy !== "false") {
      fail(`[${L}/1h] the empty state is settled but still reports `
        + `aria-busy=${empty.busy}`);
    }
    // EMPTY IS A RENDERED STATE (#365 owner correction). It used to return the
    // empty string for both reasons; a no-Program operator received no
    // explanation of what was missing and no path to it.
    if (!empty.slotHtml) {
      fail(`[${L}/1h] EMPTY/"no_program" rendered NOTHING at all, so an `
        + `operator with no Program is told nothing about what is missing`);
    }
    if (JSON.stringify(empty.headings) !== JSON.stringify([H_EMPTY_NO_PROGRAM])) {
      fail(`[${L}/1h] EMPTY/"no_program" must carry its own stable semantic `
        + `heading "${H_EMPTY_NO_PROGRAM}": ${JSON.stringify(empty.headings)}`);
    }
    if (empty.emptyStatus !== EMPTY_NO_PROGRAM_STATUS) {
      fail(`[${L}/1h] EMPTY/"no_program" must carry its own status text `
        + `"${EMPTY_NO_PROGRAM_STATUS}"; it carried `
        + `${JSON.stringify(empty.emptyStatus)}`);
    }
    // REASON-SPECIFIC: the explanation names the missing PROGRAM and the path
    // to it, and is not the other reason's sentence.
    if (empty.slotText.indexOf(EMPTY_NO_PROGRAM_PATH) === -1) {
      fail(`[${L}/1h] EMPTY/"no_program" does not explain how the missing `
        + `program comes into being: ${empty.slotText}`);
    }
    if (empty.slotText.indexOf(EMPTY_NOTHING_STATUS) !== -1) {
      fail(`[${L}/1h] EMPTY/"no_program" is rendering EMPTY/"nothing_actionable"`
        + `'s copy, so the two named reasons have been collapsed into one: `
        + `${empty.slotText}`);
    }
    // ROLE-CORRECT USABLE ACTION COUNT. This operator is a League Admin, the
    // one role MANAGE_SETUP-gated onboarding.js will actually let create the
    // first Program — so exactly ONE enabled primary path is exposed, and it
    // is the wizard that genuinely resolves this state.
    if (empty.buttons.length !== 1 || empty.buttons[0].disabled
        || empty.buttons[0].text !== CTA_START_ONBOARDING
        || empty.buttons[0].goto !== "onboarding") {
      fail(`[${L}/1h] EMPTY/"no_program" must expose exactly one ENABLED, `
        + `genuinely authorized primary path ("${CTA_START_ONBOARDING}" → the `
        + `Initial Setup wizard): ${JSON.stringify(empty.buttons)}`);
    }
    if (JSON.stringify(empty.primaries) !== JSON.stringify([CTA_START_ONBOARDING])) {
      fail(`[${L}/1h] the one-primary-action-per-screen rule: `
        + `${JSON.stringify(empty.primaries)}`);
    }
    await assertSentenceAppearsOnce(page, H_EMPTY_NO_PROGRAM, L, "1h");
    await assertSentenceAppearsOnce(page, EMPTY_NO_PROGRAM_STATUS, L, "1h");
    await assertNoNestedEcho(page, L, "1h");
    assertCardSpokeOnce(await liveWrites(page), H_EMPTY_NO_PROGRAM, L, "1h");
    // The rest of the Dashboard is painted — this is a PER-CARD empty, not a
    // blank page that would satisfy the assertions above for the wrong reason.
    const emptyOutside = await dashboardOutsideCard(page);
    if (!emptyOutside.count) {
      fail(`[${L}/1h] the Dashboard has no other card at all, so "the card's `
        + `own empty state renders nothing" is indistinguishable from "the `
        + `page failed to render"`);
    }

    // ---------------------------- fixtures -----------------------------
    // The BODY is read, not just the status: a fetch whose response stream is
    // never consumed stays open until the next navigation cancels it, and
    // Chromium reports that cancellation as a failed request the recorder
    // above correctly refuses to ignore.
    const loadStatus = await page.evaluate(() => fetch("/api/demo/load", {
      method: "POST", credentials: "same-origin",
      headers: { "Content-Type": "application/json" }, body: "{}",
    }).then(async (r) => { await r.text(); return r.status; }));
    if (loadStatus !== 200) fail(`[${L}] demo load failed (status ${loadStatus})`);

    // Two brand-new Programs with NOTHING in them, and the demo Program that
    // is fully set up. Three DIFFERENT settled states on the same page, which
    // is what makes every "unchanged" and "differs" comparison sensitive.
    //   EMPTY_PROG  the no-data Program every role leg stands on.
    //   CTA_PROG    a second one, so leg 2a's real drawer submit (which really
    //               creates a Season) cannot silently change EMPTY_PROG's own
    //               precondition for the Arena Manager leg that follows.
    const fx = await page.evaluate(async () => {
      const post = async (p, b) => (await fetch(p, {
        method: "POST", credentials: "same-origin",
        headers: { "Content-Type": "application/json" }, body: JSON.stringify(b),
      })).json();
      const org = await post("/api/v2/setup/organization", { name: "HTSM Org" });
      const empty = await post("/api/v2/setup/program",
        { name: "HTSM Empty Program", country: "US",
          operator_organization_id: org.id });
      const cta = await post("/api/v2/setup/program",
        { name: "HTSM CTA Program", country: "US",
          operator_organization_id: org.id });
      return { org: org.id, empty: empty.id, cta: cta.id };
    });
    for (const k of ["org", "empty", "cta"]) {
      if (!fx[k]) fail(`[${L}] fixture failed to create ${k}: ${JSON.stringify(fx)}`);
    }
    // The demo Program and its Season, resolved from the switcher's own
    // options rather than hardcoded.
    await reenter(page, base);
    await reachDashboard(page, L, "fixtures");
    await settled(page, `${L}/fixtures`);
    const demo = await page.evaluate((skip) => {
      const sel = document.getElementById("ctx-select");
      const opt = Array.from(sel.options).map((o) => o.value)
        .find((v) => v.indexOf("|") > 0 && v.split("|")[1]
          && skip.indexOf(v.split("|")[0]) === -1);
      if (!opt) return null;
      return { program: opt.split("|")[0], season: opt.split("|")[1] };
    }, [fx.empty, fx.cta]);
    if (!demo) fail(`[${L}] the switcher offers no fully-populated Program/Season`);

    // ============ LEG 1a — LOADING, and the per-card boundary =============
    // The card's own read is held OPEN, so the loading state is observed at
    // the moment production actually shows it rather than raced past.
    await switchContext(page, demo.program, demo.season, L, "1a-arrive");
    await settled(page, `${L}/1a-arrive`);
    await armLiveRegions(page);
    const loadHold = holdNextProgress();
    await renderDashboardAgain(page, L, "1a");
    await page.waitForFunction(() => {
      const s = document.getElementById("sp-card-slot");
      return !!s && s.getAttribute("aria-busy") === "true"
        && !!s.querySelector(".skeleton");
    }, null, { timeout: 20000 })
      .catch(() => fail(`[${L}/1a] the held read never produced the card's own `
        + `LOADING state`));
    const loading = await homeCard(page);
    if (!loadHold.d.started) {
      fail(`[${L}/1a] the LOADING state was sampled but no progress request was `
        + `held, so this leg observed something else entirely`);
    }
    if (loading.busy !== "true") {
      fail(`[${L}/1a] the loading card is not aria-busy: ${loading.busy}`);
    }
    if (JSON.stringify(loading.headings) !== JSON.stringify([H_LOADING])) {
      fail(`[${L}/1a] the LOADING state must carry the stable semantic heading `
        + `"${H_LOADING}" (it was heading-less once, and so structurally `
        + `invisible to assistive tech): ${JSON.stringify(loading.headings)}`);
    }
    if (loading.slotText.indexOf(LOADING_SR) === -1) {
      fail(`[${L}/1a] the LOADING state lost its visually-hidden status text `
        + `"${LOADING_SR}": ${loading.slotText}`);
    }
    if (loading.buttons.length) {
      fail(`[${L}/1a] a card that is still reading offered controls: `
        + `${JSON.stringify(loading.buttons)}`);
    }
    // THE PER-CARD BOUNDARY: a slow read for THIS card must not hold up or
    // blank the rest of the Dashboard.
    const loadingOutside = await dashboardOutsideCard(page);
    if (!loadingOutside.count || loadingOutside.headings.every((h) => !h)) {
      fail(`[${L}/1a] the rest of the Dashboard is blank while this one card `
        + `is still reading — the per-card loading boundary is gone: `
        + `${JSON.stringify(loadingOutside)}`);
    }
    // The window is re-asserted AFTER the sample: a read that resolved
    // underneath would have turned every assertion above into an observation
    // of a different state.
    if (loadHold.d.settled) {
      fail(`[${L}/1a] the held read resolved while the LOADING state was being `
        + `sampled, so the sample was not taken inside the window`);
    }
    loadHold.release();
    await settled(page, `${L}/1a-release`);
    const afterLoad = await homeCard(page);
    if (afterLoad.state !== "success") {
      fail(`[${L}/1a] the demo Program's card must settle COMPLETE, got `
        + `${afterLoad.state}`);
    }
    assertCardSpokeOnce(await liveWrites(page), H_SUCCESS, L, "1a");

    // ============ LEG 1g — SUCCESS / COMPLETE, in full ====================
    if (JSON.stringify(afterLoad.headings) !== JSON.stringify([H_SUCCESS])) {
      fail(`[${L}/1g] the SUCCESS state's heading: `
        + `${JSON.stringify(afterLoad.headings)}`);
    }
    if (JSON.stringify(afterLoad.primaries) !== JSON.stringify([CTA_SCHEDULE])) {
      fail(`[${L}/1g] the SUCCESS state must offer exactly one primary action `
        + `("${CTA_SCHEDULE}"), got ${JSON.stringify(afterLoad.primaries)}`);
    }
    if (!afterLoad.progressLine || afterLoad.progressLine.indexOf("required") === -1) {
      fail(`[${L}/1g] the SUCCESS state lost its completion status text: `
        + `${afterLoad.progressLine}`);
    }
    await assertSentenceAppearsOnce(page, H_SUCCESS, L, "1g");
    await assertNoNestedEcho(page, L, "1g");

    // ================= LEG 1b — READY, the no-data path ===================
    await armLiveRegions(page);
    await switchContext(page, fx.empty, null, L, "1b");
    await settled(page, `${L}/1b`);
    const ready = await homeCard(page);
    if (ready.state !== "ready") {
      fail(`[${L}/1b] a Program with nothing in it must resolve READY with a `
        + `recommendation, got ${ready.state}/${ready.reason}`);
    }
    if (JSON.stringify(ready.headings) !== JSON.stringify([H_READY])) {
      fail(`[${L}/1b] the READY heading: ${JSON.stringify(ready.headings)}`);
    }
    // "Empty states explain what is missing": every required workflow is
    // listed, with its own status text and its own reason.
    if (!ready.rows || ready.rows.length < 5) {
      fail(`[${L}/1b] the no-data card must list what is missing; it listed `
        + `${ready.rows ? ready.rows.length : 0} workflow row(s)`);
    }
    const unexplained = ready.rows.filter((r) => !r.status || !r.detail);
    if (unexplained.length) {
      fail(`[${L}/1b] workflow rows without visible status text or an `
        + `explanation: ${JSON.stringify(unexplained)}`);
    }
    if (!ready.rows.some((r) => r.status === "To do")) {
      fail(`[${L}/1b] nothing is marked "To do" on a Program with no data: `
        + `${JSON.stringify(ready.rows)}`);
    }
    // "…and expose ONLY the authorized primary action" — exactly one, and it
    // is the one the backend named for this role.
    if (JSON.stringify(ready.primaries) !== JSON.stringify([CTA_ADD_SEASON])) {
      fail(`[${L}/1b] the no-data card must expose exactly one authorized `
        + `primary action ("${CTA_ADD_SEASON}"), got `
        + `${JSON.stringify(ready.primaries)}`);
    }
    if (ready.buttons.length !== 1 || ready.buttons[0].disabled) {
      fail(`[${L}/1b] the authorized primary action must be the card's only `
        + `control and must be ENABLED: ${JSON.stringify(ready.buttons)}`);
    }
    assertCardSpokeOnce(await liveWrites(page), H_READY, L, "1b");
    await assertSentenceAppearsOnce(page, H_READY, L, "1b");
    await assertNoNestedEcho(page, L, "1b");
    // Kept for the role comparison in leg 2b: the SAME tuple, a different role.
    const adminOnEmpty = ready;

    // =========== LEG 1c/1d/1e — ERROR, keyboard retry, scoping ============
    const readyOutside = await dashboardOutsideCard(page);
    await armLiveRegions(page);
    failNextProgress(503);
    await renderDashboardAgain(page, L, "1c");
    await settled(page, `${L}/1c`);
    const errored = await homeCard(page);
    if (errored.state !== "error") {
      fail(`[${L}/1c] a forced 503 on this card's own read must produce its `
        + `ERROR state, got ${errored.state}`);
    }
    if (JSON.stringify(errored.headings) !== JSON.stringify([H_ERROR])) {
      fail(`[${L}/1c] the ERROR heading: ${JSON.stringify(errored.headings)}`);
    }
    if (errored.slotText.indexOf(ERROR_SENTENCE) === -1) {
      fail(`[${L}/1c] the ERROR state lost its status text "${ERROR_SENTENCE}": `
        + `${errored.slotText}`);
    }
    if (errored.busy !== "false") {
      fail(`[${L}/1c] a card whose read has failed still reports aria-busy=true`);
    }
    if (errored.buttons.length !== 1 || !errored.buttons[0].retry
        || errored.buttons[0].text !== CTA_RETRY) {
      fail(`[${L}/1c] the ERROR state must offer exactly one control, its own `
        + `scoped "${CTA_RETRY}": ${JSON.stringify(errored.buttons)}`);
    }
    // A stale primary action must not survive into the failure.
    if (errored.slotText.indexOf(CTA_ADD_SEASON) !== -1) {
      fail(`[${L}/1c] the failed card still shows the previous state's primary `
        + `action "${CTA_ADD_SEASON}"`);
    }
    // PER-CARD FAILURE NEVER BLANKS UNRELATED CARDS. Compared against the same
    // page in its READY state: same cards, same headings, same row counts.
    const erroredOutside = await dashboardOutsideCard(page);
    if (erroredOutside.count !== readyOutside.count
        || JSON.stringify(erroredOutside.headings) !== JSON.stringify(readyOutside.headings)
        || JSON.stringify(erroredOutside.rowCounts) !== JSON.stringify(readyOutside.rowCounts)) {
      fail(`[${L}/1c] this card's failure changed the OTHER Dashboard cards.\n`
        + `  ready:   ${JSON.stringify(readyOutside.headings)} rows `
        + `${JSON.stringify(readyOutside.rowCounts)}\n`
        + `  errored: ${JSON.stringify(erroredOutside.headings)} rows `
        + `${JSON.stringify(erroredOutside.rowCounts)}`);
    }
    assertCardSpokeOnce(await liveWrites(page), ERROR_SENTENCE, L, "1c");
    await assertSentenceAppearsOnce(page, ERROR_SENTENCE, L, "1c");
    await assertNoNestedEcho(page, L, "1c");

    // (1d) KEYBOARD ACTIVATION, and the EXACT focus target after retry.
    //      Reached by a real bounded Tab traversal from a defined starting
    //      point, never page.focus().
    await armLiveRegions(page);
    const reachRetry = await tabTo(page, "#sp-card-slot [data-setup-progress-retry]",
      L, "1d");
    const beforeRetryOutside = await dashboardOutsideCard(page);
    const requestsBefore = requestCount;
    const progressBefore = progressDeliveries;
    await page.keyboard.press("Enter");
    await settled(page, `${L}/1d`);
    const retried = await homeCard(page);
    if (retried.state !== "ready") {
      fail(`[${L}/1d] keyboard-activating Retry did not recover the card: `
        + `${retried.state}`);
    }
    // THE EXACT FOCUS TARGET: this card's own heading, inside its own slot,
    // stamped with the tabindex="-1" focusCardTarget() uses for a
    // non-focusable destination.
    if (!retried.focusIsCardHeading) {
      fail(`[${L}/1d] after a keyboard-activated Retry, focus must land on the `
        + `recovered card's own heading; it is on ${retried.focus}`);
    }
    if (retried.focus.indexOf(H_READY) === -1
        || retried.focus.indexOf("tabindex=-1") === -1) {
      fail(`[${L}/1d] the focus target after retry is not the "${H_READY}" `
        + `heading with tabindex="-1": ${retried.focus}`);
    }
    // (1e) RETRY IS SCOPED TO THIS CARD: exactly one request left the browser,
    //      it was this card's own read, and nothing outside the card moved by
    //      a single byte (no render ran between the two samples).
    const issued = requestLog.slice(requestsBefore);
    if (issued.length !== 1 || !/\/api\/v2\/setup\/progress$/.test(issued[0])) {
      fail(`[${L}/1e] Retry must refetch this card and nothing else; the `
        + `browser issued ${JSON.stringify(issued)}`);
    }
    if (progressDeliveries !== progressBefore + 1) {
      fail(`[${L}/1e] Retry did not produce exactly one progress delivery `
        + `(${progressBefore} -> ${progressDeliveries})`);
    }
    const afterRetryOutside = await dashboardOutsideCard(page);
    if (afterRetryOutside.html !== beforeRetryOutside.html) {
      fail(`[${L}/1e] Retry repainted something outside this card, so it is not `
        + `scoped to it`);
    }
    assertCardSpokeOnce(await liveWrites(page), H_READY, L, "1d");

    // (1d') A RETRY THAT FAILS AGAIN lands on the ERROR heading, not nowhere.
    await armLiveRegions(page);
    failNextProgress(503);
    await renderDashboardAgain(page, L, "1d-again");
    await settled(page, `${L}/1d-again`);
    await tabTo(page, "#sp-card-slot [data-setup-progress-retry]", L, "1d-again");
    failNextProgress(503);
    await page.keyboard.press("Enter");
    await settled(page, `${L}/1d-again2`);
    const twice = await homeCard(page);
    if (twice.state !== "error" || !twice.focusIsCardHeading
        || twice.focus.indexOf(H_ERROR) === -1) {
      fail(`[${L}/1d'] a retry that fails again must leave focus on the ERROR `
        + `heading rather than dropping it: state=${twice.state} focus=${twice.focus}`);
    }
    // A RENDER-DRIVEN load must NOT move focus — only a load a person asked for
    // may. Focus is parked on a stable control outside the card first.
    await page.focus('.tab[data-tab="dashboard"]');
    const parked = await homeCard(page);
    await page.evaluate(() => loadSetupProgressCard());
    await settled(page, `${L}/1d-render-driven`);
    const unmoved = await homeCard(page);
    if (unmoved.focus !== parked.focus) {
      fail(`[${L}/1d'] a routine, non-user-initiated load moved keyboard focus `
        + `(${parked.focus} -> ${unmoved.focus})`);
    }

    // ==================== LEG 1f — STALE, and completion ==================
    // The stale window is made OBSERVABLE by holding the one reconciliation
    // round trip a switch awaits. Nothing about the response changes.
    await armLiveRegions(page);
    await settled(page, `${L}/1f-arrive`);
    const beforeStale = await homeCard(page);
    if (beforeStale.state === "empty") {
      fail(`[${L}/1f] the pre-switch card is EMPTY, so there would be nothing `
        + `to retain and the STALE assertions below would be vacuous`);
    }
    const releaseOptions = holdContextOptions();
    let staleSampled = null;
    try {
      await page.evaluate(([p, s]) => {
        const sel = document.getElementById("ctx-select");
        sel.value = `${p}|${s}`;
        sel.dispatchEvent(new Event("change", { bubbles: true }));
      }, [demo.program, demo.season]);
      await page.waitForFunction(
        () => readCardState("home/setup-progress").state === "stale",
        null, { timeout: 20000 })
        .catch(() => fail(`[${L}/1f] the card never entered STALE while its `
          + `tuple was moving`));
      staleSampled = await homeCard(page);
      // Re-assert the window is still open AFTER the sample.
      const stillInside = await page.evaluate(() => !!contextSwitchIntentPending);
      if (!stillInside) {
        fail(`[${L}/1f] the switch reconciled while the STALE state was being `
          + `sampled, so the sample was not taken inside the window`);
      }
    } finally {
      releaseOptions();
    }
    if (staleSampled.staleFrom !== "ready") {
      fail(`[${L}/1f] STALE must remember which settled state it is retaining; `
        + `staleFrom=${staleSampled.staleFrom}`);
    }
    if (JSON.stringify(staleSampled.headings) !== JSON.stringify([H_STALE])) {
      fail(`[${L}/1f] the STALE heading: ${JSON.stringify(staleSampled.headings)}`);
    }
    if (staleSampled.busy !== "true") {
      fail(`[${L}/1f] a card showing earlier data while a newer read is coming `
        + `must stay aria-busy; it reports ${staleSampled.busy}`);
    }
    // The retained read is really on screen — the whole justification of the
    // state — and it is the SAME rows the settled state had.
    if (JSON.stringify(staleSampled.rows) !== JSON.stringify(beforeStale.rows)) {
      fail(`[${L}/1f] STALE claims to retain the last good read but the rows `
        + `differ.\n  before: ${JSON.stringify(beforeStale.rows)}\n  stale:  `
        + `${JSON.stringify(staleSampled.rows)}`);
    }
    // THE OBSOLETE PRIMARY ACTION IS WITHDRAWN, and only a ghost refresh is left.
    if (staleSampled.primaries.length) {
      fail(`[${L}/1f] the STALE card still offers a primary action bound to a `
        + `context the operator has left: ${JSON.stringify(staleSampled.primaries)}`);
    }
    if (staleSampled.buttons.length !== 1
        || staleSampled.buttons[0].text !== CTA_REFRESH
        || !/ghost/.test(staleSampled.buttons[0].cls)) {
      fail(`[${L}/1f] the STALE card must offer exactly one control, the ghost `
        + `"${CTA_REFRESH}": ${JSON.stringify(staleSampled.buttons)}`);
    }
    if (staleSampled.slotText.indexOf(CTA_ADD_SEASON) !== -1) {
      fail(`[${L}/1f] the withdrawn primary action's label is still on the `
        + `stale surface`);
    }
    await assertSentenceAppearsOnce(page, H_STALE, L, "1f");
    await waitForContextReconciled(page, demo.program, demo.season, L, "1f-reconcile");
    await settled(page, `${L}/1f-settled`);
    assertCardSpokeOnce(await liveWrites(page), H_SUCCESS, L, "1f");

    // EXACT FOCUS AFTER COMPLETION, driven by keyboard: from the settled
    // COMPLETE state, a user-initiated Refresh re-reads the card and must land
    // focus on the completion heading itself.
    await armLiveRegions(page);
    failNextProgress(503);
    await renderDashboardAgain(page, L, "1g-focus");
    await settled(page, `${L}/1g-focus`);
    await tabTo(page, "#sp-card-slot [data-setup-progress-retry]", L, "1g-focus");
    await page.keyboard.press("Enter");
    await settled(page, `${L}/1g-focus2`);
    const completed = await homeCard(page);
    if (completed.state !== "success" || !completed.focusIsCardHeading
        || completed.focus.indexOf(H_SUCCESS) === -1) {
      fail(`[${L}/1g] after a keyboard-driven recovery into the COMPLETE state, `
        + `focus must be on "${H_SUCCESS}": state=${completed.state} `
        + `focus=${completed.focus}`);
    }
    assertCardSpokeOnce(await liveWrites(page), H_SUCCESS, L, "1g-focus");

    // ============ LEG 3 — STALE RESPONSES CANNOT WIN =====================
    // (3a) a delayed SUCCESS released after a NEWER FAILURE settled.
    await armLiveRegions(page);
    await switchContext(page, fx.empty, null, L, "3a-arrive");
    await settled(page, `${L}/3a-arrive`);
    const staleHold = holdNextProgress();
    await renderDashboardAgain(page, L, "3a-hold");
    await page.waitForFunction(() => {
      const s = document.getElementById("sp-card-slot");
      return !!s && s.getAttribute("aria-busy") === "true";
    }, null, { timeout: 20000 })
      .catch(() => fail(`[${L}/3a] the held read never started`));
    failNextProgress(503);
    await renderDashboardAgain(page, L, "3a-newer");
    await settled(page, `${L}/3a-newer`);
    const newerFailure = await homeCard(page);
    if (newerFailure.state !== "error") {
      fail(`[${L}/3a] the NEWER read was supposed to fail; card is `
        + `${newerFailure.state}`);
    }
    if (!staleHold.d.fetched || staleHold.d.upstreamStatus !== 200) {
      fail(`[${L}/3a] the held response is not a genuine server success `
        + `(fetched=${staleHold.d.fetched} status=${staleHold.d.upstreamStatus}), `
        + `so discarding it would prove nothing`);
    }
    if (staleHold.d.upstreamProgram !== fx.empty) {
      fail(`[${L}/3a] the held response is not the one this leg thinks it is `
        + `(program ${staleHold.d.upstreamProgram})`);
    }
    await armLiveRegions(page);
    const beforeRelease3a = await homeCard(page);
    const delivered3aBefore = progressDeliveries;
    staleHold.release();
    await page.waitForTimeout(1500);
    if (progressDeliveries <= delivered3aBefore) {
      fail(`[${L}/3a] the held response never reached the page, so "nothing `
        + `changed" is the accident of an undelivered response`);
    }
    const afterRelease3a = await homeCard(page);
    assertSame(beforeRelease3a, afterRelease3a, L, "3a");
    if (afterRelease3a.slotText.indexOf(CTA_ADD_SEASON) !== -1) {
      fail(`[${L}/3a] releasing the superseded success RESTORED the obsolete `
        + `primary action "${CTA_ADD_SEASON}" over the newer failure`);
    }
    const spoke3a = spokenWrites(await liveWrites(page));
    if (spoke3a.length) {
      fail(`[${L}/3a] a superseded response ANNOUNCED: ${JSON.stringify(spoke3a)}`);
    }

    // (3b) a delayed SUCCESS released after a full CONTEXT SWITCH settled.
    await armLiveRegions(page);
    // Back to a settled READY on Program A: leg 3a deliberately left this card
    // in ERROR, and holding a read from a FAILED card would test a different
    // thing entirely.
    await renderDashboardAgain(page, L, "3b-recover");
    await settled(page, `${L}/3b-recover`);
    const onA = await homeCard(page);
    const holdA = holdNextProgress();
    await renderDashboardAgain(page, L, "3b-hold");
    await page.waitForFunction(() => {
      const s = document.getElementById("sp-card-slot");
      return !!s && s.getAttribute("aria-busy") === "true";
    }, null, { timeout: 20000 })
      .catch(() => fail(`[${L}/3b] the held read for Program A never started`));
    await switchContext(page, demo.program, demo.season, L, "3b-switch");
    await settled(page, `${L}/3b-switch`);
    const onB = await homeCard(page);
    assertDiffers(onA, onB, L, "3b",
      "Program A and Program B settle into the same card");
    if (!holdA.d.fetched || holdA.d.upstreamStatus !== 200
        || holdA.d.upstreamProgram !== fx.empty) {
      fail(`[${L}/3b] the held response is not a genuine success for Program A: `
        + `${JSON.stringify({ fetched: holdA.d.fetched, status: holdA.d.upstreamStatus,
                              program: holdA.d.upstreamProgram })}`);
    }
    await armLiveRegions(page);
    const beforeRelease3b = await homeCard(page);
    const delivered3bBefore = progressDeliveries;
    holdA.release();
    await page.waitForTimeout(1500);
    if (progressDeliveries <= delivered3bBefore) {
      fail(`[${L}/3b] Program A's held response never reached the page`);
    }
    const afterRelease3b = await homeCard(page);
    assertSame(beforeRelease3b, afterRelease3b, L, "3b");
    if (afterRelease3b.slotText.indexOf(CTA_ADD_SEASON) !== -1) {
      fail(`[${L}/3b] an older context's response restored ITS primary action `
        + `("${CTA_ADD_SEASON}") on the new context's card`);
    }
    const spoke3b = spokenWrites(await liveWrites(page));
    if (spoke3b.length) {
      fail(`[${L}/3b] a response from the context the operator LEFT announced `
        + `into the current one: ${JSON.stringify(spoke3b)}`);
    }

    // (3c) the mirror image: a delayed FAILURE released after a newer SUCCESS.
    //      A superseded failure must be exactly as inert as a superseded
    //      success, or "we discard stale responses" is only half true.
    await armLiveRegions(page);
    const holdFail = holdNextProgress({ fail: true });
    await renderDashboardAgain(page, L, "3c-hold");
    await page.waitForFunction(() => {
      const s = document.getElementById("sp-card-slot");
      return !!s && s.getAttribute("aria-busy") === "true";
    }, null, { timeout: 20000 })
      .catch(() => fail(`[${L}/3c] the held failing read never started`));
    await renderDashboardAgain(page, L, "3c-newer");
    await settled(page, `${L}/3c-newer`);
    const newerSuccess = await homeCard(page);
    if (newerSuccess.state !== "success") {
      fail(`[${L}/3c] the newer read was supposed to succeed; card is `
        + `${newerSuccess.state}`);
    }
    await armLiveRegions(page);
    const beforeRelease3c = await homeCard(page);
    const delivered3cBefore = progressDeliveries;
    holdFail.release();
    await page.waitForTimeout(1500);
    if (progressDeliveries <= delivered3cBefore) {
      fail(`[${L}/3c] the held 503 never reached the page`);
    }
    const afterRelease3c = await homeCard(page);
    assertSame(beforeRelease3c, afterRelease3c, L, "3c");
    const spoke3c = spokenWrites(await liveWrites(page));
    if (spoke3c.length) {
      fail(`[${L}/3c] a superseded FAILURE announced: ${JSON.stringify(spoke3c)}`);
    }

    // ====== LEG 2a — THE AUTHORIZED PRIMARY ACTION, PROVEN BY USING IT =====
    // Simultaneously: the anti-vacuity control for leg 2b/2c ("the control
    // exists and is enabled for an authorized role in the same fixture"), and
    // the liveness control for the announcement ledger ("#toast-root never
    // spoke" is worthless from an observer that cannot see #toast-root).
    await armLiveRegions(page);
    await switchContext(page, fx.cta, null, L, "2a");
    await settled(page, `${L}/2a`);
    const ctaCard = await homeCard(page);
    if (ctaCard.state !== "ready"
        || JSON.stringify(ctaCard.primaries) !== JSON.stringify([CTA_ADD_SEASON])) {
      fail(`[${L}/2a] the CTA fixture is not offering the authorized primary `
        + `action: ${ctaCard.state} ${JSON.stringify(ctaCard.primaries)}`);
    }
    await tabTo(page, "#sp-card-slot [data-setup-progress-action]", L, "2a");
    await page.keyboard.press("Enter");
    await page.waitForSelector(".drawer", { timeout: 20000 })
      .catch(() => fail(`[${L}/2a] keyboard-activating the card's primary action `
        + `did not open its real production entry point`));
    const seeded = await page.evaluate((pid) => {
      const f = document.querySelector("#f-season-league");
      return { hasName: !!document.querySelector("#f-season"),
               league: f ? f.value : null, matches: f ? f.value === pid : false };
    }, fx.cta);
    if (!seeded.hasName || !seeded.matches) {
      fail(`[${L}/2a] the drawer the card opened is not seeded from the active `
        + `Program: ${JSON.stringify(seeded)}`);
    }
    await page.fill("#f-season", "HTSM CTA Season");
    await page.click(".drawer button.act.primary");
    await page.waitForFunction((t) => {
      const r = document.getElementById("toast-root");
      const m = r && r.querySelector(".toast-msg");
      return !!r && !r.hidden && !!m && m.textContent.trim() === t;
    }, SEASON_CREATED_TOAST, { timeout: 20000 })
      .catch(() => fail(`[${L}/2a] the card's own primary action did not `
        + `complete a real write ("${SEASON_CREATED_TOAST}" never appeared)`));
    const controlWrites = await liveWrites(page);
    // `indexOf`, not equality: #toast-root's own markup carries the dismiss
    // control beside the sentence, so the region's textContent is
    // "Season created. ×".
    const heardToast = spokenWrites(controlWrites, "toast-root")
      .filter((w) => w.text.indexOf(SEASON_CREATED_TOAST) !== -1);
    if (!heardToast.length) {
      fail(`[${L}/2a] the live-region ledger did not record a real production `
        + `toast, so every "#toast-root never spoke" assertion in this file is `
        + `unfalsifiable. Recorded: ${JSON.stringify(controlWrites.slice(-8))}`);
    }

    // ============= LEG 2b — A LOWER-PRIVILEGE AUTHORIZED ROLE =============
    // Arena Manager holds MANAGE_ARENA and is authorized on this very tuple,
    // but the work this Program needs is MANAGE_SETUP. It must be told, not
    // handed a control it cannot execute.
    await loginAs(page, "arena", "demo", `${L}/2b`);
    await reenter(page, base);
    await reachDashboard(page, L, "2b-arrive");
    await armLiveRegions(page);
    await settled(page, `${L}/2b-arrive`);
    await switchContext(page, fx.empty, null, L, "2b");
    await settled(page, `${L}/2b`);
    const arena = await homeCard(page);
    if (!arena.hasSlot) {
      fail(`[${L}/2b] the Arena Manager has no Home/Tasks slot at all, so `
        + `"no controls" below would be vacuous`);
    }
    if (arena.state !== "ready"
        || JSON.stringify(arena.headings) !== JSON.stringify([H_READY])) {
      fail(`[${L}/2b] the Arena Manager's card: ${arena.state} `
        + `${JSON.stringify(arena.headings)}`);
    }
    // The mutation control is GONE for this role, on the SAME tuple where the
    // League Admin had it ENABLED (leg 1b, adminOnEmpty).
    if (arena.buttons.length !== 0) {
      fail(`[${L}/2b] an Arena Manager blocked on MANAGE_SETUP work was offered `
        + `${arena.buttons.length} control(s): ${JSON.stringify(arena.buttons)}`);
    }
    if (!adminOnEmpty.buttons.length || adminOnEmpty.buttons[0].disabled
        || adminOnEmpty.identityProgram !== arena.identityProgram) {
      fail(`[${L}/2b] ANTI-VACUITY FAILED: the League Admin did not have an `
        + `enabled control on this same Program, so the Arena Manager's `
        + `"no control" is not a difference. admin=${JSON.stringify(adminOnEmpty.buttons)} `
        + `adminProgram=${adminOnEmpty.identityProgram} `
        + `arenaProgram=${arena.identityProgram}`);
    }
    // …and it is replaced by EXPLICIT GUIDANCE naming the workflow and the
    // blocker, not by silence.
    if (arena.slotText.indexOf(ARENA_BLOCKED_DETAIL) === -1
        || arena.slotText.indexOf(ARENA_WORKFLOW_LABEL) === -1) {
      fail(`[${L}/2b] the blocked lower-privilege role got no explicit guidance: `
        + `${arena.slotText}`);
    }
    // The redaction is real: League-Admin-only workflows are not in this
    // role's list at all.
    if (arena.rows.length !== 1 || arena.rows[0].title !== ARENA_WORKFLOW_LABEL) {
      fail(`[${L}/2b] the Arena Manager's workflow list is not redacted to what `
        + `MANAGE_ARENA manages: ${JSON.stringify(arena.rows)}`);
    }
    if (arena.rows.length >= adminOnEmpty.rows.length) {
      fail(`[${L}/2b] the Arena Manager sees as many workflows as the League `
        + `Admin (${arena.rows.length} vs ${adminOnEmpty.rows.length}), so the `
        + `redaction assertion above is measuring nothing`);
    }
    await assertSentenceAppearsOnce(page, ARENA_BLOCKED_DETAIL, L, "2b");

    // (2b') THE OTHER EMPTY REASON, and the exact focus target after a
    //       user-initiated retry resolves into it. On the fully set-up Program
    //       this role's only visible workflow is already done, so the card
    //       resolves EMPTY/"nothing_actionable" — a DIFFERENT claim from
    //       "no_program" (leg 1h) and carrying different copy.
    await armLiveRegions(page);
    await switchContext(page, demo.program, demo.season, L, "2b'");
    await settled(page, `${L}/2b'`);
    const arenaEmpty = await homeCard(page);
    if (arenaEmpty.state !== "empty" || arenaEmpty.reason !== "nothing_actionable") {
      fail(`[${L}/2b'] expected the named EMPTY/"nothing_actionable" state, got `
        + `${arenaEmpty.state}/${arenaEmpty.reason}`);
    }
    if (!arenaEmpty.hasSlot) {
      fail(`[${L}/2b'] the Arena Manager has no Home/Tasks slot at all here, so `
        + `every assertion about what this state renders would be vacuous`);
    }
    if (!arenaEmpty.slotHtml) {
      fail(`[${L}/2b'] EMPTY/"nothing_actionable" rendered NOTHING at all, so a `
        + `role whose visible slice has nothing left to do is told nothing`);
    }
    if (JSON.stringify(arenaEmpty.headings) !== JSON.stringify([H_EMPTY_NOTHING])) {
      fail(`[${L}/2b'] EMPTY/"nothing_actionable" must carry its own stable `
        + `semantic heading "${H_EMPTY_NOTHING}": `
        + `${JSON.stringify(arenaEmpty.headings)}`);
    }
    if (arenaEmpty.emptyStatus !== EMPTY_NOTHING_STATUS) {
      fail(`[${L}/2b'] EMPTY/"nothing_actionable" must carry its own status `
        + `text "${EMPTY_NOTHING_STATUS}"; it carried `
        + `${JSON.stringify(arenaEmpty.emptyStatus)}`);
    }
    // REASON-SPECIFIC, and explicitly NOT an overclaim about the whole Program.
    if (arenaEmpty.slotText.indexOf(EMPTY_NOTHING_EXPLAIN) === -1) {
      fail(`[${L}/2b'] EMPTY/"nothing_actionable" does not explain that this is `
        + `a claim about THIS ROLE's slice only: ${arenaEmpty.slotText}`);
    }
    if (arenaEmpty.slotText.indexOf(EMPTY_NO_PROGRAM_STATUS) !== -1) {
      fail(`[${L}/2b'] EMPTY/"nothing_actionable" is rendering `
        + `EMPTY/"no_program"'s copy, so the two named reasons have been `
        + `collapsed into one: ${arenaEmpty.slotText}`);
    }
    // ROLE-CORRECT USABLE ACTION COUNT: by construction there is nothing this
    // role can act on, so this state offers GUIDANCE AND NO CONTROL — never a
    // button that cannot resolve the state it is standing in.
    if (arenaEmpty.buttons.length !== 0) {
      fail(`[${L}/2b'] a state whose whole claim is "nothing left for your `
        + `role to do" offered ${arenaEmpty.buttons.length} control(s): `
        + `${JSON.stringify(arenaEmpty.buttons)}`);
    }
    await assertSentenceAppearsOnce(page, H_EMPTY_NOTHING, L, "2b'");
    await assertSentenceAppearsOnce(page, EMPTY_NOTHING_STATUS, L, "2b'");
    await assertNoNestedEcho(page, L, "2b'");
    assertCardSpokeOnce(await liveWrites(page), H_EMPTY_NOTHING, L, "2b'");

    // ERROR -> KEYBOARD RETRY -> EMPTY, and where focus ends up.
    await armLiveRegions(page);
    failNextProgress(503);
    await renderDashboardAgain(page, L, "2b'-fail");
    await settled(page, `${L}/2b'-fail`);
    await tabTo(page, "#sp-card-slot [data-setup-progress-retry]", L, "2b'-retry");
    await page.keyboard.press("Enter");
    await settled(page, `${L}/2b'-retry`);
    const afterEmptyRetry = await homeCard(page);
    if (afterEmptyRetry.state !== "empty"
        || afterEmptyRetry.reason !== "nothing_actionable") {
      fail(`[${L}/2b'] the retry was supposed to resolve EMPTY/`
        + `"nothing_actionable", got ${afterEmptyRetry.state}/`
        + `${afterEmptyRetry.reason}`);
    }
    if (afterEmptyRetry.focusIsBody) {
      fail(`[${L}/2b'] a keyboard-activated Retry DROPPED FOCUS ON <body> — Tab `
        + `restarts from the top of the document after an action the operator `
        + `deliberately took`);
    }
    // THE EXACT TARGET: the empty state's OWN heading, not the live-region
    // wrapper. Focusing #sp-card-slot was the earlier half-fix the #365 review
    // rejected: the wrapper carries no text, no heading and no name, so focus
    // was off <body> and still nowhere a person could perceive.
    if (afterEmptyRetry.focusIsSlot) {
      fail(`[${L}/2b'] focus landed on the bare #sp-card-slot wrapper, which `
        + `carries no heading, no status text and no accessible name — a `
        + `keyboard operator cannot tell that Retry completed`);
    }
    if (!afterEmptyRetry.focusIsCardHeading
        || afterEmptyRetry.focusName !== H_EMPTY_NOTHING) {
      fail(`[${L}/2b'] focus after a Retry that resolves EMPTY must land on the `
        + `empty state's own heading ("${H_EMPTY_NOTHING}"); it is on `
        + `${afterEmptyRetry.focus}`);
    }
    if (afterEmptyRetry.focus.indexOf("tabindex=-1") === -1) {
      fail(`[${L}/2b'] the empty-state heading was focused without the `
        + `tabindex="-1" focusCardTarget() stamps on a non-focusable `
        + `destination: ${afterEmptyRetry.focus}`);
    }
    // VISIBLE, with a real box. A named target that renders zero-height is the
    // same non-destination by another route.
    if (!afterEmptyRetry.focusBox || afterEmptyRetry.focusBox.w <= 0
        || afterEmptyRetry.focusBox.h <= 0) {
      fail(`[${L}/2b'] the focus target has no perceivable box: `
        + `${JSON.stringify(afterEmptyRetry.focusBox)}`);
    }
    // …and it is STILL the focus target a beat after settled()'s own delayed
    // focus check, so a later repaint cannot quietly take it back.
    await page.waitForTimeout(1200);
    const stillOnEmptyHeading = await homeCard(page);
    if (!stillOnEmptyHeading.focusIsCardHeading
        || stillOnEmptyHeading.focusName !== H_EMPTY_NOTHING) {
      fail(`[${L}/2b'] focus did not REMAIN on the empty-state heading: it `
        + `moved to ${stillOnEmptyHeading.focus}`);
    }

    // ===================== LEG 2c — THE UNAUTHORIZED ROLE =================
    // The battery is run for the League Admin FIRST, on the same page and the
    // same fixture, so every Viewer negative below is a difference and not a
    // missing container.
    await loginAs(page, "admin", "demo", `${L}/2c-control`);
    await reenter(page, base);
    await reachDashboard(page, L, "2c-control");
    await armLiveRegions(page);
    await settled(page, `${L}/2c-control`);
    // The fully-populated tuple on purpose: the Setup surfaces are only fully
    // actionable with a Season resolved, and a control case that is itself
    // half-withdrawn would understate what the Viewer is being denied.
    await switchContext(page, demo.program, demo.season, L, "2c-control");
    await settled(page, `${L}/2c-control2`);
    const adminHome = await mutationControls(page, MUTATION_CONTROLS);
    if (!adminHome.hasHomeSlot || !adminHome.enabled.length) {
      fail(`[${L}/2c] ANTI-VACUITY FAILED on the Dashboard: the League Admin `
        + `must have at least one ENABLED mutation control for the Viewer's `
        + `absence to mean anything: ${JSON.stringify(adminHome)}`);
    }
    await page.click('.tab[data-tab="setup"]:not([data-setup-workflow-nav])');
    await page.waitForFunction(() => document.body.dataset.view === "setup",
      null, { timeout: 20000 })
      .catch(() => fail(`[${L}/2c] the League Admin could not reach Setup`));
    await page.waitForSelector('[data-setup-workflow="facilities"]', { timeout: 20000 })
      .catch(() => fail(`[${L}/2c] the Setup hub never offered a workflow to open`));
    const adminHub = await mutationControls(page, MUTATION_CONTROLS);
    if (!adminHub.setupCardSlots || !adminHub.workflowOpen) {
      fail(`[${L}/2c] ANTI-VACUITY FAILED on the Setup hub: the League Admin `
        + `must be given the hub's cards and its open controls: `
        + `${JSON.stringify(adminHub)}`);
    }
    // …and one landing, where the Setup surfaces' real MUTATION controls live.
    await page.click('[data-setup-workflow="facilities"]');
    await page.waitForSelector('[data-setup-landing-actions="facilities"] button',
      { timeout: 20000 })
      .catch(() => fail(`[${L}/2c] the Facilities landing never offered its own `
        + `action, so the Viewer's "no mutation control on Setup" would be `
        + `measured against nothing`));
    const adminLanding = await mutationControls(page, MUTATION_CONTROLS);
    if (!adminLanding.enabled.length) {
      fail(`[${L}/2c] ANTI-VACUITY FAILED on the Setup landing: the League `
        + `Admin has no ENABLED mutation control there: `
        + `${JSON.stringify(adminLanding)}`);
    }

    await loginAs(page, "viewer", "demo", `${L}/2c`);
    await reenter(page, base);
    await armLiveRegions(page);
    await page.waitForTimeout(1500);
    const viewerLanding = await mutationControls(page, MUTATION_CONTROLS);
    if (!viewerLanding.contentPainted) {
      fail(`[${L}/2c] the Viewer's landing rendered nothing at all, so every `
        + `assertion below would be about a blank page`);
    }
    if (viewerLanding.visibleTabs.indexOf("setup") !== -1) {
      fail(`[${L}/2c] a Viewer is offered the Setup tab: `
        + `${JSON.stringify(viewerLanding.visibleTabs)}`);
    }
    if (viewerLanding.hasHomeSlot) {
      fail(`[${L}/2c] a Viewer was rendered the Home/Tasks card`);
    }
    // Direct navigation, through the app's OWN switchTab() — the way a curious
    // console user reaches a view whose tab is hidden. Neither destination may
    // materialise a mutation control.
    for (const view of ["dashboard", "setup"]) {
      await page.evaluate((v) => switchTab(v), view);
      await page.waitForFunction((v) => document.body.dataset.view === v, view,
        { timeout: 20000 })
        .catch(() => fail(`[${L}/2c] switchTab("${view}") never took for a Viewer`));
      await page.waitForTimeout(1200);
      const forced = await mutationControls(page, MUTATION_CONTROLS);
      if (!forced.contentPainted) {
        fail(`[${L}/2c] the Viewer's forced "${view}" rendered nothing, so the `
          + `"no controls" assertion would be vacuous`);
      }
      if (forced.hasHomeSlot) {
        fail(`[${L}/2c] forcing "${view}" gave a Viewer the Home/Tasks card`);
      }
      if (forced.total !== 0 || forced.enabled.length !== 0) {
        fail(`[${L}/2c] forcing "${view}" gave a Viewer ${forced.total} mutation `
          + `control(s), ${forced.enabled.length} of them enabled: `
          + `${JSON.stringify(forced.enabled)}`);
      }
      if (view === "setup") {
        if (forced.setupCardSlots !== 0 || forced.workflowGo !== 0
            || forced.workflowOpen !== 0) {
          fail(`[${L}/2c] the Viewer's forced Setup surface painted `
            + `${forced.setupCardSlots} card slot(s), ${forced.workflowGo} `
            + `landing control(s) and ${forced.workflowOpen} hub open `
            + `control(s), where the League Admin had `
            + `${adminHub.setupCardSlots}/${adminLanding.enabled.length}/`
            + `${adminHub.workflowOpen}`);
        }
        // EXPLICIT GUIDANCE INSTEAD, which is the other half of the
        // requirement — an unauthorized role must be told, not left blank.
        if (forced.contentText.indexOf(VIEWER_SETUP_GUIDANCE) === -1) {
          fail(`[${L}/2c] the Viewer's Setup surface offers no controls AND no `
            + `explanation: ${forced.contentText}`);
        }
      }
    }
    // The server refuses too, so this is data protection and not UI hiding.
    const viewerProbe = await page.evaluate(async () => {
      const r = await fetch("/api/v2/setup/progress", { credentials: "same-origin" });
      return { status: r.status, body: await r.json() };
    });
    declareExpected("GET", `${base}/api/v2/setup/progress`, 403,
      "the Viewer's own refused read");
    if (viewerProbe.status !== 403
        || !viewerProbe.body.error
        || viewerProbe.body.error.details.required !== "manage_arena") {
      fail(`[${L}/2c] the Home/Tasks read must be refused for a Viewer by the `
        + `SERVER, not merely hidden by the client: ${JSON.stringify(viewerProbe)}`);
    }
    const viewerSpoke = spokenWrites(await liveWrites(page), "sp-card-slot");
    if (viewerSpoke.length) {
      fail(`[${L}/2c] something wrote into a Home/Tasks live region for a role `
        + `that has no Home/Tasks card: ${JSON.stringify(viewerSpoke)}`);
    }

    // ======= LEG 1i — CONFIRM AND PENDING ARE STRUCTURALLY UNREACHABLE ====
    // Asserted from the whole run's evidence rather than from one sample.
    const methods = Array.from(progressMethods).sort();
    if (JSON.stringify(methods) !== JSON.stringify(["GET"])) {
      fail(`[${L}/1i] this card issued a non-GET against its own endpoint, so `
        + `it is no longer a pure read and the CONFIRM/PENDING states this `
        + `journey declares unreachable may now be reachable: `
        + `${JSON.stringify(methods)}`);
    }
    const ledger = await page.evaluate(() => Object.keys(cardWrites).sort());
    if (ledger.indexOf("home/setup-progress") !== -1) {
      fail(`[${L}/1i] the Home/Tasks card registered an unresolved WRITE in the `
        + `serialization ledger: ${JSON.stringify(ledger)}`);
    }
    for (const seen of [empty, loading, afterLoad, ready, errored, retried,
                        staleSampled, arena, arenaEmpty]) {
      if (seen.confirmMarkup) {
        fail(`[${L}/1i] confirmation/pending markup appeared in the Home/Tasks `
          + `slot (${seen.confirmMarkup} node(s)) in state ${seen.state}`);
      }
    }

    // Every failed delivery this viewport saw, matched against every failure
    // this file deliberately caused, ONCE — see reconcileDeliveries().
    errors.push(...reconcileDeliveries(nonOk, declared, consoleErrors, failedReqs));
    if (errors.length) fail(`[${L}] browser errors:\n${errors.join("\n")}`);

    console.log(`[${L}] OK — the Home/Tasks card renders its six applicable `
      + `states through real production entry points with forced transport `
      + `outcomes: a held read shows the LOADING skeleton under its own `
      + `heading and aria-busy while the rest of the Dashboard stays painted; `
      + `a Program with no data settles READY listing every missing workflow `
      + `with its own status text and exposing exactly ONE enabled authorized `
      + `primary action; a forced 503 settles ERROR with its own heading, `
      + `alert sentence and one scoped Retry, leaving every other Dashboard `
      + `card identical; a bounded Tab traversal reaches that Retry with a `
      + `real focus indicator and Enter issues exactly one request — this `
      + `card's own read, nothing outside it repainted by a byte — landing `
      + `focus on the recovered card's own heading (and on the ERROR heading `
      + `when the retry fails again, and on the EMPTY state's own visible, `
      + `named heading when it resolves there), while a routine render-driven `
      + `load moves no focus at all; a context switch shows STALE with the `
      + `retained rows intact, aria-busy still true, the obsolete primary `
      + `action WITHDRAWN and only a ghost refresh left; and a keyboard-driven `
      + `recovery into COMPLETE lands on the completion heading. Across every `
      + `one of those transitions the card announces through its OWN polite `
      + `region and the sitewide #toast-root stays silent — proven non-vacuous `
      + `by the same ledger recording a real production toast from the card's `
      + `own primary action, activated by keyboard, seeded from the active `
      + `Program and completing a real write. A held-but-genuine server `
      + `success released after a newer failure, after a full context switch, `
      + `and a held failure released after a newer success all change nothing `
      + `by snapshot, restore no obsolete action and announce nothing, with `
      + `each release observed at the network first. On the same tuple a `
      + `League Admin has an enabled "Add Season" while an Arena Manager has `
      + `ZERO controls, a workflow list redacted to MANAGE_ARENA and explicit `
      + `guidance naming the blocker, and on the fully set-up Program resolves `
      + `EMPTY/"nothing_actionable" with its own heading, status sentence and `
      + `role-scoped explanation and ZERO controls — while a League Admin on a `
      + `store with no Program at all gets EMPTY/"no_program" with its own `
      + `different heading, status sentence and the single genuinely `
      + `authorized path that resolves it; a Viewer gets no Home/Tasks card, no `
      + `Setup tab, zero mutation controls on either surface even when both `
      + `views are forced through the app's own switchTab(), explicit guidance `
      + `on Setup, and a server-side 403 on the read itself — each negative `
      + `measured against the identical battery that returns enabled controls `
      + `for the League Admin in the same fixture. CONFIRM and PENDING are `
      + `proven unreachable rather than claimed: every request to this card's `
      + `endpoint in the whole run was a GET, the write ledger never gained `
      + `this card, and no confirmation or pending markup ever appeared. `
      + `Every failed delivery is reconciled by exact method, URL and status.`);
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
  // Before anything else: the allowance is not a counter and cannot be spent
  // on somebody else's failure. Deterministic, browser-free, every invocation.
  selfTestDeliveryReconciler();
  const browser = await chromium.launch();
  try {
    for (const viewport of VIEWPORTS) await checkViewport(browser, viewport);
    console.log("Home/Tasks state-matrix browser journey passed.");
  } catch (e) {
    console.error("Home/Tasks state-matrix browser journey FAILED.");
    console.error(e.message);
    process.exitCode = 1;
  } finally {
    await browser.close();
  }
})();
