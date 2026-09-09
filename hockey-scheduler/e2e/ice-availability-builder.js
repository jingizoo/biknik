// Ice Availability Builder + Arena Calendar month view (#158).
//
// At desktop and 390px, an arena operator builds a draft ice inventory from a
// recurring weekly template, previews it, and idempotently commits it. The
// journey verifies, on the real UI:
//   * the Arena Calendar has a Month view that renders a day grid;
//   * every Month day and Ice Builder's Back action preserve keyboard focus
//     on the selected Calendar destination after replacing their source node,
//     without stealing focus for a programmatic, unfocused activation;
//   * opening Builder and successfully creating ice keep keyboard focus on an
//     exact semantic target through each held loading paint and settlement,
//     while programmatic activation preserves the context selector;
//   * a same-tuple full render carries that exact loading target through an
//     overlapping render, a request that settles during the render, and the
//     queued card successor without overriding a newer external focus choice;
//   * the builder previews the correct slot count for a Tue/Thu block in the
//     selected date range, and reports rinks whose Venue lacks SeasonVenueAccess
//     for the previewed Season (never generating ice for them) — re-checking
//     access at preview time rather than trusting the rink list it offered;
//   * committing creates exactly the previewed AVAILABLE Game ice;
//   * re-running the same template is idempotent — zero new, all duplicates,
//     and the create button is disabled;
//   * an exclusion date is honored (fewer slots, and it is reported);
//   * each selected weekday carries its OWN local start/end time (#158 flow):
//     a narrower Thursday window yields fewer Thursday games than Tuesday;
//   * commit is bound to the preview: editing the template after Preview drops
//     the preview + Create, so an edited form can never be committed;
//   * a stale preview (its resolved snapshot moved) is refused by the server
//     and the UI re-previews the current proposal instead of writing it;
//   * an exact-tuple collision with existing incompatible ice (e.g. a
//     maintenance slot) is reported as a conflict, never hidden as capacity;
//   * in a DST-observing Program timezone (#315), a spring-forward window
//     whose boundary falls in the nonexistent local hour is visibly reported
//     and generates nothing; the same day's window widened to span the gap
//     commits exactly its 2 real-duration slots; and a fall-back day's 4
//     real-hour slots — two of which share a repeated local clock time — are
//     each visibly distinguished (not shown as identical rows);
//   * a SINGLE row that itself crosses a DST offset change — nothing else
//     that day repeats either boundary — is still visibly qualified with
//     both UTC offsets and an explicit transition callout, in both
//     directions, with the exact real UTC duration/tuple committed as
//     previewed (#313 follow-up review).
//
// September 2026 has Tuesdays 1,8,15,22,29 and Thursdays 3,10,17,24 = 9 days;
// a 18:00-22:00 window with 60-minute games + 15-minute turnover yields 3 games
// per day => 27 slots on one accessible rink.
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
const EXPECTED_NEW = 27;               // 9 Tue/Thu days * 3 games
const VIEWPORTS = [
  { label: "desktop", width: 1440, height: 900, port: 8283 },
  { label: "phone", width: 390, height: 844, port: 8284 },
];
const CREATE_FOCUS_MODES = Object.freeze(["keyboard", "context", "body", "back"]);

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

// Preview using whatever date range is already in the form (each Preview fully
// re-renders #content, so any prior panel is dropped first to make the wait
// block for the FRESH render rather than matching a stale one).
async function previewCurrent(page) {
  await page.evaluate(() => { const p = document.querySelector(".ib-preview"); if (p) p.remove(); });
  await page.click("[data-ib-preview]");
  await page.waitForSelector(".ib-preview, .banner.warn", { timeout: 15000 });
}

// Fill the builder date range to the September 2026 default, then Preview.
async function preview(page) {
  await page.fill("#ib-from", "2026-09-01");
  await page.fill("#ib-to", "2026-09-30");
  await previewCurrent(page);
}

function deadline(promise, label, timeoutMs = 15000) {
  let timer;
  const timeout = new Promise((_, reject) => {
    timer = setTimeout(() => reject(new Error(label)), timeoutMs);
  });
  return Promise.race([promise, timeout]).finally(() => clearTimeout(timer));
}

// Hold the next matching real response only after the backend has answered.
// That makes the loading window deterministic without replacing the response
// with a fixture, while the one-shot arm prevents later overview reads from
// being accidentally captured by the same assertion.
async function holdNextRealResponse(page, pattern, requestLabel, label, skip = 0) {
  let armed = true;
  let released = false;
  let started = false;
  let markCaptured;
  let markDelivered;
  let releaseGate;
  const captured = new Promise((resolve) => { markCaptured = resolve; });
  const delivered = new Promise((resolve) => { markDelivered = resolve; });
  const gate = new Promise((resolve) => { releaseGate = resolve; });
  let matchesSeen = 0;
  const handler = async (route) => {
    matchesSeen += 1;
    if (matchesSeen <= skip) return route.fallback();
    if (!armed) return route.fallback();
    armed = false;
    started = true;
    try {
      const response = await route.fetch();
      markCaptured(response.status());
      await gate;
      await route.fulfill({ response });
    } finally {
      markDelivered();
    }
  };
  await page.route(pattern, handler);
  const release = () => {
    if (released) return;
    released = true;
    releaseGate();
  };
  return {
    async waitCaptured(expectedStatus = 200) {
      const status = await deadline(captured,
        `${label}: real ${requestLabel} was never captured`, 30000);
      if (status !== expectedStatus) {
        throw new Error(`${label}: ${requestLabel} answered ${status}, expected ${expectedStatus}`);
      }
      return status;
    },
    release,
    async finish() {
      release();
      await deadline(delivered,
        `${label}: held ${requestLabel} was not delivered`, 10000);
      await page.unroute(pattern, handler);
    },
    async cleanup() {
      release();
      // Removing a handler while its held route is still live makes Playwright
      // auto-handle that route; the resumed handler then throws "already
      // handled" and masks the assertion that caused cleanup. Let an entered
      // handler finish first, but never wait when the action failed before the
      // matching request began.
      if (started) {
        await deadline(delivered,
          `${label}: held ${requestLabel} did not finish during cleanup`, 10000);
      }
      await page.unroute(pattern, handler);
    },
  };
}

const holdNextOverview = (page, label) => holdNextRealResponse(
  page, /\/api\/demo\/overview(?:\?|$)/, "/api/demo/overview", label);
const holdSecondOverview = (page, label) => holdNextRealResponse(
  page, /\/api\/demo\/overview(?:\?|$)/, "/api/demo/overview", label, 1);
const holdNextNotifications = (page, label) => holdNextRealResponse(
  page, /\/api\/notifications(?:\?|$)/, "/api/notifications", label);
const holdNextIceCommit = (page, label) => holdNextRealResponse(
  page, /\/api\/setup\/ice-availability\/commit(?:\?|$)/,
  "/api/setup/ice-availability/commit", label);
const holdNextIcePreview = (page, label) => holdNextRealResponse(
  page, /\/api\/setup\/ice-availability\/preview(?:\?|$)/,
  "/api/setup/ice-availability/preview", label);

function previewState(page) {
  return page.evaluate(() => {
    const p = document.querySelector(".ib-preview");
    if (!p) return { error: !!document.querySelector(".banner.warn") };
    const commit = document.querySelector("[data-ib-commit]");
    return {
      new: +p.getAttribute("data-ib-new"),
      duplicate: +p.getAttribute("data-ib-duplicate"),
      conflict: +p.getAttribute("data-ib-conflict"),
      accessMissing: +p.getAttribute("data-ib-access-missing"),
      skipped: +p.getAttribute("data-ib-skipped"),
      dstSkipped: +p.getAttribute("data-ib-dst-skipped"),
      dstAmbiguous: +p.getAttribute("data-ib-dst-ambiguous"),
      commitDisabled: commit ? commit.disabled : null,
    };
  });
}

// Check ONLY the given weekdays (unchecking every other one) via the visually-
// hidden custom toggles, firing one change event so the builder's listener
// re-renders each selected day's own time row (mirrors step (J)'s pattern).
async function selectWeekdaysOnly(page, weekdays) {
  await page.evaluate((wanted) => {
    const desired = new Set(wanted);
    const boxes = Array.from(document.querySelectorAll(".ib-weekday"));
    boxes.forEach((cb) => { cb.checked = desired.has(+cb.value); });
    if (boxes[0]) boxes[0].dispatchEvent(new Event("change", { bubbles: true }));
  }, weekdays);
  await page.waitForSelector(`.ib-wd-row[data-weekday="${weekdays[0]}"]`, { timeout: 10000 });
}

// Back remains actionable while its old Builder card is visibly STALE during
// an accepted context switch. Hold the switch's LIVE options response after
// the server has answered, so that exact window is deterministic. The first
// full render is optionally held after it has detached the Calendar date; a
// same-tuple successor must inherit the focus claim, while any newer connected
// focus choice must cancel it. Card ids come from the client authority rather
// than duplicating their literals here.
async function assertHeldContextBackFocus(page, fail, cardIds, contextIds) {
  const calendarCard = `[data-operational-card="${cardIds.calendar}"]`;
  const builderCard = `[data-operational-card="${cardIds.builder}"]`;
  const primaryValue = `${contextIds.primaryProgram}|${contextIds.primarySeason}`;
  const alternateValue = `${contextIds.alternateProgram}|${contextIds.alternateSeason}`;

  const waitForCurrentCalendar = async (value, label) => {
    await page.waitForFunction(({ cardId, wanted }) => {
      const select = document.getElementById("ctx-select");
      const root = document.querySelector(
        `[data-operational-card="${cardId}"]`);
      const entry = readCardState(cardId);
      return !contextSwitchIntentPending && select && select.value === wanted
        && root && !!root.querySelector(".cal-date")
        && cardIdentityCurrent(entry.identity);
    }, { cardId: cardIds.calendar, wanted: value }, { timeout: 30000 })
      .catch((error) => fail(`${label} never reached a settled current Calendar: `
        + error.message));
  };

  const selectContext = async (value, label) => {
    const offered = await page.$eval("#ctx-select", (select, wanted) => ({
      hidden: select.hidden,
      current: select.value,
      offered: Array.from(select.options, (option) => option.value).includes(wanted),
    }), value);
    if (offered.hidden || !offered.offered) {
      fail(`${label} is not operator-reachable through #ctx-select: `
        + JSON.stringify(offered));
    }
    if (offered.current !== value) await page.selectOption("#ctx-select", value);
    await waitForCurrentCalendar(value, label);
  };

  const focusState = () => page.evaluate(({ calendarId, builderId }) => {
    const active = document.activeElement;
    const root = active && active.closest
      ? active.closest("[data-operational-card]") : null;
    const calendarRoot = document.querySelector(
      `[data-operational-card="${calendarId}"]`);
    const builderRoot = document.querySelector(
      `[data-operational-card="${builderId}"]`);
    const exactDate = calendarRoot && calendarRoot.querySelector(".cal-date");
    const calendarEntry = readCardState(calendarId);
    return {
      activeTag: active && active.tagName,
      activeId: active && active.id,
      activeConnected: !!(active && active.isConnected),
      activeVisible: !!(active && (active.offsetParent !== null
        || active.getClientRects().length > 0)),
      activeIsExactCalendarDate: !!active && active === exactDate,
      activeCard: root && root.dataset.operationalCard,
      builderOpen: !!iceBuilder,
      builderPresent: !!builderRoot,
      builderState: builderRoot && builderRoot.dataset.cardState,
      calendarPresent: !!calendarRoot,
      calendarCurrent: cardIdentityCurrent(calendarEntry.identity),
      switchPending: !!contextSwitchIntentPending,
      selectedValue: (document.getElementById("ctx-select") || {}).value || null,
      oldBackConnected: !!(window.__heldContextBackSource
        && window.__heldContextBackSource.isConnected),
      immediateDateConnected: !!(window.__heldContextBackDate
        && window.__heldContextBackDate.isConnected),
      activeIsImmediateDate: !!active && active === window.__heldContextBackDate,
      renderPass: typeof renderPass === "number" ? renderPass : null,
    };
  }, { calendarId: cardIds.calendar, builderId: cardIds.builder });

  const openBuilderFromPrimary = async (label) => {
    await selectContext(primaryValue, `${label}: restore original fixture`);
    await page.evaluate(() => {
      iceBuilder = null;
      calendarMode = "month";
      repaintCalendarSurface(CALENDAR_CARD);
    });
    const opener = page.locator(`${calendarCard} [data-ice-builder-open]`);
    const openerCount = await opener.count();
    if (openerCount !== 1) {
      fail(`${label}: expected exactly one Ice Builder opener, got ${openerCount}`);
    }
    await opener.click();
    await page.waitForSelector(`${builderCard} .ib-form`, { timeout: 10000 });
  };

  const exercise = async (label, {
    overlapRender = false,
    moveFocusBeforeRelease = false,
    moveFocusBeforeSuccessor = false,
  } = {}) => {
    await openBuilderFromPrimary(label);

    let optionsArmed = true;
    let releaseOptions = () => {};
    let markOptionsCaptured = () => {};
    const optionsCaptured = new Promise((resolve) => { markOptionsCaptured = resolve; });
    const optionsGate = new Promise((resolve) => { releaseOptions = resolve; });
    const optionsPattern = /\/api\/context\/options(?:\?|$)/;
    const holdOptions = async (route) => {
      if (!optionsArmed) return route.fallback();
      optionsArmed = false;
      const response = await route.fetch();
      markOptionsCaptured(response.status());
      await optionsGate;
      await route.fulfill({ response });
    };

    let notificationArmed = false;
    let releaseNotification = () => {};
    let markNotificationCaptured = () => {};
    let markNotificationDelivered = () => {};
    const notificationCaptured = new Promise(
      (resolve) => { markNotificationCaptured = resolve; });
    const notificationDelivered = new Promise(
      (resolve) => { markNotificationDelivered = resolve; });
    const notificationGate = new Promise(
      (resolve) => { releaseNotification = resolve; });
    const notificationPattern = /\/api\/notifications(?:\?|$)/;
    const holdNotification = async (route) => {
      if (!notificationArmed) return route.fallback();
      notificationArmed = false;
      const response = await route.fetch();
      markNotificationCaptured(response.status());
      await notificationGate;
      await route.fulfill({ response });
      markNotificationDelivered();
    };

    await page.route(optionsPattern, holdOptions);
    await page.route(notificationPattern, holdNotification);
    let optionsReleased = false;
    let notificationReleased = false;
    try {
      await page.selectOption("#ctx-select", alternateValue);
      const optionsStatus = await deadline(optionsCaptured,
        `${label}: live /api/context/options was never captured`);
      if (optionsStatus !== 200) {
        fail(`${label}: live /api/context/options answered ${optionsStatus}`);
      }

      // The POST has genuinely committed B before its live options response is
      // delivered to the browser. Without this read-back, STALE could be a
      // fixture artefact and the race would prove nothing about an accepted switch.
      const accepted = await page.evaluate(async () => {
        const response = await fetch("/api/context", { credentials: "same-origin" });
        let body = null;
        try { body = await response.json(); } catch (_) { body = null; }
        return {
          status: response.status,
          body,
          tuple: currentCardTuple(),
          switchPending: !!contextSwitchIntentPending,
        };
      });
      if (accepted.status !== 200 || !accepted.body
          || accepted.body.program_id !== contextIds.alternateProgram
          || accepted.body.season_id !== contextIds.alternateSeason
          || accepted.tuple.program_id !== contextIds.alternateProgram
          || accepted.tuple.season_id !== contextIds.alternateSeason
          || !accepted.switchPending) {
        fail(`${label}: held window is not an accepted B switch: `
          + JSON.stringify(accepted));
      }

      await page.waitForFunction((builderId) => {
        const card = document.querySelector(
          `[data-operational-card="${builderId}"]`);
        return card && card.dataset.cardState === "stale"
          && !!card.querySelector("[data-ib-cancel]");
      }, cardIds.builder, { timeout: 10000 });

      const back = page.locator(`${builderCard} [data-ib-cancel]`);
      await back.focus();
      await page.evaluate((selector) => {
        window.__heldContextBackSource = document.querySelector(selector);
        window.__heldContextBackDate = null;
      }, `${builderCard} [data-ib-cancel]`);
      await page.keyboard.press("Enter");
      await page.evaluate((calendarId) => {
        const root = document.querySelector(
          `[data-operational-card="${calendarId}"]`);
        window.__heldContextBackDate = root && root.querySelector(".cal-date");
      }, cardIds.calendar);
      const immediate = await focusState();
      if (immediate.builderOpen || immediate.builderPresent
          || !immediate.calendarPresent || immediate.oldBackConnected
          || !immediate.immediateDateConnected || !immediate.activeIsImmediateDate
          || !immediate.activeIsExactCalendarDate || !immediate.activeConnected
          || !immediate.activeVisible || immediate.activeCard !== cardIds.calendar
          || !immediate.switchPending || immediate.selectedValue !== alternateValue) {
        fail(`${label}: keyboard Back did not immediately land on its connected `
          + `Calendar date while the accepted switch remained held: `
          + JSON.stringify(immediate));
      }

      if (moveFocusBeforeRelease) {
        await page.locator("#ctx-select").focus();
        const moved = await focusState();
        if (moved.activeId !== "ctx-select" || !moved.activeConnected
            || !moved.activeVisible || !moved.immediateDateConnected) {
          fail(`${label}: could not establish the newer pre-release focus choice: `
            + JSON.stringify(moved));
        }
      }

      if (overlapRender) notificationArmed = true;
      releaseOptions();
      optionsReleased = true;

      if (overlapRender) {
        const notificationStatus = await deadline(notificationCaptured,
          `${label}: the first full render never reached held /api/notifications`, 30000);
        if (notificationStatus !== 200) {
          fail(`${label}: held /api/notifications answered ${notificationStatus}`);
        }
        const held = await focusState();
        if (held.immediateDateConnected || held.oldBackConnected
            || !(held.renderPass > immediate.renderPass)) {
          fail(`${label}: first full render did not reach the held post-skeleton `
            + `window with both navigation nodes detached: ${JSON.stringify(held)}`);
        }
        if (moveFocusBeforeSuccessor) {
          await page.locator("#ctx-select").focus();
          const moved = await focusState();
          if (moved.activeId !== "ctx-select" || !moved.activeConnected
              || !moved.activeVisible) {
            fail(`${label}: newer connected focus was not established before the `
              + `same-tuple successor: `
              + JSON.stringify(moved));
          }
        }
        const firstRenderPass = held.renderPass;
        await page.evaluate(async () => { await render(); });
        await waitForCurrentCalendar(alternateValue,
          `${label}: same-tuple successor Calendar`);
        const successor = await focusState();
        if (!(successor.renderPass > firstRenderPass)
            || successor.oldBackConnected || successor.immediateDateConnected) {
          fail(`${label}: same-tuple successor did not finish after replacing both `
            + `old navigation nodes: `
            + JSON.stringify(successor));
        }
        if (moveFocusBeforeSuccessor) {
          if (successor.activeId !== "ctx-select" || !successor.activeConnected
              || !successor.activeVisible) {
            fail(`${label}: same-tuple successor stole the newer context-selector focus: `
              + JSON.stringify(successor));
          }
        } else if (!successor.activeIsExactCalendarDate
            || !successor.activeConnected || !successor.activeVisible
            || successor.activeCard !== cardIds.calendar
            || !successor.calendarCurrent) {
          fail(`${label}: same-tuple successor did not inherit focus onto its `
            + `current Calendar date: ${JSON.stringify(successor)}`);
        }
        releaseNotification();
        notificationReleased = true;
        await deadline(notificationDelivered,
          `${label}: held /api/notifications was not released`, 10000);
      }

      await waitForCurrentCalendar(alternateValue,
        `${label}: accepted alternate context`);
      const final = await focusState();
      if (final.oldBackConnected || final.immediateDateConnected
          || final.builderOpen || final.builderPresent || !final.calendarPresent
          || !final.calendarCurrent || final.selectedValue !== alternateValue
          || final.switchPending) {
        fail(`${label}: final reconciliation retained stale navigation state: `
          + JSON.stringify(final));
      }
      if (moveFocusBeforeRelease || moveFocusBeforeSuccessor) {
        if (final.activeId !== "ctx-select" || !final.activeConnected
            || !final.activeVisible) {
          fail(`${label}: final reconciliation stole the operator's newer focus: `
            + JSON.stringify(final));
        }
      } else if (!final.activeIsExactCalendarDate || !final.activeConnected
          || !final.activeVisible || final.activeCard !== cardIds.calendar) {
        fail(`${label}: final current Calendar did not own exact .cal-date focus: `
          + JSON.stringify(final));
      }
    } finally {
      if (!optionsReleased) releaseOptions();
      if (!notificationReleased) releaseNotification();
      await page.unroute(optionsPattern, holdOptions);
      await page.unroute(notificationPattern, holdNotification);
    }
  };

  await exercise("held accepted switch + overlapping render", {
    overlapRender: true,
  });
  await exercise("new focus before held switch release", {
    moveFocusBeforeRelease: true,
  });
  await exercise("new focus before same-tuple successor", {
    overlapRender: true,
    moveFocusBeforeSuccessor: true,
  });

  // The rest of this long journey creates ice under the original fixture.
  // Restore it through the real switcher and prove the restored Calendar is
  // current before handing control back to the existing assertions.
  await selectContext(primaryValue, "restore original fixture after focus races");
  await page.evaluate(() => {
    delete window.__heldContextBackSource;
    delete window.__heldContextBackDate;
  });
}

