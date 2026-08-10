// Facilities venue-access gate (#365 review) — the Facilities card must treat
// "a Rink is VISIBLE" and "a Rink is SCHEDULABLE this Season" as different
// claims.
//
// THE DEFECT THIS EXISTS FOR, verbatim from the review: "Facilities treats a
// visible historical/ungranted Rink as schedulable and offers a dead-end Add
// Ice primary." The card's prerequisite chain asked only whether any Venue and
// Rink appeared in the scoped setup overview. That overview's contract
// DELIBERATELY includes revoked-grant history (so the cleanup section can name
// the row) and creator-owned pending rows (so create-then-link works) — both
// correct reads. So a Venue+Rink whose grant to the selected Season had been
// revoked settled READY with `blockedBecause: null` and a sole "Add Ice"
// primary, while `get_setup_progress` independently computed ZERO schedulable
// rinks and the Ice Availability Builder would refuse every one of them with
// `venue_access_missing` and generate zero slots.
//
// The fix makes "at least one Rink reachable through ACTIVE SeasonVenueAccess
// for this exact selected Season" an asserted per-workflow prerequisite on
// /api/v2/setup/progress, bound into the Facilities card model under the same
// identity (card id + context tuple + generation) as everything else. This
// journey is the regression for that, at desktop and canonical 390x844.
//
// WHAT IT ASSERTS, and why each leg is a real risk rather than markup presence:
//
//  (A) REVOKED GRANT — Venue + Rink, granted to the selected Season and then
//      revoked. The reviewer's own reproduction.
//  (B) CREATOR-OWNED PENDING — Venue + Rink that never had a grant at all,
//      reaching the payload through `pending_link_*`. A different code path
//      into the same visibility, so it needs its own fixture.
//
//  In BOTH, for BOTH roles:
//    * EXACT TUPLE IDENTITY — the committed model's identity is this card,
//      this Program, this Season, this League. Without it the assertions below
//      could be satisfied by a model bound to a neighbouring tuple's inventory,
//      which is precisely what the per-card identity discipline exists to stop.
//    * NON-VACUOUS VISIBLE COUNTS/HISTORY — the card's own Venues and Rinks
//      counts are NON-ZERO and the rows are really in the scoped overview
//      (and, for (A), the revoked grant is really in the Season's venue-access
//      history). This is what keeps the whole journey from passing against a
//      card that is merely EMPTY: an empty Facilities card also offers no "Add
//      Ice", and would prove nothing.
//    * NO "Add Ice" CONTROL AND NO ICE-PREVIEW REQUEST — not only that the
//      button is absent, but that the page issues no
//      /api/setup/ice-availability/preview at all. A withdrawn control that
//      still let some other path reach the builder would be the same dead end
//      one click further along.
//    * ROLE-CORRECT GUIDANCE/ACTION — League Admin (holds MANAGE_SETUP) gets
//      the REAL venue-access resolution path: exactly one control, which lands
//      on the selected Season's own "Allowed venues" picker with focus on it.
//      Arena Manager (MANAGE_ARENA, and explicitly NOT able to grant Season
//      venue access) gets NO mutation control at all plus explicit guidance
//      that a League Admin must set it up. Offering an Arena Manager a grant
//      action would just be a second dead end.
//
//  (C) RECOVERY — the grant is then made through that real entry point (the
//      Allow picker, not a raw fetch), and the SAME card advances to "Add Ice"
//      with its demoted actions restored, with NO page reload; then the card's
//      OWN refresh path is exercised and must leave every adjacent card's
//      generation and committed model untouched. Without this leg the
//      withdrawal above could pass by withdrawing "Add Ice" forever.
//
//  (D) THE DESTINATION SETTLES LATE (#365 review round 11). The recovery
//      action's promise is that a keyboard/screen-reader operator lands ON the
//      control that grants venue access. It used to be kept by a CLOCK: poll
//      for the picker for 200x50ms (10s) while a skeleton was up, then hand
//      off to focusContentHeading(), whose own 40x50ms poll ends in an
//      unconditional #content landing -- ~12s, after which the operator was
//      silently left at the page region instead. That is not a test quirk: it
//      was recorded failing 1 run in 15 at 390px, and any budget loses under
//      enough load.
//      So this leg makes the destination settle WELL PAST that budget: the
//      hierarchy, venue-access and venue-candidate reads are each held (real
//      response captured first, then released) so the picker cannot appear for
//      ~15s. It then proves, with NO test-side focus action anywhere, that
//      focus eventually lands on the exact selected Season's picker, that it
//      NEVER settles on #content at any point during the wait (sampled
//      continuously AND via every focusin event, not merely checked at the
//      end), and that the picker really did appear after the old budget would
//      have expired -- so the leg cannot pass vacuously against a fast render.
//
//  (E) A SUPERSEDED CONTEXT CANCELS IT. The same delayed window, but the
//      operator switches Season in the real context switcher while the
//      destination is still loading. The intent registered under the old tuple
//      must be CANCELLED and focus NOTHING: not the Season it was registered
//      for, and not the one that arrives in its place. Asserted over the whole
//      window -- no venue-access picker is ever focused at all -- and the
//      intent record itself must be gone.
//
//  (F) A STALE GENERIC POLL MUST NOT OUTLIVE THE DEEP LINK (#365 review
//      round 12). Reaching the Facilities landing goes through
//      openSetupWorkflowLanding(), which starts focusContentHeading()'s own
//      poll -- 40 x 50ms, ending in an unconditional #content landing. That
//      poll used to keep running after the operator activated the recovery
//      action, and then land focus on behalf of the navigation they had
//      already left: it ticked once more after the deep link had put focus on
//      the Allow picker and moved it to #content, permanently, because the
//      intent was by then spent. Traced verbatim while this leg was written
//      (nav at 19ms, landing painted at 38ms, activation at 60ms, intent kept
//      at 89ms, the old poll's next tick at 121ms taking #content), and it is
//      the SAME recorded failure legs (D)/(E) were built for -- they only
//      avoid it because they drain that poll first, which production cannot.
//      So this leg drains nothing. It activates the recovery action from
//      INSIDE the page, in the microtask checkpoint of the very DOM mutation
//      that paints the control, so no 50ms tick can possibly have run between
//      the landing appearing and the activation -- the older poll is
//      PROVABLY still pending (asserted: nothing had been focused yet, and
//      less than the poll's whole 2s life had elapsed) -- and then proves the
//      stale fallback never fires: focus reaches the picker and is still
//      there after the entire poll budget has gone by, with #content never
//      focused once.
//      Its second half is the other side of the same rule: with the
//      destination's reads held back and NO newer request anywhere, the very
//      same poll must still take its #content floor. That floor closed a real
//      CI failure ("focus restore (removed trigger): focus was left on <body>
//      instead of the view fallback") and supersession must not cost it.
//
//  (G) ...AND MUST NOT CROSS A CONTEXT BOUNDARY — asserted over the window
//      BEFORE /api/context answers (#365 review round 13). setActiveContext()
//      exposes the operator's new Season in the native control and starts the
//      POST; until that POST answers, contextOptions.selected still holds the
//      OLD tuple, so a standing intent and an in-flight generic poll both
//      still read as current. That interval is the defect, and it is exactly
//      the interval a leg keyed on contextOptions.selected moving cannot see.
//      So this leg HOLDS POST /api/context before forwarding it, for longer
//      than the poll's entire 40 x 50ms life, and opens its observation at the
//      REAL `change` event on #ctx-select (capturing listener, so it is
//      recorded before the select's own onchange calls setActiveContext).
//      Across that whole pre-response window -- proven to be pre-response: the
//      POST is still held, contextSwitchInFlight is set and the canonical
//      tuple still names the departing Season -- NOTHING may be focused:
//      not #content, not a heading of either shape, not the old Season's Allow
//      picker, not the new Season's. Then the request is RELEASED and the same
//      silence must continue past another whole poll life. The poll's
//      pendency at the change is proven rather than assumed (nothing focused
//      yet, focus still on the nav control, skeleton still up, inside the 2s
//      life), and the crossing is armed in the microtask checkpoint of the
//      navigation's own first paint so no tick can have run before it.
//
//  (G2) ...AND THE SAME WHEN THE SWITCH ONLY QUEUES (#365 review round 14).
//      (G) issues one switch while nothing is in flight, so it never takes
//      setActiveContext()'s `if (contextSwitchInFlight) ... return` early
//      return and cannot see where the abandonment sits relative to it. This
//      leg reverses the order: the operator picks the SECOND Season and its
//      POST is held, THEN the Facilities navigation starts the generic poll
//      inside that in-flight window, THEN the operator picks the THIRD Season
//      -- armed in the same first-paint microtask -- so that call queues and
//      returns having sent nothing. The queue is read from the page
//      (contextSwitchQueued names the third Season, contextSwitchInFlight is
//      set, the canonical tuple is still the departing one, and the route has
//      intercepted exactly one POST), and across the whole window from that
//      second change event nothing is focused and focus does not move at all.
//      Both switches are then released and the silence must continue.
//
//  (H) ...AND MUST NOT CROSS AN IDENTITY BOUNDARY — the same shape, crossed
//      through the app's own no-reload signIn() to a different principal.
//      That boundary is synchronous, so it is watched from the confirmed
//      crossing onward, with the same continuous sampling.
//
// Fails on any browser console/page error.
const { chromium } = require("playwright");
const { spawn } = require("child_process");
const http = require("http");
const path = require("path");
const {
  installContextFixture, selectProgramSeason,
} = require("./context-fixture.js");

const HOST = "127.0.0.1";
const BACKEND_DIR = path.resolve(__dirname, "..", "backend");
const READY_TIMEOUT_MS = 15000;
const VIEWPORTS = [
  { label: "desktop", width: 1440, height: 900, port: 8391 },
  { label: "phone", width: 390, height: 844, port: 8392 },
];

// The one route that proves a dead end was actually reachable. "Add Ice" opens
// the Ice Availability Builder, whose preview POST is the first thing it does
// with a rink selection.
const ICE_PREVIEW = "/api/setup/ice-availability/preview";

// ---- (D)/(E): making the destination settle LATE ------------------------
//
// THE BUDGET THIS HAS TO OUTLAST, measured from the code it replaced:
// focusVenueAccessControl() polled 200 x 50ms = 10000ms while a `.skeleton`
// was still up, then called focusContentHeading(), which polls 40 x 50ms =
// 2000ms and then focuses #content UNCONDITIONALLY. 12000ms combined, after
// which the deep link had silently become "somewhere in the page region".
const OLD_COMBINED_BUDGET_MS = 12000;
// The Setup hierarchy destination reads these three in sequence before it can
// paint a picker, so holding each one back 5.2s puts the picker ~15.6s out --
// comfortably past the budget above, with no reliance on machine speed.
const LATE_READ_MS = 5200;
const HIERARCHY_READS =
  /\/api\/v2\/setup\/(hierarchy|seasons\/[^/?]+\/venue-(access|candidates))(\?|$)/;

// Hold the destination's own reads. The REAL response is captured first
// (route.fetch()), held, and only then fulfilled -- so the payload the app
// finally renders is the server's genuine one and this leg is a timing
// change, not a fixture substitution.
async function delayHierarchyReads(page) {
  await page.route(HIERARCHY_READS, async (route, request) => {
    if (request.method() !== "GET") return route.continue();
    try {
      const response = await route.fetch();
      await new Promise((r) => setTimeout(r, LATE_READ_MS));
      await route.fulfill({ response });
    } catch (e) {
      // A read still being HELD when the page moves on (the next leg's
      // reenter(), or the route being lifted at the end of this one) can no
      // longer be answered — Playwright has already handled it. That is a
      // property of deliberately holding a request for seconds, not a product
      // signal, and it must not surface as an unhandled rejection that takes
      // the whole runner down mid-leg. Anything the app itself did wrong still
      // reaches the pageerror/console listeners.
    }
  });
}

// Focus OBSERVATION, never focus action. Two independent records, because
// "focus never settles on #content" is a claim about the whole wait and not
// about where it happens to be when the test looks: a 40ms sampler walks the
// entire window, and a capturing focusin listener catches every transition
// even if it lasted less than one sample.
async function startFocusTrace(page) {
  await page.evaluate(() => {
    window.__vagFocus = { t0: Date.now(), samples: [], events: [] };
    const describe = (el) => ({
      id: (el && el.id) || null, tag: (el && el.tagName) || null,
      // focusContentHeading()'s heading selector is "h1, h2, h3,
      // .section-title", and a .section-title need not be a heading ELEMENT --
      // so the tag alone cannot see that exit. Recorded for leg (G), which has
      // to rule out every landing the stale poll can take.
      cls: (el && typeof el.className === "string") ? el.className : null,
      t: Date.now() - window.__vagFocus.t0 });
    window.__vagFocusTimer = setInterval(
      () => window.__vagFocus.samples.push(describe(document.activeElement)), 40);
    window.__vagFocusListener = (e) => window.__vagFocus.events.push(describe(e.target));
    document.addEventListener("focusin", window.__vagFocusListener, true);
  });
}

async function readFocusTrace(page) {
  return page.evaluate(() => ({
    samples: window.__vagFocus.samples, events: window.__vagFocus.events }));
}

// Wait until focus has been sitting still for longer than focusContentHeading's
// entire poll (40 x 50ms), so nothing it started earlier is still in flight.
//
// THIS IS ABOUT THE SETUP STEP, NOT THE SUBJECT. Reaching the Facilities
// landing goes through openSetupWorkflowLanding(), which calls
// focusContentHeading() on its own account -- and that helper's #content floor
// is a DIFFERENT accepted fix (it closed a real CI failure where a slow render
// left focus on <body>), deliberately left alone here. On a loaded machine the
// landing's own render outruns that poll, so the floor fires ~2s after the nav
// click -- which can land inside the leg below and would be attributed to the
// deep link. Draining it first is what keeps the leg's #content claim a claim
// about the deep link.
async function quiesceFocus(page) {
  await page.evaluate(() => { window.__vagQuiesce = null; });
  await page.waitForFunction(() => {
    const a = document.activeElement;
    const key = `${(a && a.id) || ""}/${a && a.tagName}`;
    if (window.__vagQuiesce && window.__vagQuiesce.key === key) {
      return Date.now() - window.__vagQuiesce.since > 2400;
    }
    window.__vagQuiesce = { key, since: Date.now() };
    return false;
  }, null, { timeout: 30000 });
}

