# #345 human-validation facilitator pack

## This pack contains no evidence

Every session document in here is **blank instrumentation**: environments to
bring up, scripts to run, prompts to read, and sheets to fill in. The one
non-document is [`check_pack.py`](check_pack.py), which checks this pack's own
consistency and performs no part of a session. Nothing in this directory is, or
may be cited as, a performed pass.

- No keyboard pass, screen-reader pass, or moderated operator session has been
  run under this pack.
- Every result field, timing, rating, quote and sign-off is deliberately empty
  and must stay empty until a real human produces it.
- **The two Human-only acceptance criteria remain unperformed.** Each protocol
  names its own, and this pack cites them by their words rather than by a
  position in a checklist. The keyboard/screen-reader protocol's "Authority"
  section: *"Desktop, 390px, breakpoint-boundary, keyboard, screen-reader, WCAG
  2.2 AA, and zero-console-error evidence is attached."* The moderated protocol's:
  *"All three moderated operator-validation sessions are completed and
  documented."* This pack does not advance either; it only makes them runnable.
  They are satisfied by filled-in, signed-off copies of the two protocols' own
  evidence templates, never by this directory's existence.
- Nothing here is a substitute for the automated gates, and the automated gates
  are not a substitute for any of this.

If you are reviewing a PR and want to know whether the human validation
happened: the answer is no, and no file in this pack says otherwise.

## What this is

#345's last two acceptance criteria can only be met by people: a manual
keyboard pass, a manual screen-reader pass, and three moderated operator
sessions (League Admin, Arena Manager, Coach). No pull request can produce that
evidence. This pack is the operational layer that makes those sessions runnable
by a facilitator — seeded environments with checkable preconditions, the
scripts, neutral task prompts, and capture sheets.

## The two protocols are the authority — this pack never paraphrases them

Both are already merged and both govern:

- [`../manual-keyboard-screenreader-validation-protocol.md`](../manual-keyboard-screenreader-validation-protocol.md)
  — session setup (§1), surfaces (§2), the keyboard procedure K1–K17 (§3), the
  screen-reader procedure S1–S14 (§4), the evidence template (§5), the rules
  (§6). It carries a "Superseded — do not run the earlier gating" warning and a
  conflicts log; **K5/S5 were corrected by #394** and now describe observed
  context filtering.
- [`../moderated-operator-validation-protocol.md`](../moderated-operator-validation-protocol.md)
  — session setup including the reset procedure (§1.5), consent and privacy
  (§1.6) and moderator instructions (§1.7); the three role scripts (§2, §3, §4)
  with their own "Record" subsections; common measurements (§5); the evidence
  template (§6); the readiness checklist (§7); the scope boundary (§8).

Two rules held throughout this pack:

1. **Where a protocol's content is reproduced, it is quoted verbatim and
   cited.** Every `>` blockquote in this directory is exact protocol text.
2. **Everywhere else it is referenced by section**, never paraphrased.

That is not stylistic. Before #394, this protocol's K5/S5 told a validator to
expect the *opposite* of shipped behaviour and to record that as the expected
passing result — so a conscientious human would have produced evidence
asserting the opposite of what ships, and it would have looked valid. A
paraphrase that drifts is exactly that defect, one copy further from the
source. If you find any statement in this pack that conflicts with a protocol,
the protocol wins and the pack is wrong; raise it rather than reconciling it
locally.