// Month cells and Ice Builder's Back button are navigation controls, not
// toolbar toggles: their source has no same-selector replacement in the
// destination. The Calendar date is their shared semantic destination. Sweep
// the rendered Month axis rather than naming a convenient day, then drive Back
// through the real async Builder entry. Each negative leg proves a synthetic
// activation still changes the view without stealing a newer focus choice.
async function assertCalendarNavigationFocus(page, fail, contextIds) {
  const cardIds = await page.evaluate(() => ({
    calendar: CALENDAR_CARD,
    builder: ICE_BUILDER_CARD,
  }));
  const calendarCard = `[data-operational-card="${cardIds.calendar}"]`;
  const builderCard = `[data-operational-card="${cardIds.builder}"]`;
  const anchor = await page.evaluate(() => calendarDate);
  const monthDays = await page.$$eval(`${calendarCard} button[data-cal-day]`,
    (buttons) => buttons.map((button) => button.dataset.calDay));
  if (monthDays.length !== 42 || new Set(monthDays).size !== 42) {
    fail(`Month navigation focus axis must contain 42 unique rendered days: `
      + JSON.stringify(monthDays));
  }

  const resetMonth = () => page.evaluate((date) => {
    iceBuilder = null;
    calendarDate = date;
    calendarMode = "month";
    repaintCalendarSurface(CALENDAR_CARD);
  }, anchor);

  for (const day of monthDays) {
    await resetMonth();
    const selector = `${calendarCard} button[data-cal-day="${day}"]`;
    const source = page.locator(selector);
    if (await source.count() !== 1) {
      fail(`expected one rendered Month cell for ${day}, got ${await source.count()}`);
    }
    await source.focus();
    await page.evaluate((query) => {
      window.__calendarNavigationFocusSource = document.querySelector(query);
    }, selector);
    await page.keyboard.press("Enter");
    const landed = await page.evaluate((expectedDay) => {
      const oldSource = window.__calendarNavigationFocusSource;
      const active = document.activeElement;
      const root = active && active.closest
        ? active.closest("[data-operational-card]") : null;
      const entry = readCardState(CALENDAR_CARD);
      return {
        date: calendarDate,
        mode: calendarMode,
        oldSourceConnected: !!(oldSource && oldSource.isConnected),
        sameNode: !!active && active === oldSource,
        activeTag: active && active.tagName,
        activeIsCalendarDate: !!(active && active.matches
          && active.matches(".cal-date")),
        activeTabIndex: active && active.tabIndex,
        activeConnected: !!(active && active.isConnected),
        activeVisible: !!(active && (active.offsetParent !== null
          || active.getClientRects().length > 0)),
        card: root && root.dataset.operationalCard,
        cardCurrent: cardIdentityCurrent(entry.identity),
        expectedDay,
      };
    }, day);
    if (landed.date !== day || landed.mode !== "day"
        || landed.oldSourceConnected || landed.sameNode
        || landed.activeTag !== "DIV" || !landed.activeIsCalendarDate
        || landed.activeTabIndex !== -1 || !landed.activeConnected
        || !landed.activeVisible || landed.card !== cardIds.calendar
        || !landed.cardCurrent) {
      fail(`Month day ${day} did not move focus to its Calendar destination: `
        + JSON.stringify(landed));
    }
  }

  // The source-focus guard is part of the contract: code may activate a local
  // navigation control, but it cannot override a person's newer focus choice.
  await resetMonth();
  const syntheticDay = monthDays[0];
  await page.locator("#ctx-select").focus();
  const syntheticMonth = await page.evaluate((day) => {
    const source = document.querySelector(`button[data-cal-day="${day}"]`);
    if (!source) return { prepared: false };
    window.__calendarNavigationFocusSource = source;
    source.click();
    return {
      prepared: true,
      date: calendarDate,
      mode: calendarMode,
      activeId: document.activeElement && document.activeElement.id,
      oldSourceConnected: source.isConnected,
    };
  }, syntheticDay);
  if (!syntheticMonth.prepared || syntheticMonth.date !== syntheticDay
      || syntheticMonth.mode !== "day" || syntheticMonth.activeId !== "ctx-select"
      || syntheticMonth.oldSourceConnected) {
    fail(`an unfocused Month-day activation stole focus: `
      + JSON.stringify(syntheticMonth));
  }

  await resetMonth();
  const openBuilder = page.locator(`${calendarCard} [data-ice-builder-open]`);
  await openBuilder.focus();
  await page.keyboard.press("Enter");
  await page.waitForSelector(`${builderCard} .ib-form`, { timeout: 10000 });
  const back = page.locator(`${builderCard} [data-ib-cancel]`);
  await back.focus();
  await page.evaluate((query) => {
    window.__calendarNavigationFocusSource = document.querySelector(query);
  }, `${builderCard} [data-ib-cancel]`);
  await page.keyboard.press("Enter");
  const returned = await page.evaluate(() => {
    const oldSource = window.__calendarNavigationFocusSource;
    const active = document.activeElement;
    const root = active && active.closest
      ? active.closest("[data-operational-card]") : null;
    const entry = readCardState(CALENDAR_CARD);
    return {
      builderOpen: !!iceBuilder,
      builderCard: !!document.querySelector(
        `[data-operational-card="${ICE_BUILDER_CARD}"]`),
      calendarCard: !!document.querySelector(
        `[data-operational-card="${CALENDAR_CARD}"]`),
      oldSourceConnected: !!(oldSource && oldSource.isConnected),
      sameNode: !!active && active === oldSource,
      activeTag: active && active.tagName,
      activeIsCalendarDate: !!(active && active.matches
        && active.matches(".cal-date")),
      activeTabIndex: active && active.tabIndex,
      activeConnected: !!(active && active.isConnected),
      activeVisible: !!(active && (active.offsetParent !== null
        || active.getClientRects().length > 0)),
      card: root && root.dataset.operationalCard,
      cardCurrent: cardIdentityCurrent(entry.identity),
    };
  });
  if (returned.builderOpen || returned.builderCard || !returned.calendarCard
      || returned.oldSourceConnected || returned.sameNode
      || returned.activeTag !== "DIV" || !returned.activeIsCalendarDate
      || returned.activeTabIndex !== -1 || !returned.activeConnected
      || !returned.activeVisible || returned.card !== cardIds.calendar
      || !returned.cardCurrent) {
    fail(`Ice Builder Back did not return focus to Calendar: `
      + JSON.stringify(returned));
  }

  // Repeat Back without focusing it. The transition must still work, while
  // the persistent context selector keeps the focus the operator gave it.
  await page.locator(`${calendarCard} [data-ice-builder-open]`).click();
  await page.waitForSelector(`${builderCard} .ib-form`, { timeout: 10000 });
  await page.locator("#ctx-select").focus();
  const syntheticBack = await page.evaluate(() => {
    const source = document.querySelector("[data-ib-cancel]");
    if (!source) return { prepared: false };
    window.__calendarNavigationFocusSource = source;
    source.click();
    return {
      prepared: true,
      builderOpen: !!iceBuilder,
      activeId: document.activeElement && document.activeElement.id,
      oldSourceConnected: source.isConnected,
    };
  });
  if (!syntheticBack.prepared || syntheticBack.builderOpen
      || syntheticBack.activeId !== "ctx-select"
      || syntheticBack.oldSourceConnected) {
    fail(`an unfocused Ice Builder Back activation stole focus: `
      + JSON.stringify(syntheticBack));
  }
  await resetMonth();
  await page.evaluate(() => { delete window.__calendarNavigationFocusSource; });
  await assertHeldContextBackFocus(page, fail, cardIds, contextIds);
  return cardIds;
}

function navigationLoadingFocusState(page, cardIds) {
  return page.evaluate(({ calendarId, builderId }) => {
    const active = document.activeElement;
    const activeRoot = active && active.closest
      ? active.closest("[data-operational-card]") : null;
    const calendarRoot = document.querySelector(
      `[data-operational-card="${calendarId}"]`);
    const builderRoot = document.querySelector(
      `[data-operational-card="${builderId}"]`);
    const builderHeading = builderRoot && builderRoot.querySelector(".ib-head h2");
    const builderLoadingHeading = builderRoot
      && builderRoot.querySelector(".operational-stale-note h2");
    const builderErrorHeading = builderRoot
      && builderRoot.querySelector(".banner.alert h2");
    const calendarLoadingHeading = calendarRoot
      && calendarRoot.querySelector(".operational-stale-note h2");
    const builderBack = builderRoot && builderRoot.querySelector("[data-ib-cancel]");
    const builderPreview = builderRoot && builderRoot.querySelector("[data-ib-preview]");
    const builderCommit = builderRoot && builderRoot.querySelector("[data-ib-commit]");
    const builderRink = builderRoot && builderRoot.querySelector(".ib-rink");
    const calendarOpen = calendarRoot
      && calendarRoot.querySelector("[data-ice-builder-open]");
    const calendarDateTarget = calendarRoot && calendarRoot.querySelector(".cal-date");
    const calendarEntry = readCardState(calendarId);
    const builderEntry = readCardState(builderId);
    return {
      activeTag: active && active.tagName,
      activeId: active && active.id,
      activeText: active && String(active.textContent || "").trim(),
      activeTabIndex: active && active.tabIndex,
      activeConnected: !!(active && active.isConnected),
      activeVisible: !!(active && (active.offsetParent !== null
        || active.getClientRects().length > 0)),
      activeIsBody: active === document.body || active === document.documentElement,
      activeCard: activeRoot && activeRoot.dataset.operationalCard,
      activeIsBuilderHeading: !!active && active === builderHeading,
      activeIsBuilderLoadingHeading: !!active && active === builderLoadingHeading,
      activeIsBuilderErrorHeading: !!active && active === builderErrorHeading,
      activeIsCalendarLoadingHeading: !!active && active === calendarLoadingHeading,
      activeIsBuilderBack: !!active && active === builderBack,
      activeIsBuilderPreview: !!active && active === builderPreview,
      activeIsBuilderCommit: !!active && active === builderCommit,
      activeIsBuilderRink: !!active && active === builderRink,
      activeIsCalendarOpen: !!active && active === calendarOpen,
      activeIsCalendarDate: !!active && active === calendarDateTarget,
      builderHeadingText: builderHeading
        && String(builderHeading.textContent || "").trim(),
      builderLoadingText: builderLoadingHeading
        && String(builderLoadingHeading.textContent || "").trim(),
      builderErrorText: builderErrorHeading
        && String(builderErrorHeading.textContent || "").trim(),
      calendarLoadingText: calendarLoadingHeading
        && String(calendarLoadingHeading.textContent || "").trim(),
      builderPresent: !!builderRoot,
      builderState: builderRoot && builderRoot.dataset.cardState,
      builderModelState: builderEntry && builderEntry.state,
      builderBusy: builderRoot && builderRoot.getAttribute("aria-busy"),
      builderCurrent: !!(builderEntry && builderEntry.identity
        && cardIdentityCurrent(builderEntry.identity)),
      builderForm: !!(builderRoot && builderRoot.querySelector(".ib-form")),
      calendarPresent: !!calendarRoot,
      calendarState: calendarRoot && calendarRoot.dataset.cardState,
      calendarBusy: calendarRoot && calendarRoot.getAttribute("aria-busy"),
      calendarCurrent: !!(calendarEntry && calendarEntry.identity
        && cardIdentityCurrent(calendarEntry.identity)),
      calendarOpen: !!calendarOpen,
      oldSourceConnected: !!(window.__loadingFocusSource
        && window.__loadingFocusSource.isConnected),
      oldBackConnected: !!(window.__loadingFocusBack
        && window.__loadingFocusBack.isConnected),
      oldRinkConnected: !!(window.__loadingFocusRink
        && window.__loadingFocusRink.isConnected),
      oldLoadingHeadingConnected: !!(window.__loadingFocusHeading
        && window.__loadingFocusHeading.isConnected),
      renderPass: typeof renderPass === "number" ? renderPass : null,
      sameTupleRenderError: window.__sameTupleRenderError || "",
    };
  }, { calendarId: cardIds.calendar, builderId: cardIds.builder });
}