async function stopFocusTrace(page) {
  await page.evaluate(() => {
    clearInterval(window.__vagFocusTimer);
    document.removeEventListener("focusin", window.__vagFocusListener, true);
  });
}

// The standing intent, read from the app's own state rather than inferred.
async function readFocusIntent(page) {
  return page.evaluate(() => destinationFocusIntent && {
    view: destinationFocusIntent.view, setupView: destinationFocusIntent.setupView,
    epoch: destinationFocusIntent.epoch, principal: destinationFocusIntent.principal,
    program_id: destinationFocusIntent.program_id,
    season_id: destinationFocusIntent.season_id,
    league_id: destinationFocusIntent.league_id,
  });
}

// THE WAIT ITSELF: everything from the instant the operator's activation took
// focus onward. Where focus was before that belongs to the navigation that
// reached the landing (see quiesceFocus), and this leg does not speak for it.
// Fails loudly rather than silently widening if the activation never took
// focus, since then the window would be undefined.
function focusWindow(trace, L, step) {
  const click = trace.events.find((e) => e.tag === "BUTTON");
  if (!click) {
    fail(`[${L}/${step}] the activated control never took focus, so there is no `
      + `wait to measure: ${JSON.stringify(trace.events)}`);
  }
  const from = click.t;
  return { from,
    samples: trace.samples.filter((s) => s.t >= from),
    events: trace.events.filter((e) => e.t >= from) };
}

const traceHits = (trace, pred) =>
  trace.samples.filter(pred).concat(trace.events.filter(pred));

// ---- (G)/(H): arming a BOUNDARY crossing inside the poll's own window ----
//
// Same technique as leg (F)'s arming, for the same reason and with the same
// guarantee: a MutationObserver callback is a MICROTASK of the task that
// mutated the DOM, and every tick of focusContentHeading()'s poll is a
// setTimeout MACROTASK -- so an action fired from here provably runs before
// the poll started by this very navigation can tick even once. Nothing is
// slept on and nothing is drained.
//
// The mutation it waits for is the navigation's OWN first paint: render()
// sets document.body.dataset.view and writes #content's loading skeleton
// synchronously, before its first await, and focusContentHeading() is called
// immediately after -- so at the instant this callback runs, attempt 0 has
// already seen the skeleton and scheduled attempt 1 fifty milliseconds out.
// Gated on view === "setup" AND a skeleton being present so a leftover
// mutation from the surface being left behind cannot arm it early.
//
// `kind` picks WHICH boundary is crossed, and both are crossed the way an
// operator crosses them:
//   "context"  — the real #ctx-select, its own value + change event, which is
//                the one handler the context bar has.
//   "identity" — signIn(), the exact function the login form's submit handler
//                and every demo-persona button call. No reload: a reload
//                would destroy the pending poll by destroying the document,
//                which would make the leg prove nothing.
async function armCrossingAtFirstPaint(page, kind, arg) {
  await page.evaluate(([k, a]) => {
    window.__vagCrossArm = null;
    window.__vagCrossAt = null;
    window.__vagSignInResult = null;
    const content = document.getElementById("content");
    window.__vagCrossObserver = new MutationObserver(() => {
      if (window.__vagCrossArm) return;
      if (document.body.dataset.view !== "setup") return;
      if (!content.querySelector(".skeleton")) return;
      window.__vagCrossObserver.disconnect();
      const el = document.activeElement;
      window.__vagCrossAt = el;
      window.__vagCrossArm = {
        t: Date.now() - window.__vagFocus.t0,
        kind: k,
        active: { id: (el && el.id) || null, tag: (el && el.tagName) || null },
        landing: !!document.querySelector('[data-setup-workflow-landing="facilities"]'),
        epoch: uiIdentityEpoch,
        season: ((contextOptions && contextOptions.selected) || {}).season_id || null,
        principal: currentUser ? currentUser.username : null,
      };
      if (k === "context") {
        const sel = document.getElementById("ctx-select");
        sel.value = a;
        sel.dispatchEvent(new Event("change", { bubbles: true }));
      } else {
        // The production login form discards this promise too; parked so the
        // leg can assert the sign-in genuinely succeeded rather than assuming.
        Promise.resolve(signIn(a, "demo")).then(
          (v) => { window.__vagSignInResult = v; },
          (e) => { window.__vagSignInResult = `threw: ${e && e.message}`; });
      }
    });
    window.__vagCrossObserver.observe(content, { childList: true, subtree: true });
  }, [kind, arg]);
}

// THE CROSSING ITSELF, timestamped on the page's own clock at the first
// instant the boundary is observably behind us -- contextOptions.selected
// having moved (sendContextSwitch's confirmed success path) or currentUser
// having been replaced (setUser -> resetTransientUiState). Both are read
// AFTER the app has already invalidated, so `t` is an UPPER bound on when the
// invalidation happened, which is the conservative direction for every claim
// the legs make with it.
async function waitForCrossing(page, kind, arg) {
  await page.waitForFunction(([k, a]) => {
    if (window.__vagCross) return true;
    const moved = k === "context"
      ? (((contextOptions && contextOptions.selected) || {}).season_id === a)
      : (!!currentUser && currentUser.username === a);
    if (!moved) return false;
    const el = document.activeElement;
    window.__vagCross = {
      t: Date.now() - window.__vagFocus.t0,
      active: { id: (el && el.id) || null, tag: (el && el.tagName) || null },
      atArm: el === window.__vagCrossAt,
      skeleton: !!document.querySelector("#content .skeleton"),
      landing: !!document.querySelector('[data-setup-workflow-landing="facilities"]'),
      epoch: uiIdentityEpoch,
      season: ((contextOptions && contextOptions.selected) || {}).season_id || null,
      principal: currentUser ? currentUser.username : null,
      events: window.__vagFocus.events.slice(),
    };
    return true;
  }, [kind, arg], { timeout: 30000 });
  return page.evaluate(() => window.__vagCross);
}

// ---- (G): holding /api/context, and watching from the operator's `change` --
//
// WHY waitForCrossing() ABOVE IS NOT WHERE LEG (G) OPENS ITS WINDOW (#365
// review round 13, reviewer's own finding on the round-12 leg). It begins
// judging only once contextOptions.selected has MOVED -- which is the instant
// /api/context answers, and therefore the very instant the confirmed-switch
// cancellation runs. A leg that starts looking there is blind to the entire
// interval the defect lives in: the operator has already chosen the new Season
// in the native control, the canonical tuple has NOT moved yet, and so every
// piece of focus work started under the old tuple still reads as perfectly
// current. Leg (H)'s boundary has no such gap -- setUser()/
// resetTransientUiState() move the identity synchronously -- so (H) goes on
// using waitForCrossing().
//
// (G) therefore does two things instead.
//
// FIRST, it HOLDS the switch's own POST /api/context BEFORE forwarding it. The
// request does not reach the server at all until the leg releases it, so the
// pre-response window is exactly as long as the leg chooses and is genuinely
// pre-response -- not "probably slow enough". Held comfortably past the
// generic poll's whole 40 x 50ms life, so every tick that poll could ever have
// had falls inside a window in which the switch has been ATTEMPTED and the
// server has not been told.
//
// SECOND, it opens observation at the REAL `change` event on #ctx-select,
// caught on a CAPTURING document listener so the record is taken before the
// select's own onchange property handler -- the one production wires -- has
// called setActiveContext(). That is the operator's action, and it is the
// earliest instant at which anything held under the old tuple is stale.
const CONTEXT_POST = /\/api\/context(\?|$)/;

async function holdContextSwitchPost(page) {
  const state = { seen: 0, heldAt: null, releasedAt: null };
  let open = null;
  const gate = new Promise((resolve) => { open = resolve; });
  await page.route(CONTEXT_POST, async (route, request) => {
    // Only the FIRST switch POST is held; anything the app issues afterwards
    // (the reconciliation path, a later leg) must not silently hang.
    if (request.method() !== "POST" || state.seen++) return route.continue();
    state.heldAt = Date.now();
    await gate;
    // Same reasoning as delayHierarchyReads: a held request the page has
    // already moved past cannot be forwarded, and must not crash the runner.
    try { await route.continue(); } catch (e) { state.forwardError = String(e); }
  });
  return {
    state,
    release: () => { state.releasedAt = Date.now(); open(); },
    stop: () => page.unroute(CONTEXT_POST),
  };
}

// EVERY change is recorded, not just the first: leg (G2) makes two real
// selections in one window and opens its observation at the SECOND one, so it
// needs the whole ordered list (and needs to be able to say there were exactly
// two). `__vagCtxChange` stays the FIRST record, which is what (G) reads.
async function watchContextChangeEvent(page) {
  await page.evaluate(() => {
    window.__vagCtxChange = null;
    window.__vagCtxChanges = [];
    window.__vagCtxChangeListener = (e) => {
      if (!e.target || e.target.id !== "ctx-select") return;
      const el = document.activeElement;
      const rec = {
        t: Date.now() - window.__vagFocus.t0,
        value: e.target.value,
        active: { id: (el && el.id) || null, tag: (el && el.tagName) || null },
        skeleton: !!document.querySelector("#content .skeleton"),
        landing: !!document.querySelector('[data-setup-workflow-landing="facilities"]'),
        epoch: uiIdentityEpoch,
        // Read in the CAPTURE phase, i.e. before ctx-select's own onchange has
        // run: this is the canonical tuple as it stood at the operator's
        // action, which is exactly the "has not moved yet" the defect hid in.
        season: ((contextOptions && contextOptions.selected) || {}).season_id || null,
        principal: currentUser ? currentUser.username : null,
        events: window.__vagFocus.events.slice(),
      };
      window.__vagCtxChanges.push(rec);
      if (!window.__vagCtxChange) window.__vagCtxChange = rec;
    };
    document.addEventListener("change", window.__vagCtxChangeListener, true);
  });
}

async function stopContextChangeWatch(page) {
  await page.evaluate(() => {
    document.removeEventListener("change", window.__vagCtxChangeListener, true);
  });
}

// Where focus actually ended up, and whether it was ever taken off the
// element that legitimately held it when the boundary was crossed. `armGone`
// is the honest alternative: an element the arriving surface's own render
// removed cannot still hold focus, and losing it that way is not the poll
// yanking it.
async function crossingOutcome(page) {
  return page.evaluate(() => {
    const el = document.activeElement;
    return {
      active: { id: (el && el.id) || null, tag: (el && el.tagName) || null },
      atArm: el === window.__vagCrossAt,
      armGone: !(window.__vagCrossAt && document.contains(window.__vagCrossAt)),
      signIn: window.__vagSignInResult,
    };
  });
}

// EVERY exit focusContentHeading() can ever take, as one predicate: its
// #content landing (early or at the 2s floor) and its heading landing in all
// three shapes the selector "h1, h2, h3, .section-title" admits -- an element
// carrying that CLASS is a landing even though its tag is not H1-H3, which is
// why startFocusTrace() records className at all. A boundary crossing must
// produce NONE of them, so every boundary leg ((G), (G2) and (H)) uses this
// one predicate rather than a narrower per-leg copy: a transient
// .section-title landing must not be able to pass unobserved anywhere.
const isPollLanding = (f) => f.id === "content"
  || /^H[1-3]$/.test(f.tag || "")
  || /(^|\s)section-title(\s|$)/.test(f.cls || "");

const traceRuns = (trace) => {
  const out = [];
  trace.samples.forEach((s) => {
    const key = `${s.id || ""}/${s.tag}`;
    const last = out[out.length - 1];
    if (last && last.key === key) { last.to = s.t; last.n += 1; return; }
    out.push({ key, from: s.t, to: s.t, n: 1 });
  });
  return out;
};

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

async function apiGet(page, path_) {
  return page.evaluate(async (p) =>
    (await fetch(p, { credentials: "same-origin" })).json(), path_);
}

async function loginAs(page, username, password) {
  const res = await apiPost(page, "/api/auth/login", { username, password });
  if (!res || res.error) {
    throw new Error(`login as ${username} failed: ${JSON.stringify(res)}`);
  }
}

// A fresh page load in the given context. `contextOptions` is seeded once per
// page load and never re-polled by render(), so a raw /api/context POST needs
// this to take effect — and the leftover "#ctx=" deep link has to go first, or
// bootstrap() faithfully POSTs the PREVIOUS selection straight back over it.
async function reenter(page, base) {
  await page.evaluate(() => history.replaceState(
    null, "", location.pathname + location.search));
  await page.goto(base, { waitUntil: "domcontentloaded" });
  await page.waitForSelector("#content > *", { timeout: 15000 });
  await installContextFixture(page);
}

// Open the Facilities LANDING through a real, permission-gated entry point.
// Deliberately the nav destination rather than the hub toggle: it is the same
// openSetupWorkflowLanding() transition for both roles, and it is the Arena
// Manager's own primary journey into this workflow.
async function openFacilitiesLanding(page, step) {
  await page.click('[data-setup-workflow-nav="facilities"]');
  await page.waitForSelector('[data-setup-workflow-landing="facilities"]',
    { timeout: 15000 }).catch(() => {
      throw new Error(`[${step}] the Facilities landing never rendered`);
    });
  // Actions and copy mean nothing until the card has SETTLED: LOADING
  // withdraws every action group by design, so sampling mid-flight would read
  // zero controls and blame the wrong thing.
  await page.waitForFunction(() =>
    readCardState("setup/facilities").state !== "loading", null, { timeout: 15000 });
}

