# Screen-reader script — S1 to S14, runnable

Part of the [#345 human-validation facilitator pack](README.md). **This sheet is
blank instrumentation. Nothing in it is evidence of a performed pass.**

## An automated browser journey is not a screen-reader session

This is the protocol's boundary, in its own words. Protocol §4's preamble:

> Run with a real screen reader (VoiceOver, NVDA, or JAWS — record which, per
> §1.2). For each item, record the announcement heard **verbatim**, not a
> paraphrase of what it should have said.

Protocol "Authority", quoting `operator-ux-requirements.md` §7's own gate:

> *"A manual keyboard-only and screen-reader pass on the Home/Tasks hub and guided Setup hub is required before #345 is considered done... this is what catches everything the automated gate structurally cannot."*

Protocol §6, first rule:

> - Do not fabricate results, recordings, announcements, or sign-offs. Every
>   field in §5's template must reflect something actually observed in a real
>   browser/screen-reader session, or be explicitly marked not yet run.

A Playwright/axe run, a DOM inspection, or a reading of `app.js` can not fill
in the "Announcement heard" column below. Only a real screen reader, driven by
a person, can.

Source of every quoted step and expected announcement:
`docs/product/manual-keyboard-screenreader-validation-protocol.md` §4, read at
`origin/main` = `36195faadb5c97936022d8f3706af51181a6b64d`. Reproduced verbatim,
including the correction #394 made to S5.

**Source pin.** Everything this sheet quotes or references from a protocol was
read at `origin/main` = `36195faadb5c97936022d8f3706af51181a6b64d`, at these
exact document versions:

| Canonical document (current path) | git blob SHA-1 at the pinned commit |
| --- | --- |
| `docs/product/moderated-operator-validation-protocol.md` | `c99935885b3de9141cad9b575a43a3d4fd62e0b3` |
| `docs/product/manual-keyboard-screenreader-validation-protocol.md` | `738e6c096e5d95d671b211e3f3df21bf975d17cc` |

A protocol quotation is not like a code citation. If the protocol is corrected
and this pack is not, the pack goes on instructing a human to perform a step the
protocol has since fixed, and nothing can see it — which is precisely what the
pre-#394 K5/S5 inversion did for a week. So the blob SHA-1 is the enforcement
and the prose is not: `check_pack.py`'s `protocol-pin` check recomputes both
blobs and fails the moment either document changes by a single byte. When it
fails, re-verify every quotation in this pack against the new text *before*
advancing the pin.

Before you start, bring the environment up and complete the pre-flight in
[01-environment-league-admin.md](01-environment-league-admin.md) (or the
Arena Manager / Coach sheet, if the surface under test needs that role).

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

### S1

**Step (protocol §4, S1 — verbatim):**

> Load the signed-out login page.

**Expected announcement (protocol §4, S1 — verbatim, the protocol's own column):**

> The browser tab title (read by the screen reader on page focus / via a "read title" command) announces a **Sign in**-specific title, not a stale or generic one — per `setShellTitle("login", ...)`.

| Field | Value |
| --- | --- |
| Applies to this pass? (yes / N/A + reason) | |
| Surface exercised (protocol §2 item) | |
| Pass / fail | |
| What actually happened | |
| Focus before | |
| Focus after | |
| Announcement heard (verbatim — not a paraphrase) | |
| Defect note (and severity: blocker / major / minor) | |

### S2

**Step (protocol §4, S2 — verbatim):**

> Navigate to the Public Schedule (anonymous portal), then use the control that returns to Staff sign-in.

**Expected announcement (protocol §4, S2 — verbatim, the protocol's own column):**

> The title announces **Public Schedule** while on that surface, and announces the sign-in-specific title again once back on the sign-in card — both transitions update the announced title, not just the sign-in→public leg.

| Field | Value |
| --- | --- |
| Applies to this pass? (yes / N/A + reason) | |
| Surface exercised (protocol §2 item) | |
| Pass / fail | |
| What actually happened | |
| Focus before | |
| Focus after | |
| Announcement heard (verbatim — not a paraphrase) | |
| Defect note (and severity: blocker / major / minor) | |

### S3

**Step (protocol §4, S3 — verbatim):**

> Sign in and land on Home/Tasks.

**Expected announcement (protocol §4, S3 — verbatim, the protocol's own column):**

> The title announces the authenticated destination's `<NAV label> — Hockey Scheduler` form (per `setShellTitle("app", ...)`), and the screen reader's landmark navigation (e.g. VoiceOver's rotor, NVDA's elements list) surfaces at least a `main`/`nav` landmark structure with a sensible heading hierarchy (one clear top-level heading per view, not multiple or none).

| Field | Value |
| --- | --- |
| Applies to this pass? (yes / N/A + reason) | |
| Surface exercised (protocol §2 item) | |
| Pass / fail | |
| What actually happened | |
| Focus before | |
| Focus after | |
| Announcement heard (verbatim — not a paraphrase) | |
| Defect note (and severity: blocker / major / minor) | |

### S4

**Step (protocol §4, S4 — verbatim):**

> Navigate the sidebar to a different top-level area.

**Expected announcement (protocol §4, S4 — verbatim, the protocol's own column):**

> The current navigation destination is announced as selected/current (e.g. `aria-current` or equivalent), so a screen-reader user can tell which area they're in without relying on sight.

| Field | Value |
| --- | --- |
| Applies to this pass? (yes / N/A + reason) | |
| Surface exercised (protocol §2 item) | |
| Pass / fail | |
| What actually happened | |
| Focus before | |
| Focus after | |
| Announcement heard (verbatim — not a paraphrase) | |
| Defect note (and severity: blocker / major / minor) | |

### S5

**Step (protocol §4, S5 — verbatim):**

> Move to `#ctx-select`, then to `#ctx-league-select`, and change each.

**Expected announcement (protocol §4, S5 — verbatim, the protocol's own column):**

> On reaching each control the screen reader announces its **name**, role and value, then its **description** — `#ctx-scope-note`, wired as `aria-describedby` on both selects. Record all of it verbatim. On changing the selection, the newly selected option is announced (native `<select>` semantics — confirm this is preserved, since #345 requires keeping the native control specifically for this reason). Then confirm the underlying content genuinely re-filtered (K5's check), and record **how a non-sighted user learns that it did** — the repaint itself is silent. If nothing announces the content change, log it in the conflicts log as a real gap: it is a known limitation of this slice, not a passing result, and it must not be recorded as either "no change expected" or "announced".

| Field | Value |
| --- | --- |
| Applies to this pass? (yes / N/A + reason) | |
| Surface exercised (protocol §2 item) | |
| Pass / fail | |
| What actually happened | |
| Focus before | |
| Focus after | |
| Announcement heard (verbatim — not a paraphrase) | |
| Defect note (and severity: blocker / major / minor) | |

### S6

**Step (protocol §4, S6 — verbatim):**

> Open a drawer or modal.

**Expected announcement (protocol §4, S6 — verbatim, the protocol's own column):**

> The screen reader announces the dialog's role ("dialog"), its accessible name (the heading/`aria-label` the trigger implies), and that it is modal — confirming `role="dialog" aria-modal="true"` plus a real accessible name, not just "dialog" with no name.

| Field | Value |
| --- | --- |
| Applies to this pass? (yes / N/A + reason) | |
| Surface exercised (protocol §2 item) | |
| Pass / fail | |
| What actually happened | |
| Focus before | |
| Focus after | |
| Announcement heard (verbatim — not a paraphrase) | |
| Defect note (and severity: blocker / major / minor) | |

### S7

**Step (protocol §4, S7 — verbatim):**

> Within an open dialog, tab to a required form field, then submit with it empty.

**Expected announcement (protocol §4, S7 — verbatim, the protocol's own column):**

> The field's label, its required state, and the resulting validation error are all announced — not just visually shown. Check specifically that the error is announced once (not silently, not duplicated) and is associated with the specific field via a description/error-message relationship, not just proximate text.

| Field | Value |
| --- | --- |
| Applies to this pass? (yes / N/A + reason) | |
| Surface exercised (protocol §2 item) | |
| Pass / fail | |
| What actually happened | |
| Focus before | |
| Focus after | |
| Announcement heard (verbatim — not a paraphrase) | |
| Defect note (and severity: blocker / major / minor) | |

### S8

**Step (protocol §4, S8 — verbatim):**

> Trigger a loading state (e.g. a fresh navigation to a data-backed screen).

**Expected announcement (protocol §4, S8 — verbatim, the protocol's own column):**

> A loading/busy state is perceivable to a screen-reader user (e.g. an announced "Loading" status, or a live region that later announces the result) — it must not be silent from the screen reader's perspective even though sighted users see a skeleton.

| Field | Value |
| --- | --- |
| Applies to this pass? (yes / N/A + reason) | |
| Surface exercised (protocol §2 item) | |
| Pass / fail | |
| What actually happened | |
| Focus before | |
| Focus after | |
| Announcement heard (verbatim — not a paraphrase) | |
| Defect note (and severity: blocker / major / minor) | |

### S9

**Step (protocol §4, S9 — verbatim):**

> Trigger a success action (e.g. a create/submit that succeeds).

**Expected announcement (protocol §4, S9 — verbatim, the protocol's own column):**

> The toast/status channel (`#toast-root[aria-live="polite"]`) announces the success message once.

| Field | Value |
| --- | --- |
| Applies to this pass? (yes / N/A + reason) | |
| Surface exercised (protocol §2 item) | |
| Pass / fail | |
| What actually happened | |
| Focus before | |
| Focus after | |
| Announcement heard (verbatim — not a paraphrase) | |
| Defect note (and severity: blocker / major / minor) | |

### S10

**Step (protocol §4, S10 — verbatim):**

> Trigger an error on a per-card (Home/Tasks, Setup workflow landing) and separately on a whole-pane screen.

**Expected announcement (protocol §4, S10 — verbatim, the protocol's own column):**

> Each error is announced in a way that identifies *which* card failed (per-card) or that the page failed (whole-pane) — not a generic unlocalized "error" with no scope, and the retry control's label/purpose is announced clearly.

| Field | Value |
| --- | --- |
| Applies to this pass? (yes / N/A + reason) | |
| Surface exercised (protocol §2 item) | |
| Pass / fail | |
| What actually happened | |
| Focus before | |
| Focus after | |
| Announcement heard (verbatim — not a paraphrase) | |
| Defect note (and severity: blocker / major / minor) | |

### S11

**Step (protocol §4, S11 — verbatim):**

> Navigate to the "Imports and onboarding" workflow when workflows 1–5 are otherwise complete.

**Expected announcement (protocol §4, S11 — verbatim, the protocol's own column):**

> Its status is announced as distinctly **optional** — not as "done" and not as blocking/"todo" the way a required workflow would be — matching Decision 9's third-status contract. Record the exact wording heard.

| Field | Value |
| --- | --- |
| Applies to this pass? (yes / N/A + reason) | |
| Surface exercised (protocol §2 item) | |
| Pass / fail | |
| What actually happened | |
| Focus before | |
| Focus after | |
| Announcement heard (verbatim — not a paraphrase) | |
| Defect note (and severity: blocker / major / minor) | |

### S12

**Step (protocol §4, S12 — verbatim):**

> Navigate to a restricted/403 surface, or a control unavailable to the current role (e.g. Viewer attempting a mutation).

**Expected announcement (protocol §4, S12 — verbatim, the protocol's own column):**

> The restricted/unauthorized state is announced explicitly (a real status, e.g. "restricted" or equivalent wording) — not silently absent controls with no explanation, and not a generic error indistinguishable from a real failure.

| Field | Value |
| --- | --- |
| Applies to this pass? (yes / N/A + reason) | |
| Surface exercised (protocol §2 item) | |
| Pass / fail | |
| What actually happened | |
| Focus before | |
| Focus after | |
| Announcement heard (verbatim — not a paraphrase) | |
| Defect note (and severity: blocker / major / minor) | |

### S13

**Step (protocol §4, S13 — verbatim):**

> With a screen reader running, trigger two rapid state changes in succession (e.g. a stale-guarded context switch that supersedes an in-flight load, or two toast messages back to back).

**Expected announcement (protocol §4, S13 — verbatim, the protocol's own column):**

> Only the current, correct state is announced — no stale announcement from the superseded request, and no duplicate/overlapping announcement that garbles both messages. This is the live-region equivalent of the stale-response guard's visual behavior — confirm it holds for what's *announced*, not only what's rendered.

| Field | Value |
| --- | --- |
| Applies to this pass? (yes / N/A + reason) | |
| Surface exercised (protocol §2 item) | |
| Pass / fail | |
| What actually happened | |
| Focus before | |
| Focus after | |
| Announcement heard (verbatim — not a paraphrase) | |
| Defect note (and severity: blocker / major / minor) | |

### S14

**Step (protocol §4, S14 — verbatim):**

> Complete a full Setup workflow item (e.g. register a team) and return to Home/Tasks.

**Expected announcement (protocol §4, S14 — verbatim, the protocol's own column):**

> The now-complete task card's status is announced as **completed**, distinctly from its prior loading/todo announcement — confirm the change is perceivable via the screen reader, not just a visual checkmark.

| Field | Value |
| --- | --- |
| Applies to this pass? (yes / N/A + reason) | |
| Surface exercised (protocol §2 item) | |
| Pass / fail | |
| What actually happened | |
| Focus before | |
| Focus after | |
| Announcement heard (verbatim — not a paraphrase) | |
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

S5 names one outcome that has to be logged rather than scored — read its
expected-announcement text above again before deciding what to write. Anything
else that is unclear or in tension with the application's contract belongs in
the protocol's own Conflicts log per §6.

| Step | What the tension is | Raised with owner (date) |
| --- | --- | --- |
| | | |

## Sign-off

| Role | Name | Date | Signature/approval |
| --- | --- | --- | --- |
| Reviewer (the person who ran this pass) | | | |
| Repository owner | | | |
