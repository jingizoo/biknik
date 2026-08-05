# Task prompts — League Admin (facilitator-facing)

Part of the [#345 human-validation facilitator pack](README.md). **This sheet is
blank instrumentation. Nothing in it is evidence of a performed session.**

Do not show this page to the participant. It contains the intended path.

Script source: `docs/product/moderated-operator-validation-protocol.md` §2, read
at `origin/main` = `36195faadb5c97936022d8f3706af51181a6b64d`. Environment and
pre-flight: [01-environment-league-admin.md](01-environment-league-admin.md).

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

**On the two wordings printed for each step.** The "read this" line is the
protocol's task with UI vocabulary removed; the protocol's own line is printed
directly beneath it, verbatim. §1.7 governs: either wording is legitimate
delivery, neither may add a hint, and the de-hinted line exists only because
some of the protocol's phrasing names things the participant would otherwise
have to find. Record on the capture sheet which wording you actually used.

Timing, per §1.7: "Time the task from the moment the prompt finishes being read
to the moment the participant either completes it or the moderator ends it."

---

## Task 1

**Read this:**

```text
You have just been handed this league to run. Looking at what is in front of
you, what do you think still needs doing before it is ready to use?
```

*(The line above is this pack's de-hinted delivery, not protocol text.)*

**Protocol §2 step 1, verbatim:**

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

**Read this:**

```text
Go ahead and start on that.
```

*(De-hinted only in tense; §2's own wording is one word different.)*

**Protocol §2 step 2, verbatim:**

> 2. **"Go ahead and open that."** — observe whether they open the correct
>    incomplete workflow, or a different one.

**What §2's "Record" subsection requires here, verbatim:**

> - Elapsed time from prompt to opening the workflow.
> - Number and description of moderator interventions.
> - **Wrong destinations**: any screen opened before the correct one, named
>   exactly.

"Named exactly" means the screen's own name as it appears, not "some setup
page".

---

## Task 3

**Read this:**

```text
Before you do anything else — what do you expect to happen if you take the
action this screen is putting in front of you?
```

*(De-hinted delivery: the protocol's wording names the control by prominence,
which on some surfaces is itself a pointer.)*

**Protocol §2 step 3, verbatim:**

> 3. **"Before you do anything else — what do you think the primary button
>    on this screen will do if you click it?"** — record their stated
>    expectation before they click, then let them click and observe whether
>    the actual result matches.

**Pass condition** — §2's "Record" subsection, verbatim:

> - Whether the participant's stated expectation of the primary action
>   matched its actual effect (yes/no), with their exact words for both the
>   expectation and, if different, their reaction to the actual result.

**Do not correct them.** §1.7:

> - Do not correct a wrong destination or a misstated understanding of what
>   a primary action does until after the participant has committed to an
>   answer for that step; corrections belong to the practitioner debrief
>   after the session, not mid-task.

---

## Immediately after each task

Ask for the ease rating before moving on. §5:

> | Ease rating | See the 1–5 scale in §6 — collected once per task, immediately after that task, before moving to the next. |

And judge completion against the stated condition, not an impression. §5:

> | Completion | Yes/no against the specific pass condition stated in that script's task step — not a subjective "did fine" judgment. |

Everything above is recorded on
[09-capture-sheet-league-admin.md](09-capture-sheet-league-admin.md).
