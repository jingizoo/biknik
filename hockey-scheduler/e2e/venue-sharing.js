// Multi-operator venue-sharing regression (#254 E2b, epic #233 Slice E
// acceptance criteria): proves, through real UI surfaces only, the three
// remaining unchecked Slice E acceptance boxes that allowed-venues.js's
// single-season/single-venue journey does not exercise:
//
//   - One Season can use multiple Venues.
//   - One Venue can host multiple independent Programs/Seasons
//     (the Twin Rinks-managed + external-IHSH-hosted scenario from #233).
//   - The facility owner and each Program's operator remain independent
//     organizations, end to end, after the legacy Venue->Program bridge's
//     removal in E2a.
//
// At desktop and 390px, a League Admin:
//   1. Creates a facility Organization owning two Venues (a shared arena and
//      a second, separate rink), entirely through Setup > Records drawers.
//   2. Creates two independent Programs, each operated by its OWN
//      organization (distinct from the facility owner and from each other) —
//      Program A modelling "Twin Rinks' own program", Program B modelling
//      an external, unrelated operator (Illinois High School Hockey) — and
//      re-reads the PERSISTED Venue-owner and Program-operator links (not
//      just the ids submitted on the forms) to confirm all three
//      organizations are actually distinct.
//   3. Grants Program A's Season access to BOTH venues (multi-venue Season).
//   4. Grants Program B's Season access to the SAME shared venue Program A
//      already uses (multi-program Venue) — confirming the shared venue is
//      still offered in Program B's Allow picker despite Program A's grant,
//      then reads EACH Season's own "Allowed venues" subsection (scoped to
//      that Season's own hierarchy node, not a page-wide count) and asserts
//      its exact venue-name set: Season A = {shared, second}, Season B =
//      {shared}, and Program B's spring half-season = {second} — no list
//      leaking another's rows, and each grant provably on the Season meant
//      to hold it.
//   5. Proves the second Venue — which Season B was never granted — is NOT
//      OFFERED anywhere the operator could aim a game at it: no slot card, no
//      venue or rink filter option, nothing in the payload the Calendar
//      renders from, its name nowhere on the page, and no control inside the
//      wizard that could re-aim a game at it. Three controls keep that
//      absence meaningful: the SAME slot IS offered while browsing the Season
//      that does hold the grant; the GRANTED Venue is fully present in the
//      very same render; and the Venue is still offered in Season B's own
//      "Allow a venue" picker, so withholding is escapable by design.
//   6. Successfully creates a Game for each Program on the shared venue, and
//      a further Game for Program A on the second venue, through the
//      Calendar wizard — proving eligibility and isolation both hold for the
//      allowed combinations while the ungranted one stays blocked.
//
// #369 review: BOTH reads this journey drives are now ceilinged on the
// persisted active context, so every step explicitly switches to the context
// it operates in (through the real header context switcher, `#ctx-select`)
// rather than relying on one surface showing everything the League Admin
// administers:
//
//   - Setup > Records and its create drawers read get_setup_overview_v2,
//     which ceilings on the active PROGRAM and collapses `programs` to just
//     that one — so the Season drawer's Program picker (`#f-season-league`)
//     only ever offers the active Program. Program B is therefore made ACTIVE
//     before any of its own records are created, and Program A is made active
//     again before its grants.
//   - The Calendar/wizard read (get_demo_overview) ceilings on the active
//     SEASON: ice slots and venues come only from Venues holding an active
//     SeasonVenueAccess grant to whichever Season is SELECTED (a Program-only
//     context sees none at all), and divisions/registrations/games narrow the
//     same way. Steps 5-6 each select the exact (Program, Season) the step
//     acts for before touching the wizard.
//
// That Season ceiling is exactly the mechanism step 5 now proves: ice is only
// visible where the SELECTED Season holds the grant. Program B's second Season
// (`seasonBAlt`) holds the grant to the second Venue and Season B does not, so
// the two together give step 5 its contrast control — the SAME slot offered
// under one Season and absent under the other, with nothing else changed.
//
// Fails on any browser console/page error.
const { chromium } = require("playwright");
const { spawn } = require("child_process");
const http = require("http");
const path = require("path");
const {
  installContextFixture, selectProgram, selectProgramSeason,
} = require("./context-fixture.js");

const HOST = "127.0.0.1";
const BACKEND_DIR = path.resolve(__dirname, "..", "backend");
const READY_TIMEOUT_MS = 15000;
const VIEWPORTS = [
  { label: "desktop", width: 1440, height: 900, port: 8193 },
  { label: "phone", width: 390, height: 844, port: 8194 },
];