// Opening Builder is an async navigation whose source surface disappears on
// the first loading paint. Focus inside that departing Calendar owns a semantic
// destination immediately, while a synthetic activation from BODY owns none
// and persistent external focus stays authoritative. The keyboard leg also
// moves from the loading heading to Back before settlement, proving the final
// repaint carries that newer card-local choice to Back's replacement.
async function assertCalendarBuilderLoadingFocus(page, fail, cardIds) {
  const calendarCard = `[data-operational-card="${cardIds.calendar}"]`;
  const builderCard = `[data-operational-card="${cardIds.builder}"]`;
  const waitForBuilderLoading = () => page.waitForFunction((builderId) => {
    const root = document.querySelector(`[data-operational-card="${builderId}"]`);
    return root && root.dataset.cardState === "loading"
      && root.getAttribute("aria-busy") === "true"
      && !!root.querySelector(".ib-head h2")
      && !!root.querySelector("[data-ib-cancel]");
  }, cardIds.builder, { timeout: 10000 });
  const waitForBuilderSettled = () => page.waitForFunction((builderId) => {
    const root = document.querySelector(`[data-operational-card="${builderId}"]`);
    return root && root.dataset.cardState !== "loading"
      && root.getAttribute("aria-busy") === "false"
      && !!root.querySelector(".ib-form")
      && cardIdentityCurrent(readCardState(builderId).identity);
  }, cardIds.builder, { timeout: 10000 });
  const waitForCalendar = () => page.waitForFunction((calendarId) => {
    const root = document.querySelector(`[data-operational-card="${calendarId}"]`);
    return root && !!root.querySelector(".cal-date")
      && cardIdentityCurrent(readCardState(calendarId).identity);
  }, cardIds.calendar, { timeout: 10000 });

  let hold = await holdNextOverview(page, "keyboard Build ice loading focus");
  let finished = false;
  try {
    const opener = page.locator(`${calendarCard} [data-ice-builder-open]`);
    if (await opener.count() !== 1) fail("keyboard Build ice needs one live opener");
    await opener.focus();
    await page.evaluate((selector) => {
      window.__loadingFocusSource = document.querySelector(selector);
      window.__loadingFocusBack = null;
    }, `${calendarCard} [data-ice-builder-open]`);
    await page.keyboard.press("Enter");
    await hold.waitCaptured();
    await waitForBuilderLoading();
    const loading = await navigationLoadingFocusState(page, cardIds);
    if (loading.oldSourceConnected || loading.calendarPresent
        || !loading.builderPresent || loading.builderState !== "loading"
        || loading.builderBusy !== "true" || !loading.builderCurrent
        || !loading.activeIsBuilderHeading || loading.activeTag !== "H2"
        || loading.activeTabIndex !== -1 || !loading.activeConnected
        || !loading.activeVisible || loading.activeIsBody
        || loading.activeCard !== cardIds.builder
        || !/build recurring ice/i.test(loading.builderHeadingText || "")) {
      fail(`keyboard Build ice stranded or misdirected loading focus: `
        + JSON.stringify(loading));
    }

    const back = page.locator(`${builderCard} [data-ib-cancel]`);
    await back.focus();
    await page.evaluate((selector) => {
      window.__loadingFocusBack = document.querySelector(selector);
    }, `${builderCard} [data-ib-cancel]`);
    const moved = await navigationLoadingFocusState(page, cardIds);
    if (!moved.activeIsBuilderBack || !moved.activeConnected
        || !moved.activeVisible || moved.activeIsBody) {
      fail(`could not establish Back focus inside the held Builder: `
        + JSON.stringify(moved));
    }

    hold.release();
    await hold.finish();
    finished = true;
    await waitForBuilderSettled();
    const settled = await navigationLoadingFocusState(page, cardIds);
    if (settled.oldBackConnected || !settled.activeIsBuilderBack
        || !settled.activeConnected || !settled.activeVisible
        || settled.activeIsBody || settled.activeCard !== cardIds.builder
        || settled.builderState === "loading" || settled.builderBusy !== "false"
        || !settled.builderCurrent || !settled.builderForm) {
      fail(`Builder settlement did not carry focus to Back's replacement: `
        + JSON.stringify(settled));
    }
  } finally {
    if (!finished) await hold.cleanup();
  }

  await page.keyboard.press("Enter");
  await waitForCalendar();
  const returned = await navigationLoadingFocusState(page, cardIds);
  if (!returned.activeIsCalendarDate || !returned.activeConnected
      || !returned.activeVisible || returned.activeCard !== cardIds.calendar
      || !returned.calendarCurrent || returned.builderPresent) {
    fail(`held Builder Back did not settle on Calendar: ${JSON.stringify(returned)}`);
  }

  // A full same-tuple render is a third replacement boundary: it first
  // detaches Builder for the page skeleton, then queues a programmatic card
  // reconciliation after the shell is painted. Hold both the original card
  // read and render's real notifications read so the otherwise-transient
  // post-skeleton window is observable. Start a second same-tuple render while
  // that first pass still owns the detached source: only explicit carry
  // transfer lets the newer pass inherit a claim it can no longer recapture
  // from DOM. Focus must cross that successor render and card settlement; the
  // stale first render and original response must not reclaim it afterwards.
  hold = await holdNextOverview(page,
    "Builder loading focus across a same-tuple full render");
  let notificationsHold = await holdNextNotifications(page,
    "same-tuple full render loading-focus carry");
  let renderDone = null;
  finished = false;
  let notificationsFinished = false;
  try {
    const opener = page.locator(`${calendarCard} [data-ice-builder-open]`);
    await opener.focus();
    await page.keyboard.press("Enter");
    await hold.waitCaptured();
    await waitForBuilderLoading();
    const beforeRender = await page.evaluate(() => {
      window.__loadingFocusHeading = document.activeElement;
      window.__sameTupleRenderError = "";
      return {
        renderPass,
        headingConnected: !!(window.__loadingFocusHeading
          && window.__loadingFocusHeading.isConnected),
      };
    });
    if (!beforeRender.headingConnected) {
      fail(`same-tuple carry could not capture its live loading heading: `
        + JSON.stringify(beforeRender));
    }
    renderDone = page.evaluate(async () => {
      const before = renderPass;
      try {
        await render();
        return { before, after: renderPass, error: "" };
      } catch (error) {
        window.__sameTupleRenderError = String(error && error.message || error);
        return { before, after: renderPass, error: window.__sameTupleRenderError };
      }
    });
    await notificationsHold.waitCaptured();
    const duringRender = await navigationLoadingFocusState(page, cardIds);
    if (duringRender.oldLoadingHeadingConnected || !duringRender.activeIsBody
        || duringRender.renderPass <= beforeRender.renderPass
        || duringRender.builderModelState !== "loading"
        || duringRender.sameTupleRenderError) {
      fail(`full render did not expose the detached owned-loading window: `
        + JSON.stringify({ beforeRender, duringRender }));
    }
    const successorRenderResult = await page.evaluate(async () => {
      const before = renderPass;
      try {
        await render();
        return { before, after: renderPass, error: "" };
      } catch (error) {
        return { before, after: renderPass,
          error: String(error && error.message || error) };
      }
    });
    if (successorRenderResult.error
        || successorRenderResult.after <= successorRenderResult.before) {
      fail(`overlapping same-tuple successor render did not complete cleanly: `
        + JSON.stringify(successorRenderResult));
    }
    await waitForBuilderSettled();
    let settled = await navigationLoadingFocusState(page, cardIds);
    if (settled.oldLoadingHeadingConnected || !settled.activeIsBuilderPreview
        || !settled.activeConnected || !settled.activeVisible
        || settled.activeIsBody || settled.activeCard !== cardIds.builder
        || settled.builderState === "loading" || settled.builderBusy !== "false"
        || !settled.builderCurrent || !settled.builderForm) {
      fail(`same-tuple successor lost Builder focus after settlement: `
        + JSON.stringify(settled));
    }
    notificationsHold.release();
    await notificationsHold.finish();
    notificationsFinished = true;
    const staleRenderResult = await renderDone;
    if (staleRenderResult.error
        || staleRenderResult.after <= staleRenderResult.before) {
      fail(`stale overlapping render did not retire cleanly: `
        + JSON.stringify(staleRenderResult));
    }
    hold.release();
    await hold.finish();
    finished = true;
    await page.waitForTimeout(50);
    settled = await navigationLoadingFocusState(page, cardIds);
    if (!settled.activeIsBuilderPreview || !settled.activeConnected
        || !settled.activeVisible || settled.activeIsBody
        || settled.builderState === "loading" || !settled.builderCurrent) {
      fail(`stale predecessor changed focus after the same-tuple successor: `
        + JSON.stringify(settled));
    }
  } finally {
    if (!notificationsFinished) await notificationsHold.cleanup();
    if (!finished) await hold.cleanup();
  }
  await page.locator(`${builderCard} [data-ib-cancel]`).focus();
  await page.keyboard.press("Enter");
  await waitForCalendar();

  // The source request may settle while the full render is still awaiting an
  // unrelated page read. The carry is earned while the source is LOADING; it
  // must not expire merely because the model advances before the render can
  // paint its replacement. Release the original overview first, prove the
  // state advanced while focus remained orphaned, then let render finish.
  hold = await holdNextOverview(page,
    "Builder settlement during a same-tuple full render");
  notificationsHold = await holdNextNotifications(page,
    "same-tuple render held past Builder settlement");
  renderDone = null;
  finished = false;
  notificationsFinished = false;
  try {
    const opener = page.locator(`${calendarCard} [data-ice-builder-open]`);
    await opener.focus();
    await page.keyboard.press("Enter");
    await hold.waitCaptured();
    await waitForBuilderLoading();
    const beforeRender = await page.evaluate(() => {
      window.__loadingFocusHeading = document.activeElement;
      window.__sameTupleRenderError = "";
      return renderPass;
    });
    renderDone = page.evaluate(async () => {
      const before = renderPass;
      try {
        await render();
        return { before, after: renderPass, error: "" };
      } catch (error) {
        window.__sameTupleRenderError = String(error && error.message || error);
        return { before, after: renderPass, error: window.__sameTupleRenderError };
      }
    });
    await notificationsHold.waitCaptured();
    let state = await navigationLoadingFocusState(page, cardIds);
    if (state.oldLoadingHeadingConnected || !state.activeIsBody
        || state.renderPass <= beforeRender
        || state.builderModelState !== "loading") {
      fail(`settle-during-render probe missed its detached loading window: `
        + JSON.stringify({ beforeRender, state }));
    }
    hold.release();
    await hold.finish();
    finished = true;
    await waitForBuilderSettled();
    state = await navigationLoadingFocusState(page, cardIds);
    if (!state.activeIsBody || state.builderState === "loading"
        || state.builderModelState === "loading" || !state.builderCurrent
        || !state.builderForm || state.oldLoadingHeadingConnected) {
      fail(`Builder did not settle behind the held render without stealing focus: `
        + JSON.stringify(state));
    }
    notificationsHold.release();
    await notificationsHold.finish();
    notificationsFinished = true;
    const renderResult = await renderDone;
    if (renderResult.error || renderResult.after <= renderResult.before) {
      fail(`render held past Builder settlement did not complete cleanly: `
        + JSON.stringify(renderResult));
    }
    await waitForBuilderSettled();
    state = await navigationLoadingFocusState(page, cardIds);
    if (!state.activeIsBuilderPreview || !state.activeConnected
        || !state.activeVisible || state.activeIsBody
        || state.activeCard !== cardIds.builder
        || state.builderState === "loading" || state.builderBusy !== "false"
        || !state.builderCurrent || !state.builderForm) {
      fail(`settled-before-finish carry missed the successor destination: `
        + JSON.stringify(state));
    }
  } finally {
    if (!notificationsFinished) await notificationsHold.cleanup();
    if (!finished) await hold.cleanup();
  }
  await page.locator(`${builderCard} [data-ib-cancel]`).focus();
  await page.keyboard.press("Enter");
  await waitForCalendar();

  // The inverse chooses a newer persistent control only AFTER the carry was
  // captured and the full render detached its source. That connected focus
  // event invalidates the carry; neither the final shell paint, the queued
  // Builder successor, nor the stale predecessor may pull focus back.
  hold = await holdNextOverview(page,
    "same-tuple full render newer-focus inverse");
  notificationsHold = await holdNextNotifications(page,
    "same-tuple full render newer-focus inverse window");
  renderDone = null;
  finished = false;
  notificationsFinished = false;
  try {
    const opener = page.locator(`${calendarCard} [data-ice-builder-open]`);
    await opener.focus();
    await page.keyboard.press("Enter");
    await hold.waitCaptured();
    await waitForBuilderLoading();
    const beforeRender = await page.evaluate(() => {
      window.__loadingFocusHeading = document.activeElement;
      window.__sameTupleRenderError = "";
      return renderPass;
    });
    renderDone = page.evaluate(async () => {
      const before = renderPass;
      try {
        await render();
        return { before, after: renderPass, error: "" };
      } catch (error) {
        window.__sameTupleRenderError = String(error && error.message || error);
        return { before, after: renderPass, error: window.__sameTupleRenderError };
      }
    });
    await notificationsHold.waitCaptured();
    let state = await navigationLoadingFocusState(page, cardIds);
    if (state.oldLoadingHeadingConnected || !state.activeIsBody
        || state.renderPass <= beforeRender
        || state.builderModelState !== "loading") {
      fail(`inverse did not reach the post-skeleton carry window: `
        + JSON.stringify({ beforeRender, state }));
    }
    await page.locator("#ctx-select").focus();
    state = await navigationLoadingFocusState(page, cardIds);
    if (state.activeId !== "ctx-select" || !state.activeConnected
        || !state.activeVisible || state.activeIsBody) {
      fail(`inverse could not establish newer context-selector focus: `
        + JSON.stringify(state));
    }
    notificationsHold.release();
    await notificationsHold.finish();
    notificationsFinished = true;
    const renderResult = await renderDone;
    if (renderResult.error || renderResult.after <= renderResult.before) {
      fail(`inverse full render did not complete cleanly: `
        + JSON.stringify(renderResult));
    }
    await waitForBuilderSettled();
    state = await navigationLoadingFocusState(page, cardIds);
    if (state.activeId !== "ctx-select" || !state.activeConnected
        || !state.activeVisible || state.activeIsBody
        || state.builderState === "loading" || state.builderBusy !== "false"
        || !state.builderCurrent || !state.builderForm) {
      fail(`same-tuple successor stole newer context-selector focus: `
        + JSON.stringify(state));
    }
    hold.release();
    await hold.finish();
    finished = true;
    await page.waitForTimeout(50);
    state = await navigationLoadingFocusState(page, cardIds);
    if (state.activeId !== "ctx-select" || !state.activeConnected
        || !state.activeVisible || state.activeIsBody || !state.builderCurrent) {
      fail(`stale predecessor stole newer focus after full-render inverse: `
        + JSON.stringify(state));
    }
  } finally {
    if (!notificationsFinished) await notificationsHold.cleanup();
    if (!finished) await hold.cleanup();
  }
  await page.evaluate(() => {
    const back = document.querySelector("[data-ib-cancel]");
    if (back) back.click();
  });
  await waitForCalendar();
  const fullRenderInverseReturned = await navigationLoadingFocusState(page, cardIds);
  if (fullRenderInverseReturned.activeId !== "ctx-select"
      || !fullRenderInverseReturned.activeConnected
      || !fullRenderInverseReturned.activeVisible
      || fullRenderInverseReturned.builderPresent
      || !fullRenderInverseReturned.calendarCurrent) {
    fail(`same-tuple inverse return stole context-selector focus: `
      + JSON.stringify(fullRenderInverseReturned));
  }

  // A synthetic click on Build ice may still be owned by the operator when
  // their focus is on another control inside the Calendar card. Today is the
  // stable, keyboard-reachable representative: the departing card detaches it,
  // so the transition must advance semantically rather than strand BODY. This
  // is distinct from the persistent #ctx-select inverse below, which lives
  // outside the departing card and therefore must retain focus.
  hold = await holdNextOverview(page,
    "Calendar-owned synthetic Build ice loading focus");
  finished = false;
  try {
    const today = page.locator(`${calendarCard} [data-cal="0"]`);
    const opener = page.locator(`${calendarCard} [data-ice-builder-open]`);
    if (await today.count() !== 1 || await opener.count() !== 1) {
      fail("Calendar-owned synthetic Build ice needs one Today and one opener");
    }
    await today.focus();
    await page.evaluate((selector) => {
      window.__loadingFocusSource = document.querySelector(selector);
      window.__loadingFocusBack = null;
    }, `${calendarCard} [data-cal="0"]`);
    await page.evaluate((selector) => {
      document.querySelector(selector).click();
    }, `${calendarCard} [data-ice-builder-open]`);
    await hold.waitCaptured();
    await waitForBuilderLoading();
    const loading = await navigationLoadingFocusState(page, cardIds);
    if (loading.oldSourceConnected || loading.calendarPresent
        || !loading.builderPresent || loading.builderState !== "loading"
        || loading.builderBusy !== "true" || !loading.builderCurrent
        || !loading.activeIsBuilderHeading || loading.activeTag !== "H2"
        || loading.activeTabIndex !== -1 || !loading.activeConnected
        || !loading.activeVisible || loading.activeIsBody
        || loading.activeCard !== cardIds.builder
        || !/build recurring ice/i.test(loading.builderHeadingText || "")) {
      fail(`Calendar-owned synthetic Build ice misplaced loading focus: `
        + JSON.stringify(loading));
    }
    hold.release();
    await hold.finish();
    finished = true;
    await waitForBuilderSettled();
    const settled = await navigationLoadingFocusState(page, cardIds);
    if (!settled.activeIsBuilderPreview || !settled.activeConnected
        || !settled.activeVisible || settled.activeIsBody
        || settled.activeCard !== cardIds.builder
        || settled.builderState === "loading" || settled.builderBusy !== "false"
        || !settled.builderCurrent || !settled.builderForm) {
      fail(`Calendar-owned synthetic Build ice missed settled Preview focus: `
        + JSON.stringify(settled));
    }
  } finally {
    if (!finished) await hold.cleanup();
  }
  await page.locator(`${builderCard} [data-ib-cancel]`).focus();
  await page.keyboard.press("Enter");
  await waitForCalendar();
  const syntheticOwnedReturned = await navigationLoadingFocusState(page, cardIds);
  if (!syntheticOwnedReturned.activeIsCalendarDate
      || !syntheticOwnedReturned.activeConnected
      || !syntheticOwnedReturned.activeVisible
      || syntheticOwnedReturned.builderPresent
      || !syntheticOwnedReturned.calendarCurrent) {
    fail(`Calendar-owned synthetic Build ice did not return to Calendar: `
      + JSON.stringify(syntheticOwnedReturned));
  }

  hold = await holdNextOverview(page, "keyboard Build ice late no-steal");
  finished = false;
  try {
    const opener = page.locator(`${calendarCard} [data-ice-builder-open]`);
    await opener.focus();
    await page.evaluate((selector) => {
      const source = document.querySelector(selector);
      window.__loadingFocusSource = source;
      window.__loadingFocusBack = null;
    }, `${calendarCard} [data-ice-builder-open]`);
    await page.keyboard.press("Enter");
    await hold.waitCaptured();
    await waitForBuilderLoading();
    let loading = await navigationLoadingFocusState(page, cardIds);
    if (loading.oldSourceConnected || !loading.activeIsBuilderHeading
        || !loading.activeConnected || !loading.activeVisible
        || loading.activeIsBody || !loading.builderPresent
        || loading.builderState !== "loading" || loading.builderBusy !== "true"
        || !loading.builderCurrent || !loading.builderHeadingText) {
      fail(`keyboard Build ice missed its initial loading heading: `
        + JSON.stringify(loading));
    }
    await page.locator("#ctx-select").focus();
    loading = await navigationLoadingFocusState(page, cardIds);
    if (loading.activeId !== "ctx-select" || !loading.activeConnected
        || !loading.activeVisible || loading.activeIsBody) {
      fail(`could not establish newer context-selector focus during Builder load: `
        + JSON.stringify(loading));
    }
    hold.release();
    await hold.finish();
    finished = true;
    await waitForBuilderSettled();
    const settled = await navigationLoadingFocusState(page, cardIds);
    if (settled.activeId !== "ctx-select" || !settled.activeConnected
        || !settled.activeVisible || settled.activeIsBody
        || settled.builderState === "loading" || !settled.builderCurrent
        || !settled.builderForm) {
      fail(`Builder settlement stole newer context-selector focus: `
        + JSON.stringify(settled));
    }
  } finally {
    if (!finished) await hold.cleanup();
  }

  await page.evaluate(() => {
    const back = document.querySelector("[data-ib-cancel]");
    if (back) back.click();
  });
  await waitForCalendar();
  const inverseReturned = await navigationLoadingFocusState(page, cardIds);
  if (inverseReturned.activeId !== "ctx-select"
      || !inverseReturned.activeConnected || !inverseReturned.activeVisible
      || inverseReturned.builderPresent || !inverseReturned.calendarCurrent) {
    fail(`programmatic Builder return stole context-selector focus: `
      + JSON.stringify(inverseReturned));
  }

  // The explicit-false path must remain unfocused for the whole request, not
  // merely for its first paint. Keep BODY authoritative through the real
  // response and final replacement before separately testing a later in-card
  // focus choice below.
  hold = await holdNextOverview(page,
    "orphaned programmatic Build ice through settlement");
  finished = false;
  try {
    const orphaned = await page.evaluate(() => {
      const active = document.activeElement;
      if (active && active.blur) active.blur();
      return document.activeElement === document.body
        || document.activeElement === document.documentElement;
    });
    if (!orphaned) fail("could not establish BODY for the full no-source probe");
    const invoked = await page.evaluate((selector) => {
      const source = document.querySelector(selector);
      if (!source) return false;
      window.__loadingFocusSource = source;
      window.__loadingFocusBack = null;
      source.click();
      return true;
    }, `${calendarCard} [data-ice-builder-open]`);
    if (!invoked) fail("full no-source Build ice could not find its source");
    await hold.waitCaptured();
    await waitForBuilderLoading();
    let state = await navigationLoadingFocusState(page, cardIds);
    if (!state.activeIsBody || state.builderState !== "loading"
        || state.builderBusy !== "true" || !state.builderCurrent
        || state.oldSourceConnected) {
      fail(`no-source Build ice acquired focus during loading: `
        + JSON.stringify(state));
    }
    hold.release();
    await hold.finish();
    finished = true;
    await waitForBuilderSettled();
    state = await navigationLoadingFocusState(page, cardIds);
    if (!state.activeIsBody || state.builderState === "loading"
        || state.builderBusy !== "false" || !state.builderCurrent
        || !state.builderForm) {
      fail(`no-source Build ice acquired focus at settlement: `
        + JSON.stringify(state));
    }
  } finally {
    if (!finished) await hold.cleanup();
  }
  await page.evaluate(() => {
    const back = document.querySelector("[data-ib-cancel]");
    if (back) back.click();
  });
  await waitForCalendar();
  const bodyReturned = await navigationLoadingFocusState(page, cardIds);
  if (!bodyReturned.activeIsBody || bodyReturned.builderPresent
      || !bodyReturned.calendarCurrent) {
    fail(`no-source Builder return acquired focus: ${JSON.stringify(bodyReturned)}`);
  }

  // Explicit false suppresses inheritance from the unfocused source; it does
  // not freeze ownership for the whole request. A later focus choice inside
  // the loading destination is real operator intent and must survive settle.
  hold = await holdNextOverview(page,
    "orphaned programmatic Build ice with later Back ownership");
  finished = false;
  try {
    const orphaned = await page.evaluate(() => {
      const active = document.activeElement;
      if (active && active.blur) active.blur();
      return document.activeElement === document.body
        || document.activeElement === document.documentElement;
    });
    if (!orphaned) fail("could not establish BODY before no-source Build ice");
    const invoked = await page.evaluate((selector) => {
      const source = document.querySelector(selector);
      if (!source) return false;
      window.__loadingFocusSource = source;
      window.__loadingFocusBack = null;
      source.click();
      return true;
    }, `${calendarCard} [data-ice-builder-open]`);
    if (!invoked) fail("later-ownership Build ice could not find its source");
    await hold.waitCaptured();
    await waitForBuilderLoading();
    let loading = await navigationLoadingFocusState(page, cardIds);
    if (!loading.activeIsBody || loading.builderState !== "loading"
        || !loading.builderCurrent || loading.oldSourceConnected) {
      fail(`no-source Build ice acquired focus before later ownership: `
        + JSON.stringify(loading));
    }
    await page.locator(`${builderCard} [data-ib-cancel]`).focus();
    await page.evaluate((selector) => {
      window.__loadingFocusBack = document.querySelector(selector);
    }, `${builderCard} [data-ib-cancel]`);
    loading = await navigationLoadingFocusState(page, cardIds);
    if (!loading.activeIsBuilderBack || !loading.activeConnected
        || !loading.activeVisible || loading.activeIsBody) {
      fail(`could not establish later Back ownership: ${JSON.stringify(loading)}`);
    }
    hold.release();
    await hold.finish();
    finished = true;
    await waitForBuilderSettled();
    const settled = await navigationLoadingFocusState(page, cardIds);
    if (settled.oldBackConnected || !settled.activeIsBuilderBack
        || !settled.activeConnected || !settled.activeVisible
        || settled.activeIsBody || settled.activeCard !== cardIds.builder
        || settled.builderState === "loading" || !settled.builderCurrent
        || !settled.builderForm) {
      fail(`later Back ownership did not survive Builder settlement: `
        + JSON.stringify(settled));
    }
  } finally {
    if (!finished) await hold.cleanup();
  }
  await page.keyboard.press("Enter");
  await waitForCalendar();
  const laterReturned = await navigationLoadingFocusState(page, cardIds);
  if (!laterReturned.activeIsCalendarDate || !laterReturned.activeConnected
      || !laterReturned.activeVisible || laterReturned.builderPresent
      || !laterReturned.calendarCurrent) {
    fail(`later-owned Back did not return to Calendar: `
      + JSON.stringify(laterReturned));
  }

  // Builder has two distinct loading headings once retained data exists: its
  // stable card title and the status heading describing the in-flight read.
  // Drive a real weekday-change refresh, focus that status, then hold the
  // SECOND overview launched by full-render reconciliation (Calendar owns the
  // first). This makes target-kind propagation observable: collapsing the
  // carry to a bare card id moves focus to the title instead of retaining the
  // status through the queued Builder successor.
  await page.click(`${calendarCard} [data-ice-builder-open]`);
  await waitForBuilderSettled();
  hold = await holdNextOverview(page,
    "retained Builder status before same-tuple render");
  const successorHold = await holdSecondOverview(page,
    "queued Builder status after same-tuple render");
  notificationsHold = await holdNextNotifications(page,
    "retained Builder status full-render window");
  finished = false;
  let successorFinished = false;
  notificationsFinished = false;
  try {
    await page.locator(`${builderCard} .ib-day`).first().click();
    await hold.waitCaptured();
    await page.waitForFunction((builderId) => {
      const root = document.querySelector(`[data-operational-card="${builderId}"]`);
      return root && root.dataset.cardState === "loading"
        && !!root.querySelector(".operational-stale-note h2");
    }, cardIds.builder, { timeout: 10000 });
    const focusedStatus = await page.evaluate((selector) => {
      const heading = document.querySelector(selector);
      if (!heading) return false;
      heading.setAttribute("tabindex", "-1");
      heading.focus();
      window.__loadingFocusHeading = heading;
      return document.activeElement === heading;
    }, `${builderCard} .operational-stale-note h2`);
    if (!focusedStatus) fail("could not focus retained Builder's loading status");
    const renderResult = page.evaluate(async () => {
      const before = renderPass;
      await render();
      return { before, after: renderPass };
    });
    await notificationsHold.waitCaptured();
    const during = await navigationLoadingFocusState(page, cardIds);
    if (during.oldLoadingHeadingConnected || !during.activeIsBody
        || during.builderModelState !== "loading") {
      fail(`retained-status carry missed its detached full-render window: `
        + JSON.stringify(during));
    }
    notificationsHold.release();
    await notificationsHold.finish();
    notificationsFinished = true;
    const completedRender = await renderResult;
    if (completedRender.after <= completedRender.before) {
      fail(`retained-status full render did not advance: `
        + JSON.stringify(completedRender));
    }
    await successorHold.waitCaptured();
    const successorLoading = await navigationLoadingFocusState(page, cardIds);
    if (!successorLoading.activeIsBuilderLoadingHeading
        || successorLoading.activeIsBuilderHeading
        || !successorLoading.activeConnected || !successorLoading.activeVisible
        || successorLoading.activeIsBody
        || successorLoading.activeCard !== cardIds.builder
        || successorLoading.builderState !== "loading"
        || successorLoading.builderBusy !== "true"
        || !successorLoading.builderCurrent) {
      fail(`queued Builder successor collapsed status focus to its title: `
        + JSON.stringify(successorLoading));
    }
    successorHold.release();
    await successorHold.finish();
    successorFinished = true;
    await waitForBuilderSettled();
    let settled = await navigationLoadingFocusState(page, cardIds);
    if (!settled.activeIsBuilderPreview || !settled.activeConnected
        || !settled.activeVisible || settled.activeIsBody
        || settled.builderState === "loading" || !settled.builderCurrent) {
      fail(`queued status-owned Builder did not settle on Preview: `
        + JSON.stringify(settled));
    }
    hold.release();
    await hold.finish();
    finished = true;
    await page.waitForTimeout(50);
    settled = await navigationLoadingFocusState(page, cardIds);
    if (!settled.activeIsBuilderPreview || settled.activeIsBody
        || !settled.builderCurrent) {
      fail(`stale retained-options response reclaimed status-owned focus: `
        + JSON.stringify(settled));
    }
  } finally {
    if (!notificationsFinished) await notificationsHold.cleanup();
    if (!successorFinished) await successorHold.cleanup();
    if (!finished) await hold.cleanup();
  }
  await page.locator(`${builderCard} [data-ib-cancel]`).focus();
  await page.keyboard.press("Enter");
  await waitForCalendar();
  await page.evaluate(() => {
    delete window.__loadingFocusSource;
    delete window.__loadingFocusBack;
    delete window.__loadingFocusHeading;
  });
}

