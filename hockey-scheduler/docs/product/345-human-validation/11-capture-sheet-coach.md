# Capture sheet — Coach moderated session

Part of the [#345 human-validation facilitator pack](README.md). **This sheet is
blank instrumentation. Nothing in it is evidence of a performed session, and no
filled-in value may be cited until a real participant produces it.**

Script: `docs/product/moderated-operator-validation-protocol.md` §4, read at
`origin/main` = `36195faadb5c97936022d8f3706af51181a6b64d`.
Environment and pre-flight: [03-environment-coach.md](03-environment-coach.md).
Prompts to read: [08-task-prompts-coach.md](08-task-prompts-coach.md).

---

## Session blanks — fill in before the participant arrives

Protocol §6 on empty fields:

> Copy this section once per session (one League Admin copy, one Arena
> Manager copy, one Coach copy) and fill in every field. Do not leave a field
> blank without an explicit "N/A" and a reason — a missing field reads as an
> unrun check.

| Field | Value |
| --- | --- |
| Date | |
| Session start time / end time | |
| Moderator | |
| Git head SHA under test | |
| Application version / release tag | |
| Backend datastore (Memory / SQLite / PostgreSQL) | |
| Deployment target (local dev server / staging / other) | |
| OS + version | |
| Browser + version | |
| Viewport / device (desktop, 390x844, or named device — exact px if neither) | |
| Assistive technology in use + version (screen reader / keyboard-only / voice control / none) | |
| Participant identity (see the privacy rule below) | |
| Participant's experience level with this app (New / some exposure / regular user) | |
| Participant's real-world familiarity with the role | |
| Consent obtained (see below) — how, and at what time | |
| Any deviation from verbatim delivery of the §§2–4 prompts (exact words spoken, at which task; "none" is the expected value) | |
| Persona signed in as (never the password) | |
| Permissions confirmed to match the role table (yes/no, note deviations) | |
| Active Program / Season / League at hand-over | |
| Fixture / data state, including every deviation applied | |
| Pre-flight checklist completed (which environment sheet, all lines PASS) | |

### The SHA field

Protocol §1.1 states what it has to satisfy:

> | Git head SHA under test | *(fill in — `git rev-parse HEAD` on the exact checkout the session runs against; must match the PR head being validated for #345)* |

### The assistive-technology field

Protocol §1.2, verbatim:

> | Assistive technology in use, if any | Screen reader (name + version) / keyboard-only / voice control / none *(fill in — required for any session standing in as the manual keyboard-only or screen-reader evidence #345 also requires; note if this session is not intended to satisfy that evidence)* |

### The participant-identity field — protocol §1.6 governs, and nothing here overrides it

This pack invents no privacy rule. Protocol §1.6, verbatim:

> - Obtain the participant's verbal or written consent before starting,
>   covering: that the session is being observed/recorded (per the
>   moderator's choice — recording is optional per the requirements package),
>   that anonymized task performance and direct quotes may be documented, and
>   that they may stop at any time.
> - **Do not record**: the participant's real login credentials (use only the
>   seeded demo personas above, or a scoped non-production test account —
>   never a real operator's live password), unnecessary personal information
>   (do not capture full name, contact details, employer, or anything beyond
>   what's needed to interpret the result — e.g. "an Arena Manager with ~2
>   years' rink-scheduling experience" is sufficient; a name is not needed).
> - If recording audio/video, store it outside version control and outside
>   this repository; only the anonymized written evidence (§6) is intended to
>   be attached to #345.
> - If a participant discloses anything unrelated to the task (unrelated
>   personal or business information), do not transcribe it into the
>   evidence document.

Protocol §1.3 says the same thing about the experience-level field:

> | Participant's real-world experience level with this app | New / some exposure / regular user *(fill in — do not record name or other identifying detail beyond what consent in §1.6 permits)* |

---

## Task-by-task results

Column shape follows protocol §6's own table, so this transcribes across
unchanged.

| Task step | Pass/fail | Completion time | # interventions | Navigation path taken | Ease (1–5) |
| --- | --- | --- | --- | --- | --- |
| Step 1 — find your roster | | | | | |
| Step 2 — find the next-game work | | | | | |
| Step 3 — what would you do next | | | | | |

**Step 1 — find your roster — what protocol §4's "Record" subsection requires here, verbatim:**

> - Task completion (yes/no) for "found roster" and (yes/no, separately) for
>   "found the next-game workflow through Home/Tasks and Schedule."

This line carries both of the Coach session's completion judgments; score the
first here and the second at step 2.

**Step 2 — find the next-game work — what protocol §4's "Record" subsection requires here, verbatim:**

> - Elapsed time for each of the two finds.

The route is part of this step's completion condition (quoted at step 1), so
record the path actually taken, not only the destination.

**Step 3 — what would you do next — what protocol §4's "Record" subsection requires here, verbatim:**

> - Whether the stated next action was a real, available action for that
>   game (yes/no), with their exact words.

---

## How each column is judged — protocol §5, verbatim

> | Measurement | Definition |
> | --- | --- |
> | Completion | Yes/no against the specific pass condition stated in that script's task step — not a subjective "did fine" judgment. |
> | Elapsed time | Seconds or minutes from the end of the moderator's prompt to task completion or moderator-ended abandonment. |
> | Moderator interventions | A count, each with a one-line description of exactly what was said or shown. Zero is a valid and good result. |
> | Ease rating | See the 1–5 scale in §6 — collected once per task, immediately after that task, before moving to the next. |
> | Confusion quotes | Verbatim participant speech, in quotation marks, with enough surrounding context to interpret it. |

---

## Ease rating — protocol §6's scale, verbatim

Collected once per task, immediately after that task. Ask it with the two fixed
lines on [08-task-prompts-coach.md](08-task-prompts-coach.md) — the same
wording in all three sessions — and read the five anchors below to the
participant, so the anchor column records **their** mapping rather than your
interpretation of their number.

> **Ease rating scale** (record which anchor the participant's number maps to):
>
> 1 = Very difficult — could not complete without heavy intervention
>
> 2 = Difficult — completed only with significant help
>
> 3 = Neutral — completed with some hesitation or minor help
>
> 4 = Easy — completed with little to no hesitation
>
> 5 = Very easy — completed immediately and confidently

| Task | Number given | Anchor the participant mapped it to | Their words, if they explained it |
| --- | --- | --- | --- |
| Task 1 | | | |
| Task 2 | | | |
| Task 3 | | | |

---

## Moderator interventions — count and detail

Zero is a valid and good result. Every intervention, however small, is logged.
Protocol §1.7:

> - A moderator *may* offer a **defined intervention** only after the
>   participant is stuck long enough to be at risk of abandoning the task
>   (use your judgment, but pick a consistent threshold across all three
>   sessions, e.g. ~60 seconds of no forward progress) — and every
>   intervention, however small, must be logged in §6 with what was said.

Threshold used in this session (must be the same across all three sessions):
______________

| # | Task | Point in the task | Exactly what was said or shown |
| --- | --- | --- | --- |
| 1 | | | |
| 2 | | | |
| 3 | | | |
| 4 | | | |

Total interventions this session: ______

---

## Verbatim confusion quotes

Protocol §1.7: "Capture confusion quotes verbatim (their exact words), not a
paraphrase or your interpretation of what they meant."

Write what they said, in quotation marks, with enough context to interpret it —
which task, before or after which action, and what was on screen.

| # | Task | Exact words (in quotation marks) | Context: before/after which action, what was on screen |
| --- | --- | --- | --- |
| 1 | | | |
| 2 | | | |
| 3 | | | |
| 4 | | | |
| 5 | | | |

---

## Navigation ambiguity — protocol §4's own category

> - **Navigation ambiguity**: any point where the participant paused between
>   two plausible destinations, named both.

| # | Task | Destination A | Destination B | Which they chose | How long they paused |
| --- | --- | --- | --- | --- | --- |
| 1 | | | | | |
| 2 | | | | | |
| 3 | | | | | |

## The two finds

| Field | Roster (step 1) | Next-game work (step 2) |
| --- | --- | --- |
| Found? (yes/no) | | |
| Elapsed time | | |
| Path actually taken, named exactly | | |
| Did they try an unrelated area first? | | |

## Stated next action (step 3)

| Field | Value |
| --- | --- |
| Their exact words | |
| Is that a real, available action for that game? (yes/no — check afterwards, never by prompting) | |
| If no: what is actually available | |

---

## Accessibility / keyboard observations

Protocol §6's own fields. This is incidental observation during a usability
session; it does not substitute for the keyboard (§3) or screen-reader (§4)
passes, which are a separate protocol and separate evidence set.

| Field | Value |
| --- | --- |
| Keyboard-only navigation issues observed (if applicable) | |
| Screen-reader announcement issues observed (if applicable) | |
| Any control the participant could not reach or activate without a mouse | |

## Errors or dead ends

| Field | Value |
| --- | --- |
| Any console error, broken control, or unrecoverable state encountered | |
| Did an error state occur that was not deliberately induced? | |

## Follow-up findings

| Finding | Severity (blocker / major / minor) | Supporting evidence |
| --- | --- | --- |
| | | |

## Sign-off

| Role | Name | Date | Signature/approval |
| --- | --- | --- | --- |
| Moderator | | | |
| Repository owner | | | |

## Transcription

This sheet is shaped to the protocol §6 evidence template so a filled copy
transcribes into it field-for-field, with no reinterpretation. Copy the §6
block from the protocol into the session's evidence document and move each
value across as-is; do not summarise, average, or round anything on the way.

Protocol §8 remains true of this sheet until a real session fills it in:

> - No participant quotes, timings, ratings, or sign-offs in §6 are real —
>   the template's fields are intentionally blank and must stay that way
>   until an actual session produces them.
