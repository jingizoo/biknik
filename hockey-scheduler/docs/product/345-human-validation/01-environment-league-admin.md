# Environment 1 — League Admin

Part of the [#345 human-validation facilitator pack](README.md). **This sheet is
blank instrumentation. Nothing in it is evidence of a performed session.**

Used by: the moderated League Admin session (moderated protocol §2), and the
keyboard (§3) / screen-reader (§4) passes, which run as League Admin for every
surface that needs operator permissions.

Everything below was exercised by the pack's author against `origin/main` =
`36195faadb5c97936022d8f3706af51181a6b64d` on 2026-08-05, purely to make the
bring-up reproducible. **Running it produced no session evidence and none is
recorded here.**

---

## A. Bring-up

Run every step yourself before the participant is in the room.

### A1. Start the application

From the repository root:

```bash
cd hockey-scheduler/backend
python3 -m hockey_scheduler.web --port 8000
```

Open `http://127.0.0.1:8000/` — the desktop Operator Console. Leave the terminal
running and visible; you will need it in A6.

Three properties of this server that decide how the rest of the session has to
be run:

1. **Demo mode boots to an empty installation.** On a cold boot the only account
   that exists is the League Admin persona and the app lands on the Initial
   Setup wizard, not the Home/Tasks hub. The canonical dataset has to be loaded
   (A3) before anything in the scripts is reachable.
2. **Restarting the process destroys everything.** A demo-mode boot rebuilds the
   store from scratch, `DATABASE_URL` or not. If the backend restarts
   mid-session the fixture, the context selection and the participant's progress
   are all gone and this whole sheet has to be re-run. Do not stop the server to
   produce an error state — use A6 instead.
3. **The active Program/Season/League selection is per account.** Selecting a
   context as `admin` does not select it for `arena` or `coach`. Each
   environment sheet sets its own.

### A2. Record the build before you touch anything

`git rev-parse HEAD` on the checkout you just started, into section C1. The
moderated protocol §1.1 states what that value has to satisfy:

> | Git head SHA under test | *(fill in — `git rev-parse HEAD` on the exact checkout the session runs against; must match the PR head being validated for #345)* |

### A3. Load the canonical dataset

Sign in first (A4 explains why there is no password to type), then use the
database-icon menu in the header:

- On an empty installation the menu offers **Load demo data** only. Choose it.
- **Reload the browser page afterwards.** The dataset is built server-side
  immediately, but the signed-in page does not fully re-derive itself from it —
  after a reload the sign-in card offers all seven personas and the console
  lands on the Dashboard.

Once the dataset exists the same menu offers **Reset demo data** and **Clear
demo data** instead.

### A4. Sign in as the persona — never as yourself, never with a typed password

The sign-in card lists the seeded demo accounts as buttons. Click **admin**. No
password is typed and none needs to be known. The keyboard/screen-reader
protocol §1.3 governs what may be written down:

> | Account / persona used | Use the app's existing seeded demo personas (`admin`/League Admin, `arena`/Arena Manager, `coach`/Coach, or `player`/`guardian`/`official`/`viewer` as the surface requires) per `backend/hockey_scheduler/web/auth.py`. **Do not record the password**, only which persona/role. |

The moderated protocol §1.4 says the same thing for a moderated session:

> | Starting account | Use the app's existing seeded demo personas where the session runs against demo data: `admin` (League Admin), `arena` (Arena Manager), `coach` (Coach) — shared demo password per `backend/hockey_scheduler/web/auth.py`. **Do not write the actual password into session evidence**; record only which persona/role was used. |

**Reload the page once after signing in**, and confirm what you land on (C5).
The view the console settles on can differ between a fresh sign-in and a
reloaded, already-signed-in page; the participant must start from the same
place you verified.

### A5. Reset between participants

The reset procedure is the moderated protocol §1.5. Run it in full — it is five
steps, not one — before every participant. Its first step:

> 1. From the header menu, run **Reset** (type `RESET` to confirm) — this
>    calls `POST /api/demo/reset` and rebuilds the canonical demo dataset from
>    scratch, matching the existing `e2e/demo-lifecycle.js` reset journey.
>    Confirm the league tree is non-empty and the header again reads its
>    pre-reset state before proceeding.

After the reset completes, check whether you are still signed in. A reset
rebuilds the accounts, and depending on the store the session may or may not
survive it; if the app returns to the sign-in card, or an action reports that
the session expired, sign in again as the persona (A4) before continuing.

Then §1.5's second step is what section B below exists to satisfy:

> 2. Re-apply whatever fixture deviates from the canonical demo dataset that
>    this session's script needs (e.g. the League Admin script's requirement
>    that a specific workflow starts incomplete) and record exactly what was
>    changed in §1.4 above for that participant's copy of this template.

### A6. Have the error-triggering condition ready (do not fire it yet)

The keyboard/screen-reader protocol §1.4 requires the fixture to make an error
state actually occur. Produce it with the browser's own request blocking, which
leaves the dataset intact:

- **Per-card error** (Home/Tasks setup-progress card, whole rest of the page
  still loading): block `/api/v2/setup/progress`, then navigate away from the
  Dashboard and back. In Chrome DevTools: **Network** → right-click the request
  → **Block request URL**. In Firefox's Network Monitor the item is **Block
  URL**. Un-block it and use the card's own retry control to recover.
- **Whole-pane error**: block `/api/demo/overview` and switch to a data-backed
  screen the same way, then un-block and retry.
- Do **not** stop the backend process to force this. It works, and it also
  destroys the fixture (A1, point 2).

Keep the blocking rule prepared but disabled until the step that needs it. A
moderated operator session should normally never see it — it belongs to the
keyboard/screen-reader pass (protocol §2 surface 7).

---

## B. Fixture deviation this environment needs

The canonical dataset is a **finished** league: on it the Home/Tasks
setup-progress card reports every required workflow done. The League Admin
script cannot run on that.

Moderated protocol §2, precondition:

> **Precondition**: starting account is the League Admin persona, landing on
> the normal landing page (Home/Tasks hub), with at least one Setup workflow
> genuinely incomplete (§1.4).

So create one, using nothing but the app's own operator controls:

1. Signed in as `admin`, open the **Setup** area (Administration → Setup), tab
   **Workflows**.
2. Choose **Open league profile and seasons**.
3. Use the landing's primary action, **Add Season**.
4. In the **New season** drawer, fill in **Season name** only — leave the dates
   blank and leave the pre-filled Program as it is. Submit (**Create season**).
   Suggested name: `Validation Season` plus the session date, so the deviation
   is self-documenting.
5. In the context bar, select that new Season.

That is the entire deviation: one Season, added through the UI, then selected.
Record it verbatim in the evidence template's fixture field (C6).

Two notes on why this shape:

- A newly created Season has no teams registered, no ice granted and no league
  binding yet, so several required workflows are genuinely — not artificially —
  incomplete under it. Nothing is faked, and the participant is looking at real
  application state.
- The canonical Season is left untouched and still selectable, so switching
  context back is a one-control undo.

---

## C. Pre-flight checklist

Complete every line before the participant arrives. A precondition you assumed
rather than checked is how a session produces unusable evidence.

Judge each check by the **state**, not by matching a sentence. Where a check
quotes application copy, that copy is what the author saw at
`36195faadb5c97936022d8f3706af51181a6b64d` and it may legitimately change; if
the wording differs but the state is the one described, the check passes —
note the difference in C11. (Tying a manual check to a literal sentence is the
exact defect the keyboard/screen-reader protocol's 2026-08-04 conflicts-log
entry records for K5/S5.)

| # | Check | Result |
| --- | --- | --- |
| C1 | The head under test is deployed here and its SHA is recorded (not inferred from a branch name). SHA: ______________________ | ☐ PASS ☐ FAIL |
| C2 | CI required for #345 is green on that exact head (moderated protocol §7, item 2). | ☐ PASS ☐ FAIL |
| C3 | Reset run in full per moderated protocol §1.5, and you are signed in as `admin` afterwards. | ☐ PASS ☐ FAIL |
| C4 | Signed in by clicking the **admin** persona; no password recorded anywhere. | ☐ PASS ☐ FAIL |
| C5 | Landing screen is the Home/Tasks hub (Dashboard), not the Initial Setup wizard. If it is the wizard, the dataset was not loaded — go back to A3. | ☐ PASS ☐ FAIL |
| C6 | Active context recorded exactly as the participant will see it. Program: ____________ Season: ____________ League: ____________ | ☐ PASS ☐ FAIL |
| C7 | **§1.4 condition 1 — an incomplete Setup workflow exists and is visible.** On the Dashboard, the setup-progress card is in its "continue" state and at least one workflow row is badged as not done (observed wording: card heading "Continue setup"; rows badged "To do"). Count of not-done rows: ____ | ☐ PASS ☐ FAIL |
| C8 | **§1.4 condition 2 — a restricted/403 case is reachable.** League Admin holds every operator permission, so nothing in this console is restricted *to this role*; the reachable case uses another seeded persona, which protocol §1.3 explicitly allows ("or `player`/`guardian`/`official`/`viewer` as the surface requires"). Verified path: sign in as `coach`, open Games, expand a game that does not involve the coach's own team, choose **Open Roster** — the screen reports the access refusal instead of the roster (observed heading: "Restricted"). Sign back in as `admin` afterwards. Which persona/path did you use? ____________________ | ☐ PASS ☐ FAIL |
| C9 | **§1.4 condition 3 — an error-triggering condition is available.** The request-blocking rule from A6 is prepared and you have confirmed you can enable and disable it. Do not leave it enabled. | ☐ PASS ☐ FAIL |
| C10 | For a keyboard or screen-reader pass only: the assistive-technology setup is confirmed working *before* the session (moderated protocol §7, item 5). Screen reader + version: ____________________ | ☐ PASS ☐ FAIL / ☐ N/A |
| C11 | Any wording that differed from the observed copy quoted above, noted here: ______________________________________________ | ☐ done |
| C12 | The participant has not seen the task script or either protocol before the session (moderated protocol §7, item 4). | ☐ PASS ☐ FAIL |
| C13 | A plan exists for where results will be attached to #345 (moderated protocol §7, item 6). | ☐ PASS ☐ FAIL |

Moderated protocol §7 is explicit about what a partial checklist means:

> Confirm every item below before inviting a participant. Do not proceed on a
> partial checklist — an incomplete readiness check invalidates the session's
> evidentiary value for #345's merge gate.

---

## D. What to carry into the evidence template

Copy into the session's own copy of moderated protocol §6 (or, for a
mechanics pass, keyboard/screen-reader protocol §5):

- Build: SHA from C1; backend datastore in use; deployment target.
- Starting account: persona name only.
- Active context: the three values from C6.
- Fixture/data state: "canonical demo dataset (Load/Reset), plus one Season
  added through the UI and selected — `<name you used>`", and the not-done
  workflow count from C7.
- Which §1.4 conditions were verified, how, and any that were not (with the
  reason) — a condition that was not verified must not be written as verified.

---

## E. Between participants

1. Sign the participant out completely; do not reuse an open session
   (moderated protocol §1.5, step 5).
2. Re-run A5 (reset), then B (deviation), then the whole of C again for the next
   participant. §1.5 step 3 also requires re-confirming the SHA has not changed;
   if it has, this is a new session build and the readiness checklist restarts.
3. Recording, if any, is stored outside this repository — moderated protocol
   §1.6 covers this and nothing in this pack overrides it.