// Retry is the retained-form sibling of a fresh Builder open. Its LOADING
// surface remains editable, so a person can move from Retry to a checkbox
// before the real response settles. Rink checkboxes deliberately have no id or
// data-* identity; after replacement, their focus claim must fall back to the
// settled Preview action instead of becoming indistinguishable from BODY.
async function assertBuilderRetryFocusFallback(page, fail, cardIds) {
  const calendarCard = `[data-operational-card="${cardIds.calendar}"]`;
  const builderCard = `[data-operational-card="${cardIds.builder}"]`;
  await page.click(`${calendarCard} [data-ice-builder-open]`);
  await page.waitForSelector(`${builderCard} .ib-form`, { timeout: 10000 });

  const failurePattern = /\/api\/demo\/overview(?:\?|$)/;
  let failureCount = 0;
  const failOneOptionsRead = async (route) => {
    if (failureCount) return route.fallback();
    failureCount += 1;
    await route.fulfill({
      status: 503,
      contentType: "application/json",
      body: JSON.stringify({ error: {
        code: "held_retry_setup",
        message: "Deliberate options failure before the retry focus probe.",
      } }),
    });
  };
  await page.route(failurePattern, failOneOptionsRead);
  try {
    // The visible label drives the real Builder change listener. A weekday
    // change asks for fresh options while retaining the editable form.
    await page.locator(`${builderCard} .ib-day`).first().click();
    await page.waitForFunction((builderId) => {
      const root = document.querySelector(`[data-operational-card="${builderId}"]`);
      return root && root.dataset.cardState === "error"
        && !!root.querySelector(`[data-card-retry="${builderId}"]`)
        && !!root.querySelector(".ib-form .ib-rink");
    }, cardIds.builder, { timeout: 10000 });
  } finally {
    await page.unroute(failurePattern, failOneOptionsRead);
  }
  if (failureCount !== 1) {
    fail(`Builder retry setup must fail exactly one options read, got ${failureCount}`);
  }

  const hold = await holdNextOverview(page, "retained Builder retry focus fallback");
  let finished = false;
  try {
    const retry = page.locator(
      `${builderCard} [data-card-retry="${cardIds.builder}"]`);
    if (await retry.count() !== 1) fail("retained Builder retry needs one Retry action");
    await retry.focus();
    await page.keyboard.press("Enter");
    await hold.waitCaptured();
    await page.waitForFunction((builderId) => {
      const root = document.querySelector(`[data-operational-card="${builderId}"]`);
      return root && root.dataset.cardState === "loading"
        && root.getAttribute("aria-busy") === "true"
        && !!root.querySelector(".ib-form .ib-rink");
    }, cardIds.builder, { timeout: 10000 });

    const rink = page.locator(`${builderCard} .ib-rink`).first();
    await rink.focus();
    const unselectable = await rink.evaluate((checkbox) => ({
      id: checkbox.id,
      dataAttributes: Array.from(checkbox.attributes)
        .map((attribute) => attribute.name)
        .filter((name) => name.startsWith("data-")),
      active: document.activeElement === checkbox,
      connected: checkbox.isConnected,
      visible: checkbox.offsetParent !== null || checkbox.getClientRects().length > 0,
    }));
    if (unselectable.id || unselectable.dataAttributes.length
        || !unselectable.active || !unselectable.connected || !unselectable.visible) {
      fail(`retry probe did not establish an unselectable in-card checkbox: `
        + JSON.stringify(unselectable));
    }
    await page.evaluate((selector) => {
      window.__loadingFocusRink = document.querySelector(selector);
    }, `${builderCard} .ib-rink`);
    const held = await navigationLoadingFocusState(page, cardIds);
    if (!held.activeIsBuilderRink || !held.activeConnected || !held.activeVisible
        || held.activeIsBody || held.builderState !== "loading"
        || held.builderBusy !== "true" || !held.builderCurrent || !held.builderForm) {
      fail(`retained Builder retry did not hold the checkbox focus window: `
        + JSON.stringify(held));
    }

    hold.release();
    await hold.finish();
    finished = true;
    await page.waitForFunction((builderId) => {
      const root = document.querySelector(`[data-operational-card="${builderId}"]`);
      return root && root.dataset.cardState === "empty"
        && root.getAttribute("aria-busy") === "false"
        && !!root.querySelector(".ib-form [data-ib-preview]")
        && cardIdentityCurrent(readCardState(builderId).identity);
    }, cardIds.builder, { timeout: 10000 });
    const settled = await navigationLoadingFocusState(page, cardIds);
    if (settled.oldRinkConnected || !settled.activeIsBuilderPreview
        || !settled.activeConnected || !settled.activeVisible
        || settled.activeIsBody || settled.activeCard !== cardIds.builder
        || settled.builderState !== "empty" || settled.builderBusy !== "false"
        || !settled.builderCurrent || !settled.builderForm) {
      fail(`retained Builder retry did not fall back to settled Preview focus: `
        + JSON.stringify(settled));
    }
  } finally {
    if (!finished) await hold.cleanup();
  }

  await page.click(`${builderCard} [data-ib-cancel]`);
  await page.waitForFunction((calendarId) => {
    const root = document.querySelector(`[data-operational-card="${calendarId}"]`);
    return root && !!root.querySelector(".cal-date")
      && cardIdentityCurrent(readCardState(calendarId).identity);
  }, cardIds.calendar, { timeout: 10000 });
  await page.evaluate(() => { delete window.__loadingFocusRink; });
}

