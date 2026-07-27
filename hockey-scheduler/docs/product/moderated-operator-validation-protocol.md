# Moderated Operator-Validation Session Protocol

## Status

**Protocol and evidence templates only. No moderated session has been run
under this document.** This deliverable prepares the repeatable procedure
and blank evidence artifacts required by [#345][issue-345]; it does not
itself constitute session evidence, and nothing in this document should be
read as a completion claim for any of the three sessions.

This document adds no application code, tests, CI configuration, or product
behavior. It is documentation only, filed alongside the rest of the
guided-Setup / IA / accessibility requirements work in `docs/product/`.

[issue-345]: https://github.com/jingizoo/biknik/issues/345

## Authority

This protocol implements — and invents nothing beyond — the following
already-approved sources:

- **[#345][issue-345]**, "Role journeys and human validation": *"Commission,
  run, and document three moderated sessions—League Admin, Arena Manager,
  and Coach—including task completion, time, interventions, ease rating, and
  confusion quotes. These sessions are transferred here as a merge gate;
  they are not waived or simulated."* Also #345's per-role task framing
  (League Admin: identify and enter the next incomplete setup task without
  understanding the data model; Arena Manager: reach recurring ice creation
  through Facilities/Setup with only authorized primary actions; Coach:
  reach roster and next-game work through Home/Tasks and Schedule) and the
  acceptance-criteria checklist item *"All three moderated
  operator-validation sessions are completed and documented."*
- **[`docs/product/operator-ux-requirements.md`](operator-ux-requirements.md),
  §8 "Operator validation and measurable success criteria"** — owner
  (@jingizoo), three sessions minimum (League Admin, Arena Manager, Coach),
  ~30–45 minutes each, evidence set (completion, time-on-task, moderator
  interventions, 1–5 ease rating, verbatim confusion quotes), and Decision 6
  (signed off 2026-07-27), which moved the session milestone from before
  PR #331 to before #345's implementation PR merges — *"the sessions are not
  waived or simulated."*
- **#345's Required Implementation → "Keep one primary action per screen"**
  and the per-screen primary-action table in §4 of the requirements
  package — the basis for the League Admin task's "explain what they
  believe the primary action will do" step, and for treating "reach the
  authorized primary action" (not necessarily complete a multi-step write)
  as a valid Arena Manager stopping point.

### Reconciling the two source documents — no conflict found

The requirements package's §8 draft (2026-07-24) describes the Arena
Manager task as completing a full ice-availability write ("add a week of
recurring ice via the Ice Availability Builder"). #345's own body, written
after the owner's 2026-07-27 scope split, narrows the *validated* role
journey to *"reach recurring ice creation through Facilities/Setup with
only authorized primary actions"* — i.e., reaching and correctly
identifying the primary action, not necessarily completing the write. This
protocol follows #345's body as the more current, binding text per this
task's instruction to treat #345 as authority, and records the difference
here rather than silently picking one. The two are not in tension: reaching
the primary action is a strict subset of completing the full flow, so a
moderator may let a participant continue through completion if they reach it
naturally, but the pass/fail bar in this protocol is "reached the
authorized primary action," matching #345's own text.

**Owner-confirmed (2026-07-27):** the repository owner confirmed "reach the
primary action" as the intended Arena Manager task scope, matching #345's
body and this protocol's default. The §8 draft's fuller "complete the
write" task is superseded for the purpose of this validation session.

No other conflicts between #345 and the requirements package were found for
this protocol. If a future reviewer finds one, record it in the
[Conflicts log](#conflicts-log) below rather than resolving it silently.

### Conflicts log

| Date found | Section | Description | Resolution |
| --- | --- | --- | --- |
| 2026-07-27 | Arena Manager task scope | §8 draft asks for a completed write; #345 body asks only to reach the primary action | **Resolved 2026-07-27** — repository owner confirmed "reach the primary action" per #345's body; §8's fuller task does not apply to this session |

---

## 1. Shared session setup

Complete this section identically for every session, before the
participant is invited in. One copy per session — use the
[evidence template](#6-evidence-template) below, which repeats these
fields.

### 1.1 Build identification

| Field | Value |
| --- | --- |
| Application version / release tag | *(fill in)* |
| Git head SHA under test | *(fill in — `git rev-parse HEAD` on the exact checkout the session runs against; must match the PR head being validated for #345)* |
| Backend datastore under test | Memory / SQLite / PostgreSQL *(circle one; #345 requires parity evidence across all three, but a single moderated session only needs one — record which)* |
| Deployment target | local dev server / staging / other *(fill in)* |

### 1.2 Environment fields

| Field | Value |
| --- | --- |
| Operating system + version | *(fill in)* |
| Browser + version | *(fill in — record the participant's actual browser; #345's automated CI gate runs Chromium only, but moderated sessions should reflect what real operators use, which may differ)* |
| Viewport / device | Desktop, or phone at the canonical **390×844**, or a named physical device *(fill in exact width×height in px if not one of the four approved breakpoint tokens — 480 / 720 / 880 / 1040 — per #345)* |
| Assistive technology in use, if any | Screen reader (name + version) / keyboard-only / voice control / none *(fill in — required for any session standing in as the manual keyboard-only or screen-reader evidence #345 also requires; note if this session is not intended to satisfy that evidence)* |

### 1.3 Participant

| Field | Value |
| --- | --- |
| Session role under test | League Admin / Arena Manager / Coach *(one per session — #345 requires all three)* |
| Participant's real-world experience level with this app | New / some exposure / regular user *(fill in — do not record name or other identifying detail beyond what consent in §1.6 permits)* |
| Participant's real-world familiarity with the underlying role (league admin work, rink/ice scheduling, coaching) outside this app | *(fill in)* |

### 1.4 Starting state

| Field | Value |
| --- | --- |
| Starting account | Use the app's existing seeded demo personas where the session runs against demo data: `admin` (League Admin), `arena` (Arena Manager), `coach` (Coach) — shared demo password per `backend/hockey_scheduler/web/auth.py`. **Do not write the actual password into session evidence**; record only which persona/role was used. |
| Permissions active at session start | Confirm the account's permission set matches the role table in `docs/product/operator-ux-requirements.md` (§"Roles today") — e.g. Arena Manager should hold exactly `manage_arena`, `manage_schedule`, `view`, nothing beyond it. Record any deviation. |
| Program / Season / League context active at session start | *(fill in the exact context bar state — Program, Season, and, where applicable, League — the participant sees on landing)* |
| Fixture / data state | *(fill in — which Program(s)/Season(s)/League(s)/Divisions/Teams exist, and specifically whether the task's target workflow starts incomplete/empty as the script requires — e.g. the League Admin script requires at least one genuinely incomplete Setup workflow to exist)* |

### 1.5 Reset procedure between participants

Run this between every participant so no session inherits state, partial
progress, or artifacts from the prior one:

1. From the header menu, run **Reset** (type `RESET` to confirm) — this
   calls `POST /api/demo/reset` and rebuilds the canonical demo dataset from
   scratch, matching the existing `e2e/demo-lifecycle.js` reset journey.
   Confirm the league tree is non-empty and the header again reads its
   pre-reset state before proceeding.
2. Re-apply whatever fixture deviates from the canonical demo dataset that
   this session's script needs (e.g. the League Admin script's requirement
   that a specific workflow starts incomplete) and record exactly what was
   changed in §1.4 above for that participant's copy of this template.
3. Re-confirm the git head SHA (§1.1) has not changed since the previous
   participant; if it has, treat this as a new session build and restart
   the readiness checklist.
4. If the session runs against a non-demo (staging/production-like)
   environment, use the production factory-reset flow instead
   (`e2e/factory-reset.js` covers its safety gating) — never reuse a
   participant's mutated state for the next participant.
5. Sign the participant out completely before the next participant signs
   in; do not reuse an open session/token.

### 1.6 Consent and privacy guidance

- Obtain the participant's verbal or written consent before starting,
  covering: that the session is being observed/recorded (per the
  moderator's choice — recording is optional per the requirements package),
  that anonymized task performance and direct quotes may be documented, and
  that they may stop at any time.
- **Do not record**: the participant's real login credentials (use only the
  seeded demo personas above, or a scoped non-production test account —
  never a real operator's live password), unnecessary personal information
  (do not capture full name, contact details, employer, or anything beyond
  what's needed to interpret the result — e.g. "an Arena Manager with ~2
  years' rink-scheduling experience" is sufficient; a name is not needed).
- If recording audio/video, store it outside version control and outside
  this repository; only the anonymized written evidence (§6) is intended to
  be attached to #345.
- If a participant discloses anything unrelated to the task (unrelated
  personal or business information), do not transcribe it into the
  evidence document.

### 1.7 Moderator instructions

- Read the task prompt for the relevant script (§§2–4) verbatim, or as
  close to verbatim as natural delivery allows. Do not paraphrase in a way
  that adds hints not in the script.
- **Do not lead the participant toward the answer.** Concretely:
  - Do not name the screen, menu, or control the participant should use.
  - If the participant asks "where do I click," respond with something
    neutral like "wherever you think you need to look" — do not narrow
    their search.
  - Do not react (verbally or visibly) differently to a correct vs. incorrect
    click; a moderator's tone or hesitation is itself a leading cue.
  - A moderator *may* offer a **defined intervention** only after the
    participant is stuck long enough to be at risk of abandoning the task
    (use your judgment, but pick a consistent threshold across all three
    sessions, e.g. ~60 seconds of no forward progress) — and every
    intervention, however small, must be logged in §6 with what was said.
  - Do not correct a wrong destination or a misstated understanding of what
    a primary action does until after the participant has committed to an
    answer for that step; corrections belong to the practitioner debrief
    after the session, not mid-task.
- Time the task from the moment the prompt finishes being read to the
  moment the participant either completes it or the moderator ends it.
- Capture confusion quotes verbatim (their exact words), not a
  paraphrase or your interpretation of what they meant.

---

## 2. League Admin script

**Precondition**: starting account is the League Admin persona, landing on
the normal landing page (Home/Tasks hub), with at least one Setup workflow
genuinely incomplete (§1.4).

Ask the participant, in order:

1. **"Looking at this screen, what Setup work do you think should happen
   next?"** — without explaining the underlying Program/Season/League/
   Division data model. Record what they say before they click anything.
2. **"Go ahead and open that."** — observe whether they open the correct
   incomplete workflow, or a different one.
3. **"Before you do anything else — what do you think the primary button
   on this screen will do if you click it?"** — record their stated
   expectation before they click, then let them click and observe whether
   the actual result matches.

### Record

- Task completion (yes/no) for "identified and opened the correct
  incomplete workflow."
- Elapsed time from prompt to opening the workflow.
- Number and description of moderator interventions.
- **Wrong destinations**: any screen opened before the correct one, named
  exactly.
- Ease rating (§6 scale).
- Verbatim confusion quotes.
- Whether the participant's stated expectation of the primary action
  matched its actual effect (yes/no), with their exact words for both the
  expectation and, if different, their reaction to the actual result.

---

## 3. Arena Manager script

**Precondition**: starting account is the Arena Manager persona, landing on
the normal landing page, with the active Program/Season/League context set
per §1.4.

Ask the participant, in order:

1. **"Find where you'd set up recurring ice."** — through the normal
   navigation only (no search, no help text, no hints). Observe the path
   taken.
2. **"Before you go further — what Program/Season/League is currently
   active, and how do you know?"** — record their answer and whether it
   matches the actual active context shown in the context bar.
3. **"Now reach the action you'd use to actually create that recurring
   ice."** — the pass condition is reaching the authorized primary action
   (see [Reconciling the two source documents](#reconciling-the-two-source-documents--no-conflict-found)
   above for why this protocol stops at "reach," not "complete"), **without
   entering any unrelated Administration area** along the way.

### Record

- Task completion (yes/no) for "reached the authorized primary action
  without detouring into unrelated Administration areas."
- Elapsed time.
- Number and description of moderator interventions.
- Wrong destinations, specifically flagging any excursion into
  Administration that was not part of the authorized path.
- Ease rating (§6 scale).
- Verbatim confusion quotes.
- **Authorization confusion**: any moment the participant expected to be
  able to do something the Arena Manager role does not permit, or expected
  a control to be hidden/disabled that wasn't (or vice versa).
- **Terminology confusion**: any label, menu name, or field name the
  participant misread, mispronounced, or asked to have defined.

---

## 4. Coach script

**Precondition**: starting account is the Coach persona, scoped to a single
team per the existing Coach scope enforcement, landing on the normal
landing page.

Ask the participant, in order:

1. **"Find your roster."** — observe the path taken (through Home/Tasks,
   direct navigation, or elsewhere).
2. **"Now find the workflow for your next game."** — specifically through
   Home/Tasks and Schedule; note if the participant instead tries an
   unrelated area first.
3. **"Based on what you're looking at, what would you do next?"** — record
   their stated next action (not necessarily performed) and whether it
   matches a real, available action for the next game (e.g. filling an
   open roster slot, confirming availability).

### Record

- Task completion (yes/no) for "found roster" and (yes/no, separately) for
  "found the next-game workflow through Home/Tasks and Schedule."
- Elapsed time for each of the two finds.
- Number and description of moderator interventions.
- **Navigation ambiguity**: any point where the participant paused between
  two plausible destinations, named both.
- Ease rating (§6 scale).
- Verbatim confusion quotes.
- Whether the stated next action was a real, available action for that
  game (yes/no), with their exact words.

---

## 5. Common measurements reference

All three scripts record the same base set, defined once here so every
session uses identical definitions:

| Measurement | Definition |
| --- | --- |
| Completion | Yes/no against the specific pass condition stated in that script's task step — not a subjective "did fine" judgment. |
| Elapsed time | Seconds or minutes from the end of the moderator's prompt to task completion or moderator-ended abandonment. |
| Moderator interventions | A count, each with a one-line description of exactly what was said or shown. Zero is a valid and good result. |
| Ease rating | See the 1–5 scale in §6 — collected once per task, immediately after that task, before moving to the next. |
| Confusion quotes | Verbatim participant speech, in quotation marks, with enough surrounding context to interpret it. |

---

## 6. Evidence template

Copy this section once per session (one League Admin copy, one Arena
Manager copy, one Coach copy) and fill in every field. Do not leave a field
blank without an explicit "N/A" and a reason — a missing field reads as an
unrun check.

```markdown
### Moderated session evidence — <role> — <date>

**Build**
- Application version / release tag:
- Git head SHA:
- Backend datastore (Memory / SQLite / PostgreSQL):
- Deployment target:

**Environment**
- OS + version:
- Browser + version:
- Viewport / device:
- Assistive technology in use:

**Participant**
- Role under test:
- Experience level with this app:
- Real-world familiarity with the role:

**Starting state**
- Account/persona used (never the password):
- Permissions confirmed to match the role table (yes/no, note deviations):
- Active Program/Season/League context:
- Fixture/data state (what was seeded/modified for this session):

**Task-by-task results**

| Task step | Pass/fail | Completion time | # interventions | Navigation path taken | Ease (1–5) |
| --- | --- | --- | --- | --- | --- |
| <step 1> | | | | | |
| <step 2> | | | | | |
| <step 3> | | | | | |

**Ease rating scale** (record which anchor the participant's number maps to):
1 = Very difficult — could not complete without heavy intervention
2 = Difficult — completed only with significant help
3 = Neutral — completed with some hesitation or minor help
4 = Easy — completed with little to no hesitation
5 = Very easy — completed immediately and confidently

**Moderator interventions (detail)**
1. <what was said/shown, and at what point in the task>
2. ...

**Verbatim confusion quotes**
- "<exact words>" — <context: which step, before/after which action>

**Wrong destinations / dead ends / navigation ambiguity**
(role-specific category per §§2–4 — wrong destinations for League Admin,
authorization/terminology confusion for Arena Manager, navigation ambiguity
for Coach)
-

**Accessibility / keyboard observations**
- Keyboard-only navigation issues observed (if applicable):
- Screen-reader announcement issues observed (if applicable):
- Any control the participant could not reach or activate without a mouse:

**Errors or dead ends**
- Any console error, broken control, or unrecoverable state encountered:

**Follow-up findings**

| Finding | Severity (blocker / major / minor) | Supporting evidence |
| --- | --- | --- |
| | | |

**Sign-off**

| Role | Name | Date | Signature/approval |
| --- | --- | --- | --- |
| Moderator | | | |
| Repository owner | | | |
```

---

## 7. Readiness checklist (before a real session)

Confirm every item below before inviting a participant. Do not proceed on a
partial checklist — an incomplete readiness check invalidates the session's
evidentiary value for #345's merge gate.

- [ ] The intended production/PR head is actually deployed to the
      environment the session will run against, and its exact git SHA has
      been recorded (§1.1) — not assumed from a branch name.
- [ ] All CI required for #345 is green on that exact head (Memory, SQLite,
      PostgreSQL backend parity; authenticated HTTP where relevant; the
      required browser journeys).
- [ ] Test accounts and fixtures have been reset per §1.5 and re-verified
      to match the fixture state each script requires (e.g. a genuinely
      incomplete Setup workflow exists for the League Admin script).
- [ ] The participant has not seen the task script (§§2–4) or this document
      before the session.
- [ ] Where the session is intended to also serve as keyboard-only or
      screen-reader evidence, the assistive-technology setup is confirmed
      working (screen reader announces correctly, keyboard-only navigation
      is possible) before the participant arrives — do not discover a setup
      problem mid-session.
- [ ] A plan exists for where results will be attached to
      [#345][issue-345] (e.g. as a PR comment or linked file) once the
      session is complete.
- [x] The [Conflicts log](#conflicts-log) has been reviewed; its one entry
      (Arena Manager task scope) was resolved by the repository owner on
      2026-07-27 — "reach the primary action" is confirmed. Re-check this
      item if the Conflicts log gains a new, unresolved entry before a
      future session.

---

## 8. Scope boundary

- This document prepares the protocol and evidence templates only.
- No claim is made anywhere in this repository that a moderated session
  under this protocol has occurred.
- No participant quotes, timings, ratings, or sign-offs in §6 are real —
  the template's fields are intentionally blank and must stay that way
  until an actual session produces them.
- This change does not modify application code, tests, CI configuration, or
  product behavior.
- #345's standing position is unchanged by this document: the three
  moderated sessions remain unperformed and unwaived, and remain a merge
  gate for #345's implementation PR.
