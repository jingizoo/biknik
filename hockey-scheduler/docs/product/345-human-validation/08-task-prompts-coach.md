# Task prompts — Coach (facilitator-facing)

Part of the [#345 human-validation facilitator pack](README.md). **This sheet is
blank instrumentation. Nothing in it is evidence of a performed session.**

Do not show this page to the participant. It contains the intended path.

Script source: `docs/product/moderated-operator-validation-protocol.md` §4, read
at `origin/main` = `36195faadb5c97936022d8f3706af51181a6b64d`. Environment and
pre-flight: [03-environment-coach.md](03-environment-coach.md).

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

**Two clauses are deliberately not read aloud.** §4 steps 1 and 2 name the areas
the participant is expected to travel through. Those are observation and scoring
notes for you; saying them would hand over the route.

---

## Task 1

**Read this:**

```text
Find your roster.
```

**Protocol §4 step 1, verbatim:**

> 1. **"Find your roster."** — observe the path taken (through Home/Tasks,
>    direct navigation, or elsewhere).

**Pass condition** — §4's "Record" subsection, verbatim (first half):

> - Task completion (yes/no) for "found roster" and (yes/no, separately) for
>   "found the next-game workflow through Home/Tasks and Schedule."

**Intended path (do not say it).** The roster destination under Teams & People,
which opens on a game belonging to this account's own team; the landing screen
also surfaces the team's next game. Observed at `36195fa`.

**Also time this find separately** — §4's "Record" subsection: "Elapsed time for
each of the two finds."

---

## Task 2

**Read this:**

```text
Now find what needs doing for your next game.
```

*(De-hinted delivery: the protocol's wording names the two areas the path is
expected to run through.)*

**Protocol §4 step 2, verbatim:**

> 2. **"Now find the workflow for your next game."** — specifically through
>    Home/Tasks and Schedule; note if the participant instead tries an
>    unrelated area first.

**Pass condition**: the second half of the completion line quoted under Task 1 —
"found the next-game workflow through Home/Tasks and Schedule". The route is part
of this condition, so record the actual path, not just the destination.

**Also record, per §4's "Record" subsection, verbatim:**

> - **Navigation ambiguity**: any point where the participant paused between
>   two plausible destinations, named both.

---

## Task 3

**Read this:**

```text
Based on what you are looking at, what would you do next?
```

**Protocol §4 step 3, verbatim:**

> 3. **"Based on what you're looking at, what would you do next?"** — record
>    their stated next action (not necessarily performed) and whether it
>    matches a real, available action for the next game (e.g. filling an
>    open roster slot, confirming availability).

**Pass condition** — §4's "Record" subsection, verbatim:

> - Whether the stated next action was a real, available action for that
>   game (yes/no), with their exact words.

They do not have to perform it. Judge whether the action they name actually
exists for that game in this environment — check it yourself afterwards, not by
prompting them.

---

## Immediately after each task

Ask for the ease rating before moving on. §5:

> | Ease rating | See the 1–5 scale in §6 — collected once per task, immediately after that task, before moving to the next. |

And judge completion against the stated condition, not an impression. §5:

> | Completion | Yes/no against the specific pass condition stated in that script's task step — not a subjective "did fine" judgment. |

Everything above is recorded on
[11-capture-sheet-coach.md](11-capture-sheet-coach.md).