// A successful Create has two distinct async loading paints: Builder becomes
// PENDING while the real write is in flight, then Calendar becomes LOADING
// while its real overview refresh is in flight. Keyboard activation must own
// each semantic loading heading and the established settled action. A later
// move to the persistent context selector supersedes that intent; a synthetic
// activation from BODY remains unfocused; and card-local focus transfers to a
// semantic destination when the Builder itself departs.
async function assertCreateLoadingFocus(
    page, fail, cardIds, focusMode, focusModeCounts) {
  if (!CREATE_FOCUS_MODES.includes(focusMode)) {
    fail(`unknown Create focus mode: ${JSON.stringify(focusMode)}`);
  }
  focusModeCounts.set(focusMode, (focusModeCounts.get(focusMode) || 0) + 1);
  const contextOwned = focusMode === "context";
  const backOwned = focusMode === "back";
  const bodyOwned = focusMode === "body";
  const programmatic = backOwned || bodyOwned;
  const label = focusMode === "keyboard" ? "keyboard Create focus"
    : contextOwned ? "keyboard Create with later context-selector ownership"
    : backOwned ? "programmatic Create with Back ownership"
    : "orphaned programmatic Create no-steal";
  const builderCard = `[data-operational-card="${cardIds.builder}"]`;
  const commitHold = await holdNextIceCommit(page, label);
  const overviewHold = await holdNextOverview(page, label);
  let commitFinished = false;
  let overviewFinished = false;
  try {
    const commit = page.locator(`${builderCard} [data-ib-commit]`);
    const source = await commit.evaluate((button) => ({
      disabled: button.disabled,
      count: document.querySelectorAll("[data-ib-commit]").length,
    })).catch(() => null);
    if (!source || source.disabled || source.count !== 1) {
      fail(`${label}: needs exactly one enabled real Create: ${JSON.stringify(source)}`);
    }
    if (backOwned) {
      await page.locator(`${builderCard} [data-ib-cancel]`).focus();
      await page.evaluate((selector) => {
        window.__loadingFocusBack = document.querySelector(selector);
      }, `${builderCard} [data-ib-cancel]`);
    }
    else if (bodyOwned) {
      const orphaned = await page.evaluate(() => {
        const active = document.activeElement;
        if (active && active.blur) active.blur();
        return document.activeElement === document.body
          || document.activeElement === document.documentElement;
      });
      if (!orphaned) fail(`${label}: could not establish BODY before activation`);
    } else await commit.focus();
    await page.evaluate((selector) => {
      window.__loadingFocusSource = document.querySelector(selector);
    }, `${builderCard} [data-ib-commit]`);
    if (programmatic) {
      await page.evaluate((selector) => document.querySelector(selector).click(),
        `${builderCard} [data-ib-commit]`);
    } else {
      await page.keyboard.press("Enter");
    }

    await commitHold.waitCaptured();
    await page.waitForFunction((builderId) => {
      const root = document.querySelector(`[data-operational-card="${builderId}"]`);
      return root && root.dataset.cardState === "loading"
        && root.getAttribute("aria-busy") === "true"
        && !!root.querySelector(".operational-stale-note h2");
    }, cardIds.builder, { timeout: 10000 });
    const pending = await navigationLoadingFocusState(page, cardIds);
    const pendingWrong = pending.oldSourceConnected || !pending.builderPresent
      || pending.calendarPresent || pending.builderState !== "loading"
      || pending.builderBusy !== "true" || !pending.builderCurrent
      || !/loading ice-builder preview/i.test(pending.builderLoadingText || "")
      || (bodyOwned
        ? !pending.activeIsBody
        : backOwned
        ? pending.oldBackConnected || !pending.activeIsBuilderBack
          || !pending.activeConnected || !pending.activeVisible
          || pending.activeCard !== cardIds.builder
        : !pending.activeIsBuilderLoadingHeading || pending.activeTag !== "H2"
          || pending.activeTabIndex !== -1 || !pending.activeConnected
          || !pending.activeVisible || pending.activeCard !== cardIds.builder);
    if (pendingWrong || (!bodyOwned && pending.activeIsBody)) {
      fail(`${label}: Builder PENDING paint misplaced focus: ${JSON.stringify(pending)}`);
    }
    if (contextOwned) {
      await page.locator("#ctx-select").focus();
      const superseding = await navigationLoadingFocusState(page, cardIds);
      if (superseding.activeId !== "ctx-select" || !superseding.activeConnected
          || !superseding.activeVisible || superseding.activeIsBody) {
        fail(`${label}: could not establish the newer context-selector focus: `
          + JSON.stringify(superseding));
      }
    }

    commitHold.release();
    await commitHold.finish();
    commitFinished = true;
    await overviewHold.waitCaptured();
    await page.waitForFunction((calendarId) => {
      const root = document.querySelector(`[data-operational-card="${calendarId}"]`);
      return root && root.dataset.cardState === "loading"
        && root.getAttribute("aria-busy") === "true"
        && !!root.querySelector(".operational-stale-note h2");
    }, cardIds.calendar, { timeout: 10000 });
    const calendarLoading = await navigationLoadingFocusState(page, cardIds);
    const calendarWrong = calendarLoading.builderPresent
      || !calendarLoading.calendarPresent || calendarLoading.calendarState !== "loading"
      || calendarLoading.calendarBusy !== "true" || !calendarLoading.calendarCurrent
      || !/loading arena calendar/i.test(calendarLoading.calendarLoadingText || "")
      || (contextOwned
        ? calendarLoading.activeId !== "ctx-select"
          || !calendarLoading.activeConnected || !calendarLoading.activeVisible
        : bodyOwned
        ? !calendarLoading.activeIsBody
        : !calendarLoading.activeIsCalendarLoadingHeading
          || calendarLoading.activeTag !== "H2"
          || calendarLoading.activeTabIndex !== -1
          || !calendarLoading.activeConnected || !calendarLoading.activeVisible
          || calendarLoading.activeCard !== cardIds.calendar);
    if (calendarWrong || (!bodyOwned && calendarLoading.activeIsBody)) {
      fail(`${label}: Calendar LOADING paint misplaced focus: `
        + JSON.stringify(calendarLoading));
    }

    // Exercise the Calendar half of the render carry once. Keep the original
    // post-Create refresh held while a full same-tuple render detaches its
    // focused loading status; the queued successor must own the final Build
    // ice destination, and the stale predecessor released below must not undo
    // it. The other three modes remain focused on their distinct no-steal and
    // card-local ownership contracts.
    if (focusMode === "keyboard") {
      const renderHold = await holdNextNotifications(page,
        "Calendar loading focus across a same-tuple full render");
      let renderHoldFinished = false;
      let calendarRenderDone = null;
      try {
        const beforeRender = await page.evaluate(() => {
          window.__loadingFocusHeading = document.activeElement;
          window.__sameTupleRenderError = "";
          return renderPass;
        });
        calendarRenderDone = page.evaluate(async () => {
          const before = renderPass;
          try {
            await render();
            return { before, after: renderPass, error: "" };
          } catch (error) {
            window.__sameTupleRenderError = String(error && error.message || error);
            return { before, after: renderPass,
              error: window.__sameTupleRenderError };
          }
        });
        await renderHold.waitCaptured();
        const duringRender = await navigationLoadingFocusState(page, cardIds);
        if (duringRender.oldLoadingHeadingConnected
            || !duringRender.activeIsBody
            || duringRender.renderPass <= beforeRender
            || duringRender.calendarState !== null
            || duringRender.sameTupleRenderError) {
          fail(`Create successor did not expose Calendar's detached loading window: `
            + JSON.stringify({ beforeRender, duringRender }));
        }
        renderHold.release();
        await renderHold.finish();
        renderHoldFinished = true;
        const renderResult = await calendarRenderDone;
        if (renderResult.error || renderResult.after <= renderResult.before) {
          fail(`Calendar same-tuple render did not complete cleanly: `
            + JSON.stringify(renderResult));
        }
        await page.waitForFunction((calendarId) => {
          const root = document.querySelector(
            `[data-operational-card="${calendarId}"]`);
          return root && root.dataset.cardState !== "loading"
            && root.getAttribute("aria-busy") === "false"
            && !!root.querySelector("[data-ice-builder-open]")
            && cardIdentityCurrent(readCardState(calendarId).identity);
        }, cardIds.calendar, { timeout: 10000 });
        const carried = await navigationLoadingFocusState(page, cardIds);
        if (!carried.activeIsCalendarOpen || !carried.activeConnected
            || !carried.activeVisible || carried.activeIsBody
            || carried.activeCard !== cardIds.calendar
            || carried.calendarState === "loading"
            || carried.calendarBusy !== "false" || !carried.calendarCurrent) {
          fail(`Calendar same-tuple successor lost its loading-focus claim: `
            + JSON.stringify(carried));
        }
      } finally {
        if (!renderHoldFinished) await renderHold.cleanup();
      }
    }

    overviewHold.release();
    await overviewHold.finish();
    overviewFinished = true;
    await page.waitForFunction((calendarId) => {
      const root = document.querySelector(`[data-operational-card="${calendarId}"]`);
      return root && root.dataset.cardState !== "loading"
        && root.getAttribute("aria-busy") === "false"
        && !!root.querySelector("[data-ice-builder-open]")
        && cardIdentityCurrent(readCardState(calendarId).identity);
    }, cardIds.calendar, { timeout: 10000 });
    const settled = await navigationLoadingFocusState(page, cardIds);
    const settledWrong = settled.builderPresent || !settled.calendarPresent
      || settled.calendarState === "loading" || settled.calendarBusy !== "false"
      || !settled.calendarCurrent || !settled.calendarOpen
      || (contextOwned
        ? settled.activeId !== "ctx-select"
          || !settled.activeConnected || !settled.activeVisible
        : bodyOwned
        ? !settled.activeIsBody
        : !settled.activeIsCalendarOpen || !settled.activeConnected
          || !settled.activeVisible || settled.activeCard !== cardIds.calendar);
    if (settledWrong || (!bodyOwned && settled.activeIsBody)) {
      fail(`${label}: final Calendar settlement misplaced focus: `
        + JSON.stringify(settled));
    }
  } finally {
    if (!commitFinished) await commitHold.cleanup();
    if (!overviewFinished) await overviewHold.cleanup();
    await page.evaluate(() => {
      delete window.__loadingFocusSource;
      delete window.__loadingFocusBack;
    });
  }
}

// A failed Create stays in Builder, but still replaces both the Create source
// and its PENDING loading heading. Inject one explicit transport failure and
// hold it at delivery so the intermediate and final destinations are both
// observable. The request count keeps this from passing without exercising
// the real commit handler.
async function assertCreateErrorFocus(page, fail, cardIds) {
  const builderCard = `[data-operational-card="${cardIds.builder}"]`;
  const pattern = /\/api\/setup\/ice-availability\/commit(?:\?|$)/;
  let started = false;
  let released = false;
  let requestCount = 0;
  let markCaptured;
  let markDelivered;
  let releaseGate;
  const captured = new Promise((resolve) => { markCaptured = resolve; });
  const delivered = new Promise((resolve) => { markDelivered = resolve; });
  const gate = new Promise((resolve) => { releaseGate = resolve; });
  const release = () => {
    if (released) return;
    released = true;
    releaseGate();
  };
  const handler = async (route) => {
    if (requestCount) return route.fallback();
    requestCount += 1;
    started = true;
    markCaptured(route.request().method());
    try {
      await gate;
      await route.fulfill({
        status: 503,
        contentType: "application/json",
        body: JSON.stringify({ error: {
          code: "service_unavailable",
          message: "Deliberate Create failure for the focus regression.",
        } }),
      });
    } finally {
      markDelivered();
    }
  };
  await page.route(pattern, handler);
  try {
    const commit = page.locator(`${builderCard} [data-ib-commit]`);
    if (await commit.count() !== 1 || await commit.isDisabled()) {
      fail("Create error focus probe needs one enabled Create action");
    }
    await commit.focus();
    await page.evaluate((selector) => {
      window.__loadingFocusSource = document.querySelector(selector);
    }, `${builderCard} [data-ib-commit]`);
    await page.keyboard.press("Enter");
    const method = await deadline(captured,
      "Create error focus probe never reached the commit endpoint", 10000);
    if (method !== "POST" || requestCount !== 1) {
      fail(`Create error focus probe captured the wrong request: `
        + JSON.stringify({ method, requestCount }));
    }
    await page.waitForFunction((builderId) => {
      const root = document.querySelector(`[data-operational-card="${builderId}"]`);
      return root && root.dataset.cardState === "loading"
        && root.getAttribute("aria-busy") === "true"
        && !!root.querySelector(".operational-stale-note h2");
    }, cardIds.builder, { timeout: 10000 });
    const pending = await navigationLoadingFocusState(page, cardIds);
    if (pending.oldSourceConnected || !pending.activeIsBuilderLoadingHeading
        || pending.activeTag !== "H2" || pending.activeTabIndex !== -1
        || !pending.activeConnected || !pending.activeVisible
        || pending.activeIsBody || pending.activeCard !== cardIds.builder
        || !/loading ice-builder preview/i.test(pending.builderLoadingText || "")) {
      fail(`failed Create did not own its held PENDING heading: `
        + JSON.stringify(pending));
    }

    release();
    await deadline(delivered,
      "Create error response was not delivered", 10000);
    await page.waitForFunction((builderId) => {
      const root = document.querySelector(`[data-operational-card="${builderId}"]`);
      return root && root.dataset.cardState === "error"
        && root.getAttribute("aria-busy") === "false"
        && !!root.querySelector(".banner.alert h2");
    }, cardIds.builder, { timeout: 10000 });
    const failed = await navigationLoadingFocusState(page, cardIds);
    if (!failed.activeIsBuilderErrorHeading || failed.activeTag !== "H2"
        || failed.activeTabIndex !== -1 || !failed.activeConnected
        || !failed.activeVisible || failed.activeIsBody
        || failed.activeCard !== cardIds.builder || failed.builderState !== "error"
        || failed.builderBusy !== "false"
        || !/couldn.t load ice-builder preview/i.test(failed.builderErrorText || "")) {
      fail(`failed Create did not settle focus on its alert heading: `
        + JSON.stringify(failed));
    }
  } finally {
    release();
    if (started) {
      await deadline(delivered,
        "Create error handler did not finish during cleanup", 10000);
    }
    await page.unroute(pattern, handler);
    await page.evaluate(() => { delete window.__loadingFocusSource; });
  }
}

