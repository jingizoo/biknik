# Task prompts — Arena Manager (facilitator-facing)

Part of the [#345 human-validation facilitator pack](README.md). **This sheet is
blank instrumentation. Nothing in it is evidence of a performed session.**

Do not show this page to the participant. It contains the intended path.

Script source: `docs/product/moderated-operator-validation-protocol.md` §3, read
at `origin/main` = `36195faadb5c97936022d8f3706af51181a6b64d`. Environment and
pre-flight: [02-environment-arena-manager.md](02-environment-arena-manager.md).

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
includes §3 step 3's "without entering any unrelated Administration area" — a
scoring condition sitting outside the quoted prompt, printed below under the pass
condition.

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

**Read this** — protocol §3 step 1's own prompt, verbatim:

```text
Find where you'd set up recurring ice.
```

**The step in full** (everything after the em-dash is yours, not theirs):

> 1. **"Find where you'd set up recurring ice."** — through the normal
>    navigation only (no search, no help text, no hints). Observe the path
>    taken.

**Part of that trailing clause is a condition on the run, not just an
observation note.** "Observe the path taken" is an instruction to you. "no
hints" binds you too — §1.7 already forbids them. But "through the normal
navigation only (no search, no help text)" is the method by which the find is
expected to happen, and it decides whether a find counts. The protocol does not
put any of it in the spoken prompt, and this pack does not invent a spoken ground
rule for it. So if the participant reaches for search or in-app help:

- **Do not interrupt, redirect, or answer.** Either would be a moderator
  intervention under §1.7 and would have to be logged as one, and it would narrow
  their search, which §1.7 forbids outright.
- Record that it happened, at which task, what they typed or opened, and whether
  you said anything — [10-capture-sheet-arena-manager.md](10-capture-sheet-arena-manager.md)
  has a row for it.
- Score the step against the path they actually took, and name the deviation in
  the completion note rather than passing it silently. A destination reached
  through search is not one reached "through the normal navigation only".

Whether this constraint should instead be stated to the participant as a ground
rule before the first task is [open question 3](README.md#open-questions-for-the-owner)
for the owner: it would need wording no protocol supplies, so the pack does not
supply it either.

**Intended path (do not say it).** From the Facilities area, the venues/rinks/ice
destination; the same workflow is also listed in the Setup area's workflow list
for this role. Observed at `36195fa`. What is being judged is where they end up,
not which of the two routes they take.

---

## Task 2

**Read this** — protocol §3 step 2's own prompt, verbatim:

```text
Before you go further — what Program/Season/League is currently active, and how
do you know?
```

**The step in full** (everything after the em-dash is yours, not theirs):

> 2. **"Before you go further — what Program/Season/League is currently
>    active, and how do you know?"** — record their answer and whether it
>    matches the actual active context shown in the context bar.

Score against the value you recorded at pre-flight C6, exactly as displayed.

---

## Task 3

**Read this** — protocol §3 step 3's own prompt, verbatim:

```text
Now reach the action you'd use to actually create that recurring ice.
```

Say nothing about stopping. Where the task stops is a scoring rule for you (the
pass condition below), not an instruction to them. Telling them up front that
they need not create anything does two things the protocol does not: it hands
them a self-declared stopping point, so any control they happen to be looking at
can be pointed at and called the answer; and it forecloses the continuation the
protocol explicitly permits (quoted further down this page) — a participant told
they need not create anything will not carry on naturally.

**The step in full** (everything after the em-dash is yours, not theirs):

> 3. **"Now reach the action you'd use to actually create that recurring
>    ice."** — the pass condition is reaching the authorized primary action
>    (see [Reconciling the two source documents](#reconciling-the-two-source-documents--no-conflict-found)
>    above for why this protocol stops at "reach," not "complete"), **without
>    entering any unrelated Administration area** along the way.

(The link inside that quote points into the protocol document, not into this
sheet. What it points at is quoted below.)

**Pass condition** — §3's "Record" subsection, verbatim:

> - Task completion (yes/no) for "reached the authorized primary action
>   without detouring into unrelated Administration areas."

The protocol also settles, in its own "Reconciling the two source documents"
section, that reaching is the bar and completing the write is not required:

> **Owner-confirmed (2026-07-27):** the repository owner confirmed "reach the
> primary action" as the intended Arena Manager task scope, matching #345's
> body and this protocol's default. The §8 draft's fuller "complete the
> write" task is superseded for the purpose of this validation session.

End the task, silently, when they reach the authorized primary action — that is
where §1.7's clock stops. If the participant carries on past that point of their
own accord, that is allowed: the same section says a moderator "may let a
participant continue through completion if they reach it naturally". Do not
prompt them to. If they ask whether they should go through with the creation,
whatever you answer is a moderator intervention — log it as one, with your exact
words.

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
[10-capture-sheet-arena-manager.md](10-capture-sheet-arena-manager.md).
