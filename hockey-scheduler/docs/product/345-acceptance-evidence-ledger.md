# #345 Acceptance-Evidence Ledger

## Status

**Audit/evidence-traceability document only.** This ledger inventories what
#345's nine acceptance boxes and required state matrix actually have behind
them today — merged, pending, missing, or human-only — so a reviewer can check
completion claims against production boundaries instead of inferring
completion from a green PR. It does not implement, fix, or waive anything.
No application code, tests, CI, `ROADMAP.md`, or existing protocol document
is changed by this PR.

**This document does not itself close any #345 acceptance box.** Where a row
below reads `Missing` or `Human-only / unperformed`, that remains true after
this document merges — the ledger records the gap, it does not fill it.

## Snapshot semantics

**This ledger is a timestamped snapshot, not a live merge gate.** It is
accurate for the stated `main` SHA and UTC timestamp below, and for nothing
else.

- **Snapshot taken**: `2026-08-02T11:53:20Z`. Every SHA, PR state, issue
  state, CI result, repository path, symbol, and test/journey name below was
  re-verified against the merged tree at that time — by reading the files at
  the recorded SHA and by querying live GitHub state — not carried over from
  an earlier revision of this document.
- **Inspected base**: `main` at
  `71bad79fc991b49a8136ef98eef14a493b4fa78b` (merge of PR #377, the #365
  per-card state matrix).
- **`main` CI at that SHA: GREEN.** Workflow run
  [30746159535](https://github.com/jingizoo/biknik/actions/runs/30746159535)
  ("Hockey Scheduler Backend CI", push to `main`), started
  `2026-08-02T11:38:32Z`, **completed `success` at `2026-08-02T11:50:52Z`**.
  All nine jobs green: `changes`, `classifier-test`, `frontend-check`,
  `test` (Memory/SQLite), `postgres`, and all four `browser-smoke` shards.
  This was verified after the run finished; it was still `in_progress` when
  this refresh began and was **not** recorded as green until it completed.

**#345's issue checklist and current GitHub state remain authoritative for
later merge readiness.** This document is a reading of the repository at one
instant. If any of the following occur after this snapshot, its status values
become stale and must not be relied on:

- New #345 work is opened, merged, reverted, or force-pushed.
- The tracked open issues listed in §4 advance or are closed.
- #345's nine acceptance boxes are edited.
- Any cited merge commit is reverted.

Every one of #345's nine acceptance boxes is currently **unchecked on the
issue itself**. Where this ledger says `Verified on main`, that is a statement
about repository evidence for that box's *text*, not a claim that the owner
has accepted it or that #345 may merge. #345's own Done condition requires
every box to have evidence **and** the moderated sessions to be documented
**and** the current-head diff to have no release blocker.

## How to read the status column

Exactly four values are used. No others.

- **`Verified on main`** — the implementation is merged to `main` at the
  inspected SHA, **and** the cited evidence (a path, symbol, test name, or
  journey name that resolves in that tree, plus the merge PR and commit)
  covers the *entire* criterion, not a subset of it.
- **`Pending active PR`** — real evidence exists, cited at an exact head SHA,
  but has not merged. **Non-authoritative** for #345's merge gate. *(No
  criterion carries this value at this snapshot: there is no open #345
  implementation PR. The value is retained because it is part of the required
  vocabulary and will apply again as soon as one is opened.)*
- **`Missing`** — no implementation and no pending PR addresses it.
- **`Human-only / unperformed`** — the *procedure* to produce this evidence
  may be merged, but the evidence itself can only come from an actual human
  session or pass, and none has been run. Never marked `Verified` regardless
  of how complete the procedure document is.

**Each top-level acceptance criterion in §1 carries exactly one overall
status — never more than one, never zero.** Where a criterion bundles several
distinct evidence types (criterion 7), that nuance belongs in the row's
supporting text and sub-item table, not in a second status value. The decision
rule:

- if any required portion has no implementation and no evidence at all, the
  criterion is `Missing`, even if other portions are merged;
- if the only outstanding portion is one a human must perform, the criterion
  is `Human-only / unperformed`;
- `Verified on main` is never used where any required portion is a subset, a
  sub-item, or a single screen/role rather than the whole;
- **a green draft PR, a local-only test run, or a rendered value is never
  `Verified on main`.**

## Base and inspected evidence

**Merged PRs cited below.** Head SHA and merge commit are recorded separately
and deliberately: earlier revisions of this document cited head SHAs while
calling them merge commits. Both resolve; they are not the same object.

