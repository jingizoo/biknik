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
//  (5) SAME-TUPLE RENDER DURING AN UNRESOLVED WRITE — the round-5 blocker,
//      verbatim from the review:
//
//        "A same-tuple render overwrites PENDING while the reopen POST is
//        still unresolved, restores 'Reopen this season,' and permits a
//        duplicate write. The new state says it is terminal until its own
//        response, but commitSetupWorkflowCards() still issues a fresh
//        generation and commits a newly fetched model for every workflow on
//        an ordinary Setup render, without preserving a current same-tuple
//        PENDING operation. ... After it settles, the same card is READY at
//        generation 5, aria-busy=false, and Reopen this season is actionable
//        again, even though the first write is still unresolved. Confirming
//        again enters PENDING at generation 7 and produces a second reopen
//        POST. The interceptor observed two concurrent writes for the same
//        Season."
//
//      Legs 2 and 3 above prove a response arriving into a MOVED tuple is
//      discarded. This leg is the opposite and previously unguarded case: the
//      tuple does NOT move, an ordinary render runs underneath the write, and
//      the write's identity must survive it intact. The request is held
//      BEFORE it reaches the server (no route.fetch() until release), the
//      render is triggered through the REAL Facilities navigation control,
//      and the duplicate-write count is taken AT THE ROUTE INTERCEPTOR — so
//      it counts writes ISSUED by the client, which is the actual harm,
//      rather than writes the server happened to accept.
//
//      (5a) held-then-503: the card must retain the EXACT generation number
//           it entered PENDING with (not merely `state === "pending"` — a
//           repaint-back would satisfy that while having already superseded
//           the write), aria-busy=true, its pending copy and focus, an
//           unchanged completion/next roll-up, and ZERO actions on both the
//           card body and the landing action groups; keyboard AND pointer
//           activation must leave the reopen-request count at exactly one;
//           and the eventual 503 must still produce the round-4 reason/error/
//           focus recovery. Non-vacuity: the SAME render must be shown to
//           have advanced the OTHER workflow cards' generations, or it would
//           not have been a render at all.
//      (5b) the same, released as a genuinely COMMITTED success: the one
//           terminal response advances only this card — every other card's
//           generation and committed model is byte-identical across the
//           release — and says the one success sentence exactly once.
//
//  (6) A → B → A ROUND TRIP DURING AN UNRESOLVED WRITE — the round-6 blocker,
//      verbatim from the review:
//
//        "Leaving a tuple deletes the unresolved-write registration, so
//        returning to that same tuple before the response settles restores the
//        mutation and permits a duplicate write. currentCardWrite() deletes
//        cardWrites[cardId] as soon as cardTupleCurrent(held) becomes false.
//        That makes Program/Season B render normally, but it permanently
//        forgets that the first request against Program/Season A is still in
//        flight; navigation did not cancel that HTTP request or its possible
//        server transaction. ... A now renders READY at generation 5,
//        aria-busy=false, with Reopen this season actionable, although its
//        first write is still unresolved. Confirm again: A enters PENDING at
//        generation 7 and the interceptor observes two concurrent reopen
//        requests for the same Season.
//
//        This is the same unknown-outcome hazard across a short round trip
//        rather than a same-tuple render. Calling the operation 'abandoned' is
//        not a cancellation/serialization rule: the server may still commit
//        it, its response is now correctly discarded as stale, and the
//        restored control can issue a second lifecycle mutation before the
//        client knows the first outcome."
//
//      Leg 5 is the case where the tuple never moves. Legs 2 and 3 are the
//      case where it moves and does not come back. This is the case in
//      between, and it is the one the previous round's own comment argued was
//      no gap at all: it documented such a write as "ABANDONED" and called
//      discarding its response "the right outcome". That conflated the
//      RESPONSE being discarded (true, and legs 2/3 exist for it) with no harm
//      being done (false — navigation cancels no HTTP request and no server
//      transaction).
//
//      (6a) Held BEFORE the server; A → B → A through the REAL #ctx-select
//           without releasing. B must render NORMALLY — its own Season, its
//           own settled state, aria-busy=false and its own action offered —
//           while returned A must come back NON-ACTIONABLE and
//           operation-pending, rebuilt from the ledger rather than from
//           anything the round trip left behind: the committed model was
//           overwritten by B's renders and the DOM was destroyed, so the card
//           reads pending at the generation it entered PENDING with even
//           though the card's COUNTER has moved past it. That gap is asserted
//           in both directions, because it is what proves the initiating UI
//           identity is genuinely stale and the settlement below therefore
//           takes the stale-identity path. Pointer, keyboard AND direct
//           code-level attempts must leave the reopen-request count at exactly
//           one. Released as a 503, returned A is then RECONCILED FROM FRESH
//           SERVER TRUTH — a progress read is observed leaving the browser
//           after the settlement, and the card lands on the REAL recovery
//           action the server's own prerequisite answer implies, not on the
//           held confirmation the client was carrying.
//      (6b) The same round trip, released as a genuinely COMMITTED success:
//           the reconcile must ADVANCE the card to the next valid action
//           ("Add Ice"), which is a state no held value could have produced —
//           the client's held model was blocked by season_active.
//      (6c) Released while the operator is STILL ON B: B stays byte-identical
//           by snapshot (the round-4 rule, unchanged), the LEDGER DRAINS
//           anyway — asserted directly on cardWrites, not inferred — and a
//           later return to A reads current server truth instead of sitting
//           blocked forever behind a registration nothing would ever retire.
//
//      No completion sentence is allowed anywhere in leg 6, on either
//      release. After a round trip the client cannot attribute what it now
//      reads to its own write: a 503 delivered here does not prove the write
//      did not commit, and a 200 describes a Season the client stopped
//      tracking. Reconciling and saying so is honest; "Season reopened." is a
//      stale claim.
//
//  (7) AN AUTHENTICATED-USER CHANGE DURING AN UNRESOLVED WRITE — the round-7
//      blocker, verbatim from the review:
//
//        "The ledger and card identity survive an authenticated-user change,
//        so the departing user's delayed operation response can mutate the
//        next user's UI and disclose the departing user's typed reason.
//        resetTransientUiState() invalidates other identity-scoped UI state
//        but does not invalidate cardWrites, cardStates, or cardGenerations;
//        the card identity contains only card + Program/Season/League +
//        generation, not the authenticated principal/session epoch. Because
//        the ledger refuses the arriving user's render for the same tuple,
//        the departing identity's generation also remains current. ... Release
//        A's request as 503. While currentUser.username === 'second_admin',
//        the card becomes CONFIRM and displays A's exact reason 'first write
//        held', A's 'held failure' error, moves focus to that reason field,
//        and writes the error into B's toast. ... With different roles it can
//        also restore controls/state that the arriving role did not initiate
//        and may not be authorized to exercise."
//
//      Legs 2/3 move the TUPLE and stay moved; leg 5 moves nothing; leg 6
//      moves the tuple and comes back. This leg moves the PERSON, which none
//      of them do: the tuple is identical throughout — both operators are
//      persisted on the very same Program/archived Season — and the card,
//      the target and the generation are identical too. The ONLY thing that
//      changes is who is signed in, so this leg fails for exactly one reason.
//
//      NON-VACUITY IS BUILT IN AND ASSERTED: while the arriving principal is
//      standing on the target, this file reads the card's generation COUNTER
//      and requires it to be UNCHANGED from the value the write entered
//      PENDING with. That is the review's own observation — the serialization
//      rule refuses the arriving principal's render for the same tuple, so
//      the counter cannot move — and it means generation equality alone still
//      says "current". Nothing but the principal/session epoch stands between
//      the departing operator's response and the arriving operator's card.
//
//      Each sub-leg holds A's reopen, switches to the arriving principal, and
//      asserts on BOTH sides of the release:
//
//        WHILE UNRESOLVED — the arriving principal's card is NON-ACTIONABLE
//        (otherwise the round-6 duplicate write returns), the reopen-request
//        count stays at exactly one under pointer, keyboard and direct
//        code-level attempts, the ledger entry still stands and is visibly
//        FOREIGN (its identity's epoch is behind the current one and its
//        principal is the departing operator) — and NOTHING the departing
//        operator entered is anywhere on the rendered surface. The reason is
//        searched for as a LITERAL across body innerText, body innerHTML,
//        every input/textarea value, the live region, the recorded
//        announcement log, the committed card models and the ledger's held
//        models: a leak into a toast, a heading or a live region is the same
//        leak as one into the reason field. The write's own busy copy
//        ("Reopening this season…") is searched for the same way, because
//        telling the arriving principal WHAT is happening discloses that the
//        departing one did something.
//
//        AFTER THE RELEASE — no toast and no announcement AT ALL (not merely
//        no success one), no focus movement (focus is parked on a stable
//        control outside the card first, and must still be there), no reason,
//        no error text, the ledger DRAINED, and the card RECONCILED from a
//        fresh /api/v2/setup/progress read that is observed leaving the
//        browser after the settlement — under the ARRIVING principal's own
//        permissions and the server's current truth.
//
//      (7a) in-app persona switch (signIn(), no reload), released as a 503:
//           the Season is still archived, so League Admin B's reconciled card
//           lands on the REAL recovery action.
//      (7b) the same switch, released as a genuinely COMMITTED success: B's
//           card advances to "Add Ice" — a state no held value could have
//           produced, since everything A was carrying was blocked by
//           season_active.
//      (7c) a REAL sign-out (the header control) followed by a sign-in
//           through the login form, released as a 503.
//      (7d) the same real sign-out/sign-in, released as a committed success.
//      (7e) a LOWER-PRIVILEGE arriving role (Arena Manager) on the same
//           authorized tuple, released as a 503. Reopening a Season is
//           MANAGE_SETUP and an Arena Manager holds MANAGE_ARENA only, so the
//           reconciled card must offer NO action at all — the reported "can
//           also restore controls/state that the arriving role ... may not be
//           authorized to exercise" is what this sub-leg forbids.
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
// The 503 body every held-then-failed release in this file answers with, as
// one constant so leg 7 can search the rendered surface for the EXACT text an
// arriving principal must never be shown (app.js publishes the server's own
// message verbatim into the restored confirmation and the toast).
const SERVER_503_MESSAGE = "The server is temporarily unavailable (503). "
  + "Please try again in a moment.";
// The card's own NEUTRAL copy for a write that belongs to a DEPARTED
// principal (app.js FOREIGN_CARD_WRITE_NOTE). Asserted as an exact string: it
// is the whole of what the arriving principal is allowed to learn, and it
// names neither the operation, nor the operator, nor the reason.
const NEUTRAL_PENDING_TEXT = "This card is waiting on the server. It can't"
  + " be changed until that finishes.";
// The ARRIVING principals of leg 7. `second_admin` is the reviewer's own
// second League Admin — same role, same authority, different person, so the
// leak cannot be excused as a permissions difference. `arena` is the demo
// Arena Manager /api/demo/load seeds: authorized on the same tuple, and NOT
// authorized to reopen a Season (that is MANAGE_SETUP).
const SECOND_ADMIN = "second_admin";
const SECOND_ADMIN_PW = "second-admin-pw";
const ARENA_USER = "arena";
const ARENA_PW = "demo";

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
  await openFacilitiesNoSettle(page, step);
  await settled(page, step);
}

