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

- **Snapshot taken**: `2026-08-06T03:16Z` — **read from the system clock, not composed.**
  See §3.11: Rounds 5 and 6 both stated a time that had not yet happened.
  Every SHA, PR state, issue state,
  CI result, repository path, symbol, and test/journey name below was
  re-verified against the merged tree at that time — by reading the files at
  the recorded SHA and by querying live GitHub state — not carried over from
  an earlier revision of this document.
- **Inspected base**: `main` at
  `34c9167e` (merge of PR #396), which is `main`'s **tip** at snapshot time.
- **`main` CI at that SHA: GREEN.** Workflow run
  [31067094378](https://github.com/jingizoo/biknik/actions/runs/31067094378)
  ("Hockey Scheduler Backend CI", push to `main`), started
  `2026-08-06T02:58:02Z`, **completed `success` at `2026-08-06T03:13:15Z`**,
  all **ten** jobs green: `changes`, `classifier-test`, `frontend-check`,
  `test` (Memory/SQLite), `postgres`, `human-validation-pack` — added by #395,
  which runs the facilitator pack's checks and every one of its mutations on
  each push — and all four `browser-smoke` shards.

**`main` was red between Round 5 and this snapshot, and is recorded here
because Round 5 documented the red.** At `29ca277d` (PR #391) the `postgres`
job failed on `test_setup_target_authorization.SetupTargetLockAtomicityTest.test_bridge_parent_lock_sqlite_file`
with a retryable `lock_not_available` 409 on a guarded remove that was
authorized under the locks. PR #392 (merge `57cd84dc`) fixed it: SQLite's
`transaction()` and `_apply_migration` now open `BEGIN IMMEDIATE`, so the unit
holds the file's write lock across authorize → mutate → commit instead of
promoting SHARED→RESERVED at its first write — the one acquisition SQLite
refuses to run the busy handler for. **Not #345 work**, and cited here only
because criterion 9 is a statement about `main`'s greenness. Round 5 pinned
deliberately behind the tip for this reason; **this round does not need to.**

**#345's issue checklist and current GitHub state remain authoritative for
later merge readiness.** This document is a reading of the repository at one
instant. If any of the following occur after this snapshot, its status values
become stale and must not be relied on:

- New #345 work is opened, merged, reverted, or force-pushed.
- **#357 advances** — the only open #345 child.
- #345's nine acceptance boxes are edited.
- Any cited merge commit is reverted.
- `main` moves past `34c9167e` **in a way that touches a cited file**.

**Deliberately NOT staleness triggers**, corrected this round: activity on
#393, #376, #206 or #287. Round 6 listed "the tracked open issues in §4" as a
blanket trigger, which swept in four items that are **not #345 work** — a
scheduling epic, a playoff-bracket child, a substitute-engine epic and a
Schedule/Facilities journey sequenced *after* #345. None of them can change
whether a #345 acceptance box has evidence, and treating their activity as
invalidating made this document look stale far more often than it was.

Every one of #345's nine acceptance boxes is currently **unchecked on the
issue itself** (verified at snapshot time: nine `- [ ]` entries, none `[x]`).
Where this ledger says `Verified on main`, that is a statement about
repository evidence for that box's *text*, not a claim that the owner has
accepted it or that #345 may merge.

## How to read the status column

Exactly four values are used. No others.

- **`Verified on main`** — the implementation is merged to `main` at the
  inspected SHA, **and** the cited evidence (a path, symbol, test name, or
  journey name that resolves in that tree, plus the merge PR and commit)
  covers the *entire* criterion, not a subset of it.
- **`Pending active PR`** — real evidence exists, cited at an exact head SHA,
  but has not merged. **Non-authoritative** for #345's merge gate. *(No criterion
  carries this value at this snapshot. Round 8 used it for criterion 8's
  ease-rating wording, which has since merged as `34c9167e`; the value is
  retained because it is part of the required vocabulary.)*
- **`Missing`** — no implementation and no pending PR addresses it.
- **`Human-only / unperformed`** — the *procedure* to produce this evidence
  may be merged, but the evidence itself can only come from an actual human
  session or pass, and none has been run. Never marked `Verified` regardless
  of how complete the procedure document is.

**Each top-level acceptance criterion in §1 carries exactly one overall
status.** The decision rule:

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
| [#394](https://github.com/jingizoo/biknik/pull/394) | #345 | Context caption truth; K5/S5 protocol correction; cross-view regression | `26f9d679c87b6c8c2cc9251d8f35108a714f621e` | `36195faa` | 2026-08-04T12:05:09Z |
| [#395](https://github.com/jingizoo/biknik/pull/395) | #345 | Human-validation facilitator pack, its checker, and a dedicated CI job | `16865e97e5b6d7a49c63a47e0b298f77819f74b6` | `3a1720d0` | 2026-08-05T12:29:20Z |
| [#396](https://github.com/jingizoo/biknik/pull/396) | #345 | Ratify the ease-rating wording; open the last non-human session gate | `f126d822c0e6ed4d068eb277acc70c336aea41b3` | `34c9167e` | 2026-08-06T02:57Z |

**Two #345 PRs have merged since Round 4's base (`71bad79f`): #394 and #395.**
#395 added the human-validation facilitator pack — blank instrumentation for
criterion 7's two manual passes and criterion 8's three moderated sessions,
with its own checker and a `human-validation-pack` CI job. It contains no
evidence and closes no box; what it changes is that those passes are now
*runnable*. **#398 also merged in this window and is NOT #345 work** — it is
#206 scheduling-format work, merged under an owner-granted roadmap exception
recorded in its merge commit, because `ROADMAP.md` otherwise holds #206
behind open #345. Noted so a reader finding it ahead of #345 learns the
exception was granted rather than overlooked. Nine FURTHER PRs merged in the same window — #382, #384,
#385, #381, #388, #380, #389, #391 and #392 — which with #398 makes ten
non-#345 merges in it, and **all ten are #206/#375/#379/#387/#390 scheduling
work or the #392 SQLite lock fix, not #345.** They are named only
so a reader can see why every `app.js` and `service.py` line number moved
without any acceptance status changing. None of the ten is cited as evidence
for any box.

**Other sources read in full for this refresh**: issue
[#345](https://github.com/jingizoo/biknik/issues/345) (body and checklist),
`docs/product/operator-ux-requirements.md`,
`docs/product/moderated-operator-validation-protocol.md`,
`docs/product/manual-keyboard-screenreader-validation-protocol.md`,
`docs/architecture/active-context-scoping.md`,
`.github/workflows/hockey-scheduler-ci.yml`, `hockey-scheduler/e2e/package.json`,
every `hockey-scheduler/e2e/*.js` file (64 of them, enumerated
programmatically — see §3.8), and the merged trees of every PR above.

---

## 1. Acceptance-criterion ledger

All line numbers below are as of `34c9167e`. Symbol names are given alongside
so a future line shift does not invalidate the citation.

### Criterion 1 — "A first-time League Admin can identify and open the correct next incomplete setup step from one primary action."

| | |
| --- | --- |
| Required boundary/evidence | Home/Tasks landing computes the next incomplete workflow without the operator reading raw entity data, exposes exactly one primary action, and that action opens the correct destination directly (not merely the Setup tab). |
| Evidence merged to `main` | `renderSetupProgressCard()` (`backend/hockey_scheduler/web/static/app.js:1503`) emits a single `data-setup-progress-action` primary button (emitted `app.js:1710,1721`; wired `app.js:1825`) naming the real next-incomplete workflow in operator vocabulary. `goToSetupWorkflow(key)` (`app.js:1886`) deep-links that one click into the correct destination for all six keys, using `contextSeededDrawerValues()` (`app.js:1993`) for the context-seeded create drawers and `focusParticipationRegisterControl()` (`app.js:2453`) for the Register-Team control. Backend: `ApiService.get_setup_progress()` (`backend/hockey_scheduler/api/service.py:1563`), regression `backend/tests/test_setup_progress.py` (48 tests). Browser: `e2e/home-tasks-hub.js` (PR #331, merge `16fe833`), asserting the deep-link lands on the real control — not just the tab — for each key, at desktop and 390×844. Extended by `e2e/home-tasks-state-matrix.js` (PR #377, merge `71bad79f`), whose leg 2a proves the authorized primary action **by using it**, and leg 2c proves the unauthorized role cannot. |
| Remaining gap | None identified for the literal criterion text. |
| **Status** | **`Verified on main`** |

### Criterion 2 — "All six workflows are reachable through summary-first hub entries with existing capability preserved and Workflow 6 follows the approved optional contract."

| | |
| --- | --- |
| Required boundary/evidence | A hub index listing all six #204-named workflows, each with its own summary-first landing; Workflow 6 ("Imports and onboarding") carries a third status distinct from done/todo, never `next`, never blocking. |
| Evidence merged to `main` | `SETUP_WORKFLOWS` (`app.js:4673`) declares exactly six entries — `league_season` (4674), `teams` (4688), `participation` (4703), `roster` (4754), `facilities` (4785), `import` (4867, carrying `optional: true`). `renderSetupHub()` (`app.js:6333`) renders the six-card index; `renderSetupWorkflowLanding()` (`app.js:6539`) renders one summary-first landing per workflow; both share one body renderer, `setupCardBodyHtml()` (`app.js:5564`), via `setupCardSlotHtml()` (`app.js:5698`). The pre-existing Records/Hierarchy sub-views remain reachable through `renderSetup()` (`app.js:6570`) and `setupCard()` (`app.js:6593`), so no previously-reachable screen became unreachable. Browser: `e2e/setup-workflow-hub.js` (PR #347, merge `f0b2caf4`; extended by PR #377). Workflow 6's optional contract is asserted end to end by `legOptionalCannotFail()` (`e2e/setup-state-matrix.js:1383`), which forces the setup overview, the player list **and** the progress read all to 500 at once and requires `import` to stay READY, reachable and optional; and by `assertWorkflowSixInvariants()` (`e2e/setup-state-matrix.js:1331`). |
| Remaining gap | None identified for the literal criterion text. |
| **Status** | **`Verified on main`** |

### Criterion 3 — "No previously reachable screen is unreachable under the seven-area IA."

| | |
| --- | --- |
| Required boundary/evidence | The approved seven-area IA (Home/Tasks, Schedule, Teams & People, Facilities, Communications, Reports, Administration — `operator-ux-requirements.md` §2) replaces the five prior nav groups, with every previously reachable destination mapped into the new IA. |
| Evidence merged to `main` | **PR #361 (issue #358), merge `768ce2a084cd352e7fabe3a3e36423da4e0d4760`.** `index.html` carries exactly seven `nav-group-label` elements, in the approved order and names: Home/Tasks (`index.html:68`, `data-nav-area="home_tasks"` at `:67`), Schedule (`:76`), Teams & People (`:85`), Facilities (`:89`), Communications (`:103`), Reports (`:108`), Administration (`:112`). The five prior groups are gone. `e2e/seven-area-navigation.js` (801 lines) drives the **real** production navigation and asserts, at desktop and 390×844: (1) **inventory in both directions** — every key in the production `NAV` map (`app.js:271`) appears exactly once in the rendered nav, and every rendered destination is a real `NAV` key, so a screen can neither be dropped from the IA nor invented by it; (2) **uniqueness**; (3) **identity** — activating each destination opens the same view, asserted against `document.body.dataset.view` rather than the clicked label; (4) **role parity** for all seven roles, with at least one forbidden destination per restricted role proven hidden *and* non-functional on direct navigation; (5) **keyboard** — a real Tab walk reaches every authorized destination; (6) **deep links**; (7) **no horizontal overflow or clipped destination at 390×844**. Registered in CI browser-smoke shard 1 (`.github/workflows/hockey-scheduler-ci.yml:318`). |
| Remaining gap | None identified. Assertion (1) is precisely the "nothing became unreachable" proof this box asks for, enforced in CI rather than asserted in prose. |
| **Status** | **`Verified on main`** |

### Criterion 4 — "Program/Season/League context filters changed screens correctly; Division remains local."

| | |
| --- | --- |
| Required boundary/evidence | League is promoted into the persistent Program/Season context bar while Division stays screen-local; every changed screen filters by the selected context or documents a narrow, approved exception; the permanent "display only · screens not filtered" caption is removed or narrowed. |
| Evidence merged to `main` | **Four merged slices.**<br><br>**(1) Backend foundation — PR #356, merge `9447bb69`.** `ActiveContext.league_id`, `exact_league_season_or_conflict()`, `authorized_league_ids()`, and `resolve_with_league` / `options_with_league` / `set_with_league` on `ContextService`. Regression: `backend/tests/test_active_context_league.py`.<br><br>**(2) Authenticated HTTP transport — PR #363 (issue #360), merge `79baac59`.** Regression: `backend/tests/test_context_league_http.py`.<br><br>**(3) Persistent context-bar UI — PR #366 (issue #364), merge `6a486b70`.** `#ctx-league-select` (`index.html:194`) beside the pre-existing `#ctx-select` (`index.html:191`) inside `#context-switcher` (`index.html:189`). **Division is not an axis in the context bar**, so Division remains screen-local as required. Browser: `e2e/league-context-bar.js` (1,293 lines). Backend: `test_league_context_canonical.py`, `test_league_context_http.py`, `test_league_context_races.py`.<br><br>**(4) Changed-screen filtering — PR #369 (issue #367), merge `802caa06`** (prerequisites #371 `9de44396`, #372 `f170e1a9`). `get_setup_progress` (`service.py:1563`), `get_demo_overview` (`service.py:7075`), `get_standings` (`service.py:4548`) and `get_setup_overview_v2` (`service.py:7908`) all resolve the persisted tuple through `ContextService.resolve_with_league()`. Facade regression: `test_league_filtered_setup_progress.py`, `test_league_filtered_dashboard.py`, `test_league_filtered_standings.py`, `test_league_filtered_overview_v2.py`. HTTP regression for the roster read: `test_players_http_scope.py`. Browser: `e2e/league-filtered-data.js`. **The named exceptions are documented**: `docs/architecture/active-context-scoping.md` carries a per-surface rules table (line 38) and an explicit "Venues have no League axis" section (line 1191) recording that `Venue.league_id` is legacy vocabulary storing a *Program* id.<br><br>**(5) Caption — now true, after two false revisions.** PR #366 replaced the permanent `display only · screens not filtered` caption with a narrowed enumeration, which was **itself false**. **PR #394 (merge `36195faa`) replaced it with capability wording**: `index.html:199` now reads *"Switching this changes what you see"*, wired as `aria-describedby` on **both** selects (`index.html:192`, `:195`) — so a keyboard user hears it at all, which the previous static topbar text never achieved in browse mode. See §3.1, now **resolved**. |
| Remaining gap | **None outstanding.** The criterion's requirement — caption "removed or narrowed" — was met by #366; every changed screen filters; and the false replacement copy that Round 6 flagged is fixed on `main` by #394. The Round 6 caveat ("a reviewer who reads this as including *telling the operator the truth about it* would mark it `Missing`") no longer bites: under either reading, this criterion now has evidence. |
| **Status** | **`Verified on main`** |

### Criterion 5 — "Loading, empty, stale, error, retry, confirmation, optional, and complete states match the approved matrix."

| | |
| --- | --- |
| Required boundary/evidence | Per `operator-ux-requirements.md` §5, the full states matrix applies to Home/Tasks **and each of the six Setup workflows**, at desktop and 390×844, including keyboard activation and exact focus after retry, confirmation and completion. |
| Evidence merged to `main` | **PR #377 (issue #365), merge `71bad79fc991b49a8136ef98eef14a493b4fa78b`.**<br><br>**The state model.** `CARD_STATE` (`app.js:660`) — `loading \| ready \| empty \| stale \| error \| confirm \| pending \| success`; `CARD_STATUS` (`app.js:678`) — `done \| todo \| optional \| unknown`, backend-owned; `CARD_READ` (`app.js:693`). Card identity is workflow/card id + exact `(program, season, league)` tuple + request generation, enforced by `cardIdentityCurrent()` (`app.js:1116`) and `cardTupleCurrent()` (`app.js:1150`), with `beginCardRequest()` (`app.js:1058`), `commitCardState()` (`app.js:1162`) and `readCardState()` (`app.js:1218`). Setup-side: `buildSetupWorkflowCardModel()` (`app.js:5327`), `setupHubRollup()` (`app.js:5454`), `setupCardBodyHtml()` (`app.js:5564`), `retrySetupWorkflowCard()` (`app.js:5830`), `resolveSetupCardConfirm()` (`app.js:5949`), `SETUP_SEASON_REOPEN_ACTION` (`app.js:5107`). Backend prerequisite authority: `_workflow_prerequisite_rows()` (`service.py:2124`).<br><br>**Home/Tasks EMPTY is a real rendered state.** The `!program_id` test lives in `buildTasksCardModel()` (`app.js:1394`), returning `CARD_STATE.EMPTY` with `reason: "no_program"` (`app.js:1410`) or `"nothing_actionable"` (`app.js:1438`); the renderer branches on the named reason at `app.js:1627`–`1650`, painting a heading and status sentence for both, and gating `Start Initial Setup` on `canBootstrap = hasPerm("manage_setup")` (`app.js:1646`).<br><br>**The browser matrix.** `e2e/home-tasks-state-matrix.js` (2,207 lines) and `e2e/setup-state-matrix.js` (2,722 lines), both at desktop and 390×844, both driving real production entry points with the *transport* forced by route interception. Supporting journeys: `e2e/setup-card-write-identity.js` (4,628 lines), `e2e/setup-prerequisite-floors.js` (748 lines). Backend: `test_setup_progress.py` grew by 607 lines. **All six journey files remain byte-identical at `34c9167e` to their state at `71bad79f`** (verified by diff), so every leg line number in §2 resolves unchanged.<br><br>**CI registration**: `setup-state-matrix` shard 1 (line 318), `home-tasks-state-matrix` shard 3 (line 322), `setup-prerequisite-floors` and `setup-card-write-identity` shard 4 (line 324). |
| Remaining gap | One bound, taken verbatim from the merged journey's own header (`e2e/setup-state-matrix.js:120`–`135`): Workflow 6's confirmation completes by navigating, so the live region is populated and emptied inside one task before paint. `legConfirmImport()` asserts the sentence was **written exactly once** and asserts nothing about whether it survived to be spoken. A bound on one announcement, not an unimplemented state. |
| **Status** | **`Verified on main`** |

### Criterion 6 — "Player, Guardian, Official, Viewer, League Admin, Arena Manager, and Coach journeys pass with correct authorization."

| | |
| --- | --- |
| Required boundary/evidence | All seven roles land on their correct destination, see the correct nav, reach their one authorized action, and cannot bypass authorization by direct navigation or a real HTTP mutation — Viewer specifically has zero enabled mutation action anywhere. |
| Evidence merged to `main` | **PR #353, head `322a9594`, merge `8decc0c416da1b6c1899fc4fe215dd4e630feb34`.** `e2e/role-authorization-matrix.js` covers all seven roles with real authenticated sessions, bounded `Tab`/`Shift+Tab` traversal, unauthorized-absent checks, direct-navigation bypass probes, and real negative HTTP mutations with a per-response failure tracker. `assertForbiddenNoChange()` snapshots both the setup audit and per-game audit arrays, and was verified falsifiable by the reviewer before merge. Registered in shard 3 (`hockey-scheduler-ci.yml:322`).<br><br>**Independently reinforced** by `e2e/seven-area-navigation.js` (PR #361, merge `768ce2a0`), re-proving all seven roles' destination visibility in the new IA, with Viewer proven to have zero enabled mutation control anywhere it can reach. |
| Remaining gap | None identified. |
| **Status** | **`Verified on main`** |

### Criterion 7 — "Desktop, 390px, breakpoint-boundary, keyboard, screen-reader, WCAG 2.2 AA, and zero-console-error evidence is attached."

| Sub-item | Evidence merged to `main` | Sub-item stage |
| --- | --- | --- |
| Desktop + 390×844 | Standing convention across every merged browser journey (viewport pairs `1440×900` / `390×844`). Both #365 matrix journeys run both (`VIEWPORTS`, `e2e/setup-state-matrix.js:188`, `e2e/home-tasks-state-matrix.js:167`), as does `e2e/seven-area-navigation.js`. | **Merged** |
| Breakpoint-boundary (480/720/880/1040) | **Merged.** PR #351 (`49d662a`) fixed `styles.css`'s two out-of-contract widths; **PR #354 (merge `d190a6f6`) closed the remaining two.** Re-verified at `34c9167e`: every `@media` width feature across all four production stylesheets is one of 480/720/880/1040 (`styles.css` 402, 861, 965, 979, 1030, 1148, 1229; `web.css` 327, 331, 370; `onboarding.css` 315; `setup.css` 135) — the only other `@media` rules are `prefers-reduced-motion` (`web.css:30`) and `print` (`styles.css:403`). `e2e/breakpoint-contract.js` **discovers** the stylesheet set from every `<link rel="stylesheet">` in `index.html` and `setup.html`, so a newly linked stylesheet is under contract immediately. `e2e/breakpoint-boundaries.js` proves the **browser** collapses and restores one pixel each side of all four tokens using the real stylesheets and the real `<nav class="side-nav">`. Registered at `hockey-scheduler-ci.yml:278` (standalone) and shard 1 (line 318). | **Merged** |
| Keyboard-only (manual pass) | `docs/product/manual-keyboard-screenreader-validation-protocol.md` (PR #350, merge `e8c7d96d`) — a **protocol and blank evidence template only**, whose own Status section states no validation has been performed under it. Re-verified at `34c9167e`: no filled-in artifact exists under `docs/`. **The K5/S5 defect that blocked this pass is fixed** by #394 (§3.9, resolved), so the pass is now runnable and waits only on a human. | **Human-only, unperformed** |
| Screen-reader (manual pass) | Same protocol, same disclaimer, same absence. `e2e/setup-state-matrix.js:136`–`139` states its own boundary: *"This journey drives a real browser with real keyboard events. It is NOT a screen-reader session and NOT a moderated human session."* #394 additionally added an S5 step asking the validator to record **how a non-sighted operator learns the content re-filtered** — the repaint is silent today, and that gap was deliberately left to be logged by the session rather than papered over with an unvalidated live-region announcement. | **Human-only, unperformed** |
| WCAG 2.2 AA — **automated repository accessibility** | **Merged.** `axe-core@^4.12.1` is a declared `devDependency` (`e2e/package.json:64`), called by exactly three journeys: `e2e/setup-accessibility-axe-gate.js:161`, `e2e/shell-accessibility-coverage.js:189`, `e2e/home-tasks-hub.js:183`. **PR #362 (issue #359), merge `9eadfb85`**, added the always-run gate reaching **twelve** surfaces through real clicks/navigation — signed-out login, anonymous public schedule, authenticated Home/Tasks, the Setup hub, **each of the six Setup workflow landings**, a forced 502, and an Official's restricted early-return — requiring zero serious/critical axe violations, zero console/page errors, no dangling skip-link target, no stale page title, no hidden focused control, at both viewports. Registered shard 4 (line 324). **This is automated repository accessibility. It is not, and is not evidence for, the two manual rows above.** | **Merged** |
| Zero-console-error | **Re-derived at `34c9167e`** (see §3.8 for the derivation rule and why these counts must never be copied forward). `e2e/` contains **64** `*.js` files. **57** are real Playwright journeys. **56 of those 57** install both `page.on("pageerror", …)` and `page.on("console", …)`. The single exception is `e2e/api-error-resilience.js`, which installs `pageerror` (line 49) only, by design — it deliberately provokes 401/403/502. The seven non-journey files (`breakpoint-contract.js`, `check-pr-body.js`, `check-v1-route-contract.js`, `ci-classify.js`, `ci-classify.test.js`, `ci-classify.integration.test.js`, `season-fmt-unit.js`) are static/unit checks. **The durable invariant holds again: the 57 Playwright journeys and the 57 names registered across the four browser-smoke shards are the same set exactly** — no orphan journey CI never runs, no registered name without a file. Both #365 matrix journeys additionally run a **delivery reconciler** (`reconcileDeliveries()`, `e2e/home-tasks-state-matrix.js:303`) keyed to (method, URL, status), consumed at most once, with unmatched responses failing the run. | **Merged** |

**The instrumentation now exists on `main`.** PR #395 merged the facilitator
pack: seeded environments per role with verifying pre-flights, K1–K17 and
S1–S14 reproduced byte-exact from the protocol, de-hinted task prompts, and
capture sheets that forbid participant identity. It is blank — no result field,
timing, rating, quote or sign-off is filled — so it is not evidence and does
not advance this criterion. What it removes is the last reason the passes could
not be *run*.

**Overall status for Criterion 7: `Human-only / unperformed`.** Four of the six
required evidence types are merged with citations that resolve. The two that
remain — **manual keyboard-only and manual screen-reader passes** — have zero
human evidence and cannot be produced by merging any PR. **Round 6's second
reason is now discharged**: the protocol no longer instructs the validator to
expect the opposite of shipped behaviour (§3.9, resolved by #394), so these
two rows are blocked on nothing but the passes being performed.

### Criterion 8 — "All three moderated operator-validation sessions are completed and documented."

| | |
| --- | --- |
| Required boundary/evidence | Three real moderated sessions (League Admin, Arena Manager, Coach) — commissioned, run, and documented with completion, timing, interventions, ease rating, and confusion quotes. Not waived, not simulated. |
| Evidence merged to `main` | `docs/product/moderated-operator-validation-protocol.md` (PR #349, merge `9d090fe6`) — a protocol and blank evidence-template document whose own Status section states verbatim: *"Protocol and evidence templates only. No moderated session has been run under this document."* A full listing of `docs/` at `34c9167e` shows that **blank capture sheets now exist and are still blank**: PR #395 added `docs/product/345-human-validation/09`–`11-capture-sheet-*.md`, one per role. Every result field, timing, intervention count, ease rating, quote and sign-off in them is empty. A reader listing `docs/` will now find session-shaped files where previously there were none — they are instrumentation, not evidence, and **no filled-in session artifact exists**. |
| Remaining gap | **The three sessions themselves, and nothing else.** Round 8 recorded a second, non-human gate — the facilitator pack's ease-rating wording was unruled, so `check_pack.py --session-readiness` refused to start a moderated session. **PR [#396](https://github.com/jingizoo/biknik/pull/396) merged as `34c9167e` and discharged it**: the wording is ratified, `--session-readiness` exits 0 at this snapshot's SHA, and the pack's pre-flight no longer blocks. What remains is **human-only** work no code change can satisfy. |
| **Status** | **`Human-only / unperformed`**. **Publishing the protocol (#349) is not performing it.** |

### Criterion 9 — "Memory, SQLite, PostgreSQL, authenticated HTTP where relevant, and all required browser CI are green."

| | |
| --- | --- |
| Required boundary/evidence | The full backend matrix (Memory/SQLite/PostgreSQL), authenticated HTTP where relevant, and all required browser CI green at the inspected head. |
| Evidence merged to `main` | **`main` at `34c9167e` — its current tip — is green.** Workflow run [31067094378](https://github.com/jingizoo/biknik/actions/runs/31067094378), push to `main`, concluded `success` at `2026-08-06T03:13:15Z`, all ten jobs green: `changes`, `classifier-test`, `frontend-check`, `test` (Memory/SQLite), `postgres`, and browser-smoke shards 1–4 covering **57 registered journeys** (`hockey-scheduler-ci.yml:318`–`324`). Authenticated-HTTP coverage sits inside the `test`/`postgres` jobs: `test_players_http_scope.py` (real `ThreadingHTTPServer`, real `Handler`, real session cookies, raw-response assertions), `test_context_league_http.py`, `test_league_context_http.py`, `test_server_authz.py`. Every merged batch in the table above was green on its own exact head at merge time. |
| Remaining gap | **This box is satisfied for the merged #345 work at this SHA only.** It is not a statement that #345 may merge: #345's Done condition additionally requires *every* box to have evidence and the moderated sessions to be documented, and criteria 7 and 8 do not. This remains the most perishable row in the document — a claim about a moving branch. It was false for roughly six hours during Round 5's window, which is why Round 5 pinned behind the tip; it has now been true across two consecutive tips (`57cd84dc`, `36195faa`). |
| **Status** | **`Verified on main`** — at `34c9167e`, `main`'s tip, with no carve-out needed this round. Recorded reasoning: under this document's own vocabulary `Missing` means "no implementation and no pending PR addresses it", which is plainly false — a completed green CI run on the recorded SHA is exactly the evidence this box asks for. |

---

## 2. Required state-matrix inventory

Per `operator-ux-requirements.md` §5, across Home/Tasks and each of the six
Setup workflows. **`N/A` is used only where a merged journey leg *asserts*
inapplicability** — never to paper over an untested state. **Both matrix
journeys are byte-identical to their `71bad79f` state**, so all leg line
numbers are unchanged from Rounds 4–5; only the `app.js` and `service.py`
production citations moved.

Shared production symbols for the six Setup landings:
`SETUP_WORKFLOWS` (`app.js:4673`), `buildSetupWorkflowCardModel()`
(`app.js:5327`), `setupCardBodyHtml()` (`app.js:5564`), `setupHubRollup()`
(`app.js:5454`), `retrySetupWorkflowCard()` (`app.js:5830`),
`resolveSetupCardConfirm()` (`app.js:5949`), `commitCardState()`
(`app.js:1162`), `readCardState()` (`app.js:1218`).

### Home/Tasks hub — `renderSetupProgressCard()` (`app.js:1503`), `loadSetupProgressCard()` (`app.js:1784`), `buildTasksCardModel()` (`app.js:1394`)

Journey: `e2e/home-tasks-state-matrix.js` (PR #377, merge `71bad79f`),
desktop + 390×844. Also `e2e/home-tasks-hub.js` (PR #331, merge `16fe833`).

| State | Production entry point/symbol | Merged journey leg | Missing behavior/evidence |
| --- | --- | --- | --- |
| Loading | `CARD_STATE.LOADING` (`app.js:660`); heading "Setup progress", visually-hidden "Loading setup progress…" | **Leg 1a** — `home-tasks-state-matrix.js:1315` | None identified |
| Empty | `buildTasksCardModel()` (`app.js:1394`) returns `EMPTY` with a named `reason` — `"no_program"` (`app.js:1410`) or `"nothing_actionable"` (`app.js:1438`); renderer branches at `app.js:1627`–`1650`. Two reasons, each with its own `<h3>`: "Setup progress — no program yet" (offers `Start Initial Setup` only under `canBootstrap`, `app.js:1646`) and "Setup progress — nothing for your role to do" | **Leg 1h** — `home-tasks-state-matrix.js:1181` | None identified |
| Stale | `cardTupleCurrent()` (`app.js:1150`); heading "Setup progress — showing earlier data" | **Legs 1f** (`:1556`) and **3** (`:1646`) | None identified |
| Per-card error + retry | `CARD_STATE.ERROR`; heading "Setup progress unavailable", sentence "Could not load your setup progress.", `data-setup-progress-retry` (`app.js:1584,1583`, wired `:1828`); focus after retry via `focusCardTarget()` (`app.js:1292`) | **Legs 1c/1d/1e** — `home-tasks-state-matrix.js:1435`, including **keyboard-activated** retry with exact focus asserted | None. The matrix **found and fixed a real defect here**: a keyboard-activated Retry resolving into `EMPTY` dropped focus onto `<body>` |
| Confirmation | — | **Leg 1i** — `home-tasks-state-matrix.js:2110`: the card is a pure read, and the leg requires that **no** confirmation/pending markup ever appears in the slot, in any state | **N/A, asserted not skipped** |
| Success/complete | `CARD_STATE.SUCCESS`; heading "✓ All setup steps complete" | **Leg 1g** — `home-tasks-state-matrix.js:1375` | None identified |
| Optional (Workflow 6) | `tasksWorkflowRowsHtml()` (`app.js:1460`) renders the "Optional" badge from `w.optional` (`app.js:1452`–`1454`), set by `partitionSetupWorkflows()` (`app.js:1310`, flag at `:1297`) from `CARD_STATUS.OPTIONAL` (`app.js:653`), backend-owned (`service.py:2003`, `"status": "optional"`). The badge and the completion arithmetic read the **same flag**; the SUCCESS branch reads `model.part.optional[0]` (`app.js:1686`) rather than the string `"import"` | `home-tasks-state-matrix.js`; `e2e/home-tasks-hub.js` | None identified |

### The six Setup workflow landings

Journey: `e2e/setup-state-matrix.js` (PR #377, merge `71bad79f`), desktop +
390×844, all six workflows declared in one registry at
`setup-state-matrix.js:251`–`313`. Every state is reached through a **real**
production entry point with the transport forced by route interception.

| State | Production behavior asserted | Merged journey leg | Coverage / N/A |
| --- | --- | --- | --- |
| Loading | Skeleton with visually-hidden label, `aria-busy="true"`, and **zero controls** on both the card body and the landing's action groups — with the action container asserted **present**, so "no buttons" can never be satisfied by a missing container | `legErrorAndLoadingPerLanding()` — `:1558` (leg 5) | All six. Workflow 6's LOADING is reached separately in leg 7a |
| Empty | The sentence **names what is missing** and the unmet prerequisite, and **exactly one** action is offered — derived from `_workflow_prerequisite_rows()` (`service.py:2124`), not declared | `legEmptyPristine()` — `:1179` (leg 1) | Five required workflows. **`import`: N/A, asserted** via `legOptionalCannotFail()` |
| Stale | Retained counts labelled as earlier data, a Refresh in the card body, every landing action group withdrawn, and `contextSwitchIntentPending` **already false** — so the withdrawal is attributable to STALE and not to the switch | `legStaleAndContextRace()` — `:1815` (legs 7a/7b) | All six. The **delayed-stale race** is N/A for `import`, asserted in 7b by byte-comparing its committed model across both tuples |
| Per-card error + retry | Exact error sentences asserted as strings. Retry is **reached by tabbing** and activated with **Enter**; the announcement and the **exact focused element** are asserted on both outcomes | `legErrorAndLoadingPerLanding()` (`:1558`) and `legHubNeighbourIsolation()` (`:1442`) | All six. **Neighbour isolation**: a per-card retry that *fails* leaves every other card's generation, committed model and painted body **byte-identical** |
| Confirmation | Both declared confirmations driven by keyboard, with exact focus asserted on open, cancel, blank-reason refusal and completion; the live region read with a `MutationObserver` | `legConfirmImport()` — `:2067`; `legConfirmReopen()` — `:2220` | `import`, `facilities`, `participation`. **`league_season`, `teams`, `roster`: N/A, asserted** by leg 8d (`:2313`) under **both** an ordinary and an archived Season |
| Success/complete | "✓ This workflow is set up. You can still add more whenever you need to." | `legSuccessComplete()` — `:1268` (leg 2) | All six |
| Optional | Workflow 6 stays visible, reachable, neither done nor todo, **never** the roll-up's `next` and **never** its `blockedBy` | `legOptionalCannotFail()` — `:1383`, and `assertWorkflowSixInvariants()` — `:1331` | `import`. **N/A for the other five, by definition** |

**Cross-cutting properties, all asserted:** one failed card beside successful
cards (leg 4); failed retry then successful retry scoped to that card (leg 4);
delayed stale success after a newer failure (`legRaceAfterNewerFailure()`,
`:1680`) and after a context switch (leg 7b); zero console errors, through the
delivery reconciler.

**Anti-vacuity rule enforced by the journey itself** (`setup-state-matrix.js:167`–`174`):
every negative assertion is paired with the positive control that proves it
could have failed.

**All seven surfaces and all seven states are accounted for above.** No cell is
blank and no cell is unexplained.

---

## 3. Stale or contradictory claims found (called out, not silently reconciled)

**1. RESOLVED — the context caption was FALSE, and this ledger understated it
for two rounds.** `index.html` told the operator *"most existing screens
(Games, Roster, Standings, etc.) are not filtered by this selection"*. All
three named screens were filtered:

- **Games** — `renderGames()` (`app.js:7527`) renders `ov.schedule`;
  `get_demo_overview()` (`service.py:7075`) resolves the active tuple and
  excludes every game failing `_in_scope_game()`.
- **Roster** — the selectable/current game is revalidated against that same
  scoped `ov.schedule` before the per-game lineup reads run.
- **Standings** — `get_standings()` (`service.py:4548`) requires the Division
  to match the active Program and exact Season, plus the selected League.

**Fixed by PR [#394](https://github.com/jingizoo/biknik/pull/394), merge
`36195faa`.** `index.html:199` now reads *"Switching this changes what you
see"* — capability wording that names no screens, so it cannot rot as more
surfaces become context-aware. It is wired `aria-describedby` on both selects
(`index.html:192`, `:195`); previously a keyboard user heard the scope
statement not at all, static topbar text being browse-mode only.

**Retained as a record of this document's own failure, not as an open
defect.** Rounds 4 and 5 both wrote that "Games remains genuinely unfiltered,
so the caption is not wholly wrong". Both were wrong, and the understatement
is what let false operator-facing copy survive two rounds of an audit whose
entire purpose is catching overstated claims. The merged fix carries the
lesson in the markup itself: a comment at `index.html:166`–`176` records that
**both** prior revisions enumerated screens and both rotted, and that a list
of what is filtered is a promise about a moving inventory while a sentence
about what the control *does* is not.

**2. Round 4 cited one symbol at two different line numbers.**
`buildTasksCardModel()` was cited as `app.js:1331` in criterion 5 and
`app.js:1346` in §2's header — the same function at two addresses. Verified
against `71bad79f`: it was at **1331**; the §2 header's `1346` resolved to
unrelated code. Both now read `app.js:1394`.

**3. A previously cited code branch no longer exists.** The Round 3 revision
recorded Home/Tasks' Empty state as `!progress || !progress.program_id`
returning `""`. That branch is gone: the test lives in `buildTasksCardModel()`
(`app.js:1394`) and returns `CARD_STATE.EMPTY` with a named reason. The merged
comment at `app.js:1609` says so: *"Both reasons used to return the empty
string."* Two `return ""` paths do still exist in `renderSetupProgressCard()`
— `app.js:1568` (a *held* EMPTY carries no retained read) and `app.js:1758`
(an unreachable-by-design fallthrough on `model.nextBlocked`, `app.js:1757`) —
but neither is the branch previously cited.

**4. Two symbols this ledger previously cited no longer exist.**
`setupProgressFetchSeq` was removed by #365 — the name survives only in
explanatory comments (`app.js:175, 610, 681, 1751, 1927, 13737`), and
`app.js:1778` states the replacement verbatim. `setupSummaryHtml()` was
removed in the same work; the shared renderer is now `setupCardBodyHtml()`
(`app.js:5564`). **The Round 4 follow-up is still open**: `setupSummaryHtml`
is still named as if live in `e2e/setup-v2-context-scope.js:763` and
`e2e/league-filtered-data.js:278`.

**5. PR #354 merged**; the Round 3 revision recorded it as an open,
conflicting draft. Accurate then, superseded now (merge `d190a6f6`,
2026-07-28T09:50:40Z). Its overlap with #351 was resolved by making the
stylesheet set **discovered** from the production HTML rather than declared.

**6. `ROADMAP.md` still describes #345 as one undifferentiated deliverable.**
Re-verified at `34c9167e`: its "Currently active sequencing" section
(`ROADMAP.md:276` onward, #345 at 290, 297, 301) does not reflect the batch
split across fourteen-plus merged pieces. Flagged for the owner; out of scope
for this PR.

**7. #345 was auto-closed by a merged PR and had to be reopened by hand.**
Closed `2026-07-27T11:59:41Z`, **two seconds after PR #347 merged** at
`11:59:39Z` — a closing keyword against the epic — and reopened by the owner
68 minutes later. **The same class recurred on #206**: closed
`2026-08-03T06:56:32Z`, reopened `2026-08-03T15:20:49Z`, unnoticed for over
eight hours. A child PR must use `Part of #<epic>`, never a closing keyword.

**8. The zero-console-error inventory held still this round while its
MEMBERSHIP changed — which is the clearest argument yet for the invariant over
the counts.** It drifted in four consecutive rounds — 35/34, then 59/53/52, then
62/55/54, then 63/56/55, then 64/57/56 — and this round it is **64/57/56
again**, byte-for-byte
the same triple, because #398 RENAMED a journey (`scheduler-meetings-format.js`
→ `scheduler-games-per-team.js`) rather than adding one. A check on the counts
alone would have seen nothing at all while the registered set changed under it.
The
*identified exception* (`api-error-resilience.js`) has been correct every
time; only the arithmetic drifts, because any new `e2e/*.js` file changes it
(the previous round's addition was `context-scope-truth`, from #394). These numbers
are **derived, not authored**: enumerate `hockey-scheduler/e2e/*.js` at the
inspected SHA, test each for `require("playwright")`, test each journey for
both `page.on("pageerror"` and `page.on("console"`. The durable claim is the
**set equality** against the four browser-smoke `scripts:` lists
(`hockey-scheduler-ci.yml:318,320,322,324`) — **57 = 57** this round, no
orphans in either direction. Four rounds, four different count triples, one
unchanged invariant: that is the whole argument for preferring it. The raw counts are not durable and must never be copied
forward.

**9. RESOLVED — the manual-validation protocol instructed the validator to
expect the OPPOSITE of shipped behaviour, which blocked criterion 7's human
passes.** Before PR #394, `manual-keyboard-screenreader-validation-protocol.md`
told a human at steps **K5/S5** that no content re-filtered on a context
change, and recorded that as the **expected passing result**. Because the
product did re-filter, a conscientious validator following the protocol as
written would have produced a filled-in artifact asserting the opposite of what
shipped — and that artifact would have looked like valid #345 evidence. It was
a sharper form of §3.1: the document carrying the false claim was the *test
procedure itself*, so running the procedure could not have caught it.

**Fixed by PR #394, merge `36195faa`.** The protocol at the inspected base now
opens that section with an explicit **"Superseded — do not run the earlier
gating"** warning recording that the previous revision "told a validator that
context filtering was *not*" expected and "would have failed a correct
implementation, which is why K5/S5 are restated below"; K5 is restated around
**observed** filtering through `#ctx-select`; and the **Conflicts log** carries
a dated `2026-08-04` row recording that the earlier `2026-07-27` resolution
"inverted with the code and became the blocker". The prior row is preserved
rather than rewritten, so the inversion is auditable instead of erased.

Criterion 7's two manual rows are now blocked on nothing but being performed.
Recorded rather than deleted because it is the sharpest instance of this
document's recurring subject: **a false claim inside the instrument meant to
detect false claims.**

**10. RESOLVED — a supporting `app.js` comment was false in the same way.** A
current-tense comment stated `ov.schedule` is "every non-draft game in the
whole demo", which #369's scoping had falsified. Corrected by PR #394 along
with several sibling comments asserting the switcher was "display-only" and
that `/api/demo/overview` was unfiltered. Recorded because §3.1's caption was
the operator-facing face of a claim the code was repeating to its own
maintainers.

**11. NEW — this document's own snapshot timestamp was authored, not
measured, in Rounds 5 and 6.** Both rounds opened with *"Snapshot taken:
`<time>`. Every SHA, PR state, issue state, CI result … was re-verified
against the merged tree at that time"* — the field that certifies every other
value in the document. Both times were **in the future when written**:

| revision | stated | actual authoring time (commit `%cI`, UTC) | error |
| --- | --- | --- | --- |
| Round 5 | `2026-08-04T09:10Z` | `2026-08-04T03:39:13Z` | **+5h31m** |
| Round 6 | `2026-08-04T13:05Z` | `2026-08-04T10:37:21Z` | **+2h28m** |

The verification work those sentences describe genuinely happened; the
timestamps attached to it did not. Two rounds is a pattern, not a slip: a
plausible-looking value was composed where a measured one was required.

**This is the document's own thesis turned on the document.** Every §3 finding
above is some version of *a value that was rendered, declared, or argued
standing in for one that was asserted* — and the provenance stamp, the one
field a reader must trust before trusting anything else, was exactly that.
A reader who spot-checked a SHA would have found it correct; a reader who
checked the clock would have found the whole certification unverifiable.
Round 7's timestamp is read from the system clock at authoring time, and the
two false ones are corrected above rather than silently overwritten.

**Superseded findings removed rather than kept.** Round 5's finding that
`main`'s tip was red is resolved and moved to §"Snapshot semantics" as history.
A stale-claims list that is itself allowed to outdate becomes another false
claim — which is the exact failure mode §3.1 demonstrates this document is not
immune to.

---

## 4. Planned/open work (never completion evidence)

**Nothing in this section is evidence for any acceptance box.**

| Item | State at snapshot | Scope |
| --- | --- | --- |
| [#357](https://github.com/jingizoo/biknik/issues/357) | **OPEN** | Refresh this ledger as a current-`main` snapshot — this task |
| [#393](https://github.com/jingizoo/biknik/issues/393) | **OPEN** | Schedule/Facilities operator journey. Sequenced **after** #345 closes; **not #345 work**, and explicitly not a staleness trigger for this document |
| [#376](https://github.com/jingizoo/biknik/issues/376) | **OPEN** | Child of #31 — playoff bracket. **Not #345 work** |
| [#206](https://github.com/jingizoo/biknik/issues/206) | **OPEN** (reopened 2026-08-03) | Epic — scheduling planner v2. **Not #345 work.** Its children drove every merge since Round 4 |
| [#287](https://github.com/jingizoo/biknik/issues/287) | **OPEN** | Epic — substitute matching engine. Sequenced after #345 and #393 |

**Merged since Round 6**: PR #394 (`36195faa`) — **#345 work**, cited in §1
criterion 4 and retiring §3.1/§3.9/§3.10.

**Closed since Round 4, none of it #345 work**: #375, #379, #383 (duplicate of
#375), #386, #387, #390 — all #206 children — plus PR #392, the SQLite lock
fix that restored `main` to green.

**Closed and merged #345 children** (evidence cited in §1 against the merge
commit, never the issue number): #358 (via PR #361), #359 (#362), #360 (#363),
#364 (#366), #365 (#377), #367 (#369).

---

## 5. What remains before #345 can close

| Outstanding item | Criterion | Why it is not closed |
| --- | --- | --- |
| Manual keyboard-only validation pass | 7 | No human pass has been run. **Publishing a protocol is not performing it.** The K5/S5 correction that previously blocked this landed in #394, so the protocol is now safe to perform |
| Manual screen-reader validation pass | 7 | Same document, same disclaimer. The merged axe gate (#362) is **automated repository accessibility** and explicitly not a substitute — `setup-accessibility-axe-gate.js:14`–`16` records that boundary in the file itself. #394's new S5 step asks the validator to record how a non-sighted operator learns the content re-filtered; the repaint is silent today and that gap is to be **logged by the session**, not pre-answered |
| Three moderated operator sessions (League Admin, Arena Manager, Coach) | 8 | None run. Transferred to #345 as a merge gate, **not waived and not simulable** |

All three are human work that no PR can produce. **No automated run, no
merged protocol, and no revision of this document can convert them.** They are
the reason #345 remains open regardless of how much of §1 reads
`Verified on main`.

---

## Revision history

- **Round 1–2** (2026-07-27): initial ledger; live-blocker surfacing; status
  vocabulary; freshness rule.
- **Round 3** (2026-07-27T13:45Z): first post-merge refresh, after
  #347/#352/#353/#356.
- **Round 4** (2026-08-02T11:53:20Z): rebased onto `71bad79f`, fully
  re-verified against the merged tree rather than PR descriptions. Criteria 3,
  4, 5, 9 moved `Missing` → `Verified on main`; criterion 7 moved `Missing` →
  `Human-only / unperformed`.
- **Round 5** (stated `2026-08-04T09:10Z`; actually authored
  `2026-08-04T03:39:13Z` — see §3.11): re-pinned to `4365e477`, deliberately
  **behind** `main`'s tip because the tip (`29ca277d`) was red. All 111 line
  citations re-resolved. Four findings added (§3.2, §3.7, §3.8, and the red
  tip).
- **Round 6** (stated `2026-08-04T13:05Z`; actually authored
  `2026-08-04T10:37:21Z` — see §3.11): per issue
  [#357](https://github.com/jingizoo/biknik/issues/357)): re-pinned to
  **`57cd84dc`, `main`'s own tip, now green** — the red Round 5 carve-out is
  retired and recorded as history. **No acceptance status changed**; no #345
  work merged. All 111 citations re-resolved again (`app.js` +18, `service.py`
  +46/+149, CI shard lines, `styles.css`).<br><br>**The substantive change is a
  correction to this document, not to the repository.** §3.1 previously said
  Games remained unfiltered and the caption was therefore "not wholly wrong".
  Both Round 4 and Round 5 said it; both were wrong. All three named screens
  are filtered, the caption is false, and the understatement is what let it
  survive two rounds of an audit whose entire purpose is catching overstated
  claims. Two consequences follow: §3.9 records that the manual-validation
  protocol repeats the same falsehood as a *passing expectation*, which blocks
  criterion 7's human passes on a documentation fix before it blocks them on
  recruiting humans; and §5 now carries that correction as its own row. PR
  #394 is open against all of it and is cited as `Pending active PR` — the
  first use of that vocabulary value since Round 3.

- **Round 7** (`2026-08-04T15:26Z`): re-pinned to `36195faa`; first round to
  RETIRE findings as resolved (§3.1, §3.9, §3.10, all closed by #394); §3.11
  recorded that Rounds 5 and 6 both stated a snapshot timestamp that had not
  yet happened. Pushed once and rejected: it mixed Round 6 facts into
  current-snapshot assertions, and the citation checker was structurally blind
  to a document contradicting itself. A second checker now asserts
  base-consistency, single-inventory, no-present-tense-in-RESOLVED, and a
  non-future timestamp.

- **Round 8** (`2026-08-05T19:21Z`): pinned to `46402840`; #395 entered the base
  table; criterion 8 gained a second, non-human gate. Its pushed head carried
  four contradictions a reviewer found and its checkers did not: criterion 9
  cited a CI run belonging to the PREVIOUS base, the vocabulary note denied a
  value two rows carried, §3.8's drift sequence listed one triple twice, and
  the window count said nine where ten had merged.

- **Round 9** (`2026-08-06T03:16Z`, this revision, per issue
  [#357](https://github.com/jingizoo/biknik/issues/357)): re-pinned to
  **`34c9167e`**, `main`'s tip, green across all ten jobs, and fixed all four
  of Round 8's contradictions.<br><br>**The first re-pin in nine rounds that
  moved no citation at all.** #396 touched only the human-validation pack,
  which this document does not cite by line, so `app.js` and `service.py` are
  byte-identical between the two tips and every one of the 115 citations held.
  Every previous round spent most of its effort on drift; this one spent none.
  <br><br>**Criterion 8's second gate is discharged.** #396 ratified the
  ease-rating wording, so `--session-readiness` exits 0 at this SHA and the
  facilitator pack's pre-flight no longer refuses a moderated session.
  `Pending active PR` returns to unused, and **criteria 7 and 8 are now
  blocked on people and nothing else** — the first time in this document's
  history that no portion of either is waiting on a PR.<br><br>Two checks were
  added, each falsified before being trusted: a cited CI run must have executed
  on the pinned SHA — Round 8 certified `46402840` while citing a run of
  `36195faa`, every fact true of something and the conjunction false — and the
  status vocabulary may not deny a value the document uses.
