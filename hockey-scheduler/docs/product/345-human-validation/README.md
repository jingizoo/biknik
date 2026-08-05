# #345 human-validation facilitator pack

## This pack contains no evidence

Everything in here is **blank instrumentation**: environments to bring up,
scripts to run, prompts to read, and sheets to fill in. Nothing in this
directory is, or may be cited as, a performed pass.

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

## The two protocols are the authority — this pack never restates them

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

Print or fill exactly the ones a given session needs.

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

## Running order

1. Bring up the environment for the role and complete its pre-flight checklist.
   Every line has a pass/fail box. Protocol §7: "Do not proceed on a partial
   checklist — an incomplete readiness check invalidates the session's
   evidentiary value for #345's merge gate."
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

The one participant-facing line this pack does supply is the ease-rating
question, because the protocol supplies the 1–5 scale and its anchors but no
wording for asking, and something has to be said nine times across three
sessions. It is marked as this pack's wording wherever it appears, and it is
item 4 below.

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
It resolved none of them: the protocol wins, and the pack delivers the protocol.
They are listed here so the owner can rule, and so nothing gets settled quietly
by a moderator mid-session. Where a ruling changes what is read aloud, it belongs
in the moderated protocol's own Conflicts log — this pack must not edit either
protocol, and does not.

**None of these blocks a session.** Running today with the protocol's wording is
the defensible default; each ruling would only make the next run better.

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
4. **The ease-rating question is this pack's wording.** Ratify it, or replace it,
   before the first session; whatever it ends up being must be identical in all
   three sessions.

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

## Scope

This pack adds no application code, tests, CI configuration, or product
behaviour, and it modifies neither protocol. Defects found while running it are
recorded in the protocols' own defect tables for separate follow-up work, not
fixed inline — the keyboard/screen-reader protocol §6 is explicit that running
a protocol does not authorise changing the application.