// Everything the assertions below need, read from the card's own committed
// MODEL plus the DOM it produced — never re-derived in the test.
async function readFacilities(page) {
  return page.evaluate(() => {
    const root = document.querySelector('[data-setup-workflow-landing="facilities"]');
    const entry = readCardState("setup/facilities");
    const box = root && root.querySelector("[data-setup-landing-actions]");
    const note = root && root.querySelector(".swf-card-blocked, .swf-card-empty");
    const sel = (contextOptions && contextOptions.selected) || {};
    return {
      state: entry.state,
      status: entry.status || null,
      identity: entry.identity || null,
      selected: { program_id: sel.program_id || null, season_id: sel.season_id || null,
                  league_id: sel.league_id || null },
      stats: (entry.stats || []).map((s) => ({ label: s.label, n: s.n })),
      blockedBecause: entry.blockedBecause || null,
      // `undefined` (never derived) is a DIFFERENT answer from `null` (derived,
      // and this role can resolve nothing) -- the Arena Manager leg turns on
      // exactly that distinction, so it must not be flattened here.
      effective: entry.effective === undefined ? "undefined"
        : entry.effective === null ? null : entry.effective.label,
      // EVERY control the landing offers, not just the primaries.
      buttons: box ? Array.from(box.querySelectorAll("button"))
        .map((b) => b.textContent.trim()) : [],
      // The Add-Ice route by its wiring, not merely by its label: the model
      // could substitute a differently-labelled control that still opened the
      // builder.
      addIceRoutes: root ? root.querySelectorAll('[data-setup-workflow-go="facilities"]').length : -1,
      note: note ? note.textContent.replace(/\s+/g, " ").trim() : null,
    };
  });
}

function fail(msg) { throw new Error(msg); }