// The same transition WITHOUT the settle wait. Leg 7's arriving principal
// lands on a card that is deliberately PENDING — the ledger keeps it
// non-actionable while the departing principal's write is unresolved — and
// settled() excludes PENDING on purpose, so waiting for it there would hang
// on the very state the leg exists to observe.
async function openFacilitiesNoSettle(page, step) {
  await page.click('.tab[data-tab="setup"]:not([data-setup-workflow-nav])');
  await page.waitForSelector('[data-setup-workflow="facilities"]', { timeout: 15000 })
    .catch(() => fail(`[${step}] the Setup hub never offered "facilities"`));
  await page.click('[data-setup-workflow="facilities"]');
  await page.waitForSelector('[data-setup-workflow-landing="facilities"]',
    { timeout: 15000 })
    .catch(() => fail(`[${step}] the facilities landing never rendered`));
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
      // Whether the two surfaces the "zero controls" assertions read are
      // actually PAINTED. Without these, a landing that failed to render at
      // all would satisfy "no buttons" for entirely the wrong reason.
      hasLanding: !!root,
      hasActionsBox: !!box,
      // The card's own generation COUNTER, beside the generation the committed
      // identity carries. After a round trip the two must differ: the counter
      // moved under the other tuple's renders while the operation's identity
      // stayed where it was, which is precisely what makes the initiating UI
      // identity stale.
      counter: cardGenerations["setup/facilities"],
      // The switch-intent withdrawal is a DIFFERENT reason for a landing to
      // have no controls (setupLandingActions checks it first). Read it, so
      // "non-actionable because a write is unresolved" is never confused with
      // "non-actionable because a switch has not reconciled yet".
      intentPending: !!contextSwitchIntentPending,
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

// Re-enter the Facilities destination through the REAL navigation control —
// the sidebar's own "Venues, rinks and ice" entry, which is what the reviewer
// re-entered — and wait until the ordinary render pipeline has actually
// REPLACED the painted surface. A synthetic render() call would prove nothing
// about the destination transition; a fixed sleep would prove nothing about
// the render having run at all.
//
// The wait is observed, not timed: the landing element is stamped before the
// click and the stamp can only disappear when render()'s own
// `c.innerHTML = renderSetup(...)` rebuilds it. That happens in BOTH a correct
// build (which refuses the card's model commit but still repaints) and a
// build without the preservation (which commits over it), so this wait cannot
// mask the defect it is here to expose.
async function sameTupleRenderThroughNav(page, step) {
  const marked = await page.evaluate(() => {
    const el = document.querySelector('[data-setup-workflow-landing="facilities"]');
    if (!el) return false;
    el.setAttribute("data-wi-render-mark", "1");
    return true;
  });
  if (!marked) fail(`[${step}] the Facilities landing is not painted, so there `
    + `is no destination to re-enter`);
  const nav = await page.$('.tab[data-setup-workflow-nav="facilities"]');
  if (!nav) fail(`[${step}] the real Facilities navigation control is not `
    + `present, so the render cannot be triggered the way an operator does`);
  await nav.click();
  await page.waitForFunction(() => {
    const el = document.querySelector('[data-setup-workflow-landing="facilities"]');
    return !!el && !el.hasAttribute("data-wi-render-mark");
  }, null, { timeout: 20000 })
    .catch(() => fail(`[${step}] the same-tuple render never repainted the `
      + `Facilities landing`));
  // The paint is the last thing render() does before its wiring; give anything
  // it kicked off (a per-card repaint, a focus poll) a beat to land, so the
  // assertions below read a quiesced page rather than a mid-flight one.
  await page.waitForTimeout(600);
}

// The UNRESOLVED-OPERATION LEDGER itself, read straight out of the page. The
// behavioural assertions below would catch a ledger that never drains only by
// hanging on a later wait; this reads the record directly, so "the ledger
// drained even though the operator was on another tuple when the response
// landed" is proven rather than inferred from an absence.
async function readLedger(page) {
  return page.evaluate(() => {
    const per = cardWrites["setup/facilities"];
    return {
      cards: Object.keys(cardWrites).sort(),
      entries: per ? Object.keys(per).length : 0,
      targets: per ? Object.keys(per).map((k) => per[k].target).sort() : [],
      // #365 round 7. WHO each surviving entry belongs to, beside who is
      // signed in now. A record that outlives an authenticated-user change has
      // to be visibly FOREIGN, or "the private payload was quarantined" could
      // not be told apart from "there was nothing to quarantine". These four
      // fields are what make the epoch's work observable rather than inferred.
      epochs: per ? Object.keys(per).map((k) => per[k].identity.epoch) : [],
      principals: per ? Object.keys(per).map((k) => per[k].identity.principal) : [],
      currentEpoch: uiIdentityEpoch,
      currentPrincipal: currentUser ? currentUser.username : null,
    };
  });
}

// ===================== LEG 7 HELPERS (#365 round 7) =======================

// Persist BOTH operators' own active context on the SAME Program/Season, and
// leave the page's session as `admin` (the departing principal).
//
// Both halves matter. The active context is per-user server state, so the
// arriving principal has to be persisted on the target INDEPENDENTLY — that is
// what makes them LAND on the write's target with no switching of their own,
// which is the only way the "arriving principal is on the target tuple at
// settlement" path is exercised at all. And it is what makes the leg's one
// variable genuinely the PERSON: card, tuple, target Season and generation are
// all identical on both sides of the switch.
async function persistContextFor(page, arrive, programId, seasonId, step) {
  await loginAs(page, arrive.username, arrive.password);
  const theirs = await apiPost(page, "/api/context",
    { program_id: programId, season_id: seasonId });
  if (!theirs || theirs.error) {
    fail(`[${step}] could not persist ${arrive.username}'s own context on the `
      + `target tuple: ${JSON.stringify(theirs)}`);
  }
  await loginAs(page, "admin", "demo");
  const mine = await apiPost(page, "/api/context",
    { program_id: programId, season_id: seasonId });
  if (!mine || mine.error) {
    fail(`[${step}] could not persist admin's context on the target tuple: `
      + `${JSON.stringify(mine)}`);
  }
}

// PATH (a): the in-app, NO-RELOAD principal change. signIn() is the exact
// function the login form and the demo role-switcher (#role-switch) call, and
// it is what drives setUser() -> resetTransientUiState(). Deliberately never a
// page.goto(): a reload starts a fresh document with an empty ledger and an
// empty card store, which would prove nothing about state surviving a switch.
async function signInInApp(page, username, password, step) {
  const ok = await page.evaluate(([u, p]) => signIn(u, p), [username, password]);
  if (ok !== true) {
    fail(`[${step}] the app's own signIn("${username}") returned `
      + `${JSON.stringify(ok)} — the in-app persona path never completed`);
  }
  await page.waitForFunction((u) => !!currentUser && currentUser.username === u,
    username, { timeout: 15000 })
    .catch(() => fail(`[${step}] the app never adopted ${username}`));
}

// PATH (b): a REAL sign-out through the header control, then a REAL sign-in
// through the login form. A different transition from (a) — it passes through
// setUser(null) first, so resetTransientUiState() fires TWICE and the session
// is genuinely ended server-side in between — and the review requires both.
//
// The sign-out control is activated through its own element (el.click()) so
// the assertion does not depend on where a 390x844 layout happens to put the
// header: it is the real control and the real handler either way.
async function signOutThenIn(page, username, password, step) {
  const pressed = await page.evaluate(() => {
    const btn = document.getElementById("signout-btn");
    if (!btn) return false;
    btn.click();
    return true;
  });
  if (!pressed) fail(`[${step}] there is no real sign-out control to press`);
  await page.waitForFunction(() => !currentUser, null, { timeout: 15000 })
    .catch(() => fail(`[${step}] the real sign-out never cleared the session`));
  await page.waitForSelector("#login-screen:not([hidden])", { timeout: 15000 })
    .catch(() => fail(`[${step}] the sign-in screen never appeared`));
  await page.fill("#login-user", username);
  await page.fill("#login-pass", password);
  await page.click("#login-form button[type=submit]");
  await page.waitForFunction((u) => !!currentUser && currentUser.username === u,
    username, { timeout: 15000 })
    .catch(() => fail(`[${step}] signing in as ${username} through the login `
      + `form never took effect`));
}

async function arrivePrincipal(page, arrive, step) {
  if (arrive.via === "sign-out") {
    return signOutThenIn(page, arrive.username, arrive.password, step);
  }
  return signInInApp(page, arrive.username, arrive.password, step);
}

// EVERY rendered surface AND every store behind it, searched for a LITERAL
// string the departing operator entered. Returns the list of places it was
// found, so a failure names the leak's route.
//
// The review is explicit that the reason input's value is not the only place
// that counts: "Assert the ABSENCE of A's reason text by searching the whole
// rendered surface for the literal string, not just the reason input's value —
// a leak into a toast, a live region or a heading is the same leak." So this
// reads innerText AND innerHTML (an <input value="…"> attribute appears only
// in the latter), every field's live value (which appears in neither), the
// sitewide live region, the recorded announcement history (a sentence spoken
// and auto-dismissed four seconds later is still a sentence that was spoken),
// and — when `includeStores` — the client stores themselves: the committed
// card models and the ledger's held models, so "quarantined" means the text is
// GONE rather than merely unpainted.
//
// `includeStores` is FALSE for the card's own DECLARED copy (the busy,
// success and refresh sentences). Those strings are constants belonging to the
// reopen action's declaration, so a card that legitimately OFFERS that action
// carries them in its committed model by construction — for the arriving
// principal exactly as for anyone else. Finding them there proves nothing; the
// question for declared copy is only ever whether it was PAINTED or SPOKEN,
// which is what the remaining sources measure. An operator's typed reason and
// a server's error message are the reverse: they can never be declared copy,
// so for those the stores are searched too.
async function privateTextSightings(page, literal, includeStores) {
  return page.evaluate(([s, stores]) => {
    const hits = [];
    const look = (where, text) => {
      if (text && String(text).indexOf(s) !== -1) hits.push(where);
    };
    look("document.body innerText", document.body && document.body.innerText);
    look("document.body innerHTML", document.body && document.body.innerHTML);
    document.querySelectorAll("input, textarea").forEach((el, i) =>
      look(`field[${i}] ${el.id || el.className || el.tagName}`, el.value));
    const root = document.getElementById("toast-root");
    look("#toast-root", root && root.textContent);
    look("announcement history", JSON.stringify(window.__ann || []));
    if (!stores) return hits;
    look("readCardState(setup/facilities)",
      JSON.stringify(readCardState("setup/facilities")));
    look("cardStates", JSON.stringify(cardStates));
    look("cardWrites held models", JSON.stringify(Object.keys(cardWrites)
      .map((c) => Object.keys(cardWrites[c]).map((k) => cardWrites[c][k].model))));
    return hits;
  }, [literal, !!includeStores]);
}

// Where keyboard focus is, as one comparable string.
async function focusNow(page) {
  return page.evaluate(() => {
    const el = document.activeElement;
    if (!el) return "null";
    return `${el.tagName}[${el.className || ""}]`
      + `[tab=${el.getAttribute && el.getAttribute("data-tab") || ""}]`
      + `{${(el.textContent || "").replace(/\s+/g, " ").trim().slice(0, 60)}}`;
  });
}

// Park focus on a real control OUTSIDE the card before the release, so "no
// focus movement from A's response" is a positive assertion rather than the
// vacuous observation that focus was on <body> before and after. The Setup
// navigation tab is outside #content, so the card's own repaint cannot destroy
// it — only a deliberate focus move could take it away.
async function parkFocusOutsideCard(page, step) {
  const ok = await page.evaluate(() => {
    const el = document.querySelector('.tab[data-tab="setup"]:not([data-setup-workflow-nav])');
    if (!el) return false;
    el.focus();
    return document.activeElement === el;
  });
  if (!ok) {
    fail(`[${step}] could not park keyboard focus on a stable control outside `
      + `the card, so "focus did not move" would be asserted against <body> `
      + `and would prove nothing`);
  }
}

// Wait until the ARRIVING principal's card is showing the NEUTRAL pending
// presentation: PENDING (so every control is withdrawn on both surfaces), the
// generic copy, and NO identity at all — the model is reconstructed for this
// principal rather than handed over from the departing one's ledger entry.
async function foreignPendingPresentation(page, step) {
  await page.waitForFunction((note) => {
    if (contextSwitchIntentPending) return false;
    const e = readCardState("setup/facilities");
    return e.state === "pending" && e.pendingNote === note && !e.identity;
  }, NEUTRAL_PENDING_TEXT, { timeout: 15000 })
    .catch(() => fail(`[${step}] the arriving principal's card is not showing `
      + `the neutral pending presentation. Either the departing principal's `
      + `own held model was handed straight over (their reason, their counts, `
      + `their confirmation), or the ledger entry was dropped on the identity `
      + `change and the card became actionable with a lifecycle write still in `
      + `flight.`));
  await page.waitForTimeout(600);
}

// A held reopen in the two shapes leg 7 needs, counted AT THE INTERCEPTOR
// because the harm is a write ISSUED for one Season, not one the server chose
// to accept.
//
//   commit: true  — the SERVER takes the write immediately (route.fetch() runs
//                   while the INITIATING principal is still authenticated;
//                   afterwards their session is gone and the same request
//                   would be answered as somebody else, or as nobody), and
//                   only the DELIVERY is held across the identity change.
//   commit: false — answered 503 without route.fetch(), so the server
//                   genuinely never takes the write and the Season stays
//                   archived throughout.
function heldReopen(opts) {
  const h = { requests: 0, status: null, body: null };
  h.held = new Promise((resolve) => { h.markHeld = resolve; });
  h.gate = new Promise((resolve) => { h.release = resolve; });
  h.handler = async (route) => {
    h.requests += 1;
    if (opts.commit) {
      const response = await route.fetch();
      h.status = response.status();
      try { h.body = await response.json(); } catch (e) { h.body = null; }
      h.markHeld();
      await h.gate;
      return route.fulfill({ response });
    }
    h.markHeld();
    await h.gate;
    opts.allowResourceError();
    return route.fulfill({ status: 503, contentType: "application/json",
      body: JSON.stringify({ error: { code: "server_unavailable",
        message: SERVER_503_MESSAGE } }) });
  };
  return h;
}

// ONE sub-leg of leg 7, start to finish. Parameterised rather than copied five
// times so the five sub-legs cannot silently drift apart: everything that
// differs between them — which Season, who arrives, how they arrive, and what
// fresh server truth under THEIR permissions must produce — is an explicit
// argument, and everything that must be identical is written once.
//
// `ctx`:
//   sub        the label used in every failure message ("7a" …)
//   season     the archived-Season fixture this sub-leg owns
//   reason     the LITERAL the departing operator types, searched for later
//   arrive     { username, password, via: "in-app" | "sign-out", label }
//   commit     release as a genuinely committed success (true) or a 503
//   expect     { effective, blocked, offersReopen } — what FRESH SERVER TRUTH
//              under the ARRIVING principal's permissions must produce
async function crossPrincipalRelease(ctx) {
  const { page, base, L, sub, arrive } = ctx;
  const step = `${L}/${sub}`;

  // ---- both operators persisted on the SAME Program/archived Season ----
  await persistContextFor(page, arrive, ctx.program, ctx.season.season, step);
  await reenter(page, base);
  await openFacilities(page, step);
  await armAnnouncements(page);

  const before = await readCard(page);
  if (before.effective !== "Reopen this season") {
    fail(`[${step}] the fixture does not offer the reopen path (effective `
      + `${JSON.stringify(before.effective)}, state "${before.state}") — there `
      + `is no card write to race`);
  }
  if (await seasonActiveFloorMet(page) !== false) {
    fail(`[${step}] the fixture's season_active floor is not unmet, so the `
      + `Season is not archived and this sub-leg proves nothing`);
  }
  await openConfirmWithReason(page, step, ctx.reason);
  await resetAnnouncements(page);

  const h = heldReopen({ commit: ctx.commit,
                         allowResourceError: ctx.allowResourceError });
  await page.route(REOPEN_RE, h.handler);
  await page.click("[data-setup-card-confirm-yes]");
  await h.held;
  await page.waitForTimeout(400);

  // ---- the DEPARTING principal's own view, for contrast ----
  const held = await readCard(page);
  if (held.state !== "pending") {
    fail(`[${step}] the card reads "${held.state}" with its own reopen POST `
      + `held`);
  }
  if (held.pendingText !== BUSY_TEXT) {
    fail(`[${step}] the initiating operator's own pending line reads `
      + `${JSON.stringify(held.pendingText)}, expected ${JSON.stringify(BUSY_TEXT)} `
      + `— without this the neutral copy asserted after the switch would not be `
      + `a substitution, it would be the only copy there ever was`);
  }
  const pendingGeneration = held.generation;
  const ledgerBefore = await readLedger(page);
  if (ledgerBefore.entries !== 1 || ledgerBefore.targets[0] !== ctx.season.season) {
    fail(`[${step}] the unresolved operation was not registered against the `
      + `target Season: ${JSON.stringify(ledgerBefore)}`);
  }

  // ================= THE AUTHENTICATED-USER CHANGE ==================
  await arrivePrincipal(page, arrive, step);
  await openFacilitiesNoSettle(page, `${step}/arrived`);
  await foreignPendingPresentation(page, `${step}/arrived`);
  // The announcement history is reset HERE, at the identity boundary, so every
  // sentence it holds from now on is one the ARRIVING principal was given.
  // Everything before this point was said to the departing operator in their
  // own session and is not a leak — the leak is a sentence crossing the
  // boundary, which is what the searches below measure.
  await resetAnnouncements(page);

  const who = await page.evaluate(() => (currentUser && currentUser.username) || null);
  if (who !== arrive.username) {
    fail(`[${step}/arrived] the signed-in principal is ${JSON.stringify(who)}, `
      + `not ${JSON.stringify(arrive.username)}`);
  }
  // THE REGISTRATION SURVIVED — half (1) of the split. Deleting it here is the
  // other way to get this round wrong: it would make the arriving principal's
  // card actionable and re-open the round-6 duplicate write while A's request
  // is still live.
  const ledgerOnB = await readLedger(page);
  if (ledgerOnB.entries !== 1 || ledgerOnB.targets[0] !== ctx.season.season) {
    fail(`[${step}/arrived] the unresolved operation against Season `
      + `${ctx.season.season} was forgotten on the identity change (ledger `
      + `${JSON.stringify(ledgerOnB)}). Signing out cancelled neither the HTTP `
      + `request nor the transaction behind it, so dropping the record hands `
      + `the arriving principal a control that fires a SECOND lifecycle write `
      + `against the same Season.`);
  }
  // ...and it is visibly FOREIGN.
  if (!(ledgerOnB.epochs[0] < ledgerOnB.currentEpoch)
      || ledgerOnB.principals[0] !== "admin"
      || ledgerOnB.currentPrincipal !== arrive.username) {
    fail(`[${step}/arrived] the surviving operation does not read as the `
      + `DEPARTING principal's: ${JSON.stringify(ledgerOnB)}. Without an epoch `
      + `that advanced and a principal that differs there is nothing for the `
      + `identity gate to compare, and the quarantine below is untestable.`);
  }
  // NON-VACUITY, and the review's own observation: the serialization rule
  // refuses the ARRIVING principal's render for this card+tuple, so the
  // generation counter cannot move. Generation equality therefore still says
  // "current", and the principal/session epoch is the ONLY thing standing
  // between the departing operator's response and this card.
  const counterOnB = await page.evaluate(() => cardGenerations["setup/facilities"]);
  if (counterOnB !== pendingGeneration) {
    fail(`[${step}/arrived] the card's generation counter moved `
      + `${pendingGeneration} -> ${counterOnB} under the arriving principal. `
      + `The reported defect depends on it NOT moving ("because the ledger `
      + `refuses the arriving user's render for the same tuple, the departing `
      + `identity's generation also remains current"), so this sub-leg is no `
      + `longer exercising the case it exists for.`);
  }

  // ---- NON-ACTIONABLE, and carrying NOTHING the departing operator entered ----
  const arrived = await readCard(page);
  if (!arrived.hasLanding || !arrived.hasActionsBox) {
    fail(`[${step}/arrived] the Facilities landing did not paint (landing `
      + `${arrived.hasLanding}, action container ${arrived.hasActionsBox}), so `
      + `"zero controls" would be true for the wrong reason`);
  }
  if (arrived.intentPending) {
    fail(`[${step}/arrived] a context switch has not reconciled, so any "no `
      + `controls" reading here would be the switch-intent withdrawal rather `
      + `than the unresolved write`);
  }
  if ((arrived.slotButtons || []).length !== 0
      || (arrived.actionButtons || []).length !== 0) {
    fail(`[${step}/arrived] the arriving principal's card is actionable (card `
      + `${JSON.stringify(arrived.slotButtons)}, landing `
      + `${JSON.stringify(arrived.actionButtons)}) while a lifecycle write `
      + `started by somebody else is still unresolved`);
  }
  if (arrived.busy !== "true") {
    fail(`[${step}/arrived] aria-busy is "${arrived.busy}" while an unresolved `
      + `write still owns this card`);
  }
  if (arrived.pendingText !== NEUTRAL_PENDING_TEXT) {
    fail(`[${step}/arrived] the arriving principal's pending line reads `
      + `${JSON.stringify(arrived.pendingText)}, expected the neutral `
      + `${JSON.stringify(NEUTRAL_PENDING_TEXT)}`);
  }
  if (arrived.reasonValue !== null || arrived.confirmError !== null) {
    fail(`[${step}/arrived] the departing operator's confirmation is standing `
      + `on the arriving principal's card (reason `
      + `${JSON.stringify(arrived.reasonValue)}, error `
      + `${JSON.stringify(arrived.confirmError)})`);
  }
  const leakedWhileHeld = await privateTextSightings(page, ctx.reason, true);
  if (leakedWhileHeld.length) {
    fail(`[${step}/arrived] the departing operator's typed reason `
      + `${JSON.stringify(ctx.reason)} is visible to ${arrive.username} in: `
      + `${JSON.stringify(leakedWhileHeld)}`);
  }
  // The stores ARE searched here: while the write is unresolved the arriving
  // principal's card offers no action at all, so the reopen action's declared
  // copy has no legitimate reason to be in any model this client holds.
  const leakedOperation = await privateTextSightings(page, BUSY_TEXT, true);
  if (leakedOperation.length) {
    fail(`[${step}/arrived] the departing operator's own operation copy `
      + `${JSON.stringify(BUSY_TEXT)} is visible to ${arrive.username} in: `
      + `${JSON.stringify(leakedOperation)} — the arriving principal must not `
      + `learn what the previous one was doing, only that this card is busy`);
  }

  // ---- every mutation entry point still blocked ----
  await attemptSecondWrite(page);
  if (h.requests !== 1) {
    fail(`[${step}/arrived] ${h.requests} reopen requests were issued for one `
      + `Season across an authenticated-user change while the first was still `
      + `unresolved`);
  }
  if ((await readLedger(page)).entries !== 1) {
    fail(`[${step}/arrived] the attempts to start a second write disturbed the `
      + `first operation's registration`);
  }
  // ...and nothing has been SAID to the arriving principal either, across the
  // switch, the navigation and every blocked mutation attempt.
  const spokenWhileHeld = await spoken(page);
  if (spokenWhileHeld.length) {
    fail(`[${step}/arrived] ${arrive.username} was told `
      + `${JSON.stringify(spokenWhileHeld)} while somebody else's write was `
      + `unresolved — they pressed nothing that could be answered`);
  }

  // ======================= THE RELEASE =========================
  await parkFocusOutsideCard(page, `${step}/arrived`);
  const focusBefore = await focusNow(page);
  await resetAnnouncements(page);
  const deliveriesBefore = ctx.deliveries();
  const progressBefore = ctx.progress();

  h.release();
  const deadline = Date.now() + 10000;
  while (ctx.deliveries() === deliveriesBefore && Date.now() < deadline) {
    await page.waitForTimeout(100);
  }
  if (ctx.deliveries() === deliveriesBefore) {
    fail(`[${step}] the released response never reached the page, so nothing `
      + `was actually settled`);
  }
  await page.waitForFunction(() => {
    const e = readCardState("setup/facilities");
    return e.state !== "pending" && e.state !== "loading";
  }, null, { timeout: 20000 })
    .catch(() => fail(`[${step}/settle] the card is STILL pending after the `
      + `departing principal's own request settled — settlement must drain the `
      + `ledger unconditionally, including across an identity change, or the `
      + `target tuple is blocked forever for everybody`));
  await page.waitForTimeout(900);
  await page.unroute(REOPEN_RE, h.handler);

  if (ctx.commit && (h.status !== 200 || !h.body || h.body.error)) {
    fail(`[${step}] the released response was not a committed success (status `
      + `${h.status}, body ${JSON.stringify(h.body)}) — a no-op would change `
      + `nothing whatever the client did`);
  }
  if (h.requests !== 1) {
    fail(`[${step}] ${h.requests} reopen requests in total for one Season`);
  }
  const ledgerAfter = await readLedger(page);
  if (ledgerAfter.entries !== 0 || ledgerAfter.cards.length !== 0) {
    fail(`[${step}/settle] the ledger did not drain across the identity `
      + `change: ${JSON.stringify(ledgerAfter)}`);
  }

  // ---- SILENT: nothing said, nothing focused ----
  const spokenAfter = await spoken(page);
  if (spokenAfter.length) {
    fail(`[${step}/settle] the settlement spoke into ${arrive.username}'s live `
      + `region: ${JSON.stringify(spokenAfter)}. The arriving principal `
      + `pressed nothing; a sentence here is the departing principal's `
      + `operation announcing itself in somebody else's session.`);
  }
  const focusAfter = await focusNow(page);
  if (focusAfter !== focusBefore) {
    fail(`[${step}/settle] focus moved on the departing principal's response `
      + `(${focusBefore} -> ${focusAfter})`);
  }
  // The reason and the server's message are searched everywhere, INCLUDING the
  // stores; the three declared sentences only where they could have been shown
  // or spoken — see privateTextSightings for why a card offering the reopen
  // action legitimately carries that action's own copy in its model.
  for (const [what, literal, stores] of [
      ["typed reason", ctx.reason, true],
      ["server error text", SERVER_503_MESSAGE, true],
      ["success sentence", DONE_TEXT, false],
      ["refresh sentence", REFRESH_TEXT, false],
      ["operation copy", BUSY_TEXT, false]]) {
    const sightings = await privateTextSightings(page, literal, stores);
    if (sightings.length) {
      fail(`[${step}/settle] the departing principal's ${what} `
        + `${JSON.stringify(literal)} reached ${arrive.username} in: `
        + `${JSON.stringify(sightings)}`);
    }
  }

  // ---- RECONCILED from a FRESH read, under the ARRIVING principal ----
  if (ctx.progress() <= progressBefore) {
    fail(`[${step}/settle] not one /api/v2/setup/progress read left the browser `
      + `after the settlement (${progressBefore} -> ${ctx.progress()}), so the `
      + `card was restored from state the client was HOLDING rather than `
      + `reconciled from what the server now says under this principal`);
  }
  const settledCard = await readCard(page);
  if (settledCard.state === "confirm") {
    fail(`[${step}/settle] the settlement restored a CONFIRM on the arriving `
      + `principal's card — the departing principal's held confirmation, `
      + `handed to somebody who never opened it`);
  }
  if (settledCard.effective !== ctx.expect.effective) {
    fail(`[${step}/settle] the reconciled card offers `
      + `${JSON.stringify(settledCard.effective)}, but fresh server truth under `
      + `${arrive.username}'s permissions is `
      + `${JSON.stringify(ctx.expect.effective)}`);
  }
  if (!!settledCard.blockedBecause !== ctx.expect.blocked) {
    fail(`[${step}/settle] blockedBecause is `
      + `${JSON.stringify(settledCard.blockedBecause)}, expected `
      + `${ctx.expect.blocked ? "a blocker" : "none"}`);
  }
  const offersReopen = (settledCard.actionButtons || [])
    .concat(settledCard.slotButtons || [])
    .some((b) => /reopen/i.test(b));
  if (offersReopen !== ctx.expect.offersReopen) {
    fail(`[${step}/settle] the reconciled card ${offersReopen ? "offers" : "does "
      + "not offer"} a reopen control (card `
      + `${JSON.stringify(settledCard.slotButtons)}, landing `
      + `${JSON.stringify(settledCard.actionButtons)}), expected `
      + `${ctx.expect.offersReopen ? "one" : "none"} for ${arrive.username}. A `
      + `control this role may not exercise is exactly the "restore controls `
      + `the arriving role did not initiate and may not be authorized to `
      + `exercise" the review forbids.`);
  }
  // The server's own answer, last: a 503 that never reached it leaves the
  // Season archived, and a committed success really did reopen it. Either way
  // the reconciled card above had to agree with THIS, not with held state.
  const floorMet = await seasonActiveFloorMet(page);
  if (floorMet !== ctx.commit) {
    fail(`[${step}/settle] the server reports season_active met=${floorMet}, `
      + `expected ${ctx.commit} — the release did not do what this sub-leg `
      + `arranged, so the reconcile assertions above measured the wrong truth`);
  }
}

// Re-enter the Facilities destination through the REAL sidebar navigation
// entry. Used after a context switch, where the previously painted landing
// may or may not have survived: every "zero controls on both surfaces"
// assertion below has to read a landing that is genuinely on screen, and
// hasLanding/hasActionsBox above then prove it was.
async function gotoFacilitiesLanding(page, step) {
  const nav = await page.$('.tab[data-setup-workflow-nav="facilities"]');
  if (!nav) fail(`[${step}] the real Facilities navigation control is not `
    + `present, so the destination cannot be re-entered the way an operator does`);
  await nav.click();
  await page.waitForSelector('[data-setup-workflow-landing="facilities"]',
    { timeout: 15000 })
    .catch(() => fail(`[${step}] the facilities landing never rendered`));
}

// The counterpart of settled(): wait until the card is showing the
// NON-ACTIONABLE PENDING presentation for `seasonId`, with the context switch
// already reconciled. Both conjuncts matter — a card that is merely mid-switch
// also paints no controls, and asserting on that would prove nothing about an
// unresolved write.
async function pendingPresentation(page, step, seasonId) {
  await page.waitForFunction((sid) => {
    if (contextSwitchIntentPending) return false;
    const e = readCardState("setup/facilities");
    return e.state === "pending" && e.identity && e.identity.season_id === sid;
  }, seasonId, { timeout: 15000 })
    .catch(() => fail(`[${step}] returning to the target of an unresolved `
      + `write did not restore the card's pending presentation — the round `
      + `trip destroyed the DOM and overwrote the committed model, so a card `
      + `that cannot be rebuilt from the operation itself comes back settled `
      + `and actionable with its first write still in flight`));
  // Let anything the return render kicked off land, so the assertions read a
  // quiesced page rather than a mid-flight one.
  await page.waitForTimeout(600);
}

// Try to start a SECOND reopen for this card, by pointer and by keyboard, the
// way an operator would if the control were back. Deliberately tolerant: in a
// correct build there is nothing to press and every attempt is a no-op, while
// in a build that restored the control this drives the duplicate write all the
// way through its confirmation to the POST. The caller asserts on the
// interceptor's request count afterwards, so this function never fails — it
// only tries.
async function attemptSecondWrite(page) {
  // ---- POINTER: whatever the landing now offers, followed through. ----
  const primary = await page.$('[data-setup-landing-actions="facilities"] .act.primary');
  if (primary) {
    await primary.click({ timeout: 3000 }).catch(() => {});
    const field = await page
      .waitForSelector('[data-setup-card-confirm-reason="facilities"]', { timeout: 3000 })
      .catch(() => null);
    if (field) {
      await field.fill("second write attempt").catch(() => {});
      const yes = await page.$("[data-setup-card-confirm-yes]");
      if (yes) await yes.click({ timeout: 3000 }).catch(() => {});
    }
  }
  // A real mouse press at the place the control occupies, so "nothing to hit"
  // is tested as a pointer fact and not only as a missing selector.
  for (const sel of ['[data-setup-card-slot="facilities"]',
                     '[data-setup-landing-actions="facilities"]']) {
    const box = await page.locator(sel).boundingBox().catch(() => null);
    if (box && box.width > 0 && box.height > 0) {
      await page.mouse.click(box.x + box.width / 2, box.y + box.height / 2)
        .catch(() => {});
    }
  }
  // ---- KEYBOARD: every control reachable inside either surface. ----
  const keyTargets = await page.$$('[data-setup-card-slot="facilities"] button, '
    + '[data-setup-landing-actions="facilities"] button');
  for (const el of keyTargets) {
    await el.focus().catch(() => {});
    await page.keyboard.press("Enter").catch(() => {});
    await page.keyboard.press(" ").catch(() => {});
  }
  // ...and on the pending line itself, which IS focusable by design and is
  // where a keyboard operator is standing while the write runs.
  const pend = await page.$('[data-setup-card-pending="facilities"]');
  if (pend) {
    await pend.focus().catch(() => {});
    await page.keyboard.press("Enter").catch(() => {});
    await page.keyboard.press(" ").catch(() => {});
  }
  // ---- CODE-LEVEL FLOOR, beyond what any input device can reach. ----
  // The two assertions above test that no CONTROL exists. This tests that the
  // card's own write entry points refuse even when called directly, so the
  // guarantee is "at most one unresolved write per card", not "at most one
  // button". Not a substitute for the pointer/keyboard attempts — an addition
  // under them.
  await page.evaluate(() => {
    try { askSetupCardConfirm("facilities", "primary:0"); } catch (e) {}
    try { resolveSetupCardConfirm("facilities", true); } catch (e) {}
    try { retrySetupWorkflowCard("facilities"); } catch (e) {}
  });
  await page.waitForTimeout(600);
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
    // Leg 5's two sub-legs each reopen (or try to reopen) their own Season for
    // the same reason legs 1-4 do: a reused Season would silently make the
    // next sub-leg's "still archived" precondition false.
    const s5 = await archivedSeasonFixture(page, shared.pa, shared.org, shared.venue, "WI Season A5");
    const s6 = await archivedSeasonFixture(page, shared.pa, shared.org, shared.venue, "WI Season A6");
    // Leg 6's three sub-legs, same rule: one Season each, because two of them
    // really are reopened and a reused Season would silently make the next
    // sub-leg's "still archived" precondition false.
    const s7 = await archivedSeasonFixture(page, shared.pa, shared.org, shared.venue, "WI Season A7");
    const s8 = await archivedSeasonFixture(page, shared.pa, shared.org, shared.venue, "WI Season A8");
    const s9 = await archivedSeasonFixture(page, shared.pa, shared.org, shared.venue, "WI Season A9");
    // Leg 7's five sub-legs, one Season each for the same reason as above: two
    // of them really are reopened, and a reused Season would silently make the
    // next sub-leg's "still archived" precondition false.
    const s10 = await archivedSeasonFixture(page, shared.pa, shared.org, shared.venue, "WI Season A10");
    const s11 = await archivedSeasonFixture(page, shared.pa, shared.org, shared.venue, "WI Season A11");
    const s12 = await archivedSeasonFixture(page, shared.pa, shared.org, shared.venue, "WI Season A12");
    const s13 = await archivedSeasonFixture(page, shared.pa, shared.org, shared.venue, "WI Season A13");
    const s14 = await archivedSeasonFixture(page, shared.pa, shared.org, shared.venue, "WI Season A14");

    // THE SECOND OPERATOR (#365 round 7). A real, separate account with the
    // SAME role as the first: the leak the review reports is not a permissions
    // failure and must not be excusable as one, so the arriving principal in
    // 7a-7d differs from the departing one in exactly one respect — who they
    // are. Sub-leg 7e then adds the lower-privilege case on top, using the
    // demo Arena Manager /api/demo/load already seeded.
    const madeAdminB = await apiPost(page, "/api/accounts",
      { username: SECOND_ADMIN, password: SECOND_ADMIN_PW,
        role: "league_admin", scope: {} });
    if (!madeAdminB || madeAdminB.error) {
      fail(`[${L}] could not create the second League Admin: `
        + `${JSON.stringify(madeAdminB)}`);
    }

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

    // ====== (5) SAME-TUPLE RENDER WHILE THE WRITE IS UNRESOLVED ========
    // The round-5 blocker. Everything above races a MOVED tuple; this races an
    // ordinary render under the tuple that has NOT moved, which is the case
    // the post-await identity guard cannot cover — by the time that guard
    // runs, a render-driven commit has already superseded the write's identity
    // and handed the operator back a control that fires a second one.
    const facilitiesGeneration = () =>
      page.evaluate(() => cardGenerations["setup/facilities"]);
    const facilitiesModel = () =>
      page.evaluate(() => JSON.stringify(cardStates["setup/facilities"]));

    // ---------------- (5a) held before the server, then 503 ---------------
    await apiPost(page, "/api/context", { program_id: shared.pa, season_id: s5.season });
    await reenter(page, base);
    await openFacilities(page, `${L}/5a`);
    await armAnnouncements(page);
    const before5 = await readCard(page);
    if (before5.effective !== "Reopen this season") {
      fail(`[${L}/5a] the fixture does not offer the reopen path (effective `
        + `${JSON.stringify(before5.effective)}, state "${before5.state}")`);
    }
    const rollup5Before = await snapshot(page);
    await openConfirmWithReason(page, `${L}/5a`, "render race, held write");
    await resetAnnouncements(page);

    // Held BEFORE the server sees it, and counted HERE — at the interceptor.
    // The harm the reviewer measured is two writes ISSUED for one Season, so
    // the count has to be taken where the client hands the request over, not
    // in page code and not from responses the server chose to accept.
    let reopen5Requests = 0;
    let releaseFive = () => {};
    let markFiveHeld = () => {};
    const fiveHeld = new Promise((resolve) => { markFiveHeld = resolve; });
    const fiveGate = new Promise((resolve) => { releaseFive = resolve; });
    const holdBeforeServer = async (route) => {
      reopen5Requests += 1;
      markFiveHeld();
      await fiveGate;
      resourceAllowance += 1;
      await route.fulfill({ status: 503, contentType: "application/json",
        body: JSON.stringify({ error: { code: "server_unavailable",
          message: "The server is temporarily unavailable (503). Please try "
            + "again in a moment." } }) });
    };
    await page.route(REOPEN_RE, holdBeforeServer);
    await page.click("[data-setup-card-confirm-yes]");
    await fiveHeld;
    await page.waitForTimeout(400);

    const held5 = await readCard(page);
    if (held5.state !== "pending") {
      fail(`[${L}/5a] the card reads "${held5.state}" with its own reopen POST `
        + `held before the server`);
    }
    // The EXACT number, kept for the comparison below. "state === pending"
    // after the render would also be true of a build that superseded the write
    // and merely painted the pending body back — and that build discards the
    // write's own response. Only the unchanged generation rules that out.
    const pendingGeneration = await facilitiesGeneration();
    if (pendingGeneration !== held5.generation) {
      fail(`[${L}/5a] the card's committed identity is generation `
        + `${held5.generation} but the counter is at ${pendingGeneration}, so `
        + `the pending model is already superseded before any render ran`);
    }
    const pendingModel = await facilitiesModel();
    const otherGensBefore5 = JSON.parse((await others()).gens);

    // The render, through the REAL Facilities navigation control, under the
    // UNCHANGED Program/Season.
    await sameTupleRenderThroughNav(page, `${L}/5a`);
    const tupleAfterRender = await page.evaluate(() =>
      JSON.stringify((contextOptions && contextOptions.selected) || null));
    if (tupleAfterRender !== rollup5Before.selected) {
      fail(`[${L}/5a] the context tuple changed across the re-entry `
        + `(${rollup5Before.selected} -> ${tupleAfterRender}), so this leg `
        + `raced a tuple move rather than a same-tuple render`);
    }

    // Attempt the second write BEFORE asserting, so a build that restored the
    // control is measured by the duplicate POST it actually issues rather than
    // stopped one assertion earlier.
    await attemptSecondWrite(page);

    if (reopen5Requests !== 1) {
      fail(`[${L}/5a] ${reopen5Requests} reopen requests were issued for one `
        + `Season while the first was still unresolved. The same-tuple render `
        + `superseded the PENDING write's identity and handed back an `
        + `actionable control, so the UI issued a second lifecycle mutation `
        + `while it could not know the outcome of the first — and the first `
        + `response is now discarded even though the server may commit it.`);
    }

    const after5 = await readCard(page);
    const generationAfter = await facilitiesGeneration();
    if (generationAfter !== pendingGeneration) {
      fail(`[${L}/5a] the same-tuple render advanced this card's generation `
        + `${pendingGeneration} -> ${generationAfter} while its own write was `
        + `unresolved. The write's response can no longer be recognised as `
        + `current, so it will be discarded even if the server commits it — `
        + `preserving PENDING means never issuing the generation, not painting `
        + `the pending body back afterwards.`);
    }
    if (after5.generation !== pendingGeneration || after5.state !== "pending") {
      fail(`[${L}/5a] the card is "${after5.state}" at generation `
        + `${after5.generation} after the render, expected "pending" at `
        + `${pendingGeneration}`);
    }
    if (after5.seasonId !== before5.seasonId || after5.programId !== before5.programId) {
      fail(`[${L}/5a] the pending identity's tuple changed across the render`);
    }
    const modelAfter = await facilitiesModel();
    if (modelAfter !== pendingModel) {
      fail(`[${L}/5a] the render replaced the PENDING card's committed model.\n`
        + `  before: ${pendingModel.slice(0, 900)}\n  after:  ${modelAfter.slice(0, 900)}`);
    }
    if (after5.busy !== "true") {
      fail(`[${L}/5a] aria-busy is "${after5.busy}" after the render, with the `
        + `write still unresolved`);
    }
    if (after5.pendingText !== BUSY_TEXT) {
      fail(`[${L}/5a] the pending copy is ${JSON.stringify(after5.pendingText)} `
        + `after the render, expected ${JSON.stringify(BUSY_TEXT)}`);
    }
    if (!after5.focusPending) {
      fail(`[${L}/5a] keyboard focus is on <${after5.focusTag}> after the `
        + `render — the repaint destroyed the pending line the operator was `
        + `standing on and left them nowhere, with a write still outstanding`);
    }
    if ((after5.slotButtons || []).length !== 0
        || (after5.actionButtons || []).length !== 0) {
      fail(`[${L}/5a] the render made the card actionable again (card `
        + `${JSON.stringify(after5.slotButtons)}, landing `
        + `${JSON.stringify(after5.actionButtons)}) with its first write still `
        + `unresolved — the reported "Reopen this season is actionable again"`);
    }
    const snap5 = await snapshot(page);
    if (snap5.rollup !== rollup5Before.rollup
        || snap5.rollupHtml !== rollup5Before.rollupHtml) {
      fail(`[${L}/5a] the completion count / next-task recommendation moved `
        + `across the render while the write was unresolved.\n  before: `
        + `${rollup5Before.rollup}\n  after:  ${snap5.rollup}`);
    }
    const held5Spoken = await spoken(page);
    if (held5Spoken.some((a) => a.text === DONE_TEXT || a.text === REFRESH_TEXT)) {
      fail(`[${L}/5a] a success sentence was spoken across the render, before `
        + `the response: ${JSON.stringify(held5Spoken)}`);
    }
    // NON-VACUITY. The refusal has to be a refusal of THIS card only. If the
    // render had not really run — or had been skipped wholesale — every
    // assertion above would pass for the wrong reason.
    const otherGensAfter5 = JSON.parse((await others()).gens);
    const advanced = Object.keys(otherGensAfter5).filter((k) =>
      k !== "setup/facilities" && otherGensAfter5[k] !== otherGensBefore5[k]);
    if (!advanced.length) {
      fail(`[${L}/5a] the re-entry advanced NO other card's generation, so no `
        + `render-driven commit actually ran and this leg proves nothing: `
        + `${JSON.stringify(otherGensBefore5)} -> ${JSON.stringify(otherGensAfter5)}`);
    }
    if (await seasonActiveFloorMet(page) !== false) {
      fail(`[${L}/5a] the server reports the Season active while its reopen is `
        + `still held before the server`);
    }

    // The held request, released as a failure: the round-4 recovery must be
    // exactly as it was — the render in between changed nothing about the
    // operation's identity, so its own response still moves it.
    releaseFive();
    await page.waitForFunction(() =>
      readCardState("setup/facilities").state === "confirm", null, { timeout: 15000 })
      .catch(() => fail(`[${L}/5a] the failed reopen never restored an `
        + `actionable confirmation after the same-tuple render — the render `
        + `superseded the write, so its own response was discarded`));
    await page.unroute(REOPEN_RE, holdBeforeServer);
    const recovered5 = await readCard(page);
    if (recovered5.reasonValue !== "render race, held write") {
      fail(`[${L}/5a] the restored confirmation lost the operator's reason: `
        + `${JSON.stringify(recovered5.reasonValue)}`);
    }
    if (!recovered5.confirmError || !/503|unavailable/i.test(recovered5.confirmError)) {
      fail(`[${L}/5a] the restored confirmation does not carry the server's own `
        + `message: ${JSON.stringify(recovered5.confirmError)}`);
    }
    if (!recovered5.focusReason) {
      fail(`[${L}/5a] focus landed on <${recovered5.focusTag}>, not the reason `
        + `field the operator has to correct`);
    }
    if (reopen5Requests !== 1) {
      fail(`[${L}/5a] ${reopen5Requests} reopen requests in total for one Season`);
    }
    if (await seasonActiveFloorMet(page) !== false) {
      fail(`[${L}/5a] the Season is active after a 503 that never reached the `
        + `server`);
    }

    // -------- (5b) the same race, released as a COMMITTED success --------
    await apiPost(page, "/api/context", { program_id: shared.pa, season_id: s6.season });
    await reenter(page, base);
    await openFacilities(page, `${L}/5b`);
    await armAnnouncements(page);
    const rollup6Before = await snapshot(page);
    await openConfirmWithReason(page, `${L}/5b`, "render race, committed write");
    await resetAnnouncements(page);

    // Held before the server here too (so the duplicate-write count stays
    // honest during the render), and only executed against the server on
    // RELEASE — so the single response this card finally receives is a
    // genuinely committed reopen, not a fabricated one.
    let reopen6Requests = 0;
    let releaseSix = () => {};
    let markSixHeld = () => {};
    const sixHeld = new Promise((resolve) => { markSixHeld = resolve; });
    const sixGate = new Promise((resolve) => { releaseSix = resolve; });
    let committedStatus = null;
    let committedBody = null;
    const holdThenCommit = async (route) => {
      reopen6Requests += 1;
      markSixHeld();
      await sixGate;
      const response = await route.fetch();      // the server takes it HERE
      committedStatus = response.status();
      try { committedBody = await response.json(); } catch (e) { committedBody = null; }
      await route.fulfill({ response });
    };
    await page.route(REOPEN_RE, holdThenCommit);
    await page.click("[data-setup-card-confirm-yes]");
    await sixHeld;
    await page.waitForTimeout(400);

    const held6 = await readCard(page);
    if (held6.state !== "pending") {
      fail(`[${L}/5b] the card reads "${held6.state}" with its reopen held`);
    }
    const pendingGeneration6 = await facilitiesGeneration();
    const pendingModel6 = await facilitiesModel();

    await sameTupleRenderThroughNav(page, `${L}/5b`);
    await attemptSecondWrite(page);
    if (reopen6Requests !== 1) {
      fail(`[${L}/5b] ${reopen6Requests} reopen requests were issued for one `
        + `Season across a same-tuple render`);
    }
    if (await facilitiesGeneration() !== pendingGeneration6) {
      fail(`[${L}/5b] the same-tuple render advanced this card's generation `
        + `while its write was unresolved`);
    }
    if (await facilitiesModel() !== pendingModel6) {
      fail(`[${L}/5b] the same-tuple render replaced the PENDING model`);
    }
    const after6 = await readCard(page);
    if ((after6.slotButtons || []).length !== 0
        || (after6.actionButtons || []).length !== 0) {
      fail(`[${L}/5b] the card is actionable again after the render (card `
        + `${JSON.stringify(after6.slotButtons)}, landing `
        + `${JSON.stringify(after6.actionButtons)})`);
    }
    if (after6.busy !== "true" || !after6.focusPending
        || after6.pendingText !== BUSY_TEXT) {
      fail(`[${L}/5b] the pending presentation did not survive the render `
        + `(aria-busy ${after6.busy}, focusPending ${after6.focusPending}, copy `
        + `${JSON.stringify(after6.pendingText)})`);
    }
    if ((await snapshot(page)).rollup !== rollup6Before.rollup) {
      fail(`[${L}/5b] the completion / next-task derivation moved across the `
        + `render while the write was unresolved`);
    }

    // "The one terminal response advances only this card": the comparison is
    // taken across the RELEASE, so the render's own (legitimate) commits to
    // the other five cards are already in the baseline.
    const others6Before = await others();
    releaseSix();
    await settled(page, `${L}/5b/done`, s6.season);
    await page.waitForTimeout(500);
    await page.unroute(REOPEN_RE, holdThenCommit);
    if (committedStatus !== 200 || !committedBody || committedBody.error) {
      fail(`[${L}/5b] the released response was not a committed success `
        + `(status ${committedStatus}, body ${JSON.stringify(committedBody)})`);
    }
    if (reopen6Requests !== 1) {
      fail(`[${L}/5b] ${reopen6Requests} reopen requests in total for one Season`);
    }
    const done6 = await readCard(page);
    if (done6.blockedBecause !== null || done6.effective !== "Add Ice") {
      fail(`[${L}/5b] the card did not advance on its own terminal response `
        + `after a same-tuple render (blockedBecause `
        + `${JSON.stringify(done6.blockedBecause)}, effective `
        + `${JSON.stringify(done6.effective)}) — the render superseded the `
        + `write's identity, so its committed success was discarded`);
    }
    if (done6.generation <= pendingGeneration6) {
      fail(`[${L}/5b] the terminal response did not advance this card's `
        + `generation (${pendingGeneration6} -> ${done6.generation})`);
    }
    const done6Spoken = await spoken(page);
    const successes6 = done6Spoken.filter((a) => a.text === DONE_TEXT);
    if (successes6.length !== 1) {
      fail(`[${L}/5b] expected EXACTLY ONE success announcement, got `
        + `${successes6.length} in ${JSON.stringify(done6Spoken)}`);
    }
    if (done6Spoken.some((a) => a.text === REFRESH_TEXT)) {
      fail(`[${L}/5b] the refresh's generic sentence was announced as well: `
        + `${JSON.stringify(done6Spoken)}`);
    }
    const others6After = await others();
    const gens6Before = JSON.parse(others6Before.gens);
    const gens6After = JSON.parse(others6After.gens);
    for (const key of Object.keys(others6Before.models)) {
      if (gens6Before[key] !== gens6After[key]) {
        fail(`[${L}/5b] the terminal response moved "${key}"'s generation `
          + `(${gens6Before[key]} -> ${gens6After[key]}) — it may advance only `
          + `its own card`);
      }
      if (others6Before.models[key] !== others6After.models[key]) {
        fail(`[${L}/5b] the terminal response mutated adjacent card "${key}"`);
      }
    }

    // ====== (6) A → B → A ROUND TRIP WHILE THE WRITE IS UNRESOLVED ======
    // Leg 5 races a render under a tuple that never moves; legs 2/3 race a
    // tuple that moves and stays moved. This races a tuple that moves AND
    // COMES BACK before the response settles — the case the previous round
    // documented as "abandoned" and dismissed. Navigation cancelled neither
    // the HTTP request nor the server transaction behind it, so the operation
    // is still live and the card it targets must still be non-actionable.
    //
    // Progress reads are counted where the BROWSER hands them over. "The card
    // was reconciled from FRESH server truth" is a claim about a request that
    // actually left after the settlement, so it is measured there rather than
    // inferred from the state that happened to result.
    let progressReads = 0;
    page.on("request", (rq) => {
      if (/\/api\/v2\/setup\/progress$/.test(new URL(rq.url()).pathname)) {
        progressReads += 1;
      }
    });

    // ------------- (6a) round trip, then released as a 503 --------------
    await apiPost(page, "/api/context", { program_id: shared.pa, season_id: s7.season });
    await reenter(page, base);
    await openFacilities(page, `${L}/6a`);
    await armAnnouncements(page);
    const before6a = await readCard(page);
    if (before6a.effective !== "Reopen this season") {
      fail(`[${L}/6a] the fixture does not offer the reopen path (effective `
        + `${JSON.stringify(before6a.effective)}, state "${before6a.state}") — `
        + `there is no card write to race`);
    }
    const rollup6aBefore = await snapshot(page);
    await openConfirmWithReason(page, `${L}/6a`, "round trip, held write");
    await resetAnnouncements(page);

    // Held BEFORE the server, and counted AT THE INTERCEPTOR: the harm is two
    // writes ISSUED for one Season, not two the server chose to accept.
    let reopen7Requests = 0;
    let releaseSeven = () => {};
    let markSevenHeld = () => {};
    const sevenHeld = new Promise((resolve) => { markSevenHeld = resolve; });
    const sevenGate = new Promise((resolve) => { releaseSeven = resolve; });
    const holdRoundTripFail = async (route) => {
      reopen7Requests += 1;
      markSevenHeld();
      await sevenGate;
      resourceAllowance += 1;
      await route.fulfill({ status: 503, contentType: "application/json",
        body: JSON.stringify({ error: { code: "server_unavailable",
          message: "The server is temporarily unavailable (503). Please try "
            + "again in a moment." } }) });
    };
    await page.route(REOPEN_RE, holdRoundTripFail);
    await page.click("[data-setup-card-confirm-yes]");
    await sevenHeld;
    await page.waitForTimeout(400);

    const held7 = await readCard(page);
    if (held7.state !== "pending") {
      fail(`[${L}/6a] the card reads "${held7.state}" with its own reopen POST `
        + `held before the server`);
    }
    const pendingGeneration7 = held7.generation;

    // ---- A → B through the REAL #ctx-select, without releasing. ----
    await switchContext(page, shared.pb, shared.sb, `${L}/6a`);
    await gotoFacilitiesLanding(page, `${L}/6a/B`);
    await settled(page, `${L}/6a/B`, shared.sb);
    await page.waitForTimeout(300);

    const bCard6 = await readCard(page);
    if (bCard6.seasonId !== shared.sb || bCard6.programId !== shared.pb) {
      fail(`[${L}/6a/B] B's card is bound to `
        + `${JSON.stringify({ p: bCard6.programId, s: bCard6.seasonId })}, not `
        + `to Program/Season B`);
    }
    if (bCard6.busy !== "false" || bCard6.intentPending) {
      fail(`[${L}/6a/B] B's card is not idle (aria-busy ${bCard6.busy}, switch `
        + `intent ${bCard6.intentPending}) — A's unresolved write is being `
        + `charged to the tuple the operator switched TO`);
    }
    if (!bCard6.hasLanding || !bCard6.hasActionsBox) {
      fail(`[${L}/6a/B] B's Facilities landing did not paint (landing `
        + `${bCard6.hasLanding}, action container ${bCard6.hasActionsBox})`);
    }
    if (!(bCard6.actionButtons || []).length) {
      fail(`[${L}/6a/B] B's card offers NO action `
        + `(${JSON.stringify(bCard6.actionButtons)}) while A's write is `
        + `unresolved — the refusal was made global-by-card and froze the `
        + `Program/Season the operator switched to, which is not the rule: B `
        + `has no unresolved operation of its own and must render normally`);
    }
    // THE registration, still standing. Deleting it here is the reported
    // defect, and it is caught HERE — before any of its consequences — so the
    // failure names the cause rather than a symptom three steps downstream.
    const ledgerOnB = await readLedger(page);
    if (ledgerOnB.entries !== 1 || ledgerOnB.targets[0] !== s7.season) {
      fail(`[${L}/6a/B] the unresolved operation against Season A7 was `
        + `forgotten the moment another tuple became current (ledger `
        + `${JSON.stringify(ledgerOnB)}). Navigation cancelled neither the `
        + `request nor its server transaction, so the operation is still live `
        + `and its record must outlive the view that started it.`);
    }

    // ---- B → A, back onto the operation's own target. ----
    await switchContext(page, shared.pa, s7.season, `${L}/6a`);
    await gotoFacilitiesLanding(page, `${L}/6a/back`);
    await pendingPresentation(page, `${L}/6a/back`, s7.season);

    const back7 = await readCard(page);
    if (back7.generation !== pendingGeneration7) {
      fail(`[${L}/6a/back] the returned card reads generation `
        + `${back7.generation}, not the ${pendingGeneration7} it entered `
        + `PENDING with — the presentation was rebuilt from something other `
        + `than the operation itself`);
    }
    // NON-VACUITY, and the point of the whole leg: the card's COUNTER moved
    // while the operator was away, so the initiating UI identity is genuinely
    // stale and the settlement below must take the stale-identity path. If
    // these were equal, leg 6 would be re-testing leg 5.
    if (!(back7.counter > pendingGeneration7)) {
      fail(`[${L}/6a/back] the card's generation counter is ${back7.counter}, `
        + `not past the pending ${pendingGeneration7}, so the round trip never `
        + `superseded the initiating identity and this leg is not exercising `
        + `the stale-identity path at all`);
    }
    if (back7.seasonId !== s7.season || back7.programId !== shared.pa) {
      fail(`[${L}/6a/back] the restored pending identity is bound to `
        + `${JSON.stringify({ p: back7.programId, s: back7.seasonId })}, not to `
        + `the Program/Season the write targets`);
    }
    if (back7.busy !== "true") {
      fail(`[${L}/6a/back] aria-busy is "${back7.busy}" on the target of a `
        + `write that has not settled; assistive technology is told the region `
        + `is idle while a lifecycle mutation is still in flight`);
    }
    if (back7.pendingText !== BUSY_TEXT) {
      fail(`[${L}/6a/back] the pending line reads `
        + `${JSON.stringify(back7.pendingText)}, expected `
        + `${JSON.stringify(BUSY_TEXT)}`);
    }
    if (back7.intentPending) {
      fail(`[${L}/6a/back] the context switch has not reconciled yet, so any `
        + `"no controls" reading here would be the switch-intent withdrawal `
        + `rather than the unresolved write`);
    }
    if (!back7.hasLanding || !back7.hasActionsBox) {
      fail(`[${L}/6a/back] the Facilities landing did not paint on the return `
        + `(landing ${back7.hasLanding}, action container `
        + `${back7.hasActionsBox}), so "zero controls" would be true for the `
        + `wrong reason`);
    }
    if ((back7.slotButtons || []).length !== 0
        || (back7.actionButtons || []).length !== 0) {
      fail(`[${L}/6a/back] the returned card is actionable again (card `
        + `${JSON.stringify(back7.slotButtons)}, landing `
        + `${JSON.stringify(back7.actionButtons)}) with its first write still `
        + `unresolved — the reported "A now renders READY ... with Reopen this `
        + `season actionable, although its first write is still unresolved"`);
    }
    const backSnap7 = await snapshot(page);
    if (backSnap7.rollup !== rollup6aBefore.rollup) {
      fail(`[${L}/6a/back] the completion count / next-task recommendation `
        + `moved across the round trip while the write was unresolved.\n`
        + `  before: ${rollup6aBefore.rollup}\n  after:  ${backSnap7.rollup}`);
    }
    // Attempted BEFORE the count is asserted, so a build that restored the
    // control is measured by the duplicate POST it actually issues rather than
    // stopped one assertion earlier.
    await attemptSecondWrite(page);
    if (reopen7Requests !== 1) {
      fail(`[${L}/6a/back] ${reopen7Requests} reopen requests were issued for `
        + `one Season across an A → B → A round trip while the first was still `
        + `unresolved. Leaving the tuple destroyed the registration, so the `
        + `return rendered the card actionable and the UI issued a second `
        + `lifecycle mutation before it could know the outcome of the first — `
        + `duplicated lifecycle and audit effects, from neither write.`);
    }
    if ((await readLedger(page)).entries !== 1) {
      fail(`[${L}/6a/back] the attempts to start a second write disturbed the `
        + `first operation's registration`);
    }

    // ---- released as a 503, then RECONCILED FROM FRESH SERVER TRUTH ----
    const deliveriesBefore7 = reopenDeliveries;
    const progressBefore7 = progressReads;
    releaseSeven();
    const deadline7 = Date.now() + 10000;
    while (reopenDeliveries === deliveriesBefore7 && Date.now() < deadline7) {
      await page.waitForTimeout(100);
    }
    if (reopenDeliveries === deliveriesBefore7) {
      fail(`[${L}/6a] the released 503 never reached the page, so nothing was `
        + `actually settled`);
    }
    await page.waitForFunction(() => {
      const e = readCardState("setup/facilities");
      return e.state !== "pending" && e.state !== "loading";
    }, null, { timeout: 15000 })
      .catch(() => fail(`[${L}/6a/settle] the card is STILL pending after its `
        + `own request settled. The registration is only released on the path `
        + `where the initiating identity survived, so an operation the `
        + `operator stepped away from leaves its target blocked forever.`));
    await page.waitForTimeout(700);
    await page.unroute(REOPEN_RE, holdRoundTripFail);

    if (progressReads <= progressBefore7) {
      fail(`[${L}/6a/settle] not one /api/v2/setup/progress read left the `
        + `browser after the settlement (${progressBefore7} -> `
        + `${progressReads}), so the card was restored from state the client `
        + `was HOLDING rather than reconciled from what the server now says. `
        + `After a round trip the held state is exactly what the client is no `
        + `longer entitled to trust.`);
    }
    const settled7 = await readCard(page);
    if (settled7.state === "confirm" || settled7.reasonValue !== null) {
      fail(`[${L}/6a/settle] the settlement restored the HELD confirmation `
        + `(state "${settled7.state}", reason `
        + `${JSON.stringify(settled7.reasonValue)}) instead of reconciling. A `
        + `503 delivered after a round trip does not prove the write did not `
        + `commit — navigation cancelled nothing — so handing back the `
        + `operator's own pre-write state is a claim the client cannot support.`);
    }
    if (settled7.effective !== "Reopen this season") {
      fail(`[${L}/6a/settle] the reconciled card offers `
        + `${JSON.stringify(settled7.effective)}, but the server still reports `
        + `Season A7 archived, so the REAL recovery action is "Reopen this `
        + `season"`);
    }
    if (!(settled7.actionButtons || []).some((b) => /reopen this season/i.test(b))) {
      fail(`[${L}/6a/settle] the reconciled landing offers no recovery control: `
        + `${JSON.stringify(settled7.actionButtons)}`);
    }
    const spoken7 = await spoken(page);
    if (spoken7.some((a) => a.text === DONE_TEXT)) {
      fail(`[${L}/6a/settle] a completion sentence was announced for a write `
        + `whose outcome the client could not know: ${JSON.stringify(spoken7)}`);
    }
    if (spoken7.some((a) => /503|unavailable|could not be reopened/i.test(a.text))) {
      fail(`[${L}/6a/settle] the discarded response's own error text was `
        + `announced: ${JSON.stringify(spoken7)}. That response was superseded `
        + `by the round trip; the card was reconciled from the server instead, `
        + `and speaking the stale outcome tells the operator something the `
        + `client did not establish.`);
    }
    const ledgerAfter7 = await readLedger(page);
    if (ledgerAfter7.entries !== 0 || ledgerAfter7.cards.length !== 0) {
      fail(`[${L}/6a/settle] the ledger did not drain on settlement: `
        + `${JSON.stringify(ledgerAfter7)}`);
    }
    if (reopen7Requests !== 1) {
      fail(`[${L}/6a] ${reopen7Requests} reopen requests in total for one Season`);
    }
    if (await seasonActiveFloorMet(page) !== false) {
      fail(`[${L}/6a] the Season is active after a 503 that never reached the `
        + `server`);
    }

    // ------- (6b) the same round trip, released as a COMMITTED success -------
    await apiPost(page, "/api/context", { program_id: shared.pa, season_id: s8.season });
    await reenter(page, base);
    await openFacilities(page, `${L}/6b`);
    await armAnnouncements(page);
    await openConfirmWithReason(page, `${L}/6b`, "round trip, committed write");
    await resetAnnouncements(page);

    let reopen8Requests = 0;
    let releaseEight = () => {};
    let markEightHeld = () => {};
    const eightHeld = new Promise((resolve) => { markEightHeld = resolve; });
    const eightGate = new Promise((resolve) => { releaseEight = resolve; });
    let committed8Status = null;
    let committed8Body = null;
    // Held before the server (so the duplicate-write count stays honest across
    // the round trip) and executed against the server only on RELEASE — by
    // which time the operator is back on the target, so the single response
    // this card receives is a genuinely committed reopen.
    const holdRoundTripCommit = async (route) => {
      reopen8Requests += 1;
      markEightHeld();
      await eightGate;
      const response = await route.fetch();
      committed8Status = response.status();
      try { committed8Body = await response.json(); } catch (e) { committed8Body = null; }
      await route.fulfill({ response });
    };
    await page.route(REOPEN_RE, holdRoundTripCommit);
    await page.click("[data-setup-card-confirm-yes]");
    await eightHeld;
    await page.waitForTimeout(400);
    const pendingGeneration8 = (await readCard(page)).generation;

    await switchContext(page, shared.pb, shared.sb, `${L}/6b`);
    await gotoFacilitiesLanding(page, `${L}/6b/B`);
    await settled(page, `${L}/6b/B`, shared.sb);
    const bCard8 = await readCard(page);
    if (!(bCard8.actionButtons || []).length || bCard8.busy !== "false") {
      fail(`[${L}/6b/B] B did not render normally while A's write was `
        + `unresolved (actions ${JSON.stringify(bCard8.actionButtons)}, `
        + `aria-busy ${bCard8.busy})`);
    }
    await switchContext(page, shared.pa, s8.season, `${L}/6b`);
    await gotoFacilitiesLanding(page, `${L}/6b/back`);
    await pendingPresentation(page, `${L}/6b/back`, s8.season);

    const back8 = await readCard(page);
    if (back8.generation !== pendingGeneration8 || !(back8.counter > pendingGeneration8)) {
      fail(`[${L}/6b/back] the returned card is at generation `
        + `${back8.generation} with counter ${back8.counter}, expected the `
        + `pending ${pendingGeneration8} behind an advanced counter`);
    }
    if ((back8.slotButtons || []).length !== 0
        || (back8.actionButtons || []).length !== 0) {
      fail(`[${L}/6b/back] the returned card is actionable with its write `
        + `still unresolved (card ${JSON.stringify(back8.slotButtons)}, landing `
        + `${JSON.stringify(back8.actionButtons)})`);
    }
    await attemptSecondWrite(page);
    if (reopen8Requests !== 1) {
      fail(`[${L}/6b/back] ${reopen8Requests} reopen requests were issued for `
        + `one Season across the round trip`);
    }

    const progressBefore8 = progressReads;
    releaseEight();
    await page.waitForFunction(() => {
      const e = readCardState("setup/facilities");
      return e.state !== "pending" && e.state !== "loading";
    }, null, { timeout: 20000 })
      .catch(() => fail(`[${L}/6b/settle] the card never left PENDING after its `
        + `committed success settled — the registration outlived the request`));
    await page.waitForTimeout(700);
    await page.unroute(REOPEN_RE, holdRoundTripCommit);
    if (committed8Status !== 200 || !committed8Body || committed8Body.error) {
      fail(`[${L}/6b] the released response was not a committed success `
        + `(status ${committed8Status}, body ${JSON.stringify(committed8Body)}) `
        + `— the "advance to the next valid action" assertion below would then `
        + `be measuring nothing`);
    }
    if (progressReads <= progressBefore8) {
      fail(`[${L}/6b/settle] no progress read left the browser after the `
        + `settlement, so the card was not reconciled from server truth`);
    }
    const settled8 = await readCard(page);
    if (settled8.blockedBecause !== null || settled8.effective !== "Add Ice") {
      fail(`[${L}/6b/settle] the reconciled card did not advance to the next `
        + `valid action (blockedBecause `
        + `${JSON.stringify(settled8.blockedBecause)}, effective `
        + `${JSON.stringify(settled8.effective)}). The server has committed the `
        + `reopen, so "Add Ice" is what fresh truth says — and it is a state no `
        + `HELD value could have produced, because everything this client was `
        + `carrying was blocked by season_active.`);
    }
    const spoken8 = await spoken(page);
    if (spoken8.some((a) => a.text === DONE_TEXT)) {
      fail(`[${L}/6b/settle] a completion sentence was announced for a write `
        + `the client had stopped tracking: ${JSON.stringify(spoken8)}`);
    }
    if (reopen8Requests !== 1) {
      fail(`[${L}/6b] ${reopen8Requests} reopen requests in total for one Season`);
    }
    const ledgerAfter8 = await readLedger(page);
    if (ledgerAfter8.entries !== 0) {
      fail(`[${L}/6b/settle] the ledger did not drain: `
        + `${JSON.stringify(ledgerAfter8)}`);
    }

    // ---- (6c) released while the operator is STILL ON B ----
    // The drain has to happen on the path where NOTHING else does: the
    // response lands, the round-4 rule forbids it from touching anything on
    // the tuple that is current, and it returns. If the registration is
    // released only where the initiating identity survived, the target tuple
    // is blocked the next time the operator looks at it — forever.
    await apiPost(page, "/api/context", { program_id: shared.pa, season_id: s9.season });
    await reenter(page, base);
    await openFacilities(page, `${L}/6c`);
    await armAnnouncements(page);
    const rollup6cBefore = await snapshot(page);
    await openConfirmWithReason(page, `${L}/6c`, "released while away");

    let reopen9Requests = 0;
    let releaseNine = () => {};
    let markNineHeld = () => {};
    const nineHeld = new Promise((resolve) => { markNineHeld = resolve; });
    const nineGate = new Promise((resolve) => { releaseNine = resolve; });
    const holdAwayFail = async (route) => {
      reopen9Requests += 1;
      markNineHeld();
      await nineGate;
      resourceAllowance += 1;
      await route.fulfill({ status: 503, contentType: "application/json",
        body: JSON.stringify({ error: { code: "server_unavailable",
          message: "The server is temporarily unavailable (503). Please try "
            + "again in a moment." } }) });
    };
    await page.route(REOPEN_RE, holdAwayFail);
    await page.click("[data-setup-card-confirm-yes]");
    await nineHeld;
    await page.waitForTimeout(400);

    await switchContext(page, shared.pb, shared.sb, `${L}/6c`);
    await gotoFacilitiesLanding(page, `${L}/6c/B`);
    await settled(page, `${L}/6c/B`, shared.sb);
    await page.waitForTimeout(300);
    // updateToast()'s 4-second auto-clear is a DISMISSAL, not a mutation.
    // Wait for the region to be quiet before the baseline, so a dismissal
    // landing between the two snapshots can never be read as the released
    // response having changed B.
    await page.waitForFunction(() => {
      const root = document.getElementById("toast-root");
      return !root || root.hidden;
    }, null, { timeout: 15000 }).catch(() => {});
    const b9Before = await snapshot(page);
    assertDiffers(rollup6cBefore, b9Before, L, "6c/B");
    await resetAnnouncements(page);
    const deliveriesBefore9 = reopenDeliveries;

    releaseNine();
    const deadline9 = Date.now() + 10000;
    while (reopenDeliveries === deliveriesBefore9 && Date.now() < deadline9) {
      await page.waitForTimeout(100);
    }
    if (reopenDeliveries === deliveriesBefore9) {
      fail(`[${L}/6c] the released response never reached the page, so nothing `
        + `was actually settled`);
    }
    await page.waitForTimeout(1500);
    await page.unroute(REOPEN_RE, holdAwayFail);

    // The round-4 rule, unchanged: a response landing on another tuple changes
    // NOTHING there — not the DOM, not focus, not the live region, not the
    // completion line or the next task.
    const b9After = await snapshot(page);
    assertSame(b9Before, b9After, L, "6c");
    const b9Spoken = await spoken(page);
    if (b9Spoken.some((a) => /unavailable|503|reopen/i.test(a.text))) {
      fail(`[${L}/6c] the settled response spoke into B's live region: `
        + `${JSON.stringify(b9Spoken)}`);
    }
    // ...and the ledger drains ANYWAY, read directly rather than inferred.
    const ledgerAfter9 = await readLedger(page);
    if (ledgerAfter9.entries !== 0 || ledgerAfter9.cards.length !== 0) {
      fail(`[${L}/6c] the operation settled while the operator was on B, but `
        + `its registration survived: ${JSON.stringify(ledgerAfter9)}. `
        + `Settlement must release the record even when the initiating UI `
        + `identity is stale — otherwise the target tuple is blocked forever `
        + `by an operation that has already finished.`);
    }

    // A later return to the target reads CURRENT SERVER TRUTH — it does not
    // sit pending behind a registration nothing will ever retire.
    await switchContext(page, shared.pa, s9.season, `${L}/6c/back`);
    await gotoFacilitiesLanding(page, `${L}/6c/back`);
    await settled(page, `${L}/6c/back`, s9.season);
    const back9 = await readCard(page);
    if (back9.effective !== "Reopen this season" || !back9.blockedBecause) {
      fail(`[${L}/6c/back] returning to Season A9 did not read the server's `
        + `current state (effective ${JSON.stringify(back9.effective)}, `
        + `blockedBecause ${JSON.stringify(back9.blockedBecause)}) — the 503 `
        + `never reached the server, so the Season is still archived and the `
        + `card must say so`);
    }
    if (!(back9.actionButtons || []).length) {
      fail(`[${L}/6c/back] the card came back non-actionable although its `
        + `operation settled while the operator was away: `
        + `${JSON.stringify(back9.actionButtons)}`);
    }
    if (reopen9Requests !== 1) {
      fail(`[${L}/6c] ${reopen9Requests} reopen requests in total for one Season`);
    }
    if (await seasonActiveFloorMet(page) !== false) {
      fail(`[${L}/6c] the Season is active after a 503 that never reached the `
        + `server`);
    }

    // ==== (7) AN AUTHENTICATED-USER CHANGE WHILE THE WRITE IS UNRESOLVED ====
    // Legs 2/3/5/6 move the tuple, or refuse to. This leg moves the PERSON and
    // nothing else: card, target Season, tuple and generation are identical on
    // both sides of every switch below, and both operators are persisted on
    // that same Program/archived Season, so each sub-leg can fail for exactly
    // one reason. See the file header for the full statement of the defect.
    const legSeven = {
      page: page, base: base, L: L, program: shared.pa,
      allowResourceError: () => { resourceAllowance += 1; },
      deliveries: () => reopenDeliveries,
      progress: () => progressReads,
    };
    const ADMIN_B = { username: SECOND_ADMIN, password: SECOND_ADMIN_PW,
                      via: "in-app" };
    const ADMIN_B_FORM = { username: SECOND_ADMIN, password: SECOND_ADMIN_PW,
                           via: "sign-out" };
    const ARENA_B = { username: ARENA_USER, password: ARENA_PW, via: "in-app" };

    // (7a) in-app persona switch, released as a 503. The reviewer's exact
    // reproduction, down to the reason string.
    await crossPrincipalRelease(Object.assign({}, legSeven, {
      sub: "7a", season: s10, reason: "first write held", arrive: ADMIN_B,
      commit: false,
      // The 503 never reached the server, so the Season is still archived and
      // fresh truth under another League Admin is the REAL recovery action.
      expect: { effective: "Reopen this season", blocked: true,
                offersReopen: true },
    }));

    // (7b) the same switch, released as a genuinely COMMITTED success. "Add
    // Ice" is a state no held value could have produced: everything the
    // departing operator was carrying was blocked by season_active.
    await crossPrincipalRelease(Object.assign({}, legSeven, {
      sub: "7b", season: s11, reason: "committed under the first admin",
      arrive: ADMIN_B, commit: true,
      expect: { effective: "Add Ice", blocked: false, offersReopen: false },
    }));

    // (7c) a REAL sign-out through the header control, then a REAL sign-in
    // through the login form — a different transition from (a): it passes
    // through setUser(null) first, ends the session server-side, and fires
    // resetTransientUiState() twice.
    await crossPrincipalRelease(Object.assign({}, legSeven, {
      sub: "7c", season: s12, reason: "held across a real sign-out",
      arrive: ADMIN_B_FORM, commit: false,
      expect: { effective: "Reopen this season", blocked: true,
                offersReopen: true },
    }));

    // (7d) the same real sign-out/sign-in, released as a committed success.
    await crossPrincipalRelease(Object.assign({}, legSeven, {
      sub: "7d", season: s13, reason: "committed across a real sign-out",
      arrive: ADMIN_B_FORM, commit: true,
      expect: { effective: "Add Ice", blocked: false, offersReopen: false },
    }));

    // (7e) a LOWER-PRIVILEGE arriving role on the same authorized tuple.
    // Reopening a Season is MANAGE_SETUP and an Arena Manager holds
    // MANAGE_ARENA only, so the reconciled card must offer NO action at all —
    // restoring the departing League Admin's confirmation here would hand this
    // role a control that is a 403 in waiting, on a Season they never touched.
    await crossPrincipalRelease(Object.assign({}, legSeven, {
      sub: "7e", season: s14, reason: "held for the arena manager",
      arrive: ARENA_B, commit: false,
      expect: { effective: null, blocked: true, offersReopen: false },
    }));

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
      + `exactly ONE success announcement, said last and after the response. `
      + `With the request held BEFORE the server and the Facilities destination `
      + `re-entered through its own navigation control under the UNCHANGED `
      + `Program/Season, the card keeps the EXACT generation it entered PENDING `
      + `with, its committed model byte-for-byte, aria-busy, its pending copy `
      + `and focus, an unchanged completion/next roll-up and zero actions on `
      + `both surfaces — while the same render advances the other workflow `
      + `cards normally — and pointer, keyboard and direct code-level attempts `
      + `to start a second write leave the interceptor's reopen count at exactly `
      + `one; the held 503 then still restores the reason/error/focus recovery, `
      + `and a held-then-committed success advances this card and only this card. `
      + `Switching A -> B -> A through the real #ctx-select without releasing, B `
      + `renders normally — its own Season, aria-busy false, its own action `
      + `offered — while the operation's registration goes on existing, and the `
      + `returned card comes back rebuilt from that registration alone: pending `
      + `at the generation it entered PENDING with, behind a counter that has `
      + `moved past it, aria-busy true, an unchanged completion/next roll-up and `
      + `zero controls on both the card body and the landing action groups, with `
      + `pointer, keyboard and direct code-level attempts leaving the reopen `
      + `count at exactly one. Settlement then drains the registration on every `
      + `path and reconciles the returned card from a FRESH server read rather `
      + `than from held state — a 503 lands on the real recovery action with the `
      + `Season still archived server-side, a committed success advances to "Add `
      + `Ice", and neither speaks a completion sentence or the discarded `
      + `response's own text. Released while the operator is still on B, B stays `
      + `snapshot-identical and silent, the ledger drains all the same, and a `
      + `later return to the target reads current server truth instead of `
      + `sitting blocked behind a registration nothing would retire. And with `
      + `the write held while the AUTHENTICATED USER changes — through the `
      + `app's own signIn() with no reload, and through a real sign-out `
      + `followed by a sign-in through the login form — a second League Admin `
      + `persisted on the very same Program/archived Season inherits a card `
      + `that is non-actionable and NEUTRAL: the generic "waiting on the `
      + `server" line, no reason, no error, no counts, and neither the typed `
      + `reason nor the reopen's own busy copy anywhere in the body text, the `
      + `markup, any field value, the live region, the announcement history, `
      + `the committed models or the ledger's held models — while the `
      + `registration survives, reads as the DEPARTING principal's (an older `
      + `epoch, their username), the generation counter stays exactly where `
      + `the write left it, and pointer, keyboard and direct code-level `
      + `attempts leave the reopen count at one. Releasing as a 503 and as a `
      + `genuinely committed success both drain the ledger, say NOTHING at `
      + `all, move no focus off a control parked outside the card, and `
      + `reconcile from a fresh progress read observed leaving the browser — `
      + `landing on the real recovery action when the Season is still archived `
      + `and on "Add Ice" when the server really did reopen it. An Arena `
      + `Manager arriving on the same authorized tuple is reconciled the same `
      + `way and is offered NO action at all, so no control the arriving role `
      + `may not exercise is ever restored.`);
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