| PR | Issue | Title | Head SHA | Merge commit | Merged (UTC) |
| --- | --- | --- | --- | --- | --- |
| [#331](https://github.com/jingizoo/biknik/pull/331) | #330 | Home/Tasks hub first slice | — | `16fe833c1d360290bb00374b5458c49831b42ea3` | 2026-07-27T02:39:37Z |
| [#347](https://github.com/jingizoo/biknik/pull/347) | #345 | Guided Setup hub: six summary-first workflow landings | `5dfa6e0b5711ddd5fbb80e7ebdd3ff148aafaea7` | `f0b2caf4f8a02041aac732a7d32062a33ede404c` | 2026-07-27T11:59:39Z |
| [#349](https://github.com/jingizoo/biknik/pull/349) | #345 | Moderated operator-session protocol and evidence templates | `334ac00ff36a2717bd7a0aeb4993cc23d4db8d5b` | `9d090fe6fef4211cab9c58c10f964b14f492c92c` | 2026-07-27T08:45:11Z |
| [#350](https://github.com/jingizoo/biknik/pull/350) | #345 | Manual keyboard and screen-reader validation protocol | `0c5ffb9879111480316ed685d2820a9212e3fca1` | `e8c7d96d3fab001c1b468ac94a100b9e4c4a6f5c` | 2026-07-27T09:03:44Z |
| [#351](https://github.com/jingizoo/biknik/pull/351) | #345 | Consolidate Game Sheet/Ice Builder to approved tokens | `5d1c726c3cdfa519d86bfbf336428e62d5306315` | `49d662a95b163bea2c8303af6fa20cf429d14e7b` | 2026-07-27T10:03:45Z |
| [#352](https://github.com/jingizoo/biknik/pull/352) | #345 | Cover login, public, error, and restricted shell states | `91c5e2a79d4cfa385d957c6205acb8f1431990d3` | `b8ad10430f867638dd6ab8cdfcde8120ee4d34c3` | 2026-07-27T11:26:59Z |
| [#353](https://github.com/jingizoo/biknik/pull/353) | #345 | Seven-role destination and authorization matrix | `322a9594b103994b647ad6038b802a40f380f61d` | `8decc0c416da1b6c1899fc4fe215dd4e630feb34` | 2026-07-27T12:20:26Z |
| [#354](https://github.com/jingizoo/biknik/pull/354) | #345 | Close the breakpoint contract's blind spot; prove all four tokens | `9a017defeca55d77f398a5d332cdfa93f5046168` | `d190a6f6a627528cb32c6bfcfa79e59733a7872e` | 2026-07-28T09:50:40Z |
| [#356](https://github.com/jingizoo/biknik/pull/356) | #345 | Persistent League-context backend foundation | `80368b38d1b967d7df38b3fc3c14453ead55e2af` | `9447bb69f69209c86f44377da212ff9f9f2fd716` | 2026-07-27T13:05:50Z |
| [#361](https://github.com/jingizoo/biknik/pull/361) | #358 | Reorganize destinations into the approved seven-area IA | `c50492e270bf80a88b907ae3c928fd15005cbb5b` | `768ce2a084cd352e7fabe3a3e36423da4e0d4760` | 2026-07-28T02:49:22Z |
| [#362](https://github.com/jingizoo/biknik/pull/362) | #359 | Automated axe CI for shell and guided Setup surfaces | `03b1465109ef7300c7329fe1e82f54cd19b18b59` | `9eadfb85a742aab569b3b611ab93d7403968975e` | 2026-07-28T03:03:55Z |
| [#363](https://github.com/jingizoo/biknik/pull/363) | #360 | Expose persistent League context through authenticated HTTP | `b59de66543574a42c391744c18ae420abee72a2a` | `79baac59218e4663e5d85cb124c1f62feb6b8edc` | 2026-07-28T02:47:25Z |
| [#366](https://github.com/jingizoo/biknik/pull/366) | #364 | League context bar; context-filter changed screens | `09dcbc3296e732a8b4db502ffdc2acd50baf158c` | `6a486b70fb906f5f16da9e3ce2b2bad526c0996c` | 2026-07-29T01:49:59Z |
| [#369](https://github.com/jingizoo/biknik/pull/369) | #367 | League-filtered Home, Dashboard, and Setup data contracts | `069fb39ec820a13e3ad7db202e39334f95dc2efb` | `802caa062715834880cf00270e1921d25e2bd3a8` | 2026-07-31T08:47:54Z |
| [#371](https://github.com/jingizoo/biknik/pull/371) | #367 prereq | Scope setup hierarchy; refuse cross-Program Team writes | `a4d48507ec01036d9e9c6cd135859624327f1af0` | `9de4439629075a1ed49fd9f9c0c49683d98f821d` | 2026-07-29T18:58:10Z |
| [#372](https://github.com/jingizoo/biknik/pull/372) | #369 prereq | Centralize target-record authorization for setup mutations | `e0dd8bdd6fdde2723f8575a5d7f540ede5a7bd2e` | `f170e1a985c4f0974bc17f77d620d7c5db5567a8` | 2026-07-31T04:39:48Z |
| [#377](https://github.com/jingizoo/biknik/pull/377) | #365 | Home and Setup per-card state matrix (model + browser matrix) | `c2666b8b4defd00d78bd6c3a8970e49f5849e652` | `71bad79fc991b49a8136ef98eef14a493b4fa78b` | 2026-08-02T11:38:30Z |

**No open #345 implementation PR exists at this snapshot.** #357 (this
document's own refresh) is the only #345 child issue still open. Issues #358,
#359, #360, #364, #365 and #367 are all `CLOSED`/`COMPLETED`.

**Other sources read in full for this refresh**: issue
[#345](https://github.com/jingizoo/biknik/issues/345) (body and checklist),
`docs/product/operator-ux-requirements.md`,
`docs/product/moderated-operator-validation-protocol.md`,
`docs/product/manual-keyboard-screenreader-validation-protocol.md`,
`docs/architecture/active-context-scoping.md`,
`.github/workflows/hockey-scheduler-ci.yml`, `hockey-scheduler/e2e/package.json`,
and the merged trees of every PR in the table above.

---

## 1. Acceptance-criterion ledger

All `app.js` line numbers below are as of `71bad79f`. `app.js` grew by ~3,955
lines at that merge, so **every line number in the previous revision of this
document was stale**; all have been re-resolved. Symbol names are given
alongside line numbers so a future line shift does not invalidate the
citation.

### Criterion 1 — "A first-time League Admin can identify and open the correct next incomplete setup step from one primary action."

| | |
| --- | --- |
| Required boundary/evidence | Home/Tasks landing computes the next incomplete workflow without the operator reading raw entity data, exposes exactly one primary action, and that action opens the correct destination directly (not merely the Setup tab). |
| Evidence merged to `main` | `renderSetupProgressCard()` (`backend/hockey_scheduler/web/static/app.js:1440`) emits a single `data-setup-progress-action` primary button (emitted `app.js:1647,1676`; wired `app.js:1762`) naming the real next-incomplete workflow in operator vocabulary. `goToSetupWorkflow(key)` (`app.js:1823`) deep-links that one click into the correct destination for all six keys, using `contextSeededDrawerValues()` (`app.js:1928`) for the context-seeded create drawers and `focusParticipationRegisterControl()` (`app.js:2387`) for the Register-Team control. Backend: `ApiService.get_setup_progress()` (`backend/hockey_scheduler/api/service.py:1505`), regression `backend/tests/test_setup_progress.py` (48 tests). Browser: `e2e/home-tasks-hub.js` (PR #331, merge `16fe833`), asserting the deep-link lands on the real control — not just the tab — for each key, at desktop and 390×844. Extended by `e2e/home-tasks-state-matrix.js` (PR #377, merge `71bad79f`), whose leg 2a proves the authorized primary action **by using it**, and leg 2c proves the unauthorized role cannot. |
| Remaining gap | None identified for the literal criterion text. |
| **Status** | **`Verified on main`** |

### Criterion 2 — "All six workflows are reachable through summary-first hub entries with existing capability preserved and Workflow 6 follows the approved optional contract."

| | |
| --- | --- |
| Required boundary/evidence | A hub index listing all six #204-named workflows, each with its own summary-first landing (not the old undifferentiated Setup mega-page as the *only* route); Workflow 6 ("Imports and onboarding") carries a third status distinct from done/todo, never `next`, never blocking. |
| Evidence merged to `main` | `SETUP_WORKFLOWS` (`app.js:4596`–`4807`) declares exactly six entries — `league_season` (4597), `teams` (4611), `participation` (4626), `roster` (4677), `facilities` (4708), `import` (4790, carrying `optional: true`). `renderSetupHub()` (`app.js:6256`) renders the six-card index; `renderSetupWorkflowLanding()` (`app.js:6462`) renders one summary-first landing per workflow; both share one body renderer, `setupCardBodyHtml()` (`app.js:5487`), via `setupCardSlotHtml()` (`app.js:5621`). The pre-existing Records/Hierarchy sub-views remain reachable through `renderSetup()` (`app.js:6493`) and `setupCard()` (`app.js:6516`), so no previously-reachable screen became unreachable. Browser: `e2e/setup-workflow-hub.js` (PR #347, merge `f0b2caf4`; extended by PR #377). Workflow 6's optional contract is asserted end to end by `legOptionalCannotFail()` (`e2e/setup-state-matrix.js:1383`), which forces the setup overview, the player list **and** the progress read all to 500 at once and requires `import` to stay READY, reachable and optional; and by `assertWorkflowSixInvariants()` (`e2e/setup-state-matrix.js:1331`), which asserts it is never the roll-up's `next` and never its `blockedBy`. |
| Remaining gap | None identified for the literal criterion text. Per-card state coverage for these landings is criterion 5, and is now also merged. |
| **Status** | **`Verified on main`** |

### Criterion 3 — "No previously reachable screen is unreachable under the seven-area IA."

| | |
| --- | --- |
| Required boundary/evidence | The approved seven-area IA (Home/Tasks, Schedule, Teams & People, Facilities, Communications, Reports, Administration — `operator-ux-requirements.md` §2) replaces the five prior nav groups, with every previously reachable destination mapped to a specific destination in the new IA. |
| Evidence merged to `main` | **PR #361 (issue #358), merge `768ce2a084cd352e7fabe3a3e36423da4e0d4760`.** `backend/hockey_scheduler/web/static/index.html` now carries exactly seven `nav-group-label` elements, in the approved order and with the approved names: Home/Tasks (`index.html:68`, `data-nav-area="home_tasks"`), Schedule (`:76`), Teams & People (`:85`), Facilities (`:89`), Communications (`:103`), Reports (`:108`), Administration (`:112`). The five prior groups (Home, Schedule, People, Operations, Admin Setup) are gone. `e2e/seven-area-navigation.js` (801 lines) drives the **real** production navigation and asserts, at desktop and 390×844: (1) **inventory in both directions** — every key in the production `NAV` map (`app.js:219`) appears exactly once in the rendered nav, and every rendered destination is a real `NAV` key, so a screen can neither be dropped from the IA nor invented by it; (2) **uniqueness** — no destination in two areas, every area one of the seven approved keys; (3) **identity** — activating each destination opens the same view, asserted against `document.body.dataset.view` rather than the clicked label; (4) **role parity** for all seven roles, with at least one forbidden destination per restricted role proven hidden *and* non-functional on direct navigation; (5) **keyboard** — a real Tab walk reaches every authorized destination across all seven groups; (6) **deep links** into every populated area; (7) **no horizontal overflow or clipped destination at 390×844**. Facilities is handled as a composite `(tab, setupWorkflow)` destination (`setup` vs `setup+facilities`) so the Administration workflow index and the Facilities landing are distinguished rather than collapsed. Registered in CI browser-smoke shard 1 as `seven-area-navigation`. |
| Remaining gap | None identified for the literal criterion text. Assertion (1) is precisely the "nothing became unreachable" proof this box asks for, and it is enforced in CI rather than asserted in prose. |
| **Status** | **`Verified on main`** — changed from `Missing`; the previous revision correctly recorded that no implementation existed, and PR #361 has since merged |

### Criterion 4 — "Program/Season/League context filters changed screens correctly; Division remains local."

| | |
| --- | --- |
| Required boundary/evidence | League is promoted into the persistent Program/Season context bar while Division stays screen-local; every changed screen filters by the selected context or documents a narrow, approved exception; the permanent "display only · screens not filtered" caption is removed or narrowed. |
| Evidence merged to `main` | **Four merged slices, together covering the whole criterion.**<br><br>**(1) Backend foundation — PR #356, merge `9447bb69`.** `ActiveContext.league_id`, `exact_league_season_or_conflict()`, `authorized_league_ids()`, and the `resolve_with_league` / `options_with_league` / `set_with_league` methods on `ContextService`. Regression: `backend/tests/test_active_context_league.py`.<br><br>**(2) Authenticated HTTP transport — PR #363 (issue #360), merge `79baac59`.** Regression: `backend/tests/test_context_league_http.py` (1,235 lines added at that merge).<br><br>**(3) Persistent context-bar UI — PR #366 (issue #364), merge `6a486b70`.** `#ctx-league-select` now exists in the production shell (`index.html:172`–`173`) as a second, Program-scoped select beside the pre-existing `#ctx-select` (`index.html:170`) inside `#context-switcher` (`index.html:168`), with the accessible name "Active League — narrows create actions and Setup summaries to this League; no League is a valid state". **Division is not an axis in the context bar** — the bar carries Program/Season and League only, so Division remains screen-local as required. Browser: `e2e/league-context-bar.js` (1,293 lines), covering persistence, the extended `#ctx=` hash, keyboard reach, dual-Season carry-forward, and atomic generic refusal with both selects snapping back to the true persisted state. Backend: `test_league_context_canonical.py`, `test_league_context_http.py`, `test_league_context_races.py`.<br><br>**(4) Changed-screen filtering — PR #369 (issue #367), merge `802caa06`** (with prerequisites PR #371 `9de44396` and PR #372 `f170e1a9`). `get_setup_progress`, `get_demo_overview`, `get_standings` (`service.py:4490`) and `get_setup_overview_v2` all now resolve the persisted Program/Season/League tuple through `ContextService.resolve_with_league()` instead of being Program/Season-only or global. Facade-level regression: `backend/tests/test_league_filtered_setup_progress.py`, `test_league_filtered_dashboard.py`, `test_league_filtered_standings.py`, `test_league_filtered_overview_v2.py`. Real authenticated-HTTP regression for the roster read: `backend/tests/test_players_http_scope.py`. Browser: `e2e/league-filtered-data.js`. **The named, justified exceptions are documented**, not implied: `docs/architecture/active-context-scoping.md` carries a per-surface rules table (§"Per-surface rules", line 38) stating which axis each read narrows on and why, and an explicit "Venues have no League axis" section (line 653) recording that `Venue.league_id` is legacy vocabulary storing a *Program* id and must never be filtered as a League.<br><br>**(5) Caption narrowed.** PR #366 replaced the permanent `display only · screens not filtered` caption with a narrowed one (`index.html:176`). See the defect recorded in §3.1 — the narrowed text is itself now partly inaccurate. |
| Remaining gap | None that leaves a required portion without evidence. One **accuracy defect** is recorded in §3.1: the narrowed caption still names Roster and Standings as unfiltered, and both are now context-scoped on `main`. That is wrong operator-facing copy, not an absent filter, so it does not make this criterion `Missing` — but it is a real defect and should not be read as closed. |
| **Status** | **`Verified on main`** — changed from `Missing`. Recorded reasoning, so a reviewer can disagree with one specific step: every required portion (League in the persistent bar, Division not promoted, changed screens filtered, exceptions documented, caption narrowed) has merged, cited evidence. The caption defect in §3.1 is an inaccuracy in copy, which the status vocabulary does not treat as a missing implementation. |

### Criterion 5 — "Loading, empty, stale, error, retry, confirmation, optional, and complete states match the approved matrix."

| | |
| --- | --- |
| Required boundary/evidence | Per `operator-ux-requirements.md` §5, the full states matrix applies to Home/Tasks **and each of the six Setup workflows**: per-card error boundaries for hub-shaped screens, named empty states, stale-response guards, confirmation, success/complete, and Workflow 6's distinct optional status — at desktop and 390×844, including keyboard activation and exact focus after retry, confirmation and completion. |
| Evidence merged to `main` | **PR #377 (issue #365), merge `71bad79fc991b49a8136ef98eef14a493b4fa78b`** — recorded here as `71bad79f`. This is the single largest change to this ledger.<br><br>**The state model.** `CARD_STATE` (`app.js:597`) — `loading \| ready \| empty \| stale \| error \| confirm \| pending \| success`, with `empty` carrying a named `reason`; `CARD_STATUS` (`app.js:615`) — `done \| todo \| optional \| unknown`, backend-owned; `CARD_READ` (`app.js:630`) — `OK \| FAILED \| UNAUTHORIZED`. Card identity is workflow/card id + exact `(program, season, league)` tuple + request generation, enforced by `cardIdentityCurrent()` (`app.js:1053`) and `cardTupleCurrent()` (`app.js:1087`), with `beginCardRequest()` (`app.js:995`), `commitCardState()` (`app.js:1099`) and `readCardState()` (`app.js:1155`). Setup-side model: `buildSetupWorkflowCardModel()` (`app.js:5250`), `setupHubRollup()` (`app.js:5377`), `setupCardBodyHtml()` (`app.js:5487`), `retrySetupWorkflowCard()` (`app.js:5753`), `resolveSetupCardConfirm()` (`app.js:5872`), `SETUP_SEASON_REOPEN_ACTION` (`app.js:5030`). Backend prerequisite authority: `ApiService._workflow_prerequisite_rows()` (`service.py:2066`) — one ordered computation from which both the published prerequisite set and the first-unmet gap are projections.<br><br>**Home/Tasks EMPTY is now a real rendered state**, not a blank render. The `!program_id` test moved into `buildTasksCardModel()` (`app.js:1331`), which returns `CARD_STATE.EMPTY` with `reason: "no_program"` (`app.js:1347`) or `reason: "nothing_actionable"` (`app.js:1375`); the renderer branches on the named reason at `app.js:1582`–`1605`, painting a heading and status sentence for both reasons, and gating the `Start Initial Setup` control on `canBootstrap = hasPerm("manage_setup")` (`app.js:1583`) so it cannot dead-end. See §3.2 — the previous revision of this ledger cited a `return ""` branch that no longer exists.<br><br>**The browser matrix.** `e2e/home-tasks-state-matrix.js` (2,207 lines) and `e2e/setup-state-matrix.js` (2,722 lines), both at desktop and 390×844, both driving real production entry points with the *transport* forced by route interception rather than by calling internal helpers. Supporting journeys merged in the same PR: `e2e/setup-card-write-identity.js` (4,628 lines), `e2e/setup-prerequisite-floors.js` (748 lines), plus extensions to `home-tasks-hub.js`, `setup-workflow-hub.js`, `facilities-venue-access.js`, `league-context-bar.js` and `accessibility-foundations.js`. Backend: `backend/tests/test_setup_progress.py` grew by 607 lines. Per-state citations are in §2.<br><br>**CI registration** (all four verified in `.github/workflows/hockey-scheduler-ci.yml`): `setup-state-matrix` in browser-smoke shard 1 (line 238), `home-tasks-state-matrix` in shard 3 (line 242), `setup-prerequisite-floors` and `setup-card-write-identity` in shard 4 (line 244).<br><br>**Cells that do not exist are asserted, not skipped** — see §2's N/A column. Every `N/A` in §2 now cites the leg that *proves* inapplicability. |
| Remaining gap | One bound, recorded rather than papered over, and taken verbatim from the merged journey's own header (`e2e/setup-state-matrix.js:120`–`135`): Workflow 6's confirmation completes by navigating, and `resolveSetupCardConfirm()` announces its `done` sentence then calls `runSetupWorkflowGo()` → `switchTab()` synchronously, so the live region is populated and emptied inside one task before paint. `legConfirmImport()` asserts the sentence was **written to the region exactly once** and asserts nothing about whether it survived to be spoken — that is not something an automated browser journey can determine. Every other announcement in the journey is asserted normally. This is a bound on one announcement, not an unimplemented state. |
| **Status** | **`Verified on main`** — changed from `Missing`. The previous revision was correct that no per-card implementation existed for the six landings; PR #377 delivered both the model and the seven-surface browser matrix. |

### Criterion 6 — "Player, Guardian, Official, Viewer, League Admin, Arena Manager, and Coach journeys pass with correct authorization."

| | |
| --- | --- |
| Required boundary/evidence | All seven roles land on their correct destination, see the correct nav, can reach their one authorized action, and cannot bypass authorization by direct navigation or a real HTTP mutation — Viewer specifically has zero enabled mutation action anywhere. |
| Evidence merged to `main` | **PR #353, head `322a9594`, merge commit `8decc0c416da1b6c1899fc4fe215dd4e630feb34`.** (The previous revision cited `322a959` as the merge commit; it is the head SHA. Both resolve; the merge commit is now recorded correctly.) `e2e/role-authorization-matrix.js` covers all seven roles with real authenticated sessions, bounded `Tab`/`Shift+Tab` traversal, unauthorized-absent checks, direct-navigation bypass probes, and real negative HTTP mutations with a per-response failure tracker. `assertForbiddenNoChange()` snapshots both the setup audit (via `/api/demo/overview`) and per-game audit arrays (via `/api/games/{id}/board`), and was verified falsifiable by the reviewer before merge. Registered in CI browser-smoke shard 3.<br><br>**Independently reinforced** by `e2e/seven-area-navigation.js` (PR #361, merge `768ce2a0`), whose role-parity assertion re-proves all seven roles' destination visibility in the new IA, with at least one forbidden destination per restricted role proven hidden *and* non-functional on direct navigation, and Viewer proven to have zero enabled mutation control anywhere it can reach. |
| Remaining gap | None identified. |
| **Status** | **`Verified on main`** |

### Criterion 7 — "Desktop, 390px, breakpoint-boundary, keyboard, screen-reader, WCAG 2.2 AA, and zero-console-error evidence is attached."

This criterion bundles six distinct evidence types at different stages.
Per-sub-item detail is kept below for traceability, but the criterion as a
whole carries **exactly one overall status, given after the table**.

| Sub-item | Evidence merged to `main` | Sub-item stage |
| --- | --- | --- |
| Desktop + 390×844 | Standing convention across every merged browser journey (`e2e/*.js`, viewport pairs `1440×900` / `390×844`). Both #365 matrix journeys run both viewports (`VIEWPORTS`, `e2e/setup-state-matrix.js:188`, `e2e/home-tasks-state-matrix.js:167`), as does `e2e/seven-area-navigation.js`. | **Merged** as a baseline convention |
| Breakpoint-boundary (the four approved tokens 480/720/880/1040) | **Merged.** PR #351 (`49d662a`) fixed `styles.css`'s two out-of-contract widths; **PR #354 (merge `d190a6f6a627528cb32c6bfcfa79e59733a7872e`, merged 2026-07-28T09:50:40Z) closed the remaining two** — `onboarding.css` 760px → 720px and `setup.css` 520px → 480px. Verified by direct read at `71bad79f`: every `@media` width feature across all four production stylesheets is now one of 480/720/880/1040 (`styles.css` 402, 854, 958, 972, 1020, 1138, 1212; `web.css` 327, 331, 370; `onboarding.css` 315; `setup.css` 135) — the only other `@media` rules are `prefers-reduced-motion` (`web.css:30`) and `print` (`styles.css:403`), neither a width token. `e2e/breakpoint-contract.js` no longer carries a hard-coded stylesheet list: the set is **discovered** from every `<link rel="stylesheet">` in `index.html` and `setup.html`, so a newly linked stylesheet is under contract immediately; it is prelude-scoped (a `@media print` block and a `width: min(100%, 520px)` property are correctly not violations) and self-tests both width extraction and stylesheet discovery against temp fixtures. `e2e/breakpoint-boundaries.js` proves the **browser** collapses and restores one pixel each side of all four tokens (479/480/481, 719/720/721, 879/880/881, 1039/1040/1041) using the real stylesheets and the real `<nav class="side-nav">` extracted from `index.html` at runtime, plus no horizontal overflow and a full Tab walk at 879px and 390×844. Both registered in CI (`breakpoint-contract` as a standalone step, line 214; `breakpoint-boundaries` in browser-smoke shard 1). | **Merged** — this sub-item's previous `Missing`/blocked state is closed |
| Keyboard-only (manual pass) | `docs/product/manual-keyboard-screenreader-validation-protocol.md` (PR #350, merge `e8c7d96d`) — a **protocol and blank evidence template only**. Its own Status section, lines 5–6, states verbatim: *"Protocol and evidence templates only. No manual keyboard or screen-reader validation has been performed under this document."* No filled-in evidence artifact exists anywhere under `docs/`. | **Human-only, unperformed** |
| Screen-reader (manual pass) | Same protocol document, same disclaimer, same absence of a filled-in artifact. `e2e/setup-state-matrix.js:136`–`139` states its own boundary explicitly: *"This journey drives a real browser with real keyboard events. It is NOT a screen-reader session and NOT a moderated human session."* | **Human-only, unperformed** |
| WCAG 2.2 AA — **automated repository accessibility** (distinct from the two rows above) | **Merged and substantially broadened.** `axe-core@^4.12.1` is a declared `devDependency` (`e2e/package.json:64`) and is called by exactly three journeys: `e2e/setup-accessibility-axe-gate.js:161`, `e2e/shell-accessibility-coverage.js:189`, `e2e/home-tasks-hub.js:183`. **PR #362 (issue #359), merge `9eadfb85a742aab569b3b611ab93d7403968975e`**, added the always-run gate `e2e/setup-accessibility-axe-gate.js`, which reaches **twelve** surfaces through real clicks/navigation — signed-out login, anonymous public schedule, authenticated Home/Tasks, the Setup hub, **each of the six Setup workflow landings**, a forced 502 error, and an Official's restricted early-return — and requires zero serious/critical axe violations, zero console/page errors, no dangling skip-link target, no stale page title, and no hidden focused control, at desktop and 390×844. Registered in CI browser-smoke shard 4. PR #352 (merge `b8ad1043`) contributed `e2e/shell-accessibility-coverage.js` plus three production accessibility fixes. `accessibility-foundations.js` (pre-#345, extended by PR #377) covers skip-link, per-view titles, and dialog focus/containment. **This is automated repository accessibility. It is not, and is not evidence for, the two manual rows above.** | **Merged**, and now covering Home/Tasks and all six Setup landings rather than shell surfaces only |
| Zero-console-error | **Verified inventory, re-counted at `71bad79f`.** `e2e/` contains **59** `*.js` files. **53** are real Playwright browser journeys (`require("playwright")` present). **52 of those 53** install both `page.on("pageerror", …)` and `page.on("console", …)`. The single exception is `e2e/api-error-resilience.js`, which installs `pageerror` tracking (line 49) only, by design — it deliberately provokes 401/403/502 responses. The six non-journey files (`breakpoint-contract.js`, `check-v1-route-contract.js`, `ci-classify.js`, `ci-classify.test.js`, `ci-classify.integration.test.js`, `season-fmt-unit.js`) are static/unit checks, correctly outside this claim. Both #365 matrix journeys additionally run a **delivery reconciler** (`reconcileDeliveries()`, `e2e/home-tasks-state-matrix.js:303`) in which every deliberate failure is an allowance keyed to (method, URL, status), consumed at most once, with unmatched responses and failed-resource console lines failing the run — a fungible "ignore the next console error" counter is forbidden. | **Merged.** See §3.3 — the previous revision's "35 journeys / 34 tracked" arithmetic was stale and undercounted by 18 |

**Overall status for Criterion 7: `Human-only / unperformed`.** Four of the
six required evidence types (desktop+390, breakpoint-boundary, automated WCAG
2.2 AA, zero-console-error) are now merged with citations that resolve. The
two that remain — **manual keyboard-only and manual screen-reader passes** —
have zero human evidence and cannot be produced by merging any PR. Per the
status rule, when the only outstanding portion is one a human must perform,
that is the value.

**This is a change from `Missing`, and it is not a promotion toward
completion.** It is a narrower, more accurate name for the same gap: the
criterion is still not satisfied, still cannot be checked off, and still
blocks #345. What changed is that the automated portions that were previously
absent or blocked have merged, so `Missing` — defined as "no implementation
and no pending PR addresses it" — became a false description of the criterion
as a whole. A merged protocol document and a green axe gate are **not**
manual accessibility evidence, and nothing in #365's or #359's own scope
claimed otherwise; both explicitly disclaimed it.

### Criterion 8 — "All three moderated operator-validation sessions are completed and documented."

| | |
| --- | --- |
| Required boundary/evidence | Three real moderated sessions (League Admin, Arena Manager, Coach) — commissioned, run, and documented with completion, timing, interventions, ease rating, and confusion quotes. Not waived, not simulated. |
| Evidence merged to `main` | `docs/product/moderated-operator-validation-protocol.md` (PR #349, merge `9d090fe6`) — a protocol and blank evidence-template document. Its own Status section, lines 5–6, states verbatim: *"Protocol and evidence templates only. No moderated session has been run under this document."* A full listing of `docs/` at `71bad79f` confirms **no filled-in session artifact exists** — the only files mentioning session vocabulary are the protocol itself, `operator-ux-requirements.md`, and this ledger. |
| Remaining gap | The three sessions themselves. **Human-only** work that no code change or automated check can satisfy. |
| **Status** | **`Human-only / unperformed`** — per this issue's own repeated language ("not waived or simulated") and the protocol document's own disclaimer. **Publishing the protocol (#349) is not performing it.** Do not read the merged protocol as progress toward this box. |

### Criterion 9 — "Memory, SQLite, PostgreSQL, authenticated HTTP where relevant, and all required browser CI are green."

| | |
| --- | --- |
| Required boundary/evidence | The full backend matrix (Memory/SQLite/PostgreSQL), authenticated HTTP where relevant, and all required browser CI are green at the inspected head. |
| Evidence merged to `main` | **`main` at `71bad79f` is green.** Workflow run [30746159535](https://github.com/jingizoo/biknik/actions/runs/30746159535), push to `main`, started `2026-08-02T11:38:32Z`, **concluded `success` at `2026-08-02T11:50:52Z`**, with all nine jobs green: `changes`, `classifier-test`, `frontend-check`, `test` (Memory/SQLite), `postgres`, and browser-smoke shards 1–4 covering **53 registered journeys** (`.github/workflows/hockey-scheduler-ci.yml:237`–`244`). Authenticated-HTTP coverage is inside the `test`/`postgres` jobs: `backend/tests/test_players_http_scope.py` (real `ThreadingHTTPServer`, real `Handler`, real session cookies, raw-response assertions), `test_context_league_http.py`, `test_league_context_http.py`, `test_server_authz.py`. Every merged batch in the §"Base and inspected evidence" table was green on its own exact head at merge time. |
| Remaining gap | **This box is satisfied for the merged #345 work at this SHA only.** It is not a statement that #345 may merge: #345's Done condition additionally requires *every* box to have evidence and the moderated sessions to be documented, and criteria 7 (manual) and 8 do not. If the outstanding manual keyboard/screen-reader pass (criterion 7) or the moderated sessions (criterion 8) surface defects requiring code, that work must re-establish greenness on its own head — this row would then be stale. |
| **Status** | **`Verified on main`** — changed from `Missing`. Recorded reasoning: the previous revision read this box as "the *final, complete* #345 PR's head is green" and marked it `Missing` because no such single head exists. That reading is defensible, but under this document's own vocabulary `Missing` means "no implementation and no pending PR addresses it", which is plainly false — a green CI run on the recorded SHA is exactly the evidence this box asks for. **This is the one status call in this document most open to reasonable disagreement**, and it is flagged as such rather than presented as settled. |

---

## 2. Required state-matrix inventory

Per `operator-ux-requirements.md` §5, across Home/Tasks and each of the six
Setup workflows. **`N/A` is used only where a merged journey leg *asserts*
inapplicability** — never to paper over an untested state. Every leg named
below is a real function in the cited file at `71bad79f`.

Shared production symbols for the six Setup landings:
`SETUP_WORKFLOWS` (`app.js:4596`), `buildSetupWorkflowCardModel()`
(`app.js:5250`), `setupCardBodyHtml()` (`app.js:5487`), `setupHubRollup()`
(`app.js:5377`), `retrySetupWorkflowCard()` (`app.js:5753`),
`resolveSetupCardConfirm()` (`app.js:5872`), `commitCardState()`
(`app.js:1099`), `readCardState()` (`app.js:1155`).

### Home/Tasks hub — `renderSetupProgressCard()` (`app.js:1440`), `loadSetupProgressCard()` (`app.js:1721`), `buildTasksCardModel()` (`app.js:1346`)

Journey: `e2e/home-tasks-state-matrix.js` (PR #377, merge `71bad79f`),
desktop + 390×844. Also `e2e/home-tasks-hub.js` (PR #331, merge `16fe833`).

| State | Production entry point/symbol | Merged journey leg | Missing behavior/evidence |
| --- | --- | --- | --- |
| Loading | `CARD_STATE.LOADING` (`app.js:597`); heading "Setup progress", visually-hidden "Loading setup progress…" | **Leg 1a** — `home-tasks-state-matrix.js:1315`, asserting the per-card boundary | None identified |
| Empty | `buildTasksCardModel()` (`app.js:1331`) returns `EMPTY` with a named `reason` — `"no_program"` (`app.js:1347`) or `"nothing_actionable"` (`app.js:1375`); renderer branches at `app.js:1582`–`1605`. Two reasons, each with its own `<h3>`: "Setup progress — no program yet" (offers `Start Initial Setup` only under `canBootstrap`, `app.js:1583`) and "Setup progress — nothing for your role to do" (offers no control, and says explicitly that unmanaged workflows aren't shown, so it is not a whole-Program claim) | **Leg 1h** — `home-tasks-state-matrix.js:1181`, the no-data empty path | None. **This row previously read "renders a blank card, not a named empty-state message" — that is no longer true**; see §3.2 |
| Stale | `cardTupleCurrent()` (`app.js:1087`); heading "Setup progress — showing earlier data" | **Legs 1f** (`:1556`) and **3** (`:1646`, "stale responses cannot win") | None identified |
| Per-card error + retry | `CARD_STATE.ERROR`; heading "Setup progress unavailable", sentence "Could not load your setup progress.", `data-setup-progress-retry` (`app.js:1521,1538`, wired `:1765`); focus after retry via `focusCardTarget()` (`app.js:1229`) | **Legs 1c/1d/1e** — `home-tasks-state-matrix.js:1435`, including **keyboard-activated** retry with exact focus asserted, and per-card scoping | None. The matrix **found and fixed a real defect here**: a keyboard-activated Retry resolving into `EMPTY` dropped focus onto `<body>`, because `.dash-card h3` exists in five of six states and `EMPTY` previously painted nothing |
| Confirmation | — | **Leg 1i** — `home-tasks-state-matrix.js:2110`, "CONFIRM and PENDING are structurally unreachable": the card is a pure read, and the leg requires that **no** confirmation/pending markup (`[data-setup-card-confirm-yes]`, `[data-setup-card-confirm-no]`, `[data-setup-card-confirm-reason]`, `[data-setup-card-pending]`) ever appears in the slot, in any state | **N/A, asserted not skipped.** Claiming "the confirmation state passes" for a card that cannot have one is what this leg exists to prevent |
| Success/complete | `CARD_STATE.SUCCESS`; heading "✓ All setup steps complete" | **Leg 1g** — `home-tasks-state-matrix.js:1375`, "in full" | None identified |
| Optional (Workflow 6) | `tasksWorkflowRowsHtml()` (`app.js:1397`) renders the "Optional" badge from `w.optional` (`app.js:1406`–`1409`), set by `partitionSetupWorkflows()` (`app.js:1247`, flag at `:1252`) from `CARD_STATUS.OPTIONAL` (`app.js:616`), which is backend-owned (`service.py:1945`, `"status": "optional"`). The badge and the completion arithmetic read the **same flag**, so they cannot disagree; the SUCCESS branch reads `model.part.optional[0]` (`app.js:1623`) rather than the string `"import"` | `home-tasks-state-matrix.js`; `e2e/home-tasks-hub.js` | None identified |

### The six Setup workflow landings

Journey: `e2e/setup-state-matrix.js` (PR #377, merge `71bad79f`), desktop +
390×844, all six workflows (`league_season`, `teams`, `participation`,
`roster`, `facilities`, `import`) declared in one registry at
`setup-state-matrix.js:251`–`313`. Every state is reached through a **real**
production entry point (the hub's own "Open …" button, the landing's own
Retry/Refresh, the landing's own primary control, the real `#ctx-select`)
with the transport forced by route interception. The journey calls no
internal helper to produce a state.

| State | Production behavior asserted | Merged journey leg | Coverage / N/A |
| --- | --- | --- | --- |
| Loading | Skeleton with its visually-hidden label, `aria-busy="true"`, and **zero controls** on both the card body and the landing's action groups — with the action container asserted **present**, so "no buttons" can never be satisfied by a missing container | `legErrorAndLoadingPerLanding()` — `setup-state-matrix.js:1558` (leg 5), with `/api/v2/setup/overview` **held** | All six. Workflow 6's LOADING is reached separately in leg 7a, because leg 5 arrives via an ERROR `import` can never be in |
| Empty | The sentence **names what is missing** (the workflow's own count labels) **and** the unmet prerequisite, and **exactly one** action is offered — derived from `_workflow_prerequisite_rows()` (`service.py:2066`), not declared | `legEmptyPristine()` — `setup-state-matrix.js:1179` (leg 1), on a pristine zero-Program installation | Five required workflows. **`import`: N/A, asserted** — it declares no `summary`, so EMPTY is *unreachable*, proven by `legOptionalCannotFail()` forcing all three reads to 500 at once and requiring `import` to stay READY |
| Stale | Retained counts still visible and labelled as earlier data ("These counts are from the program, season or league you had selected earlier."), a Refresh in the card body, every landing action group withdrawn, and `contextSwitchIntentPending` **already false** — so the withdrawal is attributable to STALE and not to the switch | `legStaleAndContextRace()` — `setup-state-matrix.js:1815` (legs 7a/7b), via a **real** `#ctx-select` switch with `/api/v2/setup/progress` held open | All six (including `import`, whose STALE and LOADING are both reachable and both asserted in 7a). The **delayed-stale race** is N/A for `import`, asserted in leg 7b by byte-comparing its committed model across both tuples |
| Per-card error + retry | Exact error sentences asserted as strings, not shapes — "Couldn't load the setup overview.", "Couldn't load the player list.", "Couldn't load this workflow's setup status." Retry is **reached by tabbing** from the landing's own back control and activated with **Enter**; the announcement and the **exact focused element** are asserted on both the success and the failure outcome | `legErrorAndLoadingPerLanding()` (`:1558`, legs 4/5) and `legHubNeighbourIsolation()` (`:1442`) | All six for the read failures they can have (`roster` is the only one with two; `import` has none that can fail it — asserted, not skipped). **Neighbour isolation**: on the hub grid, a per-card retry that *fails* leaves every other card's generation, committed model and painted body **byte-identical**, beside a neighbour that has already recovered |
| Confirmation | Both declared confirmations driven by keyboard, with exact focus asserted on open, on cancel, on the blank-reason refusal and on completion; the live region read with a `MutationObserver` so "exactly once" is what a screen reader would be handed | `legConfirmImport()` — `:2067` (leg 8a, Workflow 6's "Initial Setup wizard"); `legConfirmReopen()` — `:2220` (legs 8b/8c, the derived `SETUP_SEASON_REOPEN_ACTION` on `facilities`/`participation` under an archived Season) | `import`, `facilities`, `participation` covered. **`league_season`, `teams`, `roster`: N/A, asserted** — leg 8d (`:2313`) requires zero confirmation controls on those three under **both** an ordinary and an archived Season, with the three confirmable landings as the positive control, rather than manufacturing a confirmation for them |
| Success/complete | "✓ This workflow is set up. You can still add more whenever you need to." | `legSuccessComplete()` — `setup-state-matrix.js:1268` (leg 2), on a fully provisioned Program whose five required workflows all report `done`; plus the reopen's own completion in leg 8c | All six |
| Optional | Workflow 6 stays always visible, always reachable, neither done nor todo, **never** the roll-up's `next` and **never** its `blockedBy` | `legOptionalCannotFail()` — `:1383` (leg 3), and `assertWorkflowSixInvariants()` — `:1331`, applied in the `import` arm of every other leg | `import`. **N/A for the other five, by definition** — only Workflow 6 carries optional semantics |

**Cross-cutting properties the issue names, all asserted:** one failed card
beside successful cards (leg 4); failed retry then successful retry, scoped to
that card (leg 4); delayed stale success after a newer failure
(`legRaceAfterNewerFailure()`, `:1680`) and after a context switch (leg 7b);
zero console errors, through the delivery reconciler.

**Anti-vacuity rule enforced by the journey itself** (`setup-state-matrix.js:167`–`174`):
every negative assertion is paired with the positive control that proves it
could have failed — "exactly one action in EMPTY" is asserted beside the same
landings offering two to four when nothing is blocked; "zero controls" is
always asserted together with the container being structurally present; the
discarded stale response is shown, in a control run, to be one that *does*
change the card when nothing supersedes it.

**All seven surfaces (Home/Tasks + six workflows) and all seven states are
accounted for above** — each either with a merged, named journey leg, or with
an `N/A` backed by a leg that asserts inapplicability. No cell is blank and no
cell is unexplained.

---

## 3. Stale or contradictory claims found (called out, not silently reconciled)

**1. The narrowed context caption is now partly inaccurate.** PR #366
(`6a486b70`) narrowed the permanent caption from `display only · screens not
filtered` to, verbatim at `index.html:176`: *"most existing screens (Games,
Roster, Standings, etc.) are not filtered by this selection"*. That narrowing
satisfied criterion 4's requirement — but PR #369 (`802caa06`) subsequently
made **Standings** and **Roster** context-scoped. `get_standings()`
(`service.py:4490`) resolves through `ContextService.resolve_with_league()`
and returns empty on a mismatch; `list_players` (`/api/players`) narrows via
`Team.league_id` — both are stated as such in
`docs/architecture/active-context-scoping.md`'s per-surface rules table (line
38) and regression-covered by `test_league_filtered_standings.py` and
`test_players_http_scope.py`. **The operator-facing caption therefore tells
operators the opposite of what the product now does for two of the three
screens it names.** Games remains genuinely unfiltered, so the caption is not
wholly wrong. Flagged for the owner; not fixed here, since this document
changes no production code.

**2. This ledger's own previous revision cited a code branch that no longer
exists.** The prior revision recorded Home/Tasks' Empty state as
`!progress || !progress.program_id` returning `""` (an unverified blank
render). At `71bad79f` that branch is gone: the test moved into
`buildTasksCardModel()` (`app.js:1331`) and returns `CARD_STATE.EMPTY` with a
named reason, and the renderer paints a real heading, status sentence and
permission-gated action (`app.js:1582`–`1605`). The merged code comment at
`app.js:1546` says so directly: *"Both reasons used to return the empty
string."* Two `return ""` paths do still exist in `renderSetupProgressCard()`
— `app.js:1505` (`if (model.staleFrom === CARD_STATE.EMPTY)`, because a *held*
EMPTY carries no retained read) and `app.js:1695` (an unreachable-by-design
fallthrough on `model.nextBlocked`, `app.js:1694`) — but neither is the branch
previously cited. **Row corrected in §2.**

**3. This ledger's own zero-console-error inventory was stale.** The prior
revision claimed "35 real browser journeys, 34 tracked". Re-counted at
`71bad79f`: **59** `e2e/*.js` files, **53** real Playwright journeys, **52**
installing both handlers, one deliberate exception (`api-error-resilience.js`).
The identified exception was correct; the arithmetic undercounted by 18.
**Corrected in criterion 7.**

**4. Two symbols this ledger previously cited no longer exist.**
`setupProgressFetchSeq` was removed by #365 and replaced by the per-card
generation/identity model — the name survives only in explanatory comments
(`app.js:155, 574, 645, 1715, 1891, 13617`), and `app.js:1715` states the
replacement verbatim. `setupSummaryHtml()` was removed in the same work; the
shared hub-and-landing body renderer is now `setupCardBodyHtml()`
(`app.js:5487`). **Both citations have been replaced throughout this
document.** Note for a follow-up outside this doc's scope: `setupSummaryHtml`
is still named as if live in two merged journeys' comments —
`e2e/setup-v2-context-scope.js:544` and `e2e/league-filtered-data.js:278`.

**5. PR #354 merged; the prior revision recorded it as an open, conflicting
draft.** At the time of the prior snapshot that was accurate — zero CI runs on
its head, `CONFLICTING`/`DIRTY`. It has since been rebased and merged
(head `9a017defeca55d77f398a5d332cdfa93f5046168`, merge
`d190a6f6a627528cb32c6bfcfa79e59733a7872e`, 2026-07-28T09:50:40Z), and its
post-merge `main` CI run concluded `success`. Its overlap with already-merged
#351 was resolved by extending #351's guard rather than duplicating it: the
stylesheet set is now **discovered** from the production HTML entry points
instead of declared. Recorded here because a reader of the prior revision
would otherwise carry forward a false blocker.

**6. `ROADMAP.md` still describes #345 as one undifferentiated deliverable.**
Its "Currently active sequencing" section (line 276 onward, with #345 at lines
290, 297, 301) does not reflect the batch split now visible across
#347/#349/#350/#351/#352/#353/#354/#356/#361/#362/#363/#366/#369/#377. Not
wrong — the batches are all "part of #345" — but a reader relying on
`ROADMAP.md` alone would not learn that #345 has been split into fourteen-plus
tracked pieces, all of them now merged. Flagged for the owner; `ROADMAP.md` is
explicitly out of scope for this PR.

**Superseded findings removed rather than kept.** Every prior "stale claims"
entry about #347/#352/#353/#354 open blockers is gone: all four have merged
with those blockers resolved. A stale-claims list that is itself allowed to
outdate becomes another false claim, which is the exact failure mode this
document exists to prevent.

---

## 4. Planned/open work (never completion evidence)

Recorded for roadmap visibility only. **Nothing in this section is evidence
for any acceptance box**, and no row above cites it.

| Issue | State | Scope |
| --- | --- | --- |
| [#357](https://github.com/jingizoo/biknik/issues/357) | **OPEN** | Refresh this ledger as a current-`main` snapshot — this task. The only #345 child still open |
| [#375](https://github.com/jingizoo/biknik/issues/375) | **OPEN** | Child of #206 — configurable regular-season format (N meetings per opponent, deterministic home/away). **Not #345 work**; gated behind it |
| [#376](https://github.com/jingizoo/biknik/issues/376) | **OPEN** | Child of #31 — single-elimination playoff bracket from a locked final standings snapshot. **Not #345 work**; gated behind it |

**Closed and merged** (evidence for these is cited in §1 against the merge
commit, never against the issue number): #358 (seven-area IA, via PR #361),
#359 (automated axe CI, via PR #362), #360 (League HTTP transport, via PR
#363), #364 (League context bar, via PR #366), #365 (per-card state matrix,
via PR #377), #367 (League-filtered data contracts, via PR #369).

---

## 5. What remains before #345 can close

Stated plainly, so this document cannot be mistaken for a completion claim.

| Outstanding item | Criterion | Why it is not closed |
| --- | --- | --- |
| Manual keyboard-only validation pass | 7 | No human pass has been run. `manual-keyboard-screenreader-validation-protocol.md` publishes the procedure and says so itself. **Publishing a protocol is not performing it.** |
| Manual screen-reader validation pass | 7 | Same document, same disclaimer. The merged axe gate (#362) is **automated repository accessibility** and is explicitly not a substitute — `setup-accessibility-axe-gate.js:14`–`16` records that boundary in the file itself: *"It does NOT assert manual keyboard/screen-reader behavior or the seven-area IA — that is explicitly out of scope per #359's own 'done condition'."* |
| Three moderated operator sessions (League Admin, Arena Manager, Coach) | 8 | None run. `moderated-operator-validation-protocol.md` publishes the procedure and blank templates and says no session has been run under it. Transferred to #345 as a merge gate, **not waived and not simulable** |

Everything else in §1 has merged, cited evidence at `71bad79f`. All three
remaining items are human work that no PR can produce.

---

## Revision history

- **Round 1–2** (2026-07-27): initial ledger; live-blocker surfacing; status
  vocabulary; freshness rule.
- **Round 3** (2026-07-27T13:45Z): first post-merge refresh, after
  #347/#352/#353/#356.
- **Round 4** (2026-08-02T11:53:20Z, this revision, per issue
  [#357](https://github.com/jingizoo/biknik/issues/357)): rebased onto `main`
  at `71bad79f` and fully re-verified against the merged tree rather than
  against PR descriptions. Ten further PRs had merged since Round 3
  (#354, #361, #362, #363, #366, #369, #370, #371, #372, #374, #377).
  Criteria 3, 4, 5 and 9 moved from `Missing` to `Verified on main` on merged,
  resolving citations; criterion 7 moved from `Missing` to
  `Human-only / unperformed` as a narrower name for an unchanged gap.
  Criterion 8 is unchanged. Every §2 state-matrix row was rewritten against
  the merged journeys. Four dead citations found in the prior revision were
  corrected (§3.2–§3.4), and one prior finding (#354 as a conflicting draft)
  was superseded by its merge (§3.5). `main`'s post-merge CI was polled until
  it completed and recorded only then.
