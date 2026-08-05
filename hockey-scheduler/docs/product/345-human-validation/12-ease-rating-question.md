# The ease-rating question — the one place it is written down

Part of the [#345 human-validation facilitator pack](README.md). **This sheet is
blank instrumentation. Nothing in it is evidence of a performed session.**

The moderated protocol, read at `origin/main` =
`36195faadb5c97936022d8f3706af51181a6b64d`, supplies the 1–5 ease scale and its
five anchors (§6) and requires the rating once per task, immediately after that
task (§5). It supplies **no wording for asking**. Something has to be said, and
it has to be said the same way nine times.

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

## Why this is a gate and not a default

The ease question is the most-repeated participant-facing utterance in the whole
pack: three tasks × three sessions = **nine askings**. The three-session evidence
set exists to be compared, and the comparison is only meaningful if the question
was identical every time. Settle the wording after session one and the set is
not comparable — session one asked a different question, and there is no way to
recover it short of re-running that session.

That is why this is not something the pack may decide. The owner has ratified no
wording. Picking one on their behalf and printing it as though it were settled
would be the same class of defect as scoring a secondary control as a
primary-action match: the instrumentation quietly deciding something a person was
supposed to decide, in a way nothing downstream could see.

So: **no session starts until the ruling block below is filled in.** The three
environment sheets carry it as a hard pre-flight line, and
`python3 check_pack.py --session-readiness` exits non-zero while it is blank.

---

## The ruling

Fill in every field. The wording inside the fenced blocks is what gets said out
loud, verbatim, in all three sessions.

| Field | Value |
| --- | --- |
| Ruled by (repository owner) | Repository owner (`jingizoo`) |
| Date ruled | 2026-08-05 |
| Where the ruling was recorded (issue comment, PR review, etc.) | [PR #396](https://github.com/jingizoo/biknik/pull/396) — the owner ratified the pack's proposed wording as-is, unchanged. See the PR body for the instruction as given. |

<!-- ease-ruling:start -->

**Ratified wording — first line**, asked immediately after each task:

```text
On a scale of one to five, where one is very difficult and five is very easy,
how easy was that?
```

**Ratified wording — second line**, asked once they have given a number:

```text
Which of these descriptions fits the number you gave?
```

<!-- ease-ruling:end -->

To rule: replace the contents of both fenced blocks above with the exact words
to be spoken, and fill in the table. Do not add a third line, and do not leave
a variant in a comment — this block is what all three role sheets resolve to,
and `check_pack.py`'s `ease-single-source` check fails if any role sheet starts
carrying its own copy again.

---

## What the pack proposes, for the owner to accept or replace

This is a **proposal**, not the ruling. It has no force until it is copied into
the ruling block above. It is recorded here so the owner has something concrete
to accept or reject rather than a blank page.

It is deliberately **not** shown as a `>` blockquote. Everywhere else in this
pack a blockquote means verbatim protocol text, and dressing an unratified
proposal in that marker would give it exactly the authority it does not have —
a facilitator skimming for what to say would find quoted-looking text and read
it out. `check_pack.py`'s `blockquote-is-protocol-text` check enforces the
convention by proving every blockquote in the pack appears verbatim in one of
the two pinned protocols.

```text
On a scale of one to five, where one is very difficult and five is very easy,
how easy was that?
```

and then, once they have given a number:

```text
Which of these descriptions fits the number you gave?
```

Why this shape: it states the scale and both poles before asking, so a
participant does not have to guess which end is which; it asks about the task
just completed ("that") without characterising it; and it contains no adjective
the participant has not already been given. An improvised question is where
leading creeps in — "that seemed easy enough, right?" is what an off-the-cuff
ninth asking turns into at the end of a long afternoon, and it is not the same
measurement as the first asking.

The second line exists because protocol §6 asks for the anchor **the
participant's** number maps to, not the moderator's mapping of it:

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

Read those five anchors as they are, after the second line, and record the
anchor the participant picks.

---

## What the protocol requires, verbatim

Protocol §5, on when the rating is collected:

> | Ease rating | See the 1–5 scale in §6 — collected once per task, immediately after that task, before moving to the next. |

Protocol §5, on judging completion — a separate question from ease, and not to
be inferred from the rating:

> | Completion | Yes/no against the specific pass condition stated in that script's task step — not a subjective "did fine" judgment. |

Protocol §1.7, on delivery, which binds this question exactly as it binds the
task prompts:

> - Read the task prompt for the relevant script (§§2–4) verbatim, or as
>   close to verbatim as natural delivery allows. Do not paraphrase in a way
>   that adds hints not in the script.

---

## If the owner wants the wording in the protocol instead

That would be better, and this pack cannot do it: it modifies neither protocol.
Where a ruling changes what is read aloud, the moderated protocol's own
Conflicts log is where it belongs. If the owner amends the protocol to carry the
ease-rating wording, this file becomes a pointer to that section and the ruling
block is filled with the protocol's text — the gate stays, only its source
changes.