// A preview mismatch is a two-response failure recovery: the rejected Create
// is followed immediately by a replacement Preview. Keep the original loading
// heading truthful, then prove a newer Back focus choice is re-found through
// both replacement paints instead of collapsing to BODY or the default Create.
async function assertPreviewMismatchFocus(page, fail, cardIds) {
  const builderCard = `[data-operational-card="${cardIds.builder}"]`;
  const commitHold = await holdNextIceCommit(page, "preview mismatch focus");
  const previewHold = await holdNextIcePreview(page, "preview mismatch focus");
  let commitFinished = false;
  let previewFinished = false;
  try {
    const commit = page.locator(`${builderCard} [data-ib-commit]`);
    if (await commit.count() !== 1 || await commit.isDisabled()) {
      fail("preview mismatch focus probe needs one enabled Create action");
    }
    await commit.focus();
    await page.evaluate((selector) => {
      window.__loadingFocusSource = document.querySelector(selector);
      window.__loadingFocusBack = null;
    }, `${builderCard} [data-ib-commit]`);
    await page.keyboard.press("Enter");
    await commitHold.waitCaptured(400);
    await page.waitForFunction((builderId) => {
      const root = document.querySelector(`[data-operational-card="${builderId}"]`);
      return root && root.dataset.cardState === "loading"
        && root.getAttribute("aria-busy") === "true"
        && !!root.querySelector(".operational-stale-note h2")
        && !!root.querySelector("[data-ib-cancel]");
    }, cardIds.builder, { timeout: 10000 });
    let state = await navigationLoadingFocusState(page, cardIds);
    if (state.oldSourceConnected || !state.activeIsBuilderLoadingHeading
        || state.activeTag !== "H2" || state.activeTabIndex !== -1
        || !state.activeConnected || !state.activeVisible || state.activeIsBody
        || state.activeCard !== cardIds.builder
        || !/loading ice-builder preview/i.test(state.builderLoadingText || "")) {
      fail(`preview mismatch did not hold the Create loading heading: `
        + JSON.stringify(state));
    }

    await page.locator(`${builderCard} [data-ib-cancel]`).focus();
    await page.evaluate((selector) => {
      window.__loadingFocusBack = document.querySelector(selector);
    }, `${builderCard} [data-ib-cancel]`);
    state = await navigationLoadingFocusState(page, cardIds);
    if (!state.activeIsBuilderBack || !state.activeConnected
        || !state.activeVisible || state.activeIsBody) {
      fail(`preview mismatch could not establish newer Back focus: `
        + JSON.stringify(state));
    }

    commitHold.release();
    await commitHold.finish();
    commitFinished = true;
    await previewHold.waitCaptured();
    await page.waitForFunction((builderId) => {
      const root = document.querySelector(`[data-operational-card="${builderId}"]`);
      return root && root.dataset.cardState === "loading"
        && root.getAttribute("aria-busy") === "true"
        && !!root.querySelector("[data-ib-cancel]");
    }, cardIds.builder, { timeout: 10000 });
    state = await navigationLoadingFocusState(page, cardIds);
    if (state.oldBackConnected || !state.activeIsBuilderBack
        || !state.activeConnected || !state.activeVisible || state.activeIsBody
        || state.activeCard !== cardIds.builder || state.builderState !== "loading"
        || state.builderBusy !== "true" || !state.builderCurrent) {
      fail(`replacement Preview did not carry Back focus into its loading paint: `
        + JSON.stringify(state));
    }
    await page.evaluate((selector) => {
      window.__loadingFocusBack = document.querySelector(selector);
    }, `${builderCard} [data-ib-cancel]`);

    previewHold.release();
    await previewHold.finish();
    previewFinished = true;
    await page.waitForFunction((builderId) => {
      const root = document.querySelector(`[data-operational-card="${builderId}"]`);
      return root && root.dataset.cardState === "ready"
        && root.getAttribute("aria-busy") === "false"
        && !!root.querySelector("[data-ib-cancel]")
        && !!root.querySelector("[data-ib-commit]")
        && cardIdentityCurrent(readCardState(builderId).identity);
    }, cardIds.builder, { timeout: 10000 });
    state = await navigationLoadingFocusState(page, cardIds);
    if (state.oldBackConnected || !state.activeIsBuilderBack
        || !state.activeConnected || !state.activeVisible || state.activeIsBody
        || state.activeCard !== cardIds.builder || state.builderState !== "ready"
        || state.builderBusy !== "false" || !state.builderCurrent) {
      fail(`replacement Preview settlement did not preserve Back focus: `
        + JSON.stringify(state));
    }
    await page.waitForFunction(
      () => /changed since preview/i.test(
        (document.querySelector(".toast-msg") || {}).textContent || ""),
      null, { timeout: 10000 });
  } finally {
    if (!commitFinished) await commitHold.cleanup();
    if (!previewFinished) await previewHold.cleanup();
    await page.evaluate(() => {
      delete window.__loadingFocusSource;
      delete window.__loadingFocusBack;
    });
  }
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
  page.on("console", (m) => {
    if (m.type() !== "error") return;
    const text = m.text();
    // Browser network-status noise for a 4xx the app handles gracefully (e.g. a
    // refused stale-preview commit in step H) is not a page bug — the functional
    // assertions below catch real breakage. Keep genuine JS console errors.
    if (/Failed to load resource/i.test(text)) return;
    errors.push(`[console] ${text}`);
  });
  const fail = (msg) => { throw new Error(`[${viewport.label}] ${msg}`); };
  const createFocusModeCounts = new Map();

  try {
    await waitForServer(`${base}/api/health`, READY_TIMEOUT_MS);
    await page.goto(base, { waitUntil: "domcontentloaded" });
    await page.waitForSelector("#content > *", { timeout: 10000 });
    await installContextFixture(page);

    // Two rinks, both offered by the builder; step (B) revokes the second one's
    // venue access before previewing, to exercise the venue-access report.
    const ids = await page.evaluate(async () => {
      const F = window.hsFixture;
      // #409 EXPLICIT SELECTION on the V1 SURFACE. `POST /api/setup/league`
      // mints the PROGRAM (v1 calls it "league") and `POST /api/setup/season`
      // is PROGRAM-AXIS on the body's `league_id` (server.py:3686), behind the
      // same `setup_create_context_error` preflight v2 uses (server.py:1160).
      // Minting the Program is not selecting it, so the Program-only choice is
      // persisted first — earlier than the single context write that used to
      // sit at the BOTTOM of this fixture.
      const league = await F.create("v1 league (the Program)", "/api/setup/league", { name: "AHL" });
      await F.selectProgram("Program-only bootstrap", league.id);
      const season = await F.create("season", "/api/setup/season", { league_id: league.id, name: "Fall 2026" });
      // Both venue-access grants below are SEASON-OWNED and land in THIS
      // Season, so both axes are persisted before them.
      await F.selectProgramSeason("Program+Season", league.id, season.id);
      const venue = await F.create("venue", "/api/setup/venue", { name: "Main Arena", league_id: league.id });
      await F.call("season venue-access grant", `/api/v2/setup/seasons/${season.id}/venue-access`, { venue_id: venue.id });
      const rink = await F.create("rink", "/api/setup/rink", { venue_id: venue.id, name: "Rink A" });
      // A second venue, granted to the ACTIVE `season` so the builder actually
      // OFFERS its rink (#369: get_demo_overview's ov.rinks — what the
      // builder's checkboxes render from — treats the resolved Season as a
      // hard ceiling, admitting only Venues with an active SeasonVenueAccess
      // grant to THAT Season; a venue granted to some other Season, or to
      // none at all, simply has no checkbox to tick). Step (B) then REVOKES
      // this grant out from under the already-rendered form — a League Admin
      // pulling access while the arena operator has the builder open. That is
      // the one way an operator can still submit an un-granted rink now, and
      // it is precisely why the preview re-checks venue access server-side
      // instead of trusting the rink list the UI offered: this fixture proves
      // that check, not the (no longer reachable) "never granted" shape.
      const venue2 = await F.create("venue2", "/api/setup/venue", { name: "Annex", league_id: league.id });
      const access2 = await F.create("access2", `/api/v2/setup/seasons/${season.id}/venue-access`, { venue_id: venue2.id });
      const rink2 = await F.create("rink2", "/api/setup/rink", { venue_id: venue2.id, name: "Annex Ice" });
      // A second, operator-selectable Program+Season makes the accepted-switch
      // focus race below reachable through the real #ctx-select. Restore this
      // journey's original tuple before the reload so every pre-existing ice
      // assertion still starts from the fixture it was written against.
      const alternateLeague = await F.create("focus alternate Program",
        "/api/setup/league", { name: "Focus Alternate", timezone: "UTC" });
      await F.selectProgram("focus alternate Program", alternateLeague.id);
      const alternateSeason = await F.create("focus alternate Season",
        "/api/setup/season", {
          league_id: alternateLeague.id,
          name: "Focus Alternate Season",
          start_date: "2027-09-01",
          end_date: "2028-04-30",
        });
      await F.selectProgramSeason("focus alternate Program+Season",
        alternateLeague.id, alternateSeason.id);
      await F.selectProgramSeason("restore ice-builder fixture",
        league.id, season.id);
      // The legacy v1 "league" IS a v2 Program under the shim (server.py's
      // POST /api/setup/league routes straight to api.create_program(), and
      // /api/setup/season passes its own league_id through as create_season()'s
      // program_id) -- select it as the active #159 context so defaultIceForm()
      // resolves this Season without needing its own now-removed global-first
      // fallback (#331 review round 8: defaultIceForm() fails CLOSED, the same
      // way Import's own Season select already does, when no Season is
      // actively selected -- this fixture must actively select one, not rely
      // on a silent global default that no longer exists).
      return { league: league.id, season: season.id, rink: rink.id,
               rink2: rink2.id, access2: access2.id,
               alternateLeague: alternateLeague.id,
               alternateSeason: alternateSeason.id };
    });
    // The /api/context call above is a bare fetch, bypassing setActiveContext()
    // (the real switcher's own handler) entirely -- it moves the SERVER's
    // active context but leaves the already-loaded page's own client-side
    // contextOptions (fetched once, before this fixture's Program even
    // existed) none the wiser. A reload re-runs the boot sequence's own
    // loadContextOptions() so defaultIceForm() sees the real selection below,
    // the same requirement round 6/7's own bare-fetch fixtures already
    // documented for a freshly-created Program's #ctx-select option.
    await page.reload({ waitUntil: "domcontentloaded" });
    await page.waitForSelector("#content > *", { timeout: 10000 });
    await installContextFixture(page);

    await page.click('.tab[data-tab="calendar"]');
    await page.waitForSelector('[data-mode="month"]', { state: "visible", timeout: 10000 });

    // (A) Month view renders a day grid.
    await page.click('[data-mode="month"]');
    await page.waitForSelector(".mo-grid .mo-cell", { timeout: 10000 });
    const cellCount = await page.$$eval(".mo-grid .mo-cell", (els) => els.length);
    if (cellCount !== 42) fail(`month grid should have 42 day cells, got ${cellCount}`);
    const cardIds = await assertCalendarNavigationFocus(page, fail, {
      primaryProgram: ids.league,
      primarySeason: ids.season,
      alternateProgram: ids.alternateLeague,
      alternateSeason: ids.alternateSeason,
    });
    await assertCalendarBuilderLoadingFocus(page, fail, cardIds);
    await assertBuilderRetryFocusFallback(page, fail, cardIds);

    // (B) Open the builder and select BOTH rinks, then revoke the second
    // venue's Season access BEFORE previewing (see the fixture comment): the
    // preview re-checks access server-side, reports that rink as skipped and
    // generates no ice for it, while the still-granted rink yields its full
    // block and Create stays enabled.
    await page.click("[data-ice-builder-open]");
    await page.waitForSelector(".ib-form", { timeout: 10000 });
    await page.check(`.ib-rink[value="${ids.rink}"]`);
    await page.check(`.ib-rink[value="${ids.rink2}"]`);
    await page.evaluate(async (accessId) => {
      await fetch(`/api/v2/setup/season-venue-access/${accessId}/remove`, {
        method: "POST", credentials: "same-origin",
        headers: { "Content-Type": "application/json" }, body: "{}" });
    }, ids.access2);
    await preview(page);
    const ordinaryPreviewFocus = await navigationLoadingFocusState(page, cardIds);
    if (!ordinaryPreviewFocus.activeIsBuilderCommit
        || ordinaryPreviewFocus.activeTag !== "BUTTON"
        || !ordinaryPreviewFocus.activeConnected
        || !ordinaryPreviewFocus.activeVisible
        || ordinaryPreviewFocus.activeIsBody
        || ordinaryPreviewFocus.activeCard !== cardIds.builder) {
      fail(`ordinary Preview did not advance focus to Create: `
        + JSON.stringify(ordinaryPreviewFocus));
    }
    let s = await previewState(page);
    if (s.new !== EXPECTED_NEW) fail(`expected ${EXPECTED_NEW} new slots, got ${JSON.stringify(s)}`);
    if (s.accessMissing !== 1) fail(`expected 1 access-missing rink, got ${JSON.stringify(s)}`);
    if (s.commitDisabled !== false) fail(`commit should be enabled with new slots: ${JSON.stringify(s)}`);
    const warn = await page.$(".ib-warn");
    if (!warn) fail("venue-access warning should be shown for the un-granted rink");

    // That same revocation also dropped the un-granted rink from ov.rinks, so
    // the post-preview re-render removed its checkbox — the form Create would
    // now read no longer matches the fingerprint-bound template that was
    // reviewed above (the server would refuse it as a stale preview, exactly
    // as step H asserts). Re-preview the CURRENT, granted-rink-only proposal:
    // same slot count, nothing left to report as access-missing, and that is
    // the preview (C) commits.
    await preview(page);
    s = await previewState(page);
    if (s.new !== EXPECTED_NEW || s.accessMissing !== 0) {
      fail(`the re-preview should drop the revoked rink and keep ${EXPECTED_NEW} new slots, got ${JSON.stringify(s)}`);
    }

    // (C) Commit creates exactly the accessible rink's slots.
    await assertCreateLoadingFocus(
      page, fail, cardIds, "keyboard", createFocusModeCounts);
    const created = await page.evaluate(async (rink) => {
      const ov = await (await fetch("/api/demo/overview", { credentials: "same-origin" })).json();
      return (ov.ice_slots || []).filter((x) => x.rink_id === rink).length;
    }, ids.rink);
    if (created !== EXPECTED_NEW) fail(`expected ${EXPECTED_NEW} committed slots, got ${created}`);

    // (D) Idempotent rerun: zero new, all duplicates, commit disabled.
    await page.click("[data-ice-builder-open]");
    await page.waitForSelector(".ib-form", { timeout: 10000 });
    await page.check(`.ib-rink[value="${ids.rink}"]`);
    await preview(page);
    s = await previewState(page);
    if (s.new !== 0 || s.duplicate !== EXPECTED_NEW) {
      fail(`rerun should be idempotent (0 new / ${EXPECTED_NEW} dup), got ${JSON.stringify(s)}`);
    }
    if (s.commitDisabled !== true) fail(`commit should be disabled on an all-duplicate preview: ${JSON.stringify(s)}`);

    // (E) Exclusion date is honored and reported. Use a FRESH accessible rink so
    // the excluded run isn't all duplicates from (C), then exclude one Tuesday.
    const rink3 = await page.evaluate(async (season) => {
      const F = window.hsFixture;
      const venue = await F.create("venue", "/api/setup/venue", { name: "West", league_id: null });
      await F.call("season venue-access grant", `/api/v2/setup/seasons/${season}/venue-access`, { venue_id: venue.id });
      return (await F.create("rink", "/api/setup/rink", { venue_id: venue.id, name: "West Ice" })).id;
    }, ids.season);
    // Adding the exclusion re-renders the builder, which refetches the overview
    // and surfaces the new rink's checkbox.
    await page.fill("#ib-excl", "2026-09-08");
    await page.click("[data-ib-excl-add]");
    await page.waitForSelector(`.ib-rink[value="${rink3}"]`, { timeout: 10000 });
    await page.uncheck(`.ib-rink[value="${ids.rink}"]`);
    await page.check(`.ib-rink[value="${rink3}"]`);
    await preview(page);
    s = await previewState(page);
    if (s.new !== EXPECTED_NEW - 3) fail(`exclusion should drop 3 slots (=> ${EXPECTED_NEW - 3}), got ${JSON.stringify(s)}`);
    if (s.skipped < 1) fail(`the excluded date should be reported as skipped: ${JSON.stringify(s)}`);

    // (F) Per-weekday windows: each selected day carries its OWN local start/end
    // time. Open a fresh builder on a clean rink, keep Tuesday 18:00-22:00 (3
    // games) but narrow Thursday to 18:00-20:00 (1 game). September 2026 has 5
    // Tuesdays + 4 Thursdays, so per-weekday windows yield 5*3 + 4*1 = 19 —
    // where a single uniform block would be 27. Proves the per-day times reach
    // the planner and are not collapsed into one global window.
    const rink4 = await page.evaluate(async (season) => {
      const F = window.hsFixture;
      const venue = await F.create("venue", "/api/setup/venue", { name: "East", league_id: null });
      await F.call("season venue-access grant", `/api/v2/setup/seasons/${season}/venue-access`, { venue_id: venue.id });
      return (await F.create("rink", "/api/setup/rink", { venue_id: venue.id, name: "East Ice" })).id;
    }, ids.season);
    await page.click("[data-ib-cancel]");
    await page.waitForSelector("[data-ice-builder-open]", { timeout: 10000 });
    await page.click("[data-ice-builder-open]");
    await page.waitForSelector(".ib-form", { timeout: 10000 });
    await page.check(`.ib-rink[value="${rink4}"]`);
    // Tue (weekday 1) and Thu (weekday 3) are checked by default; each renders
    // its own start/end inputs. Narrow only Thursday's window.
    await page.fill("#ib-end-3", "20:00");
    await preview(page);
    s = await previewState(page);
    if (s.new !== 19) fail(`per-weekday windows should yield 19 new (5*3 Tue + 4*1 Thu), got ${JSON.stringify(s)}`);

    // (G) Commit is bound to the preview: editing the template AFTER Preview
    // drops the preview and its Create button, so a form edited post-preview can
    // never be committed (the server also rejects a mismatched fingerprint). All
    // template fields share one invalidation listener; editing a weekday time
    // exercises it. Use a fresh rink so "zero committed" is unambiguous.
    const rink5 = await page.evaluate(async (season) => {
      const F = window.hsFixture;
      const venue = await F.create("venue", "/api/setup/venue", { name: "North", league_id: null });
      await F.call("season venue-access grant", `/api/v2/setup/seasons/${season}/venue-access`, { venue_id: venue.id });
      return (await F.create("rink", "/api/setup/rink", { venue_id: venue.id, name: "North Ice" })).id;
    }, ids.season);
    await page.click("[data-ib-cancel]");
    await page.waitForSelector("[data-ice-builder-open]", { timeout: 10000 });
    await page.click("[data-ice-builder-open]");
    await page.waitForSelector(".ib-form", { timeout: 10000 });
    await page.check(`.ib-rink[value="${rink5}"]`);
    await preview(page);
    s = await previewState(page);
    if (s.new < 1) fail(`bind test needs a preview with slots, got ${JSON.stringify(s)}`);
    if (s.commitDisabled !== false) fail(`Create should be enabled on a fresh preview: ${JSON.stringify(s)}`);
    // Edit the Thursday end time AFTER previewing -> the preview (and Create) go.
    await page.fill("#ib-end-3", "20:30");
    await page.waitForSelector(".ib-preview", { state: "detached", timeout: 10000 });
    if (await page.$("[data-ib-commit]")) fail("Create must be gone after editing the template post-preview");
    const boundCommitted = await page.evaluate(async (rink) => {
      const ov = await (await fetch("/api/demo/overview", { credentials: "same-origin" })).json();
      return (ov.ice_slots || []).filter((x) => x.rink_id === rink).length;
    }, rink5);
    if (boundCommitted !== 0) fail(`an invalidated preview must commit nothing, got ${boundCommitted}`);
    // Re-preview the still-fresh rink, then make a generic Create transport
    // failure observable at both its PENDING and ERROR focus destinations.
    await previewCurrent(page);
    s = await previewState(page);
    if (!(s.new >= 1) || s.commitDisabled !== false) {
      fail(`Create error focus probe needs an enabled preview: ${JSON.stringify(s)}`);
    }
    await assertCreateErrorFocus(page, fail, cardIds);

    // (H) A STALE preview is refused and refreshed. Simulate the resolved
    // snapshot moving under the operator (a concurrent Season/timezone edit
    // invalidates the stored fingerprint) by staling it; the server rejects the
    // commit and the UI re-previews the current proposal instead of writing the
    // stale set. (The service/HTTP suites drive the real Season/tz change.)
    const rink6 = await page.evaluate(async (season) => {
      const F = window.hsFixture;
      const venue = await F.create("venue", "/api/setup/venue", { name: "South", league_id: null });
      await F.call("season venue-access grant", `/api/v2/setup/seasons/${season}/venue-access`, { venue_id: venue.id });
      return (await F.create("rink", "/api/setup/rink", { venue_id: venue.id, name: "South Ice" })).id;
    }, ids.season);
    await page.click("[data-ib-cancel]");
    await page.waitForSelector("[data-ice-builder-open]", { timeout: 10000 });
    await page.click("[data-ice-builder-open]");
    await page.waitForSelector(".ib-form", { timeout: 10000 });
    await page.check(`.ib-rink[value="${rink6}"]`);
    await preview(page);
    s = await previewState(page);
    if (s.commitDisabled !== false) fail(`refresh test needs an enabled Create: ${JSON.stringify(s)}`);
    await page.evaluate(() => {
      cardDisplayPayload(readCardState(ICE_BUILDER_CARD))
        .preview.template_fingerprint = "staledeadbeef00";
    });
    await assertPreviewMismatchFocus(page, fail, cardIds);
    // Crucially, the stale commit wrote nothing, and the builder stayed open
    // (a successful commit would have closed it back to the calendar).
    await page.waitForSelector(".ib-form", { timeout: 10000 });
    const staleCommitted = await page.evaluate(async (rink) => {
      const ov = await (await fetch("/api/demo/overview", { credentials: "same-origin" })).json();
      return (ov.ice_slots || []).filter((x) => x.rink_id === rink).length;
    }, rink6);
    if (staleCommitted !== 0) fail(`a refused stale commit must write nothing, got ${staleCommitted}`);

    // (H2) A same-slot-set template edit that slips past the frontend's
    // invalidation listener is still caught by the SERVER token (#158 review):
    // the token binds the whole reviewed payload, not just the generated tuples.
    // Preview, then extend the window END by 5 min (22:00 -> 22:05) by setting the
    // inputs' value WITHOUT firing `change` — so the stored preview/fingerprint
    // survive as if the edit slipped the suspenders. The same three slots/day
    // still fit, so a tuple-only token would have committed the unreviewed window;
    // the full-payload binding moves the fingerprint, the server refuses the
    // stale commit, and the UI re-previews the CURRENT (22:05) proposal — same
    // slot count, new token — which then commits.
    const rink6b = await page.evaluate(async (season) => {
      const F = window.hsFixture;
      const venue = await F.create("venue", "/api/setup/venue", { name: "West", league_id: null });
      await F.call("season venue-access grant", `/api/v2/setup/seasons/${season}/venue-access`, { venue_id: venue.id });
      return (await F.create("rink", "/api/setup/rink", { venue_id: venue.id, name: "West Ice" })).id;
    }, ids.season);
    await page.click("[data-ib-cancel]");
    await page.waitForSelector("[data-ice-builder-open]", { timeout: 10000 });
    await page.click("[data-ice-builder-open]");
    await page.waitForSelector(".ib-form", { timeout: 10000 });
    await page.check(`.ib-rink[value="${rink6b}"]`);
    await preview(page);
    const before = await previewState(page);
    if (!(before.new >= 1) || before.commitDisabled !== false) {
      fail(`same-slot edit test needs an enabled preview with slots: ${JSON.stringify(before)}`);
    }
    // Extend BOTH selected days' end time WITHOUT a change event (the listener
    // that would drop the preview never fires), so the real fingerprint is stale.
    await page.evaluate(() => {
      for (const el of document.querySelectorAll(".ib-wd-end")) el.value = "22:05";
    });
    await page.click("[data-ib-commit]");
    // Refused (same slots, but the reviewed window moved) -> refresh toast.
    await page.waitForFunction(
      () => /changed since preview/i.test((document.querySelector(".toast-msg") || {}).textContent || ""),
      null, { timeout: 10000 });
    const refreshed = await previewState(page);
    if (refreshed.new !== before.new) {
      fail(`the refreshed preview should show the SAME slot count (same tuples): ${before.new} -> ${refreshed.new}`);
    }
    if (refreshed.commitDisabled !== false) fail("Create should be enabled after the refresh");
    // The refreshed token (for the 22:05 window) now commits exactly those slots.
    await page.click("[data-ib-commit]");
    await page.waitForSelector("[data-ice-builder-open]", { timeout: 10000 });
    const editCommitted = await page.evaluate(async (rink) => {
      const ov = await (await fetch("/api/demo/overview", { credentials: "same-origin" })).json();
      return (ov.ice_slots || []).filter((x) => x.rink_id === rink).length;
    }, rink6b);
    if (editCommitted !== before.new) {
      fail(`the re-previewed edit should commit its ${before.new} slots, got ${editCommitted}`);
    }
    // The successful commit closed the builder; reopen it so the next step starts
    // from the shared "builder open" invariant (each step cancels the open builder
    // then reopens with a fresh rink).
    await page.click("[data-ice-builder-open]");
    await page.waitForSelector(".ib-form", { timeout: 10000 });

    // (I) An exact-tuple collision with EXISTING incompatible ice is REPORTED as
    // a conflict, never hidden as duplicate capacity (#158 review). A
    // maintenance slot at the builder's first window (Sep 1 18:00-19:00, program
    // tz UTC) collides exactly with a generated tuple; the display path is the
    // same the ALLOCATED-active-Game case (covered by the service + HTTP suites)
    // takes. The collided window must show as a conflict and drop out of the new
    // count, not be silently counted as idempotent capacity.
    const rink7 = await page.evaluate(async (season) => {
      const F = window.hsFixture;
      const venue = await F.create("venue", "/api/setup/venue", { name: "West End", league_id: null });
      await F.call("season venue-access grant", `/api/v2/setup/seasons/${season}/venue-access`, { venue_id: venue.id });
      const rink = (await F.create("rink", "/api/setup/rink", { venue_id: venue.id, name: "West End Ice" })).id;
      await F.call("ice-slot", "/api/setup/ice-slot", {
        rink_id: rink, start_time: "2026-09-01T18:00:00+00:00",
        end_time: "2026-09-01T19:00:00+00:00", slot_type: "maintenance" });
      return rink;
    }, ids.season);
    await page.click("[data-ib-cancel]");
    await page.waitForSelector("[data-ice-builder-open]", { timeout: 10000 });
    await page.click("[data-ice-builder-open]");
    await page.waitForSelector(".ib-form", { timeout: 10000 });
    await page.check(`.ib-rink[value="${rink7}"]`);
    await preview(page);
    s = await previewState(page);
    if (s.conflict < 1) fail(`the exact maintenance collision must be a conflict: ${JSON.stringify(s)}`);
    if (s.new !== EXPECTED_NEW - 1) fail(`the collided window must NOT be counted as capacity (expected ${EXPECTED_NEW - 1} new): ${JSON.stringify(s)}`);
    if (!(await page.$(".ib-warn"))) fail("a conflict warning should be visible for the collision");
    // The maintenance collision's row is now listed with its exact target (not a
    // bare count): every conflict is individually reviewable before commit.
    const maintConflict = await page.$eval(
      ".ib-slot-conflict", (el) => el.textContent).catch(() => null);
    if (!maintConflict || !/maintenance/i.test(maintConflict)) {
      fail(`the maintenance conflict row must show its exact target, got ${JSON.stringify(maintConflict)}`);
    }

    // (J) A season-long template (>60 generated days) with an existing-Game
    // conflict LATE in the range must expose EVERY row — the final generated day
    // and the exact Game collision (its target Game id) — not just the first 60
    // days or a bare count (#158 review). The "AHL" program tz is UTC, so the
    // seeded Game's slot tuple and a generated window coincide exactly.
    const long = await page.evaluate(async (ctx) => {
      const F = window.hsFixture;
      const venue = await F.create("venue", "/api/setup/venue", { name: "Long Range", league_id: null });
      await F.call("season venue-access grant", `/api/v2/setup/seasons/${ctx.season}/venue-access`, { venue_id: venue.id });
      const rink = (await F.create("rink", "/api/setup/rink", { venue_id: venue.id, name: "Long Ice" })).id;
      // Seed a Game on a slot LATE in the range (Nov 1 = day 62 of a Sep 1
      // start), so its conflict row falls BEYOND the old 60-day cap. An
      // exhibition game needs only active season participation (no grouping
      // league), keeping the setup minimal.
      // #409: the Division, the two registrations and the Game below are
      // SEASON-OWNED in this Season, and this journey has switched Program
      // more than once by now — state the tuple instead of assuming the
      // bootstrap's selection still stands.
      await F.selectProgramSeason("Program+Season for the long-range fixture",
        ctx.league, ctx.season);
      const club = await F.create("club", "/api/setup/club", { name: "Range Club" });
      const division = await F.create("division", "/api/setup/division", { season_id: ctx.season, name: "Range Div" });
      const mk = async (name) => (await F.create(`team ${name}`, "/api/setup/team",
        { club_id: club.id, division_id: division.id, name, league_id: ctx.league })).id;
      const home = await mk("Range Home");
      const away = await mk("Range Away");
      await F.call("team registration", `/api/setup/seasons/${ctx.season}/team-registrations`, { team_id: home, division_id: division.id });
      await F.call("team registration", `/api/setup/seasons/${ctx.season}/team-registrations`, { team_id: away, division_id: division.id });
      const slot = await F.create("slot", "/api/setup/ice-slot", {
        rink_id: rink, start_time: "2026-11-01T18:00:00+00:00",
        end_time: "2026-11-01T19:00:00+00:00", slot_type: "game" });
      const g = await F.create("exhibition game", "/api/setup/game", {
        season_id: ctx.season, division_id: division.id, home_team_id: home,
        away_team_id: away, ice_slot_id: slot.id, game_type: "exhibition" });
      return { rink, game: (g && (g.id || (g.game && g.game.id))) || null };
    }, { season: ids.season, league: ids.league });
    if (!long.game) fail("failed to seed the conflicting Game for the long-range preview");

    await page.click("[data-ib-cancel]");
    await page.waitForSelector("[data-ice-builder-open]", { timeout: 10000 });
    await page.click("[data-ice-builder-open]");
    await page.waitForSelector(".ib-form", { timeout: 10000 });
    // Select EVERY weekday. The weekday inputs are visually-hidden custom toggles
    // (their label is the click target), so set them in the DOM and fire ONE
    // change — the builder's listener reads all boxes, updates state and
    // re-renders with each day's window row. Do this BEFORE the rink so the rink
    // check survives that re-render.
    await page.evaluate(() => {
      const boxes = Array.from(document.querySelectorAll(".ib-weekday"));
      boxes.forEach((cb) => { cb.checked = true; });
      if (boxes[0]) boxes[0].dispatchEvent(new Event("change", { bubbles: true }));
    });
    await page.waitForSelector(".ib-form", { timeout: 10000 });
    await page.check(`.ib-rink[value="${long.rink}"]`);
    await page.fill("#ib-from", "2026-09-01");
    await page.fill("#ib-to", "2026-11-05");        // 66 distinct days, every weekday
    await page.evaluate(() => { const p = document.querySelector(".ib-preview"); if (p) p.remove(); });
    await page.click("[data-ib-preview]");
    await page.waitForSelector(".ib-preview", { timeout: 15000 });
    const lp = await page.evaluate(() => {
      const p = document.querySelector(".ib-preview");
      const last = p.querySelector("[data-ib-last-day]");
      const conflict = p.querySelector("[data-ib-conflict-game]");
      return {
        days: +p.getAttribute("data-ib-days"),
        newCount: +p.getAttribute("data-ib-new"),
        conflict: +p.getAttribute("data-ib-conflict"),
        lastDayDate: p.getAttribute("data-ib-last-day-date"),
        lastDayRowDate: last ? last.getAttribute("data-ib-day") : null,
        conflictGame: conflict ? conflict.getAttribute("data-ib-conflict-game") : null,
        conflictDay: conflict ? conflict.closest(".ib-day-row").getAttribute("data-ib-day") : null,
      };
    });
    if (lp.days <= 60) fail(`the long-range preview must generate >60 days, got ${JSON.stringify(lp)}`);
    if (lp.lastDayDate !== "2026-11-05" || lp.lastDayRowDate !== "2026-11-05") {
      fail(`the final generated day (2026-11-05) must be reviewable, got ${JSON.stringify(lp)}`);
    }
    if (lp.conflictGame !== long.game) {
      fail(`the exact Game conflict target must be visible, got ${JSON.stringify(lp)} (game ${long.game})`);
    }
    if (lp.conflictDay !== "2026-11-01") {
      fail(`the conflict on a day beyond the old 60-cap must be reviewable, got ${JSON.stringify(lp)}`);
    }
    // Commit stays bound to the COMPLETE preview: it creates exactly the new rows
    // across the WHOLE range (the late Game collision skipped), not a truncated
    // 60-day subset.
    await assertCreateLoadingFocus(
      page, fail, cardIds, "context", createFocusModeCounts);
    const longCreated = await page.evaluate(async (rink) => {
      const ov = await (await fetch("/api/demo/overview", { credentials: "same-origin" })).json();
      return (ov.ice_slots || []).filter((x) => x.rink_id === rink && !x.game_id).length;
    }, long.rink);
    if (longCreated !== lp.newCount) {
      fail(`commit must create the FULL previewed set (${lp.newCount}), got ${longCreated}`);
    }

    // (K) DST safety is visibly reviewable, not just correct under the hood
    // (#315 review). A fresh Program in a DST-observing timezone (America/
    // Toronto), a season spanning both a spring-forward (2027-03-14) and a
    // fall-back (2026-11-01) Sunday, one accessible rink, weekday narrowed to
    // Sunday only, turnover zeroed so the real-hour math above (2 slots / 120
    // reserved for spring, 4 slots / 240 reserved for fall) applies exactly.
    const dstIds = await page.evaluate(async () => {
      const F = window.hsFixture;
      const league = await F.create("v1 DST league (the Program)", "/api/setup/league", { name: "DST League", timezone: "America/Toronto" });
      // #409: this block builds a NEW Program, and the Season create is
      // PROGRAM-AXIS against it — so the switch has to happen HERE, before
      // the Season, not below it where the single raw context write used to
      // sit. The guard is not weakened for the fixture's convenience.
      await F.selectProgram("DST Program-only bootstrap", league.id);
      const season = await F.create("DST season", "/api/setup/season", {
        league_id: league.id, name: "DST Season",
        start_date: "2026-08-01", end_date: "2027-06-01" });
      const venue = await F.create("venue", "/api/setup/venue", { name: "DST Arena", league_id: league.id });
      // This block builds a NEW Program, so the grant below targets a Season
      // outside the currently active one. Setup mutations bind to the ACTIVE
      // Program (#369 prerequisite), so move into the Program being built --
      // the same explicit switch this journey already does at its first
      // fixture. The guard is not weakened for the fixture's convenience.
      // The grant is SEASON-OWNED and lands in the DST Season.
      await F.selectProgramSeason("DST Program+Season", league.id, season.id);
      await F.call("DST season venue-access grant",
        `/api/v2/setup/seasons/${season.id}/venue-access`, { venue_id: venue.id });
      const rink = await F.create("rink", "/api/setup/rink", { venue_id: venue.id, name: "DST Sheet" });
      // #369: get_demo_overview's `seasons` (what #ib-season's <option>s
      // render from) is ceilinged on the resolved ACTIVE Season — it lists
      // exactly that one Season — unlike before #367, where it was globally
      // unfiltered and a brand-new, never-activated Program's own Season
      // still showed up regardless of the active context. Actively switch to
      // this fresh DST League/Season so it (and its venue-access grants)
      // resolve in the builder below, mirroring the identical requirement the
      // fixture setup above already documents.
      await F.call("context", "/api/context", { program_id: league.id, season_id: season.id });
      // Clear the #ctx= deep-link hash the earlier AHL switch's reload/render
      // synced into the URL: restoreContextDeepLink() on the NEXT boot below
      // treats a hash that disagrees with the just-persisted server selection
      // as an intentional deep link and silently POSTS it right back,
      // reverting this switch to AHL before the builder ever sees it.
      history.replaceState(null, "", location.pathname + location.search);
      return { season: season.id, rink: rink.id };
    });
    // The bare /api/context fetch above moves the SERVER's active context but
    // leaves the already-loaded page's own client-side contextOptions (and
    // ov.rinks/ov.seasons) stale -- reload to re-run the boot sequence's own
    // loadContextOptions(), then re-navigate back to the calendar tab (mirrors
    // the identical reload the fixture setup above already performs).
    await page.reload({ waitUntil: "domcontentloaded" });
    await page.waitForSelector("#content > *", { timeout: 10000 });
    await installContextFixture(page);
    await page.click('.tab[data-tab="calendar"]');
    await page.waitForSelector('[data-mode="month"]', { state: "visible", timeout: 10000 });
    await page.click("[data-ice-builder-open]");
    await page.waitForSelector(".ib-form", { timeout: 10000 });
    await page.selectOption("#ib-season", dstIds.season);
    await page.waitForSelector(`.ib-rink[value="${dstIds.rink}"]`, { timeout: 10000 });
    await page.check(`.ib-rink[value="${dstIds.rink}"]`);
    await selectWeekdaysOnly(page, [6]);      // Sunday only
    await page.fill("#ib-turnover", "0");

    // (K1) A window whose START falls in the nonexistent spring-forward hour
    // (2027-03-14, 02:00-03:00 does not exist) generates NOTHING for that day —
    // and the preview must SAY SO, not just show an empty list.
    await page.fill("#ib-from", "2027-03-14");
    await page.fill("#ib-to", "2027-03-14");
    await page.fill("#ib-start-6", "02:30");
    await page.fill("#ib-end-6", "05:00");
    await previewCurrent(page);
    s = await previewState(page);
    if (s.new !== 0 || s.dstSkipped !== 1) {
      fail(`a spring-forward gap-start window must generate 0 slots and report 1 dst-skipped day, got ${JSON.stringify(s)}`);
    }
    if (s.commitDisabled !== true) fail(`commit should be disabled with zero new slots: ${JSON.stringify(s)}`);
    const skipNotice = await page.$eval(".ib-preview", (el) => el.textContent);
    if (!/2027-03-14/.test(skipNotice) || !/gap|nonexistent|doesn.?t exist/i.test(skipNotice)) {
      fail(`the spring-forward skip must be visibly reported with the day and an actionable reason: ${JSON.stringify(skipNotice.slice(0, 300))}`);
    }

    // (K2) The SAME day's window, widened to span the gap (01:00-04:00), is 2
    // REAL hours (not the wall-clock 3) => exactly 2 positive-duration slots,
    // no dst-skip, and it commits + persists exactly what was previewed.
    await page.fill("#ib-start-6", "01:00");
    await page.fill("#ib-end-6", "04:00");
    await previewCurrent(page);
    s = await previewState(page);
    if (s.new !== 2 || s.dstSkipped !== 0) {
      fail(`a spring-forward gap-spanning window should generate exactly 2 real-duration slots and report no skip, got ${JSON.stringify(s)}`);
    }
    const springSlotCount = await page.$$eval(".ib-slot", (els) => els.length);
    if (springSlotCount !== 2) fail(`expected 2 visible slot rows for the spring-forward window, got ${springSlotCount}`);
    await assertCreateLoadingFocus(
      page, fail, cardIds, "body", createFocusModeCounts);
    const springCreated = await page.evaluate(async (rink) => {
      const ov = await (await fetch("/api/demo/overview", { credentials: "same-origin" })).json();
      return (ov.ice_slots || []).filter((x) => x.rink_id === rink).length;
    }, dstIds.rink);
    if (springCreated !== 2) fail(`spring-forward commit should create exactly the 2 previewed slots, got ${springCreated}`);

    // (K3) A fall-back day (2026-11-01, the repeated 01:00-02:00 hour) on a
    // FRESH rink: 00:00-03:00 local is 4 REAL hours => 4 slots. The repeated
    // hour means two distinct UTC slots read the SAME local clock time — the
    // preview must visibly tell them apart (not just be correct server-side).
    const dstRink2 = await page.evaluate(async (season) => {
      const F = window.hsFixture;
      const venue = await F.create("venue", "/api/setup/venue", { name: "DST Arena 2", league_id: null });
      await F.call("season venue-access grant", `/api/v2/setup/seasons/${season}/venue-access`, { venue_id: venue.id });
      return (await F.create("rink", "/api/setup/rink", { venue_id: venue.id, name: "DST Sheet 2" })).id;
    }, dstIds.season);
    await page.click("[data-ice-builder-open]");
    await page.waitForSelector(".ib-form", { timeout: 10000 });
    // A fresh open resets the form to its defaults (Tue/Thu, 15-min turnover,
    // and the ACTIVE Season — the only option #ib-season now offers under the
    // #369 Season ceiling) — reselect it explicitly anyway, so this step still
    // states which Season it operates in rather than inheriting it silently.
    await page.selectOption("#ib-season", dstIds.season);
    await page.waitForSelector(`.ib-rink[value="${dstRink2}"]`, { timeout: 10000 });
    await page.check(`.ib-rink[value="${dstRink2}"]`);
    await selectWeekdaysOnly(page, [6]);      // Sunday only
    await page.fill("#ib-turnover", "0");
    await page.fill("#ib-from", "2026-11-01");
    await page.fill("#ib-to", "2026-11-01");
    await page.fill("#ib-start-6", "00:00");
    await page.fill("#ib-end-6", "03:00");
    await previewCurrent(page);
    s = await previewState(page);
    if (s.new !== 4) fail(`a fall-back 00:00-03:00 window should generate 4 real-hour slots, got ${JSON.stringify(s)}`);
    const fallSlotCount = await page.$$eval(".ib-slot", (els) => els.length);
    if (fallSlotCount !== 4) fail(`expected 4 visible slot rows for the fall-back window, got ${fallSlotCount}`);
    // Two rows share the "01:00" start clock (the repeated hour) — their
    // data-ib-start-offset must differ, proving the UI distinguishes the two
    // real, different UTC instants rather than showing identical labels.
    const startClocks = await page.$$eval(".ib-slot",
      (els) => els.map((e) => ({ clock: e.getAttribute("data-ib-start-clock"),
                                 offset: e.getAttribute("data-ib-start-offset") })));
    const repeated = startClocks.filter((c) => c.clock === "01:00");
    if (repeated.length !== 2) {
      fail(`expected exactly 2 rows starting at the repeated 01:00 local hour, got ${JSON.stringify(startClocks)}`);
    }
    if (repeated[0].offset === repeated[1].offset) {
      fail(`the two repeated-hour rows must carry DIFFERENT UTC offsets, got ${JSON.stringify(repeated)}`);
    }
    // The visible text itself must differ too (the offset is rendered, not just
    // stashed in a data attribute) for at least one of the colliding pairs.
    const fallSlotTexts = await page.$$eval(".ib-slot", (els) => els.map((e) => e.textContent));
    if (new Set(fallSlotTexts).size !== fallSlotTexts.length) {
      fail(`every fall-back row must render distinct text, got ${JSON.stringify(fallSlotTexts)}`);
    }
    await assertCreateLoadingFocus(
      page, fail, cardIds, "back", createFocusModeCounts);
    const fallCreated = await page.evaluate(async (rink) => {
      const ov = await (await fetch("/api/demo/overview", { credentials: "same-origin" })).json();
      return (ov.ice_slots || []).filter((x) => x.rink_id === rink).length;
    }, dstRink2);
    if (fallCreated !== 4) fail(`fall-back commit should create exactly the 4 previewed slots, got ${fallCreated}`);

    // (K4) A SINGLE row that itself crosses the spring-forward gap (#313
    // follow-up review): window 01:00-03:00 is a real 60-minute slot, not the
    // naive 2h the plain "01:00-03:00" clock reading implies. Nothing else
    // that day repeats either boundary, so the row only gets qualified because
    // ITS OWN start/end sit in different UTC offsets — the exact gap the
    // repeated-clock-only check left open. A fresh rink keeps "exactly 1
    // created" unambiguous.
    const dstRink3 = await page.evaluate(async (season) => {
      const F = window.hsFixture;
      const venue = await F.create("venue", "/api/setup/venue", { name: "DST Arena 3", league_id: null });
      await F.call("season venue-access grant", `/api/v2/setup/seasons/${season}/venue-access`, { venue_id: venue.id });
      return (await F.create("rink", "/api/setup/rink", { venue_id: venue.id, name: "DST Sheet 3" })).id;
    }, dstIds.season);
    await page.click("[data-ice-builder-open]");
    await page.waitForSelector(".ib-form", { timeout: 10000 });
    await page.selectOption("#ib-season", dstIds.season);
    await page.waitForSelector(`.ib-rink[value="${dstRink3}"]`, { timeout: 10000 });
    await page.check(`.ib-rink[value="${dstRink3}"]`);
    await selectWeekdaysOnly(page, [6]);
    await page.fill("#ib-turnover", "0");
    await page.fill("#ib-from", "2027-03-14");
    await page.fill("#ib-to", "2027-03-14");
    await page.fill("#ib-start-6", "01:00");
    await page.fill("#ib-end-6", "03:00");
    await previewCurrent(page);
    s = await previewState(page);
    if (s.new !== 1) fail(`the spring-crossing window should generate exactly 1 real-duration slot, got ${JSON.stringify(s)}`);
    let crossSlotCount = await page.$$eval(".ib-slot", (els) => els.length);
    if (crossSlotCount !== 1) fail(`expected exactly 1 visible slot row, got ${crossSlotCount}`);
    let crossRow = await page.$eval(".ib-slot", (el) => ({
      text: el.textContent,
      startOffset: el.getAttribute("data-ib-start-offset"),
      endOffset: el.getAttribute("data-ib-end-offset"),
      crosses: el.getAttribute("data-ib-dst-cross"),
    }));
    if (crossRow.crosses !== "1") fail(`the spring-crossing row must be flagged data-ib-dst-cross: ${JSON.stringify(crossRow)}`);
    if (crossRow.startOffset === crossRow.endOffset) {
      fail(`a row crossing the gap must carry DIFFERENT start/end UTC offsets: ${JSON.stringify(crossRow)}`);
    }
    if (!crossRow.text.includes(`(UTC${crossRow.startOffset})`) || !crossRow.text.includes(`(UTC${crossRow.endOffset})`)) {
      fail(`both offsets must be visibly rendered on the row: ${JSON.stringify(crossRow)}`);
    }
    if (!/DST/i.test(crossRow.text)) {
      fail(`the DST transition must be called out explicitly on the row: ${JSON.stringify(crossRow)}`);
    }
    // Exact UTC duration/tuple: a real 60-minute slot (06:00Z-07:00Z), never
    // the misleading 2h the "01:00-03:00" clock reading alone would suggest.
    let crossPv = await page.evaluate(() =>
      cardDisplayPayload(readCardState(ICE_BUILDER_CARD)).preview.slots[0]);
    if (crossPv.start_time !== "2027-03-14T06:00:00+00:00"
        || crossPv.end_time !== "2027-03-14T07:00:00+00:00") {
      fail(`unexpected UTC tuple for the spring-crossing slot: ${JSON.stringify(crossPv)}`);
    }
    await page.click("[data-ib-commit]");
    await page.waitForFunction(
      () => document.body.dataset.view === "calendar" && !document.querySelector(".ib-form"),
      null, { timeout: 10000 });
    let crossCommitted = await page.evaluate(async (rink) => {
      const ov = await (await fetch("/api/demo/overview", { credentials: "same-origin" })).json();
      return (ov.ice_slots || []).filter((x) => x.rink_id === rink)
        .map((x) => ({ start: x.start_time, end: x.end_time }));
    }, dstRink3);
    if (crossCommitted.length !== 1 || crossCommitted[0].start !== crossPv.start_time
        || crossCommitted[0].end !== crossPv.end_time) {
      fail(`commit must persist exactly the previewed UTC tuple: ${JSON.stringify(crossCommitted)} vs preview ${JSON.stringify(crossPv)}`);
    }

    // (K5) A SINGLE row that itself crosses the fall-back repeated hour (#313
    // follow-up review): window 01:00-02:00 with playable_minutes=120 is a
    // real 120-minute slot — the plain "01:00-02:00" clock reading alone looks
    // like an unremarkable 1-hour game, hiding the extra real hour the
    // repeated clock adds. Same shape as K4, one rink further, on the OTHER
    // DST direction and with a non-default playable_minutes.
    const dstRink4 = await page.evaluate(async (season) => {
      const F = window.hsFixture;
      const venue = await F.create("venue", "/api/setup/venue", { name: "DST Arena 4", league_id: null });
      await F.call("season venue-access grant", `/api/v2/setup/seasons/${season}/venue-access`, { venue_id: venue.id });
      return (await F.create("rink", "/api/setup/rink", { venue_id: venue.id, name: "DST Sheet 4" })).id;
    }, dstIds.season);
    await page.click("[data-ice-builder-open]");
    await page.waitForSelector(".ib-form", { timeout: 10000 });
    await page.selectOption("#ib-season", dstIds.season);
    await page.waitForSelector(`.ib-rink[value="${dstRink4}"]`, { timeout: 10000 });
    await page.check(`.ib-rink[value="${dstRink4}"]`);
    await selectWeekdaysOnly(page, [6]);
    await page.fill("#ib-turnover", "0");
    await page.fill("#ib-playable", "120");
    await page.fill("#ib-from", "2026-11-01");
    await page.fill("#ib-to", "2026-11-01");
    await page.fill("#ib-start-6", "01:00");
    await page.fill("#ib-end-6", "02:00");
    await previewCurrent(page);
    s = await previewState(page);
    if (s.new !== 1) fail(`the fall-crossing window should generate exactly 1 real-duration slot, got ${JSON.stringify(s)}`);
    crossSlotCount = await page.$$eval(".ib-slot", (els) => els.length);
    if (crossSlotCount !== 1) fail(`expected exactly 1 visible slot row, got ${crossSlotCount}`);
    crossRow = await page.$eval(".ib-slot", (el) => ({
      text: el.textContent,
      startOffset: el.getAttribute("data-ib-start-offset"),
      endOffset: el.getAttribute("data-ib-end-offset"),
      crosses: el.getAttribute("data-ib-dst-cross"),
    }));
    if (crossRow.crosses !== "1") fail(`the fall-crossing row must be flagged data-ib-dst-cross: ${JSON.stringify(crossRow)}`);
    if (crossRow.startOffset === crossRow.endOffset) {
      fail(`a row crossing the fall-back hour must carry DIFFERENT start/end UTC offsets: ${JSON.stringify(crossRow)}`);
    }
    if (!crossRow.text.includes(`(UTC${crossRow.startOffset})`) || !crossRow.text.includes(`(UTC${crossRow.endOffset})`)) {
      fail(`both offsets must be visibly rendered on the row: ${JSON.stringify(crossRow)}`);
    }
    if (!/DST/i.test(crossRow.text)) {
      fail(`the DST transition must be called out explicitly on the row: ${JSON.stringify(crossRow)}`);
    }
    // Exact UTC duration/tuple: a real 120-minute slot (05:00Z-07:00Z), never
    // the misleading 1h the "01:00-02:00" clock reading alone would suggest.
    crossPv = await page.evaluate(() =>
      cardDisplayPayload(readCardState(ICE_BUILDER_CARD)).preview.slots[0]);
    if (crossPv.start_time !== "2026-11-01T05:00:00+00:00"
        || crossPv.end_time !== "2026-11-01T07:00:00+00:00") {
      fail(`unexpected UTC tuple for the fall-crossing slot: ${JSON.stringify(crossPv)}`);
    }
    await page.click("[data-ib-commit]");
    await page.waitForFunction(
      () => document.body.dataset.view === "calendar" && !document.querySelector(".ib-form"),
      null, { timeout: 10000 });
    crossCommitted = await page.evaluate(async (rink) => {
      const ov = await (await fetch("/api/demo/overview", { credentials: "same-origin" })).json();
      return (ov.ice_slots || []).filter((x) => x.rink_id === rink)
        .map((x) => ({ start: x.start_time, end: x.end_time }));
    }, dstRink4);
    if (crossCommitted.length !== 1 || crossCommitted[0].start !== crossPv.start_time
        || crossCommitted[0].end !== crossPv.end_time) {
      fail(`commit must persist exactly the previewed UTC tuple: ${JSON.stringify(crossCommitted)} vs preview ${JSON.stringify(crossPv)}`);
    }

    const createFocusModeLedger = CREATE_FOCUS_MODES.map((mode) => [
      mode, createFocusModeCounts.get(mode) || 0,
    ]);
    if (createFocusModeLedger.some(([, count]) => count !== 1)) {
      fail(`Create focus modes must each run exactly once: `
        + JSON.stringify(createFocusModeLedger));
    }
    if (errors.length) fail(`console/page errors:\n${errors.join("\n")}`);
    console.log(`[${viewport.label}] OK — month grid renders and all 42 day destinations preserve focus; Builder Open, Back and successful Create preserve focus through their real loading paints and settlements without stealing the context selector on programmatic activation; Back also preserves focus through accepted context settlement and an overlapping same-tuple render without stealing a newer choice; builder previews ${EXPECTED_NEW} slots, reports un-granted venue, commits idempotently, honors exclusions, applies per-weekday windows (narrow Thursday => 19), binds commit to the preview (edit invalidates it), refuses+refreshes a stale preview (both a bogus token and a same-slot-set window edit that slips the suspenders), reports an exact-tuple collision as a conflict WITH its target, exposes every row of a >60-day template — the final day and a late Game collision's exact target — while committing the full previewed set, and in a DST-observing Program timezone visibly reports a spring-forward gap skip, commits a gap-spanning window's 2 real-duration slots, visibly distinguishes a fall-back day's 4 real-hour slots including the two that share a repeated local clock time, and — even with nothing else that day to collide against — visibly qualifies and explicitly calls out a single row that itself crosses the DST change, in both directions, with the exact real UTC duration/tuple committed as previewed.`);
  } catch (error) {
    throw new Error(`${error.message}\n--- demo server output ---\n${serverOutput}`);
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
    console.log("Ice Availability Builder browser journey passed.");
  } catch (error) {
    console.error("Ice Availability Builder browser journey FAILED.");
    console.error(error && error.message ? error.message : error);
    process.exitCode = 1;
  } finally {
    if (browser) await browser.close();
  }
}

main();
