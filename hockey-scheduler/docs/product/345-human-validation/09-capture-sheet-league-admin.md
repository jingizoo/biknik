# Capture sheet — League Admin moderated session

Part of the [#345 human-validation facilitator pack](README.md). **This sheet is
blank instrumentation. Nothing in it is evidence of a performed session, and no
filled-in value may be cited until a real participant produces it.**

Script: `docs/product/moderated-operator-validation-protocol.md` §2, read at
`origin/main` = `36195faadb5c97936022d8f3706af51181a6b64d`.
Environment and pre-flight: [01-environment-league-admin.md](01-environment-league-admin.md).
Prompts to read: [06-task-prompts-league-admin.md](06-task-prompts-league-admin.md).

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
| Participant code (anonymous — `LA-01`, `LA-02`, … ; see the privacy rules below) | |
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

### The participant code — and why this sheet has no identity field

Protocol §6's evidence template has no participant-identity field at all. Its
**Participant** block is three lines — role under test, experience level with
this app, real-world familiarity with the role — and §1.6 says a name "is not
needed" in as many words. This sheet is the working copy of that template, and
the running order sends the transcribed result to the **public** #345 issue. A
blank labelled "participant identity" on a sheet with that destination is not a
neutral field; it is an instruction to write a real name into a public artifact,
and a conscientious facilitator would have filled it in.

So the participant is identified here by an anonymous code and nothing else:
`LA-01` for the first League Admin participant, `LA-02` for the second, and so
on. That code is the only participant identifier permitted on this sheet, in the
transcribed §6 evidence, or in anything attached to #345.

**Never write a name, contact detail, employer, job title, or account identifier
on this sheet.** That includes initials, a club or rink name that identifies one
person, a job title specific enough to name them, and the login of any real
account. §1.6 allows exactly what is needed to interpret the result — "an Arena
Manager with ~2 years' rink-scheduling experience" is its own worked example —
and the two experience rows above are where that goes.

**The consent-to-code mapping lives outside this repository, with access
restricted to the moderator.** Someone has to be able to honour a withdrawal of
consent, so the join from `LA-01` to a person does exist; it simply never enters
version control, this pack, or the issue. Keep it wherever consent records are
already kept, not in a working file beside these sheets.

**Redaction check before attachment: re-read every quote and strike anything
that identifies the participant or anyone else.** Participants name their own
rinks, clubs, colleagues and employers inside otherwise on-task sentences, so
§1.6's rule about unrelated disclosures only bites if someone actually looks.
Strike the identifying span, mark the strike, and keep the on-task remainder —
the finding is in the remainder. `check_pack.py`'s `pii-export` check runs that
exact case against a synthetic record and fails if any of it survives.

**No recordings.** These sessions run without audio or video recording. This is
a decided rule, not a pending question: the repository owner ruled on 2026-08-05
that written anonymized notes satisfy the evidence need, and that recording
stays prohibited for this pack until a separate, owner-approved protocol change
defines access, retention, deletion and consent handling. §1.6 permits recording
at the moderator's choice and requires only that it be stored outside version
control — it sets no retention period, no access list and no deletion rule, so
"optional per the moderator" would leave a facilitator deciding alone how long a
participant's voice is kept and who may hear it. That gap is real and still sits
in the canonical protocol; it is raised for the owner in
[README, "Raised for the owner, not fixed here"](README.md#raised-for-the-owner-not-fixed-here)
and must not be patched by editing the protocol from this pack.

**Supersedes the earlier requirement for an explicit blank for participant
identity.** An earlier revision of this pack's brief asked for exactly that
blank, and this sheet carried one. The participant-code rule replaces it
deliberately — the field was removed, not forgotten — because the blank fed a
public artifact and the canonical §6 template never had it.

Protocol §1.6, which governs and which nothing here overrides, verbatim:

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
| Step 1 — what needs doing next | | | | | |
| Step 2 — open it | | | | | |
| Step 3 — what will the primary action do | | | | | |

**Step 1 — what needs doing next — what protocol §2's "Record" subsection requires here, verbatim:**

> - Task completion (yes/no) for "identified and opened the correct
>   incomplete workflow."

Steps 1 and 2 are scored together against this one condition: step 1 is what
they said, step 2 is what they opened.

**Step 2 — open it — what protocol §2's "Record" subsection requires here, verbatim:**

> - Elapsed time from prompt to opening the workflow.

**Step 3 — what will the primary action do — what protocol §2's "Record" subsection requires here, verbatim:**

> - Whether the participant's stated expectation of the primary action
>   matched its actual effect (yes/no), with their exact words for both the
>   expectation and, if different, their reaction to the actual result.

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
lines on [06-task-prompts-league-admin.md](06-task-prompts-league-admin.md) — the same
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

## Wrong destinations — protocol §2's own category

> - **Wrong destinations**: any screen opened before the correct one, named
>   exactly.

"Named exactly" means the screen's own name as it appears, not a description.

| # | Screen opened | At which task | How they got there | How they left it |
| --- | --- | --- | --- | --- |
| 1 | | | | |
| 2 | | | | |
| 3 | | | | |

## Stated expectation vs. actual effect (step 3)

Step 3 asks about the primary button. If the participant answers about some other
control, that is a result, not a spoiled task — record it. But record it as
**diagnostic detail**, in the first table below, kept apart from the §2
measurement in the second. Do not redirect them mid-task
([06-task-prompts-league-admin.md](06-task-prompts-league-admin.md), Task 3).

**If the control they described was not the screen's primary action, the
canonical §2 result is Fail/No or Not evaluated — never Yes.** A correct
prediction about a secondary control is not a primary-action match; it is
evidence that the screen failed to make its primary action identifiable, which
is the failure #345's "one primary action per screen" requirement exists to
prevent. Recording it as a match would transcribe the defect under test into §6
as positive evidence. **Only an expectation about the actual primary action may
be compared for the §2 yes/no result.**

**Diagnostic detail — always recorded, never the §2 answer**

| Field | Value |
| --- | --- |
| Which control they took to be the one on offer, named exactly | |
| Was that the screen's primary action? (yes/no) | |
| Their exact words for what they expected | |
| What that control actually did when they used it | |
| Did their prediction match *that* control? (yes/no — diagnostic only) | |
| Their exact words reacting to the actual result (if different) | |

**The canonical §2 measurement — this is the value that transcribes**

> - Whether the participant's stated expectation of the primary action
>   matched its actual effect (yes/no), with their exact words for both the
>   expectation and, if different, their reaction to the actual result.

| Field | Value |
| --- | --- |
| Was the expectation they stated an expectation about the screen's **actual primary action**? (yes/no) | |
| §2 result — expectation of the primary action matched its actual effect. `Yes` only if the row above is yes **and** the prediction was right; `No` if the row above is yes and the prediction was wrong; `Not evaluated` if the row above is no. | |

A "no" in "Was that the screen's primary action?" belongs in the follow-up
findings table as well: #345 requires one primary action per screen, so an
operator who could not tell which control that was is a finding about the
screen.

## What they said before touching anything (step 1)

Recorded before any click, per §2 step 1's own instruction.

| Field | Value |
| --- | --- |
| Their exact words | |
| Which workflow, if any, they named | |
| Was that the genuinely incomplete one in this environment (pre-flight C7)? | |

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
