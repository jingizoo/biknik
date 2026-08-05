# Keyboard script — K1 to K17, runnable

Part of the [#345 human-validation facilitator pack](README.md). **This sheet is
blank instrumentation. Nothing in it is evidence of a performed pass.**

Source of every quoted step and expected outcome:
`docs/product/manual-keyboard-screenreader-validation-protocol.md` §3, read at
`origin/main` = `36195faadb5c97936022d8f3706af51181a6b64d`. The steps below are
reproduced verbatim, including the corrections #394 made to K5. Nothing here
summarises, reorders, or re-scopes them. The §2 surface list every step below
asks you to name, and the §6 rules that govern what you do with what you find,
are both reproduced in full further down this sheet, so a pass can be run without
reopening the protocol; for anything wider than that — the authority section, the
conflicts log — read the protocol itself.

How to run it: protocol §3's own preamble.

> For each item, the evidence template (§6) records the exact steps taken,
> the expected outcome (given below), the actual outcome observed, and
> pass/fail. Use a real keyboard (or keyboard-emulation mode), not a mouse
> with occasional Tab presses.

*(One thing that will confuse you if you go looking: that "(§6)" is a stale
cross-reference in the protocol. In the protocol as it stands at `36195fa`, §5 is
the evidence template and §6 is the rules — you want §5, quoted below under
"Session blanks". Noted rather than corrected: this pack does not edit the
protocol, and a reviewer may want to fix the reference at the source.)*

Before you start, bring the environment up and complete the pre-flight in
[01-environment-league-admin.md](01-environment-league-admin.md) (or the
Arena Manager / Coach sheet, if the surface under test needs that role). A step
whose state never occurred is not a pass — record it as not run.

## Session blanks — fill in before the first step

Every field below is required. Protocol §5 (evidence template) sets that bar:

> Copy this block once per pass (or once per surface, if a pass is split
> across sessions) and fill in every field. An unfilled field must carry an
> explicit "N/A" and a reason — a blank field reads as an unrun check.

| Field | Value |
| --- | --- |
| Validator (the person running this pass) | |
| Date | |
| Git head SHA under test | |
| Application version / release tag | |
| Deployment target (local dev server / staging / other) | |
| OS + version | |
| Browser + version | |
| Screen reader + version (or "none" for a keyboard-only pass) | |
| Other assistive technology in use + version | |
| Viewport / device (desktop, 390x844, or named device) | |
| Persona signed in as (never the password) | |
| Role under test | |
| Active Program / Season / League at session start | |
| Fixture / data state (which environment sheet was used, and every deviation applied) | |
| Surfaces covered this pass (from protocol §2) | |

The SHA field is not bookkeeping. Protocol §1.1 states what it has to satisfy:

> | Git head SHA under test | *(fill in — `git rev-parse HEAD`; must match the #345 PR head this evidence is meant to support)* |

Participant/validator identity: this pass has no external participant, but the
same privacy rule applies to whoever is named above — see the moderated
operator-validation protocol §1.6, quoted in full on each moderated capture
sheet in this pack. Record what is needed to interpret the result and nothing
more.

---

## The eight surfaces — protocol §2, verbatim

Every step below has a **"Surface exercised (protocol §2 item)"** row, and the
session blanks above ask which surfaces this pass covered. This is the list those
fields refer to. Record the number and the name.

> Every pass must eventually cover all eight; track completion across passes
> rather than requiring one marathon session. Each surface below maps to the
> keyboard procedure (§3) and screen-reader procedure (§4) that apply to it —
> not every procedure step applies to every surface (e.g. "context switching"
> only applies where a context bar exists).
>
> 1. **Signed-out login** — the `showLogin()` shell state.
> 2. **Public Schedule and Staff sign-in transition** — the anonymous
>    `showPublicGuest()` shell state and the control that returns to Staff
>    sign-in.
> 3. **Authenticated Home/Tasks** — the Home/Tasks hub, its task cards, and
>    its own loading/empty/error/complete states.
> 4. **Program/Season/League context switching** — the persistent context
>    bar (`#context-switcher` and, once delivered, the promoted League
>    control), including its effect on whichever screen is active.
> 5. **All six Setup workflows** — League profile and seasons (Add Season);
>    Permanent teams (Add Team); Season participation/divisions (Register
>    Team); Clubs, players and staff (Add Player); Venues, rinks and ice (Add
>    Ice); Imports and onboarding (Import data, including its non-blocking
>    optional status per Decision 9).
> 6. **Drawers and confirmation modals** — every `role="dialog"
>    aria-modal="true"` surface (the Setup `.drawer` and the `.modal`
>    confirmation shape), including nested modal-over-drawer.
> 7. **Loading, empty, error, retry, restricted, optional, and completed
>    states** — exercised on at least one screen that genuinely reaches each
>    state (per the §5 states matrix in the requirements package), not
>    inferred from markup alone.
> 8. **Desktop and canonical 390×844 behavior** — every surface above,
>    repeated at both.

Surfaces 5 and 7 are the reason the environment sheets make you verify an
incomplete workflow, a restricted case and an error condition before you start:
a state that never occurred cannot be recorded as a pass.

---

## The rules that govern this pass — protocol §6, verbatim

Read these before the first step. The third one is the one you will need in the
middle of a pass, the moment you find something broken.

> - Do not fabricate results, recordings, announcements, or sign-offs. Every
>   field in §5's template must reflect something actually observed in a real
>   browser/screen-reader session, or be explicitly marked not yet run.
> - Do not state, in this document or elsewhere, that manual accessibility
>   validation is complete. Completion is established only by filled-in,
>   signed-off copies of the §5 template covering every surface in §2 —
>   never by this protocol document itself.
> - This protocol does not authorize and must not be used to justify changing
>   application code, tests, CI configuration, or permissions. Defects found
>   here are recorded (§5's "Confirmed defects" table) for separate follow-up
>   work, not fixed inline as part of running the protocol.
> - The approved [#345][issue-345] requirements and WCAG 2.2 AA (per
>   `operator-ux-requirements.md` §7) are the authority for expected
>   behavior. Where this protocol's expected-outcome text and the actual
>   application contract conflict, or where a step's expected behavior is
>   genuinely unclear, record it in the [Conflicts log](#conflicts-log) for
>   owner review rather than resolving it unilaterally.
> - Keep this protocol's evidence separate from the moderated operator
>   sessions' evidence (see
>   [Relationship to the moderated operator-validation protocol](#relationship-to-the-moderated-operator-validation-protocol)
>   above) — this validates accessibility mechanics, not task usability, and
>   the two evidence sets answer different questions for #345's merge gate.

The two links inside that quote — the Conflicts log, and the Relationship
section — point into the protocol document, not into this sheet; they are part
of the quoted text.

Two of those rules bind this pack directly. A defect you find here is written into the
protocol's own defect table for separate follow-up work — running the protocol
does not authorise touching the application. And an expected outcome that turns
out to be unclear or in tension with what the app actually does goes into the
protocol's Conflicts log for the owner, never quietly loosened on this sheet:
that is precisely how K5/S5 came to instruct validators to expect the opposite of
shipped behaviour until #394 corrected it.

---

## Steps

### K1

**Step (protocol §3, K1 — verbatim):**

> From a fresh page load (signed out, on the login card), press Tab repeatedly through the full card. Separately, repeat on the Public Schedule surface.

**Expected outcome (protocol §3, K1 — verbatim, the protocol's own column):**

> **No focusable skip link appears in either tab order.** `#skip-link` lives inside the authenticated `.web` shell, which `body.signed-out .web { display: none }` hides atomically on both surfaces — a focusable skip link here (with its `#content` target hidden) is the dangling-link defect #345 already fixed, not a passing result.

| Field | Value |
| --- | --- |
| Applies to this pass? (yes / N/A + reason) | |
| Surface exercised (protocol §2 item) | |
| Pass / fail | |
| What actually happened | |
| Focus before | |
| Focus after | |
| Defect note (and severity: blocker / major / minor) | |

### K2

**Step (protocol §3, K2 — verbatim):**

> Sign in, reach the authenticated shell (reload it if needed so this is a fresh boot of `.web`), and press Tab once.

**Expected outcome (protocol §3, K2 — verbatim, the protocol's own column):**

> Focus lands on the **"Skip to main content"** link (`#skip-link`), which becomes visually visible. This must be the first relevant tab stop in the authenticated shell — before any nav/chrome control. Then activate it (Enter): focus moves into `#content` (not just a scroll) — a real focus move, verified by checking `document.activeElement`.

| Field | Value |
| --- | --- |
| Applies to this pass? (yes / N/A + reason) | |
| Surface exercised (protocol §2 item) | |
| Pass / fail | |
| What actually happened | |
| Focus before | |
| Focus after | |
| Defect note (and severity: blocker / major / minor) | |

### K3

**Step (protocol §3, K3 — verbatim):**

> Continue tabbing through the full authenticated shell.

**Expected outcome (protocol §3, K3 — verbatim, the protocol's own column):**

> Navigation order is logical (matches visual reading order: skip link → sidebar/nav → context bar → main content → per-view controls), and every focused control shows a visible focus indicator (`:focus-visible`) at all times — never a focus move with no visible ring.

| Field | Value |
| --- | --- |
| Applies to this pass? (yes / N/A + reason) | |
| Surface exercised (protocol §2 item) | |
| Pass / fail | |
| What actually happened | |
| Focus before | |
| Focus after | |
| Defect note (and severity: blocker / major / minor) | |

### K4

**Step (protocol §3, K4 — verbatim):**

> Activate any button-role control with Enter, then separately with Space.

**Expected outcome (protocol §3, K4 — verbatim, the protocol's own column):**

> Both activate the control identically; neither triggers a page scroll side-effect (Space's native scroll behavior must not leak through on a `role="button"` element).

| Field | Value |
| --- | --- |
| Applies to this pass? (yes / N/A + reason) | |
| Surface exercised (protocol §2 item) | |
| Pass / fail | |
| What actually happened | |
| Focus before | |
| Focus after | |
| Defect note (and severity: blocker / major / minor) | |

### K5

**Step (protocol §3, K5 — verbatim):**

> Standing on **Games**, **Roster** or **Standings** with data visible, change the active Program/Season via `#ctx-select` (inside the `#context-switcher` wrapper) using only the keyboard (arrow keys / typeahead on the native `<select>`). Repeat separately on `#ctx-league-select`.

**Expected outcome (protocol §3, K5 — verbatim, the protocol's own column):**

> The selection updates and focus remains on the select you operated — it does not jump elsewhere unexpectedly. **The active screen re-filters:** the content repaints to the newly selected Program/Season (and League), and records belonging only to the previous selection are gone. "No visible content change" is a **defect**, not a pass. Judge this by what the screen shows, **not** by any caption text — do not gate this step on the wording of `#ctx-scope-note` or any other sentence (the previous revision of this step did exactly that and inverted when the copy changed). If the two selections you switch between happen to hold identical data, pick a different pair rather than recording a pass.

| Field | Value |
| --- | --- |
| Applies to this pass? (yes / N/A + reason) | |
| Surface exercised (protocol §2 item) | |
| Pass / fail | |
| What actually happened | |
| Focus before | |
| Focus after | |
| Defect note (and severity: blocker / major / minor) | |

### K6

**Step (protocol §3, K6 — verbatim):**

> From a trigger control, activate a drawer or modal open action via keyboard (Enter/Space, not a click).

**Expected outcome (protocol §3, K6 — verbatim, the protocol's own column):**

> The dialog opens and focus moves into it.

| Field | Value |
| --- | --- |
| Applies to this pass? (yes / N/A + reason) | |
| Surface exercised (protocol §2 item) | |
| Pass / fail | |
| What actually happened | |
| Focus before | |
| Focus after | |
| Defect note (and severity: blocker / major / minor) | |

### K7

**Step (protocol §3, K7 — verbatim):**

> Open a dialog from a fresh trigger activation (not chained from K8's run). Immediately after open, confirm focus is on the dialog *container* itself (`document.activeElement` is the `role="dialog"` element, carrying `tabindex="-1"` — per `syncOverlayFocus()`/`focusOverlayContainer()`). Then press Tab once.

**Expected outcome (protocol §3, K7 — verbatim, the protocol's own column):**

> Focus moves to **exactly the first sequential focusable control** inside the dialog (the first element `overlayFocusables()` returns) — not merely "somewhere inside," and not left on the container. This is the entry-boundary's forward case.

| Field | Value |
| --- | --- |
| Applies to this pass? (yes / N/A + reason) | |
| Surface exercised (protocol §2 item) | |
| Pass / fail | |
| What actually happened | |
| Focus before | |
| Focus after | |
| Defect note (and severity: blocker / major / minor) | |

### K8

**Step (protocol §3, K8 — verbatim):**

> Open a **separately, freshly opened** dialog (its own independent open, not reused from K7 or chained after it). Immediately after open (focus still on the container), press Shift+Tab once.

**Expected outcome (protocol §3, K8 — verbatim, the protocol's own column):**

> Focus moves to **exactly the last sequential focusable control** inside the dialog. This is the entry-boundary case #345 fixed (`df244ab`) after finding the first Shift+Tab used to escape to the page behind the drawer — before the fix, the container was excluded from `overlayFocusables()` so neither the forward-wrap nor backward-wrap check ever matched it, and native backward traversal from a `tabindex="-1"` node walked to whatever preceded it in the document (out of the dialog). Landing anywhere other than the exact last control (including "stayed inside the dialog but not on the last control") is a fail.

| Field | Value |
| --- | --- |
| Applies to this pass? (yes / N/A + reason) | |
| Surface exercised (protocol §2 item) | |
| Pass / fail | |
| What actually happened | |
| Focus before | |
| Focus after | |
| Defect note (and severity: blocker / major / minor) | |

### K9

**Step (protocol §3, K9 — verbatim):**

> With the dialog open, Tab forward repeatedly past its last focusable control (at least one full extra cycle).

**Expected outcome (protocol §3, K9 — verbatim, the protocol's own column):**

> Focus wraps back to the dialog's first focusable control — it never lands on a sidebar/background control.

| Field | Value |
| --- | --- |
| Applies to this pass? (yes / N/A + reason) | |
| Surface exercised (protocol §2 item) | |
| Pass / fail | |
| What actually happened | |
| Focus before | |
| Focus after | |
| Defect note (and severity: blocker / major / minor) | |

### K10

**Step (protocol §3, K10 — verbatim):**

> With the dialog open, Shift+Tab backward repeatedly past its first focusable control.

**Expected outcome (protocol §3, K10 — verbatim, the protocol's own column):**

> Focus wraps to the dialog's last focusable control — same containment, opposite direction.

| Field | Value |
| --- | --- |
| Applies to this pass? (yes / N/A + reason) | |
| Surface exercised (protocol §2 item) | |
| Pass / fail | |
| What actually happened | |
| Focus before | |
| Focus after | |
| Defect note (and severity: blocker / major / minor) | |

### K11

**Step (protocol §3, K11 — verbatim):**

> With the dialog open, press Escape.

**Expected outcome (protocol §3, K11 — verbatim, the protocol's own column):**

> The dialog closes.

| Field | Value |
| --- | --- |
| Applies to this pass? (yes / N/A + reason) | |
| Surface exercised (protocol §2 item) | |
| Pass / fail | |
| What actually happened | |
| Focus before | |
| Focus after | |
| Defect note (and severity: blocker / major / minor) | |

### K12

**Step (protocol §3, K12 — verbatim):**

> Open the dialog from a trigger that remains visible/enabled after close, then close via the close button (not Escape — check both paths independently).

**Expected outcome (protocol §3, K12 — verbatim, the protocol's own column):**

> Focus returns to the original triggering control (re-resolved by selector, not by a stale node reference).

| Field | Value |
| --- | --- |
| Applies to this pass? (yes / N/A + reason) | |
| Surface exercised (protocol §2 item) | |
| Pass / fail | |
| What actually happened | |
| Focus before | |
| Focus after | |
| Defect note (and severity: blocker / major / minor) | |

### K13

**Step (protocol §3, K13 — verbatim):**

> Open the dialog from a trigger that will no longer exist on close (e.g. a row that gets removed by the dialog's own action, or a context switch that removes it), then close.

**Expected outcome (protocol §3, K13 — verbatim, the protocol's own column):**

> Focus falls back to the documented view-level fallback (e.g. the view heading) — never left on `<body>`, never left silently unfocused.

| Field | Value |
| --- | --- |
| Applies to this pass? (yes / N/A + reason) | |
| Surface exercised (protocol §2 item) | |
| Pass / fail | |
| What actually happened | |
| Focus before | |
| Focus after | |
| Defect note (and severity: blocker / major / minor) | |

### K14

**Step (protocol §3, K14 — verbatim):**

> Open a modal *from inside an already-open drawer* (modal-over-drawer nesting).

**Expected outcome (protocol §3, K14 — verbatim, the protocol's own column):**

> The modal is topmost and owns focus (containment per K7–K10 applies to the modal, not the drawer). Closing the modal returns focus to the drawer; closing the drawer afterward returns focus to the drawer's own original trigger — the nested open must not have overwritten the outermost return target.

| Field | Value |
| --- | --- |
| Applies to this pass? (yes / N/A + reason) | |
| Surface exercised (protocol §2 item) | |
| Pass / fail | |
| What actually happened | |
| Focus before | |
| Focus after | |
| Defect note (and severity: blocker / major / minor) | |

### K15

**Step (protocol §3, K15 — verbatim):**

> Trigger a retry action (row-level or page-level) via keyboard on a screen in an error state, for two cases: (a) the retry succeeds and the retried control/card survives the rerender in place; (b) the retry succeeds and its rerender removes the retried control/card entirely (e.g. the failed card is replaced by a success state or disappears from the list).

**Expected outcome (protocol §3, K15 — verbatim, the protocol's own column):**

> **(a) Survives:** the control is re-resolved after rerender and retains focus — same discipline as the dialog-trigger restore in K13, not a fresh unrelated node. **(b) Removed:** focus lands on an explicitly named fallback — the resulting card/page heading, a status/toast target, or another documented target — never silently on `<body>` and never reset to the top of the page with no explanation. A `<body>`/unexplained result is a fail in both cases; "focus stayed on the exact original node" is only the expected result for case (a), not case (b).

| Field | Value |
| --- | --- |
| Applies to this pass? (yes / N/A + reason) | |
| Surface exercised (protocol §2 item) | |
| Pass / fail | |
| What actually happened | |
| Focus before | |
| Focus after | |
| Defect note (and severity: blocker / major / minor) | |

### K16

**Step (protocol §3, K16 — verbatim):**

> Tab through a screen in a **restricted** (403-style) state and through a card/row that a role does not have permission to act on (e.g. Viewer).

**Expected outcome (protocol §3, K16 — verbatim, the protocol's own column):**

> No control that the current role cannot use enters the tab order at all — it must be absent or genuinely disabled+unreachable, not merely visually hidden while still focusable.

| Field | Value |
| --- | --- |
| Applies to this pass? (yes / N/A + reason) | |
| Surface exercised (protocol §2 item) | |
| Pass / fail | |
| What actually happened | |
| Focus before | |
| Focus after | |
| Defect note (and severity: blocker / major / minor) | |

### K17

**Step (protocol §3, K17 — verbatim):**

> Across every check above, attempt to Tab/Shift+Tab *out* of any surface that is not an intentionally open modal/drawer (i.e. the normal page, not a dialog).

**Expected outcome (protocol §3, K17 — verbatim, the protocol's own column):**

> Focus always keeps moving — never gets stuck (a keyboard trap outside an intentionally open dialog is itself the specific failure this checks for; compare against K7–K10, which require containment *only* inside an open dialog).

| Field | Value |
| --- | --- |
| Applies to this pass? (yes / N/A + reason) | |
| Surface exercised (protocol §2 item) | |
| Pass / fail | |
| What actually happened | |
| Focus before | |
| Focus after | |
| Defect note (and severity: blocker / major / minor) | |

---

## Console errors

| Step that triggered it | Exact console message |
| --- | --- |
| | |

## Confirmed defects

Transcribe into protocol §5's "Confirmed defects" table without reinterpretation.

| Defect | Step(s) it violates | Reproduction (exact steps to reproduce) | Severity (blocker / major / minor) |
| --- | --- | --- | --- |
| | | | |

## Conflicts found

If an expected outcome above turned out to be unclear, unimplemented, or in
tension with the application's actual contract, protocol §6 says what to do
with it — record it for owner review in the protocol's own Conflicts log rather
than loosening the expectation here. Note it below so it is not lost.

| Step | What the tension is | Raised with owner (date) |
| --- | --- | --- |
| | | |

## Sign-off

| Role | Name | Date | Signature/approval |
| --- | --- | --- | --- |
| Reviewer (the person who ran this pass) | | | |
| Repository owner | | | |