Every application behaviour this pack describes was observed on 2026-08-05 at
the same pinned commit the protocols were read at (merge of #394), below.

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

## The files

Print or fill exactly the ones a given session needs. The last row is not a
session document — it is the checker, and it is run, not printed.

| File | What it is | Used by |
| --- | --- | --- |
| [01-environment-league-admin.md](01-environment-league-admin.md) | Bring-up, fixture deviation, and a pass/fail pre-flight checklist | League Admin session; keyboard and screen-reader passes |
| [02-environment-arena-manager.md](02-environment-arena-manager.md) | Same, for the Arena Manager persona | Arena Manager session |
| [03-environment-coach.md](03-environment-coach.md) | Same, for the Coach persona | Coach session |
| [04-keyboard-script.md](04-keyboard-script.md) | K1–K17 verbatim, one runnable row each with pass/fail, what happened, focus before/after, defect note | Keyboard pass |
| [05-screen-reader-script.md](05-screen-reader-script.md) | S1–S14 verbatim, same shape plus a verbatim-announcement field | Screen-reader pass |
| [06-task-prompts-league-admin.md](06-task-prompts-league-admin.md) | The §2 prompts as the lines to read, each with its full step, pass condition, and what to do when it goes sideways | League Admin session |
| [07-task-prompts-arena-manager.md](07-task-prompts-arena-manager.md) | Same, for §3 | Arena Manager session |
| [08-task-prompts-coach.md](08-task-prompts-coach.md) | Same, for §4 | Coach session |
| [09-capture-sheet-league-admin.md](09-capture-sheet-league-admin.md) | Completion, timing, interventions, ease, verbatim quotes — shaped to the §6 template | League Admin session |
| [10-capture-sheet-arena-manager.md](10-capture-sheet-arena-manager.md) | Same, plus §3's authorization/terminology categories | Arena Manager session |
| [11-capture-sheet-coach.md](11-capture-sheet-coach.md) | Same, plus §4's navigation-ambiguity category | Coach session |
| [12-ease-rating-question.md](12-ease-rating-question.md) | The one canonical home of the ease-rating wording, with the owner ruling block that gates every session | All three moderated sessions |
| [check_pack.py](check_pack.py) | Executable regression coverage for this pack, and the `--session-readiness` pre-flight gate | Before a session; on every CI run |

## Running order

0. Run `python3 check_pack.py --session-readiness`. It exits non-zero while the
   ease-rating wording is unruled, which is the one thing in this pack that
   blocks a session outright. See item 4 under "Open questions for the owner".
1. Bring up the environment for the role and complete its pre-flight checklist.
   Every line has a pass/fail box, and the last line is the ease-rating gate.
   Protocol §7: "Do not proceed on a partial checklist — an incomplete readiness
   check invalidates the session's evidentiary value for #345's merge gate."
2. Read the prompts from the role's task sheet, exactly as printed under **Read
   this** — see "What the moderator says" below. Do not show that sheet to the
   participant; it names the intended path.
3. Record on the role's capture sheet, live, during the session.
4. Transcribe the filled sheet into the protocol's own §6 (moderated) or §5
   (keyboard/screen-reader) evidence template and attach that to #345. The pack
   sheets are working copies; the protocols' templates are the evidence.
5. Reset between participants per the moderated protocol §1.5 — all five steps,
   quoted in full on each environment sheet — then re-run the pre-flight from the
   top.

The two protocols are independent and neither waives the other — the
keyboard/screen-reader protocol says so itself, and this pack keeps their
evidence separate for the same reason.

## What the moderator says, and what the moderator does not

The three task sheets print one delivery of each prompt: the protocol's own. The
spoken prompt is the bolded sentence in quotation marks in §§2–4; everything
after the em-dash in those steps is moderator-facing — what to observe, what to
record, what the step is scored against — and was never a line to read.

This pack does not print a second, "de-hinted" wording of any prompt, and does
not authorise a moderator to compose one. §1.7 grants exactly one latitude,
"verbatim, or as close to verbatim as natural delivery allows", and forbids
paraphrase "that adds hints not in the script"; it does not license removing the
protocol's own vocabulary. Three sessions delivered at three different wordings
cannot be compared, and comparing them is what the three-session evidence set is
for. The capture sheets record any deviation from verbatim delivery, with the
exact words spoken — "none" is the expected value.

The one participant-facing line the protocol does not supply at all is the
ease-rating question: it gives the 1–5 scale and its anchors but no wording for
asking, and something has to be said nine times across three sessions. This pack
does not supply one either — it *proposes* one, in a single file, clearly marked
as a proposal, and the owner rules. Until they do, no session starts. That is
item 4 below, and it is the only thing here that blocks.

## Participants are recorded by code, and nothing else

The capture sheets are working copies of the moderated protocol's §6 evidence
template, and the running order above sends the transcribed result to the
**public** #345 issue. §6's **Participant** block is three lines — role under
test, experience level with this app, real-world familiarity with the role — and
it has no identity field. §1.6 says a name "is not needed". So:

- **The participant is identified by an anonymous code**: `LA-01`, `LA-02`, … on
  the League Admin sheet, `AM-nn` on the Arena Manager sheet, `C-nn` on the
  Coach sheet. That code is the only participant identifier permitted on a
  sheet, in the transcribed evidence, or in anything attached to #345.
- **Never write a name, contact detail, employer, job title, or account
  identifier on a capture sheet.**
- **The consent-to-code mapping lives outside this repository, with access
  restricted to the moderator.** Withdrawal of consent has to be honourable, so
  the join exists — it just never enters version control or the issue.
- **Redaction check before attachment: re-read every quote and strike anything
  that identifies the participant or anyone else.** Strike the identifying span,
  mark the strike, keep the on-task remainder.
- **No recordings.** These sessions run without audio or video recording.

**Supersedes the earlier requirement for an explicit blank for participant
identity.** An earlier revision of this pack's brief asked for that blank and
the three sheets carried one. The participant-code rule replaces it
deliberately: the field was removed, not forgotten. A blank labelled
"participant identity" on a sheet whose destination is a public issue is an
instruction to publish a real name, and the canonical §6 template never had such
a field to begin with.

Each rule above is repeated in full, with its reasoning, on all three capture
sheets, and `check_pack.py` fails if any of them is dropped or if an identity,
full-name, contact-detail or employer field ever returns to one of the sheets.

### Why no recordings — this is decided, not pending

The repository owner ruled on 2026-08-05 that these sessions run **without**
audio or video recording, that written anonymized notes satisfy the evidence
need, and that recording stays prohibited for this pack until a separate,
owner-approved protocol change defines access, retention, deletion and consent
handling.

The reasoning, so a later reader does not reopen it as an oversight: protocol
§1.6 makes recording optional at the moderator's choice and requires only that
any recording be stored outside version control and outside this repository. It
sets no retention period, no access list, and no deletion rule. "Optional per
the moderator" therefore leaves a facilitator deciding alone how long a
participant's voice is kept and who may hear it, on material that a participant
consented to only in the abstract. Not recording removes the question instead of
answering it badly, and costs nothing this pack needs: every measurement the
protocol asks for — completion, time, interventions, ease, verbatim quotes — is
captured in writing on the sheets, live, during the session.

## Raised for the owner, not fixed here

**The canonical moderated protocol still has no retention, access or deletion
rule for optional recordings.** §1.6 permits recording and says where not to
store it; it does not say for how long it may be kept, who may access it, or
when it is destroyed. This pack cannot fix that — it modifies neither protocol,
and a facilitator pack is the wrong place for a data-handling rule that binds
every future session. The pack's own position (no recordings) closes the
exposure for these three sessions only. Closing it properly needs an
owner-approved change to the protocol itself, which is a separate piece of work
and is not attempted here.

## Open questions for the owner

The pack found four places where the owner's brief for it ("neutral task
prompts, no UI hints") and the protocols' own text pull in different directions.
It settled none of them on the owner's behalf. For 1–3 there is a protocol
wording to fall back on, and the pack delivers it. For 4 there is not — the
protocol supplies no wording at all — so instead of inventing one the pack
stops: it is a gate, and the sessions wait. They are listed here so the owner
can rule, and so nothing gets settled quietly by a moderator mid-session. Where
a ruling changes what is read aloud, it belongs in the moderated protocol's own
Conflicts log — this pack must not edit either protocol, and does not.

**Items 1–3 do not block a session; item 4 does.** For 1–3 the pack delivers the
protocol's own wording and running today is the defensible default — a ruling
would only make the next run better. Item 4 is different in kind: it is a
participant-facing line the protocol does not supply at all, so there is nothing
to fall back on. It is a **hard pre-flight gate**, on every environment sheet,
and no session starts until it is filled in.

1. **§2 step 1 names "Setup".** The prompt read to the League Admin is *"Looking
   at this screen, what Setup work do you think should happen next?"* — and Setup
   is a top-level area label in this console. The step's own constraint is only
   that the moderator not explain the Program/Season/League/Division data model,
   which is a different thing, and the pass condition is about a *Setup* workflow,
   so removing the word would leave the answer scored against something the
   participant was never asked. Delivered verbatim. Rule whether it stays that
   way, or whether the protocol's own prompt should be amended at the source.
2. **§2 step 3 names "the primary button" and says "click".** The protocol's
   Authority section grounds this step in #345's "one primary action per screen",
   so the control class is the object of the measurement — but it is also the
   vocabulary §1.7 tells the moderator not to use. Delivered verbatim. Where the
   participant describes some other control instead, the League Admin sheet keeps
   that as diagnostic detail and as a finding about the screen — and the
   canonical §2 result is `Fail/No` or `Not evaluated`, never `Yes`. Only an
   expectation about the actual primary action is compared for the §2 yes/no,
   because a correct prediction about a secondary control is the failure under
   test, not a match.
3. **§3 step 1's "no search, no help text" is never spoken.** It is a constraint
   on how the find happens, but it sits outside the quoted prompt, so no
   participant is ever told it. The pack's default: do not interrupt, do not
   redirect, record what was reached for on the Arena Manager capture sheet, and
   score the path actually taken. Rule whether it should instead be stated as a
   ground rule before the first task — that would need wording no protocol
   supplies, and the pack will not invent participant-facing text.
4. **The ease-rating wording is unruled — and this one blocks.** The protocol
   supplies the 1–5 scale and its anchors but no wording for asking, and the
   question is asked **nine times** across the three sessions. Its consistency is
   the only thing that makes the three sets comparable, so a wording settled
   after session one invalidates the set — session one asked a different
   question and there is no way to recover it short of re-running it.

   The pack does not default one on the owner's behalf. Doing that would be the
   same class of defect as item 2's: the instrumentation quietly deciding
   something a person was supposed to decide, in a way nothing downstream could
   see. So the wording lives unruled in exactly one place —
   [12-ease-rating-question.md](12-ease-rating-question.md), which carries the
   pack's *proposal* clearly marked as a proposal — and all three role sheets
   resolve to it rather than printing their own copy.

   **No session starts until the ruling block in that file is filled in.** It is
   the last line of every environment sheet's pre-flight checklist, and
   `python3 check_pack.py --session-readiness` exits non-zero until it is ruled.

## Decide once, before the first session — and then do not change it

- **The intervention threshold.** §1.7: "pick a consistent threshold across all
  three sessions, e.g. ~60 seconds of no forward progress". All three capture
  sheets have a blank for it that says it must match.
- **Which head is under test.** The SHA field on every sheet is the
  #345 PR head this evidence is meant to support — not `main`, and not a branch
  name. If it changes between participants, §1.5 step 3 restarts the readiness
  checklist.
- **Who moderates.** Ideally one person across all three sessions; if not, they
  agree the threshold, the delivery and the ease-rating wording beforehand.

## Two things that will bite a facilitator

Both observed at `36195fa`; both are properties of the demo server, not of the
protocols:

- **Restarting the backend destroys the whole environment.** A demo-mode boot
  rebuilds an empty installation regardless of `DATABASE_URL`. Never restart
  mid-session, and never stop the server to produce an error state — the
  environment sheets give a request-blocking recipe that leaves the data intact.
- **The active Program/Season/League selection is per account.** Setting it as
  one persona does not set it for another. Each environment sheet sets its own,
  signed in as its own persona.

## The checker

[`check_pack.py`](check_pack.py) is plain `python3`, standard library only, no
third-party dependencies, and it writes to nothing.

```bash
python3 check_pack.py                     # is the pack built correctly?
python3 check_pack.py --session-readiness # may a session start today?
python3 check_pack.py --verify-breaks     # can every check still fail?
python3 check_pack.py --list-breaks
```

The first two answer different questions and give different answers today: the
pack is correctly built (green), and a session may **not** start (non-zero,
because the ease-rating wording is unruled). That is the gate working.

What it covers, and why each one is executable rather than a paragraph:

| Check | What it would catch |
| --- | --- |
| `pii-fields`, `pii-rules`, `pii-supersession-note` | an identity, full-name, contact-detail or employer field returning to a sheet that gets attached to the public issue; any of the four privacy rules being dropped |
| `pii-export` | the issue-ready export leaking a name, email, phone, employer, account identifier or an unrelated personal disclosure, run against a synthetic record that contains all of them |
| `primary-action-rubric`, `primary-action-text` | a correct prediction about a *secondary* control being transcribed as the canonical primary-action match |
| `ease-readiness`, `ease-single-source`, `ease-preflight-gate` | a session starting on an unruled ease wording, three role sheets drifting to three different questions, or the README going back to saying nothing blocks |
| `recording-consistency` | any pack-authored instruction, in any file, drifting back to permitting recording — or an operational file simply going quiet about it |
| `blockquote-is-protocol-text` | pack-authored text smuggled behind a `>`, which would give it protocol authority and hide it from every check that separates the two |
| `mutation-guard` | a document-reader mutation writing the buffer — by assignment, `+=`, or a rebuilt return — without `mut()` proving the edit landed, plus any replacement call outside `mut()` in any mutation body. An unguarded edit silently stops injecting anything the moment its anchor moves, and the mutation goes on reporting success |
| `protocol-pin`, `pin-present` | either protocol changing by one byte while this pack goes on quoting the old text |
| `ksr-steps-verbatim` | any of the 17 K steps or 14 S steps drifting from the protocol's own wording |

Every check has at least one `--break` mutation that injects the exact defect it
targets, and `--verify-breaks` runs all of them and requires **the mutation's own
named check** to fail. CI runs both halves on every push.

The "own named check" part is not pedantry, and it was added because the weaker
version missed a live defect. The three environment runbooks used to end with a
pack-authored step permitting recording, contradicting the owner ruling that the
capture sheets carried. The `recordings-permitted` mutation existed and appeared
to work — but it only ever removed the prohibition from the *capture sheets*, so
it proved the suite could go red, not that the rule was enforced where the defect
actually was. A mutation that lands on a surface its check never inspects looks
identical to one that works. `--verify-breaks` now reports that as `MISDIRECT`
and fails.

That is the same shape as the earlier `ease-readiness-blind` case, where a
fixture tripped two guards so blinding one changed nothing. Both are checks
reporting green while testing nothing, which is the failure this whole file
exists to prevent — and neither was caught by reading the checker. One was
caught by a mutation, the other by a human reading the runbooks end to end.

**The mutations need the same treatment as the checks.** A mutation whose
anchor text has moved injects nothing, and looks exactly like one that works.
So every edit a document-reader mutation makes goes through `mut()`, which
raises when the edit changes nothing, and `mutation-guard` reads this checker's
own source and enforces it. Per edit, not per mutation: a mutation with two
edits, one of whose anchors had moved, kept its check red on the strength of
the surviving edit and reported success — which is how one sat here
half-working until every edit was forced through the guard.

`mutation-guard` asks what a statement **writes**, not what it calls. An
earlier version asked the second question — it flagged `.replace()` calls
outside `mut()` — while its docstring claimed to catch every edit. So
`text += "..."`, `text = re.sub(...)`, an f-string rebuild and a slice splice
all walked straight past it. The gap between what a guard claims and what it
verifies is the same defect the guard exists to catch, so the rule is now
stated in the code's terms and the docstring is written to the implementation
rather than to the intent.

## Scope

This pack adds **no application code, no product behaviour, and no
change to any application test**, and it modifies neither protocol. Defects
found while running it are recorded in the protocols' own defect tables for
separate follow-up work, not fixed inline — the keyboard/screen-reader protocol
§6 is explicit that running a protocol does not authorise changing the
application.

**It does change CI, and that is the whole of what it changes outside this
directory.** It adds a checker for this pack and a dedicated CI job that runs it:
[`check_pack.py`](check_pack.py) here, and a `human-validation-pack` job in
`.github/workflows/hockey-scheduler-ci.yml`. The job is ungated by the repo's
fail-closed path classifier, because the protocols this checker guards are
markdown and the classifier routes markdown to `docs`, where every other job
skips — so gating it would mean the one change it exists to catch never starts
it.

So a reviewer can size this from this page alone: one new Python file inside this
directory, one new job in the existing CI workflow, and markdown. No application
source, no application tests, no product behaviour, neither protocol. The
`scope-and-ci-contract` check fails if this paragraph and the workflow ever stop
agreeing with each other.
