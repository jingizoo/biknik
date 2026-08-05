# Task prompts — Arena Manager (facilitator-facing)

Part of the [#345 human-validation facilitator pack](README.md). **This sheet is
blank instrumentation. Nothing in it is evidence of a performed session.**

Do not show this page to the participant. It contains the intended path.

Script source: `docs/product/moderated-operator-validation-protocol.md` §3, read
at `origin/main` = `36195faadb5c97936022d8f3706af51181a6b64d`. Environment and
pre-flight: [02-environment-arena-manager.md](02-environment-arena-manager.md).

---

## Before you read anything aloud

Moderator discipline is protocol §1.7:

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
beneath it, verbatim. §1.7 governs: either is legitimate delivery and neither may
add a hint. Record which wording you used.

**One clause is deliberately not read aloud.** §3 step 3 carries a scoring
constraint about unrelated Administration areas. It is a pass condition, not an
instruction — saying it would tell the participant that such an area exists and
that it is the wrong way. It is printed below under the pass condition.

---

## Task 1

**Read this:**

```text
Find where you would set up recurring ice.
```

**Protocol §3 step 1, verbatim:**

> 1. **"Find where you'd set up recurring ice."** — through the normal
>    navigation only (no search, no help text, no hints). Observe the path
>    taken.

The trailing sentence is an instruction to you, not a line to read.

**Intended path (do not say it).** From the Facilities area, the venues/rinks/ice
destination; the same workflow is also listed in the Setup area's workflow list
for this role. Observed at `36195fa`. What is being judged is where they end up,
not which of the two routes they take.

---

## Task 2

**Read this:**

```text
Before you go further — which program, season and league are you working in
right now, and how can you tell?
```

*(De-hinted delivery: the protocol's wording is nearly identical; this one avoids
implying a single place to look.)*

**Protocol §3 step 2, verbatim:**

> 2. **"Before you go further — what Program/Season/League is currently
>    active, and how do you know?"** — record their answer and whether it
>    matches the actual active context shown in the context bar.

Score against the value you recorded at pre-flight C6, exactly as displayed.

---

## Task 3

**Read this:**

```text
Now get to the point where you would actually create that recurring ice. Stop
when you are at the thing you would use to do it — you do not need to create
anything.
```

*(De-hinted delivery. The protocol's own step carries the Administration
constraint, which is scoring, not instruction — see below.)*

**Protocol §3 step 3, verbatim:**

> 3. **"Now reach the action you'd use to actually create that recurring
>    ice."** — the pass condition is reaching the authorized primary action
>    (see [Reconciling the two source documents](#reconciling-the-two-source-documents--no-conflict-found)
>    above for why this protocol stops at "reach," not "complete"), **without
>    entering any unrelated Administration area** along the way.

**Pass condition** — §3's "Record" subsection, verbatim:

> - Task completion (yes/no) for "reached the authorized primary action
>   without detouring into unrelated Administration areas."

The protocol also settles, in its own "Reconciling the two source documents"
section, that reaching is the bar and completing the write is not required:

> **Owner-confirmed (2026-07-27):** the repository owner confirmed "reach the
> primary action" as the intended Arena Manager task scope, matching #345's
> body and this protocol's default. The §8 draft's fuller "complete the
> write" task is superseded for the purpose of this validation session.

If the participant carries on past that point of their own accord, that is
allowed — the same section says a moderator "may let a participant continue
through completion if they reach it naturally". Do not prompt them to.

**Intended path (do not say it).** The venues/rinks/ice destination's primary
action leads to the recurring-ice builder, which previews slots before creating
anything. Observed at `36195fa`.

**Also record, per §3's "Record" subsection, verbatim:**

> - **Authorization confusion**: any moment the participant expected to be
>   able to do something the Arena Manager role does not permit, or expected
>   a control to be hidden/disabled that wasn't (or vice versa).
> - **Terminology confusion**: any label, menu name, or field name the
>   participant misread, mispronounced, or asked to have defined.

---

## Immediately after each task

Ask for the ease rating before moving on. §5:

> | Ease rating | See the 1–5 scale in §6 — collected once per task, immediately after that task, before moving to the next. |

And judge completion against the stated condition, not an impression. §5:

> | Completion | Yes/no against the specific pass condition stated in that script's task step — not a subjective "did fine" judgment. |

Everything above is recorded on
[10-capture-sheet-arena-manager.md](10-capture-sheet-arena-manager.md).
