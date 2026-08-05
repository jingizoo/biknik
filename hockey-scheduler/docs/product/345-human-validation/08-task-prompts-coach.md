# Task prompts — Coach (facilitator-facing)

Part of the [#345 human-validation facilitator pack](README.md). **This sheet is
blank instrumentation. Nothing in it is evidence of a performed session.**

Do not show this page to the participant. It contains the intended path.

Script source: `docs/product/moderated-operator-validation-protocol.md` §4, read
at `origin/main` = `36195faadb5c97936022d8f3706af51181a6b64d`. Environment and
pre-flight: [03-environment-coach.md](03-environment-coach.md).

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

**What you read aloud, and what you don't.** Every numbered step in §§2–4 has the
same shape: a bolded sentence in quotation marks, then an em-dash, then the rest.
The bolded sentence is the prompt — it is printed below under **Read this**, and
that is the whole of what the participant hears. Everything after the em-dash is
moderator-facing: what to observe, what to record, what the step is scored
against. It was never a line to read, so not reading it withholds nothing. That
includes the areas §4 steps 1 and 2 name as the expected route — those sit
outside the quoted prompts, in the trailing clauses, and are printed below only
because you score against them.

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

**Read this** — protocol §4 step 1's own prompt, verbatim:

```text
Find your roster.
```

**The step in full** (everything after the em-dash is yours, not theirs):

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

**Read this** — protocol §4 step 2's own prompt, verbatim:

```text
Now find the workflow for your next game.
```

**The step in full** (everything after the em-dash is yours, not theirs):

> 2. **"Now find the workflow for your next game."** — specifically through
>    Home/Tasks and Schedule; note if the participant instead tries an
>    unrelated area first.

**Pass condition**: the second half of the completion line quoted under Task 1 —
"found the next-game workflow through Home/Tasks and Schedule". The route is part
of this condition, so record the actual path, not just the destination. The task
asks them to *arrive* somewhere: an answer given only in words ("I'd fill the
roster") has not found the workflow, and it also pre-empts Task 3, which asks
what they would do next based on what they are looking at. If they answer Task 2
verbally, record the words, do not accept them as the find, and let them carry on
without prompting.

**Also record, per §4's "Record" subsection, verbatim:**

> - **Navigation ambiguity**: any point where the participant paused between
>   two plausible destinations, named both.

---

## Task 3

**Read this** — protocol §4 step 3's own prompt, verbatim:

```text
Based on what you're looking at, what would you do next?
```

**The step in full** (everything after the em-dash is yours, not theirs):

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
[11-capture-sheet-coach.md](11-capture-sheet-coach.md).