// The shared blocked-state contract, asserted identically for both fixtures and
// both roles. `want` carries only what genuinely differs by role.
async function assertBlocked(page, L, step, fx, want) {
  const got = await readFacilities(page);

  // -- exact tuple identity ------------------------------------------------
  const id = got.identity;
  if (!id) fail(`[${L}/${step}] the Facilities card carries no identity at all`);
  const wantTuple = { card: "setup/facilities", program_id: fx.program,
                      season_id: fx.season, league_id: null };
  for (const k of Object.keys(wantTuple)) {
    if (id[k] !== wantTuple[k]) {
      fail(`[${L}/${step}] the card's committed identity.${k} is `
        + `${JSON.stringify(id[k])}, expected ${JSON.stringify(wantTuple[k])} `
        + `— the model is bound to a different tuple than the one on screen `
        + `(identity ${JSON.stringify(id)}, selected ${JSON.stringify(got.selected)})`);
    }
  }
  if (!(id.generation > 0)) {
    fail(`[${L}/${step}] the card's identity carries no generation: ${JSON.stringify(id)}`);
  }
  if (id.program_id !== got.selected.program_id
      || id.season_id !== got.selected.season_id
      || id.league_id !== got.selected.league_id) {
    fail(`[${L}/${step}] the card's identity ${JSON.stringify(id)} disagrees with `
      + `the live context tuple ${JSON.stringify(got.selected)}`);
  }

  // -- non-vacuous: the rows really ARE visible ----------------------------
  // Without this the whole journey would pass against an EMPTY card, which
  // also offers no "Add Ice" and would prove nothing about schedulability.
  if (got.state !== "ready") {
    fail(`[${L}/${step}] the Facilities card reads "${got.state}", expected READY — `
      + `this fixture exists to assert a card with REAL records that is still `
      + `not schedulable (stats ${JSON.stringify(got.stats)})`);
  }
  if (got.status !== "todo") {
    fail(`[${L}/${step}] expected the backend to still call facilities todo, got `
      + `"${got.status}"`);
  }
  const byLabel = {};
  got.stats.forEach((s) => { byLabel[s.label] = s.n; });
  for (const label of ["Venues", "Rinks"]) {
    if (!(byLabel[label] > 0)) {
      fail(`[${L}/${step}] the card reports ${label}=${byLabel[label]} — the fixture `
        + `is supposed to make a Venue AND a Rink VISIBLE while leaving them `
        + `unschedulable; a zero count makes every assertion below vacuous `
        + `(stats ${JSON.stringify(got.stats)})`);
    }
  }
  const overview = await apiGet(page, "/api/v2/setup/overview");
  const seenVenue = [].concat(overview.venues || [], overview.pending_link_venues || [])
    .some((v) => v.id === want.venue);
  const seenRink = [].concat(overview.rinks || [], overview.pending_link_rinks || [])
    .some((r) => r.id === want.rink);
  if (!seenVenue || !seenRink) {
    fail(`[${L}/${step}] the fixture's Venue/Rink are not in the scoped overview `
      + `(venue ${seenVenue}, rink ${seenRink}) — the counts above came from `
      + `somewhere else, so this is not the visible-but-unschedulable state`);
  }

  // -- the blocker itself --------------------------------------------------
  if (!got.blockedBecause) {
    fail(`[${L}/${step}] the card settled with blockedBecause: null while no rink is `
      + `reachable through active venue access — THE fail-open this journey `
      + `exists for (stats ${JSON.stringify(got.stats)}, `
      + `buttons ${JSON.stringify(got.buttons)})`);
  }
  if (!/venue access/i.test(got.blockedBecause)) {
    fail(`[${L}/${step}] the blocker sentence does not name venue access: `
      + `"${got.blockedBecause}"`);
  }
  if (got.blockedBecause.indexOf(fx.seasonName) === -1) {
    fail(`[${L}/${step}] the blocker sentence does not name the SELECTED season `
      + `("${fx.seasonName}"), so it is not a scoped claim: "${got.blockedBecause}"`);
  }
  if (!got.note || got.note.indexOf(got.blockedBecause) === -1) {
    fail(`[${L}/${step}] the card body does not carry the model's own blocker `
      + `sentence ("${got.blockedBecause}"); read "${got.note}"`);
  }

  // -- no Add Ice control --------------------------------------------------
  if (got.addIceRoutes !== 0) {
    fail(`[${L}/${step}] the landing still wires ${got.addIceRoutes} control(s) to the `
      + `Ice Availability Builder while no rink is schedulable`);
  }
  if (got.buttons.some((b) => /add ice/i.test(b))) {
    fail(`[${L}/${step}] the landing still offers an "Add Ice" control: `
      + `${JSON.stringify(got.buttons)}`);
  }

  // -- role-correct guidance / action --------------------------------------
  if (want.action === null) {
    if (got.effective !== null) {
      fail(`[${L}/${step}] an Arena Manager, who cannot grant season venue access, `
        + `was handed the effective action ${JSON.stringify(got.effective)} — a `
        + `mutation control they cannot execute is a second dead end`);
    }
    if (got.buttons.length !== 0) {
      fail(`[${L}/${step}] an Arena Manager's blocked landing must offer NO mutation `
        + `control at all, got ${JSON.stringify(got.buttons)}`);
    }
    if (!/league admin/i.test(got.note)) {
      fail(`[${L}/${step}] an Arena Manager gets no action, so the copy must say a `
        + `League Admin has to grant access; read "${got.note}"`);
    }
  } else {
    if (got.effective !== want.action) {
      fail(`[${L}/${step}] the effective action is ${JSON.stringify(got.effective)}, `
        + `expected "${want.action}" — the real venue-access resolution path`);
    }
    if (got.buttons.length !== 1 || got.buttons[0] !== want.action) {
      fail(`[${L}/${step}] while a prerequisite is missing the landing must expose `
        + `EXACTLY the one action that resolves it, got ${JSON.stringify(got.buttons)}`);
    }
  }
  return got;
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
  page.on("console", (m) => { if (m.type() === "error") errors.push(`[console] ${m.text()}`); });
  // Recorded from the first navigation onward: "no Add Ice control" is a claim
  // about the button, "no ice-preview request" is the stronger claim that no
  // path reached the builder at all.
  let icePreviews = [];
  page.on("request", (r) => {
    if (r.url().indexOf(ICE_PREVIEW) !== -1) icePreviews.push(r.url());
  });
  const L = viewport.label;
  const noPreviewsSince = (step) => {
    if (icePreviews.length) {
      fail(`[${L}/${step}] ${icePreviews.length} ice-availability preview request(s) `
        + `were issued while the Facilities card was blocked on venue access: `
        + `${JSON.stringify(icePreviews)}`);
    }
  };

  try {
    await waitForServer(`${base}/api/health`, READY_TIMEOUT_MS);
    await page.goto(base, { waitUntil: "domcontentloaded" });
    await page.waitForSelector("#content > *", { timeout: 15000 });
    await installContextFixture(page);
    await loginAs(page, "admin", "demo");
    // A fresh boot seeds only "admin"; the other demo personas ("arena" here)
    // are UserAccount rows /api/demo/load creates. Every fixture below selects
    // its own Program explicitly, so the seeded demo data is never in scope.
    const loadStatus = await page.evaluate(() => fetch("/api/demo/load", {
      method: "POST", credentials: "same-origin",
      headers: { "Content-Type": "application/json" }, body: "{}",
    }).then((r) => r.status));
    if (loadStatus !== 200) fail(`[${L}] demo load failed (status ${loadStatus})`);

    // ================= (A) REVOKED GRANT ==================================
    // The reviewer's reproduction, built through public APIs exactly as they
    // described it: Program + active Season + Venue + Rink, grant the Venue,
    // then revoke that grant. The overview keeps reporting 1 Venue / 1 Rink as
    // history, which is correct — and none of it is schedulable.
    const a = await page.evaluate(async () => {
      const F = window.hsFixture;
      const org = await F.create("org", "/api/v2/setup/organization", { name: "VAG Revoked Org" });
      const program = await F.create("program", "/api/v2/setup/program",
        { name: "VAG Revoked Program", country: "US",
          operator_organization_id: org.id });
      // #409 EXPLICIT SELECTION. Season is PROGRAM-AXIS, so the Program-only
      // choice has to be PERSISTED BEFORE the Season create — not after,
      // where the raw context POST used to sit. The venue-access grant below
      // is SEASON-OWNED, so both axes are saved before it, and the selection
      // is proved by the server's own write echo rather than assumed.
      await F.selectProgram("Program-only bootstrap (A)", program.id);
      const season = await F.create("season", "/api/v2/setup/season",
        { program_id: program.id, name: "VAG Revoked Season" });
      await F.selectProgramSeason("Program+Season (A)", program.id, season.id);
      const venue = await F.create("venue", "/api/v2/setup/venue",
        { name: "VAG Revoked Venue", organization_id: org.id });
      const rink = await F.create("rink", "/api/v2/setup/rink",
        { venue_id: venue.id, name: "VAG Revoked Rink" });
      const access = await F.create("access", `/api/v2/setup/seasons/${season.id}/venue-access`,
        { venue_id: venue.id });
      const revoked = await post(
        `/api/v2/setup/season-venue-access/${access.id}/remove`, {});
      return { program: program.id, season: season.id, seasonName: season.name,
               venue: venue.id, rink: rink.id, access: access.id,
               revoked: !!revoked && !revoked.error };
    });
    for (const k of ["program", "season", "venue", "rink", "access"]) {
      if (!a[k]) fail(`[${L}] fixture (A) failed to create ${k}: ${JSON.stringify(a)}`);
    }
    if (!a.revoked) fail(`[${L}] fixture (A) never revoked the grant: ${JSON.stringify(a)}`);

    // The HISTORY half of "non-vacuous": the revoked row really is preserved
    // and really does name this Venue, which is why the overview keeps showing
    // it. If this row were gone the fixture would be an ordinary ungranted one.
    const aHistory = await apiGet(page, `/api/v2/setup/seasons/${a.season}/venue-access`);
    const revokedRow = (aHistory.venue_access || []).find((r) => r.id === a.access);
    if (!revokedRow || revokedRow.active !== false || revokedRow.venue_id !== a.venue) {
      fail(`[${L}] fixture (A) expected a preserved, INACTIVE grant row for the `
        + `venue; got ${JSON.stringify(aHistory)}`);
    }
    if ((aHistory.venue_access || []).some((r) => r.active)) {
      fail(`[${L}] fixture (A) still holds an ACTIVE grant, so nothing is blocked: `
        + `${JSON.stringify(aHistory)}`);
    }

    await reenter(page, base);
    icePreviews = [];
    await openFacilitiesLanding(page, `${L}/A/admin`);
    await assertBlocked(page, L, "A/admin", a,
      { venue: a.venue, rink: a.rink, action: "Allow a venue for this season" });
    noPreviewsSince("A/admin");

    // -- the same fixture, as the role that cannot resolve it ---------------
    await apiPost(page, "/api/auth/logout", {});
    await loginAs(page, "arena", "demo");
    await reenter(page, base);
    // #409: stated and READ BACK, through the app's own switch pipeline.
    // It runs AFTER `reenter`, not before: an identity change here is a
    // bare `POST /api/auth/login` with no reload, so until the shell is
    // re-entered the page is still displaying the PREVIOUS role's view.
    // Driving `setActiveContext` against that stale view makes it re-fetch
    // under the new identity — which is how this produced 403s on
    // /api/players and /api/v2/setup/hierarchy for the Arena Manager. The
    // raw POST it replaces hid that only because it never re-rendered.
    await selectProgramSeason(page, `[${L}] select a.program/a.season`,
      a.program, a.season);
    icePreviews = [];
    await openFacilitiesLanding(page, `${L}/A/arena`);
    await assertBlocked(page, L, "A/arena", a,
      { venue: a.venue, rink: a.rink, action: null });
    noPreviewsSince("A/arena");

    // ================= (C) RECOVERY, through the real entry point =========
    await apiPost(page, "/api/auth/logout", {});
    await loginAs(page, "admin", "demo");
    await reenter(page, base);
    // #409: stated and READ BACK, through the app's own switch pipeline.
    // It runs AFTER `reenter`, not before: an identity change here is a
    // bare `POST /api/auth/login` with no reload, so until the shell is
    // re-entered the page is still displaying the PREVIOUS role's view.
    // Driving `setActiveContext` against that stale view makes it re-fetch
    // under the new identity — which is how this produced 403s on
    // /api/players and /api/v2/setup/hierarchy for the Arena Manager. The
    // raw POST it replaces hid that only because it never re-rendered.
    await selectProgramSeason(page, `[${L}] select a.program/a.season`,
      a.program, a.season);
    icePreviews = [];
    await openFacilitiesLanding(page, `${L}/C/before`);
    await assertBlocked(page, L, "C/before", a,
      { venue: a.venue, rink: a.rink, action: "Allow a venue for this season" });

    // A sentinel that survives everything EXCEPT a document reload, so "the
    // card advanced without a page reload" is asserted rather than assumed.
    await page.evaluate(() => { window.__vagNoReload = "sentinel"; });

    // The single offered control must reach the REAL venue-access surface and
    // land focus on the control that actually performs the grant -- not merely
    // dump the operator at the top of the hierarchy tree to hunt for it.
    await page.click('[data-setup-landing-actions="facilities"] .act.primary');
    await page.waitForSelector(`#va-add-${a.season}`, { timeout: 15000 })
      .catch(() => fail(`[${L}/C] "Allow a venue for this season" did not reach the `
        + `selected Season's Allowed-venues picker`));
    // Focus lands through the same settlement-bound destination intent the
    // participation deep-link uses (the destination view is fetched
    // asynchronously, so the control does not exist at click time) -- so this
    // WAITS for it rather than sampling the instant the element appears. Legs
    // (D)/(E) below are what prove the waiting is bounded by the render's own
    // settlement rather than by a clock.
    await page.waitForFunction((sid) => document.activeElement
      && document.activeElement.id === `va-add-${sid}`, a.season, { timeout: 10000 })
      .catch(async () => {
        const el = await page.evaluate(() => ({
          id: document.activeElement && document.activeElement.id,
          tag: document.activeElement && document.activeElement.tagName }));
        fail(`[${L}/C] the resolution path did not focus the Allow picker; focus is `
          + `on ${JSON.stringify(el)}`);
      });

    // The grant itself, through that picker -- not a raw fetch.
    await page.selectOption(`#va-add-${a.season}`, a.venue);
    const grantResp = page.waitForResponse((r) =>
      r.url() === `${base}/api/v2/setup/seasons/${a.season}/venue-access`
      && r.request().method() === "POST");
    await page.click(`[data-va-add="${a.season}"]`);
    const grantBody = await (await grantResp).json();
    if (!grantBody || grantBody.error) {
      fail(`[${L}/C] the grant through the real Allow control failed: `
        + `${JSON.stringify(grantBody)}`);
    }
    // Let the grant's OWN re-render land before navigating: it repaints the
    // tree with the new active grant (its Revoke control is the proof), and
    // starting a second navigation on top of an in-flight render would only be
    // racing this test's own steps.
    await page.waitForSelector(`[data-va-revoke="${grantBody.id}"]`, { timeout: 15000 })
      .catch(() => fail(`[${L}/C] the granted venue never appeared as an active `
        + `allowed venue on the Season's own list`));

    // Back to the SAME card. No reload anywhere in this leg.
    await openFacilitiesLanding(page, `${L}/C/after`);
    const after = await readFacilities(page);
    if (after.blockedBecause !== null) {
      fail(`[${L}/C] the Facilities card still reports "${after.blockedBecause}" after `
        + `access was granted through the real entry point`);
    }
    if (after.effective !== "Add Ice") {
      fail(`[${L}/C] the card did not advance to its declared primary; effective `
        + `action is ${JSON.stringify(after.effective)}`);
    }
    if (after.addIceRoutes !== 1) {
      fail(`[${L}/C] expected exactly one control wired to the Ice Availability `
        + `Builder once access exists, got ${after.addIceRoutes}`);
    }
    if (after.buttons.length < 2) {
      fail(`[${L}/C] an unblocked landing keeps its demoted actions, got `
        + `${JSON.stringify(after.buttons)}`);
    }
    const sentinel = await page.evaluate(() => window.__vagNoReload || null);
    if (sentinel !== "sentinel") {
      fail(`[${L}/C] the page reloaded during the resolution leg — the card is `
        + `required to advance without one`);
    }

    // ...and the card's OWN refresh path is card-scoped: it must not disturb a
    // neighbour's generation or committed model.
    const beforeRefresh = await page.evaluate(() => ({
      gens: Object.assign({}, cardGenerations),
      others: Object.keys(cardStates).filter((k) => k !== "setup/facilities")
        .reduce((acc, k) => { acc[k] = JSON.stringify(cardStates[k]); return acc; }, {}),
    }));
    await page.evaluate(() => retrySetupWorkflowCard("facilities"));
    await page.waitForFunction(() =>
      readCardState("setup/facilities").state !== "loading", null, { timeout: 15000 });
    const afterRefresh = await page.evaluate(() => ({
      gens: Object.assign({}, cardGenerations),
      others: Object.keys(cardStates).filter((k) => k !== "setup/facilities")
        .reduce((acc, k) => { acc[k] = JSON.stringify(cardStates[k]); return acc; }, {}),
      effective: (readCardState("setup/facilities").effective || {}).label || null,
      blockedBecause: readCardState("setup/facilities").blockedBecause || null,
    }));
    if (!(afterRefresh.gens["setup/facilities"] > beforeRefresh.gens["setup/facilities"])) {
      fail(`[${L}/C] the card's own refresh issued no new generation `
        + `(${beforeRefresh.gens["setup/facilities"]} -> `
        + `${afterRefresh.gens["setup/facilities"]}), so it asserted nothing`);
    }
    if (afterRefresh.effective !== "Add Ice" || afterRefresh.blockedBecause !== null) {
      fail(`[${L}/C] after its own refresh the card reads effective=`
        + `${JSON.stringify(afterRefresh.effective)} / blockedBecause=`
        + `${JSON.stringify(afterRefresh.blockedBecause)}`);
    }
    for (const key of Object.keys(beforeRefresh.others)) {
      if (beforeRefresh.gens[key] !== afterRefresh.gens[key]) {
        fail(`[${L}/C] the Facilities card's own refresh moved "${key}"'s generation `
          + `(${beforeRefresh.gens[key]} -> ${afterRefresh.gens[key]}) — a per-card `
          + `refresh must replace only its own card's generation`);
      }
      if (beforeRefresh.others[key] !== afterRefresh.others[key]) {
        fail(`[${L}/C] the Facilities card's own refresh mutated adjacent card `
          + `"${key}"'s committed model`);
      }
    }

    // ================= (B) CREATOR-OWNED PENDING VENUE + RINK =============
    // A second Program, so fixture (A)'s now-granted Venue is out of scope. The
    // Venue and Rink here reach the payload through `pending_link_*` -- rows
    // with no link to ANY Program that THIS caller created -- so each role must
    // create its own pair: a pending row is deliberately invisible to everyone
    // but its creator, and a leg run against rows the signed-in role cannot see
    // would assert against an EMPTY card instead of a blocked one.
    const b = await page.evaluate(async () => {
      const F = window.hsFixture;
      const program = await F.create("program", "/api/v2/setup/program",
        { name: "VAG Pending Program", country: "US" });
      // #409, same two boundaries as fixture (A). This fixture deliberately
      // makes NO grant, so only the Program axis is load-bearing for its
      // Venue/Rink — the Season is still selected so the screens the journey
      // then reads are scoped to the Season it means.
      await F.selectProgram("Program-only bootstrap (B)", program.id);
      const season = await F.create("season", "/api/v2/setup/season",
        { program_id: program.id, name: "VAG Pending Season" });
      await F.selectProgramSeason("Program+Season (B)", program.id, season.id);
      const venue = await F.create("venue", "/api/v2/setup/venue", { name: "VAG Pending Venue Admin" });
      const rink = await F.create("rink", "/api/v2/setup/rink",
        { venue_id: venue.id, name: "VAG Pending Rink Admin" });
      return { program: program.id, season: season.id, seasonName: season.name,
               venue: venue.id, rink: rink.id };
    });
    for (const k of ["program", "season", "venue", "rink"]) {
      if (!b[k]) fail(`[${L}] fixture (B) failed to create ${k}: ${JSON.stringify(b)}`);
    }
    // The other half of "non-vacuous" for this fixture: there is genuinely NO
    // grant at all, so the rows are visible purely as creator-owned drafts.
    const bHistory = await apiGet(page, `/api/v2/setup/seasons/${b.season}/venue-access`);
    if ((bHistory.venue_access || []).length !== 0) {
      fail(`[${L}] fixture (B) is supposed to have no grant history at all, got `
        + `${JSON.stringify(bHistory)}`);
    }

    await reenter(page, base);
    icePreviews = [];
    await openFacilitiesLanding(page, `${L}/B/admin`);
    await assertBlocked(page, L, "B/admin", b,
      { venue: b.venue, rink: b.rink, action: "Allow a venue for this season" });
    noPreviewsSince("B/admin");

    await apiPost(page, "/api/auth/logout", {});
    await loginAs(page, "arena", "demo");
    // Re-enter the shell BEFORE selecting: `loginAs` is a bare
    // `POST /api/auth/login` with no reload, so the page is still displaying
    // the League Admin's view, and driving `setActiveContext` against it
    // makes that view re-fetch under the Arena Manager identity (403 on
    // /api/players and /api/v2/setup/hierarchy). Same reason as every other
    // identity change in this file (#409).
    await reenter(page, base);
    const bArena = await page.evaluate(async (fx) => {
      const F = window.hsFixture;
      // #409: a Venue is PROGRAM-AXIS, so a saved Program is what this create
      // needs. This is the ARENA MANAGER's session, whose saved row is its
      // own, so the tuple is stated here rather than inherited from the admin
      // session that built fixture (B). BOTH axes are named, not just the
      // Program the create strictly requires: the assertions that follow read
      // the Facilities card, whose committed identity must stay bound to
      // fixture (B)'s Season — a Program-only selection would legitimately
      // null the Season out and the card would then disagree with the screen.
      await F.selectProgramSeason("arena session: Program+Season (B)",
        fx.program, fx.season);
      const venue = await F.create("venue", "/api/v2/setup/venue", { name: "VAG Pending Venue Arena" });
      const rink = await F.create("rink", "/api/v2/setup/rink",
        { venue_id: venue.id, name: "VAG Pending Rink Arena" });
      return { venue: venue.id, rink: rink.id };
    }, { program: b.program, season: b.season });
    if (!bArena.venue || !bArena.rink) {
      fail(`[${L}] fixture (B) could not create the Arena Manager's own pending `
        + `Venue/Rink: ${JSON.stringify(bArena)}`);
    }
    await reenter(page, base);
    await selectProgramSeason(page, `[${L}] select b.program/b.season`,
      b.program, b.season);
    icePreviews = [];
    await openFacilitiesLanding(page, `${L}/B/arena`);
    await assertBlocked(page, L, "B/arena", b,
      { venue: bArena.venue, rink: bArena.rink, action: null });
    noPreviewsSince("B/arena");

    // ================= (D)/(E) A DESTINATION THAT SETTLES LATE ============
    // One fixture for both legs: a Program with TWO active Seasons and a
    // creator-owned Venue+Rink with no grant anywhere, so the Facilities card
    // is blocked under EITHER Season and either Season can render a picker.
    // (E) needs the second Season to be a real, authorized switcher option;
    // (D) needs the first to be blocked exactly as (B) is.
    await apiPost(page, "/api/auth/logout", {});
    await loginAs(page, "admin", "demo");
    const d = await page.evaluate(async () => {
      const F = window.hsFixture;
      const program = await F.create("program", "/api/v2/setup/program",
        { name: "VAG Late Program", country: "US" });
      // #409: all three Season creates are PROGRAM-AXIS, so the Program-only
      // choice is persisted first. The Venue and Rink after them are
      // PROGRAM-AXIS too and deliberately carry NO grant, which is what makes
      // the Facilities card blocked under either Season.
      await F.selectProgram("Program-only bootstrap (D)", program.id);
      const s1 = await F.create("s1", "/api/v2/setup/season",
        { program_id: program.id, name: "VAG Late Season One" });
      const s2 = await F.create("s2", "/api/v2/setup/season",
        { program_id: program.id, name: "VAG Late Season Two" });
      // A THIRD selectable Season, for leg (G2): a switch that queues behind
      // an in-flight one needs a target that is neither the departing Season
      // nor the one the in-flight switch is already heading for, or "the
      // queued call named a different destination" would not be observable.
      const s3 = await F.create("s3", "/api/v2/setup/season",
        { program_id: program.id, name: "VAG Late Season Three" });
      const venue = await F.create("venue", "/api/v2/setup/venue", { name: "VAG Late Venue" });
      const rink = await F.create("rink", "/api/v2/setup/rink",
        { venue_id: venue.id, name: "VAG Late Rink" });
      return { program: program.id, s1: s1.id, s1Name: s1.name,
               s2: s2.id, s2Name: s2.name, s3: s3.id, s3Name: s3.name,
               venue: venue.id, rink: rink.id };
    });
    for (const k of ["program", "s1", "s2", "s3", "venue", "rink"]) {
      if (!d[k]) fail(`[${L}] fixture (D) failed to create ${k}: ${JSON.stringify(d)}`);
    }
    const dFx = { program: d.program, season: d.s1, seasonName: d.s1Name };
    const dWant = { venue: d.venue, rink: d.rink,
                    action: "Allow a venue for this season" };

    // ---- (D) the picker appears long after the old budget would have gone --
    await reenter(page, base);
    // #409: stated and READ BACK, through the app's own switch pipeline.
    // It runs AFTER `reenter`, not before: an identity change here is a
    // bare `POST /api/auth/login` with no reload, so until the shell is
    // re-entered the page is still displaying the PREVIOUS role's view.
    // Driving `setActiveContext` against that stale view makes it re-fetch
    // under the new identity — which is how this produced 403s on
    // /api/players and /api/v2/setup/hierarchy for the Arena Manager. The
    // raw POST it replaces hid that only because it never re-rendered.
    await selectProgramSeason(page, `[${L}] select d.program/d.s1`,
      d.program, d.s1);
    icePreviews = [];
    await openFacilitiesLanding(page, `${L}/D/before`);
    await assertBlocked(page, L, "D/before", dFx, dWant);
    noPreviewsSince("D/before");

    await quiesceFocus(page);
    await delayHierarchyReads(page);
    await startFocusTrace(page);
    const dT0 = Date.now();
    // The ONLY interaction in this leg: activating the real recovery action.
    // Nothing below focuses anything, selects the picker, or clicks it.
    await page.click('[data-setup-landing-actions="facilities"] .act.primary');

    // Captured now, while it is standing; ASSERTED at the end of the leg,
    // after the behaviour. The subject here is where focus goes, and an
    // implementation assertion that fired first would report a mechanism
    // instead of the defect.
    const dIntent = await readFocusIntent(page);
    // Non-vacuous: the control genuinely is NOT there yet. If it were, the
    // delay would not be delaying and everything below would prove nothing.
    if (await page.evaluate((sid) => !!document.getElementById(`va-add-${sid}`), d.s1)) {
      fail(`[${L}/D] the Allow picker was already in the document immediately after `
        + `the click — the destination is not being held back at all`);
    }

    await page.waitForSelector(`#va-add-${d.s1}`, { timeout: 60000 })
      .catch(() => fail(`[${L}/D] the delayed destination never rendered the selected `
        + `Season's Allow picker at all`));
    const dPickerMs = Date.now() - dT0;
    if (dPickerMs < OLD_COMBINED_BUDGET_MS) {
      fail(`[${L}/D] the picker appeared ${dPickerMs}ms after activation, INSIDE the `
        + `~${OLD_COMBINED_BUDGET_MS}ms budget the replaced poll had — this leg is `
        + `supposed to make the destination settle after that budget would have `
        + `expired, so as written it would pass on the old code and proves nothing`);
    }
    await page.waitForFunction((sid) => document.activeElement
      && document.activeElement.id === `va-add-${sid}`, d.s1, { timeout: 30000 })
      .catch(async () => {
        const el = await page.evaluate(() => ({
          id: document.activeElement && document.activeElement.id,
          tag: document.activeElement && document.activeElement.tagName }));
        const t = await readFocusTrace(page);
        fail(`[${L}/D] focus never reached the Allow picker for a destination that `
          + `settled at ${dPickerMs}ms; focus is on ${JSON.stringify(el)} `
          + `(focusin trace: ${JSON.stringify(t.events)})`);
      });
    const dFocusMs = Date.now() - dT0;
    await stopFocusTrace(page);
    const dTrace = focusWindow(await readFocusTrace(page), L, "D");
    // THE assertion the replaced code fails: not "focus ended somewhere else"
    // but "focus was never once put on the generic region", across every
    // sample and every focus transition in the whole wait.
    const dOnContent = traceHits(dTrace, (f) => f.id === "content");
    if (dOnContent.length) {
      fail(`[${L}/D] focus was placed on the generic #content region `
        + `${dOnContent.length} time(s) (${JSON.stringify(dOnContent.slice(0, 4))}) while `
        + `waiting for a destination that settled at ${dPickerMs}ms — the recovery `
        + `action promises the control that grants venue access, and taking the page `
        + `region instead strands a keyboard/screen-reader operator at the page top`
        + `\nfocus timeline: ${JSON.stringify(traceRuns(dTrace))}`);
    }
    if (dFocusMs < OLD_COMBINED_BUDGET_MS) {
      fail(`[${L}/D] focus landed after only ${dFocusMs}ms, inside the old budget`);
    }
    // The observers really did observe: a sampler that recorded nothing, or a
    // listener that never saw the landing, would make the claim above empty.
    if (dTrace.samples.length < 100) {
      fail(`[${L}/D] the focus sampler collected only ${dTrace.samples.length} samples `
        + `across a ${dFocusMs}ms wait — it was not sampling throughout`);
    }
    if (!dTrace.events.some((e) => e.id === `va-add-${d.s1}`)) {
      fail(`[${L}/D] no focusin on the Allow picker was ever recorded, so the trace `
        + `above is not watching what it claims to`);
    }
    // ...and the promise that was kept above was held open by an intent
    // carrying an IDENTITY -- the same principal/session epoch and context
    // tuple discipline every other #365 gate uses. One without an identity
    // could not be cancelled by a change of one, which is leg (E)'s subject.
    if (!dIntent) {
      fail(`[${L}/D] activating the recovery action registered no destination focus `
        + `intent at all, so nothing identity-bound was holding the promise open `
        + `while the destination loaded`);
    }
    if (dIntent.view !== "setup" || dIntent.setupView !== "hierarchy") {
      fail(`[${L}/D] the intent names destination `
        + `${JSON.stringify(dIntent.view)}/${JSON.stringify(dIntent.setupView)}, `
        + `expected setup/hierarchy`);
    }
    if (dIntent.program_id !== d.program || dIntent.season_id !== d.s1
        || dIntent.league_id !== null) {
      fail(`[${L}/D] the intent is bound to tuple ${JSON.stringify(dIntent)}, not to `
        + `the context it was registered under (${d.program}/${d.s1}/null)`);
    }
    if (!(dIntent.epoch > 0) || dIntent.principal !== "admin") {
      fail(`[${L}/D] the intent carries no usable principal/session identity: `
        + `${JSON.stringify(dIntent)}`);
    }
    // ...and the intent is spent, not left standing to fire again later.
    if (await readFocusIntent(page)) {
      fail(`[${L}/D] the focus intent is still standing after it was kept: `
        + `${JSON.stringify(await readFocusIntent(page))}`);
    }
    await page.unroute(HIERARCHY_READS);

    // ---- (E) a Season switch inside that same window cancels it ------------
    await reenter(page, base);
    icePreviews = [];
    await openFacilitiesLanding(page, `${L}/E/before`);
    await assertBlocked(page, L, "E/before", dFx, dWant);
    noPreviewsSince("E/before");
    const switcherOption = `${d.program}|${d.s2}`;
    const hasOption = await page.evaluate((v) => {
      const sel = document.getElementById("ctx-select");
      return !!(sel && Array.from(sel.options).some((o) => o.value === v));
    }, switcherOption);
    if (!hasOption) {
      fail(`[${L}/E] the second Season is not an option in the real context switcher, `
        + `so this leg cannot switch context the way an operator would`);
    }

    await quiesceFocus(page);
    await delayHierarchyReads(page);
    await startFocusTrace(page);
    const eT0 = Date.now();
    await page.click('[data-setup-landing-actions="facilities"] .act.primary');
    const eIntent = await readFocusIntent(page);
    if (!eIntent || eIntent.season_id !== d.s1) {
      fail(`[${L}/E] expected a standing intent bound to the first Season before the `
        + `switch, got ${JSON.stringify(eIntent)}`);
    }
    // The operator changes Season in the context bar while the destination is
    // still loading — the real switcher, the same control they would use.
    await page.selectOption("#ctx-select", switcherOption);
    await page.waitForFunction(
      () => destinationFocusIntent === null, null, { timeout: 30000 })
      .catch(async () => fail(`[${L}/E] the deep-link focus intent survived a context `
        + `switch: ${JSON.stringify(await readFocusIntent(page))} — a superseded tuple `
        + `must cancel it outright`));
    // The switched-to Season's own destination then settles, equally late, and
    // that is the moment a surviving intent would have fired.
    await page.waitForSelector(`#va-add-${d.s2}`, { timeout: 60000 })
      .catch(() => fail(`[${L}/E] the switched-to Season's hierarchy never settled, so `
        + `the window a stale intent would have resolved in never happened`));
    const eSettleMs = Date.now() - eT0;
    if (eSettleMs < OLD_COMBINED_BUDGET_MS) {
      fail(`[${L}/E] the switched-to destination settled after only ${eSettleMs}ms, `
        + `inside the old budget — the switch did not happen inside a genuinely `
        + `delayed window`);
    }
    // A settlement is synchronous with its paint, so anything that was going
    // to grab focus already has; sample a little past it anyway.
    await page.waitForTimeout(750);
    await stopFocusTrace(page);
    const eTrace = focusWindow(await readFocusTrace(page), L, "E");
    const ePickers = traceHits(eTrace,
      (f) => typeof f.id === "string" && f.id.indexOf("va-add-") === 0);
    if (ePickers.length) {
      fail(`[${L}/E] a venue-access picker was focused after the context switch `
        + `(${JSON.stringify(ePickers.slice(0, 4))}) — the intent belonged to `
        + `"${d.s1Name}", which the operator has left; neither that Season's picker `
        + `nor the arriving Season's may be focused by it`
        + `\nfocus timeline: ${JSON.stringify(traceRuns(eTrace))}`);
    }
    const eOnContent = traceHits(eTrace, (f) => f.id === "content");
    if (eOnContent.length) {
      fail(`[${L}/E] a cancelled intent still took the generic #content landing `
        + `${eOnContent.length} time(s) — cancelling means focusing NOTHING`
        + `\nfocus timeline: ${JSON.stringify(traceRuns(eTrace))}`);
    }
    if (eTrace.samples.length < 100) {
      fail(`[${L}/E] the focus sampler collected only ${eTrace.samples.length} samples `
        + `across a ${eSettleMs}ms wait — it was not sampling throughout`);
    }
    await page.unroute(HIERARCHY_READS);

    // ====== (F) THE NAVIGATION'S OWN GENERIC POLL, SUPERSEDED =============
    // No delayed reads and no quiesceFocus anywhere in this leg: the subject
    // is the ORDINARY, fast path, which is where the recorded failure lives.
    await reenter(page, base);
    // #409: stated and READ BACK, through the app's own switch pipeline.
    // It runs AFTER `reenter`, not before: an identity change here is a
    // bare `POST /api/auth/login` with no reload, so until the shell is
    // re-entered the page is still displaying the PREVIOUS role's view.
    // Driving `setActiveContext` against that stale view makes it re-fetch
    // under the new identity — which is how this produced 403s on
    // /api/players and /api/v2/setup/hierarchy for the Arena Manager. The
    // raw POST it replaces hid that only because it never re-rendered.
    await selectProgramSeason(page, `[${L}] select d.program/d.s1`,
      d.program, d.s1);
    icePreviews = [];
    if (await page.evaluate(() => !!document.querySelector(
        '[data-setup-landing-actions="facilities"] .act.primary'))) {
      fail(`[${L}/F] the Facilities landing's action is already on screen before the `
        + `navigation that is supposed to render it, so the arming below would fire `
        + `against the previous surface`);
    }
    await startFocusTrace(page);

    // ARM THE ACTIVATION INSIDE THE PAGE, and do it BEFORE the navigation.
    //
    // WHY NOT page.click(): the window this leg exists for is the gap between
    // the landing painting and the pending poll's next 50ms tick. A driver-side
    // click has to cross the CDP boundary to get there and lands somewhere in
    // that gap by luck -- which is exactly why the defect showed up as an
    // intermittent journey failure rather than a deterministic one, and why a
    // leg that clicked from outside would reproduce it only sometimes.
    //
    // A MutationObserver callback is a MICROTASK of the task that mutated the
    // DOM, and a setTimeout tick is a macrotask, so the ordering here is not a
    // race at all: the activation provably runs before any further tick of the
    // poll the navigation started. Nothing about the activation is simulated --
    // it is the landing's own control, found by its own selector, invoked
    // through its own click handler.
    //
    // It deliberately does NOT focus the button first. This leg makes no focus
    // call of any kind; where focus goes is the entire subject.
    await page.evaluate(() => {
      window.__vagArm = null;
      const sel = '[data-setup-landing-actions="facilities"] .act.primary';
      window.__vagArmObserver = new MutationObserver(() => {
        const btn = document.querySelector(sel);
        if (!btn || window.__vagArm) return;
        window.__vagArmObserver.disconnect();
        const a = document.activeElement;
        window.__vagArm = {
          t: Date.now() - window.__vagFocus.t0,
          label: (btn.textContent || "").trim(),
          activeAt: { id: (a && a.id) || null, tag: (a && a.tagName) || null },
        };
        btn.click();
      });
      window.__vagArmObserver.observe(document.getElementById("content"),
        { childList: true, subtree: true });
    });

    const fNavT = Date.now();
    await page.click('[data-setup-workflow-nav="facilities"]');
    await page.waitForFunction(() => !!window.__vagArm, null, { timeout: 20000 })
      .catch(() => fail(`[${L}/F] the Facilities landing never rendered the recovery `
        + `action, so nothing was ever activated inside the poll's window`));
    const fArm = await page.evaluate(() => window.__vagArm);

    // -- the activation really was the recovery action ----------------------
    if (fArm.label !== "Allow a venue for this season") {
      fail(`[${L}/F] the control activated on the landing was `
        + `${JSON.stringify(fArm.label)}, not the venue-access resolution path`);
    }
    // -- ...and the poll it has to outlive really was still pending ---------
    // The poll ends by FOCUSING something: the destination's heading, or
    // #content (early, or at its floor). Both halves below are checked again
    // against the full trace after the wait; this is the cheap, immediate one.
    if (!(fArm.t < 2000)) {
      fail(`[${L}/F] the recovery action was activated ${fArm.t}ms after the `
        + `navigation, i.e. after the generic poll's entire 40 x 50ms life had `
        + `already elapsed — it cannot still have been pending, so this leg would `
        + `prove nothing about superseding it`);
    }
    if (fArm.activeAt.tag !== "BUTTON" || fArm.activeAt.id) {
      fail(`[${L}/F] at the moment of activation focus was already on `
        + `${JSON.stringify(fArm.activeAt)} rather than still on the nav control — `
        + `the navigation's generic poll had already landed, so there was no `
        + `pending poll left for the deep link to supersede`);
    }

    await page.waitForSelector(`#va-add-${d.s1}`, { timeout: 30000 })
      .catch(() => fail(`[${L}/F] the recovery action never reached the selected `
        + `Season's Allow picker`));
    await page.waitForFunction((sid) => document.activeElement
      && document.activeElement.id === `va-add-${sid}`, d.s1, { timeout: 30000 })
      .catch(async () => {
        const t = await readFocusTrace(page);
        fail(`[${L}/F] focus never reached the Allow picker; focus is on `
          + `${JSON.stringify(await page.evaluate(() => ({
              id: document.activeElement && document.activeElement.id,
              tag: document.activeElement && document.activeElement.tagName })))} `
          + `(focusin trace: ${JSON.stringify(t.events)})`);
      });

    // Hold past the WHOLE life of the poll the navigation started (40 x 50ms
    // from the nav click), so "the stale fallback never fires" is a claim about
    // every tick it could ever have had, not about the moment focus arrived.
    const POLL_LIFE_MS = 2000;
    const fRemain = fNavT + POLL_LIFE_MS + 700 - Date.now();
    if (fRemain > 0) await page.waitForTimeout(fRemain);
    await stopFocusTrace(page);
    const fTrace = await readFocusTrace(page);

    const fOnContent = traceHits(fTrace, (f) => f.id === "content");
    if (fOnContent.length) {
      fail(`[${L}/F] the navigation's generic #content fallback fired `
        + `${fOnContent.length} time(s) (${JSON.stringify(fOnContent.slice(0, 4))}) `
        + `after the recovery action had registered a newer focus request — an `
        + `older navigation's poll must not get to answer for a newer one`
        + `\nfocus timeline: ${JSON.stringify(traceRuns(fTrace))}`);
    }
    // The same claim about the OTHER exit the poll can take: its heading
    // landing, arriving late, would strand the operator at the top of the tree
    // just as surely as #content.
    const fPrePoll = fTrace.events.filter((e) => e.t <= fArm.t
      && /^H[1-3]$/.test(e.tag || ""));
    if (fPrePoll.length) {
      fail(`[${L}/F] the generic poll had already landed on a heading before the `
        + `activation (${JSON.stringify(fPrePoll)}), so it was no longer pending `
        + `and this leg proves nothing`);
    }
    const fFinal = await page.evaluate((sid) => ({
      id: document.activeElement && document.activeElement.id,
      tag: document.activeElement && document.activeElement.tagName,
      want: `va-add-${sid}` }), d.s1);
    if (fFinal.id !== fFinal.want) {
      fail(`[${L}/F] focus reached the Allow picker and was then taken away — it is `
        + `now on ${JSON.stringify(fFinal)} after the whole ${POLL_LIFE_MS}ms poll `
        + `budget elapsed\nfocus timeline: ${JSON.stringify(traceRuns(fTrace))}`);
    }
    if (fTrace.samples.length < 50) {
      fail(`[${L}/F] the focus sampler collected only ${fTrace.samples.length} `
        + `samples across the poll's whole life — it was not sampling throughout`);
    }
    noPreviewsSince("F");

    // ---- (F2) ...and the floor STILL fires for the caller that is current --
    // Supersession must cost the fallback nothing when nothing supersedes it.
    // The destination's reads are held back so it cannot paint inside the
    // poll's budget -- precisely the "slow render" the floor was added for --
    // and there is no newer focus request anywhere, so the poll must run to its
    // end and land on #content rather than leaving focus on <body>.
    await reenter(page, base);
    await delayHierarchyReads(page);
    const gNavT = Date.now();
    await page.click('[data-setup-workflow-nav="facilities"]');
    await page.waitForFunction(() => document.activeElement
      && document.activeElement.id === "content", null, { timeout: 10000 })
      .catch(async () => fail(`[${L}/F2] a navigation whose destination is still `
        + `loading left focus on ${JSON.stringify(await page.evaluate(() => ({
            id: document.activeElement && document.activeElement.id,
            tag: document.activeElement && document.activeElement.tagName })))} `
        + `instead of taking focusContentHeading's #content floor — the floor is a `
        + `separate accepted fix and superseding must not remove it`));
    const gFloorMs = Date.now() - gNavT;
    if (gFloorMs < POLL_LIFE_MS) {
      fail(`[${L}/F2] focus reached #content after only ${gFloorMs}ms, i.e. before `
        + `the poll could have exhausted — that is the ordinary "painted content `
        + `with no heading" landing, not the floor this asserts`);
    }
    const gStill = await page.evaluate(() => ({
      skeleton: !!document.querySelector("#content .skeleton"),
      landing: !!document.querySelector('[data-setup-workflow-landing="facilities"]'),
    }));
    if (!gStill.skeleton || gStill.landing) {
      fail(`[${L}/F2] the destination had already painted (${JSON.stringify(gStill)}) `
        + `when focus reached #content, so this is not the still-loading floor`);
    }
    await page.unroute(HIERARCHY_READS);

    // ====== (G)/(H) A STALE GENERIC POLL MUST NOT CROSS A BOUNDARY ========
    // (#365 review round 13.) Round 12 bound the poll to newer focus REQUESTS
    // and stopped there, which is what leg (F) above asserts. The same stale
    // work was still alive across the two boundaries every other async
    // mutation in this slice is already cut off at: a poll started under
    // principal A / tuple X survived a confirmed context switch and a
    // principal change, and could still land #content on a surface belonging
    // to an identity or a tuple that never asked for it.
    //
    // Both legs share their arming and their claim:
    //   * the destination's reads are held back, so the navigation's poll is
    //     still polling a skeleton rather than having taken an early exit;
    //   * the crossing is armed in the microtask checkpoint of the
    //     navigation's own first paint, so no 50ms tick can have run before
    //     it starts (see armCrossingAtFirstPaint);
    //   * the poll's continued pendency AT the crossing is proven, not
    //     assumed: every exit it has focuses something, so "nothing had been
    //     focused, focus was still on the nav control, the skeleton was still
    //     up, and less than the poll's whole 2s life had elapsed" is exactly
    //     the statement "it had not exited yet";
    //   * and then the whole remaining life of that poll is watched. Nothing
    //     may be focused on its behalf: not #content, not a heading, and not
    //     by taking focus off whatever legitimately holds it. Cancelled means
    //     SILENT, not redirected onto the arriving surface.
    // No test-side focus call of any kind appears in either leg.
    //
    // They differ in WHERE THE WINDOW OPENS, because their boundaries differ.
    // (H)'s identity change is synchronous -- currentUser is replaced and
    // uiIdentityEpoch bumped in one turn -- so "the crossing" and "the moment
    // the app could first know" are the same instant, and waitForCrossing()
    // names it. (G)'s is not: the operator's `change` and the server's answer
    // are separated by a whole round trip, and the round-12 leg's use of
    // waitForCrossing() there made it blind to precisely that interval. (G)
    // therefore holds the POST and watches from the real `change` onward --
    // see holdContextSwitchPost/watchContextChangeEvent -- and carries its own
    // window assertions below rather than sharing this helper, which is
    // (H)'s.
    //
    // (G)'s window assertion: what "stale focus" MEANS on this surface -- the
    // generic poll's #content landing, EITHER shape of its heading landing
    // (h1-h3 and .section-title both), the departing Season's own Allow
    // picker, and the arriving Season's. Applied twice, to the whole
    // pre-response window and to the whole post-release one, over the focusin
    // trace AND the periodic samples together, so a landing that lasted less
    // than one sample interval is still caught.
    const assertNoStaleFocus = (step, when, win, seasons, minSamples) => {
      const pickers = seasons.map((s) => `va-add-${s}`);
      const hits = traceHits(win,
        (f) => isPollLanding(f) || pickers.indexOf(f.id) !== -1);
      if (hits.length) {
        fail(`[${L}/${step}] stale focus landed ${when}: `
          + `${JSON.stringify(hits.slice(0, 6))} — focus work started under the tuple `
          + `the operator has left may take neither the generic #content landing, nor `
          + `a heading, nor that tuple's own Allow picker, nor the arriving tuple's`
          + `\nfocus timeline: ${JSON.stringify(traceRuns(win))}`);
      }
      if (win.samples.length < minSamples) {
        fail(`[${L}/${step}] the focus sampler collected only ${win.samples.length} `
          + `samples ${when} — it was not sampling continuously across the window, `
          + `so "nothing was ever focused" is not something this leg observed`);
      }
    };
    const assertBoundaryCancelled = async (step, arm, cross, navT, what) => {
      // -- the crossing really was the boundary it claims to be ------------
      if (!arm.kind) fail(`[${L}/${step}] the crossing was never armed`);
      if (arm.landing) {
        fail(`[${L}/${step}] the Facilities landing had ALREADY painted when the `
          + `crossing was armed, so the navigation's poll would have exited on its `
          + `own account and there is no stale poll to cancel`);
      }
      // -- the poll was PROVABLY still pending at the confirmed crossing ---
      if (!(cross.t < POLL_LIFE_MS)) {
        fail(`[${L}/${step}] the boundary was crossed ${cross.t}ms after the focus `
          + `trace started, i.e. after the generic poll's entire 40 x 50ms life had `
          + `already elapsed — it cannot still have been pending, so this leg would `
          + `prove nothing about cancelling it`);
      }
      const before = cross.events.filter(isPollLanding);
      if (before.length) {
        fail(`[${L}/${step}] the generic poll had already landed before the boundary `
          + `was crossed (${JSON.stringify(before)}), so it was no longer pending`);
      }
      if (cross.active.tag !== "BUTTON" || cross.active.id) {
        fail(`[${L}/${step}] at the crossing focus was on `
          + `${JSON.stringify(cross.active)} rather than still on the nav control — `
          + `the navigation's generic poll had already landed, so there was no `
          + `pending poll left to cancel`);
      }
      if (!cross.skeleton || cross.landing) {
        fail(`[${L}/${step}] the destination had already painted at the crossing `
          + `(skeleton ${cross.skeleton}, landing ${cross.landing}) — the poll would `
          + `have exited on the painted content rather than still be polling`);
      }
      // -- ...and the boundary genuinely moved -----------------------------
      if (!what(arm, cross)) {
        fail(`[${L}/${step}] the boundary did not actually move: armed at `
          + `${JSON.stringify({ epoch: arm.epoch, season: arm.season, principal: arm.principal })}, `
          + `crossed at ${JSON.stringify({ epoch: cross.epoch, season: cross.season, principal: cross.principal })}`);
      }

      // -- the whole remaining life of the poll ----------------------------
      const remain = navT + POLL_LIFE_MS + 700 - Date.now();
      if (remain > 0) await page.waitForTimeout(remain);
      await stopFocusTrace(page);
      const trace = await readFocusTrace(page);
      const after = {
        samples: trace.samples.filter((s) => s.t >= cross.t),
        events: trace.events.filter((e) => e.t >= cross.t),
      };
      const onContent = traceHits(after, (f) => f.id === "content");
      if (onContent.length) {
        fail(`[${L}/${step}] the navigation's generic #content fallback fired `
          + `${onContent.length} time(s) (${JSON.stringify(onContent.slice(0, 4))}) `
          + `AFTER the boundary was crossed at ${cross.t}ms — a poll started before `
          + `the boundary must not get to answer for the surface on the other side `
          + `of it\nfocus timeline: ${JSON.stringify(traceRuns(trace))}`);
      }
      // The OTHER exit of the same stale chain, in every shape the selector
      // "h1, h2, h3, .section-title" admits (isPollLanding), and over the
      // periodic samples as well as the focusin events -- so a .section-title
      // landing, or one that lasted less than a sample interval, is caught
      // here too rather than only the H1-H3 transitions the older, narrower
      // predicate could see.
      const onHeading = traceHits(after, (f) => f.id !== "content" && isPollLanding(f));
      if (onHeading.length) {
        fail(`[${L}/${step}] the generic poll took its HEADING landing after the `
          + `boundary was crossed (${JSON.stringify(onHeading.slice(0, 6))}) — the `
          + `other exit of the same stale chain, and equally not this surface's to `
          + `take`);
      }
      const out = await crossingOutcome(page);
      if (out.active.id === "content") {
        fail(`[${L}/${step}] focus ended on the generic #content region: `
          + `${JSON.stringify(out.active)}`);
      }
      if (!out.atArm && !out.armGone) {
        fail(`[${L}/${step}] focus was taken off the control that legitimately held `
          + `it at the crossing and moved to ${JSON.stringify(out.active)} — `
          + `cancelling a stale poll must focus NOTHING, not redirect it`
          + `\nfocus timeline: ${JSON.stringify(traceRuns(trace))}`);
      }
      if (after.samples.length < 30) {
        fail(`[${L}/${step}] the focus sampler collected only ${after.samples.length} `
          + `samples across the poll's whole remaining life — it was not sampling `
          + `throughout`);
      }
      return out;
    };

    // ---- (G) the CONTEXT boundary, from the operator's own `change` -------
    // THE WINDOW: from the real `change` on #ctx-select until the held POST is
    // released, which is longer than the generic poll's entire life. The
    // switch has been ATTEMPTED throughout it and the server has not been
    // told, so contextOptions.selected still reads the DEPARTING Season and
    // every stale focus request still looks current to any test that judged by
    // the tuple. Nothing may be focused in it.
    await reenter(page, base);
    // #409: stated and READ BACK, through the app's own switch pipeline.
    // It runs AFTER `reenter`, not before: an identity change here is a
    // bare `POST /api/auth/login` with no reload, so until the shell is
    // re-entered the page is still displaying the PREVIOUS role's view.
    // Driving `setActiveContext` against that stale view makes it re-fetch
    // under the new identity — which is how this produced 403s on
    // /api/players and /api/v2/setup/hierarchy for the Arena Manager. The
    // raw POST it replaces hid that only because it never re-rendered.
    await selectProgramSeason(page, `[${L}] select d.program/d.s1`,
      d.program, d.s1);
    icePreviews = [];
    // THE SWITCHER HAS TO EXIST BEFORE THE OPERATOR CAN USE IT.
    // renderContextSwitcher() runs late in render() -- after its own awaited
    // reads -- so a page that has only just been re-entered has
    // contextOptions loaded but #ctx-select still empty and its wrapper still
    // hidden. Legs (D)/(E) never saw this because they read the switcher after
    // a full landing render; (F) never touches it. So reach a completed render
    // through the same real landing entry point, then LEAVE the Setup view
    // through a real nav control, so the navigation this leg actually measures
    // still starts from outside Setup with nothing painted.
    await openFacilitiesLanding(page, `${L}/G/switcher`);
    await page.click('.side-nav [data-tab="dashboard"]');
    await page.waitForFunction((v) => {
      const sel = document.getElementById("ctx-select");
      return !!(sel && Array.from(sel.options).some((o) => o.value === v));
    }, switcherOption, { timeout: 15000 })
      .catch(async () => fail(`[${L}/G] the second Season is not an option in the `
        + `real context switcher, so this leg cannot switch context the way an `
        + `operator would; the switcher offers ${JSON.stringify(
            await page.evaluate(() => {
              const sel = document.getElementById("ctx-select");
              return sel ? Array.from(sel.options).map((o) => o.value) : null;
            }))}`));
    // Those two navigations started generic polls of their own. They are not
    // this leg's subject -- the poll under test is the one the Facilities
    // navigation below starts, after the trace opens -- and leaving them in
    // flight would let an unrelated landing be read as the stale one.
    await quiesceFocus(page);
    await delayHierarchyReads(page);
    const gHold = await holdContextSwitchPost(page);
    await startFocusTrace(page);
    await watchContextChangeEvent(page);
    await armCrossingAtFirstPaint(page, "context", switcherOption);
    const gNav2T = Date.now();
    await page.click('[data-setup-workflow-nav="facilities"]');
    await page.waitForFunction(() => !!window.__vagCrossArm, null, { timeout: 20000 })
      .catch(() => fail(`[${L}/G] the Facilities navigation never painted, so the `
        + `context switch was never armed inside the poll's window`));
    const gArm = await page.evaluate(() => window.__vagCrossArm);
    if (!gArm.kind) fail(`[${L}/G] the crossing was never armed`);
    if (gArm.landing) {
      fail(`[${L}/G] the Facilities landing had ALREADY painted when the context `
        + `switch was armed, so the navigation's poll would have exited on its own `
        + `account and there is no stale poll to cancel`);
    }

    // -- THE WINDOW OPENS: the operator's own change on #ctx-select ---------
    await page.waitForFunction(() => !!window.__vagCtxChange, null, { timeout: 20000 })
      .catch(() => fail(`[${L}/G] the armed switch never produced a real change event `
        + `on #ctx-select, so there is no operator action to observe from`));
    const gChange = await page.evaluate(() => window.__vagCtxChange);
    if (gChange.value !== switcherOption) {
      fail(`[${L}/G] the change event carried ${JSON.stringify(gChange.value)}, not `
        + `the second Season's own switcher option`);
    }
    // -- ...and it is the PRE-response window, not the post-response one ----
    // The whole point of moving the observation start: at the operator's
    // action the canonical tuple has NOT moved, which is why
    // cancelSupersededDestinationFocus() cannot see anything to cancel here
    // and why the attempt-time abandonment has to exist.
    if (gChange.season !== d.s1) {
      fail(`[${L}/G] at the change event contextOptions.selected already read `
        + `${JSON.stringify(gChange.season)} rather than the departing Season — the `
        + `window this leg exists for is the one BEFORE the tuple moves, and it was `
        + `already over`);
    }
    // -- the poll was PROVABLY still pending at that instant ----------------
    if (!(gChange.t < POLL_LIFE_MS)) {
      fail(`[${L}/G] the change event fired ${gChange.t}ms after the focus trace `
        + `started, i.e. after the generic poll's entire 40 x 50ms life had already `
        + `elapsed — it cannot still have been pending, so this leg would prove `
        + `nothing about cancelling it`);
    }
    const gBefore = gChange.events.filter(isPollLanding);
    if (gBefore.length) {
      fail(`[${L}/G] the generic poll had already landed before the operator changed `
        + `Season (${JSON.stringify(gBefore)}), so it was no longer pending`);
    }
    if (gChange.active.tag !== "BUTTON" || gChange.active.id) {
      fail(`[${L}/G] at the change focus was on ${JSON.stringify(gChange.active)} `
        + `rather than still on the nav control — the navigation's generic poll had `
        + `already landed, so there was no pending poll left to cancel`);
    }
    if (!gChange.skeleton || gChange.landing) {
      fail(`[${L}/G] the destination had already painted at the change (skeleton `
        + `${gChange.skeleton}, landing ${gChange.landing}) — the poll would have `
        + `exited on the painted content rather than still be polling`);
    }

    // -- HOLD /api/context, and watch the ENTIRE pre-response window --------
    const gHeldBy = Date.now() + 10000;
    while (!gHold.state.heldAt && Date.now() < gHeldBy) await page.waitForTimeout(50);
    if (!gHold.state.heldAt) {
      fail(`[${L}/G] the context switch never issued POST /api/context, so there was `
        + `nothing to hold and no pre-response window to observe`);
    }
    // Comfortably past the poll's whole 40 x 50ms life, measured BOTH from the
    // moment the POST was intercepted and from the navigation that started the
    // poll — so every tick it could ever have had is inside this window.
    const G_HOLD_MS = 3400;
    const gHoldUntil = Math.max(gHold.state.heldAt + G_HOLD_MS,
      gNav2T + POLL_LIFE_MS + 700);
    const gWait = gHoldUntil - Date.now();
    if (gWait > 0) await page.waitForTimeout(gWait);
    // The hold really held: the app is still waiting on the POST and the
    // canonical tuple still names the Season the operator has already left.
    const gPending = await page.evaluate(() => ({
      season: ((contextOptions && contextOptions.selected) || {}).season_id || null,
      inFlight: contextSwitchInFlight,
      skeleton: !!document.querySelector("#content .skeleton"),
    }));
    if (gPending.season !== d.s1 || !gPending.inFlight) {
      fail(`[${L}/G] the held POST did not actually hold (${JSON.stringify(gPending)}) `
        + `— the pre-response window this leg observes never existed`);
    }
    const gHeldMs = Date.now() - gHold.state.heldAt;
    if (!(gHeldMs > POLL_LIFE_MS)) {
      fail(`[${L}/G] /api/context was held only ${gHeldMs}ms, inside the poll's own `
        + `${POLL_LIFE_MS}ms life — the poll could have been pending for the whole `
        + `window without ever reaching a tick`);
    }
    const gPreTrace = await readFocusTrace(page);
    assertNoStaleFocus("G",
      `across the whole ${gHeldMs}ms BEFORE /api/context was even forwarded to the `
        + `server (from the operator's own change event at ${gChange.t}ms)`,
      { samples: gPreTrace.samples.filter((s) => s.t >= gChange.t),
        events: gPreTrace.events.filter((e) => e.t >= gChange.t) },
      [d.s1, d.s2], 50);

    // -- RELEASE, and the silence must CONTINUE ----------------------------
    const gReleaseT = await page.evaluate(() => Date.now() - window.__vagFocus.t0);
    gHold.release();
    const gCross = await waitForCrossing(page, "context", d.s2)
      .catch(() => fail(`[${L}/G] the context switch was never confirmed after the `
        + `held POST was released — contextOptions.selected never moved to the `
        + `second Season`));
    if (gCross.season !== d.s2 || gArm.season !== d.s1) {
      fail(`[${L}/G] the boundary did not actually move: armed at `
        + `${JSON.stringify(gArm.season)}, crossed at ${JSON.stringify(gCross.season)}`);
    }
    await page.waitForTimeout(POLL_LIFE_MS + 700);
    await stopFocusTrace(page);
    const gTrace = await readFocusTrace(page);
    assertNoStaleFocus("G",
      `after the held POST was released and the switch confirmed at ${gCross.t}ms`,
      { samples: gTrace.samples.filter((s) => s.t >= gReleaseT),
        events: gTrace.events.filter((e) => e.t >= gReleaseT) },
      [d.s1, d.s2], 50);
    const gOut = await crossingOutcome(page);
    if (!gOut.atArm && !gOut.armGone) {
      fail(`[${L}/G] focus was taken off the control that legitimately held it at the `
        + `crossing and moved to ${JSON.stringify(gOut.active)} — cancelling stale `
        + `focus work must focus NOTHING, not redirect it`
        + `\nfocus timeline: ${JSON.stringify(traceRuns(gTrace))}`);
    }
    await stopContextChangeWatch(page);
    await gHold.stop();
    await page.unroute(HIERARCHY_READS);
    noPreviewsSince("G");

    // ---- (G2) A SWITCH QUEUED BEHIND ONE ALREADY IN FLIGHT ---------------
    // (#365 review round 14.) Leg (G) above issues ONE switch while nothing is
    // in flight, so it only ever walks setActiveContext()'s ordinary path: the
    // abandonment runs, the early return is NOT taken, and the POST goes out.
    // That makes (G) blind to WHERE the abandonment sits. Moving
    // abandonFocusWorkForContextSwitch() below the
    // `if (contextSwitchInFlight) { contextSwitchQueued = ...; return; }`
    // leaves (G) green, because on (G)'s path both placements run the same
    // code in the same order.
    //
    // The queued path is the independent reason the attempt-time boundary is
    // load-bearing, and it is the one the placement guarantee exists for. A
    // second selection made while an earlier switch's POST is still
    // unanswered SENDS NOTHING and returns immediately. If the abandonment is
    // below that return, that selection cancels nothing at all -- and any
    // focus work started after the first switch stays live and free to land
    // for as long as the first response takes to arrive, which is unbounded.
    //
    // So the order here is deliberately the reverse of (G)'s:
    //   1. the operator picks the SECOND Season on the real #ctx-select, and
    //      its POST /api/context is HELD before forwarding -- the switch stays
    //      in flight for exactly as long as this leg wants;
    //   2. THEN the Facilities navigation runs, starting the generic poll
    //      INSIDE that in-flight window, on a fresh ticket the first switch
    //      cannot have bumped (it bumped before this poll existed);
    //   3. THEN the operator picks the THIRD Season, armed in the microtask
    //      checkpoint of that navigation's own first paint so no 50ms tick can
    //      have run first. That call finds contextSwitchInFlight set, queues,
    //      and returns -- and cancelling the poll from (2) is something only a
    //      cancellation ABOVE the return can do.
    // The early return really being taken is read from the page rather than
    // assumed: contextSwitchQueued names the THIRD Season, contextSwitchInFlight
    // is still true, contextOptions.selected still names the DEPARTING one, and
    // the route has intercepted exactly ONE POST -- the queued call sent none.
    //
    // QUIET FIRST, for the same reason (H) does it below: (G) ends with a
    // released switch still reconciling.
    await reenter(page, base);
    await page.waitForLoadState("networkidle").catch(() => {});
    await reenter(page, base);
    // #409: stated and READ BACK, through the app's own switch pipeline.
    // It runs AFTER `reenter`, not before: an identity change here is a
    // bare `POST /api/auth/login` with no reload, so until the shell is
    // re-entered the page is still displaying the PREVIOUS role's view.
    // Driving `setActiveContext` against that stale view makes it re-fetch
    // under the new identity — which is how this produced 403s on
    // /api/players and /api/v2/setup/hierarchy for the Arena Manager. The
    // raw POST it replaces hid that only because it never re-rendered.
    await selectProgramSeason(page, `[${L}] select d.program/d.s1`,
      d.program, d.s1);
    icePreviews = [];
    // Same reason (G) does this: renderContextSwitcher() runs late in render(),
    // so a freshly re-entered page has #ctx-select still empty. Reach a
    // completed render through the real landing entry point, then leave Setup
    // through a real nav control so the navigation this leg measures still
    // starts from outside Setup with nothing painted.
    await openFacilitiesLanding(page, `${L}/G2/switcher`);
    await page.click('.side-nav [data-tab="dashboard"]');
    const switcherOption3 = `${d.program}|${d.s3}`;
    await page.waitForFunction((want) => {
      const sel = document.getElementById("ctx-select");
      if (!sel) return false;
      const have = Array.from(sel.options).map((o) => o.value);
      return want.every((v) => have.indexOf(v) !== -1);
    }, [switcherOption, switcherOption3], { timeout: 15000 })
      .catch(async () => fail(`[${L}/G2] the second and third Seasons are not both `
        + `options in the real context switcher, so this leg cannot queue one real `
        + `switch behind another the way an operator would; the switcher offers `
        + `${JSON.stringify(await page.evaluate(() => {
            const sel = document.getElementById("ctx-select");
            return sel ? Array.from(sel.options).map((o) => o.value) : null;
          }))}`));
    // Those navigations started generic polls of their own; drain them, so the
    // only pending poll in the window below is the one this leg starts.
    await quiesceFocus(page);
    await delayHierarchyReads(page);
    const g2Hold = await holdContextSwitchPost(page);
    await startFocusTrace(page);
    await watchContextChangeEvent(page);

    // -- (1) THE FIRST SWITCH, held before it can reach the server ---------
    // A real operator selection on the real control (Playwright's own select
    // action -- the same one leg (E) uses; no focus call). It happens BEFORE
    // the poll under test exists on purpose: THIS switch's abandonment runs
    // here, so it cannot be what cancels that poll.
    await page.selectOption("#ctx-select", switcherOption);
    const g2HeldBy = Date.now() + 10000;
    while (!g2Hold.state.heldAt && Date.now() < g2HeldBy) await page.waitForTimeout(50);
    if (!g2Hold.state.heldAt) {
      fail(`[${L}/G2] the first context switch never issued POST /api/context, so `
        + `there is nothing for a second switch to queue behind`);
    }
    const g2Flight = await page.evaluate(() => ({
      inFlight: contextSwitchInFlight,
      queued: contextSwitchQueued && contextSwitchQueued.seasonId,
      season: ((contextOptions && contextOptions.selected) || {}).season_id || null,
    }));
    if (!g2Flight.inFlight || g2Flight.queued || g2Flight.season !== d.s1) {
      fail(`[${L}/G2] the first switch is not cleanly in flight `
        + `(${JSON.stringify(g2Flight)}) — the second selection below would not be `
        + `taking setActiveContext()'s queued path at all`);
    }

    // -- (2) THE POLL, started INSIDE that in-flight window -----------------
    await armCrossingAtFirstPaint(page, "context", switcherOption3);
    const g2NavT = Date.now();
    await page.click('[data-setup-workflow-nav="facilities"]');
    await page.waitForFunction(() => !!window.__vagCrossArm, null, { timeout: 20000 })
      .catch(() => fail(`[${L}/G2] the Facilities navigation never painted, so the `
        + `queued switch was never armed inside the poll's window`));
    const g2Arm = await page.evaluate(() => window.__vagCrossArm);
    if (!g2Arm.kind) fail(`[${L}/G2] the crossing was never armed`);
    if (g2Arm.landing) {
      fail(`[${L}/G2] the Facilities landing had ALREADY painted when the queued `
        + `switch was armed, so the navigation's poll would have exited on its own `
        + `account and there is no stale poll to cancel`);
    }
    if (g2Arm.season !== d.s1) {
      fail(`[${L}/G2] at the navigation the canonical tuple already read `
        + `${JSON.stringify(g2Arm.season)} rather than the departing Season — the `
        + `first switch had already been answered, so nothing can queue behind it`);
    }

    // -- (3) THE WINDOW OPENS: the SECOND real change on #ctx-select --------
    await page.waitForFunction(() => (window.__vagCtxChanges || []).length >= 2,
      null, { timeout: 20000 })
      .catch(() => fail(`[${L}/G2] the armed switch never produced a SECOND real `
        + `change event on #ctx-select, so there is no queued selection to observe `
        + `from`));
    const g2Changes = await page.evaluate(() => window.__vagCtxChanges);
    if (g2Changes.length !== 2
        || g2Changes[0].value !== switcherOption
        || g2Changes[1].value !== switcherOption3) {
      fail(`[${L}/G2] #ctx-select saw `
        + `${JSON.stringify(g2Changes.map((c) => c.value))} rather than exactly the `
        + `second Season followed by the third — this leg's whole subject is the `
        + `SECOND of two real selections`);
    }
    const g2Change = g2Changes[1];
    // -- ...and it really is the PRE-response window ------------------------
    if (g2Change.season !== d.s1) {
      fail(`[${L}/G2] at the queued change event contextOptions.selected already read `
        + `${JSON.stringify(g2Change.season)} rather than the departing Season — the `
        + `first switch had been answered, so this is not the queued path`);
    }
    // -- the poll was PROVABLY still pending at that instant ----------------
    if (!(g2Change.t - g2Arm.t < POLL_LIFE_MS)) {
      fail(`[${L}/G2] the queued change fired ${g2Change.t - g2Arm.t}ms after the `
        + `navigation's first paint, i.e. after the generic poll's entire 40 x 50ms `
        + `life had already elapsed — it cannot still have been pending`);
    }
    const g2Before = g2Change.events.filter((e) => e.t >= g2Arm.t).filter(isPollLanding);
    if (g2Before.length) {
      fail(`[${L}/G2] the generic poll had already landed before the operator made `
        + `the queued selection (${JSON.stringify(g2Before)}), so it was no longer `
        + `pending`);
    }
    if (g2Change.active.tag !== "BUTTON" || g2Change.active.id) {
      fail(`[${L}/G2] at the queued change focus was on `
        + `${JSON.stringify(g2Change.active)} rather than still on the nav control — `
        + `the navigation's generic poll had already landed, so there was no pending `
        + `poll left to cancel`);
    }
    if (!g2Change.skeleton || g2Change.landing) {
      fail(`[${L}/G2] the destination had already painted at the queued change `
        + `(skeleton ${g2Change.skeleton}, landing ${g2Change.landing}) — the poll `
        + `would have exited on the painted content rather than still be polling`);
    }

    // -- (4) THE EARLY RETURN WAS TAKEN, read from the page itself ----------
    const g2Queued = await page.evaluate(() => ({
      queuedSeason: contextSwitchQueued && contextSwitchQueued.seasonId,
      queuedProgram: contextSwitchQueued && contextSwitchQueued.programId,
      inFlight: contextSwitchInFlight,
      season: ((contextOptions && contextOptions.selected) || {}).season_id || null,
    }));
    if (g2Queued.queuedSeason !== d.s3 || g2Queued.queuedProgram !== d.program
        || !g2Queued.inFlight || g2Queued.season !== d.s1) {
      fail(`[${L}/G2] the second selection did not take setActiveContext()'s queued `
        + `early return: ${JSON.stringify(g2Queued)} — expected contextSwitchQueued `
        + `to name the THIRD Season ${JSON.stringify(d.s3)}, contextSwitchInFlight to `
        + `still be true, and contextOptions.selected to still be the departing `
        + `Season ${JSON.stringify(d.s1)}`);
    }
    if (g2Hold.state.seen !== 1) {
      fail(`[${L}/G2] ${g2Hold.state.seen} POST /api/context request(s) were issued, `
        + `not the one the held first switch sent — the queued selection is supposed `
        + `to send NOTHING and return, which is the whole path this leg exercises`);
    }

    // -- (5) THE WHOLE PRE-RESPONSE WINDOW, past the poll's entire life -----
    const G2_HOLD_MS = 3400;
    const g2HoldUntil = Math.max(g2Hold.state.heldAt + G2_HOLD_MS,
      g2NavT + POLL_LIFE_MS + 700);
    const g2Wait = g2HoldUntil - Date.now();
    if (g2Wait > 0) await page.waitForTimeout(g2Wait);
    const g2Pending = await page.evaluate(() => ({
      season: ((contextOptions && contextOptions.selected) || {}).season_id || null,
      inFlight: contextSwitchInFlight,
      queuedSeason: contextSwitchQueued && contextSwitchQueued.seasonId,
      skeleton: !!document.querySelector("#content .skeleton"),
    }));
    if (g2Pending.season !== d.s1 || !g2Pending.inFlight
        || g2Pending.queuedSeason !== d.s3 || g2Hold.state.releasedAt) {
      fail(`[${L}/G2] the first POST did not stay held across the window `
        + `(${JSON.stringify(g2Pending)}, released ${g2Hold.state.releasedAt}) — the `
        + `pre-response window this leg observes never existed`);
    }
    const g2SincePoll = Date.now() - g2NavT;
    if (!(g2SincePoll > POLL_LIFE_MS) || !(Date.now() - g2Hold.state.heldAt > POLL_LIFE_MS)) {
      fail(`[${L}/G2] the window ran only ${g2SincePoll}ms from the navigation that `
        + `started the poll, inside its own ${POLL_LIFE_MS}ms life — the poll could `
        + `have been pending throughout without ever reaching its floor`);
    }
    const g2PreTrace = await readFocusTrace(page);
    const g2Pre = { samples: g2PreTrace.samples.filter((s) => s.t >= g2Change.t),
                    events: g2PreTrace.events.filter((e) => e.t >= g2Change.t) };
    assertNoStaleFocus("G2",
      `across the whole ${g2SincePoll}ms from the queued selection at ${g2Change.t}ms, `
        + `while the FIRST switch's POST was still held and the queued one had sent `
        + `nothing`,
      g2Pre, [d.s1, d.s2, d.s3], 50);
    // ...and not merely "no landing this leg names": no focus move AT ALL.
    // Focus was on the nav control the operator was standing on when they made
    // the queued selection, and cancelled focus work is SILENT, so it must
    // still be there and nothing may have taken it in between.
    const g2Moved = g2Pre.events
      .concat(g2Pre.samples)
      .filter((f) => (f.id || null) !== g2Change.active.id
        || (f.tag || null) !== g2Change.active.tag);
    if (g2Moved.length) {
      fail(`[${L}/G2] focus moved off ${JSON.stringify(g2Change.active)} — the `
        + `control that legitimately held it at the queued selection — to `
        + `${JSON.stringify(g2Moved.slice(0, 6))} while the first switch was still `
        + `unanswered\nfocus timeline: ${JSON.stringify(traceRuns(g2PreTrace))}`);
    }

    // -- (6) RELEASE BOTH, and the silence must CONTINUE --------------------
    // Releasing the first POST lets sendContextSwitch() dequeue straight into
    // the queued switch, so the tuple finally moves to the THIRD Season.
    const g2ReleaseT = await page.evaluate(() => Date.now() - window.__vagFocus.t0);
    g2Hold.release();
    const g2Cross = await waitForCrossing(page, "context", d.s3)
      .catch(() => fail(`[${L}/G2] the queued switch never reached the server after `
        + `the first POST was released — contextOptions.selected never moved to the `
        + `third Season, so the queue was dropped rather than merely deferred`));
    if (g2Cross.season !== d.s3) {
      fail(`[${L}/G2] the switch confirmed on ${JSON.stringify(g2Cross.season)} `
        + `rather than the queued third Season`);
    }
    await page.waitForTimeout(POLL_LIFE_MS + 700);
    await stopFocusTrace(page);
    const g2Trace = await readFocusTrace(page);
    assertNoStaleFocus("G2",
      `after both switches were released and the QUEUED one confirmed at `
        + `${g2Cross.t}ms`,
      { samples: g2Trace.samples.filter((s) => s.t >= g2ReleaseT),
        events: g2Trace.events.filter((e) => e.t >= g2ReleaseT) },
      [d.s1, d.s2, d.s3], 50);
    const g2Out = await crossingOutcome(page);
    if (!g2Out.atArm && !g2Out.armGone) {
      fail(`[${L}/G2] focus was taken off the control that legitimately held it and `
        + `moved to ${JSON.stringify(g2Out.active)} — cancelling stale focus work `
        + `must focus NOTHING, not redirect it`
        + `\nfocus timeline: ${JSON.stringify(traceRuns(g2Trace))}`);
    }
    await stopContextChangeWatch(page);
    await g2Hold.stop();
    await page.unroute(HIERARCHY_READS);
    noPreviewsSince("G2");

    // ---- (H) the IDENTITY boundary ----------------------------------------
    // A real in-app sign-in to a DIFFERENT principal, through the app's own
    // signIn() -- the function the login form's submit handler and every demo
    // persona button call. Deliberately not a reload: destroying the document
    // would destroy the pending poll along with it and prove nothing.
    // "arena" holds manage_arena, so the Facilities nav control the operator
    // is standing on survives the switch and can still legitimately hold
    // focus -- which is what makes "focus was not yanked" assertable at all.
    //
    // QUIET FIRST. (G2) deliberately ends with the page mid-reconciliation: it
    // released a held context switch, and the reload/repaint that switch
    // triggers is still fetching. Signing that session out from under those
    // in-flight reads produces 401s that belong to this file's own sequencing
    // and not to the product, and the journey (correctly) fails on any console
    // error. A fresh document plus an idle network is the honest way to say
    // "the previous leg is over".
    await reenter(page, base);
    await page.waitForLoadState("networkidle").catch(() => {});
    await apiPost(page, "/api/auth/logout", {});
    await loginAs(page, "admin", "demo");
    await reenter(page, base);
    // #409: stated and READ BACK, through the app's own switch pipeline.
    // It runs AFTER `reenter`, not before: an identity change here is a
    // bare `POST /api/auth/login` with no reload, so until the shell is
    // re-entered the page is still displaying the PREVIOUS role's view.
    // Driving `setActiveContext` against that stale view makes it re-fetch
    // under the new identity — which is how this produced 403s on
    // /api/players and /api/v2/setup/hierarchy for the Arena Manager. The
    // raw POST it replaces hid that only because it never re-rendered.
    await selectProgramSeason(page, `[${L}] select d.program/d.s1`,
      d.program, d.s1);
    icePreviews = [];
    await delayHierarchyReads(page);
    await startFocusTrace(page);
    await armCrossingAtFirstPaint(page, "identity", "arena");
    const hNavT = Date.now();
    await page.click('[data-setup-workflow-nav="facilities"]');
    await page.waitForFunction(() => !!window.__vagCrossArm, null, { timeout: 20000 })
      .catch(() => fail(`[${L}/H] the Facilities navigation never painted, so the `
        + `sign-in was never armed inside the poll's window`));
    const hArm = await page.evaluate(() => window.__vagCrossArm);
    const hCross = await waitForCrossing(page, "identity", "arena")
      .catch(() => fail(`[${L}/H] the in-app sign-in never adopted the new principal`));
    const hOut = await assertBoundaryCancelled("H", hArm, hCross, hNavT,
      (a, c) => a.principal === "admin" && c.principal === "arena"
        && c.epoch > a.epoch);
    if (hOut.signIn !== true) {
      fail(`[${L}/H] the app's own signIn("arena") reported `
        + `${JSON.stringify(hOut.signIn)} — the identity boundary was not crossed `
        + `through the real no-reload sign-in path`);
    }
    await page.unroute(HIERARCHY_READS);
    noPreviewsSince("H");

    if (errors.length) fail(`[${L}] browser errors:\n${errors.join("\n")}`);
    console.log(`[${L}] OK — a Venue+Rink whose grant to the selected Season was `
      + `revoked, and a creator-owned pending Venue+Rink with no grant at all, are `
      + `both VISIBLE (non-zero Venues/Rinks counts, backed by the scoped overview, `
      + `and by a preserved inactive grant row / a genuinely empty grant list) and `
      + `neither is schedulable: the Facilities card settles READY+todo under its `
      + `exact card/program/season/league identity with a blocker sentence naming `
      + `venue access AND the selected season, offers no "Add Ice" control and no `
      + `route to the Ice Availability Builder, and issues no ice-preview request `
      + `at all. League Admin is offered exactly one control -- the real `
      + `venue-access resolution path, which lands focus on the selected Season's `
      + `own Allow picker -- while Arena Manager, who cannot grant that access, is `
      + `offered no mutation control at all and told a League Admin must set it up. `
      + `Granting through that real picker advances the SAME card to "Add Ice" with `
      + `its demoted actions restored, with no page reload, and the card's own `
      + `refresh moves only its own generation and leaves every adjacent card's `
      + `committed model untouched. With the hierarchy/venue-access reads held `
      + `back past the ~${OLD_COMBINED_BUDGET_MS}ms budget the replaced poll had, that `
      + `same action still lands focus on the exact selected Season's picker -- with `
      + `no test-side focus action, and without focus ever once being placed on the `
      + `generic #content region across every sample and every focusin of the wait -- `
      + `and switching Season in the real context switcher inside that window cancels `
      + `the intent outright, focusing neither the Season it was registered for nor `
      + `the one that replaced it. On the ORDINARY fast path, with nothing held back `
      + `and no focus drained, activating that action from inside the very DOM `
      + `mutation that paints it -- while the navigation's own 40 x 50ms generic poll `
      + `is provably still pending -- lands focus on the picker and leaves it there `
      + `for the poll's whole remaining budget, with the stale generic #content `
      + `fallback never firing once; and with the destination held back and nothing `
      + `superseding it, that same poll still takes its #content floor. With `
      + `POST /api/context HELD before forwarding for longer than that poll's whole `
      + `life, and watching from the operator's own change event on #ctx-select `
      + `onward, nothing at all is focused across the entire pre-response window -- `
      + `neither #content, nor a heading, nor the departing Season's Allow picker, `
      + `nor the arriving Season's -- with the switch proven still unanswered `
      + `throughout it, and that silence continues once the request is released and `
      + `the switch confirms. The same holds when the operator's selection only `
      + `QUEUES: with the first switch's POST held and the navigation's poll started `
      + `inside that in-flight window, choosing a THIRD Season on the real switcher `
      + `takes setActiveContext's early return -- proven from the page, which shows `
      + `the queued switch naming the third Season, the switch still in flight, the `
      + `canonical tuple still the departing Season, and exactly one POST ever `
      + `issued -- and still cancels that poll: nothing is focused and focus does `
      + `not move at all across the whole window, and the silence continues once `
      + `both switches are released and the queued one confirms. The same holds `
      + `across an in-app change of principal.`);
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
    console.log("Facilities venue-access gate browser journey passed.");
  } catch (e) {
    console.error("Facilities venue-access gate browser journey FAILED.");
    console.error(e.message);
    process.exitCode = 1;
  } finally {
    await browser.close();
  }
})();