function waitForServer(url, timeoutMs) {
  const deadline = Date.now() + timeoutMs;
  return new Promise((resolve, reject) => {
    const attempt = () => {
      const request = http.get(url, (r) => { r.resume(); resolve(); });
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
  const consoleErrorHandler = (m) => { if (m.type() === "error") errors.push(`[console] ${m.text()}`); };
  page.on("console", consoleErrorHandler);

  const createViaDrawer = async (key, fields, expectedUrl) => {
    await page.click(`.setup-card .sc-new[data-drawer="${key}"]`);
    await page.waitForSelector(".drawer[role=dialog]", { timeout: 10000 });
    for (const [id, value] of Object.entries(fields)) {
      const tag = await page.$eval(`#${id}`, (el) => el.tagName);
      if (tag === "SELECT") await page.selectOption(`#${id}`, value);
      else await page.fill(`#${id}`, value);
    }
    const resp = page.waitForResponse((r) =>
      r.url() === `${base}${expectedUrl}` && r.request().method() === "POST");
    await page.click(`[data-drawer-submit="${key}"]`);
    const settled = await resp;
    // #409 diagnosis fix: ASSERT THE STATUS at this call and print the
    // server's own body. Checking only `body.error` threw the status away, so
    // a refusal returned an id-less object that the next drawer fed into a
    // parent <select> holding no such option -- and the run failed there,
    // far below the create that was actually refused.
    const body = await settled.json().catch(() => null);
    if (settled.status() !== 200 || !body || body.error || !body.id) {
      throw new Error(`[${viewport.label}] ${key} create (POST ${expectedUrl}) `
        + `failed: ${settled.status()} ${JSON.stringify(body)} `
        + `-- request body was ${settled.request().postData()}`);
    }
    await page.waitForFunction(
      () => !document.querySelector(".drawer[role=dialog]"), null, { timeout: 10000 });
    return body;
  };

  // Grants seasonId access to venueId through the real "Allowed venues"
  // control (Setup > Hierarchy > Season participation), never a raw fetch.
  // #367 prerequisite: the Setup Hierarchy tree is scoped to the ACTIVE
  // Program, so a Season's "Allowed venues" control only exists while that
  // Season's Program is active. This journey works both Programs in turn, so
  // the two helpers below move to the owning Program themselves rather than
  // every call site having to remember.
  // #367 prerequisite: creating a Team now requires its League to belong to
  // the caller's ACTIVE Program, so this journey -- which deliberately builds
  // TWO Programs in one session -- must move to each Program before
  // populating it. Without this the second Program's Teams are refused and
  // the failure surfaces much later as missing fixture data.
  const activate = async (programId, seasonId) => {
    const res = await page.evaluate(async (i) => {
      const r = await fetch("/api/context", {
        method: "POST", credentials: "same-origin",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ program_id: i.p, season_id: i.s }),
      });
      return { status: r.status, body: await r.json().catch(() => ({})) };
    }, { p: programId, s: seasonId });
    if (res.status !== 200) {
      throw new Error(`[${viewport.label}] could not activate `
        + `${programId}/${seasonId}: ${res.status} ${JSON.stringify(res.body)}`);
    }
    // Drop the URL's "#ctx=" deep link, which this raw POST just invalidated.
    // app.js keeps that hash in sync only through setActiveContext() (the
    // #ctx-select switcher), so after a raw write it still encodes the
    // PREVIOUS context -- and bootstrap()'s restoreContextDeepLink() treats a
    // hash that disagrees with the persisted selection as an intentional deep
    // link and POSTs it back, which is documented, deliberate behavior that
    // context-switcher.js step (D) asserts. Left in place it silently reverts
    // this switch on the very next reload. With no hash there is no link to
    // adopt, so boot restores the persisted selection -- exactly what we just
    // wrote -- and syncContextHash() re-stamps the URL from it.
    await page.evaluate(() => history.replaceState(
      null, "", location.pathname + location.search));
  };

  const seasonProgram = {};
  const activateForSeason = async (seasonId) => {
    const programId = seasonProgram[seasonId];
    if (!programId) return;
    await activate(programId, seasonId);
    const persisted = await page.evaluate(async () =>
      (await (await fetch("/api/context", { credentials: "same-origin" })).json()));
    if (persisted.program_id !== programId) {
      throw new Error(`[${viewport.label}] activation did not persist: asked `
        + `${programId}/${seasonId}, server reports `
        + `${persisted.program_id}/${persisted.season_id}`);
    }
    // The context was written by a raw fetch, so the client has no idea it
    // changed -- reload and re-enter Setup > Hierarchy so the tree is actually
    // rebuilt for the Program we just moved to.
    await page.reload({ waitUntil: "domcontentloaded" });
    await page.waitForSelector("#content > *", { timeout: 10000 });
    await page.click('.tab[data-tab="setup"]');
    await page.click('[data-setup-view="hierarchy"]');
    // Wait for CONTENT, not mere existence: #season-participation appears as
    // soon as the Setup tree paints at all, so waiting on the container alone
    // proves navigation happened and nothing about WHICH Program's tree is in
    // it. THIS Season's own node is rendered from hv.programs -- the
    // Program-scoped hierarchy -- so it appears only once the tree has been
    // rebuilt for the Program we just moved to, which is the real precondition
    // both callers need. Anchored on the Season's delete button (the same
    // handle allowedVenueNamesFor scopes from) rather than its "Add a venue"
    // select, which disappears once every Venue is already allowed -- Season A
    // ends this journey holding both, so that control is not a signal that
    // survives the states this journey actually reaches.
    await page.waitForFunction((sid) => {
      const panel = document.getElementById("season-participation");
      return !!(panel && panel.querySelector(
        `[data-del="season"][data-del-id="${sid}"]`));
    }, seasonId, { timeout: 10000 });
  };
  const grantViaUi = async (seasonId, venueId) => {
    await activateForSeason(seasonId);
    const vaSelId = `#va-add-${seasonId}`;
    await page.waitForSelector(vaSelId, { timeout: 10000 });
    const options = await page.$$eval(`${vaSelId} option`, (opts) => opts.map((o) => o.value));
    if (!options.includes(venueId)) {
      throw new Error(`[${viewport.label}] venue ${venueId} is not offered in the ` +
        `Allow picker for season ${seasonId}`);
    }
    await page.selectOption(vaSelId, venueId);
    const req = page.waitForResponse((r) =>
      r.url() === `${base}/api/v2/setup/seasons/${seasonId}/venue-access`
      && r.request().method() === "POST");
    await page.click(`[data-va-add="${seasonId}"]`);
    const body = await (await req).json();
    if (body.error) {
      throw new Error(`[${viewport.label}] venue-access grant failed: ${JSON.stringify(body.error)}`);
    }
    await page.waitForSelector(`[data-va-revoke="${body.id}"]`, { timeout: 10000 });
    return body;
  };

  // Switches the signed-in operator's active Program/Season through the real
  // header context switcher (#159/#345) — required throughout post-#369,
  // since BOTH reads this journey drives ceiling on the persisted active
  // context: get_setup_overview_v2 (Setup > Records + every create drawer)
  // on the active Program, get_demo_overview (Dashboard/Calendar/wizard) on
  // the active Program AND Season. Pass an EMPTY `seasonId` for a
  // Program-only selection (`"program_2|"`), which is the only shape
  // available for a Program that has no Season yet.
  //
  // #369 review: this used to "confirm" the switch by polling
  // `#ctx-select.value === value` — but `page.selectOption` sets that value
  // SYNCHRONOUSLY, so the predicate was already true on the first poll and
  // the helper returned before `POST /api/context` had persisted anything.
  // It was a no-op wait, and this round made it the gate in front of
  // creating every one of Program B's records and in front of step 5's
  // negative case. It now awaits the real round trip first: the option must
  // actually exist (it is seeded once per page load — see the reload note
  // below), then the POST must land, and only THEN is the settled value
  // meaningful, because the switcher repaints from the canonical
  // post-switch options and a REJECTED switch snaps back to the previously
  // persisted selection.
  const switchContext = async (programId, seasonId) => {
    const value = `${programId}|${seasonId}`;
    await page.waitForSelector("#ctx-select", { timeout: 10000 });
    await page.waitForFunction(
      (v) => [...document.querySelectorAll("#ctx-select option")]
        .some((o) => o.value === v),
      value, { timeout: 10000 });
    const ctxPost = page.waitForResponse(
      (r) => r.url().endsWith("/api/context") && r.request().method() === "POST",
      { timeout: 10000 });
    await page.selectOption("#ctx-select", value);
    const posted = await ctxPost;
    if (!posted.ok()) {
      throw new Error(`[${viewport.label}] context switch to "${value}" was `
        + `rejected with HTTP ${posted.status()}`);
    }
    await page.waitForFunction(
      (v) => { const s = document.getElementById("ctx-select"); return s && s.value === v; },
      value, { timeout: 10000 });
  };

  // `#ctx-select`'s option list is seeded ONCE per page load/reload
  // (loadContextOptions(), never re-polled by render()), so a Program — or a
  // Season under it — created after that point is simply absent from the
  // switcher until the operator reloads. Every switch to a just-created
  // context is therefore preceded by this. A reload can land on the
  // onboarding shell (progress < all stages) rather than the normal tabbed
  // interface, and that shell never runs the ordinary
  // render()/renderContextSwitcher() pipeline — so navigate to a real tab,
  // exactly as an operator would, and wait for the switcher to be on screen.
  const reloadForContextOptions = async () => {
    await page.reload({ waitUntil: "domcontentloaded" });
    await page.waitForSelector("#content > *", { timeout: 10000 });
    await page.click('.tab[data-tab="calendar"]');
    await page.waitForFunction(() => {
      const wrap = document.getElementById("context-switcher");
      return wrap && !wrap.hidden;
    }, null, { timeout: 15000 });
  };

  const openSetupRecords = async () => {
    await page.click('.tab[data-tab="setup"]');
    await page.click('[data-setup-view="records"]');
    await page.waitForSelector(".setup-card", { timeout: 10000 });
  };

  // Creates a Game via the Calendar wizard on the given ice slot, for the
  // given league/division/home/away combination.
  const createGameViaWizard = async (slotId, leagueId, divisionId, homeId, awayId) => {
    await page.click('.tab[data-tab="calendar"]');
    await page.waitForSelector(`[data-slot="${slotId}"]`, { timeout: 15000 });
    await page.click(`[data-slot="${slotId}"]`);
    await page.waitForSelector("#w-league", { timeout: 10000 });
    await page.selectOption("#w-league", leagueId);
    if (divisionId) {
      await page.waitForFunction(
        (d) => !!Array.from(document.querySelectorAll("#w-div option")).find((o) => o.value === d),
        divisionId, { timeout: 10000 });
      await page.selectOption("#w-div", divisionId);
    }
    await page.waitForFunction(
      (t) => !!Array.from(document.querySelectorAll("#w-home option")).find((o) => o.value === t),
      homeId, { timeout: 10000 });
    await page.selectOption("#w-home", homeId);
    await page.selectOption("#w-away", awayId);
    const createReq = page.waitForResponse((r) =>
      r.url() === `${base}/api/v2/setup/game` && r.request().method() === "POST");
    await page.click("[data-wizcreate]");
    const body = await (await createReq).json();
    if (body.error) {
      throw new Error(`[${viewport.label}] wizard game create failed: ${JSON.stringify(body.error)}`);
    }
    await page.waitForFunction(() => !document.querySelector(".wizard"), null, { timeout: 10000 });
    return body;
  };

  // ---- step 5's replacement: THE AFFORDANCE IS ABSENT ---------------------
  //
  // #409, DEFECT 2, OWNER RULING. This step used to drive the Calendar wizard
  // at a Venue the game's Season was never granted and read the
  // `venue_access_missing` refusal back off the wire. #409 closed that route
  // one rule EARLIER — a `game` is a SEASON-OWNED create, refused unless the
  // SELECTED Season IS the Season being written into — and #369's Season
  // ceiling already hides the ice of every Venue the SELECTED Season lacks a
  // grant to. Standing in the game's own Season, the ungranted Venue's ice is
  // not on the Calendar to click. Rather than manufacture an error the shipped
  // UI can no longer raise, this step proves the STRONGER property that
  // replaced it: THE OPERATOR IS NEVER OFFERED THE INACCESSIBLE VENUE, so the
  // error cannot arise. The domain rule itself is preserved, tri-store, with
  // zero-write/zero-audit and positive controls, at the HTTP and service
  // boundaries in backend/tests/test_venue_access_boundary.py.
  //
  // "Not offered" is asserted as the ABSENCE OF SPECIFIC AFFORDANCES, never as
  // "no error appeared" — which would pass for a page that failed to render, a
  // fixture that never created the slot, a Calendar showing nothing at all, or
  // a signed-out session. Every one of those is excluded below by controls, in
  // the SAME rendered view:
  //
  //   CONTRAST CONTROL (5a) — the SAME slot, SAME operator, SAME day IS
  //     offered while browsing the Season that DOES hold the grant. So the
  //     slot exists, the Calendar can render it, and the only thing that
  //     changes in 5b is the SELECTED Season's access.
  //   SAME-RENDER POSITIVE CONTROL (5b) — in the very render where the
  //     ungranted Venue is absent, the GRANTED Venue's slot, venue filter
  //     option and rink filter option are all present and clickable. The
  //     Calendar is alive and populated; the absence is specific.
  //   REMEDIATION CONTROL (5c) — the ungranted Venue is still offered in that
  //     Season's own "Allow a venue" picker. Withholding is a designed state
  //     with a way out (the same `remediation_route` the boundary error
  //     names), not a dead end or a missing record.
  //
  // Every Calendar affordance that could carry an operator to ice is checked,
  // by ITS OWN selector: the click/keyboard-activatable slot cards
  // (`[data-slot]` / `[data-drop]`), the venue filter (`[data-filter=venueId]`),
  // the rink filter (`[data-filter=rinkId]`), and the payload behind them
  // (`/api/demo/overview`'s `venues`/`rinks`/`ice_slots`). Finally the wizard
  // itself is opened on the one slot that IS offered and shown to expose NO
  // control that could re-aim it at another Venue — its Ice step is static
  // text, so even from inside the wizard the ungranted Venue is unreachable.

  // Everything the Calendar offers as a route to ice, in the CURRENT context.
  const readIceAffordances = async () => {
    await page.click('.tab[data-tab="calendar"]');
    await page.waitForSelector(".cal-toolbar", { timeout: 15000 });
    // The venue filter is rendered by the same pass as the slot cards, so its
    // presence means the Calendar really painted rather than being mid-fetch.
    return page.evaluate(async () => {
      const vals = (sel) => Array.from(document.querySelectorAll(sel));
      const optionsOf = (name) => {
        const s = document.querySelector(`select[data-filter="${name}"]`);
        if (!s) return null;                       // distinguishes "no control"
        return Array.from(s.options).map((o) => ({
          value: o.value, label: o.textContent.trim() }));
      };
      const ov = await (await fetch("/api/demo/overview",
        { credentials: "same-origin" })).json();
      return {
        slotIds: vals("[data-slot]").map((el) => el.getAttribute("data-slot")),
        dropIds: vals("[data-drop]").map((el) => el.getAttribute("data-drop")),
        venueOptions: optionsOf("venueId"),
        rinkOptions: optionsOf("rinkId"),
        calendarText: (document.getElementById("content") || document.body).innerText,
        ovVenueIds: (ov.venues || []).map((v) => v.id),
        ovVenueNames: (ov.venues || []).map((v) => v.name),
        ovRinkIds: (ov.rinks || []).map((r) => r.id),
        ovSlotIds: (ov.ice_slots || []).map((s) => s.id),
        ovSlotRinkIds: (ov.ice_slots || []).map((s) => s.rink_id),
      };
    });
  };

  const fail = (msg) => { throw new Error(`[${viewport.label}] ${msg}`); };
  const optionValues = (opts) => (opts || []).map((o) => o.value);
  const optionLabels = (opts) => (opts || []).map((o) => o.label);

  // (5a) CONTRAST CONTROL. Browsing the Season that HOLDS the grant, the
  // second Venue's slot IS an offered, activatable target and the Venue and
  // Rink ARE offered as filters. Without this the absence proved in 5b could
  // be satisfied by a slot that was never created or a Calendar that never
  // paints ice at all.
  const assertIceIsOffered = async (slotId, venueId, venueName, rinkId, why) => {
    const a = await readIceAffordances();
    if (!a.slotIds.includes(slotId)) {
      fail(`${why}: the granted Season's Calendar does not offer slot ${slotId} ` +
        `as a schedulable target — offered: ${JSON.stringify(a.slotIds)}`);
    }
    if (!a.dropIds.includes(slotId)) {
      fail(`${why}: slot ${slotId} is rendered but is not an activatable target ` +
        `(no [data-drop]), so 5b's absence would prove nothing about reachability`);
    }
    // Activatable by KEYBOARD as well as pointer — the affordance whose
    // absence 5b asserts must be a real one for every input modality.
    const reachable = await page.$eval(`[data-slot="${slotId}"]`, (el) => ({
      role: el.getAttribute("role"), tabindex: el.getAttribute("tabindex"),
      label: el.getAttribute("aria-label") || "",
    }));
    if (reachable.role !== "button" || reachable.tabindex !== "0" || !reachable.label) {
      fail(`${why}: slot ${slotId} is not a keyboard-reachable, accessibly-named ` +
        `control (${JSON.stringify(reachable)})`);
    }
    if (!optionValues(a.venueOptions).includes(venueId)) {
      fail(`${why}: the granted Season's venue filter does not offer venue ${venueId} ` +
        `— offered: ${JSON.stringify(a.venueOptions)}`);
    }
    if (!optionValues(a.rinkOptions).includes(rinkId)) {
      fail(`${why}: the granted Season's rink filter does not offer rink ${rinkId} ` +
        `— offered: ${JSON.stringify(a.rinkOptions)}`);
    }
    if (!a.ovSlotIds.includes(slotId) || !a.ovVenueIds.includes(venueId)) {
      fail(`${why}: the granted Season's own overview payload omits the slot or venue ` +
        `it renders — slots ${JSON.stringify(a.ovSlotIds)}, venues ${JSON.stringify(a.ovVenueIds)}`);
    }
    if (!a.calendarText.includes(venueName)) {
      fail(`${why}: "${venueName}" is not shown on the granted Season's Calendar`);
    }
    return a;
  };

  // (5b) THE ABSENCE ITSELF, with its same-render positive control. `denied*`
  // is the Venue this Season was never granted; `offered*` is the Venue it
  // WAS granted, and must be fully present in the same paint.
  const assertVenueNotOffered = async (denied, offered, why) => {
    const a = await readIceAffordances();

    // -- SAME-RENDER POSITIVE CONTROL: the Calendar is alive and populated.
    if (a.venueOptions === null || a.rinkOptions === null) {
      fail(`${why}: the Calendar's venue/rink filters are missing entirely, so ` +
        `nothing below would distinguish "withheld" from "did not render"`);
    }
    if (!a.slotIds.includes(offered.slotId) || !a.dropIds.includes(offered.slotId)) {
      fail(`${why}: the GRANTED venue's slot ${offered.slotId} is not offered either — ` +
        `this render shows no ice at all, so the denied venue's absence is not ` +
        `evidence of withholding. Offered: ${JSON.stringify(a.slotIds)}`);
    }
    if (!optionValues(a.venueOptions).includes(offered.venueId)
      || !optionValues(a.rinkOptions).includes(offered.rinkId)) {
      fail(`${why}: the GRANTED venue/rink is missing from the filters, so their ` +
        `contents are not a meaningful place to check for the denied one — ` +
        `venues ${JSON.stringify(a.venueOptions)}, rinks ${JSON.stringify(a.rinkOptions)}`);
    }
    if (!a.calendarText.includes(offered.venueName)) {
      fail(`${why}: the GRANTED venue "${offered.venueName}" is not even named on this ` +
        `Calendar — the render is not showing venue identity at all`);
    }

    // -- THE ABSENCE, affordance by affordance.
    if (a.slotIds.includes(denied.slotId) || a.dropIds.includes(denied.slotId)) {
      fail(`${why}: the Calendar OFFERS slot ${denied.slotId} on venue ` +
        `"${denied.venueName}", which this Season holds no access to — the ` +
        `operator can still aim a game at ice they may not use`);
    }
    // Not just that one slot: no slot of the denied venue's rink is offered.
    const deniedRinkSlots = a.ovSlotIds.filter(
      (_, i) => a.ovSlotRinkIds[i] === denied.rinkId);
    if (deniedRinkSlots.length) {
      fail(`${why}: the Season's ice inventory still contains slots on the ungranted ` +
        `venue's rink: ${JSON.stringify(deniedRinkSlots)}`);
    }
    if (optionValues(a.venueOptions).includes(denied.venueId)
      || optionLabels(a.venueOptions).includes(denied.venueName)) {
      fail(`${why}: the Calendar's venue filter still offers "${denied.venueName}" — ` +
        `${JSON.stringify(a.venueOptions)}`);
    }
    if (optionValues(a.rinkOptions).includes(denied.rinkId)
      || optionLabels(a.rinkOptions).includes(denied.rinkName)) {
      fail(`${why}: the Calendar's rink filter still offers "${denied.rinkName}" — ` +
        `${JSON.stringify(a.rinkOptions)}`);
    }
    if (a.ovVenueIds.includes(denied.venueId) || a.ovRinkIds.includes(denied.rinkId)
      || a.ovSlotIds.includes(denied.slotId)) {
      fail(`${why}: the payload the Calendar renders from still carries the ungranted ` +
        `venue/rink/slot — venues ${JSON.stringify(a.ovVenueIds)}, ` +
        `rinks ${JSON.stringify(a.ovRinkIds)}, slots ${JSON.stringify(a.ovSlotIds)}`);
    }
    if (a.calendarText.includes(denied.venueName)) {
      fail(`${why}: "${denied.venueName}" is still named somewhere on this Season's ` +
        `Calendar, so the operator is still being shown ice they cannot book`);
    }
  };

  // (5c) FROM INSIDE THE WIZARD. Opened on the one slot that IS offered, the
  // wizard exposes no control that could re-aim the game at another Venue,
  // Rink or slot — step 3 ("Ice") is static text bound to the slot the
  // operator clicked. Asserted twice: no interactive control NAMES ice, and
  // the wizard's control set is exactly the known competition/teams set. The
  // allow-list is deliberately strict: a new control here must be looked at by
  // a human and shown not to re-open this route.
  const WIZARD_CONTROLS = ["w-exhibition", "w-league", "w-div", "w-home", "w-away",
    "data-wizcancel", "data-wizcreate"];
  const assertWizardCannotRetargetVenue = async (slotId) => {
    await page.click('.tab[data-tab="calendar"]');
    await page.waitForSelector(`[data-slot="${slotId}"]`, { timeout: 15000 });
    await page.click(`[data-slot="${slotId}"]`);
    await page.waitForSelector(".wizard", { timeout: 10000 });
    const controls = await page.$$eval(".wizard select, .wizard input, .wizard button",
      (els) => els.map((el) => ({
        key: el.id || (el.hasAttribute("data-wizcancel") ? "data-wizcancel"
          : el.hasAttribute("data-wizcreate") ? "data-wizcreate" : ""),
        name: [el.id, el.getAttribute("aria-label"), el.getAttribute("name"),
          el.textContent].filter(Boolean).join(" "),
      })));
    const iceish = controls.filter((c) => /venue|rink|arena|ice|slot/i.test(c.name));
    if (iceish.length) {
      fail(`the wizard exposes control(s) that name ice — ${JSON.stringify(iceish)} — ` +
        `so the game may be re-aimed at a Venue the Season cannot use`);
    }
    const keys = controls.map((c) => c.key);
    const unknown = keys.filter((k) => !WIZARD_CONTROLS.includes(k));
    if (unknown.length || keys.some((k) => !k)) {
      fail(`the wizard has control(s) outside the known venue-free set ` +
        `${JSON.stringify(WIZARD_CONTROLS)}: ${JSON.stringify(controls)}. If one of ` +
        `these can change the game's Venue, this journey's absence proof is void`);
    }
    await page.click("[data-wizcancel]");
    await page.waitForFunction(() => !document.querySelector(".wizard"), null,
      { timeout: 10000 });
  };

  // (5d) REMEDIATION CONTROL. The ungranted Venue IS still offered in this
  // Season's own "Allow a venue" picker — the route the boundary error's
  // `remediation_route` names. Withholding ice is a designed, escapable state,
  // not a missing record. Nothing is granted here: the picker is read only.
  const assertRemediationOffered = async (seasonId, venueId, venueName) => {
    await activateForSeason(seasonId);
    const selId = `#va-add-${seasonId}`;
    await page.waitForSelector(selId, { timeout: 10000 });
    const offered = await page.$$eval(`${selId} option`,
      (opts) => opts.map((o) => o.value));
    if (!offered.includes(venueId)) {
      fail(`the Season cannot be granted "${venueName}" either — its Allow picker ` +
        `offers ${JSON.stringify(offered)}, so the withheld ice has no way back`);
    }
  };

  try {
    await waitForServer(`${base}/api/health`, READY_TIMEOUT_MS);
    await page.goto(base, { waitUntil: "domcontentloaded" });
    await page.waitForSelector("#content > *", { timeout: 10000 });
    // This journey reads its ice back off the Arena Calendar's DEFAULT day
    // without navigating, so the day it books on must be the day the calendar
    // opens on. That used to be the literal "2026-09-05", which worked only
    // because app.js opened on the same literal -- two constants agreeing with
    // each other about a date real time would pass (#387/#389). Read the app's
    // own `calendarDate` global instead, so the booked day and the rendered
    // day cannot drift apart.
    const CAL_DAY = await page.evaluate(() => calendarDate);
    await installContextFixture(page);
    await openSetupRecords();

    // (1) One facility Organization owns two Venues.
    const orgFacility = await createViaDrawer("organization",
      { "f-org": "Twin Rinks Facility" }, "/api/v2/setup/organization");
    const venueShared = await createViaDrawer("venue",
      { "f-venue": "Twin Rinks Arena", "f-venue-org": orgFacility.id }, "/api/v2/setup/venue");
    const rinkShared = await createViaDrawer("rink",
      { "f-rink-venue": venueShared.id, "f-rink": "Twin Rinks Main Sheet" }, "/api/v2/setup/rink");
    const slotSharedA = await createViaDrawer("ice-slot", {
      "f-slot-rink": rinkShared.id, "f-slot-date": CAL_DAY,
      "f-slot-start": "18:00", "f-slot-end": "19:00",
    }, "/api/v2/setup/ice-slot");
    const slotSharedB = await createViaDrawer("ice-slot", {
      "f-slot-rink": rinkShared.id, "f-slot-date": CAL_DAY,
      "f-slot-start": "20:00", "f-slot-end": "21:00",
    }, "/api/v2/setup/ice-slot");
    const venueSecond = await createViaDrawer("venue",
      { "f-venue": "North Annex Rink", "f-venue-org": orgFacility.id }, "/api/v2/setup/venue");
    const rinkSecond = await createViaDrawer("rink",
      { "f-rink-venue": venueSecond.id, "f-rink": "North Annex Sheet 1" }, "/api/v2/setup/rink");
    // Staggered so it doesn't overlap slotSharedA — Program A's two teams
    // play both of Program A's games, and a team can't be in two games at once.
    const slotSecond = await createViaDrawer("ice-slot", {
      "f-slot-rink": rinkSecond.id, "f-slot-date": CAL_DAY,
      "f-slot-start": "21:00", "f-slot-end": "22:00",
    }, "/api/v2/setup/ice-slot");

    // (2) Two independent Programs, each operated by its OWN organization —
    // distinct from the facility owner and from each other.
    const orgProgramA = await createViaDrawer("organization",
      { "f-org": "Twin Rinks Adult Hockey" }, "/api/v2/setup/organization");
    const programA = await createViaDrawer("league",
      { "f-league": "Adult Men", "f-league-org": orgProgramA.id }, "/api/v2/setup/program");
    // #409 EXPLICIT SELECTION. `season` is PROGRAM-AXIS
    // (ApiService._CREATE_CONSUMED_AXES), so it needs a PERSISTED Program --
    // the Venues/Rinks/ice slots above needed none because a Venue made under
    // an Organization inherits no axis at all (_CREATE_PARENT_NO_INHERIT).
    //
    // RECORDED TRADE (./context-fixture.js header): at this exact point the
    // install holds ONE Program and ZERO Seasons, the one state in which the
    // shipped switcher collapses to a non-interactive chip and no control can
    // persist a selection. That first-run dead end is a PRODUCT defect,
    // witnessed deliberately (and left failing) by ./season-dates.js. This
    // journey's subject is cross-Program venue sharing, so it selects
    // explicitly -- asserted by the fixture's write echo AND read-back -- and
    // goes on to drive the REAL #ctx-select for every later switch, once a
    // second entry exists to make it interactive.
    await selectProgram(page, "Program-axis Season create under Adult Men",
      programA.id);
    await openSetupRecords();
    const seasonA = await createViaDrawer("season",
      { "f-season-league": programA.id, "f-season": "2026-27 Adult" }, "/api/v2/setup/season");
    // `league` and `division` are SEASON-OWNED: both axes, and the Season
    // selected must be the one they are written into.
    await selectProgramSeason(page,
      "Season-owned League/Division creates into 2026-27 Adult",
      programA.id, seasonA.id);
    await openSetupRecords();
    const leagueA = await createViaDrawer("level",
      { "f-level-season": seasonA.id, "f-level": "Adult League" }, "/api/v2/setup/league");
    const divisionA = await createViaDrawer("division",
      { "f-div-league": leagueA.id, "f-div": "Gold" }, "/api/v2/setup/division");
    const clubA = await createViaDrawer("club",
      { "f-club": "Adult Club" }, "/api/v2/setup/club");

    await activate(programA.id, seasonA.id);
    const teamA1 = await createViaDrawer("team",
      { "f-team-club": clubA.id, "f-team-perm-league": leagueA.id, "f-team": "Adult D1" },
      "/api/v2/setup/team");
    const teamA2 = await createViaDrawer("team",
      { "f-team-club": clubA.id, "f-team-perm-league": leagueA.id, "f-team": "Adult D2" },
      "/api/v2/setup/team");

    const orgProgramB = await createViaDrawer("organization",
      { "f-org": "Illinois High School Hockey" }, "/api/v2/setup/organization");
    const programB = await createViaDrawer("league",
      { "f-league": "High School", "f-league-org": orgProgramB.id }, "/api/v2/setup/program");
    // #369: get_setup_overview_v2 collapses `programs` to the ACTIVE Program
    // alone, and narrows Seasons/Leagues/Teams to it too — so every record
    // below has to be created with Program B actually active, or its parent
    // simply isn't in the drawer's picker. Program B has no Season yet, so
    // this is a Program-only selection (empty Season part).
    await reloadForContextOptions();
    await switchContext(programB.id, "");
    await openSetupRecords();
    const seasonB = await createViaDrawer("season",
      { "f-season-league": programB.id, "f-season": "2026-27 Varsity" }, "/api/v2/setup/season");
    // #409: `league` is SEASON-OWNED (ApiService._CREATE_CONSUMED_AXES), so
    // it needs the saved Program AND the Season it is written into -- the
    // Program-only selection that was enough for the Season create above is
    // refused here. Season B did not exist when the switcher's options were
    // last seeded, so reload first and then make the two-axis selection
    // through the REAL #ctx-select, which is genuinely interactive by now
    // (two Programs, so more than one context entry).
    await reloadForContextOptions();
    await switchContext(programB.id, seasonB.id);
    await openSetupRecords();
    const leagueB = await createViaDrawer("level",
      { "f-level-season": seasonB.id, "f-level": "Varsity League" }, "/api/v2/setup/league");
    await activate(programB.id, seasonB.id);
    seasonProgram[seasonA.id] = programA.id;
    seasonProgram[seasonB.id] = programB.id;
    const teamB1 = await createViaDrawer("team",
      { "f-team-club": "", "f-team-perm-league": leagueB.id, "f-team": "Varsity Home" },
      "/api/v2/setup/team");
    const teamB2 = await createViaDrawer("team",
      { "f-team-club": "", "f-team-perm-league": leagueB.id, "f-team": "Varsity Away" },
      "/api/v2/setup/team");
    // A SECOND Season under Program B — League B's spring half-season, so the
    // same League and the same two teams take part in it (registered below).
    // It supplies step 5's CONTRAST CONTROL under #369's Season ceiling: the
    // Calendar shows ice ONLY from Venues the currently-SELECTED Season holds
    // an active grant to, and this is the one Season granted the second Venue.
    // So the very same slot is OFFERED while browsing this Season and ABSENT
    // while browsing Season B, which League B's games actually belong to and
    // which was never granted that Venue. Same operator, same day, same slot —
    // only the selected Season's access differs, which is what makes step 5's
    // absence a proof about access rather than about an empty or broken page.
    const seasonBAlt = await createViaDrawer("season",
      { "f-season-league": programB.id, "f-season": "2027 Varsity Spring" },
      "/api/v2/setup/season");
    // #369 OWNER RULING: register this Season's Program too. `activateForSeason`
    // is a no-op for a Season missing from this map, and the spring half-season
    // was missing -- which used to be survivable only because BOTH venue reads
    // ceilinged on the PROGRAM, so leaving Season B selected still served Season
    // BAlt's grants and picker. The ruling made the SELECTED Season the exact
    // ceiling, so the grant and the Allowed-venues read below must genuinely
    // switch to this Season, not merely to its Program.
    seasonProgram[seasonBAlt.id] = programB.id;
    // A THIRD ice slot on the second Venue — the piece of ice step 5 proves is
    // never OFFERED to Season B, which holds no grant to that Venue (#258
    // review; re-purposed by #409's ruling from "the create is refused" to
    // "the affordance is absent").
    const slotSecondDenied = await createViaDrawer("ice-slot", {
      "f-slot-rink": rinkSecond.id, "f-slot-date": CAL_DAY,
      "f-slot-start": "10:00", "f-slot-end": "11:00",
    }, "/api/v2/setup/ice-slot");

    // (#258 review) Confirm the persisted owner/operator links, not just the
    // ids submitted on the drawer forms: the Venue owner and both Program
    // operators must be the three DISTINCT organizations just created.
    if (venueShared.organization_id !== orgFacility.id
      || venueSecond.organization_id !== orgFacility.id) {
      throw new Error(`[${viewport.label}] a Venue's persisted organization_id does not match ` +
        `the facility owner it was created under`);
    }
    if (programA.operator_organization_id !== orgProgramA.id
      || programB.operator_organization_id !== orgProgramB.id) {
      throw new Error(`[${viewport.label}] a Program's persisted operator_organization_id does ` +
        `not match the organization it was created under`);
    }
    const distinctOrgIds = new Set([orgFacility.id, orgProgramA.id, orgProgramB.id]);
    if (distinctOrgIds.size !== 3) {
      throw new Error(`[${viewport.label}] the facility owner and the two Program operators are ` +
        `not three distinct organizations`);
    }

    // Team-registration creation is Season participation's own already-proven
    // UI (Slice B2b) — out of THIS regression's scope, so built over the wire
    // like every other journey's non-target prerequisites.
    //
    // #409: a `registration` is SEASON-OWNED, so each one needs the saved
    // Program AND the Season it is written into — and these six span THREE
    // Seasons across two Programs, so one selection cannot serve them all.
    // They also used to be posted by a `post()` helper that decoded the body
    // and DISCARDED the status: five of the six were refused 409 in silence,
    // and the only symptom was step 5's wizard offering no teams ~150 lines
    // later, a `waitForFunction` timeout that named neither the refusal nor
    // the Season it belonged to. Each POST is asserted at its own line now.
    const registerTeams = async (what, programId, seasonId, rows) => {
      await selectProgramSeason(page, what, programId, seasonId);
      await page.evaluate(async (i) => {
        for (const row of i.rows) {
          await window.hsFixture.call(
            `team registration ${row.team_id} into ${i.seasonId}`,
            `/api/v2/setup/seasons/${i.seasonId}/team-registrations`, row);
        }
      }, { seasonId, rows });
    };
    await registerTeams("Season-owned registrations into Season A",
      programA.id, seasonA.id, [
        { team_id: teamA1.id, league_id: leagueA.id, division_id: divisionA.id },
        { team_id: teamA2.id, league_id: leagueA.id, division_id: divisionA.id },
      ]);
    await registerTeams("Season-owned registrations into Season B",
      programB.id, seasonB.id, [
        { team_id: teamB1.id, league_id: leagueB.id },
        { team_id: teamB2.id, league_id: leagueB.id },
      ]);
    // The same two teams also take part in League B's spring half-season
    // (seasonBAlt) — which binds League B to that Season too, so the wizard
    // has teams to offer while it is the browsing context in step 5.
    // Registrations are ceilinged to the ACTIVE Season by get_demo_overview,
    // so without these the wizard's team picker would be empty there and the
    // negative case unreachable.
    await registerTeams("Season-owned registrations into the spring half-season",
      programB.id, seasonBAlt.id, [
        { team_id: teamB1.id, league_id: leagueB.id },
        { team_id: teamB2.id, league_id: leagueB.id },
      ]);

    // (3) Season A uses BOTH venues — one Season, multiple Venues. The
    // "Allow a venue" picker is fed by get_setup_overview_v2, whose Venue
    // list is the ACTIVE Program's granted Venues plus the not-yet-granted
    // (`unassigned_venues`) ones — so the grants are made from the Program
    // that owns the schedule they unblock, Program A, which is also the
    // Program whose Calendar step 6 exercises.
    await switchContext(programA.id, seasonA.id);
    await page.click('[data-setup-view="hierarchy"]');
    await grantViaUi(seasonA.id, venueShared.id);
    await grantViaUi(seasonA.id, venueSecond.id);

    // (4) Season B is granted the SAME shared venue Season A already uses —
    // one Venue, multiple independent Programs/Seasons — and the picker
    // still offers it despite Program A's grant.
    await grantViaUi(seasonB.id, venueShared.id);
    // The second Venue goes to League B's spring half-season ONLY (see
    // seasonBAlt's own comment above): that is what puts the Venue's ice on
    // Program B's Calendar at all under #369's Season ceiling, while Season B
    // — the Season League B's games actually belong to — still has no access
    // to it, which is exactly what step 5 proves is enforced.
    await grantViaUi(seasonBAlt.id, venueSecond.id);

    // Neither Season's Allowed-venues list leaks the other's rows: read each
    // Season's OWN "Allowed venues" subsection (scoped from its delete button
    // in the Season's own <details> node, not a page-wide count) and check
    // the exact venue-name set it lists (#258 review).
    const allowedVenueNamesFor = async (seasonId) => {
      await activateForSeason(seasonId);
      return page.evaluate((sid) => {
      // Season nodes are duplicated across trees (Competition structure AND
      // Season participation); only the latter — scoped by its own
      // #season-participation container — carries the venueAccessSection
      // this journey needs (#258 review fix: an unscoped querySelector can
      // silently grab the wrong copy).
      const panel = document.getElementById("season-participation");
      const del = panel && panel.querySelector(`[data-del="season"][data-del-id="${sid}"]`);
      if (!del) return null;
      const seasonDetails = del.closest("details.tn");
      const child = Array.from(seasonDetails.querySelectorAll(
        ":scope > div.tn-children > details.tn"))
        .find((d) => (d.querySelector("summary")?.textContent || "").includes("Allowed venues"));
      if (!child) return null;
      return Array.from(child.querySelectorAll("[data-va-revoke]")).map((btn) =>
        btn.closest(".tn-leaf").querySelector(".tn-label").textContent.trim()
          .replace(/^\S+\s*/, ""));  // drop the leading emoji glyph
    }, seasonId);
    };

    const namesA = await allowedVenueNamesFor(seasonA.id);
    const namesB = await allowedVenueNamesFor(seasonB.id);
    const namesBAlt = await allowedVenueNamesFor(seasonBAlt.id);
    const sameSet = (actual, expected) =>
      actual && actual.length === expected.length
      && expected.every((n) => actual.includes(n));
    if (!sameSet(namesA, [venueShared.name, venueSecond.name])) {
      throw new Error(`[${viewport.label}] Season A's Allowed-venues list should be exactly ` +
        `{${venueShared.name}, ${venueSecond.name}}, found ${JSON.stringify(namesA)}`);
    }
    if (!sameSet(namesB, [venueShared.name])) {
      throw new Error(`[${viewport.label}] Season B's Allowed-venues list should be exactly ` +
        `{${venueShared.name}}, found ${JSON.stringify(namesB)}`);
    }
    // The spring half-season holds the second Venue and NOT the shared one:
    // the grant that makes step 5's ice visible landed on the Season that is
    // meant to have it, so the rejection there is a real venue-access denial
    // and not an accident of a mis-targeted fixture.
    if (!sameSet(namesBAlt, [venueSecond.name])) {
      throw new Error(`[${viewport.label}] the spring half-season's Allowed-venues list should ` +
        `be exactly {${venueSecond.name}}, found ${JSON.stringify(namesBAlt)}`);
    }

    // Program B's Seasons were created after the last reload, so `#ctx-select`
    // cannot offer them until the option list is seeded again.
    await reloadForContextOptions();

    // (5) Season B was never granted the second Venue — so the real UI never
    // OFFERS it, and the operator cannot aim a game at ice their Season may
    // not use. Per #409's owner ruling this replaces the old "drive the UI
    // into venue_access_missing" step, which the SEASON-OWNED create rule plus
    // #369's Season ceiling together made unreachable; the domain rule lives
    // on at the HTTP/service boundary in
    // backend/tests/test_venue_access_boundary.py (tri-store, zero-write,
    // zero-audit, with positive controls).
    const deniedVenue = {
      slotId: slotSecondDenied.id, venueId: venueSecond.id,
      venueName: venueSecond.name, rinkId: rinkSecond.id, rinkName: rinkSecond.name,
    };
    const offeredVenue = {
      slotId: slotSharedB.id, venueId: venueShared.id,
      venueName: venueShared.name, rinkId: rinkShared.id,
    };

    // (5a) CONTRAST CONTROL — browsing Program B's spring half-season, the ONE
    // Season holding a grant to the second Venue, that Venue's ice IS offered
    // as a keyboard-reachable, accessibly-named target. The slot exists and the
    // Calendar can show it, so 5b's absence is about access and nothing else.
    await switchContext(programB.id, seasonBAlt.id);
    await assertIceIsOffered(slotSecondDenied.id, venueSecond.id, venueSecond.name,
      rinkSecond.id, "5a contrast control (the Season that HOLDS the grant)");

    // (5b) THE ABSENCE — switch to Season B, the Season League B's games are
    // actually written into and the one that was never granted the second
    // Venue. Not one Calendar affordance offers it, while the shared Venue it
    // IS granted stays fully present in the same render.
    await switchContext(programB.id, seasonB.id);
    await assertVenueNotOffered(deniedVenue, offeredVenue,
      "5b (Season B, which holds no grant to the second Venue)");

    // (5c) …and not from inside the wizard either, opened on the one slot
    // Season B IS offered.
    await assertWizardCannotRetargetVenue(slotSharedB.id);

    // (5d) …while the grant that would unblock it is still one click away.
    // `activateForSeason` reloads into Setup, so restore the Calendar context
    // the following steps expect.
    await assertRemediationOffered(seasonB.id, venueSecond.id, venueSecond.name);
    await reloadForContextOptions();

    // (6) Program A schedules a Game on the shared venue AND on its second
    // venue; Program B independently schedules a Game on the same shared
    // venue — proving eligibility and isolation both hold end to end. Each
    // creation selects ITS OWN (Program, Season): #369 scopes the wizard's
    // League choices to the active Program and its ice to the active Season.
    await switchContext(programA.id, seasonA.id);
    const gameA1 = await createGameViaWizard(
      slotSharedA.id, leagueA.id, divisionA.id, teamA1.id, teamA2.id);
    const gameA2 = await createGameViaWizard(
      slotSecond.id, leagueA.id, divisionA.id, teamA1.id, teamA2.id);
    await switchContext(programB.id, seasonB.id);
    const gameB1 = await createGameViaWizard(
      slotSharedB.id, leagueB.id, null, teamB1.id, teamB2.id);
    if (!gameA1.id || !gameA2.id || !gameB1.id) {
      throw new Error(`[${viewport.label}] one or more wizard game creates did not return an id`);
    }

    if (errors.length) {
      throw new Error(`[${viewport.label}] console/page errors:\n${errors.join("\n")}`);
    }
    console.log(`[${viewport.label}] OK — multi-venue Season, multi-program Venue, and ` +
      `facility-owner/operator independence all confirmed through the real UI.`);
  } catch (error) {
    // Keep the ORIGINAL stack. Re-wrapping in a bare Error lost it, so a
    // failure inside a shared helper (several `waitForFunction`s here carry
    // no message of their own) reported only "Timeout 10000ms exceeded" with
    // nothing to say WHICH wait timed out.
    const wrapped = new Error(
      `${error.message}\n--- demo server output ---\n${serverOutput}`);
    wrapped.stack = `${wrapped.message}\n--- original stack ---\n`
      + `${(error && error.stack) || "(none)"}`;
    throw wrapped;
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
    console.log("Venue-sharing browser journey passed.");
  } catch (error) {
    console.error("Venue-sharing browser journey FAILED.");
    console.error(error && error.message ? error.message : error);
    if (error && error.stack) console.error(error.stack);
    process.exitCode = 1;
  } finally {
    if (browser) await browser.close();
  }
}

main();
