# Environment 3 — Coach

Part of the [#345 human-validation facilitator pack](README.md). **This sheet is
blank instrumentation. Nothing in it is evidence of a performed session.**

Used by: the moderated Coach session (moderated protocol §4), and any keyboard
(§3) / screen-reader (§4) step that has to be exercised under a team-scoped,
non-operator role.

Everything below was exercised by the pack's author against `origin/main` =
`36195faadb5c97936022d8f3706af51181a6b64d` on 2026-08-05, purely to make the
bring-up reproducible. **Running it produced no session evidence and none is
recorded here.**

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

---

## A. Bring-up

### A1. Start the application

```bash
cd hockey-scheduler/backend
python3 -m hockey_scheduler.web --port 8000
```

Open `http://127.0.0.1:8000/`. Leave the terminal running.

1. **Demo mode boots to an empty installation** — only the League Admin persona
   exists until the canonical dataset is loaded (A3). The `coach` persona does
   not exist before that.
2. **Restarting the process destroys everything**, `DATABASE_URL` or not. Never
   restart mid-session; never stop the server to produce an error state (A6).
3. **The active Program/Season/League selection is per account** — set it while
   signed in as `coach` (A7). A Coach also sees a narrower set of options than
   an operator does, because the account is scoped to one team.

### A2. Record the build

`git rev-parse HEAD`, into C1. Moderated protocol §1.1:

> | Git head SHA under test | *(fill in — `git rev-parse HEAD` on the exact checkout the session runs against; must match the PR head being validated for #345)* |

### A3. Load the canonical dataset

Sign in as `admin` (the only persona on an empty installation), open the
database-icon menu in the header, choose **Load demo data**, then **reload the
browser page**. After the reload all seven personas appear on the sign-in card.

### A4. Apply the fixture deviation — as `admin`, before the participant arrives

See section B. It is invisible to the Coach but is what makes the environment
satisfy the keyboard/screen-reader protocol §1.4 fixture conditions.

### A5. Reset between participants

Moderated protocol §1.5 is the reset procedure, and all five of its steps run
before every participant. Here it is in full, verbatim, so you never have to
leave this sheet mid-session:

> Run this between every participant so no session inherits state, partial
> progress, or artifacts from the prior one:
>
> 1. From the header menu, run **Reset** (type `RESET` to confirm) — this
>    calls `POST /api/demo/reset` and rebuilds the canonical demo dataset from
>    scratch, matching the existing `e2e/demo-lifecycle.js` reset journey.
>    Confirm the league tree is non-empty and the header again reads its
>    pre-reset state before proceeding.
> 2. Re-apply whatever fixture deviates from the canonical demo dataset that
>    this session's script needs (e.g. the League Admin script's requirement
>    that a specific workflow starts incomplete) and record exactly what was
>    changed in §1.4 above for that participant's copy of this template.
> 3. Re-confirm the git head SHA (§1.1) has not changed since the previous
>    participant; if it has, treat this as a new session build and restart
>    the readiness checklist.
> 4. If the session runs against a non-demo (staging/production-like)
>    environment, use the production factory-reset flow instead
>    (`e2e/factory-reset.js` covers its safety gating) — never reuse a
>    participant's mutated state for the next participant.
> 5. Sign the participant out completely before the next participant signs
>    in; do not reuse an open session/token.

How that lands on this environment:

- **Step 1.** Check afterwards whether you are still signed in, and sign in
  again if the app has returned to the sign-in card.
- **Step 2** is section B. Reset first, deviation second — a reset discards the
  deviation. B is applied as `admin`, then you sign back in as `coach` (A7).
- **Step 3** is C1: re-record the SHA, do not assume it.
- **Step 4** applies only if you are not on the demo server this sheet brings
  up; on a non-demo environment, run the production factory-reset flow instead
  of the header-menu Reset in step 1.
- **Step 5** is section E, and it comes first in wall-clock order: sign the
  outgoing participant out before you reset anything.

### A6. Have the error-triggering condition ready (do not fire it yet)

Browser request blocking, which leaves the dataset intact: block
`/api/v2/setup/progress` for a per-card error, `/api/demo/overview` for a
whole-pane one, then un-block and use the on-screen retry control. Chrome
DevTools: **Network** → right-click the request → **Block request URL**;
Firefox's Network Monitor: **Block URL**. Do not stop the backend — it works and
it also destroys the fixture (A1, point 2).

### A7. Sign in as the persona and set its context

On the sign-in card, click **coach**. No password is typed and none is recorded.
Keyboard/screen-reader protocol §1.3:

> | Account / persona used | Use the app's existing seeded demo personas (`admin`/League Admin, `arena`/Arena Manager, `coach`/Coach, or `player`/`guardian`/`official`/`viewer` as the surface requires) per `backend/hockey_scheduler/web/auth.py`. **Do not record the password**, only which persona/role. |

Then, still as `coach`:

1. **Reload the page once** after signing in, and record the view you land on
   (C5) — the participant must start from the same place you verified.
2. In the context bar, select the Program and the canonical Season (observed:
   "Alpine Ice Hockey League" / "2026–27 Winter Season"). A Coach's Season list
   is narrower than an operator's; if the Season you expect is missing, that is
   the scope enforcement working, not a fault.
3. Note which team this account is bound to (C7) — moderated protocol §4's
   precondition is a single-team scope:

> **Precondition**: starting account is the Coach persona, scoped to a single
> team per the existing Coach scope enforcement, landing on the normal
> landing page.

---

## B. Fixture deviation this environment needs

The Coach script itself runs on the canonical dataset — the seeded team has
games, a roster and a next fixture, which is everything §4 asks for. The
deviation exists only to satisfy the keyboard/screen-reader protocol §1.4
fixture conditions for the installation:

> | Fixture / data state | *(fill in — note specifically whether the fixture includes at least one incomplete Setup workflow, at least one restricted/403 case, and at least one error-triggering condition, since surfaces 5 and 7 need those states to actually occur, not just be described)* |

Signed in as `admin` (not `coach`):

1. Open the **Setup** area (Administration → Setup), tab **Workflows**.
2. Choose **Open league profile and seasons**, then **Add Season**.
3. Fill in **Season name** only; leave dates blank and the pre-filled Program as
   it is. Submit (**Create season**). **Name it as a real next season would be
   named** — e.g. `2027–28 Winter Season`, one on from the canonical `2026–27
   Winter Season`. It should not appear in the Coach's context list at all
   (confirm that at C8), but the naming rule is the same across all three
   environments and a label announcing test scaffolding has no place on screen.
4. Sign out, sign in as `coach`, and set the context per A7.

Be honest about what this buys on this environment: the incomplete workflow now
exists in the fixture, but **a Coach cannot see any Setup workflow at all** —
the role has no setup surface, and the added Season does not appear in this
account's context list either. Record that as observed (C8) rather than
recording condition 1 as visible.

---

## C. Pre-flight checklist

Complete every line before the participant arrives. A precondition you assumed
rather than checked is how a session produces unusable evidence.

Judge each check by the **state**, not by matching a sentence. Quoted
application copy is what the author observed at
`36195faadb5c97936022d8f3706af51181a6b64d`; if the wording differs but the
state is the one described, the check passes — note the difference in C12.

| # | Check | Result |
| --- | --- | --- |
| C1 | The head under test is deployed here and its SHA is recorded (not inferred from a branch name). SHA: ______________________ | ☐ PASS ☐ FAIL |
| C2 | CI required for #345 is green on that exact head (moderated protocol §7, item 2). | ☐ PASS ☐ FAIL |
| C3 | Reset run in full per moderated protocol §1.5, then the B deviation applied, in that order. | ☐ PASS ☐ FAIL |
| C4 | Signed in by clicking the **coach** persona; no password recorded anywhere. | ☐ PASS ☐ FAIL |
| C5 | Page reloaded once after sign-in, and the view the participant will land on recorded here: ____________________ | ☐ PASS ☐ FAIL |
| C6 | Active context recorded exactly as the participant will see it. Program: ____________ Season: ____________ League: ____________ | ☐ PASS ☐ FAIL |
| C7 | The account is scoped to exactly one team, and that team is named here: ____________________ (needed to judge C9 and the §4 task steps). | ☐ PASS ☐ FAIL |
| C8 | **§1.4 condition 1 — an incomplete Setup workflow exists in the fixture.** Verified while signed in as `admin` (the Home/Tasks setup-progress card in its "continue" state under the added Season, at least one row badged not-done). Also record the observed fact that this is **not visible to the Coach**: ☐ confirmed not visible to `coach` | ☐ PASS ☐ FAIL |
| C9 | **§1.4 condition 2 — a restricted/403 case is reachable for this role.** Native to the Coach: open Games, expand a game that does not involve the team from C7, choose **Open Roster** — the screen refuses access instead of showing the roster (observed heading: "Restricted"). Game used: ____________________ | ☐ PASS ☐ FAIL |
| C10 | **§1.4 condition 3 — an error-triggering condition is available.** The request-blocking rule from A6 is prepared and you have confirmed you can enable and disable it. Leave it disabled. | ☐ PASS ☐ FAIL |
| C11 | The task is completable on this environment: signed in as `coach`, both the roster and the next-game work for the C7 team are reachable through normal navigation (the facilitator walks it once; the participant must not watch). | ☐ PASS ☐ FAIL |
| C12 | For a keyboard or screen-reader pass only: assistive-technology setup confirmed working before the session (moderated protocol §7, item 5). Screen reader + version: ____________ Any wording that differed from the copy quoted above: ____________________ | ☐ PASS ☐ FAIL / ☐ N/A |
| C13 | The participant has not seen the task script or either protocol before the session (moderated protocol §7, item 4). | ☐ PASS ☐ FAIL |
| C14 | A plan exists for where results will be attached to #345 (moderated protocol §7, item 6). | ☐ PASS ☐ FAIL |

Moderated protocol §7:

> Confirm every item below before inviting a participant. Do not proceed on a
> partial checklist — an incomplete readiness check invalidates the session's
> evidentiary value for #345's merge gate.

---

## D. What to carry into the evidence template

Into the session's copy of moderated protocol §6:

- Build: SHA from C1; backend datastore; deployment target.
- Starting account: persona name only; the team scope from C7.
- Active context: the three values from C6.
- Fixture/data state: "canonical demo dataset (Load/Reset), plus one Season
  added through the UI as League Admin — `<name you used>`; not visible to the
  Coach persona".
- Which §1.4 conditions were verified and how, including the honest note from
  C8. A condition that was not verified must not be written as verified.

---

## E. Between participants

1. Sign the participant out completely; do not reuse an open session
   (moderated protocol §1.5, step 5).
2. Re-run A5 (reset), then B (deviation), then all of C. §1.5 step 3 also
   requires re-confirming the SHA has not changed; if it has, this is a new
   session build and the readiness checklist restarts.
3. Recording, if any, is stored outside this repository — moderated protocol
   §1.6 covers this and nothing in this pack overrides it.
