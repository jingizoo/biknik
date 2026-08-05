# Task prompts — League Admin (facilitator-facing)

Part of the [#345 human-validation facilitator pack](README.md). **This sheet is
blank instrumentation. Nothing in it is evidence of a performed session.**

Do not show this page to the participant. It contains the intended path.

Script source: `docs/product/moderated-operator-validation-protocol.md` §2, read
at `origin/main` = `36195faadb5c97936022d8f3706af51181a6b64d`. Environment and
pre-flight: [01-environment-league-admin.md](01-environment-league-admin.md).

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

## Before you read anything aloud

Moderator discipline is protocol §1.7. The parts that decide whether these
prompts stay clean:

> - Read the task prompt for the relevant script (§§2–4) verbatim, or as
>   close to verbatim as natural delivery allows. Do not paraphrase in a way
>   that adds hints not in the script.
> - **Do not lead the participant toward the answer.** Concretely:
>   - Do not name the screen, menu, or control the participant should use.
>   - If the participant asks "where do I click," respond with something
>     neutral like "wherever you think you need to look" — do not narrow
>     their search.
>   - Do not react (verbally or visibly) differently to a correct vs. incorrect
>     click; a moderator's tone or hesitation is itself a leading cue.

**What you read aloud, and what you don't.** Every numbered step in §§2–4 has the
same shape: a bolded sentence in quotation marks, then an em-dash, then the rest.
The bolded sentence is the prompt — it is printed below under **Read this**, and
that is the whole of what the participant hears. Everything after the em-dash is
moderator-facing: what to observe, what to record, what the step is scored
against. It was never a line to read, so not reading it withholds nothing.

**There is one delivery, and it is the protocol's.** §1.7 allows "verbatim, or as
close to verbatim as natural delivery allows" and nothing more. This pack prints
no alternative wording of its own and blesses none: three sessions delivered at
three different wordings are not comparable, and the comparison is the evidence.
If a prompt reads to you as though it names something the participant should have
had to find, do not repair it in delivery — see
[README, "Open questions for the owner"](README.md#open-questions-for-the-owner)
and raise it there.

Timing, per §1.7: "Time the task from the moment the prompt finishes being read
to the moment the participant either completes it or the moderator ends it."

---

## Task 1

**Read this** — protocol §2 step 1's own prompt, verbatim:

```text
Looking at this screen, what Setup work do you think should happen next?
```

**The step in full** (everything after the em-dash is yours, not theirs):

> 1. **"Looking at this screen, what Setup work do you think should happen
>    next?"** — without explaining the underlying Program/Season/League/
>    Division data model. Record what they say before they click anything.

**Pass condition** — §2's "Record" subsection, verbatim:

> - Task completion (yes/no) for "identified and opened the correct
>   incomplete workflow."

Task 1 and Task 2 are scored together against that one condition: Task 1 is what
they *say*, Task 2 is what they *open*. Record the spoken answer before any
click, per the step's own instruction.

**Intended path (do not say it).** The Home/Tasks hub leads with a card naming
the next incomplete workflow and offering it as the primary action; the same
workflow is also reachable through the Setup area's workflow list. Observed at
`36195fa`; if the surface has changed, what matters is whether they reached the
workflow that is actually incomplete, not which route they took.

**Watch for:** which workflow they name, and whether it is the one that is
genuinely incomplete in this environment (pre-flight C7).

---

## Task 2

**Read this** — protocol §2 step 2's own prompt, verbatim:

```text
Go ahead and open that.
```

**The step in full** (everything after the em-dash is yours, not theirs):

> 2. **"Go ahead and open that."** — observe whether they open the correct
>    incomplete workflow, or a different one.

**What §2's "Record" subsection requires here, verbatim:**

> - Elapsed time from prompt to opening the workflow.
> - Number and description of moderator interventions.
> - **Wrong destinations**: any screen opened before the correct one, named
>   exactly.

"Named exactly" means the screen's own name as it appears, not "some setup
page".

The clock stops when the workflow **opens**. That is the event §2's "Record"
names, and it is why the prompt is "open that" and not "start on that" —
starting the work and opening it are different moments, and only one of them is
a defined stop for the timer. If they carry straight on into the form, that is
fine; the recorded time is still to the open.

---

## Task 3

**Read this** — protocol §2 step 3's own prompt, verbatim:

```text
Before you do anything else — what do you think the primary button on this
screen will do if you click it?
```

**The step in full** (everything after the em-dash is yours, not theirs):

> 3. **"Before you do anything else — what do you think the primary button
>    on this screen will do if you click it?"** — record their stated
>    expectation before they click, then let them click and observe whether
>    the actual result matches.

**Pass condition** — §2's "Record" subsection, verbatim:

> - Whether the participant's stated expectation of the primary action
>   matched its actual effect (yes/no), with their exact words for both the
>   expectation and, if different, their reaction to the actual result.

**If they describe a control that is not the primary action.** The step names
the primary button; the participant may still answer about a different control,
or may not be able to tell which control is the primary one. That is itself a
result — #345's requirement is one primary action per screen — and it is not a
reason to intervene. Do not redirect them and do not point at the right control:
§1.7 forbids narrowing their search and forbids correcting a misstated
understanding mid-task. Instead:

- Record their exact words, and record **which control they took to be the one
  on offer**, named exactly ([09-capture-sheet-league-admin.md](09-capture-sheet-league-admin.md)
  has a row for it).
- Let them proceed, and record what that control actually did. Both of those are
  **diagnostic detail**. Neither is the §2 measurement.
- **If the control they described was not the screen's primary action, the
  canonical §2 result is Fail/No or Not evaluated — never Yes.** Use `Not
  evaluated` when they never stated an expectation about the primary action at
  all, which is the usual shape of this case; use `Fail/No` if they did state one
  and it turned out wrong.
- Carry the mismatch into the follow-up findings table — an operator who cannot
  identify the primary action is a finding about the screen, not a spoiled task.

**Why that distinction is the whole point of the step.** §2's "Record"
subsection defines exactly one yes/no here, and it is about the primary action:

> - Whether the participant's stated expectation of the primary action
>   matched its actual effect (yes/no), with their exact words for both the
>   expectation and, if different, their reaction to the actual result.

A participant who misreads which control is primary and then correctly predicts
what that *other* control does has demonstrated the exact failure the
"one primary action per screen" requirement exists to prevent. Scoring that as a
match writes a `yes` into the field that transcribes into §6 as the canonical
primary-action measurement — so the product defect under test would be filed as
valid-looking positive evidence for #345, and nothing downstream could tell.
**Only an expectation about the actual primary action may be compared for the §2
yes/no result.**

**Do not correct them.** §1.7:

> - Do not correct a wrong destination or a misstated understanding of what
>   a primary action does until after the participant has committed to an
>   answer for that step; corrections belong to the practitioner debrief
>   after the session, not mid-task.

---

## Immediately after each task — the ease rating

Protocol §5 requires it once per task, immediately after that task:

> | Ease rating | See the 1–5 scale in §6 — collected once per task, immediately after that task, before moving to the next. |

The protocol supplies the scale and its anchors but no wording for asking, and
this pack does not print one here. **The wording lives in exactly one place:
[12-ease-rating-question.md](12-ease-rating-question.md).** Three copies of the
most-repeated utterance in the pack become three different questions, and the
question is asked nine times across the three sessions — its consistency is what
makes the three sets comparable at all.

**The wording is not yet ruled, and that is a hard pre-flight gate.** No session
starts until the owner has filled in the ruling block in
[12-ease-rating-question.md](12-ease-rating-question.md). The environment sheet's
pre-flight checklist carries the line, and
`python3 check_pack.py --session-readiness` exits non-zero until it is filled.
Do not improvise a wording to get started; a rating collected under a different
question is not the same measurement, and settling it after session one
invalidates the whole set.

Read the five anchors as they are — protocol §6's own scale, verbatim:

> 1 = Very difficult — could not complete without heavy intervention
>
> 2 = Difficult — completed only with significant help
>
> 3 = Neutral — completed with some hesitation or minor help
>
> 4 = Easy — completed with little to no hesitation
>
> 5 = Very easy — completed immediately and confidently

Record the number and the anchor **they** chose. §6 asks for the anchor the
participant's number maps to; do not map it on their behalf.

And judge completion against the stated condition, not an impression. §5:

> | Completion | Yes/no against the specific pass condition stated in that script's task step — not a subjective "did fine" judgment. |

Everything above is recorded on
[09-capture-sheet-league-admin.md](09-capture-sheet-league-admin.md).
