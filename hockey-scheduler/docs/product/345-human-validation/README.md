# #345 human-validation facilitator pack

## This pack contains no evidence

Everything in here is **blank instrumentation**: environments to bring up,
scripts to run, prompts to read, and sheets to fill in. Nothing in this
directory is, or may be cited as, a performed pass.

- No keyboard pass, screen-reader pass, or moderated operator session has been
  run under this pack.
- Every result field, timing, rating, quote and sign-off is deliberately empty
  and must stay empty until a real human produces it.
- **#345 acceptance criteria 7 and 8 remain Human-only and unperformed.** This
  pack does not advance them; it only makes them runnable. They are satisfied
  by filled-in, signed-off copies of the two protocols' own evidence templates,
  never by this directory's existence.
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

The protocols were read at `origin/main` =
`36195faadb5c97936022d8f3706af51181a6b64d` (merge of #394). Every application
behaviour this pack describes was observed at that same SHA on 2026-08-05.

## The files

Print or fill exactly the ones a given session needs.

| File | What it is | Used by |
| --- | --- | --- |
| [01-environment-league-admin.md](01-environment-league-admin.md) | Bring-up, fixture deviation, and a pass/fail pre-flight checklist | League Admin session; keyboard and screen-reader passes |
| [02-environment-arena-manager.md](02-environment-arena-manager.md) | Same, for the Arena Manager persona | Arena Manager session |
| [03-environment-coach.md](03-environment-coach.md) | Same, for the Coach persona | Coach session |
| [04-keyboard-script.md](04-keyboard-script.md) | K1–K17 verbatim, one runnable row each with pass/fail, what happened, focus before/after, defect note | Keyboard pass |
| [05-screen-reader-script.md](05-screen-reader-script.md) | S1–S14 verbatim, same shape plus a verbatim-announcement field | Screen-reader pass |
| [06-task-prompts-league-admin.md](06-task-prompts-league-admin.md) | Neutral prompts with no UI hints, each beside the protocol's own step and pass condition | League Admin session |
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
2. Read the prompts from the role's task sheet. Do not show that sheet to the
   participant; it names the intended path.
3. Record on the role's capture sheet, live, during the session.
4. Transcribe the filled sheet into the protocol's own §6 (moderated) or §5
   (keyboard/screen-reader) evidence template and attach that to #345. The pack
   sheets are working copies; the protocols' templates are the evidence.
5. Reset between participants per the moderated protocol §1.5, then re-run the
   pre-flight from the top.

The two protocols are independent and neither waives the other — the
keyboard/screen-reader protocol says so itself, and this pack keeps their
evidence separate for the same reason.

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
