# Manual Keyboard and Screen-Reader Validation Protocol

## Status

**Protocol and evidence templates only. No manual keyboard or screen-reader
validation has been performed under this document.** This deliverable
prepares the repeatable procedure and blank evidence artifacts required by
[#345][issue-345]'s manual accessibility gate; it does not itself
constitute validation evidence, and nothing in this document should be read
as a claim that manual accessibility validation is complete.

This document adds no application code, tests, CI configuration, or
permission changes. It is documentation only, filed alongside the rest of
the guided-Setup / IA / accessibility requirements work in `docs/product/`.

[issue-345]: https://github.com/jingizoo/biknik/issues/345

## Relationship to the moderated operator-validation protocol

This protocol is **independent of and complementary to**
[`moderated-operator-validation-protocol.md`](moderated-operator-validation-protocol.md).
Keep the two separate:

- **This document** validates accessibility *mechanics* — does the keyboard
  reach every control, does focus go where it should, does a screen reader
  announce the right thing — against WCAG 2.2 AA and #345's own
  accessibility requirements. It runs with a moderator/tester who already
  knows the app, deliberately probing implementation details (first
  Shift+Tab, focus-restore fallback, live-region timing) that an ordinary
  operator would never notice.
- **The moderated operator-validation protocol** validates *task
  usability* for real, unfamiliar operators — can a League Admin, Arena
  Manager, or Coach complete a real task without being led. It is not a
  substitute for this protocol's mechanical checks, and this protocol is
  not a substitute for that one's usability findings.
- Both are required by #345 before its implementation PR merges. Neither
  waives or simulates the other.

## Authority

This protocol implements — and invents nothing beyond — #345 and the
signed-off requirements package it inherits from:

- **[#345][issue-345]**, "State model and accessibility": *"Add dialog/
  drawer focus trapping, skip-to-content, per-view page titles, correct
  destination focus, accessible status announcements, semantic headings/
  labels, visible focus, keyboard operation, reflow, and target-size
  behavior. Add automated WCAG 2.2 AA coverage for changed surfaces with
  zero serious/critical violations, plus the required manual keyboard-only
  and screen-reader evidence."*
- **#345 acceptance criteria**: *"Desktop, 390px, breakpoint-boundary,
  keyboard, screen-reader, WCAG 2.2 AA, and zero-console-error evidence is
  attached."*
- **[`docs/product/operator-ux-requirements.md`](operator-ux-requirements.md),
  §7** ("WCAG 2.2 AA — keyboard, focus, labeling, screen-reader") — the full
  Level A/AA conformance matrix, the priority regression list (dialog focus
  management, bypass blocks, per-view page titling, dragging alternative,
  labeling convention, reflow at 320px, contrast/target-size at desktop),
  and its own explicit gate: *"A manual keyboard-only and screen-reader
  pass on the Home/Tasks hub and guided Setup hub is required before #345
  is considered done... this is what catches everything the automated gate
  structurally cannot."*
- **§5** ("Loading, empty, stale, error, retry, and confirmation states")
  for the exact state vocabulary this protocol's surfaces 7 and the
  evidence template reuse (skeleton loading, `.empty`/`pageIntro`,
  stale-response guard, per-card vs. whole-pane error, row-level vs.
  page-level retry, named-resource confirmation modals).
- The already-implemented mechanics this protocol tests against, as
  documented in §7 and in `app.js`/`e2e/accessibility-foundations.js`:
  `#skip-link` inside `.web`; `setShellTitle()`'s three shell states
  (`login`, `public`, `app`); `syncOverlayFocus()`'s container-first,
  keyed, re-anchor-on-orphan, restore-on-close-only-if-orphaned dialog
  focus lifecycle; the `#toast-root[aria-live="polite"]` status channel;
  the native `<select id="context-switcher">` context control.

### Conflicts log

If an expected behavior below turns out to be unclear, unimplemented, or in
tension with the application's actual contract (e.g. a screen that cannot
technically announce something this protocol expects), record it here
rather than silently loosening the expectation or the pass bar.

| Date found | Surface / step | Description | Resolution |
| --- | --- | --- | --- |
| *(none yet — add rows as they're found)* | | | |

---

## 1. Shared session setup

Complete once per validation pass (a "pass" may cover multiple surfaces in
one sitting, or be split across sessions — record the scope actually
covered in each evidence copy).

### 1.1 Build identification

| Field | Value |
| --- | --- |
| Application version / release tag | *(fill in)* |
| Git head SHA under test | *(fill in — `git rev-parse HEAD`; must match the #345 PR head this evidence is meant to support)* |
| Deployment target | local dev server / staging / other *(fill in)* |

### 1.2 Environment fields

| Field | Value |
| --- | --- |
| Operating system + version | *(fill in)* |
| Browser + version | *(fill in — screen-reader/browser pairing matters; record both together, e.g. VoiceOver+Safari, NVDA+Firefox, JAWS+Chrome)* |
| Screen reader + version | *(fill in, or "none" for a keyboard-only-only pass — see §2/§3 below, both are independently required)* |
| Viewport / device | Desktop, or the canonical phone viewport **390×844**, or a named physical device *(fill in)* |

### 1.3 Account and role

| Field | Value |
| --- | --- |
| Account / persona used | Use the app's existing seeded demo personas (`admin`/League Admin, `arena`/Arena Manager, `coach`/Coach, or `player`/`guardian`/`official`/`viewer` as the surface requires) per `backend/hockey_scheduler/web/auth.py`. **Do not record the password**, only which persona/role. |
| Role under test | *(fill in — some surfaces, e.g. signed-out login and Public Schedule, need no authenticated role at all)* |

### 1.4 Starting context and fixture

| Field | Value |
| --- | --- |
| Active Program / Season / League at session start | *(fill in)* |
| Fixture / data state | *(fill in — note specifically whether the fixture includes at least one incomplete Setup workflow, at least one restricted/403 case, and at least one error-triggering condition, since surfaces 5 and 7 need those states to actually occur, not just be described)* |
| Reset procedure | Same reset procedure as the moderated operator-validation protocol §1.5 (header-menu `RESET`, or production factory-reset for a non-demo environment) — run it before starting a fresh pass so no prior pass's state leaks in. |

### 1.5 Scope of this pass

| Field | Value |
| --- | --- |
| Surfaces covered (list from §2 below) | *(fill in)* |
| Keyboard-only pass, screen-reader pass, or both | *(fill in — both are required across the full surface list before #345's gate is satisfied, but a single sitting may cover only one)* |

---

## 2. Surfaces to cover

Every pass must eventually cover all eight; track completion across passes
rather than requiring one marathon session. Each surface below maps to the
keyboard procedure (§3) and screen-reader procedure (§4) that apply to it —
not every procedure step applies to every surface (e.g. "context switching"
only applies where a context bar exists).

1. **Signed-out login** — the `showLogin()` shell state.
2. **Public Schedule and Staff sign-in transition** — the anonymous
   `showPublicGuest()` shell state and the control that returns to Staff
   sign-in.
3. **Authenticated Home/Tasks** — the Home/Tasks hub, its task cards, and
   its own loading/empty/error/complete states.
4. **Program/Season/League context switching** — the persistent context
   bar (`#context-switcher` and, once delivered, the promoted League
   control), including its effect on whichever screen is active.
5. **All six Setup workflows** — League profile and seasons (Add Season);
   Permanent teams (Add Team); Season participation/divisions (Register
   Team); Clubs, players and staff (Add Player); Venues, rinks and ice (Add
   Ice); Imports and onboarding (Import data, including its non-blocking
   optional status per Decision 9).
6. **Drawers and confirmation modals** — every `role="dialog"
   aria-modal="true"` surface (the Setup `.drawer` and the `.modal`
   confirmation shape), including nested modal-over-drawer.
7. **Loading, empty, error, retry, restricted, optional, and completed
   states** — exercised on at least one screen that genuinely reaches each
   state (per the §5 states matrix in the requirements package), not
   inferred from markup alone.
8. **Desktop and canonical 390×844 behavior** — every surface above,
   repeated at both.

---

## 3. Keyboard procedure

For each item, the evidence template (§6) records the exact steps taken,
the expected outcome (given below), the actual outcome observed, and
pass/fail. Use a real keyboard (or keyboard-emulation mode), not a mouse
with occasional Tab presses.

| # | Step | Expected outcome |
| --- | --- | --- |
| K1 | From a fresh page load (signed out), press Tab once. | Focus lands on the **"Skip to main content"** link (`#skip-link`), which becomes visually visible. This must be the first relevant tab stop — before any nav/chrome control. |
| K2 | Activate the skip link (Enter). | Focus moves into `#content` (not just a scroll) — the fragment target is a real focus move, verified by checking `document.activeElement`. |
| K3 | Continue tabbing through the full authenticated shell. | Navigation order is logical (matches visual reading order: skip link → sidebar/nav → context bar → main content → per-view controls), and every focused control shows a visible focus indicator (`:focus-visible`) at all times — never a focus move with no visible ring. |
| K4 | Activate any button-role control with Enter, then separately with Space. | Both activate the control identically; neither triggers a page scroll side-effect (Space's native scroll behavior must not leak through on a `role="button"` element). |
| K5 | Change the active Program/Season(/League) via the context switcher using only the keyboard (arrow keys / typeahead on the native `<select>`). | The context updates, the active screen re-filters (or documents its named exception per §3 of the requirements package), and focus remains on the context control — it does not jump elsewhere unexpectedly. |
| K6 | From a trigger control, activate a drawer or modal open action via keyboard (Enter/Space, not a click). | The dialog opens and focus moves into it. |
| K7 | Immediately after a dialog opens, press Tab once. | Focus lands somewhere *inside* the dialog (per #345's implementation, the dialog container itself, which carries `tabindex="-1"` — not necessarily the first form field). Focus must not leave the dialog on this first Tab. |
| K8 | Immediately after a dialog opens, press Shift+Tab once (as its own, separate check — do not chain onto K7 in the same run). | Focus stays inside the dialog. This is the specific entry-boundary case #345 fixed (`df244ab`) after finding the first Shift+Tab used to escape to the page behind the drawer — re-verify it explicitly, don't assume it from K7. |
| K9 | With the dialog open, Tab forward repeatedly past its last focusable control (at least one full extra cycle). | Focus wraps back to the dialog's first focusable control — it never lands on a sidebar/background control. |
| K10 | With the dialog open, Shift+Tab backward repeatedly past its first focusable control. | Focus wraps to the dialog's last focusable control — same containment, opposite direction. |
| K11 | With the dialog open, press Escape. | The dialog closes. |
| K12 | Open the dialog from a trigger that remains visible/enabled after close, then close via the close button (not Escape — check both paths independently). | Focus returns to the original triggering control (re-resolved by selector, not by a stale node reference). |
| K13 | Open the dialog from a trigger that will no longer exist on close (e.g. a row that gets removed by the dialog's own action, or a context switch that removes it), then close. | Focus falls back to the documented view-level fallback (e.g. the view heading) — never left on `<body>`, never left silently unfocused. |
| K14 | Open a modal *from inside an already-open drawer* (modal-over-drawer nesting). | The modal is topmost and owns focus (containment per K7–K10 applies to the modal, not the drawer). Closing the modal returns focus to the drawer; closing the drawer afterward returns focus to the drawer's own original trigger — the nested open must not have overwritten the outermost return target. |
| K15 | Trigger a retry action (row-level or page-level) via keyboard on a screen in an error state. | The retry re-runs without moving focus away from the control that was just activated (focus is not silently dropped or reset to the top of the page). |
| K16 | Tab through a screen in a **restricted** (403-style) state and through a card/row that a role does not have permission to act on (e.g. Viewer). | No control that the current role cannot use enters the tab order at all — it must be absent or genuinely disabled+unreachable, not merely visually hidden while still focusable. |
| K17 | Across every check above, attempt to Tab/Shift+Tab *out* of any surface that is not an intentionally open modal/drawer (i.e. the normal page, not a dialog). | Focus always keeps moving — never gets stuck (a keyboard trap outside an intentionally open dialog is itself the specific failure this checks for; compare against K7–K10, which require containment *only* inside an open dialog). |

---

## 4. Screen-reader procedure

Run with a real screen reader (VoiceOver, NVDA, or JAWS — record which, per
§1.2). For each item, record the announcement heard **verbatim**, not a
paraphrase of what it should have said.

| # | Step | Expected announcement |
| --- | --- | --- |
| S1 | Load the signed-out login page. | The browser tab title (read by the screen reader on page focus / via a "read title" command) announces a **Sign in**-specific title, not a stale or generic one — per `setShellTitle("login", ...)`. |
| S2 | Navigate to the Public Schedule (anonymous portal), then use the control that returns to Staff sign-in. | The title announces **Public Schedule** while on that surface, and announces the sign-in-specific title again once back on the sign-in card — both transitions update the announced title, not just the sign-in→public leg. |
| S3 | Sign in and land on Home/Tasks. | The title announces the authenticated destination's `<NAV label> — Hockey Scheduler` form (per `setShellTitle("app", ...)`), and the screen reader's landmark navigation (e.g. VoiceOver's rotor, NVDA's elements list) surfaces at least a `main`/`nav` landmark structure with a sensible heading hierarchy (one clear top-level heading per view, not multiple or none). |
| S4 | Navigate the sidebar to a different top-level area. | The current navigation destination is announced as selected/current (e.g. `aria-current` or equivalent), so a screen-reader user can tell which area they're in without relying on sight. |
| S5 | Change the active Program/Season(/League) via the context switcher. | The screen reader announces the newly selected option (native `<select>` semantics — confirm this is preserved, since #345 requires keeping the native control specifically for this reason), and if the screen re-filters, any resulting content change is announced (e.g. via the toast/status channel) rather than silently changing under the user with no cue. |
| S6 | Open a drawer or modal. | The screen reader announces the dialog's role ("dialog"), its accessible name (the heading/`aria-label` the trigger implies), and that it is modal — confirming `role="dialog" aria-modal="true"` plus a real accessible name, not just "dialog" with no name. |
| S7 | Within an open dialog, tab to a required form field, then submit with it empty. | The field's label, its required state, and the resulting validation error are all announced — not just visually shown. Check specifically that the error is announced once (not silently, not duplicated) and is associated with the specific field via a description/error-message relationship, not just proximate text. |
| S8 | Trigger a loading state (e.g. a fresh navigation to a data-backed screen). | A loading/busy state is perceivable to a screen-reader user (e.g. an announced "Loading" status, or a live region that later announces the result) — it must not be silent from the screen reader's perspective even though sighted users see a skeleton. |
| S9 | Trigger a success action (e.g. a create/submit that succeeds). | The toast/status channel (`#toast-root[aria-live="polite"]`) announces the success message once. |
| S10 | Trigger an error on a per-card (Home/Tasks, Setup workflow landing) and separately on a whole-pane screen. | Each error is announced in a way that identifies *which* card failed (per-card) or that the page failed (whole-pane) — not a generic unlocalized "error" with no scope, and the retry control's label/purpose is announced clearly. |
| S11 | Navigate to the "Imports and onboarding" workflow when workflows 1–5 are otherwise complete. | Its status is announced as distinctly **optional** — not as "done" and not as blocking/"todo" the way a required workflow would be — matching Decision 9's third-status contract. Record the exact wording heard. |
| S12 | Navigate to a restricted/403 surface, or a control unavailable to the current role (e.g. Viewer attempting a mutation). | The restricted/unauthorized state is announced explicitly (a real status, e.g. "restricted" or equivalent wording) — not silently absent controls with no explanation, and not a generic error indistinguishable from a real failure. |
| S13 | With a screen reader running, trigger two rapid state changes in succession (e.g. a stale-guarded context switch that supersedes an in-flight load, or two toast messages back to back). | Only the current, correct state is announced — no stale announcement from the superseded request, and no duplicate/overlapping announcement that garbles both messages. This is the live-region equivalent of the stale-response guard's visual behavior — confirm it holds for what's *announced*, not only what's rendered. |
| S14 | Complete a full Setup workflow item (e.g. register a team) and return to Home/Tasks. | The now-complete task card's status is announced as **completed**, distinctly from its prior loading/todo announcement — confirm the change is perceivable via the screen reader, not just a visual checkmark. |

---

## 5. Evidence template

Copy this block once per pass (or once per surface, if a pass is split
across sessions) and fill in every field. An unfilled field must carry an
explicit "N/A" and a reason — a blank field reads as an unrun check.

```markdown
### Manual keyboard/screen-reader validation evidence — <surface(s)> — <date>

**Build**
- Application version / release tag:
- Git head SHA:
- Deployment target:

**Environment**
- OS + version:
- Browser + version:
- Screen reader + version (or "none" — keyboard-only pass):
- Viewport / device:

**Account and role**
- Account/persona used (never the password):
- Role under test:

**Starting context and fixture**
- Active Program/Season/League:
- Fixture/data state (confirm it includes the states this pass needs, e.g. an incomplete Setup workflow, a restricted/403 case, an error condition):

**Scope of this pass**
- Surfaces covered:
- Keyboard-only / screen-reader / both:

**Step-by-step results**

| Step ID | Surface | Expected result | Actual result | Focus before | Focus after | Announcement heard (verbatim) | Pass/fail |
| --- | --- | --- | --- | --- | --- | --- | --- |
| | | | | | | | |

(Add one row per keyboard step (§3) and per screen-reader step (§4)
actually exercised in this pass — do not summarize multiple steps into one
row.)

**Console errors**
- Any browser console error observed during this pass, with the step that triggered it:

**Confirmed defects**

| Defect | Step(s) it violates | Reproduction (exact steps to reproduce) | Severity (blocker / major / minor) |
| --- | --- | --- | --- |
| | | | |

**Sign-off**

| Role | Name | Date | Signature/approval |
| --- | --- | --- | --- |
| Reviewer (the person who ran this pass) | | | |
| Repository owner | | | |
```

---

## 6. Rules

- Do not fabricate results, recordings, announcements, or sign-offs. Every
  field in §5's template must reflect something actually observed in a real
  browser/screen-reader session, or be explicitly marked not yet run.
- Do not state, in this document or elsewhere, that manual accessibility
  validation is complete. Completion is established only by filled-in,
  signed-off copies of the §5 template covering every surface in §2 —
  never by this protocol document itself.
- This protocol does not authorize and must not be used to justify changing
  application code, tests, CI configuration, or permissions. Defects found
  here are recorded (§5's "Confirmed defects" table) for separate follow-up
  work, not fixed inline as part of running the protocol.
- The approved [#345][issue-345] requirements and WCAG 2.2 AA (per
  `operator-ux-requirements.md` §7) are the authority for expected
  behavior. Where this protocol's expected-outcome text and the actual
  application contract conflict, or where a step's expected behavior is
  genuinely unclear, record it in the [Conflicts log](#conflicts-log) for
  owner review rather than resolving it unilaterally.
- Keep this protocol's evidence separate from the moderated operator
  sessions' evidence (see
  [Relationship to the moderated operator-validation protocol](#relationship-to-the-moderated-operator-validation-protocol)
  above) — this validates accessibility mechanics, not task usability, and
  the two evidence sets answer different questions for #345's merge gate.
